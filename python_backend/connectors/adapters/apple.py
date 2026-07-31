"""
Apple / macOS platform log adapters (BATCH 6)
================================================================================
The macOS text renderings BEYOND the unified-log default (which the kernel
module's `macos_unified_log` owns): the `log show --style compact` 2-letter
level codes, the `syslog`/ASL CLI rendering with `<Notice>:` levels, the
"ISO-ish ts + host + proc[pid]:" system.log/install.log style, and the legacy
.crash text crash report. The modern .ips crash report shipped in batch 5.

Formats: macos_log_compact, macos_asl, macos_syslog_style, apple_crash_legacy.
"""
from __future__ import annotations

import re
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp,
                      ratio_detect, multiline_ratio_detect, split_any)


# ── `log show --style compact` ────────────────────────────────────────────────
#   2022-04-26 09:34:12.123 Df loginwindow[139:7a3] [com.apple.loginwindow:General] msg
class MacosLogCompactAdapter(LogAdapter):
    name = "macos_log_compact"
    language = "macos"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3,6})\s+"
        r"(?P<lvl>Db|In|Df|Er|Ft)\s+"
        r"(?P<proc>[\w.\-]+)\[(?P<pid>\d+):(?P<tid>[0-9a-fx]+)\]\s+"
        r"(?:\[(?P<sub>[\w.\-]+):(?P<cat>[\w.\-]+)\]\s*)?(?P<msg>.*)$")
    _LVL = {"Db": "debug", "In": "info", "Df": "info", "Er": "error", "Ft": "fatal"}

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {"pid": int(g["pid"]), "thread": g["tid"]}
        if g["sub"]:
            fields["subsystem"] = g["sub"]
            fields["category"] = g["cat"]
        return self._event(level=self._LVL.get(g["lvl"], "info"), message=g["msg"],
                           source=g["proc"], ts_ms=parse_timestamp(g["ts"]),
                           fields=fields, raw=line)


# ── `syslog` CLI / ASL rendering (BSD header + <Level>: token) ───────────────
#   Jul 20 12:34:56 alices-macbook-pro com.apple.xpc.launchd[1] <Notice>: msg
class MacosAslAdapter(LogAdapter):
    name = "macos_asl"
    language = "macos"
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(?P<host>\S+)\s+"
        r"(?P<proc>[\w.\-]+)\[(?P<pid>\d+)\]\s+"
        r"<(?P<lvl>Emergency|Alert|Critical|Error|Warning|Notice|Info|Debug)>:\s?"
        r"(?P<msg>.*)$")
    _LVL = {"Emergency": "fatal", "Alert": "fatal", "Critical": "fatal",
            "Error": "error", "Warning": "warn", "Notice": "info",
            "Info": "info", "Debug": "debug"}

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=self._LVL.get(g["lvl"], "info"), message=g["msg"],
                           source=g["proc"], ts_ms=parse_timestamp(g["ts"]),
                           fields={"pid": int(g["pid"]), "host": g["host"],
                                   "asl_level": g["lvl"]}, raw=line)


# ── system.log / install.log style: "ISO-ish ts host proc[pid]: msg" ─────────
#   2026-07-20 14:03:11.248693-0700  localhost kernel[0]: (AppleACPIPlatform) …
#   2022-04-25 08:53:22-07 MacBookPro softwareupdated[365]: SUOSUServiceDaemon: …
class MacosSyslogStyleAdapter(LogAdapter):
    name = "macos_syslog_style"
    language = "macos"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}(?:\d{2})?)?)\s+"
        r"(?P<host>[\w.\-]+)\s+(?P<proc>[\w.\- /]+?)\[(?P<pid>\d+)\]"
        r"(?:\s+\((?P<sub>[^)]*)\))?:\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"]
        # message may itself start "(Kext) rest" like unified-log renderings
        sub = g["sub"]
        if not sub:
            sm = re.match(r"^\((?P<s>[\w.\-]+)\)\s+(?P<m>.*)$", msg)
            if sm:
                sub, msg = sm.group("s"), sm.group("m")
        low = msg.lower()
        level = ("error" if any(w in low for w in ("error", "fail", "crash"))
                 else "warn" if "warn" in low else "info")
        # normalize an hour-only offset (-07) so the core parser accepts it
        ts = re.sub(r"([+-]\d{2})$", r"\g<1>00", g["ts"])
        fields = {"pid": int(g["pid"]), "host": g["host"]}
        if sub:
            fields["subsystem"] = sub
        return self._event(level=level, message=msg, source=g["proc"].strip(),
                           ts_ms=parse_timestamp(ts), fields=fields, raw=line)


