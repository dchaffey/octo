# Graph Report - .  (2026-07-17)

## Corpus Check
- Corpus is ~31,901 words - fits in a single context window. You may not need a graph.

## Summary
- 686 nodes · 1651 edges · 33 communities (29 shown, 4 thin omitted)
- Extraction: 84% EXTRACTED · 16% INFERRED · 0% AMBIGUOUS · INFERRED: 266 edges (avg confidence: 0.51)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Shadow Repo Commit & Attribution
- Agent Notifications & State
- Commit Graph Screen UI
- Agent Launcher & Tmux Sessions
- Root Lane & Agent Picker
- Agent Detection & Hook Install
- TUI Button Handlers
- Agent Edit Content Parsing
- Edit Block Rendering
- Worktree Sync
- Main TUI App Actions
- Commit Line Rendering
- Branches Screen & Worktree Registry
- Antigravity Transcript Tailer
- Poll Result Handling
- Claude Transcript Tailer
- File Change Entry UI
- Commit Graph Layout
- Revert Rendering
- Codex Prompt & Timestamp Parsing
- Cache Size & Clearing
- Tailer Edit Attribution
- Tool Edit & File Change Models
- History Toggle & Graph Refresh
- Antigravity Prompt Extraction
- History Load/Unload
- Startup Splash Screen
- Codex Transcript Tailer
- Last Prompt Reader
- Human Copy Unmount
- Feed Scroll & Mount
- Turn End Dispatch
- Scratch Test Script

## God Nodes (most connected - your core abstractions)
1. `EditWatcherApp` - 64 edges
2. `ShadowGitWatcher` - 62 edges
3. `ToolEdit` - 42 edges
4. `SettledEdit` - 38 edges
5. `PromptGroup` - 37 edges
6. `ShellMoveEdit` - 32 edges
7. `CommitGraphScreen` - 32 edges
8. `WorktreeInfo` - 29 edges
9. `HistoryEntry` - 28 edges
10. `AffectedCommit` - 26 edges

## Surprising Connections (you probably didn't know these)
- `AgentSession` --uses--> `RunningSession`  [INFERRED]
  agent_launcher.py → session_registry.py
- `AffectedCommit` --uses--> `EditContent`  [INFERRED]
  shadow_repo.py → agent_watcher.py
- `GraphCommit` --uses--> `EditContent`  [INFERRED]
  shadow_repo.py → agent_watcher.py
- `HistoryEntry` --uses--> `EditContent`  [INFERRED]
  shadow_repo.py → agent_watcher.py
- `SettledEdit` --uses--> `EditContent`  [INFERRED]
  shadow_repo.py → agent_watcher.py

## Import Cycles
- None detected.

## Communities (33 total, 4 thin omitted)

### Community 0 - "Shadow Repo Commit & Attribution"
Cohesion: 0.05
Nodes (37): _note_text(), _parse_attribution(), CompletedProcess, Path, Builds attribute()'s git-notes body: Agent/Session trailers plus the prompt, par, Extracts (agent, session_id, prompt, branch) from a note body (_note_text) or a, Tracks a directory tree via a shadow git repo, committing every write immediatel, Commits every currently-dirty path immediately (no settle wait) and returns them (+29 more)

### Community 1 - "Agent Notifications & State"
Cohesion: 0.06
Nodes (56): Enum, AgentState, classify_claude_notification(), notify_agent_state(), Agent lifecycle states, normalized across agents so a sink never sees an agent's, Maps a Claude Notification hook's `message` text to an AgentState: idle-waiting, Public entry: raises a desktop notification for agent_name entering state, unles, The only sink today: a libnotify desktop popup via notify-send. Absence of notif (+48 more)

### Community 2 - "Commit Graph Screen UI"
Cohesion: 0.05
Nodes (40): CommitDetailScreen, CommitRow, _effective_label(), _label_color(), ComposeResult, _rail_markup(), One clickable graph row: rail glyphs, then short-sha + agent label + prompt exce, Builds the full markup for one row: rail glyphs + a colored short-sha, agent lab (+32 more)

