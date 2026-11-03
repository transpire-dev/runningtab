"""What `runningtab health` decides, and why it exits the way it does.

The balance-invariant cases run on the Mercury fixtures; see
`tests/fixtures/mercury/README.md` for their provenance. The numbers
reproduce the two discrepancies measured against the sandbox during the
investigation: an account off by 80.00 with a failed -70.00 and a pending
-10.00, and another off by 3.50 with a pending -3.50.
"""

import dataclasses
import datetime as dt
from decimal import Decimal
from typing import Any

from runningtab.health import (
    AccountLedger,
    Check,
    Outcome,
    SuspenseEntry,
    check_balance_invariant,
    check_suspense,
    event_cursor_placeholder,
    exit_code,
    guarded,
    read_suspense_entries,
    reconcile,
    render,
    report,
    settled_total,
    suspense_account_name,
    suspense_contents,
    suspense_state,
    unclassified,
    unclassified_statuses,
    uncoded_spend_placeholder,
    webhook_subscription_placeholder,
)
from runningtab.mercury.models import parse_status
from runningtab.period import PeriodError
from tests.test_mercury import fixture, parse_accounts, parse_credit_accounts, parse_transactions

CHECKING = "5f1b2a4c-9d6e-4c2f-8a10-3b7d5e9f1c02"
SAVINGS = "7c3e8d21-4f5a-4b6c-9e0d-1a2b3c4d5e6f"
CREDIT = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"


def ledgers(*, checking_fixture: str = "transactions-checking") -> list[AccountLedger]:
    """The three fixture accounts, each with its own transactions."""
    accounts = {a.id: a for a in parse_accounts(fixture("accounts"))}
    credit = parse_credit_accounts(fixture("credit"))[0]
    rows = {
        CHECKING: parse_transactions(fixture(checking_fixture)),
        SAVINGS: parse_transactions(fixture("transactions-savings")),
        CREDIT: parse_transactions(fixture("transactions-credit")),
    }
    return [
        AccountLedger(CHECKING, accounts[CHECKING].label, accounts[CHECKING].current_balance, tuple(rows[CHECKING])),
        AccountLedger(SAVINGS, accounts[SAVINGS].label, accounts[SAVINGS].current_balance, tuple(rows[SAVINGS])),
        AccountLedger(CREDIT, credit.label, credit.current_balance, tuple(rows[CREDIT])),
    ]


# --- the balance invariant ------------------------------------------------


def test_settled_total_leaves_out_pending_and_failed():
    transactions = parse_transactions(fixture("transactions-checking"))

    # Everything: 10000.00 - 2000.00 - 70.00 - 10.00
    assert sum(t.amount for t in transactions) == Decimal("7920.00")
    # What currentBalance counts: 10000.00 - 2000.00
    assert settled_total(transactions) == Decimal("8000.00")


def test_the_measured_discrepancies_are_exactly_the_unsettled_rows():
    results = {r.account_id: r for r in reconcile(ledgers())}

    assert results[CHECKING].delta == 0
    assert results[CHECKING].unsettled_count == 2  # the failed -70.00 and the pending -10.00
    assert results[SAVINGS].delta == 0
    assert results[SAVINGS].unsettled_count == 1  # the pending -3.50


def test_every_account_reconciles_including_the_one_accounts_omits():
    check = check_balance_invariant(ledgers())

    assert check.outcome is Outcome.PASS
    assert check.summary == "3 accounts reconcile exactly"
    # The credit account is only ever returned by /credit; leaving it out
    # would make this check pass while never looking at the card.
    assert any("credit account " in line for line in check.detail)


def test_an_unpaged_read_fails_the_invariant():
    # The failure the invariant actually caught during the investigation: the
    # probe never paged past its row limit, so the settled sum was short and
    # nothing else noticed.
    check = check_balance_invariant(ledgers(checking_fixture="transactions-checking-unpaged"))

    assert check.outcome is Outcome.FAIL
    assert check.summary == "1 of 3 accounts do not reconcile"
    assert any("off by 2,000.00" in line for line in check.detail)


def test_no_accounts_at_all_is_an_error_not_a_pass():
    check = check_balance_invariant([])

    assert check.outcome is Outcome.ERROR


