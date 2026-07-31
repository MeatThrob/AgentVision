"""
OS-platform log adapters (BATCH 3)
================================================================================
Windows event/crash text renderings, Linux package managers, and Unix system
services — the formats an OS itself writes about the programs on it. (Windows
EVTX *binary* stays a source-reader concern; these adapters read the rendered
text forms that `wevtutil qe … /f:text`, WER report files, and package-manager
logs actually produce.)

Formats: windows_evtx_text, windows_wer, apt_history, dpkg_log, dnf_log,
pacman_alpm, sssd_debug; batch 4 adds windows_dns_debug, windows_dhcp_csv,
powershell_transcript, windows_app_crash, sysmon_text, windows_security_text,
windows_eventdata_xml.
"""
from __future__ import annotations

import re
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp,
                      ratio_detect, multiline_ratio_detect, block_ratio,
                      split_any, compact_ts, us_date_ts)


# ── Windows event log rendered as text (wevtutil /f:text, "Event[N]:" blocks) ─
#   Event[0]:
#     Log Name: System
#     Source: Service Control Manager
#     Level: Error
#     Message: The service terminated unexpectedly.
class WindowsEvtxTextAdapter(LogAdapter):
    name = "windows_evtx_text"
    language = "any"
    _HEADER = re.compile(r"^Event\[\d+\]:?\s*$")
    _ATTR = re.compile(r"^\s{2,}(?P<key>[A-Z][\w /()\-]*?):\s?(?P<val>.*)$")
    _LVL = {"critical": "fatal", "error": "error", "warning": "warn",
            "information": "info", "verbose": "debug"}

    def detect(self, sample_lines):
        # Never fire unless the distinctive Event[N]: header is in the sample —
        # indented "Key: Value" lines alone are far too generic.
        has_header = any(
            any(self._HEADER.match(y.strip()) for y in str(x).splitlines())
            for x in sample_lines)
        if not has_header:
            return 0.0
        def hit(el):
            subs = [x for x in str(el).splitlines() if x.strip()]
            if not subs:
                return False
            ok = sum(1 for x in subs
                     if self._HEADER.match(x.strip()) or self._ATTR.match(x))
            return (ok / len(subs)) >= 0.5
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if "\n" in s:                       # whole record block → one event
            fields = {}
            for x in s.splitlines():
                m = self._ATTR.match(x)
                if m:
                    fields[m.group("key").strip().lower().replace(" ", "_")] = \
                        m.group("val").strip()
            if not fields:
                return None
            level = self._LVL.get(str(fields.get("level", "")).lower(), "")
            return self._event(level=level,
                               message=fields.get("message", "") or fields.get("description", ""),
                               source=fields.get("source") or fields.get("provider_name")
                               or "windows.eventlog",
                               ts_ms=parse_timestamp(str(fields.get("date", ""))),
                               fields=fields, raw=line)
        if self._HEADER.match(s.strip()):
            return self._event(level="", message=s.strip(), source="windows.eventlog",
                               fields={"marker": "event_start"}, raw=line)
        m = self._ATTR.match(s)
        if m:
            key = m.group("key").strip().lower().replace(" ", "_")
            val = m.group("val").strip()
            level = self._LVL.get(val.lower(), "") if key == "level" else ""
            return self._event(level=level, message=s.strip(),
                               source="windows.eventlog", fields={key: val}, raw=line)
        return None


# ── Windows Error Reporting (WER) Report.wer file ────────────────────────────
#   Version=1 / EventType=APPCRASH / AppName=WinSCP / Sig[0].Name=… (CRLF k=v lines)
class WindowsWerAdapter(LogAdapter):
    name = "windows_wer"
    language = "any"
    _KV = re.compile(r"^(?P<key>[\w\[\].]+)=(?P<val>.*)$")
    _ANCHOR = ("EventType=", "AppName=", "AppPath=", "ReportType=", "NsAppName=")

    def detect(self, sample_lines):
        def hit(el):
            s = str(el)
            subs = [x for x in s.splitlines() if x.strip()]
            if not subs:
                return False
            if not any(x.strip().startswith(self._ANCHOR) for x in subs):
                return False
            kv = sum(1 for x in subs if self._KV.match(x.strip()))
            return (kv / len(subs)) >= 0.7
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if "\n" in s:                       # whole report → one crash event
            fields = {}
            for x in s.splitlines():
                m = self._KV.match(x.strip())
                if m:
                    fields[m.group("key")] = m.group("val")
            if not any(k.startswith(("EventType", "AppName", "AppPath")) for k in fields):
                return None
            etype = fields.get("EventType", "WER report")
            app = fields.get("AppName") or fields.get("NsAppName") or ""
            crash = etype.upper() in ("APPCRASH", "APPHANG", "BEX", "BEX64",
                                      "CLR20R3", "MOAPPCRASH")
            return self._event(level="error" if crash else "info", category="error" if crash else "",
                               message=f"WER {etype}: {app}".strip(),
                               source=app or "wer",
                               fields=fields, raw=line)
        m = self._KV.match(s.strip())
        if m:
            key, val = m.group("key"), m.group("val")
            level = "error" if (key == "EventType" and val.upper() in
                                ("APPCRASH", "APPHANG", "BEX", "BEX64")) else ""
            return self._event(level=level, message=s.strip(), source="wer",
                               fields={key: val}, raw=line)
        return None


