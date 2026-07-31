"""
Examine-before-delete retention: never throw away a frame the agent has not seen.
================================================================================
THE PROBLEM WITH A CLOCK
------------------------
The original flight recorder deleted every frame older than 60 seconds. That is
shorter than a single agent reasoning turn: AgentVision could announce "error at
frame 4213", the agent would think for two minutes, call av_error_moment(4213) —
and the pixels were already gone. Every one of those shots was taken for nothing.

THE MODEL HERE
--------------
Disk is bounded by BYTES (default 5 GiB), not by seconds, and a frame becomes
deletable only once it has been *cleared*. A frame walks these states:

    admitted  -> captured; its on-disk footprint is now known
    mined     -> everything cheap and local has been extracted into the sidecar
                 JSON + a persisted thumbnail (this is what makes the shot
                 worthwhile even if no agent ever looks at the pixels)
    queued    -> the policy says this one needs real eyes; it is HELD, i.e. not
                 deletable, and it goes into the awaiting queue
    offered   -> AgentVision has actually told the agent about it (a push
                 injection carrying this seq was delivered)
    examined  -> the agent pulled it (frame_json / region / full image / OCR)
                 or explicitly acked it
    cleared   -> deletable: mined AND (never needed eyes OR examined OR the
                 hold backstop expired)

Only cleared frames are evicted, cheapest-value first, and only when the budget
demands it. So "delete at 60s" becomes "delete when we need the space, and only
what has already served its purpose".

NOT EVERY FRAME NEEDS EYES
--------------------------
Sending 6,000 PNGs at an agent would cost more than the bug is worth. The policy
mode decides who gets promoted to the awaiting queue:

    off      never push; mine locally only (JSON history still accrues)
    errors   DEFAULT — only frames time-aligned with an error, crash, or a
             failure-shaped visual event (freeze / blank / on-screen error text)
    changes  the above, plus every frame with a real visual change
    all      every frame — gives the agent video-like continuity when motion
             itself is the evidence

Agents do not need 24 fps to perceive motion, so "all" is usually paired with a
slower capture interval; that trade-off is the user's slider to set, not ours.

THE BACKSTOP
------------
A held frame cannot wedge the disk forever: after HOLD_S with no examination it
is force-cleared, and the count is reported as `dropped_unexamined` rather than
silently swallowed. Silent truncation would read as "the agent saw everything"
when it did not — the one failure mode this module exists to prevent.

Pure bookkeeping plus small, isolated file operations: no Flask, no capture loop,
no global bridge state. That keeps it unit-testable without a running server.
"""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

# ── Configuration ─────────────────────────────────────────────────────────────

_UNITS = {"": 1, "B": 1, "K": 1024, "KB": 1024, "M": 1024 ** 2, "MB": 1024 ** 2,
          "G": 1024 ** 3, "GB": 1024 ** 3, "T": 1024 ** 4, "TB": 1024 ** 4}


