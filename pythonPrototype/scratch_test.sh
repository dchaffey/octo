#!/usr/bin/env bash
# Resets a scratch directory and launches edit_watcher.py against it, so you can manually
# drive a real agent CLI (Claude Code / Codex / Antigravity) at that directory in another
# terminal and watch how each edit gets attributed and committed to the shadow repo.
#
# Usage: ./scratch_test.sh [dir] [edit_watcher.py args...]
#   ./scratch_test.sh                       # watch ../test, all agents
#   ./scratch_test.sh ../test --agent codex # watch ../test, Codex only
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"  # this script's own directory, to locate edit_watcher.py
TEST_DIR="$SCRIPT_DIR/../test"                                # scratch directory to watch, unless overridden below

if [[ $# -gt 0 && "$1" != --* ]]; then
    TEST_DIR="$1"  # first non-flag arg overrides the default scratch directory
    shift
fi

mkdir -p "$TEST_DIR"
TEST_DIR="$(cd "$TEST_DIR" && pwd)"  # resolve to an absolute path; agent cwd matching requires one

rm -rf "$TEST_DIR/.octo"  # drop any shadow repo left over from a previous run, for a clean baseline
cat > "$TEST_DIR/main.txt" <<'EOF'
This is a single sentence in main.txt.
EOF

echo "Scratch dir ready: $TEST_DIR"
echo "Point an agent CLI's cwd at this directory in another terminal, then edit main.txt through it."
echo

exec python3 -u "$SCRIPT_DIR/edit_watcher.py" "$TEST_DIR" --diffs "$@"
