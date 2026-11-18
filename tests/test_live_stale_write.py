"""Whether a real ERPNext refuses a write planned from a stale read, on the disposable test site.

Skipped unless RUNNINGTAB_LIVE is set (see `require_live_target` in conftest.py).
The Mercury sweeps plan from rows they read once, at the start, and send back
each row's `modified` with any update so the server can refuse the write if a
person changed the row since. The fake site in the unit tests only does what it
was taught; this proves Frappe's optimistic locking (`Document.check_if_latest`)
does it for a REST update, and that an update carrying the current `modified`
still goes through.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from collections.abc import Iterator

import pytest
from icedcoffee_sync.frappe.client import Client
from icedcoffee_sync.frappe.errors import DoesNotExistError, TimestampMismatchError

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
def draft(client) -> Iterator[str]:
    companies = client.list("Company", fields=["name"], limit=2)
    if len(companies) != 1:
        pytest.fail(f"expected exactly one company on the test site, found {len(companies)}")
    company = companies[0]["name"]
    accounts = client.list(
        "Account",
        fields=["name"],
        filters=[["company", "=", company], ["is_group", "=", 0], ["root_type", "=", "Expense"]],
        limit=2,
    )
    if len(accounts) < 2:
        pytest.fail("the test site needs two postable expense accounts; run the chart apply live test first")
    today = dt.date.today().isoformat()
    created = client.insert(
        "Journal Entry",
        {
            "voucher_type": "Journal Entry",
            "company": company,
            "posting_date": today,
            "title": "live-stale-write: as drafted",
            "accounts": [
                {"account": accounts[0]["name"], "debit_in_account_currency": 1, "credit_in_account_currency": 0},
                {"account": accounts[1]["name"], "debit_in_account_currency": 0, "credit_in_account_currency": 1},
            ],
        },
    )
    name = str(created["name"])
    yield name
    with contextlib.suppress(DoesNotExistError):
        # Already gone when the test itself deleted it.
        client.delete("Journal Entry", name)


def _modified(client, name: str) -> str:
    (row,) = client.list("Journal Entry", fields=["name", "modified"], filters=[["name", "=", name]], limit=2)
    return str(row["modified"])


def test_a_write_carrying_a_stale_modified_is_refused_and_the_persons_edit_stands(client, draft) -> None:
    read_at_start = _modified(client, draft)
    # A person edits the draft after the sweep read it.
    client.update("Journal Entry", draft, {"title": "live-stale-write: a person's title"})

    with pytest.raises(TimestampMismatchError):
        client.update("Journal Entry", draft, {"title": "live-stale-write: the sweep's", "modified": read_at_start})

    assert client.get("Journal Entry", draft)["title"] == "live-stale-write: a person's title"


def test_reading_a_document_deleted_since_is_a_does_not_exist_error(client, draft) -> None:
    # What the voucher pass catches when a person deletes a draft it read.
    client.delete("Journal Entry", draft)

    with pytest.raises(DoesNotExistError):
        client.get("Journal Entry", draft)


def test_a_write_carrying_the_current_modified_goes_through(client, draft) -> None:
    client.update(
        "Journal Entry", draft, {"title": "live-stale-write: the sweep's", "modified": _modified(client, draft)}
    )

    assert client.get("Journal Entry", draft)["title"] == "live-stale-write: the sweep's"
