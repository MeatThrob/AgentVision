"""
Mainframe / midrange / legacy-UNIX log adapters (BATCH 4)
================================================================================
IBM z/OS (SYSLOG hardcopy, JES2 job logs, bare MVS message-id streams, CICS,
RACF ICH408I + IRRADU00 SMF unloads), IBM i (job logs, QHST history), AIX errpt,
and Solaris/illumos (FMA fault logs, SMF service logs). All text renderings —
the raw binary SMF/journal records stay in log_sources.py per the roadmap.

Level convention: MVS message-id severity suffix (I/E/W/A/D), RACF
VIOLATION qualifiers, IBM i Escape/Diagnostic message types, AIX errpt type
letters, and Solaris severities all map onto the canonical vocabulary so a
mainframe abend lands category=="error" exactly like a Python traceback.
"""
from __future__ import annotations

import re
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp, _MONTHS,
                      ratio_detect, block_ratio, split_any,
                      two_digit_year, mk_ts, us_date_ts)

# severity suffix of an MVS message id (IEF403I, DSNL004I, ICH408I, IEC331E…)
_MVS_SUFFIX_LEVEL = {"I": "info", "E": "error", "W": "warn",
                     "A": "error", "D": "info", "S": "fatal"}

# message-id prefix → component (small, high-signal map; default "z/OS")
_MVS_PREFIX_SOURCE = {
    "IEF": "mvs.scheduler", "IEA": "mvs.supervisor", "IEE": "mvs.console",
    "IEC": "mvs.dataset", "IOS": "mvs.ios", "IGD": "mvs.sms",
    "ICH": "racf", "IRR": "racf", "IST": "vtam", "EZZ": "tcpip",
    "EZB": "tcpip", "DSN": "db2", "CSQ": "mq", "IXC": "sysplex",
    "IWM": "wlm", "IAT": "jes3", "ASA": "mvs.smf", "ERB": "rmf",
    "ICK": "ickdsf", "IDC": "idcams", "IKJ": "tso", "BPX": "uss",
}


def _mvs_source(msgid: str) -> str:
    return _MVS_PREFIX_SOURCE.get((msgid or "")[:3], "z/OS")


def _julian_ts(jdate: str, time_s: str) -> Optional[float]:
    """z/OS Julian 'yyddd'/'yyyyddd' + 'HH:MM:SS.th' → epoch ms."""
    m = re.match(r"^(\d{2}|\d{4})(\d{3})$", jdate or "")
    t = re.match(r"^(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?$", time_s or "")
    if not m or not t:
        return None
    yr = int(m.group(1))
    yr = two_digit_year(yr) if yr < 100 else yr
    ddd = int(m.group(2))
    hh, mi, ss, frac = t.groups()
    # hundredths of a second in the .th field
    micro = int((frac or "0").ljust(2, "0")[:2]) * 10000
    try:
        from datetime import datetime, timedelta
        base = datetime(yr, 1, 1) + timedelta(days=ddd - 1)
        return mk_ts(base.year, base.month, base.day, hh, mi, ss, micro)
    except Exception:
        return None


# ── z/OS MVS SYSLOG / hardcopy log ────────────────────────────────────────────
#   N 4000000 SYSA     26202 09:15:32.17 STC04829 00000090  IEF403I CICSPROD …
class ZosSyslogAdapter(LogAdapter):
    name = "zos_syslog"
    language = "zos"
    _RE = re.compile(
        r"^(?P<rt>[NMSWDELX])\s?(?P<route>[0-9A-F]{7})\s+(?P<sys>\S+)\s+"
        r"(?P<jdate>\d{5}|\d{7})\s+(?P<time>\d{2}:\d{2}:\d{2}\.\d{2})\s+"
        r"(?P<job>\S+)\s+(?P<flags>[0-9A-F]{8})\s+(?P<msg>.*)$")
    _MSGID = re.compile(r"^([A-Z$][A-Z0-9$#@]{2,9}?\d[A-Z]?)\b")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"].strip()
        level, source = "info", "z/OS"
        im = self._MSGID.match(msg)
        fields = {"system": g["sys"], "record_type": g["rt"], "job_id": g["job"]}
        if im:
            msgid = im.group(1)
            fields["message_id"] = msgid
            level = _MVS_SUFFIX_LEVEL.get(msgid[-1], "info")
            source = _mvs_source(msgid)
        if "ABEND" in msg:
            level = "error"
        return self._event(level=level, message=msg, source=source,
                           ts_ms=_julian_ts(g["jdate"], g["time"]),
                           trace_id=g["job"], fields=fields, raw=line)


