"""Where the ingest keeps its place in the event log.

Three things live here and nothing else: the cursor, the set of event ids
already applied, and the highest `resourceVersion` seen per resource. SQLite
rather than a JSON document because the applier commits after each event -- a
run that dies halfway keeps everything it already applied, instead of losing a
batch that was going to be written out at the end.

What deliberately does **not** live here is which ERPNext document was posted
for a Mercury transaction. That lookup is the `mercury_transaction_id` custom
field on the document itself, so losing this file can never cause a double
post: it costs a re-read, not a duplicate entry.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import pathlib
import sqlite3

# Mercury retains events for 90 days. A cursor older than that may point into a
# hole the log can no longer fill, and resuming from it would skip whatever
# happened in the gap without anything looking wrong.
RETENTION = dt.timedelta(days=90)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_identity (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    identity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingest_cursor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    event_id TEXT,
    synced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_event (
    event_id TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resource_version (
    resource_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL
);
"""


class NoCursorError(RuntimeError):
    """Nothing has been snapshotted yet, so there is no place in the log to resume from."""


class StaleCursorError(RuntimeError):
    """The cursor is older than Mercury's retention window; re-snapshot rather than resume."""


class StateIdentityError(RuntimeError):
    """This state file belongs to a different Mercury deployment or ledger."""


@dataclasses.dataclass(frozen=True)
class Cursor:
    """Where the last run stopped, and when it stopped there.

    `synced_at` is our clock, not the event's. The retention rule needs to know
    how long ago we last read the log, which is a fact about us; the event's own
    timestamp field was never recorded by the investigation, and guessing its
    name to answer a question we can already answer would be a worse trade.
    """

    event_id: str | None
    synced_at: dt.datetime


def default_state_path(books_name: str, env: str, target: str) -> pathlib.Path:
    """`$XDG_STATE_HOME/<name>-books/mercury-<env>-<target>.sqlite3`, or the `~/.local/state` fallback.

    One file per set of books, Mercury deployment and ledger, never one shared
    file. A cursor is a place in one bank's event log, and the rows it
    accounts for are in one ledger; a snapshot taken against the sandbox and a
    test site would otherwise look to the production feed like a place it can
    resume from, and everything before that cursor would be skipped with
    nothing looking wrong. `books_name` is `books.toml`'s name, lowercased.
    """
    root = os.environ.get("XDG_STATE_HOME")
    base = pathlib.Path(root) if root else pathlib.Path.home() / ".local" / "state"
    return base / f"{books_name.lower()}-books" / f"mercury-{env}-{target}.sqlite3"


