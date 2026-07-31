"""
Messaging / broker / coordination-service log adapters (BATCH 2)
================================================================================
Message brokers and coordination services. (Kafka log4j, RabbitMQ, and Redis are
already covered by the core adapters; every JSON-encoded broker log lands in the
`jsonl` super-adapter.)

Formats: zookeeper, nats, mosquitto, emqx, activemq, artemis.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp,
                      _to_ms, ratio_detect, multiline_ratio_detect,
                      split_any, us_date_ts)


# ── Apache ZooKeeper (log4j) ─────────────────────────────────────────────────
#   2015-07-29 19:04:12,394 - INFO  [thread:Class$Inner@493] - Received ...
class ZookeeperAdapter(LogAdapter):
    name = "zookeeper"
    language = "java"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+-\s+"
        r"(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+"
        r"\[(?P<thread>[^\]]*?@\d+)\]\s+-\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        # the thread field carries "name:Class$Inner@line"
        cm = re.search(r":([\w.$]+)@(\d+)$", g["thread"])
        return self._event(level=g["level"], message=g["msg"],
                           source=cm.group(1) if cm else "zookeeper",
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"thread": g["thread"],
                                   "line": int(cm.group(2)) if cm else None}, raw=line)


# ── NATS server ──────────────────────────────────────────────────────────────
#   [80943] 2021/10/28 16:53:38.198090 [INF] Starting nats-server
class NatsAdapter(LogAdapter):
    name = "nats"
    language = "go"
    _RE = re.compile(
        r"^\[(?P<pid>\d+)\]\s+(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+"
        r"\[(?P<level>INF|DBG|WRN|ERR|FTL|TRC)\]\s+(?P<msg>.*)$")
    _LVL = {"INF": "info", "DBG": "debug", "WRN": "warn", "ERR": "error",
            "FTL": "fatal", "TRC": "trace"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=self._LVL.get(g["level"], "info"), message=g["msg"],
                           source="nats", ts_ms=parse_timestamp(g["ts"]),
                           fields={"pid": int(g["pid"])}, raw=line)


# ── Eclipse Mosquitto MQTT broker ────────────────────────────────────────────
#   1668433352: mosquitto version 2.0.15 starting
class MosquittoAdapter(LogAdapter):
    name = "mosquitto"
    language = "any"
    _RE = re.compile(r"^(?P<epoch>\d{10}):\s+(?P<msg>.*)$")
    # broker vocabulary that makes the epoch:msg shape unambiguously Mosquitto.
    _VOCAB = re.compile(
        r"mosquitto version|New connection from|New client connected|"
        r"Client .* (?:disconnected|closed|already connected)|Config loaded|"
        r"Socket error|Opening ipv[46] listen socket|Sending |Received |"
        r"Saving in-memory database|mosquitto version .* terminating", re.IGNORECASE)

    def detect(self, sample_lines):
        def ok(ln):
            m = self._RE.match(ln.strip())
            return bool(m) and bool(self._VOCAB.search(m.group("msg")))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        msg = m.group("msg")
        low = msg.lower()
        level = "error" if "error" in low else "warn" if "warning" in low else "info"
        ts_ms = None
        try:
            ts_ms = int(m.group("epoch")) * 1000.0
        except ValueError:
            pass
        return self._event(level=level, message=msg, source="mosquitto",
                           ts_ms=ts_ms, raw=line)


# ── EMQX broker (5.x text formatter) ─────────────────────────────────────────
#   2024-03-20T11:08:39.568980+01:00 [warning] tag: AUTHZ, clientid: client1, msg: ...
class EmqxAdapter(LogAdapter):
    name = "emqx"
    language = "erlang"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+(?:[+-]\d{2}:\d{2}|Z)?)\s+"
        r"\[(?P<level>debug|info|notice|warning|error|critical|alert|emergency)\]\s+(?P<rest>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        rest = g["rest"]
        # EMQX bodies are "k: v, k2: v2" comma lists — pull them into fields.
        fields = {}
        for k, v in re.findall(r"(\w+):\s*([^,]+?)(?:,\s+|$)", rest):
            fields[k] = v.strip()
        msg = fields.get("msg") or rest
        return self._event(level=g["level"], message=msg, source="emqx",
                           ts_ms=parse_timestamp(g["ts"]), fields=fields or None, raw=line)


# ── Apache ActiveMQ Classic (activemq.log) ───────────────────────────────────
#   2023-05-04 10:31:05,432 | INFO  | Apache ActiveMQ ... | org.apache...BrokerService | main
class ActiveMQAdapter(LogAdapter):
    name = "activemq"
    language = "java"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+\|\s+"
        r"(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+\|\s+"
        r"(?P<msg>.*?)\s+\|\s+(?P<logger>[\w.$]+)\s+\|\s+(?P<thread>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"], source=g["logger"],
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"logger": g["logger"], "thread": g["thread"]}, raw=line)


# ── Apache ActiveMQ Artemis (artemis.log) ────────────────────────────────────
#   2023-05-04 10:41:04,149 INFO  [org.apache.activemq.artemis.core.server] AMQ221007: ...
class ArtemisAdapter(LogAdapter):
    name = "artemis"
    language = "java"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+"
        r"(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+"
        r"\[(?P<logger>[\w.$]+)\]\s+(?:(?P<code>AMQ\d+):\s*)?(?P<msg>.*)$")
    # VOCABULARY GATE: the "TS,ms LEVEL [logger] msg" silhouette is shared by
    # many generic loggers (a bracketed-logger Python `logging` line is the
    # textbook case). Artemis may only claim a line that carries its OWN
    # vocabulary — an activemq/artemis logger or an AMQ###### message code —
    # so python_logging (and friends) win their common bracketed variants.
    _VOCAB = re.compile(r"activemq|artemis", re.IGNORECASE)
    _CODE = re.compile(r"\bAMQ\d{6}\b")

    def _hit(self, ln: str) -> bool:
        m = self._RE.match(ln.strip())
        if not m:
            return False
        return bool(m.group("code") or self._VOCAB.search(m.group("logger"))
                    or self._CODE.search(m.group("msg")))

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, self._hit)

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {"logger": g["logger"]}
        if g.get("code"):
            fields["amq_code"] = g["code"]
        return self._event(level=g["level"], message=g["msg"], source=g["logger"],
                           ts_ms=parse_timestamp(g["ts"]), trace_id=g.get("code"),
                           fields=fields, raw=line)


# ── Asterisk full log (BATCH 3 — telephony message routing) ──────────────────
#   [2026-01-05 10:26:12] VERBOSE[23456][C-00000001] pbx.c: Executing [6001@from-internal:1] …
class AsteriskAdapter(LogAdapter):
    name = "asterisk"
    language = "any"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?|[A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2})\]\s+"
        r"(?P<lvl>VERBOSE|NOTICE|WARNING|ERROR|DEBUG|DTMF|SECURITY|FAX)"
        r"\[(?P<tid>\d+)\](?:\[(?P<callid>C-[0-9a-fA-F]+)\])?\s+"
        r"(?P<src>\S+\.c):\s*(?P<msg>.*)$")
    _LVL = {"VERBOSE": "debug", "NOTICE": "info", "WARNING": "warn",
            "ERROR": "error", "DEBUG": "debug", "DTMF": "info",
            "SECURITY": "warn", "FAX": "info"}

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {"thread": int(g["tid"]), "source_file": g["src"]}
        if g.get("callid"):
            fields["call_id"] = g["callid"]
        return self._event(level=self._LVL.get(g["lvl"], g["lvl"]), message=g["msg"],
                           source=f'asterisk.{g["src"]}',
                           ts_ms=parse_timestamp(g["ts"]),
                           trace_id=g.get("callid"), fields=fields, raw=line)


# ── FreeSWITCH log file (BATCH 3) ────────────────────────────────────────────
#   a1b2c3d4-…-90ab 2026-01-05 10:26:12.123456 [DEBUG] switch_core_state_machine.c:473 …
class FreeSwitchAdapter(LogAdapter):
    name = "freeswitch"
    language = "any"
    _RE = re.compile(
        r"^(?:(?P<uuid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\s+)?"
        r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6})\s+"
        r"\[(?P<lvl>CONSOLE|ALERT|CRIT|ERR|WARNING|NOTICE|INFO|DEBUG)\]\s+"
        r"(?P<src>\S+\.(?:c|cpp)):(?P<ln>\d+)\s+(?P<msg>.*)$")
    _LVL = {"CONSOLE": "info", "ALERT": "fatal", "CRIT": "fatal", "ERR": "error",
            "WARNING": "warn", "NOTICE": "info", "INFO": "info", "DEBUG": "debug"}

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {"source_file": f'{g["src"]}:{g["ln"]}'}
        if g.get("uuid"):
            fields["call_uuid"] = g["uuid"]
        return self._event(level=self._LVL.get(g["lvl"], g["lvl"]), message=g["msg"],
                           source=f'freeswitch.{g["src"]}',
                           ts_ms=parse_timestamp(g["ts"]),
                           trace_id=g.get("uuid"), fields=fields, raw=line)


# ── Exim MTA main log (exim_mainlog) — BATCH 4 ───────────────────────────────
#   2002-10-31 08:57:53 16ZCW1-0005MB-00 <= kryten@dwarf.fict.example H=… S=5678
class EximMainlogAdapter(LogAdapter):
    name = "exim_mainlog"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?"
        r"(?:\s+[+-]\d{4})?(?:\s+\[\d+\])?\s+"
        r"(?:(?P<id>[0-9A-Za-z]{6}-[0-9A-Za-z]{6}-[0-9A-Za-z]{2})\s+)"
        r"(?P<flag><=|=>|->|>>|\*>|\*\*|==|Completed\b|SMTP\b|no immediate)?"
        r"\s*(?P<msg>.*)$")
    # BATCH-7 gap fix: the exim REJECTLOG carries the same "ts body" shape but
    # NO message-id (rejected before one was assigned). Accept id-less lines
    # when they speak the reject vocabulary with an H= sender token.
    _REJECT = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?"
        r"(?:\s+[+-]\d{4})?(?:\s+\[\d+\])?\s+(?P<msg>.*\bH=.*\b"
        r"(?:rejected|temporarily rejected)\b.*)$")
    _FLAG_LVL = {"**": "error", "==": "warn", "*>": "warn"}
    _FLAG_KIND = {"<=": "arrival", "=>": "delivery", "->": "additional_rcpt",
                  ">>": "cutthrough", "**": "delivery_failed", "==": "deferred",
                  "*>": "suppressed"}

    def detect(self, sample_lines):
        def hit(ln):
            s = str(ln).strip()
            m = self._RE.match(s)
            # the Exim message-id is mandatory here — a bare "ts text" line
            # belongs to generic adapters, not this one — EXCEPT the id-less
            # rejectlog form, which is vocabulary-gated.
            return bool(m and m.group("id")) or bool(self._REJECT.match(s))
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m or not m.group("id"):
            rm = self._REJECT.match(s)
            if not rm:
                return None
            g = rm.groupdict()
            fields = {"event": "rejected"}
            for key in ("H", "U", "P", "S", "F", "T", "C"):
                km = re.search(rf"\b{key}=(\S+)", g["msg"])
                if km:
                    fields[key] = km.group(1)
            return self._event(level="warn", message=g["msg"], source="exim.reject",
                               ts_ms=parse_timestamp(g["ts"]), fields=fields,
                               raw=line)
        g = m.groupdict()
        flag = (g.get("flag") or "").strip()
        fields = {"exim_id": g["id"]}
        if flag in self._FLAG_KIND:
            fields["event"] = self._FLAG_KIND[flag]
        elif flag:
            fields["event"] = flag.lower()
        for key in ("H", "U", "P", "S", "R", "T", "C"):
            km = re.search(rf"\b{key}=(\S+)", g["msg"])
            if km:
                fields[key] = km.group(1)
        return self._event(level=self._FLAG_LVL.get(flag, "info"),
                           message=f"{flag} {g['msg']}".strip(),
                           source="exim", ts_ms=parse_timestamp(g["ts"]),
                           trace_id=g["id"], fields=fields, raw=line)


# ═══════════════════════════ BATCH 5 ═════════════════════════════════════════

# ── IBM MQ AMQERRxx.LOG (text form) ────────────────────────────────────────────
#   06/15/22 06:10:59 - Process(597.4) User(mqm) Program(amqrmppa) Host(…)
#   Installation(Installation1) VRMF(9.2.0.0) QMgr(QM1) AMQ9207E: The data …
class IbmMqAdapter(LogAdapter):
    name = "ibm_mq"
    language = "any"
    _HEAD = re.compile(
        r"^(?P<date>\d{2}/\d{2}/\d{2,4})\s+(?P<time>\d{2}:\d{2}:\d{2})"
        r"(?:\s+(?P<ampm>[AP]M))?\s+-\s+Process\((?P<proc>\d+\.\d+)\)\s*(?P<rest>.*)$")
    _ATTR = re.compile(r"(\w+)\(([^)]*)\)")
    _CODE = re.compile(r"\b(?P<code>AMQ\d{4,5})(?P<suf>[EWIST])?\s*:\s*(?P<msg>.*)$")
    _SUF_LVL = {"E": "error", "W": "warn", "I": "info", "S": "fatal", "T": "fatal"}

    def detect(self, sample_lines):
        # an AMQERR record is the header line plus wrapped text/EXPLANATION/
        # ACTION lines — claim the element when ANY logical line is a header.
        def ok(el):
            return any(self._HEAD.match(x.strip()) for x in split_any(el))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        subs = split_any(s)
        hm = next((self._HEAD.match(x.strip()) for x in subs
                   if self._HEAD.match(x.strip())), None)
        if hm is None:
            # continuation lines of a record fed line-by-line
            st = s.strip()
            cm = self._CODE.search(st)
            if cm:
                lvl = self._SUF_LVL.get(cm.group("suf") or "", "info")
                return self._event(level=lvl, message=st, source="ibm.mq",
                                   fields={"message_code": cm.group("code")},
                                   raw=line)
            if re.match(r"^(EXPLANATION|ACTION)\s*:", st):
                return self._event(level="", message=st, source="ibm.mq", raw=line)
            return None
        g = hm.groupdict()
        body = g["rest"] + " " + " ".join(x.strip() for x in subs[1:])
        attrs = dict(self._ATTR.findall(g["rest"]))
        cm = self._CODE.search(body)
        level = "info"
        fields = {"process": g["proc"]}
        for k in ("User", "Program", "Host", "QMgr", "Installation", "VRMF"):
            if k in attrs:
                fields[k.lower()] = attrs[k]
        msg = body.strip()
        if cm:
            level = self._SUF_LVL.get(cm.group("suf") or "", "info")
            fields["message_code"] = cm.group("code")
            msg = f'{cm.group("code")}{cm.group("suf") or ""}: {cm.group("msg").strip()}'
        return self._event(level=level, message=msg[:400],
                           source=f'ibm.mq.{attrs.get("Program", "amqerr")}',
                           ts_ms=us_date_ts(g["date"],
                                            f'{g["time"]}{" " + g["ampm"] if g["ampm"] else ""}'),
                           fields=fields, raw=line)


# ── Microsoft Exchange message-tracking CSV ────────────────────────────────────
#   2026-07-20T09:00:12.345Z,10.0.0.5,MBX01…,…,SMTP,RECEIVE,73014444033,<id>,…
class ExchangeTrackingAdapter(LogAdapter):
    name = "exchange_tracking"
    language = "any"
    _EVENTS = {"RECEIVE", "SEND", "DELIVER", "FAIL", "SUBMIT", "TRANSFER",
               "BADMAIL", "EXPAND", "RESOLVE", "DEFER", "DSN", "AGENTINFO",
               "HAREDIRECT", "HARECEIVE", "HADISCARD", "DUPLICATEDELIVER",
               "NOTIFYMAPI", "PROCESS", "SUPPRESSED", "THROTTLE", "DROP"}
    _SOURCES = {"SMTP", "STOREDRIVER", "ROUTING", "DNS", "AGENT", "ADMIN",
                "GATEWAY", "PICKUP", "DSN", "BOOTLOADER", "MEETINGMESSAGE"}
    _HEADER = re.compile(r"^#(Software|Version|Log-type|Fields|Date):", re.I)

    def _fields_of(self, s: str):
        import csv as _csv
        try:
            return next(_csv.reader([s]))
        except Exception:
            return None

    def detect(self, sample_lines):
        def ok(ln):
            s = ln.strip()
            if self._HEADER.match(s):
                return "exchange" in s.lower() or "message tracking" in s.lower() \
                    or s.lower().startswith("#fields: date-time,client-ip")
            if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", s) \
                    or s.count(",") < 12:
                return False
            cols = self._fields_of(s)
            return bool(cols and len(cols) >= 12
                        and any(c in self._EVENTS for c in cols[:12])
                        and any(c in self._SOURCES for c in cols[:12]))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if self._HEADER.match(s):
            return self._event(level="", message=s, source="exchange.tracking",
                               fields={"csv_directive": True}, raw=line)
        cols = self._fields_of(s)
        if not cols or len(cols) < 10:
            return None
        event = next((c for c in cols[:12] if c in self._EVENTS), "")
        src = next((c for c in cols[:12] if c in self._SOURCES), "")
        level = ("error" if event in ("FAIL", "BADMAIL", "DROP")
                 else "warn" if event in ("DEFER", "THROTTLE") else "info")
        fields = {"event_id": event or None, "source": src or None,
                  "client_ip": cols[1] or None,
                  "server": cols[4] if len(cols) > 4 else None}
        # recipient + subject live at fixed offsets in the standard field list
        if len(cols) > 12 and "@" in cols[12]:
            fields["recipient"] = cols[12]
        if len(cols) > 18 and cols[18]:
            fields["subject"] = cols[18]
        mid = next((c for c in cols if c.startswith("<") and c.endswith(">")), None)
        return self._event(level=level,
                           message=f'{event or "tracking"} '
                                   f'{fields.get("recipient") or ""}'.strip(),
                           source="exchange.tracking",
                           ts_ms=parse_timestamp(cols[0]), trace_id=mid,
                           fields=fields, raw=line)


# ── Registration ─────────────────────────────────────────────────────────────
# zookeeper's "ts - LEVEL [thread] - msg" overlaps the generic python_logging
# shape → register it (and the other broker adapters) BEFORE python_logging so
# the specific broker grammar wins a confidence tie.
for _a in (ZookeeperAdapter(), NatsAdapter(), MosquittoAdapter(), EmqxAdapter(),
           ActiveMQAdapter(), ArtemisAdapter()):
    register_adapter(_a, before="python_logging")
for _a in (AsteriskAdapter(), FreeSwitchAdapter(), EximMainlogAdapter(),
           # batch 5
           IbmMqAdapter(), ExchangeTrackingAdapter()):
    register_adapter(_a)


# ═════════════════════════════════════════════════════════════════════════════
#  BATCH 7 — Artemis audit, daemontools/qmail TAI64N multilog, Exchange SMTP csv
# ═════════════════════════════════════════════════════════════════════════════


# ── ActiveMQ Artemis audit.log ────────────────────────────────────────────────
#   2023-05-04 10:45:12,003 [AUDIT](Thread-1 (…)) AMQ601715: User admin@… authenticated
class ArtemisAuditAdapter(LogAdapter):
    name = "artemis_audit"
    language = "java"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+"
        r"\[AUDIT\]\((?P<thr>[^)]*(?:\([^)]*\))?[^)]*)\)\s+"
        r"(?P<code>AMQ60\d{4}):\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        low = g["msg"].lower()
        level = ("warn" if "fail" in low or "denied" in low else "info")
        um = re.search(r"User (\S+?)@", g["msg"])
        fields = {"audit_code": g["code"], "thread": g["thr"]}
        if um:
            fields["user"] = um.group(1)
        return self._event(level=level, message=g["msg"], source="artemis.audit",
                           ts_ms=parse_timestamp(g["ts"]), fields=fields, raw=line)


# ── daemontools multilog / qmail-send TAI64N-stamped lines ────────────────────
#   @4000000052fafd8d373b5dbc starting delivery 9: msg 3890 to remote user@…
class Tai64nMultilogAdapter(LogAdapter):
    name = "tai64n_multilog"
    language = "any"
    _RE = re.compile(r"^@(?P<tai>[0-9a-f]{24})\s+(?P<msg>.*)$")

    @staticmethod
    def _tai_ms(tai: str):
        try:
            secs = int(tai[0:16], 16) - (1 << 62)   # TAI64 label → ~unix secs
            nanos = int(tai[16:24], 16)
        except ValueError:
            return None
        if not (0 < secs < 1 << 34):
            return None
        return secs * 1000.0 + nanos / 1e6

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        low = g["msg"].lower()
        level = ("error" if "failure" in low or "fatal" in low
                 else "warn" if "deferral" in low or "warning" in low else "info")
        return self._event(level=level, message=g["msg"], source="multilog",
                           ts_ms=self._tai_ms(g["tai"]), raw=line)


# ── Microsoft Exchange SMTP protocol log (RECV*/SEND*.LOG) ────────────────────
#   2026-07-20T09:00:10.123Z,MBX01\Default MBX01,08D9F…,3,10.0.0.5:25,203.0.113.7:41890,<,EHLO …,
class ExchangeSmtpCsvAdapter(LogAdapter):
    name = "exchange_smtp_csv"
    language = "any"
    _ROW = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z),(?P<conn>[^,]*),"
        r"(?P<sess>[0-9A-Fa-f.]+(?:\.\.\.)?),(?P<seq>\d+),(?P<local>[^,]*),"
        r"(?P<remote>[^,]*),(?P<ev>[<>+\-*]),(?P<data>.*?),?(?P<ctx>[^,]*)$")
    _HDR = re.compile(r"^#(?:Log-type: SMTP (?:Receive|Send) Protocol Log|"
                      r"Fields: date-time,connector-id,session-id)")
    _EV = {"<": "receive", ">": "send", "+": "connect", "-": "disconnect",
           "*": "information"}

    def detect(self, sample_lines):
        def ok(ln):
            s = str(ln).strip()
            return bool(self._HDR.match(s) or self._ROW.match(s))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if self._HDR.match(s):
            return self._event(level="info", message=s.lstrip("#"),
                               source="exchange.smtp", raw=line)
        m = self._ROW.match(s)
        if not m:
            return None
        g = m.groupdict()
        fields = {"connector": g["conn"], "session": g["sess"],
                  "sequence": int(g["seq"]), "local": g["local"],
                  "remote": g["remote"], "event": self._EV.get(g["ev"], g["ev"])}
        return self._event(level="info",
                           message=f'{g["ev"]} {g["data"]}'.strip(),
                           source="exchange.smtp", ts_ms=parse_timestamp(g["ts"]),
                           fields=fields, raw=line)


for _a in (ArtemisAuditAdapter(), Tai64nMultilogAdapter()):
    register_adapter(_a)
# Exchange protocol logs share the "#Fields:" directive prefix with the W3C
# access grammar (webserver.w3c_access, loaded earlier) → insert BEFORE it so
# the Exchange-specific comma-separated field list wins the 1.0 tie, while
# space-separated W3C headers still belong to w3c_access.
register_adapter(ExchangeSmtpCsvAdapter(), before="w3c_access")
