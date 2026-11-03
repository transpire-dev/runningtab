"""Policy for the books bot: exactly what it may touch, and the one thing it may submit.

The bot's role holds create/write for drafting and the chart of accounts, and
submit on Bank Transaction alone; cancel and amend are written as "0"
everywhere, and so is submit on every other doctype. The server is asked to
confirm all of it before a key is stored.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Protocol

from icedcoffee_sync.frappe.access import (
    ApiKeyPair,
    PermissionGrant,
    RoleChange,
    SubmitProbe,
    UserChange,
    ensure_role,
    ensure_user,
    generate_keys,
    grant,
    granted_flags_effective,
    probe_submit,
    prohibited_flags_allowed,
    revoke_grants_except,
)
from icedcoffee_sync.frappe.client import SiteConfig

from runningtab.forecast.names import CASH_FORECAST

# Which role and user each identity is comes from the books directory's
# `books.toml` (`runningtab.books`); what each may touch is fixed here, and
# `bot_policy`/`reader_policy` put the two together.

# The one doctype the bot may submit. A Bank Transaction posts nothing to the
# general ledger on submit -- ERPNext writes the GL entries when a person
# reconciles it to a voucher -- and the Bank Reconciliation Tool only sees
# submitted rows, so the Mercury ingest submits the rows it creates and
# "machines draft, humans post" still holds in substance. Every other
# submittable doctype commits money on submit and stays draft-only.
BOT_SUBMITS: frozenset[str] = frozenset({"Bank Transaction"})

# Never granted: cancel or amend on any doctype, and submit outside
# BOT_SUBMITS. No DocType grant either: a role permission on DocType does not
# take effect (measured live), and nothing here reads meta.
GRANTS: tuple[PermissionGrant, ...] = (
    PermissionGrant("Account", frozenset({"read", "write", "create", "delete"})),
    PermissionGrant("Company", frozenset({"read", "write"})),
    # `runningtab.period.fence` pushes Books Closed Through and its bypass role to
    # permission level 1, so the ordinary permlevel-0 Company read above no
    # longer reaches them. The Mercury ingest reads `accounts_frozen_till_date`
    # every run to leave a closed quarter's rows to a person, so the bot is
    # granted read at that level too -- read only, so the fence still proves
    # the bot cannot write it.
    PermissionGrant("Company", frozenset({"read"}), permlevel=1),
    PermissionGrant("GL Entry", frozenset({"read"})),
    PermissionGrant("Journal Entry", frozenset({"read", "write", "create"})),
    PermissionGrant("Purchase Invoice", frozenset({"read", "write", "create"})),
    PermissionGrant("Bank Transaction", frozenset({"read", "write", "create", "submit"})),
    PermissionGrant("Supplier", frozenset({"read", "write", "create"})),
    PermissionGrant("File", frozenset({"read", "write", "create"})),
    # A forecast is written once and never changed: create without write or
    # delete. Its child tables inherit the parent's permissions.
    PermissionGrant(CASH_FORECAST, frozenset({"read", "create"})),
)

# Exactly what `coa plan` reads and nothing else: the accounts, the company
# they hang off, and which accounts carry ledger entries.
#
# GL Entry read is a deliberate decision, not an oversight, so that reviews
# stop re-raising it: plan needs only to know WHETHER an account has entries,
# and asking that cheaply (`list("GL Entry", filters=[["account","=",...]],
# limit=1)`) means read on the ledger. Narrowing it would take a whitelisted
# method on the site and a custom Frappe app to hold it. Reading the whole
# ledger is accepted on purpose: the reader is the tooling that keeps these
# books, reading the books it keeps.
READER_GRANTS: tuple[PermissionGrant, ...] = (
    PermissionGrant("Account", frozenset({"read"})),
    PermissionGrant("Company", frozenset({"read"})),
    # `runningtab.period.fence` pushes Books Closed Through and its bypass role to
    # permission level 1, so the ordinary permlevel-0 Company read above no
    # longer reaches them. `status` and `health` still need to read
    # `accounts_frozen_till_date`, so the reader is granted read at that level
    # too -- as the bot is in `GRANTS`, for the Mercury ingest. Removing
    # either grant makes that credential's lock read refuse by name
    # (`runningtab.period.lock.read_visible_lock`).
    PermissionGrant("Company", frozenset({"read"}), permlevel=1),
    PermissionGrant("GL Entry", frozenset({"read"})),
    # Read by `runningtab period export`: the ledger's parties, the fiscal years,
    # and the vouchers whose attachments a private-file download is checked
    # against.
    PermissionGrant("Fiscal Year", frozenset({"read"})),
    PermissionGrant("Customer", frozenset({"read"})),
    PermissionGrant("Journal Entry", frozenset({"read"})),
    PermissionGrant("Purchase Invoice", frozenset({"read"})),
    PermissionGrant("Payment Entry", frozenset({"read"})),
    PermissionGrant("Sales Invoice", frozenset({"read"})),
    PermissionGrant("Bank Transaction", frozenset({"read"})),
    PermissionGrant("File", frozenset({"read"})),
    PermissionGrant("Supplier", frozenset({"read"})),
    # Version ships read to System Manager and Administrator only
    # (frappe/core/doctype/version/version.json); the reader inherits
    # neither, so this is explicit. `runningtab.period.lock.lock_history` reads
    # it to name who moved Books Closed Through and when -- status names who
    # reopened a quarter.
    PermissionGrant("Version", frozenset({"read"})),
)

# Every mutating flag, on every doctype the writing bot can reach. Read access
# is worthless as a boundary unless the absence of write access is proven
# rather than assumed: a role inherited from somewhere else, or a stray grant
# left on the user, would otherwise pass unnoticed.
#
# The list is deliberately wider than the reader's own grants. Checking only
# the doctypes we granted would prove nothing about an inherited permission on
# one we did not, and an inherited permission is the whole thing this check
# exists to catch. Every flag that changes a document is listed -- cancel and
# amend included, which a "can it write" check reads past too easily.
MUTATING_PTYPES: frozenset[str] = frozenset({"write", "create", "delete", "submit", "cancel", "amend"})
READER_PROHIBITED: dict[str, frozenset[str]] = dict.fromkeys(
    (
        "Account",
        "Company",
        "GL Entry",
        "Journal Entry",
        "Purchase Invoice",
        "Bank Transaction",
        "Supplier",
        "File",
        "Fiscal Year",
        "Customer",
        "Payment Entry",
        "Sales Invoice",
        "Version",
    ),
    MUTATING_PTYPES,
)

# What the reader can do that we did not grant and cannot take away here.
#
# MEASURED on the live site 2026-08-27, not inferred: the reader's role grants
# File nothing, the server reports create/write/delete on File as allowed, and
# a real insert SUCCEEDED (then was deleted again). Frappe hands this to
# authenticated users so attachments work; taking it away means editing a
# role's permissions for the whole site, and `DocPerm` is not even readable
# over REST to find out which role supplies it.
#
# Recorded here rather than dropped from the check, so that verification keeps
# reporting it every run instead of quietly narrowing what "read-only" claims.
# It reaches no account, no ledger entry and no company record -- but it is
# write access, and calling the reader "unable to write anything" would be a
# lie.
READER_INHERITED_WRITES: dict[str, frozenset[str]] = {"File": frozenset({"create", "write", "delete"})}


@dataclasses.dataclass(frozen=True)
class Policy:
    """One identity on the books site: its role, its user, and what that role may touch."""

    role: str
    user: str
    first_name: str
    grants: tuple[PermissionGrant, ...]


def bot_policy(role: str, user: str, first_name: str) -> Policy:
    """The writing bot: `GRANTS`, held by the role and user the books name."""
    return Policy(role, user, first_name, GRANTS)


def reader_policy(role: str, user: str, first_name: str) -> Policy:
    """The reader: `READER_GRANTS`, held by the role and user the books name."""
    return Policy(role, user, first_name, READER_GRANTS)


# Frappe permissions are per doctype and per flag: the insert-based probe
# proves submit on Journal Entry only, so every prohibited flag is checked on
# every submittable doctype the bot is granted.
SUBMITTABLE_DOCTYPES: frozenset[str] = frozenset({"Journal Entry", "Purchase Invoice", "Bank Transaction"})
PROHIBITED_PTYPES: frozenset[str] = frozenset({"submit", "cancel", "amend"})

# Cash Forecast is not submittable; what must be refused is changing or
# removing a forecast once the bot has created it.
FORECAST_PROHIBITED: frozenset[str] = frozenset({"write", "delete"})


def prohibited_ptypes_for(doctype: str) -> frozenset[str]:
    """The committing flags the bot must be denied on `doctype`: all three, less submit where it is granted."""
    if doctype in BOT_SUBMITS:
        return PROHIBITED_PTYPES - {"submit"}
    return PROHIBITED_PTYPES


class KeyRotationFailedError(RuntimeError):
    """`generate_keys` failed after it may already have rotated the live secret.

    The call has no dry-run and no read-back, so a timeout looks the same as
    a request that never arrived; assume the rotation landed.
    """


@dataclasses.dataclass(frozen=True)
class ProvisionReport:
    """What `provision` changed, and the bot's freshly minted credential."""

    role_created: bool
    role_desk_access_repaired: bool
    role_disabled_repaired: bool
    grants: dict[str, list[tuple[str, str]]]
    revoked: list[str]
    user: UserChange
    keys: ApiKeyPair


