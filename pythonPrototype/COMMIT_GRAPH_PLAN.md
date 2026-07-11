# Plan: GitKraken-style commit graph screen for octo

## Context

octo is a Textual TUI (`octo_tui.py`) that watches a project and records every edit as a commit
in a **shadow git repo** (`.octo`), attributing each commit to the agent that made it (Claude /
Codex / Antigravity / Human) via git notes. Multiple agents run concurrently in their own
worktree clones and their work is synced back into the root.

Today the activity is only shown as a linear scrolling **feed** (`#feed`) of bordered blocks.
The user wants a second view: a **GitKraken-style rail graph** — colored lanes per agent, commit
nodes flowing down, and merge glyphs where an agent's work lands back into the mainline. Per the
user: *don't* show branch-creation or sync/paused status — **only commits and merges**.

### Key data-model constraint (drives the whole design)

The root `.octo` shadow repo is a **strictly linear history**. There are **no real 2-parent merge
commits** — real merges happen in throwaway scratch clones (`worktree_sync.py`) that get deleted;
only the resulting file bytes land in root and are committed as ordinary single-parent commits
carrying a `Branch:` git-note. Therefore the graph's **lanes and merge edges must be synthesized
from each commit's `Branch:` attribution**, not read from git parent topology. There is also no
canvas widget, so lanes are drawn as **text rail glyphs** (`│ ● ├ ╯ ╰`) inside widgets — exactly
how every existing screen renders (markup strings in `Static`).

## Decisions (confirmed with user)

- **Commit nodes are clickable rows, not dropdowns.** Clicking a node opens a **detail screen**.
- **Detail screen** shows *full commit details* and renders each file exactly as the current feed
  does: an individual collapsible diff dropdown per file **plus a revert button** (reuse the
  existing `Collapsible` / `FileChange` / `RevertButton` machinery).
- **Scope mirrors the feed exactly:** graph starts fresh with **this session's commits only**;
  pressing **`l`** loads prior-run history into the graph too (same toggle semantics as the feed's
  existing `action_toggle_history`).

## Design

### 1. `shadow_repo.py` — two small read methods (no diffs on the hot path)

`history()` (`shadow_repo.py:362`) already reads the full log with attribution, but it fetches a
`git show` diff **per file per commit** — too heavy to re-run on a refresh interval. Add:

- `@dataclass GraphCommit`: `sha, timestamp, agent, session_id, prompt, branch, is_baseline,
  file_count`. **No diffs.**
- `graph_commits(self) -> list[GraphCommit]` — oldest-first, **one record per commit**. Mirror
  `history()`'s log parse (`git log --reverse --format=%H%x00%at%x00%B%x03`) and reuse
  `_attribution_for()` (`:391`) for agent/branch, but replace the per-file `git show` diff loop
  with a `--name-only` **count** only. This is the graph's live data source.
- `commit_detail(self, sha) -> list[HistoryEntry]` — per-file diffs for a **single** commit (the
  per-file `git show --name-only` + `git show -- <path>` loop from `history()`, scoped to one
  sha). Called lazily only when a node is clicked, to feed the detail screen.

### 2. `commit_graph.py` — new pure module for lane layout (testable)

