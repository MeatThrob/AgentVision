"""
Observability-agent / telemetry-pipeline log adapters (BATCH 3)
================================================================================
The internal logs of the agents that ship OTHER programs' logs and metrics —
plus the structured-console formats (structlog, Heroku router) that sit next
to them on an app dyno. JSON variants of all of these are already served by
the `jsonl` super-adapter; only the plain-text renderings live here.

Formats: fluentbit, fluentd, telegraf, rust_tracing (vector/qdrant/any
tracing-subscriber fmt), datadog_agent, fail2ban, airflow_task,
structlog_console, heroku_router.
"""
from __future__ import annotations

import re
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp,
                      ratio_detect, multiline_ratio_detect)


# ── Fluent Bit internal log ──────────────────────────────────────────────────
#   [2024/01/15 10:30:00] [ info] [engine] started (pid=1)
class FluentBitAdapter(LogAdapter):
    name = "fluentbit"
    language = "any"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\]\s+"
        r"\[\s*(?P<lvl>trace|debug|info|warn|error)\s*\]\s+"
        r"(?:\[(?P<comp>[^\]]+)\]\s*)?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["lvl"], message=g["msg"],
                           source=f'fluent-bit.{g["comp"]}' if g["comp"] else "fluent-bit",
                           ts_ms=parse_timestamp(g["ts"].replace("/", "-")), raw=line)


# ── Fluentd internal log ─────────────────────────────────────────────────────
#   2023-01-01 12:00:00 +0000 [info]: #0 fluentd worker is now running worker=0
class FluentdAdapter(LogAdapter):
    name = "fluentd"
    language = "ruby"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4})\s+"
        r"\[(?P<lvl>trace|debug|info|warn|error|fatal)\]:\s+"
        r"(?:#(?P<worker>\d+)\s+)?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["lvl"], message=g["msg"], source="fluentd",
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"worker": int(g["worker"])} if g["worker"] else None,
                           raw=line)


# ── Telegraf internal log ────────────────────────────────────────────────────
#   2023-01-01T00:00:00Z I! [agent] Config: Interval:10s, …
class TelegrafAdapter(LogAdapter):
    name = "telegraf"
    language = "go"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+"
        r"(?P<lvl>[TDIWE])!\s+(?:\[(?P<comp>[^\]]+)\]\s*)?(?P<msg>.*)$")
    _LVL = {"T": "trace", "D": "debug", "I": "info", "W": "warn", "E": "error"}

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=self._LVL.get(g["lvl"], g["lvl"]), message=g["msg"],
                           source=f'telegraf.{g["comp"]}' if g["comp"] else "telegraf",
                           ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── Rust tracing-subscriber fmt layer (Vector, Qdrant, most Rust services) ───
#   2024-01-15T10:30:00.123456Z  INFO vector::app: Log level is enabled. level="info"
class RustTracingAdapter(LogAdapter):
    name = "rust_tracing"
    language = "rust"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+(?:Z|[+-]\d{2}:?\d{2})?)\s+"
        r"(?P<lvl>TRACE|DEBUG|INFO|WARN|ERROR)\s+"
        r"(?P<target>[A-Za-z_][\w]*(?:::[\w]+)+):\s+(?P<msg>.*)$")
    # BATCH-7 gap fix: tracing-fmt pads the level to width 5 (two spaces before
    # INFO/WARN) and a top-level target has NO :: (meilisearch, index_scheduler).
    # Accept the single-segment form only with that distinctive padding, so a
    # generic "ISO INFO word: msg" line still belongs to the generic adapters.
    _RE1 = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+(?:Z|[+-]\d{2}:?\d{2})?)"
        r"\s{2,}(?P<lvl>TRACE|DEBUG|INFO|WARN|ERROR)\s+"
        r"(?P<target>[a-z_][\w]*):\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines,
            lambda ln: bool(self._RE.match(ln.strip()) or self._RE1.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s) or self._RE1.match(s)
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["lvl"], message=g["msg"], source=g["target"],
                           ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── Datadog agent log ────────────────────────────────────────────────────────
#   2023-01-01 12:00:00 UTC | CORE | INFO | (pkg/collector/runner/runner.go:261 in work) | check:cpu | Done running check
class DatadogAgentAdapter(LogAdapter):
    name = "datadog_agent"
    language = "go"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?P<tz>[A-Z]{3,4})\s+\|\s+"
        r"(?P<agent>[A-Z-]+)\s+\|\s+(?P<lvl>TRACE|DEBUG|INFO|WARN|ERROR|CRITICAL)\s+\|\s+"
        r"\((?P<src>[^)]*)\)\s*(?:\|\s*(?P<rest>.*))?$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        # UTC-stamped (naive parse then trust the label; other zones → naive)
        ts_text = g["ts"] + ("Z" if g["tz"] in ("UTC", "GMT") else "")
        return self._event(level=g["lvl"], message=(g.get("rest") or "").strip(),
                           source=f'datadog.{g["agent"].lower()}',
                           ts_ms=parse_timestamp(ts_text.replace(" ", "T", 1)),
                           fields={"caller": g["src"]}, raw=line)


