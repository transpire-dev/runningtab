"""The draft voucher that sits beside a coded Bank Transaction.

Every settled transaction is already a submitted Bank Transaction -- the bank's
record. Reconciling one means opening ERPNext's create-voucher dialog, picking
the account, and confirming the amount, once per row. For any row a person has
coded in Mercury the account is already known: the GL code list the bank shows
is our own chart, exported by `runningtab coa export-gl-codes`, so
`generalLedgerCodeName` comes back naming a real account.

So this module drafts that voucher. **A proposal and nothing else.** It sets no
`docstatus` in any request, submits nothing, reconciles nothing, and deletes
nothing -- ERPNext's reconciliation query filters `je.docstatus == 1`, so an
unsubmitted voucher is not even offered as a match until a person submits it.
A draft somebody else has since edited is left alone and not reported: that is
a person taking the proposal over, which is the point.

The shape is ERPNext's own. `bank_reconciliation_tool.create_journal_entry_bts`
-- what the create-voucher dialog calls -- builds a `Bank Entry` whose
`cheque_no` is the statement line's reference and whose two lines are the Bank
Account's chart account and the account the person picked. The tool's matching
query ranks on `je.cheque_no == transaction.reference_number` and filters on
`jea.account == Bank Account.account`, so both of those have to be right or the
draft is invisible to the very screen it exists for.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import re
from collections.abc import Iterable, Iterator
from decimal import Decimal
from typing import Any, Protocol

from icedcoffee_sync.apply.model import DesiredAccount

from runningtab.gl_codes import postable_accounts
from runningtab.mercury.classify import classify, is_internal_transfer
from runningtab.mercury.models import Transaction
from runningtab.mercury.posting import (
    BANK_TRANSACTION,
    JOURNAL_ENTRY,
    TRANSACTION_ID_FIELD,
    PostingContext,
    PostingError,
)
from runningtab.period.quarter import Quarter

# ERPNext's own choice for a Bank Account that is not flagged `is_credit_card`,
# which is all of ours -- `install_bank_accounts` sets `account`, `company` and
# `is_company_account` and nothing else. `validate_cheque_info` makes
# `cheque_no` and `cheque_date` mandatory for this type, which is why both are
# always set below.
VOUCHER_TYPE = "Bank Entry"

# What ERPNext truncates a reference to when its own dialog builds one. Mercury
# ids and ACH trace numbers are far shorter; the cap is here so a longer
# identifier would be cut the same way rather than refused by the server.
REFERENCE_LIMIT = 140

# The Bank Transaction status that means a person has already matched the row
# to a voucher of their own. A draft still sitting beside it is clutter.
RECONCILED = "Reconciled"
# Frappe's docstatus for a cancelled document.
CANCELLED = 2
# More vouchers than this for one Mercury transaction is not an amendment
# chain; `find_existing` refuses rather than read a truncated page.
_VOUCHERS_PER_TRANSACTION = 20

# The two account families where a two-line entry against the bank account is
# the whole truth: a bill paid, and a member putting money in. Revenue is
# deliberately absent -- a customer payment clears a Sales Invoice through a
# Payment Entry, and a bare entry crediting revenue would book the income twice
# and leave the invoice outstanding.
EXPENSE_PREFIX = "5"
MEMBER_CAPITAL_PREFIX = "31"

# `"<number> - <name>"`, the spelling `runningtab coa export-gl-codes` writes and the
# bank reports back. Only the number is read: an account renamed in the chart
# after the list was uploaded is still the same account.
_GL_CODE = re.compile(r"^\s*(?P<number>\d+)\s*-\s*(?P<name>\S.*?)\s*$")

# The scalar fields a draft carries, compared before it is rewritten.
_SCALARS = (
    "voucher_type",
    "company",
    "posting_date",
    "cheque_no",
    "cheque_date",
    "title",
    "remark",
    "custom_remark",
    TRANSACTION_ID_FIELD,
)


class Outcome(enum.StrEnum):
    """What happened to one transaction's voucher."""

    DRAFTED = "drafted"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    # A person edited the draft. Theirs now; nothing is written and nothing is
    # reported, because a report line every run would train them to ignore it.
    TAKEN_OVER = "taken-over"
    # Submitted or cancelled. A person committed it; not ours to touch.
    COMMITTED = "committed"
    # Its Bank Transaction is reconciled to something else, so the draft is no
    # longer needed. Reported, never deleted.
    UNNEEDED = "unneeded"
    # Its Bank Transaction was reconciled before this ever drafted anything.
    # The person has matched that statement line; there is nothing left to
    # propose, so nothing is drafted and nothing is said.
    MATCHED = "matched"


