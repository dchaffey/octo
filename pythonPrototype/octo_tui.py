#!/usr/bin/env python3
"""Textual rendering of octo's attributed edits: one bordered EditBlock per streak of
consecutive same-category commits (a run of consecutive Human flushes, or a run of consecutive
agent edits regardless of which agent/session/prompt), each block containing one header line per
prompt (or human-edit flush) seen in that streak, with every file it touched listed underneath
alongside added/removed line counts and the shadow-repo commit id -- each file's diff is collapsed
by default and expands on click -- updated live as commits land."""

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from agent_detection import AGENT_BINARIES
from agent_watcher import ShellMoveEdit, ToolEdit, build_tailers, read_last_prompt
from hook_installer import detect_and_install_hooks
from pygments import (
    lex,  # tokenizes one diff line's code content for syntax highlighting
)
from pygments.lexers import (  # language guess from a file's extension
    TextLexer,
    get_lexer_for_filename,
)
from pygments.token import (  # token types diff syntax highlighting maps to Theme roles, see _TOKEN_COLOR_ROLES
    Comment,
    Error,
    Keyword,
    Name,
    Number,
    String,
    Token,
)
from pygments.util import (
    ClassNotFound,  # raised by get_lexer_for_filename for an unrecognized extension
)
from root_lane import RootLane
from session_registry import (
    TurnEnded,
    WorktreeRegistration,
    SyncEvent,
    drain_pending_turn_ends,
    drain_pending_worktrees,
    drain_pending_syncs,
)
from shadow_repo import (
    OCTOIGNORE_FILENAME,
    AffectedCommit,
    HistoryEntry,
    SettledEdit,
    ShadowGitWatcher,
)
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches  # raised by query_one when a FileSection's Contents isn't composed yet
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import (
    Button,
    Collapsible,
    DirectoryTree,
    Footer,
    Header,
    ListView,
    ListItem,
    LoadingIndicator,
    Static,
    Tree,
)
from worktree_manager import WorktreeInfo, list_agent_worktrees
from worktree_manager import repo_root as resolve_repo_root
from worktree_sync import down_sync, up_sync

POLL_INTERVAL_SECONDS = 0.5  # how often the app re-scans watched files and transcripts
HUMAN_AGENT_LABEL = (
    "Human"  # agent label shown for filesystem-detected edits no transcript logged
)
INIT_AGENT_LABEL = "Init"  # agent label shown for the startup baseline commit
OCTO_AGENT_LABEL = "octo"  # agent label for octo-generated revert commits; must match shadow_repo._mark_as_revert's Agent trailer
PROMPT_EXCERPT_LEN = 100  # prompt characters shown in a header before truncating
INIT_NOTE_CHANGES = "(startup baseline -- picked up changes made since the last run)"  # shown when the baseline commit wasn't empty
INIT_NOTE_NO_CHANGES = "(startup baseline -- no changes since the last run)"  # shown when the baseline commit was empty
HUMAN_EMPTY_NOTE = "(no matching agent tool call -- likely human edit)"  # empty-note shown on every Human header
OCTO_EMPTY_NOTE = "(reverts a Human edit)"  # empty-note shown on an octo header whose original commit had no prompt to show
AGENT_COLOR_ROLES: dict[
    str, str
] = {  # agent label -> textual.theme.Theme attribute name, so colors follow the active theme
    "Claude": "accent",  # Anthropic's Claude
    "Antigravity": "primary",  # Google's Antigravity
    "Codex": "success",  # OpenAI's Codex
    "octo": "secondary",  # octo system (auto-generated reverts)
}
DEFAULT_AGENT_COLOR_ROLE = "foreground"  # role for labels with no brand mapping (Human, Init, unrecognized agents)
CLEAR_CACHE_KEY = "c"  # footer keybinding that wipes the .octo shadow repo
CLEAR_CACHE_ACTION = (
    "clear_cache"  # action name the CLEAR_CACHE_KEY binding dispatches to
)
TOGGLE_HISTORY_KEY = "l"  # footer keybinding that loads/unloads prior-run history
TOGGLE_HISTORY_ACTION = (
    "toggle_history"  # action name the TOGGLE_HISTORY_KEY binding dispatches to
)
IGNORE_EDITOR_KEY = "i"  # footer keybinding that opens the ignore pattern editor
IGNORE_EDITOR_ACTION = (
    "open_ignore_editor"  # action name the IGNORE_EDITOR_KEY binding dispatches to
)
BRANCHES_KEY = (
    "a"  # footer keybinding that opens the running-agent-branches overview screen
)
BRANCHES_ACTION = "show_branches"  # action name the BRANCHES_KEY binding dispatches to
BRANCHES_REFRESH_SECONDS = (
    1.0  # how often BranchesScreen re-queries `git worktree list` while open
)

_AGENT_DISPLAY_NAMES = {
    binary: agent for agent, binary in AGENT_BINARIES.items()
}  # CLI binary name -> display name, inverse of AGENT_BINARIES, for labeling a worktree's branch by its creating agent
DIFF_ADDED_BG = (
    "on #123a12"  # subtle green background tint applied to a diff's added lines
)
DIFF_REMOVED_BG = (
    "on #3a1212"  # subtle red background tint applied to a diff's removed lines
)
_TOKEN_COLOR_ROLES: dict[
    Token, str
] = {  # Pygments token type -> Theme attribute name (or the
    # literal Rich style "dim"), so diff syntax highlighting follows the active Textual theme the
    # same way AGENT_COLOR_ROLES does for agent labels, instead of a hardcoded Pygments style
    Comment: "dim",
    Keyword: "primary",
    Name.Builtin: "secondary",
    Name.Function: "secondary",
    Name.Class: "secondary",
    Name.Decorator: "warning",
    String: "success",
    Number: "accent",
    Error: "error",
}


_BRACKET_RE = re.compile(
    r"(\\*)\["
)  # run of backslashes (if any) immediately preceding a literal '['


def escape(text: str) -> str:
    """Escapes every literal '[' in arbitrary content (file paths, diff lines, prompt/command text)
    so Textual's markup parser can never misread it as a tag. rich.markup.escape / textual.markup.escape
    only escape a '[' that's already followed by a tag-like char (e.g. '[green'), which is too lenient --
    Textual's own parser raises MarkupError on plain code brackets like '["git", ...]' that pass that
    check clean. Doubles any existing backslashes before the bracket (same convention rich's escape uses)
    so a preceding literal backslash isn't swallowed."""
    return _BRACKET_RE.sub(lambda m: m.group(1) * 2 + "\\[", text)


def _agent_markup(agent: str, theme: Theme) -> str:
    """Renders an agent label in its brand color, resolved from the app's active Textual theme
    (AGENT_COLOR_ROLES) so labels stay legible and on-brand across every theme the user picks,
    falling back to plain foreground for Human/Init/unrecognized agents. Shared by every place an
    agent name is shown -- PromptGroup's header and the revert confirmation screen's
    affected-commit lines -- so they stay visually consistent."""
    role = AGENT_COLOR_ROLES.get(agent, DEFAULT_AGENT_COLOR_ROLE)
    color = getattr(theme, role)
    return f"[bold {color}]{escape(agent)}[/bold {color}]"


