# Mercury commands

The `runningtab mercury` commands install ERPNext bank metadata, ingest settled Mercury transactions, report ingest state, and manage Mercury custom categories. Ingest reads Mercury and submits Bank Transactions for reconciliation. Category apply is the only command that writes to Mercury. See [Connect Mercury](../how-to/connect-mercury.md) for setup and [How the ingest decides](../explanation/how-the-ingest-decides.md) for ingest behavior.

## `runningtab mercury install-fields`

```text
runningtab mercury install-fields --target <test|books> [--yes]
```

Creates the custom fields used by the ingest. Re-running compares existing field flags and repairs differences.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--target` | Yes | Selects the ERPNext site: `test` or `books`. |
| `--yes` | No | Skips the confirmation prompt. |
| `--help` | No | Shows help and exits. |

### Notes

Requires administrator credentials through `RUNNINGTAB_ADMIN_TOKEN`. The books bot has no Custom Field permission. See [Access](access.md).

On Bank Transaction, creates:

| Field | Properties and purpose |
| --- | --- |
| `mercury_transaction_id` | Unique; prevents a second Bank Transaction for the same Mercury transaction. |
| `mercury_resource_version` | Records the Mercury resource version. |
| `mercury_gl_code` | Writable reconciliation hint, carried verbatim as `<number> - <name>`. |
| `mercury_note` | Writable free-text reconciliation hint. |

All four fields allow updates after submission. Statement imports can map their `GL Code` and `Note` columns to the writable hint fields.

Also creates `mercury_transaction_id` on Journal Entry. This field permits duplicate values so ERPNext amendments can retain it; the ingest refuses a second live voucher for one transaction. It cannot be updated after submission. Journal Entry receives no `mercury_resource_version` field.

## `runningtab mercury install-bank-accounts`

```text
runningtab mercury install-bank-accounts --target <test|books> [--yes]
```

Creates the Bank named `Mercury` and one Bank Account per Mercury account in `mercury.toml`’s `[bank_accounts]` table. Each Bank Account links to the chart account specified by `[accounts]`. Re-running checks and repairs the links.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--target` | Yes | Selects the ERPNext site: `test` or `books`. |
| `--yes` | No | Skips the confirmation prompt. |
| `--help` | No | Shows help and exits. |

### Notes

Requires administrator credentials through `RUNNINGTAB_ADMIN_TOKEN`. The books bot has no Bank or Bank Account permission.

Bank Account names must follow ERPNext’s `<account name> - Mercury` convention. Invalid names are refused before anything is created.

## `runningtab mercury snapshot`

```text
runningtab mercury snapshot --env <sandbox|production> --target <test|books> [--company NAME] [--state PATH] [--dry-run]
```

Establishes initial state from Mercury transaction lists and records the corresponding position in the event log. Re-snapshotting also recovers from a cursor gap beyond Mercury’s 90-day event retention window.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--env` | Yes | Selects the Mercury deployment: `sandbox` or `production`. No default. |
| `--target` | Yes | Selects the ERPNext site: `test` or `books`. |
| `--company` | No | Sets the company name; defaults to the site’s only company. |
| `--state` | No | Sets the ingest state file; defaults to the XDG state path described below. |
| `--dry-run` | No | Reports what would post; writes nothing. |
| `--help` | No | Shows help and exits. |

### Exit codes

- `1`: The run needs a person’s attention.

### Notes

The combination of `--env sandbox` and `--target books` is refused. A state file belonging to a different environment and target pair is also refused.

Settled transactions become submitted Bank Transactions on their mapped Bank Accounts. Receipts are attached during the same pass. See the shared ingest notes below and [How the ingest decides](../explanation/how-the-ingest-decides.md).

## `runningtab mercury sync`

```text
runningtab mercury sync --env <sandbox|production> --target <test|books> [--company NAME] [--state PATH] [--dry-run]
```

Applies every event after the saved cursor, then refreshes Mercury’s GL code and note on every settled row read by the run. Events are deduplicated by event ID; event updates apply only when `resourceVersion` advances.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--env` | Yes | Selects the Mercury deployment: `sandbox` or `production`. No default. |
| `--target` | Yes | Selects the ERPNext site: `test` or `books`. |
| `--company` | No | Sets the company name; defaults to the site’s only company. |
| `--state` | No | Sets the ingest state file; defaults to the XDG state path described below. |
| `--dry-run` | No | Reports what would post; writes nothing. |
| `--help` | No | Shows help and exits. |

### Exit codes

- `1`: Anything needs a person’s attention: `ATTENTION`, uncoded rows, a settled row without a ledger row, a closed-quarter row, a draft to delete, or a balance mismatch.

### Notes

Refuses a cursor older than the 90-day retention window. Recover with `runningtab mercury snapshot`.

The combination of `--env sandbox` and `--target books` is refused. A state file belonging to a different environment and target pair is also refused.

Report labels are `submitted`, `drafted`, `updated`, `adopted`, `attached`, `planned`, `skipped`, `failed`, `uncoded`, `balance`, `closed`, `delete`, `ATTENTION`, `events`, and `cursor`. `planned` reports dry-run work; `skipped` identifies unsettled rows. See [How the ingest decides](../explanation/how-the-ingest-decides.md) for interpretation and [Daily reconciliation](../how-to/daily-reconciliation.md) for follow-up.

The webhook receiver is not built yet.

## `runningtab mercury status`

```text
runningtab mercury status --env <sandbox|production> --target <test|books> [--state PATH]
```

