"""
hw_blackbox — the hardware flight recorder for FULL-MACHINE crashes.
================================================================================

THE PROBLEM THIS SOLVES. When a PC hard-crashes (instant power-off, freeze,
spontaneous reboot), every ordinary diagnostic dies with it: the program log
stops mid-line, the screen recorder loses its buffer, and after the reboot an
AI agent finds NOTHING — the machine looks healthy precisely because the
evidence did not survive. Heat, PSU sag and CPU machine-checks all present as
the same symptom ("it just turned off"), and they are indistinguishable
without telemetry from the seconds BEFORE the lights went out.

So this module is a black box in the aviation sense:

  1. RECORD  — a background thread samples machine-wide hardware telemetry
     (utils/hw_sensors.py) every couple of seconds and appends it to a JSONL
     file, flushing AND fsync()ing every line. fsync is the whole product:
     a hard power cut loses only what the OS hadn't reached the platter/NAND
     with, so the last fsynced sample is at most one interval old. That is
     the sample the diagnosis reads.
  2. DETECT  — every run writes session.json at start and marks it clean at
     stop. On the next start, an unclean previous session + an OS boot time
     NEWER than the last sample = the whole machine went down while we were
     recording. (Boot time older than the last sample = only the recorder
     was killed — reported, but not called a machine crash.)
  3. COLLECT — the OS's own post-mortem sources are gathered right then,
     while they are fresh: Windows Event Log (Kernel-Power 41, WHEA-Logger,
     BugCheck 1001, EventLog 6008) + minidump listing; Linux journalctl of
     the PREVIOUS boot, /sys/fs/pstore, /var/crash; macOS `pmset -g log`
     shutdown-cause codes + .panic reports.
  4. JUDGE   — analyze() is a PURE function that scores the evidence into
     ranked causes (THERMAL / PSU-POWER / CPU-HARDWARE / DRIVER-SOFTWARE /
     RAM / FAN-FAILURE) with the human-readable evidence lines behind each
     score, what data was MISSING, and the concrete next step per cause.
     Pure so the whole rulebook is unit-tested with synthetic crashes.
  5. FREEZE  — everything lands in crashes/crash-<stamp>/ as a capsule
     (tail of the samples + post-mortem + report.json/report.md), exempt
     from retention, exactly like the visual flight recorder's incidents.

The rulebook is the distilled version of how humans read HWiNFO logs and
event logs today (sources in docs/HARDWARE_BLACKBOX.md):
  • temps climbing to Tj-max right before the record stops   → thermal
  • record stops cleanly at NORMAL temps, no bugcheck logged  → PSU/power
    (the classic "clean drop at normal temperature" signature; Kernel-Power
    41 with BugcheckCode 0 says the same thing from the OS side)
  • 12V/5V/3.3V rail readings below ATX minimums (-5%)        → PSU/power
  • WHEA-Logger / MCE machine-check records                   → CPU/board
  • bugcheck code / minidump / kernel panic present           → driver/OS
  • EDAC/ECC memory error records                             → RAM
  • fan at 0 RPM while temps climb                            → fan failure
Software polling cannot catch sub-interval transients (a microsecond 12V dip
is invisible at any polling rate) — so absence of a smoking gun NEVER yields
"no hardware fault", only a ranked "most consistent with", with the sampling
limit stated. The report says what it does not know.

Runs two ways:
  • inside the bridge (started at boot of bridge_server, av_hw_* MCP tools)
  • STANDALONE on the crashing machine — the friend's-PC case: the box that
    crashes wants the recorder from OS startup, not only while a bridge runs:
        python -m python_backend.modules.hw_blackbox --run
        python -m python_backend.modules.hw_blackbox --report
    (autostart recipes per OS are in docs/HARDWARE_BLACKBOX.md)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC     = sys.platform == "darwin"
IS_LINUX   = sys.platform.startswith("linux")

# ── Tunables (env-overridable, same pattern as the bridge recorder) ───────────
BLACKBOX_INTERVAL_S = float(os.environ.get("AGENTVISION_HW_INTERVAL_S", "2.0"))
#: byte budget for the sample store — ~4 KB/sample ⇒ 256 MiB ≈ 17 months at 2 s.
BLACKBOX_MAX_BYTES = int(os.environ.get("AGENTVISION_HW_MAX_BYTES",
                                        str(256 * 1024 * 1024)))
#: how much pre-crash telemetry a capsule freezes.
CAPSULE_TAIL_S = float(os.environ.get("AGENTVISION_HW_CAPSULE_TAIL_S", "900"))
CAPSULE_CAP    = int(os.environ.get("AGENTVISION_HW_CAPSULE_CAP", "40"))

# ── Default thresholds for the verdict rulebook ───────────────────────────────
# ATX spec is ±5% on every rail; "crit" temps are conservative consumer-CPU/GPU
# shutdown territory (Tj-max is 95-105 °C on modern parts; VRM/hotspot higher).
THRESHOLDS = {
    "cpu_temp_crit": 95.0, "gpu_temp_crit": 103.0, "generic_temp_crit": 108.0,
    "temp_high": 85.0,           # "already hot" — slope + this = thermal story
    "temp_slope_c": 10.0,        # rise over the last 5 min worth calling a climb
    "rail_min": {"12": 11.4, "5": 4.75, "3.3": 3.135},
    "rail_sag_pct": 4.0,         # dip vs session median even inside ATX limits
    "fan_stall_temp": 80.0,      # 0 RPM is only damning while something is hot
    "power_spike_x": 1.5,        # last-sample watts vs session median
}


def _utc_stamp(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(),
                                tz=timezone.utc)
    return dt.strftime("%Y%m%d-%H%M%S")


def _run(argv: list[str], timeout: float = 10.0) -> str:
    """Helper output or '' — post-mortem collectors must never raise."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           errors="replace", timeout=timeout)
        return r.stdout or ""
    except Exception:
        return ""


