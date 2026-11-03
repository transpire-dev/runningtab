"""A quarter's books as a package an accountant or auditor can work from.

Built from one read of the ledger by pure functions, so every file agrees with
every other and a rebuild of the same quarter reproduces the same bytes.
"""


class PeriodError(ValueError):
    """The package cannot be built as asked. The message names every offending item."""
