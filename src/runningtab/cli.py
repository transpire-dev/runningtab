"""`runningtab` -- keeping a set of books in ERPNext: typer wiring and printing.

The work happens in `runningtab.coa` and `runningtab.access`; this module resolves
targets, asks the operator, renders outcomes and picks exit codes. There is
no default target: reaching the ledger means naming it.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import difflib
import functools
import getpass
import hashlib
import pathlib
import re
import sys
import tomllib
import urllib.parse
from collections.abc import Callable, Generator
from contextlib import contextmanager
from decimal import Decimal
from typing import Annotated, NoReturn

import httpx2
import typer
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError
from icedcoffee_sync.apply.diff import ChartPlan
from icedcoffee_sync.apply.model import DesiredAccount
from icedcoffee_sync.apply.prune import PrunePlan, PruneReport
from icedcoffee_sync.erpnext.accounts import company_abbr, document_name
from icedcoffee_sync.erpnext.company import UnknownCompanyFieldError
from icedcoffee_sync.frappe.access import PROBE_DOCTYPE, OrphanProbeDocumentError, SubmitProbe
from icedcoffee_sync.frappe.client import Client, SiteConfig
from icedcoffee_sync.frappe.errors import FrappeError, PermissionError_, ValidationError

from runningtab import coa, health
from runningtab.access import (
    BOT_SUBMITS,
    READER_GRANTS,
    CredentialCheck,
    CredentialCheckError,
    KeyRotationFailedError,
    Policy,
    ProvisionReport,
    check_credential,
    check_reader_credential,
    missing_grants_report,
    provision,
    skipped_grants,
)
from runningtab.books import Books, BooksError, resolve_books
from runningtab.chart import (
    ChartError,
    load_chart,
    load_chart_document,
    load_chart_entries,
    parse_chart,
    parse_defaults,
    suspense_account,
)
from runningtab.coa import Context, PlanChangedError, PruneOutcome
from runningtab.doc import render_doc
from runningtab.forecast.compute import Forecast, compute
from runningtab.forecast.doctype import install_doctypes, is_installed, write_forecast
from runningtab.forecast.model import ForecastFileError, load_forecast
from runningtab.forecast.names import CASH_FORECAST
from runningtab.forecast.sources import SourceError, ledger_through, maintenance_message, opening_cash
from runningtab.gl_codes import gl_code_csv
from runningtab.mercury.categories import (
    CategoryPlan,
    MercuryCategoryWriter,
    UnmanagedCategory,
    apply_plan,
    carried_categories,
    desired_categories,
    live_flags,
    plan_categories,
)
from runningtab.mercury.client import BASE_URLS, MercuryClient, MercuryEnv, MercuryError
from runningtab.mercury.ingest import IngestReport, read_everything, run_snapshot, run_sync
from runningtab.mercury.mapping import MappingError, load_mapping
from runningtab.mercury.posting import (
    BANK,
    PostingError,
    bank_account_documents,
    install_bank_accounts,
    install_custom_fields,
)
from runningtab.mercury.receipts import ReceiptFetcher
from runningtab.mercury.session import posting_context
from runningtab.mercury.state import (
    RETENTION,
    IngestState,
    NoCursorError,
    StaleCursorError,
    StateIdentityError,
    default_state_path,
)
from runningtab.mercury.vouchers import JOURNAL_ENTRY, Chart
from runningtab.period import PeriodError
from runningtab.period.balances import trial_balance
from runningtab.period.bank import read_ledgers as read_mercury_ledgers
from runningtab.period.close import Built, CloseSteps, Held, HeldForReview, close_quarter, reclose_steps, reopen_step
from runningtab.period.crosscheck import bind_crosscheck
from runningtab.period.documents import (
    quarter_vouchers,
    read_bank_transactions,
    read_documents,
    refuse_missing_bank_transactions,
    refuse_unreadable_doctypes,
    refuse_unreadable_vouchers,
)
from runningtab.period.fence import LOCK_FIELDS, FenceProof, check_fence_probe, install_fence, prove_fence
from runningtab.period.ledger import GLRow, index_accounts, read_fiscal_years, read_gl
from runningtab.period.lock import lock_history, read_lock, read_visible_lock, set_closed_through
from runningtab.period.manifest import ManifestFacts, render_manifest
from runningtab.period.open_items import open_items
from runningtab.period.package import Package, PackageInputs, build_package, sha256sums, write_package
from runningtab.period.qbo_map import load_qbo_map
from runningtab.period.quarter import FiscalYear, Quarter, fiscal_year_for
from runningtab.period.readiness import Blocker, read_blockers
from runningtab.period.record import (
    GcsRecord,
    SuspectPackage,
    latest,
    next_prefix,
    read_lock_version,
    read_sums,
    revision_of,
    stored_packages,
    upload_package,
)
from runningtab.period.status import StatusReport, closed_periods_check, period_status
from runningtab.site import (
    Profile,
    SiteCredentialError,
    Target,
    admin_config,
    ingest_config,
    mercury_config,
    mercury_write_config,
    site_config,
    store_books_credential,
)

app = typer.Typer(help="Keep a company's books in ERPNext.", no_args_is_help=True)
coa_app = typer.Typer(help="Chart of accounts.", no_args_is_help=True)
app.add_typer(coa_app, name="coa")
chart_app = typer.Typer(help="The chart.coa outline file and the doc generated from it.", no_args_is_help=True)
app.add_typer(chart_app, name="chart")
access_app = typer.Typer(help="Bot-user provisioning and submit-permission verification.", no_args_is_help=True)
app.add_typer(access_app, name="access")
mercury_app = typer.Typer(
    help=(
        "Read the bank, post each statement line into the ledger as a Bank Transaction. "
        "The ingest is read-only; 'categories apply' is the one command here that writes to Mercury."
    ),
    no_args_is_help=True,
)
app.add_typer(mercury_app, name="mercury")
categories_app = typer.Typer(
    help=(
        "Mirror the chart's codeable accounts as Mercury's custom categories. Never deletes one a transaction carries."
    ),
    no_args_is_help=True,
)
mercury_app.add_typer(categories_app, name="categories")

TargetOption = Annotated[Target, typer.Option("--target", help="Which site to act on.")]
CompanyOption = Annotated[str | None, typer.Option("--company", help="Company name; defaults to the site's only one.")]
YesOption = Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")]

# Tests patch these names on this module.
_Context = Context


@dataclasses.dataclass
class _BooksChoice:
    """What `--books` said, and the books it named once something asked for them."""

    flag: pathlib.Path | None
    loaded: Books | None = None


@app.callback()
def main(
    ctx: typer.Context,
    books: Annotated[
        pathlib.Path | None,
        typer.Option(
            "--books",
            help=(
                "The books directory: chart.coa, mercury.toml, forecast.toml, qbo.toml and books.toml. "
                "Or set RUNNINGTAB_BOOKS. There is no default."
            ),
            show_default=False,
        ),
    ] = None,
) -> None:
    """Keep a company's books in ERPNext: the chart, the bank feed, the forecast, the quarter close."""
    ctx.obj = _BooksChoice(books)


def _books(ctx: typer.Context) -> Books:
    """The books this command acts on, loaded once per invocation; refuses, naming the flag and variable, if none.

    Every command calls this before doing anything else, so a command never
    gets halfway before discovering it does not know whose books it is
    keeping.
    """
    choice = ctx.find_root().obj
    if not isinstance(choice, _BooksChoice):
        raise TypeError("runningtab's commands run under its `main` callback, which records --books")
    if choice.loaded is None:
        try:
            choice.loaded = resolve_books(choice.flag)
        except BooksError as exc:
            raise typer.BadParameter(str(exc), param_hint="--books") from exc
    return choice.loaded


def _sole_company(client: Client) -> str:
    try:
        return coa.sole_company(client)
    except coa.NoSoleCompanyError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _plan_against(books: Books, target: Target, company: str | None, *, readonly: bool = False) -> Context:
    config = site_config(books.site, target, readonly=readonly)
    typer.echo(f"site: {config.url}")
    chart_text = books.chart_path.read_text(encoding="utf-8")
    try:
        return coa.plan_against(config, company, parse_chart(chart_text), parse_defaults(chart_text))
    except coa.NoSoleCompanyError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _confirm(yes: bool, prompt: str) -> None:
    if not (yes or typer.confirm(prompt)):
        typer.echo("aborted")
        raise typer.Exit(code=1)


# --- coa ------------------------------------------------------------------


def _print_plan(plan: ChartPlan) -> None:
    typer.echo(f"create   {len(plan.creates):4}")
    for change in plan.creates:
        typer.echo(f"  + {change.desired.number} {change.desired.name}")
    # Split for the reader, not for the tool: both are one ERPNext rename, but
    # a moved number moves the GL code the bank shows and a moved name does not.
    renumbers = [c for c in plan.renames if c.changes_number]
    renames = [c for c in plan.renames if not c.changes_number]
    typer.echo(f"renumber {len(renumbers):4}  (the account's GL code moves)")
    for change in renumbers:
        typer.echo(f"  # {change.live.document_name} -> {change.desired.number} {change.desired.name}")
    typer.echo(f"rename   {len(renames):4}  (same number, new label)")
    for change in renames:
        typer.echo(f"  ~ {change.live.document_name} -> {change.desired.name}")
    typer.echo(f"retype   {len(plan.retypes):4}")
    for change in plan.retypes:
        typer.echo(f"  ~ {change.live.document_name} account_type={change.desired.account_type}")
    typer.echo(f"enable   {len(plan.enables):4}  (chart wants this account; the site has it disabled)")
    for change in plan.enables:
        typer.echo(f"  ~ {change.live.document_name}")
    typer.echo(f"extra    {len(plan.extras):4}  (on the site, not in the chart; left alone)")
    for extra in plan.extras:
        typer.echo(f"  ? {extra.live.document_name}")
    if plan.refusals:
        typer.echo(f"REFUSED  {len(plan.refusals):4}")
        for refusal in plan.refusals:
            name, parent = refusal.key
            where = f"{name!r} under {parent!r}" if parent else f"{name!r} (root)"
            typer.echo(f"  ! {where} {refusal.reason}: {refusal.detail}")


def _print_prune_plan(plan: PrunePlan) -> None:
    typer.echo(f"delete   {len(plan.deletes):4}")
    for change in plan.deletes:
        typer.echo(f"  - {change.live.document_name}")
    if plan.refusals:
        typer.echo(f"REFUSED  {len(plan.refusals):4}")
        for refusal in plan.refusals:
            typer.echo(f"  ! {refusal.reason}: {refusal.detail}")


def _print_prune_report(report: PruneReport) -> None:
    summary = f"deleted {len(report.deleted)}, disabled {len(report.disabled)}, failed {len(report.failed)}"
    if report.already_disabled:
        summary += f", already-disabled {len(report.already_disabled)}"
    typer.echo(summary)
    for name, message in report.failed:
        typer.echo(f"  ! {name}: {message}")


