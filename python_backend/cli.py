"""
agentvision — universal auto-instrumentation CLI.

Single command bridges any program into AgentVision with zero source-code
changes to the target. The wrapper injects PYTHONPATH+env so the bootstrap's
sitecustomize.py loads at interpreter startup; for non-Python programs the
wrapper still tees stdout/stderr to the JSONL sink.

Verbs:
    agentvision attach <project_dir>          one-time profile registration
    agentvision run    -- <command...>        spawn child with hooks injected
    agentvision status                        show bridged projects + active
    agentvision daemon start|stop|status      keypress+mouse capture daemon

Run via:
    python -m python_backend.cli <verb> [args...]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Repo paths
AV_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_DIR = AV_ROOT / "agent_bootstrap"
PROFILES_FILE = AV_ROOT / "python_backend" / "profiles.json"
ACTIVE_FILE = AV_ROOT / "python_backend" / "active_profile.txt"

# Cross-platform OS helpers (temp paths, liveness, detached spawn).
sys.path.insert(0, str(AV_ROOT))
sys.path.insert(0, str(AV_ROOT / "python_backend"))
from utils import platform_shim  # noqa: E402

DAEMON_PID_FILE = platform_shim.daemon_pid_file()
DAEMON_LOG_FILE = platform_shim.daemon_log_file()


def _now_iso() -> str:
    n = datetime.now(timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"


# ── Profile load/save (mirror connectors.program_connector — no import to
#    avoid pulling unrelated deps into the CLI startup path) ─────────────────

#: Same rule as connectors.program_connector: a read/parse failure used to be
#: returned as `{}`, i.e. "there are no profiles", and the next _save_profiles
#: committed that emptiness over the user's hand-authored file. An empty
#: collection that is empty because a LOAD failed may not authorise a write.
_LAST_LOAD_OK = True
_LAST_LOAD_ERROR = ""


def _load_profiles() -> dict:
    global _LAST_LOAD_OK, _LAST_LOAD_ERROR
    _LAST_LOAD_OK, _LAST_LOAD_ERROR = True, ""
    if PROFILES_FILE.exists():
        try:
            data = json.loads(PROFILES_FILE.read_text())
            if not isinstance(data, dict):
                raise ValueError(f"expected an object, got {type(data).__name__}")
            return data
        except Exception as exc:
            _LAST_LOAD_OK = False
            _LAST_LOAD_ERROR = str(exc)
            print(f"[profiles] WARNING: could not read {PROFILES_FILE}: {exc}\n"
                  f"           saving is BLOCKED so your profiles are not "
                  f"replaced by an empty file.")
            return {}
    return {}


def _save_profiles(profiles: dict, *, force: bool = False) -> None:
    if not _LAST_LOAD_OK and not force:
        raise RuntimeError(
            f"refusing to overwrite {PROFILES_FILE}: the last load failed "
            f"({_LAST_LOAD_ERROR}). Fix or move the file first.")
    PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        if PROFILES_FILE.exists():
            PROFILES_FILE.with_suffix(".json.bak").write_text(
                PROFILES_FILE.read_text())
    except Exception:
        pass
    tmp = PROFILES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(profiles, indent=2))
    os.replace(tmp, PROFILES_FILE)


def _safe_name(s: str) -> str:
    return re.sub(r"[^\w\-]", "_", s).strip("_") or "project"


def _sniff_project(project: Path) -> tuple[str, list]:
    """Detect the project's language and likely log files so attach can pre-fill
    log_sources for ANY language. Language detection delegates to the ONE shared
    detector (connectors.log_sources.detect_language) — never reimplemented here,
    so attach agrees with installer + profile wiring. Returns ('', []) if the
    connector helper can't be imported (keeps `attach` dependency-light)."""
    try:
        from connectors import log_sources as _ls
    except Exception:
        return "", []
    try:
        lang = _ls.detect_language(str(project))
        sources = _ls.suggest_log_sources(str(project))
        return lang, sources
    except Exception:
        return "", []


# ── Verb: attach ──────────────────────────────────────────────────────────────

