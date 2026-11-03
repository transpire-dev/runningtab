# Chart of accounts

The example books' chart. The tables between each pair of `chart:` markers are
generated from `chart.coa` by `runningtab chart render-doc`; edit the chart, not
the tables.

## Assets (1000)

<!-- chart:table 1000 -->

| Number | Account                       | Type                     | What goes here                                                                                                                                               |
| ------ | ----------------------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1000   | Assets                        | Group                    | Root.                                                                                                                                                        |
| 1100   | Current Assets                | Group                    |                                                                                                                                                              |
| 1110   | Bank Accounts                 | Group                    |                                                                                                                                                              |
| 1111   | Operating Checking            | Bank                     | The account the business runs through. Customer payments land here and most bills are paid from here.                                                        |
| 1112   | Reserve Savings               | Bank                     | Cash set aside on purpose: the tax reserve and a few months of runway. Kept apart from 1111 so "cash on hand" and "cash already spoken for" read separately. |
| 1120   | Cash In Hand                  | Group                    |                                                                                                                                                              |
| 1121   | Petty Cash                    | Cash                     | Physical cash. Should stay near zero; a growing balance usually means something was miscoded.                                                                |
| 1130   | Accounts Receivable           | Group                    |                                                                                                                                                              |
| 1131   | Accounts Receivable           | Receivable               | Invoiced and not yet paid. Posted when a sales invoice is submitted; never journal into it by hand.                                                          |
| 1140   | Short-term Loans and Advances | Group                    |                                                                                                                                                              |
| 1141   | Prepaid Expenses              |                          | Paid now, used up later: annual subscriptions, prepaid insurance. ERPNext releases deferred expense from here as it amortises.                               |
| 1143   | Employee Advances             |                          | Money advanced to a person against expenses they have not yet submitted. Cleared when the expense claim arrives.                                             |
| 1200   | Fixed Assets                  | Group                    |                                                                                                                                                              |
| 1210   | Computers & Equipment         | Fixed Asset              | Laptops, test devices and lab equipment above the capitalisation threshold. Anything under it is 5208.                                                       |
| 1220   | Vehicles                      | Fixed Asset              | The company van. Running costs are 5222.                                                                                                                     |
| 1230   | Accumulated Depreciation      | Accumulated Depreciation | Contra-asset against 1210 and 1220; the expense side is 5203.                                                                                                |
| 1300   | Temporary Accounts            | Group                    |                                                                                                                                                              |
| 1310   | Temporary Opening (Migration) | Temporary                | ERPNext's scratch account for opening entries. A balance here means a migration was left half-done.                                                          |

<!-- chart:end -->

## Liabilities (2000)

<!-- chart:table 2000 -->

| Number | Account                              | Type    | What goes here                                                                                                                                                                                           |
| ------ | ------------------------------------ | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 2000   | Liabilities                          | Group   | Root.                                                                                                                                                                                                    |
| 2100   | Current Liabilities                  | Group   |                                                                                                                                                                                                          |
| 2110   | Accounts Payable                     | Group   |                                                                                                                                                                                                          |
| 2111   | Accounts Payable                     | Payable | Bills received and not yet paid. Posted when a purchase invoice is submitted.                                                                                                                            |
| 2120   | Credit Cards                         | Group   |                                                                                                                                                                                                          |
| 2121   | Business Credit Card                 | Bank    | Card balance owed. **Typed `Bank` on purpose**: ERPNext has no credit card account type, and `Bank` is what lets the account link to a Bank Account and appear in reconciliation. Do not "correct" this. |
| 2130   | Payroll Liabilities                  | Group   |                                                                                                                                                                                                          |
| 2131   | Net Pay Payable                      |         | Pay owed to people between the payroll run (Gusto) and the money leaving the bank.                                                                                                                       |
| 2132   | Payroll Taxes Payable                |         | Withheld and employer payroll taxes, federal and state, not yet remitted.                                                                                                                                |
| 2140   | Tax Liabilities                      | Group   |                                                                                                                                                                                                          |
| 2141   | Sales Tax Payable                    | Tax     | Sales tax collected for a state and not yet remitted. Held money, never revenue.                                                                                                                         |
| 2142   | State Income & Franchise Tax Payable | Tax     | Accrued state tax before it is paid. The expense side is 5212.                                                                                                                                           |
| 2150   | Accrued & Deferred Liabilities       | Group   |                                                                                                                                                                                                          |
| 2151   | Accrued Expenses                     |         | Costs incurred but not yet billed, so a month's numbers are honest when an invoice arrives late.                                                                                                         |
| 2152   | Deferred Revenue                     |         | Cash collected for work not yet delivered: the unearned part of an annual plan. ERPNext releases it to revenue over the term.                                                                            |
| 2200   | Non-Current Liabilities              | Group   |                                                                                                                                                                                                          |
| 2210   | Equipment Loan                       |         | The note financing 1220. Interest is 5223; only principal moves this balance.                                                                                                                            |
| 2220   | Member Loans                         |         | Money a member lends the company and expects back. A **contribution** is equity (3100); a **loan** lives here.                                                                                           |

