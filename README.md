# runningtab

Keeps a company's books in ERPNext: applies a versioned chart of accounts, runs
the Mercury ingest that posts bank activity into the ledger as Bank Transactions
for a person to reconcile, forecasts cash, and closes quarters into packages an
outside accountant can read.

**Documentation: <https://transpire-dev.github.io/runningtab/>**

- [Why runningtab, and what it automates](https://transpire-dev.github.io/runningtab/)
- [Tutorial: your chart in a test ledger](https://transpire-dev.github.io/runningtab/tutorial/)
- [How it works](https://transpire-dev.github.io/runningtab/explanation/how-it-works/)
- [Command reference](https://transpire-dev.github.io/runningtab/reference/coa/)
- [Contributing](https://transpire-dev.github.io/runningtab/contributing/)

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
