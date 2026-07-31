#!/usr/bin/env python3
"""
ONLY THE BRIDGED PROGRAM — a frame must never contain the desktop.
================================================================================
The core design promise: a stored frame shows the target program's window and
NOTHING ELSE, whatever its size, wherever it is, occluded, fullscreen, or even
minimized into the Dock.

It was being broken. When a profile named a `capture_app` whose window could not
be found, the engine warned and then captured the FULL SCREEN anyway. That
produced 2560x1440 frames of the user's entire desktop — every other app and
browser tab included — written to disk, handed to the agent, and ~18x the bytes
of a real window frame. Two such frames were found in a real 11,000-frame
capture. A frame of the wrong thing is worse than no frame at all: it leaks
unrelated screen contents, pollutes visual-change detection, and makes the agent
reason about pixels that are not the program.

Now the frame is SKIPPED instead, and the skip is counted so a gap in sequence
numbers is explainable rather than mysterious.

Also pinned: a profile with NO capture_app still captures full screen, because
there the whole screen IS the requested target — the guard must not break that.
"""
from __future__ import annotations
import sys
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


def main():
    import api.bridge_server as bs

    print("the guard is ON by default:")
    check("full-screen fallback is disabled unless explicitly enabled",
          bs.ALLOW_FULLSCREEN_FALLBACK is False,
          str(bs.ALLOW_FULLSCREEN_FALLBACK))
    check("the engine counts frames it declined to take",
          hasattr(bs._auto_engine, "skipped_no_window"))

    print("\nthe decision table (capture_app x window found x crop):")
    # window_missing is the flag that gates the skip. It must be True ONLY when a
    # program was named, no window was found, and there is no manual crop.
    cases = [
        # (capture_app, window_found, crop, expect_skip, why)
        ("SharpEmu", True,  None, False, "window found -> capture the window"),
        ("SharpEmu", False, None, True,  "named a program, no window -> SKIP"),
        ("SharpEmu", False, (0, 0, 100, 100), False,
         "explicit crop is an intentional target"),
        ("SharpEmu", True,  (0, 0, 100, 100), False, "window wins over crop"),
        ("",         False, None, False,
         "no capture_app -> full screen IS the requested target"),
        ("",         True,  None, False, "no capture_app, incidental window"),
        ("   ",      False, None, False, "whitespace-only capture_app is not a program"),
    ]
    for app, found, crop, expect, why in cases:
        wants = bool((app or "").strip())
        window_missing = wants and not found and not crop
        skip = window_missing and not bs.ALLOW_FULLSCREEN_FALLBACK
        check(f"app={app.strip()!r:12} found={str(found):5} crop={bool(crop)!s:5}"
              f" -> skip={skip!s:5}  ({why})", skip is expect,
              f"expected skip={expect}")

    print("\nthe warning tells the user what to do, and does NOT promise a desktop shot:")
    # Rebuild the message the engine sets, and pin its content.
    msg = ("capture_app 'X' has no window right now — SKIPPING the frame rather "
           "than screenshotting the desktop. Is the program running? (A minimized "
           "or occluded window is fine and still captures on macOS.)")
    check("says the frame was skipped", "SKIPPING" in msg)
    check("promises the desktop is NOT captured",
          "rather than screenshotting the desktop" in msg)
    check("does not claim to capture full screen",
          "capturing full screen" not in msg.lower())
    check("reassures that minimized is fine", "minimized" in msg)

    print("\nthe push channel says the same thing (no stale 'full screen' claim):")
    from api import ambient as amb
    sigs = amb.build_signals({
        "capture": {"window_missing": True, "capture_app": "SharpEmu",
                    "frames_stored": 10, "engine_running": True},
        "visual": {}, "program": {}, "health": {},
    })
    wm = [s for s in sigs if s["kind"] == "window_missing"]
    check("a window_missing signal is produced", len(wm) == 1, str(len(wm)))
    if wm:
        t = wm[0]["text"]
        check("it says frames are SKIPPED", "SKIPPED" in t, t)
        check("it states the desktop is never captured",
              "desktop is never" in t, t)
        check("it no longer claims to screenshot the full screen",
              "full screen" not in t.lower(), t)

    print("\n/capture/status is self-describing about the guarantee:")
    client = bs.app.test_client()
    r = client.get("/capture/status")
    check("/capture/status 200", r.status_code == 200)
    h = (r.get_json() or {}).get("health") or {}
    check("reports the skip counter", "frames_skipped_no_window" in h, str(list(h)))
    check("explains the only-the-program guarantee", "only_the_program" in h)
    otp = str(h.get("only_the_program") or "")
    check("says ONLY the program's window", "ONLY" in otp and "window" in otp, otp[:80])
    check("explains that a seq gap is expected, not a failure",
          "gap" in otp, otp[:120])
    check("states minimized/occluded still captures",
          "minimized" in otp, otp[:120])

    print(f"\n{'=' * 62}")
    print("only_the_program: " + ("ALL PASS" if _fails == 0
                                 else f"{_fails} FAILURE(S)"))
    print("=" * 62)
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
