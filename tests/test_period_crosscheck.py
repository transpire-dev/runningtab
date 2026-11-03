"""Our arithmetic checked against ERPNext's own Trial Balance and AR/AP Summary reports, on the real capture."""

from __future__ import annotations

import json
import pathlib
from typing import Any

from runningtab.chart import load_chart
from runningtab.period.balances import trial_balance
from runningtab.period.crosscheck import (
    bind_crosscheck,
    open_item_mismatches,
    read_party_summary,
    read_trial_balance_report,
    trial_balance_mismatches,
)
from runningtab.period.ledger import index_accounts
from runningtab.period.open_items import open_items
from runningtab.period.package import Package
from runningtab.period.quarter import Quarter, fiscal_year_for
from tests.conftest import EXAMPLE_BOOKS, PERIOD_ABBR, period_fiscal_years, period_gl_rows

Q3 = Quarter.parse("2026Q3")
INDEX = index_accounts(load_chart(EXAMPLE_BOOKS.chart_path), PERIOD_ABBR)
FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "period"

# The AR/AP Summary report's own grand-total row, exactly as observed on a
# live probe of the raw response (see tests/fixtures/period/README.md) -- a
# bare list matching `columns`' order, not a dict of named fields, which is
# what makes it worth its own test: `read_party_summary` must not choke on
# it. The Trial Balance report's own grand-total row is, unlike this one, a
# dict indistinguishable from an account row except for its missing
# `is_group_account` key -- the committed trial-balance-report.json's last
# row is exactly that, reused directly in the tests below rather than
# reconstructed here.
_RAW_PARTY_TOTAL_ROW = ["Total", "", 350.0, 2500.0, 600.0, "", 1550.0]


def _report(name: str) -> list[dict[str, Any]]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fiscal_year():
    return fiscal_year_for(Q3, period_fiscal_years())


def _tb():
    return trial_balance(period_gl_rows(), INDEX, Q3, _fiscal_year())


def _receivables():
    return tuple(open_items(period_gl_rows(), INDEX, "Receivable", Q3.end))


def _payables():
    return tuple(open_items(period_gl_rows(), INDEX, "Payable", Q3.end))


class FakeCaller:
    """A `client.call` double: records every call and answers from a canned `{report_name: envelope}` table.

    Only the network hop is faked -- the envelopes it answers with are either
    the real captured fixture rows or the literal raw shape observed on a
    live capture (see `_RAW_PARTY_TOTAL_ROW` above), so what is under test
    (the reader functions' own filtering) runs against real data either way.
    """

    def __init__(self, envelopes: dict[str, dict[str, Any]]) -> None:
        self._envelopes = envelopes
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((dotted_path, params or {}))
        assert params is not None
        return self._envelopes[params["report_name"]]


def test_our_trial_balance_matches_erpnexts_own_report():
    assert trial_balance_mismatches(_tb(), _report("trial-balance-report.json"), INDEX) == []


def test_a_changed_closing_figure_is_named():
    theirs = [
        dict(row, closing_debit=row["closing_debit"] + 1) if row.get("acc_number") == "1111" else row
        for row in _report("trial-balance-report.json")
    ]
    assert trial_balance_mismatches(_tb(), theirs, INDEX) == [
        "1111 Operating Checking closing: ours 5430.00, ERPNext 5431.00"
    ]


def test_a_changed_opening_figure_is_named_independently_of_closing():
    theirs = [
        dict(row, opening_debit=row["opening_debit"] + 5) if row.get("acc_number") == "5110" else row
        for row in _report("trial-balance-report.json")
    ]
    assert trial_balance_mismatches(_tb(), theirs, INDEX) == [
        "5110 Production Hosting (AWS, Fastly) opening: ours 120.00, ERPNext 125.00"
    ]


