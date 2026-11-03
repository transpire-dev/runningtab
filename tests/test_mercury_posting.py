"""Building Bank Transactions, and putting exactly one of them per Mercury transaction.

The ledger side goes through the real `icedcoffee-sync` client over a mock transport, so
what these tests assert is the request actually sent -- the URL, the filter, the
body -- rather than a stand-in's idea of one.
"""

from __future__ import annotations

import dataclasses
import json
import re
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from icedcoffee_sync.frappe.client import Client, SiteConfig
from icedcoffee_sync.frappe.lifecycle import SubmitNotPermittedError

from runningtab.mercury.classify import classify
from runningtab.mercury.mapping import Mapping, parse_mapping
from runningtab.mercury.posting import (
    BANK,
    GL_CODE_FIELD,
    NOTE_FIELD,
    RESOURCE_VERSION_FIELD,
    TRANSACTION_ID_FIELD,
    Outcome,
    PostingContext,
    PostingError,
    bank_account_documents,
    bank_transaction,
    custom_field_documents,
    install_bank_accounts,
    install_custom_fields,
    plan,
    upsert,
)
from tests.conftest import (
    ACCOUNT_DOCUMENT_NAMES,
    EXAMPLE_BOOKS,
    all_transactions,
    by_id,
    example_chart,
    transactions_from,
)

# The ingest's client, with the one capability the ingest needs: submitting the
# Bank Transactions it creates.
SITE = SiteConfig(url="https://example.test", api_key="KEY", api_secret="SECRET", may_submit=True)  # noqa: S106
DRAFTS_ONLY = SiteConfig(url="https://example.test", api_key="KEY", api_secret="SECRET")  # noqa: S106


_DISPOSITION_NAME = re.compile(r'name="(?P<name>[^"]+)"')
_DISPOSITION_FILENAME = re.compile(r'filename="(?P<filename>[^"]*)"')


def _multipart(request: httpx2.Request) -> tuple[dict[str, str], str, bytes]:
    """The form fields, the uploaded file's name, and its bytes.

    Parsed out of the body the real client encoded rather than out of anything
    this file constructed, so what it proves is that `upload_file` sends what
    Frappe's handler reads: the file in a part literally named `file`, and
    `doctype`/`docname`/`is_private` as plain form fields.
    """
    boundary = request.headers["content-type"].partition("boundary=")[2].encode()
    fields: dict[str, str] = {}
    filename = ""
    content = b""
    for part in request.content.split(b"--" + boundary):
        head, separator, body = part.partition(b"\r\n\r\n")
        if not separator:
            continue
        disposition = head.decode("utf-8", "replace")
        named = _DISPOSITION_NAME.search(disposition)
        if named is None:
            continue
        # Exactly the CRLF that separates a part from the boundary after it,
        # never more: `rstrip` here would eat a file's own trailing newline and
        # the fake would hand back bytes nobody uploaded.
        body = body[:-2] if body.endswith(b"\r\n") else body
        as_file = _DISPOSITION_FILENAME.search(disposition)
        if as_file is not None:
            filename, content = as_file["filename"], body
        else:
            fields[named["name"]] = body.decode()
    return fields, filename, content


class _Site:
    """A tiny stand-in ERPNext, addressed through the real client.

    It stores documents by name, answers list filters by equality, and returns
    exactly the `fields` a request asked for -- which matters, because code
    that reads a field it forgot to select sees `None` and concludes the
    document is wrong. Nothing here is asserted about; the assertions are on
    what we sent it.
    """

    def __init__(self, user: str = EXAMPLE_BOOKS.bot.user) -> None:
        self.documents: dict[tuple[str, str], dict[str, Any]] = {}
        self.requests: list[httpx2.Request] = []
        self.counter = 0
        # Who the credential is. Frappe stamps `modified_by` with the session
        # user on every write, and the voucher path reads it to tell a draft
        # of its own from one a person has taken over.
        self.user = user
        # Uploaded attachment bytes, by `File` document name. Kept apart from
        # the documents because Frappe keeps them apart too: the bytes go to
        # disk and the `File` row points at them.
        self.files: dict[str, bytes] = {}
        # What the site says when asked to store a file, if anything. Set to a
        # (status, error type, message) triple to make the next upload fail.
        self.refuse_upload: tuple[int, str, str] | None = None

    def _upload(self, request: httpx2.Request) -> httpx2.Response:
        """Frappe's `upload_file`: a `File` document, and the bytes behind it."""
        if self.refuse_upload is not None:
            status, error_type, message = self.refuse_upload
            return httpx2.Response(status, json={"errors": [{"type": error_type, "message": message}]})
        fields, filename, content = _multipart(request)
        self.counter += 1
        name = f"FI-{self.counter:04}"
        doc = {
            # Frappe's `File.set_file_name` strips `/` and stores the rest
            # verbatim, so this fake does the same and a name that would come
            # back different comes back different here too.
            "file_name": filename.replace("/", ""),
            "attached_to_doctype": fields.get("doctype"),
            "attached_to_name": fields.get("docname"),
            "is_private": int(fields.get("is_private", 1)),
            "docstatus": 0,
            "modified_by": self.user,
        }
        self.documents[("File", name)] = doc
        self.files[name] = content
        return httpx2.Response(200, json={"data": {"name": name, **doc}})

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.method == "POST" and request.url.path.endswith("/method/upload_file"):
            return self._upload(request)
        parts = [segment.replace("%20", " ") for segment in request.url.path.split("/")]
        doctype = parts[-1]
        if request.method == "GET" and parts[-2] != "document":
            # `/api/v2/document/<Doctype>/<name>` -- one document, whole,
            # child tables and all. A field list cannot return those, and the
            # voucher path compares them.
            doc = self.documents.get((parts[-2], parts[-1]))
            if doc is None:
                return httpx2.Response(404, json={"errors": [{"message": "not found"}]})
            return httpx2.Response(200, json={"data": {"name": parts[-1], **doc}})
        if request.method == "GET":
            filters = json.loads(request.url.params.get("filters", "[]"))
            fields = json.loads(request.url.params.get("fields", '["name"]'))
            wanted = {f[0]: f[2] for f in filters}
            rows = []
            for (dt_, name), doc in self.documents.items():
                if dt_ != doctype or not all(doc.get(k) == v for k, v in wanted.items()):
                    continue
                row = {"name": name, "docstatus": doc.get("docstatus", 0)}
                row.update({field: doc.get(field) for field in fields if field not in row})
                rows.append(row)
            # `limit` and `start` are honoured so a paging reader is actually
            # paged here. Ignoring them would hand `iter_all` every row on its
            # first request, and a caller that had forgotten to page would pass
            # against this fake and truncate against a real site.
            start = int(request.url.params.get("start", 0))
            limit = request.url.params.get("limit")
            page = rows[start:] if limit is None else rows[start : start + int(limit)]
            return httpx2.Response(200, json={"data": page})
        if request.method == "POST":
            body = json.loads(request.content)
            self.counter += 1
            name = f"{doctype[:2].upper()}-{self.counter:04}"
            self.documents[(doctype, name)] = {
                **body,
                "docstatus": body.get("docstatus", 0),
                "modified_by": self.user,
            }
            return httpx2.Response(200, json={"data": {"name": name, **body}})
        if request.method == "PUT":
            name = parts[-1]
            doctype = parts[-2]
            body = json.loads(request.content)
            self.documents[(doctype, name)].update({**body, "modified_by": self.user})
            return httpx2.Response(200, json={"data": {"name": name}})
        return httpx2.Response(405, json={"errors": [{"message": "no"}]})


