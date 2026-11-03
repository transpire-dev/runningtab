"""`runningtab period export`, `runningtab period close`, and `runningtab period status`: what they write, what they
refuse, and how they exit."""

import dataclasses
import datetime as dt
import hashlib
import json
import pathlib
import re

import httpx2
import pytest
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import DefaultCredentialsError, RefreshError
from icedcoffee_sync.apply.diff import ChartPlan, CreateAccount
from icedcoffee_sync.apply.model import DesiredAccount
from icedcoffee_sync.frappe.client import SiteConfig
from icedcoffee_sync.frappe.errors import PermissionError_
from typer.testing import CliRunner

from runningtab import cli
from runningtab.chart import load_chart, load_defaults
from runningtab.mercury.mapping import parse_mapping
from runningtab.period.close import Held
from runningtab.period.fence import LOCK_FIELDS
from runningtab.period.lock import LockChange
from runningtab.period.package import build_package as real_build_package
from runningtab.period.package import sha256sums
from runningtab.period.quarter import Quarter
from runningtab.period.record import SuspectPackage
from tests.conftest import (
    EXAMPLE_BOOKS,
    PERIOD_ABBR,
    TEST_MAPPING_TOML,
    FakeRecord,
    period_fiscal_years,
    period_gl_rows,
    period_inputs,
    period_ledgers,
)

runner = CliRunner()


def _no_secret_manager(monkeypatch) -> None:
    """Proves an export test never reaches Secret Manager.

    `_period_reads` used to build its own `site_config(target, readonly=True)`
    internally; the refactor moved that call out to `period_export`, so a
    test that only patches `cli._period_reads` now lets the real
    `site_config(Target.TEST, readonly=True)` run and, with `RUNNINGTAB_TEST_URL`
    unset, dial out to Secret Manager for the stored test-site credential.
    Patching `cli.site_config` to the in-memory fake is the fix; the two
    tripwires below back it up by failing outright if the real credential
    fetch is ever reached anyway, so a future regression that bypasses
    `cli.site_config` (rather than merely forgetting to patch it here) is
    still caught instead of silently dialing out again.
    """

    def _tripwire(*args, **kwargs):
        raise AssertionError("Secret Manager should not be reached by this test")

    monkeypatch.setattr(cli, "site_config", _fake_site_config)
    monkeypatch.setattr("runningtab.site._fetch_test_credential", _tripwire)
    monkeypatch.setattr("runningtab.site._secret_payload", _tripwire)


def _wire(monkeypatch, rows) -> None:
    """Patch `cli._period_reads`, `cli.site_config`, and `cli.load_mapping` to answer entirely from committed
    fixtures, never the network.

    The site read itself is proven live in Task 14; these tests prove the
    command's own decisions -- parsing, building, writing, and the exit code.
    """
    _no_secret_manager(monkeypatch)
    reads = cli.PeriodReads(
        site_url="https://test-ledger.example",
        company="Transpire",
        abbr=PERIOD_ABBR,
        fiscal_years=period_fiscal_years(),
        rows=rows,
        ledgers=period_ledgers(),
        documents={},
    )
    monkeypatch.setattr(
        cli,
        "_period_reads",
        lambda books, target, company, quarter, chart, defaults, config, *, check_reader_grants: reads,
    )
    monkeypatch.setattr(cli, "load_mapping", lambda path=None: parse_mapping(TEST_MAPPING_TOML))


@pytest.fixture
def fake_period_site(monkeypatch):
    _wire(monkeypatch, period_gl_rows())


@pytest.fixture
def fake_period_site_with_stray_account(monkeypatch):
    rows = list(period_gl_rows())
    stray = dataclasses.replace(rows[0], account="9999 - Made In The UI - X")
    _wire(monkeypatch, [*rows, stray])


def test_a_bad_quarter_is_a_usage_error(tmp_path):
    result = runner.invoke(
        cli.app, ["period", "export", "--target", "test", "--quarter", "2026q3", "--output", str(tmp_path / "x")]
    )
    assert result.exit_code == 2
    assert "YYYYQn" in result.output


def test_year_zero_is_a_usage_error_not_a_bare_valueerror(tmp_path):
    # "0000Q1" matches the regex (\d{4} accepts "0000") and only failed later,
    # with a bare ValueError from dt.date, the first time something touched
    # .start or .end.
    result = runner.invoke(
        cli.app, ["period", "export", "--target", "test", "--quarter", "0000Q1", "--output", str(tmp_path / "x")]
    )
    assert result.exit_code == 2
    assert "YYYYQn" in result.output


def test_a_bank_difference_writes_the_draft_but_exits_one(tmp_path, fake_period_site):
    # The captured GL and the captured Mercury rows describe different money,
    # so the reconciliation disagrees: exactly the case a draft exists for.
    out = tmp_path / "2026Q3-DRAFT"
    result = runner.invoke(
        cli.app, ["period", "export", "--target", "test", "--quarter", "2026Q3", "--output", str(out)]
    )
    assert result.exit_code == 1
    assert (out / "MANIFEST.md").read_text().startswith("# Example books — 2026Q3 — DRAFT")
    assert (out / "SHA256SUMS").exists()
    assert "difference" in result.output