class Ledger(Protocol):
    """The slice of `icedcoffee_sync.frappe.client.Client` this module uses.

    `get` rather than a wider `list`, because the amounts and the accounts a
    draft carries live in a child table that no field list returns.
    """

    def list(self, doctype: str, **kwargs: Any) -> Any: ...
    def get(self, doctype: str, name: str) -> Any: ...
    def insert(self, doctype: str, doc: dict[str, Any]) -> Any: ...
    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any: ...


@dataclasses.dataclass(frozen=True)
class Chart:
    """The postable accounts of `chart.coa`, by number.

    Read from the repository rather than from the site, and through the same
    `postable_accounts` that produces the list uploaded to Mercury -- so the
    vocabulary the bank offers and the vocabulary this checks against are one
    thing by construction.
    """

    postable: dict[str, str]

    @classmethod
    def load(cls, accounts: Iterable[DesiredAccount]) -> Chart:
        """The chart as this module reads it, from the books directory's parsed chart."""
        chart = list(accounts)
        return cls({account.number: account.name for account in postable_accounts(chart)})

    def drafts_a_voucher(self, number: str) -> bool:
        """Whether a two-line entry against the bank account describes this account completely."""
        if number not in self.postable:
            return False
        return number.startswith(MEMBER_CAPITAL_PREFIX) or number.startswith(EXPENSE_PREFIX)


@dataclasses.dataclass(frozen=True)
class Unneeded:
    """A draft Journal Entry of ours that nothing needs any more, which a person deletes.

    Never deleted here: a person may have edited it. Carried as the document's
    name rather than folded into prose so the report can link straight to it.
    """

    transaction_id: str
    journal_entry: str
    why: str


@dataclasses.dataclass
class VoucherReport:
    """What one voucher pass did. Folded into the run's `IngestReport` by the caller.

    Returned rather than written into `IngestReport` directly so this module
    has no import back into `ingest`, and so its whole decision table is
    exercisable without a run.
    """

    drafted: list[str] = dataclasses.field(default_factory=list)
    updated: list[str] = dataclasses.field(default_factory=list)
    planned: list[str] = dataclasses.field(default_factory=list)
    attention: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    # A coded row dated on or before Books Closed Through. Its Bank
    # Transaction still posts -- that happens before this module ever sees the
    # row -- but no voucher is drafted or updated beside it: ERPNext would
    # refuse one dated in a locked quarter on submit. A person reopens that
    # quarter, books it there, and closes it again. See `_reopen_advice`.
    closed: list[str] = dataclasses.field(default_factory=list)
    unneeded: list[Unneeded] = dataclasses.field(default_factory=list)
    # Rows Mercury has no code for that the ledger accounts for anyway: a
    # voucher already carries their Mercury id, their Bank Transaction is
    # reconciled, or their payee's history drafted one. The run stops calling
    # these `uncoded`, because nobody has to code them any more.
    handled_in_ledger: set[str] = dataclasses.field(default_factory=set)


def coded_account(transaction: Transaction, chart: Chart) -> tuple[str | None, str | None]:
    """The chart account this transaction's confirmed GL code names, or why it drafts nothing.

    `(number, None)` when a voucher should be drafted. `(None, None)` when the
    row simply is not one this drafts -- no code at all, which the run already
    reports as `uncoded`, or a code outside the two account families, which a
    person picked deliberately and which rides to the reconciliation screen on
    the Bank Transaction either way. `(None, the code)` when the code itself is
    the problem, which is one problem for the whole run rather than one per row.

    Read from `Transaction.gl_code` -- `generalLedgerCodeName`, else the
    person's custom category, which is a GL-code label -- and never from
    `glAllocations`: the
    allocations are what Mercury's own rule engine derived, not what a person
    confirmed, and a split allocation is a shape this voucher does not model.
    """
    code = transaction.gl_code
    if not code:
        return None, None
    match = _GL_CODE.match(code)
    if match is None:
        return None, code
    number = match["number"]
    if number not in chart.postable:
        return None, code
    if not chart.drafts_a_voucher(number):
        return None, None
    return number, None


def funding_account(transaction: Transaction, ctx: PostingContext) -> str:
    """The chart account holding the Mercury account this transaction moved through.

    The same `[accounts]` entry `bank_account_documents` links a Bank Account
    to, so the voucher's bank line and the Bank Account's chart account are the
    same account -- which is what the reconciliation tool filters on.
    """
    number = ctx.mapping.account_for_mercury_account(
        transaction.account_id, ctx.mercury_account_names.get(transaction.account_id)
    )
    if number is None:
        raise PostingError(
            f"Mercury account {transaction.account_id} has no [accounts] entry in mercury.toml; "
            "a voucher's bank line has to be the chart account holding that balance"
        )
    return number


def reference_number(transaction: Transaction) -> str:
    """The reference a voucher and its Bank Transaction are matched on.

    The ACH trace number where the transaction has one, because that is what a
    statement export carries as `Tracking ID` and what a hand-imported row
    therefore holds; the Mercury id otherwise. Composed from the same two
    fields the Bank Transaction's `reference_number` is, and it has to stay
    that way: the tool's Journal Entry query ranks candidates on
    `je.cheque_no == transaction.reference_number`.
    """
    return (transaction.tracking_number or transaction.id)[:REFERENCE_LIMIT]


