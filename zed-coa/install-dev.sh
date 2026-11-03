#!/usr/bin/env bash
# Build a Zed "dev extension" directory for chart.coa syntax highlighting.
#
# Why this script exists, instead of a checked-in extension.toml: Zed's
# extension.toml only lets a grammar section name a `repository` + `rev` (a
# git URL and a commit SHA) -- there is no field for pointing it at a
# subdirectory of a larger repository (confirmed against
# https://zed.dev/docs/extensions/languages). This repo's tree-sitter-coa
# grammar therefore lives as plain files under zed-coa/grammar/,
# tracked normally by this repository like everything else -- a git repository
# nested inside this one would be tracked as a broken "embedded repository"
# gitlink instead, and the actual grammar source would never get committed.
#
# So this script builds the throwaway repo Zed needs, on demand, outside
# this checkout: it copies grammar/ into a scratch directory, turns that
# copy into a fresh one-commit git repository, and renders extension.toml
# from extension.toml.in pointing at that copy's `file://` path and commit
# SHA -- both specific to this machine and moment, which is why they are
# never checked in.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: zed-coa/install-dev.sh [--help]

Builds a Zed dev-extension directory for chart.coa syntax highlighting and
prints the path to hand to Zed.

Run this:
  - the first time you want to try this extension in Zed, or
  - after editing anything under zed-coa/grammar/ or
    zed-coa/languages/ -- Zed does not watch this repo, so a dev
    extension it already loaded will not pick up a source change on its
    own. Re-run this script, then reload the dev extension in Zed.

Safe to re-run at any time: it deletes and rebuilds its scratch directory
from scratch, and prints a new grammar commit SHA every time, even if
nothing changed.

Requires, on top of git: the Rust toolchain (rustup) with the
wasm32-wasip2 target, and wasi-sdk -- Zed compiles the grammar itself when
you install the dev extension. See README.md.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GRAMMAR_SRC="$SCRIPT_DIR/grammar"
LANGUAGES_SRC="$SCRIPT_DIR/languages"
TEMPLATE="$SCRIPT_DIR/extension.toml.in"
BUILD_DIR="${TMPDIR:-/tmp}/zed-coa-dev"
GRAMMAR_BUILD="$BUILD_DIR/grammar"
EXTENSION_BUILD="$BUILD_DIR/extension"

if [[ ! -d "$GRAMMAR_SRC" ]]; then
  echo "error: $GRAMMAR_SRC not found -- run this script in place, from zed-coa/ of a runningtab checkout." >&2
  exit 1
fi
if [[ ! -d "$LANGUAGES_SRC" ]]; then
  echo "error: $LANGUAGES_SRC not found -- run this script in place, from zed-coa/ of a runningtab checkout." >&2
  exit 1
fi
if [[ ! -f "$TEMPLATE" ]]; then
  echo "error: $TEMPLATE not found -- run this script in place, from zed-coa/ of a runningtab checkout." >&2
  exit 1
fi

echo "Building dev extension in $BUILD_DIR ..."
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

cp -R "$GRAMMAR_SRC" "$GRAMMAR_BUILD"

# Zed compiles src/parser.c out of the grammar repo it clones; it does not
# run `tree-sitter generate` itself. The generated parser is ~50KB of C that
# changes wholesale on every grammar edit, so it is not checked in -- it is
# produced here, in the throwaway copy, from grammar.js.
#
# `npm ci` installs exactly what grammar/package-lock.json resolved --
# tree-sitter-cli and (should it ever gain any) every transitive dependency
# -- rather than `npx --yes tree-sitter-cli@<version>` re-resolving against
# the registry on every run with nothing pinning what a bare semver range
# pulls in underneath it. `npx tree-sitter generate` afterwards resolves the
# `tree-sitter` bin `npm ci` just installed into `node_modules/.bin/`, no
# registry lookup involved.
if ! command -v npm >/dev/null 2>&1; then
  echo "error: npm not found -- Node.js is needed to run tree-sitter generate." >&2
  exit 1
fi
if [[ ! -f "$GRAMMAR_SRC/package-lock.json" ]]; then
  echo "error: $GRAMMAR_SRC/package-lock.json not found -- run 'npm install --package-lock-only' in $GRAMMAR_SRC first." >&2
  exit 1
fi
echo "Installing tree-sitter-cli from grammar/package-lock.json ..."
(cd "$GRAMMAR_BUILD" && env npm_config_cache="$BUILD_DIR/npm-cache" npm ci)
echo "Generating the parser ..."
(cd "$GRAMMAR_BUILD" && npx tree-sitter generate)
if [[ ! -f "$GRAMMAR_BUILD/src/parser.c" ]]; then
  echo "error: tree-sitter generate produced no src/parser.c in $GRAMMAR_BUILD" >&2
  exit 1
fi

mkdir -p "$EXTENSION_BUILD"
cp -R "$LANGUAGES_SRC" "$EXTENSION_BUILD/languages"

# A throwaway, single-commit repo -- never pushed, never read back by this
# script. -c user.name/user.email so this works on a machine with no git
# identity configured; every git call is scoped to $GRAMMAR_BUILD via -C so
# this never touches the runningtab checkout this script lives in. Zed only
# needs grammar.js and the generated src/; the tree-sitter-cli install that
# produced it stays out of the repo Zed clones.
printf 'node_modules/\n' >"$GRAMMAR_BUILD/.gitignore"
git -C "$GRAMMAR_BUILD" -c user.name=zed-coa -c user.email=zed-coa@localhost init -q
git -C "$GRAMMAR_BUILD" -c user.name=zed-coa -c user.email=zed-coa@localhost add -A
git -C "$GRAMMAR_BUILD" -c user.name=zed-coa -c user.email=zed-coa@localhost commit -q -m "tree-sitter-coa dev build"
GRAMMAR_REV="$(git -C "$GRAMMAR_BUILD" rev-parse HEAD)"

sed \
  -e "s#@GRAMMAR_REPO@#file://$GRAMMAR_BUILD#" \
  -e "s#@GRAMMAR_REV@#$GRAMMAR_REV#" \
  "$TEMPLATE" >"$EXTENSION_BUILD/extension.toml"

echo "Built $EXTENSION_BUILD (grammar rev $GRAMMAR_REV)."
echo
echo "Next step: in Zed, Extensions -> Install Dev Extension -> $EXTENSION_BUILD"
