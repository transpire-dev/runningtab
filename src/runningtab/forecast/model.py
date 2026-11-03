"""The forecast file: expected cash movements, as checked-in data.

`forecast.toml` sits beside the chart in the books directory. A `[call]` table
names the member capital accounts and an optional floor; each `[[line]]` table
is one recurring cost. The file is validated against the chart on load so a
typo in an account number is refused here rather than surfacing as a wrong
capital call later.

`lag_days` and `confidence` are reserved for income and payroll lines. They are
accepted and ignored so that adding them later does not invalidate the file.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import pathlib
import tomllib
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Literal, cast

from runningtab.mercury.vouchers import EXPENSE_PREFIX, MEMBER_CAPITAL_PREFIX, Chart

_RESERVED = frozenset({"lag_days", "confidence"})
_KNOWN = (
    frozenset({
        "name",
        "kind",
        "account",
        "match",
        "cadence",
        "day",
        "on",
        "amount",
        "low",
        "high",
        "paid",
        "starts",
        "ends",
    })
    | _RESERVED
)


class ForecastFileError(ValueError):
    """`forecast.toml` is not a forecast we can compute from."""


@dataclasses.dataclass(frozen=True)
class Amount:
    """What a line costs in a month. A fixed cost has `low == high`."""

    low: Decimal
    high: Decimal


@dataclasses.dataclass(frozen=True)
class Line:
    """One recurring expense.

    `day` is set for monthly lines; `on` for yearly ones, where only its month
    and day recur. `match` names the Mercury counterparties whose charge is this
    line; for a reimbursed line that is the member Mercury pays back, one
    transfer per charge. A line without `match` is always counted owed.
    """

    name: str
    account: str
    match: tuple[str, ...]
    cadence: Literal["monthly", "yearly"]
    day: int | None
    on: dt.date | None
    amount: Amount
    paid: Literal["mercury", "reimbursed"]
    starts: dt.date | None
    ends: dt.date | None


@dataclasses.dataclass(frozen=True)
class CallPolicy:
    """Who is called for capital, and an optional fixed floor overriding the computed one."""

    members: tuple[str, ...]
    floor: Decimal | None


@dataclasses.dataclass(frozen=True)
class ForecastFile:
    """The whole parsed file."""

    lines: tuple[Line, ...]
    call: CallPolicy


def _money(value: Any, where: str) -> Decimal:
    """A TOML number as Decimal, via its text so 29.16 stays 29.16."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ForecastFileError(f"{where} must be a number, got {value!r}")
    return Decimal(str(value))


def _date(value: Any, where: str) -> dt.date:
    if not isinstance(value, dt.date) or isinstance(value, dt.datetime):
        raise ForecastFileError(f"{where} must be a TOML date (YYYY-MM-DD), got {value!r}")
    return value


def _optional_date(table: Mapping[str, Any], key: str, where: str) -> dt.date | None:
    return _date(table[key], f"{where}: {key}") if key in table else None


def _parse_account(table: Mapping[str, Any], where: str, postable: Mapping[str, str]) -> str:
    account = table.get("account")
    if not isinstance(account, str) or account not in postable:
        raise ForecastFileError(f"{where}: account {account!r} is not a postable account in chart.coa")
    if not account.startswith(EXPENSE_PREFIX):
        raise ForecastFileError(f"{where}: account {account} is not an expense account")
    return account


def _parse_match(table: Mapping[str, Any], where: str, paid: str) -> tuple[str, ...]:
    match = table.get("match", [])
    if not isinstance(match, list) or not all(isinstance(m, str) and m for m in match):
        raise ForecastFileError(f"{where}: match must be a list of counterparty names")
    if paid == "mercury" and not match:
        raise ForecastFileError(f"{where}: match is required for a mercury line; the paid-this-month check needs it")
    return tuple(match)


def _parse_when(
    table: Mapping[str, Any], where: str
) -> tuple[Literal["monthly", "yearly"], int | None, dt.date | None]:
    """The cadence with its `day` (monthly) or `on` (yearly)."""
    cadence = table.get("cadence")
    if cadence == "monthly":
        day = table.get("day")
        if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 31:
            raise ForecastFileError(f"{where}: a monthly line needs day = 1..31, got {day!r}")
        if "on" in table:
            raise ForecastFileError(f"{where}: a monthly line takes day, not on")
        return "monthly", day, None
    if cadence == "yearly":
        if "on" not in table:
            raise ForecastFileError(f"{where}: a yearly line needs on = a date; its month and day recur")
        if "day" in table:
            raise ForecastFileError(f"{where}: a yearly line takes on, not day")
        on = _date(table["on"], f"{where}: on")
        if (on.month, on.day) == (2, 29):
            raise ForecastFileError(f"{where}: on {on} (Feb 29) does not recur every year; use Feb 28 or Mar 1")
        return "yearly", None, on
    raise ForecastFileError(f"{where}: cadence must be 'monthly' or 'yearly', got {cadence!r}")


