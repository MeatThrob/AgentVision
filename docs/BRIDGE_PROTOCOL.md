# BRIDGE_PROTOCOL.md — the first-bridge contract

This is the specification of the one-time setup handshake between an AI agent and
AgentVision. Read it when your `av_bridge_commit` call was rejected, or before
you make your first one.

**The rule in one sentence:** AgentVision does not decide what logging to build
into a target program — you do, once, on first connection, by committing a plan.

Everything below was verified against the source on 2026-07-29:

- `python_backend/bridge_plan.py`
- `python_backend/api/bridge_server.py` (routes `/bridge/status`,
  `/bridge/catalog`, `/bridge/commit`, `/bridge/report`, `/capture/start`,
  `/install`, `/preflight`)
- `python_backend/api/claude_mcp.py`
- `python_backend/api/tool_meta.py`, `python_backend/api/tool_meta.json`
- `python_backend/emitters.py`, `python_backend/installer.py`

---

## 1. The three calls

Do exactly this, in this order, on a program you have never bridged:

```
1. av_bridge_status()    -> read "state"
2. av_bridge_catalog()   -> read it; keep "catalog_token"
3. av_bridge_commit(plan={...})
```

The same thing over HTTP (base URL is `http://127.0.0.1:7771`, overridable with
the `AGENTVISION_BRIDGE_URL` environment variable):

| MCP tool | HTTP |
| --- | --- |
| `av_bridge_status()` | `GET /bridge/status` |
| `av_bridge_catalog()` | `GET /bridge/catalog` |
| `av_bridge_commit(plan=..., replan=...)` | `POST /bridge/commit` with body `{"plan": {...}, "replan": false}` |
| `av_bridge_report()` — no such tool; use HTTP | `GET /bridge/report` |

There is no MCP tool for `/bridge/report`. It is reachable over HTTP only.

---

## 2. State machine

There are exactly two states. The word for "the plan is on disk" is **sealed**.

```
                 av_bridge_catalog()          av_bridge_commit(plan=...)
                 (read-only, no state         validates the plan, installs,
                  change)                     writes .av_bridge_plan.json
                        |                              |
   +---------------+    |                       +--------------+
   |  PROVISIONAL  |----+---------------------->|    BUILT     |
   |  sealed=false |                            |  sealed=true |
   +---------------+                            +--------------+
          ^                                            |
          |                                            |
          +-------- (no transition back) <-------------+
                    av_bridge_commit(plan=..., replan=True)
                    stays BUILT and overwrites the plan
```

There is no transition from BUILT back to PROVISIONAL through the API. To force
one, a human must delete both marker files named in section 9.

### 2.1 What each state blocks

Only two operations are gated. Everything else works in both states.

| Operation | PROVISIONAL | BUILT |
| --- | --- | --- |
| `av_capture_start()` | **REFUSED** | allowed |
| `av_install_project(...)` | **REFUSED** | allowed |
| `av_bridge_status`, `av_bridge_catalog` | allowed | allowed |
| `av_bridge_commit` | performs the build | no-op unless `replan=True` |
| every other one of the 90 MCP tools | allowed | allowed |

Source of truth: `bridge_server.py` line 3261 (`/capture/start`) and line 3534
(`/install`) are the only two places that call `bridge_plan.is_sealed()` as a
gate.

### 2.2 The refusal does NOT use an error status code

A refusal is **HTTP 200**. If you check only the status code you will believe it
succeeded. Check the body.

Real captured response from `av_capture_start()` on a PROVISIONAL program:

```json
{
  "ok": false,
  "started": false,
  "bridge_required": true,
  "bridge": {
    "blocked": ["capture/start", "install (emitters)"],
    "next": "av_bridge_catalog()",
    "note": "FIRST CONNECTION — the bridge is NOT built yet. AgentVision will not guess which logs and tools this program needs. Review av_bridge_catalog(), then av_bridge_commit(plan=...).",
    "plan": null,
    "program": "NaiveTest",
    "sealed": false,
    "sealed_by_legacy_marker": false,
    "state": "PROVISIONAL"
  },
  "guidance": "FIRST CONNECTION to this program — the bridge is NOT built yet, so capture will not start. …"
}
```

**Branch on these, in this order:**

1. `bridge_required === true` -> you are blocked by this gate. Go to
   `av_bridge_catalog()`.
2. `started === false` (capture) or `installed === false` (install) -> nothing
   happened.
3. `preflight_required === true` -> a *different* gate; see section 10.2.

The current source also sets `"error": "BRIDGE_NOT_BUILT"` and
`"DO_THIS_NEXT": "av_bridge_catalog()  ->  av_bridge_commit(plan=...)"` on both
refusals. The captured live response above did **not** carry those two fields,
because the running server process predated them. So: **test
`bridge_required`, not `error`.**

### 2.3 The legacy sealed case

Two files can make a program sealed. They live in the same directory
(section 9).

| File | Constant | Meaning |
| --- | --- | --- |
| `.av_bridge_plan.json` | `PLAN_FILENAME` | A real agent plan. `sealed: true` inside it. |
| `.av_preflight_ok` | `LEGACY_MARKER` | The older log-coverage-only preflight passed. No plan, no reasoning. |

`bridge_plan.is_sealed()` returns true if **either** exists (and, for the plan
file, if its `sealed` key is truthy).

`av_bridge_status()` reports this with `sealed_by_legacy_marker`:

```
sealed_by_legacy_marker = (legacy marker exists) AND NOT (a plan file says sealed: true)
```

| `state` | `sealed_by_legacy_marker` | `plan` | What it means | What to do |
| --- | --- | --- | --- | --- |
| `PROVISIONAL` | `false` | `null` | Never bridged. | Run the three calls. |
| `BUILT` | `false` | object | A real agent plan exists. | Nothing. Do not plan again. |
| `BUILT` | `true` | `null` | Sealed by the old marker only. Capture works, but **no one ever decided what this program needs**. | Optional: commit a real plan. See below. |

**Important quirk, verified:** on a legacy-sealed program you do **not** need
`replan=True`. `/bridge/commit` short-circuits on
`read_plan(folder).get("sealed")`, and on a legacy-sealed program `read_plan()`
returns `None` because there is no `.av_bridge_plan.json`. So a plain
`av_bridge_commit(plan={...})` is accepted and writes the plan. (The `note` text
in `av_bridge_status` suggests `replan=True`; that also works, and is harmless.)

The `sharpemu` profile in this installation is BUILT via the legacy marker.

---

## 3. `av_bridge_status()` — every field

`GET /bridge/status`. Always HTTP 200. Read-only.

| Field | Type | Always present | Meaning |
| --- | --- | --- | --- |
| `state` | `"PROVISIONAL"` or `"BUILT"` | yes | The only field you need to branch on. |
| `sealed` | bool | yes | `true` exactly when `state == "BUILT"`. Redundant with `state`. |
| `program` | string | yes | The target's display name (falls back to the profile name). |
| `plan` | object or `null` | yes | The full persisted plan record, or `null`. **`null` does not mean PROVISIONAL** — it is also `null` on a legacy-sealed BUILT program. |
| `sealed_by_legacy_marker` | bool | yes | See 2.3. |
| `note` | string | yes | Human-readable status sentence. Different text per state. |
| `what_this_is` | string | yes | Fixed explanatory string added by the route. |
| `blocked` | list of strings | **PROVISIONAL only** | `["capture/start", "install (emitters)"]`. |
| `next` | string | **PROVISIONAL only** | `"av_bridge_catalog()"`. |

