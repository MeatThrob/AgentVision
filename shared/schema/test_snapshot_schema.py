"""
Unit tests for the enriched SnapshotFrame schema (pure stdlib). Run:
    python3 shared/schema/test_snapshot_schema.py
Verifies: schema_version stamping, the v2 AI-triage fields, structured ErrorInfo/
AnomalyInfo, and that from_json tolerates older/newer/extra-key sidecars.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from shared.schema.snapshot_schema import (  # noqa: E402
    SnapshotFrame, ErrorInfo, AnomalyInfo, ProgramState, SCHEMA_VERSION,
)

_fails = 0


def check(name, cond, detail=""):
    global _fails
    if not cond:
        _fails += 1
    print(f"  [{'ok ' if cond else 'FAIL'}] {name}{'' if cond or not detail else '  — ' + detail}")


def test_defaults():
    print("frame defaults:")
    f = SnapshotFrame()
    check("schema_version stamped", f.schema_version == SCHEMA_VERSION)
    for fld in ("summary", "recommended_next", "tags", "confidence",
                "state_delta", "correlation"):
        check(f"has v2 field {fld}", hasattr(f, fld))
    check("tags is list", isinstance(f.tags, list))


def test_error_fields():
    print("structured ErrorInfo:")
    e = ErrorInfo(message="boom", exception_type="KeyError",
                  frames=[{"file": "a.py", "line": 3, "func": "f"}],
                  probable_cause="x", fingerprint="abc123", occurrence_count=5)
    for fld in ("exception_type", "language", "frames", "probable_cause",
                "fingerprint", "occurrence_count", "first_seen", "last_seen"):
        check(f"ErrorInfo.{fld}", hasattr(e, fld))
    check("frames retained", e.frames[0]["line"] == 3)


def test_anomaly_fields():
    print("structured AnomalyInfo:")
    a = AnomalyInfo(detected=True, type="error_in_log", confidence=0.9,
                    evidence=["x", "y"])
    check("confidence", a.confidence == 0.9)
    check("evidence", a.evidence == ["x", "y"])


def test_roundtrip():
    print("to_json / from_json round-trip:")
    f = SnapshotFrame(sequence=7, summary="hi", tags=["error"], confidence=0.2)
    f.error = ErrorInfo(exception_type="TypeError", frames=[{"file": "x", "line": 1, "func": "g"}])
    f.anomaly = AnomalyInfo(detected=True, type="t", confidence=0.5, evidence=["e"])
    f.program = ProgramState(name="P", running=True)
    g = SnapshotFrame.from_json(f.to_json())
    check("sequence", g.sequence == 7)
    check("summary", g.summary == "hi")
    check("tags", g.tags == ["error"])
    check("error type", g.error.exception_type == "TypeError")
    check("anomaly evidence", g.anomaly.evidence == ["e"])
    check("program running", g.program.running is True)
    check("schema_version preserved", g.schema_version == SCHEMA_VERSION)


def test_tolerant_from_json():
    print("from_json tolerance (drift-proof):")
    # An OLD sidecar (no v2 fields) must load without error and default them.
    old = json.dumps({"sequence": 3, "timestamp": "t",
                      "program": {"name": "P", "running": True},
                      "error": {"message": "m", "stack_trace": "s"}})
    f = SnapshotFrame.from_json(old)
    check("old sidecar loads", f.sequence == 3 and f.summary == "")
    check("old error defaults v2", f.error.exception_type == "" and f.error.frames == [])
    # A FUTURE sidecar with UNKNOWN extra keys must not raise.
    future = json.dumps({"sequence": 4, "totally_new_field": 123,
                         "error": {"message": "m", "future_error_field": "z"},
                         "anomaly": {"detected": True, "future_anom": 1}})
    try:
        f = SnapshotFrame.from_json(future)
        check("future sidecar tolerated", f.sequence == 4)
    except Exception as ex:
        check("future sidecar tolerated", False, str(ex))

    # A nested key whose VALUE drifted to a non-dict (e.g. error:"oops")
    # must not raise AttributeError out of from_json.
    try:
        f = SnapshotFrame.from_json('{"error":"oops"}')
        check("nested non-dict tolerated", f.error is None, str(f.error))
    except Exception as ex:
        check("nested non-dict tolerated", False, str(ex))
    # A present-but-EMPTY nested dict must yield a defaulted dataclass, not a
    # raw {} leaking into a dataclass-typed field.
    f = SnapshotFrame.from_json('{"program":{}}')
    check("empty nested dict → dataclass", isinstance(f.program, ProgramState), type(f.program).__name__)
    check("empty nested dict usable", f.program.running is False)
    # A nested list with a non-dict element must be skipped, not crash.
    f = SnapshotFrame.from_json('{"activity":[{"ts":"t","description":"d"}, "junk", 5]}')
    check("activity filters non-dicts", len(f.activity) == 1 and f.activity[0].description == "d",
          str(f.activity))
    # program value drifted to a non-dict → defaulted dataclass, still usable.
    f = SnapshotFrame.from_json('{"program":"notadict"}')
    check("nested non-dict → default dataclass", isinstance(f.program, ProgramState)
          and f.program.running is False)


if __name__ == "__main__":
    print("=" * 66)
    print("snapshot_schema test suite")
    print("=" * 66)
    test_defaults()
    test_error_fields()
    test_anomaly_fields()
    test_roundtrip()
    test_tolerant_from_json()
    print("=" * 66)
    if _fails:
        print(f"RESULT: {_fails} FAILURE(S)")
        sys.exit(1)
    print("RESULT: all schema tests passed")
    sys.exit(0)