def _print_prune_outcome(outcome: PruneOutcome) -> None:
    if outcome.cleared_defaults:
        typer.echo(f"cleared Company defaults: {', '.join(outcome.cleared_defaults)}")
    _print_prune_report(outcome.report)
    if outcome.parking is None:
        return
    for name in outcome.parking.gained_entries:
        typer.echo(f"not parking {name}: gained ledger entries since planning")
    for p in outcome.parking.parked:
        typer.echo(f"parked {p.old_document_name} -> {p.parked_number}")


@coa_app.command("plan")
def coa_plan(
    ctx: typer.Context,
    target: TargetOption,
    company: CompanyOption = None,
    require_empty: Annotated[
        bool,
        typer.Option(
            "--require-empty",
            help="Exit non-zero unless the site already matches the chart. For CI, after an apply.",
        ),
    ] = False,
) -> None:
    """Show what applying the chart would change. Writes nothing.

    Runs as the reader, whose credential the server refuses every write for.
    That is what lets this run on a merge request, where the branch under
    review decides what code the job executes: "plan does not write" is a
    property of this command, and a credential that cannot write is a
    property the server enforces.
    """
    context = _plan_against(_books(ctx), target, company, readonly=True)
    with context.client:
        _print_plan(context.plan)
    if not context.plan.is_applyable:
        raise typer.Exit(code=1)
    if not context.plan.is_empty:
        if require_empty:
            typer.echo("site does not match the chart")
            raise typer.Exit(code=1)
        return
    typer.echo("site already matches the chart")


@coa_app.command("apply")
def coa_apply(ctx: typer.Context, target: TargetOption, company: CompanyOption = None, yes: YesOption = False) -> None:
    """Apply the chart, then point Company's defaults at it.

    Confirmation gates the whole command, since the defaults write reaches
    the ledger even when no account changes. The plan is re-made after the
    prompt and refused if it changed while the prompt sat open.
    """
    books = _books(ctx)
    context = _plan_against(books, target, company)
    with context.client:
        _print_plan(context.plan)
        if not context.plan.is_applyable:
            typer.echo("refusing to apply: resolve the refusals above first")
            raise typer.Exit(code=1)
        _confirm(yes, f"apply to {context.company}?")

        fresh = _plan_against(books, target, company)
        fresh.client.close()
        try:
            outcome = coa.apply_confirmed(context, fresh.plan)
        except PlanChangedError:
            typer.echo("refusing to apply: the chart plan changed since planning; re-run `coa apply` to review it")
            _print_plan(fresh.plan)
            raise typer.Exit(code=1) from None
        except UnknownCompanyFieldError as exc:
            typer.echo(f"refusing to apply: {exc}")
            raise typer.Exit(code=1) from exc

        if outcome.report is None:
            typer.echo("nothing to apply")
        else:
            report = outcome.report
            summary = (
                f"created {len(report.created)}, renamed {len(report.renamed)}, retyped {len(report.retyped)}, "
                f"enabled {len(report.enabled)}"
            )
            if report.parked:
                summary += f", parked {len(report.parked)}"
            typer.echo(summary)
        typer.echo(f"company defaults set: {', '.join(outcome.defaults_written)}")


def _guard_unapplied_chart(plan: ChartPlan, allow_unapplied: bool) -> None:
    if plan.is_empty:
        return
    if not allow_unapplied:
        typer.echo("refusing to prune: the chart above is not yet applied; run `runningtab coa apply` first")
        raise typer.Exit(code=1)
    typer.echo(
        "WARNING: pruning with the chart unapplied. Company defaults pointing at accounts "
        "this run deletes are cleared first; running `runningtab coa apply` afterwards is what "
        "sets them to their replacements."
    )


def _recheck_prune_plan_after_confirmation(
    books: Books, target: Target, company: str | None, context: Context, prune_plan: PrunePlan
) -> PrunePlan:
    """Re-plan after the prompt; refuse if the chart or the prune plan changed meanwhile."""
    fresh = _plan_against(books, target, company)
    fresh_prune_plan = coa.prune_plan_for(fresh)
    if fresh.plan == context.plan and fresh_prune_plan == prune_plan:
        return prune_plan
    typer.echo("refusing to prune: the chart or prune plan changed since planning; re-run `coa prune` to review it")
    _print_plan(fresh.plan)
    _print_prune_plan(fresh_prune_plan)
    raise typer.Exit(code=1)


@coa_app.command("prune")
def coa_prune(
    ctx: typer.Context,
    target: TargetOption,
    company: CompanyOption = None,
    yes: YesOption = False,
    allow_unapplied: Annotated[
        bool,
        typer.Option(
            "--allow-unapplied",
            help=(
                "Prune even though `coa apply` has not been run. For retiring an existing chart "
                "(e.g. ERPNext's sample chart) before applying ours, when the two collide on "
                "account numbers and apply-first is impossible. Does not bypass the blocking-"
                "refusal check (ROOT_NOT_UNIQUE, AMBIGUOUS_MATCH)."
            ),
        ),
    ] = False,
) -> None:
    """Delete accounts the chart does not describe -- the only destructive command.

    Refuses when the chart plan carries a refusal that could make the extras
    set wrong, or (without `--allow-unapplied`) when the chart is not yet
    applied. Under the flag it also clears Company defaults pointing at
    accounts it deletes and parks survivors off the chart's numbers, since
    ERPNext keeps some sample accounts alive by name until apply repoints
    the defaults. Parking is a renumber, so it needs the prompt too.
    """
    books = _books(ctx)
    context = _plan_against(books, target, company)
    with context.client:
        _print_plan(context.plan)
        if coa.blocking_prune_refusals(context.plan.refusals):
            typer.echo("refusing to prune: resolve the refusals above first")
            raise typer.Exit(code=1)
        if context.plan.refusals:
            typer.echo(f"{len(context.plan.refusals)} refusal(s) above do not affect prune's extras set; proceeding")
        _guard_unapplied_chart(context.plan, allow_unapplied)

        prune_plan = coa.prune_plan_for(context)
        _print_prune_plan(prune_plan)
        if prune_plan.is_empty:
            typer.echo("nothing to prune")
            return
        parkable = allow_unapplied and bool(prune_plan.refusals)
        if not prune_plan.deletes and not parkable:
            typer.echo("nothing to delete; see the refusals above")
            return
        if prune_plan.deletes:
            prompt = f"delete {len(prune_plan.deletes)} account(s) from {context.company}?"
        else:
            typer.echo("nothing to delete; see the refusals above")
            prompt = f"attempt to park colliding survivors among the refusals above, from {context.company}?"
        _confirm(yes, prompt)

        prune_plan = _recheck_prune_plan_after_confirmation(books, target, company, context, prune_plan)
        outcome = coa.prune_confirmed(context, prune_plan, allow_unapplied=allow_unapplied)
        _print_prune_outcome(outcome)
        if outcome.failed:
            raise typer.Exit(code=1)


@coa_app.command("export-gl-codes")
def coa_export_gl_codes(
    ctx: typer.Context,
    output: Annotated[pathlib.Path | None, typer.Option("--output", help="Write here instead of stdout.")] = None,
) -> None:
    """Emit the single-column CSV the bank accepts. The upload is manual."""
    csv = gl_code_csv(load_chart(_books(ctx).chart_path))
    if output is None:
        sys.stdout.write(csv)
        return
    output.write_text(csv)
    typer.echo(f"wrote {output}")
    typer.echo("upload it through the bank's dashboard; there is no API for this")


# --- chart ----------------------------------------------------------------


@chart_app.command("render-doc")
def chart_render_doc(
    ctx: typer.Context,
    check: Annotated[
        bool,
        typer.Option("--check", help="Exit 1 and print a diff instead of writing, for a CI-style gate."),
    ] = False,
) -> None:
    """Regenerate the chart doc's account tables (books.toml's chart_doc) from chart.coa."""
    books = _books(ctx)
    doc_path = books.chart_doc
    if not doc_path.is_file():
        raise typer.BadParameter(f"{doc_path} (books.toml's chart_doc) does not exist", param_hint="--books")
    try:
        chart = load_chart_document(books.chart_path)
    except ChartError as exc:
        typer.echo(f"chart.coa: {exc}")
        raise typer.Exit(code=1) from exc
    original = doc_path.read_text(encoding="utf-8")
    try:
        rendered = render_doc(original, chart)
    except ChartError as exc:
        typer.echo(f"{doc_path}: {exc}")
        raise typer.Exit(code=1) from exc
    if rendered == original:
        typer.echo(f"{doc_path} is already up to date")
        return
    if check:
        diff_lines = difflib.unified_diff(
            original.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=str(doc_path),
            tofile=f"{doc_path} (generated)",
        )
        sys.stdout.writelines(diff_lines)
        raise typer.Exit(code=1)
    doc_path.write_text(rendered, encoding="utf-8")
    typer.echo(f"{doc_path} updated")


# --- access ---------------------------------------------------------------