def cmd_attach(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    if not project.exists():
        print(f"agentvision: project does not exist: {project}", file=sys.stderr)
        return 2

    name = args.name or _safe_name(project.name)
    profiles = _load_profiles()

    # ── UNIVERSAL auto-bridge: detect language + scaffold the agentvision/
    #    folder (sinks + per-language OUTPUT EMITTER) inside the project. This
    #    is "the most important part": one command bridges ANY language with
    #    zero code changes. Falls back gracefully if the installer can't import.
    language = ""
    emitter = {}
    overrides = {}
    try:
        from installer import install_into_project
        report = install_into_project(str(project), profile_name=name)
        language = report.language
        emitter = report.emitter
        overrides = report.profile_overrides
        if report.errors:
            for p, e in report.errors:
                print(f"agentvision: install warning: {p}: {e}", file=sys.stderr)
    except Exception as e:
        print(f"agentvision: universal install unavailable ({e}); "
              f"falling back to log/ scaffold", file=sys.stderr)

    # Sinks come from the installer's canonical agentvision/ folder when it ran;
    # otherwise fall back to the legacy log/ layout so attach never fails.
    if overrides:
        actions_path = Path(overrides.get("action_log_file", project / "agentvision" / "actions.jsonl"))
        text_path    = Path(overrides.get("log_file",        project / "agentvision" / "log.txt"))
        stats_dir    = Path(overrides.get("stats_folder",    project / "agentvision" / "stats"))
        state_file   = overrides.get("state_file", "")
        log_sources  = overrides.get("log_sources", [])
    else:
        log_dir = project / "agentvision"
        log_dir.mkdir(parents=True, exist_ok=True)
        actions_path = log_dir / "actions.jsonl"; actions_path.touch(exist_ok=True)
        text_path    = log_dir / "log.txt";       text_path.touch(exist_ok=True)
        stats_dir    = log_dir / "stats"
        state_file   = ""
        _lang2, _srcs = _sniff_project(project)
        language = language or _lang2
        log_sources = _srcs

    profile_entry = {
        "name": name,
        "display_name": args.display_name or project.name,
        "log_file": str(text_path),
        "stats_folder": str(stats_dir),
        "screenshots_folder": str(project / "agentvision" / "screenshots"),
        "config_folder": str(project / "config"),
        "state_file": str(state_file),
        "project_root": str(project),
        # process_name matches the runtime; "python" also substring-matches
        # python3/python.exe. For other languages the tee/window path is used.
        "process_name": {"python": "python", "node": "node", "ruby": "ruby",
                         "java": "java", "dotnet": "dotnet", "go": "",
                         }.get(language, ""),
        "python_exe": sys.executable,
        "test_dir": str(project / "test"),
        "notes": f"Auto-attached by agentvision on {_now_iso()}"
                 + (f" — language: {language}, emitter: {emitter.get('kind','tee')}"
                    if language else ""),
        "capture_app": "",
        "capture_crop": "",
        "action_log_file": str(actions_path),
        "language": language,
        "log_sources": log_sources,
    }

    profiles[name] = profile_entry
    _save_profiles(profiles)

    marker = project / ".agentvision_attached"
    marker.write_text(json.dumps({
        "name": name, "attached_at": _now_iso(),
        "av_root": str(AV_ROOT), "language": language,
    }, indent=2))

    print(f"agentvision: attached '{name}' → {project}")
    print(f"  language:      {language or 'unknown'}")
    if emitter:
        print(f"  emitter:       {emitter.get('kind','tee')} "
              f"({'zero-config autoload' if emitter.get('autoload') else emitter.get('kind')})")
        print(f"  agentvision/:  scaffolded (sinks + emitter inside the project)")
        print(f"  how to run:    {emitter.get('how_to_load','agentvision run -- <cmd>')}")
    print(f"  jsonl sink:    {actions_path}")
    print(f"\nNext: agentvision run -- <your program>   (bridges ANY language, zero effort)")
    return 0


# ── Verb: run ─────────────────────────────────────────────────────────────────

#: Files that mark a directory as "a project AgentVision has been set up for".
#: `agentvision/manifest.json` is the one the CURRENT bridge flow writes — it was
#: missing from this list, and that was a silent, total failure of the documented
#: front door: `agentvision run -- <cmd>` resolved to the CWD instead of the
#: target, so it instrumented the wrong directory, exited 0, and emitted NOTHING.
#: Measured on a freshly bridged project — 0 records from the wrong directory, 14
#: from the right one, with no message either way.
_PROJECT_MARKERS = (
    "agentvision/manifest.json",   # written by av_bridge_commit / install
    ".agentvision_attached",       # legacy marker from `agentvision attach`
    ".av_bridge_plan.json",        # a sealed bridge plan at the project root
)


def _find_project_marker(start: Path, seen: set[Path]) -> Path | None:
    """Walk up from `start` looking for any project marker."""
    cur = start if start.is_dir() else start.parent
    while cur != cur.parent and cur not in seen:
        seen.add(cur)
        for marker in _PROJECT_MARKERS:
            if (cur / marker).exists():
                return cur
        cur = cur.parent
    return None


def _resolve_project_for_command(cmd: list[str], quiet: bool = False) -> Path:
    """Which project is this command part of?

    Looks for a project marker, starting from any path-like argument in the
    command and then from the cwd. Falls back to the cwd — but SAYS SO, because a
    silent fallback is how this came to instrument the wrong directory: the run
    looked completely normal and produced an empty log, which reads as "the
    program logged nothing" rather than "AgentVision watched somewhere else".
    """
    candidates: list[Path] = [Path.cwd()]
    for tok in cmd:
        p = Path(tok).expanduser()
        if p.exists():
            candidates.insert(0, p.resolve().parent if p.is_file() else p.resolve())
    seen: set[Path] = set()
    for start in candidates:
        found = _find_project_marker(start, seen)
        if found is not None:
            return found
    cwd = Path.cwd()
    if not quiet:
        print(f"agentvision: no bridged project found for this command — falling "
              f"back to the current directory ({cwd}). If the program you meant "
              f"lives elsewhere, pass --project <dir>, or run from that folder. "
              f"Nothing will be recorded for a project this is not.",
              file=sys.stderr)
    return cwd


def _load_log_adapters():
    """Lazily import the (pure-stdlib) log-adapter framework so a child program
    launched in ANY language gets its console lines normalized into the unified
    JSON event schema. Returns the module, or None if unavailable (then the tee
    falls back to the raw-text behaviour, so `run` never breaks)."""
    try:
        from connectors import log_adapters as la
        return la
    except Exception:
        return None


def _emit_console_line(sink: Path, channel: str, line: str, la, locked: str) -> str:
    """Emit ONE console line as a unified JSON event, normalized through the log
    adapters (log4j / serilog / pino / logfmt / …) so a compiled program's
    stdout/stderr lands on the same time-aligned timeline as everything else.

    Preserves full backward-compat: data.text is still the raw line, and the
    channel (stdout/stderr) is retained. Adds top-level level/category + the
    parsed logger/message/fields, so an ERROR line printed by a Java or Go
    program is category='error' and shows up in bookmarks/failure detection.

    `locked` is the adapter name pinned after the first strong match (so we
    don't re-detect 23 formats on every line). Returns the (possibly updated)
    locked name for the caller to carry forward."""
    # Canonical timestamp = ARRIVAL time (the wrapper's clock, the SAME clock the
    # bridge stamps frames with) — this is what makes console lines correlate
    # with screenshots. Any timestamp embedded in the line is preserved
    # separately as data.emit_ts_ms.
    now_iso = _now_iso()
    now_ms = time.time() * 1000.0
    ev = None
    if la is not None and line.strip():
        try:
            ev = la.parse_line(line, adapter_name=locked)
        except Exception:
            ev = None

    if ev is None:
        rec = {
            "ts": now_iso, "ts_ms": now_ms, "category": channel, "level": "",
            "source": f"av.wrapper.{channel}",
            "state": None, "run_id": os.environ.get("AGENTVISION_RUN_ID"),
            "trace_id": None, "frame_seq": None, "coords": None,
            "data": {"text": line, "channel": channel},
        }
        _write_jsonl(sink, rec)
        return locked

    adapter_name = (ev.get("data") or {}).get("adapter", "")
    # Lock onto the first STRONG adapter (not the weak catch-alls) so subsequent
    # lines skip re-detection; a mixed stream keeps auto-detecting until then.
    if not locked and adapter_name and adapter_name not in ("raw", "generic_ts"):
        locked = adapter_name

    data = dict(ev.get("data") or {})
    data["text"] = line
    data["channel"] = channel
    if ev.get("ts_ms") is not None:
        data["emit_ts_ms"] = ev["ts_ms"]
    parsed_source = ev.get("source") or ""
    rec = {
        "ts": now_iso, "ts_ms": now_ms,
        "category": ev.get("category") or channel,
        "level": ev.get("level") or "",
        # Prefer the parsed logger name; keep the wrapper channel discoverable.
        "source": parsed_source if parsed_source and parsed_source != adapter_name
                  else f"av.wrapper.{channel}",
        "state": None, "run_id": os.environ.get("AGENTVISION_RUN_ID"),
        "trace_id": ev.get("trace_id"), "frame_seq": None, "coords": None,
        "data": data,
    }
    _write_jsonl(sink, rec)
    return locked


def _tee(stream, channel: str, sink_path: Path, mirror, emit_jsonl: bool = True) -> None:
    """Read child stream line-by-line, mirror to terminal AND (optionally) emit a
    normalized JSONL event per line.

    This is the "any program, any language" path: whatever the child prints to
    stdout/stderr is parsed through the log adapters and lands on the unified,
    time-aligned JSON timeline. When the child has an IN-PROCESS emitter loaded
    (Python bootstrap / Node --require / Ruby -r), that emitter already writes
    richer structured records, so the wrapper only MIRRORS to the terminal here
    (emit_jsonl=False) to avoid duplicating every line.
    """
    la = _load_log_adapters() if emit_jsonl else None
    locked = ""   # adapter name, pinned after the first strong match
    try:
        for line_b in iter(stream.readline, b""):
            try:
                line = line_b.decode("utf-8", errors="replace").rstrip("\r\n")
            except Exception:
                line = repr(line_b)
            try:
                mirror.write(line + "\n")
                mirror.flush()
            except Exception:
                pass
            if emit_jsonl:
                locked = _emit_console_line(sink_path, channel, line, la, locked)
    except Exception:
        pass


def _child_has_inprocess_emitter(cmd: list[str], project: Path) -> bool:
    """True when the child interpreter has an AgentVision IN-PROCESS emitter that
    will write structured records itself — so the wrapper tee should mirror-only.
    Keyed off the named interpreter in the command (accurate, no guessing)."""
    if not cmd:
        return False
    exe = Path(cmd[0]).name.lower()
    if exe.startswith("python"):     # bootstrap loads via PYTHONPATH (set in run)
        return True
    em = project / "agentvision" / "emitters"
    if exe in ("node", "nodejs") and (em / "av_emit.js").exists():
        return True
    if exe == "ruby" and (em / "av_emit.rb").exists():
        return True
    return False


def _write_jsonl(sink: Path, rec: dict) -> None:
    """Append one already-formed unified-schema record to the JSONL sink."""
    try:
        with sink.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _emit_jsonl(sink: Path, category: str, source: str, data: dict) -> None:
    _write_jsonl(sink, {
        "ts": _now_iso(),
        "ts_ms": time.time() * 1000.0,
        "category": category,
        "source": source,
        "state": None,
        "run_id": os.environ.get("AGENTVISION_RUN_ID"),
        "trace_id": None,
        "frame_seq": None,
        "coords": None,
        "data": data,
    })


# ── File-write watcher (stdlib polling — watchdog fails to build on py3.14) ──

# Directories ignored when scanning the project for file changes. Mostly
# noise that would drown the JSONL.
_WATCH_IGNORE_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "dist", "build", ".idea", ".vscode", "log",
}
_WATCH_IGNORE_SUFFIXES = {".pyc", ".pyo", ".swp", ".swo", ".tmp"}


