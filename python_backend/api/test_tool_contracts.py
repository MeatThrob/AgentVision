#!/usr/bin/env python3
"""Class-A defects from docs/MCP_TOOL_AUDIT.md — "the tool misleads its caller".

Each check here corresponds to one recorded defect where the tool's own
docstring, or its return value, asserted something the handler did not do. The
audit is the work queue; this file is the proof that a queue item is closed.

Run:  .venv/bin/python python_backend/api/test_tool_contracts.py
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))                 # python_backend/api
sys.path.insert(0, str(_HERE.parent.parent))          # python_backend
sys.path.insert(0, str(_HERE.parent.parent.parent))   # repo root

_fails = []


def check(name, cond, detail=""):
    print(f"  [{'ok ' if cond else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        _fails.append(name)


def main():
    work = Path(tempfile.mkdtemp(prefix="av_contracts_"))
    actions = work / "actions.jsonl"
    now_ms = time.time() * 1000.0
    # Two distinct failure fingerprints; one of them repeats 40 times, which is
    # what used to produce 40 identical entries in /anomalies/new.
    lines = [json.dumps({"ts_ms": now_ms + 1000, "ts": "2026-07-30T00:00:01.000Z",
                         "category": "error", "level": "ERROR", "source": "app.db",
                         "data": {"message": "KeyError: cfg"}})]
    for i in range(40):
        lines.append(json.dumps({
            "ts_ms": now_ms + 2000 + i, "ts": "2026-07-30T00:00:02.000Z",
            "category": "error", "level": "ERROR", "source": "app.net",
            "data": {"message": "connection reset"}}))
    actions.write_text("\n".join(lines) + "\n")

    from connectors.program_connector import (ProgramProfile, save_profiles,
                                              load_profiles, BUILTIN_PROFILES)
    p = load_profiles()
    p["contracts_fixture"] = ProgramProfile(
        name="contracts_fixture", display_name="ContractsFixture",
        action_log_file=str(actions), project_root=str(work),
        process_name="nonexistent_contract_xyz",
        log_sources=[{"path": str(actions), "adapter": "jsonl", "label": "events"}])
    save_profiles(p)

    os.environ["AGENTVISION_ACTIVE_PROFILE"] = "contracts_fixture"
    import bridge_server as bs
    bs._active_profile_name = "contracts_fixture"
    bs._collector = None
    client = bs.app.test_client()

    # ── av_delete_profile: docstring said it cannot remove the active one ────
    print("av_delete_profile — active-profile guard:")
    r = client.delete("/profiles/contracts_fixture")
    check("deleting the ACTIVE profile is refused", r.status_code == 409,
          f"HTTP {r.status_code}")
    body = r.get_json() or {}
    check("the refusal says deleted:false and names the fix",
          body.get("deleted") is False and "av_set_active_profile" in (body.get("fix") or ""),
          json.dumps(body)[:120])
    check("the profile still exists after the refusal",
          "contracts_fixture" in load_profiles())

    r = client.delete("/profiles/definitely_not_a_profile_xyz")
    check("deleting a profile that does not exist is a 404, not a silent ok",
          r.status_code == 404, f"HTTP {r.status_code}")

    for b in list(BUILTIN_PROFILES)[:1]:
        r = client.delete(f"/profiles/{b}")
        check(f"deleting the built-in '{b}' is refused", r.status_code == 400,
              f"HTTP {r.status_code}")

    # ── av_new_errors_this_session: history was never persisted, no dedupe ───
    print("av_new_errors_this_session — dedupe + persistence:")
    hist = work / "fp_history.json"
    bs._FP_HISTORY_PATH = hist
    bs._session_start_ms = now_ms
    bs._session_seen_fps.clear()

    r1 = client.get("/anomalies/new")
    b1 = r1.get_json() or {}
    fps1 = b1.get("new_fingerprints") or []
    check("41 failure records collapse to 2 fingerprints, not 41 entries",
          b1.get("count") == 2 and b1.get("records_scanned_in_session") == 41,
          f"count={b1.get('count')} scanned={b1.get('records_scanned_in_session')}")
    check("the repeated failure carries its count instead of repeating",
          any(e.get("count") == 40 for e in fps1),
          json.dumps([{k: e[k] for k in ('count', 'source')} for e in fps1]))
    check("the fingerprint history file is actually written now",
          b1.get("history_persisted") is True and hist.exists(),
          f"persisted={b1.get('history_persisted')} exists={hist.exists()}")

    r2 = client.get("/anomalies/new")
    b2 = r2.get_json() or {}
    check("a second call in the same session gives the SAME answer (idempotent)",
          b2.get("count") == b1.get("count")
          and [e["fp"] for e in (b2.get("new_fingerprints") or [])]
              == [e["fp"] for e in fps1],
          f"{b1.get('count')} then {b2.get('count')}")

    # Simulate the NEXT bridge session: history is loaded at boot.
    bs._session_seen_fps.clear()
    bs._session_seen_fps.update(bs._load_fp_history())
    check("the persisted history is non-empty on reload",
          len(bs._session_seen_fps) == 2, f"{len(bs._session_seen_fps)} known")
    r3 = client.get("/anomalies/new")
    b3 = r3.get_json() or {}
    check("a LATER session no longer calls those fingerprints new",
          b3.get("count") == 0, f"count={b3.get('count')}")

    # ── av_selftest: the input_daemon check used to hardcode ok:True ─────────
    print("av_selftest — the daemon check can actually fail:")
    r = client.get("/selftest")
    st = r.get_json() or {}
    check("/selftest -> 200", r.status_code == 200, f"HTTP {r.status_code}")
    daemon = next((c for c in (st.get("checks") or [])
                   if c.get("check") == "input_daemon"), {})
    check("the input_daemon check reports a MEASURED running flag",
          isinstance(daemon.get("running"), bool),
          json.dumps({k: daemon.get(k) for k in ("ok", "running",
                                                 "required_by_profile")}))
    check("ok is derived from running + required_by_profile, not constant True",
          daemon.get("ok") == (bool(daemon.get("running"))
                               or not bool(daemon.get("required_by_profile"))),
          f"ok={daemon.get('ok')} running={daemon.get('running')} "
          f"required={daemon.get('required_by_profile')}")
    check("failed_checks and overall ok agree with each other",
          st.get("ok") is (len(st.get("failed_checks") or []) == 0),
          f"ok={st.get('ok')} failed={st.get('failed_checks')}")

    # ── av_ui_tree: max_nodes mutated a module global and never reset ───────
    print("av_ui_tree — max_nodes does not leak into later calls:")
    try:
        from utils import ui_tree as _ut
        default_before = _ut.MAX_NODES
        client.get("/ui/tree?max_nodes=7")
        check("a custom max_nodes does not change the module default",
              _ut.MAX_NODES == default_before,
              f"{default_before} -> {_ut.MAX_NODES}")
    except Exception as e:                              # pragma: no cover
        check("ui_tree importable", False, str(e))

    # ── av_wait_for: `'log' if (...) else 'log'` — both arms identical ──────
    print("av_wait_for — a default is reported as a default:")
    src = Path(bs.__file__).read_text()
    check("the identical-arm ternary is gone from the source",
          'condition = "log" if (regex' not in src
          and "condition = 'log' if (regex" not in src)
    r = client.get("/wait_for?timeout=0.5&poll_interval=0.2&regex=zzz_no_match")
    b = r.get_json() or {}
    check("an omitted condition is reported as defaulted, not deduced",
          b.get("condition") == "log" and b.get("condition_inferred") is True
          and "defaulted" in (b.get("condition_basis") or ""),
          json.dumps({k: b.get(k) for k in ("condition", "condition_inferred")}))
    r = client.get("/wait_for?condition=log&timeout=0.5&poll_interval=0.2&regex=zzz")
    b = r.get_json() or {}
    check("an explicit condition is reported as caller-supplied",
          b.get("condition_inferred") is False,
          str(b.get("condition_basis")))

    # ── av_timeline: untimestamped rows were coerced to 0.0 and dropped first ─
    print("av_timeline — untimestamped rows are not silently dropped:")
    untimed = work / "untimed.txt"
    untimed.write_text("".join(f"[GPU] present src=0x{i:04x} ok=False\n"
                               for i in range(30)))
    p3 = load_profiles()
    p3["contracts_fixture"].log_sources = [
        {"path": str(actions), "adapter": "jsonl", "label": "events"},
        {"path": str(untimed), "adapter": "auto", "label": "textlog"}]
    save_profiles(p3)
    bs._collector = None
    r = client.get("/timeline?limit=10")
    tl = r.get_json() or {}
    rows = tl.get("rows") or []
    check("/timeline -> 200", r.status_code == 200, f"HTTP {r.status_code}")
    check("untimestamped rows keep ts_ms=null instead of a fake 0.0",
          all(rw.get("ts_ms") != 0.0 for rw in rows),
          str([rw.get("ts_ms") for rw in rows][:4]))
    check("they survive the row cap (they sort last, like read_normalized)",
          tl.get("untimestamped_rows", 0) > 0,
          f"kept={tl.get('untimestamped_rows')} of "
          f"{tl.get('untimestamped_available')}")
    check("and they do NOT crowd out the timestamped rows either",
          tl.get("timestamped_rows", 0) > 0
          and tl.get("untimestamped_rows", 0) < len(rows),
          f"timed={tl.get('timestamped_rows')} "
          f"untimed={tl.get('untimestamped_rows')} of {len(rows)} rows")
    check("truncation is declared rather than implied",
          "truncated" in tl, json.dumps({k: tl.get(k) for k in
                                         ("count", "total_available", "truncated")}))

    # ── av_errors_by_fingerprint: substring 'fail'/'error' in source ────────
    print("av_errors_by_fingerprint — no false positives from module names:")
    fp_log = work / "fpnames.jsonl"
    fp_log.write_text("\n".join([
        json.dumps({"ts_ms": now_ms, "category": "log", "level": "INFO",
                    "source": "FailoverManager", "data": {"message": "promoted"}}),
        json.dumps({"ts_ms": now_ms, "category": "log", "level": "INFO",
                    "source": "CrashReporterService", "data": {"message": "uploaded"}}),
        json.dumps({"ts_ms": now_ms, "category": "log", "level": "INFO",
                    "source": "app.error_pages", "data": {"message": "rendered 404"}}),
        json.dumps({"ts_ms": now_ms, "category": "error", "level": "ERROR",
                    "source": "app.db", "data": {"message": "real failure"}}),
    ]) + "\n")
    p2 = load_profiles()
    p2["contracts_fixture"].action_log_file = str(fp_log)
    save_profiles(p2)
    bs._collector = None
    trig = bs._detect_failure_records(limit=100)
    srcs = sorted(str(t.get("source")) for t in trig)
    check("an INFO line from FailoverManager is not counted as a failure",
          "FailoverManager" not in srcs, str(srcs))
    check("an INFO line from CrashReporterService is not counted as a failure",
          "CrashReporterService" not in srcs, str(srcs))
    check("the genuine ERROR record is still detected", "app.db" in srcs, str(srcs))

    # restore fixture log for anything after this
    p2["contracts_fixture"].action_log_file = str(actions)
    save_profiles(p2)

    # ── av_overview: /latest was fetched just to read .sequence ──────────────
    print("av_overview — a pointer lookup is not a frame read:")
    bs._agent_reads["full_frame"] = 0
    r = client.get("/latest/pointer")
    check("/latest/pointer answers without a frame body",
          r.status_code in (200, 404) and "sequence" in (r.get_json() or {}),
          f"HTTP {r.status_code}")
    check("it does NOT increment the full-frame read counter "
          "(which av_token_report uses to prove its savings)",
          bs._agent_reads["full_frame"] == 0,
          f"full_frame={bs._agent_reads['full_frame']}")
    import claude_mcp as mcpmod
    src = Path(mcpmod.__file__).read_text()
    ov = src.split("def av_overview")[1].split("def av_diagnose")[0]
    check("av_overview uses the pointer route, not /latest",
          '_http_get("/latest/pointer")' in ov and '_http_get("/latest")' not in ov)

    # ── av_log_push: claimed actions.jsonl, wrote a plain line elsewhere ─────
    print("av_log_push — the note is structured, and not in the program's log:")
    obs_dir = work / "obs"
    bs._OBSERVER_DIR = obs_dir
    before = actions.read_bytes()
    r = client.post("/log", json={"message": "reproduced at frame 42",
                                  "category": "note", "source": "claude.note",
                                  "data": {"frame": 42}})
    pb = r.get_json() or {}
    check("POST /log reports where it actually wrote",
          r.status_code == 200 and pb.get("recorded") is True
          and "observer" in json.dumps(pb.get("written_to")),
          json.dumps(pb.get("written_to")))
    check("the program's own action log is untouched",
          actions.read_bytes() == before,
          f"{len(before)}B -> {len(actions.read_bytes())}B")
    r = client.get("/observer/log?limit=10")
    recs = (r.get_json() or {}).get("records") or []
    mine = [x for x in recs if (x.get("data") or {}).get("frame") == 42]
    check("category/source/data survive instead of being discarded",
          len(mine) == 1 and mine[0].get("category") == "note"
          and mine[0].get("source", "").startswith("agentvision"),
          json.dumps(mine[:1]))

    # ── av_program_stats: a 9-key game-bot whitelist, and a dead `lines` ─────
    print("av_program_stats — every metric, and `lines` does something:")
    stats_dir = work / "stats"
    stats_dir.mkdir(exist_ok=True)
    (stats_dir / "stats_1.log").write_text(
        "╭──── stats ────╮\n"
        "│ Session length: 4h 12m\n"
        "│ Games: 37\n"
        "│ frames_rendered: 918233\n"
        "│ gpu_present_failures: 180\n"
        "│ peak_rss_mb: 4210\n"
        "╰───────────────╯\n")
    p4 = load_profiles()
    p4["contracts_fixture"].stats_folder = str(stats_dir)
    save_profiles(p4)
    bs._collector = None
    r = client.get("/program/stats")
    st2 = (r.get_json() or {}).get("stats") or {}
    check("a non-game program's own metrics are no longer dropped",
          all(k in st2 for k in ("frames_rendered", "gpu_present_failures",
                                 "peak_rss_mb")),
          str(sorted(st2)))
    check("the legacy bot keys still resolve",
          st2.get("session_length") == "4h 12m" and st2.get("games") == "37",
          json.dumps({k: st2.get(k) for k in ("session_length", "games")}))
    r = client.get("/program/stats?lines=2")
    st3 = (r.get_json() or {}).get("stats") or {}
    check("`lines` actually limits the read now",
          len(st3) < len(st2), f"{len(st2)} keys -> {len(st3)} keys with lines=2")

    # ── av_install_project: the wrapper never sent force ────────────────────
    print("av_install_project — the gate is reachable from the tool:")
    import inspect
    sig = inspect.signature(mcpmod.av_install_project.fn
                            if hasattr(mcpmod.av_install_project, "fn")
                            else mcpmod.av_install_project)
    check("force is exposed as a parameter", "force" in sig.parameters,
          str(list(sig.parameters)))
    ip = src.split("def av_install_project")[1].split("@mcp.tool")[0]
    check("and it is forwarded to the route", '"force"' in ip)

    # ── emitter selection: answered per id, not with one blanket flag ─────────
    print("emitter selection is answered id by id:")
    from emitters import selection_report
    py = selection_report("python", ["stdout_tee", "logging_bridge"])
    check("in-process hook ids on python report enforced=True",
          all(r["enforced"] for r in py) and len(py) == 2,
          json.dumps(py))
    jv = selection_report("java", ["config_dropin", "logging_bridge"])
    check("java ids say ONE artifact, not silently 'not enforced'",
          all(r["enforced"] is False for r in jv)
          and all("ONE artifact" in r["how"] for r in jv),
          json.dumps(jv)[:150])
    bogus = selection_report("python", ["no_such_emitter"])
    check("an id that maps to nothing is called out as doing nothing",
          bogus[0]["enforced"] is False
          and "no runtime hook" in bogus[0]["how"],
          json.dumps(bogus))

    # ── cleanup: remove the fixture profile we added ────────────────────────
    # "custom" is the neutral placeholder; this named a real personal profile,
    # which only existed on one machine.
    bs._active_profile_name = "custom"
    final = load_profiles()
    final.pop("contracts_fixture", None)
    save_profiles(final)

    print()
    if _fails:
        print(f"{len(_fails)} failed: " + "; ".join(_fails))
        return 1
    print("0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
