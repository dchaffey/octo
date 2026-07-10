#!/usr/bin/env python3
"""Down-sync (root -> agent worktree) and up-sync (agent worktree -> root) for octo's
worktree-per-agent scheme (see worktree_manager.py, agent_launcher.py), run at each worktree's
turn boundary (see octo_hook.py's Stop-hook signal + session_registry.notify_turn_ended).

Both operate on *real* git: the worktree (a real clone, per create_agent_worktree) and root's
real repo, which share ancestry via that original `git clone` -- this is what makes
`rebase -X ours` / a real 3-way merge meaningful at all. Root's real branch ref is only ever
read here, never advanced: down-sync fetches it as a rebase target; up-sync trial-merges against
it in a disposable scratch clone and, if clean, copies just the resulting file bytes onto root's
real working tree -- the existing ShadowGitWatcher.commit_dirty()/attribute_settled() pipeline
(driven by the caller, octo_tui.py) is what actually records that landing, so up-synced edits
show up in the live feed like any other attributed edit instead of silently rewriting the
human's checked-out branch."""

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

UPSYNC_CLONE_PREFIX = "octo-upsync-"  # tempfile.mkdtemp prefix for up_sync's disposable scratch clone, so a stray leftover is identifiable


@dataclass
class SyncResult:
    """Outcome of one down_sync/up_sync call."""
    ok: bool                          # True if this step completed without error (whether or not anything actually changed)
    conflicted: bool                    # True if a genuine content conflict was hit -- caller should pause the worktree, not retry automatically
    detail: str                           # short human-readable status/error, shown in BranchesScreen
    changed_paths: list[str] = field(default_factory=list)  # up_sync only: repo-relative paths landed onto root's working tree; empty for down_sync or a no-op up_sync
    synced_sha: str = ""                    # up_sync only, when ok: worktree HEAD this result reflects, for the caller to remember as last_synced_sha; "" when not applicable


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Runs one git command against repo via `-C`, mirroring shadow_repo.ShadowGitWatcher._git's
    convention (this module has no single fixed git-dir/work-tree pair the way that class does,
    so -C is the right equivalent here)."""
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check)


def down_sync(worktree_path: Path, root_repo_path: Path) -> SyncResult:
    """Rebases worktree_path onto root_repo_path's current branch tip, root's incoming content
    winning any textual collision (`-X ours`) -- non-conflicting worktree edits (different
    lines/files) are preserved as normal. A no-op, reported ok, if the worktree already contains
    root's tip. `-X ours` doesn't eliminate every conflict class (e.g. delete/modify, add/add) --
    a residual failure aborts the rebase (leaving worktree_path exactly as it was) and reports
    conflicted=True."""
    root_branch = _git(root_repo_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    fetch = _git(worktree_path, "fetch", "origin", root_branch, check=False)
    if fetch.returncode != 0:
        return SyncResult(ok=False, conflicted=False, detail=f"fetch from root failed: {fetch.stderr.strip()}")
    fetch_head = _git(worktree_path, "rev-parse", "FETCH_HEAD").stdout.strip()
    current = _git(worktree_path, "rev-parse", "HEAD").stdout.strip()
    already_current = _git(worktree_path, "merge-base", "--is-ancestor", fetch_head, current, check=False).returncode == 0
    if already_current:
        return SyncResult(ok=True, conflicted=False, detail="already up to date with root")
    rebase = _git(worktree_path, "rebase", "-X", "ours", fetch_head, check=False)
    if rebase.returncode != 0:
        _git(worktree_path, "rebase", "--abort", check=False)
        return SyncResult(ok=False, conflicted=True, detail="down-sync conflicted; rebase aborted")
    return SyncResult(ok=True, conflicted=False, detail="rebased onto root")


def _diff_status_pairs(scratch_dir: Path, before: str, after: str) -> list[tuple[str, str]]:
    """Parses `git diff --name-status before after` into (status_letter, path) pairs -- a rename/
    copy's old half is reported as a deletion and its new half as an addition, since that's all
    up_sync needs to know to apply the result onto root's working tree file-by-file."""
    diff = _git(scratch_dir, "diff", "--name-status", before, after).stdout
    pairs = []
    for line in diff.splitlines():
        fields = line.split("\t")
        status = fields[0][0]  # first char only -- drops a rename/copy's trailing similarity percentage (e.g. "R100")
        if status in ("R", "C"):
            pairs.append(("D", fields[1]))
            pairs.append((status, fields[2]))
        else:
            pairs.append((status, fields[1]))
    return pairs


