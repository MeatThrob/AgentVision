# LOGS AND EMITTERS

What logging AgentVision can add to a program, and how a log line becomes
something a tool can answer questions about.

Read this before you call `av_bridge_commit`. The `emitters` field in your plan
is the subject of this document.

Everything here was checked against the code in this repo. Where a statement is
about a limitation, the section says how it was checked.

---

## 0. Two words you must not confuse

| Word | What it does | Where it lives |
|---|---|---|
| **emitter** | **CREATES** log output that did not exist. Installed INTO the target program. | `python_backend/emitters.py`, `agent_bootstrap/av_runtime.py`, `python_backend/cli.py` (`agentvision run`) |
| **adapter** | **PARSES** log output that already exists. Never installed anywhere; runs inside AgentVision. | `python_backend/connectors/log_adapters.py` (658 adapters) |

The catalog says this itself, in `adapters.note`:

> adapters PARSE logs that already exist; emitters CREATE logs that do not. A
> program with no logging needs an emitter first — an adapter alone has nothing
> to read.

Consequence: pinning an adapter to a file nothing writes gives you nothing. An
adapter cannot invent data.

---

## 1. The data path

This is the whole pipeline, in order. Every filename below is real.

```
  TARGET PROGRAM
  (any language; may print, may log to a framework, may do neither)
        |
        |  1. the EMITTER intercepts or wraps the output
        v
  EMITTER  — one of four mechanisms, chosen by the target's language
        |
        |-- in-process hook (python)   <project>/sitecustomize.py
        |                              -> agent_bootstrap/av_runtime.py
        |-- preload shim   (node)      <project>/agentvision/emitters/av_emit.js
        |                   (ruby)     <project>/agentvision/emitters/av_emit.rb
        |-- config drop-in (java)      <project>/agentvision/emitters/logback-agentvision.xml
        |                   (.NET)     <project>/agentvision/emitters/serilog-agentvision.json
        |-- launch wrapper (compiled)  `agentvision run -- <cmd>`
        |                              -> python_backend/cli.py  cmd_run()
        v
  SINK FILES, inside the target project
        <project>/agentvision/actions.jsonl   structured, 1 JSON object per line
        <project>/agentvision/log.txt         human-readable mirror (python only)
        <project>/agentvision/state.json      live key/value state (whole file)
        <project>/agentvision/raw_output.txt  verbatim stdout/stderr bytes
                                              (written only by the python
                                               sitecustomize.py tee)
        |
        |  2. AgentVision must be TOLD to read these files
        v
  LOG SOURCE DECLARATION  (profile field `log_sources`)
        [{"path": "<...>/agentvision/actions.jsonl", "adapter": "jsonl",  "label": "events"},
         {"path": "<...>/agentvision/log.txt",       "adapter": "auto",   "label": "text"}]
        resolved by python_backend/connectors/log_sources.py effective_sources()
        |
        |  3. each line is parsed
        v
  ADAPTER  (explicit name > "reader:<name>" > auto-detect)
        connectors/log_adapters.py REGISTRY — 658 adapters
        connectors/log_sources.py READER_REGISTRY — 9 binary/structured readers:
          docker_json, faillock, lastlog, mrt, netflow_v5, pcap, unified2,
          utmp, wtmpdb
        |
        v
  NORMALIZED EVENT (one unified shape, whatever the input format was)
        {ts, ts_ms, level, category, source, data:{message,...},
         trace_id, log_label, log_path}
        |
        v
  TOOLS
        av_log_normalized, av_search, av_timeline, av_program_log,
        av_diagnose, av_error_moment, av_incidents, av_session_report, ...
```

### Sideways paths worth knowing

- **Frame correlation.** The bridge server writes the current screenshot number
  to `<project>/agentvision/.av_frame_seq`. The python, node and ruby in-process
  emitters read that file (cached for 1 second) and stamp `frame_seq` on each
  record. The go, shell, java and .NET emitters, and the `agentvision run`
  wrapper, always write `frame_seq: null` — those targets correlate by `ts_ms`,
  which is the same clock the screenshots are stamped with.
- **Agent waypoints — but read the warning.** `av_log_push(message="...")` posts
  to `POST /log`. That route reads **only** `message` and appends plain text to
  AgentVision's own `activity.log` in the profile output folder. It does **not**
  write `actions.jsonl`, and `category`, `source` and `data` are silently
  dropped, so a pushed note does **not** appear in `av_log_range` or
  `av_actions_around_frame` — despite what the tool's docstring says. This is a
  logged defect; see `av_log_push` in `docs/MCP_TOOL_AUDIT.md`. Verified by
  reading the route.
