# MCP tool audit — 94 tools read against their own implementations

Produced by 45 parallel agents, each reading a tool body, its Flask route and
one level of helpers. Descriptions were derived from the HANDLER, not the
docstring; where the two disagreed the handler won and the disagreement is
recorded below. Every entry is in `python_backend/api/tool_meta.json`.

- tools documented: 90/90  (av_observer_log added 2026-07-30)
- tools with a recorded defect or caveat: 78  (av_observer_log carries one too)

## THIS FILE IS A WORK QUEUE — status as of 2026-07-30

Class A was 12 items. **All 12 are now closed**: each entry below carries a
`FIXED` line naming the check that proves it. The proofs live in

- `python_backend/api/test_tool_contracts.py` — one check per Class-A item
- `python_backend/api/test_observer_isolation.py` — AgentVision must not write
  into the log it is reading
- `python_backend/connectors/test_process_identity.py` — "is the program
  running?" must not be answered by a bystander process
- `python_backend/api/test_frame_namespace.py` — a frame seq identifies a frame
  only within a profile

`tool_meta.json`'s `code_note` fields were updated in the same pass, because that
file — not this one — is what the bridge serves to an agent at runtime. A stale
caveat there is worse than none: it is read as current.

Two Class-A fixes are deliberately partial, and the reason is recorded in place:
`av_log_push` (Class A) was fixed by changing WHERE it writes rather than making
its old claim true, and `av_wait_for`'s condition "inference" is still a default —
it is now merely honest about being one, because there is genuinely only one thing
to infer.

Class B is not closed and is not claimed to be. Several of its entries are
observations about cost or ambiguity rather than defects.

## Class A — the tool misleads its caller (fix first)

These are not style nits. Each one makes a reader believe something untrue,
which is worse than a missing tool because the tool is trusted.

### `av_selftest`
FALSE GREEN: the input_daemon check hardcodes ok:True, so a dead daemon never appears in failed_checks and never flips overall ok to false.

**FIXED 2026-07-30** — `ok` is now derived: `running or not required_by_profile`, both measured. An unwanted-and-absent daemon reports ok with a note; a wanted-but-dead one reports ok=False and appears in failed_checks. Check: test_tool_contracts.py "the daemon check can actually fail".

### `av_new_errors_this_session`
BROKEN DEDUPE: _save_fp_history() is defined but never called anywhere, so fingerprint history is never persisted — the endpoint just returns everything since session start, with no intra-response dedupe either.

**FIXED 2026-07-30** — `_save_fp_history` is called, so history persists and a later session stops calling those fingerprints new. Within a session the filter set is a boot snapshot, so the answer is idempotent. Fingerprints are deduped with count/first_ts/last_ts (41 records of 2 kinds → 2 entries). Check: test_tool_contracts.py "dedupe + persistence".

### `av_overview`
POISONED METRIC: fetches /latest purely to read .sequence, which increments the full_frame read counter that av_token_report uses to prove token savings — inflating the saving it claims.

**FIXED 2026-07-30** — uses `/latest/pointer`, which returns seq/ts/summary and does not touch the read counter. It also stopped calling `_mark_seen`, which the old path did: orientation was marking frames EXAMINED, letting retention delete a frame the agent never saw. Check: test_tool_contracts.py "a pointer lookup is not a frame read".

### `av_log_push`
DOCSTRING FALSE: claims pushed records show up in av_log_range/av_actions_around_frame. The handler writes plain text to activity.log; those readers read actions.jsonl. category/source/data are silently discarded.

**FIXED 2026-07-30, by changing the behaviour rather than the promise** — the record is structured and goes to AgentVision's own observer log (`log/observer/<profile>.observer.jsonl`, read with `av_observer_log`). It is NOT written into the program's actions.jsonl on purpose: see test_observer_isolation.py. Check: test_tool_contracts.py "the note is structured, and not in the program's log".

### `av_delete_profile`
DOCSTRING FALSE + DATA LOSS: docstring says it cannot remove the active profile; the handler only guards builtins, so deleting the active custom profile succeeds and later lookups fall back to a blank default.

