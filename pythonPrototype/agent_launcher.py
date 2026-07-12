#!/usr/bin/env python3
"""`octo run <agent> [agent-args...]`: if an octo session is watching the current directory,
creates a fresh clone for this invocation, installs octo's hook config into it, tells the watching
octo process about it, then runs the real agent binary there as a supervised child -- octo's
worktree-per-agent scheme. Waits for the agent to exit for any reason (normal quit, crash, or
declining a "trust this folder" prompt before a session ever starts) and always removes the clone
afterward -- unlike relying on the agent's own session-end hook (which never fires if no session
actually started, and isn't even confirmed to exist for Codex/Antigravity), this cleanup is
unconditional and identical across every agent. If no octo session is watching, prints an error
and exits non-zero rather than launching the agent unisolated."""

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from agent_detection import AGENT_BINARIES, resolve_single_agent
from hook_installer import detect_and_install_hooks
from session_registry import find_matching_session, register_worktree, RunningSession
from worktree_manager import WorktreeHandle, create_agent_worktree, write_owner_marker


def main(argv: list[str]):
    """Entry point for octo.py's `run` dispatch, called with argv = sys.argv[2:] (everything after
    the literal "run" token) -- never argparse-parsed, so agent passthrough flags can't collide
    with octo's own flags."""
    if not argv:
        print("octo run: missing agent identifier. Usage: octo run <agent> [agent-args...]", file=sys.stderr)
        sys.exit(2)
    identifier, passthrough = argv[0], argv[1:]
    run(identifier, passthrough)


def run(identifier: str, passthrough: list[str]):
    """Resolves identifier to a supported agent CLI, requires a live octo session watching the
    current directory, redirects into a fresh per-invocation clone, then runs the real agent
    binary there with passthrough forwarded untouched. Exits non-zero without launching anything on
    any failure path below; exits with the agent's own exit code otherwise."""
    agent, binary_path = resolve_single_agent(identifier)
    if agent is None:
        print(f"octo run: unknown agent {identifier!r}. Supported: {', '.join(AGENT_BINARIES)} "
              f"(or their CLI names: {', '.join(AGENT_BINARIES.values())})", file=sys.stderr)
        sys.exit(2)
    if binary_path is None:
        print(f"octo run: {agent}'s CLI ({AGENT_BINARIES[agent]}) was not found on PATH.", file=sys.stderr)
        sys.exit(1)

    cwd = Path.cwd().resolve()
    session = find_matching_session(cwd)
    if session is None:
        print(f"octo run: no running octo session is watching {cwd} (or a related directory). "
              f"Start one first, e.g.: octo {cwd}", file=sys.stderr)
        sys.exit(1)

    # Automatically scope the worktree to the subdirectory if invoked from one
    subpath = None
    try:
        rel = cwd.relative_to(Path(session.root).resolve())
        if str(rel) != "." and str(rel) != "":
            subpath = str(rel)
    except ValueError:
        pass

    handle = _redirect_into_worktree(session, agent, subpath=subpath)
    sys.exit(_run_agent_and_cleanup(str(binary_path), passthrough, handle, owner_pid=session.pid))


def _redirect_into_worktree(session, agent: str, subpath: str | None = None) -> WorktreeHandle:
    """Creates this invocation's clone, tags it with the watching octo process's pid/root (so a
    Stop hook firing inside it later can find its way back -- see octo_hook.py), installs octo's
    hook config into it (fresh, not copied -- .claude/ etc. are gitignored, so nothing to inherit
    from the clone), and registers it with the watching octo process."""
    handle = create_agent_worktree(session.root, agent, subpath=subpath)  # cloned from session.root's shadow repo, not the real project .git
                                                          # (see worktree_manager module docstring) -- `agent` is the display
                                                          # name (e.g. "Claude"), which becomes the on-disk branch/dir naming
    write_owner_marker(handle.path, session.pid, session.root)
    detect_and_install_hooks(handle.agent_cwd)
    register_worktree(session, handle.path, handle.branch, agent)  # hands the new worktree off to the watching octo process's next poll tick
    return handle


def _monitor_parent(parent_pid: int, child: subprocess.Popen):
    """Periodically checks if the parent (watching octo) process is still alive.
    If it terminates, terminates the child agent process as well."""
    while child.poll() is None:
        try:
            os.kill(parent_pid, 0)
        except ProcessLookupError:
            # Parent is dead! Terminate the child.
            child.terminate()
            # Wait up to 3 seconds for it to exit, then kill if still running
            for _ in range(30):
                if child.poll() is not None:
                    break
                time.sleep(0.1)
            else:
                try:
                    child.kill()
                except ProcessLookupError:
                    pass
            break
        except PermissionError:
            pass
        time.sleep(1.0)


