"""Check our package's figures against ERPNext's own Trial Balance and AR/AP Summary reports.

Plan 1's `trial_balance`/`open_items` recompute a quarter's figures from the raw
GL Entries -- our own arithmetic, however carefully checked, never balancing
against ERPNext's own reports is a silent way for the close to hand out wrong
books. This module reads the three reports a person would otherwise open by
hand (Trial Balance, Accounts Receivable Summary, Accounts Payable Summary)
through `frappe.desk.query_report.run`, and compares them line by line against
`TrialBalance`/`OpenItem`: any disagreement is named, never averaged away or
silently accepted.

The comparison is account-by-account and party-by-party, not total-by-total: a
compensating pair of errors that leaves the grand total unchanged would pass a
total-only check and still be wrong books.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any, Literal, Protocol

from runningtab.period.balances import TrialBalance
from runningtab.period.csvfmt import money
from runningtab.period.ledger import AccountIndex
from runningtab.period.open_items import Kind, OpenItem
from runningtab.period.package import Package
from runningtab.period.quarter import FiscalYear, Quarter

TRIAL_BALANCE_REPORT = "Trial Balance"
# The Trial Balance report's own grand-total row names itself this, quoted by
# ERPNext itself -- see the committed tests/fixtures/period/trial-balance-report.json.
_TRIAL_BALANCE_TOTAL_ACCOUNT = "'Total'"
_PARTY_SUMMARY_REPORT: dict[Kind, str] = {
    "Receivable": "Accounts Receivable Summary",
    "Payable": "Accounts Payable Summary",
}
_ZERO = Decimal(0)


class _Caller(Protocol):
    def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any: ...


def read_trial_balance_report(
    client: _Caller, company: str, fiscal_year: FiscalYear, quarter: Quarter
) -> list[dict[str, Any]]:
    """ERPNext's own Trial Balance report for `quarter`, one row per account (postable or group) with activity, plus the report's own grand-total row.

    Net presentation (`show_net_values: 1`) so each row's debit/credit columns
    match `AccountBalance`'s sign convention directly, and every closing
    figure Period Closing Vouchers would otherwise fold in is left out
    (`with_period_closing_entry_for_*: 0`) since plan 1 does not handle those
    either (`runningtab.period.balances._refuse_out_of_scope`).

    Only a row that is not even a dict, or a dict with no `account` at all, is
    dropped here. The report's own grand-total row -- `{"account": "'Total'",
    "account_name": "'Total'", ...}`, quoted by ERPNext itself; see the
    committed `tests/fixtures/period/trial-balance-report.json`, whose last
    row is exactly this -- is a dict with an `account` like any other row, so
    it passes through unfiltered. `trial_balance_mismatches` is what
    recognizes and skips it, by the one thing that actually sets it apart: it
    carries no `is_group_account` key, which every real account row does.
    """
    result = client.call(
        "frappe.desk.query_report.run",
        {
            "report_name": TRIAL_BALANCE_REPORT,
            "filters": {
                "company": company,
                "fiscal_year": fiscal_year.name,
                "from_date": quarter.start.isoformat(),
                "to_date": quarter.end.isoformat(),
                "show_net_values": 1,
                "show_unclosed_fy_pl_balances": 0,
                "with_period_closing_entry_for_opening": 0,
                "with_period_closing_entry_for_current_period": 0,
                "include_default_book_entries": 1,
            },
        },
    )
    return [row for row in result["result"] if isinstance(row, dict) and row.get("account")]


def read_party_summary(
    client: _Caller, company: str, as_of: dt.date, kind: Literal["Receivable", "Payable"]
) -> list[dict[str, Any]]:
    """ERPNext's own Accounts Receivable/Payable Summary report as of `as_of`, one row per party with an outstanding balance.

    Like the Trial Balance report, the grand-total row is a bare list, not a
    dict, and is dropped along with anything else missing a `party`.
    """
    result = client.call(
        "frappe.desk.query_report.run",
        {
            "report_name": _PARTY_SUMMARY_REPORT[kind],
            "filters": {
                "company": company,
                "report_date": as_of.isoformat(),
                "ageing_based_on": "Due Date",
                "range": "30, 60, 90, 120",
            },
        },
    )
    return [row for row in result["result"] if isinstance(row, dict) and row.get("party")]


def _decimal(row: dict[str, Any], field: str) -> Decimal:
    return Decimal(str(row.get(field) or 0))


def _trial_balance_figures(row: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    return (
        _decimal(row, "opening_debit") - _decimal(row, "opening_credit"),
        _decimal(row, "debit"),
        _decimal(row, "credit"),
        _decimal(row, "closing_debit") - _decimal(row, "closing_credit"),
    )


def trial_balance_mismatches(ours: TrialBalance, theirs: list[dict[str, Any]], index: AccountIndex) -> list[str]:
    """Every postable account whose opening, period debit, period credit, or closing figure disagrees with `theirs`.

    `theirs` rows are matched to `ours.lines` by account document name through
    `index`, the same key `GLRow.account` uses, rather than by parsing the
    report's human-readable account string. Four kinds of row in `theirs` are
    not a postable account to compare against `ours.lines`, and each is
    handled differently:

    - The report's own grand-total row carries no `is_group_account` key at
      all (see `read_trial_balance_report`'s docstring) *and* names itself
      `account == "'Total'"` -- skipped outright, never reported. Both
      conditions are checked, not just the missing key: the key's absence
      alone is a structural accident of this one row on this ERPNext
      version, not a guarantee, and treating it alone as "this is the total
      row" would silently drop a real account row that happened to lack the
      key for some other reason.
    - Any other row missing `is_group_account` -- a shape this module does
      not recognize, not the report's own total row -- is reported by name
      rather than silently dropped: `"<account>: unrecognised row shape in
      ERPNext's trial balance (no is_group_account)"`.
    - A group account's row (`is_group_account` truthy) is a subtotal
      `ours.lines` does not carry one of -- skipped outright, never reported.
    - A row whose `account` is simply not in `index` is chart drift: ERPNext
      has an account this run's `chart.coa` does not describe. An all-zero
      row is skipped (there is nothing in it to disagree with, and the
      export's own live-chart check already refuses chart drift elsewhere);
      a row with any nonzero figure is reported by name, since it is real
      money `ours` has nowhere to put and would otherwise vanish silently.

    A postable account that `theirs` has no row for at all (no activity all
    quarter, in ERPNext's own report) is treated as all-zero, not as a
    missing-row mismatch: `ours.lines` always carries every postable account,
    active or not, and a truly inactive one is expected to be absent from
    ERPNext's report. `ours.prior_year_unclosed` has no line in ERPNext's own
    report to compare against and is not checked here.
    """
    by_number: dict[str, dict[str, Any]] = {}
    mismatches: list[str] = []
    for row in theirs:
        if "is_group_account" not in row:
            if row.get("account") == _TRIAL_BALANCE_TOTAL_ACCOUNT:
                continue
            mismatches.append(
                f"{row.get('account')}: unrecognised row shape in ERPNext's trial balance (no is_group_account)"
            )
            continue
        if row["is_group_account"]:
            continue
        account_name = row.get("account")
        account = index.get(account_name) if account_name else None
        if account is None:
            opening, debit, credit, closing = _trial_balance_figures(row)
            if any((opening, debit, credit, closing)):
                mismatches.append(
                    f"{account_name}: in ERPNext's trial balance but not in chart.coa "
                    f"(opening {money(opening)}, period debit {money(debit)}, period credit {money(credit)}, "
                    f"closing {money(closing)})"
                )
            continue
        by_number[account.number] = row

    for line in ours.lines:
        row = by_number.get(line.account.number)
        label = f"{line.account.number} {line.account.name}"
        their_opening, their_debit, their_credit, their_closing = (
            _trial_balance_figures(row) if row else (_ZERO, _ZERO, _ZERO, _ZERO)
        )
        for our_value, their_value, figure in (
            (line.opening, their_opening, "opening"),
            (line.period_debit, their_debit, "period debit"),
            (line.period_credit, their_credit, "period credit"),
            (line.closing, their_closing, "closing"),
        ):
            if our_value != their_value:
                mismatches.append(f"{label} {figure}: ours {money(our_value)}, ERPNext {money(their_value)}")
    return mismatches


def open_item_mismatches(ours: Sequence[OpenItem], theirs: list[dict[str, Any]], kind: Kind) -> list[str]:
    """Every party whose total open amount disagrees with ERPNext's own AR/AP Summary `outstanding`.

    Summed per party on our side (`OpenItem` is one row per document, the
    report is one row per party) so the two are compared at the same grain.
    ERPNext's `outstanding` is positive for money owed to us (Receivable) or
    by us (Payable) -- the same sign `runningtab.period.open_items.open_items`
    already gives `OpenItem.amount` -- so no sign flip is needed for either
    kind.
    """
    ours_by_party: dict[str, Decimal] = defaultdict(Decimal)
    for item in ours:
        ours_by_party[item.party] += item.amount
    theirs_by_party: dict[str, Decimal] = defaultdict(Decimal)
    for row in theirs:
        theirs_by_party[str(row["party"])] += _decimal(row, "outstanding")

    mismatches: list[str] = []
    for party in sorted({*ours_by_party, *theirs_by_party}):
        our_amount = ours_by_party.get(party, _ZERO)
        their_amount = theirs_by_party.get(party, _ZERO)
        if our_amount != their_amount:
            mismatches.append(f"{party}: ours {money(our_amount)}, ERPNext {money(their_amount)}")
    return mismatches


def bind_crosscheck(
    client: _Caller, company: str, index: AccountIndex
) -> Callable[[Package, TrialBalance, tuple[OpenItem, ...], tuple[OpenItem, ...], Quarter, FiscalYear], list[str]]:
    """Bind a live `client`, `company`, and chart `index` into `close.py`'s `CloseSteps.crosscheck` shape.

    `package` is accepted (the close ceremony always passes it) but unused: it
    carries only the rendered files, and every figure this module checks
    already comes through `trial_balance`/`open_items` as `TrialBalance` and
    `OpenItem`.
    """

    def crosscheck(
        package: Package,
        trial_balance: TrialBalance,
        receivables: tuple[OpenItem, ...],
        payables: tuple[OpenItem, ...],
        quarter: Quarter,
        fiscal_year: FiscalYear,
    ) -> list[str]:
        del package
        theirs_tb = read_trial_balance_report(client, company, fiscal_year, quarter)
        theirs_ar = read_party_summary(client, company, quarter.end, "Receivable")
        theirs_ap = read_party_summary(client, company, quarter.end, "Payable")
        return [
            *trial_balance_mismatches(trial_balance, theirs_tb, index),
            *open_item_mismatches(receivables, theirs_ar, "Receivable"),
            *open_item_mismatches(payables, theirs_ap, "Payable"),
        ]

    return crosscheck
