"""Every attachment on the quarter's vouchers and bank lines, by voucher."""

import re
from typing import Any

import httpx2
import pytest
from icedcoffee_sync.frappe.client import FileDownloadError

from runningtab.period import PeriodError
from runningtab.period.documents import (
    BankTransaction,
    quarter_vouchers,
    read_bank_transactions,
    read_documents,
    refuse_missing_bank_transactions,
    refuse_unreadable_doctypes,
    refuse_unreadable_vouchers,
    safe_component,
)
from runningtab.period.quarter import Quarter
from tests.conftest import period_gl_rows, period_ledgers

Q3 = Quarter.parse("2026Q3")


def test_the_quarters_vouchers_are_listed_once_each():
    vouchers = quarter_vouchers(period_gl_rows(), Q3)
    assert len(vouchers) == 5
    assert vouchers == sorted(set(vouchers))


def test_the_fixtures_journal_entries_are_all_readable():
    from runningtab.access import READER_GRANTS

    readable = frozenset(g.doctype for g in READER_GRANTS)
    refuse_unreadable_doctypes(quarter_vouchers(period_gl_rows(), Q3), readable)  # no refusal


def test_a_voucher_type_the_reader_cannot_read_is_refused_naming_it():
    from runningtab.access import READER_GRANTS

    readable = frozenset(g.doctype for g in READER_GRANTS)
    vouchers = [*quarter_vouchers(period_gl_rows(), Q3), ("Expense Claim", "EXP-0001")]
    with pytest.raises(PeriodError, match=r"Expense Claim \(EXP-0001\)"):
        refuse_unreadable_doctypes(vouchers, readable)


def test_bank_transaction_is_required_even_with_no_bank_vouchers():
    with pytest.raises(PeriodError, match="Bank Transaction"):
        refuse_unreadable_doctypes([("Journal Entry", "JV-1")], frozenset({"Journal Entry"}))


@pytest.mark.parametrize(
    ("raw", "safe"), [("ACC-JV-2026/0001", "ACC-JV-2026_0001"), ("a\\b", "a_b"), ("ok.pdf", "ok.pdf")]
)
def test_path_components_are_made_safe(raw, safe):
    assert safe_component(raw) == safe


@pytest.mark.parametrize("raw", ["", ".", ".."])
def test_a_component_that_is_no_name_is_refused(raw):
    with pytest.raises(PeriodError):
        safe_component(raw)


class _Site:
    """A File listing and its bytes, in the shape the Frappe API returns them."""

    def __init__(
        self,
        files: list[dict[str, Any]],
        blobs: dict[str, bytes],
        broken: str | None = None,
        broken_urls: set[str] | None = None,
    ) -> None:
        self.files, self.blobs, self.broken, self.broken_urls = files, blobs, broken, broken_urls or set()

    def iter_all(self, doctype: str, **kwargs: Any) -> Any:
        assert doctype == "File"
        filters = {f[0]: f[2] for f in kwargs["filters"]}
        return iter(
            f
            for f in self.files
            if f["attached_to_doctype"] == filters["attached_to_doctype"]
            and f["attached_to_name"] in filters["attached_to_name"]
        )

    def download_file(self, file_url: str, *, max_bytes: int) -> bytes:
        if file_url == self.broken or file_url in self.broken_urls:
            raise FileDownloadError(f"{file_url}: HTTP 403")
        return self.blobs[file_url]


FILES = [
    {
        "name": "f1",
        "file_name": "receipt.pdf",
        "file_url": "/private/files/receipt.pdf",
        "attached_to_doctype": "Journal Entry",
        "attached_to_name": "JV-1",
        "is_folder": 0,
    },
    {
        "name": "f2",
        "file_name": "receipt.pdf",
        "file_url": "/private/files/receipt2.pdf",
        "attached_to_doctype": "Journal Entry",
        "attached_to_name": "JV-1",
        "is_folder": 0,
    },
    {
        "name": "f3",
        "file_name": "stmt.png",
        "file_url": "/private/files/stmt.png",
        "attached_to_doctype": "Bank Transaction",
        "attached_to_name": "BT-9",
        "is_folder": 0,
    },
]
BLOBS = {"/private/files/receipt.pdf": b"a", "/private/files/receipt2.pdf": b"b", "/private/files/stmt.png": b"c"}


def test_two_attachments_with_one_file_name_do_not_collide():
    docs = read_documents(_Site(FILES, BLOBS), [("Journal Entry", "JV-1")], ["BT-9"])
    assert docs == {
        "documents/Journal Entry/JV-1/f1-receipt.pdf": b"a",
        "documents/Journal Entry/JV-1/f2-receipt.pdf": b"b",
        "documents/Bank Transaction/BT-9/f3-stmt.png": b"c",
    }


