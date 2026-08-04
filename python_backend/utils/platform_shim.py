"""
platform_shim — every OS-specific operation AgentVision needs, in one place.
================================================================================

AgentVision was originally macOS-only. It reached the OS in five places:

  1. Screen capture        (the `screencapture` CLI)
  2. Window enumeration    (Quartz  CGWindowListCopyWindowInfo)
  3. Front-app / window    (AppleScript via `osascript`)
  4. Global input tap      (Quartz  CGEventTap — see input_daemon.py)
  5. Misc shell            (temp paths, `open`, process liveness)

This module abstracts 1, 2, 3, and 5 behind small functions that dispatch on
`sys.platform`, so the SAME source tree runs on Windows, macOS AND Linux.
(4 lives in input_daemon.py, which has its own per-OS backend.)

Design rules:
  • Every function degrades gracefully — it returns a sensible empty value
    instead of raising, EXCEPT `capture_frame`, whose failure the bridge must
    see (it raises RuntimeError, matching the original screencapture path).
  • The macOS behaviour is preserved byte-for-byte; Windows and Linux are the
    newer paths.
  • Per-OS deps (pywin32, mss, python-xlib) are imported lazily so importing
    this module never fails on a machine that is missing one of them.
  • Linux additionally dispatches on the DISPLAY SESSION (X11 vs Wayland vs
    headless — see linux_session_type): X11 has global window enumeration +
    mss capture; Wayland forbids both (compositor security) and uses `grim`
    or the xdg-desktop-portal Screenshot call instead.

Windows dependencies (see requirements-windows.txt):
    pip install mss pywin32
Linux dependencies (see requirements-linux.txt):
    pip install mss python-xlib evdev
    system (Wayland): grim, or xdg-desktop-portal + a portal backend
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ── OS detection ───────────────────────────────────────────────────────────────
IS_WINDOWS = sys.platform.startswith("win")
IS_MAC     = sys.platform == "darwin"
IS_LINUX   = sys.platform.startswith("linux")

# ── DPI awareness (Windows) ──────────────────────────────────────────────────
# python.exe is DPI-UNAWARE by default, so GetWindowRect returns virtualized
# (scaled-down) coordinates on any display scaled above 100% — while mss grabs
# physical pixels (it makes the process DPI-aware itself, but only when the
# first mss.mss() is constructed, i.e. AFTER we may already have read window
# rects, and never in processes that only enumerate windows). Making every
# AgentVision process DPI-aware at import time keeps GetWindowRect, crop rects,
# Tk screen metrics and mss all in the same physical-pixel coordinate space.
if IS_WINDOWS:
    try:
        import ctypes as _ctypes
        try:
            # 2 = PER_MONITOR_DPI_AWARE (Win 8.1+)
            _ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            _ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ── Temp / runtime file locations ───────────────────────────────────────────────
# The mac build hard-coded /tmp. On Windows that folder does not exist; use the
# platform temp dir (%TEMP% / %TMP% on Windows, /tmp on POSIX) so the daemon
# pidfile and log land somewhere writable on every OS.

def temp_dir() -> Path:
    return Path(tempfile.gettempdir())


def daemon_pid_file() -> Path:
    return temp_dir() / "agentvision_daemon.pid"


def daemon_log_file() -> Path:
    return temp_dir() / "agentvision_daemon.log"


# ── Process liveness (replaces os.kill(pid, 0), which is unreliable on Win) ──────

def pid_alive(pid: int | None) -> bool:
    """True if a process with this PID exists and is running."""
    if not pid:
        return False
    try:
        import psutil
        return psutil.pid_exists(int(pid)) and \
            psutil.Process(int(pid)).status() != psutil.STATUS_ZOMBIE
    except Exception:
        # Fall back to os.kill(pid, 0) on POSIX; on Windows psutil should exist.
        if IS_WINDOWS:
            return False
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False


def terminate_pid(pid: int | None) -> bool:
    """Ask a process to stop. Cross-platform (SIGTERM on POSIX, TerminateProcess
    on Windows via psutil). Returns True if a stop was requested."""
    if not pid:
        return False
    try:
        import psutil
        p = psutil.Process(int(pid))
        p.terminate()
        return True
    except Exception:
        try:
            import signal
            os.kill(int(pid), signal.SIGTERM)
            return True
        except Exception:
            return False


# ── Detached child-process spawn flags ──────────────────────────────────────────
# macOS/Linux use start_new_session=True to detach the daemon. Windows has no
# sessions; it uses creationflags. This helper returns the right kwargs for
# subprocess.Popen so callers stay OS-agnostic.

def detached_popen_kwargs() -> dict:
    if IS_WINDOWS:
        flags = 0
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        return {"creationflags": flags}
    return {"start_new_session": True}


# ── Monospace font paths for the overlay stamp bar ──────────────────────────────

def mono_font_paths() -> list[str]:
    if IS_WINDOWS:
        win = os.environ.get("WINDIR", r"C:\Windows")
        fonts = os.path.join(win, "Fonts")
        return [
            os.path.join(fonts, "consola.ttf"),   # Consolas
            os.path.join(fonts, "cour.ttf"),       # Courier New
            os.path.join(fonts, "lucon.ttf"),      # Lucida Console
        ]
    if IS_MAC:
        return [
            "/System/Library/Fonts/Menlo.ttc",
            "/System/Library/Fonts/Monaco.dfont",
            "/System/Library/Fonts/Supplemental/Courier New.ttf",
        ]
    # Linux — DejaVu/Liberation, covering the major distro layouts
    return [
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",                       # Arch/Artix
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",           # Debian/Ubuntu
        "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",                    # Fedora
        "/usr/share/fonts/TTF/LiberationMono-Regular.ttf",               # Arch/Artix
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ]


# ═════════════════════════════════════════════════════════════════════════════════
#  Linux display-session detection  (X11 vs Wayland vs headless)
# ═════════════════════════════════════════════════════════════════════════════════
# Everything Linux-specific below dispatches on the SESSION TYPE, not just the
# OS: X11 offers global window enumeration + in-process capture (mss); Wayland
# forbids both at the compositor (security model) and needs `grim` (wlroots)
# or the xdg-desktop-portal Screenshot API; a headless box (no DISPLAY /
# WAYLAND_DISPLAY) cannot capture at all and must say so clearly.

def linux_session_type(environ: dict | None = None) -> str:
    """'wayland' | 'x11' | 'headless' — detected from the session environment.

    Order matters: WAYLAND_DISPLAY wins (an XWayland session also has DISPLAY
    set, but an X11 grab under XWayland only sees XWayland clients — black or
    partial frames — so it must be treated as Wayland); XDG_SESSION_TYPE
    breaks ties; a bare DISPLAY (e.g. `startx` without a login manager, where
    XDG_SESSION_TYPE stays 'tty') still means X11.

    Pure — pass `environ` to unit-test the routing on any OS."""
    env = environ if environ is not None else os.environ
    if (env.get("WAYLAND_DISPLAY") or "").strip():
        return "wayland"
    st = (env.get("XDG_SESSION_TYPE") or "").strip().lower()
    if st == "wayland":
        return "wayland"
    if (env.get("DISPLAY") or "").strip():
        return "x11"
    if st == "x11":
        return "x11"
    return "headless"


def _proc_name_from_pid(pid: int) -> str:
    """Lower-cased process name for a PID (psutil, falling back to /proc)."""
    try:
        import psutil
        return (psutil.Process(int(pid)).name() or "").lower()
    except Exception:
        try:
            with open(f"/proc/{int(pid)}/comm") as f:
                return f.read().strip().lower()
        except Exception:
            return ""


# ═════════════════════════════════════════════════════════════════════════════════
#  Window enumeration
# ═════════════════════════════════════════════════════════════════════════════════
# Returns a dict {wid, x, y, w, h, title, owner} for the largest real top-level
# window whose owning process / title matches `app_name`, or None.
#
#   macOS  : Quartz CGWindowListCopyWindowInfo  → wid is a CGWindowNumber, which
#            `screencapture -l<wid>` can grab directly (survives overlap/offscreen).
#   Windows: win32gui.EnumWindows                → wid is an HWND, whose bounds
#            we hand to mss for a region grab (Windows has no capture-by-id CLI).

def _match_candidates(app_name: str) -> set[str]:
    app_name = (app_name or "").strip()
    cands = {app_name.lower()}
    cands.add(app_name.replace("-ng", "").replace(".app", "").replace(".exe", "").lower())
    cands.add(app_name.split("-")[0].lower())
    cands.discard("")
    return cands


def find_window(app_name: str, pid: int | None = None,
                title: str = "") -> dict | None:
    """Locate the target's window.

    `pid` is the reliable identifier and callers should pass it whenever the
    process is already known: an owner name like `Python`, `java` or `node` is
    shared across a whole ecosystem — including AgentVision's own processes — so
    name-only lookup can select a different app's window, or a dead record from a
    previous run of the same program. `title` breaks ties within one process.
    Passing neither still works, but is a guess.
    """
    app_name = (app_name or "").strip()
    if not app_name and pid is None:
        return None
    if IS_MAC:
        return _find_window_mac(app_name, pid=pid, title=title)
    if IS_WINDOWS:
        return _find_window_win(app_name)
    if IS_LINUX:
        return _find_window_linux(app_name)
    return None


def _find_window_mac(app_name: str, pid: int | None = None,
                     title: str = "") -> dict | None:
    """The target's window, identified as narrowly as the caller allows.

    THE BUG THIS FIXES. The old version listed `kCGWindowListOptionAll` — which
    includes DEAD and off-screen window records — matched on owner name only, and
    took the largest by area. With a generic owner it therefore picked a stale
    window from an already-exited process, and `screencapture -l<that wid>` fails
    with "could not create image from window". Measured on a tkinter app relaunched
    several times: capture produced 1 frame in 22 s and 19 hard errors, while the
    live window sat there perfectly capturable (a direct
    `screencapture -x -o -l<live wid>` returned a valid 560x388 image).

    An owner name is a weak identity — `Python`, `java`, `node`, `Electron` are
    shared by everything in their ecosystem, including AgentVision itself. So:

      * `pid` is the STRONG identity. The process matcher has already worked out
        which process is the target; passing its pid here ties the window to that
        exact process and makes the owner name irrelevant. This is what makes the
        lookup universal rather than a name heuristic.
      * `title` disambiguates when one process owns several windows.
      * On-screen and non-empty are required either way, because an off-screen or
        zero-area record cannot be photographed.
    """
    try:
        import Quartz
        cands = _match_candidates(app_name) if app_name else []
        want_title = (title or "").strip().lower()
        wlist = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID) or []
        best, best_score = None, (-1, -1)
        for w in wlist:
            owner = (w.get("kCGWindowOwnerName") or "").lower()
            wpid = w.get("kCGWindowOwnerPID")
            wname = (w.get("kCGWindowName") or "")
            # capture_app is documented as "window title or process name", and
            # for a Tk/Qt/SDL program the title is the only stable identity the
            # user can see — the OWNER of every tkinter window is just
            # "Python". Matching owner only meant a capture_app set to the
            # window's actual title matched NOTHING unless the process matcher
            # supplied a pid (measured: owner='Python', name='AV GUI Probe',
            # find_window("AV GUI Probe") -> None while the window sat on
            # screen and every frame was skipped).
            name_title_hit = bool(app_name and wname
                                  and app_name.lower() in wname.lower())
            if pid is not None:
                # Strong identity: this window must belong to the process the
                # matcher identified. Nothing else can shadow it.
                if wpid != pid:
                    continue
            elif not ((cands and any(c in owner for c in cands))
                      or name_title_hit):
                continue
            if w.get("kCGWindowLayer", 1) != 0:
                continue
            # ON-SCREEN: required ONLY when there is no pid to vouch for the
            # window. MEASURED on a tkinter app, same wid throughout:
            #     normal     stddev 37.6  content
            #     occluded   stddev 36.4  content
            #     MINIMISED  stddev 37.6  content
            # `screencapture -l` reads the window's backing store, so occlusion
            # and minimisation are both fine — that is one of this product's real
            # differentiators ("only the program, even minimised"). An earlier
            # version of this fix required kCGWindowIsOnscreen to shut out DEAD
            # window records and threw away minimised windows with them.
            #
            # When `pid` is supplied the process is known to be alive, so a window
            # belonging to it is minimised, not dead — liveness is the guard and
            # the on-screen test is not needed. Without a pid there is no such
            # guard, so on-screen stays required.
            onscreen = bool(w.get("kCGWindowIsOnscreen", False))
            if pid is None and not onscreen:
                continue
            b = w.get("kCGWindowBounds", {}) or {}
            wpx, hpx = int(b.get("Width", 0)), int(b.get("Height", 0))
            if wpx <= 1 or hpx <= 1:
                continue                       # helper/measurement windows
            # Ranked, NOT filtered. Area alone picks the wrong window: a tkinter
            # process also owns a BLANK 500x500 off-screen helper (250,000 px)
            # that outscores the real 560x388 UI (217,280 px), and a 2560x24 menu
            # bar. So rank on identity first and size last:
            #   1 an explicit title match          — the caller told us the name
            #   2 on screen                        — a visible window beats a
            #     hidden helper, while still allowing a MINIMISED window to win
            #     when nothing is on screen (that case must keep working)
            #   3 has a title at all               — real UI windows are titled;
            #     Tk's hidden root and helpers are not
            #   4 area                             — only as the final tiebreak
            title_hit = 1 if ((want_title and want_title in wname.lower())
                              or name_title_hit) else 0
            score = (title_hit, 1 if onscreen else 0,
                     1 if wname.strip() else 0, wpx * hpx)
            if score > best_score:
                best_score = score
                owner_hit = bool(cands and any(c in owner for c in cands))
                best = {
                    "wid": w.get("kCGWindowNumber"),
                    "x": int(b.get("X", 0)), "y": int(b.get("Y", 0)),
                    "w": wpx, "h": hpx,
                    "title": wname,
                    "owner": owner,
                    "pid": wpid,
                    "matched_by": ("pid" if pid is not None else
                                   "owner-name" if owner_hit else
                                   "window-title")
                                  + ("+title" if title_hit else ""),
                }
        return best
    except Exception:
        return None


def _win_is_cloaked(hwnd: int) -> bool:
    """True for DWM-cloaked windows (suspended UWP apps, windows on other
    virtual desktops, shell hosts like 'Windows Input Experience'). These
    report IsWindowVisible=True but have no on-screen pixels."""
    try:
        import ctypes
        from ctypes import wintypes
        DWMWA_CLOAKED = 14
        cloaked = wintypes.DWORD(0)
        res = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), DWMWA_CLOAKED,
            ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        return res == 0 and cloaked.value != 0
    except Exception:
        return False


def _win_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the VISIBLE window frame.

    Prefers DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS): on Win 10/11
    GetWindowRect includes the invisible DWM resize borders (~7px on
    left/right/bottom), which would put slivers of the background into every
    capture. Falls back to GetWindowRect if dwmapi is unavailable."""
    try:
        import ctypes
        from ctypes import wintypes
        DWMWA_EXTENDED_FRAME_BOUNDS = 9
        rect = wintypes.RECT()
        res = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(rect), ctypes.sizeof(rect))
        if res == 0 and rect.right > rect.left and rect.bottom > rect.top:
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    import win32gui
    return win32gui.GetWindowRect(hwnd)