# ── JES2 job log ($HASP messages / JESMSGLG time-stamped lines) ───────────────
#   $HASP395 PAYROLL  ENDED - RC=0000
#   13.03.14 JOB04992  $HASP373 PAYROLL  STARTED - INIT 1 - CLASS A - SYS SYS1
class Jes2HaspAdapter(LogAdapter):
    name = "jes2_hasp"
    language = "zos"
    # optional "hh.mm.ss JOBnnnnn " prefix (dots, not colons), then a message
    # that must be a $HASPnnn or standard MVS message id.
    _RE = re.compile(
        r"^\s?(?:(?P<time>\d{2}\.\d{2}\.\d{2})\s+(?P<job>(?:JOB|STC|TSU)\d{4,7})\s+)?"
        r"(?P<msgid>\$HASP\d{2,4}|[A-Z]{3,4}\d{3,5}[IEWADS])\s+(?P<msg>\S.*)$")
    _BANNER = re.compile(r"^-{3,}\s+[A-Z]+,?\s+\d{1,2}\s+[A-Z]{3}\s+\d{4}\s+-{3,}")

    def _hit(self, s: str) -> bool:
        m = self._RE.match(s)
        # bare MVS ids without the time+jobid prefix belong to mvs_message;
        # this adapter needs either a $HASP id or the JES2 job-log prefix.
        if m and (m.group("msgid").startswith("$HASP") or m.group("time")):
            return True
        return bool(self._BANNER.match(s))

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: block_ratio(ln, lambda x: self._hit(x.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if self._BANNER.match(s):
            return self._event(level="", message=s, source="jes2",
                               fields={"day_separator": True}, raw=line)
        m = self._RE.match(s)
        if not m or not (m.group("msgid").startswith("$HASP") or m.group("time")):
            return None
        g = m.groupdict()
        msgid, msg = g["msgid"], g["msg"].strip()
        level = "info"
        if not msgid.startswith("$HASP"):
            level = _MVS_SUFFIX_LEVEL.get(msgid[-1], "info")
        rc = re.search(r"\bRC=(\d{1,4})\b", msg)
        fields = {"message_id": msgid}
        if g.get("job"):
            fields["job_id"] = g["job"]
        if rc:
            fields["return_code"] = int(rc.group(1))
            if int(rc.group(1)) != 0:
                level = "error"
        if "ABEND" in msg or "RESOURCE SHORTAGE" in msg:
            level = "error"
        ts_ms = None
        if g.get("time"):
            ts_ms = parse_timestamp(g["time"].replace(".", ":"))
        return self._event(level=level, message=f"{msgid} {msg}",
                           source="jes2" if msgid.startswith("$HASP") else _mvs_source(msgid),
                           ts_ms=ts_ms, trace_id=g.get("job"), fields=fields, raw=line)


# ── CICS DFH message streams (console / MSGUSR / CSMT transient data) ─────────
#   07/20/2026 09:15:32 CICSPROD DFHSI1517 Control is being given to CICS.
#   DFHAC2206 13:03:15 CICSPROD Transaction ABCD has failed with abend ASRA. …
class CicsDfhAdapter(LogAdapter):
    name = "cics_dfh"
    language = "zos"
    _DATED = re.compile(
        r"^(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<time>\d{2}:\d{2}:\d{2})\s+"
        r"(?P<applid>\S+)\s+(?P<msgid>DFH[A-Z]{2}\d{4,5}[IWES]?)\s*(?P<msg>.*)$")
    _MSGUSR = re.compile(
        r"^(?P<msgid>DFH[A-Z]{2}\d{4,5}[IWES]?)\s+(?P<time>\d{2}:\d{2}:\d{2})\s+"
        r"(?P<applid>\S+)\s+(?P<msg>.*)$")
    _BARE = re.compile(r"^(?P<msgid>DFH[A-Z]{2}\d{4,5}[IWES]?)\s+(?P<msg>\S.*)$")

    def detect(self, sample_lines):
        def hit(x):
            s = x.strip()
            return bool(self._DATED.match(s) or self._MSGUSR.match(s)
                        or self._BARE.match(s))
        return ratio_detect(sample_lines, lambda ln: block_ratio(ln, hit))

    @staticmethod
    def _level(msgid: str, msg: str) -> str:
        low = msg.lower()
        if "abend" in low or msgid.startswith("DFHAC22"):
            return "error"
        if msgid[-1] in "WES" and not msgid[-1].isdigit():
            return _MVS_SUFFIX_LEVEL.get(msgid[-1], "info")
        if "failed" in low or "error" in low:
            return "error"
        return "info"

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        for rx in (self._DATED, self._MSGUSR, self._BARE):
            m = rx.match(s)
            if not m:
                continue
            g = m.groupdict()
            msgid, msg = g["msgid"], (g.get("msg") or "").strip()
            fields = {"message_id": msgid}
            if g.get("applid"):
                fields["applid"] = g["applid"]
            ab = re.search(r"abend\s+([A-Z0-9]{4})", msg, re.IGNORECASE)
            if ab:
                fields["abend_code"] = ab.group(1)
            ts_ms = None
            if g.get("date"):
                ts_ms = us_date_ts(g["date"], g["time"])
            elif g.get("time"):
                ts_ms = parse_timestamp(g["time"])
            return self._event(level=self._level(msgid, msg), message=msg or msgid,
                               source=f"cics.{g['applid']}" if g.get("applid") else "cics",
                               ts_ms=ts_ms, fields=fields, raw=line)
        return None


# ── RACF ICH408I access-violation console message ─────────────────────────────
#   ICH408I USER(JSMITH  ) GROUP(DEVGRP  ) NAME(JOHN SMITH) … INSUFFICIENT
#   ACCESS AUTHORITY … ACCESS INTENT(UPDATE  )  ACCESS ALLOWED(READ    )
class RacfIch408iAdapter(LogAdapter):
    name = "racf_ich408i"
    language = "zos"
    _HEAD = re.compile(r"^ICH408I\s+USER\(")
    _CAUSES = ("INSUFFICIENT ACCESS AUTHORITY", "PROFILE NOT FOUND",
               "INVALID PASSWORD", "LOGON/JOB INITIATION", "REVOKED",
               "WARNING: INSUFFICIENT", "TOKEN")

    def detect(self, sample_lines):
        # the head line alone is decisive; continuation lines ride along.
        return ratio_detect(
            sample_lines,
            lambda ln: any(self._HEAD.match(x.strip()) for x in split_any(ln)))

    def parse_line(self, line: str) -> Optional[dict]:
        pieces = split_any(line)
        if not pieces or not any(self._HEAD.match(x.strip()) for x in pieces):
            return None
        joined = " ".join(x.strip() for x in pieces)
        fields = {}
        for key in ("USER", "GROUP", "NAME", "CL", "VOL", "ACCESS INTENT",
                    "ACCESS ALLOWED"):
            m = re.search(re.escape(key) + r"\(([^)]*)\)", joined)
            if m:
                fields[key.lower().replace(" ", "_")] = m.group(1).strip()
        cause = next((c for c in self._CAUSES if c in joined), "")
        if cause:
            fields["cause"] = cause
        level = "warn" if cause.startswith("WARNING") else "error"
        who = fields.get("user", "?")
        return self._event(level=level,
                           message=f"RACF ICH408I {cause or 'access event'} user={who}",
                           source="racf", fields=fields, raw=line)


# ── RACF IRRADU00 SMF-unload flat records (SMF 80 rendered) ───────────────────
#   ACCESS   SUCCESS  09:15:32 2026-07-20 SYSA  JSMITH   PAYROLL  …
class RacfIrradu00Adapter(LogAdapter):
    name = "racf_irradu00"
    language = "zos"
    _RE = re.compile(
        r"^(?P<ev>[A-Z][A-Z0-9]{2,11})\s+"
        r"(?P<q>SUCCESS|VIOLATION|INSAUTH|INVPSWD|WARNING|FAILURE)\s+"
        r"(?P<time>\d{2}:\d{2}:\d{2})\s+(?P<date>\d{4}-\d{2}-\d{2})\s+"
        r"(?P<sys>\S+)\s*(?P<rest>.*)$")
    _LVL = {"SUCCESS": "info", "WARNING": "warn"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        level = self._LVL.get(g["q"], "error")
        return self._event(level=level,
                           message=f"RACF {g['ev']} {g['q']}",
                           source="racf.irradu00",
                           ts_ms=parse_timestamp(f"{g['date']} {g['time']}"),
                           fields={"event": g["ev"], "qualifier": g["q"],
                                   "system": g["sys"],
                                   "detail": g["rest"].strip() or None},
                           raw=line)


# ── IBM i job log (DSPJOBLOG / spooled QPJOBLOG rows) ─────────────────────────
#   CPF1124   Information             07/20/26  13:03:14.123456  QWTPIIPP …
#   CPFBC50    Escape                  40   08/19/23  20:00:06,807455  QP0LGS3 …
class IbmiJoblogAdapter(LogAdapter):
    name = "ibmi_joblog"
    language = "ibmi"
    _RE = re.compile(
        r"^(?P<msgid>[A-Z]{2,3}[0-9A-F]{4})\s+"
        r"(?P<type>Information|Diagnostic|Escape|Completion|Command|Notify|"
        r"Request|Inquiry|Reply|Sender copy)\s+"
        r"(?:(?P<sev>\d{2})\s+)?"
        r"(?P<date>\d{2}/\d{2}/\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2}[.,]\d+)\s+"
        r"(?P<rest>\S.*)$")
    _PAGEHDR = re.compile(r"^\s*\d{4}SS1\s+V\d+R\d+M\d+\s+\d{6}\s+Display Job Log")
    _TYPE_LVL = {"Escape": "error", "Diagnostic": "warn", "Notify": "warn",
                 "Information": "info", "Completion": "info", "Command": "debug",
                 "Request": "debug", "Inquiry": "info", "Reply": "info",
                 "Sender copy": "debug"}

    def detect(self, sample_lines):
        def hit(x):
            s = x.strip()
            return bool(self._RE.match(s) or self._PAGEHDR.match(s))
        return ratio_detect(sample_lines, lambda ln: block_ratio(ln, hit))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if self._PAGEHDR.match(s):
            return self._event(level="", message=s, source="ibmi.joblog",
                               fields={"page_header": True}, raw=line)
        m = self._RE.match(s)
        if not m:
            return None
        g = m.groupdict()
        t = g["time"].replace(",", ".")
        tm = re.match(r"(\d{2}:\d{2}:\d{2})\.(\d+)", t)
        micro = 0
        if tm:
            t = tm.group(1)
            micro = int(tm.group(2).ljust(6, "0")[:6])
        dm = re.match(r"(\d{2})/(\d{2})/(\d{2})", g["date"])
        ts_ms = None
        if dm and tm:
            hh, mi, ss = tm.group(1).split(":")
            ts_ms = mk_ts(two_digit_year(dm.group(3)), dm.group(1), dm.group(2),
                          hh, mi, ss, micro)
        fields = {"message_id": g["msgid"], "message_type": g["type"]}
        if g.get("sev"):
            fields["severity"] = int(g["sev"])
        return self._event(level=self._TYPE_LVL.get(g["type"], "info"),
                           message=f"{g['msgid']} {g['type']}: {g['rest'].strip()}",
                           source="ibmi.joblog", ts_ms=ts_ms, fields=fields,
                           raw=line)


# ── IBM i QHST history log (rendered DSPLOG records) ──────────────────────────
#   CPF1124 Job 123456/QUSER/QINTER started on 07/20/26 at 13:03:14 …
class IbmiQhstAdapter(LogAdapter):
    name = "ibmi_qhst"
    language = "ibmi"
    _RE = re.compile(
        r"^(?P<msgid>(?:CPF|CPI|CPC|CPD|CPA|MCH|SQL)[0-9A-F]{4})\s+"
        r"(?!(?:Information|Diagnostic|Escape|Completion|Command|Notify|"
        r"Request|Inquiry|Reply)\b)(?P<msg>\S.*)$")
    _JOB = re.compile(r"\bJob (\d{6}/\S+?/\S+?)\b")
    _WHEN = re.compile(r"\bon (\d{2}/\d{2}/\d{2}) at (\d{2}:\d{2}:\d{2})")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msgid, msg = g["msgid"], g["msg"].strip()
        fields = {"message_id": msgid}
        jm = self._JOB.search(msg)
        if jm:
            fields["job"] = jm.group(1)
        ts_ms = None
        wm = self._WHEN.search(msg)
        if wm:
            ts_ms = us_date_ts(wm.group(1), wm.group(2))
        low = msg.lower()
        level = "error" if ("abnormal" in low or "failed" in low
                            or msgid.startswith("MCH")) else "info"
        return self._event(level=level, message=f"{msgid} {msg}",
                           source="ibmi.qhst", ts_ms=ts_ms,
                           trace_id=fields.get("job"), fields=fields, raw=line)


# ── AIX errpt (summary rows + errpt -a detail blocks) ─────────────────────────
#   A63BEB70   0626155306 P S SYSPROC        SOFTWARE PROGRAM ABNORMALLY TERMINATED
#   LABEL:          CORE_DUMP
class AixErrptAdapter(LogAdapter):
    name = "aix_errpt"
    language = "aix"
    _ROW = re.compile(
        r"^(?P<id>[0-9A-F]{8})\s+(?P<ts>\d{10})\s+(?P<typ>[PTIUO])\s+"
        r"(?P<cls>[HSOU])\s+(?P<res>\S+)\s+(?P<desc>\S.*)$")
    _HDR = re.compile(r"^IDENTIFIER\s+TIMESTAMP\s+T\s+C\s+RESOURCE_NAME")
    # labeled detail fields REQUIRE the colon (so a CSV header whose first
    # cell is "Date/Time" can never fire this); bare section headers are the
    # exact errpt -a section names on a line of their own.
    _DETAIL = re.compile(
        r"^(LABEL|IDENTIFIER|Date/Time|Sequence Number|Machine Id|Node Id|"
        r"Class|Type|WPAR|Resource Name|Resource Class|Resource Type|VPD)"
        r"\s*:\s*(?P<val>.*)$")
    _SECTION = re.compile(r"^(Description|Probable Causes|Failure Causes|"
                          r"Recommended Actions|Detail Data)$")
    _TYPE_LVL = {"P": "error", "T": "warn", "I": "info", "U": "warn", "O": "info"}

    def detect(self, sample_lines):
        def hit(x):
            s = x.strip()
            return bool(self._ROW.match(s) or self._HDR.match(s)
                        or self._DETAIL.match(s) or self._SECTION.match(s))
        return ratio_detect(sample_lines, lambda ln: block_ratio(ln, hit))

    @staticmethod
    def _row_ts(ts: str) -> Optional[float]:
        # mmddhhmmyy
        m = re.match(r"^(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$", ts or "")
        if not m:
            return None
        mo, dy, hh, mi, yy = m.groups()
        return mk_ts(two_digit_year(yy), mo, dy, hh, mi, 0)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._ROW.match(s)
        if m:
            g = m.groupdict()
            return self._event(level=self._TYPE_LVL.get(g["typ"], "info"),
                               message=g["desc"].strip(),
                               source=f"aix.errpt.{g['res']}",
                               ts_ms=self._row_ts(g["ts"]),
                               trace_id=g["id"],
                               fields={"identifier": g["id"], "type": g["typ"],
                                       "class": g["cls"], "resource": g["res"]},
                               raw=line)
        if self._HDR.match(s):
            return self._event(level="", message=s, source="aix.errpt",
                               fields={"header": True}, raw=line)
        pieces = split_any(line)
        fields = {}
        for x in pieces:
            dm = self._DETAIL.match(x.strip())
            if dm:
                fields[dm.group(1).lower().replace(" ", "_").replace("/", "_")] = \
                    dm.group("val").strip()
            elif self._SECTION.match(x.strip()):
                fields.setdefault("sections", []).append(x.strip())
        if not fields:
            return None
        level = "error" if fields.get("type", "").upper().startswith("PERM") else "warn"
        return self._event(level=level,
                           message=fields.get("description")
                           or fields.get("label", "errpt record"),
                           source="aix.errpt", trace_id=fields.get("sequence_number"),
                           fields=fields, raw=line)


# ── Solaris/illumos FMA fault rows (fmdump / fmadm faulty) ────────────────────
#   Dec 28 13:01:27.3919 bf36f0ea-9e47-42b5-fc6f-c0d979c4c8f4 FMD-8000-11
#   Aug 24 17:56:03 7b83c87c-… SUN4V-8001-8H  Minor
class SolarisFmaAdapter(LogAdapter):
    name = "solaris_fma"
    language = "solaris"
    _ROW = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})(?:\.\d+)?\s+"
        r"(?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s+"
        r"(?P<msgid>[A-Z0-9]{2,10}-\d{4}-[0-9A-Z]{1,2})"
        r"(?:\s+(?P<sev>Minor|Major|Critical))?\s*$")
    _HDR = re.compile(r"^TIME\s+(EVENT-ID|UUID)\s+(MSG-ID|SUNW-MSG-ID)")
    _SEV = {"Critical": "fatal", "Major": "error", "Minor": "warn"}

    def detect(self, sample_lines):
        def hit(x):
            s = x.strip()
            return bool(self._ROW.match(s) or self._HDR.match(s))
        return ratio_detect(sample_lines, lambda ln: block_ratio(ln, hit))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if self._HDR.match(s):
            return self._event(level="", message=s, source="solaris.fma",
                               fields={"header": True}, raw=line)
        m = self._ROW.match(s)
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=self._SEV.get(g["sev"] or "", "warn"),
                           message=f"FMA fault {g['msgid']}"
                           + (f" ({g['sev']})" if g["sev"] else ""),
                           source="solaris.fma", ts_ms=parse_timestamp(g["ts"]),
                           trace_id=g["uuid"],
                           fields={"msg_id": g["msgid"], "event_id": g["uuid"],
                                   "severity": g["sev"]}, raw=line)


