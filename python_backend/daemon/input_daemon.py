"""
AgentVision system-wide input daemon (cross-platform: Windows + macOS + Linux).

Long-running process that taps the OS at the lowest input layer and routes
every keyboard/mouse event into the JSONL sink of the CURRENTLY ACTIVE
project — but, by default, ONLY events that did NOT originate from physical
hardware.

Why filter physical hardware out by default
───────────────────────────────────────────
The recorder is meant to capture what the BRIDGED PROGRAM does, not what the
human at the keyboard does. A bot driving the screen (pyautogui / SendInput /
cliclick / Quartz / robotjs / AppleScript) posts SYNTHETIC events; the user's
USB/Bluetooth keyboard and mouse post PHYSICAL events. Recording the human's
inputs would pollute diagnostics for the bot, so physical events are dropped
unless the per-profile `capture_user_input` flag is turned on (manual play,
RPA recording, demo capture — when the USER is the agent).

How each OS distinguishes physical from synthetic
──────────────────────────────────────────────────
  • macOS  — CGEventTap. `kCGEventSourceUnixProcessID == 0` ⇒ physical HID;
             any non-zero PID ⇒ a userland process synthesized it.
  • Windows — WH_KEYBOARD_LL / WH_MOUSE_LL low-level hooks. The event struct
             carries an "injected" flag (LLKHF_INJECTED 0x10 for keys,
             LLMHF_INJECTED 0x01 for mouse). Injected ⇒ synthetic; not
             injected ⇒ physical hardware. This is the exact analogue of the
             mac PID==0 test.
  • Linux  — evdev (/dev/input/event*). The kernel does NOT tag individual
             events, so the split is PER DEVICE: a real HID device (USB/BT —
             non-empty `phys`, sysfs under a real bus) is physical; a
             uinput-created virtual device (sysfs under /devices/virtual/,
             empty `phys` — what ydotool / python-uinput / evemu create) is
             synthetic. LIMITATION (documented, surfaced in the selftest):
             (a) the injector PID is never known (source_pid = -1, like
             Windows); (b) injection that bypasses /dev/input entirely —
             X11 XTEST (xdotool's default) or Wayland virtual-pointer
             protocols — is invisible to this backend; use a uinput-based
             injector (ydotool) for recordable synthetic input.

So on every OS: the recorder is ALWAYS LISTENING (the bot's synthetic events
are always captured), and the human's physical inputs are filtered out at the
OS layer unless explicitly opted-in.

Lifecycle: managed by the GUI Permissions tab, or `python -m
python_backend.cli daemon start`. On macOS a one-time Accessibility grant is
required; on Windows no special permission is needed (installing a global
low-level hook works for a normal user process); on Linux the user must be
able to read /dev/input/event* (add them to the `input` group).
"""
from __future__ import annotations

import atexit
import ctypes
import json
import os
import signal
import sys
import threading
import time
from ctypes import wintypes   # cross-platform in CPython (type aliases only)
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

AV_ROOT = Path(__file__).resolve().parent.parent.parent
PROFILES_FILE = AV_ROOT / "python_backend" / "profiles.json"
ACTIVE_FILE = AV_ROOT / "python_backend" / "active_profile.txt"

# shared.clock + platform_shim: one source of truth for time + OS specifics.
sys.path.insert(0, str(AV_ROOT))
sys.path.insert(0, str(AV_ROOT / "python_backend"))
from shared import clock as _shared_clock  # noqa: E402
from utils import platform_shim  # noqa: E402

PID_FILE = Path(os.environ.get("AGENTVISION_DAEMON_PID_FILE",
                               str(platform_shim.daemon_pid_file())))

COALESCE_MS = 100          # mouse move emit cadence
PROFILE_REFRESH_S = 2.0    # how often to re-check active profile + opt-in flag


# ── State ─────────────────────────────────────────────────────────────────────

_RUN_ID = f"avd-{int(time.time() * 1000)}-{os.getpid()}"
_lock = threading.Lock()
_active_sink: Path | None = None
_active_profile_name: str = ""
_capture_user_input: bool = False        # per-profile opt-in
_sinks_loaded_at = 0.0
_pending_move: tuple[int, int, bool, float | None] | None = None  # (x,y,is_physical,event_ms)
_move_lock = threading.Lock()
_write_lock = threading.Lock()   # serialises JSONL appends across emitter threads

# Counters for diagnostic visibility (printed periodically)
_dropped_physical = 0
_emitted_synthetic = 0
_emitted_physical_optin = 0


def _now() -> tuple[str, float]:
    return _shared_clock.now()


def _ms_to_iso(ms: float) -> str:
    return _shared_clock.ms_to_iso(ms)


# ── Active-profile + opt-in resolution ──────────────────────────────────────

def _read_active_profile_name() -> str:
    try:
        if ACTIVE_FILE.exists():
            return ACTIVE_FILE.read_text().strip()
    except Exception:
        pass
    return os.environ.get("AGENTVISION_DAEMON_PROFILE", "")


def _refresh_target() -> None:
    """Re-read active profile + its capture_user_input opt-in flag.

    The sink is set as long as the profile defines an action_log_file.
    The opt-in flag controls only whether PHYSICAL events get recorded;
    synthetic events from the bridged bot are ALWAYS recorded.
    """
    global _active_sink, _active_profile_name, _capture_user_input, _sinks_loaded_at
    now = time.time()
    if now - _sinks_loaded_at < PROFILE_REFRESH_S:
        return
    _sinks_loaded_at = now

    name = _read_active_profile_name()
    sink: Path | None = None
    capture = False
    try:
        if name and PROFILES_FILE.exists():
            data = json.loads(PROFILES_FILE.read_text())
            prof = data.get(name)
            if prof:
                p = prof.get("action_log_file")
                if p:
                    sp = Path(p)
                    try:
                        sp.parent.mkdir(parents=True, exist_ok=True)
                        sink = sp
                    except Exception:
                        pass
                capture = bool(prof.get("capture_user_input"))
    except Exception:
        pass

    with _lock:
        if (name != _active_profile_name
                or sink != _active_sink
                or capture != _capture_user_input):
            print(f"input_daemon: target → profile='{name}' "
                  f"sink={sink} capture_user_input={capture}", flush=True)
        _active_profile_name = name
        _active_sink = sink
        _capture_user_input = capture


