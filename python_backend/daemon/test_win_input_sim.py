"""
Deterministic mock-simulation of the Windows input backend — runs the ENTIRE
Win32 control flow ON macOS/Linux by mocking the DLLs, so the logic is proven
first-try without a Windows box.

What it proves (against the real Win32 contracts):
  • KBDLLHOOKSTRUCT / MSLLHOOKSTRUCT are cast + decoded correctly for
    keydown / keyup / SYSKEY down+up / left|right|middle|X1|X2 click /
    vertical + horizontal wheel (both signs) / mouse-move / unknown wParam.
  • Struct layouts match the Win32 x64 SDK at the PER-FIELD-OFFSET level, and
    the decoders are additionally fed RAW struct.pack'ed byte buffers (not
    ctypes-built structs), so an offset/order bug cannot self-cancel.
  • The LLKHF_INJECTED (0x10, keyboard) / LLMHF_INJECTED (0x01, mouse) flag →
    injected-vs-physical, incl. proof the mouse mask is 0x01 and NOT 0x10, and
    the physical-hardware gate (drop unless capture_user_input) for BOTH
    keyboard and mouse.
  • SetWindowsHookEx install returns/stores the HHOOKs; a partial failure tears
    down the surviving hook; teardown calls UnhookWindowsHookEx.
  • The GetMessage loop handles >0 / 0 (WM_QUIT) / -1 (error) exactly.
  • The SendInput self-confirm handshake under the REAL Win32 delivery
    contract (modeled by FaithfulWin32): SendInput does NOT invoke the hook
    synchronously; the callback is dispatched ONLY when the thread that
    INSTALLED the hooks retrieves messages (GetMessage/PeekMessage on that
    same thread — per-thread queues). This proves:
      - install+pump on one thread, self_test waiting on another → OK;
      - self_test_pumping on the installing thread itself → OK;
      - the WRONG shape (installer blocked in Event.wait while a helper
        thread pumps) → FAILS, exactly as on real Windows;
      - nobody pumping → timeout → ok=False.
  • The probe PAYLOAD is validated from the real INPUT array passed to
    SendInput (type/wVk/dwFlags/cbSize/dwExtraInfo marker) and the delivered
    KBDLLHOOKSTRUCT is DERIVED from that array — a synthesize_probe bug fails
    here exactly as it would on real Windows.
  • The hook proc drops its own SELFTEST_MARKER probe (keyboard AND mouse)
    without recording it.
  • Capture region math: HWND RECT → mss region, incl. minimized rejection.

Run: python3 python_backend/daemon/test_win_input_sim.py
"""
from __future__ import annotations
import ctypes
import struct as pystruct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from daemon import input_daemon as d          # noqa: E402
from utils import platform_shim as ps          # noqa: E402

_fails = 0


def check(name, cond, detail=""):
    global _fails
    if not cond:
        _fails += 1
    print(f"  [{'ok ' if cond else 'FAIL'}] {name}{'' if cond or not detail else '  — ' + detail}")


# ── Fake DLL plumbing ────────────────────────────────────────────────────────

class FakeFn:
    """A callable that records calls, allows .restype/.argtypes assignment, and
    returns either a constant, a pop-front sequence, or a python side-effect."""
    def __init__(self, ret=0, results=None, fn=None):
        self.ret = ret
        self.results = list(results) if results is not None else None
        self.fn = fn
        self.calls = []
        self.restype = None
        self.argtypes = None

    def __call__(self, *a):
        self.calls.append(a)
        if self.fn is not None:
            return self.fn(*a)
        if self.results is not None:
            return self.results.pop(0) if self.results else 0
        return self.ret


class FakeDLL:
    pass


def _make_dlls(sethook_results=(0x1111, 0x2222), sendinput_fn=None,
               getmsg_results=None):
    u = FakeDLL()
    k = FakeDLL()
    u.SetWindowsHookExW = FakeFn(results=list(sethook_results))
    u.CallNextHookEx = FakeFn(ret=0)
    u.UnhookWindowsHookEx = FakeFn(ret=1)
    u.TranslateMessage = FakeFn(ret=1)
    u.DispatchMessageW = FakeFn(ret=0)
    u.GetMessageW = FakeFn(results=list(getmsg_results) if getmsg_results else [0])
    u.PeekMessageW = FakeFn(ret=0)
    u.SendInput = FakeFn(ret=2, fn=sendinput_fn)
    k.GetModuleHandleW = FakeFn(ret=0x400000)
    k.GetTickCount = FakeFn(ret=100000)
    return u, k


