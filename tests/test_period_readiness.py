"""What blocks a quarter's close: suspense entries, unreconciled Bank Transactions, draft vouchers."""

import dataclasses
import datetime as dt
from decimal import Decimal
from typing import Any

from runningtab.chart import load_chart, suspense_account
from runningtab.health import suspense_account_name
from runningtab.period.quarter import Quarter
from runningtab.period.readiness import Blocker, read_blockers
from tests.conftest import EXAMPLE_BOOKS, PERIOD_ABBR, period_gl_rows

Q3 = Quarter.parse("2026Q3")
COMPANY = "Transpire"

_OPS = {
    "=": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "in": lambda a, b: a in b,
}


def _suspense_account_name() -> str:
    account = suspense_account(load_chart(EXAMPLE_BOOKS.chart_path))
    return suspense_account_name(account.number, account.name, PERIOD_ABBR)


SUSPENSE = _suspense_account_name()


def _bank_transaction(
    name: str, *, unallocated_amount: float, docstatus: int = 1, date: str = "2026-08-01"
) -> dict[str, Any]:
    return {
        "name": name,
        "company": COMPANY,
        "docstatus": docstatus,
        "date": date,
        "unallocated_amount": unallocated_amount,
    }


def _voucher(name: str, *, docstatus: int = 0, posting_date: str = "2026-08-02") -> dict[str, Any]:
    return {"name": name, "company": COMPANY, "docstatus": docstatus, "posting_date": posting_date}


