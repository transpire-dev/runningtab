"""The Mercury half of the books integration: read the bank, post its statement lines into the ledger.

Read-only against Mercury: `client.py` performs no verb but GET. Against ERPNext
every settled transaction becomes a submitted Bank Transaction, the one document
this package submits; a Bank Transaction reaches the general ledger only when a
person reconciles it.
"""
