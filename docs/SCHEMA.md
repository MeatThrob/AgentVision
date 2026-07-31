# AgentVision JSON Schema (for AI consumers)

AgentVision is an **AI-facing** debugger: everything a model reads is JSON, on a
single UTC-ms timeline, with a stable, versioned shape. This document is the
contract. Every payload carries **`schema_version`** (currently `2.0.0`) so a
consumer can adapt if the shape evolves (MINOR = additive, MAJOR = breaking).

There are three JSON surfaces:

1. **The frame** — one `_frame.json` per screenshot (`shared/schema/snapshot_schema.py`).
2. **The unified event** — one object per normalized log line (`docs/LOG_ADAPTERS.md`).
3. **The digest** — a compact triage summary (`av_digest`).

---

## 1. The frame (`SnapshotFrame`)

Read the **AI triage layer** first (`summary`, `recommended_next`, `tags`,
`confidence`), then drill into `error` / `anomaly` / `program`. Always read the
**image** at `capture_meta`/`annotated_image` together with the JSON.

### Top level

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | str | Schema version, e.g. `"2.0.0"`. |
| `frame_id` | str | Short unique id for this frame. |
| `sequence` | int | Monotonic frame number (use with `av_get_frame`). |
| `timestamp` / `timestamp_ms` | str / float | Shutter instant (UTC ISO-Z / epoch ms). |
| **`summary`** | str | One-line NL description of this moment. |
| **`recommended_next`** | str | The single best next action for the AI. |
| **`tags`** | list[str] | Fast filters: `error`, `recurring`, `stuck`, `anomaly`, `healthy`, `not_running`, `state_changed`. |
| **`confidence`** | float | 0 (broken) … 1 (healthy). Mirrors `agent_eval.confidence`. |
| **`state_delta`** | obj | **Nested** key-level change vs previous frame (flattened dotted keys, e.g. `state.player.hp`): `{added,removed,changed,*_count,truncated}`. Diffs both numeric `stats` AND the program's `state.json`. |
| **`correlation`** | obj | `{trace_ids[], run_ids[]}` seen in this frame — pull the full story with `av_trace_timeline`. |
| **`perf`** | obj | Target-process resource metrics at capture (psutil): `{found, pid, cpu_percent, rss_mb, ram_gb, num_threads, num_fds?, status}`. |
| `image_file` / `json_file` / `diff_file` | str | Sibling files for this frame. |
| `profile_action_log`, `action_log_offset`, `log_offset` | str/int | Capture-time pinning — the log is bounded to `<= *_offset` as of the shutter. |
| `capture_meta` | obj | Capture + time-alignment metadata (below). |
| `delta_from_prev` | obj | stat deltas + `phash_distance` + `stuck_screen` + `action_count_delta`. |
| `program` | obj | Live program state (below). |
| `error` | obj\|null | Structured error (below) — present only when one was detected. |
| `anomaly` | obj | Anomaly (below). |
| `agent_eval` | obj | `{confidence, next_action, needs_rollback, regression_detected, what_went_wrong}`. |
| `git` | obj | `{branch, commit_hash, files_changed[], diff_summary, …}`. |
| `activity` | list | Recent AgentVision observer notes `{ts, description}`. |
| `config` | obj | Snapshot of the program's config values. |

### `capture_meta` (image↔log alignment)

| Field | Meaning |
|---|---|
| `shutter_ms` | Epoch ms the timestamp was stamped (== `timestamp_ms`). |
| `capture_end_ms` | Epoch ms the pixels finished being grabbed. |
| `capture_latency_ms` | Shutter duration; the image is valid across `[shutter_ms, capture_end_ms]`. |
| `window_found` | True if the target window was located (else full-screen/crop). |
| `black_frame` | True if the grab looks blank/black (bad capture — do NOT describe visuals). |
| `capture_target` | `window` \| `crop` \| `fullscreen`. |
| `capture_backend` | `screencapture` (macOS) \| `mss` (Windows/Linux). |
| `rate` | `{interval_s, shots_per_second}` in force at capture. |

The offsets and the timestamp are snapshotted **in the same instant, before the
shutter**, so no record with `ts_ms > shutter_ms` can leak into the frame.
Verify with `av_frame_alignment(seq)`.

### `error` (structured, any language)

Parsed by `modules.diagnostics.parse_exception` (Python/Node/Java/Go/Ruby/.NET).

| Field | Meaning |
|---|---|
| `exception_type` | e.g. `KeyError`, `NullPointerException`, `panic`. |
| `message` | The exception message (type prefix stripped). |
| `language` | `python` \| `node` \| `java` \| `go` \| `ruby` \| `dotnet` \| … |
| `frames` | `[{file, line, func}]` — the parsed stack, newest first. |
| `probable_cause` | Plain-English root-cause hypothesis. |
| `fingerprint` | Stable id (paths/numbers/hex normalized out) — dedups equivalent errors. |
| `occurrence_count` | Times this fingerprint was seen this session. |
| `first_seen` / `last_seen` | ISO-Z of first / most recent occurrence. |
| `stack_trace` | The raw traceback text (for full fidelity). |
| `likely_cause` / `source_context` | Legacy enrichment (from `error_enricher`). |

### `anomaly`

