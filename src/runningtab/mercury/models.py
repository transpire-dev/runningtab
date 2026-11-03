"""Mercury payloads as typed records.

Modelled from observed payloads rather than from the published reference: the
reference describes `transactionCategoryData` while responses carry
`categoryData` and `mercuryCategory`. Where a field name here is an assumption
rather than something the investigation recorded, it says so in a comment and
the parser accepts every spelling it might have -- `tests/capture_mercury_fixtures.py`
settles which is real.

Money is inconsistent at the source: transactions carry floats in dollars,
card budgets carry integers in cents. Everything becomes `Decimal` here, at the
parse boundary, and via `str()` -- `Decimal(0.1)` is not `0.1`. No float reaches
the ledger.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import re
import urllib.parse
from decimal import Decimal
from typing import Any


class MercuryPayloadError(ValueError):
    """A payload is missing something the ingest cannot proceed without."""


class Status(enum.StrEnum):
    """Mercury's transaction status enum.

    Wider than the four the sandbox exhibited; `REVERSED` is the one that needs
    handling rather than a skip.
    """

    PENDING = "pending"
    SENT = "sent"
    CANCELLED = "cancelled"
    FAILED = "failed"
    REVERSED = "reversed"
    BLOCKED = "blocked"


# Statuses `currentBalance` leaves out. Measured exactly on two sandbox
# accounts: one off by 80.00 with a failed -70.00 and a pending -10.00, another
# off by 3.50 with a pending -3.50. `cancelled`, `reversed`, and `blocked` never
# occurred there, so which side of this line they fall on is unverified -- they
# are counted, which is what "the balance is the bank's number" implies, but a
# real capture is what settles it.
UNBALANCED_STATUSES: frozenset[Status] = frozenset({Status.PENDING, Status.FAILED})

# The only status that posts. Everything unsettled would put the ledger out of
# step with `currentBalance` by construction.
POSTABLE_STATUSES: frozenset[Status] = frozenset({Status.SENT})

# Every status string somebody has decided the accounting meaning of. A status
# outside this set is not merely unpostable: which side of the balance line it
# falls on and whether it should reach the ledger are both undecided, so it
# needs a person rather than a skip.
KNOWN_STATUSES: frozenset[str] = frozenset(status.value for status in Status)


class CategorySource(enum.StrEnum):
    """Which of Mercury's two category fields supplied the category.

    The precedence is a person's choice first, then Mercury's automatic
    assignment. A draft records which one it came from, so a reviewer can see at
    a glance which entries had no human judgement behind them.
    """

    CUSTOM = "custom"
    MERCURY = "mercury"


@dataclasses.dataclass(frozen=True)
class Allocation:
    """One `glAllocations` row: what Mercury's own rule engine derived.

    Never the classification key -- GL fields emit no event -- but worth reading
    as corroboration once the GL code list is our account names.
    """

    amount: Decimal
    gl_code_name: str | None


# The only stable name an attachment has. Mercury's presigned URL ends
# `attachment/<uuid>.<ext>`; the query string carries the signature and changes
# on every read, and the row carries no id field of its own. Matched on the
# UUID shape rather than on the `attachment/` segment before it, because the
# shape is what makes the name a name -- a path that reorganises around it
# still identifies the same file.
_ATTACHMENT_ID = re.compile(
    r"^(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:\.[^.]+)?$",
    re.IGNORECASE,
)


@dataclasses.dataclass(frozen=True)
class Attachment:
    """One receipt a person uploaded to Mercury for this transaction.

    The URL is presigned and expires twelve hours after Mercury minted it, so
    it is a thing to use inside the run that read it and never a thing to
    store. It is kept out of the repr for the reason the bank token is: this
    record turns up in log lines and pytest assertion output, and the signature
    in that query string is a credential for the file.
    """

    file_name: str
    url: str = dataclasses.field(repr=False)

    @property
    def attachment_id(self) -> str | None:
        """The UUID naming this attachment, or None when the URL path carries none.

        The identity a run compares against what is already attached in the
        ledger. None is not an error here -- it is a fact about the URL that
        the caller reports, because an attachment with no identity would be
        uploaded again by every later run.
        """
        last = urllib.parse.urlsplit(self.url).path.rsplit("/", 1)[-1]
        match = _ATTACHMENT_ID.match(urllib.parse.unquote(last))
        return match["id"].lower() if match else None


@dataclasses.dataclass(frozen=True)
class Category:
    """One row of `GET /categories`: a UUID that survives renames, a name, three visibility flags.

    The flags decide which pickers Mercury offers the category in -- a card
    charge, an expense reimbursement, and everything else (wires, ACH,
    transfers). All three are required by the API's own schema and all three
    are required here: a flag guessed rather than read would make
    `runningtab.mercury.categories` propose an edit that changes nothing, or miss
    one that matters.
    """

    id: str
    name: str
    visible_for_card_spend: bool
    visible_for_reimbursements: bool
    visible_for_other: bool


@dataclasses.dataclass(frozen=True)
class Account:
    """A deposit account from `GET /accounts`.

    Credit accounts are not here: `/accounts` omits them entirely and `GET
    /credit` returns them with a different, much smaller schema. Two account
    models, not one with optional fields -- `CreditAccount` is the other.
    """

    id: str
    name: str
    current_balance: Decimal
    kind: str | None = None
    # What a full transaction read starts from. The endpoint's own default is
    # the last 30 days, so "every transaction on this account" is only sayable
    # with a date, and this is the only one the account itself supplies.
    created_at: dt.datetime | None = None

    @property
    def label(self) -> str:
        """What a person reading a report calls this account."""
        return self.name or self.id


@dataclasses.dataclass(frozen=True)
class CreditAccount:
    """A credit account from `GET /credit`. Has no name; the IO card is one of these."""

    id: str
    status: str | None
    current_balance: Decimal
    available_balance: Decimal | None = None
    created_at: dt.datetime | None = None

    @property
    def label(self) -> str:
        """What a person reading a report calls this account. It has no name to use."""
        return f"credit account {self.id[:8]}"


@dataclasses.dataclass(frozen=True)
class Transaction:
    """One transaction, with the account it was read from attached.

    `account_id` is not part of the payload. It comes from the request that
    fetched the row, because pairing the two halves of an internal transfer
    needs to know which side is which and `counterpartyId` alone cannot say.
    """

    id: str
    account_id: str
    amount: Decimal
    status: Status
    raw_status: str
    kind: str
    created_at: dt.datetime
    posted_at: dt.datetime | None = None
    counterparty_id: str | None = None
    counterparty_name: str | None = None
    bank_description: str | None = None
    external_memo: str | None = None
    note: str | None = None
    dashboard_link: str | None = None
    custom_category: str | None = None
    # The custom category's UUID, which is what `GET /categories` keys on. The
    # name would be the obvious handle and is the wrong one: two categories can
    # wear one name, and a rename moves the name without moving the row. Only
    # this answers "does any transaction carry that category", which is the
    # question `runningtab mercury categories apply --prune` must get right before
    # it deletes anything.
    custom_category_id: str | None = None
    mercury_category: str | None = None
    gl_code_name: str | None = None
    allocations: tuple[Allocation, ...] = ()
    # The receipts a person uploaded against this transaction. Read on every
    # run rather than in response to an event: the attachment list emits none,
    # for the same reason the GL code and the note do not.
    attachments: tuple[Attachment, ...] = ()
    # The ACH trace number, which Mercury's statement export carries as
    # `Tracking ID`. Absent on anything that never went through the ACH
    # network -- a card charge has none. Observed as a string on a real
    # production row; never seen on a sandbox one.
    tracking_number: str | None = None

    @property
    def category(self) -> str | None:
        """A person's choice, else Mercury's automatic one. Parsed as a fact about the row; nothing routes on it."""
        return self.custom_category or self.mercury_category

    @property
    def gl_code(self) -> str | None:
        """The GL code a person chose for this row: Mercury's GL code, else their custom category.

        The custom categories are the chart's GL-code labels word for word
        (`runningtab mercury categories apply` mirrors them), and Mercury's dropdown
        offers nothing else, so picking a category is coding the row. Coding at
        the card reader sets the category and leaves `generalLedgerCodeName`
        empty. Mercury's automatic category is not a GL code: nobody chose it,
        and it names Mercury's taxonomy rather than the chart.
        """
        return self.gl_code_name or self.custom_category

    @property
    def category_source(self) -> CategorySource | None:
        """Which field the category came from, or None when the transaction carries neither."""
        if self.custom_category:
            return CategorySource.CUSTOM
        if self.mercury_category:
            return CategorySource.MERCURY
        return None

    @property
    def status_recognised(self) -> bool:
        """Whether the status is one this ingest has accounting semantics for.

        Asked of `raw_status` rather than of `status`: an unrecognised status
        parses to `BLOCKED`, which makes it indistinguishable from a real one
        at the enum and would let it be handled as though somebody had decided
        what it means.
        """
        return self.raw_status in KNOWN_STATUSES

    @property
    def counts_toward_balance(self) -> bool:
        """Whether `currentBalance` includes this row."""
        return self.status not in UNBALANCED_STATUSES

    @property
    def is_postable(self) -> bool:
        """Whether this row is settled enough to reach the ledger at all."""
        return self.status in POSTABLE_STATUSES

    @property
    def accounting_date(self) -> dt.date:
        """The date a posting carries.

        `postedAt` when present, else `createdAt`. The design left the choice
        open -- the two differ by a month on some sandbox rows -- and this picks
        `postedAt` because the ledger is describing settled activity, which is
        what `currentBalance` counts.
        """
        return (self.posted_at or self.created_at).date()

    @property
    def allocated_total(self) -> Decimal:
        """The sum of the GL allocations. Compared against `amount`, never assumed equal to it."""
        return sum((a.amount for a in self.allocations), Decimal(0))


class Operation(enum.StrEnum):
    """What an event says happened to its resource."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


