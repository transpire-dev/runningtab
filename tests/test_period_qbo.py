"""The QBO journal entries: one per voucher, split where QBO's one-AR-line rule demands."""

import csv
import dataclasses
import datetime as dt
import io
import re
from decimal import Decimal

import pytest

from runningtab.chart import load_chart
from runningtab.period import PeriodError
from runningtab.period.balances import trial_balance
from runningtab.period.ledger import index_accounts
from runningtab.period.qbo import (
    CLEARING_ACCOUNT_NAME,
    QBO_DATE_FORMAT,
    QboLine,
    accounts_csv,
    check_file_size,
    check_opening_size,
    chunk,
    journal_csv,
    journal_entries,
    opening_entries,
    parties_csv,
    split_entry,
)
from runningtab.period.qbo_map import load_qbo_map
from runningtab.period.quarter import Quarter, fiscal_year_for
from tests.conftest import EXAMPLE_BOOKS, PERIOD_ABBR, period_fiscal_years, period_gl_rows

Q3 = Quarter.parse("2026Q3")
INDEX = index_accounts(load_chart(EXAMPLE_BOOKS.chart_path), PERIOD_ABBR)


def _entries():
    return journal_entries(period_gl_rows(), INDEX, load_qbo_map(EXAMPLE_BOOKS.qbo_path), Q3)


def test_one_entry_per_voucher_in_the_quarter_plus_the_split():
    # Five vouchers in Q3 (JEs #7-#11); #11 has two AR lines and becomes two entries.
    assert len(_entries()) == 6


def test_the_two_customer_voucher_splits_through_clearing_and_nets_to_zero():
    split = [e for e in _entries() if "/" in e.journal_no]
    assert [e.journal_no.rsplit("/", 1)[1] for e in split] == ["1", "2"]
    clearing = [line.net for e in split for line in e.lines if line.account == CLEARING_ACCOUNT_NAME]
    assert sum(clearing, Decimal(0)) == 0
    for entry in split:
        assert sum(1 for line in entry.lines if line.is_ar_ap) == 1
        assert entry.lines[0].is_ar_ap
        assert sum((line.net for line in entry.lines), Decimal(0)) == 0


def test_a_split_with_other_lines_keeps_them_in_the_first_entry():
    lines = [
        QboLine("Revenue", Decimal("-50"), "", None, False),
        QboLine("A/R", Decimal("30"), "", "A", True),
        QboLine("A/R", Decimal("20"), "", "B", True),
    ]
    first, second = split_entry("JV-1", dt.date(2026, 9, 1), lines, "JV-1")
    assert [line.account for line in first.lines] == ["A/R", "Revenue", CLEARING_ACCOUNT_NAME]
    assert first.lines[-1].net == Decimal("20")
    assert [(line.account, line.net) for line in second.lines] == [
        ("A/R", Decimal("20")),
        (CLEARING_ACCOUNT_NAME, Decimal("-20")),
    ]


def test_a_voucher_spanning_two_dates_is_refused():
    rows = period_gl_rows()
    last = rows[-1]
    moved = dataclasses.replace(last, posting_date=last.posting_date - dt.timedelta(days=1))
    with pytest.raises(PeriodError, match=last.voucher_no):
        journal_entries([*rows[:-1], moved], INDEX, load_qbo_map(EXAMPLE_BOOKS.qbo_path), Q3)


def test_a_voucher_straddling_the_quarter_boundary_is_refused():
    # Grouping and the spanning check must run on every row of a voucher
    # before the quarter filter, not after: filtering first would keep only
    # this voucher's in-quarter row and hand back a silently unbalanced
    # single-line entry instead of refusing it.
    rows = period_gl_rows()
    voucher = "ACC-JV-2026-00007"
    idx = next(i for i, r in enumerate(rows) if r.voucher_no == voucher)
    moved = dataclasses.replace(rows[idx], posting_date=dt.date(2026, 6, 30))
    altered = [*rows[:idx], moved, *rows[idx + 1 :]]
    with pytest.raises(PeriodError, match=voucher):
        journal_entries(altered, INDEX, load_qbo_map(EXAMPLE_BOOKS.qbo_path), Q3)