@pytest.fixture
def site() -> _Site:
    return _Site()


@pytest.fixture
def ledger(site: _Site):
    with Client(SITE, transport=httpx2.MockTransport(site.handler)) as client:
        yield client


def documents_of(site: _Site, doctype: str) -> list[dict[str, Any]]:
    return [doc for (dt_, _), doc in site.documents.items() if dt_ == doctype]


def _puts(site: _Site) -> list[dict[str, Any]]:
    return [json.loads(r.content) for r in site.requests if r.method == "PUT"]


# --- the document -------------------------------------------------------------


def test_a_bank_transaction_splits_the_sign_into_deposit_and_withdrawal(posting_ctx) -> None:
    rows = by_id(all_transactions())
    withdrawal = bank_transaction(rows["txn_ach_utilities"], posting_ctx)
    deposit = bank_transaction(rows["txn_deposit"], posting_ctx)
    assert (withdrawal["withdrawal"], withdrawal["deposit"]) == (100.0, 0.0)
    assert (deposit["deposit"], deposit["withdrawal"]) == (10000.0, 0.0)
    assert withdrawal["bank_account"] == "Mercury Checking - T"


def test_the_date_is_the_settlement_date(posting_ctx) -> None:
    transaction = by_id(all_transactions())["txn_wire_payroll"]
    assert transaction.created_at.date().isoformat() != "2026-08-04"
    assert bank_transaction(transaction, posting_ctx)["date"] == "2026-08-04"


def test_a_categorised_card_charge_is_a_bank_transaction_on_the_cards_account(posting_ctx) -> None:
    # Card spend used to be journaled straight to the category's account. Now
    # it is a statement line on the card's Bank Account like everything else,
    # and the person reconciling picks the account.
    doc = bank_transaction(by_id(all_transactions())["txn_card_saas"], posting_ctx)
    assert doc["doctype"] == "Bank Transaction"
    assert doc["bank_account"] == "Mercury IO Card - T"
    assert (doc["withdrawal"], doc["deposit"]) == (29.0, 0.0)
    assert "accounts" not in doc


def test_each_leg_of_a_transfer_is_a_bank_transaction_on_its_own_account(posting_ctx) -> None:
    rows = by_id(all_transactions())
    out = bank_transaction(rows["txn_transfer_out"], posting_ctx)
    inflow = bank_transaction(rows["txn_transfer_in"], posting_ctx)
    assert (out["bank_account"], out["withdrawal"]) == ("Mercury Checking - T", 47.61)
    assert (inflow["bank_account"], inflow["deposit"]) == ("Mercury Savings - T", 47.61)
    assert out[TRANSACTION_ID_FIELD] != inflow[TRANSACTION_ID_FIELD]


def test_a_bank_transaction_attaches_to_the_account_the_money_moved_through(posting_ctx) -> None:
    # The ingest reads checking, savings and the card. One Bank Account for all
    # of them posts a savings withdrawal against checking, where it is wrong in
    # the ledger and invisible to the reconciliation this doctype is for.
    rows = by_id(all_transactions())
    on_savings = dataclasses.replace(rows["txn_deposit"], account_id="acct_chk_2")
    assert bank_transaction(rows["txn_deposit"], posting_ctx)["bank_account"] == "Mercury Checking - T"
    assert bank_transaction(on_savings, posting_ctx)["bank_account"] == "Mercury Savings - T"


def test_a_bank_transaction_carries_mercurys_gl_code_and_note(posting_ctx) -> None:
    # Both are what a person coded in Mercury. The GL code is the account they
    # expect to pick at reconciliation, carried verbatim so it reads the same as
    # the `GL Code` column of a statement export imported by hand.
    coded = dataclasses.replace(by_id(all_transactions())["txn_ach_utilities"], note="From Gavin")
    doc = bank_transaction(coded, posting_ctx)
    assert doc[GL_CODE_FIELD] == "5215 - Phone & Internet (Verizon, Comcast)"
    assert doc[NOTE_FIELD] == "From Gavin"


