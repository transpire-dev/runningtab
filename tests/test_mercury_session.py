"""Assembling a `PostingContext`: the one place Books Closed Through is read.

The ledger side goes through the real `icedcoffee-sync` client over a mock transport,
so what these assert is the request actually sent, not a stand-in's idea of
one -- same rule as every other Mercury test.
"""

from __future__ import annotations

import datetime as dt

import httpx2
import pytest
from icedcoffee_sync.frappe.client import Client, SiteConfig

from runningtab.chart import load_chart
from runningtab.mercury.mapping import parse_mapping
from runningtab.mercury.session import posting_context
from runningtab.period import PeriodError
from runningtab.period.lock import LockState
from tests.conftest import EXAMPLE_BOOKS, TEST_MAPPING_TOML
from tests.test_mercury_posting import _Site

SITE = SiteConfig(url="https://example.test", api_key="KEY", api_secret="SECRET")  # noqa: S106
COMPANY = "Transpire"


@pytest.fixture
def site() -> _Site:
    return _Site()


@pytest.fixture
def ledger(site: _Site):
    with Client(SITE, transport=httpx2.MockTransport(site.handler)) as client:
        yield client


def test_the_context_carries_the_lock_read_alongside_company(site, ledger) -> None:
    # Read once, from the same Company document `sole_company`/an explicit
    # `--company` already names -- not a second round trip of its own.
    site.documents[("Company", COMPANY)] = {
        "accounts_frozen_till_date": "2026-06-30",
        "role_allowed_for_frozen_entries": None,
    }
    ctx = posting_context(
        ledger,
        company=COMPANY,
        mapping=parse_mapping(TEST_MAPPING_TOML),
        chart=load_chart(EXAMPLE_BOOKS.chart_path),
        bot_user=EXAMPLE_BOOKS.bot.user,
    )
    assert ctx.lock == LockState(closed_through=dt.date(2026, 6, 30), bypass_role=None)


def test_the_context_carries_no_lock_when_the_company_has_none(site, ledger) -> None:
    site.documents[("Company", COMPANY)] = {
        "accounts_frozen_till_date": None,
        "role_allowed_for_frozen_entries": None,
    }
    ctx = posting_context(
        ledger,
        company=COMPANY,
        mapping=parse_mapping(TEST_MAPPING_TOML),
        chart=load_chart(EXAMPLE_BOOKS.chart_path),
        bot_user=EXAMPLE_BOOKS.bot.user,
    )
    assert ctx.lock == LockState(closed_through=None, bypass_role=None)


def test_a_bot_that_cannot_see_the_lock_is_refused_not_told_there_is_none(site, ledger) -> None:
    # Before `access provision --profile bot` grants the bot's permlevel-1
    # Company read (or after a provision that installed the fence and then
    # failed), Frappe deletes both lock fields from what it hands back to
    # this credential. Reading that as "no lock" would draft vouchers into
    # a closed quarter, and ERPNext refuses a frozen date only on submit --
    # the ingest saves drafts -- so nothing downstream would stop it.
    site.documents[("Company", COMPANY)] = {}

    with pytest.raises(PeriodError) as exc:
        posting_context(
            ledger,
            company=COMPANY,
            mapping=parse_mapping(TEST_MAPPING_TOML),
            chart=load_chart(EXAMPLE_BOOKS.chart_path),
            bot_user=EXAMPLE_BOOKS.bot.user,
        )

    message = str(exc.value)
    assert "Books Closed Through" in message
    assert "runningtab access provision --profile bot" in message
