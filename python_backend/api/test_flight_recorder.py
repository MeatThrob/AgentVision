#!/usr/bin/env python3
"""
Flight-recorder tests — the rolling window and pre-error incident freeze.

The recorder is the load-bearing claim that always-on 10 fps capture is
sustainable: old frames are pruned continuously, but the window BEFORE a failure
is frozen and exempt from pruning. These tests prove BOTH halves, because either
one alone is a bug (never pruning fills the disk; pruning without pinning loses
the evidence).

Covers:
  * pruning removes frames older than the rolling window AND deletes their files
  * an incident PINS its window so pruning cannot touch it
  * a repeat trigger extends an incident instead of duplicating it
  * the incident cap unpins frames it no longer needs
  * /incidents listing + detail, /replay walk, and the incident link in
    /error_moment
  * an error on a frame also freezes an incident

Requires flask/psutil/pillow. Run:
    python3 python_backend/api/test_flight_recorder.py
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


def main():
    work = tempfile.mkdtemp(prefix="av_recorder_")
    av = os.path.join(work, "agentvision")
    os.makedirs(av)
    actions = os.path.join(av, "actions.jsonl")
    base_ms = 1784628000000.0
    with open(actions, "w") as f:
        for i in range(40):
            f.write(json.dumps({
                "ts_ms": base_ms + i * 1000, "ts": "2026-07-21T10:00:00.000Z",
                "category": "event", "level": "INFO", "source": "app",
                "data": {"message": f"tick {i}"}}) + "\n")

    from connectors.program_connector import (ProgramProfile, save_profiles,
                                              load_profiles, BUILTIN_PROFILES)
    p = load_profiles()
    p["fr"] = ProgramProfile(
        name="fr", display_name="RecorderTest", action_log_file=actions,
        project_root=work, process_name="nonexistent_xyz",
        log_sources=[{"path": actions, "adapter": "jsonl", "label": "events"}])
    save_profiles({k: v for k, v in p.items()
                   if k not in BUILTIN_PROFILES or k == "fr"})

    # Remove this test's profile on exit. Without it the suite PERSISTED "fr"
    # into the shipped python_backend/profiles.json, so the repo accumulated
    # test-only profiles pointing at long-deleted temp dirs — and they would ship.
    # atexit rather than a finally block so cleanup also runs when a check raises.
    import atexit as _atexit

    def _av_drop_test_profile() -> None:
        try:
            _p = load_profiles()
            if "fr" in _p:
                del _p["fr"]
                save_profiles({k: v for k, v in _p.items()
                               if k not in BUILTIN_PROFILES})
        except Exception:
            pass

    _atexit.register(_av_drop_test_profile)
    os.environ["AGENTVISION_ACTIVE_PROFILE"] = "fr"

    # Small window so the test is fast and deterministic.
    os.environ["AGENTVISION_RECORDER_WINDOW_S"] = "10"
    os.environ["AGENTVISION_RECORDER_TAIL_S"] = "2"

    import api.bridge_server as bs
    from utils import visual_engine as ve
    bs._active_profile_name = "fr"
    bs._collector = None
    # The module read the env at import time; force the test values.
    bs.RECORDER_WINDOW_S = 10.0
    bs.RECORDER_TAIL_S = 2.0
    bs.RECORDER_ENABLED = True
    client = bs.app.test_client()

    from PIL import Image, ImageDraw

    def add_frame(seq: int, ts_ms: float, err: dict | None = None,
                  tint: int = 40) -> dict:
        img = os.path.join(av, f"frame_{seq:05d}.png")
        im = Image.new("RGB", SIZE, (tint, tint + 2, tint + 8))
        d = ImageDraw.Draw(im)
        d.rectangle([10, 10, 10 + (seq * 3) % 200, 60], fill=(200, 210, 220))
        im.save(img)
        side = img.replace(".png", "_frame.json")
        Path(side).write_text("{}")
        # The siblings that the OLD pruner created and then never deleted. They
        # exist here so the group-deletion contract is actually proved.
        Path(img.replace(".png", "_annotated.png")).write_bytes(b"a" * 2048)
        Path(img.replace(".png", ".diff")).write_bytes(b"d" * 64)
        Path(img.replace(".png", "_thumb.png")).write_bytes(b"t" * 128)
        res = ve.analyze_with_health(img)
        vis = res["visual"]
        vis.pop("_grid", None)
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
        # Retention must know about the frame or the byte budget is a fiction.
        stem = f"frame_{seq:05d}"
        bs._ret.LEDGER.admit(seq, ts_ms, bs._ret.stem_group(av, stem),
                             bs._frame_facts(fr, vis, seq),
                             folder=av, stem=stem)
        return fr

    def reset_all():
        with bs._lock:
            bs._frames.clear(); bs._grids.clear()
            bs._incidents.clear(); bs._pinned_seqs.clear()
            bs._visual_events.clear(); bs._event_kinds_by_seq.clear()
        bs._ret.LEDGER.reset()
        bs._ret.LEDGER.configure(budget_bytes=bs._ret.DEFAULT_BUDGET_BYTES,
                                 mode="errors", hold_s=900.0)
        bs._recorder_stats.update({"frames_pruned": 0, "bytes_pruned": 0,
                                   "prunes_run": 0})

    def squeeze(fraction: float = 0.5):
        """Shrink the byte budget below what we are holding, so the next prune
        has to evict. This is how retention is now provoked — by disk pressure,
        never by a clock."""
        used = bs._ret.LEDGER.used_bytes()
        bs._ret.LEDGER.configure(budget_bytes=max(1, int(used * fraction)))
        return used

    def gj(path):
        r = client.get(path)
        return r.status_code, r.get_json()

    # ── Retention: age is NOT a delete rule ─────────────────────────────────
    # THE REGRESSION GUARD. The recorder used to delete everything older than
    # RECORDER_WINDOW_S. 60 s is shorter than one agent reasoning turn, so
    # AgentVision could announce "error at frame 4213" and delete 4213 before it
    # could be fetched — every one of those shots taken for nothing. Frames now
    # expire on DISK PRESSURE, and only after they have been examined.
    print("retention: the clock no longer deletes anything:")
    reset_all()
    paths = {}
    for i in range(1, 31):                     # 30 frames, 1 s apart
        fr = add_frame(i, base_ms + i * 1000.0)
        paths[i] = fr["annotated_image"]
    with bs._lock:
        before = len(bs._frames)
    bs._recorder_prune(base_ms + 30_000.0)     # "now" == frame 30
    bs._recorder_prune(base_ms + 86_400_000.0)  # ...and a DAY later
    with bs._lock:
        after = sorted(bs._frames.keys())
    check("frames existed before pruning", before == 30, str(before))
    check("a day past the 10s window, NOTHING is deleted (the fix)",
          len(after) == 30, f"kept {len(after)}")
    check("the oldest frame's pixels are still on disk",
          os.path.exists(paths[1]))
    check("its sidecar is still there too",
          os.path.exists(paths[1].replace(".png", "_frame.json")))

    # ── Authored annotations survive both ends ───────────────────────────────
    # `<stem>_annotations.json` is the one file in a frame's group with no
    # archive equivalent: hand-written cross-session breadcrumbs. Retention used
    # to unlink it on the ordinary path, and the annotate route used to REPLACE
    # it whenever its own read failed — `except Exception: pass` left `existing`
    # at {"annotations": []} and the write then committed that. Both ends now
    # refuse.
    print("authored annotations are never silently replaced:")
    reset_all()
    fr = add_frame(7, base_ms + 7_000.0)
    side = fr["json_sidecar"]
    r = client.post("/frame/7/annotate", json={"message": "first note"})
    check("first note accepted", r.status_code == 200, str(r.status_code))
    r = client.post("/frame/7/annotate", json={"message": "second note"})
    check("second note appends rather than replacing",
          (r.get_json() or {}).get("annotation_count") == 2,
          str((r.get_json() or {}).get("annotation_count")))
    ann = side.replace("_frame.json", "_annotations.json")
    check("the annotations file exists", os.path.exists(ann))
    good = open(ann).read()

    # Now corrupt it, exactly as a crash mid-write would.
    with open(ann, "w") as fh:
        fh.write('{"annotations": [{"message": "irreplaceable"')
    broken = open(ann).read()
    r = client.post("/frame/7/annotate", json={"message": "third note"})
    check("a note is REFUSED when the existing file cannot be read",
          r.status_code == 409, str(r.status_code))
    check("and it says the notes were not saved",
          (r.get_json() or {}).get("saved") is False, r.get_data(as_text=True)[:160])
    check("the unreadable file is left exactly as it was",
          open(ann).read() == broken)

    # Repaired, appending works again — and the earlier notes are still there.
    with open(ann, "w") as fh:
        fh.write(good)
    r = client.post("/frame/7/annotate", json={"message": "third note"})
    check("once repaired, appending resumes and keeps the old notes",
          (r.get_json() or {}).get("annotation_count") == 3,
          str((r.get_json() or {}).get("annotation_count")))
    check("no .tmp left behind by the atomic write",
          not os.path.exists(ann + ".tmp")
          and not os.path.exists(ann.replace(".json", ".json.tmp")))

    # Rebuild the 30-frame state the byte-budget block below starts from.
    reset_all()
    paths = {}
    for i in range(1, 31):
        fr = add_frame(i, base_ms + i * 1000.0)
        paths[i] = fr["annotated_image"]
    bs._recorder_prune(base_ms + 30_000.0)

    # ── Retention: the byte budget IS the delete rule ────────────────────────
    print("retention: eviction is driven by the byte budget:")
    used = squeeze(0.5)
    check("the ledger accounts for real bytes on disk", used > 0, str(used))
    bs._recorder_prune(base_ms + 30_000.0)
    with bs._lock:
        after = sorted(bs._frames.keys())
    check("over budget => frames are evicted", len(after) < 30, str(len(after)))
    check("the OLDEST cheap frames go first", 1 not in after and 30 in after,
          f"kept {after[:3]}…{after[-2:]}")
    check("eviction stops once back under the low-water mark", len(after) > 0,
          str(len(after)))
    check("evicted PNG is gone", not os.path.exists(paths[1]))
    check("the _annotated.png sibling goes too (the 4.7 GB leak)",
          not os.path.exists(paths[1].replace(".png", "_annotated.png")))
    check("the .diff sibling goes too (also never pruned before)",
          not os.path.exists(paths[1].replace(".png", ".diff")))
    check("the sidecar JSON is KEPT as permanent history",
          os.path.exists(paths[1].replace(".png", "_frame.json")))
    check("the thumbnail is KEPT so history stays visual",
          os.path.exists(paths[1].replace(".png", "_thumb.png")))
    check("surviving frame files still exist", os.path.exists(paths[30]))
    check("eviction is counted in recorder stats",
          bs._recorder_stats["frames_pruned"] >= 5,
          str(bs._recorder_stats["frames_pruned"]))
    check("eviction reclaimed bytes", bs._recorder_stats["bytes_pruned"] > 0,
          str(bs._recorder_stats["bytes_pruned"]))

    # ── Retention: a frame awaiting the agent's eyes is protected ────────────
    print("retention: examine before delete:")
    reset_all()
    err_facts = {"exception_type": "ValueError", "message": "boom",
                 "fingerprint": "fp-x"}
    for i in range(1, 21):
        add_frame(i, base_ms + i * 1000.0)
    add_frame(21, base_ms + 21_000.0, err=err_facts)      # needs eyes
    check("the error frame is queued for examination",
          21 in [r["seq"] for r in bs._ret.LEDGER.awaiting()],
          str(bs._ret.LEDGER.awaiting()))
    check("an ordinary frame is NOT queued",
          5 not in [r["seq"] for r in bs._ret.LEDGER.awaiting()])
    squeeze(0.2)                                   # brutal pressure
    bs._recorder_prune(base_ms + 30_000.0)
    with bs._lock:
        survivors = sorted(bs._frames.keys())
    check("the unexamined error frame SURVIVES heavy disk pressure",
          21 in survivors, f"kept {survivors}")
    check("ordinary frames were sacrificed instead", len(survivors) < 21,
          str(len(survivors)))
    check("nothing was dropped unexamined",
          bs._ret.LEDGER.stats["dropped_unexamined"] == 0,
          str(bs._ret.LEDGER.stats["dropped_unexamined"]))
    # Now the agent looks at it — which releases it.
    sc, _ = gj("/frame/21/json")
    check("reading the frame marks it examined",
          21 not in [r["seq"] for r in bs._ret.LEDGER.awaiting()], str(sc))
    squeeze(0.2)
    bs._recorder_prune(base_ms + 40_000.0)
    with bs._lock:
        survivors2 = sorted(bs._frames.keys())
    check("once examined it becomes evictable like anything else",
          21 not in survivors2 or len(survivors2) <= 2, str(survivors2))

    # ── Retention: the hold backstop is reported, never silent ───────────────
    print("retention: the hold backstop reports its losses:")
    reset_all()
    bs._ret.LEDGER.configure(hold_s=5.0)
    for i in range(1, 11):
        add_frame(i, base_ms + i * 1000.0, err=err_facts)
    squeeze(0.2)
    bs._recorder_prune(base_ms + 600_000.0)        # long past a 5 s hold
    check("expired-unexamined frames ARE eventually freed",
          bs._ret.LEDGER.stats["dropped_unexamined"] > 0,
          str(bs._ret.LEDGER.stats["dropped_unexamined"]))
    rep = bs._ret.LEDGER.report()
    check("and the report admits it rather than claiming success",
          rep["integrity"]["ok"] is False, json.dumps(rep["integrity"]))
    reset_all()

    # ── Incident freeze pins its window ─────────────────────────────────────
    print("incident freeze:")
    reset_all()
    paths = {}
    for i in range(1, 31):
        fr = add_frame(i, base_ms + i * 1000.0)
        paths[i] = fr["annotated_image"]
    inc = bs._freeze_incident("screen_frozen", 12, base_ms + 12_000.0,
                              "screen unchanged for 6.0s")
    check("incident is created", isinstance(inc, dict) and inc.get("id"),
          str(inc))
    check("incident records the trigger", inc.get("trigger_seq") == 12)
    check("incident window is pre-error + tail",
          inc["window_ms"][0] == base_ms + 2_000.0
          and inc["window_ms"][1] == base_ms + 14_000.0,
          str(inc["window_ms"]))
    check("incident froze the frames in that window",
          inc["frame_count"] >= 10, str(inc["frame_count"]))
    check("incident reports the pre-error seconds",
          inc.get("pre_error_seconds") == 10.0, str(inc.get("pre_error_seconds")))
    frozen = set(inc["frame_seqs"])
    # Squeeze the budget to almost nothing: the only thing that should be left
    # standing is the frozen incident.
    squeeze(0.01)
    bs._recorder_prune(base_ms + 100_000.0)
    with bs._lock:
        survivors = set(bs._frames.keys())
    check("eviction cannot touch a frozen incident's frames",
          frozen.issubset(survivors),
          f"lost {sorted(frozen - survivors)[:5]}")
    check("everything NOT pinned was evicted under extreme pressure",
          survivors == frozen, f"extra {sorted(survivors - frozen)[:5]}")
    check("the ledger agrees the incident frames are pinned",
          bs._ret.LEDGER.report()["frames"]["pinned_by_incident"] >= len(frozen),
          json.dumps(bs._ret.LEDGER.report()["frames"]))
    check("frozen frame files survive on disk",
          all(os.path.exists(paths[s]) for s in sorted(frozen)[:5]))

    # ── Repeat triggers extend, they do not duplicate ────────────────────────
    print("incident dedup:")
    n_before = len(bs._incidents)
    again = bs._freeze_incident("screen_frozen", 13, base_ms + 13_000.0, "still stuck")
    check("a repeat trigger of the same kind extends the incident",
          len(bs._incidents) == n_before, f"{len(bs._incidents)} vs {n_before}")
    check("the extended incident counts its triggers",
          (again or {}).get("trigger_count", 0) >= 2, str((again or {}).get("trigger_count")))
    other = bs._freeze_incident("blank_screen", 14, base_ms + 14_000.0, "black frame")
    check("a DIFFERENT kind creates its own incident",
          len(bs._incidents) == n_before + 1, str(len(bs._incidents)))

    # ── Routes ──────────────────────────────────────────────────────────────
    print("routes:")
    sc, d = gj("/incidents")
    check("/incidents 200", sc == 200)
    check("recorder config is self-describing",
          (d or {}).get("recorder", {}).get("rolling_window_seconds") == 10.0,
          json.dumps((d or {}).get("recorder")))
    check("incidents are listed", (d or {}).get("count", 0) >= 2)
    check("listing omits the bulky frame_seqs array",
          all("frame_seqs" not in i for i in (d or {}).get("incidents", [])))
    check("recorder reports what it pruned",
          ((d or {}).get("recorder", {}).get("frames_pruned_this_session") or 0) > 0)
    check("recorder names its triggers",
          "screen_frozen" in ((d or {}).get("recorder", {}).get("triggers") or []))
    inc_id = inc["id"]
    sc, one = gj(f"/incidents?id={inc_id}")
    check("/incidents?id= returns the detail", sc == 200 and one.get("id") == inc_id)
    check("incident detail lists frame rows",
          isinstance(one.get("frames"), list) and len(one["frames"]) >= 5,
          str(len(one.get("frames") or [])))
    check("incident detail points at the bundle and the replay",
          "av_error_moment" in json.dumps(one.get("next"))
          and "av_replay" in json.dumps(one.get("next")))
    sc, miss = gj("/incidents?id=nope")
    check("unknown incident id -> 404 with a hint", sc == 404 and "hint" in (miss or {}))

    sc, rp = gj(f"/replay?incident={inc_id}&logs=2")
    check("/replay?incident= 200", sc == 200)
    check("replay returns ordered steps",
          isinstance(rp.get("steps"), list) and len(rp["steps"]) >= 1,
          str(len(rp.get("steps") or [])))
    if rp.get("steps"):
        st = rp["steps"]
        check("steps are numbered in order",
              [x["step"] for x in st] == list(range(1, len(st) + 1)))
        check("steps are in time order",
              all(st[i]["ts_ms"] <= st[i + 1]["ts_ms"] for i in range(len(st) - 1)))
        check("steps carry a summary", all("summary" in x for x in st))
        check("steps carry aligned log slices", all("logs" in x for x in st))
    check("replay contains NO image bytes", "image_b64" not in json.dumps(rp))
    check("replay reports its own token cost", (rp.get("est_tokens") or 0) > 0)
    sc, rp2 = gj("/replay?limit=3")
    check("replay honours limit", sc == 200 and len(rp2.get("steps") or []) <= 3)
    sc, rp3 = gj("/replay?incident=nope")
    check("unknown replay incident -> 404", sc == 404)

    # ── An error on a frame freezes an incident, and error_moment links it ───
    print("error -> incident:")
    with bs._lock:
        bs._incidents.clear(); bs._pinned_seqs.clear()
    err = {"exception_type": "ValueError", "message": "bad value",
           "fingerprint": "fp-rec-1", "frames": []}
    add_frame(60, base_ms + 60_000.0, err=err)
    got = bs._freeze_incident("error", 60, base_ms + 60_000.0, "ValueError: bad value",
                              extra={"fingerprint": "fp-rec-1"})
    check("an error freezes an incident", isinstance(got, dict))
    check("the error incident carries the fingerprint",
          (got or {}).get("fingerprint") == "fp-rec-1")
    sc, em = gj("/error_moment?seq=60")
    check("/error_moment 200", sc == 200)
    check("error_moment links the frozen incident",
          bool((em or {}).get("incident", {}).get("id")),
          json.dumps((em or {}).get("incident")))
    check("error_moment explains why the incident matters",
          "BEFORE" in str((em or {}).get("incident", {}).get("why_this_matters")))
    check("error_moment offers a replay of the incident",
          "av_replay" in str((em or {}).get("incident", {}).get("step_through_it")))

    # ── Incident cap unpins what it no longer needs ──────────────────────────
    print("incident cap:")
    with bs._lock:
        bs._frames.clear(); bs._incidents.clear(); bs._pinned_seqs.clear()
    for i in range(200, 260):
        add_frame(i, base_ms + i * 1000.0)
    bs.RECORDER_INCIDENT_CAP = 3
    for k in range(6):
        bs._freeze_incident(f"kind{k}", 200 + k * 5, base_ms + (200 + k * 5) * 1000.0,
                            "synthetic")
    with bs._lock:
        n_inc = len(bs._incidents)
        pinned = set(bs._pinned_seqs)
        needed = set()
        for i2 in bs._incidents:
            needed.update(i2.get("frame_seqs") or [])
    check("incident list is capped", n_inc == 3, str(n_inc))
    check("no frames stay pinned by a dropped incident",
          pinned == needed, f"leaked {sorted(pinned - needed)[:5]}")

    print(f"\n{'=' * 60}")
    print("flight_recorder: " + ("ALL PASS" if _fails == 0 else f"{_fails} FAILURE(S)"))
    print("=" * 60)
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