def test_a_row_coded_by_its_category_carries_that_code_in_the_field_and_description(posting_ctx) -> None:
    # The category is a GL-code label, so it is the hint the person reconciling needs.
    categorised = dataclasses.replace(
        by_id(all_transactions())["txn_deposit"], custom_category="5204 - Office Software & SaaS"
    )
    doc = bank_transaction(categorised, posting_ctx)
    assert doc[GL_CODE_FIELD] == "5204 - Office Software & SaaS"
    assert doc["description"] == "5204 - Office Software & SaaS · ACME BICYCLES INV 1001"


def test_a_bank_transaction_without_coding_carries_neither_field(posting_ctx) -> None:
    uncoded = by_id(all_transactions())["txn_deposit"]
    assert uncoded.gl_code_name is None and uncoded.note is None
    doc = bank_transaction(uncoded, posting_ctx)
    assert GL_CODE_FIELD not in doc
    assert NOTE_FIELD not in doc


def test_the_reference_number_is_the_trace_number_a_bank_statement_shows(posting_ctx) -> None:
    # The Banking workspace shows `reference_number` as "Ref" on every
    # unreconciled card. A Mercury UUID there is twelve rows of noise; the ACH
    # trace number is what the statement says, what a hand import carries, and
    # what adoption already matches on -- so an ingested row and a hand-imported
    # one now read the same.
    transaction = by_id(all_transactions())["txn_ach_utilities"]
    assert transaction.tracking_number == TRACE
    assert bank_transaction(transaction, posting_ctx)["reference_number"] == TRACE


def test_a_transaction_with_no_trace_number_keeps_the_mercury_id_as_its_reference(posting_ctx) -> None:
    # A card charge never went through the ACH network and has no trace number.
    # The id is a poor reference and a real one: it names the row at Mercury.
    transaction = by_id(all_transactions())["txn_card_saas"]
    assert transaction.tracking_number is None
    assert bank_transaction(transaction, posting_ctx)["reference_number"] == "txn_card_saas"


def test_the_description_leads_with_the_note_then_the_gl_code_then_the_bank_text(posting_ctx) -> None:
    # The one hint a person needs when reconciling sat in custom fields the
    # card does not show, so they had to open every row to redo work the
    # software already held.
    coded = dataclasses.replace(by_id(all_transactions())["txn_ach_utilities"], note="1Password Teams")
    doc = bank_transaction(coded, posting_ctx)
    assert doc["description"] == "1Password Teams · 5215 - Phone & Internet (Verizon, Comcast) · SONIC INTERNET"
    # The custom fields stay the live copy of both.
    assert (doc[NOTE_FIELD], doc[GL_CODE_FIELD]) == ("1Password Teams", "5215 - Phone & Internet (Verizon, Comcast)")


def test_a_description_with_a_gl_code_and_no_note_skips_the_note(posting_ctx) -> None:
    coded = by_id(all_transactions())["txn_ach_utilities"]
    assert coded.note is None
    assert (
        bank_transaction(coded, posting_ctx)["description"]
        == "5215 - Phone & Internet (Verizon, Comcast) · SONIC INTERNET"
    )


def test_a_description_with_a_note_and_no_gl_code_skips_the_code(posting_ctx) -> None:
    noted = dataclasses.replace(by_id(all_transactions())["txn_deposit"], note="Deposit from Acme")
    assert noted.gl_code_name is None
    assert bank_transaction(noted, posting_ctx)["description"] == "Deposit from Acme · ACME BICYCLES INV 1001"


def test_a_transaction_with_neither_keeps_exactly_the_bank_text(posting_ctx) -> None:
    # No leading or trailing separator, ever: an uncoded row reads exactly as
    # it did before this composition existed.
    uncoded = by_id(all_transactions())["txn_deposit"]
    assert (uncoded.note, uncoded.gl_code_name) == (None, None)
    assert bank_transaction(uncoded, posting_ctx)["description"] == "ACME BICYCLES INV 1001"


def test_a_mercury_account_with_no_bank_account_refuses_rather_than_using_anothers(mercury_mapping) -> None:
    without_card = Mapping(
        accounts=mercury_mapping.accounts,
        bank_accounts={k: v for k, v in mercury_mapping.bank_accounts.items() if k != "acct_credit_1"},
    )
    ctx = PostingContext(
        company="Transpire",
        account_names=dict(ACCOUNT_DOCUMENT_NAMES),
        mapping=without_card,
        bot_user=EXAMPLE_BOOKS.bot.user,
        chart=example_chart(),
    )
    with pytest.raises(PostingError) as exc:
        bank_transaction(by_id(all_transactions())["txn_card_saas"], ctx)
    assert "acct_credit_1" in str(exc.value)
    assert "[bank_accounts]" in str(exc.value)


# --- one document per transaction -------------------------------------------


def test_upsert_creates_once_and_then_amends(ledger, site, posting_ctx) -> None:
    # A later event for the same transaction must find the row rather than
    # gain a second one.
    transaction = by_id(all_transactions())["txn_deposit"]
    first, name = upsert(ledger, bank_transaction(transaction, posting_ctx), transaction, resource_version=1)
    second, again = upsert(ledger, bank_transaction(transaction, posting_ctx), transaction, resource_version=2)
    assert (first, second) == (Outcome.SUBMITTED, Outcome.UPDATED)
    assert name == again
    assert len(documents_of(site, "Bank Transaction")) == 1
    assert site.documents[("Bank Transaction", name)][RESOURCE_VERSION_FIELD] == 2


