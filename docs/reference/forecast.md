# Forecast commands

The `runningtab forecast` commands compute the capital call each member owes to keep cash at or above a floor through the end of the third full month ahead. See [How it works](../explanation/how-it-works.md) for background and [Capital forecast](../how-to/capital-forecast.md) for the workflow.

## `runningtab forecast plan`

```text
runningtab forecast plan --env <sandbox|production> --target <test|books>
```

Computes and prints the forecast using reader credentials. Reads Mercury balances and transactions, ledger Bank Transactions, and `forecast.toml`. Writes nothing.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--env` | Yes | Selects the Mercury deployment: `sandbox` or `production`. |
| `--target` | Yes | Selects the site: `test` or `books`. |
| `--help` | No | Shows help and exits. |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | Refusal; prints one sentence. |
| `2` | Usage error when `--env sandbox` is paired with `--target books`; refused before any request. |

Refusals occur when:

- Mercury returns no accounts or an account balance differs from the sum of its transactions.
- A Bank Transaction has no readable date.
- `forecast.toml` fails validation.
- The site is in maintenance mode (`SessionStopped`).
- Mercury’s API refuses a request.

### Notes

`forecast.toml` lives in the books directory beside the chart and is validated against it on load. The global `--books DIR` option goes before the command. See [Books directory](books-directory.md).

Money uses `Decimal` throughout. Each line costs its `high` amount. A charge that has not appeared in the bank remains owed. Each member’s share rounds up to the cent. The cash floor is the largest single charge.

Output includes:

- The forecast window.
- Opening cash: the sum of every Mercury account’s balance, net of the card.
- The ledger-through date: the day before the oldest unreconciled Bank Transaction, or today.
- A table of months and each due with its status: `owed` or `seen paid`.
- The floor and what set it: a line name, `[call] floor`, or `no owed charges`.
- The total call and each member’s share.

`now` uses local time.

## `runningtab forecast apply`

```text
runningtab forecast apply --env <sandbox|production> --target <test|books>
```

Computes and prints the forecast, then records it as a new Cash Forecast document using bot credentials. Reads Mercury balances and transactions, ledger Bank Transactions, `forecast.toml`, Company, and Cash Forecast. Every run adds one document and preserves earlier documents.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--env` | Yes | Selects the Mercury deployment: `sandbox` or `production`. |
| `--target` | Yes | Selects the site: `test` or `books`. |
| `--help` | No | Shows help and exits. |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | Refusal; prints one sentence. |
| `2` | Usage error when `--env sandbox` is paired with `--target books`; refused before any request. |

Refusals occur when:

- Mercury returns no accounts or an account balance differs from the sum of its transactions.
- A Bank Transaction has no readable date.
- `forecast.toml` fails validation.
- The site is in maintenance mode (`SessionStopped`).
- The Cash Forecast doctype is missing.
- Mercury’s API refuses a request.
- The bot lacks permission to create Cash Forecast documents; run `runningtab access provision`.

### Notes

Uses the same calculation, inputs, and forecast output described under [`runningtab forecast plan`](#runningtab-forecast-plan). After writing, prints `wrote CF-00001` with the new document’s identifier and its desk URL.

Install the doctypes with `runningtab forecast install-doctype`, then run `runningtab access provision` to grant the bot `read` and `create` on Cash Forecast. See [Access commands](access.md) and [Targets and credentials](targets-and-credentials.md).

## `runningtab forecast install-doctype`

```text
runningtab forecast install-doctype --target <test|books> [--yes]
```

Creates the four Cash Forecast doctypes using administrator credentials. Reads DocType and writes the Cash Forecast doctypes. Re-running repairs field-list drift and never deletes a doctype.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--target` | Yes | Selects the site: `test` or `books`. |
| `--yes` | No | Skips the confirmation prompt. |
| `--help` | No | Shows help and exits. |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | Refusal; prints one sentence. |

### Notes

Prints one line per doctype with its outcome: `created`, `updated`, or `unchanged`.

Run this command before `runningtab access provision`, then use `runningtab forecast plan` and `runningtab forecast apply`. The bot can write forecasts; installing doctypes requires an administrator.

`runningtab access provision` and `runningtab access verify` refuse with one sentence and write nothing if the doctype is missing. See [Access model](../explanation/access-model.md) for background.