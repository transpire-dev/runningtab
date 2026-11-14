# Set up a site

Prepare a disposable ERPNext site with `--target test` first, then repeat on `--target books`. Each site needs a Company created by ERPNext’s setup wizard.

## Books and credentials

Copy [`examples/books/`](https://github.com/transpire-dev/runningtab/tree/main/examples/books) into your own [books directory](../reference/books-directory.mdNone). Configure `books.toml`, `chart.coa`, and `mercury.toml`, including the bot role and user. Set `RUNNINGTAB_BOOKS` to the directory’s path.

Allowlist the disposable site’s host in `site.test_hosts`; keep `site.host` for your real ledger. Map every Mercury account, including the card, in `[accounts]` and `[bank_accounts]`. Bank Account names must follow `<account name> - Mercury`.

| Use | Credential |
| --- | --- |
| Test ERPNext site | `RUNNINGTAB_TEST_URL`, `RUNNINGTAB_TEST_KEY`, and `RUNNINGTAB_TEST_SECRET`: site URL and administrator credentials |
| Books bot | Secret Manager secret named by `secrets.books` |
| Books reader | Secret Manager secret named by `secrets.books_readonly` |
| Books administrator | `RUNNINGTAB_ADMIN_TOKEN`: your System Manager `key:secret` |
| Production Mercury reads | Read-only token in the secret named by `secrets.mercury` |
| Mercury sandbox | `MERCURY_SANDBOX_TOKEN`, created in the sandbox dashboard |
| Production category changes | Temporary full Read and Write token in `MERCURY_ADMIN_TOKEN` |

Configure Google Application Default Credentials for Secret Manager:

```sh
gcloud auth application-default login
```

Every test command uses the test administrator credential. On books, chart planning uses the reader; application and pruning use the bot. Use `runningtab <command> --help` for command options.

## Apply the chart

Preview the changes:

```sh
runningtab coa plan --target test
```

The setup wizard’s stock chart can collide with yours. Review the plan before clearing it.

!!! warning
    `coa clear-stock` deletes accounts. Confirm the configured URL points to the disposable site you can wipe. It refuses once the company’s ledger contains any entry.

```sh
runningtab coa clear-stock --target test
runningtab coa plan --target test
runningtab coa apply --target test
runningtab coa prune --target test
```

Resolve remaining plan refusals before applying. Review each prompt. Pruning retires leftover accounts. Inspect the result in ERPNext’s **Chart of Accounts**.

## Install and provision

Install Cash Forecast before provisioning; access commands refuse when it is missing.

```sh
runningtab forecast install-doctype --target test
runningtab access provision --target test
runningtab access provision --target test --profile reader
runningtab mercury install-fields --target test
runningtab mercury install-bank-accounts --target test
```

Provisioning creates or repairs each identity, mints a key, and checks its effective permissions. Test keys are checked during provisioning; subsequent test commands continue using the administrator credential.

“Machines draft, humans post”: server roles prevent the bot from submitting Journal Entries and Purchase Invoices. Its sole submit exception, Bank Transaction, records a bank statement line and posts nothing to the general ledger. Human reconciliation supplies the accounting treatment.

Repeat setup with `--target books`, supplying `RUNNINGTAB_ADMIN_TOKEN` for administrator steps. Provision the reader and bot before planning and applying the books chart, since those commands use their stored credentials. Verify both:

```sh
runningtab access verify --target books
runningtab access verify --target books --profile reader
```

Standalone verification supports books only.

## Connect Mercury

Practise in Mercury’s sandbox first, using `--env sandbox --target test` where both flags are required. Sandbox with books is refused. The commands below connect production.

Export GL codes and upload the CSV in Mercury’s dashboard:

```sh
runningtab coa export-gl-codes --output gl-codes.csv
runningtab mercury categories plan --env production
```

Review the category plan. Mint a full **Read and Write** token, allowlist your IP, and keep it only for this sitting.

!!! danger "We think this is a bad idea, and we hate it"
    Mercury offers no token scope that can edit categories without also being able to send money. Its custom-token picker has "Fetch Categories" and nothing that writes them, so renaming one dropdown entry takes a full **Read and Write** token: the most dangerous credential the bank issues, for one of the least dangerous changes. We think that is a bad design and we hate it. runningtab works around it the only way it can: it never stores that token, reads it from your shell for one sitting, and expects you to revoke it as soon as `apply` finishes.

In bash or zsh:

```sh
read -rs MERCURY_ADMIN_TOKEN
export MERCURY_ADMIN_TOKEN
runningtab mercury categories plan --env production
runningtab mercury categories apply --env production
unset MERCURY_ADMIN_TOKEN
```

Revoke the token in Mercury’s dashboard even if applying failed. Never store it in Secret Manager or a file. Create one dashboard rule per category, mapping it to the GL code with the same name.

Preview and take the first snapshot:

```sh
runningtab mercury snapshot --env production --target books --dry-run
runningtab mercury snapshot --env production --target books
```

Continue with [daily reconciliation](daily.mdNone).

## Rotating a key

Re-run `access provision` for the identity, adding `--profile reader` for the reader. Each run rotates its live secret and stores the books replacement after checks pass. Failure can leave the stored secret stale; address the reported failure, re-provision, and verify.