class _ProvisionClient(Protocol):
    """The slice of `Client` provisioning needs."""

    def get(self, doctype: str, name: str) -> Any: ...
    def insert(self, doctype: str, doc: dict[str, Any]) -> Any: ...
    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any: ...
    def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any: ...


def _grant_label(doctype: str, permlevel: int) -> str:
    """`"<doctype>"` at permlevel 0, `"<doctype> (permlevel N)"` otherwise.

    The spelling `revoke_grants_except` already uses for a revoked row. Every
    dict this module keys per grant uses it too: two grants on the same
    doctype at different permlevels -- the bot's and the reader's Company
    grants both are, now -- must never collapse onto one key and silently
    drop one of the two findings.
    """
    return doctype if permlevel == 0 else f"{doctype} (permlevel {permlevel})"


def provision(client: _ProvisionClient, policy: Policy) -> ProvisionReport:
    """Create or repair `policy`'s role and user to exactly what it describes, and mint a fresh key.

    Idempotent up to the key: grants are rewritten in full, stray grants and
    roles revoked, a disabled role or user re-enabled. A role without desk
    access makes the user a Website User, which the REST API serves fine.
    """
    role: RoleChange = ensure_role(client, policy.role)
    sent = {_grant_label(g.doctype, g.permlevel): grant(client, policy.role, g) for g in policy.grants}
    revoked = revoke_grants_except(client, policy.role, keep=frozenset((g.doctype, g.permlevel) for g in policy.grants))
    user = ensure_user(client, policy.user, first_name=policy.first_name, roles=frozenset({policy.role}))
    try:
        keys = generate_keys(client, policy.user)
    except Exception as exc:
        raise KeyRotationFailedError(str(exc)) from exc
    return ProvisionReport(
        role_created=role.created,
        role_desk_access_repaired=role.desk_access_repaired,
        role_disabled_repaired=role.disabled_repaired,
        grants=sent,
        revoked=revoked,
        user=user,
        keys=keys,
    )


