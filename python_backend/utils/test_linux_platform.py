"""
Deterministic mock-tests of the LINUX platform layer — runs on macOS/Windows,
so the Linux port is proven before it ever touches a real Artix box (the same
approach as daemon/test_win_input_sim.py for the Windows port).

What it proves:
  • Session detection (linux_session_type) routes X11 / Wayland / headless
    correctly from the env, incl. the XWayland tie-break (WAYLAND_DISPLAY
    wins over DISPLAY) and `startx` (DISPLAY set, XDG_SESSION_TYPE=tty).
  • Capture-backend selection (linux_capture_chain): mss→maim→scrot→import on
    X11; grim→gnome-screenshot→spectacle→portal on Wayland; mss NEVER offered
    on Wayland; nothing on headless.
  • CLI capture argv construction (_linux_backend_cmd) for every backend, in
    both region and full-screen forms, incl. which backends need a post-crop.
  • xdg-desktop-portal Response-signal parsing (_parse_portal_response_line).
  • X11 window-enumeration parsing: wmctrl -lpGx lines, xdotool --shell
    geometry, xwininfo output; candidate matching (_linux_window_matches).
  • Dispatch: on (simulated) Linux+Wayland, find_window returns None and
    window_enum_selftest reports ok=None with the Wayland explanation; on
    headless, capture_frame raises the clear no-display RuntimeError.
  • Input daemon: per-DEVICE physical/synthetic classification
    (classify_linux_device — uinput/virtual ⇒ synthetic), the pure evdev
    decoder (keys / clicks / wheel / rel+abs moves / autorepeat / ignores),
    and LinuxInputBackend.handle_event through the SAME physical/synthetic
    gate contract the mac/win backends use (drop physical by default, always
    record synthetic, opt-in records physical), position accumulation and
    clamping, event_ms passthrough, and device scan/hotplug/permission logic
    via a fully fake evdev module.
  • _run_linux degrades with exit code 2 when python-evdev is missing (this
    very box), never crashing — the bridge keeps working.

Run: python3 python_backend/utils/test_linux_platform.py
"""
from __future__ import annotations
import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))          # python_backend/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))   # repo root

from utils import platform_shim as ps       # noqa: E402
from daemon import input_daemon as d        # noqa: E402

_fails = 0


def check(name, cond, detail=""):
    global _fails
    if not cond:
        _fails += 1
    print(f"  [{'ok ' if cond else 'FAIL'}] {name}"
          f"{'' if cond or not detail else '  — ' + detail}")


# ── 1. Session detection ─────────────────────────────────────────────────────