def _apply_changed_paths(scratch_dir: Path, root_repo_path: Path, pairs: list[tuple[str, str]]) -> list[str]:
    """Writes each changed path's post-merge bytes from scratch_dir onto root_repo_path, or
    deletes it there for a 'D' status -- the targeted apply up_sync uses instead of overwriting
    root's whole working tree, so anything the merge didn't touch is left alone. Returns the
    repo-relative paths written or deleted, for the caller to match against commit_dirty()'s
    settled edits when deciding which ones to attribute to the worktree's agent."""
    changed = []
    for status, rel in pairs:
        dst = root_repo_path / rel
        if status == "D":
            dst.unlink(missing_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes((scratch_dir / rel).read_bytes())
        changed.append(rel)
    return changed


def _commit_worktree_dirty(worktree_path: Path):
    """Stages and commits any uncommitted changes sitting in worktree_path's own working tree.

    The agent (Claude/Codex/Antigravity) only ever calls its own Write/Edit-equivalent tools,
    which touch disk directly -- nothing about a tool call itself runs git, so without this step
    up_sync's `rev-parse HEAD` never moves and every turn looks like a no-op forever. Committing
    straight onto worktree_path's own real branch (rather than a separate shadow git-dir, as
    root's ShadowGitWatcher uses) is deliberate: that branch is already throwaway and invisible to
    the user -- create_agent_worktree's clone is deleted whole on agent exit, and only the
    resulting file bytes ever land in root, never this commit itself -- so there's no human
    working state here to protect the way root's shadow dir protects the human's real HEAD/index.
    Committing for real also means down_sync's `rebase -X ours` and up_sync's 3-way merge keep
    working exactly as built, since both depend on this branch sharing real ancestry with root.
    A no-op if nothing is dirty -- "nothing to commit" is the expected outcome on a turn that made
    no file changes, not an error."""
    _git(worktree_path, "add", "-A")
    result = _git(worktree_path, "commit", "-q", "-m", "octo: agent turn", check=False)
    assert result.returncode == 0 or "nothing to commit" in result.stdout, \
        f"unexpected git commit failure in {worktree_path}: {result.stdout}"


def up_sync(worktree_path: Path, worktree_branch: str, root_repo_path: Path, last_synced_sha: str | None) -> SyncResult:
    """Lands worktree_path's new commits (if any) onto root_repo_path's real working tree, never
    touching root's real branch ref: first commits whatever the agent left dirty on disk this turn
    (see _commit_worktree_dirty), then trial-merges the worktree's branch against root's current
    tip in a disposable scratch clone (an unbiased 3-way merge, which can genuinely conflict,
    unlike down_sync's -X ours), and if clean, copies just the changed files' resulting bytes
    onto root_repo_path. A no-op, reported ok with no changed_paths, if the worktree's HEAD hasn't
    moved past last_synced_sha ("if that turn produced new commits" -- see WORKTREE_SYNC_PLAN.md).
    Conflict -> merge aborted, scratch clone discarded, root_repo_path untouched, conflicted=True.
    """
    _commit_worktree_dirty(worktree_path)
    current = _git(worktree_path, "rev-parse", "HEAD").stdout.strip()
    if current == last_synced_sha:
        return SyncResult(ok=True, conflicted=False, detail="no new commits this turn", synced_sha=current)
    root_head = _git(root_repo_path, "rev-parse", "HEAD").stdout.strip()
    scratch_dir = Path(tempfile.mkdtemp(prefix=UPSYNC_CLONE_PREFIX))
    try:
        subprocess.run(["git", "clone", str(root_repo_path), str(scratch_dir)], capture_output=True, text=True, check=True)
        fetch = _git(scratch_dir, "fetch", str(worktree_path), worktree_branch, check=False)
        if fetch.returncode != 0:
            return SyncResult(ok=False, conflicted=False, detail=f"fetch from worktree failed: {fetch.stderr.strip()}")
        merge = _git(scratch_dir, "merge", "--no-edit", "FETCH_HEAD", check=False)
        if merge.returncode != 0:
            _git(scratch_dir, "merge", "--abort", check=False)
            return SyncResult(ok=False, conflicted=True, detail="up-sync conflicted; merge aborted")
        pairs = _diff_status_pairs(scratch_dir, root_head, "HEAD")
        if not pairs:
            return SyncResult(ok=True, conflicted=False, detail="merge clean, no file changes", synced_sha=current)
        changed = _apply_changed_paths(scratch_dir, root_repo_path, pairs)
        return SyncResult(ok=True, conflicted=False, detail=f"landed {len(changed)} file(s)",
                           changed_paths=changed, synced_sha=current)
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)
