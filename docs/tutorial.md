# Your chart in a test ledger

Apply the example chart of accounts in a disposable ERPNext site, check the result, and make one change of your own. You need no Mercury or Google Cloud setup.

Use a site you can wipe: this tutorial deletes ERPNext's stock chart.

## Before you start

You need Python 3.14, a disposable ERPNext site with one Company, and an administrator user's API key and secret. Run ERPNext's setup wizard to create the Company. Generate credentials under **User > API Access > Generate Keys**.

Install `runningtab`:

```sh
pip install --pre runningtab
```

## Copy and configure the books

Copy the contents of [`examples/books/`](https://github.com/transpire-dev/runningtab/tree/main/examples/books) into `my-books/` in your current directory. It includes `books.toml` for configuration and `chart.coa` for the chart.

In `my-books/books.toml`, set `test_hosts` under `[site]` to your disposable site's host:

```toml
test_hosts = ["your-test-host"]
```

For `https://your-test-host`, the host is `your-test-host`. The test target refuses hosts absent from this list.

Export the directory and credentials, replacing the placeholders:

```sh
export RUNNINGTAB_BOOKS=my-books
export RUNNINGTAB_TEST_URL=https://your-test-host
export RUNNINGTAB_TEST_KEY='your-api-key'
export RUNNINGTAB_TEST_SECRET='your-api-secret'
```

Keep using this terminal so the variables stay available. Every command below explicitly selects the test target. For command details, use `runningtab coa --help`.

## Preview and clear the stock chart

Preview the changes:

```sh
runningtab coa plan --target test
```

The plan compares `chart.coa` with the site and writes nothing. On a fresh site, expect accounts under `REFUSED`: ERPNext's stock chart collides with the example chart.

Read the plan. Confirm that your configured URL points to the disposable site, then run:

```sh
runningtab coa clear-stock --target test
```

This deletes accounts created by ERPNext's stock chart. It refuses to run once the Company's ledger contains any entries. Review the listed accounts and confirm the prompt.

Plan again:

```sh
runningtab coa plan --target test
```

Nothing should appear under `REFUSED` now. If a refusal remains, the plan names the account; delete it in ERPNext's **Chart of Accounts** if it has no child accounts.

## Apply and check

Apply the chart:

```sh
runningtab coa apply --target test
```

Review and confirm the prompt. The command updates accounts and sets the Company's default accounts to accounts in your chart.

If the plan listed `extra` accounts, retire those leftovers:

```sh
runningtab coa prune --target test
```

This deletes accounts outside your chart or disables them where ERPNext refuses deletion.

Check the result:

```sh
runningtab coa plan --target test
```

Expect:

```text
site already matches the chart
```

Open **Chart of Accounts** in ERPNext and inspect the accounts.

## Make one change

Add one account to `my-books/chart.coa`, following the existing entries. The [books directory reference](reference/books-directory.md) describes the format.

Preview your edit:

```sh
runningtab coa plan --target test
```

Look for the change describing your added account, then apply it:

```sh
runningtab coa apply --target test
```

Confirm the prompt and check the new account in ERPNext.

Next, follow [set up](how-to/set-up.md) and read [why write the chart down](explanation/why-a-written-chart.md).