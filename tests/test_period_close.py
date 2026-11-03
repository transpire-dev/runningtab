"""The close ceremony's exact order, against a fake `CloseSteps` -- no site, no bucket.

`_built()` builds a real `Built` from the same captured fixtures every other
period test uses (`tests.conftest.period_inputs`), the same way
`test_period_package.py` builds a real `Package` -- never a hand-built
`Package`/`TrialBalance` that would agree with a test by construction.
`_FakeSteps` is the fake `CloseSteps`: every callable records its name in
`self.calls`, so a test can assert both *that* a step ran and, via
`.calls`, in what order -- and that a step it must not run never appears at
all. Its `upload` runs the real `next_prefix`/`upload_package`
(`runningtab.period.record`) against an in-memory `FakeRecord`
(`tests.conftest`), rather than returning whatever prefix a test told it to
-- a fake that just parrots back its own input proves nothing about
whether the prefix `close_quarter` reports and the revision its stored
manifest names actually agree.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import re
from collections.abc import Callable, Sequence

import pytest

from runningtab.period import PeriodError
from runningtab.period.balances import TrialBalance, trial_balance
from runningtab.period.close import Built, CloseOutcome, CloseSteps, Held, HeldForReview, close_quarter
from runningtab.period.ledger import GLRow, index_accounts
from runningtab.period.lock import LockChange, LockState
from runningtab.period.manifest import ManifestFacts
from runningtab.period.open_items import OpenItem, open_items
from runningtab.period.package import Package, build_package, sha256sums
from runningtab.period.quarter import FiscalYear, Quarter
from runningtab.period.readiness import Blocker
from runningtab.period.record import StoredPackage, next_prefix, stored_packages, upload_package
from tests.conftest import EXAMPLE_BOOKS, PERIOD_ABBR, FakeRecord, period_gl_rows, period_inputs

Q1 = Quarter.parse("2026Q1")
Q2 = Quarter.parse("2026Q2")
Q3 = Quarter.parse("2026Q3")
TODAY = dt.date(2026, 10, 5)
CHART_SHA256 = hashlib.sha256(EXAMPLE_BOOKS.chart_path.read_bytes()).hexdigest()


def _built(quarter: Quarter = Q3, rows: Sequence[GLRow] | None = None) -> Built:
    """A real `Built`, from the captured fixtures, the way `build_package(period_inputs())` is real everywhere else.

    The GL rows are cut at `quarter`'s end, the way the CLI's own read is:
    the fixtures were captured through 2026Q3, and `trial_balance` refuses
    rows dated after the quarter it is building.
    """
    source = period_gl_rows() if rows is None else rows
    inputs = period_inputs(rows=[row for row in source if row.posting_date <= quarter.end], quarter=quarter)
    package = build_package(inputs)
    index = index_accounts(inputs.chart, inputs.abbr)
    tb = trial_balance(inputs.rows, index, inputs.quarter, inputs.fiscal_year)
    receivables = tuple(open_items(inputs.rows, index, "Receivable", inputs.quarter.end))
    payables = tuple(open_items(inputs.rows, index, "Payable", inputs.quarter.end))
    facts = ManifestFacts(
        quarter=inputs.quarter,
        fiscal_year=inputs.fiscal_year,
        company=inputs.company,
        books_name=EXAMPLE_BOOKS.name,
        site_url=inputs.site_url,
        chart_sha256=CHART_SHA256,
        qmap=inputs.qmap,
        generated_at=dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.UTC),
        generated_by="gavin@carothers.name",
        draft=True,
        today=TODAY,
        bank=package.bank,
        split_vouchers=package.split_vouchers,
        has_journals=True,
    )
    return Built(package=package, trial_balance=tb, receivables=receivables, payables=payables, manifest_facts=facts)


def _reconciled(built: Built) -> Built:
    """`built`, with every bank line's difference zeroed.

    The captured fixture (`tests.conftest.period_inputs`) does not reconcile
    by default -- `test_period_package.py`'s own
    `test_a_fully_reconciled_bank_has_no_bold_differences` does the same
    zeroing for the same reason. Every scenario here that must clear the
    readiness gate needs this; the one that tests a bank difference wants
    the fixture's own, unmodified.
    """
    zeroed = tuple(dataclasses.replace(line, ledger=line.bank) for line in built.package.bank)
    package = dataclasses.replace(built.package, bank=zeroed)
    facts = dataclasses.replace(built.manifest_facts, bank=zeroed)
    return dataclasses.replace(built, package=package, manifest_facts=facts)


def _lock_change(*, version: str = "VER-1", by: str = "gavin@carothers.name", new: dt.date = Q3.end) -> LockChange:
    return LockChange(
        version=version,
        at=dt.datetime(2026, 10, 5, 12, 0),
        by=by,
        old=None,
        new=new,
        old_text="",
        new_text=new.isoformat(),
    )


_OPEN_LOCK = LockState(closed_through=None, bypass_role=None)
_REV_SUFFIX = re.compile(r"rev(\d+)/$")

# What `crosscheck` was called with, and the manifest-rendering callback
# `upload` was handed -- typed here so the fakes below don't have to fall
# back to `object`/untyped tuples that `ty` cannot check.
_CrosscheckArgs = tuple[Package, TrialBalance, tuple[OpenItem, ...], tuple[OpenItem, ...], Quarter, FiscalYear]
_BuildManifest = Callable[[int], bytes]


def _revision_from_prefix(prefix: str) -> int:
    match = _REV_SUFFIX.search(prefix)
    return int(match[1]) if match else 1


class _FakeSteps:
    """A fake `CloseSteps` whose every method logs its name to `self.calls`, in call order.

    `upload` is the one method that is not purely a fake: it runs the real
    `next_prefix` and `upload_package` against `self.record` (a
    `FakeRecord`), the same functions the CLI's own `upload` step will call
    for real. That is what lets a test assert the prefix `close_quarter`
    reports and the revision its stored `MANIFEST.md` names actually agree,
    and what lets a pre-seeded `FakeRecord` exercise `next_prefix`'s own
    half-failed-upload safety property end to end.
    """

    def __init__(
        self,
        *,
        today: dt.date = TODAY,
        lock: LockState = _OPEN_LOCK,
        stored: Sequence[StoredPackage] = (),
        blockers: Sequence[Blocker] = (),
        built: Built | None = None,
        build_sequence: Sequence[Built] | None = None,
        crosscheck_result: Sequence[str] = (),
        set_closed_through_result: LockChange | None = None,
        lock_history_result: Sequence[LockChange] = (),
        record: FakeRecord | None = None,
        quarter: Quarter = Q3,
        abbr: str = PERIOD_ABBR,
    ) -> None:
        self.calls: list[str] = []
        self._today = today
        self._lock = lock
        self._stored = list(stored)
        self._blockers = list(blockers)
        self._built = built or _built()
        self._build_sequence = list(build_sequence) if build_sequence is not None else None
        self._build_index = 0
        self._crosscheck_result = list(crosscheck_result)
        self.crosscheck_args: _CrosscheckArgs | None = None
        self._set_closed_through_result = set_closed_through_result or _lock_change()
        self.set_closed_through_calls: list[dt.date] = []
        self._lock_history_result = list(lock_history_result)
        self.locked_calls: list[Held] = []
        self.upload_calls: list[tuple[dict[str, bytes], _BuildManifest]] = []
        self.record = record if record is not None else FakeRecord()
        self._quarter = quarter
        self._abbr = abbr

    def read_lock(self) -> LockState:
        self.calls.append("read_lock")
        return self._lock

    def read_blockers(self) -> list[Blocker]:
        self.calls.append("read_blockers")
        return self._blockers

    def build(self) -> Built:
        self.calls.append("build")
        if self._build_sequence is not None:
            index = min(self._build_index, len(self._build_sequence) - 1)
            self._build_index += 1
            return self._build_sequence[index]
        return self._built

    def crosscheck(
        self,
        package: Package,
        tb: TrialBalance,
        receivables: tuple[OpenItem, ...],
        payables: tuple[OpenItem, ...],
        quarter: Quarter,
        fiscal_year: FiscalYear,
    ) -> list[str]:
        self.calls.append("crosscheck")
        self.crosscheck_args = (package, tb, receivables, payables, quarter, fiscal_year)
        return self._crosscheck_result

    def set_closed_through(self, date: dt.date) -> LockChange:
        self.calls.append("set_closed_through")
        self.set_closed_through_calls.append(date)
        return self._set_closed_through_result

    def lock_history(self) -> list[LockChange]:
        self.calls.append("lock_history")
        return self._lock_history_result

    def locked(self, held: Held) -> None:
        self.calls.append("locked")
        self.locked_calls.append(held)

    def stored_packages(self) -> list[StoredPackage]:
        self.calls.append("stored_packages")
        return self._stored

    def upload(self, files: dict[str, bytes], build_manifest: _BuildManifest) -> str:
        self.calls.append("upload")
        self.upload_calls.append((files, build_manifest))
        prefix = next_prefix(self.record, self._abbr, self._quarter)
        manifest = build_manifest(_revision_from_prefix(prefix))
        upload_package(self.record, prefix, {**files, "MANIFEST.md": manifest, "SHA256SUMS": sha256sums(files)})
        return prefix

    def today(self) -> dt.date:
        self.calls.append("today")
        return self._today

    def steps(self) -> CloseSteps:
        return CloseSteps(
            read_lock=self.read_lock,
            read_blockers=self.read_blockers,
            build=self.build,
            crosscheck=self.crosscheck,
            set_closed_through=self.set_closed_through,
            lock_history=self.lock_history,
            locked=self.locked,
            stored_packages=self.stored_packages,
            upload=self.upload,
            today=self.today,
        )


PRIOR_STORED = [StoredPackage(Q2, 1, f"{PERIOD_ABBR}/2026Q2/")]
# Q2 is stored, so a fresh Q3 close needs Books Closed Through sitting
# exactly at Q2's own close date -- see `_precondition_failures`.
LOCK_AFTER_Q2 = LockState(closed_through=Q2.end, bypass_role=None)


# --- happy path --------------------------------------------------------


def test_happy_path_runs_every_step_in_order_and_stores_at_the_expected_prefix():
    built = _reconciled(_built())
    change = _lock_change()
    steps = _FakeSteps(lock=LOCK_AFTER_Q2, stored=PRIOR_STORED, built=built, set_closed_through_result=change)

    outcome = close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    assert steps.calls == [
        "today",
        "stored_packages",
        "read_lock",
        "read_blockers",
        "build",
        "crosscheck",
        "set_closed_through",
        "locked",
        "build",
        "upload",
    ]
    assert steps.set_closed_through_calls == [Q3.end]
    assert outcome == CloseOutcome(prefix=f"{PERIOD_ABBR}/2026Q3/", lock=change, package=built.package)


def test_the_first_close_ever_is_exempt_from_the_previous_quarter_check():
    change = _lock_change()
    steps = _FakeSteps(stored=[], built=_reconciled(_built()), set_closed_through_result=change)

    outcome = close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    assert outcome.lock == change
    assert "stored_packages" in steps.calls


def test_the_crosscheck_receives_the_built_package_trial_balance_open_items_quarter_and_fiscal_year():
    built = _reconciled(_built())
    steps = _FakeSteps(lock=LOCK_AFTER_Q2, stored=PRIOR_STORED, built=built)

    close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    assert steps.crosscheck_args is not None
    package, tb, receivables, payables, quarter, fiscal_year = steps.crosscheck_args
    assert package == built.package
    assert tb == built.trial_balance
    assert receivables == built.receivables
    assert payables == built.payables
    assert quarter == Q3
    assert fiscal_year == built.manifest_facts.fiscal_year


def test_the_stored_manifest_names_the_lock_and_matches_the_prefixs_revision():
    built = _reconciled(_built())
    change = _lock_change(version="VER-7", by="gavin@carothers.name")
    steps = _FakeSteps(lock=LOCK_AFTER_Q2, stored=PRIOR_STORED, built=built, set_closed_through_result=change)

    outcome = close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    assert outcome.prefix == f"{PERIOD_ABBR}/2026Q3/"
    manifest = steps.record.objects[f"{outcome.prefix}MANIFEST.md"].decode()
    assert "DRAFT" not in manifest
    assert "- **Closed by:** gavin@carothers.name" in manifest
    assert "- **Lock record:** Company Version `VER-7`" in manifest
    assert "- **Revision:** 1" in manifest


def test_a_reopened_and_reclosed_quarter_writes_the_next_revision_and_the_manifest_agrees():
    # Q3 was closed once (rev1, seeded below), then reopened in the UI:
    # Books Closed Through rolled back to Q2's own close date, exactly as a
    # legitimate reopen leaves it.
    built = _reconciled(_built())
    change = _lock_change(version="VER-9", new=Q3.end)
    record = FakeRecord({
        f"{PERIOD_ABBR}/2026Q3/SHA256SUMS": b"x",
        f"{PERIOD_ABBR}/2026Q3/trial-balance.csv": b"x",
    })
    steps = _FakeSteps(
        lock=LockState(closed_through=Q2.end, bypass_role=None),
        stored=[*PRIOR_STORED, StoredPackage(Q3, 1, f"{PERIOD_ABBR}/2026Q3/")],
        built=built,
        set_closed_through_result=change,
        record=record,
    )

    outcome = close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    assert steps.set_closed_through_calls == [Q3.end]
    assert outcome.prefix == f"{PERIOD_ABBR}/2026Q3/rev2/"
    manifest = record.objects[f"{outcome.prefix}MANIFEST.md"].decode()
    assert "- **Revision:** 2" in manifest


def test_the_first_ever_quarter_reopened_and_reclosed_writes_the_next_revision():
    # Q3 is the very first quarter this company ever closed -- nothing is
    # stored before it -- and it was reopened: Books Closed Through rolled
    # back to unset, since there is no earlier quarter to roll back to.
    # The old rule (which counted Q3's own stored package as "the previous
    # quarter") refused this with a nonsensical "2026Q2 has no stored
    # package"; the fixed rule must not.
    built = _reconciled(_built())
    change = _lock_change(version="VER-9", new=Q3.end)
    record = FakeRecord({f"{PERIOD_ABBR}/2026Q3/SHA256SUMS": b"x"})
    steps = _FakeSteps(
        lock=LockState(closed_through=None, bypass_role=None),
        stored=[StoredPackage(Q3, 1, f"{PERIOD_ABBR}/2026Q3/")],
        built=built,
        set_closed_through_result=change,
        record=record,
    )

    outcome = close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    assert steps.set_closed_through_calls == [Q3.end]
    assert outcome.prefix == f"{PERIOD_ABBR}/2026Q3/rev2/"
    manifest = record.objects[f"{outcome.prefix}MANIFEST.md"].decode()
    assert "- **Revision:** 2" in manifest


def test_the_first_ever_quarter_rerun_with_nothing_reopened_is_refused():
    # Same "nothing stored before Q3" shape as above, but nothing was
    # reopened: the lock still reads Q3's own end and Q3 already has a
    # complete stored package, so this is ruling 4's refusal, not a close.
    steps = _FakeSteps(
        lock=LockState(closed_through=Q3.end, bypass_role=None),
        stored=[StoredPackage(Q3, 1, f"{PERIOD_ABBR}/2026Q3/")],
    )

    with pytest.raises(PeriodError, match=r"2026Q3 already has a stored package.*runningtab period status"):
        close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    assert "build" not in steps.calls
    assert "set_closed_through" not in steps.calls


# --- reopening any quarter: stored x lock, for Q2 and Q3 ------------------


def _stored_as(quarters: Sequence[Quarter]) -> list[StoredPackage]:
    return [StoredPackage(q, 1, f"{PERIOD_ABBR}/{q.label}/") for q in quarters]


def _record_holding(stored: Sequence[StoredPackage]) -> FakeRecord:
    return FakeRecord({f"{p.prefix}SHA256SUMS": b"x" for p in stored})


_CLOSES_REV1 = "closes at rev1"
_CLOSES_REV2 = "closes at rev2"


@pytest.mark.parametrize(
    ("closing", "stored", "lock", "expected"),
    [
        # Closing Q2, Q1 already stored.
        (Q2, [Q1], Q1.end, _CLOSES_REV1),
        (Q2, [Q1], Q2.end, _CLOSES_REV1),  # recovery: locked, never stored
        (Q2, [Q1], Q3.end, "2026Q1 is stored through 2026-03-31"),
        (Q2, [Q1], None, "Books Closed Through is unset"),
        (Q2, [Q1, Q2], Q1.end, _CLOSES_REV2),  # Q2 reopened, the latest
        (Q2, [Q1, Q2], Q2.end, "2026Q2 already has a stored package"),
        (Q2, [Q1, Q2], Q3.end, "2026Q2 is already closed"),
        (Q2, [Q1, Q2], None, "Books Closed Through is unset"),
        (Q2, [Q1, Q2, Q3], Q1.end, _CLOSES_REV2),  # Q2 reopened with Q3 stored after it
        (Q2, [Q1, Q2, Q3], Q2.end, "2026Q2 already has a stored package"),
        (Q2, [Q1, Q2, Q3], Q3.end, "2026Q2 is already closed"),
        (Q2, [Q1, Q2, Q3], None, "re-close 2026Q1, then 2026Q2, then 2026Q3"),
        (Q2, [Q2, Q3], Q1.end, _CLOSES_REV2),  # Q2 was the first close ever, reopened
        (Q2, [Q2, Q3], None, _CLOSES_REV2),
        (Q2, [Q1, Q3], Q1.end, "2026Q2 was never closed"),
        (Q2, [Q1, Q3], Q3.end, "2026Q2 was never closed"),
        # Closing Q3.
        (Q3, [Q1, Q2], Q2.end, _CLOSES_REV1),
        (Q3, [Q1, Q2], Q3.end, _CLOSES_REV1),  # recovery
        (Q3, [Q1, Q2], Q1.end, "re-close 2026Q2, then close 2026Q3"),
        (Q3, [Q1, Q2], None, "re-close 2026Q1, then 2026Q2, then close 2026Q3"),
        (Q3, [Q1, Q2, Q3], Q2.end, _CLOSES_REV2),  # Q3 reopened, the latest
        (Q3, [Q1, Q2, Q3], Q3.end, "2026Q3 already has a stored package"),
        (Q3, [Q1, Q2, Q3], Q1.end, "re-close 2026Q2, then 2026Q3"),
        (Q3, [Q1, Q2, Q3], None, "re-close 2026Q1, then 2026Q2, then 2026Q3"),
    ],
)
def test_which_closes_go_ahead_for_every_stored_and_lock_combination(closing, stored, lock, expected):
    packages = _stored_as(stored)
    record = _record_holding(packages)
    steps = _FakeSteps(
        lock=LockState(closed_through=lock, bypass_role=None),
        stored=packages,
        built=_reconciled(_built(closing)),
        set_closed_through_result=_lock_change(new=closing.end),
        lock_history_result=[_lock_change(new=closing.end)],
        record=record,
        quarter=closing,
    )

    if expected in (_CLOSES_REV1, _CLOSES_REV2):
        outcome = close_quarter(steps.steps(), closing, PERIOD_ABBR)
        suffix = "" if expected == _CLOSES_REV1 else "rev2/"
        assert outcome.prefix == f"{PERIOD_ABBR}/{closing.label}/{suffix}"
        # The lock only ever moves forward, and only to this quarter's end.
        assert steps.set_closed_through_calls in ([], [closing.end])
        if lock is not None:
            assert all(date >= lock for date in steps.set_closed_through_calls)
    else:
        with pytest.raises(PeriodError) as exc:
            close_quarter(steps.steps(), closing, PERIOD_ABBR)
        assert expected in str(exc.value)
        assert "build" not in steps.calls
        assert "set_closed_through" not in steps.calls


def test_reopening_q2_with_q3_stored_recloses_q2_then_q3_in_order():
    # Q1, Q2 and Q3 are stored and locked through Q3's end. A person reopens
    # Q2 in ERPNext by moving Books Closed Through back to Q1's end, posts a
    # correction, and then re-closes Q2 and Q3 in that order. Each close runs
    # the real ceremony against one shared record and one lock that each
    # close moves, so nothing here is a per-step fixture that could agree
    # with the rule by construction.
    record = _record_holding(_stored_as([Q1, Q2, Q3]))
    lock = LockState(closed_through=Q1.end, bypass_role=None)

    def close(quarter: Quarter) -> CloseOutcome:
        nonlocal lock
        steps = _FakeSteps(
            lock=lock,
            stored=stored_packages(record, PERIOD_ABBR),
            built=_reconciled(_built(quarter)),
            set_closed_through_result=_lock_change(new=quarter.end),
            record=record,
            quarter=quarter,
        )
        outcome = close_quarter(steps.steps(), quarter, PERIOD_ABBR)
        lock = LockState(closed_through=steps.set_closed_through_calls[-1], bypass_role=None)
        return outcome

    # Q3 first is refused, naming the true order.
    with pytest.raises(PeriodError, match=r"re-close 2026Q2, then 2026Q3"):
        close(Q3)

    assert close(Q2).prefix == f"{PERIOD_ABBR}/2026Q2/rev2/"
    assert lock.closed_through == Q2.end
    assert close(Q3).prefix == f"{PERIOD_ABBR}/2026Q3/rev2/"
    assert lock.closed_through == Q3.end

    # Both are now finished closes again: neither re-runs.
    with pytest.raises(PeriodError, match="2026Q3 already has a stored package"):
        close(Q3)
    with pytest.raises(PeriodError, match="2026Q2 is already closed"):
        close(Q2)


# --- preconditions -------------------------------------------------------


def test_refuses_to_close_a_quarter_before_a_later_one_thats_already_stored():
    # Q3 was the first-ever close, then reopened (lock cleared): closing
    # Q2 now sees nothing stored *before* Q2, so the previous-quarter rule
    # alone would wave it through as a "first close" -- exactly the gap
    # the old, un-filtered rule blocked by accident. Something stored
    # *after* the quarter being closed must refuse on its own.
    steps = _FakeSteps(
        today=dt.date(2026, 7, 5),  # after Q2 ends, so only this check is in play
        lock=_OPEN_LOCK,
        stored=[StoredPackage(Q3, 1, f"{PERIOD_ABBR}/2026Q3/")],
    )

    with pytest.raises(PeriodError) as exc:
        close_quarter(steps.steps(), Q2, PERIOD_ABBR)

    message = str(exc.value)
    assert (
        f"{PERIOD_ABBR}: 2026Q2 was never closed and 2026Q3, after it, is already stored; quarters close in "
        "order, so 2026Q2 can no longer be closed."
    ) in message
    # The lock is cleared, so Q3 -- the one stored quarter -- is what is
    # actually open, and re-closing it is the true next step.
    assert "Books Closed Through leaves stored quarters open: re-close 2026Q3" in message
    assert "build" not in steps.calls


def test_refuses_to_close_a_quarter_before_a_later_stored_one_naming_both_failures():
    # Same later-quarter gap, but the lock also reads past Q2's own end
    # (Q3's close date): both checks fire, and the refusal must name both.
    steps = _FakeSteps(
        today=dt.date(2026, 7, 5),
        lock=LockState(closed_through=Q3.end, bypass_role=None),
        stored=[StoredPackage(Q3, 1, f"{PERIOD_ABBR}/2026Q3/")],
    )

    with pytest.raises(PeriodError) as exc:
        close_quarter(steps.steps(), Q2, PERIOD_ABBR)

    message = str(exc.value)
    assert f"{PERIOD_ABBR}: 2026Q2 was never closed and 2026Q3, after it, is already stored" in message
    assert "Nothing stored is reopened" in message
    assert "already past" in message
    assert "build" not in steps.calls


def test_refuses_before_the_quarter_has_ended():
    # lock=LOCK_AFTER_Q2 keeps the previous-quarter check clean, so this
    # isolates "has not ended" as the only failure -- otherwise the
    # default (unset) lock would also fail the previous-quarter check
    # against PRIOR_STORED, and a substring match would hide that.
    steps = _FakeSteps(today=dt.date(2026, 8, 1), lock=LOCK_AFTER_Q2, stored=PRIOR_STORED)

    with pytest.raises(PeriodError) as exc:
        close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    assert str(exc.value) == "cannot close VT 2026Q3: 2026Q3 has not ended: today is 2026-08-01, it ends 2026-09-30"
    assert steps.calls == ["today", "stored_packages", "read_lock"]


def test_refuses_when_the_previous_quarter_has_no_stored_package():
    steps = _FakeSteps(stored=[StoredPackage(Q1, 1, f"{PERIOD_ABBR}/2026Q1/")])

    with pytest.raises(PeriodError, match="2026Q2, has no stored package"):
        close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    assert "build" not in steps.calls


def test_refuses_when_nothing_is_stored_yet_but_the_lock_is_already_past_the_quarter():
    steps = _FakeSteps(lock=LockState(closed_through=dt.date(2026, 12, 31), bypass_role=None), stored=[])

    with pytest.raises(PeriodError, match="already past"):
        close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    assert "build" not in steps.calls


def test_refuses_when_the_lock_was_cleared_after_the_previous_quarter_closed():
    # Q2 has a stored package, so Books Closed Through must read exactly
    # Q2's end (or Q3's, for recovery) -- not unset.
    steps = _FakeSteps(lock=_OPEN_LOCK, stored=PRIOR_STORED)

    with pytest.raises(PeriodError, match="Books Closed Through is unset"):
        close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    assert "build" not in steps.calls


def test_refuses_when_the_lock_points_at_an_earlier_quarter_than_what_is_stored():
    steps = _FakeSteps(lock=LockState(closed_through=Q1.end, bypass_role=None), stored=PRIOR_STORED)

    with pytest.raises(PeriodError, match="2026Q2 is stored through 2026-06-30"):
        close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    assert "build" not in steps.calls


def test_refuses_when_stored_and_the_lock_overshoots_past_the_quarter():
    steps = _FakeSteps(lock=LockState(closed_through=dt.date(2026, 12, 31), bypass_role=None), stored=PRIOR_STORED)

    with pytest.raises(PeriodError, match="2026Q2 is stored through 2026-06-30"):
        close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    assert "build" not in steps.calls


def test_refuses_to_reclose_a_quarter_that_already_has_a_complete_stored_package():
    steps = _FakeSteps(
        lock=LockState(closed_through=Q3.end, bypass_role=None),
        stored=[*PRIOR_STORED, StoredPackage(Q3, 1, f"{PERIOD_ABBR}/2026Q3/")],
    )

    with pytest.raises(PeriodError) as exc:
        close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    message = str(exc.value)
    assert f"2026Q3 already has a stored package at '{PERIOD_ABBR}/2026Q3/'" in message
    # An interrupted store looks identical to a finished close from here --
    # the message must tell the person how to tell them apart, since this
    # refusal alone cannot.
    assert (
        "If a close of 2026Q3 was interrupted after locking, run `runningtab period status`: if it reports "
        "changes against the stored package, reopen the quarter in ERPNext and close it again."
    ) in message
    assert "build" not in steps.calls
    assert "set_closed_through" not in steps.calls
    assert "upload" not in steps.calls


def test_refuses_when_a_bypass_role_is_set():
    # closed_through=Q2.end keeps the previous-quarter check clean, so this
    # isolates the bypass-role failure as the only one.
    steps = _FakeSteps(lock=LockState(closed_through=Q2.end, bypass_role="Accounts Manager"), stored=PRIOR_STORED)

    with pytest.raises(PeriodError) as exc:
        close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    assert str(exc.value) == (
        "cannot close VT 2026Q3: role_allowed_for_frozen_entries is 'Accounts Manager'; it must be empty before closing"
    )
    assert "build" not in steps.calls


def test_a_single_call_names_every_failing_precondition_at_once():
    steps = _FakeSteps(
        today=dt.date(2026, 8, 1),
        lock=LockState(closed_through=None, bypass_role="Accounts Manager"),
        stored=[StoredPackage(Q1, 1, f"{PERIOD_ABBR}/2026Q1/")],
    )

    with pytest.raises(PeriodError) as exc:
        close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    message = str(exc.value)
    assert "has not ended" in message
    assert "no stored package" in message
    assert "role_allowed_for_frozen_entries" in message


# --- recovery: closed_through already equal to the quarter's end --------


def test_recovery_skips_a_second_lock_and_writes_the_next_revision_around_a_half_failed_upload():
    built = _reconciled(_built())
    original = _lock_change(version="VER-1", new=Q3.end)
    # The lock succeeded and upload started, but died before SHA256SUMS:
    # stored_packages() (which only recognises a *complete* package)
    # reports nothing for Q3, but the orphaned content object is still
    # there for the real next_prefix to find and skip past.
    record = FakeRecord({f"{PERIOD_ABBR}/2026Q3/trial-balance.csv": b"stale"})
    steps = _FakeSteps(
        lock=LockState(closed_through=Q3.end, bypass_role=None),
        stored=PRIOR_STORED,
        built=built,
        lock_history_result=[original],
        record=record,
    )

    outcome = close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    assert steps.set_closed_through_calls == []
    assert "set_closed_through" not in steps.calls
    assert outcome.lock == original
    assert outcome.prefix == f"{PERIOD_ABBR}/2026Q3/rev2/"
    manifest = record.objects[f"{outcome.prefix}MANIFEST.md"].decode()
    assert "- **Revision:** 2" in manifest


def test_recovery_refuses_when_no_version_confirms_the_existing_lock():
    steps = _FakeSteps(
        lock=LockState(closed_through=Q3.end, bypass_role=None),
        stored=PRIOR_STORED,
        built=_reconciled(_built()),
        lock_history_result=[],
    )

    with pytest.raises(PeriodError, match=r"newest Version does not confirm it \(newest recorded value: unset\)"):
        close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    assert "set_closed_through" not in steps.calls
    assert "upload" not in steps.calls


def test_recovery_refuses_when_the_newest_version_disagrees_with_the_live_lock():
    # Something else changed the lock date since -- the newest Version does
    # not confirm quarter.end, even though the live field reads quarter.end.
    stale = _lock_change(version="VER-OLD", new=Q2.end)
    steps = _FakeSteps(
        lock=LockState(closed_through=Q3.end, bypass_role=None),
        stored=PRIOR_STORED,
        built=_reconciled(_built()),
        lock_history_result=[stale],
    )

    with pytest.raises(PeriodError, match="newest Version does not confirm it"):
        close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    assert "set_closed_through" not in steps.calls
    assert "upload" not in steps.calls


# --- readiness -----------------------------------------------------------


def test_refuses_naming_every_readiness_blocker_and_never_locks():
    blockers = [
        Blocker(kind="suspense", doctype="GL Entry", name="GLE-1", detail="posted to suspense on 2026-08-01"),
        Blocker(kind="unreconciled", doctype="Bank Transaction", name="BT-1", detail="12.50 unallocated"),
        Blocker(kind="draft", doctype="Journal Entry", name="JV-9", detail="draft dated 2026-08-02"),
    ]
    steps = _FakeSteps(lock=LOCK_AFTER_Q2, stored=PRIOR_STORED, blockers=blockers)

    with pytest.raises(PeriodError) as exc:
        close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    message = str(exc.value)
    for blocker in blockers:
        assert blocker.name in message
    assert steps.calls.index("read_blockers") < steps.calls.index("build")
    assert "crosscheck" not in steps.calls
    assert "set_closed_through" not in steps.calls


def test_refuses_on_a_nonzero_bank_difference_naming_each_account_and_amount():
    # The captured fixture does not reconcile out of the box -- see
    # `_reconciled`'s docstring -- so the plain `_built()` already exercises
    # this gate with no further modification needed.
    built = _built()
    steps = _FakeSteps(lock=LOCK_AFTER_Q2, stored=PRIOR_STORED, built=built)

    with pytest.raises(PeriodError) as exc:
        close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    message = str(exc.value)
    assert "bank difference on acct_chk_1 (1111): -897.61" in message
    assert "bank difference on acct_chk_2 (1112): 47.61" in message
    assert "bank difference on acct_credit_1 (2121): -159.00" in message
    assert "crosscheck" not in steps.calls
    assert "set_closed_through" not in steps.calls


def test_a_build_refusal_is_combined_with_any_blockers_into_one_refusal():
    blockers = [Blocker(kind="suspense", doctype="GL Entry", name="GLE-1", detail="posted to suspense on 2026-08-01")]

    class _FailingBuildSteps(_FakeSteps):
        def build(self) -> Built:
            self.calls.append("build")
            raise PeriodError("the trial balance does not balance: off by 12.34")

    steps = _FailingBuildSteps(lock=LOCK_AFTER_Q2, stored=PRIOR_STORED, blockers=blockers)

    with pytest.raises(PeriodError) as exc:
        close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    message = str(exc.value)
    assert "GLE-1" in message
    assert "does not balance" in message
    assert steps.calls.index("read_blockers") < steps.calls.index("build")


# --- cross-check -----------------------------------------------------------


def test_refuses_on_a_crosscheck_mismatch_and_never_locks():
    steps = _FakeSteps(
        lock=LOCK_AFTER_Q2,
        stored=PRIOR_STORED,
        built=_reconciled(_built()),
        crosscheck_result=["account 4000 Sales Revenue: ours -100.00, ERPNext -90.00"],
    )

    with pytest.raises(PeriodError, match="4000 Sales Revenue"):
        close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    assert "set_closed_through" not in steps.calls
    assert "upload" not in steps.calls


# --- rebuild and compare --------------------------------------------------


def _with_a_late_voucher() -> Built:
    """The fixture ledger plus one more Journal Entry dated in Q3: a real, balanced copy of `ACC-JV-2026-00010`
    under a new voucher name, as if someone posted it between the check and the lock.
    """
    rows = period_gl_rows()
    late = [
        dataclasses.replace(row, name=f"{row.name}-LATE", voucher_no="ACC-JV-2026-00099")
        for row in rows
        if row.voucher_no == "ACC-JV-2026-00010"
    ]
    assert len(late) == 2
    return _reconciled(_built(Q3, rows=[*rows, *late]))


def test_refuses_when_the_rebuild_differs_after_the_lock_leaving_the_lock_in_place_and_nothing_uploaded():
    before = _reconciled(_built())
    after = _with_a_late_voucher()
    change = _lock_change()
    steps = _FakeSteps(
        lock=LOCK_AFTER_Q2, stored=PRIOR_STORED, build_sequence=[before, after], set_closed_through_result=change
    )

    # HeldForReview, not a bare PeriodError: it names its own next step,
    # so the CLI adds no generic retry line of its own beside it.
    with pytest.raises(HeldForReview) as exc:
        close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    message = str(exc.value)
    assert f"{PERIOD_ABBR} 2026Q3 stays locked through {Q3.end.isoformat()} with no new stored package" in message
    assert "general-ledger.csv" in message
    # The spec's step 5 names the GL entries that differ, not only files.
    assert "Journal Entry ACC-JV-2026-00099" in message
    assert "ACC-JV-2026-00010" not in message
    # A re-run re-checks the now-locked ledger and stores it; that is the
    # recovery, and the message says so.
    assert "run `runningtab period close --quarter 2026Q3` again to re-check and store what is now locked" in message
    assert (
        "reopen 2026Q3 (move Books Closed Through back to 2026-06-30 in ERPNext), reverse it, and run "
        "`runningtab period close --quarter 2026Q3` again"
    ) in message

    # The lock already happened and stays -- there is no rollback.
    assert steps.set_closed_through_calls == [Q3.end]
    assert "upload" not in steps.calls


def test_a_rerun_after_a_rebuild_mismatch_stores_what_is_now_locked():
    # The first run locked and then saw the ledger move. The re-run finds
    # the lock at Q3's end (recovery), re-checks the ledger as it now is,
    # and -- the ledger no longer moving -- stores it.
    record = FakeRecord()
    late = _with_a_late_voucher()
    first = _FakeSteps(
        lock=LOCK_AFTER_Q2,
        stored=PRIOR_STORED,
        build_sequence=[_reconciled(_built()), late],
        set_closed_through_result=_lock_change(new=Q3.end),
        record=record,
    )
    with pytest.raises(HeldForReview):
        close_quarter(first.steps(), Q3, PERIOD_ABBR)

    rerun = _FakeSteps(
        lock=LockState(closed_through=Q3.end, bypass_role=None),
        stored=PRIOR_STORED,
        built=late,
        lock_history_result=[_lock_change(new=Q3.end)],
        record=record,
    )
    outcome = close_quarter(rerun.steps(), Q3, PERIOD_ABBR)

    assert outcome.prefix == f"{PERIOD_ABBR}/2026Q3/"
    assert outcome.package == late.package
    assert "set_closed_through" not in rerun.calls


def test_the_caller_is_told_the_lock_before_anything_after_it_can_fail():
    # `locked` is how the CLI knows the quarter is frozen when a later stage
    # fails -- on a fresh lock and on recovery alike.
    fresh = _FakeSteps(lock=LOCK_AFTER_Q2, stored=PRIOR_STORED, built=_reconciled(_built()))
    close_quarter(fresh.steps(), Q3, PERIOD_ABBR)
    assert fresh.calls[-4:] == ["set_closed_through", "locked", "build", "upload"]
    assert fresh.locked_calls == [Held(_lock_change(), reclosing=False)]

    original = _lock_change(version="VER-1", new=Q3.end)
    recovery = _FakeSteps(
        lock=LockState(closed_through=Q3.end, bypass_role=None),
        stored=PRIOR_STORED,
        built=_reconciled(_built()),
        lock_history_result=[original],
    )
    close_quarter(recovery.steps(), Q3, PERIOD_ABBR)
    assert recovery.calls[-4:] == ["lock_history", "locked", "build", "upload"]
    assert recovery.locked_calls == [Held(original, reclosing=False)]

    refused = _FakeSteps(lock=LOCK_AFTER_Q2, stored=PRIOR_STORED, built=_built())  # a bank difference
    with pytest.raises(PeriodError):
        close_quarter(refused.steps(), Q3, PERIOD_ABBR)
    assert refused.locked_calls == []


def test_a_rebuild_mismatch_on_a_reclose_says_reopen_and_close_again_not_rerun():
    # Q3 already has rev1 and was reopened. The re-close locks, then the
    # ledger moves. A plain re-run would now be refused as "already has a
    # stored package", so the message must not promise one: the way back is
    # to reopen Q3 and close it again.
    steps = _FakeSteps(
        lock=LockState(closed_through=Q2.end, bypass_role=None),
        stored=[*PRIOR_STORED, StoredPackage(Q3, 1, f"{PERIOD_ABBR}/2026Q3/")],
        build_sequence=[_reconciled(_built()), _with_a_late_voucher()],
        set_closed_through_result=_lock_change(new=Q3.end),
    )

    with pytest.raises(HeldForReview) as exc:
        close_quarter(steps.steps(), Q3, PERIOD_ABBR)

    message = str(exc.value)
    assert "re-check and store what is now locked" not in message
    assert (
        "Check what posted; then reopen 2026Q3 (move Books Closed Through back to 2026-06-30 in ERPNext), "
        "reverse anything that should not be in 2026Q3, and run `runningtab period close --quarter 2026Q3` again"
    ) in message
    assert steps.locked_calls == [Held(_lock_change(new=Q3.end), reclosing=True)]


def test_the_reclose_advice_is_true_a_plain_rerun_is_refused_and_a_reopen_goes_through():
    # Proves the advice above against close_quarter itself: after the failed
    # re-close the lock sits at Q3's end with rev1 stored, so a re-run is
    # refused; moving the lock back (the reopen) lets the close through.
    stored = [*PRIOR_STORED, StoredPackage(Q3, 1, f"{PERIOD_ABBR}/2026Q3/")]
    rerun = _FakeSteps(
        lock=LockState(closed_through=Q3.end, bypass_role=None), stored=stored, built=_reconciled(_built())
    )
    with pytest.raises(PeriodError, match="already has a stored package"):
        close_quarter(rerun.steps(), Q3, PERIOD_ABBR)

    reopened = _FakeSteps(
        lock=LockState(closed_through=Q2.end, bypass_role=None),
        stored=stored,
        built=_reconciled(_built()),
        record=FakeRecord({f"{PERIOD_ABBR}/2026Q3/SHA256SUMS": b"x"}),
    )
    assert close_quarter(reopened.steps(), Q3, PERIOD_ABBR).prefix == f"{PERIOD_ABBR}/2026Q3/rev2/"


def test_a_reclose_of_an_earlier_quarter_hands_the_caller_the_stored_quarters_after_it():
    # Q2 and Q3 are stored and Q2 was reopened. The re-close of Q2 tells the
    # caller Q3 comes after it, so a failure can name the whole order.
    record = _record_holding(_stored_as([Q1, Q2, Q3]))
    change = _lock_change(new=Q2.end)
    steps = _FakeSteps(
        lock=LockState(closed_through=Q1.end, bypass_role=None),
        stored=stored_packages(record, PERIOD_ABBR),
        built=_reconciled(_built(Q2)),
        set_closed_through_result=change,
        record=record,
        quarter=Q2,
    )

    close_quarter(steps.steps(), Q2, PERIOD_ABBR)

    assert steps.locked_calls == [Held(change, reclosing=True, later=(Q3,))]
