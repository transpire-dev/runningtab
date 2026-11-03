"""Doctype names the access policy needs, kept import-free.

`runningtab.access` must name the Cash Forecast doctype, but `runningtab.forecast.doctype`
imports the ledger code, which imports `runningtab.access`. Holding the name here
breaks that cycle; `runningtab.forecast.doctype` re-exports it.
"""

CASH_FORECAST = "Cash Forecast"