def _format_flag(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "allowed" if value else "denied"


def _report_refused(probe: SubmitProbe) -> None:
    typer.echo("submit refused by the server")
    typer.echo(f"  server-evaluated: create={_format_flag(probe.can_create)} submit={_format_flag(probe.submit_flag)}")
    if probe.error is not None:
        typer.echo(f"  submit attempt: {probe.error.message}")


def _report_cannot_submit(probe: SubmitProbe) -> None:
    if probe.can_create is False:
        typer.echo(
            f"  the credential cannot create {PROBE_DOCTYPE} drafts, so the submit refusal proves nothing; not storing"
        )
    elif probe.document_name is not None:
        typer.echo(f"  the probe document actually posted: {probe.document_name} -- this is an incident")
    elif isinstance(probe.error, ValidationError):
        typer.echo(f"  submit permission passed; validation stopped it instead: {probe.error.message}")
    elif isinstance(probe.error, PermissionError_):
        typer.echo(
            f"  the submit-flagged insert was refused ({probe.error.message}), but the doctype-level check said "
            f"submit={_format_flag(probe.submit_flag)} -- the two disagree; not storing on an inconsistent answer"
        )
    elif probe.error is not None:
        typer.echo(
            f"  the submit-flagged insert failed inconclusively, neither a permission nor a validation "
            f"answer: {probe.error.message}"
        )


def _report_flags(flags: dict[str, list[str]], verdict: str) -> None:
    typer.echo("  server-evaluated, per doctype:")
    for doctype, ptypes in sorted(flags.items()):
        typer.echo(f"    {doctype}: {', '.join(ptypes)}={verdict}")


def _report_provision_result(report: ProvisionReport) -> None:
    typer.echo(f"role created: {report.role_created}")
    if report.role_desk_access_repaired:
        typer.echo("  desk access repaired")
    if report.role_disabled_repaired:
        typer.echo("  role re-enabled")
    for doctype, sent in report.grants.items():
        typer.echo(f"  {doctype}: " + ", ".join(f"{ptype}={value}" for ptype, value in sent))
    if report.revoked:
        typer.echo(f"grants revoked: {', '.join(sorted(report.revoked))}")
    typer.echo(f"user created: {report.user.created}")
    if report.user.added_roles:
        typer.echo(f"  roles added: {', '.join(sorted(report.user.added_roles))}")
    if report.user.removed_roles:
        typer.echo(f"  roles removed: {', '.join(sorted(report.user.removed_roles))}")
    if report.user.enabled_repaired:
        typer.echo("  user re-enabled")


def _report_fence_install(results: list[tuple[str, str]]) -> None:
    typer.echo("fence:")
    for label, outcome in results:
        typer.echo(f"  {outcome:9} {label}")


def _report_fence_proof(proof: FenceProof) -> None:
    typer.echo("fence proof (the bot's key attempted each write; an administrator read it back):")
    for field, refused, how in (
        (LOCK_FIELDS[0], proof.date_refused, proof.date_how),
        (LOCK_FIELDS[1], proof.role_refused, proof.role_how),
    ):
        typer.echo(f"  {field}: {'refused' if refused else 'NOT REFUSED'} ({how})")
    if proof.restored:
        typer.echo(f"  restored by the administrator: {', '.join(proof.restored)}")


def _warn_if_rotation_left_books_broken(target: Target, profile: Profile = Profile.BOT) -> None:
    # generate_keys rotates the live secret unconditionally; only the books
    # target persists a credential that is now stale. Naming the profile
    # matters: the recovery command differs, and re-running the default would
    # rotate the bot while leaving the reader broken.
    if target is not Target.BOOKS:
        return
    flag = " --profile reader" if profile is Profile.READER else ""
    typer.echo(
        f"the {target.value} {profile.value}'s live secret has already rotated and no longer matches "
        f"the stored credential; re-run `runningtab access provision --target {target.value}{flag}` now to "
        "restore a working one"
    )


def _fail_after_rotation(target: Target, reason: str, profile: Profile = Profile.BOT) -> NoReturn:
    typer.echo(f"FAILED: {reason}")
    _warn_if_rotation_left_books_broken(target, profile)
    raise typer.Exit(code=1)


_CHECK_FAILURE = {
    "probe": "the submit probe itself failed",
    "prohibited": "checking prohibited permissions on other doctypes failed",
    "grants": "checking granted permissions failed",
}


def _report_inherited(check: CredentialCheck) -> None:
    """Say what the reader can still write that we never granted it.

    Printed on a pass as well as a failure: it is the one thing about this
    credential that "read-only" does not cover, and it should not need a
    failure to become visible.
    """
    if not check.inherited:
        return
    typer.echo("inherited (not granted by us, not removable from here):")
    for doctype, flags in sorted(check.inherited.items()):
        typer.echo(f"    {doctype}: {', '.join(flags)}=allowed")


def _report_check_failure(check: CredentialCheck, *, storing: bool) -> None:
    """Explain a failed credential check; `storing` selects the provision wording."""
    if not check.probe.refused:
        if storing:
            typer.echo("REFUSING to store: the bot's fresh credential was NOT refused when it tried to submit")
        elif check.probe.document_name is not None:
            typer.echo("the current credential CAN submit")
        else:
            typer.echo("submit denial could not be proven")
        _report_cannot_submit(check.probe)
    elif check.leaked:
        typer.echo(
            "REFUSING to store: submit was refused on Journal Entry, but the fresh credential still "
            "allows a prohibited flag on another granted doctype"
            if storing
            else "submit refused on Journal Entry, but a prohibited flag is allowed on another granted doctype"
        )
        _report_flags(check.leaked, "allowed")
    elif check.missing:
        typer.echo(
            "REFUSING to store: submit was refused and no prohibited flag leaked, but a positive grant "
            "the bot needs is not actually effective on the server"
            if storing
            else "submit refused, but a positive grant the bot needs is not effective on the server"
        )
        _report_flags(check.missing, "not effective")


ProfileOption = Annotated[
    Profile, typer.Option("--profile", help="Which identity to provision: the writing bot, or the reader.")
]


def _report_skipped_grants(policy: Policy) -> None:
    """Name any grant above permlevel 0 that this command's own check cannot prove.

    `missing_grants_report` leaves these out rather than fake-check them with
    `has_permission`, which cannot see permlevel at all -- see its docstring.
    Printed unconditionally, not just on a failure, so a person reading a
    passing run still learns the pass did not cover everything.
    """
    labels = skipped_grants(policy.grants)
    if not labels:
        return
    typer.echo("not checked here (has_permission cannot see permlevel > 0; see tests/test_live_fence.py):")
    for label in labels:
        typer.echo(f"  {label}")


def _announce_policy(policy: Policy, profile: Profile) -> None:
    typer.echo(f"role: {policy.role}")
    typer.echo(f"user: {policy.user}")
    for g in policy.grants:
        typer.echo(f"  {g.doctype}: {', '.join(sorted(g.ptypes))}")
    if profile is Profile.READER:
        typer.echo("  never: write, create, delete, submit (on any doctype)")
    else:
        typer.echo(f"  submit only on {', '.join(sorted(BOT_SUBMITS))} (posts nothing until a person reconciles it)")
        typer.echo("  never: cancel, amend (on any doctype); never submit anything else")


def _verify_minted(profile: Profile, config: SiteConfig, company: str, target: Target) -> CredentialCheck:
    """Ask the server what the fresh key can actually do, and report the headline."""
    reading = profile is Profile.READER
    try:
        check = check_reader_credential(config) if reading else check_credential(config, company)
    except OrphanProbeDocumentError as exc:
        _fail_after_rotation(
            target,
            f"the create-access probe created a real {exc.doctype} document ({exc.name}) that could not be "
            f"cleaned up ({exc.cleanup_error}) -- a human must delete or investigate it on the live site",
            profile,
        )
    except CredentialCheckError as exc:
        _fail_after_rotation(target, f"{_CHECK_FAILURE[exc.stage]}: {exc.cause}", profile)

    if profile is Profile.READER:
        _report_inherited(check)
        typer.echo("refused: every mutating flag on the ledger denied by the server")
    elif check.probe.refused:
        _report_refused(check.probe)
    return check


def _install_and_check_fence(client: Client, company: str, probe_role: str) -> list[tuple[str, str]]:
    """Fence the lock fields and refuse an unprovable probe -- both before the bot's key is minted.

    Idempotent and reversible, so -- like provision()'s own grants -- this
    runs before the one irreversible step, the key rotation: a failure here
    must never look like the live secret already rotated. The proof after
    minting repeats the probe check; running it here first keeps an
    unprovable probe from costing a freshly rotated key.
    """
    results = install_fence(client, probe_role)
    try:
        check_fence_probe(client, company, probe_role)
    except PeriodError as exc:
        typer.echo(f"refused: {exc} (no key was minted; the stored credential is unchanged)", err=True)
        raise typer.Exit(code=1) from exc
    return results


def _refuse_missing_doctypes(client: Client, policy: Policy, site_url: str, target: Target) -> None:
    """Refuse a policy that grants a doctype the site lacks.

    Runs before anything is written: after the confirmation prompt for
    `access provision`, and before the checks for `access verify`, which has
    no prompt.

    Granting permission on a doctype that does not exist fails midway, after
    earlier grants were written; a plain sentence up front leaves the site
    untouched.
    """
    if any(g.doctype == CASH_FORECAST for g in policy.grants) and not is_installed(client):
        typer.echo(_doctype_missing_sentence(site_url, target), err=True)
        raise typer.Exit(code=1)


def _doctype_missing_sentence(site_url: str, target: Target) -> str:
    return (
        f"the {CASH_FORECAST} doctype is not installed on {site_url.rstrip('/')}; "
        f"run `runningtab forecast install-doctype --target {target.value}` as an administrator first"
    )


@access_app.command("provision")
def access_provision(
    ctx: typer.Context, target: TargetOption, profile: ProfileOption = Profile.BOT, yes: YesOption = False
) -> None:
    """Create/repair a role and user, mint a fresh key, and prove the server scopes it as policy says.

    Runs as an administrator (`RUNNINGTAB_ADMIN_TOKEN`, your own `key:secret`, for
    books). For the bot, also fences Company's two close-lock fields
    (`accounts_frozen_till_date`, `role_allowed_for_frozen_entries`) at
    permission level 1 and proves with a real write that the freshly minted
    key cannot move either one -- Frappe resets an over-permlevel write
    silently rather than refusing it (`validate_higher_perm_levels`), so that
    proof is the only thing worth trusting. Stores the key, for the books
    target only, once every check has passed: for the bot, that submit was
    refused, no prohibited flag leaked, and neither lock field moved; for the
    reader, that no write flag is allowed at all. The live secret is rotated
    the moment the key is minted, so every later failure says so.
    """
    books = _books(ctx)
    config = admin_config(books.site, target)
    policy = books.reader if profile is Profile.READER else books.bot
    secret_id = books.site.books_readonly_secret if profile is Profile.READER else books.site.books_secret
    _announce_policy(policy, profile)
    _report_skipped_grants(policy)
    _confirm(yes, f"provision {profile.value} on {target.value}?")

    with Client(config) as client:
        company = _sole_company(client)  # before the key rotates, so its errors are ordinary ones
        _refuse_missing_doctypes(client, policy, config.url, target)
        fence_results = (
            _install_and_check_fence(client, company, books.fence_probe_role) if profile is Profile.BOT else []
        )
        try:
            report = provision(client, policy)
        except KeyRotationFailedError as exc:
            _fail_after_rotation(target, f"minting a fresh key failed: {exc}", profile)
    _report_provision_result(report)
    if fence_results:
        _report_fence_install(fence_results)

    probe_config = dataclasses.replace(config, api_key=report.keys.api_key, api_secret=report.keys.api_secret)
    check = _verify_minted(profile, probe_config, company, target)
    if not check.passed:
        _report_check_failure(check, storing=True)
        _warn_if_rotation_left_books_broken(target, profile)
        raise typer.Exit(code=1)

    if profile is Profile.BOT:
        # The one proof that means anything: a real write with the fresh key,
        # read back by an administrator. See runningtab.period.fence.prove_fence.
        # Wrapped the same way `_verify_minted` is: a failure here means we do
        # not actually know whether the fence held, and the secret has
        # already rotated by the time we find out, so it goes through
        # `_fail_after_rotation` like every other post-mint failure.
        with Client(config) as admin, Client(probe_config) as bot_client:
            try:
                proof = prove_fence(admin, bot_client, company, books.fence_probe_role)
            except Exception as exc:
                _fail_after_rotation(
                    target,
                    f"proving the fence held failed: {exc}; a lock field may have been left moved -- "
                    "check Company in ERPNext now",
                    profile,
                )
        _report_fence_proof(proof)
        if not (proof.date_refused and proof.role_refused):
            moved = [
                field
                for field, refused in zip(LOCK_FIELDS, (proof.date_refused, proof.role_refused), strict=True)
                if not refused
            ]
            typer.echo(f"REFUSING to store: the bot's key moved {', '.join(moved)} (now restored by the admin)")
            _warn_if_rotation_left_books_broken(target, profile)
            raise typer.Exit(code=1)

    if target is Target.BOOKS:
        try:
            version = store_books_credential(books.site, report.keys.api_key, report.keys.api_secret, secret_id)
        except Exception as exc:
            _fail_after_rotation(target, f"writing the new credential to Secret Manager failed: {exc}", profile)
        typer.echo(f"credential stored: {version}")
    else:
        typer.echo(f"target {target.value!r}: credential not stored; it keeps its credential in the environment")


@access_app.command("verify")
def access_verify(
    ctx: typer.Context, target: TargetOption, profile: ProfileOption = Profile.BOT, company: CompanyOption = None
) -> None:
    """Ask the server what the stored credential can do; exit 0 only on a full pass.

    Rotates nothing, so it is the way to re-check an identity after the policy
    it is held to changes. `--target test` is refused: its bot key is never
    persisted, so there is nothing standalone to verify -- `access provision
    --target test` probes the key it mints.
    """
    books = _books(ctx)
    if target is Target.TEST:
        raise typer.BadParameter(
            "not supported for --target test: its bot credential is never persisted, so there is nothing "
            "standalone to verify. Run `runningtab access provision --target test` instead -- it mints, probes, "
            "and reports in one step."
        )
    reading = profile is Profile.READER
    config = site_config(books.site, target, readonly=reading)
    policy = books.reader if reading else books.bot
    _report_skipped_grants(policy)
    with Client(config) as client:
        _refuse_missing_doctypes(client, policy, config.url, target)

    if reading:
        check = check_reader_credential(config)
        _report_inherited(check)
        if check.passed:
            typer.echo("refused: every mutating flag on the ledger denied by the server")
            return
        _report_check_failure(check, storing=False)
        raise typer.Exit(code=1)

    resolved = company
    if resolved is None:
        with Client(config) as client:
            resolved = _sole_company(client)

    check = check_credential(config, resolved)
    if check.passed:
        _report_refused(check.probe)
        return
    _report_check_failure(check, storing=False)
    raise typer.Exit(code=1)


# --- health ---------------------------------------------------------------

# Which Mercury deployment goes with which ERPNext site. The pairing is the
# point: the disposable site pairs with the sandbox's seeded data, and the
# real ledger pairs with the company's actual bank. Nothing should be able to
# read one and report on the other. The ingest commands take `--env` instead,
# because posting sandbox data into the books is a refusal rather than a
# pairing (`_refuse_sandbox_into_the_ledger`).
MERCURY_FOR_TARGET: dict[Target, MercuryEnv] = {
    Target.TEST: MercuryEnv.SANDBOX,
    Target.BOOKS: MercuryEnv.PRODUCTION,
}


def _bank_ledgers(books: Books, target: Target) -> list[health.AccountLedger]:
    """Every Mercury account with every transaction, read once for the checks that share it."""
    config = mercury_config(books.site, MERCURY_FOR_TARGET[target])
    with MercuryClient(config) as client:
        return health.read_ledgers(client)


def _cursor_check(books: Books, target: Target) -> health.Check:
    """The ingest's cursor for this target, from the state file a sync on this machine keeps.

    Checked for existence first, as `mercury status` does: opening it creates
    it, and a report must not leave an empty state file behind.
    """
    env = MERCURY_FOR_TARGET[target]
    path = default_state_path(books.name, env.value, target.value)
    if not path.exists():
        return health.no_ingest_state(path)
    with _open_state(books, env, target, path) as ingest_state:
        return health.event_cursor_age(ingest_state.cursor(), _now())


def _suspense_check(books: Books, target: Target, company: str | None) -> health.Check:
    """Resolve the company, derive the suspense account's document name, and read it.

    `coa.sole_company`, not `_sole_company`: the latter re-raises as a typer
    usage error, which exits before the report prints. Here the refusal to
    guess is one section's outcome, not a complaint about the command line.
    """
    with Client(site_config(books.site, target, readonly=True)) as client:
        resolved = company or coa.sole_company(client)
        wanted = suspense_account(load_chart(books.chart_path))
        account = health.suspense_account_name(wanted.number, wanted.name, company_abbr(client, resolved))
        # Local date: this is a report a person runs and reads, and the ageing
        # it prints is in days.
        return health.suspense_contents(client, resolved, account, dt.date.today())


@app.command("health")
def health_report(
    ctx: typer.Context,
    target: TargetOption,
    company: CompanyOption = None,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Fail on a check that is not available yet, not only on one that is out of bounds.",
        ),
    ] = False,
    mercury_mapping: Annotated[
        pathlib.Path | None,
        typer.Option(
            "--mercury-mapping",
            help=(
                "Path to a mercury.toml-shaped mapping file; defaults to the books directory's mercury.toml, which "
                "names only the real Mercury accounts. --target test reads the Mercury sandbox, whose "
                "account ids are not in that file, so the closed-periods section's rebuild needs a "
                "mapping of its own here, the same as `period status --target test` does."
            ),
        ),
    ] = None,
) -> None:
    """Say whether the books are actually current, and exit non-zero when they are not.

    Six sections. Webhook health is not available until a webhook is
    registered, and the cursor check is not available on a machine with no
    ingest state; both report that rather than passing vacuously -- a green
    report from a check that never ran is the failure mode this command exists
    to prevent. They count as failures only under `--strict`.

    Reads only. Runs as the reader on the ERPNext side, whose credential the
    server refuses every write for.
    """
    books = _books(ctx)
    typer.echo("runningtab health")
    typer.echo(f"  erpnext: {target.value}")
    typer.echo(f"  mercury: {MERCURY_FOR_TARGET[target].value}")
    typer.echo("")

    # Read on first use and kept: both bank checks need the same read, and a
    # read that raised is not kept, so the second check reports its own error.
    bank = functools.cache(lambda: _bank_ledgers(books, target))
    now = _now()
    checks = health.report(
        health.guarded("balance-invariant", "balance invariant", lambda: health.check_balance_invariant(bank())),
        health.guarded("uncoded-spend", "uncoded spend, aged", lambda: health.uncoded_spend(bank(), now)),
        health.guarded("suspense-contents", "suspense contents", lambda: _suspense_check(books, target, company)),
        health.guarded("event-cursor-age", "event cursor age", lambda: _cursor_check(books, target)),
        health.guarded(
            "closed-periods",
            "closed periods",
            lambda: closed_periods_check(_period_status_report(books, target, company, mercury_mapping)),
        ),
    )
    for line in health.render(checks):
        typer.echo(line)

    code = health.exit_code(checks, strict=strict)
    if code:
        raise typer.Exit(code=code)


