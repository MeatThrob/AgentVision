# AgentVision v5 — What It Is & How It Works

> **New here?** Start with the definitive project overview: [`docs/WHAT_IS_AGENTVISION.md`](docs/WHAT_IS_AGENTVISION.md) — what AgentVision is, the universal JSON debug log, and deep dives on the screenshot engine and screenshot↔log time-alignment.

> A debugging bridge that gives an AI agent (Claude) **eyes and instrumentation
> on a running program it otherwise cannot see** — especially GUI programs that
> can't be run headlessly.
>
> This is **AgentVision v5**, the master build. It is an expansion of the
> original AgentVision: a universal, any-language log framework (**656 named
> log-format adapters + a structural normalizer + binary source-readers**, all
> normalized onto one time-aligned JSON timeline), time-aligned screenshots with
> AI-facing capture-rate guidance, and a cross-platform core. **Runs on macOS,
> Windows 10/11, and Linux (incl. systemd-free Artix)** from one source tree —
> all OS-specific code is isolated behind `python_backend/utils/platform_shim.py`.
> Setup: `SETUP.md` (macOS), `SETUP-Windows.md` (Windows), `dist/linux/README.md`
> (Linux). Full overview: `docs/WHAT_IS_AGENTVISION.md`.

---

## 1. What is AgentVision *for*?

When you ask an AI coding assistant to debug a program, it can read your source
code and your log files — but it is **blind to the running program**. It can't
see the window, can't see a visual glitch, can't tell that the app froze on
frame 4,000, and can't line up "the screen looked wrong *here*" with "the log
said *this* at that exact millisecond."

AgentVision fixes that. It:

1. **Captures the target program's screen** (whole desktop, a fixed region, or
   one specific window) on a timed interval — each screenshot is a **frame**
   with a sequence number and a UTC timestamp.
2. **Tails the program's text log and structured JSONL event log**, stamping
   every line onto the same timeline as the frames.
3. **Mirrors the program's source tree** so the agent can read the code through
   the same channel.
4. **Records anomalies, errors, and (optionally) real keyboard/mouse input.**
5. **Exposes all of that to Claude as native MCP tools** (`av_latest_frame`,
   `av_program_log`, `av_actions_around_frame`, …).

The payoff is **correlation**: Claude sees a bad frame, then pulls the exact
log lines and actions from that instant. That's the whole point — and it works
for **any** program, including compiled ones (C++, .NET, a game, an emulator),
because AgentVision watches the *window and the log files* rather than injecting
into the process.

**Typical uses**
- Debugging a GUI app or game that has no headless mode.
- Watching a long-running bot / automation and catching the moment it breaks.
- Giving Claude "situational awareness" of a program during a live debugging
  session, so it reasons about what's actually on screen, not just the code.

---

## 2. The 3-layer model

```
  TARGET PROGRAM  (any Windows app: a .NET/WPF GUI, a game, a Python bot, …)
        │   observed via: window/region/full-screen screenshots
        │                 + its own log.txt / actions.jsonl (tailed)
        ▼
  BRIDGE SERVER   (python_backend/api/bridge_server.py)   ← Flask, 127.0.0.1:7771
        │   captures frames on an interval, tails logs, parses events,
        │   stores frames + a time index; exposes ~40 HTTP routes
        ▼
  MCP SERVER      (python_backend/api/claude_mcp.py)      ← stdio MCP, proxies :7771
        │   wraps each bridge route as a native Claude tool
        ▼
  CLAUDE          (Claude Code CLI, or any MCP-aware client)
        │   calls av_* tools → sees the program in real time
```

**Key fact:** the MCP server does **not** run the bridge. The bridge
(`bridge_server.py`) must already be running — start it from the GUI
(**Start Bridge**) or with `Start Bridge (headless).bat`. The MCP layer is a
thin proxy that turns HTTP routes into agent tools.

You do **not** need Claude/MCP to use AgentVision — the GUI alone lets you watch
frames and logs. MCP is what turns it into an agent's eyes.

---

## 3. How it attaches to a target program (profiles)

A **ProgramProfile** (stored in `python_backend/profiles.json`) tells
AgentVision what to watch:

