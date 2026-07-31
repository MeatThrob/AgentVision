"""
Telephony / VoIP / healthcare-interface log adapters (BATCH 4)
================================================================================
Raw SIP messages (RFC 3261), Asterisk's four non-full-log formats (AMI event
blocks, CDR Master.csv, queue_log, `pjsip set logger on` traces), the PJSIP
library log, and the healthcare wire formats HL7 v2 (ER7) and ASTM E1394/LIS2
plus the Mirth Connect integration-engine server log.

Call-ID / Uniqueid / message-control-id all map to trace_id so a call or an
HL7 exchange can be followed across events like any distributed trace.
"""
from __future__ import annotations

import csv
import io
import re
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp,
                      ratio_detect, block_ratio, split_any, compact_ts)

_SIP_METHODS = ("INVITE", "ACK", "BYE", "CANCEL", "OPTIONS", "REGISTER",
                "SUBSCRIBE", "NOTIFY", "REFER", "INFO", "MESSAGE", "UPDATE",
                "PRACK", "PUBLISH")


def _sip_headers(pieces: list) -> dict:
    """Pull the high-signal SIP headers out of a message's logical lines."""
    out = {}
    for x in pieces:
        m = re.match(r"^(Call-ID|i|CSeq|From|f|To|t|Via|v|User-Agent|Contact|m)"
                     r"\s*:\s*(.+)$", x.strip(), re.IGNORECASE)
        if m:
            key = {"i": "call-id", "f": "from", "t": "to", "v": "via",
                   "m": "contact"}.get(m.group(1).lower(), m.group(1).lower())
            out.setdefault(key, m.group(2).strip())
    return out


# ── Raw SIP message (request or response + headers) ───────────────────────────
#   INVITE sip:bob@biloxi.example.com SIP/2.0 / SIP/2.0 200 OK
class SipRawAdapter(LogAdapter):
    name = "sip_raw"
    language = "any"
    _REQ = re.compile(r"^(?P<m>" + "|".join(_SIP_METHODS) + r")\s+"
                      r"(?P<uri>sips?:\S+)\s+SIP/2\.0\s*$")
    _RSP = re.compile(r"^SIP/2\.0\s+(?P<code>\d{3})\s+(?P<reason>.*)$")

    def detect(self, sample_lines):
        def first_hit(el):
            pieces = split_any(el)
            if not pieces:
                return False
            head = pieces[0].strip()
            return bool(self._REQ.match(head) or self._RSP.match(head))
        return ratio_detect(sample_lines, first_hit)

    def parse_line(self, line: str) -> Optional[dict]:
        pieces = split_any(line)
        if not pieces:
            return None
        head = pieces[0].strip()
        hdrs = _sip_headers(pieces[1:])
        m = self._REQ.match(head)
        if m:
            fields = {"method": m.group("m"), "uri": m.group("uri"), **hdrs}
            return self._event(level="info", message=head, source="sip",
                               trace_id=hdrs.get("call-id"), fields=fields,
                               raw=line)
        m = self._RSP.match(head)
        if m:
            code = int(m.group("code"))
            level = "error" if code >= 500 else "warn" if code >= 400 else "info"
            fields = {"status": code, "reason": m.group("reason").strip(), **hdrs}
            return self._event(level=level, message=head, source="sip",
                               trace_id=hdrs.get("call-id"), fields=fields,
                               raw=line)
        return None


