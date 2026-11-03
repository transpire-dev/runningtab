"""Fence Company's two close-lock fields at permission level 1, and prove the bot cannot move them.

`accounts_frozen_till_date` and `role_allowed_for_frozen_entries` are the two
fields that make a quarter's close mean something: once set, they are supposed
to stop *anyone* below the fence from post-dating an entry into a closed
period. Company's standard permissions are all permlevel 0
(`erpnext/setup/doctype/company/company.json`), so the bot's ordinary
`Company: read, write` grant (`runningtab.access.GRANTS`) would otherwise reach
them like any other field.

Frappe does not refuse a write to a field above the writer's permlevel -- it
silently resets it to what is already stored (`validate_higher_perm_levels` in
`frappe/model/document.py`) -- so a doctype-level permission check proves
nothing here. The only proof is a real write attempted with the bot's key,
read back by an administrator: `prove_fence`. Its probe only ever pushes
`accounts_frozen_till_date` later than whatever is there (never earlier), and
restores whatever it finds changed -- verifying the restore actually took --
so a freeze a person actually set stays in force whether or not this proof
runs.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any, Literal, Protocol

from icedcoffee_sync.frappe.access import PermissionGrant, ensure_role, grant
from icedcoffee_sync.frappe.errors import PermissionError_

from runningtab.period import PeriodError

LOCK_FIELDS: tuple[str, str] = ("accounts_frozen_till_date", "role_allowed_for_frozen_entries")

COMPANY = "Company"
FENCE_ROLE = "System Manager"
FENCE_PERMLEVEL = 1

# The probe role -- the books directory's `fence.probe_role`, passed in as
# `probe_role` -- is what the probe writes into
# `role_allowed_for_frozen_entries`. It must be a role held by nobody -- specifically, it must NOT be "Guest" or "All".
# Frappe's `get_roles()` (frappe/permissions.py) adds both to every logged-in
# non-Administrator user's role list unconditionally (`roles +=
# [ALL_USER_ROLE, GUEST_ROLE]`), and ERPNext's `check_freezing_date`
# (erpnext/accounts/services/gl_validator.py) lets a posting through once the
# configured role is in `frappe.get_roles()`. A probe that ever landed
# "Guest" as the bypass role would not just fail to prove the fence -- it
# would open the closed quarter to every user on the site. This role exists
# for exactly one purpose (a value to write that nobody can ever satisfy) and
# `install_fence` creates it with desk access off; nothing ever grants it to
# a user. `runningtab.books` refuses a probe role of "Guest", "All", or the
# bot's or reader's own role.

_PLACEHOLDER_DATE = "1900-01-01"

HowMoved = Literal["raised", "reset", "took"]


def property_setter_documents() -> list[dict[str, Any]]:
    """The two Property Setters that push `LOCK_FIELDS` to permission level 1 on Company."""
    return [
        {
            "doctype": "Property Setter",
            "doctype_or_field": "DocField",
            "doc_type": COMPANY,
            "field_name": field,
            "property": "permlevel",
            "value": "1",
            "property_type": "Int",
        }
        for field in LOCK_FIELDS
    ]


class _AdminClient(Protocol):
    """The administrator's slice: create/repair the fence, and read/restore Company."""

    def list(self, doctype: str, **kwargs: Any) -> Any: ...
    def insert(self, doctype: str, doc: dict[str, Any]) -> Any: ...
    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any: ...
    def get(self, doctype: str, name: str) -> Any: ...
    def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any: ...


class _BotClient(Protocol):
    """The bot's slice: the one write `prove_fence` attempts."""

    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any: ...


def install_fence(admin: _AdminClient, probe_role: str) -> list[tuple[str, str]]:
    """Create or repair the two Property Setters, grant System Manager permlevel-1 Company, and ensure the probe role.

    Follows `runningtab.mercury.posting.install_custom_fields`: list each Property
    Setter by its `(doc_type, field_name, property)`, insert when absent,
    update `value` when it differs. Needs an administrator -- the books bot's
    role carries no Property Setter permission, deliberately, the same reason
    it carries no Custom Field permission.

    Does **not** call `revoke_grants_except` for System Manager: that is a
    standard role this code does not own, and reconciling it down to exactly
    one doctype's permissions would strip whatever else a person granted it.

    Also ensures `probe_role` exists (desk access off), so `prove_fence` has
    a role to probe with that is guaranteed to be held by nobody. Nothing
    here, or anywhere else in this codebase, ever grants it to a user.
    """
    results: list[tuple[str, str]] = []
    for doc in property_setter_documents():
        field = doc["field_name"]
        label = f"{COMPANY}.{field}"
        body = {k: v for k, v in doc.items() if k != "doctype"}
        existing = admin.list(
            "Property Setter",
            fields=["name", "value"],
            filters=[["doc_type", "=", COMPANY], ["field_name", "=", field], ["property", "=", "permlevel"]],
            limit=1,
        )
        if not existing:
            admin.insert("Property Setter", body)
            results.append((label, "created"))
            continue
        row = existing[0]
        if row.get("value") != body["value"]:
            admin.update("Property Setter", str(row["name"]), {"value": body["value"]})
            results.append((label, "updated"))
        else:
            results.append((label, "unchanged"))

    grant(admin, FENCE_ROLE, PermissionGrant(COMPANY, frozenset({"read", "write"}), permlevel=FENCE_PERMLEVEL))

    role_change = ensure_role(admin, probe_role)
    if role_change.created:
        results.append((probe_role, "created"))
    elif role_change.desk_access_repaired or role_change.disabled_repaired:
        results.append((probe_role, "updated"))
    else:
        results.append((probe_role, "unchanged"))
    return results


