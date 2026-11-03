"""Fencing Company's two lock fields at permission level 1, and proving the bot cannot move them.

Frappe does not refuse a write to a field above the writer's permlevel -- it
silently resets it (`validate_higher_perm_levels`, frappe/model/document.py) --
so the only proof is a real write attempted with the bot's key, read back by an
administrator. These tests stand in a tiny fake site: Property Setters, Roles
and one Company document, all in dicts, recording every call the same way
`tests/test_access.py`'s `_FakeClient` does.
"""

from __future__ import annotations

from typing import Any

import pytest
from icedcoffee_sync.frappe.errors import DoesNotExistError, PermissionError_

from runningtab.period import PeriodError
from runningtab.period.fence import (
    LOCK_FIELDS,
    FenceProof,
    check_fence_probe,
    install_fence,
    property_setter_documents,
    prove_fence,
)
from tests.conftest import EXAMPLE_BOOKS

_PERMISSION_MANAGER = "frappe.core.page.permission_manager.permission_manager"


def _match(row: dict[str, Any], filters: list[list[Any]]) -> bool:
    return all(row.get(f) == v for f, _op, v in filters)


class _FenceSite:
    """A tiny stand-in ERPNext: Property Setters, Roles, and one Company document, in dicts.

    `bot_writes_land` controls whether the bot's `update` on Company actually
    changes the stored document (a permission bug) or is silently dropped --
    what Frappe's own `validate_higher_perm_levels` does for a real field
    above the writer's permlevel. `bot_raises` makes the bot's `update` raise
    `PermissionError_` instead, the other way the server might refuse it;
    `bot_raises_other` raises something else, e.g. a transport error, to prove
    the restore still runs regardless of what kind of exception the write
    produced.

    `raise_on_company_get_call` fails one specific `get("Company", ...)` call
    by its 1-indexed position across the whole test (`prove_fence`'s own
    initial read is call 1), to prove the restore still runs even when a
    read-back raises. `restore_fails_to_take` makes the admin's restoring
    `update` a no-op, so the read-back that verifies it finds the field still
    stuck. `restore_raises` makes that same restoring `update` raise outright.
    """

    def __init__(
        self,
        *,
        company: dict[str, Any] | None = None,
        bot_writes_land: bool = False,
        bot_raises: bool = False,
        bot_raises_other: bool = False,
        raise_on_company_get_call: int | None = None,
        restore_fails_to_take: bool = False,
        restore_raises: bool = False,
        user_roles: dict[str, set[str]] | None = None,
    ) -> None:
        self.user_roles = user_roles or {}
        self.property_setters: dict[str, dict[str, Any]] = {}
        self.roles: dict[str, dict[str, Any]] = {}
        self.company: dict[str, Any] = {"name": "Transpire", **(company or {})}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        # What the bot actually attempted to write, whether or not it landed.
        self.bot_calls: list[dict[str, Any]] = []
        self.bot_writes_land = bot_writes_land
        self.bot_raises = bot_raises
        self.bot_raises_other = bot_raises_other
        self.raise_on_company_get_call = raise_on_company_get_call
        self.restore_fails_to_take = restore_fails_to_take
        self.restore_raises = restore_raises
        self._n = 0
        self._company_get_calls = 0

    # --- admin-side surface (`_AdminClient`-shaped) ---

    def list(self, doctype: str, *, fields: Any = None, filters: Any = None, limit: Any = None, **_: Any) -> Any:
        if doctype == "User":
            # Frappe's child-table filter shape: [child doctype, field, op, value].
            ((child, field, _op, role),) = filters
            assert (child, field) == ("Has Role", "role")
            rows = [{"name": user} for user, roles in sorted(self.user_roles.items()) if role in roles]
            return rows[:limit] if limit else rows
        assert doctype == "Property Setter"
        rows = [row for row in self.property_setters.values() if _match(row, filters or [])]
        return rows[:limit] if limit else rows

    def insert(self, doctype: str, doc: dict[str, Any]) -> Any:
        if doctype == "Role":
            name = doc["role_name"]
            stored = {"name": name, **doc}
            self.roles[name] = stored
            return stored
        assert doctype == "Property Setter"
        self._n += 1
        name = f"PS-{self._n}"
        stored = {"name": name, **doc}
        self.property_setters[name] = stored
        return stored

    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any:
        if doctype == "Property Setter":
            self.property_setters[name].update(doc)
            return self.property_setters[name]
        if doctype == "Role":
            self.roles[name].update(doc)
            return self.roles[name]
        if doctype == "Company":
            self.calls.append(("admin update Company", doc))
            if self.restore_raises:
                raise RuntimeError("transport error restoring Company")
            if self.restore_fails_to_take:
                return dict(self.company)  # a "write" that never actually lands
            self.company.update(doc)
            return dict(self.company)
        raise AssertionError(doctype)

    def get(self, doctype: str, name: str) -> Any:
        if doctype == "Role":
            if name not in self.roles:
                raise DoesNotExistError(f"no Role named {name!r}")
            return dict(self.roles[name])
        assert doctype == "Company"
        self._company_get_calls += 1
        self.calls.append(("admin get Company", None))
        if self.raise_on_company_get_call == self._company_get_calls:
            raise RuntimeError(f"transport error on Company get #{self._company_get_calls}")
        return dict(self.company)

    def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((dotted_path, params))
        return None

    # --- bot-side surface: the one write `prove_fence` attempts ---

    def bot_update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any:
        assert doctype == "Company"
        self.bot_calls.append(dict(doc))
        if self.bot_raises:
            raise PermissionError_("not permitted")
        if self.bot_raises_other:
            raise RuntimeError("transport error on the bot's write")
        if self.bot_writes_land:
            self.company.update(doc)
        return dict(self.company)


