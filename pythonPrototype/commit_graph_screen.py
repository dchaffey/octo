#!/usr/bin/env python3
"""GitKraken-style commit-graph screen for octo, plus the per-commit detail screen it drills into.

The graph is drawn as text rail glyphs (│ ● ├ ─) inside Static widgets -- there is no canvas widget,
so this mirrors how every other octo screen renders (markup strings). Lanes and merge edges are
synthesized by commit_graph.layout() from each commit's Branch: attribution, since the root .octo
history is strictly linear (see COMMIT_GRAPH_PLAN.md). Kept in its own module so octo_tui only wires
it up; octo_tui is imported lazily from the app's action to avoid an import cycle."""

import time

import commit_graph
from octo_tui import (
    AGENT_COLOR_ROLES,
    DEFAULT_AGENT_COLOR_ROLE,
    HUMAN_AGENT_LABEL,
    INIT_AGENT_LABEL,
    PROMPT_EXCERPT_LEN,
    TOGGLE_HISTORY_ACTION,
    TOGGLE_HISTORY_KEY,
    PromptGroup,
    _agent_markup,
    escape,
)
from shadow_repo import GraphCommit, ShadowGitWatcher
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

GRAPH_KEY = "g"  # footer keybinding that opens the commit graph screen (also closes it)
GRAPH_ACTION = "show_graph"  # action name GRAPH_KEY dispatches to on EditWatcherApp
GRAPH_REFRESH_SECONDS = 1.0  # how often CommitGraphScreen re-queries + re-renders while open
NODE_GLYPH = "●"  # ● -- a commit node sitting in its lane
BAR_GLYPH = "│"  # │ -- a lane's vertical rail on a row where nothing sits in it
MERGE_INTO_GLYPH = "├"  # ├ -- mainline (lane 0) receiving a branch merge on a merge row
MERGE_LINE_GLYPH = "─"  # ─ -- horizontal connector between mainline and the merging node


def _effective_label(commit: GraphCommit) -> str:
    """The agent label a commit renders under: Init for the baseline, its agent name if attributed,
    else Human -- matches how the feed labels the same commit, so colors stay consistent."""
    if commit.is_baseline:
        return INIT_AGENT_LABEL
    return commit.agent or HUMAN_AGENT_LABEL


def _label_color(label: str, theme) -> str:
    """Resolves an agent label to its brand color off the active theme (AGENT_COLOR_ROLES), the
    same mapping _agent_markup uses -- so a lane's node color matches its label color elsewhere."""
    return getattr(theme, AGENT_COLOR_ROLES.get(label, DEFAULT_AGENT_COLOR_ROLE))


def _relative_time(ts: float) -> str:
    """Renders a commit's age as a short relative string (e.g. '5m', '2h', '3d'), '<now' under a
    minute -- the trailing dim column on each graph row."""
    delta = max(0.0, time.time() - ts)
    if delta < 60:
        return "now"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    return f"{int(delta // 86400)}d"


def _rail_markup(row: commit_graph.GraphRow, width: int, theme) -> str:
    """Builds the leading rail-glyph columns for one row: a colored node in the commit's lane,
    vertical bars for every other active lane, and -- on a merge row -- a horizontal connector from
    the merging node back to lane 0 (├─●) showing that branch's work landing into root."""
    color = _label_color(_effective_label(row.commit), theme)
    cells: list[str] = []
    for col in range(width):
        if col == row.lane:
            cells.append(f"[bold {color}]{NODE_GLYPH}[/]")
        elif row.is_merge and col == 0:
            cells.append(f"[{color}]{MERGE_INTO_GLYPH}[/]")
        elif row.is_merge and 0 < col < row.lane:
            cells.append(f"[{color}]{MERGE_LINE_GLYPH}[/]")
        elif col in row.active_lanes:
            cells.append(f"[dim]{BAR_GLYPH}[/dim]")
        else:
            cells.append(" ")
    return "".join(cells)