# --- period ---------------------------------------------------------------

period_app = typer.Typer(help="A quarter's books as a package for an accountant or auditor.", no_args_is_help=True)
app.add_typer(period_app, name="period")


@dataclasses.dataclass(frozen=True)
class PeriodReads:
    """Everything `period export` reads from the two outside systems, read once."""

    site_url: str
    company: str
    abbr: str
    fiscal_years: list[FiscalYear]
    rows: list[GLRow]
    ledgers: list[health.AccountLedger]
    documents: dict[str, bytes]


def _refuse_chart_drift(plan: ChartPlan, target: Target) -> None:
    """Refuse, naming each, unless the live chart already matches `chart.coa`.

    The export classifies every GL row from `chart.coa` without ever
    checking the live site. If the chart was never applied, or has drifted
    since, every statement here still balances -- an account classified
    under the wrong number or type looks fine in isolation and wrong only
    against the real books. `plan` is the same diff `runningtab coa plan` runs;
    `is_empty` is exactly what that command calls "the site already matches
    the chart" (extras excluded -- they never block), and `is_applyable`
    exactly what it calls a plan with nothing refused.
    """
    if plan.is_applyable and plan.is_empty:
        return
    parts: list[str] = []
    parts.extend(f"create {c.desired.number} {c.desired.name}" for c in plan.creates)
    parts.extend(
        f"{'renumber' if c.changes_number else 'rename'} {c.live.document_name} -> {c.desired.number} {c.desired.name}"
        for c in plan.renames
    )
    parts.extend(f"retype {c.live.document_name} to {c.desired.account_type}" for c in plan.retypes)
    parts.extend(f"enable {c.live.document_name}" for c in plan.enables)
    for refusal in plan.refusals:
        name, parent = refusal.key
        where = f"{name!r} under {parent!r}" if parent else f"{name!r} (root)"
        parts.append(f"refused {where}: {refusal.reason} ({refusal.detail})")
    raise PeriodError(
        "the live chart does not match chart.coa: "
        + "; ".join(parts)
        + f"; run `runningtab coa plan --target {target.value}` to review, `runningtab coa apply` to bring it current"
    )


def _period_reads(
    books: Books,
    target: Target,
    company: str | None,
    quarter: Quarter,
    chart: list[DesiredAccount],
    defaults: dict[str, str],
    config: SiteConfig,
    *,
    check_reader_grants: bool,
) -> PeriodReads:
    """Read the quarter's GL, fiscal years, documents, and bank ledgers with `config`'s credential.

    `config` is the caller's to choose: `period export` reads as the reader
    (`site_config(books.site, target, readonly=True)`), `period close` as an
    administrator (`admin_config(books.site, target)`) -- an administrator's grants are
    not the reader's, so `check_reader_grants` gates the one check below that
    is specifically about the reader's own provisioning.

    `refuse_unreadable_doctypes` below checks the *intended* grants
    (`READER_GRANTS`) against the voucher types this quarter actually needs --
    it catches a voucher type nobody ever granted. It says nothing about
    whether the credential in hand actually holds what `READER_GRANTS`
    describes: if the reader was never (re-)provisioned after a grant was
    added, the server silently omits Files on a doctype it cannot read rather
    than refusing the query, and this command would ship a package silently
    short of documents. Asking the server what it actually honours, before
    any read runs, is what `missing_grants_report` is for -- the same check
    `access verify` uses. It is only meaningful against the reader's own
    config, so `check_reader_grants=False` (close) skips it.

    `refuse_missing_bank_transactions` runs last, after both the Frappe and
    the Mercury reads: it is the only check here that needs both sides at
    once, to prove every settled Mercury transaction in the quarter has a
    Bank Transaction the reader could actually see (and so query Files on).

    `chart` and `defaults` are `period_export`'s single parse of `chart.coa`, handed in
    rather than read here again: the live-chart comparison below runs
    against the exact same object `period_export` then builds the package
    from and hashes into the manifest, so a mid-export edit to the file
    cannot make one of those three see different content than another.
    """
    if check_reader_grants:
        missing = missing_grants_report(config, READER_GRANTS)
        if missing:
            named = "; ".join(f"{doctype} ({', '.join(flags)})" for doctype, flags in sorted(missing.items()))
            raise PeriodError(
                f"the reader credential does not hold: {named}; run "
                f"`runningtab access provision --target {target.value} --profile reader` with RUNNINGTAB_ADMIN_TOKEN set"
            )
    chart_plan = coa.plan_against(config, company, chart, defaults)
    chart_plan.client.close()
    _refuse_chart_drift(chart_plan.plan, target)
    with Client(config) as client:
        resolved = company or coa.sole_company(client)
        rows = read_gl(client, resolved, quarter.end)
        vouchers = quarter_vouchers(rows, quarter)
        refuse_unreadable_doctypes(vouchers, (g.doctype for g in READER_GRANTS))
        refuse_unreadable_vouchers(client, vouchers)
        bank_transactions = read_bank_transactions(client, resolved, quarter)
        documents = read_documents(client, vouchers, [bt.name for bt in bank_transactions])
        reads = PeriodReads(
            config.url,
            resolved,
            company_abbr(client, resolved),
            read_fiscal_years(client, resolved),
            rows,
            [],
            documents,
        )
    with MercuryClient(mercury_config(books.site, MERCURY_FOR_TARGET[target])) as bank:
        reads = dataclasses.replace(reads, ledgers=read_mercury_ledgers(bank))
    refuse_missing_bank_transactions(reads.ledgers, bank_transactions, quarter)
    return reads