def test_a_pre_quarter_voucher_spanning_two_dates_does_not_block_the_export():
    # A voucher with no row in the quarter at all -- here, one entirely
    # before it -- must not trip the spanning check just because its own
    # history happens to touch more than one date; it only feeds
    # opening_entries's running balance and never journal_entries's output
    # either way.
    rows = period_gl_rows()
    voucher = "ACC-JV-2026-00006"
    idx = next(i for i, r in enumerate(rows) if r.voucher_no == voucher)
    assert rows[idx].posting_date < Q3.start
    moved = dataclasses.replace(rows[idx], posting_date=rows[idx].posting_date - dt.timedelta(days=1))
    altered = [*rows[:idx], moved, *rows[idx + 1 :]]
    entries = journal_entries(altered, INDEX, load_qbo_map(EXAMPLE_BOOKS.qbo_path), Q3)
    assert voucher not in {e.journal_no for e in entries}


def test_two_voucher_types_sharing_a_number_are_refused():
    # Grouping by (voucher_type, voucher_no) keeps these two vouchers' GL
    # rows apart internally -- they never merge into one entry -- but the QBO
    # CSV's Journal No. column is voucher_no alone, so the same number would
    # still collide once both entries land in the file. Refused by name
    # rather than shipped as a kit QBO would read back as one merged entry.
    rows = period_gl_rows()
    voucher = "ACC-JV-2026-00007"
    same_number_other_type = [
        dataclasses.replace(r, voucher_type="Purchase Invoice", name=f"{r.name}-pi")
        for r in rows
        if r.voucher_no == voucher
    ]
    with pytest.raises(PeriodError, match=voucher) as exc:
        journal_entries([*rows, *same_number_other_type], INDEX, load_qbo_map(EXAMPLE_BOOKS.qbo_path), Q3)
    assert "Journal Entry" in str(exc.value)
    assert "Purchase Invoice" in str(exc.value)


def test_an_entry_that_does_not_balance_is_refused():
    # A backstop: nothing downstream would otherwise catch an unbalanced
    # entry reaching the CSV.
    rows = period_gl_rows()
    voucher = "ACC-JV-2026-00007"
    idx = next(i for i, r in enumerate(rows) if r.voucher_no == voucher)
    broken = dataclasses.replace(rows[idx], debit=rows[idx].debit + Decimal("1"))
    altered = [*rows[:idx], broken, *rows[idx + 1 :]]
    with pytest.raises(PeriodError, match=voucher):
        journal_entries(altered, INDEX, load_qbo_map(EXAMPLE_BOOKS.qbo_path), Q3)


def test_opening_balances_over_the_row_cap_are_refused():
    tb = trial_balance(period_gl_rows(), INDEX, Q3, fiscal_year_for(Q3, period_fiscal_years()))
    opening = opening_entries(tb, period_gl_rows(), INDEX, load_qbo_map(EXAMPLE_BOOKS.qbo_path))
    total = sum(len(e.lines) for e in opening)
    with pytest.raises(PeriodError, match=str(total)):
        check_opening_size(opening, max_rows=total - 1)
    check_opening_size(opening, max_rows=total)  # exactly at the cap is fine


def test_opening_balances_over_the_byte_cap_are_refused():
    # Intuit's limit is 1,000 rows AND 2 MB per file -- the row cap alone
    # would pass an opening kit whose remarks or party names blow past 2 MB.
    tb = trial_balance(period_gl_rows(), INDEX, Q3, fiscal_year_for(Q3, period_fiscal_years()))
    opening = opening_entries(tb, period_gl_rows(), INDEX, load_qbo_map(EXAMPLE_BOOKS.qbo_path))
    total_bytes = len(journal_csv(opening))
    with pytest.raises(PeriodError, match=str(total_bytes)):
        check_opening_size(opening, max_bytes=total_bytes - 1)
    check_opening_size(opening, max_bytes=total_bytes)  # exactly at the cap is fine


def test_files_split_at_entry_boundaries_under_the_row_cap():
    entries = _entries()
    files = chunk(entries, max_rows=5)
    assert all(sum(len(e.lines) for e in f) <= 5 for f in files)
    assert [e for f in files for e in f] == entries


def test_files_split_at_entry_boundaries_under_the_byte_cap():
    entries = _entries()
    # A cap small enough that at least one real entry alone would still fit,
    # but two together would not -- forces a split purely on rendered size,
    # with the row cap wide open.
    one_entry_bytes = max(len(journal_csv([e])) for e in entries)
    files = chunk(entries, max_rows=len(entries) * 10, max_bytes=one_entry_bytes + 50)
    assert len(files) > 1
    assert all(len(journal_csv(f)) <= one_entry_bytes + 50 for f in files)
    assert [e for f in files for e in f] == entries