def _find_window_win(app_name: str) -> dict | None:
    try:
        import win32gui
        import win32process
        try:
            import psutil
        except Exception:
            psutil = None
        cands = _match_candidates(app_name)

        best: dict | None = None
        best_area = 0

        def _proc_name(hwnd: int) -> str:
            if psutil is None:
                return ""
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                return (psutil.Process(pid).name() or "").lower()
            except Exception:
                return ""

        def _cb(hwnd, _extra):
            nonlocal best, best_area
            if not win32gui.IsWindowVisible(hwnd):
                return True
            # Minimized windows report -32000,-32000 origins — unusable for a
            # screen-region capture. Cloaked windows have no visible pixels.
            if win32gui.IsIconic(hwnd) or _win_is_cloaked(hwnd):
                return True
            title = (win32gui.GetWindowText(hwnd) or "")
            pname = _proc_name(hwnd)
            hay = f"{title.lower()} {pname}"
            if not any(c in hay for c in cands):
                return True
            try:
                l, t, r, b = _win_window_rect(hwnd)
            except Exception:
                return True
            w, h = r - l, b - t
            if w <= 1 or h <= 1:
                return True
            area = w * h
            if area > best_area:
                best_area = area
                best = {"wid": hwnd, "x": l, "y": t, "w": w, "h": h,
                        "title": title, "owner": pname}
            return True  # keep enumerating

        win32gui.EnumWindows(_cb, None)
        return best
    except Exception:
        return None


