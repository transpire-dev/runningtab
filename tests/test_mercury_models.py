"""Parsing captured Mercury payloads."""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

import pytest

from runningtab.mercury.models import (
    CategorySource,
    MercuryPayloadError,
    Operation,
    Status,
    parse_account,
    parse_credit_account,
    parse_event,
    parse_status,
    parse_transaction,
)
from tests.conftest import by_id, payload, transactions_from


def checking() -> dict:
    return by_id(transactions_from("transactions_checking", "acct_chk_1"))


def credit() -> dict:
    return by_id(transactions_from("transactions_credit", "acct_credit_1"))


def test_amounts_are_exact_decimals_not_floats() -> None:
    transaction = checking()["txn_transfer_out"]
    assert isinstance(transaction.amount, Decimal)
    # The wire value is a JSON float. Via `str()` this is exactly -47.61; via
    # `Decimal(float)` it would be -47.6099999999999994315658113919198513031005859375.
    assert str(transaction.amount) == "-47.61"


def test_a_persons_category_wins_over_mercurys() -> None:
    transaction = checking()["txn_wire_payroll"]
    assert transaction.category == "5210 - Salaries & Wages"
    assert transaction.category_source is CategorySource.CUSTOM


def test_the_gl_code_is_mercurys_gl_code_when_a_person_set_one() -> None:
    # Set explicitly, it wins over the category: it is the more deliberate act.
    transaction = dataclasses.replace(checking()["txn_ach_utilities"], custom_category="5204 - Office Software")
    assert transaction.gl_code == "5215 - Phone & Internet (Verizon, Comcast)"


def test_the_gl_code_falls_back_to_the_persons_category() -> None:
    # Mercury's custom categories are the chart's GL-code labels, word for
    # word, and its dropdown offers nothing else; coding a row at the card
    # reader picks a category and leaves `generalLedgerCodeName` empty.
    transaction = dataclasses.replace(
        checking()["txn_deposit"], custom_category="5110 - Production Hosting (AWS / Fastly)"
    )
    assert transaction.gl_code_name is None
    assert transaction.gl_code == "5110 - Production Hosting (AWS / Fastly)"


def test_mercurys_automatic_category_is_not_a_gl_code() -> None:
    # Nobody chose it, and it names Mercury's own taxonomy rather than the chart.
    transaction = credit()["txn_card_ai"]
    assert transaction.category_source is CategorySource.MERCURY
    assert transaction.gl_code is None


def test_the_automatic_category_is_read_from_inside_category_data() -> None:
    # The sandbox survey counted `mercuryCategory` as top-level while the Events
    # field set names it `categoryData.mercuryCategory`. Both spellings parse.
    transaction = credit()["txn_card_ai"]
    assert transaction.category == "Research & Development Tools"
    assert transaction.category_source is CategorySource.MERCURY


def test_a_transaction_with_no_category_reports_no_source() -> None:
    transaction = checking()["txn_deposit"]
    assert transaction.category is None
    assert transaction.category_source is None
    assert transaction.custom_category_id is None


def test_the_custom_categorys_uuid_is_read_alongside_its_name() -> None:
    # The UUID, not the name, answers "does any transaction carry this
    # category" -- two categories can wear one name, and a rename moves the
    # name without moving the row. `categories apply --prune` deletes on that
    # answer, so a category id that failed to parse would read as unused.
    transaction = checking()["txn_wire_payroll"]
    assert transaction.custom_category_id == "cat_payroll"


def test_the_real_flat_category_data_parses() -> None:
    # What the production organisation actually sends, measured 2026-09-21:
    # `categoryData` IS the category -- id, name and the three visibility flags,
    # with no `customCategory` wrapper. The ingest fixtures were built around
    # the nested spelling, so this shape has its own test until a real capture
    # replaces them; without it, the fixtures agree with themselves and
    # `custom_category` is None on every real row.
    transaction = parse_transaction(
        {
            "id": "t1",
            "amount": 1,
            "status": "sent",
            "createdAt": "2026-09-01T00:00:00.000Z",
            "categoryData": {
                "id": "cat-flat",
                "name": "Software & Subscriptions",
                "visibleForCardSpend": True,
                "visibleForReimbursements": True,
                "visibleForOther": True,
            },
        },
        "acct-1",
    )
    assert transaction.custom_category_id == "cat-flat"
    assert transaction.custom_category == "Software & Subscriptions"
    assert transaction.category_source is CategorySource.CUSTOM


def test_the_nested_category_data_spelling_still_parses_and_wins() -> None:
    # The ingest fixtures carry `categoryData.customCategory`, and the Events
    # field set names that path too, so it is read as well -- and preferred
    # when both are present, because then the outer object is a wrapper rather
    # than the category.
    nested = parse_transaction(
        {
            "id": "t2",
            "amount": 1,
            "status": "sent",
            "createdAt": "2026-09-01T00:00:00.000Z",
            "categoryData": {
                "id": "cat-flat",
                "name": "Outer",
                "customCategory": {"id": "cat-nested", "name": "Insurance"},
            },
        },
        "acct-1",
    )
    assert nested.custom_category_id == "cat-nested"
    assert nested.custom_category == "Insurance"