def default_dir() -> Path:
    """Where the black box lives: env override, else <repo>/log/blackbox —
    alongside the observer log, per-host so a synced folder can hold several
    machines' boxes without collision."""
    env = os.environ.get("AGENTVISION_BLACKBOX_DIR", "").strip()
    if env:
        return Path(env)
    repo = Path(__file__).resolve().parent.parent.parent
    return repo / "log" / "blackbox" / socket.gethostname()


# ═════════════════════════════════════════════════════════════════════════════
#  Post-mortem collectors — per-OS, thin subprocess wrappers + PURE parsers
# ═════════════════════════════════════════════════════════════════════════════

# Windows event queries: (label, provider-or-id XPath, max events). Kernel-Power
# 41 is "the OS did not shut down cleanly" (its BugcheckCode field is the fork:
# 0 = abrupt power loss/reset, nonzero = a bluescreen preceded the reboot);
# 6008 is the EventLog service noticing the same; 1001 carries the bugcheck
# string; WHEA-Logger records are hardware machine-checks (CPU/cache/bus/PCIe);
# Kernel-Processor-Power 37 is firmware-forced core throttling.
_WIN_QUERIES = [
    ("kernel_power_41",  "*[System[Provider[@Name='Microsoft-Windows-Kernel-Power'] and (EventID=41)]]", 6),
    ("eventlog_6008",    "*[System[(EventID=6008)]]", 4),
    ("bugcheck_1001",    "*[System[Provider[@Name='Microsoft-Windows-WER-SystemErrorReporting'] and (EventID=1001)]]", 4),
    ("whea",             "*[System[Provider[@Name='Microsoft-Windows-WHEA-Logger']]]", 10),
    ("cpu_throttle_37",  "*[System[Provider[@Name='Microsoft-Windows-Kernel-Processor-Power'] and (EventID=37)]]", 4),
]


def parse_win_bugcheck_code(kernel_power_text: str) -> int | None:
    """BugcheckCode out of a Kernel-Power 41 /f:text rendering. 0 means the OS
    recorded NO stop code — consistent with abrupt power loss/reset. Pure."""
    m = re.search(r"BugcheckCode:?\s*(\d+)", kernel_power_text or "", re.I)
    return int(m.group(1)) if m else None