def _summary_text(commit: GraphCommit) -> str:
    """The trailing text a row shows after its sha + label: the prompt excerpt for an agent commit,
    else a dim baseline/human note -- escaped so code brackets in a prompt can't break markup."""
    if commit.is_baseline:
        return "[dim](startup baseline)[/dim]"
    if commit.prompt:
        excerpt = (
            commit.prompt[:PROMPT_EXCERPT_LEN] + "..."
            if len(commit.prompt) > PROMPT_EXCERPT_LEN
            else commit.prompt
        )
        return f"[italic]{escape(excerpt)}[/italic]"
    return "[dim](human edit)[/dim]"


class CommitRow(Static):
    """One clickable graph row: rail glyphs, then short-sha + agent label + prompt excerpt + a file
    count and relative age. Clicking (or Enter while focused) opens the commit detail screen. Stores
    its sha + the watcher so it can build the detail screen without reaching back into the graph."""

    can_focus = True  # so keyboard users can Tab to a row and press Enter, not just click

    def __init__(self, row: commit_graph.GraphRow, width: int, watcher: ShadowGitWatcher, theme):
        super().__init__(self._build_markup(row, width, theme), markup=True)
        self.sha = row.commit.sha  # commit this row opens a detail screen for on click/Enter
        self.watcher = watcher  # shared ShadowGitWatcher, passed straight to the detail screen

    @staticmethod
    def _build_markup(row: commit_graph.GraphRow, width: int, theme) -> str:
        """Builds the full markup for one row: rail glyphs + a colored short-sha, agent label,
        prompt/summary, and a dim file-count + age tail."""
        rail = _rail_markup(row, width, theme)
        label = _effective_label(row.commit)
        merge_tag = "  [dim](merged)[/dim]" if row.is_merge else ""
        files = f"[dim]{row.commit.file_count} file(s)[/dim]"
        age = f"[dim]{_relative_time(row.commit.timestamp)}[/dim]"
        return (
            f"{rail}  [dim]{row.commit.sha[:8]}[/dim]  {_agent_markup(label, theme)}  "
            f"{_summary_text(row.commit)}{merge_tag}  {files}  {age}"
        )

    def _open_detail(self):
        """Pushes the detail screen for this row's commit -- shared by click and Enter."""
        self.app.push_screen(CommitDetailScreen(self.watcher, self.sha))

    def on_click(self):
        self._open_detail()

    def on_key(self, event):
        """Enter opens the detail screen when this row has keyboard focus."""
        if event.key == "enter":
            event.stop()
            self._open_detail()


