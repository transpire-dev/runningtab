"""A read-only Mercury client.

Read-only structurally, not by convention: the only verb this class can send is
GET. There is no `post`, no `patch`, and no generic `request` escape hatch, so
"the ingest never writes a Mercury record" is a property of the code rather
than a rule someone has to remember. Writing a transaction note would need
`updateTransactions`, an allowlisted scope, and the static egress behind it --
deferred deliberately, so nothing here is arranged to make it easy.

This repo can make exactly three Mercury writes, and none lives here:
`create_category`, `edit_category` and `delete_category` on
`MercuryCategoryWriter`, in `runningtab.mercury.categories`, reached only by
`runningtab mercury categories apply`. `runningtab.mercury.ingest` does not import that
module, so the ingest's dependency on Mercury stops at this file and this file
has no verb but GET.

The base URL is construction input and never defaults to production. Sandbox and
production are different hosts, and a client that defaults to one of them is a
client that eventually reaches it by accident.

`runningtab health` imports this module directly: `MercuryClient` plus `reconcile`
in `runningtab.mercury.ingest` are the two pieces a health check needs, and neither
depends on the ledger side.

Two shapes matter more than the rest, both established by measurement:

- **`/accounts` omits credit accounts entirely.** The card comes back only from
  `/credit`, with a much smaller schema. Two models, not one with optional
  fields.
- **Per-account transactions default to the last 30 days.** `start` is not
  optional in practice: leaving it off silently answers a different question
  than "every transaction on this account", and the balance invariant would
  then report a discrepancy that is really a short read. Callers pass the
  account's `createdAt` to mean "all of them".
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import enum
import json
from collections.abc import Generator
from decimal import Decimal
from typing import Any

import httpx2

from runningtab.mercury.models import (
    Account,
    Category,
    CreditAccount,
    Event,
    Transaction,
    parse_account,
    parse_category,
    parse_credit_account,
    parse_event,
    parse_transaction,
)

# Mercury's own page ceiling for the event log.
EVENT_PAGE_SIZE = 1000
# And for the category list, which pages the same way with a different cursor.
CATEGORY_PAGE_SIZE = 1000
# Transactions have no documented ceiling; this sizes each request, and is not
# a limit on what `account_transactions` returns.
TRANSACTION_PAGE_SIZE = 500


class MercuryEnv(enum.StrEnum):
    """Which Mercury deployment. Named explicitly at every call site; no default."""

    SANDBOX = "sandbox"
    PRODUCTION = "production"


BASE_URLS: dict[MercuryEnv, str] = {
    MercuryEnv.SANDBOX: "https://api-sandbox.mercury.com/api/v1",
    MercuryEnv.PRODUCTION: "https://api.mercury.com/api/v1",
}


class MercuryError(RuntimeError):
    """Mercury refused a request."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class TruncatedReadError(MercuryError):
    """The account returned fewer transactions than its own `total` claims exist.

    Worth its own type. A silently short read is the failure mode that put a
    wrong balance in front of the investigation, and on a feed driving a general
    ledger a silent gap is the worst thing that can happen.
    """


class MalformedResponseError(MercuryError):
    """Mercury answered with a success status and a body this client cannot read.

    A proxy's HTML error page under a 200, a truncated body, a field that has
    moved. An error status is the same failure wearing different clothes, which
    is why this is a `MercuryError`: to a caller both mean "no answer". Typed so
    the failure arrives as a report line -- `runningtab health` has five sections to
    print, and a `JSONDecodeError` out of the middle of one takes the other four
    with it.
    """


@dataclasses.dataclass(frozen=True)
class MercuryConfig:
    """Where Mercury is and which token reads it."""

    base_url: str
    # Kept out of the repr: this object shows up in log lines and pytest
    # assertion output, and a bank token has no business riding along.
    token: str = dataclasses.field(repr=False)
    timeout: float = 30.0


def config_for(env: MercuryEnv, token: str, *, timeout: float = 30.0) -> MercuryConfig:
    """Config for a named deployment. The only place a base URL is chosen."""
    return MercuryConfig(base_url=BASE_URLS[env], token=token, timeout=timeout)


