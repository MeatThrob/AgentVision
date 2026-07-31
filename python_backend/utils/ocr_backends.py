"""
ocr_backends — OCR that works WITHOUT asking the user to install a native binary.
================================================================================

WHY THIS EXISTS
---------------
Reading the text that is ON SCREEN is one of AgentVision's cheapest wins: an
error dialog read as JSON costs ~30 tokens instead of ~4800 for the image. But
the obvious implementation (`pytesseract`) is a thin wrapper around the
**tesseract C++ binary**, which is NOT pip-installable — so OCR silently stayed
dark for anyone who hadn't run `brew install tesseract` / `apt install
tesseract-ocr`. "Just bundle tesseract" is worse than it sounds: it means
shipping per-platform native builds (macOS arm64 + x86_64, Windows x64, Linux
glibc + musl) plus ~15-30 MB of `tessdata` per language — well over 100 MB of
binaries in the repo, with a separate build/signing story per OS.

The better answer: **macOS and Windows already ship a good OCR engine inside the
operating system.** Use those first. Nothing to install, nothing to bundle.

BACKENDS (auto-detected, best available wins)
---------------------------------------------
  tesseract     pytesseract + the tesseract binary. FASTEST when present
                (~245 ms vs ~1100 ms on a 1000x320 dialog, measured), so it is
                preferred when already installed — but never required.
  apple_vision  Apple's Vision framework (VNRecognizeTextRequest), built into
                macOS 10.15+. Reached through pyobjc, which is a pure pip
                install. ZERO system install. Measured identical accuracy to
                tesseract on realistic UI text (3/3 lines recovered).
  windows_ocr   Windows.Media.Ocr (WinRT), built into Windows 10/11. Reached
                through the `winsdk` pip wheel. ZERO system install.
  rapidocr      RapidOCR on onnxruntime — pip wheels only (no system packages),
                bundles its own ONNX models. The Linux fallback, where the OS
                has no built-in OCR engine.

Every backend is imported LAZILY inside its own probe, so importing this module
can never fail because of a missing optional dependency, and a broken backend
can never take down the others.

CONTRACT
--------
    detect_engine(prefer=None) -> {available, engine, reason, candidates, ...}
    ocr_image(path, text_cap, line_cap, prefer=None)
        -> {available, engine, text, lines:[{text,bbox,conf}], word_count}
           or {available: False, reason, install_hint} — never raises.

`bbox` is always [x, y, w, h] in TOP-LEFT pixel coordinates, whatever the
backend's native convention (Vision reports normalized bottom-left rects; those
are converted here so callers never care which engine ran).
"""
from __future__ import annotations

import os
import platform
import sys

_IS_MAC = sys.platform == "darwin"
_IS_WINDOWS = sys.platform.startswith("win")
_IS_LINUX = sys.platform.startswith("linux")

# Preference order per OS: the built-in OS engine FIRST, then tesseract, then
# the pip-only fallback.
#
# This used to put tesseract first, justified in a comment as "~4x faster".
# MEASURED on an M2 against a real 1392x860 game frame, that was backwards:
#
#     tesseract     median 563 ms   "Hew games", ".", "Sara"   (mangled)
#     apple_vision  median 152 ms   "New game", "Dreaming Sarah" (correct)
#
# Apple Vision was 3.7x FASTER and strictly more accurate — tesseract misread a
# menu item and the game logo. The OS engines are also what make OCR zero-install,
# which is the whole point: the user must never have to build a native binary.
# tesseract stays as a cross-platform fallback and remains selectable via
# prefer= / AGENTVISION_OCR_BACKEND for anyone who has tuned traineddata.
_ORDER = (["apple_vision", "tesseract", "rapidocr"] if _IS_MAC else
          ["windows_ocr", "tesseract", "rapidocr"] if _IS_WINDOWS else
          ["tesseract", "rapidocr"])


# ── what to install so OCR works here (used by `agentvision install-ocr`) ──────

def install_plan() -> dict:
    """The pip-only route to working OCR on THIS machine. No system packages,
    no bundled binaries. Returns {os, pip:[…], note}."""
    if _IS_MAC:
        return {"os": "macOS",
                "pip": ["pyobjc-framework-Vision"],
                "note": ("Uses Apple's Vision framework, which ships with "
                         "macOS 10.15+ — nothing to install system-wide. It is "
                         "also the PREFERRED engine: measured on an M2 it was "
                         "3.7x faster than tesseract (152ms vs 563ms median) "
                         "and read stylised UI text correctly where tesseract "
                         "misread it.")}
    if _IS_WINDOWS:
        return {"os": "Windows",
                "pip": ["winsdk"],
                "note": ("Uses Windows.Media.Ocr, built into Windows 10/11 — "
                         "nothing to install system-wide.")}
    return {"os": "Linux",
            "pip": ["rapidocr-onnxruntime"],
            "note": ("Linux has no built-in OCR engine, so this uses RapidOCR "
                     "on onnxruntime — pip wheels only, no system packages. "
                     "If your distro already provides tesseract "
                     "(pacman -S tesseract tesseract-data-eng) it is preferred.")}