def _emit(category: str, source: str, data: dict, coords=None,
          event_ms: float | None = None) -> None:
    """Write one record. If `event_ms` is supplied (UTC epoch ms — derived
    from the OS event timestamp) we use it as the authoritative event time.
    Otherwise fall back to `_now()` at write time (synthetic process events,
    where there's no hardware clock)."""
    with _lock:
        sink = _active_sink
        profile = _active_profile_name
    if sink is None:
        return
    if event_ms is not None:
        ms = event_ms
        iso = _ms_to_iso(ms)
    else:
        iso, ms = _now()
    # Also stamp recv-time so a consumer can measure kernel→userland latency.
    _, recv_ms = _now()
    record = {
        "ts": iso,
        "ts_ms": ms,
        "ts_recv_ms": recv_ms,
        "category": category,
        "source": source,
        "state": None,
        "run_id": _RUN_ID,
        "trace_id": None,
        "frame_seq": None,
        "coords": coords,
        "data": {**data, "profile": profile},
    }
    try:
        line = json.dumps(record, default=str, ensure_ascii=False) + "\n"
    except Exception:
        return
    try:
        # Serialise appends: the emit-worker and move-flusher threads both call
        # _emit, and interleaved writes could corrupt a JSONL line on Windows.
        with _write_lock:
            with sink.open("a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


# ── Physical-vs-synthetic gate (OS-independent) ─────────────────────────────────
#
# `is_physical` is computed by each OS backend (mac: PID==0; win: NOT injected).
# Returns (should_record, source_pid, kind). source_pid is a schema-compat hint:
# 0 = physical hardware, -1 = synthetic/injected (injector PID not exposed by
# the low-level hook layer on Windows; the mac backend overrides it with the
# real injector PID).

def _gate(is_physical: bool) -> tuple[bool, int, str]:
    global _dropped_physical, _emitted_synthetic, _emitted_physical_optin
    with _lock:
        opt_in = _capture_user_input
    if is_physical:
        if opt_in:
            _emitted_physical_optin += 1
            return True, 0, "physical"
        _dropped_physical += 1
        return False, 0, "physical"
    _emitted_synthetic += 1
    return True, -1, "synthetic"


def _button_name(btn: int) -> str:
    return {0: "left", 1: "right", 2: "middle"}.get(int(btn), f"btn{btn}")


# ── Shared background helpers ───────────────────────────────────────────────────

def _move_flusher() -> None:
    """Emit at most one move per COALESCE_MS, keeping the latest position AND
    its hardware event timestamp so the recorded ts_ms is the moment the move
    actually happened, not the moment of this flush tick."""
    global _pending_move
    while True:
        time.sleep(COALESCE_MS / 1000.0)
        with _move_lock:
            pending = _pending_move
            _pending_move = None
        if pending is None:
            continue
        x, y, is_physical, ev_ms = pending
        record_it, src_pid, kind = _gate(is_physical)
        if not record_it:
            continue
        _emit("mouse", "av.daemon.mouse.move",
              {"x": x, "y": y, "source_pid": src_pid, "kind": kind},
              coords={"x": x, "y": y}, event_ms=ev_ms)


def _stats_logger() -> None:
    """Periodic diagnostic print so the user can see what's flowing."""
    last_d = last_s = last_p = 0
    while True:
        time.sleep(30.0)
        d, s, p = _dropped_physical, _emitted_synthetic, _emitted_physical_optin
        if d == last_d and s == last_s and p == last_p:
            continue
        print(f"input_daemon: stats Δ30s "
              f"dropped_physical={d - last_d} "
              f"emitted_synthetic={s - last_s} "
              f"emitted_physical_optin={p - last_p} "
              f"(totals d={d} s={s} p={p})", flush=True)
        last_d, last_s, last_p = d, s, p


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def _write_pidfile() -> None:
    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()))
    except Exception as e:
        print(f"input_daemon: cannot write pidfile {PID_FILE}: {e}",
              file=sys.stderr)
        sys.exit(1)


def _remove_pidfile() -> None:
    try:
        if PID_FILE.exists():
            content = PID_FILE.read_text().strip()
            if content == str(os.getpid()):
                PID_FILE.unlink()
    except Exception:
        pass


def _on_signal(signum, _frame):
    _emit("process", "av.daemon.exit", {"signal": signum, "pid": os.getpid()})
    _remove_pidfile()
    sys.exit(0)


# ═════════════════════════════════════════════════════════════════════════════════
#  Windows backend — WH_KEYBOARD_LL + WH_MOUSE_LL global low-level hooks
# ═════════════════════════════════════════════════════════════════════════════════
#
# The whole Windows control flow is factored so it can be EXERCISED ON macOS with
# mocked DLLs (see daemon/test_win_input_sim.py):
#   • The structs + constants + PURE decoders below use only ctypes + wintypes,
#     which are cross-platform in CPython, so a test can cast crafted
#     KBDLLHOOKSTRUCT / MSLLHOOKSTRUCT bytes and assert the decoded unified event.
#   • WinInputBackend takes INJECTABLE user32/kernel32; the test passes fakes.
#   • ctypes.WINFUNCTYPE / ctypes.WinDLL (Windows-only) are touched ONLY inside
#     install()/run(), which the mac test never calls (it calls the procs +
#     self_test handshake directly).

# ── Win32 constants (verified against MSDN) ─────────────────────────────────────
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0
LLKHF_INJECTED = 0x10          # KBDLLHOOKSTRUCT.flags: event was injected
LLMHF_INJECTED = 0x01          # MSLLHOOKSTRUCT.flags:  event was injected

WM_QUIT = 0x0012
PM_REMOVE = 0x0001
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_XBUTTONDOWN, WM_XBUTTONUP = 0x020B, 0x020C
WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x020A, 0x020E

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_F24 = 0x87                  # a key no physical keyboard/app uses — safe probe

# Magic dwExtraInfo stamped on our SendInput self-test events so the hook proc
# recognizes its OWN synthetic probe and never records it as real input.
SELFTEST_MARKER = 0x4156_5354   # "AVST"

# IMPORTANT — use FIXED-WIDTH ctypes types, NOT wintypes.DWORD/LONG/WORD. On
# Windows (LLP64) wintypes.DWORD is c_ulong == 4 bytes, but on 64-bit macOS/Linux
# (LP64) c_ulong == 8 bytes, which would BLOAT these structs and make the mac
# mock-sim's byte layout diverge from real Windows. c_uint32/c_int32/c_uint16 are
# 4/4/2 bytes on every platform, and c_size_t is pointer-sized (== ULONG_PTR:
# 8 on Win64, 4 on Win32) — so these layouts are byte-identical to the Win32 SDK
# on the real host AND faithful in the simulation.
_DWORD = ctypes.c_uint32
_LONG = ctypes.c_int32
_WORD = ctypes.c_uint16
_ULONG_PTR = ctypes.c_size_t   # UINT_PTR / ULONG_PTR — pointer-sized
LRESULT = ctypes.c_ssize_t


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", _DWORD), ("scanCode", _DWORD),
                ("flags", _DWORD), ("time", _DWORD),
                ("dwExtraInfo", _ULONG_PTR)]


