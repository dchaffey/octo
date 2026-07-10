#!/usr/bin/env python3
"""Installs octo's hooks (octo_hook.py) into every supported agent's project-level config, for
whichever agent CLIs are found on PATH. Every agent gets the pre-write PreToolUse/BeforeTool
hook, which always allows the write -- see octo_hook.py -- so this is safe to run before any
real gating logic exists; a later change only needs to edit octo_hook.py's body, not this
installer. Claude additionally gets a Stop hook, the turn-boundary signal worktree_sync.py's
sync sequence hooks into (see octo_hook.py's Stop handling) -- Codex's and Antigravity's hook
event names for an equivalent "turn just ended" signal are unconfirmed, so they don't get one
yet; their worktrees register but are never synced until that's verified.

Codex's config path/event name/JSON envelope and Antigravity's event name/JSON envelope are
assumed to mirror Claude Code's (per a July-2026 vendor summary, unverified against live docs --
see install_codex_hook/install_antigravity_hook). Antigravity's config path is confirmed."""

import argparse
import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from agent_detection import detect_available_agents
from agent_watcher import (
    ANTIGRAVITY_SHELL_TOOL_NAME,
    ANTIGRAVITY_WRITE_TOOL_NAMES,
    CLAUDE_SHELL_TOOL_NAME,
    EDIT_TOOL_NAMES,
)


@dataclass
class HookInstallResult:
    """Outcome of installing one agent's hook, reported back to the CLI entrypoint."""
    agent: str                 # human-readable agent name, e.g. "Claude"
    binary_found: bool         # whether the agent's CLI was found on PATH via shutil.which()
    config_path: Path | None   # config file written/checked; None if binary_found is False
    already_present: bool      # True if an equivalent hook entry already existed (no write performed); meaningless if binary_found is False


def _read_json_config(path: Path) -> dict:
    """Loads path's JSON content, or {} if it doesn't exist yet -- config files are optional
    until the first hook is installed, never auto-created empty ahead of time."""
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_config(path: Path, config: dict):
    """Writes config back to path as pretty-printed JSON, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _find_existing_entry(hook_list: list[dict], hook_marker: str) -> dict | None:
    """Returns the entry in hook_list whose command already contains hook_marker,
    or None if no such entry exists yet."""
    for entry in hook_list:
        for inner in entry.get("hooks", []):
            if hook_marker in inner.get("command", ""):
                return entry
    return None


def _merge_hook_entry(config: dict, event_name: str, matcher: str | None,
                       hook_marker: str, hook_command: str) -> tuple[dict, bool]:
    """Adds a {matcher (if given), hooks:[{type:command,command}]} entry under
    config['hooks'][event_name] if no entry containing hook_marker is already there; returns
    (config, already_present). Never touches any other key already in config or config['hooks']
    -- a hand-edited settings file's unrelated keys (model, theme, other hooks, ...) survive
    untouched.

    matcher is None for events with nothing to match against (e.g. Stop -- confirmed against live
    docs: "No matcher support -- Stop hooks fire on every occurrence and ignore any matcher field
    you specify"), in which case the key is omitted entirely rather than written as a meaningless
    value.

    Matches on hook_marker as a substring of the command, not the whole command string, because
    hook_command embeds octo's own invocation (see _octo_invocation), which legitimately differs
    between venv/system/frozen-binary runs -- exact-string matching would append a duplicate entry
    every time the installer runs under a different interpreter/binary path."""
    hooks_root = config.setdefault("hooks", {})
    event_list = hooks_root.setdefault(event_name, [])
    if _find_existing_entry(event_list, hook_marker) is not None:
        return config, True
    entry = {"hooks": [{"type": "command", "command": hook_command}]}
    if matcher is not None:
        entry = {"matcher": matcher, **entry}
    event_list.append(entry)
    return config, False


def _install_hook_for_agent(agent_name: str, project_root: Path, relative_config_path: Path,
                             event_name: str, matcher: str | None, hook_marker: str,
                             hook_command: str) -> HookInstallResult:
    """Shared install path: reads relative_config_path under project_root (if present), adds our
    entry under event_name unless one already contains hook_marker, writes back only if
    something changed."""
    assert project_root.is_dir(), f"{agent_name} hook install requires an existing project directory"
    config_path = project_root / relative_config_path
    config = _read_json_config(config_path)
    config, already_present = _merge_hook_entry(config, event_name, matcher, hook_marker, hook_command)
    if not already_present:
        _write_json_config(config_path, config)
    return HookInstallResult(agent_name, True, config_path, already_present)


def install_claude_hook(project_root: Path, hook_marker: str, hook_command: str) -> HookInstallResult:
    """Installs octo's PreToolUse hook (placeholder, always approves) and its Stop hook (turn-
    boundary signal for worktree_sync.py -- see octo_hook.py) into
    <project_root>/.claude/settings.json, in one read-merge-merge-write pass so both entries land
    in a single write. Schema confirmed against a live ~/.claude/settings.json on this machine
    (PreToolUse) and against live docs (Stop -- see _merge_hook_entry)."""
    assert project_root.is_dir(), "Claude hook install requires an existing project directory"
    config_path = project_root / ".claude" / "settings.json"
    config = _read_json_config(config_path)
    matcher = "|".join((*EDIT_TOOL_NAMES, CLAUDE_SHELL_TOOL_NAME))  # single source of truth: which Claude tools write files
    config, pre_already = _merge_hook_entry(config, "PreToolUse", matcher, hook_marker, hook_command)
    config, stop_already = _merge_hook_entry(config, "Stop", None, hook_marker, hook_command)
    if not (pre_already and stop_already):
        _write_json_config(config_path, config)
    return HookInstallResult("Claude", True, config_path, pre_already and stop_already)


def install_codex_hook(project_root: Path, hook_marker: str, hook_command: str) -> HookInstallResult:
    """Installs octo's placeholder hook into <project_root>/.codex/hooks.json.
    UNVERIFIED: path, event name, and JSON shape are assumed to mirror Claude's; confirm against
    https://developers.openai.com/codex/hooks before relying on this. Matcher is "*" (match all
    tools) because Codex exposes no equivalent of EDIT_TOOL_NAMES for its file-writing operation
    (apply_patch isn't a discrete named tool the way Claude's Edit/Write are) -- over-matching is
    safe since this hook always approves; revisit before wiring real gating logic."""
    return _install_hook_for_agent("Codex", project_root, Path(".codex/hooks.json"),
                                    "PreToolUse", "*", hook_marker, hook_command)


def install_antigravity_hook(project_root: Path, hook_marker: str, hook_command: str) -> HookInstallResult:
    """Installs octo's placeholder hook into <project_root>/.agents/hooks.json (path confirmed).
    UNVERIFIED: "BeforeTool" event name and JSON envelope shape are assumed to mirror Claude's/
    Gemini CLI's; confirm against https://antigravity.google/docs/hooks before relying on this."""
    matcher = "|".join((*ANTIGRAVITY_WRITE_TOOL_NAMES, ANTIGRAVITY_SHELL_TOOL_NAME))
    return _install_hook_for_agent("Antigravity", project_root, Path(".agents/hooks.json"),
                                    "BeforeTool", matcher, hook_marker, hook_command)