def test_an_entry_longer_than_a_file_is_refused():
    entry = _entries()[0]
    with pytest.raises(PeriodError, match=entry.journal_no):
        chunk([entry], max_rows=1)


def test_an_entry_whose_rendered_size_alone_exceeds_the_byte_cap_is_refused():
    entry = _entries()[0]
    header_bytes = len(journal_csv([]))
    entry_bytes = len(journal_csv([entry])) - header_bytes
    with pytest.raises(PeriodError, match=entry.journal_no):
        chunk([entry], max_bytes=header_bytes + entry_bytes - 1)
    chunk([entry], max_bytes=header_bytes + entry_bytes)  # exactly at the cap is fine


def test_a_party_file_over_the_row_cap_is_refused_naming_it():
    # customers.csv and vendors.csv carry a plain party list, not
    # QboEntry-shaped rows -- check_opening_size and chunk can't measure
    # them, so this is the caps' own check for a finished file.
    parties = [f"Party {n}" for n in range(5)]
    data = parties_csv(parties, "customers.csv")
    with pytest.raises(PeriodError, match=r"customers\.csv needs 5 rows"):
        check_file_size("customers.csv", len(set(parties)), data, max_rows=4)
    check_file_size("customers.csv", len(set(parties)), data, max_rows=5)  # exactly at the cap is fine


def test_a_party_file_over_the_byte_cap_is_refused_naming_it():
    parties = [f"Party {n}" for n in range(5)]
    data = parties_csv(parties, "vendors.csv")
    with pytest.raises(PeriodError, match=r"vendors\.csv renders to"):
        check_file_size("vendors.csv", len(set(parties)), data, max_bytes=len(data) - 1)
    check_file_size("vendors.csv", len(set(parties)), data, max_bytes=len(data))  # exactly at the cap is fine


def test_accounts_csv_is_size_checked_too():
    # accounts.csv bypassed check_file_size entirely until this ruling --
    # it carries a chart account list, not entries, the same shape problem
    # customers.csv/vendors.csv have.
    data = accounts_csv(load_chart(EXAMPLE_BOOKS.chart_path), load_qbo_map(EXAMPLE_BOOKS.qbo_path))
    rows = sum(1 for _ in csv.reader(io.StringIO(data.decode("utf-8")))) - 1  # header
    with pytest.raises(PeriodError, match=rf"accounts\.csv needs {rows} rows"):
        check_file_size("accounts.csv", rows, data, max_rows=rows - 1)
    check_file_size("accounts.csv", rows, data, max_rows=rows)  # exactly at the cap is fine


def test_a_remark_that_would_run_as_a_formula_is_escaped_in_the_journal_csv():
    # Journal/Description is free text, not a key QuickBooks matches on --
    # a real fixture GL row's remarks, altered, proves the escape reaches
    # from the actual ingest data through to the file the same way the
    # human-facing CSVs escape it.
    rows = period_gl_rows()
    idx = next(i for i, r in enumerate(rows) if r.voucher_no == "ACC-JV-2026-00007")
    altered = [*rows[:idx], dataclasses.replace(rows[idx], remarks="=SUM(1)"), *rows[idx + 1 :]]
    entries = journal_entries(altered, INDEX, load_qbo_map(EXAMPLE_BOOKS.qbo_path), Q3)
    parsed = list(csv.DictReader(io.StringIO(journal_csv(entries).decode("utf-8"))))
    matching = [r for r in parsed if r["Journal No."] == "ACC-JV-2026-00007"]
    assert any(r["Journal/Description"] == "'=SUM(1)" for r in matching)


def test_a_party_that_would_run_as_a_formula_is_refused_naming_it():
    # Name is a key QuickBooks matches by exact text; unlike the
    # human-facing CSVs, prefixing it with an apostrophe would break that
    # match, so it is refused instead of escaped.
    entries = _entries()
    entry_idx, line_idx = next(
        (ei, li) for ei, e in enumerate(entries) for li, line in enumerate(e.lines) if line.party
    )
    entry = entries[entry_idx]
    bad_party = '=HYPERLINK("x")'
    bent_line = dataclasses.replace(entry.lines[line_idx], party=bad_party)
    bent_lines = (*entry.lines[:line_idx], bent_line, *entry.lines[line_idx + 1 :])
    bent = dataclasses.replace(entry, lines=bent_lines)
    altered_entries = [*entries[:entry_idx], bent, *entries[entry_idx + 1 :]]
    with pytest.raises(PeriodError, match=re.escape(bad_party)):
        journal_csv(altered_entries)


