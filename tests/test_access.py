"""`runningtab.access.provision`: isolating the one irreversible step from everything before it.

Role, grant, and user reconciliation are all idempotent -- safe to just
retry from scratch if any of them fails. `generate_keys` is not: Frappe
rotates the live secret unconditionally, with no dry-run and no read-back.
These tests pin that only a `generate_keys` failure comes back as
`KeyRotationFailedError`, the type `runningtab.cli.access_provision` catches to
route into its "already rotated, re-run now" recovery message -- everything
before it must propagate bare, so that message is never shown for a failure
that never touched the live secret at all.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from icedcoffee_sync.frappe.access import PermissionGrant, SubmitProbe
from icedcoffee_sync.frappe.client import SiteConfig
from icedcoffee_sync.frappe.errors import DoesNotExistError

import runningtab.access as access_module
from runningtab.access import (
    GRANTS,
    MUTATING_PTYPES,
    READER_PROHIBITED,
    KeyRotationFailedError,
    check_credential,
    check_reader_credential,
    missing_grants_report,
    provision,
    skipped_grants,
)
from tests.conftest import EXAMPLE_BOOKS

BOT = EXAMPLE_BOOKS.bot
READER = EXAMPLE_BOOKS.reader

_FAKE_CONFIG = SiteConfig(url="https://fake.example.test", api_key="k", api_secret="s")  # noqa: S106

_PERMISSION_MANAGER = "frappe.core.page.permission_manager.permission_manager"
_GENERATE_KEYS = "frappe.core.doctype.user.user.generate_keys"


class _FakeClient:
    """Enough of `_ProvisionClient` for `provision` to run role/grant/user reconciliation.

    `Role` and `User` are both reported missing, so `ensure_role`/`ensure_user`
    take their create branch -- the fewest calls needed to reach
    `generate_keys`. `get_permissions` reports no existing rows, so
    `revoke_grants_except` has nothing to revoke.
    """

    def __init__(self, *, generate_keys_error: Exception | None = None) -> None:
        self.generate_keys_error = generate_keys_error
        self.calls: list[str] = []

    def get(self, doctype: str, name: str) -> Any:
        self.calls.append(f"get {doctype}")
        raise DoesNotExistError(f"no {doctype} named {name!r}")

    def insert(self, doctype: str, doc: dict[str, Any]) -> Any:
        self.calls.append(f"insert {doctype}")
        return doc

    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any:
        self.calls.append(f"update {doctype}")
        return doc

    def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(dotted_path)
        if dotted_path == f"{_PERMISSION_MANAGER}.get_permissions":
            return []
        if dotted_path == _GENERATE_KEYS:
            if self.generate_keys_error is not None:
                raise self.generate_keys_error
            return {"api_key": "newkey", "api_secret": "newsecret"}
        return None


def test_provision_wraps_a_generate_keys_failure_as_key_rotation_failed() -> None:
    client = _FakeClient(generate_keys_error=RuntimeError("connection reset"))

    with pytest.raises(KeyRotationFailedError, match="connection reset"):
        provision(client, BOT)

    # Role, grant, and user reconciliation all ran before the failure --
    assert client.calls[-1] == _GENERATE_KEYS


def test_provision_lets_an_earlier_failure_propagate_unwrapped() -> None:
    # A failure before generate_keys has not touched the live secret at
    class _BrokenRoleClient(_FakeClient):
        def get(self, doctype: str, name: str) -> Any:
            if doctype == "Role":
                raise RuntimeError("transport error")
            return super().get(doctype, name)

    client = _BrokenRoleClient()

    with pytest.raises(RuntimeError, match="transport error"):
        provision(client, BOT)

    assert _GENERATE_KEYS not in client.calls


def _expected_labels(grants) -> set[str]:
    """The report key `provision` uses for each grant -- computed independently of `access._grant_label`."""
    return {g.doctype if g.permlevel == 0 else f"{g.doctype} (permlevel {g.permlevel})" for g in grants}


def test_provision_reader_grants_only_the_reader_doctypes() -> None:
    client = _FakeClient()

    report = provision(client, READER)

    # Two Company grants, permlevel 0 and 1: both must show up, not collapse
    # onto one key the way a bare `g.doctype` dict comprehension would.
    assert set(report.grants) == _expected_labels(READER.grants)
    assert "Company" in report.grants
    assert "Company (permlevel 1)" in report.grants


def test_the_reader_can_read_everything_the_quarterly_export_reads():
    granted = {g.doctype: g.ptypes for g in READER.grants}
    for doctype in (
        "GL Entry",
        "Account",
        "Company",
        "Fiscal Year",
        "Customer",
        "Supplier",
        "Journal Entry",
        "Purchase Invoice",
        "Payment Entry",
        "Sales Invoice",
        "Bank Transaction",
        "File",
    ):
        assert granted[doctype] == frozenset({"read"}), doctype


def test_the_reader_is_proven_unable_to_write_every_new_doctype():
    for doctype in ("Fiscal Year", "Customer", "Payment Entry", "Sales Invoice"):
        assert READER_PROHIBITED[doctype] == MUTATING_PTYPES


class _FlagServer:
    """Stands in for the server's answers about one credential's permissions."""

    def __init__(self, allowed: dict[str, set[str]]) -> None:
        self.allowed = allowed

    def prohibited(self, config, *, doctype, ptypes):
        return {p: p in self.allowed.get(doctype, set()) for p in ptypes}

    def granted(self, config, *, doctype, ptypes):
        return dict.fromkeys(ptypes, True)


def _install(monkeypatch, server: _FlagServer) -> None:
    monkeypatch.setattr(access_module, "prohibited_flags_allowed", server.prohibited)
    monkeypatch.setattr(access_module, "granted_flags_effective", server.granted)


def test_a_reader_denied_every_mutation_passes(monkeypatch) -> None:
    _install(monkeypatch, _FlagServer({}))
    check = check_reader_credential(_FAKE_CONFIG)
    assert check.passed
    assert check.inherited == {}


def test_a_reader_allowed_a_ledger_write_fails(monkeypatch) -> None:
    # The check exists for exactly this: a permission we never granted, from a
    # role inherited somewhere else, reaching a doctype that matters.
    _install(monkeypatch, _FlagServer({"Account": {"write"}}))
    check = check_reader_credential(_FAKE_CONFIG)
    assert not check.passed
    assert check.leaked == {"Account": ["write"]}


def test_a_reader_allowed_only_the_known_file_writes_still_passes(monkeypatch) -> None:
    # Measured on the live site: Frappe hands File create/write/delete to
    # authenticated users, and it cannot be taken away from here. Reported on
    # every run rather than dropped from the check, but not a failure.
    _install(monkeypatch, _FlagServer({"File": {"create", "write", "delete"}}))
    check = check_reader_credential(_FAKE_CONFIG)
    assert check.passed
    assert check.inherited == {"File": ["create", "delete", "write"]}
    assert check.leaked == {}


def test_an_unknown_write_on_a_doctype_with_a_known_exception_still_fails(monkeypatch) -> None:
    # File is excused create/write/delete, and nothing more: `submit` there is
    # new, and must not hide behind the exception next to it.
    _install(monkeypatch, _FlagServer({"File": {"create", "submit"}}))
    check = check_reader_credential(_FAKE_CONFIG)
    assert not check.passed
    assert check.leaked == {"File": ["submit"]}
    assert check.inherited == {"File": ["create"]}


def test_provision_succeeds_and_returns_the_fresh_keys() -> None:
    client = _FakeClient()

    report = provision(client, BOT)

    assert report.keys.api_key == "newkey"
    assert report.keys.api_secret == "newsecret"
    assert report.role_created is True
    assert report.user.created is True
    assert set(report.grants) == _expected_labels(GRANTS)
    assert "Company" in report.grants
    assert "Company (permlevel 1)" in report.grants
    assert report.revoked == []


def test_provision_keeps_grants_by_doctype_and_permlevel_together() -> None:
    # A grant at permlevel 1 must not save a stray permlevel-1 row on a
    # doctype only granted at permlevel 0, and a permlevel-0 grant must not
    # save a stray permlevel-1 row either -- `keep` has to carry both.
    policy = dataclasses.replace(
        BOT,
        grants=(
            PermissionGrant("Company", frozenset({"read", "write"})),
            PermissionGrant("Company", frozenset({"read"}), permlevel=1),
        ),
    )

    class _RevokeAwareClient(_FakeClient):
        def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any:
            if dotted_path == f"{_PERMISSION_MANAGER}.get_permissions":
                self.calls.append(dotted_path)
                return [
                    {"parent": "Company", "permlevel": 0},
                    {"parent": "Company", "permlevel": 1},
                    {"parent": "Account", "permlevel": 1},
                ]
            return super().call(dotted_path, params)

    client = _RevokeAwareClient()
    report = provision(client, policy)

    assert report.revoked == ["Account (permlevel 1)"]


# --- the one submit the bot holds -------------------------------------------


def _grants_by_doctype() -> dict[str, frozenset[str]]:
    return {g.doctype: g.ptypes for g in GRANTS}


def test_the_bot_may_submit_bank_transactions_and_no_other_doctype() -> None:
    # A Bank Transaction posts nothing to the general ledger on submit; the
    # posting happens when a person reconciles it. Every other submittable
    # doctype the bot can reach commits money on submit and stays draft-only.
    grants = _grants_by_doctype()
    assert "submit" in grants["Bank Transaction"]
    assert {doctype for doctype, ptypes in grants.items() if "submit" in ptypes} == {"Bank Transaction"}
    assert not any("cancel" in ptypes or "amend" in ptypes for ptypes in grants.values())


def test_the_bot_can_read_books_closed_through_at_permlevel_1() -> None:
    # `runningtab.period.fence` moves Books Closed Through to permission level 1,
    # so the ordinary permlevel-0 Company grant no longer reaches it. The
    # Mercury ingest reads it every run to leave a closed quarter's rows to a
    # person, so the bot needs read at that level too -- read only, so the
    # fence still proves the bot cannot write it.
    permlevel_1 = [g for g in GRANTS if g.doctype == "Company" and g.permlevel == 1]
    assert len(permlevel_1) == 1
    assert permlevel_1[0].ptypes == frozenset({"read"})
    # Frappe rejects a permlevel-1 rule with no permlevel-0 rule beneath it.
    permlevel_0 = [g for g in GRANTS if g.doctype == "Company" and g.permlevel == 0]
    assert len(permlevel_0) == 1


# --- permlevel blindness -----------------------------------------------------
#
# `frappe.client.has_permission` (what `granted_flags_effective` calls) has no
# permlevel of its own: it cannot tell a permlevel-1 DocPerm row's presence
# from a permlevel-0 grant already on the same doctype, so a check that asked
# it about a permlevel-1 grant would always read "effective" -- a false
# green. `missing_grants_report` must never ask, and `skipped_grants` must
# name the grant instead.


def test_missing_grants_report_reports_both_grants_on_one_doctype(monkeypatch) -> None:
    # The exact collapse the false green depended on: a permlevel-0 grant
    # that IS missing, and a permlevel-1 grant on the same doctype, must not
    # share a report key -- one finding must not silently displace the other.
    policy = dataclasses.replace(
        BOT,
        grants=(
            PermissionGrant("Company", frozenset({"read", "write"})),
            PermissionGrant("Company", frozenset({"read"}), permlevel=1),
        ),
    )
    _install(monkeypatch, _BotServer({}, effective={"Company": set()}))
    assert missing_grants_report(_FAKE_CONFIG, policy.grants) == {"Company": ["read", "write"]}


def test_a_permlevel_1_grant_is_never_asked_about_or_called_effective(monkeypatch) -> None:
    policy = dataclasses.replace(BOT, grants=(PermissionGrant("Company", frozenset({"read"}), permlevel=1),))
    asked: list[str] = []

    class _RecordingServer(_FlagServer):
        def granted(self, config, *, doctype, ptypes):
            # What a doctype-level has_permission would say for this grant if
            # it were ever asked: "effective", because Company is readable at
            # permlevel 0 regardless of what the permlevel-1 row says.
            asked.append(doctype)
            return dict.fromkeys(ptypes, True)

    _install(monkeypatch, _RecordingServer({}))
    assert missing_grants_report(_FAKE_CONFIG, policy.grants) == {}
    assert asked == []


def test_skipped_grants_names_the_permlevel_1_grant_never_the_permlevel_0_one() -> None:
    policy = dataclasses.replace(
        BOT,
        grants=(
            PermissionGrant("Company", frozenset({"read", "write"})),
            PermissionGrant("Company", frozenset({"read"}), permlevel=1),
        ),
    )
    assert skipped_grants(policy.grants) == ["Company (permlevel 1)"]


def test_skipped_grants_names_both_the_bots_and_the_readers_permlevel_1_company_grant() -> None:
    assert skipped_grants(BOT.grants) == ["Company (permlevel 1)"]
    assert skipped_grants(READER.grants) == ["Company (permlevel 1)"]


def _probe_refused(monkeypatch) -> None:
    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: SubmitProbe(refused=True, can_create=True, submit_flag=False),
    )


class _BotServer(_FlagServer):
    """A server that honours exactly the flags in `effective`, per doctype, for the granted checks too."""

    def __init__(self, allowed: dict[str, set[str]], effective: dict[str, set[str]] | None = None) -> None:
        super().__init__(allowed)
        self.effective = effective

    def granted(self, config, *, doctype, ptypes):
        if self.effective is None:
            return dict.fromkeys(ptypes, True)
        return {p: p in self.effective.get(doctype, set()) for p in ptypes}


def test_a_bot_allowed_submit_on_bank_transaction_alone_passes(monkeypatch) -> None:
    _probe_refused(monkeypatch)
    _install(monkeypatch, _BotServer({"Bank Transaction": {"submit"}}))
    check = check_credential(_FAKE_CONFIG, "Transpire")
    assert check.passed
    assert check.leaked == {}


def test_a_bot_allowed_submit_on_a_journal_entry_or_invoice_fails(monkeypatch) -> None:
    _probe_refused(monkeypatch)
    _install(monkeypatch, _BotServer({"Bank Transaction": {"submit"}, "Purchase Invoice": {"submit"}}))
    check = check_credential(_FAKE_CONFIG, "Transpire")
    assert not check.passed
    assert check.leaked == {"Purchase Invoice": ["submit"]}


def test_a_bot_allowed_cancel_or_amend_on_bank_transaction_still_fails(monkeypatch) -> None:
    # Submit is the one exception; cancelling or amending a row a person
    # reconciled would undo their work, and stays prohibited.
    _probe_refused(monkeypatch)
    _install(monkeypatch, _BotServer({"Bank Transaction": {"submit", "cancel"}}))
    check = check_credential(_FAKE_CONFIG, "Transpire")
    assert not check.passed
    assert check.leaked == {"Bank Transaction": ["cancel"]}


def test_a_bot_whose_bank_transaction_submit_is_not_effective_fails(monkeypatch) -> None:
    # The grant exists on paper and the server does not honour it: the ingest
    # would then fail on its first row, so verify says so first.
    _probe_refused(monkeypatch)
    # Union rather than a doctype-keyed dict comprehension: Company is granted
    # twice, at permlevel 0 and 1, and collapsing the two would report the
    # permlevel-0 write as not effective too.
    effective: dict[str, set[str]] = {}
    for g in GRANTS:
        effective.setdefault(g.doctype, set()).update(g.ptypes)
    effective["Bank Transaction"] = effective["Bank Transaction"] - {"submit"}
    _install(monkeypatch, _BotServer({}, effective=effective))
    check = check_credential(_FAKE_CONFIG, "Transpire")
    assert not check.passed
    assert check.missing == {"Bank Transaction": ["submit"]}


# --- the forecast the bot may write once and never change ---------------------


def test_the_bot_may_read_and_create_cash_forecasts() -> None:
    # A forecast is a dated record of what the machine predicted. Create lets
    # the bot write a new one; withholding write and delete keeps the record a
    # person later reads exactly as it was written. Child-table rows inherit
    # the parent's permissions, so the three tables need no grant of their own.
    assert _grants_by_doctype()["Cash Forecast"] == frozenset({"read", "create"})


def test_write_and_delete_on_cash_forecast_are_checked_as_prohibited(monkeypatch) -> None:
    _probe_refused(monkeypatch)
    _install(monkeypatch, _BotServer({"Cash Forecast": {"write"}}))
    check = check_credential(_FAKE_CONFIG, "Transpire")
    assert not check.passed
    assert check.leaked == {"Cash Forecast": ["write"]}


def test_a_bot_denied_write_and_delete_on_cash_forecast_passes(monkeypatch) -> None:
    _probe_refused(monkeypatch)
    asked: dict[str, frozenset[str]] = {}

    class _Recording(_BotServer):
        def prohibited(self, config, *, doctype, ptypes):
            asked[doctype] = frozenset(ptypes)
            return super().prohibited(config, doctype=doctype, ptypes=ptypes)

    _install(monkeypatch, _Recording({}))
    assert check_credential(_FAKE_CONFIG, "Transpire").passed
    assert asked["Cash Forecast"] == frozenset({"write", "delete"})