def collect_windows_postmortem() -> dict:
    out: dict = {"os": "windows", "events": {}}
    for label, xpath, count in _WIN_QUERIES:
        text = _run(["wevtutil", "qe", "System", f"/q:{xpath}",
                     "/rd:true", f"/c:{count}", "/f:text"], timeout=20.0)
        text = text.strip()
        if text:
            out["events"][label] = text[:8000]
    kp = out["events"].get("kernel_power_41", "")
    if kp:
        out["bugcheck_code"] = parse_win_bugcheck_code(kp)
    dump_dir = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Minidump"
    try:
        dumps = sorted(dump_dir.glob("*.dmp"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:5]
        out["minidumps"] = [{"file": d.name,
                             "mtime": _utc_stamp(d.stat().st_mtime)}
                            for d in dumps]
    except Exception:
        out["minidumps"] = []
    return out


_LINUX_HW_PAT = re.compile(
    r"mce|machine check|hardware error|edac|ecc|thermal|critical temperature|"
    r"over[- ]?current|under[- ]?voltage|watchdog", re.I)


def grep_hw_lines(text: str, cap: int = 80) -> list[str]:
    """The hardware-relevant lines out of a kernel/journal dump. Pure."""
    hits = [ln.strip() for ln in (text or "").splitlines()
            if _LINUX_HW_PAT.search(ln)]
    return hits[:cap]


def collect_linux_postmortem() -> dict:
    out: dict = {"os": "linux"}
    # The tail of the PREVIOUS boot — the last things the kernel said before
    # going down. -b -1 requires persistent journald (default on most distros;
    # Artix/openrc boxes may need syslog-ng equivalents — reported as absent).
    tail = _run(["journalctl", "-b", "-1", "-n", "300", "--no-pager",
                 "-o", "short-iso"], timeout=20.0)
    if tail.strip():
        out["prev_boot_tail"] = tail[-12000:]
        out["prev_boot_hw_lines"] = grep_hw_lines(tail)
    kerr = _run(["journalctl", "-b", "-1", "-k", "-p", "err",
                 "--no-pager", "-n", "150"], timeout=20.0)
    if kerr.strip():
        out["prev_boot_kernel_errors"] = kerr[-8000:]
    # pstore: panic/oops frames the kernel persisted across the reboot itself.
    try:
        pstore = sorted(Path("/sys/fs/pstore").iterdir())
        out["pstore"] = []
        for p in pstore[:8]:
            entry = {"file": p.name}
            try:
                entry["head"] = p.read_text(errors="replace")[:2000]
            except Exception:
                pass
            out["pstore"].append(entry)
    except Exception:
        out["pstore"] = []
    try:
        out["var_crash"] = [p.name for p in
                            sorted(Path("/var/crash").iterdir())[:10]]
    except Exception:
        out["var_crash"] = []
    if shutil.which("ras-mc-ctl"):
        s = _run(["ras-mc-ctl", "--summary"], timeout=15.0)
        if s.strip():
            out["rasdaemon_summary"] = s[:4000]
    return out


# macOS shutdown-cause codes, from Apple support lore + kernel headers. The
# NEGATIVE codes are hardware-initiated — exactly the split this module needs.
MAC_SHUTDOWN_CAUSES = {
    5: "clean software shutdown",
    3: "hard shutdown (power button held)",
    0: "power disconnected / power supply lost",
    -3: "multiple temperature sensors exceeded limits (thermal)",
    -60: "bad master directory block / watchdog",
    -61: "watchdog timer detected unresponsive OS (software hang)",
    -62: "watchdog timer detected unresponsive OS (software hang)",
    -64: "kernel panic (see .panic report)",
    -71: "memory/SO-DIMM overtemperature (thermal)",
    -74: "battery overtemperature (thermal)",
    -75: "AC adapter communication problem (power)",
    -78: "AC adapter incorrect current (power)",
    -79: "battery incorrect current (power)",
    -86: "proximity/system overtemperature (thermal)",
    -95: "CPU overtemperature (thermal)",
    -100: "power supply overtemperature (thermal/power)",
    -103: "battery voltage below critical (power)",
    -112: "coprocessor/system fault",
    -128: "unknown hardware cause",
}


def parse_mac_shutdown_causes(pmset_log_text: str, cap: int = 10) -> list[dict]:
    """'Shutdown Cause: N' entries (newest last) out of `pmset -g log`. Pure."""
    out: list[dict] = []
    for m in re.finditer(
            r"^(\S+ \S+ \S+)\s.*?[Ss]hutdown [Cc]ause:?\s*(-?\d+)",
            pmset_log_text or "", re.M):
        code = int(m.group(2))
        out.append({"when": m.group(1), "code": code,
                    "meaning": MAC_SHUTDOWN_CAUSES.get(
                        code, "unknown code (negative = hardware-initiated)")})
    return out[-cap:]


def collect_macos_postmortem() -> dict:
    out: dict = {"os": "macos"}
    log = _run(["pmset", "-g", "log"], timeout=25.0)
    causes = parse_mac_shutdown_causes(log)
    if causes:
        out["shutdown_causes"] = causes
    try:
        reports = Path("/Library/Logs/DiagnosticReports")
        panics = sorted(reports.glob("*.panic*"),
                        key=lambda p: p.stat().st_mtime, reverse=True)[:5]
        out["panic_reports"] = [{"file": p.name,
                                 "mtime": _utc_stamp(p.stat().st_mtime)}
                                for p in panics]
    except Exception:
        out["panic_reports"] = []
    therm = _run(["pmset", "-g", "therm"])
    if therm.strip():
        out["therm_now"] = therm.strip()[:1000]
    return out


def collect_postmortem() -> dict:
    try:
        if IS_WINDOWS:
            return collect_windows_postmortem()
        if IS_LINUX:
            return collect_linux_postmortem()
        if IS_MAC:
            return collect_macos_postmortem()
    except Exception as exc:
        return {"os": sys.platform, "error": f"{type(exc).__name__}: {exc}"}
    return {"os": sys.platform}


# ═════════════════════════════════════════════════════════════════════════════
#  The verdict engine — PURE, the whole rulebook in one function
# ═════════════════════════════════════════════════════════════════════════════

def _max_temp(sample: dict) -> tuple[float | None, str]:
    """Hottest temperature in a sample and which sensor it was."""
    best, label = None, ""
    for k, v in (sample.get("temps") or {}).items():
        if isinstance(v, (int, float)) and (best is None or v > best):
            best, label = float(v), k
    g = (sample.get("gpu") or {}).get("temp_c")
    if isinstance(g, (int, float)) and (best is None or g > best):
        best, label = float(g), "gpu"
    return best, label


def _rail_nominal(label: str, volts: float) -> float | None:
    """Which ATX rail a voltage sensor is reporting, judged by its VALUE (the
    label naming is chaos across vendors). Returns the nominal (12/5/3.3) or
    None for non-rail sensors (Vcore ~1V, DRAM ~1.35V, battery, ...)."""
    for nominal in (12.0, 5.0, 3.3):
        if abs(volts - nominal) / nominal <= 0.15:
            return nominal
    lab = label.lower()
    if "+12" in lab or "12v" in lab:
        return 12.0
    if re.search(r"\+5v|5v\b", lab) and volts > 4.0:
        return 5.0
    if "3.3" in lab or "3v3" in lab:
        return 3.3
    return None


def _total_power(sample: dict) -> float | None:
    tot = 0.0
    seen = False
    for v in (sample.get("power") or {}).values():
        if isinstance(v, (int, float)):
            tot += float(v)
            seen = True
    g = (sample.get("gpu") or {}).get("power_w")
    if isinstance(g, (int, float)):
        tot += float(g)
        seen = True
    return round(tot, 1) if seen else None


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def analyze(samples: list[dict], postmortem: dict,
            machine_rebooted: bool = True,
            thresholds: dict | None = None) -> dict:
    """Score the evidence into ranked causes. PURE — no I/O, no clock reads —
    so test_hw_blackbox.py can feed it synthetic crashes and pin every rule.

    `samples` is the pre-crash telemetry tail, oldest→newest (the last element
    is the final fsynced sample before the lights went out). `postmortem` is a
    collect_*_postmortem() dict. Returns {verdicts:[{cause, score, confidence,
    evidence:[...], next_steps:[...]}...], missing_data:[...], summary}."""
    th = dict(THRESHOLDS)
    th.update(thresholds or {})
    score: dict[str, float] = {}
    ev: dict[str, list[str]] = {}

    def add(cause: str, pts: float, line: str) -> None:
        score[cause] = score.get(cause, 0.0) + pts
        ev.setdefault(cause, []).append(line)

    last_ts = samples[-1]["ts"] if samples else None

    def age(s: dict) -> str:
        if last_ts is None:
            return "?"
        return f"T-{max(0.0, last_ts - s['ts']):.0f}s"

    # ── Telemetry rules (need samples) ───────────────────────────────────────
    final = [s for s in samples if last_ts is not None
             and last_ts - s["ts"] <= 120.0]
    recent = [s for s in samples if last_ts is not None
              and last_ts - s["ts"] <= 600.0]

    # 1. Temperature at/near shutdown limits in the final two minutes.
    hottest, hot_s, hot_label = None, None, ""
    for s in final:
        t, label = _max_temp(s)
        if t is not None and (hottest is None or t > hottest):
            hottest, hot_s, hot_label = t, s, label
    if hottest is not None and hot_s is not None:
        crit = (th["gpu_temp_crit"] if hot_label == "gpu"
                else th["cpu_temp_crit"] if "cpu" in hot_label.lower()
                or "core" in hot_label.lower() or "tctl" in hot_label.lower()
                else th["generic_temp_crit"])
        if hottest >= crit:
            add("thermal", 3.0,
                f"{hot_label} reached {hottest:.0f}°C at {age(hot_s)} — at/"
                f"above protective-shutdown territory (limit ~{crit:.0f}°C)")

    # 2. Temperature climbing into the crash (slope over the last 5 minutes).
    slope_win = [s for s in samples if last_ts is not None
                 and last_ts - s["ts"] <= 300.0]
    if len(slope_win) >= 2:
        t0, l0 = _max_temp(slope_win[0])
        t1, l1 = _max_temp(slope_win[-1])
        if t0 is not None and t1 is not None:
            if t1 - t0 >= th["temp_slope_c"] and t1 >= th["temp_high"]:
                add("thermal", 2.0,
                    f"max temp climbed {t0:.0f}°C → {t1:.0f}°C ({l1}) over the "
                    f"final {last_ts - slope_win[0]['ts']:.0f}s and the record "
                    "ends mid-climb")

    # 3. Fan stalled while hot.
    for s in final:
        t, _ = _max_temp(s)
        if t is None or t < th["fan_stall_temp"]:
            continue
        for label, rpm in (s.get("fans") or {}).items():
            if isinstance(rpm, (int, float)) and rpm == 0:
                add("fan_failure", 3.0,
                    f"fan '{label}' at 0 RPM while max temp was {t:.0f}°C "
                    f"({age(s)}) — dead/disconnected fan overheats the zone "
                    "it serves")
                break
        else:
            continue
        break

    # 4. Rails below ATX minimums, or sagging vs the session's own median.
    rail_hist: dict[str, list[float]] = {}
    for s in samples:
        for label, v in (s.get("volts") or {}).items():
            if isinstance(v, (int, float)):
                rail_hist.setdefault(label, []).append(float(v))
    for label, vals in rail_hist.items():
        nominal = _rail_nominal(label, _median(vals) or 0.0)
        if nominal is None:
            continue
        vmin = min(vals[-max(1, len(vals) // 2):])   # the newer half
        floor = th["rail_min"].get(f"{nominal:g}")
        if floor and vmin < floor:
            add("psu_power", 3.0,
                f"rail '{label}' fell to {vmin:.2f}V (ATX minimum for "
                f"{nominal:g}V is {floor}V) — out-of-spec supply under load")
        else:
            med = _median(vals) or 0.0
            if med > 0 and (med - vmin) / med * 100.0 >= th["rail_sag_pct"]:
                add("psu_power", 2.0,
                    f"rail '{label}' sagged {(med - vmin) / med * 100:.1f}% "
                    f"below its session median ({med:.2f}V → {vmin:.2f}V) — "
                    "inside ATX limits but a real droop under load")

    # 5. Power spike right at the end (a load transient tipping a weak PSU).
    powers = [p for p in (_total_power(s) for s in recent) if p is not None]
    if len(powers) >= 5:
        med = _median(powers[:-1]) or 0.0
        if med > 0 and powers[-1] >= med * th["power_spike_x"]:
            add("psu_power", 2.0,
                f"total measured draw jumped to {powers[-1]:.0f}W in the final "
                f"sample vs a session median of {med:.0f}W — a transient a "
                "marginal PSU/cable would drop out on")

    # 6. The classic PSU signature: record ends abruptly at NORMAL temps with
    #    no stop code recorded by the OS. Only meaningful if the machine
    #    actually went down and we HAVE temperature data to call "normal".
    bugcheck = postmortem.get("bugcheck_code")
    kernel_says_software = bool(
        (bugcheck not in (None, 0))
        or postmortem.get("minidumps")
        or postmortem.get("panic_reports")
        or postmortem.get("var_crash")
        or any("panic" in (p.get("file", "") + p.get("head", "")).lower()
               for p in postmortem.get("pstore", [])))
    if machine_rebooted and final and not kernel_says_software:
        temps_at_end = [_max_temp(s)[0] for s in final]
        temps_at_end = [t for t in temps_at_end if t is not None]
        if temps_at_end and max(temps_at_end) < th["temp_high"]:
            add("psu_power", 2.0,
                f"telemetry stops abruptly with all temps ≤"
                f"{max(temps_at_end):.0f}°C and the OS recorded no stop code "
                "— the classic power-cut signature (PSU, cable, mains)")

    # ── Post-mortem rules (work even with zero samples) ──────────────────────
    events = postmortem.get("events") or {}
    if events.get("whea"):
        add("cpu_hardware", 4.0,
            "WHEA-Logger machine-check events in the System log — the CPU/"
            "board reported an internal hardware error to Windows")
    hw_lines = postmortem.get("prev_boot_hw_lines") or []
    mce_lines = [ln for ln in hw_lines
                 if re.search(r"mce|machine check|hardware error", ln, re.I)]
    if mce_lines:
        add("cpu_hardware", 4.0,
            f"MCE/hardware-error lines in the previous boot's kernel log, "
            f"e.g.: {mce_lines[0][:160]}")
    edac_lines = [ln for ln in hw_lines if re.search(r"edac|ecc", ln, re.I)]
    if edac_lines:
        add("ram", 3.0,
            f"EDAC/ECC memory-error lines in the previous boot's kernel log, "
            f"e.g.: {edac_lines[0][:160]}")
    thermal_lines = [ln for ln in hw_lines
                     if re.search(r"critical temperature|thermal", ln, re.I)]
    if thermal_lines:
        add("thermal", 2.0,
            f"thermal warnings in the previous boot's kernel log, e.g.: "
            f"{thermal_lines[0][:160]}")
    if bugcheck not in (None, 0):
        add("driver_software", 3.0,
            f"Kernel-Power 41 carries BugcheckCode {bugcheck} — a bluescreen "
            "preceded the reboot; the minidump names the faulting module")
    elif bugcheck == 0 and machine_rebooted:
        add("psu_power", 1.0,
            "Kernel-Power 41 with BugcheckCode 0 — Windows recorded NO stop "
            "code, consistent with abrupt power loss or a hard reset")
    if postmortem.get("minidumps"):
        add("driver_software", 2.0,
            f"{len(postmortem['minidumps'])} minidump(s) in "
            r"C:\Windows\Minidump — read the newest with WinDbg (!analyze -v)")
    if events.get("cpu_throttle_37"):
        add("thermal", 1.0,
            "Kernel-Processor-Power 37: firmware throttled the CPU — the "
            "platform was fighting heat or power delivery before the crash")
    for c in postmortem.get("shutdown_causes") or []:
        meaning = c.get("meaning", "")
        code = c.get("code")
        if "thermal" in meaning or "overtemperature" in meaning:
            add("thermal", 4.0,
                f"macOS shutdown cause {code} at {c.get('when')}: {meaning}")
        elif "power" in meaning or code == 0:
            add("psu_power", 3.0,
                f"macOS shutdown cause {code} at {c.get('when')}: {meaning}")
        elif "panic" in meaning or "watchdog" in meaning:
            add("driver_software", 2.0,
                f"macOS shutdown cause {code} at {c.get('when')}: {meaning}")
    if postmortem.get("panic_reports"):
        add("driver_software", 2.0,
            f"{len(postmortem['panic_reports'])} kernel .panic report(s) in "
            "/Library/Logs/DiagnosticReports")
    if postmortem.get("pstore"):
        add("driver_software", 2.0,
            f"{len(postmortem['pstore'])} pstore record(s) — the kernel "
            "persisted a panic/oops across the reboot (read the capsule)")

    # ── What we could NOT see ────────────────────────────────────────────────
    missing: list[str] = []
    if not samples:
        missing.append("no telemetry samples — the recorder was not running "
                       "before this crash; verdict rests on OS logs alone")
    else:
        if not any(s.get("temps") or (s.get("gpu") or {}).get("temp_c")
                   for s in samples):
            missing.append("no temperature data in the samples — thermal "
                           "rules could not run")
        if not any(s.get("volts") for s in samples):
            missing.append("no voltage-rail data — PSU sag rules could not "
                           "run (on Windows: install LibreHardwareMonitor "
                           "and enable its Remote Web Server)")
        if not any(s.get("fans") for s in samples):
            missing.append("no fan-RPM data — fan-failure rule could not run")
    missing.append("software polling cannot catch sub-interval transients: a "
                   "microsecond rail dip or an instant VRM trip is invisible "
                   f"at a {BLACKBOX_INTERVAL_S:g}s sample rate; absence of a "
                   "smoking gun does not clear the PSU")

    # ── Rank + package ───────────────────────────────────────────────────────
    NEXT_STEPS = {
        "thermal": [
            "Blow out heatsinks/filters and confirm every fan spins; repaste "
            "the CPU cooler if it is >3 years old",
            "Watch av_hw_metrics under load — if temps ramp toward the same "
            "peak again, the cooling path is the fault",
            "Check case airflow: a GPU dumping heat onto a passive VRM shows "
            "up as motherboard-sensor climb"],
        "psu_power": [
            "Reseat the 24-pin, EPS and PCIe power cables at BOTH ends; no "
            "daisy-chained PCIe cable to a high-draw GPU",
            "Try a known-good PSU of equal/higher wattage — it is the "
            "cheapest definitive test for this signature",
            "Rule out the wall: different outlet/circuit, no power strip/UPS "
            "in the path"],
        "cpu_hardware": [
            "Machine-check errors are CPU/board/cache-level: remove any "
            "overclock/undervolt including XMP/EXPO and retest",
            "Update BIOS/AGESA/microcode — many WHEA storms are fixed there",
            "If it persists at stock settings, RMA territory: test the CPU in "
            "another board if possible"],
        "driver_software": [
            "Read the newest minidump (WinDbg: !analyze -v) or panic report — "
            "it names the faulting driver/module",
            "Update/roll back the GPU driver first; it is the most common "
            "bluescreen source on gaming machines",
            "Uninstall recently added kernel-level software (anti-cheat, "
            "RGB/fan utilities, virtual drivers)"],
        "ram": [
            "Run MemTest86 overnight (or Windows Memory Diagnostic extended)",
            "Disable XMP/EXPO and retest at JEDEC speeds",
            "Test one stick at a time to isolate the bad module"],
        "fan_failure": [
            "Replace or reconnect the stalled fan (its header may also be "
            "dead — try another header)",
            "Until then set an aggressive fan curve on the remaining fans"],
    }
    CAUSE_TITLES = {
        "thermal": "THERMAL — overheating shutdown",
        "psu_power": "PSU / POWER — supply, cable or mains dropout",
        "cpu_hardware": "CPU / BOARD — machine-check hardware fault",
        "driver_software": "DRIVER / OS — kernel-level software crash",
        "ram": "RAM — memory errors",
        "fan_failure": "FAN FAILURE — cooling component dead",
    }
    verdicts = []
    for cause, pts in sorted(score.items(), key=lambda kv: -kv[1]):
        verdicts.append({
            "cause": cause,
            "title": CAUSE_TITLES.get(cause, cause),
            "score": round(pts, 1),
            "confidence": ("high" if pts >= 4 else
                           "medium" if pts >= 2.5 else "low"),
            "evidence": ev.get(cause, []),
            "next_steps": NEXT_STEPS.get(cause, []),
        })
    if not machine_rebooted:
        # A stopped recorder on a machine that stayed up is NOT a crash, no
        # matter what the telemetry looked like — any verdicts below are
        # informational anomalies, and the summary must not claim otherwise.
        summary = ("the machine did NOT reboot — only the recorder stopped; "
                   "this was not a machine crash"
                   + ("; telemetry anomalies below are informational"
                      if verdicts else ""))
    elif verdicts:
        top = verdicts[0]
        summary = (f"most consistent with {top['title']} "
                   f"(confidence: {top['confidence']}; "
                   f"{len(top['evidence'])} piece(s) of evidence)")
    else:
        summary = ("INCONCLUSIVE — no rule fired; see missing_data for what "
                   "would make the next crash diagnosable")
    return {"summary": summary, "verdicts": verdicts,
            "machine_rebooted": machine_rebooted, "missing_data": missing,
            "samples_analyzed": len(samples)}


def render_report_md(report: dict, crash_id: str = "") -> str:
    """The capsule's human/AI-readable face. Pure."""
    lines = [f"# Crash capsule {crash_id}".rstrip(), "",
             f"**{report.get('summary', '')}**", ""]
    if not report.get("machine_rebooted", True):
        lines.append("(The OS boot time predates the last sample — the "
                     "machine stayed up; only the recorder stopped.)")
        lines.append("")
    for v in report.get("verdicts", []):
        lines.append(f"## {v['title']}  — score {v['score']} "
                     f"({v['confidence']} confidence)")
        lines.append("")
        for e in v["evidence"]:
            lines.append(f"- {e}")
        if v.get("next_steps"):
            lines.append("")
            lines.append("Next steps:")
            for s in v["next_steps"]:
                lines.append(f"  1. {s}")
        lines.append("")
    md = report.get("missing_data") or []
    if md:
        lines.append("## What the recorder could not see")
        lines.append("")
        for m in md:
            lines.append(f"- {m}")
        lines.append("")
    lines.append(f"({report.get('samples_analyzed', 0)} telemetry sample(s) "
                 "analyzed — raw tail in tail_samples.jsonl, OS logs in "
                 "postmortem.json)")
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════════
#  The recorder
# ═════════════════════════════════════════════════════════════════════════════

class BlackboxRecorder:
    """Samples hw_sensors on a timer and writes the fsync'd JSONL black box.

    Files under `root`:
      session.json           {session_id, started, pid, clean_shutdown, ...}
      samples-YYYYMMDD.jsonl one line per sample, fsync'd (day files rotate;
                             oldest deleted past BLACKBOX_MAX_BYTES)
      crashes/crash-<id>/    frozen capsules (never auto-deleted)
    """

    def __init__(self, root: Path | None = None,
                 interval: float = BLACKBOX_INTERVAL_S):
        self.root = Path(root) if root else default_dir()
        self.interval = max(0.5, float(interval))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._fh = None
        self._fh_day = ""
        self._lock = threading.Lock()
        self.samples_written = 0
        self.last_sample: dict = {}
        self.alerts: list[dict] = []      # live warnings raised while running
        self.started_ts: float | None = None

    # ── session marker ───────────────────────────────────────────────────────
    def _session_path(self) -> Path:
        return self.root / "session.json"

    def _write_session(self, clean: bool) -> None:
        data = {"session_id": _utc_stamp(self.started_ts or time.time()),
                "started": self.started_ts, "pid": os.getpid(),
                "host": socket.gethostname(), "interval_s": self.interval,
                "clean_shutdown": clean}
        tmp = self._session_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        # fsync BEFORE the atomic swap: an unsynced rename can survive a crash
        # as an empty file, which would erase the very unclean-shutdown marker
        # this system exists to preserve.
        with open(tmp, "r+") as f:
            os.fsync(f.fileno())
        os.replace(tmp, self._session_path())

    # ── sample sink ──────────────────────────────────────────────────────────
    def _day_file(self) -> Path:
        return self.root / f"samples-{datetime.now(timezone.utc):%Y%m%d}.jsonl"

    def _append(self, sample: dict) -> None:
        path = self._day_file()
        with self._lock:
            if self._fh is None or self._fh_day != path.name:
                if self._fh:
                    try:
                        self._fh.close()
                    except Exception:
                        pass
                self._fh = open(path, "a", encoding="utf-8")
                self._fh_day = path.name
                self._enforce_budget()
            self._fh.write(json.dumps(sample, separators=(",", ":")) + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())      # THE point of the black box

    def _enforce_budget(self) -> None:
        """Drop the oldest day files once the store exceeds the byte budget.
        Capsules under crashes/ are never touched."""
        try:
            files = sorted(self.root.glob("samples-*.jsonl"))
            total = sum(f.stat().st_size for f in files)
            while files[:-1] and total > BLACKBOX_MAX_BYTES:
                oldest = files.pop(0)
                total -= oldest.stat().st_size
                oldest.unlink()
        except Exception:
            pass

    # ── live alerting (early warning while the machine is still up) ──────────
    def _check_alerts(self, sample: dict) -> None:
        t, label = _max_temp(sample)
        if t is not None and t >= THRESHOLDS["temp_high"]:
            self._alert("temp_high", f"{label} at {t:.0f}°C")
        throttle = (sample.get("cpu") or {}).get("throttle") or {}
        if 0 < throttle.get("cpu_speed_limit", 100) < 100:
            self._alert("throttling",
                        f"CPU speed limit {throttle['cpu_speed_limit']}%")
        for label, v in (sample.get("volts") or {}).items():
            nominal = _rail_nominal(label, v if isinstance(v, float) else 0.0)
            floor = nominal and THRESHOLDS["rail_min"].get(f"{nominal:g}")
            if floor and v < floor:
                self._alert("rail_low", f"{label} at {v:.2f}V")

    def _alert(self, kind: str, detail: str) -> None:
        # One live alert per kind per 5 minutes — a hot box must not flood.
        now = time.time()
        for a in self.alerts:
            if a["kind"] == kind and now - a["ts"] < 300:
                a["detail"], a["ts"], a["count"] = detail, now, a["count"] + 1
                return
        self.alerts.append({"kind": kind, "detail": detail, "ts": now,
                            "count": 1})
        self.alerts[:] = self.alerts[-20:]

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "crashes").mkdir(exist_ok=True)
        self.started_ts = time.time()
        self._write_session(clean=False)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="av-hw-blackbox")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval + 2)
        with self._lock:
            if self._fh:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
        # Only mark clean if THIS recorder actually started a session. A
        # stop() from an atexit hook on a process that died before start()
        # ran (port conflict, import error) must not overwrite the PREVIOUS
        # session's unclean marker — that marker may be the only evidence of
        # a real machine crash awaiting the startup check.
        if self.started_ts is not None:
            try:
                self._write_session(clean=True)
            except Exception:
                pass

    def _loop(self) -> None:
        from utils import hw_sensors
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                sample = hw_sensors.read_sample()
                self._append(sample)
                self.last_sample = sample
                self.samples_written += 1
                self._check_alerts(sample)
            except Exception:
                pass                          # next tick may succeed
            self._stop.wait(max(0.1, self.interval - (time.monotonic() - t0)))

    def status(self) -> dict:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "root": str(self.root), "interval_s": self.interval,
            "samples_written_session": self.samples_written,
            "last_sample": self.last_sample,
            "alerts": list(self.alerts),
            "store_bytes": sum((f.stat().st_size for f in
                                self.root.glob("samples-*.jsonl")), 0)
            if self.root.exists() else 0,
        }