# ── Asterisk `pjsip set logger on` SIP trace wrapper ──────────────────────────
#   <--- Received SIP request (982 bytes) from UDP:203.0.113.10:5060 --->
class AsteriskSipTraceAdapter(LogAdapter):
    name = "asterisk_siptrace"
    language = "any"
    _WRAP = re.compile(
        r"^<--- (?P<dir>Received|Transmitting) SIP (?P<kind>request|response) "
        r"\((?P<bytes>\d+) bytes\) (?:from|to) (?P<proto>UDP|TCP|TLS|WSS?):"
        r"(?P<peer>\S+?) --->$")
    _OLD = re.compile(r"^<--- SIP (read from|transmitting)"
                      r"(?: \(\d+ bytes\))? (?:to )?(?P<proto2>UDP|TCP|TLS):")

    def detect(self, sample_lines):
        def hit(el):
            pieces = split_any(el)
            return bool(pieces and (self._WRAP.match(pieces[0].strip())
                                    or self._OLD.match(pieces[0].strip())))
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        pieces = split_any(line)
        if not pieces:
            return None
        head = pieces[0].strip()
        m = self._WRAP.match(head) or self._OLD.match(head)
        if not m:
            return None
        g = m.groupdict()
        hdrs = _sip_headers(pieces[1:])
        first_sip = pieces[1].strip() if len(pieces) > 1 else ""
        fields = {k: v for k, v in g.items() if v}
        fields.update(hdrs)
        level = "info"
        rm = re.match(r"^SIP/2\.0\s+(\d{3})", first_sip)
        if rm:
            code = int(rm.group(1))
            level = "error" if code >= 500 else "warn" if code >= 400 else "info"
            fields["status"] = code
        return self._event(level=level,
                           message=first_sip or head, source="asterisk.pjsip",
                           trace_id=hdrs.get("call-id"), fields=fields, raw=line)


# ── Asterisk Manager Interface (AMI) event/response blocks ────────────────────
#   Event: Newchannel\r\nPrivilege: call,all\r\nUniqueid: 1656419172.1\r\n\r\n
class AsteriskAmiAdapter(LogAdapter):
    name = "asterisk_ami"
    language = "any"
    _FIRST = re.compile(r"^(?P<kind>Event|Response|Action):\s*(?P<val>\S.*)$")
    _AMI_KEYS = ("Privilege", "Uniqueid", "Channel", "ActionID", "ChannelState",
                 "CallerIDNum", "CallerIDName", "Exten", "Context", "Linkedid",
                 "AccountCode", "Message")

    def detect(self, sample_lines):
        def hit(el):
            pieces = split_any(el)
            if not pieces or not self._FIRST.match(pieces[0].strip()):
                return False
            return any(x.strip().split(":", 1)[0].strip() in self._AMI_KEYS
                       for x in pieces[1:] if ":" in x)
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        pieces = split_any(line)
        if not pieces:
            return None
        m = self._FIRST.match(pieces[0].strip())
        if not m:
            return None
        fields = {}
        for x in pieces:
            km = re.match(r"^([\w]+):\s*(.*)$", x.strip())
            if km:
                fields[km.group(1)] = km.group(2).strip()
        kind, val = m.group("kind"), m.group("val").strip()
        level = "error" if (kind == "Response" and val.lower() == "error") else "info"
        ts_ms = None
        uid = fields.get("Uniqueid")
        if uid:
            um = re.match(r"^(\d{9,11})\.\d+$", uid)
            if um:
                ts_ms = float(um.group(1)) * 1000.0
        return self._event(level=level, message=f"AMI {kind}: {val}",
                           source="asterisk.ami", ts_ms=ts_ms,
                           trace_id=uid or fields.get("ActionID"),
                           fields=fields, raw=line)


# ── Asterisk CDR Master.csv (18 quoted fields, no header) ─────────────────────
_CDR_COLS = ("accountcode", "src", "dst", "dcontext", "clid", "channel",
             "dstchannel", "lastapp", "lastdata", "start", "answer", "end",
             "duration", "billsec", "disposition", "amaflags", "uniqueid",
             "userfield")