# The separator between the parts of a remark, matching the one a Bank
# Transaction's description uses, so the two read as one voice.
_REMARK_SEPARATOR = " · "


def _payee(transaction: Transaction) -> str | None:
    """Who the money went to, or came from. None when Mercury names no counterparty.

    Named on the voucher because a reviewer looking at a list of drafts sees
    the remark and nothing else; without it, telling one `5204` entry from the
    next means opening the Bank Transaction behind each.
    """
    if not transaction.counterparty_name:
        return None
    direction = "paid to" if transaction.amount < 0 else "received from"
    return f"{direction} {transaction.counterparty_name}"


def _remark(transaction: Transaction, code: str | None) -> str:
    """What the person reviewing the draft reads: their note, their code, the payee, and the row it came from."""
    parts = (transaction.note, code, _payee(transaction), f"mercury {transaction.id}")
    return _REMARK_SEPARATOR.join(part for part in parts if part)


def _title(transaction: Transaction, code: str | None) -> str:
    """What the Journal Entry list shows for a draft: who was paid, and what it was coded as."""
    return _REMARK_SEPARATOR.join(part for part in (transaction.counterparty_name, code) if part)


def _amount(value: Decimal) -> float:
    """A ledger amount as JSON. The one place `Decimal` becomes a float, after every decision is made."""
    return float(value)


def journal_entry(
    transaction: Transaction,
    number: str,
    ctx: PostingContext,
    *,
    code: str | None = None,
    copied_from: str | None = None,
) -> dict[str, Any]:
    """The draft voucher for one coded transaction, without a `docstatus` of any kind.

    `code` is what the row was coded as, when that is not the transaction's
    own GL code, and `copied_from` the submitted voucher a payee's history
    took it from; the remark names both.

    The description goes in `remark` with `custom_remark` set, and the list
    title is the payee and the code. ERPNext's `user_remark` on Journal Entry
    is hidden -- no list, report or form shows it -- and without
    `custom_remark` ERPNext rebuilds `remark` from the reference on every
    save. Left unset, `title` falls back to the first account line, which made
    every draft in the list read as the bank account the money moved through.

    Spend debits the coded account and credits the account the money left; a
    refund is the same entry with the sides swapped, which falls out of the
    sign rather than needing its own branch.

    Only `debit_in_account_currency` and `credit_in_account_currency` are
    written. ERPNext derives `debit` and `credit` from them on validate
    (`set_amounts_in_company_currency`), so stating both would be restating a
    number the server computes.

    No party on either line: member identity is the equity account itself, and
    ERPNext rejects a Shareholder party on an equity line.
    """
    label = code if code is not None else transaction.gl_code
    remark_code = f"{label} as {copied_from}" if label and copied_from else label
    coded = ctx.link(number)
    bank = ctx.link(funding_account(transaction, ctx))
    posting_date = transaction.accounting_date.isoformat()
    magnitude = abs(transaction.amount)
    outflow = transaction.amount < 0
    bank_line: dict[str, Any] = {
        "account": bank,
        # The dialog's own shape. The matching query reads `account`, but a
        # voucher a person opens should show the Bank Account link theirs does.
        "bank_account": ctx.bank_account(transaction),
        "debit_in_account_currency": 0 if outflow else _amount(magnitude),
        "credit_in_account_currency": _amount(magnitude) if outflow else 0,
    }
    coded_line: dict[str, Any] = {
        "account": coded,
        "debit_in_account_currency": _amount(magnitude) if outflow else 0,
        "credit_in_account_currency": 0 if outflow else _amount(magnitude),
    }
    return {
        "voucher_type": VOUCHER_TYPE,
        "company": ctx.company,
        "posting_date": posting_date,
        "cheque_no": reference_number(transaction),
        "cheque_date": posting_date,
        "title": _title(transaction, label),
        "remark": _remark(transaction, remark_code),
        "custom_remark": 1,
        TRANSACTION_ID_FIELD: transaction.id,
        "accounts": [bank_line, coded_line],
    }


