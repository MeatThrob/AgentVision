# Finishing the Windows port

**Read this if you are on Windows and want to take AgentVision from "built and
unit-tested" to "verified working".**

The Windows port is written. Every OS-specific path has a Windows branch, the
Windows-only pieces have their own unit tests, and the whole tree byte-compiles.
What has **never happened** is a real end-to-end run on Windows since a large
refactor. macOS is the verified platform; Windows is the one that needs you.

This document is deliberately specific about what is known-good, what is
known-unverified, and what is known-*wrong*. Nothing here is a guess dressed up as
a status.

---

## 1. What is already done

| Area | Windows implementation | Status |
|---|---|---|
| Window enumeration | `win32gui.EnumWindows` via **pywin32** (`utils/platform_shim.py::_find_window_win`) | written, unit-tested with mocks |
| Frame capture | **`mss`** region grab of the window rect | written; see the real limitation in §4 |
| Input recording | Low-level hooks `WH_KEYBOARD_LL` / `WH_MOUSE_LL` via `SetWindowsHookEx` (`daemon/input_daemon.py`) | written, has a `SendInput` self-test |
| Input synthesis test | `daemon/test_win_input_sim.py` | passes on macOS as a mock; **needs a real run** |
| On-screen text (OCR) | `Windows.Media.Ocr` (WinRT) through the **`winsdk`** wheel — zero system install | written, probe implemented |
| Launchers | `Start AgentVision.bat`, `Start Bridge (headless).bat`, `install-dependencies.bat` | written |
| Setup guide | `SETUP-Windows.md` (includes MCP registration + `mcp` install) | current |
| Dependencies | `requirements-windows.txt` — adds `mss`, `pywin32`, `winsdk` over the base set | current |

Every OS branch is centralised in `utils/platform_shim.py`. If you find OS-specific
code anywhere else, that is a bug worth reporting.

---

## 2. The one job: run the suite and report

```bat
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements-windows.txt
pip install "mcp>=2.0,<3"
python run_all_tests.py
```

Expected: **58 suites, all pass**, with `bridge_gate` the only SKIP unless a
bridge is already listening (it tests the first-connection contract, so it needs
a live one — start `python python_backend\api\bridge_server.py --no-autocapture`
first to include it). On macOS that is the current result.

If anything fails, that failure is the deliverable — please capture the verbatim
output rather than summarising it. Several of these suites exist specifically
because a summary once hid a real defect.

Then the honest part, which unit tests cannot do:

```bat
python -m python_backend.cli doctor
```

`doctor` reports capture capability, input-hook capability and OCR engine
availability as *facts* rather than making you infer them from empty frames. Read
its output before concluding anything works.

---

## 3. Start here: the highest-risk path

**Run in `mode=changes` or `mode=all` on a machine with NO OCR engine.**

That combination is where the one genuine Windows-specific defect lived, and it
was invisible on macOS because macOS has OCR built into the OS.

The mechanism: with no OCR engine, every frame classifies `could_not_read`. A
retention change made such frames rank higher so they would not be evicted first
— but `needs_eyes` derives from the same rank, so in `changes`/`all` mode every
frame also became *awaiting examination*, and the disk filled to the 900-second
hold backstop on a machine where nothing was wrong.

It is **fixed** (`utils/retention.py::assess` sets `needs_eyes=False` explicitly
for that case) and covered by `api/test_ocr_honesty.py`, which measures a 60-frame
over-budget ledger. Measured counterfactual: the old rule held 50 of 60 frames, the
current rule holds 0.

Please still verify it on real hardware, because it was found by reasoning about
Windows from a Mac, and that is exactly the kind of finding that deserves a second
look on the actual platform. Test both with and without `winsdk`/tesseract present.

---

## 4. Known limitation — capture, and it is not a bug you can fix

On macOS, `screencapture -x -l<window-id>` reads the window's **backing store**, so
a frame contains only the target program even when it is occluded by other windows
or fully minimised to the Dock. That was verified.

