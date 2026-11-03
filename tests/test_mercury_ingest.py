"""The applier: a snapshot, then the event log, with the balance invariant on top."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from icedcoffee_sync.frappe.client import Client, SiteConfig
from icedcoffee_sync.frappe.errors import PermissionError_, RateLimitError

from runningtab.mercury.client import MercuryClient, MercuryEnv, config_for
from runningtab.mercury.ingest import (
    IngestReport,
    apply_event,
    apply_transaction,
    read_everything,
    reconcile,
    run_snapshot,
    run_sync,
    uncoded_ages,
)
from runningtab.mercury.mapping import Mapping
from runningtab.mercury.models import parse_event
from runningtab.mercury.state import IngestState, NoCursorError, StaleCursorError
from runningtab.mercury.vouchers import Unneeded
from runningtab.period.lock import LockState
from tests.conftest import all_transactions, by_id, payload, transactions_from
from tests.test_mercury_posting import _Site

NOW = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC)

# The settled fixture row a person coded with Mercury's GL code field.
CODED_ROW = "txn_ach_utilities"
# Every settled fixture row a person has coded, and therefore every row a draft
# voucher is proposed for: the one above, and three coded by picking a custom
# category, which Mercury names with the chart's GL-code label.
CODED_ROWS = {CODED_ROW, "txn_wire_payroll", "txn_unmapped_cat", "txn_card_saas"}
# The outgoing half of each transfer between our own accounts -- checking to
# savings, and checking paying the card -- which identifies the one transfer
# voucher both halves are matched to.
TRANSFER_ROWS = {"txn_transfer_out", "txn_cc_payment"}
# Every row the fixtures draft a voucher for.
VOUCHER_ROWS = CODED_ROWS | TRANSFER_ROWS
SITE = SiteConfig(url="https://example.test", api_key="KEY", api_secret="SECRET", may_submit=True)  # noqa: S106

ROUTES = {
    "/accounts": "accounts",
    "/credit": "credit",
    "/account/acct_chk_1/transactions": "transactions_checking",
    "/account/acct_chk_2/transactions": "transactions_savings",
    "/account/acct_credit_1/transactions": "transactions_credit",
}

# Every settled row across the three fixture accounts. Each becomes exactly one
# Bank Transaction: both legs of each transfer, the categorised card spend, the
# row with a category nobody mapped, and the uncategorised checking movements.
SETTLED = {
    "txn_wire_payroll",
    "txn_ach_utilities",
    "txn_transfer_out",
    "txn_cc_payment",
    "txn_unmapped_cat",
    "txn_deposit",
    "txn_transfer_in",
    "txn_card_saas",
    "txn_card_ai",
    "txn_cc_payment_in",
}


class _Bank:
    """The Mercury sandbox as the fixtures describe it.

    `redeliver_events` models Mercury's at-least-once guarantee: with it on, the
    log ignores `start_after` and hands back everything every time, which is
    what a redelivered webhook or a retried page looks like from here.
    """

    def __init__(self, *, redeliver_events: bool = False, overrides: dict[str, Any] | None = None) -> None:
        self.redeliver_events = redeliver_events
        self.overrides = overrides or {}
        self.requests: list[httpx2.Request] = []

    def _payload(self, fixture: str) -> Any:
        return self.overrides.get(fixture, payload(fixture))

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v1")
        if path == "/events":
            events = self._payload("events")["events"]
            start_after = request.url.params.get("start_after")
            if start_after is not None and not self.redeliver_events:
                index = [e["id"] for e in events].index(start_after)
                events = events[index + 1 :]
            return httpx2.Response(200, json={"events": events})
        fixture = ROUTES.get(path)
        if fixture is None:
            return httpx2.Response(404, json={"error": path})
        return httpx2.Response(200, json=self._payload(fixture))


@pytest.fixture
def bank() -> _Bank:
    return _Bank()


@pytest.fixture
def mercury(bank: _Bank):
    with MercuryClient(
        config_for(MercuryEnv.SANDBOX, "SANDBOX-TOKEN"), transport=httpx2.MockTransport(bank.handler)
    ) as client:
        yield client


@pytest.fixture
def site() -> _Site:
    return _Site()


@pytest.fixture
def ledger(site: _Site):
    with Client(SITE, transport=httpx2.MockTransport(site.handler)) as client:
        yield client


@pytest.fixture
def state(tmp_path: pathlib.Path):
    with IngestState(tmp_path / "mercury.sqlite3") as ingest_state:
        yield ingest_state


def _mercury_for(bank: _Bank) -> MercuryClient:
    return MercuryClient(config_for(MercuryEnv.SANDBOX, "T"), transport=httpx2.MockTransport(bank.handler))


def documents(site: _Site, doctype: str) -> list[dict[str, Any]]:
    return [doc for (dt_, _), doc in site.documents.items() if dt_ == doctype]


def bank_rows(site: _Site) -> dict[str, dict[str, Any]]:
    return {doc["mercury_transaction_id"]: doc for doc in documents(site, "Bank Transaction")}


def vouchers(site: _Site) -> set[str]:
    """The Mercury transactions the ledger holds a draft Journal Entry for."""
    return {doc["mercury_transaction_id"] for doc in documents(site, "Journal Entry")}


def voucher_for(site: _Site, transaction_id: str) -> dict[str, Any]:
    """The one draft Journal Entry proposed beside a Mercury transaction."""
    (voucher,) = [doc for doc in documents(site, "Journal Entry") if doc["mercury_transaction_id"] == transaction_id]
    return voucher


# --- the balance invariant ------------------------------------------------


def test_an_account_reconciles_exactly() -> None:
    rows = transactions_from("transactions_checking", "acct_chk_1")
    check = reconcile("acct_chk_1", "checking", Decimal("4532.39"), rows)
    assert check.ok
    assert check.difference == 0


def test_pending_and_failed_rows_are_the_difference_the_bank_leaves_out() -> None:
    # Measured in the sandbox as an exact 80.00 gap: a pending -10.00 and a
    # failed -70.00 that `currentBalance` does not include.
    rows = transactions_from("transactions_checking", "acct_chk_1")
    everything = sum((t.amount for t in rows), Decimal(0))
    counted = sum((t.amount for t in rows if t.counts_toward_balance), Decimal(0))
    assert counted - everything == Decimal("80.00")


def test_a_missing_transaction_shows_up_as_a_balance_mismatch() -> None:
    rows = transactions_from("transactions_checking", "acct_chk_1")
    short = [t for t in rows if t.id != "txn_deposit"]
    check = reconcile("acct_chk_1", "checking", Decimal("4532.39"), short)
    assert not check.ok
    assert check.difference == Decimal("-10000.00")


def test_uncoded_settled_rows_are_aged_but_transfers_are_not_counted(own_account_ids) -> None:
    # Uncoded means neither a GL code nor a custom category at Mercury: the
    # person reconciling has no hint to go on and will have to code the row
    # somewhere. Transfers are left out because nobody codes a movement between
    # our own accounts. Mercury's automatic category on the AI card charge is
    # not a person's coding.
    uncoded = dict(uncoded_ages(all_transactions(), NOW, own_account_ids=own_account_ids))
    assert set(uncoded) == {"txn_deposit", "txn_card_ai"}
    assert uncoded["txn_deposit"] == 31


def test_a_row_coded_by_its_category_is_not_uncoded(own_account_ids) -> None:
    # The category is a GL-code label, so a person who picked one has coded the row.
    rows = [
        dataclasses.replace(t, custom_category="5215 - Phone & Internet (Verizon, Comcast)")
        if t.id == "txn_deposit"
        else t
        for t in all_transactions()
    ]
    assert "txn_deposit" not in dict(uncoded_ages(rows, NOW, own_account_ids=own_account_ids))


# --- snapshot -------------------------------------------------------------


def test_a_snapshot_posts_one_bank_transaction_per_settled_transaction(
    mercury, ledger, site, posting_ctx, state
) -> None:
    report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    assert set(bank_rows(site)) == SETTLED
    assert len(report.submitted) == len(SETTLED)
    # Each Journal Entry is a draft proposed beside a settled row a person has
    # coded -- never a second copy of a statement line.
    assert vouchers(site) == VOUCHER_ROWS


def test_a_snapshot_submits_every_bank_transaction_it_posts(mercury, ledger, site, posting_ctx, state) -> None:
    # Submitted, not drafted: the Bank Reconciliation Tool works on submitted
    # rows, and a Bank Transaction reaches the general ledger only when a
    # person reconciles it, so the bot submitting it commits nothing.
    run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    assert {doc["docstatus"] for doc in bank_rows(site).values()} == {1}


def test_a_dry_run_plans_the_submit_it_would_make(mercury, ledger, site, posting_ctx, state) -> None:
    report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW, dry_run=True)
    submits = [line for line in report.planned if line.startswith("submit Bank Transaction for ")]
    drafts = [line for line in report.planned if line.startswith("draft Journal Entry for ")]
    assert len(submits) == len(SETTLED)
    assert sorted(drafts) == sorted(f"draft Journal Entry for {row}" for row in VOUCHER_ROWS)
    assert len(report.planned) == len(submits) + len(drafts)


def test_each_leg_of_a_transfer_lands_on_its_own_bank_account(mercury, ledger, site, posting_ctx, state) -> None:
    # Two statement lines on two accounts, both posted. The Bank Reconciliation
    # Tool matches both to the one transfer voucher; pairing them here would
    # leave one account's statement a line short.
    run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    rows = bank_rows(site)
    assert (rows["txn_transfer_out"]["bank_account"], rows["txn_transfer_out"]["withdrawal"]) == (
        "Mercury Checking - T",
        47.61,
    )
    assert (rows["txn_transfer_in"]["bank_account"], rows["txn_transfer_in"]["deposit"]) == (
        "Mercury Savings - T",
        47.61,
    )
    assert rows["txn_cc_payment"]["bank_account"] == "Mercury Checking - T"
    assert rows["txn_cc_payment_in"]["bank_account"] == "Mercury IO Card - T"


def test_a_posted_row_reads_on_the_reconciliation_card_without_being_opened(
    mercury, ledger, site, posting_ctx, state
) -> None:
    # The Banking workspace shows the date, the reference, the description and
    # the amount. Everything else is a click away, and a person who has to
    # click has been asked to redo work the software already did.
    run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    rows = bank_rows(site)
    utilities = rows["txn_ach_utilities"]
    assert utilities["reference_number"] == "091000015549612"
    assert utilities["description"] == "5215 - Phone & Internet (Verizon, Comcast) · SONIC INTERNET"
    # No trace number, no GL code: the id stands in as the reference and the
    # description is exactly the bank's own text.
    assert rows["txn_deposit"]["reference_number"] == "txn_deposit"
    assert rows["txn_deposit"]["description"] == "ACME BICYCLES INV 1001"


def _checking_with_a_note(note: str) -> dict[str, Any]:
    """The checking account's transactions with a note coded onto the utilities ACH, as Mercury would report it."""
    body = payload("transactions_checking")
    for row in body["transactions"]:
        if row["id"] == "txn_ach_utilities":
            row["note"] = note
    return body


