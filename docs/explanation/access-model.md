# Access model

“Machines draft, humans post” depends on the credential the machine holds. A caller can bypass `icedcoffee-sync`’s client-side guard. The server-side roles attached to the API key enforce the boundary: the bot can prepare accounting documents, but cannot submit the Journal Entries or Purchase Invoices that commit money.

runningtab separates the bot, reader, and administrator credentials. Each has a different job.

## The bot’s permissions

`runningtab access provision` creates or repairs a dedicated role and user, using the names in `books.toml`’s `[bot]` configuration. It reconciles the role to these grants:

| Document type | Permissions | Purpose |
| --- | --- | --- |
| `Account` | `read`, `write`, `create`, `delete` | Apply and prune the chart of accounts |
| `Company` | `read`, `write` | Set default-account links |
| `Company`, permission level 1 | `read` | Read the books’ closing boundary |
| `GL Entry` | `read` | Check whether accounts have ledger entries |
| `Journal Entry` | `read`, `write`, `create` | Draft accounting entries |
| `Purchase Invoice` | `read`, `write`, `create` | Draft accounts payable |
| `Supplier` | `read`, `write`, `create` | Maintain suppliers for drafting |
| `File` | `read`, `write`, `create` | Attach supporting files |
| `Bank Transaction` | `read`, `write`, `create`, `submit` | Ingest bank statement lines |
| `Cash Forecast` | `read`, `create` | Write a new forecast each run |

The bot receives no `cancel` or `amend` permission. A saved `Cash Forecast` cannot be changed or deleted by the bot.

## Why Bank Transaction submit is safe

`Bank Transaction` is the sole submit exception. Submitting one posts nothing to the general ledger. It records the bank’s statement line.

ERPNext writes the general-ledger entries when a person reconciles that line to a voucher in the Bank Reconciliation Tool. That tool requires submitted bank transactions, so the ingest submits the statement lines it creates. Human reconciliation supplies the accounting treatment.

The ingest alone uses `ingest_config`, the client configuration that permits submission. Other commands keep the drafts-only client, which rejects a stray `docstatus` before sending it. The server’s role restrictions remain the boundary if a caller bypasses that check.

## The reader credential

The reader role grants `read` access to `Account`, `Company`, `GL Entry`, `Fiscal Year`, `Customer`, `Journal Entry`, `Purchase Invoice`, `Payment Entry`, `Sales Invoice`, `Bank Transaction`, `File`, `Supplier`, and `Version`. It also grants `Company` read access at permission level 1 for the closing boundary.

This supports chart planning, period exports, and inspection of who changed the closing boundary. Reading `GL Entry` exposes the ledger; that access is deliberately accepted so planning can check whether an account has entries.

There is a known exception to the reader’s read-only scope: Frappe gives authenticated users inherited `File` permissions for `create`, `write`, and `delete`. Verification reports these every run. Unexpected mutation permissions fail verification.

Reader verification checks effective grants and prohibited mutation flags. It cannot use the bot’s draft-and-submit probe because the reader cannot create a Journal Entry.

## Administrator credentials

For provisioning on the `books` target, supply your own System Manager API token through `RUNNINGTAB_ADMIN_TOKEN`. ERPNext shows it as `key:secret`; the optional `token ` prefix is accepted. The stored books credential belongs to the bot and cannot provision access.

Mercury category changes require a separate administrator credential. Mercury’s custom-token scopes offer category reads; category writes require a full **Read and Write** token, which can send money. `apply` reads this from `MERCURY_ADMIN_TOKEN`, or `MERCURY_SANDBOX_TOKEN` for the sandbox, and refuses to proceed when it is missing. The token is never stored. Mint it for one sitting, allowlist your IP, and revoke it afterwards.

## Proof before storage

After minting a fresh bot key, provisioning runs three checks against the server:

1. Create a real Journal Entry draft, then prove that a real submit attempt is refused.
2. Check that prohibited flags are denied: `submit`, `cancel`, and `amend` on Journal Entry and Purchase Invoice; `cancel` and `amend` on Bank Transaction; and `write` and `delete` on Cash Forecast.
3. Check that every granted permission at permission level 0 is effective, including Bank Transaction `submit`.

Permission-level-1 grants are explicitly skipped by the doctype-level permission check. Proving those requires a live round trip of the protected field’s value.

Only a passing key is stored in Secret Manager for `books`; `test` keeps its credential in the environment. Failures exit with status `1`. Every provisioning run rotates the secret, and the command reports when a failure leaves the stored credential stale.

`access verify` repeats the proofs against the current credential. See [Rotate the bot credential](../how-to/rotate-the-bot.md) and the [access command reference](../reference/access.md) for commands.