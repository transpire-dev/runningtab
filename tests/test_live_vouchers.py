"""What a real ERPNext keeps of a draft voucher's description, on the disposable test site.

Skipped unless RUNNINGTAB_LIVE is set (see `require_live_target` in conftest.py).
The unit tests run against a fake site that stores whatever it is sent, which
is how the voucher description spent weeks in Journal Entry's `user_remark`:
the API reads it back, and no list, report or form ever shows it. This proves
against a live server that the fields the drafts now use -- `title`, and
`remark` with `custom_remark` set -- survive a save as written, including with
a reference number, from which ERPNext otherwise rebuilds `remark`.
"""

from __future__ import annotations

import datetime as dt

import pytest
from icedcoffee_sync.frappe.client import Client

from runningtab.site import is_test_site
from tests.conftest import live_books, require_live_target

pytestmark = pytest.mark.live_runningtab


@pytest.fixture
def client():
    config = require_live_target()
    if not is_test_site(live_books().site, config.url):
        # Fail, never skip: this test writes a document.
        pytest.fail(f"refusing to run against a host not in the books' test_hosts: {config.url}")
    with Client(config) as c:
        yield c


@pytest.fixture
def company(client) -> str:
    rows = client.list("Company", fields=["name"], limit=2)
    if len(rows) != 1:
        pytest.fail(f"expected exactly one company on the test site, found {len(rows)}")
    return rows[0]["name"]


def test_a_draft_keeps_the_title_and_remark_it_was_given(client, company) -> None:
    accounts = client.list(
        "Account",
        fields=["name"],
        filters=[["company", "=", company], ["is_group", "=", 0], ["root_type", "=", "Expense"]],
        limit=2,
    )
    if len(accounts) < 2:
        pytest.fail("the test site needs two postable expense accounts; run the chart apply live test first")
    today = dt.date.today().isoformat()
    doc = {
        "voucher_type": "Journal Entry",
        "company": company,
        "posting_date": today,
        "cheque_no": "live-voucher-remark",
        "cheque_date": today,
        "title": "1Password · 5204 - Office Software",
        "remark": "5204 - Office Software · paid to 1Password · mercury live-voucher-remark",
        "custom_remark": 1,
        "accounts": [
            {"account": accounts[0]["name"], "debit_in_account_currency": 1, "credit_in_account_currency": 0},
            {"account": accounts[1]["name"], "debit_in_account_currency": 0, "credit_in_account_currency": 1},
        ],
    }
    created = client.insert("Journal Entry", doc)
    name = str(created["name"])
    try:
        stored = client.get("Journal Entry", name)
        assert stored["title"] == doc["title"]
        assert stored["remark"] == doc["remark"]
        assert int(stored["custom_remark"]) == 1
        assert int(stored["docstatus"]) == 0
    finally:
        client.delete("Journal Entry", name)
