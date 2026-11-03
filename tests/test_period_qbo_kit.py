"""The whole QBO kit, and the replay that proves it reproduces our books."""

import csv
import dataclasses
import io
import re
from decimal import Decimal

import pytest

from runningtab.chart import load_chart
from runningtab.period import PeriodError
from runningtab.period.balances import trial_balance
from runningtab.period.ledger import index_accounts
from runningtab.period.qbo import build_qbo_kit, check_replay, journal_entries, opening_entries
from runningtab.period.qbo_map import CLEARING_ACCOUNT_NAME, load_qbo_map
from runningtab.period.quarter import Quarter, fiscal_year_for
from tests.conftest import EXAMPLE_BOOKS, PERIOD_ABBR, period_fiscal_years, period_gl_rows

Q3 = Quarter.parse("2026Q3")
INDEX = index_accounts(load_chart(EXAMPLE_BOOKS.chart_path), PERIOD_ABBR)


def _tb():
    return trial_balance(period_gl_rows(), INDEX, Q3, fiscal_year_for(Q3, period_fiscal_years()))


def _kit():
    return build_qbo_kit(
        _tb(), period_gl_rows(), INDEX, load_chart(EXAMPLE_BOOKS.chart_path), load_qbo_map(EXAMPLE_BOOKS.qbo_path), Q3
    )


