"""
Cloud / serverless / container-control-plane log adapters (BATCH 2)
================================================================================
Text (non-JSON) cloud formats. Every JSON-based cloud log — CloudTrail,
GuardDuty, VPC/NSG flow logs, GCP LogEntry, Azure resource logs, Lambda JSON,
etcd-zap-json — is already normalized by the `jsonl` super-adapter, so only the
plain-text shapes are implemented here.

Formats: aws_alb, aws_s3_access, aws_lambda_text, cloud_init, etcd_capnslog,
gcf_text.
"""
from __future__ import annotations

import re
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp, ratio_detect)


# ── AWS Application Load Balancer access log ─────────────────────────────────
#   https 2018-07-02T22:23:00.186641Z app/my-lb/50dc.. 192.168.131.39:2817 \
#   10.0.0.1:80 0.086 0.048 0.037 200 200 0 57 "GET https://www.example.com HTTP/1.1" ...
class AwsAlbAdapter(LogAdapter):
    name = "aws_alb"
    language = "any"
    _RE = re.compile(
        r"^(?P<type>https?|h2|grpcs?|ws|wss)\s+"
        r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s+"
        r"(?P<elb>app/\S+|net/\S+|\S+)\s+"
        r"(?P<client>\S+?:\d+|-)\s+(?P<target>\S+?:\d+|-)\s+"
        r"(?P<rpt>[-\d.]+)\s+(?P<tpt>[-\d.]+)\s+(?P<respt>[-\d.]+)\s+"
        r"(?P<elb_status>\d{3}|-)\s+(?P<target_status>\d{3}|-)\s+"
        r"(?P<rx>\d+)\s+(?P<tx>\d+)\s+\"(?P<req>[^\"]*)\"")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        status = int(g["elb_status"]) if g["elb_status"].isdigit() else None
        level = ("error" if status and status >= 500 else "warn"
                 if status and status >= 400 else "info")
        return self._event(level=level,
                           message=f'{g["req"]} → {g["elb_status"]}', source="aws.alb",
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"elb": g["elb"], "client": g["client"],
                                   "target": g["target"], "elb_status": g["elb_status"],
                                   "target_status": g["target_status"],
                                   "received_bytes": int(g["rx"]),
                                   "sent_bytes": int(g["tx"]), "request": g["req"],
                                   "connection_type": g["type"]}, raw=line)


# ── AWS S3 server access log ─────────────────────────────────────────────────
#   79a59df...2be amzn-s3-demo-bucket1 [06/Feb/2019:00:00:38 +0000] 192.0.2.3 ...
class AwsS3AccessAdapter(LogAdapter):
    name = "aws_s3_access"
    language = "any"
    _RE = re.compile(
        r"^(?P<owner>[0-9a-f]{64})\s+(?P<bucket>\S+)\s+"
        r"\[(?P<ts>\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2}\s*[+-]\d{4})\]\s+"
        r"(?P<remote_ip>\S+)\s+(?P<requester>\S+)\s+(?P<request_id>\S+)\s+"
        r"(?P<operation>\S+)\s+(?P<key>\S+)\s+"
        r'(?:"(?P<req>[^"]*)"|-)\s+(?P<status>\d{3}|-)')

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        status = int(g["status"]) if g["status"].isdigit() else None
        level = ("error" if status and status >= 500 else "warn"
                 if status and status >= 400 else "info")
        return self._event(level=level,
                           message=f'{g["operation"]} {g["key"]} → {g["status"]}',
                           source=f'aws.s3.{g["bucket"]}',
                           ts_ms=parse_timestamp(g["ts"]), trace_id=g["request_id"],
                           fields={"bucket": g["bucket"], "remote_ip": g["remote_ip"],
                                   "operation": g["operation"], "key": g["key"],
                                   "status": status, "request_id": g["request_id"]},
                           raw=line)


