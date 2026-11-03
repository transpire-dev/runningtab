"""Books Closed Through: read it, read who moved it and when, and move it forward only.

Frappe records every field change to a document as a `Version`
(`frappe/core/doctype/version/version.py`): its `data` field is a JSON string
whose `"changed"` key lists `[fieldname, old, new]` triples for every field
that save touched. `lock_history` filters that down to `accounts_frozen_till_date`
alone -- the field the fence in `runningtab.period.fence` protects -- and ignores
every other change a Company save might carry, `role_allowed_for_frozen_entries`
included.

`get_diff` (same file) writes a changed Date field's old/new through
`get_formatted()` -> `formatdate()` (`frappe/utils/data.py`) before it ever
reaches `data` -- i.e. in the *site's* `date_format` system setting, not ISO.
A live site can be, and by default is, set to `yyyy-mm-dd`, but nothing
guarantees it stays that way, so `_parse_version_date` tries ISO first and
falls back to every other format System Settings' `date_format` select
offers (`frappe/core/doctype/system_settings/system_settings.json`), reading
the site's actual setting only if two of those formats disagree on the same
text. `Company` itself is not affected: an ordinary document read returns
the raw stored field value, never a formatted one, so `read_lock` still
parses it as plain ISO.

The Version doctype ships permission to `System Manager` and `Administrator`
only (`frappe/core/doctype/version/version.json`); neither is a role the books
reader inherits, so `runningtab.access.READER_GRANTS` carries an explicit read
grant on `Version` to let `status`/`health` show who reopened a quarter.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from typing import Any, Protocol

from runningtab.period import PeriodError
from runningtab.period.fence import LOCK_FIELDS
from runningtab.site import Profile

COMPANY = "Company"
_SYSTEM_SETTINGS = "System Settings"

# System Settings' own `date_format` options (system_settings.json), less
# `yyyy-mm-dd` -- that one is ISO, and `_iso_date` already covers it.
_OTHER_DATE_FORMATS: dict[str, str] = {
    "dd-mm-yyyy": "%d-%m-%Y",
    "mm-dd-yyyy": "%m-%d-%Y",
    "dd/mm/yyyy": "%d/%m/%Y",
    "mm/dd/yyyy": "%m/%d/%Y",
    "dd.mm.yyyy": "%d.%m.%Y",
}


class _Reader(Protocol):
    """What `read_lock` and `lock_history` need: read Company, read its Version history."""

    def get(self, doctype: str, name: str) -> Any: ...
    def iter_all(self, doctype: str, **kwargs: Any) -> Any: ...


class _Admin(Protocol):
    """What `set_closed_through` needs: the reader's slice, plus writing Company."""

    def get(self, doctype: str, name: str) -> Any: ...
    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any: ...
    def iter_all(self, doctype: str, **kwargs: Any) -> Any: ...


@dataclasses.dataclass(frozen=True)
class LockState:
    """Company's close lock, as it stands right now."""

    closed_through: dt.date | None
    bypass_role: str | None


@dataclasses.dataclass(frozen=True)
class LockChange:
    """One Version's edit to `accounts_frozen_till_date`, decoded.

    `old`/`new` are the decoded dates; `old_text`/`new_text` are exactly what
    Frappe recorded, unparsed -- so a person can always see the raw text
    behind a decode, whether or not it parsed cleanly.
    """

    version: str
    at: dt.datetime
    by: str
    old: dt.date | None
    new: dt.date | None
    old_text: str
    new_text: str


def _parse_date(value: Any) -> dt.date | None:
    """A Company field value as Frappe's JSON client hands it back: a date string, empty string, or null.

    An ordinary document read is never passed through `get_formatted()` --
    that only happens inside `Version.get_diff` -- so this is plain ISO,
    unlike `_parse_version_date` below.
    """
    if not value:
        return None
    return dt.date.fromisoformat(str(value))


def read_lock(client: _Reader, company: str) -> LockState:
    """Company's current `accounts_frozen_till_date` and `role_allowed_for_frozen_entries`."""
    doc = client.get(COMPANY, company)
    return LockState(closed_through=_parse_date(doc.get(LOCK_FIELDS[0])), bypass_role=doc.get(LOCK_FIELDS[1]) or None)


_LOCK_FIELD_LABELS: dict[str, str] = {
    LOCK_FIELDS[0]: "Books Closed Through",
    LOCK_FIELDS[1]: "the frozen-entries role",
}


def read_visible_lock(client: _Reader, company: str, profile: Profile) -> LockState:
    """`read_lock`, but refuses by name instead of reporting a false "unset" when this credential cannot see a
    lock field at all.

    Both `LOCK_FIELDS` sit at Company permlevel 1 (`runningtab.period.fence.install_fence`). Frappe's
    `apply_fieldlevel_read_permissions` *deletes* a permlevel-1 field from a document response the
    credential cannot read rather than returning it as null, so a credential never (re-)provisioned since
    its grant was added -- or one whose `access provision` installed the fence and then failed -- gets a
    doc with no `accounts_frozen_till_date`/`role_allowed_for_frozen_entries` key at all, indistinguishable,
    to plain `dict.get`, from Company legitimately having neither set yet. `missing_grants_report` cannot
    catch this either: it explicitly skips every grant above permlevel 0 (`runningtab.access.missing_grants_report`'s
    own docstring), since `frappe.client.has_permission` has no permlevel of its own to ask about. Presence of
    the key is the only thing that tells the two cases apart.

    `profile` is the identity the caller reads as -- the reader for `period status`/`health`, the bot for the
    Mercury ingest -- and names whose re-provision fixes it.
    """
    doc = client.get(COMPANY, company)
    missing = [field for field in LOCK_FIELDS if field not in doc]
    if missing:
        labels = " and ".join(_LOCK_FIELD_LABELS[field] for field in missing)
        raise PeriodError(
            f"the {profile.value} credential cannot see {labels} on {company} (Company, permlevel 1); run "
            f"`runningtab access provision --profile {profile.value}` with RUNNINGTAB_ADMIN_TOKEN set"
        )
    return LockState(closed_through=_parse_date(doc.get(LOCK_FIELDS[0])), bypass_role=doc.get(LOCK_FIELDS[1]) or None)


