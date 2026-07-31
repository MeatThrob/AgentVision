"""
Where is the program ACTUALLY writing?
================================================================================
A configured log path is a guess. This asks the operating system.

WHY THIS EXISTS — a real hour lost on this project. AgentVision was configured to
read `~/Developer/<project>/log/log.txt`, so it read it, and reported its
contents as the current state of the program. The file had last been written 23
HOURS earlier. The live emulator was writing somewhere else entirely, and the
only hint anything was wrong was `ts_ms: null` on every event. An entire analysis
("180 GPU present failures") was performed against a dead file.

Worse, the truth turned out to be a three-way split:

    ~/Desktop/AgentVision/sharpemu/log/log.txt      204 B, written seconds ago
    ~/Developer/<project>/log/log.txt         25,663 B, written a day ago
    ~/Developer/<project>/log/actions.jsonl  493 KB, "fresh" — but only
                                                because AgentVision's OWN
                                                watchdog was writing to it

No amount of reading logs harder finds that. `tail -f` on the wrong file is
perfectly happy to show you old bytes forever. But the OS knows exactly which
descriptors a pid holds open for writing, so we can simply ask, then compare that
against what the profile declared.

This is the kind of thing a bridge layer can do that a program's own built-in
logging structurally cannot: a program cannot tell you it is misconfigured,
because from its point of view nothing is wrong.

Read-only, best-effort, and never raises: it shells out to `lsof` with a hard
timeout and returns {available: false, reason} when that is not possible.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

#: Descriptor types that can hold program output. PIPE/CHR matter as much as REG:
#: "fd 2 is a PIPE" is the answer to "why is my log file empty".
_INTERESTING_TYPES = {"REG", "CHR", "PIPE", "FIFO"}
#: lsof access modes that mean the process can write.
_WRITE_MODES = {"w", "u"}

LSOF_TIMEOUT_S = float(os.environ.get("AGENTVISION_LSOF_TIMEOUT_S", "4"))


def _lsof_fds(pid: int) -> tuple[list[dict], str]:
    """Parse `lsof -F` for one pid. Returns (fds, error_reason)."""
    try:
        p = subprocess.run(
            ["lsof", "-p", str(int(pid)), "-a", "-d", "0-64", "-F", "fatn"],
            capture_output=True, timeout=LSOF_TIMEOUT_S)
    except FileNotFoundError:
        return [], "lsof not installed"
    except subprocess.TimeoutExpired:
        return [], f"lsof timed out after {LSOF_TIMEOUT_S}s"
    except Exception as exc:
        return [], f"lsof failed: {exc}"

    text = (p.stdout or b"").decode("utf-8", errors="replace")
    out: list[dict] = []
    cur: dict = {}
    for line in text.splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "f":                      # new descriptor begins
            if cur.get("fd") is not None:
                out.append(cur)
            cur = {"fd": val, "access": "", "type": "", "path": ""}
        elif tag == "a":
            cur["access"] = val
        elif tag == "t":
            cur["type"] = val
        elif tag == "n":
            cur["path"] = val
    if cur.get("fd") is not None:
        out.append(cur)
    return out, ""


#: Well-known descriptors, named so the report reads like an explanation rather
#: than a table of numbers.
_FD_NAMES = {"0": "stdin", "1": "stdout", "2": "stderr"}


def detect_write_targets(pid: int) -> dict:
    """Every descriptor this pid can write to, with stdout/stderr called out.

    Returns {available, pid, streams: {stdout, stderr}, writable_files: [...],
    all_fds: [...], reason}. `writable_files` is the set that could plausibly be
    a log: regular files the process holds open for writing.
    """
    if not pid:
        return {"available": False, "reason": "no pid", "pid": pid,
                "streams": {}, "writable_files": [], "all_fds": []}
    fds, err = _lsof_fds(pid)
    if err:
        return {"available": False, "reason": err, "pid": pid,
                "streams": {}, "writable_files": [], "all_fds": []}

    streams: dict = {}
    writable: list[dict] = []
    for d in fds:
        fd = d.get("fd") or ""
        typ = (d.get("type") or "").upper()
        acc = (d.get("access") or "").lower()
        path = d.get("path") or ""
        if fd in _FD_NAMES:
            streams[_FD_NAMES[fd]] = {
                "type": typ, "path": path, "access": acc,
                "is_file": typ == "REG",
                "is_terminal": typ == "CHR" and "/dev/tty" in path,
                "is_discarded": path == "/dev/null",
                "is_pipe": typ in ("PIPE", "FIFO"),
            }
        if typ in _INTERESTING_TYPES and acc in _WRITE_MODES and typ == "REG":
            try:
                size = os.path.getsize(path)
                age = round(max(0.0, time.time() - os.path.getmtime(path)), 1)
            except Exception:
                size, age = None, None
            writable.append({"fd": fd, "path": path, "bytes": size,
                             "last_write_age_s": age})
    return {"available": True, "pid": int(pid), "reason": "",
            "streams": streams, "writable_files": writable,
            "all_fds": fds}


def _same_file(a: str, b: str) -> bool:
    """Compare paths by identity where possible: /private/tmp vs /tmp, symlinks
    and relative spellings all name the same file, and a mismatch reported over a
    spelling difference would be a false alarm."""
    if not a or not b:
        return False
    try:
        if os.path.samefile(a, b):
            return True
    except Exception:
        pass
    try:
        return Path(a).resolve() == Path(b).resolve()
    except Exception:
        return os.path.abspath(a) == os.path.abspath(b)


def reconcile(pid: int, configured: list[dict], *,
              stale_after_s: float = 120.0) -> dict:
    """Compare what the OS says against what the profile declared.

    `configured` is a list of {path, label} (the profile's effective sources).

    Returns a verdict naming three concrete failures, each of which silently
    wastes debugging time:
      missing_from_config  the process writes here and nobody is reading it
      not_written_by_proc  configured, but this process does NOT hold it open
      stale                configured and open, but nothing written recently
    plus `output_destination`, which answers "so where IS the output going?" —
    including the case that matters most, stdout/stderr being a terminal or a
    pipe rather than a file, which no log path can reveal.
    """
    det = detect_write_targets(pid)
    # isinstance guard, not just `or []`: a profile list can legitimately contain
    # a None or a non-dict after hand-editing, and this runs on a diagnostic path
    # where an exception would take out the very report meant to explain things.
    conf = [{"path": os.path.expanduser(c.get("path") or ""),
             "label": c.get("label") or ""}
            for c in (configured or [])
            if isinstance(c, dict) and (c.get("path") or "").strip()]

    if not det.get("available"):
        return {"available": False, "reason": det.get("reason"),
                "pid": pid, "configured": conf, "missing_from_config": [],
                "not_written_by_proc": [], "stale": [], "output_destination": []}

    open_paths = [w["path"] for w in det["writable_files"]]

    missing = []
    seen_paths: list[str] = []
    for w in det["writable_files"]:
        if any(_same_file(w["path"], c["path"]) for c in conf):
            continue
        # AgentVision's own bookkeeping files are not the program's output.
        low = w["path"].lower()
        if "agentvision_debug.log" in low or low.endswith(".av_frame_seq"):
            continue
        # One FILE, not one descriptor: stdout and stderr are routinely redirected
        # to the same file, and reporting it twice reads like two problems.
        if any(_same_file(w["path"], p) for p in seen_paths):
            for m in missing:
                if _same_file(m["path"], w["path"]):
                    m.setdefault("also_fds", []).append(w["fd"])
            continue
        seen_paths.append(w["path"])
        missing.append(dict(w))

    not_written = []
    stale = []
    for c in conf:
        held = any(_same_file(c["path"], p) for p in open_paths)
        try:
            age = round(max(0.0, time.time() - os.path.getmtime(c["path"])), 1)
        except Exception:
            age = None
        row = {"path": c["path"], "label": c["label"],
               "held_open_by_process": held, "last_write_age_s": age}
        if not held:
            not_written.append(row)
        elif age is not None and age > stale_after_s:
            stale.append(row)

    dest: list[str] = []
    for name in ("stdout", "stderr"):
        s = det["streams"].get(name) or {}
        if not s:
            continue
        if s.get("is_terminal"):
            dest.append(f"{name} -> TERMINAL {s.get('path')} (not captured to a file)")
        elif s.get("is_discarded"):
            dest.append(f"{name} -> /dev/null (DISCARDED)")
        elif s.get("is_pipe"):
            dest.append(f"{name} -> pipe (captured by whoever launched it)")
        elif s.get("is_file"):
            dest.append(f"{name} -> file {s.get('path')}")

    ok = not missing and not not_written and not stale
    return {
        "available": True, "pid": det["pid"], "ok": ok,
        "configured": conf,
        "missing_from_config": missing,
        "not_written_by_proc": not_written,
        "stale": stale,
        "output_destination": dest,
        "writable_files": det["writable_files"],
        "streams": det["streams"],
        "verdict": ("configured sources match what the process is writing" if ok
                    else "MISMATCH between configured log sources and where the "
                         "process is actually writing — see the lists"),
    }
