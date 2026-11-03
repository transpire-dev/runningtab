"""Turning a settled transaction into an ERPNext Bank Transaction, and submitting it.

A Bank Transaction is the one document this ingest submits. Submitting it posts
nothing to the general ledger -- ERPNext writes the GL entries when a person
reconciles the row to a voucher in the Bank Reconciliation Tool, which only
works on submitted rows -- so the bot submitting it commits a statement line,
not the books. The submit rides on the insert as `docstatus: 1`, which Frappe's
own insert path treats as a submit: one request, so a refused submit leaves no
draft behind. `bank_transaction` itself builds the document without a
`docstatus`, and only the plan for a brand-new row adds one, so an amendment or
an adoption can never submit a row a person left as a draft.

Idempotency is the ledger's, not the state file's. `mercury_transaction_id` is a
unique custom field on Bank Transaction, so a transaction that arrives twice --
a replayed event, a later note, a re-snapshot -- amends the row it already has
instead of posting a second one.

A row's `reference_number` and `description` are the two things ERPNext's
Banking workspace shows a person reconciling, so both are written for them to
read: the ACH trace number a statement would show, and the note and GL code
they coded at Mercury ahead of the bank's own text. Both are fixed when the row
is posted; the custom fields below stay the live copy of the note and the code.

A Bank Transaction somebody imported by hand through Bank Statement Import
carries none of those fields, but it does carry Mercury's `Tracking ID` -- the
ACH trace number -- as its `reference_number`, and the API reports the same
number as `trackingNumber`. That is the number this module writes too, so a
hand import and an ingested row agree on what a person reads as "Ref". A
transaction with no document of its own and
exactly one such row on its Bank Account **adopts** that row: Mercury's fields
are stamped onto it and nothing else is touched, so later events find it by id
like any other. It is the one case where this module writes to a submitted
document, and it writes only the fields that are `allow_on_submit`.
"""

from __future__ import annotations

import dataclasses
import enum
import re
from collections.abc import Iterator
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from runningtab.mercury.mapping import Mapping
from runningtab.mercury.models import Transaction
from runningtab.period.lock import LockState

if TYPE_CHECKING:
    # Annotation only: `runningtab.mercury.vouchers` imports this module.
    from runningtab.mercury.vouchers import Chart

BANK_TRANSACTION = "Bank Transaction"
# Not written here -- `runningtab.mercury.vouchers` drafts the voucher -- but named
# here because this module owns the custom-field install spec, and the voucher
# is found by the same Mercury id.
JOURNAL_ENTRY = "Journal Entry"

TRANSACTION_ID_FIELD = "mercury_transaction_id"
RESOURCE_VERSION_FIELD = "mercury_resource_version"
GL_CODE_FIELD = "mercury_gl_code"
NOTE_FIELD = "mercury_note"

# What Mercury says about a Bank Transaction, as opposed to what the Bank
# Transaction says happened. These four are the only fields ever written to a
# submitted document -- they describe the row's provenance, not the money --
# and they are `allow_on_submit` on Bank Transaction for exactly that reason.
MERCURY_FIELDS = (TRANSACTION_ID_FIELD, RESOURCE_VERSION_FIELD, GL_CODE_FIELD, NOTE_FIELD)

# What a Bank Transaction says happened. A submitted document's values here are
# a person's commitment, and the ingest never changes them; it reports a
# disagreement instead.
BANK_TRANSACTION_CONTENT = ("date", "deposit", "withdrawal", "description", "bank_account")

# `reference_number` is deliberately not among them, and is compared nowhere.
# Two reasons, and either alone would settle it. Ours is spelled two ways
# across time -- rows posted before the trace number was preferred carry the
# Mercury transaction id -- and reporting our own change of mind as the
# person's disagreement would refuse every note that arrived after it. And an
# adopted row keeps whatever reference the person meant by it, a check number
# or an invoice, because adoption writes only the Mercury fields; that string
# is theirs, so the ingest neither reads it back nor holds them to it.

