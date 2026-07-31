#!/usr/bin/env python3
"""AgentVision must not write into the log it is reading.

The evidence this suite exists for, measured on a real project's log
(~/Developer/<project>/log/actions.jsonl):

    total records                    2123
    written by agentvision.watchdog  2024   (95%)
    written by the program             99
    newest program record         48.8 h old
    newest watchdog record         0.0 h old  (still being appended)
    watchdog records picked up by _detect_failure_records: 0

The write was justified in-code as "so existing bookmark detection picks it up".
It never did — the last line above is the measurement. So the append was pure
contamination, and it also broke the watchdog's own arithmetic: it compared
against the newest record of ANY source, including the one it had just written,
so it reported `silent_s: 60.3` about a program that had been silent for 48.8 h.

Run:  PYTHONPATH=. .venv/bin/python python_backend/api/test_observer_isolation.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                    # python_backend/api
sys.path.insert(0, str(HERE.parent))             # python_backend
sys.path.insert(0, str(HERE.parent.parent))      # repo root (for `shared`)

_fails = []


def check(label, cond, detail=""):
    print(f"  [{'ok ' if cond else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))
    if not cond:
        _fails.append(label)


def main():
    import bridge_server as bs
    from connectors.program_connector import ProgramProfile

    tmp = Path(tempfile.mkdtemp(prefix="av_observer_"))
    now_ms = time.time() * 1000.0

    # A program log shaped like the real one: one ancient program record, then
    # nothing but AgentVision's own watchdog output.
    prog_log = tmp / "actions.jsonl"
    old_ms = now_ms - 48 * 3600 * 1000.0
    lines = [json.dumps({"ts_ms": old_ms, "category": "log", "source": "theapp",
                         "data": {"name": "boot"}})]
    for i in range(20):
        lines.append(json.dumps({
            "ts_ms": now_ms - i * 60_000.0, "category": "event",
            "source": "agentvision.watchdog",
            "data": {"name": "program.stuck", "silent_s": 60.0}}))
    prog_log.write_text("\n".join(lines) + "\n")

    prof = ProgramProfile(
        name="observer_fixture", display_name="Observer Fixture",
        process_name="definitely_not_running_xyz",
        project_root=str(tmp), action_log_file=str(prog_log),
        log_sources=[{"label": "events", "adapter": "jsonl", "path": str(prog_log)}],
    )

    # Point the module at the fixture: active profile + an isolated observer dir.
    bs.BUILTIN_PROFILES["observer_fixture"] = prof
    bs._active_profile_name = "observer_fixture"
    obs_dir = tmp / "av_observer"
    bs._OBSERVER_DIR = obs_dir

    # ── 1. silence is measured against the PROGRAM, not against ourselves ────
    print("silence arithmetic:")
    newest = bs._newest_program_record_ms(limit=10_000)
    age_h = (now_ms - newest) / 3.6e6 if newest else None
    check("the newest PROGRAM record is found, not the newest watchdog record",
          newest is not None and abs(newest - old_ms) < 1000,
          f"age={age_h:.1f}h (watchdog records are 1m old)")

    # ── 1b. a heavily contaminated legacy log still yields the program's ts ──
    # _read_action_jsonl truncates to the newest `limit` records BEFORE the
    # agentvision.* filter, so on a file the old watchdog already filled (2072 of
    # 2171 records on the real one) a small window leaves nothing but observer
    # records and reads as "the program never wrote anything".
    fat = tmp / "fat.jsonl"
    fat_lines = [json.dumps({"ts_ms": old_ms, "category": "log", "source": "theapp",
                             "data": {"name": "boot"}})]
    for i in range(2000):
        fat_lines.append(json.dumps({
            "ts_ms": now_ms - i * 1000.0, "category": "event",
            "source": "agentvision.watchdog",
            "data": {"name": "program.stuck", "silent_s": 60.0}}))
    fat.write_text("\n".join(fat_lines) + "\n")
    prof.action_log_file = str(fat)
    bs.BUILTIN_PROFILES["observer_fixture"] = prof
    newest_fat = bs._newest_program_record_ms()
    check("2000 observer records do not hide the one program record behind them",
          newest_fat is not None and abs(newest_fat - old_ms) < 1000,
          f"got {newest_fat}")
    check("a small window is what used to lose it (kept as the contrast)",
          bs._newest_program_record_ms(limit=200) is None,
          str(bs._newest_program_record_ms(limit=200)))
    prof.action_log_file = str(prog_log)
    bs.BUILTIN_PROFILES["observer_fixture"] = prof

    # ── 2. a tick leaves the program's log byte-identical ────────────────────
    print("write isolation:")
    before = prog_log.read_bytes()
    la, wr = bs._watchdog_tick(0.0, None, running=True)     # alive + long silent
    after = prog_log.read_bytes()
    check("a stuck-detecting tick does not touch the program's log",
          before == after,
          f"{len(before)}B -> {len(after)}B")

    obs_path = obs_dir / "observer_fixture.observer.jsonl"
    check("the observation lands in AgentVision's own sink instead",
          obs_path.exists(), str(obs_path))
    recs = bs._read_observer_jsonl("observer_fixture", limit=100)
    stuck = [r for r in recs if (r.get("data") or {}).get("name") == "program.stuck"]
    check("exactly one program.stuck was recorded", len(stuck) == 1,
          f"{len(stuck)} record(s)")
    if stuck:
        rec_silent = (stuck[0].get("data") or {}).get("silent_s") or 0
        check("silent_s reflects the real 48 h silence, not a self-heartbeat",
              rec_silent > 40 * 3600,
              f"silent_s={rec_silent} ({rec_silent / 3600:.1f}h)")

    check("the observer sink is not inside the program's log directory",
          Path(obs_path).parent.resolve() != Path(prog_log).parent.resolve(),
          f"{obs_path.parent} vs {prog_log.parent}")

    # ── 3. a process that has EXITED is not reported as stuck ────────────────
    print("exited is not stuck:")
    n_before = len(bs._read_observer_jsonl("observer_fixture", limit=10_000))
    la2, wr2 = bs._watchdog_tick(0.0, None, running=False)
    recs2 = bs._read_observer_jsonl("observer_fixture", limit=10_000)
    new_stuck = [r for r in recs2[n_before:]
                 if (r.get("data") or {}).get("name") == "program.stuck"]
    check("no program.stuck is written when the process is not running",
          not new_stuck, f"{len(new_stuck)} written")
    st = bs._observer_state.get("observer_fixture") or {}
    check("live state says running=False, stuck=False",
          st.get("running") is False and st.get("stuck") is False,
          json.dumps({k: st.get(k) for k in ("running", "stuck", "program_silent_s")}))

    # ── 4. repeat ticks are rate-limited, they do not accrete a record a tick ─
    print("rate limiting:")
    n0 = len(bs._read_observer_jsonl("observer_fixture", limit=10_000))
    la3 = 0.0
    wr3 = True
    for _ in range(5):
        la3, wr3 = bs._watchdog_tick(la3, wr3, running=True)
    n1 = len(bs._read_observer_jsonl("observer_fixture", limit=10_000))
    check("five consecutive stuck ticks record ONE observation, not five",
          n1 - n0 == 1, f"+{n1 - n0} records")

    # ── 5. an exit transition is recorded once ───────────────────────────────
    print("transitions:")
    n2 = len(bs._read_observer_jsonl("observer_fixture", limit=10_000))
    la4, wr4 = bs._watchdog_tick(la3, True, running=False)
    recs3 = bs._read_observer_jsonl("observer_fixture", limit=10_000)
    exited = [r for r in recs3[n2:]
              if (r.get("data") or {}).get("name") == "program.exited"]
    check("running -> not running records program.exited once",
          len(exited) == 1, f"{len(exited)} record(s)")
    n3 = len(recs3)
    bs._watchdog_tick(la4, False, running=False)
    check("staying exited records nothing further",
          len(bs._read_observer_jsonl("observer_fixture", limit=10_000)) == n3)

    # ── 6. the route exposes them ────────────────────────────────────────────
    print("route:")
    bs.app.config["TESTING"] = True
    c = bs.app.test_client()
    r = c.get("/observer/log?profile=observer_fixture&limit=50")
    check("GET /observer/log -> 200", r.status_code == 200, str(r.status_code))
    body = r.get_json() or {}
    check("it reports its own path and record count",
          body.get("exists") is True and body.get("count", 0) >= 1,
          f"count={body.get('count')} path={body.get('path')}")
    check("it carries the live watchdog state",
          isinstance(body.get("live_state"), dict),
          str(type(body.get("live_state"))))

    print()
    if _fails:
        print(f"{len(_fails)} failed: " + "; ".join(_fails))
        return 1
    print("0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
