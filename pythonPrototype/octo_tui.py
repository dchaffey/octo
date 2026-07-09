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
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import (
    Button,
    Collapsible,
    DirectoryTree,
    Footer,
    Header,
    LoadingIndicator,
    Static,
    Tree,
)

from agent_watcher import ShellActivity, ShellMoveEdit, ToolEdit, build_tailers
from shadow_repo import AffectedCommit, HistoryEntry, OCTOIGNORE_FILENAME, SettledEdit, ShadowGitWatcher

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
SHELL_ACTIVITY_SLACK_SECONDS = 3.0  # tolerance added past a shell command's end time when matching it to a settled commit
SHELL_ACTIVITY_TTL_SECONDS = 30.0  # how long a buffered ShellActivity marker survives before it's pruned as stale
SHELL_ACTIVITY_COMMAND_EXCERPT_LEN = (
    40  # command characters shown in a tier-2 annotation before truncating
)
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
DIFF_ADDED_BG = (
    "on #123a12"  # subtle green background tint applied to a diff's added lines
)
DIFF_REMOVED_BG = (
    "on #3a1212"  # subtle red background tint applied to a diff's removed lines
)
_TOKEN_COLOR_ROLES: dict[Token, str] = {  # Pygments token type -> Theme attribute name (or the
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


def _shell_activity_note(matches: list[ShellActivity]) -> str:
    """Builds a dim, non-attributing summary suffix for a FileChange whose settle time coincided
    with unparseable agent shell activity -- informational only, never a real attribution (see
    ShellActivity's docstring); '' if there's nothing to flag."""
    if not matches:
        return ""
    first = matches[0]
    excerpt = (
        first.command
        if len(first.command) <= SHELL_ACTIVITY_COMMAND_EXCERPT_LEN
        else first.command[:SHELL_ACTIVITY_COMMAND_EXCERPT_LEN] + "..."
    )
    extra = f" (+{len(matches) - 1} more)" if len(matches) > 1 else ""
    return f"  [yellow]possibly {escape(first.agent)}: `{escape(excerpt)}`{extra} -- unverified[/yellow]"


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


@dataclass
class HumanAggregate:
    """One current Human-row aggregate for repeated edits to the same file."""

    first_commit: str
    latest_commit: str
    widget: Collapsible
    change: FileChange


def _change_summary_markup(change: FileChange, cwd: str) -> str:
    """Builds a FileChange's Collapsible title: file path (relative to cwd, struck through +
    '(reverted)' if change.is_reverted) plus added/removed stats on the first line, then a note
    suffix (revert-of info takes precedence over a tier-2 shell-activity hint) on its own line
    below, if there is one. Shared by add_change (initial render) and
    EditWatcherApp._mark_reverted (retroactive re-render once a live commit gets reverted)."""
    note_suffix = ""
    if change.reverts_commit:
        note_suffix = f"\n  [dim]reverts [bold]{change.reverts_commit}[/bold][/dim]"
    elif change.note:
        note_suffix = f"\n{change.note}"

    file_display = escape(os.path.relpath(change.file_path, cwd))
    stats_display = f"[green]+{change.added}[/green] [red]-{change.removed}[/red]"
    if change.is_reverted:
        file_display = f"[strike]{file_display}[/strike]"
        note_suffix = f"\n  [dim][bold]{change.commit[:8]}[/bold] reverted[/dim]"

    return f"{file_display}  {stats_display}{note_suffix}"


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
        return f"[bold]{header_time}[/bold]  {_agent_markup(self.agent, self.app.current_theme)}  {stats}"

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

    def add_change(self, change: FileChange) -> Collapsible:
        """Mounts one more committed file change as a collapsed-by-default section under this group,
        with a Revert button (see RevertButton) alongside its diff -- omitted for the Init baseline
        group, since there's no prior state to revert to; omitted for reverted commits. Returns the
        mounted widget so a later reattribution can move it into a different group."""
        summary = _change_summary_markup(change, self.app.cwd)
        body_markup = (
            _diff_markup(change.diff, change.file_path, self.app.current_theme)
            if change.diff
            else "[dim](no diff)[/dim]"
        )
        children = [Static(body_markup, markup=True)]
        # only show revert button for non-reverted, non-init commits
        if self.agent != INIT_AGENT_LABEL and not change.is_reverted:
            children.insert(
                0,
                RevertButton(
                    change.file_path,
                    change.commit,
                    change.added,
                    change.removed,
                    is_reverted=False,
                ),
            )
            self._active_revert_count += 1
            if (
                self.agent != HUMAN_AGENT_LABEL
            ):  # Human groups never get a group-level revert button (see compose())
                self._show_revert_button = True
                self._sync_revert_button()
        collapsible = Collapsible(*children, title=summary, collapsed=True)
        self.mount(collapsible)
        self._change_count += 1
        self._added_total += change.added
        self._removed_total += change.removed
        self._refresh_title()
        return collapsible

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

    def remove_change(self, widget: Collapsible):
        """Un-mounts one file change previously returned by add_change, e.g. to move it into another group."""
        revert_button = widget.query_one(
            RevertButton
        )  # read its line counts before it's gone, to subtract them below
        self._added_total -= revert_button.added
        self._removed_total -= revert_button.removed
        widget.remove()
        self._change_count -= 1
        self._refresh_title()

    def is_empty(self) -> bool:
        """True once every file-change Collapsible mounted under this group has been moved out via remove_change."""
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


class EditWatcherApp(App):
    """Live Textual view of octo's attributed edits, one bordered block per streak of
    consecutive Human or consecutive agent commits."""

    CSS = """
    EditBlock {
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
    ]

    def __init__(self, root: Path, cwd: str, agent: str):
        super().__init__()
        self.root = root  # directory whose files are watched for changes
        self.cwd = cwd  # agent cwd whose sessions to tail
        self.agent_filter = agent  # which agent(s) to correlate against ('both' = all)
        self.file_watcher = ShadowGitWatcher(
            root
        )  # detects settled fs changes via a shadow git repo
        self.tailers: list = []  # per-agent transcript tailers, built on mount
        self._groups: dict[
            tuple[str, str], PromptGroup
        ] = {}  # (session_id, prompt) -> header group new matching edits append into
        self._human_commits: dict[
            str, tuple[EditBlock, PromptGroup, Collapsible]
        ] = {}  # latest displayed Human commit sha -> (block, group, widget), so a later attribute() can move it in place
        self._current_human_block: EditBlock | None = (
            None  # trailing Human block that new human flushes merge into, until an agent/init event breaks the streak
        )
        self._human_group: PromptGroup | None = (
            None  # the single PromptGroup inside _current_human_block, if any
        )
        self._human_aggregates: dict[
            str, HumanAggregate
        ] = {}  # absolute file path -> current aggregate row inside the trailing Human group
        self._agent_aggregates: dict[
            tuple[tuple[str, str], str], HumanAggregate
        ] = {}  # ((session_id, prompt), absolute file path) -> current aggregate row for that agent group
        self._current_agent_block: EditBlock | None = (
            None  # trailing agent block that new agent prompts merge into, until a human/init event breaks the streak
        )
        self._current_octo_group: PromptGroup | None = (
            None  # trailing octo group that a revert of the SAME original prompt merges into, until any other event breaks the streak
        )
        self._current_octo_key: object = None  # identity of the original PromptGroup _current_octo_group is reverting (see _octo_group_for)
        self._shell_activity: list[
            ShellActivity
        ] = []  # buffered tier-2 markers not yet matched (or expired), see _matching_shell_activity
        self._commit_widgets: dict[
            str, tuple[Collapsible, FileChange]
        ] = {}  # full commit sha -> its mounted Collapsible + FileChange, for retroactively marking a commit reverted once a later commit (rendered this session) reverts it
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
        self._last_prompt_timestamp: dict[
            str, float
        ] = {}  # session_id -> timestamp of last prompt that resulted in commits
        self._splash_static: Static | None = (
            None  # set in _render_startup_splash; re-rendered by watch_theme on live theme switches
        )
        self.theme = "flexoki"  # default palette; must be set after _splash_static exists, since
        # assigning it fires watch_theme synchronously; user can still switch via the theme command palette

    def compose(self) -> ComposeResult:
        """Lays out the static header/footer chrome around a scrollable feed of blocks."""
        yield Header()
        yield VerticalScroll(id="feed")
        yield Footer()

    def on_mount(self):
        """Commits + renders the startup baseline, builds tailers, and starts polling. History loading deferred."""
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
        self.tailers = build_tailers(self.cwd, self.agent_filter)
        self.set_interval(POLL_INTERVAL_SECONDS, self.poll_once)

    def poll_once(self):
        """One drain pass: reattributes any agent edits/moves explained by new transcript lines,
        buffers unparseable shell activity for later correlation, then commits whatever's still
        dirty on disk -- octo-generated revert commits (see ShadowGitWatcher.revert_file_to) are
        split out and rendered under their own rolling 'octo' group, everything else (unexplained
        human writes) renders as an ordinary Human flush."""
        added_shell_activity = False  # tracks whether this cycle buffered anything, so pruning only runs when it can matter
        for tailer in self.tailers:
            result = tailer.poll().filter_since(
                tailer.start_time
            )  # drop history a freshly started tailer shouldn't act on
            for edit in result.edits:
                if edit.content is None:
                    continue  # content not recoverable from the log; no fs fallback, so this edit is unreportable
                if not self._is_within_root(edit.file_path):
                    self._render_external_edit(edit, edit.file_path)
                    continue  # outside the watched tree -- shadow repo has no baseline for it, so nothing to attribute
                settled = self.file_watcher.attribute(edit)
                if settled is not None:
                    self._render_agent_edit(settled, edit)
            for move in result.moves:
                if not (
                    self._is_within_root(move.src_path)
                    and self._is_within_root(move.dst_path)
                ):
                    self._render_external_edit(move, move.dst_path)
                    continue
                for settled in self.file_watcher.attribute_move(move):
                    self._render_agent_edit(settled, move)
            if result.shell_activity:
                self._shell_activity.extend(result.shell_activity)
                added_shell_activity = True
        if added_shell_activity:
            self._prune_shell_activity()
        human_settled = []  # commit_dirty() output not explained by a revert -- rendered as an ordinary Human flush
        for settled in self.file_watcher.commit_dirty():
            reverted_sha = self.file_watcher.get_reverted_commit(settled.commit)
            if reverted_sha is not None:
                self._render_octo_revert(settled, reverted_sha)
            else:
                human_settled.append(settled)
        self._render_human_flush(human_settled)

    def _is_within_root(self, file_path: str) -> bool:
        """True if file_path lives inside self.root -- the shadow repo has no baseline outside
        it, so attribute()/attribute_move() can't be called on such a path (relative_to() would raise)."""
        return Path(file_path).is_relative_to(self.root)

    def _render_external_edit(self, edit: "ToolEdit | ShellMoveEdit", file_path: str):
        """Notes an agent write outside the watched tree under its session+prompt's header group,
        alongside whatever it touched inside cwd -- reuses the same group lookup/creation as
        _render_agent_edit so the two interleave in one place, but as a plain line (see
        PromptGroup.add_external_note): there's no shadow-repo baseline outside root, so no
        diff/revert affordance backs it, just a record that it happened instead of a crash."""
        self._current_human_block = None  # an agent action breaks the trailing Human streak, same as _render_agent_edit
        self._human_group = None
        self._human_aggregates = {}
        self._current_octo_group = None  # ...and the trailing octo streak
        self._current_octo_key = None
        key = (
            edit.session_id,
            edit.prompt,
        )  # same session+prompt reuses one header group across file edits
        group = self._groups.get(key)
        if group is None:
            group = self._open_agent_group(edit.timestamp, edit.agent, edit.prompt, key)
        group.add_external_note(file_path)
        self._scroll_feed_to_end()

    def _prune_shell_activity(self):
        """Drops buffered ShellActivity markers old enough that they can no longer plausibly
        explain a not-yet-settled commit, so the buffer doesn't grow unbounded over a long session."""
        cutoff = time.time() - SHELL_ACTIVITY_TTL_SECONDS
        self._shell_activity = [
            a for a in self._shell_activity if a.end_timestamp >= cutoff
        ]

    def _matching_shell_activity(self, settled_at: float) -> list[ShellActivity]:
        """Returns buffered shell-activity markers whose call window plausibly explains a commit
        that settled at settled_at -- a hint for the TUI, never consumed as real attribution."""
        return [
            a
            for a in self._shell_activity
            if a.timestamp
            <= settled_at
            <= a.end_timestamp + SHELL_ACTIVITY_SLACK_SECONDS
        ]

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

    def _render_agent_edit(self, settled: SettledEdit, edit: ToolEdit | ShellMoveEdit):
        """Appends a reattributed commit to its session+prompt's header group, opening a new group
        (and a new block, unless the trailing agent streak is still open) on first sight of that
        session+prompt; if the commit was already rendered under a Human block this session, moves
        it out first."""
        self._unmount_human_copy(settled.commit)
        self._current_human_block = (
            None  # any agent edit breaks the trailing Human streak
        )
        self._human_group = None
        self._human_aggregates = {}
        self._current_octo_group = None  # ...and the trailing octo streak
        self._current_octo_key = None
        key = (
            edit.session_id,
            edit.prompt,
        )  # same session+prompt reuses one header group across file edits
        group = self._groups.get(key)
        if group is None:
            group = self._open_agent_group(
                settled.settled_at, edit.agent, edit.prompt, key
            )
        aggregate_key = (key, settled.file_path)
        aggregate = self._agent_aggregates.get(aggregate_key)
        if aggregate is None:
            change = self._settled_to_filechange(
                settled.file_path, settled.diff, settled.commit
            )
            widget = group.add_change(change)
            self._agent_aggregates[aggregate_key] = HumanAggregate(
                settled.commit, settled.commit, widget, change
            )
        else:
            self._commit_widgets.pop(aggregate.latest_commit, None)
            rel = str(Path(settled.file_path).relative_to(self.root))
            diff = self.file_watcher.combined_diff(
                rel, aggregate.first_commit, settled.commit
            )
            change = self._settled_to_filechange(
                settled.file_path, diff, settled.commit
            )
            group.remove_change(aggregate.widget)
            widget = group.add_change(change)
            aggregate.latest_commit = settled.commit
            aggregate.widget = widget
            aggregate.change = change
        self._commit_widgets[settled.commit] = (widget, change)
        self._scroll_feed_to_end()
        self._update_clear_cache_binding()

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
        self._human_aggregates = {}
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
        original_group = original[0].parent if original is not None else None
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
        """Retroactively updates a commit's already-mounted widget (tracked in _commit_widgets) to
        reflect that it's now reverted: crosses out its file path and removes its Revert button, then
        tells the owning PromptGroup (see PromptGroup.mark_reverted) so it can hide its own
        PromptRevertButton once every change it holds has been reverted this way.
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
        widget.title = _change_summary_markup(change, self.cwd)
        for button in widget.query(RevertButton):
            button.remove()
        group = (
            widget.parent
        )  # PromptGroup this change's Collapsible was mounted into, per add_change
        assert isinstance(group, PromptGroup)
        group.mark_reverted()

    def _open_agent_group(
        self, timestamp: float, agent: str, prompt: str, key: tuple[str, str]
    ) -> PromptGroup:
        """Starts a header group for a not-yet-seen session+prompt: fetches any preceding prompts that
        didn't cause changes, mounts the group into the trailing agent block if that streak is still
        open, else opens a fresh block first."""
        block = self._current_agent_block
        if block is None:
            block = EditBlock()
            self.query_one("#feed", VerticalScroll).mount(block)
            self._current_agent_block = block
        session_id, _ = key  # unpack (session_id, prompt_text) key
        preceding_prompts = self._get_preceding_prompts(session_id, agent, timestamp)
        group = PromptGroup(
            timestamp, agent, prompt, preceding_prompts=preceding_prompts
        )
        block.add_group(group)
        self._groups[key] = group
        self._last_prompt_timestamp[session_id] = timestamp
        return group

    def _get_preceding_prompts(
        self, session_id: str, agent: str, timestamp: float
    ) -> list[tuple[float, str]]:
        """Queries tailers for prompts in session_id between last-seen-prompt and current timestamp,
        dropping the final one -- since timestamp is the tool-call time, the prompt that triggered
        this very edit always precedes it and so always lands as the range's last entry, but it's
        shown separately as this group's own header/detail line, not as a preceding skipped one.
        A session not yet seen this run falls back to the tailer's own start_time, not 0 -- otherwise
        a long-lived/resumed session's very first group this run would pull in its entire prompt
        history instead of just what's happened since octo started watching."""
        for tailer in self.tailers:
            agent_name = tailer.__class__.__name__.replace("TranscriptTailer", "")
            if agent_name == agent:
                last_ts = self._last_prompt_timestamp.get(session_id, tailer.start_time)
                prompts = tailer.get_prompts_in_range(session_id, last_ts, timestamp)
                return prompts[:-1] if prompts else prompts
        return []

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
            self._human_aggregates = {}

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
                    group = PromptGroup(entry.timestamp, label, prompt, empty_note=note)
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
        theme = self.app.current_theme  # active Textual theme -- source of role colors below
        body = theme.accent  # octopus body color, matched to EditBlock's border ($accent)
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
        self._human_aggregates = {}
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
        one block apiece; repeated edits to the same file inside that still-open Human block
        collapse into one row showing the net diff from the first such commit through the latest."""
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
            self._human_aggregates = {}
        for settled in flushed:
            aggregate = self._human_aggregates.get(settled.file_path)
            note = _shell_activity_note(
                self._matching_shell_activity(settled.settled_at)
            )
            if aggregate is None:
                added, removed = _diff_stats(settled.diff)
                change = FileChange(
                    settled.file_path, added, removed, settled.commit, settled.diff, note
                )
                widget = group.add_change(change)
                self._human_aggregates[settled.file_path] = HumanAggregate(
                    settled.commit, settled.commit, widget, change
                )
            else:
                self._human_commits.pop(aggregate.latest_commit, None)
                self._commit_widgets.pop(aggregate.latest_commit, None)
                rel = str(Path(settled.file_path).relative_to(self.root))
                diff = self.file_watcher.combined_diff(
                    rel, aggregate.first_commit, settled.commit
                )
                added, removed = _diff_stats(diff)
                change = FileChange(
                    settled.file_path, added, removed, settled.commit, diff, note
                )
                group.remove_change(aggregate.widget)
                widget = group.add_change(change)
                aggregate.latest_commit = settled.commit
                aggregate.widget = widget
                aggregate.change = change
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
        self._human_aggregates = {}
        self._agent_aggregates = {}
        self._current_agent_block = None
        self._shell_activity = []
        self._history_anchor = None
        self._baseline_sha = None
        self._history_loaded = False
        self._history_blocks = set()
        self._history_commit_shas = []
        self._last_prompt_timestamp = {}


def run(root: Path, cwd: str, agent: str):
    """Entry point used by octo.py's --tui flag to launch the Textual app."""
    EditWatcherApp(root, cwd, agent).run()