<!-- chart:end -->

## Equity (3000)

<!-- chart:table 3000 -->

| Number | Account                         | Type  | What goes here                                                                       |
| ------ | ------------------------------- | ----- | ------------------------------------------------------------------------------------ |
| 3000   | Equity                          | Group | Root.                                                                                |
| 3100   | Member Capital Contributions    | Group | Money put in and not expected back as a debt.                                        |
| 3110   | Capital Contribution – Member 1 |       | Per member, because an LLC tracks each member's capital account.                     |
| 3120   | Capital Contribution – Member 2 |       |                                                                                      |
| 3130   | Capital Contribution – Member 3 |       |                                                                                      |
| 3200   | Member Distributions            | Group | Money taken out. Not an expense and not payroll.                                     |
| 3210   | Distributions – Member 1        |       |                                                                                      |
| 3220   | Distributions – Member 2        |       |                                                                                      |
| 3230   | Distributions – Member 3        |       |                                                                                      |
| 3300   | Retained Earnings               | Group |                                                                                      |
| 3310   | Opening Balance Equity          |       | Balances opening entries. Returns to zero once they are complete.                    |
| 3320   | Retained Earnings               |       | Prior periods' profit and loss. Closed into automatically; do not post here by hand. |

<!-- chart:end -->

## Revenue (4000)

<!-- chart:table 4000 -->

| Number | Account                  | Type  | What goes here                                                                                                                                    |
| ------ | ------------------------ | ----- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| 4000   | Revenue / Income         | Group | Root.                                                                                                                                             |
| 4100   | Operating Revenue        | Group |                                                                                                                                                   |
| 4110   | Project Services Revenue |       | Fixed-scope work billed against a statement of work. The default income account, so an invoice with no more specific account lands here.          |
| 4120   | Subscription Revenue     |       | Recurring plans. The number that describes the business.                                                                                          |
| 4130   | Training & Workshops     |       | Paid sessions and course seats. Separate from 4110 because it scales with seats, not with scope.                                                  |
| 4140   | Hardware Resale          |       | Devices bought and resold to clients at a margin. The cost side is 5140.                                                                          |
| 4200   | Other Income             | Group |                                                                                                                                                   |
| 4210   | Interest Income          |       | Interest on 1111 and 1112. Kept out of 4100 so the top line is operating revenue only. Card cashback is a rebate, credited to 5202, not interest. |
| 4220   | Miscellaneous Income     |       | Refunds of prior-year costs, small one-off receipts.                                                                                              |

<!-- chart:end -->

## Expenses (5000)

<!-- chart:table 5000 -->