def test_a_postable_account_with_no_theirs_row_is_treated_as_a_zero_mismatch():
    # Every postable account appears in `ours.lines`, active or not; an
    # account genuinely inactive all quarter and absent from ERPNext's report
    # is compared against zero, not reported as "missing".
    theirs = [row for row in _report("trial-balance-report.json") if row.get("acc_number") != "3110"]
    assert trial_balance_mismatches(_tb(), theirs, INDEX) == [
        "3110 Capital Contribution – Member 1 opening: ours -5000.00, ERPNext 0.00",  # noqa: RUF001
        "3110 Capital Contribution – Member 1 closing: ours -5000.00, ERPNext 0.00",  # noqa: RUF001
    ]


def test_the_committed_total_row_is_not_reported():
    # `trial-balance-report.json`'s last row is ERPNext's own grand-total
    # row, present because `read_trial_balance_report` cannot tell it apart
    # from a real account row by shape (see its docstring) -- only
    # `trial_balance_mismatches` can, by the missing `is_group_account` key.
    theirs = _report("trial-balance-report.json")
    assert theirs[-1]["account"] == "'Total'"
    assert "is_group_account" not in theirs[-1]
    assert not any("'Total'" in m for m in trial_balance_mismatches(_tb(), theirs, INDEX))


def test_a_row_missing_is_group_account_that_is_not_the_total_row_is_reported():
    # A real account row (1111's) with `is_group_account` removed -- a shape
    # this module does not recognize, not the report's own total row (which
    # is identified by *both* the missing key *and* `account == "'Total'"`).
    # Dropped silently before fix round 2; now reported by name rather than
    # vanishing.
    mangled = dict(_report("trial-balance-report.json")[0])
    del mangled["is_group_account"]
    theirs = [row for row in _report("trial-balance-report.json") if row.get("acc_number") != "1111"] + [mangled]
    assert (
        "1111 - Operating Checking - VT: unrecognised row shape in ERPNext's trial balance "
        "(no is_group_account)" in trial_balance_mismatches(_tb(), theirs, INDEX)
    )


def test_an_account_not_in_chart_coa_is_reported_when_it_carries_a_nonzero_figure():
    # A copy of 1111's real (nonzero) row, renamed to an account chart.coa
    # does not describe -- chart drift, not a total or a group subtotal.
    ghost = dict(
        _report("trial-balance-report.json")[0],
        account="9999 - Ghost Account - VT",
        account_name="9999 - Ghost Account",
        acc_number="9999",
    )
    theirs = [*_report("trial-balance-report.json"), ghost]
    assert trial_balance_mismatches(_tb(), theirs, INDEX) == [
        "9999 - Ghost Account - VT: in ERPNext's trial balance but not in chart.coa "
        "(opening 4580.00, period debit 850.00, period credit 0.00, closing 5430.00)"
    ]


def test_an_account_not_in_chart_coa_with_all_zero_figures_is_not_reported():
    # An all-zero row has nothing to disagree about, and the export's own
    # live-chart check already refuses chart drift elsewhere.
    ghost = dict(
        _report("trial-balance-report.json")[0],
        account="9999 - Ghost Account - VT",
        account_name="9999 - Ghost Account",
        acc_number="9999",
        opening_debit=0.0,
        opening_credit=0.0,
        debit=0.0,
        credit=0.0,
        closing_debit=0.0,
        closing_credit=0.0,
    )
    theirs = [*_report("trial-balance-report.json"), ghost]
    assert not any("Ghost Account" in m for m in trial_balance_mismatches(_tb(), theirs, INDEX))


def test_our_open_receivables_match_erpnexts_own_summary():
    assert open_item_mismatches(_receivables(), _report("ar-summary-report.json"), "Receivable") == []


def test_our_open_payables_match_erpnexts_own_summary():
    assert open_item_mismatches(_payables(), _report("ap-summary-report.json"), "Payable") == []


def test_a_changed_party_outstanding_is_named():
    theirs = [
        dict(row, outstanding=row["outstanding"] + 1) if row["party"] == "Cloud Co" else row
        for row in _report("ap-summary-report.json")
    ]
    assert open_item_mismatches(_payables(), theirs, "Payable") == ["Cloud Co: ours 400.00, ERPNext 401.00"]


