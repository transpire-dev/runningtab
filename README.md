# runningtab

Keeps a company's books in ERPNext: applies a versioned chart of accounts, runs
the Mercury ingest that posts bank activity into the ledger as Bank Transactions
for a person to reconcile, forecasts cash, and closes quarters into packages an
outside accountant can read.

**Documentation: <https://transpire-dev.github.io/runningtab/>**

- [What runningtab is, and why](https://transpire-dev.github.io/runningtab/)
- [Tutorial: your chart in a test ledger](https://transpire-dev.github.io/runningtab/tutorial/)
- [Why write the chart down](https://transpire-dev.github.io/runningtab/explanation/why-a-written-chart/)
- [Set up a site](https://transpire-dev.github.io/runningtab/how-to/set-up/)
- [Daily operations](https://transpire-dev.github.io/runningtab/how-to/daily/)
- [Close a quarter](https://transpire-dev.github.io/runningtab/how-to/close-a-quarter/)

Every command's flags, defaults and exit codes are in `runningtab <command> --help`.

[Source on GitHub](https://github.com/transpire-dev/runningtab)

## Installation

Requires Python 3.14. To include pre-releases:

```sh
pip install --pre runningtab
# or
uv add --prerelease allow runningtab
```

runningtab ships no books. Every command reads a books directory you keep in
git, named by `--books DIR` or `RUNNINGTAB_BOOKS`;
[`examples/books/`](https://github.com/transpire-dev/runningtab/tree/main/examples/books)
is a complete example.

## Development

Use a GIL-enabled Python 3.14: some dependencies publish no free-threaded
wheels. uv cannot download one by that name, so install the regular 3.14 first:

```sh
uv python install 3.14
export UV_PYTHON=3.14+gil
just check   # lint, typecheck, deptry
just test    # excludes the live tests, which write to a real ERPNext site
just docs-serve
```

`just --list` shows the rest, including `test-live`. `tests/test_docs.py` checks
every `runningtab` command shown in `docs/` against the CLI.