def _recording_backend(**dll_kw):
    """A WinInputBackend wired to fakes with a recording emit + a gate that
    honors an `opt_in` flag (physical dropped unless opt_in)."""
    emitted = []
    moves = []
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

    u, k = _make_dlls(**dll_kw)
    b = d.WinInputBackend(u, k, emit=emit, gate=gate, set_pending_move=set_move)
    return b, emitted, moves, state, u, k


def _kb_addr(vk, flags=0, phase_down=True, scan=0, extra=0, wp=None):
    kb = d.KBDLLHOOKSTRUCT(vkCode=vk, scanCode=scan, flags=flags, time=100000,
                           dwExtraInfo=extra)
    _kb_addr._keep.append(kb)          # keep alive for the cast
    if wp is None:
        wp = d.WM_KEYDOWN if phase_down else d.WM_KEYUP
    return wp, ctypes.addressof(kb)


_kb_addr._keep = []


def _ms_addr(wp, x=0, y=0, flags=0, mouse_data=0, extra=0):
    mst = d.MSLLHOOKSTRUCT(pt=d.POINT(x, y), mouseData=mouse_data & 0xFFFFFFFF,
                           flags=flags, time=100000, dwExtraInfo=extra)
    _ms_addr._keep.append(mst)
    return wp, ctypes.addressof(mst)


_ms_addr._keep = []


# ── Faithful Win32 delivery model ────────────────────────────────────────────
#
# Models the parts of the LL-hook contract that a naive mock hides and that
# broke (or would have broken) real-Windows behavior:
#
#   1. SetWindowsHookExW binds the hook to the INSTALLING thread
#      (installer_tid is recorded per install).
#   2. SendInput NEVER invokes the hook proc synchronously — it validates the
#      real INPUT array and queues a pending hook delivery, exactly as Windows
#      queues injected input for the hook to observe later.
#   3. The pending delivery is dispatched ONLY inside GetMessageW/PeekMessageW
#      AND ONLY when that retrieval call happens on the installing thread
#      (message queues are per-thread; a hook call is marshaled to the
#      installer's queue).
#
# The delivered KBDLLHOOKSTRUCT is DERIVED from the actual array passed to
# SendInput (vkCode=wVk, flags=LLKHF_INJECTED, dwExtraInfo propagated), so a
# probe-payload bug in synthesize_probe fails the handshake here just as it
# would on real Windows.