# ── backend probes ────────────────────────────────────────────────────────────

def _probe_tesseract() -> tuple[bool, str, str]:
    try:
        import pytesseract
    except Exception as ex:
        return False, "", f"pytesseract not installed ({ex})"
    try:
        ver = pytesseract.get_tesseract_version()
        return True, f"tesseract {ver} (via pytesseract)", ""
    except Exception as ex:
        return False, "", f"tesseract binary not found/usable ({ex})"


def _probe_apple_vision() -> tuple[bool, str, str]:
    if not _IS_MAC:
        return False, "", "not macOS"
    try:
        import Quartz          # noqa: F401
        import Vision          # noqa: F401
        from Foundation import NSURL   # noqa: F401
    except Exception as ex:
        return False, "", (f"pyobjc Vision bindings missing ({ex}) — "
                           "pip install pyobjc-framework-Vision")
    return True, f"Apple Vision (macOS {platform.mac_ver()[0]}, built-in)", ""


def _probe_windows_ocr() -> tuple[bool, str, str]:
    if not _IS_WINDOWS:
        return False, "", "not Windows"
    try:
        from winsdk.windows.media.ocr import OcrEngine   # noqa: F401
    except Exception as ex:
        return False, "", (f"winsdk not installed ({ex}) — pip install winsdk")
    try:
        from winsdk.windows.media.ocr import OcrEngine
        if OcrEngine.try_create_from_user_profile_languages() is None:
            return False, "", ("Windows OCR has no language pack for the "
                               "current user profile languages")
    except Exception as ex:
        return False, "", f"Windows OCR unavailable ({ex})"
    return True, "Windows.Media.Ocr (built-in)", ""


def _probe_rapidocr() -> tuple[bool, str, str]:
    try:
        from rapidocr_onnxruntime import RapidOCR   # noqa: F401
    except Exception as ex:
        return False, "", (f"rapidocr-onnxruntime not installed ({ex}) — "
                           "pip install rapidocr-onnxruntime")
    return True, "RapidOCR (onnxruntime, bundled models)", ""


_PROBES = {
    "tesseract": _probe_tesseract,
    "apple_vision": _probe_apple_vision,
    "windows_ocr": _probe_windows_ocr,
    "rapidocr": _probe_rapidocr,
}


def detect_engine(prefer: str | None = None) -> dict:
    """Pick the best available backend. `prefer` (or $AGENTVISION_OCR_ENGINE)
    forces one by name and is honoured when that backend actually works.
    Never raises."""
    prefer = (prefer or os.environ.get("AGENTVISION_OCR_ENGINE") or "").strip().lower()
    order = list(_ORDER)
    if prefer in _PROBES:
        order = [prefer] + [n for n in order if n != prefer]

    candidates, chosen, chosen_desc = [], "", ""
    for name in order:
        try:
            ok, desc, why = _PROBES[name]()
        except Exception as ex:                       # pragma: no cover
            ok, desc, why = False, "", f"probe raised ({ex})"
        candidates.append({"backend": name, "available": ok,
                           "detail": desc or why})
        if ok and not chosen:
            chosen, chosen_desc = name, desc

    if chosen:
        return {"available": True, "backend": chosen, "engine": chosen_desc,
                "reason": "", "candidates": candidates}
    plan = install_plan()
    return {"available": False, "backend": "", "engine": "",
            "reason": "; ".join(c["detail"] for c in candidates if c["detail"])
                      or "no OCR backend available",
            "candidates": candidates,
            "install_hint": (f"{plan['os']}: pip install "
                             f"{' '.join(plan['pip'])} — {plan['note']}"),
            "install_plan": plan}


# ── recognition ───────────────────────────────────────────────────────────────

def _ocr_tesseract(path: str, line_cap: int) -> tuple[str, list]:
    import pytesseract
    from PIL import Image
    img = Image.open(path)
    text = pytesseract.image_to_string(img)
    lines: list[dict] = []
    try:
        from pytesseract import Output
        d = pytesseract.image_to_data(img, output_type=Output.DICT)
        n = len(d.get("text") or [])
        for i in range(n):
            t = (d["text"][i] or "").strip()
            if not t:
                continue
            conf = d.get("conf", [])[i] if i < len(d.get("conf", [])) else -1
            try:
                conf = round(float(conf) / 100.0, 2)
            except Exception:
                conf = None
            lines.append({"text": t[:200],
                          "bbox": [d["left"][i], d["top"][i],
                                   d["width"][i], d["height"][i]],
                          "conf": conf})
            if len(lines) >= line_cap:
                break
    except Exception:
        pass
    return text, lines