# ── Solaris/illumos SMF per-service logs (/var/svc/log/*.log) ─────────────────
#   [ May  5 09:32:13 Executing start method ("/lib/svc/method/xntp") ]
class SolarisSmfAdapter(LogAdapter):
    name = "solaris_smf"
    language = "solaris"
    _RE = re.compile(
        r"^\[\s+(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+"
        r"(?P<msg>(?:Executing|Method|Stopping|Enabled|Disabled|Rereading|"
        r"Leaving|Restarting|Timed out|network)\b.*?)\s*\]$")
    _EXIT = re.compile(r'Method "(?P<mth>\w+)" exited with status (?P<st>\d+)')

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"].strip()
        level = "info"
        fields = {}
        em = self._EXIT.search(msg)
        if em:
            fields = {"method": em.group("mth"), "exit_status": int(em.group("st"))}
            level = "error" if int(em.group("st")) != 0 else "info"
        elif "Timed out" in msg:
            level = "error"
        return self._event(level=level, message=msg, source="solaris.smf",
                           ts_ms=parse_timestamp(g["ts"]),
                           fields=fields or None, raw=line)


# ── Bare MVS message-id streams (JESYSMSG, Db2 MSTR, console excerpts) ────────
#   IEF142I PAYROLL STEP1 - STEP WAS EXECUTED - COND CODE 0000
#   DSNL004I  -DB2P DDF START COMPLETE
class MvsMessageAdapter(LogAdapter):
    name = "mvs_message"
    language = "zos"
    _RE = re.compile(r"^(?P<msgid>[A-Z]{3,5}\d{3,5}(?P<suf>[IEWADS]))\s+(?P<msg>\S.*)$")

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda ln: block_ratio(ln, lambda x: bool(self._RE.match(x.strip()))))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msgid, msg = g["msgid"], g["msg"].strip()
        level = _MVS_SUFFIX_LEVEL.get(g["suf"], "info")
        fields = {"message_id": msgid}
        ab = re.search(r"ABEND=?\s*(S[0-9A-F]{3}|U\d{4})", msg)
        if ab:
            fields["abend_code"] = ab.group(1)
            level = "fatal"
        cc = re.search(r"COND CODE (\d{4})", msg)
        if cc:
            fields["cond_code"] = int(cc.group(1))
            if int(cc.group(1)) != 0 and level == "info":
                level = "warn"
        return self._event(level=level, message=f"{msgid} {msg}",
                           source=_mvs_source(msgid), fields=fields, raw=line)


