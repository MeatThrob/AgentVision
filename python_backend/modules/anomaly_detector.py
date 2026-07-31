"""
Module 13: Anomaly Detector
Detects error spikes and anomalies in the connected program's log.
"""
from collections import deque
from shared.schema.snapshot_schema import AnomalyInfo

_ERROR_HISTORY: deque[bool] = deque(maxlen=10)


def detect_from_log(log_lines: list[str], err_msg: str) -> AnomalyInfo:
    """Detect anomalies from current log state."""
    anomaly = AnomalyInfo()
    has_error = bool(err_msg)
    _ERROR_HISTORY.append(has_error)

    if has_error:
        error_run = sum(1 for e in list(_ERROR_HISTORY)[-3:] if e)
        anomaly.detected    = True
        anomaly.type        = "error_in_log"
        anomaly.description = err_msg[:120]
        anomaly.severity    = "critical" if error_run >= 3 else "high"

    return anomaly


def anomaly_overlay(a: AnomalyInfo) -> list[str]:
    if not a.detected:
        return []
    icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}.get(
        a.severity, "⚠")
    return [
        f"{icon} ANOMALY: {a.type.upper().replace('_', ' ')}",
        f"   {a.description[:70]}",
    ]