def test_a_note_coded_after_the_row_was_submitted_lands_without_refusing_it(
    mercury, ledger, site, posting_ctx, state
) -> None:
    # Coding at Mercury emits no event, so the sweep is what delivers it. The
    # description was fixed when the row was submitted and the custom field is
    # the live copy; a person's note must not be refused because our own
    # composition has moved on.
    run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    before = bank_rows(site)["txn_ach_utilities"]["description"]
    coded = _Bank(overrides={"transactions_checking": _checking_with_a_note("1Password Teams")})
    with _mercury_for(coded) as bank_client:
        report = run_sync(bank_client, ledger, posting_ctx, state, now=NOW)
    assert report.attention == []
    row = bank_rows(site)["txn_ach_utilities"]
    assert row["mercury_note"] == "1Password Teams"
    assert row["description"] == before
    assert row["docstatus"] == 1


def test_a_snapshot_checks_every_account_including_the_credit_one(mercury, ledger, posting_ctx, state) -> None:
    report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    assert [c.account_id for c in report.balances] == ["acct_chk_1", "acct_chk_2", "acct_credit_1"]
    assert report.balances_ok


def test_a_snapshot_records_where_in_the_log_that_state_was_true(mercury, ledger, posting_ctx, state) -> None:
    report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    assert report.cursor == "evt_0006"
    assert state.require_resumable(NOW).event_id == "evt_0006"


