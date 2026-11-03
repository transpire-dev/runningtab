"""Whether the books are actually current: six checks and one exit code.

The books integration's likeliest failure is not a crash, it is silence -- a
webhook Mercury disabled after repeated delivery failures, a cursor that
stopped advancing, entries a person parked in suspense and never came back
for, a quarter reopened after its books were handed off. Each leaves a ledger
that looks fine and is wrong, and none of them raises anything. This module is
the thing that looks.

Everything here returns data; `runningtab.cli` prints it and picks the exit code.
Three of the six checks run today. The other three need the Mercury ingest and
a token for a real Mercury account, and they say so rather than passing
vacuously -- a check that reports success because it did not run is worse than
no check at all.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import textwrap
import tomllib
from collections.abc import Callable, Iterable, Sequence
from decimal import Decimal
from typing import Any, Protocol

import httpx2
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError
from icedcoffee_sync.erpnext.accounts import document_name
from icedcoffee_sync.frappe.errors import FrappeError

from runningtab.chart import ChartError
from runningtab.coa import NoSoleCompanyError
from runningtab.mercury.client import MercuryError
from runningtab.mercury.ingest import BalanceCheck
from runningtab.mercury.ingest import reconcile as account_balance
from runningtab.mercury.mapping import MappingError
from runningtab.mercury.models import Account, CreditAccount, Status, Transaction
from runningtab.period import PeriodError
from runningtab.site import SiteCredentialError

# The statuses whose effect on `currentBalance` has actually been measured:
# `sent` counts toward it, `pending` and `failed` do not, on two sandbox
# accounts that then reconciled exactly. `cancelled`, `reversed`, and `blocked`
# are in the bank's enum and occurred in none of those reads, so the arithmetic
# treats them the way `models.UNBALANCED_STATUSES` says -- as counting -- and
# this report says out loud that it saw one. Absorbing an unmeasured status
# silently is how a real discrepancy gets explained away.
MEASURED_STATUSES: frozenset[Status] = frozenset({Status.SENT, Status.PENDING, Status.FAILED})


class Outcome(enum.StrEnum):
    """How a check came out. Only `PASS` and `SKIP` are ever acceptable."""

    PASS = "pass"  # noqa: S105 -- an outcome, not a credential
    FAIL = "fail"
    # The check is not built yet. The only outcome `--strict` promotes, and
    # the only one that means "nothing here has gone wrong".
    SKIP = "n/a"
    # A check that exists could not run: a credential unset or refused, an
    # unreachable site, a missing account, a short read. Never gated behind a
    # flag -- an unanswerable check is out of bounds by definition, because
    # the report cannot say whether the books are current without it.
    ERROR = "error"


@dataclasses.dataclass(frozen=True)
class Check:
    """One named section of the report."""

    key: str
    title: str
    outcome: Outcome
    summary: str
    detail: tuple[str, ...] = ()


def exit_code(checks: Sequence[Check], *, strict: bool) -> int:
    """0 only when nothing is out of bounds.

    A skipped check is not a failure by default: three of the five cannot run
    until the ingest exists, and a command that always exits 1 gets wired to
    nothing. `--strict` is for the day the ingest lands, when a skip means a
    regression rather than a known gap.
    """
    if any(check.outcome in (Outcome.FAIL, Outcome.ERROR) for check in checks):
        return 1
    if strict and any(check.outcome is Outcome.SKIP for check in checks):
        return 1
    return 0


def wrapped(text: str) -> tuple[str, ...]:
    """One explanatory sentence as indented report lines."""
    return tuple(f"  {line}" for line in textwrap.wrap(text, width=94))


def skipped(key: str, title: str, needs: str) -> Check:
    """A check that has nothing to run against yet, and says what it is waiting for.

    The summary is the same sentence for all three, so the detail line carries
    the part that differs: what specifically has to exist before this one can
    run. Three identical "not available" lines would tell a reader nothing
    about which is closest to being real.
    """
    return Check(
        key=key,
        title=title,
        outcome=Outcome.SKIP,
        summary="not available: needs the Mercury ingest",
        detail=wrapped(needs),
    )


# --- the balance invariant ------------------------------------------------


@dataclasses.dataclass(frozen=True)
class AccountLedger:
    """One account's reported balance next to the transactions behind it."""

    account_id: str
    label: str
    current_balance: Decimal
    transactions: tuple[Transaction, ...]


