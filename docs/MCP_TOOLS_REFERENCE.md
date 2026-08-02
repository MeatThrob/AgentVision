# AgentVision MCP tool reference

**Generated from `python_backend/api/tool_meta.json`. Do not edit by hand —
run `scripts/gen_tools_ref.py`.** 90 tools in 19 groups.

Every description here was derived by reading the tool's implementation and
its HTTP handler, not its docstring. Where the two disagreed, the handler won
and the disagreement is recorded as a caveat.

## How to read an entry

- **needs** — hard preconditions. A tool whose `needs` your target cannot
  satisfy will not help you. `none` means it always works.
- **cost** — `free`/`low` return JSON. `high` returns a full image; reach for
  it last (see the token rule in `AI_START_HERE.md`).
- **languages** — `any` unless the tool depends on a language mechanism.
- **caveat** — a known defect or limit found in the code. Trust this over the
  tool's own docstring.

### Precondition tokens

| token | meaning |
|---|---|
| `none` | no precondition |
| `capture_running` | the screenshot timer must be started |
| `frames_on_disk` | at least one frame captured |
| `log_source_any` / `_events` / `_text` | a declared, existing log source |
| `adapter_with_level` | the adapter must extract a severity level |
| `adapter_with_file_line` | the adapter must extract `file:line` |
| `source_index` | the target's source has been indexed |
| `ocr_backend` | an OCR engine is available |
| `accessibility_api` | OS accessibility permission + a real window |
| `gui_program` / `window_visible` | the target must have a window |
| `input_daemon` | the input-recording daemon must be running |
| `bridge_sealed` | the bridge must already be BUILT |

## `start` — 1 tool(s)

*Call this before anything else.*

### `av_start_here`

**Returns:** Returns a small orientation payload: what's bridged, liveness, OCR/daemon state, workflow hints

**Use when:** At the start of any session where you might need to observe a running program, even before you know whether AgentVision is set up for it.

GET /start_here reads the active collector/profile, the auto-capture engine's running/capturing flags and interval, in-memory frame count and latest sequence, the daemon status dict, an OCR-availability probe, the retention ledger's byte budget and awaiting-examination count, and the last 3 visual events. It also calls two small helpers, _bridge_build_hint and _preflight_hint, to tell the agent up front whether this program has been bridged/preflighted at all. No frame pixels or log lines are read; everything comes from already-maintained in-memory state.

**Why not do it by hand:** Answers 'is anything even watching, and is it safe to proceed' in one tiny call instead of guessing or calling av_capture_start blind and hitting an unexplained refusal.

needs: `none`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Not a substitute for av_preflight before a first capture on a new program, or av_diagnose when something is actually wrong — it only reports current state, it does not analyze frames or logs.

> ⚠ **Caveat (from the code):** The assignment's backend range (bridge_server.py 7606-7774) overshoots the handler: the /start_here route itself ends at line 7698 (return jsonify(...), 200); lines 7701-7774 are the '__main__' CLI entry point (argparse, profile bootstrap, app.run), unrelated to this route.

## `bridge_setup` — 5 tool(s)

*FIRST-CONNECTION ONLY. This is how a program gets bridged. Read this group first if state is PROVISIONAL.*

### `av_bridge_status`

**Returns:** Whether this program's bridge plan is BUILT (sealed) or still PROVISIONAL, and what is blocked.

**Use when:** As the very first call on a program AgentVision has not been told about, before attempting av_capture_start or emitter install, to find out whether av_bridge_catalog/av_bridge_commit must run first.

GET /bridge/status reads the per-program plan file (.av_bridge_plan.json in the profile's output folder) via bridge_plan.read_plan, and separately checks a legacy marker file (.av_preflight_ok) via bridge_plan.legacy_sealed for backward compatibility with bridges built before this gate existed. It returns state (BUILT/PROVISIONAL), sealed (bool), the raw plan dict if one exists (may be None even when sealed via the legacy marker), sealed_by_legacy_marker, and, if provisional, blocked:["capture/start","install (emitters)"] plus next: av_bridge_catalog(). No filesystem writes happen; this is a pure state read.

**Why not do it by hand:** Collapses a file-existence + precedence check (plan JSON vs. legacy marker vs. neither) into one call; without it the agent has no other way to see .av_bridge_plan.json / .av_preflight_ok inside the profile's output folder, and would otherwise only discover the PROVISIONAL gate by having capture/install calls refused.

needs: `none`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Does not enumerate what CAN be built (that is av_bridge_catalog) or what IS built with per-source adapter resolution (that is av_bridge_report). When sealed purely by the legacy marker with no real committed plan, the returned plan field is None even though state is BUILT.

> ⚠ **Caveat (from the code):** bridge_plan.status() sets plan = read_plan(folder), which is None whenever there is no .av_bridge_plan.json on disk — this is true even when sealed=True via the legacy marker. A caller that assumes plan.emitters exists whenever state=="BUILT" will get nothing in the legacy case; the human-readable 'note' field mentions this ('call av_bridge_commit(replan=True)...') but the plan field itself gives no structured signal beyond being null.

### `av_bridge_catalog`

**Returns:** Full option menu (emitters, adapter families, tool groups, readers, code-derived signals) plus a catalog_token required to commit a plan.

**Use when:** When av_bridge_status reports PROVISIONAL, immediately before drafting the plan for av_bridge_commit; also called again to refresh catalog_token if a previous commit was rejected as stale.

GET /bridge/catalog calls _bridge_catalog_body(), which assembles: mcp_tool_groups from _tool_catalog_groups(), source_readers from log_sources.list_readers(), and existing_logs_found by checking each configured log source path for existence/size via log_sources.effective_sources(). These plus the active profile's language are passed into bridge_plan.catalog(), which adds emitters_available (per-language emitter options with capture/cost from a hardcoded table), adapters (family name -> count, top 40, computed live from the log_adapters.REGISTRY, not the raw 650+ names), and code_evidence from bridge_plan.code_signals() — a bounded regex scan (cap 400 files, 400KB/file, skips node_modules/.git/venv/dist/build/vendor/etc.) of the project_root for patterns like GUI toolkits, web frameworks, threading/async, subprocess use, existing logging, print-only code, and swallowed exceptions (except: pass/return None/continue), each reported with a hit count and up to 5 example files. It appends capture_settings guidance, you_must_decide / do_not checklists, and finally computes catalog_token = sha256(...)[:16] over a stable subset of the body (version, language, sorted emitter ids, adapter total, sorted tool-group keys) so a later av_bridge_commit can be rejected if the options changed since the token was issued.

**Why not do it by hand:** Replaces the agent hand-grepping the target repo for GUI/web-framework/threading/subprocess/broad-except signals and hand-listing available emitters/adapters/tool groups: code_signals() runs those regex passes once, bounded, and returns counts with example file paths the agent can cite directly as 'why' when building the plan, instead of re-deriving the same evidence from raw source reads.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Read-only planning aid, not a build step (no bridge is created by calling this) and not a status check on an already-built program (see av_bridge_report for what is actually installed). code_evidence is bounded and can miss signals in repos over 400 matched files, files bigger than 400KB, or code living entirely under a skipped directory name (node_modules, vendor, dist, build, target, obj, bin, site-packages, .venv, venv, .tox, .idea, .vscode, agentvision).

> ⚠ **Caveat (from the code):** The catalog body reports LIVE counts (adapters from len(la.REGISTRY) and la.builtin_names(), source_readers from log_sources.list_readers(), mcp_tool_groups from _tool_catalog_groups()), so what this call returns is always current. Prose counts written into docstrings and .md files are NOT sourced from this call and have drifted before — this caveat itself used to quote a bridge_plan.py docstring that said '650+ log adapters, ~86 MCP tools', wording that no longer exists. When a number matters, take it from this response or av_capabilities(), never from prose. api/test_doc_counts.py now fails the suite when tracked prose disagrees with the registry.

### `av_bridge_commit`

**Returns:** Seals the first-connection bridge plan and scaffolds only the chosen emitters into the project

**Use when:** On first connection to a program whose bridge is not yet built (av_bridge_catalog() has just been reviewed), to commit which emitters/adapters/capture settings to install; or with replan=true to deliberately redo an existing sealed plan.

POSTs the agent's plan (catalog_token, emitters list, adapters map, capture/visual_capture settings, rationale, why) to /bridge/commit. Validates the token against the current av_bridge_catalog() output and requires a per-emitter 'why' justification (>=15 chars) tied to code evidence, rejecting empty/blanket (>=6 emitter) selections. If valid and emitters is non-empty, calls installer.install_into_project() to write a per-language agentvision/ folder (actions.jsonl, log.txt, emitter code) into profile.project_root, then registers those two files as log_sources on the profile (using the plan's adapters, defaulting to jsonl/auto) and re-inits the log collector. Writes the sealed plan JSON to the profile's plan folder and also writes a preflight-passed marker so capture is not double-gated.

**Why not do it by hand:** It is the only path that actually writes emitter code and registers log sources for a new program — without it, later capture/install calls stay refused (bridge_required) since /capture/start and /install both hard-gate on bridge_plan.is_sealed(). It also wires the emitter's output files into the profile's log_sources automatically, which a manual edit would otherwise have to do by hand.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Not for programs whose bridge is already sealed unless replan=true is passed (otherwise it returns already_sealed with no action). Not useful with emitters:[] beyond recording that decision — it writes nothing to the target program in that case. If project_root is missing/not a directory, no emitter files are written even with a non-empty emitters list, and this is only reported inside built.actions as a string, not as a top-level error.

> ⚠ **Caveat (from the code):** The av_bridge_commit docstring in claude_mcp.py (lines 1519-1551) documents the plan shape as {catalog_token, emitters, adapters, capture, visual_capture, rationale} and never mentions a 'why' field. But bridge_plan.validate_plan() (bridge_plan.py lines 433-454) hard-requires plan.why = {emitter_id: reason} for every non-empty emitters list, rejecting the commit otherwise. An agent following only the av_bridge_commit docstring would omit 'why' and get rejected with an error the docstring gave no warning about.

### `av_install_project`

**Returns:** Scaffolds AgentVision's output-side files into a project, but only once its bridge plan is sealed.

**Use when:** On first bridging a new project's output side, and only after av_bridge_catalog() + av_bridge_commit() have already sealed a plan for the active profile — calling it earlier just returns the bridge_required refusal instead of installing.

Detects the project's language via connectors.log_sources.detect_language (or uses the explicit `language` arg), then calls installer.install_into_project to idempotently create `<project_root>/agentvision/` holding actions.jsonl and log.txt sinks, state.json, stats/ and crashes/ dirs, and a per-language emitter (Python: project-root sitecustomize.py true-autoload; Node/Ruby: env-preload shim; Java/.NET: logger config drop-in; Go/Rust/C++/shell/php: stdout+stderr tee via `agentvision run --`). manifest.json, README.md and the emitter files are rewritten every call; existing sink/config files are left untouched. It also appends AgentVision's paths to .gitignore if one exists. Before any of that, the /install route checks `bridge_plan.is_sealed()` for the currently active profile; if not sealed it returns `{ok:false, installed:false, bridge_required:true, bridge:<status>, guidance:...}` and writes nothing.

**Why not do it by hand:** When it actually runs, it replaces hand-writing a sink folder, a language-specific autoload hook, a manifest, and .gitignore entries with one idempotent call covering ~9 language families from one installer module — real time saved versus doing each by hand. But most calls, as issued via this MCP tool, do none of that work at all (see code_note).

needs: `bridge_sealed`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Not for launching or attaching to the program itself (`agentvision run -- <cmd>` is the front door for that) and not a way to review or choose which emitters/adapters get installed — that decision belongs to av_bridge_catalog/av_bridge_commit, which this tool cannot bypass from the MCP surface.

> ⚠ **Caveat (from the code):** FIXED (2026-07-30). The tool takes force: bool = False and forwards it, so the seal gate is reachable from the tool instead of being an unexplainable installed:false. The docstring now states that the gate returns HTTP 200 and that callers must check `installed`.

### `av_install_verify`

**Returns:** Proves the bridge's output side actually emits events for this project's language

**Use when:** Right after av_install, before trusting that capture will produce log data — to learn whether the program can just be run normally or must be launched via `agentvision run -- <cmd>`.

Reads agentvision/manifest.json for language+emitter, then checks agentvision/actions.jsonl. For python/node/ruby it spawns a tiny no-op subprocess with the emitter's auto-load env injected (PYTHONPATH/NODE_OPTIONS/RUBYOPT) and tails the sink for a new av.<lang>.* or av.* record; it then re-runs the SAME probe with that env stripped to test whether the emitter auto-loads on a plain launch. For any other language (java/.net/go/rust/...) it skips execution and does a static check instead: agentvision/emitters/ has files and the sink is writable.

**Why not do it by hand:** Replaces manually launching the program and eyeballing actions.jsonl for a new line. Its real value is the second, env-stripped probe: it distinguishes 'the emitter code works' from 'the emitter loads automatically', a distinction a human would not normally think to test and which the tool itself found silently wrong in practice (comment in the code describes measuring verified=true/events_seen=2 while a plain `python main.py` emitted nothing before this check existed).

needs: `none`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Not a runtime guarantee for compiled/config languages (java/.net/go/rust/...): those only get a static files-present+writable check, never a real emitted-event proof, since verification there requires the real program to run. Also not instant — for python/node/ruby it runs two subprocesses back-to-back, so wall time can approach 2x the `timeout` parameter (default 12s).

> ⚠ **Caveat (from the code):** The claude_mcp.py docstring undersells the route: it advertises the return shape as {verified, mode, language, events_seen, last_event, sink, stderr} and describes only the single env-injected probe, but the actual handler (bridge_server.py ~3600-3766) also runs a SECOND bare-env probe and returns additional fields not mentioned in the docstring: emitter_works, autoloads, autoload_detail, launch_command, before_size. Critically, `verified` is defined as `emitter_works and autoloads` -- i.e. it can be False even when the emitter demonstrably works, if it only works with the env injected. An agent reading only the docstring would not expect that.

## `orient` — 4 tool(s)

*Quick situational awareness.*

### `av_digest`

**Returns:** One ranked JSON: health score, priority attention list, top errors, latest frame, capture/alignment status

**Use when:** Call this first, immediately after bridging a program or when returning to check on one, before reaching for any single-purpose status tool — it tells you what is actually wrong and which tool to call next instead of you guessing.

Aggregates state already held in the running bridge process: rescans the last 3000 records of the bridged program's JSONL action log via the generic failure detector, dedupes them by content fingerprint into top_errors (top 5 by count) and new-this-session fingerprints (diffed against a persisted fingerprint-history JSON file); pulls the latest in-memory captured frame's AI-written summary/tags/anomaly/state_delta; scans the last 20 in-memory frames to compute a rising/steady/falling error-rate trend; reads capture-engine counters (blank frames, window_missing, fps) and the in-memory visual-change-event list (freeze/blank/on-screen-error) and the flight-recorder's frozen-incident list; and folds all of it into a shared 0-100 health score/grade plus a human-readable, ranked 'attention' list where each line names the specific drill-in tool to call next.

**Why not do it by hand:** Replaces manually tailing the raw JSONL log and eyeballing recent frames: it dedupes repeated errors by fingerprint, flags which ones are brand-new this session, computes an error-rate trend across recent frames, and turns all of that plus capture/alignment/incident health into a single deterministic score with a pre-ranked next-action list — work that would otherwise require cross-referencing several tools by hand.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Not for deep investigation of one specific error (messages are truncated to 140 chars and only the top 5 fingerprints are kept) — use av_errors_by_fingerprint or av_error_moment for that. Not for pixel-level review — the visual block is counts/types only, not images (use av_visual_changes). Error counts and the health score are computed from only the most recent 3000 raw log records and the last 20 in-memory frames, so a long-running session can undercount older recurring errors or dilute the trend calculation.

> ⚠ **Caveat (from the code):** The 3000-record cap in _detect_failure_records(limit=3000) means top_errors counts and 'total_failures' silently reflect only the newest slice of a long JSONL file, not the true lifetime count, even though nothing in the response indicates truncation occurred.

### `av_overview`

**Returns:** One-round-trip bundle of bridge/daemon/capture/program status, active profile, log sources, latest frame seq, and new-error fingerprints

**Use when:** At the start (or resumption) of a debugging session, before deciding whether to call av_preflight, adjust capture rate, or dive into av_diagnose.

Fires seven sequential local GET calls (/status, /daemon/status, /capture/status, /program/status, /profiles/active, /log/sources, /anomalies/new) plus /latest, and assembles their JSON into one dict under keys bridge/daemon/capture/program/profile/log_sources/new_errors_this_session, plus a derived latest_frame_seq and a preflight hint pulled out of the /status payload. /log/sources re-detects each configured log file's adapter on disk (path exists + format sniff) rather than trusting the profile config. /anomalies/new re-scans up to 5000 recent action-log records through the generic failure detector and diffs against a persisted fingerprint-history file to find ones first seen since this bridge session started.

