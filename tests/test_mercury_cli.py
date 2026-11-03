"""The `runningtab mercury` commands: what they refuse, and what they report."""

from __future__ import annotations

import datetime as dt
import pathlib
from decimal import Decimal

import httpx2
import pytest
import typer
from icedcoffee_sync.frappe.client import Client, SiteConfig
from typer.testing import CliRunner

from runningtab import cli
from runningtab.cli import _print_report, _refuse_sandbox_into_the_ledger, app
from runningtab.mercury.client import MercuryEnv
from runningtab.mercury.ingest import BalanceCheck, IngestReport
from runningtab.mercury.state import IngestState
from runningtab.mercury.vouchers import Unneeded
from runningtab.site import Target
from tests.test_mercury_posting import _Site

runner = CliRunner()
SITE = "https://books.example"


def test_reaching_mercury_requires_naming_the_deployment() -> None:
    # No default: sandbox and production are different hosts, and a command
    # that picks one for you eventually picks the bank.
    result = runner.invoke(app, ["mercury", "snapshot", "--target", "test"])
    assert result.exit_code != 0
    assert "--env" in result.output


def test_sandbox_transactions_are_refused_into_the_books_ledger() -> None:
    with pytest.raises(typer.BadParameter) as exc:
        _refuse_sandbox_into_the_ledger(MercuryEnv.SANDBOX, Target.BOOKS)
    assert "seeded data" in str(exc.value)


def test_the_sandbox_may_still_reach_a_test_site() -> None:
    _refuse_sandbox_into_the_ledger(MercuryEnv.SANDBOX, Target.TEST)


SANDBOX_TEST = ["--env", "sandbox", "--target", "test"]


