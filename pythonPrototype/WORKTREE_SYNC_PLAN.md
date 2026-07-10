# Worktree Sync & Merge Plan

Design for keeping each agent's isolated clone (see `worktree_manager.py`) in sync
with the real project (`root`), and for landing an agent's finished work back into
`root`, without ever blocking or corrupting the human's live working tree.

**Root's real project `.git` is never touched by any of this.** Every "sync against
root" below actually means sync against `root`'s *shadow* repo (`root/.octo`, see
`shadow_repo.SHADOW_DIR_NAME`) — a separate, real git repo whose work-tree is `root`
and whose `ShadowGitWatcher.commit_dirty()` keeps its HEAD an accurate, near-real-time
git-committed snapshot of what's actually on disk. The real project `.git` is only
ever whatever the human last committed manually, and can be arbitrarily stale/
divergent from current disk state (uncommitted edits, uncommitted deletions, ...) —
cloning/rebasing/merging against it instead would check that stale tree out into
every fresh agent worktree, resurrecting files the human already deleted on disk but
never committed away. Landing an up-synced change still means writing real bytes
onto `root`'s real working tree (the human-visible files); it's only the ref/ancestry
side of every sync operation that's anchored to `.octo` instead of the real `.git`.

## Model: hub and spoke

- `root` is the hub. It's the directory the human directly edits and the only
  authority — it never gets synced *against* anything else, it only ever receives
  commits (human writes, and validated agent merges).
- Each agent worktree (an independent `git clone` of `root/.octo`, per
  `create_agent_worktree` in `worktree_manager.py`) is a spoke. Spokes sync down from
  `root`'s shadow repo and submit up to `root`'s real working tree; they never sync
  with each other directly.

## Down-sync: root -> agent worktree

- **Policy: root always wins.** On any line both sides touched, root's incoming
  content is kept.
- **Mechanism:** `git rebase -X ours <root's current commit>`, run inside the agent
  clone. Note git's ours/theirs inversion for `rebase` (opposite of `merge`):

  | goal | `git merge` | `git rebase <root>` |
  |---|---|---|
  | root's edits win | `-X theirs` | `-X ours` |
  | agent's edits win | `-X ours` | `-X theirs` |

  So "root always wins" is `git rebase -X ours <root>` on the agent branch.
  Non-conflicting agent edits (different lines, different files) are preserved as
  normal — this only overrides on genuine textual collisions.
- **Trigger: turn boundaries only, never mid-turn.** An agent's turn (processing one
  prompt) is treated as atomic and must not be interfered with — rebasing a
  worktree's files while the agent has an in-flight tool call would change files
  out from under it. Down-sync happens right before the *next* prompt is allowed to
  start, not continuously/instantly.
- **No queue needed for this side.** Only one thing ever triggers a down-sync for a
  given worktree (its own turn-boundary handler), and only one turn is ever in
  flight per worktree by definition. There's nothing to serialize against, so this
  is a single step in the turn-boundary sequence below, not a queue.

## Up-sync: agent worktree -> root

Triggered at the end of a turn, if that turn produced new commits.

1. **Trial merge in a scratch clone** — never touch `root`'s live working tree
   directly. Use the default (unbiased) merge strategy: a real 3-way merge that can
   genuinely conflict.
2. **If clean:** land it into `root` through root's serialized lane (below).
   Fast-forward/apply the validated result, tagged via a git-notes trailer marking
   it as merged from that agent — same convention `octo_tui.py` already uses for
   tagging revert commits (see `OCTO_AGENT_LABEL`).
3. **If conflicting:** abort the trial merge (`root` is never touched), mark the
   worktree paused/conflicted, and surface it for resolution (see Conflict
   handling).

## Turn-boundary sequence (per worktree)

1. **Submit** (up-sync) if the turn that just ended produced new commits.
2. **Resync** (down-sync) unconditionally onto `root`'s current HEAD — unless step
   1 left the worktree paused/conflicted, in which case skip this and stay paused.
3. Only once both are clear does the worktree's next prompt get dispatched.

## Root's serialized lane

The only real queue in the system.