def _scan_project_mtimes(root: Path) -> dict[str, float]:
    """Walk root, return {relpath: mtime_ms} for every file we care about.

    Skips _WATCH_IGNORE_DIRS and the JSONL sink itself. Cheap-ish on small
    projects (<10k files); for larger ones the wrapper's overhead is bounded
    by WATCH_INTERVAL_S which is configurable via env.
    """
    out: dict[str, float] = {}
    root_str = str(root)
    for dirpath, dirnames, filenames in os.walk(root_str):
        dirnames[:] = [d for d in dirnames if d not in _WATCH_IGNORE_DIRS
                       and not d.startswith(".")]
        for fn in filenames:
            if any(fn.endswith(s) for s in _WATCH_IGNORE_SUFFIXES):
                continue
            full = os.path.join(dirpath, fn)
            try:
                out[os.path.relpath(full, root_str)] = os.path.getmtime(full)
            except OSError:
                pass
    return out


def _file_watcher(project: Path, sink: Path, stop_event: threading.Event,
                  interval_s: float) -> None:
    try:
        prev = _scan_project_mtimes(project)
    except Exception:
        prev = {}
    while not stop_event.wait(interval_s):
        try:
            cur = _scan_project_mtimes(project)
        except Exception:
            continue
        # Created or modified
        for path, mt in cur.items():
            old = prev.get(path)
            if old is None:
                _emit_jsonl(sink, "file", "av.wrapper.file.create",
                            {"path": path, "mtime_ms": mt * 1000.0})
            elif mt > old + 0.001:
                _emit_jsonl(sink, "file", "av.wrapper.file.modify",
                            {"path": path, "mtime_ms": mt * 1000.0})
        # Deleted
        for path in prev.keys() - cur.keys():
            _emit_jsonl(sink, "file", "av.wrapper.file.delete",
                        {"path": path})
        prev = cur


