"""The trial balance and the statements derived from it, on the captured scenarios."""

import csv
import dataclasses
import datetime as dt
import io
from decimal import Decimal

import pytest

from runningtab.chart import load_chart
from runningtab.period import PeriodError
from runningtab.period.balances import (
    PRIOR_YEAR_LABEL,
    balance_sheet_csv,
    chart_of_accounts_csv,
    general_ledger_csv,
    profit_and_loss_csv,
    trial_balance,
    trial_balance_csv,
)
from runningtab.period.ledger import index_accounts
from runningtab.period.quarter import Quarter, fiscal_year_for
from tests.conftest import EXAMPLE_BOOKS, PERIOD_ABBR, period_fiscal_years, period_gl_rows

Q3 = Quarter.parse("2026Q3")


def _tb(rows=None):
    fy = fiscal_year_for(Q3, period_fiscal_years())
    return trial_balance(
        rows or period_gl_rows(), index_accounts(load_chart(EXAMPLE_BOOKS.chart_path), PERIOD_ABBR), Q3, fy
    )


def _rows(data: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8"))))


def test_checking_opens_with_everything_before_the_quarter():
    # 5,000 in (opening entry) - 300 (2025) - 120 (Feb 2026) = 4,580
    assert _tb().opening("1111") == Decimal("4580.00")


def test_checking_closes_with_the_quarters_receipts():
    assert _tb().closing("1111") == Decimal("5430.00")  # 4,580 + 600 + 250


def test_pl_opens_from_the_fiscal_year_start_only():
    tb = _tb()
    assert tb.opening("5110") == Decimal("120.00")  # Feb 2026, not 2025
    assert tb.opening("5206") == Decimal("0.00")  # its only row is 2025


def test_last_years_pl_is_carried_as_prior_year_unclosed():
    assert _tb().prior_year_unclosed == Decimal("300.00")  # the 2025 expense, debit-positive


def test_the_trial_balance_balances_including_the_prior_year_line():
    rows = _rows(trial_balance_csv(_tb()))
    total = sum(Decimal(r["closing_debit"]) - Decimal(r["closing_credit"]) for r in rows)
    assert total == 0
    assert any(r["name"] == PRIOR_YEAR_LABEL for r in rows)


def test_an_unbalanced_read_is_refused():
    rows = period_gl_rows()[:-1]  # drop one leg
    with pytest.raises(PeriodError, match="does not balance"):
        _tb(rows)


def test_a_period_closing_voucher_is_refused():
    rows = period_gl_rows()
    pcv = [dataclasses.replace(rows[0], voucher_type="Period Closing Voucher", voucher_no="PCV-0001")]
    with pytest.raises(PeriodError, match="PCV-0001"):
        _tb([*rows, *pcv])


def test_a_pl_entry_marked_opening_inside_the_fiscal_year_is_refused():
    rows = period_gl_rows()
    # The 5110 row from JE #5 (2026-02-10, debit 120) is a real captured P&L
    # row dated inside FY2026. Mark it `is_opening` -- ERPNext's own report
    # treats that case specially and we refuse it rather than guess.
    target = next(r for r in rows if r.voucher_no == "ACC-JV-2026-00005" and r.debit == Decimal("120"))
    odd = dataclasses.replace(target, is_opening=True, name="odd-opening-pl-row")
    # A balancing copy on the same account, opposite sides, so only the
    # opening-inside-the-fiscal-year refusal can fire here, not the
    # ledger-does-not-balance refusal.
    balancer = dataclasses.replace(
        target, debit=Decimal("0"), credit=Decimal("120"), is_opening=False, name="odd-opening-pl-balancer"
    )
    with pytest.raises(PeriodError, match="odd-opening-pl-row"):
        _tb([*rows, odd, balancer])


def test_an_in_quarter_opening_entry_is_refused_naming_it_and_the_cutoff_date():
    rows = period_gl_rows()
    moved = [
        dataclasses.replace(r, posting_date=Q3.start) if r.posting_date == dt.date(2025, 3, 1) else r for r in rows
    ]
    with pytest.raises(PeriodError, match="2026-06-30") as excinfo:
        _tb(moved)
    for r in rows:
        if r.posting_date == dt.date(2025, 3, 1):
            assert r.name in str(excinfo.value)
            assert r.voucher_no in str(excinfo.value)


def test_rows_dated_after_the_quarter_end_are_refused():
    q2 = Quarter.parse("2026Q2")
    fy = fiscal_year_for(q2, period_fiscal_years())
    with pytest.raises(PeriodError, match="dated after 2026Q2 ends"):
        trial_balance(period_gl_rows(), index_accounts(load_chart(EXAMPLE_BOOKS.chart_path), PERIOD_ABBR), q2, fy)


def test_the_balance_sheet_balances():
    rows = {r["name"]: r for r in _rows(balance_sheet_csv(_tb()))}
    assets = Decimal(rows["Total assets"]["amount"])
    other = Decimal(rows["Total liabilities and equity"]["amount"])
    assert assets == other != 0


def test_profit_and_loss_has_quarter_and_year_to_date():
    rows = {r["number"]: r for r in _rows(profit_and_loss_csv(_tb()))}
    assert rows["5110"]["quarter"] == "400.00"
    assert rows["5110"]["year_to_date"] == "520.00"


def test_the_gl_detail_holds_exactly_the_quarters_rows():
    rows = _rows(
        general_ledger_csv(period_gl_rows(), index_accounts(load_chart(EXAMPLE_BOOKS.chart_path), PERIOD_ABBR), Q3)
    )
    assert {r["posting_date"][:7] for r in rows} == {"2026-07", "2026-08", "2026-09"}
    assert all(r["gl_entry"] for r in rows)


def test_the_chart_lists_every_account_with_its_parent():
    rows = _rows(chart_of_accounts_csv(load_chart(EXAMPLE_BOOKS.chart_path)))
    assert len(rows) == len(load_chart(EXAMPLE_BOOKS.chart_path))
    assert {r["number"]: r["parent"] for r in rows}["1111"] == "1110"


def test_two_builds_are_byte_identical():
    assert trial_balance_csv(_tb()) == trial_balance_csv(_tb())