# ── Legacy Apple .crash text report ───────────────────────────────────────────
#   Process: Safari [1234] / Exception Type: EXC_BAD_ACCESS (SIGSEGV) /
#   Thread 0 Crashed:: … / numbered frames
class AppleCrashLegacyAdapter(LogAdapter):
    name = "apple_crash_legacy"
    language = "macos"
    _KEYS = ("Process:", "Exception Type:", "Crashed Thread:", "Identifier:",
             "Version:", "Code Type:", "OS Version:", "Exception Codes:")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            heads = sum(1 for x in subs
                        if any(x.strip().startswith(k) for k in self._KEYS))
            crashed = any(re.search(r"Thread \d+ Crashed", x) for x in subs)
            return (heads >= 2 and ("Process:" in str(el))) or \
                   (heads >= 1 and crashed)
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        subs = split_any(line)
        if not subs:
            return None
        fields = {}
        for x in subs:
            t = x.strip()
            m = re.match(r"^(Process|Identifier|Version|Code Type|OS Version|"
                         r"Exception Type|Exception Codes|Crashed Thread|Path)"
                         r":\s+(.*)$", t)
            if m:
                fields[m.group(1).lower().replace(" ", "_")] = m.group(2).strip()
        if not fields:
            return None
        proc = fields.get("process", "")
        pm = re.match(r"^(?P<name>.+?)\s+\[(?P<pid>\d+)\]$", proc)
        if pm:
            fields["process"] = pm.group("name")
            fields["pid"] = int(pm.group("pid"))
        exc = fields.get("exception_type", "")
        # A spindump/hang report shares this header grammar but nothing died —
        # it is a HANG diagnosis, not a crash.
        is_spin = bool(re.search(r"Data Source:\s+Stackshots|^\s*Steps:\s+\d+",
                                 str(line), re.MULTILINE)) or \
            "Stackshots" in str(line)
        if is_spin:
            fields["report_kind"] = "spindump"
            return self._event(level="warn",
                               message=f'{fields.get("process", "process")} '
                                       f'hang/spindump report',
                               source=fields.get("process") or "spindump",
                               fields=fields, raw=line)
        return self._event(level="fatal",
                           message=f'{fields.get("process", "process")} crashed'
                                   + (f" ({exc})" if exc else ""),
                           source=fields.get("process") or "crash_report",
                           fields=fields, raw=line)


# ── Registration ─────────────────────────────────────────────────────────────
# macos_asl wraps an RFC3164 header → before the core `syslog` adapter so the
# 1.0 tie goes to the ASL-specific grammar. The others are shape-unique.
register_adapter(MacosAslAdapter(), before="syslog")
for _a in (MacosLogCompactAdapter(), MacosSyslogStyleAdapter(),
           AppleCrashLegacyAdapter()):
    register_adapter(_a)


# ═════════════════════════════════════════════════════════════════════════════
#  BATCH 7 — os_signpost unified-log lines + iOS device syslog
# ═════════════════════════════════════════════════════════════════════════════


# ── os_signpost / OSLog `log stream --signpost` line ──────────────────────────
#   2026-07-20 12:00:00.123456-0700 0x1a2b Signpost 0xEDF… myapp: [com.acme.app:loading] Begin engine warmup
class OsSignpostAdapter(LogAdapter):
    name = "os_signpost"
    language = "macos"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+(?:[+-]\d{4})?)\s+"
        r"(?P<thread>0x[0-9a-fA-F]+)\s+Signpost\s+(?P<spid>\S+)\s+"
        r"(?P<proc>[\w.\-]+):\s+\[(?P<sub>[^:\]]+):(?P<catg>[^\]]+)\]\s*"
        r"(?P<phase>Begin|End|Event)?\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {"thread": g["thread"], "signpost_id": g["spid"],
                  "subsystem": g["sub"], "signpost_category": g["catg"]}
        if g["phase"]:
            fields["phase"] = g["phase"]
        msg = (f'{g["phase"]} {g["msg"]}'.strip() if g["phase"] else g["msg"])
        return self._event(level="info", category="event", message=msg,
                           source=f'{g["proc"]}.{g["sub"]}',
                           ts_ms=parse_timestamp(g["ts"]), fields=fields, raw=line)


# ── iOS device syslog (idevicesyslog / Console device stream) ─────────────────
#   Jul 20 12:00:00 alices-iphone SpringBoard(FrontBoard)[62] <Notice>: Booting SpringBoard
# The parenthesized sender LIBRARY is unique to the iOS rendering — the plain
# macOS `proc[pid] <Level>:` form (no parens) stays with macos_asl.
class IosSyslogAdapter(LogAdapter):
    name = "ios_syslog"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(?P<host>\S+)\s+"
        r"(?P<proc>[\w.\-]+)\((?P<lib>[\w.\-]+)\)\[(?P<pid>\d+)\]\s+"
        r"<(?P<lvl>Emergency|Alert|Critical|Error|Warning|Notice|Info|Debug)>:\s?"
        r"(?P<msg>.*)$")
    _LVL = {"Emergency": "fatal", "Alert": "fatal", "Critical": "fatal",
            "Error": "error", "Warning": "warn", "Notice": "info",
            "Info": "info", "Debug": "debug"}

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=self._LVL.get(g["lvl"], "info"), message=g["msg"],
                           source=g["proc"], ts_ms=parse_timestamp(g["ts"]),
                           fields={"host": g["host"], "pid": int(g["pid"]),
                                   "library": g["lib"]}, raw=line)


# ios_syslog wraps the same RFC3164 header family as macos_asl → keep it ahead
# of the generic syslog/systemd fallbacks too (macos_asl itself cannot match
# the parenthesized-library form, so there is no tie between the two).
register_adapter(IosSyslogAdapter(), before="macos_asl")
register_adapter(OsSignpostAdapter())
