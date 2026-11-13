# Run a capital forecast

Use this guide when you need to calculate how much each member should contribute and record the result as a Cash Forecast. You install the forecast doctypes once per site, edit `forecast.toml`, review a plan, and apply it. The forecast covers the period through the end of the third full month ahead. For background, see [How it works](../explanation/how-it-works.md).

## Before you start

You need:

- A books directory containing your chart and `forecast.toml`. See the [books directory reference](../reference/books-directory.md).
- Mercury access for the deployment you will read. See [Connect Mercury](connect-mercury.md).
- Administrator credentials to install the doctypes, reader credentials for `plan`, and bot credentials for `apply`. See [Targets and credentials](../reference/targets-and-credentials.md).
- Bot access granting `read` and `create` on Cash Forecast.

The commands below use Mercury’s `production` deployment and the `books` site. Both forecast commands require `--env` and `--target`. Supported values are `sandbox` or `production` for `--env`, and `test` or `books` for `--target`.

The combination `--env sandbox --target books` is refused before any request, with exit status `2`.

## Steps

1. **Install the Cash Forecast doctypes.**

   Install them on the test site first, then on the real one. On `--target test` the environment credential (`RUNNINGTAB_TEST_URL`, `RUNNINGTAB_TEST_KEY`, `RUNNINGTAB_TEST_SECRET`) is already an administrator's; on `--target books`, set `RUNNINGTAB_ADMIN_TOKEN` to your own System Manager `key:secret`.

   ```sh
   runningtab forecast install-doctype --target test
   runningtab forecast install-doctype --target books
   ```

   Confirm each prompt, or add `--yes` to skip it.

   The command installs four doctypes and prints one outcome per doctype: `created`, `updated`, or `unchanged`. You can rerun it to repair field-list drift; it never deletes a doctype.

2. **Provision the bot's access.**

   Run `access provision` after installing the doctypes. It grants the bot `read` and `create` on Cash Forecast. Use the same credentials as step 1:

   ```sh
   runningtab access provision --target test
   runningtab access provision --target books
   ```

   Every run mints a fresh key, so provisioning `books` rotates the bot's stored key; anything holding the old one, such as a scheduled sync, picks up the new one from Secret Manager on its next run. See [Rotate the bot key](rotate-the-bot.md).

   Both `access provision` and `access verify` refuse with one sentence and write nothing if the Cash Forecast doctype is missing. See the [access reference](../reference/access.md).

3. **Edit `forecast.toml`.**

   Open `forecast.toml`, a text file in your books directory beside the chart, in your editor and update the forecast inputs. Its fields are documented in the [books directory reference](../reference/books-directory.md). The file is validated against the chart when loaded. See the [forecast reference](../reference/forecast.md) for configuration details.

4. **Compute and review the plan.**

   Using reader credentials, run:

   ```sh
   runningtab forecast plan --env production --target books
   ```

   This reads Mercury balances and transactions, Bank Transactions, and `forecast.toml`. It writes nothing.

   Review the printed forecast window, opening cash, ledger-through date, monthly table, dues, floor, total call, and each member’s share. Opening cash sums every Mercury account’s balance, net of the card. The ledger-through date is the day before the oldest unreconciled Bank Transaction, or today.

   Each due is marked `owed` or `seen paid`. The floor identifies its source as a line name, `[call] floor`, or `no owed charges`. Check these details before recording the forecast. The run uses local time for `now`.

5. **Record the Cash Forecast.**

   Using bot credentials, run:

   ```sh
   runningtab forecast apply --env production --target books
   ```

   This computes the forecast again, prints it, and creates a new Cash Forecast document. It also reads Company and Cash Forecast records.

   After writing, the command prints the new document’s identifier and desk URL. Every successful run adds a document and preserves earlier forecasts. A successful command exits with status `0`.

## If something goes wrong

A refusal prints one sentence and exits with status `1`. Use that sentence to identify the failed prerequisite:

- **Cash Forecast is missing:** run `runningtab forecast install-doctype --target books` as described in step 1.
- **The bot cannot create Cash Forecast documents:** run `access provision` as described in step 2.
- **`forecast.toml` fails validation:** the one-sentence refusal names the cause.
- **Mercury returns no accounts, reports a balance that differs from the sum of transactions, or refuses an API request:** the forecast stops, and the one-sentence refusal names the cause.
- **A Bank Transaction has no readable date:** the forecast stops, and the one-sentence refusal names the cause.
- **The site is in maintenance mode:** the command refuses with `SessionStopped`; the one-sentence refusal names the cause.