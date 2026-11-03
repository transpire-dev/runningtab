"""Apply the real chart to a real disposable site, twice.

A fresh ERPNext site is not empty: it ships a stock chart of accounts, and
that stock chart collides with ours on some accounts (an ungrouped "Retained
Earnings" where our chart wants a group, for instance). The `cleared_site`
fixture removes exactly those colliding accounts, which is what reproduces
"set up the disposable site for a first real apply". Only then does the real
subject of this file run: apply the chart, then re-plan and assert the second
plan is empty. An engine that is not idempotent looks perfectly correct on the
first run and starts duplicating accounts on the second, which is the run
nobody watches.

Every test here is written to survive re-running against a site this suite
already applied to. Nothing may assert a property of a pristine site: the
first run destroys that state permanently, so such a test passes once and is
red forever after. What the stock chart collides on is pinned deterministically
by icedcoffee-sync's unit tests in `tests/test_apply_diff.py` instead.
"""

import pytest
from icedcoffee_sync.apply.diff import RefusalReason, diff
from icedcoffee_sync.apply.execute import apply_chart
from icedcoffee_sync.apply.prune import execute_prune, plan_prune
from icedcoffee_sync.erpnext.accounts import accounts_with_entries, company_abbr, document_name, read_chart
from icedcoffee_sync.erpnext.company import set_default_accounts
from icedcoffee_sync.frappe.client import Client

from runningtab.chart import load_chart, load_defaults
from runningtab.site import is_test_site
from tests.conftest import live_books, require_live_target

pytestmark = pytest.mark.live_runningtab

# Refusal reasons that mean "a live account structurally collides with a
_STRUCTURAL_COLLISION = frozenset({
    RefusalReason.GROUP_FLAG_CHANGED,
    RefusalReason.ROOT_TYPE_CHANGED,
    RefusalReason.AMBIGUOUS_MATCH,
})

# ROOT_NOT_UNIQUE is deliberately absent above. A missing or duplicated root is


@pytest.fixture
def client():
    config = require_live_target()
    if not is_test_site(live_books().site, config.url):
        # Fail, never skip. A skip here would read as "nothing to run" while
        pytest.fail(f"refusing to run against a host not in the books' test_hosts: {config.url}")
    with Client(config) as c:
        yield c


@pytest.fixture
def company(client) -> str:
    rows = client.list("Company", fields=["name"], limit=2)
    if len(rows) != 1:
        pytest.fail(f"expected exactly one company on the test site, found {len(rows)}")
    return rows[0]["name"]


def _plan(client, company):
    chart = load_chart(live_books().chart_path)
    live = read_chart(client, company)
    by_number = {a.number: a for a in chart}
    touched = [a.document_name for a in live]
    return chart, by_number, live, diff(chart, live, accounts_with_entries(client, touched))


def _apply(client, company, by_number, live, plan) -> None:
    """Apply a plan, then point Company's defaults at it.

    `apply_chart` alone only writes Account documents. Company's
    default-account links are a separate write
    (`icedcoffee_sync.erpnext.company.set_default_accounts`), and `runningtab coa apply`
    performs both as one operation; this mirrors that so the live suite
    exercises the real "apply the chart" flow, not half of it.
    """
    abbr = company_abbr(client, company)
    apply_chart(client, plan, live, by_number, abbr, company)
    defaults = {
        field: document_name(by_number[number].name, abbr, number)
        for field, number in load_defaults(live_books().chart_path).items()
    }
    set_default_accounts(client, company, defaults)


@pytest.fixture
def cleared_site(client, company) -> None:
    """Delete only the live accounts that structurally collide with the chart.

    A fresh site's stock chart is not simply a subset of ours to add to -- a
    handful of its accounts match ours but disagree in a way `diff` refuses to
    resolve on its own: a group-flag or root-type mismatch, or two siblings
    sharing a name. Each such refusal names the live accounts involved in
    `live_document_names`, and deleting exactly those -- nothing else; extras
    the chart doesn't mention are left alone, same as `diff` leaves them -- is
    what actually reproduces "set up the disposable site for a first real
    apply". So this fixture is test plumbing that models that operational step,
    not product functionality; `src/runningtab/cli.py` and `icedcoffee-sync` gain no
    delete/prune capability.

    Two independent checks gate every deletion, and either one failing is a
    hard `pytest.fail` -- never a skip, and never a silent leave-it-alone:

    - `accounts_with_entries(client, [name])` must report no GL entries, or
      the account is real ledger history and cannot be touched.
    - No other live account may name it as `parent_document_name`, or
      deleting it would orphan a child account.

    Runs only against the disposable test site: the `client` fixture above
    already refuses to run against the books host, and this fixture adds
    nothing that weakens that guard.
    """
    chart = load_chart(live_books().chart_path)
    live = read_chart(client, company)
    touched = [a.document_name for a in live]
    plan = diff(chart, live, accounts_with_entries(client, touched))

    to_delete: set[str] = set()
    for refusal in plan.refusals:
        if refusal.reason not in _STRUCTURAL_COLLISION:
            continue
        to_delete.update(refusal.live_document_names)

    parents = {a.parent_document_name for a in live if a.parent_document_name}
    for name in sorted(to_delete):
        if accounts_with_entries(client, [name]):
            pytest.fail(f"refusing to delete {name}: it carries GL entries")
        if name in parents:
            pytest.fail(f"refusing to delete {name}: it has child accounts")
        client.delete("Account", name)
        print(f"cleared stock-chart collision: {name}")