# ── Linux window enumeration (X11 only — EWMH) ─────────────────────────────────
# X11: enumerate via python-xlib (_NET_CLIENT_LIST + _NET_WM_NAME/_NET_WM_PID),
# falling back to the `wmctrl` / `xdotool` CLIs when python-xlib is absent.
# Wayland: NO compositor-independent global window enumeration exists (the
# security model hides other clients' windows) → return None gracefully, so
# capture falls back to crop/full-screen and the bridge surfaces its existing
# window-missing warning — exactly like the Windows minimized-window case.

def _linux_window_matches(cands: set, title: str, wm_class: str, proc: str) -> bool:
    """True if any candidate substring appears in title/class/process name."""
    hay = f"{(title or '').lower()} {(wm_class or '').lower()} {(proc or '').lower()}"
    return any(c in hay for c in cands)


def _xlib_window_title(d, w) -> str:
    """_NET_WM_NAME (UTF-8), falling back to legacy WM_NAME."""
    for atom_name in ("_NET_WM_NAME", "WM_NAME"):
        try:
            tp = w.get_full_property(d.intern_atom(atom_name), 0)  # 0 = AnyPropertyType
            if tp and tp.value:
                v = tp.value
                return (v.decode("utf-8", "replace")
                        if isinstance(v, (bytes, bytearray)) else str(v))
        except Exception:
            continue
    return ""


def _xlib_window_pid(d, w) -> int:
    try:
        pp = w.get_full_property(d.intern_atom("_NET_WM_PID"), 0)
        if pp and pp.value:
            return int(pp.value[0])
    except Exception:
        pass
    return 0


def _xlib_client_list(d) -> list[int]:
    """EWMH _NET_CLIENT_LIST — every top-level managed window id."""
    root = d.screen().root
    prop = root.get_full_property(d.intern_atom("_NET_CLIENT_LIST"), 0)
    return [int(x) for x in prop.value] if prop is not None else []


def _xlib_abs_origin(d, w) -> tuple[int, int]:
    """Absolute root coordinates of an X11 window's top-left corner, by walking
    up the parent chain accumulating each level's offset — correct under
    reparenting window managers (the client window sits inside a WM frame)."""
    x = y = 0
    root_id = d.screen().root.id
    cur = w
    for _ in range(64):                      # bounded — X trees are shallow
        g = cur.get_geometry()
        x += int(g.x)
        y += int(g.y)
        parent = cur.query_tree().parent
        if not parent or getattr(parent, "id", 0) in (0, root_id, cur.id):
            break
        cur = parent
    return x, y


def _linux_find_window_xlib(app_name: str) -> dict | None:
    from Xlib import display as _xdisplay   # lazy — optional dep (python-xlib)
    cands = _match_candidates(app_name)
    d = _xdisplay.Display()
    try:
        best, best_area = None, 0
        for wid in _xlib_client_list(d):
            try:
                w = d.create_resource_object("window", wid)
                title = _xlib_window_title(d, w)
                wm_class = " ".join(w.get_wm_class() or ())
                pid = _xlib_window_pid(d, w)
                proc = _proc_name_from_pid(pid) if pid else ""
                if not _linux_window_matches(cands, title, wm_class, proc):
                    continue
                geo = w.get_geometry()
                ww, wh = int(geo.width), int(geo.height)
                if ww <= 1 or wh <= 1:
                    continue
                area = ww * wh
                if area > best_area:
                    ax, ay = _xlib_abs_origin(d, w)
                    best_area = area
                    best = {"wid": int(wid), "x": ax, "y": ay, "w": ww, "h": wh,
                            "title": title, "owner": proc or wm_class.lower()}
            except Exception:
                continue
        return best
    finally:
        try:
            d.close()
        except Exception:
            pass


def _parse_wmctrl_list(text: str) -> list[dict]:
    """Parse `wmctrl -lpGx` output into window dicts. Pure — unit-tested on mac.
    Line: 0xID <desktop> <pid> <x> <y> <w> <h> <WM_CLASS> <host> <title…>
    desktop == -1 marks sticky/dock pseudo-windows."""
    out: list[dict] = []
    for line in (text or "").splitlines():
        parts = line.split(None, 9)
        if len(parts) < 9:
            continue
        try:
            out.append({
                "wid": int(parts[0], 16), "desktop": int(parts[1]),
                "pid": int(parts[2]),
                "x": int(parts[3]), "y": int(parts[4]),
                "w": int(parts[5]), "h": int(parts[6]),
                "wm_class": parts[7], "host": parts[8],
                "title": parts[9].strip() if len(parts) > 9 else "",
            })
        except (ValueError, IndexError):
            continue
    return out


def _linux_find_window_wmctrl(app_name: str) -> dict | None:
    if not shutil.which("wmctrl"):
        return None
    r = subprocess.run(["wmctrl", "-lpGx"], capture_output=True, text=True,
                       timeout=3)
    if r.returncode != 0:
        return None
    cands = _match_candidates(app_name)
    best, best_area = None, 0
    for w in _parse_wmctrl_list(r.stdout):
        if w["desktop"] == -1:               # sticky/dock/panel pseudo-windows
            continue
        proc = _proc_name_from_pid(w["pid"]) if w["pid"] else ""
        if not _linux_window_matches(cands, w["title"], w["wm_class"], proc):
            continue
        if w["w"] <= 1 or w["h"] <= 1:
            continue
        area = w["w"] * w["h"]
        if area > best_area:
            best_area = area
            best = {"wid": w["wid"], "x": w["x"], "y": w["y"],
                    "w": w["w"], "h": w["h"], "title": w["title"],
                    "owner": proc or w["wm_class"].lower()}
    return best


