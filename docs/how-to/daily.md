# Daily operations

Run this routine after completing [setup](set-up.mdNone). Have access to Mercury for coding transactions and to ERPNext for reviewing, submitting, matching, creating, and deleting vouchers. Run sync as often as you want the ledger current.

## Sync Mercury

```sh
runningtab mercury sync --env production --target books
```

If the site has multiple companies, add `--company "Your Company"`.

Sync reads events since its saved cursor and checks settled transactions for later changes to notes and GL codes. It posts submitted Bank Transactions and proposes draft Journal Entries where it can. Keep the report open during reconciliation.

Sync exits `1` whenever it wants a person, even when posting work completed. Read these lines:

- `drafted`: Proposed Journal Entries to review and submit.
- `uncoded`: Settled, non-transfer rows without a GL code, with their age in days.
- `delete`: Obsolete draft proposals, each linked for review and deletion.
- `ATTENTION`: Rows requiring a decision, with the reason.
- `closed`: Rows dated in a closed quarter; they receive no draft voucher and need the quarter reopened. See [Close a quarter](close-a-quarter.mdNone).
- `balance`: Each account’s summed settled transactions compared with Mercury’s `currentBalance`; investigate differences.

Use `runningtab <command> --help` for command options.

## Reconcile in ERPNext

Open the Banking workspace, then the Bank Reconciliation Tool.

Review each proposed Journal Entry’s accounts, amount, and remark. Proposals can come from a Mercury GL code, a payee’s submitted voucher history, or a transfer between your accounts. Submit accepted drafts; reviewed drafts can be submitted in bulk.

Submitted vouchers become available as matches. Match each Bank Transaction to its voucher. For an internal transfer, match both Bank Transactions to the single transfer voucher.

For rows without a voucher, use the tool’s create-voucher dialog and complete reconciliation. Mercury revenue codes do not produce coded drafts: a customer payment clears a Sales Invoice through a Payment Entry.

Open every draft under `delete`. Its Bank Transaction has been reconciled to something else. Check for human edits before deleting the obsolete proposal. Resolve remaining `ATTENTION` items using their reported reasons.

## Code uncoded rows

Add the appropriate GL code in Mercury, then sync again:

```sh
runningtab mercury sync --env production --target books
```

The next sync carries the hint to the Bank Transaction. Review new proposals and attention items.

You can also code during reconciliation. A reconciled Bank Transaction, or one whose voucher carries its Mercury transaction id, stops appearing as `uncoded`.

## Check health

```sh
runningtab health --target books
```

Health reads only and prints six sections:

- `balance invariant`: Checks account transaction totals against Mercury balances.
- `uncoded spend, aged`: Fails when uncoded, settled, non-transfer rows exceed 14 days.
- `suspense contents`: Reports the contents and age of suspense account `5299`.
- `webhook subscription health`: Currently unavailable because the check is not built.
- `event cursor age`: Fails when the last full sync exceeds seven days or state contains no cursor.
- `closed periods`: Checks close status and compares rebuilt quarter files with stored packages.

Because webhook health is unavailable, `--strict` always exits `1`.

The cursor check needs sync’s default state path: run health on the same machine, as the same user, with the same `XDG_STATE_HOME`, and run sync without `--state`. Before relying on scheduled health alerts, verify that the cursor section reports an age. Missing state produces `N/A`, and health can pass while sync has stopped.

## Forecast capital

Update `forecast.toml` in your [books directory](../reference/books-directory.mdNone), then review:

```sh
runningtab forecast plan --env production --target books
```

The plan writes nothing. It computes cash needs through the third full month ahead, including opening cash net of the card, dues, floor, total capital call, and each member’s share.

Record the reviewed forecast:

```sh
runningtab forecast apply --env production --target books
```

Apply recomputes the forecast and creates a new Cash Forecast document, preserving earlier forecasts.