def _parse_amount(table: Mapping[str, Any], where: str) -> Amount:
    if "amount" in table:
        if "low" in table or "high" in table:
            raise ForecastFileError(f"{where}: give amount or low and high, not both")
        fixed = _money(table["amount"], f"{where}: amount")
        amount = Amount(fixed, fixed)
    elif "low" in table and "high" in table:
        amount = Amount(_money(table["low"], f"{where}: low"), _money(table["high"], f"{where}: high"))
        if amount.low > amount.high:
            raise ForecastFileError(f"{where}: low {amount.low} is above high {amount.high}")
    else:
        raise ForecastFileError(f"{where}: needs amount, or both low and high")
    if amount.low < 0:
        raise ForecastFileError(f"{where}: amounts must not be negative")
    return amount


def _parse_line(table: Mapping[str, Any], index: int, postable: Mapping[str, str]) -> Line:
    name = table.get("name")
    if not isinstance(name, str) or not name:
        raise ForecastFileError(f"line #{index + 1}: name is required")
    where = f"line {name!r}"
    unknown = sorted(set(table) - _KNOWN)
    if unknown:
        raise ForecastFileError(f"{where}: unknown field(s) {', '.join(unknown)}")
    kind = table.get("kind")
    if kind != "expense":
        raise ForecastFileError(f"{where}: kind {kind!r} is not supported yet; only 'expense' is")
    paid = table.get("paid")
    if paid not in ("mercury", "reimbursed"):
        raise ForecastFileError(f"{where}: paid must be 'mercury' or 'reimbursed', got {paid!r}")
    cadence, day, on = _parse_when(table, where)
    starts = _optional_date(table, "starts", where)
    ends = _optional_date(table, "ends", where)
    if starts and ends and starts > ends:
        raise ForecastFileError(f"{where}: starts {starts} is later than ends {ends}")
    return Line(
        name=name,
        account=_parse_account(table, where, postable),
        match=_parse_match(table, where, paid),
        cadence=cadence,
        day=day,
        on=on,
        amount=_parse_amount(table, where),
        paid=paid,
        starts=starts,
        ends=ends,
    )


def _parse_call(table: Mapping[str, Any], postable: Mapping[str, str]) -> CallPolicy:
    members = table.get("members")
    if not isinstance(members, list) or not members:
        raise ForecastFileError("call: members must be a non-empty list of member capital accounts")
    for member in members:
        if not isinstance(member, str) or member not in postable or not member.startswith(MEMBER_CAPITAL_PREFIX):
            raise ForecastFileError(
                f"call: member {member!r} is not a member capital account ({MEMBER_CAPITAL_PREFIX}xx)"
            )
    duplicates = sorted({m for m in members if members.count(m) > 1})
    if duplicates:
        raise ForecastFileError(f"call: member(s) {', '.join(duplicates)} listed more than once; the split would skew")
    floor = _money(table["floor"], "call: floor") if "floor" in table else None
    if floor is not None and floor < 0:
        raise ForecastFileError(f"call: floor {floor} must not be negative")
    return CallPolicy(members=tuple(members), floor=floor)


def parse_forecast(text: str, *, postable: Mapping[str, str]) -> ForecastFile:
    """Parse and validate forecast TOML against the chart's postable accounts.

    Raises:
        ForecastFileError: naming the line and field that is wrong.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ForecastFileError(f"forecast file is not valid TOML: {error}") from error
    call = data.get("call")
    if not isinstance(call, dict):
        raise ForecastFileError("forecast file needs a [call] table")
    raw_lines = data.get("line", [])
    if not isinstance(raw_lines, list) or not all(isinstance(t, dict) for t in raw_lines):
        raise ForecastFileError("forecast file: line must be an array of [[line]] tables")
    lines = tuple(
        _parse_line(table, index, postable) for index, table in enumerate(cast("list[Mapping[str, Any]]", raw_lines))
    )
    names = [line.name for line in lines]
    duplicate_names = sorted({n for n in names if names.count(n) > 1})
    if duplicate_names:
        raise ForecastFileError(f"forecast file: duplicate line name(s) {', '.join(duplicate_names)}")
    return ForecastFile(lines=lines, call=_parse_call(call, postable))


def load_forecast(path: pathlib.Path, chart: Chart) -> ForecastFile:
    """Read `forecast.toml` and validate it against the chart beside it."""
    return parse_forecast(path.read_text(encoding="utf-8"), postable=chart.postable)
