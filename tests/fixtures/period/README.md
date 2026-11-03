# Period ledger fixtures

`gl.json` and `fiscal-years.json` are a **real capture** from the disposable
ERPNext test site, taken by
`tests/capture_period_fixtures.py` (`just capture-period-fixtures`).
Nothing here is hand-written: every row is what ERPNext itself produced for the
nine Journal Entries in that script's `SCENARIOS` table, captured 2026-09-22.

One edit since: account names were relabelled to match `examples/books/chart.coa`
(the site's chart named them differently). Numbers, amounts, dates, parties and
voucher names are as captured.

## What was seeded

Two customers (`Müller, Inc.`, `"Best" Bikes`), one supplier (`Cloud Co`),
Fiscal Years `2025` and `2026`, and nine submitted Journal Entries dated to put
activity in 2025 (a prior fiscal year), in 2026Q2 (before the quarter), and in
2026Q3 (the quarter the fixtures describe) -- the table is in
`capture_period_fixtures.py`'s docstring and the task-3 plan.

## Deviations from the plan, observed on this capture

- **Chart drift on the test site.** Before the scenarios could be inserted,
  `runningtab coa plan --target test` showed the site's chart had drifted from
  the current `chart.coa`: five accounts needed a label rename (same number, new
  name) -- including 5110, which scenario #3 and #8 post to -- and one new
  account (5221) was missing. `runningtab coa apply --target test --yes` was run
  first to bring the site's chart in sync (renames and one create only; n/a
  prune, nothing deleted) so `document_name()` resolution against the _current_
  chart.coa would find the right accounts. This is expected disposable-site
  maintenance, not a fixture-capture concern, and is recorded here only because
  it was a precondition for the capture to succeed.

- **The shared test site is under concurrent, ongoing use.** ERPNext refuses any
  Account rename/renumber for five minutes after the _most recent_ GL Entry
  write anywhere on the site (`account.py`'s `validate_recent_gl_entries`, a
  whole-table check, not scoped to the account being touched). Applying the
  chart required several retries over a number of minutes because other GL
  activity on the shared site kept resetting that window. Not something Task 3's
  capture caused or can avoid; noted so a future capture rerun is not surprised
  by it.