class AsteriskCdrAdapter(LogAdapter):
    name = "asterisk_cdr"
    language = "any"
    _DISPO = ("ANSWERED", "NO ANSWER", "BUSY", "FAILED", "CONGESTION")

    def _row(self, s: str) -> Optional[list]:
        if not s.startswith('"'):
            return None
        try:
            row = next(csv.reader(io.StringIO(s)))
        except Exception:
            return None
        if len(row) < 15:
            return None
        if not any(x in self._DISPO for x in row):
            return None
        # ≥2 'YYYY-MM-DD HH:MM:SS' timestamps (start/answer/end; answer may be "")
        stamps = sum(1 for x in row
                     if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", x))
        return row if stamps >= 2 else None

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: self._row(str(ln).strip()) is not None)

    def parse_line(self, line: str) -> Optional[dict]:
        row = self._row(line.rstrip("\r\n").strip())
        if row is None:
            return None
        fields = dict(zip(_CDR_COLS, row))
        dispo = fields.get("disposition", "")
        level = {"FAILED": "error", "CONGESTION": "error", "BUSY": "warn",
                 "NO ANSWER": "warn"}.get(dispo, "info")
        return self._event(level=level,
                           message=f"CDR {fields.get('src', '?')}→"
                                   f"{fields.get('dst', '?')} {dispo}",
                           source="asterisk.cdr",
                           ts_ms=parse_timestamp(fields.get("end")
                                                 or fields.get("start") or ""),
                           trace_id=fields.get("uniqueid") or None,
                           fields=fields, raw=line)


# ── Asterisk queue_log (pipe-delimited call-center events) ────────────────────
#   1656419091|1656419090.4|support|SIP/agent1|CONNECT|5|1656419086.12|4
class AsteriskQueueAdapter(LogAdapter):
    name = "asterisk_queue"
    language = "any"
    _EVENTS = ("ENTERQUEUE", "CONNECT", "COMPLETECALLER", "COMPLETEAGENT",
               "ABANDON", "RINGNOANSWER", "AGENTLOGIN", "AGENTLOGOFF",
               "AGENTDUMP", "TRANSFER", "ADDMEMBER", "REMOVEMEMBER", "PAUSE",
               "UNPAUSE", "PAUSEALL", "UNPAUSEALL", "CONFIGRELOAD",
               "QUEUESTART", "DID", "EXITWITHTIMEOUT", "EXITWITHKEY",
               "EXITEMPTY", "SYSCOMPAT")
    _RE = re.compile(r"^(?P<ts>\d{9,11})\|(?P<callid>[^|]*)\|(?P<queue>[^|]+)\|"
                     r"(?P<agent>[^|]*)\|(?P<event>[A-Z]+)(?:\|(?P<rest>.*))?$")

    def detect(self, sample_lines):
        def hit(ln):
            m = self._RE.match(str(ln).strip())
            return bool(m and m.group("event") in self._EVENTS)
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m or m.group("event") not in self._EVENTS:
            return None
        g = m.groupdict()
        level = "warn" if g["event"] in ("ABANDON", "RINGNOANSWER",
                                         "EXITWITHTIMEOUT", "AGENTDUMP") else "info"
        fields = {"queue": g["queue"], "agent": g["agent"] or None,
                  "event": g["event"]}
        if g.get("rest"):
            fields["params"] = g["rest"].split("|")
        return self._event(level=level,
                           message=f"queue {g['queue']}: {g['event']}",
                           source="asterisk.queue",
                           ts_ms=float(g["ts"]) * 1000.0,
                           trace_id=g["callid"] or None, fields=fields, raw=line)


# ── PJSIP/PJSUA library log (softphones, pjsua CLI) ───────────────────────────
#    10:26:12.123   pjsua_core.c  .TX 1024 bytes Request msg INVITE/cseq=21437 …
class PjsipLibAdapter(LogAdapter):
    name = "pjsip_lib"
    language = "any"
    # sender column is a bare token (pjsua_core.c, tsx0x…, dlg0x…) — no
    # brackets, so Elixir's "HH:MM:SS.mmm [info] Msg" can never match.
    _RE = re.compile(r"^\s?(?P<ts>\d{2}:\d{2}:\d{2}\.\d{3})\s+"
                     r"(?P<src>[A-Za-z_][\w.\-#$]*)\s+"
                     r"(?P<dots>[.!]*)(?P<msg>[A-Z].*)$")

    def detect(self, sample_lines):
        def hit(el):
            pieces = split_any(el)
            return bool(pieces and self._RE.match(pieces[0]))
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        pieces = split_any(line)
        if not pieces:
            return None
        m = self._RE.match(pieces[0])
        if not m:
            return None
        g = m.groupdict()
        dots = g["dots"]
        level = ("error" if "!" in dots
                 else "trace" if len(dots) >= 3
                 else "debug" if len(dots) == 2 else "info")
        hdrs = _sip_headers(pieces[1:])
        fields = {"sender": g["src"]}
        fields.update(hdrs)
        return self._event(level=level, message=g["msg"].strip(),
                           source=f"pjsip.{g['src']}",
                           ts_ms=parse_timestamp(g["ts"]),
                           trace_id=hdrs.get("call-id"), fields=fields, raw=line)