def find_existing(ledger: Ledger, transaction_id: str) -> dict[str, Any] | None:
    """The voucher standing for a Mercury transaction: the live one, else a cancelled one, else none.

    A person corrects a submitted voucher by cancelling and amending it, and
    ERPNext's Amend copies the Mercury id onto the amendment, so one id can
    name a cancelled original and its live replacement. The cancelled ones are
    history. Only a second *live* voucher is a duplicate, and the field cannot
    be `unique` to stop one -- see `posting.custom_field_documents` -- so this
    lookup is what refuses it.

    With nothing live, a cancelled voucher is still returned: a person
    cancelling ours without replacing it is their decision about the row, and
    the callers leave it alone rather than drafting it again.
    """
    rows = ledger.list(
        JOURNAL_ENTRY,
        fields=["name", "docstatus", "modified_by"],
        filters=[[TRANSACTION_ID_FIELD, "=", transaction_id]],
        limit=_VOUCHERS_PER_TRANSACTION,
    )
    if len(rows) >= _VOUCHERS_PER_TRANSACTION:
        # A full page cannot say whether a live voucher sits beyond it.
        raise PostingError(
            f"{JOURNAL_ENTRY} has {_VOUCHERS_PER_TRANSACTION} or more documents for Mercury transaction "
            f"{transaction_id}; a person looks at why before ingesting again"
        )
    live = [row for row in rows if int(row.get("docstatus") or 0) != CANCELLED]
    if len(live) > 1:
        # Rewriting one of them at random leaves the other standing.
        names = ", ".join(str(row.get("name")) for row in live)
        raise PostingError(
            f"{JOURNAL_ENTRY} has more than one live document for Mercury transaction {transaction_id} ({names}); "
            f"a person cancels or deletes the duplicate before ingesting again"
        )
    if live:
        return live[0]
    return rows[0] if rows else None


def bank_transaction_reconciled(ledger: Ledger, transaction_id: str) -> bool:
    """Whether this transaction's Bank Transaction has already been matched to a voucher."""
    rows = ledger.list(
        BANK_TRANSACTION,
        fields=["name", "status"],
        filters=[[TRANSACTION_ID_FIELD, "=", transaction_id]],
        limit=1,
    )
    return bool(rows) and str(rows[0].get("status") or "") == RECONCILED


def _text(value: Any) -> str:
    """A field as text for comparison. Frappe returns dates as strings and unset values as null."""
    return "" if value is None else str(value)


def _rows(doc: dict[str, Any]) -> list[tuple[str, Decimal, Decimal]]:
    """A voucher's lines as (account, debit, credit), exactly, for comparison."""
    return [
        (
            _text(row.get("account")),
            Decimal(_text(row.get("debit_in_account_currency") or 0)),
            Decimal(_text(row.get("credit_in_account_currency") or 0)),
        )
        for row in doc.get("accounts") or ()
    ]


def differences(current: dict[str, Any], doc: dict[str, Any]) -> list[str]:
    """Which fields of a draft the run would now write differently.

    Read back whole rather than by field list: a recoded GL code changes an
    account and an amount that settled differently changes a number, and both
    of those live in the child table.
    """
    changed = [field for field in _SCALARS if _text(current.get(field)) != _text(doc[field])]
    if _rows(current) != _rows(doc):
        changed.append("accounts")
    return changed


def plan_voucher(ledger: Ledger, transaction: Transaction, doc: dict[str, Any], bot_user: str) -> tuple[Outcome, str]:
    """Decide what to do with one transaction's voucher, writing nothing.

    The order matters. A submitted or cancelled voucher and one a person has
    edited are both theirs, and neither is reported -- the second is a person
    taking the proposal over, which is what the proposal is for. Only once a
    draft is established as ours does the question of whether it is still
    wanted, or still right, arise.

    A row whose Bank Transaction is already reconciled gets nothing new. The
    person has matched that statement line to a voucher of their own, so
    drafting one now would be clutter, and clutter this would then report as
    unneeded on every later run. A dry run plans the draft anyway, because a
    dry run has posted no Bank Transaction for this to find.
    """
    existing = find_existing(ledger, transaction.id)
    if existing is None:
        if bank_transaction_reconciled(ledger, transaction.id):
            return Outcome.MATCHED, ""
        return Outcome.DRAFTED, ""
    name = str(existing["name"])
    if int(existing.get("docstatus") or 0) != 0:
        return Outcome.COMMITTED, name
    if _text(existing.get("modified_by")) != bot_user:
        return Outcome.TAKEN_OVER, name
    if bank_transaction_reconciled(ledger, transaction.id):
        return Outcome.UNNEEDED, name
    if not differences(ledger.get(JOURNAL_ENTRY, name), doc):
        return Outcome.UNCHANGED, name
    return Outcome.UPDATED, name


def _record(
    ledger: Ledger,
    transaction: Transaction,
    doc: dict[str, Any],
    report: VoucherReport,
    *,
    bot_user: str,
    dry_run: bool,
) -> None:
    """Carry out the plan for one transaction, or say what it would have been."""
    outcome, name = plan_voucher(ledger, transaction, doc, bot_user)
    if outcome is Outcome.UNNEEDED:
        report.unneeded.append(Unneeded(transaction.id, str(name), "its Bank Transaction is reconciled"))
        return
    if outcome is Outcome.DRAFTED:
        if dry_run:
            report.planned.append(f"draft {JOURNAL_ENTRY} for {transaction.id}")
            return
        created = ledger.insert(JOURNAL_ENTRY, doc)
        report.drafted.append(str(created.get("name", "")) if isinstance(created, dict) else "")
        return
    if outcome is Outcome.UPDATED:
        if dry_run:
            report.planned.append(f"update {JOURNAL_ENTRY} {name} for {transaction.id}")
            return
        ledger.update(JOURNAL_ENTRY, name, doc)
        report.updated.append(name)


