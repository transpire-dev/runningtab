"""The applier: snapshot for initial state, then the event log as the authority.

Three layers, as the design has them:

1. **Snapshot** -- transaction lists establish initial state. Not a one-off
   convenience: events are retained 90 days, so any gap wider than that is
   closed by re-snapshotting rather than by resuming a cursor into a hole.
2. **Log** -- every subsequent change arrives as an event. `apply_event` is the
   single entry point, and it does not care where the event came from.
3. **Audit** -- the balance invariant as independent arithmetic on top.

One thing the log cannot carry, and one departure from it: Mercury emits no
event when a person codes a transaction -- the GL code and note change with no
`resourceVersion` bump either -- so every sync ends by sweeping the
transactions it read for those two fields. `sweep_hints` says why that is not
the scheduled poll this design otherwise refuses.

**The webhook receiver is not built yet, and this is the shape that lets it be
added without touching any of this.** A Mercury webhook delivers exactly the
event the log serves; when the second `webhooks` deployment lands, its consumer
parses the delivery with `models.parse_event` and hands it to `apply_event` with
the same state file. Dedup on event id makes a webhook and a later pull of the
same event idempotent with respect to each other, so the two paths can run at
once without coordination.

The applier deliberately reads only the event's envelope -- which resource
changed, at which version -- and re-reads the transaction itself from the
transactions endpoint. A pending charge that settled has a different body than
the one the event carried, and re-reading is also how this avoids depending on
the exact shape of a JSON Merge Patch nobody has captured yet.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterable
from decimal import Decimal

from icedcoffee_sync.frappe.errors import FrappeError, PermissionError_, RateLimitError

from runningtab.mercury.classify import Decision, Route, classify, is_internal_transfer
from runningtab.mercury.client import MercuryClient
from runningtab.mercury.models import Event, Operation, Transaction
from runningtab.mercury.posting import (
    TRANSACTION_ID_FIELD,
    Ledger,
    Outcome,
    Plan,
    PostingContext,
    PostingError,
    bank_transaction,
    carry_out,
    plain_message,
    plan,
    posted_transaction_ids,
)
from runningtab.mercury.receipts import ReceiptFetcher, ReceiptReport, attach_receipts
from runningtab.mercury.state import DryRunState, IngestState, State
from runningtab.mercury.vouchers import Unneeded, VoucherReport, draft_vouchers


@dataclasses.dataclass(frozen=True)
class BalanceCheck:
    """One account's reported balance against the transactions we read for it.

    Exact arithmetic, and free. It caught a real truncation during the
    investigation -- a probe that never paged past a hundred rows and never
    queried the credit account at all -- which is the failure mode that matters
    most here: a ledger that looks fine while being incomplete.
    """

    account_id: str
    label: str
    reported: Decimal
    computed: Decimal

    @property
    def ok(self) -> bool:
        """Whether the sum of settled rows is exactly the balance Mercury reports."""
        return self.reported == self.computed

    @property
    def difference(self) -> Decimal:
        return self.computed - self.reported


class BalanceMismatchError(RuntimeError):
    """An account's transactions do not sum to the balance Mercury reports."""


