"""`runningtab health`: five named sections every time, and an exit code that means something.

The Mercury half runs the real client's parsing and the real invariant over
the committed fixtures; only the transport is replaced. The ERPNext half runs
the real company/abbr resolution and the real suspense read against a site
double, the same shape `test_cli.py` uses.
"""

import datetime as dt
from typing import Any

import httpx2
import pytest
from typer.testing import CliRunner

from runningtab import cli
from runningtab.cli import app
from runningtab.mercury.client import BASE_URLS, MercuryClient, MercuryConfig, MercuryEnv
from runningtab.period.fence import LOCK_FIELDS
from tests.conftest import EXAMPLE_BOOKS, FakeRecord
from tests.test_mercury import fixture, parse_accounts, parse_credit_accounts, parse_transactions

runner = CliRunner()

CHECKING = "5f1b2a4c-9d6e-4c2f-8a10-3b7d5e9f1c02"
SAVINGS = "7c3e8d21-4f5a-4b6c-9e0d-1a2b3c4d5e6f"
CREDIT = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"

SUSPENSE = "5299 - UNCATEGORISED (needs review) - VT"

# The report's clock. Fixed, because the fixture transactions are dated early
# 2026 and the uncoded-spend check ages them: on this day the oldest uncoded
# fixture row is 10 days old, inside the 14-day limit.
CLOCK = dt.datetime(2026, 2, 11, 23, 59, tzinfo=dt.UTC)


class FakeMercury:
    """Mercury's four read endpoints, answered from the fixtures."""

    def __init__(self, checking_fixture: str = "transactions-checking") -> None:
        self.rows = {
            CHECKING: parse_transactions(fixture(checking_fixture)),
            SAVINGS: parse_transactions(fixture("transactions-savings")),
            CREDIT: parse_transactions(fixture("transactions-credit")),
        }
        self.asked: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def accounts(self):
        return parse_accounts(fixture("accounts"))

    def credit_accounts(self):
        return parse_credit_accounts(fixture("credit"))

    def account_transactions(self, account_id: str, *, start: dt.datetime | None = None):
        self.asked.append(account_id)
        # `start` is the account's createdAt: without it the endpoint answers
        # for the last 30 days, so a caller that forgot it would read a
        # different question's answer and blame the bank for the difference.
        assert start is not None, f"{account_id} was read with no start date"
        return self.rows[account_id]


# See test_health.py: a `list` method shadows the builtin inside the class
# body, so the annotations it needs are aliased at module level.
_Rows = list[dict[str, Any]]
_Names = list[str]