# ── apt history.log ──────────────────────────────────────────────────────────
#   Start-Date: 2025-11-15  14:32:14
#   Commandline: apt install vim
#   Install: vim:amd64 (2:9.0.1144-1ubuntu1), …
class AptHistoryAdapter(LogAdapter):
    name = "apt_history"
    language = "any"
    _RE = re.compile(r"^(?P<key>Start-Date|End-Date|Commandline|Requested-By|"
                     r"Install|Upgrade|Remove|Purge|Downgrade|Reinstall|Error|Comment)"
                     r":\s+(?P<val>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())), threshold=0.6)

    @staticmethod
    def _ts(text: str) -> Optional[float]:
        # apt writes a DOUBLE space between date and time
        return parse_timestamp(re.sub(r"\s+", " ", text or "").strip())

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if "\n" in s:                       # whole transaction block → one event
            fields = {}
            for x in s.splitlines():
                m = self._RE.match(x.strip())
                if m:
                    fields[m.group("key").lower().replace("-", "_")] = m.group("val")
            if not fields:
                return None
            level = "error" if "error" in fields else "info"
            return self._event(level=level,
                               message=fields.get("commandline", "apt transaction"),
                               source="apt", ts_ms=self._ts(fields.get("start_date", "")),
                               fields=fields, raw=line)
        m = self._RE.match(s.strip())
        if not m:
            return None
        key, val = m.group("key"), m.group("val")
        ts_ms = self._ts(val) if key in ("Start-Date", "End-Date") else None
        return self._event(level="error" if key == "Error" else "info",
                           message=s.strip(), source="apt",
                           ts_ms=ts_ms, fields={key.lower().replace("-", "_"): val},
                           raw=line)


# ── dpkg.log ─────────────────────────────────────────────────────────────────
#   2025-11-15 14:32:15 install vim:amd64 <none> 2:9.0.1144-1ubuntu1
#   2025-11-15 14:32:16 status installed vim:amd64 2:9.0.1144-1ubuntu1
class DpkgAdapter(LogAdapter):
    name = "dpkg_log"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
        r"(?P<action>startup|status|configure|install|upgrade|trigproc|remove|purge|"
        r"update-alternatives|conffile)\s+(?P<rest>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {"action": g["action"]}
        parts = g["rest"].split()
        if g["action"] == "status" and len(parts) >= 2:
            fields.update({"state": parts[0], "package": parts[1],
                           "version": parts[2] if len(parts) > 2 else None})
        elif g["action"] in ("install", "upgrade", "configure", "remove",
                             "purge", "trigproc") and parts:
            fields["package"] = parts[0]
            if len(parts) > 2:
                fields["version"] = parts[2] if g["action"] == "install" else parts[1]
        level = "error" if "half-" in g["rest"] else "info"
        return self._event(level=level, message=f'{g["action"]} {g["rest"]}',
                           source="dpkg", ts_ms=parse_timestamp(g["ts"]),
                           fields=fields, raw=line)


# ── dnf.log ──────────────────────────────────────────────────────────────────
#   2025-06-11T20:19:50+0000 DDEBUG timer: config: 12 ms
#   2025-06-11T20:19:52+0000 INFO --- logging initialized ---
class DnfAdapter(LogAdapter):
    name = "dnf_log"
    language = "python"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?[+-]\d{4})\s+"
        r"(?P<lvl>SUBDEBUG|DDEBUG|DEBUG|TRACE|INFO|WARNING|ERROR|CRITICAL)\s+(?P<msg>.*)$")
    _LVL = {"SUBDEBUG": "trace", "DDEBUG": "debug"}

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=self._LVL.get(g["lvl"], g["lvl"]), message=g["msg"],
                           source="dnf", ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── pacman / ALPM log ────────────────────────────────────────────────────────
#   [2020-11-25T15:16:27+0530] [ALPM] installed alacritty (0.5.0-3)
class PacmanAlpmAdapter(LogAdapter):
    name = "pacman_alpm"
    language = "any"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})\]\s+"
        r"\[(?P<src>ALPM|PACMAN|ALPM-SCRIPTLET)\]\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"]
        low = msg.lower()
        level = ("error" if low.startswith("error") else
                 "warn" if low.startswith("warning") else "info")
        fields = {}
        vm = re.match(r"(installed|upgraded|removed|reinstalled|downgraded)\s+(\S+)\s*(\(.*\))?",
                      msg)
        if vm:
            fields = {"action": vm.group(1), "package": vm.group(2),
                      "version": (vm.group(3) or "").strip("()") or None}
        return self._event(level=level, message=msg, source=g["src"].lower(),
                           ts_ms=parse_timestamp(g["ts"]), fields=fields or None,
                           raw=line)


# ── SSSD debug log ───────────────────────────────────────────────────────────
#   (Tue Nov 20 12:18:56 2020) [sssd[be[ldap.vm]]] [be_resolve_server_process] (0x1000): …
class SssdAdapter(LogAdapter):
    name = "sssd_debug"
    language = "any"
    # svc may nest brackets ("sssd[be[ldap.vm]]") → non-greedy up to "] ["
    _RE = re.compile(
        r"^\((?P<ts>[A-Z][a-z]{2} [A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2}(?::\d+)? \d{4})\)\s+"
        r"\[(?P<svc>sssd.*?)\]\s+\[(?P<func>[\w]+)\]\s+"
        r"\((?P<dbg>0x[0-9a-fA-F]+)\):\s*(?P<msg>.*)$")
    # SSSD debug-level bits → severity
    _BITS = ((0x0010, "fatal"), (0x0020, "error"), (0x0040, "warn"),
             (0x0080, "info"), (0x0100, "info"), (0x0200, "info"))

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        dbg = int(g["dbg"], 16)
        level = next((lvl for bit, lvl in self._BITS if dbg <= bit), "debug")
        return self._event(level=level, message=g["msg"], source=g["svc"],
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"function": g["func"], "debug_level": g["dbg"]},
                           raw=line)