# ── HL7 v2.x ER7 messages (MSH|^~\&|…) ────────────────────────────────────────
class Hl7v2Adapter(LogAdapter):
    name = "hl7v2"
    language = "any"
    _MSH = re.compile(r"^MSH\|\^~\\&\|")
    _SEG = re.compile(r"^(?P<seg>[A-Z][A-Z0-9]{2})\|")
    # BATCH-7 gap fix: MLLP wire framing — a captured frame wraps the message
    # in VT (0x0b) … FS CR (0x1c 0x0d). Captures are often pasted with the
    # LITERAL \xNN escape text instead of the raw bytes; strip both forms so
    # an MLLP frame routes here instead of falling to structural.
    _MLLP = re.compile(r"^(?:\x0b|\\x0b|\\v)+|(?:\x1c|\x0d|\\x1c|\\x0d|\\r)+$")

    def _unwrap(self, text: str) -> str:
        return self._MLLP.sub("", str(text))

    def detect(self, sample_lines):
        def hit(el):
            pieces = split_any(self._unwrap(el).replace("\r", "\n"))
            return bool(pieces and self._MSH.match(pieces[0].strip()))
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        pieces = split_any(self._unwrap(line).replace("\r", "\n"))
        if not pieces:
            return None
        head = pieces[0].strip()
        if not self._MSH.match(head):
            return None
        f = head.split("|")
        # MSH|enc|sending_app|sending_fac|recv_app|recv_fac|ts||type|ctrl_id|proc|ver
        def fld(i):
            return f[i] if len(f) > i else ""
        msg_type = fld(8).replace("^", " ")
        ack = ""
        segs = []
        for x in pieces[1:]:
            sm = self._SEG.match(x.strip())
            if sm:
                segs.append(sm.group("seg"))
                if sm.group("seg") == "MSA":
                    parts = x.strip().split("|")
                    ack = parts[1] if len(parts) > 1 else ""
        level = "error" if ack in ("AE", "AR") else "info"
        fields = {"message_type": fld(8), "sending_app": fld(2),
                  "sending_facility": fld(3), "receiving_app": fld(4),
                  "version": fld(11), "segments": segs or None}
        if ack:
            fields["ack_code"] = ack
        return self._event(level=level,
                           message=f"HL7 {msg_type or 'message'}".strip(),
                           source="hl7", ts_ms=compact_ts(fld(6)),
                           trace_id=fld(9) or None, fields=fields, raw=line)


# ── ASTM E1394 / CLSI LIS2-A2 analyzer frames ─────────────────────────────────
#   1H|\^&|||H500^910YOXH02826^2.2.2.2b|||||||Q|LIS2-A2|20230329110749BC
class AstmLis2Adapter(LogAdapter):
    name = "astm_lis2"
    language = "any"
    _RE = re.compile(r"^(?P<frame>[0-7])?(?P<rec>[HPORCQML])\|(?P<rest>.*)$")
    _REC = {"H": "header", "P": "patient", "O": "order", "R": "result",
            "C": "comment", "Q": "query", "M": "manufacturer", "L": "terminator"}

    def _hit(self, s: str) -> bool:
        m = self._RE.match(s)
        if not m:
            return False
        # header carries the |\^& delimiter definition; any record needs to be
        # pipe-dominated so prose starting "P|…" can't fire.
        return "\\^&" in s or s.count("|") >= 4

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda ln: block_ratio(ln, lambda x: self._hit(x.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m or not self._hit(s):
            return None
        g = m.groupdict()
        parts = s.split("|")
        fields = {"record_type": self._REC.get(g["rec"], g["rec"])}
        if g.get("frame"):
            fields["frame"] = int(g["frame"])
        ts_ms = None
        if g["rec"] == "H":
            fields["sender"] = parts[4].replace("^", " ") if len(parts) > 4 else ""
            tail = parts[-1] if parts else ""
            tm = re.match(r"^(\d{14})", tail)
            if tm:
                ts_ms = compact_ts(tm.group(1))
        return self._event(level="info",
                           message=f"ASTM {fields['record_type']} record",
                           source="astm", ts_ms=ts_ms, fields=fields, raw=line)


# ── Mirth Connect / NextGen Connect server log (level-first log4j2) ───────────
#   INFO  2026-07-21 12:00:00.123 [Main Server Thread] com.mirth…Mirth: started
class MirthConnectAdapter(LogAdapter):
    name = "mirth_connect"
    language = "java"
    _RE = re.compile(
        r"^(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+"
        r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+"
        r"\[(?P<thread>[^\]]*)\]\s+(?P<logger>[\w.$]+):\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"], source=g["logger"],
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"thread": g["thread"]}, raw=line)


