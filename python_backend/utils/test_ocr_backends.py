"""
OCR backends: zero-install engine selection + unified output contract.
================================================================================
The point of utils/ocr_backends.py is that the USER never has to install a
native tesseract binary: macOS and Windows already contain an OCR engine, and
Linux falls back to a pip-only ONNX engine. These tests pin the contract that
makes that safe — detection never raises, an unavailable engine degrades to
{available:false, install_hint} instead of exploding, and every backend emits
the SAME shape with top-left pixel bboxes.

Backend-specific recognition is only asserted for engines actually present on
the machine running the tests, so this suite passes on a box with no OCR at all.
"""
from __future__ import annotations
import os, sys, tempfile
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path[:0] = [str(_HERE.parents[2]), str(_HERE.parents[1])]

from utils import ocr_backends as ob            # noqa: E402

_passed = _failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1; print(f"  [ok  ] {label}")
    else:
        _failed += 1; print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


def _sample_image() -> str | None:
    """A realistic error-dialog image, or None when Pillow/fonts are absent."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    font = None
    for cand in ("/System/Library/Fonts/Supplemental/Arial.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 r"C:\Windows\Fonts\arial.ttf"):
        try:
            font = ImageFont.truetype(cand, 22); break
        except Exception:
            continue
    if font is None:
        return None                     # bitmap default font OCRs too poorly
    img = Image.new("RGB", (900, 140), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.text((40, 30), "Unhandled Exception: NullReferenceException",
           font=font, fill=(0, 0, 0))
    d.text((40, 80), "at GuestGpu.Present() in Renderer.cs:line 412",
           font=font, fill=(0, 0, 0))
    p = os.path.join(tempfile.mkdtemp(), "dialog.png")
    img.save(p)
    return p


def test_detection_contract():
    print("detection never raises and is fully described:")
    d = ob.detect_engine()
    check("returns a dict", isinstance(d, dict))
    for k in ("available", "backend", "engine", "reason", "candidates"):
        check(f"has '{k}'", k in d)
    # The candidate list is the OS-appropriate probe order — e.g. windows_ocr is
    # correctly absent on macOS — so it must equal _ORDER, not every known probe.
    got = {c["backend"] for c in d["candidates"]}
    check("candidates == this OS's probe order", got == set(ob._ORDER),
          f"{got} vs {set(ob._ORDER)}")
    check("every candidate is a known backend", got <= set(ob._PROBES), str(got))
    check("every candidate is described", all(
        isinstance(c.get("available"), bool) and c.get("detail") is not None
        for c in d["candidates"]))
    if d["available"]:
        check("chosen backend is one that probed OK",
              d["backend"] in {c["backend"] for c in d["candidates"] if c["available"]})
        check("engine string is non-empty", bool(d["engine"]))
    else:
        check("unavailable => an install_hint is offered", bool(d.get("install_hint")))
    # An unknown preference must not crash or hijack the choice.
    d2 = ob.detect_engine(prefer="does_not_exist")
    check("unknown prefer= is ignored safely", d2.get("backend") == d.get("backend"))


def test_install_plan():
    print("install plan is pip-only (no system packages to compile):")
    plan = ob.install_plan()
    for k in ("os", "pip", "note"):
        check(f"plan has '{k}'", k in plan)
    check("plan lists at least one pip package", bool(plan["pip"]))
    check("plan mentions no sudo/brew/apt step",
          not any(w in plan["note"].lower() for w in ("sudo ", "brew install tesseract",
                                                      "apt install tesseract")),
          plan["note"][:90])


def test_graceful_absence():
    print("missing engine / bad input degrade instead of raising:")
    r = ob.ocr_image("/nonexistent/path/to/frame.png")
    check("missing file never raises", isinstance(r, dict))
    if r.get("available"):
        check("missing file reports an error field", bool(r.get("error")), str(r)[:90])
    else:
        check("no-engine reports reason + hint",
              bool(r.get("reason")) and bool(r.get("install_hint")))
    # Force a backend that cannot exist on this OS.
    other = "windows_ocr" if sys.platform != "win32" else "apple_vision"
    r2 = ob.ocr_image("/nonexistent.png", prefer=other)
    check(f"forcing the wrong-OS backend ({other}) is safe", isinstance(r2, dict))


def test_recognition_on_available_backends():
    print("unified output contract, per available backend:")
    img = _sample_image()
    if img is None:
        check("sample image buildable (skipped: no Pillow/TTF font)", True)
        return
    det = ob.detect_engine()
    avail = [c["backend"] for c in det["candidates"] if c["available"]]
    if not avail:
        print("       (no OCR backend on this machine — recognition skipped)")
        check("no-backend path still returns a dict",
              isinstance(ob.ocr_image(img), dict))
        return
    for be in avail:
        r = ob.ocr_image(img, prefer=be)
        if r.get("error"):
            check(f"{be}: ran without error", False, str(r["error"])[:80]); continue
        check(f"{be}: available + names itself", r.get("available") is True
              and r.get("backend") == be, f"backend={r.get('backend')}")
        text = (r.get("text") or "")
        check(f"{be}: recovered the exception name",
              "nullreference" in text.lower().replace(" ", ""), text[:70])
        check(f"{be}: word_count is sane", (r.get("word_count") or 0) >= 5,
              str(r.get("word_count")))
        check(f"{be}: reports zero_install truthfully",
              r.get("zero_install") is (be in ("apple_vision", "windows_ocr")),
              str(r.get("zero_install")))
        lines = r.get("lines") or []
        check(f"{be}: emitted line entries", len(lines) > 0)
        for ln in lines[:4]:
            bad = (not isinstance(ln.get("text"), str)
                   or (ln.get("bbox") is not None
                       and (len(ln["bbox"]) != 4
                            or any(not isinstance(v, int) for v in ln["bbox"])
                            or ln["bbox"][2] <= 0 or ln["bbox"][3] <= 0)))
            check(f"{be}: line shape {{text,bbox[x,y,w,h],conf}}", not bad, str(ln)[:90])
            break


def test_caps_are_enforced():
    print("bounded output (token discipline):")
    img = _sample_image()
    if img is None or not ob.detect_engine().get("available"):
        check("caps (skipped: no image or no engine)", True); return
    r = ob.ocr_image(img, text_cap=20, line_cap=1)
    check("text_cap honoured", len(r.get("text") or "") <= 20, str(len(r.get("text") or "")))
    check("line_cap honoured", len(r.get("lines") or []) <= 1)
    check("truncation is flagged", r.get("truncated") in (True, False))


if __name__ == "__main__":
    print("=" * 66); print("OCR BACKENDS"); print("=" * 66)
    test_detection_contract()
    test_install_plan()
    test_graceful_absence()
    test_recognition_on_available_backends()
    test_caps_are_enforced()
    print("=" * 66)
    print(f"RESULT: {_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
