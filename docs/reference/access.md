# `runningtab access`

Provision and verify the writing bot and reader identities against the server’s effective permissions. See the [access model](../explanation/access-model.md) for background and [rotate the bot credential](../how-to/rotate-the-bot.md) for rotation instructions.

## `runningtab access provision`

```text
runningtab access provision --target <test|books> [--profile <bot|reader>] [--yes]
```

Create or repair the selected identity’s role and user, reconcile its grants, mint a fresh API key, and prove that the server enforces the policy. For `books`, store the key in Secret Manager only after every check passes.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--target` | Yes | Site to act on: `test` or `books`. |
| `--profile` | No | Identity to provision: `bot` or `reader`. Defaults to `bot`. |
| `--yes` | No | Skip the confirmation prompt. |
| `--help` | No | Show help and exit. |

### Exit codes

| Code | Meaning |
| --- | --- |
| `1` | A permission proof failed. Permission failures identify the doctype and flag. |

### Notes

For `books`, provisioning requires your own System Manager API token in `RUNNINGTAB_ADMIN_TOKEN`. Supply the ERPNext `key:secret`, optionally prefixed with `token `. The stored bot credential cannot provision. The bot role and user names come from `[bot]` in `books.toml`.

Every run rotates the live secret when it mints the key. Any later failure reports the rotation because the stored credential may be stale. The `test` target’s key is probed during provisioning and is never persisted for standalone verification.

Bot checks prove that:

- A real Journal Entry draft can be created and a real submit attempt is refused.
- Journal Entry and Purchase Invoice deny `submit`, `cancel`, and `amend`.
- Bank Transaction denies `cancel` and `amend`.
- Cash Forecast denies `write` and `delete`.
- Every granted permission is effective, including Bank Transaction `submit`.
- A real write cannot move either Company close-lock field: `accounts_frozen_till_date` or `role_allowed_for_frozen_entries`.

Provisioning fences both close-lock fields at permission level `1`. Frappe silently resets writes above the caller’s permission level through `validate_higher_perm_levels`, so the proof checks that the field values remain unchanged.

Reader checks reject write permissions except the inherited File permissions listed below.

## `runningtab access verify`

```text
runningtab access verify --target <test|books> [--profile <bot|reader>] [--company NAME]
```

Check the stored credential against the selected identity’s permission policy. Exit `0` only when every check passes. This command rotates nothing and can re-check an identity after its policy changes.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--target` | Yes | Site to check. Accepts `test` or `books`, but standalone verification refuses `test`. |
| `--profile` | No | Identity to check: `bot` or `reader`. Defaults to `bot`. |
| `--company` | No | Company name. Defaults to the site’s only company. |
| `--help` | No | Show help and exit. |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | All permission checks passed. |

### Notes

The `test` target has no persisted bot key to verify. Use `runningtab access provision` to mint and probe its key.

Verification checks the bot’s draft creation, submit refusal, effective grants, prohibited permissions, and close-lock protection. Reader verification reports the inherited File permissions on every run and fails on any other write flag.

## Identity grants

### Bot

| Doctype | Grants |
| --- | --- |
| Account | `read`, `write`, `create`, `delete` |
| Company | `read`, `write` |
| Company at permission level `1` | `read` |
| GL Entry | `read` |
| Journal Entry | `read`, `write`, `create` |
| Purchase Invoice | `read`, `write`, `create` |
| Supplier | `read`, `write`, `create` |
| File | `read`, `write`, `create` |
| Bank Transaction | `read`, `write`, `create`, `submit` |
| Cash Forecast | `read`, `create` |

The bot receives no `cancel` or `amend` permission. Bank Transaction is its only submit grant. Submitting a Bank Transaction posts no general-ledger entries; ERPNext writes those entries when a person reconciles the submitted row to a voucher in the Bank Reconciliation Tool. Journal Entry and Purchase Invoice remain draft-only.

Only ingest uses the submit-capable credential through `ingest_config`. Other commands use the drafts-only client, which rejects prohibited `docstatus` values before sending them.

Each capital forecast run creates a new Cash Forecast document. The bot cannot change or delete an existing forecast.

### Reader

| Doctype | Grants |
| --- | --- |
| Account | `read` |
| Company | `read` |
| Company at permission level `1` | `read` |
| GL Entry | `read` |
| Fiscal Year | `read` |
| Customer | `read` |
| Journal Entry | `read` |
| Purchase Invoice | `read` |
| Payment Entry | `read` |
| Sales Invoice | `read` |
| Bank Transaction | `read` |
| File | `read` |
| Supplier | `read` |
| Version | `read` |

Frappe additionally gives every authenticated user inherited `create`, `write`, and `delete` permissions on File. Reader verification reports this known exception on every run and rejects any other write flag.