# The parts of a description, in the order a person needs them: what they wrote
# about this payment, the account they expect to pick, and then the bank's own
# words. ERPNext's Banking workspace shows a card with the date, the reference,
# the description and the amount and nothing else, so a hint left in a custom
# field is work the person has to do again by opening the row.
DESCRIPTION_SEPARATOR = " · "

# What a person has to know when a submitted document and Mercury disagree, or
# when a document somebody submitted is not one the ingest may amend at all.
_SUBMITTED = "is already submitted; a person decides whether it still says the right thing"


class PostingError(RuntimeError):
    """A transaction cannot be posted as described."""


_MARKUP = re.compile(r"</?strong>")


def plain_message(message: str) -> str:
    """A Frappe message without the emphasis it wrote for a browser.

    The site wraps each value in `<strong>`, which in a terminal is noise
    around the only part of the sentence a person needs. Here rather than in
    one of the callers because both the posting path and the receipt path
    turn a refusal into a report line.
    """
    return _MARKUP.sub("", message).strip()


class Outcome(enum.StrEnum):
    """What happened to a document."""

    CREATED = "created"
    # A new Bank Transaction, inserted already submitted. Its own outcome
    # rather than CREATED because the report has to say plainly that the row
    # is past draft -- the one place this ingest is.
    SUBMITTED = "submitted"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    REFUSED = "refused"
    # A row somebody imported by hand, recognised by its trace number and
    # stamped with Mercury's fields. Neither created nor updated: the ingest
    # wrote no content, and the person reading the report wants to know which
    # of their imports it now accounts for.
    ADOPTED = "adopted"


@dataclasses.dataclass(frozen=True)
class Plan:
    """What `upsert` would do for one document, decided before anything is written.

    A dry run reports this; a real run carries it out. Deciding once and in
    one place is what keeps the two from disagreeing -- a dry run that plans a
    create where the real run would adopt is a plan nobody can act on.
    """

    outcome: Outcome
    doctype: str
    name: str = ""
    body: dict[str, Any] = dataclasses.field(default_factory=dict)
    reason: str = ""


class Ledger(Protocol):
    """The slice of `icedcoffee_sync.frappe.client.Client` this module uses."""

    def list(self, doctype: str, **kwargs: Any) -> Any: ...
    def iter_all(self, doctype: str, **kwargs: Any) -> Iterator[dict[str, Any]]: ...
    # Neither of these is used here. Named so that one ledger type describes
    # everything a run needs: the whole-document read `runningtab.mercury.vouchers`
    # makes to compare a draft's child table, and the multipart upload
    # `runningtab.mercury.receipts` makes to attach a receipt.
    def get(self, doctype: str, name: str) -> Any: ...
    def insert(self, doctype: str, doc: dict[str, Any]) -> Any: ...
    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any: ...
    def upload_file(
        self, doctype: str, docname: str, filename: str, content: bytes, *, is_private: bool = True
    ) -> Any: ...


@dataclasses.dataclass(frozen=True)
class PostingContext:
    """Everything a document needs that is not on the transaction.

    `account_names` maps a chart account number to the ERPNext document name it
    became, which is derived from the number and title -- callers never build
    that string themselves.

    `lock` is Books Closed Through, read once per run alongside Company in
    `runningtab.mercury.session.posting_context`, which refuses by name when the
    bot cannot see the lock fields at all. Its default -- no date known --
    is for a caller that builds a context by hand (the tests), and drafts a
    voucher for every coded row. Nothing downstream backs that default up:
    ERPNext's `check_freezing_date` runs only when GL entries are made, on
    submit, and the ingest saves vouchers as drafts, so a draft dated in a
    frozen quarter is accepted. The check in `runningtab.mercury.vouchers` is the
    only guard.
    """

    company: str
    account_names: dict[str, str]
    mapping: Mapping
    # The books bot's user. A draft voucher it last modified is its own to
    # update; one anybody else touched has been taken over by a person.
    bot_user: str
    # The chart's postable accounts, which decide what a GL code drafts.
    chart: Chart
    mercury_account_names: dict[str, str] = dataclasses.field(default_factory=dict)
    lock: LockState = dataclasses.field(default_factory=lambda: LockState(closed_through=None, bypass_role=None))

    def link(self, number: str) -> str:
        """The ERPNext document name for a chart account number."""
        name = self.account_names.get(number)
        if name is None:
            raise PostingError(f"account {number} is not on the site; apply the chart before ingesting")
        return name

    def bank_account(self, transaction: Transaction) -> str:
        """The ERPNext Bank Account document this transaction's account attaches to."""
        name = self.mapping.bank_account_for(
            transaction.account_id, self.mercury_account_names.get(transaction.account_id)
        )
        if name is None:
            raise PostingError(
                f"Mercury account {transaction.account_id} has no [bank_accounts] entry in mercury.toml; "
                "ERPNext requires a Bank Account document on every Bank Transaction, and posting this "
                "against another account's would be wrong in the ledger and invisible in reconciliation"
            )
        return name


