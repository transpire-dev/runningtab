"""`runningtab.period.status`: whether every closed quarter's stored package still matches the ledger, and whether
the lock itself is still intact.

`period_status` is pure -- `sums` and `rebuild` are fake callables here, over
the real fixture package `tests.conftest.period_inputs()` builds, never a
hand-built `Package`. The "changed"/"missing" cases tamper with that real
package's own file map rather than inventing one.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib

import pytest

from runningtab.health import Check, Outcome
from runningtab.period import PeriodError
from runningtab.period.lock import LockChange, LockState
from runningtab.period.package import Package, build_package
from runningtab.period.quarter import Quarter
from runningtab.period.record import StoredPackage
from runningtab.period.status import QuarterStatus, StatusReport, closed_periods_check, period_status
from tests.conftest import period_inputs

QUARTER = Quarter.parse("2026Q3")


def _package() -> Package:
    return build_package(period_inputs())


def _sums_of(package: Package) -> dict[str, str]:
    """The same {path: sha256 hex} shape `runningtab.period.record.read_sums` parses back out of a stored
    `SHA256SUMS` object -- computed straight from the package's own files here, since `sums` is a fake
    callable in these tests.
    """
    return {path: hashlib.sha256(data).hexdigest() for path, data in package.files.items()}


def _stored(revision: int = 1) -> StoredPackage:
    suffix = "" if revision == 1 else f"rev{revision}/"
    return StoredPackage(quarter=QUARTER, revision=revision, prefix=f"VT/{QUARTER.label}/{suffix}")


def _recorded(version: str | None = None):
    """A fake `lock_record`: the Company Version each stored package's manifest records.

    With no `version`, each quarter's manifest records `VER-<quarter>`, the
    Version `_closed_history` gives that quarter's close.
    """
    return lambda stored: version if version is not None else f"VER-{stored.quarter.label}"


def _closed_history(*labels: str) -> list[LockChange]:
    """The Versions a run of ordinary closes leaves: one forward move per quarter onto its end, newest first."""
    quarters = sorted(Quarter.parse(label) for label in labels)
    return [
        LockChange(
            version=f"VER-{q.label}",
            at=dt.datetime.combine(q.end + dt.timedelta(days=5), dt.time(9, 0)),
            by="gavin@carothers.name",
            old=q.previous.end,
            new=q.end,
            old_text=q.previous.end.isoformat(),
            new_text=q.end.isoformat(),
        )
        for q in reversed(quarters)
    ]


def _set_to(end: dt.date, version: str) -> LockChange:
    return LockChange(
        version=version,
        at=dt.datetime(2026, 10, 5, 9, 0),
        by="gavin@carothers.name",
        old=dt.date(2026, 6, 30),
        new=end,
        old_text="2026-06-30",
        new_text=end.isoformat(),
    )


def _never(quarter: Quarter) -> Package:
    raise AssertionError(f"rebuild should not have been called for {quarter.label}")


# --- nothing closed ---------------------------------------------------------


def test_nothing_closed_is_ok():
    report = period_status(
        lock=LockState(closed_through=None, bypass_role=None),
        history=[],
        stored_latest={},
        sums=lambda stored: {},
        rebuild=_never,
        lock_record=_recorded(),
    )

    assert report.ok
    assert report.quarters == []
    assert report.problems == []


# --- all ok ------------------------------------------------------------------


def test_a_stored_quarter_that_still_matches_is_ok():
    package = _package()
    stored = _stored()

    report = period_status(
        lock=LockState(closed_through=QUARTER.end, bypass_role=None),
        history=_closed_history("2026Q3"),
        stored_latest={QUARTER: stored},
        sums=lambda s: _sums_of(package),
        rebuild=lambda quarter: package,
        lock_record=_recorded(),
    )

    assert report.ok
    assert report.quarters == [QuarterStatus(quarter=QUARTER, revision=1, changed=[])]
    assert report.problems == []


# --- reopened ------------------------------------------------------------------


def test_a_reopened_quarter_names_who_moved_it_and_when():
    package = _package()
    stored = _stored()
    regression = LockChange(
        version="VER-9",
        at=dt.datetime(2026, 10, 12, 9, 30),
        by="alex@carothers.name",
        old=QUARTER.end,
        new=dt.date(2026, 6, 30),
        old_text=QUARTER.end.isoformat(),
        new_text="2026-06-30",
    )
    # A later, forward move that still leaves the lock short of the stored
    # quarter's end -- the newest entry overall, but not itself a regression,
    # so the reported change must be `regression`, not this one.
    partial_recovery = LockChange(
        version="VER-10",
        at=dt.datetime(2026, 10, 13, 9, 30),
        by="alex@carothers.name",
        old=dt.date(2026, 6, 30),
        new=dt.date(2026, 7, 31),
        old_text="2026-06-30",
        new_text="2026-07-31",
    )

    report = period_status(
        lock=LockState(closed_through=dt.date(2026, 7, 31), bypass_role=None),
        history=[partial_recovery, regression],
        stored_latest={QUARTER: stored},
        sums=lambda s: _sums_of(package),
        rebuild=lambda quarter: package,
        lock_record=_recorded(),
    )

    assert not report.ok
    named = "; ".join(report.problems)
    assert "VER-9" in named
    assert "alex@carothers.name" in named
    assert "2026-10-12" in named
    assert "2026-09-30" in named  # QUARTER.end, the old value
    assert "2026-06-30" in named  # the new value


def test_a_reopen_that_clears_the_lock_still_names_who_and_when():
    # The obvious way to "unlock" in the ERPNext UI is to empty the field
    # entirely, not type in an earlier date -- that Version's `new` is None,
    # not some earlier `dt.date`. A confident wrong answer ("no Company
    # Version records who moved it") is worse than naming the clearing.
    package = _package()
    stored = _stored()
    cleared = LockChange(
        version="VER-9",
        at=dt.datetime(2026, 10, 12, 9, 30),
        by="alex@carothers.name",
        old=QUARTER.end,
        new=None,
        old_text=QUARTER.end.isoformat(),
        new_text="",
    )

    report = period_status(
        lock=LockState(closed_through=None, bypass_role=None),
        history=[cleared],
        stored_latest={QUARTER: stored},
        sums=lambda s: _sums_of(package),
        rebuild=lambda quarter: package,
        lock_record=_recorded(),
    )

    assert not report.ok
    named = "; ".join(report.problems)
    assert "no Company Version records who moved it" not in named
    assert "VER-9" in named
    assert "alex@carothers.name" in named
    assert "2026-10-12" in named
    assert "2026-09-30" in named  # QUARTER.end, the old value


def test_a_reopen_with_no_version_evidence_is_still_reported():
    package = _package()
    stored = _stored()

    report = period_status(
        lock=LockState(closed_through=dt.date(2026, 6, 30), bypass_role=None),
        history=[],
        stored_latest={QUARTER: stored},
        sums=lambda s: _sums_of(package),
        rebuild=lambda quarter: package,
        lock_record=_recorded(),
    )

    assert not report.ok
    assert any("2026-06-30" in p for p in report.problems)


def _stored_quarters(*labels: str) -> dict[Quarter, StoredPackage]:
    quarters = [Quarter.parse(label) for label in labels]
    return {q: StoredPackage(quarter=q, revision=1, prefix=f"VT/{q.label}/") for q in quarters}


def test_a_reopened_earlier_quarter_names_every_quarter_to_reclose_in_order():
    # Q2 reopened (lock back at Q1's end) while Q3 is stored after it: the
    # true next step is re-closing Q2 and then Q3 -- `close` refuses Q3 first.
    package = _package()
    report = period_status(
        lock=LockState(closed_through=dt.date(2026, 3, 31), bypass_role=None),
        history=[],
        stored_latest=_stored_quarters("2026Q1", "2026Q2", "2026Q3"),
        sums=lambda s: _sums_of(package),
        rebuild=lambda quarter: package,
        lock_record=_recorded(),
    )

    named = "; ".join(report.problems)
    assert "re-close 2026Q2, then 2026Q3" in named
    assert "set Books Closed Through" not in named


def test_a_reopen_to_a_date_no_close_accepts_names_the_date_to_set_first():
    # The lock sits inside Q3, with Q2 stored: `close --quarter 2026Q3`
    # refuses until the lock is exactly Q2's end, so that comes first.
    package = _package()
    report = period_status(
        lock=LockState(closed_through=dt.date(2026, 8, 15), bypass_role=None),
        history=[],
        stored_latest=_stored_quarters("2026Q2", "2026Q3"),
        sums=lambda s: _sums_of(package),
        rebuild=lambda quarter: package,
        lock_record=_recorded(),
    )

    named = "; ".join(report.problems)
    assert "set Books Closed Through to 2026-06-30 in ERPNext, then re-close 2026Q3" in named


def test_a_cleared_lock_names_every_stored_quarter_to_reclose():
    package = _package()
    report = period_status(
        lock=LockState(closed_through=None, bypass_role=None),
        history=[],
        stored_latest=_stored_quarters("2026Q2", "2026Q3"),
        sums=lambda s: _sums_of(package),
        rebuild=lambda quarter: package,
        lock_record=_recorded(),
    )

    named = "; ".join(report.problems)
    assert "re-close 2026Q2, then 2026Q3" in named


# --- locked, but no package stored -------------------------------------------------


def test_a_lock_with_nothing_stored_names_the_quarter_to_close():
    # A close that locked and then failed to store (a rebuild mismatch, a
    # bucket 503, a killed process) leaves exactly this: the quarter frozen
    # in ERPNext and nothing in the record bucket.
    report = period_status(
        lock=LockState(closed_through=QUARTER.end, bypass_role=None),
        history=_closed_history("2026Q3"),
        stored_latest={},
        sums=lambda s: {},
        rebuild=_never,
        lock_record=_recorded(),
    )

    assert not report.ok
    assert report.problems == [
        "Books Closed Through is 2026-09-30 but 2026Q3 has no stored package; run "
        "`runningtab period close --quarter 2026Q3` to store it"
    ]


def test_a_lock_past_the_latest_stored_quarter_names_the_quarter_to_close():
    package = _package()
    earlier = Quarter.parse("2026Q2")
    report = period_status(
        lock=LockState(closed_through=QUARTER.end, bypass_role=None),
        history=_closed_history("2026Q3"),
        stored_latest={earlier: StoredPackage(quarter=earlier, revision=1, prefix=f"VT/{earlier.label}/")},
        sums=lambda s: _sums_of(package),
        rebuild=lambda quarter: package,
        lock_record=_recorded(),
    )

    assert not report.ok
    named = "; ".join(report.problems)
    assert "Books Closed Through is 2026-09-30 but 2026Q3 has no stored package" in named
    assert "`runningtab period close --quarter 2026Q3`" in named
    # Only the quarter the lock sits in, never every quarter before it.
    assert "2026Q1" not in named


def test_a_lock_inside_an_unstored_quarter_names_the_date_as_no_close_of_ours():
    earlier = Quarter.parse("2026Q2")
    package = _package()
    report = period_status(
        lock=LockState(closed_through=dt.date(2026, 8, 15), bypass_role=None),
        history=[],
        stored_latest={earlier: StoredPackage(quarter=earlier, revision=1, prefix=f"VT/{earlier.label}/")},
        sums=lambda s: _sums_of(package),
        rebuild=lambda quarter: package,
        lock_record=_recorded(),
    )

    assert not report.ok
    named = "; ".join(report.problems)
    assert "Books Closed Through is 2026-08-15, inside 2026Q3" in named
    assert "not a quarter's end" in named
    assert "`runningtab period close --quarter 2026Q3`" in named


def test_a_reopen_to_before_the_first_close_is_not_reported_as_an_unstored_quarter():
    # Q2 was the first close ever and is reopened back to Q1's end: Q1 was
    # never closed and can never be, so naming it would be advice that
    # cannot work. The reopen problem alone covers this.
    package = _package()
    report = period_status(
        lock=LockState(closed_through=dt.date(2026, 3, 31), bypass_role=None),
        history=[],
        stored_latest=_stored_quarters("2026Q2", "2026Q3"),
        sums=lambda s: _sums_of(package),
        rebuild=lambda quarter: package,
        lock_record=_recorded(),
    )

    named = "; ".join(report.problems)
    assert "has no stored package" not in named
    assert "re-close 2026Q2, then 2026Q3" in named


def test_the_health_check_fails_for_a_lock_with_nothing_stored():
    report = period_status(
        lock=LockState(closed_through=QUARTER.end, bypass_role=None),
        history=_closed_history("2026Q3"),
        stored_latest={},
        sums=lambda s: {},
        rebuild=_never,
        lock_record=_recorded(),
    )

    check = closed_periods_check(report)

    assert check.outcome is Outcome.FAIL
    assert "2026Q3 has no stored package" in " ".join(check.detail)


# --- bypass role ------------------------------------------------------------------


def test_a_bypass_role_is_reported():
    report = period_status(
        lock=LockState(closed_through=None, bypass_role="Accounts Manager"),
        history=[],
        stored_latest={},
        sums=lambda s: {},
        rebuild=_never,
        lock_record=_recorded(),
    )

    assert not report.ok
    assert any("Accounts Manager" in p for p in report.problems)


# --- one changed file --------------------------------------------------------


def test_one_changed_file_is_named():
    package = _package()
    stored = _stored()
    sums = _sums_of(package)
    tampered = sorted(sums)[0]
    sums[tampered] = "0" * 64

    report = period_status(
        lock=LockState(closed_through=QUARTER.end, bypass_role=None),
        history=_closed_history("2026Q3"),
        stored_latest={QUARTER: stored},
        sums=lambda s: sums,
        rebuild=lambda quarter: package,
        lock_record=_recorded(),
    )

    assert not report.ok
    assert report.quarters == [QuarterStatus(quarter=QUARTER, revision=1, changed=[tampered])]
    assert any(tampered in p for p in report.problems)


def test_bank_drift_in_an_earlier_quarter_names_the_reopen_and_every_reclose():
    # Bank activity dated in Q2 arrived after Q2 closed, so Q2's rebuilt
    # bank reconciliation differs. The way back to green is reopening Q2,
    # booking it there, and re-closing Q2 and then Q3 -- never booking it in
    # the open quarter, which would leave Q2 different for good.
    package = _package()
    earlier = Quarter.parse("2026Q2")
    drifted = _sums_of(package)
    drifted["bank-reconciliation.csv"] = "0" * 64

    report = period_status(
        lock=LockState(closed_through=QUARTER.end, bypass_role=None),
        history=_closed_history("2026Q2", "2026Q3"),
        stored_latest=_stored_quarters("2026Q2", "2026Q3"),
        sums=lambda s: drifted if s.quarter == earlier else _sums_of(package),
        rebuild=lambda quarter: package,
        lock_record=_recorded(),
    )

    (problem,) = report.problems
    assert problem.startswith("2026Q2 rev1: rebuilt files differ from the stored package: bank-reconciliation.csv")
    assert "reopen 2026Q2 in ERPNext (move Books Closed Through back to 2026-03-31)" in problem
    assert "book it there" in problem
    assert "re-close 2026Q2, then 2026Q3" in problem
    assert "open quarter" not in problem


def test_a_changed_file_other_than_the_bank_reconciliation_gets_no_bank_advice():
    package = _package()
    sums = _sums_of(package)
    sums["trial-balance.csv"] = "0" * 64

    report = period_status(
        lock=LockState(closed_through=QUARTER.end, bypass_role=None),
        history=_closed_history("2026Q3"),
        stored_latest={QUARTER: _stored()},
        sums=lambda s: sums,
        rebuild=lambda quarter: package,
        lock_record=_recorded(),
    )

    (problem,) = report.problems
    assert "bank activity" not in problem


# --- a missing file ------------------------------------------------------------


def test_a_file_missing_from_the_rebuild_is_named():
    package = _package()
    stored = _stored()
    sums = _sums_of(package)
    dropped = sorted(sums)[0]
    rebuilt = dataclasses.replace(package, files={p: d for p, d in package.files.items() if p != dropped})

    report = period_status(
        lock=LockState(closed_through=QUARTER.end, bypass_role=None),
        history=_closed_history("2026Q3"),
        stored_latest={QUARTER: stored},
        sums=lambda s: sums,
        rebuild=lambda quarter: rebuilt,
        lock_record=_recorded(),
    )

    assert not report.ok
    assert report.quarters == [QuarterStatus(quarter=QUARTER, revision=1, changed=[dropped])]


def test_a_file_extra_in_the_rebuild_is_named():
    package = _package()
    stored = _stored()
    sums = _sums_of(package)
    extra_path = "extra.csv"
    rebuilt = dataclasses.replace(package, files={**package.files, extra_path: b"x"})

    report = period_status(
        lock=LockState(closed_through=QUARTER.end, bypass_role=None),
        history=_closed_history("2026Q3"),
        stored_latest={QUARTER: stored},
        sums=lambda s: sums,
        rebuild=lambda quarter: rebuilt,
        lock_record=_recorded(),
    )

    assert not report.ok
    assert report.quarters == [QuarterStatus(quarter=QUARTER, revision=1, changed=[extra_path])]


# --- a rebuild refusal must not discard problems already found --------------


def test_a_rebuild_refusal_still_reports_the_bypass_role_already_found():
    # A likely trigger in the real CLI: `_refuse_chart_drift` raises a
    # `PeriodError` from inside `rebuild`. Whatever the exception, a person
    # who fixes it should not discover only then that a bypass role was also
    # set -- both problems must survive in one report.
    package = _package()
    stored = _stored()

    def _boom(quarter: Quarter) -> Package:
        raise PeriodError("chart drift: 5299 needs a create")

    report = period_status(
        lock=LockState(closed_through=QUARTER.end, bypass_role="Accounts Manager"),
        history=[],
        stored_latest={QUARTER: stored},
        sums=lambda s: _sums_of(package),
        rebuild=_boom,
        lock_record=_recorded(),
    )

    assert not report.ok
    named = "; ".join(report.problems)
    assert "Accounts Manager" in named
    assert "chart drift" in named
    assert QUARTER.label in named


def test_a_rebuild_refusal_on_one_quarter_does_not_stop_the_others():
    earlier = Quarter.parse("2026Q2")
    package = _package()
    sums = _sums_of(package)
    stored_latest = {
        QUARTER: _stored(),
        earlier: StoredPackage(quarter=earlier, revision=1, prefix=f"VT/{earlier.label}/"),
    }

    def rebuild(quarter: Quarter) -> Package:
        if quarter == earlier:
            raise PeriodError(f"{earlier.label}: could not read the ledger")
        return package

    report = period_status(
        lock=LockState(closed_through=QUARTER.end, bypass_role=None),
        history=_closed_history("2026Q3"),
        stored_latest=stored_latest,
        sums=lambda s: sums,
        rebuild=rebuild,
        lock_record=_recorded(),
    )

    assert not report.ok
    named = "; ".join(report.problems)
    assert "could not read the ledger" in named
    # The quarter that rebuilt fine still gets its ordinary "ok" status.
    assert QuarterStatus(quarter=QUARTER, revision=1, changed=[]) in report.quarters


# --- multiple quarters, sorted -----------------------------------------------


def test_every_stored_quarter_is_checked_in_order():
    earlier = Quarter.parse("2026Q2")
    package = _package()
    sums = _sums_of(package)
    stored_latest = {
        QUARTER: _stored(),
        earlier: StoredPackage(quarter=earlier, revision=1, prefix=f"VT/{earlier.label}/"),
    }
    seen: list[Quarter] = []

    def rebuild(quarter: Quarter) -> Package:
        seen.append(quarter)
        return package

    report = period_status(
        lock=LockState(closed_through=QUARTER.end, bypass_role=None),
        history=_closed_history("2026Q3"),
        stored_latest=stored_latest,
        sums=lambda s: sums,
        rebuild=rebuild,
        lock_record=_recorded(),
    )

    assert [q.quarter for q in report.quarters] == [earlier, QUARTER]
    assert seen == [earlier, QUARTER]


# --- closed_periods_check -----------------------------------------------------


def test_the_check_passes_and_says_nothing_closed_yet():
    report = StatusReport(lock=LockState(None, None), history=[], quarters=[], problems=[])

    check = closed_periods_check(report)

    assert isinstance(check, Check)
    assert check.key == "closed-periods"
    assert check.outcome is Outcome.PASS
    assert "nothing closed yet" in check.summary


def test_the_check_passes_when_every_stored_quarter_matches():
    report = StatusReport(
        lock=LockState(QUARTER.end, None),
        history=[],
        quarters=[QuarterStatus(quarter=QUARTER, revision=1, changed=[])],
        problems=[],
    )

    check = closed_periods_check(report)

    assert check.outcome is Outcome.PASS
    assert "1" in check.summary


def test_the_check_fails_and_names_every_problem():
    report = StatusReport(
        lock=LockState(dt.date(2026, 6, 30), None),
        history=[],
        quarters=[QuarterStatus(quarter=QUARTER, revision=1, changed=["trial-balance.csv"])],
        problems=["problem one", "problem two"],
    )

    check = closed_periods_check(report)

    assert check.outcome is Outcome.FAIL
    detail = " ".join(check.detail)
    assert "problem one" in detail
    assert "problem two" in detail


@pytest.mark.parametrize("field", ["quarter", "revision", "changed"])
def test_quarter_status_is_a_plain_dataclass(field):
    # Smoke test that the fields the brief names actually exist, with those
    # exact names.
    status = QuarterStatus(quarter=QUARTER, revision=1, changed=[])
    assert hasattr(status, field)


# --- an unfinished re-close ------------------------------------------------------


def test_a_lock_set_by_the_close_that_stored_the_quarter_is_ok():
    package = _package()

    report = period_status(
        lock=LockState(closed_through=QUARTER.end, bypass_role=None),
        history=[_set_to(QUARTER.end, "VER-1")],
        stored_latest={QUARTER: _stored()},
        sums=lambda s: _sums_of(package),
        rebuild=lambda quarter: package,
        lock_record=_recorded("VER-1"),
    )

    assert report.ok, report.problems


def test_a_reclose_that_locked_and_stored_nothing_is_reported_though_every_file_matches():
    # 2026Q3 rev1 was closed by VER-1. It was reopened, and a re-close moved
    # the lock back to Q3's end (VER-7) but its upload failed. The ledger
    # still matches rev1, so no file differs -- only the lock record says
    # that the newest close of 2026Q3 is not in the record.
    package = _package()
    reopen = LockChange(
        version="VER-6",
        at=dt.datetime(2026, 10, 4, 9, 0),
        by="gavin@carothers.name",
        old=QUARTER.end,
        new=dt.date(2026, 6, 30),
        old_text=QUARTER.end.isoformat(),
        new_text="2026-06-30",
    )

    report = period_status(
        lock=LockState(closed_through=QUARTER.end, bypass_role=None),
        history=[_set_to(QUARTER.end, "VER-7"), reopen, _set_to(QUARTER.end, "VER-1")],
        stored_latest={QUARTER: _stored()},
        sums=lambda s: _sums_of(package),
        rebuild=lambda quarter: package,
        lock_record=_recorded("VER-1"),
    )

    assert not report.ok
    assert report.quarters == [QuarterStatus(quarter=QUARTER, revision=1, changed=[])]
    (problem,) = report.problems
    assert "Company Version VER-7" in problem
    assert "2026Q3 rev1 records Company Version VER-1" in problem
    assert "stored no new revision" in problem
    assert "reopen 2026Q3 (move Books Closed Through back to 2026-06-30 in ERPNext)" in problem


def test_an_unreadable_lock_record_is_named_not_skipped():
    package = _package()

    def _unreadable(stored: StoredPackage) -> str:
        raise PeriodError("VT/2026Q3/MANIFEST.md records no lock (no Company Version line)")

    report = period_status(
        lock=LockState(closed_through=QUARTER.end, bypass_role=None),
        history=[_set_to(QUARTER.end, "VER-1")],
        stored_latest={QUARTER: _stored()},
        sums=lambda s: _sums_of(package),
        rebuild=lambda quarter: package,
        lock_record=_unreadable,
    )

    assert not report.ok
    assert report.problems == [
        "2026Q3 rev1: could not read which lock it records: VT/2026Q3/MANIFEST.md records no lock "
        "(no Company Version line)"
    ]


def test_a_reopen_back_onto_an_earlier_quarter_end_is_not_taken_for_that_quarters_close():
    # Q2 and Q3 closed normally; Q3 was reopened, which moves the lock back
    # onto Q2's end. That reopen Version has new == Q2.end but closed nothing,
    # so Q2 must not be reported as an unfinished re-close -- only Q3's
    # reopen is a problem.
    package = _package()
    q2 = Quarter.parse("2026Q2")
    reopen = LockChange(
        version="VER-reopen",
        at=dt.datetime(2026, 10, 20, 9, 0),
        by="gavin@carothers.name",
        old=QUARTER.end,
        new=q2.end,
        old_text=QUARTER.end.isoformat(),
        new_text=q2.end.isoformat(),
    )

    report = period_status(
        lock=LockState(closed_through=q2.end, bypass_role=None),
        history=[reopen, *_closed_history("2026Q2", "2026Q3")],
        stored_latest=_stored_quarters("2026Q2", "2026Q3"),
        sums=lambda s: _sums_of(package),
        rebuild=lambda quarter: package,
        lock_record=_recorded(),
    )

    assert not report.ok  # Q3 is reopened
    assert not any("stored no new revision" in p for p in report.problems)
    assert not any(p.startswith("2026Q2") for p in report.problems)


def test_an_earlier_quarters_unfinished_reclose_is_found_after_a_later_quarter_closes():
    # Q2 was re-closed (VER-Q2b locked it) but its store failed; Q3 was then
    # re-closed and stored normally. The lock sits on Q3, and every file of
    # Q2 rev1 still matches -- only Q2's lock record shows the gap.
    package = _package()
    q2 = Quarter.parse("2026Q2")
    reclose_q2 = LockChange(
        version="VER-Q2b",
        at=dt.datetime(2026, 10, 21, 9, 0),
        by="gavin@carothers.name",
        old=Quarter.parse("2026Q1").end,
        new=q2.end,
        old_text="2026-03-31",
        new_text=q2.end.isoformat(),
    )
    reclose_q3 = dataclasses.replace(_closed_history("2026Q3")[0], version="VER-Q3b", at=dt.datetime(2026, 10, 22))

    report = period_status(
        lock=LockState(closed_through=QUARTER.end, bypass_role=None),
        history=[reclose_q3, reclose_q2, *_closed_history("2026Q2", "2026Q3")],
        stored_latest=_stored_quarters("2026Q2", "2026Q3"),
        sums=lambda s: _sums_of(package),
        rebuild=lambda quarter: package,
        lock_record=lambda stored: "VER-Q3b" if stored.quarter == QUARTER else "VER-2026Q2",
    )

    (problem,) = report.problems
    assert "Company Version VER-Q2b" in problem
    assert "2026Q2 rev1 records Company Version VER-2026Q2" in problem
    assert "stored no new revision" in problem


def test_a_stored_close_no_version_confirms_is_reported_not_passed():
    # Every close writes a forward Version onto its quarter's end. A stored
    # quarter with none cannot be confirmed as closed by anything, and a pass
    # would be a confident answer resting on nothing.
    package = _package()

    report = period_status(
        lock=LockState(closed_through=QUARTER.end, bypass_role=None),
        history=[],
        stored_latest={QUARTER: _stored()},
        sums=lambda s: _sums_of(package),
        rebuild=lambda quarter: package,
        lock_record=_recorded(),
    )

    (problem,) = report.problems
    assert problem.startswith("2026Q3 rev1 records Company Version VER-2026Q3, but no Company Version moves")
    assert "cannot be confirmed" in problem