def _with_status(ledger: AccountLedger, status: str) -> AccountLedger:
    """The same account with one more transaction, wearing `status` and worth nothing."""
    parsed, raw = parse_status(status)
    extra = dataclasses.replace(ledger.transactions[0], id="added", amount=Decimal(0), status=parsed, raw_status=raw)
    return dataclasses.replace(ledger, transactions=(*ledger.transactions, extra))


def test_a_status_nobody_has_classified_fails_even_when_the_sum_still_balances():
    # `cancelled`, `reversed`, and `blocked` are in the bank's status enum and
    # none of them occurred in the reads the settled/unsettled split was
    # measured from. Counting one, or dropping one, are guesses that are wrong
    # in the same silent way; the report says it saw one instead.
    accounts = ledgers()
    accounts[0] = _with_status(accounts[0], "cancelled")

    check = check_balance_invariant(accounts)

    assert check.outcome is Outcome.FAIL
    assert check.summary == "1 transactions carry an unclassified status"
    assert any("1 cancelled" in line for line in check.detail)


def test_an_unclassified_row_is_not_counted_among_the_pending_and_failed():
    # The per-account line names the split it is showing. Counting a status
    # nobody has classified as "pending/failed" asserts the one thing that is
    # not known about the row this check exists to single out.
    accounts = ledgers()
    accounts[0] = _with_status(accounts[0], "cancelled")

    result = {r.account_id: r for r in reconcile(accounts)}[CHECKING]

    assert result.unsettled_count == 2  # still the failed -70.00 and the pending -10.00
    assert result.unclassified_count == 1

    check = check_balance_invariant(accounts)
    assert any("2 settled, 2 pending/failed, 1 unclassified" in line for line in check.detail)


def test_an_unclassified_status_is_reported_alongside_a_real_discrepancy():
    accounts = ledgers(checking_fixture="transactions-checking-unpaged")
    accounts[1] = _with_status(accounts[1], "blocked")

    check = check_balance_invariant(accounts)

    assert check.summary == "1 of 3 accounts do not reconcile; 1 transactions carry an unclassified status"


def test_a_status_outside_the_measured_split_is_never_taken_as_measured():
    # `cancelled`, `reversed`, and `blocked` are in the bank's enum and none of
    # them appeared in the reads that established which statuses
    # currentBalance counts. The sum counts them, the way the ingest does; this
    # report is the thing that says nobody has checked.
    row = parse_transactions(fixture("transactions-checking"))[0]

    assert not unclassified(row)
    for status in ("cancelled", "reversed", "blocked"):
        parsed, raw = parse_status(status)
        unknown = dataclasses.replace(row, status=parsed, raw_status=raw)
        assert not unknown.is_postable
        assert unclassified(unknown)


def test_a_status_mercury_adds_later_is_reported_under_its_own_name():
    # An unrecognised status parses to `blocked`, which would send whoever
    # reads the report off to decide about the wrong word.
    accounts = ledgers()
    accounts[0] = _with_status(accounts[0], "settling")

    assert unclassified_statuses(accounts) == {"settling": 1}


def test_unclassified_statuses_are_counted_by_name():
    accounts = ledgers()
    accounts[0] = _with_status(accounts[0], "reversed")
    accounts[1] = _with_status(accounts[1], "reversed")
    accounts[2] = _with_status(accounts[2], "blocked")

    assert unclassified_statuses(accounts) == {"blocked": 1, "reversed": 2}


# --- suspense contents ----------------------------------------------------


def entry(day: int, amount: str, *, voucher: str = "JE-0001", remarks: str = "") -> SuspenseEntry:
    return SuspenseEntry(
        posting_date=dt.date(2026, 3, day),
        creation=f"2026-03-{day:02d} 09:00:00",
        amount=Decimal(amount),
        voucher_type="Journal Entry",
        voucher_no=voucher,
        remarks=remarks,
    )


def test_an_empty_suspense_account_passes():
    state = suspense_state([], dt.date(2026, 4, 1))

    assert state.is_empty
    assert state.open_since is None
    assert check_suspense(state, "5299 - UNCATEGORISED (needs review) - T").outcome is Outcome.PASS


def test_money_sitting_in_suspense_fails_and_is_aged():
    entries = [entry(10, "125.00", remarks="raw category: Shipping & Freight")]

    state = suspense_state(entries, dt.date(2026, 4, 1))

    assert state.balance == Decimal("125.00")
    assert state.open_since == dt.date(2026, 3, 10)
    assert state.age_days == 22
    check = check_suspense(state, "5299 - UNCATEGORISED (needs review) - T")
    assert check.outcome is Outcome.FAIL
    assert "125.00 sitting in" in check.summary
    assert any("Shipping & Freight" in line for line in check.detail)