# ── Windows DNS Server debug log (dns.log packet capture) — BATCH 4 ──────────
#   4/11/2026 7:52:03 AM 06B0 PACKET 00000000028657F0 UDP Snd 10.2.0.1 6590 R Q
#   [8081   DR  NOERROR] A       (7)example(3)com(0)
class WindowsDnsDebugAdapter(LogAdapter):
    name = "windows_dns_debug"
    language = "windows"
    _RE = re.compile(
        r"^(?P<date>\d{1,2}/\d{1,2}/\d{4}|\d{8})\s+"
        r"(?P<time>\d{1,2}:\d{2}:\d{2}(?:\s?[AP]M)?)\s+"
        r"(?P<tid>[0-9A-F]{3,5})\s+(?P<ctx>PACKET|EVENT)\s+"
        r"(?P<ptr>[0-9A-Fa-f]{8,16})\s+(?P<proto>UDP|TCP)\s+"
        r"(?P<dir>Snd|Rcv)\s+(?P<ip>\S+)\s+(?P<xid>[0-9a-fA-F]{1,4})\s+"
        r"(?P<resp>R)?\s+(?P<op>[A-Z?])\s+"
        r"\[(?P<flags>[0-9A-F]+)\s+(?P<fchars>[A-Z ]*?)\s*(?P<rcode>[A-Z]+)\]\s+"
        r"(?P<qtype>\S+)\s+(?P<qname>\S+)\s*$")
    _LABEL = re.compile(r"\((\d+)\)")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        # decode "(7)example(3)com(0)" → "example.com"
        qname = self._LABEL.sub(".", g["qname"]).strip(".")
        level = "info" if g["rcode"] == "NOERROR" else "warn"
        return self._event(level=level,
                           message=f"DNS {g['dir']} {g['qtype']} {qname} "
                                   f"→ {g['rcode']}",
                           source="windows.dns",
                           ts_ms=us_date_ts(g["date"], g["time"])
                           if "/" in g["date"] else None,
                           fields={"protocol": g["proto"], "direction": g["dir"],
                                   "remote_ip": g["ip"], "xid": g["xid"],
                                   "is_response": bool(g["resp"]),
                                   "rcode": g["rcode"], "qtype": g["qtype"],
                                   "qname": qname}, raw=line)


# ── Windows DHCP Server audit log (DhcpSrvLog-<Day>.log CSV) — BATCH 4 ────────
#   10,07/22/06,22:20:25,Assign,147.100.100.120,e2k7.,0013D30C227E,
class WindowsDhcpCsvAdapter(LogAdapter):
    name = "windows_dhcp_csv"
    language = "windows"
    _ROW = re.compile(
        r"^(?P<id>\d{1,3}),(?P<date>\d{2}/\d{2}/\d{2,4}),(?P<time>\d{2}:\d{2}:\d{2}),"
        r"(?P<desc>[^,]*),(?P<ip>[^,]*),(?P<host>[^,]*),(?P<mac>[^,]*)")
    _HDR = re.compile(r"^ID,Date,Time,Description,IP Address,Host Name,MAC Address",
                      re.IGNORECASE)
    _PREAMBLE = "Microsoft DHCP Service Activity Log"
    # event-id → severity: conflicts / scope-full / denials are warnings
    _WARN_IDS = {13, 14, 15, 22, 23, 34, 36}

    def detect(self, sample_lines):
        def hit(x):
            s = x.strip()
            return bool(self._ROW.match(s) or self._HDR.match(s)
                        or self._PREAMBLE in s)
        return ratio_detect(sample_lines, lambda ln: block_ratio(ln, hit))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if self._HDR.match(s) or self._PREAMBLE in s:
            return self._event(level="", message=s, source="windows.dhcp",
                               fields={"header": True}, raw=line)
        m = self._ROW.match(s)
        if not m:
            return None
        g = m.groupdict()
        eid = int(g["id"])
        level = "warn" if eid in self._WARN_IDS else "info"
        return self._event(level=level,
                           message=f"DHCP {g['desc'] or eid}: {g['ip']}"
                                   f" {g['host']}".strip(),
                           source="windows.dhcp",
                           ts_ms=us_date_ts(g["date"], g["time"]),
                           fields={"event_id": eid, "description": g["desc"],
                                   "ip": g["ip"], "host": g["host"],
                                   "mac": g["mac"]}, raw=line)


# ── PowerShell Start-Transcript file — BATCH 4 ────────────────────────────────
#   ********************** / Windows PowerShell transcript start / Start time: …
class PowershellTranscriptAdapter(LogAdapter):
    name = "powershell_transcript"
    language = "windows"
    _BANNER = re.compile(r"^\*{10,}$")
    _START = re.compile(r"^Windows PowerShell transcript (start|end)$")
    _KV = re.compile(r"^(?P<k>Start time|End time|Username|RunAs User|Machine|"
                     r"Host Application|Process ID|PSVersion|PSEdition|"
                     r"Configuration Name|OS|Platform|CLRVersion|"
                     r"WSManStackVersion|PSRemotingProtocolVersion|"
                     r"SerializationVersion|BuildVersion):\s*(?P<v>.*)$")
    _PROMPT = re.compile(r"^PS [A-Za-z]:\\.*>")

    def detect(self, sample_lines):
        def block_hit(el):
            pieces = split_any(el)
            if not pieces:
                return False
            if any(self._START.match(x.strip()) for x in pieces):
                return True
            kv = sum(1 for x in pieces if self._KV.match(x.strip()))
            return kv >= 2 and any(self._BANNER.match(x.strip()) for x in pieces)
        return ratio_detect(sample_lines, block_hit)

    def parse_line(self, line: str) -> Optional[dict]:
        pieces = split_any(line)
        if not pieces:
            return None
        fields = {}
        commands = []
        for x in pieces:
            m = self._KV.match(x.strip())
            if m:
                fields[m.group("k").lower().replace(" ", "_")] = m.group("v").strip()
            elif self._PROMPT.match(x.strip()):
                commands.append(x.strip())
        if not fields and not commands \
                and not any(self._START.match(x.strip()) for x in pieces):
            return None
        if commands:
            fields["commands"] = commands
        ts_ms = compact_ts(fields.get("start_time", ""))
        who = fields.get("username", "")
        return self._event(level="info",
                           message=f"PowerShell transcript ({who})".strip(),
                           source="powershell.transcript", ts_ms=ts_ms,
                           fields=fields or None, raw=line)


