"""Seed the scenarios the period builders are tested on, then capture the GL they produce.

Run once, against the disposable test site only, as its administrator:

    just capture-period-fixtures

Creates Fiscal Years 2025 and 2026 if missing, the two customers and one
supplier, and the nine Journal Entries in the plan's table, submitted; then
writes every fixture GL Entry for the company, and every Fiscal Year, to
`tests/fixtures/period/`. Refuses unless the site is in the books' test_hosts
(RUNNINGTAB_BOOKS names the books directory, as for the CLI).
Re-running on a site that already has the entries creates a second set, so
the fixtures would double -- it refuses if any Journal Entry carries the
`period-fixture` remark already.

`--reports` is a second, read-only mode: it writes nothing to the site,
reads ERPNext's own Trial Balance and Accounts Receivable/Payable Summary
reports for the quarter the GL fixtures were captured around (2026Q3), and
writes them to `trial-balance-report.json`, `ar-summary-report.json`, and
`ap-summary-report.json` -- the fixtures `test_period_crosscheck.py` checks
our own arithmetic against. Run it any time after the scenarios above have
been seeded once; it does not re-seed them and can be re-run freely.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sys
from typing import Any

from icedcoffee_sync.erpnext.accounts import company_abbr, document_name
from icedcoffee_sync.frappe.client import Client

from runningtab.books import resolve_books
from runningtab.chart import load_chart
from runningtab.coa import sole_company
from runningtab.period.crosscheck import read_party_summary, read_trial_balance_report
from runningtab.period.ledger import GL_FIELDS, read_fiscal_years
from runningtab.period.quarter import Quarter, fiscal_year_for
from runningtab.site import Target, admin_config, is_test_site

OUT = pathlib.Path(__file__).parent / "fixtures" / "period"
MARK = "period-fixture"
REVENUE = "4110"  # first postable 4xxx account in chart.coa; see the plan, Task 3 Step 1
REPORTS_QUARTER = "2026Q3"  # the quarter gl.json was captured around; see tests/fixtures/period/README.md

# (date, lines, extra) -- a line is (number, debit, credit, party_type, party, reference_index)
SCENARIOS: list[tuple[str, list[tuple[str, str, str, str | None, str | None, int | None]], dict[str, Any]]] = [
    (
        "2025-03-01",
        [("1111", "5000", "0", None, None, None), ("3110", "0", "5000", None, None, None)],
        {"is_opening": "Yes"},
    ),
    ("2025-11-15", [("5206", "300", "0", None, None, None), ("1111", "0", "300", None, None, None)], {}),
    ("2026-02-10", [("5110", "120", "0", None, None, None), ("1111", "0", "120", None, None, None)], {}),
    (
        "2026-05-20",
        [("1131", "900", "0", "Customer", "Müller, Inc.", None), (REVENUE, "0", "900", None, None, None)],
        {"due": "2026-06-19"},
    ),
    (
        "2026-07-10",
        [("1131", "1500", "0", "Customer", '"Best" Bikes', None), (REVENUE, "0", "1500", None, None, None)],
        {"due": "2026-08-09"},
    ),
    ("2026-08-01", [("1111", "600", "0", None, None, None), ("1131", "0", "600", "Customer", '"Best" Bikes', 4)], {}),
    (
        "2026-08-20",
        [("1111", "250", "0", None, None, None), ("1131", "0", "250", "Customer", "Müller, Inc.", None)],
        {},
    ),
    (
        "2026-09-05",
        [("5110", "400", "0", None, None, None), ("2111", "0", "400", "Supplier", "Cloud Co", None)],
        {"due": "2026-10-05"},
    ),
    (
        "2026-09-10",
        [
            ("1131", "100", "0", "Customer", "Müller, Inc.", None),
            ("1131", "0", "100", "Customer", '"Best" Bikes', None),
        ],
        {},
    ),
]


def _ensure(client: Client, doctype: str, name_field: str, doc: dict[str, Any]) -> None:
    if not client.list(doctype, filters=[[name_field, "=", doc[name_field]]], limit=1):
        client.insert(doctype, doc)


def _account_line(
    names: dict[str, str],
    created: list[str],
    number: str,
    debit: str,
    credit: str,
    party_type: str | None,
    party: str | None,
    ref: int | None,
) -> dict[str, Any]:
    # `user_remark` set on every line, not just the header: ERPNext's GL
    # composer builds GL Entry.remarks from each line's own user_remark
    # joined with the auto-generated header remark (built from
    # reference/cheque info) -- it never reads the header-level `user_remark`
    # field. Setting it per line is what actually gets `period-fixture` onto
    # the captured GL rows so the capture below can filter to just the
    # fixture vouchers.
    line: dict[str, Any] = {
        "account": names[number],
        "debit_in_account_currency": debit,
        "credit_in_account_currency": credit,
        "user_remark": MARK,
    }
    if party_type:
        line |= {"party_type": party_type, "party": party}
    if ref is not None:
        line |= {"reference_type": "Journal Entry", "reference_name": created[ref]}
    return line


def _submit_scenario(
    client: Client,
    company: str,
    names: dict[str, str],
    created: list[str],
    date: str,
    lines: list[tuple[str, str, str, str | None, str | None, int | None]],
    extra: dict[str, Any],
) -> str:
    accounts = [_account_line(names, created, *line) for line in lines]
    doc = {
        "voucher_type": "Journal Entry",
        "company": company,
        "posting_date": date,
        "user_remark": MARK,
        "accounts": accounts,
    }
    if extra.get("is_opening"):
        doc["is_opening"] = extra["is_opening"]
    if extra.get("due"):
        doc["due_date"] = extra["due"]
    draft = client.insert("Journal Entry", doc)
    client.submit(draft)
    return str(draft["name"])


def _capture_reports() -> int:
    """The read-only `--reports` mode: write the three ERPNext report fixtures. Writes nothing to the site."""
    books = resolve_books(None)
    config = admin_config(books.site, Target.TEST)
    if not is_test_site(books.site, config.url):
        print(f"refusing: {config.url} is not a test site", file=sys.stderr)
        return 1
    quarter = Quarter.parse(REPORTS_QUARTER)
    with Client(config) as client:
        company = sole_company(client)
        fiscal_year = fiscal_year_for(quarter, read_fiscal_years(client, company))
        tb_rows = read_trial_balance_report(client, company, fiscal_year, quarter)
        ar_rows = read_party_summary(client, company, quarter.end, "Receivable")
        ap_rows = read_party_summary(client, company, quarter.end, "Payable")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "trial-balance-report.json").write_text(json.dumps(tb_rows, indent=1, sort_keys=True) + "\n")
    (OUT / "ar-summary-report.json").write_text(json.dumps(ar_rows, indent=1, sort_keys=True) + "\n")
    (OUT / "ap-summary-report.json").write_text(json.dumps(ap_rows, indent=1, sort_keys=True) + "\n")
    print(
        f"captured {len(tb_rows)} trial balance rows, {len(ar_rows)} AR rows, {len(ap_rows)} AP rows "
        f"for {quarter.label} into {OUT}"
    )
    return 0


def main() -> int:
    if "--reports" in sys.argv[1:]:
        return _capture_reports()
    # admin_config(Target.TEST) does not set may_submit -- the test site's
    # System Manager credential can submit on the server, but the local
    # `SiteConfig` guard defaults to drafts-only. Turned on here because this
    # script runs as the disposable test site's administrator and nowhere
    # else: `is_test_site` below refuses every other host before anything is
    # sent.
    books = resolve_books(None)
    config = dataclasses.replace(admin_config(books.site, Target.TEST), may_submit=True)
    if not is_test_site(books.site, config.url):
        print(f"refusing: {config.url} is not a test site", file=sys.stderr)
        return 1
    with Client(config) as client:
        company = sole_company(client)
        abbr = company_abbr(client, company)
        # docstatus 1 only: a Journal Entry a previous, interrupted run
        # submitted and then had cancelled (to clean up after an aborted
        # attempt, e.g. mid-script account-not-found error) must not block a
        # fresh run forever. Cancelling a Journal Entry does not delete it or
        # its GL Entries -- ERPNext keeps both, marked is_cancelled -- so a
        # cancelled leftover is inert but permanent on this site.
        if client.list("Journal Entry", filters=[["user_remark", "=", MARK], ["docstatus", "=", 1]], limit=1):
            print("refusing: fixture entries already exist on this site", file=sys.stderr)
            return 1
        names = {a.number: document_name(a.name, abbr, a.number) for a in load_chart(books.chart_path)}
        for year in ("2025", "2026"):
            _ensure(
                client,
                "Fiscal Year",
                "year",
                {"year": year, "year_start_date": f"{year}-01-01", "year_end_date": f"{year}-12-31"},
            )
        for customer in ("Müller, Inc.", '"Best" Bikes'):
            _ensure(client, "Customer", "customer_name", {"customer_name": customer})
        _ensure(client, "Supplier", "supplier_name", {"supplier_name": "Cloud Co"})
        created: list[str] = []
        for date, lines, extra in SCENARIOS:
            created.append(_submit_scenario(client, company, names, created, date, lines, extra))
        gl = list(
            client.iter_all(
                "GL Entry",
                fields=GL_FIELDS,
                # is_cancelled excludes a cancelled leftover's own rows (see
                # the docstatus note above) -- ledger.read_gl filters the same
                # way, so the fixtures match what a real read would return.
                filters=[
                    ["company", "=", company],
                    ["remarks", "like", f"%{MARK}%"],
                    ["is_cancelled", "=", 0],
                ],
                order_by="posting_date asc",
            )
        )
        years = list(client.iter_all("Fiscal Year", fields=["name", "year_start_date", "year_end_date"]))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "gl.json").write_text(
        json.dumps({"abbr": abbr, "rows": gl}, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
    )
    (OUT / "fiscal-years.json").write_text(json.dumps(years, indent=1, sort_keys=True) + "\n")
    print(f"captured {len(gl)} GL entries from {len(created)} journal entries into {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