class POINT(ctypes.Structure):
    _fields_ = [("x", _LONG), ("y", _LONG)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", POINT), ("mouseData", _DWORD),
                ("flags", _DWORD), ("time", _DWORD),
                ("dwExtraInfo", _ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", _WORD), ("wScan", _WORD),
                ("dwFlags", _DWORD), ("time", _DWORD),
                ("dwExtraInfo", _ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", _LONG), ("dy", _LONG),
                ("mouseData", _DWORD), ("dwFlags", _DWORD),
                ("time", _DWORD), ("dwExtraInfo", _ULONG_PTR)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", _DWORD), ("u", _INPUT_UNION)]


def _short(v: int) -> int:
    """Interpret the low 16 bits of v as a signed 16-bit int (wheel delta / XBUTTON)."""
    v &= 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


# ── PURE decoders (no ctypes DLL calls; fully testable on macOS) ─────────────────

def decode_kbd_event(wParam, lParam) -> dict | None:
    """Cast lParam→KBDLLHOOKSTRUCT and decode one keyboard event into a plain
    dict, or None if wParam is not a key up/down. NO side effects, NO DLL calls."""
    kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
    wp = int(wParam)
    if wp in (WM_KEYDOWN, WM_SYSKEYDOWN):
        phase, source = "down", "av.daemon.key.press"
    elif wp in (WM_KEYUP, WM_SYSKEYUP):
        phase, source = "up", "av.daemon.key.release"
    else:
        return None
    return {
        "is_physical": not bool(kb.flags & LLKHF_INJECTED),
        "category": "key", "source": source,
        "data": {"keycode": int(kb.vkCode), "scancode": int(kb.scanCode), "phase": phase},
        "tick": int(kb.time), "extra": int(kb.dwExtraInfo),
    }


def decode_mouse_event(wParam, lParam) -> dict | None:
    """Cast lParam→MSLLHOOKSTRUCT and decode one mouse event into a plain dict,
    or None if unrecognized. 'kind' ∈ {move,click,scroll}. NO side effects."""
    mst = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
    wp = int(wParam)
    x, y = int(mst.pt.x), int(mst.pt.y)
    is_physical = not bool(mst.flags & LLMHF_INJECTED)
    base = {"is_physical": is_physical, "x": x, "y": y,
            "tick": int(mst.time), "extra": int(mst.dwExtraInfo)}
    if wp == WM_MOUSEMOVE:
        return {**base, "kind": "move", "category": "mouse",
                "source": "av.daemon.mouse.move", "data": {"x": x, "y": y}}
    btn = None
    phase = None
    if wp in (WM_LBUTTONDOWN, WM_LBUTTONUP):
        btn, phase = "left", ("down" if wp == WM_LBUTTONDOWN else "up")
    elif wp in (WM_RBUTTONDOWN, WM_RBUTTONUP):
        btn, phase = "right", ("down" if wp == WM_RBUTTONDOWN else "up")
    elif wp in (WM_MBUTTONDOWN, WM_MBUTTONUP):
        btn, phase = "middle", ("down" if wp == WM_MBUTTONDOWN else "up")
    elif wp in (WM_XBUTTONDOWN, WM_XBUTTONUP):
        xbtn = (int(mst.mouseData) >> 16) & 0xFFFF
        btn, phase = f"x{xbtn}", ("down" if wp == WM_XBUTTONDOWN else "up")
    if btn is not None:
        return {**base, "kind": "click", "category": "mouse",
                "source": "av.daemon.mouse.click",
                "data": {"x": x, "y": y, "button": btn, "phase": phase}}
    if wp == WM_MOUSEWHEEL:
        dy = _short((int(mst.mouseData) >> 16) & 0xFFFF)
        return {**base, "kind": "scroll", "category": "mouse",
                "source": "av.daemon.mouse.scroll", "data": {"x": x, "y": y, "dx": 0, "dy": dy}}
    if wp == WM_MOUSEHWHEEL:
        dx = _short((int(mst.mouseData) >> 16) & 0xFFFF)
        return {**base, "kind": "scroll", "category": "mouse",
                "source": "av.daemon.mouse.scroll", "data": {"x": x, "y": y, "dx": dx, "dy": 0}}
    return None


def _hookproc_type():
    """Return the HOOKPROC ctypes type (WINFUNCTYPE stdcall) on Windows, or an
    identity wrapper on other OSes so the mac mock-sim can build a backend
    without WINFUNCTYPE. The identity form is never actually handed to a real
    SetWindowsHookEx (that path is Windows-only)."""
    if hasattr(ctypes, "WINFUNCTYPE"):
        return ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM)

    class _Identity:
        def __init__(self, fn):
            self.fn = fn

        def __call__(self, *a):
            return self.fn(*a)
    return _Identity


class WinInputBackend:
    """Production Windows low-level-hook backend, with injectable DLLs so the
    entire flow is unit-testable on macOS.

    Parameters
    ----------
    user32, kernel32 : the loaded DLLs (real WinDLLs in production; Mocks in tests)
    emit             : callable(category, source, data, coords=None, event_ms=None)
    gate             : callable(is_physical) -> (record_it, source_pid, kind)
    set_pending_move : callable(x, y, is_physical, event_ms)  (coalesced move sink)
    """

    def __init__(self, user32, kernel32, *, emit, gate, set_pending_move):
        self.user32 = user32
        self.kernel32 = kernel32
        self.emit = emit
        self.gate = gate
        self.set_pending_move = set_pending_move
        self.kbd_hook = None
        self.mouse_hook = None
        self._kbd_cb = None       # strong ref to the WINFUNCTYPE trampoline
        self._mouse_cb = None
        self._selftest_event = threading.Event()
        self._anchor_tick = 0
        self._anchor_wall_ms = time.time() * 1000.0
        self._running = False

    # ── time anchoring ──────────────────────────────────────────────────────
    def _tick_to_wall_ms(self, event_tick: int) -> float:
        d = (int(event_tick) - self._anchor_tick) & 0xFFFFFFFF
        if d >= 0x80000000:              # signed 32-bit delta (uptime wrap-safe)
            d -= 0x100000000
        return self._anchor_wall_ms + d

    # ── hook procedures (LRESULT CALLBACK proc(int, WPARAM, LPARAM)) ─────────
    def kbd_proc(self, nCode, wParam, lParam):
        # MSDN: if nCode < 0 the proc MUST pass to CallNextHookEx unprocessed.
        if nCode == HC_ACTION:
            try:
                dec = decode_kbd_event(wParam, lParam)
                if dec is not None:
                    if dec["extra"] == SELFTEST_MARKER:
                        self._selftest_event.set()      # our own probe — confirm & drop
                    else:
                        rec, src_pid, kind = self.gate(dec["is_physical"])
                        if rec:
                            d = dict(dec["data"]); d["source_pid"] = src_pid; d["kind"] = kind
                            self.emit(dec["category"], dec["source"], d,
                                      event_ms=self._tick_to_wall_ms(dec["tick"]))
            except Exception:
                pass
        return self.user32.CallNextHookEx(None, nCode, wParam, lParam)

    def mouse_proc(self, nCode, wParam, lParam):
        if nCode == HC_ACTION:
            try:
                dec = decode_mouse_event(wParam, lParam)
                if dec is not None:
                    ev_ms = self._tick_to_wall_ms(dec["tick"])
                    if dec["kind"] == "move":
                        self.set_pending_move(dec["x"], dec["y"], dec["is_physical"], ev_ms)
                    elif dec["extra"] == SELFTEST_MARKER:
                        self._selftest_event.set()
                    else:
                        rec, src_pid, kind = self.gate(dec["is_physical"])
                        if rec:
                            d = dict(dec["data"]); d["source_pid"] = src_pid; d["kind"] = kind
                            self.emit(dec["category"], dec["source"], d,
                                      coords={"x": dec["x"], "y": dec["y"]}, event_ms=ev_ms)
            except Exception:
                pass
        return self.user32.CallNextHookEx(None, nCode, wParam, lParam)

    # ── install / teardown ──────────────────────────────────────────────────
    def _configure_signatures(self):
        u, k = self.user32, self.kernel32
        try:
            u.SetWindowsHookExW.restype = wintypes.HHOOK
            u.CallNextHookEx.restype = LRESULT
            u.GetMessageW.restype = ctypes.c_int
            u.SendInput.restype = wintypes.UINT
            k.GetModuleHandleW.restype = wintypes.HMODULE
            k.GetTickCount.restype = wintypes.DWORD
        except Exception:
            pass  # Mocks don't need argtypes/restype

    def install(self) -> bool:
        """Install both low-level hooks. Returns True on success."""
        self._configure_signatures()
        try:
            self._anchor_tick = int(self.kernel32.GetTickCount())
        except Exception:
            self._anchor_tick = 0
        self._anchor_wall_ms = time.time() * 1000.0
        HOOKPROC = _hookproc_type()
        # Strong refs — the OS holds only a raw pointer; GC of these would crash.
        self._kbd_cb = HOOKPROC(self.kbd_proc)
        self._mouse_cb = HOOKPROC(self.mouse_proc)
        hmod = self.kernel32.GetModuleHandleW(None)
        self.kbd_hook = self.user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kbd_cb, hmod, 0)
        self.mouse_hook = self.user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_cb, hmod, 0)
        if not self.kbd_hook or not self.mouse_hook:
            self.teardown()
            return False
        return True

    def teardown(self):
        for h in (self.kbd_hook, self.mouse_hook):
            if h:
                try:
                    self.user32.UnhookWindowsHookEx(h)
                except Exception:
                    pass
        self.kbd_hook = self.mouse_hook = None

    # ── SendInput self-confirm handshake ────────────────────────────────────
    def synthesize_probe(self):
        """SendInput a VK_F24 down+up carrying SELFTEST_MARKER in dwExtraInfo."""
        arr = (INPUT * 2)()
        for i, up in enumerate((0, KEYEVENTF_KEYUP)):
            arr[i].type = INPUT_KEYBOARD
            arr[i].u.ki = KEYBDINPUT(wVk=VK_F24, wScan=0, dwFlags=up, time=0,
                                     dwExtraInfo=SELFTEST_MARKER)
        return int(self.user32.SendInput(2, arr, ctypes.sizeof(INPUT)))

    def self_test(self, timeout: float = 1.5) -> dict:
        """Fire a synthetic VK_F24 via SendInput and confirm the hook callback
        saw it within `timeout`. Returns a JSON-able health record.

        THREADING CONTRACT (MSDN, LowLevelKeyboardProc): the hook callback is
        dispatched in the context of the thread that INSTALLED the hooks, and
        only while that thread is retrieving messages (GetMessage/PeekMessage).
        Event.wait() below is a plain non-pumping wait, so this method MUST be
        called from a thread that is NOT the hook-installing thread, while the
        installing thread is inside pump(). Calling it on the installing thread
        (or before pump() has started) guarantees a timeout even when the hooks
        are perfectly healthy. If you ARE the installing thread and are not in
        pump(), use self_test_pumping() instead."""
        self._selftest_event.clear()
        detail = ""
        sent = 0
        try:
            sent = self.synthesize_probe()
        except Exception as e:
            detail = f"SendInput failed: {e}"
        fired = self._selftest_event.wait(timeout)
        ok = bool(fired and sent)
        if not detail:
            detail = ("hook callback observed the SendInput probe"
                      if ok else "probe not observed within timeout — "
                      "hooks may be dropped (LowLevelHooksTimeout) or blocked")
        return {"check": "win_input_hooks", "ok": ok, "sent_inputs": sent,
                "fired": bool(fired), "detail": detail}

    def self_test_pumping(self, timeout: float = 2.0) -> dict:
        """self_test variant for the hook-INSTALLING thread itself: pumps THIS
        thread's message queue (PeekMessageW) while waiting for the probe.

        Needed because the LL-hook callback is only dispatched while the
        installing thread retrieves messages — a message-retrieval call (even
        PeekMessage with an empty queue) is what lets Win32 deliver the pending
        hook call; a bare Event.wait() never can. Used by the one-shot
        `--selftest` / doctor path where install + confirm + teardown all
        happen on one thread."""
        self._selftest_event.clear()
        detail = ""
        sent = 0
        try:
            sent = self.synthesize_probe()
        except Exception as e:
            detail = f"SendInput failed: {e}"
        deadline = time.monotonic() + timeout
        msg = wintypes.MSG()
        while not self._selftest_event.is_set() and time.monotonic() < deadline:
            got = 0
            try:
                got = int(self.user32.PeekMessageW(
                    ctypes.byref(msg), None, 0, 0, PM_REMOVE))
            except Exception:
                pass
            if got:
                self.user32.TranslateMessage(ctypes.byref(msg))
                self.user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.005)
        fired = self._selftest_event.is_set()
        ok = bool(fired and sent)
        if not detail:
            detail = ("hook callback observed the SendInput probe"
                      if ok else "probe not observed within timeout — "
                      "hooks may be dropped (LowLevelHooksTimeout) or blocked")
        return {"check": "win_input_hooks", "ok": ok, "sent_inputs": sent,
                "fired": bool(fired), "detail": detail}

    # ── message loop ────────────────────────────────────────────────────────
    def pump(self, max_iters: int | None = None) -> int:
        """Run the Win32 message loop (REQUIRED for LL hooks to deliver events).
        Handles GetMessageW's three returns: >0 message, 0 WM_QUIT, -1 error
        (branch on all three — `while(GetMessage())` spins at 100% CPU on -1).
        Returns the count of dispatched messages; max_iters bounds it for tests."""
        msg = wintypes.MSG()
        n = 0
        self._running = True
        while self._running and (max_iters is None or n < max_iters):
            ret = int(self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0))
            if ret == 0 or ret == -1:
                break
            self.user32.TranslateMessage(ctypes.byref(msg))
            self.user32.DispatchMessageW(ctypes.byref(msg))
            n += 1
        self._running = False
        return n


