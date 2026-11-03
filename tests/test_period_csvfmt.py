"""The two formatting rules every file in the package shares."""

import csv
import io
from decimal import Decimal

import pytest

from runningtab.period import PeriodError
from runningtab.period.csvfmt import money, render_csv


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (Decimal("0"), "0.00"),
        (Decimal("12.5"), "12.50"),
        (Decimal("-1234567.89"), "-1234567.89"),
        (Decimal("-0.00"), "0.00"),
    ],
)
def test_money_is_two_places_with_no_separator(value, text):
    assert money(value) == text


def test_money_that_would_need_rounding_is_refused():
    with pytest.raises(PeriodError, match=r"0\.005"):
        money(Decimal("0.005"))


def test_awkward_names_survive_the_round_trip():
    names = ["Müller, Inc.", '"Best" Bikes', "Line\nbreak"]
    data = render_csv(["name"], [[n] for n in names])
    assert data.decode("utf-8").endswith("\n")
    assert "\r" not in data.decode("utf-8").replace("Line\nbreak", "")
    assert [row[0] for row in csv.reader(io.StringIO(data.decode("utf-8")))][1:] == names


def test_the_same_rows_render_to_the_same_bytes():
    rows = [["a", "1.00"], ["b", "2.00"]]
    assert render_csv(["k", "v"], rows) == render_csv(["k", "v"], rows)


def _rendered_cell(cell, **kwargs):
    data = render_csv(["remarks"], [[cell]], **kwargs).decode("utf-8")
    return list(csv.reader(io.StringIO(data)))[1][0]


@pytest.mark.parametrize("cell", ['=HYPERLINK("http://evil")', "+1", "@x", "-cmd"])
def test_a_cell_that_would_run_as_a_formula_is_prefixed_with_an_apostrophe(cell):
    # A bank counterparty's remarks are text from outside our control, not
    # something we authored -- a cell like this must not open as a formula
    # the moment a spreadsheet reads the file.
    assert _rendered_cell(cell) == f"'{cell}"


@pytest.mark.parametrize("cell", ["-12.00", "Müller, Inc.", '"Best" Bikes'])
def test_ordinary_text_and_a_plain_negative_amount_are_untouched(cell):
    # `-12.00` is money()'s own output for a negative amount; a `-` cell
    # only needs escaping when it is not a plain negative number.
    assert _rendered_cell(cell) == cell


def test_spreadsheet_safe_false_leaves_a_formula_like_cell_exactly_as_given():
    # The qbo/ import files must stay exact: QuickBooks receives what was
    # computed, with nothing added.
    assert _rendered_cell("=x", spreadsheet_safe=False) == "=x"


def test_two_builds_with_escaped_cells_are_still_byte_identical():
    rows = [["=x"], ["-12.00"], ["Müller, Inc."]]
    assert render_csv(["remarks"], rows) == render_csv(["remarks"], rows)
