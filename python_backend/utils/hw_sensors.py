"""
hw_sensors — machine-wide hardware telemetry, one sample at a time.
================================================================================

Everything else in AgentVision watches ONE PROGRAM. This module watches the
MACHINE: CPU load and clocks, every temperature/fan/voltage/power sensor the OS
will surrender, GPU state, memory, and throttle flags. It exists for exactly one
consumer — modules/hw_blackbox.py, the hardware flight recorder that diagnoses
full-machine crashes (thermal shutdowns, PSU drops, machine-check faults) that
no per-program log can ever explain, because the whole OS dies with the program.

Design rules (same contract as platform_shim):
  • `read_sample()` NEVER raises and never blocks long — a sensor source that
    is missing, slow, or broken contributes nothing instead of an exception.
    The recorder must keep writing samples right up to the moment of a crash;
    a raise here would kill the one witness the diagnosis depends on.
  • Every OS-specific parser is a PURE function taking text/dict in and dict
    out, so the whole surface is unit-testable on any OS (test_hw_blackbox.py
    feeds captured Windows/Linux output on macOS).
  • Sources are best-effort and ADDITIVE: psutil is the floor (load/freq/mem
    everywhere), then per-OS sources layer on temps/fans/volts/power. The
    sample records which sources actually answered in `sources`, and
    `sensor_inventory()` tells the agent what is missing and how to add it —
    a verdict engine must know "no voltage data" is absence of evidence, not
    evidence of absence.

What each OS can give (measured reality, not aspiration):

  Linux   : the richest. hwmon exposes temps + fans + VOLTAGE RAILS
            (in*_input), RAPL exposes CPU package power, psutil wraps most of
            it. Voltage sag is the classic PSU-failure signature, so a Linux
            box can often be diagnosed from software alone.
  Windows : the OS itself exposes almost nothing useful (the WMI thermal zone
            is unimplemented on most consumer boards). LibreHardwareMonitor
            (free, open source) exposes EVERYTHING — temps, fans, rails,
            watts — over its local web server (data.json, default port 8085)
            or its WMI namespace. We read either; without LHM the sample
            degrades to psutil load/freq/mem + nvidia-smi.
  macOS   : temps/power need root (powermetrics) or a helper binary
            (osx-cpu-temp / smctemp, both brew-installable). Throttle state
            (`pmset -g therm`) is readable without root and is the load-
            bearing thermal signal. Mac desktops rarely die of PSU sag, so
            this is acceptable coverage.

nvidia-smi (all OSes): GPU temp/load/power/fan when an NVIDIA card + driver
are present. The GPU is the biggest transient load in a gaming PC — its power
spike is often what tips a marginal PSU over — so it earns its own query.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC     = sys.platform == "darwin"
IS_LINUX   = sys.platform.startswith("linux")

# Subprocess sensor reads must never stall the recorder loop: a hung helper
# would open a gap in the sample record exactly when the machine is straining.
_CMD_TIMEOUT = 3.0

# ── LibreHardwareMonitor / OpenHardwareMonitor web server ─────────────────────
# Options → Remote Web Server → Run inside LHM. 8085 is its default port.
LHM_URL = os.environ.get("AGENTVISION_LHM_URL", "http://127.0.0.1:8085/data.json")

# ── Linux sysfs roots (overridable) ───────────────────────────────────────────
# The kernel exposes these at fixed paths, but making them overridable lets a
# container/unusual mount relocate them and — just as importantly — lets the
# port test drive the real Linux dispatch path against a fake tree on any OS.
HWMON_ROOT    = os.environ.get("AGENTVISION_HWMON_ROOT", "/sys/class/hwmon")
POWERCAP_ROOT = os.environ.get("AGENTVISION_POWERCAP_ROOT", "/sys/class/powercap")


def _run(argv: list[str], timeout: float = _CMD_TIMEOUT) -> str:
    """Run a helper and return stdout, '' on any failure. Never raises."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def _to_float(s) -> float | None:
    """Tolerant float: handles '45.0 °C', '12,096 V' (comma-decimal locales),
    '1234 RPM'. None when no number is present."""
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(s))
    if not m:
        return None
    return float(m.group(0).replace(",", "."))