def test_chart_coa_is_read_once_and_the_same_parse_used_for_the_plan_check_package_and_manifest(tmp_path, monkeypatch):
    # coa.plan_against's live-chart check, build_package, and the manifest's
    # SHA-256 used to each read (and, for the first two, re-parse) chart.coa
    # separately -- an edit landing between those reads could make one see
    # different content than another. Proves the fix: exactly one read, and
    # the exact same parsed DesiredAccount objects (not merely equal ones
    # from a second parse) reused for the plan check and the package.
    _no_secret_manager(monkeypatch)
    read_calls = 0
    real_read_bytes = pathlib.Path.read_bytes

    def counting_read_bytes(self: pathlib.Path) -> bytes:
        nonlocal read_calls
        if self == EXAMPLE_BOOKS.chart_path:
            read_calls += 1
        return real_read_bytes(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", counting_read_bytes)

    plan_chart: list[DesiredAccount] = []
    package_chart: tuple[DesiredAccount, ...] = ()

    def spying_period_reads(books, target, company, quarter, chart, defaults, config, *, check_reader_grants):
        plan_chart.extend(chart)
        return cli.PeriodReads(
            site_url="https://test-ledger.example",
            company="Transpire",
            abbr=PERIOD_ABBR,
            fiscal_years=period_fiscal_years(),
            rows=period_gl_rows(),
            ledgers=period_ledgers(),
            documents={},
        )

    monkeypatch.setattr(cli, "_period_reads", spying_period_reads)
    monkeypatch.setattr(cli, "load_mapping", lambda path=None: parse_mapping(TEST_MAPPING_TOML))

    real_build_package = cli.build_package

    def spying_build_package(inputs):
        nonlocal package_chart
        package_chart = inputs.chart
        return real_build_package(inputs)

    monkeypatch.setattr(cli, "build_package", spying_build_package)

    out = tmp_path / "2026Q3-DRAFT"
    runner.invoke(cli.app, ["period", "export", "--target", "test", "--quarter", "2026Q3", "--output", str(out)])

    assert read_calls == 1
    assert len(plan_chart) == len(package_chart)
    assert all(a is b for a, b in zip(plan_chart, package_chart, strict=True))

    real_bytes = EXAMPLE_BOOKS.chart_path.read_bytes()
    expected_hash = hashlib.sha256(real_bytes).hexdigest()
    manifest = (out / "MANIFEST.md").read_text()
    assert f"SHA-256 `{expected_hash}`" in manifest


def test_mercury_mapping_option_is_the_file_the_export_actually_uses(tmp_path, monkeypatch):
    # The shipped mercury.toml names only the real Mercury accounts, none of
    # which are the fixture's ids -- against it, ALL THREE fixture accounts
    # would refuse as unmapped. This mapping deliberately maps only two of
    # the three (acct_chk_2 left out): if the export used the shipped file
    # instead of --mercury-mapping's, the refusal would name all three, not
    # exactly the one this test omitted.
    _no_secret_manager(monkeypatch)
    reads = cli.PeriodReads(
        site_url="https://test-ledger.example",
        company="Transpire",
        abbr=PERIOD_ABBR,
        fiscal_years=period_fiscal_years(),
        rows=period_gl_rows(),
        ledgers=period_ledgers(),
        documents={},
    )
    monkeypatch.setattr(
        cli,
        "_period_reads",
        lambda books, target, company, quarter, chart, defaults, config, *, check_reader_grants: reads,
    )
    mapping_path = tmp_path / "mercury.toml"
    mapping_path.write_text('[accounts]\n"acct_chk_1" = "1111"\n"acct_credit_1" = "2121"\n')
    out = tmp_path / "2026Q3-DRAFT"
    result = runner.invoke(
        cli.app,
        [
            "period",
            "export",
            "--target",
            "test",
            "--quarter",
            "2026Q3",
            "--output",
            str(out),
            "--mercury-mapping",
            str(mapping_path),
        ],
    )
    assert result.exit_code == 1
    assert not out.exists()
    # Exactly acct_chk_2 named -- not all three, which is what the shipped
    # mercury.toml (none of these ids) would have produced instead.
    assert "mercury.toml maps no chart account for Mercury account(s): acct_chk_2" in result.output


def test_the_exit_code_is_zero_only_when_every_bank_line_agrees():
    package = cli.build_package(period_inputs())  # the Task 12 test helper, moved to conftest
    assert cli.export_exit_code(package) == 1
    agreed = dataclasses.replace(
        package, bank=tuple(dataclasses.replace(line, ledger=line.bank) for line in package.bank)
    )
    assert cli.export_exit_code(agreed) == 0


def test_a_refusal_writes_nothing(tmp_path, fake_period_site_with_stray_account):
    out = tmp_path / "2026Q3-DRAFT"
    result = runner.invoke(
        cli.app, ["period", "export", "--target", "test", "--quarter", "2026Q3", "--output", str(out)]
    )
    assert result.exit_code == 1
    assert not out.exists()
    assert "chart.coa does not describe" in result.output


def test_a_permission_error_is_reported_with_the_reprovision_hint(tmp_path, monkeypatch):
    # Exactly what happens if someone forgets to re-provision the reader after
    # this merges: the server refuses a doctype the role does not hold yet.
    _no_secret_manager(monkeypatch)

    def _raise(books, target, company, quarter, chart, defaults, config, *, check_reader_grants):
        raise PermissionError_("No permission for Fiscal Year")

    monkeypatch.setattr(cli, "_period_reads", _raise)
    out = tmp_path / "2026Q3-DRAFT"
    result = runner.invoke(
        cli.app, ["period", "export", "--target", "test", "--quarter", "2026Q3", "--output", str(out)]
    )
    assert result.exit_code == 1
    assert not out.exists()
    assert "refused: No permission for Fiscal Year" in result.output
    assert "runningtab access provision --target test --profile reader" in result.output


def _fake_site_config(site, target, readonly=False):
    return SiteConfig(url="https://x.test", api_key="k", api_secret="s")  # noqa: S106


def test_a_reader_credential_missing_a_grant_is_refused_before_any_read(tmp_path, monkeypatch):
    # READER_GRANTS describes what the reader is SUPPOSED to hold; a reader
    # never (re-)provisioned after a grant was added still passes that
    # static check. The server-side missing_grants_report is what actually
    # proves the credential in hand holds it.
    monkeypatch.setattr(cli, "site_config", _fake_site_config)
    monkeypatch.setattr(cli, "missing_grants_report", lambda config, policy: {"Fiscal Year": ["read"]})
    out = tmp_path / "2026Q3-DRAFT"
    result = runner.invoke(
        cli.app, ["period", "export", "--target", "test", "--quarter", "2026Q3", "--output", str(out)]
    )
    assert result.exit_code == 1
    assert not out.exists()
    assert "Fiscal Year" in result.output
    assert "read" in result.output
    assert "runningtab access provision --target test --profile reader" in result.output


def test_an_empty_missing_grants_report_proceeds_past_the_check(monkeypatch):
    # Proves the check itself does not block a credential that holds
    # everything: it must let execution reach the site read, not merely
    # avoid raising in isolation.
    class _ReachedTheSiteRead(Exception):
        pass

    class _RaisingClient:
        def __init__(self, config):
            pass

        def __enter__(self):
            raise _ReachedTheSiteRead

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(cli, "site_config", _fake_site_config)
    monkeypatch.setattr(cli, "missing_grants_report", lambda config, policy: {})
    monkeypatch.setattr(
        "runningtab.cli.coa.plan_against",
        lambda config, company, *_: _plan_context(
            ChartPlan(creates=[], renames=[], retypes=[], extras=[], refusals=[])
        ),
    )
    monkeypatch.setattr(cli, "Client", _RaisingClient)

    config = _fake_site_config(EXAMPLE_BOOKS.site, cli.Target.TEST, readonly=True)
    with pytest.raises(_ReachedTheSiteRead):
        cli._period_reads(
            EXAMPLE_BOOKS,
            cli.Target.TEST,
            None,
            Quarter.parse("2026Q3"),
            load_chart(EXAMPLE_BOOKS.chart_path),
            load_defaults(EXAMPLE_BOOKS.chart_path),
            config,
            check_reader_grants=True,
        )


class _FakePlanClient:
    """Only `close()`: `_period_reads` closes the chart-plan's own client once it has the plan."""

    def close(self) -> None:
        pass


def _plan_context(plan: ChartPlan) -> cli._Context:
    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    return cli._Context(
        client=_FakePlanClient(),  # ty: ignore[invalid-argument-type]
        company="Transpire",
        abbr="T",
        chart=chart,
        by_number={a.number: a for a in chart},
        plan=plan,
        live=[],
        with_entries=set(),
        defaults={},
    )


def test_a_chart_that_has_drifted_from_chart_coa_is_refused_before_any_read(tmp_path, monkeypatch):
    # The export classifies every GL row from chart.coa without ever
    # checking the live site: an account the chart wants created but the
    # site does not have yet still lets every statement here balance,
    # wrongly, against a chart the site does not actually carry.
    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    by_number = {a.number: a for a in chart}
    plan = ChartPlan(creates=[CreateAccount(by_number["5299"])], renames=[], retypes=[], extras=[], refusals=[])
    monkeypatch.setattr(cli, "site_config", _fake_site_config)
    monkeypatch.setattr(cli, "missing_grants_report", lambda config, policy: {})
    monkeypatch.setattr("runningtab.cli.coa.plan_against", lambda config, company, *_: _plan_context(plan))
    out = tmp_path / "2026Q3-DRAFT"
    result = runner.invoke(
        cli.app, ["period", "export", "--target", "test", "--quarter", "2026Q3", "--output", str(out)]
    )
    assert result.exit_code == 1
    assert not out.exists()
    assert "5299" in result.output
    assert "runningtab coa plan --target test" in result.output
    assert "runningtab coa apply" in result.output


def test_an_empty_chart_plan_proceeds_past_the_check(monkeypatch):
    # Proves the check itself does not block a chart that already matches:
    # it must let execution reach the site read, not merely avoid raising
    # in isolation.
    class _ReachedTheSiteRead(Exception):
        pass

    class _RaisingClient:
        def __init__(self, config):
            pass

        def __enter__(self):
            raise _ReachedTheSiteRead

        def __exit__(self, *args):
            return False

    empty_plan = ChartPlan(creates=[], renames=[], retypes=[], extras=[], refusals=[])
    monkeypatch.setattr(cli, "site_config", _fake_site_config)
    monkeypatch.setattr(cli, "missing_grants_report", lambda config, policy: {})
    monkeypatch.setattr("runningtab.cli.coa.plan_against", lambda config, company, *_: _plan_context(empty_plan))
    monkeypatch.setattr(cli, "Client", _RaisingClient)

    config = _fake_site_config(EXAMPLE_BOOKS.site, cli.Target.TEST, readonly=True)
    with pytest.raises(_ReachedTheSiteRead):
        cli._period_reads(
            EXAMPLE_BOOKS,
            cli.Target.TEST,
            None,
            Quarter.parse("2026Q3"),
            load_chart(EXAMPLE_BOOKS.chart_path),
            load_defaults(EXAMPLE_BOOKS.chart_path),
            config,
            check_reader_grants=True,
        )


def test_an_output_whose_parent_is_a_file_is_refused_not_crashed(tmp_path, fake_period_site):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    out = blocker / "2026Q3-DRAFT"
    result = runner.invoke(
        cli.app, ["period", "export", "--target", "test", "--quarter", "2026Q3", "--output", str(out)]
    )
    assert result.exit_code == 1
    assert "refused:" in result.output
    assert not out.exists()


def test_a_transport_failure_is_reported_not_crashed(tmp_path, monkeypatch):
    _no_secret_manager(monkeypatch)

    def _raise(books, target, company, quarter, chart, defaults, config, *, check_reader_grants):
        raise httpx2.ConnectError("mercury hung up")

    monkeypatch.setattr(cli, "_period_reads", _raise)
    out = tmp_path / "2026Q3-DRAFT"
    result = runner.invoke(
        cli.app, ["period", "export", "--target", "test", "--quarter", "2026Q3", "--output", str(out)]
    )
    assert result.exit_code == 1
    assert not out.exists()
    assert "refused: mercury hung up" in result.output


# --- period close -----------------------------------------------------------


class _FakeAdminClient:
    """A minimal fake `icedcoffee_sync.frappe.client.Client` for `period close`'s own tests, and for `period status`'s.

    Stateful only where `runningtab.period.lock` needs it: `.get`/`.update` on
    Company, and `.iter_all` on Version -- enough for `read_lock` and the
    real `set_closed_through`/`lock_history` to run unmocked and prove the
    lock actually moves and leaves a Version behind. `.list` answers
    `company_abbr`'s Company lookup (`period status` needs it; `period
    close`'s own tests monkeypatch `cli.company_abbr` away instead, so they
    never reach it). `read_blockers` and `_period_reads` are monkeypatched
    away in these tests, so nothing here answers a GL Entry, Bank
    Transaction, or query-report call; `.call` raises if it is asked for
    anyway, as a tripwire.
    """

    def __init__(self, config):
        self.config = config
        self.company_doc: dict[str, object] = {LOCK_FIELDS[0]: None, LOCK_FIELDS[1]: None}
        self.versions: list[dict] = []
        self._version_counter = 0
        # Set by a test to model a reader never (re-)provisioned since the
        # permlevel-1 grant on these two fields was added: Frappe deletes a
        # permlevel-1 field the credential cannot read from the response
        # rather than returning it as null, so `.get` omits the key(s)
        # named here entirely -- not merely returns None for them.
        self.hidden_lock_fields: set[str] = set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, doctype, name):
        if doctype == "Company":
            return {k: v for k, v in self.company_doc.items() if k not in self.hidden_lock_fields}
        raise AssertionError(f"unexpected get({doctype!r}, {name!r})")

    def list(self, doctype, **kwargs):
        if doctype == "Company":
            wanted = next(value for field, _op, value in kwargs["filters"] if field == "name")
            return [{"name": wanted, "abbr": PERIOD_ABBR}] if wanted == "Transpire" else []
        raise AssertionError(f"unexpected list({doctype!r})")

    def update(self, doctype, name, doc):
        assert doctype == "Company"
        old = self.company_doc.get(LOCK_FIELDS[0]) or ""
        new = doc.get(LOCK_FIELDS[0], self.company_doc.get(LOCK_FIELDS[0]))
        self.company_doc.update(doc)
        self._version_counter += 1
        self.versions.insert(
            0,
            {
                "name": f"VER-{self._version_counter}",
                "owner": "gavin@carothers.name",
                "creation": "2026-10-05 12:00:00.000000",
                "data": json.dumps({"changed": [[LOCK_FIELDS[0], old, new]]}),
            },
        )

    def iter_all(self, doctype, **kwargs):
        if doctype == "Version":
            return iter(self.versions)
        raise AssertionError(f"unexpected iter_all({doctype!r})")

    def call(self, dotted_path, params=None):
        raise AssertionError(f"unexpected call({dotted_path!r})")


