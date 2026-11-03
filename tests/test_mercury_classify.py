"""Routing: which captured transactions become a Bank Transaction, and which want a person."""

from __future__ import annotations

import dataclasses

from runningtab.mercury.classify import Route, classify, is_internal_transfer
from runningtab.mercury.models import parse_transaction
from tests.conftest import all_transactions, by_id, payload, transactions_from


def test_a_categorised_card_charge_becomes_a_bank_transaction() -> None:
    # The category used to route this to a Journal Entry against 5204. The
    # books now run through ERPNext's Banking workspace, which reconciles Bank
    # Transactions, so a category changes nothing about where the row goes.
    transaction = by_id(all_transactions())["txn_card_saas"]
    assert transaction.category == "5204 - Office Software (Microsoft 365 / Slack / 1Password)"
    assert classify(transaction).route is Route.BANK_TRANSACTION


def test_an_uncategorised_checking_movement_becomes_a_bank_transaction() -> None:
    assert classify(by_id(all_transactions())["txn_ach_utilities"]).route is Route.BANK_TRANSACTION


def test_a_category_nobody_mapped_is_still_a_bank_transaction() -> None:
    # There is no suspense route left for it to fall into: whatever Mercury
    # says about the row rides along as a hint, and the person reconciling
    # picks the account.
    decision = classify(by_id(all_transactions())["txn_unmapped_cat"])
    assert decision.route is Route.BANK_TRANSACTION
    assert decision.posts is True


def test_each_leg_of_an_internal_transfer_is_its_own_bank_transaction() -> None:
    # A transfer is two statement lines on two accounts, and the Bank
    # Reconciliation Tool matches both to the one transfer voucher. Pairing
    # them here would leave one account's statement short a line.
    rows = by_id(all_transactions())
    for transaction_id in ("txn_transfer_out", "txn_transfer_in", "txn_cc_payment", "txn_cc_payment_in"):
        assert classify(rows[transaction_id]).route is Route.BANK_TRANSACTION, transaction_id


def test_pending_and_failed_rows_post_nothing() -> None:
    rows = by_id(all_transactions())
    for transaction_id in ("txn_pending_sub", "txn_failed_ach"):
        decision = classify(rows[transaction_id])
        assert decision.route is Route.SKIP
        assert decision.posts is False


def test_a_status_nobody_has_classified_asks_for_a_person_rather_than_skipping() -> None:
    # A skip is a claim -- "settled with, nothing to post" -- and for a status
    # this code has no semantics for that claim is not knowable. Mercury adding
    # a settled status would otherwise leave those rows out of the ledger while
    # the run exited clean: skips raise no attention, and the fallback to
    # BLOCKED still counts them toward the balance, so nothing else notices.
    row = {**payload("transactions_credit")["transactions"][0], "status": "posted"}
    decision = classify(parse_transaction(row, "acct_credit_1"))
    assert decision.route is Route.ATTENTION
    assert decision.posts is False
    assert "posted" in decision.reason
    # And it comes back to be looked at again, because what is missing is our
    # semantics rather than a judgement about the row.
    assert decision.retryable is True


def test_a_reversed_row_is_never_posted_or_undone_automatically() -> None:
    transaction = transactions_from("transaction_reversed", "acct_chk_1")[0]
    decision = classify(transaction)
    assert decision.route is Route.ATTENTION
    assert decision.posts is False
    # Settled with, unlike an unclassified status: there is a decision to make
    # and no amount of re-running the ingest makes it, so the cursor may pass.
    assert decision.retryable is False


def test_a_transfer_is_recognised_by_counterparty_not_by_kind(own_account_ids) -> None:
    # Recognition survives for the uncoded report, which leaves transfers out:
    # nobody codes a movement between our own accounts. The verified
    # checking/credit pair came through as `kind: other`, so trusting `kind`
    # would count exactly that pair as uncoded spend.
    transaction = by_id(all_transactions())["txn_cc_payment"]
    assert transaction.kind == "other"
    assert is_internal_transfer(transaction, own_account_ids)
    external = by_id(all_transactions())["txn_ach_utilities"]
    assert not is_internal_transfer(external, own_account_ids)


def test_a_transfer_leg_with_no_counterparty_id_is_not_a_transfer(own_account_ids) -> None:
    original = by_id(all_transactions())["txn_transfer_out"]
    assert not is_internal_transfer(dataclasses.replace(original, counterparty_id=None), own_account_ids)