def test_the_cursor_is_read_before_the_transactions(bank, ledger, posting_ctx, state) -> None:
    # Taking it afterwards would silently drop every change made while the
    # snapshot was running; taking it first only means replaying a few events,
    # which is harmless because a transaction has exactly one document.
    with _mercury_for(bank) as mercury:
        run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    paths = [r.url.path for r in bank.requests]
    assert paths.index("/api/v1/events") < paths.index("/api/v1/account/acct_chk_1/transactions")


def test_a_snapshot_reports_the_uncoded_rows(mercury, ledger, posting_ctx, state) -> None:
    report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    assert {transaction_id for transaction_id, _ in report.uncoded} == {"txn_deposit", "txn_card_ai"}
    assert report.needs_attention


def test_a_row_coded_in_the_ledger_is_not_reported_uncoded(mercury, ledger, site, posting_ctx, state) -> None:
    # Mercury has no code for the deposit, but a person booked it in ERPNext:
    # a voucher carries its Mercury id. Nobody has to code it any more.
    site.documents[("Journal Entry", "JV-PERSON")] = {"mercury_transaction_id": "txn_deposit", "docstatus": 1}
    report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    assert {transaction_id for transaction_id, _ in report.uncoded} == {"txn_card_ai"}


def test_a_dry_sync_consumes_nothing_from_the_log(ledger, site, posting_ctx, state) -> None:
    # Including the events it decides not to apply: marking a skipped event
    # seen would make the real run that follows a dry run miss it.
    bank = _Bank()
    state.record_cursor(None, NOW)
    with _mercury_for(bank) as mercury:
        dry = run_sync(mercury, ledger, posting_ctx, state, now=NOW, dry_run=True)
        wet = run_sync(mercury, ledger, posting_ctx, state, now=NOW)
    assert state.seen_count() == wet.events_read
    assert dry.events_deduplicated == 0
    assert wet.events_deduplicated == 0
    # The plan matches what the real run then did, write for write: the dry run
    # buffers its place in memory, so the version guard reaches the same verdict
    # (evt_0004 does not advance txn_card_saas) instead of planning it twice.
    assert dry.events_out_of_order == wet.events_out_of_order == 1
    assert len(dry.planned) == len(wet.submitted) + len(wet.updated) + len(wet.drafted)


def test_re_snapshotting_changes_nothing_when_mercury_says_nothing_new(
    mercury, ledger, site, posting_ctx, state
) -> None:
    run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    second = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW + dt.timedelta(days=1))
    assert second.submitted == []
    assert second.updated == []
    assert set(bank_rows(site)) == SETTLED


def _hand_imported_utilities_row(site: _Site, name: str = "BT-HAND-1") -> str:
    """The Sonic ACH as Bank Statement Import left it: submitted, trace number in the reference, no Mercury fields."""
    site.documents[("Bank Transaction", name)] = {
        "date": "2026-08-05",
        "bank_account": "Mercury Checking - T",
        "company": "Transpire",
        "deposit": 0.0,
        "withdrawal": 100.0,
        "description": "Sonic",
        "reference_number": "091000015549612",
        "docstatus": 1,
    }
    return name


def test_a_snapshot_adopts_a_hand_imported_row_instead_of_posting_a_second(
    mercury, ledger, site, posting_ctx, state
) -> None:
    # Reported as its own outcome: it is neither a document this run created
    # nor one it amended, and a person reading the report wants to know which
    # of their imports the ingest now recognises.
    name = _hand_imported_utilities_row(site)
    report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    assert report.adopted == [name]
    assert len(report.submitted) == len(SETTLED) - 1
    assert len(documents(site, "Bank Transaction")) == len(SETTLED)
    adopted = site.documents[("Bank Transaction", name)]
    assert adopted["mercury_transaction_id"] == "txn_ach_utilities"
    assert adopted["docstatus"] == 1
    assert report.cursor == "evt_0006"


def test_a_dry_snapshot_plans_the_adoption_without_writing_it(mercury, ledger, site, posting_ctx, state) -> None:
    name = _hand_imported_utilities_row(site)
    report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW, dry_run=True)
    adoption = [line for line in report.planned if "adopt" in line]
    assert len(adoption) == 1
    assert name in adoption[0]
    assert "txn_ach_utilities" in adoption[0]
    assert "091000015549612" in adoption[0]
    assert "mercury_transaction_id" not in site.documents[("Bank Transaction", name)]
    assert len(documents(site, "Bank Transaction")) == 1


def test_two_hand_imported_rows_with_one_trace_number_want_a_person(mercury, ledger, site, posting_ctx, state) -> None:
    # Retryable: the duplicate import is a thing a person removes, and the
    # next run must then adopt the survivor rather than have retired the row.
    _hand_imported_utilities_row(site, "BT-A")
    _hand_imported_utilities_row(site, "BT-B")
    report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    reason = dict(report.attention)["txn_ach_utilities"]
    assert "BT-A" in reason
    assert "BT-B" in reason
    assert report.adopted == []
    assert len(documents(site, "Bank Transaction")) == len(SETTLED) + 1
    assert state.cursor() is None


def test_a_dry_run_writes_nothing_and_moves_nothing(mercury, ledger, site, posting_ctx, state) -> None:
    report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW, dry_run=True)
    # Every settled row, plus the draft voucher each coded row would get.
    assert len(set(report.planned)) == len(SETTLED) + len(VOUCHER_ROWS)
    assert site.documents == {}
    assert state.cursor() is None


# --- the event log --------------------------------------------------------


def test_a_sync_needs_a_snapshot_first(mercury, ledger, posting_ctx, state) -> None:
    with pytest.raises(NoCursorError):
        run_sync(mercury, ledger, posting_ctx, state, now=NOW)


def test_a_cursor_past_retention_refuses_rather_than_resuming_into_a_gap(mercury, ledger, posting_ctx, state) -> None:
    state.record_cursor("evt_0001", NOW - dt.timedelta(days=91))
    with pytest.raises(StaleCursorError):
        run_sync(mercury, ledger, posting_ctx, state, now=NOW)


def test_a_sync_applies_only_what_happened_after_the_cursor(mercury, ledger, site, posting_ctx, state) -> None:
    state.record_cursor("evt_0002", NOW)
    report = run_sync(mercury, ledger, posting_ctx, state, now=NOW)
    assert report.events_read == 4
    assert set(bank_rows(site)) == {"txn_card_saas"}
    assert report.cursor == "evt_0006"