# ── Registration ──────────────────────────────────────────────────────────────
# Order inside this module matters for 1.0-confidence ties:
#   • racf_ich408i and cics_dfh before mvs_message (their ids also fit the
#     generic MVS message-id grammar / their own grammar is stricter);
#   • ibmi_joblog before ibmi_qhst (a joblog row starts with the same CPFnnnn
#     id — the joblog type-word column disambiguates, enforced by qhst's
#     negative lookahead, but keep the stricter one first anyway);
#   • mvs_message dead last in the family so every specialised mainframe
#     adapter (and spectrum_protect's ANS/ANR ids in backup.py, loaded earlier)
#     outranks it on a tie.
register_adapter(ZosSyslogAdapter())
register_adapter(Jes2HaspAdapter())
register_adapter(CicsDfhAdapter())
register_adapter(RacfIch408iAdapter())
register_adapter(RacfIrradu00Adapter())
register_adapter(IbmiJoblogAdapter())
register_adapter(IbmiQhstAdapter())
register_adapter(AixErrptAdapter())
register_adapter(SolarisFmaAdapter(), before="systemd")
register_adapter(SolarisSmfAdapter())
register_adapter(MvsMessageAdapter())


# ═══════════════════════════ BATCH 5 ═════════════════════════════════════════

def _ibmi_ts(s: str) -> Optional[float]:
    """IBM i 'yyyy-mm-dd-hh.mm.ss.ffffff' SQL timestamp → epoch ms."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})-(\d{2})\.(\d{2})\.(\d{2})(?:\.(\d+))?",
                 s or "")
    if not m:
        return None
    yr, mo, dy, hh, mi, ss, frac = m.groups()
    micro = int((frac or "0").ljust(6, "0")[:6])
    return mk_ts(yr, mo, dy, hh, mi, ss, micro)


# ── IBM i QSYSOPR / QSYSMSG operator message queue (rendered) ─────────────────
#   Serious storage condition may exist. Press HELP.  (msgid CPF0907, severity
#   ERROR, queue QSYS/QSYSOPR, from job 541034/QSYS/QSYSARB5, sending pgm
#   QWCATARE, ts 2020-04-30-11.35.29.886549)
class IbmiQsysoprAdapter(LogAdapter):
    name = "ibmi_qsysopr"
    language = "ibmi"
    _META = re.compile(
        r"\(msgid\s+(?P<mid>[A-Z]{2,3}[A-Z0-9]{4,5}),\s*severity\s+(?P<sev>\w+),"
        r"(?P<rest>[^)]*)\)")
    _JOB = re.compile(r"from job\s+(?P<job>\d+/[\w$#@]+/[\w$#@.]+)")
    _QUEUE = re.compile(r"queue\s+(?P<q>[\w$#@]+/[\w$#@]+)")
    _PGM = re.compile(r"(?:sending )?pgm\s+(?P<pgm>[\w$#@]+)")
    _TS = re.compile(r"\bts\s+(?P<ts>\d{4}-\d{2}-\d{2}-\d{2}\.\d{2}\.\d{2}(?:\.\d+)?)")

    def detect(self, sample_lines):
        def ok(ln):
            s = str(ln)
            m = self._META.search(s)
            return bool(m and ("QSYSOPR" in s or "QSYSMSG" in s
                               or self._JOB.search(s)))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._META.search(s)
        if not m:
            return None
        g = m.groupdict()
        sev = g["sev"].upper()
        # severity may be a word (ERROR) or the 00-99 IBM i severity code
        if sev.isdigit():
            n = int(sev)
            level = ("fatal" if n >= 90 else "error" if n >= 40
                     else "warn" if n >= 20 else "info")
        else:
            level = sev
        fields = {"message_id": g["mid"]}
        for rex, key in ((self._JOB, "job"), (self._QUEUE, "queue"),
                         (self._PGM, "program")):
            mm = rex.search(s)
            if mm:
                fields[key] = mm.group(1)
        tm = self._TS.search(s)
        msg = s[:m.start()].strip() or g["mid"]
        return self._event(level=level, message=msg,
                           source=fields.get("queue", "QSYSOPR"),
                           ts_ms=_ibmi_ts(tm.group("ts")) if tm else None,
                           fields=fields, raw=line)


# ── IBM i QAUDJRN security-audit journal (CPYAUDJRNE outfile, rendered) ───────
#   QASYPWJ5 row: entry type PW, violation type P (password), timestamp
#   2026-07-20-13.03.14.123456, job 123456/QUSER/QPADEV0001, userid JSMITH, …
class IbmiQaudjrnAdapter(LogAdapter):
    name = "ibmi_qaudjrn"
    language = "ibmi"
    _ROW = re.compile(r"^(?P<file>QASY[A-Z0-9]{2,6})\s+row:", re.IGNORECASE)
    _TYPE = re.compile(r"entry type\s+(?P<t>[A-Z]{2})\b")
    _TS = re.compile(r"timestamp\s+(?P<ts>\d{4}-\d{2}-\d{2}-\d{2}\.\d{2}\.\d{2}(?:\.\d+)?)")
    # the violation-ish audit types → warn; everything else info
    _WARN_TYPES = {"AF", "PW", "CV", "IM", "VP", "VO", "X1"}

    def detect(self, sample_lines):
        def ok(ln):
            s = str(ln)
            if self._ROW.match(s.strip()):
                return True
            return bool(self._TYPE.search(s) and self._TS.search(s))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        rm = self._ROW.match(s)
        tm = self._TYPE.search(s)
        if not (rm or (tm and self._TS.search(s))):
            return None
        fields = {}
        if rm:
            fields["outfile"] = rm.group("file").upper()
        atype = tm.group("t") if tm else None
        if atype:
            fields["audit_type"] = atype
        for key, rex in (("job", r"job\s+(\d+/[\w$#@]+/[\w$#@.]+)"),
                         ("user", r"userid\s+([\w$#@]+)"),
                         ("device", r"device\s+([\w$#@]+)"),
                         ("violation", r"violation type\s+(\w+)")):
            mm = re.search(rex, s)
            if mm:
                fields[key] = mm.group(1)
        tsm = self._TS.search(s)
        level = "warn" if (atype or "") in self._WARN_TYPES else "info"
        return self._event(level=level,
                           message=s[:200], source="QAUDJRN",
                           category="security" if level == "info" else "warn",
                           ts_ms=_ibmi_ts(tsm.group("ts")) if tsm else None,
                           fields=fields, raw=line)


register_adapter(IbmiQsysoprAdapter())
register_adapter(IbmiQaudjrnAdapter())


# ═════════════════════════════════════════════════════════════════════════════
#  BATCH 7 — z/VM CP console messages + JES3 DLOG
# ═════════════════════════════════════════════════════════════════════════════


# ── IBM z/VM CP/CMS console messages ──────────────────────────────────────────
#   HCPGIR450W CP entered; disabled wait PSW 00020000 80000000 00000000 00961210
class ZvmCpAdapter(LogAdapter):
    name = "zvm_cp"
    language = "any"
    _RE = re.compile(
        r"^(?:(?P<time>\d{2}:\d{2}:\d{2})\s+(?:(?P<userid>[A-Z0-9]{1,8})\s+)?)?"
        r"(?P<msgid>(?:HCP[A-Z]{3}|DMS[A-Z]{3})\d{3,4}(?P<sev>[IWEAST]))\s+"
        r"(?P<msg>.*)$")
    _LVL = {"I": "info", "W": "warn", "E": "error", "A": "warn",
            "S": "fatal", "T": "fatal"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {"message_id": g["msgid"]}
        if g.get("userid"):
            fields["userid"] = g["userid"]
        return self._event(level=self._LVL.get(g["sev"], "info"), message=g["msg"],
                           source="zvm.cp",
                           ts_ms=parse_timestamp(g["time"]) if g.get("time") else None,
                           fields=fields, raw=line)


# ── IBM z/OS JES3 DLOG (sysplex console log) ──────────────────────────────────
#   20 JUL 2026 13:03:14.35 SY1      00000090 8000  IEF403I CICSPROD - STARTED …
class Jes3DlogAdapter(LogAdapter):
    name = "jes3_dlog"
    language = "any"
    _RE = re.compile(
        r"^(?P<dy>\d{1,2}) (?P<mon>[A-Z]{3}) (?P<yr>\d{4}) "
        r"(?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})\.(?P<hun>\d{2})\s+"
        r"(?P<sys>[A-Z0-9]{1,8})\s+(?P<route>[0-9A-F]{8})\s+(?P<desc>[0-9A-F]{4})\s+"
        r"(?P<msg>(?P<msgid>[A-Z]{3,4}\d{3,4}(?P<sev>[IWEAD])?)\b.*)$")
    _LVL = {"I": "info", "W": "warn", "E": "error", "A": "warn", "D": "info"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        mon = _MONTHS.get(g["mon"].title())
        ts_ms = mk_ts(g["yr"], mon, g["dy"], g["hh"], g["mi"], g["ss"],
                      int(g["hun"]) * 10000) if mon else None
        return self._event(level=self._LVL.get(g.get("sev") or "", "info"),
                           message=g["msg"], source=f'jes3.{g["sys"]}',
                           ts_ms=ts_ms,
                           fields={"message_id": g["msgid"], "system": g["sys"],
                                   "routing": g["route"], "descriptor": g["desc"]},
                           raw=line)


for _a in (ZvmCpAdapter(), Jes3DlogAdapter()):
    register_adapter(_a)


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH 8 — z/OS + IBM i + Solaris/AIX/SysV stragglers
# ══════════════════════════════════════════════════════════════════════════════
import re  # noqa: E402
from datetime import datetime  # noqa: E402
from ._common import RxAdapter, vocab_detect  # noqa: E402


# ── JES3 IAT messages ─────────────────────────────────────────────────────────
#   IAT2000 JOB PAYROLL  (JOB04829) SELECTED  SY1  GRP=A
class Jes3IatAdapter(RxAdapter):
    name = "jes3_iat"
    language = "zos"
    default_source = "jes3"
    _RE = re.compile(r"^(?P<msgid>IAT\d{4})\s+(?P<msg>.*)$")

    def _level(self, g, line):
        return "error" if re.search(r"\b(fail|error|abend|purged|cancel)", g.get("msg", ""), re.I) else "info"

    def _fields(self, g, line):
        return {"message_id": g["msgid"]}


# ── IBM NetView for z/OS network log (NETLOGA/NETLOGI) ────────────────────────
#   09:15:32 DSI039I MSG FROM OPER1   : DISPLAY NET,MAJNODES
class NetviewNetlogAdapter(RxAdapter):
    name = "netview_netlog"
    language = "zos"
    default_source = "netview"
    _RE = re.compile(
        r"^(?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})\s+"
        r"(?P<msgid>(?:DSI|DWO|CNM|BNH|IST|DUI|FKX)\d{3,4})(?P<suf>[IEWACD]?)\s+(?P<msg>.*)$")
    _LVL = {"I": "info", "E": "error", "W": "warn", "A": "error", "C": "warn", "D": "info"}

    def _ts(self, g):
        now = datetime.now()
        return mk_ts(now.year, now.month, now.day, g["hh"], g["mi"], g["ss"])

    def _level(self, g, line):
        return self._LVL.get(g.get("suf") or "", "info")

    def _fields(self, g, line):
        return {"message_id": g["msgid"] + (g.get("suf") or "")}


# ── JES2 JESJCL (JCL echo listing) ────────────────────────────────────────────
#            1 //PAYROLL  JOB (ACCT),'J SMITH',CLASS=A,MSGCLASS=X
class Jes2JesjclAdapter(LogAdapter):
    name = "jes2_jesjcl"
    language = "zos"
    _RE = re.compile(r"^\s*(?P<num>\d+)\s+(?P<kind>//\*?|XX\*?|X/|\+\+)(?P<msg>.*)$")

    def detect(self, sample_lines):
        return vocab_detect(
            sample_lines,
            lambda el: block_ratio(el, lambda x: bool(self._RE.match(x))), cap=0.9)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        m = self._RE.match(s)
        if not m:
            for x in split_any(s):
                if self._RE.match(x):
                    m = self._RE.match(x)
                    s = x
                    break
        if not m:
            return None
        g = m.groupdict()
        return self._event(level="info", message=(g["kind"] + g["msg"]).strip(),
                           source="jes2.jcl",
                           fields={"stmt_number": int(g["num"]), "stmt_kind": g["kind"]},
                           raw=line)


# ── Zowe (API ML + app-server ZWE launcher) ───────────────────────────────────
#   2026-07-20 09:15:32.123 <ZWEAGW1:main:12345> ZWESVUSR INFO  ((logger)) ZWEAM000I …
#   2026-07-20 13:03:14.123 <ZWED:16842753> IBMUSER INFO (_zsf.install,index.js:123) …
class ZoweAdapter(RxAdapter):
    name = "zowe"
    language = "zos"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+"
        r"<(?P<comp>ZWE[^>]*)>\s+(?P<user>\S+)\s+"
        r"(?P<level>TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\s+(?P<msg>.*)$")

    def _fields(self, g, line):
        f = {"component": g["comp"], "user": g["user"]}
        mid = re.search(r"\bZWE[A-Z]{1,3}\d{3}[IEW]\b", g["msg"])
        if mid:
            f["message_id"] = mid.group(0)
        return f


# ── IBM i DSPLOG (QHST message text) ──────────────────────────────────────────
#   Job 722506/QZRDSRMOWN/SLMSQMONS started on 25.08.20 at 18:59:04 in subsystem …
class IbmiDsplogAdapter(LogAdapter):
    name = "ibmi_dsplog"
    language = "ibmi"
    _JOB = re.compile(
        r"^Job\s+(?P<job>\d+/[A-Z0-9$#@]+/[A-Z0-9$#@]+)\s+"
        r"(?P<verb>started|completed|ended|entered)\b", re.I)
    _SUBSYS = re.compile(r"^Subsystem\s+\S+\s+(started|ended|active)", re.I)

    def detect(self, sample_lines):
        def hit(el):
            for x in split_any(el):
                x = x.strip()
                if self._JOB.match(x) or self._SUBSYS.match(x):
                    return True
            return False
        return vocab_detect(sample_lines, hit, cap=0.85)

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._JOB.match(s)
        fields = {}
        if m:
            fields["job"] = m.group("job")
        elif not self._SUBSYS.match(s):
            return None
        return self._event(level="info", message=s, source="ibmi.qhst",
                           fields=fields or None, raw=line)


# ── Solaris/illumos BSM audit trail via praudit ───────────────────────────────
#   header,69,2,su,,example-host,2026-07-20 14:33:05.121-07:00,subject,alice,…
class SolarisPrauditAdapter(LogAdapter):
    name = "solaris_praudit"
    language = "solaris"
    _RE = re.compile(r"^header,\d+,\d+,(?P<event>[\w ]+),")

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines, lambda el: bool(self._RE.match(str(el).strip())))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        cols = s.split(",")
        level = "error" if re.search(r"\bfail", s, re.I) else "info"
        ts_ms = None
        for c in cols:
            t = parse_timestamp(c.strip())
            if t:
                ts_ms = t
                break
        return self._event(level=level, message=f'audit: {m.group("event").strip()}',
                           source="solaris.bsm", ts_ms=ts_ms,
                           fields={"event": m.group("event").strip(),
                                   "token_count": len(cols)},
                           category="event", raw=line)


# ── Solaris/illumos svcs(1) listing + svcs -xv explanation ────────────────────
#   online         13:25:03 svc:/milestone/multi-user:default
#   svc:/system/intrd:default (interrupt balancer)
class SolarisSvcsAdapter(LogAdapter):
    name = "solaris_svcs"
    language = "solaris"
    _HDR = re.compile(r"^STATE\s+STIME\s+FMRI\b")
    _ROW = re.compile(
        r"^(?P<state>online|offline|disabled|maintenance|degraded|legacy_run|uninitialized)\s+"
        r"(?P<stime>[\d:]{5,8}|\w{3}_\d{2})\s+(?P<fmri>(?:svc|lrc):/\S+)")
    _EXPLAIN = re.compile(r"^(?P<fmri>svc:/\S+)\s+\((?P<desc>[^)]*)\)\s*$")
    _STATE_LVL = {"maintenance": "error", "degraded": "warn", "offline": "warn"}

    def detect(self, sample_lines):
        def hit(x):
            x = x.strip()
            return bool(self._HDR.match(x) or self._ROW.match(x) or self._EXPLAIN.match(x))
        return ratio_detect(sample_lines, lambda el: block_ratio(el, hit))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        if self._HDR.match(s):
            return self._event(level="", message=s, source="solaris.svcs",
                               fields={"header": True}, raw=line)
        m = self._ROW.match(s)
        if m:
            g = m.groupdict()
            return self._event(level=self._STATE_LVL.get(g["state"], "info"),
                               message=f'{g["fmri"]} [{g["state"]}]',
                               source="solaris.svcs",
                               fields={"state": g["state"], "fmri": g["fmri"]},
                               category="event", raw=line)
        m = self._EXPLAIN.match(s)
        if m:
            g = m.groupdict()
            return self._event(level="warn", message=g["desc"], source=g["fmri"],
                               fields={"fmri": g["fmri"]}, raw=line)
        return None


# ── System V su log (/var/adm/sulog) ──────────────────────────────────────────
#   SU 07/20 14:33 + pts/1 alice-root
class SysvSulogAdapter(RxAdapter):
    name = "sysv_sulog"
    language = "unix"
    default_source = "sulog"
    _RE = re.compile(
        r"^SU (?P<mon>\d{2})/(?P<dy>\d{2}) (?P<hh>\d{2}):(?P<mi>\d{2}) "
        r"(?P<res>[+-]) (?P<tty>\S+) (?P<users>\S+-\S+)$")

    def _ts(self, g):
        now = datetime.now()
        return mk_ts(now.year, g["mon"], g["dy"], g["hh"], g["mi"], 0)

    def _level(self, g, line):
        return "info" if g["res"] == "+" else "error"

    def _fields(self, g, line):
        return {"result": "success" if g["res"] == "+" else "failure",
                "tty": g["tty"], "users": g["users"]}

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            m = self._RE.match(line.strip())
            ev["data"]["message"] = f'su {m.group("users")} on {m.group("tty")}'
            ev["category"] = "event"
        return ev


# ── IBM AIX audit trail via auditpr ───────────────────────────────────────────
#   FILE_Open        alice     OK      Mon Jul 20 14:02:15 2026 ksh
class AixAuditprAdapter(RxAdapter):
    name = "aix_auditpr"
    language = "aix"
    default_source = "aix.audit"
    _RE = re.compile(
        r"^(?P<event>[A-Z][A-Za-z0-9_]+)\s+(?P<user>\S+)\s+(?P<status>OK|FAIL(?:ED)?)\s+"
        r"(?P<ts>[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})"
        r"(?:\s+(?P<cmd>\S+))?\s*$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return "info" if g["status"] == "OK" else "error"

    def _fields(self, g, line):
        f = {"event": g["event"], "user": g["user"], "status": g["status"]}
        if g.get("cmd"):
            f["command"] = g["cmd"]
        return f

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            m = self._RE.match(line.strip())
            ev["data"]["message"] = f'{m.group("event")} by {m.group("user")} [{m.group("status")}]'
            ev["source"] = "aix.audit"
            ev["category"] = "event"
        return ev


for _a in (Jes3IatAdapter(), NetviewNetlogAdapter(), Jes2JesjclAdapter(),
           ZoweAdapter(), IbmiDsplogAdapter(), SolarisPrauditAdapter(),
           SolarisSvcsAdapter(), SysvSulogAdapter(), AixAuditprAdapter()):
    register_adapter(_a)
