# AgentVision — Architecture & How It Actually Works

*(SETUP.md covers install steps; this file explains the internals so anyone — human or AI — fully understands what AgentVision is, how the pieces connect, and how an AI agent uses it. Written 2026-07-11.)*

## What AgentVision IS (one paragraph)

AgentVision is a **debugging bridge that gives an AI agent (Claude) eyes and instrumentation on a running program it otherwise cannot see** — especially GUI programs that can't be run headlessly. It captures the target program's **screenshots (frames), logs, structured events, source tree, and errors**, stores them, and exposes them to Claude as native MCP tools. Claude can then observe the program in real time, correlate a visual frame with the log lines and actions around that moment, and debug behavior it could never inspect from a terminal alone.

## The 3-layer model

```
  TARGET PROGRAM (e.g. SharpEmu.app — a .NET/Avalonia GUI)
        │  observed via: window screenshots (by app name) + its log/JSONL files
        ▼
  BRIDGE SERVER  (python_backend/api/bridge_server.py)   ← Flask, http://127.0.0.1:7771
        │  captures frames on an interval, tails logs, parses events, stores frames+index
        │  exposes ~40 HTTP routes: /latest, /frame/<seq>, /log/range, /program/log, ...
        ▼
  MCP SERVER  (python_backend/api/claude_mcp.py)          ← stdio MCP, proxies to :7771
        │  wraps each bridge route as a native Claude tool (av_latest_frame, av_program_log…)
        ▼
  CLAUDE  (Claude Code CLI, or any MCP-aware client)      ← calls av_* tools, sees the program
```

**Key fact:** the MCP server does NOT run the bridge — the bridge (`bridge_server.py`) must already be running (Start Bridge in the GUI, or run it directly). The MCP layer is a thin proxy that turns HTTP routes into agent tools.

## How it attaches to a target program (the connector)

`python_backend/connectors/program_connector.py` defines a **ProgramProfile** — the config that tells AgentVision what to watch:
- `capture_app` — the **app/window name to screenshot** (e.g. `"SharpEmu"`, `"chiaki-ng"`). This is how AgentVision watches NON-Python GUIs: it screenshots that window on the OS. **This is why SharpEmu (.NET) works even though AgentVision is Python — it captures the window, it does not need to inject into .NET.**
- `log_file` / `actions_file` — the program's text log and JSONL event log, which the bridge tails.
- `screenshots_folder`, `project_root`, `process_name`.

Two attach modes:
1. **Window-capture mode (works for ANY program, incl. .NET/C++/GUI):** set `capture_app` to the window title; the bridge screenshots it on an interval. No code injection. **This is the mode for SharpEmu.**
2. **Python-injection mode (Python targets only):** `agent_bootstrap/sitecustomize.py` auto-loads into a Python target and `av_runtime.py` instruments it for richer events. Not applicable to .NET.

## The frame↔log↔action correlation (the core value)