@dataclasses.dataclass(frozen=True)
class Reconciliation:
    """What one account's arithmetic came to."""

    # The arithmetic itself is the ingest's, not a second copy: one client
    # reads the bank and one function sums it, so the number this report
    # prints is the number the ingest acts on.
    balance: BalanceCheck
    settled_count: int
    # The three counts are disjoint and cover the account: `currentBalance`
    # counts the settled ones, is known not to count the unsettled ones, and
    # nothing has established what it does with the unclassified ones. That
    # last group is the one the check exists to surface, so it is counted on
    # its own rather than folded into either measured side.
    unsettled_count: int
    unclassified_count: int

    @property
    def label(self) -> str:
        """What a person reading the report calls this account."""
        return self.balance.label

    @property
    def account_id(self) -> str:
        return self.balance.account_id

    @property
    def current_balance(self) -> Decimal:
        """The balance the bank reports."""
        return self.balance.reported

    @property
    def settled_total(self) -> Decimal:
        """The sum of the rows `currentBalance` counts."""
        return self.balance.computed

    @property
    def delta(self) -> Decimal:
        """Settled sum minus the balance the bank reports. Zero, or something is wrong."""
        return self.balance.difference

    @property
    def reconciles(self) -> bool:
        """Exact equality. There is no tolerance to set: both sides are exact decimals."""
        return self.balance.ok


def unclassified(transaction: Transaction) -> bool:
    """Whether nobody has measured what `currentBalance` does with this row's status."""
    return transaction.status not in MEASURED_STATUSES


def settled_total(transactions: Iterable[Transaction]) -> Decimal:
    """Sum of the transactions `currentBalance` counts -- everything but pending and failed."""
    return account_balance("", "", Decimal(0), transactions).computed


def reconcile(ledgers: Iterable[AccountLedger]) -> list[Reconciliation]:
    """The invariant, per account. Exact and free, and it catches a short read."""
    results = []
    for ledger in ledgers:
        settled = [t for t in ledger.transactions if t.is_postable]
        unsettled = [t for t in ledger.transactions if not t.counts_toward_balance]
        unmeasured = [t for t in ledger.transactions if unclassified(t)]
        results.append(
            Reconciliation(
                balance=account_balance(ledger.account_id, ledger.label, ledger.current_balance, ledger.transactions),
                settled_count=len(settled),
                unsettled_count=len(unsettled),
                unclassified_count=len(unmeasured),
            )
        )
    return results


def _amount(value: Decimal) -> str:
    return f"{value:,.2f}"


def unclassified_statuses(ledgers: Iterable[AccountLedger]) -> dict[str, int]:
    """Transaction statuses whose effect on `currentBalance` has never been established, and how many carry them.

    The sum counts them, which is a guess, so the report says so rather than
    letting the guess ride. Whichever way one of these actually behaves,
    absorbing it silently is how a real discrepancy gets explained away.

    Counted by `raw_status`, not by the parsed enum: a status Mercury adds
    later parses to `blocked`, and reporting it under that name would name the
    wrong thing to go and decide about.
    """
    counts: dict[str, int] = {}
    for ledger in ledgers:
        for transaction in ledger.transactions:
            if unclassified(transaction):
                counts[transaction.raw_status] = counts.get(transaction.raw_status, 0) + 1
    return dict(sorted(counts.items()))


