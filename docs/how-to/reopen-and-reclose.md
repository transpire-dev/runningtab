# Reopen and re-close a quarter

Use this guide when you need to correct a closed quarter, including bank activity that reached Mercury after the close. You reopen the quarter in ERPNext, make the correction there, then close it and every later stored quarter in order. This example reopens `2026Q2` when `2026Q3` is also stored. For the guarantees behind this process, see [What a close guarantees](../explanation/what-a-close-guarantees.md).

## Before you start

- Have access to change the Company’s **Books Closed Through** field in the ERPNext UI.
- For `--target books`, set `RUNNINGTAB_ADMIN_TOKEN` to your own System Manager `key:secret`. Closing is refused before any request if it is unset. For `--target test`, the environment credential is already an administrator’s.
- Have the reader credential used by `period export` available for `period status`. See [Targets and credentials](../reference/targets-and-credentials.md).
- Authenticate Application Default Credentials with an identity holding object-create and object-viewer on the record buckets:

  ```sh
  gcloud auth application-default login
  ```

The commands below use `--target books`. Substitute your target and quarters. Add `--company "Your Company"` if the site has multiple companies; otherwise, the command defaults to its only company. For `--target test`, supply `--mercury-mapping PATH` with a mapping for the Mercury sandbox accounts. The default `mercury.toml` names only the real Mercury accounts.

## Steps

1. **Check the current lock and stored quarters.**

   ```sh
   runningtab period status --target books
   ```

   Read **Books Closed Through**, the per-quarter results, and every reported problem. Record which stored quarters follow the quarter you need to correct. You must re-close all of them in chronological order.

   This command reads only. It rebuilds each closed quarter’s latest revision and compares its package bytes with the stored checksums.

2. **Reopen the quarter in ERPNext.**

   In the ERPNext UI, change the Company’s **Books Closed Through** (`accounts_frozen_till_date`) to the previous quarter’s last day. To reopen `2026Q2`, set it to `2026-03-31`.

   Reopening requires a person to make this UI change. There is no `period reopen` command.

   Check the resulting state:

   ```sh
   runningtab period status --target books
   ```

   The report identifies the reopened quarter, including who changed the lock and when, and names the required re-close order. Until that sequence is complete, `period status` and the `closed periods` section of `health` fail.

3. **Make the correction in the reopened quarter.**

   Enter the correction in that quarter. For late Mercury activity, follow [Handle late bank activity](late-bank-activity.md): once the quarter is open, the next sync drafts the row’s voucher. Book it in its original quarter.

   Review the package differences:

   ```sh
   runningtab period status --target books
   ```

   The report names changed files. It continues to fail while the reopening and re-close sequence remains unfinished.

4. **Close the corrected quarter.**

   ```sh
   runningtab period close --target books --quarter 2026Q2
   ```

   The command checks readiness and ERPNext’s reports, advances the lock, requires the rebuilt package to match the pre-lock build byte for byte, and uploads and reads back the stored objects.

   A successful re-close stores a new revision under `<company-abbr>/2026Q2/revN/` in the target’s configured record bucket. Existing packages remain retained; nothing is deleted or overwritten.

5. **Close every later stored quarter in order.**

   For this example, close `2026Q3` next:

   ```sh
   runningtab period close --target books --quarter 2026Q3
   ```

   Repeat for each later stored quarter, earliest first. Each re-close creates a new revision.

6. **Verify the completed sequence.**

   ```sh
   runningtab period status --target books
   ```

   Confirm that **Books Closed Through** reaches the last quarter you re-closed, every quarter reports `ok`, and no problems remain. The command exits with status `1` unless all checks are clean.

## If something goes wrong

**A close fails before the lock changes.** Nothing has changed. Address the reported failure before trying the close again.

**A re-close fails after the lock changes.** The lock remains in place; there is no rollback. Read the reported **Books Closed Through** value and next step. Always check status first:

```sh
runningtab period status --target books
```

An error can arrive after the new revision landed. If recovery is still needed, reopen the affected quarter in ERPNext, close it again, then re-close every later stored quarter in order. Directly retrying a re-close is refused because the quarter already has a stored package. Status also detects an unfinished re-close even when all files still match the old revision.

**The ledger changes between the check and the lock.** The refusal names differing files and vouchers. If the posting belongs in that quarter, reopen it and close again. If it does not belong there, reopen the quarter and reverse it before closing. Complete the later quarters’ re-closes afterward.