def test_the_trace_number_is_read_when_mercury_sends_one() -> None:
    # `trackingNumber` is the ACH trace number -- the same string Mercury's
    # statement export carries as `Tracking ID`, which is how a row imported by
    # hand is recognised. A card charge never went through the ACH network and
    # carries none, so the field is optional rather than required.
    rows = checking()
    assert rows["txn_ach_utilities"].tracking_number == "091000015549612"
    assert rows["txn_deposit"].tracking_number is None


def test_pending_and_failed_rows_are_outside_the_balance() -> None:
    rows = checking()
    assert rows["txn_pending_sub"].counts_toward_balance is False
    assert rows["txn_failed_ach"].counts_toward_balance is False
    assert rows["txn_wire_payroll"].counts_toward_balance is True


def test_only_sent_rows_are_postable() -> None:
    rows = checking()
    assert rows["txn_wire_payroll"].is_postable is True
    assert rows["txn_pending_sub"].is_postable is False
    assert rows["txn_failed_ach"].is_postable is False


def test_the_accounting_date_is_posted_at_when_there_is_one() -> None:
    rows = checking()
    assert rows["txn_wire_payroll"].accounting_date == dt.date(2026, 8, 4)
    # createdAt is a day earlier; the two differ by a month on some real rows.
    assert rows["txn_wire_payroll"].created_at.date() == dt.date(2026, 8, 3)


def test_the_accounting_date_falls_back_to_created_at() -> None:
    assert checking()["txn_pending_sub"].accounting_date == dt.date(2026, 8, 28)


def test_gl_allocations_sum_to_the_transaction_amount() -> None:
    # Documented to hold only "when the transaction is fully categorized", so
    # the ingest asserts it rather than assuming it.
    transaction = checking()["txn_wire_payroll"]
    assert transaction.allocated_total == transaction.amount


def test_an_unknown_status_is_never_postable() -> None:
    status, raw = parse_status("teleported")
    assert status is Status.BLOCKED
    assert raw == "teleported"


def test_a_transaction_says_whether_its_status_is_one_we_have_semantics_for() -> None:
    # The fallback to BLOCKED makes an unrecognised status indistinguishable
    # from a real one at the enum, so the raw string is what answers this.
    row = payload("transactions_credit")["transactions"][0]
    assert parse_transaction(row, "acct_credit_1").status_recognised
    assert not parse_transaction({**row, "status": "teleported"}, "acct_credit_1").status_recognised


def test_a_missing_required_field_names_what_the_payload_had() -> None:
    with pytest.raises(MercuryPayloadError) as exc:
        parse_transaction({"amount": 1.0, "status": "sent"}, "acct_chk_1")
    assert "id" in str(exc.value)
    assert "amount" in str(exc.value)


def test_accounts_and_credit_accounts_are_different_shapes() -> None:
    account = parse_account(payload("accounts")["accounts"][0])
    credit_account = parse_credit_account(payload("credit")["accounts"][0])
    assert account.name == "Transpire Checking"
    assert account.current_balance == Decimal("4532.39")
    # A credit account has no name at all -- that is the schema, not an omission.
    assert credit_account.id == "acct_credit_1"
    assert credit_account.current_balance == Decimal("-159")


def test_events_parse_their_envelope() -> None:
    events = [parse_event(row) for row in payload("events")["events"]]
    assert [e.id for e in events] == ["evt_0001", "evt_0002", "evt_0003", "evt_0004", "evt_0005", "evt_0006"]
    assert events[4].operation is Operation.DELETE
    assert events[1].changed_paths == ("categoryData.customCategory",)
    assert events[5].is_transaction is False


def test_an_event_carries_the_status_its_merge_patch_set() -> None:
    created, failed = [parse_event(row) for row in payload("events_failed_transfer")["events"]]
    assert created.status == "pending"
    assert created.failure_reason is None
    assert failed.status == Status.FAILED
    assert failed.failure_reason == "Account Cashback doesn't have enough funds to support a $0.18 transfer"


def test_an_event_with_no_merge_patch_has_no_status() -> None:
    # The recorded-shape fixture predates the patch; an event without one says
    # nothing about status, which is different from saying it is unchanged.
    events = [parse_event(row) for row in payload("events")["events"]]
    assert all(e.status is None and e.failure_reason is None for e in events)


def test_an_unknown_operation_type_raises_rather_than_being_guessed_at() -> None:
    with pytest.raises(MercuryPayloadError):
        parse_event({"id": "e", "resourceType": "transaction", "resourceId": "t", "operationType": "merged"})
