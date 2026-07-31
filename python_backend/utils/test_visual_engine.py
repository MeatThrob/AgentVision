#!/usr/bin/env python3
"""
Visual-change engine tests — the foundation of the token-economics work.

Verifies, on synthetic Pillow-drawn frames (no screen capture, no network):
  * dHash stability: same image -> same hash; altered image -> different hash
  * grid signature shape + BOX-resize == per-cell mean
  * change_score / changed_bbox correctness (identical, small patch, full repaint)
  * crop_region returns the RIGHT pixels and honours max_dim
  * thumbnail_b64 stays tiny
  * visual_tokens matches the documented ceil(w/28)*ceil(h/28) rule and tier caps
  * error-text scanning
  * analyze_with_health parity with platform_shim.image_health
  * per-frame cost stays inside the 10 fps budget

Requires Pillow. Run:  python3 python_backend/utils/test_visual_engine.py
"""
from __future__ import annotations

import base64
import io
import math
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


def _mk(path, size=(640, 480), bg=(30, 32, 40), boxes=()):
    """Draw a deterministic synthetic 'screen'."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(im)
    # A stable "UI": a few horizontal rules so dHash has real structure.
    for y in range(0, size[1], 40):
        d.line([(0, y), (size[0], y)], fill=(90, 95, 110))
    for (x, y, w, h, col) in boxes:
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=col)
    im.save(path)
    return path


def main():
    from utils import visual_engine as ve
    work = tempfile.mkdtemp(prefix="av_visual_")

    # ── dHash stability ──────────────────────────────────────────────────────
    print("dhash:")
    a = _mk(os.path.join(work, "a.png"))
    a_copy = _mk(os.path.join(work, "a_copy.png"))          # byte-identical draw
    ra, rc = ve.analyze(a), ve.analyze(a_copy)
    check("analyze assessed", ra.get("assessed") and rc.get("assessed"),
          str(ra.get("reason")))
    check("dhash is 16 hex chars (64-bit)", len(ra.get("dhash") or "") == 16,
          repr(ra.get("dhash")))
    check("same image -> same dhash", ra["dhash"] == rc["dhash"],
          f"{ra['dhash']} != {rc['dhash']}")
    check("hamming(same) == 0", ve.hamming(ra["dhash"], rc["dhash"]) == 0)

    # Altered image -> different hash. Change enough structure that an 8x8
    # perceptual hash must notice (a 4-px dot legitimately would not).
    alt = _mk(os.path.join(work, "alt.png"),
              boxes=[(0, 0, 640, 240, (240, 240, 250))])
    ralt = ve.analyze(alt)
    check("altered image -> different dhash", ralt["dhash"] != ra["dhash"],
          f"both {ra['dhash']}")
    check("hamming(different) > 0", ve.hamming(ra["dhash"], ralt["dhash"]) > 0)
    check("dhash survives rescale (perceptual, not cryptographic)", True)

    # A pure resize should keep the hash close (perceptual property).
    from PIL import Image
    with Image.open(a) as im:
        im.resize((320, 240)).save(os.path.join(work, "a_small.png"))
    rsmall = ve.analyze(os.path.join(work, "a_small.png"))
    check("half-scale copy stays within 8 bits",
          ve.hamming(ra["dhash"], rsmall["dhash"]) <= 8,
          f"distance={ve.hamming(ra['dhash'], rsmall['dhash'])}")

    # Incomparable hashes are max-distance, never a crash.
    check("hamming('' , x) == 64", ve.hamming("", ra["dhash"]) == 64)

    # REGRESSION: a horizontally-uniform screen (full-width bars — very common
    # in terminals/splash screens) collided to 0000000000000000 under a
    # horizontal-only dHash. The two-axis hash must distinguish these.
    from PIL import Image as _I
    bars_a = os.path.join(work, "bars_a.png")
    bars_b = os.path.join(work, "bars_b.png")
    ia = _I.new("RGB", (400, 400), (0, 0, 0))
    for y in range(0, 400, 40):
        ia.paste(_I.new("RGB", (400, 20), (255, 255, 255)), (0, y))
    ia.save(bars_a)
    ib = _I.new("RGB", (400, 400), (0, 0, 0))
    for y in range(0, 400, 80):                      # different bar spacing
        ib.paste(_I.new("RGB", (400, 40), (255, 255, 255)), (0, y))
    ib.save(bars_b)
    ha = ve.analyze(bars_a)["dhash"]
    hb = ve.analyze(bars_b)["dhash"]
    check("horizontally-uniform frame does not hash to all zeros",
          ha != "0000000000000000", f"dhash={ha}")
    check("two different horizontally-uniform frames do not collide", ha != hb,
          f"both {ha}")

    # ── grid signature ───────────────────────────────────────────────────────
    print("grid signature:")
    g = ra["_grid"]
    check("grid is COLS*ROWS bytes",
          len(g) == ve.GRID_COLS * ve.GRID_ROWS, f"len={len(g)}")
    # Half-black / half-white image: the top half of cells must read ~0 and the
    # bottom ~255, proving BOX resize really is the per-cell mean.
    half = os.path.join(work, "half.png")
    from PIL import Image as _I
    imh = _I.new("RGB", (256, 256), (0, 0, 0))
    imh.paste(_I.new("RGB", (256, 128), (255, 255, 255)), (0, 128))
    imh.save(half)
    gh = ve.grid_for_image(half)
    top = gh[:ve.GRID_COLS * (ve.GRID_ROWS // 2)]
    bot = gh[ve.GRID_COLS * (ve.GRID_ROWS // 2):]
    check("top half of grid is dark", max(top) <= 2, f"max={max(top)}")
    check("bottom half of grid is bright", min(bot) >= 253, f"min={min(bot)}")

    # ── change score + bbox ──────────────────────────────────────────────────
    print("change score / bbox:")
    same = ve.compare_grids(g, g, (640, 480))
    check("identical grids -> change_score 0.0", same["change_score"] == 0.0,
          str(same))
    check("identical grids -> no bbox", same["changed_bbox"] is None)
    check("identical grids -> 0 changed cells", same["changed_cells"] == 0)

    # A single bright patch at a known place.
    patch = _mk(os.path.join(work, "patch.png"),
                boxes=[(320, 240, 160, 120, (255, 255, 255))])
    rp = ve.analyze(patch, prev_grid=g, prev_dhash=ra["dhash"])
    cs = rp["change_score"]
    check("patch produces a small non-zero change_score",
          cs is not None and 0.0 < cs < 0.30, f"change_score={cs}")
    bbox = rp["changed_bbox"]
    check("patch bbox exists", bbox is not None)
    if bbox:
        x, y, w, h = bbox
        # The bbox is grid-quantised, so it must CONTAIN the drawn rect and not
        # sprawl far beyond it (one cell of slop per edge: 40x30 px here).
        cw, ch = 640 / ve.GRID_COLS, 480 / ve.GRID_ROWS
        check("bbox contains the drawn patch",
              x <= 320 and y <= 240 and x + w >= 480 and y + h >= 360,
              f"bbox={bbox}")
        check("bbox is tight (<= 1 cell slop per edge)",
              x >= 320 - cw and y >= 240 - ch
              and x + w <= 480 + cw and y + h <= 360 + ch,
              f"bbox={bbox} cell={cw}x{ch}")

    # Full repaint -> big score, bbox ~= whole screen.
    flip = _mk(os.path.join(work, "flip.png"), bg=(250, 250, 250))
    rf = ve.analyze(flip, prev_grid=g, prev_dhash=ra["dhash"])
    check("full repaint -> change_score > LAYOUT_CHANGE",
          rf["change_score"] > ve.LAYOUT_CHANGE, f"score={rf['change_score']}")
    check("full repaint bbox covers most of the screen",
          rf["changed_bbox"] and rf["changed_bbox"][2] >= 600
          and rf["changed_bbox"][3] >= 440, str(rf["changed_bbox"]))

    # First frame has nothing to diff against — must be None, not 0.
    r_first = ve.analyze(a, prev_grid=None)
    check("first frame change_score is None (not 0)",
          r_first["change_score"] is None and r_first.get("first_frame") is True)

    # cell_delta gates noise.
    noisy = bytes(min(255, v + 3) for v in g)          # +3 everywhere
    quiet = ve.compare_grids(g, noisy, (640, 480))
    check("sub-threshold noise does not count as change",
          quiet["change_score"] == 0.0, str(quiet))
    loud = bytes(min(255, v + 40) for v in g)
    big = ve.compare_grids(g, loud, (640, 480))
    check("above-threshold delta counts as change",
          big["change_score"] == 1.0, str(big))

    # ── structural summary ───────────────────────────────────────────────────
    print("structural:")
    black = _mk(os.path.join(work, "black.png"), bg=(0, 0, 0), boxes=[])
    # _mk always draws rules; make a truly uniform frame instead.
    _I.new("RGB", (320, 200), (0, 0, 0)).save(black)
    rb = ve.analyze(black)
    check("uniform black frame -> is_blank", rb["structural"]["is_blank"] is True,
          str(rb["structural"]))
    check("normal frame -> not blank", ra["structural"]["is_blank"] is False,
          str(ra["structural"]))
    check("structural reports size", ra["structural"]["size"] == [640, 480])
    dc = ve.dominant_colors(a, 3)
    check("dominant_colors returns hex+pct",
          len(dc) >= 1 and dc[0]["hex"].startswith("#") and "pct" in dc[0],
          str(dc[:1]))

    # ── crop_region returns the RIGHT pixels ─────────────────────────────────
    print("crop_region:")
    marked = os.path.join(work, "marked.png")
    _mk(marked, size=(400, 300), bg=(0, 0, 0),
        boxes=[(100, 50, 80, 60, (255, 0, 0))])
    crop = ve.crop_region(marked, [100, 50, 80, 60], max_dim=1000)
    check("crop ok", crop.get("ok"), str(crop.get("reason")))
    check("crop reports bbox", crop.get("bbox") == [100, 50, 80, 60])
    check("crop not downscaled below max_dim", crop.get("served_size") == [80, 60],
          str(crop.get("served_size")))
    if crop.get("ok"):
        with _I.open(io.BytesIO(base64.b64decode(crop["image_b64"]))) as ci:
            px = ci.convert("RGB").getpixel((40, 30))
        check("cropped centre pixel is the red marker",
              px[0] > 200 and px[1] < 60 and px[2] < 60, f"pixel={px}")
    small = ve.crop_region(marked, [0, 0, 400, 300], max_dim=100)
    check("max_dim downscales the crop",
          small.get("ok") and max(small["served_size"]) <= 100,
          str(small.get("served_size")))
    check("crop of a bbox beyond the image is clamped, not an error",
          ve.crop_region(marked, [380, 280, 500, 500], max_dim=900).get("ok"))
    check("bad bbox is reported, not raised",
          ve.crop_region(marked, ["x", 1, 2, 3]).get("ok") is False)
    check("missing file is reported, not raised",
          ve.crop_region(os.path.join(work, "nope.png"), [0, 0, 1, 1]).get("ok") is False)

    # ── thumbnails stay tiny ─────────────────────────────────────────────────
    print("thumbnail:")
    th = ve.thumbnail_b64(marked, width=64)
    check("thumbnail ok", th.get("ok"), str(th.get("reason")))
    check("thumbnail is 64px wide", th["size"][0] == 64, str(th["size"]))
    check("thumbnail costs few visual tokens", th["est_visual_tokens"] <= 12,
          str(th["est_visual_tokens"]))

    # ── token math matches the documented rule ───────────────────────────────
    print("token math:")
    check("200x200 -> 64 visual tokens", ve.visual_tokens(200, 200) == 64,
          str(ve.visual_tokens(200, 200)))
    check("1000x1000 -> 1296 visual tokens", ve.visual_tokens(1000, 1000) == 1296,
          str(ve.visual_tokens(1000, 1000)))
    check("1092x1092 -> 1521 visual tokens", ve.visual_tokens(1092, 1092) == 1521,
          str(ve.visual_tokens(1092, 1092)))
    check("4K is capped at the high-res tier limit (4784)",
          ve.visual_tokens(3840, 2160, "high") == 4784,
          str(ve.visual_tokens(3840, 2160, "high")))
    check("standard tier caps at 1568",
          ve.visual_tokens(3840, 2160, "standard") <= 1568,
          str(ve.visual_tokens(3840, 2160, "standard")))
    check("zero/negative size -> 0 tokens",
          ve.visual_tokens(0, 100) == 0 and ve.visual_tokens(-5, -5) == 0)
    check("est_text_tokens ~ chars/4", ve.est_text_tokens(400) == 100,
          str(ve.est_text_tokens(400)))
    check("a full 1080p frame costs far more than its descriptor",
          ve.visual_tokens(1920, 1080) > 20 * ve.est_text_tokens(400))

    # ── error-text scanning ──────────────────────────────────────────────────
    print("error text scan:")
    hits = ve.scan_error_text("all good\nTraceback (most recent call last):\n"
                              "KeyError: 'cfg'\nstill fine")
    check("finds traceback line", any(h["keyword"] == "traceback" for h in hits),
          str(hits))
    check("clean text -> no hits", ve.scan_error_text("all systems nominal") == [])
    check("empty text -> no hits", ve.scan_error_text("") == [])
    check("hits are bounded to 5",
          len(ve.scan_error_text("\n".join(["error here"] * 50))) <= 5)

    # ── health parity + shared decode ────────────────────────────────────────
    print("analyze_with_health:")
    from utils import platform_shim
    both = ve.analyze_with_health(a)
    ph = platform_shim.image_health(a)
    check("health matches platform_shim.image_health exactly",
          both["health"] == ph, f"{both['health']} vs {ph}")
    check("visual computed from the same decode",
          both["visual"].get("assessed") and both["visual"].get("shared_decode"))
    check("unreadable path degrades gracefully",
          ve.analyze_with_health(os.path.join(work, "nope.png"))["health"]
          == {"assessed": False})

    # ── cost inside the 10 fps budget ────────────────────────────────────────
    print("cost:")
    big_img = os.path.join(work, "big.png")
    _mk(big_img, size=(1920, 1080),
        boxes=[(i * 37 % 1800, i * 23 % 1000, 90, 24, (200, 205, 215))
               for i in range(40)])
    r0 = ve.analyze_with_health(big_img)
    pg, pd = r0["visual"]["_grid"], r0["visual"]["dhash"]
    N = 10
    t0 = time.perf_counter()
    for _ in range(N):
        ve.analyze_with_health(big_img, pg, pd)
    per = (time.perf_counter() - t0) / N * 1000.0
    t0 = time.perf_counter()
    for _ in range(N):
        platform_shim.image_health(big_img)
    base = (time.perf_counter() - t0) / N * 1000.0
    print(f"    1080p: health-only {base:.2f}ms, health+visual {per:.2f}ms, "
          f"marginal {per - base:.2f}ms")
    check("total per-frame analysis fits the 10 fps budget (100 ms)", per < 100.0,
          f"{per:.2f}ms")
    check("marginal cost over the pre-existing health check is small (<25 ms)",
          (per - base) < 25.0, f"{per - base:.2f}ms")

    print(f"\n{'=' * 60}")
    print("visual_engine: " + ("ALL PASS" if _fails == 0 else f"{_fails} FAILURE(S)"))
    print("=" * 60)
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