@dataclasses.dataclass(frozen=True)
class Event:
    """One row of the event log, which is also exactly what a webhook delivers.

    The applier reads only the envelope -- which resource changed, at which
    version -- and re-reads the resource itself from the transactions endpoint.
    That is deliberate: the event body is a JSON Merge Patch whose exact
    envelope key the investigation never recorded, and re-reading is both
    cheaper to be sure of and correct for a pending charge that settled.
    """

    id: str
    resource_type: str
    resource_id: str
    operation: Operation
    resource_version: int | None = None
    changed_paths: tuple[str, ...] = ()

    @property
    def is_transaction(self) -> bool:
        """Whether this event is about a transaction rather than an account balance."""
        return self.resource_type.lower() == "transaction"


def _require(payload: dict[str, Any], *names: str) -> Any:
    """The first present, non-null value among `names`, or a loud failure.

    Several field names in this file are recorded observations and several are
    assumptions. Rather than guess silently and produce a wrong record, an
    absent required field raises and names both what was wanted and what the
    payload actually carried.
    """
    for name in names:
        value = payload.get(name)
        if value is not None:
            return value
    wanted = " / ".join(names)
    raise MercuryPayloadError(f"payload has no {wanted}; keys present: {sorted(payload)}")


def _optional(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = payload.get(name)
        if value is not None:
            return value
    return None


def money(value: Any) -> Decimal:
    """A Mercury amount as an exact `Decimal`.

    Goes through `str()` because the wire value is a JSON float: `Decimal(0.1)`
    carries the binary approximation and would drift a cent at a time through a
    ledger.
    """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def timestamp(value: Any) -> dt.datetime:
    """An ISO 8601 instant as an aware UTC datetime.

    Mercury sends `Z`; `datetime.fromisoformat` accepts it from 3.11 on. A naive
    value is read as UTC rather than as local time, because a bank timestamp is
    never local.
    """
    parsed = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _optional_timestamp(payload: dict[str, Any], *names: str) -> dt.datetime | None:
    value = _optional(payload, *names)
    return None if value is None else timestamp(value)


def parse_status(value: Any) -> tuple[Status, str]:
    """A status string as `(Status, raw)`.

    An unrecognised status is not an error -- Mercury may add one -- but it is
    never postable: it falls back to `BLOCKED`, whose handling is "post
    nothing", and the raw string is kept so a report can name it.
    """
    raw = str(value)
    try:
        return Status(raw), raw
    except ValueError:
        return Status.BLOCKED, raw


def parse_allocations(payload: dict[str, Any]) -> tuple[Allocation, ...]:
    rows = payload.get("glAllocations") or ()
    return tuple(Allocation(amount=money(row.get("amount", 0)), gl_code_name=row.get("glCodeName")) for row in rows)


def parse_attachments(payload: dict[str, Any]) -> tuple[Attachment, ...]:
    """The `attachments` rows as records.

    `url` goes through `_require`: an attachment row without one names a
    receipt nothing can fetch, and reading past it would drop a person's
    document without saying so. Raising here surfaces as a
    `MalformedResponseError` naming the endpoint, which is the loud failure
    this would otherwise be the silent version of.

    `fileName` falls back to the last path segment, which is the file's own
    `<uuid>.<ext>` -- a poor name for a person to read and a real one.
    """
    rows = payload.get("attachments") or ()
    attachments = []
    for row in rows:
        url = str(_require(row, "url"))
        name = _optional(row, "fileName") or urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
        attachments.append(Attachment(file_name=str(name), url=url))
    return tuple(attachments)


def _parse_categories(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """`(customCategory name, customCategory id, mercuryCategory name)`.

    Two shapes are accepted for the automatic category because the record is
    ambiguous: the sandbox survey counted `mercuryCategory` as a top-level field
    while the Events field set names it `categoryData.mercuryCategory`. Both are
    read; a live capture settles which one a real account sends.

    The custom category is **flat**: `categoryData` is the category object
    itself, carrying `id`, `name` and the three visibility flags -- measured
    against the real organisation on 2026-09-21, and the reason the nested
    `categoryData.customCategory` the ingest fixtures were built around is read
    as a fallback rather than as the shape. Reading the id wrongly would be
    worse than reading it not at all: `--prune` deletes a category no
    transaction carries, so an id that never parses makes every category look
    uncarried. `runningtab.mercury.categories` guards that separately, by refusing to
    prune at all when transactions name a category and none yields an id.
    """
    category_data = payload.get("categoryData") or {}
    nested = category_data.get("customCategory") or {}
    custom = nested.get("name") or category_data.get("name")
    custom_id = nested.get("id") or category_data.get("id")
    mercury = category_data.get("mercuryCategory") or payload.get("mercuryCategory")
    if isinstance(mercury, dict):
        mercury = mercury.get("name")
    return custom, None if custom_id is None else str(custom_id), mercury


def parse_transaction(payload: dict[str, Any], account_id: str) -> Transaction:
    """One transaction payload, with the account it was read from."""
    status, raw_status = parse_status(_require(payload, "status"))
    custom_category, custom_category_id, mercury_category = _parse_categories(payload)
    return Transaction(
        id=str(_require(payload, "id")),
        account_id=account_id,
        amount=money(_require(payload, "amount")),
        status=status,
        raw_status=raw_status,
        kind=str(_optional(payload, "kind") or "unknown"),
        created_at=timestamp(_require(payload, "createdAt")),
        posted_at=_optional_timestamp(payload, "postedAt"),
        counterparty_id=_optional(payload, "counterpartyId"),
        counterparty_name=_optional(payload, "counterpartyName"),
        bank_description=_optional(payload, "bankDescription"),
        external_memo=_optional(payload, "externalMemo"),
        note=_optional(payload, "note"),
        dashboard_link=_optional(payload, "dashboardLink"),
        custom_category=custom_category,
        custom_category_id=custom_category_id,
        mercury_category=mercury_category,
        gl_code_name=_optional(payload, "generalLedgerCodeName"),
        allocations=parse_allocations(payload),
        attachments=parse_attachments(payload),
        tracking_number=_optional_string(payload, "trackingNumber"),
    )


def _optional_string(payload: dict[str, Any], *names: str) -> str | None:
    """An optional field as a string, or None. Kept as text: a trace number with a leading zero is not a number."""
    value = _optional(payload, *names)
    return None if value is None else str(value)


def parse_account(payload: dict[str, Any]) -> Account:
    return Account(
        id=str(_require(payload, "id")),
        name=str(_optional(payload, "name", "nickname") or ""),
        current_balance=money(_require(payload, "currentBalance")),
        kind=_optional(payload, "kind", "type"),
        created_at=_optional_timestamp(payload, "createdAt"),
    )


def parse_credit_account(payload: dict[str, Any]) -> CreditAccount:
    available = _optional(payload, "availableBalance")
    return CreditAccount(
        id=str(_require(payload, "id")),
        status=_optional(payload, "status"),
        current_balance=money(_require(payload, "currentBalance")),
        available_balance=None if available is None else money(available),
        created_at=_optional_timestamp(payload, "createdAt"),
    )


def parse_category(payload: dict[str, Any]) -> Category:
    """One category payload. Every visibility flag is required, never defaulted.

    `_require` rejects a missing or null flag but accepts `False`, which is a
    real answer. Defaulting an absent flag would silently decide something the
    bank was never asked.
    """
    return Category(
        id=str(_require(payload, "id")),
        name=str(_require(payload, "name")),
        visible_for_card_spend=bool(_require(payload, "visibleForCardSpend")),
        visible_for_reimbursements=bool(_require(payload, "visibleForReimbursements")),
        visible_for_other=bool(_require(payload, "visibleForOther")),
    )


def parse_event(payload: dict[str, Any]) -> Event:
    """One event envelope.

    `resourceVersion` is optional here, not because Mercury omits it -- it is
    documented as an int64 starting at 1 -- but because an event that arrives
    without one must still be applied exactly once rather than dropped. Dedup on
    the event id is what makes that safe.
    """
    version = _optional(payload, "resourceVersion")
    operation = str(_require(payload, "operationType")).lower()
    try:
        parsed_operation = Operation(operation)
    except ValueError as exc:
        raise MercuryPayloadError(f"unknown operationType {operation!r} on event {payload.get('id')!r}") from exc
    return Event(
        id=str(_require(payload, "id")),
        resource_type=str(_require(payload, "resourceType")),
        resource_id=str(_require(payload, "resourceId")),
        operation=parsed_operation,
        resource_version=None if version is None else int(version),
        changed_paths=tuple(payload.get("changedPaths") or ()),
    )