def check_balance_invariant(ledgers: Sequence[AccountLedger]) -> Check:
    """For each account, the settled transactions must sum to `currentBalance`."""
    title = "balance invariant"
    if not ledgers:
        return Check(
            key="balance-invariant",
            title=title,
            outcome=Outcome.ERROR,
            summary="no accounts read",
            detail=wrapped("Mercury returned no accounts at all, which is not a state this organisation can be in"),
        )
    results = reconcile(ledgers)
    detail = []
    for result in results:
        mark = "ok " if result.reconciles else "OFF"
        counts = f"{result.settled_count} settled, {result.unsettled_count} pending/failed"
        if result.unclassified_count:
            counts += f", {result.unclassified_count} unclassified"
        line = (
            f"  {mark} {result.label}: settled {_amount(result.settled_total)} "
            f"vs currentBalance {_amount(result.current_balance)} "
            f"({counts})"
        )
        if not result.reconciles:
            line += f" -- off by {_amount(result.delta)}"
        detail.append(line)

    unclassified = unclassified_statuses(ledgers)
    if unclassified:
        detail.extend(
            wrapped(
                "counted in every sum above, because nothing has established whether currentBalance counts "
                "them: " + ", ".join(f"{count} {status}" for status, count in unclassified.items()) + ". "
                "Decide from a real account and put the status on the right side of "
                "runningtab.mercury.models.UNBALANCED_STATUSES."
            )
        )

    off = [r for r in results if not r.reconciles]
    if off or unclassified:
        problems = []
        if off:
            problems.append(f"{len(off)} of {len(results)} accounts do not reconcile")
        if unclassified:
            problems.append(f"{sum(unclassified.values())} transactions carry an unclassified status")
        return Check(
            key="balance-invariant",
            title=title,
            outcome=Outcome.FAIL,
            summary="; ".join(problems),
            detail=tuple(detail),
        )
    return Check(
        key="balance-invariant",
        title=title,
        outcome=Outcome.PASS,
        summary=f"{len(results)} accounts reconcile exactly",
        detail=tuple(detail),
    )


# --- suspense contents ----------------------------------------------------

# The ledger holds submitted documents only: ERPNext writes GL Entries in
# `on_submit`, so a draft against the suspense account has none of them and
# this check does not see it. That is fine now that nothing machine-made lands
# there: the Mercury ingest posts Bank Transactions, which reach the general
# ledger only when a person reconciles them, so an entry against 5299 is one a
# person made -- a voucher they reconciled a statement line to because they
# could not yet say where it belongs -- and a person's voucher is submitted.
SUSPENSE_DOCTYPE = "GL Entry"
_SUSPENSE_FIELDS = ["posting_date", "debit", "credit", "voucher_type", "voucher_no", "remarks", "creation"]


@dataclasses.dataclass(frozen=True)
class SuspenseEntry:
    """One ledger entry against the suspense account."""

    posting_date: dt.date
    creation: str
    amount: Decimal
    voucher_type: str
    voucher_no: str
    remarks: str


@dataclasses.dataclass(frozen=True)
class SuspenseState:
    """What sits in suspense, and since when."""

    balance: Decimal
    entry_count: int
    # The posting date on which the balance last left zero -- None when the
    # account is empty. Not simply the oldest entry: after a clear-out the
    # oldest entry is ancient and the account is fine.
    open_since: dt.date | None
    age_days: int
    # The entries that make up the current balance, in ledger order. Carried
    # here rather than recovered afterwards by date: a clear-out and a refill
    # on the same day are two runs that a date cannot separate, and the walk
    # below already knows where one ended.
    outstanding: tuple[SuspenseEntry, ...]

    @property
    def is_empty(self) -> bool:
        """Whether the account nets to nothing. A balance of zero is the only acceptable state."""
        return self.balance == 0


def _entry_order(entry: SuspenseEntry) -> tuple[dt.date, str]:
    return (entry.posting_date, entry.creation)