def _raw_text(value: Any) -> str:
    return str(value) if value else ""


def _iso_date(text: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def _formatted_dates(text: str) -> dict[str, dt.date]:
    """{format name: parsed date} for every one of Frappe's other date_format options that matches `text`."""
    matches: dict[str, dt.date] = {}
    for name, pattern in _OTHER_DATE_FORMATS.items():
        try:
            matches[name] = dt.datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return matches


def _site_date_format(client: _Reader) -> str | None:
    """The site's configured `date_format`, or None if it cannot be read.

    Best-effort only, and only consulted to disambiguate a Version's
    formatted text when more than one of Frappe's date formats parses it to
    a different date. A reader credential may hold no grant on System
    Settings at all; that failure (or any other) just means the format
    could not be determined, not that reading history fails outright.
    """
    try:
        settings = client.get(_SYSTEM_SETTINGS, _SYSTEM_SETTINGS)
    except Exception:
        return None
    value = settings.get("date_format") if isinstance(settings, dict) else None
    return str(value) if value else None


def _parse_version_date(client: _Reader, version: str, label: str, text: str) -> dt.date | None:
    """One raw old/new value from a Version's `data`, parsed defensively.

    ISO is tried first -- the common case, and a live site's default. When
    that fails, every other format System Settings' `date_format` offers is
    tried; if exactly one produces a date, or several agree, that date wins.
    If more than one disagrees, the site's actual `date_format` (read fresh,
    not cached) breaks the tie; if that cannot be read either, or the text
    matches nothing at all, this refuses by name rather than guess.
    """
    if not text:
        return None
    iso = _iso_date(text)
    if iso is not None:
        return iso
    matches = _formatted_dates(text)
    distinct = set(matches.values())
    if len(distinct) == 1:
        return distinct.pop()
    if not distinct:
        raise PeriodError(f"Version {version}: {label} {text!r} could not be parsed as a date")
    site_format = _site_date_format(client)
    if site_format in matches:
        return matches[site_format]
    raise PeriodError(
        f"Version {version}: {label} {text!r} is ambiguous ({', '.join(sorted(matches))}) "
        "and the site's date_format could not be determined"
    )


def _date_change(row: dict[str, Any]) -> tuple[Any, Any] | None:
    """The `[old, new]` pair for the lock date field in one Version row's `data`, or None if it was not touched."""
    data = json.loads(row["data"])
    for fieldname, old, new in data.get("changed", []):
        if fieldname == LOCK_FIELDS[0]:
            return old, new
    return None


def lock_history(client: _Reader, company: str) -> list[LockChange]:
    """Every Version of `company` that touched `accounts_frozen_till_date`, newest first.

    Ordering is the server's: requested via `order_by`, not re-sorted here.
    """
    rows = client.iter_all(
        "Version",
        fields=["name", "owner", "creation", "data"],
        filters=[["ref_doctype", "=", COMPANY], ["docname", "=", company]],
        order_by="creation desc, name desc",
    )
    history: list[LockChange] = []
    for row in rows:
        change = _date_change(row)
        if change is None:
            continue
        old_raw, new_raw = change
        version = str(row["name"])
        old_text, new_text = _raw_text(old_raw), _raw_text(new_raw)
        history.append(
            LockChange(
                version=version,
                at=dt.datetime.fromisoformat(str(row["creation"])),
                by=str(row["owner"]),
                old=_parse_version_date(client, version, "old value", old_text),
                new=_parse_version_date(client, version, "new value", new_text),
                old_text=old_text,
                new_text=new_text,
            )
        )
    return history


def set_closed_through(admin: _Admin, company: str, date: dt.date) -> LockChange:
    """Move Books Closed Through forward to `date`, refusing to move it backwards.

    Reads the current value first, so a backwards request is refused before
    anything is written. Confirmation never depends on parsing a Version's
    formatted text back to a date -- that text is in the site's
    `date_format`, not necessarily ISO, and comparing it to `date` is exactly
    the bug this guards against. Instead: `read_lock` afterwards must show
    `date` in Company's own (always-ISO) field, and a Version that did not
    exist before this call must now touch `accounts_frozen_till_date` --
    that Version, identified by name, is the one returned. Either check
    failing refuses rather than report a change that cannot be shown to have
    happened.
    """
    current = read_lock(admin, company)
    if current.closed_through is not None and date < current.closed_through:
        raise PeriodError(
            f"Books Closed Through cannot move backwards: {company} is closed through "
            f"{current.closed_through.isoformat()}, refusing {date.isoformat()}"
        )
    before = {change.version for change in lock_history(admin, company)}
    admin.update(COMPANY, company, {LOCK_FIELDS[0]: date.isoformat()})

    after = read_lock(admin, company)
    if after.closed_through != date:
        shown = after.closed_through.isoformat() if after.closed_through else "unset"
        raise PeriodError(
            f"{company}'s Books Closed Through was set to {date.isoformat()} but reading it back shows "
            f"{shown} -- refusing to report success"
        )

    for change in lock_history(admin, company):
        if change.version not in before:
            return change
    raise PeriodError(
        f"{company}'s Books Closed Through was updated to {date.isoformat()} but left no Version audit trail"
    )