`{detected, type, description, severity, confidence, evidence[]}` — e.g.
`type: "error_in_log" | "screen_stuck"`, `severity: low|medium|high|critical`,
`confidence: 0..1`, `evidence`: concrete signals observed.

### `program` (`ProgramState`)

`{name, running, log_file, log_at_capture[], last_error, last_error_block,
stats{}, keypresses[], events[], gps_track[], gps_now{}, recent_actions[]}`.
`recent_actions` is the tail of the structured event log **bounded to the
shutter** — so it pairs exactly with the screenshot.

### Example frame (abridged)

```json
{
  "schema_version": "2.0.0",
  "sequence": 7,
  "timestamp": "2026-07-21T10:00:03.000Z",
  "summary": "KeyError: 'missing'",
  "recommended_next": "Inspect error.frames (file:line) and error.probable_cause — likely: A dict/map was accessed with a key that does not exist. Correlate with av_actions_around_frame(seq).",
  "tags": ["error"],
  "confidence": 0.2,
  "state_delta": {"added": {}, "removed": {}, "changed": {}, "changed_count": 0},
  "correlation": {"trace_ids": ["t-1"], "run_ids": ["r-9"]},
  "capture_meta": {
    "shutter_ms": 1784628003000.0, "capture_end_ms": 1784628003040.0,
    "capture_latency_ms": 40.0, "window_found": true, "black_frame": false,
    "capture_target": "window", "rate": {"interval_s": 0.5, "shots_per_second": 2.0}
  },
  "program": {"name": "Example", "running": true, "recent_actions": [
    {"ts_ms": 1784628000000, "category": "event", "source": "app",
     "trace_id": "t-1", "run_id": "r-9", "data": {"name": "tick"}}
  ]},
  "error": {
    "exception_type": "KeyError", "message": "'missing'", "language": "python",
    "frames": [{"file": "/app/main.py", "line": 42, "func": "run"}],
    "probable_cause": "A dict/map was accessed with a key that does not exist.",
    "fingerprint": "69670faa9f72", "occurrence_count": 1,
    "first_seen": "2026-07-21T10:00:03.000Z", "last_seen": "2026-07-21T10:00:03.000Z"
  },
  "anomaly": {"detected": true, "type": "error_in_log", "severity": "high",
              "confidence": 0.9, "evidence": ["error in log: KeyError: 'missing'",
                                              "exception_type=KeyError"]}
}
```

---

## 2. The unified event

One JSON object per normalized log line, from any language/format (see
`docs/LOG_ADAPTERS.md` for the 41 adapters). Shape:

```json
{
  "ts": "2026-07-21T10:00:00.123Z", "ts_ms": 1784628000123.0,
  "category": "log|debug|warn|error|exception|event|process|metric|…",
  "level": "TRACE|DEBUG|INFO|WARN|ERROR|FATAL",
  "source": "logger / subsystem", "trace_id": "…|null", "frame_seq": null,
  "data": {"message": "…", "adapter": "…", "…": "…"}, "raw": "<original line>"
}
```

`category` is derived from `level`, so an ERROR from **any** language is
`category:"error"` and trips failure detection uniformly. `GET /events/schema`
(`av_events_schema`) returns the category vocabulary + auto-discovered events.

---

## 3. The digest (`av_digest`)

The compact triage payload to read **first**. Token-light; ranked.

```json
{
  "schema_version": "2.0.0",
  "health": {"score": 0-100, "grade": "healthy|degraded|unhealthy|critical",
             "factors": ["…what lowered the score…"]},
  "attention": ["…worked top-down; each item names the drill-in tool…"],
  "latest_frame": {"sequence", "summary", "recommended_next", "tags", "confidence",
                   "running", "error{…}", "anomaly{…}", "state_delta{counts}",
                   "state_change_note": "player.hp: 100→80; …", "perf{cpu_percent,rss_mb,num_threads}"},
  "error_stats": {"total_failures", "session_failures", "distinct_fingerprints",
                  "new_this_session", "recurring", "frames_scanned", "frames_with_error",
                  "error_rate_recent", "trend": "rising|steady|falling|unknown"},
  "top_errors": [{"fingerprint", "count", "sample", "source", "first_ts", "last_ts"}],
  "new_error_fingerprints": ["…just-appeared this session…"],
  "capture": {"capturing", "shots_per_second", "interval_s", "frames_stored",
              "blank_frame_count", "window_missing", "rate_guidance"},
  "alignment": {"aligned", "leaked_after_shutter", "capture_latency_ms", "black_frame"},
  "guidance": "Read this first; check health.score, then work `attention` top-down."
}
```

`health.score` is a bounded 0–100 composite (deductions for not-running, new/
recurring errors, stuck screen, anomaly, missing window, blank frames).
`error_stats.error_rate_recent` is the fraction of the last ≤20 in-memory frames
that carried an error; `error_stats.trend` compares the newer vs older half of
that window → `rising | steady | falling` (`unknown` with <4 frames).

---

## Versioning

`SCHEMA_VERSION` lives in `shared/schema/snapshot_schema.py` and is stamped on
frames, the digest, and `/events/schema`. Consumers should read it and treat
unknown fields as forward-compatible (the loader already ignores unknown keys
and defaults missing ones — see `SnapshotFrame.from_json`).