# ── AWS Lambda platform text lines (CloudWatch) ──────────────────────────────
#   START RequestId: 57f2.. Version: $LATEST
#   REPORT RequestId: 57f2.. Duration: 79.67 ms Billed Duration: 80 ms ...
class AwsLambdaTextAdapter(LogAdapter):
    name = "aws_lambda_text"
    language = "any"
    _RE = re.compile(r"^(?P<kind>START|END|REPORT)\s+RequestId:\s+(?P<rid>[0-9a-f-]{16,})(?P<rest>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {"phase": g["kind"], "request_id": g["rid"]}
        for k, v, _unit in re.findall(r"([\w ]+?):\s*([\d.]+)\s*(ms|MB)?", g["rest"]):
            fields[k.strip().lower().replace(" ", "_")] = (f"{v} {_unit}".strip()
                                                           if _unit else v.strip())
        low = g["rest"].lower()
        level = "error" if ("status: 'timeout'" in low or "error" in low) else "info"
        return self._event(level=level, message=f'{g["kind"]} {g["rid"]}',
                           source="aws.lambda", trace_id=g["rid"], fields=fields, raw=line)


# ── cloud-init log ───────────────────────────────────────────────────────────
#   2019-10-10 04:51:25,321 - util.py[DEBUG]: Failed mount of '/dev/sr0' ...
class CloudInitAdapter(LogAdapter):
    name = "cloud_init"
    language = "python"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+-\s+"
        r"(?P<mod>[\w.]+\.py)\[(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\]:\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"], source=f'cloud-init.{g["mod"]}',
                           ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── etcd capnslog legacy text (<= etcd 3.3) ──────────────────────────────────
#   2019-06-10 09:25:01.639397 I | etcdmain: etcd Version: 3.4.13
class EtcdCapnslogAdapter(LogAdapter):
    name = "etcd_capnslog"
    language = "go"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6})\s+(?P<lvl>[NEWIDC])\s+\|\s+"
        r"(?P<pkg>[\w/.\-]+):\s*(?P<msg>.*)$")
    _LVL = {"N": "info", "E": "error", "W": "warn", "I": "info", "D": "debug", "C": "fatal"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=self._LVL.get(g["lvl"], "info"), message=g["msg"],
                           source=g["pkg"], ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── Google Cloud Functions / Cloud Run execution text lines ──────────────────
#   Function execution took 4999 ms, finished with status: 'timeout'
class GcfTextAdapter(LogAdapter):
    name = "gcf_text"
    language = "any"
    _RE = re.compile(
        r"^Function execution (?:started$|took (?P<ms>\d+) ms, finished with "
        r"status(?::| code:)\s*'?(?P<status>[\w]+)'?)")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        g = m.groupdict()
        status = (g.get("status") or "").lower()
        level = "error" if status in ("timeout", "error", "crash", "connection error") else "info"
        fields = {}
        if g.get("ms"):
            fields["duration_ms"] = int(g["ms"])
        if g.get("status"):
            fields["status"] = g["status"]
        return self._event(level=level, message=s, source="gcp.functions", fields=fields, raw=line)


# ═══════════════════════════ BATCH 5 ═════════════════════════════════════════

# ── AWS Classic ELB access log ────────────────────────────────────────────────
#   2015-05-13T23:39:43.945958Z my-loadbalancer 192.168.131.39:2817 10.0.0.1:80
#   0.000073 0.001048 0.000057 200 200 0 29 "GET http://… HTTP/1.1" "curl/7.38.0" - -
#   (unlike aws_alb there is NO leading type token — the ISO ts comes first)
class AwsElbClassicAdapter(LogAdapter):
    name = "aws_elb_classic"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s+"
        r"(?P<elb>[\w.\-]+)\s+"
        r"(?P<client>\S+:\d+)\s+(?P<backend>\S+:\d+|-)\s+"
        r"(?P<rqt>[-\d.]+)\s+(?P<bpt>[-\d.]+)\s+(?P<rspt>[-\d.]+)\s+"
        r"(?P<elb_status>\d{3}|-)\s+(?P<be_status>\d{3}|-)\s+"
        r"(?P<rx>\d+)\s+(?P<tx>\d+)\s+\"(?P<req>[^\"]*)\"")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        status = int(g["elb_status"]) if g["elb_status"].isdigit() else None
        level = ("error" if status and status >= 500 else "warn"
                 if status and status >= 400 else "info")
        return self._event(level=level,
                           message=f'{g["req"]} → {g["elb_status"]}',
                           source=f'elb.{g["elb"]}', ts_ms=parse_timestamp(g["ts"]),
                           fields={"client": g["client"], "backend": g["backend"],
                                   "elb_status": g["elb_status"],
                                   "backend_status": g["be_status"],
                                   "received_bytes": int(g["rx"]),
                                   "sent_bytes": int(g["tx"]),
                                   "request": g["req"]}, raw=line)