@dataclasses.dataclass
class IngestReport:
    """What one run did, and what it wants a person to look at."""

    # New Bank Transactions, posted already submitted -- the one document
    # this ingest submits, because a Bank Transaction reaches the general
    # ledger only when a person reconciles it.
    submitted: list[str] = dataclasses.field(default_factory=list)
    # Draft Journal Entries proposed beside a coded Bank Transaction. Nothing
    # here is submitted, and nothing reaches the general ledger until a person
    # does. See `runningtab.mercury.vouchers`.
    drafted: list[str] = dataclasses.field(default_factory=list)
    # Documents this run had already written and brought up to date: Bank
    # Transactions and draft vouchers both.
    updated: list[str] = dataclasses.field(default_factory=list)
    # Hand-imported Bank Transactions recognised by their trace number and
    # stamped with Mercury's fields. Their content was a person's already.
    adopted: list[str] = dataclasses.field(default_factory=list)
    # Mercury receipts now on their Bank Transaction, as
    # `<file name> -> <document name>`. See `runningtab.mercury.receipts`.
    attached: list[str] = dataclasses.field(default_factory=list)
    planned: list[str] = dataclasses.field(default_factory=list)
    skipped: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    attention: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    # A coded row dated on or before Books Closed Through: its Bank
    # Transaction is posted above like any other, but no voucher is drafted.
    # See `runningtab.mercury.vouchers.VoucherReport.closed`.
    closed: list[str] = dataclasses.field(default_factory=list)
    # Draft vouchers of ours nothing needs any more. A person deletes them.
    # See `runningtab.mercury.vouchers.Unneeded`.
    unneeded: list[Unneeded] = dataclasses.field(default_factory=list)
    balances: list[BalanceCheck] = dataclasses.field(default_factory=list)
    uncoded: list[tuple[str, int]] = dataclasses.field(default_factory=list)
    # Bank Transactions naming a transaction the snapshot did not find in
    # Mercury: (transaction id, document name). Only a snapshot can populate
    # this -- a sync reads the log, which says nothing about what is absent.
    orphans: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    # Mercury transaction ids this run's events already put through the
    # applier. The hint sweep skips them: the event path has just written
    # whatever Mercury says about them, and planning them a second time would
    # only repeat whatever it already reported.
    applied_transactions: set[str] = dataclasses.field(default_factory=set)
    events_read: int = 0
    events_applied: int = 0
    events_deduplicated: int = 0
    events_out_of_order: int = 0
    cursor: str | None = None

    @property
    def balances_ok(self) -> bool:
        return all(check.ok for check in self.balances)

    @property
    def needs_attention(self) -> bool:
        """Whether anything in this run wants a person, which is what the command's exit code says.

        Wider than the rows that failed. A settled row with no GL code gives
        the person reconciling nothing to go on and will have to be coded
        somewhere, a balance that does not reconcile means the read was
        short, a closed-period row wants its quarter reopened, and an unneeded
        draft wants deleting, which only a person can do -- each leaves the
        ledger incomplete, or wanting a person's decision, while every
        individual step looks like it worked.
        """
        return bool(
            self.attention or self.uncoded or self.orphans or self.closed or self.unneeded or not self.balances_ok
        )


def reconcile(account_id: str, label: str, reported: Decimal, transactions: Iterable[Transaction]) -> BalanceCheck:
    """Sum the rows that count toward the balance and compare.

    `currentBalance` excludes pending and failed transactions, verified exactly
    on two sandbox accounts. Anything that does not reconcile means the read was
    incomplete, not that the arithmetic is approximate.
    """
    computed = sum((t.amount for t in transactions if t.counts_toward_balance), Decimal(0))
    return BalanceCheck(account_id=account_id, label=label, reported=reported, computed=computed)


def uncoded_ages(
    transactions: Iterable[Transaction], now: dt.datetime, *, own_account_ids: Iterable[str]
) -> list[tuple[str, int]]:
    """Settled, non-transfer transactions carrying no GL code at Mercury, aged in days.

    The GL code is the one hint the person reconciling gets about which account
    a row belongs in, and a settled row without one has to be coded somewhere
    -- in Mercury, so the hint lands on the Bank Transaction, or by hand at
    reconciliation. Either way the run names it rather than leaving it to be
    found.

    Transfers are left out: a movement between our own accounts is not
    something anybody should be coding, so counting it would put a floor under
    the number and hide the signal.
    """
    own = set(own_account_ids)
    out: list[tuple[str, int]] = []
    for transaction in transactions:
        if not transaction.is_postable or transaction.gl_code or is_internal_transfer(transaction, own):
            continue
        age = (now - (transaction.posted_at or transaction.created_at)).days
        out.append((transaction.id, age))
    return sorted(out, key=lambda row: -row[1])


def _record_route(report: IngestReport, transaction: Transaction, decision: Decision) -> None:
    if decision.route is Route.SKIP:
        report.skipped.append((transaction.id, decision.reason))
    elif decision.route is Route.ATTENTION:
        report.attention.append((transaction.id, decision.reason))


# What the report calls a plan it could not carry out, in the verbs the dry run
# already uses for the same three writes.
_VERBS = {Outcome.SUBMITTED: "submit", Outcome.UPDATED: "update", Outcome.ADOPTED: "adopt"}


