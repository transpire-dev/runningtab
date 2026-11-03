"""The receipts and invoices behind the quarter's vouchers and bank lines."""

from __future__ import annotations

import dataclasses
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any, Protocol

import httpx2
from icedcoffee_sync.frappe.client import FileDownloadError

from runningtab.health import AccountLedger
from runningtab.period import PeriodError
from runningtab.period.ledger import GLRow
from runningtab.period.quarter import Quarter

MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
_UNSAFE = re.compile(r"[/\\\x00-\x1f]")
_CHUNK = 100


class _Site(Protocol):
    def iter_all(self, doctype: str, **kwargs: Any) -> Any: ...
    def download_file(self, file_url: str, *, max_bytes: int) -> bytes: ...


def safe_component(text: str) -> str:
    cleaned = _UNSAFE.sub("_", text)
    if cleaned in ("", ".", ".."):
        raise PeriodError(f"{text!r} cannot name a file or directory")
    return cleaned


def quarter_vouchers(rows: Sequence[GLRow], quarter: Quarter) -> list[tuple[str, str]]:
    return sorted({(r.voucher_type, r.voucher_no) for r in rows if quarter.start <= r.posting_date <= quarter.end})


def refuse_unreadable_doctypes(vouchers: Sequence[tuple[str, str]], readable: Iterable[str]) -> None:
    """Refuse, before listing Files, a voucher type the reader's credential cannot read documents of.

    Frappe filters private Files attached to a document the credential
    cannot read out of the query results rather than refusing the query, so
    a voucher type the reader was never granted read on comes back with no
    Files at all -- indistinguishable from a voucher with no attachments.
    That is exactly the missed-attachment failure this catches before it
    ships a package silently short a document nobody is told about.
    "Bank Transaction" is required even though it never appears in
    `vouchers`: `read_documents` always queries Files on it too.
    """
    readable_set = frozenset(readable)
    by_doctype: dict[str, list[str]] = defaultdict(list)
    for doctype, name in vouchers:
        by_doctype[doctype].append(name)
    by_doctype.setdefault("Bank Transaction", [])
    missing = sorted(doctype for doctype in by_doctype if doctype not in readable_set)
    if not missing:
        return
    parts = [
        f"{doctype} ({', '.join(sorted(by_doctype[doctype]))})" if by_doctype[doctype] else doctype
        for doctype in missing
    ]
    raise PeriodError(
        "the reader cannot read Files on: "
        + "; ".join(parts)
        + "; Frappe silently omits attachments on a doctype it cannot read rather than refusing the query"
    )


def refuse_unreadable_vouchers(client: _Site, vouchers: Sequence[tuple[str, str]]) -> None:
    """Refuse, before listing Files, a voucher the reader's credential cannot see the document of.

    A doctype-level grant does not guarantee every document: Frappe also
    honours per-document restrictions (a User Permission, say), and -- the
    same silent omission `refuse_unreadable_doctypes` catches one level up --
    filters a private File attached to a document the credential cannot read
    out of the query results rather than refusing the query. `vouchers`'
    names come from the general ledger, read under a different grant (`GL
    Entry`), so nothing before this has proven the reader can see the
    voucher documents themselves. Listing them back through the reader, in
    chunks of at most `_CHUNK` names, is what proves it; not listed back
    means not readable.

    Bank Transactions need no matching check here: `read_bank_transactions`
    already lists them through the reader's own credential, so whatever it
    returns is by definition what the reader can see -- there is no
    separately-sourced candidate list to compare it against, the way GL
    Entry supplies one for vouchers. `refuse_missing_bank_transactions`
    below is that check, sourced from the Mercury ledgers instead.
    """
    by_doctype: dict[str, list[str]] = defaultdict(list)
    for doctype, name in vouchers:
        by_doctype[doctype].append(name)
    missing: list[str] = []
    for doctype, names in sorted(by_doctype.items()):
        seen: set[str] = set()
        for start in range(0, len(names), _CHUNK):
            rows = client.iter_all(
                doctype,
                fields=["name"],
                filters=[["name", "in", names[start : start + _CHUNK]]],
                order_by="name asc",
            )
            seen.update(str(r["name"]) for r in rows)
        absent = sorted(set(names) - seen)
        if absent:
            missing.append(f"{doctype} ({', '.join(absent)})")
    if missing:
        raise PeriodError(
            "the reader cannot read voucher document(s): "
            + "; ".join(missing)
            + "; Frappe silently omits attachments on a document it cannot read rather than refusing the query"
        )


@dataclasses.dataclass(frozen=True)
class BankTransaction:
    """One Bank Transaction, as much of it as `read_bank_transactions` needs."""

    name: str
    # The Mercury transaction this row was posted from -- `runningtab.mercury.posting`'s
    # TRANSACTION_ID_FIELD -- or None for a row Mercury never posted (a hand
    # import through Bank Statement Import, say).
    mercury_transaction_id: str | None


