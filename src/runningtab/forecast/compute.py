"""The capital call: how much each member must put in over the next three months.

Pure arithmetic over the forecast file, the bank's opening balance and this
month's Mercury transactions. Nothing here reads a clock or a network, so the
same inputs always produce the same answer.

The answer is money people will pay, so every choice errs toward asking for
enough: a line costs its `high` amount, a charge not yet seen in the bank is
still owed, and each member's share rounds up to the cent.
"""

from __future__ import annotations

import calendar
import dataclasses
import datetime as dt
from collections.abc import Iterable
from decimal import ROUND_CEILING, Decimal
from typing import Literal

from runningtab.forecast.model import ForecastFile, Line
from runningtab.mercury.models import Transaction

SCENARIO = "conservative"
FIXED_FLOOR_SOURCE = "[call] floor"
NO_FLOOR_SOURCE = "no owed charges"
_CENT = Decimal("0.01")
_FULL_MONTHS_AHEAD = 3
# A charge settles a due only within this band around the line's range. Usage
# bills drift a little from the estimate, but a charge far outside it is a
# different bill from the same vendor, and counting it as this one would leave
# the real bill uncounted and the call understated.
_SETTLE_BELOW = Decimal("0.9")
_SETTLE_ABOVE = Decimal("1.1")


@dataclasses.dataclass(frozen=True)
class Due:
    """One occurrence of a line, at the amount we plan for (its `high`)."""

    line: Line
    due: dt.date
    amount: Decimal
    status: Literal["owed", "seen paid"]


@dataclasses.dataclass(frozen=True)
class Month:
    """One month of the cash path. `month` is its first day."""

    month: dt.date
    opening: Decimal
    outflows: Decimal
    inflows: Decimal
    closing: Decimal


@dataclasses.dataclass(frozen=True)
class Forecast:
    """The whole answer, with what it was computed from."""

    computed_at: dt.datetime
    ledger_through: dt.date
    window_start: dt.date
    window_end: dt.date
    scenario: str
    opening_cash: Decimal
    floor: Decimal
    floor_from: str
    months: tuple[Month, ...]
    dues: tuple[Due, ...]
    members: tuple[tuple[str, Decimal], ...]
    total_call: Decimal