def test_an_event_that_does_not_advance_the_version_is_not_applied_twice(mercury, ledger, posting_ctx, state) -> None:
    # evt_0004 repeats txn_card_saas at version 1 after evt_0003 recorded it.
    # `resourceVersion` is the ordering guard: apply only when it advances.
    state.record_cursor("evt_0002", NOW)
    report = run_sync(mercury, ledger, posting_ctx, state, now=NOW)
    assert report.events_out_of_order == 1
    assert len(report.submitted) == 1


def test_a_redelivered_event_is_deduplicated_on_its_id(ledger, site, posting_ctx, state) -> None:
    # Mercury names the event id as the idempotency key and delivers
    # at-least-once, so the same page arriving twice must do nothing the second
    # time -- not "the same thing again harmlessly", nothing.
    bank = _Bank(redeliver_events=True)
    state.record_cursor(None, NOW)
    with _mercury_for(bank) as mercury:
        first = run_sync(mercury, ledger, posting_ctx, state, now=NOW)
        second = run_sync(mercury, ledger, posting_ctx, state, now=NOW)
    assert first.events_deduplicated == 0
    assert second.events_deduplicated == 6
    assert second.submitted == []
    assert second.updated == []
    assert len(documents(site, "Bank Transaction")) == len(first.submitted)


def test_a_later_change_amends_the_row_it_already_has(ledger, site, posting_ctx, state) -> None:
    # evt_0001 creates the wire, evt_0002 changes it days later. The second
    # event must find the first row rather than post a second one.
    bank = _Bank()
    state.record_cursor(None, NOW)
    with _mercury_for(bank) as mercury:
        report = run_sync(mercury, ledger, posting_ctx, state, now=NOW)
    payroll = [d for d in documents(site, "Bank Transaction") if d["mercury_transaction_id"] == "txn_wire_payroll"]
    assert len(payroll) == 1
    assert payroll[0]["mercury_resource_version"] == 2
    assert len(report.updated) == 1


def test_a_deleted_transaction_is_raised_rather_than_acted_on(ledger, site, posting_ctx, state) -> None:
    bank = _Bank()
    state.record_cursor("evt_0004", NOW)
    with _mercury_for(bank) as mercury:
        report = run_sync(mercury, ledger, posting_ctx, state, now=NOW)
    assert dict(report.attention)["txn_pending_sub"].startswith("deleted at Mercury")
    assert report.needs_attention
    # Nothing was deleted or cancelled on our side. The draft vouchers for the
    # coded rows are this run's only writes, and a draft commits nothing.
    assert bank_rows(site) == {}
    assert vouchers(site) == VOUCHER_ROWS


def test_an_event_about_something_other_than_a_transaction_is_recorded_and_ignored(ledger, posting_ctx, state) -> None:
    bank = _Bank()
    state.record_cursor("evt_0005", NOW)
    with _mercury_for(bank) as mercury:
        report = run_sync(mercury, ledger, posting_ctx, state, now=NOW)
    assert report.events_read == 1
    assert report.events_applied == 0
    assert state.has_seen("evt_0006")


def test_the_cursor_only_moves_after_the_events_are_applied(ledger, posting_ctx, state) -> None:
    bank = _Bank()
    state.record_cursor("evt_0002", NOW)
    with _mercury_for(bank) as mercury:
        run_sync(mercury, ledger, posting_ctx, state, now=NOW + dt.timedelta(hours=1))
    cursor = state.require_resumable(NOW + dt.timedelta(hours=1))
    assert cursor.event_id == "evt_0006"
    assert cursor.synced_at == NOW + dt.timedelta(hours=1)


def test_an_empty_page_leaves_the_cursor_where_it_was(ledger, posting_ctx, state) -> None:
    bank = _Bank()
    state.record_cursor("evt_0006", NOW)
    with _mercury_for(bank) as mercury:
        report = run_sync(mercury, ledger, posting_ctx, state, now=NOW)
    assert report.events_read == 0
    assert report.cursor == "evt_0006"


def test_a_webhook_delivery_reaches_the_same_applier_as_a_pulled_event(
    mercury, ledger, site, posting_ctx, state
) -> None:
    # The receiver is not built yet. This is the seam it will use: parse the
    # delivery, hand it to `apply_event`, and the guards do the rest -- so a
    # webhook and a later pull of the same event are idempotent with respect to
    # each other without any coordination between them.
    delivery = json.loads(
        json.dumps({
            "id": "evt_0003",
            "resourceType": "transaction",
            "resourceId": "txn_card_saas",
            "resourceVersion": 1,
            "operationType": "create",
        })
    )
    transactions, _, _ = read_everything(mercury)
    report = IngestReport()
    apply_event(parse_event(delivery), by_id(transactions), posting_ctx, ledger, state, report, now=NOW)
    assert len(report.submitted) == 1

    # The pull that follows sees the same event id and does nothing.
    state.record_cursor("evt_0002", NOW)
    pulled = run_sync(mercury, ledger, posting_ctx, state, now=NOW)
    assert pulled.events_deduplicated == 1
    assert pulled.submitted == []
    assert len(documents(site, "Bank Transaction")) == 1


def _ctx_without_the_cards_bank_account(posting_ctx):
    """A context whose mapping names no Bank Account for the credit account, so its rows cannot post."""
    mapping = posting_ctx.mapping
    crippled = Mapping(
        accounts=mapping.accounts,
        bank_accounts={k: v for k, v in mapping.bank_accounts.items() if k != "acct_credit_1"},
    )
    return dataclasses.replace(posting_ctx, mapping=crippled)


def test_an_event_whose_transaction_cannot_post_holds_the_cursor(ledger, posting_ctx, state) -> None:
    # Marking it seen and stepping the cursor over it would retire the event
    # permanently while the ledger never received the transaction. The
    # configuration gets fixed and the next run has to find it again.
    bank = _Bank()
    ctx = _ctx_without_the_cards_bank_account(posting_ctx)
    state.record_cursor("evt_0002", NOW)
    with _mercury_for(bank) as mercury:
        report = run_sync(mercury, ledger, ctx, state, now=NOW)
    assert report.cursor == "evt_0002"
    assert state.has_seen("evt_0003") is False
    assert any("has no [bank_accounts] entry" in reason for _, reason in report.attention)


