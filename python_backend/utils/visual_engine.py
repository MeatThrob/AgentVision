"""
AgentVision — Visual Change Engine
==================================

THE TOKEN THESIS, IN CODE.

Capturing, hashing, diffing and cropping frames is effectively FREE (local CPU
on the user's machine). Every token the AI spends looking at raw pixels is
EXPENSIVE. So this module does the cheap heavy lifting up front and reduces a
screenshot to a few dozen bytes of JSON:

  * `dhash`         — 64-bit perceptual hash (difference hash). Same picture →
                      same hash; a visibly different picture → different hash.
  * `grid`          — a coarse 16x16 mean-luma signature used for diffing.
  * `change_score`  — fraction of grid cells that changed vs the previous frame.
  * `changed_bbox`  — pixel bounding box of the region that actually changed,
                      so the agent can be sent ONLY those pixels.

At 10 fps roughly 99% of consecutive frames are visually identical. With those
four fields the agent reviews ten minutes of capture in a few hundred tokens and
escalates to real pixels only for the moments that changed.

DEPENDENCIES: Pillow (already required) + the standard library. Deliberately no
imagehash / numpy / opencv — perceptual hashing here is ~40 lines of Pillow.
Everything is best-effort and never raises: a failure returns
{"assessed": False, "reason": ...} so the capture loop can never be broken by it.
"""

from __future__ import annotations

import base64
import io
import math
import os
import re
import time

# ── Tunables (env-overridable so the user can retune without editing code) ────

# dHash grid: (DHASH_W + 1) x DHASH_H greyscale samples -> DHASH_W*DHASH_H bits.
DHASH_W = 8
DHASH_H = 8

# Coarse diff grid. 16x16 = 256 cells; enough to localise a changed dialog or a
# changed status line without being sensitive to per-pixel noise.
GRID_COLS = int(os.environ.get("AGENTVISION_VISUAL_GRID_COLS", "16"))
GRID_ROWS = int(os.environ.get("AGENTVISION_VISUAL_GRID_ROWS", "16"))

# A cell counts as "changed" when its mean luma moves at least this much (0-255).
# 8 rejects compression/antialiasing noise but catches real UI repaints.
CELL_DELTA = float(os.environ.get("AGENTVISION_VISUAL_CELL_DELTA", "8"))

# Auto-event thresholds.
FREEZE_SECONDS   = float(os.environ.get("AGENTVISION_VISUAL_FREEZE_SECS", "5.0"))
LAYOUT_CHANGE    = float(os.environ.get("AGENTVISION_VISUAL_LAYOUT_CHANGE", "0.35"))
# MIN_CHANGE is the fraction of grid cells that must move for a frame to be
# surfaced as a "changed moment". One 16x16 cell is 1/256 = 0.0039, so the
# default 0.008 is ~2 cells — on a 1280x800 screen that is roughly a 160x50 px
# region, i.e. an error banner or a status line. Deliberately LOW: for a
# debugging tool a false negative (silently swallowing a one-line error
# appearing on screen) is far worse than a false positive. Frames that change by
# less than this are NOT discarded — they are reported as a collapsed
# `minor_change` run that still carries seq numbers and a bbox to escalate with.
MIN_CHANGE       = float(os.environ.get("AGENTVISION_VISUAL_MIN_CHANGE", "0.008"))

# On-screen error keywords (only used when OCR is available).
ERROR_KEYWORDS = ("error", "exception", "failed", "failure", "crash",
                  "traceback", "fatal", "panic", "segmentation fault",
                  "assertion failed", "stack trace", "unhandled")

# ── Claude image-token accounting (docs: "Resolution and token cost") ─────────
# Claude views images in 28x28-pixel patches; an image costs
#   ceil(width / 28) * ceil(height / 28)
# visual tokens, capped per resolution tier (images above a tier's long-edge or
# visual-token limit are downscaled before processing).
PATCH_PX = 28
TIERS = {
    # tier name: (max long edge px, max visual tokens)
    "standard":       (1568, 1568),   # pre-4.7 models
    "high":           (2576, 4784),   # Claude 4.7 and later
}
DEFAULT_TIER = os.environ.get("AGENTVISION_VISION_TIER", "high")


