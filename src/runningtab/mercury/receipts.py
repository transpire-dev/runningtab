"""The receipt Mercury holds, attached to the Bank Transaction it belongs to.

A Mercury transaction carries the receipts somebody uploaded against it as
`attachments`, each a presigned S3 URL good for twelve hours. This module
fetches them and attaches them, private, to that transaction's **Bank
Transaction** -- the bank's record of the row, which exists for every settled
transaction, rather than the draft voucher, which exists only for the coded
ones and which a person may take over or delete. A reviewer reading a draft is
one hop away: its `remark` ends with the Mercury id the receipt hangs off.

Identity is the attachment UUID out of the S3 path, carried into the `File` as
`<uuid>-<fileName>`, so a run can compare what is already on the row against
what Mercury reports without downloading anything. A missing one is fetched and
uploaded; one already there is left alone.

**Nothing here deletes or replaces a `File`, ever** -- not one this wrote, and
not one it did not. A receipt a person can see on the bank's record is their
record, no re-run reproduces it (the URL behind it expired), and Mercury
dropping an attachment is not a reason to take a document away from somebody.
A `File` on the row with no UUID prefix is somebody else's and is left alone
without a report line, for the reason the voucher pass says nothing about a
draft a person has edited.

The fetch is a GET to S3, not to Mercury's API host, which is why it lives here
and not on `MercuryClient`: that class's promise is that the only verb it can
send is GET against one base URL, and an honest promise is better than one with
an asterisk on it.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable

import httpx2
from icedcoffee_sync.frappe.errors import FrappeError, PermissionError_, RateLimitError

from runningtab.mercury.models import Attachment, Transaction
from runningtab.mercury.posting import BANK_TRANSACTION, Ledger, PostingError, find_existing, plain_message

FILE = "File"

# What one receipt may weigh. A scanned invoice is a few hundred kilobytes and
# a phone photograph a few megabytes; twenty is generous for both and small
# enough that a run cannot be made to buffer something absurd. The stream is
# measured as it arrives and abandoned at the limit, so an enormous file costs
# this much traffic and not its own size.
RECEIPT_SIZE_LIMIT = 20 * 1024 * 1024

# Frappe stores `File.file_name` in a `Data` column, which is 140 characters.
# A longer name would be refused by the server, or silently cut somewhere we
# did not choose.
FILE_NAME_LIMIT = 140

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

# The `<uuid>-` prefix this module writes, read back off a `File` that is
# already on the row. Anchored and case-insensitive; a name that does not match
# was not written here.
_OURS = re.compile(rf"^(?P<id>{_UUID})-", re.IGNORECASE)

# What Frappe itself strips from a `file_name` on save (`File.set_file_name`
# removes `/`). The newline is ours: it would break the multipart header this
# name rides in long before Frappe ever saw it.
_UNSAFE = re.compile(r"[/\r\n]")

# The name given to an attachment whose own is empty once sanitised. It never
# stands alone -- the UUID prefix is what identifies the file -- so it only has
# to be a word rather than a blank.
_UNNAMED = "receipt"


class ReceiptError(RuntimeError):
    """A receipt could not be fetched. One report line; the run goes on."""


class ReceiptTooLargeError(ReceiptError):
    """The receipt is past `RECEIPT_SIZE_LIMIT` and was abandoned mid-stream."""


@dataclasses.dataclass
class ReceiptReport:
    """What one receipt pass did. Folded into the run's `IngestReport` by the caller.

    Returned rather than written into `IngestReport` directly so this module
    has no import back into `ingest`, and so its whole decision table is
    exercisable without a run.
    """

    # `<file name> -> <Bank Transaction name>`, one per upload.
    attached: list[str] = dataclasses.field(default_factory=list)
    planned: list[str] = dataclasses.field(default_factory=list)
    attention: list[tuple[str, str]] = dataclasses.field(default_factory=list)


def _trimmed(name: str, limit: int) -> str:
    """A file name cut to `limit`, keeping its extension.

    The extension is what tells a person -- and Frappe's own PDF scan -- what
    the file is, so it survives and the middle of the name goes.
    """
    if len(name) <= limit:
        return name
    stem, dot, extension = name.rpartition(".")
    if dot and len(extension) + 1 < limit:
        return f"{stem[: limit - len(extension) - 1]}.{extension}"
    return name[:limit]


def file_name_for(attachment: Attachment, identity: str) -> str:
    """The `File.file_name` one attachment becomes: `<uuid>-<sanitised fileName>`.

    Sanitised the way Frappe would sanitise it, so the name sent is the name
    that comes back and the comparison on the next run is against what we
    wrote. The UUID prefix is fixed-width hex and dashes, so the trim to the
    column's 140 characters can never reach it.
    """
    name = _UNSAFE.sub("", attachment.file_name).strip() or _UNNAMED
    return f"{identity}-{_trimmed(name, FILE_NAME_LIMIT - len(identity) - 1)}"


def attachment_id_of(file_name: str) -> str | None:
    """The attachment UUID a `File` we wrote carries, or None for one we did not."""
    match = _OURS.match(file_name or "")
    return match["id"].lower() if match else None


class ReceiptFetcher:
    """Downloads one presigned receipt URL, streamed, with a size cap.

    Its own client rather than the Mercury one: this reaches S3 with the
    signature in the query string, not Mercury's API host with a bearer token,
    and the two have no header, base URL or timeout in common.

    A context manager, so the connection pool closes with the run that opened
    it. Redirects are followed because S3 answers a cross-region read with one.
    """

    def __init__(
        self,
        *,
        transport: httpx2.BaseTransport | None = None,
        timeout: float = 60.0,
        size_limit: int = RECEIPT_SIZE_LIMIT,
    ) -> None:
        self.size_limit = size_limit
        self._http = httpx2.Client(timeout=timeout, transport=transport, follow_redirects=True)

    def __enter__(self) -> ReceiptFetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._http.close()

    def fetch(self, url: str) -> bytes:
        """The receipt's bytes, or `ReceiptError` describing why there are none.

        Everything that can go wrong with one download comes back as one
        exception type, because the caller does the same thing with all of
        them: name the row, name the file, and carry on with the rest of the
        run. Ending the run instead would cost every other receipt in it, and
        the URLs expire twelve hours after Mercury minted them.
        """
        try:
            with self._http.stream("GET", url) as response:
                if response.status_code >= 400:
                    response.read()
                    raise ReceiptError(f"the receipt URL answered {response.status_code}: {response.text[:200]}")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > self.size_limit:
                        raise ReceiptTooLargeError(
                            f"the receipt is larger than the {self.size_limit} byte limit; "
                            "a person downloads it from Mercury and attaches it"
                        )
                    chunks.append(chunk)
        except httpx2.RequestError as exc:
            raise ReceiptError(f"the receipt could not be fetched: {exc!r}") from exc
        return b"".join(chunks)


def receipts_on(ledger: Ledger, name: str) -> dict[str, str]:
    """The attachment UUIDs already on one Bank Transaction, mapped to their `File` names.

    Paged with `iter_all` rather than one capped `list`: a read cut short at a
    page limit looks exactly like "that receipt is not attached yet", which is
    the answer that uploads a second copy.

    A `File` whose name carries no UUID prefix is not in the result and is not
    reported. Somebody attached it, or another integration did; it is not this
    module's business either way.
    """
    have: dict[str, str] = {}
    for row in ledger.iter_all(
        FILE,
        fields=["name", "file_name"],
        filters=[["attached_to_doctype", "=", BANK_TRANSACTION], ["attached_to_name", "=", name]],
    ):
        identity = attachment_id_of(str(row.get("file_name") or ""))
        if identity is not None:
            have[identity] = str(row.get("name", ""))
    return have


def _attach(
    ledger: Ledger,
    transaction: Transaction,
    attachment: Attachment,
    row: str,
    have: dict[str, str],
    report: ReceiptReport,
    *,
    fetcher: ReceiptFetcher,
    dry_run: bool,
) -> None:
    """Put one attachment on one Bank Transaction, or say why it is not there.

    A refusal of this upload is a report line and the run goes on, mirroring
    `ingest._write`. A site that is rate limiting us and a credential it will
    not accept are each one problem the whole run has rather than one file's,
    so they propagate: a line per receipt would bury the single real problem.
    """
    identity = attachment.attachment_id
    if identity is None:
        report.attention.append((
            transaction.id,
            f"the receipt {attachment.file_name!r} has no id in its URL path, so no run can tell it from one "
            "already attached and every run would attach it again; a person downloads it from Mercury",
        ))
        return
    if identity in have:
        return
    name = file_name_for(attachment, identity)
    if dry_run:
        report.planned.append(f"attach {name} to {BANK_TRANSACTION} {row} for {transaction.id}")
        return
    try:
        content = fetcher.fetch(attachment.url)
    except ReceiptError as exc:
        report.attention.append((transaction.id, f"receipt {name}: {exc}"))
        return
    try:
        # `is_private` stated rather than inherited: a receipt is financial
        # evidence and has no business behind a world-readable URL.
        ledger.upload_file(BANK_TRANSACTION, row, name, content, is_private=True)
    except RateLimitError, PermissionError_:
        raise
    except FrappeError as exc:
        report.attention.append((
            transaction.id,
            f"attaching {name} to {BANK_TRANSACTION} {row} refused by the ledger: {plain_message(str(exc))}",
        ))
        return
    have[identity] = name
    report.attached.append(f"{name} -> {row}")


def attach_receipts(
    transactions: Iterable[Transaction],
    ledger: Ledger,
    *,
    fetcher: ReceiptFetcher,
    dry_run: bool = False,
) -> ReceiptReport:
    """Attach every Mercury receipt its Bank Transaction does not already carry.

    Sweep-shaped, and it has to be, for the reason the GL-code hint sweep and
    the voucher pass already are: **the attachment list emits no event.** That
    is an assumption rather than a measurement -- nobody has captured an event
    for an attachment upload -- but the two neighbouring fields we do know
    about, the GL code and the note, provably emit none, and if an event does
    turn out to fire the only cost is that this comparison finds nothing new.
    It adds no Mercury read: the run already fetched every transaction for the
    balance invariant.

    Two rows are skipped in silence. One with **no Bank Transaction** has
    nothing to attach to, and a settled row that failed to post has already
    produced its own report line from the Bank Transaction path. One whose Bank
    Transaction is **cancelled** was cancelled by a person deliberately;
    whatever replaces it is a new row, and the receipt lands there.
    """
    report = ReceiptReport()
    for transaction in transactions:
        if not transaction.attachments:
            continue
        try:
            existing = find_existing(ledger, transaction.id)
        except PostingError as exc:
            report.attention.append((transaction.id, str(exc)))
            continue
        if existing is None or int(existing.get("docstatus") or 0) == 2:
            continue
        row = str(existing["name"])
        have = receipts_on(ledger, row)
        for attachment in transaction.attachments:
            _attach(ledger, transaction, attachment, row, have, report, fetcher=fetcher, dry_run=dry_run)
    return report
