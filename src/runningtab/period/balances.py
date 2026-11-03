"""The trial balance, and the statements and listings derived from the same rows.

Opening balances follow ERPNext's own Trial Balance report so the two can be
compared account by account: a balance-sheet account opens with everything
before the quarter, a P&L account with everything since the fiscal year
began. P&L from earlier years that no Period Closing Voucher closed is carried
as one line, so the trial balance and balance sheet balance without one.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal

from icedcoffee_sync.apply.model import DesiredAccount

from runningtab.period import PeriodError
from runningtab.period.csvfmt import money, render_csv
from runningtab.period.ledger import AccountIndex, GLRow, require_known_accounts
from runningtab.period.quarter import FiscalYear, Quarter

PRIOR_YEAR_LABEL = "Prior-year earnings, unclosed"
CURRENT_YEAR_LABEL = "Current-year earnings"
_ZERO = Decimal(0)


def is_profit_and_loss(account: DesiredAccount) -> bool:
    return account.root_type in ("Income", "Expense")


@dataclasses.dataclass(frozen=True)
class AccountBalance:
    """One postable account's figures for the quarter. Every figure is debit minus credit."""

    account: DesiredAccount
    opening: Decimal
    period_debit: Decimal
    period_credit: Decimal

    @property
    def closing(self) -> Decimal:
        return self.opening + self.period_debit - self.period_credit


@dataclasses.dataclass(frozen=True)
class TrialBalance:
    quarter: Quarter
    fiscal_year: FiscalYear
    lines: tuple[AccountBalance, ...]
    prior_year_unclosed: Decimal

    def _line(self, number: str) -> AccountBalance:
        for line in self.lines:
            if line.account.number == number:
                return line
        raise KeyError(number)

    def closing(self, number: str) -> Decimal:
        return self._line(number).closing

    def opening(self, number: str) -> Decimal:
        return self._line(number).opening


def _refuse_out_of_scope(rows: Sequence[GLRow], index: AccountIndex, quarter: Quarter, fy: FiscalYear) -> None:
    """Refuse rows this trial balance cannot place without double-counting or reading past the quarter."""
    pcv = sorted({r.voucher_no for r in rows if r.voucher_type == "Period Closing Voucher"})
    if pcv:
        raise PeriodError(f"Period Closing Vouchers are not handled yet: {', '.join(pcv)}")
    odd = sorted(
        r.name for r in rows if r.is_opening and is_profit_and_loss(index[r.account]) and r.posting_date >= fy.start
    )
    if odd:
        raise PeriodError(f"P&L entries marked as opening inside the fiscal year are not handled: {', '.join(odd)}")
    # A balance-sheet account's `is_opening` row dated inside the quarter is
    # ERPNext's own opening-balance convention (see the module docstring),
    # but `journal_entries` and `general_ledger_csv` both take every voucher
    # dated in the quarter as period activity regardless of `is_opening` --
    # so this row would land in both the opening balance here and the
    # period's general ledger and QBO kit. Refuse rather than silently
    # double-count it.
    double_counted = sorted(
        (
            r
            for r in rows
            if r.is_opening
            and not is_profit_and_loss(index[r.account])
            and quarter.start <= r.posting_date <= quarter.end
        ),
        key=lambda r: r.name,
    )
    if double_counted:
        names = ", ".join(r.name for r in double_counted)
        vouchers = ", ".join(sorted({r.voucher_no for r in double_counted}))
        day_before = quarter.start - dt.timedelta(days=1)
        raise PeriodError(
            f"opening entries dated inside {quarter.label} would be counted both as an opening balance and "
            f"as period activity: GL Entries {names} (voucher(s) {vouchers}); opening entries must be dated "
            f"on or before {day_before.isoformat()}, the day before {quarter.label} starts"
        )
    future = [r for r in rows if r.posting_date > quarter.end]
    if future:
        latest = max(r.posting_date for r in future)
        raise PeriodError(
            f"{len(future)} GL Entries are dated after {quarter.label} ends ({quarter.end.isoformat()}); "
            f"the latest is dated {latest.isoformat()}. A caller must filter its read to the quarter's end; "
            "this refusal does not depend on it."
        )


def trial_balance(
    rows: Sequence[GLRow], index: AccountIndex, quarter: Quarter, fiscal_year: FiscalYear
) -> TrialBalance:
    require_known_accounts(rows, index)
    _refuse_out_of_scope(rows, index, quarter, fiscal_year)
    opening: dict[str, Decimal] = defaultdict(Decimal)
    debit: dict[str, Decimal] = defaultdict(Decimal)
    credit: dict[str, Decimal] = defaultdict(Decimal)
    prior = _ZERO
    for row in rows:
        account = index[row.account]
        in_quarter = quarter.start <= row.posting_date <= quarter.end
        if is_profit_and_loss(account):
            if row.posting_date < fiscal_year.start:
                prior += row.net
            elif in_quarter:
                debit[account.number] += row.debit
                credit[account.number] += row.credit
            else:
                opening[account.number] += row.net
        elif in_quarter and not row.is_opening:
            debit[account.number] += row.debit
            credit[account.number] += row.credit
        else:
            opening[account.number] += row.net
    postable = sorted((a for a in index.values() if not a.is_group), key=lambda a: a.number)
    lines = tuple(AccountBalance(a, opening[a.number], debit[a.number], credit[a.number]) for a in postable)
    total = sum((line.closing for line in lines), _ZERO) + prior
    if total != 0:
        raise PeriodError(
            f"the ledger read does not balance: debits exceed credits by {total}; "
            "the read was short or a voucher is one-sided"
        )
    return TrialBalance(quarter, fiscal_year, lines, prior)


