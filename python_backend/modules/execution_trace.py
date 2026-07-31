"""
Module 2: Execution Trace Recorder
Instruments a Python target via sys.settrace to record every call/return/exception.
Also reads the last N lines from any log file for passive trace collection.
"""
import sys
import time
import threading
from pathlib import Path
from dataclasses import dataclass

@dataclass
class TraceEvent:
    timestamp_ms: float = 0.0
    function: str = ""
    file: str = ""
    line: int = 0
    event_type: str = ""


class ExecutionTracer:
    """
    Wrap a callable to capture its full call trace.

    Usage:
        tracer = ExecutionTracer()
        tracer.start()
        run_your_code()
        events = tracer.stop()
    """

    def __init__(self, max_events: int = 500):
        self.max_events = max_events
        self._events: list[TraceEvent] = []
        self._lock = threading.Lock()
        self._active = False

    def start(self):
        self._events.clear()
        self._active = True
        sys.settrace(self._trace_handler)
        threading.settrace(self._trace_handler)

    def stop(self) -> list[TraceEvent]:
        self._active = False
        sys.settrace(None)
        threading.settrace(None)
        with self._lock:
            return list(self._events)

    def _trace_handler(self, frame, event, arg):
        if not self._active:
            return None
        if len(self._events) >= self.max_events:
            return None

        filename = frame.f_code.co_filename
        # Skip stdlib internals
        if "site-packages" in filename or filename.startswith("<"):
            return self._trace_handler

        ev = TraceEvent(
            timestamp_ms=time.time() * 1000,
            function=frame.f_code.co_qualname,
            file=Path(filename).name,
            line=frame.f_lineno,
            event_type=event,
        )
        with self._lock:
            self._events.append(ev)
        return self._trace_handler


def tail_log_file(log_path: str, lines: int = 50) -> list[str]:
    """Read last N lines from a log file."""
    try:
        path = Path(log_path)
        if not path.exists():
            return []
        text = path.read_text(errors="replace")
        return text.splitlines()[-lines:]
    except Exception:
        return []


def summarize_trace(events: list[TraceEvent]) -> str:
    """Produce a short call-chain summary for overlay."""
    seen: list[str] = []
    for ev in events:
        if ev.event_type == "call":
            entry = f"{ev.file}:{ev.line} {ev.function}()"
            if entry not in seen:
                seen.append(entry)
    chain = "\n→ ".join(seen[:8])
    if len(seen) > 8:
        chain += f"\n  …+{len(seen)-8} more calls"
    return chain