def _amount(value: Decimal) -> float:
    """A ledger amount as JSON.

    ERPNext takes numbers, so a float is unavoidable at the wire; the conversion
    happens here, once, after every arithmetic decision has already been made in
    `Decimal`.
    """
    return float(value)


def bank_text(transaction: Transaction) -> str:
    """What the bank calls this movement.

    The only part of a description that is the bank's own fact rather than
    something we composed, which is why it is the part `_content_disagreements`
    holds a submitted row to.
    """
    return (transaction.bank_description or transaction.counterparty_name or transaction.kind).strip()


def statement_description(transaction: Transaction) -> str:
    """The person's note, the GL code, then the bank's text, joined by `DESCRIPTION_SEPARATOR`.

    Composed from the parts that exist, so a transaction nobody coded reads
    exactly as it did before this composition existed -- the bank's text alone,
    with no separator hanging off either end.

    This is content we author once, when the row is posted. The custom fields
    stay the live copy of the note and the GL code: they are `allow_on_submit`
    and a later edit at Mercury lands there by sweep, while the description of
    a submitted row is the person's committed statement line and stays put.
    """
    parts = (transaction.note, transaction.gl_code, bank_text(transaction))
    return DESCRIPTION_SEPARATOR.join(part.strip() for part in parts if part and part.strip())


def bank_transaction(transaction: Transaction, ctx: PostingContext) -> dict[str, Any]:
    """One statement line, for the reconciliation tool.

    This is where the dogfooding lives: an outgoing payment has to find the
    draft Purchase Invoice the AP path created from the emailed PDF, and that
    match is the product thesis run against books we control.

    Attached to the Bank Account of the Mercury account the money actually
    moved through. The ingest reads checking, savings and the card, and a row
    posted against another account's Bank Account contaminates exactly the
    reconciliation this doctype exists for.
    """
    bank_account = ctx.bank_account(transaction)
    deposit = transaction.amount if transaction.amount > 0 else Decimal(0)
    withdrawal = -transaction.amount if transaction.amount < 0 else Decimal(0)
    doc: dict[str, Any] = {
        "doctype": BANK_TRANSACTION,
        "date": transaction.accounting_date.isoformat(),
        "bank_account": bank_account,
        "company": ctx.company,
        "deposit": _amount(deposit),
        "withdrawal": _amount(withdrawal),
        "description": statement_description(transaction),
        # The ACH trace number when Mercury has one: that is what a bank
        # statement shows, what a hand import carries, and the very value
        # adoption matches on, so an ingested row and a hand-imported one read
        # the same "Ref" on the reconciliation card. A card charge never went
        # through the ACH network and has none, so its reference is the Mercury
        # transaction id -- a poor thing to read and a real one, since it names
        # the row at the bank.
        "reference_number": transaction.tracking_number or transaction.id,
        TRANSACTION_ID_FIELD: transaction.id,
    }
    # What a person coded in Mercury rides along for the person reconciling.
    # The GL code is carried exactly as Mercury reports it -- the same
    # `"<number> - <name>"` string the `GL Code` column of a statement export
    # holds -- so a row posted here and a row imported by hand read the same.
    if transaction.gl_code:
        doc[GL_CODE_FIELD] = transaction.gl_code
    if transaction.note:
        doc[NOTE_FIELD] = transaction.note
    return doc


