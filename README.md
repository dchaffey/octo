# octo
A tool for tracking the work of your agents in your projects.

![octo screenshot](docs/images/screenshot.svg)

## Status: prototype

The current implementation is a prototype and lives entirely in [`pythonPrototype/`](pythonPrototype/).
It watches a directory for file changes and attributes each one to the Claude Code, Antigravity,
or Codex CLI session/prompt that produced it, committing every edit to a shadow git repo (`.octo`)
so you get diffing, history, and revert for free.

The end goal is to rewrite this in a systems language (Zig, C, or C++) for a faster, dependency-free
binary; the Python version exists to validate the design first.

### How it works

octo watches two things at once and joins them together: your project's files, and the session
transcripts each agent CLI already keeps on disk. Every write to disk is committed immediately
under a "Human" author — octo never delays a commit waiting to find out who made it. Once an
agent's own transcript later logs that exact content, the commit is relabeled to that agent via
a git-notes annotation. Git itself is the storage and history engine throughout: no separate
database, diff engine, or undo log — `.octo` is just a real (shadow) git repository, so diffing,
history, and revert all fall out of plain git for free.

```mermaid
flowchart LR
    subgraph Inputs
        FS["Watched directory<br/>(your project files)"]
        SESS["Agent session transcripts<br/>~/.claude/projects, ~/.codex/sessions,<br/>~/.gemini/antigravity-cli"]
    end

    FS -- "file changed" --> COMMIT["Commit as 'Human'<br/>(immediately, no waiting)"]
    COMMIT --> SHADOW[(".octo shadow git repo")]
    SESS -- "transcript later confirms<br/>which agent wrote it" --> ATTR["Relabel commit to agent<br/>(git-notes annotation)"]
    ATTR --> SHADOW
    SHADOW --> TUI["Textual UI<br/>diff / history / revert"]
```

Because everything lands in a real git repo, revert is just `git revert`/checkout against `.octo`,
and history is just `git log` — octo's own code only has to watch, correlate, and render.

### Entry point

[`pythonPrototype/octo.py`](pythonPrototype/octo.py) is the entry point:

```
python3 pythonPrototype/octo.py [root] [--cwd CWD]
```

- `root` — directory to watch for edits (defaults to the current directory)
- `--cwd` — agent working directory whose sessions to correlate against (defaults to `root`)

To launch an agent CLI in its own isolated git worktree (so its tool use never touches the real
project directory directly), run it through octo instead of invoking it directly:

```
python3 pythonPrototype/octo.py run <agent> [agent-args...]
```

- `agent` — `claude`, `codex`, or `agy` (or their display names — `Claude`, `Codex`, `Antigravity` — matched case-insensitively)
- `agent-args` — forwarded untouched to the real agent CLI
- requires a running `octo [root]` watching the current directory (or a parent/child of it); creates a fresh worktree and branch for this invocation and execs the real agent CLI there

### Building a standalone binary

The prototype ships a PyInstaller spec (`pythonPrototype/octo.spec`) that bundles
`octo.py` into a single executable:

```
cd pythonPrototype
pip install textual pygments pyinstaller
pyinstaller octo.spec
```

The resulting binary is written to `pythonPrototype/dist/octo`.