def _run_windows() -> int:
    import queue

    if not hasattr(ctypes, "WinDLL"):
        print("input_daemon: WinDLL unavailable — not a Windows host.",
              file=sys.stderr, flush=True)
        return 4

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # Full argtypes/restype on the REAL DLLs (the backend sets restype too, but
    # argtypes here make ctypes marshal pointers correctly on the hot path).
    HOOKPROC = _hookproc_type()
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                         wintypes.HINSTANCE, wintypes.DWORD]
    user32.CallNextHookEx.restype = LRESULT
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.GetMessageW.restype = ctypes.c_int
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.SendInput.restype = wintypes.UINT
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetTickCount.restype = wintypes.DWORD
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]

    # LL-hook callbacks must return well under LowLevelHooksTimeout (~300 ms) or
    # Windows silently drops the event and can evict the hook. JSON+file I/O is
    # far too slow inline, so the procs only enqueue; this worker does the write.
    _emit_q: "queue.Queue" = queue.Queue()

    def _emit_worker():
        while True:
            args = _emit_q.get()
            if args is None:
                return
            try:
                _emit(*args)
            except Exception:
                pass

    def _emit_async(category, source, data, coords=None, event_ms=None):
        _emit_q.put((category, source, data, coords, event_ms))

    threading.Thread(target=_emit_worker, daemon=True).start()

    def _set_pending_move(x, y, is_physical, ev_ms):
        global _pending_move
        with _move_lock:
            _pending_move = (x, y, is_physical, ev_ms)

    backend = WinInputBackend(user32, kernel32, emit=_emit_async, gate=_gate,
                              set_pending_move=_set_pending_move)
    if not backend.install():
        err = ctypes.get_last_error()
        print(f"input_daemon: SetWindowsHookEx FAILED (err={err}). Low-level "
              f"hooks require a normal interactive desktop session.",
              file=sys.stderr, flush=True)
        return 3

    print("input_daemon: Windows low-level hooks active — physical hardware "
          "filtered out by default; injected (synthetic) events from bridged "
          "programs always captured.", flush=True)

    # The MAIN thread is the hook-owner thread: it installed the hooks above
    # and it runs pump() below. MSDN (LowLevelKeyboardProc): the hook callback
    # is dispatched in the context of the INSTALLING thread and only while
    # that thread retrieves messages — so every self_test() wait must happen
    # on some OTHER thread, and any teardown+reinstall must happen HERE.
    try:
        owner_tid = int(kernel32.GetCurrentThreadId())
    except Exception:
        owner_tid = 0
    reinstall_requested = threading.Event()
    reinstalled = threading.Event()
    shutting_down = threading.Event()

    # STARTUP SELF-TEST — synthesize one harmless VK_F24 and confirm the hook
    # callback fires. Emits a health record so the machine SELF-CONFIRMS.
    # MUST run on a helper thread: the callback can only be dispatched while
    # the main (installing) thread is inside pump(). Blocking the main thread
    # in self_test's Event.wait() BEFORE pump() would guarantee a false
    # "hooks dropped" timeout on every boot.
    def _startup_selftest():
        time.sleep(0.25)                    # let the main thread enter pump()
        health = backend.self_test(timeout=1.5)
        _emit("event", "av.daemon.selftest", health)
        print(f"input_daemon: selftest {health}", flush=True)
    threading.Thread(target=_startup_selftest, daemon=True).start()

    def _refresher():
        while True:
            _refresh_target()
            time.sleep(PROFILE_REFRESH_S)
    threading.Thread(target=_refresher, daemon=True).start()

    # Liveness watchdog — LL hooks can be silently dropped if a callback ever
    # exceeds LowLevelHooksTimeout. Periodically re-verify via the SendInput
    # handshake; on failure, request a reinstall ON THE OWNER THREAD (a hook
    # installed from this watchdog thread would bind to a thread that never
    # pumps and could never fire again). Cadence env-tunable (0 disables).
    reverify_s = float(os.environ.get("AGENTVISION_HOOK_REVERIFY_S", "120"))
    if reverify_s > 0:
        def _watchdog():
            while not shutting_down.is_set():
                time.sleep(reverify_s)
                if shutting_down.is_set():
                    return
                try:
                    r = backend.self_test(timeout=1.0)
                    if r["ok"]:
                        continue
                    print("input_daemon: hooks appear dropped — requesting "
                          "reinstall on the hook-owner thread.",
                          file=sys.stderr, flush=True)
                    reinstalled.clear()
                    reinstall_requested.set()
                    # Wake the owner: WM_QUIT makes GetMessageW return 0, the
                    # owner loop below sees the request and reinstalls there.
                    user32.PostThreadMessageW(owner_tid, WM_QUIT, 0, 0)
                    if not reinstalled.wait(5.0):
                        _emit("event", "av.daemon.selftest",
                              {"check": "win_input_hooks", "ok": False,
                               "detail": "reinstall did not complete"})
                        continue
                    # CONFIRM with a real probe (owner is pumping again) —
                    # never report a reinstall healthy without proof.
                    r2 = backend.self_test(timeout=1.5)
                    _emit("event", "av.daemon.selftest",
                          {"check": "win_input_hooks", "ok": bool(r2["ok"]),
                           "detail": ("reinstalled after drop — probe confirmed"
                                      if r2["ok"] else
                                      "reinstalled but probe still not observed")})
                except Exception:
                    pass
        threading.Thread(target=_watchdog, daemon=True).start()

    # Hook-owner loop — install/pump/teardown all stay on THIS thread. The
    # message loop is REQUIRED for LL hooks to deliver events; a watchdog
    # reinstall request cycles teardown→install→pump here without exiting.
    try:
        while True:
            backend.pump()
            if not reinstall_requested.is_set():
                break                       # real WM_QUIT / error → exit
            reinstall_requested.clear()
            backend.teardown()
            if not backend.install():
                err = ctypes.get_last_error()
                print(f"input_daemon: hook reinstall FAILED (err={err}).",
                      file=sys.stderr, flush=True)
                return 3
            reinstalled.set()
    finally:
        shutting_down.set()
        backend.teardown()
    return 0


