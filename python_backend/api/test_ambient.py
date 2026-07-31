#!/usr/bin/env python3
"""
Ambient engine (Push Mode) tests — the three rules, and the Stop-block proof.

The ambient engine is the only part of AgentVision that speaks WITHOUT being
asked, so its failure modes are qualitatively worse than the rest of the tool: a
chatty channel burns tokens on every prompt, and a Stop backstop that misfires
can trap the user in a loop they cannot exit. These tests exist to make both
impossible.

Covers:
  RULE 1 silent by default          — a healthy program injects NOTHING
  RULE 2 delta-only                 — a repeat is suppressed; an escalation is not
  RULE 3 hard byte caps             — pathological input is truncated, not passed
  tiers                             — alert > notice > heartbeat selection
  rate limiting / coalescing        — same tier cannot spam one session
  session isolation                 — one session's memory never leaks to another
  visual sentence                   — present in every non-silent injection
  incident preference               — alerts point at the frozen incident
  STOP SAFETY                       — off by default; when on, fires at most N
                                      times and NEVER twice for one incident;
                                      a degraded health score can never block

Pure stdlib (no flask, no bridge, no screen). Run:
    python3 python_backend/api/test_ambient.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent))
sys.path.insert(0, str(_HERE.parent.parent.parent))

_fails = 0


def check(name, cond, detail=""):
    global _fails
    if not cond:
        _fails += 1
    print(f"  [{'ok ' if cond else 'FAIL'}] {name}"
          f"{'' if cond or not detail else '  — ' + detail}")


# ── State fixtures ────────────────────────────────────────────────────────────

def healthy(**over) -> dict:
    s = {
        "profile": "demo",
        "program": {"name": "DemoApp", "running": True, "was_running": True},
        "capture": {"engine_running": True, "capturing": True,
                    "frames_stored": 120, "window_missing": False,
                    "capture_app": "DemoApp", "blank_frames": 0},
        "visual": {"latest_seq": 120, "latest_change_score": 0.12,
                   "changed_bbox": [100, 50, 400, 200], "is_blank": False,
                   "frames_considered": 120, "changed_moments": 9,
                   "still_for_s": None, "on_screen_error_text": False},
        "incidents": [], "visual_events": [], "new_errors": [],
        "health": {"score": 95}, "preflight_gap": None,
    }
    s.update(over)
    return s


def with_incident(**over) -> dict:
    s = healthy()
    s["incidents"] = [{"id": "inc-error-1784628000000", "kind": "error",
                       "trigger_seq": 88, "detail": "KeyError: 'cfg'",
                       "pre_error_seconds": 60.0, "frame_count": 60}]
    s.update(over)
    return s


def main():
    # Isolate the persistent stop-block file so the test never touches real state.
    work = tempfile.mkdtemp(prefix="av_ambient_")
    os.environ["AGENTVISION_AMBIENT_STOP_STATE"] = os.path.join(work, "stop.json")

    from api import ambient as A
    A.STOP_STATE_PATH = os.path.join(work, "stop.json")

    # ── RULE 1: silent by default ────────────────────────────────────────────
    print("RULE 1 — silent by default:")
    A.MEMORY.reset()
    d = A.decide(healthy(), "s1", "UserPromptSubmit")
    check("a healthy program injects NOTHING", d["inject"] is False
          and d["tier"] == "silent" and d["text"] == "", str(d)[:150])
    check("silent costs zero bytes", d["bytes"] == 0 and d["est_tokens"] == 0)
    check("silence explains itself", bool(d.get("reason")))
    for ev in ("UserPromptSubmit", "PostToolUse", "PostToolBatch"):
        check(f"still silent on {ev}",
              A.decide(healthy(), "s1", ev)["inject"] is False)
    check("no frames yet is not an alert",
          A.decide({"profile": "p", "program": {"name": "X", "running": True},
                    "capture": {"frames_stored": 0}}, "s2",
                   "UserPromptSubmit")["inject"] is False)

    # A heartbeat is allowed ONLY on SessionStart, and only once in a while.
    A.MEMORY.reset()
    hb = A.decide(healthy(), "hb1", "SessionStart")
    check("SessionStart may emit a heartbeat",
          hb["tier"] == "heartbeat" and hb["inject"] is True, str(hb)[:120])
    check("heartbeat is tiny", hb["bytes"] <= A.HEARTBEAT_CAP,
          f"{hb['bytes']} > {A.HEARTBEAT_CAP}")
    check("heartbeat does NOT repeat on the next prompt",
          A.decide(healthy(), "hb1", "UserPromptSubmit")["inject"] is False)
    check("heartbeat is rate-limited on a second SessionStart",
          A.decide(healthy(), "hb1", "SessionStart")["inject"] is False)

    # ── Tier selection ───────────────────────────────────────────────────────
    print("tier selection:")
    A.MEMORY.reset()
    a = A.decide(with_incident(), "t1", "PostToolUse")
    check("a frozen incident is an ALERT", a["tier"] == "alert" and a["inject"],
          str(a)[:140])
    A.MEMORY.reset()
    n = A.decide(healthy(capture={"engine_running": True, "capturing": True,
                                  "frames_stored": 50, "window_missing": True,
                                  "capture_app": "DemoApp", "blank_frames": 0}),
                 "t2", "UserPromptSubmit")
    check("a missing capture window is a NOTICE", n["tier"] == "notice" and n["inject"],
          str(n)[:140])
    A.MEMORY.reset()
    both = A.decide(with_incident(preflight_gap="2 format(s)"), "t3", "UserPromptSubmit")
    check("alert wins over notice when both are present", both["tier"] == "alert")
    check("alert text is prefixed so it is unmissable",
          both["text"].startswith("[AgentVision ALERT]"), both["text"][:60])

    # ── RULE 2: delta-only ───────────────────────────────────────────────────
    print("RULE 2 — delta-only:")
    A.MEMORY.reset()
    st = with_incident()
    first = A.decide(st, "d1", "PostToolUse")
    check("first sighting injects", first["inject"] is True)
    A.MIN_GAP_MS["alert"] = 0.0          # isolate delta logic from rate limiting
    second = A.decide(st, "d1", "PostToolUse")
    check("the SAME signal is suppressed on a second call",
          second["inject"] is False, str(second)[:160])
    check("suppression is reported, not hidden",
          any(x["why"].startswith("already surfaced") for x in second["suppressed"]),
          str(second["suppressed"]))
    third = A.decide(st, "d2", "PostToolUse")
    check("a DIFFERENT session still gets told", third["inject"] is True)

    # Escalation must get through even though the fingerprint was seen.
    A.MEMORY.reset()
    ev_notice = healthy(visual_events=[{"type": "blank_screen", "id": "vis-blank-5",
                                        "seq": 5}])
    r1 = A.decide(ev_notice, "esc", "UserPromptSubmit")
    check("blank screen surfaces as notice", r1["tier"] == "notice" and r1["inject"])
    A.MEMORY.reset()
    A.MEMORY.mark_surfaced("esc2", A._fp("incident", "inc-x"), "notice")
    esc = A.decide({**healthy(), "incidents": [
        {"id": "inc-x", "kind": "error", "trigger_seq": 1, "detail": "boom",
         "pre_error_seconds": 60.0}]}, "esc2", "UserPromptSubmit")
    check("an ESCALATION (notice -> alert) is NOT suppressed",
          esc["inject"] is True and esc["tier"] == "alert", str(esc)[:140])

    # ── Rate limiting ────────────────────────────────────────────────────────
    print("rate limiting / coalescing:")
    A.MEMORY.reset()
    A.MIN_GAP_MS["notice"] = 999999.0
    s_a = healthy(capture={"engine_running": True, "capturing": True,
                           "frames_stored": 50, "window_missing": True,
                           "capture_app": "A", "blank_frames": 0})
    s_b = healthy(capture={"engine_running": True, "capturing": True,
                           "frames_stored": 50, "window_missing": True,
                           "capture_app": "B", "blank_frames": 0})
    r_a = A.decide(s_a, "rl", "UserPromptSubmit")
    r_b = A.decide(s_b, "rl", "UserPromptSubmit")
    check("first notice gets through", r_a["inject"] is True)
    check("a DIFFERENT notice is rate-limited within the gap",
          r_b["inject"] is False and "rate-limited" in r_b["reason"],
          str(r_b)[:160])
    check("force=True bypasses the rate limit",
          A.decide(s_b, "rl", "UserPromptSubmit", force=True)["inject"] is True)
    A.MIN_GAP_MS["notice"] = 45000.0

    # ── RULE 3: hard byte caps ───────────────────────────────────────────────
    print("RULE 3 — hard byte caps:")
    A.MEMORY.reset()
    A.MIN_GAP_MS["alert"] = 0.0
    huge = healthy(new_errors=[{"fingerprint": f"fp{i}", "count": 3,
                                "sample": "X" * 5000} for i in range(20)],
                   incidents=[{"id": f"inc-{i}", "kind": "error",
                               "trigger_seq": i, "detail": "Y" * 5000,
                               "pre_error_seconds": 60.0} for i in range(20)])
    big = A.decide(huge, "cap", "PostToolUse", force=True)
    check("a pathological state is TRUNCATED to the alert cap",
          big["bytes"] <= A.ALERT_CAP, f"{big['bytes']} > {A.ALERT_CAP}")
    check("truncation is visible", big["text"].endswith("..."), big["text"][-40:])
    check("at most 3 signals are ever rendered", big["signals_used"] <= 3,
          str(big["signals_used"]))
    check("cap is reported in the payload", big.get("cap_bytes") == A.ALERT_CAP)
    A.MEMORY.reset()
    hb_big = A.decide(healthy(program={"name": "Z" * 4000, "running": True,
                                       "was_running": True}),
                      "cap2", "SessionStart", force=True)
    check("heartbeat cap is enforced too", hb_big["bytes"] <= A.HEARTBEAT_CAP,
          f"{hb_big['bytes']} > {A.HEARTBEAT_CAP}")

    # ── The visual sentence ──────────────────────────────────────────────────
    print("visual sentence (the thing no log can say):")
    A.MEMORY.reset()
    A.MIN_GAP_MS["alert"] = 0.0
    for label, state in (("alert", with_incident()),
                         ("notice", healthy(preflight_gap="1 format")),
                         ("heartbeat", healthy())):
        A.MEMORY.reset()
        ev = "SessionStart" if label == "heartbeat" else "UserPromptSubmit"
        r = A.decide(state, f"vs-{label}", ev, force=True)
        check(f"{label} injection contains a Visual: sentence",
              "Visual:" in r["text"], r["text"][:120])
    v_static = A.decide(healthy(visual={"latest_seq": 9, "latest_change_score": 0.0,
                                        "is_blank": False, "frames_considered": 9,
                                        "changed_moments": 0, "still_for_s": 12.5,
                                        "on_screen_error_text": False}),
                        "vs2", "SessionStart", force=True)
    check("a static screen is described as STATIC",
          "STATIC" in v_static["text"], v_static["text"][:160])
    v_blank = A.decide(healthy(visual={"latest_seq": 9, "latest_change_score": 0.0,
                                       "is_blank": True, "frames_considered": 9,
                                       "changed_moments": 0,
                                       "on_screen_error_text": False}),
                       "vs3", "SessionStart", force=True)
    check("a blank screen is described as BLANK",
          "BLANK" in v_blank["text"], v_blank["text"][:160])
    # The FULL visual sentence (with the changed region) is used on notice/alert.
    # The heartbeat deliberately uses a BRIEF form so it fits its 220-byte cap
    # without truncating mid-sentence.
    A.MEMORY.reset()
    v_chg = A.decide(healthy(preflight_gap="1 format"), "vs4",
                     "UserPromptSubmit", force=True)
    check("a notice reports the changed region",
          "changed region" in v_chg["text"], v_chg["text"][:200])
    A.MEMORY.reset()
    v_hb = A.decide(healthy(), "vs5", "SessionStart", force=True)
    check("the heartbeat uses the BRIEF visual form (fits its cap)",
          "changed region" not in v_hb["text"]
          and "screen changing" in v_hb["text"], v_hb["text"][:200])
    check("the heartbeat is never truncated mid-sentence",
          not v_hb["text"].endswith("..."), v_hb["text"][-50:])

    # ── Alerts prefer the frozen incident ────────────────────────────────────
    print("incident preference:")
    A.MEMORY.reset()
    r = A.decide(with_incident(), "inc", "PostToolUse", force=True)
    check("alert references the frozen incident", "froze an incident" in r["text"],
          r["text"][:160])
    check("alert points at av_error_moment", "av_error_moment" in r["text"])
    check("alert says the pre-error window is already on disk",
          "already on disk" in r["text"])
    check("incident id is machine-readable in the payload",
          r.get("incident_ids") == ["inc-error-1784628000000"],
          str(r.get("incident_ids")))
    check("PostToolUse framing tells the agent it was its own change",
          "Since your last change" in r["text"], r["text"][:200])

    # ── Session isolation ────────────────────────────────────────────────────
    print("session isolation:")
    A.MEMORY.reset()
    A.decide(with_incident(), "iso-a", "PostToolUse")
    stats_a = A.MEMORY.stats("iso-a")
    stats_b = A.MEMORY.stats("iso-b")
    check("session A recorded an injection", stats_a["injections"] == 1)
    check("session B is untouched", stats_b["injections"] == 0)
    A.MEMORY.reset("iso-a")
    check("resetting one session makes it speak again",
          A.decide(with_incident(), "iso-a", "PostToolUse")["inject"] is True)

    # ── STOP BACKSTOP SAFETY (the loop-safety proof) ─────────────────────────
    print("STOP backstop — loop safety:")
    crash = healthy(program={"name": "DemoApp", "running": False,
                             "was_running": True, "died_at_ms": 1.0})

    A.MEMORY.reset()
    check("DISABLED BY DEFAULT", A.STOP_BLOCK_ENABLED is False)
    off = A.stop_backstop(crash, "st0")
    check("with the default config it never blocks", off["block"] is False,
          str(off))
    check("and it says why", "disabled" in off["reason"])

    # Now force-enable and prove the bounds.
    A.STOP_BLOCK_ENABLED = True
    A.MAX_STOP_BLOCKS = 1
    try:
        A.MEMORY.reset()
        Path(A.STOP_STATE_PATH).unlink(missing_ok=True)
        r1 = A.stop_backstop(crash, "st1")
        check("a genuine crash CAN block once", r1["block"] is True, str(r1)[:160])
        check("the block reason tells the agent what to do",
              "av_diagnose" in r1["reason"] or "av_" in r1["reason"],
              r1["reason"][:140])
        check("the block reason states its own bound",
              "at most" in r1["reason"], r1["reason"][-120:])
        r2 = A.stop_backstop(crash, "st1")
        check("it can NEVER block twice for the same incident",
              r2["block"] is False, str(r2)[:200])
        r3 = A.stop_backstop(crash, "st1")
        check("and still refuses on a third attempt", r3["block"] is False)

        # The budget must survive a bridge restart (in-memory reset).
        A.MEMORY.reset()
        r4 = A.stop_backstop(crash, "st1")
        check("the budget PERSISTS across an in-memory reset (restart-safe)",
              r4["block"] is False and "budget" in r4["reason"], str(r4)[:200])

        # Harness re-entry flag, if it ever exists, is honoured.
        A.MEMORY.reset()
        Path(A.STOP_STATE_PATH).unlink(missing_ok=True)
        r5 = A.stop_backstop(crash, "st2", stop_hook_active=True)
        check("an active-hook flag prevents blocking (defence in depth)",
              r5["block"] is False and "loop guard" in r5["reason"], str(r5)[:200])

        # max_stop_blocks=0 disables entirely.
        A.MAX_STOP_BLOCKS = 0
        A.MEMORY.reset()
        Path(A.STOP_STATE_PATH).unlink(missing_ok=True)
        r6 = A.stop_backstop(crash, "st3")
        check("max_stop_blocks=0 disables blocking", r6["block"] is False,
              str(r6)[:140])
        A.MAX_STOP_BLOCKS = 1

        # THE IMPORTANT NEGATIVE: a degraded score must never trap the user.
        A.MEMORY.reset()
        Path(A.STOP_STATE_PATH).unlink(missing_ok=True)
        degraded = healthy(health={"score": 20})
        r7 = A.stop_backstop(degraded, "st4")
        check("a DEGRADED HEALTH SCORE can never block a stop",
              r7["block"] is False, str(r7)[:200])
        A.MEMORY.reset()
        notice_only = healthy(capture={"engine_running": True, "capturing": True,
                                       "frames_stored": 10, "window_missing": True,
                                       "capture_app": "A", "blank_frames": 0})
        r8 = A.stop_backstop(notice_only, "st5")
        check("a notice-tier signal can never block a stop", r8["block"] is False,
              str(r8)[:200])
        check("only crash/fatal/hang/program_died are eligible",
              A.STOP_BLOCK_KINDS == {"program_died", "crash", "fatal", "hang"},
              str(A.STOP_BLOCK_KINDS))

        # A hang qualifies (it is the case logs cannot see).
        A.MEMORY.reset()
        Path(A.STOP_STATE_PATH).unlink(missing_ok=True)
        hang = healthy(visual_events=[{"type": "screen_frozen", "id": "vf-1",
                                       "seq": 40, "still_for_s": 30.0}])
        r9 = A.stop_backstop(hang, "st6")
        check("a HANG qualifies to block once", r9["block"] is True, str(r9)[:160])

        # Bounded N: with a budget of 3 it fires at most 3 times across DIFFERENT
        # incidents, then stops forever.
        A.MAX_STOP_BLOCKS = 3
        A.MEMORY.reset()
        Path(A.STOP_STATE_PATH).unlink(missing_ok=True)
        fired = 0
        for i in range(10):
            st = healthy(visual_events=[{"type": "screen_frozen",
                                         "id": f"vf-{i}", "seq": i,
                                         "still_for_s": 30.0}])
            if A.stop_backstop(st, "st7")["block"]:
                fired += 1
        check("fires at most max_stop_blocks times even with new incidents",
              fired == 3, f"fired {fired}x, budget 3")
    finally:
        A.STOP_BLOCK_ENABLED = False
        A.MAX_STOP_BLOCKS = 1

    print(f"\n{'=' * 60}")
    print("ambient: " + ("ALL PASS" if _fails == 0 else f"{_fails} FAILURE(S)"))
    print("=" * 60)
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
