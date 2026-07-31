#!/usr/bin/env python3
"""
Content-based severity: a failure in the message body must not read as INFO.
================================================================================
Found live against a running emulator. SharpEmu logs:

    [METAL] guestgpu.present src=0x5D80000 ok=False

"[METAL]" is the SOURCE channel, not a level, so the adapter found no severity
and defaulted to INFO. 180 consecutive GPU present failures therefore normalized
to INFO, av_log_normalized(level="WARN") returned ZERO events, and av_diagnose
reported "health 100 — no strong failure signals, program looks healthy" while
the program was failing to present a single frame. A diagnostic tool that
confidently certifies health through 180 failures is worse than no tool at all.

The fix reads the message CONTENT when the adapter found no real level. The hard
part is NOT finding failures — it is not crying wolf. "errors=0", "failed=0",
"ok=True", "rc=0" and "no errors detected" are all GOOD news, and the obvious
/error|fail/ keyword match would flag every healthy heartbeat in every log
AgentVision parses. So the false-positive half of this suite is the important
half, and it is deliberately larger than the true-positive half.

Two invariants are also pinned:
  * escalation NEVER exceeds WARN — inferring an ERROR would mint a fingerprint
    and freeze an incident off a guess.
  * an explicit level from the log ALWAYS wins; the log's own labelling is
    authoritative and is never second-guessed or downgraded.
"""
from __future__ import annotations
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path[:0] = [str(_HERE.parents[1]), str(_HERE.parents[2])]

from connectors import log_adapters as la          # noqa: E402

_passed = _failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1; print(f"  [ok  ] {label}")
    else:
        _failed += 1; print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


#: Real failure shapes seen in the wild. Each MUST escalate.
MUST_ESCALATE = [
    "[METAL] guestgpu.present src=0x5D80000 ok=False",   # the live case
    "present ok=false",
    "present ok=0",
    "swap succeeded=false",
    "upload successful=no",
    "flush failed=true",
    "commit failure=1",
    "handshake error=true",
    "errors=3 during flush",
    "error=7",
    "failures=2",
    "dropped=12 packets",
    "lost=1 frame",
    "GET /api/v1/thing status=503",
    "POST /login status=401",
    "worker exited rc=1",
    "child exit_code=137",
    "process returncode=-9",
    "Failed to open device /dev/dri/card0",
    "unable to allocate staging buffer",
    "request timed out after 30s",
    "timeout exceeded on read",
    "timeout waiting for device",
    "connection refused by peer",
    "host unreachable",
]

#: GOOD news that merely CONTAINS failure vocabulary. None may escalate — this is
#: where a naive keyword matcher does real damage.
MUST_NOT_ESCALATE = [
    "[METAL] guestgpu.present src=0xB840000 ok=True",
    "present ok=true",
    "present ok=1",
    "errors=0",
    "error=0",
    "failures=0",
    "failed=0",
    "dropped=0",
    "lost=0",
    "error_count=0",
    "failure_rate=0",
    "GET /api/v1/thing status=200",
    "POST /login status=304",
    "worker exited rc=0",
    "child exit_code=0",
    "process returncode=0",
    "succeeded=true",
    "success=1",
    "no errors detected",
    "0 errors, 0 warnings",
    "this operation cannot be undone",
    "error handling initialised",
    "failover controller ready",
    "Loaded 42 shaders",
    "timeout configured as 30s",       # configuration, not a failure
    "default timeout is 5000ms",
    "retry policy: 3 attempts",
]


def test_true_positives():
    print("real failures escalate to WARN:")
    for t in MUST_ESCALATE:
        lvl, why = la.content_severity(t)
        check(f"{t[:52]!r} -> {lvl or '-'} ({why or '-'})", lvl == "WARN", lvl)


def test_false_positives():
    print("GOOD news containing failure words must NOT escalate:")
    for t in MUST_NOT_ESCALATE:
        lvl, why = la.content_severity(t)
        check(f"{t[:52]!r} stays INFO", lvl == "", f"got {lvl} ({why})")