def suspense_state(entries: Sequence[SuspenseEntry], as_of: dt.date) -> SuspenseState:
    """Aggregate the suspense account, ageing from the last time it was empty.

    Walking a running balance rather than taking the oldest entry: a
    reclassification leaves the original entry in place with an opposite one
    beside it, so an account that was emptied last week would otherwise report
    the age of the first thing that ever landed in it.

    The balance returning to zero is the only evidence available that a run
    ended -- no ledger field links a reclassification back to what it
    reverses -- so two unrelated entries of opposite sign read as a clear-out.
    That costs the age and the listing, never the outcome: pass or fail is the
    closing balance, which the walk does not change. Separating the two needs
    provenance nothing writes yet.
    """
    balance = Decimal(0)
    open_since: dt.date | None = None
    outstanding: list[SuspenseEntry] = []
    for entry in sorted(entries, key=_entry_order):
        was_empty = balance == 0
        balance += entry.amount
        outstanding.append(entry)
        if balance == 0:
            open_since = None
            outstanding.clear()
        elif was_empty:
            open_since = entry.posting_date
    age = (as_of - open_since).days if open_since is not None else 0
    return SuspenseState(
        balance=balance,
        entry_count=len(entries),
        open_since=open_since,
        age_days=age,
        outstanding=tuple(outstanding),
    )


def check_suspense(state: SuspenseState, account: str) -> Check:
    """Anything left in suspense is unfinished bookkeeping, so a balance fails."""
    title = "suspense contents"
    if state.is_empty:
        return Check(
            key="suspense-contents",
            title=title,
            outcome=Outcome.PASS,
            summary=f"{account} is empty ({state.entry_count} historical entries, all cleared)",
        )
    detail = [f"  open since {state.open_since} ({state.age_days} days)"]
    for entry in state.outstanding:
        remarks = entry.remarks.strip().replace("\n", " ")
        if len(remarks) > 80:
            remarks = remarks[:77] + "..."
        detail.append(
            f"  {entry.posting_date} {_amount(entry.amount):>14}  {entry.voucher_type} {entry.voucher_no}"
            + (f"  {remarks}" if remarks else "")
        )
    return Check(
        key="suspense-contents",
        title=title,
        outcome=Outcome.FAIL,
        summary=f"{_amount(state.balance)} sitting in {account}, oldest {state.age_days} days",
        detail=tuple(detail),
    )


class _Reader(Protocol):
    """The slice of the Frappe client the suspense read needs."""

    def iter_all(self, doctype: str, **kwargs: Any) -> Any: ...


class MercuryReader(Protocol):
    """The slice of the Mercury client the balance invariant needs."""

    def accounts(self) -> list[Account]: ...
    def credit_accounts(self) -> list[CreditAccount]: ...
    def account_transactions(self, account_id: str, *, start: dt.datetime | None = None) -> list[Transaction]: ...


def _gl_money(value: object) -> Decimal:
    """A GL Entry amount as an exact decimal.

    The Frappe client parses responses with the standard library's defaults,
    so debit and credit arrive as floats. `str()` first: `Decimal(float)`
    would carry the binary representation's tail into the ledger arithmetic.
    """
    if value in (None, ""):
        return Decimal(0)
    return Decimal(str(value))


def read_suspense_entries(client: _Reader, company: str, account: str) -> list[SuspenseEntry]:
    """Every live ledger entry against the suspense account.

    Paged through `iter_all`, never a capped `list`: a short read here would
    report a full suspense account as empty, which is precisely the silence
    this command exists to break. Cancelled entries are excluded -- they no
    longer affect the balance.
    """
    rows = client.iter_all(
        SUSPENSE_DOCTYPE,
        fields=_SUSPENSE_FIELDS,
        filters=[["account", "=", account], ["company", "=", company], ["is_cancelled", "=", 0]],
        order_by="posting_date asc",
    )
    return [
        SuspenseEntry(
            posting_date=dt.date.fromisoformat(str(row["posting_date"])),
            creation=str(row.get("creation") or ""),
            amount=_gl_money(row.get("debit")) - _gl_money(row.get("credit")),
            voucher_type=str(row.get("voucher_type") or ""),
            voucher_no=str(row.get("voucher_no") or ""),
            remarks=str(row.get("remarks") or ""),
        )
        for row in rows
    ]