def _win_short_path(p: str) -> str:
    """On Windows, return the 8.3 short path (no spaces) for an EXISTING path —
    this is how we make a require path safe for RUBYOPT, which is whitespace-
    split with NO quoting. No-op on other OSes or on failure.

    CAVEAT (MSDN): when 8.3 name generation is DISABLED on the volume
    (`fsutil 8dot3name query` — common on modern non-system volumes),
    GetShortPathNameW SUCCEEDS but returns the LONG path unchanged, so the
    result may still contain spaces. Callers must re-check for whitespace
    (see _ruby_space_free_require)."""
    if not platform_shim.IS_WINDOWS:
        return p
    try:
        import ctypes
        from ctypes import wintypes
        GetShort = ctypes.windll.kernel32.GetShortPathNameW
        GetShort.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        GetShort.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(600)
        n = GetShort(p, buf, len(buf))
        if 0 < n < len(buf):
            return buf.value
    except Exception:
        pass
    return p


def _ruby_space_free_require(rp: str) -> str | None:
    """Return a path Ruby can be told to require via RUBYOPT (which is
    whitespace-split with NO quote support), guaranteed to contain no
    whitespace — or None if that's impossible.

    Order: the path itself if clean → its 8.3 short form (may be a no-op when
    8.3 generation is disabled on the volume, so RE-CHECK it) → a staged stub
    .rb in a whitespace-free directory whose only content is a require of the
    real (spaced) path. The stub preserves the real file's __dir__ semantics
    because the real file, not a copy, is what ultimately loads."""
    if not any(c in rp for c in " \t"):
        return rp
    short = _win_short_path(rp)
    if not any(c in short for c in " \t"):
        return short
    # 8.3 shortening unavailable — stage a stub in a space-free location.
    import hashlib
    import tempfile
    candidates = [tempfile.gettempdir()]
    if platform_shim.IS_WINDOWS:
        candidates.append(os.environ.get("SystemDrive", "C:") + "\\")
    for base in candidates:
        base_sf = base if not any(c in base for c in " \t") else _win_short_path(base)
        if any(c in base_sf for c in " \t"):
            continue
        try:
            stub_dir = Path(base_sf) / "av_emit_stubs"
            stub_dir.mkdir(parents=True, exist_ok=True)
            h = hashlib.sha1(rp.encode("utf-8")).hexdigest()[:12]
            stub = stub_dir / f"av_emit_{h}.rb"
            esc = rp.replace("\\", "\\\\").replace('"', '\\"')
            stub.write_text(f'require "{esc}"\n', encoding="utf-8")
            sp = str(stub)
            if not any(c in sp for c in " \t"):
                return sp
        except Exception:
            continue
    return None


def _inject_emitter_env(env: dict, project: Path) -> None:
    """Set env vars that auto-load the scaffolded per-language emitter, appending
    to any existing NODE_OPTIONS/RUBYOPT instead of clobbering. Best-effort and
    idempotent — safe even if the project was never `attach`ed (no-op then).

    Windows-correct: NODE_OPTIONS supports double-quoted tokens, so a require
    path containing spaces is quoted; RUBYOPT is whitespace-split with NO quote
    support, so a spaced path is converted to its 8.3 short form."""
    em = project / "agentvision" / "emitters"
    node_js = em / "av_emit.js"
    ruby_rb = em / "av_emit.rb"

    if node_js.exists():
        np = str(node_js)
        cur = env.get("NODE_OPTIONS", "")
        if np not in cur:                       # idempotent (raw path substring)
            token = f'--require "{np}"' if " " in np else f"--require {np}"
            env["NODE_OPTIONS"] = (cur + " " + token).strip() if cur else token

    if ruby_rb.exists():
        rp = str(ruby_rb)
        cur = env.get("RUBYOPT", "")
        if rp not in cur and _win_short_path(rp) not in cur:
            safe = _ruby_space_free_require(rp)
            if safe is None:
                # A spaced token would make Ruby try to require the first
                # word and choke on the rest — worse than not injecting.
                print("agentvision: RUBYOPT emitter injection skipped — the "
                      "emitter path contains whitespace and no whitespace-free "
                      "form is available (8.3 names disabled?). Ruby auto-load "
                      "disabled for this run.", file=sys.stderr)
            elif safe not in cur:
                token = f"-r{safe}"
                env["RUBYOPT"] = (cur + " " + token).strip() if cur else token


def _ensure_scaffolded(project: Path) -> None:
    """Zero-step first-run bootstrap: if the project has never been bridged
    (no agentvision/manifest.json), auto-scaffold it now — detect language, drop
    the sink files + per-language emitter — so ONE `agentvision run` bridges any
    program with no prior `attach`. Best-effort; never blocks the run."""
    manifest = project / "agentvision" / "manifest.json"
    if manifest.exists():
        return
    try:
        from installer import install_into_project
        install_into_project(str(project))
    except Exception as e:
        print(f"agentvision: auto-scaffold skipped ({e})", file=sys.stderr)


