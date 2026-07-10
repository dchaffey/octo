#!/usr/bin/env python3
"""Creates one throwaway local git clone per agent invocation, for octo's worktree-per-agent
scheme (see agent_launcher.py): each `octo run <agent>` launch gets its own independent clone
under ~/.octo/worktrees/, with its own branch and commit history entirely separate from the real
project repo. Its git-dir is named SHADOW_DIR_NAME (.octo), not `.git` -- a plain `git clone`
leaves a `.git` dir behind, so create_agent_worktree renames it immediately after cloning, the
same convention root's own shadow repo uses (see shadow_repo.ShadowGitWatcher.git_dir). No agent
worktree ever carries a `.git` of its own; only the real project repo does. A clone, not `git
worktree add` -- the real repo's branches, refs, and worktree registry are never touched; the
agent's isolation lives entirely inside the throwaway clone, and removing it (see
agent_launcher._run_agent_and_cleanup) is a single directory delete, no git bookkeeping in the
real repo to undo.

Cloned from the watched root's shadow repo (root/.octo -- see shadow_repo.SHADOW_DIR_NAME), never
from the real project .git: ShadowGitWatcher.commit_dirty() keeps the shadow repo's HEAD an
accurate, near-real-time git-committed snapshot of what's actually on disk, while the real repo's
last commit is whatever the human last committed manually and can be arbitrarily stale/divergent
from current disk state (uncommitted edits, uncommitted deletions, ...). Cloning from the real repo
instead would check out that stale tree verbatim into every fresh agent worktree -- including files
the human already deleted on disk but never got around to committing away."""

import json
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path

from shadow_repo import SHADOW_DIR_NAME

WORKTREES_ROOT = Path.home() / ".octo" / "worktrees"  # parent dir every agent clone is created under, keyed by repo slug
AGENT_CONFIG_DIR_NAMES = (".claude", ".codex", ".agents")  # per-agent hook config dirs hook_installer.py writes into a clone; mirrors its install_*_hook path literals -- keep in sync if those change
AGENT_BRANCH_PREFIX = "octo/"  # branch namespace create_agent_worktree uses, kept out of the user's own branches; purely cosmetic now that clones live in their own repo, but keeps naming consistent/collision-free
OWNER_MARKER_NAME = "octo-owner.json"  # filename (under a clone's SHADOW_DIR_NAME git-dir) naming which live octo process/root owns this clone; read back by octo_hook.py's Stop handler, which only knows its own cwd


@dataclass
class WorktreeHandle:
    """One freshly created agent clone."""
    path: Path        # absolute path to the new clone's checkout
    branch: str       # branch name checked out there, unique to this invocation


def _repo_slug(repo_root: Path) -> str:
    """Short, filesystem-safe identifier for repo_root, unique enough that two different repos
    sharing a directory name (e.g. two checkouts both named 'app') don't collide under
    WORKTREES_ROOT -- name kept for readability, hash suffix for uniqueness."""
    digest = sha1(str(repo_root).encode()).hexdigest()[:8]  # short hash of the repo's absolute path -- disambiguates same-named repos
    return f"{repo_root.name}-{digest}"


def _git(clone_path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Runs one git command against clone_path's own git-dir (clone_path/SHADOW_DIR_NAME -- see
    create_agent_worktree, which renames the git-dir off of `.git` right after cloning), always
    passing --git-dir/--work-tree explicitly since git only auto-detects a directory literally
    named `.git`; mirrors shadow_repo.ShadowGitWatcher._git's convention for the same reason."""
    git_dir = clone_path / SHADOW_DIR_NAME
    return subprocess.run(
        ["git", f"--git-dir={git_dir}", f"--work-tree={clone_path}", *args],
        capture_output=True, text=True, check=check,
    )


