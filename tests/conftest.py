"""Shared Mercury fixtures: the captured payloads, and the mapping the tests post with.

Every test that needs a transaction gets one by parsing a file in
`tests/fixtures/mercury/ingest/`, never by constructing a `Transaction` in the
test body. A hand-built record agrees with whatever the test expects, which is
exactly the property that makes it worthless as evidence about payloads.

The ingest's files sit in their own directory because they are a different
artefact from the ones beside them: these wrap each response in a
`scenario/method/path/status/payload` envelope so a capture run can rewrite
them, while `tests/fixtures/mercury/*.json` are bare response bodies read by
the health report's cases. Two sets of numbers, one client.
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import TYPE_CHECKING, Any

import pytest
import typer.rich_utils
from icedcoffee_sync.frappe.client import SiteConfig

from runningtab import cli
from runningtab.books import BOOKS_ENV, Books, load_books, resolve_books
from runningtab.chart import load_chart
from runningtab.mercury.client import MercuryClient
from runningtab.mercury.mapping import Mapping, parse_mapping
from runningtab.mercury.models import Transaction, parse_transaction
from runningtab.mercury.posting import PostingContext
from runningtab.mercury.vouchers import Chart
from runningtab.site import Target, mercury_config, site_config

if TYPE_CHECKING:
    from runningtab.health import AccountLedger
    from runningtab.period.ledger import GLRow
    from runningtab.period.package import PackageInputs
    from runningtab.period.quarter import FiscalYear, Quarter

# The books every test runs against: a complete books directory with a fake
# ledger host, fake Mercury account ids and fake secret names.
EXAMPLE_BOOKS_DIR = pathlib.Path(__file__).parent.parent / "examples" / "books"
EXAMPLE_BOOKS: Books = load_books(EXAMPLE_BOOKS_DIR)


@pytest.fixture(autouse=True)
def _example_books(monkeypatch):
    """Point every CLI invocation at the example books, the way an operator's RUNNINGTAB_BOOKS would.

    Not under RUNNINGTAB_LIVE: the live suite reaches a real test site, so it
    takes the books directory from the environment exactly as the CLI does
    (`live_books`), and a missing one refuses rather than defaulting.
    """
    if not os.environ.get("RUNNINGTAB_LIVE"):
        monkeypatch.setenv(BOOKS_ENV, str(EXAMPLE_BOOKS_DIR))


@pytest.fixture(autouse=True)
def _plain_cli_output(monkeypatch):
    """Make the CLI's help and errors plain text under CliRunner, wherever the suite runs.

    Typer decides at import time to force a colour terminal when GITHUB_ACTIONS,
    FORCE_COLOR or PY_COLORS is set, and Rich honours FORCE_COLOR itself. Under
    either, escape codes split the words tests look for ("--books" no longer
    appears in the output), so the suite passed on a workstation and failed on
    Actions. None restores Typer's own detection, which finds no terminal here.
    """
    monkeypatch.setattr(typer.rich_utils, "FORCE_TERMINAL", None)
    for name in ("FORCE_COLOR", "PY_COLORS"):
        monkeypatch.delenv(name, raising=False)


def live_books() -> Books:
    """The books the live suite acts on: `RUNNINGTAB_BOOKS`, as the CLI reads it, with no default."""
    return resolve_books(None)


@pytest.fixture(autouse=True)
def _refuse_real_network(monkeypatch):
    """Fail loudly, by name, on any outbound connection this suite makes for real.

    A forgotten `wired()`-style patch -- or a new code path (`closed
    periods`'s `missing_grants_report` opening its own `icedcoffee-sync` `Client`
    rather than the one a test already faked, say) -- used to be invisible
    from pytest's own point of view: the sandbox denies the connection,
    `guarded`/`_CLOSE_REFUSALS` correctly turn the resulting error into a
    report line, and a test asserting only on that line passes either way,
    leaving only a sandbox notice nothing here ever saw. This turns that
    into a named test failure instead of a silent pass: an `httpx2.Client`
    (every `icedcoffee-sync`/Mercury client) whose transport is not a `MockTransport`
    -- every test that means to reach the network at all already builds one
    -- and a real `socket.socket.connect` (the path anything else, Google
    Cloud Storage's own client included, would take) both refuse instead of
    dialing out, naming the host. `RUNNINGTAB_LIVE=1` opts out: the live suite
    (`test_live_*.py`) talks to the real disposable site and the real
    Mercury sandbox on purpose.
    """
    if os.environ.get("RUNNINGTAB_LIVE"):
        yield
        return

    import socket

    import httpx2

    real_send = httpx2.Client.send

    def guarded_send(self, request, *args, **kwargs):
        if type(self._transport).__name__ != "MockTransport":
            raise AssertionError(
                f"real outbound httpx2 request to {request.url} -- RUNNINGTAB_LIVE is not set; "
                "route this test through a fake client (or an httpx2.MockTransport) instead"
            )
        return real_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx2.Client, "send", guarded_send)

    def guarded_connect(self, address, *_args, **_kwargs):
        raise AssertionError(
            f"real outbound socket connection to {address!r} -- RUNNINGTAB_LIVE is not set; "
            "route this test through a fake client instead of a real one"
        )

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    yield


def require_live_target() -> SiteConfig:
    """Skip unless RUNNINGTAB_LIVE is set, then resolve the test target's credential.

    Every live fixture in this directory (`client` in `test_live_access.py`
    and `test_live_coa.py`) calls this before touching the network, so the
    default suite -- `just test`, CI -- never dials out. RUNNINGTAB_TEST_URL
    alone used to gate the live tests, but `site_config` now falls back to
    Secret Manager when it is unset, so that variable stopped being able to
    keep a real network call out of the default suite. A plain function
    rather than a second fixture: pytest fails outright if a test calls a
    `@pytest.fixture`-decorated function directly, and this needs to be
    unit-tested that way in `test_live_gate.py`.
    """
    if not os.environ.get("RUNNINGTAB_LIVE"):
        pytest.skip("RUNNINGTAB_LIVE not set")
    return site_config(live_books().site, Target.TEST)


# The chart account each Mercury account *kind* maps to. The shipped
# mercury.toml maps the REAL Mercury accounts' ids, and the sandbox's ids are
# not those -- posting through it would either refuse ("maps no chart
# account") or, worse, silently land sandbox activity on a real chart account.
# So a live test against --target test never uses mercury.toml directly: it
# reads the sandbox's own account list (the same MercuryClient/read_ledgers
# path the code under test itself reads) and builds a mapping from what it
# finds, by kind, matching the checking/savings/credit-card shape the nine
# period-fixture Journal Entries were written against. See
# tests/fixtures/period/README.md. Shared here, not defined once per live
# test file, because `test_live_period.py` and `test_live_close.py` both need
# it -- a second cross-import between test modules was the signal to move it.
CHART_ACCOUNT_FOR_KIND = {
    "checking": "1111",
    "savings": "1112",
    "credit": "2121",
}


def write_sandbox_mercury_mapping(path: pathlib.Path) -> None:
    """Write a mercury.toml-shaped mapping from the sandbox's live accounts, keyed by kind, to `path`.

    Report-only: prints what it found so a sandbox with more or fewer
    accounts than checking/savings/credit shows up in the calling test's own
    output rather than failing silently on a partial mapping. Written to a
    file and passed through `--mercury-mapping` rather than built as a
    `Mapping` object and monkeypatched in, so the caller exercises the same
    option and `load_mapping(path)` call a person running `--target test`
    for real would use. Requires RUNNINGTAB_LIVE (reads the live Mercury sandbox);
    every caller already went through `require_live_target()` first.
    """
    lines = ["[accounts]"]
    found_kinds: list[str] = []
    with MercuryClient(mercury_config(live_books().site, cli.MERCURY_FOR_TARGET[Target.TEST])) as client:
        for account in client.accounts():
            chart_account = CHART_ACCOUNT_FOR_KIND.get(account.kind or "")
            if chart_account is None:
                continue
            found_kinds.append(f"{account.kind}={account.id}")
            lines.append(f'"{account.id}" = "{chart_account}"')
        for credit in client.credit_accounts():
            found_kinds.append(f"credit={credit.id}")
            lines.append(f'"{credit.id}" = "{CHART_ACCOUNT_FOR_KIND["credit"]}"')
    print(f"sandbox accounts mapped by kind: {', '.join(found_kinds) or '(none found)'}")
    path.write_text("\n".join(lines))


FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "mercury" / "ingest"

# The accounts the fixtures describe, and the transaction file for each. The
# credit account is here on purpose: `/accounts` omits credit accounts, and the
# investigation's first probe forgot them and got a wrong balance for it.
FIXTURE_ACCOUNTS = {
    "acct_chk_1": "transactions_checking",
    "acct_chk_2": "transactions_savings",
    "acct_credit_1": "transactions_credit",
}

# What the tests post with. Deliberately not the shipped `mercury.toml`: a
# suite that tracked the real account ids would go quiet the day they changed.
TEST_MAPPING_TOML = """
[accounts]
"acct_chk_1" = "1111"
"acct_chk_2" = "1112"
"acct_credit_1" = "2121"

[bank_accounts]
"acct_chk_1" = "Mercury Checking - T"
"acct_chk_2" = "Mercury Savings - T"
"acct_credit_1" = "Mercury IO Card - T"
"""

# The chart accounts the tests link Bank Accounts to, as number -> ERPNext
# document name.
ACCOUNT_DOCUMENT_NAMES = {
    "1111": "1111 - Operating Checking - T",
    "1112": "1112 - Reserve Savings - T",
    "2121": "2121 - Business Credit Card - T",
    # The accounts a GL code points a draft voucher at: expenses and one
    # member-capital account, the two families a two-line entry describes.
    "5110": "5110 - Cloud Hosting & Infrastructure - T",
    "5204": "5204 - Office Software & SaaS - T",
    "5210": "5210 - Payroll - Salaries & Wages - T",
    "5215": "5215 - Phone & Internet (Verizon, Comcast) - T",
    "5216": "5216 - Travel / Conferences & Professional Development - T",
    "3110": "3110 - Capital Contribution - Member 1 - T",
    # Card cashback, booked by a person as income: what a payee's history can
    # propose that a Mercury code never drafts.
    "4210": "4210 - Interest Income - T",
}


def load_fixture(name: str) -> dict[str, Any]:
    """One captured response file, whole."""
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text())


def payload(name: str) -> Any:
    """The response body of one captured file."""
    return load_fixture(name)["payload"]


def transactions_from(name: str, account_id: str) -> list[Transaction]:
    """Parsed transactions from one account's captured read."""
    return [parse_transaction(row, account_id) for row in payload(name)["transactions"]]


def all_transactions() -> list[Transaction]:
    """Every transaction across every account the fixtures describe."""
    rows: list[Transaction] = []
    for account_id, fixture in FIXTURE_ACCOUNTS.items():
        rows.extend(transactions_from(fixture, account_id))
    return rows


def by_id(transactions: list[Transaction]) -> dict[str, Transaction]:
    return {t.id: t for t in transactions}


@pytest.fixture
def mercury_mapping() -> Mapping:
    return parse_mapping(TEST_MAPPING_TOML)


@pytest.fixture
def transactions() -> list[Transaction]:
    return all_transactions()


@pytest.fixture
def own_account_ids() -> list[str]:
    return list(FIXTURE_ACCOUNTS)


@pytest.fixture
def posting_ctx(mercury_mapping: Mapping) -> PostingContext:
    return PostingContext(
        company="Transpire",
        account_names=dict(ACCOUNT_DOCUMENT_NAMES),
        mapping=mercury_mapping,
        bot_user=EXAMPLE_BOOKS.bot.user,
        chart=example_chart(),
        mercury_account_names={"acct_chk_1": "Transpire Checking", "acct_chk_2": "Transpire Savings"},
    )


def example_chart() -> Chart:
    """The example books' postable accounts, as the voucher pass reads them."""
    return Chart.load(load_chart(EXAMPLE_BOOKS.chart_path))


# The period builders' tests run on GL Entries captured from the disposable
# test site by tests/capture_period_fixtures.py, never rows built in a test
# body -- same rule as the Mercury fixtures above. See
# tests/fixtures/period/README.md for provenance.
PERIOD_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "period"


def _period_gl() -> dict[str, Any]:
    return json.loads((PERIOD_FIXTURES / "gl.json").read_text(encoding="utf-8"))


PERIOD_ABBR: str = _period_gl()["abbr"]


def period_gl_rows() -> list[GLRow]:
    """The captured GL Entries, parsed. See tests/fixtures/period/README.md."""
    from runningtab.period.ledger import parse_gl_row

    return [parse_gl_row(row) for row in _period_gl()["rows"]]


def period_fiscal_years() -> list[FiscalYear]:
    from runningtab.period.ledger import parse_fiscal_year

    return [parse_fiscal_year(row) for row in json.loads((PERIOD_FIXTURES / "fiscal-years.json").read_text())]


def period_ledgers() -> list[AccountLedger]:
    """The ingest fixtures' accounts, each with its captured balance and transactions.

    Used by both the bank-reconciliation tests and the package-builder tests,
    so a quarter's package is always reconciled against the same captured bank
    data the ingest itself is tested with -- never a hand-built ledger.
    """
    from decimal import Decimal

    from runningtab.health import AccountLedger

    balances = {a["id"]: Decimal(str(a["currentBalance"])) for a in payload("accounts")["accounts"]}
    balances |= {c["id"]: Decimal(str(c["currentBalance"])) for c in payload("credit")["accounts"]}
    return [
        AccountLedger(account_id, account_id, balances[account_id], tuple(transactions_from(name, account_id)))
        for account_id, name in FIXTURE_ACCOUNTS.items()
    ]


def period_inputs(rows: list[GLRow] | None = None, quarter: Quarter | None = None) -> PackageInputs:
    """A `PackageInputs` built entirely from committed fixtures: the captured GL, fiscal years, and bank ledgers.

    Defaults to 2026Q3, the quarter the fixtures were captured around. Every
    field comes from a fixture file or a fixed test constant, never a value
    invented in a test body.
    """
    from runningtab.period.package import PackageInputs
    from runningtab.period.qbo_map import load_qbo_map
    from runningtab.period.quarter import Quarter, fiscal_year_for

    resolved_quarter = quarter or Quarter.parse("2026Q3")
    return PackageInputs(
        quarter=resolved_quarter,
        fiscal_year=fiscal_year_for(resolved_quarter, period_fiscal_years()),
        company="Transpire",
        site_url="https://test-ledger.example",
        chart=tuple(load_chart(EXAMPLE_BOOKS.chart_path)),
        abbr=PERIOD_ABBR,
        rows=tuple(period_gl_rows() if rows is None else rows),
        ledgers=tuple(period_ledgers()),
        mapping=parse_mapping(TEST_MAPPING_TOML),
        qmap=load_qbo_map(EXAMPLE_BOOKS.qbo_path),
        documents={"documents/Journal Entry/JV-1/f1-r.pdf": b"%PDF"},
    )


class FakeRecord:
    """An in-memory `runningtab.period.record.Record`. `create` raises `PeriodError` on an existing name, like the real
    bucket's `if_generation_match=0` precondition. Shared by `test_period_record.py` (the record module's own
    tests) and `test_period_close.py` (which runs the real `next_prefix`/`upload_package` against one, rather
    than asserting whatever prefix a fake `upload` step was told to parrot back).
    """

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects: dict[str, bytes] = dict(objects or {})
        self.create_order: list[str] = []

    def list_names(self, prefix: str) -> list[str]:
        return [name for name in self.objects if name.startswith(prefix)]

    def read(self, name: str) -> bytes:
        return self.objects[name]

    def create(self, name: str, data: bytes) -> None:
        from runningtab.period import PeriodError

        if name in self.objects:
            raise PeriodError(f"{name!r} already exists; refusing to overwrite")
        self.objects[name] = data
        self.create_order.append(name)