def _parse_xdotool_shell_geometry(text: str) -> tuple[int, int, int, int] | None:
    """Parse `xdotool getwindowgeometry --shell` output (X=…, Y=…, WIDTH=…,
    HEIGHT=… lines) into (x, y, w, h). Pure — unit-tested on mac."""
    vals: dict[str, int] = {}
    for line in (text or "").splitlines():
        k, sep, v = line.partition("=")
        if not sep:
            continue
        try:
            vals[k.strip().upper()] = int(v.strip())
        except ValueError:
            continue
    if all(k in vals for k in ("X", "Y", "WIDTH", "HEIGHT")):
        return vals["X"], vals["Y"], vals["WIDTH"], vals["HEIGHT"]
    return None


def _linux_find_window_xdotool(app_name: str) -> dict | None:
    if not shutil.which("xdotool"):
        return None
    cands = _match_candidates(app_name)
    ids: list[str] = []
    for c in cands:
        pat = re.escape(c)
        for flag in ("--name", "--class"):
            try:
                r = subprocess.run(
                    ["xdotool", "search", "--onlyvisible", flag, pat],
                    capture_output=True, text=True, timeout=3)
                ids.extend(x.strip() for x in r.stdout.split() if x.strip())
            except Exception:
                continue
    best, best_area = None, 0
    for wid in dict.fromkeys(ids):           # dedupe, preserve order
        try:
            g = subprocess.run(["xdotool", "getwindowgeometry", "--shell", wid],
                               capture_output=True, text=True, timeout=3)
            geo = _parse_xdotool_shell_geometry(g.stdout)
            if not geo or geo[2] <= 1 or geo[3] <= 1:
                continue
            t = subprocess.run(["xdotool", "getwindowname", wid],
                               capture_output=True, text=True, timeout=3)
            x, y, w, h = geo
            if w * h > best_area:
                best_area = w * h
                best = {"wid": int(wid), "x": x, "y": y, "w": w, "h": h,
                        "title": (t.stdout or "").strip(), "owner": ""}
        except Exception:
            continue
    return best


def _find_window_linux(app_name: str) -> dict | None:
    """X11: EWMH enumeration via python-xlib → wmctrl → xdotool. Wayland /
    headless: None (see the section comment). Never raises."""
    try:
        if linux_session_type() != "x11":
            return None
        for fn in (_linux_find_window_xlib, _linux_find_window_wmctrl,
                   _linux_find_window_xdotool):
            try:
                w = fn(app_name)
            except Exception:
                w = None
            if w:
                return w
    except Exception:
        pass
    return None


def _parse_xwininfo_geometry(text: str) -> tuple[int, int, int, int] | None:
    """Parse `xwininfo -id <wid>` output into (x, y, w, h). Pure."""
    pats = {"x": r"Absolute upper-left X:\s*(-?\d+)",
            "y": r"Absolute upper-left Y:\s*(-?\d+)",
            "w": r"Width:\s*(\d+)",
            "h": r"Height:\s*(\d+)"}
    vals: dict[str, int] = {}
    for k, p in pats.items():
        m = re.search(p, text or "")
        if not m:
            return None
        vals[k] = int(m.group(1))
    return vals["x"], vals["y"], vals["w"], vals["h"]


def _x11_window_geometry(wid) -> tuple[int, int, int, int] | None:
    """(x, y, w, h) in root pixels for an X11 window id — re-queried at capture
    time so a moved/resized window is grabbed at its CURRENT bounds (mirrors
    the Windows hwnd re-query). python-xlib → xdotool → xwininfo. None if the
    window is gone or no tool is available."""
    try:
        wid_i = int(wid)
    except (TypeError, ValueError):
        return None
    try:
        from Xlib import display as _xdisplay
        d = _xdisplay.Display()
        try:
            w = d.create_resource_object("window", wid_i)
            geo = w.get_geometry()
            if int(geo.width) > 1 and int(geo.height) > 1:
                ax, ay = _xlib_abs_origin(d, w)
                return ax, ay, int(geo.width), int(geo.height)
        finally:
            try:
                d.close()
            except Exception:
                pass
    except Exception:
        pass
    try:
        if shutil.which("xdotool"):
            r = subprocess.run(
                ["xdotool", "getwindowgeometry", "--shell", str(wid_i)],
                capture_output=True, text=True, timeout=3)
            geo = _parse_xdotool_shell_geometry(r.stdout)
            if geo and geo[2] > 1 and geo[3] > 1:
                return geo
    except Exception:
        pass
    try:
        if shutil.which("xwininfo"):
            r = subprocess.run(["xwininfo", "-id", str(wid_i)],
                               capture_output=True, text=True, timeout=3)
            geo = _parse_xwininfo_geometry(r.stdout)
            if geo and geo[2] > 1 and geo[3] > 1:
                return geo
    except Exception:
        pass
    return None


def get_window_id(app_name: str):
    w = find_window(app_name)
    return w["wid"] if w else None


def get_window_bounds(app_name: str):
    w = find_window(app_name)
    return (w["x"], w["y"], w["w"], w["h"]) if w else None


# ═════════════════════════════════════════════════════════════════════════════════
#  Screen capture  (the core of the bridge)
# ═════════════════════════════════════════════════════════════════════════════════
# Priority mirrors the original mac bridge:
#   1. capture the exact window (wid)      — survives overlap/off-screen on
#      macOS (screencapture -l); on Windows it is a region grab of the window
#      bounds, so it does NOT survive occlusion or minimization
#   2. capture a fixed crop rect (crop)    — manual x,y,w,h override
#   3. primary-screen fallback
#
# `crop` is (x, y, w, h) in screen pixels, or None.
# `wid`  is the value returned by find_window()["wid"] for the active OS, or None.
# Raises RuntimeError on failure so the bridge's AutoCapture surfaces it.

def capture_frame(image_path: str, wid=None, crop=None) -> None:
    if IS_MAC:
        _capture_frame_mac(image_path, wid, crop)
    elif IS_LINUX:
        _capture_frame_linux(image_path, wid, crop)
    else:
        _capture_frame_pillib(image_path, wid, crop)


def _capture_frame_mac(image_path: str, wid, crop) -> None:
    if wid is not None:
        cmd = ["screencapture", "-x", f"-l{wid}", str(image_path)]
    elif crop:
        x, y, w, h = crop
        cmd = ["screencapture", "-x", f"-R{x},{y},{w},{h}", str(image_path)]
    else:
        cmd = ["screencapture", "-x", str(image_path)]
    result = subprocess.run(cmd, capture_output=True, timeout=5)
    if result.returncode != 0:
        raise RuntimeError(f"screencapture failed: {result.stderr.decode()}")
    if not Path(image_path).exists():
        raise RuntimeError(f"screencapture produced no file at {image_path}")


def rect_to_region(left: int, top: int, right: int, bottom: int) -> dict | None:
    """Convert a Win32 window RECT (left,top,right,bottom, in PHYSICAL pixels —
    the process is DPI-aware, so GetWindowRect/DwmGetWindowAttribute and mss are
    in the same coordinate space) into an mss grab region {left,top,width,height},
    or None if the rect is degenerate/minimized. Pure — unit-tested on macOS."""
    try:
        left, top, right, bottom = int(left), int(top), int(right), int(bottom)
    except (TypeError, ValueError):
        return None
    w, h = right - left, bottom - top
    if w <= 1 or h <= 1:
        return None
    # Minimized windows report an off-screen origin (~ -32000). Reject.
    if left <= -30000 or top <= -30000:
        return None
    return {"left": left, "top": top, "width": w, "height": h}