def _split(net: Decimal) -> tuple[str, str]:
    return (money(net), money(_ZERO)) if net >= 0 else (money(_ZERO), money(-net))


def trial_balance_csv(tb: TrialBalance) -> bytes:
    """One row per postable account by number, then the prior-year line. Net presentation."""
    header = [
        "number",
        "name",
        "root_type",
        "opening_debit",
        "opening_credit",
        "period_debit",
        "period_credit",
        "closing_debit",
        "closing_credit",
    ]
    rows = [
        [
            line.account.number,
            line.account.name,
            line.account.root_type,
            *_split(line.opening),
            money(line.period_debit),
            money(line.period_credit),
            *_split(line.closing),
        ]
        for line in tb.lines
    ]
    rows.append([
        "",
        PRIOR_YEAR_LABEL,
        "Equity",
        *_split(tb.prior_year_unclosed),
        money(_ZERO),
        money(_ZERO),
        *_split(tb.prior_year_unclosed),
    ])
    return render_csv(header, rows)


def profit_and_loss_csv(tb: TrialBalance) -> bytes:
    """Income then expense accounts by number; amounts in their natural sign (income and expense both positive)."""
    header = ["number", "name", "root_type", "quarter", "year_to_date"]
    rows: list[list[str]] = []
    totals = {"Income": [_ZERO, _ZERO], "Expense": [_ZERO, _ZERO]}
    for root in ("Income", "Expense"):
        sign = -1 if root == "Income" else 1
        for line in tb.lines:
            if line.account.root_type != root:
                continue
            quarter = sign * (line.period_debit - line.period_credit)
            ytd = sign * line.closing
            totals[root][0] += quarter
            totals[root][1] += ytd
            rows.append([line.account.number, line.account.name, root, money(quarter), money(ytd)])
        rows.append(["", f"Total {root.lower()}", root, money(totals[root][0]), money(totals[root][1])])
    net_q = totals["Income"][0] - totals["Expense"][0]
    net_y = totals["Income"][1] - totals["Expense"][1]
    rows.append(["", "Net income", "", money(net_q), money(net_y)])
    return render_csv(header, rows)


def balance_sheet_csv(tb: TrialBalance) -> bytes:
    """As of the quarter's end. Assets debit-positive; liabilities and equity credit-positive."""
    header = ["number", "name", "root_type", "amount"]
    rows: list[list[str]] = []
    assets = sum((line.closing for line in tb.lines if line.account.root_type == "Asset"), _ZERO)
    for line in tb.lines:
        if line.account.root_type == "Asset":
            rows.append([line.account.number, line.account.name, "Asset", money(line.closing)])
    rows.append(["", "Total assets", "Asset", money(assets)])
    other = _ZERO
    for root in ("Liability", "Equity"):
        for line in tb.lines:
            if line.account.root_type == root:
                other += -line.closing
                rows.append([line.account.number, line.account.name, root, money(-line.closing)])
    current = -sum((line.closing for line in tb.lines if is_profit_and_loss(line.account)), _ZERO)
    prior = -tb.prior_year_unclosed
    rows.append(["", CURRENT_YEAR_LABEL, "Equity", money(current)])
    rows.append(["", PRIOR_YEAR_LABEL, "Equity", money(prior)])
    other += current + prior
    rows.append(["", "Total liabilities and equity", "", money(other)])
    if assets != other:
        raise PeriodError(f"the balance sheet does not balance: assets {assets}, liabilities and equity {other}")
    return render_csv(header, rows)


def chart_of_accounts_csv(chart: Sequence[DesiredAccount]) -> bytes:
    """Every account in chart.coa file order: number, name, parent, root type, account type, group."""
    header = ["number", "name", "parent", "root_type", "account_type", "is_group"]
    rows = [
        [a.number, a.name, a.parent_number or "", a.root_type, a.account_type or "", "1" if a.is_group else "0"]
        for a in chart
    ]
    return render_csv(header, rows)


def general_ledger_csv(rows: Sequence[GLRow], index: AccountIndex, quarter: Quarter) -> bytes:
    """Every GL Entry dated in the quarter, in ledger read order."""
    header = [
        "posting_date",
        "voucher_type",
        "voucher_no",
        "number",
        "account",
        "party_type",
        "party",
        "debit",
        "credit",
        "against_voucher",
        "remarks",
        "gl_entry",
    ]
    out = [
        [
            r.posting_date.isoformat(),
            r.voucher_type,
            r.voucher_no,
            index[r.account].number,
            index[r.account].name,
            r.party_type or "",
            r.party or "",
            money(r.debit),
            money(r.credit),
            r.against_voucher or "",
            r.remarks,
            r.name,
        ]
        for r in rows
        if quarter.start <= r.posting_date <= quarter.end
    ]
    return render_csv(header, out)