def prohibited_flags_report(config: SiteConfig) -> dict[str, list[str]]:
    """{doctype: [flag, ...]} for every prohibited flag the server still allows this credential.

    Submit on Bank Transaction is granted, not prohibited, and `missing_grants_report`
    is what proves the server honours it. Cash Forecast is checked for write and
    delete, which the bot must never hold.
    """
    leaked: dict[str, list[str]] = {}
    for doctype in sorted(SUBMITTABLE_DOCTYPES):
        flags = prohibited_flags_allowed(config, doctype=doctype, ptypes=prohibited_ptypes_for(doctype))
        allowed = sorted(ptype for ptype, value in flags.items() if value)
        if allowed:
            leaked[doctype] = allowed
    flags = prohibited_flags_allowed(config, doctype=CASH_FORECAST, ptypes=FORECAST_PROHIBITED)
    if allowed := sorted(ptype for ptype, value in flags.items() if value):
        leaked[CASH_FORECAST] = allowed
    return leaked


def missing_grants_report(config: SiteConfig, grants: tuple[PermissionGrant, ...] = GRANTS) -> dict[str, list[str]]:
    """{label: [flag, ...]} for every permlevel-0 granted flag the server does not honour.

    A grant above permlevel 0 is left out entirely here -- never asked about,
    never reported missing. `granted_flags_effective` asks
    `frappe.client.has_permission`, which has no permlevel of its own: it
    cannot tell a permlevel-1 DocPerm row's presence from a permlevel-0 grant
    already on the same doctype, so treating its answer as proof for a
    permlevel-1 grant would be a check that always reads "effective" whether
    or not the row exists -- a false green. `skipped_grants` names these
    instead, and what actually proves them is a live round trip of the
    field's value through the credential (`tests/test_live_fence.py`), not a
    doctype-level yes/no.
    """
    missing: dict[str, list[str]] = {}
    for g in grants:
        if g.permlevel > 0:
            continue
        flags = granted_flags_effective(config, doctype=g.doctype, ptypes=g.ptypes)
        not_effective = sorted(ptype for ptype, value in flags.items() if not value)
        if not_effective:
            missing[_grant_label(g.doctype, g.permlevel)] = not_effective
    return missing


