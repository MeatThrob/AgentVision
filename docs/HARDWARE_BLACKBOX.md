# The Hardware Black Box — diagnosing full-machine crashes

Every other part of AgentVision watches one program. This subsystem watches the
**machine**, for the failure no program-level tool survives: the PC that turns
itself off, hard-freezes, or spontaneously reboots. After that kind of crash an
AI agent normally finds nothing — the evidence died with the OS — and heat, a
failing power supply, and a faulty CPU all present as the same symptom: *"it
just shut off."*

The black box works the way an aviation flight recorder does:

1. **Record** — a background thread samples machine-wide telemetry every ~2 s
   (CPU load/clocks, every temperature, fan RPM, voltage rail, package watts,
   GPU state, throttle flags) and appends it to a JSONL file, **flushing and
   `fsync()`ing every line**. A hard power cut therefore loses at most one
   sample; the last line on disk is the moment before the lights went out.
2. **Detect** — each run writes `session.json` at start and marks it clean at
   stop. On the next start, an unclean marker plus an OS boot time *newer*
   than the last sample means the whole machine went down mid-recording.
   (Boot time *older* than the last sample = only the recorder was killed —
   reported once, deliberately **not** treated as a crash.)
3. **Collect** — the OS's own post-mortem sources are gathered immediately:
   | OS | what survives the reboot |
   |---|---|
   | Windows | Event Log: Kernel-Power **41** (its `BugcheckCode` field is the fork: `0` = abrupt power loss/reset, non-zero = a bluescreen), EventLog **6008**, BugCheck **1001**, **WHEA-Logger** machine-checks, Kernel-Processor-Power **37** (firmware throttling); `C:\Windows\Minidump` listing |
   | Linux | `journalctl -b -1` (the previous boot's tail + MCE/EDAC/thermal lines), `/sys/fs/pstore` (kernel-persisted panic frames), `/var/crash`, `ras-mc-ctl --summary` when rasdaemon is installed |
   | macOS | `pmset -g log` shutdown-cause codes (negative codes are hardware-initiated: `-86`/`-95` thermal, `0` power loss, `-61`/`-62` watchdog…), `.panic` reports in `/Library/Logs/DiagnosticReports` |
4. **Judge** — a pure rulebook (`modules/hw_blackbox.py: analyze()`) scores it
   all into ranked causes with evidence and next steps:
   | signature | verdict |
   |---|---|
   | temps at/climbing toward Tj-max as the record ends | **THERMAL** |
   | record stops abruptly at *normal* temps, no stop code logged | **PSU / POWER** (the classic "clean drop") |
   | 12 V / 5 V / 3.3 V below ATX −5 % minimums, or sagging vs session median | **PSU / POWER** |
   | WHEA-Logger / MCE machine-check records | **CPU / BOARD** |
   | bugcheck code, minidump, kernel panic, pstore frame | **DRIVER / OS** |
   | EDAC / ECC error records | **RAM** |
   | a fan at 0 RPM while a zone is hot | **FAN FAILURE** |
5. **Freeze** — everything lands in a crash **capsule**
   (`crashes/crash-<stamp>/`): the pre-crash telemetry tail, the raw OS
   post-mortem, `report.json`, and a ready-to-read `report.md`.

The verdict is *ranked-most-consistent*, never proof: software polling cannot
see a microsecond rail transient, and every report says so in `missing_data`
along with anything else it could not observe on this machine.

## Quick start

**Inside the bridge** — nothing to do. The bridge starts the recorder at boot
(`AGENTVISION_HW_BLACKBOX=0` disables) and runs the crash check on every
start. The MCP surface:

| tool | what |
|---|---|
| `av_hw_status` | recorder state, live sample, sensor inventory + gaps, alerts, recent capsules — read `boot_capsule` first if present |
| `av_hw_metrics` | trends over the last N seconds: hottest temp, CPU load, watts, per-rail min/latest |
| `av_hw_crashes` | list capsules, or one capsule's full evidence by id |
| `av_hw_monitor` | start/stop/interval control |

**Standalone, on the machine that crashes** (recommended for a box that dies
at random times — it should record from OS startup, not only while a bridge
happens to be running):

```
python -m python_backend.modules.hw_blackbox --run         # record until Ctrl-C
python -m python_backend.modules.hw_blackbox --report      # newest crash verdict
python -m python_backend.modules.hw_blackbox --check       # run the crash check now
python -m python_backend.modules.hw_blackbox --inventory   # what can this machine sense?
```

Samples land in `log/blackbox/<hostname>/` (override with
`AGENTVISION_BLACKBOX_DIR`). Disk use is bounded (256 MiB by default,
`AGENTVISION_HW_MAX_BYTES`) — at the 2 s default interval that is over a year
of history; capsules are never auto-deleted (newest 40 kept).

### Autostart recipes

*Windows (Task Scheduler, run at logon, highest privileges recommended so
LibreHardwareMonitor sensors are readable):*

```
schtasks /Create /TN "AgentVision HW Blackbox" /SC ONLOGON /RL HIGHEST ^
  /TR "\"C:\Path\to\python.exe\" -m python_backend.modules.hw_blackbox --run"
```

*Linux (systemd — save as `/etc/systemd/system/av-hw-blackbox.service`, then
`systemctl enable --now av-hw-blackbox`):*

```
[Unit]
Description=AgentVision hardware black box
After=multi-user.target

[Service]
WorkingDirectory=/path/to/AgentVision
ExecStart=/usr/bin/python3 -m python_backend.modules.hw_blackbox --run
Restart=always

[Install]
WantedBy=multi-user.target
```

*macOS (launchd — save as
`~/Library/LaunchAgents/com.agentvision.hwblackbox.plist`, then
`launchctl load` it):*

```
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.agentvision.hwblackbox</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>-m</string><string>python_backend.modules.hw_blackbox</string>
    <string>--run</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/AgentVision</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```

The bridge and a standalone recorder can coexist: the session marker carries a
PID, and a live PID is never mistaken for a crash.

## What each OS can sense (and how to widen it)

Sensor depth decides how sharp the verdict can be. `av_hw_status` /
`--inventory` reports the live state of exactly this table:

| OS | out of the box | to get the full picture |
|---|---|---|
| **Windows** | CPU load/clocks, RAM, GPU via `nvidia-smi` | **Install [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)** and enable *Options → Remote Web Server → Run*. Its `data.json` (port 8085) feeds temps, fans, **voltage rails**, and package watts into every sample automatically (`AGENTVISION_LHM_URL` overrides the URL; its WMI namespace is used as a fallback). Without it, thermal-vs-PSU runs on far less evidence — the Windows WMI thermal zone is unimplemented on most consumer boards. |
| **Linux** | temps/fans via hwmon (psutil), **voltage rails** from `in*_input`, package watts from RAPL, GPU via `nvidia-smi` (AMD appears through hwmon) | run `sensors-detect` (lm_sensors) once so the motherboard chip is exposed; install `rasdaemon` for machine-check persistence |
| **macOS** | load/clocks/RAM, throttle state via `pmset -g therm`, shutdown-cause codes | `brew install osx-cpu-temp` (Intel) or `smctemp` (Apple Silicon) for CPU temperature without root; as root, `powermetrics` die temperature is used automatically |

## Reading a capsule as an agent

1. `av_hw_status` — if `boot_capsule` is present, the previous session died
   with the machine and the verdict is already frozen.
2. `av_hw_crashes(id=...)` — the ranked verdicts. Lead with the top cause and
   its evidence lines (they carry timestamps relative to the moment of death,
   e.g. *"Core (Tctl/Tdie) reached 97°C at T-4s"*).
3. Give the user the `next_steps` of the top verdict, then the runner-up if
   confidence is not high. Quote `missing_data` — if the machine has no
   voltage sensing, say what installing LibreHardwareMonitor would add before
   the next crash.
4. Between crashes, `av_hw_metrics` under load is the early-warning view: the
   same series the verdict engine scores, live.

## Why these signals (research the rulebook is built on)

- Kernel-Power 41 is a symptom, not a cause; its `BugcheckCode`, and the
  WHEA/BugCheck/thermal records around it, carry the diagnosis
  ([Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/2285389/how-to-diagnose-and-fix-event-41-kernel-power)).
- Reading a sensor log across a shutdown: temperature at junction max just
  before the cut = thermal; a clean drop at normal temperature = PSU/cable;
  polling cannot catch sub-interval transients
  ([HWiNFO forum practice](https://www.hwinfo.com/forum/threads/sudden-pc-shutdowns-hwinfo-logs-analysis-help-needed.10405/),
  [reading your own HWiNFO logs](https://blog.silverpc.hu/2025/10/20/how-to-read-your-own-hwinfo-logs-to-help-diagnose-pc-issues/)).
- ATX rails must stay within ±5 % (11.4 / 4.75 / 3.135 V floors).
- Linux: previous-boot journal, MCE/EDAC, pstore and rasdaemon are the
  post-reboot evidence chain
  ([Arch wiki: Machine-check exception](https://wiki.archlinux.org/title/Machine-check_exception),
  [Rackspace: check logs for a reboot/shutdown](https://docs.rackspace.com/docs/check-logs-for-why-a-system-reboot-shutdown-in-linux)).
- LibreHardwareMonitor's web server / WMI namespace is the standard
  programmatic sensor source on Windows
  ([Home Assistant integration docs](https://www.home-assistant.io/integrations/libre_hardware_monitor/),
  [PyHardwareMonitor](https://github.com/snip3rnick/PyHardwareMonitor)).
- macOS `pmset -g log` shutdown causes and `powermetrics` samplers
  ([powermetrics reference](https://ss64.com/mac/powermetrics.html)).
