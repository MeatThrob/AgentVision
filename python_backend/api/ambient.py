"""
AgentVision — AMBIENT ENGINE (Push Mode)
========================================

PULL mode (everything else in AgentVision) waits for the agent to ask. That has a
hole: **the agent has to already suspect something is wrong.** After a compaction
it has usually forgotten AgentVision exists at all.

PUSH mode closes that hole. A tiny hook injects a few lines of pre-digested state
into the conversation at the moments that matter — session start, after a
compaction, before a prompt is answered, after an Edit/Write/Bash — so the agent
learns "the thing you just changed made the program crash" without spending a
single tool call.

THE THREE RULES THIS FILE ENFORCES
----------------------------------
1. **SILENT BY DEFAULT.** If nothing meaningful changed, inject NOTHING. An
   ambient channel that chatters is worse than no channel: it burns tokens on
   every prompt and trains the agent to ignore it.
2. **DELTA-ONLY.** Never re-say what this session has already been told. Every
   surfaced signal is fingerprinted and remembered per session; a repeat is
   suppressed unless it escalates in severity.
3. **HARD BYTE CAPS.** Each tier has a byte ceiling enforced by truncation, so a
   pathological program can never blow the context window through the hook.

Severity tiers (a tier is chosen, not accumulated):

    silent     nothing worth saying                        0 bytes
    heartbeat  "still watching, all normal"      <= HEARTBEAT_CAP   (rare)
    notice     something changed worth knowing   <= NOTICE_CAP
    alert      something is broken NOW           <= ALERT_CAP

The visual sentence in every non-silent injection comes from the real visual
engine (change_score / changed_bbox / one_line_summary / visual events /
flight-recorder incidents / image_health) — never a parallel reimplementation.

Pure stdlib. No Flask import. Callers pass state in; this module decides what to
say. That keeps it unit-testable without a bridge and without a screen.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time

# ── Byte caps (hard, enforced by truncation) ──────────────────────────────────
# 220 was too tight to carry any actual information: a heartbeat came in at 211
# bytes of which ~100 were the same standing instruction every time, which left
# no room for the one genuinely useful free fact — WHAT is on screen. 340 is still
# ~85 tokens and buys a screen-text snippet that regularly saves a whole tool call.
HEARTBEAT_CAP = int(os.environ.get("AGENTVISION_AMBIENT_HEARTBEAT_CAP", "340"))
#: Deliver new RAW log lines even when AgentVision itself has nothing to say.
#: On by default: the program's output is evidence, not commentary, and it is not
#: AgentVision's place to decide the agent does not need to see it.
RAW_ALWAYS = os.environ.get("AGENTVISION_RAW_ALWAYS", "1") not in ("0", "", "false")
NOTICE_CAP    = int(os.environ.get("AGENTVISION_AMBIENT_NOTICE_CAP", "700"))
ALERT_CAP     = int(os.environ.get("AGENTVISION_AMBIENT_ALERT_CAP", "1200"))

# ── Rate limiting / coalescing ────────────────────────────────────────────────
# Minimum gap between two injections of the same tier for one session. An alert
# is allowed through much sooner than a heartbeat.
MIN_GAP_MS = {
    "heartbeat": float(os.environ.get("AGENTVISION_AMBIENT_GAP_HEARTBEAT_MS", "600000")),
    "notice":    float(os.environ.get("AGENTVISION_AMBIENT_GAP_NOTICE_MS", "45000")),
    "alert":     float(os.environ.get("AGENTVISION_AMBIENT_GAP_ALERT_MS", "10000")),
}
# A heartbeat is only ever emitted on these hook events — never on every prompt.
HEARTBEAT_EVENTS = {"SessionStart"}

# Severity ordering, for "may a repeat escalate?" decisions.
TIER_RANK = {"silent": 0, "heartbeat": 1, "notice": 2, "alert": 3}

# ── Stop-hook backstop safety ─────────────────────────────────────────────────
# A Stop hook that blocks can trap the user in a loop, so it is OFF by default
# and, when on, is bounded three ways: only genuinely critical signals qualify,
# a per-session block budget, and the signal is marked surfaced BEFORE the block
# is returned so the same incident can never re-fire.
STOP_BLOCK_ENABLED = os.environ.get("AGENTVISION_AMBIENT_STOP_BLOCK", "0") not in ("0", "", "false")
MAX_STOP_BLOCKS    = int(os.environ.get("AGENTVISION_AMBIENT_MAX_STOP_BLOCKS", "1"))
# Only these signal kinds may ever block a Stop. A merely-degraded health score
# must NEVER qualify — that is the difference between a useful backstop and a
# tool that will not let the user leave.
STOP_BLOCK_KINDS = {"program_died", "crash", "fatal", "hang"}


def _now_ms() -> float:
    return time.time() * 1000.0


class SessionMemory:
    """Per-session record of what has already been pushed.

    Keyed by an opaque session id supplied by the hook (Claude Code gives one).
    Bounded so a long-lived bridge cannot leak: oldest sessions are evicted."""

    MAX_SESSIONS = int(os.environ.get("AGENTVISION_AMBIENT_MAX_SESSIONS", "40"))
    MAX_FP_PER_SESSION = int(os.environ.get("AGENTVISION_AMBIENT_MAX_FP", "300"))

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}

    def _sess(self, sid: str) -> dict:
        s = self._sessions.get(sid)
        if s is None:
            s = {"created_ms": _now_ms(), "last_seen_ms": _now_ms(),
                 "surfaced": {},          # fingerprint -> {tier, ts_ms, count}
                 "last_tier_ms": {},      # tier -> ts_ms
                 "injections": 0, "bytes_pushed": 0, "silent_calls": 0,
                 "stop_blocks": 0}
            self._sessions[sid] = s
            if len(self._sessions) > self.MAX_SESSIONS:
                oldest = sorted(self._sessions.items(),
                                key=lambda kv: kv[1]["last_seen_ms"])
                for k, _ in oldest[:len(self._sessions) - self.MAX_SESSIONS]:
                    self._sessions.pop(k, None)
        s["last_seen_ms"] = _now_ms()
        return s

    def already_surfaced(self, sid: str, fp: str, tier: str) -> bool:
        """True if this exact signal was already pushed to this session at an
        equal-or-higher severity. An escalation (notice -> alert) is allowed."""
        with self._lock:
            s = self._sess(sid)
            prev = s["surfaced"].get(fp)
            if not prev:
                return False
            return TIER_RANK.get(tier, 0) <= TIER_RANK.get(prev["tier"], 0)

    def mark_surfaced(self, sid: str, fp: str, tier: str,
                      text: str = "", kind: str = "") -> None:
        with self._lock:
            s = self._sess(sid)
            prev = s["surfaced"].get(fp)
            s["surfaced"][fp] = {
                "tier": tier if not prev else
                        (tier if TIER_RANK.get(tier, 0) > TIER_RANK.get(prev["tier"], 0)
                         else prev["tier"]),
                "ts_ms": _now_ms(),
                "count": (prev or {}).get("count", 0) + 1,
                # Retained so a later CORRECTION can quote the claim it overturns
                # verbatim. Without the original text the correction would have to
                # say "the thing I mentioned earlier", forcing the agent to go
                # find it — in the part of the context where recall is worst.
                "text": text or (prev or {}).get("text", ""),
                "kind": kind or (prev or {}).get("kind", ""),
            }
            if len(s["surfaced"]) > self.MAX_FP_PER_SESSION:
                oldest = sorted(s["surfaced"].items(), key=lambda kv: kv[1]["ts_ms"])
                for k, _ in oldest[:len(s["surfaced"]) - self.MAX_FP_PER_SESSION]:
                    s["surfaced"].pop(k, None)

    def claims(self, sid: str) -> list:
        """[(fp, record)] for everything surfaced to this session — the ledger of
        what AgentVision actually asserted. Snapshotted under the lock."""
        with self._lock:
            return list(self._sess(sid)["surfaced"].items())

    def rate_limited(self, sid: str, tier: str) -> bool:
        gap = MIN_GAP_MS.get(tier)
        if not gap:
            return False
        with self._lock:
            s = self._sess(sid)
            last = s["last_tier_ms"].get(tier)
        return bool(last and (_now_ms() - last) < gap)

    def record_injection(self, sid: str, tier: str, n_bytes: int) -> None:
        with self._lock:
            s = self._sess(sid)
            s["last_tier_ms"][tier] = _now_ms()
            s["injections"] += 1
            s["bytes_pushed"] += n_bytes

    def record_silent(self, sid: str) -> None:
        with self._lock:
            self._sess(sid)["silent_calls"] += 1

    def stop_blocks(self, sid: str) -> int:
        with self._lock:
            return self._sess(sid)["stop_blocks"]

    def record_stop_block(self, sid: str) -> int:
        with self._lock:
            s = self._sess(sid)
            s["stop_blocks"] += 1
            return s["stop_blocks"]

    def stats(self, sid: str | None = None) -> dict:
        with self._lock:
            if sid is not None:
                s = dict(self._sess(sid))
                s["surfaced_count"] = len(s.pop("surfaced", {}))
                return s
            return {
                "sessions_tracked": len(self._sessions),
                "total_injections": sum(v["injections"] for v in self._sessions.values()),
                "total_bytes_pushed": sum(v["bytes_pushed"] for v in self._sessions.values()),
                "total_silent_calls": sum(v["silent_calls"] for v in self._sessions.values()),
            }

    def reset(self, sid: str | None = None) -> None:
        with self._lock:
            if sid is None:
                self._sessions.clear()
            else:
                self._sessions.pop(sid, None)


MEMORY = SessionMemory()


# ── Signal extraction ─────────────────────────────────────────────────────────

def _fp(*parts) -> str:
    """Stable short fingerprint for a signal, so a repeat can be suppressed."""
    h = hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8", "replace"))
    return h.hexdigest()[:12]


def build_signals(state: dict) -> list[dict]:
    """Turn raw bridge state into candidate ambient signals, most severe first.

    `state` is assembled by the caller (the /ambient route) from machinery that
    already exists — the digest, the visual engine, the flight recorder — so this
    function never reimplements detection. Each signal is
    {kind, tier, fp, text, next} where `text` is ONE short sentence.
    """
    out: list[dict] = []

    prog = state.get("program") or {}
    cap = state.get("capture") or {}
    vis = state.get("visual") or {}
    incidents = state.get("incidents") or []
    events = state.get("visual_events") or []
    errors = state.get("new_errors") or []
    health = state.get("health") or {}

    # ── ALERT tier: something is broken right now ────────────────────────────

    # A frozen incident is the highest-value thing we can say, because the
    # pre-error window is already on disk.
    for inc in incidents[-2:]:
        kind = str(inc.get("kind") or "incident")
        out.append({
            "kind": "crash" if kind == "error" else ("hang" if kind == "screen_frozen" else kind),
            "tier": "alert",
            "fp": _fp("incident", inc.get("id")),
            "text": (f"AgentVision froze an incident ({kind}) at frame "
                     f"{inc.get('trigger_seq')}: {str(inc.get('detail') or '')[:160]} "
                     f"— the {inc.get('pre_error_seconds')}s BEFORE it is already on disk."),
            "next": (f"av_error_moment(seq={inc.get('trigger_seq')})"
                     f" · av_incidents(id='{inc.get('id')}')"),
            "incident_id": inc.get("id"),
        })

    if prog and prog.get("was_running") and not prog.get("running"):
        out.append({
            "kind": "program_died", "tier": "alert",
            "fp": _fp("program_died", prog.get("name"), prog.get("died_at_ms")),
            "text": (f"The program AgentVision is watching "
                     f"({prog.get('name')}) is NO LONGER RUNNING."),
            "next": "av_diagnose() · av_program_log(lines=40)",
        })

    for ev in events[-2:]:
        et = ev.get("type")
        if et == "on_screen_error":
            lines = ev.get("on_screen_text") or []
            out.append({
                "kind": "fatal", "tier": "alert",
                "fp": _fp("on_screen_error", ev.get("id")),
                "text": ("Error text is VISIBLE ON SCREEN: "
                         + " / ".join(str(x)[:90] for x in lines[:2])),
                "next": f"av_error_moment(seq={ev.get('seq')})",
            })
        elif et == "screen_frozen":
            out.append({
                "kind": "hang", "tier": "alert",
                "fp": _fp("screen_frozen", ev.get("id")),
                "text": (f"The screen has been FROZEN for "
                         f"{ev.get('still_for_s')}s (possible hang) since frame "
                         f"{ev.get('seq')} — no log line will tell you this."),
                "next": f"av_frame_json(seq={ev.get('seq')}) · av_visual_events()",
            })
        elif et == "blank_screen":
            out.append({
                "kind": "blank", "tier": "notice",
                "fp": _fp("blank_screen", ev.get("id")),
                "text": (f"The captured screen went BLANK/BLACK at frame "
                         f"{ev.get('seq')} (window minimized, occluded, or not painting)."),
                "next": f"av_frame_json(seq={ev.get('seq')}) · av_capture_status()",
            })

    for e in errors[:2]:
        out.append({
            "kind": "new_error", "tier": "alert",
            "fp": _fp("error_fp", e.get("fingerprint")),
            "text": (f"NEW error this session ({e.get('count', 1)}x): "
                     f"{str(e.get('sample') or '')[:170]}"
                     + (f" — likely: {str(e.get('probable_cause'))[:90]}"
                        if e.get("probable_cause") else "")),
            "next": f"av_error_moment(fingerprint='{e.get('fingerprint')}')",
        })

    # ── STANDING conditions: broken RIGHT NOW, even if we didn't witness it ───
    # The signals above are all DELTAS the bridge personally observed. That left
    # a hole: a bridge started (or restarted) AFTER a crash saw `was_running`
    # False and no session-new fingerprints, so it stayed silent about a program
    # that /diagnose was simultaneously reporting as dead with a known error.
    # These signals close it. Per-fingerprint suppression means each is said at
    # most once per session, so this stays cheap.
    standing = state.get("standing_errors") or []
    prog_down = bool(prog) and prog.get("running") is False

    if prog_down and not prog.get("was_running") and standing:
        # Not a witnessed death, but it left errors behind — so it CRASHED
        # before we started watching, and that IS news. A program that simply
        # was never started stays SILENT here on purpose (the same reason the
        # witnessed-death signal above gates on `was_running`): "not started"
        # is not news, and silence-by-default is what keeps push mode cheaper
        # than the agent asking.
        out.append({
            "kind": "program_down", "tier": "alert",
            "fp": _fp("program_down", prog.get("name"),
                      standing[0].get("fingerprint")),
            "text": (f"The program AgentVision is watching ({prog.get('name')}) "
                     f"is NOT running and left {len(standing)} error(s) behind "
                     f"— it looks like it crashed before this session."),
            "next": "av_diagnose() · av_program_log(lines=40)",
        })

    for e in standing[:2]:
        fatalish = any(k in f"{e.get('exception_type') or ''} {e.get('sample') or ''}".lower()
                       for k in ("fatal", "panic", "segv", "abort", "oom", "crash"))
        out.append({
            "kind": "standing_error",
            "tier": "alert" if (fatalish or prog_down) else "notice",
            "fp": _fp("error_fp", e.get("fingerprint")),   # dedupes vs new_error
            "text": (f"Error on record ({e.get('count', 1)}x): "
                     f"{str(e.get('sample') or '')[:150]}"
                     + (f" — likely: {str(e.get('probable_cause'))[:90]}"
                        if e.get("probable_cause") else "")),
            "next": f"av_error_moment(fingerprint='{e.get('fingerprint')}')",
        })

    # ── FRAMES AWAITING EXAMINATION ──────────────────────────────────────────
    # The reason capture is worth doing at all. Screenshots the agent never hears
    # about are shots taken for nothing, so retention holds a failure-aligned
    # frame until it has been examined — and this is how the agent gets told,
    # for free, before the hold expires.
    #
    # The fingerprint is bucketed by the oldest frame's age in whole minutes, so
    # a batch that stays unexamined is re-offered about once a minute instead of
    # being said once and then suppressed forever. A pending obligation that
    # silently stops being mentioned is the same bug as deleting the frame.
    aw = state.get("frames_awaiting") or {}
    rows = aw.get("rows") or []
    if rows:
        oldest = max((r.get("age_seconds") or 0) for r in rows)
        failing = [r for r in rows if r.get("failure")]
        expiring = [r.get("hold_expires_in_seconds") for r in rows
                    if r.get("hold_expires_in_seconds") is not None]
        soonest = min(expiring) if expiring else None
        urgent = bool(failing) or (soonest is not None and soonest < 120)
        seqs = [r.get("seq") for r in rows]
        span = (str(seqs[0]) if len(seqs) == 1
                else f"{min(seqs)}-{max(seqs)}")
        why = str((failing[0] if failing else rows[0]).get("reason") or "")[:110]
        pick = (failing[0] if failing else rows[0]).get("seq")
        out.append({
            "kind": "frames_awaiting",
            "tier": "alert" if urgent else "notice",
            "fp": _fp("frames_awaiting", span, int(oldest // 60)),
            "text": (f"{len(rows)} captured frame(s) are waiting on your eyes "
                     f"(seq {span}"
                     + (f", {len(failing)} failure-aligned" if failing else "")
                     + f"): {why}."
                     + (f" The oldest is released in {int(soonest)}s and its "
                        f"pixels are then reclaimed."
                        if soonest is not None else "")),
            "next": (f"av_frame_json({pick}) · av_frame_region({pick})"
                     f" · av_examine_ack(seqs=[{span.replace('-', ',')}])"),
            "awaiting_seqs": seqs,
            # The only signal with an expiry: it gets a reserved slot in decide()
            # so the 3-signal cap can never silently drop it.
            "deadline": True,
        })

    # ── NOTICE tier: worth knowing, not on fire ──────────────────────────────

    if cap.get("window_missing"):
        out.append({
            "kind": "window_missing", "tier": "notice",
            "fp": _fp("window_missing", cap.get("capture_app")),
            "text": (f"AgentVision cannot find the '{cap.get('capture_app')}' window "
                     "— frames are being SKIPPED (the desktop is never "
                     "captured). Is the program running?"),
            "next": "av_capture_status()",
        })

    if cap.get("engine_running") is False and cap.get("frames_stored"):
        out.append({
            "kind": "capture_stopped", "tier": "notice",
            "fp": _fp("capture_stopped"),
            "text": "Screen capture is STOPPED, so visual state is going stale.",
            "next": "av_capture_start()",
        })

    if state.get("preflight_gap"):
        out.append({
            "kind": "preflight_gap", "tier": "notice",
            "fp": _fp("preflight_gap", state.get("preflight_gap")),
            "text": (f"AgentVision cannot specifically parse "
                     f"{state.get('preflight_gap')} of this program's log formats, "
                     "so log correlation is degraded."),
            "next": "av_preflight()",
        })

    score = health.get("score")
    if isinstance(score, (int, float)) and score < 60:
        out.append({
            # NOTE: deliberately NOT in STOP_BLOCK_KINDS — a degraded score must
            # never be allowed to block the user from ending their turn.
            "kind": "health_degraded", "tier": "notice",
            "fp": _fp("health", int(score // 10)),   # bucketed: no per-point spam
            "text": f"AgentVision health score is {score}/100.",
            "next": "av_diagnose()",
        })

    out.sort(key=lambda s: -TIER_RANK.get(s["tier"], 0))
    return out


def visual_sentence(state: dict, brief: bool = False) -> str:
    """ONE sentence describing what the screen is doing, from the real visual
    engine. Present in EVERY non-silent injection — it is the thing no log can
    tell the agent."""
    vis = state.get("visual") or {}
    cap = state.get("capture") or {}
    if not cap.get("frames_stored"):
        return "Visual: no frames captured yet."
    seq = vis.get("latest_seq")
    changed = vis.get("changed_moments")
    total = vis.get("frames_considered")
    cs = vis.get("latest_change_score")
    bits = []
    if vis.get("is_blank"):
        bits.append("screen is BLANK/BLACK")
    elif cs is None:
        bits.append("screen state not yet analyzed")
    elif cs <= 0.0:
        still = vis.get("still_for_s")
        bits.append("screen is STATIC" + (f" (~{still}s unchanged)" if still else ""))
    else:
        bits.append(f"screen changing ({cs * 100:.0f}% of the last frame)")
        bb = vis.get("changed_bbox")
        if bb and not brief:
            bits.append(f"changed region {bb[2]}x{bb[3]} at ({bb[0]},{bb[1]})")
    if not brief and changed is not None and total:
        bits.append(f"{changed} changed moment(s) in the last {total} frames")
    if vis.get("on_screen_error_text"):
        bits.append("ERROR TEXT visible on screen")
    line = "Visual: " + "; ".join(bits) + "."
    # WHAT the screen says, not just whether it moved. Free — the capture path
    # already OCR'd it, and the text used to be discarded after an error-keyword
    # test. Without this an injection could report "screen is STATIC" while the
    # agent still had to spend a call to learn it was sitting on a title menu.
    # Included even in brief/heartbeat form (shorter), because the heartbeat is
    # the injection that fires most often and was carrying no information at all.
    snip = vis.get("on_screen_text")
    # WHOSE text is it? OCR is throttled, so the newest frame is usually NOT the
    # one that was scanned. Reporting the snippet beside "(latest frame #N)"
    # attributed one frame's text to another — measured: the push quoted frame
    # #100's OCR while naming #4321, which had never been scanned at all. If the
    # snippet is not from the frame being described, say which frame it IS from.
    ocr_seq = vis.get("ocr_seq")
    from_other_frame = (snip and ocr_seq is not None and seq is not None
                        and int(ocr_seq) != int(seq))
    if snip:
        # "Screen reads:" was a quotation — a claim of verbatim fidelity that OCR
        # cannot honour. MEASURED: Apple Vision read `src=0x5D80000` as
        # `0x5080000` and reported confidence 1.0 on every line, including one
        # that was pure noise. Name the transcription step so the agent knows
        # this is a machine reading and not the pixels themselves. Costs ~4 tokens.
        _whose = (f" from frame #{ocr_seq}, NOT the frame below"
                  if from_other_frame else "")
        line += (f' Screen OCR (approximate{_whose}): '
                 f'"{snip[:70] if brief else snip}".')
        unver = [t["token"] for t in (vis.get("ocr_tokens") or [])
                 if not t.get("appears_in_log")]
        if unver:
            line += (f' NOT corroborated by the aligned log: {", ".join(unver[:4])}'
                     f' — confirm against the log or a full-res crop before'
                     f' relying on any of them.')
    elif str(vis.get("ocr_state") or "") == "could_not_read":
        # Absence of OCR text is NOT absence of on-screen text.
        line += (" OCR could not read this frame — that is not the same as the"
                 " screen being clean; look at the image if it matters.")
    if not brief and seq is not None:
        line += f" (latest frame #{seq})"
    return line


def _truncate(text: str, cap: int) -> tuple[str, bool]:
    b = text.encode("utf-8")
    if len(b) <= cap:
        return text, False
    cut = b[:max(0, cap - 3)].decode("utf-8", "ignore").rstrip()
    return cut + "...", True


def render(tier: str, signals: list[dict], state: dict, event: str) -> str:
    """Render the injection text for a tier. Terse, plain text, no JSON — this
    goes straight into the conversation, so every byte must earn its place."""
    if tier == "silent":
        return ""
    prof = (state.get("profile") or "?")
    prog = (state.get("program") or {}).get("name") or "?"

    if tier == "heartbeat":
        # Kept deliberately short so it fits HEARTBEAT_CAP without truncating
        # mid-sentence — a heartbeat that gets cut off looks broken.
        # The heartbeat carries the raw log too. "Nothing needs attention" is
        # AgentVision's OPINION; the program's actual output is not optional, and
        # the heartbeat is the injection that fires most often.
        short = prog if len(prog) <= 34 else prog[:31] + "..."
        # The standing "don't screenshot yourself" instruction is already in the
        # MCP server instructions the agent receives every session, so repeating
        # it in full here bought nothing and crowded out the screen text.
        txt = (f"[AgentVision] Watching {short}. {visual_sentence(state, brief=True)} "
               f"Nothing needs attention — ask av_diagnose / av_start_here "
               f"rather than screenshotting or grepping yourself.")
        head = _truncate(txt, HEARTBEAT_CAP)[0]
        raw = render_raw_block(state)
        return head + ("\n" + raw if raw else "")

    cap_bytes = ALERT_CAP if tier == "alert" else NOTICE_CAP
    head = ("[AgentVision ALERT]" if tier == "alert" else "[AgentVision]")
    lines = [f"{head} {prog} (profile '{prof}')"]
    if event == "PostToolUse":
        lines.append("Since your last change:")
    for s in signals[:3]:
        lines.append(f"  - {s['text']}")
        if s.get("next"):
            lines.append(f"    -> {s['next']}")
    lines.append(f"  {visual_sentence(state)}")
    if tier == "alert":
        lines.append("  This was pushed to you for free — no tool call was spent. "
                     "Use the -> calls above rather than re-deriving it.")

    # ── THE RAW LOG, forced ──────────────────────────────────────────────────
    # Appended AFTER the byte cap is applied to the prose, and carrying its own
    # budget, because it must never be squeezed out by AgentVision's own
    # commentary. The whole point: the agent gets the program's ACTUAL output,
    # not this module's opinion of it. The summary above is a convenience; these
    # bytes are the evidence. (Measured on this project: the summary said
    # "health 100, program looks healthy" while 180 GPU present failures sat in
    # exactly these lines, and the real bug was only visible in their raw
    # `target=`/`tex0=` fields.)
    head = _truncate("\n".join(lines), cap_bytes)[0]
    raw = render_raw_block(state)
    return head + ("\n" + raw if raw else "")


def render_raw_block(state: dict) -> str:
    """The raw new log lines for this injection, verbatim.

    Consecutive identical lines arrive pre-collapsed as {line, repeat:N} — that
    is lossless, unlike dropping or re-levelling a line, which this never does.
    """
    blk = state.get("raw_log") or {}
    all_srcs = blk.get("sources") or []
    srcs = [s for s in all_srcs if s.get("lines")]

    # A DEAD LOG IS WORTH SAYING EVEN WITH NOTHING TO SHOW. A source that has not
    # been written for a long while, while the program is supposedly running, is
    # usually a misconfigured path — output going to stderr or elsewhere. Reading
    # a stale file as if it were live output cost real time on this project, and
    # a raw tail cannot warn you about it.
    stale = [s for s in all_srcs if s.get("stale") and not s.get("lines")]
    prog_up = bool((state.get("program") or {}).get("running"))
    warn: list[str] = []
    if prog_up and stale:
        for s in stale:
            age = s.get("last_write_age_s")
            warn.append(
                f"  ! [{s.get('label') or s.get('path')}] has had NO writes for "
                f"{int(age or 0)}s while the program is running — it is probably "
                f"not this run's output: {s.get('path')}")
        # Turn the suspicion into an ANSWER when we know it: the OS was asked
        # which descriptors the process actually holds, so name the real
        # destination instead of leaving the agent to guess "stderr? other path?".
        wt = state.get("write_targets") or {}
        if wt.get("available") and not wt.get("ok"):
            for d in (wt.get("output_destination") or [])[:2]:
                warn.append(f"    -> the process's {d}")
            for m in (wt.get("missing_from_config") or [])[:2]:
                warn.append(f"    -> it IS writing {m.get('path')} "
                            f"({m.get('bytes')} B, {m.get('last_write_age_s')}s ago) "
                            f"— nothing is reading that")
            warn.append("    -> av_log_where() for the full reconciliation")

    if not srcs:
        return "\n".join(warn) if warn else ""
    out: list[str] = list(warn)
    # TIME ALIGNMENT is the thing a raw `tail -f` cannot give you: these bytes
    # arrived while these frames were on screen, so the log and the picture can be
    # read together instead of guessed at.
    vis = state.get("visual") or {}
    seq = vis.get("latest_seq")
    span = f" (up to frame #{seq})" if seq is not None else ""
    out.append(f"  --- RAW LOG (new since last push, verbatim){span} ---")
    for s in srcs:
        label = s.get("label") or s.get("path") or "?"
        n = s.get("lines_total") or 0
        hdr = f"  [{label}] {n} new line(s)"
        if s.get("rotated"):
            hdr += " (file was truncated/rotated — new run)"
        if s.get("stale"):
            hdr += f" (LAST WRITE {int(s.get('last_write_age_s') or 0)}s AGO — stale?)"
        if s.get("withheld_oldest_lines"):
            hdr += f", {s['withheld_oldest_lines']} older withheld"
        out.append(hdr + ":")
        for item in s.get("lines") or []:
            if isinstance(item, dict):
                out.append(f"    {item.get('line')}   [x{item.get('repeat')}]")
            else:
                out.append(f"    {item}")
        if s.get("withheld_note"):
            out.append(f"    ...{s['withheld_note']}")
    return "\n".join(out)


#: Predicate verdicts. CANNOT_TELL is the whole safety property: if capture
#: stopped you may not claim the screen unfroze, and if the log went stale you may
#: not claim the error stopped — you stopped looking. Emitting a correction there
#: would manufacture exactly the false read this is meant to remove.
_GONE, _STILL, _UNKNOWN = "gone", "still_present", "cannot_tell"

#: Below this many seconds of stillness the screen counts as moving again. Kept
#: local to the correction path so it can be loosened without changing what
#: build_signals considers a hang in the first place.
FREEZE_HINT_S = float(os.environ.get("AGENTVISION_CORRECTION_FREEZE_S", "3.0"))


#: Kinds that must NEVER be auto-corrected, with the reason. `fatal` is the
#: important one and it was a real bug: build_signals labels ON-SCREEN ERROR TEXT
#: as `fatal`, and an earlier version routed it into the process-liveness branch,
#: so a claim about text visible on screen was answered with "the process has
#: been running again for 0s" — an all-clear about a completely different thing.
#: Its evidence is OCR, which was measured unreliable, so silence is the only
#: honest answer.
_NEVER_CORRECT = {
    "fatal": "its evidence is OCR text, which is not reliable enough to clear",
    "health_degraded": "a health score is a verdict, not an observation",
    "frames_awaiting": "not a failure — it has its own re-offer cadence",
    "preflight_gap": "a configuration gap does not resolve itself",
}


def _correction_observation(kind: str, state: dict, since_ms: float) -> tuple:
    """(verdict, observation) for one prior claim. Observation is COUNTED.

    Never the word "recovered" — that is a conclusion. "the process has been
    running again for 214s" is an observation the agent can check and disprove,
    and it carries its own evidence.

    READS THE STATE SHAPE `_ambient_state()` ACTUALLY PRODUCES. An earlier
    version read `logs.new_lines_since`, `visual.changes_since`,
    `visual.mean_luma`, `program.capture_running` and `program.pid` — none of
    which exist. Every one silently defaulted, so the stale-log guard never
    engaged, most kinds were dead, and the error correction emitted "has not
    appeared in the 0 new log lines", which is fabricated evidence rather than an
    observation. The unit tests passed throughout because their fixture invented
    that shape. Fields used here are asserted against the real producer by
    test_corrections.py.
    """
    if kind in _NEVER_CORRECT:
        return _UNKNOWN, ""
    prog = state.get("program") or {}
    vis = state.get("visual") or {}
    cap = state.get("capture") or {}
    logs = state.get("logs") or {}
    age_s = max(0.0, (_now_ms() - float(since_ms or 0)) / 1000.0)

    # If we cannot see, we cannot clear. Absence of the key means UNKNOWN, not
    # "assume we can see" — a safety gate must fail toward silence.
    capture_live = cap.get("engine_running")
    visual_kind = kind in ("hang", "blank", "capture_stopped", "window_missing")
    if visual_kind and capture_live is not True:
        return _UNKNOWN, ""

    if kind in ("program_died", "program_down", "crash"):
        if not prog.get("running"):
            return _STILL, ""
        name = str(prog.get("name") or "the process")
        return _GONE, f"{name} has been running again for {age_s:.0f}s"

    if kind in ("new_error", "standing_error"):
        # Only the LOG can clear a log-derived claim, and only a fresh one. A
        # stale source means we stopped reading, which is not evidence.
        if logs.get("stale") is not False:
            return _UNKNOWN, ""
        silent_s = logs.get("program_silent_s")
        if silent_s is None:
            return _UNKNOWN, ""
        return _GONE, (f"that fingerprint is no longer among the errors "
                       f"AgentVision is tracking, and the log has stayed fresh "
                       f"since (last program event {float(silent_s):.0f}s ago, "
                       f"claim was {age_s:.0f}s ago)")

    if kind == "hang":
        still_s = vis.get("still_for_s")
        if still_s is None:
            return _UNKNOWN, ""
        moments = int(vis.get("changed_moments") or 0)
        if float(still_s) >= FREEZE_HINT_S:
            return _STILL, ""
        return _GONE, (f"the screen is moving again (still for only "
                       f"{float(still_s):.1f}s now, {moments} changed moments "
                       f"on record)")

    if kind == "blank":
        is_blank = vis.get("is_blank")
        if is_blank is None:
            return _UNKNOWN, ""
        if is_blank:
            return _STILL, ""
        return _GONE, (f"frames are non-blank again (latest change score "
                       f"{vis.get('latest_change_score')}, frame "
                       f"#{vis.get('latest_seq')})")

    if kind in ("capture_stopped", "window_missing"):
        n = vis.get("frames_considered")
        return _GONE, (f"capture is running again"
                       + (f"; {int(n)} frames considered since" if n else ""))

    return _UNKNOWN, ""


def build_corrections(state: dict, session_id: str,
                      signals: list) -> list[dict]:
    """Claims AgentVision made this session that are no longer observable.

    THE TRAP THIS CLOSES. An alert fires once, the condition clears, and
    `already_surfaced()` suppresses every equal-or-lower-tier repeat — so
    AgentVision simply goes quiet. Meanwhile the verdict drifts into the middle
    of the context, where recall is measurably worst, and multi-turn research is
    blunt about what happens next: models commit early and "do not recover". The
    agent is left acting on something AgentVision itself no longer believes.

    WHY THIS CANNOT BECOME A NAG. It fires ONLY against a claim this session
    already made, at most once per claim, ever. The correction channel's volume
    is therefore hard-capped by the alert channel's volume, and a session that
    claimed nothing can never emit an all-clear. That is a structural bound, not
    a tuning parameter — which matters, because an unsolicited "still fine" line
    would be exactly the irrelevant repeated item that measurably degrades
    reasoning.
    """
    live = {s.get("fp") for s in (signals or [])}
    # `signals` is TRUNCATED — build_signals takes errors[:2] and standing[:2], and
    # the state it reads capped standing_errors at 4. So absence from `signals` is
    # not absence from reality: an error ranked 3rd was still being counted while
    # the correction path announced it was "no longer among the errors AgentVision
    # is tracking". That is this feature firing backwards — retracting a live
    # failure — which is worse than the silence it was written to fix. Use the
    # untruncated fingerprint list the state publishes for exactly this.
    tracked = {_fp("error_fp", f)
               for f in (state.get("tracked_error_fingerprints") or [])}
    if not tracked:
        # Older/partial state: fall back to whatever error lists are present
        # rather than treating a missing key as "nothing is tracked".
        tracked = {_fp("error_fp", e.get("fingerprint"))
                   for e in ((state.get("new_errors") or [])
                             + (state.get("standing_errors") or []))
                   if e.get("fingerprint")}
    out: list[dict] = []
    for fp, rec in MEMORY.claims(session_id):
        if fp in live or fp in tracked:
            continue                        # still true; nothing to correct
        if TIER_RANK.get(rec.get("tier", ""), 0) < TIER_RANK["notice"]:
            continue                        # only real claims get corrected
        kind = str(rec.get("kind") or "")
        cfp = _fp("correction", fp)
        if MEMORY.already_surfaced(session_id, cfp, "notice"):
            continue                        # one correction per claim, ever
        verdict, observation = _correction_observation(
            kind, state, rec.get("ts_ms") or 0)
        if verdict != _GONE or not observation:
            continue
        original = str(rec.get("text") or "").strip()
        if not original:
            continue
        out.append({
            "kind": "correction", "tier": "notice", "fp": cfp,
            "corrects": fp,
            # Quoting verbatim is load-bearing: it re-states the belief being
            # overturned so the agent never has to retrieve it from the part of
            # the context where retrieval fails.
            "text": (f"CORRECTION — earlier this session I told you: "
                     f"\"{original[:220]}\" That is NO LONGER OBSERVABLE: "
                     f"{observation}. This does not mean it was wrong then, or "
                     f"that the cause is fixed."),
            "next": "av_diagnose() to confirm the current state",
        })
    return out


def decide(state: dict, session_id: str = "default", event: str = "manual",
           force: bool = False) -> dict:
    """THE ambient decision. Returns a self-describing verdict:

        {tier, inject (bool), text, bytes, est_tokens, signals_considered,
         signals_used, suppressed:[{fp, why}], reason}

    `force=True` bypasses rate-limiting and delta-suppression (used by the GUI's
    live preview and by tests) but never bypasses the byte caps."""
    signals = build_signals(state)
    suppressed: list[dict] = []

    fresh: list[dict] = []
    for s in signals:
        if not force and MEMORY.already_surfaced(session_id, s["fp"], s["tier"]):
            suppressed.append({"fp": s["fp"], "kind": s["kind"],
                               "why": "already surfaced to this session"})
            continue
        fresh.append(s)

    # Corrections join the pool AFTER the suppression loop, deliberately. Running
    # them through it is the bug: a notice-tier correction against an alert-tier
    # claim satisfies TIER_RANK[notice] <= TIER_RANK[alert] and gets dropped as a
    # "lower-tier repeat" — so the retraction is silenced by the very assertion it
    # retracts. A correction must be at least as loud as the thing it corrects.
    corrections = build_corrections(state, session_id, signals)
    fresh = corrections + fresh

    if fresh:
        # HIGHEST tier wins, not first-in-list. Prepending corrections made
        # fresh[0] a notice whenever one was pending, which forced the whole tick
        # to notice — and `tier_sigs` then filtered the co-occurring ALERT out of
        # the injection entirely. Worse, notice carries a 45s rate-limit gap
        # against alert's 10s, so a pending all-clear could withhold a genuine
        # crash alert for up to 45 seconds. A retraction must never cost the
        # delivery of live bad news.
        tier = max((s["tier"] for s in fresh),
                   key=lambda t: TIER_RANK.get(t, 0))
    elif event in HEARTBEAT_EVENTS:
        tier = "heartbeat"
    else:
        tier = "silent"

    if tier == "silent":
        # SILENCE MUST NOT HIDE PROGRAM OUTPUT.
        # "Nothing worth saying" is a judgement about AgentVision's own signals.
        # New raw log lines are not a judgement — they are the program talking,
        # and swallowing them is exactly the mistake that let 180 present
        # failures read as "healthy". If there are new bytes, deliver them.
        raw = render_raw_block(state) if RAW_ALWAYS else ""
        if raw:
            n = len(raw.encode("utf-8"))
            if not force:
                MEMORY.record_injection(session_id, "raw", n)
            return {"tier": "raw", "inject": True, "text": raw, "bytes": n,
                    "est_tokens": (n + 3) // 4,
                    "signals_considered": len(signals), "signals_used": 0,
                    "signal_kinds": ["raw_log"], "offered_seqs": [],
                    "suppressed": suppressed,
                    "reason": "no AgentVision signal, but the program logged new "
                              "lines — raw output is never withheld",
                    "event": event, "session_id": session_id}
        MEMORY.record_silent(session_id)
        return {"tier": "silent", "inject": False, "text": "", "bytes": 0,
                "est_tokens": 0, "signals_considered": len(signals),
                "signals_used": 0, "suppressed": suppressed,
                "reason": ("nothing new to say" if signals else "no signals"),
                "event": event, "session_id": session_id}

    if not force and MEMORY.rate_limited(session_id, tier):
        MEMORY.record_silent(session_id)
        return {"tier": "silent", "inject": False, "text": "", "bytes": 0,
                "est_tokens": 0, "signals_considered": len(signals),
                "signals_used": 0, "suppressed": suppressed,
                "reason": f"rate-limited: {tier} injected too recently "
                          f"(min gap {MIN_GAP_MS.get(tier)}ms)",
                "event": event, "session_id": session_id}

    tier_sigs = [s for s in fresh if s["tier"] == tier]
    # A correction rides along in whatever tier won, rather than being filtered
    # out for being a mere notice. render() prints every entry it is given
    # regardless of the entry's own tier, so this costs nothing structurally.
    pending_corrections = [s for s in fresh
                           if s.get("kind") == "correction" and s not in tier_sigs]
    used = tier_sigs[:3]
    # RESERVE A SLOT FOR ANYTHING WITH A DEADLINE.
    # Every other signal describes something that already happened and will still
    # be true next turn, so losing it to the 3-signal cap costs nothing. A
    # deadline signal is different: frames awaiting examination have pixels that
    # get reclaimed, so if it is not said NOW the evidence is gone and the
    # screenshots were taken for nothing. It therefore displaces the last
    # ordinary signal rather than being dropped.
    deadline = next((s for s in tier_sigs if s.get("deadline")), None)
    if deadline is not None and deadline not in used:
        used = used[:2] + [deadline]
    # A CORRECTION ALSO GETS A RESERVED SLOT, for the same reason. Losing a
    # retraction to three fresher alerts is the precise failure being fixed: the
    # agent keeps acting on a belief AgentVision has already abandoned, and every
    # additional turn makes that belief harder to dislodge. Placed FIRST so it
    # occupies the strongest recall position in the injection.
    corr = next((s for s in tier_sigs if s.get("kind") == "correction"),
                pending_corrections[0] if pending_corrections else None)
    if corr is not None:
        # One branch, not two. The old pair had a latent bug: when the deadline
        # reservation above had already displaced the correction, the "not in
        # used" branch rebuilt the list from used[:2] and silently dropped the
        # deadline signal back out. Rebuild once, keeping the deadline.
        rest = [s for s in used if s is not corr]
        keep_deadline = [s for s in rest if s.get("deadline")]
        rest = keep_deadline + [s for s in rest if not s.get("deadline")]
        used = [corr] + rest[:2]
    text = render(tier, used, state, event)
    if not text:
        MEMORY.record_silent(session_id)
        return {"tier": "silent", "inject": False, "text": "", "bytes": 0,
                "est_tokens": 0, "signals_considered": len(signals),
                "signals_used": 0, "suppressed": suppressed,
                "reason": "rendered empty", "event": event,
                "session_id": session_id}

    n = len(text.encode("utf-8"))
    if not force:
        for s in used:
            MEMORY.mark_surfaced(session_id, s["fp"], s["tier"],
                                 text=s.get("text", ""),
                                 kind=s.get("kind", ""))
        MEMORY.record_injection(session_id, tier, n)

    return {
        "tier": tier, "inject": True, "text": text, "bytes": n,
        "est_tokens": (n + 3) // 4,
        "signals_considered": len(signals), "signals_used": len(used),
        "signal_kinds": [s["kind"] for s in used],
        # Frame seqs this injection actually NAMED to the agent. Only from
        # `used`, which is what render() puts in the text — a seq the agent was
        # never shown must not be recorded as offered.
        "offered_seqs": sorted({int(q) for s in used
                                for q in (s.get("awaiting_seqs") or [])}),
        "incident_ids": [s.get("incident_id") for s in used if s.get("incident_id")],
        "suppressed": suppressed,
        "cap_bytes": (ALERT_CAP if tier == "alert" else
                      NOTICE_CAP if tier == "notice" else HEARTBEAT_CAP),
        "reason": f"{tier}: {len(used)} fresh signal(s)",
        "event": event, "session_id": session_id, "forced": bool(force),
    }


# ── Persistent Stop-block budget ──────────────────────────────────────────────
# The Claude Code hooks reference documents that Stop hooks CAN block, but does
# NOT document any loop-guard field on the Stop payload and does NOT document a
# retry cap — its own recommendation is to implement your own counter. So the
# budget is persisted to disk, keyed by session id: an in-memory-only counter
# would reset if the bridge restarted mid-session, which is exactly the situation
# in which a loop would form.
STOP_STATE_PATH = os.environ.get(
    "AGENTVISION_AMBIENT_STOP_STATE",
    os.path.join("log", "agentvision_stop_blocks.json"))
_stop_file_lock = threading.Lock()


def _load_stop_counts() -> dict:
    try:
        with open(STOP_STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_stop_counts(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STOP_STATE_PATH) or ".", exist_ok=True)
        # Bound the file so it cannot grow forever.
        if len(d) > 200:
            for k in list(d.keys())[:len(d) - 200]:
                d.pop(k, None)
        tmp = STOP_STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, STOP_STATE_PATH)
    except Exception:
        pass


def persistent_stop_blocks(session_id: str) -> int:
    with _stop_file_lock:
        return int((_load_stop_counts().get(session_id) or {}).get("blocks", 0))


def _bump_persistent_stop_blocks(session_id: str) -> int:
    with _stop_file_lock:
        d = _load_stop_counts()
        rec = d.get(session_id) or {"blocks": 0}
        rec["blocks"] = int(rec.get("blocks", 0)) + 1
        rec["last_ms"] = _now_ms()
        d[session_id] = rec
        _save_stop_counts(d)
        return rec["blocks"]


def stop_backstop(state: dict, session_id: str, stop_hook_active: bool = False) -> dict:
    """Decide whether a Stop hook should BLOCK so the agent keeps working.

    LOOP SAFETY — five independent guards, ALL of which must pass:
      1. Disabled by default (AGENTVISION_AMBIENT_STOP_BLOCK=0).
      2. Honour the harness's re-entry flag IF it exists. The official hooks
         reference documents no such field for Stop, so this is defence-in-depth
         and is NOT relied upon.
      3. Only STOP_BLOCK_KINDS may block (crash / fatal / hang / program_died).
         A degraded health score explicitly may NOT — that is the difference
         between a backstop and a trap.
      4. A PERSISTENT per-session budget (MAX_STOP_BLOCKS, default 1; 0 disables)
         that survives a bridge restart.
      5. The signal is marked surfaced BEFORE the block is returned, so the very
         same incident can never justify a second block.
    """
    if not STOP_BLOCK_ENABLED:
        return {"block": False, "reason": "stop-block disabled (default)"}
    if stop_hook_active:
        return {"block": False,
                "reason": "harness reports this stop already followed a hook block "
                          "— refusing to block again (loop guard)"}
    if MAX_STOP_BLOCKS <= 0:
        return {"block": False, "reason": "max_stop_blocks=0"}
    used = max(MEMORY.stop_blocks(session_id),
               persistent_stop_blocks(session_id))
    if used >= MAX_STOP_BLOCKS:
        return {"block": False,
                "reason": f"per-session block budget exhausted ({used}/{MAX_STOP_BLOCKS})"}

    critical = [s for s in build_signals(state)
                if s["kind"] in STOP_BLOCK_KINDS
                and not MEMORY.already_surfaced(session_id, s["fp"], "alert")]
    if not critical:
        return {"block": False,
                "reason": "no un-surfaced critical signal (crash/fatal/hang/"
                          "program-died); a degraded score never qualifies"}

    s = critical[0]
    # ORDER MATTERS: mark surfaced BEFORE returning the block, so if the agent
    # stops again for the same reason we can no longer justify blocking.
    MEMORY.mark_surfaced(session_id, s["fp"], "alert",
                         text=s.get("text", ""), kind=s.get("kind", ""))
    MEMORY.record_stop_block(session_id)
    n = _bump_persistent_stop_blocks(session_id)
    return {
        "block": True,
        "kind": s["kind"],
        "reason": (f"AgentVision: {s['text']} Do not stop yet — investigate with "
                   f"{s.get('next') or 'av_diagnose()'}. "
                   f"(This backstop fires at most {MAX_STOP_BLOCKS}x per session "
                   f"and never twice for the same incident.)"),
        "blocks_used": n,
        "budget": MAX_STOP_BLOCKS,
    }
