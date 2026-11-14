# Close a quarter

Close a reconciled quarter to lock its ledger and store an accounting package for an accountant or auditor unfamiliar with your books. The package contains financial statements, the general ledger, open receivables and payables, bank reconciliation, a QuickBooks Online import kit, and supporting documents. `MANIFEST.md` explains its contents; `SHA256SUMS` records file checksums.

## Before you start

Have working ledger and Mercury credentials, including the reader credential for export and status. For closing `books`, have your own System Manager API `key:secret`.

Health, close, and status require Application Default Credentials with object-create and object-viewer access to the record bucket:

```sh
gcloud auth application-default login
```

Replace `YYYYQn` below with your quarter, such as `2026Q3`. If the site has multiple companies, add `--company "Company name"` to period commands. Use `runningtab <command> --help` for command options.

## What blocks a close

The close refuses before locking anything when:

- The quarter has not ended.
- An entry dated in the quarter sits in suspense (`5299`).
- A submitted Bank Transaction dated in the quarter is not fully reconciled.
- A draft Journal Entry or Purchase Invoice is dated in the quarter, including unsubmitted ingest drafts. Drafts cannot be submitted once their quarter is locked.
- A Mercury account and the ledger disagree at quarter end.
- Books Closed Through is misplaced: it must be at the previous quarter’s end once an earlier quarter is stored, or this quarter’s end when recovering a lock whose store failed.
- An earlier quarter was never stored while a later one is.
- `role_allowed_for_frozen_entries` is set on the Company.
- The quarter already has a complete stored package. Reopen it before closing again.

## Close the quarter

1. Complete [daily reconciliation](daily.md), then run health:

   ```sh
   runningtab health --target books
   ```

2. Export a draft:

   ```sh
   runningtab period export --target books --quarter YYYYQn
   ```

   Open `<quarter>-DRAFT/MANIFEST.md`. Review the statements, open receivables and payables, bank reconciliation, and supporting documents. A bank difference still produces a draft for investigation. A build failure writes nothing. Export refuses an existing destination.

3. Set your administrator credential:

   ```sh
   export RUNNINGTAB_ADMIN_TOKEN='YOUR_KEY:YOUR_SECRET'
   ```

   Replace the placeholder with your own System Manager `key:secret`. The `books` close refuses before any request if this variable is unset. Stored credentials cannot authorize it.

4. Close:

   ```sh
   runningtab period close --target books --quarter YYYYQn
   ```

5. Confirm:

   ```sh
   runningtab period status --target books
   ```

   Check that every quarter reports `ok` and no problems remain. Status rebuilds packages without writing anything and checks the lock.

## What success guarantees

Books Closed Through reaches the quarter’s end. The package has passed cross-checks against ERPNext’s Trial Balance and receivables/payables reports, matched byte for byte across the lock, and been uploaded and read back for verification.

Packages live under `<company-abbr>/<quarter>/` in the configured record bucket. Re-closes add `revN/` revisions. Stored packages are never deleted or overwritten.

## Recover after a failure

Before the lock, nothing has changed. Address the reported failure and retry.

After the lock, it stays in place; there is no rollback. Read the reported Books Closed Through value and next step. Always run `runningtab period status --target books` first: an error can arrive after the package landed.

For a first close with no stored package, rerun the close. It repeats readiness checks against the locked ledger. If recovery from a re-close is still needed, reopen the quarter and close it again, then re-close every later stored quarter in order.

## Reopen and re-close

Check status and identify every later stored quarter. A person must move the Company’s Books Closed Through back to the previous quarter’s end in ERPNext. There is no reopen command.

Correct the quarter, close it again, then re-close every later stored quarter chronologically. Status and health fail until the sequence is complete. Finish by checking status.

## Late bank activity

Every late Mercury row dated in a closed quarter changes its rebuilt bank reconciliation. Period status signals every such row. Sync’s `closed` line covers only rows it would draft vouchers for.

Reopen the affected quarter, sync again, and book the activity in its original quarter using [daily reconciliation](daily.md). Then re-close it and every later stored quarter in order, and check status.