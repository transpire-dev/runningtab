"""Provision the books bot against a real disposable site, then prove it cannot submit.

Skipped unless RUNNINGTAB_LIVE is set (see `require_live_target` in conftest.py).
NOT RUN in the session that wrote this file -- no RUNNINGTAB_TEST_URL/KEY/SECRET
were available -- so it is unverified against a live site. It is written to be
re-runnable: `provision` is
idempotent by design, so running this file twice in a row (or against a site
a prior run already provisioned) must not fail on that account. Nothing here
asserts a property that is only true of a freshly created bot.

`test_provisioned_bot_can_read_the_chart_and_the_company_record` only proves
Account/Company `read`. The bot's real job needs `Account: write, create,
delete`, `Company: write`, and `GL Entry: read` too (see `GRANTS` in
`runningtab/access.py`) -- and this codebase has already been bitten once by a
Custom DocPerm row that existed, read back correctly from
`permission_manager.get_permissions`, and still did not take effect for this
user's `has_permission` answer at the document level (the DocType-meta read,
noted in `provision`'s own docstring). Accepted doctype-level does not mean
effective; only a real document-level attempt proves it. The tests below
exercise each remaining grant directly, the same way `coa apply`/`coa prune`
would.
"""

import contextlib
import dataclasses
import datetime
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from icedcoffee_sync.apply.park import RENUMBER_METHOD
from icedcoffee_sync.erpnext.accounts import read_chart
from icedcoffee_sync.frappe.access import granted_flags_effective, probe_submit, prohibited_flags_allowed
from icedcoffee_sync.frappe.client import Client, SiteConfig
from icedcoffee_sync.frappe.errors import DoesNotExistError, FrappeError, PermissionError_

from runningtab.access import provision
from runningtab.chart import load_defaults
from runningtab.mercury.posting import (
    BANK_TRANSACTION,
    GL_CODE_FIELD,
    NOTE_FIELD,
    RESOURCE_VERSION_FIELD,
    TRANSACTION_ID_FIELD,
    install_custom_fields,
)
from runningtab.site import Target, is_test_site, site_config
from tests.conftest import live_books, require_live_target

pytestmark = pytest.mark.live_runningtab


@pytest.fixture
def client():
    config = require_live_target()
    if not is_test_site(live_books().site, config.url):
        # Fail, never skip: this fixture provisions a real user and role and
        pytest.fail(f"refusing to run against a host not in the books' test_hosts: {config.url}")
    with Client(config) as c:
        yield c


@pytest.fixture
def company(client) -> str:
    rows = client.list("Company", fields=["name"], limit=2)
    if len(rows) != 1:
        pytest.fail(f"expected exactly one company on the test site, found {len(rows)}")
    return rows[0]["name"]


def _bot_config(report):
    """The freshly minted bot credential, same URL as the test site."""
    return dataclasses.replace(
        site_config(live_books().site, Target.TEST), api_key=report.keys.api_key, api_secret=report.keys.api_secret
    )


def _submitting(config: SiteConfig) -> SiteConfig:
    """The same credential with `icedcoffee-sync`'s client-side submit guard lifted.

    `ingest_config` does exactly this to the stored credential. Lifting it is
    what makes the server's own answer the only thing these tests measure:
    a refusal raised locally would prove nothing about the API key's roles.
    """
    return dataclasses.replace(config, may_submit=True)


@pytest.fixture
def submitting_admin(client) -> Iterator[Client]:
    """A second administrator client that may cancel -- what teardown needs.

    The `client` fixture's credential carries the drafts-only guard, and a
    submitted Bank Transaction has to be cancelled before it can be deleted.
    """
    with Client(_submitting(require_live_target())) as c:
        yield c