def _fake_admin_config(site, target):
    return SiteConfig(url="https://x.test", api_key="k", api_secret="s")  # noqa: S106


def test_close_refuses_before_any_request_when_admin_token_is_unset(monkeypatch):
    # admin_config(Target.BOOKS) reads RUNNINGTAB_ADMIN_TOKEN alone, before
    # anything else opens a connection -- these tripwires fail the test if
    # either the ERPNext client or the record bucket is ever reached.
    monkeypatch.delenv("RUNNINGTAB_ADMIN_TOKEN", raising=False)

    class _NoRequestsClient:
        def __init__(self, config):
            raise AssertionError("no ERPNext request should have been made: RUNNINGTAB_ADMIN_TOKEN is unset")

    def _no_bucket(bucket):
        raise AssertionError("no bucket request should have been made: RUNNINGTAB_ADMIN_TOKEN is unset")

    monkeypatch.setattr(cli, "Client", _NoRequestsClient)
    monkeypatch.setattr(cli, "GcsRecord", _no_bucket)

    result = runner.invoke(
        cli.app, ["period", "close", "--target", "books", "--quarter", "2026Q3", "--company", "Transpire"]
    )

    assert result.exit_code == 1
    assert "refused:" in result.output
    assert "RUNNINGTAB_ADMIN_TOKEN" in result.output