**Branching logic, copy this:**

```
status = av_bridge_status()
if status["state"] == "BUILT":
    # Setup is done, forever. Do NOT call av_bridge_catalog or av_bridge_commit.
    # Go straight to av_capture_start() / av_diagnose() / whatever you came for.
else:
    # PROVISIONAL. Call av_bridge_catalog(), then av_bridge_commit(plan=...).
```

Never infer the state from `plan` being `null`. Use `state`.

---

## 4. `av_bridge_catalog()` — every top-level key

`GET /bridge/catalog`. Always HTTP 200. Read-only, but it does scan the target's
source files (bounded: 400 files max, 400 KB per file, skipping `.git`,
`node_modules`, `venv`, `.venv`, `__pycache__`, `dist`, `build`, `target`,
`vendor`, `.tox`, `site-packages`, `agentvision`, `.idea`, `.vscode`, `obj`,
`bin`).

| Key | Type | What it is FOR |
| --- | --- | --- |
| `catalog_token` | string, 16 hex chars | Paste into `plan.catalog_token`. Without a matching one, commit is rejected. See 4.1. |
| `version` | int (`1`) | Plan-format version. |
| `program` | string | Which target this catalog describes. |
| `language_detected` | string | The **profile's** declared language, lowercased, or `"unknown"`. This is what gates `emitters_available`. Not the file scan — see the warning in 4.3. |
| `emitters_available` | list of objects | The menu you pick `plan.emitters` from. See 4.2. |
| `adapters` | object | `{total, families: {family: count}, drill_in, note}`. Adapters PARSE logs that already exist. Use `av_list_adapters(family=..., q=...)` for names. Live count at time of writing: 658. |
| `source_readers` | list of strings | Binary log readers. Currently: `docker_json`, `faillock`, `lastlog`, `mrt`, `netflow_v5`, `pcap`, `unified2`, `utmp`, `wtmpdb`. |
| `mcp_tool_groups` | object | 19 groups. Each is `{tools: [...], relevant_here: N, ruled_out: N}`. Pick `plan.tools.primary` from here. See 4.4. |
| `existing_logs_found` | list of objects | Every log this program has: `{label, path, declared, exists, bytes}`. `declared: true` = the profile already reads it. `declared: false` = **found on disk in the project and NOT read yet**; those entries also carry `detected_adapter`, `covered`, `sample`, and (when `covered` is false) `coverage_warning` + a copy-paste `how_to_add_an_adapter` body. **An undeclared log is only read if you pin it in `plan.adapters` — see 5.3.** |
| `code_evidence` | object | The scan of the target's own source. This is what justifies your choices. See 4.3. |
| `capture_settings` | object | `{interval_seconds: "0.1 - 10; ASK THE USER, do not assume", how_to_ask: ..., note: ...}`. You do not have to ask in prose — see 4.6. |
| `you_must_decide` | list of strings | The four decisions expected of you. |
| `do_not` | list of strings | Three named failure modes. |
| `required_in_plan` | object | Short per-field reminder of the plan schema. |
| `what_this_is`, `how_to_commit` | strings | Fixed guidance text. |

### 4.1 `catalog_token` — what it digests, why, when it goes stale

The token is `sha256(...)[:16]` over exactly five things
(`bridge_plan.catalog_token`):

1. `version`
2. `language_detected`
3. the sorted list of `emitters_available[*].id`
4. `adapters.builtin_total` (the count of BUILT-IN adapters — **not** `total`,
   which includes user adapters; see below)
5. the sorted list of `mcp_tool_groups` keys

Nothing else. Not `code_evidence`, not `existing_logs_found`, not the program
name. That is deliberate: the token stays valid while you think about the code,
and changes only when the *option set itself* changes.

**Why it exists:** `validate_plan` rejects any plan whose token does not match
the token the server computes right now. You therefore cannot commit a plan for
options you never fetched. "Review the options first" is enforced, not
requested.

**It goes stale when any of these happens between your catalog call and your
commit:**

| Cause | Which digest input changed |
| --- | --- |
| The active profile changed to a program of another language | `language_detected`, and therefore `emitters_available` |
| The profile's `language` field was filled in or edited | same |
| AgentVision was upgraded, adding/removing an emitter id or a tool group | `emitters_available` / `mcp_tool_groups` |
| AgentVision was upgraded with new BUILT-IN adapters | `adapters.builtin_total` |

**`av_add_adapter` does NOT invalidate your token.** It used to, because the
digest counted the whole registry — and the catalog is what *tells* you to add an
adapter when it reports a format as uncovered. So the prescribed path (catalog →
add the missing adapter → commit) ended in a guaranteed stale-token rejection and
a forced re-read; a cold run spent one of its only two failed attempts there. An
adapter you added yourself is not the options changing under you, which is the
only thing this guard is for. You may add adapters before or after fetching the
catalog.

Recovery is always the same: call `av_bridge_catalog()` again, take the new
token, resubmit.

### 4.2 `emitters_available` — the menu, and it is language-gated

Emitters CREATE logs that do not exist. Adapters PARSE logs that do. A program
with no logging needs an emitter first.

Each entry carries `id`, `captures`, `misses`, `cost`, `good_for`, `languages`,
`builds_as`, and sometimes `note`. **Read `misses`.** An emitter chosen without
reading what it does not cover is how a bridge ends up blind to the actual
failure.

The menu depends on `language_detected`. Verified by running
`bridge_plan._emitter_options()` for every language:

| `language_detected` | ids offered | count |
| --- | --- | --- |
| `python` | `stdout_tee`, `lifecycle`, `uncaught_exceptions`, `logging_bridge`, `swallowed_exceptions` | 5 |
| `node`, `ruby` | `stdout_tee`, `lifecycle`, `uncaught_exceptions`, `logging_bridge` | 4 |
| `java`, `dotnet`, `csharp` | `stdout_tee`, `lifecycle`, `config_dropin` | 3 |
| `go`, `rust`, `c`, `cpp`, `shell`, `""`, `unknown` | `stdout_tee`, `lifecycle`, `run_wrapper` | 3 |
| anything else (`swift`, `kotlin`, `php`, …) | `stdout_tee`, `lifecycle` | 2 |

What each id claims to do:

| id | Captures | Notable miss |
| --- | --- | --- |
| `stdout_tee` | stdout + stderr, line by line | anything never printed: silent exit, swallowed exception, a hang |
| `lifecycle` | process start/exit, argv, pid, exit code | everything between start and exit |
| `uncaught_exceptions` | uncaught + thread + shutdown exceptions | every exception the program catches |
| `logging_bridge` | the language's logging framework, level-mapped | bare `print()`/`printf()`, and anything before the bridge loads |
| `swallowed_exceptions` | exceptions the program catches and hides | non-exception failure; and it needs Python 3.12+ |
| `config_dropin` | structured JSON via logback/Serilog | anything written outside the framework |
| `run_wrapper` | stdout/stderr via `agentvision run -- <cmd>`, plus exit code | anything not on stdout/stderr; a segfault leaves only the exit status |

