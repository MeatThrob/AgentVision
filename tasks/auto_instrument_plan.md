# Universal Auto-Instrumentation — Plan

**Goal:** bridge any program once → AgentVision auto-installs all diagnostic hooks (stdout, stderr, exceptions, log lines, keypresses, mouse, file writes) → forever after, that program emits to AgentVision with **zero code changes**.

**Promise to the user:** "I have a tiny app with no logging. I run `agentvision attach <project>` once, then `agentvision run -- python myapp.py`. From that moment on, AgentVision sees everything."

---

## Architecture (3 layers)

### Layer 1 — Bootstrap dir (lives inside AgentVision, never modifies the user's project)
```
AgentVision/agent_bootstrap/
    sitecustomize.py    # auto-loaded by Python at interpreter startup
    av_runtime.py       # the actual hook implementations
    __init__.py
```
- The user's project is **never touched**. We never write into their repo.
- Bootstrap is loaded by setting `PYTHONPATH=<av_root>/agent_bootstrap:$PYTHONPATH` before launching their interpreter.

### Layer 2 — `agentvision` CLI (the wrapper)
Single command exposing four verbs:
- `agentvision attach <project_dir>` — register a project as bridged. Writes a one-time profile entry to AgentVision's profiles. Idempotent — running twice is a no-op.
- `agentvision run -- <command...>` — spawn a child process with PYTHONPATH+env injected. Tees stdout/stderr to both the user's terminal AND AgentVision's JSONL.
- `agentvision daemon start|stop|status` — the macOS keypress+mouse capture daemon (CGEventTap via pynput).
- `agentvision status` — show what's bridged + daemon state.

### Layer 3 — Per-program JSONL sink (already exists)
Every hook writes to `<project>/log/actions.jsonl` using the same schema we built. AgentVision's bridge_server.py picks it up via the existing `profile.action_log_file` field.

---

## What gets captured (universal across any Python program)

| Source | Mechanism | Hook point |
|--------|-----------|------------|
| Uncaught exceptions | `sys.excepthook = ...` | sitecustomize on import |
| Thread exceptions | `threading.excepthook = ...` | sitecustomize on import |
| Unraisable (during interpreter shutdown) | `sys.unraisablehook = ...` | sitecustomize on import |
| All `logging` calls | `logging.root.addHandler(JSONLHandler())` | sitecustomize on import |
| stdout / stderr | `sys.stdout = TeeStream(sys.stdout, jsonl_sink)` | sitecustomize on import |
| File writes (within project) | `watchdog.Observer` on project root | started by wrapper, runs in daemon thread |
| Keypresses + mouse | `pynput.keyboard.Listener` + `pynput.mouse.Listener` | `agentvision daemon` (separate process, system-wide) |
| Process start/exit | `atexit.register(...)` + wrapper Popen wait | sitecustomize + wrapper |

---

## What gets captured (any non-Python program — Node, Rust, Go binary)

The wrapper alone gives you:
- stdout / stderr (via `subprocess.Popen(stdout=PIPE, stderr=PIPE)` + tee threads)
- Process exit code
- Keypresses + mouse (from the always-on daemon)
- File writes inside project dir (from the always-on daemon if started, or short-lived watcher inside wrapper)

You lose: in-process exceptions, logging-module integration. That's a fundamental constraint of "no code changes" — we get what the OS gives us.

---

## One-time setup the user does

```
cd <AGENTVISION_ROOT>
python -m python_backend.cli install     # writes ~/bin/agentvision (or symlink)
agentvision daemon start                 # prompts for Accessibility once
agentvision attach ~/myproject
agentvision run -- python myproject/app.py
# … from now on, everything streams to AgentVision JSONL …
```

The Accessibility prompt for the daemon is the **only** one-time interactive step. After that, every future run with `agentvision run` is silent.

---

## File layout

```
AgentVision/
  agent_bootstrap/
    __init__.py
    sitecustomize.py           # NEW — picked up automatically when on PYTHONPATH
    av_runtime.py              # NEW — install_all_hooks(), TeeStream, JSONLHandler
  python_backend/
    cli.py                     # NEW — argparse-based agentvision command
    daemon/
      __init__.py
      input_daemon.py          # NEW — pynput keyboard+mouse → JSONL
    connectors/
      program_connector.py     # ALREADY — adds auto_attach_project() helper
```

---

## Idempotency guarantees

The user's instinct: "do this once, stay forever." Implementation:

1. **`agentvision attach`** writes a marker file `<project>/.agentvision_attached` AND adds a profile entry to AgentVision. If both exist already, it's a no-op. Running 5 times = same as running once.
2. **`sitecustomize.py`** sets `os.environ["AGENTVISION_HOOKED"] = "1"` first thing, and bails if already set — so even if PYTHONPATH gets duplicated, hooks install exactly once per process.
3. **JSONL sink** opens with append mode + line buffering; multiple processes can safely write concurrently because each line is a complete record (atomic write up to PIPE_BUF on POSIX).
4. **Daemon** uses a PID file at `/tmp/agentvision_daemon.pid`. Second `daemon start` exits cleanly if a live PID is found.

---

## What NOT to do (footguns avoided)

- **No PYTHONSTARTUP** — only fires for interactive REPL, useless for scripts.
- **No DYLD_INSERT_LIBRARIES** — blocked by macOS hardened runtime in many Python builds.
- **No editing the user's project files** — ever. The whole point is "the program stays simple."
- **No global Python site-packages install** — `pipx`-style isolation; bootstrap dir lives inside AgentVision's repo.
- **No sudo** — Accessibility permission is the one user prompt, system-managed.
- **No daemon auto-start on system boot** — user controls daemon lifecycle explicitly.

---

## Phasing

| Phase | Component | Test |
|-------|-----------|------|
| 1 | `agent_bootstrap/sitecustomize.py` + `av_runtime.py` (in-process hooks) | `PYTHONPATH=... python -c "raise RuntimeError('x')"` produces JSONL records |
| 2 | `python_backend/cli.py` with `attach` + `run` verbs | `agentvision run -- python script.py` → stdout teed + JSONL has stdout records |
| 3 | `python_backend/daemon/input_daemon.py` (pynput keyboard+mouse) | running daemon emits JSONL on every key/mouse event |
| 4 | `daemon start|stop|status` subcommands + PID-file lifecycle | second `start` exits gracefully |
| 5 | watchdog file writer (in wrapper, not daemon) | edit a file in attached project → JSONL record |
| 6 | `agentvision install` to drop binary into `~/bin` (or just document `python -m`) | user can call `agentvision` from anywhere |

---

## Default fields on every auto-emitted record

Every record uses the existing `ActionLog` schema so AgentVision's bridge picks it up with zero changes:
```json
{
  "ts": "...Z", "ts_ms": 1234567890.0,
  "category": "stdout|stderr|exception|log|key|mouse|file|process",
  "source": "av.bootstrap.<hook>",
  "state": null, "run_id": null, "trace_id": null, "frame_seq": null,
  "coords": null,
  "data": { ... category-specific payload ... }
}
```

The `frame_seq` field is read from `<project>/log/.av_frame_seq` the same way an instrumented program's action log does — same correlation works automatically.

---

## Approval checkpoint

Before implementing, confirm:
- Project root for the bootstrap is `<AGENTVISION_ROOT>/agent_bootstrap/`
- CLI lives at `<AGENTVISION_ROOT>/python_backend/cli.py`
- We use `pynput` (already common, MIT-licensed, no compile step on macOS)
- `agentvision attach <project>` writes to AgentVision's existing profile system

If yes → proceed phase by phase.
