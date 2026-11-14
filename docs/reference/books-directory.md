# Books directory

Every command reads a books directory selected by `--books DIR` or `RUNNINGTAB_BOOKS`. The flag takes precedence and goes before the command:

```sh
runningtab --books DIR coa plan --target test
```

Use `runningtab <command> --help` for command options.

The directory must contain `books.toml`, `chart.coa`, `mercury.toml`, `forecast.toml`, and `qbo.toml`. The chart document’s path comes from `books.toml`. The complete [example directory](https://github.com/transpire-dev/runningtab/tree/main/examples/books) uses made-up hosts, projects, secrets, buckets, and account IDs.

## `books.toml`

This file identifies the books, ledger, credentials, record storage, and users. All keys below are required; unknown keys are refused. Strings must contain non-whitespace text.

| Key | Type | Purpose |
| --- | --- | --- |
| `name` | String | Manifest name and Mercury ingest state namespace. |
| `chart_doc` | String | Chart document path; relative paths start at the books directory. |
| `site.host` | String | Ledger host. |
| `site.test_hosts` | String array | Hosts permitted for the test target. |
| `secrets.project` | String | Secret Manager project. |
| `secrets.books` | String | Secret ID for the bot’s ledger key. |
| `secrets.books_readonly` | String | Secret ID for the reader’s ledger key. |
| `secrets.mercury` | String | Secret ID for the read-only Mercury token. |
| `secrets.test_admin` | String | Secret ID for a test-site System Manager key. |
| `record_buckets.books` | String | Closed-quarter package bucket for books. |
| `record_buckets.test` | String | Closed-quarter package bucket for test. |
| `bot.role` | String | Role that drafts and applies. |
| `bot.user` | String | Bot identity. |
| `bot.first_name` | String | Bot’s first name. |
| `reader.role` | String | Read-only role. |
| `reader.user` | String | Reader identity. |
| `reader.first_name` | String | Reader’s first name. |
| `fence.probe_role` | String | Company bypass role for proving the close-lock fence; nobody may hold it. |

Credential values belong in Secret Manager. The bot and reader need different users and roles. The probe role must differ from both roles, `Guest`, and `All`. Test hosts must differ from the ledger host.

## `chart.coa`

The chart is a versioned account outline. Account lines contain a digit-only number, a name, and optional comma-separated tags. Roots start at column one; each child level adds two spaces.

This excerpt comes from the example:

```text
1000 Assets                                                   [root: Asset]
     > Root.
  1100 Current Assets
    1110 Bank Accounts
      1111 Operating Checking                                 [Bank]
           > The account the business runs through. Customer payments land
           > here and most bills are paid from here.
```

Accounts with children become groups; accounts without children are postable. Account numbers must be unique.

Each root requires a `root:` tag. The chart must have exactly one root for each of `Asset`, `Liability`, `Equity`, `Income`, and `Expense`. Children inherit their root type.

An ERPNext account-type tag, such as `Bank` or `Receivable`, sets the account type. Unknown types are refused. `no-bank-line` omits an account from Mercury category selection for period-end or adjustment journals. Each tag block permits one root tag, one account-type tag, and one `no-bank-line` tag.

Lines beginning with `>` add notes to the preceding account. Consecutive note lines join with spaces and supply the chart document’s “What goes here” column. Use bold text and code spans for formatting. Blank lines and lines beginning with `#` are ignored.

A single `[defaults]` block comes last. Entries use `field = number`, with unquoted account numbers referencing existing postable accounts. All fields shown in the [example chart](https://github.com/transpire-dev/runningtab/blob/main/examples/books/chart.coa) are required; additional fields are refused. The chart also requires postable Expense account `5299` for suspense.

See [Why a written chart](../explanation/why-a-written-chart.mdNone).

## `mercury.toml`

Two tables map Mercury account IDs or reported names:

- `[accounts]` maps to chart account numbers as strings.
- `[bank_accounts]` maps to ERPNext Bank Account document names.

Supply mappings for every Mercury account, including savings and credit accounts. Exact IDs take precedence over names. Bank Transactions attach to the documents named in `[bank_accounts]`.

`[categories]` and `[posting]` are refused. Mercury’s GL code travels as a hint; the person reconciling selects the account.

## `forecast.toml`

This file describes recurring expenses and capital-call members.

| Key | Requirement and meaning |
| --- | --- |
| `call.members` | Required list of postable member capital accounts beginning with `31`. |
| `call.floor` | Optional non-negative cash floor. |
| `line.name` | Required unique expense name. |
| `line.kind` | Required; `"expense"`. |
| `line.account` | Required postable account beginning with `5`. |
| `line.paid` | Required; `"mercury"` or `"reimbursed"`. |
| `line.match` | Matching names; required for Mercury payments. |
| `line.cadence` | Required; `"monthly"` or `"yearly"`. |
| `line.day` / `line.on` | Monthly day or yearly TOML date. |
| `line.amount` / `line.low`, `line.high` | Fixed amount or range; computation uses `high`. |
| `line.starts`, `line.ends` | Optional due-date bounds. |

Use a `[call]` table and one `[[line]]` table per expense. Capital calls divide evenly among members, rounding each share up to the cent. Without `floor`, the largest owed charge sets the floor.

## `qbo.toml`

This file maps accounts for `period export`. Its required tables are `[types]` for accepted type/detail pairs, `[accounts]` for ordinary account mappings, `[builtin]` for QBO built-ins, and `[clearing]` for `QBO Import Clearing`.

Every postable account must appear exactly once across `[accounts]` and `[builtin]`. See the [example mapping](https://github.com/transpire-dev/runningtab/blob/main/examples/books/qbo.toml).

## Chart document

The document at `chart_doc` combines explanatory prose with generated regions:

```markdown
<!-- chart:table 1000 -->
<!-- chart:end -->

<!-- chart:defaults -->
<!-- chart:end -->
```

A table marker generates that account’s subtree. The defaults marker generates Company settings. `chart render-doc` replaces content inside the markers and preserves surrounding prose. Edit account data in `chart.coa`; edit explanatory prose outside the markers.