def _run_agent_and_cleanup(binary_path: str, passthrough: list[str], handle: WorktreeHandle, owner_pid: int) -> int:
    """Spawns the real agent binary as a child process with handle.agent_cwd as its cwd (inheriting our
    stdio directly, so the terminal session is fully interactive), waits for it to exit for any
    reason, then always removes handle.path -- see module docstring for why this replaces the old
    SessionEnd-hook-based cleanup."""
    child = subprocess.Popen([binary_path, *passthrough], cwd=handle.agent_cwd, env=_exec_env())

    # Spawn background thread to monitor the parent octo process liveness
    monitor_thread = threading.Thread(target=_monitor_parent, args=(owner_pid, child), daemon=True)
    monitor_thread.start()

    forward_sigterm = lambda signum, frame: child.terminate()  # `kill <this pid>` (unlike Ctrl+C) targets only us, not the child, so it needs explicit forwarding
    previous_handler = signal.signal(signal.SIGTERM, forward_sigterm)
    try:
        returncode = _wait_ignoring_sigint(child)
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        shutil.rmtree(handle.path)
    return returncode


def _wait_ignoring_sigint(child: subprocess.Popen) -> int:
    """Blocks until child exits, retrying through KeyboardInterrupt -- Ctrl+C at the terminal
    delivers SIGINT to the whole foreground process group (us and the child both, since the child
    was spawned without its own process group), so the child is already handling/exiting on its
    own; we just need to keep waiting for it, not treat our own interrupted wait() as a reason to
    bail early and skip cleanup."""
    while True:
        try:
            return child.wait()
        except KeyboardInterrupt:
            continue


def _exec_env() -> dict[str, str]:
    """Copy of the environment with a PyInstaller-frozen octo's bundled LD_LIBRARY_PATH undone, so
    the spawned agent binary -- and anything it shells out to, e.g. /bin/sh for a shell tool call --
    links against the system's real shared libraries instead of the ones bundled into octo's own
    frozen binary. PyInstaller's bootloader saves the pre-bundle value (if any) in
    LD_LIBRARY_PATH_ORIG specifically so a frozen app's own child processes can restore it; under a
    plain (non-frozen) interpreter neither var is set, so this is a no-op copy of os.environ."""
    env = dict(os.environ)
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if original is not None:
        env["LD_LIBRARY_PATH"] = original
    else:
        env.pop("LD_LIBRARY_PATH", None)
    return env


@dataclass
class AgentSession:
    handle: WorktreeHandle
    session_name: str


def _ensure_tmux_config() -> Path:
    config_dir = Path.home() / ".octo"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "octo.tmux.conf"
    config_content = (
        "set-option -g prefix C-o\n"
        "unbind-key C-b\n"
        "bind-key C-o send-prefix\n"
        "bind-key q detach-client\n"
        "set-option -g status on\n"
        "set-option -g status-style 'bg=default,fg=yellow'\n"
        "set-option -g status-left '[Press Ctrl+o q to detach]'\n"
        "set-option -g status-left-length 40\n"
        "set-option -g status-right ''\n"
        "set-window-option -g window-status-current-format ''\n"
        "set-window-option -g window-status-format ''\n"
    )
    if not config_path.exists() or config_path.read_text(encoding="utf-8") != config_content:
        config_path.write_text(config_content, encoding="utf-8")
    return config_path


def list_live_tmux_sessions() -> set[str]:
    """Returns a set of active tmux session names on octo's dedicated server."""
    if not shutil.which("tmux"):
        return set()
    try:
        result = subprocess.run(
            ["tmux", "-L", "octo", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, check=False
        )
        if result.returncode == 0:
            return {line.strip() for line in result.stdout.splitlines() if line.strip()}
    except Exception:
        pass
    return set()


def launch_agent_session(root: Path, agent: str, subpath: str | None = None) -> AgentSession:
    """Launches a new agent worktree session detached inside a tmux session on octo's dedicated server."""
    if not shutil.which("tmux"):
        raise RuntimeError("tmux is not installed or not found on PATH. Background sessions require tmux.")
    
    agent_name, binary_path = resolve_single_agent(agent)
    if agent_name is None:
        raise ValueError(f"Unknown agent: {agent}")
    if binary_path is None:
        raise RuntimeError(f"Agent binary {agent_name} not found on PATH.")

    session = RunningSession(root=root, pid=os.getpid())
    handle = _redirect_into_worktree(session, agent_name, subpath=subpath)
    
    # tmux session names cannot contain . or :
    safe_agent = agent_name.replace(".", "-").replace(":", "-")
    safe_tag = handle.tag.replace(".", "-").replace(":", "-")
    session_name = f"octo-{safe_agent}-{safe_tag}"
    
    tmux_config = _ensure_tmux_config()
    env = _exec_env()
    
    # Spawn detached tmux session
    cmd = [
        "tmux", "-L", "octo", "-f", str(tmux_config),
        "new-session", "-d", "-s", session_name, "-c", str(handle.agent_cwd),
        "--", str(binary_path)
    ]
    subprocess.run(cmd, env=env, check=True)
    
    return AgentSession(handle, session_name)


if __name__ == "__main__":
    main(sys.argv[1:])
