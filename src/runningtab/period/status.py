"""`runningtab period status`: whether every closed quarter's stored package still matches the ledger, and whether
the lock itself is still intact.

There are three ways a closed quarter can go bad, and this is the one
place that looks for all three: Books Closed Through moved backward without a
matching close (a reopen, done directly in the ERPNext UI -- there is no
`period reopen`); `role_allowed_for_frozen_entries` left set, which would let
someone post into a period nominally still closed; and a stored package whose
files no longer match what the ledger now says, because something in ERPNext
changed after the close (a correcting Journal Entry against a document a
closed quarter's trial balance already used, say). It also reports the
spec's "Failure behaviour" case: Books Closed Through moved past every
stored quarter, so a quarter is locked with no final package.

`period_status` itself is pure -- everything it needs from ERPNext and the
record bucket arrives as already-read values (`lock`, `history`,
`stored_latest`) or as callables (`sums`, `rebuild`) the caller closes over
its own credential and bucket with. `runningtab.cli` is the one place that reads
the reader's credential and the record bucket for real; the CLI's `rebuild`
closure runs each stored quarter through the exact same read-and-build path
`period export` uses, so a mismatch this reports is never an artefact of a
different code path disagreeing with itself.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
from collections.abc import Callable, Iterable

from runningtab import health
from runningtab.period.close import reclose_order, reclose_steps, reopen_step
from runningtab.period.lock import LockChange, LockState
from runningtab.period.package import Package
from runningtab.period.quarter import Quarter
from runningtab.period.record import StoredPackage


@dataclasses.dataclass(frozen=True)
class QuarterStatus:
    """One stored quarter, and every path (if any) where the rebuild disagrees with what is stored."""

    quarter: Quarter
    revision: int
    changed: list[str]


@dataclasses.dataclass(frozen=True)
class StatusReport:
    """Books Closed Through, its history, every stored quarter's status, and every problem found by name."""

    lock: LockState
    history: list[LockChange]
    quarters: list[QuarterStatus]
    problems: list[str]

    @property
    def ok(self) -> bool:
        """Whether anything named in `problems` needs a person -- an empty `quarters` list is not itself a problem."""
        return not self.problems


def _newest_regression(history: list[LockChange]) -> LockChange | None:
    """The newest Version (in `history`'s newest-first order) that moved `accounts_frozen_till_date` backward.

    Not simply `history[0]`: a reopen can be followed by a partial, still
    incomplete recovery -- a later Version that moves the lock forward again
    but not yet back to the latest stored quarter's end. That later Version
    is not itself a regression, and reporting it would name the wrong edit as
    "who reopened this". Walking newest-first and returning the first actual
    decrease finds the regression whichever Version is newest overall.

    A regression that *clears* the field entirely (`new` is None) counts too
    -- the obvious way to "unlock" in the ERPNext UI is to empty
    `accounts_frozen_till_date`, not type in an earlier date. Treating that
    as "no regression found" (since `None < old` can't be compared) would
    have `_reopen_problem` fall back to "no Company Version records who
    moved it" even though this Version is exactly that record.
    """
    for change in history:
        if change.old is None:
            continue
        if change.new is None or change.new < change.old:
            return change
    return None


def _reopen_problem(lock: LockState, history: list[LockChange], latest_end: dt.date) -> str:
    shown = lock.closed_through.isoformat() if lock.closed_through else "unset"
    change = _newest_regression(history)
    if change is None:
        return (
            f"Books Closed Through is {shown}, earlier than the latest stored quarter's end "
            f"({latest_end.isoformat()}) -- a reopen, but no Company Version records who moved it"
        )
    old = change.old.isoformat() if change.old else "unset"
    new = change.new.isoformat() if change.new else "cleared"
    return (
        f"Books Closed Through is {shown}, earlier than the latest stored quarter's end "
        f"({latest_end.isoformat()}) -- reopened by {change.by} on {change.at.isoformat()} "
        f"(Company Version {change.version} moved it {old} -> {new})"
    )


def _reclose_next_step(lock: LockState, stored: Iterable[Quarter]) -> str:
    """What gets the books closed again after a reopen: the quarters to re-close, in order, and the lock date
    `close` needs first when the lock sits somewhere no re-close accepts.
    """
    quarters = set(stored)
    order = reclose_order(lock.closed_through, quarters)
    previous = order[0].previous
    if previous in quarters and lock.closed_through != previous.end:
        return (
            f"to close them again, set Books Closed Through to {previous.end.isoformat()} in ERPNext, then "
            f"{reclose_steps(order)}"
        )
    return f"to close them again, {reclose_steps(order)}"


