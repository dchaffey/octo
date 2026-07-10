#!/usr/bin/env python3
"""Watches a directory for file changes and attributes each one to the Claude Code,
Antigravity, or Codex CLI session/prompt that produced it, using each agent's own
on-disk session records as the sole source of truth, rendered live in a Textual UI.
Every settled disk write is committed immediately as "Human" (see ShadowGitWatcher.commit_dirty);
an edit is only ever relabeled to an agent once its transcript logs the exact content written,
via a git-notes annotation attached after the fact (see ShadowGitWatcher.attribute) -- there is
no fs-based fallback that guesses at agent authorship up front. No file locking or interception
is attempted here -- this only observes and reports."""

import argparse
import sys
from pathlib import Path

import agent_launcher
import octo_hook
from octo_tui import run as run_tui
from session_registry import register, unregister

AGENT_FILTER = "both"  # always correlate against every supported agent (Claude, Antigravity, Codex)

# argv[1] -> handler, for subcommands dispatched before the watch-mode parser below. `run` is
# user-facing; `_hook` is only ever invoked by octo's own installed hook configs (see
# hook_installer.py) -- routing it through this binary itself (rather than shelling out to a
# script path via sys.executable) is what makes it work identically whether running under a plain
# `python3 octo.py` or a PyInstaller-frozen `octo` binary, where sys.executable is the frozen
# binary itself and can't run an arbitrary .py file by path.
SUBCOMMANDS = {
    "run": lambda argv: agent_launcher.main(argv),
    "_hook": lambda argv: octo_hook.main(),
}


def main():
    """Watches ROOT for file changes and attributes each one to an agent session/prompt, live in a TUI.
    Subcommands in SUBCOMMANDS are dispatched separately, before the watch-mode parser below, so they
    can't be confused with the positional `root` argument (e.g. a project literally named "run")."""
    if len(sys.argv) > 1 and sys.argv[1] in SUBCOMMANDS:
        SUBCOMMANDS[sys.argv[1]](sys.argv[2:])
        return

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, nargs="?", default=None,
                         help="directory to watch for edits (default: current directory)")
    parser.add_argument("--cwd", default=None, help="agent cwd whose sessions to tail (default: root)")
    args = parser.parse_args()

    root = (args.root or Path.cwd()).resolve()    # directory whose files we watch for changes
    cwd = args.cwd or str(root)    # which project's sessions to correlate against
    registry_path = register(root)  # advertises this process as a live octo session watching root, for `octo run` invocations to detect
    try:
        run_tui(root, cwd, AGENT_FILTER)
    finally:
        unregister(registry_path)  # only reached on clean exit/exception -- a SIGKILL leaves a stale entry, cleaned up later by find_matching_session's liveness check


if __name__ == "__main__":
    main()