# ═════════════════════════════════════════════════════════════════════════════
#  Boot-time crash detection + capsule freeze
# ═════════════════════════════════════════════════════════════════════════════

def read_recent_samples(root: Path, seconds: float,
                        now: float | None = None) -> list[dict]:
    """The last `seconds` worth of samples from the day files, oldest→newest.
    Reads at most the two newest files — a capsule tail never spans more."""
    root = Path(root)
    cutoff_now = now
    files = sorted(root.glob("samples-*.jsonl"))[-2:]
    rows: list[dict] = []
    for f in files:
        try:
            for line in f.read_text(errors="replace").splitlines():
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        except Exception:
            continue
    if not rows:
        return []
    if cutoff_now is None:
        cutoff_now = rows[-1].get("ts", 0.0)
    return [r for r in rows if isinstance(r.get("ts"), (int, float))
            and cutoff_now - r["ts"] <= seconds]


def _boot_time() -> float:
    try:
        import psutil
        return float(psutil.boot_time())
    except Exception:
        return 0.0


def check_previous_session(root: Path | None = None,
                           boot_ts: float | None = None,
                           pid_alive_fn=None,
                           postmortem_fn=None) -> dict | None:
    """Did the previous recorder session end in a machine crash? Called once
    at every startup, BEFORE the new session marker overwrites the old one.

    Returns a freshly built capsule dict {id, dir, report} when the previous
    session was unclean, else None. `boot_ts`/`pid_alive_fn`/`postmortem_fn`
    are injectable for tests (the real postmortem shells out to the OS)."""
    root = Path(root) if root else default_dir()
    spath = root / "session.json"
    try:
        session = json.loads(spath.read_text())
    except Exception:
        return None
    if session.get("clean_shutdown"):
        return None
    # Another live recorder (same box, bridge + standalone) is not a crash.
    pid = session.get("pid")
    if pid_alive_fn is None:
        def pid_alive_fn(p):
            try:
                import psutil
                return psutil.pid_exists(int(p))
            except Exception:
                return False
    if pid and pid_alive_fn(pid):
        return None

    tail = read_recent_samples(root, CAPSULE_TAIL_S)
    last_ts = tail[-1]["ts"] if tail else session.get("started") or 0.0
    boot = boot_ts if boot_ts is not None else _boot_time()
    # boot AFTER the last sample ⇒ the whole machine went down while recording.
    machine_rebooted = bool(boot and last_ts and boot > last_ts)

    postmortem = (postmortem_fn or collect_postmortem)()
    report = analyze(tail, postmortem, machine_rebooted=machine_rebooted)

    if not machine_rebooted:
        # The recorder died but the machine stayed up (killed process, crash
        # of AgentVision itself). Worth REPORTING once, not worth a capsule:
        # a kill -9'd bridge restarting daily would otherwise mint a fake
        # "crash" per restart and bury the real ones.
        return {"id": None, "dir": None, "report": report}

    crash_id = f"crash-{_utc_stamp(last_ts or None)}"
    cdir = root / "crashes" / crash_id
    try:
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "tail_samples.jsonl").write_text(
            "\n".join(json.dumps(s, separators=(",", ":")) for s in tail))
        (cdir / "postmortem.json").write_text(json.dumps(postmortem, indent=1))
        full = dict(report, id=crash_id,
                    previous_session=session,
                    last_sample_ts=last_ts, boot_ts=boot)
        (cdir / "report.json").write_text(json.dumps(full, indent=1))
        (cdir / "report.md").write_text(render_report_md(full, crash_id))
        _prune_capsules(root)
    except Exception:
        pass
    return {"id": crash_id, "dir": str(cdir), "report": report}


