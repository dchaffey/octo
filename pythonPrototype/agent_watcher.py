#!/usr/bin/env python3
"""Polls agent session records (Claude Code, Antigravity, Codex) for file-writing tool calls."""

import glob
import json
import os
import re
import shlex
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"  # base dir Claude Code stores all session transcripts under
ANTIGRAVITY_ROOT = Path.home() / ".gemini" / "antigravity-cli"  # base dir the Antigravity CLI (agy) stores conversation state under
CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"  # base dir the Codex CLI stores its dated rollout-*.jsonl transcripts under

EDIT_TOOL_NAMES = ("Edit", "Write", "NotebookEdit")  # Claude Code tools that write file content to disk
ANTIGRAVITY_WRITE_TOOL_NAMES = ("replace_file_content", "multi_replace_file_content", "write_to_file")  # Antigravity tools that write file content to disk
ANTIGRAVITY_SHELL_TOOL_NAME = "run_command"  # Antigravity tool name for its shell-exec tool (the Bash-equivalent); may rm/mv/cp files
ANTIGRAVITY_USER_STEP_TYPE = 14  # steps.step_type value Antigravity uses for a human-typed turn; reverse-engineered from a live session, no public schema exists
CODEX_USER_MESSAGE_EVENT = "user_message"    # event_msg payload.type Codex logs for a human-typed turn
CODEX_PATCH_EVENT = "patch_apply_end"         # event_msg payload.type Codex logs once an apply_patch tool call finishes
CODEX_IDE_REQUEST_MARKER = "## My request for Codex:\n"  # VS Code extension prefixes typed prompts with IDE context up to this marker
CODEX_EXEC_TOOL_NAME = "exec_command"  # Codex function_call name for its shell-exec tool (the Bash-equivalent, separate from apply_patch)
CLAUDE_SHELL_TOOL_NAME = "Bash"  # Claude Code tool name for its shell-exec tool

SHELL_SEGMENT_SEPARATORS = {";", "&&", "||", "|"}  # operators that just sequence/pipe independent commands, safe to split on
SHELL_UNSAFE_OPERATORS = {">", ">>", "<", "<<", "&", "(", ")"}  # operators whose effect on file state we don't model; presence voids a segment
SHELL_RM_FLAGS = {"-f", "-v", "--force", "--verbose"}  # rm flags that don't change which files are affected
SHELL_GLOB_CHARS = set("*?[")  # presence in a path arg means the shell -- not us -- decides which files are touched


@dataclass
class EditContent:
    """The literal content transform one tool call made, precise enough to replay without reading disk."""
    replacements: list[tuple[str, str]] | None  # ordered (old_fragment, new_fragment) pairs applied in sequence; None if full_content is used instead
    full_content: str | bytes | None               # complete new file content: str for a real tool edit's reported content, bytes when shadow_repo.attribute_move() replays a moved/copied file's exact prior bytes verbatim (may be binary); None if replacements is used instead

    def apply(self, prior_content: str) -> str | None:
        """Reconstructs the file's new full content from a candidate prior content, or None if a
        replacement's old_fragment isn't found there -- callers use this to probe whether prior_content
        is really the state this edit was made against, without crashing on a mismatched guess."""
        if self.full_content is not None:
            return self.full_content  # tool call rewrote the whole file; prior content is irrelevant
        assert self.replacements is not None, "EditContent must carry either full_content or replacements"
        content = prior_content
        for old_fragment, new_fragment in self.replacements:
            if old_fragment not in content:
                return None  # this candidate prior content isn't what the edit was actually made against
            content = content.replace(old_fragment, new_fragment, 1)
        return content


@dataclass
class ToolEdit:
    """One file-writing tool call parsed out of a Claude Code, Antigravity, or Codex session."""
    timestamp: float   # epoch seconds the tool call was logged by the agent
    file_path: str      # absolute path the tool call wrote to
    session_id: str      # transcript/conversation UUID the call belongs to
    prompt: str           # most recent human-typed prompt text seen in that session before this call
    agent: str             # human-readable name of the agent that made the call, e.g. "Claude" or "Antigravity"
    content: EditContent | None  # exact content transform if the log carried enough to reconstruct it; else None (caller must fall back to reading disk)


@dataclass
class ShellMoveEdit:
    """One mv/cp shell command parsed from an agent's Bash/exec tool call. Unlike ToolEdit, its
    destination content is never in the log -- shadow_repo.attribute_move() verifies it against
    the shadow repo's own recorded history (what the source path held right before) instead of
    trusting anything derived from the command text alone."""
    timestamp: float    # epoch seconds the shell command was issued
    session_id: str       # transcript/conversation UUID the call belongs to
    prompt: str             # most recent human-typed prompt text seen in that session before this call
    agent: str                # human-readable agent name, e.g. "Claude" or "Codex"
    src_path: str               # absolute source path
    dst_path: str                  # absolute destination path
    src_removed: bool                 # True for mv (source deleted by the move), False for cp (source untouched)