class _Bot:
    """`prove_fence`'s `bot` argument: only `update`, routed at the shared site."""

    def __init__(self, site: _FenceSite) -> None:
        self.site = site

    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any:
        return self.site.bot_update(doctype, name, doc)


def _permission_manager_calls(site: _FenceSite) -> list[dict[str, Any]]:
    return [params for path, params in site.calls if path == f"{_PERMISSION_MANAGER}.update" and params is not None]


def _restore_writes(site: _FenceSite) -> list[dict[str, Any]]:
    return [doc for label, doc in site.calls if label == "admin update Company" and doc is not None]


_ORIGINAL = {LOCK_FIELDS[0]: "2026-06-30", LOCK_FIELDS[1]: "Accounts Manager"}


# --- property_setter_documents ---------------------------------------------


def test_property_setter_documents_is_exactly_the_two_lock_fields():
    assert LOCK_FIELDS == ("accounts_frozen_till_date", "role_allowed_for_frozen_entries")
    assert property_setter_documents() == [
        {
            "doctype": "Property Setter",
            "doctype_or_field": "DocField",
            "doc_type": "Company",
            "field_name": "accounts_frozen_till_date",
            "property": "permlevel",
            "value": "1",
            "property_type": "Int",
        },
        {
            "doctype": "Property Setter",
            "doctype_or_field": "DocField",
            "doc_type": "Company",
            "field_name": "role_allowed_for_frozen_entries",
            "property": "permlevel",
            "value": "1",
            "property_type": "Int",
        },
    ]


# --- install_fence -----------------------------------------------------------