class CommitGraphScreen(Screen[None]):
    """Full-screen GitKraken-style rail graph of the shadow repo's commits, one clickable CommitRow
    per commit, re-rendered on an interval so it stays live. Scope mirrors the feed: this session's
    commits only by default, with `l` toggling prior-run history in and out (its own local toggle,
    since the app's history state is about the feed)."""

    CSS = """
    CommitGraphScreen #graph {
        padding: 1 2;
    }
    CommitGraphScreen #graph CommitRow {
        height: auto;
        margin-bottom: 1;
    }
    CommitGraphScreen #graph CommitRow:focus {
        background: $boost;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding(GRAPH_KEY, "close", "Back"),
        Binding(TOGGLE_HISTORY_KEY, TOGGLE_HISTORY_ACTION, "Load history"),
    ]

    def __init__(self, watcher: ShadowGitWatcher, baseline_sha: str | None):
        super().__init__()
        self.watcher = watcher  # shadow repo the graph reads commits from
        self.baseline_sha = baseline_sha  # this session's baseline; commits at/after it are "this session"
        self._history_loaded = False  # true when prior-run commits are included (toggled by `l`)

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="graph")
        yield Footer()

    def on_mount(self):
        """Renders the initial graph, then keeps it live for as long as the screen stays open."""
        self.title = "octo -- commit graph"
        self._update_history_binding()
        self._refresh()
        self.set_interval(GRAPH_REFRESH_SECONDS, self._refresh)

    def _visible_commits(self) -> list[GraphCommit]:
        """The commits to graph: all of them when history is loaded, else only this session's
        (from the baseline commit onward), mirroring the feed's default scope."""
        commits = self.watcher.graph_commits()
        if self._history_loaded or self.baseline_sha is None:
            return commits
        idx = next((i for i, c in enumerate(commits) if c.sha == self.baseline_sha), 0)
        return commits[idx:]  # baseline onward -- this session, baseline included

    def _refresh(self):
        """Re-queries commits and rebuilds the graph from scratch -- cheap (no diffs on this path),
        same rebuild-every-tick approach as BranchesScreen, so no diffing against the prior render."""
        rows = commit_graph.layout(self._visible_commits())
        container = self.query_one("#graph", VerticalScroll)
        container.remove_children()
        if not rows:
            container.mount(Static("[dim]No commits yet.[/dim]", markup=True))
            return
        width = max(max(r.active_lanes) for r in rows) + 1  # widest lane across rows, so columns align
        theme = self.app.current_theme
        for row in rows:
            container.mount(CommitRow(row, width, self.watcher, theme))

    def _update_history_binding(self):
        """Relabels the `l` footer binding to reflect load state -- same replace-and-refresh trick
        the app's _update_history_binding uses, scoped to this screen's own bindings map."""
        label = "Unload history" if self._history_loaded else "Load history"
        self._bindings.key_to_bindings[TOGGLE_HISTORY_KEY] = [
            Binding(TOGGLE_HISTORY_KEY, TOGGLE_HISTORY_ACTION, label)
        ]
        self.refresh_bindings()

    def action_toggle_history(self):
        """`l`: include or exclude prior-run commits, then re-render and relabel the binding."""
        self._history_loaded = not self._history_loaded
        self._update_history_binding()
        self._refresh()

    def action_close(self):
        self.dismiss(None)


class CommitDetailScreen(Screen[None]):
    """Full details of one commit, its files rendered exactly as the feed does -- a per-file
    collapsible diff with a Revert button -- by reusing the app's PromptGroup / _settled_to_filechange
    machinery. Revert presses bubble to the app's on_button_pressed -> _confirm_and_revert flow
    unchanged, since RevertButton carries its own file_path + commit."""

    CSS = """
    CommitDetailScreen #detail {
        padding: 1 2;
    }
    CommitDetailScreen #detail PromptGroup {
        height: auto;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Back"),
    ]

    def __init__(self, watcher: ShadowGitWatcher, sha: str):
        super().__init__()
        self.watcher = watcher  # shadow repo the per-file diffs are read from (lazily, here)
        self.sha = sha  # commit this screen details

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="detail")
        yield Footer()

    def on_mount(self):
        """Fetches this commit's per-file diffs and renders them as the feed's collapsible+revert
        widgets under a single seeded PromptGroup -- reverted files come through already crossed out,
        since _settled_to_filechange resolves revert status against the shadow repo. An empty commit
        (an --allow-empty baseline that picked up no changes) has no files and just shows a note."""
        entries = self.watcher.commit_detail(self.sha)
        container = self.query_one("#detail", VerticalScroll)
        if not entries:
            self.title = f"octo -- {self.sha[:8]}"
            container.mount(Static("[dim](this commit changed no files)[/dim]", markup=True))
            return
        first = entries[0]  # every entry shares this commit's agent/prompt/session/branch attribution
        label = _effective_label(
            GraphCommit(first.commit, first.timestamp, first.agent, first.session_id,
                        first.prompt, first.branch, first.is_baseline, len(entries))
        )
        self.title = f"octo -- {label} -- {self.sha[:8]}"
        group = PromptGroup(
            first.timestamp, label, first.prompt,
            session_id=first.session_id, branch=first.branch,
        )
        container.mount(group)
        for entry in entries:
            change = self.app._settled_to_filechange(entry.file_path, entry.diff, entry.commit)
            group.add_change(change)

    def action_close(self):
        self.dismiss(None)
