"""Open receivables and payables at a date, by party and by the document each entry settles.

A trial balance carries one balance per account; a person working from it --
or QBO, which demands a customer or vendor on every AR and AP line -- needs
that balance split by who owes it and for what.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal
from typing import Literal

from runningtab.period import PeriodError
from runningtab.period.balances import TrialBalance
from runningtab.period.csvfmt import money, render_csv
from runningtab.period.ledger import AccountIndex, GLRow

Kind = Literal["Receivable", "Payable"]


@dataclasses.dataclass(frozen=True)
class OpenItem:
    party_type: str
    party: str
    document_type: str
    document: str
    document_date: dt.date
    due_date: dt.date | None
    amount: Decimal
    advance: bool


def open_items(rows: Sequence[GLRow], index: AccountIndex, kind: Kind, as_of: dt.date) -> list[OpenItem]:
    relevant = [r for r in rows if r.posting_date <= as_of and index[r.account].account_type == kind]
    orphans = sorted(r.name for r in relevant if not r.party)
    if orphans:
        raise PeriodError(f"{kind} entries with no party cannot be placed: {', '.join(orphans)}")
    groups: dict[tuple[str, str, str, str], list[GLRow]] = defaultdict(list)
    for r in relevant:
        doc_type = r.against_voucher_type or r.voucher_type
        doc = r.against_voucher or r.voucher_no
        groups[(r.party_type or "", r.party or "", doc_type, doc)].append(r)
    sign = 1 if kind == "Receivable" else -1
    items: list[OpenItem] = []
    for (party_type, party, doc_type, doc), members in groups.items():
        amount = sign * sum((m.net for m in members), Decimal(0))
        if amount == 0:
            continue
        own = [m for m in members if m.voucher_no == doc] or members
        first = min(own, key=lambda m: (m.posting_date, m.name))
        due = next((m.due_date for m in own if m.due_date), None)
        items.append(OpenItem(party_type, party, doc_type, doc, first.posting_date, due, amount, amount < 0))
    return sorted(items, key=lambda i: (i.party, i.document_date, i.document))


def open_items_csv(items: Sequence[OpenItem]) -> bytes:
    header = ["party_type", "party", "document_type", "document", "document_date", "due_date", "amount", "advance"]
    rows = [
        [
            i.party_type,
            i.party,
            i.document_type,
            i.document,
            i.document_date.isoformat(),
            i.due_date.isoformat() if i.due_date else "",
            money(i.amount),
            "yes" if i.advance else "",
        ]
        for i in items
    ]
    return render_csv(header, rows)


def check_open_items_against(items: Sequence[OpenItem], tb: TrialBalance, index: AccountIndex, kind: Kind) -> None:
    """The open items must add up to the closing balance of every account of this kind."""
    sign = 1 if kind == "Receivable" else -1
    ledger = sign * sum((line.closing for line in tb.lines if line.account.account_type == kind), Decimal(0))
    listed = sum((i.amount for i in items), Decimal(0))
    if ledger != listed:
        raise PeriodError(f"open {kind.lower()}s total {listed} but the {kind} accounts close at {ledger}")