class FaithfulWin32:
    def __init__(self):
        self.installer_tid = None
        self.hooks = {}                 # WH_* -> proc
        self.pending = []               # queued (idHook, wParam, kbstruct)
        self.lock = threading.Lock()
        self.sendinput_errors = []
        self.quit = False
        self._hhook = 0x1000
        self._keep = []                 # keep delivered structs alive

        u = FakeDLL()
        k = FakeDLL()
        u.SetWindowsHookExW = FakeFn(fn=self._sethook)
        u.UnhookWindowsHookEx = FakeFn(ret=1)
        u.CallNextHookEx = FakeFn(ret=0)
        u.SendInput = FakeFn(fn=self._sendinput)
        u.GetMessageW = FakeFn(fn=self._getmessage)
        u.PeekMessageW = FakeFn(fn=self._peekmessage)
        u.TranslateMessage = FakeFn(ret=1)
        u.DispatchMessageW = FakeFn(ret=0)
        k.GetModuleHandleW = FakeFn(ret=0x400000)
        k.GetTickCount = FakeFn(ret=100000)
        self.user32, self.kernel32 = u, k

    # -- Win32 surface ------------------------------------------------------
    def _sethook(self, idHook, proc, hmod, tid):
        self.installer_tid = threading.get_ident()
        self.hooks[int(idHook)] = proc
        self._hhook += 1
        return self._hhook

    def _sendinput(self, n, arr, size):
        try:
            n = int(n)
            if int(size) != ctypes.sizeof(d.INPUT):
                self.sendinput_errors.append(
                    f"cbSize {int(size)} != sizeof(INPUT) {ctypes.sizeof(d.INPUT)}")
                return 0
            pa = ctypes.cast(arr, ctypes.POINTER(d.INPUT))
            accepted = 0
            for i in range(n):
                if int(pa[i].type) != d.INPUT_KEYBOARD:
                    self.sendinput_errors.append(
                        f"input[{i}].type={int(pa[i].type)} (only INPUT_KEYBOARD modeled)")
                    continue
                ki = pa[i].u.ki
                is_up = bool(int(ki.dwFlags) & d.KEYEVENTF_KEYUP)
                # Windows: injected key surfaces at the LL hook with
                # LLKHF_INJECTED set, vkCode = wVk, dwExtraInfo propagated.
                kb = d.KBDLLHOOKSTRUCT(
                    vkCode=int(ki.wVk), scanCode=int(ki.wScan),
                    flags=d.LLKHF_INJECTED | (0x80 if is_up else 0),
                    time=100000, dwExtraInfo=int(ki.dwExtraInfo))
                self._keep.append(kb)
                wp = d.WM_KEYUP if is_up else d.WM_KEYDOWN
                with self.lock:
                    self.pending.append((d.WH_KEYBOARD_LL, wp, kb))
                accepted += 1
            return accepted
        except Exception as e:
            self.sendinput_errors.append(repr(e))
            return 0

    def _deliver_pending(self):
        """Dispatch queued hook calls — ONLY on the installing thread
        (per-thread queues: another thread's retrieval sees nothing)."""
        if self.installer_tid is None or threading.get_ident() != self.installer_tid:
            return 0
        with self.lock:
            batch, self.pending = self.pending, []
        for idHook, wp, kb in batch:
            proc = self.hooks.get(idHook)
            if proc is not None:
                proc(d.HC_ACTION, wp, ctypes.addressof(kb))
        return len(batch)

    def _getmessage(self, pmsg, hwnd, mmin, mmax):
        # Real GetMessageW blocks; bounded here so a wrong-thread pump can't
        # hang the suite. Hook calls (sent messages) are dispatched during
        # message retrieval — but only for the installing thread.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if self.quit:
                return 0
            if self._deliver_pending():
                return 1        # something was dispatched; report activity
            time.sleep(0.002)
        return 0                # bounded idle → behave like WM_QUIT for tests

    def _peekmessage(self, pmsg, hwnd, mmin, mmax, remove):
        return 1 if self._deliver_pending() else 0

    def post_quit(self):
        self.quit = True


def _faithful_backend():
    fw = FaithfulWin32()
    emitted = []

    def emit(category, source, data, coords=None, event_ms=None):
        emitted.append({"category": category, "source": source, "data": data})

    def gate(is_physical):
        if is_physical:
            return False, 0, "physical"
        return True, -1, "synthetic"

    b = d.WinInputBackend(fw.user32, fw.kernel32, emit=emit, gate=gate,
                          set_pending_move=lambda *a: None)
    return fw, b, emitted


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_keyboard_decode_and_gate():
    print("keyboard decode + gate:")
    b, emitted, _, state, _, _ = _recording_backend()
    # physical keydown, opt_in False → dropped (nothing emitted)
    wp, lp = _kb_addr(0x41, flags=0, phase_down=True, scan=30)
    b.kbd_proc(d.HC_ACTION, wp, lp)
    check("physical key dropped by default", emitted == [], str(emitted))
    # injected keydown → emitted as synthetic
    wp, lp = _kb_addr(0x42, flags=d.LLKHF_INJECTED, phase_down=True)
    b.kbd_proc(d.HC_ACTION, wp, lp)
    check("injected key emitted", len(emitted) == 1, str(emitted))
    ev = emitted[-1]
    check("key source=press", ev["source"] == "av.daemon.key.press")
    check("key data", ev["data"]["keycode"] == 0x42 and ev["data"]["phase"] == "down"
          and ev["data"]["kind"] == "synthetic", str(ev["data"]))
    # SYSKEY down/up (Alt-modified keys arrive as these on real Windows)
    wp, lp = _kb_addr(0x46, flags=d.LLKHF_INJECTED, wp=d.WM_SYSKEYDOWN)
    b.kbd_proc(d.HC_ACTION, wp, lp)
    check("WM_SYSKEYDOWN → press/down", emitted[-1]["source"] == "av.daemon.key.press"
          and emitted[-1]["data"]["phase"] == "down", str(emitted[-1]))
    wp, lp = _kb_addr(0x46, flags=d.LLKHF_INJECTED, wp=d.WM_SYSKEYUP)
    b.kbd_proc(d.HC_ACTION, wp, lp)
    check("WM_SYSKEYUP → release/up", emitted[-1]["source"] == "av.daemon.key.release"
          and emitted[-1]["data"]["phase"] == "up", str(emitted[-1]))
    # physical key with opt_in True → emitted as physical
    state["opt_in"] = True
    wp, lp = _kb_addr(0x43, flags=0, phase_down=False)
    b.kbd_proc(d.HC_ACTION, wp, lp)
    ev = emitted[-1]
    check("opt-in physical keyup emitted", ev["source"] == "av.daemon.key.release"
          and ev["data"]["kind"] == "physical" and ev["data"]["phase"] == "up", str(ev))
    # unknown keyboard wParam → not processed, still passed through
    before = len(emitted)
    ncalls = len(b.user32.CallNextHookEx.calls)
    wp, lp = _kb_addr(0x41, flags=d.LLKHF_INJECTED, wp=0x0102)   # WM_CHAR-ish
    b.kbd_proc(d.HC_ACTION, wp, lp)
    check("unknown kbd wParam not emitted", len(emitted) == before)
    check("unknown kbd wParam passed to CallNextHookEx",
          len(b.user32.CallNextHookEx.calls) == ncalls + 1)
    # nCode < 0 must NOT process but MUST pass through
    before = len(emitted)
    ncalls = len(b.user32.CallNextHookEx.calls)
    wp, lp = _kb_addr(0x44, flags=d.LLKHF_INJECTED)
    b.kbd_proc(-1, wp, lp)
    check("nCode<0 not processed", len(emitted) == before)
    check("nCode<0 still passes to CallNextHookEx",
          len(b.user32.CallNextHookEx.calls) == ncalls + 1)


