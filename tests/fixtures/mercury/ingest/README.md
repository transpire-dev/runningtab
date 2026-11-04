# Mercury ingest fixtures: what these are, and what they are not

These are the ingest's set. Each file wraps one response in a
`scenario/method/path/status/payload` envelope, which is what
`capture_mercury_fixtures.py` writes and what `tests/conftest.py` unwraps. The
health report's cases read the bare bodies one directory up; the two sets carry
different numbers and are parsed by the same client.

**These are not live captures.** Say it first, because every other fixture set
in this repository is one and someone will reasonably assume these are too.

icedcoffee-sync's `tests/fixtures/` holds real Frappe responses, recorded by its
`tests/capture_fixtures.py` against a disposable site. The Mercury equivalent
could not be recorded: no Mercury credential was available to record them
with, and the sandbox token used to measure the API was never checked in.

So these files are **reconstructed from measurements of Mercury's API**.
Every structural fact they encode is traceable to something that document states
was observed; the ids, names, and dates are invented, because a fixture needs
them and the real ones were never written down.

## Replacing them with the real thing

One command, once a sandbox token exists:

```
MERCURY_SANDBOX_TOKEN=<sandbox token> \
  uv run python tests/capture_mercury_fixtures.py
```

It writes the same file names with real payloads. The tests read these files as
data and assert on behaviour rather than on any invented id, so they are meant
to pass unchanged against a real capture — and if one does not, the fixture was
lying about something and the test has just earned its keep.

## What each file encodes, and where the claim comes from

| file                         | encodes                                                                                                                                                                            | recorded observation behind it                                                                                                                                                                                                                                                                                                                                   |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `accounts.json`              | deposit accounts with `currentBalance`                                                                                                                                             | `/accounts` omits credit accounts entirely                                                                                                                                                                                                                                                                                                                       |
| `credit.json`                | the credit account, with its smaller schema                                                                                                                                        | `GET /credit` returns `id`, `status`, `createdAt`, `availableBalance`, `currentBalance` and nothing else                                                                                                                                                                                                                                                         |
| `transactions_checking.json` | `total` alongside the rows; a pending −10.00 and a failed −70.00 outside the balance; a `trackingNumber` on the ACH row and on no other; one `attachments` entry, on that same row | per-account reads carry `total`; `currentBalance` excludes pending and failed, measured as an 80.00 gap on one account; `trackingNumber` is a string on a real production ACH row (2026-09-08) and was never seen on a sandbox row; attachments are presigned S3 URLs whose path ends `attachment/<uuid>.<ext>`, expiring twelve hours after Mercury minted them |
| `transactions_savings.json`  | the other half of an `internalTransfer` ∓47.61                                                                                                                                     | a transfer appears as two mirrored rows, verified in sandbox at exactly ∓47.61                                                                                                                                                                                                                                                                                   |
| `transactions_credit.json`   | card spend, and the `other +70.00` mirror of the checking `other −70.00`                                                                                                           | the verified checking/credit pair came through as `kind: other`, not `internalTransfer`                                                                                                                                                                                                                                                                          |
| `events.json`                | ascending event ids, `resourceVersion`, a non-advancing replay, a `delete`                                                                                                         | dedup on event id, order on `resourceVersion`, three operation types                                                                                                                                                                                                                                                                                             |
| `events_failed_transfer.json` | a transaction created `pending` and moved to `failed` one version later, with `mergePatch` carrying the new `status` and `reasonForFailure`; the transaction is in no account's listing | **measured**: four such pairs in the production log on 2026-11-04, cashback transfers out of an account the read-only token cannot see; `GET /transaction/{id}` returned 404 and `GET /account/{id}/transaction/{id}` 404 or 403 for every account. Ids and the account name are invented; the keys, versions, paths and values are as observed |
| `transaction_reversed.json`  | a `reversed` row, deliberately outside the balance fixtures                                                                                                                        | `reversed` is in the status enum; it never occurred in the sandbox                                                                                                                                                                                                                                                                                               |
| `categories.json`            | five categories with all three visibility flags and a `page` envelope; two wear chart GL-code labels and one of those has a flag wrong                                             | the envelope and the flags are documented (`CategoriesPaginatedResponse`, `CategoryData`); the names are invented, because Mercury's ~23 defaults were never written down                                                                                                                                                                                        |

The custom categories on the transaction rows are chart GL-code labels, because
since `runningtab mercury categories apply --prune` those are the only
categories Mercury offers. A category therefore codes its row exactly as a GL
code does: the row is not `uncoded`, and an expense or member-capital one gets a
draft voucher. It still changes nothing about where the row goes — every settled
row is a Bank Transaction. `categories.json` keeps invented default names,
because it models the dropdown before that prune.

## Things the fixtures assert that nobody has measured

Listed rather than buried, because each is a place a real capture could prove us
wrong:

- **Envelope keys.** `accounts`, `transactions`, `events` as the list keys, and
  `total` on a per-account read. The design recorded the pagination _behaviour_
  and not the wrappers, so `runningtab.mercury.client` accepts a bare list or
  any of a small set of keys and raises when it recognises none — an
  unrecognised envelope must never look like an empty account.
- **`limit`/`offset` on a per-account transactions read.** The design says the
  endpoint reports `total`, which implies offset paging, but names no parameter.
- **Where `mercuryCategory` lives.** The sandbox survey counts it as a top-level
  field; the Events field set names it `categoryData.mercuryCategory`. Both are
  read, and `transactions_credit.json` exercises the nested spelling.
- **The shape of an `attachments` row.** `fileName` and `url`, and a URL path
  ending `attachment/<uuid>.<ext>`. The design recorded that attachments are
  presigned S3 URLs with a twelve-hour expiry and nothing about the field names,
  so `runningtab.mercury.receipts` keys on the UUID shape in the path rather
  than on the `attachment/` segment, and reports an attachment it cannot name
  rather than uploading one it could never recognise again.
- **Where the custom category lives — measured, and these files are wrong about
  it.** A read of the real organisation on 2026-09-21 shows `categoryData` is
  **flat**: it _is_ the category object, carrying `id`, `name` and the three
  visibility flags, with no `customCategory` wrapper. These files nest it, so
  `custom_category` parsed as `None` on every real row while the fixtures agreed
  with themselves. `models._parse_categories` now reads both spellings, nested
  preferred, and `test_mercury_models.py` covers each; replacing these files
  with a real capture is what closes it properly. Nothing routes on the
  category, so what it cost was a wrong hint rather than a wrong posting — but
  it is the clearest instance yet of what this file warns about at the top.
- **Whether a `reversed` row counts toward `currentBalance`.** Only pending and
  failed were ever observed outside it. This is why the reversed row lives in
  its own file and not in a balance fixture.