`run_wrapper` **only takes effect if the program is actually launched through
`agentvision run`.** Selecting it does not change how the program is started.

Read section 8 before you assume selecting an id builds that specific thing.

### 4.3 `code_evidence` — how to justify a choice

`code_evidence` is the result of regex-scanning the target's source. Shape:

| Field | Meaning |
| --- | --- |
| `scanned_files` | how many source files were actually read |
| `languages_by_file_count` | `{language: file count}`, descending |
| `primary_language` | the language with the most files |
| `largest_files` | up to 8 `{lines, file}` — where the program's bulk is |
| `signals` | `{signal_name: {count, files, means, argues_for}}` |
| `how_to_use_this` | fixed instruction string |

`signals` is the part you cite. Every signal tells you what it `means` and what
it `argues_for`. The 12 signals, and what each argues for:

| Signal | What it detects | Argues for |
| --- | --- | --- |
| `exception_handlers` | any place errors are intercepted | context for the next two |
| `discards_error` | handlers that throw the error away (`pass`, `return None`, `continue`, empty `catch {}`) | `swallowed_exceptions` — the strongest case for it |
| `logs_in_handler` | handlers that DO report the error | `logging_bridge`; full hooks may be redundant |
| `threads` | `threading.`, `std::thread`, `go func(`, `Task.Run`, … | `uncaught_exceptions` (its thread hook) |
| `async` | `async def`, `await`, `Promise.`, `.then(` | `uncaught_exceptions` |
| `subprocess` | it launches other programs | `stdout_tee`, and `agentvision run` for the child |
| `existing_logging` | `logging.`, `log4j`, `Serilog`, `winston`, `fmt.Print`, … | `logging_bridge` (route what exists) |
| `prints_only` | `print(`, `puts`, `echo` at line start | `stdout_tee` — cheap and immediately useful |
| `gui_toolkit` | `tkinter`, `PyQt`, `SDL`, `GLFW`, `electron`, `SwiftUI`, … | `visual_capture: true` |
| `web_service` | `flask`, `django`, `fastapi`, `express(`, `ASP.NET`, `gin.`, … | `visual_capture: false` |
| `network_io` | `requests.`, `httpx`, `fetch(`, `HttpClient`, … | consider `av_watch` on status codes |
| `file_io` | `open(`, `fs.readFile`, `File.Open`, `std::fstream` | `swallowed_exceptions` |

**How to use it in `plan.why`:** name the signal, its count, and a file from its
`files` list. That is what makes the reason checkable. Example of a good `why`
entry, taken from a plan that was accepted:

> `"run_wrapper": "prints_only signal: src/common/Logging.c printf()s to stdout with no log file, and a native binary has no in-process hook to install, so wrapping the launch is the only way to see any of it"`

**Absence is evidence too.** No `gui_toolkit` signal and a `web_service` signal
means visual capture is wasted: set `visual_capture: false` and put the frame
tools in `not_relevant`.

**Warning — two different language fields.** `language_detected` comes from the
**profile's** `language` field. `code_evidence.primary_language` comes from the
**file scan**. When the profile's language is blank, `language_detected` is
`"unknown"` and you are offered the compiled-language menu
(`stdout_tee`/`lifecycle`/`run_wrapper`) even for a Python project. If those two
fields disagree, say so in your `rationale` and pick from the menu you were
actually given — `plan.emitters` is not validated against the menu, but naming
an id that was not offered records a decision the installer will not honour.

### 4.4 `mcp_tool_groups` — and the verdict you should obey

19 groups, 90 tools, every tool in exactly one group. Each group has this shape
(this is a shape sketch, not something you send):

```text
{ "tools": [ {"tool": <name>, "summary": <text>, "needs": [<token>, ...],
              "cost": <text>, "verdict": "core" | "useful" | "n/a",
              "verdict_reason": <text>, "caveat": <text>} ],
  "relevant_here": <int>, "ruled_out": <int> }
```

The 19 group names: `start`, `cheap_visual_path`, `flight_recorder`, `ui_tree`,
`orient`, `diagnose`, `investigate`, `frames`, `logs`, `capture`, `source`,
`raw_logs`, `bridge_setup`, `retention`, `profiles`, `health`, `program`,
`bookmarks`, `wrap_up`.

| Per-tool field | Use |
| --- | --- |
| `summary` | what the tool returns |
| `needs` | the precondition tokens. This is the load-bearing field. |
| `cost` | token cost band |
| `verdict` | `core` = usable now; `useful` = usable once a precondition is met; `n/a` = cannot work on this program |
| `verdict_reason` | present only when `verdict != "core"`. **Copy this into `not_relevant` as the reason.** |
| `caveat` | a recorded defect in that tool. 78 of 90 tools carry one. |

**Rule: never put a `verdict: "n/a"` tool in `plan.tools.primary`.** Put it in
`not_relevant` and reuse its `verdict_reason`.

Measured, so you know what to expect:

- A **headless** program (profile declares no `capture_app` and no
  `window_title`): **24** tools come back `n/a`, all of them frame/OCR/UI/
  capture tools. 63 are `core`.
- A **C GUI** program: exactly **1** tool comes back `n/a` — `av_run_tests`,
  because it hardcodes `python -m pytest`.
- 89 of the 90 tools are language-agnostic. `av_run_tests` is the only one
  restricted by language (to `python`).

So the real discriminator is GUI vs headless, not language.

**Note:** these verdicts are computed from the **profile** (`capture_app` /
`window_title` being set), not from your `plan.visual_capture`. A headless
service whose profile happens to name a window will show GUI verdicts anyway.

### 4.5 No `project_root`? The scan opened nothing

If the active profile has no `project_root`, `code_evidence` comes back with
`scanned_files: 0` and an `error`. **That is an absence of input, not an absence
of signals**, and a plan committed on it is the blind guess this gate exists to
prevent. AgentVision will not substitute its own working directory — it used to,
and that meant scanning *itself* and presenting the result as evidence about
your program.

In that case `av_bridge_catalog()` asks the user where the code lives and
returns the answer under `project_root_needed`:

```json
{"value": "/Users/you/src/widget", "how": "asked", "chosen_by_user": true,
 "note": "project root: /Users/you/src/widget — you chose this.",
 "apply_with": "av_create_profile(name=\"<program>\", project_root=\"/Users/you/src/widget\")",
 "then": "call av_bridge_catalog() again — this catalog's token describes a scan that opened NO files"}
```

It does **not** apply it. Pointing a profile at a folder changes the user's
configuration, so the tool names the call and leaves it to you. Re-fetch the
catalog afterwards: the token you hold describes the empty scan.

If `how` is anything other than `"asked"`, nobody answered — read `note`, and
ask in prose yourself.

### 4.6 The frame rate: let AgentVision ask

`capture_settings.interval_seconds` says *ASK THE USER, do not assume*. You can
do that literally, or you can call `av_capture_start()` **with no interval** and
AgentVision will put the question and the supported range in front of the user
itself over MCP elicitation.

Either way, read `capture_rate_choice` in the response:

| `how` | What it means |
|---|---|
| `asked` | the user chose this rate |
| `declined` / `cancelled` | they were asked and did not answer; the fallback is in effect |
| `unsupported` | this MCP client cannot show a prompt — **ask in prose** |
| `no_context` | not an MCP call at all (HTTP, CLI) |
| `failed` | the ask errored; `detail` has the reason verbatim |