# ── AWS VPC Flow Logs (text rows, default v2 + custom formats) ────────────────
#   2 123456789010 eni-1235b8ca123456789 172.31.16.139 172.31.16.21 20641 22 6
#   20 4249 1418530010 1418530070 ACCEPT OK
class AwsVpcFlowAdapter(LogAdapter):
    name = "aws_vpc_flow"
    language = "any"
    _DEFAULT = re.compile(
        r"^(?P<ver>\d)\s+(?P<acct>\d{12}|unknown|-)\s+(?P<eni>eni-[0-9a-f]+)\s+"
        r"(?P<src>\S+)\s+(?P<dst>\S+)\s+(?P<sport>\d+|-)\s+(?P<dport>\d+|-)\s+"
        r"(?P<proto>\d+|-)\s+(?P<pkts>\d+|-)\s+(?P<bytes>\d+|-)\s+"
        r"(?P<start>\d{9,11})\s+(?P<end>\d{9,11})\s+"
        r"(?P<action>ACCEPT|REJECT|-)\s+(?P<status>OK|NODATA|SKIPDATA)\s*$")
    _HEADER = re.compile(r"^version\s+account-id\s+interface-id\b")
    _RESOURCE = re.compile(r"\b(?:vpc|subnet|eni|i)-[0-9a-f]{8,17}\b")

    def _custom(self, s: str) -> bool:
        toks = s.split()
        return (len(toks) >= 8
                and any(t in ("ACCEPT", "REJECT") for t in toks)
                and any(t in ("ingress", "egress") for t in toks)
                and bool(self._RESOURCE.search(s)))

    def detect(self, sample_lines):
        def ok(ln):
            s = ln.strip()
            return bool(self._DEFAULT.match(s) or self._HEADER.match(s)
                        or self._custom(s))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if self._HEADER.match(s):
            return self._event(level="", message=s, source="aws.vpcflow",
                               fields={"header": True}, raw=line)
        m = self._DEFAULT.match(s)
        if m:
            g = m.groupdict()
            level = "warn" if g["action"] == "REJECT" else "info"
            ts_ms = float(g["start"]) * 1000.0 if g["start"].isdigit() else None
            return self._event(
                level=level,
                message=f'{g["src"]}:{g["sport"]} → {g["dst"]}:{g["dport"]} '
                        f'{g["action"]}',
                source=g["eni"], ts_ms=ts_ms, category="event",
                fields={"action": g["action"], "log_status": g["status"],
                        "protocol": g["proto"], "packets": g["pkts"],
                        "bytes": g["bytes"], "account": g["acct"]}, raw=line)
        if self._custom(s):
            toks = s.split()
            action = next((t for t in toks if t in ("ACCEPT", "REJECT")), "-")
            direction = next((t for t in toks if t in ("ingress", "egress")), None)
            eni = next((t for t in toks if t.startswith("eni-")), None)
            epoch = next((t for t in toks if t.isdigit() and 9 <= len(t) <= 11), None)
            ips = [t for t in toks if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", t)]
            level = "warn" if action == "REJECT" else "info"
            return self._event(
                level=level,
                message=f'{" → ".join(ips[:2]) if len(ips) >= 2 else "flow"} '
                        f'{action} {direction or ""}'.strip(),
                source=eni or "aws.vpcflow",
                ts_ms=float(epoch) * 1000.0 if epoch else None, category="event",
                fields={"action": action, "direction": direction,
                        "tokens": len(toks)}, raw=line)
        return None


# ── Azure Functions host log (console/file text form) ─────────────────────────
#   2023-06-08T06:07:53Z [Information] Executing 'Functions.approval' (Reason=…)
class AzureFunctionsAdapter(LogAdapter):
    name = "azure_functions"
    language = "dotnet"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+"
        r"\[(?P<level>Information|Warning|Error|Debug|Trace|Critical|Verbose)\]\s+"
        r"(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"]
        fields = None
        fm = re.search(r"'Functions\.(\w+)'", msg)
        idm = re.search(r"\bId=([0-9a-f\-]{8,})", msg)
        if fm or idm:
            fields = {}
            if fm:
                fields["function"] = fm.group(1)
        failed = "Failed" in msg and ("Executed" in msg or "Function" in msg)
        level = "error" if failed else g["level"]
        return self._event(level=level, message=msg, source="azure.functions",
                           ts_ms=parse_timestamp(g["ts"]),
                           trace_id=idm.group(1) if idm else None,
                           fields=fields, raw=line)