AGENT_INSTALLERS = {"Claude": install_claude_hook, "Codex": install_codex_hook, "Antigravity": install_antigravity_hook}  # agent name -> its install_*_hook function, dispatched by detect_and_install_hooks


def _octo_invocation() -> list[str]:
    """Argv prefix that re-invokes octo itself, for the placeholder hook command to shell back
    into instead of running a bare script path -- a PyInstaller-frozen build's sys.executable IS
    the frozen octo binary (it has no separate `python3` alongside it), so `sys.executable
    some_script.py` doesn't work there the way it does under a plain interpreter; routing through
    octo's own `_hook` subcommand (see octo.py's SUBCOMMANDS) works identically either way."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str((Path(__file__).parent / "octo.py").resolve())]


def _detect_and_install_one(agent: str, binary_path: Path | None, project_root: Path,
                             hook_marker: str, hook_command: str) -> HookInstallResult:
    """Installs one agent's hook if its CLI binary was found on PATH, else returns a not-found result."""
    if binary_path is None:
        return HookInstallResult(agent, False, None, False)
    return AGENT_INSTALLERS[agent](project_root, hook_marker, hook_command)


def detect_and_install_hooks(project_root: Path) -> dict[str, HookInstallResult]:
    """For every agent whose CLI binary is found on PATH, installs (or confirms) octo's
    placeholder pre-write hook in project_root; agents whose binary isn't found get a
    not-installed result. hook_command re-invokes octo itself via its `_hook` subcommand (see
    _octo_invocation), so it works the same whether this installer is running under a plain
    interpreter or a frozen octo binary."""
    hook_marker = " _hook"  # stable regardless of interpreter/binary path -- see _merge_hook_entry
    hook_command = " ".join(shlex.quote(part) for part in (*_octo_invocation(), "_hook"))
    available = detect_available_agents()  # agent name -> resolved binary path, or None if not on PATH
    return {agent: _detect_and_install_one(agent, binary_path, project_root, hook_marker, hook_command)
            for agent, binary_path in available.items()}


def _print_result(agent: str, result: HookInstallResult):
    """Prints one human-readable status line for `python hook_installer.py` output."""
    if not result.binary_found:
        print(f"{agent}: CLI not found on PATH, skipped")
        return
    status = "already present" if result.already_present else "installed"
    print(f"{agent}: {status} -> {result.config_path}")


def main():
    """CLI entrypoint: detects agent CLIs on PATH and installs octo's placeholder pre-write hook
    into each one's project-level config under root."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, nargs="?", default=None,
                         help="project directory to install hooks into (default: current directory)")
    args = parser.parse_args()

    project_root = (args.root or Path.cwd()).resolve()  # directory whose agent configs get the hook installed
    for agent, result in detect_and_install_hooks(project_root).items():
        _print_result(agent, result)


if __name__ == "__main__":
    main()