def _run_windows_selftest() -> int:
    """One-shot: install hooks, run the SendInput self-confirm, print a JSON
    health line to stdout, tear down, exit. Used by `agentvision doctor` to get
    a definitive 'hooks confirmed working' proof on the real machine."""
    result = {"check": "win_input_hooks", "ok": False, "detail": "not run"}
    if not hasattr(ctypes, "WinDLL"):
        result["detail"] = "WinDLL unavailable (not Windows)"
        print("AV_SELFTEST_JSON " + json.dumps(result), flush=True)
        return 4
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        HOOKPROC = _hookproc_type()
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                             wintypes.HINSTANCE, wintypes.DWORD]
        user32.SendInput.restype = wintypes.UINT
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        b = WinInputBackend(user32, kernel32,
                            emit=lambda *a, **k: None,
                            gate=lambda p: (False, 0, "physical"),
                            set_pending_move=lambda *a: None)
        if not b.install():
            result["detail"] = f"SetWindowsHookEx failed (err={ctypes.get_last_error()})"
        else:
            # Install + pump + wait ALL on THIS thread. MSDN: the LL-hook
            # callback is dispatched only while the INSTALLING thread
            # retrieves messages — a helper-thread PeekMessage services a
            # DIFFERENT per-thread queue and can never deliver this hook,
            # and a bare Event.wait() here never pumps. self_test_pumping
            # interleaves PeekMessageW with the wait on this same thread.
            user32.PeekMessageW.restype = ctypes.c_int
            user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG),
                                            wintypes.HWND, wintypes.UINT,
                                            wintypes.UINT, wintypes.UINT]
            result = b.self_test_pumping(timeout=2.0)
            b.teardown()
    except Exception as e:
        result["detail"] = f"exception: {e}"
    print("AV_SELFTEST_JSON " + json.dumps(result), flush=True)
    return 0 if result.get("ok") else 1


# ═════════════════════════════════════════════════════════════════════════════════
#  macOS backend — Quartz CGEventTap (original implementation, lazily imported)
# ═════════════════════════════════════════════════════════════════════════════════

def _run_macos() -> int:
    import ctypes
    try:
        from Quartz import (
            CGEventTapCreate, CGEventTapEnable,
            CFMachPortCreateRunLoopSource, CFRunLoopAddSource,
            CFRunLoopGetCurrent, CFRunLoopRun, kCFRunLoopCommonModes,
            kCGEventTapOptionListenOnly, kCGHIDEventTap, kCGHeadInsertEventTap,
            kCGEventKeyDown, kCGEventKeyUp, kCGEventFlagsChanged,
            kCGEventLeftMouseDown, kCGEventLeftMouseUp,
            kCGEventRightMouseDown, kCGEventRightMouseUp,
            kCGEventOtherMouseDown, kCGEventOtherMouseUp,
            kCGEventMouseMoved, kCGEventLeftMouseDragged,
            kCGEventRightMouseDragged, kCGEventOtherMouseDragged,
            kCGEventScrollWheel, CGEventGetIntegerValueField,
            CGEventGetLocation, CGEventGetTimestamp,
            kCGEventSourceUnixProcessID, kCGKeyboardEventKeycode,
            kCGMouseEventButtonNumber, kCGScrollWheelEventDeltaAxis1,
            kCGScrollWheelEventDeltaAxis2,
        )
    except ImportError as e:
        print(f"input_daemon: pyobjc-framework-Quartz not installed ({e})",
              file=sys.stderr)
        print("install with: pip install pyobjc-framework-Quartz", file=sys.stderr)
        return 2

    # mach_absolute_time → wall clock conversion (per-chip timebase).
    _libc = ctypes.CDLL("/usr/lib/libSystem.dylib")

    class _mach_timebase_info(ctypes.Structure):
        _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]

    _libc.mach_timebase_info.argtypes = [ctypes.POINTER(_mach_timebase_info)]
    _libc.mach_absolute_time.restype = ctypes.c_uint64
    _TB = _mach_timebase_info()
    _libc.mach_timebase_info(ctypes.byref(_TB))
    _TB_NUMER, _TB_DENOM = int(_TB.numer), int(_TB.denom)
    _ANCHOR_MACH = _libc.mach_absolute_time()
    _ANCHOR_WALL_MS = time.time() * 1000.0

    def _mach_to_wall_ms(ticks: int) -> float:
        delta = int(ticks) - _ANCHOR_MACH
        return _ANCHOR_WALL_MS + (delta * _TB_NUMER) / _TB_DENOM / 1_000_000.0

    KEY_DOWN = {kCGEventKeyDown}
    KEY_UP = {kCGEventKeyUp}
    FLAGS = {kCGEventFlagsChanged}
    M_DOWN = {kCGEventLeftMouseDown, kCGEventRightMouseDown, kCGEventOtherMouseDown}
    M_UP = {kCGEventLeftMouseUp, kCGEventRightMouseUp, kCGEventOtherMouseUp}
    M_MOVE = {kCGEventMouseMoved, kCGEventLeftMouseDragged,
              kCGEventRightMouseDragged, kCGEventOtherMouseDragged}
    SCROLL = {kCGEventScrollWheel}

    def _tap_callback(proxy, event_type, event, refcon):
        try:
            ev_ms = _mach_to_wall_ms(int(CGEventGetTimestamp(event)))
        except Exception:
            ev_ms = None
        _refresh_target()
        try:
            pid = int(CGEventGetIntegerValueField(event, kCGEventSourceUnixProcessID))
        except Exception:
            pid = 0
        is_physical = (pid == 0)
        # Drop our own synthetic events to avoid feedback loops.
        if not is_physical and pid == os.getpid():
            return event
        record_it, src_pid, kind = _gate(is_physical)
        # Preserve the real injector PID for synthetic mac events.
        if not is_physical:
            src_pid = pid
        if not record_it:
            return event
        try:
            loc = CGEventGetLocation(event)
            x, y = int(loc.x), int(loc.y)
        except Exception:
            x, y = -1, -1

        if event_type in KEY_DOWN:
            kc = int(CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode))
            _emit("key", "av.daemon.key.press",
                  {"keycode": kc, "phase": "down", "source_pid": src_pid, "kind": kind},
                  event_ms=ev_ms)
        elif event_type in KEY_UP:
            kc = int(CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode))
            _emit("key", "av.daemon.key.release",
                  {"keycode": kc, "phase": "up", "source_pid": src_pid, "kind": kind},
                  event_ms=ev_ms)
        elif event_type in FLAGS:
            kc = int(CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode))
            _emit("key", "av.daemon.key.modifier",
                  {"keycode": kc, "phase": "flags", "source_pid": src_pid, "kind": kind},
                  event_ms=ev_ms)
        elif event_type in M_DOWN or event_type in M_UP:
            btn = int(CGEventGetIntegerValueField(event, kCGMouseEventButtonNumber))
            phase = "down" if event_type in M_DOWN else "up"
            _emit("mouse", "av.daemon.mouse.click",
                  {"x": x, "y": y, "button": _button_name(btn), "phase": phase,
                   "source_pid": src_pid, "kind": kind},
                  coords={"x": x, "y": y}, event_ms=ev_ms)
        elif event_type in M_MOVE:
            global _pending_move
            with _move_lock:
                _pending_move = (x, y, is_physical, ev_ms)
        elif event_type in SCROLL:
            dy = int(CGEventGetIntegerValueField(event, kCGScrollWheelEventDeltaAxis1))
            dx = int(CGEventGetIntegerValueField(event, kCGScrollWheelEventDeltaAxis2))
            _emit("mouse", "av.daemon.mouse.scroll",
                  {"x": x, "y": y, "dx": dx, "dy": dy,
                   "source_pid": src_pid, "kind": kind},
                  coords={"x": x, "y": y}, event_ms=ev_ms)
        return event

    types = (list(KEY_DOWN) + list(KEY_UP) + list(FLAGS) + list(M_DOWN)
             + list(M_UP) + list(M_MOVE) + list(SCROLL))
    mask = 0
    for t in types:
        mask |= (1 << int(t))

    tap = CGEventTapCreate(kCGHIDEventTap, kCGHeadInsertEventTap,
                           kCGEventTapOptionListenOnly, mask, _tap_callback, None)
    if not tap:
        print("input_daemon: CGEventTapCreate FAILED — Accessibility "
              "permission likely missing. Grant it under System Settings → "
              "Privacy & Security → Accessibility.", file=sys.stderr, flush=True)
        return 3
    src = CFMachPortCreateRunLoopSource(None, tap, 0)
    CFRunLoopAddSource(CFRunLoopGetCurrent(), src, kCFRunLoopCommonModes)
    CGEventTapEnable(tap, True)
    print("input_daemon: CGEventTap active — physical hardware filtered out "
          "by default; synthetic events from bridged programs always captured.",
          flush=True)
    CFRunLoopRun()
    return 0