def test_a_close_quarter_refusal_exits_one_naming_it(monkeypatch):
    # A quarter that has not ended is the cheapest way to reach a real
    # `close_quarter` refusal: it fires in the ceremony's very first stage,
    # before `read_blockers`, `build`, or the lock are ever touched.
    monkeypatch.setattr(cli, "admin_config", _fake_admin_config)
    monkeypatch.setattr(cli, "Client", _FakeAdminClient)
    monkeypatch.setattr(cli, "company_abbr", lambda client, company: PERIOD_ABBR)
    monkeypatch.setattr(cli, "GcsRecord", lambda bucket: FakeRecord())

    result = runner.invoke(
        cli.app, ["period", "close", "--target", "test", "--quarter", "2099Q1", "--company", "Transpire"]
    )

    assert result.exit_code == 1
    assert "refused:" in result.output
    assert "2099Q1" in result.output
    assert "has not ended" in result.output


def _canned_close_reads() -> cli.PeriodReads:
    return cli.PeriodReads(
        site_url="https://test-ledger.example",
        company="Transpire",
        abbr=PERIOD_ABBR,
        fiscal_years=period_fiscal_years(),
        rows=period_gl_rows(),
        ledgers=period_ledgers(),
        documents={},
    )


def _reconciled_build_package(inputs):
    """The real `build_package`, with every bank line's difference zeroed -- the fixtures do not reconcile
    by default (see `tests.conftest.period_inputs`), and readiness refuses a close over a bank difference.
    """
    package = real_build_package(inputs)
    zeroed = tuple(dataclasses.replace(line, ledger=line.bank) for line in package.bank)
    return dataclasses.replace(package, bank=zeroed)


QUARTER_END = "2026-09-30"  # Quarter.parse("2026Q3").end.isoformat()


def _wire_close_success(monkeypatch, *, reads: cli.PeriodReads | None = None) -> list[_FakeAdminClient]:
    """Common wiring for a close that reaches (or nearly reaches) the lock, with the real `set_closed_through`
    and `read_lock` run unmocked against a real `_FakeAdminClient` -- crosscheck and readiness have their own
    test suites, so those two alone are stubbed. Returns the list every `Client(config)` this run makes is
    appended to, so a test can inspect the fake Company's final state after the run.
    """
    reads = reads or _canned_close_reads()
    created_clients: list[_FakeAdminClient] = []

    def _make_client(config: SiteConfig) -> _FakeAdminClient:
        client = _FakeAdminClient(config)
        created_clients.append(client)
        return client

    monkeypatch.setattr(cli, "admin_config", _fake_admin_config)
    monkeypatch.setattr(cli, "Client", _make_client)
    monkeypatch.setattr(cli, "company_abbr", lambda client, company: PERIOD_ABBR)
    monkeypatch.setattr(cli, "bind_crosscheck", lambda client, company, index: lambda *args: [])
    monkeypatch.setattr(cli, "read_gl", lambda client, company, through: [])
    monkeypatch.setattr(cli, "read_blockers", lambda client, company, quarter, suspense, rows: [])
    monkeypatch.setattr(
        cli,
        "_period_reads",
        lambda books, target, company, quarter, chart, defaults, config, *, check_reader_grants: reads,
    )
    monkeypatch.setattr(cli, "load_mapping", lambda path=None: parse_mapping(TEST_MAPPING_TOML))
    monkeypatch.setattr(cli, "build_package", _reconciled_build_package)
    monkeypatch.setattr(cli, "_today", lambda: dt.date(2026, 10, 5))
    return created_clients


