"""`period export` end to end against the disposable test site. Runs only under RUNNINGTAB_LIVE=1."""

import hashlib

import pytest
from typer.testing import CliRunner

from runningtab import cli
from tests.conftest import require_live_target, write_sandbox_mercury_mapping

pytestmark = pytest.mark.live_runningtab


def _assert_export_succeeded(result, label: str) -> None:
    """Exit 0, or exit 1 for exactly the reason the CLI names: a bank difference.

    A bare `exit_code in (0, 1)` would let any other refusal -- a missing
    grant, a read that failed -- through silently as long as it happened to
    exit 1. `result.output` is captured for both branches so a failure here
    ends with a real error message rather than a KeyError or FileNotFoundError
    two lines further into the test.
    """
    assert result.exit_code in (0, 1), f"{label}: {result.output}"
    if result.exit_code == 1:
        assert "the bank and the ledger disagree" in result.output, f"{label}: {result.output}"


def test_the_fixture_quarter_exports_and_verifies(tmp_path):
    require_live_target()
    mapping_path = tmp_path / "mercury.toml"
    write_sandbox_mercury_mapping(mapping_path)
    out = tmp_path / "2026Q3-DRAFT"
    result = CliRunner().invoke(
        cli.app,
        [
            "period",
            "export",
            "--target",
            "test",
            "--quarter",
            "2026Q3",
            "--output",
            str(out),
            "--mercury-mapping",
            str(mapping_path),
        ],
    )
    _assert_export_succeeded(result, "export")
    assert (out / "MANIFEST.md").exists(), result.output
    for line in (out / "SHA256SUMS").read_text().splitlines():
        digest, path = line.split("  ", 1)
        assert hashlib.sha256((out / path).read_bytes()).hexdigest() == digest


def test_two_exports_of_one_quarter_have_identical_content(tmp_path):
    require_live_target()
    mapping_path = tmp_path / "mercury.toml"
    write_sandbox_mercury_mapping(mapping_path)
    runner = CliRunner()
    first = runner.invoke(
        cli.app,
        [
            "period",
            "export",
            "--target",
            "test",
            "--quarter",
            "2026Q3",
            "--output",
            str(tmp_path / "a"),
            "--mercury-mapping",
            str(mapping_path),
        ],
    )
    _assert_export_succeeded(first, "first export")
    second = runner.invoke(
        cli.app,
        [
            "period",
            "export",
            "--target",
            "test",
            "--quarter",
            "2026Q3",
            "--output",
            str(tmp_path / "b"),
            "--mercury-mapping",
            str(mapping_path),
        ],
    )
    _assert_export_succeeded(second, "second export")
    assert (tmp_path / "a" / "SHA256SUMS").read_bytes() == (tmp_path / "b" / "SHA256SUMS").read_bytes()