def _ensure_agent_dirs_ignored(clone_path: Path):
    """Appends any of (SHADOW_DIR_NAME, *AGENT_CONFIG_DIR_NAMES) missing from clone_path's own
    SHADOW_DIR_NAME/info/exclude. SHADOW_DIR_NAME itself must be listed here: unlike a directory
    literally named `.git`, which git refuses to ever track as content on its own, a git-dir named
    SHADOW_DIR_NAME (see create_agent_worktree's rename) is just an ordinary-looking directory to
    git and gets swept up by `git add -A` like anything else unless explicitly excluded -- exactly
    the same reason shadow_repo.ShadowGitWatcher._sync_exclude_file lists its own git_dir.name.
    AGENT_CONFIG_DIR_NAMES covers the hook config detect_and_install_hooks writes into the clone
    (see create_agent_worktree) -- a fresh clone only inherits the *tracked* .gitignore checked out
    at HEAD, which won't cover these dirs unless the project happens to already exclude them
    itself. info/exclude is local-only and this clone is throwaway anyway, but keeping it clean
    avoids confusing an agent that runs `git status` itself. Idempotent: only appends patterns not
    already present."""
    exclude_path = clone_path / SHADOW_DIR_NAME / "info" / "exclude"
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.is_file() else ""
    existing_lines = set(existing.splitlines())
    patterns_needed = (SHADOW_DIR_NAME, *AGENT_CONFIG_DIR_NAMES)  # this clone's own git-dir, plus per-agent hook config dirs -- none of which this clone's git should ever track
    missing = [name for name in patterns_needed if f"{name}/" not in existing_lines]
    if not missing:
        return
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    with exclude_path.open("a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        for name in missing:
            f.write(f"{name}/\n")


def create_agent_worktree(root: Path, agent_binary: str) -> WorktreeHandle:
    """Creates a fresh local clone of root's shadow repo (root/.octo -- see module docstring) for
    one agent invocation, checked out on a new branch off its HEAD, under
    WORKTREES_ROOT/<repo slug>/<agent_binary>-<uuid>. A clone (git auto-hardlinks the object store
    for a same-filesystem local source, so this is as cheap as `git worktree add` in practice)
    rather than a linked worktree, so the new branch and every commit the agent makes live only
    inside this clone's own git-dir -- invisible to `git branch`/`git worktree list` run against
    the real repo, and to the real repo entirely. `git clone` always creates that git-dir as
    `.git`; it's renamed to SHADOW_DIR_NAME immediately after, so the clone never carries a `.git`
    of its own (every op below and in worktree_sync.py addresses it via explicit --git-dir/
    --work-tree instead of relying on auto-detection). Asserts on failure (e.g. root's shadow repo
    not initialized yet) rather than falling back silently -- `octo run` should crash loudly, not
    silently launch the agent in the wrong place."""
    shadow_git_dir = root / SHADOW_DIR_NAME              # clone source: root's own shadow repo, not the real project .git
    assert shadow_git_dir.is_dir(), f"{root}'s shadow repo ({shadow_git_dir}) must exist before an agent worktree can be cloned from it"
    tag = uuid.uuid4().hex[:8]                          # short unique suffix distinguishing this invocation's branch/dir
    branch = f"{AGENT_BRANCH_PREFIX}{agent_binary}-{tag}"  # branch name, namespaced under AGENT_BRANCH_PREFIX to stay out of the user's own branches
    clone_path = WORKTREES_ROOT / _repo_slug(root) / f"{agent_binary}-{tag}"  # this invocation's checkout dir
    clone_path.parent.mkdir(parents=True, exist_ok=True)  # repo-slug dir must exist before `git clone` can create the leaf dir under it
    subprocess.run(["git", "clone", str(shadow_git_dir), str(clone_path)], capture_output=True, text=True, check=True)
    (clone_path / ".git").rename(clone_path / SHADOW_DIR_NAME)  # drop the clone's `.git` naming entirely -- nothing inside a git-dir depends on its own directory name, so this is a plain, safe move
    _git(clone_path, "checkout", "-b", branch)
    _ensure_agent_dirs_ignored(clone_path)              # ignored inside the clone's own info/exclude, never the real repo's
    return WorktreeHandle(clone_path, branch)


def write_owner_marker(clone_path: Path, owner_pid: int, root: Path):
    """Records which live octo process (and which root it watches) owns clone_path, under its
    SHADOW_DIR_NAME git-dir -- never in the tracked working tree, and outside
    AGENT_CONFIG_DIR_NAMES so it needs no separate info/exclude entry. octo_hook.py's Stop handler
    reads this back via read_owner_marker, keyed only by its own cwd (a hook subprocess has no
    other way to learn which octo process is watching the worktree it's running in)."""
    marker_path = clone_path / SHADOW_DIR_NAME / OWNER_MARKER_NAME
    marker_path.write_text(json.dumps({"pid": owner_pid, "root": str(root)}), encoding="utf-8")


def read_owner_marker(clone_path: Path) -> dict | None:
    """Reads back the owner info write_owner_marker recorded for clone_path, or None if clone_path
    carries no marker (e.g. not an octo-managed clone at all)."""
    marker_path = clone_path / SHADOW_DIR_NAME / OWNER_MARKER_NAME
    if not marker_path.is_file():
        return None
    return json.loads(marker_path.read_text(encoding="utf-8"))


@dataclass
class WorktreeInfo:
    """One worktree/clone currently live for a repo's branches overview."""
    path: Path      # absolute path to the checkout
    branch: str     # branch checked out there (short name, without the refs/heads/ prefix); '' if detached
    commit: str     # HEAD commit sha checked out there
    is_main: bool   # True for the repo's main working tree


def _parse_worktree_list(porcelain: str) -> list[WorktreeInfo]:
    """Parses `git worktree list --porcelain` output into one WorktreeInfo per block ('worktree'/
    'HEAD'/optional 'branch' lines, blocks separated by a blank line), tagging the first block as
    the repo's main working tree -- git always lists it first, and it's never itself named by a
    'branch <ref>' line the way a linked worktree's own is, so there's no other structural marker
    to key off."""
    entries = []
    path = commit = None  # worktree path / HEAD sha for the block currently being parsed
    branch = ""            # branch for the block currently being parsed; stays '' for a detached HEAD
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
        elif line.startswith("HEAD "):
            commit = line.removeprefix("HEAD ")
        elif line.startswith("branch "):
            branch = line.removeprefix("branch ").removeprefix("refs/heads/")
        elif line == "" and path is not None:
            entries.append(WorktreeInfo(path, branch, commit, is_main=not entries))
            path = commit = None
            branch = ""
    return entries


def list_worktrees(repo_root: Path) -> list[WorktreeInfo]:
    """Returns every *linked* worktree currently registered for the repo containing repo_root, via
    `git worktree list --porcelain` -- always includes at least the main working tree itself.
    Agent clones (see create_agent_worktree) are independent repos, not linked worktrees, so they
    never appear here; see list_agent_worktrees for the combined view."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
        capture_output=True, text=True, check=True,
    )
    return _parse_worktree_list(result.stdout)


def _clone_info(clone_path: Path) -> WorktreeInfo | None:
    """Reads clone_path's currently checked-out branch and HEAD commit directly -- a clone is a
    fully independent repo (see create_agent_worktree), so there's no `git worktree list` entry to
    parse for it. Returns None if clone_path isn't a valid git repo right now, e.g. a race against
    agent_launcher's post-exit cleanup deleting it mid-scan."""
    branch_result = _git(clone_path, "branch", "--show-current", check=False)
    if branch_result.returncode != 0:
        return None
    commit_result = _git(clone_path, "rev-parse", "HEAD", check=False)
    if commit_result.returncode != 0:
        return None
    return WorktreeInfo(clone_path, branch_result.stdout.strip(), commit_result.stdout.strip(), is_main=False)


def _list_agent_clones(repo_root: Path) -> list[WorktreeInfo]:
    """Scans WORKTREES_ROOT/<repo-slug>/ for agent clone directories still present on disk --
    reflects live reality, since a clone already removed by agent_launcher's post-exit cleanup
    simply won't appear."""
    clones_root = WORKTREES_ROOT / _repo_slug(repo_root)
    if not clones_root.is_dir():
        return []
    infos = (_clone_info(path) for path in sorted(clones_root.iterdir()) if path.is_dir())
    return [info for info in infos if info is not None]


def list_agent_worktrees(repo_root: Path) -> list[WorktreeInfo]:
    """Returns the worktrees relevant to octo's branches overview: the repo's main working tree
    (via list_worktrees) plus every agent clone still on disk (via _list_agent_clones). Unlike
    session_registry's WorktreeRegistration inbox, which only covers what one octo process has
    drained since it started, this reflects live reality on both sides. Any other stray *linked*
    worktree the user created themselves is neither the main tree nor an agent clone, so it's left
    out."""
    main = [w for w in list_worktrees(repo_root) if w.is_main]
    return main + _list_agent_clones(repo_root)