# ── Windows Application Error EventID 1000 rendered description — BATCH 4 ─────
#   Faulting application name: dwm.exe, version: …\r\nException code: 0xc00001ad
class WindowsAppCrashAdapter(LogAdapter):
    name = "windows_app_crash"
    language = "windows"
    _HEAD = re.compile(r"^Faulting application name:\s*(?P<app>[^,]+)")
    _PAIR = re.compile(r"(?P<k>[A-Z][\w ]*?):\s*(?P<v>[^,]+?)(?:,|$)")

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda ln: bool(split_any(ln)
                            and self._HEAD.match(split_any(ln)[0].strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        pieces = split_any(line)
        if not pieces or not self._HEAD.match(pieces[0].strip()):
            return None
        fields = {}
        for x in pieces:
            for m in self._PAIR.finditer(x.strip()):
                fields[m.group("k").strip().lower().replace(" ", "_")] = \
                    m.group("v").strip()
        app = fields.get("faulting_application_name", "?")
        mod = fields.get("faulting_module_name", "?")
        exc = fields.get("exception_code", "?")
        return self._event(level="error", category="error",
                           message=f"{app} crashed in {mod} ({exc})",
                           source=app, trace_id=fields.get("report_id"),
                           fields=fields, raw=line)


# ── Sysmon operational log rendered as text — BATCH 4 ────────────────────────
#   Process Create:\nRuleName: -\nUtcTime: 2026-01-24 00:02:08.800\nImage: …
class SysmonTextAdapter(LogAdapter):
    name = "sysmon_text"
    language = "windows"
    _TITLE = re.compile(
        r"^(Process Create|Process terminated|Process Tampering|"
        r"Network connection detected|Dns query|DNS query|Driver loaded|"
        r"Image loaded|CreateRemoteThread detected|RawAccessRead detected|"
        r"Process accessed|File created|File Delete(?: logged| archived)?|"
        r"Registry object added or deleted|Registry value set|"
        r"Registry key and value renamed|File stream created|"
        r"Named pipe (?:created|connected)|Clipboard changed|File Block\w*|"
        r"Wmi\w* activity detected|Sysmon (?:config|service) state changed):\s*$")
    _KV = re.compile(r"^(?P<k>[A-Z][\w ]*):\s?(?P<v>.*)$")
    _ANCHORS = ("UtcTime", "ProcessGuid", "RuleName", "ProcessId", "Image")

    def detect(self, sample_lines):
        def block_hit(el):
            pieces = split_any(el)
            if not pieces or not self._TITLE.match(pieces[0].strip()):
                return False
            return any(x.strip().split(":", 1)[0] in self._ANCHORS
                       for x in pieces[1:] if ":" in x)
        return ratio_detect(sample_lines, block_hit)

    def parse_line(self, line: str) -> Optional[dict]:
        pieces = split_any(line)
        if not pieces:
            return None
        tm = self._TITLE.match(pieces[0].strip())
        if not tm:
            return None
        fields = {}
        for x in pieces[1:]:
            m = self._KV.match(x.strip())
            if m:
                fields[m.group("k")] = m.group("v").strip()
        title = tm.group(1)
        image = fields.get("Image", "")
        return self._event(level="info",
                           message=f"Sysmon: {title}"
                                   + (f" — {image}" if image else ""),
                           source="sysmon",
                           ts_ms=parse_timestamp(fields.get("UtcTime", "")),
                           trace_id=fields.get("ProcessGuid", "").strip("{}") or None,
                           fields=fields, raw=line)


# ── Windows Security event rendered Message text — BATCH 4 ────────────────────
#   An account failed to log on.\n\nSubject:\n\tSecurity ID:\tS-1-5-18 …
class WindowsSecurityTextAdapter(LogAdapter):
    name = "windows_security_text"
    language = "windows"
    _TITLE = re.compile(
        r"^(An account (?:was successfully logged on|failed to log on|"
        r"was logged off|was locked out)|A logon was attempted\b.*|"
        r"Special privileges assigned to new logon|"
        r"A new process has been created|A process has exited|"
        r"A user account was (?:created|changed|enabled|disabled|deleted)|"
        r"A privileged service was called|An attempt was made to\b.*|"
        r"The audit log was cleared|A Kerberos (?:authentication ticket|"
        r"service ticket) (?:\(TGT\) )?was requested|"
        r"The computer attempted to validate the credentials\b.*)\.$")
    _FIELD = re.compile(r"^\t*(?P<k>[A-Z][\w /()\-]*):\t+(?P<v>.*)$")
    _SECTION = re.compile(r"^(?P<k>[A-Z][\w /()\-]*):\s*$")

    def detect(self, sample_lines):
        def block_hit(el):
            pieces = split_any(el)
            if not pieces or not self._TITLE.match(pieces[0].strip()):
                return False
            return sum(1 for x in pieces[1:] if self._FIELD.match(x)) >= 2
        return ratio_detect(sample_lines, block_hit)

    def parse_line(self, line: str) -> Optional[dict]:
        pieces = split_any(line)
        if not pieces:
            return None
        tm = self._TITLE.match(pieces[0].strip())
        if not tm:
            return None
        title = tm.group(0)
        fields = {}
        section = ""
        for x in pieces[1:]:
            fm = self._FIELD.match(x)
            if fm:
                key = fm.group("k").strip().lower().replace(" ", "_")
                if section:
                    key = f"{section}.{key}"
                fields[key] = fm.group("v").strip()
                continue
            sm = self._SECTION.match(x.strip())
            if sm and "\t" not in x.strip():
                section = sm.group("k").strip().lower().replace(" ", "_")
        low = title.lower()
        level = ("warn" if ("failed" in low or "locked out" in low
                            or "cleared" in low) else "info")
        return self._event(level=level, message=title,
                           source="windows.security", fields=fields or None,
                           raw=line)


# ── Bare Windows <EventData> XML fragments (Sysmon exports, SIEM feeds) ───────
#   <EventData><Data Name="QueryName">login.malicious.example</Data>…</EventData>
class WindowsEventDataXmlAdapter(LogAdapter):
    name = "windows_eventdata_xml"
    language = "windows"
    _DATA = re.compile(r"<Data Name=['\"](?P<k>[^'\"]+)['\"]\s*>(?P<v>[^<]*)</Data>")

    def detect(self, sample_lines):
        def hit(ln):
            s = str(ln).strip()
            return s.startswith("<EventData") and bool(self._DATA.search(s))
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if not (s.startswith("<EventData") and self._DATA.search(s)):
            return None
        fields = {m.group("k"): m.group("v") for m in self._DATA.finditer(s)}
        # a human headline from the highest-signal fields present
        headline = (fields.get("QueryName") or fields.get("DestinationIp")
                    or fields.get("TargetImage") or fields.get("TargetFilename")
                    or fields.get("Image") or "event")
        return self._event(level="info", message=f"EventData: {headline}",
                           source=fields.get("Image", "windows.eventdata"),
                           ts_ms=parse_timestamp(fields.get("UtcTime", "")),
                           trace_id=(fields.get("ProcessGuid") or "").strip("{}") or None,
                           fields=fields, raw=line)


# ── Registration ─────────────────────────────────────────────────────────────
for _a in (WindowsEvtxTextAdapter(), WindowsWerAdapter(), AptHistoryAdapter(),
           DpkgAdapter(), DnfAdapter(), PacmanAlpmAdapter(), SssdAdapter(),
           # batch 4 — Windows server/crash/security text renderings
           WindowsDnsDebugAdapter(), WindowsDhcpCsvAdapter(),
           PowershellTranscriptAdapter(), WindowsAppCrashAdapter(),
           SysmonTextAdapter(), WindowsSecurityTextAdapter(),
           WindowsEventDataXmlAdapter()):
    register_adapter(_a)


# ═════════════════════════════════════════════════════════════════════════════
# BATCH 6
# ═════════════════════════════════════════════════════════════════════════════
from ._common import split_any as _split_any  # noqa: E402


# ── journald export format (`journalctl -o export`) ───────────────────────────
#   __CURSOR=s=abc;i=1f4;b=…  /  __REALTIME_TIMESTAMP=1753020191123456  /  MESSAGE=…
#   Records are KEY=value lines separated by a blank line.
class JournaldExportAdapter(LogAdapter):
    name = "journald_export"
    language = "linux"
    _FIELD = re.compile(r"^(?P<k>__?[A-Z][A-Z0-9_]*|[A-Z][A-Z0-9_]*)=(?P<v>.*)$")
    _ANCHOR = ("__CURSOR=", "__REALTIME_TIMESTAMP=", "_SYSTEMD_UNIT=",
               "SYSLOG_IDENTIFIER=")
    _PRI_LVL = {0: "fatal", 1: "fatal", 2: "fatal", 3: "error",
                4: "warn", 5: "info", 6: "info", 7: "debug"}

    def detect(self, sample_lines):
        def hit(el):
            subs = _split_any(el)
            if not subs:
                return False
            fields = sum(1 for x in subs if self._FIELD.match(x.strip()))
            anchored = any(x.strip().startswith(self._ANCHOR) for x in subs)
            return anchored and fields / len(subs) >= 0.8
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        subs = _split_any(line)
        rec = {}
        for x in subs:
            m = self._FIELD.match(x.strip())
            if m:
                rec[m.group("k")] = m.group("v")
        if not rec:
            return None
        ts_ms = None
        rt = rec.get("__REALTIME_TIMESTAMP")
        if rt and rt.isdigit():
            ts_ms = int(rt) / 1000.0                      # µs → ms
        level = ""
        pri = rec.get("PRIORITY")
        if pri and pri.isdigit():
            level = self._PRI_LVL.get(int(pri), "")
        fields = {k: v for k, v in rec.items()
                  if k in ("_PID", "_UID", "_SYSTEMD_UNIT", "SYSLOG_IDENTIFIER",
                           "_HOSTNAME", "_COMM", "PRIORITY")}
        return self._event(level=level, message=rec.get("MESSAGE", ""),
                           source=rec.get("SYSLOG_IDENTIFIER")
                                  or rec.get("_SYSTEMD_UNIT") or "journald",
                           ts_ms=ts_ms, fields=fields or None, raw=line)


# ── APT term.log (/var/log/apt/term.log) ─────────────────────────────────────
#   Log started: 2025-11-15  14:32:14   +  dpkg terminal chatter
class AptTermAdapter(LogAdapter):
    name = "apt_term"
    language = "linux"
    # anchors are dpkg/apt-specific; weak marks (Removing/Purging/Selecting)
    # appear in other tools' output too and only count alongside an anchor.
    _ANCHOR = re.compile(
        r"^(Log (?:started|ended): |Preparing to unpack |Unpacking |Setting up |"
        r"Processing triggers for |\(Reading database \.\.\.|"
        r"Errors were encountered)")
    _WEAK = re.compile(
        r"^(Selecting previously unselected package |Removing |"
        r"Purging configuration files for )")
    _MARK = re.compile(
        r"^(Log (?:started|ended): |Preparing to unpack |Unpacking |Setting up |"
        r"Processing triggers for |\(Reading database \.\.\.|"
        r"Errors were encountered|Selecting previously unselected package |"
        r"Removing |Purging configuration files for )")

    def detect(self, sample_lines):
        def hit(el):
            subs = _split_any(el)
            if not subs:
                return False
            if not any(self._ANCHOR.match(x.strip()) for x in subs):
                return False
            n = sum(1 for x in subs if self._MARK.match(x.strip()))
            return n >= 1 and n / len(subs) >= 0.5
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if "\n" in s:
            for x in s.splitlines():
                if x.strip() and self._MARK.match(x.strip()):
                    ev = self.parse_line(x)
                    if ev:
                        ev["raw"] = line
                        return ev
            s = s.splitlines()[0]
        t = s.strip()
        if not t:
            return None
        level = "error" if t.startswith("Errors were encountered") else "info"
        fields = None
        pm = re.match(r"^(?:Unpacking|Setting up|Removing|Preparing to unpack \.\.\./)"
                      r"\s*(?P<pkg>[\w.+\-]+)(?:\s+\((?P<ver>[^)]+)\))?", t)
        if pm:
            fields = {"package": pm.group("pkg")}
            if pm.group("ver"):
                fields["version"] = pm.group("ver")
        lm = re.match(r"^Log (started|ended): (.+)$", t)
        ts_ms = parse_timestamp(re.sub(r"\s{2,}", " ", lm.group(2))) if lm else None
        return self._event(level=level, message=t, source="apt.term",
                           ts_ms=ts_ms, fields=fields, raw=line)


for _a in (JournaldExportAdapter(), AptTermAdapter()):
    register_adapter(_a)


# ═════════════════════════════════════════════════════════════════════════════
#  BATCH 7 — rendered Windows event bodies (classic PS engine, .NET crash,
#  Application Hang, PowerShell script-block logging)
# ═════════════════════════════════════════════════════════════════════════════


# ── Windows PowerShell classic engine events (400/403/500/600/800) ────────────
#   Provider LifeCycle Notification\r\nProviderName=Registry\r\n…HostName=ConsoleHost…
class PowershellClassicEngineAdapter(LogAdapter):
    name = "powershell_classic_engine"
    language = "any"
    _KEYS = ("HostName", "HostVersion", "HostId", "EngineVersion", "RunspaceId",
             "PipelineId", "NewEngineState", "PreviousEngineState",
             "ProviderName", "NewProviderState", "SequenceNumber",
             "CommandName", "CommandType", "ScriptName", "CommandPath",
             "CommandLine", "HostApplication", "UserId")
    _KV = re.compile(r"^(?P<k>[A-Za-z]\w+)=(?P<v>.*)$")

    def _pairs(self, el) -> dict:
        out = {}
        for x in split_any(el):
            m = self._KV.match(x.strip())
            if m and m.group("k") in self._KEYS and m.group("k") not in out:
                out[m.group("k")] = m.group("v").strip()
        return out

    def detect(self, sample_lines):
        def ok(el):
            p = self._pairs(el)
            return len(p) >= 3 and ("HostName" in p or "EngineVersion" in p)
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        p = self._pairs(line)
        if len(p) < 3 or not ("HostName" in p or "EngineVersion" in p):
            return None
        subs = split_any(line)
        title = subs[0].strip() if subs and not self._KV.match(subs[0].strip()) else \
            (f'Engine state {p.get("PreviousEngineState", "?")} → '
             f'{p.get("NewEngineState", "?")}' if "NewEngineState" in p
             else "PowerShell engine event")
        return self._event(level="info", message=title,
                           source="windows.powershell",
                           fields={k: v for k, v in p.items() if v}, raw=line)


# ── .NET Runtime unhandled exception (Application EventID 1026) ───────────────
#   Application: MyApp.exe … Exception Info: System.NullReferenceException: … at …
class WindowsDotnetCrashAdapter(LogAdapter):
    name = "windows_dotnet_crash"
    language = "dotnet"
    _EXC = re.compile(r"Exception Info:\s*(?P<exc>[\w.`+]+(?:Exception|Error)\b[^\r\n]*)")
    _APP = re.compile(r"^Application:\s*(?P<app>.+?)\s*$", re.MULTILINE)
    _AT = re.compile(r"^\s+at\s+\S+", re.MULTILINE)

    def _norm(self, el) -> str:
        return "\n".join(split_any(el))

    def detect(self, sample_lines):
        def ok(el):
            s = self._norm(el)
            return bool(self._EXC.search(s)
                        and (self._APP.search(s) or ".NET Version" in s
                             or "CoreCLR Version" in s))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = self._norm(line)
        em = self._EXC.search(s)
        if not (em and (self._APP.search(s) or ".NET Version" in s
                        or "CoreCLR Version" in s)):
            return None
        am = self._APP.search(s)
        frames = self._AT.findall(s)
        fields = {"application": am.group("app") if am else None,
                  "stack_frames": len(frames)}
        vm = re.search(r"^\.NET Version:\s*(.+?)\s*$", s, re.MULTILINE)
        if vm:
            fields["dotnet_version"] = vm.group(1)
        return self._event(level="fatal", message=em.group("exc").strip(),
                           source=fields.get("application") or ".NET Runtime",
                           category="crash", fields=fields, raw=line)


# ── Application Hang (Application EventID 1002) ───────────────────────────────
#   The program Explorer.EXE version … stopped interacting with Windows and was closed.
class WindowsAppHangAdapter(LogAdapter):
    name = "windows_app_hang"
    language = "any"
    _HEAD = re.compile(
        r"^The program (?P<prog>\S+)(?: version (?P<ver>\S+))? stopped interacting "
        r"with Windows and was closed\.")
    _KV = re.compile(r"^(?P<k>[A-Za-z][\w ]+):\s*(?P<v>.*)$")

    def detect(self, sample_lines):
        def ok(el):
            subs = split_any(el)
            return bool(subs) and bool(self._HEAD.match(subs[0].strip()))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        subs = split_any(line)
        if not subs:
            return None
        m = self._HEAD.match(subs[0].strip())
        if not m:
            return None
        fields = {"program": m.group("prog"), "version": m.group("ver")}
        for x in subs[1:]:
            km = self._KV.match(x.strip())
            if km:
                fields[km.group("k").strip().lower().replace(" ", "_")] = km.group("v")
        return self._event(level="error", message=subs[0].strip(),
                           source=m.group("prog"), category="crash",
                           fields=fields, raw=line)


# ── PowerShell Script Block Logging (Operational EventID 4104) ────────────────
#   Creating Scriptblock text (1 of 1):\n<script>\nScriptBlock ID: …\nPath: …
class PowershellScriptblockAdapter(LogAdapter):
    name = "powershell_scriptblock"
    language = "any"
    _HEAD = re.compile(r"^Creating Scriptblock text \((?P<n>\d+) of (?P<m>\d+)\):")
    _ID = re.compile(r"^ScriptBlock ID:\s*(?P<id>[0-9a-fA-F\-]+)", re.MULTILINE)
    _PATH = re.compile(r"^Path:\s*(?P<p>.*)$", re.MULTILINE)

    def detect(self, sample_lines):
        def ok(el):
            subs = split_any(el)
            return bool(subs) and bool(self._HEAD.match(subs[0].strip()))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        subs = split_any(line)
        if not subs:
            return None
        hm = self._HEAD.match(subs[0].strip())
        if not hm:
            return None
        s = "\n".join(subs)
        body = [x for x in subs[1:]
                if not x.strip().startswith(("ScriptBlock ID:", "Path:"))]
        fields = {"part": int(hm.group("n")), "parts": int(hm.group("m")),
                  "script": "\n".join(body)[:1000] or None}
        im = self._ID.search(s)
        if im:
            fields["scriptblock_id"] = im.group("id")
        pm = self._PATH.search(s)
        if pm and pm.group("p").strip():
            fields["path"] = pm.group("p").strip()
        snippet = body[0].strip()[:120] if body else "(empty)"
        return self._event(level="info", message=f"Script block: {snippet}",
                           source="windows.powershell.scriptblock",
                           fields=fields, raw=line)


for _a in (PowershellClassicEngineAdapter(), WindowsDotnetCrashAdapter(),
           WindowsAppHangAdapter(), PowershellScriptblockAdapter()):
    register_adapter(_a)


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH 8 — Windows service/VPN logs + classic Unix transfer/su logs
# ══════════════════════════════════════════════════════════════════════════════
from ._common import (RxAdapter, vocab_detect, block_ratio,  # noqa: E402
                      ratio_detect, split_any, us_date_ts)


# ── Classic Unix FTP transfer log (wu-ftpd/proftpd/vsftpd xferlog) ────────────
#   Mon Jul 20 14:02:15 2026 1 192.168.1.5 4096 /pub/file.tgz b _ o r alice ftp 0 * c
class FtpXferlogAdapter(RxAdapter):
    name = "ftp_xferlog"
    language = "any"
    default_source = "xferlog"
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2} [A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2} \d{4})\s+"
        r"(?P<xfer>\d+)\s+(?P<host>\S+)\s+(?P<bytes>\d+)\s+(?P<file>\S+)\s+"
        r"(?P<type>[ab])\s+(?P<action>[CUT_])\s+(?P<dir>[oid])\s+(?P<mode>[agr])\s+"
        r"(?P<user>\S+)\s+(?P<svc>\S+)\s+(?P<auth>\d+)\s+(?P<authid>\S+)\s+(?P<done>[ci])")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return "info" if g["done"] == "c" else "warn"

    def _fields(self, g, line):
        return {"remote_host": g["host"], "bytes": int(g["bytes"]),
                "file": g["file"], "direction": g["dir"], "user": g["user"],
                "completed": g["done"] == "c"}

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            m = self._RE.match(line.strip())
            ev["data"]["message"] = f'{"↑" if m.group("dir")=="o" else "↓"} {m.group("file")} ({m.group("bytes")}b)'
            ev["source"] = "xferlog"
            ev["category"] = "event"
        return ev


