#!/usr/bin/env python3
"""
Structured error recovery: the frame's `error` field must see JSON records.
================================================================================
`frame.error` is the trigger for incident freezing, retention flagging and the
av_diagnose fingerprint. It used to come ONLY from last_error_from_log(), which
substring-greps the single primary text log for "ERROR"/"CRITICAL"/"Traceback".
Two real failures followed, both found against a live capture:

  1. A program that reports errors PROPERLY — structured JSONL with
     {category:"error", data:{message, exception_type}}, which is exactly what
     AgentVision's own universal emitter writes — produced NO frame.error at all.
     No incident, no retention flag, no fingerprint.

  2. When the primary log IS that JSONL file, the text grep matched the raw
     serialized record, so the "message" became the entire JSON line and the
     exception type was lost:
         message: '{"ts_ms": 1785285055226.5, "category": "error", ...}'

These pin both paths, plus the hostile-input contract (a parser on the capture
hot path must never raise).
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path[:0] = [str(_HERE.parent), str(_HERE.parent.parent)]

from context_collector import (_error_from_actions, _error_from_json_line,   # noqa: E402
                               _ERROR_LEVELS)

_passed = _failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1; print(f"  [ok  ] {label}")
    else:
        _failed += 1; print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


def _rec(**data):
    return {"ts_ms": 1.0, "category": "error", "level": "ERROR",
            "source": "test", "data": data}


def test_structured_records():
    print("structured JSONL records reach frame.error:")
    msg, block, etype = _error_from_actions(
        [_rec(message="Metal surface lost", exception_type="MetalPresentException")])
    check("exception type recovered", etype == "MetalPresentException", etype)
    check("message and type combined for the parser",
          msg == "MetalPresentException: Metal surface lost", msg)
    check("block falls back to the line when there is no stack", block == msg)

    _, blk, _ = _error_from_actions(
        [_rec(message="boom", exception_type="T", stack="at A()\nat B()")])
    check("a real stack is preserved as the block", blk == "at A()\nat B()", blk)

    for alt in ("stack_trace", "traceback"):
        _, b, _ = _error_from_actions([_rec(message="x", **{alt: "at Z()"})])
        check(f"'{alt}' is accepted as a stack", b == "at Z()", b)

    for alt in ("msg",):
        m, _, _ = _error_from_actions([_rec(**{alt: "alt message"})])
        check(f"'{alt}' is accepted as a message", m == "alt message", m)

    print("severity detection:")
    for lvl in sorted(_ERROR_LEVELS):
        m, _, _ = _error_from_actions(
            [{"level": lvl, "data": {"message": f"{lvl} thing"}}])
        check(f"level={lvl} counts as an error", bool(m), repr(m))
    m, _, _ = _error_from_actions([{"level": "INFO", "data": {"message": "fine"}}])
    check("level=INFO is NOT an error", m == "", repr(m))
    m, _, _ = _error_from_actions([{"category": "event", "data": {"message": "tick"}}])
    check("category=event is NOT an error", m == "", repr(m))
    m, _, _ = _error_from_actions([{"category": "ERROR", "data": {"message": "up"}}])
    check("category is matched case-insensitively", bool(m), repr(m))

    print("newest record wins:")
    m, _, _ = _error_from_actions([_rec(message="older"), _rec(message="newer")])
    check("the most recent error is the one reported", m == "newer", m)

    print("hostile input never raises (this is on the capture hot path):")
    for bad in (None, [], [None], ["a string"], [123], [{}], [_rec()],
                [{"data": None, "level": "ERROR"}],
                [{"data": {"message": None}, "category": "error"}]):
        try:
            r = _error_from_actions(bad)
            ok = isinstance(r, tuple) and len(r) == 3
        except Exception as exc:
            ok = False
            print(f"        raised on {bad!r}: {exc}")
        check(f"survives {str(bad)[:34]}", ok)
    check("an empty record yields nothing rather than a blank error",
          _error_from_actions([_rec()])[0] == "")


def test_jsonl_line_unwrap():
    print("a JSONL line matched by the TEXT scan is unwrapped:")
    line = json.dumps(_rec(message="device lost while presenting swapchain",
                           exception_type="DeviceLostException"))
    msg, block, etype = _error_from_json_line(line)
    check("type recovered from the raw line", etype == "DeviceLostException", etype)
    check("message is the real message, not the whole record",
          msg == "DeviceLostException: device lost while presenting swapchain", msg)
    check("the raw JSON does not leak into the message",
          "ts_ms" not in msg and "{" not in msg, msg)

    print("plain text and junk are left completely alone:")
    for txt in ("2026-01-01 12:00:00 ERROR something broke",
                "Traceback (most recent call last):",
                "{not json at all", "{}", "[]", "", "   ",
                json.dumps({"category": "event", "data": {"message": "tick"}}),
                json.dumps(["not", "an", "object"])):
        r = _error_from_json_line(txt)
        check(f"untouched: {txt[:38]!r}", r == ("", "", ""), str(r))
    check("None is safe", _error_from_json_line(None) == ("", "", ""))

    print("leading whitespace is tolerated (log writers indent):")
    r = _error_from_json_line("   " + line)
    check("indented JSON still unwraps", r[2] == "DeviceLostException", str(r))


if __name__ == "__main__":
    print("=" * 70)
    print("STRUCTURED ERROR RECOVERY")
    print("=" * 70)
    test_structured_records()
    test_jsonl_line_unwrap()
    print("=" * 70)
    print(f"RESULT: {_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