def _period_status_report(
    books: Books, target: Target, company: str | None, mercury_mapping: pathlib.Path | None = None
) -> StatusReport:
    """Build the closed-periods status report, reading as the reader -- the same credential `period export` uses.

    Runs `period export`'s own reader effective-grant check first, before
    comparing anything: a reader credential missing a grant is a named
    refusal, never reported as drift in a quarter nobody touched (that check
    is server-side, not merely a static read of `READER_GRANTS`; see
    `_period_reads`'s own docstring on why). Each stored quarter is rebuilt
    through `_period_reads` and `build_package` -- the exact path `period
    export` uses -- so a mismatch `period_status` reports is never an
    artefact of a different code path disagreeing with itself. Package
    bytes only: nothing here writes anywhere.

    `mercury_mapping` is `period export`'s own option, needed for the same
    reason there: `--target test` reads the Mercury sandbox, whose account
    ids are not in the books directory's `mercury.toml`.
    """
    config = site_config(books.site, target, readonly=True)
    missing = missing_grants_report(config, READER_GRANTS)
    if missing:
        named = "; ".join(f"{doctype} ({', '.join(flags)})" for doctype, flags in sorted(missing.items()))
        raise PeriodError(
            f"the reader credential does not hold: {named}; run "
            f"`runningtab access provision --target {target.value} --profile reader` with RUNNINGTAB_ADMIN_TOKEN set"
        )

    chart_text = books.chart_path.read_bytes().decode("utf-8")
    chart = parse_chart(chart_text)
    defaults = parse_defaults(chart_text)
    qmap = load_qbo_map(books.qbo_path)
    mapping = load_mapping(mercury_mapping or books.mercury_path)
    record = GcsRecord(books.record_buckets[target])

    with Client(config) as client:
        resolved = company or coa.sole_company(client)
        abbr = company_abbr(client, resolved)
        lock = read_visible_lock(client, resolved, Profile.READER)
        history = lock_history(client, resolved)

    stored_latest = latest(stored_packages(record, abbr))

    def rebuild(quarter: Quarter) -> Package:
        reads = _period_reads(books, target, resolved, quarter, chart, defaults, config, check_reader_grants=False)
        fiscal_year = fiscal_year_for(quarter, reads.fiscal_years)
        return build_package(
            PackageInputs(
                quarter=quarter,
                fiscal_year=fiscal_year,
                company=reads.company,
                site_url=reads.site_url,
                chart=tuple(chart),
                abbr=reads.abbr,
                rows=tuple(reads.rows),
                ledgers=tuple(reads.ledgers),
                mapping=mapping,
                qmap=qmap,
                documents=reads.documents,
            )
        )

    return period_status(
        lock=lock,
        history=history,
        stored_latest=stored_latest,
        sums=lambda stored: read_sums(record, stored),
        rebuild=rebuild,
        lock_record=lambda stored: read_lock_version(record, stored),
    )


@period_app.command("status")
def period_status_cmd(
    ctx: typer.Context,
    target: TargetOption,
    company: CompanyOption = None,
    mercury_mapping: Annotated[
        pathlib.Path | None,
        typer.Option(
            "--mercury-mapping",
            help=(
                "Path to a mercury.toml-shaped mapping file; defaults to the books directory's mercury.toml, which "
                "names only the real Mercury accounts. --target test reads the Mercury sandbox, whose "
                "account ids are not in that file, so checking it needs a mapping of its own here."
            ),
        ),
    ] = None,
) -> None:
    """Report which quarters are closed, whether their stored packages still match the ledger, and whether the
    lock itself is still intact.

    Reads only, as the reader -- the same credential `period export` uses.
    Every closed quarter's latest revision is rebuilt through that exact same
    path and compared, byte for byte, against its stored `SHA256SUMS`; a
    changed or missing file is named, not merely flagged. Also reports a
    reopened quarter (Books Closed Through moved backward, naming who and
    when from the Company Version history), a quarter locked with no stored
    package (Books Closed Through past every stored quarter), and
    `role_allowed_for_frozen_entries` left set. Exits 1 unless every one of
    those comes back clean.
    """
    try:
        report = _period_status_report(_books(ctx), target, company, mercury_mapping)
    except _CLOSE_REFUSALS as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    shown = report.lock.closed_through.isoformat() if report.lock.closed_through else "not set"
    typer.echo(f"Books Closed Through: {shown}")
    if not report.quarters:
        typer.echo("  nothing closed yet")
    for quarter_status in report.quarters:
        if quarter_status.changed:
            typer.echo(
                f"  {quarter_status.quarter.label} rev{quarter_status.revision}: changed "
                + ", ".join(quarter_status.changed)
            )
        else:
            typer.echo(f"  {quarter_status.quarter.label} rev{quarter_status.revision}: ok")
    for problem in report.problems:
        typer.echo(f"  problem: {problem}")

    if not report.ok:
        raise typer.Exit(code=1)


@period_app.command("export")
def period_export(
    ctx: typer.Context,
    target: TargetOption,
    quarter: Annotated[str, typer.Option("--quarter", help="The quarter, as YYYYQn (e.g. 2026Q3).")],
    company: CompanyOption = None,
    output: Annotated[
        pathlib.Path | None, typer.Option("--output", help="Directory to create; defaults to ./<quarter>-DRAFT.")
    ] = None,
    mercury_mapping: Annotated[
        pathlib.Path | None,
        typer.Option(
            "--mercury-mapping",
            help=(
                "Path to a mercury.toml-shaped mapping file; defaults to the books directory's mercury.toml, which "
                "names only the real Mercury accounts. --target test reads the Mercury sandbox, whose "
                "account ids are not in that file, so it needs a mapping of its own here."
            ),
        ),
    ] = None,
) -> None:
    """Build a draft package for one quarter. Reads only; writes a new local directory and nothing else.

    Exits 1 when the bank and the ledger disagree at the quarter's end -- the
    draft is still written, because it is what a person investigates with --
    and 1 without writing anything when the package cannot be built at all.
    """
    try:
        parsed = Quarter.parse(quarter)
    except PeriodError as exc:
        raise typer.BadParameter(str(exc), param_hint="--quarter") from exc
    books = _books(ctx)
    directory = output or pathlib.Path(f"{parsed.label}-DRAFT")
    try:
        # Read chart.coa once and parse it once, here: the live-chart check
        # inside _period_reads, the package below, and the manifest's SHA-256
        # all use this same snapshot, so an edit landing mid-export cannot
        # make one of the three see different content than another.
        chart_bytes = books.chart_path.read_bytes()
        chart = parse_chart(chart_bytes.decode("utf-8"))
        defaults = parse_defaults(chart_bytes.decode("utf-8"))
        reader_config = site_config(books.site, target, readonly=True)
        reads = _period_reads(books, target, company, parsed, chart, defaults, reader_config, check_reader_grants=True)
        fiscal_year = fiscal_year_for(parsed, reads.fiscal_years)
        qmap = load_qbo_map(books.qbo_path)
        package = build_package(
            PackageInputs(
                quarter=parsed,
                fiscal_year=fiscal_year,
                company=reads.company,
                site_url=reads.site_url,
                chart=tuple(chart),
                abbr=reads.abbr,
                rows=tuple(reads.rows),
                ledgers=tuple(reads.ledgers),
                mapping=load_mapping(mercury_mapping or books.mercury_path),
                qmap=qmap,
                documents=reads.documents,
            )
        )
        facts = ManifestFacts(
            quarter=parsed,
            fiscal_year=fiscal_year,
            company=reads.company,
            books_name=books.name,
            site_url=reads.site_url,
            chart_sha256=hashlib.sha256(chart_bytes).hexdigest(),
            qmap=qmap,
            generated_at=dt.datetime.now(dt.UTC),
            generated_by=getpass.getuser(),
            draft=True,
            today=dt.date.today(),
            bank=package.bank,
            split_vouchers=package.split_vouchers,
            has_journals=any(p.startswith("qbo/journal-entries-") for p in package.files),
        )
        write_package(directory, package, render_manifest(facts, package.files))
    except (
        PeriodError,
        ChartError,
        SiteCredentialError,
        MercuryError,
        # Every doctype this command reads is one the reader might not (yet)
        # hold; the same set `runningtab.health.guarded` catches, for the same
        # reason -- these are all "could not read", not a crash.
        FrappeError,
        coa.NoSoleCompanyError,
        # `company_abbr` raises LookupError for a company the site does not
        # have, or one with no abbreviation.
        LookupError,
        httpx2.HTTPError,
        # A hand-edited mercury.toml or qbo.toml that does not parse, or does
        # not describe a mapping this command can post with: refused, not
        # crashed -- the person editing the file gets the same "could not
        # build the package" report as every other input problem here.
        MappingError,
        tomllib.TOMLDecodeError,
        # write_package's own filesystem writes (a directory it cannot
        # create, a parent that is not a directory): a bad --output is a
        # refusal, not a traceback.
        OSError,
    ) as exc:
        typer.echo(f"refused: {exc}", err=True)
        if isinstance(exc, PermissionError_):
            typer.echo(
                f"the reader may be missing a grant this command needs; run "
                f"`runningtab access provision --target {target.value} --profile reader` with RUNNINGTAB_ADMIN_TOKEN set",
                err=True,
            )
        raise typer.Exit(code=1) from exc
    typer.echo(f"wrote {directory}/ ({len(package.files)} files)")
    for voucher in package.split_vouchers:
        typer.echo(f"  split through QBO Import Clearing: {voucher}")
    for line in package.bank:
        typer.echo(
            f"  bank {line.number} {line.label}: bank {line.bank}, ledger {line.ledger}, difference {line.difference}"
        )
    code = export_exit_code(package)
    if code:
        typer.echo("the bank and the ledger disagree at the quarter's end; see bank-reconciliation.csv", err=True)
        raise typer.Exit(code=code)


def export_exit_code(package: Package) -> int:
    """1 when any Mercury account disagrees with the ledger at the quarter's end: the draft wants a person."""
    return 1 if any(line.difference for line in package.bank) else 0


def _today() -> dt.date:
    """The close ceremony's clock, as its own name so a test can patch it instead of the real one."""
    return dt.date.today()


_CLOSE_REFUSALS: tuple[type[Exception], ...] = (
    PeriodError,
    ChartError,
    SiteCredentialError,
    MercuryError,
    FrappeError,
    coa.NoSoleCompanyError,
    LookupError,
    httpx2.HTTPError,
    MappingError,
    tomllib.TOMLDecodeError,
    OSError,
    GoogleAPIError,
    # ADC expiring or missing mid-command (GcsRecord's storage.Client(), or a
    # revoked/expired token discovered on upload) raises from google.auth,
    # not google.api_core -- GoogleAuthError is not a GoogleAPIError subclass,
    # so it needs naming here too or it crashes instead of refusing.
    GoogleAuthError,
)


