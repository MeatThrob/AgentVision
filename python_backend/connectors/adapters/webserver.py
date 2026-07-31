"""
Web / application-server log adapters (BATCH 2)
================================================================================
App-server and web-server formats whose grammar is distinct from the NCSA
Common/Combined access log (which the core `access_log` adapter already covers —
Apache/nginx/gunicorn-access/varnishncsa/morgan-combined all land there).

Formats: tomcat_catalina, gunicorn_error, express_morgan_dev, glassfish_odl,
weblogic, w3c_access.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp,
                      _MONTHS, _to_ms, ratio_detect, multiline_ratio_detect)


# ── Apache Tomcat JULI (catalina.out) ────────────────────────────────────────
#   18-Feb-2025 12:46:00.000 SEVERE [main] org.apache.catalina.core... message
class TomcatCatalinaAdapter(LogAdapter):
    name = "tomcat_catalina"
    language = "java"
    _RE = re.compile(
        r"^(?P<ts>\d{2}-[A-Z][a-z]{2}-\d{4} \d{2}:\d{2}:\d{2}(?:\.\d{3})?)\s+"
        r"(?P<level>SEVERE|WARNING|INFO|CONFIG|FINE|FINER|FINEST)\s+"
        r"\[(?P<thread>[^\]]+)\]\s+(?P<logger>[\w.$]+)\s*(?P<msg>.*)$")
    _LVL = {"SEVERE": "error", "WARNING": "warn", "INFO": "info", "CONFIG": "info",
            "FINE": "debug", "FINER": "debug", "FINEST": "trace"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=self._LVL.get(g["level"], "info"), message=g["msg"],
                           source=g["logger"], ts_ms=_dmy_ts(g["ts"]),
                           fields={"thread": g["thread"], "jul_level": g["level"]}, raw=line)


def _dmy_ts(s: str) -> Optional[float]:
    m = re.match(r"^(\d{2})-([A-Z][a-z]{2})-(\d{4}) (\d{2}):(\d{2}):(\d{2})(?:\.(\d{3}))?$", s)
    if not m or m.group(2) not in _MONTHS:
        return None
    dy, mon, yr, hh, mm, ss, ms = m.groups()
    try:
        return _to_ms(datetime(int(yr), _MONTHS[mon], int(dy), int(hh), int(mm),
                               int(ss), int(ms or 0) * 1000))
    except ValueError:
        return None


# ── Gunicorn error/startup log ───────────────────────────────────────────────
#   [2026-07-21 15:40:39 +0000] [12345] [INFO] Starting gunicorn 21.2.0
class GunicornErrorAdapter(LogAdapter):
    name = "gunicorn_error"
    language = "python"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\s*[+-]\d{4})?)\]\s+"
        r"\[(?P<pid>\d+)\]\s+\[(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\]\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        # gunicorn emits several of these at startup; accept a multi-line block.
        return multiline_ratio_detect(sample_lines, lambda x: bool(self._RE.match(x.strip())),
                                      threshold=0.5)

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"], source="gunicorn",
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"pid": int(g["pid"])}, raw=line)


# ── Express / morgan 'dev' format ────────────────────────────────────────────
#   GET /api/users 200 12.345 ms - 1234
class ExpressMorganDevAdapter(LogAdapter):
    name = "express_morgan_dev"
    language = "node"
    _RE = re.compile(
        r"^(?P<method>GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|TRACE|CONNECT)\s+"
        r"(?P<url>\S+)\s+(?P<status>\d{3})\s+(?P<ms>[\d.]+)\s+ms\s+-\s+(?P<size>\S+)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        status = int(g["status"])
        level = "error" if status >= 500 else "warn" if status >= 400 else "info"
        return self._event(level=level, message=f'{g["method"]} {g["url"]} → {status}',
                           source="express", fields={"method": g["method"], "url": g["url"],
                           "status": status, "duration_ms": float(g["ms"]),
                           "bytes": g["size"]}, raw=line)


# ── GlassFish / Payara ODL (Oracle Diagnostics Logging) server.log ───────────
#   [2013-04-12T08:08:30.154-0700] [glassfish 4.0] [INFO] [AS-WEB-GLUE-00172] ...
class GlassfishOdlAdapter(LogAdapter):
    name = "glassfish_odl"
    language = "java"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+(?:[+-]\d{4})?)\]\s+"
        r"\[(?P<product>[^\]]*)\]\s+\[(?P<level>[A-Z]+)\]\s+\[(?P<code>[^\]]*)\]\s+"
        r"\[(?P<module>[^\]]*)\]\s+(?P<rest>.*)$")
    _LVL = {"SEVERE": "error", "WARNING": "warn", "INFO": "info", "CONFIG": "info",
            "FINE": "debug", "FINER": "debug", "FINEST": "trace"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        # ODL puts [tid: ...] [timeMillis:...] blocks then the message at the tail.
        msg = g["rest"]
        tail = re.split(r"\]\s*(?=[A-Za-z])", msg)
        message = tail[-1].strip() if tail else msg
        return self._event(level=self._LVL.get(g["level"], g["level"].lower()),
                           message=message or msg, source=g["module"] or "glassfish",
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"product": g["product"], "message_id": g["code"],
                                   "module": g["module"]}, raw=line)


# ── Oracle WebLogic Server log (####<...> field blocks) ──────────────────────
#   ####<Sept 22, 2004 10:46:51 AM EST> <Notice> <WebLogicServer> <host> ...
class WebLogicAdapter(LogAdapter):
    name = "weblogic"
    language = "java"
    _RE = re.compile(r"^####(?P<fields><.*>)\s*$")
    _FIELD = re.compile(r"<([^>]*)>")
    # BATCH-6 gap fix — the server STDOUT variant drops the '####' sigil but
    # keeps the angle-field grammar: <ts> <Severity> <Subsystem> <BEA-nnnnnn> <msg>
    _STDOUT = re.compile(
        r"^<[^<>]{6,60}> <(?:Trace|Debug|Info|Notice|Warning|Error|Critical|"
        r"Alert|Emergency)> <[^<>]+> <")
    _LVL = {"Trace": "trace", "Debug": "debug", "Info": "info", "Notice": "info",
            "Warning": "warn", "Error": "error", "Critical": "fatal",
            "Alert": "fatal", "Emergency": "fatal"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: ln.strip().startswith("####<")
                            or bool(self._STDOUT.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if s.startswith("####"):
            pass
        elif not self._STDOUT.match(s):
            return None
        parts = self._FIELD.findall(s)
        if len(parts) < 3:
            return None
        ts_raw, level, subsystem = parts[0], parts[1], parts[2]
        # message is the last non-empty angle-bracket field, msgid is <BEA-######>
        msg = ""
        msgid = ""
        for p in parts:
            if re.match(r"^[A-Z]{2,4}-\d{4,}$", p.strip()):
                msgid = p.strip()
            elif p.strip() and p != ts_raw and p not in (level, subsystem):
                msg = p.strip()
        return self._event(level=self._LVL.get(level, "info"), message=msg or level,
                           source=f"weblogic.{subsystem}",
                           ts_ms=parse_timestamp(ts_raw),
                           fields={"subsystem": subsystem, "message_id": msgid,
                                   "wls_severity": level}, raw=line)


# ── W3C Extended Log Format (IIS, CloudFront, WebLogic access, HAProxy W3C) ──
#   #Fields: date time s-ip cs-method cs-uri-stem sc-status
#   2019-12-04 21:02:31 192.0.2.100 GET /index.html 200
class W3CAccessAdapter(LogAdapter):
    name = "w3c_access"
    language = "any"
    _DIRECTIVE = re.compile(r"^#(Version|Fields|Software|Date|Start-Date|End-Date|Remark):", re.I)
    # Detection data row: TAB-separated only (CloudFront/WebLogic-ELF). A bare
    # SPACE-separated IIS data row is deliberately NOT a detection signal — it is
    # indistinguishable from "date time <free text>" lines (etcd, mssql, …) — so
    # a space-only W3C log is detected via its mandatory '#Fields:' header. Once
    # w3c wins, parse_line still handles both tab- and space-separated rows.
    # BATCH-5 gap fix: escaped log shipping (and the CloudFront catalog sample)
    # carries the TWO-CHAR "\t" sequence instead of a real tab — accept both,
    # exactly like split_any() does for literal "\n".
    _DATA_TAB = re.compile(r"^\d{4}-\d{2}-\d{2}(?:\t|\\t)\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\t|\\t)")
    _DATA_ANY = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ \t]|\\t)\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[ \t]|\\t)")

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda ln: bool(self._DIRECTIVE.match(ln.lstrip())
                            or self._DATA_TAB.match(ln.rstrip("\r\n"))))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if "\\t" in s and "\t" not in s:       # literal-\t row → real tabs
            s = s.replace("\\t", "\t")
        if self._DIRECTIVE.match(s.lstrip()):
            return self._event(level="", message=s.strip(), source="w3c",
                               fields={"w3c_directive": True}, raw=line)
        if not self._DATA_ANY.match(s):
            return None
        cols = s.split("\t") if "\t" in s else re.split(r"\s+", s.strip())
        ts_ms = parse_timestamp(f"{cols[0]} {cols[1]}") if len(cols) >= 2 else None
        # find an HTTP status (3-digit) among the columns for a level hint.
        # BATCH-5 latent-bug fix: in every W3C dialect (IIS, ELF, CloudFront)
        # cs-method precedes sc-status, while byte-count columns (CloudFront
        # sc-bytes=392) can precede the method — so when a method column is
        # present, only look AFTER it.
        _METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS",
                    "CONNECT", "TRACE", "PROPFIND", "MKCOL"}
        start = 2
        for i, c in enumerate(cols):
            if c in _METHODS:
                start = i + 1
                break
        status = None
        for c in cols[start:]:
            if c.isdigit() and len(c) == 3 and c[0] in "12345":
                status = int(c)
                break
        level = ("error" if status and status >= 500 else "warn"
                 if status and status >= 400 else "info")
        return self._event(level=level, message=s.strip(), source="w3c", ts_ms=ts_ms,
                           fields={"status": status, "columns": len(cols)}, raw=line)


# ── IBM WebSphere traditional SystemOut.log / Liberty messages.log (BATCH 3) ──
#   [3/14/18 20:13:23:123 CDT] 0000009f ServletWrappe I com.ibm… init SRV0242EI: …
#   [10/18/21 14:50:58:159 EDT] 0000003e com.ibm.ws.kernel…FeatureManager  A CWWKF0011I: …
class WebSphereAdapter(LogAdapter):
    name = "websphere"
    language = "java"
    _RE = re.compile(
        r"^\[(?P<ts>\d{1,2}/\d{1,2}/\d{2} \d{1,2}:\d{2}:\d{2}:\d{3})\s+(?P<tz>[A-Z]{2,5})\]\s+"
        r"(?P<tid>[0-9A-Fa-f]{8})\s+(?P<comp>\S+)\s+"
        r"(?P<lvl>[AIWEOFDRZ123])\s+(?P<msg>.*)$")
    _LVL = {"A": "info", "I": "info", "O": "info", "R": "info", "D": "debug",
            "Z": "debug", "1": "trace", "2": "trace", "3": "trace",
            "W": "warn", "E": "error", "F": "fatal"}

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    @staticmethod
    def _ts(text: str):
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2}) (\d{1,2}):(\d{2}):(\d{2}):(\d{3})",
                     text or "")
        if not m:
            return None
        mo, dy, yy, hh, mm, ss, ms = (int(x) for x in m.groups())
        try:                                   # zone name isn't resolvable → naive/local
            return _to_ms(datetime(2000 + yy, mo, dy, hh, mm, ss, ms * 1000))
        except ValueError:
            return None

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"]
        fields = {"thread_id": g["tid"], "component": g["comp"]}
        mid = re.match(r"^(?:[\w.$]+\s+[\w$]+\s+)?(?P<msgid>[A-Z]{4,5}\d{4}[A-Z]?):\s", msg)
        if mid:
            fields["message_id"] = mid.group("msgid")
        return self._event(level=self._LVL.get(g["lvl"], ""), message=msg,
                           source=g["comp"], ts_ms=self._ts(g["ts"]),
                           fields=fields, raw=line)


# ── GlassFish / Payara Uniform Log Format (ULF) (BATCH 3) ────────────────────
#   [#|2013-04-18T09:27:44.315-0700|INFO|glassfish 4.0|javax.enterprise.web.core|_ThreadID=15;…|msg|#]
class GlassfishUlfAdapter(LogAdapter):
    name = "glassfish_ulf"
    language = "java"
    _LVL = {"SEVERE": "error", "ALERT": "fatal", "EMERGENCY": "fatal",
            "WARNING": "warn", "INFO": "info", "CONFIG": "info",
            "FINE": "debug", "FINER": "trace", "FINEST": "trace"}

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines,
            lambda ln: str(ln).strip().startswith("[#|") and str(ln).count("|") >= 5)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if not s.startswith("[#|"):
            return None
        body = s[3:]
        if body.endswith("|#]"):
            body = body[:-3]
        parts = body.split("|", 5)
        if len(parts) < 6:
            return None
        ts, lvl, prod, logger, info, msg = parts
        fields = {"product": prod}
        for kv in info.split(";"):
            if "=" in kv:
                k, _, v = kv.partition("=")
                if k.strip():
                    fields[k.strip().lstrip("_").lower()] = v
        return self._event(level=self._LVL.get(lvl.upper(), lvl), message=msg,
                           source=logger or "glassfish", ts_ms=parse_timestamp(ts),
                           fields=fields, raw=line)


# ── Jetty StdErrLog (colon-separated) (BATCH 3) ──────────────────────────────
#   2016-10-21 15:31:01.370:INFO:oejs.Server:main: jetty-9.4.0-SNAPSHOT
class JettyAdapter(LogAdapter):
    name = "jetty"
    language = "java"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}):"
        r"(?P<lvl>DBUG|DEBUG|INFO|WARN|ERROR):"
        r"(?P<logger>[\w.$]+):(?P<thread>[^:]+):\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["lvl"], message=g["msg"], source=g["logger"],
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"thread": g["thread"]}, raw=line)


# ── Registration ─────────────────────────────────────────────────────────────
for _a in (TomcatCatalinaAdapter(), GunicornErrorAdapter(), ExpressMorganDevAdapter(),
           GlassfishOdlAdapter(), WebLogicAdapter(), W3CAccessAdapter(),
           WebSphereAdapter(), GlassfishUlfAdapter(), JettyAdapter()):
    register_adapter(_a)


# ═════════════════════════════════════════════════════════════════════════════
# BATCH 6
# ═════════════════════════════════════════════════════════════════════════════

# ── GlassFish / Payara HTTP access log (every field double-quoted) ───────────
#   "65.112.10.87" "NULL-AUTH-USER" "06/Mar/2018:05:22:41 -0500" "GET / HTTP/1.1" 200 52598
class GlassfishAccessAdapter(LogAdapter):
    name = "glassfish_access"
    language = "java"
    _RE = re.compile(
        r'^"(?P<ip>[^"]+)"\s+"(?P<user>[^"]*)"\s+'
        r'"(?P<ts>\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2}\s*[+-]\d{4})"\s+'
        r'"(?P<req>[^"]*)"\s+(?P<status>\d{3})\s+(?P<size>\d+|-)')

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        st = int(g["status"])
        level = "error" if st >= 500 else "warn" if st >= 400 else "info"
        return self._event(level=level, message=f'{g["req"]} -> {st}',
                           source="glassfish.access",
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"remote": g["ip"], "status": st,
                                   "request": g["req"],
                                   "user": None if g["user"] == "NULL-AUTH-USER" else g["user"],
                                   "bytes": None if g["size"] == "-" else int(g["size"])},
                           raw=line)


# ── WildFly / JBoss EAP console + server.log (jboss-logmanager pattern) ──────
#   08:10:40,347 INFO  [org.jboss.as] (Controller Boot Thread) WFLYSRV0025: … started
#   2022-02-22 16:04:09,053 INFO  [org.jboss.as] (Controller Boot Thread) … (server.log)
# The same "LEVEL [logger] (thread) msg" grammar covers the whole
# jboss-logmanager family: WildFly/EAP server.log, Keycloak, Quarkus console.
class WildflyConsoleAdapter(LogAdapter):
    name = "wildfly_console"
    language = "java"
    _RE = re.compile(
        r"^(?:\x1b\[\d+m)?(?P<time>(?:\d{4}-\d{2}-\d{2}[ T])?\d{2}:\d{2}:\d{2},\d{3})\s+"
        r"(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+"
        r"\[(?P<logger>[\w.$\-]+)\]\s+\((?P<thread>[^)]+)\)\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"].rstrip("\x1b[0m")
        fields = {"thread": g["thread"]}
        cm = re.match(r"^(?P<code>[A-Z]{3,8}\d{4,6}):\s", msg)
        if cm:
            fields["message_id"] = cm.group("code")
        return self._event(level=g["level"], message=msg, source=g["logger"],
                           ts_ms=parse_timestamp(g["time"].replace(",", ".")),
                           fields=fields, raw=line)


# batch 6 — glassfish_access is a quoted CLF cousin → before access_log keeps
# the tie explicit even though access_log cannot match the quoted form.
register_adapter(GlassfishAccessAdapter(), before="access_log")
# wildfly_console's dated server.log form shares the "TS,ms LEVEL …" prefix
# with the generic python_logging grammar → register before it so the strict
# jboss-logmanager "[logger] (thread)" grammar wins the 1.0 tie.
register_adapter(WildflyConsoleAdapter(), before="python_logging")


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH 8 — Java web-app / agent log4j stragglers (level-first + ODL + JUL)
# ══════════════════════════════════════════════════════════════════════════════
from ._common import RxAdapter, vocab_detect, block_ratio, split_any  # noqa: E402


# ── Nutanix Prism gateway (prism_gateway.log, log4j level-first) ──────────────
#   INFO 2023-08-25 23:16:03,147Z http-nio-…-exec-3 [] prism.aop.…invoke:96 Request…
class NutanixPrismAdapter(RxAdapter):
    name = "nutanix_prism"
    language = "java"
    default_source = "prism"
    _RE = re.compile(
        r"^(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+"
        r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}Z?)\s+"
        r"(?P<thread>\S+)\s+\[[^\]]*\]\s+(?P<logger>[\w.$]+):(?P<lineno>\d+)\s+(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"].rstrip("Z").replace(",", "."))

    def _fields(self, g, line):
        return {"thread": g["thread"], "logger": g["logger"], "line": int(g["lineno"])}

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            ev["source"] = self._RE.match(line.strip()).group("logger")
        return ev


# ── Apache Zeppelin (log4j '%5p [%d] ({%t} %F[%M]:%L) - %m') ───────────────────
#    INFO [2019-06-14 07:26:51,166] ({main} ZeppelinServer.java[main]:150) - Starting…
class ZeppelinLog4jAdapter(RxAdapter):
    name = "zeppelin_log4j"
    language = "java"
    default_source = "zeppelin"
    _RE = re.compile(
        r"^\s*(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+"
        r"\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\]\s+"
        r"\(\{(?P<thread>[^}]*)\}\s+(?P<file>\S+)\[(?P<method>[^\]]*)\]:(?P<lineno>\d+)\)\s+-\s+(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"].replace(",", "."))

    def _fields(self, g, line):
        return {"thread": g["thread"], "file": g["file"], "method": g["method"],
                "line": int(g["lineno"])}


# ── AppDynamics agent (log4j thread-first '[%t] %d %p %c - %m') ────────────────
#   [AD Thread Pool-Global0] 01 Jan 2023 12:00:00,123  INFO ConfigurationChannel - Started…
class AppdynamicsAdapter(RxAdapter):
    name = "appdynamics"
    language = "java"
    default_source = "appdynamics"
    _RE = re.compile(
        r"^\[(?P<thread>[^\]]+)\]\s+"
        r"(?P<ts>\d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2},\d{3})\s+"
        r"(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+(?P<logger>\S+)\s+-\s+(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"].replace(",", "."))

    def _fields(self, g, line):
        return {"thread": g["thread"], "logger": g["logger"]}

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            ev["source"] = self._RE.match(line.strip()).group("logger")
        return ev


# ── Sumo Logic collector (log4j2 with tz offset + com.sumologic logger) ───────
#   2023-01-01 12:00:00,123 -0800 INFO  [HTTP Sender - 1] com.sumologic.…Collector - …
class SumologicAdapter(RxAdapter):
    name = "sumologic"
    language = "java"
    default_source = "sumologic"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) (?P<tz>[+-]\d{4})\s+"
        r"(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+\[(?P<thread>[^\]]*)\]\s+"
        r"(?P<logger>com\.sumologic\.[\w.]+)\s+-\s+(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"].replace(",", ".") + " " + g["tz"])

    def _fields(self, g, line):
        return {"thread": g["thread"], "logger": g["logger"]}


# ── Payara notification.log (ODL grammar, fish.payara logger) ─────────────────
#   [2017-02-24T14:25:02.019+0000] [INFO] [] [fish.payara.…HealthCheckService] […] [[…]]
class PayaraNotificationAdapter(LogAdapter):
    name = "payara_notification"
    language = "java"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+[+-]\d{4})\]\s+"
        r"\[(?P<level>\w+)\]\s+\[[^\]]*\]\s+\[(?P<logger>fish\.payara\.[\w.]+)\]\s+(?P<rest>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: bool(self._RE.match(str(el).strip())))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        g = m.groupdict()
        body = re.search(r"\[\[\s*(.*?)\s*\]\]\s*$", g["rest"], re.S)
        msg = body.group(1) if body else g["rest"]
        return self._event(level=g["level"], message=msg.strip(), source=g["logger"],
                           ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── Resin JUL log ([%Y/%m/%d %H:%M:%S.%s] {%thread} logger -- msg) ────────────
#   [2019/03/14 12:00:00.123] {main} com.caucho.server.resin.Resin -- Resin-4.0.61 …
class ResinJulAdapter(RxAdapter):
    name = "resin_jul"
    language = "java"
    default_source = "resin"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}[/-]\d{2}[/-]\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]\s+"
        r"\{(?P<thread>[^}]*)\}\s+(?P<logger>[\w.$]+)?\s*(?:--\s*)?(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"].replace("/", "-"))

    def _fields(self, g, line):
        return {"thread": g["thread"], "logger": g.get("logger")}

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            m = self._RE.match(line.strip())
            if m and m.group("logger"):
                ev["source"] = m.group("logger")
        return ev


# level-first log4j formats share the leading LEVEL token; each grammar is
# mutually exclusive on structure but register nutanix/zeppelin before the
# generic `syslog`/`logfmt` fallbacks is unnecessary (they already floor higher).
for _a in (NutanixPrismAdapter(), ZeppelinLog4jAdapter(), AppdynamicsAdapter(),
           SumologicAdapter(), PayaraNotificationAdapter(), ResinJulAdapter()):
    register_adapter(_a)