def test_mouse_decode():
    print("mouse decode (click / wheel / hwheel / xbutton / move):")
    b, emitted, moves, state, _, _ = _recording_backend()
    state["opt_in"] = True   # so physical mouse events emit for assertion
    # left down
    wp, lp = _ms_addr(d.WM_LBUTTONDOWN, x=100, y=200)
    b.mouse_proc(d.HC_ACTION, wp, lp)
    check("left click", emitted[-1]["data"] == {"x": 100, "y": 200, "button": "left",
          "phase": "down", "source_pid": 0, "kind": "physical"}, str(emitted[-1]["data"]))
    # right up
    wp, lp = _ms_addr(d.WM_RBUTTONUP, x=1, y=2)
    b.mouse_proc(d.HC_ACTION, wp, lp)
    check("right up", emitted[-1]["data"]["button"] == "right" and emitted[-1]["data"]["phase"] == "up")
    # middle down
    wp, lp = _ms_addr(d.WM_MBUTTONDOWN)
    b.mouse_proc(d.HC_ACTION, wp, lp)
    check("middle", emitted[-1]["data"]["button"] == "middle")
    # X button 2 down (high word = 2)
    wp, lp = _ms_addr(d.WM_XBUTTONDOWN, mouse_data=(2 << 16))
    b.mouse_proc(d.HC_ACTION, wp, lp)
    check("xbutton2 down", emitted[-1]["data"]["button"] == "x2", str(emitted[-1]["data"]))
    # X button 1 UP (high word = 1)
    wp, lp = _ms_addr(d.WM_XBUTTONUP, mouse_data=(1 << 16))
    b.mouse_proc(d.HC_ACTION, wp, lp)
    check("xbutton1 up", emitted[-1]["data"]["button"] == "x1"
          and emitted[-1]["data"]["phase"] == "up", str(emitted[-1]["data"]))
    # vertical wheel, negative delta (-120): high word 0xFF88
    wp, lp = _ms_addr(d.WM_MOUSEWHEEL, mouse_data=(0xFF88 << 16))
    b.mouse_proc(d.HC_ACTION, wp, lp)
    check("wheel dy=-120", emitted[-1]["source"] == "av.daemon.mouse.scroll"
          and emitted[-1]["data"]["dy"] == -120 and emitted[-1]["data"]["dx"] == 0, str(emitted[-1]["data"]))
    # horizontal wheel, +120 and −120 (sign bug unique to hwheel branch)
    wp, lp = _ms_addr(d.WM_MOUSEHWHEEL, mouse_data=(120 << 16))
    b.mouse_proc(d.HC_ACTION, wp, lp)
    check("hwheel dx=120", emitted[-1]["data"]["dx"] == 120 and emitted[-1]["data"]["dy"] == 0)
    wp, lp = _ms_addr(d.WM_MOUSEHWHEEL, mouse_data=(0xFF88 << 16))
    b.mouse_proc(d.HC_ACTION, wp, lp)
    check("hwheel dx=-120", emitted[-1]["data"]["dx"] == -120 and emitted[-1]["data"]["dy"] == 0,
          str(emitted[-1]["data"]))
    # move → coalesced (goes to set_pending_move, not emit)
    wp, lp = _ms_addr(d.WM_MOUSEMOVE, x=7, y=9)
    b.mouse_proc(d.HC_ACTION, wp, lp)
    check("move coalesced (not emitted)", moves and moves[-1][0] == 7 and moves[-1][1] == 9, str(moves))
    # unknown mouse wParam → decode None, nothing emitted, still passed through
    before = len(emitted)
    ncalls = len(b.user32.CallNextHookEx.calls)
    wp, lp = _ms_addr(0x0219)          # not a hook message we decode
    check("decode unknown wParam → None", d.decode_mouse_event(wp, lp) is None)
    b.mouse_proc(d.HC_ACTION, wp, lp)
    check("unknown mouse wParam not emitted", len(emitted) == before)
    check("unknown mouse wParam passed to CallNextHookEx",
          len(b.user32.CallNextHookEx.calls) == ncalls + 1)