- **Raw bytes.** `av_log_raw` reads the declared sources verbatim. It does not
  go through the normalizer. See section 5.

### Nothing flows until the bridge is built

`av_capture_start()` and `av_install_project()` are REFUSED until a plan is
committed. The refusal is **HTTP 200** with a body like this (real response):

```json
{
  "ok": false,
  "started": false,
  "bridge_required": true,
  "bridge": {
    "state": "PROVISIONAL",
    "sealed": false,
    "next": "av_bridge_catalog()",
    "blocked": ["capture/start", "install (emitters)"]
  }
}
```

Check the body. A 200 status code does not mean it worked.

---

## 2. Every emitter, one at a time

The list comes from `_emitter_options(language)` in
`python_backend/bridge_plan.py`. It is **language-gated**: you only get to
choose from what that function returns for the detected language.

### Availability

| Emitter id | Offered for |
|---|---|
| `stdout_tee` | every language |
| `lifecycle` | every language |
| `uncaught_exceptions` | python, node, ruby |
| `logging_bridge` | python, node, ruby |
| `swallowed_exceptions` | python only (and it needs Python 3.12+ at runtime) |
| `config_dropin` | java, dotnet, csharp |
| `run_wrapper` | go, rust, cpp, c, shell, and any unknown/blank language |

Read `emitters_available` in your own `av_bridge_catalog()` response. Do not
work from this table if the two disagree — the catalog is generated live.

The full option count per language, measured by calling `_emitter_options`
directly:

| Detected language | Ids offered |
|---|---|
| python | `stdout_tee`, `lifecycle`, `uncaught_exceptions`, `logging_bridge`, `swallowed_exceptions` (5) |
| node, ruby | `stdout_tee`, `lifecycle`, `uncaught_exceptions`, `logging_bridge` (4) |
| java, dotnet, csharp | `stdout_tee`, `lifecycle`, `config_dropin` (3) |
| go, c, cpp, rust, shell, unknown/blank | `stdout_tee`, `lifecycle`, `run_wrapper` (3) |
| **anything else** (swift, kotlin, php, ...) | `stdout_tee`, `lifecycle` only (2) |

That last row is a trap. A Swift or PHP target is offered no `run_wrapper` id,
and `stdout_tee` has no build of its own (section 6.2). For such a target, say in
the `rationale` that the real mechanism is `agentvision run -- <cmd>` and give
the user the command, because the id list cannot express it.

---

### 2.1 `stdout_tee`

| Field | Value (verbatim from the catalog) |
|---|---|
| captures | stdout + stderr, line by line |
| **misses** | anything the program never prints — a silent early exit, a caught-and-discarded exception, a hang with no output |
| cost | none — the program is unchanged |
| languages | interpreted languages loaded in-process |
| builds_as | the language emitter's tee path |
| note | for a COMPILED program use `run_wrapper` instead: there is no in-process hook to install in a native binary |

