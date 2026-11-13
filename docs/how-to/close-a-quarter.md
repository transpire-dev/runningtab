# Close a quarter

Use this guide when you have reconciled a quarter and are ready to lock it and store its accounting package. You will export a draft, review it, run the close, and confirm the result with `period status`. For the checks behind the close and the guarantees of the stored package, see [What a close guarantees](../explanation/what-a-close-guarantees.md).

## Before you start

- Have working ledger and Mercury credentials for your target. See [Targets and credentials](../reference/targets-and-credentials.md).
- For `--target books`, have your own System Manager API `key:secret` ready. Closing requires `RUNNINGTAB_ADMIN_TOKEN`; a stored credential cannot authorize the close.
- Set up Application Default Credentials for an identity with object-create and object-viewer access to the record bucket. `period close`, `period status`, and `health` require these credentials:

    ```sh
    gcloud auth application-default login
    ```

- Choose the quarter in `YYYYQn` format, such as `2026Q3`. Replace `YYYYQn` in the commands below with your quarter.
- If you need to select a company, add `--company "Company name"` to the `period` commands. Otherwise, they default to the site's only company.

Practise with `--target test` first. Its environment credential is already an administrator's, and its packages go to `record_buckets.test`. The Mercury sandbox needs its own account mapping: pass `--mercury-mapping PATH` to each `period` command. The default `mercury.toml` names only real Mercury accounts.

## Close the quarter

1. **Reconcile everything in the quarter and run `health`.**

    Follow [Daily reconciliation](daily-reconciliation.md), then run the health check.

    ```sh
    runningtab health --target books
    ```

    See the [Health reference](../reference/health.md) for the check.

2. **Export and review a draft.**

    ```sh
    runningtab period export --target books --quarter YYYYQn
    ```

    This reads the ledger and bank and creates `<quarter>-DRAFT/` locally. Open its `MANIFEST.md` for a description of the package. Review the financial statements, open receivables and payables, bank reconciliation, and supporting documents.

    Exit code `0` means every Mercury account reconciles with the ledger at quarter end. A bank difference returns `1` and still writes the draft for investigation. If the package cannot be built, the command returns `1` and writes nothing. An invalid quarter format returns `2`.

    An existing output directory is refused. To export another draft, choose a new directory with `--output`:

    ```sh
    runningtab period export --target books --quarter YYYYQn --output ./quarter-review-2
    ```

3. **Set your administrator credential.**

    Replace the placeholder with your own System Manager API `key:secret`:

    ```sh
    export RUNNINGTAB_ADMIN_TOKEN='YOUR_KEY:YOUR_SECRET'
    ```

    For `--target books`, the close command refuses to make any request if this variable is unset.

4. **Run the close.**

    ```sh
    runningtab period close --target books --quarter YYYYQn
    ```

    The command cross-checks the draft against ERPNext's Trial Balance and AR/AP reports, moves Books Closed Through to the quarter's last day, and rebuilds the package. It requires the rebuild to be byte-identical, then uploads the package and reads every object back to verify it.

    The package is stored under `<company-abbr>/<quarter>/` in the bucket named by `record_buckets.books` in `books.toml`. A reopened quarter's re-close adds a `revN/` revision. Stored packages are never deleted or overwritten.

5. **Confirm the result.**

    ```sh
    runningtab period status --target books
    ```

    Check that the stored packages match the ledger and the lock is intact. Status names changed or missing files and reports reopened quarters, a lock without a stored package, and `role_allowed_for_frozen_entries` left set. It exits `1` unless every check is clean.

## If something goes wrong

**Failure before the lock.** Nothing has changed. Address the reported failure before trying the close again.

**Failure after the lock on a first close.** The lock stays in place; there is no rollback. The command prints Books Closed Through and names the next step. Check status first:

```sh
runningtab period status --target books
```

An error can arrive after the package landed. If it is already stored, another close will be refused. If it has not landed, run `period close` again. It recognizes the lock at quarter end, repeats readiness and cross-checks against the locked ledger, and stores the package.

**Failure on a re-close.** Check status first: the new revision may already have landed. A re-run is refused when the quarter already has a stored package. If recovery is still needed, [reopen the quarter and close it again](reopen-and-reclose.md), then re-close every stored quarter after it in order.

**The ledger moved between the check and the lock.** The refusal names every differing file and the vouchers whose general-ledger rows differ. If the posting belongs in the quarter, run `close` again, reopening first on a re-close. If it does not belong, reopen the quarter and reverse it first.