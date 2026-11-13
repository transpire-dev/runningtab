# Handle bank activity after a quarter closes

Use this guide when Mercury receives bank activity dated in a quarter you have already closed. `runningtab mercury sync` prints a `closed` line naming the affected row, creates no draft voucher for it, and exits with status 1. `runningtab period status` shows that quarter’s rebuilt `bank-reconciliation.csv` differing from its stored package. You need to reopen the affected quarter, book the activity there, and close it again, then re-close every stored quarter after it. For the reason behind this sequence, see [What a close guarantees](../explanation/what-a-close-guarantees.md).

## Before you start

- Have the credentials needed to sync Mercury, reopen the quarter, book the activity, and close it again. `period status` uses the reader credential, which also serves `period export`. See [Targets and credentials](../reference/targets-and-credentials.md).
- Set `RUNNINGTAB_ADMIN_TOKEN` for the close and re-close commands.
- Identify the quarter containing the activity’s date and every stored quarter after it.
- Choose the Mercury environment and site target. The sync command requires both `--env` and `--target`.
- If your site has multiple companies, supply `--company` with the company name.
- For a status check against `--target test`, supply `--mercury-mapping` with a mapping for the Mercury sandbox account IDs. The default mapping names only real Mercury accounts.

The commands below use the production Mercury environment and the `books` target. Use the environment, target, and company appropriate to your books.

## Steps

1. **Identify the late activity.**

   Run the sync:

   ```sh
   runningtab mercury sync --env production --target books
   ```

   Read each `closed` line and identify the quarter containing the named row’s date. The command drafts no voucher for that row while its quarter is closed. Its exit status is 1.

2. **Check the stored quarter’s package.**

   ```sh
   runningtab period status --target books
   ```

   Find the affected quarter in the report. Its rebuilt `bank-reconciliation.csv` differs because the late activity changes that quarter’s reconciliation. The status command names changed or missing files and exits with status 1 unless all its checks are clean.

3. **Reopen the affected quarter.**

   In the ERPNext UI, move the Company’s Books Closed Through back to the previous quarter’s end. See [Reopen and re-close a quarter](reopen-and-reclose.md) for the procedure.

4. **Sync again and book the activity in that quarter.**

   ```sh
   runningtab mercury sync --env production --target books
   ```

   Once the quarter is open, the next sync drafts the row’s voucher. Review and submit the voucher in that quarter, then match it in the Bank Reconciliation Tool as usual. See [Daily reconciliation](daily-reconciliation.md).

5. **Close the affected quarter and re-close later stored quarters.**

   With `RUNNINGTAB_ADMIN_TOKEN` set, close the affected quarter again:

   ```sh
   runningtab period close --target books --quarter <that quarter>
   ```

   Replace `<that quarter>` with the affected quarter. Then run the same command for every stored quarter after it, in order, replacing `<that quarter>` with each quarter being re-closed. Follow [Reopen and re-close a quarter](reopen-and-reclose.md).

6. **Check the result.**

   ```sh
   runningtab period status --target books
   ```

   Confirm that the stored packages match their rebuilds and the lock checks are clean. A clean status check exits successfully.

## If something goes wrong

If sync still reports the row as `closed`, return to [Reopen and re-close a quarter](reopen-and-reclose.md) and check that you reopened the quarter containing that row’s date.

Booking the activity in the currently open quarter leaves the affected closed quarter’s stored package differing from its rebuild permanently. Keep the activity in its dated quarter.