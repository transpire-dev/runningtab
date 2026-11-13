# Period commands

The `runningtab period` commands export quarterly books, close quarters, and check stored packages against the ledger and bank. See [what a close guarantees](../explanation/what-a-close-guarantees.md) for background.

## `runningtab period export`

```text
runningtab period export --target <test|books> --quarter <YYYYQn> [--company NAME] [--output DIR] [--mercury-mapping PATH]
```

Builds a draft package for one quarter using read-only access to ERPNext and Mercury. Reads GL Entries, Fiscal Years, vouchers and Bank Transactions dated in the quarter, their attached receipts and invoices, and each Mercury account’s balance and transactions. Writes a new local directory.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--target` | Yes | Selects `test` or `books`. |
| `--quarter` | Yes | Selects the quarter as `YYYYQn`, for example `2026Q3`. |
| `--company` | No | Selects the company by name. Defaults to the site’s only company. |
| `--output` | No | Names the directory to create. Defaults to `./<quarter>-DRAFT`. |
| `--mercury-mapping` | No | Loads a `mercury.toml`-shaped mapping from this path. Defaults to the books directory’s `mercury.toml`, which names only real Mercury accounts. The `test` target uses the Mercury sandbox and needs its own account mapping. |
| `--help` | No | Shows help and exits. |

### Exit codes

| Code | Outcome |
| --- | --- |
| `0` | Every Mercury account reconciles with the ledger at the quarter’s end. |
| `1` | The bank and ledger disagree at the quarter’s end; the draft is written. |
| `1` | The package cannot be built; nothing is written. |
| `2` | The quarter is not in `YYYYQn` format. |

### Notes

An existing output directory is refused. The command never overwrites or merges a package.

The package contains `MANIFEST.md`, `SHA256SUMS`, the trial balance, general ledger, profit and loss statement, balance sheet, open receivables and payables, bank reconciliation, a QuickBooks Online import kit under `qbo/`, and attachments under `documents/`.

Build failures include accounts missing from `chart.coa`, credential problems, and unreachable sites.

The command uses the reader credential. The reader needs access to parties, fiscal years, and every voucher doctype used to authorize private attachment downloads. Existing readers missing these grants need re-provisioning for each target with `RUNNINGTAB_ADMIN_TOKEN` set. See [access commands](access.md) and [targets and credentials](targets-and-credentials.md).

See [close a quarter](../how-to/close-a-quarter.md) for the workflow.

## `runningtab period close`

```text
runningtab period close --target <test|books> --quarter <YYYYQn> [--company NAME] [--mercury-mapping PATH]
```

Runs the close ceremony: reads Books Closed Through and the ledger, cross-checks the draft against ERPNext’s Trial Balance and AR/AP reports account by account and party by party, moves Books Closed Through to the quarter’s last day, rebuilds the package and requires it to be byte-identical to the pre-lock build, then uploads it to the record bucket and reads every object back to verify it.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--target` | Yes | Selects `test` or `books`. |
| `--quarter` | Yes | Selects the quarter as `YYYYQn`, for example `2026Q3`. |
| `--company` | No | Selects the company by name. Defaults to the site’s only company. |
| `--mercury-mapping` | No | Loads a `mercury.toml`-shaped mapping from this path. Defaults to the books directory’s `mercury.toml`, which names only real Mercury accounts. The `test` target uses the Mercury sandbox and needs its own account mapping. |
| `--help` | No | Shows help and exits. |

### Notes

The command runs as an administrator. The `books` target requires `RUNNINGTAB_ADMIN_TOKEN`, your own System Manager `key:secret`. If it is unset, the command refuses before any request. Stored credentials cannot close a quarter. The `test` target’s environment credential is already an administrator’s. Every ERPNext read, cross-check, and lock uses the administrator credential; each package build opens fresh connections.

Record-bucket access requires Application Default Credentials, configured with `gcloud auth application-default login`, for an identity holding object-create and object-viewer on the record buckets.

Packages are stored under `<company-abbr>/<quarter>/` in the bucket named by `record_buckets.books` or `record_buckets.test` in `books.toml`. Re-closing a reopened quarter stores a new revision under `revN/` beside the original. The bucket has a retention policy; packages are never deleted or overwritten.

A failure before the lock leaves nothing changed. A failure after the lock leaves it in place, prints Books Closed Through, and names the next step. Check `runningtab period status` before recovery: an error can arrive after a package has been stored.

- On a first close with no stored package, rerun the close. It checks readiness and the locked ledger again, then stores the package.
- On a re-close with an existing stored package, another close is refused as already stored. Check status, reopen the quarter, close it again, then re-close every later stored quarter in order.

If the ledger changes between the check and the lock, the refusal names differing files and vouchers with changed general-ledger rows. If the posting belongs in the quarter, rerun the close, reopening first for a re-close. Otherwise, reopen the quarter and reverse the posting first.

See [close a quarter](../how-to/close-a-quarter.md) and [reopen and re-close](../how-to/reopen-and-reclose.md).

## `runningtab period status`

```text
runningtab period status --target <test|books> [--company NAME] [--mercury-mapping PATH]
```

Reads Books Closed Through and checks every closed quarter’s latest stored revision. Rebuilds each package through the same read-and-build path as `runningtab period export` and compares its bytes against the stored `SHA256SUMS`. Prints Books Closed Through, one line per closed quarter with `ok` or changed files, and every problem found. Writes nothing locally or remotely.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--target` | Yes | Selects `test` or `books`. |
| `--company` | No | Selects the company by name. Defaults to the site’s only company. |
| `--mercury-mapping` | No | Loads a `mercury.toml`-shaped mapping from this path. Defaults to the books directory’s `mercury.toml`, which names only real Mercury accounts. The `test` target uses the Mercury sandbox and needs its own account mapping. |
| `--help` | No | Shows help and exits. |

### Exit codes

| Code | Outcome |
| --- | --- |
| `0` | Every check is clean. |
| `1` | Any check reports a problem. |

### Notes

The command uses the reader credential and the same effective-grant proof as `runningtab period export`. Record-bucket access requires Application Default Credentials, configured with `gcloud auth application-default login`, for an identity holding object-create and object-viewer on the record buckets.

Checks report:

- Changed or missing package files.
- A reopened quarter, naming who moved Books Closed Through backward and when, from Company `Version` history.
- A quarter locked without a stored package, where Books Closed Through is past every stored quarter.
- An unfinished re-close, where a newer Company `Version` sets Books Closed Through to a stored quarter’s end than the version recorded in its stored `MANIFEST.md`. This can be detected even when all package files match.
- `role_allowed_for_frozen_entries` left set.

Reopening is a manual ERPNext UI action: move Books Closed Through to the previous quarter’s end. There is no `runningtab period reopen` command. Any stored quarter can be reopened. Re-close it, then every later stored quarter in order. Until that sequence is complete, status fails and names the required order.

The `closed periods` section of [health](health.md) runs this same report.

Late Mercury activity dated in a closed quarter changes its rebuilt `bank-reconciliation.csv`. `runningtab mercury sync` names those rows and drafts no vouchers for them. Reopen the affected quarter, book the activity there, close it again, and re-close later stored quarters. See [late bank activity](../how-to/late-bank-activity.md) and [reopen and re-close](../how-to/reopen-and-reclose.md).