def test_a_cleared_account_is_not_aged_from_its_oldest_entry():
    # Reclassifying leaves the original entry in place with an opposite one
    # beside it. Ageing from the oldest row would report a year-old problem
    # on an account somebody emptied yesterday.
    entries = [entry(1, "125.00"), entry(2, "-125.00", voucher="JE-0002")]

    state = suspense_state(entries, dt.date(2026, 12, 31))

    assert state.is_empty
    assert state.entry_count == 2
    assert state.age_days == 0


def test_ageing_runs_from_the_last_time_the_account_was_empty():
    entries = [
        entry(1, "125.00"),
        entry(2, "-125.00", voucher="JE-0002"),
        entry(20, "40.00", voucher="JE-0003"),
    ]

    state = suspense_state(entries, dt.date(2026, 4, 1))

    assert state.balance == Decimal("40.00")
    assert state.open_since == dt.date(2026, 3, 20)
    assert state.age_days == 12


def test_only_the_entries_behind_the_current_balance_are_listed():
    # An account cleared and refilled prints the rows somebody has to deal
    # with, not its whole history.
    entries = [
        entry(1, "125.00"),
        entry(2, "-125.00", voucher="JE-0002"),
        entry(20, "40.00", voucher="JE-0003", remarks="raw category: Legal"),
    ]

    check = check_suspense(suspense_state(entries, dt.date(2026, 4, 1)), "5299 - X - T")

    listed = [line for line in check.detail if "Journal Entry" in line]
    assert len(listed) == 1
    assert "JE-0003" in listed[0]


def test_a_same_day_clear_and_refill_separates_the_two_runs():
    # A date alone cannot tell the cleared pair from the refill when all three
    # post on one day; `creation` orders them, and the walk that finds the
    # balance is what decides where the open run starts.
    entries = [
        dataclasses.replace(entry(10, "125.00"), creation="2026-03-10 09:00:00"),
        dataclasses.replace(entry(10, "-125.00", voucher="JE-0002"), creation="2026-03-10 10:00:00"),
        dataclasses.replace(entry(10, "40.00", voucher="JE-0003"), creation="2026-03-10 11:00:00"),
    ]

    state = suspense_state(entries, dt.date(2026, 4, 1))

    assert state.balance == Decimal("40.00")
    assert [e.voucher_no for e in state.outstanding] == ["JE-0003"]
    listed = [line for line in check_suspense(state, "5299 - X - T").detail if "Journal Entry" in line]
    assert len(listed) == 1
    assert "JE-0003" in listed[0]


# `list` as a method name shadows the builtin inside the class body under
# PEP 649's lazy annotations, so any `list[...]` annotation after it resolves
# to the method. Module-level aliases sidestep it, the same way icedcoffee-sync's
# client does.
_Rows = list[dict[str, Any]]


class FakeSite:
    """A Frappe site holding one account and its ledger entries.

    Answers the two reads the suspense check makes, and records them, so a
    test can assert the read is complete rather than capped.
    """

    def __init__(self, *, account: str | None, rows: _Rows | None = None) -> None:
        self.account = account
        self.rows: _Rows = rows or []
        self.filters: list[Any] = []

    def list(self, doctype: str, **kwargs: Any) -> _Rows:
        assert doctype == "Account"
        wanted = next(value for field, _op, value in kwargs["filters"] if field == "name")
        return [{"name": self.account}] if self.account == wanted else []

    def iter_all(self, doctype: str, **kwargs: Any):
        assert doctype == "GL Entry"
        self.filters.append(kwargs["filters"])
        yield from self.rows


def gl_row(posting_date: str, debit: float, credit: float, remarks: str = "") -> dict[str, Any]:
    return {
        "posting_date": posting_date,
        "debit": debit,
        "credit": credit,
        "voucher_type": "Journal Entry",
        "voucher_no": "JE-0001",
        "remarks": remarks,
        "creation": f"{posting_date} 09:00:00",
    }


