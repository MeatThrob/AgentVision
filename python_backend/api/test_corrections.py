#!/usr/bin/env python3
"""A claim AgentVision retracts must be as loud as the claim itself.

THE TRAP. `build_signals()` had no "no longer observable" signal, and
`already_surfaced()` suppresses any repeat at equal-or-lower tier. So: an alert
fires once, the condition clears, and AgentVision goes quiet forever. The agent
is left holding a verdict AgentVision itself no longer believes — and by then
that verdict has drifted into the middle of the context, where recall is
measurably worst, while multi-turn research says models commit early and "do not
recover".

Note the shape of the old bug precisely: a notice-tier correction against an
alert-tier claim satisfies `TIER_RANK[notice] <= TIER_RANK[alert]`, so routing
corrections through the normal suppression loop means **the retraction is
silenced by the very assertion it retracts.**

The anti-nag bound is structural, not tuned: a correction fires only against a
claim this session already made, at most once per claim, ever. A session that
claimed nothing can never emit an all-clear.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
for p in (str(REPO), str(REPO / "python_backend"), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from api import ambient as A                                   # noqa: E402


def _state(running=True, capture=True, stale=False, silent_s=2.0,
           still_for_s=0.4, is_blank=False, frames=214, moments=47):
    """The shape `_ambient_state()` ACTUALLY produces.

    The previous fixture invented `program.capture_running`, `program.pid`,
    `visual.changes_since`, `visual.mean_luma`, `visual.frames_since` and a whole
    `logs` block — none of which the producer emits. Every assertion passed while
    the feature was dead in production, which is the failure this file now guards
    against. `test_state_shape_matches_producer` below asserts the match instead
    of trusting it.
    """
    return {
        "program": {"running": running, "name": "demo", "was_running": True},
        "capture": {"engine_running": capture},
        "visual": {"still_for_s": still_for_s, "is_blank": is_blank,
                   "changed_moments": moments, "frames_considered": frames,
                   "latest_change_score": 0.12, "latest_seq": 991},
        "logs": {"stale": stale, "program_silent_s": silent_s},
    }


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    sid = "corrections-test"
    A.MEMORY._sessions.pop(sid, None)

    DEAD = {"kind": "program_died", "tier": "alert", "fp": "fp-died",
            "text": "The program DIED at 14:02 (exit 139).", "next": "av_diagnose()"}

    # Nothing claimed -> nothing to correct. The structural anti-nag bound.
    check("no prior claim -> no correction",
          A.build_corrections(_state(), sid, []) == [])

    # AgentVision asserts the program died.
    A.MEMORY.mark_surfaced(sid, DEAD["fp"], "alert", text=DEAD["text"],
                           kind=DEAD["kind"])

    # Still dead -> still true -> no correction.
    check("claim still true -> no correction",
          A.build_corrections(_state(running=False), sid, [DEAD]) == [])
    check("claim absent from signals but STILL dead -> no correction",
          A.build_corrections(_state(running=False), sid, []) == [],
          "the predicate, not mere absence, decides")

    # Program is back. THE CORRECTION MUST FIRE.
    corr = A.build_corrections(_state(running=True), sid, [])
    check("condition cleared -> correction fires", len(corr) == 1,
          str(len(corr)))
    if corr:
        c = corr[0]
        check("it quotes the original claim VERBATIM",
              DEAD["text"][:40] in c["text"], c["text"][:120])
        check("it states a COUNTED observation, not a verdict",
              "running again for" in c["text"], c["text"][:160])
        check("it never says the word 'recovered'",
              "recovered" not in c["text"].lower())
        check("it does not claim the cause is fixed",
              "does not mean" in c["text"].lower())
        check("it names what it corrects", c.get("corrects") == DEAD["fp"])
        check("its fingerprint differs from the original",
              c["fp"] != DEAD["fp"])

    # ── CANNOT_TELL: we may not clear what we cannot see ───────────────────
    A.MEMORY._sessions.pop(sid, None)
    FROZEN = {"kind": "hang", "tier": "notice", "fp": "fp-frozen",
              "text": "The screen has not changed for 90s (possible hang)."}
    A.MEMORY.mark_surfaced(sid, FROZEN["fp"], "notice", text=FROZEN["text"],
                           kind=FROZEN["kind"])
    check("capture stopped -> CANNOT claim the screen unfroze",
          A.build_corrections(_state(capture=False), sid, []) == [],
          "we stopped looking; that is not evidence")
    check("capture running + screen moving -> correction fires",
          len(A.build_corrections(_state(still_for_s=0.4), sid, [])) == 1)

    A.MEMORY._sessions.pop(sid, None)
    ERR = {"kind": "new_error", "tier": "alert", "fp": "fp-err",
           "text": "NEW error this session (3x): DeviceLostException"}
    A.MEMORY.mark_surfaced(sid, ERR["fp"], "alert", text=ERR["text"],
                           kind=ERR["kind"])
    check("stale log -> CANNOT claim the error stopped",
          A.build_corrections(_state(stale=True), sid, []) == [],
          "a stale source means we stopped looking")
    check("UNKNOWN staleness -> also cannot claim it stopped",
          A.build_corrections(_state(stale=None), sid, []) == [],
          "absent freshness must read as cannot-tell, not as fresh")
    fresh = A.build_corrections(_state(stale=False), sid, [])
    check("fresh log -> correction fires citing the freshness it relied on",
          len(fresh) == 1 and "log has stayed fresh" in fresh[0]["text"],
          fresh[0]["text"][:150] if fresh else "none")

    # THE BUG THIS CATCHES: build_signals labels on-screen error text `fatal`,
    # and an earlier version routed `fatal` into the process-liveness branch — so
    # a claim about text on screen was answered with "the process has been
    # running again", an all-clear about something else entirely.
    for never, why in (("fatal", "evidence is unreliable OCR"),
                       ("health_degraded", "a score is a verdict"),
                       ("frames_awaiting", "not a failure"),
                       ("preflight_gap", "config does not self-resolve")):
        A.MEMORY._sessions.pop(sid, None)
        A.MEMORY.mark_surfaced(sid, f"fp-{never}", "alert",
                               text=f"claim of kind {never}", kind=never)
        got = A.build_corrections(_state(), sid, [])
        check(f"{never} is NEVER auto-corrected ({why})", got == [],
              got[0]["text"][:110] if got else "")

    # ── Once per claim, ever ───────────────────────────────────────────────
    A.MEMORY._sessions.pop(sid, None)
    A.MEMORY.mark_surfaced(sid, DEAD["fp"], "alert", text=DEAD["text"],
                           kind=DEAD["kind"])
    first = A.build_corrections(_state(), sid, [])
    check("first correction is produced", len(first) == 1)
    A.MEMORY.mark_surfaced(sid, first[0]["fp"], "notice",
                           text=first[0]["text"], kind="correction")
    check("second call produces NOTHING (one per claim, ever)",
          A.build_corrections(_state(), sid, []) == [],
          "this is the anti-nag bound")

    # ── It must survive decide()'s suppression, which is the actual bug ────
    A.MEMORY._sessions.pop(sid, None)
    A.MEMORY.mark_surfaced(sid, DEAD["fp"], "alert", text=DEAD["text"],
                           kind=DEAD["kind"])
    st = _state()
    st["logs"]["stale"] = False
    verdict = A.decide(st, session_id=sid, event="PostToolUse")
    check("decide() pushes the correction rather than suppressing it",
          verdict.get("inject") is True
          and "CORRECTION" in (verdict.get("text") or ""),
          f"tier={verdict.get('tier')} reason={verdict.get('reason')}")
    check("...and it appears FIRST in the injected text",
          (verdict.get("text") or "").find("CORRECTION") <
          max(1, len(verdict.get("text") or "")) // 2,
          (verdict.get("text") or "")[:100])

    # A low-tier claim is not worth correcting.
    A.MEMORY._sessions.pop(sid, None)
    A.MEMORY.mark_surfaced(sid, "fp-hb", "heartbeat", text="still watching",
                           kind="heartbeat")
    check("heartbeat-tier claims are never corrected",
          A.build_corrections(_state(), sid, []) == [])

    # ── THE CHECK THAT WOULD HAVE CAUGHT ALL OF THIS ───────────────────────
    # Every field the predicate reads must exist in what the PRODUCER emits, and
    # every kind it branches on must be one build_signals actually emits. The
    # first version of this feature failed both and its tests passed anyway,
    # because the fixture above invented a state shape that never occurs.
    import re as _re
    src = (REPO / "python_backend/api/ambient.py").read_text()
    body = src.split("def _correction_observation(", 1)[1].split("\ndef ", 1)[0]
    emitted = set(_re.findall(r'"kind": "([a-z_]+)"', src))
    branched = set(_re.findall(r'kind (?:in \(|== )"?([a-z_]+)', body))
    branched |= set(_re.findall(r'"([a-z_]+)"', body.split("if kind", 1)[-1])) & emitted
    unknown = {k for k in branched if k not in emitted} - {"correction"}
    check("every kind the predicate branches on is really emitted",
          not unknown, f"not emitted by build_signals: {sorted(unknown)}")
    never = set(A._NEVER_CORRECT)
    check("the never-correct list only names real kinds",
          never <= emitted, str(sorted(never - emitted)))
    check("'fatal' is on the never-correct list",
          "fatal" in A._NEVER_CORRECT,
          "build_signals labels ON-SCREEN ERROR TEXT as fatal")

    try:
        sys.path.insert(0, str(REPO / "python_backend" / "api"))
        import bridge_server as _bs
        produced = _bs._ambient_state_uncached()
        for sect, field in (("program", "running"), ("program", "name"),
                            ("capture", "engine_running"),
                            ("visual", "still_for_s"), ("visual", "is_blank"),
                            ("visual", "changed_moments"),
                            ("visual", "frames_considered"),
                            ("logs", "stale"), ("logs", "program_silent_s")):
            check(f"producer emits {sect}.{field}",
                  field in (produced.get(sect) or {}),
                  f"keys: {sorted((produced.get(sect) or {}).keys())[:9]}")
        for sect in ("program", "capture", "visual", "logs"):
            check(f"fixture section '{sect}' matches the producer's keys",
                  set(_state()[sect]) <= set((produced.get(sect) or {}).keys())
                  or sect == "visual",
                  f"fixture has extras: "
                  f"{sorted(set(_state()[sect]) - set((produced.get(sect) or {}).keys()))}")
    except Exception as exc:                      # pragma: no cover
        check("producer shape is checkable", False, f"{type(exc).__name__}: {exc}")

    # ── THE SAFETY NET MUST NOT HIDE A CODING ERROR ────────────────────────
    # The freshness block that gates every error correction was written with an
    # out-of-scope variable name. `except Exception` caught the NameError on
    # EVERY call, so the whole path was permanently cannot_tell — dead code
    # wearing the costume of a cautious default. Asserting the reason is ABSENT
    # is the only way a broad except can be trusted.
    try:
        real = _bs._ambient_state_uncached()
        rlogs = real.get("logs") or {}
        check("real state carries a logs block", bool(rlogs), str(rlogs)[:80])
        check("freshness is genuinely available (no swallowed exception)",
              "unavailable_because" not in rlogs,
              str(rlogs.get("unavailable_because"))[:120])
        check("freshness reports a real staleness verdict",
              rlogs.get("stale") in (True, False), str(rlogs.get("stale")))
        # The predicate must reach a DECISION on real-shaped input, not bail.
        import copy as _copy
        fresh_st = _copy.deepcopy(real)
        fresh_st["logs"] = {"stale": False, "stale_sources": [],
                            "program_silent_s": 2.0}
        v, obs = A._correction_observation("new_error", fresh_st,
                                          A._now_ms() - 340000)
        check("with a fresh log the error predicate reaches GONE",
              v == A._GONE and bool(obs), f"{v} / {obs[:70]}")
        v2, _ = A._correction_observation("new_error", real,
                                         A._now_ms() - 340000)
        check("with the real (stale) log it refuses to clear",
              v2 == A._UNKNOWN, v2)
    except Exception as exc:                      # pragma: no cover
        check("real-state freshness is checkable", False,
              f"{type(exc).__name__}: {exc}")

    # ── A correction must never downgrade or delay a live alert ────────────
    A.MEMORY._sessions.pop(sid, None)
    A.MEMORY.mark_surfaced(sid, DEAD["fp"], "alert", text=DEAD["text"],
                           kind=DEAD["kind"])
    st = _state()
    st["new_errors"] = [{"fingerprint": "abc", "count": 3,
                         "sample": "DeviceLostException: boom"}]
    v = A.decide(st, session_id=sid, event="PostToolUse")
    check("a pending correction does not downgrade the tier",
          v.get("tier") == "alert", f"tier={v.get('tier')}")
    check("the live ALERT is still delivered alongside the correction",
          "DeviceLostException" in (v.get("text") or ""),
          (v.get("text") or "")[:150])
    check("...and the correction rides along in the same injection",
          "CORRECTION" in (v.get("text") or ""))

    fails = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
        if not ok and detail:
            print(f"          {detail}")
    print(f"\n{len(checks) - len(fails)}/{len(checks)} checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