class PayeeHistory:
    """The one account a person has booked a payee to, read from the vouchers they submitted.

    A payee is Mercury's `counterpartyId`, which every row from one merchant
    shares. History counts only a submitted voucher carrying a Mercury id: a
    draft is a proposal nobody has agreed to, and a cancelled voucher is one
    somebody took back. It answers only when every such voucher went to the
    same single account. A payee booked to several -- a person reimbursed for
    many kinds of spend -- has no answer, because copying whichever came last
    would be a guess wearing a precedent.

    Read lazily, once per payee per run, and only for payees with an uncoded
    row: each answer costs a lookup per row of that payee's.
    """

    def __init__(self, ledger: Ledger, transactions: list[Transaction], ctx: PostingContext, chart: Chart) -> None:
        self._ledger = ledger
        self._transactions = transactions
        self._ctx = ctx
        self._chart = chart
        self._numbers = {name: number for number, name in ctx.account_names.items()}
        self._answers: dict[str, tuple[str, str] | None] = {}

    def account_for(self, payee: str | None) -> tuple[str, str] | None:
        """`(chart number, the latest voucher booked to it)` for this payee, or None when history cannot say."""
        if payee is None:
            return None
        if payee not in self._answers:
            self._answers[payee] = self._read(payee)
        return self._answers[payee]

    def _read(self, payee: str) -> tuple[str, str] | None:
        booked: list[tuple[str, str, str]] = []
        for transaction in self._transactions:
            if transaction.counterparty_id != payee:
                continue
            existing = find_existing(self._ledger, transaction.id)
            if existing is None or int(existing.get("docstatus") or 0) != 1:
                continue
            name = str(existing["name"])
            voucher = self._ledger.get(JOURNAL_ENTRY, name)
            bank = self._ctx.link(funding_account(transaction, self._ctx))
            others = {_text(row.get("account")) for row in voucher.get("accounts") or ()} - {bank}
            numbers = {self._numbers.get(account) for account in others}
            if len(numbers) != 1 or None in numbers:
                # A split voucher, or an account outside the chart: no single answer.
                return None
            booked.append((_text(voucher.get("posting_date")), name, numbers.pop() or ""))
        if not booked or len({number for _, _, number in booked}) != 1:
            return None
        _, latest, number = max(booked)
        if number not in self._chart.postable:
            return None
        return number, latest


def _draft_from_history(
    ledger: Ledger,
    transaction: Transaction,
    ctx: PostingContext,
    chart: Chart,
    history: PayeeHistory,
    report: VoucherReport,
    *,
    dry_run: bool,
) -> None:
    """For a row Mercury has no code for: leave a ledger decision alone, else repeat the payee's.

    A voucher already carrying the row's Mercury id is a person's coding in
    the ledger, or a history draft from an earlier run; either way it is
    neither rewritten nor reported. A cancelled one is neither, and leaves the
    row uncoded. A reconciled Bank Transaction is a person
    having decided where the row goes. Unlike a Mercury code, history drafts
    any postable account, revenue included: it repeats a voucher a person
    already submitted for this payee -- card cashback booked to income -- so
    the invoice concern that keeps a revenue code from drafting does not apply.
    """
    # Looked up first even when the row is reconciled: it is also what refuses
    # two vouchers carrying one Mercury id.
    existing = find_existing(ledger, transaction.id)
    if bank_transaction_reconciled(ledger, transaction.id):
        report.handled_in_ledger.add(transaction.id)
        return
    if existing is not None and int(existing.get("docstatus") or 0) == CANCELLED:
        # Cancelled: it booked nothing, and its Mercury id still blocks a new
        # draft. The row stays uncoded, which is the honest report.
        return
    if existing is not None:
        report.handled_in_ledger.add(transaction.id)
        return
    learned = history.account_for(transaction.counterparty_id)
    if learned is None:
        return
    number, source = learned
    report.handled_in_ledger.add(transaction.id)
    label = f"{number} - {chart.postable[number]}"
    closed_through = ctx.lock.closed_through
    if closed_through is not None and transaction.accounting_date <= closed_through:
        _report_closed_row(ledger, transaction, label, closed_through, report, ctx.bot_user)
        return
    try:
        doc = journal_entry(transaction, number, ctx, code=label, copied_from=source)
    except PostingError as exc:
        report.attention.append((transaction.id, str(exc)))
        return
    _record(ledger, transaction, doc, report, bot_user=ctx.bot_user, dry_run=dry_run)


