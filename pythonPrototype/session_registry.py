#!/usr/bin/env python3
"""Tracks which octo sessions are currently running and which project root each one watches, so
`octo run` (see agent_launcher.py) can tell whether the directory it was invoked from is one octo
is actively watching -- via a small per-process registry file, no live IPC needed. Also lets
`octo run` hand a newly created agent worktree (see worktree_manager.py) off to the octo process
watching it, via a per-pid inbox of pending registrations that process's own poll loop drains --
same file-based, no-live-IPC approach, just a second directory."""

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from agent_watcher import cwd_related

REGISTRY_DIR = Path.home() / ".octo" / "running"  # one JSON file per live octo process, named by its pid
PENDING_WORKTREES_DIR = Path.home() / ".octo" / "pending_worktrees"  # one subdir per octo pid, holding not-yet-drained worktree registrations
PENDING_TURN_ENDS_DIR = Path.home() / ".octo" / "pending_turn_ends"  # one subdir per octo pid, holding not-yet-drained turn-boundary notifications from octo_hook.py's Stop handler


@dataclass
class RunningSession:
    """One live octo process found in the registry."""
    root: Path  # project root that process is watching
    pid: int    # process id of the running octo process


def _registry_path(pid: int) -> Path:
    """Path to the registry file for the process with the given pid."""
    return REGISTRY_DIR / f"{pid}.json"


def register(root: Path) -> Path:
    """Records that the current process is an octo session watching root. Returns the registry
    file path so the caller can pass it to unregister() on clean shutdown."""
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    registry_path = _registry_path(os.getpid())
    registry_path.write_text(json.dumps({"root": str(root), "pid": os.getpid()}), encoding="utf-8")
    return registry_path


def unregister(registry_path: Path):
    """Removes a registry file written by register(); a no-op if it's already gone (e.g. cleaned
    up as a stale entry by find_matching_session)."""
    registry_path.unlink(missing_ok=True)


def _is_pid_alive(pid: int) -> bool:
    """True if a process with pid is currently running; checks liveness without sending a real signal."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _load_session(registry_path: Path) -> RunningSession | None:
    """Parses one registry file into a RunningSession, or None (deleting the file) if the process
    it names is no longer alive -- a stale entry left behind by a crashed octo process."""
    entry = json.loads(registry_path.read_text(encoding="utf-8"))
    if not _is_pid_alive(entry["pid"]):
        registry_path.unlink(missing_ok=True)
        return None
    return RunningSession(Path(entry["root"]), entry["pid"])


def find_matching_session(cwd: Path) -> RunningSession | None:
    """Returns the running octo session whose watched root relates to cwd (equal, or one nested
    inside the other -- see agent_watcher.cwd_related), or None if no live session matches."""
    if not REGISTRY_DIR.is_dir():
        return None
    for registry_path in REGISTRY_DIR.glob("*.json"):
        session = _load_session(registry_path)
        if session is not None and cwd_related(str(cwd), str(session.root)):
            return session
    return None


@dataclass
class WorktreeRegistration:
    """One agent worktree an `octo run` invocation created, not yet acknowledged by the octo process watching it."""
    worktree_path: Path  # absolute path to the new worktree's checkout
    branch: str           # branch name checked out there
    agent: str              # agent display name (e.g. "Claude") the worktree was created for
    created_at: float         # epoch seconds the worktree was created


def register_worktree(session: RunningSession, worktree_path: Path, branch: str, agent: str) -> Path:
    """Drops one WorktreeRegistration for session's octo process to pick up on its next poll tick
    (see drain_pending_worktrees) -- called by `octo run` right after it creates worktree_path.
    Returns the file path written."""
    pending_dir = PENDING_WORKTREES_DIR / str(session.pid)  # this octo process's own inbox, so concurrent `octo run` invocations targeting different octo processes never collide
    pending_dir.mkdir(parents=True, exist_ok=True)
    entry_path = pending_dir / f"{uuid.uuid4().hex}.json"  # unique per registration, so concurrent `octo run` invocations never clobber each other's entry
    entry_path.write_text(json.dumps({
        "worktree_path": str(worktree_path), "branch": branch, "agent": agent, "created_at": time.time(),
    }), encoding="utf-8")
    return entry_path


def drain_pending_worktrees(pid: int) -> list[WorktreeRegistration]:
    """Returns every WorktreeRegistration dropped for the octo process with the given pid since
    the last drain, deleting each entry file as it's read -- a poll_once()-friendly queue-drain,
    called once per tick from the octo process's own event loop. Empty/missing inbox returns []."""
    pending_dir = PENDING_WORKTREES_DIR / str(pid)
    if not pending_dir.is_dir():
        return []
    registrations = []
    for entry_path in sorted(pending_dir.glob("*.json")):  # sorted by filename (uuid) -- not creation-time ordered, but registrations are independent, so display order doesn't matter
        entry = json.loads(entry_path.read_text(encoding="utf-8"))
        entry_path.unlink()
        registrations.append(WorktreeRegistration(
            Path(entry["worktree_path"]), entry["branch"], entry["agent"], entry["created_at"],
        ))
    return registrations