@pytest.fixture
def mercury_fields(client) -> None:
    """The four Mercury custom fields on Bank Transaction, installed if the site lacks them.

    `install_custom_fields` is the command's own code path and is idempotent
    -- it creates what is absent and repairs what is wrong -- so this is safe
    to run on every pass. Nothing removes them afterwards: they are part of
    what a site the ingest posts into is supposed to have, not residue these
    tests own.
    """
    install_custom_fields(client)


@pytest.fixture
def bank_account(client, company) -> Iterator[str]:
    """A company Bank Transaction can hang off, reusing the site's own if it has one.

    The disposable site's data carries no Bank Account (and no Bank
    at all), so this creates a distinctly named pair and removes them again.
    An existing one is used untouched: other Bank Accounts on the site are
    somebody's, and a test that deletes one would take its transactions with
    it.
    """
    existing = client.list(
        "Bank Account",
        fields=["name"],
        filters=[["company", "=", company], ["is_company_account", "=", 1]],
        limit=1,
    )
    if existing:
        yield existing[0]["name"]
        return

    ledger_account = client.list(
        "Account",
        fields=["name"],
        filters=[["company", "=", company], ["account_type", "=", "Bank"], ["is_group", "=", 0]],
        limit=1,
    )
    if not ledger_account:
        pytest.fail(f"the test site's chart has no non-group Bank account under {company} to link a Bank Account to")

    bank = "Live Test Bank"
    created_bank = False
    try:
        client.get("Bank", bank)
    except DoesNotExistError:
        client.insert("Bank", {"bank_name": bank})
        created_bank = True

    created = client.insert(
        "Bank Account",
        {
            "account_name": "Live Test Account",
            "bank": bank,
            "company": company,
            "is_company_account": 1,
            "account": ledger_account[0]["name"],
        },
    )
    try:
        yield created["name"]
    finally:
        with contextlib.suppress(FrappeError):
            client.delete("Bank Account", created["name"])
        if created_bank:
            with contextlib.suppress(FrappeError):
                client.delete("Bank", bank)


@pytest.fixture
def created_transactions(submitting_admin) -> Iterator[list[str]]:
    """Names of the Bank Transactions a test posted, cancelled and deleted as the administrator.

    The bot holds submit and nothing else on this doctype, so only an
    administrator can take a submitted row back off the site -- which is the
    point of the grant, and the reason cleanup cannot be the bot's job.
    """
    names: list[str] = []
    try:
        yield names
    finally:
        for name in reversed(names):
            with contextlib.suppress(FrappeError):
                submitting_admin.cancel(BANK_TRANSACTION, name)
            with contextlib.suppress(FrappeError):
                submitting_admin.delete(BANK_TRANSACTION, name)


@pytest.fixture
def created_files(client, created_transactions) -> Iterator[list[str]]:
    """Names of the File documents a test attached, deleted as the administrator.

    Depends on `created_transactions` so pytest tears this down first: an
    attachment is deleted before the document it hangs on, not after it has
    already gone.
    """
    names: list[str] = []
    try:
        yield names
    finally:
        for name in reversed(names):
            with contextlib.suppress(FrappeError):
                client.delete("File", name)


def _mercury_row(bank_account: str, company: str, transaction_id: str) -> dict[str, Any]:
    """A submit-on-insert Bank Transaction body, shaped like the ingest's.

    Dated yesterday and for a token amount: this is a real statement line on
    a real site, and a future date or a round number would read as somebody's
    data rather than a test's. `docstatus: 1` is the submit -- Frappe's insert
    path commits a document created with it, which is exactly the mechanic
    under test.
    """
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    return {
        "date": yesterday.isoformat(),
        "bank_account": bank_account,
        "company": company,
        "deposit": 0.0,
        "withdrawal": 1.23,
        "description": f"books live access test {transaction_id}",
        "reference_number": transaction_id,
        TRANSACTION_ID_FIELD: transaction_id,
        RESOURCE_VERSION_FIELD: 1,
        GL_CODE_FIELD: "5299 - UNCATEGORISED",
        NOTE_FIELD: "posted by books' live access suite",
        "docstatus": 1,
    }


