#!/usr/bin/env python3
"""Tests for the hardware flight recorder (modules/hw_blackbox.py) and its
sensor parsers (utils/hw_sensors.py).

Everything here runs on ANY OS with the stdlib alone: the parsers are pure
functions fed captured Windows/Linux/macOS output, the verdict engine is fed
synthetic crashes, and the recorder is exercised against a temp dir with an
injected sensor function. No suite may shell out to wevtutil/journalctl/pmset
— the one integration point (collect_postmortem) is injected instead, because
a test that needs a crashed Windows box to pass is a test that never runs.

    python3 python_backend/modules/test_hw_blackbox.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules import hw_blackbox as bb
from utils import hw_sensors as hs

_fails = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _fails
    if cond:
        print(f"  ok   {label}")
    else:
        _fails += 1
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


# ═════════════════════════════════════════════════════════════════════════════
#  Pure sensor parsers
# ═════════════════════════════════════════════════════════════════════════════

def test_parse_lhm_json() -> None:
    print("parse_lhm_json — LibreHardwareMonitor data.json tree")
    tree = {
        "Text": "Sensor", "Value": "", "Children": [{
            "Text": "GAMING-PC", "Value": "", "Children": [{
                "Text": "AMD Ryzen 7 5800X", "Value": "", "Children": [
                    {"Text": "Temperatures", "Value": "", "Children": [
                        {"Text": "Core (Tctl/Tdie)", "Value": "78.5 °C",
                         "Children": []},
                        {"Text": "CCD1", "Value": "170.6 °F", "Children": []},
                    ]},
                    {"Text": "Powers", "Value": "", "Children": [
                        {"Text": "Package", "Value": "112.3 W",
                         "Children": []}]},
                    {"Text": "Load", "Value": "", "Children": [
                        {"Text": "CPU Total", "Value": "97.2 %",
                         "Children": []}]},
                ]},
                {"Text": "Nuvoton NCT6798D", "Value": "", "Children": [
                    {"Text": "Voltages", "Value": "", "Children": [
                        {"Text": "+12V", "Value": "11,328 V", "Children": []},
                        {"Text": "Vcore", "Value": "1.332 V", "Children": []},
                    ]},
                    {"Text": "Fans", "Value": "", "Children": [
                        {"Text": "CPU Fan", "Value": "1420 RPM",
                         "Children": []},
                        {"Text": "Case Fan #2", "Value": "0 RPM",
                         "Children": []},
                    ]},
                ]},
            ]},
        ]}
    out = hs.parse_lhm_json(tree)
    temps = out["temps"]
    check("temp leaf parsed with hardware path",
          any(v == 78.5 for v in temps.values()), str(temps))
    f_to_c = [v for v in temps.values() if 76 < v < 78]
    check("°F leaf converted to °C (170.6°F → ~77°C)", bool(f_to_c),
          str(temps))
    check("comma-decimal voltage parsed (11,328 V → 11.328)",
          any(abs(v - 11.328) < 0.001 for v in out["volts"].values()),
          str(out["volts"]))
    check("both fans present incl. the 0 RPM one",
          sorted(out["fans"].values()) == [0, 1420], str(out["fans"]))
    check("power and load classified by unit",
          112.3 in out["power"].values() and 97.2 in out["load"].values(),
          f"{out['power']} {out['load']}")
    check("group nodes contribute no phantom sensors",
          all("Temperatures" not in k for k in temps), str(temps))


def test_parse_nvidia_smi() -> None:
    print("parse_nvidia_smi_csv")
    two = ("NVIDIA GeForce RTX 3080, 83, 99, 320.5, 78, 9216, 10240\n"
           "NVIDIA GeForce GTX 1650, 41, 3, 25.1, 30, 512, 4096\n")
    g = hs.parse_nvidia_smi_csv(two)
    check("multi-GPU picks the hottest card", g.get("temp_c") == 83.0, str(g))
    check("power parsed", g.get("power_w") == 320.5, str(g))
    na = hs.parse_nvidia_smi_csv("Quadro P400, 38, 1, [N/A], [N/A], 100, 2048")
    check("[N/A] fields omitted, not zeroed",
          "power_w" not in na and na.get("temp_c") == 38.0, str(na))
    check("garbage → empty dict", hs.parse_nvidia_smi_csv("no gpus\n") == {})


def test_parse_pmset_therm() -> None:
    print("parse_pmset_therm")
    intel = ("Note: No thermal warning level has been recorded\n"
             "CPU Power notify\n"
             "\tCPU_Scheduler_Limit \t= 100\n"
             "\tCPU_Available_CPUs \t= 8\n"
             "\tCPU_Speed_Limit \t= 60\n")
    out = hs.parse_pmset_therm(intel)
    check("Intel throttle fields parsed",
          out.get("cpu_speed_limit") == 60
          and out.get("cpu_available_cpus") == 8, str(out))
    check("no-warning note → level 0", out.get("thermal_warning_level") == 0)
    check("empty text → {}", hs.parse_pmset_therm("") == {})


def test_parse_hwmon_tree() -> None:
    print("parse_hwmon_tree — fake /sys/class/hwmon")
    with tempfile.TemporaryDirectory() as td:
        chip = Path(td) / "hwmon0"
        chip.mkdir()
        (chip / "name").write_text("nct6798\n")
        (chip / "in0_input").write_text("1332\n")       # Vcore, mV
        (chip / "in0_label").write_text("Vcore\n")
        (chip / "in1_input").write_text("11856\n")      # unlabeled 12V rail
        (chip / "power1_input").write_text("112300000\n")  # µW
        out = hs.parse_hwmon_tree(td)
        check("labeled rail in volts", out["volts"].get("nct6798/Vcore") == 1.332,
              str(out["volts"]))
        check("unlabeled rail falls back to channel name",
              out["volts"].get("nct6798/in1") == 11.856, str(out["volts"]))
        check("power µW → W", out["power"].get("nct6798/power1") == 112.3,
              str(out["power"]))
    check("missing root → empty", hs.parse_hwmon_tree("/nonexistent-xyz")
          == {"volts": {}, "power": {}})


def test_postmortem_parsers() -> None:
    print("post-mortem text parsers")
    kp = ("Log Name: System\nSource: Microsoft-Windows-Kernel-Power\n"
          "Event ID: 41\nLevel: Critical\nDescription:\nThe system has "
          "rebooted without cleanly shutting down first.\n"
          "  BugcheckCode: 0\n  BugcheckParameter1: 0x0\n")
    check("BugcheckCode 0 extracted", bb.parse_win_bugcheck_code(kp) == 0)
    check("BugcheckCode 278 extracted",
          bb.parse_win_bugcheck_code(kp.replace("BugcheckCode: 0",
                                                "BugcheckCode: 278")) == 278)
    check("no code → None", bb.parse_win_bugcheck_code("nothing here") is None)

    journal = ("2026-08-06T21:14:02 host kernel: mce: [Hardware Error]: "
               "CPU 3: Machine Check: 0 Bank 5\n"
               "2026-08-06T21:14:02 host kernel: usb 1-4: new device\n"
               "2026-08-06T21:14:03 host kernel: EDAC MC0: 1 CE error on "
               "CPU_SrcID#0\n"
               "2026-08-06T21:14:09 host kernel: thermal thermal_zone0: "
               "critical temperature reached (101 C), shutting down\n")
    hits = bb.grep_hw_lines(journal)
    check("mce + edac + thermal lines kept, noise dropped",
          len(hits) == 3 and all("usb" not in h for h in hits), str(hits))

    pmlog = ("2026-08-05 19:02:11 +0000 Shutdown Cause: 5\n"
             "some unrelated line\n"
             "2026-08-06 22:41:03 +0000 Shutdown Cause: -86\n")
    causes = bb.parse_mac_shutdown_causes(pmlog)
    check("both causes parsed in order",
          [c["code"] for c in causes] == [5, -86], str(causes))
    check("-86 mapped to thermal", "overtemperature" in causes[1]["meaning"],
          str(causes[1]))


# ═════════════════════════════════════════════════════════════════════════════
#  The verdict engine
# ═════════════════════════════════════════════════════════════════════════════

def _mk_samples(n: int, step: float = 2.0, base_ts: float = 1_000_000.0,
                **series) -> list[dict]:
    """Synthetic telemetry: series maps a dotted key ('temps.cpu/Tctl',
    'volts.+12V', 'fans.CPU Fan', 'gpu.power_w') to a list of n values."""
    out = []
    for i in range(n):
        s: dict = {"ts": base_ts + i * step}
        for key, vals in series.items():
            bucket, _, leaf = key.partition(".")
            v = vals[i] if i < len(vals) else vals[-1]
            if v is None:
                continue
            if bucket == "cpu":
                s.setdefault("cpu", {})[leaf] = v
            elif bucket == "gpu":
                s.setdefault("gpu", {})[leaf] = v
            else:
                s.setdefault(bucket, {})[leaf] = v
        out.append(s)
    return out


def _top(report: dict) -> str:
    v = report.get("verdicts") or [{}]
    return v[0].get("cause", "")


def test_verdict_thermal() -> None:
    print("analyze — thermal crash (temps climb to Tj-max, record stops)")
    temps = [70 + i for i in range(28)]              # 70 → 97 °C over ~54 s
    samples = _mk_samples(28, **{"temps.cpu/Tctl": temps})
    rep = bb.analyze(samples, {}, machine_rebooted=True)
    check("thermal is the top verdict", _top(rep) == "thermal",
          json.dumps(rep["verdicts"][:2]))
    check("confidence high", rep["verdicts"][0]["confidence"] == "high")
    check("evidence names the sensor and the temperature",
          any("97" in e for e in rep["verdicts"][0]["evidence"]),
          str(rep["verdicts"][0]["evidence"]))
    check("next steps mention cooling",
          any("heatsink" in s.lower() or "fan" in s.lower() or
              "repaste" in s.lower()
              for s in rep["verdicts"][0]["next_steps"]))
    check("missing_data still warns about polling limits",
          any("transient" in m for m in rep["missing_data"]))


def test_verdict_psu_rail_sag() -> None:
    print("analyze — PSU rail sag (12V below ATX minimum)")
    rails = [12.1] * 20 + [11.9, 11.6, 11.2, 11.1]
    temps = [55.0] * 24
    samples = _mk_samples(24, **{"volts.+12V": rails, "temps.cpu/Tctl": temps})
    rep = bb.analyze(samples, {}, machine_rebooted=True)
    check("psu_power is the top verdict", _top(rep) == "psu_power",
          json.dumps(rep["verdicts"][:2]))
    check("evidence cites the ATX floor",
          any("11.4" in e or "ATX" in e
              for e in rep["verdicts"][0]["evidence"]),
          str(rep["verdicts"][0]["evidence"]))
    check("next steps mention trying another PSU",
          any("PSU" in s for s in rep["verdicts"][0]["next_steps"]))


def test_verdict_abrupt_stop_normal_temps() -> None:
    print("analyze — abrupt stop at normal temps + Kernel-Power 41 code 0")
    samples = _mk_samples(30, **{"temps.cpu/Tctl": [52.0] * 30})
    pm = {"os": "windows",
          "events": {"kernel_power_41": "Event ID: 41\nBugcheckCode: 0"},
          "bugcheck_code": 0, "minidumps": []}
    rep = bb.analyze(samples, pm, machine_rebooted=True)
    check("psu_power is the top verdict", _top(rep) == "psu_power",
          json.dumps(rep["verdicts"][:2]))
    check("the clean-drop signature is cited",
          any("no stop code" in e.lower() or "power-cut" in e.lower()
              for e in rep["verdicts"][0]["evidence"]))


def test_verdict_whea_and_bugcheck() -> None:
    print("analyze — WHEA machine-check vs driver bluescreen")
    pm_whea = {"os": "windows", "events": {"whea": "WHEA-Logger Event 18 "
               "A fatal hardware error has occurred."}}
    rep = bb.analyze([], pm_whea, machine_rebooted=True)
    check("WHEA → cpu_hardware top", _top(rep) == "cpu_hardware",
          json.dumps(rep["verdicts"][:2]))
    pm_bug = {"os": "windows",
              "events": {"kernel_power_41": "BugcheckCode: 209"},
              "bugcheck_code": 209,
              "minidumps": [{"file": "080626-1.dmp", "mtime": "x"}]}
    rep2 = bb.analyze([], pm_bug, machine_rebooted=True)
    check("bugcheck+minidump → driver_software top",
          _top(rep2) == "driver_software", json.dumps(rep2["verdicts"][:2]))
    check("next step points at WinDbg",
          any("WinDbg" in s for s in rep2["verdicts"][0]["next_steps"]))


def test_verdict_linux_and_mac_postmortem() -> None:
    print("analyze — Linux MCE/EDAC lines and macOS shutdown causes")
    pm_lin = {"os": "linux", "prev_boot_hw_lines": [
        "kernel: mce: [Hardware Error]: CPU 3: Machine Check",
        "kernel: EDAC MC0: 1 CE error"]}
    rep = bb.analyze([], pm_lin, machine_rebooted=True)
    causes = {v["cause"] for v in rep["verdicts"]}
    check("MCE → cpu_hardware present, EDAC → ram present",
          {"cpu_hardware", "ram"} <= causes, str(causes))
    check("cpu_hardware outranks ram", _top(rep) == "cpu_hardware")

    pm_mac = {"os": "macos", "shutdown_causes": [
        {"when": "2026-08-06", "code": -86,
         "meaning": bb.MAC_SHUTDOWN_CAUSES[-86]}]}
    rep2 = bb.analyze([], pm_mac, machine_rebooted=True)
    check("mac cause -86 → thermal top", _top(rep2) == "thermal",
          json.dumps(rep2["verdicts"][:1]))


def test_verdict_fan_stall() -> None:
    print("analyze — fan at 0 RPM while hot")
    samples = _mk_samples(10, **{"temps.cpu/Tctl": [88.0] * 10,
                                 "fans.CPU Fan": [0] * 10})
    rep = bb.analyze(samples, {}, machine_rebooted=True)
    causes = {v["cause"] for v in rep["verdicts"]}
    check("fan_failure fired", "fan_failure" in causes, str(causes))


def test_verdict_edge_cases() -> None:
    print("analyze — edge cases")
    rep = bb.analyze([], {}, machine_rebooted=True)
    check("nothing at all → inconclusive summary",
          "INCONCLUSIVE" in rep["summary"], rep["summary"])
    check("...but says what was missing",
          any("recorder was not running" in m for m in rep["missing_data"]))
    rep2 = bb.analyze([], {}, machine_rebooted=False)
    check("machine did not reboot → says so, no crash verdict",
          "did NOT reboot" in rep2["summary"], rep2["summary"])
    blind = _mk_samples(20, **{"cpu.load_pct": [40.0] * 20})
    rep3 = bb.analyze(blind, {}, machine_rebooted=True)
    check("samples without temps/volts → the gaps are named",
          any("no temperature data" in m for m in rep3["missing_data"])
          and any("no voltage-rail data" in m for m in rep3["missing_data"]),
          str(rep3["missing_data"]))
    md = bb.render_report_md(rep3, "crash-test")
    check("markdown report renders with the summary and gaps",
          "crash-test" in md and "could not see" in md)


# ═════════════════════════════════════════════════════════════════════════════
#  Recorder + crash detection against a temp dir
# ═════════════════════════════════════════════════════════════════════════════

def test_recorder_writes_and_clean_stop() -> None:
    print("recorder — fsync'd samples land on disk; stop marks clean")
    fake = {"n": 0}

    def fake_sample():
        fake["n"] += 1
        return {"ts": time.time(), "temps": {"cpu": 50.0}, "sources": ["fake"]}

    real = hs.read_sample
    hs.read_sample = fake_sample
    try:
        with tempfile.TemporaryDirectory() as td:
            rec = bb.BlackboxRecorder(Path(td), interval=0.5)
            rec.start()
            time.sleep(1.4)
            sess = json.loads((Path(td) / "session.json").read_text())
            check("session marker written un-clean while running",
                  sess.get("clean_shutdown") is False, str(sess))
            rec.stop()
            files = list(Path(td).glob("samples-*.jsonl"))
            check("a day file exists", len(files) == 1, str(files))
            rows = [json.loads(l) for l in
                    files[0].read_text().splitlines()]
            check("multiple samples written", len(rows) >= 2, str(len(rows)))
            check("samples carry the sensor payload",
                  rows[0]["temps"]["cpu"] == 50.0)
            sess = json.loads((Path(td) / "session.json").read_text())
            check("clean stop recorded", sess.get("clean_shutdown") is True)
            check("status() reports the write count",
                  rec.status()["samples_written_session"] == len(rows))
    finally:
        hs.read_sample = real


def test_crash_detection() -> None:
    print("crash detection — unclean session + newer boot = capsule")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        last_ts = 1_000_000.0
        (root / "session.json").write_text(json.dumps(
            {"session_id": "x", "started": last_ts - 600, "pid": 99999999,
             "clean_shutdown": False}))
        samples = _mk_samples(30, base_ts=last_ts - 58,
                              **{"temps.cpu/Tctl": [70 + i for i in range(30)]})
        (root / "samples-20260806.jsonl").write_text(
            "\n".join(json.dumps(s) for s in samples))

        cap = bb.check_previous_session(
            root, boot_ts=last_ts + 40, pid_alive_fn=lambda p: False,
            postmortem_fn=lambda: {"os": "test"})
        check("capsule built", cap is not None)
        if cap:
            check("machine_rebooted detected from boot time",
                  cap["report"]["machine_rebooted"] is True)
            check("thermal verdict from the sample tail",
                  _top(cap["report"]) == "thermal",
                  json.dumps(cap["report"]["verdicts"][:1]))
            cdir = Path(cap["dir"])
            check("capsule dir holds tail+postmortem+reports",
                  all((cdir / f).exists() for f in
                      ("tail_samples.jsonl", "postmortem.json",
                       "report.json", "report.md")),
                  str(list(cdir.iterdir()) if cdir.exists() else "missing"))
            check("capsule listed", bb.list_capsules(root)[0]["id"] == cap["id"])
            loaded = bb.load_capsule(cap["id"], root)
            check("capsule loads with markdown",
                  loaded is not None and "report_md" in loaded)

        (root / "session.json").write_text(json.dumps(
            {"clean_shutdown": True}))
        check("clean session → no capsule",
              bb.check_previous_session(
                  root, boot_ts=last_ts + 40,
                  pid_alive_fn=lambda p: False,
                  postmortem_fn=lambda: {"os": "test"}) is None)

        (root / "session.json").write_text(json.dumps(
            {"clean_shutdown": False, "pid": os.getpid()}))
        check("live pid (bridge + standalone both running) → no capsule",
              bb.check_previous_session(
                  root, boot_ts=last_ts + 40,
                  pid_alive_fn=lambda p: True,
                  postmortem_fn=lambda: {"os": "test"}) is None)

        (root / "session.json").write_text(json.dumps(
            {"clean_shutdown": False, "pid": 99999999}))
        n_before = len(bb.list_capsules(root, limit=40))
        cap2 = bb.check_previous_session(
            root, boot_ts=last_ts - 9999, pid_alive_fn=lambda p: False,
            postmortem_fn=lambda: {"os": "test"})
        check("boot BEFORE last sample → recorder died, not the machine",
              cap2 is not None
              and cap2["report"]["machine_rebooted"] is False
              and "did NOT reboot" in cap2["report"]["summary"],
              json.dumps((cap2 or {}).get("report", {}).get("summary")))
        check("...and NO capsule is minted for it (no cry-wolf spam)",
              cap2 is not None and cap2["id"] is None
              and len(bb.list_capsules(root, limit=40)) == n_before)


def test_read_recent_and_budget() -> None:
    print("sample store — tail read + byte budget")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        old = _mk_samples(5, base_ts=1000.0)
        new = _mk_samples(5, base_ts=9000.0)
        (root / "samples-20260101.jsonl").write_text(
            "\n".join(json.dumps(s) for s in old))
        (root / "samples-20260102.jsonl").write_text(
            "\n".join(json.dumps(s) for s in new))
        tail = bb.read_recent_samples(root, seconds=60.0)
        check("tail keeps only samples within the window of the newest",
              len(tail) == 5 and all(s["ts"] >= 9000.0 for s in tail),
              str(len(tail)))

        rec = bb.BlackboxRecorder(root)
        saved = bb.BLACKBOX_MAX_BYTES
        bb.BLACKBOX_MAX_BYTES = 10       # force the budget to bite
        try:
            rec._fh = open(root / "samples-20260103.jsonl", "a")
            rec._fh_day = "samples-20260103.jsonl"
            rec._enforce_budget()
        finally:
            rec._fh.close()
            bb.BLACKBOX_MAX_BYTES = saved
        left = sorted(p.name for p in root.glob("samples-*.jsonl"))
        check("oldest day files pruned, newest kept",
              left == ["samples-20260103.jsonl"], str(left))


def test_sensor_sample_and_inventory_never_raise() -> None:
    print("hw_sensors — live sample + inventory on THIS machine never raise")
    s = hs.read_sample()
    check("sample has ts and sources", "ts" in s and "sources" in s,
          str(list(s.keys())))
    inv = hs.sensor_inventory()
    check("inventory reports capability flags",
          isinstance(inv.get("have"), dict) and "temps" in inv["have"],
          str(inv))


def main() -> int:
    test_parse_lhm_json()
    test_parse_nvidia_smi()
    test_parse_pmset_therm()
    test_parse_hwmon_tree()
    test_postmortem_parsers()
    test_verdict_thermal()
    test_verdict_psu_rail_sag()
    test_verdict_abrupt_stop_normal_temps()
    test_verdict_whea_and_bugcheck()
    test_verdict_linux_and_mac_postmortem()
    test_verdict_fan_stall()
    test_verdict_edge_cases()
    test_recorder_writes_and_clean_stop()
    test_crash_detection()
    test_read_recent_and_budget()
    test_sensor_sample_and_inventory_never_raise()
    print()
    print("hw_blackbox: " + ("ALL PASS" if _fails == 0
                             else f"{_fails} FAILURE(S)"))
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