**Code signal that justifies it:** the `prints_only` signal ("it prints but does
not log properly") or the `subprocess` signal ("it launches other programs,
whose output is lost unless captured").

**Caution:** this id has no build of its own. See section 6.2.

---

### 2.2 `lifecycle`

| Field | Value |
|---|---|
| captures | process start/exit, argv, pid, exit code |
| **misses** | everything between start and exit |
| cost | negligible |
| languages | all |
| builds_as | a facet of the language emitter, not a separate file — for compiled programs the run_wrapper reports the exit code, so selecting both is not additive |

**Code signal that justifies it:** none. No entry in `_CODE_SIGNALS` argues for
`lifecycle`. Justify it from what is ABSENT instead — e.g. "nothing in the code
marks run boundaries, and a segfaulting game loop otherwise shows up only as a
non-zero exit". That is a real reason and the validator accepts it, but you have
to write it yourself.

**Caution:** for a compiled program, `run_wrapper` already reports the exit code.
Selecting both adds nothing.

---

### 2.3 `uncaught_exceptions`

| Field | Value |
|---|---|
| captures | uncaught exceptions + thread exceptions + shutdown errors |
| **misses** | every exception the program catches — which on code with broad excepts is most of them; pair with `swallowed_exceptions` |
| cost | negligible |
| languages | python, node, ruby |
| builds_as | sitecustomize / preload shim hook |

**Code signals that justify it:** `threads` ("concurrency — a crash on a worker
never reaches the main excepthook") and `async` ("async work — failures surface
as unhandled rejections, not tracebacks").

**How it really works (python):** `agent_bootstrap/av_runtime.py`
`_install_exception_hooks()` replaces `sys.excepthook` and
`threading.excepthook`, and chains to the previous hook so the program still
dies the way it would have.

---

### 2.4 `logging_bridge`

| Field | Value |
|---|---|
| captures | the language's own logging framework, mapped to levels |
| **misses** | bare `print()`/`printf` output, and anything logged before the bridge is installed |
| cost | negligible |
| languages | python (logging), node, ruby, java (logback), .NET (Serilog) |
| builds_as | logging handler attached at import, or a config drop-in |

**Code signals that justify it:** `existing_logging` ("the program ALREADY logs"
-> "route what exists — full hooks may be redundant") and `logs_in_handler`
("handlers that DO report the error").

**How it really works (python):** a `logging.Handler` subclass is added to the
root logger at level DEBUG. If the root logger is still at its default
`WARNING`, it is raised to `INFO`, because the default is too quiet to capture
anything useful.

---

### 2.5 `swallowed_exceptions`

| Field | Value |
|---|---|
| captures | exceptions the program CATCHES and hides (`try/except: pass`) |
| **misses** | non-exception failure: a wrong value returned, a branch never taken. Also silent on Python < 3.12, where `sys.monitoring EXCEPTION_HANDLED` does not exist |
| cost | near zero via `sys.monitoring`; needs Python 3.12+ |
| languages | python 3.12+ |
| builds_as | `sys.monitoring EXCEPTION_HANDLED` hook (tool id 4) |

**Code signals that justify it:** `discards_error` — "handlers whose body throws
the error away (pass / return None / continue / empty block) — the failure
becomes invisible to every other hook". The catalog calls this "the strongest
possible case for it". `file_io` also argues for it ("permission/missing-path
errors are often swallowed").

**Cautions, all verified in `av_runtime.py`:**

- On Python 3.11 and older it arms nothing and stays silent. It is also disabled
  by `AGENTVISION_CATCH_SWALLOWED=0`. **You can check which happened:** the
  `av.bootstrap.start` record carries `data.catches_swallowed_exceptions`
  (`true`/`false`). Read that field before concluding that no exception was
  swallowed. Silence with `false` means "not watched", not "not happening".
- Live records are **de-duplicated**: a repeating site is emitted at occurrence
  1, 10, 100, so `occurrences` in a live record understates the real count. The
  true totals arrive at process exit in one
  `av.bootstrap.swallowed_summary` record (`total_occurrences`,
  `distinct_sites`, `sites[]` with `raised_at` and `handled_in`). If the process
  is killed before exit, you do not get that summary.

---

### 2.6 `config_dropin`

| Field | Value |
|---|---|
| captures | structured JSON logs via the ecosystem's own appender |
| **misses** | anything written outside the logging framework, and everything before config load |
| cost | a config file drop-in; needs the program restarted |
| languages | java, .NET |
| builds_as | a logback/Serilog config file in the project |

**Code signal that justifies it:** `existing_logging` matching `log4j`,
`logback`, `Serilog` or `slf4j`.

**How it really works:** the installer writes
`agentvision/emitters/logback-agentvision.xml` (java) or
`agentvision/emitters/serilog-agentvision.json` (.NET). **The file does nothing
until the app is told to use it.** For java that is
`java -Dlogback.configurationFile=<project>/agentvision/emitters/logback-agentvision.xml ...`.
The fallback, if nobody wires the config, is launching through
`agentvision run -- java -jar app.jar`, where the stdout tee bridges it.

---

### 2.7 `run_wrapper`

| Field | Value |
|---|---|
| captures | stdout/stderr via `agentvision run -- <cmd>`, normalized, plus the exit code |
| **misses** | anything not written to stdout/stderr — a segfault leaves only the exit status, and output buffered at crash time can be lost entirely |
| cost | launch through the wrapper; no code or build change |
| languages | go, rust, c/c++, shell, anything |
| builds_as | the tee emitter + CPP_README; **ONLY takes effect if the program is actually launched through `agentvision run`** |

**Code signal that justifies it:** `prints_only` plus the fact that the language
is compiled — a native binary has no in-process hook to install.

**How it really works** (`python_backend/cli.py` `cmd_run`):

1. writes an `av.wrapper.start` record (category `process`) with `argv`, `run_id`,
   `project`;
2. spawns the child with `stdout=PIPE, stderr=PIPE`, injecting
   `AGENTVISION_PROJECT`, `AGENTVISION_LOG_DIR`, `AGENTVISION_SINK`,
   `AGENTVISION_RUN_ID`, and `PYTHONPATH`;
3. reads each line, mirrors it to your terminal, and appends one JSON record per
   line to `actions.jsonl`, parsed through the adapters;
4. writes an `av.wrapper.exit` record with `exit_code`.

If the child is python/node/ruby with its own in-process emitter, the wrapper
only mirrors to the terminal and does not emit records, to avoid duplicating
every line.

**This is the emitter that silently does nothing when ignored.** If the user
starts the binary from a terminal or a launcher instead of `agentvision run`,
you get zero records and no warning. Say this to the user when you pick it, and
give them the exact command.

---

## 3. Decision table

| If the target looks like this | Choose | Because |
|---|---|---|
| **Already logs well** — high `existing_logging` count, and it writes a file an adapter parses | `emitters: []` | Nothing needs adding. You only need a log source declared and the right adapter. See section 4 — and read section 6.6, because an empty list also skips source registration. |
| **Prints only**, interpreted (python/node/ruby) — `prints_only` signal, no log file | `["stdout_tee"]`, plus `lifecycle` if run boundaries matter | The output exists but goes nowhere durable. Teeing it is the cheapest thing that produces data. |
| **Compiled binary** (c/c++/go/rust/shell) | `["run_wrapper"]` (+ `lifecycle` only if you want a reason recorded; the wrapper already reports exit code) | No in-process hook can be installed in a native binary. The process boundary is the only place to capture. Tell the user the exact `agentvision run --` command. |
| **Broad excepts / discards errors** — `discards_error` signal, python 3.12+ | `["swallowed_exceptions"]` (+ `uncaught_exceptions` if it also has threads/async) | The bugs never reach stderr or any excepthook. `sys.monitoring EXCEPTION_HANDLED` is the only hook that sees them. |
| **Broad excepts, python 3.11 or older** | `["uncaught_exceptions", "logging_bridge"]` and say in `why` that swallowed capture is unavailable | `swallowed_exceptions` is not offered and would install nothing. Do not select it and pretend. |
| **JVM / .NET service** — logback/Serilog present | `["config_dropin"]` (+ `logging_bridge` if you want the framework mapped to levels) | The ecosystem's own appender produces structured JSON at negligible cost. Remember the app must be pointed at the config file. |
| **GUI app** — `gui_toolkit` signal (SDL, GLFW, Qt, tkinter, Electron, Cocoa...) | emitters per the language rows above, **plus** `visual_capture: true` | The screen shows state no log contains. |
| **Headless worker / web service** — `web_service` signal, no `gui_toolkit` | emitters per the language rows above, **plus** `visual_capture: false` | There is no window. Screenshot capture would burn CPU and tokens on nothing. Put GUI tools (`av_ui_tree`, `av_ocr_frame`, `av_read_screen`) in `tools.not_relevant`. |

Hard limits the validator enforces on top of this table:

- **6 or more emitters is REJECTED.** "that is close to everything on offer.
  Installing the lot is the same blanket guess this gate exists to prevent."
  (In practice unreachable with valid ids — see section 6.7.)
- Every selected emitter needs a `why` entry of **15 characters or more**.
- More than 25 tools in `tools.primary` is REJECTED as a copy of the catalog.

### A complete, valid plan (compiled C GUI game)

Replace the `catalog_token` with the one from your own `av_bridge_catalog()`
call. A stale token is rejected.

```
av_bridge_commit(plan={
  "catalog_token": "8834252a07355b23",
  "emitters": ["run_wrapper", "lifecycle"],
  "why": {
    "run_wrapper": "prints_only signal: src/common/Logging.c printf()s to stdout with no log file, and a native binary has no in-process hook, so wrapping the launch is the only way to see any of it",
    "lifecycle": "existing_logging x25 covers runtime events but nothing marks run boundaries; an SDL game loop that segfaults shows up only as a non-zero exit"
  },
  "rationale": "Compiled C/SDL2 engine. Its Log_Message macro printf()s level+file:line to stdout and writes no file, so capture has to happen at the process boundary; it renders into a real window, so visual capture earns its place.",
  "adapters": {"events": "jsonl", "text": "abyssengine"},
  "capture": {"interval_seconds": 1.0},
  "visual_capture": true,
  "tools": {
    "primary": ["av_diagnose", "av_log_raw", "av_source_at_error", "av_error_moment",
                "av_visual_changes", "av_frame_region", "av_search", "av_incidents",
                "av_log_where", "av_session_report"],
    "not_relevant": {
      "av_run_tests": "hardcodes python -m pytest; this is C with no pytest suite",
      "av_ui_tree": "SDL blits one opaque surface, so the accessibility tree is a single empty node",
      "av_ocr_frame": "OCR of rendered text is worse than the log, which carries the same strings verbatim",
      "av_state_at": "needs periodic wide full-state snapshots; this program never emits any",
      "av_trace_timeline": "needs a trace_id per record; printf output carries none"
    },
    "note": "Reviewed all 19 groups. Chosen set follows the failure mode: asset-load FATALs that carry file:line, so the log-to-source jump matters more than any pixel tool."
  }
})
```

That is a real committed plan, copied from
`AbyssEngine/agentvision/abyss/.av_bridge_plan.json`. Two things about its
`adapters` map you should know before copying the pattern:

- Keys are **log source labels**. The commit route only ever creates two:
  `events` (-> `actions.jsonl`, default adapter `jsonl`) and `text`
  (-> `log.txt`, default adapter `auto`).
- The pseudo-label `stdout` is accepted, but it is a fallback applied to **every**
  label that has no explicit entry. Verified in the commit route:
  `adapter = plan_adapters.get(label) or plan_adapters.get("stdout") or default_adapter`.
  So `{"stdout": "abyssengine"}` alone would also pin `abyssengine` onto
  `actions.jsonl`, replacing the correct `jsonl` adapter. Always name `events`
  explicitly when you use `stdout`.

---

## 4. How to choose NOTHING, and why that is respected

`emitters: []` is a legitimate, expected answer. A program that already writes a
parseable log does not need AgentVision writing anything into it. Adding hooks
to such a program is noise, risk, and a file in someone's repo they did not ask
for.

The gate does not punish you for this. It only demands you say it out loud, so
an empty list cannot be confused with a forgotten one.

**The exact rule** (`bridge_plan.validate_plan`): when `emitters` is `[]`, the
`rationale` string must contain the substring `already` **or** the substring
`no log`, case-insensitively. Nothing else satisfies it.

Valid: `"already logs well: writes structured JSON to logs/app.jsonl"`
Valid: `"the program has no logging we may add to — vendor binary"`
REJECTED: `"logging is fine as it is"` (contains neither substring)

### A complete, valid do-nothing plan

```
av_bridge_commit(plan={
  "catalog_token": "510cd849c665fb8c",
  "emitters": [],
  "rationale": "Already logs well: express service writes pino JSON to logs/app.log on every request and error, which the pino adapter parses at full fidelity. Adding hooks would duplicate every line.",
  "adapters": {"applog": "pino"},
  "capture": {"interval_seconds": 2.0},
  "visual_capture": false,
  "tools": {
    "primary": ["av_log_normalized", "av_search", "av_diagnose",
                "av_errors_by_fingerprint", "av_log_where", "av_session_report"],
    "not_relevant": {
      "av_ui_tree": "headless service, no window and no accessibility tree",
      "av_read_screen": "headless service, nothing on screen to read",
      "av_ocr_frame": "headless service, no frames worth OCR",
      "av_visual_changes": "visual capture is off for this program by decision"
    },
    "note": "Log-only program: the whole investigation happens in the normalized log."
  }
})
```

`why` is not required when `emitters` is `[]`. `tools` still is.

**Before you commit an empty plan, do one thing:** confirm the program's
existing log is already declared as a log source. Call `av_log_sources()`. With
`emitters: []` the commit installs nothing and registers nothing (section 6.6),
so if the log is not in `log_sources`, the bridge seals as BUILT with nothing to
read.

---

## 5. The sink files

All paths are inside the target project, not inside AgentVision.

| File | Written by | Contains |
|---|---|---|
| `agentvision/actions.jsonl` | every emitter, and the `agentvision run` wrapper | The structured event stream. One JSON object per line. This is the primary sink. **Not** written by `av_log_push` — see section 1. |
| `agentvision/log.txt` | **only** the python in-process runtime (`agent_bootstrap/av_runtime.py`) | A human-readable mirror of the same events, for a person running `tail -f`. |
| `agentvision/state.json` | the target program, if it chooses to | Live key/value state, read whole-file on each snapshot. |
| `agentvision/raw_output.txt` | only the python `sitecustomize.py` tee | Verbatim stdout/stderr bytes, unparsed. |
| `agentvision/.av_frame_seq` | the bridge server (AgentVision writes this one) | The current screenshot number, so the python/node/ruby emitters can stamp `frame_seq`. |
| `agentvision/manifest.json` | the installer | What was installed: language, emitter kind, sink paths, how to load it. |
| `agentvision/<profile_name>/.av_bridge_plan.json` | `av_bridge_commit` | The committed plan. This is what makes the gate fire once, ever. Non-word characters in the profile name become `_`. When the profile has no valid `project_root`, this folder falls back to AgentVision's own snapshots dir instead. |
| `agentvision/<profile_name>/.av_preflight_ok` | `av_preflight`, and `av_bridge_commit` after sealing | The legacy coverage marker. Its mere existence also counts as "sealed", so an older install is never re-gated. |

**Verified:** the Node, Ruby, Go, shell, Rust and C/C++ emitters write
`actions.jsonl` only, and `cli.py`'s wrapper writes `actions.jsonl` only. Only
`av_runtime.py` writes `log.txt`. So on a compiled target, `log.txt` stays empty
unless the program itself writes there. Do not pin a custom adapter to
`log.txt` and expect lines to appear.

### The JSONL record shape

Written by `agent_bootstrap/av_runtime.py` `_emit()` and by `cli.py`
`_emit_jsonl()`. Ten keys, always present:

```json
{"ts": "2026-07-29T19:03:38.201Z", "ts_ms": 1785345818201.0, "category": "process", "source": "av.wrapper.start", "state": null, "run_id": "av-1785345818201-40233", "trace_id": null, "frame_seq": null, "coords": null, "data": {"run_id": "av-1785345818201-40233", "argv": ["./abyss"], "project": "~/projects/AbyssEngine", "wrapper_pid": 40233}}
```

A console line captured by the wrapper and parsed by an adapter looks like this.
Note the extra top-level `level`, and that `data.text` always keeps the raw line
exactly as printed:

```json
{"ts": "2026-07-29T19:03:41.880Z", "ts_ms": 1785345821880.0, "category": "error", "level": "ERROR", "source": "Sprite.c:32", "state": null, "run_id": "av-1785345818201-40233", "trace_id": null, "frame_seq": null, "coords": null, "data": {"adapter": "abyssengine", "message": "atlas not found", "text": "[ERROR] Sprite.c:32 - atlas not found", "channel": "stderr"}}
```

Field notes:

| Field | Meaning |
|---|---|
| `ts` / `ts_ms` | ARRIVAL time on AgentVision's clock — the same clock that stamps screenshots. That is what makes correlation exact. A timestamp found inside the line is preserved separately as `data.emit_ts_ms`. |
| `category` | `process`, `stdout`, `stderr`, `log`, `error`, `exception`, `warn`, `metric`, `file`. Drives failure detection. The wrapper uses the adapter's category when a line parses, else the channel name. |
| `level` | Present on wrapper-written console records (empty string when the line did not parse). The in-process python emitter does not set a top-level `level`; it puts the level in `data.level`. |
| `source` | Who emitted it: `av.wrapper.start`, `av.wrapper.exit`, `av.wrapper.stdout`, `av.wrapper.stderr`, `av.bootstrap.stdout`, `av.bootstrap.stderr`, `av.bootstrap.log.<logger>`, `av.bootstrap.excepthook`, `av.bootstrap.thread_excepthook`. When an adapter parses a logger name out of the line, that name is used instead. |
| `frame_seq` | Screenshot number at emit time, read from `.av_frame_seq`. **Only the in-process emitters stamp it.** The `agentvision run` wrapper always writes `null` here (verified: both record builders in `cli.py` hardcode it), so a compiled target correlates by `ts_ms` alone — which is the same clock, so alignment still holds. |
| `data` | Free-form payload. `data.message` or `data.text` carries the human string. |

### The text-log line format

`av_runtime._emit()` writes one line per event, python targets only:

```
2026-07-29T19:03:41.880Z [WARNING] [av.bootstrap.log.assets] atlas missing, using fallback
2026-07-29T19:03:41.881Z [INFO] [av.bootstrap.stdout] text=loaded atlas
```

- The level in brackets is `data.level` verbatim when the record has one (so a
  python `logging` record shows `WARNING`, not `WARN`). Otherwise it comes from
  the category map: `exception`/`error` -> ERROR, `warn`/`stderr` -> WARN,
  everything else -> INFO.
- The message is `data.message`, else `data.name`, else the first four `data`
  keys joined as `k=v` — which is why a tee'd stdout line appears as
  `text=loaded atlas`.

### `state.json`

Created by the installer with this template, then owned by the target program:

```json
{
  "version": 1,
  "run_id": null,
  "started_at": null,
  "updated_at": null,
  "fields": {}
}
```

It is read whole-file (`ProgramConnector.read_state`), not tailed. Put gauges in
`fields` — HP, queue depth, frame number, whatever you want correlated with a
screenshot. A program that never writes it produces no state, and tools that
need periodic wide state snapshots (`av_state_at`, `av_state_diff`) have nothing
to work with. That is a legitimate `not_relevant` reason.

### `av_log_raw` vs `av_log_normalized`

| | `av_log_raw` | `av_log_normalized` |
|---|---|---|
| Route | `GET /log/raw` | `GET /log/normalized` |
| Returns | The program's own bytes, per source | Merged unified events across ALL sources |
| Interpretation | **none** | adapter-parsed: level, category, source, message |
| Time handling | read position / byte offsets | `from_ms` / `to_ms` window, or most-recent-by-count |
| Params | `session_id`, `all`, `from_offset`, `cap_bytes`, `peek` | `from_ms`, `to_ms`, `level`, `label`, `limit` |
| Cost (tool_meta) | medium | low |
| Use when | a summary looks suspiciously clean; you need exact field values | you want one timeline across several logs in several formats |

**Why raw is verbatim and never interpreted.** A normalizer must decide what a
line means, and every such decision can be wrong. The route says so itself:

> The program's OWN output, verbatim. AgentVision did not level it, rank it, or
> decide what mattered — that is your job, and a summary that decided for you is
> how 180 present failures got reported as 'healthy'.

That is a real incident recorded in this repo: `av_diagnose` reported "health
100, no strong failure signals, program looks healthy" while 180 GPU present
failures sat in the bytes, and the bug was only visible in the raw `target=` and
`tex0=` fields the summary had flattened into prose. When a summary and the raw
log disagree, the raw log is right.

The one reduction raw applies is lossless: consecutive byte-identical lines
collapse to `{line, repeat: N}`. On a real boot log, 49% of it was one line
repeated 21,982 times. No distinct line is dropped, re-levelled or reordered.
Records AgentVision itself wrote are excluded, and each source reports `stale`
and `last_write_age_s` so a log the program is not actually writing to cannot be
mistaken for live output.

**Caveat, verified in code:** the `av_log_raw` tool exposes no `collapse`
parameter, and the route's default branch (`_raw_log_delta`) never forwards one.
You cannot get uncollapsed output through this tool. This is documented as a
known defect in `docs/MCP_TOOL_AUDIT.md`.

---

## 6. KNOWN GAP: what the plan records vs what gets built

Read this section before you write a `why` entry that promises a specific hook.

### 6.1 `plan["emitters"]` does not drive installation

The emitter list is recorded for audit. It does not select what is installed.

- `POST /bridge/commit` calls `install_into_project(root, profile_name=..., language=...)`.
  **No emitter list is passed.** (`python_backend/api/bridge_server.py`, commit route.)
- `installer.install_into_project()` calls `build_emitter(lang, ...)`, which
  returns **one** language-appropriate `EmitterSpec`. There is no per-id branch.
- The commit response is honest about it. It returns
  `emitters_requested` alongside `install.emitter`, with this note: "`emitters_requested`
  is the agent's decision as recorded; `install.emitter` is the single emitter
  actually scaffolded for this language."

**Verified end to end on a real program.** The AbyssEngine plan names
`["run_wrapper", "lifecycle"]`. Its `agentvision/manifest.json` records exactly
one emitter:

```json
{"language": "cpp", "kind": "tee", "autoload": false, "verify": "tee",
 "run_env": {}, "files": ["agentvision/emitters/CPP_README.txt"],
 "notes": "Compiled: tee bridge by default."}
```

One emitter, one file, and that file is a README.

### 6.2 `stdout_tee` has no implementation

`stdout_tee` is offered for **every** language, and `build_emitter()` produces
nothing named for it. Its `builds_as` is "the language emitter's tee path" —
i.e. it describes a property of whatever single emitter the language gets, not a
thing that is built.

What actually tees, by language:

| Language | What really happens when you select `stdout_tee` |
|---|---|
| python | `sitecustomize.py` wraps `sys.stdout`/`sys.stderr` (this exists, and also mirrors to `raw_output.txt`) |
| node / ruby | the preload shim patches `console` / `$stdout` (this exists) |
| compiled / unknown | **nothing in-process.** Only the `agentvision run` wrapper tees, and only if the program is launched through it |

Its own `note` tells you this: "for a COMPILED program use `run_wrapper` instead:
there is no in-process hook to install in a native binary." Believe the note.

### 6.3 `lifecycle` is not a separate artifact

Its `builds_as` says so: "a facet of the language emitter, not a separate file".
Selecting `lifecycle` alongside `run_wrapper` is not additive — the wrapper
already writes `av.wrapper.start` and `av.wrapper.exit`.

### 6.4 On python, ALL hooks install regardless of your selection

`agent_bootstrap/av_runtime.py` `install_all_hooks()` installs, unconditionally:
the stdout/stderr tee, the root logging handler, the exception hooks, and the
swallowed-exception monitor (when the interpreter is 3.12+). It takes no
argument naming which hooks to install.

So on a python target, selecting only `logging_bridge` still installs the tee
and the exception hooks. Your selection narrows the RECORD, not the build.

### 6.5 `plan["capture"]["interval_seconds"]` is recorded, not applied

The commit route never sets the capture interval. Grep confirms the only
consumer of `interval_seconds` outside the plan schema is the GUI's display
code. To actually change the rate, pass it to the tool:
`av_capture_start(interval=1.0)`.

### 6.6 An empty emitter list also skips log-source registration

In the commit route, the installer call and the `log_sources` wiring are both
inside `if plan.get("emitters") and root and os.path.isdir(root)`. With
`emitters: []` the response records "no emitters requested — nothing written to
the target program (per plan.rationale)" and **no sources are registered**.

That is correct behaviour for a program that already logs — but only if its log
is already declared. Check with `av_log_sources()` before committing an empty
plan.

### 6.7 The validator does not check that emitter ids exist

`validate_plan()` checks the token, that `emitters` is a list, the `rationale`,
one `why` entry of 15+ characters per selected emitter, the 6-emitter ceiling,
and the `tools` shape. It never compares your ids against
`emitters_available`. A typo, or an id from another language, will seal
silently. Copy ids from your own catalog response.

A side effect of that: the "6 or more emitters" ceiling is unreachable with
valid, unique ids, because the most any language is offered is 5 (python). It
only fires if you repeat an id or invent ids. Do not read it as permission to
select all of them — the per-emitter `why` requirement is the check that
actually bites.

### What IS honoured

| Plan field | Honoured? | Evidence |
|---|---|---|
| `adapters` (per source label) | **Yes** | Commit maps `adapters[label]`, or `adapters["stdout"]`, onto the new source. Verified: AbyssEngine's `text` source is pinned to the custom `abyssengine` adapter, and `/bridge/report` shows `adapter_declared` = `adapter_resolved` = `abyssengine`. |
| `rationale`, `why`, `tools` | **Yes**, as a persisted record | Written to `.av_bridge_plan.json` and echoed by `GET /bridge/report`. |
| `emitters` | Recorded only | Section 6.1 |
| `capture.interval_seconds` | Recorded only | Section 6.5 |
| `visual_capture` | Recorded only | Written to the plan; capture is started by `av_capture_start`. |

---

## 7. If the adapter parses your log wrongly

This happens, and it is fixable at runtime. A real case: a C engine printed
`[DEBUG] Sprite.c:32 - msg`, and the unrelated `coreboot_cbmem` adapter claimed
that format at 1.00 confidence and reported `source=coreboot`, burying the
`file:line`.

Steps:

1. `av_test_adapter(line="<one real line>")` — see which adapter claims it and
   with what confidence. `is_fallback: true` means nothing parses it.
2. `av_add_adapter(...)` with a named-group `extract_regex`, a REAL `sample`
   line, and — when a wrong adapter already claims the format at equal or higher
   confidence — `outrank="<that adapter's name>"`. `outrank` registers yours
   immediately before it and breaks a tie in your favour. It cannot rescue a
   weaker pattern.
3. Pin it in the plan: `"adapters": {"text": "<your adapter name>"}`.

"The format is already covered" is not the same as "covered correctly".

---

## 8. File map

| Path | What it is |
|---|---|
| `python_backend/bridge_plan.py` | The gate: `_emitter_options`, `_CODE_SIGNALS`, `catalog`, `validate_plan`, plan persistence. Source of truth for this document. |
| `python_backend/emitters.py` | `build_emitter()` and the per-language emitter source. Sink name constants. |
| `python_backend/installer.py` | `install_into_project()` — what is scaffolded into a project. |
| `agent_bootstrap/av_runtime.py` | The python in-process hooks and the record writer. |
| `python_backend/cli.py` | `agentvision run` — the wrapper/tee (`cmd_run`). |
| `python_backend/connectors/log_sources.py` | Source resolution, readers, raw + normalized reads. |
| `python_backend/connectors/log_adapters.py` | The 658-adapter registry and the detector. |
| `python_backend/api/bridge_server.py` | Routes: `/start_here`, `/bridge/status`, `/bridge/catalog`, `/bridge/commit`, `/bridge/report`, `/capture/start`, `/log/raw`, `/log/normalized`. |
| `python_backend/api/claude_mcp.py` | The MCP tools and the server instructions. |
| `python_backend/api/tool_meta.json` | Per-tool metadata: what it returns, `needs`, cost. 94 tools. |
| `docs/MCP_TOOLS_REFERENCE.md` | **Generated** from `tool_meta.json` by `scripts/gen_tools_ref.py`. Never hand-edit it. |
| `docs/MCP_TOOL_AUDIT.md` | 77 known tool defects, 12 of them Class-A. Check it before trusting a tool's output. |

Note: `/bridge/report` is an HTTP route, not an MCP tool. There is no
`av_bridge_report`. Read it with a plain GET against the bridge server if you
want the per-program record.
