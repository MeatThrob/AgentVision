"""
Console / game-console / FreeBSD-derived kernel log adapters
================================================================================
PlayStation 5 (Orbis OS, FreeBSD-derived) and FreeBSD console formats. These are
directly relevant to AgentVision's PS5 homebrew / SharpEmu HLE debugging: the
[SceXxx] module tag is the primary routing signal, and the FreeBSD trap_fatal /
panic block is what a kernel crash looks like on that platform.

Formats: ps5_orbis_klog, orbis_module_tag, orbis_fatal_trap, freebsd_messages.
"""
from __future__ import annotations

import re
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp,
                      _SYSLOG_SEVERITY, ratio_detect)

# Sony userland subsystem tags used to route by module.
_SCE_TAG = re.compile(r"\[Sce[A-Za-z0-9_]+\]")
_HEX_RET = re.compile(r"\br(\d+)=0x([0-9a-fA-F]+)")


# ── PS5 / Orbis /dev/klog kernel log (klogsrv, TCP 3232) ─────────────────────
#   <134>[SceLncSysServiceProcess] app launch requested, titleId=CUSA00000
class Ps5OrbisKlogAdapter(LogAdapter):
    name = "ps5_orbis_klog"
    language = "orbis"
    # <PRI> immediately followed by a Sony [SceXxx] tag — specific enough that it
    # never collides with the generic RFC5424/RFC3164 syslog adapter (those need
    # a version digit or a 'Mon DD' date right after the '>').
    _RE = re.compile(r"^<(?P<pri>\d{1,3})>\s*(?P<tag>\[Sce[A-Za-z0-9_]+\])\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        pri = int(m.group("pri"))
        module = m.group("tag").strip("[]")
        fields = {"module": module, "facility": pri >> 3, "priority": pri}
        for rn, hx in _HEX_RET.findall(m.group("msg")):
            fields[f"r{rn}"] = "0x" + hx
        tm = re.search(r"titleId=([A-Za-z0-9]+)", m.group("msg"))
        if tm:
            fields["titleId"] = tm.group(1)
        return self._event(level=_SYSLOG_SEVERITY.get(pri & 0x07, "INFO"),
                           message=m.group("msg"), source=module, fields=fields, raw=line)


# ── Orbis userland module log lines (no <PRI>) ───────────────────────────────
#   [SceShellCore] NetworkService: connection established r0=0x0
class OrbisModuleTagAdapter(LogAdapter):
    name = "orbis_module_tag"
    language = "orbis"
    _RE = re.compile(r"^(?P<tag>\[Sce[A-Za-z0-9_]+\])\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        # A bare [SceXxx] at line start with NO leading <PRI> (that is klog above).
        return ratio_detect(
            sample_lines,
            lambda ln: bool(self._RE.match(ln.strip())) and not ln.strip().startswith("<"))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if s.startswith("<"):
            return None
        m = self._RE.match(s)
        if not m:
            return None
        module = m.group("tag").strip("[]")
        msg = m.group("msg")
        fields = {"module": module}
        for rn, hx in _HEX_RET.findall(msg):
            fields[f"r{rn}"] = "0x" + hx
        # Promote obvious failures (return code != 0 or error words) to error.
        low = msg.lower()
        level = ""
        if any(w in low for w in ("fail", "error", "denied", "fatal", "assert")):
            level = "error"
        elif re.search(r"\br\d+=0x[0-9a-fA-F]*[1-9a-fA-F]", msg):
            level = "warn"
        return self._event(level=level, message=msg, source=module,
                           fields=fields, raw=line)


# ── FreeBSD / PS4 / PS5 kernel fatal trap / panic block ──────────────────────
#   Fatal trap 12: page fault while in kernel mode
#   cpuid = 3; apic id = 03
#   fault virtual address = 0x0
#   instruction pointer = 0x20:0xffffffff82012345
class OrbisFatalTrapAdapter(LogAdapter):
    name = "orbis_fatal_trap"
    language = "freebsd"
    _HEAD = re.compile(r"^(?:Fatal trap (?P<trap>\d+):\s*(?P<desc>.*)|panic:\s*(?P<pmsg>.*))$")
    # classic FreeBSD trap_fatal body labels (values are 'k = v' with spaces)
    _KV = re.compile(r"^(?P<key>cpuid|apic id|fault virtual address|fault code|"
                     r"instruction pointer|stack pointer|frame pointer|"
                     r"code segment|processor flags|current process|"
                     r"trap number|panic message)\s*=\s*(?P<val>.*)$")

    def _is_block_line(self, s: str) -> bool:
        return bool(self._HEAD.match(s) or self._KV.match(s)
                    or s.startswith("cpuid = "))

    def detect(self, sample_lines):
        # The trap/panic report is a MULTI-LINE block. A caller (or the format
        # catalog) may hand us the whole block as ONE element with embedded
        # newlines, or feed it a line at a time. Either way, count an element as
        # a hit when a majority of its non-blank sub-lines are trap-block lines,
        # so detect_adapter([whole_block]) resolves here instead of falling to
        # the structural normalizer. The `_HEAD`/`_KV` labels are specific enough
        # that this never steals a generic multi-line text sample.
        def ok(ln):
            subs = [x for x in str(ln).splitlines() if x.strip()]
            if not subs:
                return False
            hits = sum(1 for x in subs if self._is_block_line(x.strip()))
            return hits >= 1 and (hits / len(subs)) >= 0.5
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._HEAD.match(s)
        if m:
            if m.group("trap") is not None:
                return self._event(level="fatal", message=f"Fatal trap {m.group('trap')}: "
                                   f"{m.group('desc')}".strip(), source="kernel.trap",
                                   trace_id=m.group("trap"),
                                   fields={"trap_number": int(m.group("trap")),
                                           "trap_desc": m.group("desc")}, raw=line)
            return self._event(level="fatal", message=f"panic: {m.group('pmsg')}",
                               source="kernel.panic", fields={"panic": m.group("pmsg")},
                               raw=line)
        m = self._KV.match(s)
        if m:
            key = m.group("key").replace(" ", "_")
            return self._event(level="error", message=s, source="kernel.trap",
                               fields={key: m.group("val").strip()}, raw=line)
        return None


# ── FreeBSD /var/log/messages kernel lines (BSD syslog, tag == kernel) ───────
#   Jul 20 14:03:11 fbsdhost kernel: ada0: <Samsung SSD 870 SVQ01B6Q> ...
class FreeBsdMessagesAdapter(LogAdapter):
    name = "freebsd_messages"
    language = "freebsd"
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(?P<host>[\w.\-]+)\s+"
        r"(?P<tag>kernel|syslogd|kernel\.[a-z]+)(?:\[(?P<pid>\d+)\])?:\s+(?P<msg>.*)$")

    def _ok(self, s: str) -> bool:
        # Leave <PRI>-prefixed lines to the syslog adapter, and Netfilter LOG
        # lines (SRC=/DST=) to the netfilter adapter.
        if s.startswith("<"):
            return False
        m = self._RE.match(s)
        if not m:
            return False
        return " SRC=" not in s or " IN=" not in s

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: self._ok(ln.strip()))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if s.strip().startswith("<"):
            return None
        m = self._RE.match(s.strip())
        if not m:
            return None
        msg = m.group("msg")
        low = msg.lower()
        level = "error" if any(w in low for w in ("error", "fail", "panic", "fatal")) else ""
        fields = {"host": m.group("host")}
        if m.group("pid"):
            fields["pid"] = m.group("pid")
        # FreeBSD device name prefix "ada0: ..." → surface as a hint.
        dev = re.match(r"^([a-z]+\d+):\s", msg)
        if dev:
            fields["device"] = dev.group(1)
        return self._event(level=level, message=msg, source="kernel",
                           ts_ms=parse_timestamp(m.group("ts")), fields=fields, raw=line)


