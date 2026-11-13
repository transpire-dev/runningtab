# Your chart in a test ledger

This tutorial takes you from an example books directory to a chart of accounts applied in a disposable ERPNext site. You will clear the stock chart, apply your chart, check the result in ERPNext, and make one change of your own.

Use a site you can wipe. The teardown step deletes or disables accounts from the site's existing chart. You do not need Mercury or Google Cloud for this tutorial.

## Before you start

You need:

- Python 3.14.
- `runningtab` installed with `pip install --pre runningtab`.
- A disposable ERPNext site with one Company.
- An API key and secret for an administrator user on that site. In ERPNext, open **User > API Access > Generate Keys**.

For instructions on creating a disposable ERPNext site, see [GAVIN: preferred link for getting a disposable ERPNext site].

Keep the site's URL, API key, and API secret available for the environment setup below.

## 1. Copy the example books directory

Copy the contents of [`examples/books/`](https://github.com/transpire-dev/runningtab/tree/main/examples/books) from the repository into a directory of your own, such as `my-books/`.

Your copy includes `books.toml`, which holds the configuration, and `chart.coa`, which describes the chart you will apply. Start with the example chart so you can complete the first application before editing it.

The commands below assume `my-books/` is in your current directory.

## 2. Allow your test site's host

Open `my-books/books.toml`. Find `test_hosts` in the `[site]` section and set it to a list containing your test site's host:

```toml
test_hosts = ["your-test-host"]
```

Replace `your-test-host` with the host from your site's URL. For a URL of `https://your-test-host`, the list entry is `your-test-host`.

The test target checks this allowlist and refuses any host absent from it. Check that you have entered the disposable site's host before continuing.

## 3. Set the directory and credentials

In your terminal, export these variables, replacing the placeholder values:

```sh
export RUNNINGTAB_BOOKS=my-books
export RUNNINGTAB_TEST_URL=https://your-test-host
export RUNNINGTAB_TEST_KEY='your-api-key'
export RUNNINGTAB_TEST_SECRET='your-api-secret'
```

`RUNNINGTAB_BOOKS` tells `runningtab` which books directory to use. You can also select the directory by passing `--books my-books` before the command:

```sh
runningtab --books my-books coa plan --target test
```

With `RUNNINGTAB_TEST_URL` set, all three test credential variables are required. This configuration reads no stored secret, so no Google Cloud setup is involved.

Keep using this terminal for the remaining commands so the exported variables stay available. Each command explicitly names `--target test`; there is no default target.

## 4. Preview the chart

Run:

```sh
runningtab coa plan --target test
```

This compares `chart.coa` with the site and describes what would change. It writes nothing.

On a fresh ERPNext site, expect refusals and an exit status of `1`. ERPNext's stock chart collides with the example chart on some accounts. For example, the site may have an ungrouped Retained Earnings account where the written chart requires a group.

Read the plan before proceeding. These initial collisions are why this tutorial includes a teardown step.

## 5. Clear the stock chart

!!! warning
    This step changes the existing chart destructively. Confirm that your configured URL points to the disposable site you can wipe.

Run:

```sh
runningtab coa prune --target test --allow-unapplied
```

The command prompts before proceeding. Read the prompt and confirm when you are ready. The `--yes` flag skips that prompt.

`prune` deletes accounts the chart does not describe, or disables them where ERPNext refuses deletion. With `--allow-unapplied`, it also clears Company defaults pointing at accounts it deletes and moves surviving accounts that hold chart numbers onto 9-prefixed numbers. This frees those numbers for the chart you are about to apply.

Ordinary pruning requires the chart to be applied first. The `--allow-unapplied` flag provides this teardown path for a pre-existing chart with colliding numbers.

## 6. Apply your chart

Run:

```sh
runningtab coa apply --target test
```

Review the prompt and confirm. After the prompt, `runningtab` plans again and refuses to proceed if anything changed meanwhile.

The command creates, renumbers, retypes, and re-enables accounts as needed. It then sets the Company's default-account fields to accounts in your chart. Before touching any account, it checks that those default fields exist on Company.

The result reports the counts of account changes and the Company defaults that were set.

## 7. Retire the remaining accounts

Now run ordinary pruning:

```sh
runningtab coa prune --target test
```

Review and confirm the prompt. This retires accounts left behind by the teardown. Applying the chart first has pointed the Company defaults at your chart's accounts.

## 8. Check the finished chart

Run the plan again:

```sh
runningtab coa plan --target test
```

The expected result is:

```text
site already matches the chart
```

Open **Chart of Accounts** in ERPNext and inspect the accounts. You now have the example chart applied in your disposable ledger.

## 9. Make a change of your own

Open `my-books/chart.coa` and add an account, following the structure of the existing entries. See [`chart.coa` in the books directory reference](reference/books-directory.md) for the format.

Preview your edit:

```sh
runningtab coa plan --target test
```

Look for the one-line change describing your added account. Then apply it:

```sh
runningtab coa apply --target test
```

Confirm the prompt and check the new account in ERPNext.

To continue, read [why write the chart down](explanation/why-a-written-chart.md), follow the [new-site guide](how-to/new-site.md), or explore the [books directory reference](reference/books-directory.md).