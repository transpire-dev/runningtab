# Reconcile daily bank activity

Use this routine to bring settled Mercury transactions into ERPNext, review proposed vouchers, and reconcile your bank activity. You will run the sync, work through its report in ERPNext’s Banking workspace, and finish with a health check. For the reasons behind each report line, see [How the ingest decides](../explanation/how-the-ingest-decides.md).

## Before you start

- Complete [Connect Mercury](connect-mercury.md) for the production Mercury deployment and your `books` target.
- Have the credentials required for those connections available. See [Targets and credentials](../reference/targets-and-credentials.md).
- Have access to Mercury to code transactions and to ERPNext to review, submit, match, create, and delete vouchers.
- Know which company you are reconciling. If the site has multiple companies, supply `--company` with the company name when running sync.

Run sync as often as you want the ledger current, by hand or on a schedule.

## Steps

1. **Sync settled bank activity.**

   ```sh
   runningtab mercury sync --env production --target books
   ```

   Sync processes events since its saved cursor and checks settled transactions for later changes to Mercury notes and GL codes. It posts settled transactions as submitted Bank Transactions and proposes draft Journal Entries where it can.

   Keep the report available while you reconcile. Anything requiring your attention makes sync exit with status `1`.

2. **Read the report.**

   Review the activity reported as `submitted`, `updated`, and `adopted`. Check `attached` for receipts and the Bank Transactions they landed on. Review the balance check, which compares each account’s summed settled transactions with Mercury’s `currentBalance`.

   Find the proposed vouchers under `drafted`, aged rows under `uncoded`, drafts under `delete`, and any `ATTENTION` lines. Use [How the ingest decides](../explanation/how-the-ingest-decides.md) to interpret each line.

3. **Review and submit proposed vouchers in ERPNext.**

   Open the Banking workspace, then the Bank Reconciliation Tool. For each Bank Transaction with a proposed Journal Entry, review the accounts, amount, and remark.

   Proposals may come from a Mercury GL code, a payee’s submitted voucher history, or a transfer between your own accounts. Submit the proposals you accept; you can submit them in bulk.

   Draft Journal Entries become available as matches after submission. Match each Bank Transaction to its submitted voucher. For an internal transfer, match both Bank Transactions to the single transfer voucher.

4. **Create vouchers for rows without a proposal.**

   Use the create-voucher dialog in the reconciliation tool for Bank Transactions that need a voucher. Complete the reconciliation for each row.

   Revenue codes at Mercury do not produce coded draft vouchers. A customer payment clears a Sales Invoice through a Payment Entry.

5. **Code remaining uncoded rows.**

   Review the aged `uncoded` rows and add the appropriate GL code in Mercury so the hint reaches the Bank Transaction on the next sync.

   You can also code a row by hand during reconciliation. A row whose Bank Transaction is reconciled, or whose voucher already carries its Mercury transaction id, stops appearing as `uncoded`.

   After making changes in Mercury, sync again:

   ```sh
   runningtab mercury sync --env production --target books
   ```

   Review any new proposals or attention items.

6. **Review obsolete drafts and remaining attention items.**

   Open each draft linked under `delete`. Its Bank Transaction has been reconciled to something else. Review the draft for human edits, then delete the obsolete proposal in ERPNext.

   Work through the remaining `ATTENTION` lines using the recovery paths below.

7. **Run the ledger health check.**

   ```sh
   runningtab health --target books
   ```

   Review the results using the [Health reference](../reference/health.md).

## If something goes wrong

- **Sync exits with status `1`.** Read the report for the items requiring a person. The exit status can accompany completed posting work.
- **A settled transaction has no ledger row.** Re-snapshotting is the repair. The sweep reports missing rows and never inserts them. Run:

  ```sh
  runningtab mercury snapshot --env production --target books
  ```

  This re-establishes state from the transaction lists. See the [Mercury reference](../reference/mercury.md).

- **A submitted row disagrees on amount, date, description, or account.** Sync refuses the change and names the disagreement. The person’s submitted row stands; sync never changes those fields. You decide whether to correct it in ERPNext.
- **A GL code cannot be placed in the chart.** The list uploaded to Mercury has drifted from `chart.coa`. Re-run:

  ```sh
  runningtab coa export-gl-codes --output gl-codes.csv
  ```

  Upload `gl-codes.csv` in Mercury’s dashboard, then run this command to see category drift:

  ```sh
  runningtab mercury categories plan --env production
  ```

  See [Connect Mercury](connect-mercury.md).

- **A transfer half has no unique match.** Investigate the reported halves; sync leaves unmatched or ambiguous transfers for you to resolve.
- **The state file belongs to another environment or target.** Use the state file for the requested pair, or omit `--state` to use its default path. Inspect the production/books cursor with:

  ```sh
  runningtab mercury status --env production --target books
  ```