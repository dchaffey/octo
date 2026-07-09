#!/usr/bin/env python3
"""Git-backed replacement for FileSnapshotWatcher: uses a shadow git repo (.octo) whose
work-tree is the watched directory, so settled edits become real commits -- giving free
diffing, history, and revert (git revert writes straight back into the real files).

Debounced commit-on-write, attribute-after-the-fact: settled disk writes are committed via
commit_dirty(), attributed to "Human" by default regardless of who actually wrote them. This is
always correct because it's built directly from bytes observed on disk, never replayed from a log.
When an agent transcript entry later explains a commit already made, attribute() attaches a
git-notes entry naming the agent/session/prompt to that commit -- no commit message rewriting, no
SHA changes, no stale commit references elsewhere. This avoids the crash class a prior design had:
there is no "does the log's fragment match some assumed prior state" check, because nothing is
ever reconstructed and trusted -- every commit is exactly what was on disk at that moment, and
attribute() only ever *tests* candidate matches (via EditContent.apply()'s non-asserting probe),
never commits based on one it assumes to be true."""

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from agent_watcher import EditContent, ShellMoveEdit, ToolEdit

SHADOW_DIR_NAME = ".octo"  # name of the git-dir living inside root; work-tree is root itself
IGNORED_DIR_NAMES = (
    ".git", "node_modules", "__pycache__", ".venv", "venv",  # noisy build/vcs dirs excluded from the shadow repo
    ".claude", ".cursor", ".windsurf", ".continue",  # per-project AI coding agent config/state dirs, same reasoning
)
OCTOIGNORE_FILENAME = ".octoignore"  # project-level gitignore-syntax exclude file, read from watched root
OCTOIGNORE_TEMPLATE = """# .octoignore — patterns of files and directories to exclude from tracking
# Syntax: gitignore-compatible patterns (*, ?, **, etc.)
# Examples:
#   node_modules/       — ignore all node_modules directories
#   *.log               — ignore all .log files
#   build/              — ignore the build directory
#   .env                — ignore .env file
#
# The patterns below are already excluded unconditionally (see IGNORED_DIR_NAMES in
# shadow_repo.py) -- they're listed here so they're visible/editable, not because removing
# them stops the exclusion.
""" + "\n".join(f"{name}/" for name in IGNORED_DIR_NAMES) + "\n"  # preset patterns, generated from IGNORED_DIR_NAMES so the two never drift apart
BASELINE_SUBJECT = "octo: baseline"  # first line of every initialize() commit, empty or not; marks a commit as a baseline in history()
AGENT_TRAILER = "Agent: "               # note/legacy-message line marking a commit as agent-authored, and naming the agent
SESSION_TRAILER = "Session: "             # note/legacy-message line carrying the originating session id
REVERTED_BY_TRAILER = "Reverted by: "     # note line marking a commit as having been reverted
REVERTS_TRAILER = "Reverts: "             # note line marking a commit as a revert of another
WALK_BACK_LIMIT = 5  # how many recent commits touching a path attribute() searches before giving up
DIRTY_DEBOUNCE_SECONDS = 2.0  # quiet period before unattributed dirty files are committed


@dataclass
class SettledEdit:
    """A file edit now committed to the shadow repo, whether flushed from disk or just reattributed."""
    file_path: str     # absolute path that changed
    diff: str            # diff between the previous and new committed content
    settled_at: float     # epoch seconds the commit was authored
    commit: str            # shadow-repo commit sha this edit landed in; pass to revert_commit() to undo it
    supersedes_commit: str = ""  # prior sha this edit amended/replaced; empty for a brand-new commit
    canceled: bool = False  # true when an open human commit collapsed back to no net change and was dropped


@dataclass
class AffectedCommit:
    """One commit after a revert target that also touched the same file -- restoring the target's
    pre-commit content would silently discard this commit's change to that file, so revert_file_to()
    callers use commits_after() to list these and warn before writing to disk."""
    commit: str      # sha of the later commit
    timestamp: float   # commit's author time, epoch seconds
    agent: str            # agent name if attributed, else "" (human)