def _capture_frame_pillib(image_path: str, wid, crop) -> None:
    """Windows / Linux capture via mss (fast, multi-monitor aware).

    Resolves a capture region in this priority order:
      wid (HWND bounds)  →  crop rect  →  primary monitor.

    NOTE: unlike macOS `screencapture -l<wid>`, the wid path is a screen-region
    grab of the window's bounds — it does NOT survive occlusion (an overlapping
    window's pixels would be captured) and cannot capture a minimized window
    (we skip the wid and fall through to crop/full-screen in that case).
    """
    region = None  # {top,left,width,height} for mss

    if wid is not None:
        try:
            import win32gui
            hwnd = int(wid)
            # A minimized window reports an off-screen rect (≈ -32000,-32000
            # with a small positive extent) — capturing it yields black
            # pixels. Skip the wid and fall through to crop/full-screen.
            if not win32gui.IsIconic(hwnd):
                l, t, r, b = _win_window_rect(hwnd)
                region = rect_to_region(l, t, r, b)
        except Exception:
            region = None

    if region is None and crop:
        x, y, w, h = crop
        region = {"left": int(x), "top": int(y), "width": int(w), "height": int(h)}

    try:
        import mss
        import mss.tools
    except Exception as e:
        raise RuntimeError(
            "screen capture backend 'mss' is not installed — "
            "run: pip install mss   (%s)" % e)

    try:
        with mss.mss() as sct:
            if region is not None:
                grab_area = region
            else:
                # monitors[1] = the PRIMARY display (matches the macOS
                # `screencapture -x` behaviour); monitors[0] would be the
                # whole virtual desktop spanning every monitor.
                grab_area = (sct.monitors[1] if len(sct.monitors) > 1
                             else sct.monitors[0])
            shot = sct.grab(grab_area)
            mss.tools.to_png(shot.rgb, shot.size, output=str(image_path))
    except Exception as e:
        raise RuntimeError(f"mss capture failed: {e}")

    if not Path(image_path).exists():
        raise RuntimeError(f"capture produced no file at {image_path}")


# ═════════════════════════════════════════════════════════════════════════════════
#  Linux capture — session-aware (X11: mss/CLI; Wayland: grim/portal)
# ═════════════════════════════════════════════════════════════════════════════════

_LINUX_LAST_BACKEND = ""     # backend that actually produced the last frame


def _have_mss() -> bool:
    try:
        import mss  # noqa: F401
        return True
    except Exception:
        return False


def linux_capture_chain(session: str, *, has_mss: bool | None = None,
                        which=None) -> list[str]:
    """Ordered capture backends to try for a session type. Pure — inject
    `has_mss` / `which` to unit-test the routing on any OS.

      x11     : mss (fast, in-process) → maim → scrot → import (ImageMagick)
      wayland : grim (wlroots: Sway/Hyprland/labwc — the common Artix
                compositors) → gnome-screenshot → spectacle (KDE) →
                xdg-desktop-portal Screenshot (portable, needs a portal
                backend; may prompt depending on the portal)
      headless: [] (capture_frame raises with a clear message)
    """
    if which is None:
        which = shutil.which
    chain: list[str] = []
    if session == "x11":
        if (has_mss if has_mss is not None else _have_mss()):
            chain.append("mss")
        for tool in ("maim", "scrot", "import"):
            if which(tool):
                chain.append(tool)
    elif session == "wayland":
        for tool in ("grim", "gnome-screenshot", "spectacle"):
            if which(tool):
                chain.append(tool)
        if which("gdbus"):
            chain.append("portal")
    return chain


def _linux_backend_cmd(backend: str, image_path: str,
                       region) -> tuple[list[str] | None, bool]:
    """argv for a CLI capture backend + whether the caller must POST-CROP the
    result to `region` (backends without native region support grab the full
    screen). region = {left, top, width, height} or None. Returns (None, False)
    for the non-CLI backends (mss / portal). Pure — unit-tested on mac."""
    p = str(image_path)
    if backend == "maim":
        if region:
            return (["maim", "-g", "%dx%d+%d+%d" % (
                region["width"], region["height"],
                region["left"], region["top"]), p], False)
        return (["maim", p], False)
    if backend == "scrot":
        if region:
            return (["scrot", "-o", "-a", "%d,%d,%d,%d" % (
                region["left"], region["top"],
                region["width"], region["height"]), p], False)
        return (["scrot", "-o", p], False)
    if backend == "import":                  # ImageMagick
        if region:
            return (["import", "-window", "root", "-crop", "%dx%d+%d+%d" % (
                region["width"], region["height"],
                region["left"], region["top"]), "+repage", p], False)
        return (["import", "-window", "root", p], False)
    if backend == "grim":
        if region:
            return (["grim", "-g", "%d,%d %dx%d" % (
                region["left"], region["top"],
                region["width"], region["height"]), p], False)
        return (["grim", p], False)
    if backend == "gnome-screenshot":
        return (["gnome-screenshot", "-f", p], bool(region))
    if backend == "spectacle":
        return (["spectacle", "-b", "-n", "-o", p], bool(region))
    return (None, False)


def _post_crop_image(image_path: str, region) -> None:
    """Crop a full-screen grab down to `region` for backends without native
    region support. Best-effort: if Pillow is missing the full frame is kept
    (degraded, not broken). Assumes the grab's origin is the screen's (0,0) —
    true for the primary output."""
    try:
        from PIL import Image
    except Exception:
        return
    try:
        with Image.open(image_path) as im:
            l, t = int(region["left"]), int(region["top"])
            r, b = l + int(region["width"]), t + int(region["height"])
            l, t = max(0, l), max(0, t)
            r, b = min(im.width, r), min(im.height, b)
            if r > l and b > t:
                im.crop((l, t, r, b)).save(image_path)
    except Exception:
        pass


_PORTAL_RESPONSE_URI_RE = re.compile(r"'uri':\s*<'([^']+)'>")


def _parse_portal_response_line(line: str) -> str | None:
    """Extract the file:// URI from a gdbus-monitor Request.Response signal
    line, or None. Pure — unit-tested on mac."""
    if "Response" not in (line or ""):
        return None
    m = _PORTAL_RESPONSE_URI_RE.search(line)
    return m.group(1) if m else None


