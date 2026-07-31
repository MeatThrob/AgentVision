#!/usr/bin/env python3
"""
Token-economics route tests — the agent-facing half of the visual engine.

Exercises the new bridge routes through Flask's in-process test client (no port
binding, no real screen capture) against synthetic frames:

  /visual_changes      collapses runs of identical frames; honours min_change
  /frame/<n>/json      full descriptor, NO image by default, opt-in thumbnail
  /frame/<n>/region    crops the changed region, returns the right pixels
  /error_moment        one-call bundle: error + frame + region + logs + code
  /visual_events       auto-bookmarks fire for freeze / blank / on-screen error
  /token_report        the arithmetic is internally consistent
  /start_here          orientation payload
  /bookmarks           unchanged log-driven shape + additive visual_bookmarks

Requires flask/psutil/pillow (run inside a venv). Run:
    python3 python_backend/api/test_visual_routes.py
"""
from __future__ import annotations

import base64
import io
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
    print(f"  [{'ok ' if cond else 'FAIL'}] {name}"
          f"{'' if cond or not detail else '  — ' + detail}")


# A realistic display size. NOTE: on a very small image the JSON
# descriptor can cost MORE than the image itself — the cheap path only
# pays off at real screen resolutions, and token_report says so.
SIZE = (1280, 800)


def _draw(path, boxes=(), bg=(24, 26, 34)):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", SIZE, bg)
    d = ImageDraw.Draw(im)
    for y in range(0, SIZE[1], 30):
        d.line([(0, y), (SIZE[0], y)], fill=(80, 86, 100))
    for x in range(0, SIZE[0], 60):
        d.line([(x, 0), (x, SIZE[1])], fill=(70, 76, 92))
    for (x, y, w, h, col) in boxes:
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=col)
    im.save(path)
    return path