@dataclass
class HistoryEntry:
    """One file touched by one commit already in the shadow repo's log, read back by history() to replay past sessions."""
    file_path: str    # absolute path this commit's diff applies to
    diff: str            # this path's diff introduced by the commit
    timestamp: float       # commit's author time, epoch seconds
    commit: str               # shadow-repo commit sha
    agent: str                  # agent name if a note (or legacy message) attributes this commit, else "" (human or baseline)
    session_id: str                # originating session id if agent-attributed, else ""
    prompt: str                       # prompt in effect if agent-attributed, else ""
    is_baseline: bool                   # true if this entry came from an initialize() baseline commit, not a write-watcher commit


@dataclass
class DirtyPathState:
    """Last observed on-disk state for one dirty path, used to debounce manual/autosave writes."""
    signature: tuple[int, int] | None
    changed_at: float


def _note_text(edit: ToolEdit) -> str:
    """Builds attribute()'s git-notes body: Agent/Session trailers plus the prompt, parsed back by _parse_attribution()."""
    return f"{AGENT_TRAILER}{edit.agent}\n{SESSION_TRAILER}{edit.session_id}\n\n{edit.prompt}"


def _parse_attribution(text: str) -> tuple[str, str, str]:
    """Extracts (agent, session_id, prompt) from a note body (_note_text) or a legacy commit message
    (old _edit_commit_message shape) -- both share the same fixed trailer layout. Returns (agent, '', '')
    for system commits (e.g. octo) that have no session. All '' if text doesn't have the expected shape."""
    lines = text.split("\n")
    if len(lines) < 1 or not lines[0].startswith(AGENT_TRAILER):
        return "", "", ""
    agent = lines[0][len(AGENT_TRAILER):]
    # system agents (like octo) have no session or prompt
    if len(lines) < 2 or not lines[1].startswith(SESSION_TRAILER):
        return agent, "", ""
    session_id = lines[1][len(SESSION_TRAILER):]
    prompt_lines: list[str] = []
    for line in lines[3:]:  # lines[2] is the blank separator between trailers and prompt body
        if line.startswith(REVERTED_BY_TRAILER) or line.startswith(REVERTS_TRAILER):
            break
        prompt_lines.append(line)
    prompt = "\n".join(prompt_lines).rstrip("\n")
    return agent, session_id, prompt


