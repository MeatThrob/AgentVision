#!/usr/bin/env python3
"""The structured event log must be READABLE by the tools that analyse it.

THE BUG THIS EXISTS FOR, and it is the worst one found in this project so far.

`av_bridge_commit` wires the emitter sinks it scaffolds as **log_sources**:

    [{"path": ".../agentvision/actions.jsonl", "adapter": "jsonl", "label": "events"},
     {"path": ".../agentvision/log.txt",       "adapter": "auto",  "label": "text"}]

It never sets `action_log_file`. And `_active_action_log_path()` read ONLY
`action_log_file`. So on every program bridged through the modern flow it
returned "", `_read_action_jsonl` returned [], and FOURTEEN functions built on
it saw nothing at all.

Measured end to end on a freshly bridged program that printed
"worker finished: 3 line items, total 34.00 / OK" and exited 0 while silently
dropping 2 of its 5 records. The `swallowed_exceptions` emitter caught both
KeyErrors and wrote them to actions.jsonl with type, file:line, handling
function and occurrence count. What the agent-facing tools then said:

    av_errors_by_fingerprint  ->  {"fingerprints": [], "total_failures": 0}
    av_diagnose               ->  "hypotheses": [], while its OWN
                                  counts.distinct_error_fingerprints said 2
    av_list_bookmarks         ->  []
    health                    ->  60, factors: ["program not running"] only

Eleven MCP tools answering emptily about a log full of exactly the evidence
they exist to surface. The flight recorder had the truth the whole time; the
analysis half was pointed at a field nobody sets. An empty list reads as "this
program is fine" — the confident silence this project exists to remove — and
here AgentVision was re-hiding the very failure mode its flagship emitter was
built to reveal.

Two separate promises are checked below, because they failed together and could
regress independently:

  1. RESOLUTION — the JSONL log a bridged profile actually declares is found.
  2. DISCLOSURE — a swallowed exception is reported as HANDLED. Not as a
     failure (the program caught it; a retry loop is healthy), and never as
     nothing.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE.parent.parent))
sys.path.insert(0, str(_HERE.parent.parent.parent))

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f" — {detail}" if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


#: Records in the exact shape agent_bootstrap/av_runtime.py emits. Written by
#: hand rather than captured, so a change to the emitter that breaks this
#: contract fails here instead of silently going unnoticed.
def _emitter_records(now_ms: float) -> list[dict]:
    run = "av-test-1"
    return [
        {"ts": "2026-08-04T19:18:43.078Z", "ts_ms": now_ms - 400,
         "category": "process", "source": "av.bootstrap.start",
         "run_id": run, "data": {"pid": 4242, "argv": ["worker.py"]}},
        {"ts": "2026-08-04T19:18:43.080Z", "ts_ms": now_ms - 300,
         "category": "stdout", "source": "av.bootstrap.stdout",
         "run_id": run, "data": {"text": "worker starting"}},
        {"ts": "2026-08-04T19:18:43.080Z", "ts_ms": now_ms - 250,
         "category": "warn", "source": "av.bootstrap.swallowed",
         "run_id": run, "data": {
             "message": "KeyError raised and silently handled: 'SKU-9'",
             "type": "KeyError", "raised_at": "worker.py:13",
             "handled_in": "worker.py:price", "occurrences": 1}},
        {"ts": "2026-08-04T19:18:43.291Z", "ts_ms": now_ms - 200,
         "category": "stdout", "source": "av.bootstrap.stdout",
         "run_id": run,
         "data": {"text": "worker finished: 3 line items, total 34.00"}},
        {"ts": "2026-08-04T19:18:43.291Z", "ts_ms": now_ms - 150,
         "category": "stdout", "source": "av.bootstrap.stdout",
         "run_id": run, "data": {"text": "OK"}},
        {"ts": "2026-08-04T19:18:43.292Z", "ts_ms": now_ms - 100,
         "category": "warn", "source": "av.bootstrap.swallowed_summary",
         "run_id": run, "data": {
             "message": "2 exception(s) were raised and silently handled "
                        "across 1 distinct site(s)",
             "total_occurrences": 2, "distinct_sites": 1,
             "sites": [{"type": "KeyError", "raised_at": "worker.py:13",
                        "handled_in": "worker.py:price", "occurrences": 2}]}},
        {"ts": "2026-08-04T19:18:43.292Z", "ts_ms": now_ms - 50,
         "category": "process", "source": "av.bootstrap.exit",
         "run_id": run, "data": {"pid": 4242}},
    ]


def main() -> int:
    print("action-log wiring — the analysis half must SEE the event log")
    from connectors.program_connector import (ProgramProfile, save_profiles,
                                              load_profiles)
    import bridge_server as bs

    tmp = Path(tempfile.mkdtemp(prefix="av_alw_"))
    root = tmp / "target"
    (root / "agentvision").mkdir(parents=True)
    jsonl = root / "agentvision" / "actions.jsonl"
    textlog = root / "agentvision" / "log.txt"
    now = time.time() * 1000.0
    jsonl.write_text("\n".join(json.dumps(r) for r in _emitter_records(now)) + "\n")
    textlog.write_text("2026-08-04T19:18:43.080Z [WARN] [av.bootstrap.swallowed] "
                       "KeyError raised and silently handled: 'SKU-9'\n")

    keep = load_profiles()
    _active_file = Path(bs.__file__).resolve().parent.parent / "active_profile.txt"
    _prev_active = (_active_file.read_text().strip()
                    if _active_file.exists() else "")
    _prev_name = bs._active_profile_name

    #: EXACTLY what av_bridge_commit persists — log_sources set, action_log_file
    #: absent. This fixture shape IS the bug; if it drifts from what commit
    #: writes, this suite stops protecting anything.
    MODERN = dict(
        name="alwtest", display_name="ALW Target", project_root=str(root),
        process_name="nonexistent_alw_xyz",
        log_sources=[{"path": str(jsonl), "adapter": "jsonl", "label": "events"},
                     {"path": str(textlog), "adapter": "auto", "label": "text"}])

    try:
        profs = dict(keep)
        profs["alwtest"] = ProgramProfile(**MODERN)
        save_profiles(profs)
        bs._active_profile_name = "alwtest"
        bs._collector = None

        # ── 1. RESOLUTION ───────────────────────────────────────────────────
        got = bs._active_action_log_path()
        check("a bridged profile's JSONL log is found with NO action_log_file",
              got == str(jsonl), f"got {got!r}, wanted {str(jsonl)!r}")

        recs = bs._read_action_jsonl(limit=500)
        check("...so the structured reader actually returns records",
              len(recs) == 7, f"{len(recs)} records")

        # An explicit action_log_file must still win — older profiles, and the
        # per-frame pin path, both depend on it.
        other = root / "agentvision" / "explicit.jsonl"
        other.write_text(json.dumps(_emitter_records(now)[0]) + "\n")
        profs["alwtest"] = ProgramProfile(**dict(MODERN,
                                                action_log_file=str(other)))
        save_profiles(profs)
        check("an explicit action_log_file still wins over log_sources",
              bs._active_action_log_path() == str(other),
              bs._active_action_log_path())

        # A TEXT log must never be substituted: handing it to a JSONL reader
        # parses zero records, which is the same silence in a different hat.
        profs["alwtest"] = ProgramProfile(**dict(
            MODERN, log_sources=[{"path": str(textlog), "adapter": "auto",
                                  "label": "text"}]))
        save_profiles(profs)
        check("a text-only profile resolves to NOTHING rather than a text log",
              bs._active_action_log_path() == "",
              bs._active_action_log_path())

        # With several JSONL sources, the emitter's own sink ('events') wins —
        # it is the one carrying av.bootstrap.* records.
        second = root / "agentvision" / "vendor.jsonl"
        second.write_text("{}\n")
        profs["alwtest"] = ProgramProfile(**dict(
            MODERN, log_sources=[
                {"path": str(second), "adapter": "jsonl", "label": "vendor"},
                {"path": str(jsonl), "adapter": "jsonl", "label": "events"}]))
        save_profiles(profs)
        check("the 'events' sink is preferred over another JSONL source",
              bs._active_action_log_path() == str(jsonl),
              bs._active_action_log_path())

        # A .jsonl path with no declared adapter still counts.
        profs["alwtest"] = ProgramProfile(**dict(
            MODERN, log_sources=[{"path": str(jsonl), "label": "whatever"}]))
        save_profiles(profs)
        check("a .jsonl path is recognised even without adapter='jsonl'",
              bs._active_action_log_path() == str(jsonl),
              bs._active_action_log_path())

        # ── back to the canonical modern shape for the disclosure checks ────
        profs["alwtest"] = ProgramProfile(**MODERN)
        save_profiles(profs)
        bs._collector = None

        # ── 2. DISCLOSURE ───────────────────────────────────────────────────
        he = bs._handled_exceptions()
        check("swallowed exceptions are found", he["total_occurrences"] == 2,
              json.dumps(he)[:200])
        check("...counted from the run SUMMARY, not the de-duplicated live rows",
              he["counted_from"] == "swallowed_summary", he["counted_from"])
        check("...and the site names the type, the raise line and the handler",
              he["sites"] and he["sites"][0]["type"] == "KeyError"
              and he["sites"][0]["raised_at"] == "worker.py:13"
              and he["sites"][0]["handled_in"] == "worker.py:price",
              json.dumps(he.get("sites"))[:200])

        # A HANDLED exception is NOT a failure. Folding these into the failure
        # detector would grade a retry loop unhealthy on every run.
        trig = bs._detect_failure_records(limit=500)
        check("a swallowed exception is NOT counted as a failure trigger",
              len(trig) == 0, f"{len(trig)} triggers: "
                              f"{[t.get('source') for t in trig]}")

        client = bs.app.test_client()
        r = client.get("/errors/by-fingerprint")
        b = r.get_json() or {}
        check("/errors/by-fingerprint still reports 0 failures, accurately",
              b.get("total_failures") == 0, str(b.get("total_failures")))
        check("...but does NOT return a bare empty list",
              "handled_exceptions" in b, str(sorted(b)))
        check("...reporting the same 2 occurrences",
              (b.get("handled_exceptions") or {}).get("total_occurrences") == 2,
              json.dumps(b.get("handled_exceptions"))[:200])
        check("...with a read_this that forbids reading [] as 'not failing'",
              "SILENTLY HANDLED" in (b.get("read_this") or ""),
              str(b.get("read_this"))[:160])

        r = client.get("/diagnose")
        d = r.get_json() or {}
        hyps = d.get("hypotheses") or []
        mine = [h for h in hyps
                if "SILENTLY HANDLED" in str(h.get("summary") or "")]
        check("av_diagnose produces a hypothesis for the swallowed exceptions",
              len(mine) == 1, f"{len(hyps)} hypotheses, {len(mine)} matching")
        if mine:
            h = mine[0]
            check("...naming the exception type and the raise site",
                  "KeyError" in h["summary"] and "worker.py:13" in h["summary"],
                  h["summary"][:170])
            # Hedged on purpose: the count and location are certain, the FAULT
            # is not. A retry loop produces this same record and is healthy.
            check("...hedged below 0.8, because catching may be deliberate",
                  0.3 <= float(h.get("confidence") or 0) < 0.8,
                  str(h.get("confidence")))
            check("...and stating outright that it is not counted as a failure",
                  "NOT counted" in str(h.get("not_a_failure") or ""),
                  str(h.get("not_a_failure")))
            check("...and pointing at the source file to read next",
                  any("av_source_file" in str(n)
                      for n in (h.get("recommended_next") or [])),
                  str(h.get("recommended_next")))
        check("the swallowed exception reaches top_signals",
              any("silently handled" in str(s).lower()
                  for s in (d.get("top_signals") or [])),
              str(d.get("top_signals"))[:200])

        # ── 3. THE CANARY ───────────────────────────────────────────────────
        # The original bug was not one broken function, it was every function
        # built on one path resolver going quiet at once. Assert breadth, so a
        # future regression in the resolver cannot pass by fixing one caller.
        # Response keys are read off the LIVE routes, never guessed: asserting
        # on an invented key is how a test passes while the feature is dead, and
        # the first draft of this very canary did it (looked for `fields` and
        # `records`; the routes return `categories` and `actions`).
        dark: list[str] = []
        if not bs._read_action_jsonl(limit=10):
            dark.append("_read_action_jsonl")
        if not bs._newest_program_record_ms():
            dark.append("_newest_program_record_ms")
        sch = client.get("/events/schema").get_json() or {}
        if not (sch.get("categories") or sch.get("observed")):
            dark.append("/events/schema")
        rng = client.get(f"/log/range?from_ms={now - 100000}"
                         f"&to_ms={now + 1000}").get_json() or {}
        if not rng.get("actions"):
            dark.append("/log/range")
        bm = client.get("/bookmarks").get_json()
        if bm is None:
            dark.append("/bookmarks")
        check("no structured-log consumer is left reading nothing", not dark,
              f"dark: {dark}")
        # /log/range is the frame<->log correlation the whole product rests on:
        # prove it returns THIS program's records, not merely a non-empty list.
        srcs = {a.get("source") for a in (rng.get("actions") or [])}
        check("...and /log/range returns the emitter's own records",
              any(str(s).startswith("av.bootstrap.") for s in srcs),
              str(sorted(s for s in srcs if s))[:160])
    finally:
        save_profiles(keep)
        bs._active_profile_name = _prev_name
        bs._collector = None
        if _prev_active:
            _active_file.write_text(_prev_active)
        elif _active_file.exists():
            _active_file.unlink()

    print()
    print(f"{'FAILED: ' + str(len(FAILED)) if FAILED else 'all checks passed'}")
    for f in FAILED:
        print(f"  - {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