# ── fail2ban ─────────────────────────────────────────────────────────────────
#   2023-02-17 23:44:17,037 fail2ban.actions        [992]: NOTICE  [apache-auth] Ban 45.91.244.228
class Fail2banAdapter(LogAdapter):
    name = "fail2ban"
    language = "python"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+"
        r"fail2ban\.(?P<comp>[\w.]+)\s*\[(?P<pid>\d+)\]:\s+"
        r"(?P<lvl>[A-Z]+)\s+(?:\[(?P<jail>[^\]]+)\]\s*)?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {"pid": int(g["pid"])}
        if g["jail"]:
            fields["jail"] = g["jail"]
        verb = (g["msg"].split(None, 1) or [""])[0]
        if verb in ("Ban", "Unban", "Found", "Ignore", "Restore"):
            fields["action"] = verb
        return self._event(level=g["lvl"], message=g["msg"],
                           source=f'fail2ban.{g["comp"]}',
                           ts_ms=parse_timestamp(g["ts"]), fields=fields, raw=line)


# ── Apache Airflow task log ──────────────────────────────────────────────────
#   [2024-07-15T14:18:46.143+0000] {taskinstance.py:1103} INFO - Dependencies all met …
class AirflowTaskAdapter(LogAdapter):
    name = "airflow_task"
    language = "python"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:[+-]\d{4})?)\]\s+"
        r"\{(?P<src>[^}]+)\}\s+(?P<lvl>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+-\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["lvl"], message=g["msg"], source=g["src"],
                           ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── structlog ConsoleRenderer ────────────────────────────────────────────────
#   2023-11-08 15:28:26 [info     ] user logged in                 user_id=42 ip=10.0.0.1
class StructlogConsoleAdapter(LogAdapter):
    name = "structlog_console"
    language = "python"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s+"
        r"\[(?P<lvl>trace|debug|info|warning|warn|error|critical|exception)\s*\]\s+"
        r"(?P<rest>.*)$")
    _KV = re.compile(r"([A-Za-z_][\w.]*)=(\"[^\"]*\"|\S+)")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        rest = g["rest"]
        # the message is padded with spaces before the first key=value pair
        fields = {}
        kv_start = None
        for kv in self._KV.finditer(rest):
            if kv_start is None:
                kv_start = kv.start()
            fields[kv.group(1)] = kv.group(2).strip('"')
        msg = (rest[:kv_start] if kv_start is not None else rest).rstrip()
        return self._event(level=g["lvl"], message=msg or rest.strip(),
                           source=str(fields.get("logger", "structlog")),
                           ts_ms=parse_timestamp(g["ts"]),
                           fields=fields or None, raw=line)


# ── Heroku router (logfmt dialect keyed by at=) ──────────────────────────────
#   at=info method=GET path="/db" host=… status=301 bytes=462 protocol=https
class HerokuRouterAdapter(LogAdapter):
    name = "heroku_router"
    language = "any"
    _PAIR = re.compile(r'([\w.\-]+)=("(?:[^"\\]|\\.)*"|\S*)')

    def _pairs(self, line: str) -> dict:
        out = {}
        for k, v in self._PAIR.findall(line):
            if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
                v = v[1:-1]
            out[k] = v
        return out

    def detect(self, sample_lines):
        def hit(ln):
            s = str(ln).strip()
            if not s.startswith("at="):
                return False
            p = self._pairs(s)
            return "method" in p and "path" in p
        return multiline_ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if not s.startswith("at="):
            return None
        p = self._pairs(s)
        if "method" not in p:
            return None
        at = p.get("at", "info")
        status = p.get("status", "")
        level = at
        if status.isdigit():
            sc = int(status)
            level = "error" if (at == "error" or sc >= 500) else \
                    "warn" if sc >= 400 else at
        msg = f'{p.get("method", "")} {p.get("path", "")} → {status or at}'.strip()
        fields = {k: v for k, v in p.items() if k != "at"}
        return self._event(level=level, message=msg, source="heroku.router",
                           trace_id=p.get("request_id"), fields=fields, raw=line)


# ── Registration ─────────────────────────────────────────────────────────────
# fail2ban / airflow / structlog share the "ISO timestamp then level" silhouette
# with the generic python_logging adapter → insert before it so the specific
# grammar wins any confidence tie. heroku_router is a logfmt dialect → before
# logfmt for the same reason.
for _a in (Fail2banAdapter(), AirflowTaskAdapter(), StructlogConsoleAdapter()):
    register_adapter(_a, before="python_logging")
