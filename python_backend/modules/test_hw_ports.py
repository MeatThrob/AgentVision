#!/usr/bin/env python3
"""Cross-platform PORT verification for the hardware black box.

test_hw_blackbox.py proves the pure parsers and the verdict engine. This file
proves the OS-DISPATCH layer of hw_sensors.read_sample() and the post-mortem
collectors — the code that is normally dead on the machine running the test,
and therefore the code a port breaks. It does that three ways:

  1. REAL integration where the boundary is a network socket: a live local
     HTTP server serving a captured LibreHardwareMonitor data.json, driven
     through the actual Windows sensor client (_read_lhm_http). This runs on
     any OS and is the closest thing to a real Windows sensor read without
     Windows.
  2. FORCED dispatch: IS_WINDOWS / IS_LINUX are flipped on the module and the
     external boundary (subprocess `_run`, urllib, sysfs) is injected, so the
     real branch logic — source selection, the http→wmi→none fallback, the
     per-process source cache, sample assembly — actually executes.
  3. FAKE sysfs for the Linux hwmon/RAPL enrichment, exercised for real when
     this file is run on Linux (see run_all_tests / the Docker run).

Stdlib only; runs on macOS, Linux and Windows.
    python3 python_backend/modules/test_hw_ports.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import hw_sensors as hs
from modules import hw_blackbox as bb

_fails = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _fails
    if cond:
        print(f"  ok   {label}")
    else:
        _fails += 1
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


class _Restore:
    """Flip module attributes for the duration of a block, then restore — so a
    forced IS_WINDOWS can never leak into another test."""
    def __init__(self, mod, **kw):
        self.mod, self.kw, self.old = mod, kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(self.mod, k)
            setattr(self.mod, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(self.mod, k, v)


# A captured LibreHardwareMonitor data.json (trimmed to the shape the parser
# walks: Hardware → unit-group → sensor leaf, Value carrying the unit).
_LHM_DATA = {
    "Text": "Sensor", "Value": "", "Children": [{
        "Text": "MSI MPG B550", "Value": "", "Children": [
            {"Text": "AMD Ryzen 5 5600X", "Value": "", "Children": [
                {"Text": "Temperatures", "Value": "", "Children": [
                    {"Text": "Core (Tctl/Tdie)", "Value": "63.4 °C",
                     "Children": []}]},
                {"Text": "Powers", "Value": "", "Children": [
                    {"Text": "Package", "Value": "88.0 W", "Children": []}]},
            ]},
            {"Text": "Nuvoton NCT6797D", "Value": "", "Children": [
                {"Text": "Voltages", "Value": "", "Children": [
                    {"Text": "+12V", "Value": "12.096 V", "Children": []},
                    {"Text": "+5V", "Value": "5.040 V", "Children": []},
                    {"Text": "+3.3V", "Value": "3.312 V", "Children": []}]},
                {"Text": "Fans", "Value": "", "Children": [
                    {"Text": "CPU Fan", "Value": "1180 RPM", "Children": []}]},
            ]},
            {"Text": "NVIDIA GeForce RTX 3060", "Value": "", "Children": [
                {"Text": "Temperatures", "Value": "", "Children": [
                    {"Text": "GPU Core", "Value": "51.0 °C", "Children": []}]}]},
        ]}]}


def test_lhm_http_integration() -> None:
    print("Windows sensor read — LIVE HTTP against a fake LibreHardwareMonitor")
    body = json.dumps(_LHM_DATA).encode()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        with _Restore(hs, LHM_URL=f"http://127.0.0.1:{port}/data.json"):
            got = hs._read_lhm_http()
        check("the real HTTP client fetched + parsed data.json", got is not None)
        if got:
            check("12V rail read over HTTP",
                  any(abs(v - 12.096) < 0.001 for v in got["volts"].values()),
                  str(got["volts"]))
            check("CPU temp read over HTTP",
                  any(abs(v - 63.4) < 0.1 for v in got["temps"].values()),
                  str(got["temps"]))
            check("fan RPM read over HTTP",
                  1180 in got["fans"].values(), str(got["fans"]))
            check("package watts read over HTTP",
                  88.0 in got["power"].values(), str(got["power"]))
    finally:
        srv.shutdown()

    # A dead server → None, no raise (the recorder keeps probing).
    with _Restore(hs, LHM_URL="http://127.0.0.1:1/data.json"):
        check("LHM unreachable → None, never raises",
              hs._read_lhm_http() is None)


def test_windows_read_sample_dispatch() -> None:
    print("Windows read_sample() dispatch — forced IS_WINDOWS, injected sources")
    calls = {"http": 0, "wmi": 0}

    def fake_http():
        calls["http"] += 1
        return hs.parse_lhm_json(_LHM_DATA)

    def fake_wmi():
        calls["wmi"] += 1
        return {"temps": {"wmi/CPU": 60.0}, "fans": {}, "volts": {},
                "power": {}, "load": {}, "clocks": {}}

    # HTTP source wins.
    with _Restore(hs, IS_WINDOWS=True, IS_MAC=False, IS_LINUX=False,
                  _win_source_cache=None,
                  _read_lhm_http=fake_http, _read_lhm_wmi=fake_wmi,
                  _read_nvidia_smi=lambda: {}):
        s = hs.read_sample()
        check("sample built on the Windows branch",
              "psutil" in s["sources"], str(s["sources"]))
        check("LHM http enrichment merged in",
              "lhm-http" in s["sources"] and bool(s.get("volts")),
              str(s["sources"]))
        check("WMI not queried when HTTP answered", calls["wmi"] == 0)
        check("source cache remembered http", hs._win_source_cache == "lhm-http")

    # HTTP dead → WMI fallback.
    calls["http"] = calls["wmi"] = 0
    with _Restore(hs, IS_WINDOWS=True, IS_MAC=False, IS_LINUX=False,
                  _win_source_cache=None,
                  _read_lhm_http=lambda: None, _read_lhm_wmi=fake_wmi,
                  _read_nvidia_smi=lambda: {}):
        s = hs.read_sample()
        check("WMI fallback used when HTTP is down",
              "lhm-wmi" in s["sources"], str(s["sources"]))
        check("source cache remembered wmi", hs._win_source_cache == "lhm-wmi")

    # Neither → psutil-only, cache resets so it keeps probing (LHM may start).
    with _Restore(hs, IS_WINDOWS=True, IS_MAC=False, IS_LINUX=False,
                  _win_source_cache=None,
                  _read_lhm_http=lambda: None, _read_lhm_wmi=lambda: None,
                  _read_nvidia_smi=lambda: {}):
        s = hs.read_sample()
        check("no LHM → psutil-only sample, no crash",
              s["sources"] == ["psutil"], str(s["sources"]))
        check("cache reset to None so a later LHM start is picked up",
              hs._win_source_cache is None)


def test_lhm_wmi_parsing() -> None:
    print("Windows sensor read — LHM WMI PowerShell output parsing")
    # What `Get-CimInstance -Namespace root/LibreHardwareMonitor Sensor |
    # ConvertTo-Json` emits (a list of rows; a single row would be a bare dict).
    rows = [
        {"SensorType": "Temperature", "Name": "CPU Core", "Value": 64.0},
        {"SensorType": "Voltage", "Name": "+12V", "Value": 11.9},
        {"SensorType": "Fan", "Name": "CPU Fan", "Value": 1200.0},
        {"SensorType": "Power", "Name": "CPU Package", "Value": 90.5},
        {"SensorType": "Load", "Name": "CPU Total", "Value": 42.0},
    ]

    def fake_run(argv, timeout=6.0):
        # Only the LibreHardwareMonitor namespace answers; OpenHardwareMonitor
        # returns nothing (the real fallback order).
        if "root/LibreHardwareMonitor" in " ".join(argv):
            return json.dumps(rows)
        return ""

    with _Restore(hs, IS_WINDOWS=True, _run=fake_run):
        out = hs._read_lhm_wmi()
    check("WMI rows parsed", out is not None)
    if out:
        check("temp/volt/fan/power/load routed by SensorType",
              out["temps"].get("CPU Core") == 64.0
              and out["volts"].get("+12V") == 11.9
              and out["fans"].get("CPU Fan") == 1200.0
              and out["power"].get("CPU Package") == 90.5
              and out["load"].get("CPU Total") == 42.0, str(out))

    # A single-sensor machine returns a bare dict, not a list — must not crash.
    def fake_run_single(argv, timeout=6.0):
        if "root/LibreHardwareMonitor" in " ".join(argv):
            return json.dumps({"SensorType": "Temperature",
                               "Name": "CPU", "Value": 55.0})
        return ""

    with _Restore(hs, IS_WINDOWS=True, _run=fake_run_single):
        out = hs._read_lhm_wmi()
    check("single-row WMI dict handled (not just a list)",
          out is not None and out["temps"].get("CPU") == 55.0, str(out))


def test_windows_postmortem_collector() -> None:
    print("Windows post-mortem collector — forced IS_WINDOWS, injected wevtutil")
    kp41 = ("Event[0]:\n  Log Name: System\n  Source: Microsoft-Windows-"
            "Kernel-Power\n  Event ID: 41\n  Level: Critical\n  Description: "
            "The system has rebooted without cleanly shutting down first.\n"
            "  BugcheckCode: 0\n")
    whea = ("Event[0]:\n  Source: Microsoft-Windows-WHEA-Logger\n  Event ID: "
            "18\n  A fatal hardware error has occurred. Reported by component: "
            "Processor Core.\n")

    def fake_run(argv, timeout=20.0):
        q = " ".join(argv)
        if "Kernel-Power" in q:
            return kp41
        if "WHEA-Logger" in q:
            return whea
        return ""

    with _Restore(bb, IS_WINDOWS=True, IS_MAC=False, IS_LINUX=False,
                  _run=fake_run):
        pm = bb.collect_windows_postmortem()
    check("collector ran the Windows branch", pm.get("os") == "windows")
    check("Kernel-Power 41 captured", "kernel_power_41" in pm.get("events", {}))
    check("BugcheckCode 0 extracted from the event text",
          pm.get("bugcheck_code") == 0, str(pm.get("bugcheck_code")))
    check("WHEA event captured", "whea" in pm.get("events", {}))
    check("minidumps key present (list, even if empty off-Windows)",
          isinstance(pm.get("minidumps"), list))

    # And that collector output drives the verdict engine to the right cause.
    rep = bb.analyze([], pm, machine_rebooted=True)
    top = (rep["verdicts"] or [{}])[0].get("cause")
    check("Windows WHEA post-mortem → cpu_hardware verdict",
          top == "cpu_hardware", json.dumps(rep["verdicts"][:2]))


def test_linux_enrichment_against_fake_sysfs() -> None:
    print("Linux read_sample() enrichment — fake hwmon + RAPL sysfs")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        hwmon = root / "hwmon" / "hwmon0"
        hwmon.mkdir(parents=True)
        (hwmon / "name").write_text("nct6797\n")
        (hwmon / "in0_input").write_text("12096\n")
        (hwmon / "in0_label").write_text("+12V\n")
        (hwmon / "power1_input").write_text("90500000\n")   # 90.5 W
        rapl = root / "powercap" / "intel-rapl:0"
        rapl.mkdir(parents=True)
        (rapl / "name").write_text("package-0\n")
        (rapl / "energy_uj").write_text("1000000\n")

        with _Restore(hs, IS_LINUX=True, IS_MAC=False, IS_WINDOWS=False,
                      HWMON_ROOT=str(root / "hwmon"),
                      POWERCAP_ROOT=str(root / "powercap"),
                      _read_nvidia_smi=lambda: {},
                      _rapl_last={}):
            hs.read_sample()                       # primes the RAPL counter
            (rapl / "energy_uj").write_text(str(1_000_000 + 40_000_000))
            time.sleep(0.15)
            s = hs.read_sample()
        check("hwmon voltage rail read on the Linux branch",
              any(abs(v - 12.096) < 0.001 for v in (s.get("volts") or {}).values()),
              str(s.get("volts")))
        check("hwmon power read", any(abs(v - 90.5) < 0.1
              for v in (s.get("power") or {}).values()), str(s.get("power")))
        check("'hwmon' source recorded", "hwmon" in s["sources"], str(s["sources"]))
        check("RAPL watts derived from the energy delta",
              any(k.startswith("rapl/") for k in (s.get("power") or {}))
              and "rapl" in s["sources"], str(s.get("power")))


def test_atomic_session_marker_cross_platform() -> None:
    print("session marker — atomic write works on this OS (os.replace)")
    with tempfile.TemporaryDirectory() as td:
        rec = bb.BlackboxRecorder(Path(td), interval=1.0)
        rec.root.mkdir(parents=True, exist_ok=True)
        rec.started_ts = time.time()
        rec._write_session(clean=False)
        p = rec.root / "session.json"
        check("marker written", p.exists() and
              json.loads(p.read_text())["clean_shutdown"] is False)
        rec._write_session(clean=True)        # os.replace over an existing file
        check("marker atomically replaced (Windows-safe os.replace)",
              json.loads(p.read_text())["clean_shutdown"] is True)
        check("no .tmp left behind", not (rec.root / "session.tmp").exists())


def main() -> int:
    test_lhm_http_integration()
    test_windows_read_sample_dispatch()
    test_lhm_wmi_parsing()
    test_windows_postmortem_collector()
    test_linux_enrichment_against_fake_sysfs()
    test_atomic_session_marker_cross_platform()
    print()
    print("hw_ports: " + ("ALL PASS" if _fails == 0 else f"{_fails} FAILURE(S)"))
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