@dataclass
class ShellActivity:
    """One shell command an agent ran that couldn't be safely parsed into a verified file edit.
    Purely a live-session UI hint (see edit_watcher_tui.py) that some agent activity coincided
    with an otherwise-unexplained commit -- never persisted as attribution, never revertable."""
    timestamp: float        # epoch seconds the command was issued
    end_timestamp: float      # epoch seconds the command's result was logged
    session_id: str             # transcript/conversation UUID the call belongs to
    agent: str                    # human-readable agent name
    prompt: str                     # most recent human-typed prompt text seen before this call
    command: str                      # raw command text, shown verbatim in the UI hint


@dataclass
class PollResult:
    """One tailer poll's findings: verified edits/moves ready for attribution, plus unverified
    shell activity markers the TUI renders as a hint only."""
    edits: list[ToolEdit]                # Edit/Write/NotebookEdit/apply_patch/rm calls, content-verified
    moves: list[ShellMoveEdit]             # mv/cp calls; verified later against shadow repo history
    shell_activity: list[ShellActivity]      # unparseable shell commands; annotation-only, no revert

    @classmethod
    def empty(cls) -> "PollResult":
        """No findings -- the identity element absorb()/merge() accumulate into."""
        return cls([], [], [])

    def absorb(self, other: "PollResult"):
        """Appends other's findings onto self in place; the mutable half of the merge pattern used
        by every tailer loop that accumulates one PollResult per line/call parsed."""
        self.edits.extend(other.edits)
        self.moves.extend(other.moves)
        self.shell_activity.extend(other.shell_activity)

    @classmethod
    def merge(cls, results: list["PollResult"]) -> "PollResult":
        """Concatenates several PollResults' lists into one, preserving order."""
        merged = cls.empty()
        for result in results:
            merged.absorb(result)
        return merged

    def filter_since(self, timestamp: float) -> "PollResult":
        """Drops any edit/move/shell_activity timestamped before timestamp -- keeps a freshly
        started tailer from acting on transcript history that predates it."""
        return PollResult(
            edits=[e for e in self.edits if e.timestamp >= timestamp],
            moves=[m for m in self.moves if m.timestamp >= timestamp],
            shell_activity=[a for a in self.shell_activity if a.timestamp >= timestamp],
        )


def _resolve_shell_path(token: str, cwd: str) -> str:
    """Resolves a shell command's path argument to an absolute path, relative to the call's cwd."""
    path = Path(token)
    return str(path if path.is_absolute() else Path(cwd) / path)


def _tokenize_shell_command(command: str) -> list[str] | None:
    """Splits a shell command string into words and operators, or None if quoting is unbalanced
    (e.g. an unterminated quote) and thus not safely parseable at all."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _parse_shell_segment(tokens: list[str], cwd: str) -> tuple | None:
    """Recognizes one rm/mv/cp invocation in a token segment. Returns None -- not just for
    commands that aren't rm/mv/cp, but for anything about a recognized one that makes its file
    effect ambiguous (unrecognized flag, glob, wrong arg count, an unsafe redirect/pipe operator
    inside the segment) -- so the caller falls back to an unverified marker rather than guess."""
    if not tokens or any(token in SHELL_UNSAFE_OPERATORS for token in tokens):
        return None
    name, *args = tokens
    flags = [arg for arg in args if arg.startswith("-") and arg != "-"]
    paths = [arg for arg in args if arg not in flags]
    if any(any(ch in p for ch in SHELL_GLOB_CHARS) for p in paths):
        return None  # a shell-expanded target set we can't see from the log
    if name == "rm":
        if not paths or any(flag not in SHELL_RM_FLAGS for flag in flags):
            return None
        return ("rm", [_resolve_shell_path(p, cwd) for p in paths])
    if name in ("mv", "cp"):
        if flags or len(paths) != 2:
            return None  # e.g. the multi-source "mv a b c dir/" form, or an unrecognized flag
        src, dst = (_resolve_shell_path(p, cwd) for p in paths)
        return (name, src, dst)
    return None


def _parse_shell_edits(command: str, cwd: str) -> tuple[list[str], list[tuple[str, str, bool]], bool]:
    """Parses a shell command into (rm targets, (src, dst, src_removed) moves, fully_understood).

    Walks the tokenized command in a single pass, splitting on ;/&&/||/| and recognizing rm/mv/cp
    within each segment as it goes. Conservative by construction: anything not cleanly recognized --
    unknown flags, globs, redirects, multi-source mv, unbalanced quoting -- leaves fully_understood
    False rather than guess, so the caller falls back to an unverified ShellActivity marker for
    whatever part it didn't understand, instead of risking a wrong attribution.
    """
    tokens = _tokenize_shell_command(command)
    if tokens is None:
        return [], [], False
    rm_targets: list[str] = []
    moves: list[tuple[str, str, bool]] = []
    fully_understood = True
    segment: list[str] = []  # tokens accumulated for the command segment currently being scanned
    for token in [*tokens, ";"]:  # trailing sentinel flushes the final segment without special-casing it
        if token not in SHELL_SEGMENT_SEPARATORS:
            segment.append(token)
            continue
        if segment:
            parsed = _parse_shell_segment(segment, cwd)
            if parsed is None:
                fully_understood = False
            elif parsed[0] == "rm":
                rm_targets.extend(parsed[1])
            else:
                name, src, dst = parsed
                moves.append((src, dst, name == "mv"))
        segment = []
    return rm_targets, moves, fully_understood


def _build_shell_poll_result(command: str, cwd: str, start_ts: float, end_ts: float,
                              session_id: str, prompt: str, agent: str) -> PollResult:
    """Turns one finished shell command into tier-1 ToolEdit/ShellMoveEdit matches (rm/mv/cp) plus
    a tier-2 ShellActivity marker for whatever part of it wasn't safely parseable."""
    rm_targets, moves, fully_understood = _parse_shell_edits(command, cwd)
    edits = [ToolEdit(start_ts, path, session_id, prompt, agent, EditContent(replacements=None, full_content=""))
             for path in rm_targets]
    move_edits = [ShellMoveEdit(start_ts, session_id, prompt, agent, src, dst, src_removed)
                  for src, dst, src_removed in moves]
    activity = [] if fully_understood else [ShellActivity(start_ts, end_ts, session_id, agent, prompt, command)]
    return PollResult(edits, move_edits, activity)


