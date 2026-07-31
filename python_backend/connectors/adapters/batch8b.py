"""
BATCH 8b — medium-tier straggler adapters (finish the medium non-binary tail)
================================================================================
A grab-bag of well-anchored medium-priority formats that were still landing on
the structural/generic_ts fallbacks after batch 8's family-grouped additions.
Kept in one module (loaded LAST) so the additions don't disturb any earlier
1.0-confidence `before=` ties — every adapter here anchors on a distinctive
token, so it wins its own sample outright without needing priority placement
(the two that ride a shared silhouette register `before=` explicitly).

Pure standard library; same unified-event contract as every other adapter.
"""
from __future__ import annotations

import re
from datetime import datetime

from ._common import (LogAdapter, register_adapter, parse_timestamp,
                      RxAdapter, vocab_detect, ratio_detect, block_ratio,
                      split_any, mk_ts, _MONTHS, _to_ms, us_date_ts)


# ── Gazebo / Ignition (gz) console log ────────────────────────────────────────
#   [Msg] Loading world file [/usr/share/gazebo-11/worlds/empty.world]
class GazeboAdapter(RxAdapter):
    name = "gazebo"
    language = "cpp"
    default_source = "gazebo"
    _RE = re.compile(r"^(?P<pfx>\S*)\[(?P<lvl>Msg|Wrn|Err|Dbg)\]\s+(?P<msg>.*)$")
    _LVL = {"Msg": "info", "Wrn": "warn", "Err": "error", "Dbg": "debug"}

    def _level(self, g, line):
        return self._LVL.get(g["lvl"], "info")

    def detect(self, sample_lines):
        return vocab_detect(sample_lines,
                            lambda el: block_ratio(el, self._hit), cap=0.9)


# ── HuggingFace Transformers / accelerate seed + logger prefix ────────────────
#   [INFO|trainer.py:1234] 2026-07-20 11:03:22 >> Setting random seed to 42
class HfTransformersAdapter(RxAdapter):
    name = "hf_transformers"
    language = "python"
    default_source = "transformers"
    _RE = re.compile(
        r"^\[(?P<level>INFO|WARNING|WARN|ERROR|DEBUG)\|(?P<file>[\w./]+\.py):(?P<lineno>\d+)\]\s+"
        r"(?:(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+)?(?:>>\s+)?(?P<msg>.*)$")

    def _fields(self, g, line):
        return {"file": g["file"], "line": int(g["lineno"])}


# ── FreePBX framework log (Monolog, bracketed channel) ────────────────────────
#   [2026-01-05 10:26:12] [freepbx.INFO]: Module core has been upgraded …
class FreepbxMonologAdapter(RxAdapter):
    name = "freepbx_monolog"
    language = "php"
    default_source = "freepbx"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+"
        r"\[?(?P<chan>freepbx)\.(?P<level>DEBUG|INFO|NOTICE|WARNING|ERROR|CRITICAL|ALERT|EMERGENCY)\]?\s*:\s*(?P<msg>.*)$")


# ── NSQ daemon logs (nsqd/nsqlookupd/nsqadmin) ────────────────────────────────
#   [nsqd] 2021/03/26 09:36:48.796461 INFO: TCP: listening on [::]:4150
class NsqNsqdAdapter(RxAdapter):
    name = "nsq_nsqd"
    language = "go"
    _RE = re.compile(
        r"^\[(?P<comp>nsqd|nsqlookupd|nsqadmin)\]\s+"
        r"(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d{6})\s+"
        r"(?P<level>DEBUG|INFO|WARNING|ERROR|FATAL):\s*(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            ev["source"] = self._RE.match(line.strip()).group("comp")
        return ev


# ── rspamd spam filter ─────────────────────────────────────────────────────────
#   2026-07-20 09:01:03 #4321(normal) <a1b2c3>; task; rspamd_task_write_log: …
class RspamdAdapter(RxAdapter):
    name = "rspamd"
    language = "any"
    default_source = "rspamd"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
        r"#(?P<pid>\d+)\((?P<worker>\w+)\)\s+<(?P<tag>[0-9a-f]+)>;\s+"
        r"(?P<module>\w+);\s*(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _fields(self, g, line):
        return {"pid": int(g["pid"]), "worker": g["worker"], "module": g["module"]}


# ── rsyslogd internal self-message (origin block) ─────────────────────────────
#   rsyslogd: [origin software="rsyslogd" swVersion="8.2102.0" …] start
class RsyslogdInternalAdapter(LogAdapter):
    name = "rsyslogd_internal"
    language = "any"
    _RE = re.compile(r"^rsyslogd(?:-\d+)?:\s+(?:\[origin |action ['\"])")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: bool(self._RE.match(str(el).strip())))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        if not self._RE.match(s):
            return None
        level = "warn" if re.search(r"suspend|error|fail", s, re.I) else "info"
        return self._event(level=level, message=s.split(": ", 1)[-1],
                           source="rsyslogd", raw=line)


# ── syslog-ng internal() source message ───────────────────────────────────────
#   syslog-ng[2134]: syslog-ng starting up; version='3.38.1'
class SyslogNgInternalAdapter(LogAdapter):
    name = "syslog_ng_internal"
    language = "any"
    _RE = re.compile(r"^syslog-ng\[(?P<pid>\d+)\]:\s+(?P<msg>.*?)(?:;\s*(?P<kv>[\w]+='.*))?$")
    _HASKV = re.compile(r";\s+\w+='[^']*'")

    def detect(self, sample_lines):
        def hit(x):
            x = x.strip()
            return x.startswith("syslog-ng[") and (
                self._HASKV.search(x) or "syslog-ng" in x[10:])
        return ratio_detect(sample_lines, lambda el: block_ratio(el, hit))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        g = m.groupdict()
        fields = {"pid": int(g["pid"])}
        for k, v in re.findall(r"(\w+)='([^']*)'", g.get("kv") or ""):
            fields[k] = v
        level = "error" if re.search(r"error|fail", g["msg"], re.I) else "info"
        return self._event(level=level, message=g["msg"], source="syslog-ng",
                           fields=fields, raw=line)