def _write(ledger: Ledger, verdict: Plan) -> tuple[Outcome, str]:
    """Carry the plan out, reporting a document the server refuses rather than ending the run.

    The first production run died on a Rich traceback here, one transaction in,
    because a custom field had been installed without `allow_on_submit` -- so
    nothing was attempted after it and the report nobody saw would have named
    the other sixteen rows. A refusal of one write is the same shape as a
    transaction we cannot build a document for: one report line, the run goes
    on, and the cursor stays behind it.

    Only a refusal of this document. A ledger we cannot reach -- a connection
    that fails, a request that times out -- a site rate limiting us, and a
    credential the site will not accept are each one problem the whole run
    has, not one row's, so they propagate and end it. Seventeen identical
    lines would hide that there is only one thing wrong.
    """
    try:
        return carry_out(ledger, verdict)
    except RateLimitError, PermissionError_:
        # Declared ahead of the base so a throttled site and a revoked key or
        # lost submit grant keep ending the run: both answer every write the
        # same way, and a line per row would bury the single real problem.
        raise
    except FrappeError as exc:
        where = f" {verdict.name}" if verdict.name else ""
        # `.get` rather than `[...]`: an outcome nobody named a verb for is
        # still a refusal to report, and a KeyError raised while reporting one
        # would trade a diagnosable problem for a mysterious one.
        verb = _VERBS.get(verdict.outcome, str(verdict.outcome))
        raise PostingError(f"{verb} {verdict.doctype}{where} refused by the ledger: {plain_message(str(exc))}") from exc


def _post(
    ledger: Ledger,
    transaction: Transaction,
    ctx: PostingContext,
    report: IngestReport,
    *,
    version: int | None,
    dry_run: bool,
    refresh_only: bool = False,
) -> None:
    """Plan the write, then do it -- or, on a dry run, only say what it would have been.

    The dry run reads the ledger exactly as the real run does, so it plans an
    adoption or an amendment where the real run would make one, rather than
    calling everything a submit.

    `refresh_only` is the hint sweep: it brings an existing row's GL code and
    note up to date and posts nothing at all. See `sweep_hints`.

    A write the server refuses comes back out as a `PostingError`, so the
    caller holds this transaction exactly as it holds one it could not build a
    document for. A ledger we cannot reach, cannot authenticate to, or is
    throttling us is not that, and is left to propagate. See `_write`.
    """
    doc = bank_transaction(transaction, ctx)
    transaction_id = doc[TRANSACTION_ID_FIELD]
    verdict = plan(ledger, doc, transaction, resource_version=version)
    if verdict.outcome is Outcome.REFUSED:
        report.attention.append((transaction_id, f"{doc['doctype']} {verdict.name} {verdict.reason}"))
        return
    if refresh_only and verdict.outcome in {Outcome.SUBMITTED, Outcome.ADOPTED}:
        # A settled transaction the ledger holds no row for means an event went
        # missing, and posting it here would settle that by silence: the row
        # would appear with nothing in the log ever having said so, and nobody
        # would learn the log had a hole. Snapshot is the documented repair.
        # A transaction that arrived between this run's read of the log and its
        # read of the accounts lands here too and clears itself on the next
        # run, which is why the message asks for a second sighting first.
        report.attention.append((
            transaction_id,
            "settled at Mercury with no Bank Transaction of its own; the hint sweep refreshes rows and never "
            "posts one, so if the next sync says this again an event went missing -- run `runningtab mercury snapshot`",
        ))
        return
    if dry_run:
        if verdict.outcome is Outcome.ADOPTED:
            report.planned.append(
                f"adopt {doc['doctype']} {verdict.name} (reference {transaction.tracking_number}) for {transaction_id}"
            )
        elif verdict.outcome is Outcome.SUBMITTED:
            report.planned.append(f"submit {doc['doctype']} for {transaction_id}")
        elif verdict.outcome is Outcome.UPDATED:
            report.planned.append(f"update {doc['doctype']} {verdict.name} for {transaction_id}")
        return
    outcome, name = _write(ledger, verdict)
    if outcome is Outcome.SUBMITTED:
        report.submitted.append(name)
    elif outcome is Outcome.UPDATED:
        report.updated.append(name)
    elif outcome is Outcome.ADOPTED:
        report.adopted.append(name)