**FIXED 2026-07-30** — deleting the active profile returns 409 with `deleted:false` and the fix to apply. Check: test_tool_contracts.py "active-profile guard".

### `av_ui_tree`
GLOBAL LEAK: max_nodes mutates the module-level utils.ui_tree.MAX_NODES instead of per-request, is not thread-safe, and never resets — one custom call silently changes the default for every later call.

**FIXED 2026-07-30** — per-request override under a lock, restored in a finally; the response reports `max_nodes_applied` and that the process default is unchanged. Check: test_tool_contracts.py "max_nodes does not leak into later calls".

### `av_program_stats`
MISLEADING SCOPE: documented as numeric metrics/counters/gauges; actually a hardcoded 9-key game-bot leveling whitelist that silently drops everything else. `lines` is a dead param.

**FIXED 2026-07-30** — every `key: value` pair is returned (the nine bot keys stay as aliases), table borders are stripped rather than used to discard rows, and `lines` reaches the reader. Check: test_tool_contracts.py "every metric, and `lines` does something".

### `av_install_project`
SILENT NO-OP: the MCP wrapper never sends force, and the route only bypasses the seal check when force=true — so on an unsealed profile the tool returns installed:false instead of installing, despite an unconditional-sounding docstring.

**FIXED 2026-07-30** — the tool takes and forwards `force`, and the docstring states that the gate answers HTTP 200 so callers must read `installed`. Check: test_tool_contracts.py "the gate is reachable from the tool".

### `av_timeline`
DATA LOSS: events with no parseable ts_ms are coerced to 0.0 and sort to the FRONT as oldest, then rows[-limit:] drops them first — contradicting read_normalized(), which deliberately sorts untimestamped events to the end so they are not lost.

**FIXED 2026-07-30** — untimestamped rows keep `ts_ms: null`, are marked, and are truncated SEPARATELY with a reserved share. Sorting them last alone would have inverted the bug (they would evict every real row); both directions are now covered. Check: test_tool_contracts.py "untimestamped rows are not silently dropped".

### `av_errors_by_fingerprint`
FALSE POSITIVES: _detect_failure_records substring-matches 'fail'/'error'/'crash' in `source` with no word boundary, so a module named FailoverManager or CrashReporterService pollutes the histogram.

**FIXED 2026-07-30** — word-boundary match on `source`, and the source heuristic is skipped when the record carries an explicitly benign level. Check: test_tool_contracts.py "no false positives from module names".

### `av_source_digest`
STALE FOREVER: source_index.json is rebuilt only when entirely missing — never diffed against mtimes — so edits are invisible until an explicit av_source_refresh. (Flagged independently by 3 agents.)

**FIXED 2026-07-30** — the index is compared against file mtimes (stat-only walk, 5 s cache) and rebuilt when the project changed, reporting `rebuilt_because`. Verified by adding a file and seeing the next digest include it with no explicit refresh.

### `av_wait_for`
DEAD BRANCH: condition inference reads `'log' if (...) else 'log'` — both arms identical. The 'anomaly' condition also takes no call-time baseline, so a pre-existing anomaly reports as freshly matched.

**FIXED 2026-07-30** — the identical-arm ternary is gone and the default is reported as a default (`condition_inferred`/`condition_basis`) rather than dressed as a deduction; `anomaly` now takes a t0 baseline so a pre-existing anomaly is not reported as freshly matched. Check: test_tool_contracts.py "a default is reported as a default".

## Class B — remaining caveats, verbatim from the audit

Mostly silent truncation, ambiguous empty results, and redundant work.
Kept in full because 'returned nothing' vs 'is misconfigured' being
indistinguishable is a recurring theme worth fixing as a class.