register_adapter(HerokuRouterAdapter(), before="logfmt")
for _a in (FluentBitAdapter(), FluentdAdapter(), TelegrafAdapter(),
           RustTracingAdapter(), DatadogAgentAdapter()):
    register_adapter(_a)


# ═════════════════════════════════════════════════════════════════════════════
# BATCH 6 — monitoring servers' own logs
# ═════════════════════════════════════════════════════════════════════════════
from ._common import mk_ts  # noqa: E402


# ── Nagios Core nagios.log ────────────────────────────────────────────────────
#   [1672531200] SERVICE ALERT: web01;HTTP;CRITICAL;SOFT;1;CRITICAL - Socket timeout
class NagiosAdapter(LogAdapter):
    name = "nagios"
    language = "any"
    _RE = re.compile(r"^\[(?P<epoch>\d{9,10})\]\s+(?P<type>[A-Z][A-Z _]+?):\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        etype, msg = g["type"], g["msg"]
        level = "info"
        if "ALERT" in etype or "FLAPPING" in etype:
            if re.search(r"\b(CRITICAL|DOWN|UNREACHABLE)\b", msg):
                level = "error"
            elif re.search(r"\b(WARNING|UNKNOWN)\b", msg):
                level = "warn"
        fields = {"event_type": etype}
        parts = msg.split(";")
        if "SERVICE" in etype and len(parts) >= 3:
            fields.update({"host": parts[0], "service": parts[1], "state": parts[2]})
        elif "HOST" in etype and len(parts) >= 2:
            fields.update({"host": parts[0], "state": parts[1]})
        return self._event(level=level, message=msg, source=f"nagios.{etype.lower().replace(' ', '_')}",
                           ts_ms=float(g["epoch"]) * 1000.0, fields=fields, raw=line)


# ── Icinga 2 main log ─────────────────────────────────────────────────────────
#   [2015-07-13 18:29:25 +0200] information/ApiListener: New client connection…
class Icinga2Adapter(LogAdapter):
    name = "icinga2"
    language = "any"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4})\]\s+"
        r"(?P<level>debug|notice|information|warning|critical)/(?P<facility>[\w\-]+):\s?"
        r"(?P<msg>.*)$")
    _LVL = {"information": "info", "notice": "info", "critical": "error"}

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        ts = g["ts"].replace(" +", "+").replace(" -", "-")
        return self._event(level=self._LVL.get(g["level"], g["level"]),
                           message=g["msg"], source=f'icinga2.{g["facility"]}',
                           ts_ms=parse_timestamp(ts), raw=line)


# ── Zabbix daemon log (PID:YYYYMMDD:HHMMSS.mmm prefix) ───────────────────────
#   12345:20230101:120000.123 database is down: reconnecting in 10 seconds
class ZabbixAdapter(LogAdapter):
    name = "zabbix"
    language = "any"
    _RE = re.compile(
        r"^\s*(?P<pid>\d+):(?P<date>\d{8}):(?P<time>\d{6})\.(?P<ms>\d{3})\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln)))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        d, t = g["date"], g["time"]
        ts_ms = mk_ts(d[:4], d[4:6], d[6:8], t[:2], t[2:4], t[4:6],
                      int(g["ms"]) * 1000)
        msg = g["msg"]
        low = msg.lower()
        level = ("error" if any(w in low for w in ("error", "cannot", "failed",
                                                   "is down", "crit"))
                 else "warn" if "warning" in low or "slow" in low else "info")
        return self._event(level=level, message=msg, source="zabbix",
                           ts_ms=ts_ms, fields={"pid": int(g["pid"])}, raw=line)


# ── collectd LogFile plugin ───────────────────────────────────────────────────
#   [2023-01-01 00:00:00] plugin_load: plugin "cpu" successfully loaded.
class CollectdAdapter(LogAdapter):
    name = "collectd"
    language = "any"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+"
        r"(?:\[(?P<level>debug|info|notice|warning|err|error)\]\s+)?"
        r"(?P<plugin>[a-z][\w]*(?: plugin)?):\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        level = {"err": "error", "notice": "info"}.get(g["level"], g["level"]) \
            if g["level"] else ""
        return self._event(level=level or "info", message=g["msg"],
                           source=f'collectd.{g["plugin"].replace(" plugin", "")}',
                           ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── Netdata error.log ─────────────────────────────────────────────────────────
#   2023-01-01 12:00:00: netdata INFO  : MAIN : netdata started on pid 1234.
class NetdataAdapter(LogAdapter):
    name = "netdata"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}):\s+"
        r"(?P<prog>netdata|apps\.plugin|go\.d|python\.d)\s+"
        r"(?P<level>DEBUG|INFO|ERROR|FATAL)\s*:\s+(?P<thread>[A-Z0-9_\[\]]+)\s*:\s?"
        r"(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"],
                           source=f'{g["prog"]}.{g["thread"]}',
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"thread": g["thread"]}, raw=line)