# ── Windows Netlogon debug log (%windir%\debug\netlogon.log) ──────────────────
#   01/03 10:08:39 [LOGON] [2616] CONTOZO: SamLogon: … Returns 0xC000006A
class NetlogonDebugAdapter(RxAdapter):
    name = "netlogon_debug"
    language = "windows"
    default_source = "netlogon"
    _RE = re.compile(
        r"^(?P<mon>\d{2})/(?P<dy>\d{2}) (?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})\s+"
        r"\[(?P<flag>[A-Z_]+)\]\s+\[(?P<tid>\d+)\]\s+(?P<msg>.*)$")

    def _ts(self, g):
        from datetime import datetime
        now = datetime.now()
        from ._common import mk_ts
        return mk_ts(now.year, g["mon"], g["dy"], g["hh"], g["mi"], g["ss"])

    def _level(self, g, line):
        msg = g.get("msg", "")
        if g["flag"] == "CRITICAL" or re.search(r"0xC[0-9A-Fa-f]{7}|failed|error", msg):
            return "error"
        return "info"

    def _fields(self, g, line):
        return {"flag": g["flag"], "tid": int(g["tid"])}


# ── Windows built-in VPN (RasClient/RasMan) events ────────────────────────────
#   The user CORP\alice dialed a connection named Work VPN which has failed. …
class WindowsRasclientAdapter(LogAdapter):
    name = "windows_rasclient"
    language = "windows"
    _RE = re.compile(
        r"dialed a connection named (?P<conn>.+?) which has (?P<result>failed|connected)",
        re.I)
    _ERR = re.compile(r"error code returned on failure is (?P<code>\d+)", re.I)

    def detect(self, sample_lines):
        return vocab_detect(
            sample_lines,
            lambda el: any(self._RE.search(x) for x in split_any(el)), cap=0.85)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        m = self._RE.search(s)
        if not m:
            return None
        g = m.groupdict()
        fields = {"connection": g["conn"].strip(), "result": g["result"]}
        em = self._ERR.search(s)
        if em:
            fields["error_code"] = int(em.group("code"))
        return self._event(level="error" if g["result"] == "failed" else "info",
                           message=s.strip(), source="windows.rasclient",
                           fields=fields, category="event", raw=line)