- **Handles:** (a) committing a settled human edit in `root` (already exists:
  `ShadowGitWatcher.commit_dirty`), and (b) landing a validated agent merge (up-sync
  step 2). These are the only two operations that mutate `root`'s actual
  HEAD/working tree — down-sync into a spoke never touches `root`, so it doesn't
  need this lane, and doesn't need a lane of its own either (see above).
- **Human commits are unconditional writes, not merges** — there is nothing for
  them to conflict against, so they can never be "rejected" by this design. They're
  also cheap, so in practice they're never meaningfully delayed by (b), which is
  just landing an already-trial-validated fast-forward.
- **Tie-break:** if a human write and a merge-landing are both ready at the same
  instant, the human write goes first; the merge-landing step re-checks
  fast-forwardability against the new HEAD afterward (re-trial if it no longer
  applies cleanly).
- **Per-worktree conflicts don't block the lane.** If worktree A's submit conflicts
  and gets parked, worktree B's submit and any other worktree's turn-boundary
  resync still proceed independently.

## Conflict handling

- **Default:** pause the worktree — dispatch no further prompts to it — until a
  human resolves the conflict via `BranchesScreen`.
- **Optional: delegate resolution back to the originating agent (Claude only, for
  now).**
  - Implemented as the agent's own `Stop` hook returning
    `{"decision": "block", "reason": "<conflict summary>"}` instead of approving.
    This keeps the same interactive session alive and re-prompts the model with the
    conflict details directly, rather than octo polling out-of-band or trying to
    inject a new prompt into a running interactive process from outside.
  - Codex's and Antigravity's hook systems are unverified for this "block +
    re-prompt" semantic (`hook_installer.py` already flags their hook event
    names/envelopes as unconfirmed against live docs) — they get the pause-for-human
    path only, until confirmed otherwise.
  - **Bounded retries** (small fixed N) before falling back to pause-for-human
    regardless of agent capability — an agent that can't resolve its own conflict
    must not loop indefinitely.
  - Safety is unaffected either way: a delegated resolution still has to pass the
    same trial-merge check (up-sync step 1) before it can land in `root`. A bad
    attempt just produces another conflict; it can never corrupt `root` directly.

## Existing building blocks this reuses

- `worktree_manager.py` — creates each agent's independent clone (the spoke);
  down/up-sync operate on these clones' git state.
- `session_registry.py` — file-based inbox pattern (`register_worktree` /
  `drain_pending_worktrees`); root's serialized lane should follow the same
  file-based, no-live-IPC style already established here.
- `agent_watcher.py`'s transcript tailers (`ClaudeTranscriptTailer`,
  `AntigravityTranscriptTailer`, `CodexTranscriptTailer`) — already detect
  per-session prompt boundaries live; this is the turn-boundary signal the sequence
  above hooks into.
- `octo_tui.py`'s `BranchesScreen` — existing live worktree overview; extend it to
  show paused/conflict state per worktree.
- `hook_installer.py` / `octo_hook.py` — existing `PreToolUse`/`BeforeTool`
  pre-write hook plumbing; conflict delegation adds a new `Stop`-event registration
  alongside it, reusing the same `_hook` re-invocation pattern
  (`_octo_invocation`).
- `octo_tui.py`'s existing git-notes tagging convention for revert commits
  (`OCTO_AGENT_LABEL` and friends) — the pattern to follow for tagging agent-merge
  commits landed into `root`.

## New modules (proposed)

- A sync module (e.g. `worktree_sync.py`) — `rebase -X ours` for down-sync;
  scratch-clone trial merge + land for up-sync.
- A root-lane module (e.g. `root_lane.py`) — single-writer serialization for the
  two operations that mutate `root`'s HEAD.
- Extend `octo_hook.py` with `Stop`-event handling for Claude, gated by agent
  capability.
- Extend `WorktreeInfo` (`worktree_manager.py`) and `BranchesScreen`
  (`octo_tui.py`) to carry and render a paused/conflict state per worktree.

## Open questions / not yet decided

- Exact retry cap N for agent-delegated conflict resolution.
- Exact conflict summary format handed to the agent's `Stop` hook (raw conflict
  markers vs. a summarized diff).
- Whether Codex/Antigravity ever get an equivalent delegation path once their hook
  semantics are confirmed against live docs.