def visual_tokens(width: int, height: int, tier: str = DEFAULT_TIER) -> int:
    """Estimate the visual-token cost of sending an image of this size to Claude.

    Formula (from the Claude vision docs): ceil(w/28) * ceil(h/28), after the
    image is downscaled to fit the model tier's long-edge and token caps."""
    try:
        w, h = int(width), int(height)
    except Exception:
        return 0
    if w <= 0 or h <= 0:
        return 0
    max_edge, max_tokens = TIERS.get(tier, TIERS[DEFAULT_TIER])
    long_edge = max(w, h)
    if long_edge > max_edge:
        scale = max_edge / float(long_edge)
        w, h = max(1, int(w * scale)), max(1, int(h * scale))
    tokens = math.ceil(w / PATCH_PX) * math.ceil(h / PATCH_PX)
    return int(min(tokens, max_tokens))


def est_text_tokens(n_chars: int) -> int:
    """Rough token count for a JSON/text payload. ~4 chars per token is the
    standard English/JSON approximation; stated plainly wherever it is reported
    so nobody mistakes it for an exact tokenizer count."""
    try:
        return int(math.ceil(max(0, int(n_chars)) / 4.0))
    except Exception:
        return 0


# ── Perceptual hashing + coarse signatures ────────────────────────────────────

def _open_luma(image_path: str):
    """Open an image once as 8-bit greyscale. Returns (Image, (w, h)) or None."""
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(image_path) as im:
            size = im.size
            return im.convert("L").copy(), size
    except Exception:
        return None


