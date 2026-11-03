"""Books Closed Through: the current lock, who moved it and when, and refusing to move it backwards.

`lock_history` decodes real Frappe `Version` rows: `data` is a JSON string
whose `"changed"` key lists `[fieldname, old, new]` triples for every field a
save touched (`frappe/core/doctype/version/version.py`). These fixtures are
shaped exactly like that -- a `data` string, not a pre-parsed dict -- so a
change to how this module reads `data` shows up here first.

Crucially, `get_diff` in that same file writes a changed Date field's old/new
through `get_formatted()` -> `formatdate()` (frappe/utils/data.py), i.e. in
the site's `date_format` system setting, not ISO. Several fixtures below use
non-ISO text for exactly that reason.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
from icedcoffee_sync.frappe.errors import PermissionError_

from runningtab.period import PeriodError
from runningtab.period.lock import LockChange, LockState, lock_history, read_lock, read_visible_lock, set_closed_through
from runningtab.site import Profile

COMPANY = "Transpire"


def _version(name: str, *, owner: str, creation: str, changed: list[list[Any]]) -> dict[str, Any]:
    return {
        "name": name,
        "owner": owner,
        "creation": creation,
        "data": json.dumps({"changed": changed}),
    }


class _Site:
    """A tiny stand-in ERPNext: a Company document, its Version history, and System Settings.

    `update` models Frappe's own version-tracking: a save that changes a
    field creates exactly one new Version row, given here as
    `version_created_by_update` and inserted at call time -- so a test can
    tell "a Version that existed before this call" from "one this call
    created" the same way `set_closed_through` has to. `update_lands=False`
    models a write the server silently drops (permlevel reset, the same
    failure `runningtab.period.fence` proves against): the Company document
    itself never changes, though `update_calls` still records the attempt.
    """

    def __init__(
        self,
        *,
        company: dict[str, Any] | None = None,
        versions: list[dict[str, Any]] | None = None,
        version_created_by_update: dict[str, Any] | None = None,
        update_lands: bool = True,
        system_settings: dict[str, Any] | None = None,
        system_settings_error: bool = False,
    ) -> None:
        self.company: dict[str, Any] = {"name": COMPANY, **(company or {})}
        self.versions: list[dict[str, Any]] = list(versions or [])
        self._version_created_by_update = version_created_by_update
        self._update_lands = update_lands
        self.system_settings: dict[str, Any] = dict(system_settings or {})
        self._system_settings_error = system_settings_error
        self.update_calls: list[dict[str, Any]] = []
        self.iter_calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, doctype: str, name: str) -> Any:
        if doctype == "Company":
            assert name == COMPANY
            return dict(self.company)
        if doctype == "System Settings":
            if self._system_settings_error:
                raise PermissionError_("not permitted")
            assert name == "System Settings"
            return dict(self.system_settings)
        raise AssertionError(doctype)

    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any:
        assert doctype == "Company"
        assert name == COMPANY
        self.update_calls.append(dict(doc))
        if self._update_lands:
            self.company.update(doc)
            if self._version_created_by_update is not None:
                self.versions.insert(0, self._version_created_by_update)
        return dict(self.company)

    def iter_all(self, doctype: str, **kwargs: Any) -> Any:
        self.iter_calls.append((doctype, kwargs))
        assert doctype == "Version"
        return iter(self.versions)


# --- read_lock ---------------------------------------------------------


def test_read_lock_reads_both_fields_when_set():
    site = _Site(
        company={"accounts_frozen_till_date": "2026-06-30", "role_allowed_for_frozen_entries": "Accounts Manager"}
    )

    assert read_lock(site, COMPANY) == LockState(closed_through=dt.date(2026, 6, 30), bypass_role="Accounts Manager")


def test_read_lock_treats_unset_fields_as_none():
    site = _Site(company={"accounts_frozen_till_date": "", "role_allowed_for_frozen_entries": ""})

    assert read_lock(site, COMPANY) == LockState(closed_through=None, bypass_role=None)


def test_read_lock_treats_missing_fields_as_none():
    site = _Site(company={})

    assert read_lock(site, COMPANY) == LockState(closed_through=None, bypass_role=None)


def test_read_visible_lock_reads_both_fields_when_set():
    site = _Site(
        company={"accounts_frozen_till_date": "2026-06-30", "role_allowed_for_frozen_entries": "Accounts Manager"}
    )

    assert read_visible_lock(site, COMPANY, Profile.READER) == LockState(
        closed_through=dt.date(2026, 6, 30), bypass_role="Accounts Manager"
    )


def test_read_visible_lock_treats_unset_but_present_fields_as_none():
    site = _Site(company={"accounts_frozen_till_date": "", "role_allowed_for_frozen_entries": ""})

    assert read_visible_lock(site, COMPANY, Profile.READER) == LockState(closed_through=None, bypass_role=None)


def test_read_visible_lock_refuses_when_a_field_is_missing_entirely():
    # Frappe deletes a permlevel-1 field it cannot read rather than returning
    # it as null -- a reader never (re-)provisioned since that grant was
    # added sees a Company doc with no `accounts_frozen_till_date` key at
    # all, indistinguishable from "genuinely unset" to a plain `.get()`.
    site = _Site(company={"role_allowed_for_frozen_entries": "Accounts Manager"})

    with pytest.raises(PeriodError, match="Books Closed Through"):
        read_visible_lock(site, COMPANY, Profile.READER)


def test_read_visible_lock_names_both_fields_when_both_are_missing():
    site = _Site(company={})

    with pytest.raises(PeriodError) as exc_info:
        read_visible_lock(site, COMPANY, Profile.READER)
    assert "Books Closed Through" in str(exc_info.value)
    assert "the frozen-entries role" in str(exc_info.value)
    assert "runningtab access provision --profile reader" in str(exc_info.value)


def test_read_visible_lock_names_the_profile_to_reprovision():
    # The Mercury ingest reads the lock as the bot: its remedy is the bot's
    # own re-provision, not the reader's.
    site = _Site(company={})

    with pytest.raises(PeriodError) as exc_info:
        read_visible_lock(site, COMPANY, Profile.BOT)
    assert "the bot credential cannot see" in str(exc_info.value)
    assert "runningtab access provision --profile bot" in str(exc_info.value)


# --- lock_history: basic decoding -----------------------------------------


def test_lock_history_decodes_a_version_that_touched_the_date_field():
    site = _Site(
        versions=[
            _version(
                "VER-1",
                owner="gavin@carothers.name",
                creation="2026-09-20 10:00:00",
                changed=[["accounts_frozen_till_date", "2026-03-31", "2026-06-30"]],
            )
        ]
    )

    assert lock_history(site, COMPANY) == [
        LockChange(
            version="VER-1",
            at=dt.datetime(2026, 9, 20, 10, 0, 0),
            by="gavin@carothers.name",
            old=dt.date(2026, 3, 31),
            new=dt.date(2026, 6, 30),
            old_text="2026-03-31",
            new_text="2026-06-30",
        )
    ]


def test_lock_history_ignores_a_version_that_only_touched_an_unrelated_field():
    site = _Site(
        versions=[
            _version(
                "VER-1",
                owner="gavin@carothers.name",
                creation="2026-09-20 10:00:00",
                changed=[["role_allowed_for_frozen_entries", "", "Accounts Manager"]],
            )
        ]
    )

    assert lock_history(site, COMPANY) == []


def test_lock_history_handles_an_empty_old_value_as_the_first_freeze():
    site = _Site(
        versions=[
            _version(
                "VER-1",
                owner="gavin@carothers.name",
                creation="2026-09-20 10:00:00",
                changed=[["accounts_frozen_till_date", "", "2026-03-31"]],
            )
        ]
    )

    assert lock_history(site, COMPANY) == [
        LockChange(
            version="VER-1",
            at=dt.datetime(2026, 9, 20, 10, 0, 0),
            by="gavin@carothers.name",
            old=None,
            new=dt.date(2026, 3, 31),
            old_text="",
            new_text="2026-03-31",
        )
    ]


def test_lock_history_handles_a_null_old_value():
    site = _Site(
        versions=[
            _version(
                "VER-1",
                owner="gavin@carothers.name",
                creation="2026-09-20 10:00:00",
                changed=[["accounts_frozen_till_date", None, "2026-03-31"]],
            )
        ]
    )

    change = lock_history(site, COMPANY)[0]
    assert change.old is None
    assert change.old_text == ""


def test_lock_history_picks_the_matching_change_among_several_in_one_version():
    site = _Site(
        versions=[
            _version(
                "VER-1",
                owner="gavin@carothers.name",
                creation="2026-09-20 10:00:00",
                changed=[
                    ["role_allowed_for_frozen_entries", "", "Accounts Manager"],
                    ["accounts_frozen_till_date", "2026-03-31", "2026-06-30"],
                ],
            )
        ]
    )

    assert lock_history(site, COMPANY) == [
        LockChange(
            version="VER-1",
            at=dt.datetime(2026, 9, 20, 10, 0, 0),
            by="gavin@carothers.name",
            old=dt.date(2026, 3, 31),
            new=dt.date(2026, 6, 30),
            old_text="2026-03-31",
            new_text="2026-06-30",
        )
    ]


def test_lock_history_preserves_server_order_newest_first():
    # The module does not re-sort -- ordering is the server's job, requested
    # via order_by, and this proves the rows are returned in whatever order
    # iter_all yielded them.
    site = _Site(
        versions=[
            _version(
                "VER-2",
                owner="a@example.com",
                creation="2026-09-21 10:00:00",
                changed=[["accounts_frozen_till_date", "2026-06-30", "2026-09-30"]],
            ),
            _version(
                "VER-1",
                owner="a@example.com",
                creation="2026-09-20 10:00:00",
                changed=[["accounts_frozen_till_date", "2026-03-31", "2026-06-30"]],
            ),
        ]
    )

    history = lock_history(site, COMPANY)

    assert [change.version for change in history] == ["VER-2", "VER-1"]


def test_lock_history_sends_the_ref_doctype_and_docname_filters():
    site = _Site()
    lock_history(site, COMPANY)

    doctype, kwargs = site.iter_calls[0]
    assert doctype == "Version"
    assert ["ref_doctype", "=", "Company"] in kwargs["filters"]
    assert ["docname", "=", COMPANY] in kwargs["filters"]
    assert kwargs["order_by"] == "creation desc, name desc"


# --- lock_history: non-ISO date text ---------------------------------------


def test_lock_history_parses_a_non_iso_formatted_date():
    # "30-09-2026" is unambiguous under dd-mm-yyyy: no valid mm-dd-yyyy
    # reading exists for a day-of-month of 30 as a month number.
    site = _Site(
        versions=[
            _version(
                "VER-1",
                owner="gavin@carothers.name",
                creation="2026-09-20 10:00:00",
                changed=[["accounts_frozen_till_date", "31-03-2026", "30-09-2026"]],
            )
        ]
    )

    assert lock_history(site, COMPANY) == [
        LockChange(
            version="VER-1",
            at=dt.datetime(2026, 9, 20, 10, 0, 0),
            by="gavin@carothers.name",
            old=dt.date(2026, 3, 31),
            new=dt.date(2026, 9, 30),
            old_text="31-03-2026",
            new_text="30-09-2026",
        )
    ]


def test_lock_history_disambiguates_using_the_sites_date_format():
    # "05-06-2026" is genuinely ambiguous: dd-mm-yyyy reads it as 2026-06-05,
    # mm-dd-yyyy as 2026-05-06. Only System Settings' own date_format
    # resolves it.
    site = _Site(
        versions=[
            _version(
                "VER-1",
                owner="gavin@carothers.name",
                creation="2026-09-20 10:00:00",
                changed=[["accounts_frozen_till_date", "", "05-06-2026"]],
            )
        ],
        system_settings={"date_format": "mm-dd-yyyy"},
    )

    change = lock_history(site, COMPANY)[0]
    assert change.new == dt.date(2026, 5, 6)
    assert change.new_text == "05-06-2026"


def test_lock_history_refuses_an_ambiguous_value_when_the_site_date_format_cannot_be_read():
    site = _Site(
        versions=[
            _version(
                "VER-1",
                owner="gavin@carothers.name",
                creation="2026-09-20 10:00:00",
                changed=[["accounts_frozen_till_date", "", "05-06-2026"]],
            )
        ],
        system_settings_error=True,
    )

    with pytest.raises(PeriodError, match="ambiguous") as excinfo:
        lock_history(site, COMPANY)
    assert "VER-1" in str(excinfo.value)


def test_lock_history_refuses_an_unparseable_value_by_name():
    site = _Site(
        versions=[
            _version(
                "VER-1",
                owner="gavin@carothers.name",
                creation="2026-09-20 10:00:00",
                changed=[["accounts_frozen_till_date", "", "not-a-date"]],
            )
        ]
    )

    with pytest.raises(PeriodError) as excinfo:
        lock_history(site, COMPANY)
    assert "VER-1" in str(excinfo.value)
    assert "not-a-date" in str(excinfo.value)


# --- set_closed_through ---------------------------------------------------


def test_set_closed_through_writes_the_new_date_and_returns_its_audit_trail():
    site = _Site(
        company={"accounts_frozen_till_date": "2026-03-31"},
        version_created_by_update=_version(
            "VER-1",
            owner="gavin@carothers.name",
            creation="2026-09-23 09:00:00",
            changed=[["accounts_frozen_till_date", "2026-03-31", "2026-06-30"]],
        ),
    )

    change = set_closed_through(site, COMPANY, dt.date(2026, 6, 30))

    assert site.update_calls == [{"accounts_frozen_till_date": "2026-06-30"}]
    assert change == LockChange(
        version="VER-1",
        at=dt.datetime(2026, 9, 23, 9, 0, 0),
        by="gavin@carothers.name",
        old=dt.date(2026, 3, 31),
        new=dt.date(2026, 6, 30),
        old_text="2026-03-31",
        new_text="2026-06-30",
    )


def test_set_closed_through_accepts_the_first_freeze_from_unset():
    site = _Site(
        version_created_by_update=_version(
            "VER-1",
            owner="gavin@carothers.name",
            creation="2026-09-23 09:00:00",
            changed=[["accounts_frozen_till_date", "", "2026-03-31"]],
        )
    )

    change = set_closed_through(site, COMPANY, dt.date(2026, 3, 31))

    assert change.new == dt.date(2026, 3, 31)


def test_set_closed_through_confirms_by_version_identity_not_by_parsing_its_text():
    # The Version's `new` text is the site's dd-mm-yyyy format, not ISO. A
    # match on `change.new == date` derived by parsing that text would work
    # here too, coincidentally -- the point is that the confirmation is "a
    # Version that did not exist before this call now touches the field",
    # never a parsed-text comparison, which the older implementation used
    # and which breaks the moment the site's date_format is not ISO.
    site = _Site(
        company={"accounts_frozen_till_date": "2026-03-31"},
        version_created_by_update=_version(
            "VER-1",
            owner="gavin@carothers.name",
            creation="2026-09-23 09:00:00",
            changed=[["accounts_frozen_till_date", "31-03-2026", "30-06-2026"]],
        ),
    )

    change = set_closed_through(site, COMPANY, dt.date(2026, 6, 30))

    assert change.version == "VER-1"
    assert change.new_text == "30-06-2026"


def test_set_closed_through_refuses_to_move_the_date_backwards():
    site = _Site(company={"accounts_frozen_till_date": "2026-06-30"})

    with pytest.raises(PeriodError, match="backwards"):
        set_closed_through(site, COMPANY, dt.date(2026, 3, 31))

    assert site.update_calls == []


def test_set_closed_through_refuses_when_the_write_does_not_visibly_land():
    # Models the same silent-reset failure runningtab.period.fence proves
    # against: the update call does not raise, but the stored field never
    # changes. read_lock after the write is what catches this.
    site = _Site(company={"accounts_frozen_till_date": "2026-03-31"}, update_lands=False)

    with pytest.raises(PeriodError, match="reading it back"):
        set_closed_through(site, COMPANY, dt.date(2026, 6, 30))

    assert site.update_calls == [{"accounts_frozen_till_date": "2026-06-30"}]


def test_set_closed_through_refuses_when_no_new_version_appears_at_all():
    site = _Site(company={"accounts_frozen_till_date": "2026-03-31"})

    with pytest.raises(PeriodError, match="audit trail"):
        set_closed_through(site, COMPANY, dt.date(2026, 6, 30))

    assert site.update_calls == [{"accounts_frozen_till_date": "2026-06-30"}]


def test_set_closed_through_refuses_when_only_a_stale_version_matches_the_date():
    # A pre-existing Version whose recorded `new` happens to equal the
    # target date must not be mistaken for this call's own audit trail --
    # only a Version that appeared after the update counts.
    stale = _version(
        "VER-OLD",
        owner="gavin@carothers.name",
        creation="2020-01-01 00:00:00",
        changed=[["accounts_frozen_till_date", "2019-12-31", "2026-06-30"]],
    )
    site = _Site(company={"accounts_frozen_till_date": "2026-03-31"}, versions=[stale])

    with pytest.raises(PeriodError, match="audit trail"):
        set_closed_through(site, COMPANY, dt.date(2026, 6, 30))

    assert site.update_calls == [{"accounts_frozen_till_date": "2026-06-30"}]