def skipped_grants(grants: tuple[PermissionGrant, ...] = GRANTS) -> list[str]:
    """Labels of the grants `missing_grants_report` cannot check -- see its docstring.

    Static over `grants` alone: which grants sit above permlevel 0 is known
    before anything is asked of the server, so this makes no network call.
    """
    return [_grant_label(g.doctype, g.permlevel) for g in grants if g.permlevel > 0]


def reader_prohibited_report(config: SiteConfig) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Which mutating flags the server still allows a reader: (unexpected, known-inherited).

    Split rather than merged so the known ones stay visible on every run
    without failing it, and anything new fails immediately instead of hiding
    among them.
    """
    leaked: dict[str, list[str]] = {}
    inherited: dict[str, list[str]] = {}
    for doctype, ptypes in sorted(READER_PROHIBITED.items()):
        flags = prohibited_flags_allowed(config, doctype=doctype, ptypes=ptypes)
        allowed = {ptype for ptype, value in flags.items() if value}
        known = READER_INHERITED_WRITES.get(doctype, frozenset())
        if expected := sorted(allowed & known):
            inherited[doctype] = expected
        if unexpected := sorted(allowed - known):
            leaked[doctype] = unexpected
    return leaked, inherited


def check_reader_credential(config: SiteConfig) -> CredentialCheck:
    """Prove a reader credential can read what plan needs and cannot write anything.

    No submit probe: the probe works by inserting a Journal Entry, which a
    reader may not create at all, so its refusal would prove nothing about
    submit. Asking the server which flags it honours is the whole check here,
    and `probe` is recorded as refused because a credential that cannot create
    cannot submit either.

    Raises `CredentialCheckError` (naming the stage) when a check cannot run.
    """
    try:
        leaked, inherited = reader_prohibited_report(config)
    except Exception as exc:
        raise CredentialCheckError("prohibited", exc) from exc
    probe = SubmitProbe(refused=True, can_create=False, submit_flag=False)
    if leaked:
        return CredentialCheck(probe=probe, leaked=leaked, inherited=inherited)
    try:
        missing = missing_grants_report(config, READER_GRANTS)
    except Exception as exc:
        raise CredentialCheckError("grants", exc) from exc
    return CredentialCheck(probe=probe, leaked=leaked, missing=missing, inherited=inherited)


class CredentialCheckError(RuntimeError):
    """A credential check could not run; `stage` says which one."""

    def __init__(self, stage: str, cause: Exception) -> None:
        super().__init__(f"{stage}: {cause}")
        self.stage = stage
        self.cause = cause


@dataclasses.dataclass(frozen=True)
class CredentialCheck:
    """The server's verdict on a credential, stage by stage; later stages are None once one fails."""

    probe: SubmitProbe
    leaked: dict[str, list[str]] | None = None
    missing: dict[str, list[str]] | None = None
    # Allowed, expected, and not a failure -- see READER_INHERITED_WRITES.
    inherited: dict[str, list[str]] | None = None

    @property
    def passed(self) -> bool:
        """True only when the probe's submit was refused, no prohibited flag leaked, and every grant is effective.

        For the bot that means submit denied on Journal Entry and Purchase
        Invoice, cancel and amend denied everywhere, and submit on Bank
        Transaction actually honoured -- a grant on paper the server ignores
        would fail the ingest on its first row.
        """
        return self.probe.refused and self.leaked == {} and self.missing == {}


def check_credential(config: SiteConfig, company: str) -> CredentialCheck:
    """Prove a credential is scoped as policy says, stopping at the first stage that fails.

    Raises `CredentialCheckError` (naming the stage) when a check cannot run;
    an `OrphanProbeDocumentError` from the probe propagates as itself.
    """
    from icedcoffee_sync.frappe.access import OrphanProbeDocumentError

    try:
        probe = probe_submit(config, company=company)
    except OrphanProbeDocumentError:
        raise
    except Exception as exc:
        raise CredentialCheckError("probe", exc) from exc
    if not probe.refused:
        return CredentialCheck(probe=probe)
    try:
        leaked = prohibited_flags_report(config)
    except Exception as exc:
        raise CredentialCheckError("prohibited", exc) from exc
    if leaked:
        return CredentialCheck(probe=probe, leaked=leaked)
    try:
        missing = missing_grants_report(config)
    except Exception as exc:
        raise CredentialCheckError("grants", exc) from exc
    return CredentialCheck(probe=probe, leaked=leaked, missing=missing)
