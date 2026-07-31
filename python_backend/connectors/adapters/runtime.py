"""
Language-runtime / framework log adapters (BATCH 2)
================================================================================
Framework and runtime console formats distinct from the core language adapters.
(Java Log4j/Logback, Spring Boot, Python logging/loguru, Ruby/Rails, PHP Monolog,
Go zap, .NET Serilog/MEL/NLog, and Android logcat are all already covered.)

Formats: jul_2line, ros, android_crash, android_anr, celery, uwsgi.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp,
                      _MONTHS, _to_ms, ratio_detect, multiline_ratio_detect,
                      split_any, block_ratio)


# ── java.util.logging SimpleFormatter (DEFAULT 2-line record) ────────────────
#   Jul 21, 2026 3:40:39 PM com.myco.project.Service doWork
#   INFO: Testing default format
class JulTwoLineAdapter(LogAdapter):
    name = "jul_2line"
    language = "java"
    _HEAD = re.compile(
        r"^(?P<mon>[A-Z][a-z]{2}) (?P<dy>\d{1,2}), (?P<yr>\d{4}) "
        r"(?P<hh>\d{1,2}):(?P<mm>\d{2}):(?P<ss>\d{2})(?:\.\d+)? (?P<ap>[AP]M) "
        r"(?P<cls>[\w.$]+) (?P<method>[\w$<>]+)$")
    _BODY = re.compile(
        r"^(?P<level>SEVERE|WARNING|INFO|CONFIG|FINE|FINER|FINEST):\s*(?P<msg>.*)$")
    _LVL = {"SEVERE": "error", "WARNING": "warn", "INFO": "info", "CONFIG": "info",
            "FINE": "debug", "FINER": "debug", "FINEST": "trace"}

    def _block_line(self, s: str) -> bool:
        return bool(self._HEAD.match(s) or self._BODY.match(s))

    def detect(self, sample_lines):
        score = multiline_ratio_detect(sample_lines,
                                       lambda x: self._block_line(x.strip()),
                                       threshold=0.5)
        if score <= 0.0:
            return 0.0
        # ANCHOR GATE: the "LEVEL: message" body line is shared vocabulary
        # (uvicorn, Bazel, Python basicConfig, pip all print it). Full
        # confidence only when the DISTINCTIVE two-line date header anchors
        # the sample; a body-only sample is capped below 1.0 so any
        # strict-grammar owner of the shape outranks JUL on those lines.
        has_head = any(self._HEAD.match(x.strip())
                       for ln in sample_lines for x in split_any(ln))
        return score if has_head else min(score, 0.85)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._HEAD.match(s)
        if m:
            g = m.groupdict()
            ts_ms = None
            if g["mon"] in _MONTHS:
                hh = int(g["hh"]) % 12 + (12 if g["ap"] == "PM" else 0)
                try:
                    ts_ms = _to_ms(datetime(int(g["yr"]), _MONTHS[g["mon"]], int(g["dy"]),
                                            hh, int(g["mm"]), int(g["ss"])))
                except ValueError:
                    pass
            return self._event(level="", message=f'{g["cls"]} {g["method"]}',
                               source=g["cls"], ts_ms=ts_ms,
                               fields={"method": g["method"], "record_header": True},
                               raw=line)
        m = self._BODY.match(s)
        if m:
            g = m.groupdict()
            return self._event(level=self._LVL.get(g["level"], "info"), message=g["msg"],
                               source="jul", fields={"jul_level": g["level"]}, raw=line)
        return None


# ── ROS 1 & ROS 2 console (rosout / rcutils) ─────────────────────────────────
#   [ INFO] [1620000000.123456789]: My message                       (ROS 1)
#   [INFO] [1620000000.123456789] [my_node]: My message              (ROS 2)
class RosConsoleAdapter(LogAdapter):
    name = "ros"
    language = "cpp"
    _ROS1 = re.compile(
        r"^\[\s*(?P<level>DEBUG|INFO|WARN|ERROR|FATAL)\]\s+"
        r"\[(?P<ts>\d{9,10}\.\d+)\]:\s*(?P<msg>.*)$")
    _ROS2 = re.compile(
        r"^\[(?P<level>DEBUG|INFO|WARN|ERROR|FATAL)\]\s+"
        r"\[(?P<ts>\d{9,10}\.\d+)\]\s+\[(?P<node>[^\]]+)\]:\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._ROS2.match(ln.strip())
                                            or self._ROS1.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._ROS2.match(s)
        if m:
            g = m.groupdict()
            return self._event(level=g["level"], message=g["msg"], source=g["node"],
                               ts_ms=_epoch_frac(g["ts"]),
                               fields={"node": g["node"], "ros_version": 2}, raw=line)
        m = self._ROS1.match(s)
        if m:
            g = m.groupdict()
            return self._event(level=g["level"], message=g["msg"], source="rosout",
                               ts_ms=_epoch_frac(g["ts"]), fields={"ros_version": 1}, raw=line)
        return None


def _epoch_frac(s: str) -> Optional[float]:
    try:
        return float(s) * 1000.0
    except ValueError:
        return None


# ── Android app crash — FATAL EXCEPTION block ────────────────────────────────
#   E/AndroidRuntime(  915): FATAL EXCEPTION: main
#   E/AndroidRuntime(  915): java.lang.NullPointerException: ...
class AndroidCrashAdapter(LogAdapter):
    name = "android_crash"
    language = "android"
    _RE = re.compile(r"^(?P<lvl>[EWID])/AndroidRuntime\(\s*(?P<pid>\d+)\):\s?(?P<msg>.*)$")
    _LVL = {"E": "error", "W": "warn", "I": "info", "D": "debug"}

    def _block_line(self, s: str) -> bool:
        return bool(self._RE.match(s))

    def detect(self, sample_lines):
        return multiline_ratio_detect(sample_lines, lambda x: self._block_line(x.strip()),
                                      threshold=0.5)

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"]
        # the whole block is a crash → force fatal category so bookmarks fire.
        level = "fatal" if ("FATAL EXCEPTION" in msg or "Exception" in msg
                            or msg.strip().startswith("at ")) else self._LVL.get(g["lvl"], "error")
        return self._event(level=level, message=msg, source="AndroidRuntime",
                           category="error", fields={"pid": int(g["pid"])}, raw=line)


# ── Android ANR — /data/anr/traces.txt thread dump ───────────────────────────
#   ----- pid 12345 at 2026-07-21 15:40:39 -----
#   Cmd line: com.myapp
#   "main" prio=5 tid=1 Native
class AndroidAnrAdapter(LogAdapter):
    name = "android_anr"
    language = "android"
    _HEAD = re.compile(r"^----- pid (?P<pid>\d+) at (?P<ts>[\d\- :]+) -----$")
    _CMD = re.compile(r"^Cmd line:\s*(?P<cmd>.*)$")
    # BATCH-5 gap fix: escaped shipping renders the thread line as
    # \"main\" prio=5 … — tolerate an optional backslash before each quote.
    _THREAD = re.compile(r'^\\?"(?P<tname>[^"\\]+)\\?"\s+(?:daemon\s+)?prio=(?P<prio>\d+)\s+tid=(?P<tid>\d+)\s+(?P<state>\w+)')
    _FRAME = re.compile(r"^\s*(?:at |- |\| |#\d+ |native: )")

    def _block_line(self, s: str) -> bool:
        return bool(self._HEAD.match(s) or self._CMD.match(s)
                    or self._THREAD.match(s) or self._FRAME.match(s))

    def detect(self, sample_lines):
        # require the distinctive ANR header somewhere so we don't grab a bare
        # Java thread dump. BATCH-5 gap fix: split on literal-\n too
        # (split_any/block_ratio), so the escaped one-string sample still routes.
        def any_header(ln):
            return any(self._HEAD.match(x.strip()) for x in split_any(ln))
        base = ratio_detect(
            sample_lines,
            lambda ln: block_ratio(ln, lambda x: self._block_line(x.strip()),
                                   threshold=0.4))
        has_head = ratio_detect(sample_lines, any_header)
        return base if has_head > 0 else min(base, 0.25)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").rstrip()
        subs = split_any(s)
        if len(subs) > 1:                      # whole ANR block → one event
            hm = next((self._HEAD.match(x.strip()) for x in subs
                       if self._HEAD.match(x.strip())), None)
            if hm:
                cm = next((self._CMD.match(x.strip()) for x in subs
                           if self._CMD.match(x.strip())), None)
                cmd = cm.group("cmd") if cm else ""
                threads = sum(1 for x in subs if self._THREAD.match(x.strip()))
                return self._event(level="error",
                                   message=f'ANR pid {hm.group("pid")}'
                                           + (f" ({cmd})" if cmd else ""),
                                   source="android.anr", category="error",
                                   trace_id=hm.group("pid"),
                                   ts_ms=parse_timestamp(hm.group("ts").strip()),
                                   fields={"pid": int(hm.group("pid")),
                                           "cmdline": cmd or None,
                                           "threads": threads}, raw=line)
        st = s.strip()
        m = self._HEAD.match(st)
        if m:
            return self._event(level="error", message=f'ANR pid {m.group("pid")}',
                               source="android.anr", category="error",
                               trace_id=m.group("pid"),
                               ts_ms=parse_timestamp(m.group("ts").strip()),
                               fields={"pid": int(m.group("pid")), "anr_header": True},
                               raw=line)
        m = self._CMD.match(st)
        if m:
            return self._event(level="error", message=st, source="android.anr",
                               category="error", fields={"cmdline": m.group("cmd")}, raw=line)
        m = self._THREAD.match(st)
        if m:
            g = m.groupdict()
            return self._event(level="", message=st, source="android.anr",
                               fields={"thread": g["tname"], "tid": int(g["tid"]),
                                       "state": g["state"]}, raw=line)
        if self._FRAME.match(s):
            return self._event(level="", message=st, source="android.anr",
                               fields={"stack_frame": True}, raw=line)
        return None


# ── Celery worker/beat log ───────────────────────────────────────────────────
#   [2015-01-21 22:18:10,710: INFO/MainProcess] Connected to redis://...
class CeleryAdapter(LogAdapter):
    name = "celery"
    language = "python"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}):\s*"
        r"(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)/(?P<proc>[\w\-]+)\]\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        # BATCH-6 gap fix: multi-line aware — a several-line worker-log excerpt
        # handed as one element must still route here, not to structural.
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"], source=f'celery.{g["proc"]}',
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"process": g["proc"]}, raw=line)


# ── uWSGI request log (standard host format) ─────────────────────────────────
#   [pid: 1234|app: 0|req: 1/1] 1.2.3.4 (user) {40 vars in 1200 bytes} \
#   [Mon Jul 21 15:40:39 2026] GET /uri => generated 1234 bytes in 5 msecs ...
class UwsgiAdapter(LogAdapter):
    name = "uwsgi"
    language = "python"
    _RE = re.compile(
        r"^\[pid:\s*(?P<pid>\d+)\|app:\s*(?P<app>[\d\-]+)\|req:\s*(?P<req>[\d\-]+/[\d\-]+)\]\s+"
        r"(?P<addr>\S+)\s+\((?P<user>[^)]*)\)\s+\{(?P<vars>[^}]*)\}\s+"
        r"\[(?P<ts>[^\]]*)\]\s+(?P<method>\S+)\s+(?P<uri>\S+)\s+=>\s+"
        r"generated (?P<bytes>\d+) bytes in (?P<msecs>\d+) msecs(?:\s+\((?P<proto>[^)]*)\s+(?P<status>\d{3})\))?")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        status = int(g["status"]) if g.get("status") else None
        level = ("error" if status and status >= 500 else "warn"
                 if status and status >= 400 else "info")
        return self._event(level=level,
                           message=f'{g["method"]} {g["uri"]}'
                                   + (f' → {status}' if status else ''),
                           source="uwsgi", ts_ms=parse_timestamp(g["ts"]),
                           fields={"pid": int(g["pid"]), "addr": g["addr"],
                                   "method": g["method"], "uri": g["uri"],
                                   "status": status, "bytes": int(g["bytes"]),
                                   "duration_ms": int(g["msecs"])}, raw=line)


# ═══════════════════════════ BATCH 5 ═════════════════════════════════════════

# ── Android native tombstone ───────────────────────────────────────────────────
#   *** *** *** *** *** *** *** *** *** *** *** *** *** *** *** ***
#   Build fingerprint: 'Android/aosp_angler/…'
#   pid: 17946, tid: 17949, name: crasher  >>> crasher <<<
#   signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0xc
class AndroidTombstoneAdapter(LogAdapter):
    name = "android_tombstone"
    language = "android"
    _STARS = re.compile(r"^(?:\*{3} )+\*{3}\s*$")
    _FPRINT = re.compile(r"^Build fingerprint:\s*'")
    _PID = re.compile(r"^pid:\s*(?P<pid>\d+),\s*tid:\s*(?P<tid>\d+),\s*name:\s*(?P<name>\S+)")
    _SIGNAL = re.compile(r"^signal\s+(?P<num>\d+)\s+\((?P<sig>SIG\w+)\)"
                         r"(?:,\s*code\s+(?P<code>-?\d+)\s+\((?P<codename>\w+)\))?"
                         r"(?:,\s*fault addr\s+(?P<addr>\S+))?")
    _MISC = re.compile(r"^(Revision:|ABI:|Timestamp:|Cmdline:|uid:|Abort message:|"
                       r"Cause:|backtrace:|stack:|memory near|code around|"
                       r"Tombstone written to:)")
    _BTFRAME = re.compile(r"^\s*#\d{2}\s+pc\s+[0-9a-f]+")
    _REG = re.compile(r"^\s+(?:[xrw]\d+|ip|sp|lr|pc|pst|eax|ebx|ecx|edx|rax|rbx|"
                      r"rcx|rdx|rdi|rsi|rbp|rsp|cpsr)\b\s+[0-9a-f]{6,}")

    def _block_line(self, s: str) -> bool:
        st = s.strip()
        return bool(self._STARS.match(st) or self._FPRINT.match(st)
                    or self._PID.match(st) or self._SIGNAL.match(st)
                    or self._MISC.match(st) or self._BTFRAME.match(s)
                    or self._REG.match(s))

    def detect(self, sample_lines):
        # the stars row or fingerprint+signal pair anchors a tombstone.
        def ok(el):
            subs = split_any(el)
            anchored = (any(self._STARS.match(x.strip()) for x in subs)
                        or (any(self._FPRINT.match(x.strip()) for x in subs)
                            and any(self._SIGNAL.match(x.strip()) for x in subs)))
            return anchored and block_ratio(el, lambda x: self._block_line(x),
                                            threshold=0.4)
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if len(subs) > 1:                      # whole tombstone → one fatal event
            pm = next((self._PID.match(x.strip()) for x in subs
                       if self._PID.match(x.strip())), None)
            gm = next((self._SIGNAL.match(x.strip()) for x in subs
                       if self._SIGNAL.match(x.strip())), None)
            if not (pm or gm):
                return None
            fields = {}
            msg = "native crash"
            if pm:
                fields.update({"pid": int(pm.group("pid")),
                               "tid": int(pm.group("tid")),
                               "process": pm.group("name")})
                msg = f'native crash in {pm.group("name")}'
            if gm:
                fields.update({"signal": gm.group("sig"),
                               "fault_addr": gm.group("addr")})
                msg += f' ({gm.group("sig")})'
            frames = [x.strip() for x in subs if self._BTFRAME.match(x)]
            if frames:
                fields["backtrace"] = frames[:32]
            return self._event(level="fatal", message=msg,
                               source="android.tombstone", category="error",
                               fields=fields, raw=line)
        st = s.strip()
        if not self._block_line(s):
            return None
        m = self._SIGNAL.match(st)
        if m:
            return self._event(level="fatal", message=st,
                               source="android.tombstone", category="error",
                               fields={"signal": m.group("sig")}, raw=line)
        m = self._PID.match(st)
        if m:
            return self._event(level="error", message=st,
                               source="android.tombstone", category="error",
                               fields={"pid": int(m.group("pid")),
                                       "process": m.group("name")}, raw=line)
        return self._event(level="", message=st, source="android.tombstone",
                           category="error", raw=line)


# ── Android bugreport (dumpstate container) ────────────────────────────────────
#   ========================================================
#   == dumpstate: 2026-07-20 10:00:03
#   ------ SYSTEM LOG (logcat -v threadtime …) ------
class AndroidBugreportAdapter(LogAdapter):
    name = "android_bugreport"
    language = "android"
    _BAR = re.compile(r"^={8,}\s*$")
    _HEAD = re.compile(r"^== dumpstate: (?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    _META = re.compile(r"^== (?:Build|Bootloader|Radio|Network|Kernel|Uptime):")
    _SECTION = re.compile(r"^------ (?P<name>.+?) ------\s*$")
    _DURATION = re.compile(r"^------ (?P<secs>[\d.]+)s was the duration of '(?P<name>[^']+)'")

    def _block_line(self, s: str) -> bool:
        st = s.strip()
        return bool(self._BAR.match(st) or self._HEAD.match(st)
                    or self._META.match(st) or self._SECTION.match(st)
                    or self._DURATION.match(st))

    def detect(self, sample_lines):
        # the '== dumpstate:' header (or a section+duration pair) anchors it.
        def ok(el):
            subs = split_any(el)
            anchored = (any(self._HEAD.match(x.strip()) for x in subs)
                        or any(self._DURATION.match(x.strip()) for x in subs))
            return anchored and block_ratio(el, lambda x: self._block_line(x),
                                            threshold=0.4)
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if len(subs) > 1:                      # header block → one event
            hm = next((self._HEAD.match(x.strip()) for x in subs
                       if self._HEAD.match(x.strip())), None)
            sections = [self._SECTION.match(x.strip()).group("name")
                        for x in subs if self._SECTION.match(x.strip())
                        and not self._DURATION.match(x.strip())]
            if hm:
                return self._event(level="info",
                                   message=f"bugreport (dumpstate "
                                           f'{hm.group("ts")})',
                                   source="android.bugreport",
                                   ts_ms=parse_timestamp(hm.group("ts")),
                                   fields={"sections": sections[:32] or None},
                                   raw=line)
            if sections:
                return self._event(level="info", message=f"section {sections[0]}",
                                   source="android.bugreport",
                                   fields={"sections": sections[:32]}, raw=line)
            return None
        st = s.strip()
        hm = self._HEAD.match(st)
        if hm:
            return self._event(level="info", message=st,
                               source="android.bugreport",
                               ts_ms=parse_timestamp(hm.group("ts")), raw=line)
        dm = self._DURATION.match(st)
        if dm:
            return self._event(level="info", message=st,
                               source="android.bugreport",
                               fields={"section": dm.group("name"),
                                       "duration_s": float(dm.group("secs"))},
                               raw=line)
        sm = self._SECTION.match(st)
        if sm:
            return self._event(level="info", message=st,
                               source="android.bugreport",
                               fields={"section": sm.group("name")}, raw=line)
        if self._BAR.match(st) or self._META.match(st):
            return self._event(level="", message=st,
                               source="android.bugreport", raw=line)
        return None


# ── Apple .ips crash report (macOS/iOS, sysdiagnose) ───────────────────────────
#   {"app_name":"watchman","timestamp":"…","bug_type":"309","incident_id":"…"}
#   {"uptime":110000,"procName":"watchman","exception":{…},…}
class AppleIpsAdapter(LogAdapter):
    name = "apple_ips"
    language = "any"

    @staticmethod
    def _header(piece: str):
        piece = piece.strip()
        if not piece.startswith("{"):
            return None
        try:
            import json as _json
            rec = _json.loads(piece)
        except Exception:
            return None
        if isinstance(rec, dict) and "bug_type" in rec and (
                "incident_id" in rec or "app_name" in rec):
            return rec
        return None

    def detect(self, sample_lines):
        def ok(el):
            subs = split_any(el)
            return bool(subs) and self._header(subs[0]) is not None
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if not subs:
            return None
        hdr = self._header(subs[0])
        if hdr is None:
            return None
        fields = {"bug_type": hdr.get("bug_type"),
                  "incident_id": hdr.get("incident_id"),
                  "os_version": hdr.get("os_version")}
        proc = hdr.get("app_name") or hdr.get("name") or ""
        body = " ".join(subs[1:])
        em = re.search(r'"type"\s*:\s*"(EXC_\w+)"', body)
        gm = re.search(r'"signal"\s*:\s*"(SIG\w+)"', body)
        pm = re.search(r'"procName"\s*:\s*"([^"]+)"', body)
        if pm and not proc:
            proc = pm.group(1)
        if em:
            fields["exception_type"] = em.group(1)
        if gm:
            fields["signal"] = gm.group(1)
        msg = f"crash report for {proc or '?'}"
        if em or gm:
            msg += f' ({em.group(1) if em else gm.group(1)})'
        return self._event(level="fatal", message=msg, source="apple.ips",
                           category="error",
                           ts_ms=parse_timestamp(str(hdr.get("timestamp", ""))),
                           trace_id=hdr.get("incident_id"),
                           fields=fields, raw=line)


# ── Phoenix (Elixir) request log — Logger with metadata ───────────────────────
#   15:40:39.879 request_id=F1ssWNn62Xas [info] GET /articles
#   (plain Elixir Logger lines WITHOUT metadata stay with the core `elixir`)
class PhoenixAdapter(LogAdapter):
    name = "phoenix"
    language = "elixir"
    _RE = re.compile(
        r"^(?P<ts>\d{2}:\d{2}:\d{2}\.\d{3})\s+"
        r"(?P<meta>(?:[\w@.\-]+=\S+\s+)+)"
        r"\[(?P<level>debug|info|warn|warning|error|notice|critical)\]\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda ln: block_ratio(ln, lambda x: bool(self._RE.match(x.strip()))))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if len(subs) > 1:                      # request block → one event per parse
            # keep the FIRST line's event but merge a 'Sent NNN in Tms' outcome.
            first = next((x for x in subs if self._RE.match(x.strip())), None)
            if first is None:
                return None
            ev = self._parse_one(first.strip(), raw=line)
            for x in subs[1:]:
                m = self._RE.match(x.strip())
                if m and m.group("msg").startswith("Sent "):
                    sm = re.match(r"Sent (\d{3}) in (\d+)(µs|ms|s)", m.group("msg"))
                    if sm and ev:
                        status = int(sm.group(1))
                        ev["data"]["status"] = status
                        ev["data"]["message"] += f" → {status}"
                        if status >= 500:
                            ev["level"], ev["category"] = "ERROR", "error"
                        elif status >= 400:
                            ev["level"], ev["category"] = "WARN", "warn"
            return ev
        return self._parse_one(s.strip(), raw=line)

    def _parse_one(self, st: str, raw: str) -> Optional[dict]:
        m = self._RE.match(st)
        if not m:
            return None
        g = m.groupdict()
        fields = {}
        trace = None
        for k, v in re.findall(r"([\w@.\-]+)=(\S+)", g["meta"]):
            fields[k] = v
            if k == "request_id":
                trace = v
        return self._event(level=g["level"], message=g["msg"], source="phoenix",
                           ts_ms=parse_timestamp(g["ts"]), trace_id=trace,
                           fields=fields or None, raw=raw)


# ═════════════════════════════════════════════════════════════════════════════
# BATCH 6 — application-runtime / app-server formats from the MEDIUM tier
# ═════════════════════════════════════════════════════════════════════════════

# ── aiohttp access log (default access_log_format) ───────────────────────────
#   127.0.0.1 [21/Jul/2026:15:40:39 +0000] "GET /api HTTP/1.1" 200 1234 "-" "curl/8.0"
#   CLF-like, but WITHOUT the '- -' identd/user fields the Apache CLF carries.
class AiohttpAccessAdapter(LogAdapter):
    name = "aiohttp_access"
    language = "python"
    _RE = re.compile(
        r'^(?P<ip>\S+)\s+\[(?P<ts>\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2}\s*[+-]\d{4})\]\s+'
        r'"(?P<req>[^"]*)"\s+(?P<status>\d{3})\s+(?P<size>\S+)'
        r'(?:\s+"(?P<ref>[^"]*)"\s+"(?P<ua>[^"]*)")?')

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
        fields = {"status": st, "request": g["req"], "remote": g["ip"]}
        if g.get("ua"):
            fields["user_agent"] = g["ua"]
        return self._event(level=level, message=f'{g["req"]} -> {st}',
                           source="aiohttp.access", ts_ms=parse_timestamp(g["ts"]),
                           fields=fields, raw=line)


# ── Tornado log (web/general/access with the [L yymmdd HH:MM:SS mod:line] head) ─
#   [I 260721 15:40:39 web:2271] 200 GET /api (127.0.0.1) 12.34ms
class TornadoAdapter(LogAdapter):
    name = "tornado"
    language = "python"
    _RE = re.compile(
        r"^\[(?P<lvl>[DIWE])\s+(?P<yy>\d{2})(?P<mo>\d{2})(?P<dy>\d{2})\s+"
        r"(?P<time>\d{2}:\d{2}:\d{2})\s+(?P<mod>[\w.]+):(?P<lineno>\d+)\]\s?(?P<msg>.*)$")
    _ACC = re.compile(r"^(?P<status>\d{3})\s+(?P<method>[A-Z]+)\s+(?P<path>\S+)\s+"
                      r"\((?P<ip>[^)]*)\)\s+(?P<ms>[\d.]+)ms\s*$")
    _LVL = {"D": "debug", "I": "info", "W": "warn", "E": "error"}

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        from ._common import two_digit_year, mk_ts
        hh, mi, ss = g["time"].split(":")
        ts_ms = mk_ts(two_digit_year(g["yy"]), g["mo"], g["dy"], hh, mi, ss)
        level = self._LVL.get(g["lvl"], "info")
        fields = {"module": f'{g["mod"]}:{g["lineno"]}'}
        am = self._ACC.match(g["msg"])
        if am:
            st = int(am.group("status"))
            level = "error" if st >= 500 else "warn" if st >= 400 else level
            fields.update({"status": st, "method": am.group("method"),
                           "path": am.group("path"),
                           "duration_ms": float(am.group("ms"))})
        return self._event(level=level, message=g["msg"], source=f'tornado.{g["mod"]}',
                           ts_ms=ts_ms, fields=fields, raw=line)


# ── Puma server stdout ────────────────────────────────────────────────────────
#   [12345] Puma starting in cluster mode...
#   [12345] * Listening on http://0.0.0.0:3000
#   [12345] - Worker 0 (PID: 12346) booted in 0.01s, phase: 0
class PumaAdapter(LogAdapter):
    name = "puma"
    language = "ruby"
    _LINE = re.compile(r"^(?:\[(?P<mpid>\d+)\]\s+)?(?P<mark>[*!\-])?\s*(?P<msg>\S.*)$")
    _PUMAISH = re.compile(
        r"Puma (?:starting|version)|codename:|Listening on |Worker \d+ \(PID|"
        r"Min threads:|Max threads:|booted in [\d.]+s, phase:|"
        r"Restarting\.\.\.|Gracefully sh|Environment: ")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            if not subs:
                return False
            shaped = sum(1 for x in subs
                         if re.match(r"^\[\d+\]\s+", x) or x.lstrip()[:2] in ("* ", "- ", "! "))
            branded = sum(1 for x in subs if self._PUMAISH.search(x))
            return branded >= 1 and (shaped + branded) / len(subs) >= 0.5
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if "\n" in s:
            for x in s.splitlines():
                if x.strip() and self._PUMAISH.search(x):
                    ev = self.parse_line(x)
                    if ev:
                        ev["raw"] = line
                        return ev
            s = s.splitlines()[0]
        if not s.strip():
            return None
        m = self._LINE.match(s.strip())
        if not m:
            return None
        g = m.groupdict()
        level = "warn" if g["mark"] == "!" else "info"
        if re.search(r"\bERROR\b|SIGTERM|terminating|Error r", g["msg"]):
            level = "error"
        fields = {}
        if g["mpid"]:
            fields["pid"] = int(g["mpid"])
        wm = re.search(r"Worker (\d+) \(PID: (\d+)\)", g["msg"])
        if wm:
            fields["worker"] = int(wm.group(1))
            fields["worker_pid"] = int(wm.group(2))
        return self._event(level=level, message=g["msg"], source="puma",
                           fields=fields or None, raw=line)


# ── Sidekiq job log ───────────────────────────────────────────────────────────
#   2020-11-12T05:43:12.449Z pid=1 tid=goj0ke5wl class=HardWorker jid=9f8e7d INFO: start
class SidekiqAdapter(LogAdapter):
    name = "sidekiq"
    language = "ruby"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+"
        r"pid=(?P<pid>\d+)\s+tid=(?P<tid>\w+)\s+"
        r"(?P<kv>(?:[\w.]+=\S+\s+)*)"
        r"(?P<level>DEBUG|INFO|WARN|ERROR|FATAL):\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {"pid": int(g["pid"]), "tid": g["tid"]}
        trace = None
        for pair in (g["kv"] or "").split():
            k, _, v = pair.partition("=")
            fields[k] = v
            if k == "jid":
                trace = v
        return self._event(level=g["level"], message=g["msg"],
                           source=f'sidekiq.{fields.get("class", "worker")}',
                           ts_ms=parse_timestamp(g["ts"]), trace_id=trace,
                           fields=fields, raw=line)


# ── Rails Lograge (single-line logfmt request summary) ───────────────────────
#   method=GET path=/articles format=html controller=ArticlesController
#   action=index status=200 duration=15.05 view=10.50 db=0.50
class LogrageAdapter(LogAdapter):
    name = "lograge"
    language = "ruby"
    _PAIR = re.compile(r'([\w.]+)=("[^"]*"|\S+)')

    def detect(self, sample_lines):
        def hit(ln):
            pairs = dict(self._PAIR.findall(ln.strip()))
            # heroku-router logfmt also carries method/path/status — its
            # at=/dyno= keys are the tell; those lines belong to heroku_router.
            if "at" in pairs or "dyno" in pairs:
                return False
            return ("method" in pairs and "path" in pairs
                    and ("controller" in pairs or "action" in pairs
                         or "duration" in pairs))
        return multiline_ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        pairs = dict(self._PAIR.findall(s))
        if not ("method" in pairs and "path" in pairs):
            return None
        fields = {k: (v.strip('"')) for k, v in pairs.items()}
        st = int(fields["status"]) if str(fields.get("status", "")).isdigit() else None
        level = ("error" if st and st >= 500 else
                 "warn" if st and st >= 400 else "info")
        msg = f'{fields.get("method")} {fields.get("path")}' \
              + (f' -> {st}' if st is not None else "")
        return self._event(level=level, message=msg, source="rails.lograge",
                           fields=fields, raw=line)


# ── NestJS default ConsoleLogger ─────────────────────────────────────────────
#   [Nest] 12345  - 07/21/2026, 3:40:39 PM     LOG [NestFactory] Starting…
class NestJsAdapter(LogAdapter):
    name = "nestjs"
    language = "node"
    _RE = re.compile(
        r"^\[Nest\]\s+(?P<pid>\d+)\s+-\s+(?P<date>\d{2}/\d{2}/\d{4}),\s+"
        r"(?P<time>\d{1,2}:\d{2}:\d{2}\s*[AP]M)\s+"
        r"(?P<level>LOG|ERROR|WARN|DEBUG|VERBOSE|FATAL)\s+"
        r"(?:\[(?P<ctx>[^\]]+)\]\s*)?(?P<msg>.*)$")
    _LVL = {"LOG": "info", "VERBOSE": "trace"}

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        from ._common import us_date_ts
        return self._event(level=self._LVL.get(g["level"], g["level"]),
                           message=g["msg"],
                           source=f'nestjs.{g["ctx"]}' if g["ctx"] else "nestjs",
                           ts_ms=us_date_ts(g["date"], g["time"]),
                           fields={"pid": int(g["pid"]), "context": g["ctx"]},
                           raw=line)


# ── Ruby exception backtrace ─────────────────────────────────────────────────
#   /app/lib/work.rb:7:in `do_work': boom (RuntimeError)
#       from /app/main.rb:3:in `<main>'
class RubyBacktraceAdapter(LogAdapter):
    name = "ruby_backtrace"
    language = "ruby"
    _HEAD = re.compile(
        r"^(?P<file>\S+?\.rb):(?P<line>\d+):in\s+[`'](?P<meth>[^']*)'?:\s+"
        r"(?P<msg>.*)\((?P<exc>[A-Z]\w*(?:::\w+)*)\)\s*$")
    _FRAME = re.compile(r"^\s+from\s+\S+?\.rb:\d+:in\s+[`']")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            if not subs or not self._HEAD.match(subs[0].strip()):
                return False
            rest = subs[1:]
            return not rest or all(self._FRAME.match(x) or "from " in x for x in rest)
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if not subs:
            return None
        m = self._HEAD.match(subs[0].strip())
        if m:
            g = m.groupdict()
            return self._event(level="error", message=f'{g["msg"].strip()}({g["exc"]})',
                               source=g["exc"],
                               fields={"file": g["file"], "line": int(g["line"]),
                                       "method": g["meth"],
                                       "frames": len(subs) - 1}, raw=line)
        if self._FRAME.match(s):
            return self._event(level="", message=s.strip(), source="ruby_backtrace",
                               fields={"stack_frame": True}, raw=line)
        return None


# ── ASP.NET Core Systemd console formatter ───────────────────────────────────
#   <6>Microsoft.Hosting.Lifetime[0] Now listening on: http://localhost:5000
class AspNetSystemdAdapter(LogAdapter):
    name = "aspnet_systemd"
    language = "dotnet"
    _RE = re.compile(
        r"^<(?P<pri>\d)>(?P<cat>[A-Za-z][\w.]*(?:\.[\w]+)+)\[(?P<eid>\d+)\]\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        level = _SYSLOG_LVL.get(int(g["pri"]), "")
        return self._event(level=level, message=g["msg"], source=g["cat"],
                           fields={"event_id": int(g["eid"])}, raw=line)


# ── Google glog, LONG-date variant (4-digit year) ────────────────────────────
#   I20230125 00:33:50.224948   360 EventListener.h:133] message
#   Used by Apache Doris/StarRocks, NebulaGraph, Typesense, braft… The stock
#   Immdd form (I0720 12:00:00…) is already owned by the core `klog` adapter.
class Glog4Adapter(LogAdapter):
    name = "glog4"
    language = "cpp"
    _RE = re.compile(
        r"^(?P<lvl>[IWEF])(?P<yr>\d{4})(?P<mon>\d{2})(?P<day>\d{2})\s+"
        r"(?P<time>\d{2}:\d{2}:\d{2}\.\d+)\s+(?P<tid>\d+)\s+"
        # Doris/StarRocks omit the closing ']' after file:line — it is optional
        r"(?P<file>[\w./\-]+):(?P<lineno>\d+)\]?\s+(?P<msg>.*)$")
    _LVL = {"I": "info", "W": "warn", "E": "error", "F": "fatal"}

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        from ._common import mk_ts
        hh, mi, ss = g["time"].split(":")
        sec, _, frac = ss.partition(".")
        ts_ms = mk_ts(g["yr"], g["mon"], g["day"], hh, mi, sec,
                      int((frac or "0").ljust(6, "0")[:6]))
        return self._event(level=self._LVL.get(g["lvl"], g["lvl"]), message=g["msg"],
                           source=f'{g["file"]}:{g["lineno"]}', ts_ms=ts_ms,
                           fields={"thread": g["tid"]}, raw=line)


# ── spdlog default pattern ────────────────────────────────────────────────────
#   [2024-03-23 00:40:18.691] [warning] message
#   [2024-03-23 00:40:18.691] [my_logger] [info] message
class SpdlogAdapter(LogAdapter):
    name = "spdlog"
    language = "cpp"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[.,]\d{3,6})\]\s+"
        r"(?:\[(?P<logger>[\w.\-]+)\]\s+)?"
        r"\[(?P<level>trace|debug|info|warning|error|critical)\]\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"],
                           source=g["logger"] or "spdlog",
                           ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── zerolog ConsoleWriter (Go) — cloudflared, many Go daemons ────────────────
#   2023-01-15T12:00:00Z INF Connection registered connIndex=0 location=SJC
class ZerologAdapter(LogAdapter):
    name = "zerolog"
    language = "go"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s+"
        r"(?P<lvl>TRC|DBG|INF|WRN|ERR|FTL|PNC)\s+(?P<msg>.*)$")
    _LVL = {"TRC": "trace", "DBG": "debug", "INF": "info", "WRN": "warn",
            "ERR": "error", "FTL": "fatal", "PNC": "fatal"}
    _PAIR = re.compile(r'([\w.]+)=("[^"]*"|\S+)')

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"]
        # trailing k=v pairs → structured fields, message = leading prose
        fields = {}
        fm = re.search(r"\s([\w.]+=)", msg)
        if fm:
            prose, kvs = msg[:fm.start()], msg[fm.start():]
            pairs = self._PAIR.findall(kvs)
            if pairs:
                fields = {k: v.strip('"') for k, v in pairs}
                msg = prose.strip() or msg
        return self._event(level=self._LVL.get(g["lvl"], g["lvl"]), message=msg,
                           source="zerolog", ts_ms=parse_timestamp(g["ts"]),
                           fields=fields or None, raw=line)


_SYSLOG_LVL = {0: "fatal", 1: "fatal", 2: "fatal", 3: "error",
               4: "warn", 5: "info", 6: "info", 7: "debug"}


# ── Registration ─────────────────────────────────────────────────────────────
for _a in (JulTwoLineAdapter(), RosConsoleAdapter(), AndroidCrashAdapter(),
           AndroidAnrAdapter(), CeleryAdapter(), UwsgiAdapter(),
           # batch 5
           AndroidTombstoneAdapter(), AndroidBugreportAdapter(),
           AppleIpsAdapter(), PhoenixAdapter()):
    register_adapter(_a)

# batch 6 — aiohttp's CLF-minus-identd shape shares a silhouette with the
# Apache/nginx CLF adapter → register before `access_log` so it wins the tie
# on its own samples (access_log does NOT match them, but keep the intent
# explicit). lograge/sidekiq/zerolog carry k=v payloads → before `logfmt` so a
# 1.0 tie can never fall to the generic k=v parser.
register_adapter(AiohttpAccessAdapter(), before="access_log")
for _a in (SidekiqAdapter(), LogrageAdapter(), ZerologAdapter()):
    register_adapter(_a, before="logfmt")
for _a in (TornadoAdapter(), PumaAdapter(), NestJsAdapter(),
           RubyBacktraceAdapter(), AspNetSystemdAdapter(), Glog4Adapter(),
           SpdlogAdapter()):
    register_adapter(_a)