@dataclasses.dataclass(frozen=True)
class FenceProof:
    """Whether the server actually refused the bot's write to each lock field, and how."""

    date_refused: bool
    role_refused: bool
    # "raised": the bot's write itself raised PermissionError_.
    # "reset": the write reached the server but the stored value never moved
    #   (Frappe's own `validate_higher_perm_levels`).
    # "took": the value actually moved, and was restored (verified).
    date_how: HowMoved
    role_how: HowMoved
    restored: tuple[str, ...]


def _probe_date(current: Any) -> str:
    """A later `accounts_frozen_till_date` than whatever is stored -- freezes more, never less."""
    if not current:
        return _PLACEHOLDER_DATE
    parsed = dt.date.fromisoformat(str(current))
    return (parsed + dt.timedelta(days=1)).isoformat()


def _probe_value(field: str, current: Any, probe_role: str) -> Any:
    if field == LOCK_FIELDS[0]:
        return _probe_date(current)
    return probe_role


def _probe_field(
    admin: _AdminClient, bot: _BotClient, company: str, field: str, original: Any, probe_role: str
) -> tuple[bool, bool, HowMoved]:
    """Attempt the bot's write, read it back as admin, and verify the restore actually took.

    The bot's write sits inside the same `try` the restore's `finally`
    guards: if it raises `PermissionError_`, nothing landed and the probe is
    refused outright ("raised"); if it raises anything else, the write's
    effect is unknown, so the restore below still runs -- a restore after a
    write that never landed is a harmless no-op. Whatever happens, the
    restore is attempted and then verified by reading Company back a second
    time: a restore that does not read back as `original` raises
    `PeriodError` naming the field and both values, because a probe that
    cannot put a lock field back is not something to let return quietly.
    """
    try:
        try:
            bot.update(COMPANY, company, {field: _probe_value(field, original, probe_role)})
        except PermissionError_:
            return True, False, "raised"

        after = admin.get(COMPANY, company)
        changed = after.get(field) != original
        return not changed, changed, ("took" if changed else "reset")
    finally:
        admin.update(COMPANY, company, {field: original})
        confirmed = admin.get(COMPANY, company)
        if confirmed.get(field) != original:
            raise PeriodError(
                f"the fence probe could not restore {field}: it is stuck at {confirmed.get(field)!r}, "
                f"not its original {original!r}. Set it back in ERPNext now."
            )


def probe_role_holders(admin: _AdminClient, probe_role: str) -> list[str]:
    """Every user who holds `probe_role`. Nothing in this codebase grants it, so this should always be empty."""
    rows = admin.list("User", fields=["name"], filters=[["Has Role", "role", "=", probe_role]], limit=50)
    return sorted(str(row["name"]) for row in rows)


def _refuse_an_unprovable_probe(admin: _AdminClient, original: dict[str, Any], probe_role: str) -> None:
    """Refuse, before the bot writes anything, a probe whose result could not mean what it says.

    A probe value equal to what is stored reads back unchanged whether or
    not the bot could write it, so it would report a fence that was never
    tested. And while a failing fence lets the probe role sit in
    `role_allowed_for_frozen_entries`, anyone holding that role could post
    into a closed quarter -- so the role being held by nobody is checked
    here, not assumed.
    """
    for field in LOCK_FIELDS:
        current = original.get(field)
        if _probe_value(field, current, probe_role) == current:
            raise PeriodError(
                f"{field} is already {current!r}, the fence probe's own value, so the probe could not tell a "
                "refused write from one that landed; clear it in ERPNext and run provision again"
            )
    holders = probe_role_holders(admin, probe_role)
    if holders:
        raise PeriodError(
            f"{probe_role!r} is held by {', '.join(holders)}; it must be held by nobody, because the fence probe "
            "writes it into role_allowed_for_frozen_entries and a holder could post into a closed quarter while "
            "it sits there. Remove the role from those users and run provision again"
        )


def check_fence_probe(admin: _AdminClient, company: str, probe_role: str) -> None:
    """Refuse now, as the administrator, a fence probe that could not prove anything -- before any key is minted.

    `access provision` rotates the bot's key before it can run `prove_fence`,
    so a refusal found only inside the proof would leave the stored key
    already dead. Running the same checks here first keeps that refusal an
    ordinary one; `prove_fence` still repeats them immediately before it
    writes, since the site can change between the two.
    """
    _refuse_an_unprovable_probe(admin, admin.get(COMPANY, company), probe_role)


def prove_fence(admin: _AdminClient, bot: _BotClient, company: str, probe_role: str) -> FenceProof:
    """Prove the bot's key cannot move either lock field, restoring (and verifying) anything that did move.

    Reads the Company record as `admin` first, refuses a probe that could
    not prove anything (`_refuse_an_unprovable_probe`), then attempts one
    write per field with `bot`'s key. A field whose stored value did not
    change is refused; the bot's update raising `PermissionError_` counts as
    refused too. Anything that did change is restored by `admin` to its
    original value, the restore is verified, and only then is the field
    named in `restored`.
    """
    original = admin.get(COMPANY, company)
    _refuse_an_unprovable_probe(admin, original, probe_role)
    date_refused, date_changed, date_how = _probe_field(
        admin, bot, company, LOCK_FIELDS[0], original.get(LOCK_FIELDS[0]), probe_role
    )
    role_refused, role_changed, role_how = _probe_field(
        admin, bot, company, LOCK_FIELDS[1], original.get(LOCK_FIELDS[1]), probe_role
    )
    restored = tuple(
        field for field, changed in ((LOCK_FIELDS[0], date_changed), (LOCK_FIELDS[1], role_changed)) if changed
    )
    return FenceProof(
        date_refused=date_refused,
        role_refused=role_refused,
        date_how=date_how,
        role_how=role_how,
        restored=restored,
    )