# ── Registration ──────────────────────────────────────────────────────────────
# asterisk_siptrace before sip_raw is irrelevant (different first lines), but
# both must beat the generic fallbacks only — no core adapter claims these.
# pjsip_lib's bare wall-clock start shares nothing with elixir ([level] token)
# or sharpemu ([ts] bracket), so default placement is safe everywhere.
register_adapter(SipRawAdapter())
register_adapter(AsteriskSipTraceAdapter())
register_adapter(AsteriskAmiAdapter())
register_adapter(AsteriskCdrAdapter())
register_adapter(AsteriskQueueAdapter())
register_adapter(PjsipLibAdapter())
register_adapter(Hl7v2Adapter())
register_adapter(AstmLis2Adapter())
register_adapter(MirthConnectAdapter())


# ═════════════════════════════════════════════════════════════════════════════
#  BATCH 7 — 3CX activity log, Asterisk CLI verbose, Asterisk CEL CSV
# ═════════════════════════════════════════════════════════════════════════════
import csv as _csv  # noqa: E402
import io as _io  # noqa: E402


# ── 3CX Phone System activity log ─────────────────────────────────────────────
#   2026/01/05 10:26:12.123 [CM503001]: Call(C:123.1): Incoming call from Extn:100 …
class ThreeCxAdapter(LogAdapter):
    name = "threecx"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+"
        r"\[(?P<code>CM\d{6})\]:\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        low = g["msg"].lower()
        level = ("error" if "failed" in low or "error" in low else "info")
        fields = {"message_code": g["code"]}
        cm = re.match(r"^Call\(C:([\d.]+)\)", g["msg"])
        if cm:
            fields["call_id"] = cm.group(1)
        return self._event(level=level, message=g["msg"], source="3cx",
                           ts_ms=parse_timestamp(g["ts"]), fields=fields, raw=line)


# ── Asterisk console / rasterisk verbose output ───────────────────────────────
#       -- Executing [6001@from-internal:1] Answer("PJSIP/6001-00000001", "") in new stack
class AsteriskCliAdapter(LogAdapter):
    name = "asterisk_cli"
    language = "any"
    _RE = re.compile(r"^\s+(?P<mark>--|==|>)\s+(?P<msg>\S.*)$")
    _VOCAB = re.compile(
        r"in new stack|Executing \[|(?:PJSIP|SIP|IAX2|DAHDI|Local)/|"
        r"Registered|Unregistered|answered|Hungup|hung up|Remote UNIX connection|"
        r"Called \S+|Spawn extension")

    def _hit(self, s: str) -> bool:
        m = self._RE.match(s)
        return bool(m and self._VOCAB.search(m.group("msg")))

    def detect(self, sample_lines):
        def ok(el):
            return any(self._hit(x) for x in split_any(el))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        for x in split_any(line):
            if not self._hit(x):
                continue
            m = self._RE.match(x)
            g = m.groupdict()
            fields = {}
            em = re.match(r"^Executing \[(?P<ext>[^@\]]+)@(?P<ctx>[^:\]]+):(?P<pri>\d+)\]"
                          r"\s+(?P<app>\w+)\((?P<args>.*)\)", g["msg"])
            if em:
                fields = {"exten": em.group("ext"), "context": em.group("ctx"),
                          "priority": int(em.group("pri")), "app": em.group("app")}
            return self._event(level="info", message=g["msg"],
                               source="asterisk.cli", fields=fields or None,
                               raw=line)
        return None