Reports the saved cursor, its age, and how many events have been applied for the selected Mercury deployment and ERPNext target.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--env` | Yes | Selects the Mercury deployment: `sandbox` or `production`. |
| `--target` | Yes | Selects the ERPNext site: `test` or `books`. |
| `--state` | No | Sets the ingest state file; defaults to the XDG state path described below. |
| `--help` | No | Shows help and exits. |

### Exit codes

- `1`: There is no state file, the state holds no cursor, or the cursor is past Mercury's 90-day retention window.

### Notes

Refuses a state file written for a different environment and target pair.

## `runningtab mercury categories plan`

```text
runningtab mercury categories plan --env <sandbox|production> [--prune]
```

Reports differences between Mercury custom categories and the chart’s codeable accounts. Uses the read-only token and writes nothing.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--env` | Yes | Selects the Mercury deployment: `sandbox` or `production`. |
| `--prune` | No | Previews deletion of unmanaged categories that no transaction carries. Reads every transaction on every account to check usage. |
| `--help` | No | Shows help and exits. |

### Exit codes

- `1`: Any category drift remains, including unmanaged categories still in use during a prune check, or the plan is refused (for example, two categories share a desired name).

### Notes

The plan can serve as a standing check or a checklist for manual changes. With `--prune`, it shows the deletion list before apply.

Category names, visibility rules, and matching behavior are described below. See [Connect Mercury](../how-to/connect-mercury.md) for setup.

## `runningtab mercury categories apply`

```text
runningtab mercury categories apply --env <sandbox|production> [--yes] [--prune]
```

Creates missing chart categories and corrects visibility flags on existing categories. Matches by name and never renames categories.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--env` | Yes | Selects the Mercury deployment: `sandbox` or `production`. |
| `--yes` | No | Skips the confirmation prompt. |
| `--prune` | No | Deletes unmanaged categories only when no transaction on any account carries them. Checks unsettled transactions too. |
| `--help` | No | Shows help and exits. |

### Exit codes

- `1`: The token variable is unset, the plan is refused, a write Mercury refuses or that fails, or unmanaged categories remain in use during pruning.

### Notes

Reads its token exclusively from `MERCURY_ADMIN_TOKEN` for production or `MERCURY_SANDBOX_TOKEN` for sandbox. An unset variable is refused by name before any request.

Mercury requires a full Read and Write token for category writes; that token can also send money. Keep it only in the environment for the session, then revoke it. It must never be stored in a file or Secret Manager. Mercury requires an IP allowlist for write-scoped tokens, downgrades an unused write permission after 45 days, and deletes an unused token after 45 days. See [Connect Mercury](../how-to/connect-mercury.md) and [Access model](../explanation/access-model.md).

Without `--prune`, nothing is deleted. Pruning checks category UUIDs against an exhaustive transaction read that refuses short reads. Categories in use are retained and reported as `in use  <n> transaction(s)  <name>`. Re-code those transactions in Mercury to make a category eligible for deletion.

Deletes run after creates and edits, one request per category. A refused deletion produces a report line; remaining deletions proceed.

## Shared ingest behavior

### Configuration and credentials

`mercury.toml` sits beside `chart.coa`. Its `[accounts]` table maps each Mercury account to the chart account holding its balance; `[bank_accounts]` maps it to its ERPNext Bank Account. Missing account mappings are refused. Files containing legacy `[categories]` or `[posting]` tables are refused.

Read-only Mercury access uses `MERCURY_SANDBOX_TOKEN` for sandbox and the Secret Manager secret named by `books.toml`’s `secrets.mercury` for production. See [Targets and credentials](targets-and-credentials.md) and [Books directory](books-directory.md).

### State files

The default state path is:

```text
$XDG_STATE_HOME/<name>-books/mercury-<env>-<target>.sqlite3
```

`<name>` is the lowercased name from `books.toml`. Each file belongs to one environment and target pair, which is also recorded inside the file.

### Bank Transactions

The posted `reference_number` is Mercury’s ACH `trackingNumber`, falling back to the Mercury transaction ID when no trace number exists.

The posted `description` joins the available Mercury note, GL code, and bank text, in that order, with ` · `. The reference and description are fixed at posting. The custom GL-code and note fields remain the live copies and refresh on later runs. They provide reconciliation hints and route nothing.

Internal transfers produce two Bank Transactions on two accounts; the reconciliation tool matches them to one transfer voucher. Transactions with absent or unmapped categories still become Bank Transactions with the available hints.

Runs report each account’s settled transaction total against `currentBalance` and identify aged, settled non-transfer transactions without a Mercury GL code.

### Receipts

Each run compares Mercury’s attachment list with the Bank Transaction’s files. Missing receipts are streamed from presigned URLs, capped at 20 MB, and attached privately. The URLs last twelve hours.

Attachment identity is the UUID from the URL path, stored in the file name as `<uuid>-<file name>`. Existing files are never deleted or replaced. Files without a UUID prefix are left alone. Attachment-list changes emit no event, so the list is checked on every run.

## Category matching rules

Category names exactly match the GL-code labels emitted by `runningtab coa export-gl-codes`. Mercury dashboard rules can map each category to the GL code of the same name. See [Chart commands](coa.md).

| Chart accounts | Category visibility |
| --- | --- |
| Postable `5xxx` accounts | All three visibility flags. |
| Postable `31xx` member-capital and `4xxx` revenue accounts | `visibleForOther` only. |
| Accounts tagged `no-bank-line` | No category; GL codes remain available for ERPNext reconciliation. |

A missing desired name is a create; incorrect visibility flags require an edit. Categories absent from the desired chart names are reported as `unmanaged`. Duplicate categories sharing a desired name are refused with both UUIDs reported.

Renaming a chart account creates a new category and leaves the old category unmanaged. Mercury’s shipped default categories are also eligible for pruning when no transaction carries them.