# What a lookup reads back: enough to compare a submitted row's content with
# Mercury's, and to see which Mercury fields are already on it.
_LOOKUP_FIELDS = ["name", "docstatus", *BANK_TRANSACTION_CONTENT, *MERCURY_FIELDS]


def find_existing(ledger: Ledger, transaction_id: str) -> dict[str, Any] | None:
    """The Bank Transaction already posted for a Mercury transaction, if there is one."""
    rows = ledger.list(
        BANK_TRANSACTION,
        fields=_LOOKUP_FIELDS,
        filters=[[TRANSACTION_ID_FIELD, "=", transaction_id]],
        limit=2,
    )
    if not rows:
        return None
    if len(rows) > 1:
        # Two documents for one transaction is the state the unique custom
        # field exists to prevent, so reaching it means the field was never
        # installed or was installed without `unique`. Amending one of them at
        # random would leave the other in the ledger unnoticed.
        names = ", ".join(str(row.get("name")) for row in rows)
        raise PostingError(
            f"{BANK_TRANSACTION} has more than one document for Mercury transaction {transaction_id} ({names}); "
            f"run `runningtab mercury install-fields` and remove the duplicate before ingesting again"
        )
    return rows[0]


def posted_transaction_ids(ledger: Ledger) -> dict[str, str]:
    """Every Mercury transaction id the ledger already holds a Bank Transaction for.

    Maps the id to the name of the document carrying it.

    Paged with `iter_all` rather than one capped `list`. The caller uses this
    to find rows naming a transaction Mercury no longer returns, and a read cut
    short at the page limit looks exactly like "nothing is orphaned" -- the
    answer that lets the cursor move past them.

    The `mercury_transaction_id` filter is applied here rather than sent, so
    this needs nothing of the server beyond listing the doctype. A Bank
    Transaction somebody imported by hand carries no Mercury id until a run
    adopts it, and until then it is none of this module's business.
    """
    posted: dict[str, str] = {}
    for row in ledger.iter_all(BANK_TRANSACTION, fields=["name", TRANSACTION_ID_FIELD]):
        transaction_id = row.get(TRANSACTION_ID_FIELD)
        if transaction_id:
            posted[str(transaction_id)] = str(row.get("name", ""))
    return posted


def find_by_reference(ledger: Ledger, bank_account: str, reference: str) -> list[dict[str, Any]]:
    """The Bank Transactions on one Bank Account whose `reference_number` is this trace number.

    Narrowed to the Bank Account because a trace number identifies an ACH
    entry, and a transfer between two of our accounts is the same entry seen
    from both sides. Cancelled rows are left out: a document somebody cancelled
    is not one they want Mercury's identity stamped onto.
    """
    rows = ledger.list(
        BANK_TRANSACTION,
        fields=_LOOKUP_FIELDS,
        filters=[["bank_account", "=", bank_account], ["reference_number", "=", reference]],
        limit=5,
    )
    return [row for row in rows if int(row.get("docstatus") or 0) != 2]


def _mercury_fields(body: dict[str, Any]) -> dict[str, Any]:
    return {k: body[k] for k in MERCURY_FIELDS if k in body}


