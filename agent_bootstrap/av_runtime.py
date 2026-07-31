"""
AgentVision in-process runtime hooks.

Installed into ANY Python program via sitecustomize.py (PYTHONPATH trick).
The user's project is never modified — this module is loaded by the
interpreter at startup and silently captures everything to AgentVision's
JSONL sink in the project's log/ directory.

Captures (zero code changes required of the target program):
  - Uncaught exceptions          → sys.excepthook
  - Thread exceptions            → threading.excepthook
  - Unraisable shutdown errors   → sys.unraisablehook
  - All stdlib `logging` calls   → root handler
  - stdout / stderr              → TeeStream wrapper
  - Process start / exit         → atexit + emit on import

Every record uses the same schema as bot_action_logger.py:
    {ts, ts_ms, category, source, state, run_id, trace_id, frame_seq,
     coords, data}

Idempotent: re-importing in the same process is a no-op (env-var guard).
"""
from __future__ import annotations

import atexit
import datetime as _dt
import json
import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, TextIO

# ── Configuration via environment ─────────────────────────────────────────────
# AGENTVISION_PROJECT  — root dir of the bridged project (required)
# AGENTVISION_LOG_DIR  — override for log dir (default: <project>/log)
# AGENTVISION_HOOKED   — set to "1" once installed; prevents double-install

_GUARD = "AGENTVISION_HOOKED"
_LOCK = threading.Lock()

#: Which hooks the AGENT selected in its bridge plan, as a comma-separated list of
#: emitter ids (see bridge_plan._emitter_options). The installer writes this into
#: the generated sitecustomize / run_env.
#:
#: Before this existed, install_all_hooks() armed EVERY hook unconditionally, so a
#: plan that deliberately selected only `lifecycle` still got stdout teeing, a
#: logging handler and the swallowed-exception monitor. The gate collected a
#: decision and the runtime ignored it, which made the plan file a description of
#: something that never happened.
#:
#: UNSET means "all hooks" — required for backward compatibility with projects
#: bridged before per-hook selection existed, and for a bare `agentvision run`.
_HOOKS_ENV = "AGENTVISION_HOOKS"

#: emitter id -> the hook it actually controls in install_all_hooks().
_HOOK_IDS = ("stdout_tee", "logging_bridge", "uncaught_exceptions",
             "swallowed_exceptions", "lifecycle")


def _selected_hooks() -> set[str]:
    """The hook ids to arm. Empty env or unset -> every hook."""
    raw = (os.environ.get(_HOOKS_ENV) or "").strip()
    if not raw:
        return set(_HOOK_IDS)
    want = {p.strip() for p in raw.split(",") if p.strip()}
    # An unrecognised id must never silently disable everything: fall back to all
    # rather than leave a program with no instrumentation because of a typo.
    known = want & set(_HOOK_IDS)
    return known or set(_HOOK_IDS)


def _hook_enabled(name: str) -> bool:
    return name in _selected_hooks()


def _shield_basicconfig() -> None:
    """Keep the target program's own logging.basicConfig() working.

    basicConfig() is documented as a no-op when the root logger already has a
    handler, and this runtime installs one at INTERPRETER STARTUP — before the
    program's own call runs. Left alone that silently steals the program's
    console logging: a script whose WARNING/ERROR lines normally reach the
    terminal prints NOTHING under AgentVision. Measured, not theorised.

    That failure is the worst kind for this project. The human sees a silent
    program and reasonably concludes it never logged, when in fact AgentVision
    captured every record and merely hid it — a false read manufactured by the
    observer. So: hide our handler for the duration of the call, let basicConfig
    configure the root it expects, then put ours back.

    Also narrows the level bump. Raising root WARNING->INFO is needed to capture
    INFO at all, but doing it when the program passed an explicit level=
    overrides a deliberate choice, and the program's own handlers would then
    print lines it had chosen to suppress. An explicit level is respected and
    capture simply follows it.
    """
    if getattr(logging.basicConfig, "_av_shielded", False):
        return
    _orig = logging.basicConfig

    def basicConfig(*args, **kwargs):          # noqa: N802 - mirrors stdlib name
        root = logging.getLogger()
        mine = [h for h in root.handlers if isinstance(h, _JSONLHandler)]
        for h in mine:
            root.removeHandler(h)
        try:
            return _orig(*args, **kwargs)
        finally:
            # force=True clears handlers outright, so re-add rather than assume.
            for h in mine:
                if h not in root.handlers:
                    root.addHandler(h)
            if kwargs.get("level") is None and root.level == logging.WARNING:
                root.setLevel(logging.INFO)

    basicConfig._av_shielded = True            # type: ignore[attr-defined]
    basicConfig._av_original = _orig           # type: ignore[attr-defined]
    logging.basicConfig = basicConfig          # type: ignore[assignment]