def cmd_run(args: argparse.Namespace) -> int:
    cmd = args.command
    if not cmd:
        print("agentvision run: missing command (use -- before the command)",
              file=sys.stderr)
        return 2

    project = (Path(args.project).expanduser().resolve()
               if args.project else _resolve_project_for_command(cmd))
    # Zero-step bootstrap: scaffold the agentvision/ folder + emitter on first run.
    _ensure_scaffolded(project)
    # Canonical sink folder for the universal bridge: <project>/agentvision/.
    sink_dir = project / "agentvision"
    sink_dir.mkdir(parents=True, exist_ok=True)
    sink = sink_dir / "actions.jsonl"

    # Auto-start the input recorder daemon (detached) when requested — no manual
    # step. Opt-in via --record-input or AGENTVISION_RECORD_INPUT=1 (global input
    # hooks are consequential, so they are not started silently by default).
    if getattr(args, "record_input", False) or \
            os.environ.get("AGENTVISION_RECORD_INPUT") == "1":
        ok, msg = _daemon_start_detached()
        print(f"agentvision: input recorder {msg}", file=sys.stderr)

    run_id = f"av-{int(time.time() * 1000)}-{os.getpid()}"

    env = os.environ.copy()
    env["AGENTVISION_PROJECT"] = str(project)
    env["AGENTVISION_LOG_DIR"] = str(sink_dir)
    env["AGENTVISION_SINK"] = str(sink)      # emitters (node/ruby/go/…) read this
    env["AGENTVISION_RUN_ID"] = run_id
    # Prepend bootstrap so sitecustomize.py is auto-loaded by Python children.
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (str(BOOTSTRAP_DIR) +
                         (os.pathsep + existing_pp if existing_pp else ""))
    # Clear the per-process guard so a fresh child installs hooks.
    env.pop("AGENTVISION_HOOKED", None)

    # ── Honour the bridge plan's emitter selection on THIS launch path ────────
    # `agentvision run` is the primary way a program gets bridged, and it used to
    # set no AGENTVISION_HOOKS at all. The runtime reads unset as "arm
    # everything", so an agent that deliberately chose two hooks got all five
    # here — the selection was enforced only when CPython happened to import the
    # generated <project>/sitecustomize.py. The plan is already persisted in the
    # manifest, so read it and apply it.
    #
    # An explicit environment override still wins: someone debugging the bridge
    # itself must be able to force a hook set without editing the manifest.
    if "AGENTVISION_HOOKS" not in os.environ:
        try:
            _mf = json.loads((sink_dir / "manifest.json").read_text(
                encoding="utf-8", errors="replace"))
            _requested = list(_mf.get("emitters_requested") or [])
        except Exception:
            _requested = []
        if _requested:
            try:
                from emitters import hooks_env as _hooks_env
            except Exception:
                try:
                    from python_backend.emitters import hooks_env as _hooks_env
                except Exception:
                    _hooks_env = None
            if _hooks_env is not None:
                _he = _hooks_env(_requested)
                if _he:
                    env.update(_he)
                    print(f"agentvision: honouring bridge plan — hooks "
                          f"{_he['AGENTVISION_HOOKS']}", file=sys.stderr)
                else:
                    # hooks_env() returns {} for "no hook ids named", which the
                    # runtime reads as arm-everything. Say so rather than let the
                    # agent infer its exclusions were applied.
                    print("agentvision: bridge plan names no in-process hook ids; "
                          "all hooks will arm (see emitter_selection_fail_open)",
                          file=sys.stderr)

    # ── Inject the per-language OUTPUT EMITTER so an in-process emitter
    #    auto-loads with zero code changes (Node NODE_OPTIONS=--require,
    #    Ruby RUBYOPT=-r). This is what makes `agentvision run -- <cmd>` the
    #    universal, zero-effort front door for any language. If the project was
    #    attached, its scaffolded emitter files exist under agentvision/emitters/.
    _inject_emitter_env(env, project)

    _emit_jsonl(sink, "process", "av.wrapper.start", {
        "run_id": run_id, "argv": cmd, "project": str(project),
        "wrapper_pid": os.getpid(),
    })

    try:
        proc = subprocess.Popen(
            cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0,
        )
    except FileNotFoundError as e:
        _emit_jsonl(sink, "error", "av.wrapper.spawn_fail",
                    {"error": str(e), "argv": cmd})
        print(f"agentvision: cannot spawn: {e}", file=sys.stderr)
        return 127

    # If the child has an in-process emitter (python/node/ruby), it writes the
    # structured records itself — the wrapper tee then mirror-only's to avoid
    # double-capturing every line. Compiled/other langs → the tee IS the emitter.
    _emit = not _child_has_inprocess_emitter(cmd, project)
    t_out = threading.Thread(target=_tee,
                             args=(proc.stdout, "stdout", sink, sys.stdout, _emit),
                             daemon=True)
    t_err = threading.Thread(target=_tee,
                             args=(proc.stderr, "stderr", sink, sys.stderr, _emit),
                             daemon=True)
    t_out.start()
    t_err.start()

    # File-write watcher (stdlib polling). Disable with --no-watch-files
    # or AGENTVISION_WATCH_DISABLE=1; tune interval via AGENTVISION_WATCH_INTERVAL.
    stop_watch = threading.Event()
    t_watch: threading.Thread | None = None
    if not getattr(args, "no_watch_files", False) and \
            os.environ.get("AGENTVISION_WATCH_DISABLE") != "1":
        try:
            interval = float(os.environ.get("AGENTVISION_WATCH_INTERVAL", "1.0"))
        except ValueError:
            interval = 1.0
        t_watch = threading.Thread(
            target=_file_watcher,
            args=(project, sink, stop_watch, interval),
            daemon=True,
        )
        t_watch.start()

    # Forward common signals so Ctrl-C in the wrapper kills the child cleanly.
    # On Windows, Popen.send_signal only accepts SIGTERM/CTRL_C_EVENT/
    # CTRL_BREAK_EVENT; SIGINT/SIGBREAK raise ValueError. Since the child
    # shares our console it already receives CTRL_C_EVENT directly, but fall
    # back to terminate() so the wrapper still guarantees the child stops.
    def _forward(signum, _frame):
        try:
            proc.send_signal(signum)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    for _signame in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
        _sig = getattr(signal, _signame, None)
        if _sig is None:
            continue
        try:
            signal.signal(_sig, _forward)
        except Exception:
            pass

    rc = proc.wait()
    t_out.join(timeout=2.0)
    t_err.join(timeout=2.0)
    stop_watch.set()
    if t_watch is not None:
        t_watch.join(timeout=2.0)

    _emit_jsonl(sink, "process", "av.wrapper.exit", {
        "run_id": run_id, "exit_code": rc,
    })
    return rc


# ── Verb: status ──────────────────────────────────────────────────────────────

def cmd_status(_args: argparse.Namespace) -> int:
    profiles = _load_profiles()
    print(f"agentvision root: {AV_ROOT}")
    print(f"profiles file:    {PROFILES_FILE} ({'exists' if PROFILES_FILE.exists() else 'missing'})")
    print(f"bootstrap dir:    {BOOTSTRAP_DIR} ({'exists' if BOOTSTRAP_DIR.exists() else 'MISSING'})")
    print()

    daemon_state = "stopped"
    if DAEMON_PID_FILE.exists():
        try:
            pid = int(DAEMON_PID_FILE.read_text().strip())
            if platform_shim.pid_alive(pid):
                daemon_state = f"running (pid {pid})"
            else:
                daemon_state = f"stale pidfile ({DAEMON_PID_FILE})"
        except ValueError:
            daemon_state = f"stale pidfile ({DAEMON_PID_FILE})"
    print(f"input daemon:     {daemon_state}")
    print()

    if not profiles:
        print("attached projects: (none — try `agentvision attach <dir>`)")
        return 0

    print(f"attached projects ({len(profiles)}):")
    for name, p in profiles.items():
        root = p.get("project_root", "?")
        sink = p.get("action_log_file", "?")
        sz = ""
        try:
            sz = f"  [{Path(sink).stat().st_size} B]"
        except Exception:
            pass
        print(f"  • {name:20s} {root}")
        print(f"    sink: {sink}{sz}")
    return 0