# ═════════════════════════════════════════════════════════════════════════════════
#  Linux backend — evdev (/dev/input/event*), factored for mac-testability
# ═════════════════════════════════════════════════════════════════════════════════
#
# Mirrors the Windows backend's structure so the ENTIRE control flow can be
# exercised on macOS (see utils/test_linux_platform.py):
#   • The constants + PURE decoder/classifier below use no Linux APIs at all,
#     so a test can feed crafted (type, code, value) triples / device metadata
#     and assert the unified event that comes out.
#   • LinuxInputBackend takes an INJECTABLE evdev module + sysfs resolver; the
#     test passes fakes. Only run() touches select() over real fds.
#   • Physical-vs-synthetic is PER DEVICE on Linux (kernel events carry no
#     origin tag): real HID ⇒ physical; uinput virtual device ⇒ synthetic.
#     See classify_linux_device for the rules + the module docstring for the
#     documented limitations (no injector PID; XTEST bypasses /dev/input).

# ── Linux input-event constants (linux/input-event-codes.h) ─────────────────────
EV_SYN, EV_KEY, EV_REL, EV_ABS = 0x00, 0x01, 0x02, 0x03
REL_X, REL_Y = 0x00, 0x01
REL_HWHEEL, REL_WHEEL = 0x06, 0x08          # (hi-res 0x0b/0x0c ignored — dupes)
ABS_X, ABS_Y = 0x00, 0x01
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
BTN_SIDE, BTN_EXTRA = 0x113, 0x114
BTN_MOUSE_FIRST, BTN_MOUSE_LAST = 0x110, 0x117   # BTN_LEFT .. BTN_TASK
KEY_CODE_MAX = 0xFF                          # EV_KEY codes ≤ 0xFF = keyboard
KEY_F24 = 194                                # probe key for the selftest

_LINUX_BTN_NAMES = {BTN_LEFT: "left", BTN_RIGHT: "right", BTN_MIDDLE: "middle",
                    BTN_SIDE: "x1", BTN_EXTRA: "x2"}


def classify_linux_device(name: str, phys: str, sysfs_path: str) -> str:
    """'physical' | 'synthetic' — Linux distinguishes the two PER DEVICE.

    A uinput-created virtual device (what bots inject through) registers under
    /sys/devices/virtual/input/ and has an empty `phys` (no physical bus
    topology); real HID hardware has phys like 'usb-0000:00:14.0-1/input0'.
    Known injector names (py-evdev-uinput, ydotool…) are also caught. Unlike
    the mac per-event PID or the Windows per-event injected-flag, this is a
    per-DEVICE call — the kernel does not tag individual events. Pure."""
    if "/devices/virtual/" in (sysfs_path or ""):
        return "synthetic"
    if not (phys or "").strip():
        return "synthetic"
    n = (name or "").lower()
    if "uinput" in n or "ydotool" in n or "py-evdev" in n:
        return "synthetic"
    return "physical"


def decode_evdev_event(etype: int, code: int, value: int) -> dict | None:
    """Map ONE Linux input event (type, code, value) into a decode dict, or
    None for events we don't record (EV_SYN, unknown codes, hi-res wheel
    duplicates). kinds: key / click / scroll (emit-ready) and move_rel /
    move_abs (position updates the backend folds into the coalescer).
    Autorepeat (value=2) maps to 'down' — same as the repeated WM_KEYDOWN /
    kCGEventKeyDown streams on Windows/macOS. NO side effects; pure."""
    etype, code, value = int(etype), int(code), int(value)
    if etype == EV_KEY:
        if code <= KEY_CODE_MAX:
            if value in (1, 2):
                data = {"keycode": code, "phase": "down"}
                if value == 2:
                    data["repeat"] = True
                return {"kind": "key", "category": "key",
                        "source": "av.daemon.key.press", "data": data}
            if value == 0:
                return {"kind": "key", "category": "key",
                        "source": "av.daemon.key.release",
                        "data": {"keycode": code, "phase": "up"}}
            return None
        if BTN_MOUSE_FIRST <= code <= BTN_MOUSE_LAST and value in (0, 1):
            btn = _LINUX_BTN_NAMES.get(code, f"btn{code:#x}")
            return {"kind": "click", "category": "mouse",
                    "source": "av.daemon.mouse.click",
                    "data": {"button": btn,
                             "phase": "down" if value == 1 else "up"}}
        return None
    if etype == EV_REL:
        if code == REL_X:
            return {"kind": "move_rel", "axis": "x", "delta": value}
        if code == REL_Y:
            return {"kind": "move_rel", "axis": "y", "delta": value}
        if code == REL_WHEEL:
            return {"kind": "scroll", "category": "mouse",
                    "source": "av.daemon.mouse.scroll",
                    "data": {"dx": 0, "dy": value}}
        if code == REL_HWHEEL:
            return {"kind": "scroll", "category": "mouse",
                    "source": "av.daemon.mouse.scroll",
                    "data": {"dx": value, "dy": 0}}
        return None
    if etype == EV_ABS:
        if code == ABS_X:
            return {"kind": "move_abs", "axis": "x", "value": value}
        if code == ABS_Y:
            return {"kind": "move_abs", "axis": "y", "value": value}
        return None
    return None