_SINK_PATH: Path | None = None
#: Human-readable mirror of the JSONL — the "text" sink the manifest declares.
_TEXT_PATH: Path | None = None
#: category -> level word, so the text line carries a severity a log adapter can
#: read back. Mirrors the categories this runtime actually emits.
_CATEGORY_LEVEL = {
    "exception": "ERROR", "error": "ERROR", "warn": "WARN",
    "stderr": "WARN", "stdout": "INFO", "process": "INFO",
    "file": "INFO", "log": "INFO", "metric": "INFO",
}
_FRAME_SEQ_PATH: Path | None = None
_FRAME_SEQ_CACHE: tuple[float, int | None] = (0.0, None)
_RUN_ID = f"av-{int(time.time() * 1000)}-{os.getpid()}"


# ── Time helpers (mirror shared/clock.py — no import to avoid sys.path games) ─

def _now() -> tuple[str, float]:
    n = _dt.datetime.now(_dt.timezone.utc)
    iso = n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"
    return iso, n.timestamp() * 1000.0


# ── frame_seq correlation (read-only side; bridge_server writes the file) ────

def _read_frame_seq() -> int | None:
    global _FRAME_SEQ_CACHE
    if _FRAME_SEQ_PATH is None:
        return None
    now = time.time()
    last_t, last_v = _FRAME_SEQ_CACHE
    if now - last_t < 1.0:
        return last_v
    try:
        v = int(_FRAME_SEQ_PATH.read_text().strip())
    except Exception:
        v = None
    _FRAME_SEQ_CACHE = (now, v)
    return v


# ── JSONL emit ────────────────────────────────────────────────────────────────

def _emit(category: str, source: str, data: dict[str, Any]) -> None:
    if _SINK_PATH is None:
        return
    iso, ms = _now()
    record = {
        "ts": iso,
        "ts_ms": ms,
        "category": category,
        "source": source,
        "state": None,
        "run_id": _RUN_ID,
        "trace_id": None,
        "frame_seq": _read_frame_seq(),
        "coords": None,
        "data": data,
    }
    try:
        line = json.dumps(record, default=str, ensure_ascii=False)
    except Exception as e:
        line = json.dumps({
            "ts": iso, "ts_ms": ms, "category": "error",
            "source": "av.bootstrap.emit_fail", "state": None,
            "run_id": _RUN_ID, "trace_id": None, "frame_seq": None,
            "coords": None,
            "data": {"reason": "json_encode_failed", "error": str(e)},
        })
    with _LOCK:
        try:
            with _SINK_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            # Last resort — never crash the host program because logging died.
            pass
        # ── The TEXT sink, which nothing used to write ─────────────────────
        # The installer creates agentvision/log.txt and the manifest declares it
        # as the "text" sink, but no emitter ever wrote a byte to it. So every
        # auto-installed project got a permanently empty file that the GUI's
        # "Bot Log" pane reads, that any generated profile watches, and that the
        # bridge's own staleness detector then flags forever — AgentVision
        # nagging about a dead file it created itself.
        #
        # A human-readable mirror is worth having on its own terms: the JSONL is
        # for the agent, this is for a person tailing a file.
        if _TEXT_PATH is not None:
            try:
                msg = data.get("message") or data.get("name") or ""
                if not msg and data:
                    msg = " ".join(f"{k}={v}" for k, v in list(data.items())[:4])
                lvl = str(data.get("level") or _CATEGORY_LEVEL.get(category, "INFO"))
                with _TEXT_PATH.open("a", encoding="utf-8") as f:
                    f.write(f"{iso} [{lvl}] [{source}] {str(msg)[:2000]}\n")
            except Exception:
                pass


# ── Hook 1: stdout / stderr Tee ───────────────────────────────────────────────

