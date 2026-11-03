"""`close_quarter`: the close ceremony's exact order, driven through injected callables.

The close runs in six stages: preconditions, readiness,
build-and-cross-check, lock, rebuild-and-compare, store. Every stage but the
last two guards the next: a failure in an earlier stage means nothing later
ever runs, nothing changes anywhere, and the refusal names every offending
item in that stage at once, not just the first.

`CloseSteps` is that ceremony's whole outside world -- ERPNext, the record
bucket, the clock -- as one dataclass of callables, so `close_quarter` runs
against fakes with no site and no bucket. The brief names eight of its
fields verbatim (`read_lock`, `read_blockers`, `build`, `crosscheck`,
`set_closed_through`, `stored_packages`, `upload`, `today`) and leaves each
one's exact parameters to be chosen here; this module also adds a ninth
and a tenth, `lock_history` and `locked`, and explains why below.

Two fields' shapes need a word up front:

- `build` returns `Built`, not a bare `Package`: `crosscheck` has to compare
  against ERPNext's own Trial Balance and AR/AP reports account-by-account
  and party-by-party, which needs the `TrialBalance` and `OpenItem` lists
  `build_package` computes on its way to `Package` -- and `Package` itself
  (`runningtab.period.package.Package`) carries only the rendered files, bank
  reconciliation and split-voucher list, on purpose: plan 1 already shipped
  that shape and this task does not reopen it. `Built` is the richer bundle
  `build` hands back instead, carrying a `ManifestFacts` alongside so the
  Store stage never has to re-derive company/site/chart/qmap facts that
  `build` already had to hand.
- `upload` bundles "pick the next storage prefix," "render the final
  manifest naming that prefix's revision," and "write it" into one call,
  rather than three separate `CloseSteps` fields. `next_prefix` (Task 5,
  `runningtab.period.record`) must probe the real bucket's object listing, not
  the `stored_packages` snapshot this module already holds from the
  precondition stage, to land on a free revision even after a half-failed
  prior upload (see `next_prefix`'s own docstring) -- so only the "upload"
  step's own implementation, which alone touches the real `Record`, can
  correctly choose the revision. But `MANIFEST.md`'s `revision` field must
  name that exact same number, so `close_quarter` cannot render the
  manifest before the revision is chosen. `upload` resolves that by taking
  a `build_manifest(revision) -> bytes` callback: its real implementation
  calls `next_prefix`, invokes the callback with the revision it picked,
  assembles `SHA256SUMS` and the manifest around the content files, calls
  `upload_package`, and returns the prefix used -- one code path decides the
  revision and feeds it straight to the manifest, so the two can never
  disagree.

Why `lock_history` (a ninth field, not in the brief's literal list): in the
recovery case -- `closed_through` already equals `quarter.end` -- step 4
must not call `set_closed_through` a second time (its own Version audit
trail would otherwise carry a redundant no-op edit). But `CloseOutcome.lock`
is a `LockChange`, not an optional one, in every case including recovery.
`read_lock` (Task 4) returns only a `LockState` -- closed_through and
bypass_role, no version/who/when -- so it cannot supply one. `lock_history`
(already written in `runningtab.period.lock` for exactly this "who moved the
lock and when" question) is the one honest source for the `LockChange` that
already put `quarter.end` in place; fabricating one from `LockState` alone
would be a confident wrong answer about who closed the quarter and when.

Why `locked` (a tenth field): once stage 4 has run, the quarter is frozen
in ERPNext whatever happens next, and the caller must be able to say so if
stage 5 or 6 fails -- on a fresh lock and on recovery alike, where this run
moved nothing. `close_quarter` calls `locked` with a `Held` -- the
`LockChange` that froze the quarter, and whether the quarter already had a
stored package -- before anything after the lock runs. The second half
decides the next step: a first close's store can simply be retried, but a
re-close of a stored quarter cannot (the re-run is refused as "already has a
stored package"), so the person reopens it and closes it again.

The spec's stage 5 says a rebuild mismatch "reports the GL entries that
differ". `HeldForReview` names every differing file, and for
`general-ledger.csv` the vouchers (type and name) whose rows differ, read
straight from that file's own `voucher_type`/`voucher_no` columns. A change
that reaches only other files -- a backdated entry moving an opening
balance in `trial-balance.csv`, say -- is named by file alone, since no
row in this quarter's general ledger carries it.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import io
from collections.abc import Callable, Iterable
from typing import NoReturn

from runningtab.period import PeriodError
from runningtab.period.balances import TrialBalance
from runningtab.period.csvfmt import money
from runningtab.period.lock import LockChange, LockState
from runningtab.period.manifest import ManifestFacts, render_manifest
from runningtab.period.open_items import OpenItem
from runningtab.period.package import Package
from runningtab.period.quarter import FiscalYear, Quarter
from runningtab.period.readiness import Blocker
from runningtab.period.record import StoredPackage


@dataclasses.dataclass(frozen=True)
class Built:
    """One `build` call's output: the rendered `Package` plus what `crosscheck` and the manifest need.

    `manifest_facts` is the build-time `ManifestFacts` -- quarter, fiscal
    year, company, site, chart, qmap, bank, split vouchers, and so on --
    with `closed_by`, `lock_version` and `revision` still at their defaults;
    the Store stage fills those three in with `dataclasses.replace` once the
    lock and the chosen revision are known, rather than `close_quarter`
    re-deriving the rest from scratch.
    """

    package: Package
    trial_balance: TrialBalance
    receivables: tuple[OpenItem, ...]
    payables: tuple[OpenItem, ...]
    manifest_facts: ManifestFacts


@dataclasses.dataclass(frozen=True)
class CloseSteps:
    """The close ceremony's outside world, as callables, so `close_quarter` runs with no site and no bucket.

    Every callable here is a zero-or-few-argument closure the CLI builds
    already scoped to one company, one quarter and one bucket -- the same
    style Task 4's `set_closed_through(admin, company, date)` uses, closed
    over `admin`/`company` by the CLI and given only the date each call.
    `crosscheck` and `upload`'s exact parameters are chosen here (see the
    module docstring); the rest take nothing and return the one fact their
    name promises.
    """

    read_lock: Callable[[], LockState]
    read_blockers: Callable[[], list[Blocker]]
    build: Callable[[], Built]
    crosscheck: Callable[
        [Package, TrialBalance, tuple[OpenItem, ...], tuple[OpenItem, ...], Quarter, FiscalYear], list[str]
    ]
    set_closed_through: Callable[[dt.date], LockChange]
    lock_history: Callable[[], list[LockChange]]
    locked: Callable[[Held], None]
    stored_packages: Callable[[], list[StoredPackage]]
    upload: Callable[[dict[str, bytes], Callable[[int], bytes]], str]
    today: Callable[[], dt.date]


@dataclasses.dataclass(frozen=True)
class Held:
    """The quarter is now locked: the change that locked it, whether this is a re-close of a stored quarter,
    and the stored quarters after it, which a reopen of this one leaves to be re-closed in order."""

    change: LockChange
    reclosing: bool
    later: tuple[Quarter, ...] = ()


@dataclasses.dataclass(frozen=True)
class CloseOutcome:
    """What a successful close produced: where the package landed, the lock change that closed it, the package itself."""

    prefix: str
    lock: LockChange
    package: Package


class HeldForReview(PeriodError):
    """The quarter stays locked through the close date with no stored package, because the ledger moved.

    Raised when the rebuild-and-compare stage (5) finds the ledger changed
    between the check and the lock. The lock is not rolled back (there is
    no rollback). On a first close, re-running `close` for the quarter is
    the recovery: it lands in the recovery branch, re-runs readiness and the
    cross-check against the ledger as it now is (locked, so no longer
    moving), rebuilds, and stores that. On a re-close of a stored quarter a
    re-run is refused as "already has a stored package", so the recovery is
    to reopen the quarter and close it again. If what posted does not belong
    in the quarter, the person reopens it either way. The message says which,
    so a caller adds no retry advice of its own beside it.
    """


def reclose_order(closed_through: dt.date | None, stored: Iterable[Quarter]) -> list[Quarter]:
    """Every stored quarter Books Closed Through no longer covers, oldest first: the order to re-close them in.

    Quarters close in order, so a reopened quarter is re-closed before any
    stored quarter after it -- each of those re-closes then finds the lock
    exactly at its own previous quarter's end, which is what lets it through.
    """
    return sorted({q for q in stored if closed_through is None or q.end > closed_through})


def reclose_steps(order: list[Quarter], then: Quarter | None = None) -> str:
    """`order` (from `reclose_order`) as a person's next steps: "re-close 2026Q2, then 2026Q3".

    `then` names a quarter to close after them that was never stored -- the
    one someone just tried to close, when the lock says others come first.
    """
    text = "re-close " + ", then ".join(q.label for q in order)
    if then is not None and then not in order:
        text += f", then close {then.label}"
    return text


def _refuse(abbr: str, quarter: Quarter, items: list[str]) -> NoReturn:
    raise PeriodError(f"cannot close {abbr} {quarter.label}: {'; '.join(items)}")


def _stored_entry(stored: list[StoredPackage], quarter: Quarter) -> StoredPackage | None:
    return next((p for p in stored if p.quarter == quarter), None)


def _already_stored_message(quarter: Quarter, existing: StoredPackage) -> str:
    """A finished close and an interrupted one look identical here -- lock at `quarter.end`, a stored package --
    so this refuses either way, but the interrupted case is a person's mistake to fix, not a bug to report, and
    the message says exactly what to do about it.
    """
    return (
        f"{quarter.label} already has a stored package at {existing.prefix!r}; `runningtab period status` checks it "
        f"against the ledger, `close` does not re-run it. If a close of {quarter.label} was interrupted after "
        "locking, run `runningtab period status`: if it reports changes against the stored package, reopen the "
        "quarter in ERPNext and close it again."
    )


def _already_closed_message(quarter: Quarter, existing: StoredPackage, closed_through: dt.date) -> str:
    return (
        f"{quarter.label} is already closed: stored at {existing.prefix!r}, and Books Closed Through "
        f"({closed_through.isoformat()}) is past its end. To change it, reopen {quarter.label} in ERPNext by moving "
        f"Books Closed Through back to {quarter.previous.end.isoformat()}, then re-close it and every stored "
        "quarter after it, in order"
    )


def _never_closed_message(abbr: str, quarter: Quarter, nearest: Quarter, order: list[Quarter]) -> str:
    """`quarter` was never stored, but a later quarter was: it can never be closed now, so name what can be done.

    Nothing reaches this through `close` itself -- the previous-quarter rule
    stops a gap from forming -- so `quarter` is before the first quarter this
    company ever closed. The next step is whatever the lock says is reopened,
    or, if nothing is, the next quarter after the latest stored one.
    """
    text = (
        f"{abbr}: {quarter.label} was never closed and {nearest.label}, after it, is already stored; quarters "
        f"close in order, so {quarter.label} can no longer be closed. "
    )
    if order:
        return text + f"Books Closed Through leaves stored quarters open: {reclose_steps(order)}"
    return text + "Nothing stored is reopened; `runningtab period status` shows where the books stand"


def _lock_rule_failure(
    quarter: Quarter,
    stored: list[StoredPackage],
    lock: LockState,
    own: StoredPackage | None,
    order: list[Quarter],
) -> str | None:
    """The one lock-versus-record rule `quarter` breaks, if any -- see `_precondition_failures`."""
    before = [p for p in stored if p.quarter < quarter]
    previous = quarter.previous
    previous_entry = _stored_entry(before, previous) if before else None

    if before and previous_entry is None:
        return f"the previous quarter, {previous.label}, has no stored package; quarters close in order"
    if lock.closed_through == quarter.end:
        return _already_stored_message(quarter, own) if own is not None else None
    if own is not None and lock.closed_through is not None and lock.closed_through > quarter.end:
        return _already_closed_message(quarter, own, lock.closed_through)
    if previous_entry is not None:
        required = previous_entry.quarter.end
        if lock.closed_through == required:
            return None
        shown = lock.closed_through.isoformat() if lock.closed_through else "unset"
        text = (
            f"Books Closed Through is {shown}, but {previous.label} is stored through {required.isoformat()}; "
            f"it must be {required.isoformat()} or {quarter.end.isoformat()}"
        )
        if order:
            text += f" -- a reopened quarter must be re-closed first: {reclose_steps(order, then=quarter)}"
        return text
    if lock.closed_through is not None and lock.closed_through > quarter.end:
        return (
            f"Books Closed Through is {lock.closed_through.isoformat()}, already past "
            f"{quarter.label}'s end ({quarter.end.isoformat()})"
        )
    return None


def _precondition_failures(
    quarter: Quarter, today: dt.date, stored: list[StoredPackage], lock: LockState, abbr: str
) -> list[str]:
    """Every precondition `quarter` fails right now, checked against `lock` and what is actually stored.

    A later quarter already being stored only refuses a quarter that was
    itself never stored: quarters close in order, so an unstored quarter
    before a stored one was skipped (in practice, it predates the first
    close ever) and can no longer be closed. A quarter that *was* stored
    before and is being closed again is a reopen, and a reopen of any
    quarter -- not only the latest -- goes ahead under the lock rules below,
    after which every stored quarter after it is re-closed in order (each
    finds the lock at its own previous quarter's end). The never-stored
    check can fire alongside the "already past" check below (a cleared or
    rolled-back lock can be both behind where it should be *and* trailing a
    quarter that is already stored) or entirely on its own.

    "What is stored" for the previous-quarter rule below means stored
    *strictly before* `quarter` -- never `quarter`'s own packages. A
    quarter's own prior close (from before a reopen, or from an
    interrupted store) says nothing about whether some earlier quarter was
    ever closed; counting it would make closing the very first quarter
    ever a second time (after a legitimate reopen) fail with "the previous
    quarter has no stored package", which is true of every quarter and
    tells the person nothing they can act on.

    When nothing is stored before this quarter (the very first close
    ever, including a reopened-and-reclosed first quarter), the lock only
    has to not already be past this quarter -- the old, loose rule. Once
    something is stored before it, the rule tightens: `closed_through`
    must be exactly the previous quarter's own close date (proving nothing
    was reopened since) or exactly this quarter's end (recovery from a
    lock that succeeded but whose store did not). Anything else -- unset,
    or pointing at some other date entirely -- means a quarter somewhere
    was reopened and never re-closed, and the refusal names the order to
    re-close in.

    Either way, a lock at or past `quarter.end` is further refused if
    `quarter` already has a complete, stored package of its own: at
    `quarter.end` that is a finished close (or an interrupted re-close),
    not an interrupted upload to retry, and `runningtab period status` is what
    checks a finished close still matches the ledger; past it, the quarter
    is simply closed and has to be reopened first.
    """
    failures: list[str] = []
    if not quarter.has_ended(today):
        failures.append(
            f"{quarter.label} has not ended: today is {today.isoformat()}, it ends {quarter.end.isoformat()}"
        )

    own = _stored_entry(stored, quarter)
    order = reclose_order(lock.closed_through, (p.quarter for p in stored))
    later = sorted(p.quarter for p in stored if p.quarter > quarter)
    if later and own is None:
        failures.append(_never_closed_message(abbr, quarter, later[0], order))

    lock_failure = _lock_rule_failure(quarter, stored, lock, own, order)
    if lock_failure is not None:
        failures.append(lock_failure)

    if lock.bypass_role:
        failures.append(f"role_allowed_for_frozen_entries is {lock.bypass_role!r}; it must be empty before closing")
    return failures


def _readiness_failures(blockers: list[Blocker], package: Package) -> list[str]:
    failures = [f"{b.kind} {b.doctype} {b.name}: {b.detail}" for b in blockers]
    failures.extend(
        f"bank difference on {line.label} ({line.number}): {money(line.difference)}"
        for line in package.bank
        if line.difference != 0
    )
    return failures


def _differing_paths(before: dict[str, bytes], after: dict[str, bytes]) -> list[str]:
    return sorted(path for path in {*before, *after} if before.get(path) != after.get(path))


_GENERAL_LEDGER = "general-ledger.csv"


def _csv_rows(data: bytes) -> set[tuple[str, ...]]:
    return {tuple(row) for row in csv.reader(io.StringIO(data.decode("utf-8")))}


def _differing_vouchers(before: dict[str, bytes], after: dict[str, bytes]) -> list[str]:
    """Every voucher ("Journal Entry ACC-JV-2026-00012") with a row in one build's general ledger and not the other's.

    Read from `general-ledger.csv`'s own `voucher_type` and `voucher_no`
    columns; a row too short to carry them names no voucher (the file is
    still named as differing).
    """
    old, new = before.get(_GENERAL_LEDGER, b""), after.get(_GENERAL_LEDGER, b"")
    if old == new:
        return []
    header = next(csv.reader(io.StringIO((new or old).decode("utf-8"))), [])
    if "voucher_type" not in header or "voucher_no" not in header:
        return []
    kind, name = header.index("voucher_type"), header.index("voucher_no")
    changed = _csv_rows(old) ^ _csv_rows(new)
    return sorted({f"{row[kind]} {row[name]}" for row in changed if len(row) > max(kind, name)})


def reopen_step(quarter: Quarter) -> str:
    """How a person reopens `quarter`: the one ERPNext edit that does it."""
    return f"reopen {quarter.label} (move Books Closed Through back to {quarter.previous.end.isoformat()} in ERPNext)"


def _held_for_review_message(
    abbr: str, quarter: Quarter, differing: list[str], vouchers: list[str], later: list[Quarter], *, reclosing: bool
) -> str:
    text = (
        f"{abbr} {quarter.label} stays locked through {quarter.end.isoformat()} with no new stored package: the "
        "ledger changed between the check and the lock, in " + ", ".join(differing)
    )
    if vouchers:
        text += f" (GL entries of {', '.join(vouchers)})"
    command = f"`runningtab period close --quarter {quarter.label}`"
    if reclosing:
        # A re-run with the lock at quarter.end is refused as "already has a
        # stored package" (the earlier revision), so there is no retry here.
        # The quarter is locked, so nothing can be reversed in it until it is
        # reopened: reopen comes first.
        text += (
            f". Check what posted; then {reopen_step(quarter)}, reverse anything that should not be in "
            f"{quarter.label}, and run {command} again"
        )
    else:
        text += (
            f". Check what posted. If it belongs in {quarter.label}, run {command} again to re-check and store "
            f"what is now locked. If it should not be in {quarter.label}, {reopen_step(quarter)}, reverse it, "
            f"and run {command} again"
        )
    if later:
        text += f"; then {reclose_steps(later)}"
    return text


def close_quarter(steps: CloseSteps, quarter: Quarter, abbr: str) -> CloseOutcome:
    """Run the close ceremony for `quarter`, in the spec's exact order, and return where it landed.

    Every stage before the lock leaves nothing changed and names every
    failing item in that stage, not just the first; a failure at or after
    the lock leaves the lock in place -- there is no rollback -- and says so
    by name. `abbr` is not used to look anything up (every `steps` callable
    is already scoped to one company/bucket by the CLI); every refusal
    names it, so a person reading a raised message never has to guess which
    company's close failed.
    """
    # 1. Preconditions.
    today = steps.today()
    stored = steps.stored_packages()
    lock = steps.read_lock()
    failures = _precondition_failures(quarter, today, stored, lock, abbr)
    if failures:
        _refuse(abbr, quarter, failures)

    # 2. Readiness, and 3. Build (the same read the bank-difference check
    # in readiness needs -- see the module docstring on why `build` runs
    # once here rather than being deferred to the cross-check stage).
    # Blockers are read first so a `build` refusal (an unbalanced trial
    # balance, say) is reported together with them in one refusal, rather
    # than hiding whatever else was already wrong.
    blockers = steps.read_blockers()
    try:
        built = steps.build()
    except PeriodError as exc:
        _refuse(abbr, quarter, [f"{b.kind} {b.doctype} {b.name}: {b.detail}" for b in blockers] + [str(exc)])
    failures = _readiness_failures(blockers, built.package)
    if failures:
        _refuse(abbr, quarter, failures)

    # 3. Cross-check.
    mismatches = steps.crosscheck(
        built.package, built.trial_balance, built.receivables, built.payables, quarter, built.manifest_facts.fiscal_year
    )
    if mismatches:
        _refuse(abbr, quarter, mismatches)

    # 4. Lock.
    if lock.closed_through == quarter.end:
        # Recovery: the lock already succeeded, only the store did not.
        # The newest Version is the only one that can confirm that --
        # anything else (nothing recorded, or the newest Version disagreeing
        # with the live field) means the field and its own audit trail have
        # drifted apart, and this refuses rather than trust either alone.
        history = steps.lock_history()
        if not history or history[0].new != quarter.end:
            newest = history[0].new.isoformat() if history and history[0].new else "unset"
            raise PeriodError(
                f"{abbr} {quarter.label}: Books Closed Through is already {quarter.end.isoformat()} but the "
                f"newest Version does not confirm it (newest recorded value: {newest})"
            )
        change = history[0]
    else:
        change = steps.set_closed_through(quarter.end)
    reclosing = _stored_entry(stored, quarter) is not None
    later = tuple(sorted({p.quarter for p in stored if p.quarter > quarter}))
    steps.locked(Held(change, reclosing, later))

    # 5. Rebuild and compare.
    rebuilt = steps.build()
    if rebuilt.package.files != built.package.files:
        raise HeldForReview(
            _held_for_review_message(
                abbr,
                quarter,
                _differing_paths(built.package.files, rebuilt.package.files),
                _differing_vouchers(built.package.files, rebuilt.package.files),
                list(later),
                reclosing=reclosing,
            )
        )

    # 6. Store.
    def build_manifest(revision: int) -> bytes:
        facts = dataclasses.replace(
            rebuilt.manifest_facts, draft=False, closed_by=change.by, lock_version=change.version, revision=revision
        )
        return render_manifest(facts, rebuilt.package.files)

    prefix = steps.upload(rebuilt.package.files, build_manifest)
    return CloseOutcome(prefix=prefix, lock=change, package=rebuilt.package)
