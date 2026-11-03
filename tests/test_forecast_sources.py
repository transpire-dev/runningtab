"""Where the forecast's opening numbers come from, and when it refuses to guess."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import Decimal as D

import httpx2
import pytest
from icedcoffee_sync.frappe.client import Client
from icedcoffee_sync.frappe.errors import FrappeError

from runningtab.forecast.sources import (
    MAINTENANCE,
    SourceError,
    ledger_through,
    maintenance_message,
    opening_cash,
)
from runningtab.mercury.ingest import BalanceCheck
from tests.test_mercury_posting import SITE, _Site

TODAY = dt.date(2026, 10, 26)


def _ledger_with(site: _Site, rows: Sequence[Mapping[str, object]]):
    for i, row in enumerate(rows):
        site.documents[("Bank Transaction", f"BT-{i}")] = dict(row)
    return Client(SITE, transport=httpx2.MockTransport(site.handler))


def _unreconciled(date: str) -> dict[str, object]:
    return {"date": date, "status": "Unreconciled", "docstatus": 1}


def test_opening_cash_is_net_of_what_the_card_owes() -> None:
    # A card that owes money reports a negative balance when it reconciles
    # (the ingest fixtures: currentBalance -159 against rows summing to -159;
    # -50 here is illustrative).
    assert opening_cash([
        BalanceCheck("chk", "Checking", D("1362.60"), D("1362.60")),
        BalanceCheck("cc", "credit cc", D("-50"), D("-50")),
    ]) == D("1312.60")


def test_a_balance_that_does_not_reconcile_refuses_and_names_the_account() -> None:
    checks = [
        BalanceCheck("chk", "Checking", D("100"), D("100")),
        BalanceCheck("sav", "Savings", D("10"), D("9")),
        BalanceCheck("cc", "credit cc", D("-50"), D("-60")),
    ]
    with pytest.raises(SourceError) as raised:
        opening_cash(checks)
    assert "Savings" in str(raised.value)
    assert "credit cc" in str(raised.value)
    assert "Checking" not in str(raised.value)


def test_ledger_through_is_the_day_before_the_oldest_unreconciled_row() -> None:
    site = _Site()
    with _ledger_with(site, [_unreconciled("2026-10-19"), _unreconciled("2026-10-14")]) as ledger:
        assert ledger_through(ledger, TODAY) == dt.date(2026, 10, 13)


def test_ledger_through_ignores_rows_that_are_not_submitted_unreconciled() -> None:
    site = _Site()
    rows = [
        {"date": "2026-01-01", "status": "Reconciled", "docstatus": 1},
        {"date": "2026-01-02", "status": "Unreconciled", "docstatus": 0},
    ]
    with _ledger_with(site, rows) as ledger:
        assert ledger_through(ledger, TODAY) == TODAY


def test_ledger_through_is_today_when_everything_is_reconciled() -> None:
    with _ledger_with(_Site(), []) as ledger:
        assert ledger_through(ledger, TODAY) == TODAY


def test_maintenance_mode_reads_as_a_sentence() -> None:
    message = maintenance_message(FrappeError("Session Stopped", error_type=MAINTENANCE))
    assert message == "the site is in maintenance mode (an update is running); run this again when it finishes"


def test_any_other_frappe_error_is_not_called_maintenance() -> None:
    assert maintenance_message(FrappeError("boom", error_type="ValidationError")) is None
    assert maintenance_message(FrappeError("boom")) is None


def test_no_accounts_at_all_refuses_rather_than_reporting_zero_cash() -> None:
    with pytest.raises(SourceError, match="no accounts"):
        opening_cash([])


@pytest.mark.parametrize("date", [None, "", "not a date"])
def test_a_row_without_a_readable_date_refuses_and_names_the_row(date: object) -> None:
    row = {"date": date, "status": "Unreconciled", "docstatus": 1}
    with _ledger_with(_Site(), [row]) as ledger, pytest.raises(SourceError, match="BT-0"):
        ledger_through(ledger, TODAY)