class IngestState:
    """The ingest's place in the log. One writer, committed per call.

    `identity` names the Mercury deployment and ledger this file belongs to,
    as `"<env>/<target>"`. It is recorded when an empty file is first used and
    checked on every later open, because `--state` can point at any file the
    namespaced default path was arranged to keep apart. A file that already
    holds a cursor but names no deployment is refused rather than adopted:
    there is nothing to check against, and a file predating the binding is
    exactly one that may have been written by another deployment. Omitting the
    identity -- which only tests do -- binds nothing and checks nothing.
    """

    def __init__(self, path: pathlib.Path, *, identity: str | None = None) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(_SCHEMA)
        self._db.commit()
        if identity is not None:
            self._bind(identity)

    def _is_empty(self) -> bool:
        """Whether this file has nothing to lose: no cursor, no applied events, no versions."""
        return all(
            self._db.execute(query).fetchone() is None
            for query in (
                "SELECT 1 FROM ingest_cursor WHERE id = 1",
                "SELECT 1 FROM seen_event LIMIT 1",
                "SELECT 1 FROM resource_version LIMIT 1",
            )
        )

    def _bind(self, identity: str) -> None:
        """Claim an empty file, or refuse one that belongs to -- or might belong to -- another ingest."""
        row = self._db.execute("SELECT identity FROM ingest_identity WHERE id = 1").fetchone()
        if row is not None and row[0] != identity:
            self.close()
            raise StateIdentityError(
                f"{self.path} holds the state of {row[0]} and this run is {identity}; "
                "a cursor is a place in one bank's log and its rows are in one ledger, so resuming "
                "from the wrong file would skip everything before an event this deployment never emitted"
            )
        if row is not None:
            return
        if not self._is_empty():
            # Claiming a populated file would bless whatever cursor it holds as
            # this deployment's, which is the confusion the binding exists to
            # prevent -- and a file predating the binding is exactly one that
            # may have been written against another deployment.
            self.close()
            raise StateIdentityError(
                f"{self.path} already holds ingest state but names no deployment, so there is "
                f"nothing to check {identity} against; snapshot into a fresh file rather than "
                "adopting one whose bank and ledger cannot be established"
            )
        self._db.execute("INSERT INTO ingest_identity (id, identity) VALUES (1, ?)", (identity,))
        self._db.commit()

    def __enter__(self) -> IngestState:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the database handle."""
        self._db.close()

    # --- cursor -----------------------------------------------------------

    def cursor(self) -> Cursor | None:
        """Where the last snapshot or sync left off, or None if there has never been one."""
        row = self._db.execute("SELECT event_id, synced_at FROM ingest_cursor WHERE id = 1").fetchone()
        if row is None:
            return None
        return Cursor(event_id=row[0], synced_at=dt.datetime.fromisoformat(row[1]))

    def record_cursor(self, event_id: str | None, at: dt.datetime) -> None:
        """Move the cursor to `event_id` as of `at`.

        A None `event_id` is a real state, not a missing one: an account whose
        event log is empty has been read completely, and the next sync starts
        from the beginning of the log rather than refusing.
        """
        self._db.execute(
            "INSERT INTO ingest_cursor (id, event_id, synced_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET event_id = excluded.event_id, synced_at = excluded.synced_at",
            (event_id, at.isoformat()),
        )
        self._db.commit()

    def require_resumable(self, now: dt.datetime) -> Cursor:
        """The cursor, if resuming from it can still be complete.

        Raises rather than resuming into a hole: past the retention window the
        log cannot tell us what we missed, and the honest recovery is another
        snapshot.
        """
        cursor = self.cursor()
        if cursor is None:
            raise NoCursorError("no cursor recorded; run `runningtab mercury snapshot` before syncing")
        age = now - cursor.synced_at
        if age > RETENTION:
            raise StaleCursorError(
                f"last sync was {age.days} days ago and Mercury retains events for {RETENTION.days}; "
                "the cursor may point into a gap the log can no longer fill -- re-snapshot instead of resuming"
            )
        return cursor

    # --- dedup ------------------------------------------------------------

    def has_seen(self, event_id: str) -> bool:
        """Whether this event id has already been applied. Mercury delivers at-least-once."""
        return self._db.execute("SELECT 1 FROM seen_event WHERE event_id = ?", (event_id,)).fetchone() is not None

    def mark_seen(self, event_id: str, at: dt.datetime) -> None:
        """Record an event id as applied, whether it changed anything or not.

        An event skipped by the version guard is still recorded: it has been
        considered, and reconsidering it on the next delivery would reach the
        same conclusion more slowly.
        """
        self._db.execute(
            "INSERT INTO seen_event (event_id, applied_at) VALUES (?, ?) ON CONFLICT(event_id) DO NOTHING",
            (event_id, at.isoformat()),
        )
        self._db.commit()

    def seen_count(self) -> int:
        """How many event ids have been recorded. For `runningtab mercury status`."""
        return int(self._db.execute("SELECT COUNT(*) FROM seen_event").fetchone()[0])

    # --- ordering ---------------------------------------------------------

    def version_of(self, resource_id: str) -> int | None:
        """The highest `resourceVersion` applied for a resource, or None."""
        row = self._db.execute("SELECT version FROM resource_version WHERE resource_id = ?", (resource_id,)).fetchone()
        return None if row is None else int(row[0])

    def record_version(self, resource_id: str, version: int) -> None:
        """Raise a resource's high-water mark. Never lowers it: a late replay must not reopen the past."""
        self._db.execute(
            "INSERT INTO resource_version (resource_id, version) VALUES (?, ?) "
            "ON CONFLICT(resource_id) DO UPDATE SET version = MAX(version, excluded.version)",
            (resource_id, version),
        )
        self._db.commit()


class DryRunState:
    """An `IngestState` that remembers for the length of one run and then forgets.

    So a dry run answers the question people actually ask it -- "what would a
    real run do?" -- rather than a slightly different one. Buffering the writes
    here instead of branching on `dry_run` at every write means the applier
    takes the same path either way; the only thing a dry run skips is the
    ledger write itself.
    """

    def __init__(self, inner: IngestState) -> None:
        self._inner = inner
        self._seen: set[str] = set()
        self._versions: dict[str, int] = {}

    def cursor(self) -> Cursor | None:
        return self._inner.cursor()

    def require_resumable(self, now: dt.datetime) -> Cursor:
        return self._inner.require_resumable(now)

    def record_cursor(self, event_id: str | None, at: dt.datetime) -> None:
        """Deliberately nothing: a dry run must not move the real cursor."""

    def has_seen(self, event_id: str) -> bool:
        return event_id in self._seen or self._inner.has_seen(event_id)

    def mark_seen(self, event_id: str, at: dt.datetime) -> None:
        self._seen.add(event_id)

    def seen_count(self) -> int:
        return self._inner.seen_count() + len(self._seen)

    def version_of(self, resource_id: str) -> int | None:
        buffered = self._versions.get(resource_id)
        recorded = self._inner.version_of(resource_id)
        if buffered is None:
            return recorded
        return buffered if recorded is None else max(buffered, recorded)

    def record_version(self, resource_id: str, version: int) -> None:
        self._versions[resource_id] = max(self._versions.get(resource_id, version), version)


type State = IngestState | DryRunState
"""What the applier writes its place to: the real file, or a run-scoped buffer."""