# ── Registration ─────────────────────────────────────────────────────────────
# <PRI>[Sce…] and netfilter-shaped kernel lines must outrank the generic
# syslog/systemd adapters on a 1.0 tie → register before="syslog"/"systemd".
register_adapter(Ps5OrbisKlogAdapter(), before="syslog")
register_adapter(OrbisModuleTagAdapter())
register_adapter(OrbisFatalTrapAdapter())
register_adapter(FreeBsdMessagesAdapter(), before="systemd")


# ═════════════════════════════════════════════════════════════════════════════
# BATCH 6
# ═════════════════════════════════════════════════════════════════════════════
from ._common import multiline_ratio_detect as _ml_detect, mk_ts as _mk_ts  # noqa: E402


# ── Unreal Engine log (UE_LOG) ────────────────────────────────────────────────
#   [2026.07.20-14.00.01:123][  0]LogTemp: Warning: RandomStream seed = 987654321
class UnrealAdapter(LogAdapter):
    name = "unreal"
    language = "cpp"
    _RE = re.compile(
        r"^\[(?P<yr>\d{4})\.(?P<mo>\d{2})\.(?P<dy>\d{2})-"
        r"(?P<hh>\d{2})\.(?P<mi>\d{2})\.(?P<ss>\d{2}):(?P<ms>\d{3})\]"
        r"\[\s*(?P<frame>\d+)\](?P<cat>[A-Za-z][\w]*):\s?"
        r"(?:(?P<verb>Fatal|Error|Warning|Display|Log|Verbose|VeryVerbose):\s?)?"
        r"(?P<msg>.*)$")
    _LVL = {"Fatal": "fatal", "Error": "error", "Warning": "warn",
            "Display": "info", "Log": "info", "Verbose": "debug",
            "VeryVerbose": "trace"}

    def detect(self, sample_lines):
        return _ml_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        ts_ms = _mk_ts(g["yr"], g["mo"], g["dy"], g["hh"], g["mi"], g["ss"],
                       int(g["ms"]) * 1000)
        return self._event(level=self._LVL.get(g["verb"], "info"),
                           message=g["msg"], source=g["cat"], ts_ms=ts_ms,
                           fields={"frame": int(g["frame"]),
                                   "verbosity": g["verb"] or "Log"}, raw=line)


register_adapter(UnrealAdapter())