# ── Asterisk CEL (Channel Event Logging) Master.csv ───────────────────────────
#   "CHAN_START","2026-01-05 10:26:12","Alice","1000",…
class AsteriskCelCsvAdapter(LogAdapter):
    name = "asterisk_cel_csv"
    language = "any"
    _EVENTS = {"CHAN_START", "CHAN_END", "ANSWER", "HANGUP", "APP_START",
               "APP_END", "BRIDGE_ENTER", "BRIDGE_EXIT", "LINKEDID_END",
               "BLINDTRANSFER", "ATTENDEDTRANSFER", "PICKUP", "FORWARD",
               "PARK_START", "PARK_END", "USER_DEFINED", "LOCAL_OPTIMIZE"}
    _RE = re.compile(r'^"(?P<ev>[A-Z_]+)","(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[^"]*)",')

    def _hit(self, s: str) -> bool:
        m = self._RE.match(s)
        return bool(m and m.group("ev") in self._EVENTS)

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: self._hit(str(ln).strip()))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if not self._hit(s):
            return None
        try:
            row = next(_csv.reader(_io.StringIO(s)))
        except Exception:
            return None
        names = ("event", "ts", "cid_name", "cid_num", "exten", "context",
                 "channel", "app", "app_data", "amaflags", "uniqueid", "linkedid")
        fields = {k: v for k, v in zip(names, row) if v and k not in ("event", "ts")}
        return self._event(level="info", message=f'{row[0]} {fields.get("channel", "")}'.strip(),
                           source="asterisk.cel", ts_ms=parse_timestamp(row[1]),
                           fields={"event": row[0], **fields},
                           category="event", raw=line)


for _a in (ThreeCxAdapter(), AsteriskCliAdapter(), AsteriskCelCsvAdapter()):
    register_adapter(_a)


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH 8 — FreeSWITCH ESL/CDR/sofia, SIPp traces, ngrep, AudioCodes SBC
# ══════════════════════════════════════════════════════════════════════════════
from ._common import RxAdapter, vocab_detect  # noqa: E402


# ── FreeSWITCH Event Socket (ESL) plain-format events ─────────────────────────
#   Event-Name: CHANNEL_ANSWER\nCore-UUID: …\nEvent-Date-Timestamp: 1767608772…
class FreeswitchEslAdapter(LogAdapter):
    name = "freeswitch_esl"
    language = "any"
    _KV = re.compile(r"^(?P<k>[A-Za-z][\w\-]*): (?P<v>.*)$")

    def detect(self, sample_lines):
        def hit(el):
            subs = [x.strip() for x in split_any(el)]
            has_evt = any(x.startswith("Event-Name:") for x in subs)
            kvs = sum(1 for x in subs if self._KV.match(x))
            return has_evt and kvs >= 2
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        fields = {}
        for x in split_any(s):
            m = self._KV.match(x.strip())
            if m:
                fields[m.group("k")] = m.group("v")
        if "Event-Name" not in fields:
            return None
        ts_ms = None
        edt = fields.get("Event-Date-Timestamp")
        if edt and edt.isdigit():
            ts_ms = float(edt) / 1000.0        # µs epoch → ms
        return self._event(level="", message=f'ESL event: {fields["Event-Name"]}',
                           source="freeswitch.esl", ts_ms=ts_ms,
                           fields={"event_name": fields["Event-Name"],
                                   "unique_id": fields.get("Unique-ID"),
                                   "core_uuid": fields.get("Core-UUID")},
                           category="event", raw=line)