def _portal_screenshot(image_path: str, timeout: float = 10.0) -> None:
    """xdg-desktop-portal org.freedesktop.portal.Screenshot — the portable
    Wayland fallback (works on GNOME/KDE/wlroots wherever a portal backend is
    installed; pure stdlib via the `gdbus` CLI, no dbus python package).

    The portal API is asynchronous: the method returns a request handle and
    the result arrives as a Request.Response SIGNAL carrying a file:// URI —
    so a `gdbus monitor` watches the bus while the call is made. Depending on
    the portal backend this may show a one-time permission prompt. Raises on
    failure (chain moves on / capture_frame reports it)."""
    import select as _select
    import urllib.parse
    token = f"agentvision{os.getpid()}"
    mon = subprocess.Popen(
        ["gdbus", "monitor", "--session",
         "--dest", "org.freedesktop.portal.Desktop"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        time.sleep(0.2)                      # let the monitor attach first
        r = subprocess.run(
            ["gdbus", "call", "--session",
             "--dest", "org.freedesktop.portal.Desktop",
             "--object-path", "/org/freedesktop/portal/desktop",
             "--method", "org.freedesktop.portal.Screenshot.Screenshot",
             "", "{'handle_token': <'%s'>, 'interactive': <false>}" % token],
            capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError("portal Screenshot call failed: "
                               + (r.stderr or "").strip())
        uri = None
        deadline = time.monotonic() + timeout
        while uri is None and time.monotonic() < deadline:
            if mon.stdout is None:
                break
            rl, _, _ = _select.select([mon.stdout], [], [], 0.25)
            if not rl:
                continue
            line = mon.stdout.readline()
            if not line:
                break
            uri = _parse_portal_response_line(line)
        if not uri:
            raise RuntimeError(
                "portal Screenshot: no Response with a uri within timeout — "
                "is a portal backend for your compositor installed "
                "(xdg-desktop-portal-wlr / -gtk / -kde)?")
        if not uri.startswith("file://"):
            raise RuntimeError(f"portal returned a non-file uri: {uri}")
        src = urllib.parse.unquote(uri[len("file://"):])
        shutil.copyfile(src, str(image_path))
        try:
            os.remove(src)                   # don't litter ~/Pictures
        except OSError:
            pass
    finally:
        try:
            mon.terminate()
        except Exception:
            pass


def _capture_mss_region(image_path: str, region) -> None:
    """mss grab of `region` (or the primary monitor). X11 only on Linux."""
    import mss
    import mss.tools
    with mss.mss() as sct:
        grab_area = region if region is not None else (
            sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0])
        shot = sct.grab(grab_area)
        mss.tools.to_png(shot.rgb, shot.size, output=str(image_path))


def _capture_frame_linux(image_path: str, wid, crop) -> None:
    """Linux capture, session-aware (called by capture_frame; bridge unchanged).

    Region priority mirrors the other OSes:
      window bounds (X11 only — re-queried from the X window id at capture
      time) → crop rect → primary screen.

    X11    : mss grabs the region. Like Windows this is a region grab of the
             window's bounds — occlusion-sensitive, NOT a compositor window
             snapshot. CLI fallbacks (maim / scrot / ImageMagick import) run
             when mss is unavailable.
    Wayland: per-window capture is NOT possible (compositor security) — `wid`
             is ignored and the grab falls back to crop/full-screen; the
             bridge's window_missing warning surfaces this because
             find_window returns None on Wayland. Backends: grim (wlroots),
             gnome-screenshot / spectacle, then the xdg-desktop-portal
             Screenshot call. Backends without native region support grab the
             full screen and the frame is post-cropped via Pillow.
    Raises RuntimeError on failure (matching the mac/win paths) so the
    bridge's AutoCapture surfaces it."""
    global _LINUX_LAST_BACKEND
    session = linux_session_type()
    if session == "headless":
        raise RuntimeError(
            "no display session detected (DISPLAY / WAYLAND_DISPLAY unset) — "
            "screen capture needs a running X11 or Wayland session")

    region = None                            # {left,top,width,height}
    if wid is not None and session == "x11":
        geo = _x11_window_geometry(wid)
        if geo:
            region = rect_to_region(geo[0], geo[1],
                                    geo[0] + geo[2], geo[1] + geo[3])
    if region is None and crop:
        x, y, w, h = crop
        region = {"left": int(x), "top": int(y),
                  "width": int(w), "height": int(h)}

    chain = linux_capture_chain(session)
    if not chain:
        hint = ("install one of: mss (pip install mss), maim, scrot, or "
                "ImageMagick" if session == "x11" else
                "install grim (wlroots compositors) or xdg-desktop-portal + "
                "the portal backend for your compositor")
        raise RuntimeError(
            f"no capture backend available for this {session} session — {hint}")

    errors: list[str] = []
    for backend in chain:
        try:
            if backend == "mss":
                _capture_mss_region(image_path, region)
            elif backend == "portal":
                _portal_screenshot(image_path)
                if region:
                    _post_crop_image(image_path, region)
            else:
                argv, needs_crop = _linux_backend_cmd(backend, image_path, region)
                if argv is None:
                    continue
                r = subprocess.run(argv, capture_output=True, timeout=15)
                if r.returncode != 0:
                    msg = (r.stderr or b"").decode("utf-8", "replace").strip()
                    raise RuntimeError(msg or f"exit {r.returncode}")
                if needs_crop and region:
                    _post_crop_image(image_path, region)
            p = Path(image_path)
            if not p.exists() or p.stat().st_size == 0:
                raise RuntimeError("produced no file")
            _LINUX_LAST_BACKEND = f"{backend}({session})"
            return
        except Exception as e:
            errors.append(f"{backend}: {e}")

    raise RuntimeError(f"all {session} capture backends failed — "
                       + "; ".join(errors))


# ═════════════════════════════════════════════════════════════════════════════════
#  Capture-health: detect black / blank frames
# ═════════════════════════════════════════════════════════════════════════════════
# A "successful" capture can still be useless: a minimized/occluded window, an
# anti-cheat-blocked game, or a not-yet-painted window yields an all-black (or
# single-flat-color) PNG. The bridge must SURFACE that instead of silently
# storing a black frame that the AI then tries to read. Returns a small dict so
# the bridge can stamp capture health onto the frame JSON. Degrades gracefully
# (returns {} ) if Pillow isn't available — capture still works, we just can't
# assess blankness.

def image_health(image_path: str) -> dict:
    """Cheap blank/black detector. Returns
    {"mean": float, "stddev": float, "is_blank": bool, "assessed": bool}.
    is_blank == True means near-uniform pixels (black screen / no paint).
    Never raises."""
    try:
        from PIL import Image, ImageStat
    except Exception:
        return {"assessed": False}
    try:
        with Image.open(image_path) as im:
            im = im.convert("L")
            # Downscale so the stat pass is O(1)-ish regardless of resolution.
            im.thumbnail((64, 64))
            st = ImageStat.Stat(im)
            mean = float(st.mean[0])
            stddev = float(st.stddev[0])
        # Uniform image ⇒ tiny stddev. Also flag the classic all-black grab.
        is_blank = stddev < 2.0 or mean < 3.0
        return {"mean": round(mean, 2), "stddev": round(stddev, 2),
                "is_blank": bool(is_blank), "assessed": True}
    except Exception:
        return {"assessed": False}


def capture_backend_name() -> str:
    """Which capture backend this OS uses — surfaced in frame metadata so the
    AI knows whether wid-capture survives occlusion (mac) or not (win/linux).
    On Linux this is session-aware — e.g. 'mss(x11)', 'grim(wayland)',
    'portal(wayland)', 'none(headless)' — and once a frame has been captured
    it reports the backend that actually produced it."""
    if IS_MAC:
        return "screencapture"
    if IS_LINUX:
        if _LINUX_LAST_BACKEND:
            return _LINUX_LAST_BACKEND
        session = linux_session_type()
        chain = linux_capture_chain(session)
        return f"{chain[0]}({session})" if chain else f"none({session})"
    return "mss"


def mac_screen_recording_permission() -> bool | None:
    """Ask the OS DIRECTLY whether this process may record the screen.

    CGPreflightScreenCaptureAccess reads the TCC grant without prompting
    (never CGRequestScreenCaptureAccess here — a doctor must not pop dialogs).
    Returns True/False on macOS 10.15+, None when the answer is unavailable
    (non-mac, or the symbol is missing). This exists because inferring
    permission from image pixels cannot work: a permission-denied
    `screencapture` returns the wallpaper (non-blank!), and a permitted grab
    of a uniform screen region is blank — the pixels answer a different
    question."""
    if not IS_MAC:
        return None
    try:
        import ctypes
        import ctypes.util
        lib = ctypes.util.find_library("CoreGraphics")
        if not lib:
            return None
        cg = ctypes.CDLL(lib)
        fn = getattr(cg, "CGPreflightScreenCaptureAccess", None)
        if fn is None:
            return None                          # pre-10.15: not gated
        fn.restype = ctypes.c_bool
        return bool(fn())
    except Exception:
        return None


def capture_selftest() -> dict:
    """Grab a screen region and confirm it is a real (non-blank) image.
    Returns a JSON-able health record for `agentvision doctor`. Never raises;
    reports the failure in `detail` instead.

    TWO FALSE VERDICTS THIS USED TO EMIT, both measured on a healthy machine:

      * ok=False because the fixed 64x64 top-left crop happened to land on a
        uniform patch (a full-screen video frame); the very next run passed.
        A plain-wallpaper user would fail EVERY run. So a blank small region
        escalates to a full-screen grab before any failure is declared.
      * The failure text blamed "headless session, locked screen, or
        capture-blocked surface" — while the actual macOS permission state
        was readable from the OS and was GRANTED. The permission is now asked
        directly (see mac_screen_recording_permission) and reported either
        way, so a user can tell their setup apart from a capture defect.

    On failure the sample image is KEPT and its path reported — the evidence
    beats a description of it."""
    import tempfile
    out = {"check": "capture", "ok": False, "backend": capture_backend_name(),
           "detail": ""}
    if IS_LINUX:
        out["session"] = linux_session_type()
    if IS_MAC:
        perm = mac_screen_recording_permission()
        if perm is not None:
            out["screen_recording_permission"] = perm
    tmp = os.path.join(tempfile.gettempdir(),
                       f"av_capture_selftest_{os.getpid()}.png")
    keep_evidence = False
    try:
        # A fixed small crop of the top-left of the primary screen first —
        # cheap, and sufficient when it shows content.
        capture_frame(tmp, wid=None, crop=(0, 0, 64, 64))
        if IS_LINUX:
            out["backend"] = capture_backend_name()   # backend that really ran
        health = image_health(tmp)
        if health.get("assessed") and health.get("is_blank", True):
            # The corner being uniform proves nothing about capture. Only a
            # full-screen grab that is ALSO uniform is worth failing on.
            capture_frame(tmp, wid=None, crop=None)
            health = image_health(tmp)
            out["escalated_to_fullscreen"] = True
        out["image_health"] = health
        if not health.get("assessed"):
            out["detail"] = "captured, but Pillow unavailable to assess blankness"
            out["ok"] = os.path.exists(tmp) and os.path.getsize(tmp) > 0
        else:
            out["ok"] = not health.get("is_blank", True)
            if out["ok"]:
                out["detail"] = "captured a non-blank image"
            else:
                keep_evidence = True
                out["sample_kept_at"] = tmp
                bits = ["the FULL SCREEN capture is uniform — locked screen, "
                        "headless session, capture-blocked surface, or a "
                        "genuinely uniform display (look at sample_kept_at "
                        "and judge for yourself)"]
                if out.get("screen_recording_permission") is False:
                    bits.insert(0, "macOS Screen Recording permission is NOT "
                                   "granted to this process — grant it under "
                                   "System Settings → Privacy & Security → "
                                   "Screen Recording, then re-run. This is a "
                                   "setup step, not an AgentVision defect")
                elif out.get("screen_recording_permission") is True:
                    bits.append("macOS Screen Recording permission IS granted, "
                                "so this is not the permission")
                out["detail"] = "; ".join(bits)
    except Exception as e:
        out["detail"] = f"capture failed: {e}"
        if IS_MAC and out.get("screen_recording_permission") is False:
            out["detail"] += (" — and macOS Screen Recording permission is NOT "
                              "granted to this process (System Settings → "
                              "Privacy & Security → Screen Recording); fix "
                              "that first")
    finally:
        try:
            if os.path.exists(tmp) and not keep_evidence:
                os.remove(tmp)
        except Exception:
            pass
    return out


def window_enum_selftest(app_name: str = "") -> dict:
    """Confirm window enumeration works: count visible top-level windows, and
    (if app_name given) whether a matching window was found. JSON-able.
    On Linux this is session-aware: Wayland reports ok=None (not-applicable —
    global window enumeration is impossible by design, capture falls back to
    region/full-screen), headless reports the missing display."""
    out = {"check": "window_enum", "ok": False, "detail": ""}
    if IS_LINUX:
        session = linux_session_type()
        out["session"] = session
        if session == "wayland":
            out["ok"] = None                 # not-applicable, not a failure
            out["visible_windows"] = 0
            out["detail"] = (
                "window enumeration is not possible on Wayland (compositor "
                "security) — capture falls back to region/full-screen; set a "
                "capture crop for app-scoped frames")
            return out
        if session == "headless":
            out["detail"] = ("no display session (DISPLAY unset) — window "
                             "enumeration unavailable")
            return out
    try:
        titles = list_window_titles(limit=200)
        out["visible_windows"] = len(titles)
        if app_name:
            w = find_window(app_name)
            out["target_found"] = bool(w)
            out["target"] = {"title": w.get("title"), "bounds":
                             [w["x"], w["y"], w["w"], w["h"]]} if w else None
        out["ok"] = len(titles) > 0 or IS_MAC   # mac uses Quartz, list may be empty
        out["detail"] = f"enumerated {len(titles)} visible window(s)"
        if IS_LINUX and not titles:
            out["detail"] += (" — install python-xlib (pip) or wmctrl/xdotool "
                              "(system) for X11 window enumeration")
    except Exception as e:
        out["detail"] = f"window enum failed: {e}"
    return out


def linux_session_selftest() -> dict:
    """Linux-only doctor check: the detected session type, the capture chain
    available for it, and which optional tools/python deps are present.
    ok=True when a display session exists AND at least one capture backend is
    available; ok=None (not-applicable) off-Linux. Never raises."""
    out: dict = {"check": "linux_session", "ok": False, "detail": ""}
    if not IS_LINUX:
        out["ok"] = None
        out["detail"] = f"not Linux (os={sys.platform})"
        return out
    session = linux_session_type()
    chain = linux_capture_chain(session)
    try:
        import Xlib  # noqa: F401
        have_xlib = True
    except Exception:
        have_xlib = False
    out.update({
        "session": session,
        "display": os.environ.get("DISPLAY", ""),
        "wayland_display": os.environ.get("WAYLAND_DISPLAY", ""),
        "xdg_session_type": os.environ.get("XDG_SESSION_TYPE", ""),
        "capture_chain": chain,
        "python_mss": _have_mss(),
        "python_xlib": have_xlib,
        "tools": {t: bool(shutil.which(t)) for t in
                  ("grim", "maim", "scrot", "import", "gdbus", "xdotool",
                   "wmctrl", "xwininfo", "gnome-screenshot", "spectacle")},
    })
    if session == "headless":
        out["detail"] = ("no DISPLAY/WAYLAND_DISPLAY — headless session; "
                         "capture and window enumeration are unavailable")
        return out
    out["ok"] = bool(chain)
    out["detail"] = (f"{session} session; capture chain: "
                     + (" → ".join(chain) if chain else
                        "EMPTY — install "
                        + ("mss (pip) or maim/scrot/ImageMagick"
                           if session == "x11" else
                           "grim or xdg-desktop-portal + a portal backend")))
    return out


# ═════════════════════════════════════════════════════════════════════════════════
#  Foreground app / window title  (for state_snapshot + ui_recorder)
# ═════════════════════════════════════════════════════════════════════════════════

def frontmost_app() -> str:
    if IS_MAC:
        try:
            script = ('tell application "System Events" to get name of '
                      'first process whose frontmost is true')
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=2)
            return r.stdout.strip()
        except Exception:
            return ""
    if IS_WINDOWS:
        try:
            import win32gui
            import win32process
            import psutil
            hwnd = win32gui.GetForegroundWindow()
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            return psutil.Process(pid).name() or ""
        except Exception:
            return ""
    if IS_LINUX:
        wid = _x11_active_window_id()
        if wid:
            pid = _x11_window_pid_of(wid)
            if pid:
                return _proc_name_from_pid(pid)
        return ""
    return ""


# ── X11 active-window helpers (EWMH _NET_ACTIVE_WINDOW; Wayland → none) ─────────

def _x11_active_window_id() -> int:
    """X11 _NET_ACTIVE_WINDOW id, or 0. Wayland/headless: 0 (no global query
    exists — best-effort/empty by design). Never raises."""
    if linux_session_type() != "x11":
        return 0
    try:
        from Xlib import display as _xdisplay
        d = _xdisplay.Display()
        try:
            root = d.screen().root
            prop = root.get_full_property(
                d.intern_atom("_NET_ACTIVE_WINDOW"), 0)
            if prop and prop.value and int(prop.value[0]):
                return int(prop.value[0])
        finally:
            try:
                d.close()
            except Exception:
                pass
    except Exception:
        pass
    try:
        if shutil.which("xdotool"):
            r = subprocess.run(["xdotool", "getactivewindow"],
                               capture_output=True, text=True, timeout=2)
            s = (r.stdout or "").strip()
            if s.isdigit():
                return int(s)
    except Exception:
        pass
    return 0


def _x11_window_pid_of(wid: int) -> int:
    try:
        from Xlib import display as _xdisplay
        d = _xdisplay.Display()
        try:
            w = d.create_resource_object("window", int(wid))
            return _xlib_window_pid(d, w)
        finally:
            try:
                d.close()
            except Exception:
                pass
    except Exception:
        pass
    try:
        if shutil.which("xdotool"):
            r = subprocess.run(["xdotool", "getwindowpid", str(int(wid))],
                               capture_output=True, text=True, timeout=2)
            s = (r.stdout or "").strip()
            if s.isdigit():
                return int(s)
    except Exception:
        pass
    return 0


def _x11_window_title_of(wid: int) -> str:
    try:
        from Xlib import display as _xdisplay
        d = _xdisplay.Display()
        try:
            w = d.create_resource_object("window", int(wid))
            return _xlib_window_title(d, w)
        finally:
            try:
                d.close()
            except Exception:
                pass
    except Exception:
        pass
    try:
        if shutil.which("xdotool"):
            r = subprocess.run(["xdotool", "getwindowname", str(int(wid))],
                               capture_output=True, text=True, timeout=2)
            return (r.stdout or "").strip()
    except Exception:
        pass
    return ""


def front_window_title() -> str:
    if IS_MAC:
        try:
            script = ('tell application "System Events" to get name of front '
                      'window of (first process whose frontmost is true)')
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=2)
            return r.stdout.strip()
        except Exception:
            return ""
    if IS_WINDOWS:
        try:
            import win32gui
            return win32gui.GetWindowText(win32gui.GetForegroundWindow()) or ""
        except Exception:
            return ""
    if IS_LINUX:
        wid = _x11_active_window_id()
        return _x11_window_title_of(wid) if wid else ""
    return ""