# ── Verb: daemon (Phase 3-4 stub — real impl in input_daemon.py) ─────────────

# ── Verb: install ─────────────────────────────────────────────────────────────

def cmd_install_ocr(args: argparse.Namespace) -> int:
    """Make on-screen-text OCR work on THIS machine without the user hunting
    down a native tesseract build.

    macOS and Windows already contain an OCR engine (Apple Vision /
    Windows.Media.Ocr); all that is missing is a pip-installable binding, so
    this is a pip install and nothing else. Linux has no built-in engine, so it
    falls back to RapidOCR on onnxruntime — still pip-only. tesseract is used
    automatically when already present because it is faster, but is never
    required."""
    try:
        from utils import ocr_backends
    except Exception:
        sys.path.insert(0, str(AV_ROOT / "python_backend"))
        from utils import ocr_backends  # type: ignore

    before = ocr_backends.detect_engine()
    plan = ocr_backends.install_plan()
    print(f"agentvision install-ocr  ({plan['os']})")
    print(f"  current: "
          + (f"OCR AVAILABLE via {before['engine']}" if before.get("available")
             else f"OCR unavailable — {before.get('reason', '')[:120]}"))
    print(f"  plan:    pip install {' '.join(plan['pip'])}")
    print(f"  why:     {plan['note']}")

    if before.get("available") and not args.dry_run:
        print("\nOCR already works here — nothing to do. "
              "(Re-run with --dry-run to see the plan without installing.)")
        return 0
    if args.dry_run:
        return 0

    cmd = [sys.executable, "-m", "pip", "install", *plan["pip"]]
    print(f"\n$ {' '.join(cmd)}")
    try:
        rc = subprocess.call(cmd)
    except Exception as e:
        print(f"install failed to start: {e}", file=sys.stderr)
        return 1
    if rc != 0:
        print(f"pip exited {rc}", file=sys.stderr)
        return rc

    after = ocr_backends.detect_engine()
    if after.get("available"):
        print(f"\nOCR is now AVAILABLE via {after['engine']}"
              + ("  (zero system install)" if after.get("backend") in
                 ("apple_vision", "windows_ocr") else ""))
        print("Restart the bridge so the OCR tools pick it up.")
        return 0
    print(f"\nOCR still unavailable: {after.get('reason', '')[:200]}", file=sys.stderr)
    print(f"hint: {after.get('install_hint', '')}", file=sys.stderr)
    return 1