def test_mouse_injected_gate_and_marker():
    """LLMHF_INJECTED is 0x01 (mouse) — deliberately DIFFERENT from
    LLKHF_INJECTED 0x10 (keyboard). If the mouse decoder tested 0x10, every
    synthetic (bot) mouse event would be misclassified physical and dropped —
    the product's core purpose broken while a keyboard-only suite stays green."""
    print("mouse injected-vs-physical gate + selftest marker:")
    b, emitted, moves, state, _, _ = _recording_backend()
    state["opt_in"] = False
    # injected click emits as synthetic even with opt_in False
    wp, lp = _ms_addr(d.WM_LBUTTONDOWN, x=3, y=4, flags=d.LLMHF_INJECTED)
    b.mouse_proc(d.HC_ACTION, wp, lp)
    check("injected mouse click emitted as synthetic", len(emitted) == 1
          and emitted[-1]["data"]["kind"] == "synthetic"
          and emitted[-1]["data"]["source_pid"] == -1, str(emitted))
    # physical click (flags=0) dropped
    wp, lp = _ms_addr(d.WM_RBUTTONDOWN, flags=0)
    b.mouse_proc(d.HC_ACTION, wp, lp)
    check("physical mouse click dropped by default", len(emitted) == 1, str(emitted))
    # flags=0x10 (the KEYBOARD injected bit) must be PHYSICAL for mouse →
    # proves the mask is LLMHF_INJECTED 0x01, not LLKHF_INJECTED 0x10
    wp, lp = _ms_addr(d.WM_LBUTTONDOWN, flags=0x10)
    b.mouse_proc(d.HC_ACTION, wp, lp)
    check("mouse flags=0x10 treated as PHYSICAL (mask is 0x01)",
          len(emitted) == 1, str(emitted))
    # injected move carries is_physical=False through to the coalescer
    wp, lp = _ms_addr(d.WM_MOUSEMOVE, x=5, y=6, flags=d.LLMHF_INJECTED)
    b.mouse_proc(d.HC_ACTION, wp, lp)
    check("injected move flagged synthetic in coalescer",
          moves and moves[-1][2] is False, str(moves))
    # a non-move mouse event carrying SELFTEST_MARKER sets the event, emits nothing
    check("selftest event initially clear", not b._selftest_event.is_set())
    wp, lp = _ms_addr(d.WM_LBUTTONDOWN, flags=d.LLMHF_INJECTED,
                      extra=d.SELFTEST_MARKER)
    b.mouse_proc(d.HC_ACTION, wp, lp)
    check("mouse selftest marker sets event", b._selftest_event.is_set())
    check("mouse selftest marker NOT recorded", len(emitted) == 1, str(emitted))


