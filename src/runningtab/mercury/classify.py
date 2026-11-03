"""What each transaction is, and whether it becomes a Bank Transaction.

Pure functions over parsed transactions. Nothing here performs I/O, which is
what lets the whole routing table be exercised against captured payloads.

The routing:

| Signal                                                | Destination                                       |
| ----------------------------------------------------- | ------------------------------------------------- |
| a status nobody has assigned a meaning to              | a person decides; nothing is posted               |
| status not settled -- pending, failed, cancelled, ...  | post nothing                                      |
| status reversed                                        | a person decides; nothing is posted or undone     |
| everything else                                        | a Bank Transaction on the account it moved through |

There is no category branch. The books run through ERPNext's Banking workspace
-- Bank Statement Import and the Bank Reconciliation Tool -- which works on
submitted Bank Transactions, and a person picks the account when they reconcile
each one to a voucher. Mercury's GL code and note ride along on the row as
hints for that person; they route nothing. An internal transfer is two
statement lines on two accounts and becomes two Bank Transactions, one per
account, which the tool matches to one transfer voucher. A category with no
mapping, or no category at all, is just a Bank Transaction with whatever hint
Mercury has.
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Iterable

from runningtab.mercury.models import Status, Transaction


class Route(enum.StrEnum):
    """Where a transaction goes."""

    SKIP = "skip"
    BANK_TRANSACTION = "bank-transaction"
    ATTENTION = "attention"


@dataclasses.dataclass(frozen=True)
class Decision:
    """The routing for one transaction, why, and whether a later run must look again.

    `retryable` separates the two things that arrive as `ATTENTION`. A reversal
    is settled with: there is a bookkeeping judgement to make and no amount of
    re-running makes it, so the cursor may move past it. A status nobody has
    classified is not: the row is fine and our configuration is short, and once
    somebody says what the status means the transaction has to post. Retiring
    it would need a re-snapshot to reach again, because it emits no new events.
    """

    route: Route
    reason: str
    retryable: bool = False

    @property
    def posts(self) -> bool:
        """Whether this decision produces a ledger document at all."""
        return self.route is Route.BANK_TRANSACTION


def is_internal_transfer(transaction: Transaction, own_account_ids: Iterable[str]) -> bool:
    """Whether the counterparty is one of our own Mercury accounts.

    Routing does not depend on the answer -- both legs post -- but the uncoded
    report does: nobody codes a movement between our own accounts, so counting
    one would put a floor under that number and hide the signal.

    Keyed on `counterpartyId` against the account list rather than on `kind`:
    the verified sandbox pair between checking and the credit account came
    through as `kind: other`, not `internalTransfer`, so trusting `kind` alone
    would have missed exactly the pair the rule is for.
    """
    return transaction.counterparty_id is not None and transaction.counterparty_id in set(own_account_ids)


def classify(transaction: Transaction) -> Decision:
    """Route one transaction. Pure; the caller decides what to do with the answer."""
    if not transaction.status_recognised:
        # Before any other reading of the status, because every other branch
        # here acts on a meaning somebody assigned and this one has none. A
        # skip would claim there is nothing to post, and skips raise no
        # attention -- so a settled status Mercury added after this was written
        # would be left out of the ledger while the run exited clean.
        return Decision(
            Route.ATTENTION,
            f"status {transaction.raw_status!r} is not one this ingest has semantics for; "
            "decide what it means for the ledger before these rows are posted or dismissed",
            retryable=True,
        )
    if transaction.status is Status.REVERSED:
        return Decision(
            Route.ATTENTION,
            "reversed by the bank; a person decides what happens to any Bank Transaction already posted",
        )
    if not transaction.is_postable:
        return Decision(Route.SKIP, f"status {transaction.raw_status} is not settled")
    return Decision(Route.BANK_TRANSACTION, "settled; a statement line for the person reconciling")