def test_a_row_whose_mercury_facts_are_unchanged_is_left_alone(ledger, site, posting_ctx) -> None:
    transaction = by_id(all_transactions())["txn_deposit"]
    upsert(ledger, bank_transaction(transaction, posting_ctx), transaction, resource_version=1)
    outcome, _ = upsert(ledger, bank_transaction(transaction, posting_ctx), transaction, resource_version=1)
    assert outcome is Outcome.UNCHANGED
    assert len(_puts(site)) == 0


def test_two_documents_for_one_transaction_stop_the_run(ledger, site, posting_ctx) -> None:
    # The state the unique custom field exists to prevent. Amending one at
    # random would leave the other in the ledger unnoticed.
    transaction = by_id(all_transactions())["txn_deposit"]
    doc = bank_transaction(transaction, posting_ctx)
    upsert(ledger, doc, transaction)
    site.documents[("Bank Transaction", "BT-9999")] = {**doc, "docstatus": 0}
    with pytest.raises(PostingError) as exc:
        upsert(ledger, doc, transaction)
    assert "BT-9999" in str(exc.value)
    assert "install-fields" in str(exc.value)


def test_upsert_looks_the_document_up_by_the_mercury_id(ledger, site, posting_ctx) -> None:
    transaction = by_id(all_transactions())["txn_deposit"]
    upsert(ledger, bank_transaction(transaction, posting_ctx), transaction)
    lookup = site.requests[0]
    assert lookup.url.path.replace("%20", " ").endswith("/Bank Transaction")
    assert json.loads(lookup.url.params["filters"]) == [[TRANSACTION_ID_FIELD, "=", "txn_deposit"]]


# --- adopting a row somebody imported by hand ---------------------------------

TRACE = "091000015549612"


def _hand_imported(site: _Site, *, reference: str, name: str = "BT-HAND-1", **overrides: Any) -> str:
    """A Bank Transaction as Bank Statement Import leaves it: submitted, `Tracking ID` in the reference, no Mercury fields.

    Its date is the export's `Date (UTC)` and its description the export's
    `Description`, neither of which is the spelling the ingest itself would
    write for the same transaction.
    """
    doc: dict[str, Any] = {
        "date": "2026-08-05",
        "bank_account": "Mercury Checking - T",
        "company": "Transpire",
        "deposit": 0.0,
        "withdrawal": 100.0,
        "description": "Sonic",
        "reference_number": reference,
        "docstatus": 1,
    }
    doc.update(overrides)
    site.documents[("Bank Transaction", name)] = doc
    return name


def test_a_hand_imported_row_with_the_same_trace_number_is_adopted_not_duplicated(ledger, site, posting_ctx) -> None:
    # Bank Statement Import maps Mercury's `Tracking ID` onto `reference_number`
    # and submits. The ingest reads the same trace number from the API, so the
    # row a person already committed is the document this transaction has --
    # posting a second one would put the same money in the ledger twice.
    transaction = by_id(all_transactions())["txn_ach_utilities"]
    name = _hand_imported(site, reference=TRACE)
    outcome, adopted = upsert(ledger, bank_transaction(transaction, posting_ctx), transaction, resource_version=3)
    assert (outcome, adopted) == (Outcome.ADOPTED, name)
    assert len(documents_of(site, "Bank Transaction")) == 1
    # Only Mercury's own fields are stamped on. The amount, date, description
    # and reference are the person's and stay exactly as imported.
    assert _puts(site) == [
        {
            TRANSACTION_ID_FIELD: "txn_ach_utilities",
            RESOURCE_VERSION_FIELD: 3,
            GL_CODE_FIELD: "5215 - Phone & Internet (Verizon, Comcast)",
        }
    ]
    row = site.documents[("Bank Transaction", name)]
    assert (row["docstatus"], row["reference_number"], row["description"]) == (1, TRACE, "Sonic")


def test_an_adopted_row_is_found_by_its_mercury_id_afterwards(ledger, site, posting_ctx) -> None:
    transaction = by_id(all_transactions())["txn_ach_utilities"]
    name = _hand_imported(site, reference=TRACE)
    upsert(ledger, bank_transaction(transaction, posting_ctx), transaction, resource_version=1)
    outcome, again = upsert(ledger, bank_transaction(transaction, posting_ctx), transaction, resource_version=1)
    assert (outcome, again) == (Outcome.UNCHANGED, name)
    assert len(documents_of(site, "Bank Transaction")) == 1


def test_two_rows_with_the_same_trace_number_are_refused_rather_than_guessed(ledger, site, posting_ctx) -> None:
    # A statement imported twice, or a trace number typed onto the wrong row.
    # Picking one would stamp Mercury's identity on a document that may be the
    # wrong one, and the other keeps looking like an unmatched import forever.
    transaction = by_id(all_transactions())["txn_ach_utilities"]
    _hand_imported(site, reference=TRACE, name="BT-A")
    _hand_imported(site, reference=TRACE, name="BT-B")
    with pytest.raises(PostingError) as exc:
        upsert(ledger, bank_transaction(transaction, posting_ctx), transaction)
    assert "BT-A" in str(exc.value)
    assert "BT-B" in str(exc.value)
    assert TRACE in str(exc.value)
    assert _puts(site) == []
    assert len(documents_of(site, "Bank Transaction")) == 2


def test_a_row_without_a_trace_number_posts_as_before(ledger, site, posting_ctx) -> None:
    deposit = by_id(all_transactions())["txn_deposit"]
    assert deposit.tracking_number is None
    _hand_imported(site, reference=TRACE)
    outcome, _ = upsert(ledger, bank_transaction(deposit, posting_ctx), deposit)
    assert outcome is Outcome.SUBMITTED
    assert len(documents_of(site, "Bank Transaction")) == 2


