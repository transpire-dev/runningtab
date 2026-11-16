# runningtab

runningtab is an open-source Python command-line tool that keeps your company’s books in ERPNext. It is available on PyPI under the Apache-2.0 license.

The written chart of accounts is why runningtab exists. You keep the chart in git, with explanations of what accounts are for and how to choose between them. Read [Why a written chart matters](explanation/why-a-written-chart.md) first.

## Why automate early?

Start automating bookkeeping while the company is small, and you make fewer mistakes as it grows. Accurate books also show you how the company works: where the money goes and what each software choice costs. You get that information early enough to judge whether the business is going to work.

## How it works

You keep a books directory in git. ERPNext holds the ledger, Mercury supplies bank activity and receipts, and a Cloud Storage record bucket retains closed-quarter records. Machines draft; humans post. ERPNext refuses the bot’s attempts to submit entries that commit money. Every command that touches the ledger requires `--target test` for a disposable site or `--target books` for the real ledger.

The recurring work is to sync the bank, reconcile transactions, check health, and close each quarter into a retained record.

## Early software

runningtab is at version 0.1.0, a pre-release. The Mercury webhook receiver is not built yet.

## What you need

You need Python 3.14, an ERPNext site and a disposable test site, a Mercury account, Google Cloud Secret Manager for credentials, and Cloud Storage for records.

Install with:

```sh
pip install --pre runningtab
```

## Read next

- [Tutorial: your chart in a test ledger](tutorial.md)
- [Why write the chart down](explanation/why-a-written-chart.md)
- [How it works](explanation/how-it-works.md)
- How-to: [set up a site](how-to/set-up.md), [daily operations](how-to/daily.md), [close a quarter](how-to/close-a-quarter.md)
- Reference: [the books directory](reference/books-directory.md), and `runningtab <command> --help` for every command