def apply_transaction(
    transaction: Transaction,
    ctx: PostingContext,
    ledger: Ledger,
    report: IngestReport,
    *,
    version: int | None = None,
    dry_run: bool = False,
    refresh_only: bool = False,
) -> bool:
    """Post the one Bank Transaction this transaction should have, or record why it has none.

    Returns whether this transaction is **settled with**, which is not the same
    as whether it posted. A skipped or reversed row is settled with: there is
    nothing to retry. A row whose status nobody has classified is not, and says
    so through `Decision.retryable`. A `PostingError` is not either -- a
    Mercury account with no Bank Account, or a custom field the site will not
    let us write, is a configuration a person fixes, and the next run must
    post what this one could not. The caller uses the answer to decide how far
    the cursor may move.
    """
    decision = classify(transaction)
    if not decision.posts:
        _record_route(report, transaction, decision)
        # A reversal is settled with; a status nobody has classified is not.
        # Retiring the second would mean the transaction never posts once
        # somebody says what its status means -- it emits no further events, so
        # only a re-snapshot would find it again.
        return not decision.retryable
    try:
        _post(ledger, transaction, ctx, report, version=version, dry_run=dry_run, refresh_only=refresh_only)
    except PostingError as exc:
        # A transaction we cannot post is a report line, not the end of the run:
        # stalling on one unmapped account would leave every later transaction
        # unposted too. It is still unfinished business, so it comes back False
        # and the cursor stays behind it.
        report.attention.append((transaction.id, str(exc)))
        return False
    return True


def apply_event(
    event: Event,
    transactions: dict[str, Transaction],
    ctx: PostingContext,
    ledger: Ledger,
    state: State,
    report: IngestReport,
    *,
    now: dt.datetime,
    dry_run: bool = False,
) -> bool:
    """Apply one event. The single entry point a webhook consumer will call too.

    Two guards, in this order:

    - **Dedup on event id.** Mercury names it the idempotency key and delivers
      at-least-once.
    - **Order on `resourceVersion`.** An int64, monotonic per resource, so an
      event is applied only when it advances what we have recorded -- which is
      what makes an out-of-order delivery harmless rather than a rewind.

    An event that fails either guard is still marked seen. It has been
    considered; reconsidering it would reach the same conclusion more slowly.

    An event whose transaction could not be posted is **not** marked seen, and
    comes back False so the caller keeps its cursor behind it. Recording it
    would retire the event permanently while the ledger never received it --
    the one failure a bank feed must not have.

    `dry_run` buffers the state writes here rather than leaving that to the
    caller. Handing this a real `IngestState` with `dry_run=True` would
    otherwise retire events that never reached the ledger, which is exactly the
    bug above wearing a different hat.
    """
    if dry_run and not isinstance(state, DryRunState):
        state = DryRunState(state)
    if state.has_seen(event.id):
        report.events_deduplicated += 1
        return True
    if not event.is_transaction:
        state.mark_seen(event.id, now)
        return True
    if event.resource_version is not None:
        recorded = state.version_of(event.resource_id)
        if recorded is not None and event.resource_version <= recorded:
            report.events_out_of_order += 1
            state.mark_seen(event.id, now)
            return True

    applied = True
    if event.operation is Operation.DELETE:
        # Never destructive. A transaction the bank no longer says happened must
        # produce an explicit response rather than a silent one, but which
        # response -- cancel the row, reverse it, leave a note -- is a
        # bookkeeping judgement, and a person makes it.
        report.attention.append((
            event.resource_id,
            "deleted at Mercury; any Bank Transaction posted for it is still in the ledger and needs a person",
        ))
        report.applied_transactions.add(event.resource_id)
    else:
        transaction = transactions.get(event.resource_id)
        if transaction is None:
            # Retryable. The absence may be an account we cannot see yet, and
            # retiring the event would make the transaction unreachable the
            # moment visibility is fixed -- there is no second event coming.
            report.attention.append((
                event.resource_id,
                "changed at Mercury but absent from the accounts we read; check the account is in [accounts]",
            ))
            applied = False
        else:
            applied = apply_transaction(
                transaction, ctx, ledger, report, version=event.resource_version, dry_run=dry_run
            )
            # Recorded whether or not it posted: the event path has already
            # said everything there is to say about this transaction, and the
            # hint sweep repeating it would only double the report.
            report.applied_transactions.add(transaction.id)

    if not applied:
        return False
    state.mark_seen(event.id, now)
    if event.resource_version is not None:
        state.record_version(event.resource_id, event.resource_version)
    report.events_applied += 1
    return True


