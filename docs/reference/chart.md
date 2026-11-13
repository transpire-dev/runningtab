# `chart`

The `chart` commands regenerate account tables in your chart document from `chart.coa`.

## `runningtab chart render-doc`

```text
runningtab chart render-doc [OPTIONS]
```

Regenerates the account tables in the chart document configured by `chart_doc` in `books.toml`, using `chart.coa`. By default, writes the file and reports whether it changed. With `--check`, leaves the file unchanged and prints a unified diff if the document is stale.

### Options

| Option | Required | What it does |
| --- | --- | --- |
| `--check` | No | Check for a stale document, print a diff, and exit with code 1 if stale. Writes nothing. |
| `--help` | No | Show help and exit. |

### Exit codes

| Code | Meaning |
| --- | --- |
| 1 | With `--check`, the chart document is stale. |

### Notes

Use `--check` as a CI gate.

See [Why a written chart](../explanation/why-a-written-chart.md) for background, [Set up a new site](../how-to/new-site.md) for setup, and [Books directory](books-directory.md) for configuration reference.