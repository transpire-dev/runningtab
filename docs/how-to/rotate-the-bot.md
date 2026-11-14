# Rotate the bot or reader key

Use this guide when you need a fresh API key or an upgrade has changed the grants required by the bot or reader. Rotation means running `access provision` again: every run mints a fresh key and checks its permissions. For `books`, the command stores the key in Secret Manager only after every proof passes. For the reasons behind these permission boundaries, see the [access model](../explanation/access-model.md).

## Before you start

You need:

- The target you want to update: `books` or `test`.
- The identity you want to update: `bot` or `reader`. The default is `bot`.
- Administrator credentials. For `books`, set `RUNNINGTAB_ADMIN_TOKEN` to your own System Manager API token: the `key:secret` ERPNext shows for your user. The optional `token ` prefix is accepted. The stored bot credential cannot provision access.
- For the bot, the intended role and user names in your `books.toml` `[bot]` configuration.

For credential and target details, see [targets and credentials](../reference/targets-and-credentials.md).

Provisioning rotates the live secret as soon as it mints the key. A later check or storage failure can leave the stored credential stale. Keep your administrator credentials available until provisioning and verification succeed.

## Steps

1. **Set your administrator token for `books`.**

   Replace the placeholder with your own System Manager API token:

   ```sh
   export RUNNINGTAB_ADMIN_TOKEN='YOUR_KEY:YOUR_SECRET'
   ```

2. **Re-provision the identity whose key or grants need updating.**

   To rotate the bot on `books`:

   ```sh
   runningtab access provision --target books --profile bot
   ```

   To rotate the reader, or give it grants added by an upgrade:

   ```sh
   runningtab access provision --target books --profile reader
   ```

   Follow the confirmation prompt. Add `--yes` if you need to skip that prompt.

   Provisioning creates or repairs the role and user, reconciles permissions, and mints a fresh key. The bot checks include a refused Journal Entry submit attempt, effective grants, prohibited permissions, and a real write proving that the key cannot move either Company close-lock field. The reader checks establish that no write flag is allowed. The key is stored only after every check passes.

3. **Verify the stored `books` credential.**

   For the bot:

   ```sh
   runningtab access verify --target books --profile bot
   ```

   For the reader:

   ```sh
   runningtab access verify --target books --profile reader
   ```

   Verification rotates nothing and exits `0` only on a full pass, so you can use it as a gate. The company defaults to the site's only company. To name it explicitly:

   ```sh
   runningtab access verify --target books --profile bot --company 'YOUR COMPANY'
   ```

4. **Re-provision `test` when that target also needs updated grants.**

   Run the command for each identity you need to update:

   ```sh
   runningtab access provision --target test --profile bot
   ```

   ```sh
   runningtab access provision --target test --profile reader
   ```

   Provisioning probes the newly minted key. Standalone `access verify --target test` is refused because the test bot key is never persisted.

## If something goes wrong

If provisioning fails after minting a key, read its rotation warning. The live secret has already changed, and the stored secret may be stale. Address the reported failure, then re-run the same provisioning command. That run rotates the secret again and stores the replacement for `books` only after all checks pass.

If an upgrade leaves an identity missing grants, re-provision that identity. In particular, `period export` uses the reader and requires grants beyond those used by `coa plan` and `health`. Re-provision the reader on each target you use, then verify the stored `books` reader credential.

A failed verification means the credential has not passed the full policy check. Use the reported failure and the [access reference](../reference/access.md) to investigate before relying on that credential.