# Post-lock failures whose own message already names the next step, so the
# lock line beside them adds no retry advice: a rebuild mismatch says to
# re-run or reopen, and a suspect stored package says to run `period status`
# (a retry there would only be refused as "already has a stored package").
_NAMES_ITS_OWN_NEXT_STEP: tuple[type[Exception], ...] = (HeldForReview, SuspectPackage)


def _report_held_lock(held: Held, quarter: Quarter, exc: Exception, *, refusal: bool) -> None:
    """Say the quarter is locked, before a failure after the lock is reported, so it never reads as "nothing happened".

    A named refusal that is not one of `_NAMES_ITS_OWN_NEXT_STEP` (a
    dropped connection, a bucket 503) gets its next step. On a first close
    the re-run takes the recovery path and stores the package -- after
    `period status` shows it is still missing, since an error can come back
    after the store completed and a re-run would then only be refused as
    "already has a stored package". On a re-close
    of a stored quarter the re-run would be refused as "already has a stored
    package", so the step is to reopen the quarter and close it again -- but
    only after `period status` shows the new revision did not land (an error
    can come back after the store completed, and every revision is
    permanent), and followed by every stored quarter after it, in order. An
    unexpected exception gets the lock line alone -- whether a retry helps is
    unknown -- and is re-raised.
    """
    change = held.change
    when = change.new.isoformat() if change.new else "unset"
    line = f"Books Closed Through is now {when} (Company Version {change.version}, by {change.by})"
    if refusal and not isinstance(exc, _NAMES_ITS_OWN_NEXT_STEP):
        if held.reclosing:
            then = f", then {reclose_steps(list(held.later))}" if held.later else ""
            line += (
                f"; the new revision may not have been stored -- run `runningtab period status` first. If it still "
                f"shows {quarter.label}'s earlier revision, {reopen_step(quarter)} and run `runningtab period close` "
                f"again for {quarter.label}{then}. A re-run as it stands is refused because {quarter.label} "
                "already has a stored package."
            )
        else:
            line += (
                f"; the package may not have been stored -- run `runningtab period status` first. If it shows "
                f"{quarter.label} locked with no stored package, run `runningtab period close` again for "
                f"{quarter.label} to store it."
            )
    else:
        line += "; the quarter stays locked."
    typer.echo(line, err=True)


@period_app.command("close")
def period_close(
    ctx: typer.Context,
    target: TargetOption,
    quarter: Annotated[str, typer.Option("--quarter", help="The quarter, as YYYYQn (e.g. 2026Q3).")],
    company: CompanyOption = None,
    mercury_mapping: Annotated[
        pathlib.Path | None,
        typer.Option(
            "--mercury-mapping",
            help=(
                "Path to a mercury.toml-shaped mapping file; defaults to the books directory's mercury.toml, which "
                "names only the real Mercury accounts. --target test reads the Mercury sandbox, whose "
                "account ids are not in that file, so it needs a mapping of its own here."
            ),
        ),
    ] = None,
) -> None:
    """Run the close ceremony (`runningtab.period.close.close_quarter`) for real: lock the quarter and store its package.

    Runs as an administrator -- `RUNNINGTAB_ADMIN_TOKEN`, your own `key:secret`,
    for `--target books` -- the credential that alone can move Books Closed
    Through; refused before any request when that variable is unset. Every
    ERPNext read, the lock, and the cross-check use `admin_config(books.site, target)`,
    never the reader's; the lock, the readiness read, and the cross-check
    share one `Client(admin_config(books.site, target))` for the whole command, while
    each build (the initial one and the ceremony's own rebuild-and-compare)
    opens its own fresh `Client`/Mercury connection through the same config,
    the same way `period export` reads with the reader's -- a cached read
    here would make the rebuild compare a snapshot against itself and never
    catch a ledger change between the check and the lock. The finished
    package lands in the record bucket `books.toml` names for the target.
    See `runningtab.period.close` for the ceremony's own order and its refusals;
    this only wires it to the real site, the real Mercury account, and the
    real record bucket.
    """
    try:
        parsed = Quarter.parse(quarter)
    except PeriodError as exc:
        raise typer.BadParameter(str(exc), param_hint="--quarter") from exc
    # Filled by `close_quarter` (through `CloseSteps.locked`) once the
    # quarter is locked -- by this run, or by an earlier one this run is
    # recovering -- so a failure caught below knows the difference between
    # "nothing happened" and "the quarter is locked but nothing was stored".
    held: list[Held] = []
    books = _books(ctx)
    try:
        # admin_config raises MissingSiteCredentialError from RUNNINGTAB_ADMIN_TOKEN
        # alone, reading nothing else, before anything below ever opens a
        # connection -- so an unset token refuses here, with no request made.
        config = admin_config(books.site, target)
        chart_bytes = books.chart_path.read_bytes()
        chart = parse_chart(chart_bytes.decode("utf-8"))
        defaults = parse_defaults(chart_bytes.decode("utf-8"))
        chart_sha256 = hashlib.sha256(chart_bytes).hexdigest()
        qmap = load_qbo_map(books.qbo_path)
        record = GcsRecord(books.record_buckets[target])

        with Client(config) as client:
            resolved_company = company or coa.sole_company(client)
            abbr = company_abbr(client, resolved_company)
            index = index_accounts(chart, abbr)
            crosscheck = bind_crosscheck(client, resolved_company, index)
            wanted = suspense_account(chart)
            suspense_name = health.suspense_account_name(wanted.number, wanted.name, abbr)

            def build_step() -> Built:
                # A fresh read every call -- including the ceremony's own
                # rebuild-and-compare call -- through the admin config, the
                # same way `period export` reads with the reader's; a cached
                # read here would make the rebuild compare a snapshot against
                # itself and never catch a ledger change between the check
                # and the lock.
                reads = _period_reads(
                    books, target, resolved_company, parsed, chart, defaults, config, check_reader_grants=False
                )
                fiscal_year = fiscal_year_for(parsed, reads.fiscal_years)
                step_index = index_accounts(chart, reads.abbr)
                rows = tuple(reads.rows)
                tb = trial_balance(rows, step_index, parsed, fiscal_year)
                receivables = tuple(open_items(rows, step_index, "Receivable", parsed.end))
                payables = tuple(open_items(rows, step_index, "Payable", parsed.end))
                package = build_package(
                    PackageInputs(
                        quarter=parsed,
                        fiscal_year=fiscal_year,
                        company=reads.company,
                        site_url=reads.site_url,
                        chart=tuple(chart),
                        abbr=reads.abbr,
                        rows=rows,
                        ledgers=tuple(reads.ledgers),
                        mapping=load_mapping(mercury_mapping or books.mercury_path),
                        qmap=qmap,
                        documents=reads.documents,
                    )
                )
                facts = ManifestFacts(
                    quarter=parsed,
                    fiscal_year=fiscal_year,
                    company=reads.company,
                    books_name=books.name,
                    site_url=reads.site_url,
                    chart_sha256=chart_sha256,
                    qmap=qmap,
                    generated_at=dt.datetime.now(dt.UTC),
                    generated_by=getpass.getuser(),
                    draft=True,
                    today=_today(),
                    bank=package.bank,
                    split_vouchers=package.split_vouchers,
                    has_journals=any(p.startswith("qbo/journal-entries-") for p in package.files),
                )
                return Built(
                    package=package, trial_balance=tb, receivables=receivables, payables=payables, manifest_facts=facts
                )

            def read_blockers_step() -> list[Blocker]:
                # Its own GL read, through the same admin client: stage 1's
                # preconditions must refuse (if they are going to) before any
                # heavy read runs, so this cannot borrow `build`'s rows --
                # `build` has not been called yet when this does.
                rows = read_gl(client, resolved_company, parsed.end)
                return read_blockers(client, resolved_company, parsed, suspense_name, rows)

            def upload_step(files: dict[str, bytes], build_manifest: Callable[[int], bytes]) -> str:
                prefix = next_prefix(record, abbr, parsed)
                revision = revision_of(prefix, abbr, parsed)
                manifest = build_manifest(revision)
                all_files = {**files, "MANIFEST.md": manifest, "SHA256SUMS": sha256sums(files)}
                upload_package(record, prefix, all_files)
                return prefix

            steps = CloseSteps(
                read_lock=lambda: read_lock(client, resolved_company),
                read_blockers=read_blockers_step,
                build=build_step,
                crosscheck=crosscheck,
                set_closed_through=lambda date: set_closed_through(client, resolved_company, date),
                lock_history=lambda: lock_history(client, resolved_company),
                locked=held.append,
                stored_packages=lambda: stored_packages(record, abbr),
                upload=upload_step,
                today=_today,
            )
            outcome = close_quarter(steps, parsed, abbr)
    except Exception as exc:
        refusal = isinstance(exc, _CLOSE_REFUSALS)
        if held:
            _report_held_lock(held[-1], parsed, exc, refusal=refusal)
        if not refusal:
            raise
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"closed {parsed.label}: stored at {outcome.prefix}")
    typer.echo(f"lock: Company Version {outcome.lock.version}, by {outcome.lock.by}")
    typer.echo(f"files: {len(outcome.package.files)}")


# --- mercury --------------------------------------------------------------

EnvOption = Annotated[MercuryEnv, typer.Option("--env", help="Which Mercury deployment to read.")]
PruneOption = Annotated[
    bool,
    typer.Option(
        "--prune",
        help=(
            "Also delete unmanaged categories -- but only ones no transaction carries. "
            "Reads every transaction on every account to find out."
        ),
    ),
]
StateOption = Annotated[
    pathlib.Path | None, typer.Option("--state", help="Ingest state file; defaults to the XDG state path.")
]
DryRunOption = Annotated[bool, typer.Option("--dry-run", help="Report what would post; write nothing.")]


def _open_state(books: Books, env: MercuryEnv, target: Target, state: pathlib.Path | None) -> IngestState:
    """The state file for one Mercury deployment and one ledger, and no other.

    The default path keeps them apart by name and the recorded identity keeps
    them apart when `--state` names a file directly. The single place either is
    decided, so no command can reach for the other's cursor.
    """
    identity = f"{env.value}/{target.value}"
    try:
        return IngestState(state or default_state_path(books.name, env.value, target.value), identity=identity)
    except StateIdentityError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _refuse_sandbox_into_the_ledger(env: MercuryEnv, target: Target) -> None:
    """The one combination that is never wanted: seeded sandbox data in the real books."""
    if env is MercuryEnv.SANDBOX and target is Target.BOOKS:
        raise typer.BadParameter(
            "refusing to post sandbox transactions into the books ledger; "
            "the sandbox is seeded data and the ledger is the company's"
        )


