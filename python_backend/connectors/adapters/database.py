"""
Database / datastore server-log adapters (BATCH 2)
================================================================================
Server logs for the databases teams actually run in production. All pure-stdlib,
all normalized to the unified event schema (errors → category=="error" so the
bridge's failure detector + bookmarks fire on ORA-/panic/Error lines).

Formats: mssql_errorlog, oracle_alert, db2_diag, clickhouse, cockroachdb,
scylladb, aerospike, elastic_stack, couchbase_memcached.

NOTE: PostgreSQL stderr + MySQL 8 error are already covered by the core
`database` adapter; every JSON-based DB log (MongoDB 4.4+, etcd-zap-json, MySQL
JSON) is covered by the `jsonl` super-adapter — those are deliberately not
re-implemented here.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp,
                      _MONTHS, _to_ms, ratio_detect, multiline_ratio_detect,
                      split_any, block_ratio)


# ── Microsoft SQL Server ERRORLOG (Windows & Linux) ──────────────────────────
#   2022-07-08 05:42:10.36 Server      Server process ID is 396.
class MsSqlErrorLogAdapter(LogAdapter):
    name = "mssql_errorlog"
    language = "any"
    # classic 2-DIGIT fractional second + a source column (Server/Logon/spidNNs/
    # Backup) — the 2-digit frac is what distinguishes it from every other ISO log.
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{2})\s+"
        r"(?P<src>Server|Logon|Backup|spid\d+s?|SQLServerLogMgr)\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        # ERRORLOG records can span lines (continuation lines start with a tab),
        # so accept a multi-line block as one sample element.
        return multiline_ratio_detect(sample_lines, lambda x: bool(self._RE.match(x.strip())),
                                      threshold=0.5)

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        msg = m.group("msg")
        low = msg.lower()
        level = ("error" if ("error" in low or "fail" in low or "cannot" in low
                             or "severe" in low)
                 else "warn" if "warning" in low else "info")
        return self._event(level=level, message=msg, source=f'mssql.{m.group("src").lower()}',
                           ts_ms=parse_timestamp(m.group("ts")),
                           fields={"log_source": m.group("src")}, raw=line)


# ── Oracle Database alert log (alert_SID.log) ────────────────────────────────
#   Tue Sep 24 12:01:23 2024
#   Errors in file /u01/.../orcl_ora_12345.trc:
#   ORA-00604: error occurred at recursive SQL level 1
class OracleAlertAdapter(LogAdapter):
    name = "oracle_alert"
    language = "any"
    # a bare ctime header line, OR an ORA-/error body line
    _CTIME = re.compile(r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Z][a-z]{2}\s+\d{1,2}\s+"
                        r"\d{2}:\d{2}:\d{2}\s+\d{4}$")
    # BATCH-4 gap fix: legacy alert-log bodies also read "ORA-01555 caused by
    # SQL statement below …" (no colon after the code) — accept both forms.
    _ORA = re.compile(r"^(?P<code>ORA|TNS|PLS|RMAN|IMP|EXP)-(?P<num>\d{3,5})"
                      r"(?::\s*|\s+)(?P<msg>.*)$")
    _BODY = re.compile(r"^(Errors in file |Completed:|ALTER |Starting |Shutting down|"
                       r"Thread \d+ |Instance |Database )")

    def _block_line(self, s: str) -> bool:
        return bool(self._CTIME.match(s) or self._ORA.match(s) or self._BODY.match(s))

    def detect(self, sample_lines):
        return multiline_ratio_detect(sample_lines, lambda x: self._block_line(x.strip()),
                                      threshold=0.5)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._ORA.match(s)
        if m:
            code = f'{m.group("code")}-{m.group("num")}'
            return self._event(level="error", message=f'{code}: {m.group("msg")}',
                               source="oracle.alert", trace_id=code,
                               fields={"ora_code": code}, raw=line)
        if self._CTIME.match(s):
            return self._event(level="", message=s, source="oracle.alert",
                               ts_ms=parse_timestamp_ctime(s),
                               fields={"record_header": True}, raw=line)
        if self._BODY.match(s):
            low = s.lower()
            level = "error" if ("error" in low or "fail" in low) else "info"
            return self._event(level=level, message=s, source="oracle.alert", raw=line)
        return None


def parse_timestamp_ctime(s: str) -> Optional[float]:
    m = re.match(r"^\w{3} ([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\s+(\d{4})$", s)
    if not m or m.group(1) not in _MONTHS:
        return None
    mon, dy, hh, mm, ss, yr = m.groups()
    try:
        return _to_ms(datetime(int(yr), _MONTHS[mon], int(dy), int(hh), int(mm), int(ss)))
    except ValueError:
        return None


# ── IBM Db2 LUW diagnostic log (db2diag.log) ─────────────────────────────────
#   2007-05-18-14.20.46.973000-240 I27204F655 LEVEL: Info
#   PID : 3228 TID : 8796 PROC : db2syscs.exe
class Db2DiagAdapter(LogAdapter):
    name = "db2_diag"
    language = "any"
    _HEAD = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}-\d{2}\.\d{2}\.\d{2}\.\d{6})(?P<tz>[+-]\d{3,4})?\s+"
        r"(?P<recid>[EI]\d+[EFAH]\d+)\s+LEVEL:\s*(?P<level>Info|Event|Warning|Error|Severe|Critical)")
    _KV = re.compile(r"^(?P<key>PID|TID|PROC|INSTANCE|NODE|DB|APPHDL|APPID|EDUID|EDUNAME|"
                     r"FUNCTION|MESSAGE|DATA|HOSTNAME)\s*:\s*(?P<val>.*)$")

    def _block_line(self, s: str) -> bool:
        return bool(self._HEAD.match(s) or self._KV.match(s))

    def detect(self, sample_lines):
        return multiline_ratio_detect(sample_lines, lambda x: self._block_line(x.strip()),
                                      threshold=0.5)

    _LVL = {"Info": "info", "Event": "info", "Warning": "warn", "Error": "error",
            "Severe": "fatal", "Critical": "fatal"}

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._HEAD.match(s)
        if m:
            ts = m.group("ts").replace("-", " ", 3)  # crude → let ctime helper try
            # ts form: YYYY-MM-DD-HH.MM.SS.uuuuuu ; convert to ISO for parse_timestamp
            iso = re.sub(r"^(\d{4}-\d{2}-\d{2})-(\d{2})\.(\d{2})\.(\d{2})\.(\d{6})$",
                         r"\1 \2:\3:\4.\5", m.group("ts"))
            return self._event(level=self._LVL.get(m.group("level"), "info"),
                               message=f'LEVEL: {m.group("level")}', source="db2.diag",
                               trace_id=m.group("recid"), ts_ms=parse_timestamp(iso),
                               fields={"record_id": m.group("recid"),
                                       "db2_level": m.group("level")}, raw=line)
        m = self._KV.match(s)
        if m:
            return self._event(level="", message=s, source="db2.diag",
                               fields={m.group("key").lower(): m.group("val").strip()},
                               raw=line)
        return None


# ── ClickHouse server log ────────────────────────────────────────────────────
#   2019.01.11 15:23:25.549505 [ 45 ] {} <Error> ExternalDictionaries: Failed ...
class ClickHouseAdapter(LogAdapter):
    name = "clickhouse"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+\[\s*(?P<thread>\d+)\s*\]\s+"
        r"\{(?P<query>[^}]*)\}\s+<(?P<level>Trace|Debug|Information|Warning|Notice|Error|"
        r"Fatal|Critical)>\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        # dotted date YYYY.MM.DD → ISO for the shared parser
        iso = m.group("ts").replace(".", "-", 2)
        query = m.group("query")
        return self._event(level=m.group("level"), message=m.group("msg"),
                           source="clickhouse", ts_ms=parse_timestamp(iso),
                           trace_id=query or None,
                           fields={"thread": int(m.group("thread")),
                                   "query_id": query}, raw=line)


# ── CockroachDB log format v2 (crdb-v2) ──────────────────────────────────────
#   I210116 21:49:17.073282 14 server/node.go:464 ⋮ [-] 23  started with engine ...
class CockroachDBAdapter(LogAdapter):
    name = "cockroachdb"
    language = "any"
    # YYMMDD (6 digits, distinguishing it from klog's MMDD 4 digits) + goroutine +
    # file:line + redaction marker ⋮ + tags [..] + counter.
    _RE = re.compile(
        r"^(?P<lvl>[IWEF])(?P<yy>\d{2})(?P<mo>\d{2})(?P<dy>\d{2})\s+"
        r"(?P<time>\d{2}:\d{2}:\d{2}\.\d+)\s+(?P<goid>\d+)\s+"
        r"(?P<file>[\w./\-]+:\d+)\s+(?:⋮\s+)?(?:\[(?P<tags>[^\]]*)\]\s+)?"
        r"(?P<counter>\d+)?\s*(?P<msg>.*)$")
    _LVL = {"I": "info", "W": "warn", "E": "error", "F": "fatal"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        ts_ms = None
        try:
            hh, mm, ss = g["time"].split(":")
            sec, _, frac = ss.partition(".")
            ts_ms = _to_ms(datetime(2000 + int(g["yy"]), int(g["mo"]), int(g["dy"]),
                                    int(hh), int(mm), int(sec),
                                    int((frac or "0").ljust(6, "0")[:6])))
        except Exception:
            pass
        return self._event(level=self._LVL.get(g["lvl"], "info"), message=g["msg"],
                           source=g["file"], ts_ms=ts_ms,
                           fields={"goroutine": g["goid"], "tags": g.get("tags"),
                                   "file": g["file"]}, raw=line)


# ── ScyllaDB (seastar logger) ────────────────────────────────────────────────
#   INFO  2026-06-21 06:40:32,536 [shard 0:main] database - Loading schema table ...
class ScyllaDBAdapter(LogAdapter):
    name = "scylladb"
    language = "any"
    _RE = re.compile(
        r"^(?P<level>TRACE|DEBUG|INFO|WARN|ERROR)\s+"
        r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+"
        r"\[shard (?P<shard>\d+)(?::(?P<sname>[a-z]+))?\]\s+(?P<logger>[\w_]+)\s+-\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"], source=g["logger"],
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"shard": int(g["shard"]), "logger": g["logger"]},
                           raw=line)


# ── Aerospike server (asd) log ───────────────────────────────────────────────
#   Apr 20 2019 05:25:55 GMT: INFO (as): (as.c:372) initializing services...
class AerospikeAdapter(LogAdapter):
    name = "aerospike"
    language = "any"
    _RE = re.compile(
        r"^(?P<mon>[A-Z][a-z]{2}) (?P<dy>\d{2}) (?P<yr>\d{4}) (?P<hh>\d{2}):(?P<mm>\d{2}):(?P<ss>\d{2})"
        r" (?P<tz>\w+): (?P<level>CRITICAL|WARNING|INFO|DEBUG|DETAIL|FAILED)\s+"
        r"\((?P<ctx>[\w-]+)\):\s*\((?P<src>[^)]+)\)\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        ts_ms = None
        if g["mon"] in _MONTHS:
            try:
                ts_ms = _to_ms(datetime(int(g["yr"]), _MONTHS[g["mon"]], int(g["dy"]),
                                        int(g["hh"]), int(g["mm"]), int(g["ss"])))
            except ValueError:
                pass
        lvl = {"CRITICAL": "fatal", "FAILED": "error", "DETAIL": "debug"}.get(
            g["level"], g["level"])
        return self._event(level=lvl, message=g["msg"], source=f'aerospike.{g["ctx"]}',
                           ts_ms=ts_ms, fields={"context": g["ctx"], "source_loc": g["src"]},
                           raw=line)


# ── Elastic stack log4j2 plain (Elasticsearch server.log / Logstash) ─────────
#   [2023-01-01T00:00:00,123][INFO ][logstash.agent           ] Successfully ...
class ElasticStackAdapter(LogAdapter):
    name = "elastic_stack"
    language = "java"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2},\d{3})\]"
        r"\[(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s*\]"
        r"\[(?P<logger>[^\]]*?)\s*\](?:\s*\[(?P<node>[^\]]*)\])?\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {"logger": g["logger"].strip()}
        if g.get("node"):
            fields["node"] = g["node"].strip()
        return self._event(level=g["level"], message=g["msg"], source=g["logger"].strip(),
                           ts_ms=parse_timestamp(g["ts"]), fields=fields, raw=line)


# ── Couchbase memcached (kv-engine) log ──────────────────────────────────────
#   2018-05-24T10:53:05.159957Z INFO 53: HELO [{"a":"gocbcore/..."}] ...
class CouchbaseMemcachedAdapter(LogAdapter):
    name = "couchbase_memcached"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s+"
        r"(?P<level>TRACE|DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\s+"
        r"(?P<conn>\d+):\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"], source="couchbase.memcached",
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"connection": int(g["conn"])}, raw=line)


# ── SAP HANA trace file (BATCH 3) ────────────────────────────────────────────
#   [12345]{678}[1/1] 2024-01-15 09:01:12.456789 i indexserver  Database opened …
#   full form: [4712]{300052}[25/-1] 2024-01-15 09:01:12.456789 i TraceContext TraceContext.cpp(00923) : msg
class SapHanaAdapter(LogAdapter):
    name = "sap_hana"
    language = "any"
    _RE = re.compile(
        r"^\[(?P<pid>\d+)\]\{(?P<conn>-?\d+)\}\[(?P<tx>-?\d+/-?\d+)\]\s+"
        r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+"
        r"(?P<sev>[diwef])\s+(?P<comp>\S+)\s+(?P<msg>.*)$")
    _LVL = {"d": "debug", "i": "info", "w": "warn", "e": "error", "f": "fatal"}

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"]
        fields = {"pid": int(g["pid"]), "connection": int(g["conn"]),
                  "transaction": g["tx"]}
        sm = re.match(r"^(?P<src>[\w.\-]+\.(?:cc|cpp|h))\((?P<ln>\d+)\)\s*:\s*(?P<m2>.*)$", msg)
        if sm:
            fields["source_file"] = f'{sm.group("src")}:{int(sm.group("ln"))}'
            msg = sm.group("m2")
        return self._event(level=self._LVL.get(g["sev"], ""), message=msg,
                           source=f'hana.{g["comp"]}', ts_ms=parse_timestamp(g["ts"]),
                           fields=fields, raw=line)


# ── ArangoDB server log (BATCH 3) ────────────────────────────────────────────
#   2021-11-09T20:15:37Z [29960] INFO [144fe] {general} using storage engine 'rocksdb'
class ArangoDbAdapter(LogAdapter):
    name = "arangodb"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}))\s+"
        r"\[(?P<pid>\d+)(?:-(?P<tid>\d+))?\]\s+"
        r"(?P<lvl>TRACE|DEBUG|INFO|WARNING|ERROR|FATAL)\s+"
        r"(?:\[(?P<msgid>[0-9a-f]{5})\]\s+)?\{(?P<topic>[\w\-]+)\}\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {"pid": int(g["pid"]), "topic": g["topic"]}
        if g.get("msgid"):
            fields["message_id"] = g["msgid"]
        if g.get("tid"):
            fields["thread"] = int(g["tid"])
        return self._event(level=g["lvl"], message=g["msg"],
                           source=f'arangodb.{g["topic"]}',
                           ts_ms=parse_timestamp(g["ts"]), fields=fields, raw=line)


# ═══════════════════════════ BATCH 5 ═════════════════════════════════════════

# ── Oracle listener.log (text form) ───────────────────────────────────────────
#   14-MAY-2012 15:28:58 * (connect_data=(service_name=…)) * (address=…) *
#   establish * sales.us.example.com * 0
class OracleListenerAdapter(LogAdapter):
    name = "oracle_listener"
    language = "any"
    _RE = re.compile(
        r"^(?P<dy>\d{2})-(?P<mon>[A-Z]{3})-(?P<yr>\d{4})\s+"
        r"(?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})\s+\*\s+(?P<rest>.+)$")
    _MON3 = {m.upper(): n for m, n in _MONTHS.items()}

    def detect(self, sample_lines):
        def ok(ln):
            m = self._RE.match(ln.strip())
            # a listener entry's ' * '-separated tail ends in a numeric return code
            return bool(m and re.search(r"\*\s*\d+\s*$", m.group("rest")))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        ts_ms = None
        if g["mon"] in self._MON3:
            try:
                ts_ms = _to_ms(datetime(int(g["yr"]), self._MON3[g["mon"]],
                                        int(g["dy"]), int(g["hh"]), int(g["mi"]),
                                        int(g["ss"])))
            except ValueError:
                ts_ms = None
        parts = [p.strip() for p in g["rest"].split(" * ")]
        rc = parts[-1] if parts and re.fullmatch(r"\d+", parts[-1]) else None
        event = next((p for p in parts
                      if re.fullmatch(r"[a-z_]+", p)), "")
        level = "error" if rc not in (None, "0") else "info"
        fields = {"return_code": int(rc) if rc else None, "listener_event": event or None,
                  "segments": len(parts)}
        svc = re.search(r"service_name=([\w.\-]+)", g["rest"])
        if svc:
            fields["service"] = svc.group(1)
        host = re.search(r"host=([\w.\-]+)\)", g["rest"])
        if host:
            fields["client_host"] = host.group(1)
        return self._event(level=level,
                           message=f'{event or "listener entry"} rc={rc}',
                           source="oracle.listener", ts_ms=ts_ms,
                           fields=fields, raw=line)


# ── Oracle audit trail .aud file (OS audit destination) ───────────────────────
#   Wed Oct 21 11:58:08 2020 -04:00
#   LENGTH : '392'
#   ACTION :[151] 'select …'
#   DATABASE USER:[1] '/'
class OracleAuditAdapter(LogAdapter):
    name = "oracle_audit"
    language = "any"
    _DATE = re.compile(
        r"^[A-Z][a-z]{2}\s+(?P<mon>[A-Z][a-z]{2})\s+(?P<dy>\d{1,2})\s+"
        r"(?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})\s+(?P<yr>\d{4})"
        r"\s+(?P<off>[+-]\d{2}:\d{2})\s*$")
    _KV = re.compile(r"^(?P<key>[A-Z][A-Z0-9 /_]*[A-Z0-9])\s*:\s*(?:\[\d+\]\s*)?'(?P<val>.*)'\s*$")

    def _block_line(self, s: str) -> bool:
        st = s.strip()
        return bool(self._DATE.match(st) or self._KV.match(st))

    def detect(self, sample_lines):
        # require an audit-vocabulary key so generic KEY : 'value' dumps don't match.
        def ok(el):
            subs = split_any(el)
            has_vocab = any(re.match(r"^(ACTION|DATABASE USER|PRIVILEGE|CLIENT USER|"
                                     r"CLIENT TERMINAL|STATUS|DBID|SESSIONID|USERHOST|"
                                     r"LENGTH)\b", x.strip()) for x in subs)
            return has_vocab and block_ratio(el, lambda x: self._block_line(x))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if len(subs) > 1:                      # whole audit record → one event
            fields = {}
            ts_ms = None
            for x in subs:
                st = x.strip()
                dm = self._DATE.match(st)
                if dm:
                    g = dm.groupdict()
                    if g["mon"] in _MONTHS:
                        try:
                            from datetime import timedelta, timezone as _tz
                            sign = 1 if g["off"][0] == "+" else -1
                            tzinfo = _tz(sign * timedelta(
                                hours=int(g["off"][1:3]), minutes=int(g["off"][4:6])))
                            ts_ms = datetime(int(g["yr"]), _MONTHS[g["mon"]],
                                             int(g["dy"]), int(g["hh"]), int(g["mi"]),
                                             int(g["ss"]), tzinfo=tzinfo).timestamp() * 1000.0
                        except ValueError:
                            ts_ms = None
                    continue
                km = self._KV.match(st)
                if km:
                    fields[km.group("key")] = km.group("val")
            if not fields:
                return None
            status = fields.get("STATUS")
            level = "warn" if status not in (None, "0") else "info"
            action = fields.get("ACTION", "")
            return self._event(level=level,
                               message=action[:200] or "oracle audit record",
                               source="oracle.audit", ts_ms=ts_ms,
                               category="security", fields=fields, raw=line)
        st = s.strip()
        dm = self._DATE.match(st)
        if dm:
            return self._event(level="", message=st, source="oracle.audit",
                               category="security",
                               fields={"record_header": True}, raw=line)
        km = self._KV.match(st)
        if km:
            return self._event(level="", message=st, source="oracle.audit",
                               category="security",
                               fields={km.group("key"): km.group("val")}, raw=line)
        return None


# ── Sybase ASE errorlog ────────────────────────────────────────────────────────
#   00:0002:00000:00036:2019/03/26 01:41:39.17 server  The configuration option …
class SybaseAseAdapter(LogAdapter):
    name = "sybase_ase"
    language = "any"
    _RE = re.compile(
        r"^(?P<inst>\d{2}):(?P<eng>\d{4,5}):(?P<fam>\d{5}):(?P<spid>\d{5}):"
        r"(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d{2})\s+"
        r"(?P<fac>kernel|server|backup)\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"]
        level = ("error" if re.search(r"(?i)\berror\b|stack trace|infected|"
                                      r"severity\s+1[6-9]|severity\s+2\d", msg)
                 else "warn" if re.search(r"(?i)\bwarn", msg) else "info")
        return self._event(level=level, message=msg, source=f'sybase.{g["fac"]}',
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"engine": int(g["eng"]), "spid": int(g["spid"]),
                                   "facility": g["fac"]}, raw=line)


# ── IBM Informix online.log ────────────────────────────────────────────────────
#   11:46:35  Checkpoint Completed:  duration was 0 seconds.
class InformixAdapter(LogAdapter):
    name = "informix"
    language = "any"
    _RE = re.compile(r"^(?P<ts>\d{2}:\d{2}:\d{2})  (?P<msg>\S.*)$")
    _VOCAB = re.compile(
        r"(?i)checkpoint|logical log|on-line mode|informix|oninit|dbspace|"
        r"chunk|quiescent|listener-thread|dynamic server|maximum server connections|"
        r"physical (?:log|restore)|archive|onconfig|sqlexec|shared memory")

    def detect(self, sample_lines):
        base = ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(ln.rstrip())))
        if base <= 0.0:
            return 0.0
        vocab = any(self._VOCAB.search(str(ln)) for ln in sample_lines)
        # bare "HH:MM:SS  msg" without Informix vocabulary is too generic for a
        # confident claim — stay above generic_ts (0.6) but below any 1.0 owner.
        return base if vocab else base * 0.65

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").rstrip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"]
        level = ("error" if re.search(r"(?i)error|fail|assert|abort", msg)
                 else "warn" if re.search(r"(?i)warn", msg) else "info")
        return self._event(level=level, message=msg, source="informix",
                           ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── Firebird firebird.log ──────────────────────────────────────────────────────
#   X8000 (Client)\tThu Jan 21 13:06:51 2010
#   \tINET/inet_error: connect errno = 10061
class FirebirdAdapter(LogAdapter):
    name = "firebird"
    language = "any"
    _HEAD = re.compile(
        r"^(?P<host>\S+)(?:\s+\((?P<role>Client|Server)\))?\s+"
        r"(?P<ts>[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s*$")
    _BODY = re.compile(r"^[\t ]+\S")

    def _block_line(self, s: str) -> bool:
        return bool(self._HEAD.match(s.strip()) or self._BODY.match(s))

    def detect(self, sample_lines):
        def ok(el):
            subs = split_any(el)
            return (any(self._HEAD.match(x.strip()) for x in subs)
                    and block_ratio(el, lambda x: self._block_line(x)))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if len(subs) > 1:                      # whole record → one event
            hm = next((self._HEAD.match(x.strip()) for x in subs
                       if self._HEAD.match(x.strip())), None)
            if not hm:
                return None
            g = hm.groupdict()
            body = " ".join(x.strip() for x in subs
                            if self._BODY.match(x)).strip()
            level = ("error" if re.search(r"(?i)error|abort|terminated|gds__",
                                          body) else "info")
            return self._event(level=level, message=body or "firebird entry",
                               source=g["host"],
                               ts_ms=parse_timestamp(g["ts"]),
                               fields={"role": g.get("role")}, raw=line)
        st = s.strip()
        hm = self._HEAD.match(st)
        if hm:
            g = hm.groupdict()
            return self._event(level="", message=st, source=g["host"],
                               ts_ms=parse_timestamp(g["ts"]),
                               fields={"role": g.get("role"),
                                       "record_header": True}, raw=line)
        if self._BODY.match(s):
            body = st
            level = ("error" if re.search(r"(?i)error|abort|terminated|gds__",
                                          body) else "info")
            return self._event(level=level, message=body, source="firebird",
                               raw=line)
        return None


# ── Neo4j query.log (register BEFORE neo4j: its lines also fit the plain form) ─
#   2020-01-22 08:58:41.449+0000 ERROR 7364 ms: (planning: 0, waiting: 0) - …
class Neo4jQueryAdapter(LogAdapter):
    name = "neo4j_query"
    language = "java"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{4})\s+"
        r"(?P<level>DEBUG|INFO|WARN|ERROR)\s+(?:id:\s*\d+\s+-\s+)?"
        r"(?P<ms>\d+)\s+ms:\s+\(planning:\s*(?P<plan>\d+),\s*waiting:\s*(?P<wait>\d+)\)"
        r"\s*-\s*(?P<rest>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        cols = g["rest"].split("\t")
        query = cols[-1].split(" - ", 1)[-1].strip() if cols else ""
        return self._event(level=g["level"], message=query[:300] or g["rest"][:300],
                           source="neo4j.query", ts_ms=parse_timestamp(g["ts"]),
                           fields={"duration_ms": int(g["ms"]),
                                   "planning_ms": int(g["plan"]),
                                   "waiting_ms": int(g["wait"]),
                                   "session": cols[0].strip() if cols else None},
                           raw=line)


# ── Neo4j neo4j.log / debug.log (plain form) ──────────────────────────────────
#   2019-12-09 13:45:00.796+0000 INFO  [johnsmith]: logged in
class Neo4jAdapter(LogAdapter):
    name = "neo4j"
    language = "java"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{4})\s+"
        r"(?P<level>DEBUG|INFO|WARN|ERROR)\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"]
        source = "neo4j"
        sm = re.match(r"^\[([^\]]+)\][: ]\s*(.*)$", msg)
        fields = None
        if sm:
            fields = {"context": sm.group(1)}
            msg = sm.group(2) or msg
        return self._event(level=g["level"], message=msg, source=source,
                           ts_ms=parse_timestamp(g["ts"]), fields=fields, raw=line)


# ── Registration ─────────────────────────────────────────────────────────────
for _a in (MsSqlErrorLogAdapter(), OracleAlertAdapter(), Db2DiagAdapter(),
           ClickHouseAdapter(), CockroachDBAdapter(), ScyllaDBAdapter(),
           AerospikeAdapter(), ElasticStackAdapter(), CouchbaseMemcachedAdapter(),
           SapHanaAdapter(), ArangoDbAdapter(),
           # batch 5 — neo4j_query BEFORE neo4j: a query line also fits the
           # plain neo4j grammar and the earlier registration wins the 1.0 tie.
           OracleListenerAdapter(), OracleAuditAdapter(), SybaseAseAdapter(),
           InformixAdapter(), FirebirdAdapter(), Neo4jQueryAdapter(),
           Neo4jAdapter()):
    register_adapter(_a)


# ═════════════════════════════════════════════════════════════════════════════
# BATCH 6
# ═════════════════════════════════════════════════════════════════════════════

# ── CouchDB server log (level-first bracket + Erlang node + pid triple) ──────
#   [notice] 2023-01-01T00:00:00.000000Z couchdb@127.0.0.1 <0.202.0> b2d5e10ac9 …
class CouchDbAdapter(LogAdapter):
    name = "couchdb"
    language = "erlang"
    _RE = re.compile(
        r"^\[(?P<level>debug|info|notice|warning|error|critical|alert|emergency)\]\s+"
        r"(?P<ts>\d{4}-\d{2}-\d{2}T\S+Z?)\s+(?P<node>\S+@\S+)\s+"
        r"<(?P<pid>[\d.]+)>\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level={"notice": "info"}.get(g["level"], g["level"]),
                           message=g["msg"], source=g["node"],
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"erlang_pid": g["pid"]}, raw=line)


# ── 389 Directory Server errors log ──────────────────────────────────────────
#   [27/Apr/2024:13:16:35.123456789 -0400] - ERR - oc_check_required - Entry "…"
class Ds389ErrorsAdapter(LogAdapter):
    name = "ds389_errors"
    language = "any"
    _RE = re.compile(
        r"^\[(?P<ts>\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2}(?:\.\d+)?\s*[+-]\d{4})\]\s+-\s+"
        r"(?P<level>EMERG|ALERT|CRIT|ERR|WARN|NOTICE|INFO|DEBUG)\s+-\s+"
        r"(?P<fn>[\w.\-]+)\s+-\s?(?P<msg>.*)$")
    _LVL = {"EMERG": "fatal", "ALERT": "fatal", "CRIT": "fatal", "ERR": "error",
            "NOTICE": "info"}

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        # the CLF-style parser takes no fractional seconds — drop them so the
        # zone offset is still honored
        ts = re.sub(r"\.\d+", "", g["ts"])
        return self._event(level=self._LVL.get(g["level"], g["level"]),
                           message=g["msg"], source=f'ns-slapd.{g["fn"]}',
                           ts_ms=parse_timestamp(ts),
                           fields={"function": g["fn"]}, raw=line)


for _a in (CouchDbAdapter(), Ds389ErrorsAdapter()):
    register_adapter(_a)


# ═════════════════════════════════════════════════════════════════════════════
#  BATCH 7 — cloud-DB audit rows + analytics-engine node logs
# ═════════════════════════════════════════════════════════════════════════════
import csv as _csv  # noqa: E402
import io as _io  # noqa: E402
from ._common import mk_ts as _mk_ts, split_any as _split_any  # noqa: E402


# ── Aurora MySQL Advanced Auditing audit log ──────────────────────────────────
#   1646766494550603,ip-10-21-0-160,admin,10.0.2.44,102,922,QUERY,mydb,'SELECT …',0
class AuroraAuditCsvAdapter(LogAdapter):
    name = "aurora_audit_csv"
    language = "any"
    _RE = re.compile(
        r"^(?P<us>\d{16}),(?P<server>[\w.\-]*),(?P<user>[^,]*),(?P<host>[^,]*),"
        r"(?P<conn>\d+),(?P<qid>\d+),"
        r"(?P<ev>CONNECT|QUERY|QUERY_DCL|QUERY_DDL|QUERY_DML|TABLE_ACCESS_DATA|"
        r"DISCONNECT|FAILED_CONNECT|PING),(?P<db>[^,]*),(?P<rest>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        rest = g["rest"]
        sql = rest.rsplit(",", 1)[0].strip("'\"") if "," in rest else rest
        retcode = rest.rsplit(",", 1)[1] if "," in rest else ""
        level = ("error" if g["ev"] == "FAILED_CONNECT"
                 else "warn" if retcode.strip().isdigit() and retcode.strip() != "0"
                 else "info")
        return self._event(level=level,
                           message=f'{g["ev"]} {g["db"] or ""} {sql[:120]}'.strip(),
                           source="aurora.audit", ts_ms=float(g["us"]) / 1000.0,
                           fields={"event": g["ev"], "user": g["user"],
                                   "host": g["host"], "db": g["db"],
                                   "connection_id": int(g["conn"]),
                                   "retcode": retcode.strip() or None}, raw=line)


# ── Amazon Redshift audit: connection log (pipe-delimited) ────────────────────
#   authenticated |Mon, 26 Jun 2023 17:54:55:951|::ffff:10.0.0.1 |23906|13892|dev |awsuser |…
class RedshiftConnectionAdapter(LogAdapter):
    name = "redshift_connection"
    language = "any"
    _RE = re.compile(
        r"^(?P<ev>initiating session|authenticated|disconnecting session|"
        r"authentication failure|set application_name)\s*\|"
        r"(?P<ts>[A-Z][a-z]{2}, \d{1,2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2}:\d{3})\|"
        r"(?P<rest>.*)$")

    @staticmethod
    def _ts(t: str):
        m = re.match(r"^[A-Z][a-z]{2}, (\d{1,2}) ([A-Z][a-z]{2}) (\d{4}) "
                     r"(\d{2}):(\d{2}):(\d{2}):(\d{3})$", t)
        if not m or m.group(2) not in _MONTHS:
            return None
        dy, mon, yr, hh, mi, ss, ms = m.groups()
        return _mk_ts(yr, _MONTHS[mon], dy, hh, mi, ss, int(ms) * 1000)

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        cols = [c.strip() for c in g["rest"].split("|")]
        fields = {"event": g["ev"]}
        for idx, key in ((0, "remotehost"), (1, "remoteport"), (2, "pid"),
                         (3, "dbname"), (4, "username"), (5, "authmethod")):
            if idx < len(cols) and cols[idx]:
                fields[key] = cols[idx]
        level = "error" if g["ev"] == "authentication failure" else "info"
        return self._event(level=level,
                           message=f'{g["ev"]} {fields.get("username", "")}@'
                                   f'{fields.get("dbname", "")}'.strip(),
                           source="redshift.connectionlog", ts_ms=self._ts(g["ts"]),
                           fields=fields, raw=line)


# ── Amazon Redshift audit: user activity log ──────────────────────────────────
#   '2023-06-26T18:02:47Z UTC [ db=dev user=awsuser pid=26189 userid=100 xid=712798 ]' LOG: SELECT …
class RedshiftUserActivityAdapter(LogAdapter):
    name = "redshift_useractivity"
    language = "any"
    _RE = re.compile(
        r"^'?(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) UTC "
        r"\[ (?P<ctx>[^\]]*?)\s*\]'?\s+LOG:\s*(?P<sql>.*)$", re.DOTALL)

    def detect(self, sample_lines):
        def ok(el):
            subs = _split_any(el)
            return bool(subs) and bool(self._RE.match("\n".join(subs).strip()))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {k: v for k, v in re.findall(r"(\w+)=(\S+)", g["ctx"])}
        return self._event(level="info", message=g["sql"].strip()[:500],
                           source="redshift.useractivity",
                           ts_ms=parse_timestamp(g["ts"]), fields=fields, raw=line)


# ── Greenplum CSV server log (coordinator/segment log/*.csv) ──────────────────
#   ts,"user","db",p1234,th5678,"host","port",ts2,txid,con10,cmd3,seg-1,…,"LOG","00000","msg",…
# The pNNN/thNNN/conNN/cmdNN/segN tokens are Greenplum-unique vs postgres csvlog.
class GreenplumCsvAdapter(LogAdapter):
    name = "greenplum_csv"
    language = "any"
    _FP = re.compile(r",p\d+,th-?\d+,.*,con\d+,")
    _ISO = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
    _SEV = ("PANIC", "FATAL", "ERROR", "WARNING", "NOTICE", "INFO", "LOG",
            "DEBUG1", "DEBUG2", "DEBUG3", "DEBUG4", "DEBUG5", "DEBUG")

    def detect(self, sample_lines):
        def ok(ln):
            s = str(ln).strip()
            return bool(self._ISO.match(s) and self._FP.search(s))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if not (self._ISO.match(s) and self._FP.search(s)):
            return None
        try:
            row = next(_csv.reader(_io.StringIO(s)))
        except Exception:
            row = s.split(",")
        sev = next((c for c in row if c in self._SEV), "LOG")
        msg = ""
        try:
            msg = row[row.index(sev) + 2] if sev in row else ""
        except Exception:
            pass
        fields = {"user": row[1] if len(row) > 1 else None,
                  "db": row[2] if len(row) > 2 else None,
                  "pid": row[3] if len(row) > 3 else None,
                  "session": next((c for c in row if re.match(r"^con\d+$", c)), None),
                  "segment": next((c for c in row if re.match(r"^(seg|mir)-?\d+$", c)), None)}
        lvl = {"LOG": "info", "NOTICE": "info"}.get(sev, sev)
        return self._event(level=lvl, message=msg or f"greenplum {sev} record",
                           source="greenplum", ts_ms=parse_timestamp(row[0]),
                           fields=fields, raw=line)


# ── Vertica node log (vertica.log) ────────────────────────────────────────────
#   2019-05-29 02:07:16.002 Spread Service InOrder Queue:7fe51e7fc700 [Init] <INFO> Startup …
class VerticaAdapter(LogAdapter):
    name = "vertica"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+"
        r"(?P<thread>.+?):(?P<tid>[0-9a-f]{6,16})(?:-[0-9a-fx]+)?\s+"
        r"(?:\[(?P<comp>[\w ]+)\]\s+)?<(?P<lvl>[A-Z]+)>\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {"thread": g["thread"], "tid": g["tid"]}
        if g["comp"]:
            fields["component"] = g["comp"]
        return self._event(level=g["lvl"], message=g["msg"],
                           source=f'vertica.{g["comp"] or "node"}',
                           ts_ms=parse_timestamp(g["ts"]), fields=fields, raw=line)


# ── FoundationDB XML trace (trace.*.xml — one <Event …/> per line) ────────────
#   <Event Severity="10" Time="1578010020.882538" DateTime="…" Type="Role" … />
class FoundationDbXmlAdapter(LogAdapter):
    name = "foundationdb_xml"
    language = "any"
    _RE = re.compile(r'^<Event\s+Severity="(?P<sev>\d+)"\s')
    _ATTR = re.compile(r'(\w+)="([^"]*)"')

    def detect(self, sample_lines):
        def ok(el):
            subs = _split_any(el)
            return bool(subs) and bool(self._RE.match(subs[0].strip()))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        for x in _split_any(line):
            m = self._RE.match(x.strip())
            if not m:
                continue
            attrs = dict(self._ATTR.findall(x))
            sev = int(attrs.get("Severity", "10"))
            level = ("debug" if sev < 10 else "info" if sev < 20
                     else "warn" if sev < 40 else "error")
            ts_ms = None
            if attrs.get("Time"):
                try:
                    ts_ms = float(attrs["Time"]) * 1000.0
                except ValueError:
                    ts_ms = None
            if ts_ms is None and attrs.get("DateTime"):
                ts_ms = parse_timestamp(attrs["DateTime"])
            fields = {k: v for k, v in attrs.items()
                      if k not in ("Severity", "Time", "DateTime", "Type")}
            return self._event(level=level, message=attrs.get("Type", "Event"),
                               source=f'fdb.{attrs.get("Machine", "trace")}',
                               ts_ms=ts_ms, fields=fields, raw=line)
        return None


for _a in (AuroraAuditCsvAdapter(), RedshiftConnectionAdapter(),
           RedshiftUserActivityAdapter(), GreenplumCsvAdapter(),
           VerticaAdapter(), FoundationDbXmlAdapter()):
    register_adapter(_a)


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH 8 — DB engine straggler text logs
# ══════════════════════════════════════════════════════════════════════════════
from ._common import RxAdapter, vocab_detect, block_ratio, split_any  # noqa: E402


# ── Microsoft SQL Server Agent error log (SQLAGENT.OUT) ───────────────────────
#   [100] Microsoft SQLServerAgent version 15.0.4236.7 (…): Process ID 5180
#   2026-07-20 12:00:00 - + [396] An idle CPU condition has occurred …
class MssqlAgentAdapter(LogAdapter):
    name = "mssql_agent"
    language = "sql"
    _FULL = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+-\s+(?P<sym>[?+!])\s+"
        r"\[(?P<id>\d+)\]\s+(?P<msg>.*)$")
    _BARE = re.compile(r"^\[(?P<id>\d+)\]\s+(?P<msg>.*)$")
    _VOCAB = re.compile(r"SQLServerAgent|SQLAGENT|\bjob\b|schedule|subsystem|"
                        r"Process ID|\[Runnable\]|alert", re.I)
    _SYM = {"?": "info", "+": "warn", "!": "error"}

    def detect(self, sample_lines):
        def hit(x):
            x = x.strip()
            if self._FULL.match(x):
                return True
            m = self._BARE.match(x)
            return bool(m) and self._VOCAB.search(x) is not None
        return vocab_detect(sample_lines, lambda el: block_ratio(el, hit), cap=0.9)

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._FULL.match(s)
        if m:
            g = m.groupdict()
            return self._event(level=self._SYM.get(g["sym"], "info"), message=g["msg"],
                               source="sqlserveragent", ts_ms=parse_timestamp(g["ts"]),
                               fields={"message_id": int(g["id"])}, raw=line)
        m = self._BARE.match(s)
        if m and self._VOCAB.search(s):
            g = m.groupdict()
            return self._event(level="info", message=g["msg"], source="sqlserveragent",
                               fields={"message_id": int(g["id"])}, raw=line)
        return None


# ── Firebird Trace/Audit output (fbtracemgr) ──────────────────────────────────
#   2021-03-31T19:47:25.6070 (3148:000000007ED424C0) PREPARE_STATEMENT
class FirebirdTraceAdapter(RxAdapter):
    name = "firebird_trace"
    language = "sql"
    match_scope = "first"
    default_source = "firebird.trace"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{4})\s+"
        r"\((?P<att>\d+:[0-9A-Fa-f]{16})\)\s+(?P<event>[A-Z_]+)\s*$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return "error" if re.search(r"ERROR|FAIL|ROLLBACK", g.get("event", "")) else ""

    def _fields(self, g, line):
        return {"attachment": g["att"], "trace_event": g["event"]}

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            m = self._RE.match(split_any(line)[0].strip())
            ev["data"]["message"] = m.group("event")
        return ev


# ── Oracle Net client/server log (sqlnet.log) ─────────────────────────────────
#   Fatal OSN connect error 12543, connecting to:  … TNS-12543 …
class OracleSqlnetAdapter(LogAdapter):
    name = "oracle_sqlnet"
    language = "sql"
    _HEAD = re.compile(r"^Fatal (?:OSN|NI) connect error (?P<code>\d+)")
    _TNS = re.compile(r"^\s*(?P<tns>TNS-\d{5}):\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        def hit(el):
            subs = [x.strip() for x in split_any(el)]
            return any(self._HEAD.match(x) for x in subs) or (
                any(self._TNS.match(x) for x in subs)
                and any("VERSION INFORMATION" in x or "err code" in x for x in subs))
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        subs = [x.strip() for x in split_any(s)]
        head = next((x for x in subs if self._HEAD.match(x)), "")
        code = self._HEAD.match(head).group("code") if head else None
        tns = next((self._TNS.match(x) for x in subs if self._TNS.match(x)), None)
        fields = {}
        if code:
            fields["osn_error"] = int(code)
        if tns:
            fields["tns_code"] = tns.group("tns")
        return self._event(level="error", message=head or (subs[0] if subs else s),
                           source="oracle.sqlnet", fields=fields or None,
                           category="error", raw=line)


# ── Progress OpenEdge database log (<dbname>.lg) ──────────────────────────────
#   [2017/11/12@17:41:31.729+0100] P-20932  T-21792 I DBUTIL : (451) prostrct …
class ProgressOpenedgeAdapter(RxAdapter):
    name = "progress_openedge"
    language = "any"
    default_source = "openedge"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}/\d{2}/\d{2}@\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{4})\]\s+"
        r"P-(?P<pid>\d+)\s+T-(?P<tid>\d+)\s+(?P<lvl>[IWEF])\s+(?P<subsys>\w*)\s*:\s*"
        r"(?:\((?P<msgnum>\d+)\)\s*)?(?P<msg>.*)$")
    _LVL = {"I": "info", "W": "warn", "E": "error", "F": "fatal"}

    def _ts(self, g):
        return parse_timestamp(g["ts"].replace("@", " "))

    def _level(self, g, line):
        return self._LVL.get(g["lvl"], "info")

    def _fields(self, g, line):
        f = {"pid": int(g["pid"]), "tid": int(g["tid"])}
        if g.get("subsys"):
            f["subsystem"] = g["subsys"]
        if g.get("msgnum"):
            f["message_number"] = int(g["msgnum"])
        return f


# ── Barman (PostgreSQL backup manager) barman.log ─────────────────────────────
#   2026-07-21 02:00:01,123 [12345] barman.server INFO: Starting backup …
class BarmanAdapter(RxAdapter):
    name = "barman"
    language = "python"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+\[(?P<pid>\d+)\]\s+"
        r"(?P<logger>barman\.[\w.]+)\s+(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL):\s*(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"].replace(",", "."))

    def _fields(self, g, line):
        return {"pid": int(g["pid"])}

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            m = self._RE.match(line.strip())
            ev["source"] = m.group("logger")
        return ev


# ── ArangoDB Enterprise audit log ─────────────────────────────────────────────
#   2016-10-03 15:44:23 | server1 | audit-authentication | n/a | database1 | …
class ArangoAuditAdapter(RxAdapter):
    name = "arango_audit"
    language = "any"
    default_source = "arangodb.audit"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+\|\s+(?P<server>\S+)\s+\|\s+"
        r"(?P<topic>audit-\w+)\s+\|\s+(?P<rest>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return "warn" if re.search(r"fail|denied|unknown", g.get("rest", ""), re.I) else "info"

    def _fields(self, g, line):
        cols = [c.strip() for c in g["rest"].split("|")]
        return {"server": g["server"], "audit_topic": g["topic"],
                "columns": cols}

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            m = self._RE.match(line.strip())
            ev["data"]["message"] = f'{m.group("topic")}: {m.group("rest").split("|")[-1].strip()}'
            ev["category"] = "event"
        return ev


for _a in (MssqlAgentAdapter(), FirebirdTraceAdapter(), OracleSqlnetAdapter(),
           ProgressOpenedgeAdapter(), BarmanAdapter(), ArangoAuditAdapter()):
    register_adapter(_a)