def _unstored_lock_problem(closed_through: dt.date) -> str:
    """Books Closed Through sits in a quarter with no stored package: frozen in ERPNext, absent from the record.

    What a close that locked and then failed to store leaves behind -- a
    rebuild mismatch, a bucket error, a killed process. Re-running `close`
    for that quarter is its recovery. A date that is not a quarter's end
    was never set by `close` at all, so it is named as such.
    """
    quarter = Quarter.containing(closed_through)
    command = f"`runningtab period close --quarter {quarter.label}`"
    if closed_through == quarter.end:
        return (
            f"Books Closed Through is {closed_through.isoformat()} but {quarter.label} has no stored package; "
            f"run {command} to store it"
        )
    return (
        f"Books Closed Through is {closed_through.isoformat()}, inside {quarter.label}, which has no stored "
        "package -- not a quarter's end, so no close set it; find who did in the Company's Version history, "
        f"then run {command}"
    )


_BANK_RECONCILIATION = "bank-reconciliation.csv"


def _late_bank_activity_next_step(quarter: Quarter, stored: Iterable[Quarter]) -> str:
    """What clears a closed quarter whose bank reconciliation moved: reopen it, book the row there, re-close.

    Usually bank activity dated in `quarter` that reached Mercury after the
    close (the ingest names each such row). Booking it in the open quarter
    instead would leave this quarter's rebuild different from its stored
    package for good. Re-closing writes a new revision, and every stored
    quarter after it is re-closed after it, in order.
    """
    order = [quarter, *sorted(q for q in stored if q > quarter)]
    return (
        f"if bank activity dated in {quarter.label} arrived after it closed, reopen {quarter.label} in ERPNext "
        f"(move Books Closed Through back to {quarter.previous.end.isoformat()}), book it there, then "
        f"{reclose_steps(order)}"
    )


def _unfinished_reclose_problem(quarter: Quarter, stored: StoredPackage, setter: LockChange, recorded: str) -> str:
    """Books Closed Through is at `quarter`'s end, but a newer Version put it there than the one its stored close
    records: a re-close locked the quarter and never stored its revision.

    The earlier revision can still match the ledger, so no file differs --
    this is the only sign that the lock and the record now disagree about
    which close froze the quarter.
    """
    return (
        f"Books Closed Through was set to {quarter.end.isoformat()} by Company Version {setter.version} "
        f"({setter.by}, {setter.at.isoformat()}), but {quarter.label} rev{stored.revision} records Company Version "
        f"{recorded}: a re-close locked {quarter.label} and stored no new revision; {reopen_step(quarter)} and "
        f"run `runningtab period close --quarter {quarter.label}` again"
    )


def _closing_change(history: list[LockChange], end: dt.date) -> LockChange | None:
    """The newest Version that moved Books Closed Through *forward* onto `end` -- the one that closed that quarter.

    Only forward moves count. A reopen that moves the lock back onto an
    earlier quarter's end (reopening 2026Q3 lands on 2026Q2's end) also sets
    `new == end`, but it closed nothing, and taking it for 2026Q2's close
    would report a re-close of 2026Q2 that never happened.
    """
    return next((change for change in history if change.new == end and (change.old is None or change.old < end)), None)


def _lock_record_problems(
    lock: LockState,
    history: list[LockChange],
    stored_latest: dict[Quarter, StoredPackage],
    lock_record: Callable[[StoredPackage], str],
) -> list[str]:
    """For every stored quarter Books Closed Through covers: does its latest revision record the close that
    actually froze it?

    Every covered quarter, not only the one the lock ends in: a re-close of
    2026Q2 that locked and failed to store, followed by a re-close of
    2026Q3, leaves the lock on 2026Q3 and 2026Q2 still on its old revision.
    The stored manifest is read either way, and a covered quarter with no
    forward Version onto its end is reported as unconfirmed -- every close
    writes one, so its absence is itself something a person must look at,
    not a pass.
    """
    if lock.closed_through is None:
        return []
    problems: list[str] = []
    for quarter in sorted(q for q in stored_latest if q.end <= lock.closed_through):
        stored = stored_latest[quarter]
        try:
            recorded = lock_record(stored)
        except Exception as exc:
            problems.append(f"{quarter.label} rev{stored.revision}: could not read which lock it records: {exc}")
            continue
        setter = _closing_change(history, quarter.end)
        if setter is None:
            problems.append(
                f"{quarter.label} rev{stored.revision} records Company Version {recorded}, but no Company Version "
                f"moves Books Closed Through forward to {quarter.end.isoformat()}, so its close cannot be "
                "confirmed; check the Company's Version history in ERPNext"
            )
        elif setter.version != recorded:
            problems.append(_unfinished_reclose_problem(quarter, stored, setter, recorded))
    return problems