def _diff_stats(diff: str) -> tuple[int, int]:
    """Counts added/removed content lines in a unified diff, ignoring the +++/--- file headers."""
    added = sum(
        1
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    removed = sum(
        1
        for line in diff.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )
    return added, removed


def _lexer_for_file(file_path: str):
    """Returns the Pygments lexer matching file_path's extension, or a plain-text lexer (no
    highlighting, just passthrough tokens) if the extension is unrecognized."""
    try:
        return get_lexer_for_filename(file_path, stripnl=False)
    except ClassNotFound:
        return TextLexer(stripnl=False)


def _token_style(token_type, theme: Theme) -> str | None:
    """Looks up token_type's Rich style, walking Pygments' token hierarchy from most specific to
    most general (mirroring how Pygments styles resolve inheritance, e.g. String.Double falling
    back to String) via _TOKEN_COLOR_ROLES; None if no ancestor has a role mapped, in which case
    it's left in the default foreground. A mapped role is either a Theme attribute name (resolved
    off the active theme, so colours follow theme switches) or the literal Rich style "dim"."""
    for candidate in reversed(token_type.split()):
        role = _TOKEN_COLOR_ROLES.get(candidate)
        if role == "dim":
            return "dim"
        if role:
            return getattr(theme, role)
    return None


def _highlight_code(lexer, code: str, theme: Theme) -> str:
    """Tokenizes one diff line's code (lexer state resets per call, so constructs spanning multiple
    lines -- docstrings, block comments -- won't be highlighted correctly; an accepted tradeoff since
    diff lines aren't contiguous source anyway) and renders it as Rich markup, colouring each token by
    _token_style (resolved against theme) and leaving unmatched tokens uncoloured."""
    rendered = []
    for token_type, value in lex(code, lexer):
        value = value.rstrip(
            "\n"
        )  # lex() emits the line's trailing newline as its own token
        if not value:
            continue
        escaped = escape(
            value
        )  # neutralize literal '[' in code content so it isn't parsed as markup
        color = _token_style(token_type, theme)
        rendered.append(f"[{color}]{escaped}[/]" if color else escaped)
    return "".join(rendered)


def _diff_markup(diff: str, file_path: str, theme: Theme) -> str:
    """Renders a unified diff as indented lines with a coloured +/- gutter, a background tint on
    changed lines, and syntax highlighting of each line's code (guessed from file_path's extension,
    coloured from theme via _highlight_code) -- file (+++/---) and hunk (@@) header lines are left
    dim/unhighlighted since they aren't code. Rendered for a Static(markup=True)."""
    lexer = _lexer_for_file(
        file_path
    )  # language guess shared by every code line in this diff
    lines = []
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            lines.append(f"      [dim]{escape(line)}[/dim]")
        elif line.startswith("+"):
            code = _highlight_code(lexer, line[1:], theme)
            lines.append(f"      [{DIFF_ADDED_BG}][bold green]+[/] {code}[/]")
        elif line.startswith("-"):
            code = _highlight_code(lexer, line[1:], theme)
            lines.append(f"      [{DIFF_REMOVED_BG}][bold red]-[/] {code}[/]")
        else:
            code = _highlight_code(
                lexer, line[1:] if line.startswith(" ") else line, theme
            )
            lines.append(f"        {code}")
    return "\n".join(lines)


def _branch_agent_display(branch: str) -> str:
    """Maps an octo-created branch name (AGENT_BRANCH_PREFIX + '<agent_binary>-<tag>', see
    worktree_manager.create_agent_worktree) back to its agent's display name (e.g. 'claude' ->
    'Claude') for coloring via _agent_markup; falls back to the raw binary name if unrecognized."""
    suffix = branch.split("/", 1)[
        -1
    ]  # strip the AGENT_BRANCH_PREFIX namespace (e.g. 'octo/')
    agent_binary = (
        suffix.rsplit("-", 1)[0] if "-" in suffix else suffix
    )  # tag has no dashes, so this is always the agent_binary
    return _AGENT_DISPLAY_NAMES.get(agent_binary, agent_binary)


def _dir_size_bytes(path: Path) -> int:
    """Sums the on-disk size of every file under path, recursively -- used to report the .octo
    shadow repo's total size next to the clear-cache keybinding."""
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _format_size(num_bytes: int) -> str:
    """Renders a byte count as a short human-readable size (e.g. '4.2 MB'), whole numbers for bytes."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


class RevertButton(Button):
    """Mounted inside one FileChange's Collapsible; carries the commit + absolute path a press
    should revert, since the button itself has no other way to reach that context. Also carries
    that change's added/removed line counts, so the owning PromptGroup's remove_change can find
    this specific change's contribution to subtract from its running line-change total. Disabled if
    the commit has already been reverted."""

    def __init__(
        self,
        file_path: str,
        commit: str,
        added: int,
        removed: int,
        is_reverted: bool = False,
    ):
        label = "Revert (already reverted)" if is_reverted else "Revert"
        variant = "default" if is_reverted else "warning"
        super().__init__(label, variant=variant, disabled=is_reverted)
        self.file_path = file_path  # absolute path this button reverts
        self.commit = (
            commit  # commit sha whose pre-commit content file_path is restored to
        )
        self.added = added  # content lines this change added, per _diff_stats
        self.removed = removed  # content lines this change removed, per _diff_stats
        self.is_reverted = is_reverted  # true if this commit was already reverted


class PromptRevertButton(Button):
    """Mounted in a PromptGroup's header; reverts every file that group touched in one confirmed
    action, one prompt/flush at a time instead of file-by-file."""

    def __init__(self, group: "PromptGroup"):
        super().__init__("Revert prompt", variant="warning")
        self.group = group  # owning PromptGroup; targets() reads it live at press time

    def targets(self) -> list[tuple[str, str]]:
        """Returns (file_path, commit) for every non-reverted file change currently mounted in the owning
        group -- queried live, not cached, so a change moved out via remove_change (e.g. a
        Human->agent reattribution) is correctly excluded. Skips reverted commits (idempotent)."""
        return [
            (button.file_path, button.commit)
            for button in self.group.query(RevertButton)
            if not button.is_reverted
        ]


@dataclass
class RevertTarget:
    """One (file, commit) pair a revert action would restore to its pre-commit content, plus the
    later commits on that file it would discard -- what RevertConfirmScreen renders, built by
    EditWatcherApp._confirm_and_revert for both a single RevertButton and a PromptRevertButton's
    whole list of targets."""

    file_path: str  # absolute path this target reverts
    commit: str  # commit sha whose pre-commit content file_path is restored to
    affected: list[
        AffectedCommit
    ]  # later commits touching file_path this revert would discard, newest first


def _affected_commit_line(info: AffectedCommit, theme: Theme) -> str:
    """Renders one AffectedCommit as a dim summary line for the revert confirmation screen."""
    when = time.strftime(
        "%H:%M:%S", time.localtime(info.timestamp)
    )  # wall-clock time of the later commit
    label = info.agent or HUMAN_AGENT_LABEL
    return f"    [dim]{when}[/dim]  {_agent_markup(label, theme)}  [dim]{info.commit[:8]}[/dim]"


class RevertConfirmScreen(ModalScreen[bool]):
    """Modal confirming a revert (one file, or every file a prompt/flush touched) before it
    touches disk: lists every later commit that also changed each target file, since restoring
    the pre-target content silently discards each of those commits' change to that file (no
    git-revert conflict would ever surface it otherwise)."""

    CSS = """
    RevertConfirmScreen {
        align: center middle;
    }
    #revert-dialog {
        width: 70%;
        height: auto;
        max-height: 80%;
        border: thick $error;
        padding: 1 2;
        background: $surface;
    }
    #revert-dialog Static {
        height: auto;
        margin-bottom: 1;
    }
    #revert-dialog Horizontal {
        height: auto;
    }
    #revert-dialog Button {
        height: 1;
        min-height: 1;
        border: none;
        padding: 0 1;
    }
    """

    def __init__(self, targets: list[RevertTarget]):
        super().__init__()
        self.targets = (
            targets  # every (file, commit) this confirmation covers, one section each
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="revert-dialog"):
            title = (
                "Revert 1 file"
                if len(self.targets) == 1
                else f"Revert {len(self.targets)} files"
            )
            yield Static(
                f"[bold]{title}[/bold] to its/their state just before each commit below",
                markup=True,
            )
            for target in self.targets:
                yield Static(
                    f"{escape(target.file_path)}  [dim]{target.commit[:8]}[/dim]",
                    markup=True,
                )
                if target.affected:
                    yield Static(
                        f"  [bold red]{len(target.affected)} later change(s) to this file will be discarded:[/bold red]",
                        markup=True,
                    )
                    for info in target.affected:
                        yield Static(
                            _affected_commit_line(info, self.app.current_theme),
                            markup=True,
                        )
                else:
                    yield Static(
                        "  [dim]No later changes to this file -- this is a clean undo.[/dim]",
                        markup=True,
                    )
            with Horizontal():
                yield Button("Cancel", id="cancel", flat=True)
                yield Button("Revert", id="confirm", variant="error")

    def on_button_pressed(self, event: Button.Pressed):
        """Dismisses the modal with True (proceed) only if the Revert button was pressed."""
        event.stop()  # don't let this bubble to EditWatcherApp.on_button_pressed as a RevertButton press
        self.dismiss(event.button.id == "confirm")


class ClearCacheConfirmScreen(ModalScreen[bool]):
    """Modal confirming a full .octo wipe before it touches disk: deletes every commit, git-notes
    attribution, and history entry recorded so far, with no undo (unlike a file revert, there's no
    prior state left anywhere to recover from)."""

    CSS = """
    ClearCacheConfirmScreen {
        align: center middle;
    }
    #clear-cache-dialog {
        width: 60%;
        height: auto;
        max-height: 80%;
        border: thick $error;
        padding: 1 2;
        background: $surface;
    }
    #clear-cache-dialog Static {
        height: auto;
        margin-bottom: 1;
    }
    #clear-cache-dialog Horizontal {
        height: auto;
    }
    #clear-cache-dialog Button {
        height: 1;
        min-height: 1;
        border: none;
        padding: 0 1;
    }
    """

    def __init__(self, size_bytes: int):
        super().__init__()
        self.size_bytes = size_bytes  # current .octo size, shown so the user knows what they're discarding

    def compose(self) -> ComposeResult:
        with Vertical(id="clear-cache-dialog"):
            yield Static(
                f"[bold]Clear the .octo cache[/bold] ({_format_size(self.size_bytes)})?",
                markup=True,
            )
            yield Static(
                "[dim]This permanently deletes every commit, history entry, and agent "
                "attribution recorded so far. There is no undo.[/dim]",
                markup=True,
            )
            with Horizontal():
                yield Button("Cancel", id="cancel", flat=True)
                yield Button("Clear cache", id="confirm", variant="error")

    def on_button_pressed(self, event: Button.Pressed):
        """Dismisses the modal with True (proceed) only if the Clear cache button was pressed."""
        event.stop()
        self.dismiss(event.button.id == "confirm")


@dataclass
class WorktreeSyncState:
    """Tracks one registered agent worktree's turn-boundary sync status (see worktree_sync.py),
    updated by EditWatcherApp._sync_worktree and rendered by BranchesScreen."""

    path: Path  # worktree clone path, per WorktreeRegistration
    branch: str  # branch checked out there
    agent: str  # display name of the agent that created it (e.g. "Claude")
    last_synced_sha: str | None = (
        None  # worktree HEAD as of the last successful up_sync; None before any turn has landed
    )
    status: str = "ok"  # "ok" | "syncing" | "paused" -- paused means a conflict needs a manual retry
    detail: str = ""  # last SyncResult.detail, shown alongside status
    session_id: str = ""  # most recent Stop-hook session id seen for this worktree, reused by a manual retry
    transcript_path: str = ""  # most recent Stop-hook transcript path seen, for read_last_prompt on a manual retry

    @property
    def status_suffix(self) -> str:
        """Dim status text appended to this worktree's BranchesScreen line; '' when nothing worth
        flagging (ok with no changes to report)."""
        if self.status == "syncing":
            return "  [dim](syncing...)[/dim]"
        if self.status == "paused":
            return f"  [dim](paused: {escape(self.detail)})[/dim]"
        return ""


def _worktree_line(
    info: WorktreeInfo, theme: Theme, sync_state: WorktreeSyncState | None
) -> str:
    """Renders one WorktreeInfo as a two-line entry: agent-colored label (HUMAN_AGENT_LABEL for
    the main working tree, else the creating agent's display name) + branch + short commit sha on
    the first line (plus a sync-status suffix if sync_state says there's something to flag), dim
    worktree path on the second."""
    label = HUMAN_AGENT_LABEL if info.is_main else _branch_agent_display(info.branch)
    branch_display = info.branch or "(detached HEAD)"
    suffix = "  [dim](main working tree)[/dim]" if info.is_main else ""
    status_suffix = sync_state.status_suffix if sync_state is not None else ""
    return (
        f"{_agent_markup(label, theme)}  [bold]{escape(branch_display)}[/bold]  "
        f"[dim]{info.commit[:8]}[/dim]{suffix}{status_suffix}\n"
        f"  [dim]{escape(str(info.path))}[/dim]"
    )


def discover_tui_sessions(repo_root: Path) -> dict[Path, str]:
    from agent_launcher import list_live_tmux_sessions
    tui_sessions = {}
    live_sessions = list_live_tmux_sessions()
    if not live_sessions:
        return tui_sessions
    
    # Get all agent clones for this repo
    worktrees = list_agent_worktrees(repo_root)
    for info in worktrees:
        if info.is_main:
            continue
        parts = info.path.name.rsplit("-", 1)
        if len(parts) == 2:
            agent_name, tag = parts
            safe_agent = agent_name.replace(".", "-").replace(":", "-")
            session_name = f"octo-{safe_agent}-{tag}"
            if session_name in live_sessions:
                tui_sessions[info.path] = session_name
    return tui_sessions


class WorktreeItem(ListItem):
    def __init__(self, info: WorktreeInfo, sync_state: WorktreeSyncState | None, theme: Theme, session_name: str | None):
        super().__init__()
        self.worktree_info = info
        self.sync_state = sync_state
        self.theme = theme
        self.session_name = session_name

    def compose(self) -> ComposeResult:
        text_widget = Static(_worktree_line(self.worktree_info, self.theme, self.sync_state), markup=True, classes="worktree-text")
        if not self.worktree_info.is_main and self.session_name:
            with Horizontal():
                yield text_widget
                yield Button("Attach", id="btn-attach", variant="primary")
        else:
            yield text_widget

    def update_state(self, info: WorktreeInfo, sync_state: WorktreeSyncState | None, theme: Theme, session_name: str | None) -> bool:
        """Updates the item's internal state. If the widget structure needs to change (e.g.
        attaching button presence), returns True to signal that the item should be replaced.
        Otherwise, updates the text static widget in-place and returns False."""
        structure_changed = (
            not self.worktree_info.is_main
            and bool(self.session_name) != bool(session_name)
        ) or self.theme != theme
        
        self.worktree_info = info
        self.sync_state = sync_state
        self.theme = theme
        self.session_name = session_name
        
        if structure_changed:
            return True
            
        try:
            text_widget = self.query_one(".worktree-text", Static)
            text_widget.update(_worktree_line(self.worktree_info, self.theme, self.sync_state))
        except Exception:
            return True
        return False


class AgentPickerScreen(ModalScreen[str | None]):
    """Modal listing available agents to pick for a new session."""

    CSS = """
    AgentPickerScreen {
        align: center middle;
    }
    #agent-picker-dialog {
        width: 40%;
        height: auto;
        border: thick $primary;
        padding: 1 2;
        background: $surface;
    }
    #agent-picker-dialog Static {
        height: auto;
        margin-bottom: 1;
        content-align: center middle;
    }
    #agent-picker-dialog Button {
        width: 100%;
        margin-bottom: 1;
    }
    #agent-picker-dialog Horizontal {
        height: auto;
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="agent-picker-dialog"):
            yield Static("[bold]Launch New Agent[/bold]\nSelect an agent CLI to start:", markup=True)
            for agent in AGENT_BINARIES:
                yield Button(agent, id=f"agent-{agent}")
            with Horizontal():
                yield Button("Cancel", id="cancel", variant="error")

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id and event.button.id.startswith("agent-"):
            agent_name = event.button.id.removeprefix("agent-")
            self.dismiss(agent_name)