def test_a_transfer_leg_posts_without_waiting_for_its_mirror(ledger, site, posting_ctx) -> None:
    # Each leg is a statement line on its own account, so the outflow posts
    # whether or not the savings side has been read yet.
    outflow = by_id(transactions_from("transactions_checking", "acct_chk_1"))["txn_transfer_out"]
    report = IngestReport()
    assert apply_transaction(outflow, posting_ctx, ledger, report) is True
    assert set(bank_rows(site)) == {"txn_transfer_out"}
    assert report.attention == []


def test_an_event_for_a_transaction_we_cannot_see_is_retryable(ledger, posting_ctx, state) -> None:
    # No second event is coming for it, so retiring this one makes the
    # transaction unreachable the moment account visibility is fixed.
    event = parse_event({
        "id": "evt_9999",
        "resourceType": "transaction",
        "resourceId": "txn_invisible",
        "resourceVersion": 1,
        "operationType": "update",
    })
    report = IngestReport()
    assert apply_event(event, {}, posting_ctx, ledger, state, report, now=NOW) is False
    assert state.has_seen("evt_9999") is False
    assert "absent from the accounts we read" in dict(report.attention)["txn_invisible"]


def test_a_held_cursor_does_not_look_freshly_synced(ledger, posting_ctx, state) -> None:
    # Re-stamping a cursor that did not move would make a stuck ingest look
    # caught up forever, and the 90-day retention guard measures from the last
    # time the log was actually caught up.
    bank = _Bank()
    ctx = _ctx_without_the_cards_bank_account(posting_ctx)
    stamped = NOW - dt.timedelta(days=10)
    state.record_cursor("evt_0002", stamped)
    with _mercury_for(bank) as mercury:
        run_sync(mercury, ledger, ctx, state, now=NOW)
    cursor = state.cursor()
    assert cursor is not None
    assert cursor.synced_at == stamped


def test_a_snapshot_that_could_not_post_everything_records_no_cursor(ledger, posting_ctx, state) -> None:
    # Those transactions emit no further events, so a recorded cursor would put
    # them permanently out of reach of every later sync.
    bank = _Bank()
    ctx = _ctx_without_the_cards_bank_account(posting_ctx)
    with _mercury_for(bank) as mercury:
        report = run_snapshot(mercury, ledger, ctx, state, now=NOW)
    assert state.cursor() is None
    assert report.cursor is None
    assert any(key == "cursor" for key, _ in report.attention)


def test_a_dry_apply_event_leaves_a_real_state_untouched(mercury, ledger, posting_ctx, state) -> None:
    # The documented single-event entry point, handed a writable state. Leaving
    # the buffering to the caller means a webhook consumer that passes
    # `dry_run=True` retires events the ledger never received.
    transactions, _, _ = read_everything(mercury)
    event = parse_event(payload("events")["events"][0])
    apply_event(event, by_id(transactions), posting_ctx, ledger, state, IngestReport(), now=NOW, dry_run=True)
    assert state.seen_count() == 0
    assert state.version_of(event.resource_id) is None


def test_a_run_that_leaves_the_ledger_incomplete_wants_a_person() -> None:
    assert IngestReport().needs_attention is False
    assert IngestReport(attention=[("txn_x", "deleted at Mercury")]).needs_attention
    # A settled row with no GL code has no hint for the person reconciling; it
    # will have to be coded somewhere, and the run says so.
    assert IngestReport(uncoded=[("txn_x", 12)]).needs_attention
    # A closed-period row wants its quarter reopened, which only a person can do.
    assert IngestReport(closed=["txn_x 2026-08-06 5215: closed period"]).needs_attention
    # A draft nobody needs any more is still a document a person deletes.
    assert IngestReport(
        unneeded=[Unneeded("txn_x", "ACC-JV-0001", "its Bank Transaction is reconciled")]
    ).needs_attention


def _bank_whose_checking_balance_is_a_cent_out() -> _Bank:
    """The sandbox with one account's reported balance moved, so the read cannot reconcile."""
    accounts = payload("accounts")
    wrong = {"accounts": [{**accounts["accounts"][0], "currentBalance": 4532.40}, accounts["accounts"][1]]}
    return _Bank(overrides={"accounts": wrong})


def test_a_balance_that_does_not_reconcile_is_surfaced(ledger, posting_ctx, state) -> None:
    bank = _bank_whose_checking_balance_is_a_cent_out()
    with _mercury_for(bank) as mercury:
        report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    assert not report.balances_ok
    assert report.needs_attention
    assert report.balances[0].difference == Decimal("-0.01")


def test_a_snapshot_that_read_no_accounts_records_no_cursor(ledger, posting_ctx, state) -> None:
    # Every account check passes vacuously over an empty inventory: nothing
    # failed to post, and no balance disagreed. A cursor recorded there covers
    # transactions the run never looked at, and once account visibility is
    # fixed no event brings them back.
    bank = _Bank(overrides={"accounts": {"accounts": []}, "credit": {"accounts": []}})
    with _mercury_for(bank) as mercury:
        report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    assert report.balances_ok
    assert state.cursor() is None
    assert "no account was read" in dict(report.attention)["cursor"]


def test_a_snapshot_holding_a_status_nobody_has_classified_records_no_cursor(ledger, posting_ctx, state) -> None:
    # The row is fine and our configuration is short. Retiring it would mean
    # that adding the status later never posts it: it emits no further events,
    # so only another snapshot would ever reach it again.
    checking = payload("transactions_checking")
    rows = checking["transactions"]
    # A settled row, so the balance still reconciles exactly and the cursor is
    # withheld for this reason and no other.
    unclassified = [{**row, "status": "teleported"} if row["id"] == "txn_deposit" else row for row in rows]
    bank = _Bank(overrides={"transactions_checking": {**checking, "transactions": unclassified}})
    with _mercury_for(bank) as mercury:
        report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    assert report.balances_ok
    assert state.cursor() is None
    assert "teleported" in dict(report.attention)["txn_deposit"]
    assert "could not be posted" in dict(report.attention)["cursor"]