# ── Registration ─────────────────────────────────────────────────────────────
for _a in (AwsAlbAdapter(), AwsS3AccessAdapter(), AwsLambdaTextAdapter(),
           CloudInitAdapter(), EtcdCapnslogAdapter(), GcfTextAdapter(),
           # batch 5
           AwsElbClassicAdapter(), AwsVpcFlowAdapter(), AzureFunctionsAdapter()):
    register_adapter(_a)


# ═════════════════════════════════════════════════════════════════════════════
# BATCH 6
# ═════════════════════════════════════════════════════════════════════════════
from ._common import multiline_ratio_detect  # noqa: E402
from typing import Optional as _Opt  # noqa: E402


# ── AWS Network Load Balancer access log (TLS listeners) ─────────────────────
#   tls 2.0 2018-12-20T02:59:40 net/my-network-loadbalancer/c6e77e28c25b2234 …
class AwsNlbAdapter(LogAdapter):
    name = "aws_nlb"
    language = "any"
    _RE = re.compile(
        r"^(?P<type>tls)\s+(?P<ver>[\d.]+)\s+"
        r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)\s+"
        r"(?P<elb>net/[\w\-./]+)\s+(?P<listener>\S+)\s+"
        r"(?P<client>\S+)\s+(?P<dest>\S+)\s+(?P<conn_ms>\d+)\s+(?P<hs_ms>[\d\-]+)\s+"
        r"(?P<rx>\d+)\s+(?P<tx>\d+)\s+(?P<rest>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> _Opt[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        cipher = tlsver = ""
        cm = re.search(r"\s([A-Z0-9\-]{8,})\s+(tlsv\d+[\w.]*)\s", " " + g["rest"] + " ")
        if cm:
            cipher, tlsver = cm.group(1), cm.group(2)
        fields = {"lb": g["elb"], "client": g["client"], "destination": g["dest"],
                  "rx_bytes": int(g["rx"]), "tx_bytes": int(g["tx"]),
                  "connection_ms": int(g["conn_ms"])}
        if cipher:
            fields.update({"cipher": cipher, "tls_version": tlsver})
        return self._event(level="info",
                           message=f'tls {g["client"]} -> {g["dest"]}',
                           source="aws.nlb", ts_ms=parse_timestamp(g["ts"]),
                           fields=fields, raw=line)


# ── Fly.io log line ───────────────────────────────────────────────────────────
#   2023-03-07T16:18:08Z app[5683606c41098e] lhr [info]Mounting /dev/vdb …
class FlyIoAdapter(LogAdapter):
    name = "flyio"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+"
        r"(?P<src>app|proxy|runner|health)\[(?P<machine>[0-9a-f]+)\]\s+"
        r"(?P<region>[a-z]{3})\s+\[(?P<level>debug|info|warn|error)\]\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> _Opt[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"],
                           source=f'fly.{g["src"]}', ts_ms=parse_timestamp(g["ts"]),
                           fields={"machine": g["machine"], "region": g["region"]},
                           raw=line)


for _a in (AwsNlbAdapter(), FlyIoAdapter()):
    register_adapter(_a)


# ═════════════════════════════════════════════════════════════════════════════
#  BATCH 7 — Azure Storage Analytics, CloudFront real-time TSV, Kinesis Agent
# ═════════════════════════════════════════════════════════════════════════════


