"""
Network-infrastructure log adapters (BATCH 3)
================================================================================
Routers, load balancers, proxies, VPNs and DNS servers — the text formats that
sit between the app and the internet. Firewall/IDS families (ASA, FortiGate,
Suricata, pfSense…) shipped in batch 1 (security.py); these are the remaining
high-priority network formats.

Formats: bind_named, unbound, coredns, squid_access, squid_cache, openvpn,
cisco_iosxr, mikrotik_routeros, f5_bigip_ltm, citrix_netscaler.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp,
                      ratio_detect, multiline_ratio_detect,
                      _SYSLOG_SEVERITY, _MONTHS, _to_ms, us_date_ts)


def _dd_mon_yyyy_ts(text: str) -> Optional[float]:
    """BIND-style '24-Mar-2022 15:52:00.178' → epoch ms (naive → local)."""
    m = re.match(r"(\d{1,2})-([A-Z][a-z]{2})-(\d{4}) (\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?",
                 text or "")
    if not m or m.group(2) not in _MONTHS:
        return None
    dy, mon, yr, hh, mm, ss, frac = m.groups()
    micro = int((frac or "0").ljust(6, "0")[:6])
    try:
        return _to_ms(datetime(int(yr), _MONTHS[mon], int(dy), int(hh),
                               int(mm), int(ss), micro))
    except ValueError:
        return None


# ── BIND / named query log ───────────────────────────────────────────────────
#   24-Mar-2022 15:52:00.178 queries: info: client @0x7f34… 10.155.105.100#54387 (www.akamai.com): query: …
class BindNamedAdapter(LogAdapter):
    name = "bind_named"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{1,2}-[A-Z][a-z]{2}-\d{4} \d{2}:\d{2}:\d{2}\.\d+)\s+"
        r"(?P<cat>[\w-]+):\s+(?P<lvl>debug|info|notice|warning|error|critical)"
        r"(?:\s+\d+)?:?\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {"log_category": g["cat"]}
        qm = re.search(r"query:\s+(?P<qname>\S+)\s+(?P<qclass>\S+)\s+(?P<qtype>\S+)", g["msg"])
        if qm:
            fields.update({"qname": qm.group("qname"), "qclass": qm.group("qclass"),
                           "qtype": qm.group("qtype")})
        return self._event(level=g["lvl"], message=g["msg"], source=f'named.{g["cat"]}',
                           ts_ms=_dd_mon_yyyy_ts(g["ts"]), fields=fields, raw=line)


# ── Unbound resolver ─────────────────────────────────────────────────────────
#   [1608044190] unbound[7809:0] info: 192.168.2.10 www.google.com. A IN NOERROR 0.068642 0 48
class UnboundAdapter(LogAdapter):
    name = "unbound"
    language = "any"
    _RE = re.compile(
        r"^\[(?P<epoch>\d{9,10})\]\s+unbound\[(?P<pid>\d+):(?P<tid>\d+)\]\s+"
        r"(?P<lvl>debug|info|notice|warning|error|fatal):\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["lvl"], message=g["msg"], source="unbound",
                           ts_ms=float(g["epoch"]) * 1000.0,
                           fields={"pid": int(g["pid"]), "thread": int(g["tid"])},
                           raw=line)


# ── CoreDNS log plugin ───────────────────────────────────────────────────────
#   [INFO] [::1]:50759 - 29008 "A IN example.org. udp 41 false 4096" NOERROR qr,rd,ra,ad 68 0.037990251s
#   [ERROR] plugin/errors: 2 example.org. A: unreachable backend
class CoreDnsAdapter(LogAdapter):
    name = "coredns"
    language = "go"
    _QUERY = re.compile(
        r"^\[(?P<lvl>DEBUG|INFO|WARNING|ERROR|FATAL)\]\s+(?P<client>\S+)\s+-\s+"
        r"(?P<qid>\d+)\s+\"(?P<q>[^\"]*)\"\s+(?P<rcode>[A-Z]+)\s+(?P<flags>\S+)\s+"
        r"(?P<size>\d+)\s+(?P<dur>[\d.]+s)\s*$")
    _PLUGIN = re.compile(
        r"^\[(?P<lvl>DEBUG|INFO|WARNING|ERROR|FATAL)\]\s+plugin/(?P<plug>[\w.\-]+):\s+"
        r"(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines,
            lambda ln: bool(self._QUERY.match(ln.strip()) or self._PLUGIN.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._QUERY.match(s)
        if m:
            g = m.groupdict()
            level = g["lvl"]
            if g["rcode"] not in ("NOERROR", "NXDOMAIN") and level == "INFO":
                level = "WARNING"
            return self._event(level=level, message=f'{g["q"]} → {g["rcode"]}',
                               source="coredns",
                               fields={"client": g["client"], "query": g["q"],
                                       "rcode": g["rcode"], "flags": g["flags"],
                                       "size": int(g["size"]), "duration": g["dur"]},
                               raw=line)
        m = self._PLUGIN.match(s)
        if m:
            g = m.groupdict()
            return self._event(level=g["lvl"], message=g["msg"],
                               source=f'coredns.plugin.{g["plug"]}', raw=line)
        return None


# ── Squid native access.log ──────────────────────────────────────────────────
#   1525344856.899  16867 10.170.72.111 TCP_TUNNEL/200 6256 CONNECT logs.… - HIER_DIRECT/… -
class SquidAccessAdapter(LogAdapter):
    name = "squid_access"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{9,10}\.\d{3})\s+(?P<dur>\d+)\s+(?P<client>\S+)\s+"
        r"(?P<result>[A-Z_]+)/(?P<status>\d{3})\s+(?P<bytes>\d+)\s+"
        r"(?P<method>[A-Z_]+)\s+(?P<url>\S+)\s+(?P<user>\S+)\s+(?P<hier>\S+/\S+)")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        status = int(g["status"])
        level = ("error" if status >= 500 else "warn"
                 if status >= 400 or "DENIED" in g["result"] else "info")
        return self._event(level=level,
                           message=f'{g["method"]} {g["url"]} → {g["result"]}/{status}',
                           source="squid", ts_ms=float(g["ts"]) * 1000.0,
                           fields={"client": g["client"], "result": g["result"],
                                   "status": status, "bytes": int(g["bytes"]),
                                   "method": g["method"], "url": g["url"],
                                   "duration_ms": int(g["dur"]), "hierarchy": g["hier"]},
                           raw=line)


# ── Squid cache.log ──────────────────────────────────────────────────────────
#   2023/10/10 02:30:10 kid1| Starting Squid Cache version 4.10 for x86_64…
class SquidCacheAdapter(LogAdapter):
    name = "squid_cache"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?\s+"
        r"kid(?P<kid>\d+)\|\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        low = g["msg"].lower()
        level = ("error" if ("error" in low or "fatal" in low) else
                 "warn" if "warning" in low else "info")
        return self._event(level=level, message=g["msg"], source=f'squid.kid{g["kid"]}',
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"kid": int(g["kid"])}, raw=line)


# ── OpenVPN log ──────────────────────────────────────────────────────────────
#   Fri Dec 13 10:19:19 2019 TLS: Initial packet from [AF_INET]203.0.113.5:51044, sid=…
class OpenVpnAdapter(LogAdapter):
    name = "openvpn"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2} [A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2} \d{4})\s+"
        r"(?P<msg>.*)$")
    # ctime-prefixed lines are common → require an OpenVPN marker in the body.
    _MARK = re.compile(
        r"\b(?:TLS|VERIFY|OpenVPN|MANAGEMENT|AUTH|PUSH|SIGUSR1|SIGTERM|"
        r"Initialization Sequence|Data Channel|Control Channel|tun/tap|TUN/TAP|"
        r"peer info|link remote|LZO|cipher)\b|\[AF_INET6?\]")

    def detect(self, sample_lines):
        def hit(ln):
            m = self._RE.match(str(ln).strip())
            return bool(m and self._MARK.search(m.group("msg")))
        return multiline_ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"]
        low = msg.lower()
        level = ("error" if ("error" in low or "fatal" in low or "cannot" in low
                             or "failed" in low) else
                 "warn" if ("warning" in low or low.startswith("note:")) else "info")
        # ctime carries no ts parseable by the ISO paths, but parse_timestamp
        # handles "Mon DD HH:MM:SS YYYY" after the weekday is included
        return self._event(level=level, message=msg, source="openvpn",
                           ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── Cisco IOS-XR syslog ──────────────────────────────────────────────────────
#   RP/0/RSP0/CPU0:Mar 26 22:04:26.104 : ifmgr[240]: %PKT_INFRA-LINK-3-UPDOWN : Interface …
class CiscoIosXrAdapter(LogAdapter):
    name = "cisco_iosxr"
    language = "any"
    _RE = re.compile(
        r"^(?P<node>(?:RP|LC|DRP|SP)/\d+/[\w]+/CPU\d+):"
        r"(?P<ts>[A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)(?:\s+[A-Z]{3,4})?\s*:\s+"
        r"(?P<proc>[\w\-]+)\[(?P<pid>\d+)\]:\s+"
        r"%(?P<fac>[A-Z][A-Z0-9_]*)-(?:(?P<sub>[A-Z][A-Z0-9_]*)-)?(?P<sev>[0-7])-"
        r"(?P<mnem>[A-Z][A-Z0-9_]*)\s*:\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=_SYSLOG_SEVERITY.get(int(g["sev"]), "INFO"),
                           message=g["msg"], source=f'cisco.{g["fac"].lower()}',
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"node": g["node"], "process": g["proc"],
                                   "pid": int(g["pid"]), "facility": g["fac"],
                                   "subfacility": g.get("sub"),
                                   "severity": int(g["sev"]), "mnemonic": g["mnem"]},
                           raw=line)


# ── MikroTik RouterOS topics log ─────────────────────────────────────────────
#   jan/02/2026 12:11:07 system,info,account user admin logged in from 192.168.88.254 via winbox
class MikrotikAdapter(LogAdapter):
    name = "mikrotik_routeros"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>[a-z]{3}/\d{2}/\d{4} \d{2}:\d{2}:\d{2})\s+"
        r"(?P<topics>[\w\-]+(?:,[\w\-]+)+)\s+(?P<msg>.*)$")
    # BATCH-6 gap fix — the same topic-list grammar also arrives (a) behind a
    # syslog RFC3164 header when shipped to a remote collector, and (b) with a
    # bare HH:MM:SS prefix in the local `/log print` output.
    _RE_SYSLOG = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(?:(?P<host>\S+)\s+)?"
        r"(?P<topics>[a-z][\w\-]*(?:,[\w\-]+)+)\s+(?P<msg>.*)$")
    _RE_TIME = re.compile(
        r"^(?P<ts>\d{2}:\d{2}:\d{2})\s+"
        r"(?P<topics>[a-z][\w\-]*(?:,[\w\-]+)+)\s+(?P<msg>.*)$")
    _LVL_TOPICS = {"critical": "fatal", "error": "error", "warning": "warn",
                   "info": "info", "debug": "debug"}

    def _match(self, s: str):
        return self._RE.match(s) or self._RE_SYSLOG.match(s) or self._RE_TIME.match(s)

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._match(ln.strip())))

    @staticmethod
    def _ts(text: str) -> Optional[float]:
        m = re.match(r"([a-z]{3})/(\d{2})/(\d{4}) (\d{2}):(\d{2}):(\d{2})", text or "")
        if not m:
            return None
        mon = m.group(1).capitalize()
        if mon not in _MONTHS:
            return None
        try:
            return _to_ms(datetime(int(m.group(3)), _MONTHS[mon], int(m.group(2)),
                                   int(m.group(4)), int(m.group(5)), int(m.group(6))))
        except ValueError:
            return None

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        topics = g["topics"].split(",")
        level = next((self._LVL_TOPICS[t] for t in topics if t in self._LVL_TOPICS), "")
        fields = {"topics": g["topics"]}
        if g.get("host"):
            fields["host"] = g["host"]
        return self._event(level=level, message=g["msg"],
                           source=f'routeros.{topics[0]}',
                           ts_ms=self._ts(g["ts"]) or parse_timestamp(g["ts"]),
                           fields=fields, raw=line)


# ── F5 BIG-IP LTM syslog body ────────────────────────────────────────────────
#   err tmm[11661]: 01010028:3: No members available for pool /Common/my_pool
class F5BigIpAdapter(LogAdapter):
    name = "f5_bigip_ltm"
    language = "any"
    _RE = re.compile(
        r"^(?P<lvl>emerg|alert|crit|err|error|warning|notice|info|debug)\s+"
        r"(?P<proc>[\w\-]+)\[(?P<pid>\d+)\]:\s+"
        r"(?P<code>[0-9A-Fa-f]{8}):(?P<sev>\d{1,2}):\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["lvl"], message=g["msg"], source=f'bigip.{g["proc"]}',
                           fields={"pid": int(g["pid"]), "msg_code": g["code"],
                                   "severity": int(g["sev"])}, raw=line)


# ── Citrix NetScaler / ADC native syslog ─────────────────────────────────────
#   Jun 22 19:14:37 <local0.info> 81.2.69.144 06/22/2015:19:14:37 GMT ns 0-PPE-1 : default APPFW …
class CitrixNetscalerAdapter(LogAdapter):
    name = "citrix_netscaler"
    language = "any"
    _RE = re.compile(
        r"^(?:[A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2}\s+)?"
        r"<(?P<fac>[\w]+)\.(?P<sev>[\w]+)>\s+(?P<host>\S+)\s+"
        r"(?P<ts>\d{2}/\d{2}/\d{4}:\d{2}:\d{2}:\d{2})\s+GMT\s+"
        r"(?P<dev>\S+)\s+(?P<ppe>\d+-PPE-\d+)\s*:\s*(?P<msg>.*)$")
    _SEV = {"emerg": "fatal", "alert": "fatal", "crit": "fatal", "err": "error",
            "error": "error", "warning": "warn", "warn": "warn",
            "notice": "info", "info": "info", "debug": "debug"}

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    @staticmethod
    def _ts(text: str) -> Optional[float]:
        m = re.match(r"(\d{2})/(\d{2})/(\d{4}):(\d{2}):(\d{2}):(\d{2})", text or "")
        if not m:
            return None
        from datetime import timezone
        mo, dy, yr, hh, mm, ss = (int(x) for x in m.groups())
        try:                                   # the stamp is explicitly GMT
            return datetime(yr, mo, dy, hh, mm, ss,
                            tzinfo=timezone.utc).timestamp() * 1000.0
        except ValueError:
            return None

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=self._SEV.get(g["sev"].lower(), g["sev"]),
                           message=g["msg"], source=f'netscaler.{g["dev"]}',
                           ts_ms=self._ts(g["ts"]),
                           fields={"host": g["host"], "ppe": g["ppe"],
                                   "facility": g["fac"]}, raw=line)


# ── Registration ─────────────────────────────────────────────────────────────
# cisco_iosxr shares the %FAC-…-SEV-MNEM grammar with cisco_ios (batch 1) →
# ── FRRouting daemon logs (bgpd/ospfd/zebra/…) — BATCH 4 ─────────────────────
#   2020/03/23 15:29:01.283 BGP: %ADJCHANGE: neighbor 10.10.10.2(Unknown) … Up
#   2023/01/01 12:00:00.123 BGP: [VCGF0-X62M1][EC 33554454] message
class FrrAdapter(LogAdapter):
    name = "frr"
    language = "any"
    _DAEMONS = ("BGP", "OSPF", "OSPF6", "ZEBRA", "ISIS", "PIM", "PIM6", "BFD",
                "STATIC", "MGMTD", "WATCHFRR", "RIP", "RIPNG", "BABEL", "EIGRP",
                "NHRP", "PBR", "LDP", "VRRP", "PATH", "SHARP", "FABRICD")
    _RE = re.compile(
        r"^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)\s+"
        r"(?P<daemon>" + "|".join(_DAEMONS) + r"):\s+"
        r"(?:\[(?P<uid>[A-Z0-9]{5}-[A-Z0-9]{5})\]\s*)?"
        r"(?:\[EC (?P<ec>\d+)\]\s*)?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {"daemon": g["daemon"]}
        if g.get("uid"):
            fields["frr_uid"] = g["uid"]
        level = "info"
        if g.get("ec"):
            fields["error_code"] = int(g["ec"])
            level = "warn"
        low = g["msg"].lower()
        if "error" in low or "failed" in low:
            level = "error"
        return self._event(level=level, message=g["msg"],
                           source=f"frr.{g['daemon'].lower()}",
                           ts_ms=parse_timestamp(g["ts"]), fields=fields,
                           raw=line)


# ── Huawei VRP syslog (%%01MODULE/SEV/BRIEF(l)[n]:) — BATCH 4 ─────────────────
#   Aug 16 2015 10:56:41 HUAWEI %%01IFNET/4/IF_STATE(l)[42]:Interface … UP …
class HuaweiVrpAdapter(LogAdapter):
    name = "huawei_vrp"
    language = "any"
    _RE = re.compile(
        r"^(?:(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{4}\s+\d{2}:\d{2}:\d{2})(?:[.+]\d+)?\s+"
        r"(?P<host>\S+)\s+)?%%(?P<ver>\d{2})"
        r"(?P<module>[A-Za-z0-9_]+)/(?P<sev>\d)/(?P<brief>[A-Za-z0-9_]+)"
        r"\((?P<typ>[lst])\)(?:\[(?P<cnt>\d+)\])?:\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        sev = int(g["sev"])
        fields = {"module": g["module"], "brief": g["brief"],
                  "log_type": g["typ"], "severity": sev}
        if g.get("host"):
            fields["host"] = g["host"]
        if g.get("cnt"):
            fields["counter"] = int(g["cnt"])
        from ._common import bsd_year_ts
        return self._event(level=_SYSLOG_SEVERITY.get(sev, "INFO"),
                           message=g["msg"],
                           source=f"huawei.{g['module'].lower()}",
                           ts_ms=bsd_year_ts(g["ts"]) if g.get("ts") else None,
                           fields=fields, raw=line)


# ═══════════════════════════ BATCH 5 ═════════════════════════════════════════

# ── net-snmp snmptrapd log ─────────────────────────────────────────────────────
#   2026-07-20 12:11:07 <UNKNOWN> [UDP: [192.0.2.1]:57620->[192.0.2.10]:162]:
#   DISMAN-EVENT-MIB::sysUpTimeInstance = Timeticks: (2544) … snmpTrapOID.0 = OID: …
class SnmptrapdAdapter(LogAdapter):
    name = "snmptrapd"
    language = "any"
    _ANCHOR = re.compile(
        r"snmpTrapOID\.0 = OID:|sysUpTimeInstance = Timeticks:|"
        r"\[UDP:\s*\[[\d.a-fA-F:]+\]:\d+->\[[\d.a-fA-F:]+\]:\d+\]")
    _TS = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(?P<rest>.*)$")
    _PEER = re.compile(r"\[UDP:\s*\[(?P<src>[\d.a-fA-F:]+)\]:(?P<sport>\d+)->"
                       r"\[(?P<dst>[\d.a-fA-F:]+)\]:(?P<dport>\d+)\]")
    _TRAP = re.compile(r"snmpTrapOID\.0 = OID:\s*(?P<oid>\S+)")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._ANCHOR.search(str(ln))))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if not self._ANCHOR.search(s):
            return None
        ts_ms = None
        tm = self._TS.match(s)
        if tm:
            ts_ms = parse_timestamp(tm.group("ts"))
        fields = {}
        pm = self._PEER.search(s)
        if pm:
            fields["agent"] = f'{pm.group("src")}:{pm.group("sport")}'
        om = self._TRAP.search(s)
        trap = om.group("oid") if om else None
        if trap:
            fields["trap_oid"] = trap
        level = ("warn" if re.search(r"linkDown|authenticationFailure|coldStart",
                                     trap or s) else "info")
        return self._event(level=level,
                           message=f'trap {trap}' if trap else "snmp trap",
                           source="snmptrapd", ts_ms=ts_ms, category="event",
                           fields=fields, raw=line)


# ── tailscaled log ─────────────────────────────────────────────────────────────
#   2023/01/15 12:00:00 wgengine: Reconfig: configuring userspace WireGuard config
class TailscaledAdapter(LogAdapter):
    name = "tailscaled"
    language = "go"
    _COMPONENTS = ("wgengine", "magicsock", "control", "controlclient", "netmap",
                   "monitor", "ipnlocal", "ipnserver", "portmapper", "tka",
                   "health", "derphttp", "derp", "dns", "router", "peerapi",
                   "taildrop", "netcheck", "logtail", "tsnet", "natc",
                   "logpolicy", "ssh-server")
    _RE = re.compile(
        r"^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\s+"
        r"(?P<comp>[\w.\-]+)(?P<sep>[:(])\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        def ok(ln):
            m = self._RE.match(ln.strip())
            if not m:
                return False
            comp = m.group("comp")
            return (comp in self._COMPONENTS
                    or any(comp.startswith(c + ".") for c in self._COMPONENTS)
                    or comp.startswith("health("))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"] if g["sep"] == ":" else f'({g["msg"]}'
        low = msg.lower()
        level = ("error" if "error" in low or "failed" in low
                 else "warn" if "warning" in low or "retrying" in low else "info")
        return self._event(level=level, message=msg,
                           source=f'tailscaled.{g["comp"]}',
                           ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── NetApp ONTAP `event log show` ─────────────────────────────────────────────
#   2/18/2025 23:16:31  NODE_001   ERROR   secd.dns.srv.lookup.failed: DNS server …
class NetappEmsAdapter(LogAdapter):
    name = "netapp_ems"
    language = "any"
    _RE = re.compile(
        r"^(?P<date>\d{1,2}/\d{1,2}/\d{4})\s+(?P<time>\d{2}:\d{2}:\d{2})\s+"
        r"(?P<node>\S+)\s+(?P<sev>NOTICE|INFORMATIONAL|ERROR|ALERT|EMERGENCY|DEBUG)\s+"
        r"(?P<event>[\w.]+\.[\w.]+):\s*(?P<msg>.*)$")
    _LVL = {"NOTICE": "info", "INFORMATIONAL": "info", "ERROR": "error",
            "ALERT": "fatal", "EMERGENCY": "fatal", "DEBUG": "debug"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=self._LVL.get(g["sev"], g["sev"]),
                           message=g["msg"], source=g["event"],
                           ts_ms=us_date_ts(g["date"], g["time"]),
                           fields={"node": g["node"], "event": g["event"]},
                           raw=line)


# ── Registration ─────────────────────────────────────────────────────────────
# insert before it so the node-prefixed XR form wins the tie. citrix starts
# with a syslog-3164 prefix → before systemd.
register_adapter(CiscoIosXrAdapter(), before="cisco_ios")
register_adapter(CitrixNetscalerAdapter(), before="systemd")
for _a in (BindNamedAdapter(), UnboundAdapter(), CoreDnsAdapter(),
           SquidAccessAdapter(), SquidCacheAdapter(), OpenVpnAdapter(),
           MikrotikAdapter(), F5BigIpAdapter(),
           # batch 4 — routing daemons + telco gear
           FrrAdapter(), HuaweiVrpAdapter(),
           # batch 5
           SnmptrapdAdapter(), TailscaledAdapter(), NetappEmsAdapter()):
    register_adapter(_a)


# ═════════════════════════════════════════════════════════════════════════════
# BATCH 6 — DNS/DHCP servers, VRRP, embedded syslogd, RADIUS
# ═════════════════════════════════════════════════════════════════════════════
from ._common import split_any, mk_ts  # noqa: E402


# ── PowerDNS (authoritative + recursor) text log ─────────────────────────────
#   Remote 127.0.0.1 wants 'example.com|A', do = 0, bufsize = 512: packetcache MISS
#   pdns_recursor[12056]: 2 [395002/1] question for 'www.example.com|A' from 10.11.12.13:56765
class PowerDnsAdapter(LogAdapter):
    name = "powerdns"
    language = "any"
    _AUTH = re.compile(
        r"^(?:.*?pdns(?:_server)?\[\d+\]:\s+)?"
        r"Remote (?P<ip>[0-9a-fA-F.:]+) wants '(?P<q>[^']+)', do = (?P<do>[01]), "
        r"bufsize = (?P<buf>\d+)")
    _REC = re.compile(
        r"^(?:.*?pdns_recursor\[\d+\]:\s+)?(?P<tid>\d+)\s+\[(?P<mthread>[\d/]+)\]\s+"
        r"question for '(?P<q>[^']+)' from (?P<from>\S+)")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines,
            lambda ln: bool(self._AUTH.match(ln.strip()) or self._REC.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._AUTH.match(s)
        if m:
            g = m.groupdict()
            qname, _, qtype = g["q"].partition("|")
            return self._event(level="info", message=s, source="pdns_server",
                               fields={"remote": g["ip"], "qname": qname,
                                       "qtype": qtype, "dnssec_do": g["do"] == "1",
                                       "bufsize": int(g["buf"])}, raw=line)
        m = self._REC.match(s)
        if m:
            g = m.groupdict()
            qname, _, qtype = g["q"].partition("|")
            return self._event(level="info", message=s, source="pdns_recursor",
                               fields={"qname": qname, "qtype": qtype,
                                       "from": g["from"], "mthread": g["mthread"]},
                               raw=line)
        return None


# ── ISC Kea DHCP server log (kea-dhcp4 / kea-dhcp6) ──────────────────────────
#   INFO  [kea-dhcp6.leases/182198.139733433005760] DHCP6_LEASE_ADVERT duid=[…]…
class KeaDhcpAdapter(LogAdapter):
    name = "kea_dhcp"
    language = "any"
    _RE = re.compile(
        r"^(?:(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+)?"
        r"(?P<level>DEBUG|INFO|WARN|ERROR|FATAL)\s+"
        r"\[(?P<logger>kea-dhcp[46][\w.\-]*)/(?P<pidthread>[\d.]+)\]\s+"
        r"(?P<msgid>[A-Z][A-Z0-9_]+)\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=f'{g["msgid"]} {g["msg"]}'.strip(),
                           source=g["logger"],
                           ts_ms=parse_timestamp(g["ts"]) if g["ts"] else None,
                           fields={"message_id": g["msgid"]}, raw=line)


# ── keepalived (VRRP + healthcheckers) syslog body ────────────────────────────
#   Keepalived_vrrp[1234]: VRRP_Instance(VI_1) Entering MASTER STATE
class KeepalivedAdapter(LogAdapter):
    name = "keepalived"
    language = "any"
    _RE = re.compile(
        r"^(?:(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(?P<host>\S+)\s+)?"
        r"(?P<proc>Keepalived(?:_(?:vrrp|healthcheckers))?)\[(?P<pid>\d+)\]:\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"]
        level = "info"
        if "FAULT" in msg or "Error" in msg or "failed" in msg.lower():
            level = "warn" if "FAULT" in msg else "error"
        fields = {"pid": int(g["pid"])}
        im = re.search(r"VRRP_Instance\((?P<inst>[^)]+)\)", msg)
        if im:
            fields["instance"] = im.group("inst")
        sm = re.search(r"(?:Entering|Transition to)\s+(MASTER|BACKUP|FAULT)\s+STATE", msg)
        if sm:
            fields["state"] = sm.group(1)
        return self._event(level=level, message=msg, source=g["proc"],
                           ts_ms=parse_timestamp(g["ts"]) if g.get("ts") else None,
                           fields=fields, raw=line)


# ── BusyBox syslogd / OpenWrt logread (facility.priority token) ──────────────
#   Jul 20 14:03:11 hostname user.info kernel: eth0: link up
#   Sun Jul 20 14:03:11 2026 kern.warn kernel: [ 1234.5] br-lan: …
class BusyboxSyslogAdapter(LogAdapter):
    name = "busybox_syslog"
    language = "any"
    _FAC = (r"kern|user|mail|daemon|auth|syslog|lpr|news|uucp|cron|authpriv|ftp|"
            r"local[0-7]")
    _PRI = r"emerg|alert|crit|err|error|warn|warning|notice|info|debug"
    _RE3164 = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(?P<host>\S+)\s+"
        r"(?P<fac>(?:" + _FAC + r"))\.(?P<pri>(?:" + _PRI + r"))\s+"
        r"(?P<tag>[\w./\-]+)(?:\[(?P<pid>\d+)\])?:\s?(?P<msg>.*)$")
    _RECTIME = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+"
        r"(?P<fac>(?:" + _FAC + r"))\.(?P<pri>(?:" + _PRI + r"))\s+"
        r"(?P<tag>[\w./\-]+)(?:\[(?P<pid>\d+)\])?:\s?(?P<msg>.*)$")

    def _match(self, s: str):
        return self._RE3164.match(s) or self._RECTIME.match(s)

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._match(ln.strip())))

    @staticmethod
    def _ctime_ts(text: str):
        m = re.match(r"[A-Z][a-z]{2}\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+"
                     r"(\d{2}):(\d{2}):(\d{2})\s+(\d{4})", text or "")
        if m and m.group(1) in _MONTHS:
            mon, dy, hh, mi, ss, yr = m.groups()
            return mk_ts(yr, _MONTHS[mon], dy, hh, mi, ss)
        return parse_timestamp(text or "")

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        level = {"emerg": "fatal", "alert": "fatal", "crit": "fatal",
                 "err": "error", "error": "error", "warn": "warn",
                 "warning": "warn", "notice": "info", "info": "info",
                 "debug": "debug"}.get(g["pri"], "")
        fields = {"facility": g["fac"], "priority": g["pri"]}
        if g.get("host"):
            fields["host"] = g["host"]
        if g.get("pid"):
            fields["pid"] = int(g["pid"])
        return self._event(level=level, message=g["msg"], source=g["tag"],
                           ts_ms=self._ctime_ts(g["ts"]), fields=fields, raw=line)


# ── FreeRADIUS radius.log ─────────────────────────────────────────────────────
#   Wed Oct 30 10:18:08 2019 : Auth: (0) Login OK: [bob] (from client host1 port 0)
class FreeRadiusAdapter(LogAdapter):
    name = "freeradius"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+:\s+"
        r"(?P<cat>Auth|Info|Error|Proxy|Acct|Debug|Warning)\s*:\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"]
        level = {"Error": "error", "Warning": "warn"}.get(g["cat"], "info")
        if "Login incorrect" in msg or "Invalid user" in msg:
            level = "warn"
        fields = {"category": g["cat"]}
        um = re.search(r"\[([^\]/]+)(?:/[^\]]*)?\]", msg)
        if um:
            fields["user"] = um.group(1)
        return self._event(level=level, message=msg, source="freeradius",
                           ts_ms=BusyboxSyslogAdapter._ctime_ts(g["ts"]),
                           fields=fields, raw=line)


# ── dnsmasq DHCP leases file (/var/lib/misc/dnsmasq.leases) ──────────────────
#   1108086503 00:b0:d0:01:32:86 142.174.150.208 M61480 01:00:b0:d0:01:32:86
class DnsmasqLeasesAdapter(LogAdapter):
    name = "dnsmasq_leases"
    language = "any"
    _RE = re.compile(
        r"^(?P<exp>\d{9,10})\s+(?P<mac>(?:[0-9a-f]{2}:){5}[0-9a-f]{2})\s+"
        r"(?P<ip>\d{1,3}(?:\.\d{1,3}){3})\s+(?P<host>\S+)\s+(?P<cid>\S+)\s*$",
        re.IGNORECASE)
    _DUID = re.compile(r"^duid\s+[0-9a-f:]+\s*$", re.IGNORECASE)

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines,
            lambda ln: bool(self._RE.match(ln.strip()) or self._DUID.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if self._DUID.match(s):
            return self._event(level="info", message=s, source="dnsmasq.leases",
                               raw=line)
        m = self._RE.match(s)
        if not m:
            return None
        g = m.groupdict()
        return self._event(level="info",
                           message=f'lease {g["ip"]} -> {g["mac"]}'
                                   + (f' ({g["host"]})' if g["host"] != "*" else ""),
                           source="dnsmasq.leases",
                           ts_ms=float(g["exp"]) * 1000.0,   # lease EXPIRY time
                           fields={"ip": g["ip"], "mac": g["mac"],
                                   "hostname": None if g["host"] == "*" else g["host"],
                                   "client_id": None if g["cid"] == "*" else g["cid"],
                                   "expires": int(g["exp"])}, raw=line)


# ── Batch-6 registration ──────────────────────────────────────────────────────
# busybox_syslog wraps a facility.priority token inside an otherwise plain
# RFC3164 header → register before the core `syslog` adapter so a 1.0 tie goes
# to the more specific grammar. keepalived may also arrive behind that header.
register_adapter(BusyboxSyslogAdapter(), before="syslog")
register_adapter(KeepalivedAdapter(), before="syslog")
for _a in (PowerDnsAdapter(), KeaDhcpAdapter(), FreeRadiusAdapter(),
           DnsmasqLeasesAdapter()):
    register_adapter(_a)


# ═════════════════════════════════════════════════════════════════════════════
#  BATCH 7 — routing daemons, flow-tool prints, home-lab network daemons
# ═════════════════════════════════════════════════════════════════════════════
from ._common import mk_ts as _mk_ts  # noqa: E402


# ── BIRD Internet Routing Daemon log ──────────────────────────────────────────
#   2019-06-19 17:47:03.822 <TRACE> bgp1: Connect delayed by 5 seconds
class BirdAdapter(LogAdapter):
    name = "bird"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+"
        r"<(?P<lvl>TRACE|DEBUG|INFO|WARN|ERR|FATAL|DBG|BUG|AUTH|RMT)>\s+"
        r"(?P<proto>[\w.\-]+):\s*(?P<msg>.*)$")
    _LVL = {"TRACE": "trace", "DBG": "debug", "DEBUG": "debug", "INFO": "info",
            "RMT": "info", "AUTH": "warn", "WARN": "warn", "ERR": "error",
            "BUG": "fatal", "FATAL": "fatal"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=self._LVL.get(g["lvl"], "info"), message=g["msg"],
                           source=f'bird.{g["proto"]}',
                           ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── nfdump default flow print ─────────────────────────────────────────────────
#   2026-07-20 12:11:07.123    0.000 UDP    10.0.0.1:53246 ->  8.8.8.8:53   1  76  1
class NfdumpAdapter(LogAdapter):
    name = "nfdump"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+"
        r"(?P<dur>[\d.]+)\s+(?P<proto>[A-Z][A-Z\d.\-]{1,8})\s+"
        r"(?P<src>[\da-fA-F.:]+):(?P<sport>\d+)\s+->\s+"
        r"(?P<dst>[\da-fA-F.:]+):(?P<dport>\d+)\s+(?P<rest>[\d\s.MGK]+)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        nums = g["rest"].split()
        fields = {"protocol": g["proto"], "duration_s": float(g["dur"]),
                  "src": f'{g["src"]}:{g["sport"]}', "dst": f'{g["dst"]}:{g["dport"]}'}
        for k, v in zip(("packets", "bytes", "flows"), nums):
            fields[k] = v
        return self._event(level="info",
                           message=f'{g["proto"]} {fields["src"]} -> {fields["dst"]}',
                           source="nfdump", ts_ms=parse_timestamp(g["ts"]),
                           fields=fields, category="event", raw=line)


# ── sflowtool -l line/CSV output ──────────────────────────────────────────────
#   FLOW,10.0.0.254,0,0,00902773db08,001083265e00,0x0800,0,0,10.0.0.1,10.0.0.254,17,…
class SflowtoolAdapter(LogAdapter):
    name = "sflowtool"
    language = "any"
    _RE = re.compile(r"^(?P<kind>FLOW|CNTR),(?P<agent>(?:\d{1,3}\.){3}\d{1,3}),")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        f = s.split(",")
        fields = {"kind": f[0], "agent": f[1]}
        if f[0] == "FLOW" and len(f) >= 16:
            fields.update({"src_mac": f[4], "dst_mac": f[5], "ethertype": f[6],
                           "src_ip": f[9], "dst_ip": f[10], "ip_proto": f[11],
                           "src_port": f[14], "dst_port": f[15]})
            msg = f'FLOW {f[9]}:{f[14]} -> {f[10]}:{f[15]} proto {f[11]}'
        else:
            msg = f'{f[0]} agent {f[1]}'
        return self._event(level="info", message=msg, source="sflowtool",
                           fields=fields, category="event", raw=line)


# ── SONiC NOS syslog (container#program token) ────────────────────────────────
#   Aug 17 01:20:14.903331 sonic INFO swss#orchagent: :- setPortAdminStatus: …
class SonicNosAdapter(LogAdapter):
    name = "sonic_nos"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\.\d{3,6})\s+"
        r"(?P<host>\S+)\s+(?P<lvl>DEBUG|INFO|NOTICE|WARNING|ERR|ERROR|CRIT|ALERT|EMERG)\s+"
        r"(?P<prog>[\w\-]+#[\w.\-]+):\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        container, _, prog = g["prog"].partition("#")
        return self._event(level=g["lvl"], message=g["msg"], source=g["prog"],
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"host": g["host"], "container": container,
                                   "program": prog}, raw=line)


# ── Pi-hole FTL daemon log ────────────────────────────────────────────────────
#   [2024-01-14 12:32:52.584 1234M] INFO: FTL is running as user pihole (UID 999)
class PiholeFtlAdapter(LogAdapter):
    name = "pihole_ftl"
    language = "any"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) (?P<pid>\d+)"
        r"(?P<thr>[A-Za-z/\d]*)\]\s+(?P<lvl>INFO|WARNING|ERR|ERROR|CRIT|DEBUG\w*):\s*"
        r"(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        lvl = "debug" if g["lvl"].startswith("DEBUG") else g["lvl"]
        return self._event(level=lvl, message=g["msg"], source="pihole.ftl",
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"pid": int(g["pid"]),
                                   "thread": g["thr"] or None}, raw=line)


# ── stunnel log ───────────────────────────────────────────────────────────────
#   2023.01.15 12:00:00 LOG5[0]: Service [https] accepted connection from 203.0.113.5:50624
class StunnelAdapter(LogAdapter):
    name = "stunnel"
    language = "any"
    _RE = re.compile(
        r"^(?P<yr>\d{4})\.(?P<mo>\d{2})\.(?P<dy>\d{2}) "
        r"(?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2}) "
        r"LOG(?P<sev>\d)\[(?P<tid>[^\]]+)\]:\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        sev = int(g["sev"])
        fields = {"tid": g["tid"]}
        sm = re.match(r"^Service \[([^\]]+)\]", g["msg"])
        if sm:
            fields["service"] = sm.group(1)
        return self._event(level=_SYSLOG_SEVERITY.get(sev, "INFO"),
                           message=g["msg"], source="stunnel",
                           ts_ms=_mk_ts(g["yr"], g["mo"], g["dy"], g["hh"],
                                        g["mi"], g["ss"]),
                           fields=fields, raw=line)


# ── Kea memfile lease database (kea-leases4.csv / kea-leases6.csv) ────────────
#   address,hwaddr,client_id,valid_lft,expire,subnet_id,fqdn_fwd,fqdn_rev,hostname,…
#   192.168.1.100,aa:bb:cc:dd:ee:ff,01:aa:…,3600,1626778872,1,0,0,laptop,0,,0
class KeaLeasesCsvAdapter(LogAdapter):
    name = "kea_leases_csv"
    language = "any"
    _HDR = re.compile(r"^address,hwaddr,client_id,valid_lft,expire,subnet_id\b")
    _ROW4 = re.compile(
        r"^(?P<ip>(?:\d{1,3}\.){3}\d{1,3}),(?P<mac>[0-9a-fA-F:]{11,29}),"
        r"(?P<cid>[0-9a-fA-F:]*),(?P<lft>\d+),(?P<exp>\d+),(?P<subnet>\d+),")

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda ln: bool(self._HDR.match(str(ln).strip())
                            or self._ROW4.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if self._HDR.match(s):
            return self._event(level="info", message="Kea lease file header",
                               source="kea.leases",
                               fields={"columns": s.split(",")}, raw=line)
        m = self._ROW4.match(s)
        if not m:
            return None
        g = m.groupdict()
        cols = s.split(",")
        fields = {"address": g["ip"], "hwaddr": g["mac"],
                  "valid_lft": int(g["lft"]), "subnet_id": int(g["subnet"])}
        if len(cols) > 8 and cols[8]:
            fields["hostname"] = cols[8]
        ts_ms = float(g["exp"]) * 1000.0 if int(g["exp"]) > 1e8 else None
        return self._event(level="info",
                           message=f'lease {g["ip"]} -> {g["mac"]}',
                           source="kea.leases", ts_ms=ts_ms, fields=fields,
                           category="event", raw=line)


# sonic_nos wraps a syslog-style header (fractional-seconds variant the core
# syslog/systemd grammars cannot match) — keep it ahead of them regardless.
register_adapter(SonicNosAdapter(), before="syslog")
for _a in (BirdAdapter(), NfdumpAdapter(), SflowtoolAdapter(), PiholeFtlAdapter(),
           StunnelAdapter(), KeaLeasesCsvAdapter()):
    register_adapter(_a)


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH 8 — vendor switch/router event logs, VPN daemons, DNS query logs
# ══════════════════════════════════════════════════════════════════════════════
from ._common import (RxAdapter, vocab_detect, block_ratio,  # noqa: E402
                      split_any, _MONTHS, _to_ms)
from datetime import datetime as _dt  # noqa: E402


# ── Nokia (Alcatel-Lucent) SR OS event log ────────────────────────────────────
#   528 2013/07/02 09:15:31.42 UTC MINOR: BGP #2005 Base Peer 1: … 
class NokiaSrosAdapter(RxAdapter):
    name = "nokia_sros"
    language = "any"
    default_source = "sros"
    _RE = re.compile(
        r"^(?P<seq>\d+)\s+(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+"
        r"(?P<zone>[A-Z]{2,4})\s+"
        r"(?P<sev>CRITICAL|MAJOR|MINOR|WARNING|INDETERMINATE|CLEARED):\s+"
        r"(?P<app>\w+)\s+#(?P<eid>\d+)\s+(?P<msg>.*)$")
    _LVL = {"CRITICAL": "fatal", "MAJOR": "error", "MINOR": "warn",
            "WARNING": "warn", "INDETERMINATE": "info", "CLEARED": "info"}

    def _ts(self, g):
        return parse_timestamp(g["ts"].replace("/", "-"))

    def _level(self, g, line):
        return self._LVL.get(g["sev"], "info")

    def _fields(self, g, line):
        return {"sequence": int(g["seq"]), "application": g["app"],
                "event_id": int(g["eid"]), "severity": g["sev"]}


# ── H3C / HPE Comware switches & routers ──────────────────────────────────────
#   %Jun 26 17:08:35:809 2013 H3C SHELL/5/SHELL_LOGIN: VTY logged in from …
class H3cComwareAdapter(RxAdapter):
    name = "h3c_comware"
    language = "any"
    default_source = "comware"
    _RE = re.compile(
        r"^%(?P<mon>[A-Z][a-z]{2}) (?P<dy>\d{1,2}) (?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2}):(?P<ms>\d{3}) "
        r"(?P<yr>\d{4}) (?P<host>\S+) (?P<mod>\w+)/(?P<sev>\d)/(?P<mnem>\w+):\s*(?P<msg>.*)$")

    def _ts(self, g):
        if g["mon"] not in _MONTHS:
            return None
        try:
            return _to_ms(_dt(int(g["yr"]), _MONTHS[g["mon"]], int(g["dy"]),
                              int(g["hh"]), int(g["mi"]), int(g["ss"]), int(g["ms"]) * 1000))
        except ValueError:
            return None

    def _level(self, g, line):
        return _SYSLOG_SEVERITY.get(int(g["sev"]), "INFO")

    def _fields(self, g, line):
        return {"host": g["host"], "module": g["mod"], "mnemonic": g["mnem"],
                "severity": int(g["sev"])}


# ── Extreme Networks EXOS event log ───────────────────────────────────────────
#   05/20/2015 14:02:33.85 <Info:AAA.authPass> Login passed for user admin …
class ExtremeExosAdapter(RxAdapter):
    name = "extreme_exos"
    language = "any"
    default_source = "exos"
    _RE = re.compile(
        r"^(?P<mon>\d{2})/(?P<dy>\d{2})/(?P<yr>\d{4}) (?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})\.(?P<cs>\d{1,2})\s+"
        r"<(?P<sev>Info|Noti|Warn|Erro|Crit|Debu|Summ)[^:]*:(?P<comp>[\w.]+)>\s*(?P<msg>.*)$")
    _LVL = {"Debu": "debug", "Info": "info", "Noti": "info", "Summ": "info",
            "Warn": "warn", "Erro": "error", "Crit": "fatal"}

    def _ts(self, g):
        try:
            return _to_ms(_dt(int(g["yr"]), int(g["mon"]), int(g["dy"]),
                              int(g["hh"]), int(g["mi"]), int(g["ss"]),
                              int(g["cs"].ljust(2, "0")) * 10000))
        except ValueError:
            return None

    def _level(self, g, line):
        return self._LVL.get(g["sev"], "info")

    def _fields(self, g, line):
        return {"component": g["comp"]}


# ── NLnet Labs NSD authoritative server ───────────────────────────────────────
#   [2021-05-23 20:20:03.214] nsd[2361906]: info: new control connection …
class NsdLogAdapter(RxAdapter):
    name = "nsd_log"
    language = "any"
    default_source = "nsd"
    _RE = re.compile(
        r"^(?:\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\]\s+)?"
        r"nsd\[(?P<pid>\d+)\]:\s+(?P<level>info|notice|warning|error):\s*(?P<msg>.*)$")

    def _fields(self, g, line):
        return {"pid": int(g["pid"])}


# ── tinc VPN daemon (syslog) ──────────────────────────────────────────────────
#   tinc.mynet[1234]: Connection from 203.0.113.5 port 655
class TincSyslogAdapter(RxAdapter):
    name = "tinc_syslog"
    language = "any"
    default_source = "tinc"
    _RE = re.compile(r"^(?P<prog>tinc(?:\.[\w\-]+)?|tincd)\[(?P<pid>\d+)\]:\s*(?P<msg>.*)$")

    def _level(self, g, line):
        return "error" if re.search(r"error|fail|could not|refused", g.get("msg", ""), re.I) else "info"

    def _fields(self, g, line):
        return {"pid": int(g["pid"]), "netname": g["prog"]}


# ── OpenVPN management-interface real-time notifications ───────────────────────
#   >LOG:1576232359,I,Initialization Sequence Completed
class OpenvpnMgmtAdapter(LogAdapter):
    name = "openvpn_mgmt"
    language = "any"
    _RE = re.compile(r"^>(?P<kind>LOG|STATE|BYTECOUNT|CLIENT|PASSWORD|HOLD|INFO|FATAL|ECHO):(?P<payload>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines, lambda el: bool(self._RE.match(str(el).strip())))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        g = m.groupdict()
        parts = g["payload"].split(",")
        ts_ms = None
        level = ""
        msg = g["payload"]
        if g["kind"] == "LOG" and len(parts) >= 3 and parts[0].isdigit():
            ts_ms = float(parts[0]) * 1000.0
            level = {"I": "info", "W": "warn", "N": "info",
                     "F": "fatal", "D": "debug"}.get(parts[1], "")
            msg = ",".join(parts[2:])
        return self._event(level=level or ("fatal" if g["kind"] == "FATAL" else ""),
                           message=f'{g["kind"]}: {msg}', source="openvpn.mgmt",
                           ts_ms=ts_ms, fields={"notify": g["kind"]},
                           category="event", raw=line)


# ── dnstap text output (verbose + quiet -q) ───────────────────────────────────
#   2020-09-16T18:51:53.547352+00:00 lb1 CLIENT_QUERY NOERROR - - INET UDP 43b …
#   14:23:41.857291 CQ 192.168.0.250 UDP 52b "min.zet.com." IN A
class DnstapTextAdapter(LogAdapter):
    name = "dnstap_text"
    language = "any"
    _MTYPE = ("CLIENT_QUERY", "CLIENT_RESPONSE", "RESOLVER_QUERY",
              "RESOLVER_RESPONSE", "AUTH_QUERY", "AUTH_RESPONSE",
              "FORWARDER_QUERY", "FORWARDER_RESPONSE", "STUB_QUERY",
              "STUB_RESPONSE", "TOOL_QUERY", "TOOL_RESPONSE")
    _VERBOSE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T[\d:.]+(?:[+-]\d{2}:?\d{2}|Z)?)\s+(?P<ident>\S+)\s+"
        r"(?P<mtype>" + "|".join(_MTYPE) + r")\s+")
    _QUIET = re.compile(
        r"^(?P<ts>\d{2}:\d{2}:\d{2}\.\d+)\s+"
        r"(?P<code>CQ|CR|RQ|RR|AQ|AR|FQ|FR|SQ|SR|TQ|TR|UQ|UR)\s+"
        r"(?P<ip>[\d.:a-fA-F]+)\s+(?P<proto>UDP|TCP)\s+")

    def detect(self, sample_lines):
        def hit(x):
            x = x.strip()
            return bool(self._VERBOSE.match(x) or self._QUIET.match(x))
        return ratio_detect(sample_lines, lambda el: block_ratio(el, hit))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._VERBOSE.match(s)
        if m:
            g = m.groupdict()
            return self._event(level="", message=s, source="dnstap",
                               ts_ms=parse_timestamp(g["ts"]),
                               fields={"message_type": g["mtype"], "identity": g["ident"]},
                               category="event", raw=line)
        m = self._QUIET.match(s)
        if m:
            g = m.groupdict()
            return self._event(level="", message=s, source="dnstap",
                               ts_ms=parse_timestamp(g["ts"]),
                               fields={"code": g["code"], "client": g["ip"],
                                       "protocol": g["proto"]},
                               category="event", raw=line)
        return None


# ── dnscrypt-proxy 2 query_log (TSV + LTSV) ───────────────────────────────────
#   [2020-12-15 14:36:30]\t127.0.0.1\twww.ripe.net\tA\tPASS\t1ms\tfaelix-ch-ipv4
#   time:1608044190\thost:127.0.0.1\tmessage:www.ripe.net\ttype:A\treturn:PASS…
class DnscryptQueryAdapter(LogAdapter):
    name = "dnscrypt_query"
    language = "any"
    _RETURNS = {"PASS", "FORWARD", "DROP", "REJECT", "SYNTH", "NOT_READY",
                "PARSE_ERROR", "NXDOMAIN", "RESPONSE_ERROR", "SERVER_ERROR", "CLOAK"}
    _TSV = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\t")
    _LTSV = re.compile(r"(?:^|\t)(time|host|message|type|return|cached|duration|server|relay):")

    def _cells(self, s):
        return s.split("\t")

    def detect(self, sample_lines):
        def hit(x):
            x = x.rstrip()
            if self._TSV.match(x):
                cells = self._cells(x)
                return len(cells) >= 6 and any(c in self._RETURNS for c in cells)
            return len(self._LTSV.findall(x)) >= 4
        return ratio_detect(sample_lines, lambda el: block_ratio(el, hit))

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        if self._TSV.match(s):
            cells = self._cells(s)
            ts = parse_timestamp(self._TSV.match(s).group("ts"))
            ret = next((c for c in cells if c in self._RETURNS), "")
            return self._event(level="warn" if ret in ("DROP", "REJECT") else "info",
                               message=" ".join(cells[1:4]), source="dnscrypt-proxy",
                               ts_ms=ts, fields={"return": ret, "format": "tsv"},
                               category="event", raw=line)
        if len(self._LTSV.findall(s)) >= 4:
            kv = dict(p.split(":", 1) for p in s.split("\t") if ":" in p)
            ts = None
            if kv.get("time", "").isdigit():
                ts = float(kv["time"]) * 1000.0
            return self._event(level="warn" if kv.get("return") in ("DROP", "REJECT") else "info",
                               message=kv.get("message", s), source="dnscrypt-proxy",
                               ts_ms=ts, fields={**kv, "format": "ltsv"},
                               category="event", raw=line)
        return None


def _prog_vocab_adapter(cls_name, adapter_name, prog_re, vocab_re, source, lang="any"):
    """Factory for a bare 'prog[pid]: msg' / 'prog: msg' daemon adapter gated
    on the program token plus a vocabulary anchor."""
    class _A(LogAdapter):
        name = adapter_name
        language = lang
        _PROG = re.compile(prog_re)
        _VOCAB = re.compile(vocab_re, re.I) if vocab_re else None

        def detect(self, sample_lines):
            def hit(x):
                x = x.strip()
                if not self._PROG.match(x):
                    return False
                return self._VOCAB.search(x) is not None if self._VOCAB else True
            return vocab_detect(sample_lines, lambda el: block_ratio(el, hit), cap=0.85)

        def parse_line(self, line):
            s = line.rstrip("\r\n").strip()
            m = self._PROG.match(s)
            if not m:
                return None
            g = m.groupdict()
            msg = g.get("msg", s)
            level = "error" if re.search(r"fail|error|refused|denied|cannot|unable",
                                         msg, re.I) else "info"
            fields = {}
            if g.get("pid"):
                fields["pid"] = int(g["pid"])
            if g.get("iface"):
                fields["interface"] = g["iface"]
            return self._event(level=level, message=msg, source=source,
                               fields=fields or None, raw=line)
    _A.__name__ = cls_name
    return _A


PppdSyslogAdapter = _prog_vocab_adapter(
    "PppdSyslogAdapter", "pppd_syslog",
    r"^pppd(?:\[(?P<pid>\d+)\])?:\s*(?P<msg>.*)$",
    r"CHAP|PAP|authentication|IP address|Connect:|LCP|IPCP|Modem hangup|ppp\d|MPPE",
    "pppd")

DhcpcdAdapter = _prog_vocab_adapter(
    "DhcpcdAdapter", "dhcpcd",
    r"^dhcpcd(?:\[(?P<pid>\d+)\])?:\s*(?:(?P<iface>[\w.\-]+):\s*)?(?P<msg>.*)$",
    r"leased|offered|soliciting|carrier|rebinding|renew|adding route|expired|DHCP",
    "dhcpcd")

OcservAdapter = _prog_vocab_adapter(
    "OcservAdapter", "ocserv",
    r"^ocserv(?:\[(?P<pid>\d+)\])?:\s*(?P<msg>(?:main|worker|sec-mod)(?:\[[^\]]*\])?:.*)$",
    r"main|worker|sec-mod|logged in|connected|disconnected|user",
    "ocserv")

UdhcpcAdapter = _prog_vocab_adapter(
    "UdhcpcAdapter", "udhcpc",
    r"^udhcpc(?:\[(?P<pid>\d+)\])?:\s*(?P<msg>.*)$",
    r"sending discover|sending select|lease of|obtained|renew|no lease|adding",
    "udhcpc")

IscDhcrelayAdapter = _prog_vocab_adapter(
    "IscDhcrelayAdapter", "isc_dhcrelay",
    r"^dhcrelay(?:\[(?P<pid>\d+)\])?:\s*(?P<msg>.*)$",
    r"Forwarded BOOTREQUEST|Forwarded BOOTREPLY|BOOT|relay",
    "dhcrelay")


# ── coturn TURN/STUN server ───────────────────────────────────────────────────
#   1234: session 001…: realm <example.org> user <alice>: incoming packet …
class CoturnAdapter(LogAdapter):
    name = "coturn"
    language = "any"
    _RE = re.compile(r"^(?P<secs>\d+):\s+(?P<msg>.*)$")
    _VOCAB = re.compile(r"session \d+|realm <|allocation|BINDING|ERROR:|"
                        r"tls connected|tcp connected|IPv[46]", re.I)

    def detect(self, sample_lines):
        def hit(x):
            x = x.strip()
            return bool(self._RE.match(x)) and self._VOCAB.search(x) is not None
        return vocab_detect(sample_lines, lambda el: block_ratio(el, hit), cap=0.85)

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        g = m.groupdict()
        level = "error" if "ERROR:" in g["msg"] else "info"
        return self._event(level=level, message=g["msg"], source="coturn",
                           fields={"uptime_s": int(g["secs"])}, raw=line)


register_adapter(NokiaSrosAdapter(), before="syslog")
register_adapter(H3cComwareAdapter(), before="syslog")
for _a in (ExtremeExosAdapter(), NsdLogAdapter(), TincSyslogAdapter(),
           OpenvpnMgmtAdapter(), DnstapTextAdapter(), DnscryptQueryAdapter(),
           PppdSyslogAdapter(), DhcpcdAdapter(), OcservAdapter(),
           UdhcpcAdapter(), IscDhcrelayAdapter(), CoturnAdapter()):
    register_adapter(_a)