def _content_disagreements(existing: dict[str, Any], body: dict[str, Any], transaction: Transaction) -> list[str]:
    """Where a Bank Transaction's content says something Mercury does not.

    The comparison is against Mercury's facts, not against the ingest's own
    spelling of them. A row imported by hand carries the export's `Description`
    and `Date (UTC)`, which are the counterparty's name and the day the
    transaction was created; the ingest would have written the bank description
    and the day it posted. Both describe the same transaction truthfully, so
    any of Mercury's own spellings agrees, and only a value Mercury never said
    -- a different amount, another account, an unrelated description -- is a
    disagreement.

    The description is held to the bank's text alone. Everything before it is
    ours: a note and a GL code composed in when the row was posted, either of
    which a person may have recoded at Mercury since, and the composition rule
    itself has changed once already. Holding a submitted row to a prefix we
    wrote would refuse a person's own note for disagreeing with our older
    spelling of their older note -- the first law inverted. So a description
    agrees when it ends with the bank text, which the whole composed string
    does and so does a row posted before any prefix existed, or when it is one
    of Mercury's own spellings of that movement. The bank's words changing
    under a row somebody committed is still refused by name.
    """
    disagreements: list[str] = []
    for field in ("deposit", "withdrawal"):
        if Decimal(str(existing.get(field) or 0)) != Decimal(str(body[field])):
            disagreements.append(f"{field} {existing.get(field)} -> {body[field]}")
    if existing.get("bank_account") != body["bank_account"]:
        disagreements.append(f"bank_account {existing.get('bank_account')!r} -> {body['bank_account']!r}")
    date = str(existing.get("date") or "")[:10]
    dates = {transaction.accounting_date.isoformat(), transaction.created_at.date().isoformat()}
    if date not in dates:
        disagreements.append(f"date {date!r} -> {body['date']!r}")
    description = str(existing.get("description") or "").strip()
    spellings = {
        s.strip() for s in (transaction.bank_description, transaction.counterparty_name, transaction.kind) if s
    }
    if description not in spellings and not description.endswith(bank_text(transaction)):
        disagreements.append(f"description {description!r} -> {body['description']!r}")
    return disagreements


def plan(
    ledger: Ledger,
    doc: dict[str, Any],
    transaction: Transaction,
    *,
    resource_version: int | None = None,
) -> Plan:
    """Decide what `upsert` would do for this document, writing nothing.

    Never writes content to a document somebody has submitted. A change at
    Mercury to a transaction whose row a person already committed is not ours
    to rewrite -- it is theirs to know about, so it comes back `REFUSED` and
    lands in the report rather than in the ledger. The one narrowing: on a
    submitted Bank Transaction whose content agrees with Mercury, the Mercury
    fields alone may still be written. They say where the row came from and
    what a person coded at the bank, not what happened, and a note or GL code
    that arrives after the person submitted is exactly what those fields are
    for; a person keeps every decision about the money.

    A transaction with no document of its own, carrying a trace number, looks
    for a row imported by hand: same Bank Account, same number in
    `reference_number`. One match is adopted. Two is not a match, it is a
    question -- a statement imported twice, a number typed onto the wrong row
    -- and picking either would stamp Mercury's identity on a document that
    may be the wrong one, so it raises and the run reports it.

    **The lookup-then-insert is not atomic**, and it guards against a re-run,
    never against two runs at once. Two concurrent ingests could each find
    nothing and both insert; `unique` on the transaction id makes the server
    refuse the second, so the failure is loud rather than a duplicate. Left
    that way deliberately: the ingest is a single-writer CLI with no scheduler
    behind it. Revisit when something runs this on a timer or a webhook
    consumer runs alongside it.
    """
    transaction_id = doc[TRANSACTION_ID_FIELD]
    body = {k: v for k, v in doc.items() if k != "doctype"}
    if resource_version is not None:
        body[RESOURCE_VERSION_FIELD] = resource_version
    existing = find_existing(ledger, transaction_id)
    if existing is None:
        return _plan_adoption(ledger, body, transaction)
    return _plan_amendment(existing, body, transaction)


def _submitted(body: dict[str, Any]) -> dict[str, Any]:
    """A new row's body, submitted on insert. The only place this module sets a docstatus."""
    return {**body, "docstatus": 1}


def _plan_adoption(ledger: Ledger, body: dict[str, Any], transaction: Transaction) -> Plan:
    """Adopt the one hand-imported row carrying this transaction's trace number, or plan a fresh submitted row."""
    if not transaction.tracking_number:
        return Plan(Outcome.SUBMITTED, BANK_TRANSACTION, body=_submitted(body))
    matches = find_by_reference(ledger, body["bank_account"], transaction.tracking_number)
    if len(matches) > 1:
        names = ", ".join(str(row["name"]) for row in matches)
        raise PostingError(
            f"{len(matches)} Bank Transactions on {body['bank_account']} carry reference "
            f"{transaction.tracking_number} ({names}); Mercury transaction {transaction.id} matches one of "
            "them and guessing which would stamp its identity on the wrong row -- remove the duplicate "
            "import and the next run adopts the survivor"
        )
    if matches:
        return Plan(Outcome.ADOPTED, BANK_TRANSACTION, str(matches[0]["name"]), _mercury_fields(body))
    return Plan(Outcome.SUBMITTED, BANK_TRANSACTION, body=_submitted(body))


