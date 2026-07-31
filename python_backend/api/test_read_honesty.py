#!/usr/bin/env python3
"""Read-honesty tests: AgentVision may not assert what it did not check.

Every case here corresponds to a false read observed on a REAL profile
(SharpEmu, post-mortem: a text log with no timestamps, last written 47 h before
the newest frame, plus an action log that AgentVision's own watchdog keeps
appending to). The rule these lock in:

    a claim must be warranted by data the tool actually inspected, and what it
    cannot warrant must be marked unknown rather than asserted.

They are deliberately built on synthetic fixtures rather than that one profile,
so they keep working when its logs move on.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

_fails: list[str] = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [ok  ] {name}")
    else:
        print(f"  [FAIL] {name}  — {detail}")
        _fails.append(name)


def main():
    os.environ.setdefault("AGENTVISION_ACTIVE_PROFILE", "custom")
    import bridge_server as bs
    from connectors.program_connector import ProgramProfile

    tmp = Path(tempfile.mkdtemp(prefix="av_honesty_"))
    now_ms = time.time() * 1000.0

    # ── fixture: one untimestamped text log, one JSONL whose only recent
    # records are AgentVision's own watchdog ────────────────────────────────
    # Failure/recovery lines in a shape the STRUCTURAL normalizer parses on its
    # own (logfmt: `op key=v ok=bool`). Earlier these carried a `[GPU]` channel
    # tag and the source was pinned `adapter: "auto"` — which only routed and
    # tag-stripped correctly because the developer's user_adapters.json defined a
    # `sharpemu_channel` adapter. That file is (correctly) gitignored, so on a
    # clean clone `auto` fell to `structural`, which leaves the `[GPU]` prefix in
    # the message; `_subject_of` then no longer matched the hand-passed
    # text_failures message and the RECOVERED factor silently vanished. The tag
    # was cosmetic to this test — the operation is `present`, the field is `src`,
    # the outcome is `ok` — so it is removed and the adapter is pinned explicitly.
    text_log = tmp / "log.txt"
    text_log.write_text(
        "present src=0x1000 ok=False\n" * 5
        + "present src=0x2000 ok=True\n"
    )
    jsonl = tmp / "actions.jsonl"
    old = now_ms - 48 * 3600 * 1000.0
    lines = [json.dumps({"ts_ms": old, "category": "log", "source": "app",
                         "message": "started"})]
    for i in range(20):
        lines.append(json.dumps({
            "ts_ms": now_ms - i * 1000.0, "category": "event",
            "source": "agentvision.watchdog",
            "data": {"name": "program.stuck", "silent_s": 100.0}}))
    jsonl.write_text("\n".join(lines) + "\n")

    prof = ProgramProfile(
        name="honesty", display_name="Honesty Fixture",
        process_name="definitely_not_running_xyz",
        project_root=str(tmp), log_file=str(text_log),
        action_log_file=str(jsonl),
        log_sources=[{"label": "textlog", "adapter": "structural", "path": str(text_log)},
                     {"label": "events", "adapter": "jsonl", "path": str(jsonl)}],
    )

    # ── 1. source freshness ─────────────────────────────────────────────────
    print("source freshness:")
    fr = bs._source_freshness(prof, ref_ms=now_ms)
    check("an untimestamped source is named as untimestamped",
          "textlog" in (fr.get("untimestamped_sources") or []),
          str(fr.get("untimestamped_sources")))
    check("a source whose only recent records are AgentVision's own is not 'live'",
          "events" not in (fr.get("alignable_sources") or []),
          str(fr.get("alignable_sources")))
    check("program silence is measured from PROGRAM records, not file mtime",
          (fr.get("program_silent_s") or 0) > 47 * 3600,
          str(fr.get("program_silent_s")))
    ev = [s for s in fr["sources"] if s["label"] == "events"][0]
    check("AgentVision's own records are counted separately",
          ev.get("self_emitted") == 20, str(ev.get("self_emitted")))
    check("freshness note refuses the blanket alignment claim",
          "NOT all log sources" in (fr.get("note") or ""), fr.get("note", "")[:80])

    # ── 2. alignment may not be asserted over unalignable sources ───────────
    print("alignment:")
    frame = {"timestamp_ms": now_ms, "capture_meta": {}, "action_log_offset": 0,
             "profile_action_log": str(jsonl)}
    al = bs._alignment_health(frame, freshness=fr)
    check("aligned=False when a source cannot be time-aligned",
          al.get("aligned") is False, str(al.get("aligned")))
    check("the leak check is still reported separately",
          al.get("no_future_records") is True, str(al.get("no_future_records")))
    check("the reason is carried, not just the verdict",
          al.get("alignment_verdict") == "unalignable_sources",
          str(al.get("alignment_verdict")))
    # …and it MUST still be able to say aligned=True when that is true.
    clean = dict(fr, stale_sources=[], untimestamped_sources=[],
                 alignable_sources=["events"], note="")
    al2 = bs._alignment_health(frame, freshness=clean)
    check("aligned=True is still reachable when every source is current",
          al2.get("aligned") is True, str(al2.get("aligned")))

    # ── 3. recovery is represented at all ───────────────────────────────────
    print("recovery:")
    rec = bs._recovery_report(prof)
    ops = [o for o in (rec.get("operations") or []) if "present" in o["operation"]]
    check("an operation that failed then succeeded is found", bool(ops),
          json.dumps(rec)[:200])
    if ops:
        op = ops[0]
        check("failure count is exact", op.get("failed") == 5, str(op.get("failed")))
        check("success count is exact", op.get("succeeded") == 1,
              str(op.get("succeeded")))
        check("recovery is stated", op.get("recovered") is True,
              str(op.get("recovered")))
        check("the field that changed is identified",
              any(c["field"] == "src" and c["while_failing"] == "0x1000"
                  and c["when_it_worked"] == "0x2000"
                  for c in (op.get("changed_fields") or [])),
              str(op.get("changed_fields")))
        check("causation is explicitly NOT claimed",
              "correlation only" in str(op.get("reading", "")).lower(),
              str(op.get("reading"))[:120])

    # ── 4. an all-clear may not contradict the health grade ─────────────────
    print("health:")
    h = bs._health_block(None, [], [], bs._auto_engine,
                         text_failures={"total": 180, "escalated": 180,
                                        "groups": [{"source": "GPU", "count": 180,
                                                    "message": "present src=0x1000 ok=False",
                                                    "escalated": True,
                                                    "escalation_reason": "ok=false"}]},
                         running_now=False, program_silent_s=170000.0)
    check("a not-running program cannot grade healthy",
          h.get("grade") != "healthy", json.dumps(h))
    check("the not-running deduction fires from the LIVE check",
          any("not running" in f for f in h["factors"]), str(h["factors"]))
    check("silence is scored",
          any("no log output" in f for f in h["factors"]), str(h["factors"]))
    check("a post-mortem grade says it is a post-mortem",
          "POST-MORTEM" in (h.get("basis") or ""), str(h.get("basis")))
    # Recovery must soften, never erase, a real failure.
    h2 = bs._health_block(None, [], [], bs._auto_engine,
                          text_failures={"total": 180, "escalated": 180,
                                         "groups": [{"source": "GPU", "count": 180,
                                                     "message": "present src=0x1000 ok=False",
                                                     "escalated": True,
                                                     "escalation_reason": "ok=false"}]},
                          recovery=rec, running_now=True)
    check("recovery is named in the factors when it happened",
          any("RECOVERED" in f for f in h2["factors"]), str(h2["factors"]))
    check("recovery does not erase the failure deduction",
          h2["score"] < 100 and any("180" in f for f in h2["factors"]),
          json.dumps(h2))

    # ── 5. cross-source disjointness is measured, not implied by key format ─
    print("source event map:")
    sm = bs._source_event_map(prof)
    labels = {s["label"]: s for s in sm["sources"]}
    check("the present events are exclusive to the text log",
          any("present" in x["signature"]
              for x in labels["textlog"]["top_exclusive"]),
          json.dumps(labels["textlog"]["top_exclusive"])[:160])
    check("the watchdog events are exclusive to the JSONL source",
          any("program.stuck" in x["signature"]
              for x in labels["events"]["top_exclusive"]),
          json.dumps(labels["events"]["top_exclusive"])[:160])
    # The same line written to both sources must be seen as SHARED even though
    # each adapter derives a different `source` field for it.
    a = tmp / "a.txt"
    b = tmp / "b.txt"
    a.write_text("[ALPHA] widget count=1 loaded\n")
    b.write_text("[BETA] widget count=2 loaded\n")
    prof2 = ProgramProfile(
        name="xsig", display_name="xsig", process_name="nope_xyz",
        project_root=str(tmp),
        log_sources=[{"label": "a", "adapter": "auto", "path": str(a)},
                     {"label": "b", "adapter": "auto", "path": str(b)}])
    sm2 = bs._source_event_map(prof2)
    check("channel tag + number differences do not fake disjointness",
          sm2.get("shared_signatures", 0) >= 1,
          f"shared={sm2.get('shared_signatures')} note={sm2.get('note','')[:80]}")

    # ── 6. every hypothesis carries the documented shape + a confidence ─────
    print("hypothesis shape:")
    norm = bs._normalize_hypotheses([
        {"hypothesis": "shape A", "severity": "high", "next": ["x"]},
        {"summary": "shape B", "confidence": 0.4, "evidence": "e",
         "probable_cause": "p", "recommended_next": ["y"]},
    ])
    need = ("summary", "confidence", "evidence", "probable_cause",
            "recommended_next", "rank")
    check("all hypotheses expose the documented keys",
          all(all(k in h for k in need) for h in norm),
          str([sorted(h.keys()) for h in norm]))
    check("aliases are NOT duplicated alongside the canonical names",
          all("hypothesis" not in h and "next" not in h for h in norm),
          str([sorted(h.keys()) for h in norm]))
    check("confidence is always a number in 0..1",
          all(isinstance(h["confidence"], (int, float))
              and 0.0 <= h["confidence"] <= 1.0 for h in norm),
          str([h["confidence"] for h in norm]))
    check("a derived confidence says it was derived",
          "confidence_basis" in norm[0] and "confidence_basis" not in norm[1],
          str(norm))

    print()
    if _fails:
        print(f"RESULT: {len(_fails)} FAILURE(S)")
        for f in _fails:
            print(f"  - {f}")
        return 1
    print("RESULT: all read-honesty tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
