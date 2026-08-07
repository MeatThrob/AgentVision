# What Is AgentVision?

> **AgentVision is a multi-language, multi-program *universal debug log* built
> for an AI assistant.** It normalizes everything a running program does — logs
> in any language *and* screenshots of its screen — into token-efficient JSON
> files, time-aligned onto a single clock, so a model like Claude can see and
> reason about a live program it otherwise cannot observe. It is engineered to
> emit *less* text than an ordinary debug log while carrying *more* actionable
> signal per token.

This is **AgentVision v5** (see `VERSION` → `5.1`). This document is the
definitive explanation of what the project is, what problem it solves, and how
its two hardest parts work: **the screenshot engine** and the **screenshot↔log
time-alignment** that is the core of its value. Every number and name below is
taken from the source in this repository.

- Companion doc: [`HOW_IT_WORKS.md`](../HOW_IT_WORKS.md) — narrative walkthrough
- Event/frame schema reference: [`docs/SCHEMA.md`](SCHEMA.md)
- Log-adapter registry: [`docs/LOG_ADAPTERS.md`](LOG_ADAPTERS.md)
- Setup: [`SETUP.md`](../SETUP.md) (macOS), [`SETUP-Windows.md`](../SETUP-Windows.md), [`dist/linux/README.md`](../dist/linux/README.md)

---

## Table of contents