- **`user_remark` marks GL rows on the _line_, not the header.** ERPNext's
  Journal Entry -> GL Entry composer builds `GL Entry.remarks` from each account
  row's own `user_remark` joined with the Journal Entry's auto-generated
  `remark` (built from cheque/reference info) -- it never reads the Journal
  Entry's header-level `user_remark` field. Setting `user_remark` only at the
  header (as the plan's script draft did) would have produced GL rows whose
  `remarks` never contain `period-fixture`, so the capture's
  `["remarks", "like", "%period-fixture%"]` filter would have matched nothing.
  The script sets `user_remark: "period-fixture"` on **every account line** as
  well as the header (the header copy is still what
  `["user_remark", "=", "period-fixture"]` checks for the already-ran-once
  refusal, since that field does exist and is queryable on the Journal Entry
  doctype itself). Confirmed against this capture: every captured GL row's
  `remarks` field is exactly `period-fixture`.

- **`due_date` lands on every line of a Journal Entry, not just the party
  line.** ERPNext's GL composer copies the Journal Entry's _header_ `due_date`
  onto every GL row it produces for that entry, not just the Receivable/Payable
  line. So JE #4, #5, and #8's `due_date` appears on both the debit and the
  credit row, not only the party row as the plan's table implied. `parse_gl_row`
  and the tests take this as-is: it is what ERPNext produces.

- **`is_opening`** arrives as the string `"Yes"`/`"No"` as expected;
  `parse_gl_row` treats anything else as `"No"`.

- **The refusal check, and the GL capture filter, are scoped to submitted,
  non-cancelled rows.** The first capture attempt on this site aborted partway
  through (the chart-drift issue above -- scenario #3 posts to 5110, whose live
  name did not yet match `chart.coa`), after two Journal Entries had already
  been inserted and submitted. Cancelling those two to retry left them on the
  site permanently: **cancelling a Journal Entry does not delete it or its GL
  Entries** -- ERPNext keeps both, marks them `is_cancelled`, and
  `client.delete()` then refuses ("LinkExistsError") because the (cancelled) GL
  Entries still reference it. So the already-ran-once check is
  `["user_remark", "=", "period-fixture"], ["docstatus", "=", 1]` (a cancelled
  leftover, docstatus 2, does not block a fresh run), and the GL capture filter
  adds `["is_cancelled", "=", 0]` (a cancelled leftover's rows never reach
  `gl.json`) -- the same filter `ledger.read_gl` uses for a real read, so the
  fixtures and production reads agree on what "live" means. Two cancelled
  Journal Entries (`ACC-JV-2026-00001`, `ACC-JV-2026-00002`, both dated 2025,
  both `is_opening`/prior-year scenarios re-created cleanly as
  `ACC-JV-2026-00003`/`00004` in the successful run) remain on the test site as
  inert leftovers; nothing in this repo reads them.

## The files

| File                        | What it is                                                                                                                                                                                                                                 |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `gl.json`                   | `{"abbr": <company abbreviation>, "rows": [<GL Entry field dict>, ...]}` -- every non-cancelled GL Entry whose `remarks` contains `period-fixture`, for the site's sole company                                                            |
| `fiscal-years.json`         | Every Fiscal Year on the site (`name`, `year_start_date`, `year_end_date`), not filtered                                                                                                                                                   |
| `trial-balance-report.json` | ERPNext's own Trial Balance report for 2026Q3, one row per postable account with activity, **plus the report's own grand-total row as its last entry** (see below -- it looks like an account row and is not dropped by the read function) |
| `ar-summary-report.json`    | ERPNext's own Accounts Receivable Summary report as of 2026-09-30, one row per customer with an outstanding balance                                                                                                                        |
| `ap-summary-report.json`    | ERPNext's own Accounts Payable Summary report as of 2026-09-30, one row per supplier with an outstanding balance                                                                                                                           |

Captured 2026-09-22 against the disposable test site, company
abbreviation `VT`: 18 GL Entries from the nine submitted Journal Entries
(`ACC-JV-2026-00003` through `ACC-JV-2026-00011`), and the two Fiscal Years
(`2025`, `2026`) the capture script created.

## Re-running the capture

`tests/capture_period_fixtures.py` refuses if any **submitted** Journal Entry
already carries the `period-fixture` remark, so a second run against the same
site would need those nine entries cancelled first (docstatus 1 -> 2 is enough;
deleting them is not required and, per the note above, may not be possible
without also deleting their GL Entries). It is meant to be run once per
test-site lifetime, or after a fresh disposable site is provisioned.

## The three report fixtures (task 6, `runningtab.period.crosscheck`)

Captured 2026-09-23 against the same site, with
`tests/capture_period_fixtures.py --reports`
(`just capture-period-fixtures --reports` is not wired up as a
separate recipe; run the script directly). Unlike the GL/Fiscal-Year capture
above, this mode is **read-only** against the site -- it calls
`frappe.desk.query_report.run` for the three reports and writes what came back,
minus what each read function itself drops (see below) -- and it can be re-run
at any time without needing anything cancelled first.

The filters in the task-6 brief worked exactly as written for the Trial Balance
report (`company`, `fiscal_year`, `from_date`, `to_date`, `show_net_values: 1`,
`show_unclosed_fy_pl_balances: 0`, `with_period_closing_entry_for_opening: 0`,
`with_period_closing_entry_for_current_period: 0`,
`include_default_book_entries: 1`) and for the AR/AP Summary reports (`company`,
`report_date`, `ageing_based_on: "Due Date"`, `range: "30, 60, 90, 120"`) -- no
filter needed adjusting from what the brief specified. The one surprise was the
**shape of `result`, not the filters**, and it is _not_ the same shape across
the three reports:

- **AR/AP Summary**: `result` is not uniformly a list of field dicts. Every real
  party row is a dict, but the report's own grand-total row is a bare list
  matching the `columns` order, confirmed on a direct probe of the raw response
  (`["Total", "", 350.0, 2500.0, 600.0, "", 1550.0, ...]` for AR) --
  `row.get("party")` on that row raises
  `AttributeError: 'list' object has no attribute 'get'`. `read_party_summary`
  filters with `isinstance(row, dict)` before touching `.get`, which drops it;
  `ar-summary-report.json` and `ap-summary-report.json` hold only real party
  rows (2 and 1 respectively), no total row.
- **Trial Balance**: `result`'s grand-total row is, perhaps confusingly, **also
  a dict** -- `{"account": "'Total'", "account_name": "'Total'", ...}`, quoted
  by ERPNext itself, with no `is_group_account` key (every real account row
  carries one, `0` or `1`) and a `warn_if_negative` key no real account row
  carries. `read_trial_balance_report` only drops a row that is not a dict, or a
  dict with no `account` at all -- this row has both, so it is **not** dropped
  there and is present, as the seventh and last row, in the committed
  `trial-balance-report.json`. `trial_balance_mismatches` is what recognizes and
  skips it, by the one thing that actually sets it apart: the missing
  `is_group_account` key.

**Our arithmetic matches ERPNext's own reports exactly, account by account and
party by party**, on this capture: `trial_balance_mismatches` and
`open_item_mismatches` (`runningtab.period.crosscheck`) both return an empty
list against these three files and the trial balance/open items
`test_period_balances.py` and `test_period_open_items.py` already compute from
`gl.json`. Confirmed specifically: 1111 (checking) opens 4,580.00 and closes
5,430.00 on both sides; 4110/5110 (revenue/expense) opening/period/closing all
agree; AR outstanding is 750.00 for Müller, Inc. and 800.00 for `"Best" Bikes`,
matching the open-receivables total for each; AP outstanding is 400.00 for Cloud
Co, matching the open payable, confirming ERPNext's Payable `outstanding` is
**positive** for money we owe -- the same sign
`runningtab.period.open_items.open_items` already gives `OpenItem.amount`, so
`open_item_mismatches` needs no sign flip for either kind.
