#!/usr/bin/env python3
"""Detects which supported agent CLIs (Claude Code, Codex, Antigravity) are present on this
machine. Shared by hook_installer.py and agent_launcher.py so the agent-name -> binary-name
mapping and PATH-lookup logic has one source of truth instead of being duplicated per caller."""

import shutil
from pathlib import Path

AGENT_BINARIES = {"Claude": "claude", "Codex": "codex", "Antigravity": "agy"}  # agent name -> CLI binary name to look up on PATH


def detect_available_agents() -> dict[str, Path | None]:
    """Returns agent name -> resolved absolute binary path for every agent in AGENT_BINARIES, or
    None for an agent whose CLI isn't found on PATH."""
    return {agent: _resolve_binary(binary) for agent, binary in AGENT_BINARIES.items()}


def resolve_agent_name(identifier: str) -> str | None:
    """Case-insensitively resolves identifier to its canonical AGENT_BINARIES key (e.g. "claude"
    or "Claude" -> "Claude"), matching against both the agent's display name and its CLI binary
    name. Returns None if identifier matches neither -- used by `octo run` to reject unknown agent
    identifiers before ever touching PATH."""
    lowered = identifier.lower()
    for agent, binary in AGENT_BINARIES.items():
        if lowered == agent.lower() or lowered == binary.lower():
            return agent
    return None


def resolve_single_agent(identifier: str) -> tuple[str | None, Path | None]:
    """Resolves identifier to (canonical agent name, resolved real binary path). Returns
    (None, None) if identifier doesn't match any AGENT_BINARIES entry; (agent, None) if it's
    recognized but the CLI isn't found on PATH; (agent, path) on success."""
    agent = resolve_agent_name(identifier)
    if agent is None:
        return None, None
    return agent, _resolve_binary(AGENT_BINARIES[agent])


def _resolve_binary(binary_name: str) -> Path | None:
    """Resolves binary_name to its absolute path via PATH lookup, or None if not found."""
    found = shutil.which(binary_name)
    return Path(found).resolve() if found else None