def _ocr_apple_vision(path: str, line_cap: int) -> tuple[str, list]:
    import Quartz
    import Vision
    from Foundation import NSURL

    src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(path), None)
    if src is None:
        raise RuntimeError("could not read image")
    cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if cg is None:
        raise RuntimeError("could not decode image")
    W = float(Quartz.CGImageGetWidth(cg) or 0)
    H = float(Quartz.CGImageGetHeight(cg) or 0)

    req = Vision.VNRecognizeTextRequest.alloc().init()
    try:
        req.setRecognitionLevel_(0)     # 0 = accurate
        req.setUsesLanguageCorrection_(True)
    except Exception:
        pass
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
    handler.performRequests_error_([req], None)

    lines: list[dict] = []
    for obs in (req.results() or []):
        try:
            cands = obs.topCandidates_(1)
            if not cands or not len(cands):
                continue
            best = cands[0]
            txt = str(best.string() or "").strip()
            if not txt:
                continue
            # Vision boundingBox: normalized, origin BOTTOM-left → top-left px.
            bbox = None
            try:
                r = obs.boundingBox()
                x = r.origin.x * W
                w = r.size.width * W
                h = r.size.height * H
                y = (1.0 - r.origin.y - r.size.height) * H
                bbox = [int(x), int(y), int(w), int(h)]
            except Exception:
                pass
            conf = None
            try:
                conf = round(float(best.confidence()), 2)
            except Exception:
                pass
            lines.append({"text": txt[:200], "bbox": bbox, "conf": conf})
            if len(lines) >= line_cap:
                break
        except Exception:
            continue
    return "\n".join(l["text"] for l in lines), lines


def _ocr_windows_ocr(path: str, line_cap: int) -> tuple[str, list]:
    import asyncio
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage import FileAccessMode, StorageFile

    async def _run():
        f = await StorageFile.get_file_from_path_async(os.path.abspath(path))
        stream = await f.open_async(FileAccessMode.READ)
        dec = await BitmapDecoder.create_async(stream)
        bmp = await dec.get_software_bitmap_async()
        eng = OcrEngine.try_create_from_user_profile_languages()
        if eng is None:
            raise RuntimeError("no Windows OCR language pack")
        return await eng.recognize_async(bmp)

    result = asyncio.run(_run())
    lines: list[dict] = []
    for ln in (result.lines or []):
        txt = str(getattr(ln, "text", "") or "").strip()
        if not txt:
            continue
        bbox = None
        try:
            rects = [w.bounding_rect for w in (ln.words or [])]
            if rects:
                x0 = min(r.x for r in rects); y0 = min(r.y for r in rects)
                x1 = max(r.x + r.width for r in rects)
                y1 = max(r.y + r.height for r in rects)
                bbox = [int(x0), int(y0), int(x1 - x0), int(y1 - y0)]
        except Exception:
            pass
        lines.append({"text": txt[:200], "bbox": bbox, "conf": None})
        if len(lines) >= line_cap:
            break
    return "\n".join(l["text"] for l in lines), lines


def _ocr_rapidocr(path: str, line_cap: int) -> tuple[str, list]:
    from rapidocr_onnxruntime import RapidOCR
    engine = RapidOCR()
    res, _elapsed = engine(path)
    lines: list[dict] = []
    for item in (res or []):
        try:
            box, txt, conf = item[0], str(item[1] or "").strip(), item[2]
            if not txt:
                continue
            bbox = None
            try:
                xs = [p[0] for p in box]; ys = [p[1] for p in box]
                bbox = [int(min(xs)), int(min(ys)),
                        int(max(xs) - min(xs)), int(max(ys) - min(ys))]
            except Exception:
                pass
            lines.append({"text": txt[:200], "bbox": bbox,
                          "conf": round(float(conf), 2) if conf is not None else None})
            if len(lines) >= line_cap:
                break
        except Exception:
            continue
    return "\n".join(l["text"] for l in lines), lines


_RECOGNIZERS = {
    "tesseract": _ocr_tesseract,
    "apple_vision": _ocr_apple_vision,
    "windows_ocr": _ocr_windows_ocr,
    "rapidocr": _ocr_rapidocr,
}


def ocr_image(image_path: str, text_cap: int = 8000, line_cap: int = 300,
              prefer: str | None = None) -> dict:
    """OCR one image into bounded JSON using the best available backend.
    Never raises: an unavailable engine or a decode failure comes back as
    {available: False, reason, install_hint} / {..., error}."""
    det = detect_engine(prefer)
    if not det.get("available"):
        return {"available": False, "reason": det.get("reason", ""),
                "install_hint": det.get("install_hint", ""),
                "candidates": det.get("candidates", [])}
    if not image_path or not os.path.exists(image_path):
        return {"available": True, "engine": det["engine"],
                "backend": det["backend"], "error": "image not found"}
    try:
        text, lines = _RECOGNIZERS[det["backend"]](image_path, line_cap)
    except Exception as ex:
        return {"available": True, "engine": det["engine"],
                "backend": det["backend"],
                "error": f"OCR failed ({type(ex).__name__}: {ex})"[:220]}
    text = (text or "").strip()
    return {
        "available": True,
        "engine": det["engine"],
        "backend": det["backend"],
        "text": text[:text_cap],
        "truncated": len(text) > text_cap,
        "lines": lines[:line_cap],
        "word_count": len(text.split()),
        "zero_install": det["backend"] in ("apple_vision", "windows_ocr"),
    }