def _month_end(year: int, month: int) -> dt.date:
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def _add_months(first: dt.date, n: int) -> dt.date:
    index = first.year * 12 + first.month - 1 + n
    return dt.date(index // 12, index % 12 + 1, 1)


def window(today: dt.date) -> tuple[dt.date, dt.date]:
    """From today through the end of the third full month after this one."""
    last = _add_months(today.replace(day=1), _FULL_MONTHS_AHEAD)
    return today, _month_end(last.year, last.month)


def _occurrences(line: Line, first: dt.date, end: dt.date) -> Iterable[dt.date]:
    """Every date the line falls due between the first of the current month and `end`."""
    if line.cadence == "monthly":
        if line.day is None:
            raise ValueError(f"monthly line {line.name!r} has no day")
        month = first
        while month <= end:
            # Day 31 falls on the last day of a shorter month.
            day = min(line.day, calendar.monthrange(month.year, month.month)[1])
            yield month.replace(day=day)
            month = _add_months(month, 1)
    elif line.cadence == "yearly":
        if line.on is None:
            raise ValueError(f"yearly line {line.name!r} has no date")
        for year in range(first.year, end.year + 1):
            yield line.on.replace(year=year)
    else:
        raise ValueError(f"line {line.name!r} has unknown cadence {line.cadence!r}")


def _payments(transactions: Iterable[Transaction], today: dt.date) -> list[Transaction]:
    """Charges we paid this month, up to today, in money the opening balance already reflects."""
    return [
        t
        for t in transactions
        if t.counterparty_name
        and t.amount < 0
        and t.counts_toward_balance
        and (t.accounting_date.year, t.accounting_date.month) == (today.year, today.month)
        and t.accounting_date <= today
    ]


def _dues(file: ForecastFile, today: dt.date, end: dt.date, payments: list[Transaction]) -> tuple[Due, ...]:
    """Every due in the window, each marked `seen paid` only if a payment of its own settles it.

    One bank charge pays at most one due: two lines can share a counterparty
    ("Google"), and a single charge must not mark both paid. Dues are settled
    by pairing every eligible payment with the due it is nearest in price to
    (earlier due date, then file order, break ties); each payment and each due
    is used once. A payment is eligible for a due when its counterparty matches
    and its amount is within the tolerance band of the line's range. A due left
    without a payment stays owed.
    """
    first = today.replace(day=1)
    found: list[tuple[dt.date, int, Line]] = []
    for index, line in enumerate(file.lines):
        for due in _occurrences(line, first, end):
            if not first <= due <= end:
                continue
            if (line.starts and due < line.starts) or (line.ends and due > line.ends):
                continue
            found.append((due, index, line))
    found.sort(key=lambda f: (f[0], f[1]))

    # Every way a payment could settle a due, nearest-priced first.
    pairs = sorted(
        (abs(abs(t.amount) - line.amount.high), due, index, p, f)
        for f, (due, index, line) in enumerate(found)
        if due < today
        for p, t in enumerate(payments)
        if (t.counterparty_name or "").casefold() in {m.casefold() for m in line.match}
        and line.amount.low * _SETTLE_BELOW <= abs(t.amount) <= line.amount.high * _SETTLE_ABOVE
    )
    settled: set[int] = set()
    used: set[int] = set()
    for _, _, _, p, f in pairs:
        if p not in used and f not in settled:
            used.add(p)
            settled.add(f)

    dues = [
        Due(line, due, line.amount.high, "seen paid" if f in settled else "owed")
        for f, (due, _, line) in enumerate(found)
    ]
    return tuple(sorted(dues, key=lambda d: (d.due, d.line.name)))


def compute(
    file: ForecastFile,
    *,
    now: dt.datetime,
    opening_cash: Decimal,
    ledger_through: dt.date,
    transactions: Iterable[Transaction],
) -> Forecast:
    """The capital each member must contribute to keep cash at or above the floor.

    The floor is the largest single charge, so a one-off bill is always
    coverable. The call is whatever lifts the cash path's lowest closing
    balance to the floor, `max(0, floor - lowest closing)`, split evenly and
    rounded up per member.
    """
    today = now.date()
    start, end = window(today)
    dues = _dues(file, today, end, _payments(transactions, today))
    owed = [d for d in dues if d.status == "owed"]

    months: list[Month] = []
    opening = opening_cash
    month = start.replace(day=1)
    while month <= end:
        outflows = sum((d.amount for d in owed if (d.due.year, d.due.month) == (month.year, month.month)), Decimal(0))
        inflows = Decimal(0)
        closing = opening + inflows - outflows
        months.append(Month(month, opening, outflows, inflows, closing))
        opening = closing
        month = _add_months(month, 1)

    if file.call.floor is not None:
        floor, floor_from = file.call.floor, FIXED_FLOOR_SOURCE
    elif owed:
        biggest = max(owed, key=lambda d: d.amount)
        floor, floor_from = biggest.amount, biggest.line.name
    else:
        floor, floor_from = Decimal(0), NO_FLOOR_SOURCE

    call = max(Decimal(0), floor - min(m.closing for m in months))
    share = (call / len(file.call.members)).quantize(_CENT, rounding=ROUND_CEILING)
    members = tuple((m, share) for m in file.call.members)
    return Forecast(
        computed_at=now,
        ledger_through=ledger_through,
        window_start=start,
        window_end=end,
        scenario=SCENARIO,
        opening_cash=opening_cash,
        floor=floor,
        floor_from=floor_from,
        months=tuple(months),
        dues=dues,
        members=members,
        total_call=sum((s for _, s in members), Decimal(0)),
    )
