# Why write the chart down?

Your chart of accounts is your company’s vocabulary for where money goes. It defines the distinctions you want your books to preserve: production hosting and development hosting, money contributed by an owner and money lent by an owner, operating cash and reserves. Every downstream system depends on those decisions.

Writing that vocabulary down makes it available wherever you keep the books. In runningtab, `chart.coa` is a versioned file that defines the chart. You can review changes in a diff, document what each account means, and reuse the same decisions across your ledger, bank, and quarterly packages. If you change ledgers, the chart comes with you.

## Make the decisions readable

The chart uses an indented outline. Each account has a number and a name, with optional tags for its root type, ERPNext account type, or `no-bank-line` designation. Accounts sit beneath their parents, indented two spaces per level. Lines beginning with `>` carry notes.

These lines are copied from the [example chart](https://github.com/transpire-dev/runningtab/blob/main/examples/books/chart.coa):

```text
5000 Expenses                                                 [root: Expense]
     > Root.
  5100 Cost of Revenue
    5110 Production Hosting (AWS, Fastly)
         > Infrastructure that serves customers. The default expense account.
         > Development and staging are 5150.
    5120 Subcontractors
         > Outside people doing billable client work. Contractors on internal
         > work go to the 5200 account for what they did.
```

The notes explain how to choose an account. Here, the hosting note gives a specific boundary: infrastructure serving customers belongs in `5110`; development and staging belong in `5150`. That explanation is useful whenever you return to a transaction and need to remember the decision.

Versioning gives changes a reviewable form. A diff shows an added account, a changed name, or a revised explanation. You can assess the accounting choice alongside the structure that downstream systems will use.

## Keep the explanation connected to the chart

An account name can only say so much. The annotated chart document explains what accounts are for and how to choose between neighbouring accounts. The `chart_doc` key in `books.toml` identifies that document.

Its account tables are generated from `chart.coa` by `chart render-doc`. You edit the document’s prose sections directly; the command regenerates the tables inside their markers. This keeps the account reference connected to the file that drives the systems.

You can also run `chart render-doc --check` to check whether the document is current. It writes nothing and reports a diff if the generated content is stale. See the [chart command reference](../reference/chart.md) for details.

The result is stable documentation you can consult while doing the books, with account tables that can be checked against their source.

## Reuse the vocabulary across systems

The written chart supplies several parts of the bookkeeping workflow:

- ERPNext accounts are managed through `coa plan`, `coa apply`, and `coa prune`.
- Mercury’s GL-code picker receives labels exported by `coa export-gl-codes`, with a manual upload into the bank dashboard.
- Mercury’s custom categories are managed through `mercury categories`.
- Each quarter’s QuickBooks Online import kit uses the chart through `qbo.toml`.
- The ingest uses the chart when proposing draft vouchers.

Each system has its own requirements. Mercury’s GL-code importer, for example, requires comma-free labels, so an exported label can differ from the ERPNext account name. The chart remains the shared definition behind those representations.

The `no-bank-line` designation also carries a decision across systems. It identifies an account reached only through period-end or adjustment journals, so `mercury categories` offers no category for it.

## Give changes a clear starting point

`chart.coa` is the source of truth. Changes to the chart start in that file; editing accounts in ERPNext’s UI does not redefine it.

Before applying changes, runningtab validates the chart’s structure and account types. `coa plan` then shows how the chart differs from the ledger without writing anything. This gives you a chance to review both the written decision and its effect on the accounts.

The work of defining your chart belongs to your company. Keeping it in a versioned file preserves that work as documentation you can review, explain, and carry between systems. To try the workflow, follow [Your chart in a test ledger](../tutorial.md).