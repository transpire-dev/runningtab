"""Mercury's receipts, on the Bank Transactions they belong to.

Three fakes, all of them addressed through real clients: the `icedcoffee-sync` client
over a mock transport for the ledger, the Mercury client over another for the
bank, and a third standing in for the presigned S3 endpoint the receipt URLs
point at. What these assert is the request actually sent -- the multipart part
names, the filters, the bytes -- rather than a stand-in's idea of one.
"""

from __future__ import annotations

import copy
import dataclasses
import pathlib
from typing import Any

import httpx2
import pytest
from icedcoffee_sync.frappe.client import Client

from runningtab.mercury.client import MercuryClient, MercuryEnv, config_for
from runningtab.mercury.ingest import run_snapshot
from runningtab.mercury.models import Attachment
from runningtab.mercury.posting import BANK_TRANSACTION, TRANSACTION_ID_FIELD
from runningtab.mercury.receipts import (
    ReceiptError,
    ReceiptFetcher,
    ReceiptTooLargeError,
    attach_receipts,
    attachment_id_of,
    file_name_for,
    receipts_on,
)
from runningtab.mercury.state import IngestState
from tests.conftest import all_transactions, by_id, payload
from tests.test_mercury_ingest import NOW, SITE, _Bank
from tests.test_mercury_posting import _Site

# The one fixture row carrying a receipt, and the attachment UUID in its URL.
RECEIPT_ROW = "txn_ach_utilities"
RECEIPT_ID = "7c2f5a10-3b44-4c5d-9e6f-1a2b3c4d5e6f"
RECEIPT_FILE = f"{RECEIPT_ID}-sonic-invoice-august.pdf"

# What the bucket serves. Not a structurally valid PDF on purpose: this fake
# stores whatever arrives, and whether a real Frappe accepts the bytes is a
# live question, answered by `test_live_access.py` against the test site.
RECEIPT_BYTES = b"%PDF-1.4\nsonic invoice\n%%EOF\n"