# ── systemd-networkd DHCPv4/v6 client messages ────────────────────────────────
#   systemd-networkd[321]: eth0: DHCPv4 address 192.168.1.100/24 via 192.168.1.1
class SystemdNetworkdAdapter(RxAdapter):
    name = "systemd_networkd"
    language = "any"
    default_source = "systemd-networkd"
    _RE = re.compile(
        r"^systemd-networkd\[(?P<pid>\d+)\]:\s+(?P<iface>[\w.\-]+):\s+"
        r"(?P<msg>DHCPv[46]\b.*|DHCP lease.*)$")

    def _level(self, g, line):
        return "warn" if re.search(r"lost|critical|expired", g.get("msg", ""), re.I) else "info"

    def _fields(self, g, line):
        return {"pid": int(g["pid"]), "interface": g["iface"]}


# ── NetworkManager internal DHCP client ───────────────────────────────────────
#   NetworkManager[875]: <info>  [1623334455.6789] dhcp4 (eth0): state changed …
class NetworkManagerDhcpAdapter(RxAdapter):
    name = "networkmanager_dhcp"
    language = "any"
    default_source = "NetworkManager"
    _RE = re.compile(
        r"^NetworkManager\[(?P<pid>\d+)\]:\s+<(?P<lvl>\w+)>\s+\[(?P<epoch>[\d.]+)\]\s+"
        r"dhcp(?P<ver>[46])\s+\((?P<iface>[\w.\-]+)\):\s+(?P<msg>.*)$")
    _LVL = {"info": "info", "warn": "warn", "err": "error", "debug": "debug", "trace": "trace"}

    def _ts(self, g):
        try:
            return float(g["epoch"]) * 1000.0
        except (ValueError, TypeError):
            return None

    def _level(self, g, line):
        return self._LVL.get(g["lvl"], "info")

    def _fields(self, g, line):
        return {"pid": int(g["pid"]), "interface": g["iface"], "dhcp_version": int(g["ver"])}


# ── HiveMQ event.log (client lifecycle) ───────────────────────────────────────
#   2018-05-11 14:33:02,456 - Client ID: testClient, IP: 127.0.0.1, … connected.
class HivemqEventAdapter(RxAdapter):
    name = "hivemq_event"
    language = "java"
    default_source = "hivemq.event"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s*-\s+"
        r"(?P<msg>(?:Client ID:|Outgoing publish message was dropped).*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"].replace(",", "."))

    def _level(self, g, line):
        return "warn" if "dropped" in g.get("msg", "") else "info"