# ═════════════════════════════════════════════════════════════════════════════
#  Pure parsers — one per external format, unit-tested with captured output
# ═════════════════════════════════════════════════════════════════════════════

def parse_lhm_json(node: dict) -> dict:
    """Flatten a LibreHardwareMonitor / OpenHardwareMonitor data.json tree into
    {"temps": {...°C}, "fans": {...rpm}, "volts": {...V}, "power": {...W},
     "load": {...%}, "clocks": {...MHz}}.

    The tree is {Text, Value, Children:[...]}; sensor type is classified by the
    VALUE'S UNIT, not by the group heading — headings are localized (German LHM
    says "Temperaturen") while units are not. Keys are "Hardware/Sensor" paths
    kept short enough to read in a JSONL sample. Pure."""
    out = {"temps": {}, "fans": {}, "volts": {}, "power": {}, "load": {},
           "clocks": {}}

    def _walk(n: dict, path: list[str]) -> None:
        text = str(n.get("Text") or "").strip()
        val = n.get("Value")
        children = n.get("Children") or []
        # Only leaf nodes carry sensor readings; group nodes have Value == "".
        if val not in (None, "") and not children:
            v = _to_float(val)
            if v is not None:
                # Key = "Hardware/Sensor". The leaf's DIRECT parent is the
                # unit-group heading ("Temperatures"/"Voltages"/localized) —
                # redundant with the bucket, so it is skipped in favor of the
                # hardware node above it.
                parent_hw = next((p for p in reversed(path[:-1]) if p), "")
                key = "/".join(([parent_hw] if parent_hw else []) + [text])[:80]
                unit = str(val)
                if "°" in unit:                       # °C or °F
                    if "°F" in unit:
                        v = (v - 32.0) * 5.0 / 9.0
                    out["temps"][key] = round(v, 1)
                elif "RPM" in unit.upper():
                    out["fans"][key] = round(v)
                elif re.search(r"\bV\b", unit):
                    out["volts"][key] = round(v, 3)
                elif re.search(r"\bW\b", unit):
                    out["power"][key] = round(v, 1)
                elif "MHz" in unit:
                    out["clocks"][key] = round(v)
                elif "%" in unit:
                    out["load"][key] = round(v, 1)
        for c in children:
            if isinstance(c, dict):
                _walk(c, path + [text])

    if isinstance(node, dict):
        _walk(node, [])
    return out


