#!/usr/bin/env python3
"""
Retention over HTTP: the routes, the push signal, and the release paths.
================================================================================
utils/test_retention.py proves the policy engine in isolation. This suite proves
the part that actually keeps the promise end-to-end:

  * /retention reports the budget and reconfigures the mode live
  * /frames_awaiting lists what is being held and how to discharge it
  * reading a frame through the REAL routes releases it (av_frame_json,
    av_frame_region, av_get_frame, av_error_moment, /replay)
  * av_visual_changes releases ordinary frames but NOT failure frames — a summary
    row is not an inspection of a crash
  * /examine_ack releases in bulk
  * the ambient push actually NAMES the awaiting frames, and re-offers them about
    once a minute instead of saying it once and going quiet

Requires flask/psutil/pillow.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent))
sys.path.insert(0, str(_HERE.parent.parent.parent))

_fails = 0


def check(name, cond, detail=""):
    global _fails
    if not cond:
        _fails += 1
    print(f"  [{'ok ' if cond else 'FAIL'}] {name}"
          f"{'' if cond or not detail else '  — ' + detail}")


SIZE = (320, 240)
ERR = {"exception_type": "ValueError", "message": "boom", "fingerprint": "fp-r1"}


def main():
    work = tempfile.mkdtemp(prefix="av_retroutes_")
    av = os.path.join(work, "agentvision")
    os.makedirs(av)
    actions = os.path.join(av, "actions.jsonl")
    base_ms = 1784628000000.0
    with open(actions, "w") as f:
        for i in range(20):
            f.write(json.dumps({
                "ts_ms": base_ms + i * 1000, "ts": "2026-07-21T10:00:00.000Z",
                "category": "event", "level": "INFO", "source": "app",
                "data": {"message": f"tick {i}"}}) + "\n")

    from connectors.program_connector import (ProgramProfile, save_profiles,
                                              load_profiles, BUILTIN_PROFILES)
    p = load_profiles()
    p["rr"] = ProgramProfile(
        name="rr", display_name="RetentionTest", action_log_file=actions,
        project_root=work, process_name="nonexistent_xyz",
        log_sources=[{"path": actions, "adapter": "jsonl", "label": "events"}])
    save_profiles({k: v for k, v in p.items()
                   if k not in BUILTIN_PROFILES or k == "rr"})

    # Remove this test's profile on exit. Without it the suite PERSISTED "rr"
    # into the shipped python_backend/profiles.json, so the repo accumulated
    # test-only profiles pointing at long-deleted temp dirs — and they would ship.
    # atexit rather than a finally block so cleanup also runs when a check raises.
    import atexit as _atexit

    def _av_drop_test_profile() -> None:
        try:
            _p = load_profiles()
            if "rr" in _p:
                del _p["rr"]
                save_profiles({k: v for k, v in _p.items()
                               if k not in BUILTIN_PROFILES})
        except Exception:
            pass

    _atexit.register(_av_drop_test_profile)
    os.environ["AGENTVISION_ACTIVE_PROFILE"] = "rr"

    import api.bridge_server as bs
    from api import ambient as amb
    from utils import visual_engine as ve
    bs._active_profile_name = "rr"
    bs._collector = None
    bs.RECORDER_ENABLED = True
    client = bs.app.test_client()

    from PIL import Image, ImageDraw

    def add_frame(seq, ts_ms, err=None, tint=40):
        img = os.path.join(av, f"frame_{seq:05d}.png")
        im = Image.new("RGB", SIZE, (tint, tint + 2, tint + 8))
        ImageDraw.Draw(im).rectangle([10, 10, 10 + (seq * 7) % 260, 70],
                                     fill=(200, 210, 220))
        im.save(img)
        side = img.replace(".png", "_frame.json")
        Path(side).write_text("{}")
        res = ve.analyze_with_health(img)
        vis = res["visual"]; vis.pop("_grid", None)
        fr = {"sequence": seq, "timestamp_ms": ts_ms,
              "timestamp": "2026-07-21T10:00:00.000Z",
              "annotated_image": img, "frame_image": img, "json_sidecar": side,
              "program": {"running": True},
              "capture_meta": {"shutter_ms": ts_ms, "visual": vis,
                               "black_frame": False, "window_found": True},
              "state_delta": {}, "error": err or {}}
        with bs._lock:
            bs._frames[seq] = fr
            bs._latest_frame = fr
        stem = f"frame_{seq:05d}"
        bs._ret.LEDGER.admit(seq, ts_ms, bs._ret.stem_group(av, stem),
                             bs._frame_facts(fr, vis, seq),
                             folder=av, stem=stem)
        return fr

    def reset_all(mode="errors"):
        with bs._lock:
            bs._frames.clear(); bs._grids.clear()
            bs._incidents.clear(); bs._pinned_seqs.clear()
            bs._visual_events.clear(); bs._event_kinds_by_seq.clear()
        bs._ret.LEDGER.reset()
        bs._ret.LEDGER.configure(budget_bytes=bs._ret.DEFAULT_BUDGET_BYTES,
                                 mode=mode, hold_s=900.0)
        amb.MEMORY.reset(None)

    def gj(path):
        r = client.get(path)
        return r.status_code, r.get_json()

    def awaiting_seqs():
        return [r["seq"] for r in bs._ret.LEDGER.awaiting(limit=1000)]

    # ── /retention ──────────────────────────────────────────────────────────
    print("/retention reports and reconfigures the contract:")
    reset_all()
    sc, d = gj("/retention")
    check("/retention 200", sc == 200)
    check("defaults to a 5 GB budget", "5.00 GB" == (d or {}).get("budget", {}).get("human"),
          str((d or {}).get("budget", {}).get("human")))
    check("defaults to mode=errors", d["policy"]["mode"] == "errors")
    check("documents all four modes",
          set(d["policy"]["modes_available"]) == {"off", "errors", "changes", "all"})
    check("says deletion is not clock-driven",
          "not by a clock" in d["budget"]["note"], d["budget"]["note"])
    check("explains the hold", "never deleted" in d["policy"]["hold_meaning"],
          d["policy"]["hold_meaning"])
    check("reports integrity", d["integrity"]["ok"] is True)
    check("lists the env knobs", "AGENTVISION_DISK_BUDGET" in d["env"])
    check("points at the next call", "av_frames_awaiting" in d["next"])
    check("reports free disk", d.get("free_disk_bytes") is None
          or d["free_disk_bytes"] > 0, str(d.get("free_disk_bytes")))

    r = client.post("/retention", json={"mode": "all", "budget": "2GB",
                                        "hold_seconds": 30})
    d2 = r.get_json()
    check("POST reconfigures the mode", d2["policy"]["mode"] == "all")
    check("POST reconfigures the budget", d2["budget"]["human"] == "2.00 GB",
          d2["budget"]["human"])
    check("POST reconfigures the hold", d2["policy"]["hold_seconds"] == 30.0)
    check("POST echoes what changed", "changed" in d2)
    r = client.post("/retention", json={})
    check("POST with nothing to change -> 400", r.status_code == 400)
    r = client.post("/retention", json={"mode": "garbage"})
    check("a bad mode falls back to errors, not a crash",
          r.status_code == 200 and r.get_json()["policy"]["mode"] == "errors")

    # ── /frames_awaiting ────────────────────────────────────────────────────
    print("/frames_awaiting lists what is held FOR the agent:")
    reset_all()
    for i in range(1, 6):
        add_frame(i, base_ms + i * 1000.0, tint=40 + i * 9)
    add_frame(6, base_ms + 6000.0, err=ERR, tint=90)
    sc, aw = gj("/frames_awaiting")
    check("/frames_awaiting 200", sc == 200)
    check("the error frame is listed", 6 in [r["seq"] for r in aw["rows"]],
          str(aw["rows"]))
    check("ordinary frames are NOT listed (mode=errors)",
          all(r["seq"] == 6 for r in aw["rows"]), str([r["seq"] for r in aw["rows"]]))
    row = aw["rows"][0]
    check("the row says WHY", "ValueError" in str(row.get("reason")), str(row))
    check("the row is flagged as a failure", row.get("failure") is True)
    check("the row reports its age", row.get("age_seconds") is not None)
    check("the row reports time left before expiry",
          row.get("hold_expires_in_seconds") is not None)
    check("a ready-to-inject push line is provided",
          "av_frame_json(6)" in aw["push_line"], aw["push_line"])
    check("the cheapest-first ladder is spelled out",
          "1_cheapest" in aw["how_to_discharge"]
          and "no pixels" in aw["how_to_discharge"]["1_cheapest"])
    check("it warns that a summary row does not clear a crash",
          "NOT released by av_visual_changes" in aw["note"], aw["note"])

    # ── Reading through the real routes releases the frame ───────────────────
    print("every read route discharges the obligation:")
    for route, seq in (("/frame/{}/json", 6), ("/frame/{}/region", 6),
                       ("/frame/{}", 6), ("/frame/{}/ocr", 6),
                       ("/error_moment?seq={}", 6)):
        reset_all()
        for i in range(1, 6):
            add_frame(i, base_ms + i * 1000.0, tint=40 + i * 9)
        add_frame(6, base_ms + 6000.0, err=ERR, tint=90)
        check(f"awaiting before {route.format(seq)}", 6 in awaiting_seqs())
        sc, _ = gj(route.format(seq))
        check(f"{route.format(seq)} released the frame (status {sc})",
              6 not in awaiting_seqs())

    reset_all()
    for i in range(1, 7):
        add_frame(i, base_ms + i * 1000.0, tint=40 + i * 9)
    add_frame(7, base_ms + 7000.0, err=ERR, tint=95)
    sc, _ = gj("/replay?limit=20")
    check(f"/replay released the frames it walked (status {sc})",
          7 not in awaiting_seqs(), str(awaiting_seqs()))

    # ── visual_changes: ordinary yes, failures no ────────────────────────────
    print("av_visual_changes clears ordinary frames but not crashes:")
    reset_all(mode="all")
    for i in range(1, 6):
        add_frame(i, base_ms + i * 1000.0, tint=40 + i * 20)
    add_frame(6, base_ms + 6000.0, err=ERR, tint=140)
    before = set(awaiting_seqs())
    check("in mode=all, every frame is queued", len(before) == 6, str(before))
    sc, _ = gj("/visual_changes?limit=200")
    after = set(awaiting_seqs())
    check("the survey released the ordinary frames",
          not (after & {1, 2, 3, 4, 5}), str(after))
    check("but the failure frame is STILL awaiting real eyes", 6 in after,
          str(after))
    sc, _ = gj("/frame/6/region")
    check("opening it finally releases it", 6 not in awaiting_seqs())

    # ── /examine_ack ────────────────────────────────────────────────────────
    print("/examine_ack releases in bulk:")
    reset_all(mode="all")
    for i in range(1, 6):
        add_frame(i, base_ms + i * 1000.0, tint=40 + i * 20)
    r = client.post("/examine_ack", json={"seqs": [1, 2]})
    d3 = r.get_json()
    check("ack 200", r.status_code == 200)
    check("acked exactly what was asked", d3["acked"] == 2, str(d3))
    check("the rest are still awaiting", d3["still_awaiting"] == 3, str(d3))
    r = client.post("/examine_ack", json={"seqs": "3,4"})
    check("a comma string works too", r.get_json()["acked"] == 2)
    r = client.post("/examine_ack", json={"all": 1})
    check("all=1 clears the remainder", r.get_json()["still_awaiting"] == 0)
    r = client.post("/examine_ack", json={"all": 1})
    check("all=1 on an empty queue is a no-op, not a 400",
          r.status_code == 200 and r.get_json()["acked"] == 0)
    r = client.post("/examine_ack", json={})
    check("naming nothing at all IS a 400", r.status_code == 400)
    r = client.post("/examine_ack", json={"seqs": [99999]})
    check("acking an unknown seq is counted, not an error",
          r.status_code == 200
          and r.get_json()["already_examined_or_unknown"] == 1)

    # ── The push actually names them ─────────────────────────────────────────
    print("push mode tells the agent BEFORE the pixels expire:")
    reset_all()
    for i in range(1, 6):
        add_frame(i, base_ms + i * 1000.0, tint=40 + i * 9)
    add_frame(6, base_ms + 6000.0, err=ERR, tint=90)
    state = bs._ambient_state_uncached()
    check("ambient state carries the awaiting rows",
          (state.get("frames_awaiting") or {}).get("rows"), str(state.get("frames_awaiting")))
    sigs = amb.build_signals(state)
    fa = [s for s in sigs if s["kind"] == "frames_awaiting"]
    check("a frames_awaiting signal is produced", len(fa) == 1, str([s["kind"] for s in sigs]))
    check("it names the seq", "6" in fa[0]["text"], fa[0]["text"])
    check("it says the pixels get reclaimed",
          "reclaimed" in fa[0]["text"], fa[0]["text"])
    check("it offers the cheapest call first",
          "av_frame_json(6)" in fa[0]["next"], fa[0]["next"])
    check("it offers the bulk release", "av_examine_ack" in fa[0]["next"])
    check("a failure-aligned batch is ALERT tier", fa[0]["tier"] == "alert",
          fa[0]["tier"])
    check("it carries the seqs for offer-tracking",
          fa[0].get("awaiting_seqs") == [6], str(fa[0].get("awaiting_seqs")))

    verdict = amb.decide(state, session_id="s-ret", event="UserPromptSubmit")
    check("the injection fires", verdict["inject"] is True, json.dumps(verdict)[:200])
    check("the injected TEXT names the frame", "6" in verdict["text"], verdict["text"])
    check("decide reports which seqs it offered",
          verdict.get("offered_seqs") == [6], str(verdict.get("offered_seqs")))

    r = client.get("/ambient?session_id=s-http&event=UserPromptSubmit")
    d4 = r.get_json()
    check("/ambient injects it over HTTP", d4.get("inject") is True,
          json.dumps(d4)[:200])
    check("and marks the frame OFFERED",
          any(x["seq"] == 6 and x["offered"] for x in
              bs._ret.LEDGER.awaiting(limit=10)),
          str(bs._ret.LEDGER.awaiting(limit=10)))
    check("offering does NOT release it — being told is not looking",
          6 in awaiting_seqs(), str(awaiting_seqs()))

    # Re-offering: the same pending batch must be re-raised, not said once.
    print("a pending batch is re-offered, not silently dropped:")
    v2 = amb.decide(state, session_id="s-ret", event="UserPromptSubmit")
    check("immediately repeating is suppressed (no per-turn spam)",
          v2.get("inject") is False or "frames_awaiting" not in
          (v2.get("signal_kinds") or []), json.dumps(v2)[:160])
    older = dict(state)
    older["frames_awaiting"] = {
        "rows": [dict(r, age_seconds=(r["age_seconds"] or 0) + 120.0)
                 for r in state["frames_awaiting"]["rows"]],
        "total": 1, "mode": "errors"}
    sig_old = [s for s in amb.build_signals(older) if s["kind"] == "frames_awaiting"]
    check("a minute later the fingerprint changes, so it is raised again",
          sig_old and sig_old[0]["fp"] != fa[0]["fp"],
          f"{sig_old[0]['fp'] if sig_old else None} vs {fa[0]['fp']}")

    # ── Silence when there is nothing to say ────────────────────────────────
    print("silence by default:")
    reset_all()
    for i in range(1, 6):
        add_frame(i, base_ms + i * 1000.0, tint=40 + i * 9)
    st2 = bs._ambient_state_uncached()
    check("no flagged frames => no awaiting signal",
          not [s for s in amb.build_signals(st2) if s["kind"] == "frames_awaiting"])
    bs._ret.LEDGER.configure(mode="off")
    for i in range(10, 13):
        add_frame(i, base_ms + i * 1000.0, err=ERR, tint=60)
    check("mode=off queues nothing at all, even for errors",
          awaiting_seqs() == [], str(awaiting_seqs()))
    reset_all()

    print(f"\n{'=' * 60}")
    print("retention_routes: " + ("ALL PASS" if _fails == 0
                                  else f"{_fails} FAILURE(S)"))
    print("=" * 60)
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