def test_a_successful_close_stores_a_manifest_whose_revision_matches_the_written_prefix(monkeypatch):
    created_clients = _wire_close_success(monkeypatch)
    records: dict[str, FakeRecord] = {}

    def _record_for(bucket: str) -> FakeRecord:
        # `--target test` must land in the test bucket, never the real one:
        # if `period_close` ever passed the wrong bucket, this would be the
        # only thing in the test that could catch it.
        assert bucket == EXAMPLE_BOOKS.record_buckets[cli.Target.TEST]
        # Seed revision 1 as already stored, so this close is forced onto
        # revision 2 -- a real check of `revision_of` against something
        # other than the trivial, always-1 default.
        record = records.setdefault(bucket, FakeRecord())
        record.objects[f"{PERIOD_ABBR}/2026Q3/SHA256SUMS"] = b"seed"
        return record

    monkeypatch.setattr(cli, "GcsRecord", _record_for)

    result = runner.invoke(
        cli.app, ["period", "close", "--target", "test", "--quarter", "2026Q3", "--company", "Transpire"]
    )

    assert result.exit_code == 0, result.output
    (record,) = records.values()
    prefix = f"{PERIOD_ABBR}/2026Q3/rev2/"
    assert f"stored at {prefix}" in result.output
    assert (prefix + "MANIFEST.md") in record.objects

    manifest = record.objects[prefix + "MANIFEST.md"].decode()
    match = re.search(r"\*\*Revision:\*\* (\d+)", manifest)
    assert match is not None
    assert match[1] == "2"

    # The lock actually moved, not just the printed line claiming it did.
    (client,) = created_clients
    assert client.company_doc[LOCK_FIELDS[0]] == QUARTER_END
    assert "lock: Company Version VER-1, by gavin@carothers.name" in result.output

    content_count = len([name for name in record.create_order if name.startswith(prefix)]) - 2  # - MANIFEST/SHA256SUMS
    assert f"files: {content_count}" in result.output


def test_a_failure_after_the_lock_moves_says_so_and_how_to_recover(monkeypatch):
    # The lock and readiness/build/crosscheck stages all succeed -- only the
    # final upload fails, the same way a GCS 503 would. `refused:` alone
    # would read as "nothing happened", but Books Closed Through already
    # moved and nothing was stored.
    created_clients = _wire_close_success(monkeypatch)
    monkeypatch.setattr(cli, "GcsRecord", lambda bucket: FakeRecord())

    def _raise_upload(record, prefix, files):
        raise GoogleAPIError("503 Service Unavailable")

    monkeypatch.setattr(cli, "upload_package", _raise_upload)

    result = runner.invoke(
        cli.app, ["period", "close", "--target", "test", "--quarter", "2026Q3", "--company", "Transpire"]
    )

    assert result.exit_code == 1
    (client,) = created_clients
    assert client.company_doc[LOCK_FIELDS[0]] == QUARTER_END
    assert (
        f"Books Closed Through is now {QUARTER_END} (Company Version VER-1, by gavin@carothers.name); "
        "the package may not have been stored -- run `runningtab period status` first. If it shows 2026Q3 locked "
        "with no stored package, run `runningtab period close` again for 2026Q3 to store it."
    ) in result.output
    assert "refused: 503 Service Unavailable" in result.output


def test_a_failure_after_the_lock_on_a_reclose_says_reopen_and_close_again(monkeypatch):
    # 2026Q3 already has rev1 stored, so this close is a re-close. The
    # upload fails after the lock moved; a plain re-run would be refused as
    # "already has a stored package", so the next step is reopen-and-close.
    _wire_close_success(monkeypatch)
    monkeypatch.setattr(cli, "GcsRecord", lambda bucket: FakeRecord({f"{PERIOD_ABBR}/2026Q3/SHA256SUMS": b"seed"}))

    def _raise_upload(record, prefix, files):
        raise GoogleAPIError("503 Service Unavailable")

    monkeypatch.setattr(cli, "upload_package", _raise_upload)

    result = runner.invoke(
        cli.app, ["period", "close", "--target", "test", "--quarter", "2026Q3", "--company", "Transpire"]
    )

    assert result.exit_code == 1
    assert (
        f"Books Closed Through is now {QUARTER_END} (Company Version VER-1, by gavin@carothers.name); "
        "the new revision may not have been stored -- run `runningtab period status` first. If it still shows "
        "2026Q3's earlier revision, reopen 2026Q3 (move Books Closed Through back to 2026-06-30 in "
        "ERPNext) and run `runningtab period close` again for 2026Q3. A re-run as it stands is refused because "
        "2026Q3 already has a stored package."
    ) in result.output
    assert "to store it." not in result.output


def test_a_failed_reclose_of_an_earlier_quarter_names_every_later_quarter_to_reclose(capsys):
    # Reopening Q2 again leaves Q3 open too, so the next step names Q3 after it.
    held = Held(
        LockChange(
            version="VER-2",
            at=dt.datetime(2026, 10, 5),
            by="gavin@carothers.name",
            old=None,
            new=dt.date(2026, 6, 30),
            old_text="",
            new_text="2026-06-30",
        ),
        reclosing=True,
        later=(Quarter.parse("2026Q3"),),
    )

    cli._report_held_lock(held, Quarter.parse("2026Q2"), GoogleAPIError("503"), refusal=True)

    err = capsys.readouterr().err
    assert "run `runningtab period close` again for 2026Q2, then re-close 2026Q3." in err
    assert "run `runningtab period status` first" in err


def _drifting_build_package(monkeypatch) -> None:
    """Replace `cli.build_package` so the *second* call (the ceremony's own rebuild-and-compare) returns a
    package with an extra general-ledger row -- simulating a live ledger change between the check and the
    lock, the one case `close_quarter` raises `HeldForReview` for rather than an ordinary `PeriodError`.
    """
    calls = 0

    def _build(inputs):
        nonlocal calls
        calls += 1
        package = _reconciled_build_package(inputs)
        if calls > 1:
            changed = dict(package.files)
            changed["general-ledger.csv"] = changed["general-ledger.csv"] + b"\nextra,row\n"
            package = dataclasses.replace(package, files=changed)
        return package

    monkeypatch.setattr(cli, "build_package", _build)


def test_a_rebuild_mismatch_names_the_lock_and_its_own_next_step(monkeypatch):
    # close_quarter's own rebuild-mismatch message names the changed files
    # and the next step -- re-run `close` to re-check and store what is now
    # locked, or reopen first -- so the CLI prints the lock line beside it
    # and no generic retry sentence of its own.
    created_clients = _wire_close_success(monkeypatch)
    _drifting_build_package(monkeypatch)
    monkeypatch.setattr(cli, "GcsRecord", lambda bucket: FakeRecord())

    result = runner.invoke(
        cli.app, ["period", "close", "--target", "test", "--quarter", "2026Q3", "--company", "Transpire"]
    )

    assert result.exit_code == 1
    (client,) = created_clients
    # The lock did move -- this is not the pre-lock case.
    assert client.company_doc[LOCK_FIELDS[0]] == QUARTER_END
    assert (
        f"Books Closed Through is now {QUARTER_END} (Company Version VER-1, by gavin@carothers.name); "
        "the quarter stays locked."
    ) in result.output
    assert f"stays locked through {QUARTER_END} with no new stored package" in result.output
    assert "general-ledger.csv" in result.output
    assert (
        "run `runningtab period close --quarter 2026Q3` again to re-check and store what is now locked" in result.output
    )
    assert "the package may not have been stored" not in result.output