def test_install_fence_on_an_empty_site_creates_both_property_setters_and_grants_system_manager():
    site = _FenceSite()

    results = install_fence(site, EXAMPLE_BOOKS.fence_probe_role)

    assert results == [
        ("Company.accounts_frozen_till_date", "created"),
        ("Company.role_allowed_for_frozen_entries", "created"),
        (EXAMPLE_BOOKS.fence_probe_role, "created"),
    ]
    # Stored without a redundant "doctype" key -- same convention as
    # `install_custom_fields`, which strips it before `insert` since the
    # doctype is already the endpoint's own first argument.
    stored = {row["field_name"]: row for row in site.property_setters.values()}
    for field, expected in zip(LOCK_FIELDS, property_setter_documents(), strict=True):
        row = dict(stored[field])
        del row["name"]
        assert row == {k: v for k, v in expected.items() if k != "doctype"}

    granted = {
        params["ptype"]: params["value"]
        for params in _permission_manager_calls(site)
        if params.get("role") == "System Manager" and params.get("permlevel") == 1
    }
    assert granted["read"] == "1"
    assert granted["write"] == "1"
    # Every other managed flag is sent explicitly as "0" -- see icedcoffee-sync's `grant`.
    assert granted["submit"] == "0"

    add_calls = [
        params
        for path, params in site.calls
        if path == f"{_PERMISSION_MANAGER}.add" and params is not None and params.get("role") == "System Manager"
    ]
    assert any(p.get("parent") == "Company" and p.get("permlevel") == 1 for p in add_calls)


def test_install_fence_creates_the_probe_role_with_no_desk_access_and_grants_it_to_nobody():
    site = _FenceSite()

    install_fence(site, EXAMPLE_BOOKS.fence_probe_role)

    role = site.roles[EXAMPLE_BOOKS.fence_probe_role]
    assert role["role_name"] == EXAMPLE_BOOKS.fence_probe_role
    assert not role["desk_access"]
    # Nothing in `install_fence` ever assigns this role to a user -- it exists
    # purely as a value the probe can write that satisfies nobody's check.


def test_install_fence_a_second_time_reports_unchanged():
    site = _FenceSite()
    install_fence(site, EXAMPLE_BOOKS.fence_probe_role)

    results = install_fence(site, EXAMPLE_BOOKS.fence_probe_role)

    assert results == [
        ("Company.accounts_frozen_till_date", "unchanged"),
        ("Company.role_allowed_for_frozen_entries", "unchanged"),
        (EXAMPLE_BOOKS.fence_probe_role, "unchanged"),
    ]


def test_install_fence_repairs_a_setter_whose_value_drifted_to_zero():
    site = _FenceSite()
    install_fence(site, EXAMPLE_BOOKS.fence_probe_role)
    drifted = next(row for row in site.property_setters.values() if row["field_name"] == LOCK_FIELDS[0])
    drifted["value"] = "0"

    results = install_fence(site, EXAMPLE_BOOKS.fence_probe_role)

    assert ("Company.accounts_frozen_till_date", "updated") in results
    fixed = next(row for row in site.property_setters.values() if row["field_name"] == LOCK_FIELDS[0])
    assert fixed["value"] == "1"


# --- prove_fence: the ordinary refusal/restore cases ------------------------


def test_prove_fence_against_a_silently_reset_bot_refuses_both_and_restores_nothing():
    site = _FenceSite(company=dict(_ORIGINAL))
    bot = _Bot(site)

    proof = prove_fence(site, bot, "Transpire", EXAMPLE_BOOKS.fence_probe_role)

    assert proof == FenceProof(date_refused=True, role_refused=True, date_how="reset", role_how="reset", restored=())
    assert site.company[LOCK_FIELDS[0]] == "2026-06-30"
    assert site.company[LOCK_FIELDS[1]] == "Accounts Manager"


def test_prove_fence_against_a_bot_write_that_lands_restores_the_original_and_names_it():
    site = _FenceSite(company=dict(_ORIGINAL), bot_writes_land=True)
    bot = _Bot(site)

    proof = prove_fence(site, bot, "Transpire", EXAMPLE_BOOKS.fence_probe_role)

    assert proof.date_refused is False
    assert proof.role_refused is False
    assert proof.date_how == "took"
    assert proof.role_how == "took"
    assert sorted(proof.restored) == sorted(LOCK_FIELDS)
    assert isinstance(proof.restored, tuple)
    # Restored by the administrator, back to what it was before either probe --
    # and verified, not merely attempted.
    assert site.company[LOCK_FIELDS[0]] == "2026-06-30"
    assert site.company[LOCK_FIELDS[1]] == "Accounts Manager"