# ── FreeSWITCH mod_cdr_csv (default 'example' template) ───────────────────────
#   "Alice","1000","2565551212","default","…start","…answer","…end","108",…"NORMAL_CLEARING",uuid,…
class FreeswitchCdrCsvAdapter(LogAdapter):
    name = "freeswitch_cdr_csv"
    language = "any"
    _CAUSES = ("NORMAL_CLEARING", "ORIGINATOR_CANCEL", "NO_ANSWER", "USER_BUSY",
               "CALL_REJECTED", "NORMAL_TEMPORARY_FAILURE", "RECOVERY_ON_TIMER_EXPIRE",
               "NO_USER_RESPONSE", "NORMAL_UNSPECIFIED")
    _UUID = re.compile(r'"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"')

    def detect(self, sample_lines):
        def hit(x):
            x = x.strip()
            return (x.startswith('"') and self._UUID.search(x)
                    and any(f'"{c}"' in x for c in self._CAUSES))
        return ratio_detect(sample_lines, lambda el: block_ratio(el, hit))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        cells = re.findall(r'"([^"]*)"', s)
        cause = next((c for c in cells if c in self._CAUSES), "")
        uuid = next((c for c in cells if re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", c)), "")
        level = "info" if cause == "NORMAL_CLEARING" else "warn"
        return self._event(level=level,
                           message=f'CDR {cells[1] if len(cells) > 1 else ""}→'
                                   f'{cells[2] if len(cells) > 2 else ""} [{cause}]',
                           source="freeswitch.cdr",
                           fields={"hangup_cause": cause, "uuid": uuid,
                                   "fields": len(cells)},
                           category="event", raw=line)


# ── FreeSWITCH mod_xml_cdr per-call XML ───────────────────────────────────────
#   <cdr core-uuid="…"><channel_data>…</channel_data><variables>…</variables></cdr>
class FreeswitchCdrXmlAdapter(LogAdapter):
    name = "freeswitch_cdr_xml"
    language = "any"
    _ROOT = re.compile(r"^\s*<cdr[ >]")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            return bool(subs) and self._ROOT.match(subs[0]) is not None
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        if not self._ROOT.match(s.lstrip()):
            return None
        cause = re.search(r"<hangup_cause>([^<]*)</hangup_cause>", s)
        uuid = re.search(r'core-uuid="([^"]*)"', s)
        dur = re.search(r"<duration>(\d+)</duration>", s)
        fields = {}
        if uuid:
            fields["core_uuid"] = uuid.group(1)
        if dur:
            fields["duration"] = int(dur.group(1))
        cv = cause.group(1) if cause else ""
        if cv:
            fields["hangup_cause"] = cv
        return self._event(level="info" if cv in ("", "NORMAL_CLEARING") else "warn",
                           message=f'CDR XML [{cv or "call"}]',
                           source="freeswitch.cdr", fields=fields or None,
                           category="event", raw=line)


# ── FreeSWITCH 'sofia global siptrace on' (nua/tport SIP dump) ─────────────────
#   recv 1330 bytes from udp/[203.0.113.10]:5060 at 10:26:12.123456:
class FreeswitchSofiaAdapter(RxAdapter):
    name = "freeswitch_sofia"
    language = "any"
    match_scope = "first"
    default_source = "freeswitch.sofia"
    _RE = re.compile(
        r"^(?P<dir>send|recv) (?P<bytes>\d+) bytes (?P<prep>to|from) "
        r"(?P<proto>udp|tcp|tls|ws|wss)/\[?(?P<peer>[^\]]+?)\]?:(?P<port>\d+) "
        r"at (?P<ts>[\d:]+\.\d+):")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return ""

    def _fields(self, g, line):
        return {"direction": g["dir"], "bytes": int(g["bytes"]),
                "protocol": g["proto"], "peer": g["peer"], "port": int(g["port"])}

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            m = self._RE.match(split_any(line)[0].strip())
            ev["data"]["message"] = f'SIP {m.group("dir")} {m.group("bytes")}b {m.group("prep")} {m.group("peer")}'
            ev["category"] = "event"
        return ev


# ── SIPp load generator message-log + error-log ───────────────────────────────
#   ----------- 2026-01-05 10:26:12.123456\nUDP message sent (523 bytes):\nINVITE…
#   2026-01-05 10:26:12.123456 1767608772.123456: Aborting call on unexpected …
class SippAdapter(LogAdapter):
    name = "sipp"
    language = "any"
    _MSGLOG = re.compile(r"^-{5,}\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+\s*$")
    _MSGBODY = re.compile(r"^(?:UDP|TCP|TLS) message (?:sent|received)")
    _ERRLOG = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6})\s+"
        r"(?P<epoch>\d+\.\d{6}):\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        def hit(el):
            subs = [x.strip() for x in split_any(el)]
            if not subs:
                return False
            if self._MSGLOG.match(subs[0]) or any(self._MSGBODY.match(x) for x in subs):
                return True
            return bool(self._ERRLOG.match(subs[0]))
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        subs = [x.strip() for x in split_any(s)]
        m = self._ERRLOG.match(subs[0]) if subs else None
        if m and not any(self._MSGBODY.match(x) for x in subs):
            g = m.groupdict()
            level = "error" if re.search(r"abort|unexpected|error|fail", g["msg"], re.I) else "info"
            return self._event(level=level, message=g["msg"], source="sipp",
                               ts_ms=parse_timestamp(g["ts"]),
                               fields={"epoch": g["epoch"]}, raw=line)
        # message-log block
        dirn = next((x for x in subs if self._MSGBODY.match(x)), "")
        tsm = next((x for x in subs if self._MSGLOG.match(x)), "")
        ts_ms = parse_timestamp(tsm.strip("- ").strip()) if tsm else None
        return self._event(level="", message=dirn or (subs[0] if subs else s),
                           source="sipp", ts_ms=ts_ms,
                           fields={"block_lines": len(subs)} if len(subs) > 1 else None,
                           category="event", raw=line)


