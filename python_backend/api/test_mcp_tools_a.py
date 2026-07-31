"""
Tests for the v5 ANALYSIS / SEARCH / INVESTIGATION bridge routes + their MCP
tools (av_diagnose, av_timeline, av_search, av_wait_for, av_diff, av_state_diff,
av_metrics, av_capabilities, av_test_adapter, av_list_adapters).

Runs via Flask's in-process test client (no port binding) like
test_bridge_routes.py: seed a temp profile with a mixed log (JSONL errors +
'wide' state records + a plain-text WARN line), seed a couple of synthetic frames
into the in-memory store, then assert each new route returns well-formed,
token-bounded JSON with the expected keys. Also asserts the MCP tool count went
51 → 61 and every new tool proxies its route.

Requires flask/psutil/pillow (run inside a venv).
    python3 python_backend/api/test_mcp_tools_a.py
"""
from __future__ import annotations
import os
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent))          # python_backend
sys.path.insert(0, str(_HERE.parent.parent.parent))   # repo root

_fails = 0


def check(name, cond, detail=""):
    global _fails
    if not cond:
        _fails += 1
    print(f"  [{'ok ' if cond else 'FAIL'}] {name}"
          f"{'' if cond or not detail else '  — ' + detail}")


def _seed_frame(bs, seq, ts_ms, *, running=True, error=None, anomaly=None,
                perf=None, summary="", tags=None, state_delta=None):
    """Insert a synthetic frame dict into the bridge's in-memory store."""
    frame = {
        "sequence": seq,
        "timestamp": bs.clock.ms_to_iso(ts_ms),
        "timestamp_ms": ts_ms,
        "summary": summary,
        "tags": tags or [],
        "program": {"running": running},
        "error": error,
        "anomaly": anomaly or {},
        "perf": perf or {},
        "state_delta": state_delta or {},
        "capture_meta": {"black_frame": False, "capture_target": "window"},
    }
    with bs._lock:
        bs._frames[seq] = frame
        bs._latest_frame = frame
    return frame