def test_prove_fence_against_a_bot_update_that_raises_permission_error_counts_as_refused():
    site = _FenceSite(company=dict(_ORIGINAL), bot_raises=True)
    bot = _Bot(site)

    proof = prove_fence(site, bot, "Transpire", EXAMPLE_BOOKS.fence_probe_role)

    assert proof == FenceProof(date_refused=True, role_refused=True, date_how="raised", role_how="raised", restored=())


def test_prove_fence_probes_an_unset_date_with_1900_01_01():
    site = _FenceSite(company={LOCK_FIELDS[1]: "Accounts Manager"}, bot_writes_land=True)
    bot = _Bot(site)

    prove_fence(site, bot, "Transpire", EXAMPLE_BOOKS.fence_probe_role)

    date_writes = [doc[LOCK_FIELDS[0]] for doc in site.bot_calls if LOCK_FIELDS[0] in doc]
    assert date_writes == ["1900-01-01"]


def test_prove_fence_probes_a_set_date_with_one_day_later():
    site = _FenceSite(company=dict(_ORIGINAL), bot_writes_land=True)
    bot = _Bot(site)

    prove_fence(site, bot, "Transpire", EXAMPLE_BOOKS.fence_probe_role)

    date_writes = [doc[LOCK_FIELDS[0]] for doc in site.bot_calls if LOCK_FIELDS[0] in doc]
    assert date_writes == ["2026-07-01"]


def test_prove_fence_probes_the_role_field_with_the_dedicated_probe_role():
    # Never "Guest" or "All": Frappe's get_roles() hands both to every
    # logged-in non-Administrator user, so probing with either would (if the
    # write ever landed) unlock the frozen period for everyone rather than
    # proving anything. See the module docstring's citation.
    site = _FenceSite(company=dict(_ORIGINAL), bot_writes_land=True)
    bot = _Bot(site)

    prove_fence(site, bot, "Transpire", EXAMPLE_BOOKS.fence_probe_role)

    role_writes = [doc[LOCK_FIELDS[1]] for doc in site.bot_calls if LOCK_FIELDS[1] in doc]
    assert role_writes == [EXAMPLE_BOOKS.fence_probe_role]
    assert EXAMPLE_BOOKS.fence_probe_role not in ("Guest", "All")


# --- prove_fence: the restore's own failure modes ---------------------------


def test_prove_fence_still_restores_when_the_read_back_raises():
    # `raise_on_company_get_call=2`: call 1 is prove_fence's own initial read
    # of Company; call 2 is the date probe's read-back, which raises here.
    # The restore must still run despite never seeing a confirmed value.
    site = _FenceSite(company=dict(_ORIGINAL), raise_on_company_get_call=2)
    bot = _Bot(site)

    with pytest.raises(RuntimeError, match="transport error on Company get #2"):
        prove_fence(site, bot, "Transpire", EXAMPLE_BOOKS.fence_probe_role)

    restores = _restore_writes(site)
    assert {LOCK_FIELDS[0]: "2026-06-30"} in restores, (
        f"no restore of {LOCK_FIELDS[0]} was attempted after its read-back raised: {restores}"
    )


def test_prove_fence_still_restores_when_the_bots_write_raises_something_other_than_permission_error():
    site = _FenceSite(company=dict(_ORIGINAL), bot_raises_other=True)
    bot = _Bot(site)

    with pytest.raises(RuntimeError, match="transport error on the bot's write"):
        prove_fence(site, bot, "Transpire", EXAMPLE_BOOKS.fence_probe_role)

    restores = _restore_writes(site)
    assert {LOCK_FIELDS[0]: "2026-06-30"} in restores, (
        f"no restore of {LOCK_FIELDS[0]} was attempted after the bot's write raised: {restores}"
    )


