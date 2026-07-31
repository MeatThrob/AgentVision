"""
Context Collector
------------------
Assembles one SnapshotFrame per capture cycle.

Goal: pair each screenshot with the EXACT program state at that moment so
Claude can read image + JSON together and understand what the program was
doing, what went wrong, and what to fix — no guessing.

Data captured per frame:
  - Exact log lines present at capture time (last 40)
  - Any error / traceback in those log lines
  - Key events / button presses the program logged
  - Program-specific stats (score, level, xp/hr, etc.)
  - Git state (branch, changed files — what code is actually running)
  - Config values (how the program is configured right now)
  - AgentVision activity log (what the observer noticed)
  - Agent self-eval (confidence + recommended action)
"""
from __future__ import annotations
import json
import time
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from shared.schema.snapshot_schema import (
    SnapshotFrame, ErrorInfo, ProgramState,
    GitInfo, AnomalyInfo, AgentEval, ActivityEntry
)

from modules.diff_analyzer          import collect_git_info, get_full_diff
from modules.activity_log           import get_logger
from modules.error_enricher         import enrich_error
from modules import diagnostics as _dx

from connectors.program_connector import ProgramProfile, ProgramDataReader


# Patterns that indicate a key action / button press in the log
_KEYPRESS_PATTERNS = [
    re.compile(r'(?i)(key|press|click|button|action|cast|skill|move|attack|pickup|tp|town portal|run|walk|portal)'),
]

# Match a game-bot PS5/movement debug line, e.g.:
#   [...] DEBUG  PS5: RUN  malah dist=30px dir=264° walk=264° ... world=(5081.5,5029.5) gps=3.9u
# Only match command verbs that immediately follow a PS5/INPUT/BOT/GAMEPAD prefix.
# This excludes false positives where words like "PORTAL" or "ATTACK" appear in
# OCR tooltip text (e.g. "TOME OF TOWN PORTAL", "THROW DAMAGE").
_KIND_RE  = re.compile(
    r'(?:PS5|GAMEPAD|INPUT|BOT|KEY)\s*:\s*'
    r'(?P<kind>RUN|WALK|CAST|ATTACK|PICKUP|TP|PORTAL|CLICK|PRESS|MOVE)\b'
    r'(?:\s+(?P<target>[A-Za-z0-9_\-]+))?',
    re.IGNORECASE,
)
_TIME_RE  = re.compile(r'(\d{2}:\d{2}:\d{2}[,.]\d+)')
_DIR_RE   = re.compile(r'dir=(-?\d+(?:\.\d+)?)°')
_WALK_RE  = re.compile(r'walk=(-?\d+(?:\.\d+)?)°')
_WORLD_RE = re.compile(r'world=\((-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\)')
_GPS_RE   = re.compile(r'gps=(-?\d+(?:\.\d+)?)u?')

# Chiaki keybind map (left stick = walk direction).
# Translates a degree heading → the keystroke chiaki sends to the OS.
# 0° = right, 90° = up (standard math convention used by a game bot).
def _dir_to_chiaki_key(deg: float | None) -> str:
    if deg is None:
        return ""
    d = deg % 360
    # 8-way: each sector 45°, centered on cardinals/diagonals
    sectors = [
        (0,    "right",      "U"),       # right stick right? actually left stick right=7
        (45,   "up-right",   "G+U"),
        (90,   "up",         "G"),
        (135,  "up-left",    "G+Y"),
        (180,  "left",       "Y"),
        (225,  "down-left",  "8+Y"),
        (270,  "down",       "8"),
        (315,  "down-right", "8+U"),
    ]
    # Note: from the user's chiaki config:
    #   Left Stick Right=7, Left Stick Left=Y, Left Stick Up=G, Left Stick Down=8
    sectors = [
        (0,    "right",      "7"),
        (45,   "up-right",   "G+7"),
        (90,   "up",         "G"),
        (135,  "up-left",    "G+Y"),
        (180,  "left",       "Y"),
        (225,  "down-left",  "8+Y"),
        (270,  "down",       "8"),
        (315,  "down-right", "8+7"),
    ]
    best = min(sectors, key=lambda s: min(abs(d - s[0]), 360 - abs(d - s[0])))
    return f"{best[1]} ({best[2]})"