def suspense_account_name(number: str, name: str, abbr: str) -> str:
    """The suspense account's ERPNext document name, derived the way ERPNext derives it."""
    return document_name(name, abbr, number)


# --- the report -----------------------------------------------------------


def uncoded_spend_placeholder() -> Check:
    """Settled, non-transfer transactions carrying no GL code at Mercury, aged by days since posting.

    The GL code is the one hint the person reconciling a Bank Transaction gets
    about which account it belongs in; a row without one has to be coded
    somewhere. `runningtab mercury snapshot`/`sync` report these per run as
    `uncoded`; this section has nothing persisted to read them from between
    runs.
    """
    return skipped(
        "uncoded-spend",
        "uncoded spend, aged",
        "needs the ingest's read of the bank: `runningtab mercury sync` reports these rows per run as `uncoded`, "
        "and this section has nothing persisted to read them from between runs",
    )


def webhook_subscription_placeholder() -> Check:
    """Mercury disables a webhook after consecutive delivery failures, invisibly from our side."""
    return skipped(
        "webhook-subscriptions",
        "webhook subscription health",
        "needs the ingest's webhook subscription to exist; there is nothing registered to report the status of",
    )


def event_cursor_placeholder() -> Check:
    """How long since the event log was last advanced."""
    return skipped(
        "event-cursor-age",
        "event cursor age",
        "needs the ingest's persisted last-processed event id; nothing writes a cursor yet",
    )


def guarded(key: str, title: str, run: Callable[[], Check]) -> Check:
    """Run one check, turning the ways it can fail to answer into the report itself.

    Every one of these is an `ERROR`, including a credential that is simply
    not configured. `SKIP` is reserved for the three checks that are not built
    yet; a check that exists and did not run leaves the report unable to say
    whether the books are current, and reporting success on that is the
    failure this command exists to prevent. The messages say what to set or
    create, so an `ERROR` is actionable rather than merely loud.

    The closed-periods check reaches further than the other two that run
    today -- the chart, the record bucket, a Mercury mapping file -- so its
    own refusals (`PeriodError`, `ChartError`, `MappingError`, a bad TOML
    file, and the two Google exceptions a missing or expired ADC credential
    can raise) are caught here too, the same list `runningtab.cli`'s `period
    close` and `period export` already refuse by name.

    `LookupError` and `OSError` are caught in a second clause, with the
    exception's own type name prefixed to the summary. Both are generic
    enough to come from a genuine bug -- `company_abbr`'s deliberate
    `LookupError` writes a full sentence, but its sibling `KeyError` from a
    typo'd dict key anywhere in the rebuild carries only the repr of the
    missing key, with no context at all -- and every other caught type here
    already writes a hand-composed, self-explaining message, so prefixing
    those would only be noise.
    """
    try:
        return run()
    except (
        # Every Mercury credential this command uses is fetched by
        # `runningtab.site`, so an unset token and a refused secret both arrive as
        # a `SiteCredentialError`. `MercuryError` covers the reads themselves:
        # an error status, a body the client cannot read, and a short read are
        # all subclasses, and all mean the same thing here -- no answer.
        SiteCredentialError,
        MercuryError,
        FrappeError,
        NoSoleCompanyError,
        httpx2.HTTPError,
        PeriodError,
        ChartError,
        MappingError,
        tomllib.TOMLDecodeError,
        GoogleAPIError,
        # ADC expiring or missing (constructing the record bucket client, or a
        # revoked/expired token discovered on a read) raises from google.auth,
        # not google.api_core -- GoogleAuthError is not a GoogleAPIError
        # subclass, so it needs naming here too or it crashes instead of
        # refusing.
        GoogleAuthError,
    ) as exc:
        return Check(key=key, title=title, outcome=Outcome.ERROR, summary=f"could not run: {exc}")
    except (
        # `company_abbr` raises LookupError for a company the site does not
        # have, or one with no abbreviation -- an operator naming the wrong
        # `--company` should read a line, not a stack. But `LookupError`'s
        # own subclass `KeyError` (and `IndexError`) is just as likely to be
        # a programming bug in the rebuild path, whose bare text alone is
        # useless -- the type name is what a reader needs to tell the two
        # apart.
        LookupError,
        OSError,
    ) as exc:
        return Check(key=key, title=title, outcome=Outcome.ERROR, summary=f"could not run: {type(exc).__name__}: {exc}")


