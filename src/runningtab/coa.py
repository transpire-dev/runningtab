"""What `runningtab coa` does between the prompts: plan, apply, prune, park, clear the stock chart.

Everything here returns data or raises a typed error; `runningtab.cli` prints and
picks exit codes. The rules that matter: a refused chart plan is never
executed; a plan is re-made after the operator confirms and refused if it
changed; prune only ever touches extras; parking (a renumber) is confined
to `--allow-unapplied`.
"""

from __future__ import annotations

import contextlib
import dataclasses

from icedcoffee_sync.apply.diff import ChartPlan, Refusal, RefusalReason, diff
from icedcoffee_sync.apply.execute import ApplyReport, apply_chart
from icedcoffee_sync.apply.model import DesiredAccount, LiveAccount
from icedcoffee_sync.apply.park import ParkedNumber, park_colliding_numbers, parking_candidates, unresolved_collisions
from icedcoffee_sync.apply.prune import PrunePlan, PruneRefusal, PruneReport, execute_prune, plan_prune
from icedcoffee_sync.erpnext.accounts import accounts_with_entries, company_abbr, document_name, read_chart
from icedcoffee_sync.erpnext.company import clear_defaults_pointing_at, require_default_fields, set_default_accounts
from icedcoffee_sync.frappe.client import Client, SiteConfig
from icedcoffee_sync.frappe.errors import FrappeError

from runningtab.stock_chart import STANDARD_CHART_PARENTS

# Prune only touches extras, so a chart refusal blocks it only when it could
# make the extras set wrong: ROOT_NOT_UNIQUE breaks the tree walk and
# AMBIGUOUS_MATCH marks every candidate matched. The rest attach to a create
# or a matched account. An explicit allow-list, so a new reason is classified
# on purpose.
PRUNE_BLOCKING_REFUSAL_REASONS = frozenset({RefusalReason.ROOT_NOT_UNIQUE, RefusalReason.AMBIGUOUS_MATCH})


class NoSoleCompanyError(ValueError):
    """The site has no company, or more than one, so `--company` is required."""


class PlanChangedError(RuntimeError):
    """The chart changed between the plan the operator confirmed and now."""


@dataclasses.dataclass
class Context:
    """One planning pass against a site: the client, the chart, the live tree, the diff."""

    client: Client
    company: str
    abbr: str
    chart: list[DesiredAccount]
    by_number: dict[str, DesiredAccount]
    plan: ChartPlan
    live: list[LiveAccount]
    with_entries: set[str]
    # The chart's Company default-account fields, as account numbers.
    defaults: dict[str, str]


def sole_company(client: Client) -> str:
    """The site's only company; refuses to guess when there are none or several."""
    # Two rows through iter_all, never one capped list call: a server-side
    # page cap could make several companies look like one.
    companies = client.iter_all("Company", fields=["name"])
    first = next(companies, None)
    if first is None:
        raise NoSoleCompanyError("--company is required: this site has no companies")
    second = next(companies, None)
    if second is None:
        return str(first["name"])
    raise NoSoleCompanyError(
        f"--company is required: this site has more than one company (at least {first['name']!r} and {second['name']!r})"
    )


def plan_against(
    config: SiteConfig, company: str | None, chart: list[DesiredAccount], defaults: dict[str, str]
) -> Context:
    """Read the site and diff the chart against it.

    `chart` and `defaults` are the caller's parse of `chart.coa`, never read
    here: `period export` hands in its single snapshot, used for the
    live-chart check, the package, and the manifest hash, and every other
    caller reads the books directory's chart once and passes both halves.
    """
    client = Client(config)
    resolved = company or sole_company(client)
    live = read_chart(client, resolved)
    with_entries = accounts_with_entries(client, [a.document_name for a in live])
    return Context(
        client=client,
        company=resolved,
        abbr=company_abbr(client, resolved),
        chart=chart,
        by_number={a.number: a for a in chart},
        plan=diff(chart, live, with_entries),
        live=live,
        with_entries=with_entries,
        defaults=dict(defaults),
    )


def blocking_prune_refusals(refusals: list[Refusal]) -> list[Refusal]:
    """The chart refusals that make the extras set untrustworthy."""
    return [refusal for refusal in refusals if refusal.reason in PRUNE_BLOCKING_REFUSAL_REASONS]


def prune_plan_for(context: Context) -> PrunePlan:
    """Which extras prune may delete, reusing the ledger probe the plan already paid for."""
    extras = [extra.live for extra in context.plan.extras]
    return plan_prune(extras, context.live, context.with_entries)