class _Bucket:
    """The presigned S3 endpoint a receipt URL points at.

    Answers every path with the same body unless `status` says to refuse. It
    counts its requests, because "a dry run downloads nothing" is a claim about
    requests and not about outcomes.
    """

    def __init__(self, body: bytes = RECEIPT_BYTES, *, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.requests: list[httpx2.Request] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.status >= 400:
            return httpx2.Response(self.status, text="Request has expired")
        return httpx2.Response(200, content=self.body)


@pytest.fixture
def bucket() -> _Bucket:
    return _Bucket()


@pytest.fixture
def fetcher(bucket: _Bucket):
    with ReceiptFetcher(transport=httpx2.MockTransport(bucket.handler)) as receipts:
        yield receipts


@pytest.fixture
def site() -> _Site:
    return _Site()


@pytest.fixture
def ledger(site: _Site):
    with Client(SITE, transport=httpx2.MockTransport(site.handler)) as client:
        yield client


@pytest.fixture
def state(tmp_path: pathlib.Path):
    with IngestState(tmp_path / "mercury.sqlite3") as ingest_state:
        yield ingest_state


def _mercury(bank: _Bank) -> MercuryClient:
    return MercuryClient(config_for(MercuryEnv.SANDBOX, "T"), transport=httpx2.MockTransport(bank.handler))


def _checking_with(attachments: list[dict[str, str]]) -> dict[str, Any]:
    """The checking fixture with the receipt row's attachment list replaced."""
    body = copy.deepcopy(payload("transactions_checking"))
    for row in body["transactions"]:
        if row["id"] == RECEIPT_ROW:
            row["attachments"] = attachments
    return body


def _attachment(identity: str, name: str) -> dict[str, str]:
    return {
        "fileName": name,
        "url": f"https://mercury-attachments.s3.amazonaws.com/attachment/{identity}.pdf?X-Amz-Signature=00",
    }


def files_on(site: _Site, row: str) -> dict[str, dict[str, Any]]:
    """The `File` documents attached to one Bank Transaction, by document name."""
    return {
        name: doc
        for (doctype, name), doc in site.documents.items()
        if doctype == "File" and doc.get("attached_to_name") == row
    }


def bank_row(site: _Site, transaction_id: str) -> str:
    """The name of the Bank Transaction posted for one Mercury transaction."""
    for (doctype, name), doc in site.documents.items():
        if doctype == BANK_TRANSACTION and doc.get(TRANSACTION_ID_FIELD) == transaction_id:
            return name
    raise AssertionError(f"no {BANK_TRANSACTION} for {transaction_id}")


def uploads(site: _Site) -> list[httpx2.Request]:
    return [r for r in site.requests if r.method == "POST" and r.url.path.endswith("/method/upload_file")]


def _post_row(ledger: Client, transaction_id: str, *, docstatus: int = 1) -> str:
    """A submitted Bank Transaction for one Mercury transaction, without a run."""
    created = ledger.insert(
        BANK_TRANSACTION,
        {
            "date": "2026-08-06",
            "bank_account": "Mercury Checking - T",
            "company": "Transpire",
            "deposit": 0.0,
            "withdrawal": 100.0,
            "description": "SONIC INTERNET",
            "reference_number": "091000015549612",
            TRANSACTION_ID_FIELD: transaction_id,
            "docstatus": docstatus,
        },
    )
    return str(created["name"])


def receipt_transaction(*attachments: Attachment):
    """The fixture row that carries a receipt, with its attachment list replaced."""
    return dataclasses.replace(by_id(all_transactions())[RECEIPT_ROW], attachments=attachments)


# --- identity -----------------------------------------------------------------


def test_the_attachment_id_is_the_uuid_in_the_url_path() -> None:
    # The presigned query string changes on every read and the file name is
    # whatever a person called it. The UUID in the path is the only stable name.
    transaction = by_id(all_transactions())[RECEIPT_ROW]
    assert [a.file_name for a in transaction.attachments] == ["sonic-invoice-august.pdf"]
    assert transaction.attachments[0].attachment_id == RECEIPT_ID


def test_a_url_with_no_uuid_in_its_path_has_no_id() -> None:
    plain = Attachment(file_name="scan.pdf", url="https://example.test/attachment/scan.pdf")
    assert plain.attachment_id is None


def test_the_presigned_url_stays_out_of_the_repr() -> None:
    # It carries a signature that is a credential for the file, and this record
    # lands in log lines and pytest assertion output.
    attachment = by_id(all_transactions())[RECEIPT_ROW].attachments[0]
    assert "X-Amz-Signature" not in repr(attachment)
    assert "sonic-invoice-august.pdf" in repr(attachment)


def test_the_file_name_carries_the_uuid_and_the_name_a_person_gave_it() -> None:
    attachment = by_id(all_transactions())[RECEIPT_ROW].attachments[0]
    name = file_name_for(attachment, RECEIPT_ID)
    assert name == RECEIPT_FILE
    assert attachment_id_of(name) == RECEIPT_ID


def test_a_long_name_is_trimmed_to_the_column_keeping_its_extension() -> None:
    attachment = Attachment(file_name=f"{'invoice-' * 40}.pdf", url="https://example.test/x.pdf")
    name = file_name_for(attachment, RECEIPT_ID)
    assert len(name) == 140
    assert name.startswith(f"{RECEIPT_ID}-")
    assert name.endswith(".pdf")


def test_a_name_frappe_would_rewrite_is_sanitised_before_it_is_sent() -> None:
    # Frappe's `File.set_file_name` strips `/`. Sending a name it would rewrite
    # means the next run compares against something we never wrote.
    attachment = Attachment(file_name="aug/2026/receipt.pdf", url="https://example.test/x.pdf")
    assert file_name_for(attachment, RECEIPT_ID) == f"{RECEIPT_ID}-aug2026receipt.pdf"


def test_a_file_nobody_here_wrote_has_no_attachment_id() -> None:
    assert attachment_id_of("scan-of-the-invoice.pdf") is None
    assert attachment_id_of("") is None


# --- fetching -----------------------------------------------------------------


def test_a_receipt_comes_back_as_its_bytes(fetcher: ReceiptFetcher, bucket: _Bucket) -> None:
    assert fetcher.fetch("https://mercury-attachments.s3.amazonaws.com/attachment/x.pdf") == RECEIPT_BYTES
    assert len(bucket.requests) == 1


def test_a_receipt_past_the_cap_is_refused_rather_than_buffered(bucket: _Bucket) -> None:
    bucket.body = b"x" * 64
    with (
        ReceiptFetcher(transport=httpx2.MockTransport(bucket.handler), size_limit=8) as fetcher,
        pytest.raises(ReceiptTooLargeError) as exc,
    ):
        fetcher.fetch("https://example.test/attachment/big.pdf")
    assert "8 byte limit" in str(exc.value)


def test_an_expired_url_is_a_receipt_error_and_not_a_traceback(bucket: _Bucket) -> None:
    bucket.status = 403
    with (
        ReceiptFetcher(transport=httpx2.MockTransport(bucket.handler)) as fetcher,
        pytest.raises(ReceiptError) as exc,
    ):
        fetcher.fetch("https://example.test/attachment/gone.pdf")
    assert "403" in str(exc.value)


# --- the pass -----------------------------------------------------------------


def test_the_receipt_lands_on_the_row_as_a_private_file(ledger: Client, site: _Site, fetcher: ReceiptFetcher) -> None:
    row = _post_row(ledger, RECEIPT_ROW)
    transaction = by_id(all_transactions())[RECEIPT_ROW]

    report = attach_receipts([transaction], ledger, fetcher=fetcher)

    assert report.attached == [f"{RECEIPT_FILE} -> {row}"]
    assert report.attention == []
    attached = files_on(site, row)
    assert [doc["file_name"] for doc in attached.values()] == [RECEIPT_FILE]
    assert all(doc["attached_to_doctype"] == BANK_TRANSACTION for doc in attached.values())
    assert all(doc["is_private"] == 1 for doc in attached.values()), "a receipt is financial evidence"
    assert list(site.files.values()) == [RECEIPT_BYTES]


def test_a_second_pass_attaches_nothing(ledger: Client, site: _Site, fetcher: ReceiptFetcher) -> None:
    _post_row(ledger, RECEIPT_ROW)
    transaction = by_id(all_transactions())[RECEIPT_ROW]
    attach_receipts([transaction], ledger, fetcher=fetcher)

    second = attach_receipts([transaction], ledger, fetcher=fetcher)

    assert second.attached == []
    assert second.attention == []
    assert len(uploads(site)) == 1


def test_an_attachment_added_later_is_the_only_one_uploaded(
    ledger: Client, site: _Site, fetcher: ReceiptFetcher
) -> None:
    later = "9a1b2c3d-4e5f-4061-8273-849506172839"
    first = by_id(all_transactions())[RECEIPT_ROW]
    _post_row(ledger, RECEIPT_ROW)
    attach_receipts([first], ledger, fetcher=fetcher)

    second = attach_receipts(
        [receipt_transaction(*first.attachments, Attachment("march.pdf", f"https://e.test/a/{later}.pdf"))],
        ledger,
        fetcher=fetcher,
    )

    assert second.attached == [f"{later}-march.pdf -> {bank_row(site, RECEIPT_ROW)}"]
    assert len(uploads(site)) == 2


def test_an_attachment_mercury_dropped_deletes_nothing(ledger: Client, site: _Site, fetcher: ReceiptFetcher) -> None:
    # A receipt a person can see on the bank's record is their record now.
    # Mercury changing its mind is not a reason to take it away from them, and
    # nothing reproduces it: the URL behind it expired twelve hours in.
    row = _post_row(ledger, RECEIPT_ROW)
    attach_receipts([by_id(all_transactions())[RECEIPT_ROW]], ledger, fetcher=fetcher)

    report = attach_receipts([receipt_transaction()], ledger, fetcher=fetcher)

    assert report.attached == []
    assert report.attention == []
    assert [doc["file_name"] for doc in files_on(site, row).values()] == [RECEIPT_FILE]
    assert [r.method for r in site.requests if r.method == "DELETE"] == []


def test_a_file_this_never_wrote_is_left_alone_and_not_reported(
    ledger: Client, site: _Site, fetcher: ReceiptFetcher
) -> None:
    # No UUID prefix means a person attached it, or another integration did.
    # It is not a problem, so a line about it every run would train the reader
    # to skip the report.
    row = _post_row(ledger, RECEIPT_ROW)
    site.documents[("File", "FI-manual")] = {
        "file_name": "scan-from-the-accountant.pdf",
        "attached_to_doctype": BANK_TRANSACTION,
        "attached_to_name": row,
        "is_private": 1,
    }

    report = attach_receipts([by_id(all_transactions())[RECEIPT_ROW]], ledger, fetcher=fetcher)

    assert report.attention == []
    assert report.attached == [f"{RECEIPT_FILE} -> {row}"]
    assert site.documents[("File", "FI-manual")]["file_name"] == "scan-from-the-accountant.pdf"


def test_an_attachment_with_no_id_is_reported_rather_than_uploaded(
    ledger: Client, site: _Site, fetcher: ReceiptFetcher
) -> None:
    # Uploading it would be worse than doing nothing: with no identity to
    # compare, every later run would upload it again.
    _post_row(ledger, RECEIPT_ROW)

    report = attach_receipts(
        [receipt_transaction(Attachment("scan.pdf", "https://e.test/attachment/scan.pdf"))],
        ledger,
        fetcher=fetcher,
    )

    assert report.attached == []
    assert uploads(site) == []
    assert len(report.attention) == 1
    transaction_id, reason = report.attention[0]
    assert transaction_id == RECEIPT_ROW
    assert "scan.pdf" in reason


def test_a_receipt_past_the_cap_is_one_report_line_naming_it(ledger: Client, site: _Site, bucket: _Bucket) -> None:
    bucket.body = b"x" * 64
    _post_row(ledger, RECEIPT_ROW)
    with ReceiptFetcher(transport=httpx2.MockTransport(bucket.handler), size_limit=8) as fetcher:
        report = attach_receipts([by_id(all_transactions())[RECEIPT_ROW]], ledger, fetcher=fetcher)

    assert report.attached == []
    assert uploads(site) == []
    assert len(report.attention) == 1
    assert RECEIPT_FILE in report.attention[0][1]


def test_a_ledger_refusal_is_one_line_and_the_run_goes_on(ledger: Client, site: _Site, fetcher: ReceiptFetcher) -> None:
    rows = by_id(all_transactions())
    _post_row(ledger, RECEIPT_ROW)
    other = "txn_deposit"
    _post_row(ledger, other)
    site.refuse_upload = (417, "ValidationError", "<strong>sonic.pdf</strong> is not a valid PDF")
    coded = rows[RECEIPT_ROW]
    second = dataclasses.replace(rows[other], attachments=coded.attachments)

    report = attach_receipts([coded, second], ledger, fetcher=fetcher)

    assert report.attached == []
    assert [transaction_id for transaction_id, _ in report.attention] == [RECEIPT_ROW, other]
    # Both rows were attempted, and the site's `<strong>` markup is stripped.
    assert len(uploads(site)) == 2
    assert "<strong>" not in report.attention[0][1]
    assert "is not a valid PDF" in report.attention[0][1]


def test_a_row_with_no_bank_transaction_is_skipped_in_silence(
    ledger: Client, site: _Site, fetcher: ReceiptFetcher
) -> None:
    # There is nothing to attach to, and a settled row that failed to post has
    # already produced its own line from the Bank Transaction path.
    report = attach_receipts([by_id(all_transactions())[RECEIPT_ROW]], ledger, fetcher=fetcher)

    assert (report.attached, report.planned, report.attention) == ([], [], [])
    assert uploads(site) == []


def test_a_cancelled_bank_transaction_is_skipped(ledger: Client, site: _Site, fetcher: ReceiptFetcher) -> None:
    # Somebody cancelled that row deliberately. Whatever replaces it is a new
    # row, and the receipt lands there.
    _post_row(ledger, RECEIPT_ROW, docstatus=2)

    report = attach_receipts([by_id(all_transactions())[RECEIPT_ROW]], ledger, fetcher=fetcher)

    assert (report.attached, report.attention) == ([], [])
    assert uploads(site) == []


def test_a_dry_run_downloads_nothing_and_plans_the_line(
    ledger: Client, site: _Site, fetcher: ReceiptFetcher, bucket: _Bucket
) -> None:
    row = _post_row(ledger, RECEIPT_ROW)

    report = attach_receipts([by_id(all_transactions())[RECEIPT_ROW]], ledger, fetcher=fetcher, dry_run=True)

    assert report.planned == [f"attach {RECEIPT_FILE} to {BANK_TRANSACTION} {row} for {RECEIPT_ROW}"]
    assert report.attached == []
    assert bucket.requests == []
    assert uploads(site) == []


def test_the_lookup_reads_only_the_files_on_that_row(ledger: Client, site: _Site, fetcher: ReceiptFetcher) -> None:
    row = _post_row(ledger, RECEIPT_ROW)
    other = _post_row(ledger, "txn_deposit")
    attach_receipts([by_id(all_transactions())[RECEIPT_ROW]], ledger, fetcher=fetcher)

    assert set(receipts_on(ledger, row)) == {RECEIPT_ID}
    assert receipts_on(ledger, other) == {}


# --- through a whole run ------------------------------------------------------


def test_a_snapshot_attaches_the_receipt_to_the_row_it_posted(
    site: _Site, ledger: Client, state: IngestState, posting_ctx, fetcher: ReceiptFetcher
) -> None:
    with _mercury(_Bank()) as mercury:
        report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW, receipts=fetcher)

    assert report.attached == [f"{RECEIPT_FILE} -> {bank_row(site, RECEIPT_ROW)}"]


