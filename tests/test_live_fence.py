"""Fence Company's two lock fields on the real disposable site, and prove the bot cannot move them.

Skipped unless RUNNINGTAB_LIVE is set (see `require_live_target` in conftest.py).
Frappe does not refuse a write to a field above the writer's permlevel -- it
silently resets it (`validate_higher_perm_levels`, frappe/model/document.py) --
so `prove_fence` is the only thing that proves the fence holds. This file's job
is to run it for real, against a live server, and let the assertions below say
plainly whether the server refused the bot's write outright or merely left the
stored value unchanged. `install_fence` and `provision` are both idempotent, so
every test here calls them itself rather than depending on run order.
"""

from __future__ import annotations

import dataclasses
import re

import pytest
from icedcoffee_sync.frappe.access import ensure_user
from icedcoffee_sync.frappe.client import Client, SiteConfig

from runningtab.access import provision
from runningtab.period import PeriodError
from runningtab.period.fence import (
    LOCK_FIELDS,
    check_fence_probe,
    install_fence,
    probe_role_holders,
    prove_fence,
)
from runningtab.site import Target, is_test_site, site_config
from tests.conftest import live_books, require_live_target

pytestmark = pytest.mark.live_runningtab


@pytest.fixture
def client():
    config = require_live_target()
    if not is_test_site(live_books().site, config.url):
        # Fail, never skip: this fixture provisions a real role and user.
        pytest.fail(f"refusing to run against a host not in the books' test_hosts: {config.url}")
    with Client(config) as c:
        yield c


@pytest.fixture
def company(client) -> str:
    rows = client.list("Company", fields=["name"], limit=2)
    if len(rows) != 1:
        pytest.fail(f"expected exactly one company on the test site, found {len(rows)}")
    return rows[0]["name"]


def _credential_config(report) -> SiteConfig:
    """The freshly minted key's own client config, same URL as the test site."""
    return dataclasses.replace(
        site_config(live_books().site, Target.TEST), api_key=report.keys.api_key, api_secret=report.keys.api_secret
    )


# A throwaway user that exists only inside the test below. `.test` is
# reserved (RFC 2606), so no mail can ever reach anyone.
_PROBE_HOLDER = "fence-probe-holder@transpire.test"


def test_a_user_holding_the_probe_role_is_found_and_refused_before_any_write(client, company) -> None:
    """The holder check against real Frappe: a user who actually holds the probe role must be found.

    The unit tests' fake answers the `Has Role` child-table filter itself, so
    only this proves the site does. The user is created holding nothing but
    the probe role, and deleted afterwards whatever the assertion says; the
    refusal comes before any write to Company, so the lock fields are never
    touched.
    """
    install_fence(client, live_books().fence_probe_role)
    before = {field: client.get("Company", company).get(field) for field in LOCK_FIELDS}
    ensure_user(
        client, _PROBE_HOLDER, first_name="Fence probe holder", roles=frozenset({live_books().fence_probe_role})
    )
    try:
        assert _PROBE_HOLDER in probe_role_holders(client, live_books().fence_probe_role)
        with pytest.raises(PeriodError, match=re.escape(_PROBE_HOLDER)):
            check_fence_probe(client, company, live_books().fence_probe_role)
    finally:
        client.delete("User", _PROBE_HOLDER)
    assert _PROBE_HOLDER not in probe_role_holders(client, live_books().fence_probe_role)
    assert {field: client.get("Company", company).get(field) for field in LOCK_FIELDS} == before


def test_install_fence_creates_or_repairs_both_property_setters_at_permlevel_1(client) -> None:
    install_fence(client, live_books().fence_probe_role)

    rows = client.list(
        "Property Setter",
        fields=["field_name", "value"],
        filters=[["doc_type", "=", "Company"], ["property", "=", "permlevel"]],
        limit=10,
    )
    by_field = {row["field_name"]: row["value"] for row in rows}
    for field in LOCK_FIELDS:
        assert by_field.get(field) == "1", (
            f"{field}: expected a permlevel Property Setter with value '1', got {by_field.get(field)!r}"
        )


def test_the_bots_key_cannot_move_either_lock_field(client, company) -> None:
    """The one proof that means anything: a real write with the bot's key, read back by an administrator.

    Both `date_refused` and `role_refused` are True whether the server
    answered with a `PermissionError_` or silently kept the stored value --
    `prove_fence` treats them the same, but which one actually happened on
    this site is worth knowing, so `date_how`/`role_how` are printed (visible
    with `-s`) and recorded in the task report, not just checked through the
    boolean.
    """
    install_fence(client, live_books().fence_probe_role)
    report = provision(client, live_books().bot)

    with Client(_credential_config(report)) as bot_client:
        proof = prove_fence(client, bot_client, company, live_books().fence_probe_role)

    print(f"accounts_frozen_till_date: refused={proof.date_refused} how={proof.date_how}")
    print(f"role_allowed_for_frozen_entries: refused={proof.role_refused} how={proof.role_how}")

    assert proof.date_refused, f"accounts_frozen_till_date was NOT refused by the server; restored={proof.restored}"
    assert proof.role_refused, (
        f"role_allowed_for_frozen_entries was NOT refused by the server; restored={proof.restored}"
    )


def test_a_reader_can_read_the_frozen_date_after_being_provisioned(client, company) -> None:
    """`runningtab status`/`runningtab health` need permlevel-1 Company read; only the reader is granted it (`READER_GRANTS`).

    A field fenced above the reader's permlevel still appears as a key in a
    `get` response, just with its value nulled out -- `"field" in doc` would
    pass either way, proving nothing. This instead has the administrator set
    a known value first (the existing one if already set, since that is
    already known and this must never move an existing freeze earlier; the
    harmless placeholder otherwise) and asserts the reader reads back that
    *exact* value, restoring it afterwards regardless of how the assertion
    comes out.
    """
    install_fence(client, live_books().fence_probe_role)
    reader_report = provision(client, live_books().reader)

    original = client.get("Company", company).get(LOCK_FIELDS[0])
    known = original or "1900-01-01"
    if not original:
        client.update("Company", company, {LOCK_FIELDS[0]: known})
    try:
        with Client(_credential_config(reader_report)) as reader_client:
            doc = reader_client.get("Company", company)
        assert doc.get(LOCK_FIELDS[0]) == known, (
            f"the reader read {doc.get(LOCK_FIELDS[0])!r} for {LOCK_FIELDS[0]}, not the admin-known {known!r} -- "
            "permlevel-1 read is not actually effective for this credential"
        )
    finally:
        if not original:
            client.update("Company", company, {LOCK_FIELDS[0]: original})