def parse_nvidia_smi_csv(text: str) -> dict:
    """Parse `nvidia-smi --query-gpu=... --format=csv,noheader,nounits` output
    (one line per GPU; fields ordered as in _NVSMI_FIELDS). Multi-GPU machines
    report the HOTTEST/most-loaded card — for crash diagnosis the worst card is
    the interesting one. Pure."""
    best: dict = {}
    for line in (text or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        row = {"name": parts[0]}
        for key, raw in zip(("temp_c", "load_pct", "power_w", "fan_pct",
                             "mem_used_mb", "mem_total_mb"), parts[1:]):
            v = _to_float(raw)              # "[N/A]" → None
            if v is not None:
                row[key] = v
        if not best or (row.get("temp_c") or 0) > (best.get("temp_c") or 0):
            best = row
    return best


_NVSMI_FIELDS = ("name,temperature.gpu,utilization.gpu,power.draw,fan.speed,"
                 "memory.used,memory.total")


def parse_pmset_therm(text: str) -> dict:
    """Parse macOS `pmset -g therm`. Two shapes exist:
      Intel:         CPU_Speed_Limit = 100 / CPU_Available_CPUs = 8 / ...
      Apple Silicon: 'Note: No thermal warning level has been recorded' or a
                     recorded warning level line.
    Returns {} when nothing thermal is being reported (the healthy case).
    CPU_Speed_Limit < 100 IS the throttle signal. Pure."""
    out: dict = {}
    for m in re.finditer(r"(CPU_Speed_Limit|CPU_Available_CPUs|"
                         r"CPU_Scheduler_Limit)\s*=\s*(\d+)", text or ""):
        out[m.group(1).lower()] = int(m.group(2))
    m = re.search(r"thermal warning level.*?(\d+)", text or "", re.I)
    if m:
        out["thermal_warning_level"] = int(m.group(1))
    elif re.search(r"No thermal warning level", text or "", re.I):
        out["thermal_warning_level"] = 0
    return out


def parse_hwmon_tree(root: str | Path) -> dict:
    """Walk /sys/class/hwmon for the sensor files psutil does NOT wrap:
    VOLTAGE RAILS (in*_input, millivolts) and power (power*_input, microwatts).
    Labels come from the sibling *_label file when present, else the chip name
    plus channel index. Injectable root → unit-testable with a fake tree."""
    out = {"volts": {}, "power": {}}
    root = Path(root)
    try:
        chips = sorted(root.iterdir())
    except Exception:
        return out
    for chip_dir in chips:
        try:
            chip = (chip_dir / "name").read_text().strip()
        except Exception:
            chip = chip_dir.name
        for f in sorted(chip_dir.glob("in[0-9]*_input")):
            try:
                mv = float(f.read_text().strip())
            except Exception:
                continue
            label_f = chip_dir / f.name.replace("_input", "_label")
            try:
                label = label_f.read_text().strip()
            except Exception:
                label = f.name.replace("_input", "")
            out["volts"][f"{chip}/{label}"] = round(mv / 1000.0, 3)
        for f in sorted(chip_dir.glob("power[0-9]*_input")):
            try:
                uw = float(f.read_text().strip())
            except Exception:
                continue
            label_f = chip_dir / f.name.replace("_input", "_label")
            try:
                label = label_f.read_text().strip()
            except Exception:
                label = f.name.replace("_input", "")
            out["power"][f"{chip}/{label}"] = round(uw / 1_000_000.0, 1)
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Per-OS collectors (thin wrappers around the pure parsers)
# ═════════════════════════════════════════════════════════════════════════════

def _read_lhm_http() -> dict | None:
    """LibreHardwareMonitor/OpenHardwareMonitor web server, if running."""
    try:
        import urllib.request
        with urllib.request.urlopen(LHM_URL, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        parsed = parse_lhm_json(data)
        return parsed if any(parsed.values()) else None
    except Exception:
        return None


def _read_lhm_wmi() -> dict | None:
    """LHM/OHM WMI namespace via PowerShell — the fallback when the web server
    is not enabled but the app is running. ~1 s per query, so the recorder
    only uses it when HTTP said no (source choice is cached per process)."""
    if not IS_WINDOWS:
        return None
    for ns in ("root/LibreHardwareMonitor", "root/OpenHardwareMonitor"):
        text = _run(["powershell", "-NoProfile", "-Command",
                     f"Get-CimInstance -Namespace {ns} -ClassName Sensor "
                     "-ErrorAction Stop | "
                     "Select-Object SensorType,Name,Value | "
                     "ConvertTo-Json -Compress"], timeout=6.0)
        if not text.strip():
            continue
        try:
            rows = json.loads(text)
            if isinstance(rows, dict):
                rows = [rows]
        except Exception:
            continue
        out = {"temps": {}, "fans": {}, "volts": {}, "power": {},
               "load": {}, "clocks": {}}
        kind_map = {"Temperature": "temps", "Fan": "fans", "Voltage": "volts",
                    "Power": "power", "Load": "load", "Clock": "clocks"}
        for r in rows or []:
            bucket = kind_map.get(str(r.get("SensorType")))
            v = _to_float(r.get("Value"))
            if bucket and v is not None:
                out[bucket][str(r.get("Name"))[:80]] = round(v, 3)
        if any(out.values()):
            return out
    return None


def _read_nvidia_smi() -> dict:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {}
    return parse_nvidia_smi_csv(_run(
        [exe, f"--query-gpu={_NVSMI_FIELDS}",
         "--format=csv,noheader,nounits"]))


# RAPL power is an energy COUNTER (µJ); watts need a delta between two reads,
# so the previous reading is kept per-process. First call reports nothing.
_rapl_last: dict[str, tuple[float, float]] = {}


def _read_rapl_power() -> dict:
    """Linux CPU package power from /sys/class/powercap/intel-rapl (works on
    AMD too — the kernel driver reuses the interface)."""
    out: dict = {}
    base = Path(POWERCAP_ROOT)
    try:
        domains = sorted(base.glob("intel-rapl:[0-9]*"))
    except Exception:
        return out
    now = time.monotonic()
    for d in domains:
        try:
            uj = float((d / "energy_uj").read_text().strip())
            name = (d / "name").read_text().strip()
        except Exception:
            continue
        key = str(d)
        last = _rapl_last.get(key)
        _rapl_last[key] = (now, uj)
        if last:
            dt = now - last[0]
            duj = uj - last[1]
            if dt > 0.1 and duj >= 0:        # counter wrapped → skip one beat
                out[f"rapl/{name}"] = round(duj / dt / 1_000_000.0, 1)
    return out


def _mac_temp_helpers() -> dict:
    """Best-effort mac temperatures without root: osx-cpu-temp or smctemp
    (both `brew install`-able). Root gets powermetrics' die temperature."""
    temps: dict = {}
    if shutil.which("osx-cpu-temp"):
        v = _to_float(_run(["osx-cpu-temp"]))
        if v and v > 0:                       # prints 0.0°C when unsupported
            temps["cpu/osx-cpu-temp"] = round(v, 1)
    if not temps and shutil.which("smctemp"):
        v = _to_float(_run(["smctemp", "-c"]))
        if v and v > 0:
            temps["cpu/smctemp"] = round(v, 1)
    if not temps and hasattr(os, "geteuid") and os.geteuid() == 0:
        text = _run(["powermetrics", "-n", "1", "-i", "200",
                     "--samplers", "smc,thermal"], timeout=8.0)
        m = re.search(r"CPU die temperature:\s*([\d.]+)", text)
        if m:
            temps["cpu/powermetrics"] = round(float(m.group(1)), 1)
    return temps


# ═════════════════════════════════════════════════════════════════════════════
#  The sample
# ═════════════════════════════════════════════════════════════════════════════

# Which rich Windows source answered last time, so the recorder does not pay
# for a dead HTTP probe + a 1 s WMI query on every 2 s tick.
_win_source_cache: str | None = None


def read_sample() -> dict:
    """One machine-wide telemetry sample. Never raises; missing sources simply
    contribute nothing and are absent from `sources`.

    Shape (every key optional except ts/sources):
      {ts, cpu:{load_pct, per_core, freq_mhz, throttle}, mem:{...},
       temps:{label: °C}, fans:{label: rpm}, volts:{label: V},
       power:{label: W}, clocks:{label: MHz}, gpu:{...}, battery:{...},
       sources:[...]}
    """
    global _win_source_cache
    sample: dict = {"ts": round(time.time(), 3), "sources": []}

    # ── psutil floor: load / clocks / memory — available everywhere ──────────
    try:
        import psutil
        sample["cpu"] = {
            # interval=None → since the LAST call: right for a recorder loop.
            "load_pct": psutil.cpu_percent(interval=None),
            "per_core": psutil.cpu_percent(interval=None, percpu=True),
        }
        try:
            freq = psutil.cpu_freq()
            if freq and freq.current:
                sample["cpu"]["freq_mhz"] = round(freq.current)
        except Exception:
            pass
        vm = psutil.virtual_memory()
        sample["mem"] = {"used_pct": vm.percent,
                         "used_gb": round(vm.used / 2**30, 2),
                         "total_gb": round(vm.total / 2**30, 2)}
        try:
            batt = psutil.sensors_battery()
            if batt is not None:
                sample["battery"] = {"pct": round(batt.percent, 1),
                                     "plugged": bool(batt.power_plugged)}
        except Exception:
            pass
        sample["sources"].append("psutil")

        # psutil's own temp/fan wrappers (Linux mainly; no-ops elsewhere).
        try:
            temps = getattr(psutil, "sensors_temperatures", lambda: {})() or {}
            for chip, entries in temps.items():
                for e in entries:
                    if e.current is not None:
                        label = e.label or chip
                        sample.setdefault("temps", {})[
                            f"{chip}/{label}"[:80]] = round(e.current, 1)
        except Exception:
            pass
        try:
            fans = getattr(psutil, "sensors_fans", lambda: {})() or {}
            for chip, entries in fans.items():
                for e in entries:
                    if e.current is not None:
                        label = e.label or chip
                        sample.setdefault("fans", {})[
                            f"{chip}/{label}"[:80]] = round(e.current)
        except Exception:
            pass
    except Exception:
        pass

    # ── Per-OS enrichment ────────────────────────────────────────────────────
    try:
        if IS_LINUX:
            hw = parse_hwmon_tree(HWMON_ROOT)
            if hw["volts"]:
                sample.setdefault("volts", {}).update(hw["volts"])
            if hw["power"]:
                sample.setdefault("power", {}).update(hw["power"])
            if hw["volts"] or hw["power"]:
                sample["sources"].append("hwmon")
            rapl = _read_rapl_power()
            if rapl:
                sample.setdefault("power", {}).update(rapl)
                sample["sources"].append("rapl")

        elif IS_WINDOWS:
            rich = None
            if _win_source_cache in (None, "lhm-http"):
                rich = _read_lhm_http()
                if rich:
                    _win_source_cache = "lhm-http"
            if rich is None and _win_source_cache in (None, "lhm-wmi"):
                rich = _read_lhm_wmi()
                if rich:
                    _win_source_cache = "lhm-wmi"
            if rich is None:
                _win_source_cache = None      # keep probing — LHM may start later
            else:
                for bucket in ("temps", "fans", "volts", "power", "clocks"):
                    if rich.get(bucket):
                        sample.setdefault(bucket, {}).update(rich[bucket])
                sample["sources"].append(_win_source_cache)

        elif IS_MAC:
            therm = parse_pmset_therm(_run(["pmset", "-g", "therm"]))
            if therm:
                sample["cpu"] = sample.get("cpu", {})
                sample["cpu"]["throttle"] = therm
                sample["sources"].append("pmset")
            temps = _mac_temp_helpers()
            if temps:
                sample.setdefault("temps", {}).update(temps)
                sample["sources"].append("mac-temp-helper")
    except Exception:
        pass

    # ── GPU (any OS with an NVIDIA driver) ───────────────────────────────────
    try:
        gpu = _read_nvidia_smi()
        if gpu:
            sample["gpu"] = gpu
            sample["sources"].append("nvidia-smi")
    except Exception:
        pass

    return sample


def sensor_inventory() -> dict:
    """What this machine CAN report, what it CANNOT, and how to fix each gap.
    The verdict engine attaches these hints to any 'inconclusive' finding, so
    the user learns exactly which missing tool would have answered the
    question. Never raises."""
    sample = read_sample()
    have = {
        "cpu_load": bool(sample.get("cpu")),
        "temps": bool(sample.get("temps")),
        "fans": bool(sample.get("fans")),
        "voltage_rails": bool(sample.get("volts")),
        "power_draw": bool(sample.get("power")) or "power_w" in
                      (sample.get("gpu") or {}),
        "gpu": bool(sample.get("gpu")),
        "throttle_flag": bool((sample.get("cpu") or {}).get("throttle")),
    }
    hints: list[str] = []
    if IS_WINDOWS and not (have["temps"] and have["voltage_rails"]):
        hints.append(
            "Install LibreHardwareMonitor (free, open source), then enable "
            "Options → Remote Web Server → Run. That exposes CPU/GPU/VRM "
            "temperatures, fan RPM, voltage rails and package watts at "
            f"{LHM_URL} and this recorder picks them up automatically. "
            "Without it, thermal-vs-PSU diagnosis runs on far less evidence.")
    if IS_LINUX and not have["temps"]:
        hints.append("Run sensors-detect (lm_sensors) so hwmon exposes the "
                     "motherboard chip; temps/fans/rails then appear here.")
    if IS_MAC and not have["temps"]:
        hints.append("brew install osx-cpu-temp (Intel) or smctemp (Apple "
                     "Silicon) for CPU temperature without root.")
    if not have["gpu"] and shutil.which("nvidia-smi") is None:
        hints.append("No nvidia-smi found — GPU temp/power is only collected "
                     "on NVIDIA cards with the driver installed. (AMD on "
                     "Linux appears via hwmon instead.)")
    return {"platform": sys.platform, "have": have, "hints": hints,
            "sources_live": sample.get("sources", []),
            "sample_keys": sorted(k for k in sample
                                  if k not in ("ts", "sources"))}