def read_ledgers(
    client: MercuryReader,
    *,
    accounts: Sequence[Account] | None = None,
    credit_accounts: Sequence[CreditAccount] | None = None,
) -> list[AccountLedger]:
    """Every account, deposit and credit, with every transaction since it opened.

    Read per account rather than through the org-wide `/transactions`
    endpoint: only the per-account response carries a `total`, and without one
    a short read is undetectable. `/accounts` omits credit accounts entirely,
    so both endpoints are read or the card account is silently skipped.

    `accounts`/`credit_accounts` are optional so a caller that already has
    the account lists -- `runningtab.period.bank.read_ledgers`, checking
    `created_at` before this runs -- passes them straight through instead
    of this function fetching them a second time. `runningtab health` passes
    neither, so its own behaviour (one fetch, done here) is unchanged.
    """
    ledgers: list[AccountLedger] = []
    for account in accounts if accounts is not None else client.accounts():
        ledgers.append(
            AccountLedger(
                account_id=account.id,
                label=account.label,
                current_balance=account.current_balance,
                transactions=tuple(client.account_transactions(account.id, start=account.created_at)),
            )
        )
    for credit in credit_accounts if credit_accounts is not None else client.credit_accounts():
        ledgers.append(
            AccountLedger(
                account_id=credit.id,
                label=credit.label,
                current_balance=credit.current_balance,
                transactions=tuple(client.account_transactions(credit.id, start=credit.created_at)),
            )
        )
    return ledgers


def balance_invariant(client: MercuryReader) -> Check:
    """Read every account and check each one's arithmetic."""
    return check_balance_invariant(read_ledgers(client))


def suspense_contents(client: Any, company: str, account: str, as_of: dt.date) -> Check:
    """What is in the suspense account, and how long it has been there."""
    if not client.list("Account", fields=["name"], filters=[["name", "=", account]], limit=1):
        return Check(
            key="suspense-contents",
            title="suspense contents",
            outcome=Outcome.ERROR,
            summary=f"{account} is not on this site",
            detail=wrapped(
                "the chart declares the suspense account; run `runningtab coa apply` so entries have somewhere to land"
            ),
        )
    entries = read_suspense_entries(client, company, account)
    return check_suspense(suspense_state(entries, as_of), account)


def report(balance: Check, suspense: Check, closed_periods: Check) -> list[Check]:
    """The six sections, in the one order the report ever prints them.

    Assembled here rather than in the CLI so that "all six, always, in this
    order" is a property of the report and not of one call site. Arithmetic
    first, then what the arithmetic cannot see, then whether a closed
    quarter is still closed.
    """
    return [
        balance,
        uncoded_spend_placeholder(),
        suspense,
        webhook_subscription_placeholder(),
        event_cursor_placeholder(),
        closed_periods,
    ]


def render(checks: Sequence[Check]) -> list[str]:
    """The report as lines. Human-readable first; the exit code is the contract."""
    width = max(len(check.title) for check in checks)
    lines = []
    for check in checks:
        lines.append(f"{check.title.ljust(width)}  {check.outcome.value.upper():<5}  {check.summary}")
        lines.extend(check.detail)
    counts = {outcome: sum(1 for c in checks if c.outcome is outcome) for outcome in Outcome}
    lines.append("")
    lines.append(
        f"{counts[Outcome.PASS]} passed, {counts[Outcome.FAIL]} failed, "
        f"{counts[Outcome.ERROR]} could not run, {counts[Outcome.SKIP]} not available"
    )
    return lines