def sweep_hints(
    transactions: Iterable[Transaction],
    ctx: PostingContext,
    ledger: Ledger,
    report: IngestReport,
    *,
    dry_run: bool = False,
) -> None:
    """Bring Mercury's GL code and note up to date on every settled row no event touched.

    Coding a transaction at Mercury -- picking its GL code, writing its note --
    **emits no event and bumps no `resourceVersion`**. A sync that applied only
    what the log named would therefore never deliver a person's coding, and
    would say so in no report: the correction quietly stops applying and
    nobody learns to distrust it. That work is the person's, so the run goes
    and gets it rather than waiting for a message that is never sent.

    Not the scheduled poll the design rules out. It adds no read: the run has
    already fetched every transaction for the balance invariant, and this
    compares what it holds against the ledger.

    Writes through the same planner the event path uses, so a submitted row's
    amount, date, description and account are compared and refused exactly as
    before and only the two `allow_on_submit` hint fields are ever written.
    `version=None` throughout: a GL edit bumps no resource version at Mercury,
    and stamping one here would claim the log said something it did not.

    O(rows) -- one ledger read per settled transaction per run, which at the
    seventeen rows these books hold is nothing. If that ever matters, read the
    posted rows once (`posted_transaction_ids` already pages the doctype),
    widen it to carry the hint fields, and plan only the rows whose hints
    differ.
    """
    for transaction in transactions:
        if transaction.id in report.applied_transactions or not classify(transaction).posts:
            continue
        # The return value is the cursor's business and the sweep is not: it
        # reads the transaction list, not the log, so nothing it finds may move
        # or hold a cursor into the log.
        apply_transaction(transaction, ctx, ledger, report, version=None, dry_run=dry_run, refresh_only=True)


def propose_vouchers(
    transactions: Iterable[Transaction],
    ctx: PostingContext,
    ledger: Ledger,
    report: IngestReport,
    *,
    own_account_ids: Iterable[str],
    dry_run: bool = False,
) -> None:
    """Draft the voucher every coded row should have, and fold the result into the run's report.

    Runs after the Bank Transaction side of every transaction has been dealt
    with, on both paths. A sweep rather than an event response, for the reason
    `sweep_hints` already is one: a person's GL code arrives with no event
    behind it. `runningtab.mercury.vouchers` has the rules; nothing it does can
    move or hold the cursor, because nothing it reads is the log.
    """
    result: VoucherReport = draft_vouchers(transactions, ctx, ledger, own_account_ids=own_account_ids, dry_run=dry_run)
    report.drafted.extend(result.drafted)
    report.updated.extend(result.updated)
    report.planned.extend(result.planned)
    report.attention.extend(result.attention)
    report.closed.extend(result.closed)
    report.unneeded.extend(result.unneeded)
    # Mercury had no code for these, and the ledger accounts for them anyway.
    report.uncoded = [row for row in report.uncoded if row[0] not in result.handled_in_ledger]


def fetch_receipts(
    transactions: Iterable[Transaction],
    ledger: Ledger,
    report: IngestReport,
    *,
    receipts: ReceiptFetcher | None,
    dry_run: bool = False,
) -> None:
    """Attach each Mercury receipt to its Bank Transaction, and fold the result into the run's report.

    Runs last on both paths, after the Bank Transaction side of every
    transaction has been dealt with, because a receipt needs the row it hangs
    off to exist. A sweep for the reason `sweep_hints` is one: the attachment
    list emits no event either. `runningtab.mercury.receipts` has the rules.

    Nothing here can move or hold the cursor. A receipt that could not be
    fetched is a document missing from a row, not an event missing from the
    ledger, and holding the log's cursor for it would stall the feed over a
    PDF.

    **A caller that passes no fetcher gets no receipt pass.** This is the one
    part of the ingest that reaches a host which is neither Mercury nor the
    ledger, and a caller with no business making that request -- a test, a
    future webhook consumer that only wants the log applied -- has to be able
    to say so without a flag that reads like a feature toggle. The CLI always
    passes one.
    """
    if receipts is None:
        return
    result: ReceiptReport = attach_receipts(transactions, ledger, fetcher=receipts, dry_run=dry_run)
    report.attached.extend(result.attached)
    report.planned.extend(result.planned)
    report.attention.extend(result.attention)