# ── ngrep / sipgrep packet trace ──────────────────────────────────────────────
#   U 2026/01/05 10:26:12.123456 203.0.113.10:5060 -> 10.0.0.1:5060\nINVITE …
class NgrepSipAdapter(RxAdapter):
    name = "ngrep_sip"
    language = "any"
    match_scope = "first"
    default_source = "ngrep"
    _RE = re.compile(
        r"^(?P<proto>[UT]) (?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d+) "
        r"(?P<src>[\d.]+:\d+) -> (?P<dst>[\d.]+:\d+)")

    def _ts(self, g):
        return parse_timestamp(g["ts"].replace("/", "-"))

    def _level(self, g, line):
        return ""

    def _fields(self, g, line):
        return {"protocol": "UDP" if g["proto"] == "U" else "TCP",
                "src": g["src"], "dst": g["dst"]}

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            m = self._RE.match(split_any(line)[0].strip())
            ev["data"]["message"] = f'{m.group("src")} -> {m.group("dst")}'
            ev["category"] = "event"
        return ev


# ── AudioCodes SBC / Mediant gateway debug syslog ─────────────────────────────
#   10.0.0.20 local0.notice [S=1234567] [SID=board01:12:345678] (N 1234567) …
class AudiocodesAdapter(LogAdapter):
    name = "audiocodes"
    language = "any"
    _SEQ = re.compile(r"\[S=\d+\]")
    _SID = re.compile(r"\[SID=[^\]]+\]")
    _TIME = re.compile(r"\[Time:(\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]")

    def detect(self, sample_lines):
        def hit(x):
            return bool(self._SEQ.search(x) and self._SID.search(x))
        return vocab_detect(sample_lines, lambda el: block_ratio(el, hit), cap=0.9)

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        if not (self._SEQ.search(s) and self._SID.search(s)):
            return None
        tm = self._TIME.search(s)
        sid = self._SID.search(s)
        level = "error" if re.search(r"\berror|fail", s, re.I) else "info"
        return self._event(level=level, message=s, source="audiocodes.sbc",
                           ts_ms=parse_timestamp(tm.group(1)) if tm else None,
                           fields={"session_id": sid.group(0)[5:-1]},
                           raw=line)


for _a in (FreeswitchEslAdapter(), FreeswitchCdrCsvAdapter(), FreeswitchCdrXmlAdapter(),
           FreeswitchSofiaAdapter(), SippAdapter(), NgrepSipAdapter(),
           AudiocodesAdapter()):
    register_adapter(_a)