def test_a_matching_trace_number_on_another_bank_account_is_not_adopted(ledger, site, posting_ctx) -> None:
    # The trace number identifies an ACH entry, and the same entry shows up on
    # both accounts of a transfer between them. The Bank Account narrows it to
    # the side this transaction is.
    transaction = by_id(all_transactions())["txn_ach_utilities"]
    _hand_imported(site, reference=TRACE, bank_account="Mercury Savings - T")
    outcome, _ = upsert(ledger, bank_transaction(transaction, posting_ctx), transaction)
    assert outcome is Outcome.SUBMITTED
    assert len(documents_of(site, "Bank Transaction")) == 2


def test_a_cancelled_row_with_the_trace_number_is_not_adopted(ledger, site, posting_ctx) -> None:
    transaction = by_id(all_transactions())["txn_ach_utilities"]
    _hand_imported(site, reference=TRACE, docstatus=2)
    outcome, _ = upsert(ledger, bank_transaction(transaction, posting_ctx), transaction)
    assert outcome is Outcome.SUBMITTED


def test_a_later_note_change_updates_an_adopted_submitted_row(ledger, site, posting_ctx) -> None:
    # The four Mercury fields are `allow_on_submit`, so a note or GL code
    # arriving after the person submitted still lands. The hand-imported row
    # spells the date and description the export's way, not the ingest's; those
    # are Mercury's own facts in Mercury's own words and not a disagreement.
    transaction = by_id(all_transactions())["txn_ach_utilities"]
    name = _hand_imported(site, reference=TRACE)
    upsert(ledger, bank_transaction(transaction, posting_ctx), transaction, resource_version=1)
    noted = dataclasses.replace(transaction, note="From Gavin")
    outcome, same = upsert(ledger, bank_transaction(noted, posting_ctx), noted, resource_version=2)
    assert (outcome, same) == (Outcome.UPDATED, name)
    assert _puts(site)[-1] == {RESOURCE_VERSION_FIELD: 2, NOTE_FIELD: "From Gavin"}
    row = site.documents[("Bank Transaction", name)]
    assert (row["docstatus"], row["withdrawal"], row["date"], row["description"]) == (1, 100.0, "2026-08-05", "Sonic")


def test_a_change_to_the_amount_of_a_submitted_row_still_refuses(ledger, site, posting_ctx) -> None:
    # Metadata is the only thing the ingest may touch on a committed document.
    # Money, date, description and account are the person's, and a disagreement
    # is reported by name rather than resolved either way.
    transaction = by_id(all_transactions())["txn_ach_utilities"]
    _hand_imported(site, reference=TRACE)
    upsert(ledger, bank_transaction(transaction, posting_ctx), transaction, resource_version=1)
    resized = dataclasses.replace(transaction, amount=Decimal("-101.00"), note="From Gavin")
    verdict = plan(ledger, bank_transaction(resized, posting_ctx), resized, resource_version=2)
    assert verdict.outcome is Outcome.REFUSED
    assert "withdrawal" in verdict.reason
    outcome, _ = upsert(ledger, bank_transaction(resized, posting_ctx), resized, resource_version=2)
    assert outcome is Outcome.REFUSED
    assert len(_puts(site)) == 1
    assert site.documents[("Bank Transaction", "BT-HAND-1")].get(NOTE_FIELD) is None


def test_a_change_to_the_description_of_a_submitted_row_is_refused_by_name(ledger, site, posting_ctx) -> None:
    # Adoption keys on the trace number alone, so the row is adopted with the
    # description the person typed; the next event compares content and finds
    # a description Mercury never said.
    transaction = by_id(all_transactions())["txn_ach_utilities"]
    _hand_imported(site, reference=TRACE, description="Somebody else entirely")
    upsert(ledger, bank_transaction(transaction, posting_ctx), transaction, resource_version=1)
    verdict = plan(ledger, bank_transaction(transaction, posting_ctx), transaction, resource_version=2)
    assert verdict.outcome is Outcome.REFUSED
    assert "description" in verdict.reason
    assert "Somebody else entirely" in verdict.reason


def _already_ingested(site: _Site, transaction_id: str, *, reference: str, description: str, **overrides: Any) -> str:
    """A submitted row the ingest already accounts for, spelled however an earlier run spelled it."""
    fields: dict[str, Any] = {TRANSACTION_ID_FIELD: transaction_id, RESOURCE_VERSION_FIELD: 1, **overrides}
    return _hand_imported(site, reference=reference, description=description, **fields)


def test_an_old_uuid_reference_on_a_submitted_row_is_not_a_disagreement(ledger, site, posting_ctx) -> None:
    # Rows posted before the reference became the trace number carry the
    # Mercury id there. Both spellings are ours, and reporting our own change
    # of mind as the person's disagreement would refuse every later note.
    transaction = by_id(all_transactions())["txn_ach_utilities"]
    _already_ingested(
        site,
        "txn_ach_utilities",
        reference="txn_ach_utilities",
        description="5215 - Phone & Internet (Verizon, Comcast) · SONIC INTERNET",
        **{GL_CODE_FIELD: "5215 - Phone & Internet (Verizon, Comcast)"},
    )
    verdict = plan(ledger, bank_transaction(transaction, posting_ctx), transaction, resource_version=2)
    assert verdict.outcome is Outcome.UPDATED
    assert verdict.body == {RESOURCE_VERSION_FIELD: 2}


def test_a_reference_a_person_typed_is_never_a_disagreement(ledger, site, posting_ctx) -> None:
    # Adoption writes no reference, so an adopted row keeps whatever the person
    # meant by it -- a check number, an invoice, the number they read off the
    # statement. It is theirs, and the ingest neither compares nor rewrites it.
    transaction = by_id(all_transactions())["txn_ach_utilities"]
    _already_ingested(
        site,
        "txn_ach_utilities",
        reference="CHECK 1042",
        description="Sonic",
        **{GL_CODE_FIELD: "5215 - Phone & Internet (Verizon, Comcast)"},
    )
    noted = dataclasses.replace(transaction, note="1Password Teams")
    verdict = plan(ledger, bank_transaction(noted, posting_ctx), noted, resource_version=2)
    assert verdict.outcome is Outcome.UPDATED
    assert verdict.body == {RESOURCE_VERSION_FIELD: 2, NOTE_FIELD: "1Password Teams"}
    assert site.documents[("Bank Transaction", "BT-HAND-1")]["reference_number"] == "CHECK 1042"