def _client_already_locked(created: list[_FakeAdminClient]):
    """A `cli.Client` factory whose Company already reads Books Closed Through as the quarter's end, recorded
    in a Version by the real `.update` -- a close that locked and then failed to store.
    """

    def _make(config: SiteConfig) -> _FakeAdminClient:
        client = _FakeAdminClient(config)
        client.update("Company", "Transpire", {LOCK_FIELDS[0]: QUARTER_END})
        created.append(client)
        return client

    return _make


def test_a_recovery_run_whose_store_fails_still_says_the_quarter_is_locked(monkeypatch):
    # This run did not move the lock -- a previous one did -- but the
    # quarter is just as locked, and a bare `refused:` would read as
    # "nothing happened".
    _wire_close_success(monkeypatch)
    created: list[_FakeAdminClient] = []
    monkeypatch.setattr(cli, "Client", _client_already_locked(created))
    monkeypatch.setattr(cli, "GcsRecord", lambda bucket: FakeRecord())

    def _raise_upload(record, prefix, files):
        raise GoogleAPIError("503 Service Unavailable")

    monkeypatch.setattr(cli, "upload_package", _raise_upload)

    result = runner.invoke(
        cli.app, ["period", "close", "--target", "test", "--quarter", "2026Q3", "--company", "Transpire"]
    )

    assert result.exit_code == 1
    (client,) = created
    assert [v["name"] for v in client.versions] == ["VER-1"]  # no second lock edit
    assert (
        f"Books Closed Through is now {QUARTER_END} (Company Version VER-1, by gavin@carothers.name); "
        "the package may not have been stored -- run `runningtab period status` first. If it shows 2026Q3 locked "
        "with no stored package, run `runningtab period close` again for 2026Q3 to store it."
    ) in result.output
    assert "refused: 503 Service Unavailable" in result.output


def test_an_unexpected_error_after_the_lock_prints_the_lock_line_then_raises(monkeypatch):
    # Not one of the named refusals, so it stays a traceback -- but the
    # person still learns the quarter is locked before it.
    _wire_close_success(monkeypatch)
    monkeypatch.setattr(cli, "GcsRecord", lambda bucket: FakeRecord())

    def _raise_upload(record, prefix, files):
        raise ValueError("Invalid isoformat string: 'garbage'")

    monkeypatch.setattr(cli, "upload_package", _raise_upload)

    result = runner.invoke(
        cli.app, ["period", "close", "--target", "test", "--quarter", "2026Q3", "--company", "Transpire"]
    )

    assert isinstance(result.exception, ValueError)
    assert (
        f"Books Closed Through is now {QUARTER_END} (Company Version VER-1, by gavin@carothers.name); "
        "the quarter stays locked."
    ) in result.output
    assert "refused:" not in result.output


def test_a_suspect_stored_package_points_at_status_not_a_retry(monkeypatch):
    # SHA256SUMS was written but read back wrong: the package is already
    # listed as complete, so a retry would only be refused as "already has
    # a stored package".
    _wire_close_success(monkeypatch)
    monkeypatch.setattr(cli, "GcsRecord", lambda bucket: FakeRecord())

    def _raise_upload(record, prefix, files):
        raise SuspectPackage(f"{prefix}SHA256SUMS did not read back intact; treat {prefix!r} as suspect")

    monkeypatch.setattr(cli, "upload_package", _raise_upload)

    result = runner.invoke(
        cli.app, ["period", "close", "--target", "test", "--quarter", "2026Q3", "--company", "Transpire"]
    )

    assert result.exit_code == 1
    assert f"Books Closed Through is now {QUARTER_END}" in result.output
    assert "the quarter stays locked." in result.output
    assert "run `runningtab period close` again" not in result.output
    assert "as suspect" in result.output


def test_a_missing_adc_credential_refuses_before_the_lock(monkeypatch):
    # google.auth.exceptions.GoogleAuthError is not a google.api_core
    # GoogleAPIError, so without its own catch this crashes instead of
    # refusing -- checked here where it is cheapest to trigger: constructing
    # `GcsRecord` before the lock is ever touched.
    monkeypatch.setattr(cli, "admin_config", _fake_admin_config)

    def _raise_record(bucket):
        raise DefaultCredentialsError("could not automatically determine credentials")

    monkeypatch.setattr(cli, "GcsRecord", _raise_record)

    result = runner.invoke(
        cli.app, ["period", "close", "--target", "test", "--quarter", "2026Q3", "--company", "Transpire"]
    )

    assert result.exit_code == 1
    assert "refused: could not automatically determine credentials" in result.output
    # Nothing moved -- this must not claim the lock did.
    assert "Books Closed Through is now" not in result.output


def test_an_expired_adc_token_during_upload_refuses_and_says_the_lock_already_moved(monkeypatch):
    # The other GoogleAuthError case: a token that dies mid-command, after
    # the lock has already moved -- both the auth-error catch (finding 3)
    # and the post-lock wording (finding 2) have to hold together here.
    created_clients = _wire_close_success(monkeypatch)
    monkeypatch.setattr(cli, "GcsRecord", lambda bucket: FakeRecord())

    def _raise_upload(record, prefix, files):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(cli, "upload_package", _raise_upload)

    result = runner.invoke(
        cli.app, ["period", "close", "--target", "test", "--quarter", "2026Q3", "--company", "Transpire"]
    )

    assert result.exit_code == 1
    (client,) = created_clients
    assert client.company_doc[LOCK_FIELDS[0]] == QUARTER_END
    assert f"Books Closed Through is now {QUARTER_END}" in result.output
    assert "refused: invalid_grant: Token has been expired or revoked." in result.output


# --- period status -----------------------------------------------------------

STATUS_QUARTER = Quarter.parse("2026Q3")