def test_a_snapshot_whose_balances_do_not_reconcile_records_no_cursor(ledger, posting_ctx, state) -> None:
    # A mismatch is evidence the read was short, and the rows a short read
    # missed emit no further events -- so a cursor recorded over them puts them
    # permanently out of reach of every later sync. Exactly the reason a
    # snapshot that could not post everything records nothing either.
    bank = _bank_whose_checking_balance_is_a_cent_out()
    with _mercury_for(bank) as mercury:
        report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    assert state.cursor() is None
    assert report.cursor is None
    assert "does not reconcile" in dict(report.attention)["cursor"]


def test_a_snapshot_reports_a_row_whose_transaction_mercury_dropped(mercury, ledger, site, posting_ctx, state) -> None:
    # The applier only ever walks forward, from Mercury into the ledger, so a
    # transaction that disappeared -- a pending charge cancelled while the log
    # aged past its 90-day retention -- leaves its Bank Transaction standing
    # and no loop here ever looks at it. Snapshot is the documented recovery
    # from that gap, which makes it the one run that can notice.
    #
    # Submitted, because that is the only kind of row this ingest posts, and a
    # lookup that only saw drafts would find no orphan at all.
    run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    ledger.insert(
        "Bank Transaction",
        {"mercury_transaction_id": "txn_cancelled_in_the_gap", "company": "Transpire", "docstatus": 1},
    )

    later = NOW + dt.timedelta(days=1)
    report = run_snapshot(mercury, ledger, posting_ctx, state, now=later)

    assert [tid for tid, _ in report.orphans] == ["txn_cancelled_in_the_gap"]
    assert report.needs_attention
    # Held, not merely reported: moving the cursor settles the question by
    # silence -- the row stays and nothing looks for it again.
    assert report.cursor is None
    assert state.cursor().synced_at == NOW, "the held cursor was re-stamped"
    assert "no longer returns" in dict(report.attention)["cursor"]


def test_a_snapshot_whose_transactions_all_still_exist_reports_no_orphans(mercury, ledger, posting_ctx, state) -> None:
    """The ordinary re-snapshot: every row has its transaction, cursor moves."""
    run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW + dt.timedelta(days=1))
    assert report.orphans == []
    assert report.cursor == "evt_0006"


def test_a_snapshot_that_read_no_accounts_does_not_call_every_row_an_orphan(
    mercury, ledger, posting_ctx, state
) -> None:
    # Losing account visibility means Mercury returned nothing, which is not
    # the same as Mercury no longer holding these transactions. Reporting the
    # whole ledger as orphaned would bury the one thing that is actually wrong.
    run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    blind = _Bank(overrides={"accounts": {"accounts": []}, "credit": {"accounts": []}})
    with _mercury_for(blind) as no_accounts:
        report = run_snapshot(no_accounts, ledger, posting_ctx, state, now=NOW)
    assert report.orphans == []
    assert "no account was read" in dict(report.attention)["cursor"]


def test_a_sync_that_read_no_accounts_holds_the_cursor(ledger, posting_ctx, state) -> None:
    # A quiet hour and a token that lost its accounts look identical from the
    # log: no events, nothing posted, every balance check vacuously fine. The
    # cursor moving there refreshes the retention guard too, so the ingest
    # reports healthy for as long as the access stays broken.
    state.record_cursor("evt_0006", NOW)
    blind = _Bank(overrides={"accounts": {"accounts": []}, "credit": {"accounts": []}})
    with _mercury_for(blind) as mercury:
        report = run_sync(mercury, ledger, posting_ctx, state, now=NOW + dt.timedelta(hours=1))

    assert report.balances_ok, "vacuously true over no accounts -- which is the trap"
    assert report.needs_attention
    assert "no account was read" in dict(report.attention)["accounts"]
    # The timestamp is held as well as the id, so the retention guard still
    # measures from the last run that actually caught up.
    assert state.require_resumable(NOW + dt.timedelta(hours=1)).synced_at == NOW


# --- a write the ledger refuses -------------------------------------------


# What the site said on the first production run, 2026-09-21, with the custom
# field installed without `allow_on_submit`: one row of seventeen, phrased for a
# browser.
REFUSAL = (
    "Not allowed to change <strong>Mercury GL Code</strong> after submission "
    "from <strong>None</strong> to <strong>3110 - Capital Contribution - Member 1</strong>"
)


class _SiteRefusingOneWrite(_Site):
    """The stand-in ERPNext, refusing every write to one document.

    Answers with Frappe's own error envelope through the real client rather
    than raising a `FrappeError` in the test body, so what the ingest handles
    is what a site actually produces.
    """

    def __init__(self, name: str, message: str) -> None:
        super().__init__()
        self.refused = name
        self.message = message

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "PUT" and request.url.path.split("/")[-1] == self.refused:
            self.requests.append(request)
            return httpx2.Response(417, json={"errors": [{"type": "ValidationError", "message": self.message}]})
        return super().handler(request)


def _stamped_transaction_ids(site: _Site) -> set[str]:
    """The Mercury ids the ledger's Bank Transactions carry, skipping rows no run has stamped."""
    field = "mercury_transaction_id"
    return {doc[field] for doc in documents(site, "Bank Transaction") if field in doc}


def test_a_refused_ledger_write_is_a_report_line_not_the_end_of_the_run(posting_ctx, state) -> None:
    # The first production run died on a Rich traceback here, having attempted
    # exactly one transaction. A traceback says nothing about the other sixteen
    # rows, so a refusal is one line in the report and the run carries on.
    site = _SiteRefusingOneWrite("BT-HAND-1", REFUSAL)
    name = _hand_imported_utilities_row(site)
    with (
        Client(SITE, transport=httpx2.MockTransport(site.handler)) as ledger,
        _mercury_for(_Bank()) as mercury,
    ):
        report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)

    assert report.adopted == []
    assert _stamped_transaction_ids(site) == SETTLED - {"txn_ach_utilities"}
    assert dict(report.attention)["txn_ach_utilities"] == (
        f"adopt Bank Transaction {name} refused by the ledger: Not allowed to change Mercury GL Code "
        "after submission from None to 3110 - Capital Contribution - Member 1"
    )
    assert report.needs_attention
    # Unfinished business, so the snapshot claims no state it did not establish.
    assert report.cursor is None
    assert state.cursor() is None


