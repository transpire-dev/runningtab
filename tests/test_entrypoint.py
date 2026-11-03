"""The `runningtab` command exists and runs the CLI.

Deployments call `uv run runningtab ...`, and the
rest of the suite imports `runningtab.cli` directly, so a console script that
names the wrong command passes every other test and only fails in the pipeline.
"""

from importlib.metadata import entry_points

from runningtab.cli import app


def test_the_runningtab_command_runs_the_cli() -> None:
    (script,) = entry_points(group="console_scripts", name="runningtab")
    assert script.load() is app
