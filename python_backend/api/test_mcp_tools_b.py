"""
Tests for the v5 batch-B high-value bridge routes + their MCP tools:
  av_session_report, av_ocr_frame, av_read_screen, av_source_at_error,
  av_baseline, av_watch, av_watches.

Runs via Flask's in-process test client (no port binding) like
test_mcp_tools_a.py: seed a temp profile with a JSONL log (recurring errors +
'wide' state) and a real source file, seed synthetic frames — including one
carrying a STRUCTURED error whose frames point at that real mirrored file — then
assert each new route returns well-formed, token-bounded JSON.

Asserts specifically:
  • session_report composes digest+diagnose+timeline (health/hypotheses/
    top_errors/frames_of_interest) AND returns a bounded `markdown` string.
  • ocr degrades GRACEFULLY to {available:false, install_hint} when tesseract is
    absent (and, if it IS installed, returns the expected text/lines shape).
  • source_at_error returns code_context for the seeded error's frame.
  • baseline stamps a ts_ms marker (and GET reads it back).
  • watch registers, av_watches returns hits for a matching seeded event, and
    clear=1 empties the watch set.
  • the MCP tool count has not regressed below 68 and every new tool proxies its
    route (a floor, not an equality — see the note at the check).

Requires flask/psutil/pillow (run inside a venv).
    python3 python_backend/api/test_mcp_tools_b.py
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
                perf=None, summary="", tags=None, state_delta=None,
                annotated_image=""):
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
        "annotated_image": annotated_image,
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

    # A real source file the seeded error's stack frame points at.
    srcdir = os.path.join(work, "app")
    os.makedirs(srcdir)
    dbpy = os.path.join(srcdir, "db.py")
    with open(dbpy, "w") as f:
        f.write("def load_cfg(cfg):\n"
                "    # line 2\n"
                "    return cfg['missing_key']   # line 3 — the boom\n"
                "    # line 4\n"
                "    # line 5\n")

    now_ms = time.time() * 1000.0
    t0 = now_ms - 4000.0
    with open(actions, "w") as f:
        f.write('{"ts_ms":%d,"ts":"%s","category":"error","level":"ERROR",'
                '"source":"app.db","data":{"message":"KeyError: cfg",'
                '"exception_type":"KeyError"}}\n' % (int(t0), _iso(t0)))
        f.write('{"ts_ms":%d,"ts":"%s","category":"error","level":"ERROR",'
                '"source":"app.db","data":{"message":"KeyError: cfg",'
                '"exception_type":"KeyError"}}\n' % (int(t0 + 500), _iso(t0 + 500)))
        f.write('{"ts_ms":%d,"ts":"%s","category":"wide","source":"app",'
                '"data":{"name":"state.wide","hp":100,"phase":"init"}}\n'
                % (int(t0 + 800), _iso(t0 + 800)))
        f.write('{"ts_ms":%d,"ts":"%s","category":"event","source":"app",'
                '"data":{"name":"tick"}}\n' % (int(t0 + 3000), _iso(t0 + 3000)))
    with open(textlog, "w") as f:
        f.write("2026-07-21 10:00:00.500 [main] WARN com.acme.Cache - shader recompile\n")

    from connectors.program_connector import (ProgramProfile, save_profiles,
                                               load_profiles, BUILTIN_PROFILES)
    p = load_profiles()
    p["mtb"] = ProgramProfile(
        name="mtb", display_name="MCPToolsTestB", action_log_file=actions,
        log_file=textlog, project_root=work, process_name="nonexistent_xyz",
        log_sources=[{"path": actions, "adapter": "jsonl", "label": "events"},
                     {"path": textlog, "adapter": "auto", "label": "textlog"}])
    save_profiles({k: v for k, v in p.items()
                   if k not in BUILTIN_PROFILES or k == "mtb"})

    os.environ["AGENTVISION_ACTIVE_PROFILE"] = "mtb"
    # Isolate the persisted baseline marker to a temp file (don't touch repo log/).
    os.environ["AGENTVISION_BASELINES"] = os.path.join(work, "baselines.json")
    import api.bridge_server as bs
    bs._active_profile_name = "mtb"
    bs._collector = None
    bs._session_start_ms = t0 - 10_000.0
    # Reset in-memory watch/baseline state for a clean run.
    with bs._watch_lock:
        bs._watches.clear()
        bs._baselines.clear()
    bs._BASELINES_PATH = Path(os.environ["AGENTVISION_BASELINES"])
    client = bs.app.test_client()

    # Seed: a healthy frame, then an ERROR frame (latest) with a structured error
    # whose stack frame points at app/db.py:3 (a real, resolvable file).
    _seed_frame(bs, 1, t0 + 400, perf={"cpu_percent": 12.0, "rss_mb": 120.0,
                                        "num_threads": 8},
                summary="Running normally", tags=["healthy"])
    _seed_frame(bs, 2, t0 + 3200, error={
        "exception_type": "KeyError", "message": "KeyError: 'missing_key'",
        "probable_cause": "A dict/map was accessed with a key that does not exist.",
        "fingerprint": "fpSRC", "occurrence_count": 2,
        "frames": [{"file": dbpy, "line": 3, "func": "load_cfg"}]},
        perf={"cpu_percent": 30.0, "rss_mb": 180.0, "num_threads": 10},
        summary="KeyError: 'missing_key'", tags=["error"])

    def gj(path):
        r = client.get(path)
        return r.status_code, r.get_json()

    # ── 1. /session_report ───────────────────────────────────────────────────
    print("session_report:")
    sc, d = gj("/session_report")
    check("/session_report ok", sc == 200, str(sc))
    check("health block", (d.get("health") or {}).get("grade") in
          ("healthy", "degraded", "unhealthy", "critical"), str(d.get("health")))
    check("composes hypotheses (from diagnose)", isinstance(d.get("hypotheses"), list))
    check("composes top_errors (from digest)", isinstance(d.get("top_errors"), list))
    check("top_signals present", isinstance(d.get("top_signals"), list))
    check("frames_of_interest lists the error frame",
          any(fr.get("seq") == 2 for fr in d.get("frames_of_interest", [])),
          str(d.get("frames_of_interest")))
    check("key_moments list", isinstance(d.get("key_moments"), list))
    check("capture coverage block", "frames_stored" in (d.get("capture") or {}))
    md = d.get("markdown")
    check("markdown is a string", isinstance(md, str) and bool(md))
    check("markdown bounded (<=8.2KB)", isinstance(md, str) and len(md) <= 8200,
          str(len(md) if isinstance(md, str) else None))
    check("markdown has a title", isinstance(md, str) and
          md.startswith("# AgentVision session report"))
    # windowed variant stays well-formed
    sc, d2 = gj("/session_report?from_ms=%d&to_ms=%d" % (int(t0), int(t0 + 5000)))
    check("windowed session_report ok", sc == 200 and
          d2.get("window", {}).get("mode") == "explicit", str(d2.get("window")))

    # ── 2. OCR (graceful optional dependency) ────────────────────────────────
    print("ocr / read_screen:")
    avail, engine, reason = bs._ocr_engine()
    print(f"    (tesseract available in this env: {avail}"
          f"{'  engine=' + engine if avail else '  reason=' + reason})")
    sc, d = gj("/frame/2/ocr")
    check("/frame/2/ocr ok", sc == 200, str(sc))
    check("ocr has available flag", "available" in d, str(list(d.keys())))
    check("ocr frame_seq echoed", d.get("frame_seq") == 2)
    if not avail:
        check("ocr degrades gracefully (available:false)", d.get("available") is False)
        check("ocr gives install_hint", "install_hint" in d and
              "tesseract" in d["install_hint"].lower(), str(d.get("install_hint")))
        check("ocr gives a reason", bool(d.get("reason")))
    else:
        check("ocr returns text field", isinstance(d.get("text"), str))
        check("ocr returns lines list", isinstance(d.get("lines"), list))
        # Must NOT assert a specific engine: OCR is multi-backend and prefers the
        # built-in OS one (Apple Vision / Windows.Media.Ocr) over tesseract, so a
        # hardcoded "tesseract" check here only ever passed by accident of order.
        eng = (d.get("engine") or "").lower()
        check("ocr names a recognised engine",
              any(k in eng for k in ("tesseract", "apple vision", "windows",
                                     "rapidocr")), d.get("engine"))
        check("ocr text bounded", len(d.get("text", "")) <= 8000)
    sc, d = gj("/read_screen")
    check("/read_screen ok (latest frame)", sc == 200 and "available" in d, str(sc))
    check("read_screen targets latest seq", d.get("frame_seq") == 2, str(d.get("frame_seq")))
    r = client.get("/frame/999999/ocr")
    check("ocr missing frame → 404", r.status_code == 404, str(r.status_code))

    # ── 3. /source_at_error ──────────────────────────────────────────────────
    print("source_at_error:")
    sc, d = gj("/source_at_error?fingerprint=fpSRC")
    check("/source_at_error ok", sc == 200, str(sc))
    check("error block echoed", (d.get("error") or {}).get("fingerprint") == "fpSRC",
          str(d.get("error")))
    frames = d.get("frames") or []
    check("returns the stack frame", len(frames) >= 1, str(len(frames)))
    fr0 = frames[0] if frames else {}
    check("frame resolved on disk", fr0.get("found") is True, str(fr0))
    ctx = fr0.get("code_context") or []
    check("code_context non-empty", isinstance(ctx, list) and len(ctx) >= 1,
          str(ctx))
    check("code_context marks the error line (>> …3)",
          any(ln.strip().startswith(">>") and "3 |" in ln for ln in ctx),
          str(ctx))
    check("code_context contains the boom source",
          any("missing_key" in ln for ln in ctx), str(ctx))
    check("frame_seq tied to the error", d.get("frame_seq") == 2, str(d.get("frame_seq")))
    # latest-frame variant (no fingerprint) resolves the same error
    sc, d = gj("/source_at_error")
    check("source_at_error (latest) ok", sc == 200 and
          (d.get("error") or {}).get("fingerprint") == "fpSRC", str(d.get("error")))
    # unknown fingerprint degrades cleanly (200, empty frames)
    sc, d = gj("/source_at_error?fingerprint=nope_zzz")
    check("unknown fingerprint → clean empty", sc == 200 and d.get("frames") == [],
          str(d))

    # ── 4. /baseline ─────────────────────────────────────────────────────────
    print("baseline:")
    base_ms = t0 - 5000.0
    r = client.post("/baseline", json={"ts_ms": base_ms})
    d = r.get_json()
    check("/baseline stamps ok", d.get("ok") is True and
          abs((d.get("baseline_ms") or 0) - base_ms) < 1.0, str(d))
    check("baseline profile echoed", d.get("profile") == "mtb", str(d.get("profile")))
    sc, d = gj("/baseline")
    check("baseline GET reads it back",
          abs((d.get("baseline_ms") or 0) - base_ms) < 1.0, str(d))
    check("baseline persisted to disk", bs._BASELINES_PATH.exists())

    # ── 5. /watch + /watches ─────────────────────────────────────────────────
    print("watch / watches:")
    r = client.post("/watch", json={"name": "keyerr", "kind": "log",
                                     "regex": "KeyError"})
    d = r.get_json()
    check("/watch registers ok", d.get("ok") is True and
          (d.get("watch") or {}).get("kind") == "log", str(d))
    # a second watch by fingerprint over the seeded failure records
    import modules.diagnostics as dx
    fp = dx.fingerprint("KeyError: cfg")
    client.post("/watch", json={"name": "fp_watch", "kind": "error_fingerprint",
                                "fingerprint": fp})
    r = client.post("/watch", json={"kind": "log"})  # missing name
    check("watch without name → 400", r.status_code == 400, str(r.status_code))

    sc, d = gj("/watches?since_baseline=1")
    check("/watches ok", sc == 200)
    check("both watches listed", d.get("watch_count") == 2, str(d.get("watch_count")))
    byname = {w["name"]: w for w in d.get("watches", [])}
    check("log watch caught the seeded KeyError events",
          byname.get("keyerr", {}).get("hit_count", 0) >= 1,
          str(byname.get("keyerr")))
    check("hits are bounded lists",
          all(len(w.get("hits", [])) <= 25 for w in d.get("watches", [])))
    check("fingerprint watch caught failure records",
          byname.get("fp_watch", {}).get("hit_count", 0) >= 1,
          str(byname.get("fp_watch")))

    # clear=1 returns them one last time (with hits), then empties the set
    sc, d = gj("/watches?since_baseline=1&clear=1")
    check("clear returns watches one last time", d.get("watch_count") == 2 and
          d.get("cleared") is True, str({"n": d.get("watch_count"),
                                         "cleared": d.get("cleared")}))
    sc, d = gj("/watches")
    check("watches empty after clear", d.get("watch_count") == 0, str(d.get("watch_count")))

    # ── 6. MCP tool wiring: 61 → 68, every new tool proxies its route ────────
    print("MCP tool surface:")
    src = (_HERE.parent / "claude_mcp.py").read_text()
    tool_count = src.count("@mcp.tool()")
    # A FLOOR, not an equality. This was `== 68`, which fails the moment a tool is
    # legitimately added (it did, on av_observer_log) while proving nothing the
    # checks below do not: that the tools this suite is about exist and proxy
    # their routes. test_tool_meta.py is what enforces the real invariant — every
    # registered tool is grouped, documented, and not a phantom.
    check("tool count has not regressed below 68", tool_count >= 68,
          str(tool_count))
    new_tools = ["av_session_report", "av_ocr_frame", "av_read_screen",
                 "av_source_at_error", "av_baseline", "av_watch", "av_watches"]
    for t in new_tools:
        check(f"tool {t} defined", f"def {t}(" in src)
    # Plain substring: av_ocr_frame builds its path with an f-string
    # (f"/frame/{int(seq)}/ocr"), so the quoted form never appears literally.
    for route in ("/session_report", "/ocr", "/read_screen", "/source_at_error",
                  "/baseline", "/watch", "/watches"):
        check(f"route {route} referenced by an MCP tool", route in src)

    # cleanup profile
    p = load_profiles()
    p.pop("mtb", None)
    save_profiles({k: v for k, v in p.items() if k not in BUILTIN_PROFILES})

    print("=" * 66)
    if _fails:
        print(f"RESULT: {_fails} FAILURE(S)")
        return 1
    print("RESULT: all MCP-tools-B tests passed")
    return 0


def _iso(ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc) \
        .isoformat(timespec="milliseconds").replace("+00:00", "Z")


if __name__ == "__main__":
    print("=" * 66)
    print("MCP wrap-up / OCR / error→source / watches tools test suite")
    print("=" * 66)
    raise SystemExit(main())