class LinuxInputBackend:
    """Production Linux evdev backend, with an injectable evdev module + sysfs
    resolver so the whole flow is unit-testable on macOS.

    Parameters
    ----------
    evdev_module     : the imported `evdev` module (a fake in tests)
    emit             : callable(category, source, data, coords=None, event_ms=None)
    gate             : callable(is_physical) -> (record_it, source_pid, kind)
    set_pending_move : callable(x, y, is_physical, event_ms)  (coalesced sink)
    sysfs_resolver   : callable(dev_path) -> sysfs device path (fake in tests)
    initial_pos      : absolute anchor for the relative-accumulated pointer
                       position (X11: queried via xdotool; else (0,0))

    POINTER POSITION NOTE: evdev mice report RELATIVE deltas with no absolute
    origin, so x/y are accumulated from `initial_pos` (clamped ≥ 0) — exact
    for uinput bots that inject ABS_X/ABS_Y in pixel space (ydotool absolute
    moves), best-effort for physical mice (which are gated out by default
    anyway unless capture_user_input is on)."""

    RESCAN_S = 2.0            # hotplug poll — a bot's uinput device appears
                              # AFTER daemon start and must be picked up

    def __init__(self, evdev_module, *, emit, gate, set_pending_move,
                 sysfs_resolver=None, initial_pos=(0, 0)):
        self.evdev = evdev_module
        self.emit = emit
        self.gate = gate
        self.set_pending_move = set_pending_move
        self._sysfs = sysfs_resolver or self._sysfs_path
        self.devices: dict = {}              # path -> (InputDevice, is_physical)
        self.perm_denied: list[str] = []
        self._px = max(0, int(initial_pos[0]))
        self._py = max(0, int(initial_pos[1]))

    # ── device classification ───────────────────────────────────────────────
    @staticmethod
    def _sysfs_path(dev_path: str) -> str:
        """/dev/input/eventN → the real sysfs device path (identifies uinput
        virtual devices via /sys/devices/virtual/…)."""
        node = os.path.basename(dev_path or "")
        try:
            return os.path.realpath(f"/sys/class/input/{node}/device")
        except OSError:
            return ""

    def device_is_physical(self, dev) -> bool:
        name = getattr(dev, "name", "") or ""
        phys = getattr(dev, "phys", "") or ""
        sysfs = self._sysfs(getattr(dev, "path", "") or "")
        return classify_linux_device(name, phys, sysfs) == "physical"

    @staticmethod
    def _is_input_device(dev) -> bool:
        """Keyboard/mouse-ish only: exposes EV_KEY, EV_REL or EV_ABS."""
        try:
            caps = dev.capabilities()
        except Exception:
            return True      # can't introspect — keep it; decode filters anyway
        return any(k in caps for k in (EV_KEY, EV_REL, EV_ABS))

    # ── device scan (initial + hotplug rescan) ──────────────────────────────
    def scan_devices(self) -> tuple[int, int]:
        """(Re)open every /dev/input/event* we can read. Returns
        (open_device_count, permission_denied_count). Idempotent — safe to
        call repeatedly; picks up hotplugged devices, drops vanished ones."""
        try:
            paths = list(self.evdev.list_devices())
        except Exception:
            paths = []
        current = set(paths)
        for path in [p for p in self.devices if p not in current]:
            dev, _ = self.devices.pop(path)
            try:
                dev.close()
            except Exception:
                pass
        denied = 0
        for path in paths:
            if path in self.devices:
                continue
            try:
                dev = self.evdev.InputDevice(path)
            except PermissionError:
                denied += 1
                if path not in self.perm_denied:
                    self.perm_denied.append(path)
                continue
            except Exception:
                continue
            if not self._is_input_device(dev):
                try:
                    dev.close()
                except Exception:
                    pass
                continue
            is_phys = self.device_is_physical(dev)
            self.devices[path] = (dev, is_phys)
            print(f"input_daemon: opened {path} "
                  f"'{getattr(dev, 'name', '?')}' "
                  f"[{'physical' if is_phys else 'synthetic'}]", flush=True)
        return len(self.devices), denied

    # ── unified-event routing (pure-ish; testable without evdev) ────────────
    def handle_event(self, is_physical: bool, etype: int, code: int,
                     value: int, ts_ms: float | None,
                     device_name: str = "") -> None:
        """Decode ONE kernel event and route it through the SAME physical/
        synthetic gate + JSONL emit the mac/win backends use."""
        dec = decode_evdev_event(etype, code, value)
        if dec is None:
            return
        kind = dec["kind"]
        if kind == "move_rel":
            if dec["axis"] == "x":
                self._px = max(0, self._px + dec["delta"])
            else:
                self._py = max(0, self._py + dec["delta"])
            self.set_pending_move(self._px, self._py, is_physical, ts_ms)
            return
        if kind == "move_abs":
            if dec["axis"] == "x":
                self._px = max(0, dec["value"])
            else:
                self._py = max(0, dec["value"])
            self.set_pending_move(self._px, self._py, is_physical, ts_ms)
            return
        record_it, src_pid, k = self.gate(is_physical)
        if not record_it:
            return
        data = dict(dec["data"])
        data["source_pid"] = src_pid
        data["kind"] = k
        if device_name:
            data["device"] = device_name
        coords = None
        if dec["category"] == "mouse":
            data.setdefault("x", self._px)
            data.setdefault("y", self._py)
            coords = {"x": self._px, "y": self._py}
        self.emit(dec["category"], dec["source"], data, coords=coords,
                  event_ms=ts_ms)

    # ── event loop ──────────────────────────────────────────────────────────
    def run(self) -> None:
        """select() across every open device fd; periodic rescan for hotplug.
        Never returns (the daemon's main loop)."""
        import select as _select
        last_scan = time.time()
        while True:
            if time.time() - last_scan >= self.RESCAN_S:
                self.scan_devices()
                last_scan = time.time()
            fd_map = {}
            for path, (dev, is_phys) in list(self.devices.items()):
                fd = getattr(dev, "fd", None)
                if fd is not None:
                    fd_map[fd] = (path, dev, is_phys)
            if not fd_map:
                time.sleep(0.5)
                continue
            try:
                readable, _, _ = _select.select(list(fd_map), [], [], 0.5)
            except OSError:
                # a device vanished mid-select — rescan immediately
                self.scan_devices()
                last_scan = time.time()
                continue
            for fd in readable:
                path, dev, is_phys = fd_map[fd]
                try:
                    for ev in dev.read():
                        try:
                            ts_ms = float(ev.timestamp()) * 1000.0
                        except Exception:
                            ts_ms = None
                        self.handle_event(is_phys, int(ev.type), int(ev.code),
                                          int(ev.value), ts_ms,
                                          device_name=getattr(dev, "name", ""))
                except BlockingIOError:
                    continue
                except OSError:
                    # unplugged — drop it; the rescan picks up replacements
                    self.devices.pop(path, None)
                    try:
                        dev.close()
                    except Exception:
                        pass


def _linux_initial_pointer_pos() -> tuple[int, int]:
    """Best-effort absolute anchor for the accumulated pointer position
    (X11 via xdotool; Wayland has no global query — start at 0,0)."""
    try:
        import shutil as _sh
        import subprocess as _sp
        if platform_shim.linux_session_type() == "x11" and _sh.which("xdotool"):
            r = _sp.run(["xdotool", "getmouselocation", "--shell"],
                        capture_output=True, text=True, timeout=2)
            vals = {}
            for line in (r.stdout or "").splitlines():
                k, sep, v = line.partition("=")
                if sep and v.strip().lstrip("-").isdigit():
                    vals[k.strip().upper()] = int(v.strip())
            if "X" in vals and "Y" in vals:
                return max(0, vals["X"]), max(0, vals["Y"])
    except Exception:
        pass
    return 0, 0


_LINUX_PERM_HELP = (
    "input_daemon: PERMISSION DENIED opening /dev/input/event* — on Linux the "
    "input recorder needs read access to the evdev devices. Add your user to "
    "the `input` group:\n"
    "    sudo usermod -aG input $USER\n"
    "then log out and back in (or reboot). The bridge keeps working; only "
    "key/mouse capture is disabled until then.")


