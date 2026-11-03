"""The bank's balance at the quarter's end, next to the ledger's, per Mercury account."""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol

from runningtab import health
from runningtab.health import AccountLedger, MercuryReader, reconcile, unclassified_statuses
from runningtab.mercury.mapping import Mapping
from runningtab.period import PeriodError
from runningtab.period.csvfmt import money, render_csv


class _Closings(Protocol):
    def closing(self, number: str) -> Decimal: ...


@dataclasses.dataclass(frozen=True)
class BankLine:
    label: str
    account_id: str
    number: str
    bank: Decimal
    ledger: Decimal

    @property
    def difference(self) -> Decimal:
        return self.bank - self.ledger


def read_ledgers(client: MercuryReader) -> list[AccountLedger]:
    """Every Mercury ledger, refusing an account with no inception date on record.

    `health.read_ledgers` passes `start=account.created_at` to the per-account
    transaction read; when that is `None`, Mercury's own endpoint default --
    the last 30 days -- silently stands in, so an account open longer than
    that reads short. `runningtab.health`'s balance invariant does not catch it:
    it only proves the settled sum equals `currentBalance`, never that every
    transaction was actually read, and a short read whose missing rows net to
    zero passes it clean. The quarter-end balance `bank_reconciliation` builds
    from that short ledger would then be wrong with nothing to say so.

    A period-side wrapper around `health.read_ledgers` rather than a change to
    it: `runningtab health`'s own report is unaffected by this refusal on purpose
    -- there is nothing wrong with running health against an account still
    missing its inception date, only with building a quarter's historical
    balance from one.

    `client.accounts()`/`client.credit_accounts()` are fetched once, here,
    and passed straight into `health.read_ledgers` rather than read again --
    the inception-date check and the ledger build need the same two lists.
    """
    accounts = client.accounts()
    credit_accounts = client.credit_accounts()
    missing = sorted(
        f"{account.label} ({account.id})" for account in (*accounts, *credit_accounts) if account.created_at is None
    )
    if missing:
        raise PeriodError(
            "Mercury account(s) with no inception date on record: "
            + ", ".join(missing)
            + "; the transaction read would silently default to the last 30 days"
        )
    return health.read_ledgers(client, accounts=accounts, credit_accounts=credit_accounts)


def bank_balance_through(ledger: AccountLedger, end: dt.date) -> Decimal:
    """Settled transactions dated on or before `end`, by the date the ingest books them."""
    return sum((t.amount for t in ledger.transactions if t.is_postable and t.accounting_date <= end), Decimal(0))


def bank_reconciliation(
    ledgers: Sequence[AccountLedger], mapping: Mapping, tb: _Closings, end: dt.date
) -> list[BankLine]:
    """Each Mercury account's balance at `end` next to the chart account's closing balance.

    Refuses unless the balance invariant holds for every account: a balance
    at a past date built from a short read cannot be trusted, and an account
    `mercury.toml` does not map has nowhere in the chart to compare against.
    Also refuses when a transaction carries a status nobody has classified
    (`runningtab.health.unclassified`): such a row would be silently excluded from
    `bank_balance_through` with no established accounting treatment, and a
    quiet exclusion is a wrong balance nobody can see. Also refuses when two
    Mercury accounts map to the same chart account: each would be compared
    against that one account's whole closing balance rather than its own
    share of it, producing a false difference for at least one of them. Also
    refuses when `mercury.toml` maps an account to a number that is not a
    postable account in `tb` (a group account, say): `TrialBalance.closing`
    has no line for it and would otherwise fail with a bare `KeyError`. Every
    refusal names the offending account rather than failing silently.
    """
    if not ledgers:
        raise PeriodError("Mercury returned no accounts; a short read, not an empty bank")
    failing = [r.label for r in reconcile(ledgers) if not r.reconciles]
    if failing:
        raise PeriodError(
            "the balance invariant fails for "
            + ", ".join(sorted(failing))
            + "; a balance at a past date cannot be trusted until `runningtab health` passes"
        )
    unclassified_by_account = {
        ledger.label: statuses for ledger in ledgers if (statuses := unclassified_statuses([ledger]))
    }
    if unclassified_by_account:
        named = ", ".join(
            f"{label} ({', '.join(f'{count} {status}' for status, count in statuses.items())})"
            for label, statuses in sorted(unclassified_by_account.items())
        )
        raise PeriodError(
            "transaction status(es) nobody has classified would be silently excluded from the historical "
            f"balance with unknown accounting treatment: {named}"
        )
    maybe_numbers = {
        ledger.account_id: mapping.account_for_mercury_account(ledger.account_id, ledger.label) for ledger in ledgers
    }
    unmapped = sorted(account_id for account_id, number in maybe_numbers.items() if number is None)
    if unmapped:
        raise PeriodError(f"mercury.toml maps no chart account for Mercury account(s): {', '.join(unmapped)}")
    numbers: dict[str, str] = {account_id: number for account_id, number in maybe_numbers.items() if number is not None}
    accounts_by_number: dict[str, list[AccountLedger]] = {}
    for ledger in ledgers:
        accounts_by_number.setdefault(numbers[ledger.account_id], []).append(ledger)
    duplicated = sorted(number for number, accounts in accounts_by_number.items() if len(accounts) > 1)
    if duplicated:
        named = "; ".join(
            f"{number} ({', '.join(f'{a.label} ({a.account_id})' for a in accounts_by_number[number])})"
            for number in duplicated
        )
        raise PeriodError(f"mercury.toml maps more than one Mercury account to chart account: {named}")
    lines = []
    for ledger in ledgers:
        number = numbers[ledger.account_id]
        try:
            closing = tb.closing(number)
        except KeyError:
            raise PeriodError(
                f"mercury.toml maps Mercury account {ledger.label} ({ledger.account_id}) to {number}, "
                "which is not a postable account in the trial balance"
            ) from None
        lines.append(BankLine(ledger.label, ledger.account_id, number, bank_balance_through(ledger, end), closing))
    return sorted(lines, key=lambda line: line.number)


def bank_reconciliation_csv(lines: Sequence[BankLine]) -> bytes:
    header = ["number", "account", "mercury_account", "bank_balance", "ledger_balance", "difference"]
    rows = [
        [line.number, line.label, line.account_id, money(line.bank), money(line.ledger), money(line.difference)]
        for line in lines
    ]
    return render_csv(header, rows)
