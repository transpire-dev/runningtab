"""Where the forecast's opening numbers come from.

Opening cash is Mercury's own balance, and the ledger's reconciliation frontier
is the last day ERPNext agrees with the bank. Both refuse plainly when they
cannot be trusted: a capital call computed from a balance that does not
reconcile is a confident wrong number that members then wire money against.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Protocol

from icedcoffee_sync.frappe.errors import FrappeError

from runningtab.mercury.ingest import BalanceCheck

MAINTENANCE = "SessionStopped"
"""The `error_type` a Frappe site answers with while an update is running."""


class SourceError(RuntimeError):
    """An input the forecast cannot trust. The message is the sentence a person reads."""


class _Reader(Protocol):
    def iter_all(self, doctype: str, **kwargs: Any) -> Any: ...


def opening_cash(balances: Sequence[BalanceCheck]) -> Decimal:
    """Cash on hand: the sum of every account's reported balance, net of cards.

    A credit account reports a negative balance when it owes money, so a plain
    sum is already net of what the card owes.

    Raises:
        SourceError: Naming every account whose reported balance differs from
            the sum of the transactions read for it, since that means the read
            was incomplete and the balance cannot anchor a forecast.
    """
    if not balances:
        raise SourceError("Mercury returned no accounts, so there is no balance to forecast from")
    bad = [check.label for check in balances if not check.ok]
    if bad:
        raise SourceError(
            "Mercury's balance does not match its transactions for "
            + ", ".join(bad)
            + "; the read was incomplete, so no forecast"
        )
    return sum((check.reported for check in balances), Decimal(0))


def ledger_through(ledger: _Reader, today: dt.date) -> dt.date:
    """The last day the ledger is known to agree with the bank.

    That is the day before the oldest submitted Bank Transaction still
    unreconciled, or `today` when none is.
    """
    rows = ledger.iter_all(
        "Bank Transaction",
        fields=["name", "date"],
        filters=[["status", "=", "Unreconciled"], ["docstatus", "=", 1]],
    )
    dates = [_row_date(row) for row in rows]
    return min(dates) - dt.timedelta(days=1) if dates else today


def _row_date(row: Any) -> dt.date:
    """A Bank Transaction's date, or a SourceError naming the row when it has none we can read."""
    raw = row.get("date")
    try:
        return dt.date.fromisoformat(str(raw))
    except ValueError:
        raise SourceError(
            f"Bank Transaction {row.get('name')} has no readable date ({raw!r}), so the reconciliation frontier is unknown"
        ) from None


def maintenance_message(exc: FrappeError) -> str | None:
    """The sentence for a site mid-update, or None when the error is something else."""
    if exc.error_type == MAINTENANCE:
        return "the site is in maintenance mode (an update is running); run this again when it finishes"
    return None
