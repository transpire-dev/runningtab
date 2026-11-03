"""What blocks a quarter's close.

A hard stop is a fact ERPNext already holds: an entry parked in suspense, a
Bank Transaction the reconciliation report has not zeroed out, or a draft
Journal Entry or Purchase Invoice dated in the quarter that can never be
submitted once the quarter closes. `runningtab health` uses the same readers; this
just asks them one quarter at a time and names each finding as a `Blocker` the
close command can print or refuse on.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from runningtab.period.csvfmt import money
from runningtab.period.ledger import GLRow
from runningtab.period.quarter import Quarter

_DRAFT_DOCTYPES = ("Journal Entry", "Purchase Invoice")
_CENT = Decimal("0.01")


class _Reader(Protocol):
    def iter_all(self, doctype: str, **kwargs: Any) -> Any: ...


@dataclasses.dataclass(frozen=True)
class Blocker:
    """One thing that must be resolved in ERPNext before the quarter can close."""

    kind: str
    doctype: str
    name: str
    detail: str


def _order(blocker: Blocker) -> tuple[str, str]:
    return (blocker.kind, blocker.name)


def _amount(value: object) -> Decimal:
    """A currency field from a raw ERPNext row, safe for `runningtab.period.csvfmt.money`.

    The client parses JSON with the standard library, so this arrives as a
    float; going through `Decimal(str(value))` rather than `Decimal(value)`
    keeps the float's own binary repr noise out of the arithmetic -- the same
    guard `ledger._money` uses -- and quantizing to the cent ERPNext actually
    stores money at is what lets `money`, which refuses anything not already
    a whole number of cents, accept the result.
    """
    return Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _suspense_blockers(rows: Sequence[GLRow], suspense_account: str, quarter: Quarter) -> list[Blocker]:
    """Every row from the same ledger read the package builds from that landed on suspense, dated in the quarter.

    Reusing `rows` rather than a second, separately-filtered GL Entry query
    means this can never disagree with what the package itself will contain.
    """
    return [
        Blocker(
            kind="suspense",
            doctype="GL Entry",
            name=row.name,
            detail=(
                f"{row.voucher_type} {row.voucher_no} posted {money(row.net)} to suspense "
                f"on {row.posting_date.isoformat()}"
            ),
        )
        for row in rows
        if row.account == suspense_account and quarter.start <= row.posting_date <= quarter.end
    ]


def _unreconciled_blockers(client: _Reader, company: str, quarter: Quarter) -> list[Blocker]:
    """Submitted Bank Transactions dated in the quarter ERPNext has not fully allocated.

    ERPNext marks a Bank Transaction "Reconciled" only once it is submitted
    (`docstatus = 1`) and its `unallocated_amount` reaches zero or below
    (erpnext/accounts/doctype/bank_transaction/bank_transaction.py:85-91);
    this filters on exactly that.
    """
    rows = client.iter_all(
        "Bank Transaction",
        fields=["name", "date", "unallocated_amount"],
        filters=[
            ["company", "=", company],
            ["docstatus", "=", 1],
            ["date", ">=", quarter.start.isoformat()],
            ["date", "<=", quarter.end.isoformat()],
            ["unallocated_amount", ">", 0],
        ],
        order_by="name asc",
    )
    return [
        Blocker(
            kind="unreconciled",
            doctype="Bank Transaction",
            name=str(row["name"]),
            detail=f"{money(_amount(row['unallocated_amount']))} unallocated on {row['date']}",
        )
        for row in rows
    ]


def _draft_blockers(client: _Reader, company: str, quarter: Quarter, doctype: str) -> list[Blocker]:
    """Draft (`docstatus = 0`) documents of `doctype` dated in the quarter.

    A draft dated in a quarter that has closed can never be submitted, so
    leaving one there strands whatever work it represents.
    """
    rows = client.iter_all(
        doctype,
        fields=["name", "posting_date"],
        filters=[
            ["company", "=", company],
            ["docstatus", "=", 0],
            ["posting_date", ">=", quarter.start.isoformat()],
            ["posting_date", "<=", quarter.end.isoformat()],
        ],
        order_by="name asc",
    )
    return [
        Blocker(
            kind="draft",
            doctype=doctype,
            name=str(row["name"]),
            detail=f"draft {doctype} dated {row['posting_date']}",
        )
        for row in rows
    ]


def read_blockers(
    client: _Reader, company: str, quarter: Quarter, suspense_account: str, rows: Sequence[GLRow]
) -> list[Blocker]:
    """Everything that must be resolved in ERPNext before `quarter` can close, sorted by (kind, name)."""
    blockers = [
        *_suspense_blockers(rows, suspense_account, quarter),
        *_unreconciled_blockers(client, company, quarter),
        *[blocker for doctype in _DRAFT_DOCTYPES for blocker in _draft_blockers(client, company, quarter, doctype)],
    ]
    return sorted(blockers, key=_order)