def _minimal_pdf_bytes() -> bytes:
    """A structurally valid, byte-accurate minimal single-page PDF.

    Frappe scans every uploaded PDF for embedded JavaScript on save
    (`pdf_contains_js` in `frappe/utils/pdf.py`, via `pypdf.PdfReader`), so a
    byte string that merely starts with `%PDF` is rejected -- measured here,
    live: `%PDF-1.4\n%%EOF\n` came back as `PdfReadError`. Twin of the
    generator in icedcoffee-sync's `tests/test_live_frappe.py`; duplicated rather than
    shared because a test helper does not belong in the shipped library.
    """
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << >> >>",
    ]
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, content in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{i} 0 obj\n".encode()
        body += content + b"\n"
        body += b"endobj\n"
    xref_offset = len(body)
    n = len(objects) + 1
    xref = bytearray(f"xref\n0 {n}\n".encode())
    xref += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        xref += f"{off:010d} 00000 n \n".encode()
    body += xref
    body += f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    return bytes(body)


def test_provisioned_bot_cannot_submit(client, company) -> None:
    report = provision(client, live_books().bot)
    probe = probe_submit(_bot_config(report), company=company)
    assert probe.refused, (
        f"the bot's credential was NOT refused: can_create={probe.can_create} submit_flag={probe.submit_flag} "
        f"{probe.error.message if probe.error else f'it posted {probe.document_name}'}"
    )
    # The two server-evaluated flags this probe exists to check: a bare
    assert probe.can_create is True, "the bot must still be able to create a draft"
    assert probe.submit_flag is False, "the bot must not hold submit"


def test_provisioned_bot_may_submit_bank_transactions_and_nothing_else(client, company) -> None:
    # The one submit the bot holds. A Bank Transaction posts nothing to the
    # general ledger on submit -- the posting happens when a person reconciles
    # it -- so this is the exception "machines draft, humans post" can carry.
    # NOT RUN in the session that wrote it (no RUNNINGTAB_TEST_URL); read-only
    # against the server's own permission answers, so re-runnable at will.
    report = provision(client, live_books().bot)
    bot = _bot_config(report)
    assert granted_flags_effective(bot, doctype="Bank Transaction", ptypes=frozenset({"submit"})) == {"submit": True}
    for doctype in ("Journal Entry", "Purchase Invoice"):
        flags = prohibited_flags_allowed(bot, doctype=doctype, ptypes=frozenset({"submit", "cancel", "amend"}))
        assert flags == {"amend": False, "cancel": False, "submit": False}, doctype
    flags = prohibited_flags_allowed(bot, doctype="Bank Transaction", ptypes=frozenset({"cancel", "amend"}))
    assert flags == {"amend": False, "cancel": False}


def test_provisioned_bot_can_read_the_chart_and_the_company_record(client, company) -> None:
    # What `coa apply`/`coa plan` need: read_chart lists Account, and
    report = provision(client, live_books().bot)
    with Client(_bot_config(report)) as bot_client:
        chart = read_chart(bot_client, company)
        assert isinstance(chart, list)
        doc = bot_client.get("Company", company)
        assert doc["name"] == company