def test_apply_then_replan_is_a_no_op(cleared_site, client, company) -> None:
    _chart, by_number, live, plan = _plan(client, company)
    assert plan.is_applyable, [r.detail for r in plan.refusals]
    _apply(client, company, by_number, live, plan)

    _, _, _, second = _plan(client, company)
    assert second.is_empty, {
        "creates": [c.desired.number for c in second.creates],
        "renames": [r.desired.number for r in second.renames],
        "retypes": [r.desired.number for r in second.retypes],
    }


def test_every_chart_account_exists_afterwards(cleared_site, client, company) -> None:
    chart, by_number, live, plan = _plan(client, company)
    assert plan.is_applyable, [r.detail for r in plan.refusals]
    _apply(client, company, by_number, live, plan)
    live_numbers = {a.number for a in read_chart(client, company)}
    missing = sorted({a.number for a in chart} - live_numbers)
    assert not missing, f"chart accounts absent after apply: {missing}"


def test_company_defaults_point_at_chart_accounts(cleared_site, client, company) -> None:
    _chart, by_number, live, plan = _plan(client, company)
    assert plan.is_applyable, [r.detail for r in plan.refusals]
    _apply(client, company, by_number, live, plan)

    doc = client.get("Company", company)
    for field, number in load_defaults(live_books().chart_path).items():
        value = doc.get(field)
        assert value and value.startswith(f"{number} - "), f"{field} = {value!r}, expected {number}"


def test_prune_leaves_no_enabled_account_unaccounted_for(cleared_site, client, company) -> None:
    """Apply, then prune, then account for every enabled account left standing.

    This is the end-to-end proof the whole plan was building toward, but a
    clean sweep is not the achievable property: ERPNext owns some stock
    accounts outright -- Stock Adjustment, Stock Received But Not Billed,
    Gain/Loss on Asset Disposal -- and refuses to delete *or* disable them
    while nothing in our chart has a counterpart to take over their role
    (this business holds no inventory and has disposed of no assets, so
    inventing accounts to satisfy a cleanup would be backwards). Those stay
    enabled and referenced, and `execute_prune` reports them as failed.

    So the property this test actually proves is narrower and true: every
    chart account is on the site and enabled, and every enabled account
    that is *not* in the chart is one `execute_prune` already told us about
    -- no enabled stranger goes unexplained. A prior fix wired
    `accumulated_depreciation_account` / `depreciation_expense_account` at
    1230/5203, which is why Depreciation is not on this list: once the
    Company record stopped pointing at ERPNext's own Depreciation account,
    ERPNext's disable succeeded.

    Deliberately last in this file: it is the only test here that deletes
    or disables anything, and every other test in this module is written to
    tolerate (but never require) the stray extras a fresh site or a prior
    run left behind. Running this one first would remove those extras for
    the rest of the suite, which is not a precondition any earlier test
    asked for.
    """
    chart, by_number, _pre_live, plan = _plan(client, company)
    assert plan.is_applyable, [r.detail for r in plan.refusals]
    _apply(client, company, by_number, _pre_live, plan)

    live = read_chart(client, company)
    with_entries = accounts_with_entries(client, [a.document_name for a in live])
    post_apply_plan = diff(chart, live, with_entries)
    assert post_apply_plan.is_applyable, [r.detail for r in post_apply_plan.refusals]

    extras = [extra.live for extra in post_apply_plan.extras]
    prune_plan = plan_prune(extras, live, with_entries)
    assert not prune_plan.refusals, [r.detail for r in prune_plan.refusals]
    report = execute_prune(client, prune_plan)

    abbr = company_abbr(client, company)
    expected = {document_name(a.name, abbr, a.number) for a in chart}
    enabled = {a.document_name for a in read_chart(client, company) if not a.disabled}

    missing = expected - enabled
    assert not missing, f"chart accounts absent or disabled after prune: {sorted(missing)}"

    failed_names = {name for name, _ in report.failed}
    unexplained = (enabled - expected) - failed_names
    assert not unexplained, f"enabled accounts outside the chart with no recorded failure: {sorted(unexplained)}"
