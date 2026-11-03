# Mercury fixtures

These are the bare response bodies the **health report's** cases read. The
ingest's own set lives in `ingest/`, wrapped one level deeper in a
`scenario/method/path/status/payload` envelope so a capture run can rewrite
them; see that directory's README. Two sets of numbers, both reconstructed, one
client — `runningtab.mercury.client` is what parses both.

**Provenance, stated plainly: these are not captures.** Nobody on this branch
had a Mercury token, so no response was recorded from a live account or from the
sandbox.

Each file is a response constructed to conform to Mercury's **published OpenAPI
definitions**, fetched 2026-09-01 from:

- `https://docs.mercury.com/reference/getaccounts.md` (`Account`,
  `AccountsPaginatedResponse`)
- `https://docs.mercury.com/reference/listcredit.md` (`CreditAccount`,
  `CreditAccountsResponse`)
- `https://docs.mercury.com/reference/listaccounttransactions.md`
  (`Transaction`, `TransactionsResponse`, `TransactionStatus`,
  `TransactionKind`)

Every required field named by those schemas is present, and every enum value
used is one the schema lists.

The **numbers** are not arbitrary. They reproduce the two balance discrepancies
actually measured against the Mercury sandbox: one
account off by 80.00 with a failed −70.00 and a pending −10.00, another off by
3.50 with a pending −3.50. So the arithmetic these fixtures exercise is the
arithmetic that was observed, even though the surrounding JSON was not.

The design also warns that the published reference and the real payloads
**disagree** — the docs describe `transactionCategoryData` where responses carry
`categoryData` — so a schema-derived fixture is weaker evidence than a capture.
Nothing here depends on that field, which is why schema-derived was good enough
for the balance invariant and would not be good enough for classification.

## Replace these with real captures

Once a read-only Mercury token exists, re-record these against the sandbox and
commit the raw responses in their place, unedited apart from redaction. The
tests should not need changing: they read whatever these files contain.

## The files

| File                                 | What it is                                                                 |
| ------------------------------------ | -------------------------------------------------------------------------- |
| `accounts.json`                      | `GET /accounts` — two checking accounts                                    |
| `credit.json`                        | `GET /credit` — one credit account, the shape `/accounts` omits            |
| `transactions-checking.json`         | `GET /account/{id}/transactions` — settled, failed, and pending rows       |
| `transactions-savings.json`          | the second measured discrepancy: one settled row, one pending              |
| `transactions-credit.json`           | the credit account's own rows                                              |
| `transactions-checking-unpaged.json` | the same account read without paging — the truncation the invariant caught |