# ── ExaBGP pipe-column log ────────────────────────────────────────────────────
#   14:30:41 | 21730  | welcome       | Thank you for using ExaBGP
class ExabgpAdapter(RxAdapter):
    name = "exabgp"
    language = "python"
    default_source = "exabgp"
    _RE = re.compile(
        r"^(?P<ts>\d{2}:\d{2}:\d{2})\s+\|\s+(?P<pid>\d+)\s+\|\s+"
        r"(?P<src>[\w\-]+)\s+\|\s+(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _fields(self, g, line):
        return {"pid": int(g["pid"]), "exabgp_source": g["src"]}


# ── Cisco Meraki syslog (flows/urls/ids-alerts/security_event) ────────────────
#   1374543986.038687615 MX84 flows src=192.168.1.186 dst=8.8.8.8 …
class CiscoMerakiAdapter(RxAdapter):
    name = "cisco_meraki"
    language = "any"
    default_source = "meraki"
    _RE = re.compile(
        r"^(?P<epoch>\d{10}\.\d+)\s+(?P<dev>(?:MX|MR|MS|MV|MG|Z\d)\w*)\s+"
        r"(?P<role>flows|urls|ids-alerts|security_event|events|airmarshal_events)\s+(?P<msg>.*)$")

    def _ts(self, g):
        return float(g["epoch"]) * 1000.0

    def _level(self, g, line):
        return "warn" if g["role"] in ("ids-alerts", "security_event") else "info"

    def _fields(self, g, line):
        return {"device": g["dev"], "role": g["role"]}


# ── Proxmox Backup Server task log ────────────────────────────────────────────
#   2026-07-21T02:00:01+02:00: starting new backup on datastore 'store1': …
class ProxmoxBackupTaskAdapter(RxAdapter):
    name = "proxmox_backup_task"
    language = "any"
    default_source = "proxmox-backup"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}):\s+(?P<msg>\S.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        m = g.get("msg", "")
        if m.startswith("TASK ERROR") or re.search(r"\berror\b|failed", m, re.I):
            return "error"
        return "info"


# ── Squid store.log ────────────────────────────────────────────────────────────
#   1771492054.779 RELEASE -1 FFFFFFFF <md5> 400 … text/html;… NONE /
class SquidStoreAdapter(RxAdapter):
    name = "squid_store"
    language = "any"
    default_source = "squid.store"
    _RE = re.compile(
        r"^(?P<epoch>\d{9,10}\.\d{3})\s+(?P<action>CREATE|RELEASE|SWAPOUT|SWAPIN)\s+"
        r"-?\d+\s+[0-9A-F]{8}\s+[0-9A-F]{32}\b")

    def _ts(self, g):
        return float(g["epoch"]) * 1000.0

    def _level(self, g, line):
        return ""

    def _fields(self, g, line):
        return {"action": g["action"]}


# ── squidGuard URL-filter log ─────────────────────────────────────────────────
#   2012-10-25 14:13:14 [31277] Request(default/adult/-) http://… 10.10.30.17/- - GET REDIRECT
class SquidguardAdapter(RxAdapter):
    name = "squidguard"
    language = "any"
    default_source = "squidguard"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+\[(?P<pid>\d+)\]\s+"
        r"Request\((?P<rule>[^)]*)\)\s+(?P<url>\S+)\s+(?P<client>\S+)\s+\S+\s+"
        r"(?P<method>[A-Z]+)\s+(?P<action>REDIRECT|PASS|BLOCK)\s*$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return "warn" if g["action"] in ("REDIRECT", "BLOCK") else "info"

    def _fields(self, g, line):
        return {"rule": g["rule"], "url": g["url"], "client": g["client"],
                "method": g["method"], "action": g["action"]}


# ── Varnish family: child lifecycle + panic + backend-health ──────────────────
class VarnishAdapter(LogAdapter):
    name = "varnish"
    language = "any"
    _CHILD = re.compile(
        r"^(?:(?P<lvl>Debug|Info|Error):\s+)?Child \((?P<pid>\d+)\)\s+(?P<msg>.*)$")
    _PANIC = re.compile(r"^Panic at:\s+(?P<ts>[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4}.+)$")
    _HEALTH = re.compile(
        r"^(?P<vcl>[\w.\-]+)\.(?P<backend>[\w.\-]+)\s+"
        r"(?P<status>Still healthy|Back healthy|Still sick|Went sick)\s+"
        r"(?P<bits>[46UxXrRHE-]{8})\s+")

    def detect(self, sample_lines):
        def hit(el):
            subs = [x.strip() for x in split_any(el)]
            if not subs:
                return False
            # a panic is a multi-line block whose FIRST line is the banner.
            if self._PANIC.match(subs[0]):
                return True
            good = sum(1 for x in subs
                       if self._CHILD.match(x) or self._HEALTH.match(x))
            return good >= 1 and good / len(subs) >= 0.5
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        subs = [x.strip() for x in split_any(s)]
        first = subs[0] if subs else s.strip()
        m = self._PANIC.match(first)
        if m:
            return self._event(level="fatal", message="varnishd panic",
                               source="varnishd", ts_ms=parse_timestamp(m.group("ts")),
                               fields={"block_lines": len(subs)}, category="crash", raw=line)
        m = self._HEALTH.match(first)
        if m:
            g = m.groupdict()
            sick = "sick" in g["status"]
            return self._event(level="warn" if sick else "info",
                               message=f'{g["vcl"]}.{g["backend"]} {g["status"]}',
                               source="varnishd.backend",
                               fields={"backend": g["backend"], "probe_bits": g["bits"]},
                               category="event", raw=line)
        m = self._CHILD.match(first)
        if m:
            g = m.groupdict()
            lvl = (g["lvl"] or "").lower() or (
                "error" if re.search(r"died|panic|signal", g["msg"], re.I) else "info")
            return self._event(level=lvl, message=f'Child ({g["pid"]}) {g["msg"]}',
                               source="varnishd",
                               fields={"pid": int(g["pid"])}, raw=line)
        return None


# ── IBM WebSphere traditional trace.log / advanced format ─────────────────────
#   [26/10/21 10:29:25:011 EDT] 00000073 >  UOW= source=com.ibm.ws.event… Entry
class WebsphereTraceAdapter(RxAdapter):
    name = "websphere_trace"
    language = "java"
    default_source = "websphere"
    _RE = re.compile(
        r"^\[(?P<dy>\d{1,2})/(?P<mon>\d{1,2})/(?P<yr>\d{2}) (?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2}):(?P<ms>\d{3}) (?P<zone>\w+)\]\s+"
        r"(?P<thread>[0-9a-fA-F]{8})\s+(?P<etype>[A-Zioaudit0-9<>123 ]{1,3}?)\s+UOW=(?P<rest>.*)$")

    def _ts(self, g):
        try:
            return _to_ms(datetime(2000 + int(g["yr"]), int(g["mon"]), int(g["dy"]),
                                   int(g["hh"]), int(g["mi"]), int(g["ss"]), int(g["ms"]) * 1000))
        except ValueError:
            return None

    def _level(self, g, line):
        et = (g.get("etype") or "").strip()
        return {"E": "error", "W": "warn", "F": "fatal", "A": "info", "O": "info"}.get(et, "")

    def _fields(self, g, line):
        f = {"thread": g["thread"], "event_type": (g.get("etype") or "").strip()}
        sm = re.search(r"source=(\S+)", g.get("rest", ""))
        if sm:
            f["ws_source"] = sm.group(1)
        return f


# ── IBM WebSphere FFDC incident stream ────────────────────────────────────────
#   ------Start of DE processing------ = [6/26/09 9:03:07:161 PDT] , key = … Exception = …
class WebsphereFfdcAdapter(LogAdapter):
    name = "websphere_ffdc"
    language = "java"
    _RE = re.compile(r"^-{4,}Start of DE processing-{4,}")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            return any(self._RE.match(x.strip()) for x in subs)
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        if not any(self._RE.match(x.strip()) for x in split_any(s)):
            return None
        exc = re.search(r"Exception = (\S+)", s)
        src = re.search(r"Source = (\S+)", s)
        probe = re.search(r"probeid = (\d+)", s)
        fields = {}
        if exc:
            fields["exception"] = exc.group(1)
        if src:
            fields["ffdc_source"] = src.group(1)
        if probe:
            fields["probe_id"] = int(probe.group(1))
        return self._event(level="error", message="FFDC incident: "
                           + (exc.group(1) if exc else "captured"),
                           source="websphere.ffdc", fields=fields or None,
                           category="error", raw=line)


# ── SoftEther VPN Server log ───────────────────────────────────────────────────
#   2023-01-15 12:00:00.123 On the TCP Listener (Port 5555), a Client … connected.
class SoftetherAdapter(LogAdapter):
    name = "softether"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+(?P<msg>.*)$")
    _VOCAB = re.compile(
        r"On the TCP Listener|Session \"SID-|a Client \(IP address|"
        r"VPN Client|Virtual Hub|has connected|has been|Connection", re.I)

    def detect(self, sample_lines):
        def hit(x):
            m = self._RE.match(x.strip())
            return bool(m) and self._VOCAB.search(m.group("msg")) is not None
        return vocab_detect(sample_lines, lambda el: block_ratio(el, hit), cap=0.85)

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        return self._event(level="info", message=m.group("msg"), source="softether",
                           ts_ms=parse_timestamp(m.group("ts")), raw=line)


# ── Step Function I/O dnp3 tracing ────────────────────────────────────────────
#   INFO DNP3-Master-TCP{endpoint="127.0.0.1:20000"}: connected to 127.0.0.1:20000
class StepfuncDnp3Adapter(RxAdapter):
    name = "stepfunc_dnp3"
    language = "rust"
    default_source = "dnp3"
    _RE = re.compile(
        r"^(?P<level>TRACE|DEBUG|INFO|WARN|ERROR)\s+"
        r"DNP3-(?P<kind>Master|Outstation)-(?P<transport>TCP|TLS|Serial)\{(?P<span>[^}]*)\}:\s*(?P<msg>.*)$")

    def _fields(self, g, line):
        return {"role": g["kind"], "transport": g["transport"], "span": g["span"]}


# ── YARN ResourceManager/NodeManager audit log ───────────────────────────────
#   USER=alice\tOPERATION=Submit Application Request\tTARGET=…\tRESULT=SUCCESS\t…
class YarnRmAuditAdapter(LogAdapter):
    name = "yarn_rm_audit"
    language = "java"
    _RE = re.compile(r"USER=(?P<user>[^\t]*)\tOPERATION=(?P<op>[^\t]*)\tTARGET=(?P<target>[^\t]*)\tRESULT=(?P<result>\w+)")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: bool(self._RE.search(str(el))))

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        m = self._RE.search(s)
        if not m:
            return None
        g = m.groupdict()
        return self._event(level="info" if g["result"] == "SUCCESS" else "warn",
                           message=f'{g["op"]} → {g["result"]}', source="yarn.audit",
                           fields={"user": g["user"], "operation": g["op"],
                                   "target": g["target"], "result": g["result"]},
                           category="event", raw=line)


