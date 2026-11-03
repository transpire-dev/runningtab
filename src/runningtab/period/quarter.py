"""Calendar quarters, and the ERPNext Fiscal Year each must sit inside."""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from collections.abc import Sequence

from runningtab.period import PeriodError

_QUARTER = re.compile(r"(?P<year>\d{4})Q(?P<number>[1-4])")


@dataclasses.dataclass(frozen=True, order=True)
class Quarter:
    """One calendar quarter, named the way the command line takes it: `2026Q3`."""

    year: int
    number: int

    @classmethod
    def parse(cls, text: str) -> Quarter:
        # `\d{4}` matches "0000"; `dt.date`'s minimum year is 1, so "0000Q1"
        # parsed clean and only failed later, with a bare ValueError, the
        # first time something touched `.start` or `.end`.
        match = _QUARTER.fullmatch(text)
        if match is None or int(match["year"]) < 1:
            raise PeriodError(f"{text!r} is not a quarter; write it as YYYYQn, e.g. 2026Q3")
        return cls(int(match["year"]), int(match["number"]))

    @classmethod
    def containing(cls, date: dt.date) -> Quarter:
        """The quarter `date` falls in."""
        return cls(date.year, (date.month - 1) // 3 + 1)

    @property
    def previous(self) -> Quarter:
        """The quarter just before this one."""
        if self.number == 1:
            return Quarter(self.year - 1, 4)
        return Quarter(self.year, self.number - 1)

    @property
    def label(self) -> str:
        return f"{self.year}Q{self.number}"

    @property
    def start(self) -> dt.date:
        return dt.date(self.year, 3 * self.number - 2, 1)

    @property
    def end(self) -> dt.date:
        if self.number == 4:
            return dt.date(self.year, 12, 31)
        return dt.date(self.year, 3 * self.number + 1, 1) - dt.timedelta(days=1)

    def has_ended(self, today: dt.date) -> bool:
        return today > self.end


@dataclasses.dataclass(frozen=True)
class FiscalYear:
    """An ERPNext Fiscal Year: P&L accounts reset at its start."""

    name: str
    start: dt.date
    end: dt.date


def fiscal_year_for(quarter: Quarter, years: Sequence[FiscalYear]) -> FiscalYear:
    """The one Fiscal Year that wholly contains `quarter`.

    Refuses a quarter no year holds, one split across two, and more than one
    candidate -- the opening balance of every P&L account depends on the
    answer, so it is never guessed.
    """
    holding = [y for y in years if y.start <= quarter.start and quarter.end <= y.end]
    if len(holding) == 1:
        return holding[0]
    if len(holding) > 1:
        names = ", ".join(sorted(y.name for y in holding))
        raise PeriodError(f"{quarter.label} sits in more than one ERPNext Fiscal Year: {names}")
    touching = [y for y in years if y.start <= quarter.end and quarter.start <= y.end]
    if touching:
        raise PeriodError(
            f"{quarter.label} does not fall within a single Fiscal Year; it spans "
            f"{', '.join(sorted(y.name for y in touching))}"
        )
    raise PeriodError(f"{quarter.label}: no ERPNext Fiscal Year covers it; create one first")
