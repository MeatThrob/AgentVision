"""
AgentVision Snapshot Schema
----------------------------
One _frame.json is written beside every screenshot.

Designed for Claude to read image + JSON together to understand exactly what
the program was doing at the moment the screenshot was taken:

  1. identity      — frame number, timestamp, which PNG this pairs with
  2. program       — is it running? what are the EXACT log lines right now?
  3. log_at_capture — last 40 log lines timestamped at capture moment
  4. errors        — any error/traceback detected in the log right now
  5. keypresses    — key events the program logged (if available)
  6. stats         — program-specific stats (score, level, game count, etc.)
  7. activity      — recent natural-language events from AgentVision
  8. git           — branch + what files changed (code context)
  9. config        — key config values so you know how the program is set up
 10. agent_eval    — confidence + recommended next action
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict, fields as _dc_fields
from typing import Optional
import json, time, uuid


# ── Schema version ────────────────────────────────────────────────────────────
# Stamped onto every frame (and surfaced by the digest/events schema) so an AI
# consumer can adapt if the shape evolves. Bump the MINOR for additive fields,
# the MAJOR for breaking changes. Kept as a plain string for easy comparison.
SCHEMA_VERSION = "2.0.0"


def _only_known(cls, d: dict) -> dict:
    """Filter a dict to the dataclass's own fields — makes from_json tolerant of
    older/newer sidecars on disk (extra keys ignored, missing keys defaulted).
    Non-dict input (a nested key whose value drifted to a string/number/None)
    yields {} rather than raising, so from_json never blows up on bad sidecars."""
    if not isinstance(d, dict):
        return {}
    known = {f.name for f in _dc_fields(cls)}
    return {k: v for k, v in d.items() if k in known}


# ── Legacy stubs (used by helper modules, NOT written to frame JSON) ──────────

@dataclass
class PerfInfo:
    cpu_percent:  float = 0.0
    ram_gb:       float = 0.0
    disk_free_gb: float = 0.0
    system_cpu:   float = 0.0
    system_ram_gb: float = 0.0


@dataclass
class TestInfo:
    pass_count:     int       = 0
    fail_count:     int       = 0
    skip_count:     int       = 0
    failed_tests:   list[str] = field(default_factory=list)
    failure_detail: str       = ""
    duration_ms:    float     = 0.0
    #: Did the test runner actually EXECUTE? A subprocess that could not start
    #: (no interpreter, pytest not installed, timeout) must not read as
    #: "0 passed, 0 failed" — that is an all-clear for tests that never ran.
    ran:            bool      = True
    error:          str       = ""


@dataclass
class EnvInfo:
    python_version: str  = ""
    os_version:     str  = ""
    cwd:            str  = ""
    active_branch:  str  = ""
    venv:           str  = ""
    docker_running: bool = False


@dataclass
class CodebaseInfo:
    root:             str       = ""
    total_files:      int       = 0
    total_lines:      int       = 0
    file_tree:        str       = ""
    dependency_edges: list[str] = field(default_factory=list)


@dataclass
class ProgramState:
    """Live state of the connected program at the exact moment of capture."""
    name:    str  = ""       # e.g. "MyApp (desktop GUI)"
    running: bool = False
    log_file: str = ""
    # WHY `running` says what it says: {pid, process_name, exe, matched_by}.
    # 12,921 frames were once stored with running=true for a program that had
    # exited; nothing recorded what had matched, so no frame could be checked
    # afterwards. A liveness claim with no evidence is not reviewable.
    running_evidence: dict = field(default_factory=dict)
    # The exact log lines present at capture time — pairs directly with the PNG
    log_at_capture:   list[str] = field(default_factory=list)
    # Last error/traceback found in the log at this moment
    last_error:       str       = ""
    last_error_block: str       = ""   # full traceback if available
    # Program-specific stats (score, xp/hr, level, games played, etc.)
    stats: dict = field(default_factory=dict)
    # Raw key/action lines the program logged (kept for full fidelity)
    keypresses: list[str] = field(default_factory=list)
    # Structured events parsed from the log — easy for Claude to read at a glance.
    # Each item: {t, kind, target, dir_deg, walk_deg, key, world_x, world_y, gps, note}
    events: list[dict] = field(default_factory=list)
    # Compact GPS breadcrumb trail — last N world coords with timestamps.
    # Each item: {t, x, y, dir_deg}
    gps_track: list[dict] = field(default_factory=list)
    # Most recent world coord at capture moment: {x, y, dir_deg}
    gps_now: dict = field(default_factory=dict)
    # Tail of the program's structured action log (e.g. actions.jsonl) at this moment.
    # Each item is the raw JSON record emitted by the bridged program — fully generic.
    # Designed so that one frame fetch shows the screenshot, the log tail, AND the
    # last N structured events without needing a follow-up call.
    recent_actions: list[dict] = field(default_factory=list)


@dataclass
class StackFrameInfo:
    """One parsed frame of a traceback/stack — structured for AI reading."""
    file: str = ""
    line: int = 0
    func: str = ""


@dataclass
class ErrorInfo:
    """Enriched, STRUCTURED error — parsed from the raw traceback so an AI can
    reason about it without re-parsing text. Works across languages (Python,
    Node, Java, Go, Ruby, .NET, …) via modules.diagnostics.parse_exception."""
    file:           str = ""
    line:           int = 0
    message:        str = ""
    stack_trace:    str = ""
    likely_cause:   str = ""   # plain-English explanation
    source_context: str = ""   # ±5 lines of source around the error
    # ── Structured (v2) ──────────────────────────────────────────────────────
    exception_type: str = ""                 # e.g. "KeyError", "TypeError"
    language:       str = ""                 # python|node|java|go|ruby|dotnet|…
    frames:         list[dict] = field(default_factory=list)  # [{file,line,func}]
    probable_cause: str = ""                 # heuristic root-cause hypothesis
    fingerprint:    str = ""                 # stable id (dedup across occurrences)
    occurrence_count: int = 1                # times this fingerprint seen this session
    first_seen:     str = ""                 # ISO-Z of first occurrence
    last_seen:      str = ""                 # ISO-Z of most recent occurrence


@dataclass
class GitInfo:
    """What code state is the program running right now."""
    branch:         str       = ""
    commit_hash:    str       = ""
    files_changed:  list[str] = field(default_factory=list)
    diff_summary:   str       = ""   # short stat
    unstaged_count: int       = 0
    staged_count:   int       = 0


@dataclass
class AnomalyInfo:
    detected:    bool = False
    type:        str  = ""   # error_in_log | screen_stuck | error_spike | …
    description: str  = ""
    severity:    str  = ""   # low | medium | high | critical
    # ── Structured (v2) ──────────────────────────────────────────────────────
    confidence:  float     = 0.0   # 0..1 — how sure we are this is a real anomaly
    evidence:    list[str] = field(default_factory=list)  # concrete signals seen


@dataclass
class AgentEval:
    """
    Per-frame self-evaluation.
    confidence:  0.0 (broken) → 1.0 (healthy)
    next_action: what Claude should do next based on current state
    """
    confidence:          float = 0.0
    next_action:         str   = ""
    needs_rollback:      bool  = False
    regression_detected: bool  = False
    what_went_wrong:     str   = ""


@dataclass
class ActivityEntry:
    ts:          str = ""
    description: str = ""


@dataclass
class SnapshotFrame:
    """
    Complete diagnostic frame — one per screenshot.
    Read the PNG for visual context, this JSON for program state.
    Together they tell you exactly what was happening at this moment.
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    schema_version: str = SCHEMA_VERSION   # so AI consumers can adapt to changes
    frame_id:     str   = field(default_factory=lambda: str(uuid.uuid4())[:8])
    sequence:     int   = 0
    timestamp:    str   = ""
    timestamp_ms: float = field(default_factory=lambda: time.time() * 1000)

    # ── AI triage layer (read THESE first) ────────────────────────────────────
    # A model can act on this frame from these fields alone, then drill in.
    summary:          str        = ""   # one-line NL description of this moment
    recommended_next: str        = ""   # the single best next action for the AI
    tags:             list[str]   = field(default_factory=list)  # e.g. ["error","stuck"]
    confidence:       float       = 0.0  # 0 (broken) .. 1 (healthy) — mirror of agent_eval
    # Key-level diff of program state vs the previous frame (added/removed/changed).
    state_delta:      dict        = field(default_factory=dict)
    # Correlation handles so the AI can pull the full story for this moment.
    correlation:      dict        = field(default_factory=dict)  # {trace_ids, run_ids}
    # Resource/perf metrics for the target process at capture time (psutil):
    # {found, pid, cpu_percent, rss_mb, ram_gb, num_threads, num_fds?, status}.
    perf:             dict        = field(default_factory=dict)

    # ── Associated files (same folder as the PNG) ─────────────────────────────
    image_file: str = ""   # frame_00001.png  — the screenshot
    json_file:  str = ""   # frame_00001_frame.json — this file
    diff_file:  str = ""   # frame_00001.diff — full git diff

    # ── Capture-time pinning (so this frame stays correct even if profile
    #    switches or files grow). Reads against the action log are bounded
    #    by action_log_offset so no record written AFTER the shutter sneaks in.
    profile_action_log: str = ""    # path that was active at capture time
    action_log_offset:  int = 0     # bytes — read [..offset] for this frame
    log_offset:         int = 0     # bytes — same for the primary text log

    # ── Capture + time-alignment metadata (see bridge_server._take_frame) ─────
    # Everything an AI needs to trust image↔log correlation for THIS frame:
    #   shutter_ms         epoch ms the timestamp was stamped (== timestamp_ms)
    #   capture_end_ms     epoch ms the pixels finished being grabbed
    #   capture_latency_ms how long the shutter took (image is valid across this)
    #   log_offset/action_log_offset  byte cutoffs, snapshotted AT the shutter
    #   window_found       True if the target window was located (else full/crop)
    #   black_frame        True if the grabbed image looks blank/black (bad grab)
    #   capture_backend    "screencapture" | "mss" | ...
    #   rate               {interval_s, shots_per_second} in force at capture
    #   note               human-readable alignment note for the AI
    capture_meta: dict = field(default_factory=dict)

    # ── Cross-frame deltas (Replay.io / Sentry session-replay style) ─────────
    # Surfaces what CHANGED since the previous frame, so Claude can scan a
    # sequence and pick the moment things started going wrong.
    delta_from_prev: dict = field(default_factory=dict)

    # ── Diagnostic sections (ordered for top-to-bottom reading) ───────────────
    program:    ProgramState        = field(default_factory=ProgramState)
    error:      Optional[ErrorInfo] = None
    git:        GitInfo             = field(default_factory=GitInfo)
    anomaly:    AnomalyInfo         = field(default_factory=AnomalyInfo)
    agent_eval: AgentEval           = field(default_factory=AgentEval)
    activity:   list[ActivityEntry] = field(default_factory=list)
    config:     dict                = field(default_factory=dict)

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent, default=str)

    @classmethod
    def from_json(cls, raw: str) -> "SnapshotFrame":
        """Rebuild a frame from a sidecar. Tolerant of older/newer sidecars: every
        nested object is filtered to its dataclass's known fields, so extra keys
        are ignored and missing keys default (no drift-induced TypeErrors)."""
        d = json.loads(raw)
        # Build the top-level frame from scalar/simple fields only — the nested
        # dataclass fields are reconstructed explicitly below so a present-but-
        # empty {} (falsy) still yields a properly-defaulted dataclass rather
        # than leaking a raw dict into a field typed as a dataclass.
        _nested = {"program", "error", "git", "anomaly", "agent_eval", "activity"}
        top = {k: v for k, v in _only_known(cls, d).items() if k not in _nested}
        frame = cls(**top)
        if isinstance(d.get("program"), dict):
            frame.program = ProgramState(**_only_known(ProgramState, d["program"]))
        # error stays None unless a dict is actually present.
        if isinstance(d.get("error"), dict):
            frame.error = ErrorInfo(**_only_known(ErrorInfo, d["error"]))
        if isinstance(d.get("git"), dict):
            frame.git = GitInfo(**_only_known(GitInfo, d["git"]))
        if isinstance(d.get("anomaly"), dict):
            frame.anomaly = AnomalyInfo(**_only_known(AnomalyInfo, d["anomaly"]))
        if isinstance(d.get("agent_eval"), dict):
            frame.agent_eval = AgentEval(**_only_known(AgentEval, d["agent_eval"]))
        if isinstance(d.get("activity"), list):
            frame.activity = [ActivityEntry(**_only_known(ActivityEntry, e))
                              for e in d["activity"] if isinstance(e, dict)]
        return frame