def test_read_trial_balance_report_keeps_the_dict_shaped_total_row_but_drops_malformed_entries():
    # The Trial Balance report's own grand-total row is a dict like any
    # account row (see the module docstring above and
    # tests/fixtures/period/README.md) -- `read_trial_balance_report` cannot
    # and does not drop it; only a row that is not even a dict is dropped
    # here, defensively, since `trial_balance_mismatches` is what actually
    # recognizes and skips the total row.
    committed = _report("trial-balance-report.json")  # 6 accounts + the real total row, exactly as captured
    caller = FakeCaller({"Trial Balance": {"result": [*committed, ["not", "a", "dict", "row"]]}})
    fy = _fiscal_year()
    rows = read_trial_balance_report(caller, "Transpire", fy, Q3)
    assert rows == committed
    dotted_path, params = caller.calls[0]
    assert dotted_path == "frappe.desk.query_report.run"
    assert params["filters"] == {
        "company": "Transpire",
        "fiscal_year": fy.name,
        "from_date": "2026-07-01",
        "to_date": "2026-09-30",
        "show_net_values": 1,
        "show_unclosed_fy_pl_balances": 0,
        "with_period_closing_entry_for_opening": 0,
        "with_period_closing_entry_for_current_period": 0,
        "include_default_book_entries": 1,
    }


def test_read_party_summary_drops_the_raw_total_row_and_passes_the_documented_filters():
    caller = FakeCaller({
        "Accounts Payable Summary": {"result": [*_report("ap-summary-report.json"), _RAW_PARTY_TOTAL_ROW]}
    })
    rows = read_party_summary(caller, "Transpire", Q3.end, "Payable")
    assert [r["party"] for r in rows] == ["Cloud Co"]
    dotted_path, params = caller.calls[0]
    assert dotted_path == "frappe.desk.query_report.run"
    assert params["report_name"] == "Accounts Payable Summary"
    assert params["filters"] == {
        "company": "Transpire",
        "report_date": "2026-09-30",
        "ageing_based_on": "Due Date",
        "range": "30, 60, 90, 120",
    }


def test_bind_crosscheck_reads_all_three_reports_and_reports_no_mismatches_on_the_real_capture():
    caller = FakeCaller({
        "Trial Balance": {"result": _report("trial-balance-report.json")},
        "Accounts Receivable Summary": {"result": _report("ar-summary-report.json")},
        "Accounts Payable Summary": {"result": _report("ap-summary-report.json")},
    })
    crosscheck = bind_crosscheck(caller, "Transpire", INDEX)
    package = Package(files={}, bank=(), split_vouchers=())
    mismatches = crosscheck(package, _tb(), _receivables(), _payables(), Q3, _fiscal_year())
    assert mismatches == []
    assert {c[1]["report_name"] for c in caller.calls} == {
        "Trial Balance",
        "Accounts Receivable Summary",
        "Accounts Payable Summary",
    }


def test_bind_crosscheck_surfaces_a_mismatch_from_any_one_report():
    theirs_ar = [
        dict(row, outstanding=row["outstanding"] + 1) if row["party"] == "Müller, Inc." else row
        for row in _report("ar-summary-report.json")
    ]
    caller = FakeCaller({
        "Trial Balance": {"result": _report("trial-balance-report.json")},
        "Accounts Receivable Summary": {"result": theirs_ar},
        "Accounts Payable Summary": {"result": _report("ap-summary-report.json")},
    })
    crosscheck = bind_crosscheck(caller, "Transpire", INDEX)
    package = Package(files={}, bank=(), split_vouchers=())
    mismatches = crosscheck(package, _tb(), _receivables(), _payables(), Q3, _fiscal_year())
    assert mismatches == ["Müller, Inc.: ours 750.00, ERPNext 751.00"]
