#!/usr/bin/env python3
"""Desktop notifications for agent state changes -- octo's herder-style "your agent needs you"
signal, fired from the short-lived hook process (see octo_hook.py) straight to notify-send.

Claude Code is the only agent wired today, but the design is the extension seam for the rest:
raw per-agent hook vocabulary is normalized into AgentState here (classify_claude_notification),
NOTIFY_STATES decides which states actually pop, and notify_agent_state fans out to sinks --
so adding an agent, a state, or a sink is a local edit here, never a reshape of the callers."""

import shutil
import subprocess
from enum import Enum


class AgentState(Enum):
    """Agent lifecycle states, normalized across agents so a sink never sees an agent's raw
    hook vocabulary. Only the members in NOTIFY_STATES actually raise a notification; the rest
    are modeled now so enabling them later is a one-line change to NOTIFY_STATES."""
    NEEDS_PERMISSION = "needs_permission"  # agent is blocked awaiting the human's tool approval
    TURN_COMPLETE = "turn_complete"        # agent finished its turn (Stop hook)
    WAITING_INPUT = "waiting_input"        # agent idle awaiting a prompt (~60s idle Notification) -- modeled, not notified
    WORKING = "working"                    # agent started a turn/tool -- modeled, not notified


# Which states actually pop a desktop notification -- the user-selected scope. Enabling
# WAITING_INPUT/WORKING later is just adding it here (plus wiring its triggering hook event).
NOTIFY_STATES = frozenset({AgentState.NEEDS_PERMISSION, AgentState.TURN_COMPLETE})

# Per-state predicate text used to build the notification body, keyed by state.
_STATE_PHRASES = {
    AgentState.NEEDS_PERMISSION: "needs your permission",
    AgentState.TURN_COMPLETE: "finished its turn",
    AgentState.WAITING_INPUT: "is waiting for your input",
    AgentState.WORKING: "started working",
}

# Substring (lowercased) that marks Claude's idle "your turn" Notification apart from its
# permission-request Notification -- the two share the hook event, differing only in message text.
_CLAUDE_IDLE_MARKER = "waiting for your input"


def classify_claude_notification(message: str) -> AgentState:
    """Maps a Claude Notification hook's `message` text to an AgentState: idle-waiting when the
    message reads like the ~60s "waiting for your input" case, else a tool permission request.
    The per-agent name is deliberate -- classify_codex_notification etc. slot in beside it."""
    if _CLAUDE_IDLE_MARKER in message.lower():
        return AgentState.WAITING_INPUT
    return AgentState.NEEDS_PERMISSION


def notify_agent_state(agent_name: str, state: AgentState, detail: str = "") -> None:
    """Public entry: raises a desktop notification for agent_name entering state, unless state
    isn't in NOTIFY_STATES (then a no-op -- how idle/working stay silent). detail is an optional
    trailing context string (e.g. the project name) so multi-project users can tell agents apart.
    Add a second sink (TUI toast, mobile push) by dispatching it alongside _send_desktop here."""
    assert isinstance(state, AgentState), "notify_agent_state expects a normalized AgentState"
    if state not in NOTIFY_STATES:
        return
    title = f"octo · {agent_name}"                       # app-style header line: "octo · Claude"
    body = f"{agent_name} {_STATE_PHRASES[state]}"            # e.g. "Claude needs your permission"
    if detail:
        body = f"{body} — {detail}"                      # append " — <project>" when a context string is given
    _send_desktop(title, body)


def _send_desktop(title: str, body: str) -> None:
    """The only sink today: a libnotify desktop popup via notify-send. Absence of notify-send is a
    capability gap, not an error, and this runs inside the agent's hook process which must never
    break the agent's turn -- so a missing binary no-ops rather than raising (see octo_hook.py's
    'pure observer' contract). -a tags the notification with octo's app name for grouping."""
    if shutil.which("notify-send") is None:
        return
    subprocess.run(["notify-send", "-a", "octo", title, body], check=False)