def _print_ledger_outcome(report: IngestReport) -> None:
    """What reached the ledger, and what did not."""
    typer.echo(f"submitted {len(report.submitted):4}  (new Bank Transactions, ready for reconciliation)")
    typer.echo(f"drafted   {len(report.drafted):4}  (draft Journal Entries for coded rows; a person submits them)")
    typer.echo(f"updated   {len(report.updated):4}  (Bank Transactions and draft vouchers brought up to date)")
    typer.echo(f"adopted   {len(report.adopted):4}  (hand-imported rows recognised by their trace number)")
    typer.echo(f"attached  {len(report.attached):4}  (Mercury receipts now on their Bank Transaction)")
    for line in report.attached:
        typer.echo(f"  . {line}")
    if report.planned:
        typer.echo(f"planned   {len(report.planned):4}  (dry run; nothing was written)")
        for line in report.planned:
            typer.echo(f"  . {line}")
    typer.echo(f"skipped   {len(report.skipped):4}  (not settled)")
    if report.failed:
        typer.echo(
            f"failed    {len(report.failed):4}  (failed at Mercury in an account we cannot read; nothing to post)"
        )
        for transaction_id, reason in report.failed:
            typer.echo(f"  . {transaction_id} {reason}")


def _print_coverage(report: IngestReport) -> None:
    """The two ways the ledger can look fine while being incomplete."""
    if report.uncoded:
        # Rows the person reconciling gets no hint for; somebody codes each one
        # in Mercury or by hand.
        typer.echo(f"uncoded   {len(report.uncoded):4}  (settled, non-transfer, no GL code)")
        for transaction_id, age in report.uncoded[:10]:
            typer.echo(f"  ! {transaction_id} {age}d")
    for check in report.balances:
        verdict = "ok" if check.ok else f"OFF BY {check.difference}"
        typer.echo(f"balance   {check.label}: reported {check.reported} computed {check.computed} {verdict}")


def _print_closed(report: IngestReport) -> None:
    """Rows dated in a closed quarter: posted to the bank, drafted to nobody."""
    if not report.closed:
        return
    typer.echo(f"closed    {len(report.closed):4}  (closed period; no draft voucher -- reopen the quarter to book it)")
    for line in report.closed:
        typer.echo(f"  ! {line}")


def _desk_url(site_url: str, doctype: str, name: str) -> str:
    """The document's page in the ERPNext desk: `/app/<doctype slug>/<name>`."""
    slug = doctype.lower().replace(" ", "-")
    return f"{site_url.rstrip('/')}/app/{slug}/{urllib.parse.quote(name)}"


def _print_unneeded(report: IngestReport, site_url: str) -> None:
    """Drafts of ours nothing needs any more, each linked so a person can open and delete it."""
    if not report.unneeded:
        return
    typer.echo(f"delete    {len(report.unneeded):4}  (draft Journal Entries no longer needed; a person deletes them)")
    for draft in report.unneeded:
        typer.echo(f"  ! {draft.journal_entry} for {draft.transaction_id}: {draft.why}")
        typer.echo(f"    {_desk_url(site_url, JOURNAL_ENTRY, draft.journal_entry)}")


def _print_report(report: IngestReport, site_url: str) -> None:
    _print_ledger_outcome(report)
    _print_coverage(report)
    _print_closed(report)
    _print_unneeded(report, site_url)
    if report.attention:
        typer.echo(f"ATTENTION {len(report.attention):4}")
        for transaction_id, reason in report.attention:
            typer.echo(f"  ! {transaction_id} {reason}")
    typer.echo(
        f"events    read {report.events_read} applied {report.events_applied} "
        f"deduplicated {report.events_deduplicated} out-of-order {report.events_out_of_order}"
    )
    typer.echo(f"cursor    {report.cursor}")