def _prune_capsules(root: Path) -> None:
    """Keep the newest CAPSULE_CAP capsules. A capsule is small (~1 MB), so
    the cap is generous; the point is only to bound a box that crashes daily
    for a year."""
    try:
        caps = sorted((root / "crashes").iterdir())
        for old in caps[:-CAPSULE_CAP]:
            shutil.rmtree(old, ignore_errors=True)
    except Exception:
        pass


def list_capsules(root: Path | None = None, limit: int = 10) -> list[dict]:
    root = Path(root) if root else default_dir()
    out: list[dict] = []
    try:
        caps = sorted((root / "crashes").iterdir(), reverse=True)[:limit]
    except Exception:
        return out
    for c in caps:
        row = {"id": c.name, "dir": str(c)}
        try:
            rep = json.loads((c / "report.json").read_text())
            row["summary"] = rep.get("summary", "")
            row["machine_rebooted"] = rep.get("machine_rebooted")
            row["top_cause"] = (rep.get("verdicts") or [{}])[0].get("cause")
        except Exception:
            row["summary"] = "(report.json unreadable)"
        out.append(row)
    return out


def load_capsule(crash_id: str, root: Path | None = None) -> dict | None:
    root = Path(root) if root else default_dir()
    cdir = root / "crashes" / re.sub(r"[^A-Za-z0-9_.-]", "", crash_id)
    try:
        report = json.loads((cdir / "report.json").read_text())
    except Exception:
        return None
    out = {"id": cdir.name, "report": report}
    try:
        out["postmortem"] = json.loads((cdir / "postmortem.json").read_text())
    except Exception:
        pass
    try:
        out["report_md"] = (cdir / "report.md").read_text()
    except Exception:
        pass
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Standalone CLI — for the machine that crashes (no bridge required)
# ═════════════════════════════════════════════════════════════════════════════