def list_window_titles(limit: int = 60) -> list[str]:
    """Visible top-level window titles — used by the GUI's app picker on
    Windows and Linux/X11 (where there is no AppleScript `choose
    application`). Wayland: [] (no global enumeration — type the title/crop
    manually)."""
    out: list[str] = []
    if IS_WINDOWS:
        try:
            import win32gui

            def _cb(hwnd, _):
                if win32gui.IsWindowVisible(hwnd) and not _win_is_cloaked(hwnd):
                    t = win32gui.GetWindowText(hwnd)
                    if t and t.strip():
                        out.append(t.strip())
                return True  # keep enumerating
            win32gui.EnumWindows(_cb, None)
        except Exception:
            pass
    elif IS_LINUX and linux_session_type() == "x11":
        try:
            from Xlib import display as _xdisplay
            d = _xdisplay.Display()
            try:
                for wid in _xlib_client_list(d):
                    try:
                        w = d.create_resource_object("window", wid)
                        t = _xlib_window_title(d, w)
                        if t and t.strip():
                            out.append(t.strip())
                    except Exception:
                        continue
            finally:
                try:
                    d.close()
                except Exception:
                    pass
        except Exception:
            pass
        if not out:                          # python-xlib absent → wmctrl CLI
            try:
                if shutil.which("wmctrl"):
                    r = subprocess.run(["wmctrl", "-lpGx"], capture_output=True,
                                       text=True, timeout=3)
                    out.extend(w["title"] for w in _parse_wmctrl_list(r.stdout)
                               if w["title"].strip())
            except Exception:
                pass
    return sorted(set(out))[:limit]