### Community 3 - "Agent Launcher & Tmux Sessions"
Cohesion: 0.07
Nodes (49): AgentSession, _ensure_tmux_config(), _exec_env(), launch_agent_session(), list_live_tmux_sessions(), main(), _monitor_parent(), Path (+41 more)

### Community 4 - "Root Lane & Agent Picker"
Cohesion: 0.11
Nodes (31): One mv/cp shell command parsed from an agent's Bash/exec tool call. Unlike ToolE, ShellMoveEdit, CommitGraphScreen, Full-screen GitKraken-style rail graph of the shadow repo's commits, one clickab, AgentPickerScreen, ClearCacheConfirmScreen, FolderPickerScreen, PromptRevertButton (+23 more)

### Community 5 - "Agent Detection & Hook Install"
Cohesion: 0.09
Nodes (38): detect_available_agents(), Path, Returns agent name -> resolved absolute binary path for every agent in AGENT_BIN, Case-insensitively resolves identifier to its canonical AGENT_BINARIES key (e.g., Resolves identifier to (canonical agent name, resolved real binary path). Return, Resolves binary_name to its absolute path via PATH lookup, or None if not found., resolve_agent_name(), _resolve_binary() (+30 more)

### Community 6 - "TUI Button Handlers"
Cohesion: 0.07
Nodes (11): _file_section_title(), Path, Rebuilds the collapsed title from the current aggregate stats and all-reverted s, Live per-worktree sync status, read by BranchesScreen -- there's nothing on disk, Handles a RevertButton (one file) or PromptRevertButton (every file in a group), Previews the later commits touching each target file in a confirmation modal, th, Returns (file_path, commit) for every non-reverted file change currently mounted, Dismisses the modal with True (proceed) only if the Revert button was pressed. (+3 more)

### Community 7 - "Agent Edit Content Parsing"
Cohesion: 0.10
Nodes (27): _antigravity_edit_content(), _build_shell_poll_result(), _codex_edit_content(), _edit_content_for(), EditContent, _extract_edit(), _extract_edits(), _parse_diff_hunks() (+19 more)

### Community 8 - "Edit Block Rendering"
Cohesion: 0.11
Nodes (17): EditBlock, PromptGroup, Renders one prompt (or human-edit flush) within a block: preceding no-change pro, Yields preceding no-change prompts as > lines, then the 'time  agent  +N -M' lin, Builds the 'time  agent  +N -M' line, N/M being the running line-change totals., Brings the mounted PromptRevertButton (if any) in line with _show_revert_button., True once every edit mounted under this group has been moved out via remove_chan, Bordered container for one streak of same-category commits: a run of consecutive (+9 more)

### Community 9 - "Worktree Sync"
Cohesion: 0.19
Nodes (19): Runs one worktree's turn-boundary sequence: up-sync (land its new commits into r, _apply_changed_paths(), _commit_worktree_dirty(), _diff_status_pairs(), down_sync(), _git(), _git_worktree(), CompletedProcess (+11 more)

### Community 10 - "Main TUI App Actions"
Cohesion: 0.12
Nodes (11): App, EditWatcherApp, Live Textual view of octo's attributed edits, one bordered block per streak of, Lays out the static header/footer chrome around a scrollable feed of blocks., Kills any active agent tmux sessions and removes their worktrees on exit., Classifies one non-octo history entry into (streak_key, group_key, header label,, Bound to IGNORE_EDITOR_KEY; opens the interactive ignore pattern editor., Bound to BRANCHES_KEY; opens the running-agent-branches overview screen. (+3 more)

### Community 11 - "Commit Line Rendering"
Cohesion: 0.19
Nodes (11): Button, _affected_commit_line(), _entry_line_markup(), ComposeResult, Re-renders this row struck-through and removes its Revert button once this edit, Mounted inside one FileChange's Collapsible; carries the commit + absolute path, Renders one AffectedCommit as a dim summary line for the revert confirmation scr, Builds one FileChangeEntry's header line -- the dim 'sha  +N -M' row shown insid (+3 more)

### Community 12 - "Branches Screen & Worktree Registry"
Cohesion: 0.16
Nodes (12): ListItem, BranchesScreen, Tracks one registered agent worktree's turn-boundary sync status (see worktree_s, Updates the item's internal state. If the widget structure needs to change (e.g., Full-screen overview of every currently-running worktree/branch for the watched, Renders the initial snapshot, then keeps it live for as long as this screen stay, Re-queries live worktrees and updates the list view, avoiding clearing/recreatin, WorktreeItem (+4 more)

### Community 13 - "Antigravity Transcript Tailer"
Cohesion: 0.13
Nodes (12): AntigravityTranscriptTailer, _extract_shell_call(), _parse_antigravity_timestamp(), Converts an Antigravity transcript's whole-second UTC created_at string into epo, Returns (command, cwd) for one Antigravity tool_calls[] entry, if it's the run_c, Incrementally reads new Antigravity CLI activity for one cwd and yields ToolEdit, When this tailer started watching; callers filter poll() results against it to d, Finds the conversation mapped to cwd and reads any new prompt/tool-call activity (+4 more)

### Community 14 - "Poll Result Handling"
Cohesion: 0.19
Nodes (10): PollResult, No findings -- the identity element absorb()/merge() accumulate into., Appends other's findings onto self in place; the mutable half of the merge patte, Concatenates several PollResults' lists into one, preserving order., Drops any edit/move/shell_activity timestamped before timestamp -- keeps a fresh, Reads any new transcript lines since the last call and returns new findings., Reads whole new lines appended to one transcript file since it was last read., Reads any new lines across all rollout transcripts and returns new findings. (+2 more)

### Community 15 - "Claude Transcript Tailer"
Cohesion: 0.12
Nodes (12): build_tailers(), ClaudeTranscriptTailer, _extract_bash_calls(), _extract_tool_result_ids(), Returns (tool_use_id, command) for every Bash tool call in this assistant record, Returns every tool_use_id a non-prompt user record's tool_result blocks respond, Incrementally reads new lines appended to *.jsonl transcripts across every Claud, When this tailer started watching; callers filter poll() results against it to d (+4 more)

### Community 16 - "File Change Entry UI"
Cohesion: 0.20
Nodes (10): Collapsible, FileChangeEntry, FileSection, Every edit to one file within a PromptGroup, collapsed to a single row titled by, Mounts one more edit's row under this file and updates the aggregate title. Moun, Un-mounts one edit's row (e.g. a Human->agent reattribution moving it to another, Re-renders the title line after add_change/remove_change change the line-change, Mounts one committed edit under this group, combined into its file's FileSection (+2 more)

### Community 17 - "Commit Graph Layout"
Cohesion: 0.25
Nodes (12): GraphRow, _is_mainline(), layout(), _next_free_lane(), One commit placed on the synthetic rail graph, ready for the TUI to render as te, True if the commit belongs in lane 0: the baseline, Human commits, and any root-, Returns the lowest lane column >= 1 not currently claimed by an active branch., Places each commit (display order, oldest-first) onto a lane. Mainline commits s (+4 more)

### Community 18 - "Revert Rendering"
Cohesion: 0.14
Nodes (8): _diff_stats(), Re-renders the title struck-through once every edit to this file has been revert, Called by EditWatcherApp._mark_reverted once one of this group's file changes ha, Renders one commit_dirty() batch: octo-generated revert commits (see         Sha, Counts added/removed content lines in a unified diff, ignoring the +++/--- file, Builds a FileChange for a landed commit, resolving its revert status against the, Renders one octo-generated revert commit (see ShadowGitWatcher.revert_file_to /, Retroactively updates a commit's already-mounted FileChangeEntry (tracked in

### Community 19 - "Codex Prompt & Timestamp Parsing"
Cohesion: 0.19
Nodes (10): cwd_related(), _extract_codex_prompt(), _parse_timestamp(), Path, Converts a transcript's ISO-8601 UTC timestamp string into epoch seconds., True if session_cwd and watched_cwd sit on the same directory chain -- equal, or, Looks up which conversation Antigravity last opened for self.cwd or its nearest, Strips the Codex VS Code extension's synthetic IDE-context wrapper, keeping only (+2 more)

### Community 20 - "Cache Size & Clearing"
Cohesion: 0.18
Nodes (8): _dir_size_bytes(), _format_size(), Rewrites the CLEAR_CACHE_KEY footer label with .octo's current on-disk size, the, Bound to CLEAR_CACHE_KEY; hands off to the confirm-then-wipe worker (push_screen, Confirms wiping .octo entirely, then deletes it and rebuilds a fresh shadow repo, Clears every tracking dict/streak-pointer tied to the old shadow repo's commits,, Sums the on-disk size of every file under path, recursively -- used to report th, Renders a byte count as a short human-readable size (e.g. '4.2 MB'), whole numbe

### Community 21 - "Tailer Edit Attribution"
Cohesion: 0.20
Nodes (6): Reattributes agent edits/moves that new transcript lines now explain: for each t, Relabels the shadow commit matching one transcript Edit/Write to its agent, rend, Relabels the shadow commit(s) behind one transcript mv/cp to its agent. Skips mo, True if file_path lives inside self.root -- the shadow repo has no baseline outs, Records up_sync's file-apply result as a SINGLE attributed shadow commit coverin, Appends a reattributed commit to its session+prompt's header group, opening a ne

### Community 22 - "Tool Edit & File Change Models"
Cohesion: 0.27
Nodes (8): One file-writing tool call parsed out of a Claude Code, Antigravity, or Codex se, ToolEdit, FileChange, Renders one commit_dirty() batch of genuine human writes (octo revert commits ar, One committed file edit inside a header group, formatted for display., Builds a note-ready ToolEdit for one verified half of an mv/cp and annotates it., A file edit now committed to the shadow repo, whether flushed from disk or just, SettledEdit

### Community 23 - "History Toggle & Graph Refresh"
Cohesion: 0.22
Nodes (5): Renders the initial graph, then keeps it live for as long as the screen stays op, The commits to graph: all of them when history is loaded, else only this session, Re-queries commits and rebuilds the graph from scratch -- cheap (no diffs on thi, Relabels the `l` footer binding to reflect load state -- same replace-and-refres, `l`: include or exclude prior-run commits, then re-render and relabel the bindin

### Community 24 - "Antigravity Prompt Extraction"
Cohesion: 0.28
Nodes (5): AntigravityPromptExtractor, Recovers the human-typed sentence from an Antigravity user-turn's raw protobuf b, Best-effort guess at the prompt text encoded in payload, or None if nothing plau, True if candidate reads like a typed sentence rather than a path/URI/JSON tool p, Strips protobuf length prefixes and trailing wire garbage off candidate, if a ma

### Community 25 - "History Load/Unload"
Cohesion: 0.29
Nodes (4): Rewrites the TOGGLE_HISTORY_KEY footer label based on current load state., Bound to TOGGLE_HISTORY_KEY; loads or unloads prior-run history based on current, Loads prior-run history and renders it above the baseline., Unloads prior-run history and removes all history blocks from the feed.

### Community 26 - "Startup Splash Screen"
Cohesion: 0.25
Nodes (4): Commits + renders the startup baseline, installs agent hooks, builds tailers, an, Builds the octopus ASCII art markup colored from the active Textual theme (like, Renders a friendly octopus ASCII art welcome message and keeps a handle to it so, Reactive hook Textual calls on every theme switch (including the initial one, be

### Community 27 - "Codex Transcript Tailer"
Cohesion: 0.29
Nodes (4): CodexTranscriptTailer, Incrementally reads new lines appended to Codex CLI's dated rollout-*.jsonl tran, When this tailer started watching; callers filter poll() results against it to d, Returns all prompts from session_id between start_time and end_time (exclusive),

### Community 28 - "Last Prompt Reader"
Cohesion: 0.50
Nodes (4): _extract_user_prompt(), Returns the human-typed prompt text if this record is a real, typed user turn, e, One-shot scan of a Claude Code transcript file for the most recent human-typed p, read_last_prompt()

## Knowledge Gaps
- **1 isolated node(s):** `scratch_test.sh script`
  These have ≤1 connection - possible missing edges or undocumented components.
- **4 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `EditWatcherApp` connect `Main TUI App Actions` to `Shadow Repo Commit & Attribution`, `Agent Notifications & State`, `Commit Graph Screen UI`, `Root Lane & Agent Picker`, `TUI Button Handlers`, `Edit Block Rendering`, `Worktree Sync`, `Branches Screen & Worktree Registry`, `Revert Rendering`, `Cache Size & Clearing`, `Tailer Edit Attribution`, `Tool Edit & File Change Models`, `History Load/Unload`, `Startup Splash Screen`, `Human Copy Unmount`, `Feed Scroll & Mount`, `Turn End Dispatch`?**
  _High betweenness centrality (0.156) - this node is a cross-community bridge._
- **Why does `ShadowGitWatcher` connect `Shadow Repo Commit & Attribution` to `Commit Graph Screen UI`, `Root Lane & Agent Picker`, `TUI Button Handlers`, `Agent Edit Content Parsing`, `Edit Block Rendering`, `Main TUI App Actions`, `Commit Line Rendering`, `Branches Screen & Worktree Registry`, `File Change Entry UI`, `Commit Graph Layout`, `Cache Size & Clearing`, `Tool Edit & File Change Models`?**
  _High betweenness centrality (0.144) - this node is a cross-community bridge._
- **Why does `ToolEdit` connect `Tool Edit & File Change Models` to `Shadow Repo Commit & Attribution`, `Root Lane & Agent Picker`, `Agent Edit Content Parsing`, `Edit Block Rendering`, `Main TUI App Actions`, `Commit Line Rendering`, `Branches Screen & Worktree Registry`, `Antigravity Transcript Tailer`, `Claude Transcript Tailer`, `File Change Entry UI`, `Commit Graph Layout`, `Codex Prompt & Timestamp Parsing`, `Tailer Edit Attribution`, `Turn End Dispatch`?**
  _High betweenness centrality (0.109) - this node is a cross-community bridge._
- **Are the 12 inferred relationships involving `EditWatcherApp` (e.g. with `ShellMoveEdit` and `ToolEdit`) actually correct?**
  _`EditWatcherApp` has 12 INFERRED edges - model-reasoned connections that need verification._
- **Are the 24 inferred relationships involving `ShadowGitWatcher` (e.g. with `CommitDetailScreen` and `CommitGraphScreen`) actually correct?**
  _`ShadowGitWatcher` has 24 INFERRED edges - model-reasoned connections that need verification._
- **Are the 23 inferred relationships involving `ToolEdit` (e.g. with `AgentPickerScreen` and `BranchesScreen`) actually correct?**
  _`ToolEdit` has 23 INFERRED edges - model-reasoned connections that need verification._
- **Are the 21 inferred relationships involving `SettledEdit` (e.g. with `AgentPickerScreen` and `BranchesScreen`) actually correct?**
  _`SettledEdit` has 21 INFERRED edges - model-reasoned connections that need verification._