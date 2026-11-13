# `runningtab health`

Checks whether your books are current. For background, see [how runningtab works](../explanation/how-it-works.md) and the [access model](../explanation/access-model.md). For recurring checks, see [scheduled health checks](../how-to/scheduled-health.md).

## `runningtab health`

```text
runningtab health --target <test|books> [--company NAME] [--strict] [--mercury-mapping PATH]
```

Prints six health sections covering bank balances, uncoded spend, suspense, webhook subscriptions, the event cursor, and closed periods. Reads only. ERPNext checks run as the reader, whose credential the server refuses every write for.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--target` | Yes | Selects `test` or `books`. |
| `--company` | No | Company name. Defaults to the site's only company. |
| `--strict` | No | Also fails when a check is unavailable. |
| `--mercury-mapping` | No | Path to a `mercury.toml`-shaped mapping file. Defaults to the books directory's `mercury.toml`. The closed-periods rebuild for `--target test` needs a mapping containing Mercury sandbox account IDs. |
| `--help` | No | Shows help and exits. |

### Exit codes

| Outcome | Default | With `--strict` |
| --- | --- | --- |
| Every section passes | `0` | `0` |
| A section is out of bounds (`FAIL`) | `1` | `1` |
| A section cannot run (`ERROR`) | `1` | `1` |
| A section is unavailable (`N/A`) | `0` | `1` |

`ERROR` includes an unset or refused credential, an unreachable site, a missing account, or an unreadable response. Each such message names what to set or create.

`N/A` applies only to webhook subscription health and to event cursor age on a machine without ingest state. While the webhook check remains a placeholder, `--strict` exits `1`.

### Sections

All six sections are always printed in this order:

| Section | What it checks |
| --- | --- |
| `balance invariant` | Runs. For each account, transactions whose status is neither `pending` nor `failed` must sum to the bank's `currentBalance`. |
| `uncoded spend, aged` | Runs using the same Mercury read as the balance invariant. Fails when any uncoded, settled, non-transfer row is older than 14 days. Otherwise passes with a count. |
| `suspense contents` | Runs. Reports what sits in the chart's suspense account, `5299`, and how long it has been there. |
| `webhook subscription health` | Unavailable: no subscription is registered yet. |
| `event cursor age` | Runs when this machine has the ingest state file. Fails when the last full sync is more than 7 days old or the state holds no cursor. Beyond Mercury's 90-day retention, says to re-snapshot. Without a state file, reports `N/A` and names the path checked. |
| `closed periods` | Runs the `runningtab period status` report. Checks for reopened quarters, locked quarters without stored packages, unfinished re-closes, `role_allowed_for_frozen_entries` left set, and rebuilt files that differ from stored packages. |

### Balance invariant notes

Reads transactions per account because that response carries the `total` needed to detect short reads. Pages are joined on transaction ID; a server ignoring the offset produces a reported short read. The check also reads the credit endpoint because the account list omits credit accounts.

Measured balance behavior shows that `sent` counts and `pending` and `failed` do not. The unobserved statuses `cancelled`, `reversed`, and `blocked` count toward the sum, matching ingest behavior. The check also fails by name whenever one appears.

### Suspense notes

Ages suspense from the last time the account was empty and lists only entries behind its current balance. It reads submitted ledger documents.

The ingest posts Bank Transactions, which reach the general ledger when a person reconciles them. An entry against `5299` is a submitted voucher used by a person to reconcile a statement line whose classification needs review. See [daily reconciliation](../how-to/daily-reconciliation.md).

### Closed-period notes

Uses the same reader credential and effective-grant checks as `runningtab period export`. Rebuilds every closed quarter through that export path and compares the files byte for byte with the stored package. A passing section means the handed-off quarters still reconcile with the current ledger.

See the [period command reference](period.md), [what a close guarantees](../explanation/what-a-close-guarantees.md), and [closing a quarter](../how-to/close-a-quarter.md).

### Credentials

| Target | Bank source | Bank credential |
| --- | --- | --- |
| `test` | Mercury sandbox | `MERCURY_SANDBOX_TOKEN`, created inside the sandbox dashboard. Production tokens do not work there. |
| `books` | Real Mercury organisation | Read-only token in the Secret Manager secret named by `secrets.mercury` in `books.toml`. |

ERPNext checks use the reader credential. See [targets and credentials](targets-and-credentials.md) and [connecting Mercury](../how-to/connect-mercury.md).