class _TeeStream:
    """Pass-through writer that mirrors writes to AgentVision's JSONL sink.

    Wraps the original stream so terminal output is preserved AND captured.
    """

    def __init__(self, original: TextIO, channel: str):
        self._orig = original
        self._channel = channel  # "stdout" or "stderr"
        self._buf = ""

    def write(self, s: str) -> int:
        try:
            n = self._orig.write(s)
        except Exception:
            n = len(s)
        self._buf += s
        # Emit per-line so JSONL records are atomic units.
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line:
                _emit(self._channel, f"av.bootstrap.{self._channel}",
                      {"text": line})
        return n

    def flush(self) -> None:
        try:
            self._orig.flush()
        except Exception:
            pass
        if self._buf:
            _emit(self._channel, f"av.bootstrap.{self._channel}",
                  {"text": self._buf})
            self._buf = ""

    # Delegate everything else (isatty, fileno, encoding, etc.) so libraries
    # that introspect sys.stdout don't get surprised.
    def __getattr__(self, name: str) -> Any:
        return getattr(self._orig, name)


# ── Hook 2: logging ───────────────────────────────────────────────────────────

class _JSONLHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        # Map the Python log LEVEL onto the unified `category` so ERROR/CRITICAL
        # logs trip AgentVision's failure detection the SAME way an ERROR line
        # from any other language does (universal consistency). data.level keeps
        # the exact original level.
        if record.levelno >= logging.ERROR:
            cat = "error"
        elif record.levelno >= logging.WARNING:
            cat = "warn"
        else:
            cat = "log"
        _emit(cat, f"av.bootstrap.log.{record.name}", {
            "level": record.levelname,
            "logger": record.name,
            "message": msg,
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
        })


# ── Hook 3, 4, 5: exception hooks ─────────────────────────────────────────────

# ── Hook: SWALLOWED exceptions ────────────────────────────────────────────────
# The hidden-bug class that matters most. An uncaught exception is loud — the
# excepthook above catches it and the process dies. But this:
#
#     try:
#         risky()
#     except Exception:
#         pass
#
# fails silently forever, and no log, no stderr and no excepthook will ever
# mention it. A debug system that only reports crashes misses the bugs that
# never crash, which are the ones that survive in code for years.
#
# CPython 3.12+ can see them: sys.monitoring fires EXCEPTION_HANDLED when an
# exception is caught by a handler.
#
# The reason this is not simply "subscribe and emit" is VOLUME. Normal Python
# raises-and-catches constantly inside libraries — retries, feature probes,
# optional imports — so an unfiltered feed would bury the program's own bugs in
# other people's control flow and cost real overhead. Two filters make it useful:
#
#   1. SCOPE to the project's own files. urllib3's retry logic is not the user's
#      bug; their own swallowed KeyError is.
#   2. DEDUPE by (type, file, line) with counts. A swallow inside a loop happens
#      thousands of times and is ONE defect; emitting it thousands of times would
#      be the same mistake as the 21,982 identical log lines found elsewhere in
#      this project.
_SWALLOW_SEEN: dict[tuple, int] = {}
#: key -> "file:function" of the HANDLER. Kept beside the counts because the raise
#: site is often inside a library (a swallowed JSONDecodeError points at CPython's
#: decoder.py, not at the caller), and the line the developer can actually change
#: is the one that hid it. Both get reported.
_SWALLOW_HANDLER: dict[tuple, str] = {}
_SWALLOW_CAP = int(os.environ.get("AGENTVISION_SWALLOW_CAP", "200") or 200)
_MON_TOOL_ID = 4          # 0-5 are free for tools; 4 avoids debugger/coverage ids