def read_everything(mercury: MercuryClient) -> tuple[list[Transaction], list[BalanceCheck], list[str]]:
    """Every transaction on every account, with each account's balance checked.

    Reads the credit accounts as well as the deposit ones. `/accounts` omits
    credit accounts entirely, and the first probe that forgot them produced a
    balance that looked plausible and was wrong.
    """
    transactions: list[Transaction] = []
    balances: list[BalanceCheck] = []
    account_ids: list[str] = []
    for account in mercury.accounts():
        rows = mercury.account_transactions(account.id, start=account.created_at)
        transactions.extend(rows)
        balances.append(reconcile(account.id, account.name or account.id, account.current_balance, rows))
        account_ids.append(account.id)
    for credit in mercury.credit_accounts():
        rows = mercury.account_transactions(credit.id, start=credit.created_at)
        transactions.extend(rows)
        balances.append(reconcile(credit.id, f"credit {credit.id}", credit.current_balance, rows))
        account_ids.append(credit.id)
    return transactions, balances, account_ids


def run_snapshot(
    mercury: MercuryClient,
    ledger: Ledger,
    ctx: PostingContext,
    state: IngestState,
    *,
    now: dt.datetime,
    dry_run: bool = False,
    receipts: ReceiptFetcher | None = None,
) -> IngestReport:
    """Establish initial state, and record where in the log that state was true.

    The cursor is read **before** the transactions, not after. Anything that
    changes while the snapshot is running then replays through the applier
    afterwards; the alternative -- taking the cursor last -- silently drops
    every change made during the read. Replay is harmless because a transaction
    posts to one document, found by its Mercury id.

    The cursor is recorded only when the run posted everything, reconciled, and
    found a Mercury transaction behind every row the ledger already holds.
    Any of those failing means the snapshot did not establish the state the
    cursor would claim, and the rows it missed emit no further events.
    """
    report = IngestReport()
    place = DryRunState(state) if dry_run else state
    cursor = mercury.latest_event_id()
    transactions, balances, account_ids = read_everything(mercury)
    report.balances = balances
    report.uncoded = uncoded_ages(transactions, now, own_account_ids=account_ids)
    posted = True
    for transaction in transactions:
        posted &= apply_transaction(transaction, ctx, ledger, report, dry_run=dry_run)
    propose_vouchers(transactions, ctx, ledger, report, own_account_ids=account_ids, dry_run=dry_run)
    fetch_receipts(transactions, ledger, report, receipts=receipts, dry_run=dry_run)
    # Bank Transactions whose Mercury transaction is no longer returned. The
    # loop above only ever walks FORWARD, from Mercury into the ledger, so a
    # transaction that disappeared -- a pending charge cancelled while the log
    # had already aged past its retention window -- leaves its row standing and
    # is invisible to every check here. Snapshot is the documented recovery
    # from exactly that gap, which makes this the one place that can notice.
    #
    # Safe to read as "no longer returns": read_everything pages each account
    # from its own createdAt and raises on a short read, so the set below is
    # every transaction Mercury currently holds, not a recent window.
    if transactions or report.balances:
        seen = {transaction.id for transaction in transactions}
        report.orphans = sorted(
            (transaction_id, name)
            for transaction_id, name in posted_transaction_ids(ledger).items()
            if transaction_id not in seen
        )
    # Three ways a snapshot fails to establish the state its cursor would claim.
    # The balance one is the more dangerous: the rows a short read missed were
    # never seen at all, so nothing in the loop above could have reported them,
    # and they emit no further events for a later sync to find.
    unestablished: list[str] = []
    if not posted:
        unestablished.append("some transactions could not be posted")
    if not report.balances:
        # `balances_ok` is vacuously true over no accounts, so a read that saw
        # none at all -- a token scoped to nothing, an endpoint that changed
        # shape -- would otherwise sail past every check and record a cursor
        # over transactions it never looked at.
        unestablished.append("no account was read, so there was no state to establish")
    if not report.balances_ok:
        unestablished.append("an account does not reconcile, so the read was short")
    if report.orphans:
        # Not repaired here: nothing in this ingest cancels or deletes from the
        # ledger, and whether an orphaned row is cancelled, reversed or kept is
        # a bookkeeping decision. Held rather than merely reported, because
        # moving the cursor would settle the question by silence -- the rows
        # stay, and no later run ever looks for them again.
        unestablished.append(
            f"{len(report.orphans)} Bank Transaction(s) name a transaction Mercury no longer returns "
            f"({', '.join(f'{name} -> {tid}' for tid, name in report.orphans[:5])}"
            f"{', ...' if len(report.orphans) > 5 else ''}) -- resolve those rows, then re-run"
        )
    if unestablished:
        report.attention.append((
            "cursor",
            f"not recorded: {'; '.join(unestablished)} -- a cursor would claim a state this run "
            "did not establish, so fix the reported problems and re-run the snapshot",
        ))
    else:
        place.record_cursor(cursor, now)
        report.cursor = cursor
    return report


