"""
Unified clock for AgentVision and any bridged program.

Every timestamp emitted ANYWHERE in the system MUST come through here.
This guarantees that screenshots, log lines, JSONL records, and frame
metadata all line up on a single monotonic UTC timeline.

Two views of every instant:
    iso  — "YYYY-MM-DDTHH:MM:SS.mmmZ"  — human-grep-friendly UTC ISO-8601
    ms   — float epoch milliseconds    — machine-comparable, sortable

These two values describe the SAME moment. Always emit both.
"""

from __future__ import annotations

from datetime import datetime, timezone


def now() -> tuple[str, float]:
    """Return (iso_z, epoch_ms) for the current instant in UTC."""
    dt = datetime.now(timezone.utc)
    iso = dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    ms  = dt.timestamp() * 1000.0
    return iso, ms


def now_iso() -> str:
    """Just the ISO-Z string."""
    return now()[0]


def now_ms() -> float:
    """Just the epoch ms."""
    return now()[1]


def iso_to_ms(iso: str) -> float | None:
    """Parse ISO-Z (or ISO with offset) to epoch ms. Returns None on failure."""
    if not iso:
        return None
    try:
        s = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
        return datetime.fromisoformat(s).timestamp() * 1000.0
    except Exception:
        return None


def ms_to_iso(ms: float) -> str:
    """Format epoch ms back to ISO-Z."""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc) \
        .isoformat(timespec="milliseconds").replace("+00:00", "Z")
