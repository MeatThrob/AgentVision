"""
AgentVision Bridge Server
--------------------------
Automatically captures screenshots of the connected program window the moment
it starts running, enriches every frame with live log/stats/git data, burns an
overlay, and writes a JSON sidecar alongside each PNG — all as one atomic op.
SnapBurst is no longer required; capture is driven entirely by this server.
"""
from __future__ import annotations
import sys
import os
import json
import logging
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from flask import Flask, request, jsonify, abort, make_response

from context_collector import ContextCollector
from utils.overlay_renderer import burn_overlay
from modules.activity_log import get_logger
from modules.failure_explainer import run_tests
from modules.codebase_map import build_map
from connectors.program_connector import (
    ProgramProfile, load_profiles, save_profiles, BUILTIN_PROFILES,
    profile_output_folder, resolve_action_log_path
)
from connectors import log_sources as _log_sources
from connectors import log_adapters as _log_adapters
from utils import retention as _ret
from shared.schema.snapshot_schema import SnapshotFrame
from shared import clock


def _safe_size(path: str) -> int:
    """Return file size in bytes, 0 if missing — used for capture-time offset snapshots."""
    try:
        return os.path.getsize(path) if path else 0
    except Exception:
        return 0

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False


# ── Uniform JSON errors ────────────────────────────────────────────────────────
# Every route must return clean, self-describing JSON — never Flask's default
# HTML error page — so an AI consumer can always json.loads() the response. These
# handlers convert aborts (404/400/…) and any uncaught exception into a JSON body
# with a stable shape: {"error", "status", "path"}.

def _json_error(status: int, message: str):
    from flask import request as _rq
    try:
        path = _rq.path
    except Exception:
        path = ""
    return jsonify({"error": message, "status": status, "path": path}), status


@app.errorhandler(400)
def _err_400(e):
    return _json_error(400, getattr(e, "description", "bad request"))


@app.errorhandler(404)
def _err_404(e):
    return _json_error(404, getattr(e, "description", "not found"))


@app.errorhandler(405)
def _err_405(e):
    return _json_error(405, getattr(e, "description", "method not allowed"))


@app.errorhandler(500)
def _err_500(e):
    return _json_error(500, "internal server error")


@app.errorhandler(Exception)
def _err_uncaught(e):
    # Last-resort guard: the bridge must NEVER hand back a non-JSON 500 or crash
    # the request thread on bad input. Let real HTTPExceptions keep their status.
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return _json_error(e.code or 500, getattr(e, "description", str(e)))
    try:
        _err(f"uncaught route exception: {e}", e)
    except Exception:
        pass
    return _json_error(500, f"{type(e).__name__}: {e}")


# ── AgentVision debug log ─────────────────────────────────────────────────────
_AV_ROOT    = Path(__file__).parent.parent.parent
_DEBUG_LOG  = _AV_ROOT / "agentvision_debug.log"

logging.basicConfig(
    filename=str(_DEBUG_LOG),
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_log = logging.getLogger("agentvision")

def _dbg(msg: str):
    _log.debug(msg)
    print(msg, flush=True)

def _info(msg: str):
    _log.info(msg)
    print(msg, flush=True)

def _warn(msg: str):
    _log.warning(msg)
    print(f"[WARN] {msg}", flush=True)

def _err(msg: str, exc: Exception | None = None):
    if exc:
        _log.error(msg, exc_info=exc)
    else:
        _log.error(msg)
    print(f"[ERROR] {msg}", flush=True)


# ── Global state ──────────────────────────────────────────────────────────────

_collector: ContextCollector | None = None
_frames: dict[int, dict]            = {}
_latest_frame: dict | None          = None
#: RLock, not Lock: a frame lookup may lazily parse a sidecar (_frame_or_load),
#: which needs the lock itself and is called from inside existing `with _lock`
#: blocks. A plain Lock would deadlock the bridge on the first access to any
#: frame that startup chose not to parse. RLock is a drop-in superset here.
_lock = threading.RLock()

#: Serialises the per-request override of utils.ui_tree.MAX_NODES, which is a
#: module global. /ui/tree sets it, captures, and restores it in a finally; two
#: concurrent requests with different caps would otherwise interleave.
_ui_tree_lock = threading.Lock()

# ── Visual-change state (the token-economics engine) ──────────────────────────
# `_grids` holds each frame's 16x16 mean-luma signature IN MEMORY ONLY. It is
# deliberately NOT written into the frame JSON: the sidecar is what the agent
# reads, and 256 bytes of signature per frame would be pure token waste. It is
# recomputable from the PNG on demand (visual_engine.grid_for_image).
_grids: dict[int, bytes]      = {}
_visual_prev: dict            = {"seq": None, "dhash": "", "ts_ms": 0.0}
_visual_events: list[dict]    = []      # auto-detected freeze/blank/layout/error
_visual_stats: dict           = {       # for /token_report — capture-side facts
    "frames_analyzed":   0,
    "frames_unchanged":  0,             # change_score below MIN_CHANGE
    "frames_changed":    0,
    "unique_dhashes":    set(),
    "analysis_ms_total": 0.0,
    "analysis_ms_max":   0.0,
    "ocr_scans":         0,
    "last_ocr_scan_ms":  0.0,
}
# Frames the AGENT actually pulled, so the token report can compare
# "captured for free" vs "paid for".
_agent_reads: dict            = {"frame_json": 0, "region": 0, "full_frame": 0,
                                 "thumbnail": 0, "visual_changes": 0,
                                 "seqs_full": set(), "seqs_json": set()}
# Throttle for the optional OCR error-text scan on the capture path.
VISUAL_OCR_SCAN     = os.environ.get("AGENTVISION_VISUAL_OCR_SCAN", "1") != "0"
VISUAL_OCR_MIN_GAP  = float(os.environ.get("AGENTVISION_VISUAL_OCR_GAP_MS", "2000"))
VISUAL_EVENT_CAP    = int(os.environ.get("AGENTVISION_VISUAL_EVENT_CAP", "500"))

# ── FLIGHT RECORDER ───────────────────────────────────────────────────────────
# When a failure fires, atomically FREEZE the pre-error window (plus a short
# post-error tail) as an "incident" exempt from eviction. The crucial moment is
# therefore already saved BEFORE the agent asks for it.
#
# RETENTION IS NO LONGER A CLOCK. Deleting everything older than 60 s meant the
# pixels could expire inside a single agent reasoning turn — AgentVision would
# announce "error at frame 4213" and then delete 4213 before it could be
# fetched, so the shot was taken for nothing. Disk is now bounded by BYTES
# (utils/retention.py, default 5 GiB) and a frame is only evicted once it has
# been EXAMINED, or its hold has expired. RECORDER_WINDOW_S survives purely as
# the width of the pre-error window an incident freezes — not as a delete rule.
RECORDER_ENABLED   = os.environ.get("AGENTVISION_RECORDER", "1") != "0"
RECORDER_WINDOW_S  = float(os.environ.get("AGENTVISION_RECORDER_WINDOW_S", "60"))
RECORDER_TAIL_S    = float(os.environ.get("AGENTVISION_RECORDER_TAIL_S", "5"))
RECORDER_MAX_FRAMES = int(os.environ.get("AGENTVISION_RECORDER_MAX_FRAMES", "1200"))
RECORDER_INCIDENT_CAP = int(os.environ.get("AGENTVISION_RECORDER_INCIDENT_CAP", "25"))
#: Absolute frame ceiling — a backstop against unbounded dict growth if the
#: byte budget is disabled. 0 disables it; the byte budget is the real bound.
RECORDER_HARD_FRAME_CAP = int(os.environ.get(
    "AGENTVISION_RECORDER_HARD_FRAME_CAP", "20000"))
#: Visual-event kinds recorded per seq at capture time, so retention can tell a
#: failure-shaped frame from an ordinary one when it classifies the frame.
_event_kinds_by_seq: dict[int, list[str]] = {}
#: How many sidecars startup will actually PARSE (newest first). Retention keeps
#: frames until the byte budget bites rather than for 60s, so the retained set is
#: now tens of thousands of frames; parsing all of them measured at 14s per ~8.5k
#: and would push startup into minutes. Everything beyond this is ledger-accounted
#: and lazy-loaded on first access — see _frames_on_disk / _frame_or_load.
HYDRATE_PARSE_LIMIT = int(os.environ.get("AGENTVISION_HYDRATE_PARSE_LIMIT", "1500"))
#: seq -> sidecar path for frames present on disk but not parsed into _frames.
#: Both this and _frames hold ONLY the ACTIVE profile's frames: the key is a bare
#: sequence number, and sequence numbers are only unique within a profile.
_frames_on_disk: dict[int, str] = {}
#: (profile, seq) -> sidecar path, for every OTHER profile's frames on disk. They
#: are addressable and countable without competing for a key in the seq-keyed
#: index. Measured: two watched profiles both own a frame 1, so
#: one of the two used to be silently unreachable depending on dict order.
_foreign: dict[tuple[str, int], str] = {}
#: Foreign frames whose seq collides with an active-profile frame — the concrete
#: evidence that the namespace is per profile. Reported by /frames/collisions.
_shadowed: list[dict] = []
_incidents: list[dict] = []          # frozen pre-error windows
_pinned_seqs: set[int] = set()       # frames an incident owns — never pruned
_recorder_stats: dict = {"frames_pruned": 0, "bytes_pruned": 0,
                         "incidents_frozen": 0, "prunes_run": 0}

#: Escape hatch for the old behaviour. OFF by default: a frame must contain ONLY
#: the bridged program, so when a capture_app is named but has no window we skip
#: the frame rather than storing a screenshot of the whole desktop. Profiles with
#: NO capture_app still capture full screen — there, that IS the requested target.
ALLOW_FULLSCREEN_FALLBACK = os.environ.get(
    "AGENTVISION_ALLOW_FULLSCREEN_FALLBACK", "0") not in ("0", "", "false")

SAVE_FOLDER       = os.environ.get("AGENTVISION_SAVE_FOLDER", "snapshots")
_ACTIVE_FILE      = Path(__file__).resolve().parent.parent / "active_profile.txt"


def _load_persisted_active_profile() -> str:
    """AgentVision is a UNIVERSAL program — it must NOT hardcode any one target
    (previously defaulted to "a game bot"). On startup, honor the profile the user
    last selected (persisted in active_profile.txt); if none, fall back to the
    neutral "custom" profile, never a specific project. The env var
    AGENTVISION_ACTIVE_PROFILE overrides everything (useful for scripted runs)."""
    env = os.environ.get("AGENTVISION_ACTIVE_PROFILE", "").strip()
    if env:
        return env
    try:
        name = _ACTIVE_FILE.read_text().strip()
        if name:
            return name
    except Exception:
        pass
    return "custom"


_active_profile_name: str = _load_persisted_active_profile()


def _persist_active_profile(name: str) -> None:
    """Write the active profile name to disk so out-of-process consumers
    (like the input recorder daemon) can know which sink to target."""
    try:
        _ACTIVE_FILE.write_text(name)
    except Exception as e:
        _warn(f"could not persist active profile: {e}")


def _base_for(profile: "ProgramProfile") -> Path:
    """Single source of truth for where a profile's output lives.
    Always use profile.project_root when set — screenshots, JSON sidecars,
    and frame metadata go to <project_root>/agentvision/<profile_name>/.
    Falls back to SAVE_FOLDER (AgentVision's own snapshots/) only when
    project_root is blank — never into another profile's directory."""
    root = (profile.project_root or "").strip()
    if root:
        p = Path(root)
        if p.exists():
            return p
    # Fallback: AV's own snapshots dir (next to the bridge script)
    return Path(SAVE_FOLDER) if Path(SAVE_FOLDER).is_absolute() \
        else _AV_ROOT / SAVE_FOLDER
RUN_TESTS         = os.environ.get("AGENTVISION_RUN_TESTS", "false").lower() == "true"
CAPTURE_INTERVAL  = float(os.environ.get("AGENTVISION_INTERVAL", "1.0"))

# ── Capture-rate envelope (shots per second) ──────────────────────────────────
# The AI model must understand — and, crucially, ASK THE USER about — how many
# screenshots per second to take. These constants are the single source of truth
# for that envelope and are surfaced verbatim to the model via /capture/status,
# /status and /overview (and the MCP docstrings). See capture_rate_info().
#
#   interval_s  seconds between shots  (smaller = faster = more shots/sec)
#   fps         shots per second       (= 1 / interval_s)
#
# The bridge can drive from CAPTURE_MIN_INTERVAL (fastest) upward with no hard
# upper bound; CAPTURE_MAX_INTERVAL is the slowest *commonly useful* cadence we
# advertise. The GUI slider exposes a friendlier sub-range.
CAPTURE_MIN_INTERVAL = float(os.environ.get("AGENTVISION_MIN_INTERVAL", "0.1"))   # 10 fps
CAPTURE_MAX_INTERVAL = float(os.environ.get("AGENTVISION_MAX_INTERVAL", "10.0"))  # 0.1 fps
GUI_SLIDER_MIN, GUI_SLIDER_MAX = 0.1, 2.0   # what the desktop GUI slider offers


def _fps(interval_s: float) -> float:
    return round(1.0 / interval_s, 3) if interval_s > 0 else 0.0


def capture_rate_info(current_interval: float) -> dict:
    """The capture-rate envelope, described for a machine AND for the AI to act
    on. `guidance` is deliberately imperative: the model should present the
    range and ASK the user for a shots-per-second value at the start OR
    continuation of every project."""
    return {
        "current_interval_s":  round(current_interval, 3),
        "current_fps":         _fps(current_interval),
        "min_interval_s":      CAPTURE_MIN_INTERVAL,
        "max_interval_s":      CAPTURE_MAX_INTERVAL,
        "fastest_fps":         _fps(CAPTURE_MIN_INTERVAL),   # e.g. 10.0
        "slowest_fps":         _fps(CAPTURE_MAX_INTERVAL),   # e.g. 0.1
        "supported_fps_range": [_fps(CAPTURE_MAX_INTERVAL), _fps(CAPTURE_MIN_INTERVAL)],
        "gui_slider_fps_range": [_fps(GUI_SLIDER_MAX), _fps(GUI_SLIDER_MIN)],
        "set_via": {
            "start":        "POST /capture/start {interval}  (av_capture_start)",
            "change":       "PUT  /capture/interval {interval}  (av_capture_set_interval)",
            "stop":         "POST /capture/stop  (av_capture_stop)",
        },
        "guidance": (
            "Screenshot cadence is user-configurable. At the START or CONTINUATION "
            "of EVERY project, ASK THE USER how many screenshots per second they "
            f"want, and present the full supported range: {_fps(CAPTURE_MAX_INTERVAL)}"
            f"–{_fps(CAPTURE_MIN_INTERVAL)} shots/sec (interval "
            f"{CAPTURE_MIN_INTERVAL}s–{CAPTURE_MAX_INTERVAL}s). Faster = more shots/sec "
            "= finer visual detail but more frames to review; slower = fewer shots. "
            "Then apply it with av_capture_set_interval(interval=1/fps) — e.g. 4 "
            "shots/sec = interval 0.25. Every frame is time-aligned to the logs, so "
            "pick a rate that matches how fast the thing you're debugging changes. "
            "You do not have to ask in prose: call av_capture_start() or "
            "av_capture_set_interval() with NO interval and AgentVision puts the "
            "question to the user itself over MCP elicitation. Where the client "
            "cannot show a prompt, the response says so in `capture_rate_choice` "
            "rather than passing a default off as the user's decision."
        ),
    }


def _get_collector() -> ContextCollector:
    global _collector
    if _collector is None:
        profiles = load_profiles()
        profile  = profiles.get(_active_profile_name,
                                profiles.get("custom", ProgramProfile()))
        out_folder = profile_output_folder(profile, _base_for(profile))
        _info(f"Output folder for '{profile.display_name}': {out_folder}")
        _collector = ContextCollector(
            profile=profile,
            save_folder=str(out_folder),
            run_tests_each_frame=RUN_TESTS)
        get_logger().log(f"Bridge started — connected to: {profile.display_name}")
        _info(f"Collector ready. Profile: {profile.display_name}, "
              f"log: {profile.log_file}, stats: {profile.stats_folder}")
    return _collector


def _hydrate_frame_index() -> None:
    """Repopulate the frame index from on-disk sidecars at startup, so bookmarks
    and /frame/<seq> keep working across a bridge restart.

    ONLY the newest HYDRATE_PARSE_LIMIT sidecars are parsed. Parsing all of them
    measured at 14s for 8,487 frames — and since retention now keeps frames until
    the 5 GB budget is reached rather than for 60 seconds, that set grows to tens
    of thousands and startup would run into minutes. Older frames are still fully
    accounted for in the retention ledger (so the byte budget stays honest) and
    are registered in _frames_on_disk for LAZY parsing on first access, which is
    what keeps them addressable without paying for them up front.
    """
    global _frames, _latest_frame
    profiles = load_profiles()
    loaded = deferred = 0
    folders: set[Path] = set()
    group_cache: dict[str, dict[str, list[str]]] = {}
    _foreign.clear()
    _shadowed.clear()
    for prof_name, prof in profiles.items():
        # THE SEQ NAMESPACE IS PER PROFILE, and the index is keyed by seq alone.
        # Hydrating every profile into it meant two programs' frame 100 were the
        # same key: first writer won (`if seq not in _frames`), the other frame
        # became unreachable, and every derived read had to re-filter by profile
        # afterwards to undo the mixing. Measured on this machine: a game bot and
        # sharpemu both own a frame 1.
        #
        # So only the ACTIVE profile's frames enter the seq-keyed index. Other
        # profiles' frames are still admitted to the retention ledger (the byte
        # budget must cover every folder) and are recorded in `_foreign` so they
        # remain addressable and countable — but they can no longer shadow, or be
        # shadowed by, the frames of the program actually being debugged.
        mine = (prof_name == _active_profile_name)
        try:
            base = _base_for(prof)
            out_folder = profile_output_folder(prof, base)
            # Newest first: the recent past is what a debugger actually wants
            # loaded, and it is what the visual/diagnose routes read.
            sidecars = sorted(Path(out_folder).rglob("frame_*_frame.json"),
                              key=lambda p: p.name, reverse=True)
            for idx, json_path in enumerate(sidecars):
                try:
                    stem = json_path.stem.replace("_frame", "")
                    if idx >= HYDRATE_PARSE_LIMIT:
                        # Ledger-only: account for the bytes without parsing.
                        key = str(json_path.parent)
                        if key not in group_cache:
                            group_cache[key] = _ret.stem_groups(json_path.parent)
                        paths = group_cache[key].get(stem, [])
                        try:
                            seq_i = int(stem.split("_")[-1])
                        except Exception:
                            continue
                        with _lock:
                            if mine:
                                _frames_on_disk[seq_i] = str(json_path)
                            else:
                                _foreign[(prof_name, seq_i)] = str(json_path)
                                if seq_i in _frames or seq_i in _frames_on_disk:
                                    _shadowed.append(
                                        {"seq": seq_i, "profile": prof_name,
                                         "path": str(json_path)})
                        if not _ret.LEDGER.known(seq_i):
                            _ret.LEDGER.admit(
                                seq_i, json_path.stat().st_mtime * 1000.0, paths,
                                {}, folder=key, stem=stem)
                        deferred += 1
                        continue
                    data = json.loads(json_path.read_text(errors="replace"))
                    seq = int(data.get("sequence") or 0)
                    if not seq:
                        continue
                    img  = json_path.parent / f"{stem}.png"
                    thumb = json_path.parent / f"{stem}_thumb.png"
                    data["annotated_image"] = str(img) if img.exists() else ""
                    data["frame_image"]     = str(img) if img.exists() else ""
                    data["thumbnail_file"]  = str(thumb) if thumb.exists() else ""
                    data["json_sidecar"]    = str(json_path)
                    data["program_folder"]  = str(json_path.parent)
                    data["stem"]            = stem
                    fresh = False
                    with _lock:
                        if not mine:
                            # Another program's frame: addressable, never in the
                            # seq-keyed index. Still ledgered below for bytes.
                            _foreign[(prof_name, seq)] = str(json_path)
                            if seq in _frames or seq in _frames_on_disk:
                                _shadowed.append({"seq": seq, "profile": prof_name,
                                                  "path": str(json_path)})
                        # Don't overwrite live in-memory frames
                        elif seq not in _frames:
                            _frames[seq] = data
                            loaded += 1
                            fresh = True
                    # Re-admit to the retention ledger, otherwise a restart would
                    # leave every surviving frame unaccounted for and the byte
                    # budget would silently under-count what is on disk.
                    #
                    # THIS MUST NOT BE GATED ON `mine`. The orphan sweep below
                    # deletes every frame file whose stem the ledger does not
                    # know, so skipping admission for another profile's frames
                    # deletes them. That is not theoretical: gating this on
                    # `fresh` alone (which is only ever True for the active
                    # profile) destroyed the newest 1,500 frames — 7,501 files,
                    # 270 MB — of a real capture the first time it ran.
                    if (fresh or not mine) and not _ret.LEDGER.known(seq):
                        try:
                            # Batched stem map: a per-frame glob here is
                            # O(frames x files) and stalled startup for minutes
                            # once a folder held ~8k frames / ~33k files.
                            key = str(json_path.parent)
                            if key not in group_cache:
                                group_cache[key] = _ret.stem_groups(json_path.parent)
                            # Same one-fault-one-frame rule as live capture:
                            # sidecars persist the error, so without this every
                            # restart re-queued hundreds of historical frames
                            # carrying the SAME fault (measured: 378).
                            h_facts = _frame_facts(
                                data, (data.get("capture_meta") or {}).get("visual")
                                or {}, seq)
                            h_facts = _dedupe_error_facts(h_facts, data, seq)
                            _ret.LEDGER.admit(
                                seq, float(data.get("timestamp_ms") or 0.0),
                                group_cache[key].get(stem, []), h_facts,
                                folder=str(json_path.parent), stem=stem)
                        except Exception:
                            pass
                except Exception:
                    continue
            folders.add(Path(out_folder))
        except Exception:
            continue

    # ── Orphan sweep ──────────────────────────────────────────────────────────
    # The old pruner deleted a frame's PNG and sidecar but left its
    # *_annotated.png and .diff siblings behind; with the sidecar gone this
    # hydrator could no longer even see them, so nothing ever removed them —
    # 4.7 GB accumulated that way in a single project. Sweeping at startup both
    # reclaims that history and makes the byte budget honest.
    #
    # _archive/ IS EXCLUDED. The sweep's protection is "a file whose
    # <stem>_frame.json sits beside it is a frame, not an orphan" — and that
    # guard is structurally vacuous inside _archive/, because archive_frame only
    # ever writes <stem>.webp/.jpg and never a sidecar. So every archived file
    # there runs on ledger-only authority: exactly the authority that deleted
    # 1,500 frames. And an archive copy is, by construction, the ONLY surviving
    # pixels of a frame whose PNG was already reclaimed — the sweep would delete
    # precisely the frames the planner judged most worth keeping (failure-shaped
    # or interest >= 0.6). Route, verified end to end by the audit: capture until
    # the budget bites so webps get written, click Clear Data (which deletes the
    # sidecars but cannot see into _archive/), restart — and with an empty ledger
    # every webp qualifies as an orphan.
    #
    # The archive is bounded by its own budget share instead (see
    # ARCHIVE_BUDGET_FRACTION and _measure_archive_bytes below), which is a
    # sizing question, not a reason to destroy irreplaceable pixels.
    live = {r for r in _ret.LEDGER.live_stems() if r}
    for folder in folders:
        for sub in ({folder} | {p for p in folder.rglob("*") if p.is_dir()}):
            if "_archive" in sub.parts:
                _dbg(f"orphan sweep skips the archive: {sub}")
                continue
            try:
                res = _ret.sweep_orphans(sub, live, min_age_s=300.0)
                if res.get("removed"):
                    _ret.LEDGER.note_orphan_sweep(res)
                    _info(f"Orphan sweep: removed {res['removed']} untracked "
                          f"frame file(s) ({_ret._human(res['bytes_freed'])}) "
                          f"from {sub}")
                if res.get("refused"):
                    _warn(f"Orphan sweep REFUSED in {sub}: {res.get('reason')}")
                if res.get("capped_remaining"):
                    _warn(f"Orphan sweep capped in {sub}: "
                          f"{res['capped_remaining']} file(s) deferred to the "
                          f"next sweep ({res.get('reason')})")
            except Exception as exc:
                _dbg(f"orphan sweep failed for {sub}: {exc}")

    # The archive's size is in-memory state that resets to 0 on every restart,
    # so without this the 10%-of-budget cap is only ever enforced within one
    # process and the archive grows without limit across restarts. Measure it off
    # the disk instead — the same principle as everything else in this pass.
    try:
        _measure_archive_bytes()
    except Exception as exc:
        _dbg(f"archive measurement failed: {exc}")

    if loaded:
        # Restore _latest_frame to the highest seq we found
        with _lock:
            top = max(_frames.keys()) if _frames else None
            if top is not None:
                _latest_frame = _frames[top]
        _info(f"Hydrated {loaded} frame sidecar(s) from disk")


# ── Auto-capture engine ───────────────────────────────────────────────────────

class AutoCaptureEngine:
    """
    Background thread that watches the connected program.
    Once the program starts running (is_running() == True), it begins taking
    screenshots at `interval` seconds, enriches each with collect(), burns the
    overlay, and writes PNG + JSON sidecar.  Pauses automatically when the
    program stops; resumes when it restarts.
    """

    def __init__(self):
        self._thread:   threading.Thread | None = None
        self._stop_evt  = threading.Event()
        self.interval:  float = CAPTURE_INTERVAL
        self.running:   bool  = False   # engine loop active
        self.capturing: bool  = False   # currently taking shots
        self.frame_count: int = 0
        self.last_error: str  = ""
        # ── Capture-health telemetry (surfaced via /capture/status) ─────────
        self.last_frame_ms:      float = 0.0   # shutter ms of most recent frame
        self.last_latency_ms:    float = 0.0   # how long that shutter took
        self.blank_frame_count:  int   = 0     # frames that looked black/blank
        #: HARD capture failures. `last_error` was already recorded but the
        #: health block never exposed it, so 14 consecutive
        #: "screencapture failed: could not create image from window" errors
        #: reported as skipped=0 / window_missing=False / no error at all —
        #: a capture that produced almost nothing looked healthy.
        self.frame_error_count:  int   = 0
        self.window_missing:     bool  = False # capture_app set but not found
        self.last_warning:       str   = ""    # most recent non-fatal issue
        self.skipped_no_window:  int   = 0     # frames NOT taken (no window)
        #: Frames taken while the watchdog's most recent check said the process
        #: was alive but the program had written NOTHING for longer than
        #: SOURCE_STALE_S. Capture is deliberately NOT stopped on that — an idle
        #: GUI is legitimately silent — but 12,921 frames were once stored for a
        #: program that had exited, and a live count is what makes that visible
        #: while it is happening rather than a day later.
        self.frames_while_silent: int  = 0

    def resume_counter_from_disk(self) -> int:
        """Continue numbering ABOVE whatever already exists on disk.

        The counter used to reset to 0 on every bridge restart, so a new run
        began writing frame_00001.png over the previous run's frame_00001.png —
        silently destroying retained history and leaving the ledger pointing at
        files whose contents belong to a different run. That was harmless while
        frames only lived 60 seconds (nothing old survived a restart), but
        retention now keeps them until the byte budget bites, so a restart
        overwrote hours of real evidence. Seeding from the highest seq present
        makes numbering monotonic across restarts.
        """
        top = 0
        with _lock:
            if _frames:
                top = max(top, max(_frames.keys()))
            if _frames_on_disk:
                top = max(top, max(_frames_on_disk.keys()))
        # The FILENAMES are authoritative for "what could be overwritten".
        # Sidecar `sequence` fields cannot be trusted for this: frames written
        # before the two counters were unified carry an internal number that
        # disagrees with their own filename, so trusting them picked a resume
        # point BELOW files that already existed.
        try:
            for prof in load_profiles().values():
                folder = Path(profile_output_folder(prof, _base_for(prof)))
                for stem in _ret.stem_groups(folder):
                    try:
                        top = max(top, int(stem.rsplit("_", 1)[-1]))
                    except Exception:
                        continue
                # stem_groups is deliberately non-recursive (it is O(files) and
                # runs per frame), so it cannot see _archive/. The compressed
                # copies in there are named frame_NNNNN.webp — the SAME frame
                # numbering — and for an already-evicted frame they are the only
                # surviving pixels. Left out of the high-water mark, a restarted
                # counter re-numbers from 1 and the next eviction re-archives
                # frame_00001.webp straight over the older run's copy. One extra
                # iterdir of one directory closes that.
                try:
                    arch = folder / "_archive"
                    if arch.is_dir():
                        for p in arch.iterdir():
                            m = _ret._FRAME_FILE_RE.match(p.name)
                            if m:
                                try:
                                    top = max(top, int(m.group(0).rsplit("_", 1)[-1]))
                                except Exception:
                                    continue
                except Exception:
                    pass
        except Exception as exc:
            _dbg(f"filename high-water scan failed: {exc}")
        if top > self.frame_count:
            self.frame_count = top
            _info(f"Frame numbering resumes at #{top + 1} "
                  f"(existing frames on disk are never overwritten)")
        return self.frame_count

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="av-autocapture")
        self._thread.start()
        _info("AutoCaptureEngine started")

    def stop(self):
        self._stop_evt.set()
        self.running   = False
        self.capturing = False
        _info("AutoCaptureEngine stop requested")

    def set_interval(self, seconds: float):
        # Clamp to the fastest supported cadence; no hard upper bound (the user
        # may legitimately want a very slow poll). See capture_rate_info().
        self.interval = max(CAPTURE_MIN_INTERVAL, float(seconds))

    # ── Internal loop ─────────────────────────────────────────────────────────

    def _loop(self):
        _info("AutoCapture loop running — waiting for program to start…")
        was_running = False
        while not self._stop_evt.is_set():
            collector = _get_collector()
            # Evidence, not a bare bool: the reason the match was made is logged
            # at the transition, so a capture run started by a bystander process
            # (a shell or pager that merely mentioned the project — see
            # ProgramDataReader._match_reason) is visible in the log that started
            # it, instead of only in 12,921 frames of a dead window.
            _lev = collector._reader.running_evidence()
            prog_running = bool(_lev.get("running"))

            if prog_running and not was_running:
                _info(f"Program '{collector.profile.display_name}' detected — "
                      f"starting capture  [matched pid={_lev.get('pid')} "
                      f"{_lev.get('process_name')!r} by={_lev.get('matched_by')}]")
                get_logger().log(f"Auto-capture started for {collector.profile.display_name}")
                self.capturing = True
            elif not prog_running and was_running:
                _info(f"Program stopped — pausing capture")
                get_logger().log("Program stopped — auto-capture paused")
                self.capturing = False

            was_running = prog_running

            if prog_running and self.capturing:
                t0 = time.monotonic()
                try:
                    self._take_frame(collector)
                    # Cheap cross-check, from the watchdog's cached verdict (no
                    # extra scan): the process is alive but the program has gone
                    # quiet. Counted, not acted on — see frames_while_silent.
                    _st = _observer_state.get(_active_profile_name or "default") or {}
                    _sil = _st.get("program_silent_s")
                    if _sil is not None and _sil > SOURCE_STALE_S:
                        self.frames_while_silent += 1
                        if self.frames_while_silent in (1, 100, 1000, 10000):
                            self.last_warning = (
                                f"{self.frames_while_silent} frame(s) captured "
                                f"while the program has written nothing for "
                                f"{_human_age(_sil)} — these frames may show a "
                                f"window that is no longer being updated")
                            _warn(self.last_warning)
                except Exception as exc:
                    self.last_error = str(exc)
                    self.frame_error_count += 1
                    _err(f"AutoCapture frame error: {exc}", exc)
                elapsed = time.monotonic() - t0
                sleep_for = max(0.0, self.interval - elapsed)
                self._stop_evt.wait(sleep_for)
            else:
                # Poll every second waiting for program to start
                self._stop_evt.wait(1.0)

        self.running   = False
        self.capturing = False
        _info("AutoCapture loop exited")

    def _take_frame(self, collector: ContextCollector):
        """Capture screenshot → collect() → overlay → JSON sidecar.

        TIME-ALIGNMENT (the whole point): the frame timestamp AND the log byte
        offsets are snapshotted in the SAME instant, immediately BEFORE the
        shutter. That way the log window bounded by those offsets reflects
        exactly what had been logged as of the shutter — no line that arrived
        during the (possibly ~100 ms) capture sneaks in with a ts_ms greater
        than the frame's own timestamp. capture_end_ms records when the pixels
        finished, so the AI knows the image is valid across
        [shutter_ms, capture_end_ms]."""
        from utils import platform_shim
        program_folder = Path(collector.save_folder)
        seq_hint       = self.frame_count + 1
        filename       = f"frame_{seq_hint:05d}.png"
        image_path     = program_folder / filename

        # ── Screenshot targeting ────────────────────────────────────────────
        # THE FRAME MUST CONTAIN ONLY THE BRIDGED PROGRAM.
        # Priority 1: capture by window ID. On macOS `screencapture -l<wid>`
        #             reads the window's backing store, so it works regardless
        #             of size, position, occlusion, fullscreen — and MEASURED
        #             here, even when the window is fully minimized to the Dock
        #             (verified: a minimized window captured its real content,
        #             identical to the un-minimized capture). On Windows it is a
        #             region grab of the window bounds, so it is
        #             occlusion-sensitive and cannot see a minimized window.
        # Priority 2: custom crop rect (manual x,y,w,h override).
        # Priority 3: full screen — ONLY when no capture_app was named.
        wid  = collector._reader.get_window_id()
        crop = collector._reader.get_capture_crop()

        wants_window = bool((getattr(collector.profile, "capture_app", "") or "").strip())
        window_found = wid is not None
        self.window_missing = wants_window and not window_found and not crop
        if self.window_missing:
            # DO NOT screenshot the desktop. The old behaviour warned and then
            # captured the full screen anyway, which produced 2560x1440 frames of
            # the user's entire desktop — every other app, browser tabs and all —
            # stored to disk, fed to the agent, and 18x the bytes of a real window
            # frame. A frame of the wrong program is worse than no frame: it
            # leaks unrelated screen contents, pollutes visual-change detection,
            # and makes the agent reason about pixels that are not the program.
            # Skip the frame instead and say so.
            self.last_warning = (
                f"capture_app '{collector.profile.capture_app}' has no window "
                "right now — SKIPPING the frame rather than screenshotting the "
                "desktop. Is the program running? (A minimized or occluded "
                "window is fine and still captures on macOS.)")
            if not ALLOW_FULLSCREEN_FALLBACK:
                self.skipped_no_window += 1
                _dbg(f"  frame skipped: no '{collector.profile.capture_app}' window")
                return
            self.last_warning += " (AGENTVISION_ALLOW_FULLSCREEN_FALLBACK=1 set)"

        # ── Shutter: stamp time AND snapshot log offsets together ───────────
        # Resolved with the SAME rule as _active_action_log_path: on a
        # modern-bridged profile the JSONL sink lives in log_sources and
        # `action_log_file` is empty, so the old one-field read pinned
        # profile_action_log="" and action_log_offset=0 on every frame — and a
        # 0 offset means "no cap" to the readers, so records written AFTER the
        # shutter were served as this frame's context. That silently voids the
        # frame↔log alignment guarantee (and av_frame_alignment then reports
        # phantom leaks for frames that were captured perfectly).
        action_log_path = resolve_action_log_path(collector.profile)
        log_path        = getattr(collector.profile, "log_file", "") or ""
        capture_iso, capture_ms = clock.now()
        offsets = {
            "action_log_offset": _safe_size(action_log_path),
            "log_offset":        _safe_size(log_path),
            "_action_log_path":  action_log_path,
        }
        platform_shim.capture_frame(str(image_path), wid=wid, crop=crop)
        capture_end_ms = clock.now_ms()
        if not image_path.exists():
            raise RuntimeError(f"capture produced no file at {image_path}")

        # ── Capture health + VISUAL ANALYSIS off ONE image decode ───────────
        # Both are computed on the RAW capture, BEFORE the overlay is burned:
        # the overlay contains a live timestamp, so hashing the overlaid PNG
        # would make every frame "different" and destroy the whole point.
        # Decoding is ~90% of the cost, so sharing it makes the perceptual hash
        # + diff nearly free on the 10 fps path (measured: ~1 ms marginal at
        # 1080p/1440p, ~10 ms at 4K, vs a 100 ms budget at 10 shots/sec).
        visual: dict = {"assessed": False, "reason": "not run"}
        health: dict = {"assessed": False}
        try:
            from utils import visual_engine
            with _lock:
                prev_grid  = _grids.get(_visual_prev.get("seq")) if _visual_prev.get("seq") else None
                prev_dhash = _visual_prev.get("dhash") or ""
            both = visual_engine.analyze_with_health(str(image_path),
                                                    prev_grid=prev_grid,
                                                    prev_dhash=prev_dhash)
            health = both.get("health") or {"assessed": False}
            visual = both.get("visual") or {"assessed": False}
        except Exception as exc:
            _warn(f"visual analysis failed: {exc}")
        if not health.get("assessed"):
            # Preserve exact prior behaviour when visual analysis is unavailable.
            health = platform_shim.image_health(str(image_path))
        is_blank = bool(health.get("is_blank"))
        if is_blank:
            self.blank_frame_count += 1
            self.last_warning = (
                f"frame #{seq_hint} looks blank/black (mean={health.get('mean')}, "
                f"stddev={health.get('stddev')}) — window may be minimized, "
                "occluded, not yet painted, or capture-blocked.")
            _warn(self.last_warning)

        latency_ms = round(capture_end_ms - capture_ms, 1)
        self.last_frame_ms   = capture_ms
        self.last_latency_ms = latency_ms

        capture_meta = {
            "shutter_ms":         capture_ms,
            "capture_end_ms":     capture_end_ms,
            "capture_latency_ms": latency_ms,
            "action_log_offset":  offsets["action_log_offset"],
            "log_offset":         offsets["log_offset"],
            "window_found":       window_found,
            "capture_target":     ("window" if window_found else
                                   "crop" if crop else "fullscreen"),
            "black_frame":        is_blank,
            "image_health":       health,
            "capture_backend":    platform_shim.capture_backend_name(),
            "rate": {"interval_s": round(self.interval, 3),
                     "shots_per_second": _fps(self.interval)},
            "note": (
                "image is valid across [shutter_ms, capture_end_ms]; logs are "
                "bounded to bytes <= *_offset as of shutter_ms, so no record "
                "with ts_ms > shutter_ms leaks into this frame's context."),
        }

        # The 16x16 signature stays out of the persisted frame (token waste).
        grid_bytes = visual.pop("_grid", b"") if isinstance(visual, dict) else b""
        if isinstance(visual, dict) and visual.get("assessed"):
            capture_meta["visual"] = visual

        # ── Collect context ─────────────────────────────────────────────────
        # Pin the frame number to the SAME counter that named the PNG, so
        # frame.sequence and frame_<seq>.png can never drift apart.
        frame = collector.collect(image_file=filename,
                                  capture_iso=capture_iso,
                                  capture_ms=capture_ms,
                                  offsets=offsets,
                                  capture_meta=capture_meta,
                                  sequence=seq_hint)
        self.frame_count += 1

        # Bridge↔program correlation: write the frame seq to a tiny file the
        # bridged program reads (cached, refresh ~1s) and stamps onto every
        # JSONL record. Fully optional — no-op if the program doesn't read it.
        if action_log_path:
            try:
                seq_file = Path(action_log_path).parent / ".av_frame_seq"
                seq_file.write_text(str(frame.sequence))
            except Exception as _e:
                _dbg(f"frame_seq write failed: {_e}")

        # ── Overlay ───────────────────────────────────────────────────────────
        try:
            from utils.overlay_renderer import burn_overlay
            burn_overlay(str(image_path), frame)
        except Exception as exc:
            _warn(f"Overlay burn failed: {exc}")

        # ── JSON sidecar (same folder, same stem) ─────────────────────────────
        stem      = Path(filename).stem
        json_path = program_folder / f"{stem}_frame.json"
        json_path.write_text(frame.to_json())

        # ── Persisted thumbnail ───────────────────────────────────────────────
        # Thumbnails used to be rendered on demand FROM the full PNG, so they
        # died with it and the JSON history went blind the moment a frame was
        # reclaimed. Writing one now, while the pixels still exist, is what lets
        # a whole day of capture stay visually reviewable for a few MB.
        thumb_path = program_folder / f"{stem}_thumb.png"
        try:
            _ret.write_thumbnail(str(image_path), str(thumb_path))
        except Exception as exc:
            _dbg(f"thumbnail write failed: {exc}")

        # ── Update global latest frame ────────────────────────────────────────
        result_dict = json.loads(frame.to_json())
        # NOTE: "annotated_image" is a historical misnomer — it points at the
        # ORIGINAL frame PNG, which is what every read route serves. The overlay
        # is burned into a separate *_annotated.png sibling. Retention deletes by
        # file GROUP rather than by these two keys, which is why the annotated
        # copies and .diff files no longer accumulate unreferenced.
        result_dict["annotated_image"] = str(image_path)
        result_dict["frame_image"]     = str(image_path)
        result_dict["thumbnail_file"]  = str(thumb_path) if thumb_path.exists() else ""
        result_dict["json_sidecar"]    = str(json_path)
        result_dict["program_folder"]  = str(program_folder)

        with _lock:
            _frames[frame.sequence] = result_dict
            global _latest_frame
            _latest_frame = result_dict
            if grid_bytes:
                _grids[frame.sequence] = grid_bytes
                _visual_prev["seq"]    = frame.sequence
                _visual_prev["dhash"]  = visual.get("dhash") or ""
                _visual_prev["ts_ms"]  = capture_ms
            _record_visual_stats(visual)

        # Auto-bookmark the moments that matter (freeze / blank / big layout
        # change / on-screen error text) so the agent jumps straight there
        # instead of scanning frames. Failure signatures also freeze the flight
        # recorder's pre-error window.
        try:
            _detect_visual_events(frame.sequence, capture_ms, visual,
                                  str(image_path))
        except Exception as exc:
            _dbg(f"visual event detection failed: {exc}")

        # A structured error on this frame is also an incident trigger.
        try:
            fe = result_dict.get("error") or {}
            if isinstance(fe, dict) and (fe.get("message") or fe.get("exception_type")):
                _freeze_incident("error", frame.sequence, capture_ms,
                                 f"{fe.get('exception_type') or 'error'}: "
                                 f"{(fe.get('message') or '')[:120]}",
                                 extra={"fingerprint": fe.get("fingerprint")})
        except Exception as exc:
            _dbg(f"error-incident freeze failed: {exc}")

        # ── RETENTION: admit the frame and decide whether it needs eyes ────────
        # Runs AFTER visual-event detection and the error freeze, because those
        # are what tell us whether this frame is failure-shaped — and that is
        # exactly what decides if the agent must be shown it before it expires.
        try:
            _admit_to_ledger(frame.sequence, capture_ms, result_dict, visual,
                             program_folder, stem)
        except Exception as exc:
            _warn(f"retention admit failed: {exc}")

        # FLIGHT RECORDER: enforce the byte budget, evicting only frames that
        # have already been examined (or whose hold expired). Deliberately NOT
        # age-based — see the RETENTION note at the top of this module.
        try:
            _recorder_prune(capture_ms)
        except Exception as exc:
            _warn(f"recorder prune failed: {exc}")

        _dbg(f"  AutoCapture frame #{frame.sequence}  "
             f"running={frame.program.running}  "
             f"log_lines={len(frame.program.log_at_capture)}  "
             f"stats={list(frame.program.stats.keys())[:3]}")


_auto_engine = AutoCaptureEngine()


# ── Visual-change bookkeeping + auto-detected visual events ───────────────────
# Must be called with _lock held.
def _record_visual_stats(visual: dict) -> None:
    if not isinstance(visual, dict) or not visual.get("assessed"):
        return
    st = _visual_stats
    st["frames_analyzed"] += 1
    cs = visual.get("change_score")
    if cs is None:
        pass                                   # first frame: nothing to compare
    elif cs < visual_min_change():
        st["frames_unchanged"] += 1
    else:
        st["frames_changed"] += 1
    dh = visual.get("dhash") or ""
    if dh:
        st["unique_dhashes"].add(dh)
    ms = float(visual.get("cost_ms") or 0.0)
    st["analysis_ms_total"] += ms
    if ms > st["analysis_ms_max"]:
        st["analysis_ms_max"] = ms


def visual_min_change() -> float:
    from utils import visual_engine
    return visual_engine.MIN_CHANGE


# Which visual events are worth freezing an incident for. A layout change is
# normal program behaviour; a freeze, a blank screen or on-screen error text is
# a failure signature.
_INCIDENT_EVENT_TYPES = {"screen_frozen", "blank_screen", "on_screen_error"}


def _push_visual_event(ev: dict) -> None:
    """Append a visual event (auto-bookmark), newest last, bounded.

    Failure-signature events also FREEZE the flight recorder's pre-error window
    so the moment survives pruning."""
    # Coalesce FIRST, freeze second. The freeze used to run before this check,
    # so every per-frame push froze ANOTHER incident even though the events
    # themselves collapsed correctly — one idle title menu produced 25 identical
    # "possible hang" incidents and re-alerted the agent every turn. Only a
    # genuinely NEW event is worth freezing a pre-error window for.
    with _lock:
        # Collapse a repeat of the same type on consecutive frames into one
        # event so a 30-second freeze is ONE bookmark, not 300.
        if _visual_events:
            last = _visual_events[-1]
            if (last.get("type") == ev.get("type")
                    and int(ev.get("seq", 0)) - int(last.get("seq_end", last.get("seq", 0))) <= 1):
                last["seq_end"]  = ev.get("seq")
                last["ts_end_ms"] = ev.get("ts_ms")
                last["frames"]   = int(last.get("frames", 1)) + 1
                for k in ("detail", "change_score", "on_screen_text",
                          "still_for_s", "log_bytes_since_static"):
                    if ev.get(k) is not None:
                        last[k] = ev[k]
                return
        ev.setdefault("seq_end", ev.get("seq"))
        ev.setdefault("ts_end_ms", ev.get("ts_ms"))
        ev.setdefault("frames", 1)
        ev["id"] = f"vis-{ev.get('type')}-{ev.get('seq')}"
        _visual_events.append(ev)
        if len(_visual_events) > VISUAL_EVENT_CAP:
            del _visual_events[:len(_visual_events) - VISUAL_EVENT_CAP]
        # Retention classifies frames by what happened on them. Only a GENUINELY
        # NEW event is recorded: the continuation frames of a 300-frame freeze
        # are already preserved by the incident pin, and pushing all 300 at the
        # agent is precisely what the coalescing above exists to prevent.
        try:
            _event_kinds_by_seq.setdefault(int(ev.get("seq") or 0), []).append(
                str(ev.get("type") or ""))
            if len(_event_kinds_by_seq) > VISUAL_EVENT_CAP * 4:
                for k in sorted(_event_kinds_by_seq)[:VISUAL_EVENT_CAP]:
                    _event_kinds_by_seq.pop(k, None)
        except Exception:
            pass

    if ev.get("type") in _INCIDENT_EVENT_TYPES:
        try:
            inc = _freeze_incident(ev["type"], int(ev.get("seq") or 0),
                                   float(ev.get("ts_ms") or 0),
                                   str(ev.get("detail") or ""),
                                   extra={"on_screen_text": ev.get("on_screen_text")}
                                   if ev.get("on_screen_text") else None)
            if inc:
                ev["incident_id"] = inc["id"]   # same dict object as in the list
        except Exception as exc:
            _dbg(f"incident freeze failed: {exc}")


def _log_bytes_total() -> int:
    """Total bytes across every log source the active profile watches.

    A cheap liveness signal for the static-screen detector: if this GROWS while
    the screen is frozen, the target is still running and merely idle (a menu, a
    paused game, a modal waiting on input). If it does NOT grow, the process has
    gone quiet as well as still — that is what a real hang looks like. Two or
    three os.path.getsize() calls, so it is safe on the 10 fps capture path.
    Never raises; returns 0 when nothing is resolvable."""
    total = 0
    try:
        from connectors import log_sources as _ls
        srcs = _ls.effective_sources(_collector.profile) if _collector else []
    except Exception:
        srcs = []
    for s in srcs:
        try:
            total += os.path.getsize(os.path.expanduser(s.get("path") or ""))
        except Exception:
            continue
    return total


def _detect_visual_events(seq: int, ts_ms: float, visual: dict,
                          image_path: str) -> None:
    """Turn the cheap visual signal into named events the agent can jump to.

    Four detectors, all threshold-configurable via env:
      * screen_frozen  — nothing changed for FREEZE_SECONDS (a hang)
      * blank_screen   — sudden black/blank frame
      * layout_change  — a large fraction of the screen repainted (new view/dialog)
      * on_screen_error — OCR found error/exception/traceback text (OPTIONAL:
                          only when tesseract is present; throttled so it can
                          never stall the capture cadence)"""
    from utils import visual_engine as ve
    if not isinstance(visual, dict) or not visual.get("assessed"):
        return
    cs = visual.get("change_score")
    struct = visual.get("structural") or {}

    # blank / black
    if struct.get("is_blank"):
        _push_visual_event({"type": "blank_screen", "seq": seq, "ts_ms": ts_ms,
                            "severity": "warning",
                            "detail": (f"frame looks blank/black "
                                       f"(mean_luma={struct.get('mean_luma')}, "
                                       f"contrast={struct.get('contrast')})"),
                            "change_score": cs,
                            "next": f"av_frame_json(seq={seq})"})

    # Static screen. TWO things this used to get wrong, both found by running it
    # against a real emulator sitting on its title menu:
    #   1. It re-fired on EVERY frame of a still run (still_since_ms is only
    #      cleared when the screen changes), so one idle menu minted 25
    #      incidents and re-alerted the agent every turn. Now it announces ONCE
    #      per continuous still run, then only at escalating milestones.
    #   2. It called every static screen a "possible hang". A menu, a paused
    #      game, or a modal dialog is SUPPOSED to be static. A real hang is
    #      static AND the process has stopped producing output — so cross-check
    #      the logs before using hang language.
    if cs is not None and cs < ve.MIN_CHANGE:
        with _lock:
            if not _visual_prev.get("still_since_ms"):
                _visual_prev["still_since_ms"] = ts_ms
                _visual_prev["still_log_bytes"] = _log_bytes_total()
                _visual_prev["still_kind"] = None
                _visual_prev["still_alive_why"] = None
            run_start = _visual_prev["still_since_ms"]
            base_bytes = _visual_prev.get("still_log_bytes") or 0
        still_for = (ts_ms - run_start) / 1000.0
        if still_for >= ve.FREEZE_SECONDS:
            grew = max(0, _log_bytes_total() - base_bytes)
            # Decide idle-vs-hang ONCE per still run and keep that verdict for
            # the whole run: the type has to stay stable so _push_visual_event
            # can coalesce the run into a single event with a frames count
            # (flip-flopping the type mid-run would emit alternating events).
            with _lock:
                kind = _visual_prev.get("still_kind")
                why_alive = _visual_prev.get("still_alive_why") or ""
            if not kind:
                # TWO independent liveness signals, because either alone gives a
                # false verdict. Logs alone: an idle menu can legitimately stop
                # logging (observed on SharpEmu at its title screen), which then
                # reads as a hang. CPU alone: a busy-spinning deadlock would
                # read as alive. Together: still producing output OR still
                # burning CPU ⇒ the process is running and merely idle; quiet
                # AND essentially no CPU ⇒ it really may be stalled.
                cpu = None
                try:
                    cpu = (_collector._reader.process_perf() or {}).get("cpu_percent")
                except Exception:
                    cpu = None
                alive_by_cpu = cpu is not None and float(cpu) >= 1.0
                if grew > 0:
                    kind, why_alive = "screen_idle", f"still logging (+{grew} bytes)"
                elif alive_by_cpu:
                    kind, why_alive = "screen_idle", f"still using CPU ({cpu}%)"
                else:
                    kind, why_alive = "screen_frozen", (
                        f"no new log output and CPU "
                        f"{'unknown' if cpu is None else str(cpu) + '%'}")
                with _lock:
                    _visual_prev["still_kind"] = kind
                    _visual_prev["still_alive_why"] = why_alive
            if kind == "screen_idle":
                sev = "info"
                detail = (f"screen unchanged for {still_for:.1f}s, but the program "
                          f"is alive — {why_alive}. Idle (menu, paused, or waiting "
                          f"on input), NOT hung")
            else:
                sev = "warning"
                detail = (f"screen unchanged for {still_for:.1f}s "
                          f"(>= {ve.FREEZE_SECONDS}s) and the process looks "
                          f"inactive ({why_alive}) — possible hang")
            _push_visual_event({"type": kind, "seq": seq, "ts_ms": ts_ms,
                                "severity": sev, "detail": detail,
                                "still_for_s": round(still_for, 1),
                                "log_bytes_since_static": grew,
                                "change_score": cs,
                                "next": f"av_frame_json(seq={seq})"})
    else:
        with _lock:
            _visual_prev["still_since_ms"] = None
            _visual_prev["still_log_bytes"] = None
            _visual_prev["still_kind"] = None
            _visual_prev["still_alive_why"] = None

    # big layout change
    if cs is not None and cs >= ve.LAYOUT_CHANGE:
        _push_visual_event({"type": "layout_change", "seq": seq, "ts_ms": ts_ms,
                            "severity": "info",
                            "detail": (f"{cs * 100:.0f}% of the screen repainted "
                                       f"(>= {ve.LAYOUT_CHANGE * 100:.0f}%) — new "
                                       "view, dialog, or navigation"),
                            "change_score": cs,
                            "changed_bbox": visual.get("changed_bbox"),
                            "next": f"av_frame_region(seq={seq})"})

    # on-screen text (optional OCR, throttled)
    #
    # Two reasons to scan, not one:
    #   1. the screen CHANGED — new content may contain error text
    #   2. we have no idea what is on screen yet — bootstrap
    # (2) matters because a program parked on a menu or a hung dialog never
    # changes, so a change-only gate meant the single most useful free fact
    # ("static screen showing WHAT?") was never computed in exactly the case
    # where it is most wanted. The probe is one-shot per static run and still
    # throttled, so an idle screen costs at most one extra scan (~150ms).
    if not VISUAL_OCR_SCAN:
        return
    changed_enough = cs is not None and cs >= ve.MIN_CHANGE
    with _lock:
        if changed_enough:
            _visual_stats["ocr_probe_done"] = False      # new content to read
        need_bootstrap = not _visual_stats.get("ocr_probe_done")
    if not (changed_enough or need_bootstrap):
        return
    with _lock:
        last = float(_visual_stats.get("last_ocr_scan_ms") or 0.0)
        if ts_ms - last < VISUAL_OCR_MIN_GAP:
            return
        _visual_stats["last_ocr_scan_ms"] = ts_ms
        # Mark the probe spent BEFORE the scan: if OCR finds nothing readable
        # (a genuinely blank screen) we must not retry it every 2s forever.
        _visual_stats["ocr_probe_done"] = True
    ocr = _ocr_image(image_path, text_cap=4000, line_cap=120)
    if not ocr.get("available"):
        # OCR could not run at all. Record that as UNKNOWN rather than returning
        # silently: an early return left `ocr_state` at whatever the last frame
        # set, so a readable frame's "no error" verdict leaked forward onto
        # frames nobody ever looked at.
        with _lock:
            _visual_stats["last_ocr_state"] = ve.OCR_UNREADABLE
            _visual_stats["last_ocr_seq"] = seq
            # DROP the previous frame's text. Leaving it made the push quote
            # frame #100's OCR while reporting frame #4321, and because the
            # snippet was then non-empty the honest "could not read" branch in
            # ambient.visual_sentence could never fire. Attributing one frame's
            # text to another is a false read, not a stale cache.
            _visual_stats["last_ocr_text"] = ""
        return
    with _lock:
        _visual_stats["ocr_scans"] += 1
        # KEEP the text, don't just test it for error keywords. This scan already
        # ran on the user's CPU; discarding the result meant the push could say
        # "the screen is STATIC" while the free answer to "static showing WHAT?"
        # was thrown away, forcing the agent to spend a tool call to re-read what
        # we already knew. A short snippet is worth ~20 tokens in an injection.
        _visual_stats["last_ocr_text"] = (ocr.get("text") or "")[:600]
        _visual_stats["last_ocr_seq"] = seq
    state, hits = ve.classify_ocr_error(ocr.get("text") or "",
                                        bool(ocr.get("available")))
    with _lock:
        _visual_stats["last_ocr_state"] = state
    if hits:
        _push_visual_event({
            "type": "on_screen_error", "seq": seq, "ts_ms": ts_ms,
            "severity": "error",
            "detail": f"{len(hits)} error-ish line(s) visible on screen",
            "on_screen_text": [h["line"] for h in hits],
            "keywords": sorted({h["keyword"] for h in hits}),
            "change_score": cs,
            "next": f"av_error_moment(seq={seq})"})
    elif state == ve.OCR_UNREADABLE:
        # The screen was NOT established to be clean — OCR simply produced
        # nothing. Measured: 32 of 72 small on-screen error lines on a 2560x1440
        # frame return empty. Say so, so nothing downstream can read this as
        # "no error on screen".
        _push_visual_event({
            "type": "screen_unreadable", "seq": seq, "ts_ms": ts_ms,
            "severity": "info",
            "detail": ("OCR returned no text for this frame — the screen was not "
                       "established to be error-free, it was not readable"),
            "change_score": cs,
            "next": f"av_get_frame(seq={seq}) and look at the image yourself"})


# ── Flight recorder: rolling window + pre-error incident freeze ───────────────

def _frame_facts(result_dict: dict, visual: dict, seq: int) -> dict:
    """Everything retention needs to judge one frame's worth. Kept separate so
    the classification rule is inspectable and testable on its own."""
    err = result_dict.get("error") if isinstance(result_dict, dict) else None
    err = err if isinstance(err, dict) else {}
    has_error = bool(err.get("message") or err.get("exception_type"))
    label = ""
    if has_error:
        label = (f"{err.get('exception_type') or 'error'}: "
                 f"{(err.get('message') or '')[:100]}").strip()
    vis = visual if isinstance(visual, dict) else {}
    with _lock:
        kinds = list(_event_kinds_by_seq.get(int(seq), []))
        pinned = int(seq) in _pinned_seqs
    return {
        "has_error": has_error, "error_label": label,
        "incident": pinned, "event_kinds": kinds,
        "change_score": vis.get("change_score") or 0.0,
        "changed": bool(vis.get("changed")),
        "ocr_error": bool(vis.get("on_screen_error")
                          or vis.get("error_text_detected")),
        # Tri-state companion to `ocr_error`. A bare False conflated "OCR read the
        # screen and there was no error" with "OCR read nothing", and retention
        # treats the second as clean — so a frame showing an error dialog too
        # small or too faint to OCR was scored evictable. Carry the distinction
        # rather than making the reader infer it. Literal default rather than the
        # visual_engine constant: this runs on the capture path, which imports
        # that module lazily, and an unresolved name here would kill the frame.
        #
        # This is the ROLLING global OCR state, not a per-frame verdict: OCR is
        # throttled, so most frames are never scanned and inherit whatever the
        # last scanned frame set (and on a machine with no OCR engine, that is
        # could_not_read for the whole run — see retention.assess, which relies
        # on exactly this and no longer forces such frames to await examination).
        # It is "" only before the first scan of the session. retention treats
        # could_not_read as "keep a little longer", never "demand an eye".
        "ocr_state": str(_visual_stats.get("last_ocr_state") or ""),
    }


#: Error fingerprints already represented in the awaiting queue -> the seq that
#: represents them. One fault should cost the agent ONE frame to look at.
_flagged_fingerprints: dict[str, int] = {}


def _dedupe_error_facts(facts: dict, result_dict: dict, seq: int) -> dict:
    """ONE FAULT, ONE FRAME TO EXAMINE.

    An error stays in the log tail, so every subsequent frame legitimately
    carries it — and each frame's sidecar persists it, so a restart re-reads them
    all. Both paths flooded the agent's queue with the same fault: 43 frames
    live, then 378 after a restart re-hydrated historical errors. Only the FIRST
    frame of a given fingerprint asks for eyes; the others keep their incident
    pin, so no evidence is lost.

    Clears BOTH has_error and incident, because assess() treats `incident` as an
    error signal too — leaving it set makes the de-duplication a no-op.
    """
    err = result_dict.get("error") if isinstance(result_dict, dict) else None
    if not isinstance(err, dict):
        return facts
    fp = str(err.get("fingerprint") or "")
    if not fp or not facts.get("has_error"):
        return facts
    with _lock:
        owner = _flagged_fingerprints.get(fp)
        if owner is None:
            _flagged_fingerprints[fp] = seq
            return facts
        if owner == seq:
            return facts
        if len(_flagged_fingerprints) > 2000:
            _flagged_fingerprints.clear()
    return dict(facts, has_error=False, incident=False, error_label="")


def _admit_to_ledger(seq: int, ts_ms: float, result_dict: dict, visual: dict,
                     program_folder: Path, stem: str) -> dict:
    """Register a captured frame with the retention ledger."""
    if not RECORDER_ENABLED:
        return {}
    facts = _frame_facts(result_dict, visual, seq)
    # Remember the real pin state BEFORE de-duplication: the incident pin is what
    # preserves the frame's pixels, and it must survive independently of whether
    # we ask the agent to look at this particular frame.
    was_pinned = bool(facts.get("incident"))
    facts = _dedupe_error_facts(facts, result_dict, seq)
    group = _ret.stem_group(program_folder, stem)
    rec = _ret.LEDGER.admit(seq, ts_ms, group, facts,
                            folder=str(program_folder), stem=stem)
    # Pin on the PRE-dedup value — de-duplicating the agent's queue must never
    # cost the frame its eviction protection.
    if was_pinned:
        _ret.LEDGER.set_pinned([seq], True)
    # Mirror the verdict onto the frame dict so /frame/<seq> and the visual
    # routes can say "this one is waiting on you" without a second lookup.
    result_dict["retention"] = {
        "needs_eyes": rec["needs_eyes"], "priority": rec["priority"],
        "reason": rec["reason"], "bytes": rec["bytes"],
    }
    return rec


def _frame_or_load(seq: int) -> dict | None:
    """A frame from memory, or parsed from its sidecar on first access.

    Startup only parses the newest HYDRATE_PARSE_LIMIT sidecars; this is what
    keeps every OLDER retained frame addressable anyway, without paying to parse
    tens of thousands of them before the bridge can answer its first request.
    """
    with _lock:
        fr = _frames.get(seq)
        if fr is not None:
            return fr
        path = _frames_on_disk.get(seq)
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(errors="replace"))
    except Exception as exc:
        _dbg(f"lazy frame load failed for seq {seq}: {exc}")
        return None
    p = Path(path)
    stem = p.stem.replace("_frame", "")
    img = p.parent / f"{stem}.png"
    thumb = p.parent / f"{stem}_thumb.png"
    data["annotated_image"] = str(img) if img.exists() else ""
    data["frame_image"]     = str(img) if img.exists() else ""
    data["thumbnail_file"]  = str(thumb) if thumb.exists() else ""
    data["json_sidecar"]    = str(p)
    data["program_folder"]  = str(p.parent)
    data["stem"]            = stem
    with _lock:
        _frames.setdefault(seq, data)
        _frames_on_disk.pop(seq, None)
    return data


def _no_such_frame(seq: int):
    """404 for a frame that does not exist — SAYING WHICH ONES DO.

    `abort(404)` handed back Flask's stock "The requested URL was not found on
    the server. If you entered the URL manually please check your spelling",
    which is wrong twice over: the URL is a real route, and the caller is a
    program that did not type anything. A caller reading that reasonably
    concludes AgentVision is misconfigured, when the true fact is narrower and
    more useful — that sequence has not been captured, or has been pruned.

    So state what is actually retained. `frames_retained: 0` after a capture
    that never started is a completely different diagnosis from a seq that
    fell off the end of a 900-frame window, and the caller cannot tell those
    apart from a bare 404.
    """
    with _lock:
        live = sorted(_frames)
        on_disk = sorted(_frames_on_disk)
    known = sorted(set(live) | set(on_disk))
    body = {
        "error": f"no frame {seq}",
        # Every error body in this server carries `status`; callers key off it
        # rather than parsing prose. Omitting it here broke that contract.
        "status": 404,
        "seq": seq,
        "frames_retained": len(known),
        "retained_range": [known[0], known[-1]] if known else None,
    }
    if not known:
        body["reason"] = ("no frames have been captured yet for the active "
                          "profile — this is an empty recorder, not a missing "
                          "route")
        body["next"] = "av_capture_start()"
    elif seq > known[-1]:
        body["reason"] = (f"sequence {seq} is ahead of the newest captured "
                          f"frame ({known[-1]})")
        body["next"] = "av_latest_frame() for the newest, or av_visual_changes()"
    elif seq < known[0]:
        body["reason"] = (f"sequence {seq} is older than the retention window "
                          f"(oldest retained is {known[0]}) and has been pruned")
        body["next"] = "av_incidents() — frozen failure windows survive pruning"
    else:
        body["reason"] = (f"sequence {seq} falls inside the retained range but "
                          f"is not present; frames are skipped when the target "
                          f"window is not on screen")
        body["next"] = "av_visual_changes() lists the sequences that do exist"
    return jsonify(body), 404


def _mark_seen(seq, how: str) -> None:
    """Record that the agent consumed this frame, which is what releases it for
    eviction. Every tier of the token ladder counts: reading a frame's JSON
    descriptor IS a legitimate examination — the whole point of the ladder is
    that full pixels are the last resort, not the proof of having looked.

    Safe to call while holding _lock: the ledger owns a separate lock and never
    calls back into this module.
    """
    try:
        if seq is not None:
            _ret.LEDGER.mark_examined(int(seq), how)
    except Exception:
        pass


def _recorder_prune(now_ms: float) -> None:
    """Enforce the disk BUDGET, evicting only frames that have served their
    purpose.

    This replaced a rolling 60-second delete. The clock was the bug: 60 s is
    shorter than one agent reasoning turn, so a frame could be advertised and
    then deleted before it could be fetched. Now nothing is evicted while there
    is headroom, and when the budget bites the cheapest already-examined frames
    go first. A frame still awaiting examination is protected until its hold
    expires — and if that ever happens it is counted in `dropped_unexamined`
    rather than quietly discarded.
    """
    if not RECORDER_ENABLED:
        return
    try:
        free_bytes, total_bytes = _free_disk_bytes(with_total=True)
    except Exception:
        free_bytes = total_bytes = None

    # total_bytes lets the ledger cap a nonsensical AGENTVISION_MIN_FREE_DISK
    # against the size of the volume it was measured on. Set the floor above the
    # disk's own capacity — easy to do, it is documented one line from
    # AGENTVISION_DISK_BUDGET — and every pass would otherwise believe the disk
    # is full forever, which is what unlocks the emergency tiers.
    plan = _ret.LEDGER.plan_eviction(now_ms, free_bytes=free_bytes,
                                     total_bytes=total_bytes)
    doomed = [int(r["seq"]) for r in plan.get("evict") or []]

    # Absolute backstop on dict growth if someone disables the byte budget.
    if RECORDER_HARD_FRAME_CAP > 0:
        with _lock:
            unpinned = [s for s in sorted(_frames) if s not in _pinned_seqs]
        overflow = max(0, len(unpinned) - RECORDER_HARD_FRAME_CAP)
        for s in unpinned[:overflow]:
            if s not in doomed:
                doomed.append(s)
                plan.setdefault("evict", []).append(
                    {"seq": s, "tier": 0, "bytes": 0, "interest": 0.0,
                     "archive": False, "reason": "hard frame cap"})

    with _lock:
        _recorder_stats["prunes_run"] += 1

    if not doomed:
        return

    # Archive + unlink happen outside the lock; the ledger deletes by file GROUP
    # so annotated siblings and .diff files go with the frame, and keeps the
    # sidecar JSON, thumbnail and authored annotations as permanent history.
    #
    # COMMIT FIRST, THEN FORGET. This used to drop every planned seq from the
    # bridge's index before commit_eviction ran, so a frame the ledger declined
    # to spend — one whose archive copy could not be written, say — vanished from
    # the index while its files and its ledger record both survived: unreachable
    # through the API but still on disk. Only the seqs actually evicted are
    # forgotten now.
    res = _ret.LEDGER.commit_eviction(plan, archive_dir=_archive_dir())
    actually_evicted = res.get("evicted_seqs")
    if actually_evicted is None:              # defensive: older ledger contract
        actually_evicted = doomed
    with _lock:
        removed = []
        for s in actually_evicted:
            fr = _frames.pop(int(s), None)
            _grids.pop(int(s), None)
            _event_kinds_by_seq.pop(int(s), None)
            if fr:
                removed.append(fr)
        _recorder_stats["frames_pruned"] += len(removed)

    _recorder_stats["bytes_pruned"] += int(res.get("bytes_reclaimed") or 0)
    if res.get("dropped_unexamined"):
        _warn(f"retention: {res['dropped_unexamined']} frame(s) expired before "
              f"the agent examined them — lower the capture rate, raise "
              f"AGENTVISION_DISK_BUDGET, or examine sooner")
    # Tier 2 = frames the policy said the agent MUST see, spent by a free-disk
    # emergency while still inside their hold. These were counted in no counter
    # and logged by no line: 70 must-see frames could die reporting zero losses.
    if res.get("dropped_unseen"):
        _warn(f"retention: a free-disk emergency spent "
              f"{res['dropped_unseen']} frame(s) the agent had not seen yet "
              f"(a compressed copy of each was verified on disk first). Free "
              f"disk space or raise AGENTVISION_MIN_FREE_DISK's headroom.")
    if res.get("dropped_pinned"):
        _warn(f"retention: {res['dropped_pinned']} incident-pinned frame(s) "
              f"were evicted — this should be impossible; please report it")
    if res.get("refused_pinned"):
        _dbg(f"retention: refused to evict {res['refused_pinned']} pinned "
             f"frame(s) named by the plan")
    if res.get("kept_unarchived"):
        _dbg(f"retention: kept {res['kept_unarchived']} unseen frame(s) whose "
             f"pixels are the only copy (no archive copy could be written)")
    if plan.get("archive_overflow_bytes"):
        _warn(f"retention: the compressed archive is "
              f"{_ret._human(plan['archive_overflow_bytes'])} over its share of "
              f"the budget. Evicting frames cannot reclaim archive bytes — "
              f"delete old files from {_archive_dir()} or raise "
              f"AGENTVISION_DISK_BUDGET / AGENTVISION_ARCHIVE_FRACTION.")
    if plan.get("free_disk_floor_capped"):
        _dbg(f"retention: AGENTVISION_MIN_FREE_DISK exceeded "
             f"{_ret.MIN_FREE_MAX_FRACTION:.0%} of the volume and was capped to "
             f"{_ret._human(plan.get('free_disk_floor_bytes') or 0)}")
    if plan.get("unmet_bytes"):
        _warn(f"retention: still {_ret._human(plan['unmet_bytes'])} over the "
              f"budget target after evicting everything releasable — the rest is "
              f"pinned evidence or frames still awaiting examination. Raise "
              f"AGENTVISION_DISK_BUDGET or examine the awaiting frames.")
    _dbg(f"retention evicted {len(removed)} frame(s), reclaimed "
         f"{res.get('bytes_reclaimed')} bytes "
         f"(used={plan.get('used_bytes')}/{plan.get('budget_bytes')}, "
         f"archived={res.get('archived')})")


def _measure_archive_bytes() -> int:
    """Set LEDGER.archive_bytes from what is ACTUALLY in the archive folder.

    `archive_bytes` is incremented as copies are written and reset only by
    Ledger.reset(), so a restart forgets every byte already on disk and the
    archive's share of the budget is re-granted from scratch each time. Reading
    the folder makes the cap mean what it says, and it is what lets
    plan_eviction report `archive_overflow_bytes` honestly.
    """
    d = _archive_dir()
    if not d or not d.exists():
        return 0
    total = 0
    for p in d.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except Exception:
            continue
    with _ret.LEDGER._lock:
        _ret.LEDGER.archive_bytes = int(total)
        _ret.LEDGER.stats["archive_bytes"] = int(total)
    if total:
        _info(f"Archive holds {_ret._human(total)} on disk "
              f"(measured, not assumed)")
    return total


def _archive_dir() -> Path | None:
    """Where compressed history lands: a sibling of the frames folder."""
    try:
        prof = _active_profile_obj()
        folder = Path(profile_output_folder(prof, _base_for(prof))) / "_archive"
        return folder
    except Exception:
        return None


def _free_disk_bytes(with_total: bool = False):
    """Free bytes on the volume the frames live on — and, on request, its total
    size, so the ledger can sanity-check the configured free-disk floor against
    the disk it is supposed to describe."""
    try:
        import shutil as _sh
        prof = _active_profile_obj()
        target = Path(profile_output_folder(prof, _base_for(prof)))
        probe = target if target.exists() else target.parent
        usage = _sh.disk_usage(str(probe))
        if with_total:
            return int(usage.free), int(usage.total)
        return int(usage.free)
    except Exception:
        return (None, None) if with_total else None


def _freeze_incident(kind: str, seq: int, ts_ms: float, detail: str,
                     extra: dict | None = None) -> dict | None:
    """FREEZE the pre-error window as an incident.

    Pins every frame in [ts - RECORDER_WINDOW_S, ts + RECORDER_TAIL_S] so the
    recorder can never prune them, and records the incident so av_incidents /
    av_error_moment can serve the whole moment without re-running anything.
    Idempotent-ish: a second trigger of the same kind within the tail window —
    or ANY later trigger carrying the same error fingerprint — extends the
    existing incident instead of creating a duplicate."""
    if not RECORDER_ENABLED:
        return None
    t0 = ts_ms - RECORDER_WINDOW_S * 1000.0
    t1 = ts_ms + RECORDER_TAIL_S * 1000.0
    fp = str((extra or {}).get("fingerprint") or "")
    with _lock:
        for inc in reversed(_incidents):
            same_kind_recent = (
                inc.get("kind") == kind
                and abs(float(inc.get("trigger_ms") or 0) - ts_ms)
                <= RECORDER_TAIL_S * 1000.0)
            # SAME FINGERPRINT => SAME INCIDENT, however long it persists.
            # An error stays in the log tail, so every frame re-detects it; with
            # only the tail-window check a standing error minted a brand-new
            # incident every ~5s (seen live: two "RetentionProbeError" incidents
            # 5s apart), which then churned the 25-incident cap, re-pinned frames
            # and re-alerted the agent about one single fault.
            same_fp = bool(fp) and str(inc.get("fingerprint") or "") == fp
            if same_kind_recent or same_fp:
                inc["window_ms"][1] = max(inc["window_ms"][1], t1)
                inc["trigger_count"] = int(inc.get("trigger_count", 1)) + 1
                inc["last_trigger_ms"] = ts_ms
                inc["last_trigger_seq"] = seq
                return inc
        window_seqs = sorted(
            s for s, f in _frames.items()
            if t0 <= float(f.get("timestamp_ms") or 0) <= t1)
        _pinned_seqs.update(window_seqs)
        inc = {
            "id": f"inc-{kind}-{int(ts_ms)}",
            "kind": kind,
            "trigger_seq": seq,
            "trigger_ms": ts_ms,
            "trigger_iso": clock.ms_to_iso(ts_ms),
            "detail": detail,
            "window_ms": [t0, t1],
            "pre_error_seconds": RECORDER_WINDOW_S,
            "post_error_seconds": RECORDER_TAIL_S,
            "frame_seqs": window_seqs,
            "frame_count": len(window_seqs),
            "frozen_at_ms": clock.now_ms(),
            "trigger_count": 1,
            "profile": _active_profile_name,
        }
        if extra:
            inc.update(extra)
        _incidents.append(inc)
        _recorder_stats["incidents_frozen"] += 1
        # Bound incident count; unpin the oldest one's exclusive frames.
        released: list[int] = []
        while len(_incidents) > RECORDER_INCIDENT_CAP:
            old = _incidents.pop(0)
            still_needed: set[int] = set()
            for other in _incidents:
                still_needed.update(other.get("frame_seqs") or [])
            for s in old.get("frame_seqs") or []:
                if s not in still_needed:
                    _pinned_seqs.discard(s)
                    released.append(s)
    # Keep the retention ledger's pin set in step with ours, outside the lock:
    # a pinned frame is the last thing eviction will ever touch.
    try:
        _ret.LEDGER.set_pinned(window_seqs, True)
        if released:
            _ret.LEDGER.set_pinned(released, False)
    except Exception as exc:
        _dbg(f"ledger pin sync failed: {exc}")
    _info(f"INCIDENT frozen: {inc['id']} — {len(window_seqs)} frame(s) "
          f"({RECORDER_WINDOW_S}s before + {RECORDER_TAIL_S}s after)")
    return inc


def _incident_for_seq(seq: int) -> dict | None:
    """The incident that owns this frame, if any."""
    with _lock:
        for inc in reversed(_incidents):
            if seq in (inc.get("frame_seqs") or []):
                return dict(inc)
    return None


def _grid_for_seq(seq: int) -> bytes:
    """Grid signature for a frame — from the in-memory cache, else recomputed
    from the PNG (survives a bridge restart) and cached."""
    with _lock:
        g = _grids.get(seq)
        if g:
            return g
        frame = _frame_or_load(seq)
    if not frame:
        return b""
    from utils import visual_engine
    g = visual_engine.grid_for_image(frame.get("annotated_image") or "")
    if g:
        with _lock:
            _grids[seq] = g
    return g


# ── Capture ───────────────────────────────────────────────────────────────────

@app.route("/capture", methods=["POST"])
def capture():
    data        = request.get_json(force=True, silent=True) or {}
    image_path  = data.get("image_path", "")
    save_folder = data.get("save_folder", SAVE_FOLDER)
    image_file  = Path(image_path).name if image_path else ""

    _dbg(f"/capture  image={image_path!r}  folder={save_folder!r}")

    collector = _get_collector()

    # Always use profile.project_root as the base — matches GUI path exactly
    program_folder = profile_output_folder(collector.profile, _base_for(collector.profile))
    collector.save_folder = program_folder

    try:
        frame = collector.collect(image_file=image_file)
        _dbg(f"  frame #{frame.sequence} collected  "
             f"running={frame.program.running}  "
             f"log_lines={len(frame.program.log_at_capture)}  "
             f"stats_keys={list(frame.program.stats.keys())[:5]}")
    except Exception as exc:
        _err(f"collect() failed for image {image_file!r}", exc)
        return jsonify({"error": str(exc)}), 500

    # Burn overlay onto image
    annotated_path = image_path
    if image_path and Path(image_path).exists():
        try:
            annotated_path = burn_overlay(image_path, frame)
            _dbg(f"  overlay burned → {annotated_path}")
        except Exception as exc:
            _warn(f"Overlay burn failed: {exc}")
            get_logger().log(f"Overlay error: {exc}")

    # Write JSON sidecar next to the PNG (SnapBurst folder)
    json_beside_png = ""
    if image_path:
        stem = Path(image_path).stem
        json_path = Path(image_path).parent / f"{stem}_frame.json"
        try:
            json_path.write_text(frame.to_json())
            json_beside_png = str(json_path)
            _dbg(f"  JSON sidecar → {json_path}")
        except Exception as exc:
            _err(f"JSON sidecar write failed: {exc}", exc)

    # Also write JSON into the per-program folder for history
    try:
        stem = Path(image_file).stem if image_file else f"frame_{frame.sequence:05d}"
        (program_folder / f"{stem}_frame.json").write_text(frame.to_json())
    except Exception as exc:
        _warn(f"Program-folder JSON write failed: {exc}")

    result = json.loads(frame.to_json())
    result["annotated_image"] = annotated_path
    result["json_sidecar"]    = json_beside_png
    result["program_folder"]  = str(program_folder)

    with _lock:
        _frames[frame.sequence] = result
        global _latest_frame
        _latest_frame = result

    return jsonify(result), 200


# ── Status ────────────────────────────────────────────────────────────────────

@app.route("/status", methods=["GET"])
def status():
    collector = _get_collector()
    # Per-profile: the seq-keyed frame store is shared by every profile, so a bare
    # len(_frames) reported 1501 frames under the abyss profile when abyss owned
    # exactly none of them (1500 were sharpemu's, 1 was a game bot's).
    mine, total = _active_frame_count()
    return jsonify({
        "status":           "running",
        "active_profile":   _active_profile_name,
        "program":          collector.profile.display_name,
        "save_folder":      str(collector.save_folder),
        "frames_stored":    mine,
        "frames_stored_all_profiles": total,
        "frames_stored_note": (
            f"{mine} of the {total} frame(s) in the shared store belong to "
            f"'{_active_profile_name}'."
            if mine != total else
            f"all {total} in-memory frame(s) belong to '{_active_profile_name}'."),
        "timestamp":        datetime.now().isoformat(),
        "debug_log":        str(_DEBUG_LOG),
        "capture_rate":     capture_rate_info(_auto_engine.interval),
        "preflight":        _preflight_hint(collector.profile),
        "read_this_first":  "av_start_here",
        "token_rule":       ("Capture/hashing/diffing is FREE local CPU; your "
                             "tokens are not. Prefer av_visual_changes and "
                             "av_frame_json over raw frames, and escalate to "
                             "pixels (av_frame_region) only when JSON is "
                             "insufficient. av_token_report proves the saving."),
    }), 200


def _folder_to_profile() -> dict[str, str]:
    """{output_folder: profile_name} for every profile, so a frame's own
    program_folder identifies which program it came from."""
    m: dict[str, str] = {}
    for name, prof in (load_profiles() or {}).items():
        try:
            m[str(Path(profile_output_folder(prof, _base_for(prof))).resolve())] = name
        except Exception:
            continue
    return m


def _frame_provenance(frame: dict) -> dict:
    """Which profile a frame actually belongs to, and whether that is the profile
    being asked about.

    `_frames` is ONE dict keyed by `seq`, and startup hydrates EVERY profile into
    it. Sequence numbers are per-program counters, so they collide: measured here,
    a game bot's only frame and sharpemu's oldest frame are both seq 1, a game bot wins
    the insert, and `/frame/1` then served a June a game bot screenshot while the
    active profile was sharpemu — with nothing in the response saying so. The
    ambient push had the same bug in reverse, attributing sharpemu's frame #13016
    to AbyssEngine. Frames must carry their owner.
    """
    folder = str(frame.get("program_folder") or "")
    owner = ""
    if folder:
        try:
            owner = _folder_to_profile().get(str(Path(folder).resolve()), "")
        except Exception:
            owner = ""
    active = _active_profile_name
    foreign = bool(owner and owner != active)
    out = {"owning_profile": owner or "unknown",
           "active_profile": active,
           "program_folder": folder,
           "foreign_profile": foreign}
    if foreign:
        out["warning"] = (
            f"This frame belongs to profile '{owner}', NOT the active profile "
            f"'{active}'. Frame sequence numbers are per-program and collide "
            f"across profiles; do not attribute this image to '{active}'.")
    elif not owner:
        out["warning"] = (
            "This frame's program_folder does not match any known profile's "
            "output folder, so its owning program could not be confirmed.")
    return out


def _active_frame_count() -> tuple[int, int]:
    """(frames belonging to the active profile, frames held in total).

    Reporting only the total made `frames_stored` a cross-program number: it read
    1501 under the sharpemu profile when 1500 were sharpemu's and one was
    a game bot's, and it read the same 1501 under the abyss profile, which owned NONE
    of them."""
    try:
        f2p = _folder_to_profile()
    except Exception:
        f2p = {}
    with _lock:
        items = list(_frames.values())
    mine = 0
    for fr in items:
        folder = str(fr.get("program_folder") or "")
        if not folder:
            continue
        try:
            if f2p.get(str(Path(folder).resolve())) == _active_profile_name:
                mine += 1
        except Exception:
            continue
    return mine, len(items)


def _image_serve_plan(out: dict) -> dict:
    """What this frame costs to look at, and whether a smaller copy is safe.

    Two measured facts drive this. A 2560x1440 frame costs 4784 visual tokens —
    more than the entire text budget of a push — so a ten-frame look-back is
    ~47,840 tokens on its own, which is squarely inside the range where every
    frontier model's recall measurably degrades. But a blanket downscale is not
    the answer either: on a text-bearing debug UI, OCR-recoverable strings went
    4/8 at full resolution to 0/8 at HALF size. Cheap and wrong is not an
    improvement on expensive and right.

    So the frame decides. Pictorial frames can be served at 640px for 16x less;
    text-bearing frames stay full. Either way the choice is DECLARED, because a
    silent downscale would let the agent reason over pixels that are not the
    pixels captured and never know.
    """
    try:
        from utils import visual_engine as ve
        path = str(out.get("annotated_image") or out.get("frame_image") or "")
        if not path:
            return {}
        w = int(out.get("width") or 0)
        h = int(out.get("height") or 0)
        if not (w and h):
            from PIL import Image
            with Image.open(path) as im:
                w, h = im.size
        plan = ve.serve_plan(path, w, h)
        plan["captured"] = f"{w}x{h}"
        if plan.get("serve") == "downscaled":
            plan["how"] = (f"av_frame_region(seq={out.get('sequence')}) returns "
                           f"pixels inline; or read the path for full detail")
        plan["note"] = ("this is what LOOKING at the frame costs. The JSON above "
                        "is a few hundred tokens and often answers the question "
                        "without opening the image at all.")
        return plan
    except Exception:
        return {}


def _augment_frame_for_ai(frame: dict) -> dict:
    """Return a shallow copy of a frame with a self-describing `_ai` block so the
    model naturally uses screenshots well: it spells out the exact time-aligned
    log window, the paired sidecar files, the capture rate, and how to correlate
    image↔log. Non-destructive — the stored frame is untouched."""
    if not isinstance(frame, dict):
        return frame
    out = dict(frame)
    prov = _frame_provenance(out)
    out["provenance"] = prov
    if prov.get("foreign_profile"):
        out["foreign_profile"] = True
    seq = out.get("sequence")
    shutter_ms = float(out.get("timestamp_ms") or 0)
    cm = out.get("capture_meta") or {}
    # Whether "they describe the same moment" is TRUE for this frame, per source.
    # It is a claim about the logs, so it has to be checked against them: on a
    # post-mortem profile the frame's pinned text log was 47 h older than the
    # shutter and carried no timestamps at all, and this block still told the
    # model the two described the same moment.
    try:
        _sf = _source_freshness(_get_collector().profile,
                               ref_ms=shutter_ms or None)
    except Exception:
        _sf = {}
    _sf_stale = list(_sf.get("stale_sources") or [])
    _sf_untimed = list(_sf.get("untimestamped_sources") or [])
    if _sf_stale or _sf_untimed:
        _what = (
            "A screenshot ('frame') plus the program state AgentVision pinned to "
            "it. CAUTION — image and logs do NOT all describe the same moment "
            f"here: source(s) {_sf_untimed} carry no timestamps (ordering only) "
            f"and source(s) {_sf_stale} were already stale when this shutter "
            "fired. Read the image as of the shutter; read those sources' content "
            "as EARLIER, not concurrent. av_frame_alignment(seq=" f"{seq}) "
            "itemises it.")
    else:
        _what = (
            "A screenshot ('frame') paired with the program's exact state at the "
            "shutter instant. READ THE IMAGE at `annotated_image` together with "
            "this JSON — they describe the same moment.")
    out["_ai"] = {
        "what_this_is": _what,
        "log_source_freshness_at_shutter": {
            "stale_sources": _sf_stale,
            "untimestamped_sources": _sf_untimed,
            "alignable_sources": list(_sf.get("alignable_sources") or []),
            "program_silent_s": _sf.get("program_silent_s"),
        },
        "provenance": prov,
        "image_path":     out.get("annotated_image"),
        "json_sidecar":   out.get("json_sidecar"),
        # What it costs to LOOK at this frame, and whether looking at a smaller
        # copy would be safe. The agent is told to open image_path, so that cost
        # is real and was previously invisible: 4784 tokens for a 2560x1440 frame,
        # more than the entire text budget of a push.
        "image_cost":     _image_serve_plan(out),
        "shutter_ms":     shutter_ms,
        "shutter_iso":    out.get("timestamp"),
        "capture_rate":   cm.get("rate") or {"interval_s": _auto_engine.interval,
                                             "shots_per_second": _fps(_auto_engine.interval)},
        "time_alignment": {
            "log_window": "logs bounded to bytes <= *_offset as of shutter_ms",
            "action_log_offset": out.get("action_log_offset"),
            "log_offset":        out.get("log_offset"),
            "verify_with":       f"av_frame_alignment(seq={seq})",
        },
        "correlate_with": {
            "actions_around_this_frame": f"av_actions_around_frame(seq={seq}, window_secs=5)",
            "normalized_multilog":       "av_log_normalized(from_ms=<shutter-2000>, to_ms=<shutter+2000>)",
            "state_at_shutter":          f"av_state_at(at_ms={shutter_ms})",
        },
        "capture_health": {
            "black_frame":   cm.get("black_frame"),
            "window_found":  cm.get("window_found"),
            "capture_target": cm.get("capture_target"),
        },
    }
    # ── The cheap path ──────────────────────────────────────────────────────
    # This route is the EXPENSIVE tier (you are about to read a full image).
    # Point the agent at the JSON-first alternatives every single time.
    vis = (cm.get("visual") or {}) if isinstance(cm.get("visual"), dict) else {}
    if vis:
        out["_ai"]["visual"] = {
            "dhash":        vis.get("dhash"),
            "change_score": vis.get("change_score"),
            "changed_bbox": vis.get("changed_bbox"),
            "is_blank":     (vis.get("structural") or {}).get("is_blank"),
        }
    out["_ai"]["CHEAPER_PATH"] = {
        "why": ("Reading the full PNG costs hundreds-to-thousands of visual "
                "tokens. These cost a fraction and are usually enough."),
        "frame_as_json":      f"av_frame_json(seq={seq})",
        "only_changed_pixels": f"av_frame_region(seq={seq})",
        "review_a_whole_run": "av_visual_changes()",
        "one_call_failure_bundle": f"av_error_moment(seq={seq})",
    }
    # If this frame looks blank, say so loudly so the AI doesn't hallucinate.
    if cm.get("black_frame"):
        out["_ai"]["WARNING"] = (
            "This frame looks blank/black — the target window was likely "
            "minimized, occluded, capture-blocked, or not yet painted. Do NOT "
            "describe visual content from it; check av_capture_status health "
            "and av_program_status, and consider re-capturing.")
    return out


@app.route("/latest", methods=["GET"])
def latest():
    with _lock:
        if _latest_frame is None:
            return jsonify({"error": "No frames yet",
                            "hint": "capture may be stopped or the program "
                                    "isn't running; see av_capture_status"}), 404
        _agent_reads["full_frame"] += 1
        seq = _latest_frame.get("sequence")
        if seq is not None:
            _agent_reads["seqs_full"].add(seq)
        _mark_seen(seq, "latest")
        return jsonify(_augment_frame_for_ai(_latest_frame)), 200


@app.route("/latest/pointer", methods=["GET"])
def latest_pointer():
    """Just WHICH frame is newest — sequence, timestamp, summary. No frame body.

    Exists because /latest has two side effects that a pointer lookup must not
    have: it increments the `full_frame` read counter that av_token_report uses
    to prove its savings, and it marks the frame EXAMINED, which is what lets
    retention delete it. av_overview called /latest purely to read `.sequence`,
    so every orientation call inflated the token-saving figure and quietly
    consumed a frame's examine flag on the agent's behalf.
    """
    with _lock:
        lf = _latest_for_active()
        if lf is None:
            return jsonify({"error": "No frames yet",
                            "sequence": None,
                            "hint": "capture may be stopped or the program "
                                    "isn't running; see av_capture_status"}), 404
        return jsonify({
            "sequence":     lf.get("sequence"),
            "timestamp":    lf.get("timestamp"),
            "timestamp_ms": lf.get("timestamp_ms"),
            "summary":      lf.get("summary"),
            "profile":      _active_profile_name,
            "note": ("pointer only — this call does NOT count as a frame read "
                     "and does NOT mark the frame examined. Use /latest "
                     "(av_latest_frame) when you actually want the frame."),
        }), 200


@app.route("/frame/<int:seq>", methods=["GET"])
def get_frame(seq: int):
    with _lock:
        frame = _frame_or_load(seq)
        if frame:
            _agent_reads["full_frame"] += 1
            _agent_reads["seqs_full"].add(seq)
            _mark_seen(seq, "full_frame")
    if not frame:
        # A seq is unique only WITHIN a profile. If ANOTHER profile owns this one,
        # say so here — this 404 is exactly the moment the caller needs to know
        # that a bare sequence number does not identify a frame. Before the index
        # was made single-profile, this same case silently returned the OTHER
        # program's image instead.
        with _lock:
            owners = sorted({p for (p, s) in _foreign if s == seq})
        if owners:
            return jsonify({
                "error": f"frame {seq} does not belong to the active profile",
                "status": 404,
                "active_profile": _active_profile_name,
                "seq_exists_in_profiles": owners,
                "why": ("frame sequence numbers are unique per profile, not "
                        "globally — the same number names a different image "
                        "under each program"),
                "fix": (f"av_set_active_profile('{owners[0]}') to index that "
                        f"program's frames, then request this seq again"),
                "see": "GET /frames/collisions lists every seq owned by >1 profile",
            }), 404
        return _no_such_frame(seq)
    return jsonify(_augment_frame_for_ai(frame)), 200


def _active_action_log_path() -> str:
    """The program's structured JSONL event log.

    THE BUG THIS SHAPE EXISTS FOR. This used to return `action_log_file` and
    nothing else. But `av_bridge_commit` wires the emitter sinks it scaffolds as
    **log_sources** — `[{path: .../agentvision/actions.jsonl, adapter: jsonl,
    label: events}, ...]` — and never sets `action_log_file`. So on every
    program bridged through the modern flow this returned "", and
    `_read_action_jsonl` returned [] for all fourteen functions built on it.

    Measured on a freshly bridged program whose emitter had just recorded two
    silently-swallowed KeyErrors, with type, file:line, handling function and
    occurrence count all present in actions.jsonl:

        av_errors_by_fingerprint  ->  {"fingerprints": [], "total_failures": 0}
        av_diagnose               ->  hypotheses: [], while its OWN
                                      counts.distinct_error_fingerprints said 2
        av_list_bookmarks         ->  []

    Eleven MCP tools answering emptily about a log that was sitting on disk,
    full of exactly the evidence they exist to surface — and an empty list reads
    as "this program is fine". The flight recorder had the truth the whole time;
    the analysis half was pointed at a field nobody sets.

    So: explicit `action_log_file` still wins (older profiles and per-frame pins
    depend on it), and otherwise the JSONL source the installer actually wired
    is used. A non-JSONL source is never substituted — handing a text log to a
    JSONL reader parses zero records, which is the same silence wearing a
    different hat.
    """
    profiles = load_profiles()
    p = profiles.get(_active_profile_name) or BUILTIN_PROFILES.get(_active_profile_name)
    # The resolution itself lives in connectors.program_connector
    # .resolve_action_log_path, because the identical one-field read was ALSO
    # sitting in the capture shutter, the input daemon and the GUI panes —
    # fixing only this function left frames pinning profile_action_log="" with
    # action_log_offset=0, i.e. the read side healed while the capture side
    # kept producing unbounded, unpinned frames.
    return resolve_action_log_path(p) if p else ""


def _read_action_jsonl(window: tuple[float, float] | None = None,
                       category: str | None = None,
                       source_substr: str | None = None,
                       limit: int = 500,
                       path_override: str = "",
                       max_offset: int = 0) -> list[dict]:
    """Generic JSONL reader for any bridged program's structured action log.
    window=(t0_ms, t1_ms) inclusive. category and source_substr filter records.
    path_override: read this path instead of the active profile's (per-frame pin).
    max_offset>0: only read bytes [..max_offset] (capture-time alignment).
    Returns up to `limit` records, oldest→newest (newest preserved on truncation)."""
    path_str = path_override or _active_action_log_path()
    if not path_str or not os.path.exists(path_str):
        return []
    out: list[dict] = []
    try:
        size = max_offset if max_offset > 0 else os.path.getsize(path_str)
        with open(path_str, "rb") as f:
            data = f.read(size)
        for line in data.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            # Prefer ts_ms (epoch float) — falls back to parsing ts ISO string
            if window is not None:
                rec_ms = rec.get("ts_ms")
                if rec_ms is None:
                    rec_ms = _iso_to_ms(rec.get("ts", ""))
                if rec_ms is None:
                    continue
                if rec_ms < window[0] or rec_ms > window[1]:
                    continue
            if category and rec.get("category") != category:
                continue
            if source_substr and source_substr not in str(rec.get("source", "")):
                continue
            out.append(rec)
        # When truncating, keep the most recent — but for windowed queries
        # the window already bounds the result, so limit just caps overflow.
        return out[-limit:] if limit > 0 else out
    except Exception:
        return []


def _iso_to_ms(ts: str) -> float | None:
    try:
        # ActionLog emits "...Z"; normalize for fromisoformat
        from datetime import datetime
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).timestamp() * 1000.0
    except Exception:
        return None


def _int_arg(name: str, default, *, source=None):
    """Parse an int query/body param, abort(400) on bad input. A mistyped param
    is a CLIENT error (400) — not a server fault (500). `source` defaults to
    request.args; pass a dict for JSON-body params."""
    src = request.args if source is None else source
    raw = src.get(name, default)
    try:
        return int(raw)
    except (ValueError, TypeError):
        abort(400)


def _float_arg(name: str, default, *, source=None):
    """Parse a float query/body param, abort(400) on bad input (see _int_arg)."""
    src = request.args if source is None else source
    raw = src.get(name, default)
    try:
        return float(raw)
    except (ValueError, TypeError):
        abort(400)


@app.route("/frame/<int:seq>/actions", methods=["GET"])
def get_frame_actions(seq: int):
    """Return action JSONL records within ±window_secs of the given frame's timestamp.
    Generic — works for any bridged program with an action_log_file profile field.
    Uses the per-frame profile_action_log so an old frame still maps to the
    JSONL it was actually paired with at capture time."""
    with _lock:
        frame = _frame_or_load(seq)
    if not frame:
        return _no_such_frame(seq)
    window_secs = _float_arg("window_secs", "5")
    frame_ms = float(frame.get("timestamp_ms") or 0)
    if not frame_ms:
        return jsonify({"frame_seq": seq, "actions": []}), 200
    half = window_secs * 1000.0
    pinned = frame.get("profile_action_log") or ""
    recs = _read_action_jsonl(window=(frame_ms - half, frame_ms + half),
                              path_override=pinned, limit=10000)
    return jsonify({"frame_seq": seq, "window_secs": window_secs,
                    "frame_ts_ms": frame_ms, "count": len(recs),
                    "actions": recs}), 200


@app.route("/log/range", methods=["GET"])
def get_log_range():
    """Time-range query against the active program's action JSONL.
    Query params: from_ms, to_ms (epoch ms), optional category, source, limit."""
    try:
        from_ms = float(request.args.get("from_ms", "0"))
        to_ms   = float(request.args.get("to_ms",   str(time.time() * 1000.0)))
    except ValueError:
        abort(400)
    category   = request.args.get("category") or None
    source     = request.args.get("source")   or None
    limit      = _int_arg("limit", "500")
    recs = _read_action_jsonl(window=(from_ms, to_ms),
                              category=category, source_substr=source, limit=limit)
    return jsonify({"from_ms": from_ms, "to_ms": to_ms,
                    "count": len(recs), "actions": recs}), 200


# ── Universal normalized multi-log timeline ───────────────────────────────────
# The bridge no longer assumes JSONL. These routes read EVERY log source the
# active profile declares (log_sources[] + legacy log_file + action_log_file),
# auto-detect each one's format via connectors.log_adapters, and merge them onto
# the one UTC-ms timeline as unified JSON events. This is what makes AgentVision
# "connect to any program of any language and already have the log ready."

def _active_profile_obj() -> "ProgramProfile":
    profiles = load_profiles()
    return (profiles.get(_active_profile_name)
            or BUILTIN_PROFILES.get(_active_profile_name)
            or ProgramProfile())


@app.route("/log/sources", methods=["GET"])
def log_sources_route():
    """List the resolved log sources for the active profile plus the detected
    format of each — so a human or the AI can confirm 'the log is ready' before
    debugging, across any language.

    Each source carries `adapter` — the adapter name the merge ACTUALLY uses
    for it (the configured one when explicit, `reader:<name>` for SourceReader
    sources, else the auto-detected format) — alongside the raw
    configured_adapter / detected_adapter / detect_confidence breakdown."""
    prof = _active_profile_obj()
    srcs = _log_sources.effective_sources(prof)
    out = []
    for s in srcs:
        path = os.path.expanduser(s["path"])
        exists = os.path.exists(path)
        detected, conf, scores = _log_adapters.detect_adapter_for_file(path)
        reader = s.get("reader") or ""
        configured = s["adapter"]
        if reader:
            resolved = f"reader:{reader}"
        elif configured and configured != "auto":
            resolved = configured
        elif exists:
            resolved = detected
        else:
            resolved = None
        out.append({
            "label":            s["label"],
            "path":             s["path"],
            "exists":           exists,
            "adapter":          resolved,
            "configured_adapter": configured,
            "detected_adapter": detected if exists else None,
            "detect_confidence": conf if exists else None,
        })
    return jsonify({
        "active_profile": _active_profile_name,
        "language":       getattr(prof, "language", "") or None,
        "source_count":   len(out),
        "sources":        out,
        "available_adapters": _log_adapters.list_adapters(),
    }), 200


@app.route("/log/normalized", methods=["GET"])
def log_normalized_route():
    """Merged, time-aligned, format-normalized view across ALL of the active
    profile's log sources. Query params:
        from_ms, to_ms   epoch-ms window. DEFAULT (neither given): NO time
                         filter — the most recent `limit` events by count, so
                         history with stale timestamps is never silently
                         hidden. Give only from_ms for "since t → now"; only
                         to_ms for "everything up to t".
        level            minimum canonical level (TRACE/DEBUG/INFO/WARN/ERROR/FATAL)
        label            restrict to one source label
        limit            max events (default 500; keeps most recent)
    Each event is the unified schema (ts/ts_ms/level/category/source/data/…)
    with extra log_label + log_path so you can tell which log it came from."""
    prof = _active_profile_obj()
    from_arg = request.args.get("from_ms")
    to_arg   = request.args.get("to_ms")
    try:
        from_ms = float(from_arg) if from_arg is not None else None
        to_ms   = float(to_arg)   if to_arg   is not None else None
    except ValueError:
        abort(400)
    window = None
    if from_arg is not None or to_arg is not None:
        window = (from_ms if from_ms is not None else float("-inf"),
                  to_ms   if to_ms   is not None else float("inf"))
    level = (request.args.get("level") or "").strip()
    label = (request.args.get("label") or "").strip()
    limit = _int_arg("limit", "500")
    events = _log_sources.read_normalized(
        prof, window=window, limit=limit, level=level, label=label)
    return jsonify({
        "from_ms": from_ms, "to_ms": to_ms,
        "window": "explicit" if window is not None else "none (most recent by count)",
        "level": level or None,
        "label": label or None, "count": len(events), "events": events,
    }), 200


@app.route("/frame/<int:seq>/alignment", methods=["GET"])
def frame_alignment(seq: int):
    """Self-check the image↔log time-alignment for one frame. Recomputes, from
    the frame's own capture metadata, whether every action bounded into this
    frame's context truly predates the shutter — and reports any leakage.

    A healthy frame returns aligned=True with leaked_after_shutter=0. This is
    the proof the correlation the whole tool rests on is actually correct."""
    with _lock:
        frame = _frame_or_load(seq)
    if not frame:
        return _no_such_frame(seq)
    shutter_ms = float(frame.get("timestamp_ms") or 0)
    cm = frame.get("capture_meta") or {}
    pinned = frame.get("profile_action_log") or ""
    action_offset = int(frame.get("action_log_offset") or 0)
    # Read exactly what this frame pinned (bounded by its capture-time offset).
    recs = _read_action_jsonl(path_override=pinned,
                              max_offset=action_offset, limit=100000)
    leaked = []
    newest_ms = 0.0
    for r in recs:
        rms = r.get("ts_ms") or _iso_to_ms(r.get("ts", "")) or 0.0
        if rms > newest_ms:
            newest_ms = rms
        # Anything stamped AFTER the shutter (beyond a small clock-skew grace)
        # should not have been visible at capture time.
        if shutter_ms and rms > shutter_ms + 50.0:
            leaked.append({"ts_ms": rms, "over_ms": round(rms - shutter_ms, 1),
                           "source": r.get("source")})
    # How much of the "context" this frame was certified against is AgentVision's
    # own watchdog rather than the program. On a post-mortem profile this reached
    # 100%, so the old check was proving AgentVision's timestamps precede
    # AgentVision's shutter — true by construction, and worthless as evidence.
    self_emitted = sum(
        1 for r in recs
        if str(r.get("source") or "").lower().startswith(_AV_SELF_EMITTERS))
    fresh = _source_freshness(_get_collector().profile, ref_ms=shutter_ms or None)
    stale = list(fresh.get("stale_sources") or [])
    untimed = list(fresh.get("untimestamped_sources") or [])
    no_future = len(leaked) == 0
    aligned = no_future and not stale and not untimed
    resp = {
        "frame_seq":        seq,
        "shutter_ms":       shutter_ms,
        "capture_end_ms":   cm.get("capture_end_ms"),
        "capture_latency_ms": cm.get("capture_latency_ms"),
        "action_log_offset": action_offset,
        "records_in_context": len(recs),
        "records_self_emitted": self_emitted,
        "records_from_program": len(recs) - self_emitted,
        "newest_record_ms": newest_ms,
        "aligned":          aligned,
        "alignment_verdict": ("sound" if aligned else
                              "leak" if not no_future else "unalignable_sources"),
        "no_future_records": no_future,
        "leaked_after_shutter": len(leaked),
        "leaks":            leaked[:20],
        # Per-source, so the reader can see WHICH log may be paired with the
        # image and which may only be ordered against it.
        "sources":          fresh.get("sources") or [],
        "stale_sources":    stale,
        "untimestamped_sources": untimed,
        "alignable_sources": list(fresh.get("alignable_sources") or []),
        "program_silent_s": fresh.get("program_silent_s"),
        "capture_meta":     cm,
    }
    if aligned:
        resp["note"] = ("aligned=True: no record bounded into this frame carries "
                        "a timestamp after the shutter, AND every declared log "
                        "source is timestamped and current with it.")
    else:
        parts = []
        if not no_future:
            parts.append(f"{len(leaked)} record(s) in this frame's context "
                         f"postdate its shutter")
        if untimed:
            parts.append(f"source(s) {untimed} carry NO timestamps, so their "
                         f"content cannot be time-aligned to this image — only "
                         f"ordered")
        if stale:
            parts.append(f"source(s) {stale} are stale relative to this "
                         f"shutter, so their content was NOT concurrent with "
                         f"this image")
        if self_emitted and self_emitted == len(recs) and recs:
            parts.append(f"all {self_emitted} record(s) in context are "
                         f"AgentVision's own watchdog, not the program's output")
        resp["note"] = ("aligned=False: " + "; ".join(parts)
                        + ". Do not read these sources' content as concurrent "
                          "with this frame.")
    return jsonify(resp), 200


# ── Event schema registry (auto-discovered) ───────────────────────────────────

# Static documentation Claude reads first. Programs add their own conventions.
# The FIRST block is the universal category vocabulary every normalized event
# uses (from the log adapters + emitters); the second is game/bot-specific.
_EVENT_DOC: dict[str, dict] = {
    # ── Universal categories (level-derived; used across all languages) ──────
    "debug":  {"desc": "debug/trace-level log line; data: {message, level}"},
    "log":    {"desc": "info-level log line; data: {message, level, ...}"},
    "warn":   {"desc": "warning-level log line; data: {message, level}"},
    "error":  {"desc": "error/critical log OR failure; data: {message, level, exception_type?}"},
    "exception": {"desc": "uncaught exception; data: {type, message, traceback|stack}"},
    "process": {"desc": "process lifecycle; data: {name: start|exit, pid, ...}"},
    "event":  {"desc": "generic structured event; data: {name, ...payload}"},
    "metric": {"desc": "numeric measurement; data: {name, value, unit?}"},
    "stdout": {"desc": "teed stdout line; data: {text, channel}"},
    "stderr": {"desc": "teed stderr line; data: {text, channel}"},
    "trace":  {"desc": "logical-action span boundary; data: {trace_id, name}"},
    "state":  {"desc": "state machine transition; data: {from, to, run_id}"},
    # ── Domain-specific (bot/game) categories ────────────────────────────────
    "key":    {"desc": "raw key press / release; data: {button, edge|duration}"},
    "combo":  {"desc": "multi-key combo; data: {combo, duration}"},
    "move":   {"desc": "directional move; data: {dir_deg, duration, label?}"},
    "cast":   {"desc": "skill cast; data: {skill, target?}"},
    "attack": {"desc": "attack on monster; data: {monster, skill?}"},
    "npc":    {"desc": "NPC interaction; data: {action, target}"},
    "nav":    {"desc": "nav phase marker; data: {phase, label}"},
}


@app.route("/events/schema", methods=["GET"])
def events_schema():
    """Return the event vocabulary the bridged program emits.
    Combines static docs (categories) with auto-discovered (category, event-name)
    pairs scanned from the active JSONL. Claude reads this once per session."""
    discovered: dict[str, dict] = {}
    recs = _read_action_jsonl(limit=2000)
    for r in recs:
        cat  = r.get("category") or "?"
        d    = r.get("data") or {}
        name = d.get("name") if isinstance(d, dict) else None
        key  = f"{cat}.{name}" if name else cat
        if key not in discovered:
            sample_fields = sorted(list(d.keys())) if isinstance(d, dict) else []
            discovered[key] = {
                "category":     cat,
                "event_name":   name,
                "sample_source": r.get("source"),
                "sample_fields": sample_fields,
                "count":        1,
            }
        else:
            discovered[key]["count"] += 1
    from shared.schema.snapshot_schema import SCHEMA_VERSION
    return jsonify({
        "schema_version": SCHEMA_VERSION,
        "categories":  _EVENT_DOC,
        "discovered":  discovered,
        "scanned":     len(recs),
        "note": ("Every event is one JSON object with {ts, ts_ms, category, level, "
                 "source, trace_id, frame_seq, data{...}}. category is derived "
                 "from level so ERROR/FATAL from any language is category='error'."),
    }), 200


def _fingerprint(text: str) -> str:
    """SHA1 of normalized error text — drops paths/numbers so similar crashes
    collapse to the same id. ECS-style; lets us group recurring failures.
    Delegates to modules.diagnostics.fingerprint — the single implementation —
    so an emit-time fingerprint always matches a query-time one."""
    from modules.diagnostics import fingerprint as _dx_fingerprint
    return _dx_fingerprint(text)


def _record_fingerprint(rec: dict) -> str:
    """Compute a stable fingerprint for any record, prioritizing error fields."""
    d = rec.get("data") or {}
    if isinstance(d, dict):
        # Prefer explicit error fields if present
        for field in ("error", "stack_trace", "stack", "message", "reason"):
            v = d.get(field)
            if v:
                return _fingerprint(str(v))
        name = d.get("name") or ""
        # Otherwise fingerprint the (source, category, event-name) triple
        return _fingerprint(f"{rec.get('source')}|{rec.get('category')}|{name}")
    return _fingerprint(str(rec.get("source")))


@app.route("/errors/by-fingerprint", methods=["GET"])
def errors_by_fingerprint():
    """Return all records sharing a fingerprint, oldest→newest. If fp is
    omitted, return a histogram of all fingerprints in the active log."""
    fp = request.args.get("fp")
    triggers = _detect_failure_records(limit=2000)
    if fp:
        matches = [r for r in triggers if _record_fingerprint(r) == fp]
        return jsonify({"fp": fp, "count": len(matches), "records": matches}), 200
    hist: dict[str, dict] = {}
    for r in triggers:
        f = _record_fingerprint(r)
        if f not in hist:
            d = r.get("data") or {}
            sample_msg = (d.get("message") or d.get("name") or
                          d.get("reason")  or r.get("source") or "")
            hist[f] = {"fp": f, "count": 0, "first_ts": r.get("ts"),
                       "last_ts": r.get("ts"), "sample": sample_msg[:120]}
        hist[f]["count"] += 1
        hist[f]["last_ts"] = r.get("ts")
    ranked = sorted(hist.values(), key=lambda x: -x["count"])
    out = {"fingerprints": ranked, "total_failures": len(triggers)}

    # THE TWO PATHS MUST AGREE. `_detect_failure_records` reads only
    # actions.jsonl; `_text_log_failures` reads the text log-source pipeline. That
    # split is already documented at _text_log_failures — and the fix was applied
    # to the HEALTH path and not to this one, so the disagreement survived here.
    #
    # Measured on a live bridged program: av_diagnose reported "2 failure-shaped
    # log line(s) … DeviceLostException" and graded it unhealthy 35, while this
    # tool returned fingerprints: [] and total_failures: 0. An empty list reads as
    # "no errors", which is the confident silence this project exists to remove —
    # and it is worse here than in diagnose, because an agent reaches for this
    # tool precisely to ask "what is failing?".
    #
    # Not fingerprinted, deliberately: a bare log line has no exception type or
    # frames, so minting a fingerprint from it would invent an identity that
    # cannot be looked up again. Reported instead, with the reason.
    try:
        if not ranked:
            # _active_profile_obj(), not `collector` — that name is not in scope
            # here, and the except below would have hidden the NameError,
            # leaving this disclosure permanently dead. Third time this
            # exact shape has bitten: a broad except over an out-of-scope
            # name looks like caution and is really silence.
            _tf = _text_log_failures(_active_profile_obj())
            if int(_tf.get("total") or 0) > 0:
                out["no_fingerprints_but_failures_exist"] = {
                    "text_log_failures": int(_tf.get("total") or 0),
                    "groups": (_tf.get("groups") or [])[:5],
                    "why_not_fingerprinted": (
                        "these are failure-shaped lines in a TEXT log, not "
                        "structured exception records. They carry no exception "
                        "type or stack frames, so there is nothing stable to "
                        "fingerprint — a synthesised id could not be looked up "
                        "again. This is a real failure signal, not noise."),
                    "read_this": ("do NOT read fingerprints:[] as 'this program "
                                  "is not failing'. av_diagnose() and "
                                  "av_log_normalized(level='WARN') see these; "
                                  "av_log_raw() shows them verbatim."),
                }
    except Exception:
        pass

    # SWALLOWED EXCEPTIONS ARE THE OTHER WAY THIS LIST LIES BY BEING EMPTY.
    # They are not failures and are not fingerprinted (see _handled_exceptions),
    # but a caller asking "what is failing?" must not be told nothing while the
    # emitter has two named, located, counted exceptions on disk.
    try:
        _he = _handled_exceptions()
        if int(_he.get("total_occurrences") or 0) > 0:
            out["handled_exceptions"] = _he
            out["read_this"] = (
                f"total_failures is {out['total_failures']}, and that is "
                f"accurate — but {_he['total_occurrences']} exception(s) were "
                f"raised and SILENTLY HANDLED at "
                f"{_he['distinct_sites']} site(s). Those are reported under "
                f"`handled_exceptions`, not as failures, because the program "
                f"caught them. If it reported success while doing less work "
                f"than expected, start there.")
    except Exception as exc:
        # Never silent: a disclosure that fails must say it failed.
        out["handled_exceptions_unavailable"] = f"{type(exc).__name__}: {exc}"
    return jsonify(out), 200


@app.route("/trace/<tid>/timeline", methods=["GET"])
def trace_timeline(tid: str):
    """All records carrying this trace_id, plus any frames captured during the
    span. Lets Claude reason about one logical action attempt end-to-end."""
    recs = _read_action_jsonl(limit=20000)
    matched = [r for r in recs if r.get("trace_id") == tid]
    if not matched:
        return jsonify({"trace_id": tid, "count": 0,
                        "records": [], "frames": []}), 200
    t0 = min((r.get("ts_ms") or _iso_to_ms(r.get("ts","")) or 0) for r in matched)
    t1 = max((r.get("ts_ms") or _iso_to_ms(r.get("ts","")) or 0) for r in matched)
    with _lock:
        frames = [{"sequence": s, "timestamp_ms": float(f.get("timestamp_ms") or 0),
                   "image_file": f.get("image_file"),
                   "annotated_image": f.get("annotated_image")}
                  for s, f in _frames.items()
                  if t0 <= float(f.get("timestamp_ms") or 0) <= t1]
    frames.sort(key=lambda x: x["sequence"])
    return jsonify({
        "trace_id":   tid,
        "span_ms":    [t0, t1],
        "duration_s": round((t1 - t0) / 1000.0, 3),
        "count":      len(matched),
        "records":    matched,
        "frames":     frames,
    }), 200


@app.route("/wide", methods=["GET"])
def wide_at():
    """Return the 'wide' state record nearest the given epoch ms.
    Wide records are category=='wide' (or data.name=='state.wide') —
    one fat record/sec containing full program state."""
    try:
        at_ms = float(request.args.get("at_ms", str(time.time() * 1000.0)))
    except ValueError:
        abort(400)
    recs = _read_action_jsonl(limit=20000)
    wide = []
    for r in recs:
        cat  = (r.get("category") or "").lower()
        name = ((r.get("data") or {}).get("name") or "").lower()
        if cat == "wide" or name == "state.wide":
            wide.append(r)
    if not wide:
        return jsonify({"at_ms": at_ms, "match": None,
                        "hint": "no records with category='wide' or data.name='state.wide'"}), 200
    nearest = min(wide, key=lambda r: abs((r.get("ts_ms")
                  or _iso_to_ms(r.get("ts","")) or 0) - at_ms))
    return jsonify({"at_ms": at_ms, "match": nearest,
                    "match_ts_ms": nearest.get("ts_ms")}), 200


#: Word-boundary match for a failure word inside a `source` name. The old test
#: was `any(k in src for k in ("fail","error","crash"))` — a bare substring, so
#: every INFO line from a module called FailoverManager, CrashReporterService or
#: app.error_pages was counted as a program failure, inflating the bookmark list
#: and the fingerprint histogram with routine logging. `_` is a word character,
#: so `error_pages` correctly does not match while `gpu-error` does.
_SRC_FAILWORD = re.compile(
    r"\b(fail|failed|failing|failure|error|errors|crash|crashed|fatal|panic)\b")

#: An explicit benign level from the program outranks a guess made from a module
#: name: if the emitter says INFO, a module called `app.error` is not a failure.
_BENIGN_LEVELS = ("INFO", "DEBUG", "TRACE", "NOTICE", "VERBOSE")


def _handled_exceptions(limit: int = 2000) -> dict:
    """Exceptions the program RAISED and then silently swallowed.

    These are deliberately NOT failure triggers. A swallowed exception is
    handled by definition, and a retry loop that catches and retries is healthy;
    folding these into `_detect_failure_records` would grade such a program
    unhealthy every run, and asserting "failed" about something the program
    handled is the same class of false claim this project bans everywhere else.

    But reporting them as NOTHING is worse, and that is what happened. The
    `swallowed_exceptions` emitter exists precisely because this failure mode is
    invisible — it records the exception type, the raise site with file:line,
    the handling function, and an occurrence count, which is the highest-quality
    evidence AgentVision ever gets. Measured on a program that printed
    "3 line items, total 34.00 / OK" and exited 0 while silently dropping 2 of
    5 records: the emitter caught both KeyErrors at worker.py:13, and
    av_errors_by_fingerprint answered `{"fingerprints": [], "total_failures":
    0}`. AgentVision re-hid the exact thing it was built to reveal.

    So: a third state, like the OCR tri-state. Not "failed", not "clean" —
    "handled, and here is where".

    SCOPED TO THE NEWEST RUN. A log survives across runs, and "what did THIS
    run swallow?" is the question being answered. The first version used the
    newest summary in the whole read window, which asserted a PREVIOUS run's
    swallows as current in two ways: mid-run (run 2 swallowing, no summary yet
    -> run 1's summary reported, wrong sites and all) and post-fix (run 2
    clean, run 1's summary still reported — the exact stale-verdict trap the
    corrections signal exists for). Every emitter record carries run_id, so
    the newest run is read off the log, not assumed. Reads are filtered to
    av.bootstrap.* BEFORE the limit is applied, so a chatty program cannot
    evict the swallow records out of the window.
    """
    boot = _read_action_jsonl(source_substr="av.bootstrap.", limit=limit)
    current_run = None
    for r in reversed(boot):
        if r.get("run_id"):
            current_run = r.get("run_id")
            break
    live: list[dict] = []
    summaries: list[dict] = []
    older_swallows = 0
    for r in boot:
        src = (r.get("source") or "")
        if not src.endswith(("bootstrap.swallowed", "bootstrap.swallowed_summary")):
            continue
        if current_run is not None and r.get("run_id") \
                and r.get("run_id") != current_run:
            older_swallows += 1
            continue
        (summaries if src.endswith("_summary") else live).append(r)

    sites: list[dict] = []
    total = 0
    if summaries:
        # The run's exit summary carries the authoritative totals (the live
        # records are de-duplicated, so counting them undercounts).
        d = summaries[-1].get("data") or {}
        total = int(d.get("total_occurrences") or 0)
        sites = list(d.get("sites") or [])
        counted_from = "swallowed_summary"
    else:
        # No summary yet: the run has not exited. The live records are emitted
        # on a decaying schedule (occurrence 1, 10, 100, ...), each carrying
        # the count AT EMISSION — so per site the NEWEST count is the best
        # known, and summing records instead of sites both overcounts the
        # sites (3 records = 1 site) and undercounts the occurrences.
        by_site: dict[tuple, dict] = {}
        for r in live:
            d = r.get("data") or {}
            key = (d.get("type") or d.get("exception_type"), d.get("raised_at"))
            n = int(d.get("occurrences") or 1)
            cur = by_site.get(key)
            if cur is None or n > cur["occurrences"]:
                by_site[key] = {"type": key[0], "raised_at": key[1],
                                "handled_in": d.get("handled_in"),
                                "occurrences": n}
        sites = sorted(by_site.values(), key=lambda s: -s["occurrences"])
        total = sum(s["occurrences"] for s in sites)
        counted_from = ("live records only — the run has not exited, and live "
                        "records are de-duplicated, so these totals are "
                        "at-emission lower bounds")
    out = {
        "total_occurrences": total,
        "distinct_sites": len(sites),
        "sites": sites[:20],
        "counted_from": counted_from,
        "what_this_is": (
            "exceptions the program raised and swallowed without logging. NOT "
            "counted as failures — the program handled them, and catching is "
            "often deliberate. But nothing else in the program records these, "
            "so if a run reported success while doing less work than expected, "
            "this is where to look first."),
    }
    if current_run is not None:
        out["run_id"] = current_run
    if older_swallows:
        # Say what was left out and why, rather than silently narrowing.
        out["earlier_runs"] = (
            f"{older_swallows} swallowed-exception record(s) from EARLIER runs "
            f"were excluded — this reports only the newest run "
            f"({current_run!r}). av_log_raw() still shows the history.")
    return out


def _detect_failure_records(limit: int = 200) -> list[dict]:
    """Generic failure detection across any bridged program's JSONL.

    A record is a 'failure trigger' if its category is 'error'/'exception'/
    'fatal', its level is ERROR/FATAL/CRITICAL, its data.name is run.fail/fatal/
    crash, or a failure WORD appears in its source (word-boundary, and only when
    the record does not carry an explicitly benign level). Language-agnostic — a
    Python uncaught exception, a Java ERROR log and a Go stderr 'error' line all
    trip it identically because everything normalizes to the unified schema.
    """
    triggers: list[dict] = []
    recs = _read_action_jsonl(limit=limit)
    for r in recs:
        cat = (r.get("category") or "").lower()
        src = (r.get("source") or "").lower()
        lvl = (r.get("level") or "").upper()
        name = ""
        d = r.get("data")
        if isinstance(d, dict):
            name = (d.get("name") or "").lower()
        if (cat in ("error", "exception", "fatal")
                or lvl in ("ERROR", "FATAL", "CRITICAL")
                or name in ("run.fail", "fatal", "crash")
                or (lvl not in _BENIGN_LEVELS and _SRC_FAILWORD.search(src))):
            triggers.append(r)
    return triggers


@app.route("/bookmarks", methods=["GET"])
def list_bookmarks():
    """List all auto-detected failure bookmarks across the active program's JSONL.
    Each bookmark has a stable id derived from the trigger record's timestamp."""
    triggers = _detect_failure_records(limit=500)
    out = []
    for t in triggers:
        ts = t.get("ts", "")
        out.append({
            "id":          ts,                         # ts is unique enough
            "ts":          ts,
            "category":    t.get("category"),
            "source":      t.get("source"),
            "summary":     (t.get("data") or {}).get("name") or t.get("source"),
            "fingerprint": _record_fingerprint(t),     # group recurring failures
        })
    # VISUAL auto-bookmarks — the screen-side equivalent, found for free at
    # capture time (freeze / blank / big layout change / on-screen error text).
    # Additive: the `bookmarks` list keeps its original log-driven shape.
    with _lock:
        vis = [dict(e) for e in _visual_events][-50:]
    return jsonify({"count": len(out), "bookmarks": out,
                    "visual_count": len(vis),
                    "visual_bookmarks": vis,
                    "visual_note": ("auto-detected from the screen itself; see "
                                    "av_visual_events for detectors + thresholds")}), 200


@app.route("/bookmark/<bid>", methods=["GET"])
def get_bookmark(bid: str):
    """Return the full context bundle for a bookmark: 30s before + trigger +
    10s after, plus the closest matching frame_seq if any."""
    trigger_ms = _iso_to_ms(bid)
    if trigger_ms is None:
        abort(400)
    before_ms = trigger_ms - 30_000.0
    after_ms  = trigger_ms + 10_000.0
    # Wide limit so the "30s before" half can't be silently truncated even on
    # bursty programs. Window already bounds the result to ~40s of records.
    bundle = _read_action_jsonl(window=(before_ms, after_ms), limit=100000)

    # Find the closest frame in our in-memory store
    closest_seq = None
    closest_dt  = None
    with _lock:
        for seq, fr in _frames.items():
            fts = float(fr.get("timestamp_ms") or 0)
            if not fts:
                continue
            dt = abs(fts - trigger_ms)
            if closest_dt is None or dt < closest_dt:
                closest_dt, closest_seq = dt, seq

    return jsonify({
        "id":            bid,
        "trigger_ms":    trigger_ms,
        "window":        {"before_ms": before_ms, "after_ms": after_ms},
        "closest_frame": closest_seq,
        "closest_dt_ms": closest_dt,
        "actions":       bundle,
        "count":         len(bundle),
    }), 200


@app.route("/bookmark/<bid>/outliers", methods=["GET"])
def bookmark_outliers(bid: str):
    """Honeycomb BubbleUp-style: for each numeric field in the 30s-before window
    of this bookmark, report how far its mean deviates from the session-wide
    baseline. Auto-points Claude at the smoking gun."""
    trigger_ms = _iso_to_ms(bid)
    if trigger_ms is None:
        abort(400)
    before_ms  = trigger_ms - 30_000.0
    sample = _read_action_jsonl(window=(before_ms, trigger_ms), limit=100000)
    baseline = _read_action_jsonl(limit=20000)
    if not sample:
        return jsonify({"id": bid, "outliers": [], "note": "empty window"}), 200

    def _numeric_fields(recs: list[dict]) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for r in recs:
            d = r.get("data") or {}
            if not isinstance(d, dict):
                continue
            for k, v in d.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out.setdefault(f"data.{k}", []).append(float(v))
        return out

    sample_fields   = _numeric_fields(sample)
    baseline_fields = _numeric_fields(baseline)

    outliers = []
    for name, vals in sample_fields.items():
        if len(vals) < 3:
            continue
        sample_mean = sum(vals) / len(vals)
        base = baseline_fields.get(name, [])
        if len(base) < 5:
            continue
        base_mean = sum(base) / len(base)
        if abs(base_mean) < 1e-9:
            ratio = float("inf") if sample_mean else 1.0
        else:
            ratio = sample_mean / base_mean
        # Z-ish signal: how many baseline-stddevs is the sample mean off?
        var = sum((x - base_mean) ** 2 for x in base) / max(1, len(base) - 1)
        std = var ** 0.5
        z   = (sample_mean - base_mean) / std if std > 0 else 0.0
        outliers.append({
            "field":         name,
            "sample_mean":   round(sample_mean, 4),
            "baseline_mean": round(base_mean, 4),
            "ratio":         round(ratio, 3) if ratio != float("inf") else None,
            "z":             round(z, 2),
            "n_sample":      len(vals),
            "n_baseline":    len(base),
        })
    # Rank by absolute z-score — most-deviant first
    outliers.sort(key=lambda x: -abs(x["z"]))
    return jsonify({"id": bid, "trigger_ms": trigger_ms,
                    "window": {"before_ms": before_ms, "trigger_ms": trigger_ms},
                    "outliers": outliers[:20]}), 200


_STUCK_THRESHOLD_S = float(os.environ.get("AGENTVISION_STUCK_THRESHOLD_S", "30"))
_FP_HISTORY_PATH   = Path(os.environ.get("AGENTVISION_FP_HISTORY",
                                         "log/agentvision_fp_history.json"))
_session_seen_fps = set()    # type: set[str]
_session_start_ms = 0.0      # type: float


# ── observer log: AgentVision's own notes, kept OUT of the program's stream ──
#
# The stuck watchdog used to append its `program.stuck` records straight into the
# bridged program's actions.jsonl, justified in-code as "so existing bookmark
# detection picks it up". Measured on SharpEmu's real log, both halves of that
# were wrong:
#
#   * 2024 of 2123 records in the file (95%) were AgentVision's own watchdog,
#     and the newest program-emitted record was 48.8 h old. The file read as a
#     live stream because the observer kept touching it.
#   * ZERO of those 2024 records were picked up by _detect_failure_records —
#     source `agentvision.watchdog` contains none of fail/error/crash, category
#     is `event`, and `program.stuck` is not in the recognised name set. The
#     write bought nothing at all; it was pure contamination.
#
# So observations now go to AgentVision's OWN sink, one file per profile, and the
# program's log is never written to. Nothing downstream loses a signal: the same
# fact (`program_silent_s`) is computed live and correctly by _source_freshness()
# at read time, which is where a reader should get it from anyway.
_OBSERVER_DIR = Path(os.environ.get("AGENTVISION_OBSERVER_DIR", "log/observer"))

#: Last watchdog verdict per profile, in memory, for the endpoints to report.
_observer_state: dict[str, dict] = {}


def _observer_log_path(profile_name: str | None = None) -> Path:
    """AgentVision's own JSONL sink for the given profile. Never the program's."""
    name = profile_name or _active_profile_name or "default"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(name)) or "default"
    return _OBSERVER_DIR / f"{safe}.observer.jsonl"


def _append_observer_record(rec: dict, profile_name: str | None = None) -> bool:
    """Append one AgentVision observation to AgentVision's own sink."""
    path = _observer_log_path(profile_name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
        return True
    except Exception as e:
        _warn(f"observer write failed ({path}): {e}")
        return False


def _read_observer_jsonl(profile_name: str | None = None,
                         limit: int = 200) -> list[dict]:
    """Read back AgentVision's own observations (oldest→newest, newest kept)."""
    path = _observer_log_path(profile_name)
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return out[-limit:] if limit > 0 else out


def _load_fp_history() -> set[str]:
    try:
        if _FP_HISTORY_PATH.exists():
            return set(json.loads(_FP_HISTORY_PATH.read_text()))
    except Exception:
        pass
    return set()


def _save_fp_history(fps: set[str]) -> bool:
    """Persist the known-fingerprint set. Returns whether the write landed —
    this used to be a `def` with zero call sites anywhere in the repo, so the
    history file was never created and `_load_fp_history()` always returned the
    empty set. That reduced /anomalies/new to "everything since session start",
    reported in full on every call, forever."""
    try:
        _FP_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FP_HISTORY_PATH.write_text(json.dumps(sorted(fps)))
        return True
    except Exception as e:
        _warn(f"fingerprint history write failed ({_FP_HISTORY_PATH}): {e}")
        return False


@app.route("/anomalies/new", methods=["GET"])
def anomalies_new():
    """Failure fingerprints first seen during the CURRENT bridge session — the
    bugs that just appeared, as opposed to the program's usual background noise.

    Two properties this deliberately has:

      * it is IDEMPOTENT within a session. The filter set is snapshotted at boot
        (`_session_seen_fps`), so calling this twice gives the same answer. A
        read tool whose result changes because you read it is not usable as
        evidence.
      * it PERSISTS what it reported, so the next bridge session no longer calls
        those fingerprints new. `_save_fp_history` was dead code, which is what
        made "new this session" mean "everything since boot" indefinitely.

    Each fingerprint appears ONCE, with `count` / `first_ts` / `last_ts`, rather
    than once per matching record — a failure recurring in a tight loop used to
    fill the response with thousands of identical entries with no cap.
    """
    history = set(_session_seen_fps)          # snapshot: boot-time known set
    triggers = _detect_failure_records(limit=5000)
    seen: dict[str, dict] = {}
    scanned_in_session = 0
    for r in triggers:
        ts_ms = r.get("ts_ms") or _iso_to_ms(r.get("ts", "")) or 0
        if ts_ms < _session_start_ms:
            continue
        scanned_in_session += 1
        fp = _record_fingerprint(r)
        if not fp or fp in history:
            continue
        ts = r.get("ts")
        if fp in seen:
            e = seen[fp]
            e["count"] += 1
            e["last_ts"] = ts
        else:
            seen[fp] = {"fp": fp, "ts": ts, "first_ts": ts, "last_ts": ts,
                        "count": 1, "source": r.get("source"),
                        "summary": (r.get("data") or {}).get("name") or r.get("source")}
    new = sorted(seen.values(), key=lambda e: -e["count"])
    # Persist for the NEXT session. Not added to the in-memory snapshot, so this
    # session keeps answering the same way however many times it is asked.
    persisted = False
    if new:
        persisted = _save_fp_history(history | set(seen))
    return jsonify({
        "session_start_ms":  _session_start_ms,
        "history_size":      len(history),
        "count":             len(new),
        "records_scanned_in_session": scanned_in_session,
        "new_fingerprints":  new,
        "history_persisted": persisted,
        "history_path":      str(_FP_HISTORY_PATH),
        "semantics": ("`count` is how many records share the fingerprint, not a "
                      "separate finding. This answer is stable for the rest of "
                      "this bridge session; these fingerprints will not be "
                      "reported as new by the next one."),
    }), 200


#: Prefix of the `source` field on records AgentVision itself emits. Freshness of
#: those records proves AGENTVISION is alive; it is not evidence the PROGRAM is.
#: Measured on SharpEmu: the newest 200 records in actions.jsonl were 100%
#: agentvision.watchdog while the program had been silent for 47 h, and the
#: alignment check happily certified the frames against them.
#:
#: The watchdog no longer writes into the program's log at all (see _OBSERVER_DIR
#: and /observer/log), so new logs cannot be contaminated this way. This filter
#: still matters for two reasons: logs written by earlier versions are already
#: contaminated on disk, and a bridged program is free to declare AgentVision's
#: own observer file as one of its sources.
#: Source prefixes that are AgentVision's OWN machinery, not the program.
#: "agentvision" is the old watchdog (see _newest_program_record_ms for the
#: 48.8-hours-dead incident). "av.wrapper." is the run wrapper's file watcher
#: and lifecycle notes — and it MUST be here because the bridge writes frame
#: PNGs into the project's agentvision/ folder, the wrapper dutifully reports
#: each one as file.create/file.modify, and those records then kept the
#: "newest program record" clock ticking: AgentVision's own shutter was
#: certifying the program as current. Measured on a tkinter target: 140
#: records in one frame's context, ~120 of them av.wrapper.* noise, and
#: records_self_emitted said 0. av.bootstrap.* stays PROGRAM evidence on
#: purpose — the tee re-emits the program's own stdout/stderr/logging.
_AV_SELF_EMITTERS = ("agentvision", "av.wrapper.")

#: How far a source's newest PROGRAM-emitted record may lag the reference clock
#: before the source is called stale. 120 s is well beyond any capture cadence.
SOURCE_STALE_S = float(os.environ.get("AGENTVISION_SOURCE_STALE_S", "120"))


#: (path, size, mtime, label, limit) -> per-source scan totals. Keyed on the
#: file's own identity so a changed log invalidates itself.
_sf_scan_cache: dict[tuple, dict] = {}
_sf_lock = threading.Lock()


def _human_age(seconds: float | None) -> str:
    """'47h 18m' / '3m 20s' / '12s'. Age is the difference between a claim and
    now; spelling it out is what stops a stale claim reading as a live one."""
    if seconds is None:
        return "unknown"
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def _file_mtime_ms(path: str) -> float | None:
    try:
        return os.path.getmtime(path) * 1000.0
    except Exception:
        return None


def _source_freshness(profile, ref_ms: float | None = None,
                      limit: int = 8000) -> dict:
    """Per-log-source freshness and time-ALIGNABILITY, measured not assumed.

    AgentVision's pitch is that imagery time-aligned to log events beats reading
    a log by hand. That only holds for sources whose events actually carry
    timestamps and are actually contemporaneous with the frames. Neither was ever
    checked. Two ways it silently fails, both observed on the SharpEmu profile:

      * `log.txt` has NO timestamps on any of its 451 lines, so all 180
        `guestgpu.present ok=False` failures normalize with ts_ms=None and CANNOT
        be aligned to any frame. Ordering is all that survives.
      * that file was last written 47 h before the newest frame, yet every one of
        the 1500 hydrated frames pinned it at the same EOF offset (25663) and so
        carried the same frozen tail — whose last line is `ok=True`. A reader
        pairing frames with that tail concludes the GPU path was healthy for a
        day after the program had actually exited.

    So this returns, per declared source: whether it exists, whether its events
    carry parseable timestamps at all, how far its newest PROGRAM-emitted record
    lags `ref_ms`, and how much of its recent traffic is AgentVision's own
    watchdog. Callers use it to qualify — or withhold — an alignment claim.

    `ref_ms` is the clock to compare against: pass a frame's shutter to ask "was
    this source current when the shutter fired", or leave None for wall-clock now.
    """
    ref = float(ref_ms) if ref_ms else time.time() * 1000.0
    out: dict = {
        "ref_ms": ref,
        "ref_is_now": not bool(ref_ms),
        "stale_threshold_s": SOURCE_STALE_S,
        "sources": [],
        "stale_sources": [],
        "untimestamped_sources": [],
        "alignable_sources": [],
        "missing_sources": [],
        "program_silent_s": None,
    }
    try:
        srcs = _log_sources.effective_sources(profile)
    except Exception as exc:
        _dbg(f"source freshness: cannot enumerate sources: {exc}")
        out["error"] = str(exc)
        return out

    newest_program_overall: float | None = None
    for src in srcs:
        label = src.get("label") or "?"
        path = src.get("path") or ""
        rec: dict = {
            "label": label, "path": path, "adapter": src.get("adapter"),
            "exists": bool(path) and os.path.exists(path),
            "size_bytes": None, "mtime_ms": _file_mtime_ms(path),
            "events": 0, "timestamped": 0, "untimestamped": 0,
            "self_emitted": 0,
            "newest_event_ms": None, "newest_program_event_ms": None,
            "age_s": None, "program_age_s": None,
            "time_alignable": False, "verdict": "unknown", "why": "",
        }
        try:
            rec["size_bytes"] = os.path.getsize(path)
        except Exception:
            pass
        if not rec["exists"]:
            rec["verdict"] = "missing"
            rec["why"] = "declared in the profile but not present on disk"
            out["missing_sources"].append(label)
            out["sources"].append(rec)
            continue
        # The scan is the only expensive part and does NOT depend on ref_ms, so
        # it is cached against (path, size, mtime): the same source re-verdicted
        # against 1500 different frame shutters costs one parse, not 1500.
        ck = (path, rec["size_bytes"], rec["mtime_ms"], label, limit)
        with _sf_lock:
            scan = _sf_scan_cache.get(ck)
        if scan is None:
            try:
                evs = _log_sources.read_normalized(profile, window=None,
                                                   limit=limit, label=label)
            except Exception as exc:
                rec["verdict"] = "unreadable"
                rec["why"] = f"read failed: {exc}"
                out["sources"].append(rec)
                continue
            scan = {"events": 0, "timestamped": 0, "untimestamped": 0,
                    "self_emitted": 0, "newest_event_ms": None,
                    "newest_program_event_ms": None}
            for ev in evs:
                scan["events"] += 1
                ts = ev.get("ts_ms")
                selfish = str(ev.get("source") or "").lower().startswith(
                    _AV_SELF_EMITTERS)
                if selfish:
                    scan["self_emitted"] += 1
                if ts:
                    scan["timestamped"] += 1
                    if (scan["newest_event_ms"] is None
                            or ts > scan["newest_event_ms"]):
                        scan["newest_event_ms"] = ts
                    if not selfish and (scan["newest_program_event_ms"] is None
                                        or ts > scan["newest_program_event_ms"]):
                        scan["newest_program_event_ms"] = ts
                else:
                    scan["untimestamped"] += 1
            with _sf_lock:
                if len(_sf_scan_cache) > 64:
                    _sf_scan_cache.clear()
                _sf_scan_cache[ck] = scan
        rec.update(scan)
        if rec["newest_event_ms"]:
            rec["age_s"] = round((ref - rec["newest_event_ms"]) / 1000.0, 1)
        if rec["newest_program_event_ms"]:
            rec["program_age_s"] = round(
                (ref - rec["newest_program_event_ms"]) / 1000.0, 1)
            if (newest_program_overall is None
                    or rec["newest_program_event_ms"] > newest_program_overall):
                newest_program_overall = rec["newest_program_event_ms"]

        # ── verdict ──────────────────────────────────────────────────────────
        if rec["events"] and not rec["timestamped"]:
            rec["verdict"] = "untimestamped"
            rec["why"] = (
                f"{rec['events']} event(s), none with a parseable timestamp — "
                f"this source can be ORDERED against the frames but NOT "
                f"time-aligned to them")
            out["untimestamped_sources"].append(label)
        elif rec["newest_program_event_ms"] is None and rec["self_emitted"]:
            rec["verdict"] = "self-emitted-only"
            rec["why"] = (
                f"every timestamped record is AgentVision's own "
                f"({rec['self_emitted']} of them); the program itself has "
                f"written nothing to this source")
            out["stale_sources"].append(label)
        elif rec["program_age_s"] is not None and rec["program_age_s"] > SOURCE_STALE_S:
            rec["verdict"] = "stale"
            rec["why"] = (
                f"newest PROGRAM-emitted record is {rec['program_age_s']:.0f}s "
                f"older than the reference clock"
                + (f"; the {rec['self_emitted']} newer record(s) here are "
                   f"AgentVision's own watchdog, not the program's"
                   if rec["self_emitted"] else ""))
            out["stale_sources"].append(label)
        elif rec["timestamped"]:
            rec["verdict"] = "live"
            rec["time_alignable"] = True
            rec["why"] = "timestamped and current with the reference clock"
            out["alignable_sources"].append(label)
        else:
            rec["verdict"] = "empty"
            rec["why"] = "no events read"
        out["sources"].append(rec)

    if newest_program_overall:
        out["program_silent_s"] = round(
            (ref - newest_program_overall) / 1000.0, 1)
        out["newest_program_event_ms"] = newest_program_overall

    # `missing` belongs here. Without it a profile declaring a log that does not
    # exist got the all-clear below — "frame↔log pairing is sound" — while the
    # note's own text already listed "missing" among the verdicts it claims to
    # report. This note is surfaced in /diagnose, the first thing an agent reads,
    # and it also feeds logs.stale, which gates whether a correction may retract
    # an error. A file we cannot open is the strongest reason NOT to claim
    # alignment is sound.
    bad = (out["stale_sources"] + out["untimestamped_sources"]
           + list(out.get("missing_sources") or []))
    if bad:
        out["note"] = (
            "NOT all log sources can be time-aligned to the imagery: "
            + "; ".join(f"{s['label']}={s['verdict']} ({s['why']})"
                        for s in out["sources"]
                        if s["verdict"] in ("stale", "untimestamped",
                                            "self-emitted-only", "missing"))
            + ". Treat frame↔log pairings involving these sources as ORDERING "
              "only, and do not read their content as concurrent with a frame.")
    elif not out["sources"]:
        # Zero sources also reached the all-clear. "Every declared source is
        # sound" is vacuously true of none, and reads as a health statement.
        out["note"] = ("NO log sources are declared for this profile, so there is "
                       "nothing to align against the imagery — this is not a "
                       "clean bill of health, it is an absence of evidence")
    else:
        out["note"] = ("every declared source is timestamped and current with "
                       "the reference clock — frame↔log pairing is sound")
    return out


_SIG_NUM = re.compile(r"0x[0-9A-Fa-f]+|\b\d[\d.,:]*\b")


_CHANNEL_TAG = re.compile(r"^(\s*\[[A-Za-z0-9_.\-]+\])+\s*")


def _event_signature(ev: dict) -> str:
    """A stable shape for 'the same kind of line', with numbers/addresses masked
    so 180 repeats of one event collapse to one signature. Includes the source, so
    it is only meaningful WITHIN one log source."""
    d = ev.get("data") or {}
    name = d.get("name")
    if name:
        return f"event:{name}"
    msg = str(d.get("message") or ev.get("raw") or "")[:160]
    src = str(ev.get("source") or "")
    return f"{src}:{_SIG_NUM.sub('#', msg).strip()}"


def _cross_signature(ev: dict) -> str:
    """Source-AGNOSTIC shape, for asking whether two DIFFERENT log sources carry
    the same event.

    `_event_signature` embeds the source field, and adapters derive that field
    differently per source: the identical loader line normalises to
    `LOADER:TryLoadTableBytes...` from log.txt and to
    `sharpemuloader:[LOADER] TryLoadTableBytes...` from actions.jsonl. Comparing
    those, nothing ever matched, and the overlap report concluded the two sources
    were "completely disjoint" — a confident claim produced entirely by its own
    key format, while 19 loader lines were in fact present in both. So the
    cross-source key drops the source, strips leading [CHANNEL] tags, masks
    numbers and case-folds.
    """
    d = ev.get("data") or {}
    name = d.get("name")
    if name:
        return f"event:{str(name).lower()}"
    msg = str(d.get("message") or ev.get("raw") or "")[:160]
    msg = _CHANNEL_TAG.sub("", msg)
    return _SIG_NUM.sub("#", msg).strip().lower()


def _source_event_map(profile, limit: int = 8000) -> dict:
    """Which kinds of event live in WHICH log source, and which live in only one.

    A profile can declare several sources, and they are NOT views of the same
    stream. Measured on SharpEmu: all 180 `guestgpu.present ok=False` events exist
    ONLY in log.txt, while actions.jsonl held 1929 `program.stuck` events that
    existed nowhere else (those were AgentVision's own watchdog, which no longer
    writes there — but the disjointness it exposed is general). A reader who
    checks one source and generalises is wrong
    either way — and both of AgentVision's own failure paths did exactly that
    (`_detect_failure_records` reads only actions.jsonl; the frame's `_ai` block
    points at the text log). Naming the disjointness is what stops that.
    """
    out: dict = {"sources": [], "shared_signatures": 0, "note": ""}
    try:
        srcs = _log_sources.effective_sources(profile)
    except Exception as exc:
        out["error"] = str(exc)
        return out
    per: dict[str, dict[str, int]] = {}
    for src in srcs:
        label = src.get("label") or "?"
        try:
            evs = _log_sources.read_normalized(profile, window=None,
                                               limit=limit, label=label)
        except Exception:
            evs = []
        c: dict[str, int] = {}
        for ev in evs:
            s = _cross_signature(ev)
            c[s] = c.get(s, 0) + 1
        per[label] = c
    # A signature is "exclusive" when exactly one source ever produces it.
    owners: dict[str, list[str]] = {}
    for label, c in per.items():
        for s in c:
            owners.setdefault(s, []).append(label)
    shared = [s for s, ls in owners.items() if len(ls) > 1]
    out["shared_signatures"] = len(shared)
    for label, c in per.items():
        excl = {s: n for s, n in c.items() if len(owners.get(s, [])) == 1}
        top = sorted(excl.items(), key=lambda kv: -kv[1])[:5]
        out["sources"].append({
            "label": label,
            "events": sum(c.values()),
            "distinct_signatures": len(c),
            "exclusive_signatures": len(excl),
            "top_exclusive": [{"signature": s[:110], "count": n}
                              for s, n in top],
        })
    labels = [s["label"] for s in out["sources"]]
    out["shared_examples"] = [s[:110] for s in sorted(shared)[:5]]
    if len(labels) > 1 and not shared:
        out["note"] = (
            f"The {len(labels)} declared source(s) {labels} share no event "
            f"signature after channel-tag and number masking. Checking one and "
            f"generalising to the program is unsound; each source's exclusive "
            f"events are listed above.")
    elif len(labels) > 1:
        out["note"] = (
            f"{len(shared)} event signature(s) appear in more than one of "
            f"{labels}, and the rest are exclusive to a single source — so no one "
            f"source is a complete view of the program, and a count taken from one "
            f"source is NOT the program's total. Per-source `exclusive_signatures` "
            f"is what each log alone would tell you.")
    elif len(labels) == 1:
        out["note"] = "only one log source is declared for this profile"
    else:
        out["note"] = ("no log sources are declared for this profile — there is "
                       "nothing to cross-reference")
    return out


_KV = re.compile(r"([A-Za-z_][\w.]*)=([^\s,;]+)")
#: Field names whose value states whether the operation succeeded.
_OUTCOME_KEYS = ("ok", "success", "succeeded", "result", "status", "passed")
_TRUEISH = ("true", "1", "yes", "success", "succeeded", "ok")
_FALSEISH = ("false", "0", "no", "fail", "failed", "error")
#: A categorical (named) value — starts with a letter and contains no digits, so
#: `cpu` / `guestgpu` / `coreaudio` qualify and `0x5D80000` / `1280x720` do not.
_CATEGORICAL = re.compile(r"^[A-Za-z][A-Za-z_.\-]*$")


def _kv_fields(msg: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in _KV.finditer(msg)}


def _outcome_of(fields: dict[str, str]) -> bool | None:
    for k in _OUTCOME_KEYS:
        for fk, fv in fields.items():
            if fk.lower() != k:
                continue
            v = fv.strip().lower()
            if v in _TRUEISH:
                return True
            if v in _FALSEISH:
                return False
    return None


def _subject_of(msg: str) -> str:
    """The message with every `k=v` VALUE masked, so the failing and succeeding
    forms of the same operation land in one group:
      `guestgpu.present src=0x5D80000 ok=False`  ->  `guestgpu.present src=? ok=?`
      `guestgpu.present src=0xB840000 ok=True`   ->  `guestgpu.present src=? ok=?`
    """
    return _KV.sub(lambda m: f"{m.group(1)}=?", msg).strip()[:160]


def _recovery_report(profile, limit: int = 8000, max_subjects: int = 6) -> dict:
    """Did the thing that was failing start succeeding again — and what changed?

    "How many times, and did it recover?" is the question a flight recorder is
    for, and AgentVision had no representation of it: failures were counted
    (`_text_log_failures`) and successes were ignored, so a run that failed 180×
    and then worked read identically to one that failed 180× and stayed broken.

    For each operation that has BOTH failing and succeeding instances, this pairs
    the last failure with the next success and reports:
      * how many times it failed and then succeeded,
      * `recovered`: whether the LAST instance succeeded,
      * `changed_fields`: which fields differ between the last failure and the
        first success — the mechanical difference between broken and working,
      * `between`: the log lines in the gap, which is where the cause usually is.

    On SharpEmu this derives, with no knowledge of Metal, that
    `guestgpu.present src=? ok=?` failed 180× on src=0x5D80000, then succeeded 8×
    on src=0xB840000, that it therefore RECOVERED, and that a
    `staging 1280x720 (3 MB, page-aligned, zero-copy)` line sits in the gap.
    """
    out: dict = {"operations": [], "note": ""}
    try:
        srcs = _log_sources.effective_sources(profile)
    except Exception as exc:
        out["error"] = str(exc)
        return out
    for src in srcs:
        label = src.get("label") or "?"
        try:
            evs = _log_sources.read_normalized(profile, window=None,
                                               limit=limit, label=label)
        except Exception:
            continue
        # Ordered records with their outcome, plus a plain line list for the gap.
        recs: list[dict] = []
        for i, ev in enumerate(evs):
            d = ev.get("data") or {}
            msg = str(d.get("message") or ev.get("raw") or "")
            fields = _kv_fields(msg)
            recs.append({"i": i, "msg": msg, "fields": fields,
                         "outcome": _outcome_of(fields),
                         "subject": _subject_of(msg),
                         "source": ev.get("source"), "ts_ms": ev.get("ts_ms")})
        # ── mode transitions ────────────────────────────────────────────────
        # A field that takes exactly two values with exactly ONE switch between
        # them is a mode change, and mode changes are usually the story: the
        # SharpEmu composite ran `via=cpu` and then `via=guestgpu`, i.e. a CPU
        # FALLBACK covered the failing GPU path and then the GPU path took over.
        # Both lines say ok=True, so no failure count can ever reveal that —
        # counting failures alone reports "180 failures, then fine" and misses
        # that the screen was being produced the whole time, by a different path.
        allsub: dict[str, list[dict]] = {}
        for r in recs:
            allsub.setdefault(r["subject"], []).append(r)
        for subject, rs in allsub.items():
            if not (2 <= len(rs) <= 5000):
                continue
            keys = set()
            for r in rs:
                keys.update(r["fields"].keys())
            for k in sorted(keys):
                if k.lower() in _OUTCOME_KEYS:
                    continue
                seq = [r["fields"].get(k) for r in rs]
                if any(v is None for v in seq):
                    continue
                distinct = list(dict.fromkeys(seq))
                if len(distinct) != 2:
                    continue
                # A MODE is a named alternative (`via=cpu` -> `via=guestgpu`); an
                # address or size that merely differs between two calls is not.
                # Without this the detector "found" a mode switch every time a
                # two-occurrence line carried a different hex value — 8 confident
                # findings on this log, all of them junk. Categorical values only.
                if not all(_CATEGORICAL.match(v or "") for v in distinct):
                    continue
                if any((v or "").lower() in _TRUEISH + _FALSEISH
                       for v in distinct):
                    continue
                switches = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
                if switches != 1:
                    continue
                n_before = seq.index(distinct[1])
                n_after = len(seq) - n_before
                # Weight, stated rather than implied. One occurrence either side
                # is two values in a row, which MAY be a mode switch and may just
                # be two different values; only repetition on both sides makes it
                # a pattern. Reporting these identically would be false precision.
                weight = "suggestive" if (n_before >= 3 and n_after >= 3) else "weak"
                _subs = sorted({str(r.get("source") or "")
                                for r in rs if r.get("source")})
                out.setdefault("field_switches", []).append({
                    "log_source": label,
                    "subsystem": _subs[0] if len(_subs) == 1 else _subs,
                    "operation": subject,
                    "field": k,
                    "from": distinct[0],
                    "to": distinct[1],
                    "occurrences_before": n_before,
                    "occurrences_after": n_after,
                    "evidence_weight": weight,
                    "reading": (
                        f"'{subject}' logged {k}={distinct[0]} {n_before}x, then "
                        f"{k}={distinct[1]} for the remaining {n_after}x, with a "
                        f"single switch between them"
                        + ("" if weight == "suggestive" else
                           " — only %dx/%dx either side, so this may simply be two "
                           "different values rather than a mode change"
                           % (n_before, n_after))),
                })

        groups: dict[str, list[dict]] = {}
        for r in recs:
            if r["outcome"] is None:
                continue
            groups.setdefault(r["subject"], []).append(r)
        for subject, rs in groups.items():
            fails = [r for r in rs if r["outcome"] is False]
            oks = [r for r in rs if r["outcome"] is True]
            if not fails or not oks:
                continue
            last_fail = fails[-1]
            after = [r for r in oks if r["i"] > last_fail["i"]]
            first_ok = after[0] if after else None
            # The SUBSYSTEM that emitted these lines, kept explicit. This is the
            # field that stops adjacent lines from different subsystems being read
            # as one event: in the SharpEmu log, line 256 is
            # `[METAL] guestgpu.present ... ok=False` and line 257 is
            # `[PRESENT] metal.composite ... ok=True via=cpu` — two subsystems,
            # both matching "ok=", and a reader who merged them concluded the
            # first present had SUCCEEDED. Grouping by subject and naming the
            # channel makes that conflation structurally impossible.
            subs = sorted({str(r.get("source") or "") for r in rs if r.get("source")})
            op: dict = {
                "log_source": label,
                "subsystem": subs[0] if len(subs) == 1 else subs,
                "operation": subject,
                "failed": len(fails),
                "succeeded": len(oks),
                "recovered": bool(rs[-1]["outcome"]),
                "first_failure_line": fails[0]["msg"][:160],
                "last_failure_line": last_fail["msg"][:160],
            }
            # Distinguishing values: what was constant across the failures, and
            # what it became on success.
            if first_ok:
                changed = []
                for k, v in first_ok["fields"].items():
                    if k.lower() in _OUTCOME_KEYS:
                        continue
                    was = last_fail["fields"].get(k)
                    if was is not None and was != v:
                        changed.append({"field": k, "while_failing": was,
                                        "when_it_worked": v})
                op["first_success_line"] = first_ok["msg"][:160]
                op["changed_fields"] = changed
                # The gap: what happened between the final failure and the first
                # success. Capped, and reported verbatim rather than summarised.
                gap = [r["msg"][:160] for r in recs
                       if last_fail["i"] < r["i"] < first_ok["i"]]
                op["lines_between_last_failure_and_first_success"] = gap[:8]
                op["gap_line_count"] = len(gap)
                if changed:
                    op["reading"] = (
                        "; ".join(f"{c['field']} changed "
                                  f"{c['while_failing']} -> {c['when_it_worked']}"
                                  for c in changed)
                        + (f", with {len(gap)} line(s) in between"
                           if gap else " with nothing logged in between")
                        + ". Correlation only — the log shows the change, not that "
                          "it caused the fix.")
            else:
                op["note"] = ("every success PRECEDES the last failure — this "
                              "operation ended in the failing state")
            out["operations"].append(op)
    out["operations"].sort(key=lambda o: -(o.get("failed") or 0))
    out["operations"] = out["operations"][:max_subjects]
    if out.get("field_switches"):
        # Strongest first, and say what the list is for.
        out["field_switches"].sort(
            key=lambda s: (s.get("evidence_weight") != "suggestive",
                           -(s.get("occurrences_after") or 0)))
        out["field_switches"] = out["field_switches"][:8]
        out["field_switches_note"] = (
            "Fields that took one value and then another, with a single switch. "
            "A change of MODE shows up here when a success/failure count cannot "
            "show it at all — e.g. an operation that logs ok=True both times but "
            "did the work a different way. Check evidence_weight before relying "
            "on any row.")
    if out["operations"]:
        rec = [o for o in out["operations"] if o.get("recovered")]
        out["note"] = (
            f"{len(out['operations'])} operation(s) both failed and succeeded in "
            f"this log; {len(rec)} ended in the succeeding state. `changed_fields` "
            f"is the mechanical difference between the broken and working calls, "
            f"and `lines_between_...` is the untouched log gap — read those rather "
            f"than trusting this summary.")
    else:
        out["note"] = ("no operation in the declared log sources has both a "
                       "failing and a succeeding instance, so no recovery can be "
                       "asserted either way")
    return out


#: severity -> confidence, used only when a hypothesis did not state its own.
_SEVERITY_CONF = {"high": 0.8, "medium": 0.55, "low": 0.3}


def _compact_freshness(fr: dict) -> dict:
    """The verdicts without the per-source detail. /digest advertises itself as
    token-light, so it carries the finding; av_diagnose carries the workings."""
    return {
        "verdicts": {s.get("label"): s.get("verdict")
                     for s in (fr.get("sources") or [])},
        "stale_sources": list(fr.get("stale_sources") or []),
        "untimestamped_sources": list(fr.get("untimestamped_sources") or []),
        "alignable_sources": list(fr.get("alignable_sources") or []),
        "program_silent_s": fr.get("program_silent_s"),
        "note": fr.get("note"),
        "full_detail": "av_diagnose() -> log_sources_freshness",
    }


def _compact_recovery(rc: dict) -> dict:
    """One line per operation that both failed and succeeded."""
    ops = []
    for o in (rc.get("operations") or [])[:4]:
        ops.append({
            "operation": o.get("operation"),
            "failed": o.get("failed"),
            "succeeded": o.get("succeeded"),
            "recovered": o.get("recovered"),
            "what_changed": o.get("reading") or o.get("note"),
        })
    out = {"operations": ops,
           "full_detail": "av_diagnose() -> recovery"}
    if rc.get("field_switches"):
        out["field_switches"] = [
            {"operation": s.get("operation"), "field": s.get("field"),
             "from": s.get("from"), "to": s.get("to"),
             "evidence_weight": s.get("evidence_weight")}
            for s in rc["field_switches"][:4]]
    if not ops:
        out["note"] = rc.get("note")
    return out


def _normalize_hypotheses(hyps: list[dict]) -> list[dict]:
    """Give every hypothesis the same documented shape, whatever built it.

    /diagnose's contract is {summary, confidence, evidence, probable_cause,
    recommended_next}, but hypotheses were assembled by several independent code
    paths and two of them emitted {hypothesis, severity, next} instead. Nothing
    reconciled them, so which keys a caller got depended on which finding happened
    to rank first — a shape the reader cannot rely on is a shape they will
    misread. Every hypothesis also has to carry an explicit `confidence`: without
    one, a directly measured fact and an inference are presented identically.
    """
    out: list[dict] = []
    for i, h in enumerate(hyps or []):
        h = dict(h)
        h["rank"] = i + 1
        # ONE canonical name per field. Emitting `summary` and `hypothesis`, and
        # `next` and `recommended_next`, and `probable_cause` duplicating
        # `evidence`, tripled this block to 5.4 KB of the same strings — length
        # that carries no information is its own accuracy problem, because it
        # buries the fields that do.
        h["summary"] = h.pop("hypothesis", None) or h.get("summary") or ""
        # `evidence` is the field a reader checks a claim against, so it is never
        # allowed to be absent; when a builder supplied none, say that plainly
        # rather than leaving the key missing and letting it read as unchecked.
        if not h.get("evidence"):
            h["evidence"] = (h.get("probable_cause")
                             or "no supporting evidence was recorded for this "
                                "hypothesis — treat it as unverified")
        nxt = h.pop("next", None) or h.get("recommended_next") or []
        h["recommended_next"] = nxt
        # probable_cause is only worth a key when it says something evidence
        # does not.
        if not h.get("probable_cause"):
            h["probable_cause"] = h["summary"]
        if h["probable_cause"] == h["evidence"]:
            h["probable_cause"] = h["summary"]
        c = h.get("confidence")
        try:
            c = float(c)
        except (TypeError, ValueError):
            c = None
        if c is None:
            c = _SEVERITY_CONF.get(str(h.get("severity") or "").lower(), 0.5)
            h["confidence_basis"] = (
                f"derived from severity={h.get('severity')!r}; this hypothesis did "
                f"not state a measured confidence")
        h["confidence"] = max(0.0, min(1.0, c))
        out.append(h)
    return out


def _alignment_health(frame: dict, freshness: dict | None = None) -> dict:
    """Compact alignment health for a frame — is image↔log correlation sound?

    `freshness` is _source_freshness(). Without it this function reported
    aligned=True from the action log alone, which on SharpEmu meant certifying
    1788 of AgentVision's OWN watchdog heartbeats against its own shutter — a
    self-referential check that cannot fail and says nothing about whether the
    program's real log (a separate, untimestamped, 47 h-stale file) lines up.
    """
    if not frame:
        return {"available": False}
    shutter_ms = float(frame.get("timestamp_ms") or 0)
    cm = frame.get("capture_meta") or {}
    pinned = frame.get("profile_action_log") or ""
    offset = int(frame.get("action_log_offset") or 0)
    leaked = 0
    try:
        recs = _read_action_jsonl(path_override=pinned, max_offset=offset, limit=100000)
        for r in recs:
            rms = r.get("ts_ms") or _iso_to_ms(r.get("ts", "")) or 0.0
            if shutter_ms and rms > shutter_ms + 50.0:
                leaked += 1
    except Exception:
        pass
    fr = freshness or {}
    stale = list(fr.get("stale_sources") or [])
    untimed = list(fr.get("untimestamped_sources") or [])
    out = {
        "available": True,
        # Leak check: nothing bounded into the frame postdates the shutter. This
        # is necessary but NOT sufficient for "image and logs line up".
        "no_future_records": leaked == 0,
        "leaked_after_shutter": leaked,
        "capture_latency_ms": cm.get("capture_latency_ms"),
        "black_frame": cm.get("black_frame"),
        "window_found": cm.get("window_found"),
        # Which sources can actually carry an alignment claim.
        "stale_sources": stale,
        "untimestamped_sources": untimed,
        "alignable_sources": list(fr.get("alignable_sources") or []),
        "program_silent_s": fr.get("program_silent_s"),
    }
    # `aligned` is only allowed to be True when the leak check passed AND every
    # declared source could actually be time-aligned. Otherwise it is False with
    # the reason attached — an unqualified True here is what let a 47 h-stale log
    # be presented as concurrent with a fresh frame.
    if leaked == 0 and not stale and not untimed:
        out["aligned"] = True
        out["alignment_verdict"] = "sound"
        out["note"] = ("no record bounded into this frame postdates the shutter "
                       "and every source is timestamped and current")
    else:
        out["aligned"] = False
        out["alignment_verdict"] = ("leak" if leaked else "unalignable_sources")
        out["note"] = fr.get("note") or (
            f"{leaked} record(s) bounded into this frame postdate its shutter")
    return out


def _text_log_failures(profile, limit: int = 4000) -> dict:
    """Failure-shaped lines from the LOG-SOURCE pipeline, grouped and counted.

    `_detect_failure_records` reads ONLY actions.jsonl. That made every failure
    invisible on a program whose errors live in a text log — measured on SharpEmu,
    whose Metal layer logged `guestgpu.present ok=False` 180 times to log.txt while
    actions.jsonl contained zero such events. Result: total_failures 0, no
    hypotheses, and a 100/100 "healthy" grade over a GPU path that failed 180
    times. The two paths must agree, so failure detection has to read what the
    warning path reads.

    Counts a line as failure-shaped when the adapter said ERROR/FATAL/CRITICAL, or
    when content-severity ESCALATED it because the text itself says it failed
    (see connectors.log_adapters.escalate_by_content).
    """
    out = {"total": 0, "groups": [], "escalated": 0}
    try:
        evs = _log_sources.read_normalized(profile, window=None, limit=limit)
    except Exception as exc:
        _dbg(f"text-log failure scan failed: {exc}")
        return out
    groups: dict[tuple, dict] = {}
    for ev in evs:
        lvl = str(ev.get("level") or "").upper()
        esc = bool(ev.get("level_escalated_from"))
        hard = lvl in ("ERROR", "FATAL", "CRITICAL")
        if not (hard or esc):
            continue
        d = ev.get("data") or {}
        msg = str(d.get("message") or d.get("name") or ev.get("raw") or "")[:140]
        key = (ev.get("source") or ev.get("log_label"), msg)
        g = groups.get(key)
        if g is None:
            groups[key] = {"source": key[0], "message": msg, "count": 1,
                           "level": lvl, "escalated": esc,
                           "escalation_reason": ev.get("escalation_reason") or "",
                           "first_ts_ms": ev.get("ts_ms"),
                           "last_ts_ms": ev.get("ts_ms"),
                           "log_label": ev.get("log_label")}
        else:
            g["count"] += 1
            g["last_ts_ms"] = ev.get("ts_ms")
        out["total"] += 1
        if esc:
            out["escalated"] += 1
    out["groups"] = sorted(groups.values(), key=lambda g: -g["count"])[:10]
    return out


def _health_block(latest: dict | None, new_fps: list, top_errors: list,
                  engine: "AutoCaptureEngine",
                  text_failures: dict | None = None,
                  recovery: dict | None = None,
                  running_now: bool | None = None,
                  program_silent_s: float | None = None) -> dict:
    """Deterministic 0–100 health score + the factors that lowered it. Shared by
    /digest and /diagnose so both agree on 'how healthy is this program right now'.
    Pure function of already-computed signals — no I/O, never raises.

    `text_failures` is _text_log_failures(). Passing it is what stops the score
    reporting "healthy, no deductions" while the response's own recent_warnings
    block carries a 180× failure. A signal the program collects and then does not
    score is worse than one it never collected: it reads as an all-clear.
    """
    score = 100
    factors: list[str] = []
    tf = text_failures or {}
    if tf.get("total"):
        worst = (tf.get("groups") or [{}])[0]
        n = int(worst.get("count") or 0)
        # Same shape as the top_errors deduction so a text-log failure and a JSONL
        # failure of equal weight score identically.
        d = min(35, max(10, n // 5))
        why = (f" — {worst.get('escalation_reason')}"
               if worst.get("escalated") else "")
        # Did that operation later succeed? A run that failed 180x and then worked
        # is not in the same state as one still failing, and grading them the same
        # is itself an inaccurate read. The failures are still counted — the
        # deduction is halved and the recovery is named, not hidden.
        recovered_op = None
        try:
            wsub = _subject_of(str(worst.get("message") or ""))
            for op in ((recovery or {}).get("operations") or []):
                if op.get("operation") == wsub and op.get("recovered"):
                    recovered_op = op
                    break
        except Exception:
            recovered_op = None
        if recovered_op:
            d = max(5, d // 2)
            why += (f"; that operation then succeeded "
                    f"{recovered_op.get('succeeded')}x and ended in the "
                    f"succeeding state (RECOVERED)")
        score -= d
        factors.append(
            f"{tf['total']} failure-shaped log line(s), worst repeated {n}× "
            f"({worst.get('source') or '?'}: {str(worst.get('message'))[:60]})"
            f"{why} (-{d})")
    # Liveness from a LIVE check, falling back to the frame only when no live
    # check was supplied. The frame's `running` flag is a record of the shutter
    # moment: on a 19 h-old frame from an exited program it was still True, so
    # this deduction never fired and the program graded "healthy" while dead.
    _dead = ((running_now is False) if running_now is not None
             else bool(latest and not (latest.get("program") or {}).get("running")))
    if _dead:
        score -= 40
        factors.append("program not running (-40)"
                       if running_now is False else
                       "program not running as of the last frame (-40)")
    if program_silent_s is not None and program_silent_s > SOURCE_STALE_S:
        # A silent program is not a healthy one, and the imagery cannot tell you
        # otherwise: frames kept being captured for 23 h after this program's last
        # log line, every one of them pinned to the same frozen tail.
        score -= 15
        factors.append(
            f"program has emitted no log output for "
            f"{_human_age(program_silent_s)} — this read is post-mortem (-15)")
    if new_fps:
        d = min(30, 10 * len(new_fps)); score -= d
        factors.append(f"{len(new_fps)} new error fingerprint(s) (-{d})")
    if top_errors:
        d = min(20, top_errors[0]["count"]); score -= d
        factors.append(f"top error recurred {top_errors[0]['count']}× (-{d})")
    if latest and "stuck" in (latest.get("tags") or []):
        score -= 15; factors.append("screen stuck (-15)")
    if latest and (latest.get("anomaly") or {}).get("detected"):
        score -= 10; factors.append("anomaly detected (-10)")
    if engine.window_missing:
        score -= 10; factors.append("capture window missing (-10)")
    if engine.blank_frame_count:
        score -= 10; factors.append("blank frame(s) captured (-10)")
    score = max(0, min(100, score))
    grade = ("healthy" if score >= 80 else "degraded" if score >= 50
             else "unhealthy" if score >= 20 else "critical")
    out = {"score": score, "grade": grade,
           "factors": factors or ["no deductions"]}
    # A grade for a program that is not running describes a RECORDING, not a live
    # system. Without saying so, "unhealthy" reads as "it is failing right now" —
    # which is a different claim, and the wrong one here.
    if _dead:
        out["basis"] = (
            "POST-MORTEM: the program is not running, so this grades the recorded "
            "run and the not-running state itself — it is not a measurement of a "
            "live program.")
    else:
        out["basis"] = "live: the program is running and was measured as it ran"
    return out


@app.route("/digest", methods=["GET"])
def digest():
    """AI TRIAGE digest — the FIRST thing a model should read. One compact,
    ranked JSON: what is broken, what changed, capture + alignment health, and
    where to look next. Deliberately token-light (summaries, capped lists)."""
    from shared.schema.snapshot_schema import SCHEMA_VERSION
    collector = _get_collector()
    e = _auto_engine
    with _lock:
        latest = _latest_for_active()
        frame_count = len(_frames)

    # ── Top errors, ranked by occurrence (dedup by fingerprint) ──────────────
    triggers = _detect_failure_records(limit=3000)
    hist: dict[str, dict] = {}
    for r in triggers:
        fp = _record_fingerprint(r)
        d = r.get("data") or {}
        if fp not in hist:
            sample = (d.get("message") or d.get("name") or d.get("error")
                      or r.get("source") or "")
            hist[fp] = {"fingerprint": fp, "count": 0, "sample": str(sample)[:140],
                        "source": r.get("source"), "first_ts": r.get("ts"),
                        "last_ts": r.get("ts")}
        hist[fp]["count"] += 1
        hist[fp]["last_ts"] = r.get("ts")
    top_errors = sorted(hist.values(), key=lambda x: -x["count"])[:5]

    # ── New-this-session fingerprints (highest-priority: just appeared) ──────
    new_fps = []
    try:
        history = _load_fp_history()
        for r in triggers:
            ts_ms = r.get("ts_ms") or _iso_to_ms(r.get("ts", "")) or 0
            if ts_ms >= _session_start_ms:
                fp = _record_fingerprint(r)
                if fp and fp not in history and fp not in new_fps:
                    new_fps.append(fp)
    except Exception:
        pass

    # ── Freshness of every declared log source, vs the newest frame ──────────
    # Computed BEFORE the views below so they can qualify their own claims.
    _fresh = _source_freshness(
        collector.profile,
        ref_ms=float(latest.get("timestamp_ms") or 0) or None if latest else None)
    # Liveness must come from the SAME call /program/status uses, or the two
    # endpoints disagree about whether the program is alive. (`profile` is a
    # dataclass and has no is_running; reading it there silently returned None
    # and let the stale frame's running=True stand.)
    _running_now = None
    try:
        _running_now = bool(collector._reader.is_running())
    except Exception:
        pass

    # ── Health score (0–100, bounded) + the factors that lowered it ──────────
    # Computed HERE, before the attention list, because attention is not allowed
    # to issue an all-clear that contradicts this grade.
    # Reads the SAME failure surface the warnings block reads. Scoring only
    # actions.jsonl is what let this report "healthy, no deductions" on a program
    # whose 180 failures were all in its text log.
    _txt_fail = _text_log_failures(collector.profile)
    _recov = _recovery_report(collector.profile)

    # ── Latest-frame triage (summary/recommended_next/tags/confidence) ───────
    latest_view = None
    if latest:
        err = latest.get("error") or {}
        anom = latest.get("anomaly") or {}
        # A frame's own self-assessment ("Running normally", tags=[healthy],
        # running=True, live cpu/rss numbers) is a statement about the MOMENT THE
        # SHUTTER FIRED, not about now. Reported without an age it reads as
        # present tense: on this profile a 12 h-old frame from an exited program
        # was presented as "Running normally; no notable change", tagged healthy,
        # with 118% CPU. So every latest-frame view carries its age, and any
        # assertion the current world contradicts is demoted to as-of.
        _age_s = None
        _ts = float(latest.get("timestamp_ms") or 0)
        if _ts:
            _age_s = round((time.time() * 1000.0 - _ts) / 1000.0, 1)
        _stale_frame = bool(_age_s and _age_s > SOURCE_STALE_S)
        _summary = latest.get("summary")
        _tags = list(latest.get("tags") or [])
        _rec_next = latest.get("recommended_next")
        _frame_running = (latest.get("program") or {}).get("running")
        _contradicted = (_running_now is False and _frame_running is True)
        if _stale_frame or _contradicted:
            _pfx = (f"AS OF {_human_age(_age_s)} AGO"
                    if _age_s else "AS OF THE LAST FRAME")
            if _summary:
                _summary = f"[{_pfx}] {_summary}"
            if _contradicted:
                _summary = ((_summary or "") +
                            " — CONTRADICTED NOW: the program is not running.")
                if "healthy" in _tags:
                    _tags = [t for t in _tags if t != "healthy"] + [
                        "healthy_at_capture_only"]
                _rec_next = ("The program has exited since this frame. Read this "
                             "as post-mortem: av_program_status(), then "
                             "av_diagnose().")
            elif "healthy" in _tags:
                _tags = [t for t in _tags if t != "healthy"] + [
                    "healthy_at_capture_only"]
        latest_view = {
            "sequence":         latest.get("sequence"),
            "timestamp_ms":     latest.get("timestamp_ms"),
            "age_s":            _age_s,
            "age_human":        _human_age(_age_s) if _age_s else None,
            "stale":            _stale_frame,
            "as_of_only": ("Every field below describes the shutter moment, not "
                           "now." if _stale_frame or _contradicted else False),
            "summary":          _summary,
            "recommended_next": _rec_next,
            "tags":             _tags,
            "confidence":       latest.get("confidence"),
            # What the frame recorded, and what is true now — kept separate.
            "running_at_capture": _frame_running,
            "running_now":      _running_now,
            "running":          (_running_now if _running_now is not None
                                 else _frame_running),
            "error": {"exception_type": err.get("exception_type"),
                      "message": (err.get("message") or "")[:140],
                      "probable_cause": err.get("probable_cause"),
                      "occurrence_count": err.get("occurrence_count"),
                      "fingerprint": err.get("fingerprint")} if err else None,
            "anomaly": {"type": anom.get("type"), "severity": anom.get("severity"),
                        "confidence": anom.get("confidence")} if anom.get("detected") else None,
            "state_delta": {k: latest.get("state_delta", {}).get(k)
                            for k in ("changed_count", "added_count", "removed_count")},
            "perf": {k: (latest.get("perf") or {}).get(k)
                     for k in ("cpu_percent", "rss_mb", "num_threads")} if latest.get("perf") else None,
        }
        # Compact human note of the top few state changes (token-light).
        sd = latest.get("state_delta") or {}
        changed = sd.get("changed") or {}
        if changed:
            parts = [f"{k}: {v.get('from')}→{v.get('to')}"
                     for k, v in list(changed.items())[:4]]
            more = "" if len(changed) <= 4 else f" (+{len(changed) - 4} more)"
            latest_view["state_change_note"] = "; ".join(parts) + more

    # ── Rank what to look at first ───────────────────────────────────────────
    attention = []
    if _running_now is False or (latest and
                                 not (latest.get("program") or {}).get("running")):
        attention.append("Program is NOT running — see latest_frame + av_program_log.")
    # Stale / unalignable log sources belong at the TOP of triage: every other
    # item in this digest is derived from those sources, so if they are not
    # current the rest of the read is about the past, not the present.
    for _s in (_fresh.get("sources") or []):
        if _s.get("verdict") in ("stale", "self-emitted-only"):
            attention.append(
                f"Log source '{_s['label']}' is {_s['verdict'].upper()}: "
                f"{_s['why']}. Anything below derived from it describes the past "
                f"— av_log_sources().")
        elif _s.get("verdict") == "untimestamped":
            attention.append(
                f"Log source '{_s['label']}' has NO timestamps "
                f"({_s.get('untimestamped')} event(s)), so its content cannot be "
                f"time-aligned to any frame — treat frame↔log pairings from it as "
                f"ORDERING only.")
        elif _s.get("verdict") == "missing":
            attention.append(
                f"Log source '{_s['label']}' is declared in the profile but "
                f"MISSING on disk ({_s.get('path')}) — nothing from it is in "
                f"this read.")
    if _fresh.get("program_silent_s") is not None and \
            _fresh["program_silent_s"] > SOURCE_STALE_S:
        attention.append(
            f"The PROGRAM has written nothing to any log for "
            f"{_human_age(_fresh['program_silent_s'])}. A log file's mtime can "
            f"move for reasons other than the program writing to it, so source "
            f"freshness alone does not mean the program is alive.")
    if new_fps:
        attention.append(f"{len(new_fps)} NEW error fingerprint(s) this session — "
                         "av_new_errors_this_session, then av_errors_by_fingerprint(fp).")
    if top_errors:
        attention.append(f"Top recurring error ({top_errors[0]['count']}×): "
                         f"{top_errors[0]['sample'][:80]}")
    if latest and "stuck" in (latest.get("tags") or []):
        attention.append("Screen appears STUCK — compare recent frames.")
    if e.window_missing:
        attention.append("Capture window not found — screenshots may be full-screen. "
                         "Check the profile's capture_app.")
    if e.blank_frame_count:
        attention.append(f"{e.blank_frame_count} blank/black frame(s) captured — "
                         "the window may be minimized/occluded.")
    # VISUAL signals — found for free at capture time by the change engine.
    with _lock:
        vis_events = [dict(x) for x in _visual_events]
    vis_by_type: dict[str, int] = {}
    for _ev in vis_events:
        vis_by_type[_ev.get("type") or "?"] = vis_by_type.get(_ev.get("type") or "?", 0) + 1
    for _t, _label in (("on_screen_error", "on-screen ERROR text"),
                       ("screen_frozen",   "screen FROZEN (possible hang)"),
                       ("blank_screen",    "blank/black screen")):
        if vis_by_type.get(_t):
            _last = [x for x in vis_events if x.get("type") == _t][-1]
            attention.append(
                f"{vis_by_type[_t]}x {_label} detected visually — "
                f"av_visual_events(type='{_t}'), then "
                f"av_error_moment(seq={_last.get('seq')}).")
    if _incidents:
        _li = _incidents[-1]
        attention.insert(0, (
            f"{len(_incidents)} INCIDENT(S) already frozen by the flight recorder "
            f"(latest: {_li.get('kind')} at seq {_li.get('trigger_seq')}) — "
            f"av_incidents(id='{_li.get('id')}') has the "
            f"{_li.get('pre_error_seconds')}s run-up; "
            f"av_error_moment(seq={_li.get('trigger_seq')}) has the bundle."))
    # The all-clear is only allowed when the health block ALSO grades healthy.
    # Previously this line was emitted purely because none of the checks above
    # fired, so a digest could open with "No urgent issues detected. Program
    # looks healthy." while its own health block said 65/"degraded" over a 180×
    # GPU failure. The field a reader trusts first must not contradict the
    # document it heads.
    _grade_now = _health_block(
        latest, new_fps, top_errors, e, text_failures=_txt_fail,
        recovery=_recov, running_now=_running_now,
        program_silent_s=_fresh.get("program_silent_s")).get("grade") or "unknown"
    if not attention:
        if _grade_now == "healthy":
            attention.append("No urgent issues detected. Program looks healthy.")
        else:
            attention.append(
                f"No frame-level or capture-level issue fired, but health grades "
                f"{_grade_now.upper()} — the deductions are in health.factors "
                f"(they come from the log sources, not the imagery). Start there.")
    elif _grade_now == "healthy":
        pass

    # ── Error-rate / trend signal ────────────────────────────────────────────
    total_failures = len(triggers)
    distinct_fps = len(hist)
    session_failures = sum(
        1 for r in triggers
        if (r.get("ts_ms") or _iso_to_ms(r.get("ts", "")) or 0) >= _session_start_ms)
    recurring = sum(1 for h in hist.values() if h["count"] > 1)
    # Error density over the most recent in-memory frames.
    with _lock:
        recent = sorted(_frames.values(), key=lambda f: f.get("sequence") or 0)[-20:]
    frames_scanned = len(recent)

    def _has_err(f):
        e = f.get("error") or {}
        return bool(e.get("message") or e.get("exception_type"))

    frames_with_error = sum(1 for f in recent if _has_err(f))
    # Trend direction: compare the error rate of the newer half of the recent
    # window vs the older half → rising | steady | falling (needs ≥4 frames).
    trend = "unknown"
    if frames_scanned >= 4:
        half = frames_scanned // 2
        older, newer = recent[:half], recent[half:]
        r_old = sum(1 for f in older if _has_err(f)) / max(1, len(older))
        r_new = sum(1 for f in newer if _has_err(f)) / max(1, len(newer))
        if r_new > r_old + 0.15:
            trend = "rising"
        elif r_new < r_old - 0.15:
            trend = "falling"
        else:
            trend = "steady"
    error_stats = {
        "total_failures":        total_failures,
        "session_failures":      session_failures,
        "distinct_fingerprints": distinct_fps,
        "new_this_session":      len(new_fps),
        "recurring":             recurring,
        "frames_scanned":        frames_scanned,
        "frames_with_error":     frames_with_error,
        "error_rate_recent":     round(frames_with_error / frames_scanned, 3) if frames_scanned else 0.0,
        "trend":                 trend,   # rising | steady | falling | unknown
    }

    health = _health_block(latest, new_fps, top_errors, e,
                           text_failures=_txt_fail, recovery=_recov,
                           running_now=_running_now,
                           program_silent_s=_fresh.get("program_silent_s"))
    _mine_ct, _all_ct = _active_frame_count()

    return jsonify({
        "schema_version": SCHEMA_VERSION,
        "generated_ms":   time.time() * 1000.0,
        "program":        collector.profile.display_name,
        "active_profile": _active_profile_name,
        "health":         health,                  # 0–100 score + factors
        "attention":      attention,               # ranked, read top-down
        "latest_frame":   latest_view,
        "error_stats":    error_stats,             # rate / trend / new-vs-recurring
        "top_errors":     top_errors,
        "new_error_fingerprints": new_fps[:10],
        "capture": {
            "capturing":         e.capturing,
            "shots_per_second":  _fps(e.interval),
            "interval_s":        e.interval,
            # Per-profile, because the frame store is shared across profiles.
            "frames_stored":     _mine_ct,
            "frames_stored_all_profiles": _all_ct,
            "frames_stored_note": (
                f"{_mine_ct} of the {_all_ct} frame(s) held in memory belong to "
                f"'{_active_profile_name}'; the rest belong to other profiles "
                f"(one shared seq-keyed store)."
                if _all_ct != _mine_ct else
                f"all {_all_ct} frame(s) held in memory belong to "
                f"'{_active_profile_name}'"),
            "blank_frame_count": e.blank_frame_count,
            "window_missing":    e.window_missing,
            "frames_skipped_no_window": e.skipped_no_window,
            "frames_while_program_silent": e.frames_while_silent,
            "frame_errors":      e.frame_error_count,
            "last_error":        e.last_error or None,
            "only_the_program": ("Frames contain ONLY the bridged program's "
                                 "window. When it has no window the frame is "
                                 "SKIPPED — the desktop is never captured — so "
                                 "a gap in seq numbers means the program was "
                                 "not showing anything, not that capture broke."),
            "last_warning":      e.last_warning,
            "frame_errors":      e.frame_error_count,
            "last_error":        e.last_error or None,
            "frame_errors_note": (
                "HARD failures of the capture call itself. Nonzero with a low\n"
                "frame_count means capture is BROKEN, not idle — a window it\n"
                "cannot photograph looks identical to a program with nothing\n"
                "to show unless this number is read."),
            "rate_guidance":     ("Ask the user their preferred shots/sec at the start of "
                                  "a project; set via av_capture_set_interval(1/fps)."),
        },
        "alignment":      _alignment_health(latest, freshness=_fresh),
        "log_sources_freshness": _compact_freshness(_fresh),
        "recovery":       _compact_recovery(_recov),
        "flight_recorder": {
            "enabled": RECORDER_ENABLED,
            "incidents_frozen": len(_incidents),
            "incident_window_seconds": RECORDER_WINDOW_S,
            "frames_awaiting_examination": len(_ret.LEDGER.awaiting(limit=100000)),
            "list_them": "av_incidents()",
            "note": ("The window BEFORE each failure is already frozen on disk; "
                     "you do not need to reproduce the bug to see its run-up. "
                     "Frames are held until examined, not deleted on a timer — "
                     "av_retention()."),
        },
        "visual": {
            # PRIMARY, and recomputed from the retained frames — the same basis
            # av_visual_changes uses, so the two cannot disagree.
            #
            # The old primary key was `events_detected: len(vis_events)`, a
            # counter living in THIS bridge process. A restart empties it while
            # every frame and diff is still on disk, so the digest answered
            # "events_detected: 0" — indistinguishable from "the screen never
            # changed" — about the same 1500 frames in which av_visual_changes
            # found 34 changed moments. The key is renamed rather than annotated:
            # a caveat under a number does not stop the number being read.
            "changed_moments": _changed_moment_count(),
            "changed_moments_basis": (
                "recomputed from the active profile's retained frames at the "
                "same threshold av_visual_changes uses"),
            "special_events_detected_this_process": len(vis_events),
            "special_events_caveat": (
                "on_screen_error / screen_frozen / blank_screen detections made "
                "by THIS bridge process at capture time. It resets to 0 on "
                "restart, so 0 here is NOT evidence that none occurred — it is "
                "only evidence that this process did not see them."
                if not vis_events else
                "detected live at capture time by this bridge process."),
            "by_type":         vis_by_type,
            "recent":          vis_events[-3:],
            "review_the_run":  "av_visual_changes()",
            "note": ("The screen is hashed and diffed at capture time for free. "
                     "Review changes as JSON instead of paging through images."),
        },
        "guidance": ("Read this digest FIRST to orient. Check `health.score`, then work "
                     "the `attention` list top-down; each item names the tool to drill "
                     "in with. For the visual side use av_visual_changes (JSON, cheap) "
                     "and escalate to av_frame_region / av_latest_frame only when you "
                     "actually need pixels."),
    }), 200


def _newest_program_record_ms(limit: int = 50_000) -> float | None:
    """Newest timestamp among records the PROGRAM emitted, AgentVision's own
    excluded. The old watchdog compared against the newest record of ANY source
    — including the records it had just written itself — so after its first
    write it was measuring its own heartbeat. Observed on SharpEmu: it reported
    `silent_s: 60.3` on a program that had been silent for 48.8 h, once a minute,
    2024 times. Excluding `agentvision.*` is what makes the number mean what it
    says, and it also repairs the reading of logs already contaminated by the
    old behaviour.

    The default limit is large ON PURPOSE. `_read_action_jsonl` truncates to the
    newest `limit` records BEFORE this filter runs, so on a file the old watchdog
    already filled — 2072 of 2171 records on the log that prompted this — a small
    limit leaves nothing but observer records and returns None, i.e. "the program
    never wrote anything". Measured live at limit=200: None, with a program record
    sitting 2072 lines back. The read parses the whole file either way, so the
    larger window costs nothing.
    """
    recs = _read_action_jsonl(limit=limit)
    stamps = []
    for r in recs:
        if str(r.get("source") or "").startswith(_AV_SELF_EMITTERS):
            continue
        ms = r.get("ts_ms") or _iso_to_ms(r.get("ts", "")) or 0
        if ms:
            stamps.append(ms)
    return max(stamps) if stamps else None


def _stuck_watchdog():
    """Background thread: while the target process is RUNNING but emitting
    nothing for > threshold seconds, record a `program.stuck` observation in
    AgentVision's OWN observer log. Generic — works for any bridged program.

    Two things this deliberately does NOT do, both of them regressions it used
    to have:

      * it does not write into the bridged program's own action log. See the
        _OBSERVER_DIR comment: 95% of SharpEmu's actions.jsonl was this thread,
        and none of it was ever read by the failure detector it claimed to feed.
      * it does not call a process that has EXITED "stuck". An exited program is
        not a hang, and reporting it as one is what let a reader conclude a
        48-hour-dead process was unresponsive-but-alive. Exit is recorded once,
        as `program.exited`, and then the thread goes quiet.
    """
    last_alerted = 0.0
    was_running: bool | None = None
    while True:
        try:
            time.sleep(5.0)
            last_alerted, was_running = _watchdog_tick(last_alerted, was_running)
        except Exception as e:
            _warn(f"watchdog tick error: {e}")


def _watchdog_tick(last_alerted: float,
                   was_running: bool | None,
                   running: bool | None = "probe") -> tuple[float, bool | None]:
    """One watchdog evaluation. Split out from the loop so the decision can be
    tested without a thread or a sleep — the old version could only be checked by
    letting it run against a real log, which is how it shipped writing 2024
    useless records into someone's project.

    `running` defaults to probing the process; pass True/False/None to test.
    """
    now_ms = time.time() * 1000.0
    prof_name = _active_profile_name
    if running == "probe":
        try:
            running = bool(_get_collector()._reader.is_running())
        except Exception:
            running = None          # unknown — cannot claim stuck

    # Liveness transitions are worth one record each, not one a minute.
    if running is not None and was_running is not None and running != was_running:
        _append_observer_record({
            "ts": clock.now_iso(), "ts_ms": now_ms,
            "category": "event", "source": "agentvision.watchdog",
            "data": {"name": "program.started" if running else "program.exited",
                     "profile": prof_name},
        }, prof_name)
        _info(f"watchdog: program {'started' if running else 'exited'}")
        last_alerted = 0.0          # allow a fresh stuck report next run
    if running is not None:
        was_running = running

    newest = _newest_program_record_ms()
    silent_s = (now_ms - newest) / 1000.0 if newest else None
    _observer_state[prof_name or "default"] = {
        "checked_ms":       now_ms,
        "running":          running,
        "newest_program_record_ms": newest,
        "program_silent_s": None if silent_s is None else round(silent_s, 1),
        "stuck":            bool(running and silent_s is not None
                                 and silent_s > _STUCK_THRESHOLD_S),
        "basis": ("silence measured against PROGRAM-emitted records only; "
                  "AgentVision's own records are excluded"),
    }

    # `stuck` requires: process alive, records exist, and they stopped.
    if not running or silent_s is None:
        return last_alerted, was_running
    if silent_s > _STUCK_THRESHOLD_S and (now_ms - last_alerted) > 60_000:
        last_alerted = now_ms
        ok = _append_observer_record({
            "ts":       clock.now_iso(),
            "ts_ms":    now_ms,
            "category": "event",
            "source":   "agentvision.watchdog",
            "state":    None, "run_id": None, "trace_id": None,
            "frame_seq": None, "coords": None,
            "data": {"name": "program.stuck",
                     "silent_s": round(silent_s, 1),
                     "last_seen_ms": newest,
                     "process_running": True,
                     "profile": prof_name},
        }, prof_name)
        if ok:
            _info(f"watchdog: program.stuck after {silent_s:.0f}s "
                  f"(process alive) -> {_observer_log_path(prof_name)}")
    return last_alerted, was_running


@app.route("/frames/collisions", methods=["GET"])
def frame_collisions():
    """Which frame sequence numbers exist in more than one profile.

    A frame's sequence number is unique WITHIN a profile, not across profiles, so
    two programs can each own a frame 1. The in-memory index is keyed by sequence
    alone, so hydrating every profile into it made the second frame 1 unreachable
    — whichever one lost the dict race. The index now holds only the active
    profile; this endpoint is where the rest of the picture lives, so a frame that
    is not addressable right now can still be found and explained.
    """
    with _lock:
        mine = set(_frames.keys()) | set(_frames_on_disk.keys())
        foreign = dict(_foreign)
        shadowed = list(_shadowed)
    by_profile: dict[str, list[int]] = {}
    for (prof, seq), _path in foreign.items():
        by_profile.setdefault(prof, []).append(seq)
    overlap = {prof: sorted(set(seqs) & mine)
               for prof, seqs in by_profile.items()}
    overlap = {p: s for p, s in overlap.items() if s}
    return jsonify({
        "active_profile":       _active_profile_name,
        "indexed_frames":       len(mine),
        "other_profile_frames": len(foreign),
        "other_profiles":       {p: len(s) for p, s in by_profile.items()},
        "colliding_seqs_by_profile": {p: s[:50] for p, s in overlap.items()},
        "colliding_total":      sum(len(s) for s in overlap.values()),
        "shadowed_at_hydrate":  shadowed[:50],
        "why_it_matters": (
            "A sequence number alone does not identify a frame. If you have a seq "
            "from one program's report, it addresses a DIFFERENT image under "
            "another profile. Switch profile (av_set_active_profile) to index "
            "that program's frames."),
        "note": ("The seq-keyed index holds the ACTIVE profile only, so nothing "
                 "here is currently shadowing anything. These are the frames "
                 "that would collide if they shared one namespace."),
    }), 200


@app.route("/observer/log", methods=["GET"])
def observer_log():
    """AgentVision's OWN observations about the bridged program — the stuck
    watchdog's records and process start/exit transitions.

    These used to be appended into the program's actions.jsonl, where they
    outnumbered the program's own records 20:1 and made a dead process look
    live. They live here instead. The program's log now contains only the
    program's output.
    """
    prof = request.args.get("profile") or _active_profile_name
    limit = _int_arg("limit", 200)
    recs = _read_observer_jsonl(prof, limit=limit)
    path = _observer_log_path(prof)
    return jsonify({
        "profile":  prof,
        "path":     str(path),
        "exists":   path.exists(),
        "count":    len(recs),
        "records":  recs,
        "live_state": _observer_state.get(prof or "default"),
        "note": ("AgentVision's own observations, kept OUT of the program's log. "
                 "`program_silent_s` here is measured against PROGRAM-emitted "
                 "records only. For the authoritative live figure use "
                 "av_log_sources() / av_diagnose(), which recompute it at read "
                 "time rather than trusting a stored record."),
    }), 200


@app.route("/frame/<int:seq>/overlay", methods=["GET"])
def frame_overlay(seq: int):
    """Structured overlay layer derived from JSONL records that match this
    frame (by frame_seq if present, else by ±2s timestamp window). Returns
    detections, OCR reads, path waypoints — all in the program's coordinate
    space. Lets Claude reason 'saw monster HERE, walked THERE' without pixels."""
    with _lock:
        frame = _frame_or_load(seq)
    if not frame:
        return _no_such_frame(seq)
    _mark_seen(seq, "overlay")
    frame_ms = float(frame.get("timestamp_ms") or 0)
    pinned   = frame.get("profile_action_log") or ""
    # Prefer exact frame_seq match; fall back to timestamp window
    by_seq = _read_action_jsonl(path_override=pinned, limit=10000)
    matched = [r for r in by_seq if r.get("frame_seq") == seq]
    if not matched:
        half = 2_000.0
        matched = _read_action_jsonl(window=(frame_ms - half, frame_ms + half),
                                     path_override=pinned, limit=10000)
    detections, ocr_reads, path_waypoints, walks, casts = [], [], [], [], []
    for r in matched:
        d = r.get("data") or {}
        if not isinstance(d, dict):
            continue
        cat = r.get("category")
        name = d.get("name")
        if name == "yolo.frame":
            detections.append({"ts": r.get("ts"), "enemies": d.get("enemies"),
                               "npcs": d.get("npcs"), "loot": d.get("loot")})
        elif name == "ocr.read":
            ocr_reads.append({"ts": r.get("ts"), "engine": d.get("engine"),
                              "sample": d.get("sample"), "conf": d.get("avg_conf")})
        elif name == "path.compute":
            path_waypoints.append({"ts": r.get("ts"), "start": d.get("start"),
                                   "goal": d.get("goal"), "len": d.get("len"),
                                   "ok": d.get("ok")})
        elif cat == "move":
            walks.append({"ts": r.get("ts"), "dir_deg": d.get("dir_deg"),
                          "duration": d.get("duration"), "label": d.get("label")})
        elif cat == "cast":
            casts.append({"ts": r.get("ts"), "skill": d.get("skill"),
                          "target": d.get("target")})
    return jsonify({
        "frame_seq":     seq,
        "frame_ts_ms":   frame_ms,
        "matched_by":    "frame_seq" if any(r.get("frame_seq") == seq for r in matched) else "ts_window",
        "detections":    detections,
        "ocr_reads":     ocr_reads,
        "path_waypoints": path_waypoints,
        "walks":         walks,
        "casts":         casts,
    }), 200


@app.route("/frame/<int:seq>/annotate", methods=["POST", "GET"])
def frame_annotate(seq: int):
    """POST: append Claude's note to this frame's annotations sidecar.
    GET:  return all annotations for this frame.
    Lets Claude leave breadcrumbs across sessions ('this is when OCR misread X
    — root cause in d2r_image/ocr.py:432'). Stored next to the frame JSON."""
    with _lock:
        frame = _frame_or_load(seq)
    if not frame:
        return _no_such_frame(seq)
    sidecar_json = frame.get("json_sidecar") or ""
    if not sidecar_json:
        # The frame EXISTS; it just has no sidecar file to hang notes on. The
        # stock 404 said the URL was not found, which is a different claim.
        return jsonify({
            "error": f"frame {seq} has no JSON sidecar on disk, so there is "
                     f"nowhere to store or read annotations",
            "status": 404,
            "seq": seq,
            "frame_exists": True,
            "why": ("annotations live beside the frame's sidecar; frames held "
                    "only in memory (not yet flushed, or captured with sidecar "
                    "writing off) have no such file"),
        }), 404
    ann_path = Path(sidecar_json).with_name(
        Path(sidecar_json).stem.replace("_frame", "") + "_annotations.json")
    if request.method == "GET":
        if ann_path.exists():
            try:
                return jsonify(json.loads(ann_path.read_text())), 200
            except Exception:
                return jsonify({"annotations": []}), 200
        return jsonify({"annotations": []}), 200
    # POST
    body = request.get_json(force=True, silent=True) or {}
    note = {
        "ts":      clock.now_iso(),
        "ts_ms":   clock.now_ms(),
        "author":  body.get("author") or "claude",
        "level":   body.get("level")  or "info",
        "message": body.get("message", ""),
        "tags":    body.get("tags") or [],
    }
    # READ-MODIFY-WRITE, and the read used to fail silently: a truncated or
    # unparseable file left `existing` at {"annotations": []}, and the write then
    # replaced every note already in it with just this one. These are
    # hand-authored root-cause findings — the retention layer now refuses to
    # delete `_annotations.json` for exactly that reason (it is the one file in a
    # frame's group with no archive equivalent) — so silently discarding them
    # here would undo that from the other side. A failed READ may not authorise
    # a WRITE: keep the file, and say what happened.
    existing = {"annotations": []}
    if ann_path.exists():
        try:
            loaded = json.loads(ann_path.read_text())
            if not isinstance(loaded, dict):
                raise ValueError(f"expected an object, got {type(loaded).__name__}")
            existing = loaded
        except Exception as exc:
            _warn(f"annotations for seq {seq} exist but could not be read "
                  f"({exc}); refusing to overwrite them")
            return jsonify({
                "error": f"existing annotations could not be read: {exc}",
                "saved": False,
                "path": str(ann_path),
                "why": ("appending would have replaced every note already in "
                        "this file with just the new one; these are authored by "
                        "hand and nothing regenerates them"),
                "fix": "repair or move that file, then re-post the note",
            }), 409
    existing.setdefault("annotations", []).append(note)
    # Atomic, so a crash mid-write cannot leave the truncated file that the read
    # above must then refuse.
    try:
        tmp_path = ann_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(existing, indent=2))
        os.replace(tmp_path, ann_path)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "annotation_count": len(existing["annotations"]),
                    "path": str(ann_path)}), 200


@app.route("/log", methods=["POST"])
def push_log():
    """Record a note from the agent/GUI. Goes to AgentVision's OWN sinks.

    The MCP docstring claimed this injected a structured record into the active
    program's actions.jsonl and that it would show up in av_log_range /
    av_actions_around_frame. It did neither: the handler wrote `message` as a
    plain line to activity.log and silently discarded category/source/data, while
    those readers read actions.jsonl.

    Both halves are now true in the only way they should be. The record IS
    structured, and it is written to AgentVision's observer log — NOT into the
    program's own log, because a note from the observer is not the program's
    output. That is the same rule the stuck watchdog now follows: 95% of one real
    project's actions.jsonl turned out to be AgentVision talking to itself.
    Read them back with av_observer_log / GET /observer/log.
    """
    data = request.get_json(force=True, silent=True) or {}
    msg  = data.get("message", "")
    src  = (data.get("source") or "claude.note").strip() or "claude.note"
    if not str(src).startswith(_AV_SELF_EMITTERS):
        # Prefixed so every read-time self-filter recognises it for what it is:
        # an observer record, which must never count as program liveness.
        src = f"agentvision.{src}"
    # `written_to` and `read_it_back` are ASSERTIONS ABOUT WHAT HAPPENED, so they
    # are built from what happened. They used to be emitted unconditionally: an
    # absent `message` — or a real observer-write failure, which
    # _append_observer_record reports by returning False — still answered
    # ok=true, read_it_back="av_observer_log()" and a written_to naming two files
    # nothing had been written to. Measured: POST /log {"line": ...} (the wrong
    # key) returned ok=true written_to=[observer jsonl, activity.log] while
    # `grep -r` over both trees found the note nowhere and the observer log did
    # not exist. `recorded: false` was the sole honest field, sitting next to a
    # list of filenames that contradicted it.
    if not str(msg).strip():
        return jsonify({
            "ok": False, "recorded": False, "written_to": [],
            "error": "MESSAGE_REQUIRED",
            "errors": [
                "Nothing was recorded: `message` was absent or empty, so there "
                "was no note to write.",
                'Send: {"message": "reproduced at frame 42"}',
            ],
            "hint": ("the note text goes in `message` — not `line`, `text`, "
                     "`msg` or `note`. category/source/data are optional."),
        }), 400

    get_logger().log(msg)              # unchanged: the human-readable line
    _dbg(f"External log: {msg}")
    wrote = _append_observer_record({
        "ts":       clock.now_iso(),
        "ts_ms":    time.time() * 1000.0,
        "category": data.get("category") or "note",
        "source":   src,
        "data":     {"message": msg, **(data.get("data") or {})},
    })
    written = ([str(_observer_log_path())] if wrote else []) + ["activity.log"]
    body = {
        "ok": wrote,
        "recorded": wrote,
        "source": src,
        "written_to": written,
        "not_written_to": ("the program's actions.jsonl — AgentVision does not "
                           "write into the log it is reading"),
    }
    if wrote:
        body["read_it_back"] = "av_observer_log()"
    else:
        body["error"] = "OBSERVER_WRITE_FAILED"
        body["errors"] = [
            f"The plain line reached activity.log, but the STRUCTURED record "
            f"could not be appended to {_observer_log_path()}, so this note is "
            f"NOT retrievable with av_observer_log(). The underlying OS error "
            f"is in the bridge log (search: 'observer write failed').",
        ]
    return jsonify(body), 200


# ── Browser ingest: the client half of a web app ─────────────────────────────
# Every other emitter appends to a file. A browser CANNOT — it has no filesystem
# — so the page emitter (agent_bootstrap/av_browser.js) POSTs newline-delimited
# JSON here and this route appends it to the program's text sink, where
# connectors/adapters/browser.py::browser_av_json parses it.
#
# These records go to the PROGRAM's sink, not the observer log, and that is the
# opposite of POST /log above. The distinction is authorship: a note from
# AgentVision is not the program's output, but a TypeError thrown in the user's
# React component IS — it is the most program-authored thing there is. Filing it
# as an observer record would hide the app's own failures from liveness and
# freshness, which is the same mistake the watchdog made in reverse.
_BROWSER_MAX_BATCH = 500          # records per POST
_BROWSER_MAX_LINE = 8_000         # bytes per record
_BROWSER_MAX_BODY = 2_000_000     # bytes per POST


def _cors(resp):
    """Allow the page to POST here from any dev origin.

    Without this the browser blocks the request at the same-origin policy and the
    emitter fails SILENTLY — the page keeps working, no error surfaces, and
    AgentVision simply never receives anything. A silent transport failure is the
    exact class of defect this project keeps having to dig out, so the preflight
    is handled explicitly rather than left to a proxy.
    """
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


@app.route("/browser/ingest", methods=["POST", "OPTIONS"])
def browser_ingest():
    """Receive browser-emitted events (NDJSON) and append them to the text sink.

    Body: one JSON object per line, each carrying `av: "browser"` — the marker the
    browser_av_json adapter keys on. Records without it are REJECTED and counted,
    so this cannot be used to inject arbitrary lines into a program's log.
    """
    if request.method == "OPTIONS":
        return _cors(make_response("", 204))

    raw = request.get_data(cache=False, as_text=True) or ""
    if len(raw) > _BROWSER_MAX_BODY:
        return _cors(make_response(jsonify({
            "ok": False, "error": "BODY_TOO_LARGE",
            "limit_bytes": _BROWSER_MAX_BODY,
            "hint": "the page emitter batches; lower MAX_QUEUE or flush sooner",
        }), 413))

    prof = _active_profile_obj()
    root = (getattr(prof, "project_root", "") or "").strip()
    if not root:
        return _cors(make_response(jsonify({
            "ok": False, "error": "NO_PROJECT_ROOT",
            "hint": ("the active profile has no project_root, so there is no sink "
                     "to append to — set one, or point the emitter at a profile "
                     "that has one"),
            "active_profile": _active_profile_name,
        }), 409))

    sink = Path(root) / "agentvision" / "log.txt"
    accepted = rejected = 0
    reasons: dict[str, int] = {}
    lines_out: list[str] = []

    def _reject(why: str) -> None:
        nonlocal rejected
        rejected += 1
        reasons[why] = reasons.get(why, 0) + 1

    for ln in raw.splitlines():
        s = ln.strip()
        if not s:
            continue
        if accepted >= _BROWSER_MAX_BATCH:
            _reject("batch_cap")
            continue
        if len(s) > _BROWSER_MAX_LINE:
            _reject("line_too_long")
            continue
        try:
            rec = json.loads(s)
        except Exception:
            _reject("not_json")
            continue
        if not isinstance(rec, dict) or rec.get("av") != "browser":
            # The marker is the authorisation: only the page emitter's own shape
            # may be appended to a program's log.
            _reject("missing_av_browser_marker")
            continue
        # Stamp arrival so a record is never MORE recent than when we saw it, and
        # so a page with a wrong clock cannot claim the future.
        rec.setdefault("ts_ms", time.time() * 1000.0)
        rec["received_ms"] = time.time() * 1000.0
        lines_out.append(json.dumps(rec, separators=(",", ":")))
        accepted += 1

    wrote = False
    if lines_out:
        try:
            sink.parent.mkdir(parents=True, exist_ok=True)
            with open(sink, "a", encoding="utf-8", errors="replace") as f:
                f.write("\n".join(lines_out) + "\n")
            wrote = True
        except Exception as exc:
            _warn(f"browser ingest write failed ({sink}): {exc}")
            return _cors(make_response(jsonify({
                "ok": False, "error": "SINK_WRITE_FAILED", "detail": str(exc),
                "sink": str(sink), "accepted": 0, "rejected": rejected,
            }), 500))

    if accepted:
        _dbg(f"browser ingest: +{accepted} record(s) -> {sink}")
    return _cors(make_response(jsonify({
        "ok": True, "accepted": accepted, "rejected": rejected,
        "reject_reasons": reasons, "written": wrote, "sink": str(sink),
        "read_it_back": "av_log_raw() / av_log_normalized()",
        "adapter": "browser_av_json",
    }), 200))


# ── Profiles ──────────────────────────────────────────────────────────────────

@app.route("/profiles", methods=["GET"])
def list_profiles():
    profiles = load_profiles()
    return jsonify({name: p.to_dict() for name, p in profiles.items()}), 200


@app.route("/profiles", methods=["POST"])
def create_or_update_profile():
    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    profiles = load_profiles()
    # NAME THE PROGRAM AFTER THE PROGRAM. ProgramProfile.display_name defaults
    # to "Custom Program" — correct for `custom`, the shipped placeholder, and
    # wrong for everything else. Without this, a profile created as
    # "orderworker" reported its program as "Custom Program", which breaks the
    # first instruction av_start_here gives: "check that av_start_here()'s
    # program is the program you were asked about." An agent that follows it
    # concludes it is looking at someone else's bridge and creates a duplicate
    # profile. Measured on a cold run through the MCP client.
    if not str(data.get("display_name") or "").strip():
        data = dict(data)
        if name == "custom":
            # The shipped placeholder is the one profile whose display_name
            # ("Custom Program") is CORRECT and is not the profile name — an
            # update that omits display_name must not rename it to "custom".
            # Dropping the key lets the dataclass default hold.
            data.pop("display_name", None)
        else:
            data["display_name"] = name
    profiles[name] = ProgramProfile.from_dict(data)
    try:
        save_profiles(profiles)
    except Exception as exc:
        # profiles.json could not be read completely, so writing it now would
        # commit an incomplete view over the user's hand-authored file. Say so
        # rather than reporting success or raising a bare 500.
        _warn(f"Profile save refused: {exc}")
        return jsonify({"error": str(exc), "saved": False,
                        "fix": "repair or move python_backend/profiles.json, "
                               "then retry"}), 409
    # Pre-create the output folder right now
    profile_output_folder(profiles[name], _base_for(profiles[name]))
    _info(f"Profile saved: {name}")
    return jsonify({"ok": True, "name": name}), 200


@app.route("/profiles/active", methods=["GET"])
def get_active_profile():
    profiles = load_profiles()
    p = profiles.get(_active_profile_name, ProgramProfile())
    return jsonify(p.to_dict()), 200


@app.route("/profiles/active", methods=["PUT"])
def set_active_profile():
    global _active_profile_name, _collector, _latest_frame
    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    profiles = load_profiles()
    if name not in profiles:
        return jsonify({"error": f"Profile '{name}' not found"}), 404
    prev = _active_profile_name
    _active_profile_name = name
    # Force re-init collector for new profile
    _collector = None
    collector  = _get_collector()
    _persist_active_profile(name)
    # The seq-keyed frame index belongs to ONE profile (see _foreign): swap it
    # for the new profile's frames instead of leaving the old program's frames
    # answering /frame/<seq> under a profile they do not belong to.
    reindexed = 0
    if prev != name:
        with _lock:
            _frames.clear()
            _frames_on_disk.clear()
            _latest_frame = None
        _hydrate_frame_index()
        # And re-seed the frame counter. The index is per-profile but the counter
        # is global: without this, a counter left low by the previous profile
        # (say, by a Clear Data) numbers the next shot straight over this
        # profile's existing frame_00001.png.
        _auto_engine.resume_counter_from_disk()
        with _lock:
            reindexed = len(_frames) + len(_frames_on_disk)
    _info(f"Active profile switched to: {name} ({profiles[name].display_name})"
          + (f" — frame index rebuilt: {reindexed} frame(s)" if prev != name else ""))
    return jsonify({"ok": True, "active": name,
                    "display_name": profiles[name].display_name,
                    "frames_indexed": reindexed,
                    "frame_index_note": (
                        "the seq-keyed frame index holds one profile at a time; "
                        "it was rebuilt for this profile. Other profiles' frames "
                        "stay on disk — av_frame_collisions / /frames/collisions"
                    ) if prev != name else None}), 200


@app.route("/profiles/<name>", methods=["DELETE"])
def delete_profile(name: str):
    """Delete a custom profile. Built-ins and the ACTIVE profile are refused.

    The active-profile guard was documented in the MCP tool's docstring
    ("Cannot remove the active one.") but was never implemented — only the
    built-in check existed. Deleting the active custom profile succeeded, and
    every later `load_profiles().get(_active_profile_name)` then returned None,
    so `_active_action_log_path()` and friends silently fell back to a blank
    default ProgramProfile: no process name, no log paths, no capture target.
    The bridge kept answering 200 with empty results instead of reporting that
    its subject no longer existed.
    """
    if name in BUILTIN_PROFILES:
        return jsonify({"error": "Cannot delete built-in profiles",
                        "deleted": False}), 400
    if name == _active_profile_name:
        return jsonify({
            "error": "Cannot delete the ACTIVE profile",
            "deleted": False,
            "active": _active_profile_name,
            "fix": ("switch to another profile first: "
                    "av_set_active_profile(<other>), then delete this one"),
            "why": ("with the active profile gone, every profile lookup falls "
                    "back to a blank default — the bridge would keep answering "
                    "with empty log paths and no capture target instead of "
                    "telling you its subject was deleted"),
        }), 409
    profiles = load_profiles()
    if name not in profiles:
        return jsonify({
            "error": f"no profile named {name!r}",
            "status": 404,
            "deleted": False,
            "profiles": sorted(profiles),
            "hint": "names are case-sensitive; av_list_profiles() lists them",
        }), 404
    del profiles[name]
    try:
        save_profiles(profiles)
    except Exception as exc:
        _warn(f"Profile delete refused: {exc}")
        return jsonify({"error": str(exc), "deleted": False,
                        "fix": "repair or move python_backend/profiles.json, "
                               "then retry"}), 409
    return jsonify({"ok": True, "deleted": True, "name": name}), 200


# ── Live program data ─────────────────────────────────────────────────────────

@app.route("/program/status", methods=["GET"])
def program_status():
    """Is the target program running — and on what evidence.

    `running` alone has been wrong in a way nothing downstream could see: the
    matcher accepted any process whose command line merely MENTIONED the project
    (a shell, `cat`, `less`, an editor), so capture ran against a program that
    had exited. `liveness_evidence` names the process and the rule that matched,
    and `contradiction` fires when this claim disagrees with the program's own
    output or with its resource sample — the two cheap cross-checks available.
    """
    collector = _get_collector()
    ev        = collector._reader.running_evidence()
    running   = bool(ev.get("running"))
    cpu, ram  = collector._reader.process_cpu_ram()
    _dbg(f"/program/status  running={running}  cpu={cpu}  ram={ram}  "
         f"matched_by={ev.get('matched_by')}")
    contradiction = None
    if running and not ev.get("exe") and cpu == 0.0 and ram == 0.0:
        contradiction = ("matched a process but sampled no cpu/ram from it — "
                         "the match may be a bystander, not the program")
    try:
        _f = _source_freshness(collector.profile)
        _sil = _f.get("program_silent_s")
    except Exception:
        _sil = None
    if running and _sil is not None and _sil > SOURCE_STALE_S:
        contradiction = (f"a process matched, but the program has written "
                         f"nothing to any log for {_human_age(_sil)} "
                         f"(matched_by={ev.get('matched_by')})")
    return jsonify({
        "running":       running,
        "program":       collector.profile.display_name,
        "cpu_percent":   cpu,
        "ram_gb":        ram,
        "liveness_evidence": ev,
        "program_silent_s":  _sil,
        "contradiction":     contradiction,
    }), 200


@app.route("/program/log", methods=["GET"])
def program_log():
    lines = _int_arg("lines", 40)
    collector = _get_collector()
    log_lines = collector._reader.tail_log(lines)
    _dbg(f"/program/log  source={collector.profile.log_file!r}  lines={len(log_lines)}")
    return jsonify({
        "lines":  log_lines,
        "source": collector.profile.log_file,
    }), 200


@app.route("/program/stats", methods=["GET"])
def program_stats():
    """`key: value` pairs from the program's own stats output — all of them.

    The parser used to keep a hardcoded nine keys (session length, games played,
    xp/hour — a Diablo-II bot's vocabulary) and silently discard every other line,
    while the tool advertised generic "numeric metrics, counters, gauges". The
    `lines` parameter was accepted here and by the MCP tool and then never used.
    """
    lines = _int_arg("lines", 0)
    collector = _get_collector()
    stats = collector._reader.latest_stats(lines=lines)
    _dbg(f"/program/stats  keys={list(stats.keys())[:5]}")
    return jsonify({
        "stats":   stats,
        "count":   len(stats),
        "lines_read": lines or "all",
        "source":  collector.profile.stats_folder,
        "program": collector.profile.display_name,
        "note": ("every `key: value` line from the newest stats_*.log is "
                 "returned; nine legacy bot-specific aliases are additionally "
                 "provided for backwards compatibility. Empty means the "
                 "profile's stats_folder holds no stats_*.log, not that the "
                 "program has no metrics."),
    }), 200


@app.route("/program/crop", methods=["GET"])
def program_crop():
    """Return the current crop/window bounds for the active profile."""
    collector = _get_collector()
    reader    = collector._reader
    result = {
        "capture_app":  collector.profile.capture_app,
        "capture_crop": collector.profile.capture_crop,
        "window_bounds": None,
        "active_crop":  None,
    }
    # Try getting live window bounds
    bounds = reader.get_window_bounds()
    if bounds:
        result["window_bounds"] = list(bounds)
        result["active_crop"]   = list(bounds)
    # Custom crop overrides window bounds
    crop = reader.get_capture_crop()
    if crop:
        result["active_crop"] = list(crop)
    return jsonify(result), 200


# ── Test runner ───────────────────────────────────────────────────────────────

@app.route("/run-tests", methods=["POST"])
def trigger_tests():
    collector = _get_collector()
    body = request.get_json(force=True, silent=True) or {}
    root = body.get("project_root", collector.profile.project_root or ".")
    # Use the target profile's own interpreter (its venv is where its deps and
    # pytest live), falling back to the bridge's interpreter. Bare "python" is
    # absent on stock macOS and was the source of a silent all-zero result.
    python_exe = body.get("python_exe") or getattr(
        collector.profile, "python_exe", "") or ""
    timeout = int(body.get("timeout") or 60)
    test_info, errors = run_tests(root, timeout=timeout, python_exe=python_exe)
    # A run that never executed must not read as "0 passed, 0 failed" — that is
    # an all-clear for tests that did not run. Say so explicitly.
    if not getattr(test_info, "ran", True):
        return jsonify({
            "ran": False,
            "error": test_info.error,
            "failure_detail": (test_info.failure_detail or "")[-500:],
            "what_this_is": ("The test runner did not execute, so there is NO "
                             "pass/fail result. This is not a passing suite."),
        }), 200
    return jsonify({
        "ran":            True,
        "pass_count":     test_info.pass_count,
        "fail_count":     test_info.fail_count,
        "skip_count":     test_info.skip_count,
        "failed_tests":   test_info.failed_tests,
        "failure_detail": test_info.failure_detail[-500:],
    }), 200


@app.route("/codebase-map", methods=["GET"])
def codebase_map():
    from dataclasses import asdict
    collector = _get_collector()
    root = request.args.get("root", collector.profile.project_root or ".")
    return jsonify(asdict(build_map(root))), 200


# ── Source mirror endpoints ──────────────────────────────────────────────────
# Mirror the bridged project's source into AgentVision so Claude (via MCP)
# and the GUI can browse it without scouring the user's filesystem. Three
# views: tree (paths only), light (1-line/file), digest (full symbols).

import source_mirror as _src_mirror  # noqa: E402

_SOURCE_BASE = _AV_ROOT / "snapshots"


def _profile_dir() -> Path:
    return _SOURCE_BASE / _active_profile_name


#: (profile) -> (checked_at_monotonic, stale_dict). The staleness walk is stat-only
#: but there is no reason to repeat it for a burst of source calls.
_src_stale_cache: dict[str, tuple[float, dict]] = {}
_SRC_STALE_RECHECK_S = float(os.environ.get("AGENTVISION_SRC_RECHECK_S", "5"))


def _source_index_staleness(pdir: Path, root: Path) -> dict:
    """Has the project changed since the source index was built?

    The index was rebuilt only when it was entirely MISSING — never compared
    against the files it describes — so once built it answered from the state of
    the project at first call, for the rest of the bridge's life. Every edit made
    while debugging was invisible to av_source_digest / av_source_file until
    someone happened to call av_source_refresh. A stale mirror of the code you are
    actively editing is the worst possible kind: it looks authoritative.

    This is a stat-only walk (no file contents), reusing the mirror's own skip
    rules, so it costs a fraction of a rebuild.
    """
    idx = pdir / "source_index.json"
    out = {"stale": False, "index_exists": idx.exists(), "newest_change_ms": None,
           "index_built_ms": None, "changed_files_sampled": [],
           "basis": "mtime of every indexed-eligible file vs the index build time"}
    if not idx.exists():
        out["stale"] = True
        return out
    now = time.monotonic()
    cached = _src_stale_cache.get(_active_profile_name)
    if cached and (now - cached[0]) < _SRC_STALE_RECHECK_S:
        return cached[1]
    try:
        built_ms = idx.stat().st_mtime * 1000.0
        out["index_built_ms"] = built_ms
        newest = 0.0
        changed: list[str] = []
        for p in _src_mirror._walk_project(root):
            try:
                m = p.stat().st_mtime * 1000.0
            except OSError:
                continue
            if m > newest:
                newest = m
            if m > built_ms and len(changed) < 10:
                changed.append(str(p))
        out["newest_change_ms"] = newest or None
        out["changed_files_sampled"] = changed
        out["stale"] = bool(newest and newest > built_ms)
    except Exception as e:
        out["error"] = str(e)
    _src_stale_cache[_active_profile_name] = (now, out)
    return out


def _ensure_source_built() -> tuple[Path, dict | None]:
    """Build the source mirror if missing, and REBUILD it if the project has
    changed since it was built (see _source_index_staleness)."""
    pdir = _profile_dir()
    coll = _get_collector()
    root = Path(coll.profile.project_root or "").expanduser()
    if not root.exists():
        if (pdir / "source_index.json").exists():
            return pdir, None
        return pdir, {"error": "project_root does not exist", "root": str(root)}
    st = _source_index_staleness(pdir, root)
    if st.get("index_exists") and not st.get("stale"):
        return pdir, None
    try:
        out = _src_mirror.refresh(root, _active_profile_name, _SOURCE_BASE,
                                  do_mirror=False)
        out["rebuilt_because"] = ("index missing" if not st.get("index_exists")
                                  else "project changed since the index was built")
        out["staleness"] = st
        _src_stale_cache.pop(_active_profile_name, None)
        _info(f"source index rebuilt for {_active_profile_name}: "
              f"{out['rebuilt_because']}")
        return pdir, out
    except Exception as e:
        return pdir, {"error": str(e)}


@app.route("/source/refresh", methods=["POST"])
def source_refresh():
    """Re-walk the project + rebuild index, light, digest, tree.
    Pass ?mirror=1 to also copy files into the source_mirror dir."""
    coll = _get_collector()
    root = Path(coll.profile.project_root or "").expanduser()
    if not root.exists():
        return jsonify({"error": "project_root does not exist",
                        "root": str(root)}), 404
    do_mirror = request.args.get("mirror", "0") == "1"
    try:
        out = _src_mirror.refresh(root, _active_profile_name, _SOURCE_BASE,
                                  do_mirror=do_mirror)
        _info(f"/source/refresh  {_active_profile_name}  files={out['file_count']}  "
              f"light={out['light_bytes']}B  digest={out['digest_bytes']}B")
        return jsonify(out), 200
    except Exception as e:
        _warn(f"/source/refresh failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/source/tree", methods=["GET"])
def source_tree():
    """Hierarchical file tree of the active project. Small (~10K tokens
    for a few-hundred-file repo)."""
    pdir, err = _ensure_source_built()
    if err and "error" in err:
        return jsonify(err), 404
    p = pdir / "source_tree.json"
    if not p.exists():
        return jsonify({"error": "tree not built yet — POST /source/refresh"}), 404
    return jsonify(json.loads(p.read_text())), 200


@app.route("/source/light", methods=["GET"])
def source_light():
    """Token-frugal per-file index — every file with a 1-line summary.
    Read this FIRST when opening a session — it's the orientation map."""
    pdir, err = _ensure_source_built()
    if err and "error" in err:
        return jsonify(err), 404
    p = pdir / "source_light.json"
    if not p.exists():
        return jsonify({"error": "light digest not built — POST /source/refresh"}), 404
    return jsonify(json.loads(p.read_text())), 200


@app.route("/source/digest", methods=["GET"])
def source_digest_full():
    """Full per-file digest with all symbols, signatures, docstrings.
    Bigger — use after light/tree to drill into specific subdirs."""
    pdir, err = _ensure_source_built()
    if err and "error" in err:
        return jsonify(err), 404
    p = pdir / "source_digest.json"
    if not p.exists():
        return jsonify({"error": "digest not built — POST /source/refresh"}), 404
    # Allow scoping by path prefix to keep responses small.
    prefix = request.args.get("prefix", "").strip().lstrip("/")
    full = json.loads(p.read_text())
    if prefix:
        full["files"] = [f for f in full["files"] if f["path"].startswith(prefix)]
    return jsonify(full), 200


@app.route("/source/file", methods=["GET"])
def source_file():
    """Read one file's source. Path is relative to project_root.
    Optional ?from_line=N&to_line=M for paginated reads."""
    rel = (request.args.get("path") or "").strip().lstrip("/\\")
    # Separator-agnostic. On Windows `..\..\etc` splits on "/" into ONE element,
    # so a "/"-only check waves it through — this guard read as traversal
    # protection while providing none off-POSIX. The real confinement is the
    # relative_to() check below (verified to hold on both platforms), so this was
    # never a hole; it is fixed so the code means what it appears to mean.
    if not rel or ".." in rel.replace("\\", "/").split("/"):
        return jsonify({"error": "invalid path"}), 400
    coll = _get_collector()
    root = Path(coll.profile.project_root or "").expanduser()
    target = (root / rel).resolve()
    # Confine to project_root.
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return jsonify({"error": "path escapes project_root"}), 400
    if not target.exists() or not target.is_file():
        return jsonify({"error": "not found", "path": rel}), 404
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    lines = text.splitlines()
    fl = max(0, _int_arg("from_line", 1) - 1)
    tl = _int_arg("to_line", len(lines))
    chunk = lines[fl:tl]
    return jsonify({
        "path": rel,
        "project_root": str(root),
        "total_lines": len(lines),
        "from_line": fl + 1,
        "to_line": fl + len(chunk),
        "lines": chunk,
    }), 200


@app.route("/source/search", methods=["GET"])
def source_search():
    """Grep-style content search across the bridged project's source.
    Returns up to ?limit=200 matches: {path, line, text}. Case-insensitive
    by default; pass ?case=1 for case-sensitive."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "q parameter required"}), 400
    case_sensitive = request.args.get("case", "0") == "1"
    limit = max(1, min(2000, _int_arg("limit", 200)))
    pdir, err = _ensure_source_built()
    if err and "error" in err:
        return jsonify(err), 404
    idx_path = pdir / "source_index.json"
    if not idx_path.exists():
        return jsonify({"error": "index not built — POST /source/refresh"}), 404
    idx = json.loads(idx_path.read_text())
    coll = _get_collector()
    root = Path(coll.profile.project_root or "").expanduser()
    needle = q if case_sensitive else q.lower()

    matches: list[dict] = []
    files_with_hits = 0
    for f in idx["files"]:
        if f["lang"] in ("other",) and f["lines"] > 5000:
            continue
        full = root / f["path"]
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hay = text if case_sensitive else text.lower()
        if needle not in hay:
            continue
        files_with_hits += 1
        for n, line in enumerate(text.splitlines(), 1):
            check = line if case_sensitive else line.lower()
            if needle in check:
                matches.append({
                    "path": f["path"], "line": n,
                    "text": line[:240],
                })
                if len(matches) >= limit:
                    return jsonify({
                        "q": q, "case_sensitive": case_sensitive,
                        "match_count": len(matches),
                        "files_with_hits": files_with_hits,
                        "truncated": True,
                        "matches": matches,
                    }), 200
    return jsonify({
        "q": q, "case_sensitive": case_sensitive,
        "match_count": len(matches),
        "files_with_hits": files_with_hits,
        "truncated": False,
        "matches": matches,
    }), 200


@app.route("/source/list", methods=["GET"])
def source_list():
    """Flat list of every indexed path with size + lines + lang.
    Like a `find . -type f` for the bridged project, with metadata."""
    pdir, err = _ensure_source_built()
    if err and "error" in err:
        return jsonify(err), 404
    p = pdir / "source_index.json"
    if not p.exists():
        return jsonify({"error": "index not built — POST /source/refresh"}), 404
    idx = json.loads(p.read_text())
    return jsonify({
        "project_root": idx["project_root"],
        "profile": idx["profile_name"],
        "file_count": idx["file_count"],
        "files": [{"path": f["path"], "size": f["size"],
                   "lines": f["lines"], "lang": f["lang"]}
                  for f in idx["files"]],
    }), 200


@app.route("/debug/log", methods=["GET"])
def debug_log_tail():
    """Return last 100 lines of the AgentVision debug log."""
    lines_n = _int_arg("lines", 100)
    try:
        text  = _DEBUG_LOG.read_text(errors="replace")
        lines = text.splitlines()[-lines_n:]
    except Exception:
        lines = []
    return jsonify({"log_file": str(_DEBUG_LOG), "lines": lines}), 200


# ── PRE-FLIGHT log-coverage FORCE ─────────────────────────────────────────────
# THE FORCE: before AgentVision's FIRST bridge start for a program, the AI must
# prove the program already emits log/debug-log data AgentVision can specifically
# parse — and, where it can't, ADD the missing adapter(s) first. /preflight is the
# coverage check; /adapter/add is the runtime self-extension; a per-profile marker
# + a preflight_required nudge on the first /capture/start wire it together.

def _preflight_marker_path(profile: "ProgramProfile") -> Path:
    """Per-profile marker file that records 'preflight has passed / gaps accepted'
    for this program. Lives in the profile's own AgentVision output folder so it
    travels with the profile's data and is naturally per-program."""
    try:
        base = profile_output_folder(profile, _base_for(profile))
    except Exception:
        base = _AV_ROOT / SAVE_FOLDER
    return Path(base) / ".av_preflight_ok"


def _preflight_marker_read(profile: "ProgramProfile") -> dict | None:
    try:
        p = _preflight_marker_path(profile)
        if p.exists():
            return json.loads(p.read_text(errors="replace"))
    except Exception:
        pass
    return None


def _preflight_marker_write(profile: "ProgramProfile", verdict: dict,
                            *, accepted_gaps: bool) -> None:
    try:
        p = _preflight_marker_path(profile)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "profile": getattr(profile, "name", ""),
            "ts": datetime.now().isoformat(),
            "ready": bool(verdict.get("ready")),
            "accepted_gaps": bool(accepted_gaps),
            "gap_count": len(verdict.get("gaps") or []),
            "covered": [c.get("adapter") for c in (verdict.get("covered") or [])],
        }, indent=2))
    except Exception as e:
        _warn(f"could not write preflight marker: {e}")


def _preflight_hint(profile: "ProgramProfile") -> dict:
    """One-line preflight status surfaced in /status and /overview so the model
    always sees whether the FORCE has been satisfied for this program."""
    marker = _preflight_marker_read(profile)
    if marker:
        return {
            "ok": True,
            "hint": (f"Preflight satisfied for '{getattr(profile,'name','')}' "
                     f"({'ready' if marker.get('ready') else 'gaps accepted'}) — "
                     "log coverage was verified before first capture."),
            "marker": marker,
        }
    return {
        "ok": False,
        "hint": ("Preflight NOT yet run for this program. Before the first bridge "
                 "start, call av_preflight; if it reports gaps, call av_add_adapter "
                 "for each missing debug-log type, then start capture."),
        "marker": None,
    }


def _run_preflight(profile: "ProgramProfile", body: dict | None = None) -> dict:
    """Compute the preflight verdict for a profile with optional explicit inputs."""
    body = body or {}
    return _log_sources.preflight_report(
        profile,
        project_root=(body.get("project_root") or "").strip(),
        language=(body.get("language") or "").strip(),
        sample_lines=body.get("sample_lines") or None,
        log_paths=body.get("log_paths") or None,
    )


@app.route("/preflight", methods=["GET", "POST"])
def preflight_route():
    """PRE-FLIGHT log-coverage check — the FORCE. Verifies the active program (or
    an explicitly-supplied project_root/language/sample_lines/log_paths) already
    emits log/debug-log data AgentVision can SPECIFICALLY parse, before the first
    bridge start.

    Returns {ready, language, covered, gaps, pending, recommended_actions, …}.
    A `gap` is a source whose real lines only route to a generic FALLBACK
    (structural/generic_ts/raw) — i.e. no adapter specifically understands it.
    When ready=true (or accept_gaps=true) the per-profile preflight marker is set
    so /capture/start stops nagging. For each gap, call /adapter/add
    (av_add_adapter) to add the missing debug-log type, then re-run this."""
    body = request.get_json(silent=True) or {} if request.method == "POST" else {}
    if request.method == "GET":
        body = {
            "project_root": request.args.get("project_root", ""),
            "language": request.args.get("language", ""),
        }
    prof = _active_profile_obj()
    verdict = _run_preflight(prof, body)
    accept_gaps = bool(body.get("accept_gaps")) or \
        request.args.get("accept_gaps") in ("1", "true", "True")
    if verdict.get("ready") or accept_gaps:
        _preflight_marker_write(prof, verdict, accepted_gaps=accept_gaps and not verdict.get("ready"))
        verdict["marker_written"] = True
    else:
        verdict["marker_written"] = False
    return jsonify(verdict), 200


@app.route("/adapter/add", methods=["POST"])
def adapter_add_route():
    """RUNTIME SELF-EXTENSION — add a log adapter so AgentVision can specifically
    parse a format it currently only handles with a generic fallback. Body = a
    spec:
      {name, family?, language?,
       detect:{regex?, anchor_tokens?[]}, extract:{regex (named groups
       ts/level/source/message)}, level_map?, default_level?, default_source?,
       category?, match_scope?("lines"|"first"), sample (a real line it must own)}

    The adapter is built (reusing RxAdapter), VALIDATED (it must route its own
    `sample` to itself and must NOT steal any existing catalog sample from a named
    adapter — a collision is rejected with the offending sample), registered LIVE,
    and PERSISTED to connectors/adapters/user_adapters.json so it survives a
    restart. Idempotent by name. Returns {ok, adapter_name, persisted, registered,
    errors, self_route, collisions}."""
    spec = request.get_json(silent=True) or {}
    if not isinstance(spec, dict) or not spec:
        return jsonify({"ok": False, "errors": ["a JSON adapter spec body is required"]}), 400

    # The body IS the spec — `extract` etc. are TOP-LEVEL keys. A cold model was
    # measured burning 13 attempts here: it wrapped the spec in {"spec": {...}}
    # (a natural guess) and the validator answered "spec.extract.regex is
    # required", which names a nested path and so CONFIRMS the wrong shape. Detect
    # the wrapper explicitly instead of letting the field-level error mislead.
    if "extract" not in spec:
        for wrapper in ("spec", "adapter", "body", "payload"):
            inner = spec.get(wrapper)
            if isinstance(inner, dict) and ("extract" in inner or "name" in inner):
                return jsonify({
                    "ok": False, "registered": False, "persisted": False,
                    "error": "SPEC_WRAPPED",
                    "errors": [
                        f"The adapter spec was nested under {wrapper!r}, but this "
                        f"route expects the spec fields at the TOP LEVEL of the "
                        f"body. Nothing is missing — the envelope is wrong.",
                        'Send: {"name": "my_fmt", "extract": {"regex": "^(?P<level>'
                        '[A-Z]+) (?P<message>.*)$"}, "sample": "INFO it works"}',
                    ],
                    "hint": ("Unwrap it: send the contents of "
                             f"{wrapper!r} as the whole body."),
                }), 400

    try:
        from connectors.adapters import user_adapters as _ua
    except Exception:
        from python_backend.connectors.adapters import user_adapters as _ua  # type: ignore
    result = _ua.add_adapter(spec, persist=True)
    if result.get("ok"):
        _info(f"Adapter '{result.get('adapter_name')}' added + persisted via /adapter/add")
    else:
        _warn(f"/adapter/add rejected '{spec.get('name')}': {result.get('errors')}")
    status = 200 if result.get("ok") else 422
    return jsonify(result), status


# ── Auto-capture control endpoints ───────────────────────────────────────────

@app.route("/capture/start", methods=["POST"])
def capture_start():
    data = request.get_json(force=True, silent=True) or {}
    force = bool(data.get("force")) or \
        request.args.get("force") in ("1", "true", "True")
    prof = _active_profile_obj()

    # THE FORCE — on the FIRST start for a program with no preflight marker, do
    # NOT silently proceed blind. Return a prominent preflight_required nudge with
    # the coverage summary so the AI runs av_preflight (and av_add_adapter for any
    # gaps) first. `force=true` overrides (accepts gaps) — the GUI passes it so a
    # human clicking Start Bridge is never blocked.
    # ── THE FIRST-CONNECTION GATE ────────────────────────────────────────────
    # HARD gate, and it comes before the coverage check because it is the more
    # fundamental question. Coverage asks "can we parse the logs that exist?";
    # this asks "which logs and tools should exist AT ALL for this program?" —
    # and AgentVision must not answer that itself. Left to its own devices it
    # scaffolds the same emitter set for a web server and a GPU emulator, and
    # reports success either way, so a wrong bridge looks exactly like a right one.
    #
    # Fires ONCE per program: after the agent commits a plan the logging is built
    # in and every later connection proceeds straight through.
    #
    # `force=true` still overrides — the GUI passes it, so a human clicking Start
    # is never locked out of their own tool by a gate meant for the agent.
    import bridge_plan as _bp
    if not force and not _bp.is_sealed(_plan_folder(prof)):
        # HTTP 200 is kept deliberately: the MCP layer and the GUI both read this
        # body, and a 4xx here would change behaviour for every existing client.
        # But a status code alone misleads a model that checks only the code, so
        # the refusal carries an explicit machine-checkable discriminator and
        # states the next call in one unambiguous field.
        return jsonify({
            "error": "BRIDGE_NOT_BUILT",
            "ok": False, "started": False,
            "bridge_required": True,
            "DO_THIS_NEXT": "av_bridge_catalog()  ->  av_bridge_commit(plan=...)",
            "bridge": _bp.status(_plan_folder(prof), prof),
            "guidance": (
                "REFUSED — this is not a failure, it is the one-time setup step. "
                "FIRST CONNECTION to this program: the bridge is NOT built yet, so "
                "capture will not start. AgentVision supplies the tools but will not "
                "choose them: which emitters, adapters and settings a program needs "
                "depends on what its code IS and DOES. Call av_bridge_catalog() to "
                "review every option, then av_bridge_commit(plan=..., "
                "catalog_token=...) — the token proves the options were read. This "
                "happens once per program, ever; after it, this program's bridge is "
                "built for good and restarting AgentVision will not undo it."),
        }), 200

    marker = _preflight_marker_read(prof)
    if marker is None and not force:
        verdict = _run_preflight(prof, {})
        return jsonify({
            "ok": False,
            "started": False,
            "preflight_required": True,
            "preflight": verdict,
            "guidance": (
                "FIRST capture for this program and preflight has not run. Call "
                "av_preflight to verify AgentVision can specifically parse this "
                "program's logs. If it reports gaps, call av_add_adapter for EACH "
                "missing debug-log type, then start capture again. To start anyway "
                "and accept the current coverage, retry with force=true."),
        }), 200

    # The agent may have decided this program has nothing worth looking at. That
    # decision was previously stored and then ignored: capture ran anyway, burning
    # disk on screenshots of a headless service the plan had already ruled out.
    # Honoured here, with the same force= escape the other gates use.
    if not force:
        try:
            _vp = (_bp.read_plan(_plan_folder(prof)) or {}).get("visual_capture")
            if _vp is False:
                return jsonify({
                    "error": "VISUAL_CAPTURE_DISABLED_BY_PLAN",
                    "ok": False, "started": False,
                    "guidance": (
                        "This program's committed plan sets visual_capture=false, so "
                        "screenshots are intentionally off — frames would be waste "
                        "for a target with no useful window. This is not a fault. "
                        "Use the log tools instead (av_log_raw, av_diagnose, "
                        "av_search). If the plan was wrong, either pass force=true "
                        "for this run or av_bridge_commit(replan=True) to change the "
                        "decision properly."),
                    "plan_visual_capture": False,
                }), 200
        except Exception as exc:
            _dbg(f"visual_capture gate skipped: {exc}")

    # An explicit interval wins; otherwise honour the one the agent committed in
    # the plan. Without this the planned `capture.interval_seconds` was inert —
    # a model could ask the user for 0.2s, record it, and silently capture at the
    # engine's existing rate. A decision the gate collects but never applies is
    # worse than not collecting it, because the plan file then misdescribes the run.
    applied_from = None
    if "interval" in data:
        _auto_engine.set_interval(float(data["interval"]))
        applied_from = "request"
    else:
        try:
            _plan = _bp.read_plan(_plan_folder(prof)) or {}
            _pi = (_plan.get("capture") or {}).get("interval_seconds")
            if _pi:
                _auto_engine.set_interval(float(_pi))
                applied_from = "plan"
        except Exception as exc:
            _dbg(f"plan interval not applied: {exc}")
    _auto_engine.start()
    # Record that this program's first start has happened with coverage accepted
    # (either preflight passed earlier, or the caller forced through).
    if marker is None:
        try:
            verdict = _run_preflight(prof, {})
            _preflight_marker_write(prof, verdict,
                                    accepted_gaps=not verdict.get("ready"))
        except Exception:
            pass
    _info(f"Auto-capture started via API  interval={_auto_engine.interval}s"
          f"{'  (force)' if force else ''}")
    return jsonify({"ok": True, "started": True, "interval": _auto_engine.interval,
                    "interval_source": applied_from or "engine default",
                    "shots_per_second": _fps(_auto_engine.interval),
                    "preflight_ok": True,
                    "rate": capture_rate_info(_auto_engine.interval)}), 200


@app.route("/capture/stop", methods=["POST"])
def capture_stop():
    _auto_engine.stop()
    _info("Auto-capture stopped via API")
    return jsonify({"ok": True}), 200


@app.route("/capture/reset-counter", methods=["POST"])
def capture_reset_counter():
    """Reset the frame counter, then re-seed it from what is still on disk.

    Called by the GUI after the user clears all snapshot files so the Shots:
    label doesn't show a stale count from the previous run.

    The bare `frame_count = 0` was a destructive reset. The counter is GLOBAL but
    the GUI's Clear Data is PER-PROFILE, and switching profiles rebuilds the frame
    index without re-seeding the counter — so: clear profile A, switch to profile
    B, and the next shutter writes frame_00001.png over B's untouched frame 1,
    taking its sidecar, thumbnail and diff with it. The machinery to prevent that
    already existed (resume_counter_from_disk, which documents this very bug) and
    had exactly one caller: process start.

    So reset to 0 and then ask the DISK what the counter is allowed to be. When
    the files really are gone the answer is 0 and numbering restarts at 1, which
    is what the label wants; when anything survives — including the compressed
    copies in _archive/ — numbering continues above it. The label itself is fed
    by len(_frames), not by this counter (see /capture/status), so clearing the
    cache is what actually fixes the display.
    """
    _auto_engine.frame_count = 0
    # Also clear the in-memory frame cache so /latest / /frame/<seq>
    # don't return stale results from the deleted files.
    global _frames
    _frames.clear()
    resumed = _auto_engine.resume_counter_from_disk()
    _info(f"Capture counter + frame cache reset via /capture/reset-counter "
          f"(numbering resumes at #{resumed + 1} — existing frames on disk are "
          f"never overwritten)")
    return jsonify({"ok": True, "frame_count": resumed,
                    "next_frame": resumed + 1}), 200


@app.route("/capture/interval", methods=["PUT"])
def capture_set_interval():
    data = request.get_json(force=True, silent=True) or {}
    secs = _float_arg("interval", 1.0, source=data)
    _auto_engine.set_interval(secs)
    _info(f"Auto-capture interval → {_auto_engine.interval}s "
          f"({_fps(_auto_engine.interval)} shots/sec)")
    return jsonify({"ok": True, "interval": _auto_engine.interval,
                    "shots_per_second": _fps(_auto_engine.interval),
                    "rate": capture_rate_info(_auto_engine.interval)}), 200


@app.route("/capture/status", methods=["GET"])
def capture_status():
    # Use len(_frames) as the authoritative count — it reflects disk reality
    # (hydrated on startup, cleared on reset) rather than the engine's
    # in-session counter which starts at 0 each bridge run regardless of
    # how many frames exist on disk.
    with _lock:
        frame_count = len(_frames)
    e = _auto_engine
    return jsonify({
        "engine_running": e.running,
        "capturing":      e.capturing,
        "interval":       e.interval,
        "shots_per_second": _fps(e.interval),
        "frame_count":    frame_count,
        "last_error":     e.last_error,
        # ── Capture health (the user's "it doesn't work right all the time") ──
        "health": {
            "last_frame_ms":      e.last_frame_ms,
            "last_latency_ms":    e.last_latency_ms,
            "blank_frame_count":  e.blank_frame_count,
            "window_missing":     e.window_missing,
            "frames_skipped_no_window": e.skipped_no_window,
            # HARD failures of the capture call. Measured: 14 consecutive
            # "screencapture failed: could not create image from window" errors
            # while this block reported skipped=0, window_missing=False and no
            # error — a capture that produced 1 frame in 22 s looked healthy. The
            # engine recorded last_error all along; nothing surfaced it here.
            "frame_errors":       e.frame_error_count,
            "frame_errors_note": (
                "hard failures of the capture call itself. Nonzero alongside a low "
                "frame_count means capture is BROKEN, not idle: a window that "
                "cannot be photographed is indistinguishable from a program with "
                "nothing to show unless this number is read."),
            "frames_while_program_silent": e.frames_while_silent,
            "frames_while_program_silent_note": (
                "frames taken while the process was alive but the program had "
                "written nothing for longer than "
                f"{SOURCE_STALE_S:.0f}s. Not an error on its own — an idle GUI "
                "is legitimately silent — but a large number means the window "
                "being captured may no longer be updating. 12,921 such frames "
                "were once stored for a program that had already exited."),
            "liveness_evidence": (_get_collector()._reader.running_evidence()
                                  if e.capturing else None),
            "only_the_program": ("Every frame contains ONLY the bridged "
                                 "program's window — never the desktop, never "
                                 "another app. If the program has no window the "
                                 "frame is SKIPPED, so a gap in seq numbers "
                                 "means it was showing nothing. On macOS a "
                                 "minimized or occluded window still captures."),
            "last_warning":       e.last_warning,
        },
        # ── Rate envelope + guidance the AI must act on (ASK THE USER) ──
        "rate": capture_rate_info(e.interval),
    }), 200


# ── Input-daemon status ──────────────────────────────────────────────────────
# Lets Claude (and the GUI) see whether the system-wide input recorder is
# running, which profile its current sink belongs to, and whether the
# per-profile capture-physical-input opt-in is on. This is essential for
# diagnosing "why don't I see any keys/clicks in the JSONL?" questions.
@app.route("/selftest", methods=["GET"])
def selftest():
    """Runtime SELF-CONFIRMATION on the target machine: capture non-blank,
    window enumeration, input-hook fire (Windows spawns the daemon --selftest
    SendInput handshake), and daemon status. Returns a JSON health report — the
    definitive 'confirmed working' proof at first run (esp. on Windows)."""
    from utils import platform_shim
    from shared.schema.snapshot_schema import SCHEMA_VERSION
    checks = []
    if platform_shim.IS_LINUX:
        try:
            checks.append(platform_shim.linux_session_selftest())
        except Exception as e:
            checks.append({"check": "linux_session", "ok": False, "detail": str(e)})
    try:
        checks.append(platform_shim.capture_selftest())
    except Exception as e:
        checks.append({"check": "capture", "ok": False, "detail": str(e)})
    try:
        collector = _get_collector()
        app_name = getattr(collector.profile, "capture_app", "") or ""
        checks.append(platform_shim.window_enum_selftest(app_name))
    except Exception as e:
        checks.append({"check": "window_enum", "ok": False, "detail": str(e)})

    # Input self-test (Windows: one-shot SendInput hook probe; Linux: evdev
    # readability + optional uinput round-trip — both via daemon --selftest).
    hook = {"check": "input_hooks", "ok": None,
            "detail": f"no one-shot input self-test on this OS (os={sys.platform})"}
    if platform_shim.IS_WINDOWS or platform_shim.IS_LINUX:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "python_backend.daemon.input_daemon", "--selftest"],
                cwd=str(_AV_ROOT), capture_output=True, text=True, timeout=10)
            for line in (proc.stdout or "").splitlines():
                if line.startswith("AV_SELFTEST_JSON "):
                    hook = json.loads(line[len("AV_SELFTEST_JSON "):])
                    break
        except Exception as e:
            hook = {"check": ("win_input_hooks" if platform_shim.IS_WINDOWS
                              else "linux_input_evdev"),
                    "ok": False, "detail": str(e)}
    checks.append(hook)

    dstat = _http_daemon_status_dict()
    # `ok` used to be hardcoded True, so a dead input daemon never appeared in
    # failed_checks and never flipped the overall verdict — a self-test that could
    # not fail its own check. It is now a real assertion, but a MISSING daemon is
    # not a fault on its own: input recording is optional, so absent-and-not-asked-
    # for reports ok with a note, while asked-for-but-dead reports ok=False.
    _d_running = bool(dstat.get("running"))
    _d_wanted = bool(getattr(_active_profile_obj(), "capture_user_input", False))
    checks.append({"check": "input_daemon",
                   "ok": (_d_running or not _d_wanted),
                   "running": _d_running, "pid": dstat.get("pid"),
                   "required_by_profile": _d_wanted,
                   "detail": (
                       f"daemon running={_d_running}"
                       + ("" if not _d_wanted else
                          " but this profile sets capture_user_input=true, so "
                          "input events are being LOST"
                          if not _d_running else " (required by profile)")
                       + ("" if _d_wanted else
                          " — not required: profile does not capture user input"))})

    failed = [c for c in checks if c.get("ok") is False]
    return jsonify({
        "schema_version": SCHEMA_VERSION,
        "os": sys.platform,
        "ok": len(failed) == 0,
        "failed_checks": [c["check"] for c in failed],
        "checks": checks,
        "note": ("Run `agentvision doctor` on the target host for the full "
                 "report incl. the emitter auto-injection round-trip."),
    }), 200


def _http_daemon_status_dict() -> dict:
    from utils import platform_shim
    pid_file = Path(os.environ.get("AGENTVISION_DAEMON_PID_FILE",
                                   str(platform_shim.daemon_pid_file())))
    out = {"running": False, "pid": None}
    try:
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            out["pid"] = pid
            out["running"] = platform_shim.pid_alive(pid)
    except Exception:
        pass
    return out


@app.route("/daemon/status", methods=["GET"])
def daemon_status():
    from utils import platform_shim
    pid_file = Path(os.environ.get("AGENTVISION_DAEMON_PID_FILE",
                                   str(platform_shim.daemon_pid_file())))
    active_file = _AV_ROOT / "python_backend" / "active_profile.txt"
    info: dict = {
        "pid_file":    str(pid_file),
        "active_file": str(active_file),
        "running":     False,
        "pid":         None,
        "active_profile": None,
        "active_sink":    None,
        "capture_user_input": False,
    }
    # Liveness check via PID file + kill(0).
    try:
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            info["pid"] = pid
            info["running"] = platform_shim.pid_alive(pid)
    except Exception as e:
        info["pid_error"] = str(e)
    # Resolve which profile + sink the daemon is currently writing to.
    try:
        if active_file.exists():
            name = active_file.read_text().strip()
            info["active_profile"] = name
            profiles = load_profiles()
            prof = profiles.get(name)
            if prof:
                # Same resolution as the daemon itself now performs: a
                # modern-bridged profile keeps its sink in log_sources, and
                # reporting the empty legacy field here said "no sink" about a
                # daemon that was writing happily.
                info["active_sink"] = resolve_action_log_path(prof)
                info["capture_user_input"] = bool(
                    getattr(prof, "capture_user_input", False))
    except Exception as e:
        info["profile_error"] = str(e)
    return jsonify(info), 200


# ── Install / Verify routes ───────────────────────────────────────────────────
# Install drops AgentVision's outbound emitter into the bridged program.
# Verify spawns a tiny Python in the project and confirms the emitter writes
# back into log/actions.jsonl — proving the "phone call" is two-way.

@app.route("/install", methods=["POST"])
def install_route():
    body = request.get_json(silent=True) or {}
    project_root = (body.get("project_root") or "").strip()
    profile_name = (body.get("profile_name") or "").strip()
    language     = (body.get("language") or "").strip()
    install_python_hook = bool(body.get("install_python_hook", True))
    if not project_root:
        return jsonify({"error": "project_root required"}), 400

    # Writing emitters into someone's program is the most consequential thing
    # AgentVision does, so it does not do it on a language guess. Until the agent
    # has reviewed the catalog and committed a plan, this refuses — the plan says
    # WHICH emitters, and /bridge/commit is what performs the install.
    # force=true keeps the CLI/GUI paths working for a human doing it manually.
    if not bool(body.get("force")):
        import bridge_plan as _bp
        prof = _active_profile_obj()
        folder = _plan_folder(prof)
        if not _bp.is_sealed(folder):
            return jsonify({
                "error": "BRIDGE_NOT_BUILT",
                "ok": False, "installed": False, "bridge_required": True,
                "DO_THIS_NEXT": "av_bridge_catalog()  ->  av_bridge_commit(plan=...)",
                "bridge": _bp.status(folder, prof),
                "guidance": (
                    "REFUSED — this is the one-time setup step, not a failure. "
                    "Refusing to scaffold emitters before the bridge is planned. "
                    "AgentVision would otherwise install the same fixed set for "
                    "every program of this language, which is a guess dressed as a "
                    "success. Call av_bridge_catalog(), decide what THIS program "
                    "needs, then av_bridge_commit(plan=...) — that performs the "
                    "install for exactly the emitters you selected. Pass force=true "
                    "to install the default set anyway."),
            }), 200
    try:
        from installer import install_into_project
    except Exception:
        from python_backend.installer import install_into_project
    try:
        report = install_into_project(
            project_root,
            profile_name=profile_name,
            install_python_hook=install_python_hook,
            language=language,
        )
    except Exception as e:
        return jsonify({"error": f"install failed: {e}"}), 500
    return jsonify({
        "project_root":      str(report.project_root),
        "language":          report.language,
        "emitter":           report.emitter,
        "created":           report.created,
        "existed":           report.existed,
        "skipped":           report.skipped,
        "errors":            [{"path": p, "error": e} for p, e in report.errors],
        "profile_overrides": report.profile_overrides,
        "summary":           report.summary(),
    }), 200


def _probe_spec(root: Path, language: str, emitter: dict):
    """Return (argv, extra_env, marker_prefix) for a per-language verify probe,
    or None if the language can't be actively probed (config/tee → static check).
    The probe is a tiny do-nothing program launched with the emitter's auto-load
    env; if the in-process emitter is wired, it emits av.<lang>.start into the
    sink, proving the round-trip."""
    lang = (language or "").lower()
    em = root / "agentvision" / "emitters"
    if lang == "python":
        env = {"PYTHONPATH": str(root) + os.pathsep + os.environ.get("PYTHONPATH", ""),
               "AGENTVISION_PROJECT": str(root),
               "AGENTVISION_LOG_DIR": str(root / "agentvision"),
               "AGENTVISION_SINK": str(root / "agentvision" / "actions.jsonl")}
        return ([sys.executable, "-c", "import time; time.sleep(0.1)"], env, "av.bootstrap.")
    if lang == "node":
        js = em / "av_emit.js"
        if js.exists() and _which("node"):
            return (["node", "-e", "setTimeout(function(){}, 60)"],
                    {"NODE_OPTIONS": f"--require {js}",
                     "AGENTVISION_SINK": str(root / "agentvision" / "actions.jsonl")},
                    "av.node.")
    if lang == "ruby":
        rb = em / "av_emit.rb"
        if rb.exists() and _which("ruby"):
            return (["ruby", "-e", "sleep 0.05"],
                    {"RUBYOPT": f"-r{rb}",
                     "AGENTVISION_SINK": str(root / "agentvision" / "actions.jsonl")},
                    "av.ruby.")
    return None


def _which(exe: str) -> bool:
    import shutil as _sh
    return _sh.which(exe) is not None


@app.route("/install/verify", methods=["POST"])
def install_verify_route():
    """Prove the OUTPUT side of the bridge is live for ANY language.

    Reads the scaffolded agentvision/manifest.json to learn the language +
    emitter. For in-process emitters (python/node/ruby) it spawns a tiny probe
    with the emitter's auto-load env and confirms an av.<lang>.* event lands in
    agentvision/actions.jsonl. For config/tee emitters (java/.net/go/rust/…),
    which can only emit when the real program runs, it does a STATIC check: the
    emitter files exist and the sink is writable (mode="static").

    Returns: {verified, mode, language, events_seen, last_event, sink, stderr}.
    """
    body = request.get_json(silent=True) or {}
    project_root = (body.get("project_root") or "").strip()
    timeout = float(body.get("timeout", 6.0))
    if not project_root:
        return jsonify({"error": "project_root required"}), 400
    root = Path(project_root).expanduser().resolve()
    if not root.exists():
        return jsonify({"error": f"project_root does not exist: {root}"}), 400

    # Learn language + emitter from the manifest the installer wrote.
    language = (body.get("language") or "").strip()
    emitter: dict = {}
    manifest_path = root / "agentvision" / "manifest.json"
    if manifest_path.exists():
        try:
            man = json.loads(manifest_path.read_text(errors="replace"))
            language = language or man.get("language", "")
            emitter = man.get("emitter", {}) or {}
        except Exception:
            pass

    sink = root / "agentvision" / "actions.jsonl"
    if not sink.exists():
        return jsonify({
            "verified": False, "mode": "none", "language": language,
            "events_seen": 0, "last_event": None, "stderr": "",
            "reason": f"sink missing: {sink} — run /install first",
        }), 200

    probe = _probe_spec(root, language, emitter)

    # ── Config/tee languages: static verification (no generic binary to run) ──
    if probe is None:
        em_dir = root / "agentvision" / "emitters"
        files_present = em_dir.exists() and any(em_dir.iterdir())
        writable = os.access(str(sink), os.W_OK)
        return jsonify({
            "verified":    bool(files_present and writable),
            "mode":        "static",
            "language":    language or "unknown",
            "events_seen": 0,
            "last_event":  None,
            "sink":        str(sink),
            "note": ("config/tee emitter — verified structurally (emitter files "
                     "present + sink writable). Runtime events flow when the "
                     "program is launched via `agentvision run -- <cmd>`."),
        }), 200

    argv, extra_env, marker = probe

    try:
        before_size = sink.stat().st_size
    except Exception:
        before_size = 0

    env = dict(os.environ)
    env.update(extra_env)
    env.pop("AGENTVISION_HOOKED", None)
    stderr_text = ""
    try:
        proc = subprocess.run(argv, cwd=str(root), env=env,
                              capture_output=True, text=True, timeout=timeout)
        stderr_text = proc.stderr or ""
    except subprocess.TimeoutExpired:
        stderr_text = "probe timed out"
    except Exception as e:
        return jsonify({"error": f"probe failed: {e}"}), 500

    time.sleep(0.25)   # let any atexit/exit emit flush

    new_events: list[dict] = []
    last_event: dict | None = None
    try:
        with sink.open("rb") as f:
            f.seek(before_size)
            tail = f.read()
        for raw in tail.decode("utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            src = rec.get("source", "")
            # Accept the language-specific marker OR any av.* lifecycle event.
            if isinstance(src, str) and (src.startswith(marker) or src.startswith("av.")):
                new_events.append(rec)
                last_event = rec
    except Exception as e:
        return jsonify({"error": f"tail failed: {e}"}), 500

    emitter_works = len(new_events) > 0

    # ── DOES IT AUTOLOAD? The question the agent and the user actually have ──
    # The probe above injects the emitter's env (PYTHONPATH / NODE_OPTIONS /
    # RUBYOPT), so it proves the emitter CODE works — not that it will load when
    # the user runs their program normally. Those are different claims, and
    # reporting the first as "verified" is a false green light: measured here, a
    # freshly installed project reported verified=true, events_seen=2 while
    # `python main.py` emitted absolutely nothing. That is the worst kind of bug —
    # install says done, verify says verified, and the program is still silent.
    #
    # So run the SAME probe again with the env REMOVED. If nothing lands, autoload
    # does not work in this environment and the program must be launched via
    # `agentvision run` (or the env set) to emit at all.
    autoloads = None
    autoload_detail = ""
    try:
        bare_env = dict(os.environ)
        for k in extra_env:
            bare_env.pop(k, None)
        bare_env.pop("AGENTVISION_HOOKED", None)
        try:
            mark = sink.stat().st_size
        except Exception:
            mark = 0
        subprocess.run(argv, cwd=str(root), env=bare_env,
                       capture_output=True, text=True, timeout=timeout)
        time.sleep(0.25)
        with sink.open("rb") as f:
            f.seek(mark)
            bare_tail = f.read().decode("utf-8", errors="replace")
        autoloads = any(
            '"source"' in ln and "av." in ln for ln in bare_tail.splitlines() if ln.strip())
        autoload_detail = (
            "the program emits with NO env set — logging is genuinely zero-effort"
            if autoloads else
            "the program emits ONLY when the emitter env is set; a plain "
            "`python your_script.py` will NOT log. Launch via "
            "`agentvision run -- <cmd>` (or export the emitter env) for capture. "
            "A project-local sitecustomize.py cannot fix this: `site` imports "
            "sitecustomize BEFORE the script's directory joins sys.path, so it "
            "never wins — measured on both Homebrew python and a clean venv.")
    except Exception as exc:
        autoload_detail = f"autoload check could not run: {exc}"

    return jsonify({
        # verified now means what the caller assumes it means: this program will
        # produce diagnostics. If it only works with the env injected, that is
        # reported honestly instead of being rounded up to success.
        "verified":       bool(emitter_works and autoloads),
        "emitter_works":  emitter_works,
        "autoloads":      autoloads,
        "autoload_detail": autoload_detail,
        "launch_command": f"agentvision run -- <your command>   (in {root})",
        "mode":        "spawn",
        "language":    language,
        "events_seen": len(new_events),
        "last_event":  last_event,
        "stderr":      stderr_text[-2000:],
        "sink":        str(sink),
        "before_size": before_size,
    }), 200


# ── Analysis / Search / Investigation surface (v5) ────────────────────────────
# A suite of HIGH-VALUE diagnostic routes that turn the rich per-frame + multi-log
# data into ranked, correlated, TOKEN-BOUNDED answers. Every route here summarizes,
# caps and paginates — it never dumps unbounded data. All frame-store access goes
# through _lock; everything is pure stdlib + cross-platform.

# Hard caps so no single call can blow the token budget.
_MAX_TIMELINE_ROWS = 1000
_MAX_SEARCH_HITS   = 500
_MAX_METRIC_FRAMES = 2000
_WAIT_FOR_CAP_S    = 120.0


def _frames_sorted(limit: int = 0, all_profiles: bool = False) -> list[dict]:
    """Snapshot of the ACTIVE PROFILE's in-memory frames, oldest→newest.
    Optionally keep only the most recent `limit`. Copies under _lock so callers
    never touch live dicts.

    The store is one seq-keyed dict shared by every profile (startup hydrates them
    all), so an unfiltered read mixed programs together: under the sharpemu
    profile this list carried a game bot's June frame at seq 1, and every derived
    read — /visual_changes, /timeline, error scans — silently included it. Pass
    all_profiles=True only when you really mean the whole store.
    """
    with _lock:
        frames = sorted((dict(f) for f in _frames.values()),
                        key=lambda f: f.get("sequence") or 0)
    if not all_profiles:
        try:
            f2p = _folder_to_profile()
            kept = []
            for f in frames:
                folder = str(f.get("program_folder") or "")
                if not folder:
                    # Live frames captured this session may not carry a folder;
                    # they are by definition the active profile's.
                    kept.append(f)
                    continue
                owner = f2p.get(str(Path(folder).resolve()), "")
                if not owner or owner == _active_profile_name:
                    kept.append(f)
            frames = kept
        except Exception as exc:
            _dbg(f"frame profile filter failed, returning unfiltered: {exc}")
    if limit and len(frames) > limit:
        frames = frames[-limit:]
    return frames


def _latest_for_active() -> dict | None:
    """The newest frame belonging to the ACTIVE profile.

    `_latest_frame` is the global highest sequence number across every hydrated
    profile, so whichever program has captured the most frames owns it. Observed:
    with the abyss profile active, the ambient push reported "AbyssEngine … latest
    frame #13016" and 78 changed moments — frame 13016 and all of those moments
    are SharpEmu's, and abyss had captured nothing at all. Every triage surface
    that opens with `latest_frame` inherited that misattribution.
    """
    with _lock:
        lf = dict(_latest_frame) if _latest_frame else None
    if lf is None:
        return None
    folder = str(lf.get("program_folder") or "")
    if not folder:
        # Captured live this session: by definition the active profile's.
        return lf
    try:
        owner = _folder_to_profile().get(str(Path(folder).resolve()), "")
    except Exception:
        return lf
    if not owner or owner == _active_profile_name:
        return lf
    mine = _frames_sorted()
    return mine[-1] if mine else None


def _changed_moment_count(min_change: float | None = None) -> int:
    """How many retained frames of the ACTIVE profile actually differ from their
    predecessor, by the same threshold /visual_changes uses. Recomputed from the
    frames on hand so the digest and /visual_changes cannot disagree."""
    try:
        from utils import visual_engine as ve
        thr = ve.MIN_CHANGE if min_change is None else min_change
    except Exception:
        thr = 0.008 if min_change is None else min_change
    n = 0
    for f in _frames_sorted():
        v = _visual_of(f)
        if not v:
            continue
        cs = v.get("change_score")
        if cs is None or cs >= thr:
            n += 1
    return n


def _frame_error(frame: dict) -> dict | None:
    """The frame's structured error block, or None if it carries no real error."""
    e = frame.get("error") or {}
    if isinstance(e, dict) and (e.get("message") or e.get("exception_type")):
        return e
    return None


def _error_sample(etype: str, msg: str) -> str:
    """A one-line human sample for an error group. Keeps the exception TYPE in
    front of the message when the message doesn't already start with it, so a
    bare KeyError arg ("'height'") still reads as "KeyError: 'height'" in the
    summary the agent sees. Never raises."""
    etype = str(etype or "").strip()
    msg = str(msg or "").strip()
    if not etype:
        return msg
    if not msg:
        return etype
    return msg if msg.lower().startswith(etype.lower()) else f"{etype}: {msg}"


def _error_groups(triggers: list[dict]) -> dict[str, dict]:
    """Dedup failure records by fingerprint → {fingerprint, count, sample,
    source, exception_type, probable_cause, first/last ts+ms}. Reuses
    diagnostics.probable_cause for a plain-English hypothesis when the record
    doesn't already carry one."""
    from modules.diagnostics import probable_cause as _pc
    from modules.diagnostics import parse_exception as _px
    groups: dict[str, dict] = {}
    for r in triggers:
        fp = _record_fingerprint(r)
        d = r.get("data") if isinstance(r.get("data"), dict) else {}
        ms = r.get("ts_ms") or _iso_to_ms(r.get("ts", "")) or 0.0
        # The exception TYPE arrives under different keys depending on who wrote
        # the record: our own emitters use `type` (av.bootstrap.excepthook),
        # normalized log adapters may use `exception_type`, and other producers
        # use exc_type/error_type/class. Reading only `exception_type` silently
        # lost the type for our OWN richest source — which then made
        # probable_cause() a no-op (it needs the type or a recognisable
        # message), so the agent saw "error: 'height'" instead of
        # "KeyError: 'height' — a dict was accessed with a missing key".
        etype = ((d.get("exception_type") or d.get("type") or d.get("exc_type")
                  or d.get("error_type") or d.get("class") or "") if d else "")
        msg = (d.get("message") or d.get("error") or d.get("name")
               or d.get("reason") or "") if d else ""
        cause = (d.get("probable_cause") or "") if d else ""
        # Structured records often carry the full traceback too; parse it as a
        # last resort so a stack-bearing record is never less informative than
        # a scraped text log.
        if (not etype or not cause) and d:
            tb = d.get("traceback") or d.get("stack") or d.get("stacktrace") or ""
            if tb:
                try:
                    px = _px(str(tb)) or {}
                except Exception:
                    px = {}
                etype = etype or (px.get("exception_type") or "")
                msg = msg or (px.get("message") or "")
                cause = cause or (px.get("probable_cause") or "")
        if not cause:
            cause = _pc(etype, str(msg))
        g = groups.get(fp)
        if g is None:
            g = groups[fp] = {
                "fingerprint": fp, "count": 0, "sample": str(msg)[:140],
                "source": r.get("source"), "exception_type": etype or None,
                "probable_cause": cause or None,
                "first_ms": ms, "last_ms": ms,
                "first_ts": r.get("ts"), "last_ts": r.get("ts"),
            }
        g["count"] += 1
        if ms and ms < g["first_ms"]:
            g["first_ms"], g["first_ts"] = ms, r.get("ts")
        if ms >= g["last_ms"]:
            g["last_ms"], g["last_ts"] = ms, r.get("ts")
        if etype and not g["exception_type"]:
            g["exception_type"] = etype
        if cause and not g["probable_cause"]:
            g["probable_cause"] = cause
    return groups


def _window_from_args() -> tuple[float | None, float | None, tuple | None]:
    """Parse optional from_ms/to_ms query params → (from_ms, to_ms, window_tuple).
    window_tuple is None when neither is supplied (means 'most recent by count')."""
    from_arg = request.args.get("from_ms")
    to_arg   = request.args.get("to_ms")
    try:
        from_ms = float(from_arg) if from_arg is not None else None
        to_ms   = float(to_arg)   if to_arg   is not None else None
    except ValueError:
        abort(400)
    window = None
    if from_arg is not None or to_arg is not None:
        window = (from_ms if from_ms is not None else float("-inf"),
                  to_ms   if to_ms   is not None else float("inf"))
    return from_ms, to_ms, window


@app.route("/diagnose", methods=["GET"])
def diagnose():
    """THE FLAGSHIP root-cause assessment. Deterministically correlates the recent
    window — deduped structured errors, active anomalies, latest state_delta/perf,
    program liveness, capture health, and the tail of WARN/ERROR log events — into
    a RANKED list of hypotheses. No LLM is called; this is the correlation the AI
    then reasons over. Hypotheses are ranked by severity × recency × recurrence."""
    prof = _active_profile_obj()
    collector = _get_collector()
    e = _auto_engine
    now = time.time() * 1000.0

    frames = _frames_sorted()
    with _lock:
        latest = _latest_for_active()

    # ── Deduped error signals ────────────────────────────────────────────────
    triggers = _detect_failure_records(limit=3000)
    groups = _error_groups(triggers)
    top_errors = sorted(groups.values(), key=lambda g: -g["count"])[:5]

    # New-this-session fingerprints (highest priority — just appeared).
    history = _load_fp_history()
    new_fps = [fp for fp, g in groups.items()
               if g["last_ms"] >= _session_start_ms and fp not in history]

    # Map fingerprint → frame seqs that carry it (evidence handles).
    fp_frames: dict[str, list[int]] = {}
    for f in frames:
        fe = _frame_error(f)
        if fe and fe.get("fingerprint"):
            fp_frames.setdefault(fe["fingerprint"], []).append(f.get("sequence"))

    def _recency(ms: float) -> float:
        if not ms:
            return 0.0
        age_s = max(0.0, (now - ms) / 1000.0)
        return 1.0 / (1.0 + age_s / 60.0)   # ~0.5 at 60s old, →0 as it ages

    hypotheses: list[dict] = []

    # (a) Program not running — usually the dominant signal.
    if latest and not (latest.get("program") or {}).get("running"):
        hypotheses.append({
            "summary": "Target program is NOT running (crashed, exited, or not started).",
            "confidence": 0.8,
            "_score": 100.0,
            "evidence": [f"frame:{latest.get('sequence')}",
                         "program.running=false"],
            "probable_cause": "Process is not alive — inspect the last log lines before exit.",
            "recommended_next": ["av_program_log(lines=60)", "av_program_status()",
                                 f"av_actions_around_frame(seq={latest.get('sequence')})"],
        })

    # (b) One hypothesis per top recurring/severe error fingerprint.
    for g in top_errors:
        fp = g["fingerprint"]
        count = g["count"]
        cause = g.get("probable_cause") or ""
        blob = f"{g.get('exception_type') or ''} {g.get('sample') or ''}".lower()
        severity = 1.3 if any(k in blob for k in
                              ("fatal", "panic", "segv", "abort", "oom", "crash")) else 1.0
        recency = _recency(g["last_ms"])
        is_new = fp in new_fps
        score = severity * (0.3 + 0.7 * recency) * (1.0 + 0.3 * min(count - 1, 9))
        if is_new:
            score *= 1.4
        conf = 0.4
        if count > 1: conf += 0.15
        if count >= 5: conf += 0.1
        if is_new: conf += 0.2
        if cause: conf += 0.15
        if recency > 0.5: conf += 0.1
        conf = max(0.05, min(0.95, conf))
        seqs = [s for s in fp_frames.get(fp, []) if s is not None][-3:]
        etype = g.get("exception_type") or "error"
        # _error_sample avoids "KeyError: KeyError: 'height'" when the sample
        # already leads with the type (normalized adapters often include it).
        summary = _error_sample(etype, g.get("sample") or "").strip(": ")[:160]
        if count > 1:
            summary += f"  (recurred {count}×)"
        if is_new:
            summary = "NEW this session — " + summary
        evidence = [f"fingerprint:{fp}", f"count:{count}",
                    f"last_seen:{g.get('last_ts')}"]
        evidence += [f"frame:{s}" for s in seqs]
        rec = [f"av_errors_by_fingerprint(fp='{fp}')"]
        if seqs:
            rec.append(f"av_actions_around_frame(seq={seqs[-1]})")
        rec.append(f"av_timeline(from_ms={int(g['last_ms'] - 5000)}, "
                   f"to_ms={int(g['last_ms'] + 2000)})")
        hypotheses.append({
            "summary": summary,
            "confidence": round(conf, 2),
            "_score": round(score, 3),
            "evidence": evidence[:6],
            "probable_cause": cause or None,
            "recommended_next": rec[:4],
        })

    # (c) Active anomaly on the latest frame (stuck / spike / …).
    anom = (latest or {}).get("anomaly") or {}
    if anom.get("detected"):
        conf = _safe_float(anom.get("confidence"), 0.6)
        hypotheses.append({
            "summary": anom.get("description") or f"Anomaly: {anom.get('type')}",
            "confidence": round(conf, 2),
            "_score": 40.0 + 30.0 * conf,
            "evidence": [f"frame:{latest.get('sequence')}",
                         f"anomaly.type:{anom.get('type')}",
                         f"severity:{anom.get('severity')}"],
            "probable_cause": None,
            "recommended_next": [f"av_get_frame(seq={latest.get('sequence')})",
                                 f"av_timeline(limit=40)"],
        })

    # (d) Capture-health problem — flagged low so it never masks a real bug, but
    #     surfaced so the AI doesn't trust a bad grab.
    if e.window_missing or e.blank_frame_count:
        bits = []
        if e.window_missing:
            bits.append("capture window not found (screenshots may be full-screen)")
        if e.blank_frame_count:
            bits.append(f"{e.blank_frame_count} blank/black frame(s)")
        hypotheses.append({
            "summary": "Capture health issue — " + "; ".join(bits) + ".",
            "confidence": 0.5,
            "_score": 20.0,
            "evidence": ["capture.window_missing=" + str(e.window_missing),
                         f"capture.blank_frame_count={e.blank_frame_count}"],
            "probable_cause": "The target window may be minimized/occluded or the "
                              "profile capture_app is wrong.",
            "recommended_next": ["av_capture_status()", "av_program_crop()"],
        })

    hypotheses.sort(key=lambda h: -h["_score"])
    for h in hypotheses:
        h.pop("_score", None)
    hypotheses = hypotheses[:6]

    # ── Tail of WARN/ERROR normalized events, COALESCED ──────────────────────
    # A stuck subsystem repeats one line forever. Emitting the raw tail sent ten
    # byte-identical copies of "guestgpu.present ok=False" — ten times the tokens
    # for one fact, and it buried how BAD it was: the real story is "180x", not
    # "here are 10 lines". Group by (source, message) and report a count.
    recent_warnings = []
    warn_total = 0
    try:
        warn_events = _log_sources.read_normalized(prof, window=None, limit=1000,
                                                   level="WARN")
        warn_total = len(warn_events)
        groups: dict[tuple, dict] = {}
        for ev in warn_events:
            d = ev.get("data") or {}
            msg = str(d.get("message") or d.get("name") or ev.get("raw") or "")[:140]
            src = ev.get("source") or ev.get("log_label")
            key = (src, msg)
            g = groups.get(key)
            if g is None:
                groups[key] = {
                    "ts_ms": ev.get("ts_ms"), "level": ev.get("level"),
                    "source": src, "message": msg, "count": 1,
                    "first_ts_ms": ev.get("ts_ms"), "last_ts_ms": ev.get("ts_ms"),
                }
                if ev.get("level_escalated_from"):
                    groups[key]["escalation_reason"] = ev.get("escalation_reason")
            else:
                g["count"] += 1
                g["last_ts_ms"] = ev.get("ts_ms")
        # Most repeated first: a line seen 180 times is the headline.
        recent_warnings = sorted(groups.values(), key=lambda g: -g["count"])[:10]
    except Exception:
        pass

    # ── Latest state_delta + perf (compact) ──────────────────────────────────
    latest_delta = None
    latest_perf = None
    if latest:
        sd = latest.get("state_delta") or {}
        changed = sd.get("changed") or {}
        note = ""
        if changed:
            parts = [f"{k}: {v.get('from')}→{v.get('to')}"
                     for k, v in list(changed.items())[:4]]
            note = "; ".join(parts) + ("" if len(changed) <= 4 else f" (+{len(changed) - 4} more)")
        latest_delta = {"changed_count": sd.get("changed_count"),
                        "added_count": sd.get("added_count"),
                        "removed_count": sd.get("removed_count"),
                        "note": note or None}
        p = latest.get("perf") or {}
        if p:
            latest_perf = {k: p.get(k) for k in ("cpu_percent", "rss_mb", "num_threads")}

    # ── Top signals (ranked, human one-liners) ───────────────────────────────
    top_signals: list[str] = []
    if latest and not (latest.get("program") or {}).get("running"):
        top_signals.append("program not running")
    if new_fps:
        top_signals.append(f"{len(new_fps)} NEW error fingerprint(s) this session")
    if top_errors:
        top_signals.append(f"top error recurred {top_errors[0]['count']}× "
                           f"({(top_errors[0]['sample'] or '')[:60]})")
    if anom.get("detected"):
        top_signals.append(f"anomaly: {anom.get('type')}")
    if e.window_missing:
        top_signals.append("capture window missing")
    if e.blank_frame_count:
        top_signals.append(f"{e.blank_frame_count} blank frame(s)")
    if recent_warnings:
        # Report the TOTAL and the worst offender, not the group count — "10
        # groups" hid that one line had fired 180 times.
        worst = recent_warnings[0]
        top_signals.append(
            f"{warn_total} warn/error log event(s) in "
            f"{len(recent_warnings)} distinct message(s)")
        if worst.get("count", 1) >= 5:
            top_signals.append(
                f"REPEATING {worst['count']}x: {str(worst.get('message'))[:90]}")
    # Failure-shaped lines from the log-source pipeline (text logs included), not
    # just actions.jsonl. Computed BEFORE the all-clear below so the "looks
    # healthy" line cannot be printed over a program that is visibly failing.
    _txt_fail = _text_log_failures(prof)
    if _txt_fail.get("total"):
        _w = (_txt_fail.get("groups") or [{}])[0]
        hypotheses.append({
            "rank": len(hypotheses) + 1,
            "hypothesis": (f"{_w.get('source') or 'log'} reports failure "
                           f"{_w.get('count')}x: {str(_w.get('message'))[:110]}"),
            "severity": "high" if int(_w.get("count") or 0) >= 20 else "medium",
            "evidence": (f"{_txt_fail['total']} failure-shaped line(s) in the "
                         f"log-source pipeline"
                         + (f"; escalated because {_w.get('escalation_reason')}"
                            if _w.get("escalated") else "")),
            "why_not_in_fingerprints": (
                "these lines are in a TEXT log, not actions.jsonl — "
                "av_errors_by_fingerprint only groups JSONL records"),
            "next": ["av_log_raw()",
                     f"av_search(q={str(_w.get('message'))[:40]!r})"],
        })
        top_signals.insert(
            0, f"{_w.get('count')}x {str(_w.get('message'))[:70]}")
    # ── Exceptions the program ate ───────────────────────────────────────────
    # A run that swallows exceptions and still reports success is the single
    # hardest failure to see from outside, and the `swallowed_exceptions`
    # emitter exists to make it visible. Producing NO hypothesis from it left
    # av_diagnose returning hypotheses:[] on a program that had silently dropped
    # 40% of its input — while its own counts field said 2. Hedged on purpose:
    # the counts and the site are read straight off the log, but whether the
    # catch is a BUG is not something AgentVision can know.
    try:
        _he = _handled_exceptions()
        if int(_he.get("total_occurrences") or 0) > 0:
            _site = (_he.get("sites") or [{}])[0]
            _where = (f"{_site.get('type') or 'exception'} at "
                      f"{_site.get('raised_at') or '?'}"
                      + (f", handled in {_site.get('handled_in')}"
                         if _site.get("handled_in") else ""))
            hypotheses.append({
                "rank": len(hypotheses) + 1,
                "hypothesis": (
                    f"{_he['total_occurrences']} exception(s) were raised and "
                    f"SILENTLY HANDLED across {_he['distinct_sites']} site(s) — "
                    f"{_where}. Nothing else in the program records these, so a "
                    f"run can report success having done less work than "
                    f"expected."),
                "severity": "medium",
                # Deliberately not high: the counts and the location are
                # certain, the FAULT is not. A retry loop that catches and
                # retries produces this same record and is perfectly healthy.
                "confidence": 0.6,
                "confidence_basis": (
                    "the count and the raise site are read directly off the "
                    "emitter's own records (near-certain). That the catch is a "
                    "DEFECT is not established — catching may be deliberate. "
                    "Check whether the swallowed path silently skips work."),
                "evidence": {"handled_exceptions": _he},
                "not_a_failure": (
                    "these are NOT counted in total_failures or health, because "
                    "the program handled them"),
                "next": ["av_errors_by_fingerprint()  (see handled_exceptions)",
                         f"av_source_file(path={str(_site.get('raised_at') or '').split(':')[0]!r})",
                         "av_log_raw()"],
            })
            top_signals.insert(
                0, f"{_he['total_occurrences']}x exception silently handled — "
                   f"{_where}")
    except Exception as exc:
        _dbg(f"handled-exception hypothesis failed: {exc}")

    # ── Is any of the above even current? ────────────────────────────────────
    # Every hypothesis so far is derived from the log sources. If a source is
    # stale or untimestamped, that has to be said BEFORE the hypotheses, because
    # it changes what they mean: on this profile the top hypothesis described a
    # GPU failure that stopped being observable 47 h earlier.
    _fresh = _source_freshness(prof, ref_ms=None)
    _smap = _source_event_map(prof)
    # Recovery: "did it recover?" is half of every debugging question and was not
    # represented at all — failures were counted and successes discarded, so a run
    # that healed looked exactly like one that stayed broken.
    _recov = _recovery_report(prof)
    for _op in (_recov.get("operations") or []):
        if int(_op.get("failed") or 0) < 2:
            continue
        _chg = _op.get("changed_fields") or []
        hypotheses.append({
            "rank": len(hypotheses) + 1,
            "hypothesis": (
                f"{_op['operation']} failed {_op['failed']}x then succeeded "
                f"{_op['succeeded']}x — "
                + ("RECOVERED" if _op.get("recovered")
                   else "still in the failing state")
                + (f"; {_chg[0]['field']} changed "
                   f"{_chg[0]['while_failing']} -> {_chg[0]['when_it_worked']}"
                   if _chg else "")),
            "severity": "medium" if _op.get("recovered") else "high",
            # The counts and the changed field are read off the log; the implied
            # causal link is NOT, so this is deliberately not scored near 1.
            "confidence": 0.75,
            "confidence_basis": (
                "counts and the changed field are read directly off the log "
                "(near-certain); that the change EXPLAINS the recovery is "
                "ordering-based inference, which is what holds this below the "
                "directly measured findings"),
            "evidence": (
                f"last failure: {_op.get('last_failure_line')} | first success: "
                f"{_op.get('first_success_line')} | "
                f"{_op.get('gap_line_count', 0)} line(s) logged in between: "
                f"{_op.get('lines_between_last_failure_and_first_success')}"),
            "confidence_note": (
                "The counts and the field change are read directly off the log. "
                "That the change CAUSED the recovery is not established — the log "
                "shows correlation and ordering only."),
            "next": ["av_log_raw()", "av_diagnose()"],
        })
    for _s in (_fresh.get("sources") or []):
        if _s.get("verdict") in ("stale", "self-emitted-only", "untimestamped",
                                 "missing"):
            hypotheses.insert(0, {
                "rank": 0,
                # The instruction has to match the verdict: a STALE source means
                # "historical", an UNTIMESTAMPED one means "unorderable in time",
                # and a MISSING one means "absent from this read entirely". One
                # generic sentence for all three would misdescribe two of them.
                "hypothesis": (
                    f"Log source '{_s['label']}' is {_s['verdict'].upper()} — "
                    + {"untimestamped":
                       "its events cannot be placed on the timeline or paired "
                       "with a frame at all; you have their ORDER and nothing more",
                       "missing":
                       "nothing from it is present in this read",
                       "self-emitted-only":
                       "it contains only AgentVision's own records, none of the "
                       "program's",
                       }.get(_s["verdict"],
                             "read every finding derived from it as historical, "
                             "not current")),
                "severity": "high" if _s["verdict"] != "untimestamped" else "medium",
                # Measured directly from the file (mtime + parsed timestamps),
                # not inferred, so this is as near certain as anything here gets.
                "confidence": 0.97,
                "confidence_basis": (
                    "measured from the source file's own timestamps and mtime"),
                "evidence": _s.get("why") or "",
                "why_it_matters": (
                    "AgentVision's whole read is derived from these sources; an "
                    "unqualified finding from a stale or untimestamped source "
                    "will be mistaken for the program's present state."),
                "next": ["av_log_sources()", "av_program_status()"],
            })
    if _fresh.get("program_silent_s") is not None and \
            _fresh["program_silent_s"] > SOURCE_STALE_S:
        hypotheses.insert(0, {
            "rank": 0,
            "hypothesis": (
                f"The program has emitted NOTHING for "
                f"{_human_age(_fresh['program_silent_s'])} — this is a "
                f"post-mortem read, not a live one"),
            "severity": "high",
            "confidence": 0.97,
            "confidence_basis": (
                "measured: newest program-emitted record across every declared "
                "source, with AgentVision's own watchdog records excluded"),
            "evidence": (
                f"newest program-emitted record across all sources is "
                f"{_fresh['program_silent_s']:.0f}s old. This is measured from "
                f"record timestamps, not from file mtime: a log file can keep "
                f"being touched (rotation, a tee, an earlier AgentVision's "
                f"watchdog) while the program itself is silent, so file "
                f"freshness is not program liveness."),
            "next": ["av_program_status()", "av_log_sources()"],
        })
    hypotheses = _normalize_hypotheses(hypotheses)

    if not top_signals:
        top_signals.append("no strong failure signals — program looks healthy")

    _running_now_d = None
    try:
        _running_now_d = bool(collector._reader.is_running())
    except Exception:
        pass
    health = _health_block(latest, new_fps, top_errors, e,
                           text_failures=_txt_fail, recovery=_recov,
                           running_now=_running_now_d,
                           program_silent_s=_fresh.get("program_silent_s"))
    # An all-clear in top_signals must not contradict the health grade computed
    # from the same inputs.
    if (health.get("grade") != "healthy"
            and top_signals == ["no strong failure signals — program looks healthy"]):
        top_signals = [
            f"no per-frame failure signal, but health grades "
            f"{str(health.get('grade')).upper()} — see health.factors"]

    return jsonify({
        "generated_ms": now,
        "program": collector.profile.display_name,
        "active_profile": _active_profile_name,
        "health": health,
        "hypotheses": hypotheses,
        # The cap must hold every signal the builders above can emit at once
        # (currently 10: 8 appends + the two insert(0) prepends). At 8, the
        # handled-exception and text-failure prepends silently pushed the LAST
        # two signals off the list — dropping a distinct signal to make room
        # for another is exactly the ranking this project forbids.
        "top_signals": top_signals[:12],
        "log_sources_freshness": _fresh,
        "log_source_event_map": _smap,
        "recovery": _recov,
        "latest_frame_seq": (latest or {}).get("sequence"),
        "latest_state_delta": latest_delta,
        "latest_perf": latest_perf,
        "recent_warnings": recent_warnings,
        "counts": {"distinct_error_fingerprints": len(groups),
                   "new_this_session": len(new_fps),
                   "frames_in_memory": len(frames)},
        "guidance": ("Ranked root-cause hypotheses (severity × recency × recurrence). "
                     "Work them top-down; each carries evidence handles (frame seqs / "
                     "fingerprints / log timestamps) and the exact follow-up tool calls."),
    }), 200


def _safe_float(v, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


@app.route("/timeline", methods=["GET"])
def timeline():
    """Unified, ts-sorted, token-bounded merge of EVERYTHING in a window: frames
    (seq + summary + error/anomaly), normalized log events from ALL sources
    (which already fold in the input daemon), and auto-detected failure bookmarks.
    Each row is a compact {ts_ms, kind, source, line}. Query: from_ms, to_ms
    (default = most recent `limit` rows), limit (default 200, cap 1000)."""
    from_ms, to_ms, window = _window_from_args()
    limit = max(1, min(_MAX_TIMELINE_ROWS, _int_arg("limit", 200)))
    prof = _active_profile_obj()

    def _in_window(ms):
        if window is None or ms is None:
            return True
        return window[0] <= ms <= window[1]

    rows: list[dict] = []

    # Frames
    for f in _frames_sorted():
        ms = float(f.get("timestamp_ms") or 0)
        if not ms or not _in_window(ms):
            continue
        err = f.get("error") or {}
        anom = f.get("anomaly") or {}
        line = f.get("summary") or ""
        if not line:
            bits = []
            if err.get("exception_type") or err.get("message"):
                bits.append(f"ERROR {err.get('exception_type') or ''}: "
                            f"{(err.get('message') or '')[:70]}")
            if anom.get("detected"):
                bits.append(f"anomaly:{anom.get('type')}")
            line = "; ".join(bits) or "frame"
        rows.append({"ts_ms": ms, "kind": "frame", "source": "capture",
                     "seq": f.get("sequence"), "line": line[:160]})

    # Normalized log events (all sources → includes input-daemon records)
    try:
        events = _log_sources.read_normalized(prof, window=window,
                                              limit=max(limit * 3, 600))
        for ev in events:
            ms = ev.get("ts_ms")
            d = ev.get("data") or {}
            msg = d.get("message") or d.get("name") or ev.get("raw") or ""
            row = {"ts_ms": ms, "kind": "log",
                   "source": ev.get("source") or ev.get("log_label"),
                   "level": ev.get("level"),
                   "line": str(msg)[:160]}
            if ms is None:
                # An event from an UNTIMESTAMPED source (SharpEmu's log.txt has
                # 451 such lines, including all 180 GPU failures). It used to be
                # coerced to ts_ms=0.0, which sorted it to the front as the
                # oldest thing that ever happened and made it the FIRST casualty
                # of rows[-limit:] — the opposite of read_normalized(), which
                # deliberately keeps untimestamped events at the end so they
                # survive. Marked and sorted last instead of faked.
                row["ts_ms"] = None
                row["untimestamped"] = True
                row["ordering"] = ("source order only — this line carries no "
                                   "parseable timestamp and cannot be aligned "
                                   "to a frame")
            rows.append(row)
    except Exception:
        pass

    # Failure bookmarks (AgentVision's auto-flagged failure points)
    for t in _detect_failure_records(limit=500):
        ms = t.get("ts_ms") or _iso_to_ms(t.get("ts", "")) or 0.0
        if not _in_window(ms):
            continue
        d = t.get("data") or {}
        label = d.get("name") or d.get("message") or t.get("source") or "failure"
        rows.append({"ts_ms": ms, "kind": "bookmark", "source": t.get("source"),
                     "line": ("FAILURE " + str(label))[:160]})

    # Truncation is where this went wrong, in both directions. Coercing
    # untimestamped rows to ts_ms=0.0 sorted them to the front as "oldest" and
    # rows[-limit:] dropped them first, losing them entirely. Simply sorting them
    # last inverts the bug: 30 untimestamped lines would then fill a limit=10
    # window and evict every real timeline row. So the two groups are truncated
    # SEPARATELY, with a small reserved share for the untimestamped ones, and
    # whatever was dropped is counted in the response instead of vanishing.
    timed = sorted((r for r in rows if r.get("ts_ms") is not None),
                   key=lambda r: r["ts_ms"])
    untimed = [r for r in rows if r.get("ts_ms") is None]
    total = len(rows)
    n_untimed = len(untimed)
    reserve = min(n_untimed, max(1, limit // 4)) if n_untimed else 0
    kept_timed = timed[-(limit - reserve):] if limit - reserve > 0 else []
    kept_untimed_rows = untimed[-reserve:] if reserve else []
    rows = kept_timed + kept_untimed_rows      # untimestamped last, as ordering
    kept_untimed = len(kept_untimed_rows)
    return jsonify({
        "from_ms": from_ms, "to_ms": to_ms,
        "window": "explicit" if window is not None else "none (most recent by count)",
        "count": len(rows), "total_available": total,
        "truncated": total > len(rows),
        "timestamped_rows": len(kept_timed),
        "timestamped_available": len(timed),
        "untimestamped_rows": kept_untimed,
        "untimestamped_available": n_untimed,
        "untimestamped_note": (
            "rows with ts_ms=null come from a source with no parseable "
            "timestamps; their position is source order, not time, and they "
            "cannot be aligned to a frame. They are truncated separately from "
            f"the timestamped rows ({reserve} of {limit} rows reserved) so "
            "neither group can crowd the other out" if n_untimed else ""),
        "kinds": ["frame", "log", "bookmark"],
        "rows": rows,
    }), 200


@app.route("/search", methods=["GET"])
def search():
    """The AI's grep over the normalized event stream + frame summaries. Query:
    q (required), regex (0/1 — substring vs regex, case-insensitive either way),
    level (min canonical level), category, source (substring), trace_id, from_ms,
    to_ms, limit (default 100, cap 500). Returns compact matches with ts + which
    source/frame they came from."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "q parameter required"}), 400
    use_regex = request.args.get("regex", "0") in ("1", "true", "True")
    level = (request.args.get("level") or "").strip()
    category = (request.args.get("category") or "").strip()
    source = (request.args.get("source") or "").strip()
    trace_id = (request.args.get("trace_id") or "").strip()
    from_ms, to_ms, window = _window_from_args()
    limit = max(1, min(_MAX_SEARCH_HITS, _int_arg("limit", 100)))

    if use_regex:
        try:
            pat = re.compile(q, re.IGNORECASE)
        except re.error as ex:
            return jsonify({"error": f"invalid regex: {ex}", "q": q}), 400
        def _match(text): return bool(pat.search(text))
    else:
        needle = q.lower()
        def _match(text): return needle in text.lower()

    prof = _active_profile_obj()
    matches: list[dict] = []
    scanned = 0

    # Event-stream matches
    try:
        events = _log_sources.read_normalized(prof, window=window, limit=5000,
                                              level=level)
        for ev in events:
            if len(matches) >= limit:
                break
            scanned += 1
            if category and ev.get("category") != category:
                continue
            if source and source not in str(ev.get("source") or ""):
                continue
            if trace_id and ev.get("trace_id") != trace_id:
                continue
            d = ev.get("data") or {}
            msg = str(d.get("message") or d.get("name") or "")
            hay = msg + " " + str(ev.get("raw") or "")
            if not _match(hay):
                continue
            matches.append({
                "ts_ms": ev.get("ts_ms"), "ts": ev.get("ts"),
                "kind": "log", "level": ev.get("level"),
                "category": ev.get("category"),
                "source": ev.get("source") or ev.get("log_label"),
                "trace_id": ev.get("trace_id"),
                "message": msg[:200],
            })
    except Exception:
        pass

    # Frame-summary matches (only when not narrowed to a log-only filter)
    if not (category or source or trace_id) and len(matches) < limit:
        for f in _frames_sorted():
            if len(matches) >= limit:
                break
            ms = float(f.get("timestamp_ms") or 0)
            if window is not None and ms and not (window[0] <= ms <= window[1]):
                continue
            err = f.get("error") or {}
            hay = " ".join([str(f.get("summary") or ""),
                            " ".join(f.get("tags") or []),
                            str(err.get("message") or ""),
                            str(err.get("exception_type") or "")])
            if not _match(hay):
                continue
            matches.append({
                "ts_ms": ms or None, "ts": f.get("timestamp"),
                "kind": "frame", "seq": f.get("sequence"),
                "summary": str(f.get("summary") or "")[:200],
                "tags": f.get("tags") or [],
            })

    matches.sort(key=lambda m: m.get("ts_ms") or 0.0)
    return jsonify({
        "q": q, "regex": use_regex,
        "filters": {"level": level or None, "category": category or None,
                    "source": source or None, "trace_id": trace_id or None,
                    "from_ms": from_ms, "to_ms": to_ms},
        "count": len(matches), "truncated": len(matches) >= limit,
        "matches": matches,
    }), 200


@app.route("/wait_for", methods=["POST", "GET"])
def wait_for():
    """Block server-side (bounded) until a condition appears, then return it.
    Body/query: condition ('log'|'error_fingerprint'|'anomaly'|'frame'; inferred
    from the other fields when omitted), regex/level/category/source (for 'log'),
    timeout (default 30s, HARD CAP 120s), poll_interval (default 1s). Polls
    internally and ALWAYS returns by the cap. Use it after triggering an action to
    catch the result. Returns {matched, condition, waited_ms, matched_event|matched_frame}."""
    body = request.get_json(silent=True) or {} if request.method == "POST" else {}
    def _p(name, default=None):
        if name in body:
            return body[name]
        return request.args.get(name, default)

    try:
        timeout = float(_p("timeout", 30.0))
    except (TypeError, ValueError):
        timeout = 30.0
    timeout = max(0.5, min(_WAIT_FOR_CAP_S, timeout))
    try:
        poll = float(_p("poll_interval", 1.0))
    except (TypeError, ValueError):
        poll = 1.0
    poll = max(0.2, min(10.0, poll))

    regex = (_p("regex") or "").strip()
    level = (_p("level") or "").strip()
    category = (_p("category") or "").strip()
    source = (_p("source") or "").strip()
    condition = (_p("condition") or "").strip()
    #: The inference used to read `"log" if (regex or level or category or source)
    #: else "log"` — both arms identical, so it was not an inference at all, just
    #: a default dressed as one. There genuinely is only one thing to infer from
    #: those fields, so the default stays "log"; what changes is that the caller
    #: is TOLD it was defaulted rather than believing a condition was deduced.
    condition_inferred = not condition
    if not condition:
        condition = "log"

    pat = None
    if regex:
        try:
            pat = re.compile(regex, re.IGNORECASE)
        except re.error as ex:
            return jsonify({"error": f"invalid regex: {ex}"}), 400

    prof = _active_profile_obj()
    start = time.monotonic()
    start_ms = time.time() * 1000.0

    # Baselines captured at t0 so we only match things that appear AFTER the call.
    with _lock:
        base_max_seq = max((f.get("sequence") or 0 for f in _frames.values()),
                           default=0)
        _blf = _latest_for_active()
    base_fps = set(_error_groups(_detect_failure_records(limit=3000)).keys())
    # 'anomaly' had NO t0 baseline: it returned whatever anomaly the latest frame
    # already carried, so a wait that never actually waited reported `matched:
    # true` for a condition that predated the call. Anchor it to the frame that
    # was latest at t0 — a match now requires a NEWER frame carrying one.
    base_anomaly_seq = (_blf or {}).get("sequence") or 0
    base_anomaly = bool(((_blf or {}).get("anomaly") or {}).get("detected"))

    def _check():
        if condition == "frame":
            with _lock:
                newest = max((dict(f) for f in _frames.values()),
                             key=lambda f: f.get("sequence") or 0, default=None)
            if newest and (newest.get("sequence") or 0) > base_max_seq:
                return {"matched_frame": {
                    "seq": newest.get("sequence"),
                    "timestamp_ms": newest.get("timestamp_ms"),
                    "summary": newest.get("summary"),
                    "tags": newest.get("tags") or []}}
            return None
        if condition == "error_fingerprint":
            groups = _error_groups(_detect_failure_records(limit=3000))
            for fp, g in groups.items():
                if fp not in base_fps:
                    return {"matched_event": {
                        "fingerprint": fp, "count": g["count"],
                        "sample": g["sample"], "source": g["source"],
                        "probable_cause": g["probable_cause"],
                        "last_ts": g["last_ts"]}}
            return None
        if condition == "anomaly":
            with _lock:
                lf = _latest_for_active()
            a = (lf or {}).get("anomaly") or {}
            seq = (lf or {}).get("sequence") or 0
            # A pre-existing anomaly on the t0 frame is not a match: this tool
            # promises "wait until it appears", not "tell me it is already true".
            if a.get("detected") and seq > base_anomaly_seq:
                return {"matched_event": {
                    "frame_seq": seq,
                    "type": a.get("type"), "severity": a.get("severity"),
                    "description": a.get("description"),
                    "confidence": a.get("confidence"),
                    "baseline_frame_seq": base_anomaly_seq,
                    "anomaly_at_baseline": base_anomaly}}
            return None
        # default: log condition — only events since we started
        events = _log_sources.read_normalized(
            prof, window=(start_ms, float("inf")), limit=500, level=level)
        for ev in events:
            if category and ev.get("category") != category:
                continue
            if source and source not in str(ev.get("source") or ""):
                continue
            d = ev.get("data") or {}
            msg = str(d.get("message") or d.get("name") or "")
            hay = msg + " " + str(ev.get("raw") or "")
            if pat is not None and not pat.search(hay):
                continue
            return {"matched_event": {
                "ts_ms": ev.get("ts_ms"), "ts": ev.get("ts"),
                "level": ev.get("level"), "category": ev.get("category"),
                "source": ev.get("source") or ev.get("log_label"),
                "message": msg[:200]}}
        return None

    result = None
    while True:
        try:
            result = _check()
        except Exception:
            result = None
        if result is not None:
            break
        if time.monotonic() - start >= timeout:
            break
        remaining = timeout - (time.monotonic() - start)
        time.sleep(min(poll, max(0.05, remaining)))

    waited_ms = round((time.monotonic() - start) * 1000.0, 1)
    out = {"matched": result is not None, "condition": condition,
           "condition_inferred": condition_inferred,
           "condition_basis": ("defaulted to 'log' — no `condition` was given"
                               if condition_inferred else "supplied by the caller"),
           "waited_ms": waited_ms, "timeout_s": timeout}
    if result:
        out.update(result)
    else:
        out["hint"] = ("No match within the timeout. Widen the condition, raise "
                       "timeout (cap 120s), or confirm capture/logging is live.")
    return jsonify(out), 200


@app.route("/diff", methods=["GET"])
def diff_frames():
    """What changed between two frames a and b (a<b): net state_delta aggregated
    across the intervening frames, new/resolved errors (by fingerprint), anomaly
    change, perf delta (cpu/rss/threads), and frame-metadata delta. Compact.
    Query: a=<seq>, b=<seq>."""
    a_seq = _int_arg("a", 0)
    b_seq = _int_arg("b", 0)
    with _lock:
        fa = dict(_frames[a_seq]) if a_seq in _frames else None
        fb = dict(_frames[b_seq]) if b_seq in _frames else None
        between = sorted((dict(f) for f in _frames.values()
                          if a_seq < (f.get("sequence") or 0) <= b_seq),
                         key=lambda f: f.get("sequence") or 0)
    if fa is None or fb is None:
        return jsonify({"error": "frame not found",
                        "a_found": fa is not None, "b_found": fb is not None}), 404

    # Net state change across (a, b]: merge each step's changed/added/removed.
    net_changed: dict[str, dict] = {}
    net_added, net_removed = {}, {}
    for f in between:
        sd = f.get("state_delta") or {}
        for k, v in (sd.get("changed") or {}).items():
            if k in net_changed:
                net_changed[k]["to"] = v.get("to")
            else:
                net_changed[k] = {"from": v.get("from"), "to": v.get("to")}
        for k, v in (sd.get("added") or {}).items():
            net_added[k] = v
        for k, v in (sd.get("removed") or {}).items():
            net_removed[k] = v

    def _err_id(fr):
        ee = _frame_error(fr) or {}
        return ee.get("fingerprint") or (ee.get("exception_type"),
                                         (ee.get("message") or "")[:80])
    ea, eb = _frame_error(fa), _frame_error(fb)
    id_a, id_b = (_err_id(fa) if ea else None), (_err_id(fb) if eb else None)
    new_error = None
    resolved_error = None
    if eb and id_a != id_b:
        new_error = {"exception_type": eb.get("exception_type"),
                     "message": (eb.get("message") or "")[:140],
                     "fingerprint": eb.get("fingerprint")}
    if ea and id_a != id_b:
        resolved_error = {"exception_type": ea.get("exception_type"),
                          "message": (ea.get("message") or "")[:140],
                          "fingerprint": ea.get("fingerprint")}

    pa = fa.get("perf") or {}
    pb = fb.get("perf") or {}
    def _pd(k):
        va, vb = pa.get(k), pb.get(k)
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            return {"from": va, "to": vb, "delta": round(vb - va, 3)}
        return {"from": va, "to": vb, "delta": None}
    perf_delta = {k: _pd(k) for k in ("cpu_percent", "rss_mb", "num_threads")}

    aa = fa.get("anomaly") or {}
    ab = fb.get("anomaly") or {}
    anomaly_change = None
    if bool(aa.get("detected")) != bool(ab.get("detected")) or \
       aa.get("type") != ab.get("type"):
        anomaly_change = {"from": {"detected": aa.get("detected"), "type": aa.get("type")},
                          "to": {"detected": ab.get("detected"), "type": ab.get("type")}}

    def _meta(fr):
        cm = fr.get("capture_meta") or {}
        return {"running": (fr.get("program") or {}).get("running"),
                "tags": fr.get("tags") or [],
                "summary": fr.get("summary"),
                "black_frame": cm.get("black_frame"),
                "capture_target": cm.get("capture_target")}

    return jsonify({
        "a": a_seq, "b": b_seq, "frames_between": len(between),
        "state_delta": {
            "changed": dict(list(net_changed.items())[:30]),
            "added": dict(list(net_added.items())[:30]),
            "removed": dict(list(net_removed.items())[:30]),
            "changed_count": len(net_changed),
            "added_count": len(net_added),
            "removed_count": len(net_removed),
            "truncated": max(len(net_changed), len(net_added), len(net_removed)) > 30,
        },
        "new_error": new_error,
        "resolved_error": resolved_error,
        "anomaly_change": anomaly_change,
        "perf_delta": perf_delta,
        "meta_delta": {"a": _meta(fa), "b": _meta(fb)},
    }), 200


@app.route("/state_diff", methods=["GET"])
def state_diff():
    """State-field diff between two MOMENTS in time using the nearest 'wide' state
    records (category='wide' / data.name='state.wide'). Query: a_ms, b_ms (epoch
    ms). Returns diagnostics.state_delta between the two — added/removed/changed
    leaf keys — bounded/token-aware."""
    from modules.diagnostics import state_delta as _sd
    a_ms = _float_arg("a_ms", 0.0)
    b_ms = _float_arg("b_ms", time.time() * 1000.0)
    recs = _read_action_jsonl(limit=20000)
    wide = [r for r in recs
            if (r.get("category") or "").lower() == "wide"
            or ((r.get("data") or {}).get("name") or "").lower() == "state.wide"]
    if not wide:
        return jsonify({"a_ms": a_ms, "b_ms": b_ms, "match": None,
                        "hint": "no records with category='wide' or data.name='state.wide'"}), 200

    def _nearest(t):
        return min(wide, key=lambda r: abs((r.get("ts_ms")
                   or _iso_to_ms(r.get("ts", "")) or 0) - t))
    ra, rb = _nearest(a_ms), _nearest(b_ms)
    da = ra.get("data") if isinstance(ra.get("data"), dict) else {}
    db = rb.get("data") if isinstance(rb.get("data"), dict) else {}
    delta = _sd(da, db)
    return jsonify({
        "a_ms": a_ms, "b_ms": b_ms,
        "a_match_ts_ms": ra.get("ts_ms"), "b_match_ts_ms": rb.get("ts_ms"),
        "state_delta": delta,
    }), 200


@app.route("/metrics", methods=["GET"])
def metrics():
    """Perf/resource trend + error/capture rate over a window of recent frames.
    Query: window = number of most recent frames to summarize (default 50, cap
    2000). Returns cpu/rss/threads latest/min/max/avg, error-rate, capture rate,
    and blank-frame count. Small JSON."""
    window = max(1, min(_MAX_METRIC_FRAMES, _int_arg("window", 50)))
    frames = _frames_sorted(limit=window)
    e = _auto_engine

    def _series(key):
        vals = []
        for f in frames:
            v = (f.get("perf") or {}).get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                vals.append(float(v))
        if not vals:
            return {"latest": None, "min": None, "max": None, "avg": None, "n": 0}
        return {"latest": round(vals[-1], 3), "min": round(min(vals), 3),
                "max": round(max(vals), 3),
                "avg": round(sum(vals) / len(vals), 3), "n": len(vals)}

    frames_with_error = sum(1 for f in frames if _frame_error(f))
    blank_in_window = sum(1 for f in frames
                          if (f.get("capture_meta") or {}).get("black_frame"))
    n = len(frames)
    return jsonify({
        "window_frames": n,
        "perf": {"cpu_percent": _series("cpu_percent"),
                 "rss_mb": _series("rss_mb"),
                 "num_threads": _series("num_threads")},
        "error_rate": {"frames_with_error": frames_with_error,
                       "frames_scanned": n,
                       "rate": round(frames_with_error / n, 3) if n else 0.0},
        "capture": {"capturing": e.capturing,
                    "shots_per_second": _fps(e.interval),
                    "interval_s": round(e.interval, 3),
                    "blank_frame_count_session": e.blank_frame_count,
                    "blank_frames_in_window": blank_in_window},
    }), 200


def _tool_catalog_groups() -> dict:
    """The MCP tools, grouped by what they are FOR.

    Single source of truth: /capabilities and /bridge/catalog both need it,
    and two copies would silently drift apart — the catalog would then show
    the agent a tool list that no longer matches what exists.

    EVERY registered tool must appear in exactly one group. This is enforced by
    test_tool_groups_cover_every_tool: an ungrouped tool is invisible to the
    first-bridge review, so the agent cannot choose it and cannot even know it
    exists — which quietly defeats the point of the review gate. (31 tools were
    ungrouped before this was checked.)
    """
    return {
        "start": ["av_start_here"],
        "cheap_visual_path": ["av_visual_changes", "av_frame_json",
                              "av_frame_region", "av_visual_events",
                              "av_error_moment", "av_token_report"],
        "flight_recorder": ["av_incidents", "av_replay"],
        "ui_tree": ["av_ui_tree", "av_ui_diff"],
        "orient": ["av_digest", "av_overview", "av_status", "av_capabilities"],
        "diagnose": ["av_diagnose", "av_diff", "av_state_diff", "av_metrics",
                     "av_errors_by_fingerprint", "av_new_errors_this_session",
                     "av_bookmark_outliers", "av_source_at_error"],
        "investigate": ["av_timeline", "av_search", "av_wait_for",
                        "av_actions_around_frame", "av_trace_timeline",
                        "av_log_normalized", "av_state_at",
                        "av_baseline", "av_watch", "av_watches"],
        "frames": ["av_latest_frame", "av_get_frame", "av_frame_alignment",
                   "av_frame_overlay", "av_frame_annotate",
                   "av_ocr_frame", "av_read_screen"],
        "logs": ["av_log_sources", "av_log_range", "av_program_log",
                 "av_test_adapter", "av_list_adapters", "av_add_adapter",
                 "av_preflight"],
        "capture": ["av_capture_start", "av_capture_stop",
                    "av_capture_set_interval", "av_capture_status"],
        "source": ["av_source_light", "av_source_tree", "av_source_digest",
                   "av_source_file", "av_source_search", "av_source_list",
                   "av_source_refresh", "av_codebase_map"],
        "raw_logs": ["av_log_raw", "av_log_where", "av_log_entities",
                     "av_log_push", "av_observer_log", "av_debug_log",
                     "av_events_schema"],
        "bridge_setup": ["av_bridge_status", "av_bridge_catalog",
                         "av_bridge_commit", "av_install_project",
                         "av_install_verify"],
        "retention": ["av_retention", "av_frames_awaiting", "av_examine_ack"],
        "profiles": ["av_list_profiles", "av_active_profile",
                     "av_set_active_profile", "av_create_profile",
                     "av_delete_profile"],
        "health": ["av_selftest", "av_daemon_status", "av_program_status",
                   "av_run_tests"],
        "program": ["av_program_stats", "av_program_crop", "av_ambient"],
        "bookmarks": ["av_list_bookmarks", "av_get_bookmark",
                      "av_frame_annotations"],
        "wrap_up": ["av_session_report"],
    }


def _program_capabilities(profile) -> tuple[set, bool, bool]:
    """Which `needs` tokens THIS program can actually satisfy.

    Derived from the profile rather than assumed, so the relevance verdicts in the
    catalog are about the real program and not about a generic one. Errs toward
    granting a capability: a tool wrongly marked available costs one wasted call,
    while one wrongly withheld hides a diagnostic the agent needed.
    """
    caps = {"none", "log_source_any", "log_source_events", "log_source_text",
            "adapter_with_level", "source_index"}
    gui = bool(getattr(profile, "capture_app", "") or
               getattr(profile, "window_title", ""))
    visual = gui
    if visual:
        caps |= {"frames_on_disk", "capture_running", "window_visible",
                 "gui_program", "accessibility_api"}
    try:
        if (_ocr_backend() or {}).get("available"):
            caps.add("ocr_backend")
    except Exception:
        pass
    return caps, visual, gui


def _describe_tool_groups(groups: dict, profile, full: bool = False) -> dict:
    """Attach per-tool metadata + a relevance verdict to each catalog group.

    Without this the first-bridge review is a list of 90 names: the agent can see
    THAT a tool exists but has no basis to judge whether it applies here, which
    makes "choose the relevant tools" an invitation to guess. The `needs`/verdict
    pair is what turns it into a decision with evidence behind it.
    """
    try:
        from api import tool_meta as _tm
    except Exception:
        try:
            import tool_meta as _tm  # flat-import fallback
        except Exception:
            return {k: {"tools": v} for k, v in groups.items()}

    caps, visual, gui = _program_capabilities(profile)
    lang = (getattr(profile, "language", "") or "").lower()
    out: dict = {}
    caveats_hidden = 0
    for gname, names in (groups or {}).items():
        entries = []
        for n in names:
            e = _tm.compact(n)
            r = _tm.relevance(n, language=lang, visual=visual, gui=gui,
                              capabilities=caps)
            e["verdict"] = r.get("verdict")
            # A `core` verdict needs no explanation; only a downgrade does.
            if r.get("verdict") != "core":
                e["verdict_reason"] = r.get("reason")
            note = (_tm.for_tool(n).get("code_note") or "").strip()
            if note:
                if full:
                    e["caveat"] = note
                else:
                    e["has_caveat"] = True
                    caveats_hidden += 1
            if not full:
                # Trim the summary. The decision the agent is making is "is this
                # tool relevant here", and `needs` + a one-line gist answer it;
                # the full prose does not.
                s = e.get("summary") or ""
                if len(s) > 96:
                    e["summary"] = s[:95].rstrip() + "…"
            entries.append(e)
        out[gname] = {
            "tools": entries,
            "relevant_here": sum(1 for e in entries if e.get("verdict") == "core"),
            "ruled_out": sum(1 for e in entries if e.get("verdict") == "n/a"),
        }
    if not full and caveats_hidden:
        # Say what was withheld and how to get it. Silently trimming would make
        # the catalog look complete when it is not — the same confident-silence
        # problem this project keeps fixing.
        out["_detail"] = {
            "mode": "compact",
            "caveats_hidden": caveats_hidden,
            "why": ("full per-tool metadata is ~51 KB of a ~59 KB catalog (87%), "
                    "which every first bridge would otherwise pay. Measured on a "
                    "real run: ~14.7k tokens, and a cold model reported having to "
                    "read all of it."),
            "get_the_rest": ("GET /bridge/catalog?detail=full — or read the "
                             "per-tool caveats in docs/MCP_TOOL_AUDIT.md, which "
                             "is where they are recorded permanently."),
            "note": ("`needs`, `cost` and `verdict` are NOT trimmed — those are "
                     "what the relevance decision actually rests on."),
        }
    return out


@app.route("/capabilities", methods=["GET"])
def capabilities():
    """What AgentVision can do RIGHT NOW: platform, capture backend, adapter +
    reader counts, active profile, capture/daemon status, and the catalog of
    analysis tools. The AI's 'what can I do here'. Token-bounded (counts + a
    curated tool list, never the full adapter dump)."""
    from utils import platform_shim
    collector = _get_collector()
    e = _auto_engine
    try:
        backend = platform_shim.capture_backend_name()
    except Exception:
        backend = "unknown"
    adapters = _log_adapters.list_adapters()
    tool_catalog = _tool_catalog_groups()
    return jsonify({
        "platform": {"os": sys.platform,
                     "is_windows": getattr(platform_shim, "IS_WINDOWS", None),
                     "is_linux": getattr(platform_shim, "IS_LINUX", None),
                     "is_macos": getattr(platform_shim, "IS_MACOS",
                                         sys.platform == "darwin")},
        "capture_backend": backend,
        "active_profile": _active_profile_name,
        "program": collector.profile.display_name,
        "language": getattr(collector.profile, "language", "") or None,
        "counts": {"log_adapters": len(adapters),
                   "source_readers": len(_log_sources.list_readers()),
                   "frames_in_memory": len(_frames)},
        "source_readers": _log_sources.list_readers(),
        "capture": {"engine_running": e.running, "capturing": e.capturing,
                    "shots_per_second": _fps(e.interval)},
        "daemon": _http_daemon_status_dict(),
        "flagship_tool": "av_diagnose",
        "read_this_first": "av_start_here",
        "token_rule": ("AgentVision does the expensive observing for free on "
                       "local CPU. Prefer av_visual_changes / av_frame_json "
                       "over raw frames; escalate to av_frame_region for "
                       "pixels and to the full image last. av_token_report "
                       "measures the difference."),
        "tool_catalog": tool_catalog,
    }), 200


@app.route("/adapter/test", methods=["GET"])
def adapter_test():
    """Route ONE log line through the adapter detector — the AI's coverage probe.
    Query: line=<raw log line>. Returns {adapter, confidence, top_scores, event}
    so you can see how a format would be classified + parsed before wiring it up."""
    line = request.args.get("line", "")
    if not line:
        return jsonify({"error": "line parameter required"}), 400
    adapter, conf, scores = _log_adapters.detect_adapter([line])
    top = sorted(scores.items(), key=lambda kv: -kv[1])[:8]
    ev = None
    try:
        ev = adapter.parse_line(line)
    except Exception:
        ev = None
    is_fallback = adapter.name in _log_sources.FALLBACK_ADAPTERS
    return jsonify({
        "line": line[:300],
        "adapter": adapter.name,
        "language": getattr(adapter, "language", None),
        "confidence": round(conf, 3),
        "is_fallback": is_fallback,
        "top_scores": [{"adapter": n, "score": s} for n, s in top],
        "event": ev,
        "note": ("is_fallback=true means no adapter specifically parses this "
                 "format — consider av_add_adapter."),
    }), 200


@app.route("/adapters", methods=["GET"])
def adapters_list():
    """Paginated/filterable list of the log adapters (hundreds of them) — NEVER
    the full raw dump. Query: family (matches the adapter's family module OR its
    language), q (substring on name), limit (default 50, cap 200), offset.
    Returns {total, matched, families, adapters:[{name, language, family}]}."""
    family = (request.args.get("family") or "").strip().lower()
    q = (request.args.get("q") or "").strip().lower()
    limit = max(1, min(200, _int_arg("limit", 50)))
    offset = max(0, _int_arg("offset", 0))

    rows = []
    fam_counts: dict[str, int] = {}
    for a in _log_adapters.REGISTRY:
        fam = type(a).__module__.split(".")[-1]
        fam_counts[fam] = fam_counts.get(fam, 0) + 1
        rows.append({"name": a.name, "language": a.language, "family": fam})

    def _keep(r):
        if family and family not in (r["family"].lower(), (r["language"] or "").lower()):
            return False
        if q and q not in r["name"].lower():
            return False
        return True

    matched = [r for r in rows if _keep(r)]
    page = matched[offset:offset + limit]
    return jsonify({
        "total": len(rows),
        "matched": len(matched),
        "offset": offset, "limit": limit,
        "returned": len(page),
        "families": dict(sorted(fam_counts.items(), key=lambda kv: -kv[1])),
        "adapters": page,
    }), 200


# ── Investigation wrap-up / OCR / error→source / standing watches (v5 batch B) ─
# The second high-value MCP batch. Every route here COMPOSES the existing
# internals (digest/diagnose/timeline/source-mirror/normalized-stream) rather
# than re-deriving anything, stays token-bounded, is thread-safe (_lock /
# _watch_lock), and is pure stdlib EXCEPT the OPTIONAL, lazily-imported
# pytesseract used by the OCR routes (which degrade gracefully when absent).

def _compose_view(view_fn, query: dict | None = None) -> dict:
    """Invoke another route's VIEW FUNCTION inside a fresh request context and
    return its parsed JSON. This is how av_session_report reuses the exact
    digest/diagnose/timeline correlation already implemented here — no logic is
    duplicated or recomputed. Never raises; returns {} on any failure."""
    qs = urllib.parse.urlencode({k: v for k, v in (query or {}).items()
                                 if v is not None})
    path = "/_compose" + (("?" + qs) if qs else "")
    try:
        with app.test_request_context(path):
            rv = view_fn()
            resp = rv[0] if isinstance(rv, tuple) else rv
            return resp.get_json() or {}
    except Exception as e:
        _warn(f"_compose_view({getattr(view_fn, '__name__', '?')}) failed: {e}")
        return {}


def _frames_of_interest(window: tuple | None, cap: int = 10) -> list[dict]:
    """Frames worth a human/AI look in the window: any carrying a structured
    error, a detected anomaly, a 'stuck' tag, or a blank grab. Bounded."""
    out: list[dict] = []
    for f in _frames_sorted():
        ms = float(f.get("timestamp_ms") or 0)
        if window is not None and ms and not (window[0] <= ms <= window[1]):
            continue
        reasons: list[str] = []
        fe = _frame_error(f)
        if fe:
            reasons.append("error:" + (fe.get("exception_type") or "error"))
        anom = f.get("anomaly") or {}
        if anom.get("detected"):
            reasons.append("anomaly:" + str(anom.get("type")))
        if "stuck" in (f.get("tags") or []):
            reasons.append("stuck")
        if (f.get("capture_meta") or {}).get("black_frame"):
            reasons.append("blank_frame")
        if reasons:
            out.append({"seq": f.get("sequence"), "ts_ms": ms or None,
                        "summary": (f.get("summary") or "")[:140],
                        "why": reasons})
    return out[-cap:]


def _session_report_markdown(d: dict) -> str:
    """Render the composed session-report dict into a readable, bounded (~4-8KB)
    Markdown string the user/AI can save. Pure formatting — no recomputation."""
    L: list[str] = []
    L.append(f"# AgentVision session report — {d.get('program', '?')}")
    win = d.get("window") or {}
    L.append(f"_Generated {d.get('generated_iso', '')} · profile "
             f"`{d.get('active_profile', '?')}` · window: {win.get('mode', 'session')}_")
    h = d.get("health") or {}
    L.append(f"\n## Health: {h.get('grade', '?')} ({h.get('score', '?')}/100)")
    for fct in (h.get("factors") or [])[:8]:
        L.append(f"- {fct}")
    sig = d.get("top_signals") or []
    if sig:
        L.append("\n## Top signals")
        for s in sig[:8]:
            L.append(f"- {s}")
    hyps = d.get("hypotheses") or []
    if hyps:
        L.append("\n## Ranked hypotheses")
        for i, hy in enumerate(hyps[:6], 1):
            L.append(f"{i}. **{(hy.get('summary') or '')[:180]}** "
                     f"(confidence {hy.get('confidence')})")
            if hy.get("probable_cause"):
                L.append(f"   - cause: {hy['probable_cause']}")
            rn = hy.get("recommended_next") or []
            if rn:
                L.append(f"   - next: {rn[0]}")
    te = d.get("top_errors") or []
    if te:
        L.append("\n## Top errors (deduped by fingerprint)")
        for e in te[:6]:
            L.append(f"- {e.get('count')}× `{e.get('fingerprint')}` "
                     f"{(e.get('sample') or '')[:100]}")
    nf = d.get("new_error_fingerprints") or []
    if nf:
        L.append("\n## New this session")
        L.append("- " + ", ".join(f"`{x}`" for x in nf[:10]))
    km = d.get("key_moments") or []
    if km:
        L.append("\n## Key moments")
        for r in km[:25]:
            L.append(f"- [{r.get('kind')}] {(r.get('line') or '')[:120]}")
    foi = d.get("frames_of_interest") or []
    if foi:
        L.append("\n## Frames to look at")
        for fr in foi[:10]:
            L.append(f"- #{fr.get('seq')} — {(fr.get('summary') or '')[:90]} "
                     f"({', '.join(fr.get('why') or [])})")
    cap = d.get("capture") or {}
    L.append("\n## Capture / coverage")
    L.append(f"- frames stored: {cap.get('frames_stored')} · "
             f"{cap.get('shots_per_second')} shots/sec · "
             f"blank: {cap.get('blank_frame_count')} · "
             f"window_missing: {cap.get('window_missing')}")
    al = d.get("alignment") or {}
    L.append(f"- image↔log alignment: aligned={al.get('aligned')} "
             f"(leaked_after_shutter={al.get('leaked_after_shutter')})")
    L.append(f"- log sources: {cap.get('log_source_count')}")
    md = "\n".join(L)
    if len(md) > 8000:
        md = md[:8000] + "\n\n_(report truncated at 8 KB)_"
    return md


@app.route("/session_report", methods=["GET"])
def session_report():
    """WRAP UP / HAND OFF an investigation. Assembles ONE shareable diagnostic
    report for a window by COMPOSING the existing internals (no recompute):
    digest health, diagnose hypotheses + top_signals, a trimmed timeline of the
    key moments (errors/warns/anomalies/failure-bookmarks), top errors deduped by
    fingerprint, new-this-session fingerprints, the frames worth looking at
    (seqs), and a capture/alignment coverage summary. Returns compact JSON AND a
    `markdown` field (a readable, ~4-8KB report string) the user/AI can save.
    Query: from_ms, to_ms (optional; scope the timeline + frames-of-interest)."""
    from_ms, to_ms, window = _window_from_args()
    collector = _get_collector()

    # Compose the flagship correlations — reuse the exact route logic.
    dig = _compose_view(digest)
    diag = _compose_view(diagnose)
    tl = _compose_view(timeline, {"from_ms": from_ms, "to_ms": to_ms, "limit": 400})

    # Trim the timeline to KEY moments only (failures/warns/anomalies/frames-of-note).
    key_moments: list[dict] = []
    for r in (tl.get("rows") or []):
        kind = r.get("kind")
        keep = False
        if kind == "bookmark":
            keep = True
        elif kind == "log":
            if str(r.get("level") or "").upper() in ("WARN", "ERROR", "FATAL"):
                keep = True
        elif kind == "frame":
            line = (r.get("line") or "").lower()
            if "error" in line or "anomaly" in line or "stuck" in line:
                keep = True
        if keep:
            key_moments.append({"ts_ms": r.get("ts_ms"), "kind": kind,
                                "source": r.get("source"), "level": r.get("level"),
                                "seq": r.get("seq"), "line": (r.get("line") or "")[:160]})
    key_moments = key_moments[-40:]

    e = _auto_engine
    with _lock:
        latest = _latest_for_active()
    frames_stored, frames_all_profiles = _active_frame_count()
    try:
        log_source_count = len(_log_sources.effective_sources(_active_profile_obj()))
    except Exception:
        log_source_count = None

    capture = {
        "frames_stored":     frames_stored,
        "frames_stored_all_profiles": frames_all_profiles,
        "capturing":         e.capturing,
        "shots_per_second":  _fps(e.interval),
        "interval_s":        round(e.interval, 3),
        "blank_frame_count": e.blank_frame_count,
        "window_missing":    e.window_missing,
        "log_source_count":  log_source_count,
    }

    report = {
        "generated_ms":  time.time() * 1000.0,
        "generated_iso": clock.now_iso(),
        "program":       collector.profile.display_name,
        "active_profile": _active_profile_name,
        "window": {"from_ms": from_ms, "to_ms": to_ms,
                   "mode": "explicit" if window is not None else "whole session"},
        "health":        dig.get("health") or diag.get("health"),
        "top_signals":   diag.get("top_signals") or [],
        "hypotheses":    (diag.get("hypotheses") or [])[:6],
        "top_errors":    (dig.get("top_errors") or [])[:6],
        "new_error_fingerprints": (dig.get("new_error_fingerprints") or [])[:10],
        "key_moments":   key_moments,
        "frames_of_interest": _frames_of_interest(window),
        "latest_frame_seq": (latest or {}).get("sequence"),
        "capture":       capture,
        "alignment":     _alignment_health(
            latest, freshness=_source_freshness(
                _active_profile_obj(),
                ref_ms=float((latest or {}).get("timestamp_ms") or 0) or None)),
        "counts": {"key_moments": len(key_moments),
                   "timeline_total": tl.get("total_available"),
                   "distinct_error_fingerprints":
                       (diag.get("counts") or {}).get("distinct_error_fingerprints")},
        "guidance": ("A shareable wrap-up composed from digest + diagnose + timeline. "
                     "Save the `markdown` field for the hand-off; drill back in with the "
                     "frames_of_interest seqs and top_errors fingerprints."),
    }
    report["markdown"] = _session_report_markdown(report)
    return jsonify(report), 200


# ── OCR (OPTIONAL dep — degrades gracefully) ──────────────────────────────────

_OCR_INSTALL_HINT = ("pip install pytesseract  +  the tesseract binary "
                     "(brew install tesseract  /  apt install tesseract-ocr)")


def _ocr_backend() -> dict:
    """The full multi-backend OCR detection result (see utils.ocr_backends).
    Prefers a ZERO-INSTALL engine that ships with the OS — Apple Vision on
    macOS, Windows.Media.Ocr on Windows — so the user never has to install a
    native tesseract binary by hand. Never raises."""
    try:
        from utils import ocr_backends
    except Exception:
        try:
            from python_backend.utils import ocr_backends  # type: ignore
        except Exception as ex:
            return {"available": False, "backend": "", "engine": "",
                    "reason": f"ocr_backends unavailable ({ex})",
                    "install_hint": _OCR_INSTALL_HINT, "candidates": []}
    try:
        return ocr_backends.detect_engine()
    except Exception as ex:                       # pragma: no cover - defensive
        return {"available": False, "backend": "", "engine": "",
                "reason": f"OCR detection failed ({ex})",
                "install_hint": _OCR_INSTALL_HINT, "candidates": []}


def _ocr_engine():
    """Back-compat 3-tuple wrapper: (available, engine_str, reason).

    Detection now spans several backends instead of only pytesseract, so OCR is
    available out of the box on macOS (Apple Vision) and Windows
    (Windows.Media.Ocr) with nothing installed system-wide. The BUILT-IN OS
    engine is preferred: measured on an M2, Apple Vision was 3.7x faster than
    tesseract and read stylised UI text that tesseract got wrong. tesseract
    remains the cross-platform fallback."""
    d = _ocr_backend()
    return (bool(d.get("available")), d.get("engine", ""), d.get("reason", ""))


def _ocr_image(image_path: str, text_cap: int = 8000, line_cap: int = 300) -> dict:
    """OCR one image into bounded JSON. Optional-dependency safe: if pytesseract /
    tesseract is unavailable, returns {available:false, reason, install_hint}
    without raising. When available, returns {available:true, engine, text,
    lines:[{text,bbox,conf}], word_count}."""
    det = _ocr_backend()
    available, engine = bool(det.get("available")), det.get("engine", "")
    if not available:
        return {"available": False, "reason": det.get("reason", ""),
                "install_hint": det.get("install_hint") or _OCR_INSTALL_HINT,
                "candidates": det.get("candidates", [])}
    if not image_path or not os.path.exists(image_path):
        return {"available": True, "engine": engine, "error": "frame image not found",
                "image_path": image_path, "text": "", "lines": [], "word_count": 0}

    # Non-tesseract backends (Apple Vision / Windows OCR / RapidOCR) already
    # return line-level results, so hand off to the shared implementation. The
    # tesseract path below stays as-is because it groups WORDS into coherent
    # lines itself, which reads better than raw word boxes.
    if det.get("backend") != "tesseract":
        try:
            from utils import ocr_backends
        except Exception:
            from python_backend.utils import ocr_backends  # type: ignore
        out = ocr_backends.ocr_image(image_path, text_cap=text_cap,
                                     line_cap=line_cap)
        out.setdefault("word_count", len((out.get("text") or "").split()))
        return out
    try:
        import pytesseract
        from PIL import Image
        with Image.open(image_path) as im:
            data = pytesseract.image_to_data(im, output_type=pytesseract.Output.DICT)
    except Exception as ex:
        return {"available": True, "engine": engine,
                "error": f"OCR failed: {ex}", "text": "", "lines": [], "word_count": 0}

    # Group words into lines by (block, par, line) so the AI reads coherent lines.
    lines_map: dict[tuple, dict] = {}
    n = len(data.get("text", []))
    word_count = 0
    for i in range(n):
        word = (data["text"][i] or "").strip()
        if not word:
            continue
        word_count += 1
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1.0
        key = (data.get("block_num", [0])[i], data.get("par_num", [0])[i],
               data.get("line_num", [0])[i])
        x, y = int(data["left"][i]), int(data["top"][i])
        w, hgt = int(data["width"][i]), int(data["height"][i])
        entry = lines_map.get(key)
        if entry is None:
            lines_map[key] = {"words": [word], "confs": [conf] if conf >= 0 else [],
                              "x0": x, "y0": y, "x1": x + w, "y1": y + hgt}
        else:
            entry["words"].append(word)
            if conf >= 0:
                entry["confs"].append(conf)
            entry["x0"] = min(entry["x0"], x); entry["y0"] = min(entry["y0"], y)
            entry["x1"] = max(entry["x1"], x + w); entry["y1"] = max(entry["y1"], y + hgt)

    lines_out: list[dict] = []
    for entry in lines_map.values():
        txt = " ".join(entry["words"]).strip()
        if not txt:
            continue
        confs = entry["confs"]
        lines_out.append({
            "text": txt[:240],
            "bbox": [entry["x0"], entry["y0"], entry["x1"], entry["y1"]],
            "conf": round(sum(confs) / len(confs), 1) if confs else None,
        })
        if len(lines_out) >= line_cap:
            break
    full_text = "\n".join(ln["text"] for ln in lines_out)
    truncated = len(full_text) > text_cap
    return {
        "available":  True,
        "engine":     engine,
        # Same keys the other backends return, so a caller never has to guess
        # which engine produced the text.
        "backend":    det.get("backend") or "tesseract",
        "zero_install": False,          # tesseract is the one needing a binary
        "text":       full_text[:text_cap],
        "text_truncated": truncated,
        "lines":      lines_out,
        "line_count": len(lines_out),
        "word_count": word_count,
    }


@app.route("/frame/<int:seq>/ocr", methods=["GET"])
def frame_ocr(seq: int):
    """OCR the on-screen text of captured frame N into JSON — read UI text, error
    dialogs, and stack traces WITHOUT spending vision tokens. OCR is an OPTIONAL
    capability: if tesseract/pytesseract is missing this returns
    {available:false, reason, install_hint} gracefully."""
    with _lock:
        frame = _frame_or_load(seq)
    if not frame:
        return _no_such_frame(seq)
    _mark_seen(seq, "ocr")
    img = frame.get("annotated_image") or ""
    out = _ocr_image(img)
    out["frame_seq"] = seq
    return jsonify(out), 200


@app.route("/read_screen", methods=["GET"])
def read_screen():
    """OCR the LATEST captured frame's on-screen text into JSON (see /frame/<seq>/
    ocr). The cheap way to read what is currently on screen instead of describing
    the image. Degrades gracefully when tesseract/pytesseract is absent."""
    with _lock:
        latest = _latest_for_active()
    if latest is None:
        return jsonify({"error": "No frames yet",
                        "hint": "capture may be stopped or the program isn't running"}), 404
    img = latest.get("annotated_image") or ""
    out = _ocr_image(img)
    out["frame_seq"] = latest.get("sequence")
    return jsonify(out), 200


# ── Error → source (tie an error straight to the code) ────────────────────────

def _resolve_source_path(file_str: str, root: Path) -> Path | None:
    """Map a traceback frame's file to an on-disk file: prefer a project_root
    -relative path, then an absolute path that exists, then a basename match in
    the mirror index. Returns None if nothing resolves."""
    if not file_str:
        return None
    fs = file_str.strip()
    # 1. project_root-relative
    try:
        cand = (root / fs).resolve()
        if cand.exists() and cand.is_file():
            return cand
    except Exception:
        pass
    # 2. absolute path that exists (the target's own source tree)
    try:
        p = Path(fs)
        if p.is_absolute() and p.exists() and p.is_file():
            return p
    except Exception:
        pass
    # 3. basename match against the mirror index (best-effort)
    try:
        pdir = _profile_dir()
        idx_path = pdir / "source_index.json"
        if idx_path.exists():
            base = Path(fs).name
            idx = json.loads(idx_path.read_text())
            for f in idx.get("files", []):
                if Path(f["path"]).name == base:
                    cand = (root / f["path"]).resolve()
                    if cand.exists():
                        return cand
    except Exception:
        pass
    return None


def _code_context(file_str: str, line: int, root: Path, ctx: int = 4) -> dict:
    """A few lines of source around (file_str:line). Bounded, degrades cleanly if
    the file isn't resolvable in the mirror / on disk."""
    resolved = _resolve_source_path(file_str, root)
    if resolved is None:
        return {"found": False, "resolved_path": None, "code_context": []}
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"found": False, "resolved_path": str(resolved),
                "code_context": [], "error": str(e)}
    lines = text.splitlines()
    if line and line > 0:
        lo = max(0, line - 1 - ctx)
        hi = min(len(lines), line + ctx)
    else:
        lo, hi = 0, min(len(lines), ctx * 2 + 1)
    context = []
    for i in range(lo, hi):
        marker = ">>" if (line and (i + 1) == line) else "  "
        context.append(f"{marker}{i + 1:>5} | {lines[i][:200]}")
    return {"found": True,
            "resolved_path": str(resolved),
            "from_line": lo + 1, "to_line": hi,
            "code_context": context}


def _find_error_by_fingerprint(fp: str) -> tuple[dict | None, int | None]:
    """Find a structured error block (and its frame seq) for a fingerprint by
    scanning the in-memory frames. Returns (error_dict, seq) or (None, None)."""
    for f in _frames_sorted():
        fe = _frame_error(f)
        if fe and fe.get("fingerprint") == fp:
            return fe, f.get("sequence")
    return None, None


@app.route("/source_at_error", methods=["GET"])
def source_at_error():
    """Tie an error straight to the code. Takes a structured error's frames
    [{file,line,func}] and pulls the mirrored source AROUND each frame (a few
    lines of context each). Query: fingerprint=<fp> (a specific error) or none
    (the latest frame's error). Bounded to the top N frames, few lines each;
    degrades cleanly when a file isn't in the mirror."""
    fp = (request.args.get("fingerprint") or "").strip()
    top_n = max(1, min(10, _int_arg("frames", 5)))
    ctx = max(1, min(12, _int_arg("context", 4)))

    err: dict | None = None
    seq: int | None = None
    if fp:
        err, seq = _find_error_by_fingerprint(fp)
        if err is None:
            return jsonify({"fingerprint": fp, "error": None,
                            "hint": "no in-memory frame carries that fingerprint; "
                                    "try av_errors_by_fingerprint(fp) or av_diagnose",
                            "frames": []}), 200
    else:
        with _lock:
            latest = _latest_for_active()
        err = _frame_error(latest or {})
        seq = (latest or {}).get("sequence")
        if err is None:
            return jsonify({"error": None,
                            "hint": "latest frame carries no structured error; "
                                    "pass fingerprint= or call av_diagnose",
                            "frames": []}), 200

    coll = _get_collector()
    root = Path(coll.profile.project_root or "").expanduser()
    frames_in = (err.get("frames") or [])[:top_n]
    out_frames = []
    for fr in frames_in:
        if not isinstance(fr, dict):
            continue
        cc = _code_context(str(fr.get("file") or ""), int(fr.get("line") or 0),
                           root, ctx=ctx)
        out_frames.append({
            "file": fr.get("file"), "line": fr.get("line"),
            "func": fr.get("func"),
            "found": cc.get("found"),
            "resolved_path": cc.get("resolved_path"),
            "code_context": cc.get("code_context"),
        })
    return jsonify({
        "fingerprint": err.get("fingerprint") or fp or None,
        "frame_seq":   seq,
        "error": {"type": err.get("exception_type"),
                  "message": (err.get("message") or "")[:200],
                  "fingerprint": err.get("fingerprint"),
                  "probable_cause": err.get("probable_cause")},
        "project_root": str(root),
        "frames": out_frames,
        "note": ("Each frame carries a few lines of source around its file:line "
                 "(>> marks the error line). found=false = file not in the mirror "
                 "or not on disk."),
    }), 200


# ── Standing watches + baseline (tripwires set before reproducing) ────────────
# STATE MODEL (in-memory, documented): watches live in `_watches` (name → spec)
# guarded by `_watch_lock`; they are EVALUATED ON QUERY against the normalized
# event stream / failure records / frames (no background thread), so a hit list
# is always fresh and bounded. Baselines (`_baselines`: profile → ts_ms) are a
# small marker persisted to disk so "what changed since I started" survives a
# bridge restart.

_watch_lock = threading.Lock()
_watches: dict[str, dict] = {}
_baselines: dict[str, float] = {}
_BASELINES_PATH = Path(os.environ.get("AGENTVISION_BASELINES",
                                      "log/agentvision_baselines.json"))
_WATCH_HIT_CAP = 25


#: Did the last baseline load see the whole file? A baseline is a MOMENT IN TIME
#: ("what changed since I started reproducing") and nothing can recompute one
#: that is lost. The loader swallowed any read/parse failure and left `_baselines`
#: as it was — usually {} — and _save_baselines then wrote that dict over the
#: file, so stamping a baseline for ONE profile erased every other profile's
#: marker. Same shape as the profiles.json defect: an empty-because-the-load-
#: failed collection authorising a write.
_baselines_load_ok = True


def _load_baselines() -> dict:
    global _baselines, _baselines_load_ok
    _baselines_load_ok = True
    if not _BASELINES_PATH.exists():
        return _baselines
    try:
        _baselines = {str(k): float(v) for k, v in
                      json.loads(_BASELINES_PATH.read_text()).items()}
    except Exception as exc:
        _baselines_load_ok = False
        _warn(f"baselines file could not be read ({_BASELINES_PATH}): {exc} — "
              f"not persisting new baselines until it is repaired or moved, so "
              f"the markers already in it are not overwritten")
    return _baselines


def _save_baselines() -> None:
    if not _baselines_load_ok:
        _warn("refusing to rewrite the baselines file: the last read of it "
              "failed, so writing now would drop the markers it still holds. "
              f"Repair or move {_BASELINES_PATH}.")
        return
    try:
        _BASELINES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _watch_lock:
            snap = dict(_baselines)
        tmp = _BASELINES_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snap))
        os.replace(tmp, _BASELINES_PATH)
    except Exception as e:
        _warn(f"could not persist baselines: {e}")


_load_baselines()


@app.route("/baseline", methods=["POST", "GET"])
def baseline_route():
    """Standing-watch anchor. POST stamps a 'since' marker (ts_ms) for the active
    profile so later queries can ask 'what changed since I started'. Call it
    BEFORE reproducing. Body (optional): {ts_ms, profile}. GET returns the current
    baseline. The marker is persisted (survives a bridge restart)."""
    prof_name = _active_profile_name
    if request.method == "GET":
        with _watch_lock:
            ms = _baselines.get(prof_name)
        return jsonify({"profile": prof_name, "baseline_ms": ms,
                        "baseline_iso": clock.ms_to_iso(ms) if ms else None}), 200
    body = request.get_json(silent=True) or {}
    prof_name = (body.get("profile") or prof_name).strip() or prof_name
    ts = body.get("ts_ms")
    try:
        ms = float(ts) if ts is not None else clock.now_ms()
    except (TypeError, ValueError):
        ms = clock.now_ms()
    with _watch_lock:
        _baselines[prof_name] = ms
    _save_baselines()
    _info(f"Baseline stamped for '{prof_name}' at {ms:.0f}ms")
    return jsonify({"ok": True, "profile": prof_name, "baseline_ms": ms,
                    "baseline_iso": clock.ms_to_iso(ms)}), 200


@app.route("/watch", methods=["POST"])
def watch_route():
    """Register a standing condition (a tripwire) under a name. Call it BEFORE
    reproducing, then check av_watches later. Body:
        {name (required),
         kind: 'log'|'error_fingerprint'|'anomaly' (inferred when omitted),
         regex/level/category/source   (for kind='log'),
         fingerprint                    (for kind='error_fingerprint'),
         anomaly_type                   (for kind='anomaly'; any type if omitted)}
    Idempotent by name (re-registering resets its created marker). Watches are
    IN-MEMORY and EVALUATED ON QUERY — see /watches."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name is required"}), 400

    # An unrecognised criterion key used to be dropped in silence, and for
    # kind='log' that is not a no-op — it is an INVERSION. A log watch with no
    # criteria matches EVERY event (see _evaluate_watch: pat is None and no
    # level/category/source filter applies), so the caller who asked for a
    # specific tripwire got an indiscriminate one and was told ok=true.
    # Measured on the same two log lines: {"regex": "flange"} -> 1 hit, the right
    # one; {"pattern": "flange"} -> 2 hits, including "totally unrelated disk
    # failure", reported as tripwire hits. `pattern` is the natural guess, and
    # av_watch's typed signature hides this from MCP callers but not from anyone
    # posting to the route.
    _known = {"name", "kind", "regex", "level", "category", "source",
              "fingerprint", "anomaly_type"}
    _aliases = {"pattern": "regex", "match": "regex", "contains": "regex",
                "text": "regex", "msg": "regex", "message": "regex",
                "severity": "level", "levels": "level", "type": "anomaly_type",
                "anomaly": "anomaly_type", "fp": "fingerprint"}
    unknown = [k for k in body if k not in _known]
    if unknown:
        return jsonify({
            "ok": False, "registered": False, "error": "UNKNOWN_WATCH_FIELD",
            "errors": [
                f"Not registered: {', '.join(repr(k) for k in sorted(unknown))} "
                f"is not a watch field. Ignoring it would have built a "
                f"kind='log' watch with NO criteria, which matches EVERY log "
                f"event — the opposite of the tripwire you asked for.",
            ],
            "did_you_mean": {k: _aliases[k] for k in unknown if k in _aliases},
            "valid_fields": sorted(_known),
        }), 400

    kind = (body.get("kind") or "").strip()
    fingerprint = (body.get("fingerprint") or "").strip()
    anomaly_type = (body.get("anomaly_type") or "").strip()
    regex = (body.get("regex") or "").strip()
    if not kind:
        kind = ("error_fingerprint" if fingerprint else
                "anomaly" if anomaly_type else "log")
    if kind == "log" and regex:
        try:
            re.compile(regex, re.IGNORECASE)
        except re.error as ex:
            return jsonify({"ok": False, "error": f"invalid regex: {ex}"}), 400
    watch = {
        "name": name, "kind": kind,
        "regex": regex or None,
        "level": (body.get("level") or "").strip() or None,
        "category": (body.get("category") or "").strip() or None,
        "source": (body.get("source") or "").strip() or None,
        "fingerprint": fingerprint or None,
        "anomaly_type": anomaly_type or None,
        "profile": _active_profile_name,
        "created_ms": clock.now_ms(),
        "created_iso": clock.now_iso(),
    }
    with _watch_lock:
        _watches[name] = watch
        count = len(_watches)
    _info(f"Watch '{name}' registered (kind={kind})")
    resp = {"ok": True, "watch": watch, "watch_count": count}
    # A criterion-less log watch is legal — "tell me if it logs anything at all"
    # is a real question — but it must not LOOK like a filter. Say so, so a
    # caller who did not mean it finds out here rather than from a hit list full
    # of events they never asked about.
    if kind == "log" and not any(watch[k] for k in
                                ("regex", "level", "category", "source")):
        resp["matches"] = ("EVERY log event — this watch has no regex, level, "
                           "category or source, so it is a firehose, not a "
                           "filter. Add `regex` if you meant a specific line.")
    return jsonify(resp), 200


def _evaluate_watch(w: dict, start_ms: float) -> dict:
    """Evaluate ONE watch against events since start_ms → {count, hits[bounded]}.
    Reads the normalized stream / failure records / frames on demand; never raises."""
    kind = w.get("kind")
    hits: list[dict] = []
    try:
        if kind == "error_fingerprint":
            fp = w.get("fingerprint")
            for r in _detect_failure_records(limit=3000):
                ms = r.get("ts_ms") or _iso_to_ms(r.get("ts", "")) or 0.0
                if ms < start_ms:
                    continue
                if fp and _record_fingerprint(r) != fp:
                    continue
                hits.append({"ts_ms": ms, "source": r.get("source"),
                             "fingerprint": _record_fingerprint(r),
                             "sample": str((r.get("data") or {}).get("message")
                                           or (r.get("data") or {}).get("name")
                                           or r.get("source") or "")[:120]})
        elif kind == "anomaly":
            want = (w.get("anomaly_type") or "").lower()
            for f in _frames_sorted():
                ms = float(f.get("timestamp_ms") or 0)
                if ms < start_ms:
                    continue
                a = f.get("anomaly") or {}
                if not a.get("detected"):
                    continue
                if want and (a.get("type") or "").lower() != want:
                    continue
                hits.append({"ts_ms": ms, "seq": f.get("sequence"),
                             "type": a.get("type"), "severity": a.get("severity"),
                             "description": (a.get("description") or "")[:120]})
        else:  # log
            pat = None
            if w.get("regex"):
                pat = re.compile(w["regex"], re.IGNORECASE)
            prof = _active_profile_obj()
            events = _log_sources.read_normalized(
                prof, window=(start_ms, float("inf")), limit=1000,
                level=(w.get("level") or ""))
            for ev in events:
                if w.get("category") and ev.get("category") != w["category"]:
                    continue
                if w.get("source") and w["source"] not in str(ev.get("source") or ""):
                    continue
                d = ev.get("data") or {}
                msg = str(d.get("message") or d.get("name") or "")
                hay = msg + " " + str(ev.get("raw") or "")
                if pat is not None and not pat.search(hay):
                    continue
                hits.append({"ts_ms": ev.get("ts_ms"), "level": ev.get("level"),
                             "category": ev.get("category"),
                             "source": ev.get("source") or ev.get("log_label"),
                             "message": msg[:160]})
    except Exception as e:
        return {"count": 0, "hits": [], "error": str(e)}
    hits.sort(key=lambda h: h.get("ts_ms") or 0.0)
    return {"count": len(hits), "hits": hits[-_WATCH_HIT_CAP:]}


@app.route("/watches", methods=["GET"])
def watches_route():
    """List the registered standing watches and their NEW hits (bounded per
    watch). Query: since_baseline=1 scans from the active profile's baseline
    marker instead of each watch's own created time; clear=1 removes all watches
    after reporting (returns them one last time with hits). Set tripwires with
    av_watch/av_baseline, reproduce, then poll this."""
    since_baseline = request.args.get("since_baseline") in ("1", "true", "True")
    clear = request.args.get("clear") in ("1", "true", "True")
    with _watch_lock:
        watches = [dict(w) for w in _watches.values()]
        baseline_ms = _baselines.get(_active_profile_name)
    out = []
    for w in watches:
        start = w.get("created_ms") or 0.0
        if since_baseline and baseline_ms:
            start = baseline_ms
        res = _evaluate_watch(w, start)
        out.append({**w, "scan_since_ms": start,
                    "hit_count": res["count"], "hits": res["hits"],
                    **({"eval_error": res["error"]} if res.get("error") else {})})
    cleared = False
    if clear:
        with _watch_lock:
            _watches.clear()
        cleared = True
    return jsonify({
        "active_profile": _active_profile_name,
        "baseline_ms": baseline_ms,
        "since_baseline": since_baseline,
        "watch_count": len(out),
        "watches": out,
        "cleared": cleared,
    }), 200


# ══════════════════════════════════════════════════════════════════════════════
# TOKEN-ECONOMICS ENGINE
# ══════════════════════════════════════════════════════════════════════════════
# The asymmetry these routes exploit: capturing, hashing, diffing, OCR-ing and
# cropping are FREE (local CPU). Every token the agent spends looking at raw
# pixels or scrolling raw logs is EXPENSIVE. So the bridge does the cheap heavy
# lifting up front and hands the agent the smallest sufficient JSON.
#
# TIERED DETAIL — escalate only when the cheaper tier is insufficient:
#   1. /visual_changes  — the moments the screen changed        (~cheapest)
#   2. /frame/<n>/json  — one frame as JSON, no image at all
#   3. /frame/<n>/region — ONLY the changed pixels, downscaled
#   4. /frame/<n> + the PNG — the full frame                    (most expensive)

_TOKEN_NOTE = ("Token estimates use the documented Claude image rule "
               "ceil(w/28)*ceil(h/28) visual tokens (capped per model tier), "
               "and ~4 characters per token for JSON/text. They are ESTIMATES, "
               "not tokenizer output.")


def _visual_of(frame: dict) -> dict:
    """The visual block a frame carries (empty dict when analysis didn't run)."""
    v = ((frame.get("capture_meta") or {}).get("visual") or {})
    return v if isinstance(v, dict) else {}


def _frame_row(frame: dict) -> dict:
    """One compact row describing a frame's visual state. ~30 tokens."""
    v = _visual_of(frame)
    st = v.get("structural") or {}
    return {
        "seq":           frame.get("sequence"),
        "ts_ms":         float(frame.get("timestamp_ms") or 0),
        "ts":            frame.get("timestamp"),
        "change_score":  v.get("change_score"),
        "changed_bbox":  v.get("changed_bbox"),
        "dhash":         v.get("dhash"),
        "is_blank":      st.get("is_blank"),
        "error":         bool(_frame_error(frame)),
    }


def _one_line_summary(frame: dict) -> str:
    """A human/AI-readable one-liner for a changed moment — no image needed."""
    v = _visual_of(frame)
    st = v.get("structural") or {}
    cs = v.get("change_score")
    bits = []
    if cs is None:
        bits.append("first frame")
    else:
        bits.append(f"{cs * 100:.0f}% of screen changed")
    bbox = v.get("changed_bbox")
    if bbox:
        bits.append(f"region {bbox[2]}x{bbox[3]} at ({bbox[0]},{bbox[1]})")
    if st.get("is_blank"):
        bits.append("BLANK/BLACK frame")
    err = _frame_error(frame)
    if err:
        bits.append(f"error: {(err.get('exception_type') or 'error')}: "
                    f"{(err.get('message') or '')[:80]}")
    prog = frame.get("program") or {}
    if prog and not prog.get("running"):
        bits.append("program NOT running")
    return "; ".join(bits)


@app.route("/visual_changes", methods=["GET"])
def visual_changes():
    """THE token saver. Return only the moments the screen ACTUALLY changed, with
    runs of visually identical frames collapsed into one row.

    Query: from_ms, to_ms, min_change (0-1, default MIN_CHANGE), limit (default 50),
           include_ocr (0/1 — adds a short OCR snippet per changed moment).
    """
    from utils import visual_engine as ve
    _from, _to, window = _window_from_args()
    min_change = _float_arg("min_change", str(ve.MIN_CHANGE))
    limit      = _int_arg("limit", "50")
    limit      = max(1, min(limit, 500))
    include_ocr = request.args.get("include_ocr", "0") not in ("0", "", "false")

    with _lock:
        _agent_reads["visual_changes"] += 1
    frames = _frames_sorted()
    if window is not None:
        frames = [f for f in frames
                  if window[0] <= float(f.get("timestamp_ms") or 0) <= window[1]]

    rows: list[dict] = []
    run: dict | None = None          # current collapsed run (identical or minor)
    analyzed = 0

    def _flush():
        nonlocal run
        if run:
            rows.append(run)
            run = None

    for f in frames:
        v = _visual_of(f)
        if not v:
            continue
        analyzed += 1
        cs = v.get("change_score")
        changed = (cs is None) or (cs >= min_change)
        if changed:
            _flush()
            row = _frame_row(f)
            row["one_line_summary"] = _one_line_summary(f)
            if include_ocr:
                snip = _ocr_image(f.get("annotated_image") or "",
                                  text_cap=400, line_cap=12)
                if snip.get("available"):
                    row["ocr_snippet"] = (snip.get("text") or "")[:400]
                else:
                    row["ocr_snippet"] = None
                    row["ocr_unavailable"] = snip.get("reason")
            rows.append(row)
        else:
            # Sub-threshold frames are COLLAPSED, never discarded. Two kinds,
            # kept distinct so a tiny-but-real change is not mistaken for
            # "nothing happened":
            #   identical      change_score == 0 (nothing moved at grid resolution)
            #   minor_change   0 < change_score < min_change (something small moved)
            # Every run keeps seq_range + a bbox, so the agent can always escalate
            # to the real pixels — we never drop without a retrieval handle.
            seq = f.get("sequence")
            ts  = float(f.get("timestamp_ms") or 0)
            kind = "identical" if (cs or 0) <= 0.0 else "minor_change"
            if run is None or run.get("kind") != kind:
                _flush()
                run = {"no_change": True, "kind": kind,
                       "seq_range": [seq, seq], "frames": 1,
                       "ts_range_ms": [ts, ts],
                       "max_change_score": cs or 0.0,
                       "changed_bbox_union": v.get("changed_bbox")}
                if kind == "minor_change":
                    run["note"] = ("something small changed here (below "
                                   "min_change) — escalate with "
                                   "av_frame_json(seq=<seq_range[1]>) if the "
                                   "detail matters")
            else:
                run["seq_range"][1] = seq
                run["ts_range_ms"][1] = ts
                run["frames"] += 1
                if (cs or 0.0) > (run.get("max_change_score") or 0.0):
                    run["max_change_score"] = cs or 0.0
                bb = v.get("changed_bbox")
                if bb:
                    prev_bb = run.get("changed_bbox_union")
                    if not prev_bb:
                        run["changed_bbox_union"] = list(bb)
                    else:
                        x0 = min(prev_bb[0], bb[0]); y0 = min(prev_bb[1], bb[1])
                        x1 = max(prev_bb[0] + prev_bb[2], bb[0] + bb[2])
                        y1 = max(prev_bb[1] + prev_bb[3], bb[1] + bb[3])
                        run["changed_bbox_union"] = [x0, y0, x1 - x0, y1 - y0]
    _flush()

    # Keep the MOST RECENT `limit` rows (that's what a debugger wants).
    # Totals are taken BEFORE truncation. Reporting only the post-truncation
    # counts alongside `frames_considered: 1500` invited exactly one reading —
    # "34 of 1500 frames changed" — when the real figure over those 1500 frames
    # was 576; the number moved with `limit` (34 at limit=40, 347 at limit=500)
    # while frames_considered stayed at 1500.
    total_changed_moments = sum(1 for r in rows if not r.get("no_change"))
    total_minor_runs = sum(1 for r in rows if r.get("kind") == "minor_change")
    total_collapsed_frames = sum(int(r.get("frames") or 0)
                                 for r in rows if r.get("no_change"))
    total_rows = len(rows)
    truncated = len(rows) > limit
    if truncated:
        rows = rows[-limit:]

    # A survey counts as examination for ORDINARY frames — the whole design says
    # reading the JSON is looking. Failure-shaped frames are deliberately NOT
    # cleared here: a one-line row in a summary is not the same as inspecting the
    # crash, so those stay in the awaiting queue until they are opened or acked.
    try:
        _surveyed: list[int] = []
        for _r in rows:
            lo, hi = (_r.get("seq_range") or [_r.get("seq"), _r.get("seq")])[:2]
            _surveyed.extend(range(int(lo or 0), int(hi or 0) + 1))
        _ret.LEDGER.mark_surveyed(_surveyed, "visual_changes")
    except Exception as _e:
        _dbg(f"visual_changes examine-marking failed: {_e}")

    changed_rows = [r for r in rows if not r.get("no_change")]
    collapsed_frames = sum(int(r.get("frames") or 0)
                           for r in rows if r.get("no_change"))
    minor_runs = [r for r in rows if r.get("kind") == "minor_change"]
    return jsonify({
        "what_this_is": (
            "Only the moments the screen changed. Runs of visually identical "
            "frames collapse into {no_change, kind:'identical', seq_range, "
            "frames}; runs that changed by less than min_change collapse into "
            "kind:'minor_change' with max_change_score and a bbox. NOTHING is "
            "discarded — every collapsed run keeps the seq numbers you need to "
            "escalate to the real frame. This is how you review a long capture "
            "without looking at any image."),
        "window": {"from_ms": _from, "to_ms": _to},
        "min_change": min_change,
        "min_change_meaning": (
            "fraction of a 16x16 grid of screen cells that must change for a "
            "frame to be surfaced as its own moment; 1 cell = 0.0039"),
        "frames_considered": analyzed,
        # TOTALS over `frames_considered` — independent of `limit`.
        "changed_moments": total_changed_moments,
        "frames_collapsed_as_unchanged": total_collapsed_frames,
        "minor_change_runs": total_minor_runs,
        "counts_are": ("totals over frames_considered, NOT over the returned "
                       "rows; raise `limit` to see more rows, it will not change "
                       "these numbers"),
        # …and what this response actually returned, kept separate.
        "rows_returned": len(rows),
        "rows_total_before_truncation": total_rows,
        "changed_moments_in_returned_rows": len(changed_rows),
        "truncated_to_most_recent": truncated,
        "rows": rows,
        "next": {
            "describe_one_moment_as_json": "av_frame_json(seq=<seq>)",
            "see_only_the_changed_pixels": "av_frame_region(seq=<seq>)",
            "full_failure_bundle":         "av_error_moment(seq=<seq>)",
            "lower_the_threshold":         "av_visual_changes(min_change=0.0)",
        },
        "note": _TOKEN_NOTE,
    }), 200


@app.route("/frame/<int:seq>/json", methods=["GET"])
def frame_json(seq: int):
    """IMAGES AS JSON — a frame descriptor with NO full image in it.

    Query: thumbnail (0/1, default 0 — a tiny <=64px thumb), thumb_width,
           ocr (0/1, default 1 — on-screen text when tesseract is available),
           logs (count of time-aligned log lines, default 6).
    """
    from utils import visual_engine as ve
    with _lock:
        frame = _frame_or_load(seq)
        _agent_reads["frame_json"] += 1
        _agent_reads["seqs_json"].add(seq)
        _mark_seen(seq, "frame_json")
    if not frame:
        return _no_such_frame(seq)
    want_thumb = request.args.get("thumbnail", "0") not in ("0", "", "false")
    thumb_w    = _int_arg("thumb_width", "64")
    want_ocr   = request.args.get("ocr", "1") not in ("0", "", "false")
    n_logs     = max(0, min(_int_arg("logs", "6"), 50))

    v = _visual_of(frame)
    st = v.get("structural") or {}
    img = frame.get("annotated_image") or ""
    size = v.get("size") or st.get("size") or [0, 0]

    out: dict = {
        "what_this_is": (
            "This frame described as JSON. It intentionally contains NO full "
            "image — read this first; escalate to av_frame_region (changed "
            "pixels only) and only then to the full PNG."),
        "seq":            seq,
        "ts_ms":          float(frame.get("timestamp_ms") or 0),
        "ts":             frame.get("timestamp"),
        "size":           size,
        "dhash":          v.get("dhash"),
        "dhash_distance_from_prev": v.get("dhash_distance"),
        "change_score":   v.get("change_score"),
        "mean_abs_diff":  v.get("mean_abs_diff"),
        "changed_bbox":   v.get("changed_bbox"),
        "changed_cells":  v.get("changed_cells"),
        "structural":     st,
        "one_line_summary": _one_line_summary(frame),
        "image_path":     img,
        "json_sidecar":   frame.get("json_sidecar"),
    }
    out["structural"] = dict(st)
    out["structural"]["dominant_colors"] = ve.dominant_colors(img, 3)
    # Entropy quadtree: WHERE the information is, as bboxes instead of pixels.
    if request.args.get("content_map", "0") not in ("0", "", "false"):
        q = ve.quadtree_regions(img)
        if q.get("available"):
            out["content_map"] = {
                "dense_regions": q.get("densest"),
                "dense_region_count": q.get("dense_region_count"),
                "region_count": q.get("region_count"),
                "screen_coverage_pct": q.get("coverage_pct"),
                "tau": q.get("tau"),
                "meaning": q.get("meaning"),
                "crop_the_densest": f"av_frame_region(seq={seq}, bbox='dense')",
            }
        else:
            out["content_map"] = {"available": False, "reason": q.get("reason")}

    # Error carried by this frame (already structured by the collector).
    err = _frame_error(frame)
    if err:
        out["error"] = {"exception_type": err.get("exception_type"),
                        "message": (err.get("message") or "")[:300],
                        "file": err.get("file"), "line": err.get("line"),
                        "probable_cause": err.get("probable_cause"),
                        "fingerprint": err.get("fingerprint")}

    # Any auto-detected visual event that covers this frame.
    with _lock:
        evs = [dict(e) for e in _visual_events
               if int(e.get("seq") or 0) <= seq <= int(e.get("seq_end") or e.get("seq") or 0)]
    if evs:
        out["visual_events"] = evs[-3:]

    if want_ocr:
        ocr = _ocr_image(img, text_cap=2000, line_cap=60)
        if ocr.get("available"):
            out["ocr_text"] = ocr.get("text")
            out["ocr_line_count"] = ocr.get("line_count")
        else:
            out["ocr_text"] = None
            out["ocr_unavailable"] = ocr.get("reason")
            out["ocr_install_hint"] = ocr.get("install_hint")

    if n_logs:
        ms = out["ts_ms"]
        try:
            prof = _active_profile_obj()
            evs2 = _log_sources.read_normalized(
                prof, window=(ms - 3000.0, ms + 500.0), limit=n_logs)
        except Exception:
            evs2 = []
        out["aligned_logs"] = [
            {"ts_ms": e.get("ts_ms"), "level": e.get("level"),
             "source": e.get("source"),
             "message": str(((e.get("data") or {}).get("message")
                             or (e.get("data") or {}).get("name") or ""))[:160]}
            for e in evs2[-n_logs:]
        ]

    if want_thumb:
        th = ve.thumbnail_b64(img, width=max(16, min(thumb_w, 256)))
        if th.get("ok"):
            out["thumbnail_b64"] = th["image_b64"]
            out["thumbnail_size"] = th["size"]
            out["thumbnail_est_visual_tokens"] = th["est_visual_tokens"]
        else:
            out["thumbnail_error"] = th.get("reason")

    # Honest cost accounting, right in the payload.
    body_chars = len(json.dumps(out))
    out["token_math"] = {
        "this_descriptor_est_tokens": ve.est_text_tokens(body_chars),
        "full_frame_est_visual_tokens": ve.visual_tokens(size[0] or 0, size[1] or 0),
        "method": _TOKEN_NOTE,
    }
    out["next"] = {"changed_pixels_only": f"av_frame_region(seq={seq})",
                   "full_failure_bundle": f"av_error_moment(seq={seq})"}
    return jsonify(out), 200


@app.route("/frame/<int:seq>/region", methods=["GET"])
def frame_region(seq: int):
    """Serve ONLY a crop of a frame — by default the region that CHANGED.

    Query: bbox=x,y,w,h  or  bbox=changed (default) or bbox=full,
           max_dim (default 900), ocr (0/1, default 1).
    Tier 3 of the cheap path: when JSON isn't enough, send the smallest
    sufficient pixels rather than a full 4K screenshot."""
    from utils import visual_engine as ve
    with _lock:
        frame = _frame_or_load(seq)
        _agent_reads["region"] += 1
        _mark_seen(seq, "region")
    if not frame:
        return _no_such_frame(seq)
    img = frame.get("annotated_image") or ""
    v = _visual_of(frame)
    size = v.get("size") or (v.get("structural") or {}).get("size") or [0, 0]
    raw = (request.args.get("bbox") or "changed").strip().lower()
    max_dim = max(32, min(_int_arg("max_dim", "900"), 4096))
    want_ocr = request.args.get("ocr", "1") not in ("0", "", "false")

    mode = "explicit"
    if raw in ("changed", "change", "auto", ""):
        mode = "changed"
        bbox = v.get("changed_bbox")
        if not bbox:
            mode = "full (no changed region recorded for this frame)"
            bbox = [0, 0, size[0] or 0, size[1] or 0]
    elif raw == "dense":
        # Entropy quadtree: the most information-dense region of the screen.
        mode = "dense (highest-entropy region)"
        bbox = ve.densest_region(img)
        if not bbox:
            mode = "full (quadtree unavailable)"
            bbox = [0, 0, size[0] or 0, size[1] or 0]
    elif raw == "full":
        mode = "full"
        bbox = [0, 0, size[0] or 0, size[1] or 0]
    else:
        try:
            parts = [int(float(p)) for p in raw.replace(" ", "").split(",")]
        except Exception:
            abort(400)
        if len(parts) != 4:
            abort(400)
        bbox = parts

    if not bbox or not bbox[2] or not bbox[3]:
        return jsonify({"error": "no usable bbox",
                        "seq": seq, "hint": "pass bbox=x,y,w,h or bbox=full"}), 400

    crop = ve.crop_region(img, bbox, max_dim=max_dim)
    if not crop.get("ok"):
        return jsonify({"error": crop.get("reason"), "seq": seq}), 500
    crop["seq"] = seq
    crop["bbox_mode"] = mode
    crop["ts_ms"] = float(frame.get("timestamp_ms") or 0)
    crop["change_score"] = v.get("change_score")
    # Describe what was ACTUALLY served. `bbox=full` returns the whole frame
    # (downscaled to max_dim), so claiming "only the changed region" there was a
    # false description of the very bytes being handed over.
    if mode.startswith("full"):
        crop["what_this_is"] = (
            "The FULL frame, base64-encoded and downscaled to max_dim. Decode "
            "`image_b64` to view it. For fewer tokens, request bbox=changed "
            "(the region that changed) or bbox=dense (the busiest region).")
    else:
        crop["what_this_is"] = (
            f"A CROP of the frame ({mode}), base64-encoded. Decode `image_b64` to "
            "view it. Because it is only part of the frame, downscaled, it costs "
            "a fraction of the tokens a full frame would.")
    if want_ocr:
        # OCR the crop itself (its own text, not the whole screen's).
        try:
            import base64 as _b64, tempfile as _tf
            with _tf.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                tf.write(_b64.b64decode(crop["image_b64"]))
                tmp_path = tf.name
            ocr = _ocr_image(tmp_path, text_cap=1500, line_cap=40)
            os.unlink(tmp_path)
        except Exception as exc:
            ocr = {"available": False, "reason": f"crop OCR failed: {exc}"}
        if ocr.get("available"):
            crop["region_ocr_text"] = ocr.get("text")
        else:
            crop["region_ocr_text"] = None
            crop["ocr_unavailable"] = ocr.get("reason")
    saved = (crop.get("est_visual_tokens_full_frame") or 0) - (crop.get("est_visual_tokens") or 0)
    crop["token_math"] = {"est_visual_tokens_this_crop": crop.get("est_visual_tokens"),
                          "est_visual_tokens_full_frame": crop.get("est_visual_tokens_full_frame"),
                          "est_tokens_saved_vs_full_frame": max(0, saved),
                          "method": _TOKEN_NOTE}
    return jsonify(crop), 200


def _nearest_frame(ts_ms: float) -> tuple[dict | None, float | None]:
    """Frame whose shutter is closest to ts_ms, plus |delta| in ms."""
    best, best_dt = None, None
    with _lock:
        for f in _frames.values():
            fts = float(f.get("timestamp_ms") or 0)
            if not fts:
                continue
            dt = abs(fts - ts_ms)
            if best_dt is None or dt < best_dt:
                best, best_dt = dict(f), dt
    return best, best_dt


@app.route("/error_moment", methods=["GET"])
def error_moment():
    """THE ONE-CALL FAILURE BUNDLE — replaces 5-6 separate calls.

    Query: fingerprint=<fp> | seq=<n> | (nothing = the latest error),
           window_secs (default 6), include_image (0/1, default 0),
           max_dim (default 900).

    Returns, correlated for you: the structured error (type/message/
    probable_cause/stack frames), the frame at that exact shutter, its changed
    region + region OCR, the time-aligned log window from ALL sources, the
    state_delta, and the source code around each stack frame."""
    from utils import visual_engine as ve
    fp        = (request.args.get("fingerprint") or "").strip()
    seq_arg   = request.args.get("seq")
    win_s     = _float_arg("window_secs", "6")
    want_img  = request.args.get("include_image", "0") not in ("0", "", "false")
    max_dim   = max(32, min(_int_arg("max_dim", "900"), 4096))

    frame: dict | None = None
    err: dict | None = None
    how = ""
    if seq_arg is not None:
        try:
            seq = int(seq_arg)
        except ValueError:
            abort(400)
        with _lock:
            frame = dict(_frames[seq]) if seq in _frames else None
        if frame is None:
            return _no_such_frame(seq)
        err = _frame_error(frame)
        how = f"seq={seq}"
    elif fp:
        f2, seq2 = _find_error_by_fingerprint(fp)
        if f2 is not None:
            frame, err, how = dict(f2), _frame_error(f2), f"fingerprint={fp}"
        else:
            # No captured FRAME carries this fingerprint — but there are TWO
            # error indexes: frame-attached errors, and the log-scan index
            # (_error_groups) that /diagnose and the ambient push channel hand
            # out. Without this fallback, AgentVision would recommend
            # av_error_moment(fingerprint=…) and then 404 on its own advice —
            # a dead end for the agent. Resolve it from the log index and
            # attach the nearest frame in time (if any) for the visual half.
            g = _error_groups(_detect_failure_records(limit=3000)).get(fp)
            if g is None:
                return jsonify({"error": f"unknown error fingerprint {fp!r}",
                                "hint": "av_diagnose() or av_errors_by_fingerprint() "
                                        "list the known ones"}), 404
            ms = float(g.get("last_ms") or 0.0)
            frame, _dt = _nearest_frame(ms) if ms else (None, None)
            err = {"exception_type": g.get("exception_type"),
                   "message": g.get("sample"),
                   "fingerprint": fp,
                   "probable_cause": g.get("probable_cause"),
                   "occurrence_count": g.get("count"),
                   "first_seen": g.get("first_ts"),
                   "last_seen": g.get("last_ts")}
            how = (f"fingerprint={fp} via the log index"
                   + (f"; nearest frame in time attached (Δ{int(_dt)}ms)"
                      if frame is not None and _dt is not None
                      else "; no frame was captured near it"))
    else:
        # Latest frame that carries an error; else the latest error record.
        for f in reversed(_frames_sorted()):
            if _frame_error(f):
                frame, err, how = f, _frame_error(f), "latest frame with an error"
                break
        if frame is None:
            trig = _detect_failure_records(limit=2000)
            if trig:
                last = trig[-1]
                ms = last.get("ts_ms") or _iso_to_ms(last.get("ts", "")) or 0.0
                frame, _dt = _nearest_frame(float(ms))
                how = "latest error RECORD (no frame carried a structured error)"
                d = last.get("data") if isinstance(last.get("data"), dict) else {}
                err = {"exception_type": d.get("exception_type"),
                       "message": d.get("message") or last.get("source"),
                       "fingerprint": _record_fingerprint(last),
                       "probable_cause": d.get("probable_cause")}
    if frame is None and err is None:
        return jsonify({
            "found": False,
            "resolved_by": "nothing",
            "hint": ("No error found. av_diagnose gives ranked hypotheses even "
                     "without a hard error; av_visual_changes shows what the "
                     "screen did."),
        }), 200

    seq = (frame or {}).get("sequence")
    ts_ms = float((frame or {}).get("timestamp_ms") or 0)
    # This is THE call for inspecting a failure, so it discharges the frame's
    # examination obligation even for failure-shaped frames.
    _mark_seen(seq, "error_moment")
    v = _visual_of(frame or {})
    size = v.get("size") or (v.get("structural") or {}).get("size") or [0, 0]

    bundle: dict = {
        "found": True,
        "what_this_is": (
            "One pre-correlated failure bundle: the error, the frame at that "
            "instant, the pixels that changed, the on-screen text, the log "
            "window from every source, the state delta, and the code. You do "
            "NOT need to make follow-up calls to assemble this."),
        "resolved_by": how,
        "frame_seq": seq,
        "ts_ms": ts_ms,
        "ts": (frame or {}).get("timestamp"),
        "error": None,
        "frame": None,
        "changed_region": None,
        "on_screen_text": None,
        "logs": [],
        "state_delta": (frame or {}).get("state_delta") or {},
        "code": None,
        "note": _TOKEN_NOTE,
    }

    if err:
        bundle["error"] = {
            "exception_type":  err.get("exception_type"),
            "message":         (err.get("message") or "")[:600],
            "file":            err.get("file"),
            "line":            err.get("line"),
            "language":        err.get("language"),
            "probable_cause":  err.get("probable_cause") or err.get("likely_cause"),
            "fingerprint":     err.get("fingerprint"),
            "occurrence_count": err.get("occurrence_count"),
            "stack_frames":    (err.get("frames") or [])[:8],
        }

    if frame:
        bundle["frame"] = {
            "seq":  seq,
            "size": size,
            "dhash": v.get("dhash"),
            "change_score": v.get("change_score"),
            "changed_bbox": v.get("changed_bbox"),
            "structural": v.get("structural") or {},
            "one_line_summary": _one_line_summary(frame),
            "image_path": frame.get("annotated_image"),
            "capture_health": {
                "black_frame": ((frame.get("capture_meta") or {}).get("black_frame")),
                "window_found": ((frame.get("capture_meta") or {}).get("window_found")),
            },
            "program_running": (frame.get("program") or {}).get("running"),
        }
        # Changed region: always its bbox + OCR; the pixels only on request.
        bbox = v.get("changed_bbox") or ([0, 0, size[0], size[1]] if size and size[0] else None)
        if bbox:
            crop = ve.crop_region(frame.get("annotated_image") or "", bbox,
                                  max_dim=max_dim)
            if crop.get("ok"):
                region = {"bbox": crop["bbox"], "served_size": crop["served_size"],
                          "est_visual_tokens": crop["est_visual_tokens"],
                          "est_visual_tokens_full_frame": crop["est_visual_tokens_full_frame"]}
                if want_img:
                    region["image_b64"] = crop["image_b64"]
                    region["format"] = crop["format"]
                else:
                    region["image_b64"] = None
                    region["how_to_get_pixels"] = (
                        f"av_frame_region(seq={seq}) or add include_image=1 — "
                        "omitted by default to keep this bundle cheap")
                bundle["changed_region"] = region
        ocr = _ocr_image(frame.get("annotated_image") or "", text_cap=2500, line_cap=80)
        if ocr.get("available"):
            bundle["on_screen_text"] = ocr.get("text")
        else:
            bundle["on_screen_text"] = None
            bundle["ocr_unavailable"] = ocr.get("reason")
            bundle["ocr_install_hint"] = ocr.get("install_hint")

    # Time-aligned log window from EVERY source the profile declares.
    if ts_ms:
        half = abs(win_s) * 1000.0
        try:
            prof = _active_profile_obj()
            evs = _log_sources.read_normalized(
                prof, window=(ts_ms - half, ts_ms + half), limit=60)
        except Exception:
            evs = []
        bundle["logs"] = [
            {"ts_ms": e.get("ts_ms"), "level": e.get("level"),
             "source": e.get("source"), "label": e.get("log_label"),
             "message": str(((e.get("data") or {}).get("message")
                             or (e.get("data") or {}).get("name") or ""))[:200]}
            for e in evs
        ]
        bundle["log_window"] = {"from_ms": ts_ms - half, "to_ms": ts_ms + half,
                                "window_secs": win_s,
                                "sources": "all profile log_sources, normalized"}

    # Code context via the existing source_at_error machinery.
    fp_use = (bundle.get("error") or {}).get("fingerprint") or fp
    if fp_use:
        try:
            with app.test_request_context(f"/source_at_error?fingerprint={fp_use}&frames=4&context=4"):
                resp = source_at_error()
            body = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
            if isinstance(body, dict) and body.get("frames"):
                bundle["code"] = {"project_root": body.get("project_root"),
                                  "frames": body.get("frames")[:4]}
        except Exception as exc:
            bundle["code_error"] = str(exc)

    # FLIGHT RECORDER: if this moment was frozen as an incident, hand over the
    # whole pre-error window — that is strictly better than the single frame.
    if seq is not None:
        inc = _incident_for_seq(int(seq))
        if inc:
            bundle["incident"] = {
                "id": inc.get("id"),
                "kind": inc.get("kind"),
                "detail": inc.get("detail"),
                "window_ms": inc.get("window_ms"),
                "pre_error_seconds": inc.get("pre_error_seconds"),
                "frame_count": inc.get("frame_count"),
                "why_this_matters": ("The recorder had already frozen the "
                                     f"{inc.get('pre_error_seconds')}s BEFORE this "
                                     "failure, so the run-up is still on disk."),
                "step_through_it": (f"av_replay(incident='{inc.get('id')}')"),
                "detail_call": f"av_incidents(id='{inc.get('id')}')",
            }

    bundle["est_bundle_tokens"] = ve.est_text_tokens(len(json.dumps(bundle)))
    bundle["replaces_calls"] = ["av_get_frame", "av_ocr_frame", "av_log_normalized",
                                "av_state_diff", "av_source_at_error",
                                "av_frame_region"]
    return jsonify(bundle), 200


# Short TTL cache for the ambient state. Hook events arrive in bursts —
# PostToolBatch immediately followed by PostToolUse, or several PostToolUse in a
# row — and re-reading the JSONL each time is the dominant cost (measured ~26 ms
# steady-state, ~100 ms cold). One second of staleness is invisible to a human and
# far cheaper than recomputing per hook.
_AMBIENT_TTL_MS = float(os.environ.get("AGENTVISION_AMBIENT_TTL_MS", "1200"))
_ambient_cache: dict = {"ts_ms": 0.0, "state": None}


def _ambient_state(use_cache: bool = True) -> dict:
    """Assemble the ambient engine's input from machinery that ALREADY exists —
    the digest's health/error signals, the visual engine, the flight recorder,
    the capture engine. The ambient layer must never re-implement detection."""
    if use_cache and _ambient_cache["state"] is not None:
        if (clock.now_ms() - float(_ambient_cache["ts_ms"])) < _AMBIENT_TTL_MS:
            return _ambient_cache["state"]
    state = _ambient_state_uncached()
    _ambient_cache["state"] = state
    _ambient_cache["ts_ms"] = clock.now_ms()
    return state


def _ocr_corroboration(limit: int = 8) -> list:
    """Cross-check the last OCR pass against the recent raw log. Cheap by design.

    Runs on the PUSH path, not the capture path. The capture loop works to a
    ~100 ms budget at up to 10 fps and already throttles OCR itself; matching a
    handful of tokens against a few KB of text is microseconds, and the push path
    happens to hold both halves at the same instant, which is the whole point.

    Failure here must never break an injection — a missing corroboration block is
    a smaller problem than no push at all.
    """
    try:
        with _lock:
            text = str(_visual_stats.get("last_ocr_text") or "")
        if not text.strip():
            return []
        try:
            from connectors.log_fields import corroborate_ocr_tokens
        except Exception:
            from python_backend.connectors.log_fields import corroborate_ocr_tokens
        prof = _active_profile_obj()
        window = ""
        for src in (getattr(prof, "log_sources", None) or [])[:4]:
            path = src.get("path") if isinstance(src, dict) else str(src)
            if not path:
                continue
            try:
                with open(path, "r", encoding="utf-8",
                          errors="replace") as fh:
                    fh.seek(0, 2)
                    fh.seek(max(0, fh.tell() - 24_000))
                    window += fh.read()
            except Exception:
                continue
        if not window:
            return []
        return corroborate_ocr_tokens(text, window, limit=limit)
    except Exception:
        return []


def _screen_text_snippet(max_chars: int = 120, max_lines: int = 4) -> str:
    """A very short digest of the most recent on-screen text.

    Sourced from the OCR the capture path ALREADY performed (throttled to one
    scan per AGENTVISION_VISUAL_OCR_GAP_MS on changed frames), so this costs no
    extra CPU — the text used to be tested for error keywords and then thrown
    away. Deliberately tiny: this rides inside a byte-capped push injection,
    where it is worth roughly 20 tokens and can save a whole tool call.
    """
    with _lock:
        raw = str(_visual_stats.get("last_ocr_text") or "")
    if not raw:
        return ""
    lines: list[str] = []
    for ln in raw.splitlines():
        ln = " ".join(ln.split())
        # Skip noise: blanks and single stray glyphs OCR sometimes invents.
        if len(ln) > 1:
            lines.append(ln)
        if len(lines) >= max_lines:
            break
    if not lines:
        return ""
    text = " | ".join(lines)
    return text[:max_chars - 1] + "…" if len(text) > max_chars else text


def _ambient_state_uncached() -> dict:
    """The real assembly (see _ambient_state)."""
    collector = _get_collector()
    e = _auto_engine
    frames = _frames_sorted()
    with _lock:
        latest = _latest_for_active()
        # Per-profile. The ambient push opens with "Visual: …" only when
        # frames_stored is non-zero, so a global count made the push describe
        # ANOTHER program's screen to a profile that had captured nothing —
        # observed as "AbyssEngine … 78 changed moment(s) … latest frame #13016",
        # all three figures SharpEmu's.
        n_frames = _active_frame_count()[0]
        incidents = [dict(x) for x in _incidents]
        vis_events = [dict(x) for x in _visual_events]
        still_since = _visual_prev.get("still_since_ms")

    # ── Visual: straight from the visual engine's stored per-frame analysis ──
    v = _visual_of(latest or {})
    st = v.get("structural") or {}
    analyzed = [f for f in frames[-200:] if _visual_of(f)]
    changed = 0
    for f in analyzed:
        cs = _visual_of(f).get("change_score")
        if cs is None or cs >= visual_min_change():
            changed += 1
    visual = {
        "latest_seq":          (latest or {}).get("sequence"),
        "latest_change_score": v.get("change_score"),
        "changed_bbox":        v.get("changed_bbox"),
        "is_blank":            st.get("is_blank"),
        "frames_considered":   len(analyzed),
        "changed_moments":     changed,
        "still_for_s": (round((clock.now_ms() - float(still_since)) / 1000.0, 1)
                        if still_since else None),
        "on_screen_error_text": any(x.get("type") == "on_screen_error"
                                    for x in vis_events[-5:]),
        # What the screen actually SAYS, from the throttled capture-path OCR that
        # already ran. Free to include, and it answers the obvious follow-up to
        # "the screen is STATIC" — static showing what? — without a tool call.
        "on_screen_text": _screen_text_snippet(),
        # Tri-state, so the push can distinguish "OCR read the screen and it was
        # clean" from "OCR read nothing". The second is not evidence of anything.
        "ocr_state": str(_visual_stats.get("last_ocr_state") or ""),
        # Which frame the OCR snippet above actually came from. Written since the
        # feature was built and read by nothing, which is exactly how the snippet
        # came to be attributed to whatever frame happened to be latest.
        "ocr_seq": _visual_stats.get("last_ocr_seq"),
        # Machine-shaped tokens from that OCR, each marked with whether the SAME
        # value appears in the time-aligned log. Absence is not proof of error —
        # a clock or a version string appears in no log — but for a hex address
        # it is the only cheap check there is, and OCR's own confidence is
        # useless (measured: 1.0 on a line of pure noise).
        "ocr_tokens": _ocr_corroboration(),
    }

    # ── Log freshness, so a log-derived claim can only be cleared by a log ───
    # The correction path needs to know whether we are still READING before it
    # may say an error stopped. Without this it defaulted to "not stale" and
    # cleared errors on the strength of having stopped looking.
    try:
        _fresh = _source_freshness(collector.profile)
        logs_block = {
            "stale": bool(_fresh.get("stale_sources")),
            "stale_sources": list(_fresh.get("stale_sources") or []),
            "program_silent_s": _fresh.get("program_silent_s"),
            "newest_event_ms": _fresh.get("newest_program_event_ms"),
        }
    except Exception as _exc:
        # Unknown freshness must read as "cannot tell", never as "fresh" — an
        # error correction that fires on unknown freshness is claiming an error
        # stopped when we may simply have stopped reading.
        #
        # But the exception is NAMED here rather than swallowed silently. The
        # first version of this block referenced an out-of-scope `prof`, so a
        # NameError was caught on EVERY call and the whole error-correction path
        # was permanently cannot_tell — dead code hidden by its own safety net.
        # A blanket `except Exception` that discards the reason is how a coding
        # error disguises itself as a cautious default.
        logs_block = {"stale": None, "program_silent_s": None,
                      "unavailable_because": f"{type(_exc).__name__}: {_exc}"[:160]}

    # ── Errors that are NEW this session (same rule the digest uses) ─────────
    # Plus STANDING errors: every known error group regardless of when it was
    # first seen. Without these, a bridge that started AFTER a crash reported
    # nothing (no session-new fingerprint, no witnessed death) while /diagnose
    # was reporting the crash — the ambient channel went quiet on a program that
    # was demonstrably broken. build_signals() decides the tier; per-fingerprint
    # suppression keeps each one to a single mention per session.
    new_errors = []
    standing_errors = []
    try:
        triggers = _detect_failure_records(limit=2000)
        groups = _error_groups(triggers)
        history = _load_fp_history()
        for fp, g in groups.items():
            # Lead with the exception type so the injected line reads
            # "KeyError: 'port'" rather than a bare "'port'".
            row = {"fingerprint": fp, "count": g["count"],
                   "sample": _error_sample(g.get("exception_type") or "",
                                           g.get("sample") or ""),
                   "exception_type": g.get("exception_type"),
                   "probable_cause": g.get("probable_cause"),
                   "last_ms": g.get("last_ms") or 0.0}
            if g["last_ms"] >= _session_start_ms and fp not in history:
                # carry the cause too — a NEW error deserves at least as much
                # explanation as a standing one
                new_errors.append({k: row[k] for k in
                                   ("fingerprint", "count", "sample",
                                    "exception_type", "probable_cause")})
            else:
                standing_errors.append(row)
        new_errors.sort(key=lambda x: -x["count"])
        # most recent first, then most frequent — the freshest problem leads
        standing_errors.sort(key=lambda x: (-x["last_ms"], -x["count"]))
    except Exception as exc:
        _dbg(f"ambient new/standing errors failed: {exc}")

    # ── Health, reusing the digest's own scorer ──────────────────────────────
    try:
        health = _health_block(latest, [x["fingerprint"] for x in new_errors],
                              [], e)
    except Exception:
        health = {}

    running = False
    try:
        running = bool(collector._reader.is_running())
    except Exception:
        pass

    ph = _preflight_hint(collector.profile) or {}
    gap = None
    marker = ph.get("marker") or {}
    if marker and marker.get("gap_count"):
        gap = f"{marker.get('gap_count')} format(s)"

    return {
        "profile": _active_profile_name,
        "program": {"name": collector.profile.display_name,
                    "running": running,
                    # "was_running" lets the engine detect a death rather than a
                    # never-started program (which is not news).
                    "was_running": bool(n_frames) and bool(
                        (latest or {}).get("program", {}).get("running")),
                    "died_at_ms": (latest or {}).get("timestamp_ms")},
        "capture": {"engine_running": e.running, "capturing": e.capturing,
                    "frames_stored": n_frames,
                    "window_missing": e.window_missing,
                    "capture_app": getattr(collector.profile, "capture_app", ""),
                    "blank_frames": e.blank_frame_count},
        "visual": visual,
        # Whether we are still READING. A log-derived claim may only be cleared
        # by a fresh log; without this the correction path defaulted to "not
        # stale" and retracted errors on the strength of having stopped looking.
        "logs": logs_block,
        "incidents": incidents,
        "visual_events": vis_events,
        "new_errors": new_errors,
        "standing_errors": standing_errors[:4],
        # EVERY fingerprint still being tracked, untruncated. The lists above are
        # capped for injection size (and build_signals caps them again at 2), which
        # made an error ranked 3rd invisible to the correction path — so
        # AgentVision retracted a claim about an error it was still counting. A
        # display cap must never become the authority on what exists.
        "tracked_error_fingerprints": sorted(
            {str(e.get("fingerprint")) for e in (new_errors or [])
             if e.get("fingerprint")}
            | {str(e.get("fingerprint")) for e in (standing_errors or [])
               if e.get("fingerprint")}),
        "health": health,
        "preflight_gap": gap,
        # Frames retention is holding for the agent. Bounded to a handful: the
        # push channel names the batch, it does not enumerate 500 seqs.
        "frames_awaiting": {
            "rows": _ret.LEDGER.awaiting(limit=8),
            "total": len(_ret.LEDGER.awaiting(limit=1000)),
            "mode": _ret.LEDGER.mode,
        },
    }


@app.route("/ambient", methods=["GET", "POST"])
def ambient_route():
    """PUSH MODE — what AgentVision would tell the agent RIGHT NOW, unprompted.

    Silent by default: if nothing meaningful changed since this session was last
    told, it returns {inject:false, text:""} and costs nothing. Otherwise it
    returns a tiny, byte-capped, plain-text injection at one of three tiers
    (heartbeat / notice / alert).

    Query/body: session_id (delta tracking is per session), event (the hook event
    name), force (1 = bypass rate-limit + delta suppression, for previews),
    stop_check (1 = evaluate the Stop backstop instead of an injection),
    stop_hook_active (1 = the harness says this Stop already followed a block)."""
    from api import ambient as _amb
    src = request.args if request.method == "GET" else (
        request.get_json(silent=True) or request.args)
    sid = str(src.get("session_id") or "default")
    event = str(src.get("event") or "manual")
    force = str(src.get("force") or "0") not in ("0", "", "false")
    t0 = time.perf_counter()
    state = _ambient_state()

    if str(src.get("stop_check") or "0") not in ("0", "", "false"):
        active = str(src.get("stop_hook_active") or "0") not in ("0", "", "false")
        verdict = _amb.stop_backstop(state, sid, stop_hook_active=active)
        verdict["build_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        verdict["enabled"] = _amb.STOP_BLOCK_ENABLED
        verdict["max_stop_blocks"] = _amb.MAX_STOP_BLOCKS
        verdict["eligible_kinds"] = sorted(_amb.STOP_BLOCK_KINDS)
        return jsonify(verdict), 200

    # RAW LOG: peek WITHOUT advancing the session offset, decide, and only then
    # commit. Advancing first would repeat the PostToolUse bug found on this
    # project — the payload was consumed and marked delivered while never
    # actually reaching the agent, losing it permanently.
    try:
        state = dict(state)
        state["raw_log"] = _raw_log_delta(sid, advance=False,
                                         cap_bytes=RAW_PUSH_CAP)
        # Only pay for lsof when a source actually looks dead — it is a subprocess
        # on the agent's critical path, and asking on every quiet tick would be a
        # cost with no answer to give.
        if any(s.get("stale") for s in (state["raw_log"].get("sources") or [])):
            state["write_targets"] = _write_targets_report()
    except Exception as exc:
        _dbg(f"raw log delta failed: {exc}")

    out = _amb.decide(state, session_id=sid, event=event, force=force)

    # Commit the offset ONLY if the raw lines actually went out, and never for a
    # forced preview (which must not blind the next real injection).
    if out.get("inject") and not force and (state.get("raw_log") or {}).get("sources"):
        try:
            _raw_log_delta(sid, advance=True)
        except Exception as exc:
            _dbg(f"raw log offset commit failed: {exc}")
    # Record that the agent was actually TOLD about these frames. Offering is
    # not the same as examining — it does not release the frame — but it is what
    # lets /retention distinguish "the agent never heard about it" from "the
    # agent was told and did not look", which are very different failures.
    if out.get("inject") and out.get("offered_seqs"):
        try:
            _ret.LEDGER.mark_offered(out["offered_seqs"])
        except Exception as exc:
            _dbg(f"mark_offered failed: {exc}")
    out["build_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
    out["caps"] = {"heartbeat": _amb.HEARTBEAT_CAP, "notice": _amb.NOTICE_CAP,
                   "alert": _amb.ALERT_CAP}
    out["min_gap_ms"] = _amb.MIN_GAP_MS
    out["session_stats"] = _amb.MEMORY.stats(sid)
    # Is another push channel already delivering to this agent? Lets a second
    # channel (the MCP resource-updated poller) stand down rather than announce
    # what the Claude Code hook just injected. None = no other session has ever
    # been injected, which is not the same as "no other channel exists".
    out["other_channel_ms_ago"] = _amb.MEMORY.other_channel_ms_ago(sid)
    out["what_this_is"] = (
        "The ambient (push) view. A hook calls this and injects `text` into the "
        "conversation only when inject=true. Silent by default, delta-only, "
        "byte-capped.")
    return jsonify(out), 200


@app.route("/ambient/reset", methods=["POST"])
def ambient_reset():
    """Forget what has been pushed (whole bridge, or one session_id). Makes the
    next /ambient call speak again — used by the GUI preview and by tests."""
    from api import ambient as _amb
    body = request.get_json(silent=True) or {}
    sid = body.get("session_id")
    _amb.MEMORY.reset(sid)
    return jsonify({"ok": True, "reset": sid or "ALL sessions"}), 200


# ── Retention: examine before delete ─────────────────────────────────────────

#: Per-session raw-log read offsets, so each push carries the lines that are
#: genuinely NEW to that session rather than re-sending the whole tail.
_raw_log_offsets: dict[str, dict] = {}
#: Byte budget for the raw-log block inside a push injection. Much larger than
#: the prose caps: this is the actual program output, which is the point.
#: TOTAL across all sources, not per source. Sized to stay inside the harness's
#: inline hook-output limit: a 12.7 KB block was spilled to a file and the agent
#: got a 2 KB preview instead of the lines, which defeats the whole purpose.
RAW_PUSH_CAP = int(os.environ.get("AGENTVISION_RAW_PUSH_CAP", "3500"))


def _raw_log_delta(session_id: str, *, advance: bool = True,
                   cap_bytes: int = 0) -> dict:
    """New RAW log lines for this session, verbatim.

    `advance=False` peeks without consuming, so a preview cannot make the next
    real injection go blind.
    """
    prof = _active_profile_obj()
    key = f"{session_id}::{_active_profile_name}"
    prev = _raw_log_offsets.get(key) or {}
    res = _log_sources.read_raw_delta(prof, prev)
    if advance:
        _raw_log_offsets[key] = res.get("offsets") or {}
    if cap_bytes > 0:
        _cap_raw_block(res, cap_bytes)
    return res


def _cap_raw_block(res: dict, cap_bytes: int) -> dict:
    """Trim to a TOTAL byte budget by dropping the OLDEST lines.

    The budget is shared across sources, not per source: a per-source cap let two
    log files each spend the full allowance, and the resulting 12.7 KB injection
    blew the harness's inline limit and got spilled to a file — so the agent saw a
    2 KB preview instead of the lines. Newest-wins, because a push answers "what
    just happened". Whatever is cut is reported with the exact offset to fetch it,
    so the agent is always told the truth about what it is NOT seeing.
    """
    def _len(item) -> int:
        return len(item if isinstance(item, str) else item.get("line", "")) + 1

    srcs = [s for s in (res.get("sources") or []) if s.get("lines")]
    if not srcs or cap_bytes <= 0:
        return res
    # Share the budget evenly among sources that actually have new lines, then
    # hand any unspent remainder back so one chatty source can use the slack.
    share = max(240, cap_bytes // max(1, len(srcs)))
    spent = 0
    for i, s in enumerate(srcs):
        budget = share + max(0, (cap_bytes - spent) - share * (len(srcs) - i))
        lines = s.get("lines") or []
        if sum(_len(x) for x in lines) <= budget:
            spent += sum(_len(x) for x in lines)
            continue
        kept: list = []
        acc = 0
        for item in reversed(lines):
            n = _len(item)
            if acc + n > budget:
                break
            kept.append(item)
            acc += n
        kept.reverse()
        spent += acc
        s["withheld_oldest_lines"] = len(lines) - len(kept)
        s["withheld_note"] = (
            f"{len(lines) - len(kept)} older line(s) not shown here — "
            f"av_log_raw(from_offset={s.get('from_offset')}) for the full delta")
        s["lines"] = kept
    return res


@app.route("/log/raw", methods=["GET"])
def log_raw_route():
    """THE RAW LOG. Verbatim bytes, every source, no interpretation.

    No adapter runs, no level is assigned, nothing is ranked or filtered for
    being "uninteresting". Use this whenever you want the program's own output
    rather than AgentVision's reading of it — and prefer it when a summary looks
    suspiciously clean: on this project the normalized view reported "health 100,
    program looks healthy" while 180 GPU present failures sat in these bytes.

    The only reduction is LOSSLESS collapsing of byte-identical consecutive
    lines into {line, repeat:N} — 49% of a real boot log was one line repeated
    21,982 times. A distinct line is never dropped.

    Query: session_id (offset tracking is per session), from_offset (override),
    peek=1 (do not advance the session's offset), cap_bytes, all=1 (ignore
    offsets and return the whole retained tail), collapse=0 (no collapsing)."""
    src = request.args
    sid = str(src.get("session_id") or "default")
    peek = str(src.get("peek") or "0") not in ("0", "", "false")
    take_all = str(src.get("all") or "0") not in ("0", "", "false")
    collapse = str(src.get("collapse") or "1") not in ("0", "", "false")
    cap = _int_arg("cap_bytes", "0")

    prof = _active_profile_obj()
    if take_all:
        res = _log_sources.read_raw_delta(prof, {}, collapse_repeats=collapse)
    elif src.get("from_offset") is not None:
        off = {s["path"]: _int_arg("from_offset", "0")
               for s in _log_sources.effective_sources(prof)}
        res = _log_sources.read_raw_delta(prof, off, collapse_repeats=collapse)
    else:
        res = _raw_log_delta(sid, advance=not peek)
    if cap > 0:
        _cap_raw_block(res, cap)

    res["what_this_is"] = (
        "The program's OWN output, verbatim. AgentVision did not level it, rank "
        "it, or decide what mattered — that is your job, and a summary that "
        "decided for you is how 180 present failures got reported as 'healthy'. "
        "Consecutive identical lines are collapsed to {line, repeat:N}, which is "
        "lossless; nothing distinct is removed.")
    res["totals"] = {
        "sources": len(res.get("sources") or []),
        "bytes_new": sum(s.get("bytes_new", 0) for s in res.get("sources") or []),
        "lines_total": sum(s.get("lines_total", 0) for s in res.get("sources") or []),
    }
    return jsonify(res), 200


def _write_targets_report() -> dict:
    """Ask the OS where the bridged process is actually writing, and reconcile it
    against the configured log sources."""
    from utils import write_targets as _wt
    prof = _active_profile_obj()
    try:
        pid = int((_get_collector()._reader.process_perf() or {}).get("pid") or 0)
    except Exception:
        pid = 0
    conf = [{"path": s.get("path"), "label": s.get("label")}
            for s in _log_sources.effective_sources(prof)]
    return _wt.reconcile(pid or 0, conf,
                         stale_after_s=_log_sources.STALE_LOG_SECONDS)


def _plan_folder(profile=None) -> Path:
    prof = profile or _active_profile_obj()
    return Path(profile_output_folder(prof, _base_for(prof)))


def _format_coverage(path: str, sample: int = 30) -> dict:
    """Does any adapter specifically understand this file's format?

    Samples real lines and reports the winning adapter. `covered: False` means the
    lines only route to a generic fallback (structural/generic_ts/raw): events are
    still produced and the LEVEL usually survives, but `source` degrades to the
    adapter's own name, which silently breaks error fingerprint grouping and
    av_source_at_error. Surfacing this at catalog time is the difference between
    the agent pinning an adapter and the agent never knowing it needed one.
    """
    out: dict = {}
    try:
        lines = [l for l in _log_sources._first_lines(path, sample) if l.strip()]
    except Exception:
        return out
    if not lines:
        out["sample_lines"] = 0
        return out
    try:
        from connectors import log_adapters as _la
        # detect_adapter takes the LIST of sample lines — that is the unit every
        # adapter's detect() scores over. This loop used to pass one line at a
        # time as a bare STRING, which every adapter then iterated CHARACTER by
        # character: the result was `structural` at 0.02 for literally any input.
        # Measured before the fix: a stdlib-`logging` line that really scores
        # python_logging 1.00, a JSONL line that scores jsonl 1.00 and a Java line
        # that scores trino_airlift 1.00 were ALL reported as `covered: false`.
        # So the catalog told the agent "NO adapter specifically parses this
        # format" about every log it found, and handed over a recipe to write a
        # redundant custom adapter — manufacturing the exact wild-goose chase this
        # coverage probe exists to prevent. Invisible unless you test a format that
        # IS covered, which is why it survived: the rig log was genuinely uncovered.
        winner, best_conf, _scores = _la.detect_adapter(lines[:sample])
        best_name = getattr(winner, "name", str(winner))
        best_conf = float(best_conf)
        fallbacks = getattr(_log_sources, "FALLBACK_ADAPTERS",
                            {"structural", "generic_ts", "raw"})
        out.update({
            "sample_lines": len(lines),
            "detected_adapter": best_name,
            "detect_confidence": round(best_conf, 3),
            "covered": bool(best_name) and best_name not in fallbacks,
            "sample": lines[-1][:160],
            # Stated on EVERY source, covered or not. `covered: true` only means
            # some named adapter claimed the format — not that it claimed it
            # CORRECTLY. A real case: `coreboot_cbmem` (a firmware adapter) took a
            # C engine's "[DEBUG] Sprite.c:32 - msg" at confidence 1.00, buried the
            # file:line inside the message and set source=coreboot. That reports as
            # covered. So the agent must look at the parsed fields, not the verdict.
            "you_must_verify": {
                "why": ("`covered` says an adapter CLAIMED this format, not that it "
                        "parsed it correctly. A wrong adapter at high confidence is "
                        "worse than no adapter: every call still succeeds and the "
                        "fields are quietly wrong."),
                "call": "av_test_adapter(line=<a real line from this file>)",
                "line_to_try": lines[-1][:160],
                "check": [
                    "is_fallback is false",
                    "`level` matches what the line actually says",
                    "`source` is the PROGRAM's module/subsystem — not the "
                    "adapter's own name, and not 'structural'",
                    "the message is the message, with ts/level/source lifted OUT "
                    "of it rather than left inside",
                ],
                "if_wrong": ("write a correct one with av_add_adapter(...); if a "
                             "named adapter already scores as high, pass "
                             "outrank='<that adapter>' or it cannot win the tie"),
            },
        })
        if not out["covered"]:
            out["coverage_warning"] = (
                f"NO adapter specifically parses this format — it falls back to "
                f"'{best_name}'. Level usually survives, but `source` becomes "
                f"'{best_name}' for every line, which breaks "
                f"av_errors_by_fingerprint grouping and av_source_at_error. "
                f"Fix it with av_add_adapter, then pin it in plan.adapters — or "
                f"accept the weaker parse deliberately and say so in rationale.")
            # Hand over the exact body shape with a real line already in it. The
            # spec's fields are TOP-LEVEL; a cold model that guessed a {"spec":{}}
            # wrapper spent 13 attempts on this.
            # Hand over MORE THAN ONE real line, and make the extras binding.
            # Measured on a cold Haiku run: given a single sample it wrote
            # anchor_tokens: [":: spool"] — a fragment of that one line's MESSAGE
            # text. The spec validated at own_score 1.00 and was accepted, then
            # matched 1 of the file's 4 lines; everything else fell to `structural`,
            # and the model read the resulting source="structural" as AgentVision
            # caching or being broken. `also_match` turns those extra lines into
            # test cases the validator enforces, so that spec is rejected with the
            # reason instead of accepted and quietly useless.
            _others = [l for l in lines[:-1] if l.strip() and l != lines[-1]]
            _step = max(1, len(_others) // 3)
            out["how_to_add_an_adapter"] = {
                "call": "av_add_adapter  (POST /adapter/add)",
                "body_is_the_spec": True,
                # The two surfaces take DIFFERENT shapes and only the HTTP one was
                # shown, so an MCP caller handed this recipe had to work out the
                # translation itself — the same "the example confirms a shape the
                # callee does not accept" trap that cost a cold model 13 attempts on
                # this very route, one layer up.
                "if_you_are_calling_the_MCP_TOOL": {
                    "note": ("av_add_adapter takes FLAT arguments, not this nested "
                             "body. Map it like this — same values, different shape."),
                    "mapping": {
                        "name": "name",
                        "extract.regex": "extract_regex",
                        "detect.regex": "detect_regex",
                        "detect.anchor_tokens": "anchor_tokens",
                        "level_map": "level_map",
                        "sample": "sample",
                        "also_match": "also_match",
                        "language": "language",
                    },
                },
                "example_body": {
                    "name": "<short_name>",
                    "language": "any",
                    "detect": {"anchor_tokens": ["<punctuation/structure present in "
                                                 "EVERY line of this format — NOT a "
                                                 "word from one message>"]},
                    "extract": {"regex": "^(?P<level>[A-Z]+) +(?P<source>[\\w.]+) "
                                         "+(?P<message>.*)$"},
                    "level_map": {"ERR": "ERROR", "WRN": "WARN"},
                    "sample": lines[-1][:160],
                    "also_match": [l[:160] for l in _others[::_step][:3]],
                },
                "rules": [
                    "extract.regex uses NAMED groups: ts, level, source, message.",
                    "sample must be a REAL line — the adapter must route it to "
                    "itself or the add is rejected.",
                    "also_match holds MORE real lines from the same file and is "
                    "ENFORCED: your pattern must match every one of them. Keep them "
                    "as given. It is what stops an adapter that only fits the one "
                    "line you looked at.",
                    "Anchor tokens must be structural (a bracket, a separator like "
                    "' :: ', a prefix) and present in EVERY line. A token taken "
                    "from one message body will match that line and nothing else.",
                    "If a named adapter already scores higher on that sample, pass "
                    "outrank='<that adapter>' to take precedence.",
                    "Verify with av_test_adapter(line=<the same line>) — check "
                    "is_fallback is false and `source` is the field you wanted.",
                ],
            }
    except Exception as exc:
        _dbg(f"format coverage probe failed for {path}: {exc}")
    return out


def _bridge_catalog_body(full_detail: bool = False) -> dict:
    """The reviewable option set for the active program."""
    import bridge_plan as _bp
    prof = _active_profile_obj()
    lang = getattr(prof, "language", "") or ""
    # Tool groups + readers come from the existing capability surfaces so the
    # catalog cannot drift from what actually exists.
    try:
        groups = _describe_tool_groups(_tool_catalog_groups(), prof,
                                       full=full_detail)
    except Exception:
        groups = {}
    try:
        readers = _log_sources.list_readers()
    except Exception:
        readers = []
    _emitter_full = bool(full_detail)
    # Declared sources AND undeclared logs found on disk. Reporting only the
    # declared ones made a first bridge blind in the worst way: with nothing
    # declared yet (the definition of a first bridge) the catalog said
    # existing_logs_found: [] even with a log full of FATALs sitting in the
    # project, so the agent was told to CREATE logging for output that already
    # existed. The adapters-vs-emitters choice the catalog insists on cannot be
    # made without this.
    existing = []
    try:
        seen_paths = set()
        for s in _log_sources.effective_sources(prof):
            p = os.path.expanduser(s.get("path") or "")
            seen_paths.add(os.path.normcase(p))
            existing.append({"label": s.get("label"), "path": s.get("path"),
                             "declared": True,
                             "exists": os.path.exists(p),
                             "bytes": _safe_size(p)})
        root = (getattr(prof, "project_root", "") or "").strip()
        if root:
            for s in _log_sources.suggest_log_sources(root):
                p = os.path.expanduser(s.get("path") or "")
                if os.path.normcase(p) in seen_paths:
                    continue
                seen_paths.add(os.path.normcase(p))
                entry = {"label": s.get("label"), "path": s.get("path"),
                         "declared": False, "exists": True,
                         "bytes": _safe_size(p)}
                # Say whether anything actually UNDERSTANDS this format. A file
                # that only routes to a fallback still yields events, but level
                # and source extraction is weak — that is precisely the gap the
                # agent is here to close, so it must be visible before deciding.
                entry.update(_format_coverage(p))
                existing.append(entry)
    except Exception as exc:
        _dbg(f"catalog log discovery failed: {exc}")
    return _bp.catalog(prof, lang, tool_groups=groups, readers=readers,
                       existing_logs=existing, full_detail=_emitter_full)


def _bridge_build_hint(profile, brief: bool = False) -> dict:
    """One-glance "is this program's bridge built?" for /start_here and /status."""
    import bridge_plan as _bp
    try:
        folder = _plan_folder(profile)
        sealed = _bp.is_sealed(folder)
        plan = _bp.read_plan(folder) or {}
    except Exception:
        return {"state": "unknown"}
    if sealed:
        return {
            "state": "BUILT", "ok": True,
            "emitters": plan.get("emitters") or ("(legacy preflight marker)"),
            "what_it_means": ("This program's bridge is built — the first-connection "
                              "review already happened and does not repeat."),
        }
    out = {
        "state": "PROVISIONAL", "ok": False,
        "what_it_means": ("FIRST CONNECTION — the bridge is NOT built and capture "
                          "will be REFUSED. AgentVision will not choose which logs "
                          "and tools this program needs; that is your call."),
        "do_this_now": "av_bridge_catalog()  ->  av_bridge_commit(plan=...)",
    }
    if brief:
        # start_here EMBEDS this block and has a hard size budget, being the
        # first call of every session. The guidance below is for the caller who
        # asked /bridge/status directly, i.e. someone about to write a plan.
        return out
    out.update({
        # The catalog is deliberately BIG (it is read once per program and decides
        # what every later call can see). But a cold Haiku reported the full form
        # "would have been unusable" and that it only knew a short form existed
        # because it had been told out of band — nothing advertised it. Say so
        # here, which is the call immediately before it.
        "about_the_catalog": (
            "it is LARGE on purpose — read once per program, and it decides what "
            "every later call can possibly see. If you need the short form first, "
            "av_bridge_catalog(detail='compact') (HTTP: ?detail=compact) returns "
            "the same options and the SAME catalog_token, so asking for either "
            "does not invalidate the plan you are about to commit."),
        "plan_gotchas": [
            "wrap it: {\"plan\": {...}} — sending the fields at the top level is "
            "rejected as PLAN_NOT_WRAPPED",
            "a plan with no emitters AND no adapter pins is rejected as "
            "BRIDGE_WOULD_READ_NOTHING — that is not a complaint about you, it "
            "means the bridge as described would have no source to read, so pick "
            "at least one emitter or pin an adapter to a log that already exists",
        ],
    })
    return out


@app.route("/bridge/status", methods=["GET"])
def bridge_status_route():
    """Is this program's bridge BUILT, or still provisional?

    A program AgentVision has never seen starts PROVISIONAL: capture and emitter
    installation are refused until the agent reviews the catalog and commits a
    plan. This is deliberately a hard gate — AgentVision would otherwise scaffold
    the same fixed emitter set for a web server and a GPU emulator, and report
    success either way, which makes a wrong bridge indistinguishable from a right
    one.

    It fires ONCE per program. After the plan is committed the logging is built in
    and every later connection proceeds immediately."""
    import bridge_plan as _bp
    prof = _active_profile_obj()
    st = _bp.status(_plan_folder(prof), prof)
    st["what_this_is"] = (
        "The first-connection contract: AgentVision supplies the tools, YOU decide "
        "which of them this specific program needs.")
    return jsonify(st), 200


@app.route("/bridge/report", methods=["GET"])
def bridge_report_route():
    """WHAT IS BUILT INTO THIS PROGRAM — the per-program record.

    Every program gets a different bridge: its own emitters, its own adapters
    (possibly a custom one written for its log format), its own tool relevance.
    This is that state in one place, so neither the user nor the agent has to
    reconstruct it from files: which emitters were installed and WHY, which
    adapter actually resolves for each log source, whether those sources are
    alive, and what the agent decided when it planned the bridge.
    """
    import bridge_plan as _bp
    prof = _active_profile_obj()
    folder = _plan_folder(prof)
    plan = _bp.read_plan(folder) or {}
    st = _bp.status(folder, prof)

    # Resolve the adapter that will ACTUALLY be used per source — the declared
    # value may be "auto", and what auto picks is the thing worth showing.
    sources = []
    try:
        for s in _log_sources.effective_sources(prof):
            path = os.path.expanduser(s.get("path") or "")
            declared = s.get("adapter", "auto")
            resolved, conf, unresolved = declared, None, ""
            if os.path.exists(path):
                try:
                    ev = _log_sources.read_raw_delta(
                        prof, {}, collapse_repeats=False)
                    lines = []
                    for src in ev.get("sources") or []:
                        if src.get("path") == s.get("path"):
                            lines = [x if isinstance(x, str) else x.get("line", "")
                                     for x in (src.get("lines") or [])][:40]
                    if declared == "auto" and lines:
                        a, c, _ = _log_adapters.detect_adapter(lines)
                        resolved, conf = a.name, round(c, 2)
                except Exception:
                    pass
            # A pinned name that is not registered resolves to `raw` at read time.
            # Echoing the declared name here made that invisible on the one surface
            # whose job is to show what the bridge actually reads.
            if declared != "auto" and not _log_adapters.get_adapter(declared):
                resolved = "raw"
                unresolved = declared
            try:
                age = round(time.time() - os.path.getmtime(path), 1)
            except Exception:
                age = None
            sources.append({
                "label": s.get("label"), "path": s.get("path"),
                "exists": os.path.exists(path), "bytes": _safe_size(path),
                "adapter_declared": declared, "adapter_resolved": resolved,
                "detect_confidence": conf,
                **({"adapter_warning":
                    f"the pinned adapter {unresolved!r} is NOT registered — this "
                    f"source is being parsed by the `raw` fallback, so `source` and "
                    f"`level` extraction are lost. Add it with av_add_adapter, or "
                    f"re-pin with av_bridge_commit(replan=True)."} if unresolved
                   else {}),
                "last_write_age_s": age,
                "stale": (age is not None and age > _log_sources.STALE_LOG_SECONDS),
            })
    except Exception as exc:
        _dbg(f"bridge report sources failed: {exc}")

    try:
        groups = _tool_catalog_groups()
    except Exception:
        groups = {}

    return jsonify({
        "what_this_is": ("What AgentVision has actually built into THIS program. "
                         "Each program gets its own emitters, adapters and tool "
                         "relevance — this is the per-program record, not a global "
                         "capability list."),
        "program": getattr(prof, "display_name", "") or getattr(prof, "name", ""),
        "profile": getattr(prof, "name", ""),
        "project_root": getattr(prof, "project_root", ""),
        "language": getattr(prof, "language", "") or "unknown",
        "state": st["state"],
        "sealed": st["sealed"],
        "planned_by": plan.get("decided_by") or ("legacy marker" if st["sealed"]
                                                 else None),
        "sealed_at": plan.get("sealed_at"),
        "emitters_built": plan.get("emitters") or [],
        "why": plan.get("why") or {},
        "rationale": plan.get("rationale") or "",
        "visual_capture": plan.get("visual_capture"),
        "capture": plan.get("capture") or {},
        "log_sources": sources,
        "mcp_tool_groups": groups,
        "mcp_tool_count": sum(len(v) for v in groups.values()),
        # The tool half of the plan. Distinct from mcp_tool_groups above, which is
        # every tool that EXISTS: this is the subset the agent judged worth calling
        # on this program, plus what it explicitly ruled out and why. Empty on a
        # bridge sealed before tool review existed (or by the legacy marker).
        "tools_chosen": ((plan.get("tools") or {}).get("primary") or []),
        "tools_not_relevant": ((plan.get("tools") or {}).get("not_relevant") or {}),
        "tools_note": ((plan.get("tools") or {}).get("note") or ""),
        "tools_reviewed": bool(plan.get("tools")),
        "adapters_available": len(_log_adapters.REGISTRY),
        "next": ("av_bridge_catalog() — this program is not planned yet"
                 if not st["sealed"] else "av_capture_start()"),
    }), 200


@app.route("/bridge/catalog", methods=["GET"])
def bridge_catalog_route():
    """EVERYTHING available to build into this program — emitters, adapters,
    readers, MCP tool groups, capture settings — with nothing pre-selected.

    Returns a `catalog_token`. /bridge/commit REJECTS a plan without a matching
    token, so "review the options before building" is a mechanical precondition
    rather than an instruction that can be skipped.

    FULL by default; `?detail=compact` for the short form.

    This default was reversed on purpose. Compact was chosen when the full form
    measured ~51 KB of a ~59 KB catalog and a cold model reported having to read
    all of it to make one decision. But the cost is paid ONCE PER PROGRAM, EVER —
    the bridge is not rebuilt on restart — and it buys the one decision that
    determines what every later call can possibly see. Trading detail for tokens
    here is trading the quality of an irreversible choice against a rounding error,
    and the models this is written for are exactly the ones that need the detail.
    Token discipline still governs the steady-state tools, which are called in a
    loop for as long as the program runs."""
    _d = str(request.args.get("detail", "")).lower()
    full = _d not in ("compact", "short", "0", "false", "min")
    return jsonify(_bridge_catalog_body(full_detail=full)), 200


def _wire_plan_sources(prof, plan: dict, cat: dict, built: dict) -> list:
    """Register the log sources this plan actually DECIDED about. Returns the
    final source list.

    Two separate holes lived here, and they are the same hole seen twice: the
    catalog was taught to report undeclared logs already sitting in the project,
    and plan.adapters was taught to reject a mis-keyed pin — but nothing connected
    either of those to what the bridge ends up reading. Measured on this rig:

      * a plan with adapters={"widgetd": "widgetd_tilde"} (a label the catalog
        itself advertised in adapter_pin_labels, and an adapter the agent had just
        written for that exact format) sealed ok=true with
        "registered log sources: events -> jsonl, text -> auto" and the pin
        DISCARDED — the FATAL-filled widgetd.log was never registered at all;
      * the "already logs well, emitters: []" plan — the very conclusion the
        catalog's new discovery exists to make possible — sealed BUILT with
        log_sources == [], a flight recorder wired to nothing.

    Both because the wiring lived inside `if plan.get("emitters")` and hardcoded
    the two emitter sinks as the only wireable labels. So: run for every plan, and
    treat a pin on a discovered log as the instruction it plainly is.

    A discovered log the plan did NOT pin is deliberately left unwired — deciding
    to read a file is the agent's call, not AgentVision's — but it is reported in
    built.discovered_not_wired rather than passed over in silence.
    """
    root = (getattr(prof, "project_root", "") or "").strip()
    plan_adapters = {str(k): str(v).strip()
                     for k, v in (plan.get("adapters") or {}).items()
                     if str(v).strip()}
    srcs = list(getattr(prof, "log_sources", None) or [])
    seen = {os.path.normcase(os.path.expanduser(s.get("path") or "")) for s in srcs}

    # (label, path, default_adapter, is_emitter_sink)
    cands: list[tuple] = []
    if root:
        av_dir = Path(root) / "agentvision"
        cands.append(("events", av_dir / "actions.jsonl", "jsonl", True))
        cands.append(("text", av_dir / "log.txt", "auto", True))
    for e in (cat.get("existing_logs_found") or []):
        if e.get("declared") or not e.get("exists"):
            continue
        label, p = str(e.get("label") or "").strip(), str(e.get("path") or "")
        if label and p:
            cands.append((label, Path(p),
                          "jsonl" if p.endswith(".jsonl") else "auto", False))

    added: list[str] = []
    repinned: list[str] = []
    not_wired: list[dict] = []
    honoured: set = set()

    # RE-PIN an already-declared source. A replan is exactly when the agent has
    # learned enough to choose a better parser — it is the case the `abyss` profile
    # (text -> abyssengine) documents. Without this the pin was dropped: the source
    # is already declared, so the catalog no longer lists it as an undeclared log
    # and it never entered the candidate list at all. Measured on a cold Haiku run:
    # it wrote a custom adapter, replanned with adapters={"mailerd": "mailerd"},
    # was told "no log source for them exists" (false — it was registered and
    # visible in /bridge/report), concluded AgentVision could not do this, and
    # stopped. A wrong answer that reads as a limitation costs more than an error.
    for s in srcs:
        lbl = str(s.get("label") or "")
        if lbl and lbl in plan_adapters:
            honoured.add(lbl)
            want_ad = plan_adapters[lbl]
            if str(s.get("adapter") or "") != want_ad:
                repinned.append(f"{lbl}: {s.get('adapter')} -> {want_ad}")
                s["adapter"] = want_ad

    for label, path, default_adapter, is_sink in cands:
        key = os.path.normcase(os.path.expanduser(str(path)))
        if key in seen:
            if label in plan_adapters:
                honoured.add(label)      # already registered; pin is not lost
            continue
        # "stdout" is a pseudo-label for what a run_wrapper tees, so it may stand
        # in for the emitter sinks — never for a file the project writes itself.
        pin = plan_adapters.get(label) or (plan_adapters.get("stdout")
                                          if is_sink else "")
        if not is_sink and label not in plan_adapters:
            not_wired.append({
                "label": label, "path": str(path),
                "why": ("this log already exists in the project but the plan did "
                        "not pin an adapter for it, and AgentVision does not "
                        "decide on your behalf which files to read"),
                "to_read_it": f'av_bridge_commit(replan=True) with adapters={{"{label}": '
                              f'"auto"}} (or a specific adapter name)'})
            continue
        try:
            if not path.exists():
                continue
        except OSError:
            continue
        adapter = pin or default_adapter
        srcs.append({"path": str(path), "adapter": adapter, "label": label})
        seen.add(key)
        added.append(f"{label} -> {adapter}")
        if label in plan_adapters:
            honoured.add(label)

    if plan_adapters.get("stdout") and any(x for x in added):
        honoured.add("stdout")
    unapplied = sorted(set(plan_adapters) - honoured)
    if unapplied:
        # A pin that matched nothing must be visible. Silently dropping it is the
        # original defect; the label validator only catches keys that are not
        # labels at all, not a valid label whose file does not exist.
        built["adapters_unapplied"] = {
            "labels": unapplied,
            "why": ("these labels are valid but no log source for them exists, so "
                    "the pin had nothing to attach to (an emitter sink is only "
                    "created when that emitter is selected AND the install runs)"),
            "check": "av_bridge_report() lists every source that IS wired",
        }
    if not_wired:
        built["discovered_not_wired"] = not_wired
    if repinned:
        built["adapters_repinned"] = repinned

    if added or repinned:
        profs = load_profiles()
        tgt = profs.get(getattr(prof, "name", ""))
        if tgt is not None:
            tgt.log_sources = srcs
            # The scan knows the language even when the profile does not;
            # recording it stops the report saying "unknown" about a program we
            # just fingerprinted.
            detected = ((cat.get("code_evidence") or {}).get("primary_language")
                        or "")
            if detected and not (getattr(tgt, "language", "") or ""):
                tgt.language = detected
                built["actions"].append(f"recorded language={detected}")
            try:
                save_profiles({k: v for k, v in profs.items()
                               if k not in BUILTIN_PROFILES
                               or k == getattr(prof, "name", "")})
            except Exception as exc:
                _warn(f"log-source registration not persisted: {exc}")
                built["actions"].append(f"NOT saved to profiles.json: {exc}")
            globals()["_collector"] = None
            _get_collector()               # re-init against the new sources
            if added:
                built["actions"].append("registered log sources: "
                                        + ", ".join(added))
            if repinned:
                built["actions"].append("re-pinned adapters: " + ", ".join(repinned))
    return srcs


#: Rationale wording that makes "this bridge reads no logs at all" a DECISION
#: rather than an accident. Mirrors the existing emitters==[] convention, which
#: requires the rationale to say "already"/"no log", so the idiom is one a model
#: only has to learn once.
_VISUAL_ONLY_PHRASES = ("visual only", "visual-only", "no logs", "no log file",
                        "screenshot only", "screenshots only", "frames only")


def _no_readable_source_error(prof, cat: dict, plan: dict) -> dict | None:
    """Refuse to seal a bridge that would read NOTHING, unless that is declared.

    "Sealed BUILT with log_sources == []" is the worst possible end state: every
    later tool answers emptily and the state surface says the bridge is fine. It
    was reachable by the most sensible-looking plan available — emitters: [] with
    the rationale "already logs well" — because the pin that was supposed to
    attach the existing log went nowhere. Wiring fixes the common case; this
    stops the remaining silent version of it.
    """
    discovered = [e for e in (cat.get("existing_logs_found") or [])
                  if not e.get("declared") and e.get("exists")]
    if str(plan.get("visual_capture")).lower() == "true" and any(
            p in str(plan.get("rationale") or "").lower()
            for p in _VISUAL_ONLY_PHRASES):
        return None
    return {
        "ok": False, "sealed": False, "error": "BRIDGE_WOULD_READ_NOTHING",
        "errors": [
            "This plan seals a bridge with NO log sources at all: no emitter was "
            "installed and no existing log was pinned, so every log-side tool "
            "(av_log_normalized, av_diagnose, av_error_moment, av_search) would "
            "return empty for the rest of this program's life while the state "
            "surface reported BUILT.",
        ],
        "logs_you_could_pin": [
            {"label": e.get("label"), "path": e.get("path"),
             "bytes": e.get("bytes"), "covered_by": e.get("detected_adapter"),
             "pin_it_with": f'adapters={{"{e.get("label")}": "auto"}}'}
            for e in discovered] or None,
        "fix": ("Pick one: (a) pin an existing log — adapters={\"<label from "
                "catalog.adapter_pin_labels>\": \"auto\"}; (b) select an emitter "
                "so AgentVision has something to read; or (c) if this program "
                "genuinely has no readable log and you want frames only, set "
                "visual_capture=true and say \"visual only\" in the rationale — "
                "then this is recorded as your decision instead of an accident."),
        "next": "av_bridge_catalog() — existing_logs_found lists what is already there",
    }


@app.route("/bridge/commit", methods=["POST"])
def bridge_commit_route():
    """Commit the agent's plan and BUILD the bridge — the selected emitters only.

    Body: {plan: {catalog_token, emitters:[ids], adapters:{label:adapter},
    capture:{interval_seconds}, visual_capture: bool, rationale: str},
    replan: bool}

    Rejects a plan with a missing/stale catalog_token, an absent `emitters` key,
    or no rationale — each of those would seal a bridge nobody actually chose."""
    import bridge_plan as _bp
    body = request.get_json(silent=True) or {}
    plan = body.get("plan") or {}
    replan = str(body.get("replan") or "0") not in ("0", "", "false", "False")

    # A caller that sends the plan's FIELDS at the top level instead of nested
    # under "plan" used to get four confident "field is missing" errors for
    # fields it had demonstrably sent — measured on a cold model, which then had
    # to guess-and-check the envelope. Diagnose the envelope before validating
    # the contents, because "catalog_token is missing" is a lie in that case and
    # a lie is worse than a vague error.
    _PLAN_KEYS = {"catalog_token", "emitters", "why", "rationale", "tools",
                  "adapters", "capture", "visual_capture"}
    if not plan and (_PLAN_KEYS & set(body)):
        stray = sorted(_PLAN_KEYS & set(body))
        return jsonify({
            "ok": False, "sealed": False,
            "error": "PLAN_NOT_WRAPPED",
            "errors": [
                "The plan fields were sent at the TOP LEVEL of the request body, "
                "but this route expects them nested under a \"plan\" key. Nothing "
                "is missing — the envelope is wrong.",
                f"Found at top level: {stray}",
                'Send: {"plan": {"catalog_token": "...", "emitters": [...], '
                '"why": {...}, "rationale": "...", "tools": {...}}, '
                '"replan": false}',
            ],
            "hint": ("If you are calling the MCP tool av_bridge_commit(plan={...}) "
                     "this wrapping is done for you — pass your object as the "
                     "`plan` argument and do not wrap it yourself."),
        }), 400

    prof = _active_profile_obj()
    folder = _plan_folder(prof)
    existing = _bp.read_plan(folder)
    if existing and existing.get("sealed") and not replan:
        return jsonify({
            "ok": False, "already_sealed": True, "plan": existing,
            "note": ("This program's bridge is already built — the gate is "
                     "first-connection only. Pass replan=true to deliberately "
                     "re-decide."),
        }), 200

    cat = _bridge_catalog_body()
    expected = cat.get("catalog_token")
    # Pass the actual menu so the anti-blanket rule is proportional to it. With an
    # absolute threshold the rule was unreachable: no language offers 6 emitters.
    try:
        _known_adapters = [a.get("name") for a in _log_adapters.list_adapters()]
    except Exception:
        _known_adapters = []
    ok, errs = _bp.validate_plan(
        plan, expected,
        offered=[e.get("id") for e in (cat.get("emitters_available") or [])],
        known_labels=cat.get("adapter_pin_labels") or [],
        # A pin naming an adapter that does not exist used to validate clean and
        # then silently degrade to `raw` at read time.
        known_adapters=_known_adapters,
        # Labels whose format AgentVision SAMPLED and could not specifically
        # parse. The agent must either write an adapter for them or say it accepts
        # the weaker parse — because a fallback keeps every call succeeding while
        # flattening `source` to the fallback's own name.
        uncovered_labels=[e.get("label") for e in
                          (cat.get("existing_logs_found") or [])
                          if e.get("covered") is False and e.get("label")])
    if not ok:
        return jsonify({
            "ok": False, "sealed": False, "errors": errs,
            "catalog_token_expected": expected,
            "next": "av_bridge_catalog() — then commit with its token",
        }), 400

    # The plan's emitter list is recorded verbatim, but install_into_project()
    # scaffolds ONE language-appropriate emitter rather than one artifact per id
    # (several ids are facets of the same emitter — see _emitter_options'
    # `builds_as`). Reporting "requested" separately from what the installer
    # actually produced keeps that distinction visible instead of implying a
    # per-id build that does not happen.
    built: dict = {"emitters_requested": plan.get("emitters") or [], "actions": [],
                   "emitter_build_note": (
                       "On in-process-hook languages (python) your selection is "
                       "ENFORCED: it becomes AGENTVISION_HOOKS and only those hooks "
                       "arm at runtime — check install.selection_enforced, and the "
                       "program's own av.bootstrap.start record reports "
                       "hooks_requested vs hooks_armed. On config/tee languages "
                       "(java, .NET, c/c++, go, rust, shell) there is ONE artifact "
                       "whatever you select, so the selection is recorded as intent "
                       "rather than built per id.")}
    try:
        root = (getattr(prof, "project_root", "") or "").strip()
        if plan.get("emitters") and root and os.path.isdir(root):
            from installer import install_into_project
            rep = install_into_project(
                root, profile_name=getattr(prof, "name", ""),
                language=(getattr(prof, "language", "") or ""),
                # The agent's selection now GATES what arms at runtime on
                # in-process-hook languages, instead of being recorded and ignored.
                emitters=list(plan.get("emitters") or []))
            # InstallReport is an object, not a dict — to_dict()/summary() where
            # available, else a plain string, so a shape change here can never
            # swallow the one thing the caller needs: did it build?
            try:
                built["install"] = rep.to_dict()
            except Exception:
                built["install"] = {"summary": str(getattr(rep, "summary", lambda: rep)())}
            built["actions"].append(f"scaffolded emitters into {root}")
        elif not plan.get("emitters"):
            built["actions"].append("no emitters requested — nothing written to the "
                                    "target program (per plan.rationale)")
        else:
            built["actions"].append(f"project_root {root!r} is not a directory — "
                                    f"no emitter files written")
    except Exception as exc:
        built["install_error"] = str(exc)

    # WIRE THE LOG SOURCES THIS PLAN DECIDED ABOUT. Runs for EVERY plan, not just
    # ones that install emitters — see _wire_plan_sources for why that matters.
    final_sources: list = list(getattr(prof, "log_sources", None) or [])
    try:
        final_sources = _wire_plan_sources(prof, plan, cat, built)
    except Exception as exc:
        built["wire_error"] = str(exc)

    # A sealed bridge that reads nothing is worse than an unsealed one, because
    # the gate never fires again to correct it. Refuse unless it is declared.
    if not final_sources:
        problem = _no_readable_source_error(prof, cat, plan)
        if problem:
            problem["built_so_far"] = built
            return jsonify(problem), 400
        built["actions"].append("no log sources — visual-only bridge, per "
                                "plan.rationale")

    rec = _bp.write_plan(folder, plan, catalog_token_value=expected, built=built)

    # The plan SUPERSEDES the old coverage-only preflight. Without this the agent
    # clears the bridge gate and immediately hits a second gate telling it to run
    # av_preflight — two sequential blocks for one first connection, which
    # contradicts "decide once, then it just works". The plan already names the
    # adapters deliberately, which is strictly more than preflight was checking.
    try:
        verdict = _run_preflight(prof, {})
        _preflight_marker_write(prof, verdict, accepted_gaps=True)
        built["actions"].append("preflight marker written — the plan supersedes "
                                "the coverage-only check")
    except Exception as exc:
        _dbg(f"could not write preflight marker after commit: {exc}")

    _info(f"Bridge SEALED for '{getattr(prof, 'name', '?')}' — "
          f"emitters={rec['emitters']}")
    return jsonify({
        "ok": True, "sealed": True, "plan": rec, "built": built,
        "note": ("Bridge built. The gate will not fire again for this program — "
                 "later connections proceed immediately."),
        "next": "av_capture_start()",
    }), 200


@app.route("/log/where", methods=["GET"])
def log_where_route():
    """WHERE IS THE PROGRAM ACTUALLY WRITING? Asked of the OS, not of config.

    A configured log path is a guess; this checks the descriptors the process
    really holds open. It answers the failure that a log reader cannot detect on
    its own — reading a file the program stopped writing to, and believing it.

    Cost of not having this, measured here: an analysis of "180 GPU present
    failures" was run against a log last written 23 hours earlier, because the
    emulator's own sink wrote to a DIFFERENT directory. `tail -f` on the wrong
    file shows stale bytes forever and never complains.

    Returns: missing_from_config (the process writes here, nothing reads it),
    not_written_by_proc (configured, but this pid does not hold it open), stale
    (open but nothing written lately), and output_destination — including
    stdout/stderr going to a terminal, a pipe, or /dev/null, which no configured
    path can reveal."""
    rep = _write_targets_report()
    rep["what_this_is"] = (
        "Where the bridged process is writing, according to the operating "
        "system. Compare it against what the profile declared — a mismatch means "
        "you are reading the wrong file, which looks exactly like a quiet program.")
    if rep.get("available") and not rep.get("ok"):
        rep["next"] = ("Fix the profile's log_sources to the paths under "
                       "missing_from_config, or launch the program so its output "
                       "lands where the profile expects.")
    return jsonify(rep), 200


@app.route("/log/entities", methods=["GET"])
def log_entities_route():
    """ROLE INDEX over the raw log — treat lines as records, not sentences.

    For every guest address/handle in the log, which KEYS did it appear under and
    how many times. Nothing is summarized or ranked by importance; these are
    counts of what the program itself printed.

    This exists because the hardest bug on this project was solved by noticing
    one thing in raw text: an address appeared as `tex0` and `src` but NEVER as
    `target`, so the buffer being presented was one nothing ever rendered into.
    `never=target` asks that question directly instead of relying on someone
    spotting it across 45,000 lines.

    Query: never=<key>   addresses that never appear under this key (the good one)
           address=<hex> roles for one address (any of 0x08040.., 0x8040.., 8040..)
           key=<k>       restrict the index to these keys (repeatable, csv)
           limit         max rows (default 25)
    """
    from connectors import log_fields as _lf
    src = request.args
    prof = _active_profile_obj()
    limit = max(1, min(_int_arg("limit", "25"), 200))

    raw = _log_sources.read_raw_delta(prof, {})     # whole retained tail
    lines: list = []
    for s in raw.get("sources") or []:
        lines.extend(s.get("lines") or [])

    keys = {k.strip() for k in (src.get("key") or "").split(",") if k.strip()}
    idx = _lf.build_role_index(lines, key_filter=keys or None)

    out = {
        "what_this_is": (
            "Every address the log mentions, and which field names it appeared "
            "under. AgentVision did not decide what matters here — it counted "
            "what your program printed. Ask never=<key> for the question that "
            "finds a value used in every role EXCEPT the one you expect."),
        "lines_scanned": idx["lines_scanned"],
        "distinct_addresses": len(idx["addresses"]),
        "keys_seen": dict(sorted(idx["keys"].items(), key=lambda kv: -kv[1])),
        "sources": [{"label": s.get("label"), "path": s.get("path"),
                     "lines_total": s.get("lines_total"),
                     "stale": s.get("stale"),
                     "last_write_age_s": s.get("last_write_age_s")}
                    for s in raw.get("sources") or []],
    }

    want_addr = (src.get("address") or "").strip()
    never = (src.get("never") or "").strip()
    if want_addr:
        a = _lf.normalize_address(want_addr)
        rec = (idx["addresses"] or {}).get(a)
        out["address"] = a
        out["found"] = rec is not None
        out["roles"] = (rec or {}).get("roles") or {}
        out["total"] = (rec or {}).get("total", 0)
        out["first_line"] = (rec or {}).get("first_line")
        out["last_line"] = (rec or {}).get("last_line")
    elif never:
        rows = _lf.roles_never(idx, never)
        out["query"] = f"addresses that NEVER appear under '{never}'"
        out["count"] = len(rows)
        out["rows"] = rows[:limit]
        out["how_to_read"] = (
            f"A row with a high `total` and no '{never}' role is a value your "
            f"program uses constantly in other positions but never once as "
            f"'{never}'. Whether that is a bug is your call — the counts are the "
            f"evidence, not a verdict.")
    else:
        rows = sorted(idx["addresses"].items(), key=lambda kv: -kv[1]["total"])
        out["rows"] = [{"address": a, **r} for a, r in rows[:limit]]
        out["next"] = ("/log/entities?never=<key> is the high-value query; "
                       "/log/entities?address=0x... for one value's roles")
    return jsonify(out), 200


@app.route("/retention", methods=["GET", "POST"])
def retention_route():
    """DISK BUDGET + the examine-before-delete contract.

    GET returns the whole picture: how much of the byte budget is used, which
    frames are still waiting on the agent's eyes, what has been evicted, and
    whether anything was ever lost unexamined.

    POST reconfigures it live — body/query: mode (off|errors|changes|all),
    budget (e.g. "5GB"), hold_seconds. `mode` is the important dial: `errors`
    (default) only asks the agent to look at frames aligned with a failure, while
    `all` pushes every frame so the agent gets video-like continuity. Pair `all`
    with a slower capture interval — an agent does not need 24 fps to perceive
    motion, so the interval is the real cost lever.
    """
    src = request.args if request.method == "GET" else (
        request.get_json(silent=True) or request.args)
    changed = {}
    if request.method == "POST":
        kw = {}
        if src.get("mode") is not None:
            kw["mode"] = str(src.get("mode"))
        if src.get("budget") is not None:
            kw["budget_bytes"] = _ret.parse_bytes(src.get("budget"),
                                                  _ret.LEDGER.budget_bytes)
        if src.get("hold_seconds") is not None:
            # NB: not _safe_float() — that one clamps to [0,1] for fractions.
            try:
                kw["hold_s"] = max(0.0, float(src.get("hold_seconds")))
            except (TypeError, ValueError):
                return _json_error(400, "hold_seconds must be a number")
        if not kw:
            return _json_error(400, "nothing to change; send mode, budget "
                                    "and/or hold_seconds")
        changed = _ret.LEDGER.configure(**kw)

    rep = _ret.LEDGER.report()
    rep["what_this_is"] = (
        "Screenshots are only worth taking if the agent hears about them. A "
        "frame the policy flags for examination is NOT deleted until it has "
        "been examined (or its hold expires); disk is bounded by bytes, not by "
        "a clock. Frame descriptors and thumbnails are kept permanently — only "
        "full-resolution pixels are ever reclaimed, and interesting ones are "
        "compressed into the archive on the way out.")
    rep["awaiting"] = _ret.LEDGER.awaiting(limit=25)
    rep["free_disk_bytes"] = _free_disk_bytes()
    rep["env"] = {
        "AGENTVISION_DISK_BUDGET": "total bytes on disk, e.g. 5GB (default 5GB)",
        "AGENTVISION_EXAMINE": "off | errors | changes | all (default errors)",
        "AGENTVISION_EXAMINE_HOLD_S": "seconds a flagged frame is held (900)",
        "AGENTVISION_MIN_FREE_DISK": "free-disk floor that forces eviction (2GB)",
        "AGENTVISION_ARCHIVE": "0 disables the compressed archive",
    }
    rep["next"] = ("av_frames_awaiting() to see what needs eyes · "
                   "av_examine_ack(seqs=[...]) to release them")
    if changed:
        rep["changed"] = changed
    return jsonify(rep), 200


@app.route("/frames_awaiting", methods=["GET"])
def frames_awaiting_route():
    """The frames AgentVision is holding FOR YOU, most urgent first.

    Each row says why it was flagged and how long before its hold expires. Work
    them cheapest-first: av_frame_json(seq) is usually enough, av_frame_region
    (seq) when you need the pixels that changed, av_get_frame(seq) only as a last
    resort. Reading any of those releases the frame automatically."""
    limit = max(1, min(_int_arg("limit", "25"), 500))
    rows = _ret.LEDGER.awaiting(limit=limit)
    total = len(_ret.LEDGER.awaiting(limit=100000))
    return jsonify({
        "what_this_is": ("Captured frames flagged for your eyes that you have "
                         "not looked at yet. They are protected from eviction "
                         "until you do, or until the hold expires."),
        "mode": _ret.LEDGER.mode,
        "count": len(rows), "total_awaiting": total,
        "truncated": total > len(rows),
        "rows": rows,
        "push_line": _ret.push_sentence(rows),
        "how_to_discharge": {
            "1_cheapest": "av_frame_json(seq) — JSON descriptor, no pixels",
            "2_thumb":    "av_frame_json(seq, thumbnail=True)",
            "3_region":   "av_frame_region(seq) — only the changed pixels",
            "4_full":     "av_get_frame(seq) — the whole image, last resort",
            "5_bulk":     ("av_examine_ack(seqs=[...]) — release without "
                           "looking, when the JSON already told you enough"),
        },
        "note": ("Failure-aligned frames are NOT released by av_visual_changes "
                 "alone — a one-line summary row is not an inspection."),
    }), 200


@app.route("/examine_ack", methods=["POST"])
def examine_ack_route():
    """Release frames you are done with, so their pixels can be reclaimed.

    Body: seqs=[1,2,3] (or seqs="1,2,3"), or all=1 to release everything
    currently awaiting. Use this when the JSON already answered your question and
    you do not need the pixels — it is the honest way to keep the disk bounded
    without the recorder guessing on your behalf."""
    body = request.get_json(silent=True) or request.args
    raw = body.get("seqs")
    take_all = str(body.get("all") or "0") not in ("0", "", "false")
    seqs: list[int] = []
    if take_all:
        seqs = [r["seq"] for r in _ret.LEDGER.awaiting(limit=100000)]
    elif isinstance(raw, (list, tuple)):
        seqs = [int(x) for x in raw if str(x).strip().lstrip("-").isdigit()]
    elif raw is not None:
        for part in str(raw).replace(" ", "").split(","):
            if part.lstrip("-").isdigit():
                seqs.append(int(part))
    # all=1 with an empty queue is a successful no-op, not a bad request; only a
    # call that named nothing at all is an error.
    if not seqs and not take_all:
        return _json_error(400, "send seqs=[...] or all=1")
    n = _ret.LEDGER.mark_examined_many(seqs, "ack")
    return jsonify({
        "ok": True, "acked": n, "requested": len(seqs),
        "already_examined_or_unknown": len(seqs) - n,
        "still_awaiting": len(_ret.LEDGER.awaiting(limit=100000)),
        "note": ("Released frames become ordinary eviction candidates; their "
                 "descriptors and thumbnails are kept regardless."),
    }), 200


@app.route("/visual_backfill", methods=["POST", "GET"])
def visual_backfill():
    """Compute visual analysis for frames captured BEFORE this feature existed.

    After upgrading, previously captured frames carry no perceptual hash or change
    score, so av_visual_changes cannot see them. This walks the most recent
    `limit` frames in sequence order, computes dhash/change_score/changed_bbox
    from the PNGs still on disk, updates the in-memory frames and (optionally)
    their JSON sidecars, and reports the measured cost.

    Query: limit (default 200, cap 5000), write_sidecars (0/1, default 0),
           force (0/1 — redo frames that already have analysis)."""
    from utils import visual_engine as ve
    limit = max(1, min(_int_arg("limit", "200"), 5000))
    write_side = request.args.get("write_sidecars", "0") not in ("0", "", "false")
    force = request.args.get("force", "0") not in ("0", "", "false")

    frames = _frames_sorted()[-limit:]
    t0 = time.perf_counter()
    done = skipped = missing = failed = 0
    prev_grid, prev_dhash = None, ""
    for f in frames:
        seq = f.get("sequence")
        img = f.get("annotated_image") or ""
        if not img or not os.path.exists(img):
            missing += 1
            prev_grid, prev_dhash = None, ""     # gap: don't diff across it
            continue
        if _visual_of(f) and not force:
            skipped += 1
            g = _grid_for_seq(seq)
            if g:
                prev_grid = g
                prev_dhash = _visual_of(f).get("dhash") or ""
            continue
        res = ve.analyze(img, prev_grid=prev_grid, prev_dhash=prev_dhash)
        if not res.get("assessed"):
            failed += 1
            continue
        grid = res.pop("_grid", b"")
        res["backfilled"] = True
        with _lock:
            live = _frames.get(seq)
            if live is not None:
                cm = live.setdefault("capture_meta", {})
                cm["visual"] = res
                if grid:
                    _grids[seq] = grid
            _record_visual_stats(res)
        if write_side:
            side = f.get("json_sidecar") or ""
            try:
                if side and os.path.exists(side):
                    data = json.loads(Path(side).read_text())
                    data.setdefault("capture_meta", {})["visual"] = res
                    Path(side).write_text(json.dumps(data))
            except Exception as exc:
                _dbg(f"backfill sidecar write failed for {seq}: {exc}")
        prev_grid, prev_dhash = grid, res.get("dhash") or ""
        done += 1
    dt = (time.perf_counter() - t0) * 1000.0
    return jsonify({
        "what_this_is": ("Backfilled perceptual hashes + change scores onto frames "
                         "captured before the visual engine existed, so "
                         "av_visual_changes can review them."),
        "frames_considered": len(frames),
        "analyzed": done, "already_had_analysis": skipped,
        "image_missing": missing, "failed": failed,
        "sidecars_updated": bool(write_side),
        "elapsed_ms": round(dt, 1),
        "ms_per_frame": round(dt / done, 2) if done else None,
        "next": "av_visual_changes()",
    }), 200


@app.route("/incidents", methods=["GET"])
def incidents_route():
    """FLIGHT RECORDER — the moments AgentVision already froze for you.

    Capture runs continuously and old frames are pruned, so the disk never fills.
    But the instant a failure signature appears (structured error, screen freeze,
    blank screen, on-screen error text) the recorder FREEZES the preceding window
    plus a short tail as an incident and exempts those frames from pruning. The
    crucial moment is saved BEFORE you ask for it.

    Query: id=<incident id> for one incident's detail, limit (default 25)."""
    want_id = (request.args.get("id") or "").strip()
    limit = max(1, min(_int_arg("limit", "25"), 100))
    with _lock:
        incs = [dict(x) for x in _incidents]
        pinned = len(_pinned_seqs)
        live = len(_frames)
        stats = dict(_recorder_stats)

    if want_id:
        match = next((i for i in incs if i.get("id") == want_id), None)
        if not match:
            return jsonify({"error": f"no incident {want_id!r}",
                            "hint": "av_incidents() lists them"}), 404
        rows = []
        for s in (match.get("frame_seqs") or []):
            with _lock:
                fr = dict(_frames[s]) if s in _frames else None
            if fr:
                row = _frame_row(fr)
                row["one_line_summary"] = _one_line_summary(fr)
                rows.append(row)
        match["frames"] = rows[-200:]
        match["frames_available"] = len(rows)
        match["next"] = {
            "full_bundle": f"av_error_moment(seq={match.get('trigger_seq')})",
            "step_through": (f"av_replay(from_ms={match['window_ms'][0]}, "
                             f"to_ms={match['window_ms'][1]})"),
        }
        return jsonify(match), 200

    truncated = len(incs) > limit
    return jsonify({
        "what_this_is": (
            "Frozen pre-error windows. Each one is the "
            f"{RECORDER_WINDOW_S:.0f}s BEFORE a failure plus {RECORDER_TAIL_S:.0f}s "
            "after, already on disk and exempt from eviction."),
        "recorder": {
            "enabled": RECORDER_ENABLED,
            "incident_window_seconds": RECORDER_WINDOW_S,
            # Retained under its old name for compatibility, but this is the
            # width of the window an incident FREEZES — it is NOT a delete rule.
            # Frames expire on the byte budget, once examined: av_retention().
            "rolling_window_seconds": RECORDER_WINDOW_S,
            "retention": "byte budget + examine-before-delete (av_retention)",
            "disk_budget_bytes": _ret.LEDGER.budget_bytes,
            "examine_mode": _ret.LEDGER.mode,
            "frames_awaiting_examination": len(_ret.LEDGER.awaiting(limit=100000)),
            "post_trigger_tail_seconds": RECORDER_TAIL_S,
            "max_unpinned_frames": RECORDER_MAX_FRAMES,
            "frames_live_now": live,
            "frames_pinned_by_incidents": pinned,
            "frames_pruned_this_session": stats.get("frames_pruned"),
            "bytes_pruned_this_session": stats.get("bytes_pruned"),
            "incidents_frozen": stats.get("incidents_frozen"),
            "triggers": sorted(_INCIDENT_EVENT_TYPES | {"error"}),
        },
        "count": len(incs),
        "truncated_to_most_recent": truncated,
        "incidents": [{k: v for k, v in i.items() if k != "frame_seqs"}
                      for i in incs[-limit:]],
        "next": "av_incidents(id='<id>') for one, then av_error_moment(seq=...)",
    }), 200


@app.route("/replay", methods=["GET"])
def replay_route():
    """TIME-TRAVEL — step through what happened without re-running anything.

    A bounded, ordered walk of frame descriptors each paired with the log slice
    that lines up with it. This is the recorded past, replayed as JSON.

    Query: from_ms, to_ms (default: the whole session), step (take every Nth
    changed moment, default 1), limit (default 40), incident=<id> to replay one
    frozen incident, logs (log lines per step, default 3)."""
    from utils import visual_engine as ve
    inc_id = (request.args.get("incident") or "").strip()
    _from, _to, window = _window_from_args()
    if inc_id:
        inc = next((dict(i) for i in _incidents if i.get("id") == inc_id), None)
        if not inc:
            return jsonify({"error": f"no incident {inc_id!r}"}), 404
        window = (inc["window_ms"][0], inc["window_ms"][1])
        _from, _to = window
    step   = max(1, min(_int_arg("step", "1"), 100))
    limit  = max(1, min(_int_arg("limit", "40"), 200))
    n_logs = max(0, min(_int_arg("logs", "3"), 20))

    frames = _frames_sorted()
    if window is not None:
        frames = [f for f in frames
                  if window[0] <= float(f.get("timestamp_ms") or 0) <= window[1]]
    # Replay the moments that CHANGED — that is the story of the run.
    interesting = [f for f in frames
                   if (_visual_of(f).get("change_score") is None
                       or (_visual_of(f).get("change_score") or 0) >= ve.MIN_CHANGE
                       or _frame_error(f))]
    if not interesting:
        interesting = frames
    picked = interesting[::step][-limit:]
    # Replaying a frame IS examining it — including failure frames, since replay
    # is how the agent walks a frozen incident.
    for _f in picked:
        _mark_seen(_f.get("sequence"), "replay")

    prof = _active_profile_obj()
    steps = []
    for i, f in enumerate(picked):
        ms = float(f.get("timestamp_ms") or 0)
        logs = []
        if n_logs:
            try:
                evs = _log_sources.read_normalized(
                    prof, window=(ms - 2000.0, ms + 200.0), limit=n_logs)
                logs = [{"ts_ms": e.get("ts_ms"), "level": e.get("level"),
                         "message": str(((e.get("data") or {}).get("message")
                                         or (e.get("data") or {}).get("name")
                                         or ""))[:120]} for e in evs[-n_logs:]]
            except Exception:
                logs = []
        err = _frame_error(f)
        steps.append({
            "step": i + 1,
            "seq": f.get("sequence"),
            "ts_ms": ms,
            "change_score": _visual_of(f).get("change_score"),
            "changed_bbox": _visual_of(f).get("changed_bbox"),
            "summary": _one_line_summary(f),
            "logs": logs,
            "error": ({"type": err.get("exception_type"),
                       "message": (err.get("message") or "")[:120]} if err else None),
        })
    return jsonify({
        "what_this_is": (
            "A replay of the recorded past: the moments that changed, in order, "
            "each with the log lines that line up with it. No image bytes — "
            "escalate with av_frame_region(seq) or av_error_moment(seq)."),
        "incident": inc_id or None,
        "window": {"from_ms": _from, "to_ms": _to},
        "frames_in_window": len(frames),
        "changed_moments_in_window": len(interesting),
        "step": step,
        "steps_returned": len(steps),
        "steps": steps,
        "est_tokens": ve.est_text_tokens(len(json.dumps(steps))),
        "next": "av_frame_json(seq) / av_frame_region(seq) / av_error_moment(seq)",
    }), 200


@app.route("/visual_events", methods=["GET"])
def visual_events_route():
    """Auto-detected VISUAL events — the screen-side equivalent of log bookmarks.

    Detectors: screen_frozen (hang), blank_screen, layout_change,
    on_screen_error (needs OCR). Consecutive repeats of one type collapse into a
    single event with seq_range + frames, so a 30 s freeze is ONE bookmark.
    Query: type=<filter>, limit (default 50)."""
    from utils import visual_engine as ve
    want = (request.args.get("type") or "").strip().lower()
    limit = max(1, min(_int_arg("limit", "50"), 500))
    with _lock:
        evs = [dict(e) for e in _visual_events]
    if want:
        evs = [e for e in evs if (e.get("type") or "") == want]
    truncated = len(evs) > limit
    evs = evs[-limit:]
    return jsonify({
        "what_this_is": ("Moments the SCREEN told us something was wrong, found "
                         "for free at capture time. Jump straight to these "
                         "instead of scanning frames."),
        "count": len(evs),
        "truncated_to_most_recent": truncated,
        "events": evs,
        "detectors": {
            "screen_frozen":  f"no visual change for >= {ve.FREEZE_SECONDS}s",
            "blank_screen":   "frame is near-uniform black/blank",
            "layout_change":  f">= {ve.LAYOUT_CHANGE * 100:.0f}% of the screen repainted",
            "on_screen_error": ("OCR found error/exception/traceback text "
                                "(requires tesseract; throttled to "
                                f"1 scan / {VISUAL_OCR_MIN_GAP:.0f}ms)"),
        },
        "thresholds_env": {
            "AGENTVISION_VISUAL_FREEZE_SECS": ve.FREEZE_SECONDS,
            "AGENTVISION_VISUAL_LAYOUT_CHANGE": ve.LAYOUT_CHANGE,
            "AGENTVISION_VISUAL_MIN_CHANGE": ve.MIN_CHANGE,
            "AGENTVISION_VISUAL_CELL_DELTA": ve.CELL_DELTA,
            "AGENTVISION_VISUAL_OCR_SCAN": VISUAL_OCR_SCAN,
        },
        "next": "av_error_moment(seq=<seq>) or av_frame_region(seq=<seq>)",
    }), 200


@app.route("/token_report", methods=["GET"])
def token_report():
    """HONESTY + PROOF: what the cheap path actually saved this session.

    Compares frames captured (free) against frames the agent actually paid for,
    the dedup ratio from perceptual hashing, and a MEASURED per-frame comparison
    of full-image base64 vs av_frame_json vs a changed-region crop. States the
    estimation method plainly — these are estimates, not tokenizer counts."""
    from utils import visual_engine as ve
    frames = _frames_sorted()
    with _lock:
        st = dict(_visual_stats)
        uniq = len(_visual_stats["unique_dhashes"])
        reads = {k: (len(v) if isinstance(v, set) else v)
                 for k, v in _agent_reads.items()}
        n_full = len(_agent_reads["seqs_full"])
        n_json = len(_agent_reads["seqs_json"])

    analyzed  = int(st.get("frames_analyzed") or 0)
    unchanged = int(st.get("frames_unchanged") or 0)
    changed   = int(st.get("frames_changed") or 0)

    # Measured comparison on a real frame from this session (else synthetic-free).
    measured = {"available": False,
                "reason": "no analyzed frame available to measure"}
    sample = None
    for f in reversed(frames):
        if _visual_of(f).get("size"):
            sample = f
            break
    if sample is not None:
        v = _visual_of(sample)
        size = v.get("size") or [0, 0]
        img = sample.get("annotated_image") or ""
        full_bytes = _safe_size(img)
        full_b64_chars = int(full_bytes * 4 / 3) if full_bytes else 0
        # The descriptor as this bridge would actually serve it.
        try:
            with app.test_request_context(
                    f"/frame/{sample.get('sequence')}/json?logs=6&ocr=1"):
                resp = frame_json(sample.get("sequence"))
            desc = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
            desc_chars = len(json.dumps(desc))
        except Exception:
            desc_chars = 0
        bbox = v.get("changed_bbox")
        crop = ve.crop_region(img, bbox, max_dim=900) if bbox else {"ok": False}
        measured = {
            "available": True,
            "sample_frame_seq": sample.get("sequence"),
            "frame_size": size,
            "full_frame": {
                "png_bytes": full_bytes,
                "base64_chars": full_b64_chars,
                "est_visual_tokens_if_sent_as_image":
                    ve.visual_tokens(size[0] or 0, size[1] or 0),
                "est_text_tokens_if_sent_as_base64_text":
                    ve.est_text_tokens(full_b64_chars),
            },
            "av_frame_json": {
                "json_chars": desc_chars,
                "est_tokens": ve.est_text_tokens(desc_chars),
                "contains_full_image": False,
            },
            "changed_region_crop": ({
                "bbox": crop.get("bbox"),
                "served_size": crop.get("served_size"),
                "png_bytes": crop.get("bytes"),
                "est_visual_tokens": crop.get("est_visual_tokens"),
            } if crop.get("ok") else {"available": False,
                                      "reason": crop.get("reason",
                                                         "no changed bbox recorded")}),
        }
        fi = measured["full_frame"]["est_visual_tokens_if_sent_as_image"] or 0
        dj = measured["av_frame_json"]["est_tokens"] or 0
        if fi and dj:
            measured["descriptor_vs_full_frame_ratio"] = round(dj / float(fi), 3)
            measured["descriptor_saves_est_tokens"] = max(0, fi - dj)

    # Session-level estimate. Stated as a counterfactual, plainly.
    per_frame_img = 0
    if sample is not None:
        sz = _visual_of(sample).get("size") or [0, 0]
        per_frame_img = ve.visual_tokens(sz[0] or 0, sz[1] or 0)
    naive_all_frames = per_frame_img * len(frames)
    paid_estimate = per_frame_img * n_full + (
        (measured.get("av_frame_json") or {}).get("est_tokens", 0) or 0) * n_json

    return jsonify({
        "what_this_is": (
            "An honest accounting of the token asymmetry. Capture, hashing and "
            "diffing happen locally and cost the agent nothing; only what the "
            "agent actually pulls costs tokens."),
        "estimation_method": (
            "Image tokens use the documented Claude rule ceil(w/28)*ceil(h/28), "
            "capped at the model tier's visual-token limit (high-res tier: 2576px "
            "long edge / 4784 tokens). Text/JSON tokens use ~4 characters per "
            "token. Both are ESTIMATES; no tokenizer is called. Session savings "
            "are a COUNTERFACTUAL: what it would have cost to send every "
            "captured frame as a full image."),
        "capture_side_free_work": {
            "frames_captured":        len(frames),
            "frames_analyzed":       analyzed,
            "frames_visually_unchanged": unchanged,
            "frames_changed":        changed,
            "unchanged_ratio": (round(unchanged / float(analyzed), 4)
                                if analyzed else None),
            "unique_perceptual_hashes": uniq,
            "dedup_ratio": (round(1.0 - (uniq / float(analyzed)), 4)
                            if analyzed else None),
            "dedup_ratio_meaning": ("fraction of analyzed frames that were "
                                    "perceptual duplicates of another frame"),
            "analysis_cost": {
                "total_ms": round(float(st.get("analysis_ms_total") or 0.0), 1),
                "avg_ms_per_frame": (round(float(st.get("analysis_ms_total") or 0) / analyzed, 3)
                                     if analyzed else None),
                "max_ms_per_frame": round(float(st.get("analysis_ms_max") or 0.0), 3),
                "budget_ms_at_current_rate": round(_auto_engine.interval * 1000.0, 1),
                "shares_one_image_decode_with_the_health_check": True,
            },
            "ocr_scans_on_capture_path": int(st.get("ocr_scans") or 0),
        },
        "agent_side_paid_work": {
            "av_frame_json_calls":    reads.get("frame_json"),
            "av_frame_region_calls":  reads.get("region"),
            "full_frames_pulled":     reads.get("full_frame"),
            "distinct_frames_pulled_full": n_full,
            "distinct_frames_read_as_json": n_json,
            "visual_changes_calls":   reads.get("visual_changes"),
        },
        "measured_comparison": measured,
        "session_estimate": {
            "est_tokens_if_every_frame_were_sent_as_an_image": naive_all_frames,
            "est_tokens_actually_spent_on_frames": int(paid_estimate),
            "est_tokens_avoided": max(0, int(naive_all_frames - paid_estimate)),
            "caveat": ("The naive baseline is a counterfactual nobody would "
                       "actually run; treat it as the ceiling the cheap path "
                       "avoids, not as a bill you dodged."),
        },
        "the_rule": ("Prefer av_visual_changes and av_frame_json. Escalate to "
                     "av_frame_region for pixels, and to the full PNG only when "
                     "a crop is genuinely insufficient."),
    }), 200


@app.route("/ui_tree", methods=["GET"])
def ui_tree_route():
    """THE CHEAPEST WAY TO READ A SCREEN — the target window's ACCESSIBILITY TREE
    as JSON: exact element text, roles and pixel bboxes, with no OCR guessing.

    Query: app (window/app name — defaults to the profile's capture_app), flat
    (0/1, default 1 — flat rows are usually cheaper than a nested tree),
    max_nodes, compare_to_seq (report the cost against that frame's screenshot).

    Returns {available, backend, element_count, prune_stats, elements|tree,
    cost{est_tokens, cheaper_than_screenshot, verdict}} — and an honest
    {available:false, reason, fallback} for custom-drawn UIs (games, emulators,
    canvas/WebGL) which legitimately expose no tree."""
    from utils import ui_tree as ut
    collector = _get_collector()
    app_name = (request.args.get("app")
                or getattr(collector.profile, "capture_app", "") or "").strip()
    flat = request.args.get("flat", "1") not in ("0", "", "false")
    seq_arg = request.args.get("compare_to_seq")
    frame_size = None
    if seq_arg:
        try:
            with _lock:
                fr = _frames.get(int(seq_arg))
            if fr:
                frame_size = _visual_of(fr).get("size")
        except Exception:
            frame_size = None
    if frame_size is None:
        with _lock:
            lf = _latest_for_active()
        if lf:
            frame_size = _visual_of(lf).get("size")
    # max_nodes is PER REQUEST. It used to assign ut.MAX_NODES and never restore
    # it, so one call with max_nodes=20 silently became the default cap for every
    # later caller in the process — and two concurrent requests could not both be
    # right. The module global is restored in a finally, under a lock, so the
    # override cannot outlive or overlap the request that asked for it.
    _mn = None
    if request.args.get("max_nodes"):
        try:
            _mn = max(10, min(int(request.args["max_nodes"]), 2000))
        except ValueError:
            abort(400)
    with _ui_tree_lock:
        _saved = ut.MAX_NODES
        try:
            if _mn is not None:
                ut.MAX_NODES = _mn
            out = ut.capture_tree(app_name=app_name, flat=flat,
                                  frame_size=frame_size)
        finally:
            ut.MAX_NODES = _saved
    out["max_nodes_applied"] = _mn if _mn is not None else _saved
    out["max_nodes_scope"] = ("this request only — the process default "
                              f"({_saved}) is unchanged")
    out["requested_app"] = app_name or None
    out["what_this_is"] = (
        "The window's UI element tree. Text and coordinates are EXACT (not "
        "OCR-guessed), which makes this both cheaper and more precise than a "
        "screenshot — when the app exposes a tree at all.")
    out["limitations"] = [
        "Custom-drawn UIs (games, emulators, canvas/WebGL, Dear ImGui) expose "
        "no tree — expect available:false or a near-empty tree.",
        "Unlabelled controls are missing even when visible on screen.",
        "Icon colour, layout correctness, progress bars and rendering "
        "corruption are NOT in the tree — use av_frame_region for those.",
        "macOS needs the Accessibility permission; Linux needs at-spi2 running.",
    ]
    return jsonify(out), 200


@app.route("/ui_diff", methods=["GET"])
def ui_diff_route():
    """SEMANTIC screen diff — what CHANGED in the UI, in words.

    Takes a UI-tree snapshot, waits `wait_ms`, takes another, and reports
    {appeared, disappeared, changed, summary} — e.g. "changed: AXStaticText
    'Ready' -> 'Error: timeout'". Far cheaper and far more meaningful than a
    pixel diff, and it names the element rather than a rectangle.

    Query: app, wait_ms (default 1000, max 10000)."""
    from utils import ui_tree as ut
    collector = _get_collector()
    app_name = (request.args.get("app")
                or getattr(collector.profile, "capture_app", "") or "").strip()
    wait_ms = max(0.0, min(_float_arg("wait_ms", "1000"), 10000.0))
    a = ut.capture_tree(app_name=app_name)
    if not a.get("available"):
        return jsonify({"available": False, "reason": a.get("reason"),
                        "fallback": a.get("fallback"),
                        "hint": "av_visual_changes gives the pixel-level answer"}), 200
    time.sleep(wait_ms / 1000.0)
    b = ut.capture_tree(app_name=app_name)
    if not b.get("available"):
        return jsonify({"available": False, "reason": b.get("reason")}), 200
    d = ut.diff_trees(a.get("tree") or [], b.get("tree") or [])
    d["available"] = True
    d["what_this_is"] = ("A SEMANTIC diff of the UI between two moments "
                         f"{wait_ms:.0f}ms apart — named elements, not rectangles.")
    d["wait_ms"] = wait_ms
    d["backend"] = b.get("backend")
    d["element_counts"] = {"before": a.get("element_count"),
                           "after": b.get("element_count")}
    return jsonify(d), 200


@app.route("/start_here", methods=["GET"])
def start_here():
    """READ THIS FIRST. Tiny orientation payload: what AgentVision is watching
    right now, whether the bridge/capture/daemon are alive, the recommended
    workflow, and the token rule of thumb."""
    collector = _get_collector()
    e = _auto_engine
    with _lock:
        n_frames = len(_frames)
        latest_seq = (_latest_frame or {}).get("sequence")
        n_events = len(_visual_events)
        recent_events = [dict(x) for x in _visual_events[-3:]]
    ocr_ok, ocr_engine, ocr_reason = _ocr_engine()
    # Bound once: the workflow list and the bridge_build block must agree, and
    # calling the hint twice invited them to drift.
    _bh = _bridge_build_hint(collector.profile, brief=True)
    return jsonify({
        "what_agentvision_is": (
            "A local debug flight recorder. It screenshots the target program on "
            "a timer, parses every log it declares, and time-aligns the two. All "
            "of that is FREE local CPU. Your job is to ASK it questions, not to "
            "take screenshots or grep logs yourself."),
        "read_this_first": (
            "AgentVision does NOT decide what logging to build into a program — "
            "YOU do, ONCE, on first connection. Until you commit a plan, "
            "av_capture_start() is refused (HTTP 200 with started:false, so check "
            "the body). Follow DO_THIS_NEXT and recommended_workflow below; they "
            "already account for whether this program is built. Once BUILT it "
            "stays built across restarts — never plan the same program twice."),
        "watching_now": {
            "profile":  _active_profile_name,
            "program":  collector.profile.display_name,
            "running":  bool(collector._reader.is_running()),
            "project_root": getattr(collector.profile, "project_root", "") or None,
            "language": getattr(collector.profile, "language", "") or None,
            # THE FIRST QUESTION A COLD MODEL MUST ASK, AND THE ONE NOTHING USED
            # TO ANSWER. Measured with a cold Haiku pointed at a project that was
            # not the active profile: it read BUILT, concluded the bridge was
            # sealed against a different program, decided the task was impossible
            # and stopped. Nothing in the server instructions or this response
            # mentioned profiles at all, so it had no way to know that adding a
            # program is a NEW PROFILE rather than a re-plan.
            # Short on purpose: start_here has a hard size budget (it is the first
            # call of every session). The full explanation is in the server
            # instructions under STEP ZERO. This only has to stop a cold model
            # concluding a sealed bridge on ANOTHER profile blocks it — measured:
            # one did exactly that and gave up.
            "not_your_program?": ("av_create_profile(...) + "
                                  "av_set_active_profile(...) — a new program is "
                                  "a new profile, not a replan"),
        },
        "state": {
            "bridge": "up",
            "capture_engine_running": e.running,
            "capturing_now": e.capturing,
            "shots_per_second": _fps(e.interval),
            "frames_in_memory": n_frames,
            "latest_frame_seq": latest_seq,
            "daemon": _http_daemon_status_dict(),
            # FIRST THING THE AGENT SHOULD SEE on a program that has never been
            # bridged. av_start_here is the documented first call, so if the gate
            # is not surfaced here the agent goes straight to av_capture_start,
            # hits a refusal with no context, and has to work out why.
            "bridge_build": _bh,
            "preflight": _preflight_hint(collector.profile),
            "visual_events_detected": n_events,
            "ocr": {"available": ocr_ok, "engine": ocr_engine or None,
                    "reason": ocr_reason or None},
            "flight_recorder": {
                "enabled": RECORDER_ENABLED,
                "incident_window_seconds": RECORDER_WINDOW_S,
                "incidents_frozen": len(_incidents),
                "what_it_means": ("Capture runs continuously and the window "
                                  "BEFORE any failure is frozen automatically "
                                  "— av_incidents()."),
            },
            "retention": {
                "disk_budget": _ret._human(_ret.LEDGER.budget_bytes),
                "examine_mode": _ret.LEDGER.mode,
                "frames_awaiting_your_eyes":
                    len(_ret.LEDGER.awaiting(limit=100000)),
                "what_it_means": ("Frames are NOT deleted on a timer. A frame "
                                  "flagged for examination is held until you "
                                  "examine it; disk is bounded by bytes. If "
                                  "frames_awaiting_your_eyes is above zero, "
                                  "AgentVision is holding visual evidence FOR "
                                  "YOU — av_frames_awaiting()."),
            },
        },
        # The workflow MUST depend on whether the bridge is built. A single static
        # list is what made this response self-contradictory: it told the agent to
        # run av_preflight "before the first capture" while bridge_build said
        # capture would be refused until a plan was committed. A literal-minded
        # model follows the numbered list, so the numbered list has to be right.
        "DO_THIS_NEXT": _bh.get("do_this_now") or "av_diagnose()",
        "recommended_workflow": (
            [
                "THIS PROGRAM HAS NEVER BEEN BRIDGED. Capture is REFUSED until you",
                "plan it. This is a ONE-TIME setup, per program, and YOU decide it.",
                "",
                "1. av_bridge_status()   — confirm state is PROVISIONAL (not BUILT)",
                "2. av_bridge_catalog()  — read the menu: emitters, adapters, tools,",
                "                          plus this program's own code evidence.",
                "                          Returns the catalog_token you need next.",
                "3. av_bridge_commit(plan={catalog_token, emitters, why, rationale,",
                "                          tools})  — YOUR decision; builds it.",
                "",
                "AFTER that commit succeeds, the steps below become available and",
                "this gate never fires for this program again.",
                "4. av_capture_start()   — begin recording",
                "5. av_diagnose()        — when something is wrong",
                "6. av_session_report()  — wrap up",
            ] if not _bh.get("ok") else [
                "This program's bridge is BUILT. Setup is done — do NOT plan again.",
                "",
                "1. av_start_here            — this call; orient (you just did it)",
                "2. av_capture_start         — if capturing_now is false and you "
                "need visual evidence",
                "3. av_diagnose              — when something is wrong: ranked "
                "hypotheses",
                "4. av_log_raw               — the raw log, verbatim, uninterpreted",
                "5. av_visual_changes        — instead of browsing frames",
                "6. av_incidents             — pre-error windows already frozen",
                "7. av_error_moment          — one-call bundle for a specific failure",
                "8. av_replay                — step through the recorded past",
                "9. av_session_report        — wrap up",
            ]),
        "token_rule_of_thumb": (
            "Prefer av_frame_json / av_visual_changes over raw frames. Escalate "
            "to pixels only when JSON is insufficient, and then take the "
            "changed region (av_frame_region) before the full image. "
            "av_token_report shows what that saved."),
        "cheap_path": {
            "1_json_only":     "av_visual_changes(), av_frame_json(seq)",
            "2_tiny_thumb":    "av_frame_json(seq, thumbnail=True)",
            "3_changed_pixels": "av_frame_region(seq)",
            "4_full_frame":    "av_get_frame(seq) then read annotated_image",
        },
        "recent_visual_events": recent_events,
        "do_not": [
            "Do NOT take your own screenshots — capture is already running.",
            "Do NOT grep raw logs — av_search / av_log_normalized are parsed and merged.",
            "Do NOT page through frames one by one — av_visual_changes collapses them.",
            "Do NOT hand-correlate a timestamp to a frame — av_error_moment did it.",
        ],
    }), 200


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AgentVision Bridge Server")
    parser.add_argument("--host",     default="127.0.0.1")
    parser.add_argument("--port",     type=int, default=7771)
    # AgentVision is UNIVERSAL — no hardcoded target. --profile defaults to whatever
    # was persisted / set via AGENTVISION_ACTIVE_PROFILE (resolved at import), so the
    # last-selected program is honored instead of always snapping back to a game bot.
    parser.add_argument("--profile",  default=_active_profile_name)
    parser.add_argument("--folder",   default="snapshots")
    parser.add_argument("--tests",    action="store_true")
    parser.add_argument("--interval", type=float, default=CAPTURE_INTERVAL)
    parser.add_argument("--no-autocapture", action="store_true",
                        help="Start bridge without auto-capture engine "
                             "(GUI controls when capture starts via /capture/start)")
    args = parser.parse_args()

    _active_profile_name = args.profile
    SAVE_FOLDER          = args.folder
    RUN_TESTS            = args.tests
    _persist_active_profile(_active_profile_name)

    profiles = load_profiles()
    profile  = profiles.get(args.profile, ProgramProfile())

    _info(f"AgentVision Bridge Server starting")
    _info(f"  URL          : http://{args.host}:{args.port}")
    _info(f"  Profile      : {args.profile} ({profile.display_name})")
    _info(f"  Project root : {profile.project_root or '(not set)'}")
    _info(f"  Log file     : {profile.log_file or '(not set)'}")
    _info(f"  Save folder  : {SAVE_FOLDER}")
    _info(f"  capture_app  : {profile.capture_app or '(none)'}")
    _info(f"  capture_crop : {profile.capture_crop or '(none)'}")
    _info(f"  Interval     : {args.interval}s")
    _info(f"  Debug log    : {_DEBUG_LOG}")

    # Pre-create output folder
    out = profile_output_folder(profile, _base_for(profile))
    _info(f"  Output folder: {out}")

    # Repopulate frame index from on-disk sidecars so bookmark→frame and
    # /frame/<seq> still work after a bridge restart.
    _hydrate_frame_index()
    # ...then continue numbering above what survived, so this run cannot
    # overwrite the previous run's retained frames.
    _auto_engine.resume_counter_from_disk()

    # Session bootstrap — mark the moment this bridge started; fingerprints
    # of failures appearing after this point are flagged as "new this session".
    # (Module-level scope here, no `global` keyword needed.)
    _session_start_ms = time.time() * 1000.0
    _session_seen_fps.update(_load_fp_history())
    _info(f"Session start: {_session_start_ms:.0f}ms  "
          f"(fingerprint history: {len(_session_seen_fps)} known)")

    # Stuck-state watchdog — records program.stuck in AGENTVISION's own observer
    # log (log/observer/<profile>.observer.jsonl, see /observer/log) when the
    # process is alive but has emitted nothing for too long. It never writes to
    # the bridged program's own log.
    import threading as _t
    _t.Thread(target=_stuck_watchdog, daemon=True, name="av-watchdog").start()

    # Start the auto-capture engine — it watches for the program and fires.
    # When --no-autocapture is set, the engine is initialised but NOT started;
    # the GUI must explicitly POST /capture/start to begin screenshotting.
    _auto_engine.set_interval(args.interval)
    if not args.no_autocapture:
        _auto_engine.start()
    else:
        _info("Auto-capture suppressed at startup (--no-autocapture). "
              "Use POST /capture/start to begin.")

    app.run(host=args.host, port=args.port, threaded=True, debug=False)