# How far apart the two halves of one transfer may be dated. They settle the
# same day in every pair observed; the slack absorbs a weekend, and is far
# short of the month between two autopays of the same amount.
TRANSFER_WINDOW_DAYS = 5


def transfer_pairs(
    transactions: Iterable[Transaction], own_account_ids: Iterable[str]
) -> tuple[list[tuple[Transaction, Transaction]], list[tuple[Transaction, str]]]:
    """Pair the two statement lines of each movement between our own accounts.

    Returns `(out, in)` pairs -- the half the money left, then the half it
    arrived on -- and every settled half that could not be paired, with why.
    Two halves pair only when each is the other's one and only match: the
    same amount with opposite signs, each naming the other's account as its
    counterparty, dated within `TRANSFER_WINDOW_DAYS`. Anything else is
    reported rather than guessed, because a wrong pair books a payment
    against the wrong statement line.
    """
    own = frozenset(own_account_ids)
    halves = [t for t in transactions if classify(t).posts and is_internal_transfer(t, own)]

    def partners(half: Transaction) -> list[Transaction]:
        return [
            other
            for other in halves
            if other.account_id == half.counterparty_id
            and other.counterparty_id == half.account_id
            and other.amount == -half.amount
            and abs((other.accounting_date - half.accounting_date).days) <= TRANSFER_WINDOW_DAYS
        ]

    pairs: list[tuple[Transaction, Transaction]] = []
    unpaired: list[tuple[Transaction, str]] = []
    for half in halves:
        found = partners(half)
        if len(found) == 1 and partners(found[0]) == [half]:
            if half.amount < 0:
                pairs.append((half, found[0]))
        elif not found:
            unpaired.append((half, "no matching other half on the account it names"))
        else:
            unpaired.append((half, f"{len(found)} possible other halves, so none is assumed"))
    return pairs, unpaired


def transfer_entry(out: Transaction, into: Transaction, ctx: PostingContext) -> dict[str, Any]:
    """The draft voucher both halves of one transfer are matched to.

    Debits the account the money arrived on and credits the one it left --
    for a card payment, the card's liability falls and checking falls with it.
    Identified by the outgoing half's Mercury id, so the unique field keeps it
    to one voucher; the remark names both halves.
    """
    magnitude = abs(out.amount)
    posting_date = out.accounting_date.isoformat()
    destination = f"Transfer to {out.counterparty_name}" if out.counterparty_name else "Transfer"
    remark = _REMARK_SEPARATOR.join(
        part for part in (out.note, f"transfer {magnitude:.2f}", f"mercury {out.id}", f"mercury {into.id}") if part
    )
    return {
        "voucher_type": VOUCHER_TYPE,
        "company": ctx.company,
        "posting_date": posting_date,
        "cheque_no": reference_number(out),
        "cheque_date": posting_date,
        "title": f"{destination} · {magnitude:.2f}",
        "remark": remark,
        "custom_remark": 1,
        TRANSACTION_ID_FIELD: out.id,
        "accounts": [
            {
                "account": ctx.link(funding_account(into, ctx)),
                "bank_account": ctx.bank_account(into),
                "debit_in_account_currency": _amount(magnitude),
                "credit_in_account_currency": 0,
            },
            {
                "account": ctx.link(funding_account(out, ctx)),
                "bank_account": ctx.bank_account(out),
                "debit_in_account_currency": 0,
                "credit_in_account_currency": _amount(magnitude),
            },
        ],
    }


def _draft_transfers(
    ledger: Ledger,
    transactions: list[Transaction],
    ctx: PostingContext,
    report: VoucherReport,
    *,
    own_account_ids: Iterable[str],
    dry_run: bool,
) -> None:
    """Draft one voucher per paired transfer, and report every half that could not be paired."""
    pairs, unpaired = transfer_pairs(transactions, own_account_ids)
    for half, why in unpaired:
        existing = find_existing(ledger, half.id)
        draft = (
            f"; its earlier draft {existing['name']} was paired before and should be checked before anyone submits it"
            if existing is not None and int(existing.get("docstatus") or 0) == 0
            else "; no transfer voucher is drafted until both halves pair"
        )
        report.attention.append((
            half.id,
            f"transfer of {abs(half.amount):.2f} dated {half.accounting_date.isoformat()}: {why}{draft}",
        ))
    closed_through = ctx.lock.closed_through
    for out, into in pairs:
        if closed_through is not None and min(out.accounting_date, into.accounting_date) <= closed_through:
            _report_closed_transfer(ledger, out, into, closed_through, report)
            continue
        if _booked_by_a_person(ledger, out, into, report):
            continue
        try:
            doc = transfer_entry(out, into, ctx)
        except PostingError as exc:
            report.attention.append((out.id, str(exc)))
            continue
        _record(ledger, out, doc, report, bot_user=ctx.bot_user, dry_run=dry_run)


