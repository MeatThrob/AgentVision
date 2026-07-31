"""
Tests for connectors.log_sources — multi-source merge + the SOURCE-READER
interface (streamed/structured telemetry). Pure stdlib. Run:
    python3 python_backend/connectors/test_log_sources.py
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from connectors import log_sources as ls  # noqa: E402

_fails = 0


def check(name, cond, detail=""):
    global _fails
    if not cond:
        _fails += 1
    print(f"  [{'ok ' if cond else 'FAIL'}] {name}{'' if cond or not detail else '  — ' + detail}")


class _Prof:
    def __init__(self, **kw):
        self.log_sources = kw.get("log_sources", [])
        self.action_log_file = kw.get("action_log_file", "")
        self.log_file = kw.get("log_file", "")


def test_multisource_merge():
    print("multi-source merge (jsonl + text on one timeline):")
    d = tempfile.mkdtemp()
    aj = os.path.join(d, "actions.jsonl")
    tl = os.path.join(d, "app.log")
    with open(aj, "w") as f:
        f.write('{"ts_ms":1784628000000,"category":"event","source":"a","data":{"name":"tick"}}\n')
        f.write('{"ts_ms":1784628002000,"category":"error","source":"a","data":{"message":"boom"}}\n')
    with open(tl, "w") as f:
        f.write("2026-07-21 10:00:01,000 WARN app: mid event\n")   # local-ts; between the two
    p = _Prof(action_log_file=aj, log_file=tl)
    evs = ls.read_normalized(p, limit=50)
    check("all three merged", len(evs) == 3, str(len(evs)))
    # error-level filter across sources
    errs = ls.read_normalized(p, level="ERROR", limit=50)
    check("level filter → 1 error", len(errs) == 1 and errs[0]["data"].get("message") == "boom",
          str(errs))


def test_docker_json_reader():
    print("SOURCE READER: docker json-file → unified events:")
    d = tempfile.mkdtemp()
    p = os.path.join(d, "container.log")
    with open(p, "w") as f:
        f.write(json.dumps({"log": "server started\n", "stream": "stdout",
                            "time": "2026-07-21T10:00:00.000Z"}) + "\n")
        f.write(json.dumps({"log": "boom failed\n", "stream": "stderr",
                            "time": "2026-07-21T10:00:01.000Z"}) + "\n")
    prof = _Prof(log_sources=[{"path": p, "reader": "docker_json", "label": "container"}])
    evs = ls.read_normalized(prof, limit=50)
    check("2 records decoded", len(evs) == 2, str(len(evs)))
    check("stdout→stdout / stderr→error", evs[0]["category"] == "stdout"
          and evs[1]["category"] == "error", str([e["category"] for e in evs]))
    check("message decoded (newline stripped)", evs[1]["data"]["message"] == "boom failed",
          str(evs[1]["data"]))
    check("ts backfilled from `time`", isinstance(evs[0].get("ts_ms"), float)
          and evs[0]["ts_ms"] > 1e12, str(evs[0].get("ts_ms")))
    check("docker_json registered", "docker_json" in ls.list_readers())


def test_custom_reader_registration():
    print("SOURCE READER: register a custom reader:")

    class OtlpJsonlReader(ls.SourceReader):
        kind = "otlp_jsonl"

        def read_records(self, path, *, max_offset=0, tail_bytes=1_048_576):
            out = []
            for ln in self._tail_lines(path, max_offset, tail_bytes):
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                # OTLP-ish: {"severityText","body","timeUnixNano","name"}
                sev = (rec.get("severityText") or "info").lower()
                out.append({
                    "ts_ms": (rec.get("timeUnixNano", 0) / 1e6) or None,
                    "category": "error" if sev.startswith("err") or sev.startswith("fatal") else "log",
                    "level": sev, "source": rec.get("name", "otlp"),
                    "data": {"message": rec.get("body", "")},
                })
            return out

    ls.register_reader("otlp_jsonl", OtlpJsonlReader)
    check("custom reader registered", "otlp_jsonl" in ls.list_readers())
    d = tempfile.mkdtemp()
    p = os.path.join(d, "otlp.jsonl")
    with open(p, "w") as f:
        f.write(json.dumps({"severityText": "ERROR", "body": "span failed",
                            "timeUnixNano": 1784628000000000000, "name": "svc"}) + "\n")
    prof = _Prof(log_sources=[{"path": p, "reader": "otlp_jsonl", "label": "otlp"}])
    evs = ls.read_normalized(prof, limit=10)
    check("otlp decoded to error", len(evs) == 1 and evs[0]["category"] == "error", str(evs))
    check("otlp ts from nanos", evs[0].get("ts_ms") and evs[0]["ts_ms"] > 1e12, str(evs[0].get("ts_ms")))


def test_missing_source_graceful():
    print("robustness:")
    prof = _Prof(log_sources=[{"path": "/no/such/file.log", "reader": "docker_json"}])
    check("missing source → [] (no raise)", ls.read_normalized(prof) == [])
    prof2 = _Prof(log_sources=[{"path": "/no/such/file.log", "adapter": "auto"}])
    check("missing plain source → []", ls.read_normalized(prof2) == [])


if __name__ == "__main__":
    print("=" * 66)
    print("log_sources test suite")
    print("=" * 66)
    test_multisource_merge()
    test_docker_json_reader()
    test_custom_reader_registration()
    test_missing_source_graceful()
    print("=" * 66)
    if _fails:
        print(f"RESULT: {_fails} FAILURE(S)")
        sys.exit(1)
    print("RESULT: all log_sources tests passed")
    sys.exit(0)