# ── Shibboleth IdP audit log ───────────────────────────────────────────────────
#   20240301T102233Z|urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST|_c12…|https://sp…
class ShibbolethIdpAdapter(LogAdapter):
    name = "shibboleth_idp"
    language = "java"
    _RE = re.compile(r"^(?P<ts>\d{8}T\d{6}Z)\|(?P<rest>urn:|https?://|[^|]*\|).*")

    def detect(self, sample_lines):
        def hit(x):
            x = x.strip()
            return bool(re.match(r"^\d{8}T\d{6}Z\|", x)) and ("urn:" in x or "shibboleth" in x)
        return ratio_detect(sample_lines, lambda el: block_ratio(el, hit))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        if not re.match(r"^\d{8}T\d{6}Z\|", s):
            return None
        cols = s.split("|")
        ts_ms = parse_timestamp(
            f"{cols[0][:4]}-{cols[0][4:6]}-{cols[0][6:8]}T{cols[0][9:11]}:{cols[0][11:13]}:{cols[0][13:15]}Z")
        return self._event(level="info", message="SAML audit event",
                           source="shibboleth.idp", ts_ms=ts_ms,
                           fields={"columns": len(cols)}, category="event", raw=line)


# ── Brocade Fabric OS RASLog errdump ──────────────────────────────────────────
#   raslogd: 2022/12/06-18:17:08, [FW-1424], 2387, FID 128, WARNING, HKSTPSSW02, …
class BrocadeFosAdapter(RxAdapter):
    name = "brocade_fos"
    language = "any"
    default_source = "brocade.raslog"
    _RE = re.compile(
        r"^(?:raslogd:\s+)?(?P<ts>\d{4}/\d{2}/\d{2}-\d{2}:\d{2}:\d{2}),\s+"
        r"\[(?P<code>[A-Z]+-\d+)\],\s+(?P<seq>\d+),\s+FID (?P<fid>\d+),\s+"
        r"(?P<sev>INFO|WARNING|ERROR|CRITICAL),\s+(?P<sw>[^,]+),\s+(?P<msg>.*)$")
    _LVL = {"INFO": "info", "WARNING": "warn", "ERROR": "error", "CRITICAL": "fatal"}

    def _ts(self, g):
        return parse_timestamp(g["ts"].replace("/", "-").replace("-", "-", 2).replace("-", " ", 1)
                               if False else g["ts"].replace("/", "-").replace("-1", "-1")) \
            or parse_timestamp(g["ts"][:10].replace("/", "-") + " " + g["ts"][11:])

    def _level(self, g, line):
        return self._LVL.get(g["sev"], "info")

    def _fields(self, g, line):
        return {"message_id": g["code"], "fid": int(g["fid"]), "switch": g["sw"].strip()}


# ── Kea forensic/legal logging hook ───────────────────────────────────────────
#   2018-01-06 01:02:03 CET Address: 192.168.1.1 has been assigned for … to a device…
class KeaLegalAdapter(RxAdapter):
    name = "kea_legal"
    language = "any"
    default_source = "kea.legal"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\s+[A-Z]{2,4})?\s+"
        r"Address:\s+(?P<addr>[0-9a-fA-F:.]+)\s+has been (?P<verb>assigned|renewed|released)\b.*$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _fields(self, g, line):
        return {"address": g["addr"], "action": g["verb"]}