Only `asked` means a human chose. Everything else is AgentVision's default
wearing a value's clothes, and saying "capture is running at 1 fps as you asked"
on the strength of it would be a claim nobody established.

The same shape governs `user_input`, the one emitter that records the human
system-wide: selecting it makes `av_bridge_commit` ask for consent, an explicit
**no** removes it from the plan, and where nobody can be asked your selection
stands but `input_recording_consent` says plainly that nobody consented.

---

## 5. The plan schema

### 5.1 Field by field

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `catalog_token` | string | **yes** | Copied verbatim from the catalog you just read. |
| `emitters` | list of strings | **yes** | Emitter ids to build in. May be `[]`, but then see the rationale rule. |
| `why` | object `{emitter_id: string}` | **yes when `emitters` is non-empty** | One reason per selected emitter, each ≥ 15 characters, each tied to a `code_evidence` signal. |
| `rationale` | string | **yes** | One line on the program as a whole. Must be non-empty after stripping whitespace. |
| `tools` | object | **yes** | `{primary: [...], not_relevant: {tool: reason}, note: "..."}`. |
| `tools.primary` | list of strings | **yes** | Tools you would actually reach for, in order. Max 25. May be `[]` only if `tools.note` explains why. |
| `tools.not_relevant` | object `{tool: reason}` | no | Must be an object if present. A bare list is rejected. |
| `tools.note` | string | no | Required in practice when `primary` is empty. |
| `adapters` | object `{source_label: adapter_name}` | no | Pin a parser per log source. `"auto"` lets the detector choose. See 5.3. |
| `capture` | object `{interval_seconds: number}` | no | Recorded only. See section 8.3. |
| `visual_capture` | bool | no | Recorded only. See section 8.3. |

Any other key you add is accepted and silently dropped — `write_plan` persists
only the fields listed above.

### 5.2 The complete validation rule list

From `bridge_plan.validate_plan` and `bridge_plan._validate_tools`, in
evaluation order. All failures are collected, not short-circuited (except where
noted), so one rejection can list several errors.

1. `plan` must be a JSON object. (Short-circuits; nothing else is checked.)
2. `plan.catalog_token` must be present and non-empty.
3. `plan.catalog_token` must equal the token the server computes right now.
4. `plan.emitters` must be present.
5. `plan.emitters` must be a list.
6. `plan.rationale` must be present and non-blank.
7. If `plan.emitters == []`, then `plan.rationale`, lowercased, must contain the
   substring `already` **or** the substring `no log`. This is a literal
   substring test.
8. If `plan.emitters` is a non-empty list, `plan.why` must be an object.
9. `plan.why` must have a non-blank entry for **every** id in `plan.emitters`.
10. Each of those entries must be **≥ 15 characters** after stripping. (Exactly
    15 passes.)
11. `len(plan.emitters)` must be **< 6**.
12. `plan.tools` must be present. (Short-circuits the rest of the tool checks.)
13. `plan.tools` must be an object, not a list. (Short-circuits.)
14. `plan.tools.primary` must be present.
15. `plan.tools.primary` must be a list.
16. `len(plan.tools.primary)` must be **≤ 25**.
17. If `plan.tools.primary` is empty or absent, `plan.tools.note` must be
    non-blank.
18. If `plan.tools.not_relevant` is present, it must be an object.

**Things that are NOT validated** — verified by reading the whole of
`validate_plan`:

- Emitter ids are **not** checked against `emitters_available`. A typo or an
  invented id passes as long as `why` has an entry for it.
- Duplicate ids in `emitters` are allowed (and count toward rule 11).
- `why` entries for ids not in `emitters` are ignored, not rejected.
- Tool names in `primary` and `not_relevant` are **not** checked against the
  89 real tools.
- `capture.interval_seconds` is not range-checked.

Adapter names in `adapters` **are** checked against the registry (they were not,
until a pin of a non-existent adapter was found validating clean, being written
into the profile, and then parsing with `raw` while `/bridge/report` echoed the
name that never resolved). Use `"auto"` or a name from `av_list_adapters()`; add
your own with `av_add_adapter` first if the format has none.

**Consequence of rule 11 worth knowing:** the largest menu on offer is 5 ids
(Python). So rule 11 can only fire if you list ids that were not offered, or
duplicates. If you get that error, you have almost certainly invented ids.

### 5.3 `adapters` keys — a pin is how a log gets READ

`plan.adapters` is `{source label: adapter name}`. On commit
(`_wire_plan_sources` in `bridge_server.py`) each key is matched against the
wireable sources for this program:

| Key | Applies to | Default if you omit the key |
| --- | --- | --- |
| `"events"` | `<project_root>/agentvision/actions.jsonl` (emitter sink) | `"jsonl"` |
| `"text"` | `<project_root>/agentvision/log.txt` (emitter sink) | `"auto"` |
| `"stdout"` | fallback for either sink above — never for a file the project writes itself | — |
| any label from `catalog.existing_logs_found` with `declared: false` | that log file | **not read at all** |

The last row is the load-bearing one. A log the program already writes is
reported by the catalog but **not** registered as a source unless your plan pins
it: deciding to read a file is your call, not AgentVision's. If you leave one
unpinned, the commit response carries `built.discovered_not_wired` naming it and
the one-line command to pin it — it is reported, never silently skipped.

Every valid label that matched no file is reported in
`built.adapters_unapplied` (for example pinning `"text"` while selecting no
emitters: the sink is never created, so the pin has nothing to attach to).