def cmd_install(args: argparse.Namespace) -> int:
    target_dir = Path(args.dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    py = sys.executable

    if platform_shim.IS_WINDOWS:
        # A .bat shim so `agentvision` works from any cmd/PowerShell prompt.
        target = target_dir / "agentvision.bat"
        # `setlocal` (with the implicit endlocal at script end) restores the
        # caller's current directory, so `cd /d` here doesn't permanently move
        # the user's shell to AV_ROOT on every invocation.
        shim = (
            "@echo off\r\n"
            "setlocal\r\n"
            "REM agentvision shim - auto-generated by `agentvision install`\r\n"
            f'cd /d "{AV_ROOT}" && "{py}" -m python_backend.cli %*\r\n'
        )
        # newline="" suppresses universal-newline translation; without it text
        # mode would turn each embedded \r\n into \r\r\n on Windows.
        target.write_text(shim, newline="")
        print(f"agentvision: installed shim -> {target}")
        if str(target_dir).lower() not in os.environ.get("PATH", "").lower().split(os.pathsep):
            print(f"  note: {target_dir} is NOT on PATH. Add it (user Path only) via PowerShell:")
            print(f"    [Environment]::SetEnvironmentVariable('Path', "
                  f"[Environment]::GetEnvironmentVariable('Path','User') + "
                  f"';{target_dir}', 'User')")
            print("    (or: Settings > System > About > Advanced system settings"
                  " > Environment Variables, add it to the user Path)")
        print("  test: agentvision status")
        return 0

    # POSIX bash shim. Run from AV_ROOT so `python -m python_backend.cli` resolves.
    target = target_dir / "agentvision"
    shim_with_cwd = (
        "#!/usr/bin/env bash\n"
        "# agentvision shim — auto-generated by `agentvision install`\n"
        f'cd "{AV_ROOT}" && exec "{py}" -m python_backend.cli "$@"\n'
    )
    target.write_text(shim_with_cwd)
    target.chmod(0o755)

    print(f"agentvision: installed shim → {target}")
    if str(target_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"  note: {target_dir} is NOT on $PATH. Add this to your shell rc:")
        print(f'    export PATH="{target_dir}:$PATH"')
    print("  test: agentvision status")
    return 0


def _daemon_running() -> int | None:
    """Return the live daemon pid, or None. Clears a stale pidfile."""
    if not DAEMON_PID_FILE.exists():
        return None
    try:
        pid = int(DAEMON_PID_FILE.read_text().strip())
    except ValueError:
        return None
    if platform_shim.pid_alive(pid):
        return pid
    return None


def _daemon_start_detached() -> tuple[bool, str]:
    """Spawn the input daemon fully DETACHED (Windows: CREATE_NEW_PROCESS_GROUP|
    DETACHED_PROCESS via platform_shim; POSIX: start_new_session). Idempotent —
    a no-op if already running. Returns (ok, message). Used by both
    `daemon start` and the zero-step auto-start in `run --record-input`."""
    pid = _daemon_running()
    if pid:
        return True, f"already running (pid {pid})"
    try:
        if DAEMON_PID_FILE.exists():
            DAEMON_PID_FILE.unlink()
    except Exception:
        pass
    cmd = [sys.executable, "-m", "python_backend.daemon.input_daemon"]
    env = os.environ.copy()
    env.setdefault("AGENTVISION_DAEMON_LOG", str(DAEMON_LOG_FILE))
    env.setdefault("AGENTVISION_DAEMON_PID_FILE", str(DAEMON_PID_FILE))
    try:
        with open(DAEMON_LOG_FILE, "ab") as logf:
            proc = subprocess.Popen(
                cmd, env=env, cwd=str(AV_ROOT),
                stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                **platform_shim.detached_popen_kwargs(),
            )
    except Exception as e:
        return False, f"spawn failed: {e}"
    for _ in range(20):
        time.sleep(0.1)
        pid = _daemon_running()
        if pid:
            return True, f"started (pid {pid}, log {DAEMON_LOG_FILE})"
        if proc.poll() is not None:
            return False, f"failed to start — see {DAEMON_LOG_FILE}"
    return False, f"spawn timed out — see {DAEMON_LOG_FILE}"


def cmd_daemon(args: argparse.Namespace) -> int:
    sub = args.daemon_cmd
    if sub == "status":
        if not DAEMON_PID_FILE.exists():
            print("daemon: stopped")
            return 0
        try:
            pid = int(DAEMON_PID_FILE.read_text().strip())
        except ValueError:
            print(f"daemon: stale pidfile at {DAEMON_PID_FILE}")
            return 1
        if platform_shim.pid_alive(pid):
            print(f"daemon: running (pid {pid})")
            return 0
        print(f"daemon: stale pidfile at {DAEMON_PID_FILE} (process gone)")
        return 1

    if sub == "stop":
        if not DAEMON_PID_FILE.exists():
            print("daemon: not running")
            return 0
        try:
            pid = int(DAEMON_PID_FILE.read_text().strip())
            platform_shim.terminate_pid(pid)
            for _ in range(20):
                time.sleep(0.1)
                if not platform_shim.pid_alive(pid):
                    break
            try:
                DAEMON_PID_FILE.unlink()
            except FileNotFoundError:
                pass
            print(f"daemon: stopped (pid {pid})")
            return 0
        except Exception as e:
            print(f"daemon: stop failed: {e}", file=sys.stderr)
            return 1

    if sub == "start":
        ok, msg = _daemon_start_detached()
        print(f"daemon: {msg}", file=sys.stderr if not ok else sys.stdout)
        return 0 if ok else 1

    return 2


# ── Verb: doctor (cross-platform runtime self-confirmation) ──────────────────

def _doctor_input_hooks(timeout: float = 3.0) -> dict:
    """Run the input-hook self-test by spawning the daemon in --selftest mode
    and parsing its JSON verdict — the DEFINITIVE 'input capture confirmed
    working' proof on real hardware.
      Windows: installs the LL hooks + SendInput probe + confirms the callback
               fired.
      Linux  : confirms python-evdev + /dev/input readability (+ per-device
               physical/synthetic classification), and when /dev/uinput is
               writable proves the uinput→evdev injection round-trip.
      macOS  : not applicable (CGEventTap is exercised by the daemon at start).
    """
    if not (platform_shim.IS_WINDOWS or platform_shim.IS_LINUX):
        return {"check": "input_hooks", "ok": None,
                "detail": f"no one-shot self-test on this OS (host os={sys.platform}); "
                          "the macOS CGEventTap path is exercised by the daemon at start."}
    label = "win_input_hooks" if platform_shim.IS_WINDOWS else "linux_input_evdev"
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "python_backend.daemon.input_daemon", "--selftest"],
            cwd=str(AV_ROOT), capture_output=True, text=True, timeout=timeout + 5)
        for line in (proc.stdout or "").splitlines():
            if line.startswith("AV_SELFTEST_JSON "):
                return json.loads(line[len("AV_SELFTEST_JSON "):])
        return {"check": label, "ok": False,
                "detail": "no self-test verdict emitted", "stderr": (proc.stderr or "")[-500:]}
    except Exception as e:
        return {"check": label, "ok": False, "detail": f"self-test spawn failed: {e}"}


def _doctor_emitter_roundtrip(project: Path) -> dict:
    """Prove auto-injection end-to-end: scaffold the project, `agentvision run`
    a tiny child that prints a unique marker, and confirm the marker lands in the
    scaffolded sink. This is the OUTPUT-side 'confirmed working' proof."""
    out = {"check": "emitter_roundtrip", "ok": False, "detail": ""}
    try:
        _ensure_scaffolded(project)
        sink = project / "agentvision" / "actions.jsonl"
        before = sink.stat().st_size if sink.exists() else 0
        marker = f"AV_DOCTOR_{int(time.time()*1000)}"
        # A tiny child in a language that always works here: the current Python.
        args = argparse.Namespace(
            command=[sys.executable, "-c", f"print('{marker}')"],
            project=str(project), no_watch_files=True, record_input=False)
        # Suppress the child's terminal mirror so it can't corrupt the doctor's
        # own (JSON) stdout — the tee mirrors to whatever sys.stdout is now.
        import contextlib
        with open(os.devnull, "w") as _dn:
            with contextlib.redirect_stdout(_dn), contextlib.redirect_stderr(_dn):
                cmd_run(args)
        found = False
        if sink.exists():
            with sink.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(before)
                for raw in f:
                    if marker in raw:
                        found = True
                        break
        out["ok"] = found
        out["sink"] = str(sink)
        out["detail"] = ("child output round-tripped into the scaffolded sink"
                         if found else "marker did not appear in sink")
    except Exception as e:
        out["detail"] = f"round-trip failed: {e}"
    return out


