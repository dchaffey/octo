#!/usr/bin/env python3
"""Hook octo installs into each agent's config (see hook_installer.py). PreToolUse/BeforeTool
carries no real gating logic yet -- always approves, so it can never block a genuine write.
Stop is the turn-boundary signal worktree_sync.py's sync sequence hooks into: it notifies the
octo process that owns the worktree this session ran in (see worktree_manager.read_owner_marker),
then always exits 0 too -- it is a pure observer, never blocking/re-prompting the agent (that's
the doc's separately-scoped, not-yet-implemented conflict-delegation path)."""

import json
import sys
from pathlib import Path

from session_registry import notify_turn_ended
from worktree_manager import read_owner_marker


def main():
    """Consumes the hook's JSON payload from stdin, dispatches on hook_event_name, and exits 0
    unconditionally -- neither event type here can ever deny a write or block a turn from ending."""
    payload = json.loads(sys.stdin.read())
    if payload.get("hook_event_name") == "Stop" and not payload.get("stop_hook_active", False):
        _signal_turn_end(payload)
    sys.exit(0)  # always approve/allow -- no gating or blocking logic yet, for either event type


def _signal_turn_end(payload: dict):
    """Resolves which live octo process owns the worktree this Stop event fired in (via its
    cwd, the only thing this hook subprocess knows about its own context) and notifies it, so
    that process's next poll tick runs the turn-boundary sync sequence for this worktree.
    No-op if cwd carries no owner marker -- e.g. the hook fired in a plain Claude session that
    isn't one of octo's managed worktrees at all."""
    cwd = Path(payload["cwd"])
    owner = read_owner_marker(cwd)
    if owner is None:
        return
    notify_turn_ended(owner["pid"], cwd, payload.get("session_id", ""), payload.get("transcript_path", ""))


if __name__ == "__main__":
    main()