class FolderPickerScreen(ModalScreen[Path | None | bool]):
    """Modal showing a directory tree to pick a folder for scoping the agent's work."""

    CSS = """
    FolderPickerScreen {
        align: center middle;
    }
    #folder-picker-dialog {
        width: 70%;
        height: 80%;
        border: thick $primary;
        padding: 1 2;
        background: $surface;
    }
    #folder-picker-dialog Static {
        height: auto;
        margin-bottom: 1;
    }
    #folder-picker-dialog DirectoryTree {
        height: 1fr;
        border: solid $primary-darken-2;
        background: $panel;
        margin-bottom: 1;
    }
    #folder-picker-dialog Horizontal {
        height: auto;
        align: right middle;
    }
    #folder-picker-dialog Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("w", "select_whole", "Whole repo"),
        Binding("s", "select_highlighted", "Scope to folder"),
    ]

    def __init__(self, repo_root: Path):
        super().__init__()
        self.repo_root = repo_root

    def compose(self) -> ComposeResult:
        with Vertical(id="folder-picker-dialog"):
            yield Static("[bold]Select Scoped Folder[/bold]\nChoose a directory to scope the agent's work, or select 'Whole repo':", markup=True)
            yield DirectoryTree(str(self.repo_root), id="folder-tree")
            with Horizontal():
                yield Button("Cancel (Esc)", id="cancel", variant="error")
                yield Button("Whole repo (W)", id="whole-repo", variant="default")
                yield Button("Scope to highlighted (S)", id="confirm", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#folder-tree", DirectoryTree).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "cancel":
            self.dismiss(False)
        elif event.button.id == "whole-repo":
            self.dismiss(None)
        elif event.button.id == "confirm":
            self.action_select_highlighted()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_select_whole(self) -> None:
        self.dismiss(None)

    def action_select_highlighted(self) -> None:
        try:
            tree = self.query_one("#folder-tree", DirectoryTree)
            node = tree.cursor_node
        except Exception:
            self.dismiss(None)
            return

        if node and node.data:
            path = node.data.path
            if path.is_file():
                path = path.parent
            try:
                rel_path = path.relative_to(self.repo_root)
                if str(rel_path) == "." or str(rel_path) == "":
                    self.dismiss(None)
                else:
                    self.dismiss(rel_path)
            except ValueError:
                self.dismiss(None)
        else:
            self.dismiss(None)


class BranchesScreen(Screen[None], inherit_bindings=False):
    """Full-screen overview of every currently-running worktree/branch for the watched repo --
    the main working tree plus every agent worktree (see worktree_manager.list_agent_worktrees) --
    a live snapshot, re-queried on an interval while open since a listed agent session can end
    (and its worktree get cleaned up by agent_launcher's post-exit cleanup) at any moment."""

    CSS = """
    BranchesScreen #branches-list {
        padding: 1 2;
    }
    BranchesScreen #branches-list ListItem {
        height: auto;
        margin-bottom: 1;
        padding: 0 1;
    }
    BranchesScreen #branches-list ListItem Horizontal {
        height: auto;
        align: left middle;
    }
    BranchesScreen #branches-list ListItem .worktree-text {
        width: 1fr;
    }
    BranchesScreen #branches-list ListItem Button {
        height: 1;
        min-height: 1;
        border: none;
        padding: 0 1;
        margin-left: 2;
        width: auto;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding(BRANCHES_KEY, "close", "Back"),
        Binding("enter", "attach", "Attach"),
        Binding("n", "new_agent", "New agent"),
        Binding("q", "quit", "Quit"),
        Binding(TOGGLE_HISTORY_KEY, TOGGLE_HISTORY_ACTION, "Load history", show=False),
        Binding(CLEAR_CACHE_KEY, CLEAR_CACHE_ACTION, "Clear cache", show=False),
        Binding(IGNORE_EDITOR_KEY, IGNORE_EDITOR_ACTION, "Edit ignore", show=False),
        Binding("g", "show_graph", "Graph", show=False),
    ]

    def __init__(self, repo_root: Path):
        super().__init__()
        self.repo_root = repo_root  # repo whose worktrees are listed, per worktree_manager.list_agent_worktrees

    def compose(self) -> ComposeResult:
        yield Header()
        yield ListView(id="branches-list")
        yield Footer()

    def on_mount(self):
        """Renders the initial snapshot, then keeps it live for as long as this screen stays open."""
        self.title = f"octo -- branches -- {self.repo_root}"
        self._refresh()
        self.set_interval(BRANCHES_REFRESH_SECONDS, self._refresh)

    def _refresh(self):
        """Re-queries live worktrees and updates the list view, avoiding clearing/recreating
        items if their paths haven't changed to prevent flickering."""
        worktrees = list_agent_worktrees(self.repo_root)
        list_view = self.query_one("#branches-list", ListView)
        
        current_children = list_view.children
        has_placeholder = len(current_children) == 1 and not isinstance(current_children[0], WorktreeItem)
        
        if not worktrees:
            if has_placeholder:
                return
            list_view.clear()
            list_view.append(ListItem(Static("[dim]No worktrees found.[/dim]", markup=True)))
            return
            
        if has_placeholder:
            list_view.clear()
            current_children = []
            
        # Get the paths of current items
        current_worktree_items = [item for item in current_children if isinstance(item, WorktreeItem)]
        current_paths = [item.worktree_info.path for item in current_worktree_items]
        new_paths = [info.path for info in worktrees]
        
        # If the set of paths or their order has changed, we rebuild the list
        if current_paths != new_paths:
            old_index = list_view.index
            list_view.clear()
            for info in worktrees:
                sync_state = self.app.worktree_states.get(info.path)
                session_name = self.app._tui_sessions.get(info.path)
                list_view.append(
                    WorktreeItem(info, sync_state, self.app.current_theme, session_name)
                )
            if old_index is not None and old_index < len(worktrees):
                list_view.index = old_index
            elif worktrees:
                list_view.index = 0
        else:
            # The paths are identical in order! We can update each item in-place.
            # If any item signals it needs layout recreation (due to button presence/theme change),
            # we rebuild the list.
            needs_rebuild = False
            for item, info in zip(current_worktree_items, worktrees):
                sync_state = self.app.worktree_states.get(info.path)
                session_name = self.app._tui_sessions.get(info.path)
                if item.update_state(info, sync_state, self.app.current_theme, session_name):
                    needs_rebuild = True
            
            if needs_rebuild:
                old_index = list_view.index
                list_view.clear()
                for info in worktrees:
                    sync_state = self.app.worktree_states.get(info.path)
                    session_name = self.app._tui_sessions.get(info.path)
                    list_view.append(
                        WorktreeItem(info, sync_state, self.app.current_theme, session_name)
                    )
                if old_index is not None and old_index < len(worktrees):
                    list_view.index = old_index
                elif worktrees:
                    list_view.index = 0

    def action_close(self):
        self.dismiss(None)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        selected_item = event.item
        if not isinstance(selected_item, WorktreeItem):
            return
        session_name = selected_item.session_name
        if session_name:
            with self.app.suspend():
                subprocess.run(["tmux", "-L", "octo", "attach-session", "-t", session_name], check=False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-attach":
            event.stop()
            ancestor = event.button.parent
            while ancestor is not None and not isinstance(ancestor, WorktreeItem):
                ancestor = ancestor.parent
            if ancestor is None:
                return
            session_name = ancestor.session_name
            if session_name:
                with self.app.suspend():
                    subprocess.run(["tmux", "-L", "octo", "attach-session", "-t", session_name], check=False)

    def action_attach(self):
        list_view = self.query_one("#branches-list", ListView)
        if list_view.index is None or list_view.index >= len(list_view.children):
            return
        selected_item = list_view.children[list_view.index]
        if not isinstance(selected_item, WorktreeItem):
            return
        session_name = selected_item.session_name
        if not session_name:
            return  # not attachable
        with self.app.suspend():
            subprocess.run(["tmux", "-L", "octo", "attach-session", "-t", session_name], check=False)

    @work
    async def action_new_agent(self):
        agent = await self.app.push_screen_wait(AgentPickerScreen())
        if not agent:
            return
        
        folder_res = await self.app.push_screen_wait(FolderPickerScreen(self.repo_root))
        if folder_res is False:
            return  # Cancelled
            
        subpath = str(folder_res) if folder_res else None
        
        from agent_launcher import launch_agent_session
        try:
            session = launch_agent_session(self.repo_root, agent, subpath=subpath)
            self.app._tui_sessions[session.handle.path] = session.session_name
        except Exception as e:
            self.notify(f"Failed to launch agent: {e}", severity="error")
        self._refresh()


@dataclass
class FileChange:
    """One committed file edit inside a header group, formatted for display."""

    file_path: str  # absolute path of the file this commit changed
    added: int  # content lines added by this commit, per _diff_stats
    removed: int  # content lines removed by this commit, per _diff_stats
    commit: str  # shadow-repo commit sha this change landed in; shown truncated
    diff: str  # full unified diff for this commit; shown only once its Collapsible is expanded
    note: str = ""  # optional tier-2 annotation suffix; shell activity or revert info
    is_reverted: bool = False  # true if this commit has been reverted by a later commit
    reverts_commit: str = ""  # if this commit is a revert, the SHA of what it reverted (truncated for display)


def _file_section_title(
    file_path: str, added: int, removed: int, cwd: str, fully_reverted: bool
) -> str:
    """Builds a FileSection's collapsed title: the file path (relative to cwd) plus that file's
    aggregate +added/-removed across every edit to it in the owning group -- shown once per file no
    matter how many commits touched it. Struck through (with an '(all reverted)' note) only once
    every edit to the file has been reverted; a single reverted edit among several strikes just its
    own FileChangeEntry line, not the whole file."""
    file_display = escape(os.path.relpath(file_path, cwd))
    stats_display = f"[green]+{added}[/green] [red]-{removed}[/red]"
    if fully_reverted:
        return f"[strike]{file_display}[/strike]  {stats_display}  [dim](all reverted)[/dim]"
    return f"{file_display}  {stats_display}"


def _entry_line_markup(change: FileChange) -> str:
    """Builds one FileChangeEntry's header line -- the dim 'sha  +N -M' row shown inside a file's
    FileSection above that single edit's diff and Revert button. One line per individual commit, so
    a file edited repeatedly in a prompt lists each edit separately under the one file row. Struck
    through when this edit has been reverted; annotated with the reverted-commit sha when it is
    itself an octo revert; carries any tier-2 shell-activity note otherwise."""
    stats = f"[green]+{change.added}[/green] [red]-{change.removed}[/red]"
    sha = change.commit[:8]
    if change.reverts_commit:
        return f"[dim][bold]{sha}[/bold]  {stats}  reverts [bold]{change.reverts_commit}[/bold][/dim]"
    if change.is_reverted:
        return f"[dim][strike][bold]{sha}[/bold][/strike]  {stats}  reverted[/dim]"
    line = f"[dim][bold]{sha}[/bold]  {stats}[/dim]"
    if change.note:
        line += f"\n{change.note}"
    return line


class FileChangeEntry(Vertical):
    """One committed edit to a file, rendered as a row inside its file's FileSection: a dim
    'sha  +N -M' line, a Revert button that reverts just this edit (omitted for the Init baseline
    group and for already-reverted edits), and the diff (hidden until the section is expanded).
    Several stack under one FileSection when a file is edited repeatedly in a prompt -- the file
    shows once, each edit stays independently revertable."""

    def __init__(self, change: FileChange, theme: Theme, revertable: bool):
        super().__init__()
        self.change = change  # the single edit this row renders and (if revertable) reverts
        self._theme = theme  # active theme, for diff syntax highlighting
        self._revertable = (
            revertable  # false for Init baseline / already-reverted edits -- no Revert button
        )
        self.section: "FileSection | None" = (
            None  # owning FileSection, set by add_entry; lets _mark_reverted reach the group
        )
        self._line_static: Static | None = (
            None  # the dim header line; re-rendered by mark_reverted when this edit is reverted
        )

    def compose(self) -> ComposeResult:
        self._line_static = Static(_entry_line_markup(self.change), markup=True)
        yield self._line_static
        if self._revertable and not self.change.is_reverted:
            yield RevertButton(
                self.change.file_path,
                self.change.commit,
                self.change.added,
                self.change.removed,
                is_reverted=False,
            )
        body = (
            _diff_markup(self.change.diff, self.change.file_path, self._theme)
            if self.change.diff
            else "[dim](no diff)[/dim]"
        )
        yield Static(body, markup=True)

    def mark_reverted(self):
        """Re-renders this row struck-through and removes its Revert button once this edit has been
        reverted by a later commit (change.is_reverted is set by the caller first)."""
        if self._line_static is not None:  # None only before compose() has run
            self._line_static.update(_entry_line_markup(self.change))
        for button in self.query(RevertButton):
            button.remove()


class FileSection(Collapsible):
    """Every edit to one file within a PromptGroup, collapsed to a single row titled by the file
    path and that file's aggregate +/-; expanding lists each edit (FileChangeEntry) with its own
    Revert button. Combining a file's repeated edits into one row is what keeps the feed uncluttered
    while still allowing per-edit revert."""

    def __init__(self, file_path: str, cwd: str, group: "PromptGroup"):
        super().__init__(
            collapsed=True, title=_file_section_title(file_path, 0, 0, cwd, False)
        )
        self.file_path = file_path  # absolute path every entry under this section edits
        self._cwd = cwd  # working dir, for the title's relative path
        self.group = group  # owning PromptGroup, so a reverted entry can reach group.mark_reverted
        self._entries: list[FileChangeEntry] = []  # every edit to this file, in add order
        self._added = 0  # aggregate added lines across entries, for the title
        self._removed = 0  # aggregate removed lines across entries, for the title

    def add_entry(self, entry: FileChangeEntry):
        """Mounts one more edit's row under this file and updates the aggregate title. Mounts into
        the already-composed Contents if it exists, else defers to compose() via _contents_list --
        the same not-yet-composed race PromptGroup handles for its own header (several edits to one
        file can land in one synchronous replay pass before Contents is ever composed)."""
        entry.section = self
        self._entries.append(entry)
        self._added += entry.change.added
        self._removed += entry.change.removed
        try:
            contents = self.query_one(Collapsible.Contents)
        except NoMatches:
            self._contents_list.append(entry)  # not composed yet; compose() picks it up
        else:
            contents.mount(entry)
        self._refresh_title()

    def remove_entry(self, entry: FileChangeEntry) -> bool:
        """Un-mounts one edit's row (e.g. a Human->agent reattribution moving it to another group)
        and updates the aggregate title. Returns True if the section is now empty, so the owning
        PromptGroup can drop it."""
        self._added -= entry.change.added
        self._removed -= entry.change.removed
        self._entries.remove(entry)
        entry.remove()
        if self._entries:
            self._refresh_title()
        return not self._entries

    def refresh_after_revert(self):
        """Re-renders the title struck-through once every edit to this file has been reverted."""
        self._refresh_title()

    def _refresh_title(self):
        """Rebuilds the collapsed title from the current aggregate stats and all-reverted state."""
        fully_reverted = bool(self._entries) and all(
            e.change.is_reverted for e in self._entries
        )
        self.title = _file_section_title(
            self.file_path, self._added, self._removed, self._cwd, fully_reverted
        )


class PromptGroup(Vertical):
    """Renders one prompt (or human-edit flush) within a block: preceding no-change prompts as > lines,
    then a single 'time  agent :: prompt' header line, then each file change as a collapsed-by-default
    section the user expands to see the diff. Several PromptGroups can share one bordered EditBlock."""

    def __init__(
        self,
        timestamp: float,
        agent: str,
        prompt: str,
        empty_note: str = HUMAN_EMPTY_NOTE,
        preceding_prompts: list[tuple[float, str]] | None = None,
        session_id: str = "",
        branch: str = "",
    ):
        super().__init__()
        self.timestamp = timestamp  # epoch seconds of this group's first edit; fixed once the group is created
        self.agent = agent  # agent name (Claude/Antigravity/Codex/Human/Init) every change in this group shares
        self.prompt = prompt  # prompt text in effect for the whole group; empty string for human/init edits
        self.empty_note = (
            empty_note  # dim text shown after '::' in place of a prompt excerpt
        )
        self.preceding_prompts = (
            preceding_prompts or []
        )  # (timestamp, prompt_text) tuples for prompts before this one that didn't cause changes
        self.session_id = session_id  # originating session id if agent-attributed, else ""
        self.branch = branch          # originating worktree branch if agent-attributed, else ""
        self._file_sections: dict[str, FileSection] = (
            {}
        )  # file_path -> its FileSection, so repeated edits to the same file combine into one row
        self._change_count = (
            0  # tracked separately from self.children: Widget.remove() is queued, not
        )
        # synchronous, so is_empty() can't just read the DOM right after a remove_change()
        self._added_total = (
            0  # running sum of added lines across mounted changes; same tracking
        )
        self._removed_total = (
            0  # rationale as _change_count -- not a live DOM query, for the same reason
        )
        self._active_revert_count = 0  # live (non-reverted) RevertButtons in this group; hits 0 -> mark_reverted hides PromptRevertButton
        self._title_static: Static | None = (
            None  # set in compose(); refreshed by _refresh_title() as changes land/move
        )
        self._header: Horizontal | None = (
            None  # set in compose(); lets add_change/mark_reverted mount/unmount the button after compose has run
        )
        self._revert_button: PromptRevertButton | None = (
            None  # the group-level revert button, if currently mounted
        )
        self._show_revert_button = False  # intended button state; add_change/mark_reverted set it, compose() or _sync_revert_button() applies it

    def compose(self) -> ComposeResult:
        """Yields preceding no-change prompts as > lines, then the 'time  agent  +N -M' line (with a
        group-wide revert button alongside it if _show_revert_button is already set -- omitted for the
        Init baseline group since there's no prior state to revert to, and for Human groups since a
        flush can bundle unrelated edits -- only its individual files, added in add_change, are
        revertable there), then the prompt-or-note detail on its own line below; file changes are
        mounted later via add_change as they land. _show_revert_button is decided synchronously by
        add_change before this ever runs (compose() is deferred to an async message), so there's no
        need to retroactively hide the button once it's created here."""
        for _, preceding_prompt in self.preceding_prompts:
            excerpt = (
                (preceding_prompt[:PROMPT_EXCERPT_LEN] + "...")
                if len(preceding_prompt) > PROMPT_EXCERPT_LEN
                else preceding_prompt
            )
            yield Static(f"[dim]> {escape(excerpt)}[/dim]\n", markup=True)
        with Horizontal(classes="prompt-header") as header:
            self._header = header
            self._title_static = Static(self._title_markup(), markup=True)
            yield self._title_static
            if self._show_revert_button:
                self._revert_button = PromptRevertButton(self)
                yield self._revert_button
        yield Static(self._detail_markup(), markup=True)

    def _title_markup(self) -> str:
        """Builds the 'time  agent  +N -M' line, N/M being the running line-change totals."""
        header_time = time.strftime(
            "%H:%M:%S", time.localtime(self.timestamp)
        )  # wall-clock time of this group
        stats = f"[green]+{self._added_total}[/green] [red]-{self._removed_total}[/red]"
        
        # Determine if there's a branch/worktree label to display
        branch_display = ""
        # 1. Check if self.branch is directly set
        if self.branch:
            clean_branch = self.branch.removeprefix("octo/")
            branch_display = f" [dim]({clean_branch})[/dim]"
        # 2. Otherwise, look it up in the app's worktree_states using session_id
        elif self.session_id and hasattr(self, "app") and self.app is not None:
            for state in self.app.worktree_states.values():
                if state.session_id == self.session_id:
                    clean_branch = state.branch.removeprefix("octo/")
                    branch_display = f" [dim]({clean_branch})[/dim]"
                    break

        return f"[bold]{header_time}[/bold]  {_agent_markup(self.agent, self.app.current_theme)}{branch_display}  {stats}"

    def _refresh_title(self):
        """Re-renders the title line after add_change/remove_change change the line-change totals."""
        if (
            self._title_static is not None
        ):  # None only in the brief window before compose() has run
            self._title_static.update(self._title_markup())

    def _detail_markup(self) -> str:
        """Builds the prompt excerpt (or empty_note, for Human/Init groups) shown below the title line."""
        if self.prompt:
            excerpt = (
                (self.prompt[:PROMPT_EXCERPT_LEN] + "...")
                if len(self.prompt) > PROMPT_EXCERPT_LEN
                else self.prompt
            )
            return f"[italic]{escape(excerpt)}[/italic]"
        return f"[dim]{self.empty_note}[/dim]"

    def add_change(self, change: FileChange) -> FileChangeEntry:
        """Mounts one committed edit under this group, combined into its file's FileSection so each
        file shows once no matter how many commits touched it (see FileSection) -- the edit gets its
        own Revert button inside that section, omitted for the Init baseline group (no prior state to
        revert to) and for already-reverted commits. Returns the mounted FileChangeEntry so a later
        reattribution can move it into a different group."""
        revertable = self.agent != INIT_AGENT_LABEL and not change.is_reverted
        section = self._file_sections.get(change.file_path)
        if section is None:
            section = FileSection(change.file_path, self.app.cwd, self)
            self._file_sections[change.file_path] = section
            self.mount(section)
        entry = FileChangeEntry(change, self.app.current_theme, revertable)
        section.add_entry(entry)
        if revertable:
            self._active_revert_count += 1
            if (
                self.agent != HUMAN_AGENT_LABEL
            ):  # Human groups never get a group-level revert button (see compose())
                self._show_revert_button = True
                self._sync_revert_button()
        self._change_count += 1
        self._added_total += change.added
        self._removed_total += change.removed
        self._refresh_title()
        return entry

    def add_external_note(self, file_path: str):
        """Mounts a plain, non-collapsible line noting file_path changed outside the watched tree --
        unlike add_change, there's no shadow-repo commit backing it (out of root => no baseline to
        diff against), so no Collapsible/diff/RevertButton, just the path and a dim annotation.
        Not counted in _change_count: it never lands a commit, so nothing here is ever moved or
        reverted the way add_change's widgets are."""
        note = f"[dim]{escape(file_path)}  (changed outside watched folder)[/dim]"
        self.mount(Static(note, markup=True))

    def mark_reverted(self):
        """Called by EditWatcherApp._mark_reverted once one of this group's file changes has had its
        RevertButton removed; once every change here has been reverted, removes the group's own
        PromptRevertButton too -- there's nothing left it could revert. No-op for Init/Human groups,
        which never set _show_revert_button in the first place."""
        self._active_revert_count -= 1
        if self._active_revert_count <= 0:
            self._show_revert_button = False
            self._sync_revert_button()

    def _sync_revert_button(self):
        """Brings the mounted PromptRevertButton (if any) in line with _show_revert_button.
        Mounting only happens here if compose() has already run (self._header set) -- if not,
        add_change/mark_reverted have already set _show_revert_button correctly and compose() will
        create the button itself the one time it runs, so there's no async gap where a stale query
        could miss or wrongly hide a button that hasn't been realized yet (unlike the old
        query-then-remove approach, which raced compose() during _render_history's synchronous
        replay)."""
        if (
            self._show_revert_button
            and self._revert_button is None
            and self._header is not None
        ):
            self._revert_button = PromptRevertButton(self)
            self._header.mount(self._revert_button)
        elif not self._show_revert_button and self._revert_button is not None:
            self._revert_button.remove()
            self._revert_button = None

    def remove_change(self, entry: FileChangeEntry):
        """Un-mounts one edit previously returned by add_change, e.g. to move it into another group;
        drops its FileSection too if that was the file's only remaining edit. Reads the line counts
        off the edit's own FileChange (always present, unlike its Revert button, which is absent on
        reverted edits)."""
        self._added_total -= entry.change.added
        self._removed_total -= entry.change.removed
        section = entry.section
        assert section is not None, "entry came from add_change, which always sets its section"
        if section.remove_entry(entry):
            del self._file_sections[section.file_path]
            section.remove()
        self._change_count -= 1
        self._refresh_title()

    def is_empty(self) -> bool:
        """True once every edit mounted under this group has been moved out via remove_change."""
        return self._change_count <= 0


class EditBlock(Vertical):
    """Bordered container for one streak of same-category commits: a run of consecutive Human
    flushes (always exactly one shared PromptGroup, no per-commit header) or a run of consecutive
    agent edits (one PromptGroup -- and header -- per distinct session+prompt seen during the
    streak). Groups are mounted top-to-bottom in the order they're first seen."""

    def __init__(self):
        super().__init__()
        self._groups: list[
            PromptGroup
        ] = []  # every PromptGroup mounted in this block, in display order

    def compose(self) -> ComposeResult:
        """No fixed content; PromptGroups are mounted live via add_group as they're seen."""
        return ()

    def add_group(self, group: PromptGroup):
        """Mounts a new header+changes group at the bottom of this block."""
        self.mount(group)
        self._groups.append(group)

    def remove_group(self, group: PromptGroup):
        """Un-mounts an emptied group (all its changes moved out), e.g. after a Human->agent reattribution."""
        group.remove()
        self._groups.remove(group)

    def is_empty(self) -> bool:
        """True once every group mounted in this block has been removed."""
        return len(self._groups) == 0


class RegistrationBlock(Vertical):
    """Bordered container for one worktree registration, styled like EditBlock."""
    pass


class SyncBlock(Vertical):
    """Bordered container for one worktree sync notification, styled like EditBlock."""
    pass


class EditWatcherApp(App):
    """Live Textual view of octo's attributed edits, one bordered block per streak of
    consecutive Human or consecutive agent commits."""

    CSS = """
    EditBlock, RegistrationBlock, SyncBlock {
        border: round $accent;
        margin: 0 1 1 1;
        padding: 0 1;
        height: auto;
    }
    PromptGroup {
        height: auto;
        margin-bottom: 1;
    }
    PromptGroup:last-child {
        margin-bottom: 0;
    }
    .prompt-header {
        height: auto;
    }
    .prompt-header Static {
        width: 1fr;
        content-align: left middle;
    }
    .prompt-header Button {
        min-width: 0;
        width: auto;
    }
    RevertButton, PromptRevertButton {
        height: 1;
        min-height: 1;
        border: none;
        padding: 0 1;
    }
    FileChangeEntry {
        height: auto;
        margin-bottom: 1;
    }
    FileChangeEntry:last-child {
        margin-bottom: 0;
    }
    """
    BINDINGS = [
        (
            "q",
            "quit",
            "Quit",
        ),  # 'q' exits the app, mirroring the console watcher's Ctrl-C
        Binding(
            TOGGLE_HISTORY_KEY, TOGGLE_HISTORY_ACTION, "Load history"
        ),  # label rewritten live by _update_history_binding
        Binding(
            CLEAR_CACHE_KEY, CLEAR_CACHE_ACTION, "Clear cache"
        ),  # label rewritten live by _update_clear_cache_binding
        Binding(
            IGNORE_EDITOR_KEY, IGNORE_EDITOR_ACTION, "Edit ignore"
        ),  # opens interactive .octoignore editor
        Binding(
            BRANCHES_KEY, BRANCHES_ACTION, "Branches"
        ),  # opens the running-agent-branches overview screen
        Binding(
            "g", "show_graph", "Graph"
        ),  # opens the GitKraken-style commit graph screen (see commit_graph_screen.py)
    ]

    def __init__(self, root: Path, cwd: str, agent: str):
        super().__init__()
        self.root = root  # directory whose files are watched for changes
        self.cwd = cwd  # agent cwd whose sessions to tail
        self.agent_filter = agent  # which agent(s) to correlate against ('both' = all)
        self.tailers: list = []  # per-agent transcript tailers, built on mount; polled each tick to relabel Human commits to their agent
        self.pid = os.getpid()  # this process's pid -- matches what register() advertised, so drain_pending_worktrees reads our own inbox
        self.file_watcher = ShadowGitWatcher(
            root
        )  # detects settled fs changes via a shadow git repo
        self._worktree_states: dict[
            Path, WorktreeSyncState
        ] = {}  # worktree path -> its turn-boundary sync status, seeded on registration, updated by _sync_worktree; read by BranchesScreen
        self._tui_sessions: dict[Path, str] = {}  # worktree path -> tmux session name for sessions launched via TUI
        self._root_lane = RootLane()  # serializes commit_dirty() against worktree_sync's up-sync file-apply step, since _sync_worktree runs on its own @work worker
        self._repo_root: Path | None = (
            None  # real git repo root behind self.root, resolved lazily on the first worktree registration (self.root need not itself be a git repo)
        )
        self._groups: dict[
            tuple[str, str], PromptGroup
        ] = {}  # (session_id, prompt) -> header group new matching edits append into
        self._human_commits: dict[
            str, tuple[EditBlock, PromptGroup, Collapsible]
        ] = {}  # commit sha -> (block, group, widget) for a still-Human commit, so a later attribute() can move it in place
        self._current_human_block: EditBlock | None = (
            None  # trailing Human block that new human flushes merge into, until an agent/init event breaks the streak
        )
        self._human_group: PromptGroup | None = (
            None  # the single PromptGroup inside _current_human_block, if any
        )
        self._current_agent_block: EditBlock | None = (
            None  # trailing agent block that new agent prompts merge into, until a human/init event breaks the streak
        )
        self._current_octo_group: PromptGroup | None = (
            None  # trailing octo group that a revert of the SAME original prompt merges into, until any other event breaks the streak
        )
        self._current_octo_key: object = None  # identity of the original PromptGroup _current_octo_group is reverting (see _octo_group_for)
        self._commit_widgets: dict[
            str, tuple[FileChangeEntry, FileChange]
        ] = {}  # full commit sha -> its mounted FileChangeEntry + FileChange, for retroactively marking a commit reverted once a later commit (rendered this session) reverts it
        self._history_anchor: EditBlock | None = (
            None  # fixed insertion point — history blocks mount immediately before this
        )
        self._baseline_sha: str | None = (
            None  # this session's startup baseline commit; history() reads up to but not including this
        )
        self._history_loaded = False  # true if prior-run history is currently mounted
        self._history_blocks: set[EditBlock] = (
            set()
        )  # EditBlocks from history loading, for unloading
        self._history_commit_shas: list[
            str
        ] = []  # commits from history loading, for pruning from _commit_widgets
        self._splash_static: Static | None = (
            None  # set in _render_startup_splash; re-rendered by watch_theme on live theme switches
        )
        self.theme = (
            "flexoki"  # default palette; must be set after _splash_static exists, since
        )
        # assigning it fires watch_theme synchronously; user can still switch via the theme command palette

    def compose(self) -> ComposeResult:
        """Lays out the static header/footer chrome around a scrollable feed of blocks."""
        yield Header()
        yield VerticalScroll(id="feed")
        yield Footer()

    def on_mount(self):
        """Commits + renders the startup baseline, installs agent hooks, builds tailers, and starts polling.
        History loading deferred."""
        self.title = f"octo -- {self.root}"
        baseline = (
            self.file_watcher.initialize()
        )  # commit current on-disk state before any tailer can drain into it
        self._baseline_sha = (
            self.file_watcher.baseline_sha
        )  # capture baseline sha for later history() cutoff
        self._render_startup_splash()  # octopus ASCII art welcome message
        self._history_anchor = self._render_init_baseline(
            baseline
        )  # render baseline and capture its block as insertion point
        self._update_history_binding()
        self._update_clear_cache_binding()
        # Install hooks into the root project's agent configs (idempotent). Hooks drive worktree-lane
        # attribution (via octo run) and desktop notifications; root-lane edits (a claude/codex run
        # directly in the watched tree) are attributed by the transcript tailers below instead.
        results = detect_and_install_hooks(self.root)
        self._tui_sessions = discover_tui_sessions(self.root)
        self.tailers = build_tailers(self.cwd, self.agent_filter)  # content-match agent tool calls against Human commits each tick
        self.set_interval(POLL_INTERVAL_SECONDS, self.poll_once)

    def on_unmount(self):
        """Kills any active agent tmux sessions and removes their worktrees on exit."""
        for path, session_name in list(self._tui_sessions.items()):
            try:
                subprocess.run(["tmux", "-L", "octo", "kill-session", "-t", session_name], check=False)
            except Exception:
                pass
            if path.exists():
                try:
                    shutil.rmtree(path)
                except Exception:
                    pass

    def poll_once(self):
        """One drain pass: processes turn-boundary signals (worktree + root-lane), sync events,
        reattributes root-lane agent edits their transcripts now explain, then commits whatever's
        still dirty on disk."""
        from agent_launcher import list_live_tmux_sessions
        live_sessions = list_live_tmux_sessions()
        ended_paths = []
        for path, session_name in list(self._tui_sessions.items()):
            if session_name not in live_sessions:
                if path.exists():
                    try:
                        shutil.rmtree(path)
                    except Exception:
                        pass
                ended_paths.append(path)
        for path in ended_paths:
            self._tui_sessions.pop(path, None)
            self._worktree_states.pop(path, None)

        for registration in drain_pending_worktrees(self.pid):
            self._render_worktree_registration(registration)
        for turn_ended in drain_pending_turn_ends(self.pid):
            self._handle_turn_ended(turn_ended)
        for sync in drain_pending_syncs(self.pid):
            self._handle_sync_event(sync)
        self._drain_tailers()  # relabel Human commits an agent transcript now explains, before they'd flush as Human
        with self._root_lane:
            self._process_commit_dirty(self.file_watcher.commit_dirty())

    def _drain_tailers(self):
        """Reattributes agent edits/moves that new transcript lines now explain: for each tailer,
        reads its new findings (dropping pre-start history), then relabels the matching shadow
        commit from "Human" to the agent via attribute()/attribute_move(), rendering each in place.
        Runs before commit_dirty() each tick so a write the transcript already explains is annotated
        before it would otherwise flush as an unattributed Human edit (see commit_dirty's docstring).
        This is the sole attribution path for a claude/codex run directly in the watched tree
        (root lane) -- the worktree lane attributes via the up-sync hooks instead."""
        for tailer in self.tailers:
            result = tailer.poll().filter_since(tailer.start_time)  # drop history a freshly started tailer shouldn't act on
            for edit in result.edits:
                self._attribute_tailer_edit(edit)
            for move in result.moves:
                self._attribute_tailer_move(move)

    def _attribute_tailer_edit(self, edit: ToolEdit):
        """Relabels the shadow commit matching one transcript Edit/Write to its agent, rendering it
        in place. Skips edits whose content isn't recoverable from the log, or whose path is outside
        the watched tree (the shadow repo has no baseline there, so attribute() can't address it)."""
        if edit.content is None:
            return  # content not recoverable from the log; no fs fallback, so this edit is unreportable
        if not self._is_within_root(edit.file_path):
            return  # outside the watched tree -- nothing in the shadow repo to attribute
        settled = self.file_watcher.attribute(edit)
        if settled is not None:  # None when no committed content matches (yet) -- next tick's commit_dirty picks it up as Human
            self._render_agent_edit(settled, edit)

    def _attribute_tailer_move(self, move: ShellMoveEdit):
        """Relabels the shadow commit(s) behind one transcript mv/cp to its agent. Skips moves
        touching a path outside the watched tree, for the same baseline reason as _attribute_tailer_edit."""
        if not (self._is_within_root(move.src_path) and self._is_within_root(move.dst_path)):
            return  # a side outside the watched tree -- shadow repo can't address it
        for settled in self.file_watcher.attribute_move(move):
            self._render_agent_edit(settled, move)

    def _is_within_root(self, file_path: str) -> bool:
        """True if file_path lives inside self.root -- the shadow repo has no baseline outside it,
        so attribute()/attribute_move() (which relative_to() the path) can't be called otherwise."""
        return Path(file_path).is_relative_to(self.root)

    def _process_commit_dirty(self, settled_list: list[SettledEdit]):
        """Renders one commit_dirty() batch: octo-generated revert commits (see
        ShadowGitWatcher.revert_file_to) are split out and rendered under their own rolling
        'octo' group, everything else (unexplained human writes -- including any up-synced files
        _land_up_synced_edits hasn't already claimed via attribute_settled) renders as an
        ordinary Human flush. Shared by poll_once's own tick and _sync_worktree's up-sync landing,
        both of which call commit_dirty() from inside self._root_lane."""
        human_settled = []  # commit_dirty() output not explained by a revert -- rendered as an ordinary Human flush
        for settled in settled_list:
            reverted_sha = self.file_watcher.get_reverted_commit(settled.commit)
            if reverted_sha is not None:
                self._render_octo_revert(settled, reverted_sha)
            else:
                human_settled.append(settled)
        self._render_human_flush(human_settled)

    @property
    def worktree_states(self) -> dict[Path, WorktreeSyncState]:
        """Live per-worktree sync status, read by BranchesScreen -- there's nothing on disk that
        says a worktree is paused, so this is the only source of truth for it."""
        return self._worktree_states

    def _render_worktree_registration(self, registration: WorktreeRegistration):
        """Mounts one plain notification line into the feed for a worktree an `octo run` invocation
        just created and registered (see session_registry.register_worktree), and seeds its
        WorktreeSyncState -- from here on, a Stop-hook notification naming this path (see
        _handle_turn_ended) drives its turn-boundary sync. Also resolves self._repo_root on first
        sight, lazily: self.root need not itself be a git repo, but a worktree registration having
        arrived at all proves it (or an ancestor of it) is one (see worktree_manager.repo_root)."""
        if self._repo_root is None:
            self._repo_root = resolve_repo_root(self.root)
        self._worktree_states[registration.worktree_path] = WorktreeSyncState(
            registration.worktree_path, registration.branch, registration.agent
        )
        self._current_human_block = None
        self._current_agent_block = None
        self._human_group = None
        self._current_octo_group = None
        self._current_octo_key = None

        block = RegistrationBlock()
        self.query_one("#feed", VerticalScroll).mount(block)

        line = (
            f"{_agent_markup(registration.agent, self.current_theme)} "
            f"worktree registered: {escape(str(registration.worktree_path))} "
            f"[dim](branch {escape(registration.branch)})[/dim]"
        )
        block.mount(Static(line, markup=True))
        self._scroll_feed_to_end()

    def _handle_sync_event(self, sync: SyncEvent):
        """Mounts one sync notification line into the feed showing the status of a worktree sync."""
        self._current_human_block = None
        self._current_agent_block = None
        self._human_group = None
        self._current_octo_group = None
        self._current_octo_key = None

        block = SyncBlock()
        self.query_one("#feed", VerticalScroll).mount(block)

        status_str = "[green]SUCCESS[/green]" if sync.ok else "[red]FAILED[/red]"
        detail_str = f" ({sync.detail})" if sync.detail else ""
        line = (
            f"{_agent_markup(sync.agent_name, self.current_theme)} "
            f"worktree sync: {escape(str(sync.worktree_path))} -> {status_str}{detail_str}"
        )
        block.mount(Static(line, markup=True))
        self._scroll_feed_to_end()

        # Update the tracked worktree's state in BranchesScreen
        state = self._worktree_states.get(sync.worktree_path)
        if state is not None:
            state.status = "ok" if sync.ok else "paused"
            state.detail = sync.detail

    def _handle_turn_ended(self, turn_ended: TurnEnded):
        """Dispatches a Stop-hook turn-boundary signal: worktree-lane if worktree_path names a
        tracked clone (existing — see _sync_worktree), root-lane if worktree_path is empty
        (new — agent running directly in the watched project)."""
        if turn_ended.worktree_path != Path(""):
            # Worktree lane (existing)
            state = self._worktree_states.get(turn_ended.worktree_path)
            if state is None:
                return
            state.session_id = turn_ended.session_id
            state.transcript_path = turn_ended.transcript_path
            self._sync_worktree(state)
            return

        # Root lane (NEW)
        self._handle_root_lane_turn_end(turn_ended)

    def _handle_root_lane_turn_end(self, turn_ended: TurnEnded):
        """A Stop hook fired in the root project (not a worktree clone).
        Commits everything dirty, attributes to the agent that just finished, and renders
        immediately — no log polling needed.  Models the same commit-then-attribute pattern
        _land_up_synced_edits uses for worktree up-sync, minus the up/down-sync mechanics."""
        prompt = read_last_prompt(turn_ended.transcript_path)
        with self._root_lane:
            settled_list = self.file_watcher.commit_dirty()

        changed_abs = {s.file_path for s in settled_list}
        for settled in settled_list:
            # Skip octo-generated reverts — let _process_commit_dirty handle them
            if self.file_watcher.get_reverted_commit(settled.commit) is not None:
                continue
            self.file_watcher.attribute_settled(
                settled,
                turn_ended.agent_name,
                turn_ended.session_id,
                prompt,
            )
            self._render_agent_edit(
                settled,
                ToolEdit(
                    settled.settled_at,
                    settled.file_path,
                    turn_ended.session_id,
                    prompt,
                    turn_ended.agent_name,
                    None,
                ),
            )

        # Pass any remaining (reverts, unattributed) through the normal pipeline
        self._process_commit_dirty(
            [
                s
                for s in settled_list
                if s.file_path not in changed_abs
                or self.file_watcher.get_reverted_commit(s.commit) is not None
            ]
        )

    @work
    async def _sync_worktree(self, state: WorktreeSyncState):
        """Runs one worktree's turn-boundary sequence: up-sync (land its new commits into root,
        if any), then down-sync (rebase it onto root's latest, root always winning) -- down-sync
        is skipped if up-sync conflicted, per WORKTREE_SYNC_PLAN.md's sequencing. Root-mutating
        work (up-sync's file-apply step and the commit_dirty() it triggers) runs inside
        self._root_lane, so it can't interleave with poll_once's own commit_dirty() tick."""
        assert self._repo_root is not None, (
            "a worktree registration always resolves _repo_root before any turn-boundary signal can arrive"
        )
        state.status = "syncing"
        up_result = up_sync(
            state.path,
            state.branch,
            self.file_watcher.git_dir,
            self._repo_root,
            state.last_synced_sha,
        )
        if not up_result.ok:
            state.status = "paused" if up_result.conflicted else "ok"
            state.detail = up_result.detail
            return
        state.last_synced_sha = up_result.synced_sha
        if up_result.changed_paths:
            with self._root_lane:
                self._land_up_synced_edits(state, up_result.changed_paths)
        down_result = down_sync(state.path, self.file_watcher.git_dir)
        state.status = "paused" if not down_result.ok else "ok"
        state.detail = down_result.detail

    def _land_up_synced_edits(self, state: WorktreeSyncState, changed_paths: list[str]):
        """Records up_sync's file-apply result as a SINGLE attributed shadow commit covering every
        path this up-sync landed (commit_landing), so one agent prompt is one commit-graph row
        instead of one row per file. Renders each landed file through the existing _render_agent_edit
        path off that shared sha, same as any other attributed agent edit. Any unrelated human write
        that settled in the same tick is intentionally left dirty here -- the next poll_once tick's
        commit_dirty() picks it up as an ordinary Human flush, keeping this commit purely the agent's
        prompt."""
        assert self._repo_root is not None, (
            "a worktree registration always resolves _repo_root before any turn-boundary signal can arrive"
        )
        prompt = (
            read_last_prompt(state.transcript_path) if state.transcript_path else ""
        )
        settled_list = self.file_watcher.commit_landing(
            changed_paths, state.agent, state.session_id, prompt, branch=state.branch
        )
        for settled in settled_list:
            self._render_agent_edit(
                settled,
                ToolEdit(
                    settled.settled_at,
                    settled.file_path,
                    state.session_id,
                    prompt,
                    state.agent,
                    None,
                ),
                branch=state.branch,
            )

    def on_button_pressed(self, event: Button.Pressed):
        """Handles a RevertButton (one file) or PromptRevertButton (every file in a group) press
        by handing its target(s) off to _confirm_and_revert: push_screen_wait (used there to await
        the confirmation modal's result) requires a Textual worker, which a plain message handler
        is not, so this just dispatches into one via @work. Skips reverted commits (idempotent revert)."""
        button = event.button
        if isinstance(button, RevertButton):
            if not button.is_reverted:
                self._confirm_and_revert([(button.file_path, button.commit)])
        elif isinstance(button, PromptRevertButton):
            targets = button.targets()
            if targets:  # a group whose changes were all since moved out (e.g. reattributed away) has nothing to revert
                self._confirm_and_revert(targets)

    @work
    async def _confirm_and_revert(self, targets: list[tuple[str, str]]):
        """Previews the later commits touching each target file in a confirmation modal, then --
        only if the user confirms -- restores every target file's pre-commit content straight to
        disk; the next poll_once's commit_dirty() then records each restore as its own ordinary
        Human commit, same as any other write."""
        revert_targets = []
        for file_path, commit in targets:
            rel = str(Path(file_path).relative_to(self.root))
            affected = self.file_watcher.commits_after(rel, commit)
            revert_targets.append(RevertTarget(file_path, commit, affected))
        confirmed = await self.push_screen_wait(RevertConfirmScreen(revert_targets))
        if confirmed:
            for file_path, commit in targets:
                rel = str(Path(file_path).relative_to(self.root))
                self.file_watcher.revert_file_to(rel, commit)

    def _settled_to_filechange(
        self, file_path: str, diff: str, commit: str
    ) -> FileChange:
        """Builds a FileChange for a landed commit, resolving its revert status against the shadow
        repo -- shared by every render path that doesn't already know that status (i.e. not
        _render_octo_revert, which is handed reverted_sha directly by its caller)."""
        added, removed = _diff_stats(
            diff
        )  # +/- line counts for the change's summary line
        is_reverted = self.file_watcher.is_commit_reverted(
            commit
        )  # whether a later commit already undid this one
        reverted = self.file_watcher.get_reverted_commit(
            commit
        )  # sha this commit itself reverts, if any
        return FileChange(
            file_path,
            added,
            removed,
            commit,
            diff,
            is_reverted=is_reverted,
            reverts_commit=reverted[:8] if reverted else "",
        )

    def _finish_render(self, group: "PromptGroup", commit: str, change: FileChange):
        """Mounts change into group, tracks its widget for later lookup/revert-marking, and
        refreshes UI chrome -- the common tail of every single-commit render path."""
        widget = group.add_change(
            change
        )  # Collapsible widget just mounted for this change
        self._commit_widgets[commit] = (
            widget,
            change,
        )  # lets _mark_reverted / revert lookups find it later
        self._scroll_feed_to_end()
        self._update_clear_cache_binding()

    def _render_agent_edit(self, settled: SettledEdit, edit: ToolEdit | ShellMoveEdit, branch: str = ""):
        """Appends a reattributed commit to its session+prompt's header group, opening a new group
        (and a new block, unless the trailing agent streak is still open) on first sight of that
        session+prompt; if the commit was already rendered under a Human block this session, moves
        it out first."""
        self._unmount_human_copy(settled.commit)
        self._current_human_block = (
            None  # any agent edit breaks the trailing Human streak
        )
        self._human_group = None
        self._current_octo_group = None  # ...and the trailing octo streak
        self._current_octo_key = None
        key = (
            edit.session_id,
            edit.prompt,
        )  # same session+prompt reuses one header group across file edits
        group = self._groups.get(key)
        if group is None:
            group = self._open_agent_group(
                settled.settled_at, edit.agent, edit.prompt, key, branch=branch
            )
        change = self._settled_to_filechange(
            settled.file_path, settled.diff, settled.commit
        )
        self._finish_render(group, settled.commit, change)

    def _render_octo_revert(self, settled: SettledEdit, reverted_sha: str):
        """Renders one octo-generated revert commit (see ShadowGitWatcher.revert_file_to /
        _mark_as_revert), grouping it with any other still-trailing revert of the very same
        original prompt (e.g. every file a PromptRevertButton reverted together) into one bordered
        EditBlock via _octo_group_for -- but never with an unrelated revert, agent edit, or Human
        flush, so a block's on-screen position always matches when its revert(s) actually
        happened. Also retroactively marks reverted_sha's own widget (if it's still mounted from
        earlier this session) as reverted -- crossed out, its Revert button removed -- since that
        widget was rendered before this revert existed and so can't have known to render itself
        that way already."""
        self._unmount_human_copy(settled.commit)
        self._current_human_block = (
            None  # a revert commit breaks the trailing Human streak
        )
        self._current_agent_block = None  # ...and the trailing agent streak
        self._human_group = None
        group, self._current_octo_group, self._current_octo_key = self._octo_group_for(
            reverted_sha,
            settled.settled_at,
            self._current_octo_group,
            self._current_octo_key,
        )
        added, removed = _diff_stats(settled.diff)
        change = FileChange(
            settled.file_path,
            added,
            removed,
            settled.commit,
            settled.diff,
            reverts_commit=reverted_sha[:8],
        )
        self._finish_render(group, settled.commit, change)
        self._mark_reverted(reverted_sha)

    def _octo_group_for(
        self,
        reverted_sha: str,
        settled_at: float,
        current_group: "PromptGroup | None",
        current_key: object,
        insert_before: "EditBlock | None" = None,
    ) -> tuple["PromptGroup", "PromptGroup", object]:
        """Resolves which octo PromptGroup a revert of reverted_sha belongs to: reuses
        current_group if it's still reverting the very same original PromptGroup (so a
        multi-file PromptRevertButton press, or several individual reverts of one prompt in a
        row, land in one block), else opens a fresh block+group positioned right here -- reusing
        a stale group instead would freeze this revert at that group's old position, ahead of
        whatever unrelated activity actually happened in between. Shared by the live path
        (_render_octo_revert, threading self._current_octo_group/_current_octo_key through) and
        history replay (_render_history, threading its own local trailing-state instead). The
        original PromptGroup also supplies the header: its prompt (if any -- Human/Init originals
        have none) so the block reads as "reverts: <that prompt>" instead of a generic note.
        insert_before: if provided, mount the new block before this widget (for history insertion).
        Returns (group_to_use, new_current_group, new_current_key) for the caller to carry
        forward into whichever trailing-state it's tracking."""
        original = self._commit_widgets.get(reverted_sha)
        original_group = (
            original[0].section.group if original is not None else None
        )  # entry -> its FileSection -> owning PromptGroup (entry.parent is the section's Contents, not the group)
        key = (
            original_group if original_group is not None else reverted_sha
        )  # sha fallback: never seen this original -- always a fresh block
        if current_key == key and current_group is not None:
            return current_group, current_group, current_key
        block = EditBlock()
        self.query_one("#feed", VerticalScroll).mount(block, before=insert_before)
        original_prompt = original_group.prompt if original_group is not None else ""
        prompt = f"reverts: {original_prompt}" if original_prompt else ""
        note = OCTO_EMPTY_NOTE if not prompt else ""
        group = PromptGroup(settled_at, OCTO_AGENT_LABEL, prompt, empty_note=note)
        block.add_group(group)
        return group, group, key

    def _mark_reverted(self, sha: str):
        """Retroactively updates a commit's already-mounted FileChangeEntry (tracked in
        _commit_widgets) to reflect that it's now reverted: strikes through that one edit's row and
        removes its Revert button, restrikes the whole file's FileSection title if every edit to it
        is now reverted, then tells the owning PromptGroup (see PromptGroup.mark_reverted) so it can
        hide its own PromptRevertButton once every change it holds has been reverted this way.
        No-op if sha was never rendered this session (e.g. reverted in a prior run -- _render_history
        already renders it correctly reverted from git notes) or is already marked, keeping repeat
        calls (e.g. re-reverting back and forth) safe."""
        entry = self._commit_widgets.get(sha)
        if entry is None:
            return
        widget, change = entry
        if change.is_reverted:
            return  # already marked -- nothing new to reflect
        change.is_reverted = True
        widget.mark_reverted()  # strike this edit's row, drop its Revert button
        section = widget.section
        assert section is not None, "a rendered entry always has its section set by add_entry"
        section.refresh_after_revert()  # restrike the file title if every edit to it is now reverted
        section.group.mark_reverted()  # let the group hide its prompt-level revert button once done

    def _open_agent_group(
        self, timestamp: float, agent: str, prompt: str, key: tuple[str, str], branch: str = ""
    ) -> PromptGroup:
        """Starts a header group for a not-yet-seen session+prompt, mounts the group into the
        trailing agent block if that streak is still open, else opens a fresh block first."""
        block = self._current_agent_block
        if block is None:
            block = EditBlock()
            self.query_one("#feed", VerticalScroll).mount(block)
            self._current_agent_block = block
        group = PromptGroup(timestamp, agent, prompt, session_id=key[0], branch=branch)
        block.add_group(group)
        self._groups[key] = group
        return group

    def _unmount_human_copy(self, commit: str):
        """Removes commit's change from wherever a prior Human block rendered it, so a later
        attribute() reattaching a note doesn't leave a duplicate; drops the group/block too if now
        empty (a Human block always holds exactly one group)."""
        entry = self._human_commits.pop(commit, None)
        if entry is None:
            return  # never rendered as Human this session (e.g. attributed within the same poll tick it was written)
        block, group, widget = entry
        group.remove_change(widget)
        if not group.is_empty():
            return  # group still has other changes; nothing to unmount structurally
        block.remove_group(group)
        block.remove()
        if block is self._current_human_block:
            self._current_human_block = None  # trailing block just emptied out; next flush must start a fresh one
            self._human_group = None

    def _render_history(self, entries: list[HistoryEntry]):
        """Reconstructs past-session blocks from the shadow repo's existing commit log, oldest first.

        Groups consecutive entries into streaks rendered as one bordered EditBlock: a run of
        consecutive agent commits (one header group per distinct session+prompt seen in the run),
        a run of consecutive Human commits (one shared header, no per-commit header), a single
        baseline commit (its own block; one commit can span several files), or a run of reverts of
        the very same original prompt (via _octo_group_for -- kept separate from the generic
        streak below since its grouping key is the *original* commit being reverted, not anything
        about the revert commit itself). No batch id survives in git log, so this is best-effort,
        not exact. History blocks are mounted before self._history_anchor so they appear above
        the baseline in the feed.
        """
        streak_key = None  # streak key of the previously processed non-octo entry; a change starts a new block
        block = None  # EditBlock the current non-octo streak is appending into
        groups: dict = {}  # group-key -> PromptGroup, reused while the current non-octo streak stays open
        octo_group = None  # trailing octo PromptGroup a same-prompt revert merges into (see _octo_group_for)
        octo_key = None  # identity of the original PromptGroup octo_group is reverting
        for entry in entries:
            reverted_sha = (
                self.file_watcher.get_reverted_commit(entry.commit)
                if entry.agent == OCTO_AGENT_LABEL
                else None
            )
            if reverted_sha is not None:
                group, octo_group, octo_key = self._octo_group_for(
                    reverted_sha,
                    entry.timestamp,
                    octo_group,
                    octo_key,
                    insert_before=self._history_anchor,
                )
                streak_key = None  # any octo entry breaks whatever generic (agent/human/init) streak was open
            else:
                octo_group = None  # any non-octo entry breaks the trailing octo streak
                octo_key = None
                streak_key_new, group_key, label, prompt, note = (
                    self._history_entry_meta(entry)
                )
                if streak_key_new != streak_key:
                    block = EditBlock()
                    self.query_one("#feed", VerticalScroll).mount(
                        block, before=self._history_anchor
                    )
                    groups = {}  # new streak: forget groups from the previous block
                    streak_key = streak_key_new
                group = groups.get(group_key)
                if group is None:
                    group = PromptGroup(entry.timestamp, label, prompt, empty_note=note, session_id=entry.session_id, branch=entry.branch)
                    block.add_group(group)
                    groups[group_key] = group
            change = self._settled_to_filechange(
                entry.file_path, entry.diff, entry.commit
            )
            widget = group.add_change(change)
            self._commit_widgets[entry.commit] = (widget, change)
            self._history_blocks.add(
                group.parent
            )  # track which blocks were loaded so we can unload them
            self._history_commit_shas.append(
                entry.commit
            )  # track which commits were loaded for _commit_widgets cleanup

    @staticmethod
    def _history_entry_meta(entry: HistoryEntry):
        """Classifies one non-octo history entry into (streak_key, group_key, header label, prompt,
        empty-note). Octo-revert entries are handled separately by _render_history before this is
        ever called -- see _octo_group_for."""
        if entry.is_baseline:
            return (
                ("init", entry.commit),
                ("init", entry.commit),
                INIT_AGENT_LABEL,
                "",
                INIT_NOTE_CHANGES,
            )
        if entry.agent:
            return (
                ("agent",),
                (entry.session_id, entry.prompt),
                entry.agent,
                entry.prompt,
                "",
            )
        return ("human",), ("human",), HUMAN_AGENT_LABEL, "", HUMAN_EMPTY_NOTE

    def _splash_markup(self) -> str:
        """Builds the octopus ASCII art markup colored from the active Textual theme (like
        _agent_markup), so both the initial render and watch_theme's refresh stay in sync. Body
        uses $accent to match EditBlock's border color; eyes use $primary to stand out from it."""
        theme = (
            self.app.current_theme
        )  # active Textual theme -- source of role colors below
        body = (
            theme.accent
        )  # octopus body color, matched to EditBlock's border ($accent)
        eyes = theme.primary  # eye highlight color, distinct from the body color
        return f"""
[bold {body}]   ,---.[/bold {body}]
[bold {body}]  ([/bold {body}][bold {eyes}] @ @ [/bold {eyes}][bold {body}])[/bold {body}]  [bold {body}]OCTO[/bold {body}]
[bold {body}]   ).-.([/bold {body}]   [dim]Tracking files across agent sessions[/dim]
[bold {body}]  '/|||\\`[/bold {body}]
[bold {body}]    '|`[/bold {body}]
        """.strip()

    def _render_startup_splash(self):
        """Renders a friendly octopus ASCII art welcome message and keeps a handle to it so
        watch_theme can recolor it in place when the user switches themes later."""
        self._splash_static = Static(self._splash_markup(), markup=True)
        self.query_one("#feed", VerticalScroll).mount(self._splash_static)

    def watch_theme(self, theme_name: str) -> None:
        """Reactive hook Textual calls on every theme switch (including the initial one, before
        _render_startup_splash has run) -- recolors the already-mounted splash to match."""
        if self._splash_static is not None:
            self._splash_static.update(self._splash_markup())

    def _render_init_baseline(self, baseline: list[SettledEdit]) -> EditBlock:
        """Renders the startup baseline commit as its own block: changes it picked up, or a 'no changes' notice."""
        note = INIT_NOTE_CHANGES if baseline else INIT_NOTE_NO_CHANGES
        block = EditBlock()
        self.query_one("#feed", VerticalScroll).mount(
            block
        )  # block must be mounted before add_group can mount into it
        group = PromptGroup(time.time(), INIT_AGENT_LABEL, "", empty_note=note)
        block.add_group(group)
        self._current_human_block = None  # baseline breaks any trailing Human streak
        self._human_group = None
        self._current_agent_block = None  # baseline breaks any trailing agent streak
        self._current_octo_group = None  # ...and the trailing octo streak
        self._current_octo_key = None
        for settled in baseline:
            change = self._settled_to_filechange(
                settled.file_path, settled.diff, settled.commit
            )
            widget = group.add_change(change)
            self._commit_widgets[settled.commit] = (widget, change)
        self._scroll_feed_to_end()
        return block  # return the block so callers can use it as an insertion anchor

    def _render_human_flush(self, flushed: list[SettledEdit]):
        """Renders one commit_dirty() batch of genuine human writes (octo revert commits are
        filtered out by poll_once and rendered via _render_octo_revert instead), merging into the
        trailing Human block's single header group if that streak hasn't been broken by an
        agent/init event since, so consecutive human flushes render as one growing block instead of
        one block apiece."""
        if not flushed:
            return  # nothing was dirty on disk this poll tick
        self._current_agent_block = (
            None  # any human flush breaks the trailing agent streak
        )
        self._current_octo_group = None  # ...and the trailing octo streak
        self._current_octo_key = None
        block = self._current_human_block
        group = self._human_group
        if block is None:
            block = EditBlock()
            self.query_one("#feed", VerticalScroll).mount(
                block
            )  # block must be mounted before add_group can mount into it
            group = PromptGroup(flushed[0].settled_at, HUMAN_AGENT_LABEL, "")
            block.add_group(group)
            self._current_human_block = block
            self._human_group = group
        for settled in flushed:
            added, removed = _diff_stats(settled.diff)
            change = FileChange(
                settled.file_path, added, removed, settled.commit, settled.diff
            )
            widget = group.add_change(change)
            self._human_commits[settled.commit] = (block, group, widget)
            self._commit_widgets[settled.commit] = (widget, change)
        self._scroll_feed_to_end()
        self._update_clear_cache_binding()

    def _update_history_binding(self):
        """Rewrites the TOGGLE_HISTORY_KEY footer label based on current load state."""
        label = "Unload history" if self._history_loaded else "Load history"
        self._bindings.key_to_bindings[TOGGLE_HISTORY_KEY] = [
            Binding(TOGGLE_HISTORY_KEY, TOGGLE_HISTORY_ACTION, label)
        ]
        self.refresh_bindings()

    def _update_clear_cache_binding(self):
        """Rewrites the CLEAR_CACHE_KEY footer label with .octo's current on-disk size, then asks
        the Footer to redraw -- Binding is frozen, so this replaces the app's own bindings-map entry
        rather than mutating one in place."""
        label = (
            f"Clear cache ({_format_size(_dir_size_bytes(self.file_watcher.git_dir))})"
        )
        self._bindings.key_to_bindings[CLEAR_CACHE_KEY] = [
            Binding(CLEAR_CACHE_KEY, CLEAR_CACHE_ACTION, label)
        ]
        self.refresh_bindings()

    def _scroll_feed_to_end(self):
        """Scrolls the feed all the way to its bottom -- scroll_visible on a single group only
        scrolls enough to bring that widget into view, which can stop short of the true end (e.g.
        the block border/spacing trailing it), reading as "doesn't scroll all the way". Scrolling
        the container itself to its actual end has no such shortfall."""
        self.query_one("#feed", VerticalScroll).scroll_end(animate=False)

    def action_toggle_history(self):
        """Bound to TOGGLE_HISTORY_KEY; loads or unloads prior-run history based on current state."""
        if self._history_loaded:
            self._unload_history()
        else:
            self._load_history()

    def _load_history(self):
        """Loads prior-run history and renders it above the baseline."""
        assert not self._history_loaded, "history already loaded"
        history = self.file_watcher.history(until=self._baseline_sha)
        self._render_history(history)
        self._history_loaded = True
        self._update_history_binding()

    def _unload_history(self):
        """Unloads prior-run history and removes all history blocks from the feed."""
        assert self._history_loaded, "history not currently loaded"
        for block in self._history_blocks:
            block.remove()
        for sha in self._history_commit_shas:
            self._commit_widgets.pop(sha, None)
        self._history_blocks.clear()
        self._history_commit_shas.clear()
        self._history_loaded = False
        self._update_history_binding()

    def action_clear_cache(self):
        """Bound to CLEAR_CACHE_KEY; hands off to the confirm-then-wipe worker (push_screen_wait,
        used there to await the confirmation modal's result, requires a Textual worker, which a
        plain action method is not)."""
        self._confirm_and_clear_cache()

    def action_open_ignore_editor(self):
        """Bound to IGNORE_EDITOR_KEY; opens the interactive ignore pattern editor."""
        self._open_ignore_editor()

    def action_show_branches(self):
        """Bound to BRANCHES_KEY; opens the running-agent-branches overview screen."""
        self.push_screen(BranchesScreen(self.root))

    def action_show_graph(self):
        """Bound to 'g'; opens the commit-graph screen. Imported lazily to break the import cycle
        (commit_graph_screen imports this module's helpers at load time)."""
        from commit_graph_screen import CommitGraphScreen

        self.push_screen(CommitGraphScreen(self.file_watcher, self._baseline_sha))

    def _find_editor(self) -> str | None:
        """Returns the first available editor from (nvim, emacs, vim, vi, nano), or None if none found."""
        for editor in ("nvim", "emacs", "vim", "vi", "nano"):
            if shutil.which(editor):
                return editor
        return None

    @work
    async def _open_ignore_editor(self):
        """Spawns an external editor (nvim/emacs/vim/vi/nano) to edit .octoignore, then reinitializes.
        The file itself is already guaranteed to exist by this point -- initialize() (run at startup
        and after a cache clear) creates it from the preset template if missing."""
        editor = self._find_editor()
        if not editor:
            return
        octoignore = self.root / OCTOIGNORE_FILENAME
        try:
            with self.app.suspend():
                subprocess.run([editor, str(octoignore)], check=False)
            self.file_watcher.initialize()
        except Exception:
            pass

    @work
    async def _confirm_and_clear_cache(self):
        """Confirms wiping .octo entirely, then deletes it and rebuilds a fresh shadow repo +
        baseline, resetting every bit of render/tracking state that pointed at the old one's
        commits -- a clear is a hard reset, not a revert, so nothing here is recoverable after."""
        assert self.file_watcher.git_dir.is_dir(), (
            "clear cache requires an initialized shadow repo"
        )
        size = _dir_size_bytes(self.file_watcher.git_dir)
        confirmed = await self.push_screen_wait(ClearCacheConfirmScreen(size))
        if not confirmed:
            return
        shutil.rmtree(self.file_watcher.git_dir)
        self.query_one("#feed", VerticalScroll).remove_children()
        self._reset_render_state()
        self.file_watcher = ShadowGitWatcher(self.root)
        baseline = self.file_watcher.initialize()
        self._baseline_sha = self.file_watcher.baseline_sha
        self._history_anchor = self._render_init_baseline(baseline)
        self._update_history_binding()
        self._update_clear_cache_binding()

    def _reset_render_state(self):
        """Clears every tracking dict/streak-pointer tied to the old shadow repo's commits, so the
        fresh one rendered after a cache clear starts from a blank slate instead of e.g. trying to
        move a now-gone commit out of a stale Human group."""
        self._groups = {}
        self._human_commits = {}
        self._current_human_block = None
        self._human_group = None
        self._current_agent_block = None
        self._history_anchor = None
        self._baseline_sha = None
        self._history_loaded = False
        self._history_blocks = set()
        self._history_commit_shas = []


def run(root: Path, cwd: str, agent: str):
    """Entry point used by octo.py's --tui flag to launch the Textual app."""
    EditWatcherApp(root, cwd, agent).run()