def test_never_exceeds_warn():
    print("escalation is capped at WARN (never invents an ERROR):")
    for t in MUST_ESCALATE:
        lvl, _ = la.content_severity(t)
        if lvl and lvl != "WARN":
            check(f"{t[:40]!r} escalated past WARN", False, lvl)
            return
    check("no input anywhere escalates beyond WARN", True)


def test_explicit_level_wins():
    print("the log's own level is authoritative and never overridden:")
    # A real ERROR must stay ERROR, not be rewritten.
    ev = {"level": "ERROR", "category": "error",
          "data": {"message": "present ok=False"}}
    out = la.escalate_by_content(dict(ev))
    check("explicit ERROR is untouched", out["level"] == "ERROR", out["level"])
    check("no escalation marker added on ERROR",
          "level_escalated_from" not in out)
    ev = {"level": "WARN", "data": {"message": "present ok=False"}}
    check("explicit WARN is untouched",
          la.escalate_by_content(dict(ev))["level"] == "WARN")
    ev = {"level": "FATAL", "data": {"message": "all fine ok=True"}}
    check("explicit FATAL is untouched",
          la.escalate_by_content(dict(ev))["level"] == "FATAL")

    print("weakly-levelled events DO get a second look:")
    for weak in ("", "INFO", "DEBUG", "TRACE", "NOTICE", "VERBOSE"):
        ev = {"level": weak, "data": {"message": "guestgpu.present ok=False"}}
        out = la.escalate_by_content(dict(ev))
        check(f"level={weak or '(none)'!r} -> WARN", out["level"] == "WARN",
              out["level"])
        check(f"level={weak or '(none)'!r} records what it came from",
              out.get("level_escalated_from") == (weak.upper() or "(none)"),
              str(out.get("level_escalated_from")))


def test_event_shape():
    print("the escalated event stays a valid event and is auditable:")
    ev = {"level": "INFO", "category": "log",
          "data": {"message": "guestgpu.present ok=False"}, "raw": "raw line"}
    out = la.escalate_by_content(ev)
    check("level raised", out["level"] == "WARN")
    check("category follows the level", out["category"] == "error"
          or out["category"] == la.level_to_category("WARN"), out["category"])
    check("reason is human-readable and names the trigger",
          "ok=false" in out.get("escalation_reason", ""),
          out.get("escalation_reason"))
    check("original data preserved",
          out["data"]["message"] == "guestgpu.present ok=False")
    check("mutates in place and returns the same object", out is ev)

    print("falls back to raw when there is no data.message:")
    ev2 = {"level": "INFO", "raw": "worker exited rc=1"}
    check("raw text is inspected", la.escalate_by_content(ev2)["level"] == "WARN")

    print("hostile input never raises:")
    for bad in (None, {}, {"level": None}, {"data": None},
                {"data": {"message": None}}, {"data": "not a dict"},
                {"level": 5, "data": {"message": 7}}):
        try:
            la.escalate_by_content(bad)
            ok = True
        except Exception as exc:
            ok = False
            print(f"        raised on {bad!r}: {exc}")
        check(f"survives {str(bad)[:34]}", ok)
    check("empty text yields nothing", la.content_severity("") == ("", ""))
    check("None text yields nothing", la.content_severity(None) == ("", ""))


def test_kill_switch():
    print("it can be turned off:")
    orig = la.CONTENT_SEVERITY
    try:
        la.CONTENT_SEVERITY = False
        check("disabled -> no inference",
              la.content_severity("present ok=False") == ("", ""))
        ev = {"level": "INFO", "data": {"message": "present ok=False"}}
        check("disabled -> event untouched",
              la.escalate_by_content(ev)["level"] == "INFO")
    finally:
        la.CONTENT_SEVERITY = orig
    check("re-enabled", la.content_severity("present ok=False")[0] == "WARN")


if __name__ == "__main__":
    print("=" * 70)
    print("CONTENT-BASED SEVERITY")
    print("=" * 70)
    test_true_positives()
    test_false_positives()
    test_never_exceeds_warn()
    test_explicit_level_wins()
    test_event_shape()
    test_kill_switch()
    print("=" * 70)
    print(f"RESULT: {_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
