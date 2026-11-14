# Schedule health checks

Run `runningtab health` on a schedule when you want an alert if your books fall out of date or a check cannot run. It reads only and runs as the reader on ERPNext. Run it where `health` sees the same state file `runningtab mercury sync` writes: the same machine, the same user, the same `XDG_STATE_HOME`, and sync using its default state file (no `--state`). `health` reads the cursor only from that default path, `$XDG_STATE_HOME/<name>-books/mercury-<env>-<target>.sqlite3` (or `~/.local/state/...` when `XDG_STATE_HOME` is unset). If it resolves to anywhere else, the `event cursor age` check finds nothing, reports `N/A`, and the run can pass while sync has stopped. Before relying on the exit code, run the scheduled command once by hand, as the scheduled user, and check that `event cursor age` reports an age rather than `N/A`. For background on the checks, see the [health reference](../reference/health.md); for why closed quarters are rebuilt, see [what a close guarantees](../explanation/what-a-close-guarantees.md).

## Before you start

Make sure the account running the scheduled command has:

- Access to `runningtab` and your books configuration.
- The ERPNext reader credential for your target.
- For `--target books`, access to the Mercury read-only token in the Secret Manager secret named by `secrets.mercury` in `books.toml`.
- Application Default Credentials for the record bucket, used by the `closed periods` section.
- Access to the ingest’s state file on the machine where `mercury sync` runs.

See [targets and credentials](../reference/targets-and-credentials.md) for credential setup and [connect Mercury](connect-mercury.md) for the bank connection.

For `--target test`, set `MERCURY_SANDBOX_TOKEN` to a token created inside the Mercury sandbox dashboard. Supply a sandbox account mapping through `--mercury-mapping`; the books directory’s `mercury.toml` names real Mercury accounts.

## Steps

1. **Run a check manually with the scheduled account’s environment.**

    For your real books:

    ```sh
    runningtab health --target books
    ```

    If your site has multiple companies, select yours explicitly:

    ```sh
    runningtab health --target books --company "Your Company"
    ```

    For a test ledger, replace the mapping path with your sandbox mapping:

    ```sh
    runningtab health --target test --mercury-mapping /path/to/sandbox-mercury.toml
    ```

    The report always prints six sections: `balance invariant`, `uncoded spend, aged`, `suspense contents`, `webhook subscription health`, `event cursor age`, and `closed periods`. Read each section before scheduling the command.

2. **Choose how unavailable checks affect alerts.**

    Use the default command to alert on `FAIL` or `ERROR`. Add `--strict` if an unavailable section should also trigger an alert:

    ```sh
    runningtab health --target books --strict
    ```

    | Report outcome | Default exit code | With `--strict` |
    | --- | --- | --- |
    | Every section passes | `0` | `0` |
    | Any section reports `FAIL` | `1` | `1` |
    | Any section reports `ERROR` | `1` | `1` |
    | A section reports `N/A`, with no failures or errors | `0` | `1` |

    Webhook subscription health is currently always unavailable, so every `--strict` run exits `1`. On a machine without ingest state, the cursor check is unavailable and names where it looked.

3. **Schedule the command and connect its exit code to an alert.**

    Choose cron, a systemd timer, or a CI schedule. This cron entry runs daily at 06:00:

    ```cron
    0 6 * * * runningtab --books /absolute/path/to/my-books health --target books
    ```

    cron does not read your shell's environment, so name the books directory with `--books` (or set `RUNNINGTAB_BOOKS` in the crontab), and make sure the scheduled environment can find `runningtab` and has the prerequisites above. Preserve the report for diagnosis and configure your alerting system to notify you when the command exits non-zero. The exit code is the alert signal; the cron entry supplies the schedule.

## If something goes wrong

An `ERROR` means an implemented check could not run. Its message names the credential to set or resource to create when one is missing. Resolve the reported access, connectivity, account, or response problem, then rerun the health command.

Uncoded rows fail after 14 days. Review them through [daily reconciliation](daily-reconciliation.md).

The cursor fails after seven days. Inspect the production ingest’s state with:

```sh
runningtab mercury status --env production --target books
```

For a sandbox ingest targeting the test ledger, use `--env sandbox --target test`. If you use a custom state file, supply its path with `--state`. Past Mercury’s 90-day retention window, the report tells you to re-snapshot.

For a closed-period failure, use the report to identify the affected quarter and follow [reopen and re-close](reopen-and-reclose.md). Rerun health after recovery and check its exit code.