class ShadowGitWatcher:
    """Tracks a directory tree via a shadow git repo, committing settled writes and
    reattributing commits to agents after the fact via git notes."""

    def __init__(self, root: Path, shadow_dir_name: str = SHADOW_DIR_NAME):
        assert root.is_dir(), "watched root must exist"
        self.root = root                              # directory tree being watched; doubles as the shadow repo's work-tree
        self.git_dir = root / shadow_dir_name           # separate git-dir so we never touch a real .git the project may have
        self._initialized = False                           # true once initialize() has committed the startup baseline
        self.last_commit_for_path: dict[str, str] = {}          # rel path -> most recent commit sha touching it; attribute()'s fast-path index
        self._pending_reverts: dict[str, str] = {}        # rel path -> commit sha that was reverted; cleared after commit_dirty
        self._dirty_path_state: dict[str, DirtyPathState] = {}  # rel path -> last observed dirty stat signature + timestamp
        self._open_human_commits: dict[str, str] = {}     # rel path -> amendable human shadow commit still being upserted
        self.baseline_sha: str | None = None            # this session's startup baseline commit; history() uses this as an exclusive upper bound
        self._last_project_head: str | None = self._project_head()  # last observed real-repo HEAD; a change closes human amend windows

    def commit_dirty(self) -> list[SettledEdit]:
        """Commits dirty paths after a short quiet period and returns them as SettledEdits.

        Callers run this after draining agent transcripts each poll tick, so any write an agent's
        transcript already explained this tick has already been committed and annotated by
        attribute() (and is thus no longer dirty) -- everything left here is a genuine unattributed
        (human, or not-yet-explained) write. Pending reverts bypass the debounce so the revert UI
        can update immediately.
        """
        assert self._initialized, "initialize() must run before commit_dirty(), so a baseline exists to diff against"
        self.refresh_human_commit_boundary()
        dirty = self._dirty_paths()
        now = time.time()
        for path in list(self._dirty_path_state):
            if path not in dirty:
                self._dirty_path_state.pop(path, None)
        settled = [
            self._commit_path(path, self._pending_reverts.get(path))
            for path in sorted(dirty)
            if self._pending_reverts.get(path) or self._is_debounced_dirty(path, now)
        ]
        self._pending_reverts.clear()
        return [s for s in settled if s is not None]  # drop no-op commits (see _commit_path)

    def attribute(self, edit: ToolEdit) -> SettledEdit | None:
        """Reattributes the write-watcher commit behind edit to its agent/session/prompt, via a git note.

        Never creates the commit's content from the log -- only tests candidates (the fast-path
        index, then a same-tick commit-now if the path is dirty but not yet indexed, then a short
        walk back) against what's actually on disk, and annotates whichever one matches. Returns
        None if nothing matches (e.g. a later, unrelated write already landed on top of this one);
        the caller's next commit_dirty() pass then just picks the path up as unattributed.
        """
        assert self._initialized, "initialize() must run before attribute(), so a baseline exists to diff against"
        assert edit.content is not None, "attribute requires a resolved edit; caller must filter unresolvable edits first"
        rel = str(Path(edit.file_path).relative_to(self.root))  # path as git (and _dirty_paths) addresses it
        sha = self._find_matching_commit(rel, edit.content)
        if sha is None:
            return None
        return self._settle_attribution(sha, rel, edit)

    def attribute_move(self, move: ShellMoveEdit) -> list[SettledEdit]:
        """Reattributes an mv/cp shell command's affected commit(s) to its agent/session/prompt.

        mv/cp's destination content is never in the log the way Edit/Write's is, so there's
        nothing to hand attribute() directly. Instead this chains two of the same verified
        lookups attribute() itself uses: for mv, prove the source path became absent (matched the
        same way as any other delete), then read what it held just before that -- real committed
        bytes, not a guess -- and prove the destination's committed content equals exactly that.
        For cp, the source is untouched, so its current committed content stands in for "just
        before" directly. Either half can match independently (e.g. if the other side's write
        hasn't landed this tick yet); returns a SettledEdit per half that did.
        """
        assert self._initialized, "initialize() must run before attribute_move(), so a baseline exists to diff against"
        dst_rel = str(Path(move.dst_path).relative_to(self.root))
        src_rel = str(Path(move.src_path).relative_to(self.root))
        settled: list[SettledEdit] = []
        if move.src_removed:
            src_sha = self._find_matching_commit(src_rel, EditContent(replacements=None, full_content=""))
            if src_sha is None:
                return settled  # source's deletion hasn't settled (or matched) yet -- nothing to chain dst's check off of
            prior_content = self._show_at(f"{src_sha}^", src_rel)  # exact bytes src held right before the move, from history
            settled.append(self._settle_move_half(src_sha, src_rel, move))
        else:
            src_sha = self.last_commit_for_path.get(src_rel)
            if src_sha is None:
                return settled  # source's content was never recorded -- nothing to verify dst against
            prior_content = self._show_at(src_sha, src_rel)
        dst_sha = self._find_matching_commit(dst_rel, EditContent(replacements=None, full_content=prior_content))
        if dst_sha is not None:
            settled.append(self._settle_move_half(dst_sha, dst_rel, move))
        return settled

    def _settle_move_half(self, sha: str, rel: str, move: ShellMoveEdit) -> SettledEdit:
        """Builds a note-ready ToolEdit for one verified half of an mv/cp and annotates it."""
        edit = ToolEdit(move.timestamp, str(self.root / rel), move.session_id, move.prompt, move.agent, None)
        return self._settle_attribution(sha, rel, edit)

    def _settle_attribution(self, sha: str, rel: str, edit: ToolEdit) -> SettledEdit:
        """Attaches edit's agent/session/prompt to an already-matched commit and returns it as a SettledEdit."""
        self.break_human_edit_batch()
        self._add_note(sha, edit)
        diff = self._git("show", "--format=", sha, "--", rel).stdout
        at = float(self._git("log", "-1", "--format=%at", sha).stdout.strip())
        return SettledEdit(str(self.root / rel), diff, at, sha)

    def _find_matching_commit(self, rel: str, content: EditContent) -> str | None:
        """Finds the commit whose content for rel matches content's transform: the indexed last
        commit for rel, or one commit-now away if rel is dirty but this tick's write-watcher pass
        hasn't reached it yet, else a short walk back through rel's recent history."""
        sha = self.last_commit_for_path.get(rel)
        if sha is not None and self._content_matches(sha, rel, content):
            return sha
        if rel in self._dirty_paths():
            settled = self._commit_path(rel)
            if settled is not None and self._content_matches(settled.commit, rel, content):
                return settled.commit
        return self._walk_back_match(rel, content)

    def _walk_back_match(self, rel: str, content: EditContent) -> str | None:
        """Searches the last WALK_BACK_LIMIT commits touching rel for one matching content's transform."""
        log = self._git("log", f"-{WALK_BACK_LIMIT}", "--format=%H", "--", rel)
        for sha in log.stdout.split():
            if self._content_matches(sha, rel, content):
                return sha
        return None

    def _content_matches(self, sha: str, rel: str, content: EditContent) -> bool:
        """True if content's transform, applied to rel's content just before sha, equals rel's content at sha.
        Never raises: EditContent.apply() returns None (no match) rather than asserting on a mismatched guess,
        and a parent blob that isn't valid UTF-8 (binary content) is treated the same way -- a text-based
        replacement edit can never have been made against it, so it's a non-match, not a crash."""
        if content.replacements is not None:
            try:
                parent_content = self._show_at(f"{sha}^", rel).decode("utf-8")
            except UnicodeDecodeError:
                return False
            expected = content.apply(parent_content)
        else:
            expected = content.apply("")  # full_content branch ignores prior_content entirely
        if expected is None:
            return False
        expected_bytes = expected if isinstance(expected, bytes) else expected.encode("utf-8")  # full_content is str for real tool edits, bytes when replayed verbatim from _show_at (attribute_move)
        return self._show_at(sha, rel) == expected_bytes

    def _add_note(self, sha: str, edit: ToolEdit):
        """Attaches edit's agent/session/prompt to sha as a git note; appends instead of clobbering if a note is already there."""
        existing = self._git("notes", "show", sha, check=False)
        subcommand = "append" if existing.returncode == 0 else "add"
        self._git("notes", subcommand, "-m", _note_text(edit), sha)

    def _commit_path(self, rel: str, reverted_commit: str | None = None) -> SettledEdit | None:
        """Stages and commits one dirty path as-is, no attribution; updates the fast-path index. If
        reverted_commit is provided, marks this new commit as a revert of that commit. Returns None
        if the commit turns out to be a no-op: either git status's stat-based dirty check flagged
        rel but its content already matches HEAD, or rel vanished between that dirty scan and this
        call (e.g. a transient temp file an editor's atomic save renamed away) -- nothing to record
        in either case."""
        open_commit = None if reverted_commit else self._open_human_commits.get(rel)
        if open_commit and self._git("rev-parse", "--verify", "--quiet", open_commit, check=False).returncode != 0:
            self._open_human_commits.pop(rel, None)
            open_commit = None
        diff_base = f"{open_commit}^" if open_commit else "HEAD"
        diff = self._git("diff", diff_base, "--", rel).stdout  # when amending, show the net diff from the batch's original parent
        add_result = self._git("add", "--", rel, check=False)
        if add_result.returncode != 0:
            assert "did not match any files" in add_result.stderr, f"unexpected git add failure for {rel}: {add_result.stderr}"
            return None  # rel disappeared before we could stage it
        commit_args = (
            ("commit", "-q", "--amend", "--no-edit")
            if open_commit is not None
            else ("commit", "-q", "-m", f"octo: edit {rel}")
        )
        result = self._git(*commit_args, check=False)
        if result.returncode != 0:
            output = f"{result.stdout}\n{result.stderr}"
            assert "nothing to commit" in output or "would make\nit empty" in output, (
                f"unexpected git commit failure for {rel}: {output}"
            )
            if open_commit is not None:
                self._git("reset", "-q", "--mixed", f"{open_commit}^")
                self._open_human_commits.pop(rel, None)
                self.last_commit_for_path[rel] = self._last_commit_touching(rel)
                self._dirty_path_state.pop(rel, None)
                return SettledEdit(
                    str(self.root / rel),
                    "",
                    time.time(),
                    "",
                    supersedes_commit=open_commit,
                    canceled=True,
                )
            self._dirty_path_state.pop(rel, None)
            return None
        sha = self._git("rev-parse", "HEAD").stdout.strip()
        if reverted_commit:
            self._mark_as_revert(sha, reverted_commit)
            self._open_human_commits.pop(rel, None)
        else:
            self._open_human_commits[rel] = sha
        self.last_commit_for_path[rel] = sha
        self._dirty_path_state.pop(rel, None)
        return SettledEdit(str(self.root / rel), diff, time.time(), sha, open_commit or "")

    def _git(self, *args: str, check: bool = True, text: bool = True) -> subprocess.CompletedProcess:
        """Runs one git command against the shadow repo, with root as its work-tree. text=False
        for blob-content reads (_show_at): a tracked file's raw bytes aren't guaranteed UTF-8
        (binaries, other encodings), while every other git output used here (log/diff/status
        metadata) always is."""
        return subprocess.run(
            ["git", f"--git-dir={self.git_dir}", f"--work-tree={self.root}", *args],
            capture_output=True, text=text, check=check,
        )

    def _ensure_octoignore(self):
        """Writes root/.octoignore from OCTOIGNORE_TEMPLATE if it doesn't exist yet -- runs on every
        initialize() so a fresh project gets the preset file up front, instead of waiting for the
        ignore editor to be opened on demand."""
        octoignore = self.root / OCTOIGNORE_FILENAME
        if not octoignore.is_file():
            octoignore.write_text(OCTOIGNORE_TEMPLATE)

    def _sync_exclude_file(self):
        """(Re)writes the shadow repo's local exclude file from IGNORED_DIR_NAMES plus root/.octoignore's
        patterns (if present) -- real git parses this as .gitignore syntax, so wildcards/negation/comments
        just work. Runs on every initialize(), not just first-time init, so edits to .octoignore between
        runs (or before a cache-clear reinit) take effect without touching prior shadow-repo history."""
        lines = [self.git_dir.name, *IGNORED_DIR_NAMES]
        octoignore = self.root / OCTOIGNORE_FILENAME
        if octoignore.is_file():
            lines.extend(octoignore.read_text(encoding="utf-8").splitlines())
        exclude = self.git_dir / "info" / "exclude"
        exclude.write_text("\n".join(lines) + "\n")

    def _untrack_ignored(self):
        """Drops from the index any path that's currently tracked but now matches an ignore rule
        (e.g. a rule added to .octoignore after the path was already committed) -- restores the
        invariant that a path is never both tracked and ignored, so _dirty_paths() (git status) can
        never surface it again and _commit_path's explicit `git add -- rel` never gets refused by
        git for it. Leaves the file itself untouched on disk. Commits the untracking as its own
        commit, separate from the next baseline/edit commit, so history doesn't mislabel it."""
        ignored = self._git("ls-files", "-i", "-c", "--exclude-standard").stdout.splitlines()
        if not ignored:
            return
        self._git("rm", "--cached", "-q", "--", *ignored)
        self._git("commit", "-q", "-m", "octo: untrack ignored paths")

    def initialize(self) -> list[SettledEdit]:
        """Creates the shadow repo (if needed) and commits the current on-disk state as the startup baseline.

        Must run to completion before any tool-edit draining starts: this is a straight diff of
        current files against whatever the shadow repo last recorded (or a first commit if the repo
        is brand new), never a replay of past commits -- so a fresh start never has an uncommitted
        gap. Returns a SettledEdit per path the baseline picked up (all sharing the one baseline
        commit sha), or [] if nothing was dirty -- callers use this to report the baseline like any
        other commit instead of silently swallowing it.
        """
        if not self.git_dir.is_dir():
            self._git("init", "-q")
            self._git("config", "user.name", "octo-watcher")  # local identity so commits don't need the user's global git config
            self._git("config", "user.email", "octo-watcher@local")
        self._ensure_octoignore()  # creates the preset .octoignore up front if this project doesn't have one yet
        self._sync_exclude_file()  # covers first init and picks up any .octoignore changes since the last run
        self._untrack_ignored()  # drops any path that became ignored since it was tracked, before computing dirty
        now = time.time()
        dirty = sorted(self._dirty_paths())                                # paths changed since the last recorded commit
        diffs = {path: self._git("diff", "--", path).stdout for path in dirty}  # diffs taken before staging, like commit_dirty
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "octo: baseline", "--allow-empty")
        sha = self._git("rev-parse", "HEAD").stdout.strip()  # single commit shared by every path the baseline picked up
        self.baseline_sha = sha  # mark the startup baseline, used by history() as an exclusive upper bound
        self._open_human_commits = {}
        self._last_project_head = self._project_head()
        for path in dirty:
            self.last_commit_for_path[path] = sha
        self._initialized = True
        return [SettledEdit(str(self.root / path), diffs[path], now, sha) for path in dirty]

    def history(self, until: str | None = None) -> list[HistoryEntry]:
        """Reads every commit already recorded from previous runs, oldest first, one entry per file it touched.

        If until is provided, stops before (exclusive) that commit -- used to load only prior-run history
        when called after this session's baseline has already been committed, avoiding double-rendering
        of this session's own baseline and live activity. Returns [] on a brand-new shadow repo.
        Also backfills last_commit_for_path from this replay, so attribute() can walk back into
        commits made by a previous run of this process.
        """
        if not self.git_dir.is_dir():
            return []
        log = self._git("log", "--reverse", "--format=%H%x00%at%x00%B%x03")
        entries: list[HistoryEntry] = []
        for raw in log.stdout.split("\x03"):
            raw = raw.strip("\n")
            if not raw:
                continue
            sha, at, message = raw.split("\x00", 2)
            if sha == until:
                break  # reached this session's own baseline -- everything after is live activity, not prior-run history
            is_baseline = message.startswith(BASELINE_SUBJECT)
            agent, session_id, prompt = self._attribution_for(sha, message, is_baseline)
            paths = [p for p in self._git("show", "--format=", "--name-only", sha).stdout.splitlines() if p]
            for path in paths:
                diff = self._git("show", "--format=", sha, "--", path).stdout
                entries.append(HistoryEntry(str(self.root / path), diff, float(at), sha, agent, session_id, prompt, is_baseline))
                self.last_commit_for_path[path] = sha
        return entries

    def _attribution_for(self, sha: str, message: str, is_baseline: bool) -> tuple[str, str, str]:
        """Resolves a commit's (agent, session_id, prompt): git notes are the source of truth; falls back to
        parsing the commit message itself for repos written before notes-based attribution existed."""
        if is_baseline:
            return "", "", ""
        note = self._git("notes", "show", sha, check=False)
        if note.returncode == 0:
            return _parse_attribution(note.stdout)
        return _parse_attribution(message)  # legacy repos: attribution lived in the commit message itself

    def commits_after(self, rel: str, sha: str) -> list[AffectedCommit]:
        """Returns every commit after sha (exclusive) up to HEAD that also touched rel, newest first --
        restoring rel to its pre-sha content would silently overwrite each of these commits' change to
        that file, so callers (the TUI's revert confirmation screen) show this list before reverting."""
        log = self._git("log", "--format=%H%x00%at%x00%B%x03", f"{sha}..HEAD", "--", rel)
        affected = []
        for raw in log.stdout.split("\x03"):
            raw = raw.strip("\n")
            if not raw:
                continue
            commit_sha, at, message = raw.split("\x00", 2)
            is_baseline = message.startswith(BASELINE_SUBJECT)
            agent, _, _ = self._attribution_for(commit_sha, message, is_baseline)
            affected.append(AffectedCommit(commit_sha, float(at), agent))
        return affected

    def revert_file_to(self, rel: str, sha: str):
        """Restores rel's on-disk content to what it was immediately before sha, by writing the old
        bytes straight to disk -- no git-revert, no commit made here. Like any other write, the
        caller's next commit_dirty() records this as its own new commit, so a revert is
        indistinguishable from a human editing the file back by hand; consistent with this repo's
        commit-on-write design, where every commit is exactly what was on disk, never reconstructed.
        Deletes rel if it didn't exist yet at that point in history. Idempotent: if already reverted,
        does nothing."""
        if self.is_commit_reverted(sha):
            return  # this commit already reverted, nothing to do
        self.break_human_edit_batch()
        parent = f"{sha}^"
        assert self._git("rev-parse", "--verify", "--quiet", parent, check=False).returncode == 0, \
            f"commit {sha} has no parent -- there is no earlier state of {rel} to revert to"
        path = self.root / rel
        existed = self._git("cat-file", "-e", f"{parent}:{rel}", check=False).returncode == 0
        if not existed:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(self._show_at(parent, rel))
        self._pending_reverts[rel] = sha  # track for annotation after commit_dirty

    def _show_at(self, ref: str, rel: str) -> bytes:
        """Returns rel's raw content at ref, or b'' if it doesn't exist there (e.g. ref^ before the file
        was created). Bytes, not str: a tracked blob's content isn't guaranteed to be valid UTF-8 --
        tracking binaries is a supported use case, not just text source files."""
        result = self._git("show", f"{ref}:{rel}", check=False, text=False)
        return result.stdout if result.returncode == 0 else b""

    def combined_diff(self, rel: str, first_commit: str, last_commit: str) -> str:
        """Returns rel's net diff from just before first_commit through last_commit."""
        return self._git("diff", f"{first_commit}^", last_commit, "--", rel).stdout

    def is_commit_reverted(self, sha: str) -> bool:
        """True if this commit is currently reverted (has a revert that itself hasn't been reverted)."""
        note = self._git("notes", "show", sha, check=False)
        if note.returncode != 0:
            return False
        # Find the revert commit that reverted this one
        reverted_by = None
        for line in note.stdout.split('\n'):
            if line.startswith(REVERTED_BY_TRAILER):
                reverted_by = line[len(REVERTED_BY_TRAILER):].strip()
                break
        if not reverted_by:
            return False
        # Recursively check: is the revert commit itself reverted? If so, this commit is no longer reverted
        return not self.is_commit_reverted(reverted_by)

    def get_reverted_commit(self, sha: str) -> str | None:
        """If this commit is a revert, returns the SHA of the original commit it reverts, else None."""
        note = self._git("notes", "show", sha, check=False)
        if note.returncode == 0:
            for line in note.stdout.split('\n'):
                if line.startswith(REVERTS_TRAILER):
                    return line[len(REVERTS_TRAILER):].strip()
        return None

    def _mark_as_revert(self, revert_commit: str, original_commit: str):
        """Marks revert_commit as a revert of original_commit, and flags original_commit as reverted.
        Also attributes the revert commit to the 'octo' system agent."""
        self._git("notes", "append", "-m", f"{AGENT_TRAILER}octo", revert_commit)
        self._git("notes", "append", "-m", f"{REVERTS_TRAILER}{original_commit}", revert_commit)
        self._git("notes", "append", "-m", f"{REVERTED_BY_TRAILER}{revert_commit}", original_commit)

    def _dirty_paths(self) -> set[str]:
        """Returns paths (relative to root) that differ from the shadow repo's last commit."""
        status = self._git("status", "--porcelain")
        return {line[3:] for line in status.stdout.splitlines()}

    def _is_debounced_dirty(self, rel: str, now: float) -> bool:
        """True once rel has stayed dirty with the same on-disk stat signature for the debounce window."""
        signature = self._dirty_signature(rel)
        state = self._dirty_path_state.get(rel)
        if state is None or state.signature != signature:
            self._dirty_path_state[rel] = DirtyPathState(signature, now)
            return False
        return now - state.changed_at >= DIRTY_DEBOUNCE_SECONDS

    def _dirty_signature(self, rel: str) -> tuple[int, int] | None:
        """Returns a cheap change signature for rel, or None when the dirty path is deleted."""
        try:
            stat = (self.root / rel).lstat()
        except FileNotFoundError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _last_commit_touching(self, rel: str) -> str:
        """Returns the newest shadow-repo commit touching rel, or '' if none remains after a reset."""
        result = self._git("log", "-1", "--format=%H", "--", rel, check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    def break_human_edit_batch(self):
        """Closes every open human amend-window so the next human save starts a fresh shadow commit."""
        self._open_human_commits = {}

    def _project_head(self) -> str | None:
        """Returns the real repo HEAD commit for self.root, or None if this tree is not in a git repo."""
        result = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def refresh_human_commit_boundary(self) -> bool:
        """Closes human amend-windows after any real git commit in the watched tree."""
        current = self._project_head()
        if current != self._last_project_head:
            self.break_human_edit_batch()
            self._last_project_head = current
            return True
        return False


def revert_commit(root: Path, shadow_dir_name: str, sha: str):
    """Reverts one shadow-repo commit via `git revert`, writing the undone content straight back
    into root. Unused by the TUI today (which uses ShadowGitWatcher.revert_file_to's direct
    content-restore instead) -- kept as the alternate whole-commit mechanism for a future per-file
    vs. whole-commit revert choice: unlike revert_file_to, this fails/conflicts loudly if a later
    commit touched the same path, rather than silently overwriting it."""
    git_dir = root / shadow_dir_name  # shadow repo whose work-tree is root; same one commit_dirty()/attribute() committed into
    assert git_dir.is_dir(), "revert requires an initialized shadow repo"
    subprocess.run(
        ["git", f"--git-dir={git_dir}", f"--work-tree={root}", "revert", "--no-edit", sha],
        check=True,
    )