def test_two_files_landing_on_the_same_path_are_refused_naming_both():
    # "JV/1" and "JV_1" both sanitize (via safe_component) to "JV_1", so two
    # different Files -- one attached to each -- collide on the same package
    # path even though neither voucher name is the other's.
    colliding = [
        {
            "name": "f1",
            "file_name": "r.pdf",
            "file_url": "/private/files/a.pdf",
            "attached_to_doctype": "Journal Entry",
            "attached_to_name": "JV/1",
            "is_folder": 0,
        },
        {
            "name": "f1",
            "file_name": "r.pdf",
            "file_url": "/private/files/b.pdf",
            "attached_to_doctype": "Journal Entry",
            "attached_to_name": "JV_1",
            "is_folder": 0,
        },
    ]
    blobs = {"/private/files/a.pdf": b"a", "/private/files/b.pdf": b"b"}
    with pytest.raises(PeriodError, match=r"documents/Journal Entry/JV_1/f1-r\.pdf"):
        read_documents(_Site(colliding, blobs), [("Journal Entry", "JV/1"), ("Journal Entry", "JV_1")], [])


def test_a_download_that_fails_refuses_the_package_naming_the_voucher():
    with pytest.raises(PeriodError, match=r"could not fetch all documents[\s\S]*JV-1[\s\S]*receipt2.pdf"):
        read_documents(_Site(FILES, BLOBS, broken="/private/files/receipt2.pdf"), [("Journal Entry", "JV-1")], [])


class _ConnectionDroppingSite:
    """One File, one voucher; `download_file` always raises httpx2.ConnectError.

    A timeout or a dropped connection surfaces from httpx2 as
    httpx2.HTTPError, not FileDownloadError (a bad status or an over-cap
    read) -- this proves it is collected the same way rather than escaping
    the per-file loop and crashing the export outright.
    """

    def __init__(self) -> None:
        self._site = _Site(
            [
                {
                    "name": "f9",
                    "file_name": "unreachable.pdf",
                    "file_url": "/private/files/unreachable.pdf",
                    "attached_to_doctype": "Journal Entry",
                    "attached_to_name": "JV-1",
                    "is_folder": 0,
                }
            ],
            {},
        )

    def iter_all(self, doctype: str, **kwargs: Any) -> Any:
        return self._site.iter_all(doctype, **kwargs)

    def download_file(self, file_url: str, *, max_bytes: int) -> bytes:
        raise httpx2.ConnectError("connection reset")


def test_a_connection_error_during_download_refuses_naming_the_file_and_voucher():
    with pytest.raises(PeriodError, match=r"could not fetch all documents[\s\S]*JV-1[\s\S]*unreachable.pdf"):
        read_documents(_ConnectionDroppingSite(), [("Journal Entry", "JV-1")], [])


class _RecordingSite:
    """Wraps `_Site`, recording every `iter_all` call's doctype and kwargs."""

    def __init__(self, files: list[dict[str, Any]], blobs: dict[str, bytes]) -> None:
        self._site = _Site(files, blobs)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def iter_all(self, doctype: str, **kwargs: Any) -> Any:
        self.calls.append((doctype, kwargs))
        return self._site.iter_all(doctype, **kwargs)

    def download_file(self, file_url: str, *, max_bytes: int) -> bytes:
        return self._site.download_file(file_url, max_bytes=max_bytes)


def test_the_file_listing_is_ordered_by_name():
    # iter_all pages by offset; a listing with no unique sort key can then
    # skip or repeat a row across a page boundary, silently dropping an
    # attachment. A stable sort key is the fix; a real cross-page read
    # against icedcoffee-sync's Client + MockTransport would only re-prove icedcoffee-sync's
    # own, separately-tested pagination, so this asserts the parameter
    # instead (see refine-3-report.md).
    recording = _RecordingSite(FILES, BLOBS)
    read_documents(recording, [("Journal Entry", "JV-1")], ["BT-9"])
    assert recording.calls
    assert all(kwargs.get("order_by") == "name asc" for _doctype, kwargs in recording.calls)


def test_multiple_download_failures_are_collected_and_named():
    broken_site = _Site(
        FILES,
        BLOBS,
        broken_urls={"/private/files/receipt.pdf", "/private/files/receipt2.pdf"},
    )

    with pytest.raises(PeriodError) as exc_info:
        read_documents(broken_site, [("Journal Entry", "JV-1")], [])

    error_msg = str(exc_info.value)
    assert "receipt.pdf" in error_msg
    assert "receipt2.pdf" in error_msg
    assert "JV-1" in error_msg


