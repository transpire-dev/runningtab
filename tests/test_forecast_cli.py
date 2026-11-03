"""`runningtab forecast`: what a person sees, what reaches the ledger, and every refusal."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from icedcoffee_sync.frappe.client import Client, SiteConfig
from typer.testing import CliRunner

from runningtab import cli
from runningtab.cli import _desk_url, app
from runningtab.forecast.names import CASH_FORECAST
from runningtab.mercury.models import Account, Status, Transaction
from runningtab.site import SiteCredentialError
from tests.test_forecast_compute import NOW, A, B, forecast_file, txn
from tests.test_mercury_posting import _Site

runner = CliRunner()

CONFIG = SiteConfig(url="https://books.example.test/", api_key="k", api_secret="s")  # noqa: S106
PLAN = ["forecast", "plan", "--env", "production", "--target", "books"]
APPLY = ["forecast", "apply", "--env", "production", "--target", "books"]


class _Mercury:
    """Enough of `MercuryClient` for `read_everything`: one account whose rows add up to its balance."""

    def __init__(self, balance: str = "300", rows: str = "300") -> None:
        self.account = Account(id="chk", name="Checking", current_balance=Decimal(balance))
        self.rows = [
            deposit(rows),
        ]

    def __enter__(self) -> _Mercury:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def accounts(self) -> list[Account]:
        return [self.account]

    def credit_accounts(self) -> list[Any]:
        return []

    def account_transactions(self, account_id: str, *, start: object = None) -> list[Any]:
        return self.rows


def deposit(amount: str) -> Transaction:
    return txn("Deposit", amount, dt.date(2026, 9, 1), Status.SENT)


class _Wiring:
    """The fakes one invocation needs, and the ledger they share."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, mercury: _Mercury | None = None) -> None:
        self.site = _Site()
        self.site.documents[("Company", "Transpire")] = {"name": "Transpire", "abbr": "T"}
        self.site.documents[("Bank Transaction", "BT-1")] = {
            "date": "2026-10-20",
            "status": "Unreconciled",
            "docstatus": 1,
        }
        self.configs: list[bool] = []
        # Replaceable per test; read when the client is built.
        self.handler: Callable[[httpx2.Request], httpx2.Response] = self.site.handler
        monkeypatch.setattr(cli, "site_config", self._site_config)
        monkeypatch.setattr(cli, "mercury_config", lambda site, env: None)
        monkeypatch.setattr(cli, "MercuryClient", lambda config: mercury or _Mercury())
        monkeypatch.setattr(
            cli, "Client", lambda config: Client(config, transport=httpx2.MockTransport(lambda r: self.handler(r)))
        )
        monkeypatch.setattr(cli, "load_forecast", lambda path, chart: forecast_file(A, B))
        monkeypatch.setattr(cli, "_now", lambda: NOW)

    def _site_config(self, site: object, target: object, *, readonly: bool = False) -> SiteConfig:
        self.configs.append(readonly)
        return CONFIG

    def writes(self) -> list[httpx2.Request]:
        return [r for r in self.site.requests if r.method != "GET"]


@pytest.fixture
def wiring(monkeypatch: pytest.MonkeyPatch) -> _Wiring:
    return _Wiring(monkeypatch)


def test_plan_prints_each_members_share(wiring: _Wiring) -> None:
    result = runner.invoke(app, PLAN)

    assert result.exit_code == 0, result.output
    # 300 in the bank against 180 owed a month: the path bottoms at -420, the floor is
    # A's 100, so the call is 520, split three ways and rounded up.
    assert "opening cash: $300.00" in result.output
    assert "ledger through: 2026-10-19" in result.output
    assert "window: 2026-10-26 to 2027-01-31" in result.output
    assert "floor: $100.00 (from A)" in result.output
    assert "3110 - Capital Contribution – Member 1: $173.34" in result.output  # noqa: RUF001
    assert "3120 - Capital Contribution – Member 2: $173.34" in result.output  # noqa: RUF001
    assert "3130 - Capital Contribution – Member 3: $173.34" in result.output  # noqa: RUF001
    assert "total call: $520.02" in result.output
    assert "$" in result.output
    assert "owed" in result.output
    assert wiring.configs == [True]


def test_plan_writes_nothing(wiring: _Wiring) -> None:
    result = runner.invoke(app, PLAN)

    assert result.exit_code == 0, result.output
    assert wiring.writes() == []
    assert "wrote" not in result.output


def test_apply_prints_the_new_documents_url(wiring: _Wiring) -> None:
    result = runner.invoke(app, APPLY)

    assert result.exit_code == 0, result.output
    stored = [(name, doc) for (doctype, name), doc in wiring.site.documents.items() if doctype == CASH_FORECAST]
    assert len(stored) == 1
    name, doc = stored[0]
    assert f"wrote {name}" in result.output
    assert f"https://books.example.test/app/cash-forecast/{name}" in result.output
    assert doc["members"][0]["account"] == "3110 - Capital Contribution – Member 1 - T"  # noqa: RUF001
    assert wiring.configs[-1] is False
    assert "$" in result.output


def test_a_site_in_maintenance_is_one_sentence_not_a_traceback(wiring: _Wiring) -> None:
    def maintenance(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503, json={"errors": [{"type": "SessionStopped", "message": "Updating"}]})

    wiring.handler = maintenance

    result = runner.invoke(app, PLAN)

    assert result.exit_code == 1
    assert "the site is in maintenance mode" in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