def test_a_snapshot_without_a_fetcher_reaches_no_bucket(
    site: _Site, ledger: Client, state: IngestState, posting_ctx, bucket: _Bucket
) -> None:
    # Fetching is the one thing in this ingest that reaches a host which is
    # neither Mercury nor the ledger. A caller with no business making that
    # request says so by passing no fetcher.
    with _mercury(_Bank()) as mercury:
        report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)

    assert report.attached == []
    assert bucket.requests == []
    assert uploads(site) == []


def test_a_second_snapshot_attaches_nothing_new(
    ledger: Client, site: _Site, state: IngestState, posting_ctx, fetcher: ReceiptFetcher
) -> None:
    with _mercury(_Bank()) as mercury:
        run_snapshot(mercury, ledger, posting_ctx, state, now=NOW, receipts=fetcher)
        second = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW, receipts=fetcher)

    assert second.attached == []
    assert len(uploads(site)) == 1


def test_a_receipt_uploaded_at_the_bank_later_reaches_the_row_on_the_next_run(
    ledger: Client, site: _Site, state: IngestState, posting_ctx, fetcher: ReceiptFetcher
) -> None:
    # The attachment list emits no event, so this is the only way it arrives.
    later = "0f9e8d7c-6b5a-4231-9081-726354afbe19"
    with _mercury(_Bank()) as mercury:
        run_snapshot(mercury, ledger, posting_ctx, state, now=NOW, receipts=fetcher)

    grown = _Bank(
        overrides={
            "transactions_checking": _checking_with([
                _attachment(RECEIPT_ID, "sonic-invoice-august.pdf"),
                _attachment(later, "sonic-invoice-september.pdf"),
            ])
        }
    )
    with _mercury(grown) as mercury:
        report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW, receipts=fetcher)

    assert report.attached == [f"{later}-sonic-invoice-september.pdf -> {bank_row(site, RECEIPT_ROW)}"]
    assert len(uploads(site)) == 2


def test_a_dry_run_snapshot_plans_the_attachment_without_downloading_it(
    ledger: Client, site: _Site, state: IngestState, posting_ctx, fetcher: ReceiptFetcher, bucket: _Bucket
) -> None:
    # The rows already exist, as they do on the books site after the first real
    # run, so the plan names the document the receipt would land on.
    with _mercury(_Bank()) as mercury:
        run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
        report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW, dry_run=True, receipts=fetcher)

    row = bank_row(site, RECEIPT_ROW)
    assert f"attach {RECEIPT_FILE} to {BANK_TRANSACTION} {row} for {RECEIPT_ROW}" in report.planned
    assert bucket.requests == []
