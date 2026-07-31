"""
Industrial / SCADA / OT log adapters (BATCH 5)
================================================================================
  ignition_wrapper : Inductive Automation Ignition wrapper.log (Tanuki Java
                     Service Wrapper) — `LEVEL  | jvm 1    | YYYY/MM/DD HH:MM:SS | msg`
  osisoft_pi       : OSIsoft PI / AVEVA PI message log (pigetmsg) — 2-line
                     records `I 09-Jan-19 10:20:14 pinetmgr:7` + `>> msg`
  wonderware       : Wonderware / AVEVA ArchestrA aaLog exports —
                     `Timestamp="…", LogFlag="…", Message="…", …` k="v" rows
  tpm2_eventlog    : tpm2_eventlog YAML (TPM2 measured-boot event log) —
                     `- EventNum:`/`PCRIndex:`/`EventType: EV_*` blocks
  conpot           : Conpot ICS/SCADA honeypot log — python-style ts + protocol
                     wording (modbus/s7comm/bacnet/…) with NO level word
"""
from __future__ import annotations

import re
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp,
                      ratio_detect, split_any, block_ratio, two_digit_year,
                      mk_ts, _MONTHS)


# ── Ignition wrapper.log (Tanuki Java Service Wrapper) ────────────────────────
#   INFO   | jvm 1    | 2019/01/09 16:22:21 | Syntax error
class IgnitionWrapperAdapter(LogAdapter):
    name = "ignition_wrapper"
    language = "java"
    _RE = re.compile(
        r"^(?P<level>INFO|ERROR|STATUS|WARN|DEBUG|FATAL)\s*\|\s*"
        r"(?P<jvm>jvm \d+|wrapper|wrapperm)\s*\|\s*"
        r"(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\s*\|\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        level = "info" if g["level"] == "STATUS" else g["level"]
        return self._event(level=level, message=g["msg"], source=g["jvm"],
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"wrapper_source": g["jvm"]}, raw=line)


# ── OSIsoft PI message log (pigetmsg output) ──────────────────────────────────
#   I 09-Jan-19 10:20:14 pinetmgr:7
#   >> Connection accepted: Process name: piartool(4321)
class OsisoftPiAdapter(LogAdapter):
    name = "osisoft_pi"
    language = "any"
    _HEAD = re.compile(
        r"^(?P<sev>[IWEC])\s+(?P<dy>\d{2})-(?P<mon>[A-Z][a-z]{2})-(?P<yy>\d{2})\s+"
        r"(?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})\s+(?P<src>[\w.]+):(?P<mid>\d+)\s*$")
    _CONT = re.compile(r"^>>\s?(?P<msg>.*)$")
    _LVL = {"I": "info", "W": "warn", "E": "error", "C": "fatal"}

    def _block_line(self, s: str) -> bool:
        return bool(self._HEAD.match(s.strip()) or self._CONT.match(s.strip()))

    def detect(self, sample_lines):
        # a header line must be present — '>> ' continuations alone are too generic.
        def ok(el):
            subs = split_any(el)
            return (any(self._HEAD.match(x.strip()) for x in subs)
                    and block_ratio(el, lambda x: self._block_line(x)))
        return ratio_detect(sample_lines, ok)

    def _ts(self, g) -> Optional[float]:
        if g["mon"] not in _MONTHS:
            return None
        return mk_ts(two_digit_year(g["yy"]), _MONTHS[g["mon"]], g["dy"],
                     g["hh"], g["mi"], g["ss"])

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if len(subs) > 1:                      # whole 2-line record → one event
            hm = next((self._HEAD.match(x.strip()) for x in subs
                       if self._HEAD.match(x.strip())), None)
            if not hm:
                return None
            g = hm.groupdict()
            msgs = [self._CONT.match(x.strip()).group("msg") for x in subs
                    if self._CONT.match(x.strip())]
            return self._event(level=self._LVL.get(g["sev"], "info"),
                               message=" ".join(msgs) or f'PI message {g["mid"]}',
                               source=g["src"], ts_ms=self._ts(g),
                               fields={"message_id": int(g["mid"])}, raw=line)
        st = s.strip()
        m = self._HEAD.match(st)
        if m:
            g = m.groupdict()
            return self._event(level=self._LVL.get(g["sev"], "info"),
                               message=f'PI message {g["mid"]}', source=g["src"],
                               ts_ms=self._ts(g),
                               fields={"message_id": int(g["mid"])}, raw=line)
        m = self._CONT.match(st)
        if m:
            return self._event(level="", message=m.group("msg"), source="pi",
                               raw=line)
        return None