@pytest.mark.parametrize("command", [PLAN, APPLY])
def test_an_unreadable_credential_is_one_sentence_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    def unreachable(site: object, target: object, *, readonly: bool = False) -> SiteConfig:
        raise SiteCredentialError("could not read secret example-ledger-reader-key in example-project: timed out")

    monkeypatch.setattr(cli, "site_config", unreachable)

    result = runner.invoke(app, command)

    assert result.exit_code == 1
    assert "refused: could not read secret example-ledger-reader-key" in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


@pytest.mark.parametrize(
    ("command", "provision"),
    [
        (PLAN, "runningtab access provision --target books --profile reader"),
        (APPLY, "runningtab access provision --target books`"),
    ],
)
def test_a_credential_without_bank_transaction_read_names_the_provision_to_run(
    wiring: _Wiring, command: list[str], provision: str
) -> None:
    def forbidden(request: httpx2.Request) -> httpx2.Response:
        if "Bank%20Transaction" in str(request.url) or "Bank Transaction" in str(request.url):
            return httpx2.Response(
                403,
                json={
                    "exc_type": "PermissionError",
                    "_server_messages": '["{\\"message\\": \\"Insufficient Permission for <strong>Bank Transaction</strong>\\"}"]',
                },
            )
        return wiring.site.handler(request)

    wiring.handler = forbidden

    result = runner.invoke(app, command)

    assert result.exit_code == 1
    assert "Insufficient Permission for Bank Transaction" in result.output
    assert provision in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_a_balance_mismatch_refuses_before_anything_is_written(monkeypatch: pytest.MonkeyPatch) -> None:
    wiring = _Wiring(monkeypatch, _Mercury(balance="300", rows="250"))

    result = runner.invoke(app, APPLY)

    assert result.exit_code == 1
    assert "Checking" in result.output
    assert "no forecast" in result.output
    assert wiring.writes() == []


def test_a_forecast_file_that_does_not_validate_is_one_sentence(
    wiring: _Wiring, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runningtab.forecast.model import ForecastFileError

    def broken(path: object, chart: object) -> None:
        raise ForecastFileError("forecast.toml: line 'X' names account 9999, which is not in the chart")

    monkeypatch.setattr(cli, "load_forecast", broken)

    result = runner.invoke(app, PLAN)

    assert result.exit_code == 1
    assert "names account 9999" in result.output
    assert wiring.site.requests == []


def test_apply_refuses_when_the_doctype_is_not_installed(wiring: _Wiring) -> None:
    inner = wiring.site.handler

    def without_the_doctype(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET" and request.url.path.replace("%20", " ").endswith("/Cash Forecast"):
            return httpx2.Response(404, json={"errors": [{"type": "DoesNotExistError", "message": "not found"}]})
        return inner(request)

    wiring.handler = without_the_doctype

    result = runner.invoke(app, APPLY)

    assert result.exit_code == 1
    assert "the Cash Forecast doctype is not installed on https://books.example.test; run" in result.output
    assert "runningtab forecast install-doctype --target books" in result.output
    assert wiring.writes() == []


def test_the_sandbox_is_never_forecast_into_the_real_books(wiring: _Wiring) -> None:
    result = runner.invoke(app, ["forecast", "plan", "--env", "sandbox", "--target", "books"])

    assert result.exit_code != 0
    assert "sandbox" in result.output
    assert wiring.site.requests == []


def test_install_doctype_reports_each_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    site = _Site()
    monkeypatch.setattr(cli, "admin_config", lambda site, target: CONFIG)
    monkeypatch.setattr(cli, "Client", lambda config: Client(config, transport=httpx2.MockTransport(site.handler)))

    result = runner.invoke(app, ["forecast", "install-doctype", "--target", "test", "--yes"])

    assert result.exit_code == 0, result.output
    for name in ("Cash Forecast", "Cash Forecast Month", "Cash Forecast Member", "Cash Forecast Line"):
        assert name in result.output
    assert "created" in result.output.lower()


def test_the_desk_url_encodes_the_name() -> None:
    assert _desk_url("https://x.test/", "Cash Forecast", "CF 00001") == "https://x.test/app/cash-forecast/CF%2000001"


def test_apply_says_when_the_bot_may_not_create_forecasts(wiring: _Wiring) -> None:
    inner = wiring.site.handler

    def refuses_the_insert(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            return httpx2.Response(
                403, json={"errors": [{"type": "PermissionError", "message": "No permission for Cash Forecast"}]}
            )
        return inner(request)

    wiring.handler = refuses_the_insert

    result = runner.invoke(app, APPLY)

    assert result.exit_code == 1
    assert "the books bot may not create Cash Forecast documents on https://books.example.test; run" in result.output
    assert "runningtab access provision --target books" in result.output
    assert "Traceback" not in result.output


def test_apply_refuses_the_sandbox_before_any_request(wiring: _Wiring) -> None:
    result = runner.invoke(app, ["forecast", "apply", "--env", "sandbox", "--target", "books"])

    assert result.exit_code == 2
    assert "sandbox" in result.output
    assert wiring.site.requests == []


def test_install_doctype_in_maintenance_is_one_sentence_not_a_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    def maintenance(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503, json={"errors": [{"type": "SessionStopped", "message": "Updating"}]})

    monkeypatch.setattr(cli, "admin_config", lambda site, target: CONFIG)
    monkeypatch.setattr(cli, "Client", lambda config: Client(config, transport=httpx2.MockTransport(maintenance)))

    result = runner.invoke(app, ["forecast", "install-doctype", "--target", "test", "--yes"])

    assert result.exit_code == 1
    assert "refused: the site is in maintenance mode" in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