def dhash_from_luma(luma) -> str:
    """TWO-AXIS difference hash of a greyscale PIL image -> 16 hex chars (64 bit).

    Classic dHash compares only horizontally-adjacent pixels. That has a real
    failure mode for SCREENS: a window whose content is horizontally uniform
    (full-width bars, a plain terminal with centred rules, a solid splash) has
    left == right everywhere and hashes to all zeros — so two visibly different
    screens collide. So this uses both axes:

        bits 63..32  horizontal differences from a 9x4 sample (8 pairs x 4 rows)
        bits 31..0   vertical   differences from a 4x9 sample (4 cols x 8 pairs)

    Same 64-bit width and the same perceptual properties (robust to scale, mild
    blur and compression; sensitive to real content change), but no blind axis."""
    try:
        from PIL import Image
        bits = 0
        # Horizontal half.
        small = luma.resize((DHASH_W + 1, DHASH_H // 2), Image.BILINEAR)
        px = list(small.getdata())
        for row in range(DHASH_H // 2):
            base = row * (DHASH_W + 1)
            for col in range(DHASH_W):
                bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
        # Vertical half.
        smallv = luma.resize((DHASH_W // 2, DHASH_H + 1), Image.BILINEAR)
        pv = list(smallv.getdata())
        cols = DHASH_W // 2
        for col in range(cols):
            for row in range(DHASH_H):
                top = pv[row * cols + col]
                bot = pv[(row + 1) * cols + col]
                bits = (bits << 1) | (1 if top > bot else 0)
        return f"{bits:016x}"
    except Exception:
        return ""


def grid_from_luma(luma) -> bytes:
    """Coarse mean-luma signature: GRID_COLS x GRID_ROWS bytes, row-major.

    Pillow's BOX resample IS the per-cell mean, so this is one C-level resize
    rather than a Python pixel loop."""
    try:
        from PIL import Image
        small = luma.resize((GRID_COLS, GRID_ROWS), Image.BOX)
        return bytes(small.getdata())
    except Exception:
        return b""


def hamming(a: str, b: str) -> int:
    """Hamming distance between two hex hashes. 64 (max) when incomparable."""
    if not a or not b or len(a) != len(b):
        return 64
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except Exception:
        return 64


def compare_grids(prev: bytes, cur: bytes, size: tuple[int, int],
                  cell_delta: float = CELL_DELTA) -> dict:
    """Diff two grid signatures.

    Returns {change_score, mean_abs_diff, max_abs_diff, changed_cells,
    changed_bbox}. `change_score` is the FRACTION of grid cells whose mean luma
    moved by >= cell_delta (0.0 == visually identical). `changed_bbox` is
    [x, y, w, h] in ORIGINAL image pixels — the region worth sending."""
    total = GRID_COLS * GRID_ROWS
    if not prev or not cur or len(prev) != len(cur) or len(cur) != total:
        return {"change_score": None, "mean_abs_diff": None,
                "max_abs_diff": None, "changed_cells": 0, "changed_bbox": None}
    changed = 0
    total_diff = 0
    max_diff = 0
    min_cx = min_cy = 10 ** 9
    max_cx = max_cy = -1
    for i in range(total):
        d = prev[i] - cur[i]
        if d < 0:
            d = -d
        total_diff += d
        if d > max_diff:
            max_diff = d
        if d >= cell_delta:
            changed += 1
            cx, cy = i % GRID_COLS, i // GRID_COLS
            if cx < min_cx: min_cx = cx
            if cy < min_cy: min_cy = cy
            if cx > max_cx: max_cx = cx
            if cy > max_cy: max_cy = cy

    bbox = None
    if max_cx >= 0:
        w, h = int(size[0] or 0), int(size[1] or 0)
        if w > 0 and h > 0:
            cw, ch = w / float(GRID_COLS), h / float(GRID_ROWS)
            x0 = int(min_cx * cw)
            y0 = int(min_cy * ch)
            x1 = min(w, int(math.ceil((max_cx + 1) * cw)))
            y1 = min(h, int(math.ceil((max_cy + 1) * ch)))
            bbox = [x0, y0, max(1, x1 - x0), max(1, y1 - y0)]

    return {
        "change_score":  round(changed / float(total), 4),
        "mean_abs_diff": round(total_diff / float(total), 2),
        "max_abs_diff":  int(max_diff),
        "changed_cells": changed,
        "changed_bbox":  bbox,
    }


def structural_from_luma(luma, size: tuple[int, int], thumb=None) -> dict:
    """Cheap structural summary of a frame: brightness, contrast, blankness and
    an estimate of how many distinct text-ish regions there are. Lets the agent
    reason about a frame from JSON alone.

    `thumb` may be a pre-built 64px greyscale thumbnail to reuse (avoids a second
    downscale when the caller already made one for the health check)."""
    try:
        from PIL import ImageStat
        if thumb is None:
            thumb = luma.copy()
            thumb.thumbnail((64, 64))
        st = ImageStat.Stat(thumb)
        mean = float(st.mean[0])
        stddev = float(st.stddev[0])
        px = list(thumb.getdata())
        # Rows containing a strong light/dark transition read as text/UI rows.
        w, h = thumb.size
        text_rows = 0
        for y in range(h):
            row = px[y * w:(y + 1) * w]
            if not row:
                continue
            swings = sum(1 for i in range(1, len(row)) if abs(row[i] - row[i - 1]) > 40)
            if swings >= 3:
                text_rows += 1
        return {
            "mean_luma":   round(mean, 1),
            "contrast":    round(stddev, 1),
            "is_blank":    bool(stddev < 2.0 or mean < 3.0),
            "is_dark":     bool(mean < 60),
            "text_rows":   text_rows,
            "size":        [int(size[0] or 0), int(size[1] or 0)],
        }
    except Exception:
        return {"size": [int(size[0] or 0), int(size[1] or 0)]}


def dominant_colors(image_path: str, k: int = 3) -> list[dict]:
    """Top-k coarse colours as {hex, pct}. Quantised to a 4-bit-per-channel cube
    off a 32x32 thumbnail so this stays sub-millisecond."""
    try:
        from PIL import Image
        with Image.open(image_path) as im:
            im = im.convert("RGB")
            im.thumbnail((32, 32))
            px = list(im.getdata())
    except Exception:
        return []
    if not px:
        return []
    buckets: dict[tuple, int] = {}
    for r, g, b in px:
        key = (r >> 4, g >> 4, b >> 4)
        buckets[key] = buckets.get(key, 0) + 1
    top = sorted(buckets.items(), key=lambda kv: -kv[1])[:max(1, k)]
    total = float(len(px))
    out = []
    for (r, g, b), n in top:
        out.append({"hex": "#%02x%02x%02x" % (r * 17, g * 17, b * 17),
                    "pct": round(100.0 * n / total, 1)})
    return out


def _derive(luma, size, prev_grid, prev_dhash, thumb=None) -> dict:
    """All derivations from an already-decoded greyscale image. Cheap (~1-5 ms
    even at 4K) — the expensive part is the PNG decode, which the caller owns."""
    dh = dhash_from_luma(luma)
    grid = grid_from_luma(luma)
    cmp_ = compare_grids(prev_grid or b"", grid, size)
    out = {
        "assessed":       True,
        "dhash":          dh,
        "dhash_distance": hamming(prev_dhash, dh) if prev_dhash and dh else None,
        "structural":     structural_from_luma(luma, size, thumb=thumb),
        "size":           [int(size[0] or 0), int(size[1] or 0)],
        "_grid":          grid,
    }
    out.update(cmp_)
    # First frame of a session has nothing to diff against.
    if prev_grid is None or not prev_grid:
        out["change_score"] = None
        out["changed_bbox"] = None
        out["first_frame"] = True
    return out


def analyze(image_path: str, prev_grid: bytes | None = None,
            prev_dhash: str = "") -> dict:
    """THE capture-time entry point. One image open, three cheap derivations.

    Returns a dict safe to store in a frame's capture_meta:
      {assessed, dhash, dhash_distance, change_score, mean_abs_diff,
       changed_bbox, changed_cells, structural, size, cost_ms}
    plus `_grid` (raw bytes) for the caller to cache in memory — callers should
    NOT persist `_grid`; it is recomputable from the image and would bloat the
    per-frame JSON the agent reads."""
    t0 = time.perf_counter()
    opened = _open_luma(image_path)
    if opened is None:
        return {"assessed": False, "reason": "image unreadable or Pillow missing"}
    luma, size = opened
    try:
        out = _derive(luma, size, prev_grid, prev_dhash)
        out["cost_ms"] = round((time.perf_counter() - t0) * 1000.0, 3)
        return out
    except Exception as exc:
        return {"assessed": False, "reason": f"{type(exc).__name__}: {exc}",
                "cost_ms": round((time.perf_counter() - t0) * 1000.0, 3)}
    finally:
        try:
            luma.close()
        except Exception:
            pass


def analyze_with_health(image_path: str, prev_grid: bytes | None = None,
                        prev_dhash: str = "") -> dict:
    """ONE decode -> BOTH the capture-health verdict and the visual descriptor.

    The capture loop already paid for a full image decode to run the blank/black
    health check (platform_shim.image_health). Decoding is ~90% of the cost of
    visual analysis at 4K, so doing both off a single decode makes the visual
    engine nearly free on the 10 fps path instead of doubling the per-frame cost.

    Returns {"health": {...same shape as platform_shim.image_health...},
             "visual": {...analyze() output...}}. `health` is {"assessed": False}
    when Pillow or the image is unavailable, so the caller can fall back to
    platform_shim.image_health() and preserve exact prior behaviour."""
    t0 = time.perf_counter()
    opened = _open_luma(image_path)
    if opened is None:
        return {"health": {"assessed": False},
                "visual": {"assessed": False,
                           "reason": "image unreadable or Pillow missing"}}
    luma, size = opened
    try:
        from PIL import ImageStat
        thumb = luma.copy()
        thumb.thumbnail((64, 64))
        st = ImageStat.Stat(thumb)
        mean, stddev = float(st.mean[0]), float(st.stddev[0])
        health = {"mean": round(mean, 2), "stddev": round(stddev, 2),
                  "is_blank": bool(stddev < 2.0 or mean < 3.0), "assessed": True}
        visual = _derive(luma, size, prev_grid, prev_dhash, thumb=thumb)
        visual["cost_ms"] = round((time.perf_counter() - t0) * 1000.0, 3)
        visual["shared_decode"] = True
        return {"health": health, "visual": visual}
    except Exception as exc:
        return {"health": {"assessed": False},
                "visual": {"assessed": False,
                           "reason": f"{type(exc).__name__}: {exc}",
                           "cost_ms": round((time.perf_counter() - t0) * 1000.0, 3)}}
    finally:
        try:
            luma.close()
        except Exception:
            pass


def grid_for_image(image_path: str) -> bytes:
    """Recompute a grid signature for an image on disk (used to lazily rebuild
    the in-memory grid cache after a bridge restart)."""
    opened = _open_luma(image_path)
    if opened is None:
        return b""
    luma, _ = opened
    try:
        return grid_from_luma(luma)
    finally:
        try:
            luma.close()
        except Exception:
            pass


# ── Serving the smallest sufficient pixels ────────────────────────────────────

def crop_region(image_path: str, bbox, max_dim: int = 900,
                fmt: str = "PNG") -> dict:
    """Crop `bbox` = [x, y, w, h] out of an image and return it base64-encoded,
    downscaled so the long edge is <= max_dim.

    This is the tier-3 escalation of the cheap path: when the JSON descriptor is
    not enough, send ONLY the pixels that changed instead of a full 4K frame."""
    try:
        from PIL import Image
    except Exception:
        return {"ok": False, "reason": "Pillow not available"}
    if not image_path or not os.path.exists(image_path):
        return {"ok": False, "reason": f"image not found: {image_path}"}
    try:
        x, y, w, h = [int(v) for v in bbox]
    except Exception:
        return {"ok": False, "reason": f"bad bbox {bbox!r}; want x,y,w,h"}
    try:
        with Image.open(image_path) as im:
            full_w, full_h = im.size
            x = max(0, min(x, full_w - 1))
            y = max(0, min(y, full_h - 1))
            w = max(1, min(w, full_w - x))
            h = max(1, min(h, full_h - y))
            crop = im.crop((x, y, x + w, y + h))
            pre = crop.size
            if max(crop.size) > max_dim:
                crop.thumbnail((max_dim, max_dim))
            buf = io.BytesIO()
            crop.convert("RGB").save(buf, format=fmt, optimize=True)
            raw = buf.getvalue()
        cw, ch = crop.size
        return {
            "ok": True,
            "bbox": [x, y, w, h],
            "full_size": [full_w, full_h],
            "crop_size": [pre[0], pre[1]],
            "served_size": [cw, ch],
            "format": fmt.lower(),
            "bytes": len(raw),
            "image_b64": base64.b64encode(raw).decode("ascii"),
            "est_visual_tokens": visual_tokens(cw, ch),
            "est_visual_tokens_full_frame": visual_tokens(full_w, full_h),
        }
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def thumbnail_b64(image_path: str, width: int = 64, fmt: str = "PNG") -> dict:
    """A deliberately tiny thumbnail (default 64px wide). Off by default in the
    frame descriptor — a 64px thumb still costs real visual tokens, whereas the
    JSON descriptor costs almost none."""
    try:
        from PIL import Image
    except Exception:
        return {"ok": False, "reason": "Pillow not available"}
    if not image_path or not os.path.exists(image_path):
        return {"ok": False, "reason": f"image not found: {image_path}"}
    try:
        with Image.open(image_path) as im:
            full = im.size
            ratio = width / float(im.size[0] or 1)
            target = (max(1, int(width)), max(1, int((im.size[1] or 1) * ratio)))
            thumb = im.convert("RGB").resize(target)
            buf = io.BytesIO()
            thumb.save(buf, format=fmt, optimize=True)
            raw = buf.getvalue()
        return {"ok": True, "size": list(target), "full_size": list(full),
                "bytes": len(raw), "format": fmt.lower(),
                "image_b64": base64.b64encode(raw).decode("ascii"),
                "est_visual_tokens": visual_tokens(*target)}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


# ── Entropy quadtree: WHERE the information actually is ───────────────────────
# Screen content is non-uniformly distributed: huge flat background panels next
# to small, dense, text-heavy regions. A quadtree that subdivides only where
# Shannon entropy is high therefore describes a screen with far fewer regions
# than a fixed grid, and points OCR/cropping at the parts that carry signal.
# (AQuaUI, arXiv 2605.19260, reports ~50% visual-token reduction with this idea;
# ours is the pure-Pillow, no-model version.)

# TAU IS IN BITS of Shannon entropy over a luma histogram, and the useful range
# for SCREENS is surprisingly LOW. A photograph has 5-7 bits, but a UI screenshot
# is mostly flat background: a 1920x1080 frame with a dense 360x240 block of
# error text measured 0.41 bits GLOBALLY. Measured sweep on that frame:
#   tau 0.20 -> 40 regions, 22 dense, 8.5% coverage
#   tau 0.35 -> 34 regions, 18 dense, 7.0% coverage, densest == the text block
#   tau 0.60+ -> 1 region (never subdivides — useless)
# So the default is 0.35. Do not "correct" it upward to a photographic value.
QUAD_TAU   = float(os.environ.get("AGENTVISION_QUAD_TAU", "0.35"))
QUAD_DEPTH = int(os.environ.get("AGENTVISION_QUAD_DEPTH", "4"))
QUAD_MIN_PX = int(os.environ.get("AGENTVISION_QUAD_MIN_PX", "48"))


def _entropy(hist: list[int], total: int) -> float:
    """Shannon entropy (bits) of a luma histogram."""
    if total <= 0:
        return 0.0
    h = 0.0
    for c in hist:
        if c:
            p = c / total
            h -= p * math.log2(p)
    return h


def quadtree_regions(image_path: str, tau: float = QUAD_TAU,
                     max_depth: int = QUAD_DEPTH,
                     min_px: int = QUAD_MIN_PX) -> dict:
    """Entropy-guided quadtree over a frame.

    Uniform regions stay coarse; busy/text-dense regions recurse. Returns
    {available, tau, max_depth, region_count, regions:[{bbox, entropy, depth,
    dense}], densest, coverage_pct, cost_ms} where `dense` marks the leaves worth
    OCR-ing or cropping.

    Cheap by construction: the image is downscaled once to <=256px and every
    entropy is computed from a Pillow histogram of a crop of that thumbnail."""
    t0 = time.perf_counter()
    opened = _open_luma(image_path)
    if opened is None:
        return {"available": False, "reason": "image unreadable or Pillow missing"}
    luma, size = opened
    try:
        W, H = size
        small = luma.copy()
        small.thumbnail((256, 256))
        sw, sh = small.size
        sx, sy = (W / float(sw or 1)), (H / float(sh or 1))
        leaves: list[dict] = []

        def _rec(x, y, w, h, depth):
            if w <= 1 or h <= 1:
                return
            box = small.crop((x, y, x + w, y + h))
            hist = box.histogram()
            ent = _entropy(hist, w * h)
            # Map back to ORIGINAL image pixels.
            bbox = [int(x * sx), int(y * sy),
                    max(1, int(w * sx)), max(1, int(h * sy))]
            too_small = (bbox[2] <= min_px or bbox[3] <= min_px)
            if ent > tau and depth < max_depth and not too_small:
                hw, hh = w // 2, h // 2
                if hw >= 1 and hh >= 1:
                    _rec(x, y, hw, hh, depth + 1)
                    _rec(x + hw, y, w - hw, hh, depth + 1)
                    _rec(x, y + hh, hw, h - hh, depth + 1)
                    _rec(x + hw, y + hh, w - hw, h - hh, depth + 1)
                    return
            leaves.append({"bbox": bbox, "entropy": round(ent, 2),
                           "depth": depth, "dense": bool(ent > tau)})

        _rec(0, 0, sw, sh, 0)
        leaves.sort(key=lambda r: -r["entropy"])
        dense = [r for r in leaves if r["dense"]]
        area = sum(r["bbox"][2] * r["bbox"][3] for r in dense)
        return {
            "available": True,
            "tau": tau, "max_depth": max_depth, "min_region_px": min_px,
            "size": [W, H],
            "region_count": len(leaves),
            "dense_region_count": len(dense),
            "coverage_pct": (round(100.0 * area / float(W * H), 1)
                             if W and H else None),
            "densest": leaves[:6],
            "regions": leaves[:64],
            "meaning": ("`dense` leaves are the information-rich parts of the "
                        "screen — OCR or crop those instead of the whole frame"),
            "cost_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}",
                "cost_ms": round((time.perf_counter() - t0) * 1000.0, 2)}
    finally:
        try:
            luma.close()
        except Exception:
            pass


def densest_region(image_path: str, **kw) -> list | None:
    """The single highest-entropy region bbox — the 'most interesting' pixels.
    Used by av_frame_region(bbox='dense')."""
    q = quadtree_regions(image_path, **kw)
    if not q.get("available") or not q.get("densest"):
        return None
    return q["densest"][0]["bbox"]


# ── On-screen error text ──────────────────────────────────────────────────────

#: Separators OCR can hallucinate INSIDE a word. Collapsed before keyword
#: matching so "Unhand led" still matches "unhandled".
_SPLIT_RE = re.compile(r"[\s\-_]+")


#: What an OCR pass actually established about on-screen errors. Three states,
#: because "OCR read the screen and saw no error" and "OCR read nothing at all"
#: are opposite facts that a bare False silently merges — and the merge is what
#: lets a frame showing an unreadable error dialog be scored as clean.
OCR_ERROR_FOUND = "error_found"
OCR_NO_ERROR = "no_error_found"
OCR_UNREADABLE = "could_not_read"


def classify_ocr_error(text: str, available: bool = True,
                       keywords=ERROR_KEYWORDS) -> tuple[str, list[dict]]:
    """(state, hits) — never collapse "nothing readable" into "nothing wrong".

    MEASURED: on a 2560x1440 frame, 32 of 72 single-keyword error lines at 12-18px
    produced EMPTY OCR output. The same lines on a canvas where the text was large
    relative to the frame read 192/192. So on real full-screen captures the common
    failure is not a misread — it is silence, and silence scored as `False` told
    retention the frame was clean and made it evictable. That is the 180-present-
    failures incident again, in the visual channel, with the evidence deleted at
    the end rather than merely unmentioned.
    """
    if not available:
        return OCR_UNREADABLE, []
    if not (text or "").strip():
        return OCR_UNREADABLE, []
    hits = scan_error_text(text, keywords)
    return (OCR_ERROR_FOUND if hits else OCR_NO_ERROR), hits

def scan_error_text(text: str, keywords=ERROR_KEYWORDS) -> list[dict]:
    """Find error-ish lines in OCR text. Returns [{keyword, line}] bounded to 5.

    Whitespace-tolerant, because a missed keyword here is not cosmetic: it feeds
    `ocr_error`, which feeds retention's P_EVENT promotion, so the frame showing
    an on-screen error stops being held for examination and becomes evictable.
    A keyword lost to OCR therefore DELETES the evidence, not just the mention.

    MEASURED across 192 rendered error lines (4 fonts x 3 sizes x 2 blur levels):
    OCR destroyed the keyword in 1 case, and it did so by SPLITTING the word —
    "Unhandled" came back as "Unhand led" — not by substituting characters. A
    plain substring test misses that; collapsing intra-word separators catches it
    at no cost to precision (verified: no new matches on clean negative text).
    """
    if not text:
        return []
    hits: list[dict] = []
    for line in text.splitlines():
        low = line.lower()
        squashed = _SPLIT_RE.sub("", low)
        for kw in keywords:
            if kw in low or kw.replace(" ", "") in squashed:
                hits.append({"keyword": kw, "line": line.strip()[:200]})
                break
        if len(hits) >= 5:
            break
    return hits


# ── Is this frame text-bearing? ───────────────────────────────────────────────

#: Above this fraction of strongly-edged pixels a frame is treated as carrying
#: text/UI detail that a downscale would destroy. MEASURED: a 13px-monospace
#: debug UI scores 0.0043, a pictorial game frame 0.0013 — a 3.3x separation.
TEXT_DENSITY_THRESHOLD = float(
    os.environ.get("AGENTVISION_TEXT_DENSITY_THRESHOLD", "0.0025"))


def text_density(image_path: str) -> float:
    """Fraction of pixels sitting on a strong edge. -1.0 if it cannot be measured.

    MUST be computed at CAPTURE resolution. Measured trap: the same frame scored
    0.0043 at 2560x1440 and 0.0091 after a downscale to 640x360, because
    resampling aliases fine detail into harder edges. Run it post-resize and it
    reports the opposite of the truth.
    """
    try:
        from PIL import Image, ImageFilter
        with Image.open(image_path) as im:
            g = im.convert("L")
            edges = g.filter(ImageFilter.FIND_EDGES)
            hist = edges.histogram()
            strong = sum(hist[60:])
            total = max(1, g.width * g.height)
            return strong / total
    except Exception:
        return -1.0


def serve_plan(image_path: str, width: int, height: int,
               density: float | None = None) -> dict:
    """Decide what resolution to hand an agent for this frame, and say why.

    A blanket downscale was rejected on evidence. MEASURED on a realistic debug
    UI: critical strings recovered by OCR went 4/8 at full resolution to 0/8 at
    1280 and 0/8 at 640 — at 640 the entire readable output was 34 corrupted
    characters. Serving that to save tokens would buy cost with a false read,
    which is the one trade this project must never make.

    But a pictorial frame carries no such risk and costs 4784 tokens against 299
    — 16x — so refusing to ever downscale is equally wrong. Hence: decide per
    frame, and ALWAYS declare what was served, so a downscale is visible to the
    reader rather than silent.
    """
    d = text_density(image_path) if density is None else float(density)
    full = visual_tokens(width, height)
    small_w = 640
    small_h = max(1, int(height * small_w / max(1, width)))
    small = visual_tokens(small_w, small_h)
    if d < 0:
        return {"serve": "full", "reason": "text density could not be measured — "
                "defaulting to full resolution rather than risk losing detail",
                "tokens_full": full, "text_density": d}
    if d >= TEXT_DENSITY_THRESHOLD:
        return {"serve": "full",
                "reason": (f"text-bearing (edge density {d:.4f} >= "
                           f"{TEXT_DENSITY_THRESHOLD}); a downscale would destroy "
                           f"on-screen text — measured 4/8 -> 0/8 recoverable "
                           f"strings at half size"),
                "tokens_full": full, "text_density": d}
    if width <= small_w:
        # Already at or below the downscale target — there is nothing to save,
        # and claiming "text-bearing" here (as this branch used to) was a false
        # arithmetic statement whenever the frame was in fact pictorial.
        return {"serve": "full",
                "reason": (f"already {width}px wide (<= the {small_w}px downscale "
                           f"target), so a downscale would save nothing"),
                "tokens_full": full, "text_density": d}
    return {"serve": "downscaled", "width": small_w, "height": small_h,
            "reason": (f"pictorial (edge density {d:.4f} < "
                       f"{TEXT_DENSITY_THRESHOLD}); no fine text to lose"),
            "tokens_full": full, "tokens_served": small,
            "saves_tokens": max(0, full - small), "text_density": d}