def test_provisioned_bot_can_create_renumber_write_and_delete_an_account(client, company) -> None:
    # Account: write, create, delete are the three grants coa apply and coa
    report = provision(client, live_books().bot)
    live = read_chart(client, company)
    parent = next((a for a in live if a.is_group), None)
    if parent is None:
        pytest.fail("the test site's chart has no group account to create a throwaway child under")

    created_name: str | None = None
    deleted_by_bot = False
    with Client(_bot_config(report)) as bot_client:
        try:
            created = bot_client.insert(
                "Account",
                {
                    "account_name": "Transpire Live Access Test",
                    "account_number": "99999999",
                    "company": company,
                    "is_group": 0,
                    "root_type": parent.root_type,
                    "parent_account": parent.document_name,
                },
            )
            created_name = created.get("name") if isinstance(created, dict) else None
            assert created_name, "Account.insert did not return a document name"

            renamed = bot_client.call(
                RENUMBER_METHOD,
                {"name": created_name, "account_name": "Transpire Live Access Test", "account_number": "99999998"},
            )
            created_name = str(renamed) if renamed else created_name.replace("99999999", "99999998", 1)

            bot_client.update("Account", created_name, {"disabled": 1})
            after_write = client.get("Account", created_name)
            assert bool(after_write.get("disabled"))

            bot_client.delete("Account", created_name)
            deleted_by_bot = True
            with pytest.raises(DoesNotExistError):
                client.get("Account", created_name)
        finally:
            # Admin fallback only: proving the bot's own delete grant is the
            if created_name is not None and not deleted_by_bot:
                client.delete("Account", created_name)


def test_provisioned_bot_can_write_a_company_default_field_back_to_itself(client, company) -> None:
    # Company: write is what set_default_accounts needs, and nothing above
    report = provision(client, live_books().bot)
    admin_doc = client.get("Company", company)
    field = next((f for f in load_defaults(live_books().chart_path) if admin_doc.get(f)), None)
    if field is None:
        pytest.skip("no chart default field is set on the test site's Company document to round-trip")
    value = admin_doc[field]

    with Client(_bot_config(report)) as bot_client:
        bot_client.update("Company", company, {field: value})

    after = client.get("Company", company)
    assert after.get(field) == value


def test_provisioned_bot_can_read_gl_entries(client, company) -> None:
    # GL Entry: read is what accounts_with_entries probes before every
    report = provision(client, live_books().bot)
    with Client(_bot_config(report)) as bot_client:
        rows = bot_client.list("GL Entry", fields=["account"], filters=[["company", "=", company]], limit=1)
    assert isinstance(rows, list)


def test_provisioned_bot_submits_a_bank_transaction_on_insert_and_a_refused_submit_leaves_no_draft(
    client, company, mercury_fields, bank_account, created_transactions
) -> None:
    """The ingest's one write path, end to end, against the server that has to allow it.

    `test_provisioned_bot_may_submit_bank_transactions_and_nothing_else`
    asks the server which flags it honours; this posts a real row. A
    doctype-level `submit` flag that reads back true and still refuses at the
    document is the failure this codebase has already been bitten by once.

    Three things are proven here that a flag check cannot: the row lands at
    `docstatus: 1` in one request, it carries no general-ledger entry (which
    is the entire justification for granting the bot submit on this doctype
    and nothing else), and a credential the server refuses leaves nothing
    behind -- not even a draft somebody would later have to find and delete.
    """
    report = provision(client, live_books().bot)
    transaction_id = f"books-live-{uuid.uuid4()}"

    with Client(_submitting(_bot_config(report))) as bot_client:
        posted = bot_client.insert(BANK_TRANSACTION, _mercury_row(bank_account, company, transaction_id))
        name = posted["name"]
        created_transactions.append(name)

        assert posted["docstatus"] == 1, "the bot's submit-on-insert did not commit the row"
        # Read back rather than trusting the insert response: what the site
        # stores is what the Bank Reconciliation Tool will look for.
        stored = bot_client.get(BANK_TRANSACTION, name)
        assert stored["docstatus"] == 1
        # OBSERVED on ERPNext 16 (the disposable test site, 2026-09-21):
        # a freshly submitted Bank Transaction with nothing allocated is
        # "Unreconciled". That is the state the reconciliation tool lists.
        assert stored["status"] == "Unreconciled", f"expected 'Unreconciled', site said {stored['status']!r}"
        assert stored[TRANSACTION_ID_FIELD] == transaction_id
        assert int(stored[RESOURCE_VERSION_FIELD]) == 1
        assert stored[GL_CODE_FIELD] == "5299 - UNCATEGORISED"
        assert stored[NOTE_FIELD] == "posted by books' live access suite"

        # A Bank Transaction posts nothing to the ledger on submit; a person
        # reconciling it to a voucher is what writes the entries. If this ever
        # returns a row, the bot's submit grant is committing money and the
        # policy behind BOT_SUBMITS no longer holds.
        entries = client.list("GL Entry", fields=["name"], filters=[["voucher_no", "=", name]], limit=1)
        assert entries == [], f"a submitted Bank Transaction wrote GL entries: {entries}"

        # Submit without cancel: the bot can commit a statement line and can
        # never take one back. Cheap to check while the row is here.
        with pytest.raises(PermissionError_):
            bot_client.cancel(BANK_TRANSACTION, name)

    # The refused path. The reader's role grants Bank Transaction nothing, so
    # the server refuses this insert outright -- and the row it refused is not
    # sitting on the site as a draft afterwards. Frappe's insert-with-docstatus
    # is one request, which is what makes that true; a client that drafted
    # first and submitted second would leave one behind on every refusal.
    refused_id = f"books-live-refused-{uuid.uuid4()}"
    reader = provision(client, live_books().reader)
    with Client(_submitting(_bot_config(reader))) as reader_client, pytest.raises(PermissionError_):
        reader_client.insert(BANK_TRANSACTION, _mercury_row(bank_account, company, refused_id))

    left_behind = client.list(
        BANK_TRANSACTION,
        fields=["name", "docstatus"],
        filters=[[TRANSACTION_ID_FIELD, "=", refused_id]],
        limit=5,
    )
    assert left_behind == [], f"a refused submit left a row behind: {left_behind}"


