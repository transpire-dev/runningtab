# Connect Mercury to your books

Use this guide to connect your Mercury accounts to ERPNext, install the required fields and Bank Accounts, set up GL codes and categories, and start importing transactions. Settled transactions become submitted Bank Transactions for you to reconcile in the Banking workspace. For how updates and posting decisions work, see [How the ingest decides](../explanation/how-the-ingest-decides.md).

## Before you start

You need:

- Your books directory, with `books.toml`, `chart.coa`, and `mercury.toml`.
- Administrator access to the ERPNext site for installing fields and Bank Accounts.
- A production read-only Mercury token in the Secret Manager secret named by `secrets.mercury` in `books.toml`.
- Access to Mercury’s dashboard to upload GL codes, configure rules, and create and revoke tokens.

See [Targets and credentials](../reference/targets-and-credentials.md) for credential setup.

Practise against the Mercury sandbox first. Create its token inside the sandbox dashboard and supply it through `MERCURY_SANDBOX_TOKEN`. For commands requiring both flags, use `--env sandbox --target test`. The combination `--env sandbox --target books` is refused. The commands below connect production Mercury to your books.

## 1. Map every Mercury account

Edit `mercury.toml` beside `chart.coa`. For each Mercury account, add:

- An entry in `[accounts]` naming the chart account that holds its balance.
- An entry in `[bank_accounts]` naming its ERPNext Bank Account.

Use one Bank Account per Mercury account, including checking, savings, and the card. An account without a mapping causes a refusal. See [Books directory](../reference/books-directory.md) for the file format.

Remove obsolete `[categories]` or `[posting]` tables if present; files containing either are refused.

## 2. Install fields and Bank Accounts

Run both commands with administrator credentials:

```sh
runningtab mercury install-fields --target books
runningtab mercury install-bank-accounts --target books
```

The books bot lacks permission to perform these installations. The commands create the custom fields, the Bank, and the mapped Bank Accounts. Each Bank Account links to its mapped chart account. Re-running checks and repairs the installation.

## 3. Export and upload GL codes

Write the CSV:

```sh
runningtab coa export-gl-codes --output gl-codes.csv
```

Upload `gl-codes.csv` manually in Mercury’s dashboard. It contains the GL-code labels used to name the custom categories in the next step.

## 4. Plan and apply categories

Inspect category drift using the read-only token:

```sh
runningtab mercury categories plan --env production
```

The plan describes missing categories, incorrect visibility flags, and unmanaged categories. It writes nothing and exits with status 1 when drift exists.

To preview removal of unused unmanaged categories, use:

```sh
runningtab mercury categories plan --env production --prune
```

To apply changes, mint a full **Read and Write** token in Mercury’s dashboard and allowlist your IP. This token can also send money. Keep it only for this sitting; never store it in Secret Manager or a file.

In bash or zsh, enter the token without echoing it:

```sh
read -rs MERCURY_ADMIN_TOKEN
export MERCURY_ADMIN_TOKEN
```

In fish, use:

```fish
read -s -x MERCURY_ADMIN_TOKEN
```

Run the plan again, then apply:

```sh
runningtab mercury categories plan --env production
runningtab mercury categories apply --env production
```

If you reviewed the pruning plan and want those deletions, use `--prune` on both commands.

Remove the token from bash or zsh:

```sh
unset MERCURY_ADMIN_TOKEN
```

In fish:

```fish
set -e MERCURY_ADMIN_TOKEN
```

Then revoke the token in Mercury’s dashboard, including if the apply failed. See [Access model](../explanation/access-model.md) for the credential boundaries.

## 5. Configure category-to-GL-code rules

In Mercury’s dashboard, create one rule per category that maps it to the GL code with the same name.

## 6. Take the first snapshot

Preview what would post:

```sh
runningtab mercury snapshot --env production --target books --dry-run
```

Establish the initial state:

```sh
runningtab mercury snapshot --env production --target books
```

The snapshot reads transaction lists and records the corresponding position in the event log. If the site has multiple companies, specify `--company` with your company name.

## 7. Run regular syncs and check progress

Run this command by hand or on your schedule:

```sh
runningtab mercury sync --env production --target books
```

Check the cursor, its age, and the number of events seen:

```sh
runningtab mercury status --env production --target books
```

If you choose a custom state file with `--state`, use that path consistently for snapshot, sync, and status. Continue with [Daily reconciliation](daily-reconciliation.md).

## If something goes wrong

- **The cursor is too old:** Mercury retains events for 90 days. Sync refuses a gap beyond that retention period. Run the snapshot command again to recover.
- **Two categories share a desired name:** The command refuses and names both UUIDs. Resolve the duplicate in Mercury's dashboard, then run plan again.
- **Pruning leaves categories in use:** Review the reported transaction counts. Re-code those transactions in Mercury and run again, or keep the categories. The exit status remains 1 while they remain.
- **A category deletion is refused:** The command reports the refusal and continues with the remaining deletions.
- **The state file belongs to another environment or ledger:** Use the state file matching your `--env` and `--target`; status refuses a mismatched pair.