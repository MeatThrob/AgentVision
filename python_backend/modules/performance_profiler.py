"""
Module 7: Performance Profiler + Progression Tracker
Tracks metrics over time, detects trends, and annotates improvements/regressions.
"""
import time
import json
from pathlib import Path
from collections import deque
from dataclasses import dataclass, asdict


MAX_HISTORY = 100


@dataclass
class PerfSample:
    ts: float
    cpu: float
    ram_gb: float
    disk_free_gb: float
    test_pass: int
    test_fail: int
    build_ms: float


class ProgressionTracker:
    """
    Maintains a rolling window of PerfSamples.
    Persists to a JSON file so history survives restarts.
    """

    def __init__(self, history_file: str = ".agentvision_perf_history.json"):
        self._path = Path(history_file)
        self._samples: deque[PerfSample] = deque(maxlen=MAX_HISTORY)
        self._load()

    def record(self, sample: PerfSample):
        self._samples.append(sample)
        self._save()

    def trend_overlay(self) -> list[str]:
        """Returns 3–5 overlay lines showing trends."""
        if len(self._samples) < 2:
            return []
        lines = []
        first, last = self._samples[0], self._samples[-1]

        # CPU trend
        cpu_delta = last.cpu - first.cpu
        arrow = "▲" if cpu_delta > 5 else ("▼" if cpu_delta < -5 else "→")
        lines.append(f"CPU  {arrow}  {last.cpu:.0f}%")

        # RAM trend
        ram_delta = last.ram_gb - first.ram_gb
        arrow = "▲" if ram_delta > 0.5 else ("▼" if ram_delta < -0.5 else "→")
        lines.append(f"RAM  {arrow}  {last.ram_gb:.1f}GB")

        # Test progression
        if last.test_pass != first.test_pass or last.test_fail != first.test_fail:
            lines.append(
                f"Tests  {first.test_pass}→{last.test_pass} pass  "
                f"{first.test_fail}→{last.test_fail} fail")

        # Build time trend
        build_samples = [s for s in self._samples if s.build_ms > 0]
        if len(build_samples) >= 2:
            b1, b2 = build_samples[0].build_ms, build_samples[-1].build_ms
            arrow = "▲" if b2 > b1 * 1.1 else ("▼" if b2 < b1 * 0.9 else "→")
            lines.append(f"Build  {arrow}  {b2/1000:.1f}s")

        return lines

    def latest(self) -> PerfSample | None:
        return self._samples[-1] if self._samples else None

    def _save(self):
        try:
            data = [asdict(s) for s in self._samples]
            self._path.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _load(self):
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text())
                for d in data[-MAX_HISTORY:]:
                    self._samples.append(PerfSample(**d))
        except Exception:
            pass


def time_subprocess(cmd: list[str], cwd: str = ".") -> tuple[float, str]:
    """Run a command and return (elapsed_ms, output)."""
    import subprocess
    t0 = time.monotonic()
    r  = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=120)
    ms = (time.monotonic() - t0) * 1000
    return round(ms, 1), r.stdout + r.stderr