def _parse_event(line: str) -> dict | None:
    """Parse a single log line into a structured event dict, or None if not interesting.

    UNIVERSAL: the primary structured-event source for any bridged program is its
    action_log_file (JSONL), read separately by the bridge. This text-log parser is a
    best-effort fallback that (a) extracts a generic log note for any program, and
    (b) additionally pulls game-bot movement fields ONLY when those specific patterns
    actually match — so no game-bot-specific interpretation is forced onto other programs."""
    km = _KIND_RE.search(line)
    if not km:
        # Generic path for ANY program: surface a plain log note. (No program-specific
        # keypress requirement — every program's interesting lines are worth a note.)
        stripped = line.strip()
        if not stripped:
            return None
        return {"kind": "log", "note": stripped[:200]}

    # A game-bot-style "kind" line matched — extract the game-bot movement fields, but only
    # populate the chiaki key translation when a walk/dir heading is actually present.
    tm = _TIME_RE.search(line)
    dm = _DIR_RE.search(line)
    wm = _WALK_RE.search(line)
    pm = _WORLD_RE.search(line)
    gm = _GPS_RE.search(line)
    dir_deg  = float(dm.group(1)) if dm else None
    walk_deg = float(wm.group(1)) if wm else None
    heading = walk_deg if walk_deg is not None else dir_deg
    return {
        "t":        tm.group(1) if tm else "",
        "kind":     km.group("kind").upper(),
        "target":   km.group("target") or "",
        "dir_deg":  dir_deg,
        "walk_deg": walk_deg,
        "key":      _dir_to_chiaki_key(heading) if heading is not None else "",
        "world_x":  float(pm.group(1)) if pm else None,
        "world_y":  float(pm.group(2)) if pm else None,
        "gps":      float(gm.group(1)) if gm else None,
        "note":     "",
    }


def _extract_events(log_lines: list[str], limit: int = 20) -> list[dict]:
    """Parse the log tail into a clean list of structured events for Claude."""
    out: list[dict] = []
    for line in log_lines:
        ev = _parse_event(line)
        if ev:
            out.append(ev)
    return out[-limit:]


#: Levels that mean "this is a failure", matched case-insensitively.
_ERROR_LEVELS = {"ERROR", "CRITICAL", "FATAL", "SEVERE", "EMERG", "ALERT", "PANIC"}


def _error_from_actions(actions: list[dict]) -> tuple[str, str]:
    """Recover an error from the STRUCTURED action records for this frame.

    The primary-log scan looks for the literal substrings "ERROR"/"CRITICAL"/
    "Traceback" in one text file. That misses the case AgentVision itself
    creates: a program wired up by av_install_project emits JSONL with
    {category:"error", level:"ERROR", data:{message, exception_type, stack}} —
    structured, unambiguous, and previously ignored, so the frame carried
    error=None and no incident was ever frozen for it.

    Returns (message, block, exception_type). The first two match the text path's
    shape so downstream enrichment is unchanged; the third is handed over
    DIRECTLY rather than being recovered by regex, because a structured record
    already states the type and parse_exception cannot reliably extract it from a
    bare "Type: message" line (it looks for language-specific traceback shapes).
    Newest matching record wins.
    """
    for rec in reversed(actions or []):
        if not isinstance(rec, dict):
            continue
        cat = str(rec.get("category") or "").strip().lower()
        lvl = str(rec.get("level") or "").strip().upper()
        if cat != "error" and lvl not in _ERROR_LEVELS:
            continue
        data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
        etype = str(data.get("exception_type") or data.get("type")
                    or rec.get("exception_type") or "").strip()
        msg = str(data.get("message") or data.get("msg")
                  or rec.get("message") or "").strip()
        stack = str(data.get("stack") or data.get("stack_trace")
                    or data.get("traceback") or rec.get("stack_trace") or "").strip()
        if not (etype or msg or stack):
            continue
        line = f"{etype}: {msg}" if (etype and msg) else (etype or msg)
        return line, (stack or line), etype
    return "", "", ""


def _error_from_json_line(line: str) -> tuple[str, str, str]:
    """Recover an error from a raw JSONL line that the TEXT scan matched.

    last_error_from_log() greps the primary log for the substring "ERROR". When
    that log is itself JSONL — which is exactly what AgentVision's own universal
    emitter writes, and may be a program's only log — the match is the whole JSON
    record, so the "message" became the entire serialized line and the exception
    type was lost:

        message: '{"ts_ms": 1785285055226.5, "category": "error", ... }'

    Parsing it here restores the real message and type. Returns ("", "", "") for
    anything that is not an error-bearing JSON object, so plain text is untouched.
    """
    s = (line or "").strip()
    if not s.startswith("{"):
        return "", "", ""
    try:
        rec = json.loads(s)
    except Exception:
        return "", "", ""
    if not isinstance(rec, dict):
        return "", "", ""
    return _error_from_actions([rec])


