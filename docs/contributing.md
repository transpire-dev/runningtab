# Contributing to runningtab

This page covers changing [runningtab itself](https://github.com/transpire-dev/runningtab): the source layout, development checks, live tests, fixtures, documentation, and editor grammar.

## Set up and run checks

Use Python 3.14, `uv`, and `just`. Select a GIL-enabled Python before installing or testing. uv cannot download an interpreter by the name `3.14+gil`, so install the regular 3.14 first, as CI does:

```sh
uv python install 3.14
export UV_PYTHON=3.14+gil
just install
just check
just test
```

Some dependencies, `grpcio` among them, publish no free-threaded wheels, so on a free-threaded 3.14 uv builds them from source. CI sets `UV_PYTHON=3.14+gil` too. The docs recipes ask for a GIL-enabled Python on their own.

No lock file is committed: the package resolves against the dependencies its users get.

The [justfile](https://github.com/transpire-dev/runningtab/blob/main/justfile) defines these development recipes:

| Recipe | What it runs |
| --- | --- |
| `just install` | `uv sync` |
| `just lint` | Ruff lint and formatting checks |
| `just lint-fix` | Ruff automatic fixes and formatting |
| `just typecheck` | `ty` on `src` and `tests` |
| `just deps` | `deptry` on `src` |
| `just check` | Lint, type checking, and dependency checks |
| `just test` | Pytest, excluding live tests |

Extra arguments to `just test` pass through to pytest:

```sh
just test tests/test_docs.py
```

## Source layout

Code lives in [`src/runningtab/`](https://github.com/transpire-dev/runningtab/blob/main/src/runningtab). These summaries follow the module docstrings.

- `__init__.py` — Applies a books directory’s chart, bank feed, and close.
- `access.py` — Defines the books bot’s permissions.
- `books.py` — Loads the books directory and its configuration.
- `chart.py` — Loads and validates `chart.coa`.
- `cli.py` — Provides Typer wiring, prompts, output, and exit codes.
- `coa.py` — Orchestrates chart planning, applying, pruning, and parking.
- `doc.py` — Renders chart documentation tables from `chart.coa`.
- `gl_codes.py` — Produces Mercury’s postable-account CSV.
- `health.py` — Defines six health checks and their exit-code rule.
- `site.py` — Resolves ERPNext targets and credentials.
- `stock_chart.py` — The account names of ERPNext's "Standard" chart, which `coa clear-stock` recognises.

The `mercury/` package handles bank ingestion:

- `mercury/__init__.py` — Reads the bank and posts statement lines.
- `mercury/categories.py` — Mirrors chart accounts into Mercury custom categories.
- `mercury/classify.py` — Decides which transactions become Bank Transactions.
- `mercury/client.py` — Provides a read-only Mercury client.
- `mercury/ingest.py` — Applies snapshots and subsequent event-log changes.
- `mercury/mapping.py` — Maps Mercury accounts into the books.
- `mercury/models.py` — Parses Mercury payloads into typed records.
- `mercury/posting.py` — Builds and submits ERPNext Bank Transactions.
- `mercury/receipts.py` — Attaches Mercury receipts to Bank Transactions.
- `mercury/session.py` — Assembles posting context from site, chart, and mapping.
- `mercury/state.py` — Tracks the ingest’s event-log position.
- `mercury/vouchers.py` — Drafts vouchers beside coded Bank Transactions.

The `period/` package handles quarter-end records:

- `period/__init__.py` — Packages a quarter’s books for accountants and auditors.
- `period/balances.py` — Builds trial balances, statements, and listings.
- `period/bank.py` — Compares quarter-end bank and ledger balances.
- `period/close.py` — Runs the close ceremony in order.
- `period/crosscheck.py` — Checks package figures against ERPNext reports.
- `period/csvfmt.py` — Formats package money and CSV rows.
- `period/documents.py` — Collects supporting receipts and invoices.
- `period/fence.py` — Protects close-lock fields and verifies bot restrictions.
- `period/ledger.py` — Reads and parses the ledger.
- `period/lock.py` — Reads the close lock, its history, and advances it.
- `period/manifest.py` — Writes the human-readable `MANIFEST.md`.
- `period/open_items.py` — Calculates open receivables and payables by party and document.
- `period/package.py` — Builds, checks, and writes package contents.
- `period/qbo.py` — Builds the QuickBooks Online import kit.
- `period/qbo_map.py` — Loads QuickBooks chart mappings from `qbo.toml`.
- `period/quarter.py` — Handles calendar quarters and ERPNext fiscal years.
- `period/readiness.py` — Identifies blockers to closing.
- `period/record.py` — Stores closed-quarter packages in the record bucket.
- `period/status.py` — Checks stored packages against the ledger and lock.

The `forecast/` package calculates capital needs:

- `forecast/__init__.py` — Defines the capital forecast’s scope.
- `forecast/compute.py` — Calculates the capital call through the end of the third full month ahead.
- `forecast/doctype.py` — Defines and writes Cash Forecast documents.
- `forecast/model.py` — Loads expected cash movements from the forecast file.
- `forecast/names.py` — Holds import-free doctype names for access policy.
- `forecast/sources.py` — Supplies opening cash and reconciliation figures.

`icedcoffee-sync` supplies `DesiredAccount`, the diff/apply/prune engine, and ERPNext facts including `ROOT_TYPES` and `ACCOUNT_TYPES`. It does not depend on runningtab.

## Live tests and fixtures

Live tests write to a real disposable ERPNext site and take several minutes. They carry the `live_runningtab` marker and are skipped unless `RUNNINGTAB_LIVE=1`. They never run through `just test` or CI’s default suite.

Set `RUNNINGTAB_BOOKS` to your own books directory. `examples/books` contains fake hosts and secrets.

```sh
export RUNNINGTAB_BOOKS=/path/to/your/books
just test-live
```

The recipe enables `RUNNINGTAB_LIVE`. Test credentials come from `RUNNINGTAB_TEST_URL`, `RUNNINGTAB_TEST_KEY`, and `RUNNINGTAB_TEST_SECRET`, or the stored `secrets.test_admin` key. Use the environment variables to target your own bench. See [targets and credentials](reference/targets-and-credentials.md).

To seed period scenarios on that disposable site and capture their general ledger into `tests/fixtures/period/`, run once:

```sh
just capture-period-fixtures
```

## Documentation and Zed grammar

Zensical builds `docs/` using `zensical.toml`. Preview with `just docs-serve`; validate with `just docs-build`, which fails on any warning. Add pages to the navigation in `zensical.toml`.

`tests/test_docs.py` checks every `runningtab` invocation shown on any page, and every command and flag in `docs/reference/`, against the CLI, and fails when a command has no reference entry. It does not notice a new flag that no page mentions, so add one to the command's reference section when you add it.

The [documentation workflow](https://github.com/transpire-dev/runningtab/blob/main/.github/workflows/docs.yml) publishes GitHub Pages from `main`.

For `chart.coa` syntax highlighting, see the [Zed grammar README](https://github.com/transpire-dev/runningtab/blob/main/zed-coa/README.md). Run `just zed-coa-check` with Node.js and a C compiler installed.

## CI and publishing

[CI](https://github.com/transpire-dev/runningtab/blob/main/.github/workflows/ci.yml) runs `check`, `test`, and `zed-coa-check` on every pull request, on every push to `main`, and on `v*` tags.

A push to `main` publishes `<version>.devN` to PyPI. A `v*` tag on `main` matching the version in `pyproject.toml` publishes the release. Both use trusted publishing, with no stored PyPI credential.