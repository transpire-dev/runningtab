"""Capture real Mercury sandbox responses as fixtures. Run by hand, not by pytest.

Usage:
    MERCURY_SANDBOX_TOKEN=<sandbox token> uv run python tests/capture_mercury_fixtures.py

Writes one file per scenario into tests/fixtures/mercury/ingest/, overwriting the
reconstructed ones that are there now (see that directory's README for why they
are reconstructed). Nothing is normalised on the way in beyond re-serialising
the JSON so the diff is reviewable.

Two properties are structural rather than careful:

- **Sandbox only.** The base URL is Mercury's sandbox host, and there is no flag
  to point this at production. A capture script that can reach the real bank is
  a capture script that eventually does.
- **Read-only.** Every request is a GET, and the client it uses cannot send
  anything else.

Account and category ids from a sandbox are not secrets, but the token is:
nothing here writes the token into a fixture, and the payloads are the sandbox's
seeded data rather than anyone's real banking.
"""

import json
import os
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

from runningtab.mercury.client import MercuryClient, MercuryEnv, config_for

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "mercury" / "ingest"


def _row_count(payload: Any) -> int:
    """How many rows a captured envelope holds, for the progress line only."""
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                return len(value)
    return 0


def _write(name: str, method: str, path: str, payload: Any) -> None:
    body = {"scenario": name, "method": method, "path": path, "status": 200, "payload": payload}
    (FIXTURES / f"{name}.json").write_text(json.dumps(body, indent=2) + "\n")
    print(f"{name:28} {_row_count(payload)} rows")


def main() -> None:
    token = os.environ.get("MERCURY_SANDBOX_TOKEN")
    if not token:
        sys.exit("MERCURY_SANDBOX_TOKEN is not set; this script only ever reads the sandbox")
    config = config_for(MercuryEnv.SANDBOX, token)
    FIXTURES.mkdir(parents=True, exist_ok=True)

    with MercuryClient(config) as client:
        # Raw payloads, not parsed models: a fixture that has been through the
        # parser can no longer disprove the parser.
        accounts = client._get("/accounts")
        _write("accounts", "GET", "/accounts", accounts)
        _write("credit", "GET", "/credit", client._get("/credit"))
        _write("events", "GET", "/events", client._get("/events", {"limit": 200}))
        _write("categories", "GET", "/categories", client._get("/categories", {"limit": 1000, "order": "asc"}))

        for account in client.accounts():
            path = f"/account/{account.id}/transactions"
            payload = client._get(path, {"limit": 500, "offset": 0})
            _write(f"transactions_{account.id}", "GET", path, payload)
        for credit in client.credit_accounts():
            path = f"/account/{credit.id}/transactions"
            payload = client._get(path, {"limit": 500, "offset": 0})
            _write(f"transactions_{credit.id}", "GET", path, payload)

    print(
        "\nCaptured. The reconstructed fixtures used different account ids, so rename the new "
        "transactions_* files (or update the tests' fixture names) and re-read the README's "
        "'things nobody has measured' list against what actually arrived."
    )


if __name__ == "__main__":
    main()