def test_an_old_style_description_on_a_submitted_row_is_not_a_disagreement(ledger, site, posting_ctx) -> None:
    # A row posted when the description was the bank text alone. It still ends
    # with the bank text, which is the bank's fact and the only part of the
    # description the ingest holds anyone to.
    transaction = by_id(all_transactions())["txn_ach_utilities"]
    _already_ingested(site, "txn_ach_utilities", reference=TRACE, description="SONIC INTERNET")
    verdict = plan(ledger, bank_transaction(transaction, posting_ctx), transaction, resource_version=2)
    assert verdict.outcome is Outcome.UPDATED
    assert verdict.body == {RESOURCE_VERSION_FIELD: 2, GL_CODE_FIELD: "5215 - Phone & Internet (Verizon, Comcast)"}


def test_a_note_changed_after_submit_does_not_make_the_old_description_a_disagreement(
    ledger, site, posting_ctx
) -> None:
    # The description is fixed at submit time and the custom fields follow. A
    # person recoding a row at Mercury must not have their note refused because
    # the description we composed last month no longer matches the one we would
    # compose today.
    transaction = by_id(all_transactions())["txn_ach_utilities"]
    _already_ingested(
        site,
        "txn_ach_utilities",
        reference=TRACE,
        description="Google Cloud August · 5215 - Phone & Internet (Verizon, Comcast) · SONIC INTERNET",
        **{GL_CODE_FIELD: "5215 - Phone & Internet (Verizon, Comcast)", NOTE_FIELD: "Google Cloud August"},
    )
    recoded = dataclasses.replace(transaction, note="1Password Teams")
    verdict = plan(ledger, bank_transaction(recoded, posting_ctx), recoded, resource_version=2)
    assert verdict.outcome is Outcome.UPDATED
    assert verdict.body == {RESOURCE_VERSION_FIELD: 2, NOTE_FIELD: "1Password Teams"}
    assert site.documents[("Bank Transaction", "BT-HAND-1")]["description"].startswith("Google Cloud August")


def test_a_bank_text_the_row_never_carried_is_still_refused(ledger, site, posting_ctx) -> None:
    # The promise the tolerance must not break: the bank's own words changing
    # under a row a person committed is still a disagreement they decide.
    transaction = by_id(all_transactions())["txn_ach_utilities"]
    _already_ingested(
        site,
        "txn_ach_utilities",
        reference=TRACE,
        description="1Password Teams · 5215 - Phone & Internet (Verizon, Comcast) · SONIC INTERNET",
    )
    elsewhere = dataclasses.replace(transaction, bank_description="COMCAST BUSINESS", counterparty_name="Comcast")
    verdict = plan(ledger, bank_transaction(elsewhere, posting_ctx), elsewhere, resource_version=2)
    assert verdict.outcome is Outcome.REFUSED
    assert "description" in verdict.reason
    assert "COMCAST BUSINESS" in verdict.reason


# --- submitting ---------------------------------------------------------------


def test_a_new_bank_transaction_is_submitted_in_the_request_that_creates_it(ledger, site, posting_ctx) -> None:
    # A Bank Transaction posts nothing to the general ledger on submit; the
    # posting happens when a person reconciles it to a voucher. Submitting is
    # what makes the row visible to the Bank Reconciliation Tool at all. One
    # request rather than insert-then-submit, so a refused submit leaves no
    # draft behind for the next run to find and wonder about.
    transaction = by_id(all_transactions())["txn_deposit"]
    outcome, name = upsert(ledger, bank_transaction(transaction, posting_ctx), transaction)
    assert outcome is Outcome.SUBMITTED
    assert site.documents[("Bank Transaction", name)]["docstatus"] == 1
    posted = [json.loads(r.content) for r in site.requests if r.method == "POST"]
    assert len(posted) == 1
    assert posted[0]["docstatus"] == 1


def test_only_a_new_bank_transaction_ever_carries_a_docstatus(ledger, site, posting_ctx) -> None:
    # The document itself is built without one, so nothing that reuses the body
    # -- an amendment, an adoption -- can submit a row a person left as a draft
    # or re-submit one they already committed.
    transaction = by_id(all_transactions())["txn_ach_utilities"]
    assert "docstatus" not in bank_transaction(transaction, posting_ctx)
    _hand_imported(site, reference=TRACE, docstatus=0)
    upsert(ledger, bank_transaction(transaction, posting_ctx), transaction, resource_version=1)
    noted = dataclasses.replace(transaction, note="From Gavin")
    upsert(ledger, bank_transaction(noted, posting_ctx), noted, resource_version=2)
    assert len(_puts(site)) == 2
    assert all("docstatus" not in body for body in _puts(site))
    assert site.documents[("Bank Transaction", "BT-HAND-1")]["docstatus"] == 0


def test_a_drafts_only_client_refuses_to_post_a_bank_transaction_before_sending(site, posting_ctx) -> None:
    # The client-side guard is a courtesy, not the boundary -- that is the
    # bot's role on the server -- but the ingest asks for the capability
    # deliberately, and a client built without it must fail loudly rather than
    # quietly land a draft nothing will ever submit.
    transaction = by_id(all_transactions())["txn_deposit"]
    with (
        Client(DRAFTS_ONLY, transport=httpx2.MockTransport(site.handler)) as drafts_only,
        pytest.raises(SubmitNotPermittedError),
    ):
        upsert(drafts_only, bank_transaction(transaction, posting_ctx), transaction)
    assert documents_of(site, "Bank Transaction") == []