def test_gl_entries_become_signed_amounts_without_float_drift():
    site = FakeSite(account="5299 - X - T", rows=[gl_row("2026-03-10", 47.61, 0.0), gl_row("2026-03-11", 0.0, 12.34)])

    entries = read_suspense_entries(site, "Transpire LLC", "5299 - X - T")

    assert [e.amount for e in entries] == [Decimal("47.61"), Decimal("-12.34")]


def test_cancelled_entries_are_left_out_of_the_read():
    site = FakeSite(account="5299 - X - T")

    read_suspense_entries(site, "Transpire LLC", "5299 - X - T")

    assert ["is_cancelled", "=", 0] in site.filters[0]


def test_a_missing_suspense_account_is_an_error_not_an_empty_pass():
    # Reading GL entries for an account that does not exist returns nothing,
    # which is indistinguishable from a clean account unless somebody checks.
    site = FakeSite(account=None)

    check = suspense_contents(site, "Transpire LLC", "5299 - X - T", dt.date(2026, 4, 1))

    assert check.outcome is Outcome.ERROR
    assert "not on this site" in check.summary


def test_the_suspense_account_document_name_is_erpnexts_own():
    assert suspense_account_name("5299", "UNCATEGORISED (needs review)", "TL") == (
        "5299 - UNCATEGORISED (needs review) - TL"
    )


# --- the report -----------------------------------------------------------


def passing(key: str) -> Check:
    return Check(key=key, title=key, outcome=Outcome.PASS, summary="fine")


def test_the_report_is_always_six_sections_in_one_order():
    checks = report(passing("balance-invariant"), passing("suspense-contents"), passing("closed-periods"))

    assert [c.key for c in checks] == [
        "balance-invariant",
        "uncoded-spend",
        "suspense-contents",
        "webhook-subscriptions",
        "event-cursor-age",
        "closed-periods",
    ]


def test_the_three_ingest_dependent_checks_say_what_they_are_waiting_for():
    for check in (uncoded_spend_placeholder(), webhook_subscription_placeholder(), event_cursor_placeholder()):
        assert check.outcome is Outcome.SKIP
        assert check.summary == "not available: needs the Mercury ingest"
        assert check.detail  # each names its own dependency, not just the generic one


def test_a_report_of_passes_and_unavailables_exits_zero():
    checks = [passing("balance-invariant"), uncoded_spend_placeholder()]

    assert exit_code(checks, strict=False) == 0


def test_strict_makes_an_unavailable_check_a_failure():
    checks = [passing("balance-invariant"), uncoded_spend_placeholder()]

    assert exit_code(checks, strict=True) == 1


def test_a_failure_exits_non_zero_whatever_the_flag_says():
    checks = [Check(key="k", title="k", outcome=Outcome.FAIL, summary="off")]

    assert exit_code(checks, strict=False) == 1


def test_a_check_that_could_not_run_is_never_suppressed_by_the_default():
    checks = [Check(key="k", title="k", outcome=Outcome.ERROR, summary="refused")]

    assert exit_code(checks, strict=False) == 1


def test_the_rendered_report_counts_every_outcome():
    checks = [
        passing("balance-invariant"),
        uncoded_spend_placeholder(),
        Check(key="k", title="k", outcome=Outcome.FAIL, summary="off"),
    ]

    lines = render(checks)

    assert lines[-1] == "1 passed, 1 failed, 0 could not run, 1 not available"


# --- guarded ----------------------------------------------------------------


def test_a_keyerror_names_its_type_so_a_bug_is_distinguishable():
    # A bare `KeyError`'s own text is just the repr of the missing key, with
    # no context -- prefixing the type name is what tells a reader this is a
    # bug surfacing, not a named domain refusal.
    def _raise():
        raise KeyError("chart_of_accounts.csv")

    check = guarded("k", "k", _raise)

    assert check.outcome is Outcome.ERROR
    assert check.summary == "could not run: KeyError: 'chart_of_accounts.csv'"


def test_an_oserror_names_its_type():
    def _raise():
        raise OSError("disk full")

    check = guarded("k", "k", _raise)

    assert check.summary == "could not run: OSError: disk full"


def test_a_named_domain_refusal_is_not_prefixed_with_its_type():
    # PeriodError (and its siblings) already write a full sentence; a type
    # prefix here would be redundant noise, not new information.
    def _raise():
        raise PeriodError("chart drift: 5299 needs a create")

    check = guarded("k", "k", _raise)

    assert check.summary == "could not run: chart drift: 5299 needs a create"
