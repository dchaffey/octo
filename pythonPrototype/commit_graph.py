#!/usr/bin/env python3
"""Synthesizes a GitKraken-style lane graph from octo's strictly-linear shadow history.

The root .octo repo has no real 2-parent merge commits (see COMMIT_GRAPH_PLAN.md): every commit
is single-parent, and an agent's worktree work lands as ordinary commits carrying a Branch: note.
So lanes and merge edges can't be read from git parent topology -- they're synthesized here from
each commit's branch attribution. Kept out of the TUI so it's unit-testable (data dominates)."""

from dataclasses import dataclass

from shadow_repo import GraphCommit


@dataclass
class GraphRow:
    """One commit placed on the synthetic rail graph, ready for the TUI to render as text glyphs."""
    commit: GraphCommit          # the commit this row draws
    lane: int                    # column index this commit's node sits in; 0 = mainline
    active_lanes: frozenset[int]  # lanes with a vertical bar drawn on this row (always includes 0 and this row's lane)
    is_merge: bool               # true when this node ends its branch's contiguous run -- work landing back into root
    merge_from_lane: int | None  # the branch lane the merge glyph joins from back to lane 0; None unless is_merge


def _is_mainline(commit: GraphCommit) -> bool:
    """True if the commit belongs in lane 0: the baseline, Human commits, and any root-lane agent
    commit -- all of which carry no Branch: note. Only worktree-lane agent commits have a branch."""
    return commit.branch == ""


def _next_free_lane(occupied: set[int]) -> int:
    """Returns the lowest lane column >= 1 not currently claimed by an active branch."""
    lane = 1
    while lane in occupied:
        lane += 1
    return lane


def layout(commits: list[GraphCommit]) -> list[GraphRow]:
    """Places each commit (display order, oldest-first) onto a lane. Mainline commits sit in lane 0;
    each contiguous run of one branch claims the next free lane on its first commit and frees it on
    its last commit, which is flagged as a merge (that branch's work landing back into root). Asserts
    inputs rather than guarding -- a malformed commit list is a programming error, not a runtime case."""
    branch_lanes: dict[str, int] = {}  # branch name -> lane currently claimed by its open run
    occupied: set[int] = set()         # lanes (>=1) claimed by an open branch run right now
    rows: list[GraphRow] = []
    for i, commit in enumerate(commits):
        assert commit.timestamp >= 0, "commit timestamps must be non-negative epoch seconds"
        if _is_mainline(commit):
            lane = 0
        else:
            branch = commit.branch
            if branch not in branch_lanes:
                branch_lanes[branch] = _next_free_lane(occupied)
                occupied.add(branch_lanes[branch])
            lane = branch_lanes[branch]
        is_merge = _run_ends(commits, i)
        active = frozenset(occupied | {0, lane})  # bars for mainline, every open branch, and this node's own lane
        rows.append(GraphRow(commit, lane, active, is_merge, lane if is_merge else None))
        if is_merge:
            assert lane != 0, "mainline commits never end a branch run"
            occupied.discard(lane)
            del branch_lanes[commit.branch]
    assert not occupied, "every claimed branch lane must be freed by a merge before layout ends"
    return rows


def _run_ends(commits: list[GraphCommit], i: int) -> bool:
    """True if commit i is the last of its contiguous same-branch run -- the merge point. Never for
    mainline commits; true for a branch commit whose successor is mainline, a different branch, or
    absent (end of log)."""
    commit = commits[i]
    if _is_mainline(commit):
        return False
    if i + 1 >= len(commits):
        return True
    nxt = commits[i + 1]
    return _is_mainline(nxt) or nxt.branch != commit.branch