def _plan_amendment(existing: dict[str, Any], body: dict[str, Any], transaction: Transaction) -> Plan:
    """What to write to the Bank Transaction this transaction already has.

    Its content is compared rather than overwritten, because the row may be
    one a person imported: their spelling of the date and description is theirs
    to keep, and on a submitted document Frappe would refuse the write anyway.
    Content that agrees leaves only the Mercury fields to bring up to date;
    content that disagrees is rewritten on a draft and refused, by name, on a
    submitted document.
    """
    name = str(existing["name"])
    docstatus = int(existing.get("docstatus") or 0)
    disagreements = _content_disagreements(existing, body, transaction)
    if disagreements:
        if docstatus != 0:
            reason = f"{_SUBMITTED}: Mercury now says otherwise about {'; '.join(disagreements)}"
            return Plan(Outcome.REFUSED, BANK_TRANSACTION, name, reason=reason)
        return Plan(Outcome.UPDATED, BANK_TRANSACTION, name, body)
    if docstatus == 2:
        # Cancelled. Frappe accepts no write at all, `allow_on_submit` or not.
        return Plan(Outcome.REFUSED, BANK_TRANSACTION, name, reason=_SUBMITTED)
    changes = {k: v for k, v in _mercury_fields(body).items() if existing.get(k) != v}
    if not changes:
        return Plan(Outcome.UNCHANGED, BANK_TRANSACTION, name)
    return Plan(Outcome.UPDATED, BANK_TRANSACTION, name, changes)


def carry_out(ledger: Ledger, verdict: Plan) -> tuple[Outcome, str]:
    """Do what the plan says, and return the outcome with the document's name."""
    if verdict.outcome is Outcome.SUBMITTED:
        created = ledger.insert(verdict.doctype, verdict.body)
        return Outcome.SUBMITTED, str(created.get("name", "")) if isinstance(created, dict) else ""
    if verdict.outcome in {Outcome.UPDATED, Outcome.ADOPTED}:
        ledger.update(verdict.doctype, verdict.name, verdict.body)
    return verdict.outcome, verdict.name


def upsert(
    ledger: Ledger,
    doc: dict[str, Any],
    transaction: Transaction,
    *,
    resource_version: int | None = None,
) -> tuple[Outcome, str]:
    """Post the row submitted, amend the one this transaction already has, or adopt a hand-imported row. See `plan`."""
    return carry_out(ledger, plan(ledger, doc, transaction, resource_version=resource_version))