# ── Couchbase ns_server cluster-manager log ───────────────────────────────────
#   [ns_server:debug,2025-11-26T18:47:35.841Z,ns_1@node1:ns_audit<0.728.0>:…]Audit …
class CouchbaseNsAdapter(RxAdapter):
    name = "couchbase_ns"
    language = "erlang"
    default_source = "couchbase.ns_server"
    _RE = re.compile(
        r"^\[(?P<subsys>ns_server|user|couchdb|chronicle|menelaus|stats):"
        r"(?P<level>debug|info|warn|error),(?P<ts>\d{4}-\d{2}-\d{2}T[\d:.]+Z?),"
        r"(?P<node>[^:]+):(?P<proc>[^\]]*)\](?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _fields(self, g, line):
        return {"subsystem": g["subsys"], "node": g["node"], "process": g["proc"]}


# ── Windows ETW tracefmt/tracerpt text ────────────────────────────────────────
#   [0]0F70.1A34::2026-07-20 14:03:11.123456 [Microsoft-Windows-Kernel-Process]Process…
class WindowsEtwTracefmtAdapter(RxAdapter):
    name = "windows_etw_tracefmt"
    language = "windows"
    default_source = "windows.etw"
    _RE = re.compile(
        r"^\[(?P<cpu>\d+)\](?P<pid>[0-9A-F]{4})\.(?P<tid>[0-9A-F]{4})::"
        r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+"
        r"\[(?P<provider>[^\]]+)\](?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _fields(self, g, line):
        return {"provider": g["provider"], "cpu": int(g["cpu"]),
                "pid_hex": g["pid"], "tid_hex": g["tid"]}


# ── VMware vSphere Update Manager / vCenter log4cpp ───────────────────────────
#   2022-02-11T09:09:01:713Z 'VcIntegrity' 140676406601472 INFO [vcIntegrity, 1526] …
class VmwareVumAdapter(RxAdapter):
    name = "vmware_vum"
    language = "any"
    default_source = "vmware.vum"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}):(?P<ms>\d{3})Z\s+"
        r"'(?P<cat>[^']*)'\s+(?P<tid>\d+)\s+"
        r"(?P<level>TRACE|DEBUG|VERBOSE|INFO|WARNING|ERROR|FATAL)\s+"
        r"(?:\[(?P<loc>[^\]]*)\]\s+)?(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(f'{g["ts"]}.{g["ms"]}Z')

    def _fields(self, g, line):
        return {"category": g["cat"], "tid": int(g["tid"]), "location": g.get("loc")}


# ── Vespa search/vector engine vespa.log (7 tab fields) ───────────────────────
#   1102675319.726342\tmyhost\t12345\tsearchnode\tcontainer.handler\tinfo\tmsg
class VespaAdapter(LogAdapter):
    name = "vespa"
    language = "java"
    _LVLS = {"fatal", "error", "warning", "info", "config", "event", "debug", "spam"}

    def detect(self, sample_lines):
        def hit(x):
            parts = x.rstrip("\n").split("\t")
            return (len(parts) >= 7 and re.match(r"^\d+\.\d+$", parts[0])
                    and parts[5] in self._LVLS)
        return ratio_detect(sample_lines, lambda el: block_ratio(el, hit))

    def parse_line(self, line):
        parts = line.rstrip("\r\n").split("\t")
        if len(parts) < 7 or parts[5] not in self._LVLS:
            return None
        lvl = {"warning": "warn", "config": "info", "event": "info",
               "spam": "trace"}.get(parts[5], parts[5])
        return self._event(level=lvl, message=parts[6], source=parts[4],
                           ts_ms=float(parts[0]) * 1000.0,
                           fields={"host": parts[1], "pid": parts[2], "service": parts[3]},
                           raw=line)


# ── OpenVPN --status file (v1 client list rows) ───────────────────────────────
#   client1,203.0.113.5:51044,3325163,3227110,Fri Dec 13 10:19:19 2019
class OpenvpnStatusAdapter(LogAdapter):
    name = "openvpn_status"
    language = "any"
    _HDR = re.compile(r"^(OpenVPN CLIENT LIST|ROUTING TABLE|GLOBAL STATS|TITLE,|HEADER,)")
    _ROW = re.compile(
        r"^(?P<cn>[^,]+),(?P<addr>[\d.]+:\d+),(?P<rx>\d+),(?P<tx>\d+),"
        r"(?P<since>[A-Z][a-z]{2} [A-Z][a-z]{2}\s+\d+ \d{2}:\d{2}:\d{2} \d{4})\s*$")

    def detect(self, sample_lines):
        def hit(x):
            x = x.strip()
            return bool(self._HDR.match(x) or self._ROW.match(x))
        return vocab_detect(sample_lines, lambda el: block_ratio(el, hit), cap=0.85)

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        if self._HDR.match(s):
            return self._event(level="", message=s, source="openvpn.status",
                               fields={"header": True}, raw=line)
        m = self._ROW.match(s)
        if not m:
            return None
        g = m.groupdict()
        return self._event(level="info", message=f'{g["cn"]} @ {g["addr"]}',
                           source="openvpn.status",
                           ts_ms=parse_timestamp(g["since"]),
                           fields={"common_name": g["cn"], "address": g["addr"],
                                   "bytes_recv": int(g["rx"]), "bytes_sent": int(g["tx"])},
                           category="event", raw=line)


# ── Symantec Endpoint Protection AV export CSV ────────────────────────────────
#   2020-01-16 08:00:31,Critical,WIN-DESKTOP01,Trojan.Gen.2,C:\…\evil.exe,jdoe,…
class SymantecSepCsvAdapter(RxAdapter):
    name = "symantec_sep_csv"
    language = "any"
    default_source = "symantec.sep"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(?P<sev>Critical|Major|Minor|Info),"
        r"(?P<host>[^,]*),(?P<risk>[^,]*),(?P<path>[^,]*),")
    _LVL = {"Critical": "fatal", "Major": "error", "Minor": "warn", "Info": "info"}

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return self._LVL.get(g["sev"], "info")

    def _fields(self, g, line):
        return {"host": g["host"], "risk": g["risk"], "path": g["path"]}


# ── PowerMTA accounting CSV ────────────────────────────────────────────────────
#   d,2019-02-08 21:54:05+0000,2019-02-08 21:54:05+0000,…@bounce…,…,relayed,…
class PmtaAccountingAdapter(RxAdapter):
    name = "pmta_accounting"
    language = "any"
    default_source = "pmta"
    _RE = re.compile(
        r"^(?P<rt>rb?|d|b|t|tq|f),(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{4}),"
        r"(?P<ts2>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{4}),")
    _RT = {"d": "delivered", "b": "bounced", "rb": "bounced", "r": "received",
           "t": "transient", "tq": "queued", "f": "failed"}

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return "warn" if g["rt"] in ("b", "rb", "f") else "info"

    def _fields(self, g, line):
        return {"record_type": g["rt"], "event": self._RT.get(g["rt"], g["rt"])}