def _rows(payload: Any, *keys: str) -> list[dict[str, Any]]:
    """The row list out of a response envelope.

    Mercury's envelope keys are not uniform across resources and the
    investigation recorded the pagination behaviour rather than the exact
    wrappers, so each caller names the keys it expects and a bare list is
    accepted too. An envelope carrying none of them raises rather than
    returning nothing -- an empty read and an unrecognised envelope must not
    look alike.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise MercuryError(f"expected a list under one of {list(keys)}; got {type(payload).__name__} {str(payload)[:200]}")


def _loads(body: str) -> Any:
    """Parse a Mercury response with every JSON number as `Decimal`.

    `parse_float=Decimal` is the whole point: `-47.61` must stay `-47.61`, not
    become the nearest double. `models.money` would recover it from `str()`
    anyway, but no float ever existing is a shorter thing to be sure of.
    """
    return json.loads(body, parse_float=Decimal)


@contextlib.contextmanager
def _readable(path: str) -> Generator[None]:
    """Turn a body this client cannot read into `MalformedResponseError`.

    Wraps decoding and parsing, which fail with whatever the standard library
    raises for a missing key, a value of the wrong type, or a timestamp that is
    not one -- `models.MercuryPayloadError` among them, since it is a
    `ValueError`. Those are the ways a 200 can still be no answer, and they are
    the caller's to report rather than to crash on.
    """
    try:
        yield
    except (KeyError, TypeError, ValueError) as exc:
        raise MalformedResponseError(f"{path} answered 200 with a body this client cannot read: {exc!r}") from exc


class MercuryClient:
    """Synchronous, read-only. No verb but GET, and no method that writes anything.

    The three Mercury writes this repo can make are on `MercuryCategoryWriter`
    in `runningtab.mercury.categories`, which is a separate class holding a separate
    token. Adding any of those verbs here would hand the ingest a capability it
    has no use for -- `delete_category` most of all, since the ingest reads the
    rows a deletion would strip the coding off.
    """

    def __init__(self, config: MercuryConfig, *, transport: httpx2.BaseTransport | None = None) -> None:
        self._config = config
        self._http = httpx2.Client(timeout=config.timeout, transport=transport)

    def __enter__(self) -> MercuryClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._http.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self._http.request(
            "GET",
            f"{self._config.base_url.rstrip('/')}{path}",
            params=params or None,
            headers={"Authorization": f"Bearer {self._config.token}", "Accept": "application/json"},
        )
        if response.status_code >= 400:
            raise MercuryError(
                f"GET {path} -> {response.status_code}: {response.text[:300]}", status=response.status_code
            )
        with _readable(path):
            return _loads(response.text)

    def accounts(self) -> list[Account]:
        """Deposit accounts. Credit accounts are not here; see `credit_accounts`."""
        payload = self._get("/accounts")
        with _readable("/accounts"):
            return [parse_account(row) for row in _rows(payload, "accounts", "data")]

    def credit_accounts(self) -> list[CreditAccount]:
        """Credit accounts, which `/accounts` omits entirely."""
        payload = self._get("/credit")
        with _readable("/credit"):
            return [parse_credit_account(row) for row in _rows(payload, "accounts", "credit", "data")]

    def categories(self, *, page_size: int = CATEGORY_PAGE_SIZE) -> list[Category]:
        """Every custom category, read to the end of the list.

        Paged to exhaustion rather than read a page at a time, because a short
        read here is not a smaller answer -- it is a wrong one.
        `runningtab.mercury.categories` reconciles by name, so a category the read
        missed is reported as absent, and acting on that creates a duplicate.
        Two categories with one name is the single state that design then
        refuses to resolve, since it cannot tell which one transactions carry.

        Walks `page.nextPage`, which is the cursor Mercury's own envelope
        supplies, and stops on a page carrying nothing new -- a server that
        ignores the cursor repeats itself rather than paging forever.
        """
        collected: list[Category] = []
        seen: set[str] = set()
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": page_size, "order": "asc"}
            if cursor is not None:
                params["start_after"] = cursor
            payload = self._get("/categories", params)
            with _readable("/categories"):
                page = [parse_category(row) for row in _rows(payload, "categories", "data")]
                next_page = (payload.get("page") or {}).get("nextPage") if isinstance(payload, dict) else None
            fresh = 0
            for category in page:
                if category.id in seen:
                    continue
                seen.add(category.id)
                collected.append(category)
                fresh += 1
            if next_page is None or not fresh:
                break
            cursor = str(next_page)
        return collected

    def account_transactions(
        self, account_id: str, *, start: dt.datetime | None = None, page_size: int = TRANSACTION_PAGE_SIZE
    ) -> list[Transaction]:
        """Every transaction on one account, checked against the account's own `total`.

        Per-account rather than org-wide deliberately: the org-wide endpoint
        pages on an opaque cursor and reports no total, so a truncated read
        there is indistinguishable from a small account. Here `total` makes
        completeness assertable, and a short read raises. A response carrying no
        `total` is unreadable rather than assumed complete: guessing would turn
        the one check that makes this endpoint worth preferring into a shrug.

        `start` is how "every transaction" is said -- pass the account's
        `createdAt`. The endpoint's own default is the last 30 days, and a read
        that quietly stopped there would hand the balance invariant a short sum
        to blame on the bank.

        Counted by distinct transaction id, because `total` counts distinct
        transactions. A row the server repeats -- inside one page, or on a page
        it re-sends after a retry -- would otherwise carry a short read up to
        `total`, so the read would report itself complete while missing a
        transaction and handing a doubled charge to the balance invariant that
        exists to catch exactly that.
        """
        path = f"/account/{account_id}/transactions"
        collected: list[Transaction] = []
        seen: set[str] = set()
        total = 0
        while True:
            params: dict[str, Any] = {"limit": page_size, "offset": len(collected), "order": "asc"}
            if start is not None:
                params["start"] = start.date().isoformat()
            payload = self._get(path, params)
            with _readable(path):
                total = int(payload["total"])
                page = [parse_transaction(row, account_id) for row in _rows(payload, "transactions", "data")]
            fresh = 0
            for transaction in page:
                if transaction.id in seen:
                    continue
                seen.add(transaction.id)
                collected.append(transaction)
                fresh += 1
            if not fresh:
                # An empty page, or one carrying nothing this read has not
                # already got: either way the read has stopped advancing, and
                # asking again returns the same thing. Whether stopping here
                # leaves it short is the `total` check's answer, below -- not
                # something to page forever hoping to fix. The offset is walked
                # by distinct rows for the same reason: a page overlapping the
                # last one would otherwise step past as many positions as it
                # repeated, and the gap would never be asked for.
                break
            if len(collected) >= total:
                break
        if len(collected) != total:
            raise TruncatedReadError(
                f"account {account_id}: read {len(collected)} transactions but the server reported total={total}; "
                f"the balance invariant would blame the bank for a short read"
            )
        return collected

    def events(self, *, start_after: str | None = None, page_size: int = EVENT_PAGE_SIZE) -> list[Event]:
        """Every event after `start_after`, in the log's own ascending order.

        `start_after` is exclusive and the cursor is an event id, so resumption
        has no overlap window to reason about. Paging walks by handing the last
        id of one page back as the `start_after` of the next, which needs no
        knowledge of the envelope's own cursor field.
        """
        collected: list[Event] = []
        cursor = start_after
        while True:
            params: dict[str, Any] = {"limit": page_size}
            if cursor is not None:
                params["start_after"] = cursor
            payload = self._get("/events", params)
            with _readable("/events"):
                page = _rows(payload, "events", "data")
                parsed = [parse_event(row) for row in page]
            if not page:
                break
            collected.extend(parsed)
            cursor = parsed[-1].id
            if len(page) < page_size:
                break
        return collected

    def latest_event_id(self) -> str | None:
        """The newest event id, or None on an empty log.

        Read by walking the log to its end rather than by asking for it
        descending: the log's ordering parameter is documented as defaulting to
        ascending, and a reversed read is one more thing to be wrong about for
        a call made once per snapshot.
        """
        events = self.events()
        return events[-1].id if events else None