**A plan that leaves nothing readable is refused** with
`error: "BRIDGE_WOULD_READ_NOTHING"` — see section 6. It was reachable by the
most reasonable-looking plan on offer (`emitters: []`, rationale "already logs
well") and sealed a permanently blind bridge that every state surface reported
as BUILT.

Adapter pinning is honoured end to end: the `abyss` profile pinned
`{"events": "jsonl", "text": "abyssengine"}` and `/bridge/report` shows
`adapter_declared` = `adapter_resolved` = `abyssengine` for the text source. A
pinned name that is **not** registered now shows `adapter_resolved: "raw"` plus
an `adapter_warning`, instead of echoing the name that never resolved.

---

## 6. Every rejection reason

A validation failure returns **HTTP 400** with:

```json
{"ok": false, "sealed": false, "errors": ["...", "..."],
 "catalog_token_expected": "510cd849c665fb8c",
 "next": "av_bridge_catalog() — then commit with its token"}
```

The `errors` list holds the exact strings below. They were produced by running
`validate_plan` on deliberately broken plans, so the text is verbatim.

| # | Exact error text | What triggered it | The fix |
| --- | --- | --- | --- |
| 1 | `plan must be an object` | You sent a list or a string as `plan`. Note: an empty list or `null` becomes `{}` server-side and produces errors 2, 4, 6, 12 instead. | Send a JSON object. |
| 2 | `catalog_token is missing — call av_bridge_catalog() first and pass the token it returns; the bridge is not sealed on trust` | `catalog_token` absent, `null`, or `""`. | Call `av_bridge_catalog()`, copy its `catalog_token` string into the plan. |
| 3 | `catalog_token 'X' does not match the current catalog ('Y') — the available options changed, re-read av_bridge_catalog() before committing` | Your token is stale. Most common cause: you called `av_add_adapter` after fetching the catalog, or the active profile changed. | Call `av_bridge_catalog()` again. Use the new token. Do not hand-edit the old one. `catalog_token_expected` in the response is the value you need. |
| 4 | `plan.emitters is required — a list of emitter ids to build in, or [] with a reason if the program already logs well` | The `emitters` key is absent. | Add `"emitters": [...]`. Use `[]` plus rule 7's rationale wording if the answer is genuinely none. |
| 5 | `plan.emitters must be a list` | You sent a string, e.g. `"emitters": "logging_bridge"`. | Wrap it: `"emitters": ["logging_bridge"]`. |
| 6 | `plan.rationale is required — one line on WHY this set fits this program, so the choice is auditable later` | `rationale` absent, `null`, or only whitespace. | Add one sentence about the program as a whole (not per emitter — that is `why`). |
| 7 | `plan.emitters is empty — if that is deliberate, say so in rationale (e.g. 'already logs well: ...'), because an empty set is indistinguishable from a forgotten one` | `emitters: []` and your `rationale` contains neither `already` nor `no log` (case-insensitive substring test). | Rewrite the rationale to literally contain one of those phrases. `"already logs well: writes structured JSON to /var/log/app.jsonl"` passes. |
| 8 | `plan.why is required: {emitter_id: reason} with one line per selected emitter, each tied to something in catalog.code_evidence — without it the selection cannot be told apart from installing things at random` | `emitters` is non-empty and `why` is absent or is not an object. | Add `"why": {"<id>": "<reason>", ...}`. |
| 9 | `plan.why is missing a reason for: ['a', 'b'] — every emitter must answer to something in the code` | One or more selected ids have no `why` entry, or the entry is blank/whitespace. | Add an entry for each id named in the error. Keys must match the ids exactly. |
| 10 | `plan.why entries for ['a'] are too short to be a reason — say which code signal justifies each` | A `why` entry is 1-14 characters. | Expand to ≥ 15 characters. Name the `code_evidence` signal, its count, and a file. |
| 11 | `plan selects N emitters of the M on offer — that is close to everything available. Installing the lot is the same blanket guess this gate exists to prevent; keep what the code evidence actually supports.` | `len(emitters) >= bridge_plan.blanket_threshold(M)`, i.e. `max(3, ceil(0.75 × M))` where M is the size of *this language's* menu. With Python's 5 ids the limit is 4, so 4 or more is rejected. (It used to be an absolute `>= 6`, which no menu could reach — dead code.) | Re-read `emitters_available`; `catalog.select_at_most.emitters` states the largest accepted count as a number. Most programs need 1-2. |
| 12 | `plan.tools is required — {'primary': [...], 'not_relevant': {tool: reason}} naming the tools worth calling for THIS program. Pass {'primary': [], 'note': '...'} if you truly reviewed them and have no preference, but say so explicitly.` | The `tools` key is absent. | Add the `tools` object. This is not optional. |
| 13 | `plan.tools must be an object, not a bare list — it needs both the tools you chose and (at least briefly) what you ruled out` | You sent `"tools": ["av_diagnose"]`. | Wrap it: `"tools": {"primary": ["av_diagnose"], "not_relevant": {...}}`. |
| 14 | `plan.tools.primary is required — the handful of tools you would actually reach for on this program, in order` | `tools` exists but has no `primary` key. Usually arrives together with error 17. | Add `"primary": [...]`. |
| 15 | `plan.tools.primary must be a list of tool names` | You sent a bare string. | Wrap it in a list. |
| 16 | `plan.tools.primary lists N tools — that is a copy of the catalog, not a choice. Name the ones you would reach for first; the rest stay callable regardless.` | `len(primary) > 25`. | Cut to the handful you would actually call first. Every tool stays callable regardless of what you list. 8-12 is a normal size. |
| 17 | `plan.tools.primary is empty and no note explains why — an empty list is indistinguishable from a skipped review` | `primary` is `[]`, or absent, and `tools.note` is missing or blank. | Either name some tools, or add `"note": "<why you have no preference>"`. |
| 18 | `plan.tools.not_relevant must be {tool: reason} — the reason is the point; a bare list records no thinking` | You sent `"not_relevant": ["av_ui_tree"]`. | Convert to an object: `{"av_ui_tree": "headless, no accessibility tree"}`. Reuse the catalog's `verdict_reason`. |
| 19 | `plan.adapters has unknown source label(s) ['log'] — valid labels for this program are [...]. The key is the log source's LABEL (see catalog.adapter_pin_labels), not the adapter name and not a filename.` | A key of `plan.adapters` is not a label of any log source for this program. Measured on a cold model that pinned `"log"` when the label was `"text"`. | Copy a key from `catalog.adapter_pin_labels`. |
| 20 | `plan.adapters['text'] = 'no_such_adapter' is not a registered adapter. Use "auto", or a name from av_list_adapters(); add your own first with av_add_adapter if this format has none.` | The *value* names an adapter that does not exist. Previously accepted, then silently degraded to `raw` at read time. | Use `"auto"`, an existing name, or `av_add_adapter` first. |
| 21 | `plan.adapters['text'] is empty — use "auto" to detect the format, or a real adapter name.` | The value is `""`/whitespace. | `"auto"` is the right answer when you do not want to choose. |

Two more rejections do not come from `validate_plan`, and both carry a
machine-checkable `error` key rather than only prose:

| `error` | HTTP | What triggered it | The fix |
| --- | --- | --- | --- |
| `PLAN_NOT_WRAPPED` | 400 | The plan's fields were sent at the **top level** of the HTTP body instead of nested under `"plan"`. Nothing is missing; the envelope is wrong. Measured on a cold model that then guess-and-checked the envelope for four attempts. | `{"plan": {...}, "replan": false}`. The MCP tool `av_bridge_commit(plan={...})` wraps for you — do not wrap it yourself as well. |
| `BRIDGE_WOULD_READ_NOTHING` | 400 | The plan would seal a bridge with **no log sources at all**: no emitter installed and no existing log pinned. Every log-side tool would return empty forever while the state surface said BUILT — and the gate never fires again to correct it. | Pin an existing log (`adapters={"<label>": "auto"}` — the response lists candidates in `logs_you_could_pin`), or select an emitter, or, if frames-only is the real decision, set `visual_capture: true` and say `"visual only"` / `"no logs"` in the rationale so it is recorded as a choice. |

### 6.1 Two responses that are NOT rejections

| Response | HTTP | Meaning | Action |
| --- | --- | --- | --- |
| `{"ok": false, "already_sealed": true, "plan": {...}, "note": "This program's bridge is already built — the gate is first-connection only. Pass replan=true to deliberately re-decide."}` | 200 | The plan file already exists. Your plan was **not** applied and **nothing was overwritten**. | Nothing. This is the correct outcome for a program you should not have re-planned. Do not retry with `replan=True` unless you genuinely mean to re-decide. |
| `{"ok": true, "sealed": true, "plan": {...}, "built": {...}, "note": "Bridge built. …", "next": "av_capture_start()"}` | 200 | Success. | Proceed. Check `built.install` and `built.actions`; see 8.4. |

---

## 7. Three worked plans

The point of these three is that they are **different**. The same program shape
does not recur; copy the reasoning method, not the field values.

In all three, replace `catalog_token` with the value from **your** catalog call.
The tokens shown are placeholders and will be rejected as stale.

### 7.1 A Python web service that already uses `logging`

**Catalog signals that drove this:** `existing_logging` 214 hits across
`app/api/*.py`; `async` 486 and `threads` 31; `discards_error` 37 hits with
`app/api/payments.py` and `app/db/session.py` in `files`; `web_service` present;
`gui_toolkit` **absent**. `language_detected` = `python`, so all 5 ids are on
the menu.

**Reasoning.** "Already uses logging" is not "already logs well" — the handlers
write to stdout and the container throws it away, so `logging_bridge` routes
what already exists and is the cheapest complete win. `async` + `threads` means
failures surface as unhandled task rejections that never reach the default
excepthook, so `uncaught_exceptions` earns its place. `discards_error` in the
payment and DB retry paths is the textbook case for `swallowed_exceptions`.
`stdout_tee` is **not** selected: the same lines arrive through
`logging_bridge`, already level-mapped. `lifecycle` is **not** selected: a
long-lived service under a supervisor restarts on its own schedule and process
boundaries are not the interesting event here. No `gui_toolkit` signal, so
`visual_capture` is false and every frame/OCR/UI tool goes to `not_relevant`.

```json
{
  "catalog_token": "REPLACE_WITH_YOUR_TOKEN",
  "emitters": ["logging_bridge", "uncaught_exceptions", "swallowed_exceptions"],
  "why": {
    "logging_bridge": "existing_logging x214 across app/api/*.py: every module calls getLogger, but the handlers write to stdout only and the container discards it, so routing what already exists is the cheapest full win",
    "uncaught_exceptions": "async x486 plus threads x31: FastAPI request tasks fail as unhandled rejections on the loop, which never reaches the default excepthook and so never lands in the log",
    "swallowed_exceptions": "discards_error x37 with files app/api/payments.py and app/db/session.py: `except Exception: return None` in the payment and DB retry paths means the exact failures we care about are invisible today"
  },
  "rationale": "Headless FastAPI service. It logs heavily but into a discarded stdout, and its error paths swallow; the work is routing and un-hiding, not adding new print statements.",
  "adapters": {"events": "jsonl", "text": "auto"},
  "capture": {"interval_seconds": 5.0},
  "visual_capture": false,
  "tools": {
    "primary": ["av_diagnose", "av_log_raw", "av_search", "av_errors_by_fingerprint",
                "av_new_errors_this_session", "av_log_normalized", "av_log_where",
                "av_watch", "av_incidents", "av_session_report"],
    "not_relevant": {
      "av_ui_tree": "needs accessibility_api, which a headless service cannot provide",
      "av_ui_diff": "same as av_ui_tree: there is no UI to diff",
      "av_visual_changes": "needs frames_on_disk, but visual capture is off for this program",
      "av_frame_region": "needs frames_on_disk, but visual capture is off for this program",
      "av_error_moment": "needs frames_on_disk; on this program the log alone carries the moment",
      "av_source_at_error": "needs frames_on_disk, so it is n/a here despite being the tool I would want",
      "av_ocr_frame": "needs frames_on_disk and an OCR backend; the log is already text",
      "av_read_screen": "needs capture_running; there is no screen on a container workload",
      "av_program_crop": "needs gui_program, which this service is not",
      "av_trace_timeline": "needs a trace_id per record and the service emits none today"
    },
    "note": "Reviewed all 19 groups. Log-side tools only: the entire failure surface is text, and 24 tools came back verdict n/a for this headless program."
  }
}
```

Validated against `bridge_plan.validate_plan`: accepted.

### 7.2 A compiled C/SDL game with `printf`-only output

This is the **real** plan committed for the `abyss` profile (AbyssEngine). It is
reproduced from `.av_bridge_plan.json` on disk.

**Catalog signals that drove this:** `prints_only` in `src/common/Logging.c`;
`existing_logging` 25 hits; `gui_toolkit` present (SDL); no log file anywhere.
`language_detected` = `c`, so the menu is `stdout_tee`, `lifecycle`,
`run_wrapper` — three ids only.

**Reasoning.** A native binary has no in-process hook to install, so
`stdout_tee` is the wrong pick even though the program prints: the catalog's own
note says to use `run_wrapper` for a compiled program. `lifecycle` is selected
here (unlike 7.1) because an SDL game loop that segfaults produces no output at
all — only a non-zero exit code, which nothing else would record. `visual_capture`
is **true** here (unlike 7.1 and 7.3) because `gui_toolkit` is present and the
window shows state no log contains. The log format `[DEBUG] Sprite.c:32 - msg`
was being mis-claimed by the `coreboot_cbmem` adapter, so a custom
`abyssengine` adapter was registered first and pinned to the `text` label —
note that the pin is by label, and that the adapter was added **before** the
catalog call so the token stayed valid.

```json
{
  "catalog_token": "REPLACE_WITH_YOUR_TOKEN",
  "emitters": ["run_wrapper", "lifecycle"],
  "why": {
    "run_wrapper": "prints_only signal: src/common/Logging.c printf()s to stdout with no log file, and a native binary has no in-process hook to install, so wrapping the launch is the only way to see any of it",
    "lifecycle": "existing_logging x25 covers runtime events but nothing marks run boundaries; an SDL game loop that segfaults shows up only as a non-zero exit, which is otherwise invisible"
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
      "av_ui_tree": "SDL blits one opaque surface — the window has no accessibility children, so the tree is a single empty node regardless of what is drawn",
      "av_ui_diff": "same reason as av_ui_tree: nothing structured to diff in an SDL-rendered frame",
      "av_program_stats": "its parser is a hardcoded 9-key game-bot leveling whitelist, unrelated to this engine",
      "av_state_at": "needs periodic wide full-state snapshots; this program never emits any",
      "av_state_diff": "same missing wide snapshots as av_state_at",
      "av_trace_timeline": "needs a trace_id per record; printf output carries none",
      "av_ocr_frame": "OCR of rendered game text is strictly worse than the log, which already carries the same strings verbatim",
      "av_read_screen": "same as av_ocr_frame — the log is the truthful text source here"
    },
    "note": "Reviewed all 19 groups. Chosen set follows the failure mode: asset-load FATALs that carry file:line, so the log-to-source jump matters more than any pixel tool."
  }
}
```

Note that `av_ui_tree` and `av_ocr_frame` are `verdict: "core"` for this GUI
program — the catalog does **not** rule them out. They are in `not_relevant`
because the agent judged them worthless *for this program's failure mode*. That
is a legitimate use of `not_relevant`, and it is what the field is for.

### 7.3 A headless .NET worker service

**Catalog signals that drove this:** `existing_logging` 96 hits, all Serilog
`ILogger` calls; `subprocess` 12 hits in `Worker/JobRunner.cs`; no
`gui_toolkit`; no `discards_error`. `language_detected` = `dotnet`, so the menu
is `stdout_tee`, `lifecycle`, `config_dropin` — and neither
`swallowed_exceptions` nor `uncaught_exceptions` is even on offer.

**Reasoning.** `config_dropin` is the whole job here: the program already uses
Serilog, and the drop-in config writes AgentVision's exact unified schema, one
JSON object per line, to `agentvision/actions.jsonl` — with no code change.
`stdout_tee` would only duplicate it in a worse format. `lifecycle` is selected
because a supervisor restarts this worker, and without start/exit/argv/exit-code
a crash loop is indistinguishable from healthy recycling. Because the sink is
JSON lines, `adapters.events` is pinned to `jsonl` rather than left on `auto`.
`visual_capture` is false: `web_service`-shaped, no `gui_toolkit`, nothing to
look at. Note the smallest `primary` of the three — a queue worker's failures
are repetitive, so fingerprinting beats any single-moment tool.

```json
{
  "catalog_token": "REPLACE_WITH_YOUR_TOKEN",
  "emitters": ["config_dropin", "lifecycle"],
  "why": {
    "config_dropin": "existing_logging x96 is all Serilog ILogger calls, so the ecosystem's own file sink writes AgentVision's schema with no code edit; nothing else reaches those records",
    "lifecycle": "subprocess x12 in Worker/JobRunner.cs plus a supervisor that restarts the worker: without start/exit/argv/exit-code there is no way to tell a clean restart from a crash loop"
  },
  "rationale": "Headless .NET worker service driven by a queue. It already uses Serilog, so the drop-in config is the whole job; there is no window, so nothing visual applies.",
  "adapters": {"events": "jsonl"},
  "capture": {"interval_seconds": 10.0},
  "visual_capture": false,
  "tools": {
    "primary": ["av_diagnose", "av_log_raw", "av_errors_by_fingerprint", "av_search",
                "av_metrics", "av_log_where", "av_log_entities", "av_session_report"],
    "not_relevant": {
      "av_run_tests": "language dotnet is not in ['python']; it hardcodes python -m pytest",
      "av_ui_tree": "needs accessibility_api, which a headless worker cannot provide",
      "av_ui_diff": "nothing structured to diff without a UI",
      "av_visual_changes": "needs frames_on_disk, but visual capture is off for this program",
      "av_frame_region": "needs frames_on_disk, but visual capture is off for this program",
      "av_ocr_frame": "needs frames_on_disk; there are no frames",
      "av_read_screen": "needs capture_running; there is no screen on this workload",
      "av_program_stats": "its parser is a fixed game-bot key whitelist, unrelated to a queue worker",
      "av_source_at_error": "needs frames_on_disk, and Serilog records carry SourceContext rather than file:line, so there is nothing to anchor a source jump on"
    },
    "note": "Reviewed all 19 groups. Queue worker: fingerprinting repeated failures matters more than any single moment, and 24 tools came back n/a for this headless program."
  }
}
```

Validated against `bridge_plan.validate_plan`: accepted.

### 7.4 The `emitters: []` case, complete

Legitimate when the program already writes a structured log that the profile
already declares as a source. Watch the rationale wording — it must contain
`already` or `no log`.

```json
{
  "catalog_token": "REPLACE_WITH_YOUR_TOKEN",
  "emitters": [],
  "rationale": "already logs well: the service writes structured JSON via Serilog to /var/log/app/app.jsonl, which is registered as a log source and parses at 1.00 confidence, so adding an emitter would only duplicate it.",
  "adapters": {"app": "jsonl"},
  "visual_capture": false,
  "tools": {
    "primary": ["av_diagnose", "av_log_raw", "av_search"],
    "not_relevant": {"av_ui_tree": "needs accessibility_api, which a headless service cannot provide"},
    "note": "Log-only program, log-only tools."
  }
}
```

Validated: accepted. `why` may be omitted entirely when `emitters` is `[]`.

**Know the consequence:** with `emitters: []` the installer does not run at all,
so **no log sources are registered** by the commit. The commit records
`built.actions = ["no emitters requested — nothing written to the target program
(per plan.rationale)"]`. Only choose `[]` when the profile's `log_sources`
already point at a real, live file. Confirm that with
`catalog.existing_logs_found` before you commit, and with `av_log_where()`
after.

---

## 8. THE KNOWN GAP — `plan.emitters` does not drive installation

State this plainly to yourself before you plan: **the emitter ids you select are
recorded for audit, but they do not control what gets built.**

### 8.1 What was verified

- `installer.install_into_project(project_root, *, profile_name,
  install_python_hook, language)` takes **no emitter list**. The commit route
  calls it with the project root, profile name and language only.
- It scaffolds **one** language-appropriate emitter, chosen from the language
  alone.
- The string `swallowed_exceptions` — and `sys.monitoring` /
  `EXCEPTION_HANDLED`, the mechanism its catalog entry advertises — appear
  **nowhere** in the implementation. They exist only in `bridge_plan.py`'s
  option text and in tests.
- The same is true of `stdout_tee`, `logging_bridge`, `lifecycle`,
  `config_dropin` and `run_wrapper`: no code outside `bridge_plan.py` and its
  tests references any emitter id.
- `stdout_tee` is offered for **every** language and has no implementation of
  its own at all.
- Concrete case: the `abyss` plan named `["run_wrapper", "lifecycle"]`. The
  resulting `agentvision/manifest.json` records exactly one emitter:
  `{"language": "cpp", "kind": "tee", ...}`.

### 8.2 What this means in practice

| You select | You actually get |
| --- | --- |
| any non-empty list of ids | the single default emitter for the profile's language (Python: autoload hooks; Node/Ruby: preload shim; Java/.NET: config drop-in; compiled: the `agentvision run` tee) |
| `[]` | nothing installed, and no log sources wired |

So the only part of `plan.emitters` the installer reacts to is **empty vs
non-empty**.

The commit response is honest about this. It returns
`built.emitters_requested` (your list) separately from `built.install.emitter`
(what was actually produced), with `built.emitter_build_note` spelling out the
difference. Read both.

### 8.3 Two more recorded-but-not-enforced fields

Verified by grepping every use of these keys:

| Field | Recorded in the plan | Shown by `/bridge/report` and the GUI | Acted on |
| --- | --- | --- | --- |
| `capture.interval_seconds` | yes | yes | **no** — set the real interval with `av_capture_start(interval=...)` or `av_capture_set_interval(...)` |
| `visual_capture` | yes | yes | **no** — it does not stop the capture engine, and it does not change the catalog's tool verdicts (those come from the profile) |

Plan them honestly anyway: they are the record of your decision, and the next
session reads them. But if you want a 5-second interval, you must also call
`av_capture_start(interval=5.0)`.

### 8.4 What IS honoured

- **Adapter pins** per source label (`events` / `text` / `stdout`) — verified
  end to end on the `abyss` profile.
- **Whether any emitter is installed at all** (empty vs non-empty).
- **Log-source wiring**: on a non-empty plan, `agentvision/actions.jsonl` and
  `agentvision/log.txt` are added to the profile's `log_sources` if they exist
  and are not already registered, and the collector is reinitialised against
  them.
- **The profile's `language`** is filled in from `code_evidence.primary_language`
  if it was blank — but only when at least one new log source was added.
- **The plan record itself**, which is what `/bridge/report`, the GUI and every
  later session read.
- **The seal**, which is what stops the gate firing again.

---

## 9. Where the plan lives

```
<project_root>/agentvision/<sanitised_profile_name>/.av_bridge_plan.json
<project_root>/agentvision/<sanitised_profile_name>/.av_preflight_ok
```

`<sanitised_profile_name>` is the profile name with every character outside
`[A-Za-z0-9_-]` replaced by `_`. If the profile's `project_root` is blank or
does not exist, the folder falls back to AgentVision's own snapshots directory
instead.

Real example on this machine:

```
~/projects/AbyssEngine/agentvision/abyss/.av_bridge_plan.json
```

Persisted record shape (`bridge_plan.write_plan`):

| Key | Source |
| --- | --- |
| `version` | `1` |
| `sealed` | always `true` |
| `sealed_at` | ISO timestamp |
| `catalog_token` | the token the server expected, not necessarily the one you sent |
| `decided_by` | always `"agent"` |
| `emitters`, `adapters`, `capture`, `visual_capture`, `rationale`, `why`, `tools` | your plan |
| `built` | `{emitters_requested, actions[], emitter_build_note, install?, wire_error?, install_error?}` |

**This file survives everything.** Restarting the target program, the bridge
server, AgentVision, or your own agent session does not clear it. The gate fires
**once per program, ever.**

To inspect what was built later, use `GET /bridge/report`. It adds live
per-source facts the plan file does not have: `adapter_resolved`,
`detect_confidence`, `exists`, `bytes`, `last_write_age_s`, `stale`.

---

## 10. `replan` — semantics and when it is legitimate

`av_bridge_commit(plan={...}, replan=True)`, or `POST /bridge/commit` with
`{"plan": {...}, "replan": true}`.

### 10.1 What it does

- Without `replan`, a program whose `.av_bridge_plan.json` exists and says
  `sealed: true` returns `{"ok": false, "already_sealed": true, ...}` at HTTP
  200 and **nothing changes**.
- With `replan=True`, the existing check is skipped. Your plan is validated
  normally, the installer runs again if `emitters` is non-empty (it is
  idempotent), and `write_plan` **overwrites** the file. The previous plan,
  including its `why` and `tools` reasoning, is gone — there is no history.
- `replan` does **not** unseal anything. The program stays BUILT.

Pass a real boolean. The server parses it as
`str(body.get("replan") or "0") not in ("0", "", "false", "False")`, so `true`
works, `0` and `false` mean no — but odd values like `"no"` or `"FALSE"` would
be read as **yes**.

### 10.2 When replanning is legitimate

| Situation | Replan? |
| --- | --- |
| `state` is `BUILT` and `sealed_by_legacy_marker` is `true` — sealed by the old marker with no plan | **Yes** (and you do not even need the flag — see 2.3). Recording a real decision is a strict improvement. |
| The target program changed shape: a headless service grew a GUI, a Python module was rewritten in Rust, logging was replaced | **Yes.** The old plan's `why` entries now cite signals that no longer exist. |
| The user explicitly asks you to re-decide the bridge | **Yes.** |
| The previous plan was demonstrably wrong — you have evidence the bridge is blind to the actual failure (e.g. `av_log_where()` shows the program writes somewhere nothing reads) | **Yes**, and say so in the new `rationale`. |
| Your commit was rejected and you are retrying | **No.** A rejected commit never sealed anything. Fix the plan and commit without `replan`. |
| You got `already_sealed: true` and want your plan applied | **No** by default. That response is usually telling you the program was already correctly planned, possibly by an earlier session. Read `plan` in the response first. |
| You are unsure whether a plan exists | **No.** Call `av_bridge_status()` and read `state`. |
| You just want capture to start | **No.** If `state` is `BUILT`, call `av_capture_start()`. |

Before replanning, always re-fetch `av_bridge_catalog()`. The stored plan's
`catalog_token` is stale by definition once anything about the option set has
moved, and you need the current token anyway.

---

## 11. Traps

Read these once. Each one is a real behaviour of the current code.

### 11.1 `av_preflight` can seal the bridge behind your back

`av_preflight()` writes the legacy `.av_preflight_ok` marker whenever its
verdict is `ready: true`, or whenever you pass `accept_gaps=True`. That marker
alone makes `is_sealed()` true.

So calling `av_preflight` **before** `av_bridge_commit` on a never-bridged
program can flip it to `BUILT` with `sealed_by_legacy_marker: true` and
`plan: null` — the gate silently stops firing and no plan is ever recorded.

**Do the three bridge calls first.** Committing a plan also writes the preflight
marker for you (`built.actions` will say `"preflight marker written — the plan
supersedes the coverage-only check"`), so the preflight nudge in `av_status` and
`av_start_here` is satisfied by the commit. If you need `av_add_adapter` for a
log format, add the adapter first, then fetch the catalog, then commit.

### 11.2 `force=true` also skips the gate — and seals it badly

`av_capture_start(force=True)` bypasses the bridge gate. On a virgin program it
then writes `.av_preflight_ok`, which permanently seals the program as
legacy-BUILT with no plan. The `force` flag exists so a human clicking Start in
the GUI is never locked out of their own tool. **Do not use it to get past this
gate.**

`av_install_project(...)` has no `force` parameter in the MCP layer at all; only
the raw HTTP route accepts one.

### 11.3 `docs/MCP_TOOLS_REFERENCE.md` is generated

It is produced from `python_backend/api/tool_meta.json` by
`scripts/gen_tools_ref.py`. Never hand-edit it. Edit the JSON and regenerate.

### 11.4 78 of 90 tools have a recorded defect

`docs/MCP_TOOL_AUDIT.md` lists them, 12 of which are Class A — the tool actively
misleads its caller. The catalog surfaces the same text per tool as `caveat`.
Read the `caveat` before you put a tool in `primary`. Do not duplicate that
audit here; consult it.

---

## 12. Quick reference

```
GET  /bridge/status     -> {state, sealed, plan, sealed_by_legacy_marker, note, ...}
GET  /bridge/catalog    -> {catalog_token, emitters_available, adapters, mcp_tool_groups,
                            code_evidence, existing_logs_found, capture_settings, ...}
POST /bridge/commit     -> 400 {ok:false, errors:[...]}          (validation failed)
                        -> 200 {ok:false, already_sealed:true}   (nothing changed)
                        -> 200 {ok:true, sealed:true, plan, built}
GET  /bridge/report     -> what is actually built, plus live per-source adapter facts

Refusals from /capture/start and /install are HTTP 200 with
  "bridge_required": true  and  "started"/"installed": false.

The catalog is also a RESOURCE, with the same bytes and the same token:
  agentvision://catalog          -> read it without putting it in the transcript
  agentvision://frame/{seq}.json     one frame, no image bytes
  agentvision://frame/{seq}/region   only the pixels that changed
  agentvision://incident/{id}        one frozen failure window
  agentvision://log/raw{?from_offset}  the program's output, verbatim; PEEKS
```

Minimum viable plan — every required field, nothing optional:

```json
{
  "catalog_token": "REPLACE_WITH_YOUR_TOKEN",
  "emitters": ["lifecycle"],
  "why": {"lifecycle": "no signal marks run boundaries and the program exits silently, so start/exit/exit-code is the one thing nothing else records"},
  "rationale": "Minimal bridge: record that runs happen and how they end, nothing more.",
  "tools": {
    "primary": ["av_diagnose", "av_log_raw"],
    "not_relevant": {"av_run_tests": "hardcodes python -m pytest and this project has no pytest suite"},
    "note": "Narrow first pass; will revisit once there is output to read."
  }
}
```