def main():
    work = tempfile.mkdtemp(prefix="av_vroutes_")
    av = os.path.join(work, "agentvision")
    os.makedirs(av)
    actions = os.path.join(av, "actions.jsonl")
    base_ms = 1784628000000.0
    # One record per second across the whole synthetic run so every frame has a
    # real time-aligned log window.
    with open(actions, "w") as f:
        for i in range(18):
            is_err = (i == 13)
            rec = {"ts_ms": base_ms + i * 1000,
                   "ts": "2026-07-21T10:00:%02d.000Z" % i,
                   "category": "error" if is_err else "event",
                   "level": "ERROR" if is_err else "INFO",
                   "source": "app.db",
                   "data": {"message": "KeyError: 'cfg'" if is_err else f"tick {i}",
                            "exception_type": "KeyError" if is_err else ""}}
            f.write(json.dumps(rec) + "\n")

    from connectors.program_connector import (ProgramProfile, save_profiles,
                                              load_profiles, BUILTIN_PROFILES)
    p = load_profiles()
    p["vrt"] = ProgramProfile(
        name="vrt", display_name="VisualRouteTest", action_log_file=actions,
        project_root=work, process_name="nonexistent_xyz",
        log_sources=[{"path": actions, "adapter": "jsonl", "label": "events"}])
    save_profiles({k: v for k, v in p.items()
                   if k not in BUILTIN_PROFILES or k == "vrt"})

    # Remove this test's profile on exit. Without it the suite PERSISTED "vrt"
    # into the shipped python_backend/profiles.json, so the repo accumulated
    # test-only profiles pointing at long-deleted temp dirs — and they would ship.
    # atexit rather than a finally block so cleanup also runs when a check raises.
    import atexit as _atexit

    def _av_drop_test_profile() -> None:
        try:
            _p = load_profiles()
            if "vrt" in _p:
                del _p["vrt"]
                save_profiles({k: v for k, v in _p.items()
                               if k not in BUILTIN_PROFILES})
        except Exception:
            pass

    _atexit.register(_av_drop_test_profile)
    os.environ["AGENTVISION_ACTIVE_PROFILE"] = "vrt"

    import api.bridge_server as bs
    from utils import visual_engine as ve
    bs._active_profile_name = "vrt"
    bs._collector = None
    client = bs.app.test_client()

    def gj(path):
        r = client.get(path)
        try:
            return r.status_code, r.get_json()
        except Exception:
            return r.status_code, None

    # ── Build a synthetic capture run ────────────────────────────────────────
    # 12 frames: 1-4 identical, 5 = small patch appears, 6-9 identical,
    # 10 = full repaint (layout change), 11 = blank, 12 = error frame.
    plans = []
    for i in range(1, 5):
        plans.append(("same", ()))
    plans.append(("patch", ((300, 200, 90, 60, (250, 250, 255)),)))
    for i in range(6, 10):
        plans.append(("patch", ((300, 200, 90, 60, (250, 250, 255)),)))
    plans.append(("flip", ()))
    plans.append(("blank", ()))
    # 12 follows the blank frame, so its whole screen changes (correctly).
    plans.append(("patch", ((300, 200, 90, 60, (250, 250, 255)),)))
    # 13 differs from 12 only by a small red box -> a SMALL changed region.
    plans.append(("patch_err", ((300, 200, 90, 60, (250, 250, 255)),
                                (160, 120, 200, 100, (255, 40, 40)))))

    prev_grid, prev_dhash = None, ""
    for idx, (kind, boxes) in enumerate(plans, start=1):
        img = os.path.join(av, f"frame_{idx:05d}.png")
        if kind == "flip":
            _draw(img, bg=(240, 240, 245))
        elif kind == "blank":
            from PIL import Image
            Image.new("RGB", SIZE, (0, 0, 0)).save(img)
        else:
            _draw(img, boxes=boxes)
        res = ve.analyze_with_health(img, prev_grid=prev_grid, prev_dhash=prev_dhash)
        vis = res["visual"]
        grid = vis.pop("_grid", b"")
        frame = {
            "sequence": idx,
            "timestamp_ms": base_ms + idx * 1000.0,
            "timestamp": "2026-07-21T10:00:%02d.000Z" % idx,
            "image_file": f"frame_{idx:05d}.png",
            "annotated_image": img,
            "json_sidecar": img.replace(".png", "_frame.json"),
            "program": {"running": True},
            "capture_meta": {"shutter_ms": base_ms + idx * 1000.0,
                             "black_frame": bool(res["health"].get("is_blank")),
                             "window_found": True,
                             "image_health": res["health"],
                             "visual": vis},
            "state_delta": {"changed_count": 1, "changed": {"phase": {"from": "a", "to": "b"}}},
            "error": {},
        }
        if idx == 13:
            frame["error"] = {
                "exception_type": "KeyError", "message": "KeyError: 'cfg'",
                "file": os.path.join(work, "app.py"), "line": 3,
                "probable_cause": "missing config key",
                "fingerprint": "fp-test-1",
                "frames": [{"file": os.path.join(work, "app.py"),
                            "line": 3, "func": "load"}],
            }
        with bs._lock:
            bs._frames[idx] = frame
            bs._latest_frame = frame
            if grid:
                bs._grids[idx] = grid
            bs._record_visual_stats(vis)
        prev_grid, prev_dhash = grid, vis.get("dhash") or ""

    Path(work, "app.py").write_text("import json\ncfg = {}\nvalue = cfg['cfg']\n")

    # ── /visual_changes ─────────────────────────────────────────────────────
    print("visual_changes:")
    sc, d = gj("/visual_changes?limit=100")
    check("/visual_changes 200", sc == 200)
    rows = (d or {}).get("rows") or []
    no_change_rows = [r for r in rows if r.get("no_change")]
    changed_rows = [r for r in rows if not r.get("no_change")]
    check("identical runs are collapsed", len(no_change_rows) >= 2,
          f"{len(no_change_rows)} collapsed rows of {len(rows)}")
    check("collapsed rows carry seq_range + frames",
          all(("seq_range" in r and r.get("frames", 0) >= 1) for r in no_change_rows),
          json.dumps(no_change_rows[:2]))
    check("collapsed rows are typed identical vs minor_change",
          all(r.get("kind") in ("identical", "minor_change") for r in no_change_rows),
          json.dumps(no_change_rows[:2]))
    check("EVERY collapsed run keeps an escalation handle (seq_range)",
          all(isinstance(r.get("seq_range"), list) and len(r["seq_range"]) == 2
              for r in no_change_rows), json.dumps(no_change_rows[:2]))
    check("collapsed rows cover more than one frame each",
          any(r.get("frames", 0) > 1 for r in no_change_rows),
          json.dumps(no_change_rows[:3]))
    check("fewer rows than frames (that IS the saving)",
          len(rows) < 12, f"{len(rows)} rows for 12 frames")
    check("collapsed frame count is reported",
          (d or {}).get("frames_collapsed_as_unchanged", 0) >= 2, str(d and d.get("frames_collapsed_as_unchanged")))
    check("changed moments carry a one-line summary",
          all("one_line_summary" in r for r in changed_rows),
          json.dumps(changed_rows[:1]))
    check("changed moments carry change_score + bbox",
          any(r.get("changed_bbox") for r in changed_rows))
    check("no image bytes anywhere in the response",
          "image_b64" not in json.dumps(d or {}))
    check("response names the escalation path",
          "av_frame_region" in json.dumps((d or {}).get("next") or {}))
    # min_change gating
    sc2, d2 = gj("/visual_changes?limit=100&min_change=0.99")
    changed_hi = [r for r in (d2 or {}).get("rows", []) if not r.get("no_change")]
    check("high min_change surfaces fewer moments",
          len(changed_hi) <= len(changed_rows), f"{len(changed_hi)} vs {len(changed_rows)}")
    # windowing
    sc3, d3 = gj(f"/visual_changes?from_ms={base_ms + 9500}&to_ms={base_ms + 12500}")
    check("window filters frames", sc3 == 200
          and (d3 or {}).get("frames_considered", 99) <= 4,
          str(d3 and d3.get("frames_considered")))
    # payload really is small
    payload_chars = len(json.dumps(d or {}))
    check("whole-run review payload is small (<8000 chars)", payload_chars < 8000,
          f"{payload_chars} chars")
    print(f"    12-frame run reviewed in {payload_chars} chars "
          f"(~{ve.est_text_tokens(payload_chars)} est tokens)")

    # ── /frame/<n>/json ─────────────────────────────────────────────────────
    print("frame_json:")
    sc, fj = gj("/frame/5/json")
    check("/frame/5/json 200", sc == 200)
    for k in ("seq", "ts_ms", "size", "dhash", "change_score", "changed_bbox",
              "structural", "one_line_summary", "token_math"):
        check(f"descriptor has {k}", k in (fj or {}))
    check("NO full image by default",
          "image_b64" not in (fj or {}) and "thumbnail_b64" not in (fj or {}))
    check("structural includes dominant_colors",
          "dominant_colors" in ((fj or {}).get("structural") or {}))
    check("token_math compares to the full frame",
          ((fj or {}).get("token_math") or {}).get("full_frame_est_visual_tokens", 0) > 0)
    check("descriptor is cheaper than the full frame it describes",
          (fj["token_math"]["this_descriptor_est_tokens"]
           < fj["token_math"]["full_frame_est_visual_tokens"]),
          json.dumps(fj.get("token_math")))
    check("aligned_logs are included and bounded",
          isinstance((fj or {}).get("aligned_logs"), list)
          and len(fj["aligned_logs"]) <= 6, str(len(fj.get("aligned_logs") or [])))
    check("ocr degrades gracefully when tesseract is absent",
          ("ocr_text" in fj) and (fj["ocr_text"] is not None
                                  or "ocr_unavailable" in fj))
    sc, fj_t = gj("/frame/5/json?thumbnail=1&thumb_width=64")
    check("thumbnail is opt-in and tiny",
          sc == 200 and fj_t.get("thumbnail_size", [999])[0] == 64,
          str(fj_t.get("thumbnail_size")))
    check("thumbnail token cost is reported",
          (fj_t.get("thumbnail_est_visual_tokens") or 0) > 0)
    sc, fj_e = gj("/frame/9999/json")
    check("unknown seq -> 404 JSON", sc == 404 and isinstance(fj_e, dict))
    sc, fj12 = gj("/frame/13/json")
    check("error frame surfaces its error in the descriptor",
          (fj12 or {}).get("error", {}).get("exception_type") == "KeyError",
          json.dumps((fj12 or {}).get("error")))

    # ── /frame/<n>/region ───────────────────────────────────────────────────
    print("frame_region:")
    sc, rg = gj("/frame/13/region?bbox=changed&max_dim=900&ocr=0")
    check("/frame/13/region 200", sc == 200, json.dumps(rg)[:200] if rg else "")
    check("region returns base64 pixels", bool((rg or {}).get("image_b64")))
    check("region reports bbox_mode=changed", (rg or {}).get("bbox_mode") == "changed")
    check("region is smaller than the full frame",
          (rg or {}).get("served_size", [999, 999])[0] < SIZE[0])
    check("region token math shows the saving",
          ((rg or {}).get("token_math") or {}).get("est_tokens_saved_vs_full_frame", 0) > 0,
          json.dumps((rg or {}).get("token_math")))
    # The changed region of frame 13 is the red box at (160,120,200,100).
    if rg and rg.get("image_b64"):
        from PIL import Image
        with Image.open(io.BytesIO(base64.b64decode(rg["image_b64"]))) as ci:
            ci = ci.convert("RGB")
            cx, cy = ci.size[0] // 2, ci.size[1] // 2
            px = ci.getpixel((cx, cy))
        bx = rg["bbox"]
        check("changed bbox contains the drawn red box",
              bx[0] <= 160 and bx[1] <= 120 and bx[0] + bx[2] >= 360
              and bx[1] + bx[3] >= 220, f"bbox={bx}")
        check("cropped pixels are the red box", px[0] > 180 and px[1] < 90,
              f"centre pixel={px}")
    sc, rgf = gj("/frame/13/region?bbox=full&ocr=0")
    check("bbox=full serves the whole frame", sc == 200
          and rgf.get("bbox") == [0, 0, SIZE[0], SIZE[1]], str(rgf and rgf.get("bbox")))
    sc, rge = gj("/frame/13/region?bbox=10,10,50,40&ocr=0")
    check("explicit bbox honoured", sc == 200 and rge.get("bbox") == [10, 10, 50, 40],
          str(rge and rge.get("bbox")))
    check("explicit bbox crop is 50x40", rge.get("served_size") == [50, 40],
          str(rge and rge.get("served_size")))
    sc, _ = gj("/frame/13/region?bbox=nonsense,1&ocr=0")
    check("malformed bbox -> 400 (client error, not 500)", sc == 400, f"got {sc}")
    sc, small = gj("/frame/13/region?bbox=full&max_dim=64&ocr=0")
    check("max_dim caps the served crop",
          max(small.get("served_size") or [999]) <= 64, str(small.get("served_size")))
    check("a capped crop costs fewer tokens than the full frame",
          small["est_visual_tokens"] < small["est_visual_tokens_full_frame"],
          f"{small.get('est_visual_tokens')} vs {small.get('est_visual_tokens_full_frame')}")

    # ── /error_moment ───────────────────────────────────────────────────────
    print("error_moment:")
    sc, em = gj("/error_moment?seq=13&window_secs=6")
    check("/error_moment 200", sc == 200)
    check("bundle found", (em or {}).get("found") is True)
    check("bundle has the structured error",
          (em or {}).get("error", {}).get("exception_type") == "KeyError",
          json.dumps((em or {}).get("error")))
    check("bundle names probable_cause",
          bool((em or {}).get("error", {}).get("probable_cause")))
    check("bundle has stack frames",
          isinstance((em or {}).get("error", {}).get("stack_frames"), list))
    check("bundle has the frame at that shutter",
          (em or {}).get("frame", {}).get("seq") == 13)
    check("bundle has the changed region bbox",
          bool((em or {}).get("changed_region", {}).get("bbox")),
          json.dumps((em or {}).get("changed_region")))
    check("changed region pixels omitted by default (cheap)",
          (em or {}).get("changed_region", {}).get("image_b64") is None)
    check("changed region tells you how to get pixels",
          "av_frame_region" in str((em or {}).get("changed_region", {})
                                   .get("how_to_get_pixels")))
    check("bundle has the time-aligned log window",
          isinstance((em or {}).get("logs"), list) and len(em["logs"]) >= 1,
          str(len(em.get("logs") or [])))
    check("log window bounds are reported",
          bool((em or {}).get("log_window", {}).get("from_ms")))
    check("bundle has the state_delta", "state_delta" in (em or {}))
    check("bundle has the code context",
          bool((em or {}).get("code", {}).get("frames")),
          json.dumps((em or {}).get("code"))[:200])
    check("bundle names the calls it replaces",
          len((em or {}).get("replaces_calls") or []) >= 5)
    check("bundle reports its own token cost",
          ((em or {}).get("est_bundle_tokens") or 0) > 0)
    check("on_screen_text key present (None when OCR absent)",
          "on_screen_text" in (em or {}))
    sc, em2 = gj("/error_moment?include_image=1&seq=13")
    check("include_image=1 attaches the pixels",
          sc == 200 and bool((em2 or {}).get("changed_region", {}).get("image_b64")))
    sc, em3 = gj("/error_moment")
    check("no args -> resolves the latest error", sc == 200
          and (em3 or {}).get("found") is True
          and (em3 or {}).get("frame_seq") == 13, str(em3 and em3.get("resolved_by")))
    sc, em4 = gj("/error_moment?fingerprint=fp-test-1")
    check("fingerprint lookup works", sc == 200 and (em4 or {}).get("found") is True,
          str(em4 and em4.get("resolved_by")))
    sc, em5 = gj("/error_moment?fingerprint=does-not-exist")
    check("unknown fingerprint -> 404 with a hint",
          sc == 404 and "hint" in (em5 or {}), f"{sc}")

    # ── /visual_events (auto-bookmarks) ─────────────────────────────────────
    print("visual_events:")
    with bs._lock:
        bs._visual_events.clear()
        bs._visual_prev["still_since_ms"] = None
    # blank_screen: frame 11 is uniform black
    bs._detect_visual_events(11, base_ms + 11000,
                             bs._visual_of(bs._frames[11]),
                             bs._frames[11]["annotated_image"])
    sc, ev = gj("/visual_events")
    types = {e["type"] for e in (ev or {}).get("events", [])}
    check("blank_screen event fires", "blank_screen" in types, str(types))
    # layout_change: frame 10 is a full repaint
    bs._detect_visual_events(10, base_ms + 10000,
                             bs._visual_of(bs._frames[10]),
                             bs._frames[10]["annotated_image"])
    sc, ev = gj("/visual_events")
    types = {e["type"] for e in (ev or {}).get("events", [])}
    check("layout_change event fires", "layout_change" in types, str(types))
    # screen_frozen: feed unchanged frames spanning > FREEZE_SECONDS
    with bs._lock:
        bs._visual_events.clear()
        bs._visual_prev["still_since_ms"] = None
    still = {"assessed": True, "dhash": "aaaaaaaaaaaaaaaa", "change_score": 0.0,
             "structural": {"is_blank": False, "size": list(SIZE)},
             "size": list(SIZE), "changed_bbox": None}
    t = base_ms
    for i in range(12):
        bs._detect_visual_events(100 + i, t + i * 1000.0, still, "")
    sc, ev = gj("/visual_events?type=screen_frozen")
    evs = (ev or {}).get("events") or []
    check("screen_frozen event fires after the freeze threshold", len(evs) >= 1,
          str(types))
    check("a long freeze collapses into ONE event (not one per frame)",
          len(evs) == 1 and evs[0].get("frames", 0) > 1,
          f"{len(evs)} events, frames={evs[0].get('frames') if evs else '-'}")
    check("freeze event reports how long it was still",
          (evs[0].get("still_for_s") or 0) >= 5.0 if evs else False,
          str(evs[0].get("still_for_s") if evs else None))
    check("freeze event points at a next call",
          "av_frame_json" in str(evs[0].get("next")) if evs else False)
    check("detectors + thresholds are self-describing",
          "screen_frozen" in ((ev or {}).get("detectors") or {})
          and "AGENTVISION_VISUAL_FREEZE_SECS" in ((ev or {}).get("thresholds_env") or {}))
    # on_screen_error: OCR is optional, so stub it to prove the detector works,
    # then prove the no-OCR path is silent rather than broken.
    with bs._lock:
        bs._visual_events.clear()
        bs._visual_stats["last_ocr_scan_ms"] = 0.0
    real_ocr = bs._ocr_image
    bs._ocr_image = lambda *a, **k: {
        "available": True, "engine": "stub",
        "text": "Loading...\nTraceback (most recent call last):\nKeyError: 'cfg'"}
    try:
        moved = {"assessed": True, "dhash": "bbbbbbbbbbbbbbbb", "change_score": 0.5,
                 "structural": {"is_blank": False, "size": list(SIZE)},
                 "size": list(SIZE), "changed_bbox": [0, 0, 100, 100]}
        bs._detect_visual_events(200, base_ms + 60000.0, moved, "")
    finally:
        bs._ocr_image = real_ocr
    sc, ev = gj("/visual_events?type=on_screen_error")
    evs = (ev or {}).get("events") or []
    check("on_screen_error event fires when OCR finds a traceback", len(evs) == 1,
          str(evs))
    check("on_screen_error carries the offending lines",
          bool(evs and evs[0].get("on_screen_text")), str(evs[:1]))
    check("on_screen_error names the matched keywords",
          bool(evs and evs[0].get("keywords")), str(evs[:1]))
    with bs._lock:
        bs._visual_events.clear()
        bs._visual_stats["last_ocr_scan_ms"] = 0.0
    bs._ocr_image = lambda *a, **k: {"available": False, "reason": "no tesseract"}
    try:
        bs._detect_visual_events(201, base_ms + 90000.0, moved, "")
    finally:
        bs._ocr_image = real_ocr
    sc, ev = gj("/visual_events?type=on_screen_error")
    check("no OCR -> no on_screen_error event, no crash",
          len(((ev or {}).get("events") or [])) == 0)

    # ── /bookmarks additive, not breaking ───────────────────────────────────
    print("bookmarks:")
    sc, bm = gj("/bookmarks")
    check("/bookmarks 200", sc == 200)
    check("original log-driven keys intact",
          "count" in (bm or {}) and isinstance((bm or {}).get("bookmarks"), list))
    check("visual bookmarks added alongside",
          "visual_bookmarks" in (bm or {}) and "visual_count" in (bm or {}))

    # ── /token_report ───────────────────────────────────────────────────────
    print("token_report:")
    sc, tr = gj("/token_report")
    check("/token_report 200", sc == 200)
    free = (tr or {}).get("capture_side_free_work") or {}
    check("estimation method is stated plainly",
          "ESTIMATES" in ((tr or {}).get("estimation_method") or ""))
    check("frames captured is reported", free.get("frames_captured", 0) >= 13,
          str(free.get("frames_captured")))
    check("unchanged + changed <= analyzed",
          (free.get("frames_visually_unchanged", 0)
           + free.get("frames_changed", 0)) <= free.get("frames_analyzed", 0),
          json.dumps({k: free.get(k) for k in
                      ("frames_analyzed", "frames_visually_unchanged", "frames_changed")}))
    check("unchanged_ratio is a fraction in [0,1]",
          0.0 <= (free.get("unchanged_ratio") or 0) <= 1.0,
          str(free.get("unchanged_ratio")))
    check("dedup ratio is a fraction in [0,1]",
          0.0 <= (free.get("dedup_ratio") or 0) <= 1.0, str(free.get("dedup_ratio")))
    check("dedup ratio is non-zero on a run with repeats",
          (free.get("dedup_ratio") or 0) > 0.0, str(free.get("dedup_ratio")))
    cost = free.get("analysis_cost") or {}
    check("per-frame analysis cost is reported",
          (cost.get("avg_ms_per_frame") or 0) > 0, json.dumps(cost))
    check("analysis cost fits the capture budget",
          (cost.get("max_ms_per_frame") or 0) < (cost.get("budget_ms_at_current_rate") or 0),
          json.dumps(cost))
    m = (tr or {}).get("measured_comparison") or {}
    check("measured comparison is available", m.get("available") is True,
          str(m.get("reason")))
    check("measured: full frame costs real visual tokens",
          (m.get("full_frame") or {}).get("est_visual_tokens_if_sent_as_image", 0) > 0)
    check("measured: descriptor is cheaper than the full frame",
          (m.get("av_frame_json") or {}).get("est_tokens", 10 ** 9)
          < (m.get("full_frame") or {}).get("est_visual_tokens_if_sent_as_image", 0),
          json.dumps({"json": (m.get("av_frame_json") or {}).get("est_tokens"),
                      "img": (m.get("full_frame") or {}).get("est_visual_tokens_if_sent_as_image")}))
    check("measured: descriptor contains no full image",
          (m.get("av_frame_json") or {}).get("contains_full_image") is False)
    check("measured: crop is cheaper than the full frame",
          ((m.get("changed_region_crop") or {}).get("est_visual_tokens", 10 ** 9)
           <= (m.get("full_frame") or {}).get("est_visual_tokens_if_sent_as_image", 0)),
          json.dumps(m.get("changed_region_crop")))
    check("descriptor/full-frame ratio reported and < 1",
          0 < (m.get("descriptor_vs_full_frame_ratio") or 9) < 1.0,
          str(m.get("descriptor_vs_full_frame_ratio")))
    sess = (tr or {}).get("session_estimate") or {}
    check("session estimate: avoided == naive - spent",
          (sess.get("est_tokens_avoided")
           == max(0, sess.get("est_tokens_if_every_frame_were_sent_as_an_image", 0)
                  - sess.get("est_tokens_actually_spent_on_frames", 0))),
          json.dumps(sess))
    check("session estimate is labelled a counterfactual",
          "counterfactual" in (sess.get("caveat") or "").lower())
    paid = (tr or {}).get("agent_side_paid_work") or {}
    check("agent-side reads are counted",
          (paid.get("av_frame_json_calls") or 0) > 0, json.dumps(paid))

    # ── /start_here ─────────────────────────────────────────────────────────
    print("start_here:")
    sc, sh = gj("/start_here")
    check("/start_here 200", sc == 200)
    for k in ("what_agentvision_is", "watching_now", "state",
              "recommended_workflow", "token_rule_of_thumb", "cheap_path",
              "do_not"):
        check(f"start_here has {k}", k in (sh or {}))
    # recommended_workflow is STATE-DEPENDENT: on a program whose bridge is not
    # built it must lead with the one-time setup, because the old static list told
    # the agent to run av_preflight while the same response said capture would be
    # refused. Asserting one fixed list is what let that contradiction survive.
    _wf = json.dumps((sh or {}).get("recommended_workflow"))
    _built = bool((((sh or {}).get("state") or {})
                   .get("bridge_build") or {}).get("ok"))
    check("workflow always names an unambiguous next call",
          bool((sh or {}).get("DO_THIS_NEXT")), str((sh or {}).get("DO_THIS_NEXT")))
    if _built:
        check("BUILT: workflow lists av_start_here first",
              "av_start_here" in ((sh or {}).get("recommended_workflow") or [""])[0])
        check("BUILT: workflow mentions av_diagnose and av_visual_changes",
              "av_diagnose" in _wf and "av_visual_changes" in _wf)
    else:
        check("PROVISIONAL: workflow leads with the bridge gate",
              "NEVER BEEN BRIDGED" in _wf, _wf[:80])
        check("PROVISIONAL: workflow walks catalog -> commit",
              "av_bridge_catalog" in _wf and "av_bridge_commit" in _wf)
    check("cheap path is ordered json -> thumb -> region -> full",
          list(((sh or {}).get("cheap_path") or {}).keys())
          == ["1_json_only", "2_tiny_thumb", "3_changed_pixels", "4_full_frame"],
          str(list(((sh or {}).get("cheap_path") or {}).keys())))
    check("do_not tells the agent not to screenshot itself",
          any("screenshot" in x.lower() for x in ((sh or {}).get("do_not") or [])))
    check("start_here reports OCR availability",
          "ocr" in ((sh or {}).get("state") or {}))
    # Two budgets, because the two states earn different amounts of space. The
    # BUILT case is the steady state — called at the top of every session, so it
    # must stay lean (measured 3775). The PROVISIONAL case happens ONCE per
    # program and carries the setup walkthrough that stops an agent guessing, so
    # it gets more room (measured 4036). A single 4000 limit would have forced the
    # first-run instructions back out of the payload that most needs them.
    _sz = len(json.dumps(sh or {}))
    _cap = 4000 if _built else 5000
    check(f"start_here stays small ({'BUILT' if _built else 'PROVISIONAL'} "
          f"budget {_cap})", _sz < _cap, f"{_sz} chars")

    # ── existing surfaces still healthy ─────────────────────────────────────
    print("no regressions:")
    sc, st = gj("/status")
    check("/status still 200 and now carries token_rule",
          sc == 200 and "token_rule" in (st or {}))
    sc, cp = gj("/capabilities")
    check("/capabilities lists the cheap visual path",
          sc == 200 and "cheap_visual_path" in ((cp or {}).get("tool_catalog") or {}))
    sc, dg = gj("/digest")
    check("/digest still 200 and now has a visual block",
          sc == 200 and "visual" in (dg or {}))
    sc, lf = gj("/frame/5")
    check("/frame/<n> still returns the full frame + _ai",
          sc == 200 and "_ai" in (lf or {}))
    check("full-frame route points at the cheaper path",
          "CHEAPER_PATH" in ((lf or {}).get("_ai") or {}))

    print(f"\n{'=' * 60}")
    print("visual_routes: " + ("ALL PASS" if _fails == 0 else f"{_fails} FAILURE(S)"))
    print("=" * 60)
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