def company_defaults(context: Context) -> dict[str, str]:
    """Company default-account fields mapped to the chart accounts' document names."""
    return {
        field: document_name(context.by_number[number].name, context.abbr, number)
        for field, number in context.defaults.items()
    }


@dataclasses.dataclass(frozen=True)
class ApplyOutcome:
    """What an apply did: the account changes (None when the plan was empty) and the defaults written."""

    report: ApplyReport | None
    defaults_written: list[str]


def apply_confirmed(context: Context, fresh_plan: ChartPlan) -> ApplyOutcome:
    """Execute a confirmed plan, after checking it still holds and the defaults can be set.

    Raises `PlanChangedError` if `fresh_plan` differs from the confirmed one,
    and `UnknownCompanyFieldError` before any account is touched if a
    default field is not on this build's Company.
    """
    if fresh_plan != context.plan:
        raise PlanChangedError("the chart plan changed since planning")
    require_default_fields(context.client, context.company, context.defaults.keys())
    report: ApplyReport | None = None
    if not context.plan.is_empty:
        report = apply_chart(
            context.client, context.plan, context.live, context.by_number, context.abbr, context.company
        )
    # Always set the defaults: an applied chart with unset defaults is a
    # half-configured ledger, and a rerun is how a failed default gets fixed.
    written = set_default_accounts(context.client, context.company, company_defaults(context))
    return ApplyOutcome(report=report, defaults_written=written)


@dataclasses.dataclass(frozen=True)
class ParkOutcome:
    """Survivors renumbered, those skipped for gaining entries, and collisions left standing."""

    gained_entries: list[str]
    parked: list[ParkedNumber]
    unresolved: list[str]


def park_survivors(
    client: Client,
    extras: list[LiveAccount],
    report: PruneReport,
    refusals: list[PruneRefusal],
    live: list[LiveAccount],
    chart_numbers: frozenset[str],
) -> ParkOutcome:
    """Renumber surviving extras off the chart's numbers, never one carrying ledger entries."""
    candidates, excluded = parking_candidates(extras, report, refusals)
    gained: set[str] = set()
    if candidates:
        # Plan-time knowledge is stale by now: the prompt and the deletes
        # both left time for an account to gain its first entry.
        gained = accounts_with_entries(client, [a.document_name for a in candidates])
        excluded = excluded + [a for a in candidates if a.document_name in gained]
        candidates = [a for a in candidates if a.document_name not in gained]
    taken = frozenset(a.number for a in live if a.number) | chart_numbers
    parked = park_colliding_numbers(client, candidates, chart_numbers, taken)
    return ParkOutcome(
        gained_entries=sorted(gained),
        parked=parked,
        unresolved=unresolved_collisions(excluded, chart_numbers),
    )


@dataclasses.dataclass(frozen=True)
class PruneOutcome:
    """What a prune did, in the order it happened."""

    cleared_defaults: list[str]
    report: PruneReport
    parking: ParkOutcome | None

    @property
    def failed(self) -> bool:
        """True when a delete failed without being parked, or a collision is left standing."""
        parked_names = {p.old_document_name for p in self.parking.parked} if self.parking else set()
        unresolved = bool(self.parking and self.parking.unresolved)
        return unresolved or any(name not in parked_names for name, _ in self.report.failed)


def prune_confirmed(context: Context, prune_plan: PrunePlan, *, allow_unapplied: bool) -> PruneOutcome:
    """Execute a confirmed prune plan; under `--allow-unapplied`, clear defaults first and park afterwards."""
    cleared: list[str] = []
    if allow_unapplied:
        # ERPNext refuses to retire an account the Company record names as a
        # default; apply repoints the defaults afterwards.
        deleting = frozenset(d.live.document_name for d in prune_plan.deletes)
        cleared = clear_defaults_pointing_at(context.client, context.company, deleting)
    report = execute_prune(context.client, prune_plan)
    parking: ParkOutcome | None = None
    if allow_unapplied:
        extras = [extra.live for extra in context.plan.extras]
        parking = park_survivors(
            context.client, extras, report, prune_plan.refusals, context.live, frozenset(context.by_number)
        )
    return PruneOutcome(cleared_defaults=cleared, report=report, parking=parking)


class LedgerNotEmptyError(RuntimeError):
    """The company's ledger holds entries, so the stock chart is real history and stays."""


@dataclasses.dataclass(frozen=True)
class StockClearPlan:
    """Which of ERPNext's stock accounts `coa clear-stock` deletes, children before parents, and which it keeps."""

    deletes: tuple[LiveAccount, ...]
    kept: tuple[tuple[LiveAccount, str], ...]