class _Site:
    """A tiny in-memory Frappe: rows per doctype, filtered the way a real query would be, every call recorded.

    Applying `filters` here (rather than just recording them) is what lets a
    test prove a row `read_blockers` should exclude -- an already-allocated
    Bank Transaction, say -- never reaches its result, the same way ERPNext's
    own server-side filtering would exclude it.
    """

    def __init__(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def iter_all(self, doctype: str, **kwargs: Any) -> Any:
        self.calls.append((doctype, kwargs))
        rows = self.rows.get(doctype, [])
        for field, op, value in kwargs.get("filters", []):
            rows = [r for r in rows if _OPS[op](r[field], value)]
        return iter(rows)


def test_an_empty_site_and_a_suspense_free_quarter_has_no_blockers():
    # period_gl_rows() is the real captured fixture, which -- per the task
    # brief -- has no rows on the suspense account at all.
    client = _Site({})
    assert read_blockers(client, COMPANY, Q3, SUSPENSE, period_gl_rows()) == []


def test_each_kind_is_found_and_named():
    suspense_row = dataclasses.replace(period_gl_rows()[0], account=SUSPENSE, posting_date=Q3.start)
    client = _Site({
        "Bank Transaction": [_bank_transaction("BT-1", unallocated_amount=12.5)],
        "Journal Entry": [_voucher("JV-9")],
        "Purchase Invoice": [_voucher("PINV-3")],
    })

    blockers = read_blockers(client, COMPANY, Q3, SUSPENSE, [suspense_row])

    by_name = {b.name: b for b in blockers}
    assert by_name[suspense_row.name] == Blocker(
        kind="suspense", doctype="GL Entry", name=suspense_row.name, detail=by_name[suspense_row.name].detail
    )
    assert by_name["BT-1"] == Blocker(
        kind="unreconciled", doctype="Bank Transaction", name="BT-1", detail=by_name["BT-1"].detail
    )
    assert by_name["JV-9"] == Blocker(kind="draft", doctype="Journal Entry", name="JV-9", detail=by_name["JV-9"].detail)
    assert by_name["PINV-3"] == Blocker(
        kind="draft", doctype="Purchase Invoice", name="PINV-3", detail=by_name["PINV-3"].detail
    )
    assert {b.kind for b in blockers} == {"suspense", "unreconciled", "draft"}


def test_blockers_are_sorted_by_kind_then_name():
    suspense_row = dataclasses.replace(period_gl_rows()[0], account=SUSPENSE, posting_date=Q3.start)
    client = _Site({
        "Bank Transaction": [
            _bank_transaction("BT-2", unallocated_amount=1),
            _bank_transaction("BT-1", unallocated_amount=1),
        ],
        "Journal Entry": [_voucher("JV-9")],
    })

    blockers = read_blockers(client, COMPANY, Q3, SUSPENSE, [suspense_row])

    assert blockers == sorted(blockers, key=lambda b: (b.kind, b.name))
    assert [b.name for b in blockers if b.kind == "unreconciled"] == ["BT-1", "BT-2"]


def test_a_fully_allocated_bank_transaction_is_not_a_blocker():
    client = _Site({
        "Bank Transaction": [
            _bank_transaction("BT-1", unallocated_amount=0),
            _bank_transaction("BT-2", unallocated_amount=5),
        ]
    })

    blockers = read_blockers(client, COMPANY, Q3, SUSPENSE, [])

    assert [b.name for b in blockers] == ["BT-2"]


def test_a_draft_bank_transaction_is_not_a_blocker():
    # Only Journal Entry and Purchase Invoice drafts block; the spec never
    # names Bank Transaction among the draft doctypes, and read_documents
    # elsewhere already treats a draft Bank Transaction as ordinary.
    client = _Site({"Bank Transaction": [_bank_transaction("BT-1", unallocated_amount=5, docstatus=0)]})

    assert read_blockers(client, COMPANY, Q3, SUSPENSE, []) == []


def test_a_cancelled_bank_transaction_is_not_a_blocker():
    # docstatus 2 is cancelled; it matches neither the unreconciled query's
    # docstatus = 1 filter nor any other kind, so it blocks nothing.
    client = _Site({"Bank Transaction": [_bank_transaction("BT-1", unallocated_amount=5, docstatus=2)]})

    assert read_blockers(client, COMPANY, Q3, SUSPENSE, []) == []


def test_a_cancelled_journal_entry_is_not_a_draft_blocker():
    # docstatus 2 is cancelled; it matches neither the draft query's
    # docstatus = 0 filter nor any other kind, so it blocks nothing.
    client = _Site({"Journal Entry": [_voucher("JV-1", docstatus=2)]})

    assert read_blockers(client, COMPANY, Q3, SUSPENSE, []) == []


def test_a_bank_transaction_amount_with_float_noise_renders_as_money():
    # 0.1 + 0.2 as a Python float is 0.30000000000000004; the JSON client
    # hands rows back with exactly this kind of binary-float noise on any
    # computed amount. The detail text must still read "0.30", never the
    # raw float repr.
    client = _Site({"Bank Transaction": [_bank_transaction("BT-1", unallocated_amount=0.1 + 0.2)]})

    blockers = read_blockers(client, COMPANY, Q3, SUSPENSE, [])

    assert blockers == [
        Blocker(kind="unreconciled", doctype="Bank Transaction", name="BT-1", detail="0.30 unallocated on 2026-08-01")
    ]


def test_the_suspense_detail_is_money_formatted():
    row = dataclasses.replace(
        period_gl_rows()[0], account=SUSPENSE, posting_date=Q3.start, debit=Decimal("42.5"), credit=Decimal("0")
    )

    blockers = read_blockers(_Site({}), COMPANY, Q3, SUSPENSE, [row])

    assert blockers == [
        Blocker(
            kind="suspense",
            doctype="GL Entry",
            name=row.name,
            detail=f"{row.voucher_type} {row.voucher_no} posted 42.50 to suspense on {Q3.start.isoformat()}",
        )
    ]


def test_a_suspense_row_outside_the_quarter_is_not_a_blocker():
    outside = dataclasses.replace(period_gl_rows()[0], account=SUSPENSE, posting_date=Q3.start - dt.timedelta(days=1))

    assert read_blockers(_Site({}), COMPANY, Q3, SUSPENSE, [outside]) == []


def test_the_filters_include_the_company_and_the_quarter_bounds():
    client = _Site({})
    read_blockers(client, COMPANY, Q3, SUSPENSE, [])

    by_doctype = dict(client.calls)
    assert set(by_doctype) == {"Bank Transaction", "Journal Entry", "Purchase Invoice"}

    bt_filters = by_doctype["Bank Transaction"]["filters"]
    assert ["company", "=", COMPANY] in bt_filters
    assert ["docstatus", "=", 1] in bt_filters
    assert ["date", ">=", Q3.start.isoformat()] in bt_filters
    assert ["date", "<=", Q3.end.isoformat()] in bt_filters
    assert ["unallocated_amount", ">", 0] in bt_filters
    assert by_doctype["Bank Transaction"]["order_by"] == "name asc"

    for doctype in ("Journal Entry", "Purchase Invoice"):
        filters = by_doctype[doctype]["filters"]
        assert ["company", "=", COMPANY] in filters
        assert ["docstatus", "=", 0] in filters
        assert ["posting_date", ">=", Q3.start.isoformat()] in filters
        assert ["posting_date", "<=", Q3.end.isoformat()] in filters
        assert by_doctype[doctype]["order_by"] == "name asc"
