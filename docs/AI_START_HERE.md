# AI_START_HERE.md — read this before you call anything

You are an AI model. Something connected you to a tool called **AgentVision**.
This file tells you everything you need. You do not need any other file to start.

**If you read only four lines, read these:**

1. Call `av_start_here()` first. Do what its `DO_THIS_NEXT` field says.
2. On a program AgentVision has never seen, **you** must design and commit a
   logging plan before capture will start. This is called *building the bridge*.
   It is 3 calls. It happens once per program, ever.
3. A refusal comes back as **HTTP 200**. Read the body, not the status code.
4. Never take your own screenshots and never grep raw logs. Ask a tool instead.

---

## 1. What AgentVision is

AgentVision is a local debug flight recorder for **one** program at a time (the
"target"): it screenshots that program on a timer, parses every log the program
declares, and time-aligns the screenshots and the log lines onto one timeline.
All of that work runs on the user's own CPU, continuously, and costs you **zero
tokens** — the screenshots are already taken, the logs are already parsed, the
timestamps are already correlated. **Your job is to ASK it questions**, not to
take screenshots, not to run `grep`, not to page through images, and not to
match a timestamp to a frame by hand.

Spend your tokens on writing code. Spend AgentVision's free CPU on observing.

---

## 2. THE ONE THING THAT WILL CONFUSE YOU

**AgentVision does not decide what logging to build into a program. YOU do.**

This is backwards from every other tool you have used. Most tools inspect a
project, guess, and install something. AgentVision refuses to guess.

Why it refuses: AgentVision owns 658 log-format adapters, 9 binary log readers,
94 MCP tools, and a per-language library of "emitters" (small hooks that make a
silent program start talking). Which of those a program needs depends on what
the code **is** and what it **does** — and nothing in a directory listing tells
you that. The old behaviour sniffed the language and scaffolded a fixed set, so
it installed **the same logging for a web server and for a GPU emulator**, and
reported success either way. A wrong bridge was indistinguishable from a right
one. That is the failure this design removes.

So on first connection to a program:

- The bridge state is `PROVISIONAL`.
- `av_capture_start()` and `av_install_project()` are **REFUSED**.
- You read a catalog of options, you decide, you commit a plan.
- AgentVision then builds exactly what you named, and nothing else.

**The refusal is HTTP 200.** A model that checks only the status code will think
it succeeded and will then wonder why there are no frames. The refusal body
always contains these fields:

```json
{
  "ok": false,
  "started": false,
  "bridge_required": true,
  "guidance": "FIRST CONNECTION to this program - the bridge is NOT built yet ...",
  "bridge": {"state": "PROVISIONAL", "sealed": false, "next": "av_bridge_catalog()"}
}
```

Check `started` and `bridge_required`. (Newer builds also add
`"error": "BRIDGE_NOT_BUILT"` and `"DO_THIS_NEXT"`; do not depend on those two
being present, but obey them if they are.)

---

## 3. Sixty-second quickstart — the literal first three calls

### Call 0 (always): `av_start_here()`

Returns your orientation. Look at exactly three things:

| Field | Why you care |
|---|---|
| `watching_now.program` / `.project_root` / `.language` | Confirm this is the program you were asked to debug. |
| `state.bridge_build.state` | `PROVISIONAL` = you must build the bridge. `BUILT` = skip to section 6. |
| `DO_THIS_NEXT` | Your literal next call. It already accounts for BUILT vs PROVISIONAL. |

Abridged real response on an unbuilt program:

```json
{
  "watching_now": {"profile": "naivetest", "program": "NaiveTest",
                   "language": "node",
                   "project_root": "/tmp/scratchpad/naive_prog",
                   "running": false},
  "state": {"bridge_build": {"state": "PROVISIONAL", "ok": false,
                             "do_this_now": "av_bridge_catalog()  ->  av_bridge_commit(plan=...)"},
            "capturing_now": false, "shots_per_second": 1.0},
  "DO_THIS_NEXT": "av_bridge_catalog()  ->  av_bridge_commit(plan=...)"
}
```