def test_session_detection():
    print("session detection (X11 / Wayland / headless):")
    cases = [
        ({}, "headless"),
        ({"DISPLAY": ":0"}, "x11"),
        ({"XDG_SESSION_TYPE": "x11"}, "x11"),
        ({"XDG_SESSION_TYPE": "tty", "DISPLAY": ":0"}, "x11"),   # startx
        ({"WAYLAND_DISPLAY": "wayland-1"}, "wayland"),
        ({"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":0"}, "wayland"),  # XWayland
        ({"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}, "wayland"),
        ({"XDG_SESSION_TYPE": "Wayland"}, "wayland"),            # case-insensitive
        ({"XDG_SESSION_TYPE": "tty"}, "headless"),
        ({"WAYLAND_DISPLAY": "   "}, "headless"),                # blank ignored
        ({"DISPLAY": "  "}, "headless"),
    ]
    for env, want in cases:
        got = ps.linux_session_type(env)
        check(f"{env or '{}'} → {want}", got == want, f"got {got}")


# ── 2. Capture chain selection ───────────────────────────────────────────────

def test_capture_chain():
    print("capture-backend selection (linux_capture_chain):")
    all_tools = lambda t: f"/usr/bin/{t}"           # noqa: E731
    no_tools = lambda t: None                       # noqa: E731

    c = ps.linux_capture_chain("x11", has_mss=True, which=all_tools)
    check("x11 full chain order", c == ["mss", "maim", "scrot", "import"], str(c))
    c = ps.linux_capture_chain("x11", has_mss=False, which=all_tools)
    check("x11 without mss → CLI fallbacks", c == ["maim", "scrot", "import"], str(c))
    c = ps.linux_capture_chain("x11", has_mss=False,
                               which=lambda t: "/x" if t == "scrot" else None)
    check("x11 only scrot present", c == ["scrot"], str(c))
    c = ps.linux_capture_chain("x11", has_mss=False, which=no_tools)
    check("x11 nothing available → empty", c == [], str(c))

    c = ps.linux_capture_chain("wayland", has_mss=True, which=all_tools)
    check("wayland chain order (grim first, portal last)",
          c == ["grim", "gnome-screenshot", "spectacle", "portal"], str(c))
    check("mss NEVER offered on wayland", "mss" not in c)
    c = ps.linux_capture_chain("wayland", has_mss=True,
                               which=lambda t: "/x" if t == "gdbus" else None)
    check("wayland with only gdbus → portal fallback", c == ["portal"], str(c))
    c = ps.linux_capture_chain("wayland", has_mss=True, which=no_tools)
    check("wayland nothing available → empty", c == [], str(c))

    c = ps.linux_capture_chain("headless", has_mss=True, which=all_tools)
    check("headless → empty chain", c == [], str(c))


# ── 3. CLI capture argv construction ─────────────────────────────────────────

def test_backend_cmds():
    print("capture argv construction (_linux_backend_cmd):")
    p = "/tmp/f.png"
    region = {"left": 10, "top": 20, "width": 300, "height": 200}

    argv, crop = ps._linux_backend_cmd("maim", p, region)
    check("maim region", argv == ["maim", "-g", "300x200+10+20", p] and not crop, str(argv))
    argv, crop = ps._linux_backend_cmd("maim", p, None)
    check("maim full", argv == ["maim", p] and not crop, str(argv))

    argv, crop = ps._linux_backend_cmd("scrot", p, region)
    check("scrot region (-a x,y,w,h)",
          argv == ["scrot", "-o", "-a", "10,20,300,200", p] and not crop, str(argv))
    argv, crop = ps._linux_backend_cmd("scrot", p, None)
    check("scrot full (overwrite)", argv == ["scrot", "-o", p] and not crop, str(argv))

    argv, crop = ps._linux_backend_cmd("import", p, region)
    check("import region (root + crop + repage)",
          argv == ["import", "-window", "root", "-crop", "300x200+10+20",
                   "+repage", p] and not crop, str(argv))
    argv, crop = ps._linux_backend_cmd("import", p, None)
    check("import full", argv == ["import", "-window", "root", p] and not crop, str(argv))

    argv, crop = ps._linux_backend_cmd("grim", p, region)
    check("grim region ('X,Y WxH')",
          argv == ["grim", "-g", "10,20 300x200", p] and not crop, str(argv))
    argv, crop = ps._linux_backend_cmd("grim", p, None)
    check("grim full", argv == ["grim", p] and not crop, str(argv))

    argv, crop = ps._linux_backend_cmd("gnome-screenshot", p, region)
    check("gnome-screenshot full-grab + post-crop",
          argv == ["gnome-screenshot", "-f", p] and crop is True, str((argv, crop)))
    argv, crop = ps._linux_backend_cmd("spectacle", p, region)
    check("spectacle full-grab + post-crop",
          argv == ["spectacle", "-b", "-n", "-o", p] and crop is True, str((argv, crop)))
    argv, crop = ps._linux_backend_cmd("gnome-screenshot", p, None)
    check("gnome-screenshot full, no crop needed", crop is False, str(crop))

    check("mss is not a CLI backend", ps._linux_backend_cmd("mss", p, region) == (None, False))
    check("portal is not a CLI backend", ps._linux_backend_cmd("portal", p, None) == (None, False))


# ── 4. Portal response parsing ───────────────────────────────────────────────

def test_portal_parse():
    print("xdg-desktop-portal Response parsing:")
    line = ("/org/freedesktop/portal/desktop/request/1_42/agentvision99: "
            "org.freedesktop.portal.Request.Response "
            "(uint32 0, {'uri': <'file:///home/u/Pictures/Screenshot%20x.png'>})")
    check("uri extracted",
          ps._parse_portal_response_line(line)
          == "file:///home/u/Pictures/Screenshot%20x.png")
    check("non-Response line → None",
          ps._parse_portal_response_line("signal time=1 sender=:1.2 path=/x") is None)
    check("Response without uri → None",
          ps._parse_portal_response_line(
              "…Request.Response (uint32 1, {})") is None)
    check("empty/None-ish input → None", ps._parse_portal_response_line("") is None)


# ── 5. X11 enumeration parsing + matching ────────────────────────────────────

def test_x11_parsers():
    print("wmctrl / xdotool / xwininfo parsing:")
    wm = ("0x03400003  0 1234   10 20  800 600  Navigator.firefox  host Mozilla Firefox\n"
          "0x0000dead -1 0      0 0    50 50    tint2.tint2        host tint2 panel\n"
          "garbage line that should be skipped\n"
          "0x00a00007  1 4321   -5 30  1024 768 sharpemu.SharpEmu  host SharpEmu — Sarah\n")
    wins = ps._parse_wmctrl_list(wm)
    check("wmctrl parsed 3 rows", len(wins) == 3, str(len(wins)))
    w0 = wins[0]
    check("wmctrl fields (hex wid, pid, geometry, class, title)",
          w0["wid"] == 0x03400003 and w0["pid"] == 1234 and
          (w0["x"], w0["y"], w0["w"], w0["h"]) == (10, 20, 800, 600) and
          w0["wm_class"] == "Navigator.firefox" and w0["title"] == "Mozilla Firefox",
          str(w0))
    check("wmctrl sticky/dock row keeps desktop=-1 marker", wins[1]["desktop"] == -1)
    check("wmctrl negative x parsed", wins[2]["x"] == -5, str(wins[2]))

    geo = ps._parse_xdotool_shell_geometry(
        "WINDOW=52428803\nX=100\nY=200\nWIDTH=640\nHEIGHT=480\nSCREEN=0\n")
    check("xdotool --shell geometry", geo == (100, 200, 640, 480), str(geo))
    check("xdotool incomplete → None",
          ps._parse_xdotool_shell_geometry("X=1\nY=2\nWIDTH=3\n") is None)

    xw = ("xwininfo: Window id: 0x3400003 \"Mozilla Firefox\"\n"
          "  Absolute upper-left X:  100\n"
          "  Absolute upper-left Y:  200\n"
          "  Relative upper-left X:  0\n"
          "  Width: 640\n"
          "  Height: 480\n")
    check("xwininfo geometry", ps._parse_xwininfo_geometry(xw) == (100, 200, 640, 480))
    check("xwininfo garbage → None", ps._parse_xwininfo_geometry("nope") is None)

    print("window matching (_match_candidates + _linux_window_matches):")
    cands = ps._match_candidates("SharpEmu")
    check("title match", ps._linux_window_matches(cands, "SharpEmu — Sarah", "", ""))
    check("class match", ps._linux_window_matches(cands, "", "sharpemu.SharpEmu", ""))
    check("proc match", ps._linux_window_matches(cands, "", "", "sharpemu"))
    check("no match", not ps._linux_window_matches(cands, "Files", "nautilus", "nautilus"))
    cands2 = ps._match_candidates("firefox-ng")
    check("candidate stripping (-ng)", ps._linux_window_matches(cands2, "", "firefox", ""))


# ── 6. Dispatch under simulated Linux sessions ───────────────────────────────

class _EnvPatch:
    """Temporarily force IS_* flags + session env vars on platform_shim."""
    KEYS = ("WAYLAND_DISPLAY", "DISPLAY", "XDG_SESSION_TYPE")

    def __init__(self, session_env: dict):
        self.session_env = session_env

    def __enter__(self):
        self.old_flags = (ps.IS_MAC, ps.IS_WINDOWS, ps.IS_LINUX)
        self.old_env = {k: os.environ.get(k) for k in self.KEYS}
        ps.IS_MAC = ps.IS_WINDOWS = False
        ps.IS_LINUX = True
        for k in self.KEYS:
            os.environ.pop(k, None)
        os.environ.update(self.session_env)
        return self

    def __exit__(self, *a):
        ps.IS_MAC, ps.IS_WINDOWS, ps.IS_LINUX = self.old_flags
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def test_linux_dispatch():
    print("dispatch on simulated Linux sessions:")
    with _EnvPatch({"WAYLAND_DISPLAY": "wayland-1"}):
        check("find_window → None on Wayland", ps.find_window("firefox") is None)
        out = ps.window_enum_selftest("firefox")
        check("window_enum_selftest ok=None on Wayland (not a failure)",
              out["ok"] is None and out.get("session") == "wayland", str(out))
        check("…detail explains the Wayland limitation",
              "Wayland" in out["detail"] and "region/full-screen" in out["detail"],
              out["detail"])
        check("frontmost_app empty on Wayland", ps.frontmost_app() == "")
        check("front_window_title empty on Wayland", ps.front_window_title() == "")
        check("list_window_titles empty on Wayland", ps.list_window_titles() == [])
        note = ps.screen_capture_permission_note()
        check("permission note mentions grim/portal + no per-window",
              "grim" in note and "Per-window" in note, note[:80])

    with _EnvPatch({}):   # headless
        try:
            ps.capture_frame("/tmp/av_test_never_written.png")
            check("capture_frame raises on headless", False)
        except RuntimeError as e:
            check("capture_frame raises on headless", True)
            check("…with a clear no-display message",
                  "no display session" in str(e).lower()
                  or "DISPLAY" in str(e), str(e))
        out = ps.window_enum_selftest()
        check("window_enum_selftest headless → ok False + display hint",
              out["ok"] is False and "display" in out["detail"].lower(), str(out))
        sess = ps.linux_session_selftest()
        check("linux_session_selftest headless shape",
              sess["check"] == "linux_session" and sess["ok"] is False
              and sess["session"] == "headless" and "capture_chain" in sess,
              str({k: sess[k] for k in ('ok', 'session')}))

    # capture_backend_name is session-aware (no tools → none(session))
    orig_which = ps.shutil.which
    ps.shutil.which = lambda t: None
    try:
        with _EnvPatch({"WAYLAND_DISPLAY": "wayland-1"}):
            check("capture_backend_name none(wayland) with no tools",
                  ps.capture_backend_name() == "none(wayland)",
                  ps.capture_backend_name())
        with _EnvPatch({}):
            check("capture_backend_name none(headless)",
                  ps.capture_backend_name() == "none(headless)",
                  ps.capture_backend_name())
    finally:
        ps.shutil.which = orig_which

    # off-Linux (this box): linux_session_selftest is not-applicable
    sess = ps.linux_session_selftest()
    check("linux_session_selftest ok=None off-Linux", sess["ok"] is None, str(sess))


# ── 7. Device classification (per-device physical/synthetic) ─────────────────

def test_classify_device():
    print("classify_linux_device (per-DEVICE physical vs synthetic):")
    check("real USB mouse → physical",
          d.classify_linux_device("Logitech G502", "usb-0000:00:14.0-1/input0",
                                  "/sys/devices/pci0000:00/…/input/input5")
          == "physical")
    check("bluetooth kbd → physical",
          d.classify_linux_device("Keychron K2", "aa:bb:cc:dd:ee:ff",
                                  "/sys/devices/…/bluetooth/…/input7")
          == "physical")
    check("uinput virtual sysfs → synthetic",
          d.classify_linux_device("something", "phys-set-but-virtual",
                                  "/sys/devices/virtual/input/input99")
          == "synthetic")
    check("empty phys → synthetic",
          d.classify_linux_device("mystery", "", "/sys/devices/pci…/input3")
          == "synthetic")
    check("py-evdev-uinput name → synthetic",
          d.classify_linux_device("py-evdev-uinput", "x", "/sys/devices/pci…")
          == "synthetic")
    check("ydotool name → synthetic",
          d.classify_linux_device("ydotoold virtual device", "x",
                                  "/sys/devices/pci…") == "synthetic")


# ── 8. Pure evdev decoder ────────────────────────────────────────────────────

def test_decode_evdev():
    print("decode_evdev_event (kernel code → unified event):")
    e = d.decode_evdev_event(d.EV_KEY, 30, 1)      # KEY_A down
    check("key down", e["kind"] == "key" and e["source"] == "av.daemon.key.press"
          and e["data"] == {"keycode": 30, "phase": "down"}, str(e))
    e = d.decode_evdev_event(d.EV_KEY, 30, 0)
    check("key up", e["source"] == "av.daemon.key.release"
          and e["data"]["phase"] == "up", str(e))
    e = d.decode_evdev_event(d.EV_KEY, 30, 2)      # autorepeat
    check("autorepeat → down + repeat flag (win/mac parity)",
          e["data"]["phase"] == "down" and e["data"].get("repeat") is True, str(e))
    check("key bogus value → None", d.decode_evdev_event(d.EV_KEY, 30, 5) is None)

    e = d.decode_evdev_event(d.EV_KEY, d.BTN_LEFT, 1)
    check("BTN_LEFT down", e["kind"] == "click" and e["data"]["button"] == "left"
          and e["data"]["phase"] == "down", str(e))
    e = d.decode_evdev_event(d.EV_KEY, d.BTN_RIGHT, 0)
    check("BTN_RIGHT up", e["data"]["button"] == "right" and e["data"]["phase"] == "up")
    check("BTN_MIDDLE", d.decode_evdev_event(d.EV_KEY, d.BTN_MIDDLE, 1)["data"]["button"] == "middle")
    check("BTN_SIDE → x1", d.decode_evdev_event(d.EV_KEY, d.BTN_SIDE, 1)["data"]["button"] == "x1")
    check("BTN_EXTRA → x2", d.decode_evdev_event(d.EV_KEY, d.BTN_EXTRA, 1)["data"]["button"] == "x2")
    e = d.decode_evdev_event(d.EV_KEY, 0x115, 1)   # BTN_FORWARD
    check("unnamed BTN in mouse range → btn0x115", e["data"]["button"] == "btn0x115", str(e))
    check("BTN outside mouse range → None",
          d.decode_evdev_event(d.EV_KEY, 0x120, 1) is None)   # BTN_JOYSTICK

    e = d.decode_evdev_event(d.EV_REL, d.REL_WHEEL, -1)
    check("wheel dy=-1", e["kind"] == "scroll" and e["data"] == {"dx": 0, "dy": -1}, str(e))
    e = d.decode_evdev_event(d.EV_REL, d.REL_HWHEEL, 2)
    check("hwheel dx=2", e["data"] == {"dx": 2, "dy": 0}, str(e))
    e = d.decode_evdev_event(d.EV_REL, d.REL_X, -7)
    check("REL_X delta", e == {"kind": "move_rel", "axis": "x", "delta": -7}, str(e))
    e = d.decode_evdev_event(d.EV_REL, d.REL_Y, 4)
    check("REL_Y delta", e == {"kind": "move_rel", "axis": "y", "delta": 4}, str(e))
    check("hi-res wheel (0x0b) ignored — dupe of REL_WHEEL",
          d.decode_evdev_event(d.EV_REL, 0x0B, 120) is None)

    e = d.decode_evdev_event(d.EV_ABS, d.ABS_X, 500)
    check("ABS_X", e == {"kind": "move_abs", "axis": "x", "value": 500}, str(e))
    e = d.decode_evdev_event(d.EV_ABS, d.ABS_Y, 300)
    check("ABS_Y", e == {"kind": "move_abs", "axis": "y", "value": 300}, str(e))
    check("other ABS axis ignored", d.decode_evdev_event(d.EV_ABS, 24, 1) is None)

    check("EV_SYN ignored", d.decode_evdev_event(d.EV_SYN, 0, 0) is None)
    check("unknown type ignored", d.decode_evdev_event(0x15, 0, 1) is None)  # EV_LED-ish


# ── 9. Backend event routing through the shared gate ─────────────────────────

def _recording_backend(initial_pos=(0, 0)):
    emitted, moves = [], []
    state = {"opt_in": False}

    def emit(category, source, data, coords=None, event_ms=None):
        emitted.append({"category": category, "source": source, "data": data,
                        "coords": coords, "event_ms": event_ms})

    def gate(is_physical):
        if is_physical:
            if state["opt_in"]:
                return True, 0, "physical"
            return False, 0, "physical"
        return True, -1, "synthetic"

    def set_move(x, y, phys, ms):
        moves.append((x, y, phys, ms))

    b = d.LinuxInputBackend(None, emit=emit, gate=gate,
                            set_pending_move=set_move,
                            sysfs_resolver=lambda p: "",
                            initial_pos=initial_pos)
    return b, emitted, moves, state


def test_backend_routing():
    print("LinuxInputBackend.handle_event (gate + position + schema):")
    b, emitted, moves, state = _recording_backend()

    # physical key, opt_in False → dropped (the product's core default)
    b.handle_event(True, d.EV_KEY, 30, 1, 1000.0)
    check("physical key dropped by default", emitted == [], str(emitted))
    # synthetic key → always recorded
    b.handle_event(False, d.EV_KEY, 31, 1, 2000.0, device_name="py-evdev-uinput")
    check("synthetic key emitted", len(emitted) == 1, str(emitted))
    ev = emitted[-1]
    check("key schema (keycode/phase/kind/source_pid/device)",
          ev["data"]["keycode"] == 31 and ev["data"]["phase"] == "down"
          and ev["data"]["kind"] == "synthetic" and ev["data"]["source_pid"] == -1
          and ev["data"]["device"] == "py-evdev-uinput", str(ev["data"]))
    check("event_ms passthrough", ev["event_ms"] == 2000.0, str(ev["event_ms"]))

    # opt-in physical
    state["opt_in"] = True
    b.handle_event(True, d.EV_KEY, 30, 0, 3000.0)
    ev = emitted[-1]
    check("opt-in physical keyup emitted as physical",
          ev["source"] == "av.daemon.key.release" and ev["data"]["kind"] == "physical"
          and ev["data"]["source_pid"] == 0, str(ev["data"]))
    state["opt_in"] = False

    # moves: accumulate relative deltas, clamp at 0, ABS sets position
    b.handle_event(False, d.EV_REL, d.REL_X, 10, 4000.0)
    check("REL_X accumulates → (10,0)", moves[-1][:2] == (10, 0), str(moves[-1]))
    b.handle_event(False, d.EV_REL, d.REL_Y, 5, 4001.0)
    check("REL_Y accumulates → (10,5)", moves[-1][:2] == (10, 5), str(moves[-1]))
    b.handle_event(False, d.EV_REL, d.REL_X, -50, 4002.0)
    check("negative delta clamps at 0 → (0,5)", moves[-1][:2] == (0, 5), str(moves[-1]))
    check("move is_physical flag carried", moves[-1][2] is False, str(moves[-1]))
    b.handle_event(True, d.EV_ABS, d.ABS_X, 500, 4003.0)
    b.handle_event(True, d.EV_ABS, d.ABS_Y, 300, 4004.0)
    check("ABS sets position → (500,300)", moves[-1][:2] == (500, 300), str(moves[-1]))
    check("physical move reaches coalescer (gate applies at flush, like mac/win)",
          moves[-1][2] is True, str(moves[-1]))
    check("moves never emitted directly", all(m["category"] != "mouse" or
          m["source"] != "av.daemon.mouse.move" for m in emitted))

    # click carries current position as coords
    b.handle_event(False, d.EV_KEY, d.BTN_LEFT, 1, 5000.0)
    ev = emitted[-1]
    check("click stamped with tracked position",
          ev["data"]["x"] == 500 and ev["data"]["y"] == 300
          and ev["coords"] == {"x": 500, "y": 300}, str(ev))

    # scroll
    b.handle_event(False, d.EV_REL, d.REL_WHEEL, -1, 6000.0)
    ev = emitted[-1]
    check("scroll emitted with dx/dy + coords",
          ev["source"] == "av.daemon.mouse.scroll" and ev["data"]["dy"] == -1
          and ev["coords"] == {"x": 500, "y": 300}, str(ev))

    # initial_pos anchor honored
    b2, _, moves2, _ = _recording_backend(initial_pos=(100, 200))
    b2.handle_event(False, d.EV_REL, d.REL_X, 1, None)
    check("initial_pos anchors accumulation → (101,200)",
          moves2[-1][:2] == (101, 200), str(moves2[-1]))


# ── 10. Device scan / hotplug / permissions with a fake evdev ────────────────

class FakeDev:
    def __init__(self, path, name, phys, caps=None):
        self.path, self.name, self.phys = path, name, phys
        self.fd = abs(hash(path)) & 0xFFFF
        self._caps = caps if caps is not None else {d.EV_KEY: [30]}
        self.closed = False

    def capabilities(self):
        return self._caps

    def close(self):
        self.closed = True


class FakeEvdev:
    def __init__(self, devs, denied=()):
        self.devs = {dv.path: dv for dv in devs}
        self.denied = set(denied)

    def list_devices(self):
        return sorted(self.devs) + sorted(self.denied)

    def InputDevice(self, path):
        if path in self.denied:
            raise PermissionError(13, "Permission denied", path)
        return self.devs[path]


def test_device_scan():
    print("device scan / hotplug / permission handling (fake evdev):")
    sysfs = {
        "/dev/input/event0": "/sys/devices/pci0000:00/usb1/…/input/input0",
        "/dev/input/event1": "/sys/devices/virtual/input/input99",
        "/dev/input/event2": "/sys/devices/pci0000:00/usb2/…/input/input2",
        "/dev/input/event3": "/sys/devices/pci0000:00/usb3/…/input/input3",
    }
    kb = FakeDev("/dev/input/event0", "AT Keyboard", "isa0060/serio0/input0")
    ui = FakeDev("/dev/input/event1", "py-evdev-uinput", "")
    fx = FakeDev("/dev/input/event3", "PC Speaker", "isa0061/input",
                 caps={0x12: []})     # EV_SND only — not an input device
    fake = FakeEvdev([kb, ui, fx], denied={"/dev/input/event2"})

    b, _, _, _ = _recording_backend()
    b.evdev = fake
    b._sysfs = lambda p: sysfs.get(p, "")

    opened, denied = b.scan_devices()
    check("opened 2 (kbd + uinput), 1 denied, speaker skipped",
          opened == 2 and denied == 1, f"opened={opened} denied={denied}")
    check("keyboard classified physical", b.devices["/dev/input/event0"][1] is True)
    check("uinput classified synthetic", b.devices["/dev/input/event1"][1] is False)
    check("non-input device not kept", "/dev/input/event3" not in b.devices)
    check("denied path recorded", b.perm_denied == ["/dev/input/event2"])

    # hotplug: a bot creates its uinput device AFTER daemon start
    bot = FakeDev("/dev/input/event9", "ydotoold virtual device", "")
    fake.devs[bot.path] = bot
    sysfs[bot.path] = "/sys/devices/virtual/input/input120"
    opened, _ = b.scan_devices()
    check("rescan picks up hotplugged bot device", opened == 3
          and b.devices["/dev/input/event9"][1] is False, str(opened))

    # unplug: device disappears from the listing
    del fake.devs["/dev/input/event0"]
    opened, _ = b.scan_devices()
    check("rescan drops vanished device + closes it",
          opened == 2 and kb.closed, str(opened))

    # rescan is idempotent
    opened2, denied2 = b.scan_devices()
    check("rescan idempotent", opened2 == 2 and denied2 == 1,
          f"opened={opened2} denied={denied2}")

    # all-denied → (0, N) so _run_linux can exit 3 with the input-group help
    b3, _, _, _ = _recording_backend()
    b3.evdev = FakeEvdev([], denied={"/dev/input/event0", "/dev/input/event1"})
    b3._sysfs = lambda p: ""
    opened, denied = b3.scan_devices()
    check("all permission-denied → (0, 2)", opened == 0 and denied == 2,
          f"opened={opened} denied={denied}")


# ── 11. Graceful degradation on a box without evdev ─────────────────────────

def test_run_linux_degrades():
    print("_run_linux degrades cleanly without python-evdev:")
    if importlib.util.find_spec("evdev") is not None:
        print("  [skip] evdev IS installed here — degradation path not testable")
        return
    rc = d._run_linux()
    check("returns exit code 2 (like the mac missing-Quartz path), no crash",
          rc == 2, str(rc))


if __name__ == "__main__":
    print("=" * 70)
    print("linux platform + input backend — mock tests (run on any OS)")
    print("=" * 70)
    test_session_detection()
    test_capture_chain()
    test_backend_cmds()
    test_portal_parse()
    test_x11_parsers()
    test_linux_dispatch()
    test_classify_device()
    test_decode_evdev()
    test_backend_routing()
    test_device_scan()
    test_run_linux_degrades()
    print("=" * 70)
    if _fails:
        print(f"RESULT: {_fails} FAILURE(S)")
        sys.exit(1)
    print("RESULT: all linux-platform tests passed")
    sys.exit(0)
