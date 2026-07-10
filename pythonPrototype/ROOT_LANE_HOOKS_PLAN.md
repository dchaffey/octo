# Root-Lane Agent Hooks — Implementation Plan

## Overview

Agents running directly in the watched project (not via `octo run`) currently rely
solely on **log polling** for attribution: files are committed as "Human" first,
then re-labeled later when transcript lines catch up. This plan adds **Stop-hook
integration** so the agent itself signals "turn done" and octo commits + attributes
everything immediately — no log-polling lag.

Everything goes into `.octo` (the shadow git repo). Real `.git` is never touched.

---

## 1. How hooks are registered (already built)

### Claude's hook config

`hook_installer.py` writes into `<project>/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|NotebookEdit|Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/octo.py _hook"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/octo.py _hook"
          }
        ]
      }
    ]
  }
}
```

Key points:
- `PreToolUse` fires before every Edit/Write/Bash tool call (always approves — exits 0)
- `Stop` fires when a turn ends (the Claude process yields control back to the user)
- Both invoke the same `octo.py _hook` entrypoint
- Codex and Antigravity have equivalent config paths (`.codex/hooks.json`, `.agents/hooks.json`) but no confirmed Stop-equivalent event yet

### Installing hooks into the root project

Currently `detect_and_install_hooks()` is only called for worktrees (via `agent_launcher.py`).
For root-lane, call it on TUI startup:

```python
# In octo_tui.py, EditWatcherApp.on_mount():
from hook_installer import detect_and_install_hooks
detect_and_install_hooks(self.root)  # idempotent — won't double-install
```

This makes the root project's own Claude config carry the Stop hook.
Now when the user runs `claude` in that directory, every turn-end fires octo's hook.

---

## 2. Hook payload format (what Claude sends on stdin)

When Claude's Stop hook fires, it pipes this JSON to the hook process's stdin:

```json
{
  "hook_event_name": "Stop",
  "cwd": "/home/user/projects/myapp",
  "session_id": "abc123-def456-ghi789",
  "transcript_path": "/home/user/.claude/projects/-home-user-projects-myapp/abc123.jsonl",
  "stop_hook_active": false
}
```

`stop_hook_active` is `false` on the first Stop event in a turn. Claude may fire
Stop multiple times (e.g. for internal sub-turns). Only the outer one matters.

---

## 3. How the hook is parsed and routed (`octo_hook.py`)

Current code (`octo_hook.py`):

```python
def main():
    payload = json.loads(sys.stdin.read())
    if payload.get("hook_event_name") == "Stop" and not payload.get("stop_hook_active", False):
        _signal_turn_end(payload)
    sys.exit(0)

def _signal_turn_end(payload):
    cwd = Path(payload["cwd"])
    owner = read_owner_marker(cwd)        # ← only works for worktrees
    if owner is None:
        return                              # ← silently drops root-lane Stop events!
    notify_turn_ended(owner["pid"], cwd, session_id, transcript_path)
```

**Change needed** — add root-lane fallback:

```python
def _signal_turn_end(payload):
    cwd = Path(payload["cwd"])
    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")

    # Path 1: worktree lane (existing)
    owner = read_owner_marker(cwd)
    if owner is not None:
        notify_turn_ended(owner["pid"], cwd, session_id, transcript_path,
                          agent_name="Claude")
        return

    # Path 2: root lane (NEW)
    from session_registry import find_matching_session
    session = find_matching_session(cwd)
    if session is not None:
        # worktree_path = empty Path() signals "this is root-lane, not a worktree"
        notify_turn_ended(session.pid, Path(""), session_id, transcript_path,
                          agent_name="Claude")
```

The empty `worktree_path` (`Path("")`) is the discriminator — the TUI uses it to
tell "this turn-end is from the root project, not a worktree clone."

---

## 4. IPC: how the notification reaches the TUI

`session_registry.py` already has the file-based inbox pattern. Adding `agent_name`:

### `TurnEnded` dataclass change

```python
@dataclass
class TurnEnded:
    worktree_path: Path     # clone path (Path("") for root-lane)
    session_id: str         # Claude session id
    transcript_path: str    # transcript file path
    agent_name: str         # NEW: which agent (e.g. "Claude")
    notified_at: float      # epoch seconds
```

