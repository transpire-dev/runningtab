"""The cursor, the dedup set, and the per-resource version high-water mark."""

from __future__ import annotations

import datetime as dt
import pathlib

import pytest

from runningtab.mercury.state import (
    RETENTION,
    DryRunState,
    IngestState,
    NoCursorError,
    StaleCursorError,
    StateIdentityError,
    default_state_path,
)

NOW = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
def state(tmp_path: pathlib.Path):
    with IngestState(tmp_path / "nested" / "mercury.sqlite3") as ingest_state:
        yield ingest_state


def test_the_state_file_and_its_directory_are_created(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "a" / "b" / "mercury.sqlite3"
    with IngestState(path):
        pass
    assert path.exists()


def test_a_fresh_state_has_no_cursor(state: IngestState) -> None:
    assert state.cursor() is None
    with pytest.raises(NoCursorError):
        state.require_resumable(NOW)


def test_a_cursor_survives_reopening(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "mercury.sqlite3"
    with IngestState(path) as first:
        first.record_cursor("evt_0006", NOW)
    with IngestState(path) as second:
        cursor = second.require_resumable(NOW)
    assert cursor.event_id == "evt_0006"
    assert cursor.synced_at == NOW


def test_recording_a_cursor_twice_moves_it_rather_than_adding_a_row(state: IngestState) -> None:
    state.record_cursor("evt_0001", NOW)
    state.record_cursor("evt_0009", NOW + dt.timedelta(hours=1))
    cursor = state.cursor()
    assert cursor is not None
    assert cursor.event_id == "evt_0009"


def test_an_empty_log_records_a_real_cursor_rather_than_nothing(state: IngestState) -> None:
    # A None event id means "read the log from the beginning", which is a
    # different state from "never snapshotted" and must not raise.
    state.record_cursor(None, NOW)
    assert state.require_resumable(NOW).event_id is None


def test_a_cursor_inside_the_retention_window_resumes(state: IngestState) -> None:
    state.record_cursor("evt_0006", NOW - RETENTION + dt.timedelta(days=1))
    assert state.require_resumable(NOW).event_id == "evt_0006"


def test_a_cursor_past_retention_refuses_to_resume_into_a_hole(state: IngestState) -> None:
    state.record_cursor("evt_0006", NOW - RETENTION - dt.timedelta(days=1))
    with pytest.raises(StaleCursorError) as exc:
        state.require_resumable(NOW)
    assert "re-snapshot" in str(exc.value)
    assert "90" in str(exc.value)


def test_event_ids_are_deduplicated(state: IngestState) -> None:
    assert state.has_seen("evt_0002") is False
    state.mark_seen("evt_0002", NOW)
    assert state.has_seen("evt_0002") is True


def test_marking_the_same_event_twice_is_harmless(state: IngestState) -> None:
    state.mark_seen("evt_0002", NOW)
    state.mark_seen("evt_0002", NOW + dt.timedelta(minutes=5))
    assert state.seen_count() == 1


def test_a_resource_version_never_moves_backwards(state: IngestState) -> None:
    state.record_version("txn_wire_payroll", 2)
    state.record_version("txn_wire_payroll", 1)
    assert state.version_of("txn_wire_payroll") == 2


def test_an_unknown_resource_has_no_version(state: IngestState) -> None:
    assert state.version_of("txn_nothing") is None


def test_a_dry_run_remembers_within_the_run(state: IngestState) -> None:
    dry = DryRunState(state)
    dry.mark_seen("evt_0002", NOW)
    dry.record_version("txn_x", 3)
    # Answers the question a dry run is actually asked -- what would a real run
    # do -- rather than a slightly different one.
    assert dry.has_seen("evt_0002")
    assert dry.version_of("txn_x") == 3


def test_a_dry_run_writes_nothing_through(state: IngestState) -> None:
    dry = DryRunState(state)
    dry.mark_seen("evt_0002", NOW)
    dry.record_version("txn_x", 3)
    dry.record_cursor("evt_0009", NOW)
    assert state.has_seen("evt_0002") is False
    assert state.version_of("txn_x") is None
    assert state.cursor() is None


def test_a_dry_run_reads_through_to_what_is_already_recorded(state: IngestState) -> None:
    state.mark_seen("evt_0001", NOW)
    state.record_version("txn_x", 5)
    dry = DryRunState(state)
    assert dry.has_seen("evt_0001")
    assert dry.version_of("txn_x") == 5
    # A buffered version never lowers the recorded high-water mark.
    dry.record_version("txn_x", 2)
    assert dry.version_of("txn_x") == 5


def test_the_default_state_path_follows_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert (
        default_state_path("Example", "sandbox", "test") == tmp_path / "example-books" / "mercury-sandbox-test.sqlite3"
    )


def test_the_default_state_path_falls_back_to_local_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    path = default_state_path("Example", "production", "books")
    assert path.parts[-3:] == ("state", "example-books", "mercury-production-books.sqlite3")


def test_each_deployment_and_ledger_gets_its_own_default_file(monkeypatch: pytest.MonkeyPatch) -> None:
    # A cursor is a place in one bank's log, and the drafts it accounts for are
    # in one ledger. One shared file makes a snapshot taken against the sandbox
    # and a test site look like a place the production feed can resume from.
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    paths = {
        default_state_path(books, env, target)
        for books in ("Example", "Other")
        for env in ("sandbox", "production")
        for target in ("test", "books")
    }
    assert len(paths) == 8


def test_a_state_file_refuses_an_ingest_it_was_not_written_by(tmp_path: pathlib.Path) -> None:
    # An explicit --state path is the way around the namespaced default, so the
    # binding is recorded in the file rather than left to its name. Resuming a
    # sandbox cursor as a production one would start the next sync after an
    # event the bank never emitted, and skip everything before it.
    path = tmp_path / "mercury.sqlite3"
    with IngestState(path, identity="sandbox/test"):
        pass
    with pytest.raises(StateIdentityError) as exc:
        IngestState(path, identity="production/books")
    assert "sandbox/test" in str(exc.value)
    assert "production/books" in str(exc.value)


def test_a_state_file_reopens_under_the_identity_that_wrote_it(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "mercury.sqlite3"
    with IngestState(path, identity="sandbox/test") as first:
        first.record_cursor("evt_0006", NOW)
    with IngestState(path, identity="sandbox/test") as second:
        assert second.require_resumable(NOW).event_id == "evt_0006"


def test_an_empty_file_is_claimed_by_the_first_ingest_to_name_itself(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "mercury.sqlite3"
    with IngestState(path):
        pass
    with IngestState(path, identity="sandbox/test") as claimed:
        claimed.record_cursor("evt_0006", NOW)
    with pytest.raises(StateIdentityError):
        IngestState(path, identity="production/books")


def test_a_populated_file_naming_no_deployment_is_refused_rather_than_adopted(tmp_path: pathlib.Path) -> None:
    # Claiming it would bless whatever cursor it holds as this deployment's,
    # which is the confusion the binding exists to prevent -- and a file that
    # predates the binding is exactly one that may have been written against
    # another deployment.
    path = tmp_path / "mercury.sqlite3"
    with IngestState(path) as unbound:
        unbound.record_cursor("evt_0006", NOW)
    with pytest.raises(StateIdentityError) as exc:
        IngestState(path, identity="sandbox/test")
    assert "snapshot into a fresh file" in str(exc.value)


def test_a_file_holding_only_applied_events_is_populated_too(tmp_path: pathlib.Path) -> None:
    # The cursor is not the only thing worth being wrong about: a seen-event
    # set from another deployment silently deduplicates events this one needs.
    path = tmp_path / "mercury.sqlite3"
    with IngestState(path) as unbound:
        unbound.mark_seen("evt_0006", NOW)
    with pytest.raises(StateIdentityError):
        IngestState(path, identity="sandbox/test")
