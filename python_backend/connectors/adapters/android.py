"""
Android platform log adapters (BATCH 6)
================================================================================
The Android formats BEYOND plain logcat (which the core `logcat` adapter owns,
including the batch-6 year/usec/zone/uid modifier variants): the `-v long`
2-line rendering, the ART GC message grammar, the `am instrument -r` raw test
protocol, and DropBoxManager entry headers. Crash formats (tombstone, ANR,
bugreport) shipped in batch 5 (runtime.py).

Formats: logcat_long, art_gc, android_instrumentation, android_dropbox.
"""
from __future__ import annotations

import re
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp,
                      ratio_detect, split_any, mk_ts)
from datetime import datetime


# ── logcat -v long (bracket header line + message line) ──────────────────────
#   [ 04-01 16:00:00.278   172:0xb0 D/dalvikvm ]
#   GC_CONCURRENT freed 3840K, 19% free 18438K/22727K, paused 6ms+6ms
class LogcatLongAdapter(LogAdapter):
    name = "logcat_long"
    language = "android"
    _HEAD = re.compile(
        r"^\[\s*(?P<ts>\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+"
        r"(?P<pid>\d+):\s*(?P<tid>0x[0-9a-f]+|\d+)\s+"
        r"(?P<lvl>[VDIWEFS])/(?P<tag>\S+)\s*\]\s*$")
    _LVL = {"V": "trace", "D": "debug", "I": "info",
            "W": "warn", "E": "error", "F": "fatal", "S": "fatal"}

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            return bool(subs) and any(self._HEAD.match(x.strip()) for x in subs)
        return ratio_detect(sample_lines, hit)

    @staticmethod
    def _ts(mmdd_hms: str) -> Optional[float]:
        m = re.match(r"(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})\.(\d{3})", mmdd_hms)
        if not m:
            return None
        mo, dy, hh, mi, ss, ms = m.groups()
        return mk_ts(datetime.now().year, mo, dy, hh, mi, ss, int(ms) * 1000)

    def parse_line(self, line: str) -> Optional[dict]:
        subs = split_any(line)
        if not subs:
            return None
        m = self._HEAD.match(subs[0].strip())
        if not m:
            # bare header on its own (streamed line-by-line)
            m = self._HEAD.match(line.rstrip("\r\n").strip())
            if not m:
                return None
            subs = [line.strip()]
        g = m.groupdict()
        msg = " ".join(x.strip() for x in subs[1:]) if len(subs) > 1 else ""
        tid = g["tid"]
        return self._event(level=self._LVL.get(g["lvl"], g["lvl"]),
                           message=msg or f'{g["tag"]} (header)',
                           source=g["tag"], ts_ms=self._ts(g["ts"]),
                           fields={"pid": int(g["pid"]),
                                   "tid": int(tid, 16) if tid.startswith("0x") else int(tid)},
                           raw=line)


