# Why write the chart down?

Your chart of accounts is your company’s vocabulary for where money goes. It defines the distinctions you want your books to preserve: production hosting and development hosting, money contributed by an owner and money lent by an owner, operating cash and reserves. Every downstream system depends on those decisions.

In runningtab, `chart.coa` is a versioned file that defines that vocabulary. You can review changes in a diff, document what each account means, and reuse the same decisions across your ledger, bank, and quarterly packages. If you change ledgers, the chart comes with you.

## Make decisions readable

The chart uses an indented outline. Each account has a number and a name, sits beneath its parent, and can carry notes explaining how to use it.

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

The hosting note gives a specific boundary: infrastructure serving customers belongs in `5110`; development and staging belong in `5150`. The subcontractor note distinguishes billable client work from internal work. These explanations help you choose an account and remember the decision when you return to a transaction.

Versioning makes changes reviewable. A diff shows an added account, a changed name, or a revised explanation. You can assess the accounting choice alongside the structure that downstream systems will use.

## Keep explanations connected

An account name can only say so much. The annotated chart document explains what accounts are for and how to choose between neighbouring accounts. Its account tables are generated from `chart.coa`.

You edit the document’s prose directly. `chart render-doc` regenerates the account tables, keeping the reference connected to the file that drives the systems. You can consult the explanations while doing the books and check the tables against their source.

`chart render-doc --check` checks whether the generated content is current. It writes nothing and reports a diff when that content is stale. For command details, use `runningtab chart render-doc --help`. See [Books directory](../reference/books-directory.md) for the files that hold the chart and its documentation.

## Reuse the vocabulary

The written chart supplies several parts of the bookkeeping workflow:

- ERPNext accounts are managed from the chart.
- Mercury’s GL-code picker receives exported labels through a manual upload into the bank dashboard.
- Mercury’s custom categories are managed from the chart.
- Each quarter’s QuickBooks Online import kit uses the chart through `qbo.toml`.
- The ingest uses the chart when proposing draft vouchers.

Each system has its own requirements. Mercury’s GL-code importer requires comma-free labels, so an exported label can differ from the ERPNext account name. The chart remains the shared definition behind those representations.

The chart also records which accounts are reached only through period-end or adjustment journals. Mercury’s category management carries that decision through by offering no category for those accounts.

These uses connect the chart to [daily bookkeeping](../how-to/daily.md) and [closing a quarter](../how-to/close-a-quarter.md).

## Start changes in the chart

`chart.coa` is the source of truth. Chart changes start in that file. Edits in ERPNext’s UI leave the written definition unchanged.

Before applying changes, runningtab validates the chart’s structure and account types. `coa plan` then shows how the chart differs from the ledger without writing anything. You can review both the written decision and its effect on the accounts before applying it.

The work of defining your chart belongs to your company. Keeping it in a versioned file preserves that work as documentation you can review, explain, and carry between systems. To try the workflow, follow [Set up](../how-to/set-up.md).