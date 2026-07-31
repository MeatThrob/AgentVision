"""
Overlay Renderer
----------------
Keeps the screenshot clean and readable.

What gets burned onto the PNG:
  - A single slim stamp bar at the very bottom edge (18px tall):
      frame ID | timestamp | connected program | key status flags
  - Nothing else. The image is preserved for visual inspection.

The full diagnostic data lives in the .json sidecar next to the image.
Claude reads both: image for visual context, JSON for machine state.
"""
from __future__ import annotations
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


# ── Font ───────────────────────────────────────────────────────────────────────

def _mono_paths() -> list[str]:
    # Platform-appropriate monospace fonts (Consolas/Courier on Windows,
    # Menlo/Monaco on macOS, DejaVu on Linux).
    try:
        from utils.platform_shim import mono_font_paths
        return mono_font_paths()
    except Exception:
        try:
            from python_backend.utils.platform_shim import mono_font_paths
            return mono_font_paths()
        except Exception:
            return ["/System/Library/Fonts/Menlo.ttc"]


def _load_mono(size: int):
    for path in _mono_paths():
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()

_STAMP_FONT = _load_mono(12)


# ── Colors ─────────────────────────────────────────────────────────────────────

_BAR_BG      = (10,  12,  20,  220)   # near-black, semi-transparent
_TEXT_NORMAL = (200, 220, 255, 255)   # light blue-white
_TEXT_OK     = ( 80, 220, 120, 255)   # green
_TEXT_WARN   = (255, 200,  60, 255)   # amber
_TEXT_ERROR  = (255,  90,  90, 255)   # red
_TEXT_DIM    = (120, 130, 150, 255)   # dimmed


# ── Public ─────────────────────────────────────────────────────────────────────

def burn_overlay(image_path: str, frame) -> str:
    """
    Burns a minimal stamp bar onto the bottom of the screenshot.
    Saves as a new file:  shot_00001.png  →  shot_00001_annotated.png
    Returns the output path.
    """
    img  = Image.open(image_path).convert("RGBA")
    W, H = img.size

    BAR_H   = 20
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)

    # Stamp bar background
    draw.rectangle([0, H - BAR_H, W, H], fill=_BAR_BG)

    # Build stamp text
    stamp = _build_stamp(frame)

    # Left side: identity + timing
    draw.text((6, H - BAR_H + 4), stamp["left"],
              font=_STAMP_FONT, fill=_TEXT_NORMAL)

    # Center: key status flags
    center_x = W // 2
    cx_text  = stamp["center"]
    try:
        cw = draw.textlength(cx_text, font=_STAMP_FONT)
    except Exception:
        cw = len(cx_text) * 7
    draw.text((center_x - cw // 2, H - BAR_H + 4),
              cx_text, font=_STAMP_FONT,
              fill=_status_color(frame))

    # Right side: program name + running state
    right_text = stamp["right"]
    try:
        rw = draw.textlength(right_text, font=_STAMP_FONT)
    except Exception:
        rw = len(right_text) * 7
    draw.text((W - rw - 6, H - BAR_H + 4),
              right_text, font=_STAMP_FONT, fill=_TEXT_DIM)

    combined = Image.alpha_composite(img, overlay).convert("RGB")
    out_path = image_path.replace(".png", "_annotated.png")
    combined.save(out_path, "PNG", optimize=False)
    return out_path


# ── Stamp builder ──────────────────────────────────────────────────────────────

def _build_stamp(frame) -> dict[str, str]:
    # Left: frame ID + timestamp
    left = f"#{frame.sequence:05d}  {frame.timestamp}"

    # Center: most important status at a glance
    parts = []

    # Anomaly (highest priority)
    if frame.anomaly.detected:
        sev = frame.anomaly.severity.upper()
        parts.append(f"[{sev}: {frame.anomaly.type.replace('_',' ')}]")

    # Test status (field may not exist on all frame versions)
    t = getattr(frame, "tests", None)
    if t is not None:
        total = getattr(t, "pass_count", 0) + getattr(t, "fail_count", 0)
        if total > 0:
            if getattr(t, "fail_count", 0) == 0:
                parts.append(f"TESTS ✓{t.pass_count}")
            else:
                parts.append(f"TESTS ✓{t.pass_count} ✗{t.fail_count}")

    # Error
    if frame.error and frame.error.message:
        parts.append(f"ERR: {frame.error.file}:{frame.error.line}")

    # Confidence
    conf = int(frame.agent_eval.confidence * 100)
    parts.append(f"CONF:{conf}%")

    # Stats summary (program-specific)
    stats = getattr(frame.program, "stats", {})
    if stats:
        stat_str = "  ".join(f"{k}:{v}" for k, v in list(stats.items())[:3])
        parts.append(stat_str)

    center = "  |  ".join(parts)

    # Right: connected program
    prog    = getattr(frame, "_program_name", "")
    running = getattr(frame, "_program_running", None)
    if prog:
        state = " ●" if running else " ○"
        right = f"{prog}{state}  →  {Path(frame.image_file).stem}.json"
    else:
        right = f"→ {Path(frame.image_file).stem}.json"

    return {"left": left, "center": center, "right": right}


def _status_color(frame) -> tuple:
    if frame.anomaly.detected:
        sev = frame.anomaly.severity
        return _TEXT_ERROR if sev in ("critical", "high") else _TEXT_WARN
    if frame.error:
        return _TEXT_WARN
    if frame.agent_eval.confidence > 0.75:
        return _TEXT_OK
    if frame.agent_eval.confidence < 0.45:
        return _TEXT_WARN
    return _TEXT_NORMAL
