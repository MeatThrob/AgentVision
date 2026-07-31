"""
Security / network-appliance / IDS log adapters
================================================================================
Firewalls, IDS/IPS, and auth logs — the high-value operational formats a SOC or
a homelab actually stares at. All pure-stdlib, all normalized to the unified
event schema (category=="error"/"warn" so the bridge's failure + bookmark logic
fires on denials, drops, and auth failures).

Formats: cef, leef, cisco_asa, cisco_ios, fortigate_kv, checkpoint,
pfsense_filterlog, paloalto_panos, suricata_fast, snort_fast, sshd_auth,
dnsmasq, isc_dhcpd, zeek_tsv, ossec_wazuh_alerts, selinux_avc.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp,
                      _SYSLOG_SEVERITY, _to_ms, bsd_year_ts, ratio_detect,
                      split_any, block_ratio, mk_ts)


def _sev_num_to_level(n: int) -> str:
    """CEF / normalized 0-10 severity → canonical level."""
    if n >= 9:
        return "fatal"
    if n >= 7:
        return "error"
    if n >= 4:
        return "warn"
    return "info"


# ── ArcSight CEF (MikroTik, Imperva, many vendors) ───────────────────────────
#   Jan 24 12:32:10 host CEF:0|Security|threatmanager|1.0|100|worm stopped|10|src=..
class CEFAdapter(LogAdapter):
    name = "cef"
    language = "any"
    _MARK = re.compile(r"CEF:\d\|")
    _EXT = re.compile(r"(\w+)=((?:[^=\\]|\\.)*?)(?=\s+\w+=|$)")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._MARK.search(ln)))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        mk = self._MARK.search(s)
        if not mk:
            return None
        prefix = s[:mk.start()].strip()
        body = s[mk.start():]
        # split header on unescaped '|' into 8 parts (7 header fields + extension)
        parts = re.split(r"(?<!\\)\|", body, maxsplit=7)
        parts += [""] * (8 - len(parts))
        _, vendor, product, version, sig_id, name, severity, ext = parts[:8]
        fields = {"vendor": vendor, "product": product, "device_version": version,
                  "signature_id": sig_id}
        for k, v in self._EXT.findall(ext):
            fields[k] = v.replace("\\=", "=").replace("\\\\", "\\")
        # severity may be numeric 0-10 or Low/Medium/High/Very-High
        lvl = ""
        sev = severity.strip()
        if sev.isdigit():
            lvl = _sev_num_to_level(int(sev))
        else:
            lvl = {"low": "info", "medium": "warn", "high": "error",
                   "very-high": "fatal"}.get(sev.lower(), "")
        ts_ms = None
        if prefix:
            ts_ms = parse_timestamp(prefix) or parse_timestamp(prefix[-40:])
        ts_ms = ts_ms or parse_timestamp(fields.get("rt", "")) or None
        return self._event(level=lvl, message=name or sig_id, source=product or vendor,
                           ts_ms=ts_ms, fields=fields, raw=line)


# ── IBM QRadar LEEF ──────────────────────────────────────────────────────────
#   <134>May 1 12:00:00 gw LEEF:2.0|Lancope|SW|1.0|41|^|src=..^dst=..^sev=5
class LEEFAdapter(LogAdapter):
    name = "leef"
    language = "any"
    _MARK = re.compile(r"LEEF:(?P<ver>[12]\.0)\|")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._MARK.search(ln)))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        mk = self._MARK.search(s)
        if not mk:
            return None
        prefix = s[:mk.start()].strip()
        body = s[mk.start():]
        ver = mk.group("ver")
        parts = re.split(r"(?<!\\)\|", body)
        # LEEF:1.0 → 5 header fields before attrs; LEEF:2.0 → optional delimiter field
        delim = "\t"
        if ver == "2.0" and len(parts) >= 7:
            d = parts[5]
            if d:
                # delimiter may be a literal char or 'x09'/'0x09' hex notation
                if d.lower() in ("x09", "0x09"):
                    delim = "\t"
                elif len(d) == 1:
                    delim = d
                else:
                    try:
                        delim = chr(int(d.lstrip("x0"), 16))
                    except Exception:
                        delim = "\t"
            attrs = "|".join(parts[6:])
            vendor, product, dver, event_id = parts[1], parts[2], parts[3], parts[4]
        else:
            attrs = "|".join(parts[5:])
            vendor, product, dver, event_id = (parts[1] if len(parts) > 1 else "",
                                               parts[2] if len(parts) > 2 else "",
                                               parts[3] if len(parts) > 3 else "",
                                               parts[4] if len(parts) > 4 else "")
        fields = {"vendor": vendor, "product": product, "device_version": dver,
                  "event_id": event_id}
        for pair in attrs.split(delim):
            if "=" in pair:
                k, _, v = pair.partition("=")
                fields[k.strip()] = v.strip()
        lvl = ""
        sev = str(fields.get("sev", ""))
        if sev.isdigit():
            lvl = _sev_num_to_level(int(sev))
        ts_ms = None
        if prefix:
            ts_ms = parse_timestamp(prefix) or parse_timestamp(prefix[-40:])
        return self._event(level=lvl, message=fields.get("cat", event_id) or "leef",
                           source=product or vendor, ts_ms=ts_ms, fields=fields, raw=line)


# ── Cisco ASA / PIX / FWSM / FTD syslog ──────────────────────────────────────
#   <166>Jun 27 2018 12:17:46 asa-fw : %ASA-6-302016: Teardown UDP connection ...
class CiscoASAAdapter(LogAdapter):
    name = "cisco_asa"
    language = "any"
    _RE = re.compile(r"%(?P<fac>ASA|PIX|FWSM|ASASM|FTD)-(?P<sev>[0-7])-(?P<msgid>\d{3,7}):\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.search(ln)))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        m = self._RE.search(s)
        if not m:
            return None
        head = s[:m.start()]
        head = re.sub(r"^<\d{1,3}>", "", head).strip().rstrip(":").strip()
        return self._event(level=_SYSLOG_SEVERITY.get(int(m.group("sev")), "INFO"),
                           message=m.group("msg"), source=f"cisco.{m.group('fac').lower()}",
                           ts_ms=bsd_year_ts(head),
                           fields={"message_id": m.group("msgid"),
                                   "severity": int(m.group("sev")),
                                   "facility": m.group("fac")}, raw=line)


# ── Cisco IOS / IOS-XE / NX-OS syslog ────────────────────────────────────────
#   Jan 24 12:20:11 sw1 123456: *Jan 24 12:20:10.512: %LINK-3-UPDOWN: Interface ...
class CiscoIOSAdapter(LogAdapter):
    name = "cisco_ios"
    language = "any"
    # facility not one of the ASA-family names; mnemonic starts with a letter.
    _RE = re.compile(r"%(?P<fac>[A-Z][A-Z0-9_]+)-(?P<sev>[0-7])-(?P<mnem>[A-Z][A-Z0-9_]*):\s*(?P<msg>.*)$")
    _ASA_FAC = {"ASA", "PIX", "FWSM", "ASASM", "FTD"}

    def detect(self, sample_lines):
        def ok(ln):
            m = self._RE.search(ln)
            return bool(m) and m.group("fac") not in self._ASA_FAC
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        m = self._RE.search(s)
        if not m or m.group("fac") in self._ASA_FAC:
            return None
        # the last "Mon DD HH:MM:SS(.mmm)" before the %FAC token is the service ts
        tsm = re.findall(r"([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)", s[:m.start()])
        ts_ms = parse_timestamp(tsm[-1]) if tsm else None
        return self._event(level=_SYSLOG_SEVERITY.get(int(m.group("sev")), "INFO"),
                           message=m.group("msg"),
                           source=f"cisco.{m.group('fac').lower()}", ts_ms=ts_ms,
                           fields={"facility": m.group("fac"), "mnemonic": m.group("mnem"),
                                   "severity": int(m.group("sev"))}, raw=line)


# ── Fortinet FortiGate / FortiOS (key=value syslog) ──────────────────────────
#   date=2019-05-10 time=11:37:47 logid="..." type="traffic" level="notice" srcip=..
class FortiGateAdapter(LogAdapter):
    name = "fortigate"
    language = "any"
    _KV = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')

    def _pairs(self, s: str) -> dict:
        out = {}
        for k, v in self._KV.findall(s):
            if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
                v = v[1:-1]
            out[k] = v
        return out

    def detect(self, sample_lines):
        def ok(ln):
            s = ln.strip()
            return (s.startswith("date=") and "time=" in s
                    and ("logid=" in s or "devname=" in s or "type=" in s))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if not (s.startswith("date=") and "time=" in s):
            return None
        p = self._pairs(s)
        ts_ms = parse_timestamp(f'{p.get("date","")} {p.get("time","")}'.strip())
        if ts_ms is None and p.get("eventtime", "").isdigit():
            ev = int(p["eventtime"])
            ts_ms = ev / 1e6 if ev > 1e15 else ev * 1000.0 if ev < 1e11 else float(ev)
        subtype = p.get("subtype", "")
        msg = p.get("msg") or p.get("logdesc") or f'{p.get("type","")}/{subtype} ' \
              f'{p.get("action","")}'.strip()
        return self._event(level=p.get("level", ""), message=msg,
                           source=f'fortigate.{p.get("type","")}'.rstrip("."),
                           ts_ms=ts_ms, fields=p, raw=line)


# ── Check Point Log Exporter (syslog, [k:"v"; …]) ────────────────────────────
#   <134>1 2024-03-21T17:32:32Z gw CheckPoint 18160 - [action:"Accept"; src:"..."; ...]
class CheckPointAdapter(LogAdapter):
    name = "checkpoint"
    language = "any"
    _PAIR = re.compile(r'(\w+):"((?:[^"\\]|\\.)*)"')

    def detect(self, sample_lines):
        def ok(ln):
            return "CheckPoint" in ln and bool(re.search(r'\baction:"', ln))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if "CheckPoint" not in s:
            return None
        fields = {k: v for k, v in self._PAIR.findall(s)}
        if not fields:
            return None
        action = (fields.get("action") or "").lower()
        level = "warn" if action in ("drop", "reject", "block") else "info"
        ts_ms = None
        tm = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?", s)
        if tm:
            ts_ms = parse_timestamp(tm.group(0))
        if ts_ms is None and fields.get("time", "").isdigit():
            ts_ms = int(fields["time"]) * 1000.0
        msg = f'{fields.get("action","")} {fields.get("src","")}->{fields.get("dst","")}' \
              f':{fields.get("service","")}'.strip()
        return self._event(level=level, message=msg, source="checkpoint",
                           ts_ms=ts_ms, fields=fields, raw=line)


# ── pfSense / OPNsense filterlog (pf CSV) ────────────────────────────────────
#   Mar  2 10:15:23 fw01 filterlog: 4,,,1000000103,pppoe0,match,block,in,4,...
class PfSenseFilterlogAdapter(LogAdapter):
    name = "pfsense_filterlog"
    language = "any"
    _RE = re.compile(
        r"^(?:(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(?P<host>\S+)\s+)?"
        r"filterlog(?:\[\d+\])?:\s*(?P<csv>\d+,.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        cols = m.group("csv").split(",")
        def col(i):
            return cols[i] if i < len(cols) else ""
        action = col(6)
        direction = col(7)
        ipver = col(8)
        fields = {"rule": col(0), "interface": col(4), "reason": col(5),
                  "action": action, "direction": direction, "ipversion": ipver}
        # protocol/addr layout differs by IP version
        if ipver == "4":
            fields.update({"proto": col(16), "src": col(18), "dst": col(19),
                           "srcport": col(20), "dstport": col(21)})
        elif ipver == "6":
            fields.update({"proto": col(15), "src": col(16), "dst": col(17),
                           "srcport": col(18), "dstport": col(19)})
        level = "warn" if action in ("block", "reject") else "info"
        msg = f'{action} {direction} {fields.get("src","")}:{fields.get("srcport","")}' \
              f' -> {fields.get("dst","")}:{fields.get("dstport","")}'
        return self._event(level=level, message=msg, source="pfsense.filterlog",
                           ts_ms=parse_timestamp(m.group("ts")) if m.group("ts") else None,
                           fields=fields, raw=line)


# ── Palo Alto PAN-OS (TRAFFIC/THREAT/… CSV) ──────────────────────────────────
#   Jan 24 12:28:00 pa1 1,2026/01/24 12:28:00,00180…,TRAFFIC,end,2561,…,allow,…
class PaloAltoAdapter(LogAdapter):
    name = "paloalto_panos"
    language = "any"
    _TYPES = {"TRAFFIC", "THREAT", "SYSTEM", "CONFIG", "HIPMATCH", "HIP-MATCH",
              "URL", "WILDFIRE", "GLOBALPROTECT", "USERID", "DECRYPTION"}
    # optional syslog header, then: FUTURE_USE, receive_time, serial, TYPE, ...
    _RE = re.compile(
        r"^(?:.*?\s)?(?P<fu>\d+),(?P<rt>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}),"
        r"(?P<serial>[^,]*),(?P<type>[A-Z\-]+),(?P<rest>.*)$")

    def detect(self, sample_lines):
        def ok(ln):
            m = self._RE.match(ln.strip())
            return bool(m) and m.group("type") in self._TYPES
        return ratio_detect(sample_lines, ok)

    _ACTIONS = {"allow", "deny", "drop", "alert", "block", "reset-both",
                "reset-client", "reset-server", "sinkhole", "default", "drop-all",
                "random-drop", "block-url", "block-ip", "continue", "override"}
    _SEVERITIES = {"critical": "fatal", "high": "error", "medium": "warn",
                   "low": "info", "informational": "info"}

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m or m.group("type") not in self._TYPES:
            return None
        typ = m.group("type")
        cols = m.group("rest").split(",")
        def col(i):
            return cols[i] if i < len(cols) else ""
        # `rest` starts at Threat/Content Type (PAN field 5). The early columns are
        # stable across PAN-OS versions: [0]=subtype [1]=FUTURE_USE [2]=generated_time
        # [3]=src [4]=dst [5]=natsrc [6]=natdst [7]=rule [8]=srcuser [9]=dstuser [10]=app.
        fields = {"log_type": typ, "serial": m.group("serial"), "subtype": col(0),
                  "src": col(3), "dst": col(4), "rule": col(7), "app": col(10)}
        # action / threat-severity positions drift across versions → keyword-scan.
        action = next((c for c in cols if c.lower() in self._ACTIONS), "")
        fields["action"] = action
        ts = _pan_ts(m.group("rt"))
        level = "info"
        if typ == "THREAT":
            sev = next((c for c in cols if c.lower() in self._SEVERITIES), "")
            level = self._SEVERITIES.get(sev.lower(), "warn")
            fields["threat_severity"] = sev
        elif action.lower() in ("deny", "drop", "reset-both", "reset-client",
                                 "reset-server", "block", "sinkhole"):
            level = "warn"
        msg = f'{typ} {action} {fields["src"]}->{fields["dst"]} {fields["app"]}'.strip()
        return self._event(level=level, message=msg, source=f"panos.{typ.lower()}",
                           ts_ms=ts, fields=fields, raw=line)


def _pan_ts(s: str) -> Optional[float]:
    m = re.match(r"(\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})", s or "")
    if not m:
        return None
    try:
        return _to_ms(datetime(*[int(x) for x in m.groups()]))
    except (ValueError, TypeError):
        return None


# ── Suricata fast.log ────────────────────────────────────────────────────────
#   07/16/2015-01:32:12.275324  [**] [1:2008983:6] ET … [Priority: 1] {TCP} a:b -> c:d
class SuricataFastAdapter(LogAdapter):
    name = "suricata_fast"
    language = "any"
    _RE = re.compile(
        r"^(?P<mo>\d{2})/(?P<dy>\d{2})/(?P<yr>\d{4})-(?P<hh>\d{2}):(?P<mm>\d{2}):(?P<ss>\d{2})"
        r"\.(?P<us>\d+)\s+\[\*\*\]\s+\[(?P<gid>\d+):(?P<sid>\d+):(?P<rev>\d+)\]\s+"
        r"(?P<msg>.*?)\s+\[\*\*\](?:\s+\[Classification:\s*(?P<cls>[^\]]*)\])?"
        r"(?:\s+\[Priority:\s*(?P<pri>\d+)\])?\s+\{(?P<proto>[^}]+)\}\s+"
        r"(?P<src>\S+):(?P<sport>\d+)\s+->\s+(?P<dst>\S+):(?P<dport>\d+)")
    _PRI = {"1": "error", "2": "warn", "3": "info", "4": "debug"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        ts_ms = None
        try:
            ts_ms = _to_ms(datetime(int(g["yr"]), int(g["mo"]), int(g["dy"]),
                                    int(g["hh"]), int(g["mm"]), int(g["ss"]),
                                    int(g["us"].ljust(6, "0")[:6])))
        except ValueError:
            pass
        return self._event(level=self._PRI.get(g["pri"], "warn"), message=g["msg"],
                           source="suricata", ts_ms=ts_ms,
                           fields={"gid": g["gid"], "sid": g["sid"], "rev": g["rev"],
                                   "classification": g["cls"], "priority": g["pri"],
                                   "proto": g["proto"], "src": f'{g["src"]}:{g["sport"]}',
                                   "dst": f'{g["dst"]}:{g["dport"]}'}, raw=line)


# ── Snort fast alert (no year in timestamp) ──────────────────────────────────
#   07/16-09:23:39.153899  [**] [1:1000000:0] "…" [**] [Priority: 1] {TCP} a:b -> c:d
class SnortFastAdapter(LogAdapter):
    name = "snort_fast"
    language = "any"
    _RE = re.compile(
        r"^(?P<mo>\d{2})/(?P<dy>\d{2})-(?P<hh>\d{2}):(?P<mm>\d{2}):(?P<ss>\d{2})"
        r"\.(?P<us>\d+)\s+\[\*\*\]\s+\[(?P<gid>\d+):(?P<sid>\d+):(?P<rev>\d+)\]\s+"
        r'(?P<msg>.*?)\s+\[\*\*\](?:\s+\[Classification:\s*(?P<cls>[^\]]*)\])?'
        r"(?:\s+\[Priority:\s*(?P<pri>\d+)\])?\s+\{(?P<proto>[^}]+)\}\s+"
        r"(?P<src>\S+):(?P<sport>\d+)\s+->\s+(?P<dst>\S+):(?P<dport>\d+)")
    _PRI = {"1": "error", "2": "warn", "3": "info", "4": "debug"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        ts_ms = None
        try:                                   # no year in the stream → assume current
            ts_ms = _to_ms(datetime(datetime.now().year, int(g["mo"]), int(g["dy"]),
                                    int(g["hh"]), int(g["mm"]), int(g["ss"]),
                                    int(g["us"].ljust(6, "0")[:6])))
        except ValueError:
            pass
        msg = g["msg"].strip('"')
        return self._event(level=self._PRI.get(g["pri"], "warn"), message=msg,
                           source="snort", ts_ms=ts_ms,
                           fields={"gid": g["gid"], "sid": g["sid"], "rev": g["rev"],
                                   "classification": g["cls"], "priority": g["pri"],
                                   "proto": g["proto"], "src": f'{g["src"]}:{g["sport"]}',
                                   "dst": f'{g["dst"]}:{g["dport"]}'}, raw=line)


# ── OpenSSH sshd auth events (with or without a syslog envelope) ──────────────
#   May  9 06:11:22 host sshd[2843]: Accepted password for alice from 10.0.0.5 port 51044 ssh2
class SSHDAuthAdapter(LogAdapter):
    name = "sshd_auth"
    language = "any"
    _TAG = re.compile(r"\bsshd(?:\[\d+\])?:\s*(?P<msg>.*)$")
    _PHRASE = re.compile(
        r"(Accepted|Failed) (password|publickey|keyboard-interactive)|Invalid user|"
        r"authentication failure|Connection closed by|Disconnected from|"
        r"maximum authentication attempts|Received disconnect", re.IGNORECASE)
    _FROM = re.compile(r"for (?:invalid user )?(?P<user>\S+) from (?P<ip>\S+) port (?P<port>\d+)")

    def detect(self, sample_lines):
        def ok(ln):
            m = self._TAG.search(ln)
            return bool(m) and bool(self._PHRASE.search(m.group("msg")))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        m = self._TAG.search(s)
        if not m or not self._PHRASE.search(m.group("msg")):
            return None
        msg = m.group("msg")
        low = msg.lower()
        level = "warn" if ("failed" in low or "invalid user" in low
                           or "authentication failure" in low) else "info"
        fields = {}
        fm = self._FROM.search(msg)
        if fm:
            fields.update({"user": fm.group("user"), "src_ip": fm.group("ip"),
                           "port": fm.group("port")})
        tm = re.match(r"([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})", s)
        return self._event(level=level, message=msg, source="sshd",
                           ts_ms=parse_timestamp(tm.group(1)) if tm else None,
                           fields=fields, raw=line)


# ── dnsmasq query/DHCP log ───────────────────────────────────────────────────
#   Jul  7 20:19:36 dnsmasq[536]: query[A] dnl-14.geo.kaspersky.com from 10.0.10.128
class DnsmasqAdapter(LogAdapter):
    name = "dnsmasq"
    language = "any"
    _TAG = re.compile(r"\bdnsmasq(?:-dhcp)?(?:\[\d+\])?:\s*(?P<msg>.*)$")
    # verbs include the Pi-hole additions (its FTL logs through dnsmasq):
    #   "gravity blocked tags.tiqcdn.com is 0.0.0.0" / "exactly blacklisted …"
    _QUERY = re.compile(r"^(?P<verb>query|forwarded|cached(?: reverse)?|reply|config|"
                        r"gravity blocked|exactly (?:blacklisted|denied)|"
                        r"regex (?:blacklisted|denied)|blacklisted|special domain)"
                        r"(?:\[(?P<qt>[^\]]+)\])?"
                        r"\s+(?P<name>\S+)(?:\s+(?:from|to|is)\s+(?P<peer>\S+))?")

    def detect(self, sample_lines):
        def ok(ln):
            m = self._TAG.search(ln)
            if not m:
                return False
            body = m.group("msg")
            return bool(self._QUERY.match(body)) or body.startswith("DHCP")
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        m = self._TAG.search(s)
        if not m:
            return None
        body = m.group("msg")
        fields = {}
        q = self._QUERY.match(body)
        if q:
            fields.update({"verb": q.group("verb"), "qtype": q.group("qt"),
                           "name": q.group("name"), "peer": q.group("peer")})
        elif not body.startswith("DHCP"):
            return None
        else:
            fields["verb"] = body.split(None, 1)[0]
        tm = re.match(r"([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})", s)
        return self._event(level="", message=body, source="dnsmasq",
                           ts_ms=parse_timestamp(tm.group(1)) if tm else None,
                           fields=fields, raw=line)


# ── ISC dhcpd (also OpenBSD/VyOS) ────────────────────────────────────────────
#   Mar  2 09:15:23 server dhcpd: DHCPREQUEST for 192.168.1.130 from aa:bb:.. via eth0
class IscDhcpdAdapter(LogAdapter):
    name = "isc_dhcpd"
    language = "any"
    _TAG = re.compile(r"\bdhcpd(?:\[\d+\])?:\s*(?P<msg>.*)$")
    _MSG = re.compile(r"^(?P<type>DHCP[A-Z]+)\b(?P<rest>.*)$")

    def detect(self, sample_lines):
        def ok(ln):
            m = self._TAG.search(ln)
            return bool(m) and bool(self._MSG.match(m.group("msg")))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        m = self._TAG.search(s)
        if not m:
            return None
        mm = self._MSG.match(m.group("msg"))
        if not mm:
            return None
        rest = mm.group("rest")
        fields = {"msg_type": mm.group("type")}
        ipm = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", rest)
        if ipm:
            fields["ip"] = ipm.group(1)
        macm = re.search(r"\b([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})\b", rest)
        if macm:
            fields["mac"] = macm.group(1)
        ifm = re.search(r"via (\S+)", rest)
        if ifm:
            fields["interface"] = ifm.group(1).rstrip(":")
        level = "warn" if mm.group("type") in ("DHCPNAK", "DHCPDECLINE") else ""
        tm = re.match(r"([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})", s)
        return self._event(level=level, message=m.group("msg"), source="dhcpd",
                           ts_ms=parse_timestamp(tm.group(1)) if tm else None,
                           fields=fields, raw=line)


# ── Zeek (Bro) TSV logs (conn/dns/http/…) ────────────────────────────────────
#   #fields ts uid id.orig_h …   /   1747147647.668533<TAB>Cgno…<TAB>192.168.1.8 …
class ZeekTsvAdapter(LogAdapter):
    name = "zeek_tsv"
    language = "any"
    _HEADER = re.compile(r"^#(separator|set_separator|empty_field|unset_field|path|"
                         r"open|fields|types|close)\b")
    # Real Zeek logs are TAB-separated, but samples (and some viewers) render the
    # same rows with runs of spaces. Accept EITHER separator so a space-rendered
    # conn row still routes here. The signature stays highly specific — epoch.frac,
    # an 8+ char Zeek UID, a dotted-quad, and a port — so it never steals generic
    # whitespace-columned text.
    _SEP = r"(?:\t| {1,})"
    _DATA = re.compile(rf"^\d{{9,10}}\.\d+{_SEP}[A-Za-z0-9]{{8,}}{_SEP}"
                       rf"\d{{1,3}}(?:\.\d{{1,3}}){{3}}{_SEP}\d+{_SEP}")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._HEADER.match(ln)
                                            or self._DATA.match(ln.rstrip("\r\n"))))

    @staticmethod
    def _split_cols(s: str) -> list:
        # Tab-separated is authoritative (preserves '-'/empty fields); fall back
        # to whitespace-run splitting for the space-rendered form.
        return s.split("\t") if "\t" in s else re.split(r"\s+", s.strip())

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if self._HEADER.match(s):
            return self._event(level="", message=s, source="zeek",
                               fields={"zeek_meta": True}, raw=line)
        if not self._DATA.match(s):
            return None
        cols = self._split_cols(s)
        def col(i):
            return cols[i] if i < len(cols) else ""
        ts_ms = None
        try:
            ts_ms = float(cols[0]) * 1000.0
        except (ValueError, IndexError):
            pass
        conn_state = col(11)
        fields = {"uid": col(1), "orig_h": col(2), "orig_p": col(3),
                  "resp_h": col(4), "resp_p": col(5), "proto": col(6),
                  "service": col(7), "conn_state": conn_state}
        # failed / rejected connection states → warn
        level = "warn" if conn_state in ("REJ", "RSTO", "RSTR", "S0", "OTH") else ""
        return self._event(level=level,
                           message=f'{col(2)}:{col(3)} -> {col(4)}:{col(5)} '
                                   f'{col(6)}/{col(7)} {conn_state}'.strip(),
                           source="zeek.conn", ts_ms=ts_ms, trace_id=col(1),
                           fields=fields, raw=line)


# ── OSSEC / Wazuh alerts.log (multi-line ** Alert blocks) ────────────────────
#   ** Alert 1618409999.12345: - syslog,sshd,authentication_failed,
#   Rule: 5716 (level 5) -> 'sshd: authentication failed.'
class OssecWazuhAdapter(LogAdapter):
    name = "ossec_wazuh_alerts"
    language = "any"
    _ALERT = re.compile(r"^\*\* Alert (?P<id>\d+\.\d+):(?:\s*-?\s*(?P<groups>.*))?$")
    _RULE = re.compile(r"^Rule:\s*(?P<sid>\d+)\s*\(level\s*(?P<lvl>\d+)\)\s*->\s*'?(?P<desc>.*?)'?$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._ALERT.match(ln.strip())
                                            or self._RULE.match(ln.strip())))

    @staticmethod
    def _wazuh_level(n: int) -> str:
        if n >= 12:
            return "error"
        if n >= 7:
            return "warn"
        return "info"

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._ALERT.match(s)
        if m:
            return self._event(level="", message=f"Alert {m.group('id')}",
                               source="wazuh", trace_id=m.group("id"),
                               fields={"alert_id": m.group("id"),
                                       "groups": (m.group("groups") or "").strip(", ")},
                               raw=line)
        m = self._RULE.match(s)
        if m:
            lvl_n = int(m.group("lvl"))
            return self._event(level=self._wazuh_level(lvl_n), message=m.group("desc"),
                               source="wazuh", fields={"rule_id": m.group("sid"),
                                                       "wazuh_level": lvl_n}, raw=line)
        return None


# ── SELinux / kernel AVC audit records ───────────────────────────────────────
#   type=AVC msg=audit(1455805464.059:137): avc:  denied  { append } for  pid=861 …
class SelinuxAvcAdapter(LogAdapter):
    name = "selinux_avc"
    language = "any"
    _RE = re.compile(r"type=AVC\s+msg=audit\((?P<epoch>\d+(?:\.\d+)?):(?P<seq>\d+)\):"
                     r"\s*avc:\s*(?P<verdict>denied|granted)\s*\{(?P<perms>[^}]*)\}\s*(?P<rest>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: "type=AVC" in ln and "avc:" in ln
                            and bool(self._RE.search(ln)))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        m = self._RE.search(s)
        if not m:
            return None
        rest = m.group("rest")
        fields = {"verdict": m.group("verdict"), "perms": m.group("perms").strip(),
                  "audit_seq": m.group("seq")}
        for k in ("pid", "comm", "name", "path", "dev", "ino", "scontext",
                  "tcontext", "tclass", "permissive"):
            km = re.search(rf'\b{k}=("[^"]*"|\S+)', rest)
            if km:
                fields[k] = km.group(1).strip('"')
        try:
            ts_ms = float(m.group("epoch")) * 1000.0
        except ValueError:
            ts_ms = None
        level = "warn" if m.group("verdict") == "denied" else "info"
        return self._event(level=level,
                           message=f"avc {m.group('verdict')} {{{m.group('perms').strip()}}} "
                                   f'comm={fields.get("comm","")}'.strip(),
                           source="selinux", ts_ms=ts_ms, fields=fields, raw=line)


# ── MIT Kerberos KDC log (krb5kdc.log) — BATCH 4 ─────────────────────────────
#   Jul 30 23:18:26 host krb5kdc[10544](info): AS_REQ (…) 192.168.1.83(88):
#   ISSUE: authtime 1028085506, … jgarman@WEDGIE.ORG for krbtgt/WEDGIE.ORG@…
class Krb5KdcAdapter(LogAdapter):
    name = "krb5kdc"
    language = "any"
    _RE = re.compile(
        r"^(?:(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(?P<host>\S+)\s+)?"
        r"krb5kdc\[(?P<pid>\d+)\]\((?P<lvl>\w+)\):\s*(?P<msg>.*)$")
    _REQ = re.compile(r"^(?P<kind>AS_REQ|TGS_REQ)\b.*?\s(?P<client>[\d.:a-fA-F\[\]]+)"
                      r"\((?P<port>\d+)\):\s*(?P<result>[A-Z_]+):?\s*(?P<rest>.*)$")
    _PRINC = re.compile(r"(?P<who>\S+@\S+?) for (?P<svc>\S+?)(?:,|$|\s)")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"]
        level = g["lvl"]
        fields = {"pid": int(g["pid"])}
        rm = self._REQ.match(msg)
        if rm:
            fields["request"] = rm.group("kind")
            fields["client"] = rm.group("client")
            fields["result"] = rm.group("result")
            pm = self._PRINC.search(msg)
            if pm:
                fields["principal"] = pm.group("who")
                fields["service"] = pm.group("svc")
            # anything except an ISSUE is an auth failure of some flavor
            if rm.group("result") != "ISSUE":
                level = "warn" if rm.group("result") == "NEEDED_PREAUTH" else "error"
        return self._event(level=level, message=msg, source="krb5kdc",
                           ts_ms=parse_timestamp(g["ts"] or ""),
                           fields=fields, raw=line)


# ── 389 Directory Server access log — BATCH 4 ────────────────────────────────
#   [27/Apr/2015:13:16:35 -0400] conn=324375 op=606903 SRCH base="…" scope=2 …
class Ds389AccessAdapter(LogAdapter):
    name = "ds389_access"
    language = "any"
    _RE = re.compile(
        r"^\[(?P<ts>\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2})(?:\.\d+)?"
        r"\s*(?P<tz>[+-]\d{4})?\]\s+conn=(?P<conn>\d+|Internal\(\d+\))\s+"
        r"op=(?P<op>\S+)\s+(?P<rest>\S.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        rest = g["rest"]
        fields = {"conn": g["conn"], "op": g["op"]}
        vm = re.match(r"^(?P<verb>[A-Z]+)\b", rest)
        if vm:
            fields["verb"] = vm.group("verb")
        for k in ("base", "filter", "dn"):
            km = re.search(rf'\b{k}="([^"]*)"', rest)
            if km:
                fields[k] = km.group(1)
        level = "info"
        em = re.search(r"\berr=(\d+)", rest)
        if em:
            fields["err"] = int(em.group(1))
            if int(em.group(1)) not in (0,):
                level = "warn"
        ts_ms = parse_timestamp(f"{g['ts']} {g['tz'] or ''}".strip())
        return self._event(level=level,
                           message=rest[:200], source="389ds",
                           ts_ms=ts_ms, trace_id=f"conn={g['conn']}",
                           fields=fields, raw=line)


# ── Libreswan / Openswan pluto IKE daemon — BATCH 4 ──────────────────────────
#   pluto[3001]: "west-east" #1: initiating IKEv2 connection
class LibreswanPlutoAdapter(LogAdapter):
    name = "libreswan_pluto"
    language = "any"
    _RE = re.compile(
        r"^(?:(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(?P<host>\S+)\s+)?"
        r"pluto\[(?P<pid>\d+)\]:\s+(?P<msg>.*)$")
    _CONN = re.compile(r'^"(?P<conn>[^"]+)"(?:\[\d+\])?\s+#(?P<state>\d+):\s*(?P<rest>.*)$')

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"].strip()
        fields = {"pid": int(g["pid"])}
        cm = self._CONN.match(msg)
        if cm:
            fields["connection"] = cm.group("conn")
            fields["state_num"] = int(cm.group("state"))
            msg_body = cm.group("rest")
        else:
            msg_body = msg
        low = msg_body.lower()
        level = ("error" if any(w in low for w in
                                ("failed", "error", "timeout", "cannot",
                                 "authentication failed", "invalid"))
                 else "info")
        return self._event(level=level, message=msg, source="pluto",
                           ts_ms=parse_timestamp(g["ts"] or ""),
                           trace_id=fields.get("connection"),
                           fields=fields, raw=line)


# ── Registration ─────────────────────────────────────────────────────────────
# Everything that can ride inside a <PRI>/BSD syslog envelope registers
# before="syslog" so it outranks the generic syslog/systemd adapters on a tie.
# cisco_asa registers before cisco_ios so the ASA-family (numeric msgid) wins its
# own lines. selinux_avc registers before the generic auditd adapter.
# Batch 4: krb5kdc + pluto ride the same envelope → before="syslog" too.
register_adapter(Krb5KdcAdapter(), before="syslog")
register_adapter(LibreswanPlutoAdapter(), before="syslog")
register_adapter(Ds389AccessAdapter())
register_adapter(CEFAdapter(), before="syslog")
register_adapter(LEEFAdapter(), before="syslog")
register_adapter(CiscoASAAdapter(), before="syslog")
register_adapter(CiscoIOSAdapter(), before="syslog")
register_adapter(FortiGateAdapter(), before="syslog")
register_adapter(CheckPointAdapter(), before="syslog")
register_adapter(PfSenseFilterlogAdapter(), before="syslog")
register_adapter(PaloAltoAdapter(), before="syslog")
register_adapter(SSHDAuthAdapter(), before="syslog")
register_adapter(DnsmasqAdapter(), before="syslog")
register_adapter(IscDhcpdAdapter(), before="syslog")
register_adapter(SelinuxAvcAdapter(), before="auditd")
register_adapter(SuricataFastAdapter())
register_adapter(SnortFastAdapter())
register_adapter(ZeekTsvAdapter())
register_adapter(OssecWazuhAdapter())


# ═══════════════════════════ BATCH 5 ═════════════════════════════════════════

# ── ModSecurity serial audit log (multi-part boundary format) ─────────────────
#   --a1b2c3d4-A--
#   [24/Jan/2026:12:38:00 +0000] Yabc123 203.0.113.5 51882 192.168.1.10 443
#   --a1b2c3d4-H--
#   Message: Access denied with code 403. … [severity "CRITICAL"]
#   --a1b2c3d4-Z--
class ModSecurityAdapter(LogAdapter):
    name = "modsecurity"
    language = "any"
    _BOUNDARY = re.compile(r"^--(?P<id>[0-9a-fA-F]{6,10})-(?P<sec>[A-Z])--\s*$")
    _MESSAGE = re.compile(r"^Message:\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        def ok(el):
            subs = split_any(el)
            bounds = sum(1 for x in subs if self._BOUNDARY.match(x.strip()))
            if bounds >= 2:
                return True
            # a single boundary line fed line-by-line still belongs to us
            return len(subs) == 1 and bounds == 1
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if len(subs) > 1:                      # whole audit entry → one event
            if not any(self._BOUNDARY.match(x.strip()) for x in subs):
                return None
            tx_id = None
            ts_ms = None
            msg = ""
            request = ""
            section = ""
            for x in subs:
                st = x.strip()
                bm = self._BOUNDARY.match(st)
                if bm:
                    section = bm.group("sec")
                    tx_id = tx_id or bm.group("id")
                    continue
                if section == "A" and st.startswith("["):
                    am = re.match(r"^\[(?P<ts>[^\]]+)\]\s+(?P<uid>\S+)\s+(?P<rest>.*)$", st)
                    if am:
                        ts_ms = parse_timestamp(am.group("ts"))
                        tx_id = am.group("uid")
                elif section == "B" and not request:
                    request = st
                elif section == "H":
                    mm = self._MESSAGE.match(st)
                    if mm:
                        msg = mm.group("msg")
            sev = re.search(r"\[severity \"(\w+)\"\]", msg or "")
            sevname = (sev.group(1).upper() if sev else "")
            level = ("fatal" if sevname in ("EMERGENCY", "ALERT")
                     else "error" if sevname in ("CRITICAL", "ERROR")
                     or "Access denied" in (msg or "")
                     else "warn")
            rid = re.search(r"\[id \"(\d+)\"\]", msg or "")
            return self._event(level=level,
                               message=msg or request or "modsecurity audit entry",
                               source="modsecurity", ts_ms=ts_ms,
                               category="security" if level == "warn" else "error",
                               trace_id=tx_id,
                               fields={"request": request or None,
                                       "rule_id": rid.group(1) if rid else None},
                               raw=line)
        st = s.strip()
        bm = self._BOUNDARY.match(st)
        if bm:
            return self._event(level="", message=st, source="modsecurity",
                               category="security",
                               fields={"section": bm.group("sec")},
                               trace_id=bm.group("id"), raw=line)
        mm = self._MESSAGE.match(st)
        if mm:
            return self._event(level="error", message=mm.group("msg"),
                               source="modsecurity", raw=line)
        return None


# ── F5 BIG-IP ASM request-event k="v" log ──────────────────────────────────────
#   hostname="asm.example.com",…,device_vendor="F5",…,support_id="316…",…
class F5AsmAdapter(LogAdapter):
    name = "f5_asm"
    language = "any"
    _PAIR = re.compile(r'([\w.\-]+)="((?:[^"\\]|\\.)*)"')

    def detect(self, sample_lines):
        def ok(ln):
            s = ln.strip()
            return ('support_id="' in s
                    and ('device_vendor="F5"' in s or 'errdefs_msgno="' in s
                         or 'unit_hostname="' in s
                         or 'management_ip_address="' in s))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        pairs = dict(self._PAIR.findall(s))
        if "support_id" not in pairs:
            return None
        status = pairs.get("request_status", "")
        level = ("error" if status in ("blocked",)
                 else "warn" if status in ("illegal", "alerted")
                 else "info")
        msg = (f'{pairs.get("http_method", "?")} '
               f'{pairs.get("client_request_uri") or pairs.get("uri", "?")} '
               f'[{status or "passed"}]')
        keep = {k: v for k, v in pairs.items()
                if k in ("client_ip", "dest_ip", "dest_port", "http_method",
                         "request_status", "action", "attack_type", "bot_name",
                         "severity", "violations", "errdefs_msgno", "session_id",
                         "policy_name", "sig_ids")}
        return self._event(level=level, message=msg, source="f5.asm",
                           category="security" if level == "info" else level_cat(level),
                           trace_id=pairs.get("support_id"),
                           fields=keep, raw=line)


# ── OpenBSD/pf pflog rendered by tcpdump ───────────────────────────────────────
#   Jun 19 21:29:24.402370 rule 16/(match) block in on em0: 10.0.0.1.4923 > …
class PflogTextAdapter(LogAdapter):
    name = "pflog_text"
    language = "any"
    _RE = re.compile(
        r"^(?:(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+)?"
        r"rule\s+(?P<rule>[\w.\-]+/(?:\([\w-]+\)|[\w-]+))[:,]?\s+"
        r"(?P<action>pass|block|match|rdr|nat|binat|scrub)\s+"
        r"(?P<dir>in|out)\s+on\s+(?P<iface>[\w.]+):\s*(?P<pkt>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        level = "warn" if g["action"] == "block" else "info"
        flow = re.match(r"^(?P<src>\S+)\s+>\s+(?P<dst>\S+?):?\s", g["pkt"] + " ")
        fields = {"rule": g["rule"], "action": g["action"],
                  "direction": g["dir"], "interface": g["iface"]}
        if flow:
            fields.update({"src": flow.group("src"), "dst": flow.group("dst")})
        return self._event(level=level,
                           message=f'{g["action"]} {g["dir"]} on {g["iface"]}: '
                                   f'{g["pkt"][:120]}',
                           source="pf", category="security",
                           ts_ms=parse_timestamp(g["ts"]) if g["ts"] else None,
                           fields=fields, raw=line)


# ── ISC dhcpd.leases file ──────────────────────────────────────────────────────
#   lease 192.168.1.150 { starts 4 2026/03/04 10:15:30; … binding state active; …}
class DhcpdLeasesAdapter(LogAdapter):
    name = "dhcpd_leases"
    language = "any"
    _OPEN = re.compile(r"^lease\s+(?P<ip>\d+\.\d+\.\d+\.\d+)\s*\{")
    _BODY = re.compile(
        r"^\s*(starts|ends|tstp|tsfp|atsfp|cltt)\s+\d\s+\d{4}/\d{2}/\d{2}|"
        r"^\s*(binding state|next binding state|rewind binding state|hardware ethernet|"
        r"uid|client-hostname|set |option |on |bootp|reserved|abandoned)|^\s*\}\s*$|"
        r"^(server-duid|authoring-byte-order|lease-file-format)\b|^#")

    def _block_line(self, s: str) -> bool:
        return bool(self._OPEN.match(s.strip()) or self._BODY.match(s))

    def detect(self, sample_lines):
        def ok(el):
            subs = split_any(el)
            has_lease = any(self._OPEN.match(x.strip()) for x in subs)
            has_body = "binding state" in str(el) or any(
                self._BODY.match(x) for x in subs if not self._OPEN.match(x.strip()))
            if has_lease and (has_body or len(subs) == 1 and "binding state" in str(el)):
                return True
            # header-only elements (server-duid …) count when fed alone
            return len(subs) == 1 and bool(re.match(
                r"^(server-duid|authoring-byte-order|lease-file-format)\b",
                subs[0].strip()))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        st = s.strip()
        om = self._OPEN.match(st)
        if om:                                  # inline or block-open lease
            fields = {"ip": om.group("ip")}
            bs = re.search(r"\bbinding state (\w+);", st)
            if bs:
                fields["binding_state"] = bs.group(1)
            hw = re.search(r"hardware ethernet ([0-9a-fA-F:]+);", st)
            if hw:
                fields["mac"] = hw.group(1)
            hn = re.search(r'client-hostname "([^"]*)"', st)
            if hn:
                fields["hostname"] = hn.group(1)
            ts_ms = None
            sm = re.search(r"starts \d (\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})", st)
            if sm:
                # dhcpd lease times are UTC by default
                from datetime import datetime as _dt, timezone as _tz
                try:
                    ts_ms = _dt(*(int(x) for x in sm.groups()),
                                tzinfo=_tz.utc).timestamp() * 1000.0
                except ValueError:
                    ts_ms = None
            msg = f'lease {om.group("ip")}'
            if fields.get("binding_state"):
                msg += f' ({fields["binding_state"]})'
            return self._event(level="info", message=msg, source="dhcpd.leases",
                               ts_ms=ts_ms, category="event", fields=fields,
                               raw=line)
        if self._BODY.match(s):
            return self._event(level="", message=st, source="dhcpd.leases",
                               category="event", raw=line)
        return None


def level_cat(level: str) -> str:
    return {"error": "error", "warn": "warn"}.get(level, "security")


register_adapter(ModSecurityAdapter())
register_adapter(F5AsmAdapter())
register_adapter(PflogTextAdapter(), before="systemd")
register_adapter(DhcpdLeasesAdapter())


# ═════════════════════════════════════════════════════════════════════════════
# BATCH 6 — runtime security + Windows security text formats
# ═════════════════════════════════════════════════════════════════════════════
from ._common import multiline_ratio_detect  # noqa: E402


# ── Falco runtime-security alerts (text output) ───────────────────────────────
#   12:41:00.123456789: Warning Shell spawned in a container (user=root …) k8s.ns=…
class FalcoAdapter(LogAdapter):
    name = "falco"
    language = "any"
    _RE = re.compile(
        r"^(?P<time>\d{2}:\d{2}:\d{2}\.\d{6,9}):\s+"
        r"(?P<prio>Emergency|Alert|Critical|Error|Warning|Notice|Informational|Debug)\s+"
        r"(?P<msg>.*)$")
    _LVL = {"Emergency": "fatal", "Alert": "fatal", "Critical": "fatal",
            "Error": "error", "Warning": "warn", "Notice": "info",
            "Informational": "info", "Debug": "debug"}

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"]
        fields = {"priority": g["prio"]}
        km = re.search(r"\(([^)]*=[^)]*)\)", msg)
        if km:
            for k, v in re.findall(r"([\w.]+)=(\S+)", km.group(1)):
                fields[k] = v
            fields["rule"] = msg[:km.start()].strip()
        for k, v in re.findall(r"(k8s\.[\w.]+)=(\S+)", msg):
            fields[k] = v
        level = self._LVL.get(g["prio"], "info")
        # a triggered Falco rule is a security event — floor at warn
        if level in ("info", "debug"):
            level = "warn" if g["prio"] not in ("Informational", "Debug") else level
        return self._event(level=level, message=msg, source="falco",
                           ts_ms=parse_timestamp(g["time"].split(".")[0]),
                           fields=fields, raw=line)


# ── Microsoft Defender detection message (rendered event 1116/1117 text) ─────
#   Microsoft Defender Antivirus has detected malware…  + ' Name:'/' Severity:' lines
class DefenderDetectionAdapter(LogAdapter):
    name = "defender_detection"
    language = "windows"
    _HEAD = re.compile(r"^Microsoft Defender Antivirus has (?:detected|taken action|found)")
    _KV = re.compile(r"^\s*(?P<k>Name|ID|Severity|Category|Path|Detection Origin|"
                     r"Detection Type|Detection Source|User|Process Name|Action|"
                     r"Signature Version|Engine Version)\s*:\s*(?P<v>.*)$")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            if not subs:
                return False
            head = any(self._HEAD.match(x.strip()) for x in subs)
            kvs = sum(1 for x in subs if self._KV.match(x))
            return head and (len(subs) == 1 or kvs >= 2)
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if not subs or not any(self._HEAD.match(x.strip()) for x in subs):
            return None
        fields = {}
        for x in subs:
            m = self._KV.match(x)
            if m:
                fields[m.group("k").lower().replace(" ", "_")] = m.group("v").strip()
        name = fields.get("name", "")
        sev = fields.get("severity", "")
        level = "error" if sev in ("Severe", "High") or not sev else "warn"
        msg = f"Defender detected {name}".strip() if name else subs[0].strip()
        return self._event(level=level, message=msg, source="windows_defender",
                           fields=fields or None, raw=line)


# ── Windows Firewall pfirewall.log (W3C-style rows) ──────────────────────────
#   2023-05-01 12:00:00 DROP TCP 203.0.113.9 10.0.0.2 51234 445 52 S … RECEIVE
class WindowsFirewallAdapter(LogAdapter):
    name = "windows_firewall"
    language = "windows"
    _ROW = re.compile(
        r"^(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2})\s+"
        r"(?P<action>DROP|ALLOW|OPEN|CLOSE|OPEN-INBOUND|INFO-EVENTS-LOST)\s+"
        r"(?P<proto>TCP|UDP|ICMP|IGMP|\d+|-)\s+"
        r"(?P<src>[\da-fA-F.:\-]+)\s+(?P<dst>[\da-fA-F.:\-]+)\s+"
        r"(?P<sport>\d+|-)\s+(?P<dport>\d+|-)\s?(?P<rest>.*)$")
    _HDR = re.compile(r"^#(?:Software: Microsoft Windows Firewall|Fields: date time action protocol|Version:)")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines,
            lambda ln: bool(self._ROW.match(ln.strip()) or self._HDR.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if self._HDR.match(s):
            return self._event(level="info", message=s.lstrip("#"),
                               source="windows_firewall", raw=line)
        m = self._ROW.match(s)
        if not m:
            return None
        g = m.groupdict()
        level = "warn" if g["action"] in ("DROP", "INFO-EVENTS-LOST") else "info"
        toks = g["rest"].split()
        path = toks[-1] if toks and toks[-1] in ("SEND", "RECEIVE") else None
        return self._event(level=level,
                           message=f'{g["action"]} {g["proto"]} '
                                   f'{g["src"]}:{g["sport"]} -> {g["dst"]}:{g["dport"]}',
                           source="windows_firewall",
                           ts_ms=parse_timestamp(f'{g["date"]} {g["time"]}'),
                           fields={"action": g["action"], "protocol": g["proto"],
                                   "src": g["src"], "dst": g["dst"],
                                   "src_port": g["sport"], "dst_port": g["dport"],
                                   "path": path}, raw=line)


# batch 6 — the pfirewall data rows also satisfy the generic W3C '#Fields'
# family when a header is present → register before w3c_access so the named
# grammar wins the tie.
register_adapter(WindowsFirewallAdapter(), before="w3c_access")
register_adapter(FalcoAdapter())
register_adapter(DefenderDetectionAdapter())


# ═════════════════════════════════════════════════════════════════════════════
#  BATCH 7 — F5 APM/AFM events, ClamAV scan output, Defender MPLog
# ═════════════════════════════════════════════════════════════════════════════


# ── F5 BIG-IP APM session log line (bare errdefs form) ────────────────────────
#   01490102:5: /Common/test:Common:acb12bdc: Access policy result: Logon_Deny
class F5ApmAdapter(LogAdapter):
    name = "f5_apm"
    language = "any"
    _RE = re.compile(
        r"^(?P<code>01[0-9a-f]{6}):(?P<sev>\d):\s+"
        r"(?P<profile>/\S+?):(?P<partition>\w*):(?P<session>[0-9a-f]{8}):\s*"
        r"(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        sev = int(g["sev"])
        level = _SYSLOG_SEVERITY.get(sev, "INFO")
        if re.search(r"deny|denied|failed", g["msg"], re.IGNORECASE):
            level = "warn" if level in ("INFO", "DEBUG") else level
        return self._event(level=level, message=g["msg"], source="f5.apm",
                           fields={"errdefs_code": g["code"],
                                   "access_profile": g["profile"],
                                   "session_id": g["session"]}, raw=line)


# ── F5 BIG-IP ASM/AFM DoS event (semicolon-delimited key=value) ───────────────
#   action=Blocking;hostname=…;dos_attack_id=…;errdefs_msgno=…;severity=3;…
class F5DosKvAdapter(LogAdapter):
    name = "f5_dos_kv"
    language = "any"

    @staticmethod
    def _pairs(s: str) -> dict:
        out = {}
        for part in s.split(";"):
            if "=" in part:
                k, _, v = part.partition("=")
                if re.match(r"^[\w.\-]+$", k.strip()):
                    out[k.strip()] = v.strip()
        return out

    def _hit(self, s: str) -> bool:
        if "dos_attack_id=" not in s or "errdefs_msgno=" not in s:
            return False
        return len(self._pairs(s)) >= 5

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: self._hit(str(ln).strip()))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if not self._hit(s):
            return None
        p = self._pairs(s)
        sev = p.get("severity", "")
        level = (_SYSLOG_SEVERITY.get(int(sev), "WARN")
                 if sev.isdigit() else "warn")
        msg = f'{p.get("dos_attack_name", "DoS event")} — {p.get("action", "?")}' \
              f' ({p.get("dos_mitigation_action", "")})'.rstrip(" ()")
        ts_ms = bsd_year_ts(p["date_time"]) if p.get("date_time") else None
        consumed = {"severity", "date_time"}
        return self._event(level=level, message=msg, source="f5.dos",
                           ts_ms=ts_ms,
                           fields={k: v for k, v in p.items() if k not in consumed},
                           raw=line)


# ── ClamAV clamscan stdout (+ SCAN SUMMARY block) ─────────────────────────────
#   /home/user/eicar.com: Win.Test.EICAR_HDB-1 FOUND
#   ----------- SCAN SUMMARY -----------
#   Infected files: 1
class ClamavScanAdapter(LogAdapter):
    name = "clamav_scan"
    language = "any"
    _FOUND = re.compile(r"^(?P<path>\S[^:]*): (?P<sig>\S+) FOUND\s*$")
    _BANNER = re.compile(r"^-+ SCAN SUMMARY -+$")
    _SUMKEY = re.compile(
        r"^(Known viruses|Engine version|Scanned directories|Scanned files|"
        r"Infected files|Data scanned|Data read|Time|Start Date|End Date):\s*(.*)$")

    def detect(self, sample_lines):
        def ok(el):
            subs = split_any(el)
            return any(self._FOUND.match(x.strip()) or self._BANNER.match(x.strip())
                       for x in subs)
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        subs = split_any(line)
        fields = {}
        found = []
        for x in subs:
            s = x.strip()
            fm = self._FOUND.match(s)
            if fm:
                found.append((fm.group("path"), fm.group("sig")))
                continue
            km = self._SUMKEY.match(s)
            if km:
                fields[km.group(1).lower().replace(" ", "_")] = km.group(2)
        if not found and not fields and not any(
                self._BANNER.match(x.strip()) for x in subs):
            return None
        if found:
            fields["detections"] = [f"{p}: {sig}" for p, sig in found[:10]]
        infected = fields.get("infected_files", "")
        level = ("error" if found or (infected.split() and infected.split()[0].isdigit()
                                      and int(infected.split()[0]) > 0)
                 else "info")
        msg = (f'{found[0][1]} FOUND in {found[0][0]}' if found
               else f'scan summary: {infected or "?"} infected')
        return self._event(level=level, message=msg, source="clamav",
                           fields=fields, raw=line)


# ── Microsoft Defender MPLog engine text ──────────────────────────────────────
#   2023-01-05T09:44:55.112Z … [Mini-filter] BM telemetry … DETECTIONEVENT MPSOURCE_REALTIME …
class DefenderMpLogAdapter(LogAdapter):
    name = "defender_mplog"
    language = "any"
    _TS = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+(?P<msg>.*)$")
    _MARK = re.compile(
        r"DETECTIONEVENT|DETECTION_ADD|MPSOURCE_\w+|\[Mini-filter\]|"
        r"MpCmdRun|BM telemetry|EngineScanCallback|RTP Perf Log|ThreatCommand")

    def detect(self, sample_lines):
        def ok(ln):
            s = str(ln).strip()
            m = self._TS.match(s)
            return bool(m and self._MARK.search(s))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._TS.match(s)
        if not (m and self._MARK.search(s)):
            return None
        g = m.groupdict()
        detection = "DETECTIONEVENT" in g["msg"] or "DETECTION_ADD" in g["msg"]
        fields = {}
        tm = re.search(r"DETECTIONEVENT\s+(\S+)\s+(\S+?)\s+file:(\S+)", g["msg"])
        if tm:
            fields = {"detection_source": tm.group(1), "threat": tm.group(2),
                      "file": tm.group(3)}
        return self._event(level="error" if detection else "info",
                           message=g["msg"][:300], source="defender.mplog",
                           ts_ms=parse_timestamp(g["ts"]),
                           fields=fields or None, raw=line)


for _a in (F5ApmAdapter(), F5DosKvAdapter(), ClamavScanAdapter(),
           DefenderMpLogAdapter()):
    register_adapter(_a)