def test_install_and_teardown():
    print("install / teardown:")
    b, _, _, _, u, _ = _recording_backend(sethook_results=(0x1111, 0x2222))
    ok = b.install()
    check("install ok (both hooks)", ok and b.kbd_hook == 0x1111 and b.mouse_hook == 0x2222)
    check("SetWindowsHookExW called 2x", len(u.SetWindowsHookExW.calls) == 2)
    check("hook ids WH_KEYBOARD_LL/WH_MOUSE_LL",
          u.SetWindowsHookExW.calls[0][0] == d.WH_KEYBOARD_LL
          and u.SetWindowsHookExW.calls[1][0] == d.WH_MOUSE_LL)
    b.teardown()
    check("teardown unhooked both", len(u.UnhookWindowsHookEx.calls) == 2)
    # partial failure: mouse hook returns 0 → install False, surviving kbd hook freed
    b2, _, _, _, u2, _ = _recording_backend(sethook_results=(0x3333, 0))
    check("install fails on partial", b2.install() is False)
    check("surviving hook cleaned up", len(u2.UnhookWindowsHookEx.calls) == 1)


def test_message_loop():
    print("GetMessage loop (>0 / 0 / -1):")
    b, _, _, _, u, _ = _recording_backend(getmsg_results=[1, 1, 0])
    n = b.pump()
    check("dispatched 2 before WM_QUIT", n == 2, str(n))
    check("Translate/Dispatch each 2x",
          len(u.TranslateMessage.calls) == 2 and len(u.DispatchMessageW.calls) == 2)
    b2, _, _, _, u2, _ = _recording_backend(getmsg_results=[1, -1, 1])
    n2 = b2.pump()
    check("stops on -1 (error) after 1", n2 == 1, str(n2))


def test_probe_payload():
    """Validate synthesize_probe's ACTUAL INPUT array (finding: a mock that
    fabricates its own struct can't catch a wrong union member / missing
    marker / wrong cbSize — real Windows would inject garbage silently)."""
    print("SendInput probe payload (validated from the real array):")
    cap = {}

    def capture(n, arr, size):
        cap["n"], cap["arr"], cap["size"] = int(n), arr, int(size)
        return int(n)

    b, _, _, _, _, _ = _recording_backend(sendinput_fn=capture)
    sent = b.synthesize_probe()
    check("SendInput returned 2", sent == 2)
    check("nInputs == 2", cap.get("n") == 2)
    check("cbSize == sizeof(INPUT) == 40", cap.get("size") == ctypes.sizeof(d.INPUT) == 40,
          str(cap.get("size")))
    pa = ctypes.cast(cap["arr"], ctypes.POINTER(d.INPUT))
    for i, want_flags in ((0, 0), (1, d.KEYEVENTF_KEYUP)):
        check(f"input[{i}].type == INPUT_KEYBOARD", int(pa[i].type) == d.INPUT_KEYBOARD)
        check(f"input[{i}].ki.wVk == VK_F24", int(pa[i].u.ki.wVk) == d.VK_F24,
              hex(int(pa[i].u.ki.wVk)))
        check(f"input[{i}].ki.dwFlags == {want_flags}",
              int(pa[i].u.ki.dwFlags) == want_flags, hex(int(pa[i].u.ki.dwFlags)))
        check(f"input[{i}].ki.dwExtraInfo carries SELFTEST_MARKER",
              int(pa[i].u.ki.dwExtraInfo) == d.SELFTEST_MARKER,
              hex(int(pa[i].u.ki.dwExtraInfo)))