# ── ART / Dalvik GC lines (message level, tag stripped) ───────────────────────
#   Explicit concurrent copying GC freed 104710(3MB) AllocSpace objects, …
#   GC_CONCURRENT freed 3840K, 19% free 18438K/22727K, paused 6ms+6ms
class ArtGcAdapter(LogAdapter):
    name = "art_gc"
    language = "android"
    _ART = re.compile(
        r"^(?:Explicit |Background |)(?:concurrent copying|concurrent mark sweep|"
        r"partial mark sweep|sticky concurrent mark sweep|CollectorTransition|"
        r"HomogeneousSpaceCompact|young concurrent copying).*GC freed \d+\(.*paused .*total ")
    _DALVIK = re.compile(r"^GC_(?:CONCURRENT|FOR_ALLOC|EXPLICIT|EXTERNAL_ALLOC) freed ")

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda ln: bool(self._ART.match(str(ln).strip())
                            or self._DALVIK.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if not (self._ART.match(s) or self._DALVIK.match(s)):
            return None
        fields = {}
        fm = re.search(r"GC freed (\d+)\((\d+[KMG]?B)\)", s) \
            or re.search(r"freed (\d+)K", s)
        if fm:
            fields["freed"] = fm.group(1)
        pm = re.search(r"paused ([^,]+?)(?:,| total)", s)
        if pm:
            fields["paused"] = pm.group(1).strip()
        tm = re.search(r"total ([\d.]+m?s)", s)
        if tm:
            fields["total"] = tm.group(1)
        return self._event(level="info", message=s, source="art.gc",
                           fields=fields or None, raw=line)


# ── am instrument -r / AndroidJUnitRunner raw protocol ───────────────────────
#   INSTRUMENTATION_STATUS: test=useAppContext / INSTRUMENTATION_STATUS_CODE: 1
class AndroidInstrumentationAdapter(LogAdapter):
    name = "android_instrumentation"
    language = "android"
    _RE = re.compile(
        r"^INSTRUMENTATION_(?P<kind>STATUS_CODE|STATUS|RESULT|CODE|FAILED|ABORTED)"
        r"(?::\s?(?P<rest>.*))?$")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            if not subs:
                return False
            n = sum(1 for x in subs if self._RE.match(x.strip()))
            return n >= 1 and n / len(subs) >= 0.5
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if len(subs) > 1:
            fields = {}
            level = "info"
            for x in subs:
                m = self._RE.match(x.strip())
                if not m:
                    continue
                kind, rest = m.group("kind"), m.group("rest") or ""
                if kind in ("FAILED", "ABORTED"):
                    level = "error"
                elif kind == "STATUS_CODE" and rest.strip() in ("-2", "-1"):
                    level = "error"
                if "=" in rest:
                    k, _, v = rest.partition("=")
                    fields[k.strip()] = v.strip()
                elif rest:
                    fields[kind.lower()] = rest.strip()
            if not fields and level == "info":
                return None
            msg = fields.get("test") or fields.get("class") or "instrumentation status"
            return self._event(level=level, message=str(msg),
                               source="android.instrumentation",
                               fields=fields or None, raw=line)
        m = self._RE.match(s.strip())
        if not m:
            return None
        kind, rest = m.group("kind"), m.group("rest") or ""
        level = "error" if kind in ("FAILED", "ABORTED") or \
            (kind == "STATUS_CODE" and rest.strip() in ("-2", "-1")) else "info"
        fields = {}
        if "=" in rest:
            k, _, v = rest.partition("=")
            fields[k.strip()] = v.strip()
        return self._event(level=level, message=rest or kind,
                           source="android.instrumentation",
                           fields={"kind": kind, **fields}, raw=line)


# ── DropBoxManager entry headers (dumpsys dropbox --print) ───────────────────
#   2026-07-20 10:15:00 system_app_anr (compressed text, 13566 bytes)
class AndroidDropboxAdapter(LogAdapter):
    name = "android_dropbox"
    language = "android"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
        r"(?P<tag>[a-z][a-z0-9_]+)\s+"
        r"\((?:compressed )?(?:text|data),\s*(?P<bytes>\d+) bytes\)$")
    _HDR = re.compile(r"^Drop box contents: \d+ entr")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            return bool(subs) and any(
                self._RE.match(x.strip()) or self._HDR.match(x.strip()) for x in subs)
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if "\n" in s:
            s = next((x.strip() for x in s.splitlines()
                      if self._RE.match(x.strip())), s.splitlines()[0].strip())
        if self._HDR.match(s):
            return self._event(level="info", message=s, source="android.dropbox",
                               raw=line)
        m = self._RE.match(s)
        if not m:
            return None
        g = m.groupdict()
        tag = g["tag"]
        level = ("error" if any(w in tag for w in ("crash", "anr", "wtf", "watchdog"))
                 else "warn" if "strictmode" in tag else "info")
        return self._event(level=level, message=f"dropbox entry: {tag}",
                           source=f"android.dropbox.{tag}",
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"tag": tag, "bytes": int(g["bytes"])}, raw=line)


# ── Registration ─────────────────────────────────────────────────────────────
for _a in (LogcatLongAdapter(), ArtGcAdapter(), AndroidInstrumentationAdapter(),
           AndroidDropboxAdapter()):
    register_adapter(_a)


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH 8 — dumpsys / batterystats-checkin / older logcat column formats
# ══════════════════════════════════════════════════════════════════════════════
import re as _re8  # noqa: E402
from ._common import (RxAdapter, vocab_detect as _vd8, block_ratio as _br8,  # noqa: E402
                      split_any as _sa8, ratio_detect as _rd8)

_LOGCAT_LVL = {"V": "trace", "D": "debug", "I": "info",
               "W": "warn", "E": "error", "F": "fatal", "S": "fatal"}


