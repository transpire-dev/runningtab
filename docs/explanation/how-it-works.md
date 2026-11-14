# How runningtab works

runningtab connects a written chart of accounts, bank activity, and a ledger. You keep the books configuration in git, test changes against a disposable ledger, and explicitly select the real ledger when working on the company’s books. Machines prepare drafts; humans decide what gets posted.

The recurring rhythm is to sync the bank, reconcile transactions, check health, and close each quarter into a retained record.

## The books directory

runningtab ships no books. You supply a books directory and keep it under version control so the chart and configuration have a history.

Its five files define the chart, bank-account connections, expected cash movements, QuickBooks Online export information, and connections to outside systems. The chart lives in `chart.coa`, an indented outline of account numbers, names, hierarchy, and accounting types. Chart changes begin there.

An annotated chart document explains each account’s purpose and how to choose between neighbouring accounts. Its account tables are generated from the chart. [Why a written chart matters](why-a-written-chart.md) explains the reasoning behind runningtab.

Every command requires an explicitly selected books directory through `--books DIR` or `RUNNINGTAB_BOOKS`. See the [books directory reference](../reference/books-directory.md) for its contents.

## Three systems and credentials

**ERPNext** holds the ledger. runningtab manages its chart, imports bank transactions, and prepares draft journal entries for human review.

**Mercury** holds bank activity. runningtab reads accounts and transactions; its only writes manage custom categories.

**Cloud Storage** holds closed-quarter records in a bucket under a retention policy.

Google Secret Manager supplies credentials. The books directory describes how these systems fit together. Follow the [setup guide](../how-to/set-up.md) to connect them.

## Two explicit targets

Selecting a books directory identifies the configuration. Selecting a target identifies the ledger.

Every command that touches the ledger requires `--target test` or `--target books`. There is no default target.

The test target uses a disposable ERPNext site whose host must be allowlisted in `books.toml`. That allowlist cannot include the real ledger’s configured host. The books target selects the real ledger and fetches its credential from Secret Manager.

## Machines draft, humans post

ERPNext’s server permissions prevent the bot from submitting Journal Entries or Purchase Invoices that commit money to the general ledger.

Its sole submission exception is a Bank Transaction. This records a bank statement line and posts nothing to the general ledger. Submitted bank transactions are required for reconciliation; a human supplies the accounting treatment.

## The working rhythm

Sync brings Mercury activity into ERPNext. Reconciliation connects bank transactions with accounting entries. runningtab can draft entries for coded bank rows and attach receipts uploaded at the bank.

Use the [daily guide](../how-to/daily.md) for syncing, reconciliation, and health checks, and the [quarterly close guide](../how-to/close-a-quarter.md) for retaining records. Command details are available through `runningtab <command> --help`.