def _report_closed_transfer(
    ledger: Ledger, out: Transaction, into: Transaction, closed_through: dt.date, report: VoucherReport
) -> None:
    """A transfer with either half on or before Books Closed Through: never drafted, reported until settled.

    One rule: report it until both halves are booked -- reconciled, or covered
    by our transfer voucher once submitted -- and no draft of ours remains.
    The line says which half is booked and names any leftover draft, and
    promises no future draft: a person decides how a closed quarter is
    corrected, and a leftover draft could otherwise be submitted to book the
    movement again.
    """
    existing = find_existing(ledger, out.id)
    status = int(existing.get("docstatus") or 0) if existing is not None else None
    ours_submitted = status == 1
    halves = [(half.id, ours_submitted or bank_transaction_reconciled(ledger, half.id)) for half in (out, into)]
    leftover = str(existing["name"]) if existing is not None and status == 0 else None
    if all(booked for _, booked in halves) and leftover is None:
        return
    states = ", ".join(f"{half_id} {'booked' if booked else 'not booked'}" for half_id, booked in halves)
    quarter = Quarter.containing(min(out.accounting_date, into.accounting_date))
    advice = (
        f"reopen {quarter.label} in ERPNext (move Books Closed Through back to {quarter.previous.end.isoformat()}), "
        "book a transfer voucher by hand and match each unbooked half to it, then run "
        f"`runningtab period close --quarter {quarter.label}` again"
    )
    if closed_through > quarter.end:
        advice += " and re-close every closed quarter after it, in order"
    if leftover is not None:
        advice += f"; draft {JOURNAL_ENTRY} {leftover} was made before the close and a person deletes it"
    report.closed.append(
        f"{out.id} {out.accounting_date.isoformat()} transfer of {abs(out.amount):.2f}: closed period — "
        f"{states}; {advice}"
    )


def _booked_by_a_person(ledger: Ledger, out: Transaction, into: Transaction, report: VoucherReport) -> bool:
    """Whether a person reconciled a half to something other than our voucher; reported, and nothing drafted.

    A reconciled half can only be matched to a submitted voucher, so a half
    reconciled while ours is absent or still a draft was booked by something
    else. Ours would book the movement twice.
    """
    existing = find_existing(ledger, out.id)
    if bank_transaction_reconciled(ledger, into.id):
        if existing is None:
            report.attention.append((
                out.id,
                f"transfer of {abs(out.amount):.2f}: its incoming half ({into.id}) is already reconciled, "
                "so no transfer voucher is drafted; match this half to the same voucher",
            ))
            return True
        if int(existing.get("docstatus") or 0) == 0:
            report.unneeded.append(
                Unneeded(out.id, str(existing["name"]), f"its incoming half {into.id} is reconciled to another voucher")
            )
            return True
        return False
    if existing is None and bank_transaction_reconciled(ledger, out.id):
        report.attention.append((
            out.id,
            f"transfer of {abs(out.amount):.2f}: this half is reconciled to a voucher of a person's own, "
            f"so no transfer voucher is drafted; match its incoming half ({into.id}) to the same voucher",
        ))
        return True
    return False


def candidates(transactions: Iterable[Transaction], own_account_ids: Iterable[str]) -> Iterator[Transaction]:
    """The settled, non-transfer rows a voucher could be drafted for.

    A transfer between two of our own accounts is one economic event seen
    twice, and a voucher per leg would double-count a balance-sheet movement.
    The reconciliation tool already matches the pair as a transfer.
    """
    own = frozenset(own_account_ids)
    for transaction in transactions:
        if classify(transaction).posts and not is_internal_transfer(transaction, own):
            yield transaction


@dataclasses.dataclass(frozen=True)
class _ClosedRow:
    """Where a row dated in a closed quarter stands: booked or not, and any draft of ours it left behind."""

    booked: bool
    leftover_draft: str | None = None


def _closed_row(ledger: Ledger, transaction_id: str, bot_user: str) -> _ClosedRow:
    """Whether a row dated in a closed quarter already reached the ledger before the close.

    That is every row a quarter was closed with: its voucher submitted
    (docstatus 1 -- a cancelled one, 2, booked nothing), or its Bank
    Transaction reconciled against a voucher of the person's own. The voucher
    pass sweeps every transaction on every run, so only a row with neither --
    late bank activity, or a draft left unsubmitted when the quarter closed --
    is the person's to reopen for. A reconciled row can still carry a draft
    this module proposed and nobody took over; that draft is clutter a person
    deletes, the same as `Outcome.UNNEEDED` in an open quarter.
    """
    existing = find_existing(ledger, transaction_id)
    status = int(existing.get("docstatus") or 0) if existing is not None else None
    if status == 1:
        return _ClosedRow(booked=True)
    if not bank_transaction_reconciled(ledger, transaction_id):
        return _ClosedRow(booked=False)
    ours = existing is not None and status == 0 and _text(existing.get("modified_by")) == bot_user
    return _ClosedRow(booked=True, leftover_draft=str(existing["name"]) if ours and existing is not None else None)