def test_status_says_where_the_ingest_is(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "mercury.sqlite3"
    with IngestState(path, identity="sandbox/test") as state:
        state.record_cursor("evt_0006", dt.datetime.now(dt.UTC))
        state.mark_seen("evt_0006", dt.datetime.now(dt.UTC))
    result = runner.invoke(app, ["mercury", "status", *SANDBOX_TEST, "--state", str(path)])
    assert result.exit_code == 0
    assert "evt_0006" in result.output
    assert "1 applied" in result.output


def test_status_fails_on_a_cursor_past_retention(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "mercury.sqlite3"
    with IngestState(path, identity="sandbox/test") as state:
        state.record_cursor("evt_0006", dt.datetime.now(dt.UTC) - dt.timedelta(days=120))
    result = runner.invoke(app, ["mercury", "status", *SANDBOX_TEST, "--state", str(path)])
    assert result.exit_code == 1
    assert "STALE" in result.output


def test_status_without_a_state_file_says_to_snapshot(tmp_path: pathlib.Path) -> None:
    result = runner.invoke(app, ["mercury", "status", *SANDBOX_TEST, "--state", str(tmp_path / "absent.sqlite3")])
    assert result.exit_code == 1
    assert "snapshot" in result.output


def test_a_state_file_from_another_deployment_is_refused(tmp_path: pathlib.Path) -> None:
    # `--state` is the way past the namespaced default path, so the binding
    # travels in the file. Reading a sandbox cursor as a production one would
    # report a place in a log the bank never wrote.
    path = tmp_path / "mercury.sqlite3"
    with IngestState(path, identity="sandbox/test") as state:
        state.record_cursor("evt_0006", dt.datetime.now(dt.UTC))
    result = runner.invoke(app, ["mercury", "status", "--env", "production", "--target", "books", "--state", str(path)])
    assert result.exit_code != 0
    assert "sandbox/test" in result.output


def test_reaching_the_ingest_state_requires_naming_the_deployment_and_ledger() -> None:
    # The cursor belongs to one bank and one ledger; a status that guessed
    # which would answer a question nobody asked.
    result = runner.invoke(app, ["mercury", "status"])
    assert result.exit_code != 0
    assert "--env" in result.output


def test_the_report_counts_adopted_rows_apart_from_submitted_ones(capsys: pytest.CaptureFixture[str]) -> None:
    _print_report(IngestReport(submitted=["BT-0001"], adopted=["BT-HAND-1"]), SITE)
    output = capsys.readouterr().out
    assert "submitted    1" in output
    assert "adopted      1" in output
    assert "created" not in output


def test_the_report_counts_draft_vouchers_apart_from_submitted_rows(capsys: pytest.CaptureFixture[str]) -> None:
    # A submitted Bank Transaction is the bank's record; a draft Journal Entry
    # is a proposal a person still has to submit. Counting them together would
    # tell somebody reading the report that more had been committed than was.
    _print_report(IngestReport(submitted=["BT-0001"], drafted=["ACC-JV-0001"]), SITE)
    output = capsys.readouterr().out
    assert "submitted    1" in output
    assert "drafted      1" in output
    assert "a person submits them" in output


def test_the_report_names_each_receipt_and_the_row_it_landed_on(capsys: pytest.CaptureFixture[str]) -> None:
    # A count alone would not say which rows now carry evidence, which is the
    # only part somebody reconciling cares about.
    _print_report(IngestReport(attached=["7c2f5a10-3b44-4c5d-9e6f-1a2b3c4d5e6f-invoice.pdf -> ACC-BTN-0007"]), SITE)
    output = capsys.readouterr().out
    assert "attached     1" in output
    assert "invoice.pdf -> ACC-BTN-0007" in output


def test_the_report_shows_a_broken_balance_rather_than_burying_it(capsys: pytest.CaptureFixture[str]) -> None:
    report = IngestReport(
        balances=[BalanceCheck("acct_chk_1", "Transpire Checking", Decimal("100.00"), Decimal("90.00"))],
        attention=[("txn_y", "deleted at Mercury")],
    )
    _print_report(report, SITE)
    output = capsys.readouterr().out
    assert "OFF BY -10.00" in output
    assert "ATTENTION" in output
    assert report.needs_attention


def test_the_report_names_the_rows_with_no_gl_code(capsys: pytest.CaptureFixture[str]) -> None:
    # A person will have to code these somewhere; the run names them rather
    # than leaving them to be found at reconciliation.
    _print_report(IngestReport(uncoded=[("txn_x", 12)]), SITE)
    output = capsys.readouterr().out
    assert "uncoded" in output
    assert "no GL code" in output
    assert "txn_x 12d" in output


def test_the_report_links_each_draft_a_person_has_to_delete(capsys: pytest.CaptureFixture[str]) -> None:
    # Deleting is a person's job, done in the desk; a name alone sends them
    # searching for each one, so every line carries the document's own URL.
    report = IngestReport(
        unneeded=[Unneeded("txn_z", "ACC-JV-2026-00033", "its Bank Transaction is reconciled")],
    )
    _print_report(report, SITE + "/")
    output = capsys.readouterr().out
    assert "delete       1  (draft Journal Entries no longer needed; a person deletes them)" in output
    assert "ACC-JV-2026-00033 for txn_z: its Bank Transaction is reconciled" in output
    assert "https://books.example/app/journal-entry/ACC-JV-2026-00033" in output
    assert report.needs_attention


class _UntouchedMercury:
    """Stands in for `MercuryClient` where the run must refuse before reading the bank at all."""

    def __init__(self, config: object) -> None:
        pass

    def __enter__(self) -> _UntouchedMercury:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the bank should not have been read ({name})")


def test_an_ingest_that_cannot_see_the_lock_refuses_by_name_and_posts_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bot's Company response carries no lock fields: Frappe deleted
    # them, as it does for a permlevel-1 field the credential cannot read.
    # Reading on would draft vouchers into a closed quarter.
    site = _Site()
    site.documents[("Company", "Transpire")] = {}
    config = SiteConfig(url="https://x.test", api_key="k", api_secret="s", may_submit=True)  # noqa: S106
    monkeypatch.setattr(cli, "ingest_config", lambda site, target: config)
    monkeypatch.setattr(cli, "Client", lambda cfg: Client(cfg, transport=httpx2.MockTransport(site.handler)))
    monkeypatch.setattr(cli, "mercury_config", lambda site, env: None)
    monkeypatch.setattr(cli, "MercuryClient", _UntouchedMercury)

    result = runner.invoke(
        app,
        [
            "mercury", "snapshot", "--env", "sandbox", "--target", "test",
            "--company", "Transpire", "--state", str(tmp_path / "state.json"),
        ],
    )  # fmt: skip

    assert result.exit_code == 1
    assert "refused: the bot credential cannot see Books Closed Through" in result.output
    assert "runningtab access provision --profile bot" in result.output
    assert [r.method for r in site.requests if r.method != "GET"] == []