def main():
    work = tempfile.mkdtemp()
    av = os.path.join(work, "agentvision")
    os.makedirs(av)
    actions = os.path.join(av, "actions.jsonl")
    textlog = os.path.join(av, "log.txt")

    now_ms = time.time() * 1000.0
    t0 = now_ms - 4000.0
    with open(actions, "w") as f:
        # two identical errors (recurring) + a wide state record pair + an event
        f.write('{"ts_ms":%d,"ts":"%s","category":"error","level":"ERROR",'
                '"source":"app.db","data":{"message":"KeyError: cfg",'
                '"exception_type":"KeyError"}}\n' % (int(t0), _iso(t0)))
        f.write('{"ts_ms":%d,"ts":"%s","category":"error","level":"ERROR",'
                '"source":"app.db","data":{"message":"KeyError: cfg",'
                '"exception_type":"KeyError"}}\n' % (int(t0 + 500), _iso(t0 + 500)))
        f.write('{"ts_ms":%d,"ts":"%s","category":"wide","source":"app",'
                '"data":{"name":"state.wide","hp":100,"phase":"init"}}\n'
                % (int(t0 + 800), _iso(t0 + 800)))
        f.write('{"ts_ms":%d,"ts":"%s","category":"wide","source":"app",'
                '"data":{"name":"state.wide","hp":40,"phase":"combat"}}\n'
                % (int(t0 + 2500), _iso(t0 + 2500)))
        f.write('{"ts_ms":%d,"ts":"%s","category":"event","source":"app",'
                '"data":{"name":"tick"}}\n' % (int(t0 + 3000), _iso(t0 + 3000)))
    with open(textlog, "w") as f:
        f.write("2026-07-21 10:00:00.500 [main] WARN com.acme.Cache - shader recompile\n")

    from connectors.program_connector import (ProgramProfile, save_profiles,
                                               load_profiles, BUILTIN_PROFILES)
    p = load_profiles()
    p["mt"] = ProgramProfile(
        name="mt", display_name="MCPToolsTest", action_log_file=actions,
        log_file=textlog, project_root=work, process_name="nonexistent_xyz",
        log_sources=[{"path": actions, "adapter": "jsonl", "label": "events"},
                     {"path": textlog, "adapter": "auto", "label": "textlog"}])
    save_profiles({k: v for k, v in p.items()
                   if k not in BUILTIN_PROFILES or k == "mt"})

    os.environ["AGENTVISION_ACTIVE_PROFILE"] = "mt"
    import api.bridge_server as bs
    bs._active_profile_name = "mt"
    bs._collector = None
    # Make the session-start early so seeded errors count as "this session".
    bs._session_start_ms = t0 - 10_000.0
    client = bs.app.test_client()

    # Seed two synthetic frames: an error frame then a healthy one.
    _seed_frame(bs, 1, t0 + 400, error={
        "exception_type": "KeyError", "message": "KeyError: cfg",
        "probable_cause": "A dict/map was accessed with a key that does not exist.",
        "fingerprint": "fpKEYERR", "occurrence_count": 2},
        perf={"cpu_percent": 12.0, "rss_mb": 120.0, "num_threads": 8},
        summary="KeyError: cfg", tags=["error"])
    _seed_frame(bs, 2, t0 + 3200, perf={"cpu_percent": 30.0, "rss_mb": 180.0,
                                        "num_threads": 10},
                summary="Running normally; 1 state field(s) changed",
                tags=["healthy"],
                state_delta={"changed": {"phase": {"from": "init", "to": "combat"}},
                             "added": {}, "removed": {},
                             "changed_count": 1, "added_count": 0, "removed_count": 0})

    def gj(path):
        r = client.get(path)
        return r.status_code, r.get_json()

    # ── 1. /diagnose ─────────────────────────────────────────────────────────
    print("diagnose:")
    sc, d = gj("/diagnose")
    check("/diagnose ok", sc == 200, str(sc))
    check("hypotheses list", isinstance(d.get("hypotheses"), list) and d["hypotheses"])
    h0 = (d.get("hypotheses") or [{}])[0]
    check("hypothesis shape", all(k in h0 for k in
          ("summary", "confidence", "evidence", "probable_cause", "recommended_next")),
          str(list(h0.keys())))
    check("confidence 0..1", isinstance(h0.get("confidence"), (int, float))
          and 0.0 <= h0["confidence"] <= 1.0, str(h0.get("confidence")))
    check("hypotheses capped <=6", len(d["hypotheses"]) <= 6)
    check("health block", (d.get("health") or {}).get("grade") in
          ("healthy", "degraded", "unhealthy", "critical"), str(d.get("health")))
    check("top_signals list", isinstance(d.get("top_signals"), list) and d["top_signals"])
    check("recurring KeyError surfaced",
          any("KeyError" in (hp.get("summary") or "") for hp in d["hypotheses"]),
          str([hp.get("summary") for hp in d["hypotheses"]]))

    # ── 2. /timeline ─────────────────────────────────────────────────────────
    print("timeline:")
    sc, d = gj("/timeline?limit=100")
    check("/timeline ok", sc == 200)
    kinds = {r.get("kind") for r in d.get("rows", [])}
    check("merges frames + logs", "frame" in kinds and "log" in kinds, str(kinds))
    check("rows ts-sorted", _sorted([r.get("ts_ms") or 0 for r in d.get("rows", [])]))
    check("rows bounded", len(d.get("rows", [])) <= 100)
    check("row is one-line compact", all(len(r.get("line", "")) <= 160
          for r in d.get("rows", [])))
    sc, d = gj("/timeline?limit=5")
    check("limit respected", len(d.get("rows", [])) <= 5, str(len(d.get("rows", []))))

    # ── 3. /search ───────────────────────────────────────────────────────────
    print("search:")
    sc, d = gj("/search?q=KeyError")
    check("/search substring ok", sc == 200 and d.get("count", 0) >= 1, str(d.get("count")))
    sc, d = gj("/search?q=key.*cfg&regex=1")
    check("regex search matches", d.get("count", 0) >= 1, str(d.get("count")))
    sc, d = gj("/search?q=shader&level=WARN")
    check("level filter finds WARN line", d.get("count", 0) >= 1, str(d.get("count")))
    sc, d = gj("/search?q=KeyError&category=error")
    check("category filter", all(m.get("category") == "error"
          for m in d.get("matches", []) if m.get("kind") == "log"), str(d.get("matches")))
    sc, d = gj("/search?q=(bad")
    check("bad regex without flag = substring ok", sc == 200)
    sc, d = gj("/search?q=(bad&regex=1")
    check("bad regex → 400", sc == 400, str(sc))
    sc, d = gj("/search")
    check("missing q → 400", sc == 400, str(sc))

    # ── 4. /wait_for ─────────────────────────────────────────────────────────
    print("wait_for:")
    t = time.monotonic()
    r = client.post("/wait_for", json={"condition": "log", "regex": "willnevermatch_zzz",
                                       "timeout": 1.0, "poll_interval": 0.3})
    d = r.get_json()
    waited = time.monotonic() - t
    check("no-match times out cleanly", d.get("matched") is False, str(d))
    check("respects timeout cap (returned quickly)", waited < 5.0, f"{waited:.1f}s")
    check("reports waited_ms", isinstance(d.get("waited_ms"), (int, float)))
    # A 'frame' condition matches immediately once a new frame is seeded mid-wait.
    import threading

    def _late_frame():
        time.sleep(0.4)
        _seed_frame(bs, 3, time.time() * 1000.0, perf={"cpu_percent": 5.0},
                    summary="new frame", tags=["healthy"])
    threading.Thread(target=_late_frame, daemon=True).start()
    r = client.post("/wait_for", json={"condition": "frame", "timeout": 5.0,
                                       "poll_interval": 0.2})
    d = r.get_json()
    check("frame condition matches present/new frame", d.get("matched") is True, str(d))
    check("matched_frame carries seq", (d.get("matched_frame") or {}).get("seq") == 3,
          str(d.get("matched_frame")))

    # ── 5. /diff + /state_diff ───────────────────────────────────────────────
    print("diff / state_diff:")
    sc, d = gj("/diff?a=1&b=2")
    check("/diff ok", sc == 200)
    check("diff perf_delta present", "perf_delta" in d and
          d["perf_delta"]["rss_mb"]["delta"] == 60.0, str(d.get("perf_delta")))
    check("diff state_delta present", "state_delta" in d and
          "changed_count" in d["state_delta"])
    check("diff resolved_error (err→healthy)", d.get("resolved_error") is not None,
          str(d.get("resolved_error")))
    sc, d = gj("/diff?a=1&b=999999")
    check("diff missing frame → 404", sc == 404, str(sc))
    sc, d = gj("/state_diff?a_ms=%d&b_ms=%d" % (int(t0 + 800), int(t0 + 2500)))
    check("/state_diff ok", sc == 200)
    sdl = d.get("state_delta") or {}
    check("state_diff detects hp/phase change",
          sdl.get("changed_count", 0) >= 1, str(sdl))

    # ── 6. /metrics ──────────────────────────────────────────────────────────
    print("metrics:")
    sc, d = gj("/metrics?window=50")
    check("/metrics ok", sc == 200)
    check("perf cpu series", (d.get("perf") or {}).get("cpu_percent", {}).get("max")
          is not None, str(d.get("perf")))
    check("error_rate block", "rate" in (d.get("error_rate") or {}))
    check("capture block", "shots_per_second" in (d.get("capture") or {}))

    # ── 7. introspection: /capabilities /adapter/test /adapters ──────────────
    print("capabilities / adapter test / list adapters:")
    sc, d = gj("/capabilities")
    check("/capabilities ok", sc == 200)
    check("adapter count reported", (d.get("counts") or {}).get("log_adapters", 0) > 100,
          str(d.get("counts")))
    check("flagship = av_diagnose", d.get("flagship_tool") == "av_diagnose")
    check("tool_catalog groups", "diagnose" in (d.get("tool_catalog") or {}))

    sc, d = gj("/adapter/test?line=" + _q("2026-07-21 10:00:00,123 ERROR root: boom"))
    check("/adapter/test ok", sc == 200)
    check("adapter detected", bool(d.get("adapter")))
    check("top_scores list", isinstance(d.get("top_scores"), list) and d["top_scores"])
    check("parsed event", isinstance(d.get("event"), dict))
    sc, d = gj("/adapter/test")
    check("adapter/test missing line → 400", sc == 400)

    sc, d = gj("/adapters?limit=10")
    check("/adapters ok", sc == 200)
    check("adapters paginated", len(d.get("adapters", [])) <= 10, str(len(d.get("adapters", []))))
    check("adapters total large", d.get("total", 0) > 100, str(d.get("total")))
    check("families map", isinstance(d.get("families"), dict) and d["families"])
    check("adapter row shape", all(set(("name", "language", "family")) <= set(a.keys())
          for a in d.get("adapters", [])), str(d.get("adapters", [])[:1]))
    sc, d2 = gj("/adapters?q=json&limit=5")
    check("adapters q filter", all("json" in a["name"].lower()
          for a in d2.get("adapters", [])), str([a["name"] for a in d2.get("adapters", [])]))

    # ── 8. MCP tool wiring: 51 → 61, every new tool proxies its route ────────
    # NOTE: read claude_mcp.py as text — importing it hard-exits when the optional
    # `mcp` package isn't installed (FastMCP import guard).
    print("MCP tool surface:")
    src = (_HERE.parent / "claude_mcp.py").read_text()
    tool_count = src.count("@mcp.tool()")
    # Batch A brought the surface to 61; later batches only ADD tools, so this
    # asserts the floor (>=61) plus that every batch-A tool below is still wired.
    check("tool count >= 61 (batch A floor)", tool_count >= 61, str(tool_count))
    new_tools = ["av_diagnose", "av_timeline", "av_search", "av_wait_for",
                 "av_diff", "av_state_diff", "av_metrics", "av_capabilities",
                 "av_test_adapter", "av_list_adapters"]
    for t in new_tools:
        check(f"tool {t} defined", f"def {t}(" in src)
    # each proxies a bridge route
    for route in ("/diagnose", "/timeline", "/search", "/wait_for", "/diff",
                  "/state_diff", "/metrics", "/capabilities", "/adapter/test",
                  "/adapters"):
        check(f"route {route} referenced by an MCP tool", f'"{route}"' in src)
    check("av_status points at av_diagnose", "av_diagnose" in src and
          "INVESTIGATING A FAILURE" in src)

    # cleanup profile
    p = load_profiles()
    p.pop("mt", None)
    save_profiles({k: v for k, v in p.items() if k not in BUILTIN_PROFILES})

    print("=" * 66)
    if _fails:
        print(f"RESULT: {_fails} FAILURE(S)")
        return 1
    print("RESULT: all MCP-tools-A tests passed")
    return 0


def _iso(ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc) \
        .isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sorted(xs):
    return all(xs[i] <= xs[i + 1] for i in range(len(xs) - 1))


def _q(s):
    import urllib.parse
    return urllib.parse.quote(s, safe="")


if __name__ == "__main__":
    print("=" * 66)
    print("MCP analysis/search/investigation tools test suite")
    print("=" * 66)
    raise SystemExit(main())
