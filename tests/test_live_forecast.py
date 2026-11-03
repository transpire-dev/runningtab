"""Install the Cash Forecast doctypes on a real disposable site and write one forecast.

Skipped unless RUNNINGTAB_LIVE is set (see `require_live_target` in conftest.py).
Re-runnable: the install is idempotent, and the forecast written here is
deleted in teardown with the administrator credential. The doctypes stay; they
are what a site the forecast writes into is supposed to have.
"""

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal

import pytest
from icedcoffee_sync.frappe.client import Client

from runningtab.forecast.compute import compute
from runningtab.forecast.doctype import CASH_FORECAST, install_doctypes, write_forecast
from runningtab.mercury.posting import Outcome
from runningtab.site import is_test_site
from tests.conftest import live_books, require_live_target
from tests.test_forecast_compute import MEMBERS, NOW, A, B, forecast_file

pytestmark = pytest.mark.live_runningtab


@pytest.fixture
def client() -> Iterator[Client]:
    config = require_live_target()
    if not is_test_site(live_books().site, config.url):
        pytest.fail(f"refusing to run against a host not in the books' test_hosts: {config.url}")
    with Client(config) as c:
        yield c


def test_a_forecast_is_written_and_read_back(client: Client) -> None:
    # The members and lines must link accounts that exist on this site.
    accounts = {
        row["account_number"]: row["name"]
        for row in client.list("Account", fields=["name", "account_number"], limit=0)
        if row.get("account_number")
    }
    numbers = {*MEMBERS, A.account, B.account}
    missing = sorted(numbers - accounts.keys())
    if missing:
        pytest.fail(f"the test site's chart lacks accounts {missing}; apply the chart first")

    install_doctypes(client)
    assert {outcome for _, outcome in install_doctypes(client)} == {Outcome.UNCHANGED}
    forecast = compute(
        forecast_file(A, B),
        now=NOW,
        opening_cash=Decimal("300"),
        ledger_through=dt.date(2026, 10, 25),
        transactions=(),
    )
    name = write_forecast(client, forecast, link=lambda number: accounts[number])
    try:
        stored = client.get(CASH_FORECAST, name)
        assert Decimal(str(stored["total_call"])) == forecast.total_call
        assert len(stored["months"]) == len(forecast.months)
        assert len(stored["members"]) == len(forecast.members)
        assert len(stored["lines"]) == len(forecast.dues)
        first = stored["lines"][0]
        assert (first["status"], first["account"]) == (forecast.dues[0].status, accounts[forecast.dues[0].line.account])
        assert Decimal(str(stored["months"][0]["closing"])) == forecast.months[0].closing
        # A Data field keeps the ISO string with its offset; a Datetime field would drop it.
        assert stored["computed_at"] == forecast.computed_at.isoformat(timespec="seconds")
    finally:
        client.delete(CASH_FORECAST, name)