# ── Amazon Neptune audit log CSV ──────────────────────────────────────────────
#   1690000000123456,10.0.0.5,ip-10-0-0-8,HTTP_POST,arn:aws:iam::…,{…},"/gremlin…",g.V()…
class NeptuneAuditAdapter(RxAdapter):
    name = "neptune_audit"
    language = "any"
    default_source = "neptune.audit"
    _RE = re.compile(
        r"^(?P<epoch>\d{16}),(?P<client>[^,]+),(?P<server>[^,]+),"
        r"(?P<conn>Websocket|HTTP_POST|HTTP_GET|Bolt),")

    def _ts(self, g):
        return float(g["epoch"]) / 1000.0     # µs epoch → ms

    def _level(self, g, line):
        return ""

    def _fields(self, g, line):
        return {"client": g["client"], "server": g["server"], "connection": g["conn"]}


# ── FactoryTalk Diagnostics export CSV (header + rows) ────────────────────────
#   Time,Severity,Message,Provider,User,Computer
class FactorytalkAdapter(LogAdapter):
    name = "factorytalk"
    language = "any"
    _HDR = re.compile(r"^Time,Severity,Message,Provider,User,Computer")
    _ROW = re.compile(r"^[^,]*,(?P<sev>Audit|Error|Warning|Information),")
    _LVL = {"Audit": "info", "Error": "error", "Warning": "warn", "Information": "info"}

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda el: block_ratio(el, lambda x: bool(self._HDR.match(x.strip())
                                                      or self._ROW.match(x.strip()))))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        if self._HDR.match(s):
            return self._event(level="", message="FactoryTalk diagnostics header",
                               source="factorytalk", fields={"header": True}, raw=line)
        m = self._ROW.match(s)
        if not m:
            return None
        cols = s.split(",")
        return self._event(level=self._LVL.get(m.group("sev"), "info"),
                           message=cols[2] if len(cols) > 2 else s,
                           source="factorytalk",
                           fields={"severity": m.group("sev")},
                           category="event", raw=line)


# ── FreeRADIUS detail file (ctime header + tab AVP block) ─────────────────────
#   Tue Jul 20 11:01:12 2026 / \tUser-Name = "bob" / \tNAS-IP-Address = …
class FreeradiusDetailAdapter(LogAdapter):
    name = "freeradius_detail"
    language = "any"
    _HDR = re.compile(r"^[A-Z][a-z]{2} [A-Z][a-z]{2}\s+\d+ \d{2}:\d{2}:\d{2} \d{4}\s*$")
    # an AVP line is tab-indented; escaped shipping renders the tab as literal \t
    _AVP = re.compile(r"^(?:\t|\\t|\s)+(?P<attr>[\w\-]+) = (?P<val>.*)$")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            return bool(subs) and self._HDR.match(subs[0].strip()) is not None and (
                any(self._AVP.match(x) for x in subs[1:]) or len(subs) == 1)
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if not subs or not self._HDR.match(subs[0].strip()):
            return None
        fields = {}
        for x in subs[1:]:
            m = self._AVP.match(x)
            if m:
                fields[m.group("attr")] = m.group("val").strip('"')
        return self._event(level="info",
                           message=f'RADIUS detail: {fields.get("Acct-Status-Type", "record")}',
                           source="freeradius.detail",
                           ts_ms=parse_timestamp(subs[0].strip()),
                           fields=fields or None, category="event", raw=line)


# ── multipathd 'show paths' / 'multipath -ll' topology table ──────────────────
#   1:0:0:0 sda 8:0   50  active ready  running XXXX...... 8/20
class MultipathdAdapter(LogAdapter):
    name = "multipathd"
    language = "linux"
    _HDR = re.compile(r"^\s*(hcil|uuid)\s+.*\bdev(_t)?\b")
    _ROW = re.compile(
        r"^(?P<hcil>\d+:\d+:\d+:\d+)\s+(?P<dev>\w+)\s+(?P<devt>\d+:\d+)\s+"
        r"(?P<pri>\d+)\s+(?P<dmst>\w+)\s+(?P<chkst>\w+)\s+(?P<devst>\w+)\b")

    def detect(self, sample_lines):
        def hit(x):
            x = x.rstrip()
            return bool(self._HDR.match(x) or self._ROW.match(x))
        return vocab_detect(sample_lines, lambda el: block_ratio(el, hit), cap=0.85)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        if self._HDR.match(s):
            return self._event(level="", message=s.strip(), source="multipathd",
                               fields={"header": True}, raw=line)
        m = self._ROW.match(s.strip())
        if not m:
            return None
        g = m.groupdict()
        bad = g["dmst"] != "active" or g["chkst"] != "ready" or g["devst"] != "running"
        return self._event(level="warn" if bad else "info",
                           message=f'{g["dev"]} {g["hcil"]} {g["dmst"]}/{g["chkst"]}/{g["devst"]}',
                           source="multipathd",
                           fields={"device": g["dev"], "hcil": g["hcil"],
                                   "dm_state": g["dmst"], "check_state": g["chkst"],
                                   "dev_state": g["devst"]},
                           category="event", raw=line)


# ── IBM RMF Postprocessor / Monitor III text report ───────────────────────────
#   C P U  A C T I V I T Y     z/OS V2R5   SYSTEM ID SYS1    DATE 07/20/2026 …
class RmfPostprocessorAdapter(LogAdapter):
    name = "rmf_postprocessor"
    language = "zos"
    _RE = re.compile(
        r"^(?P<title>(?:[A-Z] )+[A-Z])\s{2,}.*?(z/OS V\d|SYSTEM ID|RMF|DATE \d{2}/\d{2}/\d{4})")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: bool(self._RE.match(str(el).rstrip())))

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        m = self._RE.match(s)
        if not m:
            return None
        title = re.sub(r"\s+", "", m.group("title"))
        return self._event(level="", message=f"RMF report: {title}",
                           source="rmf", fields={"report": title},
                           category="event", raw=line)