def run_sync(
    mercury: MercuryClient,
    ledger: Ledger,
    ctx: PostingContext,
    state: IngestState,
    *,
    now: dt.datetime,
    dry_run: bool = False,
    receipts: ReceiptFetcher | None = None,
) -> IngestReport:
    """Apply everything that happened since the cursor, then the three sweeps the log cannot carry.

    Four passes, and only the first is the log's. Mercury reports a GL code or
    note edited after a transaction posted through no event at all, so the
    second pass sweeps every settled row this run read and brings those two
    fields up to date -- `sweep_hints` has the reasoning -- the third drafts
    the voucher each coded row should have, which keys on the same unannounced
    field, and the fourth attaches the receipts, whose list is unannounced too.
    All three are independent of the cursor in both directions: they neither
    move it nor hold it.

    Raises `StaleCursorError` rather than resuming a cursor older than Mercury's
    retention window: past 90 days the log cannot say what was missed, and the
    honest recovery is another snapshot.

    Holds the cursor when no account was read at all, the same as a snapshot
    does. A quiet hour and a token that lost its accounts look identical from
    here -- no events, nothing posted -- and only the account inventory tells
    them apart.
    """
    report = IngestReport()
    place = DryRunState(state) if dry_run else state
    cursor = place.require_resumable(now)
    events = mercury.events(start_after=cursor.event_id)
    report.events_read = len(events)
    transactions, balances, account_ids = read_everything(mercury)
    report.balances = balances
    by_id = {t.id: t for t in transactions}
    report.uncoded = uncoded_ages(transactions, now, own_account_ids=account_ids)
    # The cursor stops before the first event that could not be applied, while
    # the events after it are still attempted: they are idempotent, so doing
    # them now saves work, and the ones that succeeded are marked seen and
    # deduplicated when the re-run reaches them. What must not happen is the
    # cursor stepping over an event whose transaction never reached the ledger.
    last = cursor.event_id
    held = False
    for event in events:
        applied = apply_event(event, by_id, ctx, ledger, place, report, now=now, dry_run=dry_run)
        if not applied:
            held = True
        elif not held:
            last = event.id
    # Then everything the log had nothing to say about. A person's coding at
    # the bank arrives no other way -- see `sweep_hints` -- and it changes
    # nothing above it: the cursor, the dedup set and the version guard are the
    # log's, and the sweep never touches them.
    sweep_hints(transactions, ctx, ledger, report, dry_run=dry_run)
    propose_vouchers(transactions, ctx, ledger, report, own_account_ids=account_ids, dry_run=dry_run)
    fetch_receipts(transactions, ledger, report, receipts=receipts, dry_run=dry_run)
    if not report.balances:
        # `balances_ok` is vacuously true over no accounts, so losing account
        # visibility during a quiet spell produced a clean report and a
        # refreshed cursor: nothing to apply, nothing failing to reconcile,
        # nothing wrong. The ingest would go on looking healthy for as long as
        # the access stayed broken, and the retention guard -- which measures
        # from the last recorded cursor -- would never fire either, because
        # every run kept moving it.
        held = True
        report.attention.append((
            "accounts",
            "no account was read, so this run could not tell a quiet period from lost access -- "
            "the cursor is held until at least one account comes back",
        ))
    if held:
        # The timestamp is left alone as well as the id. Re-stamping a cursor
        # that did not move would make a stuck ingest look freshly synced
        # forever, and the retention guard measures from the last time we
        # actually caught up -- which this run did not.
        report.cursor = cursor.event_id
    else:
        place.record_cursor(last, now)
        report.cursor = last
    return report