# ── Azure Storage Analytics classic $logs (semicolon-delimited) ───────────────
#   1.0;2014-06-19T22:59:23.1967767Z;GetBlob;AnonymousSuccess;200;17;16;…
class AzureStorageAnalyticsAdapter(LogAdapter):
    name = "azure_storage_analytics"
    language = "any"
    _RE = re.compile(
        r"^(?P<ver>[12]\.\d);(?P<ts>\d{4}-\d{2}-\d{2}T[\d:.]+Z);(?P<op>\w+);"
        r"(?P<auth>\w+);(?P<status>\d{3});")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        g = m.groupdict()
        status = int(g["status"])
        level = ("error" if status >= 500
                 else "warn" if status >= 400 or "Failure" in g["auth"]
                 or "Error" in g["auth"] else "info")
        parts = s.split(";")
        fields = {"version": g["ver"], "operation": g["op"],
                  "auth_status": g["auth"], "status": status}
        if len(parts) > 7:
            fields["requester"] = parts[7]
        if len(parts) > 9:
            fields["account"] = parts[9]
        return self._event(level=level,
                           message=f'{g["op"]} {g["auth"]} {status}',
                           source="azure.storage", ts_ms=parse_timestamp(g["ts"]),
                           fields=fields, raw=line)


# ── CloudFront real-time logs (headerless TSV via Kinesis) ────────────────────
#   1573840000.762⇥192.0.2.100⇥0.001⇥200⇥392⇥GET⇥https⇥d111….cloudfront.net⇥/index.html
class CloudfrontRealtimeAdapter(LogAdapter):
    name = "cloudfront_realtime"
    language = "any"
    _EPOCH = re.compile(r"^\d{9,10}\.\d{1,3}$")
    _IP = re.compile(r"^[0-9a-fA-F.:]+$")

    @staticmethod
    def _fields_of(s: str) -> list:
        # rows arrive TAB-separated; escaped shipping turns tabs into the
        # literal two-char "\t" sequence — accept both.
        return re.split(r"\t|\\t", s.strip())

    def _hit(self, s: str) -> bool:
        f = self._fields_of(s)
        return (len(f) >= 5 and bool(self._EPOCH.match(f[0]))
                and bool(self._IP.match(f[1]))
                and ("." in f[1] or ":" in f[1]))

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: self._hit(str(ln)))

    def parse_line(self, line: str) -> Optional[dict]:
        if not self._hit(str(line)):
            return None
        f = self._fields_of(str(line))
        fields = {"client_ip": f[1]}
        status = None
        # default field order: timestamp c-ip time-to-first-byte sc-status …
        if len(f) > 3 and re.match(r"^\d{3}$", f[3]):
            status = int(f[3])
            fields["status"] = status
        for idx, key in ((2, "time_to_first_byte"), (4, "sc_bytes"),
                         (5, "method"), (6, "protocol"), (7, "host"), (8, "uri")):
            if idx < len(f):
                fields[key] = f[idx]
        level = ("error" if status and status >= 500
                 else "warn" if status and status >= 400 else "info")
        msg = " ".join(x for x in (fields.get("method"), fields.get("uri"),
                                   str(status) if status else None) if x) \
            or f"cloudfront realtime record ({len(f)} fields)"
        return self._event(level=level, message=msg, source="cloudfront.realtime",
                           ts_ms=float(f[0]) * 1000.0, fields=fields, raw=line)


# ── AWS Kinesis Agent log (aws-kinesis-agent.log) ─────────────────────────────
#   2021-01-29 03:04:51.573+0000  (Agent.MetricsEmitter RUNNING) com.amazon.kinesis.… [INFO] msg
class KinesisAgentAdapter(LogAdapter):
    name = "kinesis_agent"
    language = "java"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?[+-]\d{4})\s+"
        r"\((?P<thr>[^)]+)\)\s+(?P<cls>com\.amazon\.kinesis\.[\w.]+)\s+"
        r"\[(?P<lvl>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\]\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["lvl"], message=g["msg"], source=g["cls"],
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"thread": g["thr"]}, raw=line)


for _a in (AzureStorageAnalyticsAdapter(), CloudfrontRealtimeAdapter(),
           KinesisAgentAdapter()):
    register_adapter(_a)