def _wire_status(monkeypatch, *, reads: cli.PeriodReads | None = None) -> None:
    """Common wiring for `period status` tests: reads as the reader (`_fake_site_config`, an unmocked
    `_FakeAdminClient` so `read_lock`/`lock_history`/`company_abbr` all run for real against a fake site),
    with `_period_reads` and `build_package` canned/zeroed the same way `_wire_close_success` wires `period
    close`'s tests -- crosscheck and readiness have their own suites, so `period status`'s tests do not need
    to re-prove them.
    """
    reads = reads or _canned_close_reads()
    monkeypatch.setattr(cli, "site_config", _fake_site_config)
    monkeypatch.setattr(cli, "missing_grants_report", lambda config, policy: {})
    monkeypatch.setattr(cli, "Client", _FakeAdminClient)
    monkeypatch.setattr(
        cli,
        "_period_reads",
        lambda books, target, company, quarter, chart, defaults, config, *, check_reader_grants: reads,
    )
    monkeypatch.setattr(cli, "load_mapping", lambda path=None: parse_mapping(TEST_MAPPING_TOML))
    monkeypatch.setattr(cli, "build_package", _reconciled_build_package)


def _status_package():
    # `_canned_close_reads()` (what `_wire_status` hands `period_status`'s
    # rebuild through the patched `_period_reads`) carries no documents,
    # unlike `period_inputs()`'s own default -- matched here so the "expected"
    # package these tests seed the record with is built from the exact same
    # inputs the rebuild itself sees.
    return _reconciled_build_package(dataclasses.replace(period_inputs(), documents={}))


def _client_closed_through(date_iso: str):
    """A `cli.Client` factory whose `_FakeAdminClient` already shows Books Closed Through as `date_iso`,
    without going through `set_closed_through`'s own Version-writing machinery -- `period status` only ever
    reads the lock, never moves it.
    """

    def _make(config: SiteConfig) -> _FakeAdminClient:
        client = _FakeAdminClient(config)
        client.company_doc[LOCK_FIELDS[0]] = date_iso
        return client

    return _make


def test_status_refuses_before_any_read_when_the_reader_is_missing_a_grant(monkeypatch):
    monkeypatch.setattr(cli, "site_config", _fake_site_config)
    monkeypatch.setattr(cli, "missing_grants_report", lambda config, policy: {"Version": ["read"]})

    result = runner.invoke(cli.app, ["period", "status", "--target", "test", "--company", "Transpire"])

    assert result.exit_code == 1
    assert "refused:" in result.output
    assert "Version (read)" in result.output
    assert "runningtab access provision --target test --profile reader" in result.output


def test_a_reader_that_cannot_see_the_lock_fields_is_refused_not_confidently_wrong(monkeypatch):
    # `missing_grants_report` explicitly skips permlevel > 0 grants, so it
    # cannot catch a reader never (re-)provisioned since Books Closed
    # Through/the frozen-entries role moved to permlevel 1 -- Frappe just
    # deletes those two keys from the Company response. Without I3's fix
    # this would read as "nothing closed yet" (silently hiding a live
    # bypass role) or a false reopen, never a refusal.
    _wire_status(monkeypatch)
    monkeypatch.setattr(cli, "GcsRecord", lambda bucket: FakeRecord())

    def _make(config: SiteConfig) -> _FakeAdminClient:
        client = _FakeAdminClient(config)
        client.hidden_lock_fields = {LOCK_FIELDS[0], LOCK_FIELDS[1]}
        return client

    monkeypatch.setattr(cli, "Client", _make)

    result = runner.invoke(cli.app, ["period", "status", "--target", "test", "--company", "Transpire"])

    assert result.exit_code == 1
    assert "refused:" in result.output
    assert "nothing closed yet" not in result.output
    assert "Books Closed Through" in result.output
    assert "the frozen-entries role" in result.output
    assert "runningtab access provision --profile reader" in result.output


def test_nothing_closed_exits_zero(monkeypatch):
    _wire_status(monkeypatch)
    monkeypatch.setattr(cli, "GcsRecord", lambda bucket: FakeRecord())

    result = runner.invoke(cli.app, ["period", "status", "--target", "test", "--company", "Transpire"])

    assert result.exit_code == 0, result.output
    assert "Books Closed Through: not set" in result.output
    assert "nothing closed yet" in result.output


def test_a_locked_quarter_with_no_stored_package_exits_one_naming_it(monkeypatch):
    _wire_status(monkeypatch)
    monkeypatch.setattr(cli, "GcsRecord", lambda bucket: FakeRecord())
    monkeypatch.setattr(cli, "Client", _client_closed_through(STATUS_QUARTER.end.isoformat()))

    result = runner.invoke(cli.app, ["period", "status", "--target", "test", "--company", "Transpire"])

    assert result.exit_code == 1
    assert (
        f"problem: Books Closed Through is {STATUS_QUARTER.end.isoformat()} but {STATUS_QUARTER.label} has no "
        f"stored package; run `runningtab period close --quarter {STATUS_QUARTER.label}` to store it"
    ) in result.output


def _client_closed_once(config: SiteConfig) -> _FakeAdminClient:
    """A fake site whose one Version (VER-1) moved Books Closed Through forward to 2026Q3's end -- what a close
    leaves behind, written through the same `.update` a real lock move goes through."""
    client = _FakeAdminClient(config)
    client.update("Company", "Transpire", {LOCK_FIELDS[0]: STATUS_QUARTER.end.isoformat()})
    return client


def test_a_stored_quarter_that_still_matches_exits_zero(monkeypatch):
    _wire_status(monkeypatch)
    record = _record_closed_by("VER-1")
    monkeypatch.setattr(cli, "GcsRecord", lambda bucket: record)
    monkeypatch.setattr(cli, "Client", _client_closed_once)

    result = runner.invoke(cli.app, ["period", "status", "--target", "test", "--company", "Transpire"])

    assert result.exit_code == 0, result.output
    assert f"Books Closed Through: {STATUS_QUARTER.end.isoformat()}" in result.output
    assert f"{STATUS_QUARTER.label} rev1: ok" in result.output


def _client_closed_reopened_and_relocked(config: SiteConfig) -> _FakeAdminClient:
    """A fake site whose Version history shows 2026Q3 closed (VER-1), reopened (VER-2), and locked again by a
    re-close (VER-3) -- written through the same `.update` a real lock move goes through."""
    client = _FakeAdminClient(config)
    client.update("Company", "Transpire", {LOCK_FIELDS[0]: STATUS_QUARTER.end.isoformat()})
    client.update("Company", "Transpire", {LOCK_FIELDS[0]: STATUS_QUARTER.previous.end.isoformat()})
    client.update("Company", "Transpire", {LOCK_FIELDS[0]: STATUS_QUARTER.end.isoformat()})
    return client


