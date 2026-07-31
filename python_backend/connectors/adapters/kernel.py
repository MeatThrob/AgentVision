"""
Kernel / OS / embedded-RTOS log adapters
================================================================================
Linux kernel ring-buffer variants, the ISO rsyslog/journald envelope, macOS
unified logging, ftrace, and the common embedded/RTOS consoles (ESP-IDF, Zephyr,
U-Boot, FreeRTOS). These are the "what is the machine itself doing" formats that
sit under an application — exactly what you want on the timeline next to a crash.

Formats: linux_dmesg, linux_dmesg_ctime, linux_devkmsg, kernel_panic_oops,
netfilter_klog, esp32_panic, esp32_rom_boot, esp_idf_log, zephyr_log,
zephyr_fatal, uboot_boot_log, freertos_logging, macos_unified_log, rsyslog,
ftrace (incl. function_graph), and the batch-4 firmware consoles:
tfa_bootlog, edk2_uefi, coreboot_cbmem.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp,
                      _SYSLOG_SEVERITY, _to_ms, ratio_detect, block_ratio,
                      split_any)


def _subsys(msg: str):
    """A kernel/driver message usually reads 'subsys: text' — surface the
    subsystem name as source without dropping it from the message."""
    m = re.match(r"^([a-zA-Z][\w .\-]{0,30}?):\s+", msg)
    return m.group(1) if m else ""


def _infer(msg: str) -> str:
    low = msg.lower()
    if any(w in low for w in ("panic", "oops", "bug:", "fatal", "segfault",
                              " error", "error:", "failed", "cannot", "unable to")):
        return "error"
    if any(w in low for w in ("warn", "deprecat")):
        return "warn"
    return ""


# ── Linux dmesg — relative (monotonic) timestamp ─────────────────────────────
#   [    3.123456] usb 1-1: new high-speed USB device number 2 using ci_hdrc
class LinuxDmesgAdapter(LogAdapter):
    name = "linux_dmesg"
    language = "linux"
    # BATCH-6 gap fix: optional '<N>' klog-priority prefix (Android last_kmsg /
    # pstore console-ramoops keep it) and multi-line-aware detection so a whole
    # dmesg excerpt (e.g. the ACPI catalog block) routes here.
    _RE = re.compile(r"^(?:<(?P<pri>\d{1,2})>)?\[\s*(?P<up>\d+\.\d{3,6})\]\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda el: block_ratio(el, lambda x: bool(self._RE.match(x))))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if "\n" in s:                       # whole excerpt → parse first record
            for x in s.splitlines():
                if x.strip() and self._RE.match(x):
                    ev = self.parse_line(x)
                    if ev:
                        ev["raw"] = line
                        return ev
            return None
        m = self._RE.match(s)
        if not m:
            return None
        msg = m.group("msg")
        level = _infer(msg)
        if m.group("pri") is not None:
            level = {"FATAL": "fatal", "ERROR": "error", "WARN": "warn",
                     "INFO": "info", "DEBUG": "debug"}.get(
                _SYSLOG_SEVERITY.get(int(m.group("pri")) % 8, ""), level)
        # boot-relative seconds are MONOTONIC, not wall-clock → no ts_ms.
        return self._event(level=level, message=msg, source=_subsys(msg),
                           ts_ms=None, fields={"uptime": float(m.group("up"))}, raw=line)


# ── Linux dmesg — ctime wall-clock (dmesg -T) ────────────────────────────────
#   [Sun Jul 20 14:03:11 2026] usb 1-1: new high-speed USB device number 4
class LinuxDmesgCtimeAdapter(LogAdapter):
    name = "linux_dmesg_ctime"
    language = "linux"
    _RE = re.compile(r"^\[(?P<ts>[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+"
                     r"\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+\d{4})\]\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln)))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        msg = m.group("msg")
        return self._event(level=_infer(msg), message=msg, source=_subsys(msg),
                           ts_ms=parse_timestamp(m.group("ts")), raw=line)


# ── Linux /dev/kmsg structured record ────────────────────────────────────────
#   6,339,5140900,-;usb 1-1: USB disconnect, device number 4
class LinuxDevKmsgAdapter(LogAdapter):
    name = "linux_devkmsg"
    language = "linux"
    _RE = re.compile(r"^(?P<pri>\d{1,3}),(?P<seq>\d+),(?P<us>\d+),(?P<flags>[^;,]*);(?P<msg>.*)$")
    # multi-line records: continuation lines start with ONE space + KEY=VALUE
    # (SUBSYSTEM=usb / DEVICE=c189:131) per the kernel's dev-kmsg ABI doc.
    _CONT = re.compile(r"^\s+[A-Z_]+=\S")

    def detect(self, sample_lines):
        # BATCH-4 gap fix: a whole record (header + " KEY=VALUE" continuation
        # lines) may arrive as ONE element — recognize the block, not just the
        # bare header line.
        def hit(el):
            pieces = str(el).splitlines() or [str(el)]
            if not self._RE.match(pieces[0]):
                return False
            return all(self._CONT.match(x) for x in pieces[1:] if x.strip())
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        pieces = line.rstrip("\r\n").splitlines() or [line.rstrip("\r\n")]
        m = self._RE.match(pieces[0])
        if not m:
            return None
        pri = int(m.group("pri"))
        msg = m.group("msg")
        fields = {"seq": m.group("seq"), "facility": pri >> 3,
                  "priority": pri,
                  "uptime": round(int(m.group("us")) / 1e6, 6)}
        for x in pieces[1:]:                     # continuation dictionary lines
            cm = re.match(r"^\s+([A-Z_]+)=(.*)$", x)
            if cm:
                fields[cm.group(1).lower()] = cm.group(2).strip()
        return self._event(level=_SYSLOG_SEVERITY.get(pri & 0x07, "INFO"), message=msg,
                           source=_subsys(msg) or "kernel", ts_ms=None,
                           fields=fields,
                           raw=line)


# ── Linux kernel Oops / panic / BUG / call-trace block ───────────────────────
#   [  345.678901] BUG: unable to handle page fault for address: 00000000
#   [  345.678902] RIP: 0010:my_func+0x12/0x40
class KernelPanicOopsAdapter(LogAdapter):
    name = "kernel_panic_oops"
    language = "linux"
    _BRACKET = re.compile(r"^\[\s*(?P<up>\d+\.\d{3,6})\]\s?(?P<msg>.*)$")
    _MARKERS = re.compile(
        r"(Oops:|BUG:|kernel panic|Kernel panic|Kernel data abort|general protection fault|"
        r"unable to handle|RIP:|RSP:|Call Trace:|Modules linked in:|Tainted:|"
        r"stack segment|invalid opcode|not syncing)", re.IGNORECASE)

    def _body(self, s: str) -> Optional[str]:
        m = self._BRACKET.match(s)
        return m.group("msg") if m else (s if self._MARKERS.search(s) else None)

    def detect(self, sample_lines):
        def ok(ln):
            b = self._body(ln.rstrip("\r\n"))
            return b is not None and bool(self._MARKERS.search(b))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        m = self._BRACKET.match(s)
        body = m.group("msg") if m else s
        if not self._MARKERS.search(body):
            return None
        low = body.lower()
        level = "fatal" if ("panic" in low or "bug:" in low or "oops" in low
                            or "general protection" in low or "data abort" in low) else "error"
        fields = {}
        if m:
            fields["uptime"] = float(m.group("up"))
        rip = re.search(r"RIP:\s*\S+:(\S+)", body)
        if rip:
            fields["rip"] = rip.group(1)
        return self._event(level=level, message=body, source="kernel.panic",
                           ts_ms=None, fields=fields or None, raw=line)


# ── Linux Netfilter / iptables / nftables / UFW LOG target ───────────────────
#   Dec  4 08:25:00 host kernel: [1659.916] [UFW BLOCK] IN=eth0 OUT= SRC=.. DST=.. PROTO=TCP ..
class NetfilterKlogAdapter(LogAdapter):
    name = "netfilter_klog"
    language = "linux"
    _NF = re.compile(r"\bIN=\S*\s.*\bSRC=\S+\s.*\bDST=\S+\s.*\bPROTO=\S+")
    _TS = re.compile(r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s")
    _PREFIX = re.compile(r"kernel:\s*(?:\[\s*\d+\.\d+\]\s*)?(?P<prefix>.*?)IN=")
    _KV = re.compile(r"\b([A-Z][A-Z0-9]*)=(\S*)")
    _FLAGS = re.compile(r"\b(SYN|ACK|FIN|RST|PSH|URG|CWR|ECE|DF|MF)\b")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._NF.search(ln)))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if not self._NF.search(s):
            return None
        fields = {}
        for k, v in self._KV.findall(s):
            if v:
                fields[k.lower()] = v
        flags = self._FLAGS.findall(s[s.find("PROTO="):]) if "PROTO=" in s else []
        if flags:
            fields["tcp_flags"] = ",".join(dict.fromkeys(flags))
        prefix = ""
        pm = self._PREFIX.search(s)
        if pm:
            prefix = pm.group("prefix").strip()
        if not prefix:
            bp = re.search(r"\[([A-Z][A-Z0-9 _]+?)\]", s)
            prefix = bp.group(1) if bp else ""
        low = (prefix or s).lower()
        level = "warn" if any(w in low for w in ("block", "drop", "reject", "deny")) else "info"
        tm = self._TS.match(s)
        msg = f'{prefix or "netfilter"} {fields.get("src","")}:{fields.get("spt","")} ' \
              f'-> {fields.get("dst","")}:{fields.get("dpt","")} {fields.get("proto","")}'.strip()
        if prefix:
            fields["log_prefix"] = prefix
        return self._event(level=level, message=msg, source="netfilter",
                           ts_ms=parse_timestamp(tm.group("ts")) if tm else None,
                           fields=fields, raw=line)


# ── ESP32 Guru Meditation panic ──────────────────────────────────────────────
#   Guru Meditation Error: Core  1 panic'ed (LoadProhibited). Exception was unhandled.
class Esp32PanicAdapter(LogAdapter):
    name = "esp32_panic"
    language = "embedded"
    _GURU = re.compile(r"^Guru Meditation Error:\s*Core\s*(?P<core>\d+)\s*panic'ed\s*"
                       r"\((?P<cause>[^)]*)\)")
    _REG = re.compile(r"^(?:Backtrace:|PC\s*:|[A-Z][A-Z0-9]{1,7}\s*:\s*0x[0-9a-fA-F]+)")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._GURU.match(ln.strip())
                                            or self._REG.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._GURU.match(s)
        if m:
            return self._event(level="fatal",
                               message=f"Core {m.group('core')} panic'ed ({m.group('cause')})",
                               source="esp32.panic", trace_id=m.group("core"),
                               fields={"core": int(m.group("core")), "cause": m.group("cause")},
                               raw=line)
        if s.startswith("Backtrace:"):
            frames = re.findall(r"0x[0-9a-fA-F]+:0x[0-9a-fA-F]+", s)
            return self._event(level="error", message=s, source="esp32.panic",
                               fields={"backtrace": frames}, raw=line)
        if self._REG.match(s):
            regs = dict(re.findall(r"([A-Z][A-Z0-9]{1,7})\s*:\s*(0x[0-9a-fA-F]+)", s))
            return self._event(level="error", message=s, source="esp32.panic",
                               fields={"registers": regs} if regs else None, raw=line)
        return None


# ── ESP-IDF logging (esp_log) ────────────────────────────────────────────────
#   I (1523) wifi:connected with MyAP, aid = 1, channel 6
class EspIdfLogAdapter(LogAdapter):
    name = "esp_idf_log"
    language = "embedded"
    _RE = re.compile(r"^(?P<lvl>[IWEDV])\s+\((?P<ts>\d+|\d{2}:\d{2}:\d{2}\.\d+)\)\s+"
                     r"(?P<tag>[^:]+):\s?(?P<msg>.*)$")
    _LVL = {"E": "error", "W": "warn", "I": "info", "D": "debug", "V": "trace"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        ts = m.group("ts")
        fields = {}
        if ts.isdigit():
            fields["uptime_ms"] = int(ts)   # ms since boot (monotonic)
        return self._event(level=self._LVL.get(m.group("lvl"), ""),
                           message=m.group("msg"), source=m.group("tag").strip(),
                           ts_ms=None, fields=fields or None, raw=line)


# ── Zephyr RTOS logging ──────────────────────────────────────────────────────
#   [00:00:00.000,274] <inf> sample_instance.inst1: logging message
class ZephyrLogAdapter(LogAdapter):
    name = "zephyr_log"
    language = "embedded"
    _RE = re.compile(r"^\[(?P<ts>\d{2}:\d{2}:\d{2}\.\d{3},\d{3})\]\s+"
                     r"<(?P<lvl>err|wrn|inf|dbg)>\s+(?P<mod>[^:]+):\s?(?P<msg>.*)$")
    _LVL = {"err": "error", "wrn": "warn", "inf": "info", "dbg": "debug"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        return self._event(level=self._LVL.get(m.group("lvl"), ""), message=m.group("msg"),
                           source=m.group("mod").strip(), ts_ms=None,
                           fields={"uptime": m.group("ts")}, raw=line)


# ── Zephyr fatal error block ─────────────────────────────────────────────────
#   E: >>> ZEPHYR FATAL ERROR 3: Kernel oops on CPU 0
class ZephyrFatalAdapter(LogAdapter):
    name = "zephyr_fatal"
    language = "embedded"
    _HEAD = re.compile(r"^E:\s*>>> ZEPHYR FATAL ERROR (?P<num>\d+):\s*(?P<msg>.*)$")
    _REG = re.compile(r"^E:\s+(?:r\d+/a\d+:|xpsr:|lr:|pc:|EXC_RETURN|Faulting|Current thread:|"
                      r"[a-z0-9]+/[a-z0-9]+:\s+0x)")

    def detect(self, sample_lines):
        # BATCH-4 gap fix: the whole fault dump (head + register lines) may be
        # ONE element — score per logical sub-line so the block still detects.
        return ratio_detect(
            sample_lines,
            lambda ln: block_ratio(ln, lambda x: bool(
                self._HEAD.match(x.strip()) or self._REG.match(x.strip()))))

    def parse_line(self, line: str) -> Optional[dict]:
        pieces = split_any(line)
        if len(pieces) > 1:                # whole fault block → ONE fatal event
            head = next((self._HEAD.match(x.strip()) for x in pieces
                         if self._HEAD.match(x.strip())), None)
            regs = {}
            for x in pieces:
                if self._REG.match(x.strip()):
                    regs.update(dict(re.findall(
                        r"([a-zA-Z][\w/]*)\s*:\s*(0x[0-9a-fA-F]+)", x)))
            if head:
                fields = {"error_number": int(head.group("num"))}
                if regs:
                    fields["registers"] = regs
                return self._event(level="fatal", message=head.group("msg"),
                                   source="zephyr.fatal",
                                   trace_id=head.group("num"),
                                   fields=fields, raw=line)
            if regs:
                return self._event(level="error", message=pieces[0].strip(),
                                   source="zephyr.fatal",
                                   fields={"registers": regs}, raw=line)
            return None
        s = line.rstrip("\r\n").strip()
        m = self._HEAD.match(s)
        if m:
            return self._event(level="fatal", message=m.group("msg"),
                               source="zephyr.fatal", trace_id=m.group("num"),
                               fields={"error_number": int(m.group("num"))}, raw=line)
        if self._REG.match(s):
            regs = dict(re.findall(r"([a-zA-Z][\w/]*)\s*:\s*(0x[0-9a-fA-F]+)", s))
            return self._event(level="error", message=s[3:].strip(), source="zephyr.fatal",
                               fields={"registers": regs} if regs else None, raw=line)
        return None


# ── U-Boot boot log ──────────────────────────────────────────────────────────
#   U-Boot 2022.04 (Apr 20 2022 - 10:14:33 +0000)
#   DRAM:  2 GiB
class UBootAdapter(LogAdapter):
    name = "uboot_boot_log"
    language = "embedded"
    _BANNER = re.compile(r"^U-Boot(?: SPL)? (?P<ver>\d{4}\.\d{2}\S*)\s*(?:\((?P<build>[^)]*)\))?")
    _LINE = re.compile(r"^(Hit any key to stop autoboot|## Booting|Starting kernel|"
                       r"DRAM:\s|CPU:\s+\S|MMC:\s|Net:\s|Loading Environment|"
                       r"Model:\s|Board:\s|Reset cause:\s)")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._BANNER.match(ln.strip())
                                            or self._LINE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._BANNER.match(s)
        if m:
            return self._event(level="info", message=s, source="u-boot",
                               fields={"version": m.group("ver"),
                                       "build": m.group("build")}, raw=line)
        if not self._LINE.match(s):
            return None
        fields = {}
        lm = re.match(r"^(?P<k>[\w ]+?):\s+(?P<v>.*)$", s)
        if lm:
            fields[lm.group("k").strip().lower().replace(" ", "_")] = lm.group("v").strip()
        return self._event(level="info", message=s, source="u-boot",
                           fields=fields or None, raw=line)


# ── FreeRTOS logging (time + task name) ──────────────────────────────────────
#      12.045.678 [Tmr Svc      ] MQTT connection established
class FreeRtosAdapter(LogAdapter):
    name = "freertos_logging"
    language = "embedded"
    _RE = re.compile(r"^\s*(?P<s>\d+)\.(?P<ms>\d{3})\.(?P<us>\d{3})\s+"
                     r"\[(?P<task>[^\]]{1,20})\]\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln)))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        uptime = int(m.group("s")) + int(m.group("ms")) / 1e3 + int(m.group("us")) / 1e6
        return self._event(level=_infer(m.group("msg")), message=m.group("msg"),
                           source=m.group("task").strip(), ts_ms=None,
                           fields={"uptime": round(uptime, 6),
                                   "task": m.group("task").strip()}, raw=line)


# ── macOS unified logging (`log show`/`log stream` default) ──────────────────
#   2026-07-20 14:03:11.248693-0700 0x7c393  Default  0x0  10371  0  kernel: (Kext) foo
class MacOsUnifiedLogAdapter(LogAdapter):
    name = "macos_unified_log"
    language = "macos"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}[+-]\d{4})\s+"
        r"(?P<thread>0x[0-9a-f]+)\s+(?P<type>Default|Info|Debug|Error|Fault|Activity)\s+"
        r"(?P<act>0x[0-9a-f]+)\s+(?P<pid>\d+)\s+(?P<ttl>\d+)\s+(?P<rest>.*)$")
    _LVL = {"Fault": "fatal", "Error": "error", "Default": "info",
            "Info": "debug", "Debug": "debug", "Activity": "info"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln)))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        rest = m.group("rest")
        proc = ""
        msg = rest
        pm = re.match(r"^(?P<proc>[^:(]+?):\s*(?:\((?P<sub>[^)]*)\)\s*)?(?P<msg>.*)$", rest)
        fields = {"thread": m.group("thread"), "pid": int(m.group("pid")),
                  "type": m.group("type")}
        if pm:
            proc = pm.group("proc").strip()
            msg = pm.group("msg")
            if pm.group("sub"):
                fields["subsystem"] = pm.group("sub")
        return self._event(level=self._LVL.get(m.group("type"), ""), message=msg or rest,
                           source=proc or "macos", ts_ms=parse_timestamp(m.group("ts")),
                           fields=fields, raw=line)


# ── rsyslog RSYSLOG_FileFormat + journald short-iso ──────────────────────────
#   2026-07-20T14:03:11.123456+00:00 myhost myapp[1234]: config reloaded
class RsyslogAdapter(LogAdapter):
    name = "rsyslog"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.,]\d+)?[+-]\d{2}:?\d{2})\s+"
        r"(?P<host>[\w.\-]+)\s+(?P<tag>[\w.\-/]+)(?:\[(?P<pid>\d+)\])?:\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        msg = m.group("msg")
        fields = {"host": m.group("host")}
        if m.group("pid"):
            fields["pid"] = m.group("pid")
        return self._event(level=_infer(msg), message=msg, source=m.group("tag"),
                           ts_ms=parse_timestamp(m.group("ts")), fields=fields, raw=line)


# ── Linux ftrace (tracefs) ───────────────────────────────────────────────────
#            <idle>-0     [000] d.h. 12345.678901: sched_switch: prev_comm=swapper ...
class FtraceAdapter(LogAdapter):
    name = "ftrace"
    language = "linux"
    # BATCH-6 gap fix: the irq/preempt flags column is OPTIONAL — `trace-cmd
    # report` emits the same TASK-PID [CPU] TIMESTAMP: event: layout without it.
    _RE = re.compile(
        r"^\s*(?P<task>\S.*?)-(?P<pid>\d+)\s+\[(?P<cpu>\d{3})\]\s+"
        r"(?:(?P<flags>[.a-zA-Z0-9]{4})\s+)?(?P<ts>\d+\.\d{6}):\s+"
        r"(?P<event>[\w.]+):\s?(?P<msg>.*)$")
    # BATCH-4 gap fix — the function_graph tracer gutter:
    #    2)   1.381 us    |  _raw_spin_lock();
    #    2) + 12.912 us   |  } /* handle_mm_fault */
    _FGRAPH = re.compile(
        r"^\s*(?P<cpu>\d+)\)\s+(?:(?P<mark>[+!#*@])\s*)?"
        r"(?:(?P<dur>[\d.]+)\s+us\s+)?\|\s?(?P<body>.*)$")

    def detect(self, sample_lines):
        def hit(el):
            return any(self._RE.match(x) or self._FGRAPH.match(x)
                       or x.strip().startswith("# tracer:")
                       for x in split_any(el))
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if s.startswith("#"):
            return self._event(level="", message=s, source="ftrace",
                               fields={"ftrace_meta": True}, raw=line)
        m = self._RE.match(s)
        if m:
            return self._event(level="", message=f'{m.group("event")}: {m.group("msg")}',
                               source=m.group("event"), ts_ms=None,
                               fields={"task": m.group("task").strip(), "pid": int(m.group("pid")),
                                       "cpu": int(m.group("cpu")), "flags": m.group("flags"),
                                       "uptime": float(m.group("ts"))}, raw=line)
        # function_graph gutter — a block may carry several rows; emit the first
        for x in split_any(s):
            fg = self._FGRAPH.match(x)
            if not fg:
                continue
            fields = {"cpu": int(fg.group("cpu")), "tracer": "function_graph"}
            if fg.group("dur"):
                fields["duration_us"] = float(fg.group("dur"))
            if fg.group("mark"):
                fields["latency_mark"] = fg.group("mark")   # + >10us, ! >100us…
            return self._event(level="", message=fg.group("body").strip(),
                               source="ftrace", fields=fields, raw=line)
        return None


# ── ARM Trusted Firmware-A boot log (BL1/BL2/BL31/BL32) — BATCH 4 ─────────────
#   NOTICE:  BL1: Booting BL2 / INFO:    Loading image id=1 at address 0x4001000
class TfaBootAdapter(LogAdapter):
    name = "tfa_bootlog"
    language = "embedded"
    # the level word is padded to a fixed 9-char column → ≥2 spaces after the
    # colon, which is what keeps a generic "ERROR: msg" line from firing this.
    _RE = re.compile(r"^(?P<lvl>NOTICE|INFO|WARNING|ERROR|VERBOSE):\s{2,}(?P<msg>\S.*)$")
    _BL = re.compile(r"^(?P<bl>BL3[12]|BL[12]U?|EL3 Payload|SP_MIN):\s*(?P<rest>.*)$")
    # uvicorn's "INFO:     msg" console format is byte-identical in shape — a
    # sample only detects as TF-A when a firmware anchor is present somewhere
    # in it (a BLx boot-stage token, a NOTICE/VERBOSE level, or the banner).
    _ANCHOR = re.compile(r"\b(BL3[12]|BL[12]U?|EL3 Payload|SP_MIN|"
                         r"Trusted Firmware)\b|^(NOTICE|VERBOSE):\s{2,}")
    _LVL = {"NOTICE": "info", "VERBOSE": "trace"}

    def detect(self, sample_lines):
        anchored = any(
            any(self._ANCHOR.search(x.strip()) for x in split_any(el))
            for el in sample_lines if str(el).strip())
        if not anchored:
            return 0.0
        return ratio_detect(
            sample_lines,
            lambda ln: block_ratio(ln, lambda x: bool(self._RE.match(x.strip()))))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"].strip()
        fields = {}
        source = "tf-a"
        bm = self._BL.match(msg)
        if bm:
            fields["boot_stage"] = bm.group("bl")
            source = f"tf-a.{bm.group('bl')}"
        return self._event(level=self._LVL.get(g["lvl"], g["lvl"]), message=msg,
                           source=source, fields=fields or None, raw=line)


# ── EDK2 / TianoCore UEFI DEBUG serial log — BATCH 4 ──────────────────────────
#   Loading driver at 0x0007E4C4000 EntryPoint=0x0007E4C8260 …
#   DEBUG [DXE] InstallProtocolInterface: 5B1B31A1-9562-11D2-… 7E4A98
class Edk2UefiAdapter(LogAdapter):
    name = "edk2_uefi"
    language = "firmware"
    _MARK = re.compile(
        r"^(DEBUG \[[A-Z]+\]|PROGRESS CODE:|Loading driver at 0x|"
        r"InstallProtocolInterface:|Loading PEIM |Loading DXE CORE|"
        r"BdsDxe:|add-symbol-file |Found DXE Core|CoreInitialize|"
        r"ConvertPages:|Memory Allocation |ASSERT [/\\\w])")
    _ASSERT = re.compile(r"^ASSERT (?P<file>\S+)\((?P<ln>\d+)\):\s*(?P<expr>.*)$")
    _GUID = re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                       r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b")

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda ln: block_ratio(ln, lambda x: bool(self._MARK.match(x.strip()))))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if not self._MARK.match(s):
            return None
        am = self._ASSERT.match(s)
        if am:
            return self._event(level="fatal", category="error",
                               message=s, source="edk2",
                               fields={"file": am.group("file"),
                                       "line": int(am.group("ln")),
                                       "expression": am.group("expr")}, raw=line)
        fields = {}
        gm = self._GUID.search(s)
        if gm:
            fields["guid"] = gm.group(0)
        phase = re.match(r"^DEBUG \[(?P<ph>[A-Z]+)\]\s*(?P<rest>.*)$", s)
        if phase:
            fields["phase"] = phase.group("ph")
            s_msg = phase.group("rest") or s
        else:
            s_msg = s
        entry = re.search(r"EntryPoint=(0x[0-9A-Fa-f]+)", s)
        if entry:
            fields["entry_point"] = entry.group(1)
        return self._event(level="debug", message=s_msg, source="edk2",
                           fields=fields or None, raw=line)


# ── coreboot cbmem console (cbmem -c) — BATCH 4 ───────────────────────────────
#   [DEBUG]  Enumerating buses... / [INFO ]  PCI: pci_scan_bus for bus 00
class CorebootCbmemAdapter(LogAdapter):
    name = "coreboot_cbmem"
    language = "firmware"
    # the tag content is EXACTLY 5 chars (width-padded: "INFO ", "WARN ",
    # "NOTE ", "SPEW ", "CRIT ") — that trailing pad is what separates coreboot
    # from ROS/coredns "[INFO]" prefixes, so it is mandatory here.
    _RE = re.compile(r"^(?:\[(?P<up>\d+\.?\d*)\]\s*)?"
                     r"\[(?P<lvl>EMERG|ALERT|ERROR|DEBUG|CRIT |WARN |NOTE |INFO |SPEW )\]"
                     r"\s+(?P<msg>\S.*)$")
    _LVL = {"EMERG": "fatal", "ALERT": "fatal", "CRIT": "fatal",
            "NOTE": "info", "SPEW": "trace"}

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda ln: block_ratio(ln, lambda x: bool(self._RE.match(x.strip()))))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"].strip()
        fields = {}
        if g.get("up"):
            try:
                fields["uptime"] = float(g["up"])
            except ValueError:
                pass
        sub = re.match(r"^(?P<s>[A-Z][A-Za-z0-9]{1,12}):\s+", msg)
        lvl = g["lvl"].strip()             # width-padded tag → bare level word
        return self._event(level=self._LVL.get(lvl, lvl), message=msg,
                           source=f"coreboot.{sub.group('s')}" if sub else "coreboot",
                           fields=fields or None, raw=line)


# ── ESP32 ROM / 2nd-stage bootloader banner — BATCH 4 ─────────────────────────
#   rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)
#   configsip: 0, SPIWP:0xee
class Esp32RomBootAdapter(LogAdapter):
    name = "esp32_rom_boot"
    language = "embedded"
    _RST = re.compile(r"^rst:(?P<rst>0x[0-9a-fA-F]+)\s*\((?P<cause>[A-Z0-9_]+)\)"
                      r"(?:,boot:(?P<boot>0x[0-9a-fA-F]+)\s*\((?P<mode>[A-Z0-9_]+)\))?")
    _MARK = re.compile(r"^(configsip:|clk_drv:|mode:(?:DIO|QIO|DOUT|QOUT)|"
                       r"load:0x[0-9a-fA-F]+|entry 0x[0-9a-fA-F]+|"
                       r"ets [A-Z][a-z]{2}\s|waiting for download|"
                       r"ho \d+ tail \d+ room \d+|csum 0x)")

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda ln: block_ratio(ln, lambda x: bool(
                self._RST.match(x.strip()) or self._MARK.match(x.strip())),
                threshold=0.4))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._RST.match(s)
        if m:
            g = m.groupdict()
            cause = g["cause"]
            # brown-out / watchdog resets are the diagnostic gold here
            level = ("error" if any(w in cause for w in ("BROWN", "WDT", "PANIC"))
                     else "info")
            fields = {"reset_reg": g["rst"], "reset_cause": cause}
            if g.get("mode"):
                fields["boot_mode"] = g["mode"]
            return self._event(level=level, message=s, source="esp32.rom",
                               fields=fields, raw=line)
        if self._MARK.match(s):
            return self._event(level="debug", message=s, source="esp32.rom",
                               raw=line)
        return None


# ── Registration ─────────────────────────────────────────────────────────────
# kernel_panic_oops must beat linux_dmesg on a panic-in-dmesg line → register it
# first (default placement preserves registration order). netfilter rides in a
# syslog envelope → before="syslog"; the ISO rsyslog envelope → before="systemd"
# (so the specific security auth adapters, before="syslog", still win their lines).
# esp32_rom_boot before esp_idf_log: a boot banner block contains one esp_idf
# "I (31) boot:" line — the ROM markers dominate, but keep the tiebreak sane.
# ═══════════════════════════ BATCH 5 ═════════════════════════════════════════

# ── WireGuard kernel-module dmesg lines ────────────────────────────────────────
#   wireguard: wg0: Receiving handshake initiation from peer 3 (203.0.113.7:51820)
#   (with or without the dmesg "[ 12.345]" / syslog prefix)
class WireguardAdapter(LogAdapter):
    name = "wireguard"
    language = "any"
    _RE = re.compile(
        r"^(?:<\d+>)?(?:\[\s*\d+\.\d+\]\s*)?"
        r"wireguard:\s+(?P<iface>[\w.\-]+):\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"]
        level = ("error" if re.search(r"(?i)invalid|failed|could not|unable",
                                      msg)
                 else "warn" if re.search(r"(?i)did not complete|retrying|"
                                          r"zeroing out|expired", msg)
                 else "info")
        fields = {"interface": g["iface"]}
        pm = re.search(r"peer (\d+)(?: \(([\d.a-fA-F:\[\]]+:\d+)\))?", msg)
        if pm:
            fields["peer"] = int(pm.group(1))
            if pm.group(2):
                fields["endpoint"] = pm.group(2)
        return self._event(level=level, message=msg,
                           source=f'wireguard.{g["iface"]}',
                           fields=fields, raw=line)


register_adapter(KernelPanicOopsAdapter())
register_adapter(LinuxDmesgAdapter())
register_adapter(LinuxDmesgCtimeAdapter())
register_adapter(LinuxDevKmsgAdapter())
register_adapter(NetfilterKlogAdapter(), before="syslog")
register_adapter(Esp32PanicAdapter())
register_adapter(Esp32RomBootAdapter())
register_adapter(EspIdfLogAdapter())
register_adapter(ZephyrLogAdapter())
register_adapter(ZephyrFatalAdapter())
register_adapter(UBootAdapter())
register_adapter(FreeRtosAdapter())
register_adapter(MacOsUnifiedLogAdapter())
register_adapter(RsyslogAdapter(), before="systemd")
register_adapter(FtraceAdapter())
register_adapter(TfaBootAdapter())
register_adapter(Edk2UefiAdapter())
register_adapter(CorebootCbmemAdapter())
# batch 5 — wireguard lines usually ride the dmesg "[ts]" envelope; register
# before linux_dmesg so the named adapter wins that 1.0 tie.
register_adapter(WireguardAdapter(), before="linux_dmesg")


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH 8 — storage-driver kernel messages + embedded-target consoles
# ══════════════════════════════════════════════════════════════════════════════
from ._common import RxAdapter, vocab_detect  # noqa: E402


# ── Lustre parallel-filesystem kernel messages ────────────────────────────────
#   LustreError: 5303:0:(quota_ctl.c:328:client_quota_ctl()) ptlrpc_queue_wait …
class LustreKernelAdapter(RxAdapter):
    name = "lustre_kernel"
    language = "linux"
    default_source = "lustre"
    _RE = re.compile(
        r"^(?:kernel: )?(?P<comp>Lustre|LustreError):\s*"
        r"(?:(?P<loc>\d+:\d+:\([^)]*\))\s*)?(?P<msg>.*)$")

    def _level(self, g, line):
        return "error" if g["comp"] == "LustreError" else "info"

    def _fields(self, g, line):
        return {"locator": g["loc"]} if g.get("loc") else None


# ── QLogic qla2xxx / Emulex lpfc Fibre-Channel HBA kernel log ─────────────────
#   kernel: qla2xxx [0000:0b:00.0]-500a:0: LOOP UP detected (8 Gbps).
class Qla2xxxAdapter(RxAdapter):
    name = "qla2xxx"
    language = "linux"
    default_source = "qla2xxx"
    _RE = re.compile(
        r"^(?:kernel: )?(?P<drv>qla2xxx|lpfc)\b\s*"
        r"(?:\[(?P<pci>[0-9a-fA-F:.]+)\])?[-\s](?P<msg>.*)$")

    def _level(self, g, line):
        return "error" if re.search(r"fail|down|error|abort|reset", g.get("msg", ""), re.I) else "info"

    def _fields(self, g, line):
        return {"driver": g["drv"], "pci": g.get("pci")}


# ── Linux nfsd / lockd server-side kernel messages ────────────────────────────
#   kernel: nfsd: last server has exited, flushing export cache
class NfsdKernelAdapter(RxAdapter):
    name = "nfsd_kernel"
    language = "linux"
    default_source = "nfsd"
    _RE = re.compile(r"^(?:kernel: )?(?P<svc>nfsd|lockd|rpc\.\w+):\s*(?P<msg>.*)$")

    def _level(self, g, line):
        return "error" if re.search(r"fail|error|cannot|unable|refused", g.get("msg", ""), re.I) else "info"

    def _fields(self, g, line):
        return {"subsystem": g["svc"]}


# ── IBM Storage Scale / GPFS mmfs.log ─────────────────────────────────────────
#   Fri Dec 29 18:26:11 CST 2023: 6027-1623 mmmount: Mounting file systems …
class GpfsMmfsAdapter(RxAdapter):
    name = "gpfs_mmfs"
    language = "any"
    default_source = "gpfs"
    _RE = re.compile(
        r"^(?:(?P<ts>[A-Z][a-z]{2} [A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2}(?:\s+[A-Z]{2,4})?\s+\d{4}):\s+)?"
        r"(?:GPFS:\s*)?(?P<code>6027-\d+)\s+(?P<msg>.*)$")

    def _ts(self, g):
        ts = g.get("ts")
        if not ts:
            return None
        return parse_timestamp(re.sub(r"\s+[A-Z]{2,4}\s+(\d{4})", r" \1", ts))

    def _level(self, g, line):
        return "error" if re.search(r"fail|error|cannot|unable", g.get("msg", ""), re.I) else "info"

    def _fields(self, g, line):
        return {"message_id": g["code"]}


# ── SEGGER J-Link RTT terminal output ─────────────────────────────────────────
#   00> [APP] sensor init ok   /   01> assertion failed at main.c:42
class SeggerRttAdapter(RxAdapter):
    name = "segger_rtt"
    language = "embedded"
    default_source = "rtt"
    _RE = re.compile(r"^(?P<chan>\d{2})>\s?(?P<msg>.*)$")

    def _level(self, g, line):
        return "error" if re.search(r"assert|fail|error|fault|panic", g.get("msg", ""), re.I) else ""

    def _fields(self, g, line):
        return {"rtt_channel": int(g["chan"])}


# ── Wind River VxWorks logMsg() console output ────────────────────────────────
#   0x1f4a2c0 (tNetTask): route add failed, errno = 0x3d
class VxworksLogmsgAdapter(RxAdapter):
    name = "vxworks_logmsg"
    language = "vxworks"
    default_source = "vxworks"
    _RE = re.compile(r"^(?P<taskid>0x[0-9a-fA-F]+)\s+\((?P<task>t\w+)\):\s*(?P<msg>.*)$")

    def _level(self, g, line):
        return "error" if re.search(r"fail|error|errno|abort|fault", g.get("msg", ""), re.I) else ""

    def _fields(self, g, line):
        return {"task_id": g["taskid"], "task": g["task"]}

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            m = self._RE.match(line.strip())
            ev["source"] = m.group("task")
        return ev


# ── VxWorks exception dump block ──────────────────────────────────────────────
#   Exception!  /  Program Counter: 0x00123abc  /  Exception Type: … /  Task: …
class VxworksExceptionAdapter(LogAdapter):
    name = "vxworks_exception"
    language = "vxworks"
    _HEAD = re.compile(r"^Exception!?\s*$|^Exception\b")
    _PC = re.compile(r"^Program Counter:\s*(?P<pc>0x[0-9a-fA-F]+)")
    _TYPE = re.compile(r"^Exception Type:\s*(?P<t>.+)$")
    _TASK = re.compile(r'^Task:\s*(?P<task>0x[0-9a-fA-F]+(?:\s+"[^"]*")?)')

    def detect(self, sample_lines):
        def hit(el):
            subs = [x.strip() for x in split_any(el)]
            return bool(subs) and (self._HEAD.match(subs[0])
                                   and any(self._PC.match(x) or self._TYPE.match(x)
                                           for x in subs))
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        subs = [x.strip() for x in split_any(s)]
        fields = {}
        for x in subs:
            for rx, key in ((self._PC, "program_counter"), (self._TYPE, "exception_type"),
                            (self._TASK, "task")):
                m = rx.match(x)
                if m:
                    fields[key] = list(m.groupdict().values())[0].strip()
        return self._event(level="fatal", message="VxWorks exception: "
                           + fields.get("exception_type", "fault"),
                           source="vxworks", fields=fields or None,
                           category="crash", raw=line)


# ── libmodbus debug frame trace ───────────────────────────────────────────────
#   [00][01][00][00][00][06][FF][03][00][00][00][01]
class LibmodbusDebugAdapter(LogAdapter):
    name = "libmodbus_debug"
    language = "embedded"
    _FRAME = re.compile(r"^(?:\[[0-9A-Fa-f]{2}\]){4,}$|^(?:<[0-9A-Fa-f]{2}>){4,}$")
    _WAIT = re.compile(r"^Waiting for (?:an indication|a confirmation)\.\.\.$")

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda el: block_ratio(el, lambda x: bool(self._FRAME.match(x.strip())
                                                       or self._WAIT.match(x.strip()))))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        if self._WAIT.match(s):
            return self._event(level="", message=s, source="libmodbus", raw=line)
        if not self._FRAME.match(s):
            return None
        octets = re.findall(r"[0-9A-Fa-f]{2}", s)
        return self._event(level="", message=f"Modbus frame ({len(octets)} bytes)",
                           source="libmodbus",
                           fields={"bytes": octets, "direction": "rx" if s[0] == "<" else "tx"},
                           category="event", raw=line)


# ── coreboot cbmem -t boot-timestamp table ────────────────────────────────────
#   1:start of bootblock         912,344
#   10:start of ramstage        515,655 (12,003)
class CorebootTimestampsAdapter(LogAdapter):
    name = "coreboot_timestamps"
    language = "firmware"
    _RE = re.compile(
        r"^(?P<id>\d+):(?P<desc>.+?)\s+(?P<val>\d{1,3}(?:,\d{3})+)"
        r"(?:\s+\((?P<delta>\d[\d,]*)\))?\s*$")

    def detect(self, sample_lines):
        return vocab_detect(
            sample_lines,
            lambda el: block_ratio(el, lambda x: bool(self._RE.match(x.strip()))),
            cap=0.9)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        m = self._RE.match(s.strip())
        if not m:
            for x in split_any(s):
                if self._RE.match(x.strip()):
                    m = self._RE.match(x.strip())
                    break
        if not m:
            return None
        g = m.groupdict()
        fields = {"entry_id": int(g["id"]),
                  "value_us": int(g["val"].replace(",", ""))}
        if g.get("delta"):
            fields["delta_us"] = int(g["delta"].replace(",", ""))
        return self._event(level="", message=g["desc"].strip(),
                           source="coreboot.cbmem", fields=fields,
                           category="event", raw=line)


# ── Android LK / aboot bootloader (Little Kernel) UART log ────────────────────
#   [0] welcome to lk   /   [230] booting linux @ 0x80008000 …
class AndroidLkAdapter(LogAdapter):
    name = "android_lk"
    language = "android"
    _RE = re.compile(r"^\[(?P<ms>\d+)\]\s+(?P<msg>.*)$")
    _VOCAB = re.compile(
        r"welcome to lk|platform_init|booting linux|aboot|target_init|"
        r"mmc_boot|display_init|APPS image|fastboot", re.I)

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            bracketed = [x for x in subs if self._RE.match(x.strip())]
            return bool(bracketed) and any(self._VOCAB.search(x) for x in subs)
        return vocab_detect(sample_lines, hit, cap=0.8)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        m = self._RE.match(s.strip())
        if not m:
            for x in split_any(s):
                if self._RE.match(x.strip()):
                    m = self._RE.match(x.strip())
                    break
        if not m:
            return None
        g = m.groupdict()
        return self._event(level="error" if re.search(r"fail|error|panic", g["msg"], re.I) else "",
                           message=g["msg"], source="android.lk",
                           fields={"uptime_ms": int(g["ms"])}, raw=line)


# lustre/qla2xxx/nfsd ride the bare "kernel:" line shape; register before
# linux_dmesg so the specific driver wins the tie on its own samples.
for _a in (LustreKernelAdapter(), Qla2xxxAdapter(), NfsdKernelAdapter()):
    register_adapter(_a, before="linux_dmesg")
for _a in (GpfsMmfsAdapter(), SeggerRttAdapter(), VxworksLogmsgAdapter(),
           VxworksExceptionAdapter(), LibmodbusDebugAdapter(),
           CorebootTimestampsAdapter(), AndroidLkAdapter()):
    register_adapter(_a)
