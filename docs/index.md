# runningtab

runningtab is an open-source Python command-line tool that keeps your company’s books in ERPNext. It is available on PyPI under the Apache-2.0 license.

## Why automate your books early?

Start automating your bookkeeping while the company is small, and you make fewer mistakes as it grows. Books kept accurately from the start also show you how the company works: where the money goes and what each software choice costs, early enough to tell whether the business is going to work.

Machines draft; humans post. The bot credential can prepare entries, but the server refuses any submission from it that commits money. A person posts every entry.

## What it automates

- **Chart of accounts.** You keep `chart.coa` as a versioned text file in a books directory in git. `coa plan` shows proposed ERPNext changes; `coa apply` makes them. The same chart produces Mercury’s GL-code list and custom categories, an annotated chart document, and a QuickBooks Online import kit for your accountant.

- **Bank transactions.** `mercury sync` brings every settled Mercury transaction and its uploaded receipts into ERPNext for you to reconcile. It drafts a matching journal entry when Mercury coding or the payee’s history identifies an account. Transfers between your own accounts get one drafted voucher.

- **Quarter close.** `period close` cross-checks the books against ERPNext’s reports, locks the quarter, and stores a checksummed package: trial balance, general ledger, statements, bank reconciliation, receipts, a QuickBooks import kit, and a plain-language MANIFEST. An accountant who has never worked with you can read it. `period status` rebuilds every closed quarter and reports differences from the stored packages.

- **Health checks.** `runningtab health` reports failures that can otherwise stay silent: bank transactions that no longer sum to the balance, transactions uncoded at Mercury for more than 14 days, money in suspense, bank ingest more than 7 days behind, and closed quarters that no longer match their stored packages.

- **Cash forecast.** `forecast` calculates how much each member must contribute to keep cash above a floor through the end of the third full month ahead. This fits a company funded by its members, such as an LLC.

## What you need

You need Python 3.14, an ERPNext site plus a disposable test site for practice, a Mercury account, and Google Cloud Secret Manager and Cloud Storage. Secret Manager holds credentials; Cloud Storage holds closed-quarter records. Secret Manager is the current credential backend and may become pluggable.

Install with `pip install --pre runningtab` or `uv add --prerelease allow runningtab`.

## Where to go next

Start with [Your chart in a test ledger](tutorial.md). The tutorial needs only an ERPNext test site.

Then read [How it works](explanation/how-it-works.md) for the overall workflow. Use the [chart command reference](reference/coa.md) for command details and [targets and credentials](reference/targets-and-credentials.md) for connection settings.