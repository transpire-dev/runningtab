"""One read of the ledger, parsed exactly.

Every submitted, non-cancelled GL Entry for the company through the quarter's
end. Paged with `iter_all`, never a capped `list`: a short read would build a
package that balances and is wrong.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Protocol

from icedcoffee_sync.apply.model import DesiredAccount
from icedcoffee_sync.erpnext.accounts import document_name

from runningtab.period import PeriodError
from runningtab.period.quarter import FiscalYear

GL_FIELDS = [
    "name",
    "posting_date",
    "account",
    "party_type",
    "party",
    "debit",
    "credit",
    "voucher_type",
    "voucher_no",
    "against_voucher_type",
    "against_voucher",
    "due_date",
    "remarks",
    "is_opening",
]

AccountIndex = dict[str, DesiredAccount]


class _Reader(Protocol):
    def iter_all(self, doctype: str, **kwargs: Any) -> Any: ...
    def get(self, doctype: str, name: str) -> Any: ...


@dataclasses.dataclass(frozen=True)
class GLRow:
    """One GL Entry, with money as exact decimals."""

    name: str
    posting_date: dt.date
    account: str
    party_type: str | None
    party: str | None
    debit: Decimal
    credit: Decimal
    voucher_type: str
    voucher_no: str
    against_voucher_type: str | None
    against_voucher: str | None
    due_date: dt.date | None
    remarks: str
    is_opening: bool

    @property
    def net(self) -> Decimal:
        return self.debit - self.credit


def _money(value: object) -> Decimal:
    # The client parses JSON with the standard library, so amounts arrive as
    # floats; `str()` first so the binary tail never reaches the arithmetic.
    if value in (None, ""):
        return Decimal(0)
    return Decimal(str(value))


def _text(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def _date(value: object) -> dt.date | None:
    return dt.date.fromisoformat(str(value)[:10]) if value not in (None, "") else None


def parse_gl_row(row: dict[str, Any]) -> GLRow:
    posting = _date(row["posting_date"])
    if posting is None:
        raise PeriodError(f"GL Entry {row.get('name')} has no posting date")
    return GLRow(
        name=str(row["name"]),
        posting_date=posting,
        account=str(row["account"]),
        party_type=_text(row.get("party_type")),
        party=_text(row.get("party")),
        debit=_money(row.get("debit")),
        credit=_money(row.get("credit")),
        voucher_type=str(row.get("voucher_type") or ""),
        voucher_no=str(row.get("voucher_no") or ""),
        against_voucher_type=_text(row.get("against_voucher_type")),
        against_voucher=_text(row.get("against_voucher")),
        due_date=_date(row.get("due_date")),
        remarks=str(row.get("remarks") or ""),
        is_opening=str(row.get("is_opening") or "No") == "Yes",
    )


def _order(row: GLRow) -> tuple[dt.date, str, str, str]:
    return (row.posting_date, row.voucher_type, row.voucher_no, row.name)


def _refuse_malformed_rows(rows: Sequence[GLRow]) -> None:
    """Refuse what offset paging over a non-unique sort key could still hand back.

    `read_gl` pages on `posting_date asc, name asc`, a unique order, but a
    reader depending on that guarantee should not trust it silently: refuse a
    duplicate GL Entry `name` (paging that returned the same row twice, or
    skipped one and duplicated another across a page boundary) and any row
    with no `voucher_type` or `voucher_no`, which nothing downstream can
    group into a voucher.
    """
    seen: dict[str, int] = {}
    for row in rows:
        seen[row.name] = seen.get(row.name, 0) + 1
    duplicates = sorted(name for name, count in seen.items() if count > 1)
    if duplicates:
        raise PeriodError(f"the GL Entry read returned duplicate rows: {', '.join(duplicates)}")
    unvouchered = sorted(row.name for row in rows if not row.voucher_type or not row.voucher_no)
    if unvouchered:
        raise PeriodError(
            f"GL Entries with no voucher_type or voucher_no cannot be grouped into a voucher: {', '.join(unvouchered)}"
        )


def read_gl(client: _Reader, company: str, through: dt.date) -> list[GLRow]:
    """Every live GL Entry for `company` dated on or before `through`, in package order.

    Pages on `order_by="posting_date asc, name asc"`: `name` breaks ties on a
    date, since offset paging over `posting_date asc` alone is a non-unique
    sort key that can silently duplicate or skip a row across a page
    boundary. `_refuse_malformed_rows` is the backstop for what a bad page
    could still let through.
    """
    rows = client.iter_all(
        "GL Entry",
        fields=GL_FIELDS,
        filters=[["company", "=", company], ["is_cancelled", "=", 0], ["posting_date", "<=", through.isoformat()]],
        order_by="posting_date asc, name asc",
    )
    parsed = sorted((parse_gl_row(row) for row in rows), key=_order)
    _refuse_malformed_rows(parsed)
    return parsed


def parse_fiscal_year(row: dict[str, Any]) -> FiscalYear:
    return FiscalYear(
        name=str(row["name"]),
        start=dt.date.fromisoformat(str(row["year_start_date"])[:10]),
        end=dt.date.fromisoformat(str(row["year_end_date"])[:10]),
    )


def read_fiscal_years(client: _Reader, company: str) -> list[FiscalYear]:
    """Every enabled Fiscal Year that applies to `company`.

    Fiscal Year's `companies` child table (doctype `Fiscal Year Company`,
    field `company`) restricts which companies a year applies to -- an
    empty table means every company, so a year with rows naming other
    companies but not this one is excluded, and a year with no rows at all
    is kept. `iter_all`'s listing does not expand child tables, so each
    year is read individually with `client.get`; there are few Fiscal
    Years, so this stays cheap.
    """
    rows = client.iter_all(
        "Fiscal Year", fields=["name", "year_start_date", "year_end_date"], filters=[["disabled", "=", 0]]
    )
    years = []
    for row in rows:
        doc = client.get("Fiscal Year", str(row["name"]))
        companies = [str(c["company"]) for c in (doc.get("companies") or [])]
        if companies and company not in companies:
            continue
        years.append(parse_fiscal_year(row))
    return years


def index_accounts(chart: Sequence[DesiredAccount], abbr: str) -> AccountIndex:
    """Chart accounts by the document name ERPNext gives them on this company."""
    return {document_name(a.name, abbr, a.number): a for a in chart}


def require_known_accounts(rows: Sequence[GLRow], index: AccountIndex) -> None:
    """Refuse when any GL Entry sits on an account `chart.coa` does not describe.

    Such rows would otherwise have nowhere to go in the package, and dropping
    them would hand over books that balance and are wrong.
    """
    unknown = sorted({r.account for r in rows if r.account not in index})
    if unknown:
        raise PeriodError(
            "GL Entries sit on accounts chart.coa does not describe: "
            + "; ".join(unknown)
            + ". Add them to the chart (and apply it) or move the entries."
        )