# --- the custom fields --------------------------------------------------------


def test_journal_entry_carries_the_identity_field_and_no_version() -> None:
    # The voucher path drafts a Journal Entry beside a coded Bank Transaction
    # and finds it again by the same Mercury id. A resource version on it would
    # claim the log said something it did not: coding a transaction at Mercury
    # bumps no `resourceVersion`.
    on_journal_entry = [f for f in custom_field_documents() if f["dt"] == "Journal Entry"]
    assert [f["fieldname"] for f in on_journal_entry] == [TRANSACTION_ID_FIELD]


def test_the_journal_entry_identity_field_is_not_unique_so_a_person_can_amend() -> None:
    # ERPNext's Amend copies the id onto the amendment while the cancelled
    # original still holds it. A unique index -- which cannot exempt cancelled
    # rows -- refuses that save, and with it every correction to our vouchers.
    (on_journal_entry,) = [f for f in custom_field_documents() if f["dt"] == "Journal Entry"]
    assert on_journal_entry["unique"] == 0


def test_the_bank_transaction_fields_are_the_four_the_ingest_writes() -> None:
    on_bank_transaction = [f for f in custom_field_documents() if f["dt"] == "Bank Transaction"]
    assert [f["fieldname"] for f in on_bank_transaction] == [
        TRANSACTION_ID_FIELD,
        RESOURCE_VERSION_FIELD,
        GL_CODE_FIELD,
        NOTE_FIELD,
    ]


def test_the_transaction_id_field_is_unique_so_the_server_enforces_idempotency() -> None:
    fields = {f["fieldname"]: f for f in custom_field_documents() if f["dt"] == "Bank Transaction"}
    assert fields[TRANSACTION_ID_FIELD]["unique"] == 1
    assert fields[RESOURCE_VERSION_FIELD]["fieldtype"] == "Int"


def test_the_gl_code_and_note_fields_are_writable() -> None:
    # The Bank Statement Import mapper cannot target a read-only custom field,
    # and a hand-imported statement row has to carry the same two columns the
    # ingest fills.
    fields = {f["fieldname"]: f for f in custom_field_documents() if f["dt"] == "Bank Transaction"}
    gl_code = fields[GL_CODE_FIELD]
    note = fields[NOTE_FIELD]
    assert (gl_code["fieldtype"], gl_code["read_only"], gl_code["unique"]) == ("Data", 0, 0)
    assert (note["fieldtype"], note["read_only"], note["unique"]) == ("Small Text", 0, 0)


def test_every_bank_transaction_field_may_be_written_on_a_submitted_document() -> None:
    # A hand-imported row is submitted before the ingest ever sees it, and the
    # Mercury fields are stamped onto it afterwards. Frappe refuses any write
    # to a submitted document except to fields marked `allow_on_submit`.
    fields = custom_field_documents()
    assert all(f["allow_on_submit"] == 1 for f in fields if f["dt"] == "Bank Transaction")
    # The voucher never writes to a submitted Journal Entry, so it must not be
    # able to: a person's committed entry is theirs.
    assert all(f["allow_on_submit"] == 0 for f in fields if f["dt"] == "Journal Entry")


def test_a_field_installed_before_allow_on_submit_is_repaired(ledger, site) -> None:
    # The fields were installed once already without the flag. A presence
    # check would call them correct and the first adoption would then fail on
    # the server with a message about a submitted document.
    install_custom_fields(ledger)
    name = next(
        n
        for (dt_, n), doc in site.documents.items()
        if dt_ == "Custom Field" and doc.get("dt") == "Bank Transaction" and doc.get("fieldname") == NOTE_FIELD
    )
    site.documents[("Custom Field", name)]["allow_on_submit"] = 0
    results = dict(install_custom_fields(ledger))
    assert results["Bank Transaction.mercury_note"] is Outcome.UPDATED
    assert site.documents[("Custom Field", name)]["allow_on_submit"] == 1


def test_installing_the_custom_fields_twice_creates_them_once(ledger, site) -> None:
    first = install_custom_fields(ledger)
    second = install_custom_fields(ledger)
    assert [outcome for _, outcome in first] == [Outcome.CREATED] * 5
    assert [outcome for _, outcome in second] == [Outcome.UNCHANGED] * 5
    assert len([k for k in site.documents if k[0] == "Custom Field"]) == 5


def test_a_field_that_exists_without_unique_is_repaired(ledger, site) -> None:
    # A presence check would call this correct and the ingest would then tell
    # people to run this command to fix a duplicate it can never prevent.
    install_custom_fields(ledger)
    name = next(
        n
        for (dt_, n), doc in site.documents.items()
        if dt_ == "Custom Field"
        and doc.get("dt") == "Bank Transaction"
        and doc.get("fieldname") == TRANSACTION_ID_FIELD
    )
    site.documents[("Custom Field", name)]["unique"] = 0
    results = dict(install_custom_fields(ledger))
    assert results["Bank Transaction.mercury_transaction_id"] is Outcome.UPDATED
    assert site.documents[("Custom Field", name)]["unique"] == 1


def test_a_version_field_wrongly_marked_unique_is_repaired(ledger, site) -> None:
    # `unique` on the version field would reject the second transaction to
    # reach version 1, which is every transaction after the first -- so the
    # repair pass has to state the value it expects rather than only checking
    # the properties it happens to set.
    install_custom_fields(ledger)
    name = next(
        n
        for (dt_, n), doc in site.documents.items()
        if dt_ == "Custom Field" and doc.get("fieldname") == "mercury_resource_version"
    )
    site.documents[("Custom Field", name)]["unique"] = 1
    results = dict(install_custom_fields(ledger))
    assert Outcome.UPDATED in results.values()
    assert site.documents[("Custom Field", name)]["unique"] == 0


