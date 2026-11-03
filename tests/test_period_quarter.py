"""Quarters and the fiscal year that holds them."""

import datetime as dt

import pytest

from runningtab.period import PeriodError
from runningtab.period.quarter import FiscalYear, Quarter, fiscal_year_for


def test_a_quarter_parses_and_knows_its_dates():
    q = Quarter.parse("2026Q3")
    assert (q.start, q.end, q.label) == (dt.date(2026, 7, 1), dt.date(2026, 9, 30), "2026Q3")


def test_the_fourth_quarter_ends_on_new_years_eve():
    assert Quarter.parse("2026Q4").end == dt.date(2026, 12, 31)


@pytest.mark.parametrize("text", ["2026q3", "2026Q5", "2026Q0", "26Q3", "2026-Q3", "", "Q3 2026", "0000Q1"])
def test_anything_but_yyyyQn_is_refused_naming_the_form(text):
    with pytest.raises(PeriodError, match=r"YYYYQn"):
        Quarter.parse(text)


@pytest.mark.parametrize(
    ("date", "label"),
    [
        (dt.date(2026, 1, 1), "2026Q1"),
        (dt.date(2026, 3, 31), "2026Q1"),
        (dt.date(2026, 4, 1), "2026Q2"),
        (dt.date(2026, 8, 15), "2026Q3"),
        (dt.date(2026, 9, 30), "2026Q3"),
        (dt.date(2026, 12, 31), "2026Q4"),
    ],
)
def test_the_quarter_containing_a_date(date, label):
    assert Quarter.containing(date).label == label


def test_the_previous_quarter_crosses_the_year():
    assert Quarter.parse("2026Q3").previous == Quarter.parse("2026Q2")
    assert Quarter.parse("2026Q1").previous == Quarter.parse("2025Q4")


def test_a_quarter_has_ended_only_after_its_last_day():
    q = Quarter.parse("2026Q3")
    assert not q.has_ended(dt.date(2026, 9, 30))
    assert q.has_ended(dt.date(2026, 10, 1))


YEARS = [
    FiscalYear("2025", dt.date(2025, 1, 1), dt.date(2025, 12, 31)),
    FiscalYear("2026", dt.date(2026, 1, 1), dt.date(2026, 12, 31)),
]


def test_the_fiscal_year_holding_the_quarter_is_found():
    assert fiscal_year_for(Quarter.parse("2026Q3"), YEARS).name == "2026"


def test_a_quarter_no_fiscal_year_holds_is_refused():
    with pytest.raises(PeriodError, match=r"2027Q1.*no ERPNext Fiscal Year"):
        fiscal_year_for(Quarter.parse("2027Q1"), YEARS)


def test_a_quarter_straddling_two_fiscal_years_is_refused():
    split = [
        FiscalYear("A", dt.date(2025, 8, 1), dt.date(2026, 7, 31)),
        FiscalYear("B", dt.date(2026, 8, 1), dt.date(2027, 7, 31)),
    ]
    with pytest.raises(PeriodError, match=r"2026Q3.*single Fiscal Year"):
        fiscal_year_for(Quarter.parse("2026Q3"), split)


def test_overlapping_fiscal_years_are_refused_rather_than_picked_between():
    twice = [*YEARS, FiscalYear("2026-dup", dt.date(2026, 1, 1), dt.date(2026, 12, 31))]
    with pytest.raises(PeriodError, match="2026, 2026-dup"):
        fiscal_year_for(Quarter.parse("2026Q3"), twice)