@dataclasses.dataclass(frozen=True)
class StockClearOutcome:
    """What a stock-chart clear did."""

    cleared_defaults: list[str]
    deleted: list[str]
    failed: list[tuple[str, str]]


def plan_stock_clear(live: list[LiveAccount]) -> StockClearPlan:
    """The stock-chart accounts on the site, deepest first; a stock group holding anything else is kept.

    An account is the stock chart's when it has no account number (the
    chart's own accounts always carry one), is not a root, its name and its
    parent's name are a pair in ERPNext's Standard template, and that parent
    is itself a root or the stock chart's -- so a stock name under one of our
    numbered accounts is never taken for ERPNext's.
    """
    by_name = {a.document_name: a for a in live}
    children: dict[str, list[LiveAccount]] = {}
    for account in live:
        if account.parent_document_name:
            children.setdefault(account.parent_document_name, []).append(account)

    def is_stock(account: LiveAccount) -> bool:
        parent = by_name.get(account.parent_document_name or "")
        if parent is None or account.number or STANDARD_CHART_PARENTS.get(account.name) != parent.name:
            return False
        return parent.parent_document_name is None or is_stock(parent)

    def depth(account: LiveAccount) -> int:
        level, parent = 0, account.parent_document_name
        while parent and parent in by_name:
            level, parent = level + 1, by_name[parent].parent_document_name
        return level

    def only_stock_below(account: LiveAccount) -> bool:
        return all(is_stock(child) and only_stock_below(child) for child in children.get(account.document_name, []))

    candidates = [a for a in live if is_stock(a)]
    deletes = [a for a in candidates if only_stock_below(a)]
    kept = [(a, "holds accounts outside ERPNext's stock chart") for a in candidates if not only_stock_below(a)]
    deletes.sort(key=lambda a: (-depth(a), a.document_name))
    kept.sort(key=lambda pair: pair[0].document_name)
    return StockClearPlan(deletes=tuple(deletes), kept=tuple(kept))


def ledger_is_empty(client: Client, company: str) -> bool:
    """Whether the company has no GL Entry at all, submitted or cancelled."""
    return not client.list("GL Entry", fields=["name"], filters=[["company", "=", company]], limit=1)


def _restore_defaults_if_still_there(client: Client, company: str, fields: list[str], name: str) -> None:
    """After a delete failed in transport, point the cleared defaults back at the account if it survived.

    Best effort while another error is already on its way up: a site that
    just dropped a connection may drop this one too, and that error must
    not replace the one that says what happened.
    """
    if not fields:
        return
    with contextlib.suppress(Exception):
        if client.list("Account", fields=["name"], filters=[["name", "=", name]], limit=1):
            client.update("Company", company, dict.fromkeys(fields, name))


def clear_stock_confirmed(client: Client, company: str, plan: StockClearPlan) -> StockClearOutcome:
    """Delete a confirmed plan's accounts, each after pointing Company's defaults away from it.

    The ledger is probed again here, after the prompt, so an entry posted
    while the prompt sat open still stops the run before anything changes.
    ERPNext refuses to delete an account a Company default names, so each
    account's defaults are cleared just before its delete and put back if
    the delete is refused: a refused account keeps its defaults. A refused
    delete is recorded and the run goes on; an account whose child is still
    standing is not attempted, since ERPNext would refuse it too.

    Raises:
        LedgerNotEmptyError: The company has a GL Entry.
    """
    if not ledger_is_empty(client, company):
        raise LedgerNotEmptyError(f"{company} has entries in its ledger; the stock chart is real history now")
    cleared: list[str] = []
    deleted: list[str] = []
    failed: list[tuple[str, str]] = []
    standing: set[str] = set()
    for account in plan.deletes:
        name = account.document_name
        if name in standing:
            failed.append((name, "a child account could not be deleted"))
        else:
            fields = clear_defaults_pointing_at(client, company, frozenset({name}))
            try:
                client.delete("Account", name)
            except FrappeError as error:
                if fields:
                    client.update("Company", company, dict.fromkeys(fields, name))
                failed.append((name, str(error)))
            except Exception:
                _restore_defaults_if_still_there(client, company, fields, name)
                raise
            else:
                cleared.extend(fields)
                deleted.append(name)
                continue
        if account.parent_document_name:
            standing.add(account.parent_document_name)
    return StockClearOutcome(cleared_defaults=sorted(cleared), deleted=deleted, failed=failed)