Windows has no capture-by-window-id equivalent, so AgentVision does an `mss`
**region grab** of the window rectangle. Consequences:

- **Occlusion-sensitive.** Another window on top of the target appears in the frame.
- **Blind to minimised windows.** There is no rectangle to grab.

AgentVision handles the second case by **skipping the frame** rather than
falling back to a full-screen capture — a fallback would silently store your
entire desktop and feed it to an AI agent. Skips are counted in
`capture/status.health.frames_skipped_no_window`, so a skipped frame is visible,
not silent.

If you want to close this properly, the real fix is `PrintWindow` with
`PW_RENDERFULLCONTENT`, which can capture an occluded window's content on Windows
10+. **It is not implemented** — the README's platform table says `mss` region
grab, which is the truth. This is the single largest functional gap between the
macOS and Windows experience, and it is a well-scoped contribution.

---

## 5. Traps that already cost time — do not rediscover them

**`platform_shim.IS_MAC` is the gating idiom, not `sys.platform`.** A plain
`grep sys.platform` produces false alarms about "ungated macOS calls" that are in
fact properly gated. This has wasted time twice.

**A long-running bridge runs stale code.** Flask is not in debug mode, so editing
a `.py` file does not reload it. After any change to `bridge_plan.py`,
`ambient.py` or `bridge_server.py`, **restart the bridge** before trusting a
result. A whole debugging session was spent on a "bug" that was an unrestarted
process.

**Locale-default text encoding.** ~57 sites read AgentVision's own JSON with
`open()` / `read_text()` and no `encoding=`. On Windows that is cp1252, not UTF-8.
These are **latent, not broken**: `json.dumps` ASCII-escapes its output and these
files round-trip on one machine, so nothing fails today. The path that reads
*foreign* data — your program's logs, in `connectors/log_sources.py` — is already
correct (binary reads plus one explicit `encoding="utf-8"`). If you hit a
`UnicodeDecodeError` anywhere, this is the first thing to suspect, and adding
`encoding="utf-8"` is the fix.

**Illegal filename characters.** Filenames containing `<` or `>` are legal on
POSIX and illegal on Windows. One test created a directory literally named
`<project>` and would have hard-failed here; it now uses `demoapp`. If you see a
path-creation failure, check for this shape.

**Low-level hooks must be installed and pumped on the SAME thread.** Windows only
delivers `WH_KEYBOARD_LL` / `WH_MOUSE_LL` events while the *installing* thread is
inside a message loop (`GetMessage`/`PeekMessage`). A non-pumping `Event.wait()`
on that thread guarantees a timeout even when the hooks are installed correctly.
This is documented at `daemon/input_daemon.py:605` and was a real blocker found
during the port — the mock now models asynchronous delivery so the test cannot
pass while the real thing is broken.

---

## 6. What to report back

Useful, in rough order:

1. The verbatim `run_all_tests.py` output — suite count, and any FAIL or SKIP lines.
2. `python -m python_backend.cli doctor` output.
3. Whether `SETUP-Windows.md` works followed **literally**, and anything required
   but undocumented. A Mac user following the equivalent guide got no `av_*` tools
   at all because MCP registration was missing from it; assume the Windows guide
   has a comparable hole until proven otherwise.
4. Whether a real bridge cycle works: create a profile → the first-connection
   catalog → commit a plan → capture → `av_diagnose()` returns something true
   about a program you know is broken.
5. The `mode=changes` / no-OCR disk test from §3.
6. Anything AgentVision *claimed* that was not true. That is the highest-value
   bug report for this project — its governing rule is that it may not assert
   what it did not verify, and every violation found so far has been worth more
   than a crash.

---

## 7. Scope note

Linux is in the same position: written, unit-tested (`utils/test_linux_platform.py`),
init templates in `dist/linux/`, but not re-run end to end. X11 gets window
enumeration and capture; **Wayland cannot enumerate windows at all** — a
compositor security restriction, not something AgentVision can work around, so it
uses portal-based capture and loses per-window targeting.

Anything you learn here about the message-pump, encoding or capture traps almost
certainly applies there too.