| Field                | Meaning |
|----------------------|---------|
| `capture_app`        | Window title / process name to screenshot (e.g. `"Notepad"`, `"SharpEmu"`). This is how AgentVision watches a non-Python GUI: it finds and captures that window — **no code injection**. |
| `capture_crop`       | A fixed screen rectangle `"x,y,w,h"` to capture instead (manual override). |
| `log_file`           | The program's text log — the bridge tails it. |
| `action_log_file`    | The program's structured JSONL event log — one JSON record per line. |
| `project_root`       | Where frames/data are saved and where source is mirrored from. |
| `process_name`       | Used to detect whether the program is running (auto-pause capture when it isn't). |
| `capture_user_input` | Opt-in: also record the human's physical keyboard/mouse (default off). |

**Two attach modes**

1. **Window / region capture (works for ANY program).** Set `capture_app` (or a
   `capture_crop`); the bridge screenshots it on an interval. This is the mode
   for compiled GUIs and games.
2. **Universal auto-bridge (ANY language).** On first attach AgentVision installs
   the OUTPUT side into the project — see below.

---

## 3b. The two-sided bridge & universal auto-install ("the most important part")

AgentVision is a **two-sided bridge**:

- **INPUT side** (built into AgentVision): it *reads* logs — the 41-adapter
  registry + multi-source merge + the MCP tools. See `docs/LOG_ADAPTERS.md`.
- **OUTPUT side** (auto-installed on first attach, for ANY language): it makes
  the target program *write* those logs. `agentvision attach <dir>` (or the
  `av_install_project` MCP tool / `/install` route) **detects the language** and
  scaffolds a self-contained **`agentvision/` folder inside the project**:

  ```
  <project>/agentvision/
    actions.jsonl     ← structured event sink (the unified schema)
    log.txt           ← plain-text sink
    state.json        ← live program state
    manifest.json     ← attach marker + language + emitter wiring
    emitters/         ← the per-language OUTPUT EMITTER (see below)
    stats/  crashes/  README.md
  ```

  and wires zero-effort logging per language (`python_backend/emitters.py`):

  | Language | Mechanism | How it loads |
  |---|---|---|
  | Python | in-process hooks | `agentvision run -- python …` (guaranteed); project `sitecustomize.py` autoload as a bonus |
  | Node.js | `av_emit.js` preload | `agentvision run` sets `NODE_OPTIONS=--require …` |
  | Ruby | `av_emit.rb` preload | `agentvision run` sets `RUBYOPT=-r …` |
  | Java | logback JSON appender | reference the config, or `agentvision run` tee |
  | .NET | Serilog JSON file sink | load the config, or `agentvision run` tee |
  | Go / Rust / C++ / shell / other | **stdout/stderr tee** (normalized via the adapters) | `agentvision run -- <cmd>` — zero effort |

  **`agentvision run -- <cmd>` is the universal front door:** it injects the
  emitter env for in-process languages AND tees+normalizes stdout/stderr for
  everything, so one command bridges any program with zero code changes. The
  emitter writes the SAME unified JSON schema the INPUT side reads, and the
  installer points the profile's `log_sources` at the scaffolded sinks. Verify
  the round-trip with `av_install_verify` (spawns a probe for python/node/ruby;
  static check for config/tee languages).

---

## 4. The frame ↔ log ↔ action correlation (the core value)

Every screenshot ("frame") gets a sequence number and a UTC-ms timestamp,
captured from the same clock (`shared/clock.py`) as everything else. The bridge
indexes them so Claude can ask:

- `av_latest_frame()` — newest screenshot (what's on screen NOW)
- `av_get_frame(seq)` — a specific past frame
- `av_actions_around_frame(seq, window_secs)` — what the program logged/did in
  the ±N seconds around that frame
- `av_log_range(from_ms, to_ms)` — logs in a time window
- `av_state_at(at_ms)` — reconstructed program state at a moment

The timestamp **and** the log byte-offsets are snapshotted **together, in the
same instant, immediately before the shutter**, so no line that arrived *during
or after* the shutter sneaks into that frame's context (a record can never carry
a `ts_ms` greater than the frame's own timestamp). Every frame's `capture_meta`
records `shutter_ms`, `capture_end_ms`, `capture_latency_ms`, the byte offsets,
whether the target window was found, and whether the grab looked blank/black.
`av_frame_alignment(seq)` re-verifies this on demand. This is what makes "the
glitch is on frame 4213, what happened right then?" answerable — and *provably*
aligned.

### Screenshot rate (shots per second)
Capture cadence is user-configurable: interval `0.1 s` (10 shots/sec, fastest)
and up. The rate envelope + a `guidance` string are surfaced in `av_status`,
`av_capture_status`, `av_overview`, and every capture-control response, and the
MCP tool docstrings instruct the model to **ask the user how many shots per
second they want at the start or continuation of every project** and present the
full range, then apply it with `av_capture_set_interval(interval = 1/fps)`.

### Any language, any log — normalized onto one timeline
A profile can list N `log_sources`, each in a **different format/language**, all
auto-detected (`python_backend/connectors/log_adapters.py`), normalized into the
one unified JSON event schema, and merged onto the single time-aligned timeline
(`av_log_normalized`). **41 adapters ship today:** JSON-lines (pino/winston/
bunyan/zap-json/structlog/AV-native), Java Log4j/SLF4J, Rust `env_logger`/
`tracing`, .NET Serilog+MEL, **SharpEmu** (`[HH:mm:ss.fff] [LEVEL] [Cat] file:line
msg`), syslog RFC5424/3164, systemd journal, Android logcat, k8s klog/glog, Go
zap, Ruby Logger, PHP Monolog, Elixir Logger, nginx/Apache access (CLF), nginx
error, Apache error, Postgres/MySQL, compiler diagnostics (gcc/clang/rustc/MSVC),
test runners (pytest/go/cargo), logfmt, Python `logging`, generic-timestamped,
and raw. `agentvision attach` sniffs a project's language and pre-fills likely
log sources with zero manual setup. Add a format by subclassing `LogAdapter` and
registering it — see the ROADMAP at the bottom of `log_adapters.py`.

**Console tee (any language, zero config):** `agentvision run -- <cmd>` launches
any program and normalizes its stdout/stderr through the SAME adapters — so a
compiled Java/Go/Rust program's console ERROR line becomes `category:"error"`
and shows up in bookmarks/failure-detection, while the raw text is preserved in
`data.text`.

**Timezone policy:** a log timestamp with an explicit `Z`/offset is honored as
written; a bare timestamp with no zone (e.g. SharpEmu's `HH:mm:ss.fff`) is read
as **local machine time**, because AgentVision correlates a program's log with
frames stamped on the *same machine* — so the text log and an epoch-ms event
stream line up exactly even when the box isn't on UTC.

---

## 5. The MCP tool surface (≈44 tools, grouped)

**Observe the live program**
`av_status`, `av_overview`, `av_program_status`, `av_capture_status`,
`av_daemon_status`, `av_latest_frame`, `av_get_frame(seq)`, `av_program_crop`,
`av_program_log(lines)`, `av_debug_log(lines)`, `av_program_stats(lines)`

**Triage first (read this before anything else)**
`av_digest` — one compact, ranked JSON: what's broken, what's new, capture +
alignment health, and the latest frame's `summary`/`recommended_next`/`tags`/
`confidence`. Each `attention` item names the tool to drill in with.

**Correlate frame ↔ time ↔ action**
`av_actions_around_frame(seq, window_secs)`, `av_log_range(from_ms, to_ms)`,
`av_state_at(at_ms)`, `av_frame_alignment(seq)` (prove image↔log alignment is exact)

**Universal multi-language logs** (any language, any format, merged on one timeline)
`av_log_sources` (what's watched + each log's auto-detected format),
`av_log_normalized(from_ms, to_ms, level, label)` (all logs normalized + merged)

**Errors & anomalies**
`av_errors_by_fingerprint(fp)`, `av_new_errors_this_session`,
`av_bookmark_outliers`, `av_events_schema`, `av_trace_timeline(trace_id)`

**Frame annotation / overlay** (mark up a screenshot to point at something)
`av_frame_overlay(seq)`, `av_frame_annotate(seq)`, `av_frame_annotations(seq)`

**Bookmarks** (name a moment to return to)
`av_list_bookmarks`, `av_get_bookmark(id)`

**Source access** (the bridge mirrors the target's source)
`av_source_tree`, `av_source_file(path)`, `av_source_search(q)`,
`av_source_digest(prefix)`, `av_source_list`, `av_source_light`,
`av_source_refresh`, `av_codebase_map`

**Control the capture** (interval is seconds/shot; `fps = 1/interval`)
`av_capture_start(interval)`, `av_capture_stop`,
`av_capture_set_interval(interval)`, `av_log_push(message)`, `av_run_tests`
— each reports the supported shots-per-second range and reminds the model to
ask the user their preferred rate.

**Profiles**
`av_list_profiles`, `av_active_profile`, `av_set_active_profile(name)`,
`av_create_profile(name)`, `av_delete_profile(name)`,
`av_install_project(project_root)`, `av_install_verify(project_root)`

---

## 6. Connecting Claude (the exact wiring)

With the bridge running on `:7771`, install the optional `mcp` package and
register the server. `claude_mcp.py` imports only the standard library plus
`mcp`, so point Claude Code directly at the script file (no `cwd` needed):

```bat
py -3 -m pip install "mcp>=2.0,<3"
claude mcp add agentvision -- python C:\Users\<you>\AgentVision\python_backend\api\claude_mcp.py
```

The `av_*` tools then appear in Claude's session. Claude typically calls
`av_status` first (confirm the bridge is live), then `av_latest_frame` /
`av_program_log` to observe. `AGENTVISION_BRIDGE_URL` overrides the bridge
address (default `http://127.0.0.1:7771`).

---

## 7. Startup order (must be this order)

1. **Start the bridge** — GUI → **Start Bridge**, or `Start Bridge (headless).bat`
   (listens on `:7771`).
2. **Set the active profile** to the target program (its `capture_app` = the
   window to watch).
3. **Start capture** — GUI → **Start**, or the MCP tool `av_capture_start`.
4. In Claude's client, the `agentvision` MCP server exposes the `av_*` tools.
5. Claude calls `av_status` → `av_latest_frame` → observes.

---

## 8. The input recorder (optional)

A system-wide daemon can record keyboard/mouse events into the active profile's
`actions.jsonl`, so Claude can see *what the program did*, not just what it drew.

It distinguishes **synthetic** input (posted by a bot / automation) from
**physical** input (your real keyboard and mouse):

- **Synthetic events are always recorded** — that's usually the bot you're
  debugging.
- **Physical events are filtered out by default** so your own typing doesn't
  pollute the diagnostics. Flip `capture_user_input` on (Permissions tab) only
  when *you* are the agent (manual play, RPA recording, demo capture).

**How the distinction is made per-OS:**
- **Windows** — global low-level hooks (`WH_KEYBOARD_LL` / `WH_MOUSE_LL`). Each
  event carries an *injected* flag (`LLKHF_INJECTED` / `LLMHF_INJECTED`).
  Injected ⇒ synthetic; not injected ⇒ physical hardware.
- **macOS** — a `CGEventTap`; `kCGEventSourceUnixProcessID == 0` ⇒ physical.

Windows needs **no special permission** for this — global hooks work for a
normal user account. (macOS requires a one-time Accessibility grant.)

**Self-confirming on Windows.** When the daemon installs the hooks it runs a
STARTUP SELF-TEST — it synthesizes a harmless `VK_F24` via `SendInput` and
confirms the low-level hook callback observed it, emitting a
`{"check":"win_input_hooks","ok":…}` health record. A liveness watchdog re-runs
that probe periodically and re-installs the hooks if Windows ever dropped them
(LowLevelHooksTimeout). The whole Win32 control flow is proven by a deterministic
mocked-Win32 test (`daemon/test_win_input_sim.py`) that runs on any OS.

---

## 8b. Zero-step bridging + self-confirmation (`run` / `doctor`)

- **`agentvision run -- <cmd>`** is the one-command, zero-step bridge on every
  OS: it auto-scaffolds the project's `agentvision/` folder on first run,
  injects the per-language emitter env (Windows-correct — `NODE_OPTIONS` paths
  are double-quoted for spaces; `RUBYOPT` uses the 8.3 short path since Ruby
  can't quote), and tees+normalizes stdout/stderr. Add `--record-input` (or
  `AGENTVISION_RECORD_INPUT=1`) to also auto-start the input daemon **detached**
  (`CREATE_NEW_PROCESS_GROUP|DETACHED_PROCESS`) with no manual step.
- **`agentvision doctor`** (and the `av_selftest` MCP tool / `/selftest` route)
  RUN ON THE TARGET MACHINE and return a JSON health report: capture is
  non-blank, window enumeration works, the OS input hooks FIRE (Windows spawns
  the `SendInput` probe), and the emitter auto-injection round-trips (a tiny
  child's output is confirmed to land in the scaffolded sink). On Windows this
  is the definitive "confirmed working" proof at first run. Example:

  ```json
  {"ok": true, "os": "win32", "failed_checks": [], "checks": [
    {"check": "capture", "ok": true, "detail": "captured a non-blank region"},
    {"check": "window_enum", "ok": true, "visible_windows": 37},
    {"check": "win_input_hooks", "ok": true, "sent_inputs": 2, "fired": true,
     "detail": "hook callback observed the SendInput probe"},
    {"check": "emitter_roundtrip", "ok": true,
     "detail": "child output round-tripped into the scaffolded sink"},
    {"check": "input_daemon", "ok": true, "running": true, "pid": 4812}]}
  ```

---

## 9. What changed for the Windows port

The original AgentVision was macOS-only and reached the OS in five places. Every
one of those is now behind **`python_backend/utils/platform_shim.py`**, which
dispatches on the OS. The macOS behaviour is preserved exactly; Windows is the
new path.

| Concern | macOS (original) | Windows (this port) |
|---|---|---|
| **Screen capture** | `screencapture` CLI | [`mss`](https://pypi.org/project/mss/) region/full grab |
| **Window enumeration** | Quartz `CGWindowListCopyWindowInfo` | `win32gui.EnumWindows` (pywin32) |
| **Foreground app / window title** | AppleScript (`osascript`) | `win32gui.GetForegroundWindow` |
| **Global input tap** | Quartz `CGEventTap` | `WH_KEYBOARD_LL` / `WH_MOUSE_LL` via `ctypes` |
| **Process liveness** | `os.kill(pid, 0)` | `psutil.pid_exists` |
| **Detached child spawn** | `start_new_session=True` | `CREATE_NEW_PROCESS_GROUP` \| `DETACHED_PROCESS` |
| **Temp/pid/log paths** | hard-coded `/tmp` | `tempfile.gettempdir()` (`%TEMP%`) |
| **Reveal a file / open URL** | `open` / `open -R` | `explorer /select,` / `os.startfile` |
| **`agentvision` shim** | bash script | `agentvision.bat` |
| **Overlay stamp font** | Menlo/Monaco | Consolas/Courier New |
| **Window app-picker (GUI)** | AppleScript `choose application` | window-title chooser dialog |
| **Screen-recording permission** | required, with helper buttons | not required (buttons show an info note) |

There is **one** source tree for both OSes; nothing was forked.

### Note on capturing games with anti-cheat
Windows does not gate normal desktop capture, but some anti-cheat-protected
games block third-party screen capture of their window. For those, prefer
**windowed / borderless** mode and run AgentVision as the same user. Ordinary
apps, tools, emulators, and Python bots capture with no fuss.

---

## 10. Directory map

```
AgentVision/
├── Start AgentVision.bat          ← launch the GUI (Windows)
├── Start Bridge (headless).bat    ← run the bridge with no GUI
├── install-dependencies.bat       ← one-click pip install
├── requirements-windows.txt       ← Windows dependency list
├── SETUP-Windows.md               ← 5-minute quick start
├── HOW_IT_WORKS.md                ← this file
├── ARCHITECTURE.md                ← deeper internals (cross-platform)
├── python_backend/
│   ├── api/bridge_server.py       ← the :7771 Flask capture server
│   ├── api/claude_mcp.py          ← stdio MCP proxy → av_* tools for Claude
│   ├── connectors/program_connector.py  ← ProgramProfile: what/how to watch
│   ├── daemon/input_daemon.py     ← cross-platform key/mouse recorder
│   ├── gui/agent_vision_gui.py    ← Tkinter control panel
│   ├── modules/                   ← anomaly_detector, error_enricher, state_snapshot,
│   │                                execution_trace, performance_profiler, ui_recorder, …
│   ├── utils/
│   │   ├── platform_shim.py       ← ★ all OS-specific code lives here
│   │   └── overlay_renderer.py    ← burns the timestamp bar onto frames
│   └── source_mirror.py           ← mirrors target source for av_source_* tools
├── agent_bootstrap/               ← Python-only injection (sitecustomize + av_runtime)
├── shared/                        ← unified clock + snapshot schema
└── snapshots/  checkpoints/       ← where frames + data are stored
```

---

## 11. Requirements

- **Windows 10 or 11**
- **Python 3.11+** (`https://python.org/downloads` — tick *Add python.exe to PATH*)
- Python packages from `requirements-windows.txt` (installed by
  `install-dependencies.bat`): `flask`, `psutil`, `pillow`, `requests`,
  `mss`, `pywin32`, plus `pytest` for the test runner. Tkinter ships with the
  official Python installer — no separate install.

---

## 12. Troubleshooting

**GUI won't open / `ModuleNotFoundError`**
→ Run `install-dependencies.bat`. If Python isn't found, reinstall from
python.org with *Add to PATH* ticked.

**Frames are black / empty for a specific game**
→ That game's anti-cheat is likely blocking capture. Use windowed/borderless
mode; ordinary apps are unaffected.

**"Capture App" finds the wrong window**
→ Use **Select App** (Profile tab) to pick from a list of visible window titles,
or type an exact substring of the title / the process name (e.g. `notepad.exe`).

**Bridge won't start / port 7771 in use**
→ Find and stop the stray process:
`netstat -ano | findstr :7771` then `taskkill /PID <pid> /F`.

**Input recorder shows nothing**
→ Check the Permissions tab says *Recording*. Remember physical input is
filtered out by default — turn on `capture_user_input` for the active profile if
you want your own keyboard/mouse recorded.

**Claude doesn't see the `av_*` tools**
→ The bridge must be running first, and `cwd` in `settings.json` must point at
this folder. Confirm with `av_status`.

---

## Token economics — the cheap path (v5.1)

Screenshotting, perceptual hashing and diffing run on the user's CPU and are
effectively **free**. Every token the AI spends looking at raw pixels is not. So
AgentVision does the expensive observing up front and hands the agent the smallest
sufficient JSON.

**At capture time, for free:** every frame gets a two-axis perceptual hash (dHash),
a change score against the previous frame, and the bounding box of the region that
changed — sharing the image decode the blank-frame health check already performed,
so it costs ~1 ms per frame at 1080p against a 100 ms budget at 10 fps.

**The tiered path the agent is told to follow:**

| Tier | Tool | Cost |
|---|---|---|
| 1 | `av_ui_tree` — exact element text + coordinates, no OCR guessing | measured 25–59× cheaper than a screenshot |
| 2 | `av_visual_changes`, `av_frame_json` — JSON, no image bytes | ~0.3× a full frame |
| 3 | `av_frame_json(thumbnail=True)` | a tiny thumbnail |
| 4 | `av_frame_region` — ONLY the changed (or densest) pixels | ≪ a full frame |
| 5 | `av_get_frame` + the PNG | full cost, last resort |

Pixels remain available and are sometimes **necessary** — icon colour, layout
breakage and rendering corruption are invisible to text. The point is not to start
there.

**The flight recorder:** capture runs continuously and old frames are pruned so the
disk never fills, but the instant a failure signature appears (structured error,
screen freeze, blank screen, on-screen error text) the window *before* it plus a
short tail is frozen and exempt from pruning. The crucial moment is saved before the
agent asks — `av_incidents`, `av_replay`.

**Visual failure detection:** a hang leaves no log line. `av_visual_events` detects
`screen_frozen` from the screen itself, which log-only analysis cannot.

**Honesty:** `av_token_report` reports measured comparisons and states its
estimation method; `av_ui_tree` reports when a tree would cost *more* than the
screenshot it replaces. See `docs/RESEARCH_TOKEN_EFFICIENCY.md` for the published
basis and our own measurements, and `docs/AGENT_INSTRUCTIONS.md` for a paste-ready
block for a project's `CLAUDE.md`.