def test_prove_fence_raises_period_error_naming_the_field_when_the_restore_does_not_take():
    site = _FenceSite(company=dict(_ORIGINAL), bot_writes_land=True, restore_fails_to_take=True)
    bot = _Bot(site)

    with pytest.raises(PeriodError, match=LOCK_FIELDS[0]) as excinfo:
        prove_fence(site, bot, "Transpire", EXAMPLE_BOOKS.fence_probe_role)

    # Both the stuck value and the original are named, so whoever reads this
    # knows exactly what to set the field back to in ERPNext.
    assert "2026-07-01" in str(excinfo.value)  # the probe's own moved value, still stuck
    assert "2026-06-30" in str(excinfo.value)  # the original it should be


def test_prove_fence_lets_a_restore_that_raises_propagate_loudly():
    site = _FenceSite(company=dict(_ORIGINAL), bot_writes_land=True, restore_raises=True)
    bot = _Bot(site)

    with pytest.raises(RuntimeError, match="transport error restoring Company"):
        prove_fence(site, bot, "Transpire", EXAMPLE_BOOKS.fence_probe_role)


# --- prove_fence: a probe that could not prove anything ----------------------


def test_prove_fence_refuses_when_the_bypass_role_already_is_the_probe_role():
    # Writing the value already stored reads back unchanged whether or not the
    # bot could write it, so a "reset" here would be a pass that proved
    # nothing. Refused before the bot writes anything.
    site = _FenceSite(
        company={LOCK_FIELDS[0]: "2026-06-30", LOCK_FIELDS[1]: EXAMPLE_BOOKS.fence_probe_role}, bot_writes_land=True
    )

    with pytest.raises(PeriodError, match=f"already {EXAMPLE_BOOKS.fence_probe_role!r}"):
        prove_fence(site, _Bot(site), "Transpire", EXAMPLE_BOOKS.fence_probe_role)

    assert site.bot_calls == []


def test_prove_fence_refuses_when_anyone_holds_the_probe_role():
    # For the moment the probe's value sits in role_allowed_for_frozen_entries
    # (a fence that fails lets it land), anyone holding the probe role could
    # post into a closed quarter. The role must be held by nobody, and that is
    # checked, not assumed. Refused before the bot writes anything, naming who.
    site = _FenceSite(
        company=dict(_ORIGINAL), user_roles={"sam@example.com": {EXAMPLE_BOOKS.fence_probe_role, "Accounts User"}}
    )

    with pytest.raises(PeriodError, match=r"sam@example\.com"):
        prove_fence(site, _Bot(site), "Transpire", EXAMPLE_BOOKS.fence_probe_role)

    assert site.bot_calls == []


def test_check_fence_probe_refuses_as_admin_alone_before_any_bot_exists():
    # `access provision` runs this before minting the bot's key, so it must
    # need nothing but the administrator's client and must write nothing.
    site = _FenceSite(company=dict(_ORIGINAL), user_roles={"sam@example.com": {EXAMPLE_BOOKS.fence_probe_role}})

    with pytest.raises(PeriodError, match=r"sam@example\.com"):
        check_fence_probe(site, "Transpire", EXAMPLE_BOOKS.fence_probe_role)

    assert _restore_writes(site) == []
    assert site.bot_calls == []


def test_prove_fence_goes_ahead_when_other_roles_are_held_but_not_the_probe_role():
    site = _FenceSite(company=dict(_ORIGINAL), user_roles={"sam@example.com": {"Accounts User"}})

    proof = prove_fence(site, _Bot(site), "Transpire", EXAMPLE_BOOKS.fence_probe_role)

    assert (proof.date_refused, proof.role_refused) == (True, True)
