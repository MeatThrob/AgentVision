"""
Virtualization / private-cloud log adapters (BATCH 3)
================================================================================
OpenStack (oslo.log covers nova/neutron/cinder/glance/keystone text logs),
VMware ESXi/hostd, and Proxmox task records. JSON renderings of these stacks
are already served by the `jsonl` super-adapter.

Formats: openstack_oslo, esxi_vmkernel, vmware_vmacore, proxmox_task.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp,
                      multiline_ratio_detect, ratio_detect, _MONTHS)


# ── OpenStack oslo.log default/context format ────────────────────────────────
#   2017-05-16 00:00:04.500 2931 INFO nova.compute.manager [req-… -] [instance: …] VM Started …
class OpenStackOsloAdapter(LogAdapter):
    name = "openstack_oslo"
    language = "python"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[.,]\d+)\s+"
        r"(?P<pid>\d+)\s+"
        r"(?P<lvl>TRACE|DEBUG|INFO|AUDIT|WARNING|ERROR|CRITICAL)\s+"
        r"(?P<mod>[\w.]+)\s+(?P<rest>\[.*)$")
    _REQ = re.compile(r"^\[(?P<ctx>req-[0-9a-f\-]+[^\]]*|instance: [^\]]+|-)\]\s*(?P<msg>.*)$",
                      re.DOTALL)

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        rest = g["rest"]
        fields = {"pid": int(g["pid"])}
        trace = None
        rm = self._REQ.match(rest)
        msg = rest
        if rm:
            ctx = rm.group("ctx")
            msg = rm.group("msg")
            if ctx.startswith("req-"):
                trace = ctx.split()[0]
                fields["request_id"] = trace
            elif ctx.startswith("instance:"):
                fields["instance"] = ctx.split(":", 1)[1].strip()
            # a second bracket group may follow ([req-…] [instance: …] msg)
            im = re.match(r"^\[instance:\s*(?P<inst>[^\]]+)\]\s*(?P<m2>.*)$", msg, re.DOTALL)
            if im:
                fields["instance"] = im.group("inst")
                msg = im.group("m2")
        return self._event(level=g["lvl"], message=msg.strip() or rest,
                           source=g["mod"], ts_ms=parse_timestamp(g["ts"]),
                           trace_id=trace, fields=fields, raw=line)


# ── VMware ESXi vmkernel.log ─────────────────────────────────────────────────
#   2022-02-11T09:08:05.317Z cpu40:2100116)MemSchedAdmit: 489: uw.2100116 (6935) …
class EsxiVmkernelAdapter(LogAdapter):
    name = "esxi_vmkernel"
    language = "any"
    # BATCH-6 gap fix: ESXi 7+ writes a 2-letter level code (In/Wa/Er/Cr/Al/No/
    # Db) and an optional literal 'vmkernel:' tag between level and cpuN.
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)\s+"
        r"(?:(?P<lvl>WARNING|ALERT|Info|In|Wa|Er|Cr|Al|No|Db)\((?P<code>\d+)\)\s+)?"
        r"(?:(?P<proc>vmkernel|vmkwarning):\s+)?"
        r"cpu(?P<cpu>\d+):(?P<world>\d+)(?:\s+opID=\S+)?\)"
        r"(?:(?P<lvl2>WARNING|ALERT):\s*)?"
        r"(?:(?P<sub>[\w.\-]+):\s*(?:(?P<lineno>\d+):\s*)?)?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        lvl = (g.get("lvl") or g.get("lvl2") or "").upper()
        level = {"WARNING": "warn", "ALERT": "fatal", "IN": "info", "INFO": "info",
                 "WA": "warn", "ER": "error", "CR": "fatal", "AL": "fatal",
                 "NO": "info", "DB": "debug"}.get(lvl, "info")
        return self._event(level=level, message=g["msg"],
                           source=f'vmkernel.{g["sub"]}' if g.get("sub") else "vmkernel",
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"cpu": int(g["cpu"]), "world": int(g["world"]),
                                   "subsystem": g.get("sub")}, raw=line)


# ── VMware hostd/vpxa vmacore "Originator" lines ─────────────────────────────
#   info hostd[2100216] [Originator@6876 sub=Vimsvc.TaskManager opID=… user=…] Task Created : …
class VmwareVmacoreAdapter(LogAdapter):
    name = "vmware_vmacore"
    language = "any"
    _RE = re.compile(
        r"^(?:(?P<ts>\d{4}-\d{2}-\d{2}T[\d:.]+Z?)\s+)?"
        r"(?P<lvl>verbose|trivia|info|warning|error|panic)\s+"
        r"(?P<proc>[\w.\-]+)\[(?P<pid>[0-9A-Fa-f]+)\]\s+"
        r"\[Originator@(?P<chain>\d+)\s*(?P<attrs>[^\]]*)\]\s*(?P<msg>.*)$")
    _LVL = {"verbose": "debug", "trivia": "trace", "info": "info",
            "warning": "warn", "error": "error", "panic": "fatal"}

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {"process": g["proc"], "pid": g["pid"]}
        for k, v in re.findall(r"(\w+)=(\S+)", g["attrs"] or ""):
            fields[k] = v
        return self._event(level=self._LVL.get(g["lvl"], g["lvl"]), message=g["msg"],
                           source=f'{g["proc"]}.{fields.get("sub", "")}'.rstrip("."),
                           ts_ms=parse_timestamp(g["ts"]) if g.get("ts") else None,
                           trace_id=fields.get("opID"), fields=fields, raw=line)


# ── Proxmox task index / UPID records ────────────────────────────────────────
#   UPID:pve1:0003C4E9:0A63F8D2:65D4A1B3:qmstart:103:root@pam: 65D4A1C0 OK
class ProxmoxTaskAdapter(LogAdapter):
    name = "proxmox_task"
    language = "any"
    _RE = re.compile(
        r"^UPID:(?P<node>[\w\-.]+):(?P<pid>[0-9A-Fa-f]{8}):(?P<pstart>[0-9A-Fa-f]{8,9}):"
        r"(?P<start>[0-9A-Fa-f]{8}):(?P<type>[\w\-]+):(?P<id>[^:]*):(?P<user>[^:]*):"
        r"(?:\s*(?P<end>[0-9A-Fa-f]{8})\s+(?P<status>.*))?$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        status = (g.get("status") or "").strip()
        level = ("info" if status.upper() == "OK" or not status else
                 "warn" if status.upper().startswith("WARNINGS") else "error")
        ts_ms = None
        try:
            ts_ms = float(int(g["start"], 16)) * 1000.0
        except ValueError:
            pass
        end_ms = None
        if g.get("end"):
            try:
                end_ms = float(int(g["end"], 16)) * 1000.0
            except ValueError:
                pass
        msg = f'{g["type"]} {g["id"]}'.strip() + (f" → {status}" if status else " started")
        return self._event(level=level, message=msg, source=f'pve.{g["node"]}',
                           ts_ms=ts_ms,
                           fields={"node": g["node"], "task_type": g["type"],
                                   "task_id": g["id"], "user": g["user"],
                                   "status": status or None,
                                   "end_ts": datetime.fromtimestamp(
                                       end_ms / 1000.0, tz=timezone.utc).isoformat()
                                   if end_ms else None},
                           raw=line)


# ── oVirt / RHV engine.log (WildFly-hosted java engine) — BATCH 4 ────────────
#   2018-08-22 00:16:14,357+05 INFO  [org.ovirt.engine…] (default task-133)
#   [e3bc976c-…] START, HotUnplugLeaseVDSCommand(…), log id: 7a634963
class OvirtEngineAdapter(LogAdapter):
    name = "ovirt_engine"
    language = "java"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})(?P<tz>[+-]\d{2}(?::?\d{2})?)?\s+"
        r"(?P<lvl>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+"
        r"\[(?P<cls>[\w.$]+)\]\s+\((?P<thread>[^)]*)\)\s+"
        r"(?:\[(?P<corr>[^\]]*)\]\s*)?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {"thread": g["thread"]}
        return self._event(level=g["lvl"], message=g["msg"], source=g["cls"],
                           ts_ms=parse_timestamp(g["ts"]),
                           trace_id=(g.get("corr") or "").strip() or None,
                           fields=fields, raw=line)


# ── oVirt/RHV host agent VDSM v4+ (vdsm.log) — BATCH 4 ───────────────────────
#   2017-04-18 14:00:00,000+0200 INFO  (jsonrpc/2) [jsonrpc.JsonRpcServer]
#   RPC call Host.getStats succeeded in 0.02 seconds (__init__:515)
class VdsmLogAdapter(LogAdapter):
    name = "vdsm_log"
    language = "python"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})(?P<tz>[+-]\d{4})?\s+"
        r"(?P<lvl>TRACE|DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL)\s+"
        r"\((?P<thread>[^)]*)\)\s+\[(?P<logger>[^\]]*)\]\s+"
        r"(?P<msg>.*?)(?:\s+\((?P<loc>[\w.]+:\d+)\))?$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {"thread": g["thread"]}
        if g.get("loc"):
            fields["location"] = g["loc"]
        return self._event(level=g["lvl"], message=g["msg"], source=g["logger"],
                           ts_ms=parse_timestamp(g["ts"]), fields=fields,
                           raw=line)


# ═══════════════════════════ BATCH 5 ═════════════════════════════════════════

# ── ESXi vobd (VMkernel Observation daemon) ───────────────────────────────────
#   vobd: [netCorrelator] 25665383us: [esx.audit.net.firewall.config.changed] …
class EsxiVobdAdapter(LogAdapter):
    name = "esxi_vobd"
    language = "any"
    _RE = re.compile(
        r"^(?:\S+\s+)?vobd:\s+\[(?P<corr>\w+)\]\s+(?P<us>\d+)us:\s+"
        r"\[(?P<vid>[\w.]+)\]\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        vid = g["vid"]
        level = ("error" if ".problem." in vid
                 else "warn" if re.search(r"\.(warning|degraded|lost)\b", vid)
                 else "info")
        return self._event(level=level, message=g["msg"] or vid,
                           source=f'vobd.{g["corr"]}',
                           fields={"vob_id": vid, "uptime_us": int(g["us"])},
                           raw=line)


# ── Nutanix genesis.out / Python service logs ─────────────────────────────────
#   2023-08-25 23:15:36,701Z INFO MainThread genesis_utils.py:8077 msg
#   (the ",mmmZ" — comma-millis + literal Z UTC marker — is the fingerprint)
class NutanixGenesisAdapter(LogAdapter):
    name = "nutanix_genesis"
    language = "python"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})Z\s+"
        r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
        r"(?P<thread>\S+)\s+(?P<loc>\S+:\d+)\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        ts_ms = None
        tm = re.match(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2}),(\d{3})",
                      g["ts"])
        if tm:
            yr, mo, dy, hh, mi, ss, ms = (int(x) for x in tm.groups())
            try:                               # the literal Z pins this to UTC
                ts_ms = datetime(yr, mo, dy, hh, mi, ss, ms * 1000,
                                 tzinfo=timezone.utc).timestamp() * 1000.0
            except ValueError:
                ts_ms = None
        return self._event(level=g["level"], message=g["msg"], source=g["loc"],
                           ts_ms=ts_ms, fields={"thread": g["thread"]}, raw=line)


# ── Open vSwitch VLOG file format ─────────────────────────────────────────────
#   2016-03-14T20:19:04.741Z|00001|vlog|INFO|opened log file …
class OvsVlogAdapter(LogAdapter):
    name = "ovs_vlog"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z)\|"
        r"(?P<seq>\d{3,6})\|(?P<mod>[\w()\-]+)\|"
        r"(?P<level>EMER|ERR|WARN|INFO|DBG)\|(?P<msg>.*)$")
    _LVL = {"EMER": "fatal", "ERR": "error", "WARN": "warn",
            "INFO": "info", "DBG": "debug"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=self._LVL.get(g["level"], g["level"]),
                           message=g["msg"], source=f'ovs.{g["mod"]}',
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"seq": int(g["seq"])}, raw=line)


# ── OpenStack Swift proxy-server access line ──────────────────────────────────
#   client remote 26/Apr/2026/17/46/40 GET /v1/AUTH_x/c/o HTTP/1.0 200 … tx…-…
class SwiftProxyAdapter(LogAdapter):
    name = "swift_proxy"
    language = "python"
    _RE = re.compile(
        r"^(?P<client>\S+)\s+(?P<remote>\S+)\s+"
        r"(?P<dy>\d{2})/(?P<mon>[A-Z][a-z]{2})/(?P<yr>\d{4})/(?P<hh>\d{2})/(?P<mi>\d{2})/(?P<ss>\d{2})\s+"
        r"(?P<method>[A-Z]+)\s+(?P<path>\S+)\s+(?P<proto>HTTP/[\d.]+)\s+"
        r"(?P<status>\d{3})\s+(?P<rest>.*)$")
    _TXID = re.compile(r"\btx[0-9a-f]{21}-[0-9a-f]{10}\b")

    def detect(self, sample_lines):
        def ok(ln):
            m = self._RE.match(ln.strip())
            return bool(m and self._TXID.search(m.group("rest")))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        ts_ms = None
        if g["mon"] in _MONTHS:                # swift logs UTC by default
            try:
                ts_ms = datetime(int(g["yr"]), _MONTHS[g["mon"]], int(g["dy"]),
                                 int(g["hh"]), int(g["mi"]), int(g["ss"]),
                                 tzinfo=timezone.utc).timestamp() * 1000.0
            except ValueError:
                ts_ms = None
        status = int(g["status"])
        level = "error" if status >= 500 else "warn" if status >= 400 else "info"
        tx = self._TXID.search(g["rest"])
        return self._event(level=level,
                           message=f'{g["method"]} {g["path"]} → {status}',
                           source="swift.proxy", ts_ms=ts_ms,
                           trace_id=tx.group(0) if tx else None,
                           fields={"client": g["client"], "status": status,
                                   "method": g["method"], "path": g["path"]},
                           raw=line)


# ── OpenStack eventlet.wsgi access line (bare form) ───────────────────────────
#   10.11.10.1 "GET /v2/…/servers/detail HTTP/1.1" status: 200 len: 1893 time: 0.24
class EventletWsgiAdapter(LogAdapter):
    name = "eventlet_wsgi"
    language = "python"
    _RE = re.compile(
        r'^(?P<ip>\S+)\s+"(?P<method>GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+'
        r'(?P<path>\S+)\s+HTTP/[\d.]+"\s+status:\s*(?P<status>\d{3})\s+'
        r'len:\s*(?P<len>\d+)\s+time:\s*(?P<time>[\d.]+)\s*$')

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        status = int(g["status"])
        level = "error" if status >= 500 else "warn" if status >= 400 else "info"
        return self._event(level=level,
                           message=f'{g["method"]} {g["path"]} → {status}',
                           source=g["ip"],
                           fields={"status": status, "bytes": int(g["len"]),
                                   "duration_s": float(g["time"])}, raw=line)


# ── Proxmox pveproxy access.log ────────────────────────────────────────────────
#   192.168.1.50 - root@pam [21/07/2026:09:15:01 +0200] "GET /api2/json/… " 200 1843
#   (CLF-shaped, but the user is <user>@<realm> and the date is NUMERIC DD/MM/YYYY)
class PveProxyAdapter(LogAdapter):
    name = "pveproxy"
    language = "any"
    _RE = re.compile(
        r'^(?P<ip>\S+)\s+-\s+(?P<user>\S+@\S+|-)\s+'
        r'\[(?P<dy>\d{2})/(?P<mo>\d{2})/(?P<yr>\d{4}):(?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})'
        r'\s+(?P<off>[+-]\d{4})\]\s+"(?P<req>[A-Z]+\s+/api2/\S*[^"]*)"\s+'
        r'(?P<status>\d{3})\s+(?P<size>\S+)')

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        ts_ms = None
        try:
            from datetime import timedelta
            sign = 1 if g["off"][0] == "+" else -1
            tzinfo = timezone(sign * timedelta(hours=int(g["off"][1:3]),
                                               minutes=int(g["off"][3:5])))
            ts_ms = datetime(int(g["yr"]), int(g["mo"]), int(g["dy"]),
                             int(g["hh"]), int(g["mi"]), int(g["ss"]),
                             tzinfo=tzinfo).timestamp() * 1000.0
        except ValueError:
            ts_ms = None
        status = int(g["status"])
        level = "error" if status >= 500 else "warn" if status >= 400 else "info"
        return self._event(level=level, message=f'{g["req"]} → {status}',
                           source=g["ip"], ts_ms=ts_ms,
                           fields={"user": g["user"], "status": status,
                                   "bytes": g["size"]}, raw=line)


# ── Registration ─────────────────────────────────────────────────────────────
# vdsm before ovirt_engine is irrelevant (paren/bracket order differs); both
# default-place — their ",mmm±TZ LEVEL" prefix can't match python_logging.
for _a in (OpenStackOsloAdapter(), EsxiVmkernelAdapter(), VmwareVmacoreAdapter(),
           ProxmoxTaskAdapter(), OvirtEngineAdapter(), VdsmLogAdapter(),
           # batch 5
           EsxiVobdAdapter(), NutanixGenesisAdapter(), OvsVlogAdapter(),
           SwiftProxyAdapter(), EventletWsgiAdapter(), PveProxyAdapter()):
    register_adapter(_a)


# ═════════════════════════════════════════════════════════════════════════════
# BATCH 6
# ═════════════════════════════════════════════════════════════════════════════

# ── vCenter vpxd event record ─────────────────────────────────────────────────
#   Event [7019107] [1-1] [2022-02-11T09:08:57.995273Z] [vim.event.VmMessageErrorEvent]
#   [error] [User] [DC] [7019107] [Error message on VM …]
class VcenterVpxdEventAdapter(LogAdapter):
    name = "vcenter_vpxd"
    language = "any"
    _RE = re.compile(
        r"^Event \[(?P<eid>\d+)\] \[1-1\] \[(?P<ts>[^\]]+)\] "
        r"\[(?P<etype>vim\.event\.[\w.]+)\]\s*(?P<rest>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        brackets = re.findall(r"\[([^\]]*)\]", g["rest"])
        sev = brackets[0].lower() if brackets else ""
        level = {"error": "error", "warning": "warn", "info": "info",
                 "user": "info"}.get(sev, "info")
        msg = brackets[-1] if brackets else g["rest"]
        fields = {"event_id": int(g["eid"]), "event_type": g["etype"]}
        if sev in ("error", "warning", "info"):
            fields["severity"] = sev
        return self._event(level=level, message=msg, source="vpxd.event",
                           ts_ms=parse_timestamp(g["ts"]), fields=fields, raw=line)


register_adapter(VcenterVpxdEventAdapter())


# ═════════════════════════════════════════════════════════════════════════════
#  BATCH 7 — VCSA Python services + nova [instance:] message prefix
# ═════════════════════════════════════════════════════════════════════════════


# ── VMware vCenter Python services (vmon/content-library/analytics …) ─────────
#   2022-02-11T09:09:03.857Z info vmon-svc[04733] [vmon@6876 sub=SvcCtl] <analytics> msg
class VmwarePythonAdapter(LogAdapter):
    name = "vmware_python"
    language = "python"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+"
        r"(?P<lvl>debug|info|notice|warning|error|critical|verbose|trivia)\s+"
        r"(?P<proc>[\w\-]+)\[(?P<pid>\d+)\]\s+"
        r"(?:\[(?P<ctx>[^\]]*)\]\s*)?(?P<msg>.*)$")
    _LVL = {"verbose": "debug", "trivia": "trace", "notice": "info"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {"pid": int(g["pid"])}
        if g.get("ctx"):
            fields["context"] = g["ctx"]
            sm = re.search(r"sub=(\S+)", g["ctx"])
            if sm:
                fields["sub"] = sm.group(1)
        return self._event(level=self._LVL.get(g["lvl"], g["lvl"]),
                           message=g["msg"], source=g["proc"],
                           ts_ms=parse_timestamp(g["ts"]), fields=fields, raw=line)


# ── OpenStack nova oslo "[instance: <uuid>]" message body ─────────────────────
#   [instance: b9000564-fe1a-409b-b8cc-1e88b294cd1d] VM Paused (Lifecycle Event)
class OpenstackInstanceAdapter(LogAdapter):
    name = "openstack_instance"
    language = "python"
    _RE = re.compile(
        r"^\[instance: (?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12})\]\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        low = g["msg"].lower()
        level = ("error" if "error" in low or "failed" in low else "info")
        return self._event(level=level, message=g["msg"], source="nova.instance",
                           fields={"instance_uuid": g["uuid"]},
                           trace_id=g["uuid"], raw=line)


for _a in (VmwarePythonAdapter(), OpenstackInstanceAdapter()):
    register_adapter(_a)
