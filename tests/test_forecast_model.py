"""The forecast file: parsing, validation, and the checked-in content."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from runningtab.forecast.model import Amount, CallPolicy, ForecastFileError, load_forecast, parse_forecast
from tests.conftest import EXAMPLE_BOOKS, example_chart

POSTABLE = {"5110": "Cloud", "5219": "Software", "4210": "Revenue", "3110": "Capital A", "3120": "Capital B"}

CALL = '[call]\nmembers = ["3110", "3120"]\n'


def _line(**overrides: object) -> str:
    """A `[[line]]` table, valid by default, with fields replaced or (None) removed."""
    fields: dict[str, object] = {
        "name": "Thing",
        "kind": "expense",
        "account": "5219",
        "match": ["Thing"],
        "cadence": "monthly",
        "day": 5,
        "amount": 200,
        "paid": "mercury",
    }
    fields.update(overrides)
    rows = []
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, dt.date):
            rendered = value.isoformat()
        elif isinstance(value, str):
            rendered = f'"{value}"'
        elif isinstance(value, list):
            rendered = "[" + ", ".join(f'"{v}"' for v in value) + "]"
        else:
            rendered = str(value)
        rows.append(f"{key} = {rendered}")
    return "[[line]]\n" + "\n".join(rows) + "\n"


def _parse(*lines: str, call: str = CALL):
    return parse_forecast(call + "\n".join(lines), postable=POSTABLE)


def test_a_fixed_amount_is_a_range_of_one():
    line = _parse(_line(amount=200)).lines[0]
    assert line.amount == Amount(Decimal("200"), Decimal("200"))


def test_a_range_keeps_both_ends():
    line = _parse(_line(amount=None, low=150, high=275)).lines[0]
    assert line.amount == Amount(Decimal("150"), Decimal("275"))


def test_an_account_not_in_the_chart_is_refused():
    with pytest.raises(ForecastFileError, match="5999"):
        _parse(_line(account="5999"))


def test_a_non_expense_account_is_refused():
    with pytest.raises(ForecastFileError, match="4210"):
        _parse(_line(account="4210"))


def test_a_kind_other_than_expense_is_refused_until_it_is_built():
    with pytest.raises(ForecastFileError, match="not supported yet"):
        _parse(_line(kind="income"))


def test_low_above_high_is_refused():
    with pytest.raises(ForecastFileError, match=r"low 300 is above high 275"):
        _parse(_line(amount=None, low=300, high=275))


def test_a_mercury_line_with_no_match_is_refused():
    with pytest.raises(ForecastFileError, match=r"line 'Thing': match is required for a mercury line"):
        _parse(_line(match=None))


def test_a_reimbursed_line_may_omit_match():
    line = _parse(_line(match=None, paid="reimbursed")).lines[0]
    assert line.match == ()


def test_monthly_needs_a_day_and_yearly_a_date():
    with pytest.raises(ForecastFileError, match=r"needs day = 1\.\.31"):
        _parse(_line(day=None))
    with pytest.raises(ForecastFileError, match="needs on = a date"):
        _parse(_line(cadence="yearly", day=None))
    yearly = _parse(_line(cadence="yearly", day=None, on=dt.date(2027, 4, 29))).lines[0]
    assert (yearly.day, yearly.on) == (None, dt.date(2027, 4, 29))
    monthly = _parse(_line()).lines[0]
    assert (monthly.day, monthly.on) == (5, None)


@pytest.mark.parametrize("day", [0, 32])
def test_a_day_outside_the_month_is_refused(day):
    with pytest.raises(ForecastFileError, match=r"needs day = 1\.\.31"):
        _parse(_line(day=day))


@pytest.mark.parametrize(
    "text",
    [
        "line = 5\n" + CALL,
        "line = [5]\n" + CALL,
        "call = 5\n",
        "",
    ],
)
def test_malformed_shapes_are_refused_plainly(text):
    with pytest.raises(ForecastFileError, match=r"needs a \[call\] table|line must be an array"):
        parse_forecast(text, postable=POSTABLE)


def test_an_unknown_field_is_refused_by_name():
    with pytest.raises(ForecastFileError, match=r"line 'Thing': unknown field\(s\) colour"):
        _parse(_line(colour="red"))


def test_an_invalid_paid_is_refused():
    with pytest.raises(ForecastFileError, match=r"line 'Thing': paid must be 'mercury' or 'reimbursed'"):
        _parse(_line(paid="cash"))


def test_a_negative_amount_is_refused():
    with pytest.raises(ForecastFileError, match=r"line 'Thing': amounts must not be negative"):
        _parse(_line(amount=-5))


@pytest.mark.parametrize("field", ["starts", "ends"])
def test_a_non_date_bound_is_refused(field):
    with pytest.raises(ForecastFileError, match=rf"line 'Thing': {field} must be a TOML date"):
        _parse(_line(**{field: "soon"}))


def test_a_call_floor_override_parses():
    f = _parse(_line(), call=CALL + "floor = 250.50\n")
    assert f.call.floor == Decimal("250.50")


def test_invalid_toml_is_refused():
    with pytest.raises(ForecastFileError, match="forecast file is not valid TOML"):
        parse_forecast("[call\nmembers = ", postable=POSTABLE)


def test_the_example_reimbursed_line_matches_the_member():
    f = load_forecast(EXAMPLE_BOOKS.forecast_path, example_chart())
    by_name = {line.name: line for line in f.lines}
    assert (by_name["Microsoft 365"].paid, by_name["Microsoft 365"].match) == (
        "reimbursed",
        ("Dana Whitfield",),
    )
    assert by_name["Linear"].match == ("Linear Orbit",)


def test_duplicate_members_are_refused():
    with pytest.raises(ForecastFileError, match="listed more than once"):
        _parse(_line(), call='[call]\nmembers = ["3110", "3110"]\n')


def test_a_negative_floor_is_refused():
    with pytest.raises(ForecastFileError, match="must not be negative"):
        _parse(_line(), call=CALL + "floor = -1\n")


def test_duplicate_line_names_are_refused():
    with pytest.raises(ForecastFileError, match="duplicate line name"):
        _parse(_line(), _line())


def test_starts_after_ends_is_refused():
    with pytest.raises(ForecastFileError, match="is later than ends"):
        _parse(_line(starts=dt.date(2027, 2, 1), ends=dt.date(2027, 1, 1)))


def test_a_yearly_leap_day_is_refused():
    with pytest.raises(ForecastFileError, match="does not recur every year"):
        _parse(_line(cadence="yearly", day=None, on=dt.date(2028, 2, 29)))


def test_reserved_fields_are_accepted_and_ignored():
    line = _parse(_line(lag_days=30, confidence="committed")).lines[0]
    assert line.name == "Thing"


def test_members_must_be_member_capital_accounts():
    with pytest.raises(ForecastFileError, match="5219"):
        _parse(_line(), call='[call]\nmembers = ["3110", "5219"]\n')


def test_the_example_file_loads():
    f = load_forecast(EXAMPLE_BOOKS.forecast_path, example_chart())
    assert {line.name for line in f.lines} >= {"Linear", "AWS", "GitHub"}
    cloud = next(line for line in f.lines if line.name == "AWS")
    assert (cloud.amount.low, cloud.amount.high, cloud.account) == (Decimal("310"), Decimal("540"), "5110")
    assert f.call == CallPolicy(members=("3110", "3120", "3130"), floor=None)