# ── adb shell dumpsys (all services) / bugreport DUMPSYS section ──────────────
#   DUMP OF SERVICE package:  /  ** MEMINFO in pid 18227 [com.…messaging] **
class AndroidDumpsysAdapter(LogAdapter):
    name = "android_dumpsys"
    language = "android"
    _SERVICE = _re8.compile(r"^DUMP OF SERVICE (?P<svc>[\w.$/]+):")
    _MEMINFO = _re8.compile(r"^\*\* MEMINFO in pid (?P<pid>\d+) \[(?P<pkg>[^\]]+)\] \*\*")
    _ACTIVITY = _re8.compile(r"^(?:Current Activity Manager state:|ACTIVITY MANAGER)")

    def detect(self, sample_lines):
        def hit(el):
            subs = [x.strip() for x in _sa8(el)]
            return any(self._SERVICE.match(x) or self._MEMINFO.match(x)
                       or self._ACTIVITY.match(x) for x in subs)
        return _rd8(sample_lines, hit)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        for x in _sa8(s):
            x = x.strip()
            m = self._SERVICE.match(x)
            if m:
                return self._event(level="", message=f'dumpsys service {m.group("svc")}',
                                   source=f'android.dumpsys.{m.group("svc")}',
                                   fields={"service": m.group("svc")},
                                   category="event", raw=line)
            m = self._MEMINFO.match(x)
            if m:
                return self._event(level="", message=f'meminfo pid {m.group("pid")} [{m.group("pkg")}]',
                                   source="android.dumpsys.meminfo",
                                   fields={"pid": int(m.group("pid")), "package": m.group("pkg")},
                                   category="event", raw=line)
        return self._event(level="", message=s.strip(), source="android.dumpsys", raw=line)


# ── dumpsys batterystats --checkin (machine CSV) ──────────────────────────────
#   9,0,i,vers,11,116,K,L
class AndroidBatterystatsAdapter(LogAdapter):
    name = "android_batterystats_csv"
    language = "android"
    _RE = _re8.compile(
        r"^9,\d+,[luci4]?,(?P<key>vers|uid|apk|pr|wl|cpu|pwi|bt|dc|st|gn|br|sgt|"
        r"sst|dub|m|bcm|kwl|jb|sy|nt|gwfl|gmfl|pws|pwm|wr)\b")

    def detect(self, sample_lines):
        return _rd8(sample_lines, lambda el: _br8(el, lambda x: bool(self._RE.match(x.strip()))))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        cols = s.split(",")
        return self._event(level="", message=f'batterystats {m.group("key")}',
                           source="android.batterystats",
                           fields={"section": m.group("key"), "columns": len(cols)},
                           category="event", raw=line)


# ── logcat -v monotonic (leading uptime seconds) ──────────────────────────────
#        6.494  1810  1820 D OpenGLRenderer: Enabling debug mode 0
class LogcatMonotonicAdapter(RxAdapter):
    name = "logcat_monotonic"
    language = "android"
    _RE = _re8.compile(
        r"^\s*(?P<up>\d+\.\d{3})\s+(?P<pid>\d+)\s+(?P<tid>\d+)\s+"
        r"(?P<lvl>[VDIWEFS])\s+(?P<tag>[^:]+):\s?(?P<msg>.*)$")

    def _level(self, g, line):
        return _LOGCAT_LVL.get(g["lvl"], "info")

    def _ts(self, g):
        return None

    def _fields(self, g, line):
        return {"uptime_s": float(g["up"]), "pid": int(g["pid"]), "tid": int(g["tid"])}

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            ev["source"] = self._RE.match(line.strip()).group("tag").strip()
        return ev


# ── logcat -v thread (older 'L(pid:tid) msg' form) ────────────────────────────
#   I(  585:0x24d) Starting activity: Intent { … }
class LogcatThreadAdapter(RxAdapter):
    name = "logcat_thread"
    language = "android"
    _RE = _re8.compile(
        r"^(?P<lvl>[VDIWEFS])\(\s*(?P<pid>\d+):(?P<tid>0x[0-9a-fA-F]+|\s*\d+)\)\s+(?P<msg>.*)$")

    def _level(self, g, line):
        return _LOGCAT_LVL.get(g["lvl"], "info")

    def _fields(self, g, line):
        return {"pid": int(g["pid"]), "tid": g["tid"].strip()}


# ── logcat -v process (older 'L(pid) msg  (tag)' form) ────────────────────────
#   I(  585) Starting activity: Intent { … }  (ActivityManager)
class LogcatProcessAdapter(RxAdapter):
    name = "logcat_process"
    language = "android"
    _RE = _re8.compile(
        r"^(?P<lvl>[VDIWEFS])\(\s*(?P<pid>\d+)\)\s+(?P<msg>.*)\((?P<tag>[A-Za-z0-9_.$-]+)\)\s*$")

    def _level(self, g, line):
        return _LOGCAT_LVL.get(g["lvl"], "info")

    def _fields(self, g, line):
        return {"pid": int(g["pid"])}

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            ev["source"] = self._RE.match(line.strip()).group("tag")
        return ev


# logcat_thread before logcat_process: the thread form has a ':' inside the
# paren that the process regex forbids, but keep the tie explicit.
register_adapter(AndroidDumpsysAdapter())
register_adapter(AndroidBatterystatsAdapter())
register_adapter(LogcatMonotonicAdapter())
register_adapter(LogcatThreadAdapter())
register_adapter(LogcatProcessAdapter())
