#!/usr/bin/env python3
"""Hook octo installs into each agent's config (see hook_installer.py). PreToolUse/BeforeTool
carries no real gating logic yet -- always approves, so it can never block a genuine write.
Stop is the turn-boundary signal the sync sequence hooks into: it notifies the octo process
watching this project (worktree-lane via read_owner_marker, root-lane via find_matching_session),
then always exits 0 too -- it is a pure observer, never blocking/re-prompting the agent (that's
the doc's separately-scoped, not-yet-implemented conflict-delegation path)."""

import json
import sys
from pathlib import Path

from session_registry import find_matching_session, notify_turn_ended, notify_sync
from worktree_manager import read_owner_marker

DEFAULT_AGENT = "Claude"  # only agent with confirmed Stop hooks today; Codex/Antigravity Stop events are unverified


def main():
    """Consumes the hook's JSON payload from stdin, dispatches on hook_event_name, and exits 0
    unconditionally unless UserPromptSubmit's sync fails, in which case it blocks the prompt."""
    payload = json.loads(sys.stdin.read())
    event_name = payload.get("hook_event_name")
    if event_name == "Stop" and not payload.get("stop_hook_active", False):
        _signal_turn_end(payload)
        sys.exit(0)
    elif event_name == "UserPromptSubmit":
        _handle_prompt_submit(payload)
        sys.exit(0)
    sys.exit(0)  # always approve/allow for other events like PreToolUse


def _handle_prompt_submit(payload: dict):
    """Fires at prompt submission: syncs the worktree branch from the root's shadow repo
    before the agent begins thinking/planning. If the sync conflicts, returns a block decision
    to Claude Code so it pauses/aborts the prompt turn."""
    cwd = Path(payload["cwd"])
    owner = read_owner_marker(cwd)
    if owner is None:
        # Not a worktree (root project lane). We don't down-sync root, so just approve.
        print(json.dumps({"decision": "approve"}))
        sys.exit(0)

    from shadow_repo import SHADOW_DIR_NAME
    from worktree_sync import down_sync

    shadow_git_dir = Path(owner["root"]) / SHADOW_DIR_NAME

    # Perform the down-sync synchronously
    result = down_sync(cwd, shadow_git_dir)

    # Notify the TUI of the sync event
    notify_sync(owner["pid"], cwd, DEFAULT_AGENT, result.ok, result.conflicted, result.detail)

    if not result.ok:
        # Block Claude from proceeding
        print(json.dumps({"decision": "block", "reason": f"Octo sync failed: {result.detail}"}))
    else:
        # Approve and let Claude proceed
        print(json.dumps({"decision": "approve"}))
    sys.exit(0)


def _signal_turn_end(payload: dict):
    """Resolves which live octo process to notify: worktree-lane via the clone's owner marker,
    root-lane via find_matching_session (agent running directly in the watched project).
    No-op if neither path resolves -- the hook fired in a session octo isn't tracking."""
    cwd = Path(payload["cwd"])
    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")
    agent_name = DEFAULT_AGENT

    # Path 1: worktree lane (existing) -- clone carries an owner marker
    owner = read_owner_marker(cwd)
    if owner is not None:
        notify_turn_ended(owner["pid"], cwd, session_id, transcript_path, agent_name)
        return

    # Path 2: root lane -- agent running directly in the watched project
    session = find_matching_session(cwd)
    if session is not None:
        # worktree_path = Path("") signals "this is root-lane, not a worktree"
        notify_turn_ended(session.pid, Path(""), session_id, transcript_path, agent_name)


if __name__ == "__main__":
    main()