def _parse_diff_hunks(unified_diff: str) -> list[tuple[str, str]]:
    """Splits a unified diff's hunks (no file header) into (old_fragment, new_fragment) text pairs."""
    hunks = re.split(r"^@@.*@@\n", unified_diff, flags=re.MULTILINE)[1:]  # drop any preamble before the first hunk
    pairs = []
    for hunk in hunks:
        old_lines, new_lines = [], []  # reconstructed pre-image / post-image text for this hunk
        prev_prefix = None  # '-'/'+'/' ' of the last content line; tells a following "\ No newline" marker which side(s) to trim
        for line in hunk.splitlines(keepends=True):
            if line.startswith("-"):
                old_lines.append(line[1:])
                prev_prefix = "-"
            elif line.startswith("+"):
                new_lines.append(line[1:])
                prev_prefix = "+"
            elif line.startswith(" "):
                old_lines.append(line[1:])
                new_lines.append(line[1:])
                prev_prefix = " "
            elif line.startswith("\\"):
                # "\ No newline at end of file": the real file has no trailing '\n' on the line just emitted above,
                # even though the diff text itself always terminates that line with one; strip it back off.
                if prev_prefix in ("-", " ") and old_lines and old_lines[-1].endswith("\n"):
                    old_lines[-1] = old_lines[-1][:-1]
                if prev_prefix in ("+", " ") and new_lines and new_lines[-1].endswith("\n"):
                    new_lines[-1] = new_lines[-1][:-1]
        pairs.append(("".join(old_lines), "".join(new_lines)))
    return pairs


