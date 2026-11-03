"""The Mercury client, replaying captured responses through a real HTTP stack."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx2
import pytest

from runningtab.mercury.client import (
    BASE_URLS,
    MalformedResponseError,
    MercuryClient,
    MercuryEnv,
    MercuryError,
    TruncatedReadError,
    config_for,
)
from tests.conftest import payload

SANDBOX = config_for(MercuryEnv.SANDBOX, "SANDBOX-TOKEN")

ROUTES = {
    "/accounts": "accounts",
    "/credit": "credit",
    "/events": "events",
    "/categories": "categories",
    "/account/acct_chk_1/transactions": "transactions_checking",
    "/account/acct_chk_2/transactions": "transactions_savings",
    "/account/acct_credit_1/transactions": "transactions_credit",
}


def _sandbox_handler(seen: list[httpx2.Request] | None = None):
    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        fixture = ROUTES.get(request.url.path.removeprefix("/api/v1"))
        if fixture is None:
            return httpx2.Response(404, json={"error": "no such route"})
        return httpx2.Response(200, json=payload(fixture))

    return handler


def _client(handler, config=SANDBOX) -> MercuryClient:
    return MercuryClient(config, transport=httpx2.MockTransport(handler))


def test_the_base_url_is_construction_input_and_sandbox_is_not_production() -> None:
    assert BASE_URLS[MercuryEnv.SANDBOX] != BASE_URLS[MercuryEnv.PRODUCTION]
    assert "sandbox" in BASE_URLS[MercuryEnv.SANDBOX]


def test_the_client_has_no_verb_but_get() -> None:
    # Read-only structurally rather than by convention: there is nothing to
    # call that could write a Mercury record.
    for verb in ("post", "put", "patch", "delete", "request"):
        assert not hasattr(MercuryClient, verb)


def test_requests_carry_the_bearer_token_and_the_configured_base_url() -> None:
    seen: list[httpx2.Request] = []
    with _client(_sandbox_handler(seen)) as client:
        client.accounts()
    assert seen[0].headers["authorization"] == "Bearer SANDBOX-TOKEN"
    assert str(seen[0].url).startswith(BASE_URLS[MercuryEnv.SANDBOX])
    assert seen[0].method == "GET"


def test_the_token_stays_out_of_the_config_repr() -> None:
    assert "SANDBOX-TOKEN" not in repr(SANDBOX)


def test_accounts_and_credit_accounts_are_read_separately() -> None:
    with _client(_sandbox_handler()) as client:
        accounts = client.accounts()
        credit = client.credit_accounts()
    assert [a.id for a in accounts] == ["acct_chk_1", "acct_chk_2"]
    # `/accounts` omits credit accounts entirely; a run that only reads it
    # misses the card and its balance.
    assert [c.id for c in credit] == ["acct_credit_1"]


def test_an_account_read_is_checked_against_its_own_total() -> None:
    with _client(_sandbox_handler()) as client:
        rows = client.account_transactions("acct_chk_1")
    assert len(rows) == payload("transactions_checking")["total"]
    assert all(row.account_id == "acct_chk_1" for row in rows)


def test_a_short_read_raises_instead_of_returning_a_plausible_subset() -> None:
    body = payload("transactions_checking")
    truncated: dict[str, Any] = {"total": body["total"], "transactions": body["transactions"][:3]}

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=truncated)

    with _client(handler) as client, pytest.raises(TruncatedReadError) as exc:
        client.account_transactions("acct_chk_1")
    assert "total=8" in str(exc.value)


def test_a_row_the_server_sends_twice_is_read_once() -> None:
    # `total` is what makes completeness assertable, so it has to count
    # distinct transactions. A server repeating a row -- within a page or
    # across a retried one -- would otherwise let a short read reach `total`,
    # pass `check_complete`, and hand a duplicated charge to the balance
    # invariant that is supposed to catch exactly this.
    body = payload("transactions_checking")
    rows = body["transactions"]
    # Eight rows for a total of eight, but one of them twice -- so the account's
    # last transaction was never sent and the count says nothing is missing.
    doubled = {"total": body["total"], "transactions": [rows[0], rows[0], *rows[1:-1]]}
    # This server ignores `offset` and answers every request with that page, so
    # the read is also asserting that it gives up on a page carrying nothing
    # new rather than asking again for as long as the count stays under `total`.
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=doubled)

    with _client(handler) as client, pytest.raises(TruncatedReadError) as exc:
        client.account_transactions("acct_chk_1")
    assert "total=8" in str(exc.value)
    assert "read 7 transactions" in str(exc.value)
    assert len(seen) == 2


def test_a_row_repeated_across_pages_is_not_counted_twice() -> None:
    body = payload("transactions_checking")
    rows = body["transactions"]
    pages = [rows[:4], [rows[3], *rows[4:]]]

    def handler(request: httpx2.Request) -> httpx2.Response:
        offset = int(request.url.params.get("offset", 0))
        page = pages[0] if offset == 0 else pages[1]
        return httpx2.Response(200, json={"total": body["total"], "transactions": page})

    with _client(handler) as client:
        read = client.account_transactions("acct_chk_1", page_size=4)
    assert [r.id for r in read] == [r["id"] for r in rows]


def test_an_overlapping_page_does_not_step_over_the_rows_it_repeated() -> None:
    # The offset walks distinct rows, not rows received. Advancing by the raw
    # page length would step past as many positions as the page repeated, and
    # the transactions in that gap would never be asked for -- a short read
    # with nothing duplicated in it to give the game away.
    body = payload("transactions_checking")
    rows = body["transactions"]
    offsets: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        offsets.append(offset)
        # A server that repeats the first row of every window it serves, so a
        # page of four carries three transactions.
        window = rows[offset : offset + limit - 1]
        return httpx2.Response(
            200, json={"total": body["total"], "transactions": [*window[:1], *window] if window else []}
        )

    with _client(handler) as client:
        read = client.account_transactions("acct_chk_1", page_size=4)
    assert [r.id for r in read] == [r["id"] for r in rows]
    assert offsets == [0, 3, 6]


def test_paging_stops_when_the_total_is_reached() -> None:
    body = payload("transactions_checking")
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params.get("limit", 2))
        return httpx2.Response(
            200, json={"total": body["total"], "transactions": body["transactions"][offset : offset + limit]}
        )

    with _client(handler) as client:
        rows = client.account_transactions("acct_chk_1", page_size=3)
    assert len(rows) == 8
    assert [int(r.url.params["offset"]) for r in seen] == [0, 3, 6]


def test_events_page_by_handing_back_the_last_event_id() -> None:
    events = payload("events")["events"]
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        start_after = request.url.params.get("start_after")
        remaining = events
        if start_after is not None:
            index = [e["id"] for e in events].index(start_after)
            remaining = events[index + 1 :]
        return httpx2.Response(200, json={"events": remaining[:2]})

    with _client(handler) as client:
        read = client.events(page_size=2)
    assert [e.id for e in read] == [e["id"] for e in events]
    # start_after is exclusive, so resumption has no overlap window: each page
    # asks for what comes strictly after the last id it was handed.
    assert [r.url.params.get("start_after") for r in seen] == [None, "evt_0002", "evt_0004", "evt_0006"]


def test_the_latest_event_id_is_the_end_of_the_log() -> None:
    with _client(_sandbox_handler()) as client:
        assert client.latest_event_id() == "evt_0006"


def test_an_unrecognised_envelope_raises_rather_than_reading_as_empty() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"results": []})

    with _client(handler) as client, pytest.raises(MercuryError) as exc:
        client.accounts()
    assert "accounts" in str(exc.value)


def test_an_error_response_carries_its_status() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, text="unauthorized")

    with _client(handler) as client, pytest.raises(MercuryError) as exc:
        client.accounts()
    assert exc.value.status == 401


def test_money_never_arrives_as_a_float() -> None:
    with _client(_sandbox_handler()) as client:
        account = client.accounts()[0]
        rows = client.account_transactions("acct_chk_1")
    assert account.current_balance == Decimal("4532.39")
    assert all(isinstance(row.amount, Decimal) for row in rows)


def test_categories_carry_all_three_visibility_flags() -> None:
    with _client(_sandbox_handler()) as client:
        categories = client.categories()
    assert [c.name for c in categories] == [row["name"] for row in payload("categories")["categories"]]
    payroll = categories[0]
    assert (payroll.visible_for_card_spend, payroll.visible_for_reimbursements, payroll.visible_for_other) == (
        False,
        False,
        True,
    )


def test_a_category_missing_a_flag_is_unreadable_rather_than_defaulted() -> None:
    # A flag guessed rather than read would make the category plan propose an
    # edit that changes nothing, or skip one that matters.
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"categories": [{"id": "cat-1", "name": "Payroll"}], "page": {}})

    with _client(handler) as client, pytest.raises(MalformedResponseError) as exc:
        client.categories()
    assert "visibleForCardSpend" in str(exc.value)


def test_categories_page_to_the_end_of_the_list() -> None:
    # A short read reports a category as absent when it exists, and acting on
    # that creates a duplicate -- the one state the category plan then refuses
    # to resolve.
    rows = payload("categories")["categories"]
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        start_after = request.url.params.get("start_after")
        remaining = rows
        if start_after is not None:
            remaining = rows[[r["id"] for r in rows].index(start_after) + 1 :]
        page = remaining[:2]
        following = remaining[2:]
        cursor = {"nextPage": page[-1]["id"]} if page and following else {}
        return httpx2.Response(200, json={"categories": page, "page": cursor})

    with _client(handler) as client:
        collected = client.categories(page_size=2)
    assert [c.id for c in collected] == [r["id"] for r in rows]
    assert len(seen) == 3


def test_a_category_page_cursor_that_does_not_advance_stops_the_read() -> None:
    # A server that ignores the cursor repeats itself; paging forever on it
    # would hang the command rather than report anything.
    rows = payload("categories")["categories"]
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"categories": rows[:2], "page": {"nextPage": rows[1]["id"]}})

    with _client(handler) as client:
        collected = client.categories(page_size=2)
    assert [c.id for c in collected] == [r["id"] for r in rows[:2]]
    assert len(seen) == 2