# ── ASC X12 HIPAA EDI transaction ─────────────────────────────────────────────
#   ST*837*0021*005010X222~BHT*…~NM1*…~
class X12EdiAdapter(LogAdapter):
    name = "x12_edi"
    language = "any"
    _ISA = re.compile(r"^ISA[*|^].{50,}")
    _ST = re.compile(r"^ST\*(?P<tset>\d{3})\*")
    _SEG = re.compile(r"^[A-Z][A-Z0-9]{1,2}\*.*~")

    def detect(self, sample_lines):
        def hit(x):
            x = x.strip()
            return bool(self._ISA.match(x) or self._ST.match(x)
                        or (self._SEG.match(x) and "~" in x))
        return vocab_detect(sample_lines, lambda el: block_ratio(el, hit), cap=0.9)

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        tset = None
        m = self._ST.search(s)
        if m:
            tset = m.group("tset")
        segs = s.count("~")
        return self._event(level="", message=f'X12 EDI'
                           + (f' 8{tset[1:]}' if False else (f' ST*{tset}' if tset else '')),
                           source="x12.edi",
                           fields={"transaction_set": tset, "segments": segs},
                           category="event", raw=line)


# ── FreeBSD dmesg(8) boot probe output ────────────────────────────────────────
#   ugen0.2: <vendor product> at usbus0
class FreebsdDmesgRawAdapter(LogAdapter):
    name = "freebsd_dmesg_raw"
    language = "freebsd"
    _RE = re.compile(r"^(?P<dev>[a-z]{2,8}\d+(?:\.\d+)?):\s+(?P<msg>.*)$")
    _VOCAB = re.compile(r"<[^>]+>|\bat \w+|\bon \w+|\bport 0x|\birq \d|\bmem 0x|"
                        r"Ethernet address|drive|detached|attached", re.I)

    def detect(self, sample_lines):
        def hit(x):
            m = self._RE.match(x.strip())
            return bool(m) and self._VOCAB.search(m.group("msg")) is not None
        return vocab_detect(sample_lines, lambda el: block_ratio(el, hit), cap=0.7)

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m or not self._VOCAB.search(m.group("msg")):
            return None
        return self._event(level="", message=m.group("msg"), source=m.group("dev"),
                           fields={"device": m.group("dev")}, raw=line)


# ── Das U-Boot structured log framework ───────────────────────────────────────
#   NOTICE.efi_loader: Booting /EFI/BOOT/BOOTAA64.EFI
class UbootStructuredAdapter(RxAdapter):
    name = "uboot_structured"
    language = "firmware"
    default_source = "u-boot"
    _RE = re.compile(
        r"^(?P<level>EMERG|ALERT|CRIT|ERR|WARNING|NOTICE|INFO|DEBUG)\.(?P<cat>[\w]+):\s+(?P<msg>.*)$")
    _LVL = {"EMERG": "fatal", "ALERT": "fatal", "CRIT": "fatal", "ERR": "error",
            "WARNING": "warn", "NOTICE": "info", "INFO": "info", "DEBUG": "debug"}

    def _level(self, g, line):
        return self._LVL.get(g["level"], "info")

    def _fields(self, g, line):
        return {"category": g["cat"]}

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            ev["source"] = f'u-boot.{self._RE.match(line.strip()).group("cat")}'
        return ev


# ── Linux LIO / targetcli iSCSI target kernel messages ────────────────────────
#   kernel: iSCSI Login negotiation failed.
class LioTargetAdapter(RxAdapter):
    name = "lio_target"
    language = "linux"
    default_source = "lio"
    _RE = re.compile(
        r"^(?:kernel: )?(?P<msg>(?:iSCSI (?:Login|Session)|TARGET_CORE\[|"
        r"Unable to locate Target IQN|Received iSCSI login).*)$")

    def _level(self, g, line):
        return "error" if re.search(r"fail|unable|invalid|reject", g.get("msg", ""), re.I) else "info"


# ── Cisco AnyConnect / Secure Client endpoint log ─────────────────────────────
#   Function: … File: … Line: … Invoked Function: … Return Code: 0 (0x…) …
class CiscoAnyconnectAdapter(LogAdapter):
    name = "cisco_anyconnect"
    language = "any"
    _RE = re.compile(
        r"Function:\s+(?P<func>\S+).*File:\s+(?P<file>\S+).*Line:\s+(?P<line>\d+).*Return Code:\s+(?P<rc>\S+)")

    def detect(self, sample_lines):
        def hit(el):
            joined = " ".join(split_any(el))
            return bool(self._RE.search(joined))
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        m = self._RE.search(" ".join(split_any(s)))
        if not m:
            return None
        g = m.groupdict()
        rc0 = g["rc"] in ("0", "0x00000000")
        return self._event(level="info" if rc0 else "error",
                           message=f'{g["func"]} rc={g["rc"]}',
                           source="cisco.anyconnect",
                           fields={"function": g["func"], "file": g["file"],
                                   "line": int(g["line"]), "return_code": g["rc"]},
                           category="event", raw=line)


# ── AWS CDK cdk deploy progress output ────────────────────────────────────────
#   MyStack | 3/5 | 12:26:36 PM | CREATE_COMPLETE | AWS::S3::Bucket | MyBucket (…)
class AwsCdkAdapter(RxAdapter):
    name = "aws_cdk"
    language = "any"
    default_source = "cdk"
    _RE = re.compile(
        r"^(?P<stack>[\w\-]+)\s+\|\s+(?P<n>\d+)/(?P<tot>\d+)\s+\|\s+"
        r"(?P<time>\d{1,2}:\d{2}:\d{2} [AP]M)\s+\|\s+(?P<status>[A-Z_]+)\s+\|\s+"
        r"(?P<restype>AWS::[\w:]+)\s+\|\s+(?P<logical>.*)$")

    def _level(self, g, line):
        st = g["status"]
        return ("error" if "FAILED" in st or "ROLLBACK" in st
                else "info")

    def _fields(self, g, line):
        return {"stack": g["stack"], "progress": f'{g["n"]}/{g["tot"]}',
                "cfn_status": g["status"], "resource_type": g["restype"]}