class FakeSite:
    """A Frappe site with one company, a suspense account holding `gl_rows`, and a close lock.

    `closed_through`/`bypass_role`/`versions` answer `read_lock`/`lock_history`
    for the `closed periods` section -- unset by default, so a test that never
    touches them gets `runningtab period status`'s "nothing closed yet" case, the
    same way `gl_rows` defaults to an empty suspense account.
    """

    def __init__(
        self,
        gl_rows: _Rows | None = None,
        *,
        account: str | None = SUSPENSE,
        companies: _Names | None = None,
        closed_through: str | None = None,
        bypass_role: str | None = None,
        versions: _Rows | None = None,
        hidden_lock_fields: set[str] | None = None,
    ) -> None:
        self.gl_rows: _Rows = gl_rows or []
        self.account = account
        self.companies = ["Valley Tool Co"] if companies is None else companies
        self.closed_through = closed_through
        self.bypass_role = bypass_role
        self.versions: _Rows = versions or []
        # Set to model a reader never (re-)provisioned since Books Closed
        # Through/the frozen-entries role moved to Company permlevel 1:
        # Frappe deletes a permlevel-1 field the credential cannot read from
        # the response entirely, rather than returning it as null.
        self.hidden_lock_fields: set[str] = hidden_lock_fields or set()

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def close(self) -> None:
        return None

    def list(self, doctype: str, **kwargs: Any) -> Any:
        if doctype == "Company":
            wanted = next(value for field, _op, value in kwargs["filters"] if field == "name")
            return [{"name": wanted, "abbr": "VT"}] if wanted in self.companies else []
        if doctype == "Account":
            wanted = next(value for field, _op, value in kwargs["filters"] if field == "name")
            return [{"name": self.account}] if self.account == wanted else []
        raise AssertionError(f"unexpected list({doctype!r})")

    def get(self, doctype: str, name: str) -> Any:
        if doctype == "Company":
            doc = {LOCK_FIELDS[0]: self.closed_through, LOCK_FIELDS[1]: self.bypass_role}
            return {k: v for k, v in doc.items() if k not in self.hidden_lock_fields}
        raise AssertionError(f"unexpected get({doctype!r}, {name!r})")

    def iter_all(self, doctype: str, **kwargs: Any) -> Any:
        if doctype == "Company":
            return iter([{"name": name} for name in self.companies])
        if doctype == "GL Entry":
            return iter(self.gl_rows)
        if doctype == "Version":
            return iter(self.versions)
        raise AssertionError(f"unexpected iter_all({doctype!r})")


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Point the command at a fake bank and a fake site; return both for assertions.

    Also points the ingest's state directory at an empty temporary one, so no
    test ever reads a real cursor off the machine it runs on.
    """

    def wire(mercury: FakeMercury | None = None, site: FakeSite | None = None) -> tuple[FakeMercury, FakeSite]:
        mercury = mercury or FakeMercury()
        site = site or FakeSite()
        monkeypatch.setenv("RUNNINGTAB_TEST_URL", "https://test-ledger.example")
        monkeypatch.setenv("RUNNINGTAB_TEST_KEY", "key")
        monkeypatch.setenv("RUNNINGTAB_TEST_SECRET", "secret")
        monkeypatch.setenv("MERCURY_SANDBOX_TOKEN", "secret-token:sandbox")
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.setattr(cli, "_now", lambda: CLOCK)
        monkeypatch.setattr("runningtab.cli.MercuryClient", lambda config: mercury)
        monkeypatch.setattr("runningtab.cli.Client", lambda config: site)
        # `closed periods` reads through the reader's effective-grant check
        # and the record bucket, neither of which this fixture's fake site or
        # fake Mercury stand in for -- proven-effective grants and the bucket
        # are `missing_grants_report`'s and `GcsRecord`'s own concerns
        # (test_period_cli.py, test_period_record.py), not this module's.
        monkeypatch.setattr("runningtab.cli.missing_grants_report", lambda config, policy: {})
        monkeypatch.setattr("runningtab.cli.GcsRecord", lambda bucket: FakeRecord())
        return mercury, site

    return wire


def gl_row(posting_date: str, debit: float, credit: float, remarks: str = "") -> dict[str, Any]:
    return {
        "posting_date": posting_date,
        "debit": debit,
        "credit": credit,
        "voucher_type": "Journal Entry",
        "voucher_no": "JE-0001",
        "remarks": remarks,
        "creation": f"{posting_date} 09:00:00",
    }


def test_every_section_is_reported_in_order(wired) -> None:
    wired()

    result = runner.invoke(app, ["health", "--target", "test"])

    titles = [
        "balance invariant",
        "uncoded spend, aged",
        "suspense contents",
        "webhook subscription health",
        "event cursor age",
        "closed periods",
    ]
    positions = [result.output.index(title) for title in titles]
    assert positions == sorted(positions), result.output


def test_a_clean_ledger_and_an_empty_suspense_account_exit_zero(wired) -> None:
    wired()

    result = runner.invoke(app, ["health", "--target", "test"])

    assert result.exit_code == 0, result.output
    assert "3 accounts reconcile exactly" in result.output
    assert "nothing closed yet" in result.output
    assert "4 passed, 0 failed, 0 could not run, 2 not available" in result.output


def test_the_credit_account_is_read_as_well_as_the_deposit_accounts(wired) -> None:
    mercury, _ = wired()

    runner.invoke(app, ["health", "--target", "test"])

    # /accounts omits credit accounts, so reading only it would leave the
    # card unchecked while the report still said everything reconciles.
    assert set(mercury.asked) == {CHECKING, SAVINGS, CREDIT}


def test_the_unavailable_checks_do_not_fail_the_command_by_default(wired) -> None:
    # The webhook check, and the cursor check on a machine with no ingest state.
    wired()

    result = runner.invoke(app, ["health", "--target", "test"])

    assert result.output.count("not available: needs the Mercury ingest") == 2
    assert result.exit_code == 0


def test_strict_fails_on_the_unavailable_checks(wired) -> None:
    wired()

    result = runner.invoke(app, ["health", "--target", "test", "--strict"])

    assert result.exit_code == 1, result.output


def test_money_in_suspense_fails_the_command(wired) -> None:
    wired(site=FakeSite([gl_row("2026-03-10", 125.0, 0.0, "raw category: Shipping & Freight")]))

    result = runner.invoke(app, ["health", "--target", "test"])

    assert result.exit_code == 1
    assert "125.00 sitting in 5299 - UNCATEGORISED (needs review) - VT" in result.output
    assert "Shipping & Freight" in result.output


def test_an_out_of_balance_account_fails_the_command(wired) -> None:
    wired(mercury=FakeMercury("transactions-checking-unpaged"))

    result = runner.invoke(app, ["health", "--target", "test"])

    assert result.exit_code == 1
    assert "off by 2,000.00" in result.output


def test_a_missing_mercury_token_is_reported_not_crashed(wired, monkeypatch) -> None:
    # `wired()` patches `runningtab.cli.Client` for the ERPNext side, but
    # `closed periods`' own `missing_grants_report(config, READER_GRANTS)` call
    # opens its own `icedcoffee-sync` client, not `cli.Client` -- so this test used
    # to leave that one unpatched, and the resulting real (sandbox-denied)
    # request read as a plain `ConnectError`, indistinguishable from the
    # Mercury-token failure this test means to prove. Asserting the
    # closed-periods row too is what catches that: a fake that stops
    # covering it now fails here instead of passing on a swallowed ERROR.
    wired()
    monkeypatch.delenv("MERCURY_SANDBOX_TOKEN", raising=False)

    result = runner.invoke(app, ["health", "--target", "test"])

    # An unset token is not a passing check: the report cannot say whether the
    # books are current without the arithmetic. It says what to set instead of
    # handing back a traceback, and the other sections still run.
    assert "could not run: MERCURY_SANDBOX_TOKEN is not set" in result.output
    assert result.exit_code == 1
    assert "suspense contents" in result.output
    assert "nothing closed yet" in result.output


def test_a_missing_site_credential_is_reported_not_crashed(monkeypatch) -> None:
    # Nothing exported and the stored key unreadable: the target has no
    # credential left to try, and that is a section that says so.
    monkeypatch.delenv("RUNNINGTAB_TEST_URL", raising=False)
    monkeypatch.setenv("MERCURY_SANDBOX_TOKEN", "secret-token:sandbox")
    monkeypatch.setattr("runningtab.cli.MercuryClient", lambda config: FakeMercury())

    def _refused(secret_id):
        raise PermissionError("403 caller lacks secretmanager.versions.access")

    monkeypatch.setattr("runningtab.site._secret_payload", _refused)

    result = runner.invoke(app, ["health", "--target", "test"])

    assert f"could not run: could not read secret {EXAMPLE_BOOKS.site.test_admin_secret}" in result.output
    assert result.exit_code == 1
    # The half that could run still reports, which is the point of gathering
    # every section before deciding the exit code.
    assert "3 accounts reconcile exactly" in result.output


def test_a_mercury_that_will_not_answer_fails_the_command(wired, monkeypatch) -> None:
    # A configured token whose service is unreachable. Like an unset token it
    # fails the command; unlike one, the message is the transport's, so the
    # reader can tell "nobody set this up" from "the bank is not answering".
    wired()

    def unreachable(config):
        raise httpx2.ConnectError("mercury hung up")

    monkeypatch.setattr("runningtab.cli.MercuryClient", unreachable)

    result = runner.invoke(app, ["health", "--target", "test"])

    assert result.exit_code == 1, result.output
    assert "could not run: mercury hung up" in result.output


def test_a_test_url_outside_the_allowlist_is_reported_not_crashed(wired, monkeypatch) -> None:
    # A misconfigured RUNNINGTAB_TEST_URL is a credential the command cannot use,
    # the same class of problem as one that is unset. The bank half already
    # ran; losing it to a traceback throws away the half that worked.
    wired()
    monkeypatch.setenv("RUNNINGTAB_TEST_URL", "https://ledger-alias.example")

    result = runner.invoke(app, ["health", "--target", "test"])

    assert result.exit_code == 1, result.output
    assert "could not run:" in result.output
    assert "test_hosts" in result.output
    assert "3 accounts reconcile exactly" in result.output


def test_a_mercury_response_it_cannot_read_is_reported_not_crashed(wired, monkeypatch) -> None:
    # A 200 carrying something other than the documented shape -- a proxy's
    # error page, a truncated body. It makes one section ERROR; the other four
    # still print, because a report that stops halfway answers nothing.
    wired()

    def unreadable(config):
        return MercuryClient(
            MercuryConfig(base_url=BASE_URLS[MercuryEnv.SANDBOX], token="secret-token:test"),  # noqa: S106 -- a test string
            transport=httpx2.MockTransport(lambda request: httpx2.Response(200, text="<html>gateway timeout</html>")),
        )

    monkeypatch.setattr("runningtab.cli.MercuryClient", unreadable)

    result = runner.invoke(app, ["health", "--target", "test"])

    assert result.exit_code == 1, result.output
    assert "could not run:" in result.output
    assert "/accounts" in result.output
    # Both checks that read the bank say so; neither pretends it passed.
    assert result.output.count("could not run:") == 2
    assert result.output.count("not available: needs the Mercury ingest") == 2
    assert "suspense contents" in result.output


def test_a_suspense_account_the_site_does_not_have_is_an_error(wired) -> None:
    wired(site=FakeSite(account=None))

    result = runner.invoke(app, ["health", "--target", "test"])

    assert result.exit_code == 1
    assert "is not on this site" in result.output
    assert "runningtab coa apply" in result.output


def test_a_site_with_no_company_is_reported_not_crashed(wired) -> None:
    # `sole_company` refuses to guess, and that refusal must arrive as a
    # report line: the balance invariant already ran, and losing its result to
    # a traceback wastes the half of the command that worked.
    wired(site=FakeSite(companies=[]))

    result = runner.invoke(app, ["health", "--target", "test"])

    assert result.exit_code == 1
    assert "could not run: --company is required" in result.output
    assert "3 accounts reconcile exactly" in result.output


def test_an_unknown_company_is_reported_not_crashed(wired) -> None:
    wired()

    result = runner.invoke(app, ["health", "--target", "test", "--company", "Nobody Ltd"])

    assert result.exit_code == 1
    # LookupError is one of the two generic types `guarded` prefixes with
    # its own type name (M3): a bare `KeyError`'s text alone would be
    # useless, and this proves the well-named `company_abbr` case still
    # reads clearly with the prefix added.
    assert "could not run: LookupError: no Company named 'Nobody Ltd'" in result.output


def test_a_bypass_role_fails_the_command_through_closed_periods(wired) -> None:
    wired(site=FakeSite(bypass_role="Accounts Manager"))

    result = runner.invoke(app, ["health", "--target", "test"])

    assert result.exit_code == 1
    assert "closed periods" in result.output
    assert "Accounts Manager" in result.output
    # The half that already ran is not lost to the one section failing.
    assert "3 accounts reconcile exactly" in result.output


def test_a_locked_quarter_with_no_stored_package_fails_closed_periods(wired) -> None:
    # The quarter is frozen in ERPNext and nothing is in the record bucket:
    # the silent state a close that locked and then failed to store leaves.
    wired(site=FakeSite(closed_through="2026-09-30"))

    result = runner.invoke(app, ["health", "--target", "test"])

    assert result.exit_code == 1
    assert "closed periods" in result.output
    assert "nothing closed yet" not in result.output
    assert "2026Q3 has no stored package" in result.output


def test_a_reader_missing_a_grant_fails_closed_periods_not_crashed(wired, monkeypatch) -> None:
    wired()
    monkeypatch.setattr("runningtab.cli.missing_grants_report", lambda config, policy: {"Version": ["read"]})

    result = runner.invoke(app, ["health", "--target", "test"])

    assert result.exit_code == 1
    assert "closed periods" in result.output
    assert "could not run: the reader credential does not hold: Version (read)" in result.output
    assert "3 accounts reconcile exactly" in result.output


def test_a_reader_blind_to_the_lock_fields_is_refused_not_confidently_wrong(wired) -> None:
    # `missing_grants_report` skips permlevel > 0 grants entirely, so it
    # cannot catch this: Frappe just deletes the two permlevel-1 fields from
    # the Company response a reader never (re-)provisioned for them gets.
    # Without I3's fix this reads as a clean "nothing closed yet" PASS,
    # silently hiding a live bypass role.
    wired(site=FakeSite(bypass_role="Accounts Manager", hidden_lock_fields={LOCK_FIELDS[0], LOCK_FIELDS[1]}))

    result = runner.invoke(app, ["health", "--target", "test"])

    assert result.exit_code == 1
    assert "closed periods" in result.output
    assert "nothing closed yet" not in result.output
    assert "could not run:" in result.output
    assert "Books Closed Through" in result.output
    assert "3 accounts reconcile exactly" in result.output


def test_health_accepts_mercury_mapping_and_passes_it_to_closed_periods(wired, monkeypatch, tmp_path) -> None:
    # `closed periods` rebuilds through the same path `period export`/`period
    # status` use, and both of those need `--mercury-mapping` for `--target
    # test` (the shipped mercury.toml names only the real Mercury accounts).
    # Without this option, `health --target test` could never pass once a
    # test quarter is stored.
    wired()
    seen: list[object] = []
    real = cli._period_status_report

    def spy(books, target, company, mercury_mapping=None):
        seen.append(mercury_mapping)
        return real(books, target, company, mercury_mapping)

    monkeypatch.setattr(cli, "_period_status_report", spy)
    mapping_path = tmp_path / "mercury.toml"
    mapping_path.write_text("[accounts]\n")

    result = runner.invoke(app, ["health", "--target", "test", "--mercury-mapping", str(mapping_path)])

    assert result.exit_code == 0, result.output
    assert seen == [mapping_path]


def test_health_has_no_default_target() -> None:
    result = runner.invoke(app, ["health"])

    assert result.exit_code != 0
    assert "--target" in result.output


def test_uncoded_spend_past_the_limit_fails_the_command(wired, monkeypatch) -> None:
    wired()
    monkeypatch.setattr(cli, "_now", lambda: CLOCK + dt.timedelta(days=48))

    result = runner.invoke(app, ["health", "--target", "test"])

    assert result.exit_code == 1, result.output
    assert "2 of 3 uncoded rows are older than 14 days" in result.output


def test_the_bank_is_read_once_for_both_checks_that_need_it(wired) -> None:
    mercury, _ = wired()

    runner.invoke(app, ["health", "--target", "test"])

    assert sorted(mercury.asked) == sorted([CHECKING, SAVINGS, CREDIT])


def test_no_ingest_state_on_this_machine_is_not_available_and_names_where_it_looked(wired, tmp_path) -> None:
    wired()

    result = runner.invoke(app, ["health", "--target", "test"])

    from runningtab.mercury.state import default_state_path

    expected = default_state_path(EXAMPLE_BOOKS.name, "sandbox", "test")
    assert expected.is_relative_to(tmp_path)
    assert "event cursor age" in result.output
    assert "no ingest state" in result.output
    assert expected.name in result.output


def test_the_cursor_age_comes_from_the_ingest_state(wired, tmp_path) -> None:
    from runningtab.mercury.state import IngestState, default_state_path

    wired()
    path = default_state_path(EXAMPLE_BOOKS.name, "sandbox", "test")
    path.parent.mkdir(parents=True)
    with IngestState(path, identity="sandbox/test") as state:
        state.record_cursor("evt_1", CLOCK - dt.timedelta(days=9))

    result = runner.invoke(app, ["health", "--target", "test"])

    assert result.exit_code == 1, result.output
    assert "last synced 9 days ago, more than 7" in result.output
