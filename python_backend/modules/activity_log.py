"""
Module 10: Natural Language Activity Log
Translates raw system events into readable English timeline entries.
Thread-safe; can be written to from any module.
"""
import threading
import time
from datetime import datetime
from pathlib import Path
from shared.schema.snapshot_schema import ActivityEntry as ActivityLogEntry


class ActivityLogger:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if not cls._instance:
            cls._instance = super().__new__(cls)
            cls._instance._entries: list[ActivityLogEntry] = []
            cls._instance._file_path: Path | None = None
        return cls._instance

    def set_log_file(self, path: str):
        self._file_path = Path(path)

    def log(self, description: str):
        ts  = datetime.now().strftime("%H:%M:%S.%f")[:12]
        entry = ActivityLogEntry(ts=ts, description=description)
        with self._lock:
            self._entries.append(entry)
            if self._file_path:
                try:
                    with self._file_path.open("a") as f:
                        f.write(f"[{ts}] {description}\n")
                except Exception:
                    pass

    def get_recent(self, n: int = 20) -> list[ActivityLogEntry]:
        with self._lock:
            return list(self._entries[-n:])

    def clear(self):
        with self._lock:
            self._entries.clear()

    def tail_overlay(self, n: int = 5) -> list[str]:
        recent = self.get_recent(n)
        lines  = ["── Activity ──"]
        for e in recent:
            lines.append(f"  {e.ts}  {e.description[:55]}")
        return lines


# Module-level convenience
_logger = ActivityLogger()

def log(msg: str):
    _logger.log(msg)

def get_logger() -> ActivityLogger:
    return _logger


# ──────────────────────────────────────────────────────────────
# File-system watcher: auto-log file saves, git commits, etc.
# ──────────────────────────────────────────────────────────────
import subprocess
import threading as _th

class FileChangeWatcher:
    """
    Uses macOS FSEvents via fswatch if available, else polls.
    Logs file change events to ActivityLogger.
    """

    def __init__(self, watch_path: str, logger: ActivityLogger):
        self._watch_path = watch_path
        self._logger     = logger
        self._proc       = None
        self._thread     = None

    def start(self):
        import shutil
        if shutil.which("fswatch"):
            self._start_fswatch()
        else:
            self._start_polling()

    def stop(self):
        if self._proc:
            self._proc.terminate()

    def _start_fswatch(self):
        self._proc = subprocess.Popen(
            ["fswatch", "-r", "--event", "Updated",
             "--event", "Created", self._watch_path],
            stdout=subprocess.PIPE, text=True)

        def reader():
            for line in self._proc.stdout:
                path = line.strip()
                if path:
                    rel = Path(path).name
                    self._logger.log(f"File changed: {rel}")

        self._thread = _th.Thread(target=reader, daemon=True)
        self._thread.start()

    def _start_polling(self):
        # Simple mtime polling fallback
        mtimes: dict[str, float] = {}

        def poll():
            while True:
                time.sleep(2)
                try:
                    for p in Path(self._watch_path).rglob("*.py"):
                        mtime = p.stat().st_mtime
                        if str(p) in mtimes and mtimes[str(p)] != mtime:
                            self._logger.log(f"File saved: {p.name}")
                        mtimes[str(p)] = mtime
                except Exception:
                    pass

        self._thread = _th.Thread(target=poll, daemon=True)
        self._thread.start()