def _install_swallowed_exception_hook(project_root: Path) -> bool:
    """Report exceptions the program CATCHES and hides. Returns True if armed."""
    if os.environ.get("AGENTVISION_CATCH_SWALLOWED", "1") in ("0", "", "false"):
        return False
    mon = getattr(sys, "monitoring", None)
    if mon is None or not hasattr(mon.events, "EXCEPTION_HANDLED"):
        return False                      # < 3.12: nothing to do, stay silent

    root = str(project_root)

    def _on_handled(code, instruction_offset, exception):
        try:
            tb = getattr(exception, "__traceback__", None)
            # Walk to the INNERMOST frame: that is where it was actually raised,
            # which is the line a developer needs, not where it was caught.
            raise_file, raise_line = "", 0
            while tb is not None:
                raise_file = tb.tb_frame.f_code.co_filename
                raise_line = tb.tb_lineno
                tb = tb.tb_next
            # Scope filter: the raise site OR the handler must be project code.
            handler_file = getattr(code, "co_filename", "")
            if not (raise_file.startswith(root) or handler_file.startswith(root)):
                return
            # Never report AgentVision's own machinery.
            if "agent_bootstrap" in raise_file or "agent_bootstrap" in handler_file:
                return
            etype = type(exception).__name__
            key = (etype, raise_file, raise_line)
            n = _SWALLOW_SEEN.get(key, 0) + 1
            _SWALLOW_SEEN[key] = n
            _SWALLOW_HANDLER[key] = (
                f"{handler_file}:{getattr(code, 'co_name', '?')}")
            # First occurrence, then a decaying schedule — enough to show it is
            # recurring without one bad line dominating the log.
            if n not in (1, 10, 100, 1000) and n % 5000:
                return
            if len(_SWALLOW_SEEN) > _SWALLOW_CAP and n != 1:
                return
            _emit("warn", "av.bootstrap.swallowed", {
                "message": f"{etype} raised and silently handled: {exception}",
                "type": etype,
                "exception": str(exception)[:500],
                "raised_at": f"{raise_file}:{raise_line}",
                "handled_in": f"{handler_file}:{getattr(code, 'co_name', '?')}",
                "occurrences": n,
                "note": ("this exception never reaches stderr or the excepthook — "
                         "it is caught and hidden by the program itself"),
            })
        except Exception:
            pass          # a diagnostic hook must never break the host program

    try:
        mon.use_tool_id(_MON_TOOL_ID, "agentvision")
        mon.register_callback(_MON_TOOL_ID, mon.events.EXCEPTION_HANDLED, _on_handled)
        mon.set_events(_MON_TOOL_ID, mon.events.EXCEPTION_HANDLED)
        return True
    except Exception:
        # Another tool may already hold the id (a debugger, coverage). Not fatal.
        return False


def _install_exception_hooks() -> None:
    prev_excepthook = sys.excepthook

    def _hook(exc_type, exc, tb):
        try:
            _emit("exception", "av.bootstrap.excepthook", {
                "type": getattr(exc_type, "__name__", str(exc_type)),
                "message": str(exc),
                "traceback": "".join(
                    traceback.format_exception(exc_type, exc, tb)),
            })
        finally:
            prev_excepthook(exc_type, exc, tb)

    sys.excepthook = _hook

    prev_thread_hook = getattr(threading, "excepthook", None)

    def _thread_hook(args):
        try:
            _emit("exception", "av.bootstrap.thread_excepthook", {
                "type": getattr(args.exc_type, "__name__", str(args.exc_type)),
                "message": str(args.exc_value),
                "thread": getattr(args.thread, "name", "?"),
                "traceback": "".join(traceback.format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback)),
            })
        finally:
            if prev_thread_hook is not None:
                try:
                    prev_thread_hook(args)
                except Exception:
                    pass

    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_hook

    prev_unraisable = getattr(sys, "unraisablehook", None)

    def _unraisable(unraisable):
        try:
            exc = unraisable.exc_value
            tb = unraisable.exc_traceback
            _emit("exception", "av.bootstrap.unraisablehook", {
                "type": getattr(unraisable.exc_type, "__name__",
                                str(unraisable.exc_type)),
                "message": str(exc),
                "traceback": "".join(traceback.format_exception(
                    unraisable.exc_type, exc, tb)) if tb else "",
                "object": repr(unraisable.object)[:200],
            })
        finally:
            if prev_unraisable is not None:
                try:
                    prev_unraisable(unraisable)
                except Exception:
                    pass

    if hasattr(sys, "unraisablehook"):
        sys.unraisablehook = _unraisable


# ── Public install ────────────────────────────────────────────────────────────