# ── Wonderware / AVEVA ArchestrA aaLog export ─────────────────────────────────
#   Timestamp="2015-06-01 09:05:03.421", LogFlag="Warning", Message="…", …
class WonderwareAdapter(LogAdapter):
    name = "wonderware"
    language = "any"
    _ANCHOR = re.compile(r'^Timestamp="(?P<ts>\d{4}-\d{2}-\d{2} [\d:.]+)",\s*LogFlag="(?P<flag>\w+)"')
    _PAIR = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')
    _LVL = {"Error": "error", "Warning": "warn", "Info": "info",
            "Trace": "trace", "Debug": "debug", "Fatal": "fatal"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._ANCHOR.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._ANCHOR.match(s)
        if not m:
            return None
        pairs = dict(self._PAIR.findall(s))
        level = self._LVL.get(pairs.get("LogFlag", ""), pairs.get("LogFlag", ""))
        fields = {k: v for k, v in pairs.items()
                  if k not in ("Timestamp", "LogFlag", "Message", "Component")}
        return self._event(level=level, message=pairs.get("Message", ""),
                           source=pairs.get("Component") or pairs.get("ProcessName")
                           or "archestra",
                           ts_ms=parse_timestamp(m.group("ts")),
                           fields=fields or None, raw=line)


# ── tpm2_eventlog YAML (TPM2 measured-boot event log) ─────────────────────────
#   - EventNum: 1
#     PCRIndex: 0
#     EventType: EV_S_CRTM_VERSION
class Tpm2EventlogAdapter(LogAdapter):
    name = "tpm2_eventlog"
    language = "any"
    _KEY = re.compile(
        r"^\s*-?\s*(EventNum|PCRIndex|EventType|DigestCount|Digests|AlgorithmId|"
        r"Digest|EventSize|Event|SpecID|platformClass|specVersionMajor|"
        r"specVersionMinor|specErrata|uintnSize|numberOfAlgorithms|digestSizes|"
        r"Signature|VariableName|UnicodeNameLength|VariableDataLength|"
        r"UnicodeName|VariableData|pcrs|sha1|sha256|sha384)\s*:")
    _ETYPE = re.compile(r"EventType:\s*(EV_\w+)")

    def _anchored(self, el) -> bool:
        return "EV_" in str(el) or "EventNum" in str(el) or "SpecID" in str(el)

    def detect(self, sample_lines):
        def ok(el):
            return self._anchored(el) and block_ratio(el, lambda x: bool(self._KEY.match(x)),
                                                      threshold=0.6)
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if len(subs) > 1:                      # whole event block → one event
            if not block_ratio(s, lambda x: bool(self._KEY.match(x)), threshold=0.6):
                return None
            fields = {}
            for x in subs:
                km = re.match(r"^\s*-?\s*([A-Za-z]\w*)\s*:\s*(.*)$", x)
                if km and km.group(2):
                    fields.setdefault(km.group(1), km.group(2).strip().strip('"'))
            etype = fields.get("EventType", "")
            msg = (f'PCR {fields.get("PCRIndex", "?")} {etype}'
                   if etype else "TPM2 event")
            return self._event(level="", message=msg, source="tpm2.eventlog",
                               category="event", fields=fields, raw=line)
        if not self._KEY.match(s):
            return None
        km = re.match(r"^\s*-?\s*([A-Za-z]\w*)\s*:\s*(.*)$", s)
        return self._event(level="", message=s.strip(), source="tpm2.eventlog",
                           category="event",
                           fields={km.group(1): km.group(2).strip().strip('"')}
                           if km and km.group(2) else None, raw=line)


# ── Conpot ICS/SCADA honeypot ──────────────────────────────────────────────────
#   2018-08-09 19:13:00,438 New Modbus connection from 127.0.0.1:52011
class ConpotAdapter(LogAdapter):
    name = "conpot"
    language = "python"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+(?P<msg>\S.*)$")
    _PROTO = re.compile(
        r"(?i)\b(modbus|s7comm|bacnet|iec ?104|enip|ethernet/ip|kamstrup|"
        r"guardian_ast|conpot|ipmi|snmp agent|ftp session|tftp)\b")

    def detect(self, sample_lines):
        def ok(ln):
            m = self._RE.match(ln.strip())
            return bool(m and self._PROTO.search(m.group("msg")))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"]
        pm = self._PROTO.search(msg)
        proto = pm.group(1).lower() if pm else None
        # a honeypot touch is inherently suspicious — connections rate a warn.
        level = "warn" if re.search(r"(?i)new .*connection|attack|traffic", msg) else "info"
        ipm = re.search(r"from\s+(\d+\.\d+\.\d+\.\d+):?(\d+)?", msg)
        fields = {"protocol": proto}
        if ipm:
            fields["peer"] = ipm.group(0)[5:]
        return self._event(level=level, message=msg, source="conpot",
                           ts_ms=parse_timestamp(g["ts"]), fields=fields, raw=line)


# ── Registration ──────────────────────────────────────────────────────────────
for _a in (IgnitionWrapperAdapter(), OsisoftPiAdapter(), WonderwareAdapter(),
           Tpm2EventlogAdapter(), ConpotAdapter()):
    register_adapter(_a)


# ═════════════════════════════════════════════════════════════════════════════
#  BATCH 7 — WinCC OA (PVSS) manager log + KEPServerEX event log
# ═════════════════════════════════════════════════════════════════════════════
from ._common import us_date_ts as _us_date_ts  # noqa: E402


# ── Siemens WinCC OA / PVSS manager log ───────────────────────────────────────
#   WCCILpmon   (0), 2019.01.09 10:22:33.123, SYS, INFO, 1, Manager Start
class WinccOaAdapter(LogAdapter):
    name = "winccoa"
    language = "any"
    _RE = re.compile(
        r"^(?P<mgr>WCC[A-Za-z0-9]+)\s+\((?P<num>\d+)\),\s+"
        r"(?P<yr>\d{4})\.(?P<mo>\d{2})\.(?P<dy>\d{2}) "
        r"(?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})\.(?P<ms>\d{3}),\s+"
        r"(?P<area>\w+),\s*(?P<sev>INFO|WARNING|SEVERE|FATAL|ERROR),\s*"
        r"(?P<code>\d+)[,\s]*(?P<msg>.*)$")
    _LVL = {"SEVERE": "error"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=self._LVL.get(g["sev"], g["sev"]),
                           message=g["msg"], source=g["mgr"],
                           ts_ms=mk_ts(g["yr"], g["mo"], g["dy"], g["hh"],
                                       g["mi"], g["ss"], int(g["ms"]) * 1000),
                           fields={"manager": g["mgr"], "manager_num": int(g["num"]),
                                   "area": g["area"], "code": int(g["code"])},
                           raw=line)


# ── PTC Kepware KEPServerEX / Thingworx Kepware event log (TSV export) ────────
#   5/14/2019 10:22:01 AM⇥Information⇥KEPServerEX\Runtime⇥Runtime service started.
class KepwareAdapter(LogAdapter):
    name = "kepware"
    language = "any"
    _RE = re.compile(
        r"^(?P<date>\d{1,2}/\d{1,2}/\d{4}) (?P<time>\d{1,2}:\d{2}:\d{2} [AP]M)"
        r"(?:\t|\\t)(?P<lvl>Information|Warning|Error|Security)"
        r"(?:\t|\\t)(?P<src>[^\t]+?)(?:\t|\\t)(?P<msg>.*)$")
    _LVL = {"Information": "info", "Warning": "warn", "Error": "error",
            "Security": "warn"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=self._LVL.get(g["lvl"], "info"), message=g["msg"],
                           source=g["src"],
                           ts_ms=_us_date_ts(g["date"], g["time"]),
                           fields={"event_kind": g["lvl"]}, raw=line)


for _a in (WinccOaAdapter(), KepwareAdapter()):
    register_adapter(_a)


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH 8 — SCADA / OT / building-automation + OPC UA stacks
# ══════════════════════════════════════════════════════════════════════════════
import re as _re8  # noqa: E402
from datetime import datetime as _dt8  # noqa: E402
from ._common import (RxAdapter, vocab_detect, block_ratio, split_any,  # noqa: E402
                      ratio_detect, _MONTHS as _MONTHS8, _to_ms as _to_ms8)


# ── Tridium Niagara 4 / JACE station output ───────────────────────────────────
#   INFO [16:41:12 04-Jan-18 EST][web] Starting web server on port 443
class NiagaraStationAdapter(RxAdapter):
    name = "niagara_station"
    language = "java"
    default_source = "niagara"
    _RE = _re8.compile(
        r"^(?P<level>FINEST|FINER|FINE|CONFIG|INFO|WARNING|SEVERE)\s+"
        r"\[(?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2}) (?P<dy>\d{2})-(?P<mon>[A-Z][a-z]{2})-(?P<yr>\d{2}) (?P<zone>\w+)\]"
        r"\[(?P<comp>[\w.]+)\]\s*(?P<msg>.*)$")
    _LVL = {"FINEST": "trace", "FINER": "trace", "FINE": "debug", "CONFIG": "debug",
            "INFO": "info", "WARNING": "warn", "SEVERE": "error"}

    def _ts(self, g):
        if g["mon"] not in _MONTHS8:
            return None
        try:
            return _to_ms8(_dt8(2000 + int(g["yr"]), _MONTHS8[g["mon"]], int(g["dy"]),
                                int(g["hh"]), int(g["mi"]), int(g["ss"])))
        except ValueError:
            return None

    def _level(self, g, line):
        return self._LVL.get(g["level"], "info")

    def _fields(self, g, line):
        return {"component": g["comp"], "zone": g["zone"]}


# ── Siemens SIMATIC S7 PLC diagnostic buffer (STEP7/TIA text export) ──────────
#   Event 1 of 10:  Event ID 16# 4302
class SiemensS7Adapter(LogAdapter):
    name = "siemens_s7"
    language = "any"
    _RE = _re8.compile(
        r"^Event (?P<n>\d+) of (?P<tot>\d+):\s+Event ID 16#\s*(?P<eid>[0-9A-Fa-f]{4})")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            return bool(subs) and self._RE.match(subs[0].strip()) is not None
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        subs = split_any(s)
        m = self._RE.match(subs[0].strip()) if subs else None
        if not m:
            return None
        g = m.groupdict()
        detail = subs[1].strip() if len(subs) > 1 else ""
        return self._event(level="warn", message=f'Event ID 16#{g["eid"]} {detail}'.strip(),
                           source="siemens.s7",
                           fields={"event_index": int(g["n"]), "total": int(g["tot"]),
                                   "event_id": g["eid"]},
                           category="event", raw=line)


# ── open62541 OPC UA SDK stdout logger ────────────────────────────────────────
#   [2023-11-08 10:22:07.833 (UTC+0000)] info/server\tTCP network layer listening…
class Open62541Adapter(RxAdapter):
    name = "open62541"
    language = "c"
    default_source = "open62541"
    _RE = _re8.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) \(UTC(?P<tz>[+-]\d{4})\)\]\s+"
        r"(?P<level>trace|debug|info|warn|error|fatal)/(?P<comp>network|channel|session|server|"
        r"client|application|security|eventloop|pubsub|discovery)\t?(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"] + " " + g["tz"])

    def _fields(self, g, line):
        return {"subsystem": g["comp"]}