def test_an_account_name_that_would_run_as_a_formula_is_refused_naming_it():
    entries = _entries()
    entry = entries[0]
    bad_account = "+cmd"
    bent_line = dataclasses.replace(entry.lines[0], account=bad_account)
    bent = dataclasses.replace(entry, lines=(bent_line, *entry.lines[1:]))
    altered_entries = [bent, *entries[1:]]
    with pytest.raises(PeriodError, match=re.escape(bad_account)):
        journal_csv(altered_entries)


def test_a_journal_no_that_would_run_as_a_formula_is_refused_naming_it():
    # Journal No. is written unchanged -- a voucher number, an OPENING-...
    # number, or a split piece's /1, /2 suffix -- so a formula-like one
    # would run in a spreadsheet just like a formula-like Account Name or
    # Name would.
    entries = _entries()
    entry = entries[0]
    bad_no = "=1+1"
    bent = dataclasses.replace(entry, journal_no=bad_no)
    altered_entries = [bent, *entries[1:]]
    with pytest.raises(PeriodError, match=re.escape(bad_no)):
        journal_csv(altered_entries)


def test_a_formula_like_party_is_refused_in_parties_csv():
    with pytest.raises(PeriodError, match=r"customers\.csv.*=HYPERLINK"):
        parties_csv(["Ok Co", '=HYPERLINK("x")'], "customers.csv")
    parties_csv(["Ok Co", "Another Co"], "customers.csv")  # ordinary names are fine


def test_a_formula_like_account_name_is_refused_in_accounts_csv():
    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    qmap = load_qbo_map(EXAMPLE_BOOKS.qbo_path)
    target = next(a for a in chart if not a.is_group and a.number not in qmap.builtin)
    bent = [dataclasses.replace(a, name="=SUM(1)") if a.number == target.number else a for a in chart]
    with pytest.raises(PeriodError, match=r"accounts\.csv.*=SUM\(1\)"):
        accounts_csv(bent, qmap)
    accounts_csv(chart, qmap)  # ordinary names are fine


def test_ordinary_account_and_party_names_pass_through_the_journal_csv_unchanged():
    entries = _entries()
    originals = {(line.account, line.description, line.party or "") for e in entries for line in e.lines}
    parsed = list(csv.DictReader(io.StringIO(journal_csv(entries).decode("utf-8"))))
    written = {(r["Account Name"], r["Journal/Description"], r["Name"]) for r in parsed}
    assert written == originals


def test_the_csv_has_intuits_columns_and_names_the_party_on_ar_lines():
    rows = list(csv.DictReader(io.StringIO(journal_csv(_entries()).decode("utf-8"))))
    assert list(rows[0]) == [
        "Journal No.",
        "Journal Date",
        "Account Name",
        "Debits",
        "Credits",
        "Journal/Description",
        "Name",
    ]
    ar_name = load_qbo_map(EXAMPLE_BOOKS.qbo_path).builtin["1131"].name
    assert all(r["Name"] for r in rows if r["Account Name"] == ar_name)
    assert all(
        not r["Name"]
        for r in rows
        if r["Account Name"] != ar_name
        and r["Account Name"] != load_qbo_map(EXAMPLE_BOOKS.qbo_path).builtin["2111"].name
    )


def test_journal_date_is_iso_format_unverified_pending_manual_qbo_check():
    # ACC-JV-2026-00007 is dated 2026-07-10 in the captured fixture (see
    # tests/fixtures/period/README.md). ISO is the controller's ruling
    # (qbo.toml's header), UNVERIFIED against a real QBO import.
    assert QBO_DATE_FORMAT == "%Y-%m-%d"
    rows = list(csv.DictReader(io.StringIO(journal_csv(_entries()).decode("utf-8"))))
    matching = [r for r in rows if r["Journal No."] == "ACC-JV-2026-00007"]
    assert matching
    assert all(r["Journal Date"] == "2026-07-10" for r in matching)