class _SiteUnreachableOnWrite(_Site):
    """The stand-in ERPNext, readable but with every write failing in the transport.

    The reads have to work: a connection that fails on the lookup never reaches
    the write at all, so a test that broke both would pass however wide the
    handler around the write is.
    """

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if request.method in {"POST", "PUT"}:
            self.requests.append(request)
            raise httpx2.ConnectError("connection refused")
        return super().handler(request)


def test_a_ledger_we_cannot_reach_is_not_one_refused_row(posting_ctx, state) -> None:
    # A run that cannot reach the site has nothing to say about any particular
    # row, and turning that into seventeen identical report lines would bury
    # the one fact there is. It ends instead.
    site = _SiteUnreachableOnWrite()
    with (
        Client(SITE, transport=httpx2.MockTransport(site.handler)) as ledger,
        _mercury_for(_Bank()) as mercury,
        pytest.raises(httpx2.ConnectError),
    ):
        run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    assert state.cursor() is None


class _SiteThrottlingEveryWrite(_Site):
    """The stand-in ERPNext, readable but answering every write with 429."""

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if request.method in {"POST", "PUT"}:
            self.requests.append(request)
            return httpx2.Response(429, json={"errors": [{"message": "Too Many Requests"}]})
        return super().handler(request)


def test_a_rate_limited_ledger_is_not_one_refused_row_either(posting_ctx, state) -> None:
    # The site is telling the whole run to slow down rather than refusing this
    # document, so every write after it meets the same answer and a line each
    # would bury the single real problem.
    site = _SiteThrottlingEveryWrite()
    with (
        Client(SITE, transport=httpx2.MockTransport(site.handler)) as ledger,
        _mercury_for(_Bank()) as mercury,
        pytest.raises(RateLimitError),
    ):
        run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    assert state.cursor() is None
    # Ended on the first write, not after seventeen of them.
    assert [r.method for r in site.requests if r.method in {"POST", "PUT"}] == ["POST"]


class _SiteRefusingTheCredential(_Site):
    """The stand-in ERPNext, readable but refusing the bot's credential on every write."""

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if request.method in {"POST", "PUT"}:
            self.requests.append(request)
            return httpx2.Response(
                403,
                json={
                    "errors": [
                        {
                            "type": "PermissionError",
                            "message": "Not permitted to create <strong>Bank Transaction</strong>",
                        }
                    ]
                },
            )
        return super().handler(request)


def test_a_credential_the_site_refuses_ends_the_run_too(posting_ctx, state) -> None:
    # A revoked key or a lost submit grant answers every write the same way,
    # so it is one problem the whole run has rather than seventeen rows the
    # site declined to accept -- and a line each would hide which it is.
    site = _SiteRefusingTheCredential()
    with (
        Client(SITE, transport=httpx2.MockTransport(site.handler)) as ledger,
        _mercury_for(_Bank()) as mercury,
        pytest.raises(PermissionError_),
    ):
        run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    assert state.cursor() is None
    assert [r.method for r in site.requests if r.method in {"POST", "PUT"}] == ["POST"]


# --- the hint sweep -------------------------------------------------------


def _bank_that_recoded_the_utilities_ach(
    gl_code: str | None = "5210 - Software & Subscriptions", note: str | None = "wifi, not utilities -- CF"
) -> _Bank:
    """The sandbox after a person coded the Sonic ACH differently in Mercury's dashboard.

    The event list is untouched, because that is the fact this models: a GL
    code or note edited at the bank emits no event and bumps no
    `resourceVersion`, so nothing in the log ever mentions it.
    """
    checking = payload("transactions_checking")
    rows = [
        {
            **row,
            "generalLedgerCodeName": gl_code,
            "glAllocations": [{"amount": row["amount"], "glCodeName": gl_code}],
            "note": note,
        }
        if row["id"] == "txn_ach_utilities"
        else row
        for row in checking["transactions"]
    ]
    return _Bank(overrides={"transactions_checking": {**checking, "transactions": rows}})


def test_a_gl_code_coded_after_the_row_posted_reaches_the_ledger_with_no_event(
    mercury, ledger, site, posting_ctx, state
) -> None:
    # The whole point of the sweep. Coding a transaction at Mercury is a
    # person's work; a sync that waited for the log would never hear about it
    # and would go on reporting success while the coding never arrived.
    run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    assert bank_rows(site)["txn_ach_utilities"]["mercury_gl_code"] == "5215 - Phone & Internet (Verizon, Comcast)"

    with _mercury_for(_bank_that_recoded_the_utilities_ach()) as recoded:
        report = run_sync(recoded, ledger, posting_ctx, state, now=NOW)

    assert report.events_read == 0
    row = bank_rows(site)["txn_ach_utilities"]
    assert row["mercury_gl_code"] == "5210 - Software & Subscriptions"
    assert row["mercury_note"] == "wifi, not utilities -- CF"
    # No event said anything, so no resource version may be claimed for one.
    assert "mercury_resource_version" not in row
    # Two writes, both from the same unannounced edit: the hint on the
    # statement line, and the draft voucher proposed from it.
    assert len(report.updated) == 2
    assert report.submitted == []
    voucher = voucher_for(site, CODED_ROW)
    assert voucher["accounts"][1]["account"] == "5210 - Payroll - Salaries & Wages - T"
    assert voucher["remark"].startswith("wifi, not utilities -- CF · 5210 - Software & Subscriptions")


def test_a_sweep_over_rows_nobody_touched_writes_nothing(mercury, ledger, site, posting_ctx, state) -> None:
    # The sweep re-plans every settled row every run, so the quiet case has to
    # be genuinely quiet: an ingest that rewrote every row on every run would
    # bury a real change in its own noise.
    run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    before = {name: dict(doc) for name, doc in site.documents.items()}
    site.requests.clear()

    report = run_sync(mercury, ledger, posting_ctx, state, now=NOW)

    assert [r.method for r in site.requests if r.method in {"POST", "PUT"}] == []
    assert site.documents == before
    assert report.updated == []
    assert report.submitted == []
    assert report.attention == []