1. [What it is & the problem it solves](#1-what-it-is--the-problem-it-solves)
2. [The universal multi-language / multi-program debug log](#2-the-universal-multi-language--multi-program-debug-log)
3. [The screenshot aspect (deep)](#3-the-screenshot-aspect-deep)
4. [Time-alignment (deep) — the core value](#4-time-alignment-deep--the-core-value)
5. [Built for an AI assistant: token-conserving yet more productive](#5-built-for-an-ai-assistant-token-conserving-yet-more-productive)
6. [Architecture & platforms](#6-architecture--platforms)
7. [Quick start](#7-quick-start)

---

## 1. What it is & the problem it solves

When you ask an AI coding assistant to debug a program, it can read your source
and your log files — but it is **blind to the running program**. It cannot see
the window, cannot watch a value change over time, and cannot tell *what the
program was actually doing at the instant something looked wrong on screen*.
This is especially true of compiled GUI programs, games, bots, robotics/RPA
loops, and long-running services that can't simply be re-run headlessly under a
debugger.

AgentVision closes that gap. It gives the AI two things at once:

- **Eyes** — periodic screenshots of the exact window/region/screen the program
  draws to, each one saved as a PNG with a self-describing JSON sidecar.
- **Instrumentation** — every log the program writes, in whatever language and
  format, normalized into one unified JSON event stream on a single clock.

And then it does the thing that makes the pair *useful*: it **time-aligns** the
screenshots to the logs so precisely that, for any frame, the AI can pull "what
every log said at that exact shutter instant" and trust that nothing after the
shutter leaked in.

The design goal, in the project's own framing, is a **universal debug log for an
AI** — one that outputs everything as JSON, conserves tokens, and is *more*
productive than an average debug log because each byte the model reads is
higher-signal (triage summaries, deduplicated errors with probable causes,
state diffs, correlation handles) rather than raw wall-of-text.

---

## 2. The universal multi-language / multi-program debug log

AgentVision's promise is: *connect to ANY program in ANY language and already
have the log ready.* It reaches that through a **two-sided bridge**.

### 2a. The INPUT side — reading logs from anything

Whatever a program writes, AgentVision reads it and normalizes each line into
**one unified event schema** (`python_backend/connectors/log_adapters.py`):

```json
{
  "ts":        "2026-07-20T12:00:00.123Z",   // ISO-Z UTC, "" if unknown
  "ts_ms":     1784628000123.0,               // epoch ms, null if unknown
  "category":  "log|debug|warn|error|event",  // level-derived bucket
  "level":     "INFO|DEBUG|WARN|ERROR|FATAL",  // canonical, upper-case
  "source":    "logger.name | subsystem",      // who emitted it
  "trace_id":  "abc123" | null,                // correlation id if found
  "frame_seq": null,                            // filled in by the bridge
  "data":      { "message": "...", "adapter": "...", ...structured fields },
  "raw":       "<the original line>"
}
```

`category` is derived from `level` so the failure detector (any event with
`category == "error"`) fires for `ERROR`/`FATAL`/`CRITICAL` lines from **any**
language — Java's `SEVERE`, Go/zap's `fatal`, .NET's `WRN`, syslog's numeric
severities, and so on are all mapped onto one small canonical vocabulary.

**~656 named adapters.** The adapter `REGISTRY` currently holds **658 entries**:
**656 named format adapters**, plus the universal **`structural`** normalizer and
the **`raw`** floor. Each adapter implements `detect(sample_lines) -> float` (a
confidence in `[0,1]`) and `parse_line(line) -> event`. `detect_adapter()`
samples the head of a log, scores every adapter, and picks the winner; `raw`
always matches with a tiny floor score so **detection never fails**.

Named adapters are organized as a small hand-written core plus **~25 family
modules** in `python_backend/connectors/adapters/`:

```
android   apple    backup    bigdata   cicd      cloud     console
database  devtools industrial kernel   mainframe messaging network
observability  os_platform  profiling  runtime  security  telecom
virt      webserver  (+ _common, batch8b, batch9)
```

Two adapters do disproportionate work:

- **The `jsonl` super-adapter** — a *single* adapter that transparently
  normalizes **every JSON-based logger**: pino/winston/bunyan/roarr (Node),
  structlog/loguru-json (Python), zap/slog-json/logrus-json (Go),
  logback-json/log4j2-json/GELF (Java), Serilog/NLog-json (.NET), MongoDB,
  journald `-o json`, Docker `json-file`, Caddy, Envoy access-json, OTLP-json,
  and AgentVision's own native records. It does it with field-alias resolution
  (`msg`/`message`/`event`/`MESSAGE`…), numeric-level coercion (pino 10–60 vs
  syslog 0–7), and timestamp coercion (epoch s/ms/µs/ns, ISO strings, Mongo
  `{"$date": …}`).

- **The `structural` auto-normalizer** — pinned just above `raw`. When *no*
  named adapter recognizes a line, it still produces a schema-valid event by
  recognizing common *structural shapes* (kernel ring-buffer `[   12.345]`,
  dmesg ctime, `/dev/kmsg`, syslog RFC3164/5424, ISO-prefixed lines,
  level-bracket/level-prefix, logfmt key=value, embedded JSON, Common Log
  Format) and, failing that, inferring the level from keywords
  (`panic`/`fatal`/`traceback`/`error:` → error; `warning`/`deprecat` → warn).
  Its `detect()` is deliberately capped at `≤0.3` so it can never outrank a
  named adapter, and floored `>raw` so an utterly unknown line still lands as
  usable JSON.

**The result: nothing is ever dropped.** A line is either parsed by a named
adapter, or structurally normalized, or captured raw — but in all three cases it
becomes a valid JSON event on the shared schema.

#### Coverage

The coverage is backed by a research catalog of **1,430 real-world log formats**
(`docs/log_catalog_master.json`), triaged into **454 high-priority**,
**584 medium-priority**, and **392 low-priority** entries. Against that catalog,
the code's own coverage ledger (the `COVERAGE + ROADMAP` block at the bottom of
`log_adapters.py`) records named-adapter coverage of roughly:

| Priority tier | Formats | Named coverage (approx.) |
|---------------|--------:|--------------------------|
| High          | 454     | ~96%                     |
| Medium        | 584     | ~93%                     |
| Low           | 392     | ~83%                     |
| **Overall**   | **1,430** | **~88% named**         |

Everything *not* matched by a named adapter is still normalized — by the
`structural` normalizer, or captured by `raw` — so the effective JSON coverage
is **100%**. The remaining gaps are almost entirely template/documentation-only
catalog samples (a column description, a SQL `SELECT`) whose real formats
already route to a named adapter, plus binary telemetry handled separately (see
below).

#### Binary telemetry via source readers

Not every source is a line of text. For structured/encoded streams, a
**SourceReader** decodes the source into records (str lines *or* pre-decoded
dicts), which are then normalized through the *same* adapter pipeline — dicts go
through the `jsonl` adapter's native passthrough — so binary telemetry lands on
the **same timeline** as the text adapters. The registered readers
(`log_sources.list_readers()`) are:

```
docker_json  utmp  lastlog  faillock  wtmpdb  netflow_v5  pcap  mrt  unified2
```

The eight pure-stdlib `struct` readers live in
`python_backend/connectors/readers.py` (`BINARY_READERS`): `utmp`/wtmp/btmp
login records, `lastlog`, PAM `faillock`, `wtmpdb` (sqlite), NetFlow v5
datagrams, classic/pflog `pcap`, MRT/BGP dumps, and Snort `unified2` IDS events.

#### Multi-source merge onto ONE timeline

A single program routinely writes several logs at once — a JSONL event stream, a
plain-text app log, a framework log in a totally different format.
`python_backend/connectors/log_sources.py` resolves all of a profile's sources,
normalizes each through the adapters, and **merges them, timestamp-sorted, onto
the single UTC-ms timeline**. Every merged event carries an extra `log_label` and
`log_path` so the AI can tell *which* log a line came from after the merge.
Events with no parseable timestamp sort to the end (stable), never lost.

#### One concept, three languages, one schema

Here are three *different-language* log lines describing the same failure. Fed
through `detect_adapter()`, each is picked up by a different adapter — and all
three come out as the **identical unified event shape** (`category: "error"`):

```jsonc
// Node/pino JSON  →  adapter "jsonl"
{"ts":"2026-07-20T19:00:00.123Z","ts_ms":1784574000123.0,"category":"error",
 "level":"ERROR","source":"jsonl","data":{"message":"payment charge failed",
 "adapter":"jsonl","pid":9001,"orderId":4471}, ...}

// Python logging  →  adapter "python_logging"
{"ts":"2026-07-20T19:00:00.123Z","ts_ms":1784574000123.0,"category":"error",
 "level":"ERROR","source":"payment","data":{"message":"charge failed for order 4471",
 "adapter":"python_logging"}, ...}

// Go zap (logfmt)  →  adapter "logfmt"
{"ts":"2026-07-20T12:00:00.123Z","ts_ms":1784548800123.0,"category":"error",
 "level":"ERROR","source":"logfmt","data":{"message":"charge failed",
 "adapter":"logfmt","order":"4471"}, ...}
```

The AI never has to know that one program logs JSON, another logs Python's
`asctime LEVEL name: msg`, and a third logs `key=value`. It reads one schema.

### 2b. The OUTPUT side — bridging ANY program with zero code changes

Reading logs presumes the program *writes* logs AgentVision can find. The output
side (`python_backend/emitters.py`, `installer.py`, `cli.py`) guarantees that,
too. On first `agentvision attach <project>`, AgentVision detects the project's
language and **scaffolds a self-contained `agentvision/` folder inside the target
project** holding the sink files (`actions.jsonl` + `log.txt`) and a
language-appropriate **emitter** that makes the program write there — with zero
edits to the target's source. The emitter mechanism is honest about *how* each
language is hooked:

| `kind`          | Mechanism                                                    | Languages |
|-----------------|--------------------------------------------------------------|-----------|
| `autoload`      | Interpreter loads the emitter on a normal launch, no env var | Python (`sitecustomize.py`) |
| `env_autoload`  | In-process, one env var set automatically by `agentvision run` | Node (`NODE_OPTIONS=--require`), Ruby (`RUBYOPT=-r`) |
| `config`        | Drop-in logger config routing output to the sink            | Java (logback/log4j2), .NET (Serilog) |
| `tee`           | Launch via `agentvision run -- <cmd>`; stdout/stderr teed + normalized | Go, Rust, C/C++, anything else |

`agentvision run -- <cmd>` is the universal front door: it injects the right env
for autoload/env-autoload languages **and** tees stdout/stderr for everything
else, so a single command bridges any program. For a **compiled GUI** with no
useful stdout, the log side is complemented by the screenshot side (next
section) plus the optional system-wide input daemon.

---

## 3. The screenshot aspect (deep)

This is where AgentVision gives the AI *eyes*. The capture engine lives in
`python_backend/api/bridge_server.py` (the `AutoCaptureEngine` and `_take_frame`)
with all OS-specific work isolated in `python_backend/utils/platform_shim.py`.

### 3a. What gets captured: window vs region vs full-screen

Every frame targets the screenshot with a strict priority order
(`_take_frame`, `platform_shim.capture_frame`):

**The promise: a frame contains ONLY the bridged program, and nothing else.**

1. **Capture by window ID** — the target program's window. On **macOS**
   (`screencapture -l<wid>`) this reads the window's *backing store*, so it
   grabs the window regardless of size, position, overlap, or fullscreen — and
   **measured on macOS 15, even when the window is fully minimized to the Dock**
   (a minimized window captured its real content, identical to the un-minimized
   capture). On **Windows/Linux (X11)** the window path is a **region grab** of
   the window's bounds via `mss`, so it is occlusion-sensitive and cannot see a
   minimized window; Wayland forbids per-window capture entirely.
2. **Custom crop rect** — a manual `x,y,w,h` override for a fixed sub-region.
3. **Full-screen** — used **only when no `capture_app` was named**, because then
   the whole screen *is* the requested target.

**When a `capture_app` is named but has no window, the frame is SKIPPED.** The
desktop is never captured as a substitute. This used to warn and then screenshot
the full screen anyway, which wrote 2560×1440 frames of the user's entire desktop
— every other app and browser tab included — to disk and handed them to the
agent, at roughly 18× the bytes of a real window frame. Two such frames were
found in a real 11,000-frame capture. A frame of the wrong thing is worse than no
frame: it leaks unrelated screen contents, pollutes visual-change detection, and
makes the agent reason about pixels that are not the program.

So a **gap in frame sequence numbers is expected and explained**, not a failure:
`capture/status.health` reports `frames_skipped_no_window` alongside
`window_missing` and a `last_warning` naming the cause, and push mode says
"frames are being SKIPPED (the desktop is never captured)". Set
`AGENTVISION_ALLOW_FULLSCREEN_FALLBACK=1` to restore the old behaviour.

### 3b. Shots-per-second control — and why the AI must ask the user

Capture cadence is measured in **seconds per shot** (`interval`) and is
**user-configurable**. From `capture_rate_info()` and the environment defaults in
`bridge_server.py`:

- `CAPTURE_MIN_INTERVAL = 0.1s` → **10 shots/sec** (fastest advertised)
- `CAPTURE_MAX_INTERVAL = 10.0s` → **0.1 shots/sec** (slowest commonly useful)
- Supported range presented to the AI: **0.1–10 shots/sec** (interval 0.1s–10s)
- The desktop GUI slider exposes a friendlier sub-range (0.5–10 shots/sec).

The engine is built so the AI **asks the user for the rate at the start and
continuation of every project**. That guidance is not a suggestion buried in a
comment — it is returned in-band on `av_capture_status`, `av_capture_start`,
`av_capture_set_interval`, and `av_overview`, so the model reads it every time it
touches capture. The verbatim guidance string from `capture_rate_info()`:

> "Screenshot cadence is user-configurable. At the START or CONTINUATION of
> EVERY project, ASK THE USER how many screenshots per second they want, and
> present the full supported range: 0.1–10.0 shots/sec (interval 0.1s–10.0s).
> Faster = more shots/sec = finer visual detail but more frames to review;
> slower = fewer shots. Then apply it with av_capture_set_interval(interval=1/fps)
> — e.g. 4 shots/sec = interval 0.25. Every frame is time-aligned to the logs, so
> pick a rate that matches how fast the thing you're debugging changes."

The reasoning: the *right* rate is a trade-off only the user and the bug can
settle. A fast animation, a race, or a crash wants many shots/sec; a long idle
wait wants few. Because every frame is time-aligned to the logs, the rate is
purely about visual resolution and review cost — so the model surfaces the
choice rather than guessing.

### 3c. Self-describing frames

Each screenshot is written as `frame_NNNNN.png` beside a `frame_NNNNN_frame.json`
sidecar (the `SnapshotFrame`, schema in `shared/schema/snapshot_schema.py`,
`SCHEMA_VERSION = "2.0.0"`). The MCP frame tools additionally attach an `_ai`
block that tells the model, *in the response itself*: look at the image at this
path; here is the shutter timestamp; here is the exact time-aligned log window;
here are the precise follow-up calls to correlate this image with logs. The frame
is designed to be actionable without a second round-trip.

### 3d. Blank / health detection

A screenshot can be black — the window was minimized, occluded, not yet painted,
or capture-blocked (a locked screen, a headless session). `platform_shim.image_health()`
is a cheap blank detector: it downscales to 64×64 grayscale and computes mean and
stddev; `is_blank` is `True` when `stddev < 2.0` (near-uniform pixels) or
`mean < 3.0` (classic all-black grab). When a frame is blank, the bridge
increments `blank_frame_count`, sets a loud `last_warning`, and stamps
`black_frame: true` into `capture_meta` — and the frame tools raise an `_ai.WARNING`
so the AI **does not hallucinate visual content from a black image**.

### 3e. Cross-platform capture backends

`capture_backend_name()` reports which backend produced the pixels, so the AI
knows whether window-capture survives occlusion:

- **macOS** → `screencapture -l<wid>` — true window capture. Survives overlap,
  offscreen position, fullscreen, and (measured) full minimization to the Dock.
  This is the only platform that fully delivers the only-the-program promise.
- **Windows** → `mss` region grab of the window bounds; occlusion-sensitive and
  blind to minimized windows. A frame is skipped rather than substituted.
- **Linux** → session-aware: `mss(x11)`, `grim(wayland)`, `portal(wayland)`,
  or `none(headless)`. X11 is a region grab like Windows. On Wayland, global
  window enumeration is impossible by compositor design, so `find_window`
  returns `None` — which now means frames are **skipped** for a named
  `capture_app` rather than falling back to a desktop grab. The self-test
  reports Wayland per-window capture as *not-applicable*, not a failure.

Consequence worth stating plainly: **only macOS currently satisfies "always the
program, even minimized"**. On Windows and X11 an occluded or minimized window
yields no frame rather than a wrong one — correct, but a coverage gap. Closing it
would need per-platform compositor APIs (Windows `PrintWindow` with
`PW_RENDERFULLCONTENT`, or capturing the app's own render output).

---

## 4. Time-alignment (deep) — the core value

Eyes and logs are only useful together if the AI can trust that *this
screenshot* lines up with *those log lines*. AgentVision makes that alignment
exact and *provable*.

### 4a. One clock for everything

Every timestamp emitted anywhere in the system comes through
`shared/clock.py`. It returns two views of the *same* instant and always emits
both:

- `iso` — `"YYYY-MM-DDTHH:MM:SS.mmmZ"`, human-grep-friendly UTC
- `ms` — float epoch milliseconds, machine-comparable and sortable

Screenshots, log lines, JSONL records, and frame metadata all stamp through this
one clock, which is what lets N heterogeneous logs *and* the frames share a
single monotonic UTC-ms timeline.

### 4b. The shutter: timestamp and log offsets snapshotted together

The heart of the design is in `AutoCaptureEngine._take_frame`. Immediately
**before** the shutter, the engine stamps the time **and** snapshots the current
byte sizes of the log files **in the same instant**:

```python
# ── Shutter: stamp time AND snapshot log offsets together ───────────
capture_iso, capture_ms = clock.now()
offsets = {
    "action_log_offset": _safe_size(action_log_path),   # bytes, as of shutter
    "log_offset":        _safe_size(log_path),           # bytes, as of shutter
}
platform_shim.capture_frame(str(image_path), wid=wid, crop=crop)
capture_end_ms = clock.now_ms()                          # pixels finished
```

Why this order matters: capturing pixels can take ~100 ms. If the frame's log
context were read *after* the grab, a line that arrived *during* the capture —
with a `ts_ms` greater than the frame's own timestamp — could sneak in and
mislead the AI. By pinning the byte offsets at the shutter, **the log window for
this frame is bounded to bytes `<= *_offset` as of `shutter_ms`**, so no record
stamped after the shutter can leak into this frame's context. The image itself is
valid across `[shutter_ms, capture_end_ms]`, and `capture_latency_ms` tells the
AI exactly how wide that interval is.

### 4c. `capture_meta` — the alignment contract, per frame

Every frame carries a `capture_meta` block recording everything the AI needs to
*trust* the correlation:

```jsonc
"capture_meta": {
  "shutter_ms":         1784628000123.0,   // when the timestamp was stamped
  "capture_end_ms":     1784628000221.0,   // when the pixels finished
  "capture_latency_ms": 98.0,              // width of the valid interval
  "action_log_offset":  20481,             // byte cutoff, snapshotted at shutter
  "log_offset":         88123,             // same for the primary text log
  "window_found":       true,
  "capture_target":     "window",          // window | crop | fullscreen
  "black_frame":        false,
  "image_health":       { "mean": 61.4, "stddev": 40.2, "is_blank": false },
  "capture_backend":    "screencapture",
  "rate":               { "interval_s": 0.25, "shots_per_second": 4.0 },
  "note": "image is valid across [shutter_ms, capture_end_ms]; logs are bounded
           to bytes <= *_offset as of shutter_ms, so no record with
           ts_ms > shutter_ms leaks into this frame's context."
}
```

The frame also pins `profile_action_log` (the path active at capture time) so the
frame stays correct even if the profile later switches or files grow.

### 4d. The tools that exploit the alignment

Because alignment is captured per-frame, the MCP surface can turn it into direct
answers:

- **`av_actions_around_frame(seq, window_secs)`** — the core correlation move:
  return every structured action record within ±`window_secs` of frame N's
  shutter. *See something in the image → learn what the program logged at that
  instant.*
- **`av_log_normalized(from_ms, to_ms, level, label)`** — the merged,
  format-normalized view across **all** of the program's logs at once for a time
  window. Pass a tight window around a frame's `shutter_ms` to see exactly what
  *every* log (any language) said at that screenshot.
- **`av_state_at(at_ms)`** — the program's wide-event state (e.g.
  position/target/run_id, or any domain state) nearest a given epoch ms.
- **`av_frame_alignment(seq)`** — the *proof*. It recomputes, from the frame's own
  `capture_meta`, whether every log record bounded into that frame truly predates
  the shutter, and returns `aligned: true` with `leaked_after_shutter: 0` for a
  healthy frame (allowing a small 50 ms clock-skew grace). This is the button the
  AI presses when it must fully trust "the log shown lines up with the
  screenshot."

### 4e. Why the merge makes this powerful

Because the multi-source merge (Section 2) already puts N logs of different
formats on the *same* UTC-ms timeline, and the frames stamp through the *same*
clock, a single `av_log_normalized(from_ms=shutter−2000, to_ms=shutter+2000)`
call returns a unified, time-ordered slice of **the JSONL stream, the plain-text
app log, and the framework log in a third format**, all interleaved around one
screenshot. That is the whole point: one image, one moment, every log, one
schema.

```
        logs (any language/format)                    screenshots
   ─────────────────────────────────────         ────────────────────
   jsonl ─┐                                        frame_00042.png
   text  ─┼──►  adapters + structural  ──►  ┌───────────────────────────┐
   java  ─┤        (one schema)             │   single UTC-ms timeline  │
   go    ─┘                                 │  shutter_ms ── offsets ──  │
   binary ──►  source readers ──► jsonl ──► └───────────────────────────┘
                                                 ▲            ▲
                              av_log_normalized(window)   av_actions_around_frame
```

---

## 5. Built for an AI assistant: token-conserving yet more productive

An average debug log optimizes for a human tailing a terminal. AgentVision
optimizes for a model with a token budget: **everything is JSON, everything is
schema-documented, and the highest-signal facts are pre-computed** so the model
spends tokens on reasoning, not parsing.

### 5a. Triage-first: read the digest before anything else

`av_digest` (`GET /digest`) is the **first** tool a model should call. It returns
one compact, ranked JSON — deliberately token-light — that says *what to look at
and in what order*:

- an **`attention`** list, worked top-down, each item naming the drill-in tool
  ("Program is NOT running — see latest_frame + av_program_log", "3 NEW error
  fingerprint(s) this session", "Screen appears STUCK — compare recent frames");
- the latest frame's **`summary` / `recommended_next` / `tags` / `confidence`**;
- **top recurring errors deduped by fingerprint**, with occurrence counts;
- **brand-new error fingerprints this session** (the just-appeared bugs);
- capture health + shots/sec, and image↔log alignment health.

The instruction to the model is explicit: *prefer this over calling six status
tools.* One round-trip replaces many.

### 5b. Per-frame AI triage layer

The `SnapshotFrame` schema (`shared/schema/snapshot_schema.py`, version `2.0.0`)
leads with fields a model can act on *before* drilling into detail:

- `summary` — one-line natural-language description of the moment
- `recommended_next` — the single best next action
- `tags` — e.g. `["error","stuck"]`
- `confidence` — 0 (broken) .. 1 (healthy)
- `state_delta` — key-level diff of program state vs the previous frame
- `correlation` — `{trace_ids, run_ids}` handles to pull the full story
- `perf` — target-process CPU/RSS/threads at capture time
- plus `capture_meta`, `anomaly`, and the structured `error`

### 5c. Structured, deduplicated errors

`ErrorInfo` is a **structured** error parsed once from the raw traceback so the AI
never re-parses text, and it works across languages (Python, Node, Java, Go,
Ruby, .NET, …) via `modules.diagnostics.parse_exception`. It carries:

- `exception_type`, `language`, `message`
- `frames: [{file, line, func}]` — the parsed stack
- `probable_cause` — a heuristic root-cause hypothesis
- `fingerprint` — a stable id used to **dedup across occurrences**
- `occurrence_count`, `first_seen`, `last_seen`

So instead of the same traceback appearing 400 times in the log (400× the
tokens), the model sees it **once**, with a count of 400 and a probable cause.
`av_errors_by_fingerprint` and `av_new_errors_this_session` build directly on
this.

### 5d. Bounded, paginated, correlation-friendly

Every tool returns clean self-describing JSON with capped/paginated outputs
(`lines`, `limit`, `window_secs`, time windows), so a model never accidentally
pulls megabytes. `trace_id`/`run_id` correlation lets it reason end-to-end about a
single logical action span (`av_trace_timeline`), and the `structural` normalizer
guarantees even unknown formats arrive as compact JSON rather than raw noise.

### 5e. Contrast with an average debug log

| Average debug log | AgentVision |
|---|---|
| Free-form text, per-language format | One JSON schema across all languages |
| Errors repeat verbatim N times | Fingerprinted once + `occurrence_count` |
| "Figure out the root cause yourself" | `probable_cause` + parsed `frames` |
| No notion of "what changed" | `state_delta` / `delta_from_prev` per frame |
| Screenshots (if any) unlinked to logs | Frames pinned to logs at the shutter |
| Read it all to find the problem | `av_digest` ranks the problem first |
| More text = more signal (assumed) | Less text, higher signal-per-token |

---

## 6. Architecture & platforms

AgentVision is a **three-layer bridge**:

```
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER 3 — Claude (the AI assistant)                                  │
│    reads JSON frames + normalized events, calls av_* tools            │
└───────────────────────────▲──────────────────────────────────────────┘
                            │  MCP (stdio)  — 94 av_* tools
┌───────────────────────────┴──────────────────────────────────────────┐
│  LAYER 2 — MCP server  (python_backend/api/claude_mcp.py)             │
│    FastMCP("agentvision"); proxies HTTP to the running bridge         │
└───────────────────────────▲──────────────────────────────────────────┘
                            │  HTTP (127.0.0.1:7771)
┌───────────────────────────┴──────────────────────────────────────────┐
│  LAYER 1 — Bridge server  (python_backend/api/bridge_server.py)      │
│    capture engine · adapters · multi-source merge · digest · selftest │
│    ── platform_shim.py isolates all macOS / Windows / Linux code ──   │
└───────────────────────────▲──────────────────────────────────────────┘
                            │  reads/writes
        ┌───────────────────┴───────────────────┐
        │  the bridged program (any language)    │
        │  agentvision/ sink · emitter · window  │
        └────────────────────────────────────────┘
```

**Platforms.** One source base runs on **macOS** (primary), **Windows 10/11**,
and **Linux (developed/packaged for Artix; see `dist/linux/`)**. All OS-specific
behavior — screen capture backends, window enumeration, input hooks, temp paths,
fonts — is isolated behind `python_backend/utils/platform_shim.py`.

**Input daemon.** `python_backend/daemon/input_daemon.py` is an optional
long-running recorder that taps the OS at the lowest input layer and routes
keyboard/mouse events into the active project's JSONL sink. By default it records
only **synthetic** events (what a bot/RPA loop injects), dropping physical
human input, distinguishing them per-OS: macOS CGEventTap (`source PID == 0` ⇒
physical), Windows low-level hooks (`LLKHF_INJECTED`/`LLMHF_INJECTED`), Linux
evdev (real HID device vs `uinput` virtual device). The per-profile
`capture_user_input` flag flips it on when the *human* is the agent.

**Self-test.** `av_selftest` (`GET /selftest`) proves the runtime paths work *on
the target machine*: capture is non-blank, window enumeration works, OS input
hooks actually fire, and the daemon is healthy — each check reported as
`ok: true | false | null` (null = not applicable on this OS).

**94 MCP tools** in 20 groups. 49 of them, grouped:

- **Orientation / triage:** `av_digest`, `av_overview`, `av_status`,
  `av_selftest`
- **Frames (eyes):** `av_latest_frame`, `av_get_frame`, `av_frame_alignment`,
  `av_frame_overlay`, `av_frame_annotate`, `av_frame_annotations`
- **Capture control:** `av_capture_status`, `av_capture_start`,
  `av_capture_stop`, `av_capture_set_interval`
- **Logs & time-alignment:** `av_log_sources`, `av_log_normalized`,
  `av_log_range`, `av_actions_around_frame`, `av_state_at`, `av_trace_timeline`,
  `av_log_push`, `av_program_log`, `av_program_stats`, `av_debug_log`
- **Errors & anomalies:** `av_errors_by_fingerprint`,
  `av_new_errors_this_session`, `av_events_schema`, `av_list_bookmarks`,
  `av_get_bookmark`, `av_bookmark_outliers`
- **Source mirror:** `av_source_refresh`, `av_source_light`, `av_source_tree`,
  `av_source_digest`, `av_source_file`, `av_source_search`, `av_source_list`,
  `av_codebase_map`
- **Program & tests:** `av_program_status`, `av_program_crop`, `av_run_tests`
- **Profiles & install:** `av_list_profiles`, `av_active_profile`,
  `av_create_profile`, `av_set_active_profile`, `av_delete_profile`,
  `av_install_project`, `av_install_verify`, `av_daemon_status`

---

## 7. Quick start

1. **Install dependencies and start the bridge** — follow the setup guide for
   your OS:
   - macOS: [`SETUP.md`](../SETUP.md)
   - Windows: [`SETUP-Windows.md`](../SETUP-Windows.md)
   - Linux (Artix): [`dist/linux/README.md`](../dist/linux/README.md)
2. **Bridge any program** (zero code changes to the target):
   ```
   agentvision attach <project_dir>        # scaffold agentvision/ + emitter
   agentvision run -- <your program>       # inject hooks + tee, then run
   ```
3. **Register the MCP server** with Claude Code so the `av_*` tools appear:
   ```
   claude mcp add agentvision -- python /path/to/AgentVision/python_backend/api/claude_mcp.py
   ```
4. **In the AI session:** call `av_digest` first to orient, **ask the user for a
   shots-per-second rate**, set it with `av_capture_set_interval`, then use
   `av_latest_frame` + `av_actions_around_frame` / `av_log_normalized` to read
   the program's screen and logs together on one timeline.

---

*This document reflects AgentVision v5. Cross-references:
`python_backend/connectors/log_adapters.py` (adapters + coverage ledger),
`python_backend/connectors/log_sources.py` + `readers.py` (merge + binary
readers), `python_backend/api/bridge_server.py` (capture engine + alignment),
`python_backend/api/claude_mcp.py` (MCP tool surface),
`shared/schema/snapshot_schema.py` + `docs/SCHEMA.md` (frame schema),
`shared/clock.py` (the single clock).*

---

## What AgentVision is NOT: the inverse of "agent observability"

Every existing AI-agent observability product — AgentOps, Langfuse, Braintrust,
Arize, Honeycomb Agent Timeline — instruments **the AI agent itself**: LLM calls,
traces, token spend, reasoning chains.

**AgentVision is the inverse. It instruments the program being debugged, for the
benefit of the agent.** We found no direct competitor doing that with time-aligned
visual + log capture.

The practical consequence: what is worth borrowing from that space is the
**mechanisms** — flight recorder, session replay, span/trace correlation, turning a
failure into a reproducible case — not the product shape. Those mechanisms are why
v5.1 has `av_incidents` and `av_replay`.

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

**The flight recorder:** capture runs continuously, and the instant a failure
signature appears (structured error, screen freeze, blank screen, on-screen error
text) the window *before* it plus a short tail is frozen and exempt from eviction.
The crucial moment is saved before the agent asks — `av_incidents`, `av_replay`.

**Examine before delete.** A screenshot nobody hears about is a shot taken for
nothing, so retention is not a timer.

The recorder originally deleted every frame older than 60 seconds. That is shorter
than a single agent reasoning turn: AgentVision could announce "error at frame 4213",
the agent would think for two minutes, call `av_error_moment(4213)` — and the pixels
were already gone. Worse, the pruner deleted a frame's PNG and sidecar but left its
`_annotated.png` and `.diff` siblings behind; once the sidecar was gone nothing could
even see those files again, and one project accumulated **4.63 GB** of orphans that
way while the recorder believed it was managing 44 frames.

Retention now works like this:

* **Disk is bounded by bytes, not seconds** — 5 GB by default
  (`AGENTVISION_DISK_BUDGET`), with a free-disk floor underneath it. Nothing is
  deleted while there is headroom, so a frame can stay available for hours.
* **A flagged frame is held until it has been examined.** Reading it at any tier
  counts — `av_frame_json` is a legitimate look, since the whole token ladder exists
  to make pixels the last resort rather than the proof.
* **AgentVision pushes the frames at the agent** rather than waiting to be asked.
  The awaiting batch is named in the ambient injection, with the cheapest sufficient
  call, and it holds a *reserved slot*: every other push signal will still be true
  next turn, but this one has a deadline, so it can never be crowded out.
* **Not every frame needs eyes.** `AGENTVISION_EXAMINE` selects `errors` (default —
  only frames time-aligned with a failure), `changes`, `all` (video-like continuity,
  for when motion itself is the evidence), or `off`. Pair `all` with a slower capture
  interval: an agent does not need 24 fps to perceive motion, and the interval is the
  real cost lever.
* **Eviction spends the cheapest frames first** and refuses to spend frozen evidence
  or frames still awaiting eyes to satisfy a tight budget — it says so instead.
* **History is permanent, pixels are not.** Every frame keeps its JSON descriptor and
  a thumbnail written at capture time (~8 KB against a ~1.9 MB frame), so a day of
  capture stays searchable and visually reviewable for a few MB. Interesting frames
  are compressed into an archive on the way out.
* **Losses are reported, never hidden.** If a flagged frame ever expires unexamined
  it is counted in `integrity.dropped_unexamined` and `integrity.ok` goes false.
  Silent truncation would read as "the agent saw everything" — the one lie this
  system must not tell.

`av_retention` · `av_frames_awaiting` · `av_examine_ack`.

**Visual failure detection:** a hang leaves no log line. `av_visual_events` detects
`screen_frozen` from the screen itself, which log-only analysis cannot.

**Honesty:** `av_token_report` reports measured comparisons and states its
estimation method; `av_ui_tree` reports when a tree would cost *more* than the
screenshot it replaces. See `docs/RESEARCH_TOKEN_EFFICIENCY.md` for the published
basis and our own measurements, and `docs/AGENT_INSTRUCTIONS.md` for a paste-ready
block for a project's `CLAUDE.md`.