def test_a_journal_entry_identity_field_still_marked_unique_is_repaired(ledger, site) -> None:
    # Every site installed before vouchers became amendable carries `unique`
    # on this field; re-running install-fields is how it is taken off.
    install_custom_fields(ledger)
    name = next(
        n
        for (dt_, n), doc in site.documents.items()
        if dt_ == "Custom Field" and doc.get("dt") == "Journal Entry" and doc.get("fieldname") == TRANSACTION_ID_FIELD
    )
    site.documents[("Custom Field", name)]["unique"] = 1
    results = dict(install_custom_fields(ledger))
    assert results["Journal Entry.mercury_transaction_id"] is Outcome.UPDATED
    assert site.documents[("Custom Field", name)]["unique"] == 0


def test_a_reversed_transaction_never_reaches_the_posting_path() -> None:
    transaction = transactions_from("transaction_reversed", "acct_chk_1")[0]
    assert classify(transaction).posts is False


# --- Bank and Bank Account installation -------------------------------------

BANK_MAPPING_TOML = """
[accounts]
"acct_chk_1" = "1111"
"acct_sav_1" = "1112"
"acct_credit_1" = "2121"

[bank_accounts]
"acct_chk_1" = "Mercury Checking - Mercury"
"acct_sav_1" = "Mercury Savings - Mercury"
"acct_credit_1" = "Mercury IO Card - Mercury"
"""


def _bank_context(toml: str = BANK_MAPPING_TOML) -> PostingContext:
    return PostingContext(
        company="Transpire",
        account_names=dict(ACCOUNT_DOCUMENT_NAMES),
        mapping=parse_mapping(toml),
        bot_user=EXAMPLE_BOOKS.bot.user,
        chart=example_chart(),
    )


def test_a_bank_account_document_links_the_chart_account_holding_that_balance() -> None:
    documents = {d["account_name"]: d for d in bank_account_documents(_bank_context())}
    assert set(documents) == {"Mercury Checking", "Mercury Savings", "Mercury IO Card"}
    checking = documents["Mercury Checking"]
    assert checking["bank"] == BANK
    assert checking["account"].startswith("1111")
    assert checking["company"] == "Transpire"
    assert checking["is_company_account"] == 1
    assert documents["Mercury IO Card"]["account"].startswith("2121")
    assert all("docstatus" not in d for d in documents.values())


def test_a_bank_account_name_that_erpnext_would_not_produce_is_refused() -> None:
    # ERPNext names the document "<account name> - <bank>"; an entry without
    # that suffix would be a link the first Bank Transaction cannot resolve.
    toml = BANK_MAPPING_TOML.replace('"Mercury Savings - Mercury"', '"Mercury Savings"')
    with pytest.raises(PostingError, match="must end with ' - Mercury'"):
        bank_account_documents(_bank_context(toml))


def test_a_bank_account_for_a_mercury_account_with_no_chart_account_is_refused() -> None:
    toml = BANK_MAPPING_TOML.replace('"acct_credit_1" = "2121"\n', "")
    with pytest.raises(PostingError, match=r"acct_credit_1.*no \[accounts\] entry"):
        bank_account_documents(_bank_context(toml))


def test_a_bank_account_for_a_chart_account_the_site_does_not_have_refuses() -> None:
    ctx = PostingContext(
        company="Transpire",
        account_names={},
        mapping=parse_mapping(BANK_MAPPING_TOML),
        bot_user=EXAMPLE_BOOKS.bot.user,
        chart=example_chart(),
    )
    with pytest.raises(PostingError) as exc:
        bank_account_documents(ctx)
    assert "apply the chart" in str(exc.value)


def test_installing_the_bank_accounts_twice_creates_them_once(ledger, site) -> None:
    context = _bank_context()
    first = install_bank_accounts(ledger, context)
    second = install_bank_accounts(ledger, context)
    assert [outcome for _, outcome in first] == [Outcome.CREATED] * 4
    assert [outcome for _, outcome in second] == [Outcome.UNCHANGED] * 4
    assert first[0][0] == "Bank Mercury"
    assert len([k for k in site.documents if k[0] == "Bank"]) == 1
    assert len([k for k in site.documents if k[0] == "Bank Account"]) == 3


def test_the_installer_sends_the_bank_account_erpnext_needs(ledger, site) -> None:
    install_bank_accounts(ledger, _bank_context())
    posted = [
        json.loads(r.content)
        for r in site.requests
        if r.method == "POST" and r.url.path.replace("%20", " ").endswith("/Bank Account")
    ]
    checking = next(p for p in posted if p["account_name"] == "Mercury Checking")
    assert checking == {
        "account_name": "Mercury Checking",
        "bank": "Mercury",
        "account": "1111 - Operating Checking - T",
        "is_company_account": 1,
        "company": "Transpire",
    }


def test_a_bank_account_linked_to_the_wrong_chart_account_is_repaired(ledger, site) -> None:
    # A presence check would call this correct, and every Bank Transaction on
    # that account would then file against the wrong balance.
    context = _bank_context()
    install_bank_accounts(ledger, context)
    name = next(
        n
        for (dt_, n), doc in site.documents.items()
        if dt_ == "Bank Account" and doc["account_name"] == "Mercury Savings"
    )
    site.documents[("Bank Account", name)]["account"] = "1111 - Operating Checking - T"
    results = dict(install_bank_accounts(ledger, context))
    assert results["Bank Account Mercury Savings - Mercury"] is Outcome.UPDATED
    assert site.documents[("Bank Account", name)]["account"].startswith("1112")