def _file_hashes(package: Package) -> dict[str, str]:
    return {path: hashlib.sha256(data).hexdigest() for path, data in package.files.items()}


def period_status(
    lock: LockState,
    history: list[LockChange],
    stored_latest: dict[Quarter, StoredPackage],
    sums: Callable[[StoredPackage], dict[str, str]],
    rebuild: Callable[[Quarter], Package],
    lock_record: Callable[[StoredPackage], str],
) -> StatusReport:
    """Check the lock and every stored quarter's latest revision, and report every problem found by name.

    `rebuild` is never called for a quarter this function has no stored
    package for, and nothing here writes anything anywhere. An unstored
    quarter matters here only when Books Closed Through sits in it -- the
    quarter is frozen with nothing in the record -- and then only that one
    quarter is named, never every quarter before the first close.
    """
    problems: list[str] = []

    if lock.bypass_role:
        problems.append(f"role_allowed_for_frozen_entries is {lock.bypass_role!r}")

    latest_end = max((stored.quarter.end for stored in stored_latest.values()), default=None)
    if lock.closed_through is not None and (latest_end is None or lock.closed_through > latest_end):
        problems.append(_unstored_lock_problem(lock.closed_through))
    if latest_end is not None and (lock.closed_through is None or lock.closed_through < latest_end):
        problems.append(f"{_reopen_problem(lock, history, latest_end)}; {_reclose_next_step(lock, stored_latest)}")
    problems.extend(_lock_record_problems(lock, history, stored_latest, lock_record))

    quarters: list[QuarterStatus] = []
    for quarter in sorted(stored_latest):
        stored = stored_latest[quarter]
        try:
            stored_sums = sums(stored)
            rebuilt_sums = _file_hashes(rebuild(quarter))
        except Exception as exc:
            # `sums`/`rebuild` are the caller's read of the outside world
            # (the record bucket, ERPNext, the chart) -- a refusal there
            # (chart drift between a `chart.coa` merge and its apply, an
            # ERPNext read that failed) must not discard the bypass-role and
            # reopen problems already found above, nor stop the remaining
            # quarters from being checked. Named here rather than silently
            # skipped: the exit code already goes non-zero either way, but a
            # person reading only the top line ("refused: chart drift...")
            # would otherwise never learn the bypass role was also set.
            problems.append(f"{quarter.label} rev{stored.revision}: could not verify: {exc}")
            continue
        changed = sorted(
            path for path in {*stored_sums, *rebuilt_sums} if stored_sums.get(path) != rebuilt_sums.get(path)
        )
        quarters.append(QuarterStatus(quarter=quarter, revision=stored.revision, changed=changed))
        if changed:
            problem = (
                f"{quarter.label} rev{stored.revision}: rebuilt files differ from the stored package: "
                + ", ".join(changed)
            )
            if _BANK_RECONCILIATION in changed:
                problem += f"; {_late_bank_activity_next_step(quarter, stored_latest)}"
            problems.append(problem)

    return StatusReport(lock=lock, history=history, quarters=quarters, problems=problems)


def closed_periods_check(report: StatusReport) -> health.Check:
    """`runningtab health`'s sixth section: the same report `period status` prints, as one named `Check`."""
    key, title = "closed-periods", "closed periods"
    if not report.problems:
        summary = (
            "nothing closed yet" if not report.quarters else f"{len(report.quarters)} quarter(s) closed, all match"
        )
        return health.Check(key=key, title=title, outcome=health.Outcome.PASS, summary=summary)
    detail: list[str] = []
    for problem in report.problems:
        detail.extend(health.wrapped(problem))
    return health.Check(
        key=key,
        title=title,
        outcome=health.Outcome.FAIL,
        summary=f"{len(report.problems)} problem(s)",
        detail=tuple(detail),
    )
