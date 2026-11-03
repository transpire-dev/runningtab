"""Drafting the voucher a coded Bank Transaction will be matched to.

The ledger side goes through the real `icedcoffee-sync` client over a mock transport,
so what these assert is the request actually sent -- the filter, the body, the
absence of a `docstatus` key -- rather than a stand-in's idea of one.

Transactions come from the captured Mercury fixtures. Where a case needs a GL
code the fixtures do not carry, it is `dataclasses.replace`d onto a parsed row
rather than hand-built: a record constructed in a test body agrees with
whatever the test expects, which is what makes it worthless as evidence.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from icedcoffee_sync.frappe.client import Client, SiteConfig

from runningtab.chart import load_chart
from runningtab.mercury.models import Transaction
from runningtab.mercury.posting import TRANSACTION_ID_FIELD, PostingContext, PostingError, bank_transaction
from runningtab.mercury.vouchers import (
    JOURNAL_ENTRY,
    VOUCHER_TYPE,
    Chart,
    Outcome,
    coded_account,
    draft_vouchers,
    journal_entry,
    plan_voucher,
    reference_number,
)
from runningtab.period.lock import LockState
from tests.conftest import EXAMPLE_BOOKS, all_transactions, by_id, example_chart
from tests.test_mercury_posting import _Site

SITE = SiteConfig(url="https://example.test", api_key="KEY", api_secret="SECRET", may_submit=True)  # noqa: S106

# The one fixture row a person has coded: an outgoing ACH to the ISP, GL code
# `5215 - Phone & Internet (Verizon, Comcast)`, with an ACH trace number.
CODED = "txn_ach_utilities"

# Codes naming real postable accounts in the shipped chart, one per family the
# selection rule cares about.
EXPENSE_CODE = "5215 - Phone & Internet (Verizon, Comcast)"
# The en dash is the chart's, and therefore the bank's: this is the string
# `runningtab coa export-gl-codes` writes and Mercury reports back verbatim.
CAPITAL_CODE = "3110 - Capital Contribution – Member 1"  # noqa: RUF001
REVENUE_CODE = "4210 - Interest Income"

OWN_ACCOUNTS = ["acct_chk_1", "acct_chk_2", "acct_credit_1"]


@pytest.fixture
def site() -> _Site:
    return _Site()


@pytest.fixture
def ledger(site: _Site):
    with Client(SITE, transport=httpx2.MockTransport(site.handler)) as client:
        yield client


@pytest.fixture
def chart() -> Chart:
    return example_chart()


def rows() -> dict[str, Transaction]:
    return by_id(all_transactions())


def coded(gl_code: str | None, transaction_id: str = CODED, **fields: Any) -> Transaction:
    """One fixture row wearing a GL code."""
    return dataclasses.replace(rows()[transaction_id], gl_code_name=gl_code, **fields)


def vouchers_on(site: _Site) -> list[dict[str, Any]]:
    return [doc for (doctype, _), doc in site.documents.items() if doctype == JOURNAL_ENTRY]


def bodies(site: _Site, method: str) -> list[dict[str, Any]]:
    return [json.loads(r.content) for r in site.requests if r.method == method]


# --- selection ----------------------------------------------------------------


def test_an_expense_code_drafts_a_voucher(chart) -> None:
    number, unplaceable = coded_account(coded(EXPENSE_CODE), chart)
    assert (number, unplaceable) == ("5215", None)


def test_a_member_capital_code_drafts_a_voucher(chart) -> None:
    # A member putting money in is the other case a two-line entry against the
    # bank account describes completely.
    number, unplaceable = coded_account(coded(CAPITAL_CODE), chart)
    assert (number, unplaceable) == ("3110", None)


def test_a_revenue_code_drafts_nothing_and_is_not_reported(chart) -> None:
    # A customer payment clears a Sales Invoice through a Payment Entry. A bare
    # entry crediting revenue books the income twice and leaves the invoice
    # outstanding -- and the person coded it deliberately, so it is not an error.
    assert coded_account(coded(REVENUE_CODE), chart) == (None, None)


def test_a_group_account_number_drafts_nothing_and_is_reported(chart) -> None:
    # 5100 is a group in chart.coa. Nothing can post to it, and the GL code
    # list never offered it, so seeing one means the list has drifted.
    number, unplaceable = coded_account(coded("5100 - Cost of Revenue"), chart)
    assert number is None
    assert unplaceable == "5100 - Cost of Revenue"


def test_a_code_that_is_not_number_dash_name_is_reported(chart) -> None:
    number, unplaceable = coded_account(coded("Utilities"), chart)
    assert (number, unplaceable) == (None, "Utilities")


def test_a_row_coded_only_by_its_category_drafts_a_voucher(chart) -> None:
    # Mercury's categories are the chart's GL-code labels; picking one is coding the row.
    transaction = coded(None, custom_category=EXPENSE_CODE)
    assert coded_account(transaction, chart) == ("5215", None)


def test_an_uncoded_row_drafts_nothing_and_is_not_reported(chart) -> None:
    # It is already in the run's `uncoded` bucket, which already fails the run.
    assert coded_account(coded(None), chart) == (None, None)


def test_one_half_of_a_transfer_gets_no_voucher_of_its_own(posting_ctx, ledger, site) -> None:
    # One economic event seen twice. A voucher per leg double-counts a
    # balance-sheet movement, so a leg is only ever drafted as half of a pair.
    transfer = coded(EXPENSE_CODE, transaction_id="txn_transfer_out")
    assert transfer.counterparty_id in OWN_ACCOUNTS
    report = draft_vouchers([transfer], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert report.drafted == []
    assert vouchers_on(site) == []


def test_an_unsettled_row_gets_no_voucher(posting_ctx, ledger, site) -> None:
    pending = coded(EXPENSE_CODE, transaction_id="txn_pending_sub")
    assert not pending.is_postable
    draft_vouchers([pending], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert vouchers_on(site) == []


def test_a_drifted_code_is_reported_once_for_the_run(posting_ctx, ledger, site) -> None:
    # The cause is one thing -- the list uploaded to Mercury has drifted from
    # chart.coa -- so a line per row would bury it.
    drifted = [
        coded("9999 - Nowhere", transaction_id="txn_ach_utilities"),
        coded("9999 - Nowhere", transaction_id="txn_wire_payroll"),
        coded("8888 - Elsewhere", transaction_id="txn_card_saas"),
    ]
    report = draft_vouchers(drifted, posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert len(report.attention) == 1
    where, reason = report.attention[0]
    assert where == "gl codes"
    assert "'9999 - Nowhere'" in reason
    assert "'8888 - Elsewhere'" in reason
    assert vouchers_on(site) == []


def test_a_mercury_account_with_no_chart_account_is_reported_not_guessed(ledger, mercury_mapping) -> None:
    ctx = PostingContext(
        company="Transpire",
        account_names={"5215": "5215 - Phone & Internet (Verizon, Comcast) - T"},
        mapping=dataclasses.replace(mercury_mapping, accounts={}),
        bot_user=EXAMPLE_BOOKS.bot.user,
        chart=example_chart(),
    )
    report = draft_vouchers([coded(EXPENSE_CODE)], ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert [where for where, _ in report.attention] == [CODED]
    assert "[accounts] entry in mercury.toml" in report.attention[0][1]


# --- a payee's history ----------------------------------------------------------

# Two checking outflows made to look like one payee. Mercury keys a payee on
# `counterpartyId`, which every row from one merchant shares.
PAYEE = "cp_one_payee"
EARLIER, LATER = "txn_wire_payroll", CODED


def uncoded(transaction_id: str, payee: str | None = PAYEE) -> Transaction:
    """A fixture row nobody coded at Mercury, paid to `payee`."""
    return dataclasses.replace(rows()[transaction_id], gl_code_name=None, custom_category=None, counterparty_id=payee)


def booked(site: _Site, ctx: PostingContext, transaction: Transaction, number: str, *, docstatus: int = 1) -> str:
    """A voucher a person committed for `transaction`, against the chart account `number`."""
    name = f"JV-{transaction.id}"
    site.documents[(JOURNAL_ENTRY, name)] = {
        **journal_entry(transaction, number, ctx),
        "docstatus": docstatus,
        "modified_by": "pat@example.com",
    }
    return name


def test_an_uncoded_row_is_drafted_the_way_its_payee_was_booked_before(posting_ctx, ledger, site) -> None:
    source = booked(site, posting_ctx, uncoded(EARLIER), "5215")
    report = draft_vouchers([uncoded(EARLIER), uncoded(LATER)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert len(report.drafted) == 1
    (draft,) = [doc for doc in vouchers_on(site) if doc[TRANSACTION_ID_FIELD] == LATER]
    assert draft["accounts"][1]["account"] == posting_ctx.link("5215")
    # The reviewer sees why: the account, and the voucher it was copied from.
    assert f"5215 - Phone & Internet (Verizon, Comcast) as {source}" in draft["remark"]
    assert report.handled_in_ledger == {EARLIER, LATER}


def test_a_payee_booked_to_different_accounts_proposes_nothing(posting_ctx, ledger, site) -> None:
    # One payee, several accounts -- a person reimbursed for many kinds of
    # spend. Copying whichever came last would be a guess wearing a precedent.
    booked(site, posting_ctx, uncoded(EARLIER), "5215")
    booked(site, posting_ctx, uncoded("txn_deposit"), "5110")
    later = uncoded(LATER)
    report = draft_vouchers(
        [uncoded(EARLIER), uncoded("txn_deposit"), later], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS
    )
    assert report.drafted == []
    assert LATER not in report.handled_in_ledger


def test_only_a_submitted_voucher_is_history(posting_ctx, ledger, site) -> None:
    # A draft is a proposal nobody has agreed to yet, and a cancelled voucher
    # is one somebody took back; neither is a decision to copy.
    booked(site, posting_ctx, uncoded(EARLIER), "5215", docstatus=0)
    booked(site, posting_ctx, uncoded("txn_deposit"), "5215", docstatus=2)
    report = draft_vouchers(
        [uncoded(EARLIER), uncoded("txn_deposit"), uncoded(LATER)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS
    )
    assert report.drafted == []
    assert LATER not in report.handled_in_ledger


def test_a_different_payee_lends_no_history(posting_ctx, ledger, site) -> None:
    booked(site, posting_ctx, uncoded(EARLIER), "5215")
    report = draft_vouchers(
        [uncoded(EARLIER), uncoded(LATER, payee="cp_someone_else")], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS
    )
    assert report.drafted == []


def test_a_row_with_no_payee_has_no_history(posting_ctx, ledger, site) -> None:
    booked(site, posting_ctx, uncoded(EARLIER, payee=None), "5215")
    report = draft_vouchers(
        [uncoded(EARLIER, payee=None), uncoded(LATER, payee=None)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS
    )
    assert report.drafted == []


def test_history_drafts_an_income_account_a_mercury_code_would_not(posting_ctx, ledger, site) -> None:
    # A revenue code from Mercury drafts nothing, because a customer payment
    # clears an invoice. History is different: a person already booked this
    # payee to income with a voucher like this one -- card cashback -- and the
    # draft repeats exactly that.
    booked(site, posting_ctx, uncoded("txn_deposit"), "4210")
    later = dataclasses.replace(uncoded(LATER), amount=Decimal("1.50"))
    report = draft_vouchers([uncoded("txn_deposit"), later], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert len(report.drafted) == 1
    (draft,) = [doc for doc in vouchers_on(site) if doc[TRANSACTION_ID_FIELD] == LATER]
    assert draft["accounts"][1]["account"] == posting_ctx.link("4210")


def test_an_uncoded_row_with_a_voucher_already_is_left_alone_and_counts_as_coded(posting_ctx, ledger, site) -> None:
    # A person coded it in the ledger rather than at the bank. Nothing is
    # rewritten, and the run stops calling it uncoded.
    booked(site, posting_ctx, uncoded(LATER), "5215", docstatus=0)
    report = draft_vouchers([uncoded(LATER)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert (report.drafted, report.updated, report.planned) == ([], [], [])
    assert bodies(site, "PUT") == []
    assert report.handled_in_ledger == {LATER}


def test_an_uncoded_row_a_person_reconciled_counts_as_coded(posting_ctx, ledger, site) -> None:
    # Matching the statement line is deciding where it goes.
    site.documents[("Bank Transaction", "BT-1")] = {
        TRANSACTION_ID_FIELD: LATER,
        "status": "Reconciled",
        "docstatus": 1,
    }
    report = draft_vouchers([uncoded(LATER)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert report.drafted == []
    assert report.handled_in_ledger == {LATER}


def test_a_row_coded_to_revenue_at_mercury_gets_no_history_draft(posting_ctx, ledger, site) -> None:
    # A person coded it, and a revenue code deliberately drafts nothing: a
    # customer payment clears an invoice. History is for rows nobody coded.
    booked(site, posting_ctx, uncoded(EARLIER), "5215")
    revenue = dataclasses.replace(uncoded(LATER), gl_code_name=REVENUE_CODE)
    report = draft_vouchers([uncoded(EARLIER), revenue], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert report.drafted == []
    assert LATER not in report.handled_in_ledger


def test_an_uncoded_row_whose_only_voucher_was_cancelled_stays_uncoded(posting_ctx, ledger, site) -> None:
    # A cancelled voucher booked nothing. Calling the row handled would drop
    # it from the report with nothing in the ledger behind it.
    booked(site, posting_ctx, uncoded(LATER), "5215", docstatus=2)
    report = draft_vouchers([uncoded(LATER)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert report.drafted == []
    assert report.handled_in_ledger == set()


def test_a_cancelled_voucher_does_not_hide_a_reconciliation_made_since(posting_ctx, ledger, site) -> None:
    booked(site, posting_ctx, uncoded(LATER), "5215", docstatus=2)
    site.documents[("Bank Transaction", "BT-1")] = {
        TRANSACTION_ID_FIELD: LATER,
        "status": "Reconciled",
        "docstatus": 1,
    }
    report = draft_vouchers([uncoded(LATER)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert report.handled_in_ledger == {LATER}


def test_a_reconciled_uncoded_row_still_refuses_two_vouchers(posting_ctx, ledger, site) -> None:
    # Reconciliation does not excuse a broken ledger: two live vouchers
    # carrying one Mercury id is the state the lookup exists to refuse.
    booked(site, posting_ctx, uncoded(LATER), "5215")
    ledger.insert(JOURNAL_ENTRY, {TRANSACTION_ID_FIELD: LATER})
    site.documents[("Bank Transaction", "BT-1")] = {
        TRANSACTION_ID_FIELD: LATER,
        "status": "Reconciled",
        "docstatus": 1,
    }
    with pytest.raises(PostingError, match="more than one live document"):
        draft_vouchers([uncoded(LATER)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)


def test_an_uncoded_row_with_no_history_stays_uncoded(posting_ctx, ledger, site) -> None:
    report = draft_vouchers([uncoded(LATER)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert (report.drafted, report.handled_in_ledger) == ([], set())


def test_history_does_not_draft_into_a_closed_quarter(posting_ctx, ledger, site) -> None:
    booked(site, posting_ctx, uncoded(EARLIER), "5215")
    closed = dataclasses.replace(posting_ctx, lock=LockState(closed_through=dt.date(2026, 8, 31), bypass_role=None))
    report = draft_vouchers([uncoded(EARLIER), uncoded(LATER)], closed, ledger, own_account_ids=OWN_ACCOUNTS)
    assert report.drafted == []
    assert len(report.closed) == 1


# --- transfers between our own accounts -------------------------------------------


def legs(*ids: str) -> list[Transaction]:
    return [rows()[transaction_id] for transaction_id in ids]


def test_a_card_payment_drafts_one_transfer_voucher(posting_ctx, ledger, site) -> None:
    # Checking pays the card: two statement lines, one movement. The tool
    # matches both Bank Transactions to this one voucher.
    report = draft_vouchers(
        legs("txn_cc_payment", "txn_cc_payment_in"), posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS
    )
    assert len(report.drafted) == 1
    (voucher,) = vouchers_on(site)
    assert voucher[TRANSACTION_ID_FIELD] == "txn_cc_payment"
    lines = {
        line["account"]: (line["debit_in_account_currency"], line["credit_in_account_currency"], line["bank_account"])
        for line in voucher["accounts"]
    }
    assert lines == {
        posting_ctx.link("2121"): (70.0, 0, "Mercury IO Card - T"),
        posting_ctx.link("1111"): (0, 70.0, "Mercury Checking - T"),
    }
    assert voucher["title"] == "Transfer to Mercury IO · 70.00"
    assert "mercury txn_cc_payment" in voucher["remark"]
    assert "mercury txn_cc_payment_in" in voucher["remark"]
    assert voucher["custom_remark"] == 1


def test_a_savings_transfer_drafts_from_the_account_the_money_left(posting_ctx, ledger, site) -> None:
    report = draft_vouchers(
        legs("txn_transfer_in", "txn_transfer_out"), posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS
    )
    assert len(report.drafted) == 1
    (voucher,) = vouchers_on(site)
    assert voucher[TRANSACTION_ID_FIELD] == "txn_transfer_out"
    debit, credit = (
        next(line["account"] for line in voucher["accounts"] if line[side])
        for side in ("debit_in_account_currency", "credit_in_account_currency")
    )
    assert (debit, credit) == (posting_ctx.link("1112"), posting_ctx.link("1111"))


def test_a_half_with_no_partner_is_reported_not_guessed(posting_ctx, ledger, site) -> None:
    report = draft_vouchers(legs("txn_cc_payment"), posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert report.drafted == []
    assert [where for where, _ in report.attention] == ["txn_cc_payment"]
    assert "other half" in report.attention[0][1]


def test_two_possible_partners_are_reported_not_guessed(posting_ctx, ledger, site) -> None:
    out, back = legs("txn_cc_payment", "txn_cc_payment_in")
    twin = dataclasses.replace(back, id="txn_cc_payment_in_twin")
    report = draft_vouchers([out, back, twin], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert report.drafted == []
    assert "txn_cc_payment" in [where for where, _ in report.attention]


def test_a_partner_days_apart_is_not_this_transfers(posting_ctx, ledger, site) -> None:
    # Monthly autopays of the same amount: next month's other half is not this one's.
    out, back = legs("txn_cc_payment", "txn_cc_payment_in")
    later = dataclasses.replace(back, created_at=back.created_at + dt.timedelta(days=30), posted_at=None)
    report = draft_vouchers([out, later], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert report.drafted == []


def test_a_transfer_voucher_is_drafted_once(posting_ctx, ledger, site) -> None:
    pair = legs("txn_cc_payment", "txn_cc_payment_in")
    draft_vouchers(pair, posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    site.requests.clear()
    report = draft_vouchers(pair, posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert (report.drafted, report.updated) == ([], [])
    assert bodies(site, "POST") == bodies(site, "PUT") == []


def test_a_transfer_whose_incoming_half_is_already_reconciled_is_reported_not_drafted(
    posting_ctx, ledger, site
) -> None:
    # A person booked the card side by hand. A transfer voucher now would book
    # the payment a second time the moment somebody submitted it.
    site.documents[("Bank Transaction", "BT-IN")] = {
        TRANSACTION_ID_FIELD: "txn_cc_payment_in",
        "status": "Reconciled",
        "docstatus": 1,
    }
    report = draft_vouchers(
        legs("txn_cc_payment", "txn_cc_payment_in"), posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS
    )
    assert report.drafted == []
    assert vouchers_on(site) == []
    assert [where for where, _ in report.attention] == ["txn_cc_payment"]
    assert "already reconciled" in report.attention[0][1]


def reconciled(site: _Site, transaction_id: str) -> None:
    site.documents[("Bank Transaction", f"BT-{transaction_id}")] = {
        TRANSACTION_ID_FIELD: transaction_id,
        "status": "Reconciled",
        "docstatus": 1,
    }


def test_a_transfer_draft_whose_incoming_half_was_matched_elsewhere_is_reported(posting_ctx, ledger, site) -> None:
    # A reconciled half can only be matched to a submitted voucher, so a half
    # reconciled while our voucher is still a draft was matched to another.
    # Submitting ours now would book the movement twice.
    pair = legs("txn_cc_payment", "txn_cc_payment_in")
    draft_vouchers(pair, posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    (name,) = [n for (doctype, n) in site.documents if doctype == JOURNAL_ENTRY]
    reconciled(site, "txn_cc_payment_in")
    report = draft_vouchers(pair, posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert (report.drafted, report.updated, report.attention) == ([], [], [])
    (unneeded,) = report.unneeded
    assert (unneeded.transaction_id, unneeded.journal_entry) == ("txn_cc_payment", name)
    assert "txn_cc_payment_in" in unneeded.why
    assert (JOURNAL_ENTRY, name) in site.documents


def test_a_pair_that_turned_ambiguous_names_its_existing_draft(posting_ctx, ledger, site) -> None:
    out, back = legs("txn_cc_payment", "txn_cc_payment_in")
    draft_vouchers([out, back], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    (name,) = [n for (doctype, n) in site.documents if doctype == JOURNAL_ENTRY]
    twin = dataclasses.replace(back, id="txn_cc_payment_in_twin")
    report = draft_vouchers([out, back, twin], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert name in dict(report.attention)["txn_cc_payment"]


def test_a_transfer_whose_outgoing_half_a_person_reconciled_names_the_other_half(posting_ctx, ledger, site) -> None:
    # The person booked checking's side to a voucher of their own. Saying
    # nothing would leave the card's side unmatched without a word.
    reconciled(site, "txn_cc_payment")
    report = draft_vouchers(
        legs("txn_cc_payment", "txn_cc_payment_in"), posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS
    )
    assert report.drafted == []
    assert "txn_cc_payment_in" in dict(report.attention)["txn_cc_payment"]


# One rule for a transfer with either half dated on or before Books Closed
# Through: nothing is drafted, and it is reported until both halves are booked
# (reconciled, or covered by our submitted voucher) and no draft of ours remains.

CLOSE = dt.date(2026, 8, 31)


def closed_ctx(ctx: PostingContext, through: dt.date = CLOSE) -> PostingContext:
    return dataclasses.replace(ctx, lock=LockState(closed_through=through, bypass_role=None))


def straddling() -> tuple[list[Transaction], dt.date]:
    """The card payment with its outgoing half a day after the lock and its incoming half on it."""
    out, back = legs("txn_cc_payment", "txn_cc_payment_in")
    out = dataclasses.replace(out, created_at=out.created_at + dt.timedelta(days=1), posted_at=None)
    return [out, back], back.accounting_date


def closed_line(report: Any) -> str:
    (line,) = report.closed
    return line


def test_a_closed_transfer_drafts_nothing_and_names_both_halves(posting_ctx, ledger, site) -> None:
    report = draft_vouchers(
        legs("txn_cc_payment", "txn_cc_payment_in"), closed_ctx(posting_ctx), ledger, own_account_ids=OWN_ACCOUNTS
    )
    assert report.drafted == []
    line = closed_line(report)
    assert line.startswith("txn_cc_payment ")
    assert "txn_cc_payment not booked" in line and "txn_cc_payment_in not booked" in line


def test_a_straddling_transfer_drafts_nothing_and_is_reported(posting_ctx, ledger, site) -> None:
    # A voucher dated in the open quarter would leave the closed quarter's half unbooked.
    pair, through = straddling()
    report = draft_vouchers(pair, closed_ctx(posting_ctx, through), ledger, own_account_ids=OWN_ACCOUNTS)
    assert report.drafted == []
    assert "txn_cc_payment_in not booked" in closed_line(report)


def test_a_closed_transfer_with_both_halves_reconciled_is_quiet(posting_ctx, ledger, site) -> None:
    reconciled(site, "txn_cc_payment")
    reconciled(site, "txn_cc_payment_in")
    report = draft_vouchers(
        legs("txn_cc_payment", "txn_cc_payment_in"), closed_ctx(posting_ctx), ledger, own_account_ids=OWN_ACCOUNTS
    )
    assert (report.closed, report.attention) == ([], [])


def test_a_straddling_transfer_with_both_halves_reconciled_is_quiet(posting_ctx, ledger, site) -> None:
    pair, through = straddling()
    reconciled(site, "txn_cc_payment")
    reconciled(site, "txn_cc_payment_in")
    report = draft_vouchers(pair, closed_ctx(posting_ctx, through), ledger, own_account_ids=OWN_ACCOUNTS)
    assert (report.closed, report.attention) == ([], [])


def test_a_closed_transfer_with_one_half_booked_says_which(posting_ctx, ledger, site) -> None:
    # Advice to book the half that is booked already would duplicate it.
    reconciled(site, "txn_cc_payment_in")
    report = draft_vouchers(
        legs("txn_cc_payment", "txn_cc_payment_in"), closed_ctx(posting_ctx), ledger, own_account_ids=OWN_ACCOUNTS
    )
    line = closed_line(report)
    assert "txn_cc_payment not booked" in line and "txn_cc_payment_in booked" in line
    # Nothing is promised: this pair gets no draft, reopened or not.
    assert "next sync" not in line


def test_a_closed_transfer_with_a_leftover_draft_names_it_even_once_booked(posting_ctx, ledger, site) -> None:
    # Drafted before the close, then both halves matched elsewhere: the draft
    # could still be submitted and book the movement again.
    pair = legs("txn_cc_payment", "txn_cc_payment_in")
    draft_vouchers(pair, posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    (name,) = [n for (doctype, n) in site.documents if doctype == JOURNAL_ENTRY]
    reconciled(site, "txn_cc_payment")
    reconciled(site, "txn_cc_payment_in")
    report = draft_vouchers(pair, closed_ctx(posting_ctx), ledger, own_account_ids=OWN_ACCOUNTS)
    assert name in closed_line(report)


def test_a_closed_transfer_our_submitted_voucher_books_is_quiet(posting_ctx, ledger, site) -> None:
    pair = legs("txn_cc_payment", "txn_cc_payment_in")
    draft_vouchers(pair, posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    (name,) = [n for (doctype, n) in site.documents if doctype == JOURNAL_ENTRY]
    site.documents[(JOURNAL_ENTRY, name)]["docstatus"] = 1
    report = draft_vouchers(pair, closed_ctx(posting_ctx), ledger, own_account_ids=OWN_ACCOUNTS)
    assert (report.closed, report.attention) == ([], [])


# --- the document -------------------------------------------------------------


def test_the_voucher_has_two_lines_with_the_sides_the_sign_gives(posting_ctx) -> None:
    doc = journal_entry(coded(EXPENSE_CODE), "5215", posting_ctx)
    assert len(doc["accounts"]) == 2
    bank, expense = doc["accounts"]
    # An outflow credits the account the money left and debits the category.
    assert (bank["account"], bank["credit_in_account_currency"]) == ("1111 - Operating Checking - T", 100.0)
    assert bank["debit_in_account_currency"] == 0
    assert (expense["account"], expense["debit_in_account_currency"]) == (
        "5215 - Phone & Internet (Verizon, Comcast) - T",
        100.0,
    )
    assert expense["credit_in_account_currency"] == 0


def test_an_inflow_swaps_the_sides(posting_ctx) -> None:
    # A refund is the same entry with the sides swapped. No branch of its own.
    doc = journal_entry(coded(CAPITAL_CODE, transaction_id="txn_deposit"), "3110", posting_ctx)
    bank, capital = doc["accounts"]
    assert bank["debit_in_account_currency"] == 10000.0
    assert capital["credit_in_account_currency"] == 10000.0


def test_the_bank_line_is_the_account_the_bank_account_links(posting_ctx) -> None:
    # The reconciliation tool filters candidate vouchers on
    # `jea.account == Bank Account.account`, so a voucher without a line on
    # exactly that account is invisible to the screen it exists for.
    doc = journal_entry(coded(EXPENSE_CODE), "5215", posting_ctx)
    bank = doc["accounts"][0]
    assert bank["account"] == "1111 - Operating Checking - T"
    assert bank["bank_account"] == "Mercury Checking - T"


def test_no_line_carries_a_party(posting_ctx) -> None:
    # Member identity is the equity account itself, and ERPNext rejects a
    # Shareholder party on an equity line.
    doc = journal_entry(coded(CAPITAL_CODE), "3110", posting_ctx)
    assert all("party" not in row and "party_type" not in row for row in doc["accounts"])


def test_the_voucher_is_a_bank_entry_with_both_reference_fields(posting_ctx) -> None:
    # `validate_cheque_info` makes both mandatory for a Bank Entry, and the
    # tool's Journal Entry query ranks on `cheque_no`.
    doc = journal_entry(coded(EXPENSE_CODE), "5215", posting_ctx)
    assert doc["voucher_type"] == VOUCHER_TYPE == "Bank Entry"
    assert doc["cheque_date"] == doc["posting_date"] == "2026-08-06"
    assert doc["cheque_no"] == "091000015549612"


def test_the_cheque_number_is_the_bank_transactions_reference(posting_ctx) -> None:
    # The Bank Reconciliation Tool's Journal Entry query ranks candidates on
    # `je.cheque_no == transaction.reference_number`, so the two are composed
    # from the same two fields by two modules and have to agree. This is the
    # gate on that agreement: it fails at `just test`, not at
    # reconciliation time with a draft nobody can find.
    for transaction in all_transactions():
        if not transaction.is_postable:
            continue
        statement_line = bank_transaction(transaction, posting_ctx)
        voucher = journal_entry(dataclasses.replace(transaction, gl_code_name=EXPENSE_CODE), "5215", posting_ctx)
        assert voucher["cheque_no"] == statement_line["reference_number"]


def test_the_reference_is_the_trace_number_and_falls_back_to_the_mercury_id() -> None:
    # The trace number is what a statement export carries as `Tracking ID`, so
    # a hand-imported row holds it too. A card charge never went through ACH.
    assert reference_number(rows()[CODED]) == "091000015549612"
    assert reference_number(rows()["txn_card_saas"]) == "txn_card_saas"


def test_the_remark_carries_the_note_the_code_the_payee_and_the_mercury_id(posting_ctx) -> None:
    # A reviewer looking at a list of drafts sees the remark and nothing else,
    # so the payee is on it: telling one 5204 entry from the next should not
    # mean opening the Bank Transaction behind each.
    doc = journal_entry(coded(EXPENSE_CODE, note="Sonic, August"), "5215", posting_ctx)
    assert doc["remark"] == f"Sonic, August · {EXPENSE_CODE} · paid to Sonic · mercury {CODED}"


def test_the_remark_is_the_visible_one_and_kept_as_written(posting_ctx) -> None:
    # ERPNext's `user_remark` on Journal Entry is hidden: no list, report or
    # form shows it, so a description there reaches nobody. `remark` is the
    # visible field, which ERPNext rebuilds from the reference on every save
    # unless `custom_remark` is set.
    doc = journal_entry(coded(EXPENSE_CODE), "5215", posting_ctx)
    assert doc["custom_remark"] == 1
    assert doc["remark"] == f"{EXPENSE_CODE} · paid to Sonic · mercury {CODED}"
    assert "user_remark" not in doc


def test_the_title_names_the_payee_and_the_code(posting_ctx) -> None:
    # ERPNext falls back to the first account line, which made every draft in
    # the list read as the bank account it moved through.
    doc = journal_entry(coded(EXPENSE_CODE), "5215", posting_ctx)
    assert doc["title"] == f"Sonic · {EXPENSE_CODE}"


def test_a_title_with_no_payee_is_the_code(posting_ctx) -> None:
    doc = journal_entry(coded(EXPENSE_CODE, counterparty_name=None), "5215", posting_ctx)
    assert doc["title"] == EXPENSE_CODE


def test_a_draft_written_before_titles_existed_is_brought_up_to_date(posting_ctx, ledger, site) -> None:
    # The sync rewrites its own unedited drafts when what it would write
    # changes, so drafts from before the visible remark get it on the next run.
    draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    name = next(n for (doctype, n) in site.documents if doctype == JOURNAL_ENTRY)
    old = site.documents[(JOURNAL_ENTRY, name)]
    for field in ("title", "remark", "custom_remark"):
        old.pop(field)
    report = draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert report.updated == [name]
    assert site.documents[(JOURNAL_ENTRY, name)]["title"] == f"Sonic · {EXPENSE_CODE}"


def test_the_remark_without_a_note_starts_at_the_code(posting_ctx) -> None:
    doc = journal_entry(coded(EXPENSE_CODE), "5215", posting_ctx)
    assert doc["remark"] == f"{EXPENSE_CODE} · paid to Sonic · mercury {CODED}"


def test_the_remark_carries_a_code_that_came_from_the_category(posting_ctx) -> None:
    doc = journal_entry(coded(None, custom_category=EXPENSE_CODE), "5215", posting_ctx)
    assert doc["remark"] == f"{EXPENSE_CODE} · paid to Sonic · mercury {CODED}"


def test_an_inflow_says_received_from_rather_than_paid_to(posting_ctx) -> None:
    # Money arriving from a customer is not a payment to them, and a remark
    # that said so would be read as one by whoever approves the entry.
    transaction = coded(CAPITAL_CODE, transaction_id="txn_deposit")
    assert transaction.amount > 0
    doc = journal_entry(transaction, "3110", posting_ctx)
    assert doc["remark"] == f"{CAPITAL_CODE} · received from Acme Bicycles LLC · mercury txn_deposit"


def test_a_row_with_no_counterparty_names_none(posting_ctx) -> None:
    # Mercury does not always report one. An empty "paid to" would be worse
    # than no payee at all: it reads as a payee the bank failed to give us.
    doc = journal_entry(coded(EXPENSE_CODE, counterparty_name=None), "5215", posting_ctx)
    assert doc["remark"] == f"{EXPENSE_CODE} · mercury {CODED}"


def test_the_voucher_carries_the_mercury_id_and_no_docstatus(posting_ctx) -> None:
    doc = journal_entry(coded(EXPENSE_CODE), "5215", posting_ctx)
    assert doc[TRANSACTION_ID_FIELD] == CODED
    assert "docstatus" not in doc


def test_the_chart_account_must_be_on_the_site(mercury_mapping) -> None:
    ctx = PostingContext(
        company="Transpire",
        account_names={},
        mapping=mercury_mapping,
        bot_user=EXAMPLE_BOOKS.bot.user,
        chart=example_chart(),
    )
    with pytest.raises(PostingError, match="apply the chart before ingesting"):
        journal_entry(coded(EXPENSE_CODE), "5215", ctx)


# --- idempotency and ownership ------------------------------------------------


def test_a_second_run_writes_nothing(posting_ctx, ledger, site) -> None:
    transactions = [coded(EXPENSE_CODE)]
    first = draft_vouchers(transactions, posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert len(first.drafted) == 1
    before = {key: dict(doc) for key, doc in site.documents.items()}
    second = draft_vouchers(transactions, posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert (second.drafted, second.updated, second.attention) == ([], [], [])
    assert site.documents == before


def test_a_recoded_transaction_rewrites_its_draft(posting_ctx, ledger, site) -> None:
    # Coding a transaction at Mercury emits no event and bumps no
    # `resourceVersion`, so a pass that waited for the log would never deliver
    # the correction. The person's work has to arrive somehow.
    draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    report = draft_vouchers([coded("5110 - Cloud Hosting")], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert len(report.updated) == 1
    assert vouchers_on(site)[0]["accounts"][1]["account"] == "5110 - Cloud Hosting & Infrastructure - T"


def test_an_amount_that_settled_differently_rewrites_the_draft(posting_ctx, ledger, site) -> None:
    # The amounts live in the child table, which is why the comparison reads
    # the document whole rather than by field list.
    draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    resettled = coded(EXPENSE_CODE, amount=Decimal("-120.00"))
    report = draft_vouchers([resettled], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert len(report.updated) == 1
    assert vouchers_on(site)[0]["accounts"][0]["credit_in_account_currency"] == 120.0


def test_a_draft_a_person_edited_is_left_alone_and_not_reported(posting_ctx, ledger, site) -> None:
    # A person taking the proposal over is the point of the proposal. Saying so
    # every run would train them to ignore the report.
    draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    name = next(n for (doctype, n) in site.documents if doctype == JOURNAL_ENTRY)
    site.documents[(JOURNAL_ENTRY, name)]["modified_by"] = "pat@example.com"
    site.documents[(JOURNAL_ENTRY, name)]["remark"] = "reviewed, this is the ISP not hosting"
    report = draft_vouchers([coded("5110 - Cloud Hosting")], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert (report.drafted, report.updated, report.attention) == ([], [], [])
    assert site.documents[(JOURNAL_ENTRY, name)]["remark"] == "reviewed, this is the ISP not hosting"


def test_a_submitted_voucher_is_left_alone_and_not_reported(posting_ctx, ledger, site) -> None:
    draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    name = next(n for (doctype, n) in site.documents if doctype == JOURNAL_ENTRY)
    site.documents[(JOURNAL_ENTRY, name)]["docstatus"] = 1
    report = draft_vouchers([coded("5110 - Cloud Hosting")], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert (report.drafted, report.updated, report.attention) == ([], [], [])
    assert bodies(site, "PUT") == []


def test_a_cancelled_voucher_is_left_alone(posting_ctx, ledger, site) -> None:
    draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    name = next(n for (doctype, n) in site.documents if doctype == JOURNAL_ENTRY)
    site.documents[(JOURNAL_ENTRY, name)]["docstatus"] = 2
    report = draft_vouchers([coded("5110 - Cloud Hosting")], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert (report.drafted, report.updated, report.attention) == ([], [], [])


def amended(site: _Site, ledger: Client, *, docstatus: int) -> tuple[str, str]:
    """Cancel our voucher for CODED and give it a person's amendment, as ERPNext's Amend does.

    Amend copies `mercury_transaction_id` onto the new document even though
    the field is `no_copy`, so the cancelled original and the amendment carry
    the same Mercury id.
    """
    original = next(n for (doctype, n) in site.documents if doctype == JOURNAL_ENTRY)
    site.documents[(JOURNAL_ENTRY, original)]["docstatus"] = 2
    created = ledger.insert(JOURNAL_ENTRY, {TRANSACTION_ID_FIELD: CODED, "amended_from": original})
    amendment = str(created["name"])
    site.documents[(JOURNAL_ENTRY, amendment)].update({"docstatus": docstatus, "modified_by": "pat@example.com"})
    return original, amendment


@pytest.mark.parametrize("docstatus", [0, 1])
def test_a_persons_amendment_of_our_voucher_is_left_alone(posting_ctx, ledger, site, docstatus) -> None:
    # A person corrected a voucher the only way ERPNext allows: cancel, then
    # amend. The amendment is their work, draft or submitted; the cancelled
    # original beside it is history, not a second voucher for the row.
    draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    amended(site, ledger, docstatus=docstatus)
    puts_before = len(bodies(site, "PUT"))
    report = draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert (report.drafted, report.updated, report.attention) == ([], [], [])
    assert len(vouchers_on(site)) == 2
    assert len(bodies(site, "PUT")) == puts_before


def test_an_amendment_whose_bank_transaction_is_reconciled_is_not_called_unneeded_once_submitted(
    posting_ctx, ledger, site
) -> None:
    # The person submitted their amendment and matched the statement line to
    # it. Nothing is left over: the cancelled original is not a draft.
    draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    amended(site, ledger, docstatus=1)
    site.documents[("Bank Transaction", "BT-1")] = {
        TRANSACTION_ID_FIELD: CODED,
        "status": "Reconciled",
        "docstatus": 1,
    }
    report = draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert (report.drafted, report.attention) == ([], [])


def test_two_live_vouchers_beside_a_cancelled_one_still_refuse(posting_ctx, ledger, site) -> None:
    # Ignoring cancelled vouchers must not let a real duplicate through.
    draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    amended(site, ledger, docstatus=0)
    ledger.insert(JOURNAL_ENTRY, {TRANSACTION_ID_FIELD: CODED})
    with pytest.raises(PostingError, match="more than one live document"):
        draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)


def test_a_draft_whose_bank_transaction_is_reconciled_is_reported_not_deleted(posting_ctx, ledger, site) -> None:
    # The person matched the statement line to something else. Deleting would
    # be tidy and would occasionally destroy a draft they had edited.
    draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    name = next(n for (doctype, n) in site.documents if doctype == JOURNAL_ENTRY)
    site.documents[("Bank Transaction", "BT-1")] = {
        TRANSACTION_ID_FIELD: CODED,
        "status": "Reconciled",
        "docstatus": 1,
    }
    report = draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert report.attention == []
    assert [(u.transaction_id, u.journal_entry) for u in report.unneeded] == [(CODED, name)]
    assert (JOURNAL_ENTRY, name) in site.documents
    assert [r.method for r in site.requests if r.method == "DELETE"] == []


def test_a_row_reconciled_before_this_ever_drafted_gets_nothing(posting_ctx, ledger, site) -> None:
    # A person matched that statement line already. Drafting now would be
    # clutter, and clutter this would then report as unneeded every run after.
    site.documents[("Bank Transaction", "BT-1")] = {
        TRANSACTION_ID_FIELD: CODED,
        "status": "Reconciled",
        "docstatus": 1,
    }
    report = draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert (report.drafted, report.planned, report.attention) == ([], [], [])
    assert vouchers_on(site) == []


def test_an_unreconciled_bank_transaction_leaves_the_draft_alone(posting_ctx, ledger, site) -> None:
    draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    site.documents[("Bank Transaction", "BT-1")] = {
        TRANSACTION_ID_FIELD: CODED,
        "status": "Unreconciled",
        "docstatus": 1,
    }
    report = draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert report.attention == []


def test_two_vouchers_for_one_transaction_refuse_rather_than_pick_one(posting_ctx, ledger) -> None:
    # The state the lookup exists to refuse. Rewriting one at random leaves
    # the other standing in the ledger unnoticed.
    draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    ledger.insert(JOURNAL_ENTRY, {TRANSACTION_ID_FIELD: CODED})
    with pytest.raises(PostingError, match="more than one live document"):
        plan_voucher(
            ledger, coded(EXPENSE_CODE), journal_entry(coded(EXPENSE_CODE), "5215", posting_ctx), EXAMPLE_BOOKS.bot.user
        )


def test_the_plan_names_what_it_would_do(posting_ctx, ledger) -> None:
    transaction = coded(EXPENSE_CODE)
    doc = journal_entry(transaction, "5215", posting_ctx)
    assert plan_voucher(ledger, transaction, doc, EXAMPLE_BOOKS.bot.user)[0] is Outcome.DRAFTED
    draft_vouchers([transaction], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert plan_voucher(ledger, transaction, doc, EXAMPLE_BOOKS.bot.user)[0] is Outcome.UNCHANGED


# --- closed periods ------------------------------------------------------------


def test_a_transaction_dated_on_the_lock_drafts_no_voucher(posting_ctx, ledger, site) -> None:
    # CODED's accounting date is 2026-08-06 -- exactly the lock date. "On or
    # before" includes the day itself.
    closed_ctx = dataclasses.replace(posting_ctx, lock=LockState(closed_through=dt.date(2026, 8, 6), bypass_role=None))
    report = draft_vouchers([coded(EXPENSE_CODE)], closed_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert (report.drafted, report.updated, report.attention) == ([], [], [])
    assert vouchers_on(site) == []
    assert report.closed == [
        f"{CODED} 2026-08-06 5215 - Phone & Internet (Verizon, Comcast): closed period — reopen 2026Q3 in ERPNext (move Books "
        "Closed Through back to 2026-06-30), book it there (the next sync drafts its voucher), and run "
        "`runningtab period close --quarter 2026Q3` again, which stores a new revision"
    ]


def test_a_transaction_dated_before_the_lock_drafts_no_voucher(posting_ctx, ledger, site) -> None:
    closed_ctx = dataclasses.replace(posting_ctx, lock=LockState(closed_through=dt.date(2026, 9, 1), bypass_role=None))
    report = draft_vouchers([coded(EXPENSE_CODE)], closed_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert (report.drafted, report.updated) == ([], [])
    assert len(report.closed) == 1
    assert vouchers_on(site) == []


def test_a_row_in_an_earlier_closed_quarter_says_to_reclose_every_quarter_after_it(posting_ctx, ledger, site) -> None:
    # Q3 and Q4 are both closed: reopening Q3 means Q4 has to be re-closed
    # after it, in order, or `period close` refuses Q4.
    closed_ctx = dataclasses.replace(
        posting_ctx, lock=LockState(closed_through=dt.date(2026, 12, 31), bypass_role=None)
    )
    report = draft_vouchers([coded(EXPENSE_CODE)], closed_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    (line,) = report.closed
    assert "reopen 2026Q3 in ERPNext" in line
    assert "then re-close every closed quarter after it, in order" in line
    assert "open quarter" not in line


def test_a_transaction_dated_after_the_lock_drafts_normally(posting_ctx, ledger, site) -> None:
    open_ctx = dataclasses.replace(posting_ctx, lock=LockState(closed_through=dt.date(2026, 8, 5), bypass_role=None))
    report = draft_vouchers([coded(EXPENSE_CODE)], open_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert len(report.drafted) == 1
    assert report.closed == []


def test_no_lock_known_drafts_exactly_as_before(posting_ctx, ledger, site) -> None:
    # `posting_ctx` carries the default LockState(closed_through=None, ...):
    # the shape a Company not yet re-provisioned reads back, or any caller
    # that never learned the lock. Every candidate drafts as it always has.
    assert posting_ctx.lock.closed_through is None
    report = draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert len(report.drafted) == 1
    assert report.closed == []


def test_a_closed_row_already_booked_by_a_submitted_voucher_is_not_reported(posting_ctx, ledger, site) -> None:
    # The normal state of every row in a closed quarter: booked, then closed.
    # The voucher pass sweeps every transaction on every run, so reporting
    # these would put every sync after the first close into needs_attention
    # and tell someone to reopen a quarter that closed correctly.
    draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    name = next(n for (doctype, n) in site.documents if doctype == JOURNAL_ENTRY)
    site.documents[(JOURNAL_ENTRY, name)]["docstatus"] = 1
    closed_ctx = dataclasses.replace(posting_ctx, lock=LockState(closed_through=dt.date(2026, 9, 30), bypass_role=None))

    report = draft_vouchers([coded(EXPENSE_CODE)], closed_ctx, ledger, own_account_ids=OWN_ACCOUNTS)

    assert (report.closed, report.attention, report.drafted, report.updated) == ([], [], [], [])


def test_a_closed_row_whose_bank_line_a_person_matched_is_not_reported(posting_ctx, ledger, site) -> None:
    # Booked by a voucher of the person's own: the statement line is
    # reconciled and no draft of ours exists.
    site.documents[("Bank Transaction", "BT-1")] = {
        TRANSACTION_ID_FIELD: CODED,
        "status": "Reconciled",
        "docstatus": 1,
    }
    closed_ctx = dataclasses.replace(posting_ctx, lock=LockState(closed_through=dt.date(2026, 9, 30), bypass_role=None))

    report = draft_vouchers([coded(EXPENSE_CODE)], closed_ctx, ledger, own_account_ids=OWN_ACCOUNTS)

    assert (report.closed, report.attention, report.drafted) == ([], [], [])


def test_a_closed_row_whose_only_voucher_was_cancelled_is_still_reported(posting_ctx, ledger, site) -> None:
    # A cancelled voucher (docstatus 2) booked nothing: the row never reached
    # the ledger, so it is late activity for a person to reopen for.
    draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    name = next(n for (doctype, n) in site.documents if doctype == JOURNAL_ENTRY)
    site.documents[(JOURNAL_ENTRY, name)]["docstatus"] = 2
    closed_ctx = dataclasses.replace(posting_ctx, lock=LockState(closed_through=dt.date(2026, 9, 30), bypass_role=None))

    report = draft_vouchers([coded(EXPENSE_CODE)], closed_ctx, ledger, own_account_ids=OWN_ACCOUNTS)

    assert len(report.closed) == 1
    assert report.closed[0].startswith(f"{CODED} 2026-08-06 ")


def test_a_closed_row_matched_elsewhere_with_our_draft_left_asks_for_the_draft_not_a_reopen(
    posting_ctx, ledger, site
) -> None:
    # The person reconciled the statement line to a voucher of their own and
    # left our draft standing. The row is booked -- no reopen -- and the
    # draft is clutter a person deletes, said the same way as in an open
    # quarter.
    draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    name = next(n for (doctype, n) in site.documents if doctype == JOURNAL_ENTRY)
    site.documents[("Bank Transaction", "BT-1")] = {
        TRANSACTION_ID_FIELD: CODED,
        "status": "Reconciled",
        "docstatus": 1,
    }
    closed_ctx = dataclasses.replace(posting_ctx, lock=LockState(closed_through=dt.date(2026, 9, 30), bypass_role=None))

    report = draft_vouchers([coded(EXPENSE_CODE)], closed_ctx, ledger, own_account_ids=OWN_ACCOUNTS)

    assert report.closed == []
    assert report.attention == []
    assert [(u.transaction_id, u.journal_entry) for u in report.unneeded] == [(CODED, name)]


def test_a_closed_transaction_does_not_update_a_draft_from_before_the_close(posting_ctx, ledger, site) -> None:
    # A draft the bot proposed while the quarter was still open is left
    # standing rather than rewritten -- this run is not the one deciding what
    # replaces it.
    draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    before = {key: dict(doc) for key, doc in site.documents.items()}
    closed_ctx = dataclasses.replace(posting_ctx, lock=LockState(closed_through=dt.date(2026, 8, 6), bypass_role=None))
    report = draft_vouchers([coded("5110 - Cloud Hosting")], closed_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    assert (report.updated, report.attention) == ([], [])
    assert len(report.closed) == 1
    assert site.documents == before


# --- the boundary -------------------------------------------------------------


def test_no_request_to_journal_entry_ever_carries_a_docstatus(posting_ctx, ledger, site) -> None:
    # The bot holds no submit on Journal Entry and `runningtab access verify` proves
    # it against the server; this is the thing a test can assert. `0` is the
    # default when the key is absent, so its absence is the guarantee.
    draft_vouchers([coded(EXPENSE_CODE)], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    draft_vouchers([coded("5110 - Cloud Hosting")], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    written = [r for r in site.requests if r.method in {"POST", "PUT"}]
    assert written
    for request in written:
        assert JOURNAL_ENTRY in request.url.path
        assert "docstatus" not in json.loads(request.content)


def test_a_dry_run_writes_nothing_and_plans_in_the_reports_verbs(posting_ctx, ledger, site) -> None:
    transaction = coded(EXPENSE_CODE)
    planned = draft_vouchers([transaction], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS, dry_run=True)
    assert planned.planned == [f"draft {JOURNAL_ENTRY} for {CODED}"]
    assert site.documents == {}
    draft_vouchers([transaction], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS)
    name = next(n for (doctype, n) in site.documents if doctype == JOURNAL_ENTRY)
    update = draft_vouchers(
        [coded("5110 - Cloud Hosting")], posting_ctx, ledger, own_account_ids=OWN_ACCOUNTS, dry_run=True
    )
    # The dry run reads the ledger exactly as a real run does, so it plans an
    # update where the real run would update rather than calling it a draft.
    assert update.planned == [f"update {JOURNAL_ENTRY} {name} for {CODED}"]


def test_the_bot_identity_is_the_one_access_provisions(posting_ctx) -> None:
    # A run under somebody else's credential sees every draft as theirs and
    # writes nothing, which is the safe direction.
    assert posting_ctx.bot_user == EXAMPLE_BOOKS.bot.user


def test_every_code_the_chart_offers_parses(chart) -> None:
    # The selection rule reads the number out of the string the bank reports,
    # which is the string `runningtab coa export-gl-codes` wrote. A chart entry the
    # rule cannot parse would be a code a person could pick and never see
    # honoured.
    for number, name in chart.postable.items():
        transaction = coded(f"{number} - {name}")
        parsed, unplaceable = coded_account(transaction, chart)
        assert unplaceable is None
        assert parsed in {None, number}


def test_the_chart_is_the_shipped_one_and_holds_only_postable_accounts(chart) -> None:
    numbers = {account.number for account in load_chart(EXAMPLE_BOOKS.chart_path)}
    assert "5215" in chart.postable
    assert "5100" in numbers and "5100" not in chart.postable  # a group; nothing posts to it