def _record_closed_by(version: str) -> FakeRecord:
    package = _status_package()
    record = FakeRecord()
    prefix = f"{PERIOD_ABBR}/{STATUS_QUARTER.label}/"
    record.objects[f"{prefix}SHA256SUMS"] = sha256sums(package.files)
    record.objects[f"{prefix}MANIFEST.md"] = (
        f"# Example books — {STATUS_QUARTER.label}\n\n- **Lock record:** Company Version `{version}`\n"
    ).encode()
    return record


def test_a_reclose_that_locked_but_stored_nothing_exits_one(monkeypatch):
    # rev1 was stored by the close VER-1 made; the re-close VER-3 made never
    # stored. Every file still matches rev1, so only the lock record shows it.
    _wire_status(monkeypatch)
    record = _record_closed_by("VER-1")
    monkeypatch.setattr(cli, "GcsRecord", lambda bucket: record)
    monkeypatch.setattr(cli, "Client", _client_closed_reopened_and_relocked)

    result = runner.invoke(cli.app, ["period", "status", "--target", "test", "--company", "Transpire"])

    assert result.exit_code == 1
    assert f"{STATUS_QUARTER.label} rev1: ok" in result.output
    assert "Company Version VER-3" in result.output
    assert f"{STATUS_QUARTER.label} rev1 records Company Version VER-1" in result.output


def test_a_stored_close_that_matches_the_lock_record_exits_zero(monkeypatch):
    _wire_status(monkeypatch)
    record = _record_closed_by("VER-3")
    monkeypatch.setattr(cli, "GcsRecord", lambda bucket: record)
    monkeypatch.setattr(cli, "Client", _client_closed_reopened_and_relocked)

    result = runner.invoke(cli.app, ["period", "status", "--target", "test", "--company", "Transpire"])

    assert result.exit_code == 0, result.output


def test_a_malformed_stored_sha256sums_refuses_by_name_not_a_traceback(monkeypatch):
    _wire_status(monkeypatch)
    record = FakeRecord()
    record.objects[f"{PERIOD_ABBR}/{STATUS_QUARTER.label}/SHA256SUMS"] = b"not-the-right-shape\n"
    monkeypatch.setattr(cli, "GcsRecord", lambda bucket: record)
    monkeypatch.setattr(cli, "Client", _client_closed_through(STATUS_QUARTER.end.isoformat()))

    result = runner.invoke(cli.app, ["period", "status", "--target", "test", "--company", "Transpire"])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert f"{PERIOD_ABBR}/{STATUS_QUARTER.label}/SHA256SUMS" in result.output


def test_a_changed_file_exits_one_and_names_it(monkeypatch):
    _wire_status(monkeypatch)
    package = _status_package()
    sums = {path: hashlib.sha256(data).hexdigest() for path, data in package.files.items()}
    tampered_path = sorted(sums)[0]
    sums[tampered_path] = "0" * 64
    tampered_sha256sums = "".join(f"{sha}  {path}\n" for path, sha in sorted(sums.items())).encode()
    record = FakeRecord()
    record.objects[f"{PERIOD_ABBR}/{STATUS_QUARTER.label}/SHA256SUMS"] = tampered_sha256sums
    monkeypatch.setattr(cli, "GcsRecord", lambda bucket: record)
    monkeypatch.setattr(cli, "Client", _client_closed_through(STATUS_QUARTER.end.isoformat()))

    result = runner.invoke(cli.app, ["period", "status", "--target", "test", "--company", "Transpire"])

    assert result.exit_code == 1
    assert f"{STATUS_QUARTER.label} rev1: changed {tampered_path}" in result.output
    assert f"problem: {STATUS_QUARTER.label} rev1" in result.output


def test_a_bypass_role_exits_one(monkeypatch):
    _wire_status(monkeypatch)
    monkeypatch.setattr(cli, "GcsRecord", lambda bucket: FakeRecord())

    def _make(config: SiteConfig) -> _FakeAdminClient:
        client = _FakeAdminClient(config)
        client.company_doc[LOCK_FIELDS[1]] = "Accounts Manager"
        return client

    monkeypatch.setattr(cli, "Client", _make)

    result = runner.invoke(cli.app, ["period", "status", "--target", "test", "--company", "Transpire"])

    assert result.exit_code == 1
    assert "Accounts Manager" in result.output


def test_a_reopened_quarter_exits_one_and_names_who_and_when(monkeypatch):
    _wire_status(monkeypatch)
    package = _status_package()
    record = FakeRecord()
    record.objects[f"{PERIOD_ABBR}/{STATUS_QUARTER.label}/SHA256SUMS"] = sha256sums(package.files)
    monkeypatch.setattr(cli, "GcsRecord", lambda bucket: record)

    def _make(config: SiteConfig) -> _FakeAdminClient:
        client = _FakeAdminClient(config)
        # A reopen recorded the way ERPNext's own Version history would: the
        # lock moved from the quarter's end back to an earlier date, by a
        # named person, through the real `.update` (not hand-inserted), so
        # `lock_history`'s own decoding runs unmocked too.
        client.company_doc[LOCK_FIELDS[0]] = STATUS_QUARTER.end.isoformat()
        client.update("Company", "Transpire", {LOCK_FIELDS[0]: dt.date(2026, 6, 30).isoformat()})
        return client

    monkeypatch.setattr(cli, "Client", _make)

    result = runner.invoke(cli.app, ["period", "status", "--target", "test", "--company", "Transpire"])

    assert result.exit_code == 1
    assert "2026-06-30" in result.output
    assert "gavin@carothers.name" in result.output


def test_a_reopen_that_clears_the_lock_still_names_who_and_when(monkeypatch):
    # The obvious way to "unlock" in the ERPNext UI is to empty Books Closed
    # Through entirely, not type in an earlier date.
    _wire_status(monkeypatch)
    package = _status_package()
    record = FakeRecord()
    record.objects[f"{PERIOD_ABBR}/{STATUS_QUARTER.label}/SHA256SUMS"] = sha256sums(package.files)
    monkeypatch.setattr(cli, "GcsRecord", lambda bucket: record)

    def _make(config: SiteConfig) -> _FakeAdminClient:
        client = _FakeAdminClient(config)
        client.company_doc[LOCK_FIELDS[0]] = STATUS_QUARTER.end.isoformat()
        client.update("Company", "Transpire", {LOCK_FIELDS[0]: ""})
        return client

    monkeypatch.setattr(cli, "Client", _make)

    result = runner.invoke(cli.app, ["period", "status", "--target", "test", "--company", "Transpire"])

    assert result.exit_code == 1
    assert "no Company Version records who moved it" not in result.output
    assert "gavin@carothers.name" in result.output
    assert "cleared" in result.output