### `notify_turn_ended` change

```python
def notify_turn_ended(owner_pid, worktree_path, session_id, transcript_path, agent_name):
    pending_dir = PENDING_TURN_ENDS_DIR / str(owner_pid)
    pending_dir.mkdir(parents=True, exist_ok=True)
    entry_path = pending_dir / f"{uuid.uuid4().hex}.json"
    entry_path.write_text(json.dumps({
        "worktree_path": str(worktree_path),
        "session_id": session_id,
        "transcript_path": transcript_path,
        "agent_name": agent_name,            # NEW
        "notified_at": time.time(),
    }))
    return entry_path
```

### File on disk (what `drain_pending_turn_ends` reads)

```
~/.octo/pending_turn_ends/12345/a1b2c3d4.json
```

Contents:
```json
{
  "worktree_path": "",
  "session_id": "abc123-def456",
  "transcript_path": "/home/user/.claude/projects/.../abc123.jsonl",
  "agent_name": "Claude",
  "notified_at": 1752153600.123
}
```

---

## 5. TUI: draining and handling root-lane turn-ends

### Poll loop (already drains turn-ends!)

`octo_tui.py` line 989:

```python
def poll_once(self):
    for registration in drain_pending_worktrees(self.pid):
        self._render_worktree_registration(registration)
    for turn_ended in drain_pending_turn_ends(self.pid):   # ← already here
        self._handle_turn_ended(turn_ended)                  # ← dispatches here
    # ... log polling, commit_dirty ...
```

The drain happens BEFORE log polling — so hook-attributed commits land first.

### `_handle_turn_ended` change

Current: only handles worktrees (looks up `_worktree_states`).
New: detect root-lane by empty `worktree_path`:

```python
def _handle_turn_ended(self, turn_ended: TurnEnded):
    # Worktree lane (existing)
    if turn_ended.worktree_path != Path(""):
        state = self._worktree_states.get(turn_ended.worktree_path)
        if state is None:
            return
        state.session_id = turn_ended.session_id
        state.transcript_path = turn_ended.transcript_path
        self._sync_worktree(state)
        return

    # Root lane (NEW)
    self._handle_root_lane_turn_end(turn_ended)
```

### `_handle_root_lane_turn_end` — the new method

```python
def _handle_root_lane_turn_end(self, turn_ended: TurnEnded):
    """A Stop hook fired in the root project (not a worktree).
    Commit everything dirty, attribute to the agent that just finished,
    and render immediately. No log polling needed."""

    # 1. Read the prompt text from the transcript
    prompt = read_last_prompt(turn_ended.transcript_path)

    # 2. Commit everything currently dirty on disk
    #    (these are the files the agent just wrote during its turn)
    with self._root_lane:
        settled_list = self.file_watcher.commit_dirty()

    # 3. Attribute each commit to the agent
    for settled in settled_list:
        # Use attribute_settled — no content verification needed,
        # we KNOW these were written by this agent's turn
        self.file_watcher.attribute_settled(
            settled,
            turn_ended.agent_name,
            turn_ended.session_id,
            prompt,
        )
        # 4. Render in the TUI
        edit = ToolEdit(
            settled.settled_at,
            settled.file_path,
            turn_ended.session_id,
            prompt,
            turn_ended.agent_name,
            None,
        )
        self._render_agent_edit(settled, edit)
```

**Why `attribute_settled` not `attribute`:**

| Method | Use case | How |
|---|---|---|
| `attribute(edit)` | Log polling: has a `ToolEdit` with content transform, must FIND which commit matches | Walks recent commits, probes content via `EditContent.apply()`, then adds note |
| `attribute_settled(settled, agent, session, prompt)` | Hook path: already has the exact `SettledEdit` (just committed via `commit_dirty()`), no search needed | Just calls `_add_note()` directly |

---

## 6. Git commands: what actually happens in `.octo`

### Step A: Commit dirty files