Keep the synthetic-lane logic out of the TUI so it is unit-testable (matches the repo's
"data dominates" / assert-don't-guard style).

- Input: `list[GraphCommit]` in display order.
- Output: `list[GraphRow]` where `GraphRow` carries the commit plus its computed `lane` (column
  index), the set of `active_lanes` to draw vertical bars for on that row, and an `is_merge` flag
  + `merge_from_lane`.
- Algorithm (synthesized from `branch`, since history is linear):
  - Lane 0 = mainline: baseline + Human commits + any commit with empty `branch`.
  - Each distinct non-empty `branch` claims the next free lane column on its first commit; color
    comes from the agent (`AGENT_COLOR_ROLES`).
  - A commit is a **merge** when its contiguous same-branch run ends (the next commit returns to
    mainline or a different branch) — that node draws a merge glyph joining its lane back to lane 0,
    then frees the lane. This is the "agent work landed into root" moment.
- Assert inputs (e.g. non-negative timestamps, known lane invariants) rather than guarding.

### 3. `octo_tui.py` — graph screen, detail screen, wiring

Follow the existing `BranchesScreen` pattern (`octo_tui.py:537`) throughout.

- **Constants** near `:95-119`: `GRAPH_KEY = "g"`, `GRAPH_ACTION = "show_graph"`,
  `GRAPH_REFRESH_SECONDS` (reuse the `BRANCHES_REFRESH_SECONDS = 1.0` cadence).
- **`class CommitGraphScreen(Screen[None])`** (model on `BranchesScreen`):
  - `__init__(self, watcher, baseline_sha)` — takes the app's `ShadowGitWatcher` and baseline.
  - `compose`: `Header()` / `VerticalScroll(id="graph")` / `Footer()`.
  - `BINDINGS`: `Binding("escape", "close", "Back")`, `Binding(GRAPH_KEY, "close", "Back")`,
    `Binding(TOGGLE_HISTORY_KEY, "toggle_history", "Load history")` — its own local history toggle
    so `l` works in the graph like it does in the feed; relabel via the same
    replace-and-`refresh_bindings()` trick as `_update_history_binding` (`:1655`).
  - `on_mount`: set title, `_refresh()`, `set_interval(GRAPH_REFRESH_SECONDS, self._refresh)` for
    live updates (re-query + re-render from scratch, cheap — same as `BranchesScreen._refresh`).
  - `_refresh`: `commits = watcher.graph_commits()`; if history **not** loaded, drop commits before
    `baseline_sha` (session-only, the default); feed through `commit_graph.layout()`; rebuild
    `#graph` by mounting one **clickable row widget per commit**.
  - **`class CommitRow(Static)`** (or `Static` subclass with `on_click`): renders one `GraphRow` as
    a markup string — rail glyphs for active lanes + node/merge glyph at its column, then
    `short-sha` + agent label via `_agent_markup(...)` (`:161`) + truncated prompt + relative time.
    Stores its `sha`; `on_click`/`enter` pushes `CommitDetailScreen`. Use `escape()` (`:151`) on all
    text.
- **`class CommitDetailScreen(Screen[None])`**:
  - `__init__(self, watcher, sha)`.
  - `on_mount`: title = agent · branch · short-sha; build a single `PromptGroup` (`:641`) seeded
    with the commit's agent/prompt/session/branch, then for each `HistoryEntry` from
    `watcher.commit_detail(sha)` call `group.add_change(self.app._settled_to_filechange(...))`
    (`:756`, `:1531`) — this reproduces the feed's per-file collapsible + `RevertButton` exactly.
    Mark already-reverted files using `watcher.is_commit_reverted` as the feed does.
  - Revert presses bubble to the app's existing `on_button_pressed` (`:1258`) → `_confirm_and_revert`
    (`:1273`) flow unchanged (RevertButton carries file_path+commit).
  - `BINDINGS`: `escape` / `action_close` → `dismiss(None)`.
- **App wiring**:
  - Add `Binding(GRAPH_KEY, GRAPH_ACTION, "Graph")` to `EditWatcherApp.BINDINGS` (`:925`).
  - `def action_show_graph(self): self.push_screen(CommitGraphScreen(self.file_watcher, self._baseline_sha))`
    (mirror `action_show_branches` at `:1719`).
  - Add screen `CSS` blocks (padding, row height auto, lane spacing) mirroring `BranchesScreen.CSS`.

### Reuse summary (do not re-implement)

- Attribution parsing: `_attribution_for` / `_parse_attribution` (`shadow_repo.py:391`, `:97`).
- Agent colors: `AGENT_COLOR_ROLES` (`:86`), `_agent_markup` (`:161`), `escape` (`:151`).
- Branch→agent display: `_branch_agent_display` (`octo_tui.py:257`).
- Feed file rendering: `PromptGroup.add_change` (`:756`), `_settled_to_filechange` (`:1531`),
  `FileChange` (`:606`), `RevertButton` (`:286`) + revert flow (`:1258`, `:1273`).
- History-toggle relabel pattern: `_update_history_binding` (`:1655`).

## Verification

1. **Layout unit test** — feed `commit_graph.layout()` a synthetic `GraphCommit` list with two
   interleaved agent branches + Human commits; assert lane columns, `is_merge` flags, and that
   lanes free after a merge. Runs with no TUI.
2. **Live run** — `python octo.py` (or the `octo` entrypoint) in a repo with the shadow watcher,
   drive some edits / a couple of agent worktrees so commits with different `Branch:` notes land,
   press **`g`**: confirm lanes render per agent with colored nodes and merge glyphs, and no
   sync/branch-creation chrome appears.
3. **Interactivity** — click a commit node: detail screen opens showing that commit's files as
   collapsible diffs with revert buttons; expand a diff, and exercise a revert to confirm it routes
   through the existing revert-confirm flow. Press `l` on the graph: prior-run commits appear;
   press again: they disappear. Press `escape` from both screens to return.
4. Textual dev check: run under `textual run --dev` if needed to confirm zero markup/CSS warnings
   (repo rule: compile/lint clean).
