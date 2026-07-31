"""
AgentVision Control Panel
--------------------------
Standalone Tkinter GUI for managing connected programs, viewing live
diagnostic data, and controlling the bridge server — all in one window.

Run: python python_backend/gui/agent_vision_gui.py
"""
from __future__ import annotations
import sys
import os
import json
import signal
import subprocess
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path
from datetime import datetime
import threading
import socket
import struct

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from connectors.program_connector import (
    ProgramProfile, load_profiles, save_profiles, BUILTIN_PROFILES,
    profile_output_folder
)
from utils.checkpoint_manager import save_all_checkpoints
from utils import platform_shim

try:
    import requests
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False

BRIDGE_URL = "http://127.0.0.1:7771"

# ── Colour palette (dark theme) ───────────────────────────────────────────────
BG       = "#0f1117"
BG2      = "#1a1d27"
BG3      = "#22263a"
ACCENT   = "#4f8ef7"
GREEN    = "#4ade80"
RED      = "#f87171"
ORANGE   = "#fb923c"
YELLOW   = "#facc15"
TEXT     = "#e2e8f0"
TEXTDIM  = "#64748b"
# Platform-appropriate fonts (Tk falls back gracefully, but pick natives).
_MONO_FACE = ("Consolas" if platform_shim.IS_WINDOWS else
              "Menlo" if platform_shim.IS_MAC else "DejaVu Sans Mono")
_UI_FACE   = ("Segoe UI" if platform_shim.IS_WINDOWS else
              "Helvetica Neue" if platform_shim.IS_MAC else "DejaVu Sans")
MONO     = (_MONO_FACE, 11)
MONOSM   = (_MONO_FACE, 10)
HEADING  = (_UI_FACE, 13, "bold")
LABEL    = (_UI_FACE, 11)
LABELBOLD= (_UI_FACE, 11, "bold")


class _LabelBtn(tk.Frame):
    """
    A Label-in-Frame button that macOS Tk cannot recolour.
    tk.Button bg/fg are ignored by the macOS theme; tk.Label inside a
    tk.Frame is rendered exactly as specified.
    """
    def __init__(self, parent, text, command, bg=BG3, fg=TEXT,
                 font=None, padx=10, pady=4, state="normal", **kw):
        super().__init__(parent, bg=bg, cursor="hand2", **kw)
        self._bg       = bg
        self._fg       = fg
        self._bg_dim   = self._darken(bg)
        self._cmd      = command
        self._disabled = (state == "disabled")
        self._lbl = tk.Label(
            self, text=text,
            bg=bg, fg=fg if not self._disabled else TEXTDIM,
            font=font or LABEL,
            padx=padx, pady=pady,
            cursor="hand2")
        self._lbl.pack()
        if not self._disabled:
            self._lbl.bind("<Button-1>",        self._on_click)
            self._lbl.bind("<Enter>",            self._on_enter)
            self._lbl.bind("<Leave>",            self._on_leave)
            self.bind("<Button-1>",              self._on_click)
            self.bind("<Enter>",                 self._on_enter)
            self.bind("<Leave>",                 self._on_leave)

    @staticmethod
    def _darken(hex_color: str) -> str:
        """Return a slightly darker shade of a hex colour."""
        h = hex_color.lstrip("#")
        if len(h) != 6:
            return hex_color
        r, g, b = int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)
        r, g, b = max(0,r-30), max(0,g-30), max(0,b-30)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _on_click(self, _=None):
        if not self._disabled and self._cmd:
            self._cmd()

    def _on_enter(self, _=None):
        if not self._disabled:
            self.config(bg=self._bg_dim)
            self._lbl.config(bg=self._bg_dim)

    def _on_leave(self, _=None):
        if not self._disabled:
            self.config(bg=self._bg)
            self._lbl.config(bg=self._bg)

    def config(self, **kw):
        # Intercept state changes
        state = kw.pop("state", None)
        text  = kw.pop("text", None)
        if state is not None:
            self._disabled = (state == "disabled")
            self._lbl.config(fg=TEXTDIM if self._disabled else self._fg)
        if text is not None:
            self._lbl.config(text=text)
        if kw:
            super().config(**kw)

    # Make pack/grid/place work naturally
    def pack(self, **kw):
        super().pack(**kw)
        return self

    def grid(self, **kw):
        super().grid(**kw)
        return self


def _btn(parent, text, cmd, bg=BG3, fg=TEXT, padx=10, pady=4,
         state="normal", font=None, **kw) -> _LabelBtn:
    """Factory that returns a macOS-safe coloured button."""
    b = _LabelBtn(parent, text=text, command=cmd,
                  bg=bg, fg=fg, padx=padx, pady=pady,
                  state=state, font=font or LABEL)
    return b


# ── Universal log-system snapshot (LIVE counts) ─────────────────────────────────
# The whole GUI reads these numbers from the running registry so they can never
# go stale. Guarded so a missing/broken connector package still lets the GUI
# open — it just falls back to the last-known-good defaults.
def _coverage_snapshot() -> dict:
    """Live snapshot of AgentVision's universal log system for the GUI.
    Never raises. `named` = every named format adapter (REGISTRY minus the two
    universal fallbacks `structural` + `raw`); `families` = distinct adapter
    modules; `readers` = registered binary/stream source readers."""
    snap = {
        "ok": False, "error": "",
        # Sane fallbacks (overwritten by the live import below) so the text
        # still reads correctly even if the import ever fails.
        "total": 658, "named": 656, "families": 25, "readers": 9,
        "reader_names": [], "families_map": {}, "adapters": [],
    }
    try:
        from connectors import log_adapters as _la
        reg = list(_la.REGISTRY)
        adapters: list[dict] = []
        fams: dict[str, list] = {}
        for a in reg:
            try:
                mod = a.__class__.__module__.split(".")[-1]
            except Exception:
                mod = "?"
            name = getattr(a, "name", "?")
            lang = getattr(a, "language", "") or ""
            adapters.append({"name": name, "language": lang, "module": mod})
            fams.setdefault(mod, []).append({"name": name, "language": lang})
        snap["adapters"] = adapters
        snap["families_map"] = fams
        snap["total"] = len(reg)
        snap["named"] = max(0, len(reg) - 2)   # minus `structural` + `raw`
        snap["families"] = len(fams)
        snap["ok"] = True
    except Exception as e:                       # pragma: no cover - defensive
        snap["error"] = f"adapters: {e}"
    try:
        from connectors import log_sources as _ls
        readers = list(_ls.list_readers())
        snap["reader_names"] = readers
        snap["readers"] = len(readers)
    except Exception as e:                       # pragma: no cover - defensive
        snap["error"] = (snap["error"] + f"; readers: {e}").strip("; ")
    return snap


_COVERAGE = _coverage_snapshot()
_NAMED     = _COVERAGE["named"]
_FAMILIES  = _COVERAGE["families"]
_READERS   = _COVERAGE["readers"]


# ── How-To content ────────────────────────────────────────────────────────────
# Everything below describes the CURRENT reality: AgentVision is a UNIVERSAL,
# any-language, any-program, cross-platform bridge — not a Python-only tool.
HOW_TO_SECTIONS = [
    (
        "OVERVIEW — What is AgentVision?",
        f"""\
AgentVision is a UNIVERSAL debug bridge between ANY running program — in ANY
language, on ANY OS — and Claude (the AI). It gives the AI two things at once
and locks them to a single clock:

  →  EYES — periodic screenshots of the exact window / region / screen your
     program draws to, each saved as a PNG with a self-describing JSON sidecar.
  →  INSTRUMENTATION — every log your program writes, in whatever language and
     format, normalized into ONE unified JSON event stream.

Python, Node, Ruby, Java, .NET, Go, Rust, C/C++, PHP, shell — anything that
runs and logs — is bridged with zero code changes. Compiled GUIs and games
(no useful stdout) are covered by the screenshot side.

The AI reads the image for visual context and the JSON for precise machine
state, and — the part that makes the pair useful — every frame is TIME-ALIGNED
to the logs so the AI can trust "what every log said at this exact shutter."

You (the developer) pick WHICH program AgentVision watches via a "Profile".
Everything else is automatic once the bridge is running.
""",
    ),
    (
        "QUICK START — attach ANY program (5 steps)",
        """\
1. Drop your program into the Profile tab's Drop Zone (paste/drag/Browse) — a
   .py, .js, .rb, .jar, a compiled binary, a .app, or the project FOLDER.
   AgentVision detects the language and installs the bridge (see AUTO-INSTALL).
2. Or click "+ New" in the sidebar and set Project Root by hand, then
   "Auto-Detect" to find logs / stats / state files automatically.
3. Fill in Display Name, click "Save Profile", then "Set Active".
4. Click "Start Bridge" (top-right). The dot turns green when online.
5. Use the SCREENSHOTS bar (top of window) to Start/Stop/Pause capturing, and
   set how many shots per second you want (see SHOTS PER SECOND).

The universal command-line front door (outside this GUI) is:

      agentvision attach <project_dir>     # scaffold agentvision/ + emitter
      agentvision run -- <your program>    # inject hooks + tee, then run

`agentvision run` injects the right env for in-process languages AND tees
stdout/stderr for everything else, so ONE command bridges any program.
""",
    ),
    (
        "UNIVERSAL — any language, any program, any OS",
        f"""\
AgentVision's promise: connect to ANY program in ANY language and already have
the log ready. Two sides make that true.

INPUT side (built in) — it READS whatever your program writes and normalizes
every line into one JSON schema. {_NAMED} named format adapters, organized in
{_FAMILIES} family modules, cover the real-world catalog; the universal
`structural` normalizer catches anything unrecognized; the `raw` floor catches
the rest. See the "Log Coverage" tab to browse them and test a line live.

OUTPUT side (auto-installed) — it makes your program WRITE those logs, with no
edits to your source. On first attach it scaffolds an `agentvision/` folder
inside the project holding the sink files and a per-language emitter.

Works whether your program is:
  →  a scripting language  (Python / Node / Ruby — hooked in-process)
  →  a JVM / .NET app       (a drop-in logger config routes output to the sink)
  →  a compiled binary/game (Go / Rust / C / C++ — stdout+stderr teed & parsed)
  →  a GUI with no stdout   (covered by the screenshot side + input daemon)

No language is special-cased as "the only one" — Python is just one of many.
""",
    ),
    (
        "AUTO-INSTALL — how attach/run bridges your program",
        """\
`agentvision attach` (or the Drop Zone here) detects the project's language and
scaffolds a self-contained agentvision/ folder holding the sink files
(actions.jsonl + log.txt) and a language-appropriate EMITTER. Existing files
are NEVER overwritten. How each language is hooked (honest about the mechanism):

  →  autoload      Python — sitecustomize.py; the interpreter loads the emitter
                   on a normal launch, no env var, no code change.
  →  env_autoload  Node (NODE_OPTIONS=--require) / Ruby (RUBYOPT=-r) — in-process,
                   one env var set automatically by `agentvision run`.
  →  config        Java (logback/log4j2) / .NET (Serilog) — a drop-in logger
                   config that routes output to the sink.
  →  tee           Go / Rust / C / C++ / shell / anything else — launch via
                   `agentvision run -- <cmd>`; stdout+stderr are teed and
                   normalized through the SAME adapters.

Every emitter, whatever the language, writes the SAME unified JSONL schema.
The Profile tab shows the detected language + which emitter it would install,
and "Re-Install + Verify" proves the bridged program is actually emitting.
""",
    ),
    (
        "CAPTURE — window / region / full-screen",
        """\
For compiled GUIs, games, bots and long-running apps the screenshot side is the
main signal. Every frame targets the shot in a strict priority order:

  →  Window  — capture the target program's window by its window ID. On macOS
     this grabs the window regardless of position, overlap, or being off-screen.
     On Windows/Linux the window path is a region grab of the window bounds.
  →  Region  — a manual x,y,w,h crop for a fixed sub-region (set it in the
     Profile tab's SCREEN CAPTURE section via "Pick Region…").
  →  Full-screen — the fallback when no window/crop is set or the window is
     missing (AgentVision tells the AI it fell back, and why — never silently).

Set "Capture App" to shoot a specific program's window, or "Custom Crop" to
shoot a fixed rectangle. Leave both blank for full-screen. A blank/black frame
(minimized, occluded, not yet painted) is detected and flagged so the AI does
not hallucinate visual content from an empty image.
""",
    ),
    (
        "SHOTS PER SECOND — the capture rate (AI asks you)",
        """\
Capture cadence is user-configurable and measured in shots per second. The
SCREENSHOTS bar slider sets it; the label shows both seconds/shot and shots/sec.

  →  Supported range: 0.1 – 10 shots/sec  (interval 10s … 0.1s).
  →  The desktop slider offers a friendlier 0.5 – 10 shots/sec sub-range.
  →  Faster = finer visual detail but more frames to review; slower = fewer.

Why the AI ASKS you the rate: the right cadence is a trade-off only you and the
bug can settle — a fast animation/race/crash wants many shots/sec, a long idle
wait wants few. Because every frame is time-aligned to the logs, the rate is
purely about visual resolution and review cost. So at the START or CONTINUATION
of every project the model is instructed to ask you for a shots/sec value and
present the full range, then apply it with av_capture_set_interval(1/fps) —
e.g. 4 shots/sec = interval 0.25s.
""",
    ),
    (
        "ONE JSON TIMELINE — every log, one schema",
        f"""\
No matter the language or format, every log line becomes the SAME event shape:

      ts / ts_ms     — one UTC clock (ISO string + epoch ms)
      category       — log | debug | warn | error | event  (derived from level)
      level          — canonical INFO / WARN / ERROR / FATAL …
      source         — logger / subsystem / adapter
      trace_id       — correlation id if present
      data.message   — the human message + structured fields
      raw            — the original line, never lost

An ERROR from ANY ecosystem (Java SEVERE, Go/zap fatal, .NET WRN, syslog
numerics …) maps onto one canonical vocabulary, so failure detection fires
everywhere. Three routes guarantee nothing is dropped:

  →  {_NAMED} NAMED adapters ({_FAMILIES} family modules) recognize known
     formats. One `jsonl` super-adapter alone normalizes every JSON logger
     (pino/winston/bunyan, structlog/loguru, zap/slog/logrus, logback/log4j2,
     Serilog/NLog, GELF, MongoDB, journald, Docker, Caddy, Envoy, OTLP …).
  →  The `structural` normalizer turns any UNRECOGNIZED line into valid JSON by
     shape (syslog, kernel ring-buffer, logfmt, embedded JSON, CLF …).
  →  The `raw` floor captures whatever is left. Detection never fails.

Binary/encoded streams (login records, NetFlow, pcap, MRT, Snort unified2 …)
are decoded by {_READERS} SOURCE READERS into the same schema. And when a
program writes several logs at once, they are MERGED timestamp-sorted onto the
one timeline, each event tagged with which log it came from.

Browse and test all of this in the "Log Coverage" tab.
""",
    ),
    (
        "TIME-ALIGNMENT — frames locked to logs",
        """\
Eyes and logs are only useful together if the AI can trust that THIS screenshot
lines up with THOSE log lines. AgentVision makes that alignment exact.

  →  One clock — screenshots, log lines and frame metadata all stamp through the
     same UTC-ms clock, so N heterogeneous logs and the frames share one
     monotonic timeline.
  →  The shutter — immediately BEFORE grabbing pixels, the engine stamps the
     time AND snapshots the byte sizes of the log files in the same instant. The
     log window for a frame is bounded to bytes present at the shutter, so no
     line that arrived DURING the ~100ms capture can leak in and mislead the AI.
  →  capture_meta — every frame records shutter_ms, capture_end_ms, the byte
     offsets, the capture target (window/crop/fullscreen) and blank/health, so
     the correlation is provable, not assumed.

The AI exploits this with av_actions_around_frame (what the program logged
±N seconds of a frame) and av_log_normalized (every log, any language, for a
time window). See something on screen → learn exactly what was logged then.
""",
    ),
    (
        "CROSS-PLATFORM — macOS / Windows / Linux",
        """\
One source base runs on macOS (primary), Windows 10/11, and Linux (developed
and packaged for Artix). All OS-specific behavior — screen-capture backends,
window enumeration, input hooks, temp paths, fonts — is isolated behind one
platform shim, so the same profiles and tools work everywhere.

Screen-capture backend per OS:
  →  macOS   — screencapture (window-ID capture survives overlap/off-screen).
  →  Windows — mss region grab (occlusion-sensitive; skips minimized windows).
  →  Linux   — session-aware: mss on X11; grim / portal on Wayland; headless
     reports "not applicable" rather than failing. On Wayland, global window
     enumeration is impossible by design, so capture falls back to region/full.

Permissions differ by OS (see the Permissions tab): macOS asks once for
Accessibility + Screen Recording; Windows and Linux need no special grant for a
normal user account.
""",
    ),
    (
        "PROFILE FIELDS — what each field means",
        """\
Display Name        — Label shown in the sidebar (e.g. "My Bot").
Project Root        — Top-level folder of your program. Set this first —
                      AgentVision auto-detects everything else it can find and
                      detects the language for the emitter.
Log File            — A log your program writes to (any language / format;
                      it is auto-detected). Leave blank to skip.
Stats Folder        — Folder with stats_*.log files (key: value per line).
Config Folder       — Folder with .json / .yaml config files.
State File (JSON)   — A JSON file your program writes its live state to.
Python Executable   — OPTIONAL: only for Python programs (the venv python used
                      to run tests / spawn verification). Ignored otherwise.
Test Directory      — Folder containing tests (e.g. tests/).
Process Name        — Process name for CPU/RAM monitoring (substring match).
Capture App / Crop  — Which window or rectangle to screenshot (SCREEN CAPTURE).
Notes               — Freeform notes, shown in this GUI only.

Leave any field blank — AgentVision simply skips that data source.
""",
    ),
    (
        "DIGEST-FIRST — how Claude reads the output",
        """\
AgentVision optimizes for a model with a token budget: everything is JSON,
everything is schema-documented, and the highest-signal facts are pre-computed.

The FIRST thing the AI reads is the DIGEST (the "Digest" button in the top bar,
or av_digest / GET /digest). It is one compact, ranked JSON that says what to
look at and in what order:

  →  attention   — ranked issues, each naming the tool to drill in with.
  →  health      — 0–100 score + the factors that lowered it.
  →  top_errors  — recurring errors DEDUPED by fingerprint, with counts (so the
     same traceback is shown once with "occurred 400×", not 400 times).
  →  new_error_fingerprints — bugs that JUST appeared this session.
  →  capture     — capturing?, shots/sec, blank-frame + window-missing warnings.
  →  alignment   — image↔log alignment health.

Per frame the AI also gets summary / recommended_next / tags / confidence /
state_delta / structured error before any raw text. One digest round-trip
replaces calling six status tools.
""",
    ),
    (
        "TOKEN ECONOMICS — why this saves the AI money",
        """\
Screenshotting, hashing and diffing run on THIS machine and are effectively
free. Every token the AI spends staring at raw pixels is not. So AgentVision
does the expensive observing up front and hands the AI small JSON instead.

Press the "📉 Tokens" button in the top bar (or av_token_report) to see the
measured numbers for the current session.

WHAT HAPPENS AT CAPTURE TIME, FOR FREE
  →  Every frame gets a perceptual hash (dHash), a change score versus the
     previous frame, and the bounding box of the region that changed. This
     shares the image decode the blank-frame health check already did, so it
     costs about 1 ms per frame at 1080p — against a 100 ms budget at 10 fps.
  →  At 10 shots/sec roughly 99% of consecutive frames are identical. The AI is
     shown CHANGES, not frames.
  →  Failure signatures (screen frozen, blank screen, on-screen error text) are
     detected from the screen itself and auto-bookmarked. A HANG leaves no log
     line at all, so this catches bugs log analysis cannot.

THE FLIGHT RECORDER
  →  Capture runs continuously and old frames are pruned, so the disk does not
     fill. But the moment a failure appears, the window BEFORE it (60 s by
     default) plus a short tail is FROZEN and exempted from pruning.
  →  The crucial moment is therefore already saved before the AI asks for it —
     you usually do not need to reproduce the bug. See av_incidents.

THE CHEAP PATH THE AI IS TOLD TO FOLLOW
  1. av_ui_tree ....... exact element text + coordinates (no OCR guessing)
  2. av_visual_changes  only the moments the screen changed
     av_frame_json ... one frame fully described, with NO image in it
  3. av_frame_region .. ONLY the pixels that changed, downscaled
  4. the full PNG ..... last resort

Pixels are still available and still necessary — icon colour, layout breakage
and rendering corruption are invisible to text. The point is not to start there.

For the research behind this see docs/RESEARCH_TOKEN_EFFICIENCY.md, and to teach
any Claude session these habits paste docs/AGENT_INSTRUCTIONS.md into your
project's CLAUDE.md.
""",
    ),
    (
        "PUSH MODE — telling the AI without being asked",
        """\
Everything else in AgentVision is PULL: the AI asks, AgentVision answers. That
has a hole — the AI has to already SUSPECT something is wrong. Two cases where
it never will: right after a context compaction (it has forgotten AgentVision
exists), and right after its own edit (it has no reason to go looking).

PUSH MODE closes both. Press "📡 Push Mode" in the top bar.

A tiny hook injects a few pre-digested lines at the moments that matter:
  →  session start, INCLUDING after a compaction
  →  before the AI answers a prompt
  →  after an Edit / Write / MultiEdit / Bash  ("did my change break it?")

THE THREE RULES
  →  SILENT by default. Nothing changed = nothing injected = zero tokens.
  →  DELTA-ONLY. It never repeats itself within a session.
  →  BYTE-CAPPED. heartbeat 220B / notice 700B / alert 1200B, hard limits.

Every message carries a "Visual:" line — static vs changing, blank, frozen, or
error text on screen. That is the thing no log file can tell the AI.

MEASURED: an alert costs ~103 est tokens and zero tool calls. The same knowledge
via pull costs ~191 tokens at best and ~1149 on a realistic discovery path, and
needs the AI to go looking. Adds ~89 ms per prompt.

THE STOP BACKSTOP IS OFF ON PURPOSE. A Stop hook can block the AI from ending
its turn, and a bad one traps YOU in a loop. Claude Code documents no loop-guard
for it, so AgentVision ships it disabled. Opting in requires two separate
switches and is bounded by a persistent per-session budget.

TURNING IT OFF — any of these:
  →  the "Enable / Disable" button in the Push Mode window (instant)
  →  agentvision push-mode off
  →  AGENTVISION_PUSH_MODE=0
  →  agentvision uninstall-hooks   (then restart Claude Code)
  →  stop the bridge — with no bridge every hook exits silently

The Push Mode window shows a LIVE PREVIEW of the next injection, so you can see
exactly what the AI would be told before you enable it. Full details in
docs/PUSH_MODE.md.

NOTE: installing or removing hooks requires RESTARTING Claude Code.
""",
    ),
    (
        "TROUBLESHOOTING",
        """\
Bridge dot stays grey (offline):
  → Port 7771 may already be in use:  run  lsof -i :7771  in a terminal.
  → If another AgentVision is running, Stop it first then Start Bridge.
  → Check the bottom status bar — it shows the error if startup failed.

Attached but "Outbound link NOT verified":
  → The install dropped files but the program isn't emitting yet. Restart the
    program, or use "Re-Install + Verify". For Python, make sure the project
    root is importable (sitecustomize on PYTHONPATH); for Node/Ruby launch via
    `agentvision run`; for compiled langs run via `agentvision run -- <cmd>`.

Log/coverage shows nothing:
  → Verify the log path in your profile exists and the program writes to it.
  → Paste a sample line into "Log Coverage → Test a log line" to confirm which
    adapter claims it.

CPU/RAM always shows --:
  → Set "Process Name" to match what appears in your OS process monitor.

Screenshots blank / wrong window:
  → Grant Screen Recording (macOS) in the Permissions tab; set "Capture App" to
    the right window, or "Pick Region…" for a crop.
""",
    ),
]


# ── Auto-detect helper ────────────────────────────────────────────────────────

def _safe_profile_name(s: str) -> str:
    """Sanitize a folder name into a valid profile key (matches CLI's helper)."""
    import re as _re
    return _re.sub(r"[^\w\-]", "_", s).strip("_") or "project"


