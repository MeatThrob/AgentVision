"""
Bridge HTTP-route tests via Flask's in-process test client (no port binding).
Requires flask/psutil/pillow (run inside a venv). Run:
    python3 python_backend/api/test_bridge_routes.py

Covers: /status capture_rate, /capture/status health+rate, /events/schema
version+categories, /log/sources + /log/normalized (multi-format merge),
/digest triage shape, and uniform JSON error responses.
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent))          # python_backend
sys.path.insert(0, str(_HERE.parent.parent.parent))   # repo root

_fails = 0


def check(name, cond, detail=""):
    global _fails
    if not cond:
        _fails += 1
    print(f"  [{'ok ' if cond else 'FAIL'}] {name}{'' if cond or not detail else '  — ' + detail}")


def main():
    # Build a temp profile with a two-format log set BEFORE importing the bridge.
    work = tempfile.mkdtemp()
    av = os.path.join(work, "agentvision")
    os.makedirs(av)
    actions = os.path.join(av, "actions.jsonl")
    textlog = os.path.join(av, "log.txt")
    with open(actions, "w") as f:
        f.write('{"ts_ms":1784628000000,"ts":"2026-07-21T10:00:00.000Z","category":"error","level":"ERROR","source":"app.db","data":{"message":"KeyError: cfg","exception_type":"KeyError"}}\n')
        f.write('{"ts_ms":1784628001000,"ts":"2026-07-21T10:00:01.000Z","category":"error","level":"ERROR","source":"app.db","data":{"message":"KeyError: cfg","exception_type":"KeyError"}}\n')
        f.write('{"ts_ms":1784628002000,"ts":"2026-07-21T10:00:02.000Z","category":"event","source":"app","data":{"name":"tick"}}\n')
    with open(textlog, "w") as f:
        f.write("2026-07-21 10:00:00.500 [main] WARN com.acme.Cache - shader recompile\n")

    from connectors.program_connector import (ProgramProfile, save_profiles,
                                              load_profiles, BUILTIN_PROFILES)
    p = load_profiles()
    p["rt"] = ProgramProfile(
        name="rt", display_name="RouteTest", action_log_file=actions,
        log_file=textlog, project_root=work, process_name="nonexistent_xyz",
        log_sources=[{"path": actions, "adapter": "jsonl", "label": "events"},
                     {"path": textlog, "adapter": "auto", "label": "textlog"}])
    save_profiles({k: v for k, v in p.items() if k not in BUILTIN_PROFILES or k == "rt"})

    os.environ["AGENTVISION_ACTIVE_PROFILE"] = "rt"
    import api.bridge_server as bs
    bs._active_profile_name = "rt"
    bs._collector = None
    client = bs.app.test_client()

    def gj(path):
        r = client.get(path)
        return r.status_code, r.get_json()

    print("status + capture rate:")
    sc, d = gj("/status")
    check("/status ok", sc == 200)
    check("capture_rate.guidance present",
          "guidance" in (d.get("capture_rate") or {}))
    check("capture_rate has fps range",
          "supported_fps_range" in (d.get("capture_rate") or {}))

    print("capture/status:")
    sc, d = gj("/capture/status")
    check("has health", "health" in d and "rate" in d)
    check("shots_per_second numeric", isinstance(d.get("shots_per_second"), (int, float)))

    print("events/schema:")
    sc, d = gj("/events/schema")
    check("schema_version present", bool(d.get("schema_version")))
    check("error category documented", "error" in (d.get("categories") or {}))

    print("log/sources + log/normalized:")
    sc, d = gj("/log/sources")
    labels = {s["label"]: s for s in d.get("sources", [])}
    check("two sources resolved", "events" in labels and "textlog" in labels, str(labels))
    check("jsonl detected", labels.get("events", {}).get("detected_adapter") == "jsonl")
    sc, d = gj("/log/normalized?from_ms=1784628000000&to_ms=1784628003000&limit=50")
    check("normalized merged >=3", d.get("count", 0) >= 3, str(d.get("count")))
    sc, d = gj("/log/normalized?from_ms=1784628000000&to_ms=1784628003000&level=ERROR")
    check("level filter → errors only", d.get("count") == 2, str(d.get("count")))

    print("digest:")
    sc, d = gj("/digest")
    check("/digest ok", sc == 200)
    check("schema_version", d.get("schema_version"))
    check("attention list", isinstance(d.get("attention"), list) and d["attention"])
    check("top_errors deduped w/ count", d["top_errors"] and d["top_errors"][0]["count"] == 2,
          str(d.get("top_errors")))
    check("capture block", "capture" in d and "shots_per_second" in d["capture"])
    check("alignment block", "alignment" in d)
    check("guidance", bool(d.get("guidance")))
    # health score (0-100, bounded) + factors
    h = d.get("health") or {}
    check("health score bounded", isinstance(h.get("score"), int) and 0 <= h["score"] <= 100, str(h))
    check("health grade", h.get("grade") in ("healthy", "degraded", "unhealthy", "critical"), str(h))
    check("health factors list", isinstance(h.get("factors"), list) and h["factors"])
    # error-rate / trend stats
    es = d.get("error_stats") or {}
    check("error_stats fields", all(k in es for k in
          ("total_failures", "distinct_fingerprints", "new_this_session",
           "recurring", "error_rate_recent", "trend")), str(es))
    check("error_stats recurring>=1 (2 same errors)", es.get("recurring", 0) >= 1, str(es))
    check("error_stats trend value", es.get("trend") in
          ("rising", "steady", "falling", "unknown"), str(es.get("trend")))

    print("uniform JSON errors:")
    r = client.get("/frame/99999999")
    check("404 is JSON", r.content_type.startswith("application/json"))
    check("404 has status field", (r.get_json() or {}).get("status") == 404)
    r = client.get("/no/such/route")
    check("unknown route JSON 404", (r.get_json() or {}).get("status") == 404)

    print("numeric-param validation (client error → 400, not 500):")
    # A bad limit is a CLIENT error — must be 400, matching from_ms/to_ms.
    sc, _ = gj("/log/range?limit=abc")
    check("bad limit → 400", sc == 400, str(sc))
    # …and a good limit still works.
    sc, _ = gj("/log/range?from_ms=0&to_ms=1&limit=5")
    check("good limit → 200", sc == 200, str(sc))
    sc, _ = gj("/log/normalized?limit=abc")
    check("bad normalized limit → 400", sc == 400, str(sc))
    sc, _ = gj("/program/log?lines=abc")
    check("bad lines → 400", sc == 400, str(sc))
    sc, _ = gj("/debug/log?lines=abc")
    check("bad debug lines → 400", sc == 400, str(sc))
    # from_ms itself stays guarded (regression guard for the existing behaviour).
    sc, _ = gj("/log/range?from_ms=abc")
    check("bad from_ms still → 400", sc == 400, str(sc))
    # PUT /capture/interval with a non-numeric body → 400, not 500.
    r = client.put("/capture/interval", json={"interval": "abc"})
    check("bad interval → 400", r.status_code == 400, str(r.status_code))

    # cleanup profile
    p = load_profiles()
    p.pop("rt", None)
    save_profiles({k: v for k, v in p.items() if k not in BUILTIN_PROFILES})

    print("=" * 66)
    if _fails:
        print(f"RESULT: {_fails} FAILURE(S)")
        return 1
    print("RESULT: all bridge-route tests passed")
    return 0


if __name__ == "__main__":
    print("=" * 66)
    print("bridge routes test suite")
    print("=" * 66)
    raise SystemExit(main())
