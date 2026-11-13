# Books directory

Every command reads a books directory selected by `--books DIR` or `RUNNINGTAB_BOOKS`. Put `--books` before the command:

```sh
runningtab --books DIR coa plan
```

The flag takes precedence over the environment variable. Without either, the command refuses and names both ways to select a directory.

The directory must contain `books.toml`, `chart.coa`, `mercury.toml`, `forecast.toml`, and `qbo.toml`. Loading the directory checks that these files exist; their contents have separate loaders. The chart document’s path comes from `books.toml`.

The repository’s [`examples/books/`](https://github.com/transpire-dev/runningtab/blob/main/examples/books/) supplies a complete example used by the test suite. Its hosts, projects, secrets, buckets, and account IDs are made up.

## `books.toml`

[`books.toml`](https://github.com/transpire-dev/runningtab/blob/main/examples/books/books.toml) identifies the books, ledger, credentials, record storage, and users. [`books.py`](https://github.com/transpire-dev/runningtab/blob/main/src/runningtab/books.py) validates its schema.

Every key below is required. Every string must contain at least one non-whitespace character. The sections `site`, `secrets`, `record_buckets`, `bot`, `reader`, and `fence` must be TOML tables.

| Key | Type | Purpose |
| --- | --- | --- |
| `name` | String | Names the books in the period package’s manifest and namespaces Mercury ingest’s local state. |
| `chart_doc` | String | Path to the page whose account tables `chart render-doc` regenerates. Relative paths resolve from the books directory; absolute paths are accepted. |
| `site.host` | String | Ledger host. |
| `site.test_hosts` | Array of strings | Hosts permitted for the test target. Each entry must be non-empty; an empty array is accepted. |
| `secrets.project` | String | Secret Manager project containing the named secrets. |
| `secrets.books` | String | Secret ID for the bot’s ledger key. |
| `secrets.books_readonly` | String | Secret ID for the reader’s ledger key. |
| `secrets.mercury` | String | Secret ID for the read-only Mercury token. |
| `secrets.test_admin` | String | Secret ID for a System Manager key for the test site. |
| `record_buckets.books` | String | Bucket for closed-quarter packages from the books target. |
| `record_buckets.test` | String | Bucket for closed-quarter packages from the test target. |
| `bot.role` | String | Role for the identity that drafts and applies. |
| `bot.user` | String | Bot user identity. |
| `bot.first_name` | String | Bot user’s first name. |
| `reader.role` | String | Role for the identity that only reads. |
| `reader.user` | String | Reader user identity. |
| `reader.first_name` | String | Reader user’s first name. |
| `fence.probe_role` | String | Role used as the Company bypass role while proving the close-lock fence. It must be held by nobody. |

The `secrets` values identify stored secrets. Credential values belong in Secret Manager.

Validation also requires:

- `name` starts with an ASCII letter or digit and contains only ASCII letters, digits, `-`, and `_`.
- No entry in `site.test_hosts` matches `site.host` after host normalization.
- The bot and reader have different users and different roles.
- `fence.probe_role` differs from `Guest`, `All`, `bot.role`, and `reader.role`.

Unknown, missing, and wrongly typed keys are reported by dotted name, such as `site.test_hosts`. The loader resolves `chart_doc` to a path but does not check that the document exists.

See [Targets and credentials](targets-and-credentials.md) and [Access model](../explanation/access-model.md).

## `chart.coa`

[`chart.coa`](https://github.com/transpire-dev/runningtab/blob/main/examples/books/chart.coa) is a versioned outline of the chart of accounts. Its parser and validation live in [`chart.py`](https://github.com/transpire-dev/runningtab/blob/main/src/runningtab/chart.py).

**Account lines and nesting**

Each account line contains a digit-only account number, whitespace, a non-empty name, and an optional tag block:

```text
<number> <name> [tag, tag]
```

Root accounts start at column one. Each child level adds exactly two spaces. Indentation may increase by at most one level from the preceding account. A child belongs to the most recent account at the preceding level.

This excerpt appears exactly in the example:

```text
1000 Assets                                                   [root: Asset]
     > Root.
  1100 Current Assets
    1110 Bank Accounts
      1111 Operating Checking                                 [Bank]
           > The account the business runs through. Customer payments land
           > here and most bills are paid from here.
```

An account with children becomes a group. An account without children is postable. The format cannot express an empty group.

Account names may contain punctuation, including commas, parentheses, `/`, `&`, and `$`. The first `[` starts the tag block.

**Tags**

Tags are comma-separated, with surrounding whitespace removed. A block must contain at least one non-empty tag and end with `]`. It can contain each of the following:

| Tag | Meaning |
| --- | --- |
| `root: Asset`, `root: Liability`, `root: Equity`, `root: Income`, or `root: Expense` | Declares a root account’s type. Required on roots and forbidden on children. |
| An ERPNext account type | Sets the account’s ERPNext type. The parser validates it through `icedcoffee-sync`. |
| `no-bank-line` | Marks an account used for period-end or adjustment journals. Mercury category selection omits it. |

Each block allows at most one root tag, one account-type tag, and one `no-bank-line` tag. Unknown account types are refused.

The example uses account types including `Bank`, `Cash`, `Receivable`, `Payable`, `Fixed Asset`, `Accumulated Depreciation`, `Temporary`, `Tax`, `Depreciation`, and `Round Off`.

These are exact excerpts showing combined and standalone `no-bank-line` tags:

```text
    5203 Depreciation Expense                                 [Depreciation, no-bank-line]
         > The expense side of 1230. Posted by the depreciation schedule.
```

```text
    5218 Foreign Exchange Gain / Loss                         [no-bank-line]
```

`no-bank-line` affects bank-facing selection. The account remains an ordinary ERPNext account.

**Notes and comments**

A line whose first non-whitespace character is `>` adds a note to the preceding account. Successive note lines are stripped and joined with spaces. Note indentation does not determine ownership. A note before any account is refused.

Blank lines and lines whose first non-whitespace character is `#` are ignored. Inline comments have no special syntax.

Notes supply the generated chart document’s “What goes here” column. Bold text and code spans survive rendering and formatting. The renderer’s guidance recommends those forms because formatting can rewrite `*italic*`.

**Company defaults**

A single `[defaults]` block comes last. Each entry has the form `field = number`, with an unquoted, digit-only account number. Every field below is required, and additional fields are refused.

| Field | Company setting | Example account |
| --- | --- | --- |
| `default_bank_account` | Default bank account | `1111` |
| `default_cash_account` | Default cash account | `1121` |
| `default_receivable_account` | Default receivable account | `1131` |
| `default_payable_account` | Default payable account | `2111` |
| `default_expense_account` | Default cost of goods sold account | `5110` |
| `default_income_account` | Default income account | `4110` |
| `round_off_account` | Round off account | `5217` |
| `exchange_gain_loss_account` | Exchange gain / loss account | `5218` |
| `write_off_account` | Write off account | `5220` |
| `default_deferred_revenue_account` | Default deferred revenue account | `2152` |
| `default_deferred_expense_account` | Default deferred expense account | `1141` |
| `accumulated_depreciation_account` | Accumulated depreciation account | `1230` |
| `depreciation_expense_account` | Depreciation expense account | `5203` |
| `disposal_account` | Gain / loss account on asset disposal | `5221` |

The example begins its block with:

```text
[defaults]
default_bank_account = 1111
default_cash_account = 1121
default_receivable_account = 1131
```

Every default must reference an existing postable account. Duplicate fields are refused. After `[defaults]`, every non-comment, non-blank line is parsed as a default entry.

**Chart validation**

Loading the chart requires:

- Unique account numbers.
- A present parent for every child. Outline nesting and unique numbers prevent cycles.
- Exactly one root for each of `Asset`, `Liability`, `Equity`, `Income`, and `Expense`.
- Root types inherited by all descendants.
- Valid ERPNext account types.
- Distinct account names among children of the same parent.
- A complete, valid `[defaults]` block.
- A postable account numbered `5299` with root type `Expense`, used as suspense and aged by health checks.

Failures raise `ChartError` with the offending line, account, or default where applicable.

See [Chart commands](chart.md) and [Why a written chart](../explanation/why-a-written-chart.md).

## `mercury.toml`

[`mercury.toml`](https://github.com/transpire-dev/runningtab/blob/main/examples/books/mercury.toml) maps each Mercury account to its chart balance account and its ERPNext Bank Account document. [`mercury/mapping.py`](https://github.com/transpire-dev/runningtab/blob/main/src/runningtab/mercury/mapping.py) reads two tables:

| Table | Key | Value |
| --- | --- | --- |
| `[accounts]` | Mercury account ID or reported name | Chart account number as a string. |
| `[bank_accounts]` | Mercury account ID or reported name | ERPNext Bank Account document name as a string. |

For example:

```toml
[accounts]
"00000000-0000-4000-8000-000000000001" = "1111"

[bank_accounts]
"00000000-0000-4000-8000-000000000001" = "Mercury Checking - Mercury"
```

An exact account-ID lookup takes precedence over a name lookup. Names are compared case-insensitively with runs of whitespace collapsed.

`mercury install-bank-accounts` uses `[accounts]` to link each Bank Account to its chart account. Bank Transactions attach to the document named in `[bank_accounts]`. Supply one mapping for each Mercury account, including savings and credit accounts. Operations requiring an unmapped account refuse.

The example’s Bank Account names follow `<account name> - Mercury`. It maps checking to `1111`, savings to `1112`, and the card to liability account `2121`.

The parser accepts omitted tables as empty mappings. Present tables must contain string values. Chart validation separately identifies any `[accounts]` values absent from the chart.

`[categories]` and `[posting]` are explicitly refused. Other unknown top-level keys are ignored by this parser. Mercury’s GL code travels with the transaction as a hint; the person reconciling selects the account.

See [Connect Mercury](../how-to/connect-mercury.md) and [Mercury reference](mercury.md).

## `forecast.toml`

[`forecast.toml`](https://github.com/transpire-dev/runningtab/blob/main/examples/books/forecast.toml) describes expected recurring expenses and the member accounts used for capital calls. [`forecast/model.py`](https://github.com/transpire-dev/runningtab/blob/main/src/runningtab/forecast/model.py) validates it against the chart.

**Capital-call policy**

The `[call]` table is required.

| Key | Type | Required | Meaning |
| --- | --- | --- | --- |
| `members` | Array of strings | Yes | Non-empty, duplicate-free list of postable member capital account numbers beginning with `31`. |
| `floor` | Number | No | Non-negative cash floor overriding the computed floor. |

The capital call is divided evenly among the listed members, with each share rounded up to the cent. Without `floor`, computation uses the largest owed charge, or zero when no charges are owed.

**Expense lines**

Each `[[line]]` table describes one recurring expense. The file may contain zero lines.

| Key | Type | Required | Meaning |
| --- | --- | --- | --- |
| `name` | String | Yes | Non-empty line name, unique within the file. |
| `kind` | String | Yes | Must be `"expense"`. |
| `account` | String | Yes | Postable chart account number beginning with `5`. |
| `paid` | String | Yes | `"mercury"` or `"reimbursed"`. |
| `match` | Array of non-empty strings | For `"mercury"` | Mercury counterparty names used to recognize payment. Defaults to an empty list for reimbursed lines. |
| `cadence` | String | Yes | `"monthly"` or `"yearly"`. |
| `day` | Integer | For monthly lines | Day from `1` through `31`. Days beyond a month’s length fall on its last day. |
| `on` | TOML date | For yearly lines | Date whose month and day recur each year. February 29 is refused. |
| `amount` | Number | One amount form required | Fixed expense per occurrence. |
| `low`, `high` | Numbers | One amount form required | Expense range, with `0 ≤ low ≤ high`. Computation uses `high`. |
| `starts` | TOML date | No | Earliest included due date. |
| `ends` | TOML date | No | Latest included due date. |
| `lag_days`, `confidence` | Unvalidated | No | Reserved fields, accepted and ignored. |

Use either `amount` or both `low` and `high`. Fixed amounts must be non-negative. Monthly lines require `day` and reject `on`; yearly lines require `on` and reject `day`. Dates use unquoted TOML date syntax such as `2027-04-29`. If both `starts` and `ends` are present, `starts` must be no later than `ends`.

This excerpt from the example shows a fixed monthly expense:

```toml
[[line]]
name = "Linear"
kind = "expense"
account = "5219"
match = ["Linear Orbit"]
cadence = "monthly"
day = 17
amount = 96
paid = "mercury"
```

For a reimbursed expense, `match` names the member receiving the reimbursement. A line with no matching names remains owed. Matching compares counterparty names case-insensitively and also checks payment amount and timing; a matching name alone does not settle a line.

Unknown fields inside `[[line]]` are refused. Unknown top-level keys and extra `[call]` keys are ignored by the model.

See [Forecast reference](forecast.md) and [Capital forecast](../how-to/capital-forecast.md).

## `qbo.toml`

[`qbo.toml`](https://github.com/transpire-dev/runningtab/blob/main/examples/books/qbo.toml) supplies the QuickBooks Online account mapping used by `period export`. [`period/qbo_map.py`](https://github.com/transpire-dev/runningtab/blob/main/src/runningtab/period/qbo_map.py) reads four required tables.

| Table | Entry format | Purpose |
| --- | --- | --- |
| `[types]` | `"Type" = ["Detail Type", ...]` | Defines the accepted detail types for each QBO type used by the mapping. |
| `[accounts]` | `"number" = ["Type", "Detail Type"]` | Assigns a QBO type and detail type to a postable chart account. Each value must contain exactly two strings. |
| `[builtin]` | `"number" = { name = "...", role = "..." }` | Maps a chart account to a built-in QBO account. Both fields must be strings. |
| `[clearing]` | `type = "..."` and `detail = "..."` | Types the generated `QBO Import Clearing` account used when splitting vouchers with multiple receivable or payable lines. |

The example’s built-in mappings are:

```toml
[builtin]
"1131" = { name = "Accounts Receivable (A/R)", role = "receivable" }
"2111" = { name = "Accounts Payable (A/P)", role = "payable" }
"3320" = { name = "Retained Earnings", role = "retained_earnings" }
```

Supported roles are `receivable`, `payable`, and `retained_earnings`. Validation checks them against the chart:

| Role | Required chart property |
| --- | --- |
| `receivable` | ERPNext account type `Receivable`. |
| `payable` | ERPNext account type `Payable`. |
| `retained_earnings` | Root type `Equity`. |

Every postable account must appear in exactly one of `[accounts]` or `[builtin]`. Group accounts and numbers absent from the chart are refused. Each type/detail pair, including the clearing pair, must appear in `[types]`. Validation uses the vocabulary in this file.

Imported account names must be unique across ordinary chart accounts, built-in names, and `QBO Import Clearing`. The export also requires exactly one Equity account in `[builtin]` for retained earnings.

The parser checks the required tables and entry shapes. It does not reject additional top-level keys or additional fields in built-in and clearing entries.

!!! note
    The example file records two unresolved QBO import checks: journal dates use `%Y-%m-%d`, and unused debit or credit cells are blank. Its comments mark both conventions as unverified against a real QBO import.

See [Period reference](period.md).

## `chart-of-accounts.md`

The chart document explains what each account is for and how to choose between nearby accounts. Its path is set by `books.toml`’s `chart_doc`; `chart-of-accounts.md` is the example filename.

[`doc.py`](https://github.com/transpire-dev/runningtab/blob/main/src/runningtab/doc.py) generates tables between these marker pairs:

```markdown
<!-- chart:table 1000 -->

<!-- chart:end -->
```

```markdown
<!-- chart:defaults -->

<!-- chart:end -->
```

`<!-- chart:table N -->` generates the subtree rooted at account number `N`, including that account, in chart order. Its columns are Number, Account, Type, and What goes here. Group accounts display `Group`; other accounts display their ERPNext type when one is set. Notes come from `>` lines in `chart.coa`.

`<!-- chart:defaults -->` generates Company setting labels and account numbers from `[defaults]`, preserving the defaults’ order.

The [example chart document](https://github.com/transpire-dev/runningtab/blob/main/examples/books/chart-of-accounts.md) has one table for each root subtree and a defaults table.

`chart render-doc` replaces the content inside each marker pair and preserves the marker lines and surrounding prose. Edit account names, types, notes, and defaults in `chart.coa`. Edit explanatory prose outside the marked regions directly.

Rendering refuses unknown recognized `chart:` markers, an unmatched start or end marker, and a table marker naming an account absent from the chart. Literal `|` characters in table cells are escaped.

See [Chart commands](chart.md).