def _parse_timestamp(iso_timestamp: str) -> float:
    """Converts a transcript's ISO-8601 UTC timestamp string into epoch seconds."""
    assert iso_timestamp, "every transcript line carries a timestamp"
    return datetime.strptime(iso_timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()


def _cwd_related(session_cwd: str, watched_cwd: str) -> bool:
    """True if session_cwd and watched_cwd sit on the same directory chain -- equal, or one nested
    inside the other -- so a session started in a subdirectory of the watched root (edit_watcher's
    root recursively covers its whole subtree) or in an ancestor of it still correlates correctly,
    unlike a bare exact-string cwd match."""
    a, b = Path(session_cwd), Path(watched_cwd)
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


HUMAN_PROMPT_SOURCES = {"typed", "sdk"}  # promptSource values that mean a person actually wrote this (CLI typing vs. IDE-extension SDK calls)


def _extract_user_prompt(record: dict) -> str | None:
    """Returns the human-typed prompt text if this record is a real, typed user turn, else None."""
    if record.get("type") != "user" or record.get("promptSource") not in HUMAN_PROMPT_SOURCES:
        return None  # system-injected turns (tool results, slash commands, caveats) are not prompts
    content = record.get("message", {}).get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    # IDE clients (e.g. the VS Code extension) prepend a synthetic "<ide_opened_file>" text block
    # ahead of the real prompt; keep only the block(s) a human actually typed.
    texts = [
        block["text"] for block in content
        if isinstance(block, dict) and block.get("type") == "text"
        and not block.get("text", "").startswith("<ide_opened_file>")
    ]
    return "\n".join(texts) if texts else None


def _extract_edits(record: dict) -> list[tuple[str, EditContent | None]]:
    """Returns (path, content) for every Edit/Write/NotebookEdit tool call in this record."""
    if record.get("type") != "assistant":
        return []
    blocks = record.get("message", {}).get("content", [])  # assistant turns may issue several tool calls at once
    edits = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if name not in EDIT_TOOL_NAMES:
            continue
        tool_input = block.get("input", {})
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not path:
            continue
        edits.append((path, _edit_content_for(name, tool_input)))
    return edits


def _edit_content_for(tool_name: str, tool_input: dict) -> EditContent | None:
    """Builds the precise content transform for one Claude Code Edit/Write tool call, if known."""
    if tool_name == "Edit":
        return EditContent(replacements=[(tool_input["old_string"], tool_input["new_string"])], full_content=None)
    if tool_name == "Write":
        return EditContent(replacements=None, full_content=tool_input["content"])
    return None  # NotebookEdit cell diffs aren't plain-text replacements; caller falls back to reading disk


def _extract_bash_calls(record: dict) -> list[tuple[str, str]]:
    """Returns (tool_use_id, command) for every Bash tool call in this assistant record."""
    if record.get("type") != "assistant":
        return []
    blocks = record.get("message", {}).get("content", [])
    calls = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") != CLAUDE_SHELL_TOOL_NAME:
            continue
        command = block.get("input", {}).get("command")
        tool_use_id = block.get("id")
        if command and tool_use_id:
            calls.append((tool_use_id, command))
    return calls


def _extract_tool_result_ids(record: dict) -> list[str]:
    """Returns every tool_use_id a non-prompt user record's tool_result blocks respond to."""
    if record.get("type") != "user":
        return []
    content = record.get("message", {}).get("content")
    if not isinstance(content, list):
        return []
    return [block["tool_use_id"] for block in content
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id")]


class ClaudeTranscriptTailer:
    """Incrementally reads new lines appended to *.jsonl transcripts across every Claude Code
    project directory, keeping only the sessions whose recorded cwd is related to ours (see
    _cwd_related) -- a session started in a subdirectory of the watched root, or in an ancestor of
    it, still correlates correctly. Every transcript line already carries its session's real cwd,
    so this checks that directly rather than trusting a single slug-derived project directory
    (which only ever matches an exact cwd string) -- the same shape of check CodexTranscriptTailer
    already does against session_meta.payload.cwd."""

    def __init__(self, cwd: str):
        self.cwd = cwd                                # watched root's cwd; sessions whose recorded cwd relates to this one are ours
        self._start_time = time.time()                  # transcript lines logged before this are history, not live activity
        self._offsets: dict[Path, int] = {}               # byte offset already consumed, per transcript file
        self._matches_cwd: dict[Path, bool] = {}            # transcript path -> whether its recorded cwd relates to ours
        self._last_prompt: dict[str, str] = {}                # most recent human prompt text seen, per session id
        self._prompt_history: dict[str, list[tuple[float, str]]] = {}  # session id -> every (timestamp, prompt_text) seen, oldest first
        self._pending_bash: dict[str, tuple[float, str, str, str]] = {}  # tool_use_id -> (timestamp, command, session_id, prompt), until its tool_result arrives

    @property
    def start_time(self) -> float:
        """When this tailer started watching; callers filter poll() results against it to drop history."""
        return self._start_time

    def poll(self) -> PollResult:
        """Reads any new transcript lines since the last call and returns new findings."""
        if not CLAUDE_PROJECTS_ROOT.is_dir():
            return PollResult.empty()  # no Claude Code session has ever run on this machine
        return PollResult.merge([self._read_new_lines(path) for path in CLAUDE_PROJECTS_ROOT.glob("*/*.jsonl")])

    def _read_new_lines(self, path: Path) -> PollResult:
        """Reads whole new lines appended to one transcript file since it was last read."""
        if self._matches_cwd.get(path) is False:
            return PollResult.empty()  # already confirmed this session's cwd is unrelated to ours; skip re-reading it
        offset = self._offsets.get(path, 0)  # byte offset already consumed in this file
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
        consumed_upto = data.rfind(b"\n") + 1  # only accept complete lines; a partial tail waits for next poll
        if consumed_upto == 0:
            return PollResult.empty()
        self._offsets[path] = offset + consumed_upto
        result = PollResult.empty()  # accumulates this file's findings across its newly read lines
        for line in data[:consumed_upto].decode("utf-8").splitlines():
            if not line.strip():  # skip blank lines; json.loads has no valid parse for them
                continue
            try:
                result.absorb(self._parse_line(path, line))
            except json.JSONDecodeError:
                continue  # Claude Code can write a truncated line if the CLI is killed mid-write; skip it
        return result

    def _parse_line(self, transcript_path: Path, line: str) -> PollResult:
        """Parses one transcript JSON line, updating cwd/prompt/pending-Bash tracking and extracting findings."""
        record = json.loads(line)  # one transcript event: a user turn, assistant turn, or tool result
        record_cwd = record.get("cwd")
        if record_cwd is not None:
            self._matches_cwd[transcript_path] = _cwd_related(record_cwd, self.cwd)
        if not self._matches_cwd.get(transcript_path, False):
            return PollResult.empty()  # cwd for this session not yet confirmed as ours, or confirmed unrelated
        if record.get("type") not in ("user", "assistant"):
            return PollResult.empty()  # non-conversation records (mode, permission-mode, ...) carry no tool calls
        session_id = record.get("sessionId")
        assert session_id, "every user/assistant transcript line is tagged with its session id"
        timestamp = _parse_timestamp(record.get("timestamp"))
        prompt_text = _extract_user_prompt(record)
        if prompt_text is not None:
            self._last_prompt[session_id] = prompt_text
            self._prompt_history.setdefault(session_id, []).append((timestamp, prompt_text))
            return PollResult.empty()  # a prompt line never itself contains a tool call
        if record.get("type") == "assistant":
            prompt = self._last_prompt.get(session_id, "")  # prompt in effect when this tool call happened
            edits = [ToolEdit(timestamp, edit_path, session_id, prompt, "Claude", content)
                     for edit_path, content in _extract_edits(record)]
            for tool_use_id, command in _extract_bash_calls(record):
                self._pending_bash[tool_use_id] = (timestamp, command, session_id, prompt)
            return PollResult(edits, [], [])
        return PollResult.merge([self._resolve_bash_result(tool_use_id, timestamp)
                                    for tool_use_id in _extract_tool_result_ids(record)])

    def _resolve_bash_result(self, tool_use_id: str, end_timestamp: float) -> PollResult:
        """Finishes one pending Bash call once its tool_result line is seen, turning it into
        tier-1 matches (rm/mv/cp) plus a tier-2 marker for whatever it didn't parse."""
        pending = self._pending_bash.pop(tool_use_id, None)
        if pending is None:
            return PollResult.empty()  # not a Bash call's result (e.g. an Edit/Write result), or already resolved
        start_ts, command, session_id, prompt = pending
        return _build_shell_poll_result(command, self.cwd, start_ts, end_timestamp, session_id, prompt, "Claude")

    def get_prompts_in_range(self, session_id: str, start_time: float, end_time: float) -> list[tuple[float, str]]:
        """Returns prompts from session_id between start_time and end_time (exclusive), oldest first,
        for prompts that didn't immediately result in edits. Served from _prompt_history -- the record
        of every prompt this tailer has already parsed while polling -- rather than re-scanning
        transcript files from disk, which duplicates _parse_line's work and would need to re-derive
        every record-type edge case (mode/permission-mode/session_meta lines) it already handles."""
        return [(ts, text) for ts, text in self._prompt_history.get(session_id, [])
                if start_time <= ts < end_time]


class AntigravityPromptExtractor:
    """Recovers the human-typed sentence from an Antigravity user-turn's raw protobuf blob.

    Antigravity stores conversation steps as undocumented protobuf with no public schema, so this is
    a heuristic (scans for printable runs, keeps the longest one that isn't a UUID/file URI/path/JSON
    payload) rather than a real parser. Isolated behind this interface so the extraction strategy can
    be swapped out -- without touching AntigravityTranscriptTailer -- when the format inevitably changes.
    """

    _PRINTABLE_RUN = re.compile(rb"[\x20-\x7e]{10,}")  # candidate ASCII text runs long enough to be a real sentence

    def extract(self, payload: bytes) -> str | None:
        """Best-effort guess at the prompt text encoded in payload, or None if nothing plausible was found."""
        candidates = [m.decode("ascii") for m in self._PRINTABLE_RUN.findall(payload)]
        prose = [self._clean(c, payload) for c in candidates if self._looks_like_prose(c)]
        return max(prose, key=len, default=None)

    def _looks_like_prose(self, candidate: str) -> bool:
        """True if candidate reads like a typed sentence rather than a path/URI/JSON tool payload."""
        return " " in candidate and not candidate.startswith(("/", "file://", "{"))

    def _clean(self, candidate: str, payload: bytes) -> str:
        """Strips protobuf length prefixes and trailing wire garbage off candidate, if a matching
        prefix length can be located back in payload; otherwise returns candidate unchanged."""
        # Case 1: candidate starts with a printable length prefix character (e.g. 'V')
        if len(candidate) > 1:
            first_char = candidate[0]
            rest = candidate[1:]
            for L in range(len(rest), 9, -1):
                prefix = rest[:L]
                pos = payload.find(prefix.encode("utf-8"))
                if pos > 0 and payload[pos - 1] == L and payload[pos - 1] == ord(first_char):
                    return prefix
        # Case 2: candidate starts directly with the string (length prefix was non-printable)
        for L in range(len(candidate), 9, -1):
            prefix = candidate[:L]
            pos = payload.find(prefix.encode("utf-8"))
            if pos > 0 and payload[pos - 1] == L:
                return prefix
        return candidate


def _parse_antigravity_timestamp(iso_timestamp: str) -> float:
    """Converts an Antigravity transcript's whole-second UTC created_at string into epoch seconds."""
    assert iso_timestamp, "every antigravity transcript record carries a created_at timestamp"
    return datetime.strptime(iso_timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _extract_edit(tool_call: dict) -> tuple[str, EditContent | None] | None:
    """Returns (path, content) for one Antigravity tool_calls[] entry, if it's a write tool."""
    name = tool_call.get("name")
    if name not in ANTIGRAVITY_WRITE_TOOL_NAMES:
        return None  # read-only or non-file tool (view_file, list_dir, run_command, ...); nothing was written
    args = tool_call.get("args", {})  # Antigravity double-encodes args: each value is itself JSON
    path = json.loads(args["TargetFile"])
    return path, _antigravity_edit_content(name, args)


def _extract_shell_call(tool_call: dict) -> tuple[str, str] | None:
    """Returns (command, cwd) for one Antigravity tool_calls[] entry, if it's the run_command shell tool."""
    if tool_call.get("name") != ANTIGRAVITY_SHELL_TOOL_NAME:
        return None
    args = tool_call.get("args", {})  # Antigravity double-encodes args: each value is itself JSON
    command = json.loads(args["CommandLine"])
    cwd = json.loads(args["Cwd"])
    return command, cwd


def _antigravity_edit_content(tool_name: str, args: dict) -> EditContent | None:
    """Builds the precise content transform for one Antigravity write tool call, if known."""
    if tool_name == "replace_file_content":
        target = json.loads(args["TargetContent"])
        replacement = json.loads(args["ReplacementContent"])
        return EditContent(replacements=[(target, replacement)], full_content=None)
    if tool_name == "multi_replace_file_content":
        chunks = json.loads(args["ReplacementChunks"])  # ordered list of {TargetContent, ReplacementContent, ...}
        replacements = [(chunk["TargetContent"], chunk["ReplacementContent"]) for chunk in chunks]
        return EditContent(replacements=replacements, full_content=None)
    if tool_name == "write_to_file":
        return EditContent(replacements=None, full_content=json.loads(args["CodeContent"]))
    return None


class AntigravityTranscriptTailer:
    """Incrementally reads new Antigravity CLI activity for one cwd and yields ToolEdits.

    Antigravity has no single append-only transcript like Claude Code. The human prompt only
    lives in a per-conversation SQLite db; tool calls and their timestamps only show up later
    in a companion transcript.jsonl once the conversation starts acting, so both are tailed.
    """

    def __init__(self, cwd: str):
        self.cwd = cwd                                  # workspace path used to look up the active conversation id
        self._start_time = time.time()                    # steps/tool calls logged before this are history, not live activity
        self._db_offsets: dict[str, int] = {}                # conversation_id -> last steps.idx consumed from its sqlite db
        self._jsonl_offsets: dict[Path, int] = {}              # transcript.jsonl path -> byte offset already consumed
        self._last_prompt: dict[str, str] = {}                   # conversation_id -> most recent human prompt text seen
        self._prompt_extractor = AntigravityPromptExtractor()      # swappable strategy for recovering prompt text from raw step payloads

    @property
    def start_time(self) -> float:
        """When this tailer started watching; callers filter poll() results against it to drop history."""
        return self._start_time

    def poll(self) -> PollResult:
        """Finds the conversation mapped to cwd and reads any new prompt/tool-call activity."""
        conversation_id = self._active_conversation_id()
        if conversation_id is None:
            return PollResult.empty()  # no Antigravity session has ever run against this cwd
        self._absorb_new_prompts(conversation_id)
        return self._read_new_tool_calls(conversation_id)

    def _active_conversation_id(self) -> str | None:
        """Looks up which conversation Antigravity last opened for self.cwd or its nearest ancestor.

        We first look for active conversation SQLite databases containing the workspace URI in
        their metadata. If none are found, we fall back to last_conversations.json.
        """
        # 1. Search for active SQLite databases mapping to this workspace or its ancestors
        candidates = []
        for candidate_dir in (Path(self.cwd), *Path(self.cwd).parents):
            cwd_uri = f"file://{candidate_dir}"
            db_pattern = str(ANTIGRAVITY_ROOT / "conversations" / "*.db")
            for db_path in glob.glob(db_pattern):
                try:
                    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                    row = con.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'").fetchone()
                    con.close()
                    if row and cwd_uri.encode("utf-8") in row[0]:
                        candidates.append((os.path.getmtime(db_path), os.path.basename(db_path)[:-3]))
                except Exception:
                    continue
        if candidates:
            # Return the conversation ID of the most recently modified database
            candidates.sort(reverse=True)
            return candidates[0][1]

        # 2. Fallback: Check last_conversations.json
        mapping_path = ANTIGRAVITY_ROOT / "cache" / "last_conversations.json"
        if not mapping_path.is_file():
            return None
        try:
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            for candidate in (Path(self.cwd), *Path(self.cwd).parents):
                if str(candidate) in mapping:
                    return mapping[str(candidate)]
        except Exception:
            pass
        return None

    def _absorb_new_prompts(self, conversation_id: str):
        """Reads any new rows from the conversation's sqlite db and records human prompt text."""
        db_path = ANTIGRAVITY_ROOT / "conversations" / f"{conversation_id}.db"  # one sqlite file per conversation
        if not db_path.is_file():
            return  # conversation is legacy .pb format, or hasn't been flushed to disk yet
        start = self._db_offsets.get(conversation_id, 0)  # first not-yet-seen steps.idx for this conversation
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT idx, step_type, step_payload FROM steps WHERE idx >= ? ORDER BY idx", (start,)
            ).fetchall()
        except sqlite3.OperationalError:
            return  # db was mid-write by Antigravity itself; retry next poll
        finally:
            con.close()
        for idx, step_type, payload in rows:
            self._db_offsets[conversation_id] = idx + 1
            if step_type == ANTIGRAVITY_USER_STEP_TYPE:
                prompt_text = self._prompt_extractor.extract(payload)
                if prompt_text:
                    self._last_prompt[conversation_id] = prompt_text

    def _read_new_tool_calls(self, conversation_id: str) -> PollResult:
        """Reads new lines from the conversation's readable transcript.jsonl and extracts file writes and rm/mv/cp shell calls."""
        jsonl_path = (ANTIGRAVITY_ROOT / "brain" / conversation_id /
                      ".system_generated" / "logs" / "transcript.jsonl")  # human-readable step log, lags the sqlite db
        if not jsonl_path.is_file():
            return PollResult.empty()  # no tool call has been logged for this conversation yet
        offset = self._jsonl_offsets.get(jsonl_path, 0)  # byte offset already consumed in this file
        with jsonl_path.open("rb") as f:
            f.seek(offset)
            data = f.read()
        consumed_upto = data.rfind(b"\n") + 1  # only accept complete lines; a partial tail waits for next poll
        if consumed_upto == 0:
            return PollResult.empty()
        self._jsonl_offsets[jsonl_path] = offset + consumed_upto
        prompt = self._last_prompt.get(conversation_id, "")  # prompt in effect when these tool calls happened
        result = PollResult.empty()  # accumulates this file's findings across its newly read lines
        for line in data[:consumed_upto].decode("utf-8").splitlines():
            if not line.strip():  # skip blank lines; json.loads has no valid parse for them
                continue
            try:
                result.absorb(self._parse_transcript_line(line, conversation_id, prompt))
            except json.JSONDecodeError:
                continue  # Antigravity can write a truncated record if a step is cancelled mid-write; skip it
        return result

    def _parse_transcript_line(self, line: str, conversation_id: str, prompt: str) -> PollResult:
        """Parses one transcript.jsonl record, returning tier-1/tier-2 findings for each tool call it made.

        A run_command entry only ever appears here once it's fully finished (status DONE), unlike
        Claude's Bash tool call/result pair split across two transcript lines -- so it can be turned
        straight into a PollResult with no pending-call bookkeeping needed.
        """
        record = json.loads(line)  # one Antigravity step: a tool call request, its result, or plain assistant text
        timestamp = _parse_antigravity_timestamp(record.get("created_at"))
        result = PollResult.empty()  # accumulates this record's findings across its tool calls
        for call in record.get("tool_calls", []):
            found = _extract_edit(call)
            if found:
                path, content = found
                result.edits.append(ToolEdit(timestamp, path, conversation_id, prompt, "Antigravity", content))
                continue
            shell_call = _extract_shell_call(call)
            if shell_call:
                command, cwd = shell_call
                result.absorb(_build_shell_poll_result(command, cwd, timestamp, timestamp,
                                                         conversation_id, prompt, "Antigravity"))
        return result

    def get_prompts_in_range(self, conversation_id: str, start_time: float, end_time: float) -> list[tuple[float, str]]:
        """Returns all prompts from conversation_id between start_time and end_time (exclusive), oldest first.
        Returns list of (timestamp, prompt_text) tuples. Not yet implemented for Antigravity."""
        return []  # TODO: implement if needed


def _codex_edit_content(change: dict) -> EditContent | None:
    """Builds the precise content transform for one Codex patch_apply_end change entry, if known."""
    if change.get("type") == "add":
        return EditContent(replacements=None, full_content=change["content"])  # new file: log carries the full text directly
    if change.get("type") == "delete":
        return EditContent(replacements=None, full_content="")  # file removed: _show_at returns '' for a path absent at a commit
    unified_diff = change.get("unified_diff")
    if unified_diff is None:
        return None  # e.g. a pure file move with no content change; not recoverable from this entry
    return EditContent(replacements=_parse_diff_hunks(unified_diff), full_content=None)


def _extract_codex_prompt(raw_message: str) -> str:
    """Strips the Codex VS Code extension's synthetic IDE-context wrapper, keeping only the typed request."""
    marker_pos = raw_message.find(CODEX_IDE_REQUEST_MARKER)  # -1 for plain terminal-typed messages, which carry no wrapper
    if marker_pos == -1:
        return raw_message
    return raw_message[marker_pos + len(CODEX_IDE_REQUEST_MARKER):].strip()


class CodexTranscriptTailer:
    """Incrementally reads new lines appended to Codex CLI's dated rollout-*.jsonl transcripts.

    Codex has no per-project transcript directory like Claude Code: every session on the machine
    lands under one dated tree (~/.codex/sessions/YYYY/MM/DD/), so each file's declared cwd must
    be checked against ours (see _cwd_related) before any of its events are trusted -- a session
    started in a subdirectory of the watched root, or in an ancestor of it, still correlates
    correctly, unlike a bare exact-string cwd match.
    """

    def __init__(self, cwd: str):
        self.cwd = cwd                                 # workspace path a session's session_meta.cwd must equal to be ours
        self._start_time = time.time()                   # rollout events logged before this are history, not live activity
        self._offsets: dict[Path, int] = {}                # byte offset already consumed, per rollout file
        self._matches_cwd: dict[Path, bool] = {}            # rollout file -> whether its session_meta.cwd matched self.cwd
        self._session_ids: dict[Path, str] = {}              # rollout file -> session id declared in its session_meta
        self._last_prompt: dict[Path, str] = {}                # rollout file -> most recent human prompt text seen
        self._pending_exec: dict[Path, dict[str, tuple[float, str, str, str]]] = {}  # rollout file -> call_id -> (timestamp, cmd, workdir, prompt), until its function_call_output arrives

    @property
    def start_time(self) -> float:
        """When this tailer started watching; callers filter poll() results against it to drop history."""
        return self._start_time

    def poll(self) -> PollResult:
        """Reads any new lines across all rollout transcripts and returns new findings."""
        if not CODEX_SESSIONS_ROOT.is_dir():
            return PollResult.empty()  # no Codex CLI session has ever run on this machine
        return PollResult.merge([self._read_new_lines(path) for path in CODEX_SESSIONS_ROOT.rglob("*.jsonl")])

    def _read_new_lines(self, path: Path) -> PollResult:
        """Reads whole new lines appended to one rollout file since it was last read."""
        if self._matches_cwd.get(path) is False:
            return PollResult.empty()  # already confirmed this session belongs to a different cwd; skip re-reading it
        offset = self._offsets.get(path, 0)  # byte offset already consumed in this file
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
        consumed_upto = data.rfind(b"\n") + 1  # only accept complete lines; a partial tail waits for next poll
        if consumed_upto == 0:
            return PollResult.empty()
        self._offsets[path] = offset + consumed_upto
        result = PollResult.empty()  # accumulates this file's findings across its newly read lines
        for line in data[:consumed_upto].decode("utf-8").splitlines():
            if not line.strip():  # skip blank lines; json.loads has no valid parse for them
                continue
            try:
                result.absorb(self._parse_line(path, line))
            except json.JSONDecodeError:
                continue  # Codex can write a truncated line if the CLI is killed mid-write; skip it
        return result

    def _parse_line(self, path: Path, line: str) -> PollResult:
        """Parses one rollout JSON line, tracking session identity/prompts and extracting findings."""
        record = json.loads(line)  # one rollout event: session metadata, a response item, or an event_msg
        if record.get("type") == "session_meta":
            payload = record.get("payload", {})  # session_meta payload carries the cwd Codex was invoked in
            session_cwd = payload.get("cwd")
            self._matches_cwd[path] = session_cwd is not None and _cwd_related(session_cwd, self.cwd)
            self._session_ids[path] = payload.get("session_id", "")
            return PollResult.empty()
        if not self._matches_cwd.get(path, False):
            return PollResult.empty()  # cwd for this session not yet confirmed as ours, or confirmed as a different project
        record_type = record.get("type")
        if record_type == "response_item":
            return self._parse_response_item(path, record)
        if record_type != "event_msg":
            return PollResult.empty()
        payload = record.get("payload", {})  # event_msg payload carries the actual event type and data
        event_type = payload.get("type")
        if event_type == CODEX_USER_MESSAGE_EVENT:
            self._last_prompt[path] = _extract_codex_prompt(payload.get("message", ""))
            return PollResult.empty()  # a prompt line never itself contains a tool call
        if event_type == CODEX_PATCH_EVENT and payload.get("success"):
            timestamp = _parse_timestamp(record.get("timestamp"))
            prompt = self._last_prompt.get(path, "")  # prompt in effect when this patch was applied
            session_id = self._session_ids.get(path, "")
            changes = payload.get("changes", {})  # patch_apply_end.changes maps path -> change info
            edits = [ToolEdit(timestamp, p, session_id, prompt, "Codex", _codex_edit_content(change))
                     for p, change in changes.items()]
            return PollResult(edits, [], [])
        return PollResult.empty()

    def _parse_response_item(self, path: Path, record: dict) -> PollResult:
        """Parses one response_item line for exec_command calls -- Codex's shell-exec tool, logged
        only here (unlike apply_patch, it has no event_msg summary), matched call-to-output by
        call_id the same way patch_apply_end is already matched to its originating turn."""
        payload = record.get("payload", {})  # response_item payload carries the actual item type and data
        item_type = payload.get("type")
        if item_type == "function_call" and payload.get("name") == CODEX_EXEC_TOOL_NAME:
            timestamp = _parse_timestamp(record.get("timestamp"))
            call_id = payload.get("call_id")
            args = json.loads(payload.get("arguments", "{}"))
            prompt = self._last_prompt.get(path, "")  # prompt in effect when this call was issued
            self._pending_exec.setdefault(path, {})[call_id] = (
                timestamp, args.get("cmd", ""), args.get("workdir", self.cwd), prompt)
            return PollResult.empty()
        if item_type == "function_call_output":
            call_id = payload.get("call_id")
            pending = self._pending_exec.get(path, {}).pop(call_id, None)
            if pending is None:
                return PollResult.empty()  # not an exec_command's output (or already resolved)
            start_ts, cmd, workdir, prompt = pending
            end_ts = _parse_timestamp(record.get("timestamp"))
            session_id = self._session_ids.get(path, "")
            return _build_shell_poll_result(cmd, workdir, start_ts, end_ts, session_id, prompt, "Codex")
        return PollResult.empty()

    def get_prompts_in_range(self, session_id: str, start_time: float, end_time: float) -> list[tuple[float, str]]:
        """Returns all prompts from session_id between start_time and end_time (exclusive), oldest first.
        Returns list of (timestamp, prompt_text) tuples. Not yet implemented for Codex."""
        return []  # TODO: implement if needed


def build_tailers(cwd: str, agent: str) -> list:
    """Constructs the transcript tailers to poll, per agent (claude, antigravity, codex, or both)."""
    tailers = []  # agent-specific tailers this run should poll each cycle
    if agent in ("claude", "both"):
        tailers.append(ClaudeTranscriptTailer(cwd))
    if agent in ("antigravity", "both"):
        tailers.append(AntigravityTranscriptTailer(cwd))
    if agent in ("codex", "both"):
        tailers.append(CodexTranscriptTailer(cwd))
    return tailers