@dataclass
class TurnEnded:
    """One agent's turn-boundary signal, from octo_hook.py's Stop handler, not yet drained
    by the octo process watching it.  worktree_path is the clone path for worktree-lane
    notifications, or Path("") for root-lane (agent running directly in the project)."""
    worktree_path: Path    # clone path (Path("") for root-lane)
    session_id: str          # Claude session id the Stop event belongs to
    transcript_path: str       # transcript file for that session, for a one-shot prompt-text read
    agent_name: str             # which agent fired the hook (e.g. "Claude")
    notified_at: float           # epoch seconds the notification was written


def notify_turn_ended(owner_pid: int, worktree_path: Path, session_id: str, transcript_path: str,
                       agent_name: str) -> Path:
    """Drops one TurnEnded for owner_pid's octo process to pick up on its next poll tick (see
    drain_pending_turn_ends) -- called by octo_hook.py's Stop handler right after it resolves
    worktree_path's owner marker (see worktree_manager.read_owner_marker), or via the root-lane
    fallback (see find_matching_session). Returns the file path written."""
    pending_dir = PENDING_TURN_ENDS_DIR / str(owner_pid)  # this octo process's own inbox, so concurrent Stop hooks targeting different octo processes never collide
    pending_dir.mkdir(parents=True, exist_ok=True)
    entry_path = pending_dir / f"{uuid.uuid4().hex}.json"  # unique per notification, so concurrent Stop hooks never clobber each other's entry
    entry_path.write_text(json.dumps({
        "worktree_path": str(worktree_path), "session_id": session_id,
        "transcript_path": transcript_path, "agent_name": agent_name, "notified_at": time.time(),
    }), encoding="utf-8")
    return entry_path


def drain_pending_turn_ends(pid: int) -> list[TurnEnded]:
    """Returns every TurnEnded dropped for the octo process with the given pid since the last
    drain, deleting each entry file as it's read -- a poll_once()-friendly queue-drain, called
    once per tick alongside drain_pending_worktrees. Empty/missing inbox returns []."""
    pending_dir = PENDING_TURN_ENDS_DIR / str(pid)
    if not pending_dir.is_dir():
        return []
    turn_ends = []
    for entry_path in sorted(pending_dir.glob("*.json")):  # sorted by filename (uuid) -- not notification-time ordered, but each is independent, so drain order doesn't matter
        entry = json.loads(entry_path.read_text(encoding="utf-8"))
        entry_path.unlink()
        turn_ends.append(TurnEnded(
            Path(entry["worktree_path"]), entry["session_id"], entry["transcript_path"],
            entry.get("agent_name", ""), entry["notified_at"],
        ))
    return turn_ends


PENDING_SYNCS_DIR = Path.home() / ".octo" / "pending_syncs"


@dataclass
class SyncEvent:
    """A sync result from a worktree (e.g. from down_sync triggered by UserPromptSubmit hook),
    not yet drained by the octo process watching it."""
    worktree_path: Path
    agent_name: str
    ok: bool
    conflicted: bool
    detail: str
    notified_at: float


def notify_sync(owner_pid: int, worktree_path: Path, agent_name: str,
                ok: bool, conflicted: bool, detail: str) -> Path:
    """Drops one SyncEvent for owner_pid's octo process to pick up on its next poll tick.
    Returns the file path written."""
    pending_dir = PENDING_SYNCS_DIR / str(owner_pid)
    pending_dir.mkdir(parents=True, exist_ok=True)
    entry_path = pending_dir / f"{uuid.uuid4().hex}.json"
    entry_path.write_text(json.dumps({
        "worktree_path": str(worktree_path),
        "agent_name": agent_name,
        "ok": ok,
        "conflicted": conflicted,
        "detail": detail,
        "notified_at": time.time(),
    }), encoding="utf-8")
    return entry_path


def drain_pending_syncs(pid: int) -> list[SyncEvent]:
    """Returns every SyncEvent dropped for the octo process with the given pid since the last
    drain, deleting each entry file as it's read."""
    pending_dir = PENDING_SYNCS_DIR / str(pid)
    if not pending_dir.is_dir():
        return []
    syncs = []
    for entry_path in sorted(pending_dir.glob("*.json")):
        entry = json.loads(entry_path.read_text(encoding="utf-8"))
        entry_path.unlink()
        syncs.append(SyncEvent(
            Path(entry["worktree_path"]),
            entry.get("agent_name", ""),
            entry["ok"],
            entry["conflicted"],
            entry["detail"],
            entry["notified_at"],
        ))
    return syncs