# ── Windows Server DHCPv6 audit CSV (DhcpV6SrvLog) ────────────────────────────
#   11000,12/06/23,14:19:19,DHCPV6 Solicit,2001:db8:…,testclient.lein.io,,14,…
class WindowsDhcpv6CsvAdapter(RxAdapter):
    name = "windows_dhcpv6_csv"
    language = "windows"
    default_source = "windows.dhcpv6"
    _RE = re.compile(
        r"^(?P<eid>11\d{3}),(?P<date>\d{2}/\d{2}/\d{2}),(?P<time>\d{2}:\d{2}:\d{2}),"
        r"(?P<desc>DHCPV6[^,]*),(?P<addr>[0-9A-Fa-f:]*),(?P<host>[^,]*),")

    def _ts(self, g):
        return us_date_ts(g["date"], g["time"])

    def _level(self, g, line):
        return "warn" if re.search(r"Decline|Nak|Conflict|Expired", g.get("desc", ""), re.I) else "info"

    def _fields(self, g, line):
        return {"event_id": int(g["eid"]), "description": g["desc"].strip(),
                "address": g["addr"], "hostname": g["host"]}

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            m = self._RE.match(line.strip())
            ev["data"]["message"] = f'{m.group("desc").strip()} {m.group("addr")}'.strip()
            ev["source"] = "windows.dhcpv6"
            ev["category"] = "event"
        return ev