# ── Rapid SCADA application logs (ScadaServer/ScadaComm/ScadaWeb) ─────────────
#   2019-01-09 10:22:33 <SERVER1><ScadaServerService><ACT> Start logic processing
class RapidScadaAdapter(RxAdapter):
    name = "rapidscada"
    language = "dotnet"
    default_source = "rapidscada"
    _RE = _re8.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) <(?P<host>[^>]*)><(?P<svc>[^>]*)>"
        r"<(?P<lvl>INF|ACT|ERR|EXC)>\s*(?P<msg>.*)$")
    _LVL = {"INF": "info", "ACT": "info", "ERR": "error", "EXC": "error"}

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return self._LVL.get(g["lvl"], "info")

    def _fields(self, g, line):
        return {"host": g["host"], "service": g["svc"], "kind": g["lvl"]}


# ── Scada-LTS / Mango M2M web SCADA (ma.log, log4j level-first) ────────────────
#   INFO  2019-01-09 10:22:33,123 (org.scada_lts.…DataPointService.save:214) - Data…
class ScadaLtsMangoAdapter(RxAdapter):
    name = "scadalts_mango"
    language = "java"
    default_source = "scadalts"
    _RE = _re8.compile(
        r"^(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+"
        r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+"
        r"\((?P<loc>[\w.$]+:\d+)\)\s+-\s+(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"].replace(",", "."))

    def _fields(self, g, line):
        return {"location": g["loc"]}


for _a in (NiagaraStationAdapter(), SiemensS7Adapter(), Open62541Adapter(),
           RapidScadaAdapter(), ScadaLtsMangoAdapter()):
    register_adapter(_a)