def read_bank_transactions(client: _Site, company: str, quarter: Quarter) -> list[BankTransaction]:
    """Every Bank Transaction dated in `quarter`, draft or submitted -- cancelled excluded.

    `docstatus != 2` rather than `= 1`: a draft row can carry an attachment
    (a receipt matched to it before it was reconciled and submitted), and
    excluding drafts here means `read_documents` never queries Files on that
    row at all -- the attachment is silently missing from the package rather
    than refused or reported. `order_by="name asc"`: `iter_all` pages by
    offset over a listing with no unique sort key otherwise, and a page
    boundary can then skip or repeat a row -- silently dropping an
    attachment the same way an unhandled short read would.

    `mercury_transaction_id` rides along on the same listing --
    `refuse_missing_bank_transactions` needs it and a second read of the
    same doctype would only duplicate this one.
    """
    rows = client.iter_all(
        "Bank Transaction",
        fields=["name", "mercury_transaction_id"],
        filters=[
            ["company", "=", company],
            ["docstatus", "!=", 2],
            ["date", ">=", quarter.start.isoformat()],
            ["date", "<=", quarter.end.isoformat()],
        ],
        order_by="name asc",
    )
    transactions = [
        BankTransaction(str(r["name"]), str(r["mercury_transaction_id"]) if r.get("mercury_transaction_id") else None)
        for r in rows
    ]
    return sorted(transactions, key=lambda b: b.name)


def refuse_missing_bank_transactions(
    ledgers: Sequence[AccountLedger], bank_transactions: Sequence[BankTransaction], quarter: Quarter
) -> None:
    """Refuse, naming each, a settled Mercury transaction in `quarter` with no matching Bank Transaction.

    `read_bank_transactions`'s listing is the only source `read_documents`
    has for which Bank Transactions to look for Files on; nothing before
    this proves that listing is complete. Two different failures produce
    the identical symptom -- a settled transaction with no Bank Transaction
    the reader can see, and its attachments (if any) silently absent from
    the package:

    - A per-document restriction hides a specific Bank Transaction from the
      reader, the same class of silence `refuse_unreadable_vouchers` exists
      to catch for vouchers -- but there `vouchers` is an independently
      sourced candidate list (the general ledger); here, without the
      Mercury ledgers as that independent source, a hidden row is simply
      never queried at all.
    - The Mercury ingest has not posted the quarter's transactions yet, so
      the Bank Transaction does not exist to be hidden or found.

    This refuses either way, without telling them apart: the fix for both
    is the same (post or re-check the ingest, or re-provision the reader),
    and a person reading a silently short package could never distinguish
    them from the outside regardless.
    """
    present = {bt.mercury_transaction_id for bt in bank_transactions if bt.mercury_transaction_id}
    missing = sorted(
        (t.id, ledger.label, t.accounting_date)
        for ledger in ledgers
        for t in ledger.transactions
        if t.is_postable and quarter.start <= t.accounting_date <= quarter.end and t.id not in present
    )
    if missing:
        named = "; ".join(f"{tid} ({label}, {date.isoformat()})" for tid, label, date in missing)
        raise PeriodError(
            "settled Mercury transaction(s) in the quarter have no matching Bank Transaction the reader can "
            f"see: {named}; the Mercury ingest may not have posted them yet, or the reader cannot read them"
        )


def read_documents(
    client: _Site, vouchers: Sequence[tuple[str, str]], bank_transactions: Sequence[str]
) -> dict[str, bytes]:
    """Every File attached to `vouchers` or `bank_transactions`, keyed by the path it lands at in the package.

    `order_by="name asc"` on the File listing: `iter_all` pages by offset
    over a listing with no unique sort key otherwise, and a page boundary
    can then skip or repeat a row -- silently dropping an attachment.
    """
    by_doctype: dict[str, list[str]] = defaultdict(list)
    for doctype, name in vouchers:
        by_doctype[doctype].append(name)
    by_doctype["Bank Transaction"].extend(bank_transactions)
    documents: dict[str, bytes] = {}
    sources: dict[str, str] = {}
    problems: list[str] = []
    for doctype, names in sorted(by_doctype.items()):
        for start in range(0, len(names), _CHUNK):
            files = client.iter_all(
                "File",
                fields=["name", "file_name", "file_url", "attached_to_doctype", "attached_to_name", "is_folder"],
                filters=[
                    ["attached_to_doctype", "=", doctype],
                    ["attached_to_name", "in", names[start : start + _CHUNK]],
                ],
                order_by="name asc",
            )
            for f in files:
                if int(f.get("is_folder") or 0):
                    continue
                path = "/".join([
                    "documents",
                    safe_component(doctype),
                    safe_component(str(f["attached_to_name"])),
                    safe_component(f"{f['name']}-{f['file_name']}"),
                ])
                source = f"File {f['name']} ({f['file_name']}) on {doctype} {f['attached_to_name']}"
                if path in sources:
                    # Two distinct Files landing on the same package path is a
                    # collision this function must never resolve by silently
                    # overwriting one with the other -- that would drop a
                    # document from the package without a trace.
                    raise PeriodError(
                        f"two Files would land on the same package path {path!r}: {sources[path]} and {source}"
                    )
                sources[path] = source
                try:
                    documents[path] = client.download_file(str(f["file_url"]), max_bytes=MAX_DOCUMENT_BYTES)
                # A timeout or a connection error surfaces from httpx2 as
                # httpx2.HTTPError, not FileDownloadError (that one covers a
                # bad status or an over-cap read) -- collected the same way,
                # naming the file and voucher, or it would escape this loop
                # and crash the whole export instead of refusing it cleanly.
                except (FileDownloadError, ValueError, httpx2.HTTPError) as exc:
                    problems.append(f"{doctype} {f['attached_to_name']}: could not fetch {f['file_name']}: {exc}")
    if problems:
        raise PeriodError("could not fetch all documents:\n  " + "\n  ".join(sorted(problems)))
    return dict(sorted(documents.items()))
