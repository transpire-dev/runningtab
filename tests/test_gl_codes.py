"""The CSV Mercury accepts, which is a projection of the chart."""

import csv
import io

from icedcoffee_sync.apply.model import DesiredAccount

from runningtab.chart import load_chart
from runningtab.gl_codes import gl_code_csv, gl_code_name, postable_accounts
from tests.conftest import EXAMPLE_BOOKS


def test_groups_are_excluded() -> None:
    # A transaction cannot post to a group, so offering one in the bank's
    assert all(not a.is_group for a in postable_accounts(load_chart(EXAMPLE_BOOKS.chart_path)))


def test_the_suspense_account_is_offered() -> None:
    # It has to be pickable: a human deciding "I do not know" needs somewhere
    assert any(a.number == "5299" for a in postable_accounts(load_chart(EXAMPLE_BOOKS.chart_path)))


def test_csv_is_single_column_with_no_header() -> None:
    # "No comma on any line" is stricter than "single column" -- RFC 4180
    rows = list(csv.reader(io.StringIO(gl_code_csv(load_chart(EXAMPLE_BOOKS.chart_path)))))
    assert rows
    assert all(len(row) == 1 for row in rows)


def _account(number: str) -> DesiredAccount:
    return next(a for a in postable_accounts(load_chart(EXAMPLE_BOOKS.chart_path)) if a.number == number)


def test_a_comma_between_two_digits_is_a_thousands_separator_and_is_dropped() -> None:
    account = _account("5208")
    assert account.name == "Small Equipment under $2,500"
    assert gl_code_name(account) == "5208 - Small Equipment under $2500"


def test_any_other_comma_becomes_a_slash() -> None:
    account = _account("5204")
    assert account.name == "Office Software (Microsoft 365, Slack, 1Password)"
    assert gl_code_name(account) == "5204 - Office Software (Microsoft 365 / Slack / 1Password)"

    leading = _account("5216")
    assert leading.name == "Travel, Training & Conferences"
    assert gl_code_name(leading) == "5216 - Travel / Training & Conferences"


def test_a_name_with_no_comma_is_unchanged() -> None:
    account = _account("5299")
    assert "," not in account.name, "test subject must not contain a comma"
    assert gl_code_name(account) == f"{account.number} - {account.name}"


def test_no_exported_line_contains_a_comma_or_a_double_quote() -> None:
    # Mercury's GL-code importer splits each line at the first comma and keeps
    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    offenders = [a for a in postable_accounts(chart) if "," in gl_code_name(a) or '"' in gl_code_name(a)]
    assert not offenders, (
        "Mercury's GL-code importer splits a line at the first comma and keeps the opening quote, so an exported"
        " label must contain neither. gl_code_name() left one in: "
        + "; ".join(f"{a.number} {a.name!r} -> {gl_code_name(a)!r}" for a in offenders)
    )

    for line in gl_code_csv(chart).splitlines():
        assert "," not in line and '"' not in line, f"{line!r} is a line Mercury would truncate"


def test_csv_rows_are_the_document_names_a_person_will_recognise() -> None:
    assert any(line.startswith("5299") for line in gl_code_csv(load_chart(EXAMPLE_BOOKS.chart_path)).splitlines())