# ── Graylog server.log (log4j2: ISO+offset LEVEL [Class] msg) ────────────────
#   2023-01-01T12:00:00.123+02:00 INFO  [ServerBootstrap] Graylog server up and running.
class GraylogAdapter(LogAdapter):
    name = "graylog"
    language = "java"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2})\s+"
        r"(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+"
        r"\[(?P<logger>[A-Za-z][\w.$]*)\]\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"],
                           source=f'graylog.{g["logger"]}',
                           ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── splunkd.log (MM-DD-YYYY date order!) ──────────────────────────────────────
#   01-01-2023 12:00:00.123 +0000 INFO  TailReader [12345 tailreader0] - Batch input…
class SplunkdAdapter(LogAdapter):
    name = "splunkd"
    language = "any"
    _RE = re.compile(
        r"^(?P<mo>\d{2})-(?P<dy>\d{2})-(?P<yr>\d{4})\s+"
        r"(?P<time>\d{2}:\d{2}:\d{2})\.(?P<ms>\d{3})\s+(?P<tz>[+-]\d{4})\s+"
        r"(?P<level>DEBUG|INFO|WARN|ERROR|FATAL|CRIT)\s+"
        r"(?P<comp>\w+)\s+(?:\[(?P<thread>[^\]]+)\]\s+)?-\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        iso = (f'{g["yr"]}-{g["mo"]}-{g["dy"]}T{g["time"]}.{g["ms"]}'
               f'{g["tz"][:3]}:{g["tz"][3:]}')
        fields = {"component": g["comp"]}
        if g["thread"]:
            fields["thread"] = g["thread"]
        return self._event(level=g["level"], message=g["msg"],
                           source=f'splunkd.{g["comp"]}',
                           ts_ms=parse_timestamp(iso), fields=fields, raw=line)


# ── Batch-6 registration ──────────────────────────────────────────────────────
for _a in (NagiosAdapter(), Icinga2Adapter(), ZabbixAdapter(), CollectdAdapter(),
           NetdataAdapter(), GraylogAdapter(), SplunkdAdapter()):
    register_adapter(_a)


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH 8 — monit, riemann
# ══════════════════════════════════════════════════════════════════════════════
import re as _re8  # noqa: E402
from datetime import datetime as _dt8  # noqa: E402
from ._common import (RxAdapter, ratio_detect as _rd8, _MONTHS as _M8,  # noqa: E402
                      _to_ms as _tm8)


# ── monit process supervisor log ──────────────────────────────────────────────
#   [UTC Jan  1 12:00:00] info     : 'system' Monit 5.33.0 started
class MonitLogAdapter(RxAdapter):
    name = "monit_log"
    language = "any"
    default_source = "monit"
    _RE = _re8.compile(
        r"^\[(?:(?P<zone>[A-Z]{2,4}) )?(?P<mon>[A-Z][a-z]{2})\s+(?P<dy>\d{1,2}) "
        r"(?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})\]\s+"
        r"(?P<level>debug|info|warning|error|critical)\s*:\s*(?P<msg>.*)$")

    def _ts(self, g):
        if g["mon"] not in _M8:
            return None
        try:
            return _tm8(_dt8(_dt8.now().year, _M8[g["mon"]], int(g["dy"]),
                             int(g["hh"]), int(g["mi"]), int(g["ss"])))
        except ValueError:
            return None


# ── riemann monitoring event stream (clojure log4j-ish) ───────────────────────
#   INFO [2023-01-01 00:00:00,000] main - riemann.bin - PID 1234, prepping …
class RiemannLogAdapter(RxAdapter):
    name = "riemann_log"
    language = "clojure"
    default_source = "riemann"
    _RE = _re8.compile(
        r"^(?P<level>DEBUG|INFO|WARN|ERROR|FATAL)\s+\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\]\s+"
        r"(?P<thread>[\w\-]+)\s+-\s+(?P<ns>riemann\.[\w.\-]+)\s+-\s+(?P<msg>.*)$")

    def _ts(self, g):
        from ._common import parse_timestamp as _pt
        return _pt(g["ts"].replace(",", "."))

    def _fields(self, g, line):
        return {"thread": g["thread"], "namespace": g["ns"]}

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            ev["source"] = self._RE.match(line.strip()).group("ns")
        return ev


for _a in (MonitLogAdapter(), RiemannLogAdapter()):
    register_adapter(_a)