def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="hw_blackbox",
        description="AgentVision hardware flight recorder — run this on the "
                    "machine that crashes; read the capsule after it reboots.")
    ap.add_argument("--run", action="store_true",
                    help="record until Ctrl-C (checks for a crash of the "
                         "previous session first)")
    ap.add_argument("--report", action="store_true",
                    help="print the newest crash capsule report and exit")
    ap.add_argument("--check", action="store_true",
                    help="run the previous-session crash check and exit")
    ap.add_argument("--inventory", action="store_true",
                    help="show which sensor sources this machine can provide")
    ap.add_argument("--dir", default="", help="black-box folder "
                    "(default: <repo>/log/blackbox/<hostname>)")
    ap.add_argument("--interval", type=float, default=BLACKBOX_INTERVAL_S)
    args = ap.parse_args()
    root = Path(args.dir) if args.dir else default_dir()

    if args.inventory:
        from utils import hw_sensors
        print(json.dumps(hw_sensors.sensor_inventory(), indent=1))
        return 0
    if args.report:
        caps = list_capsules(root, limit=1)
        if not caps:
            print("no crash capsules recorded yet")
            return 0
        cap = load_capsule(caps[0]["id"], root)
        print(cap.get("report_md") or json.dumps(cap, indent=1))
        return 0
    if args.check:
        cap = check_previous_session(root)
        print(json.dumps(cap["report"], indent=1) if cap
              else "previous session ended cleanly (or no session found)")
        return 0
    if args.run:
        cap = check_previous_session(root)
        if cap:
            print(f"PREVIOUS SESSION CRASHED — capsule frozen: {cap['dir']}")
            print(f"  {cap['report']['summary']}")
        rec = BlackboxRecorder(root, interval=args.interval)
        rec.start()
        print(f"recording to {rec.root} every {rec.interval:g}s "
              "(fsync per sample) — Ctrl-C to stop cleanly")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            rec.stop()
            print("clean shutdown recorded")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