def cmd_doctor(args: argparse.Namespace) -> int:
    """Self-confirm every runtime path on THIS machine; emit a JSON health report.
    On Windows this is the definitive first-run proof that input hooks fire,
    capture is non-blank, window enum works, and auto-injection round-trips.
    On Linux it additionally reports the display session (X11/Wayland/headless
    + the capture chain picked for it) and the evdev/input-group verdict."""
    import tempfile
    checks = []
    if platform_shim.IS_LINUX:
        # Session detection first — it explains every later Linux verdict
        # (X11 vs Wayland vs headless routes capture + window enum).
        checks.append(platform_shim.linux_session_selftest())
    checks.append(platform_shim.capture_selftest())
    checks.append(platform_shim.window_enum_selftest(args.capture_app or ""))
    checks.append(_doctor_input_hooks())

    proj = (Path(args.project).expanduser().resolve() if args.project
            else Path(tempfile.mkdtemp(prefix="av_doctor_")))
    checks.append(_doctor_emitter_roundtrip(proj))

    dpid = _daemon_running()
    checks.append({"check": "input_daemon", "ok": True,
                   "running": bool(dpid), "pid": dpid,
                   "detail": f"daemon {'running pid ' + str(dpid) if dpid else 'not running'}"})

    # Overall: a check counts against health only if it explicitly failed (False).
    # `None` = not-applicable on this OS (doesn't fail the report).
    failed = [c for c in checks if c.get("ok") is False]
    report = {
        "schema_version": "2.0.0",
        "os": sys.platform,
        "python": sys.version.split()[0],
        "generated_ms": time.time() * 1000.0,
        "ok": len(failed) == 0,
        "failed_checks": [c["check"] for c in failed],
        "checks": checks,
        "note": ("Windows input-hook / Linux evdev + capture results are the "
                 "real-hardware confirmation; checks that don't apply to this "
                 "OS report ok=null (not applicable)."),
    }
    print(json.dumps(report, indent=None if args.json else 2))
    return 0 if report["ok"] else 1


# ── Argparse wiring ───────────────────────────────────────────────────────────

# ── Push Mode (hooks) ─────────────────────────────────────────────────────────

def cmd_install_hooks(args) -> int:
    """Install AgentVision's Push Mode hooks into a Claude Code settings file."""
    import push_hooks
    res = push_hooks.install(scope=args.scope, project_dir=args.project,
                             python_exe=args.python, timeout_s=args.timeout,
                             with_stop_block=args.with_stop_block,
                             dry_run=args.dry_run)
    print(json.dumps(res, indent=2))
    if res.get("ok") and not args.dry_run:
        print("\n*** RESTART CLAUDE CODE for the hooks to take effect. ***")
    return 0 if res.get("ok") else 1


def cmd_uninstall_hooks(args) -> int:
    """Remove exactly AgentVision's hook entries; leave other hooks alone."""
    import push_hooks
    res = push_hooks.uninstall(scope=args.scope, project_dir=args.project)
    print(json.dumps(res, indent=2))
    return 0 if res.get("ok") else 1


def cmd_push_mode(args) -> int:
    """Turn Push Mode on/off without touching the settings file, or show status."""
    import push_hooks
    if args.state == "status":
        print(json.dumps(push_hooks.status(scope=args.scope,
                                          project_dir=args.project), indent=2))
        return 0
    res = push_hooks.set_enabled(args.state == "on")
    print(json.dumps(res, indent=2))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentvision",
        description="Universal auto-instrumentation bridge — see, log, "
                    "and diagnose any program with zero code changes.",
    )
    sub = p.add_subparsers(dest="verb", required=True)

    sp = sub.add_parser("attach", help="register a project as bridged")
    sp.add_argument("project", help="path to the project root")
    sp.add_argument("--name", help="profile key (default: dirname)")
    sp.add_argument("--display-name", help="human-readable display name")
    sp.set_defaults(func=cmd_attach)

    sp = sub.add_parser("run", help="run a command with hooks injected")
    sp.add_argument("--project", help="project root (default: auto-detect from cwd / argv)")
    sp.add_argument("--no-watch-files", action="store_true",
                    help="disable file-write polling")
    sp.add_argument("--record-input", action="store_true",
                    help="also auto-start the input recorder daemon (detached)")
    sp.add_argument("command", nargs=argparse.REMAINDER,
                    help="command to run, e.g. -- python myapp.py")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("status", help="show bridge state + attached projects")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("daemon", help="manage the keypress+mouse daemon")
    sp.add_argument("daemon_cmd", choices=["start", "stop", "status"])
    sp.set_defaults(func=cmd_daemon)

    sp = sub.add_parser("doctor", help="self-test the runtime paths (JSON health report)")
    sp.add_argument("--project", help="project root to test emitter round-trip against")
    sp.add_argument("--capture-app", default="", help="window title to test window-enum against")
    sp.add_argument("--json", action="store_true", help="print raw JSON only")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("install-hooks",
                        help="install Push Mode hooks into Claude Code settings")
    sp.add_argument("--scope", choices=["user", "project"], default="user",
                    help="user = ~/.claude/settings.json (default); "
                         "project = <project>/.claude/settings.json")
    sp.add_argument("--project", default=None, help="project dir for --scope project")
    sp.add_argument("--python", default="python3",
                    help="interpreter the hook runs under (default: python3)")
    sp.add_argument("--timeout", type=int, default=5, help="hook timeout in seconds")
    sp.add_argument("--with-stop-block", action="store_true",
                    help="ALSO install the Stop backstop hook. Blocking still "
                         "requires AGENTVISION_AMBIENT_STOP_BLOCK=1.")
    sp.add_argument("--dry-run", action="store_true", help="show the plan only")
    sp.set_defaults(func=cmd_install_hooks)

    sp = sub.add_parser("uninstall-hooks", help="remove AgentVision's hooks")
    sp.add_argument("--scope", choices=["user", "project"], default="user")
    sp.add_argument("--project", default=None)
    sp.set_defaults(func=cmd_uninstall_hooks)

    sp = sub.add_parser("push-mode", help="turn Push Mode on/off or show status")
    sp.add_argument("state", choices=["on", "off", "status"])
    sp.add_argument("--scope", choices=["user", "project"], default="user")
    sp.add_argument("--project", default=None)
    sp.set_defaults(func=cmd_push_mode)

    sp = sub.add_parser("install", help="drop an `agentvision` shim into a bin dir")
    sp.add_argument("--dir", default=str(Path.home() / "bin"),
                    help="install directory (default: ~/bin)")
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser("install-ocr",
                        help="set up on-screen-text OCR (no system packages)")
    sp.add_argument("--dry-run", action="store_true",
                    help="show what would be installed, install nothing")
    sp.set_defaults(func=cmd_install_ocr)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    # argparse REMAINDER includes a leading "--" if present; strip it.
    if getattr(args, "command", None) and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