### Call 1: `av_bridge_status()`

Confirms whether this program is already built. Abridged real response:

```json
{
  "state": "PROVISIONAL",
  "sealed": false,
  "program": "NaiveTest",
  "plan": null,
  "blocked": ["capture/start", "install (emitters)"],
  "next": "av_bridge_catalog()"
}
```

Look at `state`. **If `state` is `BUILT`, stop here** — setup already happened,
go to section 6. If `PROVISIONAL`, continue.

### Call 2: `av_bridge_catalog()`

This is the menu **plus** evidence about the target's own source code. It is the
biggest response you will read in this flow. Read these fields:

| Field | What it gives you |
|---|---|
| `emitters_available` | Every emitter you may pick, each with `captures`, `misses`, `cost`, `good_for`, `languages`, `builds_as`. Read `misses` — it tells you what a choice will be blind to. |
| `code_evidence.primary_language` | The language actually detected by scanning files. |
| `code_evidence.signals` | The load-bearing part. Per signal: `count`, `files`, `means`, `argues_for`. Each selection you make must answer to one of these. |
| `mcp_tool_groups` | The 94 tools in 20 groups. Each tool carries `summary`, `needs`, `cost`, `verdict` (`core` / `useful` / `n/a`), `verdict_reason`, and sometimes `caveat` (a known defect — trust it over the tool's docstring). |
| `adapters` | 658 log **parsers**, grouped by family, with counts. Drill in with `av_list_adapters(family=..., q=...)`. |
| `existing_logs_found` | Every log this program has. `declared: true` = already read. `declared: false` = **sitting in the project and not read yet**, with `covered` / `detected_adapter` grading whether any parser understands its format, and a copy-paste adapter recipe when none does. An undeclared log is only read once you pin its label in `plan.adapters` — that is the whole answer to "this program already logs, use that instead of installing an emitter". |
| `capture_settings` | Frame-rate envelope, 0.1–10 seconds per shot. **Ask the user**; do not assume. |
| `catalog_token` | A 16-hex string. You must copy it into your plan verbatim. |

**The distinction that decides most plans:** adapters *parse* logs that already
exist; emitters *create* logs that do not. A program with no logging needs an
emitter first — pinning an adapter to a file nothing writes gets you nothing.

### Call 3: `av_bridge_commit(plan={...})`

Your decision. Builds the bridge. Full worked example next.

---

## 4. Complete worked first bridge — a Python web service

Target: a small Flask service at `/Users/dev/orders-api`, headless, no window.

### 4a. What the catalog told me

`emitters_available` for a Python target contains exactly these five ids:

| id | captures | misses (why it is not enough alone) |
|---|---|---|
| `stdout_tee` | stdout + stderr, line by line | anything the program never prints: a silent early exit, a swallowed exception, a hang with no output |
| `lifecycle` | process start/exit, argv, pid, exit code | everything between start and exit |
| `uncaught_exceptions` | uncaught exceptions, thread exceptions, shutdown errors | every exception the program *catches* |
| `logging_bridge` | the `logging` framework, mapped to levels | bare `print()` output, and anything logged before the bridge loads |
| `swallowed_exceptions` | exceptions the program catches and hides (`except: pass`) | non-exception failure; also silent on Python < 3.12 (needs `sys.monitoring`) |

`code_evidence` for this target (example values; the field names are exactly
what the catalog returns):

```json
{
  "primary_language": "python",
  "scanned_files": 34,
  "signals": {
    "web_service":      {"count": 6,  "files": ["app.py"],
                         "means": "it is a service - likely headless",
                         "argues_for": "visual_capture: false; prefer log/structured hooks"},
    "existing_logging": {"count": 41, "files": ["app.py", "orders/db.py"],
                         "means": "the program ALREADY logs",
                         "argues_for": "logging_bridge (route what exists)"},
    "discards_error":   {"count": 9,  "files": ["orders/db.py", "orders/pay.py"],
                         "means": "handlers whose body throws the error away",
                         "argues_for": "swallowed_exceptions - the strongest case for it"},
    "threads":          {"count": 3,  "files": ["worker.py"],
                         "means": "a crash on a worker never reaches the main excepthook",
                         "argues_for": "uncaught_exceptions (its thread hook)"}
  }
}
```

### 4b. The reasoning, made visible

This is the part that stops you selecting things at random. One line per
decision, each pointing at a signal:

- `logging_bridge` — **take it.** `existing_logging` count 41. The service
  already logs properly; routing what exists is the cheapest real coverage.
- `swallowed_exceptions` — **take it.** `discards_error` count 9 in the DB and
  payment paths. Those failures return `None` and never crash, so no other
  emitter can see them. This is the documented strongest case for it.
- `uncaught_exceptions` — **take it.** `threads` count 3 in `worker.py`. A
  worker-thread crash never reaches the main excepthook, and `logging_bridge`
  only sees what the code chose to log.
- `stdout_tee` — **skip.** `prints_only` did not appear at all; there is no bare
  `print()` traffic worth teeing, and `logging_bridge` already covers the
  framework.
- `lifecycle` — **skip.** A long-running service's start/exit boundaries are
  already in its own request log; this would add nothing that answers a signal.
- `visual_capture: false` — `web_service` present, no `gui_toolkit` signal.
  There is no window. Screenshots of nothing are pure waste.
- Tools: the visual and accessibility groups are `n/a` here because visual
  capture is off. The failure mode is a swallowed exception in a request path,
  so log and source tools earn their place.

Three emitters, each with a reason. Not five.

### 4c. The plan object — copy-pasteable

Replace `PASTE_catalog_token_FROM_STEP_2_HERE` with the exact `catalog_token`
string from **your** `av_bridge_catalog()` response. A wrong or missing token is
rejected; that is deliberate, it is what proves you read the options.

```json
{
  "catalog_token": "PASTE_catalog_token_FROM_STEP_2_HERE",
  "emitters": ["logging_bridge", "swallowed_exceptions", "uncaught_exceptions"],
  "why": {
    "logging_bridge": "existing_logging count 41 across app.py and orders/db.py - the service already logs properly, so routing its own logging output is the cheapest full coverage",
    "swallowed_exceptions": "discards_error count 9 in orders/db.py and orders/pay.py - those handlers return None and never crash, so no other emitter can see the failure at all",
    "uncaught_exceptions": "threads count 3 in worker.py - a crash on a worker thread never reaches the main excepthook, and the thread hook is the only thing that catches it"
  },
  "rationale": "Headless Flask order service that already logs well but discards errors in its DB and payment paths; the bridge targets the invisible-failure case, not screen output.",
  "adapters": {"text": "auto", "events": "jsonl"},
  "capture": {"interval_seconds": 1.0},
  "visual_capture": false,
  "tools": {
    "primary": ["av_diagnose", "av_log_raw", "av_search", "av_source_at_error",
                "av_log_normalized", "av_errors_by_fingerprint", "av_log_where",
                "av_incidents", "av_session_report"],
    "not_relevant": {
      "av_ui_tree": "headless service, no window and no accessibility tree",
      "av_ui_diff": "same as av_ui_tree - nothing structured to diff",
      "av_ocr_frame": "no window is captured, so there are no pixels to OCR",
      "av_read_screen": "no window; the log is the only text source here",
      "av_visual_changes": "visual capture is off for this program by plan",
      "av_frame_region": "no frames exist when visual capture is off",
      "av_program_stats": "its parser is a fixed game-bot key whitelist, unrelated to this service"
    },
    "note": "Reviewed all 19 groups. Failure mode is a swallowed exception inside a request path, so the log-to-source jump matters and every pixel tool is dead weight."
  }
}
```

Then call:

```
av_bridge_commit(plan=<the object above>)
```

Note: `interval_seconds: 1.0` above is a placeholder you should have **asked the
user** about first (supported range 0.1–10 seconds per shot). With
`visual_capture: false` it barely matters; on a GUI target it matters a lot.

### 4d. What a success response looks like

HTTP 200, abridged:

```json
{
  "ok": true,
  "sealed": true,
  "note": "Bridge built. The gate will not fire again for this program - later connections proceed immediately.",
  "next": "av_capture_start()",
  "plan": {"version": 1, "sealed": true, "sealed_at": "2026-07-29T19:03:38",
           "decided_by": "agent",
           "emitters": ["logging_bridge", "swallowed_exceptions", "uncaught_exceptions"],
           "visual_capture": false},
  "built": {"emitters_requested": ["logging_bridge", "swallowed_exceptions",
                                   "uncaught_exceptions"],
            "actions": ["scaffolded emitters into /Users/dev/orders-api",
                        "registered log sources: events -> jsonl, text -> auto",
                        "preflight marker written - the plan supersedes the coverage-only check"],
            "install": {"language": "python", "emitter": {"kind": "autoload"}}}
}
```

Check `ok: true` **and** `sealed: true`. If instead you get HTTP 400 with
`{"ok": false, "errors": [...], "catalog_token_expected": "..."}`, read `errors`
— each one names exactly what to fix — then commit again. Rejection is cheap and
carries no penalty.

### 4e. Two honest limits of what just happened

**Limit 1 — the emitter list is recorded, not enumerated at build time.** Your
`emitters` list is persisted verbatim for audit, and `built.emitters_requested`
echoes it, but the installer scaffolds **one** language-appropriate emitter
rather than one artifact per id (several ids are facets of the same emitter —
see each option's `builds_as`). Verified example: a plan naming
`run_wrapper` + `lifecycle` on a C project produced a single emitter recorded as
`{"language": "cpp", "kind": "tee"}`. `stdout_tee` in particular is offered for
every language and has no distinct implementation of its own. So treat your
selection as **the diagnosis you are committing to**, and verify what actually
landed with `av_install_verify()` (does the output side really emit?) and
`av_log_sources()` (which adapter resolves for each source) — do not assume three
ids means three artifacts. Adapters, unlike emitters, *are* honoured per source.

**Limit 2 — Python hooks need the right launch.** For a Python target the
installer writes a project-root `sitecustomize.py`, but CPython imports
`sitecustomize` **before** the script's own directory joins `sys.path`, so it is
never found for a plain `python app.py`. Measured: 0 events without
`PYTHONPATH`, 2 events with it. Tell the user to launch through the front door:

```
agentvision run -- python app.py
```

Same rule for compiled targets that chose `run_wrapper`: it only takes effect if
the program is actually launched through `agentvision run`.

---

## 5. IT ONLY HAPPENS ONCE

The plan is written to disk **inside the target project**:

```
<project_root>/agentvision/<profile_name>/.av_bridge_plan.json
```

(Verified on a real profile:
`~/projects/AbyssEngine/agentvision/abyss/.av_bridge_plan.json`.
When a profile has no `project_root`, it falls back to AgentVision's own
snapshots directory.)

| Event | Effect on the gate |
|---|---|
| Restart the target program | none |
| Restart the AgentVision bridge server | none |
| Restart AgentVision entirely | none |
| Your session ends; a new model connects | none |
| A different program is bridged | that program has its own gate, once |
| `av_bridge_commit(plan=..., replan=True)` | re-decides, deliberately |

**How to tell:** `av_bridge_status()` → `state` is `BUILT` and `sealed` is
`true`. `av_start_here()` shows the same thing under
`state.bridge_build.state`.

An older marker file `.av_preflight_ok` also counts as sealed (installs that
passed the previous coverage-only gate). In that case `bridge_status` sets
`sealed_by_legacy_marker: true` and `plan` may be `null` — the bridge works;
only the recorded reasoning is missing.

Calling `av_bridge_commit()` on an already-built program is a safe no-op: HTTP
200 with `{"ok": false, "already_sealed": true, "plan": {...}}`. It changes
nothing. Do not "fix" that by passing `replan=True` — see mistake 3.

---

## 6. Normal daily use, once the bridge is BUILT

Setup is done. Do not plan again. This is the whole working set:

| Call | Use it when |
|---|---|
| `av_start_here()` | Start of every session. Read `DO_THIS_NEXT`. |
| `av_capture_start()` | `capturing_now` is false and you need visual evidence. |
| `av_diagnose()` | Something is wrong. Ranked root-cause hypotheses with evidence and the next call to make. |
| `av_log_raw()` | You want what the program actually said, verbatim. **Always reach for this when a summary looks suspiciously clean.** |
| `av_visual_changes()` | Review a whole capture run without opening a single image. |
| `av_incidents()` | The seconds *before* each failure — already frozen on disk. Check this before asking the user to reproduce anything. |
| `av_error_moment(seq=1234)` or `av_error_moment(fingerprint="<fp>")` | One specific failure: error, frame, changed pixels, on-screen text, merged log window, state delta, source around each stack frame — one call. |
| `av_search(q="timeout")` | Find something across parsed logs and frame summaries. |
| `av_source_at_error()` | Jump from an error to the source lines around each stack frame. |
| `av_log_where()` | The program looks silent. This asks the OS which files the process really has open — a configured path is only a guess. |
| `av_log_sources()` | Which log sources this program declares, whether each file exists, and which adapter actually resolves for it. |
| `av_install_verify()` | Prove the output side really emits — after a build, before trusting silence. |
| `av_replay()` | Step through the recorded past without re-running anything. |
| `av_session_report()` | Wrap up or hand off. |

If you have HTTP but not MCP, the bridge server serves the same things (default
`http://127.0.0.1:7771`):

| Tool | Route |
|---|---|
| `av_start_here` | `GET /start_here` |
| `av_bridge_status` | `GET /bridge/status` |
| `av_bridge_catalog` | `GET /bridge/catalog` |
| `av_bridge_commit` | `POST /bridge/commit` with `{"plan": {...}, "replan": false}` |
| `av_capture_start` | `POST /capture/start` with `{"interval": 1.0}` |
| *(no MCP tool)* | `GET /bridge/report` — the full per-program record: emitters built, `why`, resolved adapter and staleness per log source, tools chosen and ruled out. HTTP only. |

---

## 7. The token rule — cheapest sufficient tier first

A full screenshot costs hundreds to thousands of visual tokens. At 10 shots per
second, roughly **99% of consecutive frames are pixel-identical**, so paging
through frames spends almost all of those tokens re-reading the same picture.
`av_visual_changes` collapses identical runs, which turns ten minutes of capture
into a few hundred tokens.

Escalate in this order, and only when the tier below is genuinely insufficient:

| Tier | Call | Cost | What you get |
|---|---|---|---|
| 1 | `av_visual_changes()`, `av_frame_json(seq)` | free–low | JSON only, no image bytes: change scores, changed bbox, structure, OCR text, aligned logs |
| 2 | `av_frame_json(seq, thumbnail=True)` | low | tier 1 plus a tiny base64 thumbnail |
| 3 | `av_frame_region(seq)` | medium | **only the pixels that changed** (default `bbox="changed"`) |
| 4 | `av_get_frame(seq)` | high | the full image — last resort |

Do escalate when the question truly needs pixels: colour, spatial layout,
rendering corruption and progress indicators are invisible in text, and refusing
to look is its own failure. Just never *start* at tier 4.
`av_token_report()` shows what the discipline actually saved.

### Tier 0 — resources, for things you may not need in the transcript

A tool result lands in your context whether or not you needed all of it. A
**resource** is a URI your client fetches only when something actually wants it.
Same data, same endpoints:

| URI | Instead of |
|---|---|
| `agentvision://catalog` | `av_bridge_catalog()` — ~200 KB, same `catalog_token` |
| `agentvision://digest` | `av_digest()` |
| `agentvision://frame/{seq}.json` | `av_frame_json(seq)` |
| `agentvision://frame/{seq}/region` | `av_frame_region(seq)` |
| `agentvision://incident/{id}` | `av_incidents(id=...)` |
| `agentvision://log/raw{?from_offset}` | `av_log_raw()` — the resource **peeks**, so it never moves your read position |

Reach for these when you want the artifact addressable rather than pasted.

---

## 7a. WHEN AGENTVISION CAN ASK THE USER, AND WHEN IT CANNOT

Some decisions are the user's: how many screenshots per second, and whether to
record their keystrokes. Where the MCP client supports it, AgentVision puts the
question to them directly — call `av_capture_start()` with **no** interval and
it asks.

Where the client cannot show a prompt, it falls back **and says so**. Every such
response carries a block with a `how` field:

```json
"capture_rate_choice": {
  "value": 1.0, "how": "unsupported", "chosen_by_user": false,
  "note": "capture rate: 1 shot(s)/sec (1s between shots) — AgentVision could
           not ask you, so this is its default, NOT your choice."
}
```

**Only `how: "asked"` means a human chose.** The other values —
`declined`, `cancelled`, `unsupported`, `no_context`, `failed` — all mean
AgentVision picked. If you see one of those, ask the user in prose yourself, and
never report the value back as though they had chosen it.

`av_start_here()` and `av_capabilities()` both return `your_client`, which tells
you up front whether asking is available in this client, and what AgentVision
does instead when it is not.

---

## 8. MISTAKES MODELS MAKE

Numbered so they can be cited. Each has the fix.

**1. Treating the HTTP-200 refusal as success.**
`av_capture_start()` returns 200 with `started: false`, `bridge_required: true`.
The model records "capture started", finds no frames, and blames the tool.
*Fix:* on every `av_capture_start()` / `av_install_project()` call, check
`started` (or `installed`) and `bridge_required` in the body. If
`bridge_required` is true, go build the bridge — section 3.

**2. Selecting every emitter.**
Picking all five "to be safe" is rejected at six or more, and even five is the
same blanket guess the gate exists to prevent — just with your name on it.
*Fix:* each id in `emitters` needs a `why` entry naming a real
`code_evidence.signals` entry, at least 15 characters. If you cannot write that
sentence, do not select the emitter.

**3. Re-planning a program that is already BUILT.**
The plan is on disk and survives every restart. A model that "re-initialises to
be sure", or passes `replan=True` after seeing `already_sealed`, overwrites a
working, audited bridge with a worse one.
*Fix:* `av_bridge_status()` first. `BUILT` means stop and use section 6.
`replan=True` is only for a deliberate re-decision the user asked for.

**4. Taking your own screenshots.**
Capture is already running on the user's CPU, and each of your screenshots costs
you real visual tokens for a frame AgentVision already has, unaligned to the
logs.
*Fix:* `av_visual_changes()`, then `av_frame_json(seq)`. Section 7.

**5. Grepping, tailing or reading raw log files with shell tools.**
The logs are already parsed, merged across every source, normalised to levels
and time-aligned to frames. `grep` throws all of that away — and worse, `tail`
on a path the process stopped writing to shows stale bytes forever without
complaining.
*Fix:* `av_search()`, `av_log_normalized()`, `av_log_raw()` for verbatim bytes,
`av_log_where()` when you suspect you are reading the wrong file.

**6. Reading frames one at a time in a loop.**
This is the single most expensive mistake available to you: ~99% of those frames
are identical.
*Fix:* `av_visual_changes()` once, then look only at the sequence numbers it
returns.

**7. Ignoring `DO_THIS_NEXT`.**
Every gate response, `av_start_here()` and `av_bridge_status()` all state the
exact next call. Models skip it, guess a tool, and get refused.
*Fix:* if a response contains `DO_THIS_NEXT` or `next`, that is your next call.

**8. Inventing or reusing a `catalog_token`.**
A missing token, a made-up token, or a token from a different program is
rejected with HTTP 400. The token is derived from the catalog's own contents;
this is the mechanism that makes "review the options first" impossible to skip.
*Fix:* call `av_bridge_catalog()` in this session and copy its `catalog_token`
verbatim. If the options change while you think, the next commit tells you to
re-read the catalog.

**9. Sending `tools` in the wrong shape.**
`tools` must be an object. A bare list is rejected. `not_relevant` must be
`{tool: reason}` — a bare list records no thinking and is rejected. More than 25
entries in `primary` is rejected as a copy of the catalog. An empty `primary`
with no `note` is rejected as a skipped review.
*Fix:* copy the `tools` block from section 4c and adapt the reasons.

**10. Pinning an adapter to a log file nothing writes.**
Adapters parse existing logs; they cannot create one. A pinned adapter on an
empty path yields silence that looks exactly like a healthy quiet program.
*Fix:* check `existing_logs_found` in the catalog and `av_log_where()`. If the
program does not log, you need an **emitter**, not an adapter.

**10b. Sealing a bridge that reads nothing at all.**
`emitters: []` with the rationale "already logs well" is a good answer *only* if
you also pin that existing log: `adapters={"<label from
catalog.adapter_pin_labels>": "auto"}`. Without the pin there is no log source,
and every log-side tool answers emptily for the rest of the program's life while
`av_bridge_status()` reports BUILT — and the gate never fires again to correct it.
*Fix:* the commit is now refused (`error: BRIDGE_WOULD_READ_NOTHING`) and the
response lists the logs you could have pinned. If frames-only is genuinely the
decision, set `visual_capture: true` and say "visual only" in the rationale.

**10c. Writing an adapter from ONE sample line.**
One line proves your pattern fits *that line*. Anchor tokens taken from a message
body ("`:: spool`") match one line and nothing else; the rest of the file falls to
a fallback and `source` becomes the fallback's own name. Measured: an adapter like
that scored `own_score: 1.00`, was accepted, and parsed 1 line in 4.
*Fix:* send the `also_match` list the catalog's `how_to_add_an_adapter` recipe
gives you — it is enforced, so a one-line-only pattern is rejected with the line
it missed. Anchors must be *structure* present in every line.

**11. Trusting a clean `av_diagnose()` on a freshly built bridge.**
With zero frames and an empty action log it still returns 200 with
`hypotheses: []` and "no strong failure signals - program looks healthy" —
indistinguishable from a genuinely healthy program. This is a recorded defect.
*Fix:* cross-check `av_program_status()` and `av_capture_status()` before
believing a clean result, and read `av_log_raw()` when a summary looks too
tidy.

**12. Debugging the wrong program.**
The active profile is **global** server state; other sessions may share it.
*Fix:* confirm `watching_now.program` and `watching_now.project_root` in
`av_start_here()` before you plan or diagnose anything. Changing the active
profile (`av_set_active_profile`) changes it for everyone connected — do not do
it casually.

**13. Bypassing the gate with `force=true`.**
`av_capture_start(force=True)` exists so a human clicking Start in the GUI is
never locked out. Used by a model it produces exactly the blind bridge this
whole mechanism prevents: capture with no plan and no chosen logging.
*Fix:* spend the three calls. They are cheap and they happen once.

---

## 9. Where to go next

| File | Contents |
|---|---|
| `docs/MCP_TOOLS_REFERENCE.md` | All 94 tools in 20 groups, each with `needs`, `cost`, caveats. **Generated** from `python_backend/api/tool_meta.json` by `scripts/gen_tools_ref.py` — never hand-edit it. |
| `docs/MCP_TOOL_AUDIT.md` | 77 recorded tool defects and caveats, 12 of them Class A (the tool actively misleads its caller). Read before trusting a surprising result. |
| `docs/WHAT_IS_AGENTVISION.md` | The full system description: architecture, emitters, capture, alignment. |
| `docs/AGENT_INSTRUCTIONS.md` | A paste-ready block for a project's `CLAUDE.md`, plus the MCP prompts and resources the server exposes. |
| `docs/LOG_ADAPTERS.md` | How the 658 adapters detect a format, and how to add one. |
| `docs/SCHEMA.md` | The event/frame schema on disk. |
| `docs/PUSH_MODE.md` | How AgentVision speaks to you unprompted, for free, via hooks. |
| `docs/RESEARCH_TOKEN_EFFICIENCY.md` | The measurements behind the token rule in section 7. |
| `python_backend/bridge_plan.py` | The gate itself: emitter options, code signals, `validate_plan`. The authority if this file and the code ever disagree. |