def _extract_keypresses(log_lines: list[str]) -> list[str]:
    """Pull action/keypress lines from the log tail (raw, kept for fidelity)."""
    found = []
    for line in reversed(log_lines):
        for pat in _KEYPRESS_PATTERNS:
            if pat.search(line):
                clean = line.strip()
                if clean and clean not in found:
                    found.append(clean)
                break
        if len(found) >= 15:
            break
    return list(reversed(found))


def _build_gps_track(events: list[dict], limit: int = 30) -> tuple[list[dict], dict]:
    """Compact breadcrumb trail of recent world coords. Returns (track, latest)."""
    track: list[dict] = []
    for ev in events:
        if ev.get("world_x") is not None and ev.get("world_y") is not None:
            track.append({
                "t":       ev.get("t", ""),
                "x":       round(ev["world_x"], 1),
                "y":       round(ev["world_y"], 1),
                "dir_deg": ev.get("walk_deg") or ev.get("dir_deg"),
            })
    track = track[-limit:]
    latest = track[-1] if track else {}
    return track, latest


class ContextCollector:

    def __init__(self,
                 profile: ProgramProfile,
                 save_folder: str = "snapshots",
                 run_tests_each_frame: bool = False):
        self.profile   = profile
        self.save_folder = Path(save_folder)
        self._sequence = 0
        self._reader   = ProgramDataReader(profile)
        self._logger   = get_logger()
        # Cached for delta_from_prev computation (Replay.io-style)
        self._prev_stats:        dict           = {}
        self._prev_action_count: int            = 0
        self._prev_image_path:   Optional[str]  = None
        self._prev_phash:        Optional[int]  = None
        # Session-scoped error registry for dedup / occurrence / first-last-seen,
        # keyed by fingerprint. Persists across frames within one bridge process.
        self._error_registry:    dict           = {}
        # Previous FULL program state (stats + state.json) for nested state_delta.
        self._prev_state_full:   dict           = {}
        self._logger.set_log_file(str(self.save_folder / "activity.log"))

    def _register_error(self, fp: str, iso: str, identity: str = "") -> tuple[int, str, str]:
        """Record an occurrence of fingerprint `fp` at time `iso`. Returns
        (occurrence_count, first_seen, last_seen).

        `identity` is the underlying error block for this frame. A crash halts
        the program but its traceback lingers in the log tail across many capture
        cycles; counting once per frame would inflate occurrence_count without
        bound and mislead the AI ("recurring", "seen N×"). We therefore count a
        genuinely NEW occurrence only — the fingerprint's first appearance, or
        when the block text differs from what was last registered for it. A stale
        error simply persisting in the tail keeps the count flat (last_seen still
        advances)."""
        if not fp:
            return 1, iso, iso
        rec = self._error_registry.get(fp)
        if rec is None:
            rec = {"count": 1, "first": iso, "last": iso, "identity": identity}
            self._error_registry[fp] = rec
            return rec["count"], rec["first"], rec["last"]
        rec["last"] = iso
        # Only a changed underlying block counts as a new occurrence.
        if identity != rec.get("identity"):
            rec["count"] += 1
            rec["identity"] = identity
        return rec["count"], rec["first"], rec["last"]

    def set_profile(self, profile: ProgramProfile):
        self.profile = profile
        self._reader = ProgramDataReader(profile)
        self._logger.log(f"Connected program changed to: {profile.display_name}")

    # ── Main entry ────────────────────────────────────────────────────────────

    def collect(self, image_file: str = "",
                capture_iso: str = "", capture_ms: float = 0.0,
                offsets: dict | None = None,
                capture_meta: dict | None = None,
                sequence: int = 0) -> SnapshotFrame:
        """`sequence` lets the caller pin the frame number.

        The bridge names the PNG from its own capture counter, so an internal
        counter here produced two numbers for one frame: the file said
        frame_08533.png while frame.sequence said 46. Everything keyed by seq
        (the frame index, the retention ledger, bookmarks, incidents) then
        disagreed with everything keyed by filename, and after a restart a new
        run's seq 46 aliased an old frame's record. When the caller supplies a
        sequence it wins; the internal counter remains for direct callers.
        """
        if sequence and int(sequence) > 0:
            seq = int(sequence)
            self._sequence = max(self._sequence, seq)
        else:
            self._sequence += 1
            seq = self._sequence
        stem = Path(image_file).stem if image_file else f"frame_{seq:05d}"

        # Use capture-time stamps if the caller supplied them (the bridge does);
        # otherwise stamp now (legacy direct callers, /capture endpoint, etc.).
        if not capture_iso or not capture_ms:
            from shared import clock as _clk
            capture_iso, capture_ms = _clk.now()

        frame              = SnapshotFrame()
        frame.sequence     = seq
        frame.timestamp    = capture_iso     # UTC ISO-Z, ms precision
        frame.timestamp_ms = capture_ms      # UTC epoch ms (same instant)
        frame.image_file   = image_file
        frame.json_file    = f"{stem}_frame.json"
        frame.diff_file    = f"{stem}.diff"
        # Snapshotted profile + offsets — pin THIS frame to the file paths
        # and byte positions that were authoritative at capture time. Survives
        # profile switches and file growth without losing alignment.
        frame.profile_action_log = (offsets or {}).get("_action_log_path") or \
                                   getattr(self.profile, "action_log_file", "")
        frame.action_log_offset  = (offsets or {}).get("action_log_offset", 0)
        frame.log_offset         = (offsets or {}).get("log_offset", 0)
        # Capture + time-alignment metadata (from the bridge). Backfill the
        # byte offsets so the alignment story is complete even for callers that
        # only pass `offsets` (e.g. the legacy /capture endpoint).
        cm = dict(capture_meta or {})
        cm.setdefault("shutter_ms", capture_ms)
        cm.setdefault("action_log_offset", frame.action_log_offset)
        cm.setdefault("log_offset", frame.log_offset)
        frame.capture_meta = cm

        # ── 1. Program state — exact snapshot at capture time ─────────────
        prog          = ProgramState()
        prog.name     = self.profile.display_name
        # One scan, and it carries its own evidence — see ProgramState.
        _ev           = self._reader.running_evidence()
        prog.running  = bool(_ev.get("running"))
        prog.running_evidence = {k: _ev.get(k) for k in
                                 ("pid", "process_name", "exe", "matched_by")}
        prog.log_file = self.profile.log_file

        # Exact log lines at this moment — these pair with the screenshot.
        # Bound by the capture-time byte offset so no line written after the
        # shutter sneaks into this frame.
        log_lines = self._reader.tail_log(40, max_offset=frame.log_offset)
        prog.log_at_capture = log_lines

        # Error detection from current log tail
        err_msg, err_block = self._reader.last_error_from_log()
        prog.last_error       = err_msg
        prog.last_error_block = err_block
        # NOTE: last_error_from_log() only substring-scans the PRIMARY text log.
        # It is blind to structured records — which is what AgentVision's own
        # universal emitter writes (agentvision/actions.jsonl, category="error").
        # A program that reported errors *properly* therefore never populated
        # frame.error, and so never triggered incident freezing, retention
        # flagging or an av_diagnose fingerprint. The structured fallback below
        # closes that; see _error_from_actions.

        # Program-specific stats (score, level, xp/hr, games played, etc.)
        prog.stats = self._reader.latest_stats()

        # Key events / actions / button presses logged by the program
        prog.keypresses = _extract_keypresses(log_lines)

        # Structured events + GPS breadcrumb trail — designed for Claude to read
        prog.events    = _extract_events(log_lines, limit=20)
        prog.gps_track, prog.gps_now = _build_gps_track(prog.events, limit=30)

        # Inline structured action records — one frame fetch shows screenshot,
        # log tail, AND last 20 structured events. Generic across any bridged
        # program that emits a JSONL action log.
        # Bound by the capture-time byte offset so we never include records
        # that arrived after the shutter — perfect frame↔record alignment.
        prog.recent_actions = self._reader.tail_actions(
            20,
            max_offset=frame.action_log_offset,
            path_override=frame.profile_action_log,
        )

        frame.program = prog

        # Structured fallback: if the text log showed nothing, look at the
        # action records we ALREADY read for this frame. They are bounded (20)
        # and offset-aligned to the shutter, so this costs no extra I/O and
        # cannot pull in a record that arrived after the screenshot.
        structured_etype = ""
        if not (err_msg or err_block):
            err_msg, err_block, structured_etype = _error_from_actions(
                prog.recent_actions)
            if err_msg or err_block:
                prog.last_error       = err_msg
                prog.last_error_block = err_block
        else:
            # The text scan DID match — but if the primary log is JSONL it just
            # handed us a whole serialized record as the "message". Unwrap it.
            j_msg, j_block, j_type = _error_from_json_line(err_msg)
            if j_msg:
                err_msg, err_block, structured_etype = j_msg, j_block, j_type
                prog.last_error       = err_msg
                prog.last_error_block = err_block

        # ── 2. Error enrichment — STRUCTURED, AI-consumable ───────────────
        # Enrich when EITHER a message line OR a raw block is present:
        # parse_exception works off the block and backfills the message, so a
        # non-empty block must still trigger the full deep-AI layer even if the
        # message line came back blank (e.g. traceback followed by a blank line).
        if err_msg or err_block:
            ei = ErrorInfo(message=err_msg, stack_trace=err_block)
            ei = enrich_error(ei, self.profile.project_root or ".")
            # Parse the raw traceback into structured fields (any language).
            parsed = _dx.parse_exception(err_block or err_msg)
            if parsed:
                ei.exception_type = parsed.get("exception_type", "")
                ei.language       = parsed.get("language", "")
                ei.frames         = parsed.get("frames", [])
                ei.probable_cause = parsed.get("probable_cause", "") or ei.likely_cause
                # Prefer the parsed (type-stripped) message so summaries don't
                # read "KeyError: KeyError: 'x'". Fall back to the raw last line.
                if parsed.get("message"):
                    ei.message = parsed["message"]
            # A structured record already NAMED its exception type; trust it over
            # (and as a backfill for) the text parser, which only recognises
            # language-specific traceback shapes and returns "" for a plain
            # "Type: message" line. Without this the type was silently lost and
            # probable_cause / grouping degraded to a generic "error".
            if structured_etype and not ei.exception_type:
                ei.exception_type = structured_etype
                if ei.message.startswith(structured_etype + ":"):
                    ei.message = ei.message[len(structured_etype) + 1:].strip()
            # Fingerprint + dedup/occurrence tracking across the session.
            ei.fingerprint = _dx.fingerprint(err_block or err_msg)
            cnt, first, last = self._register_error(
                ei.fingerprint, capture_iso, identity=(err_block or err_msg or ""))
            ei.occurrence_count = cnt
            ei.first_seen = first
            ei.last_seen  = last
            frame.error = ei
            self._logger.log(f"Error detected: {err_msg[:80]}")

        # ── 3. Git state — what code is actually running ──────────────────
        if self.profile.project_root:
            frame.git = collect_git_info(self.profile.project_root)

        # ── 4. Anomaly detection (error spikes, new errors) ───────────────
        # Gated identically to enrichment (err_msg OR err_block) so a block whose
        # message line was blank still raises the anomaly. Use the enriched
        # message (parse_exception backfills it) as the effective description.
        if err_msg or err_block:
            eff_msg = (frame.error.message if frame.error and frame.error.message
                       else err_msg or (err_block or "").strip().splitlines()[-1] if err_block else "")
            first_seen_now = frame.error is not None and frame.error.occurrence_count == 1
            evidence = [f"error in log: {eff_msg[:100]}"]
            if frame.error and frame.error.exception_type:
                evidence.append(f"exception_type={frame.error.exception_type}")
            if frame.error and frame.error.occurrence_count > 1:
                evidence.append(f"recurred {frame.error.occurrence_count}× this session")
            frame.anomaly = AnomalyInfo(
                detected=True,
                type="error_in_log",
                description=eff_msg[:120],
                severity="high" if "CRITICAL" in (err_block or "").upper() else "medium",
                confidence=0.9 if err_block else 0.7,
                evidence=evidence)

        # ── 4b. Nested state delta — diffs the FULL program state (numeric
        #        stats + the program's state.json), flattened to dotted leaf
        #        keys, vs the previous frame. Token-bounded. ─────────────────
        try:
            cur_state_full = {"stats": prog.stats or {},
                              "state": self._reader.read_state() or {}}
            frame.state_delta = _dx.state_delta(self._prev_state_full, cur_state_full)
            self._prev_state_full = cur_state_full
        except Exception:
            frame.state_delta = {}

        # ── 4c. Correlation — trace/run ids present in this frame's records ──
        try:
            trace_ids, run_ids = set(), set()
            for r in (prog.recent_actions or []):
                if isinstance(r, dict):
                    if r.get("trace_id"):
                        trace_ids.add(str(r["trace_id"]))
                    if r.get("run_id"):
                        run_ids.add(str(r["run_id"]))
            frame.correlation = {"trace_ids": sorted(trace_ids)[:10],
                                 "run_ids": sorted(run_ids)[:10]}
        except Exception:
            frame.correlation = {}

        # ── 4d. Per-frame resource/perf metrics (structured) ───────────────
        try:
            frame.perf = self._reader.process_perf() if prog.running else {"found": False}
        except Exception:
            frame.perf = {}

        # ── 5. Agent self-evaluation ──────────────────────────────────────
        frame.agent_eval = self._evaluate(frame)

        # ── 6. AgentVision activity log ───────────────────────────────────
        for line in self._reader.last_log_activity(5):
            self._logger.log(line)
        raw_activity = self._logger.get_recent(12)
        frame.activity = [
            ActivityEntry(ts=e.ts, description=e.description)
            for e in raw_activity]

        # ── 7. Config snapshot ────────────────────────────────────────────
        frame.config = self._reader.read_config_summary()

        # ── 8. Git diff sidecar ───────────────────────────────────────────
        if self.profile.project_root:
            try:
                diff_text = get_full_diff(self.profile.project_root)
                if diff_text.strip():
                    (self.save_folder / frame.diff_file).write_text(diff_text)
            except Exception:
                pass

        # ── 9. delta_from_prev — what CHANGED since the last frame ───────
        try:
            frame.delta_from_prev = self._compute_delta(prog, image_file)
            # Surface a structured anomaly when 5 consecutive frames hash
            # nearly identical — distinguishes "program stuck" from "running"
            self._maybe_flag_screen_stuck(frame)
        except Exception:
            frame.delta_from_prev = {}

        # ── 10. AI triage layer — one-line summary + recommended_next + tags
        #        + confidence. Deterministic; the model reads these FIRST. ───
        try:
            from dataclasses import asdict as _asdict
            # "stuck" for the summary means the CONFIRMED hung signal (the
            # ≥5-frame streak anomaly), NOT a single identical pair — a static
            # screen across one interval is normal and must not read as hung.
            confirmed_stuck = bool(frame.anomaly
                                   and getattr(frame.anomaly, "type", "") == "screen_stuck")
            tri = _dx.summarize_frame(
                running=prog.running,
                error=_asdict(frame.error) if frame.error else None,
                anomaly=_asdict(frame.anomaly) if frame.anomaly else None,
                stuck=confirmed_stuck,
                stats=prog.stats,
                state_delta_d=frame.state_delta,
                action_count_delta=(frame.delta_from_prev or {}).get("action_count_delta", 0),
            )
            frame.summary          = tri["summary"]
            frame.recommended_next = tri["recommended_next"]
            frame.tags             = tri["tags"]
            frame.confidence       = tri["confidence"]
            # Keep agent_eval.confidence consistent with the triage confidence.
            frame.agent_eval.confidence = tri["confidence"]
        except Exception:
            pass

        # Attach for overlay renderer
        frame._program_name    = self.profile.display_name
        frame._program_running = prog.running

        return frame

    def _compute_delta(self, prog: ProgramState, image_file: str) -> dict:
        """Surface what changed since prior frame: stat deltas, action-rate
        delta, screenshot perceptual-hash delta (stuck-screen detector)."""
        delta: dict = {"stats": {}, "action_count": 0,
                       "phash_distance": None, "stuck_screen": False}
        # Stat deltas — numeric fields only
        cur_stats = prog.stats or {}
        for k, v in cur_stats.items():
            if not isinstance(v, (int, float)):
                continue
            prev = self._prev_stats.get(k)
            if isinstance(prev, (int, float)):
                delta["stats"][k] = round(v - prev, 4)
        self._prev_stats = dict(cur_stats)

        # Action-rate delta — count of new JSONL records since last frame
        cur_count = len(prog.recent_actions or [])
        delta["action_count"] = cur_count
        delta["action_count_delta"] = cur_count - self._prev_action_count
        self._prev_action_count = cur_count

        # Screenshot pHash distance — flags "stuck on same screen"
        try:
            cur_phash = self._phash(self.save_folder / image_file)
            if cur_phash is not None and self._prev_phash is not None:
                dist = bin(cur_phash ^ self._prev_phash).count("1")
                delta["phash_distance"] = dist
                delta["stuck_screen"]   = dist <= 2   # ~identical
            self._prev_phash = cur_phash
        except Exception:
            pass
        return delta

    def _log_bytes_total(self) -> int:
        """Total bytes across the profile's log sources — a cheap liveness probe
        (a couple of getsize calls). Never raises."""
        total = 0
        try:
            from connectors import log_sources as _ls
            srcs = _ls.effective_sources(self.profile)
        except Exception:
            srcs = []
        for s in srcs:
            try:
                total += os.path.getsize(os.path.expanduser(s.get("path") or ""))
            except Exception:
                continue
        return total

    def _maybe_flag_screen_stuck(self, frame: SnapshotFrame) -> None:
        """Track consecutive near-identical frames; on threshold, write the
        anomaly into frame.anomaly so AgentVision surfaces it everywhere.

        A STATIC SCREEN IS NOT AN ANOMALY BY ITSELF. Verified against a real
        emulator sitting on its title menu: 5 identical frames is only ~2.5s at
        a 0.5s interval, so every menu, pause screen and modal dialog was being
        flagged `screen_stuck` at medium severity — which then cost 25 health
        points and produced hang alerts for a perfectly healthy program. A real
        stall is static AND quiet, so the log sources are checked for growth
        before flagging: if the target is still writing output it is idle, not
        stuck, and no anomaly is raised (the visual engine still records a
        `screen_idle` event, so the information is not lost)."""
        delta = frame.delta_from_prev or {}
        if delta.get("stuck_screen"):
            if getattr(self, "_stuck_streak", 0) == 0:
                self._stuck_log_bytes = self._log_bytes_total()
            self._stuck_streak = getattr(self, "_stuck_streak", 0) + 1
        else:
            self._stuck_streak = 0
            self._stuck_log_bytes = None
        if self._stuck_streak >= 5:
            grew = 0
            try:
                base = getattr(self, "_stuck_log_bytes", None)
                if base is not None:
                    grew = max(0, self._log_bytes_total() - base)
            except Exception:
                grew = 0
            if grew > 0:
                return          # alive and idle — not an anomaly
            try:
                from shared.schema.snapshot_schema import AnomalyInfo
                frame.anomaly = AnomalyInfo(
                    detected=True,
                    severity="medium",
                    type="screen_stuck",
                    description=(f"Screen unchanged for {self._stuck_streak} "
                                 f"consecutive frames "
                                 f"(phash_distance={delta.get('phash_distance')}) "
                                 f"AND no new log output — the program may be "
                                 f"stalled, not merely idle"),
                )
            except Exception:
                pass

    def _phash(self, image_path: Path) -> Optional[int]:
        """Cheap 8x8 dHash without external deps. Returns None on failure."""
        try:
            from PIL import Image
            img = Image.open(image_path).convert("L").resize((9, 8), Image.LANCZOS)
            px  = list(img.getdata())
            bits = 0
            i = 0
            for row in range(8):
                for col in range(8):
                    a = px[row*9 + col]
                    b = px[row*9 + col + 1]
                    bits = (bits << 1) | (1 if a > b else 0)
                    i += 1
            return bits
        except Exception:
            return None

    def _evaluate(self, frame: SnapshotFrame) -> AgentEval:
        ev = AgentEval()
        has_error = frame.error is not None
        running   = frame.program.running

        if not running:
            ev.confidence  = 0.3
            ev.next_action = "Program is not running. Check if it crashed — see log_at_capture for last lines."
        elif has_error:
            ev.confidence      = 0.2
            ev.needs_rollback  = "CRITICAL" in (frame.program.last_error_block or "").upper()
            ev.what_went_wrong = frame.program.last_error
            ev.next_action     = (
                f"Error detected: {frame.program.last_error[:100]}. "
                "Review error.stack_trace and program.log_at_capture for context.")
        else:
            ev.confidence  = 0.85
            ev.next_action = "Program running normally. Review program.stats and log_at_capture for activity."

        return ev
