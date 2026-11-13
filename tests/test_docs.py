"""The documentation site's reference pages against the CLI they describe.

Each command is documented under a heading ``## `runningtab <path>` ``, and the
flags it takes appear in backticks within that heading's section. A command
renamed, a flag dropped, or a command added without a reference entry fails
here, so the reference cannot quietly drift from what `runningtab` does.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import typer

from runningtab.cli import app

REFERENCE = Path(__file__).parent.parent / "docs" / "reference"

_HEADING = re.compile(r"^## `runningtab ([a-z][a-z -]*)`\s*$")
_CODE_SPAN = re.compile(r"`([^`]+)`")
_FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)")


def documented_commands(text: str) -> dict[tuple[str, ...], set[str]]:
    """Each documented command path, mapped to the flags its section names."""
    sections: dict[tuple[str, ...], set[str]] = {}
    current: set[str] | None = None
    for line in text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            current = sections.setdefault(tuple(heading.group(1).split()), set())
        elif line.startswith("## "):
            current = None
        elif current is not None:
            for span in _CODE_SPAN.findall(line):
                current.update(_FLAG.findall(span))
    return sections


def _reference_commands() -> dict[tuple[str, ...], set[str]]:
    found: dict[tuple[str, ...], set[str]] = {}
    for page in sorted(REFERENCE.glob("*.md")):
        for path, flags in documented_commands(page.read_text()).items():
            found.setdefault(path, set()).update(flags)
    return found


# Typer vendors its own click, so commands and groups are told apart by shape:
# a group is a command with `.commands`.
def _cli() -> Any:
    return typer.main.get_command(app)


def _subcommands(command: Any) -> dict[str, Any] | None:
    return getattr(command, "commands", None)


def _resolve(path: tuple[str, ...]) -> Any | None:
    command = _cli()
    for name in path:
        children = _subcommands(command)
        if children is None or name not in children:
            return None
        command = children[name]
    return command


def _leaves(group: Any, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    leaves: list[tuple[str, ...]] = []
    for name, command in (_subcommands(group) or {}).items():
        if _subcommands(command) is not None:
            leaves.extend(_leaves(command, (*prefix, name)))
        else:
            leaves.append((*prefix, name))
    return leaves


def _options(command: Any) -> set[str]:
    return {opt for param in command.params for opt in (*param.opts, *param.secondary_opts) if opt.startswith("--")}


def test_finds_headings_and_their_flags() -> None:
    page = """# coa

## `runningtab coa plan`

Run `runningtab coa plan --target test`. Prose --not-a-flag outside code.
`--company NAME` is optional.

## `runningtab mercury categories apply`

`--yes` skips the prompt.

## Exit codes

`--ignored` here belongs to no command.
"""
    assert documented_commands(page) == {
        ("coa", "plan"): {"--target", "--company"},
        ("mercury", "categories", "apply"): {"--yes"},
    }


def test_every_documented_command_exists() -> None:
    unknown = [" ".join(path) for path in _reference_commands() if _resolve(path) is None]
    assert unknown == []


def test_every_documented_flag_exists() -> None:
    global_options = _options(_cli())
    wrong = {
        " ".join(path): sorted(flags - _options(command) - global_options)
        for path, flags in _reference_commands().items()
        if (command := _resolve(path)) is not None and flags - _options(command) - global_options
    }
    assert wrong == {}


@pytest.mark.xfail(strict=True, reason="the reference pages are written later in this branch")
def test_every_command_is_documented() -> None:
    documented = set(_reference_commands())
    missing = [" ".join(path) for path in _leaves(_cli()) if path not in documented]
    assert missing == []