def _csv(data: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8"))))


def test_the_kit_holds_every_file_in_import_order():
    assert sorted(_kit().files) == [
        "accounts.csv",
        "customers.csv",
        "journal-entries-01.csv",
        "opening-balances.csv",
        "vendors.csv",
    ]


def test_opening_plus_journals_reproduce_the_trial_balance():
    entries = [
        *opening_entries(_tb(), period_gl_rows(), INDEX, load_qbo_map(EXAMPLE_BOOKS.qbo_path)),
        *journal_entries(period_gl_rows(), INDEX, load_qbo_map(EXAMPLE_BOOKS.qbo_path), Q3),
    ]
    check_replay(entries, _tb(), load_qbo_map(EXAMPLE_BOOKS.qbo_path))


def test_a_corrupted_kit_fails_the_replay_naming_the_account():
    entries = journal_entries(period_gl_rows(), INDEX, load_qbo_map(EXAMPLE_BOOKS.qbo_path), Q3)
    first = entries[0]
    bent = dataclasses.replace(
        first, lines=(dataclasses.replace(first.lines[0], net=first.lines[0].net + Decimal("1")), *first.lines[1:])
    )
    # `re.escape`: the bent line's account is a QBO builtin name with literal
    # parens ("Accounts Receivable (A/R)"), which `match` would otherwise
    # read as a regex group rather than the literal string to find.
    with pytest.raises(PeriodError, match=re.escape(first.lines[0].account)):
        check_replay(
            [
                *opening_entries(_tb(), period_gl_rows(), INDEX, load_qbo_map(EXAMPLE_BOOKS.qbo_path)),
                bent,
                *entries[1:],
            ],
            _tb(),
            load_qbo_map(EXAMPLE_BOOKS.qbo_path),
        )


def test_a_voucher_renamed_to_a_split_pieces_journal_no_is_refused():
    # journal_entries's own collision check compares voucher numbers before
    # splitting adds "/1", "/2" -- ACC-JV-2026-00011 has two AR lines and
    # splits into ".../1" and ".../2". A voucher literally named
    # "ACC-JV-2026-00011/1" passes that check (its own voucher_no is unique
    # among the *original* numbers) but collides with the split piece once
    # both land in the kit with their finished Journal No.
    rows = period_gl_rows()
    collider_no = "ACC-JV-2026-00011/1"
    collider = [dataclasses.replace(r, voucher_no=collider_no) for r in rows if r.voucher_no == "ACC-JV-2026-00007"]
    altered = [r for r in rows if r.voucher_no != "ACC-JV-2026-00007"] + collider
    with pytest.raises(PeriodError, match=re.escape(collider_no)):
        build_qbo_kit(
            _tb(), altered, INDEX, load_chart(EXAMPLE_BOOKS.chart_path), load_qbo_map(EXAMPLE_BOOKS.qbo_path), Q3
        )


def test_customers_and_vendors_are_the_parties_on_ar_and_ap_lines():
    kit = _kit()
    assert [r["Name"] for r in _csv(kit.files["customers.csv"])] == sorted(['"Best" Bikes', "Müller, Inc."])
    assert [r["Name"] for r in _csv(kit.files["vendors.csv"])] == ["Cloud Co"]


def test_customers_vendors_and_accounts_are_size_checked(monkeypatch):
    # customers.csv, vendors.csv, and accounts.csv all bypass
    # check_opening_size/chunk entirely (they carry a party or account list,
    # not entries) -- this proves build_qbo_kit still runs check_file_size
    # against all three, rather than only proving check_file_size works in
    # isolation.
    from runningtab.period import qbo

    checked: list[str] = []
    real = qbo.check_file_size

    def recording(name, *args, **kwargs):
        checked.append(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(qbo, "check_file_size", recording)
    _kit()
    assert set(checked) == {"customers.csv", "vendors.csv", "accounts.csv"}


def test_accounts_exclude_builtins_and_include_clearing():
    rows = _csv(_kit().files["accounts.csv"])
    names = [r["Account Name"] for r in rows]
    assert names[-1] == CLEARING_ACCOUNT_NAME
    assert not set(load_qbo_map(EXAMPLE_BOOKS.qbo_path).builtin.values()) & set(names)


def test_the_split_voucher_is_reported():
    assert len(_kit().split_vouchers) == 1


def test_renaming_a_voucher_to_contain_a_slash_does_not_misreport_it_as_split():
    # split_vouchers used to key off "/" appearing in journal_no; a voucher
    # whose own number contains "/" -- nothing in ERPNext prevents that --
    # would then be misreported as split even though it produced exactly
    # one QboEntry. Counting entries per source voucher instead of scanning
    # the character catches this: renamed here, this voucher produces one
    # entry and must not be reported; ACC-JV-2026-00011 (two AR lines,
    # genuinely split) still must be.
    rows = period_gl_rows()
    renamed_no = "ACC-JV-2026/00007"
    altered = [
        dataclasses.replace(r, voucher_no=renamed_no) if r.voucher_no == "ACC-JV-2026-00007" else r for r in rows
    ]
    kit = build_qbo_kit(
        _tb(), altered, INDEX, load_chart(EXAMPLE_BOOKS.chart_path), load_qbo_map(EXAMPLE_BOOKS.qbo_path), Q3
    )
    assert renamed_no not in kit.split_vouchers
    assert "ACC-JV-2026-00011" in kit.split_vouchers


def test_opening_balances_open_the_receivable_by_party():
    lines = [
        line
        for e in opening_entries(_tb(), period_gl_rows(), INDEX, load_qbo_map(EXAMPLE_BOOKS.qbo_path))
        for line in e.lines
        if line.is_ar_ap
    ]
    assert [(line.party, line.net) for line in lines] == [("Müller, Inc.", Decimal("900.00"))]


def test_a_second_postable_receivable_account_is_refused_naming_both():
    # QBO has exactly one Accounts Receivable; two postable Receivable
    # accounts in chart.coa can't be told apart once their lines land there,
    # so this must be refused by name rather than one silently winning.
    receivable = next(a for a in INDEX.values() if a.account_type == "Receivable")
    extra = dataclasses.replace(receivable, number="9999", name="Spare Receivable")
    index = {**INDEX, "Spare Receivable - duplicate": extra}
    with pytest.raises(PeriodError) as excinfo:
        opening_entries(_tb(), period_gl_rows(), index, load_qbo_map(EXAMPLE_BOOKS.qbo_path))
    assert receivable.number in str(excinfo.value)
    assert extra.number in str(excinfo.value)


def test_an_index_with_no_payable_account_is_refused_naming_payable():
    index = {name: a for name, a in INDEX.items() if a.account_type != "Payable"}
    with pytest.raises(PeriodError, match="Payable"):
        opening_entries(_tb(), period_gl_rows(), index, load_qbo_map(EXAMPLE_BOOKS.qbo_path))