def _run_linux() -> int:
    """Linux daemon main: evdev over /dev/input/event*, same gate + JSONL as
    the mac/win backends. Degrades cleanly: python-evdev missing → exit 2
    with an install hint (like the mac Quartz path); all devices permission-
    denied → exit 3 with the `input`-group hint (like the mac Accessibility
    message). The bridge is unaffected either way."""
    try:
        import evdev  # type: ignore
    except ImportError as e:
        print(f"input_daemon: python-evdev not installed ({e})",
              file=sys.stderr)
        print("install with: pip install evdev   — the bridge keeps working; "
              "only key/mouse capture is disabled.", file=sys.stderr)
        return 2

    def _set_pending_move(x, y, is_physical, ev_ms):
        global _pending_move
        with _move_lock:
            _pending_move = (x, y, is_physical, ev_ms)

    backend = LinuxInputBackend(evdev, emit=_emit, gate=_gate,
                                set_pending_move=_set_pending_move,
                                initial_pos=_linux_initial_pointer_pos())
    opened, denied = backend.scan_devices()
    if opened == 0 and denied > 0:
        print(_LINUX_PERM_HELP, file=sys.stderr, flush=True)
        return 3
    if opened == 0:
        print("input_daemon: no readable /dev/input/event* devices yet — "
              "waiting for hotplug (rescan every "
              f"{LinuxInputBackend.RESCAN_S:.0f}s).", flush=True)
    else:
        phys = sum(1 for _, (d, p) in backend.devices.items() if p)
        print(f"input_daemon: evdev backend active — {opened} device(s) open "
              f"({phys} physical, {opened - phys} synthetic/uinput). Physical "
              "hardware filtered out by default; synthetic (uinput) events "
              "from bridged programs always captured. NOTE: on Linux the "
              "physical/synthetic split is PER DEVICE; XTEST/compositor-level "
              "injection bypasses /dev/input and is not visible here.",
              flush=True)

    def _refresher():
        while True:
            _refresh_target()
            time.sleep(PROFILE_REFRESH_S)
    threading.Thread(target=_refresher, daemon=True).start()

    backend.run()                            # never returns
    return 0


def _run_linux_selftest() -> int:
    """One-shot Linux self-test for `agentvision doctor` / the bridge
    /selftest: confirm python-evdev imports, enumerate /dev/input devices
    (readability + per-device physical/synthetic classification), and — when
    /dev/uinput is writable — prove END-TO-END delivery by creating a uinput
    device, injecting a KEY_F24 press, reading it back through evdev, and
    confirming the device classifies as SYNTHETIC. That round-trip is the
    evdev analogue of the Windows SendInput handshake. Prints one
    AV_SELFTEST_JSON line; exit 0 iff ok."""
    result: dict = {"check": "linux_input_evdev", "ok": False, "detail": ""}
    try:
        import evdev  # type: ignore
    except ImportError as e:
        result["detail"] = (f"python-evdev not installed ({e}) — pip install "
                            "evdev. Bridge unaffected; key/mouse capture "
                            "disabled.")
        print("AV_SELFTEST_JSON " + json.dumps(result), flush=True)
        return 1

    backend = LinuxInputBackend(evdev, emit=lambda *a, **k: None,
                                gate=lambda p: (False, 0, "physical"),
                                set_pending_move=lambda *a: None)
    opened, denied = backend.scan_devices()
    result["devices_open"] = opened
    result["permission_denied"] = denied
    result["devices"] = [
        {"path": path, "name": getattr(dev, "name", ""),
         "phys": getattr(dev, "phys", ""),
         "class": "physical" if is_phys else "synthetic"}
        for path, (dev, is_phys) in sorted(backend.devices.items())]
    if opened == 0 and denied > 0:
        result["detail"] = ("permission denied on /dev/input/event* — add "
                            "your user to the `input` group: sudo usermod "
                            "-aG input $USER (then re-login)")
    elif opened == 0:
        result["detail"] = "no /dev/input/event* devices visible"
    else:
        result["ok"] = True
        result["detail"] = f"{opened} evdev device(s) readable"

    # uinput → evdev round-trip: the definitive on-machine proof, when allowed.
    probe: dict = {"ok": None, "detail": "skipped"}
    try:
        ui = evdev.UInput({EV_KEY: [KEY_F24]}, name="agentvision-selftest")
        try:
            time.sleep(0.3)                  # udev settle
            target = getattr(ui, "device", None)
            synth = bool(target is not None
                         and not backend.device_is_physical(target))
            ui.write(EV_KEY, KEY_F24, 1)
            ui.syn()
            ui.write(EV_KEY, KEY_F24, 0)
            ui.syn()
            seen = False
            if target is not None:
                import select as _select
                deadline = time.monotonic() + 2.0
                while not seen and time.monotonic() < deadline:
                    r, _, _ = _select.select([target.fd], [], [], 0.25)
                    if not r:
                        continue
                    try:
                        for ev in target.read():
                            if (int(ev.type) == EV_KEY
                                    and int(ev.code) == KEY_F24):
                                seen = True
                                break
                    except BlockingIOError:
                        continue
            probe = {"ok": bool(seen and synth), "injected": True,
                     "observed": bool(seen),
                     "classified_synthetic": synth,
                     "detail": ("uinput probe observed via evdev and "
                                "classified synthetic" if seen and synth
                                else "uinput probe NOT observed via evdev"
                                if not seen else
                                "probe observed but device classified "
                                "physical — classification bug")}
        finally:
            try:
                ui.close()
            except Exception:
                pass
    except PermissionError:
        probe = {"ok": None,
                 "detail": "skipped: /dev/uinput not writable (fine — only "
                           "needed for the injection round-trip proof)"}
    except Exception as e:
        probe = {"ok": None, "detail": f"skipped: uinput unavailable ({e})"}
    result["uinput_probe"] = probe
    if probe.get("ok") is False:
        result["ok"] = False
        result["detail"] += "; uinput round-trip FAILED"

    print("AV_SELFTEST_JSON " + json.dumps(result), flush=True)
    return 0 if result.get("ok") else 1


# ── Entry point ─────────────────────────────────────────────────────────────

def main() -> int:
    # One-shot self-test mode (used by `agentvision doctor`): install hooks,
    # run the SendInput self-confirm, emit a JSON line, exit. No pidfile/daemon.
    if "--selftest" in sys.argv:
        if platform_shim.IS_WINDOWS:
            return _run_windows_selftest()
        if platform_shim.IS_LINUX:
            return _run_linux_selftest()
        print("AV_SELFTEST_JSON " + json.dumps(
            {"check": "input_hooks", "ok": None,
             "detail": "self-test implemented for Windows + Linux "
                       f"(os={sys.platform}); the macOS CGEventTap path is "
                       "exercised by the daemon at start"}),
            flush=True)
        return 0

    _write_pidfile()
    atexit.register(_remove_pidfile)
    for signame in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, signame, None)
        if sig is not None:
            try:
                signal.signal(sig, _on_signal)
            except Exception:
                pass

    _refresh_target()
    _emit("process", "av.daemon.start", {
        "pid": os.getpid(),
        "os": sys.platform,
        "active_profile": _active_profile_name,
        "active_sink": str(_active_sink) if _active_sink else None,
        "capture_user_input": _capture_user_input,
        "filter": "drops physical-hardware events when capture_user_input=False",
    })
    print(f"input_daemon: pid={os.getpid()} os={sys.platform} "
          f"profile='{_active_profile_name}' sink={_active_sink} "
          f"capture_user_input={_capture_user_input} "
          f"profiles={PROFILES_FILE}", flush=True)

    threading.Thread(target=_move_flusher, daemon=True).start()
    threading.Thread(target=_stats_logger, daemon=True).start()

    if platform_shim.IS_WINDOWS:
        return _run_windows()
    if platform_shim.IS_MAC:
        return _run_macos()
    if platform_shim.IS_LINUX:
        return _run_linux()
    print("input_daemon: unsupported OS for input capture "
          f"({sys.platform}) — bridge still works, only key/mouse capture "
          "is disabled.", file=sys.stderr, flush=True)
    # Idle so the pidfile/status stays valid; bridge features are unaffected.
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    raise SystemExit(main())
