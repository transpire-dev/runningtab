"""Reading the ledger: parsing GL Entries exactly, and refusing accounts the chart does not know."""

import dataclasses
import datetime as dt
from decimal import Decimal
from typing import Any

import pytest

from runningtab.chart import load_chart
from runningtab.period import PeriodError
from runningtab.period.ledger import GL_FIELDS, index_accounts, read_fiscal_years, read_gl, require_known_accounts
from tests.conftest import EXAMPLE_BOOKS, PERIOD_ABBR, _period_gl, period_gl_rows


def test_amounts_parse_exactly():
    rows = period_gl_rows()
    assert all(isinstance(r.debit, Decimal) and isinstance(r.credit, Decimal) for r in rows)
    assert sum((r.net for r in rows), Decimal(0)) == 0


def test_the_opening_entry_is_marked():
    assert [r.is_opening for r in period_gl_rows() if r.posting_date == dt.date(2025, 3, 1)] == [True, True]


def test_a_part_payment_points_at_its_invoice():
    payment = [r for r in period_gl_rows() if r.posting_date == dt.date(2026, 8, 1) and r.party]
    assert payment[0].against_voucher_type == "Journal Entry"
    assert payment[0].against_voucher


def test_every_captured_account_is_in_the_chart():
    require_known_accounts(period_gl_rows(), index_accounts(load_chart(EXAMPLE_BOOKS.chart_path), PERIOD_ABBR))


def test_an_account_the_chart_does_not_know_is_refused_by_name():
    rows = period_gl_rows()
    stray = [
        dataclasses.replace(rows[0], account="9999 - Made In The UI - X"),
        dataclasses.replace(rows[1], account="Another - X"),
    ]
    with pytest.raises(
        PeriodError, match=r"9999 - Made In The UI - X.*Another - X|Another - X.*9999 - Made In The UI - X"
    ):
        require_known_accounts([*rows, *stray], index_accounts(load_chart(EXAMPLE_BOOKS.chart_path), PERIOD_ABBR))


class _Recorder:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows, self.calls = rows, []

    def iter_all(self, doctype: str, **kwargs: Any) -> Any:
        self.calls.append((doctype, kwargs))
        return iter(self.rows)

    def get(self, doctype: str, name: str) -> Any:
        raise NotImplementedError("read_gl never calls get()")


def test_the_read_asks_for_live_rows_through_the_quarter_end_and_sorts_them():
    raw = list(reversed(_period_gl()["rows"]))
    client = _Recorder(raw)
    rows = read_gl(client, "Transpire", dt.date(2026, 9, 30))
    doctype, kwargs = client.calls[0]
    assert doctype == "GL Entry"
    assert kwargs["fields"] == GL_FIELDS
    assert ["is_cancelled", "=", 0] in kwargs["filters"]
    assert ["posting_date", "<=", "2026-09-30"] in kwargs["filters"]
    assert ["company", "=", "Transpire"] in kwargs["filters"]
    assert kwargs["order_by"] == "posting_date asc, name asc"
    assert rows == sorted(rows, key=lambda r: (r.posting_date, r.voucher_type, r.voucher_no, r.name))


def test_a_duplicate_gl_entry_name_is_refused_by_name():
    raw = list(_period_gl()["rows"])
    dup = dict(raw[0])
    client = _Recorder([*raw, dup])
    with pytest.raises(PeriodError, match=raw[0]["name"]):
        read_gl(client, "Transpire", dt.date(2026, 9, 30))


@pytest.mark.parametrize("field", ["voucher_type", "voucher_no"])
def test_a_row_with_no_voucher_is_refused_by_name(field):
    raw = list(_period_gl()["rows"])
    broken = dict(raw[0])
    broken[field] = ""
    broken["name"] = "no-voucher-row"
    client = _Recorder([*raw[1:], broken])
    with pytest.raises(PeriodError, match="no-voucher-row"):
        read_gl(client, "Transpire", dt.date(2026, 9, 30))


class _FiscalYearReader:
    """`iter_all`'s listing has no child tables; `get` answers each year's full document, `companies` included."""

    def __init__(self, listing: list[dict[str, Any]], docs: dict[str, dict[str, Any]]) -> None:
        self.listing = listing
        self.docs = docs
        self.get_calls: list[tuple[str, str]] = []

    def iter_all(self, doctype: str, **kwargs: Any) -> Any:
        assert doctype == "Fiscal Year"
        return iter(self.listing)

    def get(self, doctype: str, name: str) -> Any:
        self.get_calls.append((doctype, name))
        return self.docs[name]


def _fy_row(name: str, start: str, end: str) -> dict[str, Any]:
    return {"name": name, "year_start_date": start, "year_end_date": end}


def _fy_doc(name: str, start: str, end: str, companies: tuple[str, ...] = ()) -> dict[str, Any]:
    # Frappe's own child-row shape: the parent's own fields, plus the row's
    # doctype metadata (parent/parentfield/parenttype/idx), the same as a
    # real `Fiscal Year Company` row.
    return {
        **_fy_row(name, start, end),
        "companies": [
            {
                "company": c,
                "parent": name,
                "parentfield": "companies",
                "parenttype": "Fiscal Year",
                "doctype": "Fiscal Year Company",
                "idx": i + 1,
            }
            for i, c in enumerate(companies)
        ],
    }


def test_a_year_restricted_to_another_company_is_excluded_an_unrestricted_one_is_kept():
    listing = [_fy_row("2026", "2026-01-01", "2026-12-31"), _fy_row("2025", "2025-01-01", "2025-12-31")]
    docs = {
        "2026": _fy_doc("2026", "2026-01-01", "2026-12-31", companies=("Other Co",)),
        # An empty (or absent) companies table means the year applies to
        # every company -- not "no company at all".
        "2025": _fy_doc("2025", "2025-01-01", "2025-12-31"),
    }
    client = _FiscalYearReader(listing, docs)
    years = read_fiscal_years(client, "Transpire")
    assert [y.name for y in years] == ["2025"]
    assert client.get_calls == [("Fiscal Year", "2026"), ("Fiscal Year", "2025")]


def test_a_year_naming_this_company_among_several_is_kept():
    listing = [_fy_row("2026", "2026-01-01", "2026-12-31")]
    docs = {"2026": _fy_doc("2026", "2026-01-01", "2026-12-31", companies=("Other Co", "Transpire"))}
    client = _FiscalYearReader(listing, docs)
    years = read_fiscal_years(client, "Transpire")
    assert [y.name for y in years] == ["2026"]