# ── Orthanc DICOM server glog-style log ───────────────────────────────────────
#   I0721 12:34:56.789012 MAIN main.cpp:123] Orthanc has started
class OrthancAdapter(RxAdapter):
    name = "orthanc"
    language = "cpp"
    default_source = "orthanc"
    _RE = re.compile(
        r"^(?P<lvl>[EWIT])(?P<mon>\d{2})(?P<dy>\d{2}) (?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})\.(?P<us>\d{6})\s+"
        r"(?P<thread>\w+)\s+(?:(?P<plugin>\w+)\s+)?(?P<file>[\w.\-]+:\d+)\]\s*(?P<msg>.*)$")
    _LVL = {"E": "error", "W": "warn", "I": "info", "T": "trace"}

    def _ts(self, g):
        now = datetime.now()
        return mk_ts(now.year, g["mon"], g["dy"], g["hh"], g["mi"], g["ss"], int(g["us"]))

    def _level(self, g, line):
        return self._LVL.get(g["lvl"], "info")

    def _fields(self, g, line):
        f = {"thread": g["thread"], "source_file": g["file"]}
        if g.get("plugin"):
            f["plugin"] = g["plugin"]
        return f


# ── HPE 3PAR StoreServ event/alert syslog ─────────────────────────────────────
#   May  1 12:00:00 3par01 3par01 sw_cli {admin super all} {createvv ...} {Error: ...} {}
class Hpe3parAdapter(LogAdapter):
    name = "hpe_3par"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2})\s+(?P<host>\S+)\s+\S+\s+"
        r"(?P<what>sw_cli|hw_cage\w*)\b(?P<rest>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: bool(self._RE.match(str(el).strip())))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        g = m.groupdict()
        level = "error" if re.search(r"\{Error", g["rest"]) else "info"
        return self._event(level=level, message=f'{g["what"]}{g["rest"]}'.strip(),
                           source="hpe.3par", ts_ms=parse_timestamp(g["ts"]),
                           fields={"host": g["host"], "subsystem": g["what"]}, raw=line)


# ── Dell EMC NetWorker daemon (rendered) ──────────────────────────────────────
#   71193 07/21/2026 02:00:01 AM  1 5 0 … backupsrv nsrd NSR notice Savegroup …
class NetworkerAdapter(RxAdapter):
    name = "networker"
    language = "any"
    default_source = "networker"
    _RE = re.compile(
        r"^(?P<msgid>\d+)\s+(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<time>\d{1,2}:\d{2}:\d{2}) (?P<ampm>[AP]M)\s+"
        r".*?\s(?P<daemon>nsrd|nsrmmd|nsrjobd|nsrexecd|nsrmmgd|nsrmmdbd)\s+NSR\s+"
        r"(?P<sev>notice|warning|info|critical|error)\s+(?P<msg>.*)$")
    _LVL = {"notice": "info", "info": "info", "warning": "warn",
            "critical": "fatal", "error": "error"}

    def _ts(self, g):
        return us_date_ts(g["date"], f'{g["time"]} {g["ampm"]}')

    def _level(self, g, line):
        return self._LVL.get(g["sev"], "info")

    def _fields(self, g, line):
        return {"message_id": int(g["msgid"]), "daemon": g["daemon"]}


# ── HPE / OpenText Data Protector session messages ────────────────────────────
#   [Normal] From: BSM@cellmgr.example.com "NightlyFS"  Time: 7/21/2026 2:00:01 AM
class DataProtectorAdapter(RxAdapter):
    name = "hpe_data_protector"
    language = "any"
    default_source = "dataprotector"
    match_scope = "first"
    _RE = re.compile(
        r"^\[(?P<sev>Normal|Warning|Minor|Major|Critical)\]\s+From:\s+(?P<agent>\S+@\S+)\s+(?P<rest>.*)$")
    _LVL = {"Normal": "info", "Warning": "warn", "Minor": "warn",
            "Major": "error", "Critical": "fatal"}

    def _level(self, g, line):
        return self._LVL.get(g["sev"], "info")

    def _ts(self, g):
        tm = re.search(r"Time:\s+(\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}:\d{2}(?: [AP]M)?)", g.get("rest", ""))
        if not tm:
            return None
        parts = tm.group(1).split(" ", 1)
        return us_date_ts(parts[0], parts[1])

    def _fields(self, g, line):
        return {"agent": g["agent"], "severity": g["sev"]}


for _a in (GazeboAdapter(), HfTransformersAdapter(), FreepbxMonologAdapter(),
           NsqNsqdAdapter(), RspamdAdapter(), RsyslogdInternalAdapter(),
           SyslogNgInternalAdapter(), SystemdNetworkdAdapter(),
           NetworkManagerDhcpAdapter(), HivemqEventAdapter(), ExabgpAdapter(),
           CiscoMerakiAdapter(), ProxmoxBackupTaskAdapter(), SquidStoreAdapter(),
           SquidguardAdapter(), VarnishAdapter(), WebsphereTraceAdapter(),
           WebsphereFfdcAdapter(), SoftetherAdapter(), StepfuncDnp3Adapter(),
           YarnRmAuditAdapter(), ShibbolethIdpAdapter(), BrocadeFosAdapter(),
           KeaLegalAdapter(), CouchbaseNsAdapter(), WindowsEtwTracefmtAdapter(),
           VmwareVumAdapter(), VespaAdapter(), OpenvpnStatusAdapter(),
           SymantecSepCsvAdapter(), PmtaAccountingAdapter(), NeptuneAuditAdapter(),
           FactorytalkAdapter(), FreeradiusDetailAdapter(), MultipathdAdapter(),
           RmfPostprocessorAdapter(), X12EdiAdapter(), FreebsdDmesgRawAdapter(),
           UbootStructuredAdapter(), CiscoAnyconnectAdapter(), AwsCdkAdapter(),
           OrthancAdapter(), Hpe3parAdapter(), NetworkerAdapter(),
           DataProtectorAdapter()):
    register_adapter(_a)

# lio_target rides the bare "kernel:" line shape → before linux_dmesg.
register_adapter(LioTargetAdapter(), before="linux_dmesg")