# ── Windows PowerShell Get-WinEvent / Get-EventLog table ──────────────────────
#   TimeCreated              Id LevelDisplayName Message
#   7/20/2026 2:03:11 PM   7034 Error            The service terminated …
class WindowsPsGetEventLogAdapter(LogAdapter):
    name = "windows_ps_geteventlog"
    language = "windows"
    _HDR = re.compile(r"^\s*(TimeCreated\b.*\bLevelDisplayName|Index\s+Time\s+EntryType\s+Source)")
    _ROW = re.compile(
        r"^(?P<date>\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}:\d{2} [AP]M)\s+"
        r"(?P<id>\d+)\s+(?P<level>Error|Warning|Information|Critical|Verbose)\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        def hit(x):
            x = x.rstrip()
            return bool(self._HDR.match(x) or self._ROW.match(x.strip()))
        return ratio_detect(sample_lines, lambda el: block_ratio(el, hit))

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        for x in split_any(s):
            m = self._ROW.match(x.strip())
            if m:
                g = m.groupdict()
                lvl = {"Error": "error", "Warning": "warn", "Critical": "fatal",
                       "Information": "info", "Verbose": "trace"}[g["level"]]
                return self._event(level=lvl, message=g["msg"].strip(),
                                   source="windows.eventlog",
                                   ts_ms=us_date_ts(*g["date"].split(" ", 1)),
                                   fields={"event_id": int(g["id"])}, raw=line)
        if any(self._HDR.match(x) for x in split_any(s)):
            return self._event(level="", message=s.strip(),
                               source="windows.eventlog",
                               fields={"header": True}, raw=line)
        return None


# ── Windows SetupAPI device-install log ───────────────────────────────────────
#   >>>  [Device Install (Hardware initiated) - USB\VID_0079]
#   >>>  Section start 2026/07/20 14:03:11.123
class WindowsSetupapiAdapter(LogAdapter):
    name = "windows_setupapi"
    language = "windows"
    _SECTION = re.compile(r"^>>>\s+\[(?P<title>.+)\]\s*$")
    _MARK = re.compile(r"^>>>\s+Section (start|end)\b")
    _CAT = re.compile(r"^\s+(?P<cat>inf|dvi|sto|cpy|pol|bak|flq|ndv|dun):\s")

    def detect(self, sample_lines):
        def hit(x):
            return bool(self._SECTION.match(x) or self._MARK.match(x)
                        or self._CAT.match(x))
        return ratio_detect(sample_lines, lambda el: block_ratio(el, hit))

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        for x in split_any(s):
            sm = self._SECTION.match(x)
            if sm:
                return self._event(level="", message=sm.group("title"),
                                   source="windows.setupapi",
                                   fields={"section": sm.group("title")},
                                   category="event", raw=line)
        tm = re.search(r"(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d{3})", s)
        return self._event(level="error" if "!!!" in s else "",
                           message=s.strip(), source="windows.setupapi",
                           ts_ms=parse_timestamp(tm.group(1)) if tm else None,
                           raw=line)


for _a in (FtpXferlogAdapter(), NetlogonDebugAdapter(), WindowsRasclientAdapter(),
           WindowsDhcpv6CsvAdapter(), WindowsPsGetEventLogAdapter(),
           WindowsSetupapiAdapter()):
    register_adapter(_a)
