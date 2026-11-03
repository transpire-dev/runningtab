"""The capital-call arithmetic, on a synthetic forecast file."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal

import pytest

from runningtab.forecast.compute import compute, window
from runningtab.forecast.model import Amount, CallPolicy, ForecastFile, Line
from runningtab.mercury.models import Status, Transaction

NOW = dt.datetime(2026, 10, 26, 9, 0, tzinfo=dt.UTC)
MEMBERS = ("3110", "3120", "3130")


def line(
    name: str,
    match: tuple[str, ...],
    low: str,
    high: str,
    *,
    cadence: Literal["monthly", "yearly"] = "monthly",
    day: int | None = 5,
    on: dt.date | None = None,
    paid: Literal["mercury", "reimbursed"] = "mercury",
    starts: dt.date | None = None,
    ends: dt.date | None = None,
) -> Line:
    return Line(
        name=name,
        account="5215",
        match=match,
        cadence=cadence,
        day=day,
        on=on,
        amount=Amount(Decimal(low), Decimal(high)),
        paid=paid,
        starts=starts,
        ends=ends,
    )


def forecast_file(*lines: Line, floor: Decimal | None = None) -> ForecastFile:
    return ForecastFile(lines, CallPolicy(MEMBERS, floor))


A = line("A", ("Acme",), "100", "100", day=5)
B = line("B", ("Beta",), "50", "80", day=28)


def txn(name: str, amount: str, on: dt.date, status: Status = Status.SENT) -> Transaction:
    return Transaction(
        id="t",
        account_id="a",
        amount=Decimal(amount),
        status=status,
        raw_status=status.value,
        kind="other",
        created_at=dt.datetime.combine(on, dt.time(12), tzinfo=dt.UTC),
        posted_at=dt.datetime.combine(on, dt.time(12), tzinfo=dt.UTC),
        counterparty_name=name,
    )


def run(file: ForecastFile, transactions=(), opening="300"):
    return compute(
        file,
        now=NOW,
        opening_cash=Decimal(opening),
        ledger_through=dt.date(2026, 10, 25),
        transactions=transactions,
    )


def d(s: str) -> Decimal:
    return Decimal(s)


def test_the_window_runs_through_the_third_full_month():
    assert window(dt.date(2026, 10, 26)) == (dt.date(2026, 10, 26), dt.date(2027, 1, 31))


def test_a_charge_seen_this_month_is_not_counted_again():
    f = run(forecast_file(A, B), [txn("Acme", "-100", dt.date(2026, 10, 5))])
    assert [m.outflows for m in f.months] == [d("80"), d("180"), d("180"), d("180")]
    assert [m.closing for m in f.months] == [d("220"), d("40"), d("-140"), d("-320")]
    assert (f.floor, f.floor_from) == (d("100"), "A")
    assert f.members == (("3110", d("140.00")), ("3120", d("140.00")), ("3130", d("140.00")))
    assert f.total_call == d("420.00")
    assert f.scenario == "conservative"
    assert [x.inflows for x in f.months] == [0, 0, 0, 0]


def test_a_charge_not_seen_yet_is_still_owed():
    f = run(forecast_file(A, B))
    assert f.months[0].outflows == d("180")
    assert [x.status for x in f.dues if x.due.month == 10] == ["owed", "owed"]


def test_a_differently_spelled_counterparty_does_not_count_as_paid():
    f = run(forecast_file(A, B), [txn("Acme Corp", "-100", dt.date(2026, 10, 5))])
    assert f.months[0].outflows == d("180")


def test_matching_ignores_case():
    f = run(forecast_file(A, B), [txn("ACME", "-100", dt.date(2026, 10, 5))])
    assert f.months[0].outflows == d("80")


def test_a_refund_from_the_counterparty_is_not_a_payment():
    f = run(forecast_file(A, B), [txn("Acme", "100", dt.date(2026, 10, 5))])
    assert f.months[0].outflows == d("180")


def test_a_pending_charge_is_not_in_the_opening_balance_so_it_is_still_owed():
    f = run(forecast_file(A, B), [txn("Acme", "-100", dt.date(2026, 10, 5), Status.PENDING)])
    assert f.months[0].outflows == d("180")


def test_a_charge_last_month_does_not_mark_this_month_paid():
    f = run(forecast_file(A, B), [txn("Acme", "-100", dt.date(2026, 9, 5))])
    assert f.months[0].outflows == d("180")


def test_a_line_without_match_is_always_owed():
    r = line("R", (), "40", "40", paid="reimbursed", day=3)
    f = run(forecast_file(r), [txn("Anyone", "-40", dt.date(2026, 10, 3))])
    assert f.months[0].outflows == d("40")


def test_each_reimbursement_transfer_settles_the_one_line_it_pays_back():
    # Mercury pays a member back once per charge, for its exact amount. Two
    # reimbursed lines cost 19.99; one transfer of 19.99 settles only one.
    member = ("Gavin Carothers",)
    workspace = line("Workspace", member, "84", "84", paid="reimbursed", day=6)
    antigravity = line("Antigravity", member, "19.99", "19.99", paid="reimbursed", day=6)
    google_one = line("Google One", member, "19.99", "19.99", paid="reimbursed", day=11)
    zed = line("Zed", member, "10", "10", paid="reimbursed", day=16)
    f = run(
        forecast_file(workspace, antigravity, google_one, zed),
        [
            txn("Gavin Carothers", "-84", dt.date(2026, 10, 6)),
            txn("Gavin Carothers", "-19.99", dt.date(2026, 10, 6)),
        ],
    )
    october = {x.line.name: x.status for x in f.dues if x.due.month == 10}
    assert october == {"Workspace": "seen paid", "Antigravity": "seen paid", "Google One": "owed", "Zed": "owed"}
    assert f.months[0].outflows == d("29.99")


def test_day_31_falls_on_the_last_day_of_a_short_month():
    f = run(forecast_file(line("E", ("Eve",), "10", "10", day=31)))
    assert [x.due for x in f.dues] == [
        dt.date(2026, 10, 31),
        dt.date(2026, 11, 30),
        dt.date(2026, 12, 31),
        dt.date(2027, 1, 31),
    ]


def test_a_yearly_charge_in_the_window_sets_the_floor():
    y = line("Y", ("Yearly",), "684.34", "684.34", cadence="yearly", day=None, on=dt.date(2024, 1, 15))
    f = run(forecast_file(A, y))
    assert (f.floor, f.floor_from) == (d("684.34"), "Y")
    assert f.months[3].outflows == d("784.34")


def test_a_yearly_charge_outside_the_window_contributes_nothing():
    y = line("Y", ("Yearly",), "50", "50", cadence="yearly", day=None, on=dt.date(2024, 6, 15))
    f = run(forecast_file(y))
    assert f.dues == ()
    assert (f.floor, f.total_call) == (d("0"), d("0.00"))


def test_a_line_that_starts_later_has_no_earlier_dues():
    f = run(forecast_file(line("S", ("S",), "10", "10", starts=dt.date(2027, 1, 5))))
    assert [x.due for x in f.dues] == [dt.date(2027, 1, 5)]


def test_a_line_that_has_ended_has_no_later_dues():
    f = run(forecast_file(line("L", ("L",), "10", "10", ends=dt.date(2026, 11, 5))))
    assert [x.due for x in f.dues] == [dt.date(2026, 10, 5), dt.date(2026, 11, 5)]


def test_a_line_wholly_outside_its_bounds_contributes_nothing():
    f = run(forecast_file(line("L", ("L",), "10", "10", ends=dt.date(2026, 10, 4))))
    assert f.dues == ()
    assert f.months[0].outflows == d("0")


def test_starts_and_ends_are_inclusive():
    bounded = line("S", ("S",), "10", "10", starts=dt.date(2026, 11, 5), ends=dt.date(2026, 12, 5))
    f = run(forecast_file(bounded))
    assert [x.due for x in f.dues] == [dt.date(2026, 11, 5), dt.date(2026, 12, 5)]


def test_shares_round_up_so_the_members_cover_the_call():
    # No charges and a fixed floor of 100.00 on an empty account: a call of 100.00,
    # which does not split in three.
    f = run(forecast_file(floor=d("100")), opening="0")
    assert [s for _, s in f.members] == [d("33.34")] * 3
    assert f.total_call == d("100.02")


def test_a_fixed_floor_overrides_the_computed_one():
    f = run(forecast_file(A, B, floor=d("500")), opening="5000")
    assert (f.floor, f.floor_from) == (d("500"), "[call] floor")
    assert f.total_call == d("0.00")


def test_no_shortfall_means_no_call():
    f = run(forecast_file(A, B), opening="5000")
    assert f.members == (("3110", d("0.00")), ("3120", d("0.00")), ("3130", d("0.00")))
    assert f.total_call == d("0.00")


def test_the_forecast_records_what_it_was_computed_from():
    f = run(forecast_file(A))
    assert (f.computed_at, f.ledger_through, f.opening_cash) == (NOW, dt.date(2026, 10, 25), d("300"))
    assert (f.window_start, f.window_end) == (dt.date(2026, 10, 26), dt.date(2027, 1, 31))
    assert f.scenario == "conservative"


def test_with_nothing_owed_and_no_fixed_floor_the_floor_says_so():
    f = run(forecast_file())
    assert (f.floor, f.floor_from) == (d("0"), "no owed charges")


def test_a_charge_dated_after_today_does_not_mark_a_due_paid():
    f = run(forecast_file(A), [txn("Acme", "-100", dt.date(2026, 10, 27))])
    assert f.months[0].outflows == d("100")


def google_lines(workspace_day: int, antigravity_day: int) -> tuple[Line, Line]:
    return (
        line("Workspace", ("Google Workspace", "Google"), "84", "84", day=workspace_day),
        line("Antigravity", ("Google",), "19.99", "19.99", day=antigravity_day),
    )


GOOGLE_DAYS = pytest.mark.parametrize(
    ("workspace_day", "antigravity_day"), [(3, 4), (4, 3)], ids=["workspace-first", "antigravity-first"]
)


@GOOGLE_DAYS
def test_one_charge_pays_only_one_of_two_lines_sharing_a_counterparty(workspace_day, antigravity_day):
    f = run(
        forecast_file(*google_lines(workspace_day, antigravity_day)), [txn("Google", "-19.99", dt.date(2026, 10, 4))]
    )
    status = {x.line.name: x.status for x in f.dues if x.due.month == 10}
    assert status == {"Workspace": "owed", "Antigravity": "seen paid"}
    assert f.months[0].outflows == d("84")


@GOOGLE_DAYS
def test_two_charges_pay_two_lines_sharing_a_counterparty(workspace_day, antigravity_day):
    charges = [txn("Google", "-84", dt.date(2026, 10, 3)), txn("Google", "-19.99", dt.date(2026, 10, 4))]
    f = run(forecast_file(*google_lines(workspace_day, antigravity_day)), charges)
    assert {x.status for x in f.dues if x.due.month == 10} == {"seen paid"}
    assert f.months[0].outflows == d("0")


@pytest.mark.parametrize("charge", ["-1", "-89.99", "-110.01", "-5000"])
def test_a_charge_far_from_the_lines_amount_does_not_settle_it(charge):
    f = run(forecast_file(A), [txn("Acme", charge, dt.date(2026, 10, 5))])
    assert [x.status for x in f.dues if x.due.month == 10] == ["owed"]
    assert f.months[0].outflows == d("100")


@pytest.mark.parametrize("charge", ["-90", "-100", "-110"])
def test_a_charge_within_the_tolerance_band_settles_the_due(charge):
    f = run(forecast_file(A), [txn("Acme", charge, dt.date(2026, 10, 5))])
    assert [x.status for x in f.dues if x.due.month == 10] == ["seen paid"]


def test_the_band_is_measured_from_a_ranges_own_ends():
    b = line("B", ("Beta",), "50", "80", day=5)
    for charge, status in [("-44.99", "owed"), ("-45", "seen paid"), ("-88", "seen paid"), ("-88.01", "owed")]:
        f = run(forecast_file(b), [txn("Beta", charge, dt.date(2026, 10, 5))])
        assert [x.status for x in f.dues if x.due.month == 10] == [status], charge
