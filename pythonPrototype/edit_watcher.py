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
from pathlib import Path

from edit_watcher_tui import run as run_tui

AGENT_FILTER = "both"  # always correlate against every supported agent (Claude, Antigravity, Codex)


def main():
    """Watches ROOT for file changes and attributes each one to an agent session/prompt, live in a TUI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, nargs="?", default=None,
                         help="directory to watch for edits (default: current directory)")
    parser.add_argument("--cwd", default=None, help="agent cwd whose sessions to tail (default: root)")
    args = parser.parse_args()

    root = (args.root or Path.cwd()).resolve()    # directory whose files we watch for changes
    cwd = args.cwd or str(root)    # which project's sessions to correlate against
    run_tui(root, cwd, AGENT_FILTER)


if __name__ == "__main__":
    main()