# ═════════════════════════════════════════════════════════════════════════════════
#  Shell helpers: reveal a file, open a URL/settings pane
# ═════════════════════════════════════════════════════════════════════════════════

def reveal_path(path) -> None:
    """Open the OS file manager showing `path` (selected if possible)."""
    p = str(path)
    try:
        if IS_WINDOWS:
            target = p if os.path.isdir(p) else os.path.dirname(p) or p
            if os.path.isfile(p):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
            else:
                os.startfile(target)  # type: ignore[attr-defined]
        elif IS_MAC:
            subprocess.Popen(["open", "-R", p] if os.path.exists(p) else ["open", p])
        else:
            subprocess.Popen(["xdg-open", p if os.path.isdir(p) else os.path.dirname(p)])
    except Exception:
        pass


def open_url(url: str) -> None:
    try:
        if IS_WINDOWS:
            os.startfile(url)  # type: ignore[attr-defined]
        elif IS_MAC:
            subprocess.Popen(["open", url])
        else:
            subprocess.Popen(["xdg-open", url])
    except Exception:
        pass


def screen_capture_permission_note() -> str:
    """One-line note for the Permissions UI describing what (if anything) the
    OS requires before AgentVision can grab frames."""
    if IS_WINDOWS:
        return ("Windows does not gate normal desktop screen capture — no "
                "permission grant is required. (Some anti-cheat-protected "
                "games block third-party capture; run AgentVision as the same "
                "user, and prefer windowed/borderless mode for those.)")
    if IS_MAC:
        return ("macOS requires Screen Recording permission. Grant it under "
                "System Settings → Privacy & Security → Screen Recording.")
    if IS_LINUX:
        session = linux_session_type()
        if session == "wayland":
            return ("Wayland gates screen capture at the compositor: install "
                    "`grim` (wlroots compositors: Sway/Hyprland/labwc) or "
                    "xdg-desktop-portal + the portal backend for your "
                    "compositor. Per-window capture is not possible on "
                    "Wayland — AgentVision falls back to region/full-screen. "
                    "The portal path may show a one-time approval prompt. "
                    "(Input recording via /dev/input needs your user in the "
                    "`input` group.)")
        if session == "headless":
            return ("No display session detected (DISPLAY / WAYLAND_DISPLAY "
                    "unset) — screen capture is unavailable on this box.")
        return ("X11 does not gate screen capture — no permission grant is "
                "required. (Input recording via /dev/input needs your user "
                "in the `input` group: sudo usermod -aG input $USER.)")
    return "No special screen-capture permission is required on this OS."