def custom_field_documents() -> list[dict[str, Any]]:
    """The Custom Fields the ingest needs: four on Bank Transaction, one on Journal Entry.

    `mercury_transaction_id` is unique on Bank Transaction, which is the point:
    idempotency stops being a property of our code and becomes one the server
    enforces, so even a bug in the applier cannot post the same transaction
    twice.

    On Journal Entry it is **not** unique. A person corrects a submitted
    voucher by cancelling and amending it, and ERPNext's Amend copies the id
    onto the amendment -- `no_copy` does not stop it -- while the cancelled
    original still holds it. Frappe's `unique` is a plain index that cannot
    exempt cancelled rows, so it refused that save and made every voucher of
    ours uncorrectable. `runningtab.mercury.vouchers.find_existing` refuses a
    second live voucher instead.

    The GL code and note are writable, not read-only like the identity fields:
    a statement export imported by hand through Bank Statement Import maps its
    `GL Code` and `Note` columns onto them, and the import mapper cannot target
    a read-only custom field.

    All four Bank Transaction fields are `allow_on_submit`: a hand-imported row
    is submitted before the ingest ever sees it, and adopting it means writing
    these onto a submitted document, which Frappe permits for no other field.

    Journal Entry carries the identity field alone, and **not**
    `allow_on_submit`: `runningtab.mercury.vouchers` drafts a voucher beside each
    coded Bank Transaction and never writes to a submitted one, so granting
    that would widen the boundary for nothing. It carries no
    `mercury_resource_version` either -- a GL code edit bumps no
    `resourceVersion` at Mercury, and stamping one on a voucher would claim the
    log said something it did not.
    """
    return [
        {
            "doctype": "Custom Field",
            "dt": JOURNAL_ENTRY,
            "fieldname": TRANSACTION_ID_FIELD,
            "label": "Mercury Transaction ID",
            "fieldtype": "Data",
            # Not unique, so a person can amend; see the docstring above.
            "unique": 0,
            "read_only": 1,
            "no_copy": 1,
            # Stated rather than left to the default so the repair pass has an
            # expected value: a field silently carrying it would let a write
            # reach a voucher a person had committed.
            "allow_on_submit": 0,
        },
        {
            "doctype": "Custom Field",
            "dt": BANK_TRANSACTION,
            "fieldname": TRANSACTION_ID_FIELD,
            "label": "Mercury Transaction ID",
            "fieldtype": "Data",
            "unique": 1,
            "read_only": 1,
            "no_copy": 1,
            "allow_on_submit": 1,
        },
        {
            "doctype": "Custom Field",
            "dt": BANK_TRANSACTION,
            "fieldname": RESOURCE_VERSION_FIELD,
            "label": "Mercury Resource Version",
            "fieldtype": "Int",
            # Stated rather than left to the default, so the repair pass has an
            # expected value to compare against. A version field wearing
            # `unique` would reject the second transaction to reach version 1,
            # which is every transaction after the first.
            "unique": 0,
            "read_only": 1,
            "no_copy": 1,
            "allow_on_submit": 1,
        },
        {
            "doctype": "Custom Field",
            "dt": BANK_TRANSACTION,
            "fieldname": GL_CODE_FIELD,
            "label": "Mercury GL Code",
            "fieldtype": "Data",
            "insert_after": RESOURCE_VERSION_FIELD,
            "unique": 0,
            "read_only": 0,
            "no_copy": 1,
            "allow_on_submit": 1,
        },
        {
            "doctype": "Custom Field",
            "dt": BANK_TRANSACTION,
            "fieldname": NOTE_FIELD,
            "label": "Mercury Note",
            "fieldtype": "Small Text",
            "insert_after": GL_CODE_FIELD,
            "unique": 0,
            "read_only": 0,
            "no_copy": 1,
            "allow_on_submit": 1,
        },
    ]


# Properties an existing field is repaired to match. `unique` is the one that
# matters -- a field that exists without it makes the whole idempotency story
# decorative -- `allow_on_submit` is what lets a hand-imported row be adopted
# at all, and the read-only flags are here so a repaired field is the same
# field a fresh install produces. A property the spec does not state is not
# compared.
_REPAIRED_PROPERTIES = ("unique", "read_only", "no_copy", "fieldtype", "allow_on_submit")


def install_custom_fields(admin: Ledger) -> list[tuple[str, Outcome]]:
    """Create the custom fields if they are absent, repair them if they are wrong.

    Needs an administrator: the books bot's role does not carry Custom Field
    permission, deliberately -- an ingest that can reshape doctypes is a larger
    thing than an ingest that can post into them.

    A field that already exists is checked rather than assumed correct. One
    created by hand without `unique` looks exactly like a correct install to a
    presence check, and the ingest would then tell people to run this command to
    fix a duplicate it can never fix.
    """
    results: list[tuple[str, Outcome]] = []
    for field in custom_field_documents():
        body = {k: v for k, v in field.items() if k != "doctype"}
        existing = admin.list(
            "Custom Field",
            fields=["name", *_REPAIRED_PROPERTIES],
            filters=[["dt", "=", field["dt"]], ["fieldname", "=", field["fieldname"]]],
            limit=1,
        )
        label = f"{field['dt']}.{field['fieldname']}"
        if not existing:
            admin.insert("Custom Field", body)
            results.append((label, Outcome.CREATED))
            continue
        row = existing[0]
        wrong = {k: body[k] for k in _REPAIRED_PROPERTIES if k in body and row.get(k) != body[k]}
        if not wrong:
            results.append((label, Outcome.UNCHANGED))
            continue
        admin.update("Custom Field", str(row["name"]), wrong)
        results.append((label, Outcome.UPDATED))
    return results


