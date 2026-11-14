# Set up a new ERPNext site

Use this guide when you have an ERPNext site and want to prepare it for runningtab. You will configure your books directory, install the required doctypes and credentials, apply your chart, and prepare the site for Mercury transactions. Complete the setup on `--target test` first, then repeat it on `--target books`. For the reasons behind the credential boundaries, see the [access model](../explanation/access-model.md).

## Before you start

Have these prerequisites ready:

- An ERPNext site for each target you will configure.
- Your books directory, starting from the example described in the [books directory reference](../reference/books-directory.md).
- For `--target test`, the administrator credential in `RUNNINGTAB_TEST_URL`, `RUNNINGTAB_TEST_KEY`, and `RUNNINGTAB_TEST_SECRET`.
- For administrator commands on `--target books`, your own System Manager API token in `RUNNINGTAB_ADMIN_TOKEN`. Use the `key:secret` ERPNext shows for your user; the `token ` prefix is optional.
- Mercury account mappings in `mercury.toml`: `[bank_accounts]` supplies Bank Account names, and `[accounts]` supplies their chart accounts.

The stored `books` bot credential cannot install fields, create Bank Accounts, or provision access. On `books`, chart planning uses the reader credential; chart application and pruning use the bot credential. Both credentials come from Secret Manager. See [targets and credentials](../reference/targets-and-credentials.md).

## Set up the site

1. **Write your books directory.**

   Start from the example and follow the [books directory reference](../reference/books-directory.md). Configure the bot role and user in `books.toml`, and check your Mercury mappings. Each Bank Account name must follow ERPNext’s `<account name> - Mercury` convention.

2. **Install the Cash Forecast doctypes as an administrator.**

   ```sh
   runningtab forecast install-doctype --target test
   ```

   This must precede access provisioning. Both `access provision` and `access verify` refuse without writing anything when Cash Forecast is missing. Re-running the installer repairs fields that have drifted.

3. **Provision the bot and reader as an administrator.**

   ```sh
   runningtab access provision --target test
   runningtab access provision --target test --profile reader
   ```

   Provisioning creates or repairs each identity and mints a fresh key. It checks effective permissions against the server. The bot checks include refused Journal Entry submission and proof that the Company close-lock fields cannot move. Reader checks require every write flag to be denied.

   For `books`, a key is stored in Secret Manager only after all checks pass. Every provisioning run rotates that identity’s live secret.

   On `test`, every command, including `coa apply` and the ingest, uses the administrator credential in `RUNNINGTAB_TEST_URL`, `RUNNINGTAB_TEST_KEY`, and `RUNNINGTAB_TEST_SECRET`. Access provisioning still creates the bot and reader and proves their grants, but runningtab does not store or use their keys for subsequent commands on `test`. No further credential setup is needed.

4. **Plan and apply your chart.**

   Follow [Your chart in a test ledger](../tutorial.md) for the complete chart replacement procedure. On `books`, planning uses the reader; applying and pruning use the bot.

   ```sh
   runningtab coa plan --target test
   ```

   On a fresh ERPNext site, the stock chart the setup wizard created collides with your chart. Review the plan, then follow the tutorial's sequence:

   ```sh
   runningtab coa clear-stock --target test
   runningtab coa apply --target test
   runningtab coa prune --target test
   ```

   `clear-stock` deletes ERPNext's stock chart and refuses while the company's ledger holds any entry, so run it before anything is posted. `prune` then retires whatever ERPNext would not let `clear-stock` delete. Review each prompt.

5. **Install the Mercury custom fields as an administrator.**

   ```sh
   runningtab mercury install-fields --target test
   ```

   This creates four Bank Transaction fields: `mercury_transaction_id`, `mercury_resource_version`, `mercury_gl_code`, and `mercury_note`. It also creates `mercury_transaction_id` on Journal Entry. Run this before the first ingest. Re-running compares and repairs existing field flags.

6. **Install the Mercury Bank Accounts as an administrator.**

   ```sh
   runningtab mercury install-bank-accounts --target test
   ```

   This creates the Bank named Mercury and the mapped Bank Accounts, linking each to its configured chart account. Re-running repairs links without duplicating accounts.

7. **Repeat for your real site, then verify the stored keys.**

   After completing the test setup, repeat the commands with `--target books`, using `RUNNINGTAB_ADMIN_TOKEN` for administrator steps. Then check both stored keys:

   ```sh
   runningtab access verify --target books
   runningtab access verify --target books --profile reader
   ```

   Verification exits `0` only when every permission proof passes. It refuses `--target test`, whose bot key is never stored; on the test site, `access provision` already ran the same proofs. On a site with several companies, add `--company NAME`.

8. **Connect Mercury.**

   Follow [Connect Mercury](connect-mercury.md) to configure the bank connection and establish the initial snapshot.

## If something goes wrong

If access provisioning or verification reports a missing Cash Forecast doctype, run `forecast install-doctype`, then retry.

If Bank Account installation rejects a name, correct its `[bank_accounts]` entry to follow `<account name> - Mercury`, then rerun.

If provisioning fails after minting a key, the live secret has already rotated. Read the failure message: it identifies when the stored credential is stale. Follow [Rotate the bot](rotate-the-bot.md) to recover access.

If `coa clear-stock` refuses because the ledger has entries, the site is not new and its accounts are history. `coa prune --allow-unapplied` can still retire the old chart's accounts that carry no entries (see the [coa reference](../reference/coa.md)), but runningtab never deletes or renumbers an account with entries. If such an account collides with your chart, `coa plan` names it under `REFUSED` and `coa apply` stays blocked until you resolve it yourself: change `chart.coa` so the two no longer collide, or move that history in ERPNext. runningtab has no command for either. If chart pruning reports `ROOT_NOT_UNIQUE` or `AMBIGUOUS_MATCH`, resolve the blocking refusal before retrying. The [chart tutorial](../tutorial.md) and [chart command reference](../reference/coa.md) cover chart setup.