def _run_ingest(
    books: Books,
    env: MercuryEnv,
    target: Target,
    company: str | None,
    state: pathlib.Path | None,
    dry_run: bool,
    *,
    snapshot: bool,
) -> None:
    _refuse_sandbox_into_the_ledger(env, target)
    # The one client in this CLI built with submit capability; see ingest_config.
    config = ingest_config(books.site, target)
    typer.echo(f"site: {config.url}")
    typer.echo(f"mercury: {BASE_URLS[env]}")
    with (
        MercuryClient(mercury_config(books.site, env)) as mercury,
        Client(config) as ledger,
        # Its own pool: a receipt comes from S3 on a presigned URL, not from
        # Mercury's API host, and it is closed with the run that opened it.
        ReceiptFetcher() as receipts,
        _open_state(books, env, target, state) as ingest_state,
    ):
        try:
            ctx = posting_context(
                ledger,
                mapping=load_mapping(books.mercury_path),
                chart=load_chart(books.chart_path),
                bot_user=books.bot.user,
                mercury=mercury,
                company=company,
            )
        except PeriodError as exc:
            # A bot that cannot see Books Closed Through: refused before
            # anything posts, rather than drafting into a closed quarter.
            typer.echo(f"refused: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        run = run_snapshot if snapshot else run_sync
        report = run(
            mercury,
            ledger,
            ctx,
            ingest_state,
            now=dt.datetime.now(dt.UTC),
            dry_run=dry_run,
            receipts=receipts,
        )
    _print_report(report, config.url)
    if report.needs_attention:
        raise typer.Exit(code=1)


@mercury_app.command("snapshot")
def mercury_snapshot(
    ctx: typer.Context,
    env: EnvOption,
    target: TargetOption,
    company: CompanyOption = None,
    state: StateOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Establish initial state from transaction lists, and record where in the event log it was true.

    Also the recovery path: events are retained 90 days, so a gap wider than
    that is closed by re-snapshotting rather than by resuming a cursor into a
    hole, which `runningtab mercury sync` refuses to do.
    """
    _run_ingest(_books(ctx), env, target, company, state, dry_run, snapshot=True)


@mercury_app.command("sync")
def mercury_sync(
    ctx: typer.Context,
    env: EnvOption,
    target: TargetOption,
    company: CompanyOption = None,
    state: StateOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Apply every event since the cursor. The same applier a webhook will feed."""
    try:
        _run_ingest(_books(ctx), env, target, company, state, dry_run, snapshot=False)
    except NoCursorError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    except StaleCursorError as exc:
        typer.echo(str(exc))
        typer.echo("run `runningtab mercury snapshot` to re-establish state")
        raise typer.Exit(code=1) from exc


@mercury_app.command("status")
def mercury_status(ctx: typer.Context, env: EnvOption, target: TargetOption, state: StateOption = None) -> None:
    """Where the ingest thinks it is: the cursor, its age, and how many events it has seen.

    Named the same way a run is, because the cursor it reports belongs to one
    deployment and one ledger. A `--state` file written by a different pair is
    refused here rather than reported as though it answered the question asked.
    """
    books = _books(ctx)
    path = state or default_state_path(books.name, env.value, target.value)
    if not path.exists():
        typer.echo(f"no state at {path}; run `runningtab mercury snapshot` first")
        raise typer.Exit(code=1)
    with _open_state(books, env, target, path) as ingest_state:
        cursor = ingest_state.cursor()
        typer.echo(f"state    {path}")
        typer.echo(f"events   {ingest_state.seen_count()} applied")
        if cursor is None:
            typer.echo("cursor   none; nothing has been snapshotted")
            raise typer.Exit(code=1)
        age = dt.datetime.now(dt.UTC) - cursor.synced_at
        typer.echo(f"cursor   {cursor.event_id} as of {cursor.synced_at.isoformat()} ({age.days}d ago)")
        if age > RETENTION:
            typer.echo(f"STALE: past Mercury's {RETENTION.days}-day retention; re-snapshot rather than resuming")
            raise typer.Exit(code=1)


@mercury_app.command("install-fields")
def mercury_install_fields(ctx: typer.Context, target: TargetOption, yes: YesOption = False) -> None:
    """Create the custom fields the ingest writes, as an administrator.

    The books bot cannot do this: its role carries no Custom Field permission,
    deliberately. `mercury_transaction_id` is unique on Bank Transaction, so
    once these exist the server itself refuses a second row for the same
    transaction. On Journal Entry it is not unique, so a person can amend a
    voucher; the ingest refuses a second live voucher itself. Re-running
    repairs a field installed with the old flags. The GL code and note are
    writable so a statement export imported by hand can map its columns onto
    them.
    """
    config = admin_config(_books(ctx).site, target)
    typer.echo(f"site: {config.url}")
    _confirm(yes, "create the Mercury custom fields on Bank Transaction and Journal Entry?")
    with Client(config) as client:
        for label, outcome in install_custom_fields(client):
            typer.echo(f"  {outcome:9} {label}")


@mercury_app.command("install-bank-accounts")
def mercury_install_bank_accounts(ctx: typer.Context, target: TargetOption, yes: YesOption = False) -> None:
    """Create the Bank and one Bank Account per mapped Mercury account, as an administrator.

    The books bot cannot do this: its role carries no Bank or Bank Account
    permission. Which accounts, and what they are called, comes from the
    bank_accounts table in mercury.toml; each links the chart account the
    accounts table gives the same Mercury account. Re-running checks and
    repairs rather than duplicating.
    """
    books = _books(ctx)
    config = admin_config(books.site, target)
    typer.echo(f"site: {config.url}")
    with Client(config) as client:
        context = posting_context(
            client,
            mapping=load_mapping(books.mercury_path),
            chart=load_chart(books.chart_path),
            bot_user=books.bot.user,
        )
        try:
            planned = bank_account_documents(context)
        except (PostingError, MappingError) as exc:
            raise typer.BadParameter(str(exc)) from exc
        _confirm(yes, f"create the Bank '{BANK}' and {len(planned)} Bank Account document(s) on {config.url}?")
        for label, outcome in install_bank_accounts(client, context):
            typer.echo(f"  {outcome:9} {label}")


# --- mercury categories ---------------------------------------------------


def _category_plan(books: Books, env: MercuryEnv, *, prune: bool = False) -> CategoryPlan:
    """Read the chart and Mercury's categories, and reconcile them. Uses the read-only token.

    Pruning costs a second, much larger read: every transaction on every
    account, through the same `read_everything` the ingest trusts, so that
    "nothing carries this category" is answered from the whole bank rather than
    from a recent window. It refuses a short read, which is the point -- a
    category that looks unused because the read stopped early is a category
    somebody's coding is about to be deleted off.
    """
    typer.echo(f"mercury: {BASE_URLS[env]}")
    with MercuryClient(mercury_config(books.site, env)) as mercury:
        live = mercury.categories()
        carried = None
        if prune:
            transactions, _, _ = read_everything(mercury)
            typer.echo(f"read {len(transactions)} transactions to find which categories are in use")
            carried = carried_categories(transactions)
    return plan_categories(desired_categories(load_chart_entries(books.chart_path)), live, carried=carried)


def _unmanaged_flags(unmanaged: UnmanagedCategory) -> str:
    """The same `CRO` column for a category the chart does not name."""
    return "".join(
        letter if value else "-" for letter, value in zip("CRO", live_flags(unmanaged.category).values(), strict=True)
    )


def _print_category_plan(plan: CategoryPlan, *, prune: bool) -> None:
    """One line per action, each carrying what a person would have to type in the dashboard.

    Mercury's write endpoints need a token this repo refuses to store, so the
    plan has to stand on its own as a checklist somebody can work through by
    hand: the exact category name and the three flags, in chart order.

    Under `--prune` each unmanaged category is reported as what it is -- a
    `delete`, or `in use` with the number of transactions that keep it -- so
    the list a person is asked to approve names the deletions individually
    rather than as a count.
    """
    typer.echo("flags: C card spend, R reimbursements, O other; - off")
    typer.echo("")
    for create in plan.creates:
        typer.echo(f"create    {create.desired.flag_label}  {create.desired.name}")
    for edit in plan.edits:
        changes = "; ".join(str(change) for change in edit.changes)
        typer.echo(f"edit      {edit.desired.flag_label}  {edit.desired.name}  {changes}")
    if prune:
        for delete in plan.deletes:
            typer.echo(f"delete    {delete.category.name}")
        for one in plan.in_use:
            typer.echo(f"in use    {one}")
    else:
        for unmanaged in plan.unmanaged:
            typer.echo(f"unmanaged {_unmanaged_flags(unmanaged)}  {unmanaged.category.name}")
    for refusal in plan.refusals:
        typer.echo(f"REFUSED   {refusal}")
    typer.echo("")
    typer.echo(f"matches   {len(plan.matches):4}")
    typer.echo(f"create    {len(plan.creates):4}")
    typer.echo(f"edit      {len(plan.edits):4}")
    if prune:
        typer.echo(f"delete    {len(plan.deletes):4}  (unmanaged, and no transaction carries them)")
        typer.echo(f"in use    {len(plan.in_use):4}  (re-code these in Mercury, or leave them)")
    else:
        typer.echo(f"unmanaged {len(plan.unmanaged):4}  (Mercury's own, or somebody's; never touched)")
    if plan.refusals:
        typer.echo(f"REFUSED   {len(plan.refusals):4}")


@categories_app.command("plan")
def mercury_categories_plan(ctx: typer.Context, env: EnvOption, prune: PruneOption = False) -> None:
    """Report how Mercury's categories differ from the chart. Writes nothing; exits 1 on any drift.

    Cheap enough to be a standing check, which is the point: a category whose
    name no longer matches a GL code derives nothing through Mercury's rules,
    so the transaction arrives uncoded and nobody finds out for weeks.

    `--prune` shows what `apply --prune` would delete. It is still read-only,
    and it is the only way to see that list before agreeing to it.
    """
    try:
        plan = _category_plan(_books(ctx), env, prune=prune)
    except MercuryError as exc:
        typer.echo(f"REFUSED   {exc}")
        raise typer.Exit(code=1) from exc
    _print_category_plan(plan, prune=prune)
    if plan.has_drift:
        raise typer.Exit(code=1)


@categories_app.command("apply")
def mercury_categories_apply(
    ctx: typer.Context, env: EnvOption, yes: YesOption = False, prune: PruneOption = False
) -> None:
    """Create the categories the chart wants and fix the flags on the ones that exist.

    Never renames: the name is what every transaction already carrying a
    category appears to say, and the match key besides.

    Deletes only under `--prune`, and then only a category the bank says no
    transaction carries -- a deletion strips somebody's coding off their rows
    and nothing reproduces it. An unmanaged category that is in use is reported
    with its count and left alone; re-code those rows in Mercury and run again.

    The token comes from `MERCURY_ADMIN_TOKEN` (or `MERCURY_SANDBOX_TOKEN`) and
    from nowhere else. Mercury issues no category-only write scope, so that is
    a Read and Write token which can also send money -- mint it, run this,
    revoke it. The refusal for an unset variable lands before any request.
    """
    books = _books(ctx)
    try:
        config = mercury_write_config(env)
    except SiteCredentialError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    try:
        plan = _category_plan(books, env, prune=prune)
    except MercuryError as exc:
        typer.echo(f"REFUSED   {exc}")
        raise typer.Exit(code=1) from exc
    _print_category_plan(plan, prune=prune)
    if not plan.has_work:
        typer.echo("nothing to write")
        raise typer.Exit(code=1 if plan.refusals or plan.in_use else 0)
    work = f"create {len(plan.creates)} and edit {len(plan.edits)}"
    if prune:
        work += f" and DELETE {len(plan.deletes)}"
    _confirm(yes, f"{work} Mercury categories?")
    failed = False
    with MercuryCategoryWriter(config) as writer:
        try:
            for applied in apply_plan(writer, plan):
                if applied.error is not None:
                    typer.echo(f"  REFUSED  {applied.category_id}  {applied.name}  {applied.error}")
                    failed = True
                else:
                    typer.echo(f"  {applied.action:8} {applied.category_id}  {applied.name}")
        except MercuryError as exc:
            # Everything before this has already printed. A run that wrote four
            # things and then failed must not read as a run that wrote nothing.
            typer.echo(f"FAILED    {exc}")
            failed = True
    if failed or plan.refusals or plan.in_use:
        raise typer.Exit(code=1)


# --- forecast -------------------------------------------------------------

forecast_app = typer.Typer(help="The cash forecast and the capital call it asks of members.", no_args_is_help=True)
app.add_typer(forecast_app, name="forecast")


def _now() -> dt.datetime:
    """Local time with its offset, as its own name so a test can patch it.

    Local, not UTC: an evening run in the US is already tomorrow in UTC, and
    the forecast's window starts today.
    """
    return dt.datetime.now().astimezone()


def _usd(value: Decimal) -> str:
    return f"-${-value:,.2f}" if value < 0 else f"${value:,.2f}"


@contextmanager
def _forecast_refusals(provision: str | None = None) -> Generator[None]:
    """Turn every failure a person can act on into one sentence and exit 1.

    `provision` is the command that grants this credential what the forecast
    reads; a permission refusal names it, since re-provisioning is the fix.
    Anything else is a bug and keeps its traceback.
    """
    try:
        yield
    except PermissionError_ as exc:
        if provision is None:
            raise
        refusal = re.sub(r"<[^>]+>", "", exc.message)
        typer.echo(f"refused: {refusal}; run `{provision}` as an administrator", err=True)
        raise typer.Exit(code=1) from exc
    except (SourceError, ForecastFileError, MercuryError, SiteCredentialError) as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except FrappeError as exc:
        sentence = maintenance_message(exc)
        if sentence is None:
            raise
        typer.echo(f"refused: {sentence}", err=True)
        raise typer.Exit(code=1) from exc


def _print_forecast(forecast: Forecast, name_of: Callable[[str], str]) -> None:
    typer.echo(f"window: {forecast.window_start} to {forecast.window_end}")
    typer.echo(f"opening cash: {_usd(forecast.opening_cash)}")
    typer.echo(f"ledger through: {forecast.ledger_through}")
    typer.echo("")
    typer.echo(f"{'month':<10} {'opening':>12} {'outflows':>12} {'inflows':>12} {'closing':>12}")
    for m in forecast.months:
        typer.echo(
            f"{m.month:%Y-%m}    {_usd(m.opening):>12} {_usd(m.outflows):>12} {_usd(m.inflows):>12} {_usd(m.closing):>12}"
        )
    typer.echo("")
    typer.echo("dues:")
    for d in forecast.dues:
        typer.echo(f"  {d.due}  {d.line.name:<30} {_usd(d.amount):>12}  {d.status}")
    typer.echo("")
    typer.echo(f"floor: {_usd(forecast.floor)} (from {forecast.floor_from})")
    typer.echo(f"total call: {_usd(forecast.total_call)}")
    typer.echo("per member:")
    for number, amount in forecast.members:
        typer.echo(f"  {name_of(number)}: {_usd(amount)}")


def _refuse_sandbox_forecast(env: MercuryEnv, target: Target) -> None:
    """Refuse, before any request, a capital call computed from seeded sandbox data."""
    if env is MercuryEnv.SANDBOX and target is Target.BOOKS:
        raise typer.BadParameter(
            "refusing to forecast the books from sandbox data; the sandbox is seeded and the capital call is real money"
        )


def _compute_forecast(books: Books, env: MercuryEnv, ledger: Client) -> Forecast:
    forecast_file = load_forecast(books.forecast_path, Chart.load(load_chart(books.chart_path)))
    now = _now()
    with MercuryClient(mercury_config(books.site, env)) as mercury:
        transactions, balances, _ = read_everything(mercury)
    return compute(
        forecast_file,
        now=now,
        opening_cash=opening_cash(balances),
        ledger_through=ledger_through(ledger, now.date()),
        transactions=transactions,
    )


def _account_names(books: Books) -> dict[str, str]:
    return Chart.load(load_chart(books.chart_path)).postable


@forecast_app.command("plan")
def forecast_plan(ctx: typer.Context, env: EnvOption, target: TargetOption) -> None:
    """Compute the forecast and print it. Reads Mercury and the ledger; writes nothing."""
    books = _books(ctx)
    _refuse_sandbox_forecast(env, target)
    with _forecast_refusals():
        config = site_config(books.site, target, readonly=True)
    typer.echo(f"site: {config.url}")
    names = _account_names(books)
    with (
        _forecast_refusals(f"runningtab access provision --target {target.value} --profile reader"),
        Client(config) as ledger,
    ):
        forecast = _compute_forecast(books, env, ledger)
    _print_forecast(forecast, lambda number: f"{number} - {names[number]}")


@forecast_app.command("apply")
def forecast_apply(ctx: typer.Context, env: EnvOption, target: TargetOption) -> None:
    """Compute the forecast, print it, and record it as a new Cash Forecast document.

    Each run adds a document and never edits an earlier one, so what a past
    run told the members stays on record.
    """
    books = _books(ctx)
    _refuse_sandbox_forecast(env, target)
    with _forecast_refusals():
        config = site_config(books.site, target)
    typer.echo(f"site: {config.url}")
    names = _account_names(books)
    with _forecast_refusals(f"runningtab access provision --target {target.value}"), Client(config) as ledger:
        if not is_installed(ledger):
            typer.echo(_doctype_missing_sentence(config.url, target), err=True)
            raise typer.Exit(code=1)
        forecast = _compute_forecast(books, env, ledger)
        _print_forecast(forecast, lambda number: f"{number} - {names[number]}")
        try:
            abbr = company_abbr(ledger, _sole_company(ledger))
        except LookupError as exc:
            raise SourceError(str(exc)) from exc
        try:
            name = write_forecast(ledger, forecast, link=lambda number: document_name(names[number], abbr, number))
        except PermissionError_ as exc:
            typer.echo(
                f"the books bot may not create {CASH_FORECAST} documents on {config.url.rstrip('/')}; "
                f"run `runningtab access provision --target {target.value}` as an administrator first",
                err=True,
            )
            raise typer.Exit(code=1) from exc
    typer.echo("")
    typer.echo(f"wrote {name}")
    typer.echo(_desk_url(config.url, CASH_FORECAST, name))


@forecast_app.command("install-doctype")
def forecast_install_doctype(ctx: typer.Context, target: TargetOption, yes: YesOption = False) -> None:
    """Create the Cash Forecast doctypes, as an administrator.

    The books bot cannot do this: it may write forecasts, not reshape
    doctypes. Run it before `runningtab access provision`, which grants the bot the
    doctype. Re-running repairs a doctype whose fields drifted.
    """
    config = admin_config(_books(ctx).site, target)
    typer.echo(f"site: {config.url}")
    _confirm(yes, f"create the {CASH_FORECAST} doctypes on {config.url}?")
    with _forecast_refusals(), Client(config) as client:
        for label, outcome in install_doctypes(client):
            typer.echo(f"  {outcome:9} {label}")