Every screenshot ("frame") gets a sequence number and timestamp. The bridge indexes them so Claude can ask:
- `av_latest_frame()` — newest screenshot (what's on screen NOW)
- `av_get_frame(seq)` — a specific past frame
- `av_actions_around_frame(seq, window_secs)` — what the program logged/did in the ±N seconds around that frame
- `av_log_range(from_ms, to_ms)` — logs in a time window
- `av_state_at(at_ms)` — reconstructed program state at a moment

So Claude sees a visual glitch in a frame, then pulls the exact log lines from that instant. That correlation is the whole point.

**Alignment guarantee (hardened 2026-07-20):** the frame timestamp and the log byte-offsets are snapshotted in the *same instant, immediately before the shutter* — never after — so a log record can never be bounded into a frame with a `ts_ms` past that frame's own shutter time. Each frame carries a `capture_meta` block (`shutter_ms`, `capture_end_ms`, `capture_latency_ms`, offsets, `window_found`, `black_frame`), and `av_frame_alignment(seq)` re-proves it on demand.

**Any language, any log:** `connectors/log_adapters.py` auto-detects a log's format (23 adapters: JSON-lines, Log4j/SLF4J, Rust, .NET Serilog/MEL, SharpEmu bracketed, syslog, systemd, logcat, klog, Go zap, Ruby, PHP Monolog, Elixir, nginx/Apache access+error, Postgres/MySQL, compiler diagnostics, test runners, logfmt, Python logging, generic-timestamped, raw) and normalizes every line into the one unified event schema; `connectors/log_sources.py` merges N heterogeneous logs per profile onto the single time-aligned timeline (`av_log_normalized`, `av_log_sources`). `agentvision run -- <cmd>` normalizes a child's stdout/stderr through the same adapters (any language, zero config). No-timezone timestamps are read as local machine time so a program's log and its epoch-ms event stream align on the same box. Flagship worked example: the built-in `sharpemu` profile (both its text log and native JSONL event stream merged + window capture).

**Screenshot rate:** interval is seconds/shot (0.1 s = 10 fps, fastest, and up). The rate envelope + a "ask the user their preferred shots/sec" guidance string are surfaced in `av_status` / `av_capture_status` / `av_overview` and baked into the capture-tool docstrings.

**AI-facing JSON depth (v2 / `schema_version` 2.0.0):** every frame carries an AI triage layer — `summary`, `recommended_next`, `tags`, `confidence` — plus a STRUCTURED `error` (multi-language exception parse → `exception_type`, `frames[{file,line,func}]`, `probable_cause`, `fingerprint`, `occurrence_count`, `first/last_seen`), a `state_delta` (key-level change vs the previous frame), `correlation` (trace/run ids), and a richer `anomaly` (`confidence`, `evidence`). `av_digest` is the compact, ranked triage entry point. All fields are documented in `docs/SCHEMA.md`; the intelligence lives in `python_backend/modules/diagnostics.py`. The bridge returns uniform JSON on every error (no HTML pages). Test suites: `run_all_tests.py` (schema, diagnostics, log adapters, langdetect, bridge routes).

## The full MCP tool surface (44 tools, grouped)

**Observe the live program**
- `av_status`, `av_overview`, `av_program_status`, `av_capture_status`, `av_daemon_status`
- `av_latest_frame`, `av_get_frame(seq)`, `av_program_crop`
- `av_program_log(lines)`, `av_debug_log(lines)`, `av_program_stats(lines)`

**Correlate frame ↔ time ↔ action**
- `av_actions_around_frame(seq, window_secs)`, `av_log_range(from_ms, to_ms)`, `av_state_at(at_ms)`

**Errors & anomalies**
- `av_errors_by_fingerprint(fp)`, `av_new_errors_this_session`, `av_bookmark_outliers`, `av_events_schema`, `av_trace_timeline(trace_id)`

**Frame annotation/overlay** (mark up a screenshot to point at something)
- `av_frame_overlay(seq)`, `av_frame_annotate(seq)`, `av_frame_annotations(seq)`

**Bookmarks** (name a moment to return to)
- `av_list_bookmarks`, `av_get_bookmark(id)`

**Source access** (the bridge mirrors the target's source so Claude can read it via the same channel)
- `av_source_tree`, `av_source_file(path)`, `av_source_search(q)`, `av_source_digest(prefix)`, `av_source_list`, `av_source_light`, `av_source_refresh`, `av_codebase_map`

**Control the capture**
- `av_capture_start(interval)`, `av_capture_stop`, `av_capture_set_interval(interval)`
- `av_log_push(message)`, `av_run_tests`

**Profiles** (which program is being watched)
- `av_list_profiles`, `av_active_profile`, `av_set_active_profile(name)`, `av_create_profile(name)`, `av_delete_profile(name)`, `av_install_project(project_root)`, `av_install_verify(project_root)`

## How Claude connects (the exact wiring)

Add to Claude Code `settings.json`:
```json
{
  "mcpServers": {
    "agentvision": {
      "command": "python3",
      "args": ["-m", "python_backend.api.claude_mcp"],
      "cwd": "<AGENTVISION_ROOT>"
    }
  }
}
```
Then, with the bridge running on :7771, the `av_*` tools appear in Claude's session. Claude calls `av_status` first to confirm the bridge is live, then `av_latest_frame` / `av_program_log` to observe.

`AGENTVISION_BRIDGE_URL` env var overrides the bridge address (default `http://127.0.0.1:7771`).

## Startup order (must be this order)

1. Start the bridge: GUI → **Start Bridge**, or `cd AgentVision && python3 python_backend/api/bridge_server.py` (listens on :7771).
2. Set the active profile to the target program (its `capture_app` = the window to watch).
3. Start capture (GUI → Start, or `av_capture_start`).
4. In Claude's client, the `agentvision` MCP server (from settings.json) exposes the `av_*` tools.
5. Claude calls `av_status` → `av_latest_frame` → observes.

## Directory map

```
AgentVision/
├── python_backend/
│   ├── api/bridge_server.py     ← the :7771 Flask capture server (RUN THIS to bridge)
│   ├── api/claude_mcp.py        ← stdio MCP proxy → exposes av_* tools to Claude
│   ├── connectors/program_connector.py  ← ProgramProfile: what/how to watch
│   ├── gui/agent_vision_gui.py  ← tkinter control panel (Start Bridge, profiles, capture)
│   ├── modules/                 ← execution_trace, anomaly_detector, error_enricher,
│   │                              state_snapshot, ui_recorder, performance_profiler, …
│   ├── utils/                   ← overlay_renderer (frame markup), checkpoint_manager
│   └── source_mirror.py         ← mirrors target source for av_source_* tools
├── agent_bootstrap/             ← Python-only injection (sitecustomize.py + av_runtime.py)
├── snapshots/ checkpoints/ shared/ tasks/
├── SETUP.md                     ← install + quick start
└── ARCHITECTURE.md              ← this file
```