def test_a_dry_sync_plans_the_hint_refresh_without_writing_it(mercury, ledger, site, posting_ctx, state) -> None:
    run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    site.requests.clear()

    with _mercury_for(_bank_that_recoded_the_utilities_ach()) as recoded:
        report = run_sync(recoded, ledger, posting_ctx, state, now=NOW, dry_run=True)

    assert [line for line in report.planned if "txn_ach_utilities" in line] == report.planned
    assert report.planned[0].startswith("update Bank Transaction ")
    assert report.updated == []
    assert [r.method for r in site.requests if r.method in {"POST", "PUT"}] == []
    assert bank_rows(site)["txn_ach_utilities"]["mercury_gl_code"] == "5215 - Phone & Internet (Verizon, Comcast)"


def test_the_sweep_reports_a_settled_transaction_with_no_row_rather_than_posting_it(
    mercury, ledger, site, posting_ctx, state
) -> None:
    # A settled transaction the ledger has no row for means an event went
    # missing. Inserting from the sweep would hide that -- the row would appear
    # with nothing in the log ever having said so -- and re-snapshotting is the
    # documented repair.
    state.record_cursor("evt_0006", NOW)

    report = run_sync(mercury, ledger, posting_ctx, state, now=NOW)

    assert report.events_read == 0
    # The sweep posts no statement line; the draft voucher it proposes beside
    # the coded row is not one, and commits nothing.
    assert bank_rows(site) == {}
    assert report.submitted == []
    assert report.adopted == []
    assert {transaction_id for transaction_id, _ in report.attention} >= SETTLED
    assert "snapshot" in dict(report.attention)["txn_ach_utilities"]
    assert report.needs_attention


# --- the draft voucher beside a coded row ---------------------------------


def test_a_snapshot_reports_the_voucher_it_drafted_apart_from_what_it_submitted(
    mercury, ledger, site, posting_ctx, state
) -> None:
    # Its own bucket: a submitted Bank Transaction is the bank's record and a
    # draft voucher is a proposal, and a person reading the report has to be
    # able to tell which of the two the run produced.
    report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    assert len(report.drafted) == len(VOUCHER_ROWS)
    assert set(report.drafted) == {name for (doctype, name) in site.documents if doctype == "Journal Entry"}
    assert len(report.submitted) == len(SETTLED)


def test_a_drafted_voucher_is_never_submitted(mercury, ledger, site, posting_ctx, state) -> None:
    # The one thing this pass must never do. `0` is the default when the key is
    # absent, so its absence is the guarantee, and the bot holds no submit on
    # Journal Entry for the server to honour either.
    run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    assert {doc["docstatus"] for doc in documents(site, "Journal Entry")} == {0}
    written = [r for r in site.requests if r.method in {"POST", "PUT"} and "Journal Entry" in r.url.path]
    assert written
    assert all("docstatus" not in json.loads(r.content) for r in written)


def test_a_sync_drafts_the_voucher_for_a_row_coded_after_it_posted(mercury, ledger, site, posting_ctx, state) -> None:
    # A person codes a transaction at Mercury with no event and no version
    # bump, so the voucher arrives by the same sweep the GL-code hint does.
    uncoded = _bank_that_recoded_the_utilities_ach(gl_code=None, note=None)
    with _mercury_for(uncoded) as before:
        run_snapshot(before, ledger, posting_ctx, state, now=NOW)
    assert vouchers(site) == VOUCHER_ROWS - {CODED_ROW}

    with _mercury_for(_bank_that_recoded_the_utilities_ach()) as coded:
        report = run_sync(coded, ledger, posting_ctx, state, now=NOW)

    assert report.events_read == 0
    assert len(report.drafted) == 1
    assert vouchers(site) == VOUCHER_ROWS


# --- the ingest respects a closed quarter ---------------------------------


def test_a_snapshot_posts_the_bank_transaction_but_drafts_no_voucher_in_a_closed_quarter(
    mercury, ledger, site, posting_ctx, state
) -> None:
    # CODED_ROW's accounting date is 2026-08-06 -- on the lock, which is
    # closed, as is the payroll wire's 2026-08-04. The Bank Transaction is the
    # bank's record and still posts; the voucher beside it is what a closed
    # quarter refuses. The rows dated after the lock, transfers included,
    # still get theirs.
    closed_ctx = dataclasses.replace(posting_ctx, lock=LockState(closed_through=dt.date(2026, 8, 6), bypass_role=None))
    report = run_snapshot(mercury, ledger, closed_ctx, state, now=NOW)
    assert CODED_ROW in bank_rows(site)
    assert vouchers(site) == VOUCHER_ROWS - {CODED_ROW, "txn_wire_payroll"}
    assert len(report.drafted) == len(VOUCHER_ROWS) - 2
    assert len(report.closed) == 2
    (closed,) = [line for line in report.closed if line.startswith(f"{CODED_ROW} ")]
    assert closed.startswith(f"{CODED_ROW} 2026-08-06 ")
    assert "closed period" in closed
    assert "reopen 2026Q3 in ERPNext" in closed
    assert "open quarter" not in closed
    assert report.needs_attention


def test_a_sync_reports_a_row_coded_after_the_quarter_closed(mercury, ledger, site, posting_ctx, state) -> None:
    # The GL-code hint sweep runs on every settled row, closed quarter or not.
    # A person coding a row after the close still gets an honest answer: no
    # voucher, told why.
    uncoded = _bank_that_recoded_the_utilities_ach(gl_code=None, note=None)
    with _mercury_for(uncoded) as before:
        run_snapshot(before, ledger, posting_ctx, state, now=NOW)
    assert CODED_ROW not in vouchers(site)

    closed_ctx = dataclasses.replace(posting_ctx, lock=LockState(closed_through=dt.date(2026, 8, 6), bypass_role=None))
    with _mercury_for(_bank_that_recoded_the_utilities_ach()) as coded:
        report = run_sync(coded, ledger, closed_ctx, state, now=NOW)

    assert CODED_ROW not in vouchers(site)
    assert report.drafted == []
    assert any(line.startswith(f"{CODED_ROW} ") for line in report.closed)
    assert report.needs_attention


def test_a_snapshot_with_no_lock_known_drafts_the_voucher_as_before(mercury, ledger, site, posting_ctx, state) -> None:
    # `posting_ctx` carries the default lock -- no date known -- which is the
    # same shape a Company not yet re-provisioned with the permlevel-1 grant
    # reads back. Behaviour is unchanged from before this quarter-close work.
    report = run_snapshot(mercury, ledger, posting_ctx, state, now=NOW)
    assert len(report.drafted) == len(VOUCHER_ROWS)
    assert report.closed == []