def parse_bytes(text: str | int | float, default: int) -> int:
    """Accept 5GB / 5G / 500MB / raw byte counts. Never raises — a malformed
    budget must not stop the recorder from running."""
    if isinstance(text, (int, float)):
        return max(0, int(text))
    s = str(text or "").strip().upper().replace(" ", "")
    if not s:
        return default
    m = re.fullmatch(r"([0-9]*\.?[0-9]+)([A-Z]*)", s)
    if not m:
        return default
    try:
        return max(0, int(float(m.group(1)) * _UNITS.get(m.group(2), 1)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except Exception:
        return default


#: Hard ceiling on everything the recorder owns on disk (frames + archive).
DEFAULT_BUDGET_BYTES = parse_bytes(os.environ.get("AGENTVISION_DISK_BUDGET", ""),
                                  5 * 1024 ** 3)
#: Start evicting at this fraction of budget, stop at LOW_WATER. Hysteresis, so
#: we do a small amount of work occasionally rather than one file per capture.
HIGH_WATER = min(1.0, max(0.10, _env_float("AGENTVISION_DISK_HIGH_WATER", 0.90)))
LOW_WATER  = min(HIGH_WATER - 0.01,
                 max(0.05, _env_float("AGENTVISION_DISK_LOW_WATER", 0.75)))
#: How long a frame may sit unexamined before the backstop force-clears it.
DEFAULT_HOLD_S = _env_float("AGENTVISION_EXAMINE_HOLD_S", 900.0)     # 15 min
#: Free-disk floor: below this we evict hard regardless of budget headroom.
MIN_FREE_BYTES = parse_bytes(os.environ.get("AGENTVISION_MIN_FREE_DISK", ""),
                             2 * 1024 ** 3)
#: A free-disk floor is a number a human typed, one line away from
#: AGENTVISION_DISK_BUDGET in the docs, and parse_bytes clamps only to >= 0.
#: Set it above the volume's size (500GB on a 250GB disk, or point the probe at
#: an unmounted external drive) and `low_disk` is True forever on a healthy
#: machine — which is what unlocks the emergency tiers. So the floor is capped
#: against the volume it is measured on: no floor may claim more than this
#: fraction of the whole disk.
MIN_FREE_MAX_FRACTION = min(0.9, max(0.01,
                            _env_float("AGENTVISION_MIN_FREE_MAX_FRACTION", 0.25)))

# ── Emergency eviction limits ────────────────────────────────────────────────
# The emergency path had no floor: one truthy `low_disk` set the target to half
# of everything held and unlocked EVERY tier, including frames the policy says
# the agent must still see (tier 2) and frames pinned to a frozen incident
# (tier 3). Reproduced on a fixture with real files: 320 MB against a 5 GiB
# budget planned 45 evictions in one pass, and six consecutive passes — 0.6 s at
# 10 fps, because nothing rate-limited them — destroyed 29 of 30 pinned incident
# frames and all 30 never-seen frames, reporting dropped_unexamined=0.
#
#: Never. Pinned evidence is not currency; if only pinned frames remain, the
#: plan reports unmet bytes instead of spending them.
_TIER_PINNED = 3
#: Frames the agent still owes eyes to, per emergency pass. Bounded blast radius
#: instead of "half of everything".
EMERGENCY_UNSEEN_PER_PASS = max(0, int(_env_float(
    "AGENTVISION_EMERGENCY_UNSEEN_PER_PASS", 20)))
#: Minimum gap between emergency passes. _recorder_prune runs once per captured
#: frame — up to ten times a second — and a disk does not become un-full that
#: fast, so consecutive passes only compound the same decision.
EMERGENCY_COOLDOWN_S = max(0.0, _env_float("AGENTVISION_EMERGENCY_COOLDOWN_S", 5.0))

DEFAULT_MODE = (os.environ.get("AGENTVISION_EXAMINE", "") or "errors").lower()

#: Archive: on eviction, an interesting frame's pixels are re-encoded small and
#: kept, so a day of capture stays reviewable for a few MB.
ARCHIVE_ENABLED = os.environ.get("AGENTVISION_ARCHIVE", "1") != "0"
ARCHIVE_MAX_W   = int(_env_float("AGENTVISION_ARCHIVE_WIDTH", 640))
ARCHIVE_QUALITY = int(_env_float("AGENTVISION_ARCHIVE_QUALITY", 72))
#: Share of the budget the archive may occupy (it must never starve live frames).
ARCHIVE_BUDGET_FRACTION = min(0.5, max(0.0,
                              _env_float("AGENTVISION_ARCHIVE_FRACTION", 0.10)))
THUMB_WIDTH = int(_env_float("AGENTVISION_THUMB_WIDTH", 96))

# ── Examination policy ────────────────────────────────────────────────────────

# Priority: lower means "the agent more urgently needs to see this one".
P_ERROR  = 0      # time-aligned with a structured error / crash / incident
P_EVENT  = 1      # failure-shaped visual event: freeze, blank, on-screen error
P_BIG    = 2      # large layout change — probably a state transition
P_CHANGE = 3      # any real visual change
P_STATIC = 4      # indistinguishable from its predecessor

#: mode -> highest priority number that still gets promoted to the queue.
MODES: dict[str, int] = {"off": -1, "errors": P_EVENT, "changes": P_CHANGE,
                         "all": P_STATIC}

#: Visual-event kinds that mean "something went wrong", as opposed to ordinary
#: scene changes. Matched per WORD, not as raw substrings: "layout_change"
#: contains "hang" ("c-hang-e") and would otherwise be misread as a failure.
#: Real kinds emitted today: blank_screen, screen_frozen, screen_idle,
#: layout_change, on_screen_error — note screen_idle is deliberately NOT a
#: failure, since the detector uses it for "static but demonstrably alive".
_FAILURE_EVENT_HINTS = ("error", "crash", "freeze", "frozen", "blank", "black",
                        "hang", "hung", "stuck", "exception", "fault", "fail",
                        "abort", "panic", "deadlock", "timeout")


def _is_failure_kind(kind: str) -> bool:
    tokens = re.split(r"[^a-z0-9]+", str(kind).lower())
    return any(tok.startswith(h) for tok in tokens if tok
               for h in _FAILURE_EVENT_HINTS)

BIG_CHANGE_SCORE = _env_float("AGENTVISION_BIG_CHANGE", 0.25)


def normalize_mode(mode: str | None) -> str:
    m = (mode or DEFAULT_MODE or "errors").lower().strip()
    return m if m in MODES else "errors"


def assess(facts: dict, mode: str | None = None) -> dict:
    """Classify one frame: how urgently does a human-or-agent eye need it, and
    how much is it worth keeping when the disk gets tight?

    `facts` is deliberately loose — the caller passes whatever it knows:
        has_error (bool), error_label (str), incident (bool),
        event_kinds (list[str]), change_score (float), changed (bool),
        ocr_error (bool)
    Missing keys simply do not contribute.
    """
    facts = facts or {}
    mode = normalize_mode(mode)
    kinds = [str(k).lower() for k in (facts.get("event_kinds") or [])]
    failure_kinds = [k for k in kinds if _is_failure_kind(k)]
    try:
        score = float(facts.get("change_score") or 0.0)
    except Exception:
        score = 0.0
    changed = bool(facts.get("changed")) or score > 0.0
    unreadable_static = (not changed
                         and str(facts.get("ocr_state") or "") == "could_not_read")

    if facts.get("has_error") or facts.get("incident"):
        pri, why = P_ERROR, (str(facts.get("error_label") or "").strip()
                             or "error on this frame")
    elif failure_kinds or facts.get("ocr_error"):
        pri = P_EVENT
        why = ("on-screen error text" if facts.get("ocr_error")
               else "visual event: " + ", ".join(failure_kinds[:3]))
    elif score >= BIG_CHANGE_SCORE:
        pri, why = P_BIG, f"large visual change ({score:.0%})"
    elif changed:
        pri, why = P_CHANGE, f"visual change ({score:.0%})"
    elif unreadable_static:
        # A STATIC frame whose text could not be read is the worst thing to throw
        # away first. `ocr_error: False` used to mean two different things —
        # "OCR read the screen and saw no error" and "OCR read nothing" — and the
        # second landed here as P_STATIC, interest 0.05, i.e. first in the
        # eviction queue. MEASURED: 32 of 72 small on-screen error lines on a
        # 2560x1440 frame return empty OCR, so this is the ordinary case for a
        # small error dialog, not an edge case. Not promoted to P_EVENT — that
        # would claim an error we have not seen — just no longer the cheapest
        # thing on the shelf.
        pri, why = P_CHANGE, ("static, and its text could not be read — not "
                              "established to be error-free")
    else:
        pri, why = P_STATIC, "no visible change"

    # Keep-value, used to order eviction. Errors are worth far more than motion.
    interest = {P_ERROR: 1.0, P_EVENT: 0.85, P_BIG: 0.6,
                P_CHANGE: 0.35, P_STATIC: 0.05}[pri]
    interest = min(1.0, interest + min(0.10, score / 10.0))

    needs_eyes = pri <= MODES[mode]
    # RE-RANKED, NEVER HELD. Promoting an unreadable static frame to P_CHANGE was
    # meant only to stop it being first in the eviction queue. But `needs_eyes`
    # is derived from the same rank, so in mode=changes/all (P_CHANGE <= P_CHANGE)
    # it also made every such frame AWAIT EXAMINATION — and it is not a rare
    # case: OCR is throttled, so most frames inherit the last global state, and
    # on a machine with no OCR engine at all EVERY frame is could_not_read. That
    # pins the disk until the 900s hold backstop force-clears it. "We could not
    # read this" is a reason to keep the frame a little longer, not a reason to
    # demand the agent look at it.
    if unreadable_static:
        needs_eyes = False
    return {"priority": pri, "interest": round(interest, 4), "reason": why,
            "needs_eyes": needs_eyes, "mode": mode,
            "failure": pri <= P_EVENT}


# ── Small file helpers ────────────────────────────────────────────────────────

#: Matches the `frame_<seq>` stem prefix of every file a frame owns.
_FRAME_FILE_RE = re.compile(r"^frame_\d+")


def _size(path: str | Path) -> int:
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def stem_group(folder: str | Path, stem: str) -> list[str]:
    """Every file this frame owns — `frame_00042.png`, `_annotated.png`,
    `.diff`, `_frame.json`, thumbnails. Retention must account for and remove
    the whole group; billing only the two paths the frame dict happens to name
    is exactly how 4.7 GB of orphans accumulated.

    For one frame on the capture path. To resolve MANY stems (startup hydration)
    use stem_groups(): calling this in a loop re-scans the directory per frame,
    which is O(frames x files) and visibly stalls a restart once a folder holds
    thousands of frames.
    """
    out: list[str] = []
    try:
        for p in Path(folder).glob(f"{stem}*"):
            if p.is_file():
                out.append(str(p))
    except Exception:
        pass
    return sorted(out)


def stem_groups(folder: str | Path) -> dict[str, list[str]]:
    """Map every `frame_*` stem in `folder` to the files it owns, in ONE scan.

    Hydration re-admits every surviving frame, and doing that with a per-frame
    glob measured as a multi-minute stall on a directory holding ~8,000 frames
    (~33,000 files) — O(frames x files). One listing makes it linear.
    """
    groups: dict[str, list[str]] = {}
    try:
        entries = list(Path(folder).iterdir())
    except Exception:
        return groups
    for p in entries:
        try:
            if not p.is_file():
                continue
        except Exception:
            continue
        m = _FRAME_FILE_RE.match(p.name)
        if m:
            groups.setdefault(m.group(0), []).append(str(p))
    for k in groups:
        groups[k].sort()
    return groups


def write_thumbnail(image_path: str | Path, out_path: str | Path,
                    width: int = THUMB_WIDTH) -> dict:
    """Persist a tiny thumbnail NOW, while the full PNG still exists.

    Thumbnails were previously rendered on demand from the full frame, which
    means they died with it. Writing one at capture time is what lets the JSON
    history stay visual after the pixels are evicted.
    """
    try:
        from PIL import Image
    except Exception as exc:
        return {"ok": False, "reason": f"pillow unavailable: {exc}"}
    try:
        with Image.open(str(image_path)) as im:
            w, h = im.size
            tw = max(16, min(int(width), w))
            th = max(1, int(h * (tw / float(w))))
            im.convert("RGB").resize((tw, th)).save(str(out_path), format="PNG",
                                                    optimize=True)
        return {"ok": True, "path": str(out_path), "size": [tw, th],
                "bytes": _size(out_path)}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


def archive_frame(image_path: str | Path, dest_dir: str | Path, *,
                  max_width: int = ARCHIVE_MAX_W,
                  quality: int = ARCHIVE_QUALITY) -> dict:
    """Convert + compress a frame on its way out: WebP if available, JPEG
    otherwise. This is the "history" half of the deal — the full-resolution PNG
    is what gets reclaimed, not the knowledge of what was on screen."""
    if not ARCHIVE_ENABLED:
        return {"ok": False, "reason": "archive disabled"}
    try:
        from PIL import Image
    except Exception as exc:
        return {"ok": False, "reason": f"pillow unavailable: {exc}"}
    try:
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        stem = Path(image_path).stem
        with Image.open(str(image_path)) as im:
            w, h = im.size
            tw = max(32, min(int(max_width), w))
            th = max(1, int(h * (tw / float(w))))
            small = im.convert("RGB").resize((tw, th))
            for fmt, ext in (("WEBP", ".webp"), ("JPEG", ".jpg")):
                out = dest / f"{stem}{ext}"
                try:
                    small.save(str(out), format=fmt, quality=int(quality),
                               optimize=True)
                    return {"ok": True, "path": str(out), "format": fmt.lower(),
                            "size": [tw, th], "bytes": _size(out)}
                except Exception:
                    continue
        return {"ok": False, "reason": "no encoder accepted the frame"}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


#: Most files one sweep may remove from one directory. The original incident
#: removed 7,501 files in a single pass and nothing anywhere would have stopped
#: it. The sidecar check below is what makes that particular mistake impossible;
#: this cap is what bounds the NEXT one, whatever shape it takes.
SWEEP_MAX_REMOVALS = max(1, int(_env_float("AGENTVISION_SWEEP_MAX_REMOVALS", 1000)))
#: If a sweep believes nearly EVERY frame file in a directory is an orphan, the
#: bookkeeping has collapsed rather than the directory having filled with
#: garbage. That is the incident's signature, so refuse the whole pass and say so.
SWEEP_REFUSE_FRACTION = min(1.0, max(0.5, _env_float(
    "AGENTVISION_SWEEP_REFUSE_FRACTION", 0.9)))
#: Below this many frame files a high fraction means nothing (a folder with two
#: files is 100% orphans as soon as one is), so the fraction guard is skipped.
SWEEP_REFUSE_MIN_FILES = max(1, int(_env_float(
    "AGENTVISION_SWEEP_REFUSE_MIN_FILES", 20)))
#: Where a sweep records what it removed, inside the folder it swept.
SWEEP_MANIFEST_NAME = ".av_sweep_manifest.jsonl"


def _write_sweep_manifest(folder: Path, payload: dict) -> None:
    """Append one line describing this sweep, BEFORE anything is unlinked.

    No destructive path in this project wrote down what it removed, which is
    exactly why the 1,500-frame loss was reconstructible only from a count. A
    manifest is cheap and it is the difference between "270 MB went missing" and
    a list of the files that went.
    """
    try:
        import json as _json
        with open(folder / SWEEP_MANIFEST_NAME, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(payload) + "\n")
    except Exception:
        pass


def sweep_orphans(folder: str | Path, live_stems: Iterable[str], *,
                  min_age_s: float = 300.0, dry_run: bool = False,
                  max_removals: int | None = None) -> dict:
    """Delete `frame_*` files belonging to no tracked frame.

    Necessary because the pruner used to delete a frame's PNG and sidecar while
    leaving its `_annotated.png` and `.diff` behind; once the sidecar was gone
    the hydrator could no longer even see those files, so nothing would ever
    remove them. Without this sweep the byte budget below would be a fiction
    from the first run.

    AN ORPHAN IS A FILE WITH NO SIDECAR — not a file the LEDGER has not heard of.
    Those are different sets, and treating the ledger as the authority makes this
    function delete real evidence whenever anything upstream fails to admit a
    frame: a parse error, an exception part-way through hydration, a profile
    temporarily absent from profiles.json. It fired for real during this change —
    one gate wrongly placed in the hydrator, and the sweep permanently deleted the
    newest 1,500 frames of a live capture (7,501 files, 270 MB) because their
    stems had not been re-admitted. The sidecar check below makes the sweep
    incapable of that: if `<stem>_frame.json` is on disk, the frame exists, and no
    bookkeeping mistake elsewhere can turn it into an orphan.
    """
    live = {str(s) for s in live_stems}
    removed = kept = 0
    freed = 0
    now = time.time()
    errors: list[str] = []
    try:
        entries = list(Path(folder).iterdir())
    except Exception as exc:
        return {"ok": False, "reason": str(exc), "removed": 0, "bytes_freed": 0}
    # Stems that still have a sidecar. These are frames, not orphans, whatever
    # the ledger currently knows.
    have_sidecar = {m.group(0) for m in
                    (_FRAME_FILE_RE.match(p.name) for p in entries
                     if p.is_file() and p.name.endswith("_frame.json")) if m}
    protected_by_sidecar = 0

    # ── Decide first, delete second ───────────────────────────────────────
    # Two things need the full list before anything is unlinked: the blast-radius
    # guards, and the manifest.
    frame_files = [p for p in entries
                   if p.is_file() and _FRAME_FILE_RE.match(p.name)]
    doomed: list[Path] = []
    for p in frame_files:
        stem = _FRAME_FILE_RE.match(p.name).group(0)
        if stem in live:
            kept += 1
            continue
        if stem in have_sidecar:
            kept += 1
            protected_by_sidecar += 1
            continue
        try:
            if (now - p.stat().st_mtime) < min_age_s:
                kept += 1                      # too fresh; may be mid-write
                continue
        except Exception as exc:
            errors.append(f"{p.name}: {exc}")
            continue
        doomed.append(p)

    cap = SWEEP_MAX_REMOVALS if max_removals is None else max(0, int(max_removals))
    refused = capped = 0
    reason = ""

    # "Almost everything here is an orphan" is what a collapse of the bookkeeping
    # looks like from in here, not what a directory of leftovers looks like.
    if (len(frame_files) >= SWEEP_REFUSE_MIN_FILES and frame_files
            and (len(doomed) / len(frame_files)) >= SWEEP_REFUSE_FRACTION):
        refused = len(doomed)
        reason = (f"refused: {len(doomed)} of {len(frame_files)} frame files in "
                  f"{folder} look like orphans "
                  f"(>= {SWEEP_REFUSE_FRACTION:.0%}); that is a bookkeeping "
                  f"failure, not a folder of leftovers")
        _write_sweep_manifest(Path(folder), {
            "ts": now, "folder": str(folder), "action": "refused",
            "reason": reason, "would_have_removed": [p.name for p in doomed],
        })
        return {"ok": True, "removed": 0, "kept": kept + len(doomed),
                "bytes_freed": 0,
                "kept_because_sidecar_exists": protected_by_sidecar,
                "refused": refused, "reason": reason,
                "dry_run": bool(dry_run), "errors": errors[:5]}

    if len(doomed) > cap:
        capped = len(doomed) - cap
        reason = (f"capped at {cap} removals this pass; {capped} left for the "
                  f"next sweep")
        doomed = doomed[:cap]

    if doomed and not dry_run:
        _write_sweep_manifest(Path(folder), {
            "ts": now, "folder": str(folder), "action": "remove",
            "count": len(doomed), "capped_remaining": capped,
            "removing": [p.name for p in doomed],
        })

    for p in doomed:
        try:
            n = _size(p)
            if not dry_run:
                p.unlink()
            removed += 1
            freed += n
        except Exception as exc:
            errors.append(f"{p.name}: {exc}")
    return {"ok": True, "removed": removed, "kept": kept, "bytes_freed": freed,
            "kept_because_sidecar_exists": protected_by_sidecar,
            "refused": refused, "capped_remaining": capped, "reason": reason,
            "dry_run": bool(dry_run), "errors": errors[:5]}


# ── The ledger ────────────────────────────────────────────────────────────────

class Ledger:
    """Authoritative record of what is on disk, who has seen it, and what may
    go next. Thread-safe and self-contained: the capture loop, the HTTP routes
    and the push layer all touch it concurrently."""

    def __init__(self, *, budget_bytes: int | None = None,
                 mode: str | None = None, hold_s: float | None = None) -> None:
        self._lock = threading.RLock()
        self._rec: dict[int, dict] = {}
        self.budget_bytes = int(budget_bytes if budget_bytes is not None
                                else DEFAULT_BUDGET_BYTES)
        self.mode = normalize_mode(mode)
        self.hold_s = float(hold_s if hold_s is not None else DEFAULT_HOLD_S)
        self.archive_bytes = 0
        #: When the last emergency (free-disk floor) pass was planned, so the
        #: cooldown can stop ten passes a second compounding one decision.
        self._last_emergency_ms: float | None = None
        self.stats = {
            "admitted": 0, "evicted": 0, "bytes_reclaimed": 0,
            "archived": 0, "archive_bytes": 0,
            "queued": 0, "offered": 0, "examined": 0, "acked": 0,
            "dropped_unexamined": 0,      # backstop fired — reported, not hidden
            "dropped_pinned": 0,          # emergency ate an incident frame
            # Tier 2 = the agent was told to look and has not. These used to be
            # counted in NEITHER of the two counters above, so 30 must-see
            # frames could die with dropped_unexamined=0 and no log line, in a
            # module whose docstring calls silent truncation the one failure
            # mode it exists to prevent.
            "dropped_unseen": 0,
            "refused_pinned": 0,          # commit declined to spend evidence
            "kept_unarchived": 0,         # no archive copy => pixels stay
            "evictions_run": 0, "orphans_removed": 0, "orphan_bytes": 0,
        }

    # -- configuration -------------------------------------------------------
    def configure(self, *, budget_bytes: int | None = None,
                  mode: str | None = None,
                  hold_s: float | None = None) -> dict:
        with self._lock:
            if budget_bytes is not None:
                self.budget_bytes = max(0, int(budget_bytes))
            if mode is not None:
                self.mode = normalize_mode(mode)
            if hold_s is not None:
                self.hold_s = max(0.0, float(hold_s))
            return {"budget_bytes": self.budget_bytes, "mode": self.mode,
                    "hold_seconds": self.hold_s}

    # -- admission -----------------------------------------------------------
    def admit(self, seq: int, ts_ms: float, paths: Iterable[str],
              facts: dict | None = None, *,
              folder: str | None = None, stem: str | None = None,
              nbytes: int | None = None) -> dict:
        """Register a freshly captured frame and classify it in one step.

        `nbytes` overrides the on-disk measurement — for a caller that already
        knows the footprint, and for tests that must not create gigabytes.
        """
        a = assess(facts or {}, self.mode)
        paths = [str(p) for p in (paths or []) if p]
        nbytes = int(nbytes) if nbytes is not None else sum(_size(p) for p in paths)
        rec = {
            "seq": int(seq), "ts_ms": float(ts_ms or 0.0),
            "paths": paths, "bytes": nbytes,
            "folder": folder, "stem": stem,
            "priority": a["priority"], "interest": a["interest"],
            "reason": a["reason"], "failure": a["failure"],
            "needs_eyes": a["needs_eyes"],
            "mined": True,            # admission happens after local extraction
            "offered_ms": None, "examined_ms": None, "examined_how": None,
            "pinned": False, "expired": False, "archived": None,
        }
        with self._lock:
            self._rec[int(seq)] = rec
            self.stats["admitted"] += 1
            if a["needs_eyes"]:
                self.stats["queued"] += 1
        return dict(rec)

    def refresh_bytes(self, seq: int, paths: Iterable[str] | None = None) -> int:
        """Re-stat a frame's group (files may be written after admission)."""
        with self._lock:
            rec = self._rec.get(int(seq))
            if not rec:
                return 0
            if paths is not None:
                rec["paths"] = [str(p) for p in paths if p]
            elif rec.get("folder") and rec.get("stem"):
                rec["paths"] = stem_group(rec["folder"], rec["stem"])
            rec["bytes"] = sum(_size(p) for p in rec["paths"])
            return rec["bytes"]

    # -- state transitions ---------------------------------------------------
    def mark_offered(self, seqs: Iterable[int], ts_ms: float | None = None) -> int:
        """AgentVision told the agent these frames exist. Offering does NOT
        clear a frame — being told is not the same as having looked."""
        now = float(ts_ms if ts_ms is not None else time.time() * 1000.0)
        n = 0
        with self._lock:
            for s in seqs or []:
                rec = self._rec.get(int(s))
                if rec and rec["offered_ms"] is None:
                    rec["offered_ms"] = now
                    self.stats["offered"] += 1
                    n += 1
        return n

    def mark_examined(self, seq: int, how: str = "read",
                      ts_ms: float | None = None) -> bool:
        """The agent actually consumed this frame. Any tier counts — reading the
        JSON descriptor of a frame is a legitimate examination; the whole point
        of the token ladder is that pixels are the last resort, not the proof."""
        now = float(ts_ms if ts_ms is not None else time.time() * 1000.0)
        with self._lock:
            rec = self._rec.get(int(seq))
            if not rec:
                return False
            first = rec["examined_ms"] is None
            if first:
                rec["examined_ms"] = now
                self.stats["examined"] += 1
                if how == "ack":
                    self.stats["acked"] += 1
            rec["examined_how"] = how
            return first

    def mark_examined_many(self, seqs: Iterable[int], how: str = "read") -> int:
        return sum(1 for s in (seqs or []) if self.mark_examined(int(s), how))

    def set_pinned(self, seqs: Iterable[int], pinned: bool = True) -> int:
        with self._lock:
            n = 0
            for s in seqs or []:
                rec = self._rec.get(int(s))
                if rec and rec["pinned"] != pinned:
                    rec["pinned"] = pinned
                    n += 1
            return n

    def forget(self, seq: int) -> dict | None:
        with self._lock:
            return self._rec.pop(int(seq), None)

    def known(self, seq: int) -> bool:
        with self._lock:
            return int(seq) in self._rec

    def is_failure(self, seq: int) -> bool:
        """Failure-shaped frames are held to a stricter standard of examination:
        a row in a summary does not discharge them, only opening or acking does."""
        with self._lock:
            r = self._rec.get(int(seq))
            return bool(r and r.get("failure"))

    def mark_surveyed(self, seqs: Iterable[int], how: str = "survey") -> int:
        """Clear ORDINARY frames that appeared in a JSON survey, leaving
        failure-shaped ones in the awaiting queue."""
        n = 0
        for s in seqs or []:
            try:
                s = int(s)
            except Exception:
                continue
            if not self.is_failure(s) and self.mark_examined(s, how):
                n += 1
        return n

    def live_stems(self) -> set[str]:
        with self._lock:
            return {r["stem"] for r in self._rec.values() if r.get("stem")}

    # -- queries -------------------------------------------------------------
    def _cleared(self, rec: dict, now_ms: float) -> bool:
        """Deletable? Mined, and either never needed eyes, already examined, or
        past the hold backstop."""
        if not rec.get("mined"):
            return False
        if not rec.get("needs_eyes"):
            return True
        if rec.get("examined_ms") is not None:
            return True
        age_s = (now_ms - float(rec.get("ts_ms") or 0.0)) / 1000.0
        return self.hold_s > 0 and age_s > self.hold_s

    def awaiting(self, limit: int = 50, *, unoffered_only: bool = False,
                 now_ms: float | None = None) -> list[dict]:
        """Frames that need eyes and have not had them, most urgent first.
        This is what the push layer hands to the agent."""
        now = float(now_ms if now_ms is not None else time.time() * 1000.0)
        with self._lock:
            rows = [r for r in self._rec.values()
                    if r.get("needs_eyes") and r.get("examined_ms") is None
                    and not r.get("expired")
                    and (not unoffered_only or r.get("offered_ms") is None)]
        rows.sort(key=lambda r: (r["priority"], -float(r.get("ts_ms") or 0)))
        out = []
        for r in rows[:max(0, int(limit))]:
            age = (now - float(r.get("ts_ms") or 0)) / 1000.0
            remain = (self.hold_s - age) if self.hold_s > 0 else float("inf")
            out.append({
                "seq": r["seq"], "priority": r["priority"],
                "reason": r["reason"], "failure": r["failure"],
                "age_seconds": round(age, 1),
                "hold_expires_in_seconds": (None if remain == float("inf")
                                            else round(max(0.0, remain), 1)),
                "offered": r["offered_ms"] is not None,
                "bytes": r["bytes"],
            })
        return out

    def used_bytes(self) -> int:
        with self._lock:
            return sum(int(r.get("bytes") or 0) for r in self._rec.values()) \
                + int(self.archive_bytes)

    # -- eviction ------------------------------------------------------------
    def plan_eviction(self, now_ms: float | None = None, *,
                      free_bytes: int | None = None,
                      total_bytes: int | None = None) -> dict:
        """Decide what may go, in ascending order of value.

        Tiers, worst-value first:
          0  cleared and unpinned          — served its purpose
          1  hold-expired, never examined  — backstop; counted as a real loss
          2  awaiting inside its hold      — emergency only, capped per pass
          3  pinned to an incident         — NEVER SPENT

        Returns the seqs to drop plus why, without touching the filesystem, so
        this is testable and so the caller can delete outside its own lock.

        `total_bytes` is the size of the volume `free_bytes` was measured on. Pass
        it and a nonsensical free-disk floor cannot hold the emergency path open
        forever; omit it and the floor is trusted as configured.
        """
        now = float(now_ms if now_ms is not None else time.time() * 1000.0)
        with self._lock:
            budget = int(self.budget_bytes)
            used = self.used_bytes()
            high = int(budget * HIGH_WATER)
            low = int(budget * LOW_WATER)

            # The floor may not claim more of the volume than a floor sensibly
            # can. A floor above the disk's own size is a typo, not an emergency.
            floor = int(MIN_FREE_BYTES)
            floor_capped = False
            if total_bytes and floor > int(int(total_bytes) * MIN_FREE_MAX_FRACTION):
                floor = int(int(total_bytes) * MIN_FREE_MAX_FRACTION)
                floor_capped = True

            low_disk = (free_bytes is not None and floor > 0
                        and int(free_bytes) < floor)
            # One pass per cooldown. The disk cannot recover in 100 ms, so ten
            # passes a second only re-decide the same thing ten times — which is
            # how six passes wiped an entire pinned incident in 0.6 s.
            cooling = False
            if low_disk and EMERGENCY_COOLDOWN_S > 0 \
                    and self._last_emergency_ms is not None \
                    and (now - self._last_emergency_ms) < EMERGENCY_COOLDOWN_S * 1000.0:
                low_disk = False
                cooling = True

            if budget <= 0:
                return self._empty_plan(used, budget, "no budget configured",
                                        floor=floor, floor_capped=floor_capped)
            if used < high and not low_disk:
                return self._empty_plan(
                    used, budget,
                    "emergency pass cooling down" if cooling
                    else "under high-water mark",
                    floor=floor, floor_capped=floor_capped)

            # Under budget pressure, fall back to the low-water mark. When the
            # DISK is the thing that is short, the budget is irrelevant — shed a
            # real fraction of what we are actually holding, or the plan comes
            # back empty on a full disk simply because we were under budget.
            target = low if not low_disk else min(low, int(used * 0.5))
            if low_disk:
                self._last_emergency_ms = now

            tiered: list[tuple[int, float, float, int]] = []
            for r in self._rec.values():
                seq = r["seq"]
                if r.get("pinned"):
                    tier = 3
                elif self._cleared(r, now):
                    # A frame that needed eyes and never got them is a loss, not
                    # a routine expiry — separate tier so it is reported.
                    tier = 1 if (r.get("needs_eyes")
                                 and r.get("examined_ms") is None) else 0
                else:
                    tier = 2
                tiered.append((tier, float(r.get("interest") or 0.0),
                               float(r.get("ts_ms") or 0.0), seq))
            # Cheapest tier, then least interesting, then oldest.
            tiered.sort(key=lambda t: (t[0], t[1], t[2]))

            # Ordinary budget pressure may ONLY spend tiers 0 and 1 — frames that
            # have served their purpose. A frame the agent still owes eyes to
            # (tier 2) is not currency for a tight budget; sacrificing those is
            # exactly the failure this module exists to prevent. It becomes
            # eligible only when the DISK itself is about to run out, it goes
            # last, and only EMERGENCY_UNSEEN_PER_PASS of them go per pass.
            #
            # An incident's frozen evidence (tier 3) is never eligible, on any
            # path. "Absolute last resort" still meant spendable, and a disk that
            # is short of space is not a reason to destroy the one thing the
            # capture existed to record. When only pinned frames remain the plan
            # reports unmet bytes and the caller warns — see _recorder_prune.
            allowed = {0, 1, 2} if low_disk else {0, 1}

            evict: list[dict] = []
            unseen_spent = 0
            unseen_withheld = 0
            projected = used
            # used_bytes() counts the archive, but evicting frames can never
            # reclaim archive bytes, and archive_bytes only ratchets up. Judging
            # `projected` against a whole-budget target while decrementing only
            # frame bytes is what made the stop condition unreachable once the
            # archive outgrew the frames — one pass then planned away the whole
            # ledger, deleting every frame and STILL not reaching the target.
            #
            # So frames are only ever asked to pay down to the allowance that is
            # theirs: the target minus the archive's own agreed share. An archive
            # that has grown past its share is the archive's problem, not a
            # reason to spend frames that cannot fix it, and the overflow is
            # reported so the caller can say so.
            arch = int(self.archive_bytes)
            archive_cap = int(budget * ARCHIVE_BUDGET_FRACTION)
            archive_overflow = max(0, arch - archive_cap)
            frame_target = max(0, target - min(arch, archive_cap))
            projected = used - arch
            for tier, interest, ts_ms, seq in tiered:
                if projected <= frame_target:
                    break
                if tier not in allowed:
                    continue
                if tier == 2:
                    if unseen_spent >= EMERGENCY_UNSEEN_PER_PASS:
                        unseen_withheld += 1
                        continue
                    unseen_spent += 1
                rec = self._rec[seq]
                evict.append({
                    "seq": seq, "tier": tier, "bytes": int(rec.get("bytes") or 0),
                    "interest": interest, "reason": rec.get("reason"),
                    "needs_eyes": bool(rec.get("needs_eyes")),
                    "examined": rec.get("examined_ms") is not None,
                    "archive": bool(rec.get("failure")
                                    or float(rec.get("interest") or 0)
                                    >= 0.6),
                })
                projected -= int(rec.get("bytes") or 0)

            # If we cannot reach the target without touching protected frames,
            # say so loudly instead of quietly overshooting the budget.
            unmet = max(0, projected - frame_target)
            pinned_withheld = sum(1 for t, _i, _ts, _s in tiered if t == _TIER_PINNED)
            return {
                "evict": evict, "used_bytes": used, "budget_bytes": budget,
                "target_bytes": target, "projected_bytes": max(0, projected + arch),
                "over_budget": used >= high, "low_disk": low_disk,
                "unmet_bytes": unmet,
                "archive_bytes": arch,
                "archive_over_target": archive_overflow > 0,
                "archive_overflow_bytes": archive_overflow,
                "frame_target_bytes": frame_target,
                "pinned_withheld": pinned_withheld,
                "unseen_spent": unseen_spent,
                "unseen_withheld": unseen_withheld,
                "free_disk_floor_bytes": floor,
                "free_disk_floor_capped": floor_capped,
                "protected_from_eviction": sorted(
                    {t for t, _i, _ts, _s in tiered} - allowed),
                "reason": ("free disk below floor" if low_disk
                           else "over high-water mark"),
            }

    def _empty_plan(self, used: int, budget: int, why: str, *,
                    floor: int | None = None,
                    floor_capped: bool = False) -> dict:
        return {"evict": [], "used_bytes": used, "budget_bytes": budget,
                "target_bytes": int(budget * LOW_WATER),
                "projected_bytes": used, "over_budget": False,
                "low_disk": False, "reason": why,
                "free_disk_floor_bytes": (int(MIN_FREE_BYTES) if floor is None
                                          else int(floor)),
                "free_disk_floor_capped": bool(floor_capped)}

    def commit_eviction(self, plan: dict, *,
                        archive_dir: str | Path | None = None,
                        deleter=None) -> dict:
        """Apply a plan: archive what is worth keeping, delete the file group,
        drop the record. `deleter` is injectable so tests never touch real files.
        """
        rows = list((plan or {}).get("evict") or [])
        if not rows:
            return {"evicted": 0, "bytes_reclaimed": 0, "archived": 0,
                    "dropped_unexamined": 0, "dropped_pinned": 0,
                    "dropped_unseen": 0, "refused_pinned": 0,
                    "kept_unarchived": 0, "evicted_seqs": []}
        unlink = deleter or self._unlink
        archive_cap = int(self.budget_bytes * ARCHIVE_BUDGET_FRACTION)

        reclaimed = archived = unexamined = pinned_lost = 0
        unseen_lost = refused_pinned = kept_unarchived = 0
        evicted_seqs: list[int] = []
        for row in rows:
            seq = int(row["seq"])
            with self._lock:
                rec = self._rec.get(seq)
            if not rec:
                continue

            # The plan is a snapshot; the RECORD is current. A plan row can be
            # stale, hand-built, or carried over from before an incident froze
            # this frame — so pin state is re-read here, at the point of
            # deletion, and pinned evidence is never spent.
            if rec.get("pinned"):
                refused_pinned += 1
                continue

            archived_path = None
            if (row.get("archive") and archive_dir
                    and self.archive_bytes < archive_cap):
                png = self._primary_image(rec)
                if png:
                    res = archive_frame(png, archive_dir)
                    if res.get("ok"):
                        archived += 1
                        with self._lock:
                            self.archive_bytes += int(res.get("bytes") or 0)
                            self.stats["archived"] += 1
                            self.stats["archive_bytes"] += int(res.get("bytes") or 0)
                        rec["archived"] = res.get("path")
                        archived_path = res.get("path")

            # A frame the agent was TOLD to look at and has not (tier 2) only
            # loses its pixels once a compressed copy provably exists on disk.
            # Not "we called archive_frame" — os.path.exists on the result. The
            # archive can be capped out, Pillow can be missing, an encoder can
            # refuse the image; in every one of those cases this frame's pixels
            # are the only pixels there are, so they stay and the record stays
            # with them so it can still be examined.
            if int(row.get("tier") or 0) == 2:
                if not (archived_path and os.path.exists(str(archived_path))):
                    kept_unarchived += 1
                    continue

            # Keep the sidecar JSON, the thumbnail, and any authored annotations:
            # that is the permanent history. `_annotations.json` holds notes
            # Claude wrote for its next session and has no archive equivalent —
            # it was the one irreplaceable thing in the group being unlinked, on
            # the ORDINARY path as well as the emergency one.
            for p in list(rec.get("paths") or []):
                if p.endswith("_frame.json") or p.endswith("_thumb.png") \
                        or p.endswith("_annotations.json"):
                    continue
                n = _size(p)
                if unlink(p):
                    reclaimed += n

            evicted_seqs.append(seq)
            if row.get("tier") == 1:
                unexamined += 1
            elif row.get("tier") == 2:
                unseen_lost += 1
            elif row.get("tier") == 3:
                pinned_lost += 1

            with self._lock:
                self._rec.pop(seq, None)

        with self._lock:
            self.stats["evictions_run"] += 1
            self.stats["evicted"] += len(evicted_seqs)
            self.stats["bytes_reclaimed"] += reclaimed
            self.stats["dropped_unexamined"] += unexamined
            self.stats["dropped_unseen"] += unseen_lost
            self.stats["dropped_pinned"] += pinned_lost
            self.stats["refused_pinned"] += refused_pinned
            self.stats["kept_unarchived"] += kept_unarchived

        return {"evicted": len(evicted_seqs), "bytes_reclaimed": reclaimed,
                "archived": archived, "dropped_unexamined": unexamined,
                "dropped_unseen": unseen_lost,
                "dropped_pinned": pinned_lost,
                "refused_pinned": refused_pinned,
                "kept_unarchived": kept_unarchived,
                "evicted_seqs": evicted_seqs}

    @staticmethod
    def _primary_image(rec: dict) -> str | None:
        """The full frame PNG — not the annotated copy, not the thumb."""
        stem = rec.get("stem") or ""
        for p in rec.get("paths") or []:
            name = os.path.basename(p)
            if name == f"{stem}.png":
                return p
        for p in rec.get("paths") or []:
            if p.endswith(".png") and "_thumb" not in p:
                return p
        return None

    @staticmethod
    def _unlink(path: str) -> bool:
        try:
            if path and os.path.exists(path):
                os.unlink(path)
                return True
        except Exception:
            return False
        return False

    def reset(self) -> None:
        """Forget every tracked frame (does not touch the filesystem)."""
        with self._lock:
            self._rec.clear()
            self.archive_bytes = 0
            for k in self.stats:
                self.stats[k] = 0

    def note_orphan_sweep(self, res: dict) -> None:
        with self._lock:
            self.stats["orphans_removed"] += int(res.get("removed") or 0)
            self.stats["orphan_bytes"] += int(res.get("bytes_freed") or 0)

    # -- reporting -----------------------------------------------------------
    def report(self, now_ms: float | None = None) -> dict:
        now = float(now_ms if now_ms is not None else time.time() * 1000.0)
        with self._lock:
            recs = list(self._rec.values())
            used = self.used_bytes()
            budget = int(self.budget_bytes)
            stats = dict(self.stats)
            mode, hold = self.mode, self.hold_s
            arch = int(self.archive_bytes)

        need = [r for r in recs if r.get("needs_eyes")]
        seen = [r for r in need if r.get("examined_ms") is not None]
        held = [r for r in need if r.get("examined_ms") is None]
        cleared = [r for r in recs if self._cleared(r, now)]
        pct = (used / budget * 100.0) if budget else 0.0

        return {
            "policy": {
                "mode": mode,
                "modes_available": {
                    "off": "mine locally only; never ask the agent to look",
                    "errors": "only frames aligned with errors/crashes/failure "
                              "visual events (default)",
                    "changes": "errors plus every frame that visibly changed",
                    "all": "every frame — video-like continuity; pair with a "
                           "slower capture interval",
                },
                "hold_seconds": hold,
                "hold_meaning": ("a frame that needs eyes is never deleted for "
                                 "this long; after that the backstop clears it "
                                 "and it is counted in dropped_unexamined"),
            },
            "budget": {
                "bytes": budget, "human": _human(budget),
                "used_bytes": used, "used_human": _human(used),
                "used_pct": round(pct, 1),
                "archive_bytes": arch, "archive_human": _human(arch),
                "high_water_pct": round(HIGH_WATER * 100, 1),
                "low_water_pct": round(LOW_WATER * 100, 1),
                "min_free_disk": _human(MIN_FREE_BYTES),
                "note": ("deletion is driven by this budget, not by a clock — a "
                         "frame is only evicted once it has been examined or "
                         "the hold expires"),
            },
            "frames": {
                "live": len(recs),
                "need_eyes": len(need),
                "examined": len(seen),
                "awaiting_examination": len(held),
                "cleared_deletable": len(cleared),
                "pinned_by_incident": sum(1 for r in recs if r.get("pinned")),
            },
            "totals": stats,
            "integrity": {
                "dropped_unexamined": stats["dropped_unexamined"],
                # Tier 2 used to be counted in neither counter, so must-see
                # frames could die with every number reading zero.
                "dropped_unseen": stats["dropped_unseen"],
                "dropped_pinned": stats["dropped_pinned"],
                "refused_pinned": stats["refused_pinned"],
                "kept_unarchived": stats["kept_unarchived"],
                "ok": stats["dropped_unexamined"] == 0
                      and stats["dropped_unseen"] == 0
                      and stats["dropped_pinned"] == 0,
                "meaning": ("dropped_unexamined > 0 means frames the policy "
                            "wanted the agent to see expired first; "
                            "dropped_unseen > 0 means a free-disk emergency "
                            "spent frames still inside their hold — lower the "
                            "capture rate, raise the budget, or examine sooner"),
            },
        }


def _human(n: int | float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} TB"


#: Process-wide ledger the bridge uses. Tests build their own instances.
LEDGER = Ledger()


def push_sentence(rows: list[dict], *, mode: str | None = None) -> str:
    """One compact line for the push channel. This is the whole "the agent knows
    what was just shot" mechanism: cheap to inject, names the seqs, says why,
    and points at the cheapest tool that discharges the obligation."""
    if not rows:
        return ""
    n = len(rows)
    fails = [r for r in rows if r.get("failure")]
    seqs = [r["seq"] for r in rows]
    span = (f"{min(seqs)}" if len(seqs) == 1
            else f"{min(seqs)}-{max(seqs)}")
    head = f"{n} frame{'s' if n != 1 else ''} awaiting your eyes (seq {span})"
    if fails:
        why = str(fails[0].get("reason") or "failure")
        head += f"; {len(fails)} failure-aligned — {why[:70]}"
    else:
        head += f"; {str(rows[0].get('reason') or '')[:70]}"
    soonest = [r.get("hold_expires_in_seconds") for r in rows
               if r.get("hold_expires_in_seconds") is not None]
    if soonest:
        head += f"; oldest expires in {int(min(soonest))}s"
    pick = (fails[0] if fails else rows[0])["seq"]
    return (head + f". Cheapest: av_frame_json({pick}) then "
                   f"av_frame_region({pick}); av_examine_ack to release.")
