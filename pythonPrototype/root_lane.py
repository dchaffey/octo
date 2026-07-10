#!/usr/bin/env python3
"""Single-writer serialization for the two operations that mutate root's shadow-repo HEAD /
working tree: ShadowGitWatcher.commit_dirty()'s regular poll-tick flush, and worktree_sync.py's
up-sync file-apply step. Everything else in octo_tui.py runs on the single Textual poll-loop
tick and is implicitly serialized by that already; these two aren't, once up-sync runs on its
own `@work` worker concurrently with the next poll tick's own commit_dirty() call (see
WORKTREE_SYNC_PLAN.md's "Root's serialized lane")."""

import threading


class RootLane:
    """A plain mutex, used as a context manager around each of the two root-mutating operations.
    No literal queue: with only two callers and no cross-worktree ordering requirement beyond
    "not at the same instant", a lock is the whole mechanism the doc's "single real queue"
    description calls for."""

    def __init__(self):
        self._lock = threading.Lock()  # held for the duration of one commit_dirty() tick or one up-sync file-apply step

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._lock.release()
        return False
