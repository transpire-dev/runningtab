"""The Mercury client from the health report's side: exact money, two account shapes, a short read that is caught.

Every case reads a fixture from `tests/fixtures/mercury/`; see that
directory's README for what those files are and, just as importantly, what
they are not. The ingest's own cases live in `test_mercury_client.py` and read
`tests/fixtures/mercury/ingest/` -- different numbers, the same one client.
"""

import datetime as dt
import json
import pathlib
from decimal import Decimal
from typing import Any

import httpx2
import pytest

from runningtab.mercury.client import (
    BASE_URLS,
    MalformedResponseError,
    MercuryClient,
    MercuryConfig,
    MercuryEnv,
    MercuryError,
    TruncatedReadError,
)
from runningtab.mercury.models import (
    Account,
    CreditAccount,
    Transaction,
    parse_account,
    parse_credit_account,
    parse_transaction,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "mercury"

CHECKING = "5f1b2a4c-9d6e-4c2f-8a10-3b7d5e9f1c02"

SINCE = dt.datetime(2026, 1, 5, tzinfo=dt.UTC)


def fixture(name: str) -> Any:
    """Read a fixture the way the client reads a response: every number a Decimal."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"), parse_float=Decimal)


def serialisable(name: str) -> Any:
    """The same fixture with plain floats, for scripting a transport to hand back.

    A `Decimal` cannot be re-serialised by the standard encoder, and the point
    of these cases is that the client's own parse is what restores exactness.
    """
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def parse_accounts(payload: Any) -> list[Account]:
    """Deposit accounts from an `/accounts` response body."""
    return [parse_account(row) for row in payload["accounts"]]


def parse_credit_accounts(payload: Any) -> list[CreditAccount]:
    """Credit accounts from a `/credit` response body. `/accounts` never returns these."""
    return [parse_credit_account(row) for row in payload["accounts"]]


def parse_transactions(payload: Any) -> list[Transaction]:
    """Rows from a `/account/{id}/transactions` body, taking each row's own `accountId`.

    The client passes the account it asked about instead, because pairing the
    two halves of an internal transfer needs to know which side is which. Here
    the fixtures carry the field, so the fixture is the authority.
    """
    return [parse_transaction(row, str(row["accountId"])) for row in payload["transactions"]]


def test_accounts_carry_exact_balances():
    accounts = parse_accounts(fixture("accounts"))

    assert [a.name for a in accounts] == ["Primary Checking", "Savings / Reserve"]
    assert accounts[0].current_balance == Decimal("8000.00")
    assert accounts[1].current_balance == Decimal("500.00")
    assert all(isinstance(a.current_balance, Decimal) for a in accounts)


def test_accounts_carry_creation_time_so_a_full_read_has_a_start_date():
    # The transactions endpoint defaults to the last 30 days; createdAt is
    # what turns "every transaction" into a query.
    accounts = parse_accounts(fixture("accounts"))

    assert accounts[0].created_at == dt.datetime(2026, 1, 5, tzinfo=dt.UTC)


def test_credit_accounts_are_a_different_shape():
    # /accounts omits credit accounts entirely, and /credit returns a much
    # smaller object -- no name, no routing number, nothing to join on.
    credit = parse_credit_accounts(fixture("credit"))

    assert len(credit) == 1
    assert credit[0].current_balance == Decimal("22.39")
    assert credit[0].label.startswith("credit account ")


def test_transactions_keep_cents():
    transactions = parse_transactions(fixture("transactions-credit"))

    amounts = {t.counterparty_name: t.amount for t in transactions}
    assert amounts["GitHub"] == Decimal("-47.61")
    # The float route would give -47.609999999999999431..., which is the
    # whole reason the client parses with parse_float=Decimal.
    assert str(amounts["GitHub"]) == "-47.61"


def test_transaction_settlement_follows_status():
    transactions = parse_transactions(fixture("transactions-checking"))

    by_status = {t.raw_status: t.is_postable for t in transactions}
    assert by_status == {"sent": True, "failed": False, "pending": False}
    # Every one of them is a status the enum names: an unrecognised string
    # parses to `blocked` while `raw_status` keeps what arrived, so the two
    # agreeing is what "nothing here was a surprise" looks like.
    assert [t.status.value for t in transactions] == [t.raw_status for t in transactions]


def test_pending_transactions_have_no_posted_at():
    transactions = parse_transactions(fixture("transactions-checking"))

    unsettled = [t for t in transactions if not t.is_postable]
    assert unsettled and all(t.posted_at is None for t in unsettled)


# --- the client's reads ---------------------------------------------------


def _client(handler) -> MercuryClient:
    """A real client over a scripted transport: real URLs, real params, real parsing."""
    return MercuryClient(
        MercuryConfig(base_url=BASE_URLS[MercuryEnv.SANDBOX], token="secret-token:test"),  # noqa: S106 -- a test string
        transport=httpx2.MockTransport(handler),
    )


def test_a_float_on_the_wire_still_lands_as_an_exact_decimal():
    # The body really does carry JSON floats; nothing should reach a ledger
    # that way. The client parses with parse_float=Decimal, so the value that
    # comes out is the literal the bank sent rather than the nearest double.
    body = serialisable("transactions-credit")

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=body)

    with _client(handler) as client:
        rows = client.account_transactions(CHECKING, start=SINCE)

    amounts = {t.counterparty_name: t.amount for t in rows}
    assert all(isinstance(t.amount, Decimal) for t in rows)
    assert str(amounts["GitHub"]) == "-47.61"


def test_a_full_account_read_names_a_start_date_and_pages_on_offset():
    # Without `start` the endpoint answers for the last 30 days, which is a
    # different question than the balance invariant is asking.
    rows = serialisable("transactions-checking")["transactions"]
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(dict(request.url.params))
        offset = int(request.url.params["offset"])
        page_size = 3  # smaller than the row count, so the read has to page
        return httpx2.Response(200, json={"total": len(rows), "transactions": rows[offset : offset + page_size]})

    with _client(handler) as client:
        transactions = client.account_transactions(CHECKING, start=SINCE, page_size=3)

    assert len(transactions) == 4
    assert [p["start"] for p in seen] == ["2026-01-05", "2026-01-05"]
    assert [p["offset"] for p in seen] == ["0", "3"]


def test_a_short_read_is_an_error_not_a_shrug():
    rows = serialisable("transactions-checking")["transactions"]

    def handler(request: httpx2.Request) -> httpx2.Response:
        # The server says nine and hands back four, then nothing.
        offset = int(request.url.params["offset"])
        return httpx2.Response(200, json={"total": 9, "transactions": rows[offset:]})

    with (
        _client(handler) as client,
        pytest.raises(TruncatedReadError, match="read 4 transactions but the server reported total=9"),
    ):
        client.account_transactions(CHECKING, start=SINCE)


def test_a_server_that_ignores_offset_reports_a_truncation_instead_of_looping():
    rows = serialisable("transactions-checking")["transactions"]

    def handler(request: httpx2.Request) -> httpx2.Response:
        # The same single row every time, never advancing.
        return httpx2.Response(200, json={"total": 99999, "transactions": rows[:1]})

    with _client(handler) as client, pytest.raises(TruncatedReadError):
        client.account_transactions(CHECKING, start=SINCE)


def test_repeated_pages_never_add_up_to_a_complete_read():
    # A total a repeated page could reach by simple concatenation: three rows
    # returned every time, four claimed. Counting the duplicates would report
    # a complete read and hand the invariant a sum with one charge in it
    # twice, which then looks like the bank being wrong.
    rows = serialisable("transactions-checking")["transactions"]

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"total": 4, "transactions": rows[:3]})

    with _client(handler) as client, pytest.raises(TruncatedReadError, match="read 3 transactions"):
        client.account_transactions(CHECKING, start=SINCE)


def test_overlapping_pages_are_joined_on_id_not_concatenated():
    # A server that re-sends the last row of the previous page is paging
    # correctly enough; the read should still come back with four rows, not
    # five, and the invariant should see each charge once.
    rows = serialisable("transactions-checking")["transactions"]
    pages = [rows[0:2], rows[1:4], []]
    calls = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        page = pages[min(len(calls), len(pages) - 1)]
        calls.append(1)
        return httpx2.Response(200, json={"total": 4, "transactions": page})

    with _client(handler) as client:
        transactions = client.account_transactions(CHECKING, start=SINCE)

    assert len(transactions) == 4
    assert len({t.id for t in transactions}) == 4


def test_a_row_repeated_inside_one_page_is_read_once():
    # The within-page twin of the overlapping-pages case. A page listing the
    # same charge twice would otherwise reach `total` by counting it twice --
    # a read that is genuinely short passing as complete, with the extra
    # charge left in the sum for the invariant to blame on the bank.
    rows = serialisable("transactions-checking")["transactions"]

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"total": 3, "transactions": [rows[0], rows[0], rows[1]]})

    with (
        _client(handler) as client,
        pytest.raises(TruncatedReadError, match="read 2 transactions but the server reported total=3"),
    ):
        client.account_transactions(CHECKING, start=SINCE)


def test_a_two_hundred_that_is_not_json_is_a_typed_error():
    # A proxy's HTML error page under a 200, or a truncated body. The health
    # report has five sections to print; an unreadable body has to become one
    # of them rather than a JSONDecodeError out of the middle of the command.
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="<html>gateway timeout</html>")

    with _client(handler) as client, pytest.raises(MalformedResponseError, match="/accounts"):
        client.accounts()


def test_a_two_hundred_missing_a_field_is_a_typed_error():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"accounts": [{"id": "a", "name": "Checking"}]})

    with _client(handler) as client, pytest.raises(MalformedResponseError, match="currentBalance"):
        client.accounts()


def test_a_transactions_page_with_no_total_is_a_typed_error():
    # `total` is what makes completeness assertable; a response without one
    # cannot be checked, and guessing zero would report a complete read.
    rows = serialisable("transactions-checking")["transactions"]

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"transactions": rows})

    with _client(handler) as client, pytest.raises(MalformedResponseError, match="total"):
        client.account_transactions(CHECKING, start=SINCE)


def test_the_token_rides_as_a_bearer_header():
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers["authorization"])
        return httpx2.Response(200, json={"accounts": []})

    with _client(handler) as client:
        client.credit_accounts()

    assert seen == ["Bearer secret-token:test"]


def test_an_error_status_names_itself():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, text="unauthorized")

    with _client(handler) as client, pytest.raises(MercuryError, match="401") as exc:
        client.accounts()

    assert exc.value.status == 401