BANK = "Mercury"
BANK_ACCOUNT = "Bank Account"
_BANK_ACCOUNT_LINKS = ("account", "company", "is_company_account")


def bank_account_documents(context: PostingContext) -> list[dict[str, Any]]:
    """One ERPNext Bank Account per Mercury account named in `[bank_accounts]`.

    ERPNext names a Bank Account `"<account name> - <bank>"`, so the document
    name the mapping carries is split back into the `account_name` ERPNext
    wants and checked for the bank's suffix -- a name that would come out
    differently is refused here rather than discovered as a dangling link on
    the first Bank Transaction.

    Each one links the chart account holding that Mercury account's balance
    (`[accounts]`), so a Mercury account without a chart account cannot get a
    Bank Account either.
    """
    suffix = f" - {BANK}"
    documents: list[dict[str, Any]] = []
    seen: set[str] = set()
    for account_id, document_name in context.mapping.bank_accounts.items():
        if document_name in seen:
            continue
        seen.add(document_name)
        number = context.mapping.account_for_mercury_account(account_id)
        if number is None:
            raise PostingError(
                f"[bank_accounts] names Mercury account {account_id!r} which has no [accounts] entry; "
                "a Bank Account needs the chart account holding that balance"
            )
        if not document_name.endswith(suffix) or document_name == suffix:
            raise PostingError(
                f"[bank_accounts] {account_id!r} = {document_name!r}: ERPNext names a Bank Account "
                f"'<account name>{suffix}', so the entry must end with {suffix!r}"
            )
        documents.append({
            "account_name": document_name[: -len(suffix)],
            "bank": BANK,
            "account": context.link(number),
            "is_company_account": 1,
            "company": context.company,
        })
    return documents


def install_bank_accounts(admin: Ledger, context: PostingContext) -> list[tuple[str, Outcome]]:
    """Create the Bank and the Bank Accounts if absent; repair a link that is wrong.

    Needs an administrator: the books bot's role carries no Bank or Bank
    Account permission. These are master records that a Bank Transaction merely
    points at, and the ingest should not be able to redefine what our bank
    accounts are.

    An existing Bank Account is matched on its bank and account name rather
    than its document name, so the check does not depend on ERPNext's naming
    rule staying what it is; its chart account, company and company-account
    flag are compared and corrected, because a Bank Account linked to the wrong
    chart account files every transaction against the wrong balance.
    """
    results: list[tuple[str, Outcome]] = []
    if admin.list("Bank", fields=["name"], filters=[["bank_name", "=", BANK]], limit=1):
        results.append((f"Bank {BANK}", Outcome.UNCHANGED))
    else:
        admin.insert("Bank", {"bank_name": BANK})
        results.append((f"Bank {BANK}", Outcome.CREATED))
    for document in bank_account_documents(context):
        label = f"{BANK_ACCOUNT} {document['account_name']} - {BANK}"
        existing = admin.list(
            BANK_ACCOUNT,
            fields=["name", *_BANK_ACCOUNT_LINKS],
            filters=[["bank", "=", BANK], ["account_name", "=", document["account_name"]]],
            limit=1,
        )
        if not existing:
            admin.insert(BANK_ACCOUNT, document)
            results.append((label, Outcome.CREATED))
            continue
        row = existing[0]
        wrong = {k: document[k] for k in _BANK_ACCOUNT_LINKS if row.get(k) != document[k]}
        if not wrong:
            results.append((label, Outcome.UNCHANGED))
            continue
        admin.update(BANK_ACCOUNT, str(row["name"]), wrong)
        results.append((label, Outcome.UPDATED))
    return results