```bash
# What ShadowGitWatcher.commit_dirty() does under the hood:
git --git-dir=/project/.octo --work-tree=/project add -- src/main.py
git --git-dir=/project/.octo --work-tree=/project commit -q -m "octo: edit src/main.py"
# Returns sha: "a1b2c3d4..."
```

### Step B: Attribute via git notes

```bash
# What ShadowGitWatcher.attribute_settled() → _add_note() does:
git --git-dir=/project/.octo --work-tree=/project notes add \
  -m "Agent: Claude
Session: abc123-def456

Fix the authentication bug in login handler" \
  a1b2c3d4
```

### Step C: Read back later

```bash
# What ShadowGitWatcher._attribution_for() does:
git --git-dir=/project/.octo --work-tree=/project notes show a1b2c3d4
# Returns:
#   Agent: Claude
#   Session: abc123-def456
#
#   Fix the authentication bug in login handler

# Or from commit message (legacy repos before notes-based attribution):
git --git-dir=/project/.octo --work-tree=/project log -1 --format=%B a1b2c3d4
```

### Full lifecycle in `.octo` (what history replay sees)

```
$ git --git-dir=/project/.octo --work-tree=/project log --oneline
a1b2c3d4 octo: edit src/main.py          # ← committed by commit_dirty()
d5e6f7g8 octo: edit src/utils.py         # ← same turn, another file
h9i0j1k2 octo: baseline                  # ← startup baseline

$ git --git-dir=/project/.octo --work-tree=/project notes show a1b2c3d4
Agent: Claude
Session: abc123-def456

Fix the authentication bug in login handler

$ git --git-dir=/project/.octo --work-tree=/project notes show d5e6f7g8
Agent: Claude
Session: abc123-def456

Fix the authentication bug in login handler
```

---

## 7. Rendering in the TUI

The existing `_render_agent_edit()` path handles the TUI display. It:

1. Breaks any trailing Human streak (closes the Human block)
2. Opens (or reuses) an agent block grouped by `(session_id, prompt)`
3. Creates a `PromptGroup` with:
   - Timestamp header: `14:32:05  Claude  +12 -3`
   - Prompt excerpt: `Fix the authentication bug in login handler`
   - Each file as a collapsed diff inside

The flow:
```
hook fires → commit_dirty() → attribute_settled() → _render_agent_edit()
                                                        ↓
                                              PromptGroup shows:
                                              14:32:05  Claude  +12 -3
                                              Fix the authentication bug...
                                                src/main.py  +8 -3  [expand]
                                                src/utils.py  +4 -0  [expand]
```

---

## 8. Summary of changes

### Files changed:

| File | Change |
|---|---|
| `octo_hook.py` | Add root-lane fallback: `find_matching_session(cwd)` when `read_owner_marker` returns None |
| `session_registry.py` | Add `agent_name` field to `TurnEnded` dataclass and `notify_turn_ended()` |
| `octo_tui.py` | In `_handle_turn_ended()`: detect root-lane (empty `worktree_path`), call new `_handle_root_lane_turn_end()` |
| `octo_tui.py` | New method `_handle_root_lane_turn_end()`: `read_last_prompt()` → `commit_dirty()` → `attribute_settled()` → `_render_agent_edit()` |
| `octo_tui.py` | (Optional) Call `detect_and_install_hooks(root)` in `on_mount()` |

### Lines of new code: ~40

### No new files. No new dependencies.

---

## 9. What about race conditions?

**What if the user hand-edits a file between the hook firing and `commit_dirty()`?**
→ The hand edit gets committed too, but attributed to the agent. In practice, the hook fires
immediately when Claude yields control — the window is milliseconds. And the log-polling
fallback still runs; if the transcript later shows the agent didn't write that file,
`attribute()` won't match it and it stays Human.

**What if `commit_dirty()` finds nothing dirty?**
→ No-op. The agent may have only done read-only work (search, think, etc.). No commit,
nothing to attribute, nothing to render. The turn-end signal is silently consumed.

**What if the hook fires for a stale/old session?**
→ `find_matching_session()` only matches live octo processes. If octo isn't running,
`_signal_turn_end` is a no-op. The hook exits 0 immediately.