class _Recorder:
    """Records Bank Transaction queries for testing."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls = []

    def iter_all(self, doctype: str, **kwargs: Any) -> Any:
        self.calls.append((doctype, kwargs))
        if doctype == "Bank Transaction":
            return iter(self.rows)
        return iter([])

    def download_file(self, file_url: str, *, max_bytes: int) -> bytes:
        # Not used in Bank Transaction name tests, but required by protocol
        return b""


def test_read_bank_transactions_queries_with_correct_filters():
    Q3 = Quarter.parse("2026Q3")
    recorder = _Recorder([
        {"name": "BT-3", "mercury_transaction_id": "m-3"},
        {"name": "BT-1", "mercury_transaction_id": None},
        {"name": "BT-2", "mercury_transaction_id": "m-2"},
    ])
    transactions = read_bank_transactions(recorder, "Test Company", Q3)

    assert len(recorder.calls) == 1
    doctype, kwargs = recorder.calls[0]
    assert doctype == "Bank Transaction"
    assert kwargs["fields"] == ["name", "mercury_transaction_id"]
    assert ["company", "=", "Test Company"] in kwargs["filters"]
    # != 2 (cancelled), not = 1 (submitted): a draft Bank Transaction can
    # carry an attachment, and excluding it here means read_documents never
    # queries Files on it at all -- the attachment goes missing silently.
    assert ["docstatus", "!=", 2] in kwargs["filters"]
    assert ["date", ">=", Q3.start.isoformat()] in kwargs["filters"]
    assert ["date", "<=", Q3.end.isoformat()] in kwargs["filters"]
    assert kwargs["order_by"] == "name asc"
    assert transactions == [
        BankTransaction("BT-1", None),
        BankTransaction("BT-2", "m-2"),
        BankTransaction("BT-3", "m-3"),
    ]


def _settled_in_quarter_ids(quarter: Quarter) -> list[str]:
    return sorted({
        t.id
        for ledger in period_ledgers()
        for t in ledger.transactions
        if t.is_postable and quarter.start <= t.accounting_date <= quarter.end
    })


def test_a_settled_mercury_transaction_with_no_bank_transaction_is_refused_by_name():
    # The real ingest fixture ledgers, a Bank Transaction listing missing
    # one of the settled in-quarter ids -- the same silence
    # refuse_unreadable_vouchers exists to catch for vouchers, but here
    # nothing else would ever notice: the row is simply never queried.
    q3 = Quarter.parse("2026Q3")
    ids = _settled_in_quarter_ids(q3)
    present = [BankTransaction(f"BT-{i}", tid) for i, tid in enumerate(ids[1:])]
    with pytest.raises(PeriodError, match=re.escape(ids[0])):
        refuse_missing_bank_transactions(period_ledgers(), present, q3)


def test_every_settled_mercury_transaction_present_proceeds():
    q3 = Quarter.parse("2026Q3")
    ids = _settled_in_quarter_ids(q3)
    present = [BankTransaction(f"BT-{i}", tid) for i, tid in enumerate(ids)]
    refuse_missing_bank_transactions(period_ledgers(), present, q3)  # no refusal


class _VoucherListingFake:
    """`iter_all(doctype, filters=[["name", "in", chunk]])`, omitting `omit`'s names."""

    def __init__(self, omit: set[str] | None = None) -> None:
        self.omit = omit or set()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def iter_all(self, doctype: str, **kwargs: Any) -> Any:
        self.calls.append((doctype, kwargs))
        wanted = next(f[2] for f in kwargs["filters"] if f[0] == "name")
        return iter({"name": n} for n in wanted if n not in self.omit)

    def download_file(self, file_url: str, *, max_bytes: int) -> bytes:
        raise NotImplementedError("refuse_unreadable_vouchers never downloads a File")


def test_every_fixture_voucher_is_readable_back_through_the_listing():
    vouchers = quarter_vouchers(period_gl_rows(), Q3)
    fake = _VoucherListingFake()
    refuse_unreadable_vouchers(fake, vouchers)  # no refusal
    assert fake.calls
    assert all(kwargs.get("order_by") == "name asc" for _doctype, kwargs in fake.calls)


def test_a_voucher_the_listing_omits_is_refused_naming_it_and_its_doctype():
    # A doctype-level grant does not prove every document is readable:
    # Frappe honours per-document restrictions too, and the listing this
    # check runs is what would actually prove it -- an omitted name is
    # exactly what a per-document restriction looks like from the outside.
    vouchers = quarter_vouchers(period_gl_rows(), Q3)
    doctype, missing_voucher = vouchers[0]
    fake = _VoucherListingFake(omit={missing_voucher})
    with pytest.raises(PeriodError, match=re.escape(f"{doctype} ({missing_voucher})")):
        refuse_unreadable_vouchers(fake, vouchers)