def test_selftest_handshake_faithful():
    """The handshake under the REAL delivery contract: SendInput queues; the
    callback fires only when the INSTALLING thread retrieves messages."""
    print("SendInput self-confirm handshake (faithful Win32 delivery model):")

    # 1) CORRECT production shape: install+pump on the hook-owner thread,
    #    self_test waits on another thread (this is what _run_windows now does,
    #    with the roles mirrored: owner thread here is a helper).
    fw, b, emitted = _faithful_backend()
    ready = threading.Event()

    def owner():
        b.install()
        ready.set()
        b.pump()                       # installer thread retrieves messages

    t = threading.Thread(target=owner, daemon=True)
    t.start()
    ready.wait(2.0)
    res = b.self_test(timeout=2.0)     # waits on THIS thread; owner pumps
    fw.post_quit()
    t.join(3.0)
    check("correct shape: selftest ok", res["ok"] is True, str(res))
    check("correct shape: sent 2 inputs", res["sent_inputs"] == 2, str(res))
    check("correct shape: marker NOT recorded as input", emitted == [], str(emitted))
    check("correct shape: payload accepted by SendInput validation",
          fw.sendinput_errors == [], str(fw.sendinput_errors))
    check("health record shape", res["check"] == "win_input_hooks" and "detail" in res)

    # 2) WRONG shape (the pre-fix doctor bug): install on THIS thread, pump on
    #    a helper thread, wait here. Per-thread queues → helper can never
    #    dispatch this thread's hook → must FAIL. A synchronous mock passed
    #    this; the faithful model must not.
    fw2, b2, _ = _faithful_backend()
    b2.install()                       # installer = main thread
    t2 = threading.Thread(target=b2.pump, daemon=True)   # pumps WRONG queue
    t2.start()
    res2 = b2.self_test(timeout=0.4)   # main blocked, not pumping
    fw2.post_quit()
    t2.join(3.0)
    check("wrong shape (pump on non-installing thread) FAILS",
          res2["ok"] is False and res2["fired"] is False, str(res2))
    check("wrong shape: probe stayed queued (never dispatched)",
          len(fw2.pending) == 2, str(len(fw2.pending)))

    # 3) Doctor shape: install + self_test_pumping on ONE thread (this thread
    #    retrieves its own messages while waiting) → OK.
    fw3, b3, emitted3 = _faithful_backend()
    b3.install()
    res3 = b3.self_test_pumping(timeout=2.0)
    check("self_test_pumping on installing thread ok", res3["ok"] is True, str(res3))
    check("self_test_pumping: marker not recorded", emitted3 == [], str(emitted3))

    # 4) Nobody pumping at all → timeout → ok False.
    fw4, b4, _ = _faithful_backend()
    b4.install()
    res4 = b4.self_test(timeout=0.2)
    check("selftest times out when nobody pumps",
          res4["ok"] is False and res4["fired"] is False, str(res4))

    # 5) Payload fidelity: a probe whose dwExtraInfo is NOT the marker must
    #    fail the handshake AND leak into the recording (proves delivery is
    #    derived from the real array, not fabricated by the mock).
    fw5, b5, emitted5 = _faithful_backend()
    ready5 = threading.Event()

    def owner5():
        b5.install()
        ready5.set()
        b5.pump()

    t5 = threading.Thread(target=owner5, daemon=True)
    t5.start()
    ready5.wait(2.0)
    bad = (d.INPUT * 2)()
    for i, up in enumerate((0, d.KEYEVENTF_KEYUP)):
        bad[i].type = d.INPUT_KEYBOARD
        bad[i].u.ki = d.KEYBDINPUT(wVk=d.VK_F24, wScan=0, dwFlags=up, time=0,
                                   dwExtraInfo=0)          # WRONG: no marker
    b5._selftest_event.clear()
    fw5.user32.SendInput(2, bad, ctypes.sizeof(d.INPUT))
    fired = b5._selftest_event.wait(1.0)
    time.sleep(0.05)
    fw5.post_quit()
    t5.join(3.0)
    check("marker-less probe does NOT set selftest event", fired is False)
    check("marker-less probe IS recorded as synthetic input (payload matters)",
          len(emitted5) == 2 and emitted5[0]["data"]["keycode"] == d.VK_F24
          and emitted5[0]["data"]["kind"] == "synthetic", str(emitted5))


def test_capture_region_math():
    print("capture region math (HWND rect → mss region):")
    r = ps.rect_to_region(100, 50, 900, 650)   # 800x600 at (100,50)
    check("normal rect → region", r == {"left": 100, "top": 50, "width": 800, "height": 600}, str(r))
    check("minimized rect rejected", ps.rect_to_region(-32000, -32000, -31900, -31900) is None)
    check("degenerate rect rejected", ps.rect_to_region(10, 10, 10, 10) is None)
    check("1px rect rejected", ps.rect_to_region(0, 0, 1, 1) is None)
    # DPI-awareness is a process-wide import-time call on Windows; here we assert
    # the helper contract that mss and rects share physical-pixel space (identity
    # math), and that the shim exposes the DPI intent.
    check("region math is physical-pixel identity",
          ps.rect_to_region(0, 0, 1920, 1080) == {"left": 0, "top": 0, "width": 1920, "height": 1080})


def test_module_importable_on_this_os():
    print("cross-platform import safety:")
    check("structs defined", hasattr(d, "KBDLLHOOKSTRUCT") and hasattr(d, "INPUT"))
    check("pure decoders present", callable(d.decode_kbd_event) and callable(d.decode_mouse_event))
    check("hookproc factory works here", d._hookproc_type() is not None)