- **`av_actions_around_frame`** — Same inaccuracy as av_get_frame: the assignment said this tool has no backend route, but it hits a real Flask route, GET /frame/<seq>/actions at bridge_server.py:1735-1755. Separately, the handler hardcodes limit=10000 in its _read_action_jsonl call regardless of window_secs, and window_secs itself has no upper bound validated server-side -- a very large window against a huge action log will read the whole file into memory before filtering.
- **`av_active_profile`** — The assignment's given backend range (bridge_server.py lines 2749-2768) is actually the PUT /profiles/active handler (set_active_profile), which SETS the active profile and is not exposed as an MCP tool at all. The real handler behind av_active_profile is the GET route immediately above it, at lines 2742-2746 (get_active_profile), which was not in the given range.
- **`av_add_adapter`** — The bridge_server docstring at lines 3201-3216 is accurate and matches user_adapters.add_adapter exactly — route is a thin, faithful wrapper with no extra behavior. One subtlety not obvious from the MCP tool docstring: registration order matters — register_from_spec() without outrank inserts 'just above the tail' (after all built-ins), so a brand-new adapter can only ever win against fallback adapters (structural/generic_ts/raw) or ties broken by outrank, never against another named built-in with a strictly higher score, by design.
- **`av_ambient`** — The MCP tool signature (claude_mcp.py:1836) only exposes session_id/event/force; it does NOT expose the route's stop_check/stop_hook_active parameters, so the Stop-hook backstop logic in ambient.py (stop_backstop(), STOP_BLOCK_ENABLED, MAX_STOP_BLOCKS) is unreachable through this MCP tool and only usable via a direct HTTP call to /ambient. Also, the assignment's extra backend ranges (/capabilities, /digest, /incidents, /latest, /start_here, /token_report, /visual_changes) are NOT called over HTTP by /ambient — the range for /digest and /incidents was worth reading because /ambient literally shares their in-process functions/globals (_health_block, _incidents), but /capabilities, /latest, /start_here, /token_report and /visual_changes turned out to be unrelated to av_ambient's actual code path (no call, no shared helper) and appear to be a mechanical/keyword false-positive in this assignment.
- **`av_baseline`** — Assignment's range (5301-5327) matched the real handler exactly; route accepts both POST and GET on the same path, not just POST as the docstring's phrasing implies.
- **`av_bookmark_outliers`** — The assignment note for this tool said 'NO route - pure local logic,' but that's wrong: claude_mcp.py:563 does call _http_get to a real Flask route, GET /bookmark/<bid>/outliers (bridge_server.py:2177-2235), which does non-trivial server-side work (two JSONL scans + per-field z-score calc). Separately, a real design gotcha: the 'baseline' is NOT 'normal behavior before things went wrong' — it's just the most-recent 20000 records in the active log file at call time (_read_action_jsonl with no window), so if the program has been failing/recovering repeatedly, the baseline itself can include failure-adjacent or post-failure records, diluting the very anomaly the tool is trying to surface.
- **`av_bridge_catalog`** — The catalog body reports LIVE counts (adapters from len(la.REGISTRY) and la.builtin_names(), source_readers from log_sources.list_readers(), mcp_tool_groups from _tool_catalog_groups()), so what this call returns is always current. Prose counts in docstrings and .md files are NOT sourced from this call and have drifted badly: a sweep on 2026-08-01 found thirteen false statements across the tree (ARCHITECTURE.md said 23 adapters and 44 tools against a live 658 and 90; this very entry used to quote a bridge_plan.py docstring reading '650+ log adapters, ~86 MCP tools', wording that no longer exists). All corrected, and api/test_doc_counts.py now fails the suite on any prose count that disagrees with the registry. When a number matters, take it from this response or av_capabilities().
- **`av_bridge_commit`** — The av_bridge_commit docstring in claude_mcp.py (lines 1519-1551) documents the plan shape as {catalog_token, emitters, adapters, capture, visual_capture, rationale} and never mentions a 'why' field. But bridge_plan.validate_plan() (bridge_plan.py lines 433-454) hard-requires plan.why = {emitter_id: reason} for every non-empty emitters list, rejecting the commit otherwise. An agent following only the av_bridge_commit docstring would omit 'why' and get rejected with an error the docstring gave no warning about.
- **`av_bridge_status`** — bridge_plan.status() sets plan = read_plan(folder), which is None whenever there is no .av_bridge_plan.json on disk — this is true even when sealed=True via the legacy marker. A caller that assumes plan.emitters exists whenever state=="BUILT" will get nothing in the legacy case; the human-readable 'note' field mentions this ('call av_bridge_commit(replan=True)...') but the plan field itself gives no structured signal beyond being null.
- **`av_capture_set_interval`** — Assignment brief for this tool claimed 'NO route - pure local logic', but that is incorrect: claude_mcp.py:784 calls _http_put('/capture/interval', ...) and bridge_server.py:3333 defines a real Flask route for it (capture_set_interval). The tool is not pure local logic; it's a normal HTTP-backed MCP tool like the others. Documented above from the actual route/handler.
- **`av_capture_start`** — Both the bridge_sealed gate and the preflight gate return HTTP 200 with started:false rather than a 4xx — a caller that only checks status code will misread a blocked start as success; must check the `started`/`bridge_required`/`preflight_required` fields. Also, calling this with an unmet preflight gate computes but does NOT persist the preflight marker (that only happens via the dedicated /preflight endpoint or after a real start) — so retrying the identical call without force repeats the same guidance rather than remembering it was already shown.
- **`av_codebase_map`** — The /codebase-map route accepts a `root` query-string override, but the av_codebase_map() MCP wrapper takes no arguments and never forwards one, so from the agent's side the root is always the active profile's project_root. Also, if project_root doesn't exist on disk, build_map() does not error: os.walk() on a missing path silently yields nothing, so the tool returns a valid-looking response with total_files=0, total_lines=0, empty tree/edges -- there is no explicit 'root not found' signal.
- **`av_create_profile`** — Overwrite-not-merge behavior is a real trap: calling av_create_profile again for the same `name` with only a subset of fields (e.g. just to add one log_sources entry) will drop every other previously-configured field (capture_app, capture_crop, language, etc.) back to defaults, since from_dict() has no knowledge of the previously-saved profile — callers must resend the full profile each time.
- **`av_debug_log`** — debug_log_tail()'s except Exception: pass swallows all read failures and returns lines: [] with no error signal, which could mislead an agent into thinking AgentVision has logged nothing rather than that the log file could not be read.
- **`av_diagnose`** — Degrades gracefully rather than erroring: with zero frames (_latest_frame is None) and an empty action log, it still returns 200 with hypotheses=[] and top_signals=['no strong failure signals - program looks healthy'] -- indistinguishable from a genuinely healthy program, so an agent should cross-check av_program_status/av_capture_status before trusting a clean diagnose result on a freshly-bridged program.
- **`av_diff`** — The 30-key truncation cap on changed/added/removed applies per-category with no way to page past it or see which keys were dropped — a maintainer relying on this for a large state diff (e.g. across a long a..b range) could silently miss the actual changed field.
- **`av_digest`** — The 3000-record cap in _detect_failure_records(limit=3000) means top_errors counts and 'total_failures' silently reflect only the newest slice of a long JSONL file, not the true lifetime count, even though nothing in the response indicates truncation occurred.
- **`av_error_moment`** — Assignment's backend range (5911-6316) overshoots the actual handler, which ends at line 6141 (return jsonify(bundle), 200); lines 6144-6316 are unrelated ambient-state helpers (_ambient_state/_ambient_state_uncached) for a different route, not part of error_moment.
- **`av_events_schema`** — _read_action_jsonl reads and JSON-parses the ENTIRE log file on every call (os.path.getsize + f.read(size) with no streaming/early-exit) before slicing to the newest `limit` records -- cost scales with total log file size, not with the 2000-record output, so this can get slow/memory-heavy on long-running programs with large JSONL logs.
- **`av_examine_ack`** — Non-numeric entries in `seqs` (e.g. "abc") are silently filtered out before `requested` is computed, so a malformed seq value never surfaces as an error or a count mismatch in the response — it just quietly vanishes from the request.
- **`av_frame_alignment`** — The assignment brief for this tool stated 'NO route - pure local logic', which is incorrect: av_frame_alignment hits a real backend route at bridge_server.py:1874 (same discrepancy already flagged for av_get_frame/av_actions_around_frame in other batches). Separately, the response gives no way to distinguish 'verified clean' from 'nothing existed to check' — both cases return aligned=True with leaked_after_shutter=0; only records_in_context==0 tells them apart, and nothing in the payload calls that out.
- **`av_frame_annotate`** — Assignment note claimed this tool has 'NO route - pure local logic', but it is a real POST to app.route('/frame/<int:seq>/annotate', methods=['POST','GET']) at bridge_server.py:2662-2706 that performs a read-modify-write of a JSON file on disk. The same route also backs the separate av_frame_annotations tool (GET) defined a few lines below this one in claude_mcp.py.
- **`av_frame_annotations`** — If the frame exists but has no json_sidecar, the route 404s outright rather than returning an empty annotation list — inconsistent with the graceful-empty behavior used when the annotations file itself is simply absent or unparseable.
- **`av_frame_json`** — Assignment metadata said this tool has 'NO route - pure local logic', but that's incorrect: it calls _http_get(f"/frame/{seq}/json", ...) which is served by a real Flask handler at bridge_server.py:5683 (frame_json). Also, the handler supports a content_map=1 query arg (entropy-quadtree dense-region map) that the MCP tool signature never exposes, so an agent can't reach it through av_frame_json as written.
- **`av_frame_overlay`** — Assignment note claimed this tool has 'NO route - pure local logic', but that is incorrect: the MCP tool body is a one-line _http_get to a real Flask route, app.route('/frame/<int:seq>/overlay') at bridge_server.py:2607-2659, which does nontrivial JSONL matching. Worth flagging in case other tools' route info was derived the same (wrong) way.
- **`av_frame_region`** — Same assignment-metadata error as av_frame_json: this is not 'pure local logic', it calls _http_get(f"/frame/{seq}/region", ...) which is served by the real handler at bridge_server.py:5810 (frame_region).
- **`av_frames_awaiting`** — The route calls _ret.LEDGER.awaiting() twice per request (once for the limited `rows`, once with limit=100000 just to get `total`), re-scanning and re-sorting the whole ledger both times — harmless at expected ledger sizes but a redundant O(n log n) pass.
- **`av_get_bookmark`** — No validation that `bookmark_id` was ever produced by av_list_bookmarks/av_bookmark_outliers -- the route accepts any parseable ISO-8601 string, so a mistyped or guessed id fails silently (empty actions, null frame) instead of 404ing. `closest_frame` has no distance cutoff, so on a sparse capture it can return a frame minutes away from the trigger while still looking like a match -- callers must check `closest_dt_ms` rather than assume proximity.
- **`av_get_frame`** — The assignment brief for this tool said 'NO route - pure local logic', but that is inaccurate: av_get_frame is a thin wrapper over a real Flask route, GET /frame/<seq> at bridge_server.py:1635-1645, which does nontrivial work (lazy sidecar hydration + _augment_frame_for_ai decoration) rather than being pure local logic in claude_mcp.py.
- **`av_incidents`** — The response key `rolling_window_seconds` is explicitly flagged in bridge_server.py as kept only for backward compatibility -- it is the width of the window an incident FREEZES, not a deletion/eviction rule. Actual eviction is byte-budget + examine-before-delete (av_retention). A reader taking the key name at face value would misread it as a rolling-delete window.
- **`av_install_verify`** — The claude_mcp.py docstring undersells the route: it advertises the return shape as {verified, mode, language, events_seen, last_event, sink, stderr} and describes only the single env-injected probe, but the actual handler (bridge_server.py ~3600-3766) also runs a SECOND bare-env probe and returns additional fields not mentioned in the docstring: emitter_works, autoloads, autoload_detail, launch_command, before_size. Critically, `verified` is defined as `emitter_works and autoloads` -- i.e. it can be False even when the emitter demonstrably works, if it only works with the env injected. An agent reading only the docstring would not expect that.
- **`av_latest_frame`** — The MCP docstring's claim that _ai gives 'the paired _frame.json' matches json_sidecar in the augment code. No discrepancy found between docstring and handler; _augment_frame_for_ai (bridge_server.py:1552-1617) is shared verbatim with /frame/<seq>, so behavior here is consistent with the by-sequence frame route.
- **`av_list_adapters`** — The docstring's family examples ('kernel', 'security', 'network', 'java', 'go') only work because those live in their own file under connectors/adapters/; the ~44 adapters defined directly in connectors/log_adapters.py (JsonLinesAdapter, SyslogAdapter, Log4jAdapter, etc.) all report family='log_adapters' regardless of what language/format they actually parse, since family is derived from the Python module path, not a declared taxonomy. That's not a bug, but it means the 'families' histogram has one oversized bucket that isn't a meaningful category by itself.
- **`av_list_bookmarks`** — Bookmark `id` is just the trigger record's raw `ts` string ('ts is unique enough' per the inline comment) — two failure records sharing the same timestamp string (millisecond-resolution ts, or a source that only logs whole seconds) would silently collide on id with no dedup check.
- **`av_list_profiles`** — Returns raw profile dicts with no redaction — absolute filesystem paths (project_root, python_exe, log_file, screenshots_folder, etc.) and free-text 'notes' are exposed verbatim to the caller.
- **`av_log_entities`** — log_entities_route's comment '# whole retained tail' is misleading — read_raw_delta enforces max_bytes_per_source=262_144 (256 KiB) by default and this route never overrides it, so the index is really built over each source's newest ~256KB only, not its full retained history. Worth fixing the comment or exposing the truncated_head/lines_total fields (already returned per-source under 'sources') more prominently so an agent knows when it's looking at a partial window.
- **`av_log_normalized`** — The `limit` query param has no server-side upper bound (_int_arg only casts to int, never clamps), so it is not truly a hard cap on response size the way the docstring implies — the real ceiling on how much comes back is each source's fixed 1 MiB tail read, which the docstring never mentions at all.
- **`av_log_range`** — Filter asymmetry: `category` must match exactly and case-sensitively (rec.get('category') != category), while `source` is a case-sensitive substring test — a caller passing category='Error' will get zero hits against a log written with category='error', with no indication why. Contrast with _detect_failure_records (used by av_list_bookmarks/av_errors_by_fingerprint), which lowercases category/source before comparing.
- **`av_log_raw`** — The /log/raw route parses a `collapse` query flag (default on) and correctly forwards it to log_sources.read_raw_delta() for the all=1 and from_offset code paths (bridge_server.py ~6505-6510), but the default path (no all, no from_offset) calls _raw_log_delta(sid, advance=not peek) at line 6512, which never passes collapse through at all -- collapsing is always on in that branch regardless of the collapse=0 query param. This is moot for the av_log_raw MCP tool specifically, since claude_mcp.py never exposes a `collapse` parameter on the tool itself (only session_id, all, from_offset, cap_bytes, peek), so an agent has no way to request uncollapsed output at all via this tool.
- **`av_log_where`** — Calling this 'read-only' GET route triggers _write_targets_report() -> _get_collector(), which LAZILY CONSTRUCTS the global ContextCollector for the active profile if one doesn't exist yet (and logs 'Bridge started - connected to: ...' as a side effect). So the very first call to av_log_where in a session can initialize global collector state before any capture has been started — a diagnostic call is not fully side-effect-free.
- **`av_metrics`** — When zero frames exist yet (or none carry a 'perf' block), the endpoint still returns HTTP 200 with all-null/zero series and no hint field — unlike the related /state_diff and /wide endpoints, which return an explicit {match: None, hint: ...} when their data source is empty. A caller can't distinguish 'no data yet' from 'genuinely flat metrics' without also checking window_frames == 0.
- **`av_ocr_frame`** — Assignment metadata said this tool has 'NO route - pure local logic', but that is wrong: the claude_mcp.py wrapper does call _http_get(f"/frame/{seq}/ocr") which hits a real Flask route at bridge_server.py:5089-5103. Also worth flagging: `annotated_image` is a documented historical misnomer (see comment at bridge_server.py:768-772) — it actually points at the original unannotated screenshot PNG, not a frame with overlays burned in.
- **`av_preflight`** — The Flask route's GET branch only forwards project_root/language from query args — sample_lines, log_paths, and accept_gaps are silently dropped on GET (POST-only). This doesn't affect the av_preflight MCP tool itself since claude_mcp.py always calls _http_post, but any other GET caller of /preflight loses those params without an error.
- **`av_program_log`** — Bypasses the newer generic `log_sources[]` system entirely -- reads only the legacy single `log_file` string field on ProgramProfile (program_connector.py), distinct from `log_sources` and `action_log_file`. A profile declared purely through log_sources (no legacy log_file set) makes this tool return an empty list even though av_log_sources/av_log_normalized see real data for that same program. The 64KB-tail cap is a comment-documented implementation detail, not surfaced in the JSON response, so a caller cannot tell from the response alone whether `lines` was fully satisfied or silently truncated by the byte cap.
- **`av_program_status`** — is_running() and process_cpu_ram() each run their own full psutil.process_iter() scan and re-apply the identical _process_matches logic, i.e. the target process is located twice per call to this one route, and process_cpu_ram()'s cpu_percent(interval=0.1) blocks ~100ms. Functionally correct but redundant work; could be collapsed into one scan returning (running, cpu, ram) together.
- **`av_replay`** — In replay_route the per-step log fetch is wrapped in a bare `except Exception: logs = []`, so a broken/misconfigured log source and a genuinely empty window are indistinguishable in the response -- an agent has no signal that log correlation failed rather than found nothing.
- **`av_retention`** — The MCP tool wraps only the GET behavior; the same /retention route also supports POST (mode/budget/hold_seconds reconfiguration) but that path is not reachable through av_retention() since the tool takes no arguments and always calls _http_get.
- **`av_run_tests`** — Capability gap: the av_run_tests MCP tool signature takes no arguments at all, so callers cannot pick a project_root or raise the 60s timeout, even though bridge_server's /run-tests route already reads project_root from the POST body and failure_explainer.run_tests() already accepts both project_root and timeout parameters — the plumbing exists but is not exposed through the tool.
- **`av_search`** — Two issues: (1) the `scanned` counter (bridge_server.py ~4247/4256) is incremented for every event considered but never placed in the JSON response, so that diagnostic is computed and thrown away. (2) `matches.sort(key=lambda m: m.get('ts_ms') or 0.0)` treats any match with no timestamp as if it occurred at epoch 0, sorting it to the very front of the results as the 'oldest' hit rather than flagging it as unstamped — the same ts_ms-coercion pattern seen in /timeline.
- **`av_session_report`** — The 'key moments' frame-kind filter and frames_of_interest use two different, independently-maintained heuristics for what counts as a notable frame: key_moments does a lowercase substring match on the timeline row's rendered 'line' text for 'error'/'anomaly'/'stuck', while frames_of_interest inspects the frame's structured error/anomaly/tags/black_frame fields directly. These can disagree (e.g. a frame with a structured anomaly but a summary line that doesn't literally say 'anomaly' would appear in frames_of_interest but not key_moments, or vice versa for coincidental text).
- **`av_set_active_profile`** — The assignment note claimed this tool is 'NO route - pure local logic', but that is incorrect: av_set_active_profile performs a real HTTP PUT to bridge_server.py's /profiles/active route, which has substantive server-side behaviour (existence validation, global mutable state mutation, collector rebuild, disk persistence) — nothing about it is local-only.
- **`av_source_at_error`** — The two line ranges in the assignment both matched the correct handler (GET /source_at_error) with no drift.
- **`av_source_file`** — bridge_server.py:2965-2997 has no server-side size cap, unlike source_mirror's digest path (DIGEST_MAX_LINES=5000, MAX_FILE_BYTES=2MB) — a caller that omits to_line on a large generated file will get the entire file back in one JSON response. Path-traversal handling itself is solid: it blocks literal '..' path segments AND re-validates target.relative_to(root.resolve()), so symlink/relative tricks are still caught by the second check.
- **`av_source_light`** — The claude_mcp.py docstring says every file gets 'a 1-line summary', but build_light_digest() in source_mirror.py only populates 'summary' for lang=='python' or 'markdown' — all other languages (js/ts/rust/go/java/rb/lua/sh/html/css/json/yaml/toml/ini/sql/other) silently get no summary field. This is a real docstring/behavior mismatch for non-Python projects.
- **`av_source_list`** — No pagination or filtering by lang/path is exposed, unlike av_source_search's limit param — on a project with thousands of indexed files this returns one large unbounded JSON array.
- **`av_source_refresh`** — In source_mirror._file_entry, the `if st.st_size > MAX_FILE_BYTES: pass` branch (line ~168) is dead code — the comment claims it affects 'symbol extraction downstream' but the branch body is a no-op; the actual size gate that skips digesting is a separate, independent check in file_digest() (line ~354). Cosmetic/misleading, not a functional bug.
- **`av_source_search`** — The size-skip only applies to lang=='other' files over 5000 lines; any file with a recognized extension (python/js/ts/rust/go/java/json/yaml/...) is read into memory and scanned in full regardless of size, unlike file_digest() elsewhere in source_mirror.py which enforces MAX_FILE_BYTES (2MB) and DIGEST_MAX_LINES (5000). A huge generated file with a recognized suffix (e.g. a multi-MB .json or .py data file) would be fully loaded and line-scanned on every matching search.
- **`av_source_tree`** — Shares source_index.json with av_source_light/av_source_digest/av_source_refresh — if that index is stale (project files changed on disk after the last build), the tree silently reflects the old layout until POST /source/refresh is called; there is no mtime-based auto-invalidation, only lazy build-if-absent.
- **`av_start_here`** — The assignment's backend range (bridge_server.py 7606-7774) overshoots the handler: the /start_here route itself ends at line 7698 (return jsonify(...), 200); lines 7701-7774 are the '__main__' CLI entry point (argparse, profile bootstrap, app.run), unrelated to this route.
- **`av_state_at`** — A missing/unconfigured action_log_file and a configured-but-empty-of-wide-records log produce the identical response shape (match: null, generic hint) — there's no way to tell from the response alone whether the feature is simply unused or the profile is misconfigured.
- **`av_state_diff`** — _read_action_jsonl reads the WHOLE action-log file but then returns only the last 20000 matching records (no category pre-filter is passed here, so the cap applies across ALL record categories, not just 'wide'). In a chatty log where wide snapshots are sparse relative to other events, the 20000-record tail cap can silently exclude genuinely-nearer old wide records for an early a_ms/b_ms, and the tool has no way to signal that a truncation happened.
- **`av_status`** — _get_collector() has a lazy-init side effect: the very first call to av_status (before any capture/start) will instantiate a ContextCollector and create the profile's output folder on disk, even though the caller only asked for status. If _active_profile_name isn't in the loaded profiles, it silently falls back to the 'custom' profile (or a bare default ProgramProfile()) rather than erroring.
- **`av_test_adapter`** — Docstring for the route says 'query: line=<raw log line>' consistent with claude_mcp.py wrapper; no discrepancy found. Note that if `line` is empty the route 400s before ever touching the detector, which the MCP tool signature (line: str, no default meaning it's required) matches correctly.
- **`av_trace_timeline`** — Assignment doc for this tool stated 'NO route - this tool is pure local logic', but the source shows it is a normal _http_get call to a real, non-trivial Flask route (bridge_server.py:2035 trace_timeline). Flagging in case that stale assumption affected other batches' tools that were assumed route-less by the same heuristic.
- **`av_watches`** — For kind='log', the 1000-event cap in log_sources.read_normalized is applied to ALL window-matching events before this route's regex/category/source filter runs, not after — so a watch's true hits can be crowded out and silently lost under high log volume even though only the unrelated events exceeded the limit. Worth surfacing to a maintainer as a real precision gap, distinct from the documented/intentional 25-hit display cap (_WATCH_HIT_CAP).

## Cross-cutting patterns

1. **Silent-empty ambiguity.** Many tools return `{}`/`[]` identically for
   'nothing happened' and 'this profile is misconfigured'. A `hint` field
   exists on some routes (`/wide`, `/state_diff`) and not others.
2. **Undisclosed caps.** 256 KiB, 1 MiB, 2000/3000/10000/20000-record and
   30-key truncations, mostly unreported in the response.
3. **Route capability unreachable from the tool.** `content_map` on
   `/frame/<seq>/json`, POST on `/retention`, `project_root`/`timeout` on
   `/run_tests`, `root` on `/codebase-map`, stop-hook params on `/ambient`.
4. **Docstrings written from intent, not behaviour.** The largest single
   source of Class A findings.
