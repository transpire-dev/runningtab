# How runningtab works

runningtab connects a written chart of accounts, your bank activity, and your ledger. You keep the books configuration in git, use a disposable ledger to test changes, and explicitly choose the real ledger when you work on your company’s books. Machines prepare drafts; humans decide what gets posted.

The recurring rhythm is to sync the bank, reconcile transactions, check health, and close each quarter into a retained record.

## The pieces

A books directory defines the books runningtab works with. Three outside systems hold the bank activity, ledger, and closed records:

- **Mercury** is the bank. runningtab reads its accounts and transactions. Its only writes to Mercury manage custom categories.
- **ERPNext** is the ledger. runningtab manages the chart there, brings in bank transactions, and prepares draft journal entries for human review.
- **Cloud Storage** holds closed quarters in a record bucket under a retention policy.

Google Secret Manager supplies credentials. An administrator’s own token for one-off admin commands comes from `RUNNINGTAB_ADMIN_TOKEN`.

The books directory tells runningtab how these fit together.

## Your books live in a directory kept in git

runningtab ships no books. You supply a books directory and keep its files under version control so that the chart and configuration have a history.

The directory contains five files:

| File | Purpose |
| --- | --- |
| `chart.coa` | Your chart of accounts and company default accounts |
| `mercury.toml` | Connections between Mercury accounts, chart accounts, and ERPNext Bank Accounts |
| `forecast.toml` | Expected cash movements used by forecasting |
| `qbo.toml` | Chart information needed for QuickBooks Online period exports |
| `books.toml` | The books’ name, site hosts, secret identifiers, record buckets, identities, and chart documentation path |

The chart is an indented outline in `chart.coa`. It defines account numbers, names, hierarchy, and accounting types. Changes to the chart begin in this versioned file. The annotated chart document explains what accounts are for and how to choose between neighbouring accounts; its account tables are generated from the chart.

See [why a written chart matters](why-a-written-chart.md) for the reasoning behind this arrangement.

Every command reads one explicitly selected books directory, supplied through `--books DIR` or `RUNNINGTAB_BOOKS`. There is no default. runningtab also validates `books.toml` strictly, rejecting missing and unknown keys.

The repository includes a complete [example books directory](https://github.com/transpire-dev/runningtab/tree/main/examples/books). The [books directory reference](../reference/books-directory.md) describes the configuration in detail.

## Two targets separate testing from the real ledger

Every command that touches the ledger requires an explicit target:

- `--target test` selects a disposable ERPNext site. Its host must appear in `books.toml` under `site.test_hosts`.
- `--target books` selects the real ledger. Its credential is fetched from Secret Manager when used and cannot be replaced by an exported environment variable.

There is no default target. Selecting the books directory identifies the configuration; selecting the target identifies the ledger that the command will reach.

The test host allowlist is checked explicitly, and it cannot include the real ledger’s configured host. See [targets and credentials](../reference/targets-and-credentials.md) for credential selection details.

## Machines draft, humans post

The bot can create drafts. ERPNext’s server refuses its attempts to submit documents that commit money to the general ledger.

The bot’s one permitted submission is a Bank Transaction, which posts nothing to the general ledger. This lets bank activity arrive for reconciliation while a human retains control over posting accounting entries.

The [access model](access-model.md) explains how that boundary is enforced.

## The daily and quarterly rhythm

Daily work starts with syncing Mercury activity into ERPNext. Reconciliation connects bank transactions with the accounting entries that explain them. runningtab can draft a journal entry for a coded bank row and attach the receipt a person uploaded at the bank.

Health checks accompany that work. They read Mercury through the same bank client and reconciliation logic used by ingestion, keeping the checks aligned with the import process. The [daily reconciliation guide](../how-to/daily-reconciliation.md) covers the working routine.

At quarter end, closing produces records stored in Cloud Storage under the bucket’s retention policy. Read [what a close guarantees](what-a-close-guarantees.md) for the meaning of those records and the [quarterly close guide](../how-to/close-a-quarter.md) for the procedure.