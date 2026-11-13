# `runningtab coa`

The `coa` commands compare, apply, prune, and export your chart of accounts. For background, see [Why a written chart](../explanation/why-a-written-chart.md) and the [access model](../explanation/access-model.md). For setup, see [Set up a new site](../how-to/new-site.md).

## `runningtab coa plan`

```text
runningtab coa plan --target <test|books> [--company NAME] [--require-empty]
```

Show what applying the chart would change. Writes nothing and runs with the reader credential, whose writes the server refuses.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--target` | Yes | Select the site: `test` or `books`. |
| `--company` | No | Select the Company by name. Defaults to the site's only Company. |
| `--require-empty` | No | Exit non-zero unless the site already matches the chart. Use for CI after an apply. |
| `--help` | No | Show help and exit. |

### Exit codes

Exits non-zero if the plan carries a refusal. With `--require-empty`, also exits non-zero if the site does not match the chart.

### Notes

The server-enforced reader credential allows this command to run on a merge request, where the branch under review controls the code executed by the job.

## `runningtab coa apply`

```text
runningtab coa apply --target <test|books> [--company NAME] [--yes]
```

Apply the chart by creating, renumbering, retyping, and re-enabling accounts, then point the Company's default-account fields at the chart.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--target` | Yes | Select the site: `test` or `books`. |
| `--company` | No | Select the Company by name. Defaults to the site's only Company. |
| `--yes` | No | Skip the confirmation prompt. |
| `--help` | No | Show help and exit. |

### Notes

Confirmation gates the whole command because updating Company defaults reaches the ledger even when no accounts change. The command re-plans after the prompt and refuses if the plan changed while the prompt was open.

The command checks that the default-account fields exist on Company before touching any account. Run it before `runningtab coa prune`.

## `runningtab coa prune`

```text
runningtab coa prune --target <test|books> [--company NAME] [--yes] [--allow-unapplied]
```

Delete accounts the chart does not describe, or disable them where ERPNext refuses deletion. This is the only destructive command.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--target` | Yes | Select the site: `test` or `books`. |
| `--company` | No | Select the Company by name. Defaults to the site's only Company. |
| `--yes` | No | Skip the confirmation prompt. |
| `--allow-unapplied` | No | Prune before the chart is applied, for retiring an existing chart whose account numbers collide with yours. Does not bypass the blocking-refusal check. |
| `--help` | No | Show help and exit. |

### Notes

By default, pruning refuses until the chart is applied, because pruning first would strand Company defaults. It always refuses on `ROOT_NOT_UNIQUE` or `AMBIGUOUS_MATCH`, which make the set of extra accounts untrustworthy.

With `--allow-unapplied`, pruning clears Company defaults pointing at accounts it deletes and moves surviving accounts that still hold chart numbers to 9-prefixed numbers. ERPNext keeps some sample accounts alive by name until applying the chart repoints the defaults. Renumbering also requires confirmation.

After this teardown, run `runningtab coa apply`, then `runningtab coa prune` to retire the remaining accounts.

## `runningtab coa clear-stock`

```text
runningtab coa clear-stock --target <test|books> [--company NAME] [--yes]
```

Delete ERPNext's stock chart from a site whose ledger is empty, so `runningtab coa apply` can run. A fresh site's setup wizard creates ERPNext's "Standard" chart, and some of it collides with yours in ways `coa apply` refuses to resolve, such as an ungrouped Retained Earnings where the chart wants a group.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--target` | Yes | Select the site: `test` or `books`. |
| `--company` | No | Select the Company by name. Defaults to the site's only Company. |
| `--yes` | No | Skip the confirmation prompt. |
| `--help` | No | Show help and exit. |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Every listed account was deleted, or there was nothing to clear. |
| `1` | The company's ledger holds an entry, the prompt was declined, or ERPNext refused an account. |

### Notes

An account is part of the stock chart when it has no account number, is not a root, and carries a name from ERPNext's "Standard" template (copied into `runningtab.stock_chart` from ERPNext's source). Your chart's accounts always carry a number, so they are never candidates. Accounts are deleted children first; a stock group that holds any other account is kept and listed under `keep`.

Before deleting, the command points every Company default-account field away from the accounts it deletes, and probes the ledger again after the prompt. A refused delete is reported with ERPNext's reason, the run continues, and that account's parent groups are left standing.

On `--target books` it runs as the bot, which holds `delete` on Account.

## `runningtab coa export-gl-codes`

```text
runningtab coa export-gl-codes [--output PATH]
```

Emit a single-column CSV of non-group account names for the bank's GL-code picker. Output goes to stdout by default; upload the CSV manually in the bank's dashboard.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--output` | No | Write the CSV to the specified path instead of stdout. |
| `--help` | No | Show help and exit. |

### Notes

Every exported label is comma-free. Mercury's importer splits each line at the first comma regardless of CSV quoting. Export removes thousands separators and replaces other commas with a spaced slash.

ERPNext account names remain unchanged, so a bank GL-code label can differ from its corresponding account name.