def test_struct_layout_matches_win32_sdk():
    """Byte-exact to the Win32 x64 SDK regardless of host (fixed-width fields).
    A wintypes.DWORD would be 8 bytes on LP64 macOS and silently break layout.
    Sizes alone can self-cancel a field-ORDER bug (the tests build structs with
    the same ctypes classes the decoder casts to), so we also pin every field
    OFFSET against the SDK and decode RAW struct.pack'ed buffers."""
    print("struct layout (byte-exact to Win32 x64 SDK):")
    expect = {"INPUT": 40, "KEYBDINPUT": 24, "MOUSEINPUT": 32,
              "KBDLLHOOKSTRUCT": 24, "MSLLHOOKSTRUCT": 32, "POINT": 8}
    for name, want in expect.items():
        got = ctypes.sizeof(getattr(d, name))
        check(f"sizeof({name})=={want}", got == want, f"got {got}")
    check("INPUT.u union at offset 8 (DWORD type + 4 pad)", d.INPUT.u.offset == 8)

    # Per-field offsets straight from the x64 SDK headers.
    sdk_offsets = {
        "KBDLLHOOKSTRUCT": {"vkCode": 0, "scanCode": 4, "flags": 8,
                            "time": 12, "dwExtraInfo": 16},
        "MSLLHOOKSTRUCT": {"pt": 0, "mouseData": 8, "flags": 12,
                           "time": 16, "dwExtraInfo": 24},
        "KEYBDINPUT": {"wVk": 0, "wScan": 2, "dwFlags": 4,
                       "time": 8, "dwExtraInfo": 16},
        "MOUSEINPUT": {"dx": 0, "dy": 4, "mouseData": 8, "dwFlags": 12,
                       "time": 16, "dwExtraInfo": 24},
    }
    for cls_name, fields in sdk_offsets.items():
        cls = getattr(d, cls_name)
        for fld, off in fields.items():
            got = getattr(cls, fld).offset
            check(f"{cls_name}.{fld} @ {off}", got == off, f"got {got}")

    # RAW byte buffers (independent of the ctypes classes): Windows writes
    # these fields at fixed offsets; the decoder must read them back exactly.
    print("raw packed-byte decode (offset-independent proof):")
    raw_kb = pystruct.pack("<4IQ", 0x41, 30, d.LLKHF_INJECTED, 111, 0xDEAD)
    check("raw KBD buffer is 24 bytes", len(raw_kb) == 24, str(len(raw_kb)))
    buf_kb = ctypes.create_string_buffer(raw_kb, len(raw_kb))
    dec = d.decode_kbd_event(d.WM_KEYDOWN, ctypes.addressof(buf_kb))
    check("raw KBD decode", dec is not None
          and dec["data"]["keycode"] == 0x41 and dec["data"]["scancode"] == 30
          and dec["is_physical"] is False and dec["tick"] == 111
          and dec["extra"] == 0xDEAD, str(dec))
    raw_ms = pystruct.pack("<iiIII4xQ", -5, 7, (0xFF88 << 16) & 0xFFFFFFFF,
                           0, 222, 0x33)
    check("raw MS buffer is 32 bytes", len(raw_ms) == 32, str(len(raw_ms)))
    buf_ms = ctypes.create_string_buffer(raw_ms, len(raw_ms))
    dec2 = d.decode_mouse_event(d.WM_MOUSEWHEEL, ctypes.addressof(buf_ms))
    check("raw MS wheel decode", dec2 is not None
          and dec2["x"] == -5 and dec2["y"] == 7
          and dec2["data"]["dy"] == -120 and dec2["data"]["dx"] == 0
          and dec2["is_physical"] is True and dec2["tick"] == 222
          and dec2["extra"] == 0x33, str(dec2))


if __name__ == "__main__":
    print("=" * 70)
    print("windows input backend — mock simulation (runs on macOS)")
    print("=" * 70)
    test_module_importable_on_this_os()
    test_struct_layout_matches_win32_sdk()
    test_keyboard_decode_and_gate()
    test_mouse_decode()
    test_mouse_injected_gate_and_marker()
    test_install_and_teardown()
    test_message_loop()
    test_probe_payload()
    test_selftest_handshake_faithful()
    test_capture_region_math()
    print("=" * 70)
    if _fails:
        print(f"RESULT: {_fails} FAILURE(S)")
        sys.exit(1)
    print("RESULT: all windows-input-sim tests passed")
    sys.exit(0)
