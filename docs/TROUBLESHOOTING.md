# AgentVision troubleshooting — symptom first

You are stuck. All you have is what a tool just returned. Find that observable
symptom in the index, go to the entry, do the fix.

Rules for using this page:

1. Match on the **symptom**, not on what you think the cause is.
2. Do the **Fix** steps in order. Do not skip the confirm step.
3. Never assume an AgentVision call failed because it returned `ok: false`.
   Several **successes** return `ok: false`. Entries T3 and T1 cover those.
4. If a tool's own docstring contradicts what the tool did, the docstring is
   wrong. See T15.

Companion documents:

| Document | What it holds |
|---|---|
| `docs/BRIDGE_PROTOCOL.md` | The full first-connection contract and plan schema |
| `docs/MCP_TOOL_AUDIT.md` | 77 known tool defects, 12 of which actively mislead |
| `docs/MCP_TOOLS_REFERENCE.md` | Every tool. **Generated** from `python_backend/api/tool_meta.json` by `scripts/gen_tools_ref.py` — never hand-edit it |

---

## Symptom index

| # | What you observed |
|---|---|
| [T1](#t1) | `av_capture_start` returned HTTP 200 but no frames are being captured |
| [T2](#t2) | `av_install_project` returned `installed: false` / `"error": "BRIDGE_NOT_BUILT"` |
| [T3](#t3) | `av_bridge_commit` returned `ok: false` with `already_sealed: true` |
| [T4](#t4) | `av_bridge_commit` was rejected with an `errors` list |
| [T5](#t5) | `av_capture_start` returned `preflight_required: true` |
| [T6](#t6) | The bridge gate fires **again** on a program I already planned |
| [T7](#t7) | Commit succeeded, but what got installed is not what I asked for |
| [T8](#t8) | A tool returned an empty list/object and I cannot tell why |
| [T9](#t9) | The log source exists but nothing new is in it (stale) |
| [T10](#t10) | Log lines parse, but the `source` / fields are nonsense |
| [T11](#t11) | Frames show the whole desktop instead of the program |
| [T12](#t12) | No frames at all, or frame sequence numbers have gaps |
| [T13](#t13) | `av_ui_tree` returned almost nothing |
| [T14](#t14) | Source search / tree / digest results are out of date or 404 |
| [T15](#t15) | A tool's docstring disagrees with what the tool actually did |
| [T16](#t16) | Every `av_*` tool returns `{"error": "URL error: ..."}` |
| [T17](#t17) | `program.stuck` records are gone from my program's log |
| [T18](#t18) | `running: true` for a program I know is not running |
| [H](#h) | How to tell AgentVision **itself** is healthy |
| [N](#n) | Things that look broken but are not |

Before anything else, if you have not called it yet this session:

```
av_start_here()
```

Read exactly two fields from it:

* `state.bridge_build.state` — `BUILT` or `PROVISIONAL`.
* `DO_THIS_NEXT` — the literal next call to make.

---

<a id="t1"></a>
## T1 — `av_capture_start` returned HTTP 200 but nothing is capturing

**Symptom.** The call did not raise. The body contains:

```json
{"ok": false, "started": false, "bridge_required": true,
 "bridge": {"state": "PROVISIONAL", "sealed": false, "blocked": ["capture/start", "install (emitters)"]},
 "guidance": "FIRST CONNECTION to this program — the bridge is NOT built yet, so capture will not start. ..."}
```

**Cause.** This program's bridge is not built. AgentVision refuses to choose the
logging for a program it has never seen. Until **you** commit a plan,
`av_capture_start` and `av_install_project` are refused. The refusal is HTTP
**200**, so a check on the status code alone reads it as success.

**Fix.** Run the three-call first-bridge sequence, in this order:

```
av_bridge_status()     # confirm state is PROVISIONAL
av_bridge_catalog()    # read the options; copy its catalog_token
av_bridge_commit(plan={...})   # your decision — see T4 for a complete plan
```

Then call `av_capture_start()` again.

**Confirm.** A real start returns `{"ok": true, "started": true, "interval": <n>,
"shots_per_second": <n>, "preflight_ok": true, ...}`. If `started` is not `true`,
capture did not begin.

**Do not** retry `av_capture_start` in a loop. The gate is not transient; the
identical call returns the identical refusal every time.

**Note on stale wording.** Some older notes say this refusal carries
`"error": "BRIDGE_NOT_BUILT"`. The live `/capture/start` refusal does **not**
include an `error` key. Key off `bridge_required` and `started`. (`error:
BRIDGE_NOT_BUILT` belongs to `av_install_project` — see T2.)

---

<a id="t2"></a>
## T2 — `av_install_project` returned `installed: false`

**Symptom.** HTTP 200, and the body contains:

```json
{"error": "BRIDGE_NOT_BUILT", "ok": false, "installed": false,
 "bridge_required": true,
 "DO_THIS_NEXT": "av_bridge_catalog()  ->  av_bridge_commit(plan=...)"}
```

**Cause.** Same gate as T1. Writing emitters into someone's program is the most
consequential thing AgentVision does, so it will not do it on a language guess.

**Fix.** Do **not** look for a way to force this tool. The MCP wrapper
`av_install_project` never sends `force`, so it cannot bypass the gate at all
(recorded in `docs/MCP_TOOL_AUDIT.md`). Instead:

1. `av_bridge_catalog()`
2. `av_bridge_commit(plan={...})` — **the commit itself performs the install**
   for the emitters you named. You do not need `av_install_project` afterwards.

**Confirm.** The commit response contains a `built` object. Look at
`built.install` and `built.actions`.

---

<a id="t3"></a>
## T3 — `av_bridge_commit` returned `ok: false` with `already_sealed: true`

**Symptom.**

```json
{"ok": false, "already_sealed": true, "plan": {"sealed": true, "emitters": ["..."], "...": "..."},
 "note": "This program's bridge is already built — the gate is first-connection only. Pass replan=true to deliberately re-decide."}
```

**Cause.** This is **not a failure**. The bridge for this program was already
built — possibly in a previous session, possibly by another agent. The gate fires
once per program, ever. `ok: false` here means "I did nothing", not "something
broke".

**Fix.** Nothing. Proceed to `av_capture_start()` and to normal work.

Only pass `replan=True` if you have a deliberate reason to throw away the
existing decision and re-decide. Re-planning rewrites the recorded plan.

---

<a id="t4"></a>
## T4 — `av_bridge_commit` was rejected with an `errors` list

**Symptom.** HTTP 400. Through the MCP tool the response looks like:

```json
{"error": "HTTP 400: BAD REQUEST", "status": 400, "ok": false, "sealed": false,
 "errors": ["plan.tools is required — ...", "..."],
 "catalog_token_expected": "510cd849c665fb8c",
 "next": "av_bridge_catalog() — then commit with its token"}
```

**Cause.** The plan failed validation. Every reason is listed in `errors`. Nothing
was written; the bridge is still `PROVISIONAL`.

**Fix.** Find each of your `errors` strings in the table below and apply its fix,
then commit once more with a corrected plan. Do not commit repeatedly hoping a
different field is at fault — the `errors` list is complete.

| The error says | Cause | Fix |
|---|---|---|
| `catalog_token is missing` | You committed without fetching the catalog | Call `av_bridge_catalog()` and copy its `catalog_token` verbatim into `plan["catalog_token"]` |
| `catalog_token ... does not match the current catalog` | The token is stale — the option set changed, or you reused a token from another program | Call `av_bridge_catalog()` again **now** and use the fresh token. `catalog_token_expected` in the rejection is the value it wants |
| `plan.emitters is required` | The `emitters` key is absent | Add `"emitters": [...]`. `[]` is legal, but read the next row |
| `plan.emitters must be a list` | You passed a string or dict | Pass a JSON list of emitter id strings |
| `plan.rationale is required` | Missing or blank `rationale` | Add one line on why this set fits this program |
| `plan.emitters is empty — if that is deliberate, say so in rationale` | `emitters` is `[]` and the rationale does not justify it | Keep `[]` and make the rationale contain the literal phrase `already` (e.g. `"already logs well: ..."`) or `no log`. Those substrings are what the validator looks for |
| `plan.why is required: {emitter_id: reason}` | `emitters` is non-empty and `why` is missing or is not an object | Add `"why": {"<emitter id>": "<reason>"}` |
| `plan.why is missing a reason for: [...]` | One selected emitter has no `why` entry | Add an entry for every id in `emitters`. The keys must match exactly |
| `plan.why entries for [...] are too short to be a reason` | A reason is 1–14 characters | Make every reason **15 characters or more** and tie it to something in `catalog.code_evidence` |
| `plan selects N emitters — that is close to everything on offer` | 6 or more emitters | Select fewer than 6. Keep only what the code evidence supports |
| `plan.tools is required` | The `tools` key is absent | Add the `tools` object shown below |
| `plan.tools must be an object, not a bare list` | You passed `"tools": ["av_diagnose"]` | Wrap it: `"tools": {"primary": ["av_diagnose"], "not_relevant": {...}}` |
| `plan.tools.primary is required` | `tools` exists but has no `primary` | Add `"primary": [...]` |
| `plan.tools.primary must be a list of tool names` | Wrong type | Pass a JSON list of tool-name strings |
| `plan.tools.primary lists N tools — that is a copy of the catalog` | More than 25 entries in `primary` | Cut it to the handful you would actually reach for. Every tool stays callable regardless |
| `plan.tools.primary is empty and no note explains why` | `primary` is `[]` with no `note` | Either name some tools, or add `"note": "<why you have no preference>"` |
| `plan.tools.not_relevant must be {tool: reason}` | You passed a list | Use an object: `{"av_ui_tree": "headless, no accessibility tree"}` |

**A complete plan that passes validation.** Replace the token with the one your
own `av_bridge_catalog()` call returned, and replace the emitters/reasons with
ones justified by that catalog's `code_evidence`. Everything else can be sent
as-is.

```json
{
  "catalog_token": "PASTE_THE_TOKEN_FROM_av_bridge_catalog_HERE",
  "emitters": ["lifecycle", "uncaught_exceptions"],
  "why": {
    "lifecycle": "nothing in this project marks run boundaries, so a silent early exit is invisible today",
    "uncaught_exceptions": "code_evidence.signals.threads shows worker threads, whose crashes never reach the main excepthook"
  },
  "rationale": "Node service that prints almost nothing; it needs run boundaries and crash reporting before any log is worth reading.",
  "adapters": {"text": "auto"},
  "capture": {"interval_seconds": 1.0},
  "visual_capture": false,
  "tools": {
    "primary": ["av_diagnose", "av_log_raw", "av_search", "av_error_moment", "av_session_report"],
    "not_relevant": {
      "av_ui_tree": "headless service, there is no window and no accessibility tree",
      "av_ocr_frame": "no visual capture for this program, so there is no frame to OCR"
    },
    "note": "chosen for a headless service: log-side tools only"
  }
}
```

Call it as `av_bridge_commit(plan=<that object>)`.

**Which emitter ids exist** depends on the target's language. Do not guess —
`av_bridge_catalog().emitters_available` lists them for this program. For
reference, the id sets are:

| Detected language | Emitter ids offered |
|---|---|
| every language | `stdout_tee`, `lifecycle` |
| python | those two `+ uncaught_exceptions`, `logging_bridge`, `swallowed_exceptions` (the last one needs Python 3.12+ at runtime; it is silent on older interpreters) |
| node, ruby | those two `+ uncaught_exceptions`, `logging_bridge` |
| java, `dotnet`, `csharp` | those two `+ config_dropin` |
| go, rust, c, cpp, shell, blank/unknown | those two `+ run_wrapper` |

**Confirm.** A successful commit returns `{"ok": true, "sealed": true, "plan":
{...}, "built": {...}, "next": "av_capture_start()"}`.

---

<a id="t5"></a>
## T5 — `av_capture_start` returned `preflight_required: true`

**Symptom.** HTTP 200, `started: false`, and the body has `preflight_required:
true` plus a `preflight` verdict object. `bridge_required` is **absent** — this is
the second, older gate, not the bridge gate.

**Cause.** The log-coverage marker for this program is missing. The marker file is
`.av_preflight_ok` in the profile's output folder. A normal
`av_bridge_commit` writes it for you, so you usually only see this if that file
was deleted, or if capture is being started on a profile whose plan lives
elsewhere (see T6).

**Fix.** Either close the coverage gaps, or accept them:

1. `av_preflight()` — read `ready` and `gaps`.
2. For each entry in `gaps`, build an adapter from that gap's `sample` line and
   register it with `av_add_adapter(...)` (see T10 for the argument shape).
3. `av_preflight()` again until `ready: true`.
4. `av_capture_start()`.

To accept the current coverage instead and start now, either of these works:

```
av_preflight(accept_gaps=True)   # records the gaps as accepted, then start normally
av_capture_start(force=True)     # start anyway, accepting current coverage
```

**Who writes the marker**, exactly:

| Call | Writes the marker? |
|---|---|
| `av_preflight()` | Only when `ready` is `true`. It tells you in `marker_written` |
| `av_preflight(accept_gaps=True)` | Yes, `marker_written: true` |
| `av_capture_start()` that actually starts | Yes |
| `av_capture_start(force=True)` | Yes |
| `av_bridge_commit(...)` on success | Yes |
| `av_capture_start()` that hit **this** gate | **No** |

That last row is the trap: hitting the gate computes the verdict but does not
persist it, so repeating the identical call replays the identical guidance
forever. Change something before you retry.

---

<a id="t6"></a>
## T6 — The bridge gate fires again on a program I already planned

**Symptom.** `av_bridge_status()` says `PROVISIONAL` for a program you (or an
earlier session) already committed a plan for.

**Cause.** The seal is a **file**, and it is looked for in one specific folder:

```
<project_root>/agentvision/<profile_name>/.av_bridge_plan.json
```

`<profile_name>` has every character outside `[A-Za-z0-9_-]` replaced with `_`.
When `project_root` is blank or does not exist on disk, the folder falls back to
AgentVision's own `snapshots/agentvision/<profile_name>/`. So the gate re-fires
when:

* a **different profile** is active than the one you planned (profile is global
  server state and other sessions can change it);
* the profile's `project_root` changed, moved, or is now unreachable — the seal
  is being looked for in a new folder;
* the plan file was deleted.

**Fix.**

1. `av_active_profile()` — the whole active profile, including `project_root`,
   `capture_app` and `log_sources`. Is this the program you think it is?
2. `av_bridge_status()` — `state`, `sealed`, `sealed_by_legacy_marker`, and the
   committed `plan` (which carries `emitters`, `why`, `rationale`, `tools`,
   `sealed_at` and `built`).
3. If the profile is wrong, do not silently switch it — other sessions share the
   active profile. Say what you found first.
4. If `project_root` is wrong, that is the bug. Fix the profile, not the gate.
5. If the plan genuinely is gone, re-run the three-call sequence (T1).

There is also a one-page per-program summary at `GET /bridge/report` (state,
plan, per-source adapters and staleness in one response). It is **not** exposed
as an MCP tool — the AgentVision GUI uses it. Through MCP, use
`av_bridge_status` + `av_active_profile` + `av_log_sources` instead.

**Also legal:** `sealed: true` with `plan: null` and
`sealed_by_legacy_marker: true`. That program was sealed by the older
coverage-only marker before plans existed. It is built; there is simply no plan
to read. Do not re-plan it unless you deliberately want to
(`av_bridge_commit(plan=..., replan=True)`).

---

<a id="t7"></a>
## T7 — Commit succeeded, but what got installed is not what I asked for

**Symptom.** `av_bridge_status().plan.emitters` lists `["run_wrapper",
"lifecycle"]`, but the project's `agentvision/manifest.json` describes a single
emitter (for example `{language: cpp, kind: tee}`).

**Cause.** This is a **known gap, not a misread.** `plan["emitters"]` is recorded
for audit; it does **not** drive installation. The installer
(`installer.install_into_project()`) takes no emitter list — it scaffolds **one**
language-appropriate emitter. Several catalog ids are facets of that same
emitter, which is what each option's `builds_as` field is telling you.

Related, same cause: `stdout_tee` is offered for every language but has no
separate implementation of its own.

**Fix.** Nothing to fix in the plan. Adjust your expectations and verify against
reality instead of against the plan:

* `built.emitters_requested` = what you asked for.
* `built.install.emitter` = the single emitter actually scaffolded.
* `av_install_verify(project_root="<path>")` = does the emitter really emit.

Note on `av_install_verify`: `verified` is defined as `emitter_works AND
autoloads`. It can be `false` while the emitter demonstrably works, if it only
works with env injected. Read `emitter_works` and `autoloads` separately.

**What the plan *does* control, verified:** adapters are honoured per log source.
A plan that pins `"abyssengine"` to a source really does pin it — visible in
`av_log_sources()` as that source's `configured_adapter` and `adapter`.

---

<a id="t8"></a>
## T8 — A tool returned an empty list/object and I cannot tell why

**Symptom.** `[]`, `{}`, `hypotheses: []`, `match: null`, `lines: []`, or all-zero
numbers. Nothing says whether that means *nothing happened* or *this is
misconfigured*.

**Cause.** This is a recurring, documented pattern across the tool surface: many
routes return the same empty shape for "no data" and "broken wiring". Some
routes carry a `hint` field; most do not.

**Fix — the disambiguation ladder.** Run these four in order. Stop at the first
one that explains the emptiness.

```
av_status()          # is the bridge live, which profile, frames_stored
av_log_sources()     # are there sources at all, do they exist, which adapter resolves
av_log_where()       # is the process writing somewhere nobody is reading
av_capture_status()  # engine_running, capturing, frame_count, health{}
```

Read them like this:

| What you see | What it means |
|---|---|
| `av_log_sources().source_count == 0` | Nothing is configured to read. Empty results are **configuration**, not silence |
| a source with `"exists": false` | The file is not there. Nothing can be parsed from it |
| a source with `"adapter": null` | Adapter unresolved because the file is missing |
| `av_log_where().missing_from_config` non-empty | The process writes to a path no source covers. You are reading the wrong file |
| `av_log_where().output_destination` says a terminal, a pipe, or `/dev/null` | The output is being discarded or is going somewhere no file read can see |
| `av_capture_status().frame_count == 0` | There are no frames. Every frame-based tool is empty for that reason |
| `av_capture_status().capturing == false` | The capture loop is not running. No new frames will appear |
| All four look healthy | The emptiness is real: nothing happened in that window |

**Tools known to be ambiguous when empty** (from `docs/MCP_TOOL_AUDIT.md` — check
the companion signal in the right column before believing the emptiness):

| Tool | Empty looks like | Check instead |
|---|---|---|
| `av_diagnose` | `hypotheses: []` + "program looks healthy" | `av_capture_status`, `av_program_status`, then `av_log_raw` |
| `av_metrics` | all-null series, no hint | `window_frames == 0` means no data |
| `av_state_at` | `match: null` + generic hint | Whether `action_log_file` is configured at all |
| `av_frame_alignment` | `aligned: true`, `leaked_after_shutter: 0` | `records_in_context == 0` means nothing was checked |
| `av_replay` | per-step `logs: []` | A broken log source is swallowed by a bare except; check `av_log_sources` |
| `av_program_log` | `lines: []` | It reads only the legacy `log_file` field, **not** `log_sources`. Use `av_log_raw` / `av_log_normalized` instead |
| `av_debug_log` | `lines: []` | Read failures are swallowed. The file may be unreadable, not empty |
| `av_program_stats` | `{}` or few keys | It is a hardcoded 9-key game-bot whitelist and silently drops everything else. `lines` is a dead parameter |
| `av_codebase_map` | `total_files: 0`, empty tree | A missing `project_root` yields this silently. Check `av_active_profile().project_root` |
| `av_log_range` | zero hits | `category` matching is exact and case-sensitive (`Error` never matches `error`); `source` is a substring test |
| `av_ui_tree` | `available: false` / near-empty | See T13. Often correct, not broken |

**Also beware the opposite failure:** a clean summary can be wrong. `av_diagnose`
has reported "no strong failure signals, program looks healthy" while 180
failures sat in the raw bytes. When a summary looks suspiciously clean, call
`av_log_raw()` and read what the program actually said.

---

<a id="t9"></a>
## T9 — The log source exists but nothing new is in it (stale)

**Symptom.** A source reports `"stale": true` with a large `last_write_age_s`.
The MCP call that shows this per source is:

```
av_log_raw(peek=True)
```

Each entry in its `sources` list carries `label`, `path`, `last_write_age_s`,
`stale`, `bytes_new` and `lines_total`. (`peek=True` means the session's read
offset is not advanced, so you can look without consuming the delta.) A Push Mode
message may also say it for you: `LAST WRITE 108727s AGO — stale?`.

**Cause.** One of:

1. The program is not running, so nothing is being written.
2. The program is running but writing **somewhere else**, and you are reading a
   file frozen at its last write.
3. The emitter is a `run_wrapper` and the program was **not** launched through
   the wrapper. A `run_wrapper` only captures output when the process is started
   through `agentvision run`. Launched directly, it captures nothing, forever,
   with no error.

`stale` means older than `STALE_LOG_SECONDS`, which is **120 seconds** by default
(`AGENTVISION_STALE_LOG_S` overrides it).

**Fix.**

1. `av_program_status()` — `running: false` explains everything. The log is stale
   because the program is not alive. Nothing is broken.
2. If it is running: `av_log_where()`. Look at `missing_from_config` (the process
   writes here and nothing reads it) and `not_written_by_proc` (configured, but
   this pid does not hold it open). Either fix the profile's `log_sources` to the
   real path, or relaunch the program so its output lands where the profile
   expects.
3. If the plan used `run_wrapper`, relaunch the target through the wrapper.
   Run this **from your program's own directory** — the wrapper resolves which
   bridged project it is instrumenting from the command it is given, so
   launching from elsewhere silently records nothing for your project:

```
python3 /path/to/AgentVision/python_backend/cli.py run -- ./your-program --your --flags
```

   (`python -m python_backend.cli` cannot be used here: it only imports from the
   AgentVision folder, which is not where your program is. `agentvision run --
   ...` works from anywhere if you ran `pip install -e .`.)

**Confirm.** Call `av_log_raw(peek=True)` again. That source's
`last_write_age_s` should now be small, `stale` should be `false`, and
`bytes_new` should be above zero.

**Caveat.** `av_log_where` is a GET but is not fully side-effect-free: the first
call in a session lazily constructs the global collector for the active profile.
That is expected and harmless.

---

<a id="t10"></a>
## T10 — Log lines parse, but the `source` or other fields are nonsense

**Symptom.** Normalized events carry a `source` that has nothing to do with your
program — a foreign subsystem name — or the message field contains text that
should have been split into `source` and line number.

**Cause.** Adapter detection picked the wrong adapter. Detection scores every
candidate against sample lines and the highest score wins. A generic pattern from
an unrelated ecosystem can score equal or higher than the right one.

Real, verified case: a C engine logging `[DEBUG] Sprite.c:32 - msg` was claimed by
the `coreboot_cbmem` adapter at 1.00 confidence, which reported
`source=coreboot` and buried `Sprite.c:32` inside the message.

**Fix.**

1. Prove it. Route one real line through the detector:

```
av_test_adapter(line="[DEBUG] Sprite.c:32 - player spawned")
```

Read `adapter`, `confidence`, `top_scores` and `is_fallback`.
`is_fallback: true` means no adapter specifically understands the format.

2. Register a correct adapter. When a **wrong incumbent** already claims the
format, you must name it in `outrank` — placement is what breaks a tie in your
favour:

```
av_add_adapter(
  name="myengine",
  extract_regex="^\\[(?P<level>[A-Z]+)\\]\\s+(?P<source>[\\w./-]+:\\d+)\\s+-\\s+(?P<message>.*)$",
  sample="[DEBUG] Sprite.c:32 - player spawned",
  anchor_tokens=["] ", " - "],
  family="game",
  language="c",
  outrank="coreboot_cbmem"
)
```

3. Re-test the same line with `av_test_adapter`. `adapter` must now be your
   adapter.

**Rules that will reject your adapter, so read them first.**

* The `sample` must route to your own adapter, or the add is rejected.
* If your pattern would steal another format's catalog sample, the add is
  rejected and the offending sample is returned. Tighten `extract_regex` or add
  `anchor_tokens` (literal substrings that must appear).
* `outrank` only breaks a **tie**. It cannot rescue a weaker pattern.
* Without `outrank`, a new adapter registers after all built-ins, so it can only
  beat the fallback adapters (`structural`, `generic_ts`, `raw`).
* Recognized named groups: `ts`, `level`, `source`, `message`. (`timestamp` and
  `message` are aliased to `ts` / `msg` for you.)

**Then pin it** so detection is not consulted again for that source: set the
adapter explicitly on the source in the profile, which is what
`plan["adapters"] = {"<label>": "myengine"}` does at commit time.

**Confirm.** `av_log_sources()` should show that source with
`configured_adapter: "myengine"` and `adapter: "myengine"` (`adapter` is the one
the merge actually uses).

---

<a id="t11"></a>
## T11 — Frames show the whole desktop instead of the program

**Symptom.** A frame is your entire screen — other apps, browser tabs — at full
screen resolution. `av_get_frame(seq)._ai.capture_health.capture_target` is
`"fullscreen"`.

**Cause.** The profile has **no `capture_app`** set. Targeting works in this
priority order:

| Priority | Condition | Result |
|---|---|---|
| 1 | window found for `capture_app` | the window only (`capture_target: "window"`) |
| 2 | a manual `capture_crop` rect | that rect (`capture_target: "crop"`) |
| 3 | **no `capture_app` named at all** | full screen (`capture_target: "fullscreen"`) |

With no `capture_app`, full screen **is** the requested target, so nothing warns
you.

(The other way to get a desktop frame is the escape hatch
`AGENTVISION_ALLOW_FULLSCREEN_FALLBACK=1`. With it unset — the default — a named
`capture_app` with no window causes the frame to be **skipped** instead. That is
T12.)

**Fix.**

1. `av_program_crop()` — read `capture_app`, `capture_crop`, `window_bounds`,
   `active_crop`. An empty `capture_app` and a null `window_bounds` confirm it.
2. Set `capture_app` on the profile to the app/window name of the target, or set
   an explicit `capture_crop`. The AgentVision GUI's window picker does this, and
   `av_create_profile` can too.
3. **Warning about `av_create_profile`:** it overwrites, it does not merge.
   Sending only `capture_app` resets every other field (`project_root`,
   `log_sources`, `language`, …) to defaults. Read the current profile with
   `av_list_profiles()` and resend the **complete** profile object with your one
   change applied.

**Confirm.** After the next capture tick,
`av_get_frame(<new seq>)._ai.capture_health.capture_target` should be `"window"`,
and `av_frame_json(<new seq>).size` should be window-sized, not screen-sized.

---

<a id="t12"></a>
## T12 — No frames at all, or frame sequence numbers have gaps

**Symptom.** `frame_count` stops rising; sequence numbers jump; frame-based tools
are empty.

**Cause and fix, in the order to check them:**

| Check | Reading | Meaning / fix |
|---|---|---|
| `av_capture_status().capturing` | `false` | The loop is not running. Start it (T1 first if the bridge is not built) |
| `av_capture_status().health.window_missing` | `true` | `capture_app` is set but has no window right now |
| `av_capture_status().health.frames_skipped_no_window` | rising | Frames are being **deliberately skipped**, not lost. A frame of the wrong program is worse than no frame, so AgentVision refuses to screenshot the desktop as a substitute. A gap in seq numbers means the program was showing nothing |
| `av_program_status().running` | `false` | The program is not alive. Start it |
| `av_capture_status().health.blank_frame_count` | rising | Frames captured but blank/black — window minimized, occluded, capture-blocked, or not yet painted. On macOS a minimized or occluded window normally still captures |
| `av_capture_status().last_error` / `.health.last_warning` | non-null | Read it. It usually names the exact cause |
| `av_debug_log(lines=100)` | | AgentVision's own log — capture errors appear here |

Also possible: frames existed and were **pruned**. Retention is a byte budget
with examine-before-delete, not a timer. Check `av_retention()` and
`av_frames_awaiting()`.

**A sequence number is unique only WITHIN a profile.** Two programs can each own a
frame 1 (measured: `a game bot` and `sharpemu` both do). The seq-keyed index therefore
holds ONE profile — the active one — and rebuilds when you switch profile. If a
seq from another program's report gives a 404, or gives you a different image than
you expected, that is why:

```
GET /frames/collisions          # which seqs exist in more than one profile
av_set_active_profile(<name>)   # index that program's frames instead
```

`av_get_frame` says this for you: when a seq belongs to another profile, the 404
body names the owning profile(s) and the switch to make, instead of a bare
"not found".

Before 2026-07-30 all profiles were hydrated into the one seq-keyed index, so
whichever program lost the dict race had that frame silently unreachable while
`/frame/<seq>` answered with the other program's image.

---

<a id="t13"></a>
## T13 — `av_ui_tree` returned almost nothing

**Symptom.** `available: false` with a `reason` and a `fallback`, or a tree with
0–2 elements plus `likely_custom_drawn: true`.

**Cause.** **This is a legitimate answer, not a bug.** Custom-drawn UIs expose no
accessibility tree at all: games, emulators, canvas/WebGL apps, Dear ImGui, most
SDL/OpenGL windows. There is nothing to read because the app draws its own
pixels.

Other legitimate causes of a thin tree: unlabelled controls are absent even when
visible; macOS needs the Accessibility permission; Linux needs `pyatspi` and a
running at-spi2; traversal is deadline-bounded at 2.5 s so a wedged app truncates
rather than hanging.

**Fix.** Stop trying to get a tree. Use pixels, cheapest tier first:

```
av_frame_json(seq)            # JSON description, no image bytes
av_ocr_frame(seq)             # on-screen text, if an OCR backend exists
av_frame_region(seq)          # only the pixels that changed
av_get_frame(seq)             # the full PNG — last resort
```

A tree also cannot answer questions about icon colour, spatial layout
correctness, progress-bar fill, or rendering corruption. Those need pixels no
matter what the tree returns.

**Two extra traps on this tool.**

* Check `cost.verdict`. On a deep, list-heavy window the tree can cost **more**
  than the screenshot it replaces.
* `max_nodes` used to mutate a module-level global — not per-request, not
  thread-safe, never reset — so one custom call changed the default for every
  later call in the process. **Fixed 2026-07-30:** the override is applied under a
  lock and restored in a `finally`, and the response reports `max_nodes_applied`
  plus the unchanged process default.

---

<a id="t14"></a>
## T14 — Source search / tree / digest results are out of date or 404

**Symptom.** `av_source_search` misses a symbol you know exists.
`av_source_tree` / `av_source_light` / `av_source_digest` show a layout that no
longer matches the project. Or a call returns `{"error": "index not built — POST
/source/refresh"}`.

**Cause (fixed 2026-07-30).** All of these read a cached index,
`source_index.json`. It used to be built **only when entirely missing** and never
diffed against file mtimes, so edits stayed invisible until an explicit refresh —
a stale mirror of the code you are actively editing, presented as authoritative.
It is now compared against the mtime of every indexed-eligible file (a stat-only
walk, cached for 5 s) and rebuilt when the project has changed; the response says
`rebuilt_because`. An explicit `av_source_refresh()` is still the way to force it,
and is still worth calling if you suspect the check itself.

Precisely which staleness bites which tool:

| Tool | What it serves | Effect of a stale index |
|---|---|---|
| `av_source_tree`, `av_source_light`, `av_source_digest`, `av_source_list` | cached JSON | Fully stale content — old layout, old symbols, old line counts |
| `av_source_search` | file **list** from the index, contents read live from disk | Finds new text in **known** files, but is blind to files created or renamed since the index was built |
| `av_source_file` | reads the file from disk directly | Always current |

**Fix.**

```
av_source_refresh()
```

Then re-run your query. Do this once after any batch of edits, and at the start
of a session on a project you edited outside this session.

**Related caveats.** `av_source_light`'s per-file `summary` is only populated for
Python and Markdown despite the docstring implying all files.
`av_source_search`'s size skip only applies to `lang == "other"` files over 5000
lines, so a multi-megabyte `.py` or `.json` is read in full on every matching
search. `av_source_file` has no server-side size cap — pass `to_line` on large
files.

---

<a id="t15"></a>
## T15 — A tool's docstring disagrees with what the tool actually did

**Symptom.** You followed a tool's documented behaviour and got something else.

**Cause.** Docstrings were written from intent; several were measurably wrong
about their own behaviour. This was audited: **78 of 90 tools** have a recorded
defect or caveat, and **12** actively misled their caller. All 12 of those were
fixed on 2026-07-30 — each with a named test — but the ordering below still holds,
because it is what to do when the next one turns up.

**Fix.** Trust this order, highest first:

1. The handler code in `python_backend/api/bridge_server.py`.
2. `docs/MCP_TOOL_AUDIT.md` and `python_backend/api/tool_meta.json` — both derived
   from the handler, with disagreements recorded.
3. `docs/MCP_TOOLS_REFERENCE.md` — generated from `tool_meta.json`. Never
   hand-edit it; regenerate with `scripts/gen_tools_ref.py`.
4. The tool docstring, last.

**The 12 that misled — all 12 fixed on 2026-07-30.** The table is kept because the
old behaviour is what you will find in any transcript, report or memory written
before that date, and because each row names what to re-check if you suspect a
regression. Every row's fix is pinned by a test named in
`docs/MCP_TOOL_AUDIT.md`; `tool_meta.json`'s `code_note` fields carry the current
status, and that — not this table — is what an agent is served at runtime.

| Tool | What it used to get wrong (now fixed) | What it does now |
|---|---|---|
| `av_selftest` | False green — the `input_daemon` check hardcodes `ok: true`, so a dead daemon never fails | `ok` is derived from a measured `running` plus whether the profile asked for the daemon; a wanted-but-dead daemon fails the selftest |
| `av_new_errors_this_session` | Fingerprint history is never persisted; returns everything since session start, no dedupe | history is persisted, the answer is idempotent within a session, and each fingerprint appears once with a `count` |
| `av_overview` | Fetches `/latest` just to read a sequence number, inflating the saving `av_token_report` claims | calls `/latest/pointer`, which neither counts a frame read nor marks the frame examined |
| `av_log_push` | Claims pushed records appear in `av_log_range` / `av_actions_around_frame`. They do not — it writes plain text to `activity.log`, and `category` / `source` / `data` are discarded | writes a STRUCTURED record to AgentVision's own observer log (`av_observer_log`), never into the program's action log |
| `av_delete_profile` | Docstring says it cannot remove the active profile. It can. Deleting the active custom profile succeeds and later lookups fall back to a blank default | 409 with `deleted: false` and the fix to apply; the profile survives |
| `av_ui_tree` | `max_nodes` mutates a module-level global (see T13) | `max_nodes` is per request, under a lock, restored in a `finally` |
| `av_program_stats` | Documented as generic metrics; actually a hardcoded 9-key game-bot whitelist. `lines` is dead | every `key: value` pair is returned (legacy keys kept as aliases), borders stripped, and `lines` limits the read |
| `av_install_project` | Silent no-op on an unsealed profile — the wrapper never sends `force` (see T2) | takes and forwards `force`; the docstring says the gate answers 200 so you must read `installed` |
| `av_timeline` | Events with no parseable `ts_ms` become `0.0`, sort to the front as oldest, and are dropped first by the limit | untimestamped rows keep `ts_ms: null`, are flagged, and get their own reserved share of the row budget |
| `av_errors_by_fingerprint` | Substring-matches `fail`/`error`/`crash` in `source` with no word boundary, so `FailoverManager` pollutes the histogram | word-boundary match, and an explicit INFO/DEBUG level outranks a guess from the module name |
| `av_source_digest` | Stale forever until an explicit `av_source_refresh` (see T14) | the index is diffed against file mtimes and rebuilt when the project changed |
| `av_wait_for` | A dead branch in condition inference; the `anomaly` condition takes no call-time baseline, so a pre-existing anomaly reports as freshly matched | the default is reported as a default, and `anomaly` takes a t0 baseline |

Also worth knowing, and the reason a rejected commit can surprise you:
`av_bridge_commit`'s docstring historically omitted the required `why` field.
`bridge_plan.validate_plan()` hard-requires it. T4 has the real schema.

---

<a id="t16"></a>
## T16 — Every `av_*` tool returns `{"error": "URL error: ..."}`

**Symptom.**

```json
{"error": "URL error: [Errno 61] Connection refused",
 "url": "http://127.0.0.1:7771/status",
 "hint": "Is bridge_server.py running on http://127.0.0.1:7771 ?"}
```

Every tool fails the same way, including read-only ones.

**Cause.** No MCP tool does anything locally. They are all thin HTTP clients for
the bridge server. Default base URL is `http://127.0.0.1:7771`, overridable with
the `AGENTVISION_BRIDGE_URL` environment variable. So either the server is not
running, or it is on a different host/port than the MCP client is using.

**Fix.**

1. Check whether it is listening:

```
curl -s http://127.0.0.1:7771/status
```

2. If that connects but your tools do not, your MCP client has a different
   `AGENTVISION_BRIDGE_URL`. Align them.
3. If nothing is listening, start the server from the AgentVision repo root:

```
python3 python_backend/api/bridge_server.py --no-autocapture
```

Useful flags: `--port <n>` (default `7771`), `--host <addr>` (default
`127.0.0.1`), `--profile <name>`, `--no-autocapture` (start the bridge without
the capture engine; capture then begins only on an explicit
`av_capture_start`).

4. A per-call `{"error": "HTTP 404", ...}` for **one** tool while others work is
   not this problem — that is a missing frame/bookmark/id, not a dead server.

**Timeout note.** The MCP client's HTTP timeout defaults to 5 seconds
(`AGENTVISION_HTTP_TIMEOUT`). A timeout error on a heavy call (a large digest, a
whole-project search) is a slow response, not a dead server.

---

<a id="t17"></a>
## T17 — `program.stuck` records are gone from my program's log

**Symptom.** A project's `actions.jsonl` used to contain `program.stuck` records
from `agentvision.watchdog`. It no longer does. `av_log_range` shows fewer
records than it used to.

**Cause. That was AgentVision contaminating the file it was reading, and it was
stopped on 2026-07-30.** Measured on one real project:

```
total records in actions.jsonl   2123
written by agentvision.watchdog  2024   (95%)
written by the program             99
newest program record         48.8 h old
newest watchdog record         0.0 h old  (still being appended)
watchdog records ever matched by the failure detector:  0
```

The in-code justification for the write was "so existing bookmark detection picks
it up". It never did — the last line above is the measurement. Worse, the watchdog
compared each check against the newest record of ANY source, including the one it
had just written, so it reported `silent_s: 60.3` about a program that had been
silent for 48.8 hours, once a minute, 2024 times. A reader took those records as
evidence the program was hung.

**Where they are now.**

```
av_observer_log()                       # or GET /observer/log
log/observer/<profile>.observer.jsonl   # AgentVision's own sink
```

Two behaviour changes came with the move, both deliberate:

* silence is measured against **program-emitted** records only (`agentvision.*`
  excluded), so `silent_s` means what it says — and logs already contaminated by
  the old behaviour now read correctly too;
* a process that has **exited** is not "stuck". Exit is recorded once as
  `program.exited` and then the thread goes quiet, instead of alerting forever.

`av_log_push` moved with it: your waypoints are structured records in the observer
log, not plain lines in the program's log.

**If you want the old records back,** they are still in the file's history — the
move only stopped new writes. Nothing deleted what was already there.

---

<a id="t18"></a>
## T18 — `running: true` for a program I know is not running

**Symptom.** `av_program_status` says `running: true` with no CPU or RAM beside
it. Frames keep being captured of a window that never changes. Every frame
sidecar records `"running": true`.

**Cause (fixed 2026-07-30).** The process matcher accepted any process whose
command line merely **mentioned** the project. Verified on a real profile
(`process_name="SharpEmu"`, `project_root=".../<project>"`): `is_running()`
returned `true` while a `cat` of a file in the project ran, while `less` had its
log open, while an editor had a file open, and for a plain `/bin/zsh -c` command
whose argv contained the project path. Capture gates on exactly this, which is how
12,921 frames came to exist for a process that had exited.

**What changed.** A match now needs the process to LOOK like a launch of the
program: `argv[0]` must not be a shell or a file-visiting utility, and some
argument must name the program itself rather than a file or folder beside it. The
strong signals are unchanged (an executable under the project root, or a process
literally named `process_name`).

**What to check.**

```
av_program_status()      # liveness_evidence: {pid, process_name, exe, matched_by}
                         # contradiction:     set when the claim disagrees with
                         #                    the program's own silence
av_capture_status()      # health.frames_while_program_silent
```

`matched_by` is the rule that fired: `exe-under-project-root`, `process-name`,
`runtime+script-argument`, `script-argument`, `generic-name+project-path`,
`project-path-only`. The weaker the rule, the more corroboration you should want.
The frame sidecar carries the same evidence under `program.running_evidence`, so a
stored frame can be checked after the fact — which the old bare boolean made
impossible.

**Capture is still not stopped by log silence.** An idle GUI is legitimately
silent, so `frames_while_program_silent` is counted and warned about rather than
acted on. A large number means the window may no longer be updating.

---

<a id="h"></a>
## H — How to tell AgentVision itself is healthy

Run one call:

```
av_selftest()
```

It probes the runtime paths on this machine and returns
`{schema_version, os, ok, failed_checks, checks, note}`. Each entry in `checks`
is `ok: true | false | null` — `null` means "not applicable on this OS", which is
not a failure. `ok` at the top level is simply "no check returned `false`".

| `check` value | Proves |
|---|---|
| `capture` | Screen capture works and is non-blank |
| `window_enum` | Window enumeration works for the profile's `capture_app` |
| `input_hooks` | OS input hooks fire (Windows spawns a `SendInput` probe; Linux uses evdev via the daemon). `null` on macOS — there is no one-shot probe there |
| `linux_session` | Linux session prerequisites (Linux only) |
| `input_daemon` | Nothing. See below |

**Known false green — do not trust this one check.** The `input_daemon` entry
hardcodes `"ok": true`. A dead daemon therefore never appears in `failed_checks`
and never flips the overall `ok` to `false`. The same entry does carry a truthful
`running` field, so read `checks[] where check == "input_daemon"` → `running`,
not its `ok`. Better, call:

```
av_daemon_status()
```

and read `running` and `pid` yourself. That is also the answer to "why aren't
keys/clicks showing up in the JSONL?" — plus the per-profile
`capture_user_input` opt-in, which is off unless enabled.

**The rest of the health picture**, three more cheap calls:

| Call | Read these fields |
|---|---|
| `av_status()` | `status`, `active_profile`, `program`, `frames_stored`, `preflight.ok` |
| `av_capture_status()` | `engine_running`, `capturing`, `frame_count`, `last_error`, `health{...}` |
| `av_bridge_status()` | `state`, `sealed`, `plan.emitters` — is this program's bridge built |
| `av_log_sources()` | per source: `exists`, `adapter`, `detected_adapter`, `detect_confidence` |

For a fuller host-side report including the emitter auto-injection round-trip,
`agentvision doctor` runs on the host (not through MCP).

Two side effects worth knowing, both harmless: the first `av_status()` or
`av_log_where()` of a session lazily constructs the collector and creates the
profile's output folder, even though you only asked for status.

---

<a id="n"></a>
## N — Things that look broken but are not

Do not spend calls "fixing" these.

| Observation | Why it is fine |
|---|---|
| `av_bridge_commit` → `ok: false, already_sealed: true` | The bridge is already built. Proceed (T3) |
| `av_bridge_status` → `sealed: true, plan: null` | Sealed by the older legacy marker. Built, just no plan to read (T6) |
| `av_ui_tree` → `available: false` or `likely_custom_drawn: true` | The app draws its own UI. Use pixels (T13) | `max_nodes` is per request, under a lock, restored in a `finally` |
| Gaps in frame sequence numbers | Frames were skipped because the program had no window — deliberate, not data loss (T12) |
| `ocr_text: null` with `ocr_unavailable` | OCR is optional. No backend installed; read the image or the tree instead |
| `plan.emitters` lists more ids than the project's manifest shows | One emitter covers several ids. Known and expected (T7) |
| A stale log while `av_program_status().running` is `false` | Nothing is writing because nothing is running (T9) |
| `av_capture_start` refused on a brand-new program | The one-time setup step, not a failure (T1) |