def _autodetect_profile(root: str) -> dict:
    """Scan a project root and return a dict of detected field values."""
    r = Path(root)
    result: dict[str, str] = {}

    # Log file
    for candidate in [
        "log/log.txt", "log/app.log", "log/runtime.log",
        "logs/app.log", "logs/runtime.log", "app.log", "runtime.log",
    ]:
        p = r / candidate
        if p.exists():
            result["log_file"] = str(p)
            break
    if "log_file" not in result:
        matches = list(r.rglob("*.log"))
        if matches:
            result["log_file"] = str(matches[0])

    # Stats folder
    for candidate in ["log/stats", "logs/stats", "stats", "log/stat"]:
        p = r / candidate
        if p.is_dir():
            result["stats_folder"] = str(p)
            break

    # Config folder
    for candidate in ["config", "conf", "settings", "cfg"]:
        p = r / candidate
        if p.is_dir():
            result["config_folder"] = str(p)
            break

    # Test directory
    for candidate in ["test", "tests", "pytest", "test_suite"]:
        p = r / candidate
        if p.is_dir():
            result["test_dir"] = str(p)
            break

    # State file
    for candidate in ["state.json", "log/state.json", "runtime_state.json"]:
        p = r / candidate
        if p.exists():
            result["state_file"] = str(p)
            break

    # Python executable (venv detection). Windows venvs put the interpreter at
    # Scripts\python.exe; POSIX venvs at bin/python. Probe both, Windows first
    # on Windows so a project's .venv is actually found there.
    _venv_candidates = [
        ".venv/bin/python3", ".venv/bin/python",
        "venv/bin/python3",  "venv/bin/python",
        "env/bin/python3",   "env/bin/python",
    ]
    if platform_shim.IS_WINDOWS:
        _venv_candidates = [
            ".venv/Scripts/python.exe",
            "venv/Scripts/python.exe",
            "env/Scripts/python.exe",
        ] + _venv_candidates
    for candidate in _venv_candidates:
        p = r / candidate
        if p.exists():
            result["python_exe"] = str(p)
            break

    # Display name — split on CamelCase boundaries too, so "ControllerMapper"
    # becomes "Controller Mapper" rather than "Controllermapper".
    import re as _re
    raw_name = r.name.replace("_", " ").replace("-", " ")
    # Insert space before uppercase letters that follow lowercase (CamelCase)
    spaced = _re.sub(r"([a-z])([A-Z])", r"\1 \2", raw_name)
    result["display_name"] = spaced.title()

    # .command launcher → use it as a hint for process_name
    cmds = list(r.glob("*.command"))
    if cmds and "process_name" not in result:
        # The .command typically runs a python script — find the main script
        for name in ["proxy.py", "main.py", "app.py", "run.py",
                     "bot.py", "start.py", "__main__.py"]:
            if (r / name).exists():
                result["process_name"] = name
                break
        else:
            result["process_name"] = cmds[0].name

    return result


# ── Main App ──────────────────────────────────────────────────────────────────

class AgentVisionApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("AgentVision v5 — Control Panel")
        self.geometry("1100x800")
        self.minsize(900, 660)
        self.configure(bg=BG)

        self._profiles: dict[str, ProgramProfile] = load_profiles()
        self._active_name: str = list(self._profiles.keys())[0] if self._profiles else "a game bot"
        self._bridge_proc: subprocess.Popen | None = None
        self._bridge_online = False
        self._poll_after_id = None
        self._log_tail_thread: threading.Thread | None = None
        self._log_tail_running = False

        # Screenshot state
        self._shot_running  = False
        self._shot_paused   = False
        self._shot_interval = tk.DoubleVar(value=1.0)
        self._shot_count    = 0
        self._shot_proc: subprocess.Popen | None = None
        # Whether the bridge should auto-start screenshots when it boots.
        # If False, bridge starts but capture stays idle until user starts it.
        self._capture_on_bridge_start = tk.BooleanVar(value=True)

        self._build_ui()
        self._refresh_profile_list()
        self._select_profile(self._active_name)
        self._start_poll()
        # Clear log panels for fresh session
        for viewer in (self._log_viewer, self._act_viewer, self._raw_viewer):
            viewer.config(state="normal")
            viewer.delete("1.0", "end")
            viewer.config(state="disabled")
        self._start_log_tail()

    # ─────────────────────────────────────────────────────────────────────────
    # Top-level layout
    # ─────────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Screenshot control bar (very top, always visible) ─────────────
        self._build_screenshot_bar()

        ttk.Separator(self, orient="horizontal").pack(fill="x")

        # ── Bridge status bar ─────────────────────────────────────────────
        self._build_bridge_bar()

        ttk.Separator(self, orient="horizontal").pack(fill="x")

        # ── Status bar (very bottom, packed before body so it stays fixed) ──
        self._status_var = tk.StringVar(value="Ready.")
        tk.Label(self, textvariable=self._status_var,
                 font=MONOSM, bg=BG2, fg=TEXTDIM,
                 anchor="w", pady=4, padx=12).pack(fill="x", side="bottom")

        # ── Horizontal scrollbar (above status bar, below body) ──────────
        _h_scroll = tk.Scrollbar(self, orient="horizontal", width=8,
                                 troughcolor=BG2, bg=BG3, bd=0,
                                 highlightthickness=0, relief="flat")
        _h_scroll.pack(fill="x", side="bottom")

        # ── Canvas wrapper for horizontal scrolling ───────────────────────
        self._h_canvas = tk.Canvas(self, bg=BG, highlightthickness=0,
                                   xscrollcommand=_h_scroll.set)
        self._h_canvas.pack(fill="both", expand=True)
        _h_scroll.config(command=self._h_canvas.xview)

        # ── Main body: sidebar + tabs (lives inside the canvas) ──────────
        main = tk.Frame(self._h_canvas, bg=BG)
        self._h_canvas_win = self._h_canvas.create_window(
            (0, 0), window=main, anchor="nw")

        # Keep canvas scroll region in sync with main frame size
        def _on_main_configure(event):
            self._h_canvas.configure(
                scrollregion=self._h_canvas.bbox("all"))
        main.bind("<Configure>", _on_main_configure)

        # Stretch inner frame height to always fill canvas height
        def _on_canvas_resize(event):
            self._h_canvas.itemconfig(
                self._h_canvas_win, height=event.height)
        self._h_canvas.bind("<Configure>", _on_canvas_resize)

        # Horizontal mouse-wheel scroll (Shift+scroll or trackpad swipe)
        def _h_wheel(event):
            # macOS two-finger swipe fires <MouseWheel> with delta on x axis;
            # Shift+wheel on any platform fires as a horizontal scroll.
            if hasattr(event, "delta") and event.delta:
                self._h_canvas.xview_scroll(
                    int(-event.delta / 30), "units")
            elif event.num == 6:   # Button-6 = scroll left (X11)
                self._h_canvas.xview_scroll(-1, "units")
            elif event.num == 7:   # Button-7 = scroll right (X11)
                self._h_canvas.xview_scroll(1, "units")
        self._h_canvas.bind("<Shift-MouseWheel>", _h_wheel)
        self._h_canvas.bind("<Button-6>",         _h_wheel)
        self._h_canvas.bind("<Button-7>",         _h_wheel)
        # Propagate to all children so scrolling works anywhere in the window
        self.bind_all("<Shift-MouseWheel>", _h_wheel)
        self.bind_all("<Button-6>",         _h_wheel)
        self.bind_all("<Button-7>",         _h_wheel)

        self._build_sidebar(main)

        content = tk.Frame(main, bg=BG)
        content.pack(side="left", fill="both", expand=True)

        self._apply_notebook_style()
        self._nb = ttk.Notebook(content)
        self._nb.pack(fill="both", expand=True)

        self._tab_profile = self._build_profile_tab()
        self._tab_live    = self._build_live_tab()
        self._tab_log     = self._build_log_tab()
        self._tab_stats   = self._build_stats_tab()
        self._tab_debug   = self._build_debug_tab()
        # NOTE: Stats + Debug are tab indices 3 + 4 (see _bg_poll_live, which
        # refreshes by index). New tabs go AFTER index 4 so those don't shift.
        self._tab_bridge  = self._build_bridge_tab()
        self._tab_coverage = self._build_coverage_tab()
        self._tab_source  = self._build_source_tab()
        self._tab_tree    = self._build_tree_tab()
        self._tab_perms   = self._build_permissions_tab()
        self._tab_howto   = self._build_howto_tab()

        self._nb.add(self._tab_profile, text="  Profile  ")
        self._nb.add(self._tab_live,    text="  Live Data  ")
        self._nb.add(self._tab_log,     text="  Program Log  ")
        self._nb.add(self._tab_stats,   text="  Stats  ")
        self._nb.add(self._tab_debug,   text="  Debug Log  ")
        self._nb.add(self._tab_bridge,  text="  This Bridge  ")
        self._nb.add(self._tab_coverage, text="  Log Coverage  ")
        self._nb.add(self._tab_source,  text="  Source Code  ")
        self._nb.add(self._tab_tree,    text="  File Tree  ")
        self._nb.add(self._tab_perms,   text="  Permissions  ")
        self._nb.add(self._tab_howto,   text="  How To Use  ")

        # When the user navigates to Source Code or File Tree, ensure the
        # currently-displayed data matches the active profile.
        self._nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    # ─────────────────────────────────────────────────────────────────────────
    # Screenshot bar
    # ─────────────────────────────────────────────────────────────────────────

    def _build_screenshot_bar(self):
        # Outer wrapper: canvas + horizontal scrollbar so the bar scrolls left/right
        _ss_outer = tk.Frame(self, bg=BG2)
        _ss_outer.pack(fill="x")

        _ss_hscroll = tk.Scrollbar(_ss_outer, orient="horizontal", width=6,
                                   troughcolor=BG2, bg=BG3, bd=0,
                                   highlightthickness=0, relief="flat")
        _ss_hscroll.pack(fill="x", side="bottom")

        self._ss_canvas = tk.Canvas(_ss_outer, bg=BG2, height=60,
                                    highlightthickness=0,
                                    xscrollcommand=_ss_hscroll.set)
        self._ss_canvas.pack(fill="x", expand=True)
        _ss_hscroll.config(command=self._ss_canvas.xview)

        bar = tk.Frame(self._ss_canvas, bg=BG2, pady=10, padx=16)
        _ss_win = self._ss_canvas.create_window((0, 0), window=bar, anchor="nw")

        def _ss_configure(event):
            self._ss_canvas.configure(scrollregion=self._ss_canvas.bbox("all"))
            # Stretch bar height to fill canvas
            self._ss_canvas.itemconfig(_ss_win, height=self._ss_canvas.winfo_height())
        bar.bind("<Configure>", _ss_configure)

        def _ss_wheel(event):
            if hasattr(event, "delta") and event.delta:
                self._ss_canvas.xview_scroll(int(-event.delta / 30), "units")
            elif event.num == 6:
                self._ss_canvas.xview_scroll(-1, "units")
            elif event.num == 7:
                self._ss_canvas.xview_scroll(1, "units")
        self._ss_canvas.bind("<MouseWheel>",       _ss_wheel)
        self._ss_canvas.bind("<Shift-MouseWheel>", _ss_wheel)
        self._ss_canvas.bind("<Button-6>",         _ss_wheel)
        self._ss_canvas.bind("<Button-7>",         _ss_wheel)
        bar.bind("<MouseWheel>",       _ss_wheel)
        bar.bind("<Shift-MouseWheel>", _ss_wheel)
        bar.bind("<Button-6>",         _ss_wheel)
        bar.bind("<Button-7>",         _ss_wheel)

        tk.Label(bar, text="SCREENSHOTS", font=(_UI_FACE, 10, "bold"),
                 bg=BG2, fg=TEXTDIM).pack(side="left", padx=(0, 16))

        # Start button
        self._btn_start = _btn(bar, "▶  Start", self._shot_start,
                               bg="#16a34a", fg="#ffffff", padx=14, pady=4)
        self._btn_start.pack(side="left", padx=(0, 6))

        # Pause button
        self._btn_pause = _btn(bar, "⏸  Pause", self._shot_pause,
                               bg="#d97706", fg="#ffffff", padx=14, pady=4)
        self._btn_pause.pack(side="left", padx=(0, 6))
        self._btn_pause.config(state="disabled")

        # Stop button
        self._btn_stop = _btn(bar, "■  Stop", self._shot_stop,
                              bg="#dc2626", fg="#ffffff", padx=14, pady=4)
        self._btn_stop.pack(side="left", padx=(0, 20))
        self._btn_stop.config(state="disabled")

        # Capture-rate slider. The value is an INTERVAL in seconds; the label
        # also shows the equivalent shots-per-second (the unit the AI reasons
        # in). Slider sub-range 0.1s–2.0s = 10–0.5 shots/sec; the full engine
        # range is 0.1–10 shots/sec (see the note below).
        tk.Label(bar, text="Rate:", font=LABEL,
                 bg=BG2, fg=TEXT).pack(side="left")
        tk.Label(bar, text="0.1s · 10/s", font=MONOSM,
                 bg=BG2, fg=TEXTDIM).pack(side="left", padx=(4, 2))

        self._slider = tk.Scale(
            bar, from_=0.1, to=2.0, resolution=0.05,
            orient="horizontal", variable=self._shot_interval,
            length=200, bg=BG2, fg=TEXT,
            troughcolor=BG3, highlightthickness=0,
            activebackground=ACCENT, sliderlength=18,
            showvalue=False, command=self._on_interval_change)
        self._slider.pack(side="left")

        tk.Label(bar, text="2.0s · 0.5/s", font=MONOSM,
                 bg=BG2, fg=TEXTDIM).pack(side="left", padx=(2, 8))

        self._interval_lbl = tk.Label(
            bar, text="1.00 s/shot · 1.0/s", font=LABELBOLD,
            bg=BG2, fg=ACCENT, width=20)
        self._interval_lbl.pack(side="left", padx=(0, 8))

        tk.Label(bar, text="(AI asks you the shots/sec — range 0.1–10/s)",
                 font=(_UI_FACE, 9), bg=BG2, fg=TEXTDIM
                 ).pack(side="left", padx=(0, 16))

        # Shot counter
        self._shot_counter_lbl = tk.Label(
            bar, text="Shots: 0", font=MONOSM,
            bg=BG2, fg=TEXTDIM)
        self._shot_counter_lbl.pack(side="left")

        # Clickable output folder path
        self._output_folder: str = ""
        self._folder_lbl = tk.Label(
            bar, text="📁 Output: (not started)",
            font=MONOSM, bg=BG2, fg=ACCENT,
            cursor="hand2")
        self._folder_lbl.pack(side="left", padx=(16, 0))
        self._folder_lbl.bind("<Button-1>", self._open_output_folder)

        # Clear all data button
        _btn(bar, "🗑 Clear All Data", self._clear_output_folder,
             bg="#7f1d1d", fg="#fca5a5", padx=10, pady=2
             ).pack(side="left", padx=(10, 0))

        # Status dot
        self._shot_status_lbl = tk.Label(
            bar, text="● Idle", font=LABEL,
            bg=BG2, fg=TEXTDIM)
        self._shot_status_lbl.pack(side="right")

        # Propagate wheel scroll from every child widget in the bar
        def _bind_children(widget):
            widget.bind("<MouseWheel>",       _ss_wheel)
            widget.bind("<Shift-MouseWheel>", _ss_wheel)
            widget.bind("<Button-6>",         _ss_wheel)
            widget.bind("<Button-7>",         _ss_wheel)
            for child in widget.winfo_children():
                _bind_children(child)
        bar.after(100, lambda: _bind_children(bar))

    def _on_interval_change(self, val=None):
        v = self._shot_interval.get()
        fps = round(1.0 / v, 2) if v > 0 else 0.0
        self._interval_lbl.config(text=f"{v:.2f} s/shot · {fps:g}/s")
        if self._bridge_online and REQUESTS_OK:
            threading.Thread(target=self._send_interval, args=(v,), daemon=True).start()

    def _send_interval(self, v: float):
        try:
            requests.put(f"{BRIDGE_URL}/capture/interval",
                         json={"interval": v}, timeout=2)
        except Exception:
            pass

    def _shot_start(self):
        if self._shot_running and not self._shot_paused:
            return
        self._shot_running = True
        self._shot_paused  = False
        self._btn_start.config(state="disabled")
        self._btn_pause.config(state="normal")
        self._btn_stop.config(state="normal")
        self._shot_status_lbl.config(text="● Watching…", fg=ORANGE)
        self._set_status("Auto-capture armed — will start shooting when your program launches.")
        # If bridge is already online, arm it immediately
        if self._bridge_online and REQUESTS_OK:
            threading.Thread(target=self._arm_capture, daemon=True).start()

    def _shot_pause(self):
        if not self._shot_running:
            return
        if self._shot_paused:
            self._shot_paused = False
            self._btn_pause.config(text="⏸  Pause")
            self._shot_status_lbl.config(text="● Watching…", fg=ORANGE)
            if self._bridge_online and REQUESTS_OK:
                threading.Thread(
                    target=lambda: requests.post(f"{BRIDGE_URL}/capture/start",
                                                 json={"interval": self._shot_interval.get(),
                                                       "force": True},
                                                 timeout=2),
                    daemon=True).start()
        else:
            self._shot_paused = True
            self._btn_pause.config(text="▶  Resume")
            self._shot_status_lbl.config(text="⏸ Paused", fg=ORANGE)
            if self._bridge_online and REQUESTS_OK:
                threading.Thread(
                    target=lambda: requests.post(f"{BRIDGE_URL}/capture/stop", timeout=2),
                    daemon=True).start()

    def _shot_stop(self):
        self._shot_running  = False
        self._shot_paused   = False
        self._shot_count    = 0
        self._btn_start.config(state="normal")
        self._btn_pause.config(state="disabled", text="⏸  Pause")
        self._btn_stop.config(state="disabled")
        self._shot_status_lbl.config(text="● Idle", fg=TEXTDIM)
        self._shot_counter_lbl.config(text="Shots: 0")
        self._set_status("Auto-capture stopped.")
        if self._bridge_online and REQUESTS_OK:
            threading.Thread(
                target=lambda: requests.post(f"{BRIDGE_URL}/capture/stop", timeout=2),
                daemon=True).start()

    # ─────────────────────────────────────────────────────────────────────────
    # Bridge bar
    # ─────────────────────────────────────────────────────────────────────────

    def _build_bridge_bar(self):
        bar = tk.Frame(self, bg=BG, pady=8, padx=16)
        bar.pack(fill="x")

        tk.Label(bar, text="⬡ AgentVision",
                 font=(_UI_FACE, 15, "bold"),
                 bg=BG, fg=ACCENT).pack(side="left")
        tk.Label(bar, text="Coding Agent Observation Platform",
                 font=LABEL, bg=BG, fg=TEXTDIM).pack(side="left", padx=10)

        # Right side
        br = tk.Frame(bar, bg=BG)
        br.pack(side="right")

        self._bridge_dot = tk.Label(br, text="●", font=(_UI_FACE, 14),
                                    bg=BG, fg=TEXTDIM)
        self._bridge_dot.pack(side="left")
        self._bridge_lbl = tk.Label(br, text=" Bridge offline",
                                    font=LABEL, bg=BG, fg=TEXTDIM)
        self._bridge_lbl.pack(side="left", padx=(0, 12))

        # Toggle: auto-start screenshots when bridge boots
        cap_cb = tk.Checkbutton(
            br, text="📸 Capture on start",
            variable=self._capture_on_bridge_start,
            bg=BG, fg=TEXTDIM, font=LABEL,
            activebackground=BG, activeforeground=TEXT,
            selectcolor=BG2, bd=0, highlightthickness=0,
            cursor="hand2")
        cap_cb.pack(side="left", padx=(0, 8))

        _btn(br, "🩺 Digest", self._show_digest,
             bg="#374151", fg="#e5e7eb", padx=10, pady=3
             ).pack(side="left", padx=(0, 6))
        _btn(br, "📉 Tokens", self._show_token_report,
             bg="#374151", fg="#e5e7eb", padx=10, pady=3
             ).pack(side="left", padx=(0, 6))
        _btn(br, "📡 Push Mode", self._show_push_mode,
             bg="#374151", fg="#e5e7eb", padx=10, pady=3
             ).pack(side="left", padx=(0, 6))
        _btn(br, "Start Bridge", self._start_bridge,
             bg="#1d4ed8", fg="#ffffff", padx=14, pady=3
             ).pack(side="left", padx=(0, 6))
        _btn(br, "Stop Bridge", self._stop_bridge,
             bg="#7f1d1d", fg="#ffffff", padx=10, pady=3
             ).pack(side="left")

    def _show_digest(self) -> None:
        """Fetch the bridge's AI-triage digest (GET /digest) and render it in a
        readable window: health, ranked attention, top errors, capture rate."""
        if not REQUESTS_OK:
            messagebox.showinfo("Digest", "The `requests` package isn't "
                                "available, so the GUI can't call the bridge.")
            return
        if not self._bridge_online:
            messagebox.showinfo("Digest", "Start the bridge first — the digest "
                                "is computed live from the running bridge.")
            return
        try:
            r = requests.get(f"{BRIDGE_URL}/digest", timeout=5)
            if r.status_code != 200:
                messagebox.showerror("Digest",
                                     f"HTTP {r.status_code}: {r.text[:300]}")
                return
            d = r.json()
        except Exception as e:
            messagebox.showerror("Digest", f"Failed to fetch digest: {e}")
            return
        self._render_digest_window(d)

    # ── Push Mode ────────────────────────────────────────────────────────────
    # Push Mode is the only part of AgentVision that speaks to the AI WITHOUT
    # being asked, so the human gets a window that shows exactly what it would
    # say next and a one-click kill switch. Nothing here is hidden.

    def _show_push_mode(self) -> None:
        try:
            import push_hooks
        except Exception as e:
            messagebox.showerror("Push Mode", f"Could not load push_hooks: {e}")
            return
        win = tk.Toplevel(self)
        win.title("AgentVision — Push Mode")
        win.configure(bg=BG)
        win.geometry("820x640")

        hdr = tk.Frame(win, bg=BG)
        hdr.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(hdr, text="Push Mode", font=HEADING, bg=BG, fg=TEXT).pack(side="left")
        state_lbl = tk.Label(hdr, text="", font=LABEL, bg=BG, fg=TEXTDIM)
        state_lbl.pack(side="left", padx=(12, 0))

        txt = tk.Text(win, bg=BG2, fg=TEXT, font=("Menlo", 11), wrap="word",
                      bd=0, padx=14, pady=12)
        sb = tk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)

        btns = tk.Frame(win, bg=BG)
        btns.pack(side="bottom", fill="x", padx=14, pady=10)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)

        def render():
            st = push_hooks.status()
            on = st["push_mode_enabled"] and st["installed"]
            state_lbl.configure(
                text=("● ON — injecting when there is news" if on else
                      "○ OFF — the AI is told nothing unprompted"),
                fg=("#4ade80" if on else TEXTDIM))
            L = []
            L.append("WHAT PUSH MODE DOES")
            L.append("-" * 70)
            L.append("Normally the AI has to ASK AgentVision for state, which means")
            L.append("it has to already suspect something is wrong. Push Mode injects")
            L.append("a few pre-digested lines at the moments that matter, so the AI")
            L.append("learns 'the change you just made crashed the program' without")
            L.append("spending a tool call.")
            L.append("")
            L.append("It is SILENT unless something changed, never repeats itself in a")
            L.append("session, and every message is byte-capped.")
            L.append("")
            L.append("STATUS")
            L.append("-" * 70)
            L.append(f"  Hooks installed ....... {st['installed']}")
            L.append(f"  Push Mode enabled ..... {st['push_mode_enabled']}"
                     f"   (source: {st['enable_source']})")
            L.append(f"  Settings file ......... {st['settings_file']}")
            L.append(f"  Hook script ........... {st['hook_script']}")
            L.append(f"  Stop-block backstop ... {st['stop_block_active']}"
                     f"   (off unless AGENTVISION_AMBIENT_STOP_BLOCK=1)")
            if st.get("settings_parse_error"):
                L.append(f"  !! settings parse error: {st['settings_parse_error']}")
            L.append("  Wired events:")
            for ev in st["installed_events"] or []:
                L.append(f"    - {ev['event']}"
                         + (f"  [{ev['matcher']}]" if ev.get("matcher") else ""))
            if not st["installed_events"]:
                L.append("    (none — press 'Install hooks')")
            L.append("")
            L.append("NEXT INJECTION PREVIEW (what the AI would be told right now)")
            L.append("-" * 70)
            prev = None
            if REQUESTS_OK and self._bridge_online:
                try:
                    r = requests.get(f"{BRIDGE_URL}/ambient",
                                     params={"session_id": "gui-preview",
                                             "event": "UserPromptSubmit",
                                             "force": "1"}, timeout=6)
                    prev = r.json() if r.status_code == 200 else None
                except Exception as e:
                    L.append(f"  (could not reach the bridge: {e})")
            elif not self._bridge_online:
                L.append("  (start the bridge to see a live preview)")
            else:
                L.append("  (the `requests` package is unavailable)")
            if prev:
                if prev.get("inject"):
                    L.append(f"  tier={prev['tier']}  bytes={prev['bytes']}"
                             f"  ~{prev['est_tokens']} est tokens"
                             f"  (cap {prev.get('cap_bytes')})")
                    L.append("")
                    for line in str(prev.get("text") or "").splitlines():
                        L.append("  | " + line)
                else:
                    L.append("  SILENT — nothing worth telling the AI right now.")
                    L.append(f"  reason: {prev.get('reason')}")
                L.append("")
                L.append(f"  (preview uses force=1, so it ignores the "
                         f"already-said suppression a real hook would apply)")
            L.append("")
            L.append("EVERY WAY TO TURN IT OFF")
            L.append("-" * 70)
            L.append("  1. The 'Disable' button below (instant, no restart needed)")
            L.append("  2. CLI:  agentvision push-mode off")
            L.append("  3. Env:  AGENTVISION_PUSH_MODE=0")
            L.append("  4. Fully remove the hooks: 'Uninstall hooks' below,")
            L.append("     or  agentvision uninstall-hooks")
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            txt.insert("1.0", "\n".join(L))
            txt.configure(state="disabled")

        def do_install():
            res = push_hooks.install(python_exe="python3")
            if res.get("ok"):
                messagebox.showinfo(
                    "Push Mode",
                    "Hooks installed.\n\nBackup: "
                    f"{res.get('backup') or '(no previous file)'}\n\n"
                    "*** RESTART CLAUDE CODE for them to take effect. ***")
            else:
                messagebox.showerror("Push Mode", str(res.get("error")))
            render()

        def do_uninstall():
            res = push_hooks.uninstall()
            if res.get("ok"):
                messagebox.showinfo("Push Mode",
                                    "Hooks removed (other hooks left intact).\n"
                                    f"Backup: {res.get('backup')}\n\n"
                                    "Restart Claude Code to stop running them.")
            else:
                messagebox.showerror("Push Mode", str(res.get("error")))
            render()

        def do_toggle():
            cur = push_hooks.get_enabled()["enabled"]
            push_hooks.set_enabled(not cur)
            render()

        _btn(btns, "Install hooks", do_install, bg="#1d4ed8", fg="#ffffff",
             padx=12, pady=4).pack(side="left", padx=(0, 6))
        _btn(btns, "Enable / Disable", do_toggle, bg="#374151", fg="#e5e7eb",
             padx=12, pady=4).pack(side="left", padx=(0, 6))
        _btn(btns, "Refresh preview", render, bg="#374151", fg="#e5e7eb",
             padx=12, pady=4).pack(side="left", padx=(0, 6))
        _btn(btns, "Uninstall hooks", do_uninstall, bg="#7f1d1d", fg="#ffffff",
             padx=12, pady=4).pack(side="right")
        render()

    def _show_token_report(self) -> None:
        """Fetch GET /token_report and render the token economics in plain
        English: what the visual-change engine did for free, what the AI actually
        paid for, and the measured comparison between a full frame, the JSON
        descriptor, and a changed-region crop."""
        if not REQUESTS_OK:
            messagebox.showinfo("Token Report", "The `requests` package isn't "
                                "available, so the GUI can't call the bridge.")
            return
        if not self._bridge_online:
            messagebox.showinfo("Token Report", "Start the bridge first — the "
                                "report is computed live from the running bridge.")
            return
        try:
            r = requests.get(f"{BRIDGE_URL}/token_report", timeout=8)
            if r.status_code != 200:
                messagebox.showerror("Token Report",
                                     f"HTTP {r.status_code}: {r.text[:300]}")
                return
            d = r.json()
        except Exception as e:
            messagebox.showerror("Token Report", f"Failed to fetch report: {e}")
            return
        self._render_token_window(d)

    def _render_token_window(self, d: dict) -> None:
        """Human-readable view of /token_report."""
        win = tk.Toplevel(self)
        win.title("AgentVision — Token Economics")
        win.configure(bg=BG)
        win.geometry("860x620")
        txt = tk.Text(win, bg=BG2, fg=TEXT, font=("Menlo", 11), wrap="word",
                      bd=0, padx=14, pady=12)
        sb = tk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)

        L = []
        L.append("WHY THIS MATTERS")
        L.append("-" * 72)
        L.append("Screenshotting, hashing and diffing run on THIS machine and are")
        L.append("effectively free. Every token the AI spends looking at raw pixels")
        L.append("is not. AgentVision does the expensive observing up front so the")
        L.append("AI can spend its budget on code instead.")
        L.append("")

        free = d.get("capture_side_free_work") or {}
        L.append("FREE WORK DONE ON THIS MACHINE")
        L.append("-" * 72)
        L.append(f"  Frames captured .............. {free.get('frames_captured')}")
        L.append(f"  Frames analyzed .............. {free.get('frames_analyzed')}")
        L.append(f"  Visually unchanged ........... {free.get('frames_visually_unchanged')}"
                 f"   (ratio {free.get('unchanged_ratio')})")
        L.append(f"  Frames that changed .......... {free.get('frames_changed')}")
        L.append(f"  Unique perceptual hashes ..... {free.get('unique_perceptual_hashes')}")
        L.append(f"  Duplicate ratio .............. {free.get('dedup_ratio')}")
        cost = free.get("analysis_cost") or {}
        L.append(f"  Analysis cost / frame ........ avg {cost.get('avg_ms_per_frame')} ms, "
                 f"max {cost.get('max_ms_per_frame')} ms")
        L.append(f"  Budget at the current rate ... {cost.get('budget_ms_at_current_rate')} ms/frame")
        L.append(f"  OCR scans on capture path .... {free.get('ocr_scans_on_capture_path')}")
        L.append("")

        paid = d.get("agent_side_paid_work") or {}
        L.append("WHAT THE AI ACTUALLY PAID FOR")
        L.append("-" * 72)
        for k, label in (("visual_changes_calls", "av_visual_changes calls"),
                         ("av_frame_json_calls", "av_frame_json calls"),
                         ("av_frame_region_calls", "av_frame_region calls"),
                         ("full_frames_pulled", "full frames pulled"),
                         ("distinct_frames_pulled_full", "distinct full frames")):
            L.append(f"  {label:.<30} {paid.get(k)}")
        L.append("")

        m = d.get("measured_comparison") or {}
        L.append("MEASURED ON A REAL FRAME FROM THIS SESSION")
        L.append("-" * 72)
        if m.get("available"):
            ff = m.get("full_frame") or {}
            fj = m.get("av_frame_json") or {}
            cr = m.get("changed_region_crop") or {}
            L.append(f"  Sample frame ................. #{m.get('sample_frame_seq')} "
                     f"{m.get('frame_size')}")
            L.append(f"  Full frame as an image ....... {ff.get('est_visual_tokens_if_sent_as_image')} est tokens")
            L.append(f"  av_frame_json descriptor ..... {fj.get('est_tokens')} est tokens"
                     f"   (no image bytes)")
            if cr.get("est_visual_tokens") is not None:
                L.append(f"  Changed-region crop .......... {cr.get('est_visual_tokens')} est tokens"
                         f"   bbox {cr.get('bbox')}")
            else:
                L.append(f"  Changed-region crop .......... n/a ({cr.get('reason')})")
            if m.get("descriptor_vs_full_frame_ratio") is not None:
                L.append(f"  Descriptor / full-frame ratio  {m.get('descriptor_vs_full_frame_ratio')}"
                         f"   (saves ~{m.get('descriptor_saves_est_tokens')} tokens/frame)")
        else:
            L.append(f"  Not available yet: {m.get('reason')}")
        L.append("")

        sess = d.get("session_estimate") or {}
        L.append("SESSION ESTIMATE")
        L.append("-" * 72)
        L.append(f"  If every frame were sent as an image: "
                 f"{sess.get('est_tokens_if_every_frame_were_sent_as_an_image')} est tokens")
        L.append(f"  Actually spent on frames ..........  "
                 f"{sess.get('est_tokens_actually_spent_on_frames')} est tokens")
        L.append(f"  Avoided ...........................  "
                 f"{sess.get('est_tokens_avoided')} est tokens")
        L.append(f"  Caveat: {sess.get('caveat')}")
        L.append("")
        L.append("HOW THESE NUMBERS ARE PRODUCED")
        L.append("-" * 72)
        for line in str(d.get("estimation_method") or "").split(". "):
            if line.strip():
                L.append("  " + line.strip().rstrip(".") + ".")
        L.append("")
        L.append("THE RULE THE AI IS TOLD TO FOLLOW")
        L.append("-" * 72)
        L.append("  " + str(d.get("the_rule") or ""))

        txt.insert("1.0", "\n".join(L))
        txt.configure(state="disabled")

    def _render_digest_window(self, d: dict) -> None:
        win = tk.Toplevel(self)
        win.title("AgentVision — AI Triage Digest")
        win.geometry("760x640")
        win.configure(bg=BG)

        health = d.get("health") or {}
        score = health.get("score")
        grade = health.get("grade", "")
        gcol = (GREEN if grade == "healthy" else YELLOW if grade == "degraded"
                else ORANGE if grade == "unhealthy" else RED)
        top = tk.Frame(win, bg=BG, padx=16, pady=12)
        top.pack(fill="x")
        tk.Label(top, text=f"{d.get('program','?')}", font=HEADING,
                 bg=BG, fg=TEXT).pack(side="left")
        tk.Label(top, text=f"health {score}/100 · {grade}", font=LABELBOLD,
                 bg=BG, fg=gcol).pack(side="right")

        cap = d.get("capture") or {}
        tk.Label(win,
                 text=(f"capture: {'ON' if cap.get('capturing') else 'idle'}  ·  "
                       f"{cap.get('shots_per_second','?')} shots/sec "
                       f"(interval {cap.get('interval_s','?')}s)  ·  "
                       f"frames {cap.get('frames_stored','?')}  ·  "
                       f"blank {cap.get('blank_frame_count',0)}"),
                 font=MONOSM, bg=BG, fg=TEXTDIM, anchor="w",
                 padx=16).pack(fill="x")
        tk.Label(win,
                 text="rate: " + (cap.get("rate_guidance") or
                                  "ask the user their preferred shots/sec."),
                 font=(_UI_FACE, 9), bg=BG, fg=TEXTDIM, anchor="w",
                 justify="left", wraplength=720, padx=16).pack(fill="x", pady=(0, 6))

        body = scrolledtext.ScrolledText(win, bg="#0a0a0a", fg=TEXT,
                                         font=MONOSM, relief="flat", wrap="word")
        body.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        body.tag_configure("h", foreground=ACCENT, font=LABELBOLD)
        body.tag_configure("err", foreground=RED)
        body.tag_configure("dim", foreground=TEXTDIM)

        def _section(title):
            body.insert("end", f"\n{title}\n", "h")

        _section("ATTENTION (work top-down)")
        for i, a in enumerate(d.get("attention") or [], 1):
            body.insert("end", f"  {i}. {a}\n")

        lf = d.get("latest_frame")
        if lf:
            _section("LATEST FRAME")
            body.insert("end", f"  seq {lf.get('sequence')}  "
                               f"conf {lf.get('confidence')}  "
                               f"running={lf.get('running')}\n", "dim")
            if lf.get("summary"):
                body.insert("end", f"  summary: {lf['summary']}\n")
            if lf.get("recommended_next"):
                body.insert("end", f"  next: {lf['recommended_next']}\n")
            if lf.get("tags"):
                body.insert("end", f"  tags: {', '.join(lf['tags'])}\n", "dim")

        es = d.get("error_stats") or {}
        _section("ERROR STATS")
        body.insert("end",
                    f"  total={es.get('total_failures',0)}  "
                    f"session={es.get('session_failures',0)}  "
                    f"distinct={es.get('distinct_fingerprints',0)}  "
                    f"new={es.get('new_this_session',0)}  "
                    f"trend={es.get('trend','?')}\n", "dim")

        te = d.get("top_errors") or []
        if te:
            _section("TOP ERRORS (deduped by fingerprint)")
            for e in te:
                body.insert("end", f"  {e.get('count')}×  ", "err")
                body.insert("end", f"{e.get('sample','')}\n")

        al = d.get("alignment") or {}
        if al:
            _section("IMAGE↔LOG ALIGNMENT")
            body.insert("end", f"  {json.dumps(al)}\n", "dim")

        _section("RAW JSON")
        body.insert("end", json.dumps(d, indent=2) + "\n", "dim")
        body.config(state="disabled")

        _btn(win, "Close", win.destroy, bg="#15803d", fg="#ffffff",
             padx=20, pady=4).pack(side="right", padx=16, pady=(0, 12))

    # ─────────────────────────────────────────────────────────────────────────
    # Sidebar
    # ─────────────────────────────────────────────────────────────────────────

    def _build_sidebar(self, parent):
        sidebar = tk.Frame(parent, bg=BG2, width=220)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        tk.Label(sidebar, text="CONNECTED PROGRAMS",
                 font=(_UI_FACE, 9, "bold"),
                 bg=BG2, fg=TEXTDIM, pady=8).pack(fill="x", padx=12)

        lf = tk.Frame(sidebar, bg=BG2)
        lf.pack(fill="both", expand=True, padx=6)
        _lb_scroll = tk.Scrollbar(lf, orient="vertical", width=6,
                                  troughcolor=BG2, bg=BG3, bd=0,
                                  highlightthickness=0, relief="flat")
        _lb_scroll.pack(side="right", fill="y")
        self._profile_listbox = tk.Listbox(
            lf, bg=BG2, fg=TEXT,
            selectbackground=ACCENT, selectforeground="#000000",
            relief="flat", borderwidth=0, font=LABEL, activestyle="none",
            yscrollcommand=_lb_scroll.set)
        self._profile_listbox.pack(fill="both", expand=True)
        _lb_scroll.config(command=self._profile_listbox.yview)
        self._profile_listbox.bind("<<ListboxSelect>>", self._on_profile_select)
        # Mouse-wheel scroll (macOS: Button-4/5; also <MouseWheel> for Windows/macOS Tk)
        def _lb_wheel(event):
            if event.num == 4 or event.delta > 0:
                self._profile_listbox.yview_scroll(-1, "units")
            else:
                self._profile_listbox.yview_scroll(1, "units")
        self._profile_listbox.bind("<MouseWheel>", _lb_wheel)
        self._profile_listbox.bind("<Button-4>",   _lb_wheel)
        self._profile_listbox.bind("<Button-5>",   _lb_wheel)

        sb = tk.Frame(sidebar, bg=BG2, pady=8)
        sb.pack(fill="x", padx=6)
        _btn(sb, "+ New", self._new_profile, bg="#1e3a5f", fg="#a8d4ff",
             padx=8).pack(side="left", fill="x", expand=True)
        _btn(sb, "Delete", self._delete_profile, bg="#3f1515", fg="#fca5a5",
             padx=8).pack(side="left", fill="x", expand=True, padx=(4, 0))

    def _apply_notebook_style(self):
        style = ttk.Style()
        style.theme_use("default")
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=BG3, foreground=TEXTDIM,
                        padding=[14, 6], font=LABEL)
        style.map("TNotebook.Tab",
                  background=[("selected", BG)],
                  foreground=[("selected", TEXT)])

    # ─────────────────────────────────────────────────────────────────────────
    # Profile tab
    # ─────────────────────────────────────────────────────────────────────────

    def _build_profile_tab(self) -> tk.Frame:
        outer = tk.Frame(self._nb, bg=BG)
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        frame = tk.Frame(canvas, bg=BG)
        win_id = canvas.create_window((0, 0), window=frame, anchor="nw")
        frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win_id, width=e.width))
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", lambda ev: canvas.yview_scroll(int(-1 * (ev.delta / 120)), "units")))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        hdr = tk.Frame(frame, bg=BG, pady=10, padx=20)
        hdr.pack(fill="x")
        self._profile_name_lbl = tk.Label(hdr, text="", font=HEADING,
                                          bg=BG, fg=TEXT)
        self._profile_name_lbl.pack(side="left")

        _btn(hdr, "▶  Set Active", self._set_active,
             bg="#15803d", fg="#ffffff", padx=14, pady=3
             ).pack(side="right")

        self._active_badge = tk.Label(hdr, text="", font=LABEL, bg=BG, fg=GREEN)
        self._active_badge.pack(side="right", padx=10)

        ttk.Separator(frame).pack(fill="x", padx=20)

        # ── Drop zone ─────────────────────────────────────────────────────
        # macOS Finder: select app/folder → Cmd+Opt+C to copy path,
        # then Cmd+V here. Or drag a .command/.py/.app file and the zone
        # will read the clipboard automatically on focus/hover.
        dz_outer = tk.Frame(frame, bg="#1a2535", padx=20, pady=(10))
        dz_outer.pack(fill="x", padx=20, pady=(10, 0))

        dz_top = tk.Frame(dz_outer, bg="#1a2535")
        dz_top.pack(fill="x")
        tk.Label(dz_top,
                 text="⬇  Drop Zone — paste or browse any app/script/folder",
                 font=(_UI_FACE, 12, "bold"),
                 bg="#1a2535", fg="#93c5fd").pack(side="left")
        _btn(dz_top, "Browse…", self._drop_zone_browse,
             bg="#1e40af", fg="#ffffff", padx=10, pady=2).pack(side="right")

        tk.Label(dz_outer,
                 text=("Explorer → right-click app or folder → Copy as path → "
                       "click the box below → Ctrl+V  |  or drag into the path box  |  or Browse…"
                       if platform_shim.IS_WINDOWS else
                       "Finder → select app or folder → Cmd+Opt+C (copy path) → "
                       "click the box below → Cmd+V  |  or drag into the path box  |  or Browse…"),
                 font=(_UI_FACE, 10), bg="#1a2535", fg="#6b7280",
                 anchor="w").pack(fill="x", pady=(2, 6))

        dz_entry_row = tk.Frame(dz_outer, bg="#1a2535")
        dz_entry_row.pack(fill="x")
        self._drop_var = tk.StringVar()
        dz_entry = tk.Entry(dz_entry_row, textvariable=self._drop_var,
                            font=MONOSM, bg="#0f172a", fg="#a8d4ff",
                            insertbackground="#a8d4ff", relief="flat", bd=6)
        dz_entry.pack(side="left", fill="x", expand=True)
        dz_entry.insert(0, "← paste or type path here, then press Enter")
        dz_entry.bind("<FocusIn>",  self._dz_focus_in)
        dz_entry.bind("<FocusOut>", self._dz_focus_out)
        dz_entry.bind("<Return>",   self._dz_commit)
        # On macOS, dragging a file onto a tk.Entry populates it with the
        # path when the OS sends it as a drop event on the widget.
        dz_entry.bind("<<Paste>>",  lambda _e: self.after(50, self._dz_commit))
        self._dz_entry = dz_entry
        _btn(dz_entry_row, "⟳ Auto-Fill", self._dz_commit,
             bg="#15803d", fg="#ffffff", padx=10, pady=2
             ).pack(side="left", padx=(6, 0))
        # Re-Install: re-run installer on the project_root field's current
        # value (idempotent), then verify the outbound link is live.
        _btn(dz_entry_row, "↻ Re-Install + Verify",
             self._reinstall_current_root,
             bg="#7c3aed", fg="#ffffff", padx=10, pady=2
             ).pack(side="left", padx=(6, 0))

        # Auto-detect hint (kept for when user sets root manually)
        hint = tk.Frame(frame, bg=BG, padx=16, pady=4)
        hint.pack(fill="x", padx=20, pady=(6, 0))
        tk.Label(hint, text="💡  Or set Project Root field below and click Auto-Detect.",
                 font=LABEL, bg=BG, fg=TEXTDIM).pack(side="left")
        _btn(hint, "⟳ Auto-Detect", self._auto_detect,
             bg="#1e40af", fg="#ffffff", padx=10, pady=2
             ).pack(side="right")

        # Form
        form = tk.Frame(frame, bg=BG, padx=24, pady=12)
        form.pack(fill="both", expand=True)

        fields = [
            ("display_name",       "Display Name",        False),
            ("project_root",       "Project Root",        True),
            ("log_file",           "Log File",            True),
            ("stats_folder",       "Stats Folder",        True),
            ("config_folder",      "Config Folder",       True),
            ("state_file",         "State File (JSON)",   True),
            ("python_exe",         "Python Executable",   True),
            ("test_dir",           "Test Directory",      True),
            ("process_name",       "Process Name",        False),
            ("notes",              "Notes",               False),
        ]

        self._field_vars: dict[str, tk.StringVar] = {}
        for key, label, has_browse in fields:
            row = tk.Frame(form, bg=BG)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=label, font=LABEL, bg=BG, fg=TEXTDIM,
                     width=20, anchor="e").pack(side="left", padx=(0, 10))
            var = tk.StringVar()
            self._field_vars[key] = var
            entry = tk.Entry(row, textvariable=var, font=MONOSM,
                             bg=BG3, fg=TEXT, insertbackground=TEXT,
                             relief="flat", bd=4)
            entry.pack(side="left", fill="x", expand=True)
            if has_browse:
                is_file = "file" in key.lower() or "exe" in key.lower()
                _btn(row, "Browse",
                     lambda k=key, f=is_file: self._browse(k, f),
                     bg="#374151", fg="#e5e7eb", padx=8
                     ).pack(side="left", padx=(4, 0))

        # ── Detected language / emitter ───────────────────────────────────
        # Shows the SAME language detection cli/installer use, and which
        # per-language emitter `agentvision attach` would install. Universal:
        # any language maps to an emitter (compiled langs → the tee bridge).
        ttk.Separator(form).pack(fill="x", pady=(10, 6))
        tk.Label(form, text="LANGUAGE / EMITTER (auto-detected)",
                 font=(_UI_FACE, 9, "bold"),
                 bg=BG, fg=TEXTDIM).pack(anchor="w", pady=(0, 4))
        lang_row = tk.Frame(form, bg=BG)
        lang_row.pack(fill="x", pady=(0, 2))
        self._lang_lbl = tk.Label(
            lang_row, text="set Project Root, then Auto-Detect",
            font=MONOSM, bg=BG, fg=TEXTDIM, anchor="w", justify="left",
            wraplength=680)
        self._lang_lbl.pack(side="left", fill="x", expand=True)
        _btn(lang_row, "↻ Re-detect", self._refresh_detected_language,
             bg="#374151", fg="#e5e7eb", padx=8).pack(side="right")

        # ── Screen / Crop section ─────────────────────────────────────────
        ttk.Separator(form).pack(fill="x", pady=(10, 6))
        tk.Label(form, text="SCREEN CAPTURE", font=(_UI_FACE, 9, "bold"),
                 bg=BG, fg=TEXTDIM).pack(anchor="w", pady=(0, 4))

        # Capture App row
        app_row = tk.Frame(form, bg=BG)
        app_row.pack(fill="x", pady=3)
        tk.Label(app_row, text="Capture App", font=LABEL, bg=BG, fg=TEXTDIM,
                 width=20, anchor="e").pack(side="left", padx=(0, 10))
        var_app = tk.StringVar()
        self._field_vars["capture_app"] = var_app
        tk.Entry(app_row, textvariable=var_app, font=MONOSM,
                 bg=BG3, fg=TEXT, insertbackground=TEXT,
                 relief="flat", bd=4).pack(side="left", fill="x", expand=True)
        _btn(app_row, "Browse…", self._browse_capture_app,
             bg="#374151", fg="#e5e7eb", padx=8, pady=2
             ).pack(side="left", padx=(6, 0))

        # Custom crop row
        crop_row = tk.Frame(form, bg=BG)
        crop_row.pack(fill="x", pady=3)
        tk.Label(crop_row, text="Custom Crop", font=LABEL, bg=BG, fg=TEXTDIM,
                 width=20, anchor="e").pack(side="left", padx=(0, 10))
        var_crop = tk.StringVar()
        self._field_vars["capture_crop"] = var_crop

        # Mutual exclusion: setting one clears the other
        _mutex = {"_lock": False}
        def _on_app_set(*_):
            if _mutex["_lock"]: return
            if var_app.get().strip():
                _mutex["_lock"] = True
                var_crop.set("")
                _mutex["_lock"] = False
        def _on_crop_set(*_):
            if _mutex["_lock"]: return
            if var_crop.get().strip():
                _mutex["_lock"] = True
                var_app.set("")
                _mutex["_lock"] = False
        var_app.trace_add("write", _on_app_set)
        var_crop.trace_add("write", _on_crop_set)
        tk.Entry(crop_row, textvariable=var_crop, font=MONOSM,
                 bg=BG3, fg=TEXT, insertbackground=TEXT,
                 relief="flat", bd=4, width=22).pack(side="left")
        tk.Label(crop_row, text="x,y,w,h  e.g. 100,50,1280,720",
                 font=MONOSM, bg=BG, fg=TEXTDIM).pack(side="left", padx=(6, 0))
        _btn(crop_row, "Pick Region…", self._open_crop_picker,
             bg="#1e40af", fg="#ffffff", padx=8, pady=2
             ).pack(side="left", padx=(10, 0))
        _btn(crop_row, "Use App Window", self._use_app_window,
             bg="#374151", fg="#e5e7eb", padx=8, pady=2
             ).pack(side="left", padx=(6, 0))

        btn_row = tk.Frame(form, bg=BG, pady=10)
        btn_row.pack(fill="x")
        _btn(btn_row, "Save Profile", self._save_profile,
             bg="#1d4ed8", fg="#ffffff", padx=16, pady=5
             ).pack(side="left")

        return outer

    def _refresh_detected_language(self) -> None:
        """Update the Profile tab's language/emitter line from the project_root
        field — the SAME detection cli/installer use, and the emitter that
        `agentvision attach` would install. Never raises (guarded imports)."""
        lbl = getattr(self, "_lang_lbl", None)
        if lbl is None:
            return
        try:
            root = (self._field_vars.get("project_root").get() or "").strip()
        except Exception:
            root = ""
        if not root or not Path(root).is_dir():
            lbl.config(text="set Project Root, then Auto-Detect", fg=TEXTDIM)
            return
        try:
            from connectors import log_sources as _ls
            from emitters import build_emitter as _be
            lang = _ls.detect_language(root) or "unknown"
            spec = _be(lang, str(Path(root) / "agentvision"), root)
            kind_desc = {
                "autoload":     "in-process autoload (zero config, no env var)",
                "env_autoload": "in-process via one auto-set env var",
                "config":       "drop-in logger config routed to the sink",
                "tee":          "universal tee bridge (stdout/stderr → normalized)",
            }.get(spec.kind, spec.kind)
            lbl.config(
                text=f"language: {spec.language}     emitter kind: {spec.kind}\n"
                     f"→ {kind_desc}",
                fg=GREEN if spec.language != "unknown" else ORANGE)
        except Exception as e:
            lbl.config(text=f"detection unavailable: {e}", fg=TEXTDIM)

    def _auto_detect(self):
        root = self._field_vars.get("project_root", tk.StringVar()).get().strip()
        if not root or not Path(root).is_dir():
            messagebox.showwarning("Auto-Detect",
                                   "Set a valid Project Root folder first.")
            return
        self._refresh_detected_language()
        found = _autodetect_profile(root)
        filled = []
        for key, val in found.items():
            if key in self._field_vars and val:
                current = self._field_vars[key].get()
                if not current:  # only fill blank fields
                    self._field_vars[key].set(val)
                    filled.append(key)
        if filled:
            self._set_status(f"Auto-detected: {', '.join(filled)}")
            # Persist so detected fields survive a restart.
            self._save_profile()
        else:
            self._set_status("Auto-detect: nothing new found (fields already filled or not detected).")

    # ── Drop-zone handlers ────────────────────────────────────────────────
    def _dz_focus_in(self, _evt=None):
        """Clear placeholder text on focus."""
        cur = self._drop_var.get()
        if cur.startswith("←"):
            self._dz_entry.delete(0, "end")
            self._dz_entry.config(fg="#a8d4ff")

    def _dz_focus_out(self, _evt=None):
        """Restore placeholder if empty."""
        if not self._drop_var.get().strip():
            self._dz_entry.config(fg="#6b7280")
            self._dz_entry.insert(0, "← paste or type path here, then press Enter")

    def _drop_zone_browse(self):
        """Browse for app bundle, .command file, or folder."""
        path = filedialog.askdirectory(title="Select project folder")
        if not path:
            # User might have a .command / .py file — offer file picker too
            path = filedialog.askopenfilename(
                title="Or select a script / launcher file",
                filetypes=[("Scripts & apps", "*.command *.py *.sh"),
                           ("All files", "*.*")])
        if path:
            self._drop_var.set(path)
            self._dz_entry.config(fg="#a8d4ff")
            self._dz_commit()

    def _dz_commit(self, _evt=None):
        """Resolve whatever is in the drop-zone entry to a project root,
        run the FULL INSTALL (creates all required folders/files inside
        the bridged project), then auto-detect and populate all fields.

        This is the moment the bridged program goes from 'unknown' to
        'fully diagnosable' — installer.install_into_project() drops in
        log/, .agentvision/, state.json, schema.json, sitecustomize.py,
        config/, test/, etc. Anything missing is created; anything
        present is left alone."""
        raw = self._drop_var.get().strip().strip("'\"")
        if not raw or raw.startswith("←"):
            return
        p = Path(raw).expanduser()
        # If they dropped a file (script, .command, .app, .py), use its
        # parent directory as the project root. For .app bundles, go up
        # one more level (out of the bundle).
        if p.is_file():
            root = p.parent
        elif p.suffix == ".app" and p.is_dir():
            root = p.parent
        elif p.is_dir():
            root = p
        else:
            self._set_status(f"Drop zone: path not found — {p}")
            return

        root_str = str(root)

        # Step 1: Confirm install (this WRITES TO the bridged project).
        from python_backend.installer import install_into_project
        proceed = messagebox.askyesno(
            "Install AgentVision into project?",
            f"AgentVision will create the diagnostic infrastructure inside:\n\n"
            f"  {root_str}\n\n"
            f"This adds (only if missing):\n"
            f"  • log/         — log.txt, actions.jsonl, stats/, crashes/\n"
            f"  • config/      — config dir\n"
            f"  • test/        — test dir\n"
            f"  • .agentvision/ — state.json, schema.json, marker, README\n"
            f"  • agentvision/ — sink files + a per-language EMITTER matched to\n"
            f"                   the detected language (Python autoload / Node /\n"
            f"                   Ruby / Java / .NET config / tee for compiled)\n\n"
            f"Existing files are NEVER overwritten. Proceed?")
        if not proceed:
            self._set_status("Drop zone install cancelled.")
            return

        # Step 2: Run the install — creates everything that's missing.
        try:
            profile_name = _safe_profile_name(root.name)
            report = install_into_project(root_str,
                                          profile_name=profile_name,
                                          install_python_hook=True)
        except Exception as e:
            messagebox.showerror("Install failed", str(e))
            return

        # Step 3: Set project_root, then run auto-detect on the NOW-populated
        # tree (so the detector finds everything we just created).
        self._field_vars["project_root"].set(root_str)
        found = _autodetect_profile(root_str)

        # Step 4: Layer install report's overrides on top — these are paths
        # we KNOW exist because the installer just created them, so they're
        # better defaults than whatever the heuristic detector guessed.
        for key, val in report.profile_overrides.items():
            found[key] = val

        # process_name fallback — installer doesn't set this; auto-detect
        # already handles .command launchers, but keep this as belt+braces.
        if not found.get("process_name"):
            for name in ["proxy.py", "main.py", "app.py", "run.py",
                         "bot.py", "start.py", "__main__.py"]:
                if (root / name).exists():
                    found["process_name"] = name
                    break

        # Step 5: Push every detected/created field into the form.
        filled = []
        for key, val in found.items():
            if key in self._field_vars:
                self._field_vars[key].set(val)
                filled.append(key)

        # Step 6: Persist the profile so it survives app restart.
        # If the profile name already exists update it; otherwise create new.
        data = {key: var.get() for key, var in self._field_vars.items()}
        data["name"] = profile_name
        if not data.get("display_name"):
            data["display_name"] = root.name
        self._profiles[profile_name] = ProgramProfile.from_dict(data)
        self._editing_name = profile_name
        self._save_profiles_or_warn()
        self._refresh_profile_list()
        # Surface the detected language + emitter for the freshly attached root.
        self._refresh_detected_language()

        # Step 7: Verify the outbound link — the install only matters if
        # the bridged program actually emits data back. Spawn a tiny
        # Python in the project and confirm av.bootstrap.* lands.
        verify = self._verify_outbound_link(root_str)

        # Step 8: Show the install report so the user knows EXACTLY what
        # was created vs already existed. Transparency over magic.
        self._show_install_report(report, len(filled), verify=verify)

    def _verify_outbound_link(self, project_root: str) -> dict:
        """POST /install/verify and return the response dict.
        Never raises — returns {'verified': False, 'error': ...} on failure
        so the caller can render a degraded result."""
        try:
            r = requests.post(f"{BRIDGE_URL}/install/verify",
                              json={"project_root": project_root, "timeout": 4.0},
                              timeout=8)
            if r.status_code != 200:
                return {"verified": False,
                        "error": f"HTTP {r.status_code}: {r.text[:200]}"}
            return r.json()
        except Exception as e:
            return {"verified": False, "error": f"verify failed: {e}"}

    def _reinstall_current_root(self):
        """Re-run install + verify on whatever's in the project_root field.
        Used when the user already attached and just wants to refresh /
        re-prove the outbound link."""
        root_str = (self._field_vars.get("project_root").get()
                    if "project_root" in self._field_vars else "").strip()
        if not root_str:
            messagebox.showwarning(
                "No project root set",
                "Set the Project Root field first, or use the Drop Zone "
                "to attach a new project.")
            return
        root = Path(root_str).expanduser()
        if not root.exists() or not root.is_dir():
            messagebox.showerror(
                "Project root missing",
                f"Path doesn't exist or isn't a directory:\n{root}")
            return
        try:
            r = requests.post(f"{BRIDGE_URL}/install",
                              json={"project_root": str(root),
                                    "profile_name": _safe_profile_name(root.name),
                                    "install_python_hook": True},
                              timeout=15)
            if r.status_code != 200:
                messagebox.showerror("Install failed",
                                     f"HTTP {r.status_code}: {r.text[:300]}")
                return
            payload = r.json()
        except Exception as e:
            messagebox.showerror("Install failed", str(e))
            return
        verify = self._verify_outbound_link(str(root))

        # Persist profile so re-install also saves any current field edits.
        profile_name = _safe_profile_name(root.name)
        data = {key: var.get() for key, var in self._field_vars.items()}
        data["name"] = profile_name
        if not data.get("display_name"):
            data["display_name"] = root.name
        self._profiles[profile_name] = ProgramProfile.from_dict(data)
        self._editing_name = profile_name
        self._save_profiles_or_warn()
        self._refresh_profile_list()

        # Build a lightweight report-shim so _show_install_report works.
        class _R:
            project_root = root
            created   = payload.get("created", [])
            existed   = payload.get("existed", [])
            skipped   = payload.get("skipped", [])
            errors    = [(e["path"], e["error"])
                         for e in payload.get("errors", [])]
            def summary(self):
                return payload.get("summary", "")
        self._show_install_report(_R(), 0, verify=verify)

    def _show_install_report(self, report, fields_filled: int,
                             verify: dict | None = None) -> None:
        """Display the install report in a scrollable dialog so the user
        sees every path that was created/touched. If `verify` is provided,
        also render the outbound-link verification banner — green if the
        bridged program emitted an av.bootstrap.* event, red otherwise."""
        win = tk.Toplevel(self)
        win.title("AgentVision Install Report")
        win.geometry("760x560")
        win.configure(bg=BG)

        tk.Label(win, text=f"Installed into {report.project_root}",
                 font=HEADING, bg=BG, fg=TEXT, anchor="w"
                 ).pack(fill="x", padx=16, pady=(12, 4))
        tk.Label(win,
                 text=(f"{len(report.created)} created   ·   "
                       f"{len(report.existed)} already present   ·   "
                       f"{len(report.skipped)} skipped   ·   "
                       f"{len(report.errors)} errors   ·   "
                       f"{fields_filled} profile fields filled"),
                 font=LABEL, bg=BG, fg=TEXTDIM, anchor="w"
                 ).pack(fill="x", padx=16, pady=(0, 8))

        # ── Outbound-link verification banner ───────────────────────────
        if verify is not None:
            ok = bool(verify.get("verified"))
            seen = int(verify.get("events_seen", 0) or 0)
            if ok:
                bar_bg, bar_fg = "#15803d", "#ffffff"
                bar_text = (f"📞  Outbound link VERIFIED — "
                            f"{seen} av.bootstrap.* event"
                            f"{'s' if seen != 1 else ''} received. "
                            f"The bridged program is now actively "
                            f"emitting to AgentVision.")
            else:
                bar_bg, bar_fg = "#991b1b", "#ffffff"
                err = verify.get("error") or verify.get("reason") or \
                      "no av.bootstrap.* events received"
                bar_text = (f"⚠  Outbound link NOT verified — {err}. "
                            f"The install dropped files but the program "
                            f"isn't emitting yet. Restart the program "
                            f"or re-run with PYTHONPATH including the "
                            f"project root.")
            tk.Label(win, text=bar_text, font=LABEL, bg=bar_bg, fg=bar_fg,
                     anchor="w", justify="left", wraplength=720,
                     padx=12, pady=8
                     ).pack(fill="x", padx=16, pady=(0, 8))

        body = scrolledtext.ScrolledText(win, bg=BG3, fg=TEXT, font=MONOSM,
                                         relief="flat", wrap="none")
        body.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        body.insert("end", report.summary())
        if verify is not None:
            body.insert("end", "\n\n──── Verify ────\n")
            for k, v in verify.items():
                if k == "last_event" and isinstance(v, dict):
                    import json as _json
                    body.insert("end", f"  {k}:\n"
                                       f"    {_json.dumps(v, indent=2)}\n")
                else:
                    body.insert("end", f"  {k}: {v}\n")
        body.config(state="disabled")

        btns = tk.Frame(win, bg=BG)
        btns.pack(fill="x", padx=16, pady=(0, 12))
        _btn(btns, "OK", win.destroy, bg="#15803d", fg="#ffffff",
             padx=20, pady=4).pack(side="right")
        self._set_status(
            f"Installed into {report.project_root.name}: "
            f"{len(report.created)} new, {len(report.existed)} existed, "
            f"{len(report.errors)} errors")

    def _open_crop_picker(self):
        """
        Annotation-style crop picker.
        Shows a fullscreen screenshot, user drags a yellow rectangle over it.
        No dark overlay, no artifacts, no ghost rects. Temp file deleted after.
        """
        import tempfile, os
        from PIL import Image, ImageTk

        # Hide GUI, wait for it to vanish, then screenshot
        self.withdraw()
        self.update_idletasks()
        import time; time.sleep(0.3)

        tmp_path = tempfile.mktemp(suffix=".png")
        try:
            platform_shim.capture_frame(tmp_path)   # full screen (mac/win/linux)
        except Exception as e:
            self.deiconify()
            messagebox.showerror("Pick Region", f"Screen capture failed:\n{e}")
            return

        img_orig = Image.open(tmp_path)
        SW, SH = img_orig.size          # real pixels (2× on Retina)

        WW = self.winfo_screenwidth()   # logical points
        WH = self.winfo_screenheight()
        scale_x = SW / WW               # logical → real pixel multiplier
        scale_y = SH / WH

        # Fullscreen borderless window — appears instead of the GUI
        picker = tk.Toplevel(self)
        picker.overrideredirect(True)
        picker.geometry(f"{WW}x{WH}+0+0")
        picker.attributes("-topmost", True)
        picker.lift()

        # Display screenshot at logical resolution
        img_display = img_orig.resize((WW, WH), Image.LANCZOS)
        tk_img = ImageTk.PhotoImage(img_display)

        canvas = tk.Canvas(picker, cursor="crosshair",
                           width=WW, height=WH,
                           highlightthickness=0, bd=0,
                           bg="black")
        canvas.pack(fill="both", expand=True)
        canvas.create_image(0, 0, anchor="nw", image=tk_img)
        canvas._tk_img = tk_img  # prevent GC

        # Thin instruction bar at very top
        canvas.create_rectangle(0, 0, WW, 28, fill="#111111", outline="")
        canvas.create_text(WW // 2, 14,
                           text="Drag to select crop region   •   ESC to cancel",
                           fill="white", font=(_UI_FACE, 12, "bold"))

        picker.update_idletasks()
        picker.focus_force()
        # On Windows an overrideredirect (WS_POPUP) window often does not get
        # keyboard focus, so bind Escape on the canvas too and give it focus.
        canvas.focus_set()

        SEL = "sel"       # single tag for all selection items
        state: dict = {"x0": None, "y0": None}

        def on_press(e):
            state["x0"], state["y0"] = e.x, e.y
            canvas.delete(SEL)

        def on_drag(e):
            if state["x0"] is None:
                return
            canvas.delete(SEL)
            x0, y0, x1, y1 = state["x0"], state["y0"], e.x, e.y
            # Selection rectangle — Tkinter doesn't support alpha fills, use stipple
            canvas.create_rectangle(x0, y0, x1, y1,
                                     outline="#facc15", width=2,
                                     fill="#facc15", stipple="gray25", tags=SEL)
            # Size readout below bottom-right corner
            rw = int(abs(x1 - x0) * scale_x)
            rh = int(abs(y1 - y0) * scale_y)
            lx, ly = min(x0, x1), max(y0, y1)
            canvas.create_text(lx + 4, ly + 4, anchor="nw",
                                text=f"{rw} × {rh} px",
                                fill="#facc15",
                                font=(_UI_FACE, 11, "bold"),
                                tags=SEL)

        def on_release(e):
            if state["x0"] is None:
                return
            x0r = int(min(state["x0"], e.x) * scale_x)
            y0r = int(min(state["y0"], e.y) * scale_y)
            wr  = int(abs(e.x - state["x0"]) * scale_x)
            hr  = int(abs(e.y - state["y0"]) * scale_y)
            _close()
            if wr > 20 and hr > 20:
                crop_str = f"{x0r},{y0r},{wr},{hr}"
                self._field_vars["capture_crop"].set(crop_str)
                self._set_status(f"Custom crop: {x0r},{y0r}  {wr}×{hr}px")

        def _close():
            picker.destroy()
            try:
                os.unlink(tmp_path)   # delete temp screenshot
            except Exception:
                pass
            self.deiconify()

        canvas.bind("<ButtonPress-1>",   on_press)
        canvas.bind("<B1-Motion>",       on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        picker.bind("<Escape>",          lambda _: _close())
        canvas.bind("<Escape>",          lambda _: _close())

    def _browse_capture_app(self):
        """Pick the app/window to capture.

        macOS: AppleScript's native `choose application`.
        Windows: a small chooser listing visible top-level window titles
        (Windows has no AppleScript equivalent). The chosen title is stored as
        capture_app; find_window() matches it against window titles + process
        names when capturing.
        """
        if platform_shim.IS_MAC:
            script = '''
            tell application "Finder"
                activate
                set chosen to choose application with prompt "Select the app to capture:"
                return POSIX path of (chosen as alias)
            end tell
            '''
            try:
                r = subprocess.run(["osascript", "-e", script],
                                   capture_output=True, text=True, timeout=30)
                if r.returncode != 0 or not r.stdout.strip():
                    return
                path = r.stdout.strip()
            except Exception:
                return
            macos_dir = Path(path.rstrip("/")) / "Contents" / "MacOS"
            proc_name = Path(path.rstrip("/")).stem
            if macos_dir.is_dir():
                exes = [f.name for f in macos_dir.iterdir()
                        if f.is_file() and not f.name.startswith(".")]
                if exes:
                    proc_name = exes[0]
            self._field_vars["capture_app"].set(proc_name)
            return

        # Windows / Linux — window-title chooser.
        titles = platform_shim.list_window_titles()
        if not titles:
            messagebox.showinfo(
                "Select App",
                "No visible windows found. Make sure the target program is "
                "running and visible, then type its window title (or process "
                "name, e.g. 'SharpEmu') into the Capture App field.")
            return

        picker = tk.Toplevel(self)
        picker.title("Select window to capture")
        picker.configure(bg=BG)
        picker.geometry("520x420")
        picker.transient(self)
        tk.Label(picker, text="Pick the window AgentVision should capture:",
                 bg=BG, fg=TEXT, font=LABEL).pack(anchor="w", padx=12, pady=(12, 6))
        lb = tk.Listbox(picker, bg=BG2, fg=TEXT, font=MONOSM,
                        selectbackground=ACCENT, activestyle="none",
                        highlightthickness=0, bd=0)
        for t in titles:
            lb.insert("end", t)
        lb.pack(fill="both", expand=True, padx=12, pady=6)

        def _choose(_evt=None):
            sel = lb.curselection()
            if sel:
                picked = lb.get(sel[0])
                # Prefer the stable process name (e.g. notepad.exe) over the
                # volatile window title (which may embed the open file/FPS/etc
                # and stop matching after it changes). Fall back to the title.
                store = picked
                try:
                    w = platform_shim.find_window(picked)
                    if w and w.get("owner"):
                        store = w["owner"]
                except Exception:
                    pass
                self._field_vars["capture_app"].set(store)
                self._set_status(f"Capture window: {picked}  →  {store}")
            picker.destroy()

        lb.bind("<Double-Button-1>", _choose)
        btns = tk.Frame(picker, bg=BG)
        btns.pack(fill="x", padx=12, pady=(0, 12))
        tk.Button(btns, text="Use selected", command=_choose,
                  bg="#15803d", fg="#ffffff").pack(side="right")
        tk.Button(btns, text="Cancel", command=picker.destroy,
                  bg="#374151", fg="#e5e7eb").pack(side="right", padx=(0, 8))

    def _use_app_window(self):
        """Resolve the capture_app window bounds and fill the crop field.
        Cross-platform via platform_shim (Quartz on macOS, win32gui on Windows)."""
        app_name = self._field_vars.get("capture_app", tk.StringVar()).get().strip()
        if not app_name:
            messagebox.showwarning("Use App Window",
                                   "Set a Capture App name first (e.g. SharpEmu).")
            return
        bounds = platform_shim.get_window_bounds(app_name)
        if bounds:
            crop_str = ",".join(str(int(v)) for v in bounds)
            self._field_vars["capture_crop"].set(crop_str)
            self._set_status(f"Window bounds for '{app_name}': {crop_str}")
        else:
            messagebox.showerror("Use App Window",
                                 f"Could not find a window matching '{app_name}'.\n"
                                 f"Make sure the app is running and visible.")

    # ─────────────────────────────────────────────────────────────────────────
    # Live data tab
    # ─────────────────────────────────────────────────────────────────────────

    def _build_live_tab(self) -> tk.Frame:
        frame = tk.Frame(self._nb, bg=BG, padx=20, pady=16)

        cards = tk.Frame(frame, bg=BG)
        cards.pack(fill="x", pady=(0, 14))

        self._card_running = self._make_card(cards, "Program",  "—",    TEXTDIM)
        self._card_cpu     = self._make_card(cards, "CPU",      "—%",   TEXTDIM)
        self._card_ram     = self._make_card(cards, "RAM",      "—GB",  TEXTDIM)
        self._card_frames  = self._make_card(cards, "Frames",   "0",    TEXTDIM)
        self._card_shots   = self._make_card(cards, "Shots",    "0",    TEXTDIM)

        ttk.Separator(frame).pack(fill="x", pady=(0, 10))

        eval_row = tk.Frame(frame, bg=BG)
        eval_row.pack(fill="x", pady=(0, 10))
        tk.Label(eval_row, text="Agent Confidence", font=LABEL,
                 bg=BG, fg=TEXTDIM).pack(side="left")
        self._conf_var = tk.DoubleVar(value=0)
        self._conf_bar = ttk.Progressbar(eval_row, variable=self._conf_var,
                                          maximum=100, length=260, mode="determinate")
        self._conf_bar.pack(side="left", padx=10)
        self._conf_lbl = tk.Label(eval_row, text="0%", font=MONO, bg=BG, fg=TEXT)
        self._conf_lbl.pack(side="left")

        action_row = tk.Frame(frame, bg=BG)
        action_row.pack(fill="x", pady=(0, 10))
        tk.Label(action_row, text="Next Action", font=LABEL,
                 bg=BG, fg=TEXTDIM).pack(side="left")
        self._next_action_lbl = tk.Label(action_row, text="—",
                                          font=LABEL, bg=BG, fg=TEXT)
        self._next_action_lbl.pack(side="left", padx=10)

        ttk.Separator(frame).pack(fill="x", pady=(0, 10))

        tk.Label(frame, text="Latest Snapshot Frame (JSON)",
                 font=HEADING, bg=BG, fg=TEXT).pack(anchor="w")
        self._frame_viewer = scrolledtext.ScrolledText(
            frame, bg=BG3, fg=TEXT, font=MONOSM,
            relief="flat", state="disabled", height=16)
        self._frame_viewer.pack(fill="both", expand=True, pady=(6, 0))

        return frame

    def _make_card(self, parent, label, value, color) -> tuple:
        f = tk.Frame(parent, bg=BG3, padx=16, pady=10)
        f.pack(side="left", padx=(0, 10), fill="y")
        tk.Label(f, text=label, font=MONOSM, bg=BG3, fg=TEXTDIM).pack()
        lbl = tk.Label(f, text=value,
                       font=(_UI_FACE, 17, "bold"), bg=BG3, fg=color)
        lbl.pack()
        return f, lbl

    # ─────────────────────────────────────────────────────────────────────────
    # Log tab — live tail
    # ─────────────────────────────────────────────────────────────────────────

    def _build_log_tab(self) -> tk.Frame:
        frame = tk.Frame(self._nb, bg=BG, padx=16, pady=10)

        # ── Shared header ────────────────────────────────────────────────
        hdr = tk.Frame(frame, bg=BG)
        hdr.pack(fill="x", pady=(0, 6))
        tk.Label(hdr, text="Program Log", font=HEADING,
                 bg=BG, fg=TEXT).pack(side="left")
        self._log_live_dot = tk.Label(hdr, text="● Live", font=MONOSM,
                                      bg=BG, fg=GREEN)
        self._log_live_dot.pack(side="left", padx=10)
        _btn(hdr, "↻ Refresh Now", self._refresh_log,
             bg="#374151", fg="#e5e7eb", padx=8
             ).pack(side="right")
        self._log_source_lbl = tk.Label(hdr, text="", font=MONOSM,
                                         bg=BG, fg=TEXTDIM)
        self._log_source_lbl.pack(side="right", padx=8)

        # ── Freshness / wrong-file banner ────────────────────────────────
        # The "● Live" dot above used to stay green over a file whose last write
        # was 23 HOURS old, because it reported whether the BRIDGE was live, not
        # whether the LOG was. That is how an entire debugging session got spent
        # reading a dead file: the emulator's own sink wrote to a different
        # directory and every surface here said everything was fine. This banner
        # exists to make "you are looking at the wrong file" impossible to miss.
        self._log_warn = tk.Label(frame, text="", font=MONOSM, bg="#7f1d1d",
                                  fg="#fee2e2", anchor="w", justify="left",
                                  padx=8, pady=4, wraplength=1000)
        self._log_warn_visible = False

        # ── Split pane: 3 resizable panels stacked vertically ───────────
        pane = tk.PanedWindow(frame, orient="vertical", bg=BG,
                              sashwidth=6, sashrelief="flat",
                              sashpad=2)
        pane.pack(fill="both", expand=True)
        # Kept so the stale-log banner can be inserted ABOVE the panes by
        # reference rather than by child index, which shifts as the tab changes.
        self._log_pane = pane

        # Top pane — bot text log (log.txt)
        top_frame = tk.Frame(pane, bg=BG)
        tk.Label(top_frame, text="Bot Log  (log.txt)",
                 font=MONOSM, bg=BG, fg=TEXTDIM, anchor="w"
                 ).pack(fill="x", padx=4, pady=(2, 2))
        self._log_viewer = scrolledtext.ScrolledText(
            top_frame, bg=BG3, fg=TEXT, font=MONOSM,
            relief="flat", state="disabled")
        self._log_viewer.pack(fill="both", expand=True)
        pane.add(top_frame, minsize=80, stretch="always")

        # Mid pane — structured action / keypress log (actions.jsonl)
        mid_frame = tk.Frame(pane, bg=BG)
        tk.Label(mid_frame, text="Action Log  (actions.jsonl) — keypresses, moves, events",
                 font=MONOSM, bg=BG, fg=TEXTDIM, anchor="w"
                 ).pack(fill="x", padx=4, pady=(2, 2))
        self._act_viewer = scrolledtext.ScrolledText(
            mid_frame, bg="#0d1117", fg=TEXT, font=MONOSM,
            relief="flat", state="disabled")
        self._act_viewer.pack(fill="both", expand=True)
        pane.add(mid_frame, minsize=80, stretch="always")

        # Bottom pane — raw program stdout/stderr (raw_output.txt)
        raw_frame = tk.Frame(pane, bg=BG)
        tk.Label(raw_frame, text="Raw Program Output  (stdout/stderr → log/raw_output.txt)",
                 font=MONOSM, bg=BG, fg=TEXTDIM, anchor="w"
                 ).pack(fill="x", padx=4, pady=(2, 2))
        self._raw_viewer = scrolledtext.ScrolledText(
            raw_frame, bg="#0a0a0a", fg="#e0e0e0", font=MONOSM,
            relief="flat", state="disabled")
        self._raw_viewer.pack(fill="both", expand=True)
        pane.add(raw_frame, minsize=80, stretch="always")

        # Place sashes at equal thirds once — only on first render.
        # After that the user can drag sashes freely.
        _sashes_set = [False]
        def _init_sashes(_pane=pane):
            if _sashes_set[0]:
                return
            h = _pane.winfo_height()
            if h < 10:
                frame.after(100, _init_sashes)
                return
            third = h // 3
            _pane.sash_place(0, 0, third)
            _pane.sash_place(1, 0, third * 2)
            _sashes_set[0] = True
        frame.after(150, _init_sashes)

        # Tags for bot log viewer
        for tag, color in [("error", RED), ("warning", ORANGE), ("info", TEXT),
                           ("debug", TEXTDIM), ("critical", YELLOW)]:
            self._log_viewer.tag_configure(tag, foreground=color)

        # Tags for action viewer
        for tag, color in [
            ("act_key",    "#f0c040"), ("act_combo",  "#f08040"),
            ("act_move",   "#80d0ff"), ("act_cast",   "#ff80ff"),
            ("act_attack", "#ff5555"), ("act_npc",    "#80ffb0"),
            ("act_state",  "#ffdd55"), ("act_nav",    "#55ccff"),
            ("act_event",  "#aaaaaa"), ("act_process","#88ff88"),
            ("act_log",    "#c0c0c0"), ("act_other",  "#888888"),
        ]:
            self._act_viewer.tag_configure(tag, foreground=color)

        # Tags for raw output viewer
        for tag, color in [
            ("raw_tb",     RED),      ("raw_warn",   ORANGE),
            ("raw_dim",    TEXTDIM),  ("raw_normal", "#e0e0e0"),
        ]:
            self._raw_viewer.tag_configure(tag, foreground=color)

        return frame

    def _start_log_tail(self):
        """Start background thread that tails the log file every 1.5s."""
        self._log_tail_running = True
        self._log_tail_thread = threading.Thread(
            target=self._log_tail_loop, daemon=True)
        self._log_tail_thread.start()

    def _log_tail_loop(self):
        last_log: list[str] = []
        last_act: list[str] = []
        last_raw: list[str] = []
        last_profile: str = ""
        while self._log_tail_running:
            try:
                current_profile = self._active_name
                log_lines, act_lines = self._read_log_lines()
                raw_lines = self._read_raw_lines()
                profile_changed = current_profile != last_profile
                if log_lines != last_log or profile_changed:
                    last_log = log_lines
                    self.after(0, lambda l=log_lines: self._set_log_text(l))
                if act_lines != last_act or profile_changed:
                    last_act = act_lines
                    self.after(0, lambda a=act_lines: self._set_act_text(a))
                if raw_lines != last_raw or profile_changed:
                    last_raw = raw_lines
                    self.after(0, lambda r=raw_lines: self._set_raw_text(r))
                last_profile = current_profile
            except Exception:
                pass
            time.sleep(1.5)

    def _read_log_lines(self) -> tuple[list[str], list[str]]:
        """Return (bot_log_lines, action_lines) separately — never merged.
        Bot log = log.txt text (bridge API or direct file read).
        Action lines = actions.jsonl formatted rows."""
        profile = self._profiles.get(self._active_name, ProgramProfile())

        # ── Bot log ───────────────────────────────────────────────────────
        log_lines: list[str] = []
        if self._bridge_online and REQUESTS_OK:
            try:
                r = requests.get(f"{BRIDGE_URL}/program/log?lines=120", timeout=2)
                if r.status_code == 200:
                    data = r.json()
                    src  = data.get("source", "")
                    self.after(0, lambda s=src: self._log_source_lbl.config(text=s))
                    log_lines = data.get("lines", [])
            except Exception:
                pass

        if not log_lines:
            # Fallback: direct file read
            log_path = Path(profile.log_file) if profile.log_file else None
            if log_path and log_path.exists():
                self.after(0, lambda p=str(log_path): self._log_source_lbl.config(text=p))
                log_lines = log_path.read_text(errors="replace").splitlines()[-120:]

        # ── Action log ────────────────────────────────────────────────────
        act_lines = self._parse_action_lines(profile)

        return log_lines, act_lines

    def _parse_action_lines(self, profile: "ProgramProfile") -> list[str]:
        """Read actions.jsonl and format each record as a readable line.
        Returns a list of formatted strings — never mixed with bot log."""
        action_path = (Path(profile.action_log_file)
                       if getattr(profile, "action_log_file", "") else None)
        if not action_path or not action_path.exists():
            return []

        act_lines: list[str] = []
        try:
            raw_lines = action_path.read_text(errors="replace").splitlines()[-120:]
            for raw in raw_lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec    = json.loads(raw)
                    cat    = rec.get("category", "event")
                    src    = rec.get("source", "")
                    ts     = rec.get("ts", "")
                    # Normalise timestamp to HH:MM:SS.mmm
                    if "T" in ts:
                        ts = ts.split("T")[-1].rstrip("Z")[:12]
                    data   = rec.get("data", {})
                    coords = rec.get("coords")
                    coord_s = (f" @({int(coords[0])},{int(coords[1])})"
                               if coords else "")
                    if cat == "key":
                        edge   = data.get("edge")
                        detail = (f"{data.get('button')} {edge}" if edge
                                  else f"{data.get('button')} dur={data.get('duration',0):.2f}s")
                    elif cat == "combo":
                        detail = f"{data.get('combo')} dur={data.get('duration',0):.2f}s"
                    elif cat == "move":
                        detail = f"dir={data.get('dir_deg')}° dur={data.get('duration',0):.2f}s"
                        if data.get("label"):
                            detail += f" [{data['label']}]"
                    elif cat == "cast":
                        detail = str(data.get("skill", ""))
                        if data.get("target"):
                            detail += f" → {data['target']}"
                    elif cat == "attack":
                        detail = f"{data.get('monster','')} skill={data.get('skill','')}"
                    elif cat == "npc":
                        detail = f"{data.get('action','')} {data.get('target','')}"
                    elif cat == "state":
                        detail = f"{data.get('from','')} → {data.get('to','')}"
                    elif cat == "nav":
                        detail = f"{data.get('phase','')} {data.get('label','')}"
                    elif cat in ("process", "log"):
                        detail = str(data.get("msg") or data.get("argv") or
                                     list(data.values())[:1] or "")[:80]
                    elif cat == "event":
                        detail = str(data.get("name", json.dumps(data)))[:80]
                    else:
                        detail = json.dumps(data)[:80]
                    act_lines.append(
                        f"[{ts}] [{cat.upper():<8}] {src:<28} {detail}{coord_s}")
                except Exception:
                    act_lines.append(raw[:120])
        except Exception:
            pass
        return act_lines

    def _refresh_log(self):
        """Force-refresh all three log panels immediately (runs in bg thread)."""
        def _do():
            try:
                log_lines, act_lines = self._read_log_lines()
                raw_lines = self._read_raw_lines()
                self.after(0, lambda l=log_lines: self._set_log_text(l))
                self.after(0, lambda a=act_lines: self._set_act_text(a))
                self.after(0, lambda r=raw_lines: self._set_raw_text(r))
            except Exception:
                pass
            try:
                self._check_log_freshness()
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True).start()

    def _check_log_freshness(self):
        """Is what we are DISPLAYING actually this run's output?

        Asks the bridge for per-source write ages, and — only when something looks
        dead — for the OS's view of where the process is really writing. Both
        questions exist because a log viewer cannot otherwise tell the difference
        between "the program is quiet" and "you are reading the wrong file", and
        those look identical while wasting completely different amounts of time.
        """
        if not (self._bridge_online and REQUESTS_OK):
            return
        stale: list[dict] = []
        try:
            r = requests.get(f"{BRIDGE_URL}/log/raw?peek=1&cap_bytes=1", timeout=3)
            if r.status_code == 200:
                stale = [s for s in (r.json().get("sources") or [])
                         if s.get("stale")]
        except Exception:
            return

        if not stale:
            self.after(0, self._hide_log_warn)
            return

        detail = ""
        try:
            w = requests.get(f"{BRIDGE_URL}/log/where", timeout=6).json()
            if w.get("available") and not w.get("ok"):
                bits = list(w.get("output_destination") or [])[:2]
                for m in (w.get("missing_from_config") or [])[:2]:
                    bits.append(f"the process IS writing {m.get('path')}")
                if bits:
                    detail = "  |  " + "  ·  ".join(bits)
        except Exception:
            pass

        names = ", ".join(f"{s.get('label') or s.get('path')} "
                          f"({int(s.get('last_write_age_s') or 0)}s ago)"
                          for s in stale[:3])
        msg = (f"⚠  STALE LOG — no writes to: {names}. "
               f"You may be reading a file this run is not writing to.{detail}")
        self.after(0, lambda m=msg: self._show_log_warn(m))

    def _show_log_warn(self, msg: str):
        self._log_warn.config(text=msg)
        if not self._log_warn_visible:
            self._log_warn.pack(fill="x", pady=(0, 6), before=self._log_pane)
            self._log_warn_visible = True
        try:
            self._log_live_dot.config(text="● Stale", fg="#f87171")
        except Exception:
            pass

    def _hide_log_warn(self):
        if self._log_warn_visible:
            self._log_warn.pack_forget()
            self._log_warn_visible = False
        try:
            self._log_live_dot.config(text="● Live", fg=GREEN)
        except Exception:
            pass

    def _set_log_text(self, lines: list[str]):
        """Update the top bot-log panel (log.txt lines only)."""
        self._log_viewer.config(state="normal")
        self._log_viewer.delete("1.0", "end")
        for line in lines:
            lu  = line.upper()
            if "CRITICAL" in lu:
                tag = "critical"
            elif "ERROR" in lu or "TRACEBACK" in lu or "EXCEPTION" in lu:
                tag = "error"
            elif "WARNING" in lu or "WARN" in lu:
                tag = "warning"
            elif "DEBUG" in lu:
                tag = "debug"
            else:
                tag = "info"
            self._log_viewer.insert("end", line + "\n", tag)
        self._log_viewer.see("end")
        self._log_viewer.config(state="disabled")

    _ACT_TAG_MAP = {
        "KEY":     "act_key",    "COMBO":   "act_combo",
        "MOVE":    "act_move",   "CAST":    "act_cast",
        "ATTACK":  "act_attack", "NPC":     "act_npc",
        "STATE":   "act_state",  "NAV":     "act_nav",
        "EVENT":   "act_event",  "PROCESS": "act_process",
        "LOG":     "act_log",
    }

    def _set_act_text(self, lines: list[str]):
        """Update the mid action-log panel (actions.jsonl lines only)."""
        self._act_viewer.config(state="normal")
        self._act_viewer.delete("1.0", "end")
        for line in lines:
            tag = "act_other"
            for cat, t in self._ACT_TAG_MAP.items():
                if f"[{cat}" in line:
                    tag = t
                    break
            self._act_viewer.insert("end", line + "\n", tag)
        self._act_viewer.see("end")
        self._act_viewer.config(state="disabled")

    def _read_raw_lines(self) -> list[str]:
        """Read the last 150 lines of log/raw_output.txt for the active profile."""
        profile = self._profiles.get(self._active_name, ProgramProfile())
        # Prefer project_root/log/raw_output.txt
        root = getattr(profile, "project_root", "") or ""
        if root:
            p = Path(root) / "log" / "raw_output.txt"
            if p.exists():
                try:
                    return p.read_text(errors="replace").splitlines()[-150:]
                except Exception:
                    pass
        # Fallback: log_file sibling
        log_file = getattr(profile, "log_file", "") or ""
        if log_file:
            p = Path(log_file).parent / "raw_output.txt"
            if p.exists():
                try:
                    return p.read_text(errors="replace").splitlines()[-150:]
                except Exception:
                    pass
        return []

    def _set_raw_text(self, lines: list[str]):
        """Update the raw program output panel."""
        self._raw_viewer.config(state="normal")
        self._raw_viewer.delete("1.0", "end")
        for line in lines:
            lu = line.upper()
            if ("TRACEBACK" in lu or "ERROR" in lu or "EXCEPTION" in lu
                    or line.strip().startswith("  File ")):
                tag = "raw_tb"
            elif "WARNING" in lu or "WARN" in lu:
                tag = "raw_warn"
            elif "DEBUG" in lu:
                tag = "raw_dim"
            else:
                tag = "raw_normal"
            self._raw_viewer.insert("end", line + "\n", tag)
        self._raw_viewer.see("end")
        self._raw_viewer.config(state="disabled")

    # ─────────────────────────────────────────────────────────────────────────
    # Stats tab
    # ─────────────────────────────────────────────────────────────────────────

    def _build_stats_tab(self) -> tk.Frame:
        frame = tk.Frame(self._nb, bg=BG, padx=20, pady=16)
        tk.Label(frame, text="Program Stats", font=HEADING,
                 bg=BG, fg=TEXT).pack(anchor="w", pady=(0, 10))
        self._stats_frame = tk.Frame(frame, bg=BG)
        self._stats_frame.pack(fill="both", expand=True)
        return frame

    # ─────────────────────────────────────────────────────────────────────────
    # ─────────────────────────────────────────────────────────────────────────
    # Debug Log tab
    # ─────────────────────────────────────────────────────────────────────────

    def _build_debug_tab(self) -> tk.Frame:
        frame = tk.Frame(self._nb, bg=BG, padx=16, pady=10)

        hdr = tk.Frame(frame, bg=BG)
        hdr.pack(fill="x", pady=(0, 6))
        tk.Label(hdr, text="AgentVision Debug Log", font=HEADING,
                 bg=BG, fg=TEXT).pack(side="left")
        self._debug_live_dot = tk.Label(hdr, text="● Auto", font=MONOSM,
                                        bg=BG, fg=GREEN)
        self._debug_live_dot.pack(side="left", padx=8)
        _btn(hdr, "↻ Refresh", self._refresh_debug_log,
             bg="#374151", fg="#e5e7eb", padx=8).pack(side="right")
        _btn(hdr, "Reveal in Files" if platform_shim.IS_WINDOWS else "Open in Finder",
             self._open_debug_log_finder,
             bg="#374151", fg="#e5e7eb", padx=8).pack(side="right", padx=(0, 6))

        self._debug_path_lbl = tk.Label(hdr, text="", font=MONOSM,
                                         bg=BG, fg=TEXTDIM)
        self._debug_path_lbl.pack(side="right", padx=8)

        self._debug_viewer = scrolledtext.ScrolledText(
            frame, bg="#0a0a0a", fg="#a3e635", font=MONOSM,
            relief="flat", state="disabled")
        self._debug_viewer.pack(fill="both", expand=True)

        self._debug_viewer.tag_configure("error",   foreground=RED)
        self._debug_viewer.tag_configure("warn",    foreground=ORANGE)
        self._debug_viewer.tag_configure("info",    foreground="#a3e635")
        self._debug_viewer.tag_configure("debug",   foreground=TEXTDIM)

        return frame

    def _refresh_debug_log(self):
        # Try bridge API first
        av_root  = Path(__file__).parent.parent.parent
        log_path = av_root / "agentvision_debug.log"
        self._debug_path_lbl.config(text=str(log_path))

        lines: list[str] = []
        if self._bridge_online and REQUESTS_OK:
            try:
                r = requests.get(f"{BRIDGE_URL}/debug/log?lines=150", timeout=2)
                if r.status_code == 200:
                    lines = r.json().get("lines", [])
            except Exception:
                pass
        if not lines and log_path.exists():
            try:
                lines = log_path.read_text(errors="replace").splitlines()[-150:]
            except Exception:
                pass

        self._debug_viewer.config(state="normal")
        self._debug_viewer.delete("1.0", "end")
        for line in lines:
            lu = line.upper()
            if "ERROR" in lu:
                tag = "error"
            elif "WARNING" in lu or "WARN" in lu:
                tag = "warn"
            elif "INFO" in lu:
                tag = "info"
            else:
                tag = "debug"
            self._debug_viewer.insert("end", line + "\n", tag)
        self._debug_viewer.see("end")
        self._debug_viewer.config(state="disabled")

    def _open_debug_log_finder(self):
        av_root  = Path(__file__).parent.parent.parent
        log_path = av_root / "agentvision_debug.log"
        platform_shim.reveal_path(log_path)

    # ─────────────────────────────────────────────────────────────────────────
    # Log Coverage tab — surfaces the UNIVERSAL log system in the frontend:
    # live adapter/family/reader counts, a searchable adapter browser grouped
    # by family, a live "test a log line" detector, and the source-reader +
    # deferred-roadmap lists. All counts come from the running registry
    # (_COVERAGE) so they can never go stale.
    # ─────────────────────────────────────────────────────────────────────────

    # Concise mirror of readers.py's DEFERRED roadmap (binary formats that need
    # stateful/template parsers or external tooling — not yet shipped as source
    # readers). Kept short here; the authoritative list is that module's docstring.
    _DEFERRED_READERS = [
        ("Template network telemetry (7)",
         "ipfix, netflow-v9, sflow-v5, bmp, dnstap, snmp-trap, hep3 — need a "
         "stateful template cache / deep TLV / ASN.1 decode."),
        ("Windows EVTX (1)",
         "windows-security-evtx — chunked binary-XML; rendered-XML/JSON exports "
         "already route through the windows_event / jsonl adapters."),
        ("z/OS SMF family (19)",
         "RDW-framed EBCDIC records needing per-type section decoders "
         "(text-side coverage exists: zos_syslog, racf_irradu00, rmf …)."),
        ("IMS/CICS binary traces (4)",
         "EBCDIC, block-structured, layout tied to product internals."),
    ]

    def _coverage_data(self) -> dict:
        """The live coverage snapshot (module-level, computed at import)."""
        return _COVERAGE

    def _build_bridge_tab(self) -> tk.Frame:
        """WHAT IS BUILT INTO THIS PROGRAM — a clean slate per program.

        Every bridged program gets a different set: its own emitters, its own
        adapters (often a custom one written for its log format), its own view of
        which tools are relevant. Nothing here is global. Without this the only
        record of "what did we install and why" lives in a JSON file nobody opens,
        which is how a bridge ends up half-built without anyone noticing.
        """
        frame = tk.Frame(self._nb, bg=BG, padx=16, pady=10)

        hdr = tk.Frame(frame, bg=BG)
        hdr.pack(fill="x", pady=(0, 6))
        tk.Label(hdr, text="This Bridge", font=HEADING, bg=BG, fg=TEXT
                 ).pack(side="left")
        self._bridge_state_lbl = tk.Label(hdr, text="—", font=MONOSM,
                                          bg=BG, fg=TEXTDIM)
        self._bridge_state_lbl.pack(side="left", padx=10)
        _btn(hdr, "\u21bb Refresh", self._refresh_bridge_tab,
             bg="#374151", fg="#e5e7eb", padx=8).pack(side="right")

        self._bridge_banner = tk.Label(frame, text="", font=MONOSM, bg="#7c2d12",
                                       fg="#ffedd5", anchor="w", justify="left",
                                       padx=8, pady=5, wraplength=1000)
        self._bridge_banner_shown = False

        self._bridge_view = scrolledtext.ScrolledText(
            frame, bg=BG3, fg=TEXT, font=MONOSM, relief="flat", state="disabled")
        self._bridge_view.pack(fill="both", expand=True)
        frame.after(600, self._refresh_bridge_tab)
        return frame

    def _refresh_bridge_tab(self):
        def _do():
            rep = None
            if self._bridge_online and REQUESTS_OK:
                try:
                    r = requests.get(f"{BRIDGE_URL}/bridge/report", timeout=8)
                    if r.status_code == 200:
                        rep = r.json()
                except Exception:
                    rep = None
            self.after(0, lambda d=rep: self._render_bridge_tab(d))
        threading.Thread(target=_do, daemon=True).start()

    def _tool_summary(self, name: str, _cache={}) -> str:
        """One-line 'what this tool returns', from the shipped tool metadata.

        Read straight from api/tool_meta.json rather than the bridge, so the panel
        still explains a chosen tool when the server is down. Cached because this
        renders inside a poll loop.
        """
        if not _cache:
            try:
                import json as _json
                from pathlib import Path as _P
                p = _P(__file__).resolve().parent.parent / "api" / "tool_meta.json"
                data = _json.loads(p.read_text(errors="replace"))
                _cache.update({k: (v.get("summary") or "")
                               for k, v in data.items()})
            except Exception:
                _cache["__missing__"] = ""
        s = _cache.get(name, "")
        return (s[:78] + "…") if len(s) > 79 else s

    def _render_bridge_tab(self, rep):
        v = self._bridge_view
        v.config(state="normal")
        v.delete("1.0", "end")
        if not rep:
            v.insert("end", "Bridge server not reachable — start it from the "
                            "Profile tab.\n")
            v.config(state="disabled")
            return

        sealed = bool(rep.get("sealed"))
        self._bridge_state_lbl.config(
            text=("\u25cf BUILT" if sealed else "\u25cf PROVISIONAL"),
            fg=(GREEN if sealed else "#fbbf24"))
        # A provisional bridge is the one state a user must not miss: capture is
        # refused until the agent plans it, and a silently-empty Live Data tab
        # looks identical to a broken install.
        if not sealed:
            self._bridge_banner.config(
                text=("\u26a0  This program has NOT been planned yet. Capture and "
                      "emitter installation are refused until an AI agent reviews "
                      "the catalog (av_bridge_catalog) and commits a plan "
                      "(av_bridge_commit). This happens once per program."))
            if not self._bridge_banner_shown:
                self._bridge_banner.pack(fill="x", pady=(0, 6),
                                         before=self._bridge_view)
                self._bridge_banner_shown = True
        elif self._bridge_banner_shown:
            self._bridge_banner.pack_forget()
            self._bridge_banner_shown = False

        L = []
        L.append(f"PROGRAM   {rep.get('program')}   [profile: {rep.get('profile')}]")
        L.append(f"language  {rep.get('language')}")
        L.append(f"root      {rep.get('project_root')}")
        L.append(f"state     {rep.get('state')}"
                 + (f"   planned by {rep.get('planned_by')}"
                    f" at {str(rep.get('sealed_at') or '')[:19]}"
                    if sealed else ""))
        L.append("")

        L.append("LOGGING BUILT INTO THIS PROGRAM")
        em = rep.get("emitters_built") or []
        if not em:
            L.append("   (none — this program's own logging was judged sufficient,")
            L.append("    or the bridge is not planned yet)")
        why = rep.get("why") or {}
        for e in em:
            L.append(f"   \u2022 {e}")
            r = why.get(e)
            if r:
                L.append(f"       why: {r}")
        L.append("")

        L.append("LOG SOURCES + THE ADAPTER THAT PARSES EACH")
        for s in rep.get("log_sources") or []:
            flag = ""
            if not s.get("exists"):
                flag = "   [MISSING]"
            elif s.get("stale"):
                flag = f"   [STALE {int(s.get('last_write_age_s') or 0)}s]"
            res = s.get("adapter_resolved")
            dec = s.get("adapter_declared")
            shown = res if res == dec else f"{res}  (declared: {dec})"
            conf = s.get("detect_confidence")
            L.append(f"   \u2022 {s.get('label')}{flag}")
            L.append(f"       adapter: {shown}"
                     + (f"   confidence {conf}" if conf is not None else ""))
            L.append(f"       {s.get('path')}   ({s.get('bytes')} bytes)")
        if not rep.get("log_sources"):
            L.append("   (no log sources declared for this profile)")
        L.append("")

        L.append("CAPTURE")
        L.append(f"   visual capture : {rep.get('visual_capture')}")
        cap = rep.get("capture") or {}
        L.append(f"   interval       : {cap.get('interval_seconds', '(unset)')}"
                 + (f"   \u2014 {cap.get('note')}" if cap.get("note") else ""))
        L.append("")

        # Two distinct things, deliberately not merged: what the agent CHOSE for
        # this program, and what merely exists. Showing only the second is what
        # made this panel look identical for every program.
        L.append("MCP TOOLS CHOSEN FOR THIS PROGRAM")
        chosen = rep.get("tools_chosen") or []
        if not rep.get("tools_reviewed"):
            L.append("   (not reviewed \u2014 this bridge was sealed before tool")
            L.append("    review existed. av_bridge_commit(replan=True) to record it.)")
        elif not chosen:
            L.append(f"   (none singled out) {rep.get('tools_note') or ''}".rstrip())
        else:
            for t in chosen:
                summ = self._tool_summary(t)
                L.append(f"   \u2022 {t}" + (f"  \u2014 {summ}" if summ else ""))
        nr = rep.get("tools_not_relevant") or {}
        if nr:
            L.append("   ruled out for this program:")
            for t, why in list(nr.items())[:8]:
                L.append(f"       \u2717 {t}: {why}")
        L.append("")

        L.append(f"ALL MCP TOOLS AVAILABLE  "
                 f"({rep.get('mcp_tool_count')} in "
                 f"{len(rep.get('mcp_tool_groups') or {})} groups \u2014 every tool "
                 f"stays callable)")
        for g, tools in (rep.get("mcp_tool_groups") or {}).items():
            names = tools.get("tools") if isinstance(tools, dict) else tools
            names = [(t.get("tool") if isinstance(t, dict) else t) for t in (names or [])]
            L.append(f"   {g:20} {len(names):>2}  {', '.join(names[:4])}"
                     + (" \u2026" if len(names) > 4 else ""))
        L.append("")
        L.append(f"ADAPTERS IN THE REGISTRY  {rep.get('adapters_available')}  "
                 f"(see the Log Coverage tab to browse)")
        if rep.get("rationale"):
            L.append("")
            L.append("AGENT'S RATIONALE FOR THIS BRIDGE")
            for chunk in [rep["rationale"][i:i + 88]
                          for i in range(0, len(rep["rationale"]), 88)]:
                L.append(f"   {chunk}")
        v.insert("end", "\n".join(L) + "\n")
        v.config(state="disabled")

    def _build_coverage_tab(self) -> tk.Frame:
        cov = self._coverage_data()
        outer = tk.Frame(self._nb, bg=BG)

        # ── Header + live count chips ────────────────────────────────────
        hdr = tk.Frame(outer, bg=BG, padx=16, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Universal Log Coverage", font=HEADING,
                 bg=BG, fg=TEXT).pack(side="left")
        tk.Label(hdr,
                 text="Every log, any language → one JSON timeline. Counts are "
                      "read live from the running registry.",
                 font=LABEL, bg=BG, fg=TEXTDIM).pack(side="left", padx=12)

        chips = tk.Frame(outer, bg=BG, padx=16)
        chips.pack(fill="x", pady=(0, 8))

        def _chip(parent, value, label, fg):
            c = tk.Frame(parent, bg=BG2, padx=14, pady=8)
            c.pack(side="left", padx=(0, 10))
            tk.Label(c, text=str(value), font=(_UI_FACE, 20, "bold"),
                     bg=BG2, fg=fg).pack(anchor="w")
            tk.Label(c, text=label, font=(_UI_FACE, 9),
                     bg=BG2, fg=TEXTDIM).pack(anchor="w")
            return c

        _chip(chips, cov["named"],    "named adapters",  ACCENT)
        _chip(chips, cov["families"], "family modules",  GREEN)
        _chip(chips, cov["readers"],  "source readers",  ORANGE)
        _chip(chips, 2,               "universal fallbacks\n(structural + raw)", YELLOW)
        if not cov.get("ok"):
            tk.Label(chips,
                     text=f"⚠ registry not fully loaded: {cov.get('error','')}",
                     font=MONOSM, bg=BG, fg=RED).pack(side="left", padx=12)

        # ── Body: left = adapter browser, right = test + readers ──────────
        pane = tk.PanedWindow(outer, orient="horizontal", bg=BG,
                              sashwidth=4, sashrelief="flat")
        pane.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        # LEFT — searchable adapter browser grouped by family
        left = tk.Frame(pane, bg=BG)
        srow = tk.Frame(left, bg=BG)
        srow.pack(fill="x", pady=(0, 4))
        tk.Label(srow, text="🔎 Filter adapters:", font=LABEL,
                 bg=BG, fg=TEXTDIM).pack(side="left")
        self._cov_search_var = tk.StringVar()
        se = tk.Entry(srow, textvariable=self._cov_search_var, font=MONOSM,
                      bg=BG3, fg=TEXT, insertbackground=TEXT, relief="flat", bd=4)
        se.pack(side="left", fill="x", expand=True, padx=(6, 0))
        se.bind("<KeyRelease>", lambda _e: self._cov_populate_tree())
        _btn(srow, "clear", lambda: (self._cov_search_var.set(""),
                                     self._cov_populate_tree()),
             bg="#374151", fg="#e5e7eb", padx=8).pack(side="left", padx=(6, 0))

        # Dark Treeview style (scoped so the light File-Tree table is untouched).
        try:
            st = ttk.Style()
            st.configure("Coverage.Treeview", background=BG2,
                         fieldbackground=BG2, foreground=TEXT,
                         borderwidth=0, font=MONOSM, rowheight=20)
            st.configure("Coverage.Treeview.Heading", background=BG3,
                         foreground=TEXT, font=LABELBOLD, borderwidth=0)
            st.map("Coverage.Treeview",
                   background=[("selected", ACCENT)],
                   foreground=[("selected", "#000000")])
        except Exception:
            pass

        tbody = tk.Frame(left, bg=BG)
        tbody.pack(fill="both", expand=True)
        self._cov_tree = ttk.Treeview(
            tbody, columns=("lang",), show="tree headings",
            style="Coverage.Treeview")
        self._cov_tree.heading("#0", text="Family / adapter")
        self._cov_tree.heading("lang", text="Language / tag")
        self._cov_tree.column("#0", width=320, stretch=True)
        self._cov_tree.column("lang", width=150, anchor="w", stretch=False)
        csb = ttk.Scrollbar(tbody, orient="vertical",
                            command=self._cov_tree.yview)
        self._cov_tree.config(yscrollcommand=csb.set)
        csb.pack(side="right", fill="y")
        self._cov_tree.pack(side="left", fill="both", expand=True)
        pane.add(left, minsize=380, width=560)

        # RIGHT — test-a-line + source readers + deferred roadmap
        right = tk.Frame(pane, bg=BG)

        # (a) Test a log line
        test_card = tk.Frame(right, bg=BG2, padx=12, pady=10)
        test_card.pack(fill="x", pady=(0, 8))
        tk.Label(test_card, text="Test a log line", font=(_UI_FACE, 13, "bold"),
                 bg=BG2, fg=TEXT).pack(anchor="w")
        tk.Label(test_card,
                 text="Paste any log line — see which adapter claims it and how "
                      "confident it is. Detection never fails: unknown lines fall "
                      "to `structural` or `raw`.",
                 font=(_UI_FACE, 10), bg=BG2, fg=TEXTDIM, justify="left",
                 wraplength=380, anchor="w").pack(fill="x", pady=(2, 6))
        self._cov_test_var = tk.StringVar()
        te = tk.Entry(test_card, textvariable=self._cov_test_var, font=MONOSM,
                      bg="#0d1117", fg="#a8d4ff", insertbackground="#a8d4ff",
                      relief="flat", bd=6)
        te.pack(fill="x")
        te.bind("<Return>", lambda _e: self._cov_test_line())
        trow = tk.Frame(test_card, bg=BG2)
        trow.pack(fill="x", pady=(6, 0))
        _btn(trow, "Detect adapter", self._cov_test_line,
             bg="#15803d", fg="#ffffff", padx=12, pady=3).pack(side="left")
        for sample in ('{"level":"error","msg":"x"}',
                       "2026-07-21 10:00:00 ERROR app: boom",
                       "E0721 10:00:00.1 1 f.go:12] msg"):
            _btn(trow, "e.g.", lambda s=sample: (self._cov_test_var.set(s),
                                                 self._cov_test_line()),
                 bg="#1e3a5f", fg="#a8d4ff", padx=6).pack(side="left", padx=(6, 0))
        self._cov_result = scrolledtext.ScrolledText(
            test_card, bg="#0a0a0a", fg=TEXT, font=MONOSM, height=8,
            relief="flat", state="disabled", wrap="word")
        self._cov_result.pack(fill="x", pady=(8, 0))
        self._cov_result.tag_configure("win", foreground=GREEN)
        self._cov_result.tag_configure("dim", foreground=TEXTDIM)

        # (b) Source readers (binary/stream)
        rd_card = tk.Frame(right, bg=BG2, padx=12, pady=10)
        rd_card.pack(fill="both", expand=True)
        tk.Label(rd_card, text=f"Binary / stream source readers "
                               f"({cov['readers']})",
                 font=(_UI_FACE, 13, "bold"), bg=BG2, fg=TEXT).pack(anchor="w")
        tk.Label(rd_card,
                 text="Non-text sources decoded to the SAME schema and merged "
                      "onto the one timeline:",
                 font=(_UI_FACE, 10), bg=BG2, fg=TEXTDIM, justify="left",
                 wraplength=380, anchor="w").pack(fill="x", pady=(2, 4))
        rn = ", ".join(cov.get("reader_names") or []) or "(none loaded)"
        tk.Label(rd_card, text=rn, font=MONOSM, bg=BG2, fg=ORANGE,
                 justify="left", wraplength=380, anchor="w").pack(fill="x")

        tk.Label(rd_card, text="Deferred (roadmap — see connectors/readers.py):",
                 font=(_UI_FACE, 11, "bold"), bg=BG2, fg=TEXTDIM,
                 anchor="w").pack(fill="x", pady=(10, 2))
        defer = scrolledtext.ScrolledText(
            rd_card, bg="#0d1117", fg=TEXTDIM, font=(_MONO_FACE, 9),
            relief="flat", state="normal", wrap="word", height=8)
        for title, desc in self._DEFERRED_READERS:
            defer.insert("end", f"• {title}\n", "t")
            defer.insert("end", f"    {desc}\n\n")
        defer.tag_configure("t", foreground=YELLOW)
        defer.config(state="disabled")
        defer.pack(fill="both", expand=True)

        pane.add(right, minsize=360)

        # Populate the tree now.
        self._cov_populate_tree()
        return outer

    def _cov_populate_tree(self) -> None:
        """(Re)build the family→adapter tree, honoring the search filter."""
        tree = getattr(self, "_cov_tree", None)
        if tree is None:
            return
        q = (self._cov_search_var.get() or "").strip().lower()
        tree.delete(*tree.get_children())
        fams = self._coverage_data().get("families_map") or {}
        for fam in sorted(fams.keys()):
            adapters = fams[fam]
            if q:
                adapters = [a for a in adapters
                            if q in a["name"].lower()
                            or q in (a.get("language") or "").lower()
                            or q in fam.lower()]
                if not adapters:
                    continue
            fam_id = tree.insert("", "end",
                                 text=f"{fam}  ({len(adapters)})",
                                 values=("",), open=bool(q))
            for a in sorted(adapters, key=lambda x: x["name"]):
                tree.insert(fam_id, "end", text=f"   {a['name']}",
                            values=(a.get("language") or "—",))

    def _cov_test_line(self) -> None:
        """Run detect_adapter() on the pasted line and render the winner +
        confidence + the top competing scores."""
        line = self._cov_test_var.get()
        out = self._cov_result
        out.config(state="normal")
        out.delete("1.0", "end")
        if not line.strip():
            out.insert("end", "Paste a log line above, then Detect adapter.\n", "dim")
            out.config(state="disabled")
            return
        try:
            from connectors import log_adapters as _la
            winner, conf, scores = _la.detect_adapter([line])
            ev = None
            try:
                ev = winner.parse_line(line)
            except Exception:
                ev = None
            out.insert("end", f"→ adapter: {winner.name}", "win")
            out.insert("end", f"   (confidence {conf:.3f})\n", "dim")
            lang = getattr(winner, "language", "") or "—"
            out.insert("end", f"   language/tag: {lang}\n", "dim")
            if ev:
                lvl = ev.get("level") or "—"
                cat = ev.get("category") or "—"
                msg = ((ev.get("data") or {}).get("message") or "")[:120]
                out.insert("end", f"   level={lvl}  category={cat}\n", "dim")
                if msg:
                    out.insert("end", f"   message: {msg}\n", "dim")
            top = sorted(scores.items(), key=lambda kv: -kv[1])[:6]
            top = [(n, s) for n, s in top if s > 0]
            if top:
                out.insert("end", "\n   top scores:\n", "dim")
                for n, s in top:
                    out.insert("end", f"     {s:>5.3f}  {n}\n", "dim")
            out.insert("end",
                       "\n   (nothing is ever dropped — structural/raw are the "
                       "guaranteed floor)\n", "dim")
        except Exception as e:
            out.insert("end", f"detect failed: {e}\n", "dim")
        out.config(state="disabled")

    # How To Use tab
    # ─────────────────────────────────────────────────────────────────────────

    # ─────────────────────────────────────────────────────────────────────────
    # Permissions tab — one-time grants the OS asks for. Once enabled, the
    # input recorder follows the bridge lifecycle automatically (no per-run
    # button to press).
    # ─────────────────────────────────────────────────────────────────────────

    _DAEMON_PID_FILE = platform_shim.daemon_pid_file()
    _DAEMON_LOG_FILE = platform_shim.daemon_log_file()
    # User preference: when True, the daemon is started automatically on
    # bridge online (and respawned if it dies). Persisted across sessions.
    _PERMS_PREFS_FILE = (Path(__file__).resolve().parent.parent.parent
                         / "snapshots" / "permissions.json")

    def _av_root(self) -> Path:
        return Path(__file__).resolve().parent.parent.parent

    def _daemon_pid(self) -> int | None:
        try:
            pid = int(self._DAEMON_PID_FILE.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None
        return pid if platform_shim.pid_alive(pid) else None

    def _daemon_running(self) -> bool:
        return self._daemon_pid() is not None

    def _load_perms(self) -> dict:
        try:
            return json.loads(self._PERMS_PREFS_FILE.read_text())
        except Exception:
            return {}

    def _save_perms(self, prefs: dict) -> None:
        try:
            self._PERMS_PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
            self._PERMS_PREFS_FILE.write_text(json.dumps(prefs, indent=2))
        except Exception:
            pass

    def _input_recorder_enabled(self) -> bool:
        return bool(self._load_perms().get("input_recorder_enabled"))

    def _spawn_input_daemon(self) -> tuple[bool, str]:
        """Idempotent: starts the daemon if not already running.
        Returns (running_after, message)."""
        if self._daemon_running():
            return True, f"already running (pid {self._daemon_pid()})"
        py = sys.executable
        env = os.environ.copy()
        env["AGENTVISION_DAEMON_PID_FILE"] = str(self._DAEMON_PID_FILE)
        env["AGENTVISION_DAEMON_LOG"] = str(self._DAEMON_LOG_FILE)
        try:
            with open(self._DAEMON_LOG_FILE, "ab") as logf:
                subprocess.Popen(
                    [py, "-m", "python_backend.daemon.input_daemon"],
                    cwd=str(self._av_root()), env=env,
                    stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                    **platform_shim.detached_popen_kwargs(),
                )
        except Exception as e:
            return False, f"spawn failed: {e}"
        # Wait briefly for pidfile.
        for _ in range(25):
            time.sleep(0.1)
            if self._daemon_pid():
                return True, f"started (pid {self._daemon_pid()})"
        return False, f"did not write pidfile — see {self._DAEMON_LOG_FILE}"

    def _on_enable_recorder(self):
        """User clicked the Enable button. Persist preference + spawn now."""
        prefs = self._load_perms()
        prefs["input_recorder_enabled"] = True
        prefs["input_recorder_first_enabled"] = (
            prefs.get("input_recorder_first_enabled")
            or datetime.now().isoformat())
        self._save_perms(prefs)
        ok, msg = self._spawn_input_daemon()
        self._set_status(("Input Recorder enabled — " if ok
                          else "Enable failed — ") + msg)
        self._poll_perms_status()

    def _on_disable_recorder(self):
        """User clicked Disable. Stop daemon AND clear persistence so it
        won't auto-start next time."""
        prefs = self._load_perms()
        prefs["input_recorder_enabled"] = False
        self._save_perms(prefs)
        pid = self._daemon_pid()
        if pid:
            platform_shim.terminate_pid(pid)
        self._set_status("Input Recorder disabled.")
        self.after(500, self._poll_perms_status)

    def _autostart_if_enabled(self):
        """Called when the bridge comes online. Spawns the daemon if the
        user has previously enabled it. Idempotent — does nothing if the
        daemon's already running."""
        if not self._input_recorder_enabled():
            return
        if self._daemon_running():
            return
        ok, msg = self._spawn_input_daemon()
        if ok:
            self._set_status(f"Input Recorder auto-started: {msg}")

    def _poll_perms_status(self):
        try:
            pid = self._daemon_pid()
            enabled = self._input_recorder_enabled()
            if pid is not None:
                self._perms_state_lbl.config(
                    text=f"● Recording   pid {pid}", fg=GREEN)
            elif enabled:
                self._perms_state_lbl.config(
                    text="● Enabled but not running (auto-starts with bridge)",
                    fg=YELLOW)
            else:
                self._perms_state_lbl.config(text="● Disabled", fg=TEXTDIM)
            # If enabled but bridge is online and daemon is dead, respawn.
            if enabled and pid is None and getattr(self, "_bridge_online", False):
                self._spawn_input_daemon()
        except Exception:
            pass
        self.after(4000, self._poll_perms_status)

    def _build_permissions_tab(self) -> tk.Frame:
        frame = tk.Frame(self._nb, bg=BG, padx=20, pady=16)

        tk.Label(frame, text="Permissions",
                 font=HEADING, bg=BG, fg=TEXT, anchor="w"
                 ).pack(fill="x", pady=(0, 4))
        _perm_intro = (
            "One-time grants AgentVision needs from macOS. After you allow "
            "each item once, AgentVision uses that permission automatically — "
            "no per-session prompts."
            if platform_shim.IS_MAC else
            "On Windows and Linux AgentVision needs NO special OS permission: "
            "desktop screen capture and the global input recorder both work "
            "for a normal user account. Just click 'Enable Input Recorder' "
            "below. (The macOS-specific buttons are harmless no-ops here. On "
            "Wayland, capture falls back to region/full-screen — see the "
            "How-To's CROSS-PLATFORM section.)")
        tk.Label(frame, text=_perm_intro,
                 font=LABEL, bg=BG, fg=TEXTDIM, justify="left", anchor="w",
                 wraplength=720).pack(fill="x", pady=(0, 16))

        # ── Card: Global Input Recorder (Accessibility) ──────────────────
        card = tk.Frame(frame, bg=BG2, padx=16, pady=14)
        card.pack(fill="x", pady=(0, 12))

        title_row = tk.Frame(card, bg=BG2)
        title_row.pack(fill="x")
        tk.Label(title_row, text="Global Input Recorder",
                 font=(_UI_FACE, 14, "bold"),
                 bg=BG2, fg=TEXT).pack(side="left")
        self._perms_state_lbl = tk.Label(title_row, text="● Disabled",
                                         font=MONOSM, bg=BG2, fg=TEXTDIM)
        self._perms_state_lbl.pack(side="right")

        tk.Label(card,
                 text=("Captures every keypress and every mouse "
                       "move/click/scroll system-wide and writes them into "
                       "the active project's actions.jsonl. This lets "
                       "Claude correlate any frame with what you were "
                       "doing at that exact moment."),
                 font=LABEL, bg=BG2, fg=TEXT, justify="left", anchor="w",
                 wraplength=680).pack(fill="x", pady=(8, 6))

        tk.Label(card,
                 text=("Windows needs NO special permission — the global input "
                       "recorder works for a normal user account. Click "
                       "'Enable Input Recorder' and AgentVision starts it "
                       "automatically whenever the bridge is running."
                       if platform_shim.IS_WINDOWS else
                       "macOS will ask for Accessibility access the first "
                       "time you enable this. Grant it under System "
                       "Settings → Privacy & Security → Accessibility. "
                       "After that, no further prompts — AgentVision will "
                       "start the recorder automatically whenever the "
                       "bridge is running."),
                 font=(_UI_FACE, 11), bg=BG2, fg=TEXTDIM,
                 justify="left", anchor="w", wraplength=680
                 ).pack(fill="x", pady=(0, 12))

        btn_row = tk.Frame(card, bg=BG2)
        btn_row.pack(fill="x")
        _btn(btn_row, "✓ Enable Input Recorder",
             self._on_enable_recorder, bg="#15803d", fg="#ffffff",
             padx=14, pady=4).pack(side="left")
        _btn(btn_row, "Disable", self._on_disable_recorder,
             bg="#374151", fg="#e5e7eb", padx=10, pady=4
             ).pack(side="left", padx=(8, 0))
        _btn(btn_row, "Open System Settings → Accessibility",
             self._open_accessibility_settings,
             bg="#1e3a5f", fg="#a8d4ff", padx=10, pady=4
             ).pack(side="left", padx=(8, 0))

        # ── Card: Screen Recording (Pick Region / screenshot) ────────────
        # macOS requires the Screen Recording permission for any API that
        # reads pixel content from other apps — including the "Pick Region"
        # crop tool in the Profile tab and the screencapture command the
        # bridge uses to grab frames. Grant it once here; macOS remembers it.
        scr_card = tk.Frame(frame, bg=BG2, padx=16, pady=14)
        scr_card.pack(fill="x", pady=(0, 12))

        scr_title_row = tk.Frame(scr_card, bg=BG2)
        scr_title_row.pack(fill="x")
        tk.Label(scr_title_row, text="Screen Recording & Video",
                 font=(_UI_FACE, 14, "bold"),
                 bg=BG2, fg=TEXT).pack(side="left")

        tk.Label(scr_card,
                 text=("Required for two things:\n\n"
                       "  • Pick Region — lets you draw a crop box over any "
                       "window so AgentVision only captures the program you "
                       "care about.\n"
                       "  • Screenshot capture — the bridge calls "
                       "screencapture to grab frames; macOS blocks this "
                       "without Screen Recording access.\n\n"
                       "This is a one-time grant. After you allow it, both "
                       "Pick Region and frame capture work automatically "
                       "with no further prompts."),
                 font=LABEL, bg=BG2, fg=TEXT, justify="left", anchor="w",
                 wraplength=680).pack(fill="x", pady=(8, 6))

        tk.Label(scr_card,
                 text=("Grant under System Settings → Privacy & Security → "
                       "Screen Recording. Add this app (Python / AgentVision) "
                       "to the list, then click the button below to verify "
                       "the grant is active."),
                 font=(_UI_FACE, 11), bg=BG2, fg=TEXTDIM,
                 justify="left", anchor="w", wraplength=680
                 ).pack(fill="x", pady=(0, 12))

        scr_btn_row = tk.Frame(scr_card, bg=BG2)
        scr_btn_row.pack(fill="x")
        _btn(scr_btn_row, "Open System Settings → Screen Recording",
             self._open_screen_recording_settings,
             bg="#1e3a5f", fg="#a8d4ff", padx=10, pady=4
             ).pack(side="left")
        _btn(scr_btn_row, "Test Screenshot Now",
             self._test_screenshot_permission,
             bg="#374151", fg="#e5e7eb", padx=10, pady=4
             ).pack(side="left", padx=(8, 0))
        self._scr_test_lbl = tk.Label(scr_btn_row, text="",
                                      font=MONOSM, bg=BG2, fg=TEXTDIM)
        self._scr_test_lbl.pack(side="left", padx=(12, 0))

        # ── Card: Per-project capture opt-in ─────────────────────────────
        # The Accessibility grant above lets the daemon LISTEN system-wide,
        # but listening ≠ recording. Each project decides whether the
        # daemon should actually write the user's physical inputs into
        # ITS actions.jsonl. Default OFF so a bot's diagnostics aren't
        # polluted by the user's Bluetooth mouse / 2.4GHz keyboard.
        cap_card = tk.Frame(frame, bg=BG2, padx=16, pady=14)
        cap_card.pack(fill="x", pady=(0, 12))

        cap_title_row = tk.Frame(cap_card, bg=BG2)
        cap_title_row.pack(fill="x")
        tk.Label(cap_title_row, text="Capture User Input → Active Project",
                 font=(_UI_FACE, 14, "bold"),
                 bg=BG2, fg=TEXT).pack(side="left")
        self._cap_state_lbl = tk.Label(cap_title_row, text="● Off",
                                       font=MONOSM, bg=BG2, fg=TEXTDIM)
        self._cap_state_lbl.pack(side="right")

        tk.Label(cap_card,
                 text=("The recorder is ALWAYS listening. Synthetic events "
                       "posted by the bridged program (pyautogui, cliclick, "
                       "Quartz, robotjs, AppleScript, etc.) are ALWAYS "
                       "recorded — that's the whole point.\n\n"
                       "This toggle only controls whether YOUR physical "
                       "keyboard + mouse (USB or Bluetooth) also get "
                       "recorded:\n\n"
                       "  OFF (default) — physical hardware events are "
                       "filtered out at the CGEvent layer "
                       "(kCGEventSourceUnixProcessID == 0). Diagnostics "
                       "stay clean.\n"
                       "  ON — physical hardware events ALSO get logged. "
                       "Turn this on only when YOU are the agent: manual "
                       "play, RPA recording, demo capture."),
                 font=LABEL, bg=BG2, fg=TEXT, justify="left", anchor="w",
                 wraplength=680).pack(fill="x", pady=(8, 10))

        self._cap_active_lbl = tk.Label(cap_card,
                                        text="(no active project)",
                                        font=MONOSM, bg=BG2, fg=TEXTDIM,
                                        anchor="w")
        self._cap_active_lbl.pack(fill="x", pady=(0, 8))

        cap_btn_row = tk.Frame(cap_card, bg=BG2)
        cap_btn_row.pack(fill="x")
        _btn(cap_btn_row, "Turn ON for active project",
             self._on_capture_user_input_on, bg="#15803d", fg="#ffffff",
             padx=14, pady=4).pack(side="left")
        _btn(cap_btn_row, "Turn OFF",
             self._on_capture_user_input_off, bg="#374151", fg="#e5e7eb",
             padx=10, pady=4).pack(side="left", padx=(8, 0))

        # Footer: where logs live so the user can debug if it ever fails.
        tk.Label(frame,
                 text=f"Daemon log: {self._DAEMON_LOG_FILE}     "
                      f"PID file: {self._DAEMON_PID_FILE}",
                 font=(_UI_FACE, 10), bg=BG, fg=TEXTDIM,
                 anchor="w").pack(fill="x", pady=(8, 0))

        # Start polling status (also drives auto-respawn while bridge online).
        self.after(800, self._poll_perms_status)
        self._refresh_capture_toggle_label()
        return frame

    # ── Per-project capture-user-input toggle ────────────────────────────
    def _refresh_capture_toggle_label(self) -> None:
        """Sync the Permissions-tab capture indicator to the active profile."""
        name = getattr(self, "_active_name", "") or ""
        prof = self._profiles.get(name)
        if not prof:
            try:
                self._cap_active_lbl.config(text="(no active project)")
                self._cap_state_lbl.config(text="● Off", fg=TEXTDIM)
            except Exception:
                pass
            return
        on = bool(getattr(prof, "capture_user_input", False))
        try:
            self._cap_active_lbl.config(
                text=f"Active project: {prof.display_name}  "
                     f"→ sink: {prof.action_log_file}")
            if on:
                self._cap_state_lbl.config(
                    text="● Physical INCLUDED (recording you too)",
                    fg="#f59e0b")
            else:
                self._cap_state_lbl.config(
                    text="● Physical FILTERED (bot-only, default)",
                    fg="#22c55e")
        except Exception:
            pass

    def _set_capture_user_input(self, value: bool) -> None:
        name = getattr(self, "_active_name", "") or ""
        prof = self._profiles.get(name)
        if not prof:
            self._set_status("No active project to toggle.")
            return
        prof.capture_user_input = bool(value)
        if not self._save_profiles_or_warn():
            return
        self._refresh_capture_toggle_label()
        state = "ON" if value else "OFF"
        self._set_status(
            f"capture_user_input = {state} for '{prof.display_name}' "
            f"(daemon picks up within 2s)")

    def _on_capture_user_input_on(self):
        self._set_capture_user_input(True)

    def _on_capture_user_input_off(self):
        self._set_capture_user_input(False)

    def _open_accessibility_settings(self):
        """Open the macOS Accessibility pane. On Windows no such grant exists
        (global low-level hooks work for a normal user), so just inform."""
        if platform_shim.IS_MAC:
            platform_shim.open_url(
                "x-apple.systempreferences:com.apple.preference.security"
                "?Privacy_Accessibility")
        else:
            messagebox.showinfo(
                "Input Recorder",
                "Windows does not require a special permission for the input "
                "recorder — the global keyboard/mouse hooks work for a normal "
                "user account. Just click 'Enable Input Recorder'.")

    def _open_screen_recording_settings(self):
        """Open the macOS Screen Recording pane. Windows needs no such grant."""
        if platform_shim.IS_MAC:
            platform_shim.open_url(
                "x-apple.systempreferences:com.apple.preference.security"
                "?Privacy_ScreenCapture")
        else:
            messagebox.showinfo(
                "Screen Capture",
                platform_shim.screen_capture_permission_note())

    def _test_screenshot_permission(self):
        """Take a small test screenshot to verify capture works.
        On macOS a missing/empty file means Screen Recording is not granted."""
        import tempfile
        from pathlib import Path as _P
        tmp = tempfile.mktemp(suffix=".png")
        try:
            platform_shim.capture_frame(tmp)
            if _P(tmp).exists() and _P(tmp).stat().st_size > 0:
                _P(tmp).unlink(missing_ok=True)
                self._scr_test_lbl.config(
                    text="✓ Screen capture works", fg="#22c55e")
            else:
                self._scr_test_lbl.config(
                    text="✗ Blocked — check capture permission/deps",
                    fg="#ef4444")
        except Exception as e:
            self._scr_test_lbl.config(
                text=f"✗ Test failed: {e}", fg="#ef4444")

    # ─────────────────────────────────────────────────────────────────────────
    # Tab-change router — keep Source Code + File Tree in sync with the
    # active profile. Tracks the last profile seen by each tab and forces
    # a refresh if it differs from the currently active one.
    # ─────────────────────────────────────────────────────────────────────────

    def _on_tab_changed(self, _evt=None):
        try:
            current = self._nb.select()
        except Exception:
            return
        active = getattr(self, "_active_name", None)
        if current == str(self._tab_source):
            # Force refresh when: (a) profile changed since last successful
            # load, OR (b) index is empty (previous load failed silently).
            # _src_seen_for is set ONLY after refresh succeeds (line ~2587),
            # so a failed refresh leaves it unchanged → next click retries.
            if (getattr(self, "_src_seen_for", None) != active
                    or not getattr(self, "_src_index", [])):
                self._refresh_source_tab()
        elif current == str(self._tab_tree):
            if (getattr(self, "_tree_seen_for", None) != active
                    or not getattr(self, "_tree_data", None)):
                self._refresh_tree_tab()

    # ─────────────────────────────────────────────────────────────────────────
    # Source Code tab — file browser + viewer for the bridged project
    # ─────────────────────────────────────────────────────────────────────────

    def _build_source_tab(self) -> tk.Frame:
        outer = tk.Frame(self._nb, bg=BG)

        hdr = tk.Frame(outer, bg=BG, padx=12, pady=8)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Source Code (bridged project)",
                 font=HEADING, bg=BG, fg=TEXT).pack(side="left")
        self._src_meta_lbl = tk.Label(hdr, text="", font=MONOSM,
                                      bg=BG, fg=TEXTDIM)
        self._src_meta_lbl.pack(side="left", padx=14)
        _btn(hdr, "↻ Refresh", self._refresh_source_tab,
             bg="#374151", fg="#e5e7eb", padx=8).pack(side="right")

        pane = tk.PanedWindow(outer, orient="horizontal", bg=BG,
                              sashwidth=4, sashrelief="flat")
        pane.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        # Left: file list with filename filter AND keyword content search
        left = tk.Frame(pane, bg=BG2)
        pane.add(left, minsize=340, width=420)

        tk.Label(left, text="Filter filenames", font=MONOSM, bg=BG2,
                 fg=TEXTDIM, anchor="w").pack(fill="x", padx=8, pady=(8, 2))
        self._src_filter_var = tk.StringVar()
        self._src_filter_var.trace_add("write",
                                       lambda *_: self._populate_source_list())
        tk.Entry(left, textvariable=self._src_filter_var,
                 bg=BG3, fg=TEXT, font=MONOSM, relief="flat"
                 ).pack(fill="x", padx=8, pady=(0, 6))

        tk.Label(left, text="Find in source code (Enter to search)",
                 font=MONOSM, bg=BG2, fg=TEXTDIM, anchor="w"
                 ).pack(fill="x", padx=8, pady=(4, 2))
        srow = tk.Frame(left, bg=BG2)
        srow.pack(fill="x", padx=8, pady=(0, 4))
        self._src_find_var = tk.StringVar()
        find_entry = tk.Entry(srow, textvariable=self._src_find_var,
                              bg=BG3, fg=TEXT, font=MONOSM, relief="flat")
        find_entry.pack(side="left", fill="x", expand=True)
        find_entry.bind("<Return>", lambda _e: self._do_source_find())
        _btn(srow, "🔍", self._do_source_find,
             bg="#374151", fg="#e5e7eb", padx=6).pack(side="left", padx=(4, 0))
        _btn(srow, "✕", self._clear_source_find,
             bg="#374151", fg="#e5e7eb", padx=6).pack(side="left", padx=(2, 0))
        self._src_find_status = tk.Label(left, text="", font=MONOSM,
                                         bg=BG2, fg=TEXTDIM, anchor="w")
        self._src_find_status.pack(fill="x", padx=8)

        list_wrap = tk.Frame(left, bg=BG2)
        list_wrap.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self._src_listbox = tk.Listbox(
            list_wrap, bg=BG3, fg=TEXT, font=MONOSM,
            selectbackground=ACCENT, selectforeground="#ffffff",
            relief="flat", activestyle="none", exportselection=False)
        sb = tk.Scrollbar(list_wrap, command=self._src_listbox.yview)
        self._src_listbox.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._src_listbox.pack(side="left", fill="both", expand=True)
        self._src_listbox.bind("<<ListboxSelect>>",
                               self._on_source_file_select)

        # Search results state — when populated, the listbox shows matches
        # rather than filenames, and selecting a match jumps to that line.
        self._src_search_results: list[dict] = []  # match dicts from /source/search

        # Right: file viewer
        right = tk.Frame(pane, bg=BG)
        pane.add(right)
        self._src_path_lbl = tk.Label(right, text="(select a file)",
                                      font=MONOSM, bg=BG, fg=TEXTDIM,
                                      anchor="w")
        self._src_path_lbl.pack(fill="x", padx=4, pady=(8, 4))
        self._src_viewer = scrolledtext.ScrolledText(
            right, bg=BG3, fg=TEXT, font=MONOSM, relief="flat",
            state="disabled", wrap="none")
        self._src_viewer.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        # Cache for full file index — populated by _refresh_source_tab.
        self._src_index: list[dict] = []

        # Auto-refresh once on first open.
        outer.after(800, self._refresh_source_tab)
        return outer

    def _refresh_source_tab(self):
        """Kick off the heavy refresh in a worker thread so Tk stays
        responsive. Without this, /source/refresh on a large repo (~30 s
        for index+light+digest+tree) would freeze the UI and ALWAYS
        time out, leaving the listbox empty with no indication of why."""
        if not REQUESTS_OK:
            try:
                self._src_meta_lbl.config(
                    text="(`requests` python package not installed — "
                         "pip install requests)")
            except Exception:
                pass
            return
        try:
            self._src_meta_lbl.config(text="refreshing source index…")
        except Exception:
            pass
        threading.Thread(target=self._refresh_source_tab_worker,
                         daemon=True).start()

    def _refresh_source_tab_worker(self):
        """Runs OFF the Tk thread. Posts back to Tk via .after() so we
        never touch widgets from a non-main thread."""
        try:
            # Refresh can take a while on large repos; 180 s ceiling.
            rp = requests.post(f"{BRIDGE_URL}/source/refresh", timeout=180)
        except Exception as e:
            self.after(0, lambda e=e: self._src_meta_lbl.config(
                text=f"refresh failed: {type(e).__name__}: {e}"))
            return
        # Surface 404 (no project_root) vs 500 (mirror crash) clearly so
        # the user knows whether to set project_root or fix a bug.
        if rp.status_code == 404:
            try:
                msg = rp.json().get("error", "404")
            except Exception:
                msg = "project_root not set"
            self.after(0, lambda: self._src_meta_lbl.config(
                text=f"⚠ {msg} — set project_root in the Profile tab"))
            return
        if rp.status_code != 200:
            try:
                detail = rp.json().get("error", rp.text[:200])
            except Exception:
                detail = rp.text[:200]
            self.after(0, lambda d=detail, sc=rp.status_code:
                       self._src_meta_lbl.config(
                           text=f"refresh failed HTTP {sc}: {d}"))
            return

        try:
            r = requests.get(f"{BRIDGE_URL}/source/list", timeout=20)
        except Exception as e:
            self.after(0, lambda e=e: self._src_meta_lbl.config(
                text=f"list failed: {type(e).__name__}: {e}"))
            return
        if r.status_code != 200:
            self.after(0, lambda: self._src_meta_lbl.config(
                text=f"list HTTP {r.status_code}"))
            return

        try:
            data = r.json()
        except Exception as e:
            self.after(0, lambda e=e: self._src_meta_lbl.config(
                text=f"list parse error: {e}"))
            return

        files = data.get("files", [])
        meta = (f"{data.get('file_count', 0)} files  "
                f"@ {data.get('project_root', '?')}")

        def _apply():
            self._src_index = files
            self._src_meta_lbl.config(text=meta)
            self._populate_source_list()
            try:
                self._refresh_tree_tab()
                self._tree_seen_for = getattr(self, "_active_name", None)
            except Exception:
                pass
            self._src_seen_for = getattr(self, "_active_name", None)
        self.after(0, _apply)

    def _populate_source_list(self):
        # When search results are active, ignore filename filter — the
        # results list IS what's shown until the user clears search.
        if self._src_search_results:
            self._src_listbox.delete(0, "end")
            for m in self._src_search_results:
                self._src_listbox.insert("end",
                    f"{m['path']}:{m['line']}  {m['text'].strip()[:80]}")
            return
        flt = (self._src_filter_var.get() or "").lower().strip()
        self._src_listbox.delete(0, "end")
        for f in self._src_index:
            if flt and flt not in f["path"].lower():
                continue
            self._src_listbox.insert("end",
                f"{f['path']}  ({f['lines']}L, {f['lang']})")

    def _do_source_find(self):
        q = (self._src_find_var.get() or "").strip()
        if not q:
            return
        if not REQUESTS_OK:
            self._src_find_status.config(text="requests module missing")
            return
        try:
            r = requests.get(f"{BRIDGE_URL}/source/search",
                             params={"q": q, "limit": 500}, timeout=20)
        except Exception as e:
            self._src_find_status.config(text=f"search failed: {e}")
            return
        if r.status_code != 200:
            self._src_find_status.config(text=f"HTTP {r.status_code}")
            return
        data = r.json()
        self._src_search_results = data.get("matches", [])
        n = data.get("match_count", 0)
        files = data.get("files_with_hits", 0)
        trunc = " (truncated)" if data.get("truncated") else ""
        self._src_find_status.config(
            text=f'"{q}" — {n} matches in {files} files{trunc}')
        self._populate_source_list()

    def _clear_source_find(self):
        self._src_find_var.set("")
        self._src_search_results = []
        self._src_find_status.config(text="")
        self._populate_source_list()

    def _on_source_file_select(self, _evt):
        sel = self._src_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        # Search-results mode: parse "path:line  preview"
        if self._src_search_results:
            if idx >= len(self._src_search_results):
                return
            m = self._src_search_results[idx]
            path = m["path"]
            jump_line = int(m["line"])
        else:
            label = self._src_listbox.get(idx)
            path = label.rsplit("  (", 1)[0]
            jump_line = None

        try:
            r = requests.get(f"{BRIDGE_URL}/source/file",
                             params={"path": path}, timeout=10)
        except Exception as e:
            self._src_path_lbl.config(text=f"read failed: {e}")
            return
        if r.status_code != 200:
            self._src_path_lbl.config(text=f"{path}  [HTTP {r.status_code}]")
            return
        d = r.json()
        self._src_path_lbl.config(
            text=f"{d['path']}   ({d['total_lines']} lines)"
            + (f"   → line {jump_line}" if jump_line else ""))
        self._src_viewer.config(state="normal")
        self._src_viewer.delete("1.0", "end")
        self._src_viewer.insert("end", "\n".join(d.get("lines", [])))
        # Highlight the jump line if we came from a search result.
        if jump_line:
            self._src_viewer.tag_configure("findhit", background="#3a4d80")
            self._src_viewer.tag_remove("findhit", "1.0", "end")
            self._src_viewer.tag_add("findhit",
                                     f"{jump_line}.0", f"{jump_line}.end")
            self._src_viewer.see(f"{jump_line}.0")
        else:
            self._src_viewer.yview_moveto(0.0)
        self._src_viewer.config(state="disabled")

    # ─────────────────────────────────────────────────────────────────────────
    # File Tree tab — folder hierarchy + every absolute path
    # ─────────────────────────────────────────────────────────────────────────

    def _build_tree_tab(self) -> tk.Frame:
        outer = tk.Frame(self._nb, bg=BG)

        hdr = tk.Frame(outer, bg=BG, padx=12, pady=8)
        hdr.pack(fill="x")
        tk.Label(hdr, text="File Tree (bridged project paths)",
                 font=HEADING, bg=BG, fg=TEXT).pack(side="left")
        self._tree_root_lbl = tk.Label(hdr, text="", font=MONOSM,
                                       bg=BG, fg=TEXTDIM)
        self._tree_root_lbl.pack(side="left", padx=14)
        _btn(hdr, "↻ Refresh", self._refresh_tree_tab,
             bg="#374151", fg="#e5e7eb", padx=8).pack(side="right")

        body = tk.Frame(outer, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self._tree_widget = ttk.Treeview(
            body, columns=("size", "lines", "abs"),
            show="tree headings")
        self._tree_widget.heading("#0", text="Path")
        self._tree_widget.heading("size", text="Size")
        self._tree_widget.heading("lines", text="Lines")
        self._tree_widget.heading("abs", text="Absolute path")
        self._tree_widget.column("#0", width=380, stretch=False)
        self._tree_widget.column("size", width=80, anchor="e", stretch=False)
        self._tree_widget.column("lines", width=80, anchor="e", stretch=False)
        self._tree_widget.column("abs", width=520, stretch=True)

        sb = ttk.Scrollbar(body, orient="vertical",
                           command=self._tree_widget.yview)
        self._tree_widget.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._tree_widget.pack(side="left", fill="both", expand=True)

        outer.after(900, self._refresh_tree_tab)
        return outer

    def _refresh_tree_tab(self):
        if not REQUESTS_OK:
            return
        try:
            r = requests.get(f"{BRIDGE_URL}/source/tree", timeout=10)
            if r.status_code != 200:
                self._tree_root_lbl.config(text=f"error: {r.status_code}")
                return
            data = r.json()
        except Exception as e:
            self._tree_root_lbl.config(text=f"refresh failed: {e}")
            return
        # Get project_root for absolute paths.
        try:
            lst = requests.get(f"{BRIDGE_URL}/source/list", timeout=5).json()
            root_abs = lst.get("project_root", "")
        except Exception:
            root_abs = ""
        self._tree_root_lbl.config(text=root_abs or "")

        self._tree_widget.delete(*self._tree_widget.get_children())

        def add_node(parent_id: str, node: dict):
            label = node["name"] + ("/" if node["type"] == "dir" else "")
            rel = node.get("path", "")
            abs_path = (str(Path(root_abs) / rel) if root_abs and rel
                        else root_abs)
            size = (f"{node['size']:,}" if node["type"] == "file"
                    and "size" in node else "")
            lines = (str(node["lines"]) if node["type"] == "file"
                     and "lines" in node else "")
            iid = self._tree_widget.insert(
                parent_id, "end", text=label,
                values=(size, lines, abs_path),
                open=(parent_id == ""))
            for child in node.get("children", []):
                add_node(iid, child)

        add_node("", data)

    # ─────────────────────────────────────────────────────────────────────────
    # How To Use Tab
    # ─────────────────────────────────────────────────────────────────────────

    def _build_howto_tab(self) -> tk.Frame:
        outer = tk.Frame(self._nb, bg=BG)

        pane = tk.PanedWindow(outer, orient="horizontal", bg=BG,
                              sashwidth=4, sashrelief="flat")
        pane.pack(fill="both", expand=True)

        # TOC list
        toc_frame = tk.Frame(pane, bg=BG2, width=230)
        toc_frame.pack_propagate(False)
        tk.Label(toc_frame, text="SECTIONS",
                 font=(_UI_FACE, 9, "bold"),
                 bg=BG2, fg=TEXTDIM, pady=8).pack(fill="x", padx=12)
        self._howto_listbox = tk.Listbox(
            toc_frame, bg=BG2, fg=TEXT,
            selectbackground=ACCENT, selectforeground="#000000",
            relief="flat", borderwidth=0, font=LABEL, activestyle="none")
        self._howto_listbox.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        for title, _ in HOW_TO_SECTIONS:
            short = title.split("—")[0].strip()
            self._howto_listbox.insert("end", f"  {short}")
        self._howto_listbox.bind("<<ListboxSelect>>", self._on_howto_select)
        pane.add(toc_frame, minsize=180)

        # Content
        right = tk.Frame(pane, bg=BG)
        self._howto_title_lbl = tk.Label(
            right, text="", font=HEADING, bg=BG, fg=ACCENT,
            anchor="w", wraplength=700, justify="left", padx=20, pady=10)
        self._howto_title_lbl.pack(fill="x")
        ttk.Separator(right).pack(fill="x", padx=20)
        self._howto_viewer = scrolledtext.ScrolledText(
            right, bg=BG, fg=TEXT, font=(_MONO_FACE, 11),
            relief="flat", state="disabled", wrap="word",
            padx=24, pady=14, cursor="arrow",
            insertbackground=BG, spacing1=2, spacing3=3)
        self._howto_viewer.pack(fill="both", expand=True)
        self._howto_viewer.tag_configure("code", foreground=GREEN,
                                          font=(_MONO_FACE, 10), background=BG2)
        self._howto_viewer.tag_configure("bullet", foreground=YELLOW)
        self._howto_viewer.tag_configure("dim", foreground=TEXTDIM)
        pane.add(right, minsize=400)

        self._howto_listbox.selection_set(0)
        self._show_howto_section(0)
        return outer

    def _on_howto_select(self, _=None):
        sel = self._howto_listbox.curselection()
        if sel:
            self._show_howto_section(sel[0])

    def _show_howto_section(self, index: int):
        if index >= len(HOW_TO_SECTIONS):
            return
        title, body = HOW_TO_SECTIONS[index]
        self._howto_title_lbl.config(text=title)
        v = self._howto_viewer
        v.config(state="normal")
        v.delete("1.0", "end")
        CODE_STARTS = (
            "import ", "from ", "logging.", "logger.", "LOG_PATH", "STATS_PATH",
            "STATE_PATH", "def ", "source ", "which ", "    ", "lsof",
        )
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith(("▸", "▶", "→")):
                v.insert("end", line + "\n", "bullet")
            elif stripped.startswith(("│", "├", "└", "┌", "┐", "┘", "┤", "─")):
                v.insert("end", line + "\n", "dim")
            elif any(line.startswith("    " + s) or line.startswith("  " + s)
                     for s in CODE_STARTS):
                v.insert("end", line + "\n", "code")
            else:
                v.insert("end", line + "\n")
        v.config(state="disabled")
        v.see("1.0")

    # ─────────────────────────────────────────────────────────────────────────
    # Profile management
    # ─────────────────────────────────────────────────────────────────────────

    def _refresh_profile_list(self):
        self._profile_listbox.delete(0, "end")
        for name, p in self._profiles.items():
            prefix = "▶ " if name == self._active_name else "  "
            self._profile_listbox.insert("end", f"{prefix}{p.display_name}")
            if name == self._active_name:
                self._profile_listbox.itemconfig("end", fg=GREEN)

    def _on_profile_select(self, _=None):
        sel = self._profile_listbox.curselection()
        if not sel:
            return
        names = list(self._profiles.keys())
        if sel[0] < len(names):
            self._select_profile(names[sel[0]])

    def _select_profile(self, name: str):
        if name not in self._profiles:
            return
        self._editing_name = name
        p = self._profiles[name]
        self._profile_name_lbl.config(text=p.display_name)
        for key, var in self._field_vars.items():
            var.set(getattr(p, key, "") or "")
        is_active = name == self._active_name
        self._active_badge.config(
            text="● Active" if is_active else "",
            fg=GREEN if is_active else BG)
        # Update folder label to match this profile's output path
        self._update_folder_label_for_profile(p)
        # Refresh the detected language/emitter line for this profile's root.
        self._refresh_detected_language()

    def _save_profile(self):
        name = getattr(self, "_editing_name", "custom")
        if not name:
            return
        data = {key: var.get() for key, var in self._field_vars.items()}
        data["name"] = name
        self._profiles[name] = ProgramProfile.from_dict(data)
        if not self._save_profiles_or_warn():
            return
        self._refresh_profile_list()
        self._set_status(f"Profile '{name}' saved.")
        if self._bridge_online and REQUESTS_OK:
            try:
                requests.post(f"{BRIDGE_URL}/profiles",
                              json=self._profiles[name].to_dict(), timeout=2)
            except Exception:
                pass

    def _new_profile(self):
        name = f"program_{int(time.time())}"
        self._profiles[name] = ProgramProfile(name=name, display_name="New Program")
        self._refresh_profile_list()
        self._select_profile(name)
        self._nb.select(self._tab_profile)

    def _delete_profile(self):
        name = getattr(self, "_editing_name", "")
        if not name or name in BUILTIN_PROFILES:
            messagebox.showwarning("Cannot Delete",
                                   "Built-in profiles cannot be deleted.")
            return
        if messagebox.askyesno("Delete", f"Delete profile '{name}'?"):
            removed = self._profiles.pop(name)
            if not self._save_profiles_or_warn():
                self._profiles[name] = removed      # keep memory == disk
                return
            self._editing_name = list(self._profiles.keys())[0]
            self._refresh_profile_list()
            self._select_profile(self._editing_name)

    def _set_active(self):
        name = getattr(self, "_editing_name", "")
        if not name or name not in self._profiles:
            return
        self._active_name = name
        self._refresh_profile_list()
        self._active_badge.config(text="● Active", fg=GREEN)
        self._set_status(f"Active: {self._profiles[name].display_name}")
        profile = self._profiles[name]

        # ── Tell bridge to switch collector to the new profile ────────────
        if self._bridge_online and REQUESTS_OK:
            try:
                requests.put(f"{BRIDGE_URL}/profiles/active",
                             json={"name": name}, timeout=2)
            except Exception:
                pass

        # ── Update screenshot output folder for this profile ─────────────
        # Must happen AFTER bridge switches so /status returns the new folder.
        # Also update locally from profile data so it works even offline.
        try:
            self._update_folder_label_for_profile(profile)
        except Exception:
            pass

        # ── Reload log panel immediately for new profile ──────────────────
        try:
            self._refresh_log()
        except Exception:
            pass

        # ── Reload Source Code + File Tree tabs for this project ──────────
        try:
            self._refresh_source_tab()
        except Exception:
            pass

        # ── Permissions tab: capture-opt-in state per project ────────────
        try:
            self._refresh_capture_toggle_label()
        except Exception:
            pass

    def _browse(self, key: str, is_file: bool):
        path = (filedialog.askopenfilename(title=f"Select {key}")
                if is_file else filedialog.askdirectory(title=f"Select {key}"))
        if path:
            self._field_vars[key].set(path)
            # If setting project_root, offer auto-detect
            if key == "project_root":
                if messagebox.askyesno("Auto-Detect",
                                       "Run auto-detect for this project root?"):
                    self._auto_detect()

    # ─────────────────────────────────────────────────────────────────────────
    # Bridge control
    # ─────────────────────────────────────────────────────────────────────────

    def _start_bridge(self):
        # Check if already running
        if self._bridge_online:
            self._set_status("Bridge is already online.")
            return
        if self._bridge_proc and self._bridge_proc.poll() is None:
            self._set_status("Bridge process already running — checking again…")
            return

        profile = self._profiles.get(self._active_name, ProgramProfile())
        project_root = profile.project_root or os.getcwd()

        # Resolve python executable — prefer a venv python inside AgentVision
        # itself (POSIX: .venv/bin/python3, Windows: .venv/Scripts/python.exe),
        # else fall back to the interpreter running the GUI.
        av_root   = Path(__file__).parent.parent.parent
        if platform_shim.IS_WINDOWS:
            venv_py = av_root / ".venv" / "Scripts" / "python.exe"
        else:
            venv_py = av_root / ".venv" / "bin" / "python3"
        python_exe = str(venv_py) if venv_py.exists() else sys.executable

        server_script = Path(__file__).parent.parent / "api" / "bridge_server.py"

        # --folder is the fallback save root used ONLY when a profile has no
        # project_root set. When project_root IS set, bridge uses it directly
        # via profile_output_folder(). Pass AV's own snapshots/ dir so a
        # misconfigured profile never lands inside another project's folder.
        av_snapshots = str(av_root / "snapshots")
        cmd = [
            python_exe,
            str(server_script),
            "--host",     "127.0.0.1",
            "--port",     "7771",
            "--profile",  self._active_name,
            "--folder",   av_snapshots,
            "--interval", str(self._shot_interval.get()),
        ]
        # If user unchecked "Capture on start", suppress auto-capture in bridge.
        if not self._capture_on_bridge_start.get():
            cmd.append("--no-autocapture")

        log_path = av_root / "bridge.log"
        try:
            log_f = open(log_path, "w")
            self._bridge_proc = subprocess.Popen(
                cmd,
                cwd=str(av_root),
                stdout=log_f,
                stderr=log_f,
            )
            self._set_status(f"Bridge starting (PID {self._bridge_proc.pid})… log: {log_path}")
        except Exception as e:
            messagebox.showerror("Bridge Error", str(e))

    def _stop_bridge(self):
        # Tell engine to stop capture first (graceful) — gives the engine a
        # chance to flush its current frame and exit cleanly. Best-effort.
        if self._bridge_online and REQUESTS_OK:
            try:
                requests.post(f"{BRIDGE_URL}/capture/stop", timeout=1)
            except Exception:
                pass
        if self._bridge_proc:
            self._bridge_proc.terminate()
            self._bridge_proc = None
        # Mirror the stop in GUI state so a later Start Bridge does NOT
        # auto-rearm capture (that path checks self._shot_running).
        self._shot_running = False
        self._shot_paused  = False
        try:
            self._btn_start.config(state="normal")
            self._btn_pause.config(state="disabled", text="⏸  Pause")
            self._btn_stop.config(state="disabled")
            self._shot_status_lbl.config(text="● Idle", fg=TEXTDIM)
        except Exception:
            pass
        self._set_bridge_status(False)
        self._set_status("Bridge stopped.")

    # ─────────────────────────────────────────────────────────────────────────
    # Polling loop
    # ─────────────────────────────────────────────────────────────────────────

    def _start_poll(self):
        # All HTTP calls run in a daemon thread so they never block Tkinter
        self._poll_tick()

    def _poll_tick(self):
        """Schedule one background poll every 2 s. Never blocks the UI thread."""
        self._check_bridge_process()
        self._update_shot_counter()
        threading.Thread(target=self._bg_poll, daemon=True).start()
        self._poll_after_id = self.after(2000, self._poll_tick)

    def _bg_poll(self):
        """Runs in background thread — does all HTTP, then posts results to UI."""
        online, save_folder = self._check_bridge_http()
        self.after(0, lambda o=online: self._set_bridge_status(o))
        if save_folder:
            self.after(0, lambda f=save_folder: self._set_output_folder(f))
        if online:
            self._bg_poll_live()

    def _check_bridge_http(self) -> tuple[bool, str]:
        if not REQUESTS_OK:
            return False, ""
        try:
            r = requests.get(f"{BRIDGE_URL}/status", timeout=2)
            if r.status_code == 200:
                data = r.json()
                return True, data.get("save_folder", "")
            return False, ""
        except Exception:
            return False, ""

    def _check_bridge_process(self):
        """Check if our spawned bridge process died unexpectedly (main thread)."""
        if self._bridge_proc and self._bridge_proc.poll() is not None:
            code = self._bridge_proc.returncode
            av_root  = Path(__file__).parent.parent.parent
            log_path = av_root / "bridge.log"
            err = ""
            if log_path.exists():
                try:
                    lines = log_path.read_text().splitlines()
                    err = " | " + lines[-1] if lines else ""
                except Exception:
                    pass
            self._set_status(f"Bridge exited (code {code}){err} — check {log_path}")
            self._bridge_proc = None
            self._set_bridge_status(False)

    # kept for compat (debug tab refresh calls it by name)
    def _poll_bridge_status(self):
        pass

    def _poll_live_data(self):
        pass

    def _bg_poll_live(self):
        """Background thread: fetch live data and push to UI via after(0,...)."""
        if not REQUESTS_OK:
            return
        try:
            r = requests.get(f"{BRIDGE_URL}/latest", timeout=2)
            if r.status_code == 200:
                data = r.json()
                self.after(0, lambda d=data: self._update_live_tab(d))
        except Exception:
            pass
        try:
            r = requests.get(f"{BRIDGE_URL}/program/status", timeout=1)
            if r.status_code == 200:
                ps = r.json()
                self.after(0, lambda p=ps: self._apply_program_status(p))
        except Exception:
            pass
        # Capture engine status → update shot counter + status dot
        try:
            r = requests.get(f"{BRIDGE_URL}/capture/status", timeout=1)
            if r.status_code == 200:
                cs = r.json()
                self.after(0, lambda c=cs: self._apply_capture_status(c))
        except Exception:
            pass
        # Tab-specific refreshes — post to UI thread
        try:
            tab = self._nb.index("current")
        except Exception:
            tab = -1
        if tab == 3:
            self.after(0, self._refresh_stats)
        if tab == 4:
            self.after(0, self._refresh_debug_log)

    def _count_shots_on_disk(self) -> int:
        """Count PNG frames actually present in the output folder.
        This is the ground truth — the bridge's in-memory counter can be
        stale (e.g. frames deleted while bridge is running, or bridge
        hydrated old files on startup that were since cleared)."""
        try:
            folder = self._output_folder
            if not folder:
                return 0
            return sum(1 for f in Path(folder).iterdir()
                       if f.suffix == ".png" and f.is_file())
        except Exception:
            return 0

    def _apply_capture_status(self, cs: dict):
        """Update screenshot bar from live capture engine state (UI thread)."""
        capturing  = cs.get("capturing", False)
        last_err   = cs.get("last_error", "")
        # Always count from disk — not from bridge's in-memory counter which
        # can be stale after a delete or bridge restart.
        count = self._count_shots_on_disk()
        self._shot_count = count
        self._shot_counter_lbl.config(text=f"Shots: {count}")
        if capturing:
            self._shot_status_lbl.config(text=f"● Capturing", fg=GREEN)
        elif cs.get("engine_running"):
            self._shot_status_lbl.config(text="● Watching…", fg=ORANGE)
        else:
            self._shot_status_lbl.config(text="● Idle", fg=TEXTDIM)
        if last_err:
            self._shot_status_lbl.config(text=f"⚠ {last_err[:40]}", fg=RED)

    def _apply_program_status(self, ps: dict):
        """Update program status cards — must be called on the UI thread."""
        running = ps.get("running", False)
        _, clbl   = self._card_running
        _, cpulbl = self._card_cpu
        _, ramlbl = self._card_ram
        clbl.config(text="RUNNING" if running else "STOPPED",
                    fg=GREEN if running else RED)
        cpulbl.config(
            text=f"{ps.get('cpu_percent', 0):.0f}%",
            fg=RED if ps.get("cpu_percent", 0) > 80 else TEXT)
        ramlbl.config(text=f"{ps.get('ram_gb', 0):.1f}GB")

    def _update_shot_counter(self):
        if self._shot_running and not self._shot_paused:
            self._shot_count += 1
            self._shot_counter_lbl.config(text=f"Shots: {self._shot_count}")
            _, shots_lbl = self._card_shots
            shots_lbl.config(text=str(self._shot_count), fg=GREEN)

    def _update_live_tab(self, data: dict):
        conf  = data.get("agent_eval", {}).get("confidence", 0)
        self._conf_var.set(conf * 100)
        pct   = int(conf * 100)
        color = GREEN if pct > 75 else (ORANGE if pct > 45 else RED)
        self._conf_lbl.config(text=f"{pct}%", fg=color)

        action = data.get("agent_eval", {}).get("next_action", "—")
        self._next_action_lbl.config(text=action[:90])

        _, frames_lbl = self._card_frames
        frames_lbl.config(text=str(data.get("sequence", 0)))

        curated = {k: data.get(k) for k in
                   ("sequence", "timestamp", "perf", "git", "tests",
                    "error", "anomaly", "agent_eval")}
        curated["activity_log"] = data.get("activity_log", [])[-4:]

        self._frame_viewer.config(state="normal")
        self._frame_viewer.delete("1.0", "end")
        self._frame_viewer.insert("1.0", json.dumps(curated, indent=2))
        self._frame_viewer.config(state="disabled")

    def _refresh_stats(self):
        for w in self._stats_frame.winfo_children():
            w.destroy()
        stats = {}
        if self._bridge_online and REQUESTS_OK:
            try:
                r = requests.get(f"{BRIDGE_URL}/program/stats", timeout=2)
                if r.status_code == 200:
                    stats = r.json().get("stats", {})
            except Exception:
                pass
        if not stats:
            from connectors.program_connector import ProgramDataReader
            profile = self._profiles.get(self._active_name, ProgramProfile())
            stats   = ProgramDataReader(profile).latest_stats()
        if not stats:
            tk.Label(self._stats_frame, text="No stats available.",
                     font=LABEL, bg=BG, fg=TEXTDIM).pack(pady=20)
            return
        for key, val in stats.items():
            row = tk.Frame(self._stats_frame, bg=BG2, padx=16, pady=7)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=key, font=LABEL, bg=BG2,
                     fg=TEXTDIM, width=24, anchor="w").pack(side="left")
            tk.Label(row, text=str(val),
                     font=(_UI_FACE, 12, "bold"),
                     bg=BG2, fg=TEXT).pack(side="left")

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _set_bridge_status(self, online: bool):
        was_offline = not self._bridge_online
        self._bridge_online = online
        self._bridge_dot.config(fg=GREEN if online else TEXTDIM)
        self._bridge_lbl.config(
            text=" Bridge online" if online else " Bridge offline",
            fg=GREEN if online else TEXTDIM)
        self._log_live_dot.config(
            text="● Live" if online else "○ Offline",
            fg=GREEN if online else TEXTDIM)
        # First time bridge comes online — arm the capture engine automatically
        if online and was_offline and self._shot_running and REQUESTS_OK:
            threading.Thread(target=self._arm_capture, daemon=True).start()
        # And auto-start the input recorder if the user previously enabled it.
        if online and was_offline:
            try:
                self._autostart_if_enabled()
            except Exception:
                pass

    def _save_profiles_or_warn(self) -> bool:
        """Persist profiles.json, surfacing a refusal instead of swallowing it.

        save_profiles now refuses when the last load of profiles.json did not
        fully succeed — because writing then would commit an incomplete view of a
        hand-authored file over the real one. That refusal has to reach the user;
        a silent failure here is how you find out at the next restart that your
        project roots, log_sources and notes are gone.
        """
        try:
            save_profiles(self._profiles)
            return True
        except Exception as e:
            messagebox.showerror(
                "Profiles NOT saved",
                f"{e}\n\nYour change is applied for this session only. "
                f"profiles.json on disk was left exactly as it was.")
            self._set_status(f"Profiles NOT saved: {e}")
            return False

    def _update_folder_label_for_profile(self, profile: ProgramProfile):
        """Recompute the output folder path from profile fields and update label."""
        base = profile.project_root or os.path.expanduser("~")
        folder = str(profile_output_folder(profile, Path(base)))
        self._set_output_folder(folder)

    def _set_output_folder(self, folder: str):
        """Update the clickable folder label (UI thread)."""
        if not folder:
            return
        self._output_folder = folder
        p = Path(folder)
        display = "…/" + "/".join(p.parts[-3:]) if len(p.parts) > 3 else folder
        self._folder_lbl.config(text=f"📁 {display}", fg=ACCENT)

    def _open_output_folder(self, _=None):
        """Open the output folder in the OS file manager when clicked."""
        folder = self._output_folder
        if not folder:
            profile = self._profiles.get(self._active_name, ProgramProfile())
            folder = profile.project_root or os.path.expanduser("~")
        p = Path(folder)
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
        platform_shim.reveal_path(p)

    def _clear_output_folder(self):
        """Delete the capture output in this profile's folder.

        This dialog used to promise "nothing is permanently lost — every clear
        creates a numbered checkpoint". That was not true, and not only at the
        edges: the checkpoint collects <project_root>/snapshots/, while the
        bridge writes frames to <project_root>/agentvision/<profile>/. Those are
        different directories, and the second one is what this button deletes.
        On this machine that was 35,746 files / 1.0 GB of frames, sidecars and
        diffs, none of which any checkpoint contained.

        The honest fix is not to copy a gigabyte into a tarball on the same
        filesystem before deleting it — that clears nothing. It is to say
        plainly which files are archived and which are going for good, with
        counts the user can check, and let them decide. So the dialog below
        computes both numbers from checkpoint_manager's own collector rather
        than asserting a guarantee.
        """
        folder = self._output_folder
        profile = self._profiles.get(self._active_name, ProgramProfile())
        if not folder:
            root = profile.project_root or ""
            if root:
                folder = str(profile_output_folder(profile, Path(root)))
        if not folder or not Path(folder).exists():
            messagebox.showwarning("Clear Data", "Output folder not found — start the bridge first.")
            return

        p = Path(folder)
        exts = {".png", ".json", ".log", ".diff"}
        files = [f for f in p.iterdir() if f.is_file() and f.suffix in exts]

        if not files:
            messagebox.showinfo("Clear Data", "Nothing to delete — folder is already empty.")
            return

        # Which of these files does the checkpoint actually cover? Ask the
        # collector instead of assuming, so this stays correct if its scope
        # ever changes.
        from utils.checkpoint_manager import (save_checkpoint,
                                              collected_runtime_paths)
        covered = collected_runtime_paths(profile)
        archived = [f for f in files if f.resolve() in covered]
        forever = [f for f in files if f.resolve() not in covered]

        lines = [f"Clear the capture output for "
                 f"{getattr(profile, 'name', 'unknown')}?", "", f"Folder: {p}", ""]
        if forever:
            lines += [f"{len(forever)} file(s) will be PERMANENTLY DELETED.",
                      "These are capture frames, sidecars and diffs; they are",
                      "not part of a checkpoint and cannot be restored.", ""]
        if archived:
            lines += [f"{len(archived)} file(s) are archived to",
                      f"  checkpoints/{getattr(profile, 'name', 'unknown')}/"
                      f"checkpoint_NNNNN.tar.gz",
                      "first, and are deleted only after the archive has been",
                      "re-opened and verified to contain them.", ""]
        lines += ["A checkpoint of this profile's logs, config and source is",
                  "written either way."]
        # Name what genuinely survives. This clear is a non-recursive iterdir, so
        # subfolders are untouched — and _archive/ is the compressed history the
        # retention layer keeps precisely so a day of capture stays reviewable
        # after the full-resolution frames are reclaimed.
        try:
            arch = p / "_archive"
            n_arch = sum(1 for f in arch.rglob("*") if f.is_file()) \
                if arch.is_dir() else 0
        except Exception:
            n_arch = 0
        if n_arch:
            lines += ["",
                      f"Kept: the {n_arch} compressed frame(s) in _archive/ —",
                      "subfolders are not touched by this clear."]
        if not messagebox.askyesno("Clear Data", "\n".join(lines)):
            return

        # Save the checkpoint FIRST. save_checkpoint removes the files it can
        # PROVE are inside the finished archive and leaves the rest alone, so
        # anything inside the standard archive set is handled there.
        ckpt_path = None
        try:
            ckpt_path = save_checkpoint(profile, verbose=True)
        except Exception as e:
            if not messagebox.askyesno(
                    "Checkpoint Failed",
                    f"Could not save checkpoint:\n{e}\n\n"
                    f"Delete {len(files)} file(s) anyway? They will NOT be "
                    f"recoverable."):
                return

        # Now the folder itself. Files the checkpoint covered are already gone;
        # the rest are the ones the dialog said would be deleted for good.
        deleted, failed = 0, 0
        for f in files:
            if not f.exists():
                deleted += 1  # already handled by the checkpoint
                continue
            try:
                f.unlink()
                deleted += 1
            except Exception:
                failed += 1

        # Reset the bridge's in-memory frame counter so the poller stops
        # overwriting "Shots: 0" with the stale pre-delete count.
        try:
            requests.post(f"{BRIDGE_URL}/capture/reset-counter", timeout=3)
        except Exception:
            pass  # bridge may not be running — local reset is enough

        # Reset all local shot-count state so the label stays at 0.
        self._shot_count = 0
        self._shot_counter_lbl.config(text="Shots: 0")
        self._shot_status_lbl.config(text="● Idle", fg=TEXTDIM)

        if ckpt_path:
            msg = f"✓ Checkpoint saved → {Path(ckpt_path).name}  |  Deleted {deleted} files."
        else:
            msg = f"Deleted {deleted} files (no checkpoint)."
        if failed:
            msg += f" ({failed} could not be deleted.)"
        self._set_status(msg)

    def _arm_capture(self):
        """Send capture/start to bridge with current interval (background thread).
        Passes force=true so the human Start-Bridge path is never blocked by the
        first-start preflight nudge (the AI-facing av_capture_start still gets the
        preflight_required prompt when force is absent)."""
        try:
            requests.post(f"{BRIDGE_URL}/capture/start",
                          json={"interval": self._shot_interval.get(),
                                "force": True},
                          timeout=3)
        except Exception:
            pass

    def _set_status(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._status_var.set(f"[{ts}]  {msg}")

    def on_close(self):
        self._log_tail_running = False
        self._stop_bridge()
        if self._poll_after_id:
            self.after_cancel(self._poll_after_id)
        # Archive every profile before closing. ARCHIVE ONLY — closing a window
        # is not a request to delete anything, and this path has no confirmation
        # dialog, no undo and no report the user ever sees. It used to unlink
        # every file it collected (including everything under <root>/log/stats
        # and log/crashes, which the debugged program writes itself) on the
        # strength of a tar.add nobody checked.
        def _do_save():
            try:
                save_all_checkpoints(self._profiles, verbose=True,
                                     delete_originals=False)
            except Exception as e:
                print(f"[checkpoint] close save failed: {e}")
        t = threading.Thread(target=_do_save, daemon=False)
        t.start()
        t.join(timeout=30)  # wait up to 30s for compression
        self.destroy()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = AgentVisionApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