def test_provisioned_bot_can_attach_a_file_to_a_submitted_bank_transaction(
    client, company, mercury_fields, bank_account, created_transactions, created_files
) -> None:
    """Whether a Mercury receipt can be attached to the row it belongs to.

    The ingest submits a Bank Transaction on insert, so by the time a receipt
    PDF is in hand the document is already submitted. Frappe refuses most
    writes to a submitted document; an attachment is a separate `File`
    document rather than a field on that one, and whether the server treats
    it as a write to the submitted row is the open design question this
    answers.
    """
    report = provision(client, live_books().bot)
    transaction_id = f"books-live-attach-{uuid.uuid4()}"

    with Client(_submitting(_bot_config(report))) as bot_client:
        posted = bot_client.insert(BANK_TRANSACTION, _mercury_row(bank_account, company, transaction_id))
        name = posted["name"]
        created_transactions.append(name)
        assert posted["docstatus"] == 1, "the row has to be submitted for this to answer anything"

        filename = f"mercury-receipt-{uuid.uuid4().hex[:8]}.pdf"
        uploaded = bot_client.upload_file(BANK_TRANSACTION, name, filename, _minimal_pdf_bytes())
        created_files.append(uploaded["name"])

        assert uploaded["attached_to_doctype"] == BANK_TRANSACTION
        assert uploaded["attached_to_name"] == name
        assert uploaded["file_name"] == filename
        assert bool(uploaded["is_private"]), "a receipt is financial evidence; it must not be world-readable"

        listed = bot_client.list(
            "File",
            fields=["name", "file_name"],
            filters=[["attached_to_doctype", "=", BANK_TRANSACTION], ["attached_to_name", "=", name]],
            limit=5,
        )
        assert [row["name"] for row in listed] == [uploaded["name"]], (
            f"the attachment is not listed against the row it claims: {listed}"
        )


def test_a_second_provision_changes_no_roles(client) -> None:
    provision(client, live_books().bot)
    second = provision(client, live_books().bot)
    assert second.role_created is False
    assert second.revoked == []
    assert second.user.created is False
    assert second.user.added_roles == frozenset()
    assert second.user.removed_roles == frozenset()