def install_all_hooks() -> bool:
    """Install every in-process hook. Idempotent — safe to call repeatedly.

    Returns True if hooks were installed in this call, False if already
    installed (or no project configured).
    """
    global _SINK_PATH, _FRAME_SEQ_PATH, _TEXT_PATH

    if os.environ.get(_GUARD) == "1":
        return False

    project = os.environ.get("AGENTVISION_PROJECT")
    if not project:
        # No project set — bootstrap loaded but nothing to do. Stay silent.
        return False

    project_path = Path(project).expanduser().resolve()
    log_dir = Path(os.environ.get("AGENTVISION_LOG_DIR")
                   or (project_path / "log")).expanduser().resolve()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return False

    _SINK_PATH = log_dir / "actions.jsonl"
    # Same directory, same name the installer/manifest already use.
    _TEXT_PATH = log_dir / "log.txt"
    _FRAME_SEQ_PATH = log_dir / ".av_frame_seq"

    # Mark guard BEFORE installing so any re-entry during install short-circuits.
    os.environ[_GUARD] = "1"

    # Each hook is armed only if the agent's plan selected it. See _HOOKS_ENV:
    # unset means all, so nothing changes for projects bridged before this existed.
    _want = _selected_hooks()
    _armed: list[str] = []

    # 1. stdout / stderr Tee
    if "stdout_tee" in _want:
        try:
            sys.stdout = _TeeStream(sys.stdout, "stdout")
            sys.stderr = _TeeStream(sys.stderr, "stderr")
            _armed.append("stdout_tee")
        except Exception:
            pass

    # 2. logging root handler — only attach if no AgentVision handler present.
    if "logging_bridge" in _want:
        try:
            root = logging.getLogger()
            if not any(isinstance(h, _JSONLHandler) for h in root.handlers):
                h = _JSONLHandler()
                h.setLevel(logging.DEBUG)
                root.addHandler(h)
                if root.level == logging.WARNING:  # default — too quiet for capture
                    root.setLevel(logging.INFO)
            # MUST come after addHandler: the shield exists precisely because our
            # handler is now the thing that would neuter the program's own call.
            _shield_basicconfig()
            _armed.append("logging_bridge")
        except Exception:
            pass

    # 3-5. exception hooks
    if "uncaught_exceptions" in _want:
        try:
            _install_exception_hooks()
            _armed.append("uncaught_exceptions")
        except Exception:
            pass

    # 5b. SWALLOWED exceptions — the ones that never reach any of the hooks above.
    _swallow_armed = False
    if "swallowed_exceptions" in _want:
        try:
            _swallow_armed = _install_swallowed_exception_hook(project_path)
            if _swallow_armed:
                _armed.append("swallowed_exceptions")
        except Exception:
            pass

    # 6. process start + atexit. Emitted even when `lifecycle` was not selected:
    # this record is also how the agent learns WHICH hooks are live, and losing
    # that would trade one guess for another. Only the atexit half is gated.
    if "lifecycle" in _want:
        _armed.append("lifecycle")
    _emit("process", "av.bootstrap.start", {
        "pid": os.getpid(),
        "argv": sys.argv,
        "python": sys.version.split()[0],
        # Stated explicitly: on <3.12, or when another tool holds the monitoring
        # id, swallowed exceptions are NOT observable and the agent must not
        # assume silence means none happened.
        "catches_swallowed_exceptions": _swallow_armed,
        # What the plan asked for vs what is actually live. A selected hook can
        # still fail to arm (swallowed_exceptions on Python < 3.12), and the agent
        # must be able to see that rather than infer it from silence.
        "hooks_requested": sorted(_want),
        "hooks_armed": sorted(set(_armed)),
        "hooks_selection_source": ("plan" if os.environ.get(_HOOKS_ENV)
                                   else "default (all)"),
        "cwd": os.getcwd(),
        "project": str(project_path),
    })

    def _on_exit() -> None:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        # TRUE totals for swallowed exceptions.
        # The live records are de-duplicated (emitted at occurrence 1, 10, 100…),
        # so a defect that fired 3 times reports "occurrences: 1" — the count AT
        # EMISSION, which understates it. Without this summary the agent would
        # read that as "happened once" and mis-rank the defect. Same rule applied
        # everywhere else here: reduce volume, never misreport the count.
        try:
            if _SWALLOW_SEEN:
                top = sorted(_SWALLOW_SEEN.items(), key=lambda kv: -kv[1])[:20]
                _emit("warn", "av.bootstrap.swallowed_summary", {
                    "message": (f"{sum(_SWALLOW_SEEN.values())} exception(s) were "
                                f"raised and silently handled across "
                                f"{len(_SWALLOW_SEEN)} distinct site(s)"),
                    "total_occurrences": sum(_SWALLOW_SEEN.values()),
                    "distinct_sites": len(_SWALLOW_SEEN),
                    "sites": [{"type": k[0], "raised_at": f"{k[1]}:{k[2]}",
                               # the line YOU can change — see _SWALLOW_HANDLER
                               "handled_in": _SWALLOW_HANDLER.get(k, "?"),
                               "occurrences": v} for k, v in top],
                    "note": ("live records above are de-duplicated, so their "
                             "`occurrences` is the count at emission time; these "
                             "are the real totals for the whole run"),
                })
        except Exception:
            pass
        _emit("process", "av.bootstrap.exit", {"pid": os.getpid()})

    try:
        atexit.register(_on_exit)
    except Exception:
        pass

    return True
