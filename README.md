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

### Entry point

[`pythonPrototype/octo.py`](pythonPrototype/octo.py) is the entry point:

```
python3 pythonPrototype/octo.py [root] [--cwd CWD]
```

- `root` — directory to watch for edits (defaults to the current directory)
- `--cwd` — agent working directory whose sessions to correlate against (defaults to `root`)

### Building a standalone binary

The prototype ships a PyInstaller spec (`pythonPrototype/octo.spec`) that bundles
`octo.py` into a single executable:

```
cd pythonPrototype
pip install textual pygments pyinstaller
pyinstaller octo.spec
```

The resulting binary is written to `pythonPrototype/dist/octo`.