**Why not do it by hand:** Replaces 6+ separate status calls (and manually reading the active profile's log-source config plus grepping for recent errors) with one aggregated snapshot; the per-source adapter detection in /log/sources also saves manually inspecting file contents to confirm a log is being parsed correctly.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Not a diagnostic tool itself — it has no hypotheses or ranking, just current state; use av_diagnose for root-cause analysis. If capture has never run or no program is bridged, most sub-blocks come back as error/empty dicts rather than a clean 'not set up' signal, so the caller must inspect each sub-block.

> ⚠ **Caveat (from the code):** FIXED (2026-07-30). It now calls GET /latest/pointer, which returns sequence/timestamp/summary only and deliberately does NOT increment _agent_reads['full_frame'] or call _mark_seen. The old call also marked the frame EXAMINED, which is what allows retention to delete it — orientation was silently spending a frame's examine flag as well as inflating av_token_report.

### `av_status`

**Returns:** Returns bridge liveness, active profile, frame count, capture-rate envelope, and preflight state

**Use when:** At the start of a session, or any time you need to confirm the bridge is live and see which profile/output folder is active, what capture interval is set, and whether the preflight coverage check has already passed for this program.

Lazily fetches (and if absent, creates) the process-wide collector for the active profile via _get_collector(), reads the in-memory frame count, and reads the per-profile preflight marker file (.av_preflight_ok) via _preflight_hint(). It returns a JSON object with status/active_profile/program/save_folder/frames_stored/capture_rate/preflight plus fixed reminder strings (token_rule, read_this_first). It does not touch the capture loop, logs, or disk beyond that marker file.

**Why not do it by hand:** One cheap call surfaces state that would otherwise require separately checking capture status, reading the profile config, and locating the preflight marker file on disk; it also proactively reminds the caller of the cheap-tiers-first rule and the preflight FORCE.

needs: `none`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Not a health/diagnosis tool — frames_stored can be non-zero and status still say nothing about whether capture is currently running or whether frames are healthy (use av_capture_status / av_diagnose for that). Calling it has a side effect: if no collector exists yet, it silently creates one (and its output folder) for the active/default profile as a side effect of just checking status.

> ⚠ **Caveat (from the code):** _get_collector() has a lazy-init side effect: the very first call to av_status (before any capture/start) will instantiate a ContextCollector and create the profile's output folder on disk, even though the caller only asked for status. If _active_profile_name isn't in the loaded profiles, it silently falls back to the 'custom' profile (or a bare default ProgramProfile()) rather than erroring.

### `av_capabilities`

**Returns:** Returns platform/backend info, adapter+reader counts, capture/daemon status, and a grouped MCP tool catalog

**Use when:** On first contact with AgentVision for a given program, or any time you need to know what capture backend/profile/language is active and which tool to reach for next, before calling av_preflight or av_diagnose

Reads the lazily-created ContextCollector's active profile (display_name, language), the auto-capture engine's running/capturing flags and interval, the log-adapter REGISTRY count via log_adapters.list_adapters(), the source-reader list via log_sources.list_readers(), the in-memory frame count, and the daemon PID file (existence + liveness via platform_shim.pid_alive). It also returns a hardcoded tool_catalog dict (_tool_catalog_groups(), shared with /bridge/catalog) grouping every av_* tool by purpose (start/cheap_visual_path/orient/diagnose/investigate/frames/logs/capture/source).

**Why not do it by hand:** Single bounded call replaces manually checking process status, capture state, and adapter/reader counts separately; it is the canonical map of which other tool to call next, not something you could get from the raw log at all.

needs: `none`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Does not tell you anything about the target program's own state or logs — no frames, no events, no errors. If no program has been bridged yet, program/language fields fall back to a default/custom profile rather than erroring.

## `raw_logs` — 7 tool(s)

*The program's log, verbatim and uninterpreted. AgentVision never hides a line from you.*

### `av_log_raw`

**Returns:** Returns new raw log bytes/lines verbatim since the last read, per source, with lossless repeat-collapsing

**Use when:** When AgentVision's normalized/summarized view (av_log_normalized, av_diagnose, etc.) looks suspiciously clean or healthy, and you need the program's actual unfiltered output — especially structured fields (like key=value pairs) that a text summary would have flattened into prose.

GETs /log/raw, which reads each configured log source file (log_sources.effective_sources) from a byte offset (per-session tracked offset by default, or an explicit from_offset, or the whole retained tail with all=1), decodes it as UTF-8, drops blank lines and any line matching AgentVision's own self-emitted markers, then collapses consecutive byte-identical lines into {line, repeat:N} (unless collapse=0). Each source's entry reports last_write_age_s and a stale flag (age > 120s by default) so a dead log file cannot be mistaken for live output. cap_bytes trims the total payload by dropping the OLDEST lines first (shared budget across sources), always reporting how many/which offset to re-fetch the trimmed lines from.

**Why not do it by hand:** Beyond being a raw tail, it tracks read offsets per session so repeated calls only return the new delta (not the whole file each time), losslessly collapses massive repeat-runs (measured: 49% of one real boot log was a single line repeated 21,982 times), strips out lines AgentVision itself wrote (902 watchdog lines in one measured case; that write was moved to AgentVision's own observer log on 2026-07-30, but logs written by earlier versions are still contaminated on disk) so they aren't mistaken for program output, and flags staleness per source so a log the process stopped writing to isn't silently treated as current.

needs: `none`  
cost: `medium`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Not for structured querying/filtering by level or field — it does no parsing at all (the docstring's own suggested follow-up is av_log_entities for that). Degrades silently rather than erroring when no log_sources are configured or none exist on disk: it just returns an empty sources list with totals all zero, giving no explicit signal that the bridge/log setup is missing. Each source is hard-capped at 262,144 bytes read from the tail end (max_bytes_per_source in read_raw_delta), so a large unread backlog is truncated with only a truncated_head flag, not an error.

> ⚠ **Caveat (from the code):** The /log/raw route parses a `collapse` query flag (default on) and correctly forwards it to log_sources.read_raw_delta() for the all=1 and from_offset code paths (bridge_server.py ~6505-6510), but the default path (no all, no from_offset) calls _raw_log_delta(sid, advance=not peek) at line 6512, which never passes collapse through at all -- collapsing is always on in that branch regardless of the collapse=0 query param. This is moot for the av_log_raw MCP tool specifically, since claude_mcp.py never exposes a `collapse` parameter on the tool itself (only session_id, all, from_offset, cap_bytes, peek), so an agent has no way to request uncollapsed output at all via this tool.

### `av_log_where`

**Returns:** Reconciles the profile's configured log paths against the file descriptors the target OS process actually has open for writing.

**Use when:** A log looks too quiet, av_log_* results seem stale or empty despite the program apparently running, or before trusting any 'looks healthy' summary that was built from a log file whose freshness was never checked.

Looks up the target process's pid via psutil (matching profile.process_name / project_root, the same matcher ContextCollector._reader.process_perf() uses), then shells out to `lsof -p <pid> -a -d 0-64 -F fatn` (utils/write_targets.py) to list its open descriptors. It keeps regular files opened for write, compares each against the profile's effective_sources() by real file identity (os.path.samefile / resolved path, not string equality), and buckets results into missing_from_config (process writes here, nothing configured reads it), not_written_by_proc (configured but not held open), and stale (open but mtime older than STALE_LOG_SECONDS, default 120s, env AGENTVISION_STALE_LOG_S). It also reports output_destination for fd 0/1/2 — terminal, /dev/null, pipe, or file.

**Why not do it by hand:** Catches a class of failure a log reader cannot see about itself: reading a file the program stopped writing to, or was never writing to, while tail-style reading shows old bytes forever without complaint. Concrete, not just convenience — this is OS-descriptor ground truth, not a guess from config.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** POSIX-only: relies entirely on `lsof`, so on Windows (no fallback implemented) it always returns {available:false, reason:'lsof not installed'}. If the profile has neither process_name nor project_root set, process_perf() short-circuits and reports not-found immediately, so nothing else runs. It only tells you a file is open and fresh, not whether its contents are correct.

> ⚠ **Caveat (from the code):** Calling this 'read-only' GET route triggers _write_targets_report() -> _get_collector(), which LAZILY CONSTRUCTS the global ContextCollector for the active profile if one doesn't exist yet (and logs 'Bridge started - connected to: ...' as a side effect). So the very first call to av_log_where in a session can initialize global collector state before any capture has been started — a diagnostic call is not fully side-effect-free.

### `av_log_entities`

**Returns:** Counts, per address/handle found via key=value tokens in the raw log tail, which field names it appeared under and how often.

**Use when:** Chasing a 'why does this address/handle/id misbehave' bug, especially one where a value is used in every expected role except one (e.g. presented as src/tex0 but never as target); or when a normalized/summarized health view claims everything is fine but you want the raw structured counts to check it yourself.

Reads each configured log source's raw tail via log_sources.read_raw_delta(profile, {}) — offsets is always the empty dict, so every call scans from byte 0, but the read is hard-capped at 262,144 bytes (256 KiB) per source, newest bytes only. Each line is regex-parsed (connectors/log_fields.py: _KV_RE for key=value/key:value, _HEX_RE for >=4-hex-digit tokens) into fields and any embedded address is normalized (0x-prefixed, no leading zeros, so '0x0000000804000010' and '804000010' collapse to the same key). build_role_index() tallies, per address, {roles: {field_name: count}, total, first_line, last_line}, skipping a fixed non-role key blacklist (size, bytes, len, count, idx, width, height, pid, tid, port, etc.). It supports address=<hex> (roles for one value), key=<csv> (restrict the index to those field names), and the highlighted never=<key> query (roles_never()) which lists addresses seen under other keys but never that one, sorted by total occurrences descending; default view lists top addresses by total.

**Why not do it by hand:** Turns 'read tens of thousands of raw lines and notice a pattern by eye' into one counted, sorted query. This is the literal mechanism the codebase credits with solving its hardest bug (an address appearing as tex0/src but never as target across 44,937 lines). It is honest about doing no severity judgment — it returns counts, never a verdict.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** The route's own docstring calls the read 'the whole retained tail', but read_raw_delta() actually reads only the newest 262,144 bytes per source (offsets={} every call), so on a large or long-running log, older lines silently fall outside the window and are never counted — there is no warning when this truncation happens beyond the generic 'truncated_head' flag on the underlying source record, which this route does not surface. Extraction is regex-based key=value/key:value matching on hex-looking tokens (>=4 hex digits) only; JSON-nested values, base64, or decimal-only IDs won't be picked up as addresses. Not for logs without structured key/value text.

> ⚠ **Caveat (from the code):** log_entities_route's comment '# whole retained tail' is misleading — read_raw_delta enforces max_bytes_per_source=262_144 (256 KiB) by default and this route never overrides it, so the index is really built over each source's newest ~256KB only, not its full retained history. Worth fixing the comment or exposing the truncated_head/lines_total fields (already returned per-source under 'sources') more prominently so an agent knows when it's looking at a partial window.

### `av_log_push`

**Returns:** Returns {ok:true} after appending a plain-text line to the ActivityLogger's activity.log

**Use when:** Leaving a free-text waypoint/note during a live debugging session (e.g. 'started investigating bug X') that you want visible in the human-readable activity timeline/overlay.

POSTs {message, category, source, data} to /log. The handler (bridge_server.py:2709-2716) reads ONLY data['message'] and calls get_logger().log(msg) — the module-level ActivityLogger singleton (modules/activity_log.py). That appends an in-memory ActivityEntry(ts, description) and, only if set_log_file() was previously called for this run (context_collector.py:269 sets it to '<save_folder>/activity.log'), appends a plain text line '[HH:MM:SS.ffffff] message\n' to that file. category, source, and data are read by nothing in this code path and are discarded.

**Why not do it by hand:** Marginal — it is a thin convenience over appending a line to a text file yourself; there is no structured storage, no query filter, and (per code_note) no correlation payload actually retained.

needs: `none`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Do NOT use this expecting the note to show up in av_log_range or av_actions_around_frame, or to carry category/source/data metadata forward for later querying — the code does not support that despite the docstring's claim. If ActivityLogger.set_log_file() was never called for the active run (no collector started yet), the message is only kept in-memory (lost on process restart) and never written to disk at all.

> ⚠ **Caveat (from the code):** FIXED (2026-07-30), by changing where it writes rather than what it claims. The record is structured (category/source/data preserved) and is appended to AgentVision's own observer log — log/observer/<profile>.observer.jsonl — readable via av_observer_log(). It is deliberately NOT written into the program's actions.jsonl: the observer's notes are not the program's output, and mixing them is how one real project's action log became 95% AgentVision. The plain line still goes to activity.log. Response reports written_to / not_written_to.

### `av_observer_log`

**Returns:** Returns AgentVision's OWN observations about the program — watchdog verdicts, process start/exit, and agent waypoints — as structured JSONL records.

**Use when:** When you want to know what AgentVision itself concluded about liveness over time, or to re-read waypoints you left with av_log_push. For the CURRENT silence figure prefer av_log_sources / av_diagnose, which recompute it at read time.

Calls GET /observer/log?limit=N[&profile=P], which reads log/observer/<profile>.observer.jsonl — AgentVision's own sink — and returns the last N records plus the watchdog's in-memory live_state {running, program_silent_s, stuck} and the sink path. Records are written by the stuck watchdog (program.stuck, program.started, program.exited) and by av_log_push. Nothing here is the program's own output.

**Why not do it by hand:** Separates the observer's notes from the observed program's log, which is the only way either can be trusted: 2024 of 2123 records in one real project's actions.jsonl were AgentVision's own watchdog, and their freshness made a process that had exited 48 h earlier read as live.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Not the program's log — use av_log_range / av_log_normalized / av_log_raw for that. A stored program_silent_s is a historical observation, not the present state.

> ⚠ **Caveat (from the code):** The watchdog only records program.stuck while the process is actually running; an exited program produces one program.exited and then silence, so an empty result means 'nothing observed', not 'the program is fine'.

### `av_debug_log`

**Returns:** Returns the last N lines of AgentVision's own internal debug log file, not the bridged program's log.

**Use when:** When something about AgentVision itself seems broken — capture producing no frames, the source mirror not syncing, daemon/profile issues, unexplained bridge errors — as opposed to debugging the target program, which uses av_log_range/av_log_normalized/av_program_log instead.

Calls GET /debug/log?lines=N, which does Path.read_text() on _AV_ROOT/agentvision_debug.log (a fixed path: the bridge server's own logging.basicConfig target, one directory above python_backend/api/), splits it into lines, and returns the last `lines` entries (default 100) as a JSON array plus the log_file path. This is the bridge's meta-log of its own activity (capture start/stop, source-mirror runs, daemon state changes, internal errors written via _info/_warn/_err) — it contains nothing about the bridged program's own stdout/stderr or application logs. On any read failure (missing/unreadable file) the handler catches the exception and returns lines: [] rather than an error.

**Why not do it by hand:** Mainly saves the agent from having to know AgentVision's own install layout (_AV_ROOT/agentvision_debug.log lives outside the user's project) to tail it manually; the actual read is a plain splitlines()[-N:], no parsing, filtering, or level extraction is applied.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Not for reading the bridged program's own log output (use av_log_range / av_log_normalized / av_program_log). Also note the handler silently returns an empty list on any read error, so lines: [] is ambiguous between 'log truly empty' and 'log file missing/unreadable' — there is no error field to distinguish the two.

> ⚠ **Caveat (from the code):** debug_log_tail()'s except Exception: pass swallows all read failures and returns lines: [] with no error signal, which could mislead an agent into thinking AgentVision has logged nothing rather than that the log file could not be read.

### `av_events_schema`

**Returns:** Returns the static event-category docs merged with (category, event-name) pairs actually seen in the most recent JSONL records.

**Use when:** Once per session, before constructing category/event-name filters for av_search, av_trace_timeline, or av_errors_by_fingerprint, to learn the actual event vocabulary this bridged program emits.

Reads the active profile's structured JSONL action log via _read_action_jsonl(limit=2000) -- which loads the whole file into memory, parses every line, then keeps only the newest 2000 records -- and groups them by `category` (and `category.data.name` when data.name is present), recording one sample_source, sorted sample_fields, and a count per key into a `discovered` dict. Merges this with a hardcoded `_EVENT_DOC` table describing ~16 known categories (log, warn, error, exception, process, event, metric, stdout, stderr, trace, state, key, combo, move, cast, attack, npc, nav) and the current SCHEMA_VERSION. If no action-log path is configured or the file is missing, `discovered` is empty and `scanned` is 0, but `categories` and `note` are still returned without error.

**Why not do it by hand:** Replaces eyeballing raw JSONL lines to guess field names and category strings: collapses up to 2000 records into one deduplicated example per (category, event) pair, and surfaces standard categories (e.g. trace, state) the agent might not otherwise know to filter on.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Not a substitute for av_search/av_trace_timeline when you need actual event data -- this only returns one sample per key. Discovery reflects only the most recent 2000 records (newest-first truncation of a full-file read), so category/event pairs that only occurred earlier in a long-running session and haven't recurred can be missed. Without a configured/existing structured JSONL action log (log_source_events), `discovered` and `scanned` are simply empty -- this is a graceful degradation, not an error.

> ⚠ **Caveat (from the code):** _read_action_jsonl reads and JSON-parses the ENTIRE log file on every call (os.path.getsize + f.read(size) with no streaming/early-exit) before slicing to the newest `limit` records -- cost scales with total log file size, not with the 2000-record output, so this can get slow/memory-heavy on long-running programs with large JSONL logs.

## `diagnose` — 8 tool(s)

*Something is wrong and you want ranked hypotheses, not a haystack.*

### `av_diagnose`

**Returns:** Ranked list of root-cause hypotheses (program-down, top errors, anomaly, capture health) with evidence and next-tool-call suggestions

**Use when:** As the first call when investigating any failure or 'something is wrong' report, before manually grepping logs or opening frames.

Reads the in-memory frame store, the latest frame, and up to 3000 recent action-log records via the generic failure detector (_detect_failure_records: category error/exception/fatal, level ERROR/FATAL/CRITICAL, data.name=='run.fail', or source containing fail/error/crash). Groups failures by fingerprint (_error_groups), cross-references each fingerprint to the frame sequences that carry it, and scores candidate hypotheses via a fixed formula (severity keyword match x recency decay x recurrence count, x1.4 if new this session). Separately pulls up to 1000 normalized WARN-level events via read_normalized, coalesces repeats by (source, message) so a 180x-repeated line reports as one entry with a count, and computes a deterministic 0-100 health score (_health_block) shared with /digest. No LLM call anywhere in this route -- purely arithmetic/rule-based correlation over already-collected data.

**Why not do it by hand:** Turns raw, possibly bursty logs (e.g. one line repeated 180x) plus scattered frame metadata into a small ranked list with a severity score and concrete follow-up calls (av_errors_by_fingerprint, av_actions_around_frame, av_timeline) -- doing that scoring/dedup/coalescing by hand from raw JSONL would mean writing the same fingerprint-grouping and recency-decay logic yourself.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Not for visual-only failures (frozen screen, blank window, on-screen error text) that never produced a log record -- the tool explicitly says to pair it with av_visual_events/av_error_moment for those, since diagnose only reasons over frames+logs it already has, not pixels. Hypothesis list is hard-capped at 6 and each hypothesis's evidence at 6 items, so low-ranked but real issues can be silently dropped from the response.

> ⚠ **Caveat (from the code):** Degrades gracefully rather than erroring: with zero frames (_latest_frame is None) and an empty action log, it still returns 200 with hypotheses=[] and top_signals=['no strong failure signals - program looks healthy'] -- indistinguishable from a genuinely healthy program, so an agent should cross-check av_program_status/av_capture_status before trusting a clean diagnose result on a freshly-bridged program.

### `av_diff`

**Returns:** Aggregated state/error/anomaly/perf delta between two already-captured frame numbers a and b.

**Use when:** You already have two frame numbers that bracket a suspected regression (e.g. from av_visual_changes, av_bookmark_outliers, or av_timeline) and want the net cause in one compact call instead of re-reading every intervening frame by hand.

GET /diff looks up frames a and b directly by sequence number in the in-memory _frames dict (404 if either is missing), then walks every intervening frame where a < sequence <= b and merges each one's state_delta.changed/added/removed into one net dict (changed values are last-write-wins across the range), each capped to the first 30 keys with a 'truncated' flag if more existed. It compares fa/fb's structured error blocks via _frame_error (fingerprint, or an (exception_type, message[:80]) fallback) to produce new_error/resolved_error, diffs anomaly.detected/type, computes perf deltas for cpu_percent/rss_mb/num_threads as {from,to,delta}, and adds a small meta_delta (running, tags, summary, black_frame, capture_target) for each endpoint frame.

**Why not do it by hand:** Turns an N-frame manual comparison into one aggregated, pre-merged delta (state/error/anomaly/perf) instead of the agent pulling each frame's JSON and diffing it itself.

needs: `frames_on_disk`  
cost: `low`  
languages: `any`  
program kinds: gui, game

**Not for:** Not for diffing two wall-clock timestamps that don't correspond to captured frame sequences (use av_state_diff instead). Each of changed/added/removed is hard-capped at 30 keys — beyond that the extra keys are silently dropped from the response and only a boolean 'truncated' flag says so, with no indication of which keys were cut. Error identity falls back to a truncated (type, message[:80]) tuple when no fingerprint exists, so two distinct errors sharing type and an 80-char message prefix could be treated as the same error.

> ⚠ **Caveat (from the code):** The 30-key truncation cap on changed/added/removed applies per-category with no way to page past it or see which keys were dropped — a maintainer relying on this for a large state diff (e.g. across a long a..b range) could silently miss the actual changed field.

### `av_state_diff`

**Returns:** Diffs the nearest 'wide' full-state log snapshots before two timestamps into added/removed/changed keys

**Use when:** When you need to know what program-state fields changed between two specific moments in time (not two capture frames) and the program logs periodic full-state 'wide' snapshots into its action log, e.g. did player.hp or connection.state change between t1 and t2.

Reads up to the last 20000 records from the active profile's action-log JSONL (_read_action_jsonl), keeps only records tagged category=='wide' or data.name=='state.wide' (fat once-a-second full-state snapshots the bridged program must itself emit), then for a_ms and b_ms independently picks the single wide record whose ts_ms is nearest that instant. Both records' 'data' dicts are flattened to dotted leaf keys (diagnostics.flatten_state, depth<=6, <=400 keys) and compared with diagnostics.state_delta, which returns added/removed/changed leaf-key dicts (each capped at 30 entries) plus counts and a truncated flag.

**Why not do it by hand:** Saves manually scanning the JSONL for the two nearest wide records and diffing two potentially large nested JSON blobs by eye; flattening to dotted paths surfaces a single deep change (e.g. player.pos.x) directly instead of you visually diffing whole nested trees. Beyond the nearest-match lookup and flatten/compare, there is no semantic interpretation — it is a mechanical two-dict diff.

needs: `log_source_events`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Frame-to-frame diffs (use av_diff instead); any program that doesn't emit category='wide' or data.name='state.wide' records — returns {match: None, hint: ...} rather than an error, so it silently produces nothing useful for ordinary text/error/event logs. Also performs no distance/staleness check: if a_ms or b_ms falls far outside the log's actual time range, or falls inside a gap between rare wide snapshots, it still silently returns whatever wide record is nearest with no warning that the match is stale.

> ⚠ **Caveat (from the code):** _read_action_jsonl reads the WHOLE action-log file but then returns only the last 20000 matching records (no category pre-filter is passed here, so the cap applies across ALL record categories, not just 'wide'). In a chatty log where wide snapshots are sparse relative to other events, the 20000-record tail cap can silently exclude genuinely-nearer old wide records for an early a_ms/b_ms, and the tool has no way to signal that a truncation happened.

### `av_metrics`

**Returns:** Aggregated cpu/rss/thread/error/blank-frame stats over the last N in-memory captured frames

**Use when:** For a quick perf/error trend check — spotting a memory climb, CPU spike, thread-count leak, or rising error rate over recent frames at a glance, typically before or alongside a deeper av_diagnose call.

Takes an in-memory snapshot of captured frames (_frames_sorted), clamps `window` to [1, 2000] and keeps only the newest `window` frames. For each of cpu_percent, rss_mb, num_threads it pulls the value from each frame's 'perf' block (a psutil sample of the bridged program's process taken by the capture engine at shot time) and reports latest/min/max/avg/n. It also counts frames whose 'error' block has a message or exception_type, counts frames flagged capture_meta.black_frame, and reports the capture engine's live state (capturing bool, shots_per_second, interval_s, and the session's cumulative blank_frame_count).

**Why not do it by hand:** Turns however many raw frame dicts (each carrying its own perf block) into one small aggregate instead of you paging through frames and hand-computing a running min/max/avg of cpu/rss/threads. It is a straightforward aggregation only — no anomaly detection, thresholding, or trend classification (that judgment is left to av_diagnose).

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Anything beyond the in-memory frame buffer's hard cap of 2000 frames (older frames are simply not summarized); getting an actual timeseries to plot (only aggregate latest/min/max/avg are returned, not per-frame values); a program whose OS process psutil never matched — frames then carry no 'perf' block and the series silently report n:0 with null latest/min/max/avg rather than erroring.

> ⚠ **Caveat (from the code):** When zero frames exist yet (or none carry a 'perf' block), the endpoint still returns HTTP 200 with all-null/zero series and no hint field — unlike the related /state_diff and /wide endpoints, which return an explicit {match: None, hint: ...} when their data source is empty. A caller can't distinguish 'no data yet' from 'genuinely flat metrics' without also checking window_frames == 0.

### `av_errors_by_fingerprint`

**Returns:** Without fp: ranked histogram of recurring failure fingerprints; with fp: all matching records oldest->newest

**Use when:** When deciding which bug is worth fixing first (call with no fp to rank recurring failures by frequency), or once a fingerprint is known (e.g. from av_diagnose or av_incidents) and you need its full history of occurrences.

GET /errors/by-fingerprint scans the active profile's action_log_file (JSONL) via _read_action_jsonl(limit=2000), then runs _detect_failure_records over that window: a record counts as a failure if category is error/exception/fatal, level is ERROR/FATAL/CRITICAL, data.name is run.fail/fatal/crash, or the source string merely contains the substring fail/error/crash. Each trigger is hashed by _record_fingerprint -> _fingerprint (delegates to modules.diagnostics.fingerprint: SHA1[:12] of the error text after hex addresses, Windows/Unix paths, and all digits are normalized to placeholders, so two errors that differ only by an address, line number, or id collapse to the same fingerprint). With no fp, returns one histogram entry per fingerprint (count, first_ts, last_ts, a 120-char sample message) sorted by count descending. With fp given, returns every raw record sharing that exact fingerprint, oldest to newest.

**Why not do it by hand:** Manually deduping a raw log by eyeballing text fails because timestamps, memory addresses, and incrementing ids make identical bugs look like distinct strings; the shared fingerprint() normalization collapses those variants so recurrence counting and full-history lookup are both a single call instead of a hand-written grep+regex pass.

needs: `log_source_any`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Not for pinpointing a single moment in time (use av_error_moment/av_state_at). The failure detector is a loose OR of substring/category/level checks, so a benign source name merely containing 'fail'/'error'/'crash' (e.g. 'FailoverManager', 'CrashReporterService') is misclassified as a failure trigger -- there is no severity/semantic filter beyond that. The scan is hard-capped to the most recent 2000 action-log records, so older failures that have scrolled out of that window are absent from both the histogram and any fp lookup.

> ⚠ **Caveat (from the code):** FIXED (2026-07-30). _detect_failure_records matches failure words in `source` on WORD BOUNDARIES and skips the source heuristic entirely when the record carries an explicitly benign level (INFO/DEBUG/TRACE/NOTICE/VERBOSE): an emitter that says INFO outranks a guess made from a module name. FailoverManager, CrashReporterService and app.error_pages no longer pollute the histogram; a real category=error record still trips it.

### `av_new_errors_this_session`

**Returns:** Lists failure records seen since the bridge started whose fingerprint isn't in the (effectively always-empty) history file

**Use when:** Right after (re)attaching to a running program, to see which failure types are new since this bridge session began — i.e. bugs that just started happening — rather than replaying every historical failure fingerprint the program has ever produced.

Calls GET /anomalies/new. Loads a fingerprint history set from a JSON file at AGENTVISION_FP_HISTORY (default log/agentvision_fp_history.json) via _load_fp_history(). Scans up to the last 5000 action-log JSONL records via _detect_failure_records() — a record counts as a failure if its category is error/exception/fatal, its level is ERROR/FATAL/CRITICAL, data.name is run.fail/fatal/crash, or its source string contains fail/error/crash. Keeps only records timestamped at or after the bridge process's own start time (_session_start_ms, set once at server boot), computes each one's fingerprint via _record_fingerprint (hashes data.error/stack_trace/stack/message/reason if present, else source|category|data.name), and returns every one whose fingerprint isn't already in the loaded history, as {fp, ts, source, summary}.

**Why not do it by hand:** Saves manually diffing 'errors I've seen before' against 'errors in the log now,' and the failure-shape detection (category/level/name/source heuristics) works the same regardless of source language. In practice it currently degrades to 'all failures since the bridge started,' see code_note.

needs: `log_source_events`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Do not rely on this to mean 'errors genuinely new to this program' across bridge restarts — see code_note, the persistence side of the history mechanism is dead, so nothing is ever actually recorded into history. Also the output is not deduplicated by fingerprint: a single recurring failure emits one entry per matching record, not one grouped entry (use av_errors_by_fingerprint for a grouped/counted view).

> ⚠ **Caveat (from the code):** FIXED (2026-07-30). _save_fp_history is called now, so the history file is written and the NEXT bridge session no longer reports those fingerprints as new. Within a session the filter set is a boot-time snapshot, so repeat calls return the same answer (a read tool whose result changes because you read it is not evidence). Each fingerprint appears ONCE with count/first_ts/last_ts instead of once per record — 41 records of 2 kinds now return 2 entries, not 41. Pinned by test_tool_contracts.py.

### `av_bookmark_outliers`

**Returns:** Ranks numeric log fields by z-score deviation vs. baseline in the 30s before a bookmarked failure

**Use when:** Right after a failure has been bookmarked (via av_list_bookmarks/av_get_bookmark) and you want the 'smoking gun' numeric field — e.g. memory, queue depth, retry count, latency — that moved abnormally in the 30s leading up to it, instead of eyeballing every field in that window by hand.

Calls GET /bookmark/<bid>/outliers, where bid must be an ISO timestamp string (from av_list_bookmarks) parseable by _iso_to_ms, else HTTP 400. Reads the active profile's structured action-log JSONL twice via _read_action_jsonl: a 'sample' windowed to [trigger_ms-30000, trigger_ms] (limit 100000), and a 'baseline' that is simply the newest 20000 records in the whole file with no window at all. For every numeric (non-bool) value found under each record's `data` dict, it computes the sample mean and, if the field has >=3 sample points and >=5 baseline points, the baseline mean/variance, then a ratio (sample_mean/base_mean) and a z-score ((sample_mean-base_mean)/baseline_stddev). Results are sorted by |z| descending and truncated to the top 20.

**Why not do it by hand:** Does a real Honeycomb/BubbleUp-style statistical comparison (per-field mean + variance + z-score against a baseline) that would otherwise mean manually extracting every numeric field from ~30s of JSONL by hand and computing stats yourself — this is a genuine analysis shortcut, not just a formatting convenience.

needs: `log_source_events`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Text-only or unstructured logs — only numeric values nested directly under a record's `data` object are considered; string/boolean fields and anything outside `data` are invisible to it. Fields need >=3 points in the 30s window and >=5 in baseline or they're silently skipped, so rare/sparse-but-real anomalies can be missed. If the window has no matching records it returns 200 with `{outliers: [], note: 'empty window'}` rather than an error, which can look like 'nothing was wrong' when it actually just found nothing to read. `bid` must resolve via _iso_to_ms; arbitrary strings 400.

> ⚠ **Caveat (from the code):** The assignment note for this tool said 'NO route - pure local logic,' but that's wrong: claude_mcp.py:563 does call _http_get to a real Flask route, GET /bookmark/<bid>/outliers (bridge_server.py:2177-2235), which does non-trivial server-side work (two JSONL scans + per-field z-score calc). Separately, a real design gotcha: the 'baseline' is NOT 'normal behavior before things went wrong' — it's just the most-recent 20000 records in the active log file at call time (_read_action_jsonl with no window), so if the program has been failing/recovering repeatedly, the baseline itself can include failure-adjacent or post-failure records, diluting the very anomaly the tool is trying to surface.

### `av_source_at_error`

**Returns:** Returns error type/fingerprint plus source-code snippets around each stack frame's file:line

**Use when:** Immediately after av_diagnose or av_errors_by_fingerprint identifies a fingerprint, to see the actual failing code instead of just the log line; or with no args right after a crash to jump straight from 'latest frame has an error' to its source.

Looks up a structured error either by fingerprint (scanning in-memory captured frames for one whose frame.error.fingerprint matches) or, if omitted, from the latest captured frame's error. That error's frames list (produced upstream by context_collector's multi-language parse_exception() run over the raw traceback/log block) is capped to `frames` entries (default 5, max 10). For each frame it resolves file:line to an actual file on disk in this order: project_root-relative path, then absolute path, then a basename lookup against the profile's source_index.json mirror, and reads back `context` lines (default 4, max 12) above and below the error line, marking the error line with '>>'. Returns found=false per frame when no path resolves.

**Why not do it by hand:** Saves the round trip of copying a file:line out of a log and manually opening/grepping the source tree; also resolves paths that don't exist relative to cwd via project_root or a basename-matched source mirror, which a plain file open would miss.

needs: `frames_on_disk`, `log_source_any`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Not for browsing source generally (use av_source_file/av_source_search instead). Only works if some captured frame actually carries a structured error with parsed stack frames (requires that a traceback/exception block was present in the log and parse_exception() recognized its shape - python/go/rust/php/cpp/java/ruby/dotnet/node; a bare one-line error message with no file:line frames yields frames=[] even though error.type/message are still returned). Captured frames are in-memory only and capped (RECORDER_MAX_FRAMES, default 1200, oldest unpinned frames evicted), so a fingerprint from an older av_errors_by_fingerprint result can silently disappear (returns error:None with a hint) once it rolls off - there is no fallback to on-disk logs. found=false per frame means the file isn't under project_root, isn't an existing absolute path, and has no basename match in source_index.json.

> ⚠ **Caveat (from the code):** The two line ranges in the assignment both matched the correct handler (GET /source_at_error) with no drift.

## `cheap_visual_path` — 6 tool(s)

*The token-efficient way to see what happened. Use in this order.*

### `av_visual_changes`

**Returns:** Returns only the frames where the screen actually changed, with identical runs collapsed

**Use when:** After reproducing a bug, after a click, or while waiting on a long operation, whenever you want 'what did the screen do?' without opening any image.

GET /visual_changes takes an optional ts window, iterates the in-memory frame list (oldest to newest, from _frames_sorted), and reads each frame's precomputed capture_meta.visual block (change_score, changed_bbox, dhash, structural stats) written earlier by the capture pipeline. Frames whose change_score is >= min_change (default 0.008, i.e. ~1 of 256 grid cells) become their own row with a one_line_summary and optional OCR snippet; frames below that threshold are merged into 'identical' or 'minor_change' run objects that keep a seq_range and a unioned bbox so nothing is silently dropped. It also marks the surveyed sequence ranges in the retention ledger (_ret.LEDGER.mark_surveyed) as examined, except failure-shaped frames which stay in the awaiting-examination queue.

**Why not do it by hand:** Turns a whole capture run (potentially thousands of near-duplicate frames at ~10fps) into a few hundred tokens of structured rows, versus manually opening/comparing frame images or hand-scanning a directory of screenshots.

needs: `frames_on_disk`  
cost: `low`  
languages: `any`  
program kinds: gui, game

**Not for:** Not useful for headless/non-visual programs with no capture frames (returns frames_considered=0). include_ocr degrades gracefully to ocr_unavailable/reason when tesseract isn't installed rather than failing, so OCR is a soft not a hard dependency. limit is capped at 500 and only the MOST RECENT rows are kept if truncated, so old changes can be silently excluded from the tail of a long run unless from_ms/to_ms narrows the window.

### `av_frame_json`

**Returns:** Returns one frame's metadata, diff/structural stats, OCR text, and aligned logs as JSON, no image.

**Use when:** The first thing to call when you need to know what was on screen at a specific frame seq — before ever opening a PNG for it, and after av_visual_changes has pointed at that seq.

Loads the frame dict by seq via _frame_or_load (from the in-memory _frames cache, or lazily parsed from its on-disk JSON sidecar under the program folder; 404 if seq is unknown to either). Builds a descriptor from the stored visual diff (_visual_of: dhash, dhash_distance_from_prev, change_score, mean_abs_diff, changed_bbox/cells, structural mean_luma/contrast/is_blank/text_rows) plus ve.dominant_colors on the frame's annotated_image, any error the collector already attached to the frame (_frame_error), any visual_events whose seq range covers it, and a one_line_summary. If ocr=True it OCRs the FULL frame image via _ocr_image (auto-detected backend: tesseract/Apple Vision/Windows OCR/RapidOCR); if none is installed it returns ocr_text=null with an install_hint instead of failing. If logs>0 it pulls that many time-aligned lines from _log_sources.read_normalized in a (-3000ms, +500ms) window around the frame's timestamp, silently returning none on error. thumbnail=True adds a small base64 PNG (via ve.thumbnail_b64) — off by default. Always appends a token_math block comparing its own JSON size to the full frame's estimated visual-token cost.

**Why not do it by hand:** Turns a full screenshot re-read into a compact JSON descriptor (dhash/change score/structural stats/OCR/aligned logs) that costs near-zero tokens versus the PNG; the tool itself reports the savings in token_math rather than asking you to trust it.

needs: `frames_on_disk`  
cost: `free`  
languages: `any`  
program kinds: gui, game

**Not for:** Never returns real pixels by default (thumbnail is off and even then capped at 256px) — for actually seeing a change, escalate to av_frame_region or av_get_frame. OCR silently degrades to null when no OCR backend is installed, and aligned_logs silently degrades to [] if the log read throws, so an empty result here isn't proof nothing happened.

> ⚠ **Caveat (from the code):** Assignment metadata said this tool has 'NO route - pure local logic', but that's incorrect: it calls _http_get(f"/frame/{seq}/json", ...) which is served by a real Flask handler at bridge_server.py:5683 (frame_json). Also, the handler supports a content_map=1 query arg (entropy-quadtree dense-region map) that the MCP tool signature never exposes, so an agent can't reach it through av_frame_json as written.

### `av_frame_region`

**Returns:** Returns a base64 PNG crop of one frame — changed region by default, or dense/full/explicit bbox.

**Use when:** After av_frame_json or av_visual_changes shows a change and you need to actually SEE it (a dialog, error banner, rendering glitch) but want only the pixels that matter, not a full screenshot.

Loads the frame the same way as av_frame_json (_frame_or_load, 404 if unknown). Picks a bounding box by mode: 'changed' (default) uses the changed_bbox already computed for that frame by the visual diff pipeline, falling back to the full frame if none was recorded; 'dense' calls ve.densest_region (entropy quadtree over luma histogram) and falls back to full if the quadtree isn't available; 'full' is the whole frame; anything else is parsed as an explicit 'x,y,w,h'. Crops and downscales the frame's annotated_image with Pillow via ve.crop_region (long edge capped at max_dim, default 900), base64-encodes it into image_b64, and if ocr=True writes the crop to a temp PNG and OCRs just that crop (not the whole screen) via _ocr_image. Returns bbox_mode (which strategy actually fired, including fallback text), change_score, and a token_math block with est tokens saved vs. the full frame.

**Why not do it by hand:** Sends only the changed (or highest-entropy) region, downscaled to max_dim, instead of a full 4K frame; token_math reports the actual visual-token delta against est_visual_tokens_full_frame so the saving is measured, not assumed.

needs: `frames_on_disk`  
cost: `medium`  
languages: `any`  
program kinds: gui, game

**Not for:** Not for surveying a whole run (use av_visual_changes across many seqs first, this is per-frame). bbox='full' or a frame with no recorded changed_bbox still serves a full-size crop and its full visual-token cost. If Pillow isn't installed or the frame's image file is missing, it returns {ok:false, reason} rather than an image — check for that before assuming image_b64 is present. OCR here only reads the crop's own text, not the rest of the screen.

> ⚠ **Caveat (from the code):** Same assignment-metadata error as av_frame_json: this is not 'pure local logic', it calls _http_get(f"/frame/{seq}/region", ...) which is served by the real handler at bridge_server.py:5810 (frame_region).

### `av_visual_events`

**Returns:** Returns the list of auto-detected screen-side events (freeze/blank/layout-change/on-screen-error), most recent first

**Use when:** Asking 'did anything visually go wrong?', especially for hangs (screen_frozen) or sudden dialogs/crashes (blank_screen, layout_change) that leave no log line at all and would be invisible to log-only analysis.

Reads the in-memory _visual_events list (populated at capture time by _detect_visual_events on every analyzed frame) and returns it verbatim, optionally filtered to one detector type and capped to `limit` (default 50, max 500). Consecutive same-type events on adjacent frames were already coalesced into one entry with seq_range/frames/detail at push time (_push_visual_event), so a 30s freeze is one row. Also echoes the four detector descriptions and their current threshold env-var values.

**Why not do it by hand:** Pure lookup over a list already built for free during capture (no OCR/analysis runs at request time) — cheap way to jump straight to a hang/blank/layout moment instead of scanning frames one by one.

needs: `capture_running`, `frames_on_disk`  
cost: `free`  
languages: `any`  
program kinds: gui, game

**Not for:** Only reports what capture already detected; if capture wasn't running or no frames were analyzed the list is empty. The on_screen_error detector specifically needs tesseract OCR (throttled to one scan per VISUAL_OCR_MIN_GAP ms) — without it, only screen_frozen/blank_screen/layout_change are ever produced. Does not include image pixels; use av_error_moment(seq=...) or av_frame_region(seq=...) for those.

### `av_error_moment`

**Returns:** Returns one pre-correlated bundle for a failure: error, frame, region OCR, log window, state delta, code

**Use when:** You know or suspect a specific failure (by fingerprint or frame seq) and want the error, screenshot region, on-screen text, correlated logs, and code in one shot instead of chaining av_get_frame + av_ocr_frame + av_log_normalized + av_state_diff + av_source_at_error.

Resolves a specific failure by fingerprint, by frame seq, or (if neither given) the latest frame carrying a structured error / latest failure record, then assembles: the structured error (type/message/probable_cause/stack frames, capped to 8), the matching in-memory frame's visual summary and changed-region bbox with its OCR text, a time-aligned log window (default +/-6s) pulled from every log source the active profile declares via log_sources.read_normalized, the frame's stored state_delta, and source-code context around the stack frames by internally calling the /source_at_error route. If the fingerprint isn't attached to any captured frame it falls back to the log-scan error index (_error_groups) and attaches the nearest frame in time by timestamp. Also attaches flight-recorder incident metadata if the resolved frame falls inside a frozen incident window.

**Why not do it by hand:** Does the timestamp-to-frame correlation and multi-source log merge for you, and pulls matching source code automatically via source_at_error — a real multi-step aggregation, not just a log read.

needs: `frames_on_disk`, `log_source_any`  
cost: `medium`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Pixels are omitted by default (include_image=False) — you only get the changed-region bbox and its OCR text unless you opt in, since full images are the expensive tier. OCR of the region silently reports unavailable (with an install hint) if tesseract isn't installed rather than failing. If no error and no frame exist at all it returns found=False with a hint to use av_diagnose/av_visual_changes instead of erroring.

> ⚠ **Caveat (from the code):** Assignment's backend range (5911-6316) overshoots the actual handler, which ends at line 6141 (return jsonify(bundle), 200); lines 6144-6316 are unrelated ambient-state helpers (_ambient_state/_ambient_state_uncached) for a different route, not part of error_moment.

### `av_token_report`

**Returns:** Returns a measured/estimated accounting of tokens saved by using cheap frame reads instead of full images

**Use when:** To sanity-check that the agent is actually using the cheap path (av_frame_json/av_visual_changes) rather than pulling full frames; when the user asks whether AgentVision is saving tokens.

Calls GET /token_report, which reads session counters (_visual_stats for frames captured/analyzed/unchanged, perceptual-hash dedup; _agent_reads for how many times the agent called frame_json/region/full_frame/visual_changes) and picks the most recent analyzed frame as a live sample. On that sample it measures actual full-image PNG size, calls the real frame_json handler in-process to get descriptor size, and calls visual_engine.crop_region on the recorded changed_bbox to size a changed-region crop. It converts these to token estimates via visual_engine.visual_tokens (Claude's ceil(w/28)*ceil(h/28) image-token rule, capped per tier) and est_text_tokens (~4 chars/token), then computes a session-level counterfactual: naive cost if every captured frame had been sent as a full image vs. estimated tokens actually paid.

**Why not do it by hand:** Turns an implicit design claim (JSON descriptors are cheaper than images) into a measured number on a real frame from this session, including an honest disclosure that both the image-token and text-token figures are estimates, not tokenizer output — something no raw log or manual token count would give without re-deriving the same formulas by hand.

needs: `capture_running`, `frames_on_disk`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Not a real token count — it explicitly states its numbers are estimates (image-token formula and ~4 chars/token heuristic), not actual tokenizer output. The measured_comparison section is unavailable (available: false) if no analyzed frame with a recorded size exists yet, and the changed_region_crop portion is unavailable if the sample frame has no recorded changed_bbox.

## `flight_recorder` — 2 tool(s)

*The window BEFORE a failure, frozen automatically.*

### `av_incidents`

**Returns:** Lists auto-frozen pre-failure incident windows, or expands one incident's frame rows by id.

**Use when:** The moment a failure is reported or observed, before asking the user to reproduce it -- the pre-failure window is typically already frozen on disk.

Reads the in-memory _incidents list (each entry built by the recorder when a screen_frozen/blank_screen/on_screen_error/error signature fires) plus retention-ledger and live frame-store counters. Without `id` it returns recorder stats (window/tail seconds, disk budget, frames pinned/pruned/live, incidents frozen this session) and the last `limit` incidents (id, kind, trigger_seq, trigger_ms, detail, window_ms, frame_count). With `id` it looks up that incident's frame_seqs in the live frame store, builds a row per still-present frame via _frame_row + _one_line_summary, and returns up to 200 rows plus a `next` hint pointing at av_error_moment/av_replay.

**Why not do it by hand:** Skips the reproduce-and-rewind cycle: the failure trigger (structured error, screen freeze, blank screen, on-screen error text) already froze the preceding window and exempted it from pruning before the agent asked, so the run-up to a crash is available without re-triggering it.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Not for current/live state (use av_start_here or av_frame_json) and not for log-only failures with no visual component (use av_diagnose). Degrades to an empty list with no error if the recorder never fired, if AGENTVISION_RECORDER=0, or if capture never ran. Only the last `limit` (clamped 1-100) incidents are kept and an expanded incident's frame rows are capped at 200 even if more exist.

> ⚠ **Caveat (from the code):** The response key `rolling_window_seconds` is explicitly flagged in bridge_server.py as kept only for backward compatibility -- it is the width of the window an incident FREEZES, not a deletion/eviction rule. Actual eviction is byte-budget + examine-before-delete (av_retention). A reader taking the key name at face value would misread it as a rolling-delete window.

### `av_replay`

**Returns:** Returns a bounded, ordered list of changed-frame steps in a time window, each paired with nearby log lines.

**Use when:** To understand a sequence of events ('what happened between the click and the crash?') or to step through a frozen incident end to end.

Sorts captured frames (or one incident's window_ms if `incident` is given) by time, keeps only frames whose change_score is missing, >= ve.MIN_CHANGE, or that carry a detected frame error (unchanged frames are dropped as uninteresting), then thins the result by `step` and caps it at `limit`. Every returned frame is marked seen via _mark_seen (replay counts as examining a frame, incident frames included). For each step it also fetches up to `logs` normalized log events from the active profile's log source in the window (ts_ms-2000, ts_ms+200) and truncates each message to 120 chars.

**Why not do it by hand:** Collapses a scroll through near-duplicate frames and a separate log grep into one time-ordered JSON walk that already lines up each visual change with the log lines nearest it, with no image bytes and no manual timestamp matching.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, game, service, headless

**Not for:** Not for a single moment (av_frame_json is cheaper) or a full one-instant failure bundle (av_error_moment already correlates everything). Only frames still retained or pinned by an incident are replayable -- evicted frames are silently absent. Unchanged frames are dropped by design, so a visually-silent stretch is not proof nothing happened. Log correlation uses a fixed asymmetric window (2000ms before to 200ms after each frame's timestamp), which can miss logs that lag the visual effect.

> ⚠ **Caveat (from the code):** In replay_route the per-step log fetch is wrapped in a bare `except Exception: logs = []`, so a broken/misconfigured log source and a genuinely empty window are indistinguishable in the response -- an agent has no signal that log correlation failed rather than found nothing.

## `investigate` — 10 tool(s)

*You have a lead and want to follow it.*

### `av_timeline`

**Returns:** Returns a merged, ts-sorted list of frame summaries, log events, and failure bookmarks in a window

**Use when:** To see everything that happened around one bad frame or moment — pass a tight from_ms/to_ms window bracketing a frame's shutter_ms to interleave frames, logs, and bookmarks in true time order; omit both to get the most recent `limit` rows overall.

Builds three row sets and merges them by ts_ms: (1) every in-memory frame in the window from _frames_sorted(), turned into a one-line summary (using the stored summary, or falling back to an error/anomaly-derived line); (2) normalized log events from ALL of the active profile's declared log sources via connectors.log_sources.read_normalized() (which already folds in the input-daemon record if configured), fetched at up to max(limit*3, 600) events; (3) auto-detected failure bookmarks from _detect_failure_records(limit=500), which scans the profile's action-log JSONL for error/exception/fatal categories, ERROR+ levels, or fail/error/crash markers. All rows are sorted by ts_ms ascending and only the last `limit` rows are kept, with `total_available` reporting the pre-truncation count.

**Why not do it by hand:** Replaces manually cross-referencing frame sequence numbers against several separately-formatted log files (each needing its own adapter) and a separately-computed failure-bookmark list; returns one already-interleaved, time-ordered view instead of three things you'd otherwise open and merge by hand.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Not a full-fidelity dump: log events are pre-fetched at only max(limit*3, 600) before merging, so on a heavy-volume run an older event that is genuinely inside your from_ms/to_ms window can still be excluded because it fell outside that pre-merge cutoff; bookmarks are capped at the most recent 500 failure records; frames with no timestamp are skipped entirely. Not a health/root-cause tool — it returns raw rows with no severity ranking or hypothesis (use av_diagnose for that).

> ⚠ **Caveat (from the code):** FIXED (2026-07-30). Untimestamped rows keep ts_ms=null and are marked {untimestamped:true, ordering:...}. Timestamped and untimestamped rows are truncated SEPARATELY, with a reserved share (limit//4) for the untimestamped ones: sorting them last alone would have inverted the bug, letting 30 untimestamped lines evict every real row from a limit=10 window. The response reports truncated / timestamped_rows / untimestamped_rows / untimestamped_available.

### `av_search`

**Returns:** Returns matching log events and/or frame summaries (substring or regex) with timestamp and source

**Use when:** To find every occurrence of a symbol, error string, or message across the whole event stream and frame summaries without opening or grepping the raw log files or paging through frames by hand.

Requires q (400 if empty); compiles it as a case-insensitive regex if regex=True else does a case-insensitive substring test. Searches the merged normalized event stream (connectors.log_sources.read_normalized() across all declared sources, capped at the most recent 5000 events pre-filter, with level applied there) against each event's message/name plus raw text, then applies category (exact), source (substring) and trace_id (exact) filters in Python. Only when none of category/source/trace_id are set, it additionally scans frame summaries/tags/error fields from _frames_sorted() for the same text match. Results are sorted by ts_ms and capped at `limit` (default 100, cap 500); `truncated` is true once that cap is hit.

**Why not do it by hand:** One case-insensitive/regex query reaches every declared log source (already adapter-normalized) plus frame summaries in a single pass, instead of grepping each raw log file separately and reconciling different timestamp/format conventions by hand.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Setting category, source, or trace_id silently disables frame-summary matching entirely (the code only runs that block when all three are unset), so a frame whose summary/error text would otherwise match becomes invisible under those filters. Also bounded: the event-stream fetch only looks at the most recent 5000 normalized events before filtering, so a real match further back than that (even inside your from_ms/to_ms window) can be missed. An invalid regex returns a 400 rather than a match.

> ⚠ **Caveat (from the code):** Two issues: (1) the `scanned` counter (bridge_server.py ~4247/4256) is incremented for every event considered but never placed in the JSON response, so that diagnostic is computed and thrown away. (2) `matches.sort(key=lambda m: m.get('ts_ms') or 0.0)` treats any match with no timestamp as if it occurred at epoch 0, sorting it to the very front of the results as the 'oldest' hit rather than flagging it as unstamped — the same ts_ms-coercion pattern seen in /timeline.

### `av_wait_for`

**Returns:** Blocks up to a timeout, polling server-side, then reports whether a new log/error/anomaly/frame appeared.

**Use when:** Right after triggering an action (click a button, restart a service, retry a request) when you need to synchronously catch the resulting log line, new error fingerprint, anomaly, or next captured frame instead of polling av_search/av_log_range yourself in a loop.

POST/GET /wait_for takes a baseline snapshot at call time (max frame sequence in the in-memory _frames dict; the set of failure-record fingerprints from _detect_failure_records/_error_groups) then loops _check() every poll_interval until a match or the timeout elapses (clamped 0.5-120s; poll clamped 0.2-10s). For condition='frame' it waits for a frame with sequence greater than the baseline. For 'error_fingerprint' it waits for a fingerprint not in the baseline set. For 'anomaly' it reads _latest_frame's anomaly block each poll. For the default 'log' condition it calls log_sources.read_normalized(window=(call_time, inf), limit=500, level=level) across every configured log source for the active profile, then filters in Python by exact category, substring source match, and a case-insensitive regex over message+raw. Returns {matched, condition, waited_ms, timeout_s, matched_event|matched_frame} and never raises past the timeout (exceptions inside _check are swallowed and treated as no-match).

**Why not do it by hand:** Replaces a manual sleep-then-grep loop with one bounded call that baselines state at t0 so it only reports genuinely NEW events (pre-existing fingerprints/frames are excluded), with a hard 120s cap so it can never hang the agent.

needs: `log_source_any`, `capture_running`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Not for waiting on program behavior with no log/frame/anomaly signature. Not a long-running watch (use av_watch) since it hard-blocks the call for up to 120s. The 'anomaly' branch has no call-time baseline (unlike 'frame' and 'error_fingerprint'), so if the current latest frame already has anomaly.detected=True before the call, it returns matched=True immediately even though nothing new happened — contradicts the docstring's 'newly-detected anomaly' claim. The default 'log' condition re-reads every configured log source from scratch on every poll tick (no offset caching), which can be wasteful against very large log files across many poll iterations.

> ⚠ **Caveat (from the code):** FIXED (2026-07-30). The identical-arm ternary is gone; the default is still 'log' but the response says so (condition_inferred / condition_basis) instead of implying a deduction. The 'anomaly' condition now takes a t0 baseline (frame seq + whether an anomaly was already present), so a pre-existing anomaly no longer reports as freshly matched by a wait that never waited.

### `av_actions_around_frame`

**Returns:** Returns raw structured action-log JSONL records within +/-window_secs of one frame's shutter timestamp

**Use when:** Right after looking at a frame (image or JSON) to learn exactly what the program's structured action log recorded at that instant — the core image-to-log correlation move, e.g. after spotting something odd in a screenshot and wanting the key/move/cast/event records around it.

Calls GET /frame/<seq>/actions on the bridge with window_secs (default 5.0). The handler loads the frame via _frame_or_load, reads its timestamp_ms (returns an empty actions list immediately if the frame has no timestamp), and reads frame.profile_action_log — the action-log file path that was pinned to THIS frame at capture time (context_collector.py sets it from the capture-time profile's action_log_file, so an old frame still points at the log it was actually paired with even if the active profile has since changed). It then calls _read_action_jsonl with that pinned path, a window of [frame_ms-half, frame_ms+half], and limit=10000: this reads the JSONL file from disk up to its current size, parses each line as JSON, keeps only records whose ts_ms (or ISO ts, converted) falls in the window, and returns them oldest-to-newest (most-recent-first if truncated by the cap). Response is {frame_seq, window_secs, frame_ts_ms, count, actions}.

**Why not do it by hand:** Removes the manual work of converting a screenshot's shutter time into the log's timestamp format and grepping the JSONL file for lines in that window; also automatically uses the log file THAT FRAME was actually captured against rather than whatever log happens to be configured now.

needs: `frames_on_disk`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Not a multi-source or format-normalized view — it only understands one JSONL 'action log' schema (ts_ms/ts + category/source/data) per profile, so a program logging plain text or multiple log files needs av_log_normalized instead. Degrades silently rather than erroring: if the frame predates the profile_action_log field, or the profile has no action_log_file configured at all, path resolution falls back to '' and the call simply returns an empty actions list with count 0 -- no warning is surfaced that the log lookup failed versus the window genuinely being quiet.

> ⚠ **Caveat (from the code):** Same inaccuracy as av_get_frame: the assignment said this tool has no backend route, but it hits a real Flask route, GET /frame/<seq>/actions at bridge_server.py:1735-1755. Separately, the handler hardcodes limit=10000 in its _read_action_jsonl call regardless of window_secs, and window_secs itself has no upper bound validated server-side -- a very large window against a huge action log will read the whole file into memory before filtering.

### `av_trace_timeline`

**Returns:** All action-log records plus any frames captured during the time span of one exact trace_id

**Use when:** When a single logical action (a login attempt, a boss-fight attempt, a nav route) is instrumented with a correlation/trace id and you need every log line AND every screenshot taken during that one attempt, joined end-to-end in one call, instead of separately grepping the log and eyeballing capture timestamps.

The MCP tool is a thin HTTP wrapper (_http_get to GET /trace/<tid>/timeline) -- despite the assignment note, this route DOES exist in bridge_server.py (trace_timeline(), line 2035); it is not pure local logic. The handler reads up to 20000 recent records from the active profile's action_log_file via _read_action_jsonl, keeps only records whose trace_id field equals the given id exactly, and if any match, computes the min/max timestamp across them (t0/t1). It then scans the in-memory _frames dict (frames already captured this session) and returns every frame whose timestamp_ms falls inside [t0, t1], sorted by sequence, alongside the matched records, the span, and duration_s. No trace_id match returns {count:0, records:[], frames:[]} with HTTP 200 (never 404).

**Why not do it by hand:** Replaces a manual two-step correlation (grep the log for a correlation id, then separately find which screenshots' timestamps fall inside that span) with one call that does the exact-match filter, computes the span, and joins matching frame metadata automatically.

needs: `log_source_events`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Useless for logs whose adapter never populates trace_id (many raw/text logs leave it null) -- there is no fallback or fuzzy match, so a real action span with no trace_id simply returns count:0 even though the records exist. Match is exact equality only, not substring. Neither `records` nor `frames` is capped by any limit parameter -- only the fixed 20000-record scan window bounds them -- so a long-lived or reused trace_id can return an uncapped, potentially large payload. Returned frame entries are metadata/filenames only, not image bytes.

> ⚠ **Caveat (from the code):** Assignment doc for this tool stated 'NO route - this tool is pure local logic', but the source shows it is a normal _http_get call to a real, non-trivial Flask route (bridge_server.py:2035 trace_timeline). Flagging in case that stale assumption affected other batches' tools that were assumed route-less by the same heuristic.

### `av_log_normalized`

**Returns:** Returns one merged, time-sorted list of log events pulled from every configured log source at once

**Use when:** When the target program writes to several logs at once, or writes in a non-JSONL format (Log4j, Serilog, syslog, logcat, Go/Rust/Python logging, plain text), and you need one unified time-ordered view instead of separately opening and hand-correlating each log; also to see what every log said in a tight window around a specific frame's shutter_ms.

Calls GET /log/normalized with from_ms/to_ms/level/label/limit. The handler resolves the active profile and, only if at least one of from_ms/to_ms was actually passed, builds a window tuple (otherwise window stays None, meaning 'no time filter'). It calls connectors.log_sources.read_normalized, which iterates effective_sources(profile) (explicit profile.log_sources plus the legacy action_log_file/log_file folded in), skips sources that don't match `label`, and for each remaining source calls _read_source: if the source declares a `reader`, records come from that SourceReader and are normalized through the jsonl adapter; otherwise the file is opened and only its last 1 MiB (tail_bytes) is read, the adapter is either the source's configured one or auto-detected by sampling the first 60 lines, each line is parsed into the unified event schema, tagged with log_label/log_path, and run through escalate_by_content (bumps a weak level like INFO to WARN when the message text itself reads as a failure, e.g. 'ok=False'). All sources' events are merged, filtered by the time window and by `level` (canonical TRACE..FATAL rank), sorted with timestamped events ascending and untimestamped ones pushed to the end, then truncated to the most recent `limit` (default 500) events by count.

**Why not do it by hand:** Removes the manual work of opening N differently-formatted log files, guessing/parsing each one's line format by hand, and cross-referencing timestamps across them; the per-source adapter is picked the same way av_log_sources reports it, and content-based severity escalation surfaces failures that would otherwise sit at INFO and be easy to miss.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Not for reading further back than each source's most recent ~1 MiB of raw bytes — _read_source always tails only tail_bytes=1_048_576 bytes of the underlying file before parsing/filtering, so older history is invisible no matter how large `limit` or the time window is. When no from_ms/to_ms is given, truncation to `limit` happens AFTER merging all sources, so a chatty source can crowd a quiet source's events out of the final page entirely. Degrades silently, not by erroring: an active profile with zero configured/existing log sources just returns count:0, events:[] rather than any warning — check av_log_sources first if that happens unexpectedly.

> ⚠ **Caveat (from the code):** The `limit` query param has no server-side upper bound (_int_arg only casts to int, never clamps), so it is not truly a hard cap on response size the way the docstring implies — the real ceiling on how much comes back is each source's fixed 1 MiB tail read, which the docstring never mentions at all.

### `av_state_at`

**Returns:** Returns the single 'wide' full-state JSONL record nearest a given epoch ms

**Use when:** When you need the program's complete state at one specific instant (e.g. right before a bookmarked failure or at a trace span boundary) and the program already logs periodic wide/state snapshots, instead of manually scanning the JSONL for the nearest one and reconciling ts vs ts_ms formats yourself.

Reads up to the last 20000 records from the active profile's action_log_file JSONL (via _read_action_jsonl), filters to records whose category=='wide' or whose data.name=='state.wide', and returns the one whose ts_ms (falling back to a parsed ISO ts string) is closest to at_ms. These 'wide' records are not created by AgentVision itself — they only exist if the bridged program periodically emits a fat state snapshot (e.g. HP/MP/position/target/run_id) into its own JSONL action log under that category/name convention. If no such records exist, or action_log_file isn't configured, it still returns HTTP 200 with match: null and a hint string rather than erroring.

**Why not do it by hand:** Saves hand-scanning a potentially huge JSONL for the nearest timestamp and normalizing between epoch ts_ms and ISO ts fields; also unifies the two accepted 'wide record' shapes (category=='wide' vs data.name=='state.wide') into one lookup.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Not a generic state-inspection tool — it only works if the target program explicitly writes periodic full-state snapshots to its action JSONL under the wide/state.wide convention; programs that only log discrete per-event records (no periodic wide snapshot) will always get match: null. Also silently caps its scan at the most recent 20000 action-log records, so a wide record far back in a very large/long-running log could be missed entirely.

> ⚠ **Caveat (from the code):** A missing/unconfigured action_log_file and a configured-but-empty-of-wide-records log produce the identical response shape (match: null, generic hint) — there's no way to tell from the response alone whether the feature is simply unused or the profile is misconfigured.

### `av_baseline`

**Returns:** GET returns, or POST stamps, a persisted 'since' timestamp marker for the active (or named) profile

**Use when:** Right before you reproduce a bug or perform an action, so later calls to av_watches(since_baseline=1) or other 'what changed since I started' queries have a fixed instant to bound from.

POST /baseline reads optional {ts_ms, profile} from the JSON body, defaults ts_ms to now and profile to the currently active profile, stores {profile: ts_ms} in the in-memory _baselines dict guarded by _watch_lock, and immediately persists the whole dict as JSON to AGENTVISION_BASELINES (default log/agentvision_baselines.json) so it survives a bridge restart. GET /baseline just returns the currently stored baseline_ms/baseline_iso for the active profile with no side effects. It does not touch frames, logs, or capture state at all - it is pure bookkeeping.

**Why not do it by hand:** Trivial convenience over hand-tracking a timestamp yourself - its only real value is that it persists to disk (survives a bridge restart) and is the anchor point av_watch/av_watches already know how to consume by name.

needs: `none`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Does not itself report anything about program state or changes - it only stores/reads a number. If AGENTVISION_BASELINES can't be written (permissions, missing parent dir creation failure), the stamp still succeeds in memory for the current process but a warning is logged and persistence silently fails until restart.

> ⚠ **Caveat (from the code):** Assignment's range (5301-5327) matched the real handler exactly; route accepts both POST and GET on the same path, not just POST as the docstring's phrasing implies.

### `av_watch`

**Returns:** Registers a named in-memory tripwire (log/error-fingerprint/anomaly) to be checked later via av_watches.

**Use when:** Before reproducing a bug, to set a standing condition ('catch a specific error fingerprint', 'catch any screen_stuck anomaly', 'catch WARN+ logs matching a regex') that you check afterward with av_watches instead of manually rescanning the whole session for the moment it happened.

POSTs to /watch, which stores {name, kind, regex, level, category, source, fingerprint, anomaly_type, active profile, created_ms/iso} in an in-memory dict keyed by name (idempotent — re-registering the same name resets its creation marker). If kind is omitted it is inferred: fingerprint given -> 'error_fingerprint', anomaly_type given -> 'anomaly', else 'log'. For kind='log' the regex is compiled with re.IGNORECASE just to validate it (400 on invalid regex); the actual match happens later. No evaluation, no log/frame reads happen on this call — it only writes the registry entry and returns it plus the total watch_count.

**Why not do it by hand:** Replaces 'remember the exact timestamp I started reproducing, then grep the whole log afterward' with a one-time registration; matching, sorting and capping are done server-side in av_watches so the agent never scrolls raw log/frame history looking for one condition.

needs: `none`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Registering a watch does not tell you anything by itself — it never evaluates against existing data, so calling av_watch alone cannot confirm a condition already occurred (you must call av_watches after reproducing). Regex matching is always case-insensitive (re.IGNORECASE is hardcoded), so case-sensitive matches are not possible. There is no cap on the number of watches that can be registered, so an agent that registers many one-off watches and never clears them grows the in-memory registry unboundedly for the process lifetime.

### `av_watches`

**Returns:** Evaluates every registered watch now and returns each with its new hits since the watch was registered (capped at 25 each).

**Use when:** After reproducing the scenario a tripwire was set for with av_watch, to see which watches tripped, when, and with what sample text/frame, without manually correlating timestamps across logs and frames.

GETs /watches, which snapshots the watch registry and, per watch, evaluates from a start_ms equal to the watch's created_ms (or the active profile's av_baseline marker if since_baseline=1). kind='error_fingerprint' scans up to the last 3000 failure-trigger records from the program's action JSONL (_detect_failure_records, matching category error/exception/fatal, level ERROR+, data.name=='run.fail', or source containing fail/error/crash) and keeps ones matching the fingerprint. kind='anomaly' scans in-memory frames (_frames_sorted) for frame.anomaly.detected entries, optionally filtered by anomaly_type. kind='log' calls log_sources.read_normalized(window=(start_ms, inf), limit=1000, level=...) then filters by category/source and regex-searches message+raw text. Hits are ts-sorted and truncated to the newest 25 per watch (_WATCH_HIT_CAP); a per-watch exception is caught and reported as eval_error rather than failing the whole call. clear=1 wipes the entire registry after building the response (a last look before losing them).

**Why not do it by hand:** Turns a manual re-scan of the whole session into a per-watch capped hit list computed server-side (sorted, deduped by watch, at most 25 hits each); log-kind watches also get level/category/source pre-filtering for free via read_normalized instead of the agent grepping raw lines.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Not a push/streaming mechanism — every call re-evaluates from scratch (up to 3000 failure records for error_fingerprint watches, or up to 1000 normalized log events for log watches), so polling it frequently repeats that scan each time. For kind='log', read_normalized applies its own limit=1000 BEFORE this route's category/source/regex filtering and keeps only the newest 1000 window-matching events; if event volume between the watch's start and now exceeds 1000, older events in that window are silently dropped before the regex ever sees them, which can hide an early hit under high-volume unrelated logging. A broken watch (e.g. bad fingerprint lookup) degrades to an eval_error field on that watch rather than surfacing loudly.

> ⚠ **Caveat (from the code):** For kind='log', the 1000-event cap in log_sources.read_normalized is applied to ALL window-matching events before this route's regex/category/source filter runs, not after — so a watch's true hits can be crowded out and silently lost under high log volume even though only the unrelated events exceeded the limit. Worth surfacing to a maintainer as a real precision gap, distinct from the documented/intentional 25-hit display cap (_WATCH_HIT_CAP).

## `frames` — 7 tool(s)

*Individual screenshots. Prefer cheap_visual_path first.*

### `av_latest_frame`

**Returns:** Returns the most recently captured screenshot+state SnapshotFrame (full image path + JSON), or 404 if none yet

**Use when:** You need to actually look at the current on-screen picture (not just its JSON description) — e.g. to visually confirm a UI state, read something OCR/text-extraction can't reliably get, or inspect a frame av_frame_json flagged as visually changed. This is the last-resort, most expensive image-reading tier.

GET /latest reads the in-memory `_latest_frame` dict (set whenever the capture engine writes a new frame) under a lock, marks it as read/seen for token-report bookkeeping, and returns it run through `_augment_frame_for_ai`, which adds an `_ai` block: the annotated image path, paired JSON sidecar path, shutter timestamp, capture rate, exact byte-offset log-alignment window, correlate-with pointers (av_actions_around_frame, av_log_normalized, av_state_at), a visual dhash/change_score/changed_bbox summary if available, a CHEAPER_PATH block advertising cheaper JSON-only tools, and a loud WARNING if capture_meta marks it a black/blank frame. It does not trigger a new capture — it only serves whatever the last capture wrote.

**Why not do it by hand:** Bundles the raw screenshot with precise time-aligned log offsets and follow-up call suggestions in one response, so the agent doesn't have to separately fetch a frame, compute which log lines are contemporaneous, and guess what to check next. But for anything answerable from text/metadata alone it is strictly worse than av_frame_json/av_read_screen since it forces reading a full image.

needs: `capture_running`, `frames_on_disk`  
cost: `high`  
languages: `any`  
program kinds: gui, game

**Not for:** Surveying a whole capture run (use av_visual_changes instead — this returns only one frame and repeated polling of it is explicitly discouraged in its own docstring). Headless/CLI/service programs with no visible window (capture_meta.window_found will be false / frame will be blank and the tool says so via the WARNING block rather than degrading). Returns 404 with a hint to check av_capture_status if capture was never started or stopped, so it hard-requires an active or previously-run capture with at least one frame in memory.

> ⚠ **Caveat (from the code):** The MCP docstring's claim that _ai gives 'the paired _frame.json' matches json_sidecar in the augment code. No discrepancy found between docstring and handler; _augment_frame_for_ai (bridge_server.py:1552-1617) is shared verbatim with /frame/<seq>, so behavior here is consistent with the by-sequence frame route.

### `av_get_frame`

**Returns:** Returns one full SnapshotFrame by sequence number: screenshot path plus JSON state and an _ai correlation block

**Use when:** Only after the cheaper JSON-only tiers (av_frame_json, av_frame_region, av_error_moment) were tried or are known insufficient, and the agent genuinely needs to look at the actual pixels of one specific, already-identified frame — e.g. confirming a visual layout/rendering detail that JSON metadata can't answer.

Calls GET /frame/<seq> on the bridge. The handler resolves the frame via _frame_or_load(): a hit in the in-memory _frames dict, or, on a miss, a lazy parse of that sequence's sidecar *_frame.json from disk (only the newest HYDRATE_PARSE_LIMIT sidecars are parsed at startup, so older frames pay a one-time JSON-parse+file-exists cost on first access) with annotated_image/frame_image/thumbnail_file/json_sidecar paths attached. It then calls _augment_frame_for_ai(), which layers on an `_ai` block: image_path, shutter_ms/iso, capture_rate, time_alignment (action_log_offset/log_offset plus av_frame_alignment pointer), correlate_with (av_actions_around_frame/av_log_normalized/av_state_at call templates), capture_health (black_frame/window_found/capture_target), a visual dhash/change_score/changed_bbox summary when present, a CHEAPER_PATH block advertising av_frame_json/av_frame_region/av_visual_changes/av_error_moment, and a loud WARNING if capture_meta marks the frame black/blank. Marks the frame 'examined' in the retention ledger via _mark_seen, which is what allows it to be pruned once the disk budget is tight. 404s if the sequence was never captured or has already been evicted from both memory and disk.

**Why not do it by hand:** Bundles the screenshot with its exact time-aligned log offsets and ready-to-call correlation/follow-up commands in one response, saving the agent from separately locating the PNG, computing the shutter time, and figuring out which other tool to call next. Its own docstring and the CHEAPER_PATH block it returns are explicit that this is the most expensive tier and should be a last resort, not a survey tool.

needs: `frames_on_disk`  
cost: `high`  
languages: `any`  
program kinds: gui, game

**Not for:** Do not loop this over a range of sequences to review a capture run — that is what av_visual_changes collapses into far fewer tokens. Not useful once a frame has aged out of both the in-memory cache and disk (HYDRATE_PARSE_LIMIT / retention pruning can evict old, already-examined frames under disk-budget pressure), at which point it 404s with nothing recoverable.

> ⚠ **Caveat (from the code):** The assignment brief for this tool said 'NO route - pure local logic', but that is inaccurate: av_get_frame is a thin wrapper over a real Flask route, GET /frame/<seq> at bridge_server.py:1635-1645, which does nontrivial work (lazy sidecar hydration + _augment_frame_for_ai decoration) rather than being pure local logic in claude_mcp.py.

### `av_frame_alignment`

**Returns:** Returns a re-derived proof {aligned, leaked_after_shutter, leaks[]} that one frame's pinned log window predates its shutter

**Use when:** Before trusting a specific image-to-log correlation (e.g. from av_actions_around_frame or av_error_moment) as forensic evidence, when it matters that the log lines shown for a frame truly predate that screenshot rather than merely being nearby in time.

Calls GET /frame/<seq>/alignment — this is a real Flask route (bridge_server.py:1874-1919), not client-side logic. The handler loads the frame via _frame_or_load (404 if seq isn't in memory or on disk), reads its shutter_ms (timestamp_ms) and capture_meta, then re-reads the action-log JSONL that was pinned to THIS frame at capture time: frame.profile_action_log (the action_log_file path snapshotted when the frame was shot) bounded to frame.action_log_offset, the exact byte offset recorded at that same shutter — so anything appended to the file after capture is deliberately excluded. For every record inside that bounded read it derives rec_ms (ts_ms, or a parsed ISO ts) and flags any record timestamped more than 50ms after shutter_ms as 'leaked' (a record that should not yet have existed at capture time). It returns aligned = (zero leaks), records_in_context, newest_record_ms, leaks (first 20), and the raw capture_meta.

**Why not do it by hand:** Turns 'the timestamps line up' from an assumption into a checked fact, recomputed from the exact pinned byte-offset the capture code snapshotted at shutter time, instead of a human eyeballing action-log timestamps against a frame time by hand.

needs: `frames_on_disk`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Not a general action-log viewer (use av_actions_around_frame/av_log_range for that) and it only checks the ONE action-log JSONL a frame is pinned to, not the other log_file/multi-source logs av_log_normalized merges. Its proof is vacuous when the profile has no action_log_file configured, or the frame's profile_action_log/action_log_offset are unset/zero: _read_action_jsonl then returns zero records, and the route still reports aligned=True, leaked_after_shutter=0 as if verified clean.

> ⚠ **Caveat (from the code):** The assignment brief for this tool stated 'NO route - pure local logic', which is incorrect: av_frame_alignment hits a real backend route at bridge_server.py:1874 (same discrepancy already flagged for av_get_frame/av_actions_around_frame in other batches). Separately, the response gives no way to distinguish 'verified clean' from 'nothing existed to check' — both cases return aligned=True with leaked_after_shutter=0; only records_in_context==0 tells them apart, and nothing in the payload calls that out.

### `av_frame_overlay`

**Returns:** Returns detections/OCR/path/move/cast records from the action-log JSONL matched to one frame

**Use when:** When you want to know what the program's own action/telemetry log recorded around one specific frame (e.g. 'what did it see/where did it walk/what did it cast right here') without opening the JSONL or the screenshot yourself.

GETs /frame/<seq>/overlay. Loads the frame (from memory or its on-disk JSON sidecar via _frame_or_load, 404 if it doesn't exist), then reads the frame's pinned action-log JSONL (profile_action_log, i.e. the profile's action_log_file at capture time) via _read_action_jsonl. It first looks for records whose frame_seq equals seq; if none match it falls back to records within +/-2s of the frame's timestamp. Matched records are bucketed by hardcoded shape into detections (name=='yolo.frame': enemies/npcs/loot), ocr_reads (name=='ocr.read'), path_waypoints (name=='path.compute'), walks (category=='move') and casts (category=='cast'); every other record shape is silently dropped.

**Why not do it by hand:** Does the frame_seq/timestamp join for you against a potentially large action-log file and buckets it into named categories, instead of you grepping the JSONL and hand-matching timestamps to a frame.

needs: `frames_on_disk`  
cost: `low`  
languages: `any`  
program kinds: gui, game

**Not for:** Not a general JSONL viewer: only five hardcoded record shapes are surfaced (yolo.frame, ocr.read, path.compute, category=move, category=cast) — a program whose action log uses any other schema gets back empty arrays for every field even though matching records exist. Also the first (frame_seq) match pass reads the WHOLE action-log file into memory and only checks the last 10000 parsed records for an exact frame_seq hit, so a very old frame in a very large log can miss the exact match and silently fall through to the +/-2s timestamp window instead (usually harmless, but not a true frame_seq match). Use av_log_range/av_log_normalized for arbitrary log content.

> ⚠ **Caveat (from the code):** Assignment note claimed this tool has 'NO route - pure local logic', but that is incorrect: the MCP tool body is a one-line _http_get to a real Flask route, app.route('/frame/<int:seq>/overlay') at bridge_server.py:2607-2659, which does nontrivial JSONL matching. Worth flagging in case other tools' route info was derived the same (wrong) way.

### `av_frame_annotate`

**Returns:** Appends a persisted note to a frame's on-disk annotations sidecar; returns the running annotation count

**Use when:** After you've diagnosed something about a specific captured frame (root cause, a misread OCR value, a suspicious detection) and want that conclusion durably attached to that frame for later retrieval via av_frame_annotations, instead of only stating it in your own reply.

POSTs {message, level, tags} to /frame/<seq>/annotate. The handler loads the frame (404 if missing or if it has no json_sidecar path), derives the sidecar's annotation file as '<stem>_annotations.json' next to the frame's own JSON, builds a note record (ts, ts_ms, author, level, message, tags) with author hardcoded to the request body's author or 'claude', reads any existing annotations.json, appends the note, and rewrites the file. Returns {ok, annotation_count, path}.

**Why not do it by hand:** Gives you frame-scoped persistent memory across tool calls/sessions with zero setup (append-only JSON next to the frame); the alternative is losing the observation once your context ends or keeping your own separate scratch file that nothing else can look up by frame seq.

needs: `frames_on_disk`  
cost: `free`  
languages: `any`  
program kinds: gui, game

**Not for:** Not a general-purpose note or task tool: the note only exists if the target frame still has a json_sidecar on disk (evicted/retention-deleted frames abort 404, silently discarding nothing written). The MCP tool signature has no 'author' parameter, so every note written through this tool is recorded as author='claude' server-side regardless of who conceptually made the observation. 'level' is accepted as any free string — the server does not validate it against info/warn/error despite the default suggesting an enum.

> ⚠ **Caveat (from the code):** Assignment note claimed this tool has 'NO route - pure local logic', but it is a real POST to app.route('/frame/<int:seq>/annotate', methods=['POST','GET']) at bridge_server.py:2662-2706 that performs a read-modify-write of a JSON file on disk. The same route also backs the separate av_frame_annotations tool (GET) defined a few lines below this one in claude_mcp.py.

### `av_ocr_frame`

**Returns:** OCRs one captured frame's screenshot into JSON text/lines/word_count

**Use when:** You need the literal text of a specific already-captured frame (an error dialog, a UI label, a stack trace visible on screen) and don't want to spend vision tokens reading the PNG directly.

GETs /frame/<seq>/ocr, which loads frame `seq` (from memory or its on-disk sidecar via _frame_or_load), reads its `annotated_image` path (actually the original screenshot PNG, not an annotated copy) and runs _ocr_image on it. That picks the best detected OCR backend (tesseract, or a zero-install OS engine — Apple Vision on macOS / Windows.Media.Ocr on Windows / RapidOCR fallback on Linux) via utils.ocr_backends.detect_engine, and returns bounded text (~8KB cap) plus per-line {text,bbox,conf}. Marks the frame as 'seen' (ocr) for eviction bookkeeping.

**Why not do it by hand:** Turns a full screenshot into ~30 tokens of structured text instead of ~4800 tokens of image, and gives per-line bounding boxes/confidence a human eyeballing the image would have to transcribe by hand.

needs: `frames_on_disk`, `ocr_backend`  
cost: `low`  
languages: `any`  
program kinds: gui, game

**Not for:** Reading the CURRENT screen (use av_read_screen instead, which needs no seq); understanding a moment holistically (av_frame_json(seq) returns this OCR text plus hash/change-score/logs in one call). If seq doesn't exist, 404s. If OCR isn't installed/available it degrades to {available:false, reason, install_hint} rather than erroring, and text is truncated at ~8KB.

> ⚠ **Caveat (from the code):** Assignment metadata said this tool has 'NO route - pure local logic', but that is wrong: the claude_mcp.py wrapper does call _http_get(f"/frame/{seq}/ocr") which hits a real Flask route at bridge_server.py:5089-5103. Also worth flagging: `annotated_image` is a documented historical misnomer (see comment at bridge_server.py:768-772) — it actually points at the original unannotated screenshot PNG, not a frame with overlays burned in.

### `av_read_screen`

**Returns:** OCRs the LATEST captured frame right now into JSON text/lines/word_count

**Use when:** You want a quick 'what does the screen say right now' answer without picking a frame sequence number first — e.g. immediately after an action to confirm the resulting UI state in text.

GETs /read_screen, which reads the in-memory _latest_frame (no seq argument, no disk lookup), takes its `annotated_image` path (the original screenshot PNG) and runs the same _ocr_image helper used by av_ocr_frame. Returns 404 with a hint if no frame has ever been captured yet.

**Why not do it by hand:** Same token savings as av_ocr_frame (text instead of a full image) but skips having to first look up the latest sequence number.

needs: `capture_running`, `frames_on_disk`, `ocr_backend`  
cost: `low`  
languages: `any`  
program kinds: gui, game

**Not for:** Historical frames (it only ever reads the single latest frame — use av_ocr_frame(seq) for anything but 'now'); it also inherits the OCR degrade-gracefully behavior ({available:false,...}) and the ~8KB text cap when tesseract/OS OCR is unavailable or the text is long.

## `ui_tree` — 2 tool(s)

*The OS accessibility tree — exact widget text, no OCR guessing. GUI programs only.*

### `av_ui_tree`

**Returns:** Returns the target window's OS accessibility tree as pruned JSON (roles, text, bboxes)

**Use when:** Before av_frame_json/OCR, when you need exact on-screen text, a control's state, or a control's precise pixel bbox to click/verify — i.e. "what does the UI say" or "where is that element" questions on a native-widget app.

Resolves the target app (query `app`, else the active profile's capture_app) and walks its native accessibility API: AXUIElement via pyobjc on macOS (needs the Accessibility TCC grant), UI Automation via comtypes on Windows (falling back to EnumChildWindows+WM_GETTEXT), AT-SPI via pyatspi on Linux. The raw tree is then pruned in utils/ui_tree.py (drop invisible/zero-size nodes, drop text-free container roles by promoting children, drop text-free leaves with no actionable role, then a breadth-first node cap) and returned flat (`{d, role, text, bbox}` rows, default) or nested. It also estimates its own JSON token cost and, if a frame is available (via `compare_to_seq` or the latest capture), compares that against the visual-token cost of an equivalent screenshot, emitting a `cheaper_than_screenshot` verdict.

**Why not do it by hand:** Exact element text/roles/coordinates straight from the OS, with no OCR guessing and (per the tool's own cost math, default 150-node cap ~2-4k tokens) usually far fewer tokens than sending a screenshot.

needs: `accessibility_api`, `gui_program`  
cost: `low`  
languages: `any`  
program kinds: gui

**Not for:** Custom-drawn UIs (games, emulators, canvas/WebGL, Dear ImGui, most SDL/OpenGL) legitimately return available:false or a near-empty tree (`likely_custom_drawn:true`) — use av_frame_json/av_ocr_frame/av_frame_region instead. Not for icon colour, layout correctness, progress-bar fill, or rendering corruption — none of that is in the tree. Unlabelled/offscreen controls are silently missing even when visible. Raising max_nodes can make the tree cost MORE than the screenshot it replaces (the code cites a measured 400-node Finder tree at ~15,900 tokens vs 4,784 for the equivalent 4K frame) — check `cost.verdict`. Traversal is wall-clock capped at 2.5s so a wedged target truncates rather than hanging (`traversal_stopped`).

> ⚠ **Caveat (from the code):** FIXED (2026-07-30). max_nodes is applied per request under a lock and restored in a finally, so it can neither outlive nor overlap the request that asked for it. The response reports max_nodes_applied and states that the process default is unchanged.

### `av_ui_diff`

**Returns:** Snapshots the UI tree twice (wait_ms apart) and returns a named appeared/disappeared/changed diff

**Use when:** Right after performing a click/keypress/action, to confirm in words what changed in the UI (a button disappearing, a label going from 'Ready' to 'Error') instead of eyeballing two screenshots.

Calls the same utils/ui_tree.capture_tree() used by av_ui_tree once, sleeps for `wait_ms` (blocking the request thread), captures again, then diffs the two flattened trees in utils/ui_tree.diff_trees(). Elements are matched by (role, bbox quantised to an 8px grid) so a 1px reflow isn't reported as a change; a match with different text is 'changed', a key only in the after-snapshot is 'appeared', only in the before-snapshot is 'disappeared'. Each list is capped at 50 entries and the human-readable `summary` at 10 lines per category. If either snapshot's tree is unavailable it returns `{available:false, reason, fallback}` instead of diffing.

**Why not do it by hand:** Names the actual element and its old/new text instead of a pixel rectangle, so a caller doesn't have to visually diff two frames or guess what a bounding-box change means.

needs: `accessibility_api`, `gui_program`  
cost: `low`  
languages: `any`  
program kinds: gui

**Not for:** Custom-drawn UIs with no accessibility tree (returns available:false — use av_visual_changes for the pixel-level answer). Not for changes that already happened in the past — this samples live, twice, synchronously, and blocks the caller for the full wait_ms (clamped 0-10000ms, default 1000ms). Also not precise for a moved-but-otherwise-identical element that crosses the 8px quantisation bucket: diff_trees only compares text at a matched key, so a large move is reported as a disappeared+appeared pair rather than a single 'moved' change.

## `logs` — 7 tool(s)

*Log source wiring and adapter management.*

### `av_log_sources`

**Returns:** Lists the active profile's log sources with existence, resolved format, and detection confidence

**Use when:** Before debugging via logs on a freshly bridged program, to confirm each declared log path actually exists and was matched to the right format; or when av_log_normalized returns empty/garbled events and you need to see whether a source is missing, unreadable, or was auto-detected as the wrong adapter.

Resolves the active profile's sources via connectors.log_sources.effective_sources() — explicit profile.log_sources entries plus the legacy action_log_file (as adapter 'jsonl') and log_file (auto-detect) folded in and de-duplicated by absolute path. For each source it checks path existence, then runs log_adapters.detect_adapter_for_file() which samples up to the first 60 non-empty lines and scores every registered LogAdapter's detect() to pick the best match plus a confidence score. The final `adapter` field it reports is the one the merge pipeline will actually use, in priority order: an explicit `reader:<name>` (a SourceReader-backed source) > an explicitly configured non-'auto' adapter > the auto-detected adapter (only if the file exists) > null. It also returns the full registry of available adapters (name + language) via list_adapters().

**Why not do it by hand:** Replaces manually opening every log file to eyeball its format and guess whether an adapter would parse it correctly; surfaces the detector's confidence score so a low-confidence/ambiguous guess is visible before you trust the merged output, and shows exactly which adapter will be used vs. merely detected.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Does not validate log content correctness, freshness, or whether the process is actively writing to it (staleness is only computed inside the /log/normalized path, not here) — it only checks path existence and samples the first 60 non-empty lines, so a source whose format changes partway through the file (e.g. a banner/header block before JSON begins) can be misdetected. Not useful once you already trust the source list and want actual merged events — use av_log_normalized for that.

### `av_log_range`

**Returns:** Returns JSONL action-log records whose timestamp falls in [from_ms, to_ms], filtered by category/source

**Use when:** When you need a bounded time-slice of a program's own structured action stream (key/move/cast/event/...) around a specific moment, already known to have a category or source you're filtering for, rather than the whole log.

Reads the active profile's `action_log_file` (a single structured JSONL, e.g. log/actions.jsonl) fresh from disk on every call, parses each line as JSON, and keeps records whose ts_ms (or ISO `ts`, parsed as fallback) falls inclusively within [from_ms, to_ms]. Optional `category` is an exact, case-sensitive match against rec['category']; optional `source` is a case-sensitive substring match against rec['source']. Returns up to `limit` records (default 500); when the window still has more matches than `limit`, the most-recent ones are kept, not the earliest. This is a raw single-file read with no cross-log merge and no adapter normalization.

**Why not do it by hand:** Honest convenience, not analysis: it is the same records you'd get from `grep`/`jq` on the JSONL for that time window, minus needing to know the file path or hand-write the time/field filter. It adds no interpretation, scoring, or correlation.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Silently returns count=0 with a 200 (not an error) if the active profile has no action_log_file configured, or if the program uses only the newer log_sources[]/text-log pipeline instead of the legacy structured JSONL (use av_log_normalized for that). Also not for tailing/streaming — it's a one-shot full-file re-read each call, and `category` is exact/case-sensitive so a differently-cased value in the log will silently not match with no warning.

> ⚠ **Caveat (from the code):** Filter asymmetry: `category` must match exactly and case-sensitively (rec.get('category') != category), while `source` is a case-sensitive substring test — a caller passing category='Error' will get zero hits against a log written with category='error', with no indication why. Contrast with _detect_failure_records (used by av_list_bookmarks/av_errors_by_fingerprint), which lowercases category/source before comparing.

### `av_program_log`

**Returns:** Returns the last N lines of the bridged program's plain-text log file, tail-read from disk.

**Use when:** For a quick look at the bridged program's raw text log output -- e.g. right after a crash/hang to see the last plain-text messages, or to sanity-check that the profile's configured `log_file` path is actually being written to -- without opening the file yourself.

Reads the `lines` query param (default 40; aborts HTTP 400 if not an int); gets/lazily-creates the singleton collector for the active profile; calls `ProgramDataReader.tail_log(lines)`, which opens `profile.log_file`, seeks to the last 64KB of the file (or byte 0 if the file is smaller), decodes it as UTF-8 with `errors='replace'`, drops the first line if the read did not start at byte 0 (it may be a partial line cut mid-read), and returns the last `lines` of what remains. Returns `{lines: [...], source: <log_file path>}`; returns `lines: []` (never an error) if `log_file` is unset or the file does not exist.

**Why not do it by hand:** Saves a manual open/seek/tail of the log file, and the 64KB tail-window keeps it cheap even on huge logs. Mechanically it is a thin convenience over `tail -c 65536 log_file | tail -n N` -- it does not parse, level-filter, structure, or time-align the lines; that is what av_log_normalized / av_search / av_errors_by_fingerprint do.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Not for structured or leveled querying (no error/warn filter, no timestamp alignment to frames) -- use av_log_normalized, av_search, or av_errors_by_fingerprint instead. Only reads the profile's single legacy `log_file` field; it does NOT read the newer `log_sources[]` multi-log list (see av_log_sources / av_log_normalized), so a profile configured only via log_sources with no legacy log_file returns `lines: []` even though real log data exists. Because the underlying read window is capped at the file's last 64KB (not scaled by `lines`), requesting a large `lines` count on a log with long lines can silently return fewer lines than asked for.

> ⚠ **Caveat (from the code):** Bypasses the newer generic `log_sources[]` system entirely -- reads only the legacy single `log_file` string field on ProgramProfile (program_connector.py), distinct from `log_sources` and `action_log_file`. A profile declared purely through log_sources (no legacy log_file set) makes this tool return an empty list even though av_log_sources/av_log_normalized see real data for that same program. The 64KB-tail cap is a comment-documented implementation detail, not surfaced in the JSON response, so a caller cannot tell from the response alone whether `lines` was fully satisfied or silently truncated by the byte cap.

### `av_test_adapter`

**Returns:** Classifies one raw log line: winning adapter name, confidence, top-8 scores, parsed event, is_fallback flag

**Use when:** Before wiring a profile to a log source, to check whether AgentVision has a dedicated parser for this program's log format, or when av_program_log/av_log_normalized events look wrong and you want to confirm which adapter is actually matching a given line

Runs log_adapters.detect_adapter([line]), which calls .detect() on every adapter in the REGISTRY (hundreds) against the single line, wrapping each call in try/except (score 0.0 on exception), and keeps the highest scorer (RAW is the guaranteed-nonzero fallback so this never fails outright). It then calls the winning adapter's .parse_line(line) (also try/except, None on failure) to show the structured event, and flags is_fallback=true if the winner is in log_sources.FALLBACK_ADAPTERS = {structural, generic_ts, raw} — meaning no format-specific adapter matched.

**Why not do it by hand:** Runs the full multi-hundred-adapter scoring pass and shows the actual parsed structured event for one line, which is far more informative than eyeballing a raw line and guessing whether it matches a known format.

needs: `none`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Only scores a single line in isolation, not a real sample/window, so multi-line-context adapters (ones that rely on surrounding lines) may score differently than they would on a real file; truncates the echoed `line` field to 300 chars.

> ⚠ **Caveat (from the code):** Docstring for the route says 'query: line=<raw log line>' consistent with claude_mcp.py wrapper; no discrepancy found. Note that if `line` is empty the route 400s before ever touching the detector, which the MCP tool signature (line: str, no default meaning it's required) matches correctly.

### `av_list_adapters`

**Returns:** Paginated catalog of registered log-format adapters: name, language, family, counts.

**Use when:** Before wiring log_sources for a new program, to check whether AgentVision already has a named adapter for its log format/language, or to browse what formats exist under a given family (e.g. 'kernel', 'java') without dumping the whole registry.

Iterates the in-process `connectors.log_adapters.REGISTRY` (built at import time from ~44 core adapters plus the long tail loaded from `connectors/adapters/*.py` family modules such as kernel, security, network, java, go, android, cloud, etc. — 605+ adapter classes total) and buckets each by `type(a).__module__.split('.')[-1]`. All core adapters collapse into one family called `log_adapters`; each family module gets its own family name. It then filters by `family` (matched against either the family bucket or the adapter's `.language`, exact match not substring) and `q` (substring on adapter name), and returns one page (default 50, hard cap 200) plus a full family→count histogram.

**Why not do it by hand:** Cheaper and more precise than grepping the ~30 adapter source files by hand for a class name or docstring keyword; returns exact registered name/language/family and per-family counts in one bounded JSON call instead of scanning hundreds of classes across multiple files.

needs: `none`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Does not tell you which adapter will actually match a specific log file (that is av_test_adapter / detect_adapter's job) and is not live log content — it is a static registry listing only. `family` matching is exact-string against the module's last dotted component or the adapter's language, not a substring or fuzzy search, so an unexpected family name (e.g. querying 'adapters' instead of the specific submodule name) silently returns zero matches.

> ⚠ **Caveat (from the code):** The docstring's family examples ('kernel', 'security', 'network', 'java', 'go') only work because those live in their own file under connectors/adapters/; the ~44 adapters defined directly in connectors/log_adapters.py (JsonLinesAdapter, SyslogAdapter, Log4jAdapter, etc.) all report family='log_adapters' regardless of what language/format they actually parse, since family is derived from the Python module path, not a declared taxonomy. That's not a bug, but it means the 'families' histogram has one oversized bucket that isn't a meaningful category by itself.

### `av_add_adapter`

**Returns:** Builds, validates, registers, and persists a new log-parsing adapter; returns validation/registration result

**Use when:** After av_preflight reports a coverage gap (a program's log lines are only matching the generic/structural/raw fallback adapter) and you have at least one real sample line of that format to build a pattern from — call this before starting capture so subsequent logs get parsed with real fields instead of generic text.

Takes a name plus an extract_regex (named groups ts/level/source/message, with message->msg and timestamp->ts aliased) and a required real sample line, POSTs a spec to /adapter/add. The route calls connectors.adapters.user_adapters.add_adapter(spec, persist=True), which: (1) builds an RxAdapter subclass from the regex/level_map/scope, (2) validates that the sample actually routes to the new adapter and STRICTLY outscores every other currently-registered adapter for that line (ties only breakable via outrank against a named incumbent), (3) checks the new adapter would not steal any sample line out of docs/log_catalog_master.json from an existing named adapter, (4) on success registers it live into the shared adapter REGISTRY and appends the spec to connectors/adapters/user_adapters.json (atomic write) so it reloads on restart. Rejects reusing a built-in adapter's name (would silently shadow it) or a reserved name (structural/raw).

**Why not do it by hand:** Turns opaque raw log lines into structured ts/level/source/message fields for every downstream tool (av_log_normalized, av_search, av_state_at, etc.) permanently — a one-time regex investment instead of re-reading raw text by hand on every future run of the same program.

needs: `log_source_any`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Does not help if you don't have a real sample line yet (sample is mandatory and must round-trip through the built adapter). Cannot override a built-in adapter by reusing its name (rejected to prevent silently shadowing it — must pick a distinct name). `outrank` only breaks an exact-score tie against one named incumbent; it cannot win against a genuinely higher-scoring incumbent pattern. Collision-checking against the catalog is capped at 8 reported collisions.

> ⚠ **Caveat (from the code):** The bridge_server docstring at lines 3201-3216 is accurate and matches user_adapters.add_adapter exactly — route is a thin, faithful wrapper with no extra behavior. One subtlety not obvious from the MCP tool docstring: registration order matters — register_from_spec() without outrank inserts 'just above the tail' (after all built-ins), so a brand-new adapter can only ever win against fallback adapters (structural/generic_ts/raw) or ties broken by outrank, never against another named built-in with a strictly higher score, by design.

### `av_preflight`

**Returns:** Returns a per-log-source coverage verdict {ready, covered, gaps, pending, recommended_actions}

**Use when:** Before the very first av_capture_start on a newly-bridged program, to prove AgentVision has an adapter that specifically parses its logs rather than falling back to a generic line/timestamp parser; re-run after each av_add_adapter call until ready:true.

Resolves language (explicit arg > profile.language > detect_language(project_root)), then assembles sources: the profile's effective_sources() unless explicit log_paths/sample_lines were passed. For each file source it reads up to 40 head lines (_first_lines) and either trusts a source's explicitly-configured adapter or runs the lines through the adapter detector (la.detect_adapter); inline sample_lines are assessed the same way as a synthetic source. Any source resolving only to a generic fallback adapter (structural/generic_ts/raw) becomes a `gap` with the offending sample line and a suggested av_add_adapter call; a source whose file doesn't exist or is empty becomes `pending` (not a gap, doesn't block readiness); everything else is `covered`. ready = at least one assessable source and zero gaps. If ready (or accept_gaps=true), writes a per-profile marker file (.av_preflight_ok in the profile's output folder) that silences the first-capture preflight nag.

**Why not do it by hand:** Replaces manually eyeballing raw log lines against every registered adapter's regex to guess whether AgentVision will parse them correctly; instead gives a machine-checkable ready boolean plus the exact sample line and av_add_adapter call needed to close each gap, and persists that verdict so it isn't re-litigated on every capture start.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Does not ingest, tail, or start reading any log — it only samples up to 40 head lines per configured source, so a log whose early lines are generic boilerplate but whose later lines match a real format can be misjudged. A source with no file yet (program never run) is marked pending, not a gap, so a totally unconfigured/silent program can report ready:false with zero actionable gaps rather than an error. accept_gaps=true writes the 'passed' marker even when real gaps remain, permanently silencing the nag for that profile until the marker is deleted.

> ⚠ **Caveat (from the code):** The Flask route's GET branch only forwards project_root/language from query args — sample_lines, log_paths, and accept_gaps are silently dropped on GET (POST-only). This doesn't affect the av_preflight MCP tool itself since claude_mcp.py always calls _http_post, but any other GET caller of /preflight loses those params without an error.

## `capture` — 4 tool(s)

*Start/stop/tune the screenshot timer.*

### `av_capture_start`

**Returns:** Starts the background screenshot loop; returns {started} or a one-time gate telling you to bridge/preflight first

**Use when:** At the start (or resumption) of a debugging session for a program whose bridge is already built and whose logs have been preflight-checked; also to resume after av_capture_stop. On a brand-new program, expect this to first return bridge_required or preflight_required rather than starting — that is the intended flow, not an error.

POSTs /capture/start. Unless force=true, it runs two one-time gates in order: (1) bridge_plan.is_sealed(profile) — if the program's bridge plan was never committed via av_bridge_commit, it returns {ok:false, started:false, bridge_required:true, bridge:status(...)} and starts nothing; (2) a per-profile preflight marker file — if av_preflight has never run for this profile, it computes the preflight verdict on the spot and returns {preflight_required:true, preflight:verdict, started:false}, again starting nothing. Both gates respond with HTTP 200, not an error code. Once past the gates (or force=true bypasses both), it optionally applies `interval` (clamped to a 0.1s floor) to the shared AutoCaptureEngine and calls its start(), spawning a daemon thread (no-op if already alive). That thread does nothing until it detects the bridged program is actually running (collector._reader.is_running()); only then does it start looping: each iteration screenshots (by window id if capture_app is configured and a window is found; by crop rect; or full-screen if no capture_app was named at all), runs collect() + a perceptual-hash/health check off the same image decode, burns the overlay, and writes PNG+JSON sidecar, snapshotting log byte-offsets at the same instant as the shutter for time-alignment. The response echoes {ok, started, interval, shots_per_second, rate: capture_rate_info(...)}.

**Why not do it by hand:** This is the primitive that produces every frame + time-aligned log-offset sidecar other av_* tools read; there is no manual equivalent short of writing your own screenshot+log-offset loop with atomic timestamp snapshotting, which is the actual hard part this solves.

needs: `bridge_sealed`  
cost: `free`  
languages: `any`  
program kinds: gui, game

**Not for:** Profiles with no capture_app configured get whole-desktop screenshots by design ('Priority 3: full screen — ONLY when no capture_app was named'), which leaks unrelated windows and is low-signal for a headless/CLI/service target. Profiles WITH capture_app configured but no matching window currently open silently SKIP frames (0 written, not an error) unless AGENTVISION_ALLOW_FULLSCREEN_FALLBACK=1. Also does not itself return any frame/image data — just a status envelope.

> ⚠ **Caveat (from the code):** Both the bridge_sealed gate and the preflight gate return HTTP 200 with started:false rather than a 4xx — a caller that only checks status code will misread a blocked start as success; must check the `started`/`bridge_required`/`preflight_required` fields. Also, calling this with an unmet preflight gate computes but does NOT persist the preflight marker (that only happens via the dedicated /preflight endpoint or after a real start) — so retrying the identical call without force repeats the same guidance rather than remembering it was already shown.

### `av_capture_stop`

**Returns:** Stops the auto-capture loop; always returns {"ok": true}

**Use when:** To pause frame capture (e.g. between debugging sessions, or before av_session_report) without tearing down the bridge, or to stop burning disk/CPU on screenshots you no longer need.

POSTs /capture/stop, which calls AutoCaptureEngine.stop(): sets a threading.Event that the capture loop checks between iterations, and immediately flips `running`/`capturing` to False in the shared engine object (before the background thread itself has necessarily woken up and exited). It does not join the thread, does not delete any frames already written to disk or memory, and does not touch the bridge/profile/plan state at all — the bridge server keeps running and answering every other endpoint.

**Why not do it by hand:** Trivial control-plane action with no manual equivalent other than killing the whole bridge process; safe to call even if capture was never started (idempotent no-op, never errors).

needs: `none`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Does not clear or retain-prune existing frames (see av_retention for that), does not stop log tailing/adapters, and does not wait for a screenshot already in flight to finish — that frame may still land on disk after this call returns.

### `av_capture_set_interval`

**Returns:** Sets the auto-capture screenshot cadence; returns applied interval/fps + rate envelope

**Use when:** After av_capture_start, when the debugging target changes speed — speed up (smaller interval) for an animation, race, or crash about to happen; slow down for a long idle wait.

PUTs {interval} to /capture/interval (bridge_server.py:3333-3342), which calls AutoCaptureEngine.set_interval() (line 548) to clamp the value to CAPTURE_MIN_INTERVAL (default 0.1s = 10fps, env-overridable) with no upper bound, then stores it on the running engine. The capture loop (_loop, lines 555-589) reads self.interval only after finishing the current frame and computing its sleep, so the new cadence takes effect starting the next tick, not mid-frame. The response echoes {ok, interval, shots_per_second, rate} where rate is capability_rate_info() — the same envelope object returned by /capture/status.

**Why not do it by hand:** Thin, direct control — one call replaces manually restarting capture with a different rate. No analysis is performed; it is a knob, not an insight tool.

needs: `capture_running`  
cost: `free`  
languages: `any`  
program kinds: gui, game

**Not for:** Does not start or stop capture (use av_capture_start/av_capture_stop) and does nothing if no capture engine is running yet — it will still report an 'applied' interval even though there is no active loop consuming it, since set_interval() just writes an attribute on the engine object regardless of engine.running state.

> ⚠ **Caveat (from the code):** Assignment brief for this tool claimed 'NO route - pure local logic', but that is incorrect: claude_mcp.py:784 calls _http_put('/capture/interval', ...) and bridge_server.py:3333 defines a real Flask route for it (capture_set_interval). The tool is not pure local logic; it's a normal HTTP-backed MCP tool like the others. Documented above from the actual route/handler.

### `av_capture_status`

**Returns:** Returns capture-loop run state, frame count, health telemetry, and the fps rate envelope

**Use when:** At the start or continuation of any project (to read rate.guidance and ask the user for a shots/sec preference), or when diagnosing why capture looks broken/stalled (blank frames, missing window, no frames arriving).

Calls GET /capture/status, which reads live attributes off the singleton AutoCaptureEngine (_auto_engine): engine_running (background thread alive), capturing (currently taking shots vs. paused because the target program isn't running), interval and derived shots_per_second, frame_count (authoritative count from the in-memory _frames dict, reflecting disk state, not the engine's own session counter), last_error, and a health block (last_frame_ms, last_latency_ms, blank_frame_count, window_missing, frames_skipped_no_window, last_warning, plus a static explanatory 'only_the_program' string). It also calls capture_rate_info(interval) to attach a rate block describing the full supported interval/fps range (CAPTURE_MIN_INTERVAL/CAPTURE_MAX_INTERVAL, default 0.1s-10s i.e. 10fps-0.1fps), the narrower GUI slider range, how to change it (av_capture_set_interval etc.), and an imperative guidance string instructing the agent to ask the user for a desired fps at project start.

**Why not do it by hand:** Surfaces internal engine state (thread-alive vs actively-shooting distinction, blank-frame/window-missing counters, latency) that is not written to any log or file the agent could otherwise read; it is live process memory, not a persisted artifact.

needs: `none`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Does not itself start or change capture — it is read-only telemetry. capturing=false with engine_running=true just means the target program isn't currently detected as running (normal idle state, not necessarily an error) — check health.window_missing/last_warning to distinguish a real problem from an idle wait.

## `source` — 8 tool(s)

*The target program's own source code, indexed.*

### `av_source_light`

**Returns:** Per-file JSON map of the bridged project: path, lang, line count, optional 1-line summary

**Use when:** First call when orienting to a newly bridged project, before deciding which files to open or digest further.

GETs /source/light, which lazily builds (via source_mirror.refresh) a source_index.json for the active profile if one doesn't exist yet, then reads source_light.json and returns it verbatim. For each indexed file it always includes path/lang/lines; it adds a 'summary' field ONLY for lang=='python' (AST module docstring, falling back to the first non-comment line) and lang=='markdown' (first non-blank line), plus a 'sym':{f,c} func/class count for Python. All other languages (js, ts, rust, go, java, html, css, json, yaml, etc.) get no summary at all. Files under IGNORE_DIRS (node_modules, .git, dist, build, data, assets, models, datasets, vendor, etc.) and IGNORE_SUFFIXES (images, binaries, archives, etc.) are excluded entirely from the walk.

**Why not do it by hand:** Avoids an agent-driven find/ls + per-file skim of the whole repo; returns a cached, pre-walked, noise-filtered map in one call (docstring claims ~5-15K tokens for a few-hundred-file repo). For non-Python/non-Markdown codebases the benefit shrinks to little more than a filtered file listing, since no summary text is generated for those languages.

needs: `source_index`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Reading actual code logic or symbol signatures (only python/markdown get any descriptive text at all; every other language is just path+lang+lines). Also not reliable if the project keeps real source under a directory literally named data/, assets/, models/, datasets/, or vendor/ — those whole subtrees are hard-excluded from the index with no override exposed by this tool.

> ⚠ **Caveat (from the code):** The claude_mcp.py docstring says every file gets 'a 1-line summary', but build_light_digest() in source_mirror.py only populates 'summary' for lang=='python' or 'markdown' — all other languages (js/ts/rust/go/java/rb/lua/sh/html/css/json/yaml/toml/ini/sql/other) silently get no summary field. This is a real docstring/behavior mismatch for non-Python projects.

### `av_source_tree`

**Returns:** Hierarchical JSON file tree (dirs then files, alphabetical) of the bridged project, no content

**Use when:** Before drilling into a specific file or subdirectory, when the agent needs the folder layout/shape rather than any file's contents.

GETs /source/tree, which lazily builds the source index for the active profile if missing (same source_mirror.refresh as av_source_light), then reads and returns source_tree.json. This is a nested {name,type,children} structure built by build_tree(): directories carry only name/type/path/children, files additionally carry size, lines and lang. It applies the same IGNORE_DIRS/IGNORE_SUFFIXES filtering as the rest of the source mirror, and sorts each directory's children with subdirectories first, then files, both alphabetically.

**Why not do it by hand:** Replaces a manual recursive directory listing (find/ls -R) with one pre-filtered, pre-sorted JSON tree that already strips build output, dependency dirs, VCS metadata and binary/log/image files — the agent doesn't have to re-derive or apply its own ignore list.

needs: `source_index`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Understanding what any file contains — there is zero symbol or summary data here, purely structural (name/size/lines/lang). Use av_source_light for a 1-line orientation or av_source_digest for signatures. Same blind spot as av_source_light: directories named data/, assets/, models/, datasets/, vendor/, etc. are entirely absent from the tree, not just unsummarized.

> ⚠ **Caveat (from the code):** Shares source_index.json with av_source_light/av_source_digest/av_source_refresh — if that index is stale (project files changed on disk after the last build), the tree silently reflects the old layout until POST /source/refresh is called; there is no mtime-based auto-invalidation, only lazy build-if-absent.

### `av_source_digest`

**Returns:** Full per-file symbol digest (signatures + 1-line docstrings) for the project or a path prefix.

**Use when:** After av_source_light or av_source_tree gives orientation, when you need real function/class signatures and docstrings for one subdirectory to decide which file to open next with av_source_file.

Lazily builds the source mirror (via _ensure_source_built) if source_index.json doesn't exist yet for the active profile, then reads the cached source_digest.json. For each indexed file it returns extracted top-level symbols: Python via ast.parse (functions/classes with first docstring line, plus UPPER_CASE module constants); JS/TS/Rust/Go via regex; markdown as headings; JSON/YAML/TOML/INI as a text preview; anything else as its first non-blank line. The optional `prefix` param filters the loaded `files` list to paths that start with it, in Python, after the full digest JSON is already read from disk. Files over 2MB or 5000 lines are indexed but excluded from symbol extraction (skipped: too_large/too_many_lines).

**Why not do it by hand:** Collapses a whole source tree into a ~10-50KB signature map instead of opening every file — by design it strips imports and function bodies down to signatures/docstrings (source_mirror.py: 'enough to know what's in the file, NOT enough to read its logic'), so it cannot answer questions about implementation logic, only what exists and where.

needs: `source_index`  
cost: `medium`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Not for reading implementation logic (bodies are deliberately stripped). Files >2MB or >5000 lines return no symbols at all. The digest is a static snapshot: /source/digest only rebuilds when source_index.json is completely absent, so it is never auto-refreshed after a file edit — call av_source_refresh first if you need current signatures/line numbers.

> ⚠ **Caveat (from the code):** FIXED (2026-07-30). _ensure_source_built now compares the index's build time against the mtime of every indexed-eligible file (a stat-only walk reusing the mirror's skip rules, cached for 5 s) and rebuilds when the project has changed, reporting rebuilt_because + staleness. Verified: adding a file to a project made the next digest call include it without an explicit av_source_refresh.

### `av_source_file`

**Returns:** Returns one project source file's lines verbatim, optionally sliced by from_line/to_line.

**Use when:** After av_source_digest, av_source_light, or av_source_search has named an exact path (and ideally a line number), to pull just those source lines instead of asking the user to paste code or reading the whole file blind.

Resolves `path` against the active profile's project_root, rejecting any path containing '..' and re-confirming after resolve() that the target is still inside project_root. Reads the whole file as UTF-8 (decode errors replaced), splits it into lines, and returns the slice from from_line to to_line (to_line defaults to the last line, i.e. the whole file) along with total_lines. It is a plain file read gated only by path-traversal checks — it does not consult source_index.json, source_digest.json, or any cache, so it works even if av_source_refresh was never run.

**Why not do it by hand:** Avoids a context round-trip through the user, and from_line/to_line pagination lets the caller pull only the relevant slice of a huge file — but it adds no intelligence beyond that: no symbol lookup by name, no syntax awareness; the caller must already know the exact relative path.

needs: `none`  
cost: `medium`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Not for discovery/orientation (path must already be known exactly, relative to project_root with forward slashes, or it 404s) and not for files outside project_root (blocked by the traversal/resolve() check). There is no line-count or byte-size cap on this endpoint: omitting to_line on a multi-thousand-line file returns it in full in one response, unlike the digest builder which caps at 5000 lines / 2MB.

> ⚠ **Caveat (from the code):** bridge_server.py:2965-2997 has no server-side size cap, unlike source_mirror's digest path (DIGEST_MAX_LINES=5000, MAX_FILE_BYTES=2MB) — a caller that omits to_line on a large generated file will get the entire file back in one JSON response. Path-traversal handling itself is solid: it blocks literal '..' path segments AND re-validates target.relative_to(root.resolve()), so symlink/relative tricks are still caught by the second check.

### `av_source_search`

**Returns:** Substring search across the bridged project's indexed source; returns {path, line, text} matches

**Use when:** When you need to locate which file/line a symbol, string literal, log-format string, or identifier lives at in the bridged project, before opening a specific file with av_source_file.

GETs /source/search with q (required), case (0/1, default 0), limit (default 200, server clamps 1-2000). It lazily builds source_index.json for the active profile if missing (same source_mirror.refresh as av_source_light/tree), 404s if project_root doesn't exist or the index still can't be built. For every indexed file it skips only files whose lang is 'other' AND line count > 5000; every recognized-language file (python, js, ts, rust, go, java, etc.) is read in full with no size cap, lower-cased (unless case=1) and substring-checked against the query. On a hit it walks the file line by line, collecting {path, line (1-based), text (truncated to 240 chars)} until 'limit' matches are reached, then returns early with truncated:true; otherwise returns all matches with truncated:false, plus match_count and files_with_hits.

**Why not do it by hand:** One call gets pre-filtered results (IGNORE_DIRS/IGNORE_SUFFIXES already exclude node_modules, .git, build output, binaries, data/assets/vendor dirs) as structured {path,line,text} instead of the agent shelling out to grep and parsing its own noise; index is cached so repeat searches don't re-walk the filesystem. It is otherwise a plain literal-substring scan, not smarter matching.

needs: `source_index`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Regex or fuzzy matching — despite the docstring calling it 'Grep-style', matching is a plain case-folded substring containment check (`needle in line`), no regex/wildcards. Also not reliable for source changed since the last index build: source_index.json is built once and reused, so new or edited files are invisible until POST /source/refresh is called explicitly (no mtime-based auto-invalidation).

> ⚠ **Caveat (from the code):** The size-skip only applies to lang=='other' files over 5000 lines; any file with a recognized extension (python/js/ts/rust/go/java/json/yaml/...) is read into memory and scanned in full regardless of size, unlike file_digest() elsewhere in source_mirror.py which enforces MAX_FILE_BYTES (2MB) and DIGEST_MAX_LINES (5000). A huge generated file with a recognized suffix (e.g. a multi-MB .json or .py data file) would be fully loaded and line-scanned on every matching search.

### `av_source_list`

**Returns:** Flat JSON list of every indexed file's path, size, line count, and language

**Use when:** When you need the complete, exact inventory of indexed file paths (e.g. to confirm a specific file exists, or to enumerate all files of one language) rather than a token-frugal summary.

GETs /source/list, which lazily builds source_index.json for the active profile if missing (same lazy-build path as av_source_search/tree/light), 404s if project_root is missing or the index can't be built or doesn't exist yet. It then reads the cached source_index.json verbatim and returns project_root, profile name, file_count, and a files array of {path, size, lines, lang} for every entry — no filtering, no pagination, and no sha1/mtime (present in the underlying index but dropped here).

**Why not do it by hand:** Equivalent to `find . -type f` plus `wc -l` and extension classification already run and cached, with the same noise-directory/binary-suffix filtering as the rest of the source mirror applied for you. Beyond that it is a thin pass-through of the index file with no analysis added.

needs: `source_index`  
cost: `medium`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Getting a token-frugal project overview on a large repo — there is no limit/pagination parameter, so every indexed file is returned in one response; use av_source_light or av_source_digest instead for a summarized/compressed view. Also stale until POST /source/refresh: it reads the same cached source_index.json as av_source_search, which is only built once and not invalidated when project files change.

> ⚠ **Caveat (from the code):** No pagination or filtering by lang/path is exposed, unlike av_source_search's limit param — on a project with thousands of indexed files this returns one large unbounded JSON array.

### `av_source_refresh`

**Returns:** Re-walks the bridged project's file tree and rebuilds the cached source index/light/digest/tree files

**Use when:** First thing when starting a session on a newly bridged project (av_source_light/av_source_tree/av_source_digest all 404 with 'not built yet' until this has run once), or after the user's source tree has changed enough that the cached maps are stale.

POST /source/refresh calls source_mirror.refresh(project_root, profile, source_base, do_mirror). This walks the project directory (skipping IGNORE_DIRS like .git/node_modules and hidden/binary-ish files via IGNORE_SUFFIXES), computes per-file size/mtime/line-count/sha1/lang for every remaining file into a SourceIndex, then writes source_index.json, source_light.json (1-line-per-file summary), source_digest.json (per-file def/class signatures — AST-based for Python, regex-based for JS/TS/Rust/Go, preview text for markdown/json/yaml/etc.), and source_tree.json to snapshots/<profile>/. If mirror=True is passed, it also copies every indexed file into snapshots/<profile>/source_mirror/, skipping files whose mirrored copy's mtime already matches (and deleting mirrored files whose source no longer exists).

**Why not do it by hand:** One call rebuilds four token-efficient project views instead of manually walking the tree and reading every file; mtime-gated mirroring avoids re-copying unchanged files on repeat calls.

needs: `none`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Not for reading source content itself — it only rebuilds the caches, use av_source_light/av_source_tree/av_source_digest afterward to actually read them. Cannot scope to a subdirectory (always walks the whole project_root). Files over MAX_FILE_BYTES (2MB) or over the digest line cap are still indexed but their digest entry is just {"skipped": "too_large"/"too_many_lines"} — no symbols extracted. Fails with a 404 JSON error (not an exception) if the active profile's project_root doesn't exist.

> ⚠ **Caveat (from the code):** In source_mirror._file_entry, the `if st.st_size > MAX_FILE_BYTES: pass` branch (line ~168) is dead code — the comment claims it affects 'symbol extraction downstream' but the branch body is a no-op; the actual size gate that skips digesting is a separate, independent check in file_digest() (line ~354). Cosmetic/misleading, not a functional bug.

### `av_codebase_map`

**Returns:** Returns an ASCII file tree with per-file line counts plus a capped Python import-dependency edge list.

**Use when:** At the start of work on an unfamiliar project, before deciding which files to open, or to see which local Python modules import which others.

Calls build_map() on the active profile's project_root (defaults to '.' if unset), which os.walks the tree up to depth 4, skipping hidden dirs and node_modules/.git/venv/.venv/dist/build/.build/__pycache__/DerivedData, and appends every .py/.ts/.tsx/.js/.swift file with its line count into one ASCII-indented string. Separately it ast.parse()s every .py file under the root, collects Import/ImportFrom names that match another local module's filename stem, and builds up to 50 deduplicated 'source.py -> target.py' edge strings (non-Python imports and non-project modules are dropped silently). Every call re-walks the disk and re-parses every .py file from scratch — nothing is cached.

**Why not do it by hand:** Saves manually walking directories and grep-parsing imports; reconstructing the Python dependency edges by hand would mean opening and reading every .py file. For non-Python trees it degrades to an annotated file listing with line counts, no better than 'find + wc -l'.

needs: `none`  
cost: `medium`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Not for finding specific symbols/functions inside a file (only filename + line count) -- use av_source_search or av_source_digest for that. dependency_edges is Python-only: TS/JS/Swift files appear in the tree but never generate edges. file_tree has no size cap (only depth<=4), so very large repos can return a large payload. Edge list silently truncates at 50 with no truncation flag in the response.

> ⚠ **Caveat (from the code):** The /codebase-map route accepts a `root` query-string override, but the av_codebase_map() MCP wrapper takes no arguments and never forwards one, so from the agent's side the root is always the active profile's project_root. Also, if project_root doesn't exist on disk, build_map() does not error: os.walk() on a missing path silently yields nothing, so the tool returns a valid-looking response with total_files=0, total_lines=0, empty tree/edges -- there is no explicit 'root not found' signal.

## `retention` — 3 tool(s)

*Disk budget and the examine-before-delete queue.*

### `av_retention`

**Returns:** Reports disk-budget usage, frames still awaiting examination, and any unexamined frames dropped

**Use when:** Before raising the capture rate or switching examine mode to `all`; when asking whether AgentVision is filling disk; to check whether any flagged frames were lost before being examined (dropped_unexamined > 0).

Calls GET /retention, which reads the in-process retention Ledger (utils/retention.py) and returns its report(): bytes used vs. the configured byte budget (default 5GB), counts of frames evicted/archived, and integrity.dropped_unexamined (frames the examine policy flagged but that expired before the agent fetched them). The route also appends the list of frames currently awaiting examination (Ledger.awaiting(limit=25)), free disk space, and the relevant AGENTVISION_* env knobs. Although the underlying Flask route also accepts POST to reconfigure mode/budget/hold_seconds live, the av_retention MCP tool itself only issues a GET and exposes no parameters, so it is read-only from the agent's side.

**Why not do it by hand:** Surfaces a byte-budget ledger and an eviction/hold policy that has no equivalent in a raw log or filesystem listing — in particular the dropped_unexamined integrity counter, which tells the agent it silently missed evidence, something `ls` on the frame directory cannot reveal.

needs: `capture_running`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Not for reading frame contents or logs themselves — it only reports the bookkeeping/policy state around stored frames. If capture has never run, the ledger will simply report near-zero usage.

> ⚠ **Caveat (from the code):** The MCP tool wraps only the GET behavior; the same /retention route also supports POST (mode/budget/hold_seconds reconfiguration) but that path is not reachable through av_retention() since the tool takes no arguments and always calls _http_get.

### `av_frames_awaiting`

**Returns:** Lists flagged-for-review frames not yet examined, most urgent first, with hold-expiry countdowns

**Use when:** After AgentVision's push channel says 'N frames are waiting on your eyes', or any time before deciding what to inspect next, to see what flagged visual evidence is outstanding without opening any images.

Reads the in-memory retention Ledger (utils/retention.py) for records where needs_eyes is True, examined_ms is still None, and not expired, sorted by priority then recency. For each of up to `limit` rows it returns seq, priority, reason, failure flag, age_seconds, hold_expires_in_seconds, whether it was already offered, and byte size; it also computes a total_awaiting count (by calling Ledger.awaiting a second time with limit=100000) and a one-line push_sentence summary.

**Why not do it by hand:** Acts as a priority-sorted unread-inbox of only the frames the recorder judged worth a look (not every captured frame), with an expiry countdown — the agent does not have to poll or diff every screenshot to find what changed or failed.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, game

**Not for:** Not a general frame browser — it only ever shows frames flagged needs_eyes, never the full capture history; if capture never ran or nothing was flagged it just returns an empty list (no error), so an empty result is not proof capture is broken.

> ⚠ **Caveat (from the code):** The route calls _ret.LEDGER.awaiting() twice per request (once for the limited `rows`, once with limit=100000 just to get `total`), re-scanning and re-sorting the whole ledger both times — harmless at expected ledger sizes but a redundant O(n log n) pass.

### `av_examine_ack`

**Returns:** Marks given (or all) awaiting frames as examined, releasing their full-res pixels for eviction

**Use when:** After a JSON descriptor, thumbnail, or region read already answered the question and the full-resolution pixels are not needed, or to bulk-clear the whole awaiting queue with all=1.

POST handler reads seqs from the JSON body (or query args as fallback) as a list or comma-separated string, or takes every currently-awaiting seq if all=1. It calls Ledger.mark_examined_many(seqs, 'ack'), which sets examined_ms/examined_how='ack' on each matching record the first time only and increments the ledger's examined/acked stats. Returns how many were newly acked, how many were requested, how many were already-examined-or-unknown, and how many remain awaiting.

**Why not do it by hand:** Lets the agent explicitly discharge its obligation on a flagged frame instead of relying on the hold-timeout backstop (which the ledger separately counts as a real loss, 'dropped_unexamined') — keeps the retention accounting honest about what was actually looked at versus what merely expired.

needs: `none`  
cost: `free`  
languages: `any`  
program kinds: gui, game

**Not for:** Does not delete or affect descriptors/thumbnails (those are kept regardless of acking) and does not inspect a frame's content itself — it is purely the release/bookkeeping step; calling with no seqs and all=0 is a 400 error ('send seqs=[...] or all=1'), but all=1 against an empty queue is a valid no-op.

> ⚠ **Caveat (from the code):** Non-numeric entries in `seqs` (e.g. "abc") are silently filtered out before `requested` is computed, so a malformed seq value never surfaces as an error or a count mismatch in the response — it just quietly vanishes from the request.

## `health` — 4 tool(s)

*Is AgentVision itself working?*

### `av_selftest`

**Returns:** Runs live capture/window-enum/input-hook/daemon checks and returns pass-fail JSON

**Use when:** Before the first capture on a new machine or OS (especially Windows, where this is the definitive proof the low-level input hook actually fires), or when a program seems bridged but nothing is happening and you need to rule out a broken capture/input/daemon runtime rather than a program-side issue.

Captures a fixed 64x64 top-left screen region to a temp PNG and uses Pillow (if available) to confirm it is not blank/black; enumerates OS top-level windows (on Linux this is session-aware — reports ok=null on Wayland since global enumeration is impossible, and reports the missing-display case when headless); on Windows or Linux spawns `python -m python_backend.daemon.input_daemon --selftest` as a subprocess (10s timeout) which does a one-shot SendInput hook probe on Windows or an evdev-readability check on Linux, and parses its 'AV_SELFTEST_JSON' stdout line; and reads the input-daemon PID file to report whether the daemon process is alive. All checks are wrapped in try/except so nothing raises — failures show up as ok:false in the JSON instead.

**Why not do it by hand:** Proves the underlying OS mechanisms (screen capture, window enumeration, input hook delivery, daemon liveness) actually work on this exact machine instead of assuming they do; on Windows/Linux it drives a real subprocess handshake you could not easily reproduce by hand.

needs: `none`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Not a check of the bridged program itself — it says nothing about log correctness, adapter parsing, or application errors. On macOS the input_hooks check is always ok=null ('no one-shot input self-test on this OS') since no probe exists for that platform, so input delivery is never actually verified there. window_enum reports ok=null (not a failure) on Wayland by design.

> ⚠ **Caveat (from the code):** FIXED (2026-07-30). The input_daemon check is now a real assertion: ok = (running or not required_by_profile), with `running` measured from the daemon status and `required_by_profile` from the profile's capture_user_input. An absent daemon nobody asked for reports ok with a note; asked-for-but-dead reports ok=False and lands in failed_checks, which flips the overall verdict. Pinned by python_backend/api/test_tool_contracts.py.

### `av_daemon_status`

**Returns:** Returns liveness + config of the system-wide input-recorder daemon as JSON

**Use when:** When expected keyboard/mouse action events are missing from the JSONL log — to check whether the daemon process is even running, which profile/sink it is currently writing to, and whether physical-input capture is enabled.

GET /daemon/status (bridge_server.py:3462-3498) reads a PID file (platform_shim.daemon_pid_file(), a per-OS temp path) and checks liveness with platform_shim.pid_alive() (psutil-based, os.kill(pid,0) fallback on POSIX). It also reads active_profile.txt to find which profile the daemon is currently bound to, loads that profile via load_profiles(), and reports its action_log_file (the JSONL sink path) and its capture_user_input opt-in flag. The daemon itself (python_backend/daemon/input_daemon.py) is a separate long-running process that taps the OS input layer (CGEventTap on macOS, WH_*_LL hooks on Windows, evdev on Linux) and by default records only SYNTHETIC input events (bot/RPA-injected), dropping physical human keyboard/mouse activity unless capture_user_input is on for the active profile.

**Why not do it by hand:** Answers 'is the daemon alive and pointed at the right profile' in one JSON call instead of manually finding the OS temp dir, reading the pid file, checking process liveness, and cross-referencing active_profile.txt and profiles.json by hand.

needs: `input_daemon`  
cost: `free`  
languages: `any`  
program kinds: gui, cli, service, game

**Not for:** Does not report the daemon's recent captured events themselves (use the log/JSONL reading tools for that), and does not start/stop the daemon. All failures inside the two try/except blocks (bad pid file, missing/corrupt profile) are swallowed into pid_error / profile_error string fields rather than raising, so a broken daemon setup surfaces as still-populated JSON with running:false plus an error string, not an HTTP error.

### `av_program_status`

**Returns:** Whether the bridged program's OS process is alive right now, plus its live CPU% and RAM (GB).

**Use when:** Before trusting a capture/log session as reflecting a live program, or when a symptom might be a hang/crash/leak: check whether the target process is actually still running and whether CPU or RAM has spiked, without shelling out to ps/top yourself.

Calls the active profile's ProgramDataReader.is_running() and process_cpu_ram() (python_backend/connectors/program_connector.py). Each independently scans every running OS process via psutil.process_iter() and applies the same _process_matches heuristic against the profile's process_name/project_root/exe: exe-path-under-project-root wins outright, known dev-tool markers (compilers, editors, git, build servers) are excluded even if they mention the project path in argv, and a bare generic interpreter name (e.g. 'python3') requires project_root corroboration in cmdline. process_cpu_ram() then samples cpu_percent with a blocking 0.1s psutil interval and reads RSS memory converted to GB. Returns {running, program: profile.display_name, cpu_percent, ram_gb}.

**Why not do it by hand:** Saves you from parsing ps/top output and reimplementing project-vs-dev-tool disambiguation yourself; the matcher already filters out build servers, editors, and other tools that merely reference the project's path in their arguments, which a naive name/cmdline grep would misidentify as the target.

needs: `none`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Not a check of whether the program's WINDOW is visible/focused (that's a capture-side concern, not this route), and it is an instantaneous snapshot, not a timeline. If the active profile's process_name is empty or too generic and project_root is unset, matching can silently fail (running=false) even though the program is running; conversely a custom profile with a loosely-set project_root could still be fooled by a tool not on the excluded dev-tool marker list.

> ⚠ **Caveat (from the code):** is_running() and process_cpu_ram() each run their own full psutil.process_iter() scan and re-apply the identical _process_matches logic, i.e. the target process is located twice per call to this one route, and process_cpu_ram()'s cpu_percent(interval=0.1) blocks ~100ms. Functionally correct but redundant work; could be collapsed into one scan returning (running, cpu, ram) together.

### `av_run_tests`

**Returns:** Runs the bridged project's pytest suite via subprocess and returns pass/fail/skip counts and failure detail.

**Use when:** After making a code change to the bridged project, to check whether its pytest suite still passes and get a quick per-failure summary without the agent invoking pytest itself via Bash.

POSTs (with an empty body, since the MCP tool takes no parameters) to /run-tests, which calls failure_explainer.run_tests(project_root) using project_root = the active profile's configured project_root, or '.' if none is set. run_tests() shells out to `python -m pytest --tb=short --json-report --json-report-file=<root>/.agentvision_test_report.json --quiet` with a hardcoded 60-second timeout and cwd=project_root. If the pytest-json-report file appears, it is parsed for pass/fail/skip counts, per-failure nodeid, traceback (longrepr), and a per-failure 'likely_cause' string produced by a simple keyword-substring classifier over the lowercased traceback (checks for strings like 'assertionerror', 'typeerror', 'keyerror', 'timeout', etc.); the report file is then deleted. If no JSON report file is produced, it falls back to regexing plain stdout/stderr for 'N failed/passed/skipped' and lines starting with 'FAILED '. The HTTP response truncates failure_detail to its last 500 characters.

**Why not do it by hand:** Gives a structured JSON summary (counts, failed test ids, per-failure traceback and a rule-based likely_cause) instead of raw pytest console output, but it is not doing anything the agent couldn't get by running `pytest --json-report` directly — the likely_cause field is a shallow substring match on exception type names, not real diagnosis.

needs: `none`  
cost: `low`  
languages: `python`  
program kinds: cli, library, service, headless, gui, game

**Language limit:** run_tests() hardcodes the subprocess command `python -m pytest` — it only works for Python projects that use pytest; it cannot invoke any other test runner or language's test tooling.

**Not for:** Not usable for non-Python projects or Python projects that don't have pytest installed — the subprocess call is hardcoded and unconfigurable timeout/runner-wise from this tool. Not suited to long-running suites: the 60s timeout is fixed in run_tests() and the av_run_tests MCP tool exposes zero parameters (no project_root, no timeout override) even though the underlying function and HTTP route both accept both. A subprocess.TimeoutExpired or FileNotFoundError (e.g. 'python' missing from PATH) is not caught inside run_tests()/trigger_tests(), so it propagates up to the app-wide error handler as a generic 500 rather than a structured test-runner failure. The JSON-report parser also has a bare except Exception: pass, so a malformed/partial report can silently yield an all-zero pass/fail/skip result with no failures listed and no indication parsing failed.

> ⚠ **Caveat (from the code):** Capability gap: the av_run_tests MCP tool signature takes no arguments at all, so callers cannot pick a project_root or raise the 60s timeout, even though bridge_server's /run-tests route already reads project_root from the POST body and failure_explainer.run_tests() already accepts both project_root and timeout parameters — the plumbing exists but is not exposed through the tool.

## `program` — 3 tool(s)

*Target-process stats and cropping.*

### `av_program_stats`

**Returns:** Returns a fixed set of parsed key/value stats from the newest stats_*.log file

**Use when:** The bridged program is a stats-emitting bot/game process (e.g. a leveling bot) and you want its numeric session stats (session length, games played, XP/hour, level, time-to-level) without opening the stats file yourself.

Hits /program/stats, which finds the most-recently-modified file matching stats_*.log in the profile's stats_folder, reads it whole, and runs it through _parse_stats_block(). That parser strips box-drawing border characters (╰│├╰ etc.) and, for any 'key: value' line, only keeps the value if the lowercased key matches one of nine hardcoded substrings: session_length, games, avg_game, current_level, xp_per_hour, xp_per_game, time_needed, games_needed, xp_gained. Everything else in the file is silently dropped. Returns {stats, source, program} where stats is that filtered dict (often {} if the folder or a matching file doesn't exist, or if no line matches the whitelist).

**Why not do it by hand:** Saves you from finding the newest stats_*.log by mtime and hand-parsing its box-drawn table, but only for programs whose stats format matches the hardcoded key set; for anything else it is strictly worse than reading the file, since matching keys are silently dropped.

needs: `log_source_any`  
cost: `free`  
languages: `any`  
program kinds: headless, gui, game, cli

**Not for:** Any stats format outside the hardcoded 9-key whitelist (session_length/games/avg_game/current_level/xp_per_hour/xp_per_game/time_needed/games_needed/xp_gained) — those fields are silently discarded with no error. Also wrong when no stats_folder is configured on the active profile.

> ⚠ **Caveat (from the code):** FIXED (2026-07-30). `lines` reaches latest_stats() and limits the read to the last N lines. The parser returns EVERY `key: value` pair (the nine Diablo-II-bot keys remain as aliases) and strips table borders instead of discarding boxed rows, which had thrown away every row of a rendered table.

### `av_program_crop`

**Returns:** Returns the capture_app name, configured crop string, live window bounds, and the effective active crop

**Use when:** A captured frame looks offset, clipped, blank, or shows the wrong content, and you need to know exactly what screen region AgentVision is screenshotting for the bridged program right now.

Hits /program/crop, which reads the active profile's capture_app and capture_crop fields, calls reader.get_window_bounds() (delegates to utils.platform_shim.find_window — Quartz CGWindowListCopyWindowInfo on macOS, win32gui EnumWindows on Windows, xwininfo on Linux — matched by capture_app name) to get live (x,y,w,h) if that window currently exists, and calls reader.get_capture_crop() to parse a manual 'x,y,w,h' override string from the profile. Returns {capture_app, capture_crop, window_bounds, active_crop} where active_crop defaults to window_bounds but is overridden by the manual crop if one is set.

**Why not do it by hand:** A single call resolves the same window-lookup + crop-override precedence logic the capture pipeline itself uses, instead of you re-deriving it from the profile config and OS window APIs by hand.

needs: `window_visible`, `gui_program`  
cost: `free`  
languages: `any`  
program kinds: gui, game

**Not for:** Headless/CLI programs with no window — window_bounds will be null and, absent a manual capture_crop, active_crop will also be null (full-screen fallback is decided elsewhere in the actual capture path, not reflected here). Does not tell you whether the window is occluded or minimized, which on Windows silently changes what capture_frame actually grabs.

### `av_ambient`

**Returns:** Returns the tiny push-mode text a hook would inject right now, or inject=false if nothing changed.

**Use when:** To check, on demand, whether AgentVision currently has something worth saying (a crash, a hang, new errors, frames waiting to be examined) without reading a full digest, or to preview what a Push Mode hook would inject for a given event (e.g. event='SessionStart' for the heartbeat path).

Calls GET /ambient, which assembles a state dict in-process from the same globals and helpers other routes use directly (the health scorer shared with /digest at bridge_server.py:2502/6269, the raw `_incidents` list also read by /incidents, `_latest_frame`, the visual engine's change score/OCR snippet, new-vs-standing error fingerprints from `_detect_failure_records`, frames-awaiting-examination from the retention ledger, and a capped tail of unread raw log bytes). It passes that state to `api/ambient.py`'s `build_signals()` to produce ranked {kind,tier,text,next} signals, then `decide()` picks one severity tier (silent/heartbeat/notice/alert), suppresses anything already surfaced to this `session_id` (unless `force=True`), rate-limits repeats per tier, and renders a byte-capped plain-text string (HEARTBEAT_CAP=340B, NOTICE_CAP=700B, ALERT_CAP=1200B) with an appended verbatim raw-log block (RAW_PUSH_CAP=3500B) that is never suppressed even when everything else is silent.

**Why not do it by hand:** Collapses a digest-read into a handful of pre-ranked sentences with an explicit next-call hint per signal, and deduplicates by fingerprint per session so the same fact is never paid for twice; because it forces in the raw log tail regardless of its own verdict, it also can't accidentally hide real program output behind a 'looks healthy' summary (the module's own comment cites a real incident where 180 GPU failures were invisible in the summary but present in this raw block).

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Not a diagnosis tool (use av_diagnose) and not a way to review a whole capture run (use av_visual_changes) — it is a one-glance 'anything urgent?' check. Silent-by-default plus per-session suppression means calling it twice in a row for the same session usually returns inject=false even while a real problem persists; use force=True to re-see it (byte caps still apply, and force never commits the raw-log read offset, so a forced preview cannot blind the next real injection).

> ⚠ **Caveat (from the code):** The MCP tool signature (claude_mcp.py:1836) only exposes session_id/event/force; it does NOT expose the route's stop_check/stop_hook_active parameters, so the Stop-hook backstop logic in ambient.py (stop_backstop(), STOP_BLOCK_ENABLED, MAX_STOP_BLOCKS) is unreachable through this MCP tool and only usable via a direct HTTP call to /ambient. Also, the assignment's extra backend ranges (/capabilities, /digest, /incidents, /latest, /start_here, /token_report, /visual_changes) are NOT called over HTTP by /ambient — the range for /digest and /incidents was worth reading because /ambient literally shares their in-process functions/globals (_health_block, _incidents), but /capabilities, /latest, /start_here, /token_report and /visual_changes turned out to be unrelated to av_ambient's actual code path (no call, no shared helper) and appear to be a mechanical/keyword false-positive in this assignment.

## `profiles` — 5 tool(s)

*Which program AgentVision is pointed at.*

### `av_list_profiles`

**Returns:** Full config dict for every configured program profile (built-ins + user-saved), keyed by profile name.

**Use when:** When first bridging a program, to see what profiles already exist and their exact field values (paths, log_sources, capture settings) before creating a new one with av_create_profile or switching with av_set_active_profile; also to sanity-check that a program's profile actually declares the log paths you expect it to.

Calls load_profiles() (python_backend/connectors/program_connector.py), which starts from the hardcoded BUILTIN_PROFILES (ships with only the neutral 'custom'), then overlays/adds any entries persisted in python_backend/profiles.json, and returns {name: ProgramProfile.to_dict()} for the merged set via a plain dataclasses.asdict() — every field verbatim: log_file, stats/screenshots/config folders, process_name, python_exe, capture_app/capture_crop, action_log_file, log_sources list, language, capture_user_input, notes, etc.

**Why not do it by hand:** Avoids hand-reading profiles.json and mentally merging it against the hardcoded built-in defaults; this returns the actual merged view (a saved profile of the same name overrides its built-in) in one call.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Does not indicate which profile is currently ACTIVE (use av_active_profile for that) and does not verify any listed path exists on disk or that the associated program is running — it is static configuration, not live state.

> ⚠ **Caveat (from the code):** Returns raw profile dicts with no redaction — absolute filesystem paths (project_root, python_exe, log_file, screenshots_folder, etc.) and free-text 'notes' are exposed verbatim to the caller.

### `av_active_profile`

**Returns:** Returns the currently active program profile's full config as JSON

**Use when:** Before issuing other av_* calls, to confirm which program/profile AgentVision is currently bridged to and where its logs/screenshots/state files are configured to live.

Calls GET /profiles/active, which loads all profiles from profiles.json, looks up the in-memory _active_profile_name, and returns that ProgramProfile's dataclass fields via asdict() (name, display_name, log_file, stats_folder, screenshots_folder, config_folder, state_file, project_root, process_name, python_exe, test_dir, notes, capture_app, capture_crop, action_log_file, log_sources, language, capture_user_input). If the active name isn't found in profiles.json it silently falls back to a blank default ProgramProfile() rather than erroring.

**Why not do it by hand:** Thin convenience over reading profiles.json by hand — it also resolves which profile is CURRENTLY active (state not visible in the file itself) and returns the resolved dataclass rather than raw JSON structure.

needs: `none`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Does not tell you whether capture is running or whether the program process is alive (use av_capture_status / av_program_status for that). If the active profile name points at a profile that no longer exists in profiles.json, it returns an empty/default profile with no error indicating the mismatch.

> ⚠ **Caveat (from the code):** The assignment's given backend range (bridge_server.py lines 2749-2768) is actually the PUT /profiles/active handler (set_active_profile), which SETS the active profile and is not exposed as an MCP tool at all. The real handler behind av_active_profile is the GET route immediately above it, at lines 2742-2746 (get_active_profile), which was not in the given range.

### `av_set_active_profile`

**Returns:** Switches the bridge's active profile; returns {ok, active, display_name} or a 404 error

**Use when:** When switching which program AgentVision reads/watches next, e.g. right after av_create_profile for a new target, or to flip back to a previously configured profile before starting a fresh capture session.

Sends HTTP PUT /profiles/active with {name} to bridge_server.py. The route (set_active_profile, bridge_server.py ~2749-2766) loads the merged profile dict (BUILTIN_PROFILES (only 'custom' ships) overlaid with anything saved in profiles.json), 404s if name isn't in it, otherwise sets the global _active_profile_name, discards the in-memory collector and rebuilds it via _get_collector() (which re-creates the ContextCollector against the new profile and pre-creates its output folder), and writes the name to active_profile.txt on disk via _persist_active_profile.

**Why not do it by hand:** Doing this through the API rather than hand-editing profiles.json also does two things a manual edit would miss: it persists the name to active_profile.txt so the out-of-process input daemon (input_daemon.py, PROFILE_REFRESH_S=2.0s poll) picks up the new recording target within ~2s, and it force-rebuilds the bridge's in-memory collector so subsequent frame/log/source reads immediately point at the new profile's paths instead of stale cached ones.

needs: `none`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Not for creating a profile first (use av_create_profile — this 404s on an unknown name). Does not stop or migrate any capture loop already running against the previous profile's files; it only redirects what NEW reads/writes target.

> ⚠ **Caveat (from the code):** The assignment note claimed this tool is 'NO route - pure local logic', but that is incorrect: av_set_active_profile performs a real HTTP PUT to bridge_server.py's /profiles/active route, which has substantive server-side behaviour (existence validation, global mutable state mutation, collector rebuild, disk persistence) — nothing about it is local-only.

### `av_create_profile`

**Returns:** Returns {ok:true, name} after writing/replacing a profile entry in profiles.json and creating its output folder

**Use when:** Registering a new program to bridge (or updating an existing profile's config) before the first av_capture_start/av_preflight, especially to declare multiple log_sources for a non-Python target.

POSTs {name, ...profile} to /profiles. The handler (bridge_server.py:2727-2739) requires a non-empty name, loads all profiles from profiles.json via load_profiles(), constructs ProgramProfile.from_dict(data) (connectors/program_connector.py:23-67, a dataclass that filters the incoming dict to known fields such as display_name, project_root, log_file, action_log_file, capture_app, capture_crop, process_name, capture_user_input, language, log_sources — all optional, no required-field or path-existence validation) and stores it under profiles[name], persists the whole dict back to profiles.json (save_profiles), then eagerly creates the profile's output folder (profile_output_folder + _base_for, which resolves to profile.project_root if set/exists else the global SAVE_FOLDER).

**Why not do it by hand:** Replaces hand-editing profiles.json and manually pre-creating the agentvision output directory; centralizes profile fields (log_sources list for multi-log watching) that the rest of the bridge (preflight, capture, log readers) consumes uniformly regardless of target language.

needs: `none`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Do not use to partially patch one field of an existing profile — updating profiles[name] = ProgramProfile.from_dict(data) FULLY REPLACES the stored object, so any field omitted from this call's `profile` dict silently resets to its dataclass default rather than being preserved from the prior profile. Also performs no validation that log_file/project_root paths actually exist.

> ⚠ **Caveat (from the code):** Overwrite-not-merge behavior is a real trap: calling av_create_profile again for the same `name` with only a subset of fields (e.g. just to add one log_sources entry) will drop every other previously-configured field (capture_app, capture_crop, language, etc.) back to defaults, since from_dict() has no knowledge of the previously-saved profile — callers must resend the full profile each time.

### `av_delete_profile`

**Returns:** Deletes a non-built-in profile from profiles.json; returns {ok: true}, or a 400/404 error

**Use when:** Cleaning up a stale custom profile created earlier with av_create_profile for a program that is no longer being debugged.

Sends HTTP DELETE /profiles/<name> (URL-quoted) to bridge_server.py. The route (delete_profile, bridge_server.py ~2769-2778) first rejects any hardcoded BUILTIN_PROFILES name (ships with only the neutral 'custom') with a 400, then loads the merged profile dict, 404s if the name isn't present, deletes that key, and calls save_profiles() which re-serializes the WHOLE merged dict (built-ins included) back to profiles.json.

**Why not do it by hand:** Thin convenience over hand-editing profiles.json to drop a key — its only real value-add is refusing to delete the three built-in profiles.

needs: `none`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game, library

**Not for:** Do not rely on it to protect the currently-active profile: despite the docstring's claim, deleting the active custom profile is NOT blocked (see code_note). Also not for removing built-ins — those always 400.

> ⚠ **Caveat (from the code):** FIXED (2026-07-30). Deleting the ACTIVE profile now returns HTTP 409 with deleted:false and the fix to apply (av_set_active_profile first); built-ins still 400; a missing profile still 404s. Pinned by test_tool_contracts.py.

## `bookmarks` — 3 tool(s)

*Saved moments and frame annotations.*

### `av_list_bookmarks`

**Returns:** Returns two independent failure lists: log-detected error/crash records and screen-detected freeze/blank/error moments

**Use when:** As a first-pass scan for known failure moments (including hangs that never wrote a log line) before drilling into av_error_moment(seq) or raw log/frame queries.

Re-scans up to the 500 most recent records in the same action_log_file JSONL and flags any whose category is error/exception/fatal, level is ERROR/FATAL/CRITICAL, data.name is run.fail/fatal/crash, or whose source contains 'fail'/'error'/'crash' (case-insensitive) — each gets a stable id (its timestamp string) and a content fingerprint (from data.error/stack_trace/stack/message/reason, else source|category|name) for later grouping. Separately, it returns the last 50 entries of the in-process `_visual_events` list, which the capture engine appends to on every frame via its own freeze/blank-screen/large-layout-change/on-screen-error-text detectors — this half has nothing to do with the log file and only exists if a capture session has actually run in this server process.

**Why not do it by hand:** The log-side half is a thin keyword/level scan over the same JSONL a human could grep. The real value is the visual half: a true UI hang or blank screen frequently emits NO log record at all, so this is the only tool surfacing that class of failure without the caller ever opening a frame image.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Not a full-history error search: log-side detection only looks at the 500 most recent JSONL records and uses a crude keyword/level heuristic (no exception-type awareness) — use av_errors_by_fingerprint for the full histogram. visual_bookmarks is capped to the last 50 in-memory events and is NOT reloaded from disk, so restarting the bridge process loses it even though frames may still be on disk. Also returns nothing log-side if the active profile has no action_log_file.

> ⚠ **Caveat (from the code):** Bookmark `id` is just the trigger record's raw `ts` string ('ts is unique enough' per the inline comment) — two failure records sharing the same timestamp string (millisecond-resolution ts, or a source that only logs whole seconds) would silently collide on id with no dedup check.

### `av_get_bookmark`

**Returns:** Returns action-log records in a fixed -30s/+10s window around a bookmark's timestamp, plus the nearest frame.

**Use when:** Right after av_list_bookmarks (or av_error_moment) surfaces a bookmark id and you need the actual surrounding log records plus the frame to pair with it -- i.e. to see what happened in the 30s leading up to a flagged crash/freeze/error and 10s after.

Parses the `bookmark_id` argument as an ISO-8601 timestamp into epoch ms via `_iso_to_ms` (aborts HTTP 400 if it does not parse); computes a fixed window from trigger-30000ms to trigger+10000ms; reads the active profile's structured action JSONL via `_read_action_jsonl` (up to 100000 records) for records whose `ts_ms`/`ts` falls inside that window; and separately scans the in-memory frame index (`_frames`) for the single frame whose `timestamp_ms` is numerically closest to the trigger, with no maximum-distance cutoff. Returns the window bounds, the matched action records (`actions`, `count`), and `closest_frame`/`closest_dt_ms` (both null if no frames have been captured yet).

**Why not do it by hand:** Replaces manually grepping the action JSONL near a timestamp and eyeballing which screenshot lines up -- one call returns both the pre-windowed structured records and the nearest frame's seq/dt so you can jump straight to av_get_frame/av_frame_json for that seq, instead of an unbounded query via av_search/av_log_range.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Not for open time-range exploration -- the window is hardcoded to -30000ms/+10000ms and is not adjustable via any parameter. Does not return frame pixels, only the closest seq/dt (call av_get_frame/av_frame_json next). Does not validate that `bookmark_id` corresponds to a real detected bookmark -- any string that parses as ISO-8601 is accepted and silently returns whatever falls in that window. `actions` is [] and `closest_frame`/`closest_dt_ms` are null (not an error) if no action log is configured or no frames have been captured yet.

> ⚠ **Caveat (from the code):** No validation that `bookmark_id` was ever produced by av_list_bookmarks/av_bookmark_outliers -- the route accepts any parseable ISO-8601 string, so a mistyped or guessed id fails silently (empty actions, null frame) instead of 404ing. `closest_frame` has no distance cutoff, so on a sparse capture it can return a frame minutes away from the trigger while still looking like a match -- callers must check `closest_dt_ms` rather than assume proximity.

### `av_frame_annotations`

**Returns:** Returns all notes previously left on one frame via av_frame_annotate

**Use when:** Resuming work on a frame you (or a prior session) previously annotated with av_frame_annotate, to recall breadcrumbs like 'root cause is ocr.py:432' before re-investigating the same failure.

GET /frame/<seq>/annotate. Resolves the frame via _frame_or_load (in-memory cache, else lazy-parsed from its on-disk JSON sidecar), aborting 404 if the frame or its json_sidecar path is missing. Derives the annotations file by taking the sidecar's stem, stripping a trailing '_frame', and appending '_annotations.json', then reads and returns that file's JSON verbatim (or {"annotations": []} if the file doesn't exist or fails to parse).

**Why not do it by hand:** Avoids manually locating and cat-ing the *_annotations.json sidecar next to a frame's JSON; also silently normalizes a missing/corrupt annotations file into an empty list instead of an error.

needs: `frames_on_disk`  
cost: `free`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Not for reading the frame's own content (pixels, detections, OCR) — use av_frame_json/av_frame_overlay/av_get_frame for that. Returns nothing useful if the frame was never annotated (empty list, not an error), and a corrupt annotations file is swallowed silently (returns empty list) rather than surfaced as an error.

> ⚠ **Caveat (from the code):** If the frame exists but has no json_sidecar, the route 404s outright rather than returning an empty annotation list — inconsistent with the graceful-empty behavior used when the annotations file itself is simply absent or unparseable.

## `wrap_up` — 1 tool(s)

*Summarise the session.*

### `av_session_report`

**Returns:** Returns one composed wrap-up JSON (plus a ready-to-save Markdown string) for an investigation

**Use when:** When done digging and ready to hand off or save a summary of the investigation -- after using av_diagnose/av_visual_changes/etc, not instead of them. Pass from_ms/to_ms to scope the timeline and frames_of_interest to a specific window.

Calls the existing /digest, /diagnose, and /timeline route functions in-process via _compose_view (a fresh Flask test_request_context per call) and reuses their output verbatim -- it computes nothing new. From the timeline it keeps only 'key moments' (bookmark rows; log rows with level WARN/ERROR/FATAL; frame rows whose textual summary line contains 'error'/'anomaly'/'stuck'), capped to the last 40. It separately computes frames_of_interest (frames carrying a structured error, a detected anomaly, a 'stuck' tag, or a blank-frame flag, capped to 10) and a capture/alignment snapshot, then renders the whole dict into an approx 4-8KB Markdown report via _session_report_markdown.

**Why not do it by hand:** Saves the agent from making 3 separate calls (digest, diagnose, timeline) and manually stitching/truncating them into a shareable write-up; the bundled Markdown is directly pastable. Honestly, it is composition and formatting only -- if digest/diagnose/timeline have little to say (no capture, no log sources), the report comes back sparse since _compose_view swallows any sub-call failure and returns {} rather than erroring.

needs: `none`  
cost: `low`  
languages: `any`  
program kinds: gui, headless, cli, service, game

**Not for:** Not for live/real-time monitoring -- it's a point-in-time snapshot that internally re-runs digest+diagnose+timeline(limit=400) every call, so it costs more than a plain field read. Not a source of new detection: any hypothesis or signal here already existed in av_diagnose's output. Also note from_ms/to_ms only scope the timeline and frames_of_interest sections -- health/top_signals/hypotheses/top_errors come from digest/diagnose called with no window and reflect current/whole-session state regardless of the window passed in.

> ⚠ **Caveat (from the code):** The 'key moments' frame-kind filter and frames_of_interest use two different, independently-maintained heuristics for what counts as a notable frame: key_moments does a lowercase substring match on the timeline row's rendered 'line' text for 'error'/'anomaly'/'stuck', while frames_of_interest inspects the frame's structured error/anomaly/tags/black_frame fields directly. These can disagree (e.g. a frame with a structured anomaly but a summary line that doesn't literally say 'anomaly' would appear in frames_of_interest but not key_moments, or vice versa for coincidental text).