def _report_closed_row(
    ledger: Ledger,
    transaction: Transaction,
    account: str,
    closed_through: dt.date,
    report: VoucherReport,
    bot_user: str,
) -> None:
    """Report a coded row dated on or before Books Closed Through, if it is the person's to act on.

    The world as it is, not an error (spec's "Late bank activity"). No draft
    here, ever, and no update to one drafted before the quarter closed: an
    existing draft beside a now-closed row would only be a proposal ERPNext
    refuses the moment somebody submits it, and this run is not the one to
    decide what replaces it. A row booked before the close is left alone.
    """
    try:
        row = _closed_row(ledger, transaction.id, bot_user)
    except PostingError as exc:
        report.attention.append((transaction.id, str(exc)))
        return
    if row.leftover_draft is not None:
        report.unneeded.append(Unneeded(transaction.id, row.leftover_draft, "its Bank Transaction is reconciled"))
    if row.booked:
        return
    report.closed.append(
        f"{transaction.id} {transaction.accounting_date.isoformat()} {account}: "
        f"closed period — {_reopen_advice(transaction.accounting_date, closed_through)}"
    )


def _reopen_advice(accounting_date: dt.date, closed_through: dt.date) -> str:
    """How a row dated in a closed quarter reaches the ledger: reopen that quarter, book it there, close it again.

    Booking it in the open quarter instead would leave the closed quarter's
    rebuilt `bank-reconciliation.csv` different from its stored package for
    good, so `period status` could never pass again. Reopening moves Books
    Closed Through back, which is also what lets the next sync draft the
    voucher this run withheld; closing again stores a new revision, and any
    closed quarter after it is re-closed after it, in order.
    """
    quarter = Quarter.containing(accounting_date)
    advice = (
        f"reopen {quarter.label} in ERPNext (move Books Closed Through back to {quarter.previous.end.isoformat()}), "
        "book it there (the next sync drafts its voucher), and run "
        f"`runningtab period close --quarter {quarter.label}` again, which stores a new revision"
    )
    if closed_through > quarter.end:
        advice += "; then re-close every closed quarter after it, in order"
    return advice


def draft_vouchers(
    transactions: Iterable[Transaction],
    ctx: PostingContext,
    ledger: Ledger,
    *,
    own_account_ids: Iterable[str],
    dry_run: bool = False,
) -> VoucherReport:
    """Draft, or bring up to date, the voucher every coded row should have.

    Sweep-shaped, and it has to be, for the reason the GL-code hint sweep
    already is: **coding a transaction at Mercury emits no event and bumps no
    `resourceVersion`**, so a pass that waited for the log would never hear
    about the one field it keys on. It adds no Mercury read -- the run has
    already fetched every transaction for the balance invariant.

    A GL code the chart cannot place is reported **once for the run**, naming
    every such code. The cause is one thing, the list in Mercury having drifted
    from `chart.coa`, and a line per row would bury it.
    """
    report = VoucherReport()
    chart = ctx.chart
    transactions = list(transactions)
    history = PayeeHistory(ledger, transactions, ctx, chart)
    drifted: list[str] = []
    for transaction in candidates(transactions, own_account_ids):
        number, unplaceable = coded_account(transaction, chart)
        if unplaceable is not None:
            if unplaceable not in drifted:
                drifted.append(unplaceable)
            continue
        if number is None:
            # A code that drafts nothing (revenue) is a person's decision too;
            # history is only for a row nobody coded.
            if transaction.gl_code is None:
                _draft_from_history(ledger, transaction, ctx, chart, history, report, dry_run=dry_run)
            continue
        closed_through = ctx.lock.closed_through
        if closed_through is not None and transaction.accounting_date <= closed_through:
            _report_closed_row(
                ledger, transaction, f"{number} - {chart.postable[number]}", closed_through, report, ctx.bot_user
            )
            continue
        try:
            doc = journal_entry(transaction, number, ctx)
        except PostingError as exc:
            report.attention.append((transaction.id, str(exc)))
            continue
        _record(ledger, transaction, doc, report, bot_user=ctx.bot_user, dry_run=dry_run)
    _draft_transfers(ledger, transactions, ctx, report, own_account_ids=own_account_ids, dry_run=dry_run)
    if drifted:
        report.attention.append((
            "gl codes",
            f"{', '.join(repr(code) for code in drifted)} name no postable account in chart.coa, so the rows "
            "carrying them get no draft voucher; re-run `runningtab coa export-gl-codes` and re-upload the list "
            "to Mercury, or add the account to the chart",
        ))
    return report