| Number | Account                                               | Type         | What goes here                                                                                                                                                                                                                                       |
| ------ | ----------------------------------------------------- | ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 5000   | Expenses                                              | Group        | Root.                                                                                                                                                                                                                                                |
| 5100   | Cost of Revenue                                       | Group        |                                                                                                                                                                                                                                                      |
| 5110   | Production Hosting (AWS, Fastly)                      |              | Infrastructure that serves customers. The default expense account. Development and staging are 5150.                                                                                                                                                 |
| 5120   | Subcontractors                                        |              | Outside people doing billable client work. Contractors on internal work go to the 5200 account for what they did.                                                                                                                                    |
| 5130   | Messaging & Delivery APIs (Twilio, Postmark)          |              | Metered services in the product's path: cost that rises with usage. Tools the team works in are 5204 or 5219.                                                                                                                                        |
| 5140   | Cost of Hardware Sold                                 |              | What the devices in 4140 cost us.                                                                                                                                                                                                                    |
| 5150   | Development & Staging Hosting (Google Cloud, Hetzner) |              | Non-production environments, CI runners and the secrets and storage the books tooling itself uses. Split from 5110 because it serves nobody, and mixing the two overstates cost of revenue.                                                          |
| 5200   | Operating Expenses                                    | Group        |                                                                                                                                                                                                                                                      |
| 5201   | Marketing & Events                                    |              | Paid ads, sponsorships, booth fees.                                                                                                                                                                                                                  |
| 5202   | Bank & Payment Processing Fees (Mercury, Stripe)      |              | Wire fees, card processing, payout fees. The cost of moving money. Card cashback is credited here: a rebate lowers an expense.                                                                                                                       |
| 5203   | Depreciation Expense                                  | Depreciation | The expense side of 1230. Posted by the depreciation schedule.                                                                                                                                                                                       |
| 5204   | Office Software (Microsoft 365, Slack, 1Password)     |              | Tools that run the company: mail and documents, chat, passwords. If it is in the product's path it is 5130; if the team builds the product with it, 5219.                                                                                            |
| 5205   | Insurance (Business Owner's Policy, Cyber)            |              | Premiums for the business owner's policy and the cyber rider.                                                                                                                                                                                        |
| 5206   | Legal & Accounting                                    |              | Counsel, the outside accountant, the QuickBooks Online seat the accountant works in. Formation costs land here.                                                                                                                                      |
| 5207   | Meals & Entertainment                                 |              | Its own account because tax treatment differs. Keep the business purpose and attendees with the receipt.                                                                                                                                             |
| 5208   | Small Equipment under $2,500                          |              | Below the capitalisation threshold, expensed now. Above it, 1210.                                                                                                                                                                                    |
| 5209   | Coworking Space                                       |              |                                                                                                                                                                                                                                                      |
| 5210   | Salaries & Wages                                      |              | Everyone on payroll.                                                                                                                                                                                                                                 |
| 5211   | Employer Payroll Taxes                                |              | The employer's own share. Amounts withheld from people are a liability (2132), not an expense.                                                                                                                                                       |
| 5212   | State Taxes & Annual Fees                             |              | State income and franchise tax, annual report fees, the registered agent. The liability side is 2142.                                                                                                                                                |
| 5215   | Phone & Internet (Verizon, Comcast)                   |              | Lines and connectivity for wherever the work happens. A domain or CDN bought to serve the product is 5110.                                                                                                                                           |
| 5216   | Travel, Training & Conferences                        |              | Airfare, lodging, conference tickets, and the courses and books people learn from. Meals while travelling go to 5207.                                                                                                                                |
| 5217   | Round Off Expense                                     | Round Off    | Sub-cent rounding. ERPNext posts here automatically; a meaningful balance means something upstream is wrong.                                                                                                                                         |
| 5218   | Foreign Exchange Gain / Loss                          |              | The difference between the rate a foreign-currency bill was booked at and the rate it settled at. Apart from 5217 because ERPNext posts the two through different settings.                                                                          |
| 5219   | Engineering Tools (GitHub, JetBrains, Linear)         |              | What the team builds the product with: source control, editors, issue tracking. Split from 5204 so R&D credit work never has to untangle it later.                                                                                                   |
| 5220   | Write-Offs & Bad Debt                                 |              | Receivables judged uncollectable, and small balances written off to close a document.                                                                                                                                                                |
| 5221   | Gain / Loss on Asset Disposal                         |              | The difference between an asset's book value and what selling or scrapping it brought in. Posted by the asset disposal flow.                                                                                                                         |
| 5222   | Vehicle Expenses                                      |              | Fuel, tolls, parking and maintenance for 1220.                                                                                                                                                                                                       |
| 5223   | Interest Expense                                      |              | Interest on 2210. Posted from the loan schedule.                                                                                                                                                                                                     |
| 5299   | UNCATEGORISED (needs review)                          |              | **Suspense.** Where an automated import puts a transaction it could not classify, with the raw category in the remarks. Named to be uncomfortable: anything here is unfinished bookkeeping, and the health report ages it. Empty it; do not grow it. |

<!-- chart:end -->

## Default accounts

<!-- chart:defaults -->

| Company setting                       | Account |
| ------------------------------------- | ------- |
| Default bank account                  | 1111    |
| Default cash account                  | 1121    |
| Default receivable account            | 1131    |
| Default payable account               | 2111    |
| Default cost of goods sold account    | 5110    |
| Default income account                | 4110    |
| Round off account                     | 5217    |
| Exchange gain / loss account          | 5218    |
| Write off account                     | 5220    |
| Default deferred revenue account      | 2152    |
| Default deferred expense account      | 1141    |
| Accumulated depreciation account      | 1230    |
| Depreciation expense account          | 5203    |
| Gain / loss account on asset disposal | 5221    |

<!-- chart:end -->
