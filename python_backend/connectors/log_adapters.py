"""
Universal log-adapter framework
================================================================================
AgentVision's promise is: *connect to ANY program in ANY language and already
have the log ready* — every line normalized into the one unified JSON event
schema the rest of the system (and Claude) already reads.

This module is that normalization layer. It is pure standard library (no third-
party deps) so it runs identically on Windows and macOS, and it never raises
into a caller — a line it cannot parse degrades to a raw-text event.

────────────────────────────────────────────────────────────────────────────────
The unified event schema (one dict per log line)
────────────────────────────────────────────────────────────────────────────────
Every adapter turns a raw line into EXACTLY this shape, which is a superset of
the JSONL records the bridge already emits (bridge_server._read_action_jsonl,
cli._emit_jsonl), so existing fingerprinting / bookmarks / outlier / trace code
keeps working unchanged on adapter output:

    {
      "ts":        "2026-07-20T12:00:00.123Z" | "",   # ISO-Z UTC, "" if unknown
      "ts_ms":     1.7e12 | None,                       # epoch ms, None if unknown
      "category":  "log|debug|warn|error|event|...",    # level-derived bucket
      "level":     "INFO|DEBUG|WARN|ERROR|...",         # canonical upper-case
      "source":    "logger.name | subsystem | adapter", # who emitted it
      "trace_id":  "abc123" | None,                     # correlation id if found
      "frame_seq": None,                                # filled by the bridge
      "data":      { "message": "...", "adapter": "...", ...structured fields },
      "raw":       "<the original line>",
    }

category is derived from level so the bridge's failure detector
(category == "error") fires for ERROR/FATAL/CRITICAL lines from ANY language.

────────────────────────────────────────────────────────────────────────────────
How detection works
────────────────────────────────────────────────────────────────────────────────
Each adapter implements detect(sample_lines) -> float in [0,1] (confidence it is
the right parser) and parse_line(line) -> event|None. `detect_adapter()` samples
the head of a log file, scores every registered adapter, and returns the winner.
`RAW` always matches with a tiny floor score, so detection never fails.

To add a new format: subclass LogAdapter, implement detect()+parse_line(), and
append an instance to `REGISTRY` (or call register_adapter()). That's the whole
extension contract — see ROADMAP at the bottom for the long tail still to add.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Optional

# ── Canonical level vocabulary ────────────────────────────────────────────────
# Map the dialect of every ecosystem onto one small set so Claude never has to
# know that Java says SEVERE, Go/zap says "fatal", Rust says "warn", .NET says
# "WRN", syslog says numeric severities, etc.
_LEVEL_CANON = {
    "trace": "TRACE", "trc": "TRACE", "verbose": "TRACE", "vrb": "TRACE", "v": "TRACE",
    "debug": "DEBUG", "dbg": "DEBUG", "dbug": "DEBUG", "d": "DEBUG", "fine": "DEBUG",
    "finer": "DEBUG", "finest": "DEBUG",
    "info": "INFO", "inf": "INFO", "information": "INFO", "notice": "INFO", "i": "INFO",
    "warn": "WARN", "warning": "WARN", "wrn": "WARN", "w": "WARN",
    "error": "ERROR", "err": "ERROR", "eror": "ERROR", "severe": "ERROR", "e": "ERROR",
    "fatal": "FATAL", "critical": "FATAL", "crit": "FATAL", "ftl": "FATAL",
    "panic": "FATAL", "emerg": "FATAL", "alert": "FATAL", "f": "FATAL", "c": "FATAL",
}

# level → coarse category. category is what the bridge's failure detection keys
# off, so anything ERROR/FATAL must map to "error".
_LEVEL_CATEGORY = {
    "TRACE": "debug", "DEBUG": "debug", "INFO": "log",
    "WARN": "warn", "ERROR": "error", "FATAL": "error",
}

# Reverse map: a native record may carry only a category (e.g. "error") and no
# explicit level. Derive a level from the category so level filters still work.
_CATEGORY_LEVEL = {
    "debug": "DEBUG", "log": "INFO", "info": "INFO", "warn": "WARN",
    "warning": "WARN", "error": "ERROR", "fatal": "FATAL", "crash": "FATAL",
}

# syslog numeric severity (RFC5424) → canonical level
_SYSLOG_SEVERITY = {
    0: "FATAL", 1: "FATAL", 2: "FATAL", 3: "ERROR", 4: "WARN",
    5: "INFO", 6: "INFO", 7: "DEBUG",
}


def canonical_level(token: str) -> str:
    """Map any ecosystem's level word to the canonical upper-case vocabulary.
    Unknown tokens pass through upper-cased (never lost)."""
    if not token:
        return ""
    return _LEVEL_CANON.get(token.strip().lower(), token.strip().upper())


def level_to_category(level: str) -> str:
    return _LEVEL_CATEGORY.get((level or "").upper(), "log")


# ── Content-based severity escalation ─────────────────────────────────────────
# Plenty of real logs encode a FAILURE in the message body while carrying no
# level token at all. Found live: SharpEmu logs
#     [METAL] guestgpu.present src=0x5D80000 ok=False
# where "[METAL]" is the SOURCE channel, not a level — so every one of 180
# consecutive GPU present failures normalized to INFO. av_diagnose consequently
# reported "health 100, no strong failure signals — program looks healthy" while
# the program was failing to present a single frame. A diagnostic tool that
# confidently reports health through 180 failures is worse than no tool.
#
# So when an adapter found NO real severity, the message content gets a second
# look. Escalation is to WARN and never further: this is an inference, and
# minting an ERROR (with a fingerprint and an incident freeze) off a guess would
# be dishonest. WARN is enough to reach diagnose, the digest and push mode.
#
# The matchers are deliberately VALUE-AWARE, because the obvious keyword approach
# is wrong in the most common case of all: "errors=0", "failed=0" and "ok=True"
# are GOOD news, and a naive /error|fail/ would flag every healthy heartbeat.
CONTENT_SEVERITY = os.environ.get("AGENTVISION_CONTENT_SEVERITY", "1") not in (
    "0", "", "false")

#: (compiled pattern, short reason). Ordered: first match wins.
_FAILURE_CONTENT_PATTERNS = [
    (re.compile(r"\bok\s*[=:]\s*(?:false|0|no)\b", re.I), "ok=false"),
    (re.compile(r"\b(?:success|succeeded|successful)\s*[=:]\s*(?:false|0|no)\b", re.I),
     "success=false"),
    (re.compile(r"\b(?:failed|failure|fail)\s*[=:]\s*(?:true|1|yes)\b", re.I),
     "failed=true"),
    (re.compile(r"\berror\s*[=:]\s*(?:true|yes)\b", re.I), "error=true"),
    # Non-zero counts only — "errors=0" must stay INFO.
    (re.compile(r"\b(?:errors?|failures?|failed|dropped|lost)"
                r"\s*[=:]\s*(?!0\b)\d+\b", re.I), "non-zero failure count"),
    (re.compile(r"\bstatus\s*[=:]\s*[45]\d\d\b", re.I), "http 4xx/5xx"),
    (re.compile(r"\b(?:rc|exit|exitcode|exit_code|returncode)"
                r"\s*[=:]\s*(?!0\b)-?\d+\b", re.I), "non-zero exit code"),
    (re.compile(r"\b(?:failed|unable)\s+to\b", re.I), "failed to ..."),
    # "timed out" (past tense — it HAPPENED), not bare "timeout": a line like
    # "timeout configured as 30s" is configuration, not a failure, and matching
    # the bare noun flagged it. Tested in test_content_severity.
    (re.compile(r"\btimed\s+out\b", re.I), "timed out"),
    (re.compile(r"\btimeout\s+(?:exceeded|expired|after|waiting|reached)\b", re.I),
     "timeout exceeded"),
    (re.compile(r"\brefused\b|\bunreachable\b", re.I), "connection refused"),
]

#: Levels that mean "the adapter did not actually find a severity", so the
#: content is allowed to speak. An explicit WARN/ERROR/FATAL is never downgraded
#: or second-guessed — the log's own labelling always wins.
_WEAK_LEVELS = {"", "INFO", "DEBUG", "TRACE", "NOTICE", "VERBOSE"}


def content_severity(text: str) -> tuple[str, str]:
    """Infer a severity from message CONTENT. Returns (level, reason) or ("","").

    Only ever returns WARN. See the note above for why this is value-aware.
    """
    if not CONTENT_SEVERITY or not text:
        return "", ""
    text = str(text)
    for pat, why in _FAILURE_CONTENT_PATTERNS:
        if pat.search(text):
            return "WARN", why
    return "", ""


def escalate_by_content(ev: dict) -> dict:
    """Raise a weakly-levelled event to WARN when its text reads as a failure.

    Mutates and returns `ev`. Records `level_escalated_from` + `escalation_reason`
    so the inference is auditable rather than silently rewriting the log.
    """
    if not CONTENT_SEVERITY or not isinstance(ev, dict):
        return ev
    # str() coercion, not `or ""`: a non-string level (seen with level=5 from a
    # numeric-severity source like syslog) made .upper() raise on the capture
    # path, where nothing may ever throw.
    cur = str(ev.get("level") or "").upper()
    if cur not in _WEAK_LEVELS:
        return ev
    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    text = str(data.get("message") or ev.get("raw") or "")
    lvl, why = content_severity(text)
    if not lvl:
        return ev
    ev["level"] = lvl
    ev["category"] = level_to_category(lvl)
    ev["level_escalated_from"] = cur or "(none)"
    ev["escalation_reason"] = f"content looks like a failure ({why})"
    return ev


# ── Timestamp parsing ──────────────────────────────────────────────────────────
# Programs stamp time in a dozen shapes. We parse the common ones to epoch ms so
# every line lands on AgentVision's single UTC-ms timeline (shared/clock.py).
# When a line has no year (syslog RFC3164, some game logs) we assume the current
# year. When a line has no timezone we assume UTC — documented, not silent.

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def _to_ms(dt: datetime) -> float:
    """datetime → epoch ms. A NAIVE datetime (no tzinfo) is interpreted as
    LOCAL machine time — because AgentVision correlates a program's logs with
    screenshots stamped on the SAME machine, and a program's bare wall-clock
    timestamp (no Z/offset) is its local time. A tz-aware datetime uses its own
    zone. This keeps the text log (local HH:MM:SS) and the JSONL event stream
    (epoch ms) on ONE timeline even when the machine isn't on UTC."""
    return dt.timestamp() * 1000.0   # naive → local tz; aware → its own tz


def parse_timestamp(text: str) -> Optional[float]:
    """Best-effort parse of a leading/embedded timestamp → epoch ms, or None.
    Handles ISO-8601 (with Z/offset/space separator, comma or dot subseconds),
    Go's slash date (2006/01/02 15:04:05), syslog RFC3164 (Mon DD HH:MM:SS),
    and bare wall-clock (HH:MM:SS[.mmm], assumes today UTC)."""
    if not text:
        return None
    s = text.strip()

    # ISO-8601: 2026-07-20T12:00:00.123Z / 2026-07-20 12:00:00,123 / +00:00
    m = re.match(
        r"(\d{4})-(\d{2})-(\d{2})[ T]"
        r"(\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,9}))?"
        r"\s*(Z|[+-]\d{2}:?\d{2})?", s)
    if m:
        yr, mo, dy, hh, mm, ss, frac, tz = m.groups()
        micro = int((frac or "0").ljust(6, "0")[:6])
        tzinfo = None   # no Z/offset → naive → LOCAL (same-machine correlation)
        if tz == "Z":
            tzinfo = timezone.utc
        elif tz:
            sign = 1 if tz[0] == "+" else -1
            tzc = tz[1:].replace(":", "")
            tzinfo = timezone(sign * _offset_delta(tzc))
        try:
            return _to_ms(datetime(int(yr), int(mo), int(dy), int(hh),
                                   int(mm), int(ss), micro, tzinfo))
        except ValueError:
            return None

    # Go standard log: 2006/01/02 15:04:05(.000000) — no tz → local
    m = re.match(r"(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?", s)
    if m:
        yr, mo, dy, hh, mm, ss, frac = m.groups()
        micro = int((frac or "0").ljust(6, "0")[:6])
        try:
            return _to_ms(datetime(int(yr), int(mo), int(dy), int(hh),
                                   int(mm), int(ss), micro))
        except ValueError:
            return None

    # Apache error-log ctime WITH year: "Mon Jul 20 12:00:00.123456 2026"
    m = re.match(r"[A-Z][a-z]{2}\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+"
                 r"(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?\s+(\d{4})", s)
    if m and m.group(1) in _MONTHS:
        mon, dy, hh, mm, ss, frac, yr = m.groups()
        micro = int((frac or "0").ljust(6, "0")[:6])
        try:                                   # Apache ctime carries no tz → local
            return _to_ms(datetime(int(yr), _MONTHS[mon], int(dy), int(hh),
                                   int(mm), int(ss), micro))
        except ValueError:
            return None

    # Common Log Format (nginx/Apache access) "20/Jul/2026:12:00:00 +0000" AND
    # the Django/werkzeug dev-server variant "20/Jul/2026 12:00:00" (space sep,
    # no tz). Separator between year and hour may be ':' or ' '.
    m = re.match(r"(\d{2})/([A-Z][a-z]{2})/(\d{4})[ :](\d{2}):(\d{2}):(\d{2})"
                 r"\s*([+-]\d{4})?", s)
    if m and m.group(2) in _MONTHS:
        dy, mon, yr, hh, mm, ss, tz = m.groups()
        tzinfo = timezone.utc
        if tz:
            sign = 1 if tz[0] == "+" else -1
            tzinfo = timezone(sign * _offset_delta(tz[1:]))
        try:
            return _to_ms(datetime(int(yr), _MONTHS[mon], int(dy), int(hh),
                                   int(mm), int(ss), 0, tzinfo))
        except ValueError:
            return None

    # syslog RFC3164 / systemd short: "Mon DD HH:MM:SS" (no year/tz → local, this yr)
    m = re.match(r"([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})", s)
    if m and m.group(1) in _MONTHS:
        mon, dy, hh, mm, ss = m.groups()
        yr = datetime.now().year
        try:
            return _to_ms(datetime(yr, _MONTHS[mon], int(dy), int(hh),
                                   int(mm), int(ss), 0))
        except ValueError:
            return None

    # Bare wall-clock: HH:MM:SS(.mmm) — assume today, LOCAL (serilog/elixir console)
    m = re.match(r"(\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,9}))?$", s) or \
        re.match(r"(\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,9}))?\b", s)
    if m:
        hh, mm, ss, frac = m.groups()
        micro = int((frac or "0").ljust(6, "0")[:6])
        now = datetime.now()
        try:
            return _to_ms(datetime(now.year, now.month, now.day, int(hh),
                                   int(mm), int(ss), micro))
        except ValueError:
            return None
    return None


def _offset_delta(hhmm: str):
    from datetime import timedelta
    hhmm = hhmm.ljust(4, "0")
    return timedelta(hours=int(hhmm[:2]), minutes=int(hhmm[2:4]))


_TRACE_ID_RE = re.compile(
    r"\b(?:trace[_\-]?id|traceId|traceparent|request[_\-]?id|correlation[_\-]?id)"
    r"[=:\s\"]+([A-Za-z0-9\-]{6,})", re.IGNORECASE)


def _find_trace_id(line: str) -> Optional[str]:
    m = _TRACE_ID_RE.search(line)
    return m.group(1) if m else None


# ── Base adapter ────────────────────────────────────────────────────────────────

class LogAdapter:
    """Base class. Subclass and implement detect() + parse_line().

    Contract:
      • name             short stable id ("jsonl", "log4j", "syslog5424", …)
      • language         the ecosystem it targets (for auto-detect on attach)
      • detect(lines)    confidence in [0,1] this adapter fits the sample
      • parse_line(line) a normalized event dict, or None to drop the line
    """
    name: str = "base"
    language: str = "any"

    def detect(self, sample_lines: list[str]) -> float:
        return 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        return None

    # helper every subclass uses to emit a schema-correct event
    def _event(self, *, level: str = "", message: str = "",
               source: str = "", ts_ms: Optional[float] = None,
               category: str = "", trace_id: Optional[str] = None,
               fields: Optional[dict] = None, raw: str = "") -> dict:
        lvl = canonical_level(level) if level else ""
        cat = category or (level_to_category(lvl) if lvl else "log")
        data = {"message": message, "adapter": self.name}
        if fields:
            data.update(fields)
        ts = ""
        if ts_ms is not None:
            ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc) \
                .isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return {
            "ts": ts,
            "ts_ms": ts_ms,
            "category": cat,
            "level": lvl,
            "source": source or self.name,
            "trace_id": trace_id or _find_trace_id(raw or message),
            "frame_seq": None,
            "data": data,
            "raw": raw,
        }


# ── 1. JSON-lines (pino, winston-json, bunyan, zap-json, structlog, AV itself) ──

class JsonLinesAdapter(LogAdapter):
    name = "jsonl"
    language = "any"

    # fields different ecosystems use for the same concept. This ONE adapter
    # therefore covers EVERY JSON-based logger: pino/winston/bunyan/roarr (Node),
    # structlog/loguru-json (Python), zap/slog-json/logrus-json (Go),
    # logback-json/log4j2-json/GELF (Java), Serilog/NLog-json (.NET), MongoDB,
    # journald `-o json`, Docker json-file, Caddy, Envoy access-json, OTLP-json,
    # and AgentVision's own native records.
    _TS_KEYS = ("ts_ms", "ts", "time", "timestamp", "@timestamp", "asctime", "t",
                "__REALTIME_TIMESTAMP", "Timestamp", "start_time")
    _LVL_KEYS = ("level", "lvl", "severity", "levelname", "loglevel", "levelName",
                 "s", "PRIORITY", "Level", "syslog_level")
    _MSG_KEYS = ("message", "msg", "text", "event", "@message", "MESSAGE",
                 "short_message", "Message", "RenderedMessage")
    _SRC_KEYS = ("source", "logger", "name", "module", "logger_name", "caller",
                 "category", "SourceContext", "c", "ctx", "SYSLOG_IDENTIFIER",
                 "_SYSTEMD_UNIT", "host", "channel")
    # numeric level scales: pino/bunyan (10-60) vs syslog/GELF/journald (0-7)
    _PINO_LEVELS = {10: "trace", 20: "debug", 30: "info", 40: "warn", 50: "error", 60: "fatal"}
    _SYSLOG_LEVELS = {0: "fatal", 1: "fatal", 2: "fatal", 3: "error",
                      4: "warn", 5: "info", 6: "info", 7: "debug"}

    @staticmethod
    def _coerce_ts(v):
        """Coerce a variety of JSON timestamp shapes → epoch ms, or None.
        Handles epoch s/ms/us numbers, ISO strings, and Mongo {"$date": …}."""
        if isinstance(v, dict):                      # Mongo extended JSON
            v = v.get("$date", v.get("$numberLong"))
        if isinstance(v, bool):
            return None
        # Numeric-string epochs (journald __REALTIME_TIMESTAMP is a µs string).
        if isinstance(v, str) and v.strip().lstrip("-").isdigit():
            try:
                v = float(v)
            except ValueError:
                return None
        if isinstance(v, (int, float)):
            v = float(v)
            if v > 1e17:    return v / 1e6            # nanoseconds
            if v > 1e14:    return v / 1e3            # microseconds (__REALTIME_TIMESTAMP)
            if v > 1e11:    return v                  # milliseconds
            if v > 1e8:     return v * 1000.0         # seconds
            return None                               # too small to be an epoch
        if isinstance(v, str):
            return parse_timestamp(v)
        return None

    def detect(self, sample_lines: list[str]) -> float:
        ok = 0
        seen = 0
        for ln in sample_lines:
            ln = ln.strip()
            if not ln:
                continue
            seen += 1
            if ln[0] in "{[":
                try:
                    json.loads(ln)
                    ok += 1
                except Exception:
                    pass
        return (ok / seen) if seen else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        line = line.strip()
        if not line:
            return None
        try:
            rec = json.loads(line)
        except Exception:
            return None
        if not isinstance(rec, dict):
            return None

        # Already an AgentVision-native record? Pass through, only backfilling.
        if "category" in rec and ("ts_ms" in rec or "ts" in rec) and "data" in rec:
            lvl = canonical_level(str((rec.get("data") or {}).get("level", "")))
            # If no explicit level, derive one from the record's category so
            # level filters (e.g. "WARN+") still catch native error/warn events.
            if not lvl:
                lvl = _CATEGORY_LEVEL.get(str(rec.get("category", "")).lower(), "")
            rec["level"] = rec.get("level") or lvl
            # Guarantee BOTH time views on every unified event: SharpEmu's
            # AgentVisionEventSink emits only ts_ms — backfill the ISO ts (and
            # vice-versa) so downstream (and the AI) can always rely on `ts`.
            if not rec.get("ts") and isinstance(rec.get("ts_ms"), (int, float)):
                rec["ts"] = datetime.fromtimestamp(
                    rec["ts_ms"] / 1000.0, tz=timezone.utc
                ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            if rec.get("ts_ms") is None and rec.get("ts"):
                rec["ts_ms"] = parse_timestamp(str(rec["ts"]))
            rec.setdefault("trace_id", None)
            rec.setdefault("frame_seq", None)
            rec.setdefault("raw", line)
            return rec

        ts_ms = None
        for k in self._TS_KEYS:
            if k in rec:
                ts_ms = self._coerce_ts(rec[k])
                if ts_ms is not None:
                    break

        level = ""
        for k in self._LVL_KEYS:
            if k in rec and rec[k] not in (None, ""):
                lv = rec[k]
                if isinstance(lv, bool):
                    level = str(lv)
                elif isinstance(lv, (int, float)):
                    n = int(lv)
                    # pino/bunyan use 10-60; syslog/GELF/journald use 0-7.
                    level = (self._PINO_LEVELS.get(n) if n >= 10
                             else self._SYSLOG_LEVELS.get(n)) or str(lv)
                elif isinstance(lv, str) and lv.isdigit():
                    n = int(lv)                       # journald PRIORITY is a str
                    level = (self._PINO_LEVELS.get(n) if n >= 10
                             else self._SYSLOG_LEVELS.get(n)) or lv
                else:
                    level = str(lv)
                break

        message = ""
        for k in self._MSG_KEYS:
            if rec.get(k):
                message = str(rec[k])
                break

        source = ""
        for k in self._SRC_KEYS:
            if rec.get(k):
                source = str(rec[k])
                break

        # Everything that wasn't a recognized envelope key becomes structured data.
        consumed = set(self._TS_KEYS + self._LVL_KEYS + self._MSG_KEYS + self._SRC_KEYS)
        fields = {k: v for k, v in rec.items() if k not in consumed}
        trace = rec.get("trace_id") or rec.get("traceId") or rec.get("traceparent")
        return self._event(level=level, message=message, source=source,
                           ts_ms=ts_ms, trace_id=trace, fields=fields, raw=line)


# ── 2. logfmt (Heroku, Go/logrus, many services) ───────────────────────────────
#     level=info ts=2026-07-20T12:00:00Z msg="thing happened" key=val

class LogfmtAdapter(LogAdapter):
    name = "logfmt"
    language = "any"
    _PAIR = re.compile(r'([\w.\-]+)=("(?:[^"\\]|\\.)*"|\S*)')

    def _pairs(self, line: str) -> dict:
        out: dict = {}
        for k, v in self._PAIR.findall(line):
            if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
                v = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            out[k] = v
        return out

    def detect(self, sample_lines: list[str]) -> float:
        hits = 0
        seen = 0
        for ln in sample_lines:
            ln = ln.strip()
            if not ln or ln[0] in "{[":
                continue
            seen += 1
            pairs = self._pairs(ln)
            # logfmt lines are DOMINATED by k=v pairs and usually carry level/msg
            if len(pairs) >= 2 and (
                    "level" in pairs or "lvl" in pairs or "msg" in pairs
                    or "time" in pairs or "ts" in pairs):
                hits += 1
        return (hits / seen) if seen else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        line = line.strip()
        if not line:
            return None
        p = self._pairs(line)
        if not p:
            return None
        ts_ms = None
        for k in ("ts", "time", "timestamp", "t"):
            if k in p:
                ts_ms = parse_timestamp(p[k])
                if ts_ms is not None:
                    break
        level = p.get("level") or p.get("lvl") or p.get("severity") or ""
        message = p.get("msg") or p.get("message") or ""
        source = p.get("source") or p.get("logger") or p.get("component") or ""
        consumed = {"ts", "time", "timestamp", "t", "level", "lvl", "severity",
                    "msg", "message", "source", "logger", "component"}
        fields = {k: v for k, v in p.items() if k not in consumed}
        return self._event(level=level, message=message, source=source,
                           ts_ms=ts_ms, trace_id=p.get("trace_id"),
                           fields=fields, raw=line)


# ── 3. Python logging (default + common formats) ────────────────────────────────
#     2026-07-20 12:00:00,123 INFO module: message
#     2026-07-20 12:00:00,123 - name - LEVEL - message
#     [2026-07-20 12:00:00] LEVEL: message

class PythonLoggingAdapter(LogAdapter):
    name = "python_logging"
    language = "python"
    _RE = re.compile(
        r"^\[?(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\]?"
        r"\s*[-\s]?\s*"
        r"(?:(?P<name1>[\w.\-]+)\s+-\s+)?"
        r"(?P<level>TRACE|DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)"
        r"\s*[:\-]?\s*(?P<msg>.*)$")
    # logging.basicConfig() with NO format → "LEVEL:name:message" (no spaces
    # around the colons — that tightness is what keeps Bazel's "ERROR: msg",
    # JUL's "INFO: msg" and uvicorn's padded "INFO:     msg" out).
    _BASIC = re.compile(
        r"^(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL):"
        r"(?P<name1>[\w.\-]*):(?P<msg>.*)$")

    def detect(self, sample_lines: list[str]) -> float:
        hits = seen = 0
        for ln in sample_lines:
            ln = ln.strip()
            if not ln or ln[0] in "{[" and ln[0] != "[":
                continue
            seen += 1
            if self._RE.match(ln) or self._BASIC.match(ln):
                hits += 1
        return (hits / seen) if seen else 0.0

    # a leading "logger.name: rest" inside the message (the very common
    # "%(asctime)s %(levelname)s %(name)s: %(message)s" shape)
    _LEADING_LOGGER = re.compile(r"^([\w][\w.\-]*):\s+(.*)$")

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        m = self._RE.match(s)
        if not m:
            m = self._BASIC.match(s.strip())
            if not m:
                return None
            g = m.groupdict()
            return self._event(level=g["level"], message=g.get("msg") or "",
                               source=g.get("name1") or "", ts_ms=None, raw=line)
        g = m.groupdict()
        source = g.get("name1") or ""
        msg = g.get("msg") or ""
        if not source:
            lm = self._LEADING_LOGGER.match(msg)
            if lm:
                source, msg = lm.group(1), lm.group(2)
        return self._event(level=g.get("level") or "", message=msg,
                           source=source, ts_ms=parse_timestamp(g.get("ts") or ""),
                           raw=line)


# ── 4. Java: Log4j / Logback / SLF4J ────────────────────────────────────────────
#     2026-07-20 12:00:00.123 [main] INFO  com.acme.Foo - message
#     12:00:00.123 [pool-1] ERROR c.a.Svc - boom

class Log4jAdapter(LogAdapter):
    name = "log4j"
    language = "java"
    # the optional trailing [+-]HHMM offset covers log4j2 %d{ISO8601_OFFSET}
    # (e.g. Apache Pulsar: "2022-01-25T23:47:26,294+0000 [main] INFO  ...").
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:[+-]\d{4})?|\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"
        r"\s+\[(?P<thread>[^\]]+)\]\s+"
        r"(?P<level>TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|SEVERE)\s+"
        r"(?P<logger>[\w.$]+)\s*-\s*(?P<msg>.*)$")

    def detect(self, sample_lines: list[str]) -> float:
        hits = seen = 0
        for ln in sample_lines:
            ln = ln.strip()
            if not ln:
                continue
            seen += 1
            if self._RE.match(ln):
                hits += 1
        return (hits / seen) if seen else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"],
                           source=g["logger"], ts_ms=parse_timestamp(g["ts"]),
                           fields={"thread": g["thread"]}, raw=line)


# ── 5. .NET: Serilog + Microsoft.Extensions.Logging ─────────────────────────────
#     [12:00:00 INF] message                              (serilog console)
#     2026-07-20 12:00:00.123 +00:00 [INF] message        (serilog file)
#     info: Category.Name[0]                              (M.E.Logging, msg next line)

class DotNetAdapter(LogAdapter):
    name = "dotnet"
    language = "dotnet"
    _SERILOG = re.compile(
        r"^\[?(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\s*[+-]\d{2}:\d{2})?|\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+"
        # \b after the level so a 3-letter code (INF) can't match a prefix of a
        # longer word (INFO) — that would steal Spring Boot's "ts INFO pid ---" lines.
        r"(?P<level>VRB|DBG|INF|WRN|ERR|FTL|TRACE|DEBUG|INFORMATION|WARNING|ERROR|FATAL|VERBOSE)\b\]?"
        r"\s*(?P<msg>.*)$", re.IGNORECASE)
    _MEL = re.compile(
        r"^(?P<level>trce|dbug|info|warn|fail|crit)"
        r":\s+(?P<cat>[\w.\-]+)\[(?P<eid>\d+)\]\s*(?P<msg>.*)$")
    # Serilog FILE sink default outputTemplate:
    #   "{Timestamp:yyyy-MM-dd HH:mm:ss.fff zzz} [{Level:u3}] {Message} …"
    # The mid-line "[INF]" defeats _SERILOG, so it gets its OWN tight form:
    # UTC-offset timestamp + bracketed UPPERCASE 3-letter code required (that
    # keeps EMQX "[warning]", RabbitMQ "[info]", HashiCorp "[INFO]" etc. out).
    _SERILOG_FILE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s+[+-]\d{2}:\d{2})\s+"
        r"\[(?P<level>VRB|DBG|INF|WRN|ERR|FTL)\]\s*(?P<msg>.*)$")

    def detect(self, sample_lines: list[str]) -> float:
        hits = seen = 0
        for ln in sample_lines:
            ln = ln.strip()
            if not ln:
                continue
            seen += 1
            if (self._SERILOG.match(ln) or self._SERILOG_FILE.match(ln)
                    or self._MEL.match(ln)):
                hits += 1
        return (hits / seen) if seen else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        m = self._SERILOG_FILE.match(s) or self._SERILOG.match(s)
        if m:
            g = m.groupdict()
            return self._event(level=g["level"], message=g["msg"], source="serilog",
                               ts_ms=parse_timestamp(g["ts"]), raw=line)
        m = self._MEL.match(s)
        if m:
            g = m.groupdict()
            lvl = {"trce": "trace", "dbug": "debug", "info": "info",
                   "warn": "warn", "fail": "error", "crit": "fatal"}[g["level"]]
            return self._event(level=lvl, message=g["msg"], source=g["cat"],
                               fields={"event_id": int(g["eid"])}, raw=line)
        return None


# ── 6. Rust: env_logger / tracing ───────────────────────────────────────────────
#     [2026-07-20T12:00:00Z INFO  my_crate::mod] message
#     [2026-07-20T12:00:00Z ERROR my_crate] boom

class RustAdapter(LogAdapter):
    name = "rust"
    language = "rust"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)\s+"
        r"(?P<level>TRACE|DEBUG|INFO|WARN|ERROR)\s+"
        r"(?P<target>[\w:]+)\]\s*(?P<msg>.*)$")

    def detect(self, sample_lines: list[str]) -> float:
        hits = seen = 0
        for ln in sample_lines:
            ln = ln.strip()
            if not ln:
                continue
            seen += 1
            if self._RE.match(ln):
                hits += 1
        return (hits / seen) if seen else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"], source=g["target"],
                           ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── 7. Go zap (console encoder) — tab-separated ─────────────────────────────────
#     2026-07-20T12:00:00.123Z\tINFO\tcaller/file.go:12\tmessage\t{json fields}

class GoZapAdapter(LogAdapter):
    name = "go_zap"
    language = "go"

    def detect(self, sample_lines: list[str]) -> float:
        hits = seen = 0
        for ln in sample_lines:
            if not ln.strip() or "\t" not in ln:
                continue
            seen += 1
            parts = ln.split("\t")
            if len(parts) >= 3 and parse_timestamp(parts[0]) is not None \
                    and canonical_level(parts[1]) in _LEVEL_CATEGORY:
                hits += 1
        return (hits / seen) if seen else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        parts = line.rstrip("\r\n").split("\t")
        if len(parts) < 3:
            return None
        ts_ms = parse_timestamp(parts[0])
        level = parts[1]
        caller = parts[2] if len(parts) > 2 else ""
        message = parts[3] if len(parts) > 3 else ""
        fields = {}
        if len(parts) > 4:
            try:
                fields = json.loads(parts[4])
            except Exception:
                fields = {"extra": parts[4]}
        return self._event(level=level, message=message, source=caller,
                           ts_ms=ts_ms, fields=fields, raw=line)


# ── 8. syslog RFC5424 + RFC3164 ─────────────────────────────────────────────────
#     <34>1 2026-07-20T12:00:00Z host app 1234 ID - message      (5424)
#     <34>Jul 20 12:00:00 host app[1234]: message                (3164)

class SyslogAdapter(LogAdapter):
    name = "syslog"
    language = "any"
    _5424 = re.compile(
        r"^<(?P<pri>\d{1,3})>(?P<ver>\d)\s+(?P<ts>\S+)\s+(?P<host>\S+)\s+"
        r"(?P<app>\S+)\s+(?P<pid>\S+)\s+(?P<msgid>\S+)\s+(?:\[[^\]]*\]|-)\s*(?P<msg>.*)$")
    _3164 = re.compile(
        r"^<(?P<pri>\d{1,3})>(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+"
        r"(?P<host>\S+)\s+(?P<tag>[^:\[]+)(?:\[(?P<pid>\d+)\])?:\s*(?P<msg>.*)$")

    def detect(self, sample_lines: list[str]) -> float:
        hits = seen = 0
        for ln in sample_lines:
            ln = ln.strip()
            if not ln:
                continue
            seen += 1
            if self._5424.match(ln) or self._3164.match(ln):
                hits += 1
        return (hits / seen) if seen else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        m = self._5424.match(s)
        if m:
            g = m.groupdict()
            sev = int(g["pri"]) & 0x07
            return self._event(level=_SYSLOG_SEVERITY.get(sev, "INFO"),
                               message=g["msg"], source=g["app"],
                               ts_ms=parse_timestamp(g["ts"]),
                               fields={"host": g["host"], "pid": g["pid"],
                                       "facility": int(g["pri"]) >> 3}, raw=line)
        m = self._3164.match(s)
        if m:
            g = m.groupdict()
            sev = int(g["pri"]) & 0x07
            return self._event(level=_SYSLOG_SEVERITY.get(sev, "INFO"),
                               message=g["msg"], source=g["tag"].strip(),
                               ts_ms=parse_timestamp(g["ts"]),
                               fields={"host": g["host"], "pid": g.get("pid")}, raw=line)
        return None


# ── 9. Android logcat (threadtime + brief) ──────────────────────────────────────
#     07-20 12:00:00.123  1234  1256 I Tag: message                (threadtime)
#     I/Tag( 1234): message                                        (brief)

class LogcatAdapter(LogAdapter):
    name = "logcat"
    language = "android"
    _TT = re.compile(
        r"^(?P<ts>\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+(?P<pid>\d+)\s+"
        r"(?P<tid>\d+)\s+(?P<lvl>[VDIWEFS])\s+(?P<tag>[^:]+):\s?(?P<msg>.*)$")
    _BRIEF = re.compile(
        r"^(?P<lvl>[VDIWEFS])/(?P<tag>[^(]+)\(\s*(?P<pid>\d+)\):\s?(?P<msg>.*)$")
    # `logcat -v time`: "04-01 16:05:35.099 I/ActivityManager(  585): message"
    _TIME = re.compile(
        r"^(?P<ts>\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+"
        r"(?P<lvl>[VDIWEFS])/(?P<tag>[^(]+)\(\s*(?P<pid>\d+)\):\s?(?P<msg>.*)$")
    # Android-Studio-style pidless brief: "I/ActivityManager: message". The tag
    # must not contain spaces so prose like "I/O error: …" can never match.
    _TAG = re.compile(
        r"^(?P<lvl>[VDIWEFS])/(?P<tag>[\w.$@/\-]+):\s(?P<msg>.*)$")
    # BATCH-6 gap fix — `-v threadtime` combined with the year/usec/UTC/zone/
    # uid output modifiers:
    #   2026-07-20 16:05:35.099123 +0000  1481  1520 10056 I ActivityManager: …
    _TTX = re.compile(
        r"^(?P<ts>(?:\d{4}-)?\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3,6})\s+"
        r"(?:(?P<zone>[+-]\d{4})\s+)?(?P<pid>\d+)\s+(?P<tid>\d+)\s+"
        r"(?:(?P<uid>\d+|u\d+a\d+|root|radio|system)\s+)?"
        r"(?P<lvl>[VDIWEFS])\s+(?P<tag>[^:]+):\s?(?P<msg>.*)$")
    _LVL = {"V": "trace", "D": "debug", "I": "info",
            "W": "warn", "E": "error", "F": "fatal", "S": "fatal"}

    def detect(self, sample_lines: list[str]) -> float:
        hits = seen = 0
        for ln in sample_lines:
            ln = ln.strip()
            if not ln:
                continue
            seen += 1
            if self._TT.match(ln) or self._BRIEF.match(ln) \
                    or self._TIME.match(ln) or self._TAG.match(ln) \
                    or self._TTX.match(ln):
                hits += 1
        return (hits / seen) if seen else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        m = self._TT.match(s) or self._TTX.match(s)
        if m:
            g = m.groupdict()
            # logcat threadtime has no year → assume current UTC year.
            ts_ms = self._logcat_ts(g["ts"]) \
                if not g["ts"][:4].isdigit() or "-" not in g["ts"][:5] \
                else parse_timestamp(g["ts"] + (" " + g["zone"] if g.get("zone") else ""))
            fields = {"pid": int(g["pid"]), "tid": int(g["tid"])}
            if g.get("uid"):
                fields["uid"] = g["uid"]
            return self._event(level=self._LVL.get(g["lvl"], g["lvl"]),
                               message=g["msg"], source=g["tag"].strip(),
                               ts_ms=ts_ms,
                               fields=fields,
                               raw=line)
        m = self._BRIEF.match(s)
        if m:
            g = m.groupdict()
            return self._event(level=self._LVL.get(g["lvl"], g["lvl"]),
                               message=g["msg"], source=g["tag"].strip(),
                               fields={"pid": int(g["pid"])}, raw=line)
        m = self._TIME.match(s)
        if m:
            g = m.groupdict()
            return self._event(level=self._LVL.get(g["lvl"], g["lvl"]),
                               message=g["msg"], source=g["tag"].strip(),
                               ts_ms=self._logcat_ts(g["ts"]),
                               fields={"pid": int(g["pid"])}, raw=line)
        m = self._TAG.match(s)
        if m:
            g = m.groupdict()
            return self._event(level=self._LVL.get(g["lvl"], g["lvl"]),
                               message=g["msg"], source=g["tag"].strip(), raw=line)
        return None

    @staticmethod
    def _logcat_ts(mmddhms: str) -> Optional[float]:
        # "07-20 12:00:00.123" — prepend current UTC year
        m = re.match(r"(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})\.(\d{3})", mmddhms)
        if not m:
            return None
        mo, dy, hh, mm, ss, ms = (int(x) for x in m.groups())
        yr = datetime.now().year                 # logcat time is device-local
        try:
            return _to_ms(datetime(yr, mo, dy, hh, mm, ss, ms * 1000))
        except ValueError:
            return None


# ── 10. Generic timestamped text (catch-all with a leading/embedded timestamp) ──
#     Finds ANY parseable timestamp anywhere near the start + an optional level
#     word, and treats the remainder as the message. This is the graceful bridge
#     between the strict adapters above and truly-raw text below.

class GenericTimestampAdapter(LogAdapter):
    name = "generic_ts"
    language = "any"
    _LEVEL_WORD = re.compile(
        r"\b(TRACE|DEBUG|INFO|INFORMATION|NOTICE|WARN|WARNING|ERROR|ERR|"
        r"CRITICAL|CRIT|FATAL|PANIC|SEVERE)\b", re.IGNORECASE)

    def detect(self, sample_lines: list[str]) -> float:
        hits = seen = 0
        for ln in sample_lines:
            ln = ln.strip()
            if not ln or ln[0] in "{[":
                continue
            seen += 1
            if parse_timestamp(ln) is not None or parse_timestamp(ln[:40]) is not None:
                hits += 1
        # Deliberately modest: this is a fallback, not a preferred match.
        return (hits / seen) * 0.6 if seen else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if not s.strip():
            return None
        ts_ms = parse_timestamp(s) or parse_timestamp(s[:40])
        lm = self._LEVEL_WORD.search(s[:60])
        level = lm.group(1) if lm else ""
        return self._event(level=level, message=s, source="", ts_ms=ts_ms, raw=line)


# ── SharpEmu / bracketed-fields text log ────────────────────────────────────────
#     [HH:mm:ss.fff] [LEVEL] [Category] SourceFile.cs:line message
# This is the FileLogSink format shipped by SharpEmu (the flagship .NET example)
# and a common shape for hand-rolled loggers: leading bracketed wall-clock time,
# bracketed level, bracketed category, then "file:line message".

class AgentVisionTextAdapter(LogAdapter):
    """AgentVision's OWN human-readable text sink (agentvision/log.txt).

        2026-07-30T00:49:31.531Z [INFO] [av.bootstrap.stdout] text=hello

    This has to exist, and existed only after the format shipped without it: the
    format was written by av_runtime and claimed by NO AgentVision adapter, so
    detection handed it to `gradle_daemon` at 0.91 confidence, which parsed the
    first few lines by luck and returned None for the rest — silently dropping
    level, source and message. AgentVision failing to parse its own emitter's
    output is the most embarrassing possible version of a coverage gap, and the
    project's own rule (every format gets a named adapter) is what prevents it.
    """
    name = "agentvision_text"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)\s+"
        r"\[(?P<level>[A-Za-z]+)\]\s+"
        r"\[(?P<source>av\.[\w.\-]+)\]\s*(?P<msg>.*)$")

    def detect(self, sample_lines: list[str]) -> float:
        hits = seen = 0
        for ln in sample_lines:
            ln = ln.strip()
            if not ln:
                continue
            seen += 1
            if self._RE.match(ln):
                hits += 1
        # The `av.` source prefix makes this unambiguous, so a match is decisive
        # rather than a score to be out-bid by a generic timestamped-line adapter.
        return (hits / seen) if seen else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"],
                           source=g["source"],
                           ts_ms=parse_timestamp(g["ts"]), raw=line)


class SharpEmuAdapter(LogAdapter):
    name = "sharpemu"
    language = "dotnet"
    _RE = re.compile(
        r"^\[(?P<ts>\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]\s+"
        r"\[(?P<level>TRACE|DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL|LOG)\]\s+"
        r"\[(?P<cat>[^\]]*)\]\s*(?P<rest>.*)$", re.IGNORECASE)
    _SRC = re.compile(
        r"^(?P<file>[\w.\-/]+\.(?:cs|cpp|cc|h|hpp|go|rs|py|js|ts)(?::\d+)?)\s+(?P<msg>.*)$")

    def detect(self, sample_lines: list[str]) -> float:
        hits = seen = 0
        for ln in sample_lines:
            ln = ln.strip()
            if not ln:
                continue
            seen += 1
            if self._RE.match(ln):
                hits += 1
        return (hits / seen) if seen else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        rest = g["rest"]
        fields = {"log_category": g["cat"]} if g["cat"] else {}
        sm = self._SRC.match(rest)
        if sm:
            fields["source_file"] = sm.group("file")
            rest = sm.group("msg")
        return self._event(level=g["level"], message=rest,
                           source=g["cat"] or "SharpEmu",
                           ts_ms=parse_timestamp(g["ts"]), fields=fields, raw=line)


# ── Ruby Logger / Rails ─────────────────────────────────────────────────────────
#     I, [2026-07-20T12:00:00.123456 #1234]  INFO -- progname: message

class RubyLoggerAdapter(LogAdapter):
    name = "ruby"
    language = "ruby"
    _RE = re.compile(
        r"^[DIWEFU], \[(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+#(?P<pid>\d+)\]\s+"
        r"(?P<level>DEBUG|INFO|WARN|ERROR|FATAL|UNKNOWN)\s+--\s+(?P<prog>[^:]*):\s*(?P<msg>.*)$")

    def detect(self, sample_lines: list[str]) -> float:
        # BATCH-6 gap fix: multi-line aware. A unicorn/puma stdout capture (or
        # the catalog sample) may hand SEVERAL Logger lines as ONE element with
        # embedded newlines — count the element as a hit when ≥½ of its
        # sub-lines are Ruby-Logger shaped, so it routes here, not structural.
        hits = seen = 0
        for ln in sample_lines:
            if not str(ln).strip():
                continue
            seen += 1
            subs = [x for x in str(ln).splitlines() if x.strip()]
            n = sum(1 for x in subs if self._RE.match(x))
            if subs and n >= 1 and n / len(subs) >= 0.5:
                hits += 1
        return (hits / seen) if seen else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if "\n" in s:                       # whole block → parse first record
            for x in s.splitlines():
                if self._RE.match(x):
                    ev = self.parse_line(x)
                    if ev:
                        ev["raw"] = line
                        return ev
            return None
        m = self._RE.match(s)
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"],
                           source=(g["prog"] or "").strip() or "ruby",
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"pid": int(g["pid"])}, raw=line)


# ── PHP Monolog ──────────────────────────────────────────────────────────────────
#     [2026-07-20T12:00:00.123456+00:00] channel.LEVEL: message {"ctx"} {"extra"}

class MonologAdapter(LogAdapter):
    name = "php_monolog"
    language = "php"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2})?)\]\s+"
        r"(?P<chan>[\w.\-]+)\.(?P<level>DEBUG|INFO|NOTICE|WARNING|ERROR|CRITICAL|ALERT|EMERGENCY)"
        r"\s*:\s*(?P<msg>.*)$")

    def detect(self, sample_lines: list[str]) -> float:
        # BATCH-4 gap fix: a Laravel entry is MULTI-LINE — the monolog envelope
        # line plus a PHP stack trace of '#N …' frames. Match on the FIRST line
        # of each element so the whole block still routes here.
        hits = seen = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            seen += 1
            first = ln.splitlines()[0] if "\n" in ln else ln
            if self._RE.match(first):
                hits += 1
        return (hits / seen) if seen else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        parts = s.splitlines() or [s]
        m = self._RE.match(parts[0])
        if not m:
            return None
        g = m.groupdict()
        fields = None
        if len(parts) > 1:                 # trailing PHP stack trace → keep it
            stack = [x for x in parts[1:] if x.strip()]
            if stack:
                fields = {"stack": "\n".join(stack)[:2000],
                          "stack_frames": sum(1 for x in stack
                                              if x.strip().startswith("#"))}
        return self._event(level=g["level"], message=g["msg"], source=g["chan"],
                           ts_ms=parse_timestamp(g["ts"]), fields=fields, raw=line)


# ── Elixir Logger ────────────────────────────────────────────────────────────────
#     12:00:00.123 [info] message      (level lowercase, in brackets; time bare)

class ElixirAdapter(LogAdapter):
    name = "elixir"
    language = "elixir"
    _RE = re.compile(
        r"^(?P<ts>\d{2}:\d{2}:\d{2}\.\d{3})\s+"
        r"\[(?P<level>debug|info|warn|warning|error|notice|critical|alert|emergency)\]\s+"
        r"(?P<msg>.*)$")

    def detect(self, sample_lines: list[str]) -> float:
        hits = seen = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            seen += 1
            if self._RE.match(ln.strip()):
                hits += 1
        return (hits / seen) if seen else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"], source="elixir",
                           ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── nginx / Apache access log (Common/Combined Log Format) ───────────────────────
#     1.2.3.4 - - [20/Jul/2026:12:00:00 +0000] "GET /x HTTP/1.1" 200 1234 "-" "UA"

class AccessLogAdapter(LogAdapter):
    name = "access_log"
    language = "any"
    _RE = re.compile(
        r'^(?P<ip>\S+)\s+\S+\s+(?P<user>\S+)\s+'
        r'\[(?P<ts>\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2}\s*[+-]\d{4})\]\s+'
        r'"(?P<req>[^"]*)"\s+(?P<status>\d{3})\s+(?P<size>\S+)(?:\s+"(?P<ref>[^"]*)"\s+"(?P<ua>[^"]*)")?')

    def detect(self, sample_lines: list[str]) -> float:
        hits = seen = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            seen += 1
            if self._RE.match(ln):
                hits += 1
        return (hits / seen) if seen else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        status = int(g["status"])
        level = "error" if status >= 500 else "warn" if status >= 400 else "info"
        return self._event(level=level, message=f'{g["req"]} → {status}',
                           source=g["ip"], ts_ms=parse_timestamp(g["ts"]),
                           fields={"status": status, "request": g["req"],
                                   "bytes": g["size"], "user_agent": g.get("ua")},
                           raw=line)


# ── nginx error log ──────────────────────────────────────────────────────────────
#     2026/07/20 12:00:00 [error] 1234#0: *5 connect() failed

class NginxErrorAdapter(LogAdapter):
    name = "nginx_error"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})\s+"
        r"\[(?P<level>debug|info|notice|warn|error|crit|alert|emerg)\]\s+"
        r"(?P<worker>\d+#\d+):\s*(?P<msg>.*)$")

    def detect(self, sample_lines: list[str]) -> float:
        hits = seen = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            seen += 1
            if self._RE.match(ln):
                hits += 1
        return (hits / seen) if seen else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"], source="nginx",
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"worker": g["worker"]}, raw=line)


# ── Apache error log ─────────────────────────────────────────────────────────────
#     [Mon Jul 20 12:00:00.123456 2026] [core:error] [pid 1234] message

class ApacheErrorAdapter(LogAdapter):
    name = "apache_error"
    language = "any"
    _RE = re.compile(
        r"^\[(?P<ts>[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+\d{4})\]\s+"
        r"\[(?P<mod>[\w.\-]+):(?P<level>\w+)\]\s+(?:\[pid\s+\d+[^\]]*\]\s+)?(?P<msg>.*)$")

    def detect(self, sample_lines: list[str]) -> float:
        hits = seen = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            seen += 1
            if self._RE.match(ln):
                hits += 1
        return (hits / seen) if seen else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"], source=g["mod"],
                           ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── systemd journal (short) ──────────────────────────────────────────────────────
#     Jul 20 12:00:00 host myunit[1234]: message   (like syslog3164, but NO <PRI>)

class SystemdAdapter(LogAdapter):
    name = "systemd"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+"
        r"(?P<host>\S+)\s+(?P<unit>[^\[\s]+)(?:\[(?P<pid>\d+)\])?:\s+(?P<msg>.*)$")

    def detect(self, sample_lines: list[str]) -> float:
        hits = seen = 0
        for ln in sample_lines:
            ln = ln.strip()
            if not ln or ln.startswith("<"):   # leave <PRI>… to the syslog adapter
                continue
            seen += 1
            if self._RE.match(ln):
                hits += 1
        return (hits / seen) if seen else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        # journal lines carry no level; promote obvious errors from the text.
        low = g["msg"].lower()
        level = "error" if any(w in low for w in ("error", "fail", "fatal")) else ""
        return self._event(level=level, message=g["msg"], source=g["unit"],
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"host": g["host"], "pid": g.get("pid")}, raw=line)


# ── Kubernetes klog / glog ───────────────────────────────────────────────────────
#     I0720 12:00:00.123456  1234 file.go:12] message

class KlogAdapter(LogAdapter):
    name = "klog"
    language = "go"
    _RE = re.compile(
        r"^(?P<lvl>[IWEF])(?P<mon>\d{2})(?P<day>\d{2})\s+"
        r"(?P<time>\d{2}:\d{2}:\d{2}\.\d+)\s+(?P<tid>\d+)\s+"
        r"(?P<file>[\w.\-]+:\d+)\]\s*(?P<msg>.*)$")
    _LVL = {"I": "info", "W": "warn", "E": "error", "F": "fatal"}

    def detect(self, sample_lines: list[str]) -> float:
        hits = seen = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            seen += 1
            if self._RE.match(ln):
                hits += 1
        return (hits / seen) if seen else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        yr = datetime.now().year                 # klog time is host-local
        ts_ms = None
        try:
            hh, mm, ss = g["time"].split(":")
            sec, _, frac = ss.partition(".")
            micro = int((frac or "0").ljust(6, "0")[:6])
            ts_ms = _to_ms(datetime(yr, int(g["mon"]), int(g["day"]), int(hh),
                                    int(mm), int(sec), micro))
        except Exception:
            pass
        return self._event(level=self._LVL.get(g["lvl"], g["lvl"]), message=g["msg"],
                           source=g["file"], ts_ms=ts_ms,
                           fields={"thread": g["tid"]}, raw=line)


# ── Compiler / build diagnostics (gcc, clang, rustc, MSVC) ───────────────────────
#     src/main.c:12:5: error: 'x' undeclared          (gnu: file:line:col: level: msg)
#     src/lib.rs:12:9: error[E0382]: borrow of moved   (rustc)
#     src\main.cpp(12): error C2065: 'x': undeclared   (msvc: file(line): level CODE: msg)

class BuildDiagnosticAdapter(LogAdapter):
    name = "build"
    language = "any"
    _GNU = re.compile(
        r"^(?P<file>[^:\s][^:]*):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
        r"(?P<level>fatal error|error|warning|note)(?:\[[^\]]*\])?\s*:\s*(?P<msg>.*)$",
        re.IGNORECASE)
    _MSVC = re.compile(
        r"^(?P<file>[^()]+)\((?P<line>\d+)(?:,\d+)?\):\s*"
        r"(?P<level>fatal error|error|warning)\s+(?P<code>[A-Z]+\d+):\s*(?P<msg>.*)$",
        re.IGNORECASE)

    def detect(self, sample_lines: list[str]) -> float:
        hits = seen = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            seen += 1
            if self._GNU.match(ln) or self._MSVC.match(ln):
                hits += 1
        return (hits / seen) if seen else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        m = self._GNU.match(s)
        if m:
            g = m.groupdict()
            lvl = "error" if "error" in g["level"].lower() else \
                  "warn" if "warning" in g["level"].lower() else "info"
            return self._event(level=lvl, message=g["msg"], source=g["file"],
                               fields={"line": int(g["line"]),
                                       "col": int(g["col"]) if g["col"] else None,
                                       "diagnostic": g["level"]}, raw=line)
        m = self._MSVC.match(s)
        if m:
            g = m.groupdict()
            lvl = "error" if "error" in g["level"].lower() else "warn"
            return self._event(level=lvl, message=g["msg"], source=g["file"],
                               fields={"line": int(g["line"]), "code": g["code"]},
                               raw=line)
        return None


# ── Test-runner results (pytest, go test, cargo test) ────────────────────────────
#     test_mod.py::test_thing PASSED        / FAILED test_mod.py::test_thing
#     --- FAIL: TestThing (0.01s)           / --- PASS: TestThing / ok  pkg 0.1s
#     test my_mod::it_works ... ok          / ... FAILED

class TestResultAdapter(LogAdapter):
    name = "test"
    language = "any"
    _PYTEST = re.compile(
        r"^(?:(?P<node1>[\w./]+::[\w.\[\]\-]+)\s+(?P<res1>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
        r"|(?P<res2>PASSED|FAILED|ERROR|SKIPPED)\s+(?P<node2>[\w./]+::[\w.\[\]\-]+))\b.*$")
    _GO = re.compile(r"^(?:---\s+(?P<res>PASS|FAIL|SKIP):\s+(?P<name>\S+)|(?P<pkgres>ok|FAIL|\?)\s+(?P<pkg>\S+))")
    _CARGO = re.compile(r"^test\s+(?P<name>[\w:]+)\s+\.\.\.\s+(?P<res>ok|FAILED|ignored)$")

    def detect(self, sample_lines: list[str]) -> float:
        hits = seen = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            seen += 1
            if self._PYTEST.match(ln) or self._GO.match(ln) or self._CARGO.match(ln):
                hits += 1
        return (hits / seen) if seen else 0.0

    def _mk(self, name, res, raw):
        failed = res.upper() in ("FAILED", "FAIL", "ERROR")
        level = "error" if failed else "info"
        return self._event(level=level, message=f"{name}: {res}", source=name,
                           category="test",
                           fields={"result": res, "passed": not failed}, raw=raw)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        m = self._PYTEST.match(s)
        if m:
            g = m.groupdict()
            return self._mk(g.get("node1") or g.get("node2") or "?",
                            g.get("res1") or g.get("res2") or "?", line)
        m = self._GO.match(s)
        if m:
            g = m.groupdict()
            if g.get("res"):
                return self._mk(g["name"], g["res"], line)
            return self._mk(g["pkg"], g["pkgres"], line)
        m = self._CARGO.match(s)
        if m:
            g = m.groupdict()
            return self._mk(g["name"], g["res"], line)
        return None


# ── Database logs (PostgreSQL, MySQL) ────────────────────────────────────────────
#     2026-07-20 12:00:00.123 UTC [1234] LOG:  statement: SELECT 1   (postgres)
#     2026-07-20T12:00:00.123456Z 12 [ERROR] [MY-000000] message      (mysql 8)

class DatabaseLogAdapter(LogAdapter):
    name = "database"
    language = "any"
    _PG = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+(?P<tz>[A-Z]{2,5})?\s*"
        r"\[(?P<pid>\d+)(?:-\w+)?\]\s+(?:\w+@\w+\s+)?"
        r"(?P<level>LOG|ERROR|FATAL|PANIC|WARNING|NOTICE|DETAIL|HINT|STATEMENT):\s*(?P<msg>.*)$")
    _MYSQL = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s+(?P<tid>\d+)\s+"
        r"\[(?P<level>System|Warning|Error|Note|ERROR|WARNING|INFO)\]\s*(?P<msg>.*)$")

    def detect(self, sample_lines: list[str]) -> float:
        hits = seen = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            seen += 1
            if self._PG.match(ln) or self._MYSQL.match(ln):
                hits += 1
        return (hits / seen) if seen else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        m = self._PG.match(s)
        if m:
            g = m.groupdict()
            return self._event(level=g["level"], message=g["msg"], source="postgres",
                               ts_ms=parse_timestamp(g["ts"]),
                               fields={"pid": int(g["pid"])}, raw=line)
        m = self._MYSQL.match(s)
        if m:
            g = m.groupdict()
            lvl = {"System": "info", "Note": "info"}.get(g["level"], g["level"])
            return self._event(level=lvl, message=g["msg"], source="mysql",
                               ts_ms=parse_timestamp(g["ts"]),
                               fields={"thread": int(g["tid"])}, raw=line)
        return None


# ── Python loguru ────────────────────────────────────────────────────────────
#     2026-07-21 10:00:00.123 | INFO     | module:func:42 - message

class LoguruAdapter(LogAdapter):
    name = "loguru"
    language = "python"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+\|\s+"
        r"(?P<level>TRACE|DEBUG|INFO|SUCCESS|WARNING|ERROR|CRITICAL)\s+\|\s+"
        r"(?P<loc>[^\s|]+)\s+-\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        h = s = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            s += 1
            if self._RE.match(ln):
                h += 1
        return (h / s) if s else 0.0

    def parse_line(self, line):
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        lvl = "info" if g["level"] == "SUCCESS" else g["level"]
        return self._event(level=lvl, message=g["msg"], source=g["loc"],
                           ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── .NET NLog (default pipe layout) ──────────────────────────────────────────
#     2026-07-21 10:00:00.1234|INFO|My.Namespace.Class|message

class NLogAdapter(LogAdapter):
    name = "nlog"
    language = "dotnet"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\|"
        r"(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\|"
        r"(?P<logger>[^|]*)\|(?P<msg>.*)$")

    def detect(self, sample_lines):
        h = s = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            s += 1
            if self._RE.match(ln):
                h += 1
        return (h / s) if s else 0.0

    def parse_line(self, line):
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"], source=g["logger"],
                           ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── Docker / containerd CRI log ──────────────────────────────────────────────
#     2026-07-21T10:00:00.123456789Z stdout F actual message

class DockerCriAdapter(LogAdapter):
    name = "docker_cri"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s+"
        r"(?P<stream>stdout|stderr)\s+(?P<tag>[FP])\s(?P<msg>.*)$")

    def detect(self, sample_lines):
        h = s = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            s += 1
            if self._RE.match(ln):
                h += 1
        return (h / s) if s else 0.0

    def parse_line(self, line):
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        return self._event(level="", message=g["msg"], source="container",
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"stream": g["stream"], "partial": g["tag"] == "P"},
                           category=g["stream"], raw=line)


# ── Redis server log ─────────────────────────────────────────────────────────
#     1234:M 21 Jul 2026 10:00:00.123 * Ready to accept connections

class RedisAdapter(LogAdapter):
    name = "redis"
    language = "any"
    _RE = re.compile(
        r"^(?P<pid>\d+):(?P<role>[XCSMR])\s+"
        r"(?P<ts>\d{1,2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2}\.\d+)\s+"
        r"(?P<sym>[.\-*#])\s+(?P<msg>.*)$")
    _LVL = {".": "debug", "-": "debug", "*": "info", "#": "warn"}

    def detect(self, sample_lines):
        h = s = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            s += 1
            if self._RE.match(ln):
                h += 1
        return (h / s) if s else 0.0

    def parse_line(self, line):
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        ts_ms = None
        mm = re.match(r"(\d{1,2}) ([A-Z][a-z]{2}) (\d{4}) (\d{2}):(\d{2}):(\d{2})\.(\d+)", g["ts"])
        if mm and mm.group(2) in _MONTHS:
            dy, mon, yr, hh, mi, ss, frac = mm.groups()
            try:
                ts_ms = _to_ms(datetime(int(yr), _MONTHS[mon], int(dy), int(hh),
                                        int(mi), int(ss), int(frac.ljust(6, "0")[:6])))
            except ValueError:
                ts_ms = None
        return self._event(level=self._LVL.get(g["sym"], "info"), message=g["msg"],
                           source="redis", ts_ms=ts_ms,
                           fields={"role": g["role"], "pid": int(g["pid"])}, raw=line)


# ── Django / Flask(werkzeug) dev-server access log ───────────────────────────
#     [21/Jul/2026 10:00:00] "GET / HTTP/1.1" 200 1234        (django runserver)
#     127.0.0.1 - - [21/Jul/2026 10:00:00] "GET / HTTP/1.1" 200 -   (werkzeug)

class DevAccessAdapter(LogAdapter):
    name = "dev_access"
    language = "any"
    _RE = re.compile(
        r'^(?:(?P<ip>\S+)\s+\S+\s+\S+\s+)?'
        r'\[(?P<ts>\d{2}/[A-Z][a-z]{2}/\d{4}[ :]\d{2}:\d{2}:\d{2})\]\s+'
        r'"(?P<req>[A-Z]+ [^"]*)"\s+(?P<status>\d{3})\s+(?P<size>\S+)')

    def detect(self, sample_lines):
        h = s = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            s += 1
            if self._RE.match(ln):
                h += 1
        return (h / s) if s else 0.0

    def parse_line(self, line):
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        status = int(g["status"])
        level = "error" if status >= 500 else "warn" if status >= 400 else "info"
        return self._event(level=level, message=f'{g["req"]} → {status}',
                           source=g["ip"] or "devserver", ts_ms=parse_timestamp(g["ts"]),
                           fields={"status": status, "request": g["req"]}, raw=line)


# ── HAProxy HTTP log (via syslog) ────────────────────────────────────────────
#     Jul 21 10:00:00 lb haproxy[123]: 1.2.3.4:5 [21/Jul/2026:10:00:00.1] fe be/srv 0/0/0/1/1 200 1234 ...

class HAProxyAdapter(LogAdapter):
    name = "haproxy"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+\S+\s+haproxy\[(?P<pid>\d+)\]:\s+"
        r"(?P<client>\S+)\s+\[[^\]]+\]\s+(?P<fe>\S+)\s+(?P<be>\S+)\s+\S+\s+(?P<status>\d{3})\s+(?P<bytes>\d+)\b.*$")

    def detect(self, sample_lines):
        h = s = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            s += 1
            if self._RE.match(ln):
                h += 1
        return (h / s) if s else 0.0

    def parse_line(self, line):
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        status = int(g["status"])
        level = "error" if status >= 500 else "warn" if status >= 400 else "info"
        return self._event(level=level, message=f'{g["fe"]}→{g["be"]} {status}',
                           source="haproxy", ts_ms=parse_timestamp(g["ts"]),
                           fields={"client": g["client"], "frontend": g["fe"],
                                   "backend": g["be"], "status": status,
                                   "bytes": int(g["bytes"])}, raw=line)


# ── Windows Event Log (single-line rendered XML) ─────────────────────────────
#     <Event ...><System>...<EventID>1000</EventID>...<Level>2</Level>...
#     <Provider Name='App'/>...</System><EventData><Data>msg</Data></EventData></Event>

class WindowsEventAdapter(LogAdapter):
    name = "windows_event"
    language = "any"
    _LVL = {"1": "fatal", "2": "error", "3": "warn", "4": "info", "5": "debug", "0": "info"}
    _EID = re.compile(r"<EventID[^>]*>(\d+)</EventID>")
    _LEV = re.compile(r"<Level>(\d+)</Level>")
    _PROV = re.compile(r"<Provider[^>]*Name=['\"]([^'\"]+)['\"]")
    _TIME = re.compile(r"SystemTime=['\"]([^'\"]+)['\"]")
    _DATA = re.compile(r"<Data[^>]*>([^<]*)</Data>")
    _MSG = re.compile(r"<Message>([^<]*)</Message>")

    def detect(self, sample_lines):
        h = s = 0
        for ln in sample_lines:
            ln = ln.strip()
            if not ln:
                continue
            s += 1
            if ln.startswith("<Event") and "</Event>" in ln:
                h += 1
        return (h / s) if s else 0.0

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        if not (s.startswith("<Event") and "</Event>" in s):
            return None
        lev = self._LEV.search(s)
        eid = self._EID.search(s)
        prov = self._PROV.search(s)
        tm = self._TIME.search(s)
        msgm = self._MSG.search(s) or self._DATA.search(s)
        return self._event(
            level=self._LVL.get(lev.group(1) if lev else "", ""),
            message=(msgm.group(1) if msgm else "").strip() or f"EventID {eid.group(1) if eid else '?'}",
            source=prov.group(1) if prov else "WindowsEvent",
            ts_ms=parse_timestamp(tm.group(1)) if tm else None,
            fields={"event_id": int(eid.group(1)) if eid else None,
                    "win_level": lev.group(1) if lev else None}, raw=line)


# ── Envoy / access proxy (default text access log) ──────────────────────────
#   [2024-01-01T00:00:00.000Z] "GET /path HTTP/1.1" 200 - 0 1234 5 4 "-" "curl" …

class EnvoyAdapter(LogAdapter):
    name = "envoy"
    language = "any"
    _RE = re.compile(
        r'^\[(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)\]\s+'
        r'"(?P<req>[A-Z]+ [^"]*)"\s+(?P<status>\d{3})\s+(?P<flags>\S+)\s+(?P<rest>.*)$')

    def detect(self, sample_lines):
        h = s = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            s += 1
            if self._RE.match(ln):
                h += 1
        return (h / s) if s else 0.0

    def parse_line(self, line):
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        status = int(g["status"])
        level = "error" if status >= 500 else "warn" if status >= 400 else "info"
        return self._event(level=level, message=f'{g["req"]} → {status}',
                           source="envoy", ts_ms=parse_timestamp(g["ts"]),
                           fields={"status": status, "request": g["req"],
                                   "response_flags": g["flags"]}, raw=line)


# ── Kafka / ZooKeeper server log (log4j "[ts] LEVEL [ctx] msg (logger)") ─────
#   [2024-01-01 00:00:00,000] INFO [KafkaServer id=1] started (kafka.server.KafkaServer)

class KafkaAdapter(LogAdapter):
    name = "kafka"
    language = "java"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+)\]\s+"
        r"(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+"
        r"(?:\[(?P<ctx>[^\]]*)\]\s+)?(?P<msg>.*?)\s*\((?P<logger>[\w.$]+)\)\s*$")

    def detect(self, sample_lines):
        h = s = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            s += 1
            if self._RE.match(ln):
                h += 1
        return (h / s) if s else 0.0

    def parse_line(self, line):
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        fields = {"logger": g["logger"]}
        if g.get("ctx"):
            fields["context"] = g["ctx"]
        return self._event(level=g["level"], message=g["msg"], source=g["logger"],
                           ts_ms=parse_timestamp(g["ts"]), fields=fields, raw=line)


# ── Linux auditd ──────────────────────────────────────────────────────────────
#   type=SYSCALL msg=audit(1712345678.123:456): arch=c000003e syscall=59 success=yes …

class AuditdAdapter(LogAdapter):
    name = "auditd"
    language = "any"
    _RE = re.compile(
        r"^type=(?P<atype>\w+)\s+msg=audit\((?P<epoch>\d+(?:\.\d+)?):(?P<seq>\d+)\):\s*(?P<rest>.*)$")

    def detect(self, sample_lines):
        h = s = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            s += 1
            if self._RE.match(ln):
                h += 1
        return (h / s) if s else 0.0

    def parse_line(self, line):
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        rest = g["rest"]
        fields = {"audit_type": g["atype"], "audit_seq": g["seq"]}
        # audit records are key=value; pull success/res for a level hint.
        for k, v in re.findall(r"(\w+)=(\S+)", rest):
            if k in ("success", "res", "exe", "syscall", "key", "acct", "terminal"):
                fields[k] = v
        level = "warn" if fields.get("success") == "no" or fields.get("res") == "failed" else "info"
        try:
            ts_ms = float(g["epoch"]) * 1000.0
        except ValueError:
            ts_ms = None
        return self._event(level=level, message=rest[:300], source=f"auditd.{g['atype']}",
                           ts_ms=ts_ms, fields=fields, raw=line)


# ── CI logs: GitHub Actions workflow commands + GitLab ──────────────────────────
#   ::error file=app.py,line=10::Something broke   |   ::warning::msg   |   ::notice::
#   GitLab: "section_start:...:" and ANSI is stripped upstream; ERROR: lines handled by generic

class CILogAdapter(LogAdapter):
    name = "ci"
    language = "any"
    _GHA = re.compile(
        r"^::(?P<cmd>error|warning|notice|debug)(?:\s+(?P<params>[^:]*))?::(?P<msg>.*)$")
    _LVL = {"error": "error", "warning": "warn", "notice": "info", "debug": "debug"}

    def detect(self, sample_lines):
        h = s = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            s += 1
            if self._GHA.match(ln):
                h += 1
        return (h / s) if s else 0.0

    def parse_line(self, line):
        m = self._GHA.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        fields = {}
        if g.get("params"):
            for k, v in re.findall(r"(\w+)=([^,]+)", g["params"]):
                fields[k] = v
        return self._event(level=self._LVL.get(g["cmd"], "info"), message=g["msg"],
                           source="github-actions", fields=fields or None, raw=line)


# ── Cassandra (logback pattern) ─────────────────────────────────────────────────
#   INFO  [main] 2024-01-01 00:00:00,123 StorageService.java:1234 - Starting up

class CassandraAdapter(LogAdapter):
    name = "cassandra"
    language = "java"
    _RE = re.compile(
        r"^(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+\[(?P<thread>[^\]]+)\]\s+"
        r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+)\s+"
        r"(?P<src>[\w.$]+\.java):(?P<line>\d+)\s+-\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        h = s = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            s += 1
            if self._RE.match(ln):
                h += 1
        return (h / s) if s else 0.0

    def parse_line(self, line):
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"], source=g["src"],
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"thread": g["thread"], "line": int(g["line"])}, raw=line)


# ── SQLite error/trace (also generic "prog: file:line: msg") ────────────────────
#   Error: near "SELCT": syntax error   |   Runtime error: near line 1: no such table: users

class SQLiteAdapter(LogAdapter):
    name = "sqlite"
    language = "any"
    _RE = re.compile(
        r"^(?:sqlite3?|SQLite)?\s*(?P<kind>Error|Runtime error|Parse error):\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        h = s = 0
        for ln in sample_lines:
            ln = ln.strip()
            if not ln:
                continue
            s += 1
            # Require SQLite-flavored wording to avoid stealing generic "Error:".
            if self._RE.match(ln) and re.search(
                    r"no such (table|column)|syntax error|database is locked|"
                    r"UNIQUE constraint|near \"|not a database", ln, re.I):
                h += 1
        return (h / s) if s else 0.0

    def parse_line(self, line):
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        return self._event(level="error", message=g["msg"], source="sqlite", raw=line)


# ── RabbitMQ ────────────────────────────────────────────────────────────────────
#   2024-01-01 00:00:00.123 [info] <0.123.0> Server startup complete

class RabbitMQAdapter(LogAdapter):
    name = "rabbitmq"
    language = "erlang"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)(?:\s*[+-]\d{2}:\d{2})?\s+"
        r"\[(?P<level>debug|info|notice|warning|error|critical|alert|emergency)\]\s+"
        r"(?P<pid><\d+\.\d+\.\d+>)\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        h = s = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            s += 1
            if self._RE.match(ln):
                h += 1
        return (h / s) if s else 0.0

    def parse_line(self, line):
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"], source="rabbitmq",
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"erlang_pid": g["pid"]}, raw=line)


# ── Spring Boot (default logback console) ───────────────────────────────────────
#   2024-01-01 00:00:00.123  INFO 12345 --- [  main] c.e.App : Started App in 3.2s

class SpringBootAdapter(LogAdapter):
    name = "springboot"
    language = "java"
    # ts covers Boot 2 ("2024-01-01 00:00:00.123") AND Boot 3's default ISO-T
    # with offset ("2026-07-22T10:00:01.123+02:00"); Boot 3.2 adds a second
    # bracketed group (application name) before the thread — optional here.
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]\d+(?:[+-]\d{2}:\d{2}|Z)?)\s+"
        r"(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+(?P<pid>\d+)\s+---\s+"
        r"(?:\[\s*(?P<app>[^\]]*?)\s*\]\s+)?"
        r"\[\s*(?P<thread>[^\]]*?)\s*\]\s+(?P<logger>\S+)\s*:\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        h = s = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            s += 1
            if self._RE.match(ln):
                h += 1
        return (h / s) if s else 0.0

    def parse_line(self, line):
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"], source=g["logger"],
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"pid": int(g["pid"]), "thread": g["thread"]}, raw=line)


# ── Rails request log ───────────────────────────────────────────────────────────
#   Completed 200 OK in 34ms (Views: 20ms | ActiveRecord: 5ms)
#   Started GET "/users" for 127.0.0.1 at 2024-01-01 00:00:00 +0000

class RailsAdapter(LogAdapter):
    name = "rails"
    language = "ruby"
    _COMPLETED = re.compile(
        r"^Completed (?P<status>\d{3}) (?P<statustext>[\w ]+?) in (?P<ms>\d+)ms(?P<rest>.*)$")
    _STARTED = re.compile(
        r'^Started (?P<verb>[A-Z]+) "(?P<path>[^"]*)" for (?P<ip>\S+) at (?P<ts>.+)$')

    def detect(self, sample_lines):
        # BATCH-4 gap fix: a whole Started→Completed request block may arrive
        # as ONE element — match per sub-line so the block still routes here.
        h = s = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            s += 1
            if any(self._COMPLETED.match(x.strip()) or self._STARTED.match(x.strip())
                   for x in ln.splitlines()):
                h += 1
        return (h / s) if s else 0.0

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        parts = s.splitlines() or [s]
        started = completed = None
        for x in parts:
            x = x.strip()
            started = started or self._STARTED.match(x)
            completed = completed or self._COMPLETED.match(x)
        if completed:                      # the outcome line wins for level
            g = completed.groupdict()
            status = int(g["status"])
            level = "error" if status >= 500 else "warn" if status >= 400 else "info"
            fields = {"status": status, "duration_ms": int(g["ms"])}
            msg = f'Completed {status} in {g["ms"]}ms'
            ts_ms = None
            if started:                    # block: merge the request info in
                sg = started.groupdict()
                fields.update({"verb": sg["verb"], "path": sg["path"],
                               "ip": sg["ip"]})
                msg = f'{sg["verb"]} {sg["path"]} → {status} in {g["ms"]}ms'
                ts_ms = parse_timestamp(sg["ts"])
            return self._event(level=level, message=msg, source="rails.request",
                               ts_ms=ts_ms, fields=fields, raw=line)
        if started:
            g = started.groupdict()
            return self._event(level="info", message=f'{g["verb"]} {g["path"]}',
                               source="rails.request", ts_ms=parse_timestamp(g["ts"]),
                               fields={"verb": g["verb"], "path": g["path"], "ip": g["ip"]},
                               raw=line)
        return None


# ── Valgrind / AddressSanitizer ─────────────────────────────────────────────────
#   ==12345== Invalid read of size 4                          (valgrind)
#   ==12345==ERROR: AddressSanitizer: heap-use-after-free …   (asan)

class SanitizerAdapter(LogAdapter):
    name = "sanitizer"
    language = "cpp"
    _ASAN = re.compile(r"^==(?P<pid>\d+)==\s*(?:ERROR:\s*)?(?P<tool>AddressSanitizer|LeakSanitizer|"
                       r"ThreadSanitizer|UndefinedBehaviorSanitizer):\s*(?P<msg>.*)$")
    # BATCH-6 gap fix: helgrind/DRD thread-error phrasing added so a race
    # report routes here instead of falling to the structural normalizer.
    _VG = re.compile(r"^==(?P<pid>\d+)==\s+(?P<msg>(?:Invalid (?:read|write)|"
                     r"Conditional jump|Use of uninitialised|.*definitely lost|Mismatched free|"
                     r"Possible data race|This conflicts with a previous|"
                     r"Conflicting (?:load|store)|Lock order|"
                     r"Thread #\d+(?::| unlocked| acquired)|"
                     r"(?:HEAP|LEAK|ERROR) SUMMARY).*)$")
    _PID_PREFIX = re.compile(r"^==\d+==")

    def detect(self, sample_lines):
        # Multi-line aware: a whole ASan/valgrind report may arrive as ONE
        # element (catalog blocks, crash dumps pasted whole). An element hits
        # when ≥1 sub-line is a recognized diagnostic AND the ==pid== prefix
        # dominates the block (so prose merely quoting valgrind can't match).
        h = s = 0
        for ln in sample_lines:
            if not str(ln).strip():
                continue
            s += 1
            subs = [x for x in str(ln).splitlines() if x.strip()]
            if not subs:
                continue
            diag = sum(1 for x in subs if self._ASAN.match(x) or self._VG.match(x))
            pref = sum(1 for x in subs if self._PID_PREFIX.match(x))
            if diag >= 1 and pref / len(subs) >= 0.6:
                h += 1
        return (h / s) if s else 0.0

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        # whole report handed as one block → summarize on the first diagnostic
        if "\n" in s:
            subs = [x for x in s.splitlines() if x.strip()]
            for x in subs:
                m = self._ASAN.match(x) or self._VG.match(x)
                if m:
                    g = m.groupdict()
                    tool = g.get("tool") if "tool" in g else None
                    return self._event(level="error", message=g["msg"],
                                       source=tool or "valgrind",
                                       fields={"pid": int(g["pid"]),
                                               "block_lines": len(subs)}, raw=line)
            return None
        m = self._ASAN.match(s)
        if m:
            g = m.groupdict()
            return self._event(level="error", message=g["msg"], source=g["tool"],
                               fields={"pid": int(g["pid"]), "tool": g["tool"]}, raw=line)
        m = self._VG.match(s)
        if m:
            g = m.groupdict()
            return self._event(level="error", message=g["msg"], source="valgrind",
                               fields={"pid": int(g["pid"])}, raw=line)
        return None


# ── Traefik (logfmt-ish but with level=) — handled by logfmt; Consul/Vault below ─
# Consul/Vault: 2024-01-01T00:00:00.000Z [INFO]  agent: started: key=value …

class HashiCorpAdapter(LogAdapter):
    name = "hashicorp"
    language = "go"
    # ts may carry Z or a ±HH:MM / ±HHMM offset (Consul/Nomad default hclog:
    # "2023-12-22T15:49:38.878+0800 [INFO]  agent.server.raft: ...", Terraform:
    # "2022-09-19T09:33:34.487-0500 [DEBUG] provider.x: ...").
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+(?:Z|[+-]\d{2}:?\d{2})?)\s+"
        r"\[(?P<level>TRACE|DEBUG|INFO|WARN|WARNING|ERROR|ERR)\]\s+"
        r"(?P<comp>[\w.\-]+):\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        h = s = 0
        for ln in sample_lines:
            if not ln.strip():
                continue
            s += 1
            if self._RE.match(ln):
                h += 1
        return (h / s) if s else 0.0

    def parse_line(self, line):
        m = self._RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"], source=g["comp"],
                           ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── Universal structural auto-normalizer ────────────────────────────────────────
# The centerpiece of "support ALL logs". It sits ONE STEP above `raw`: it never
# beats a named adapter (its confidence is capped well below theirs) but it turns
# an UNRECOGNIZED line into a genuinely useful unified event by recognizing the
# common STRUCTURAL SHAPES that cut across ecosystems and extracting whatever it
# can — timestamp, level, message, and key=value / embedded-json fields.
#
# Shapes handled (tried in this order; first match wins, then a keyword fallback):
#   1. kernel ring-buffer reltime   [   12.345678] msg          (monotonic, no wall ts)
#   2. dmesg ctime                  [Sun Jul 20 14:03:11 2026] msg
#   3. /dev/kmsg structured         6,339,5140900,-;msg
#   4. syslog RFC5424               <PRI>1 ISO host app pid msgid [sd] msg
#   5. syslog RFC3164 (+ bare BSD)  <PRI>Mmm DD HH:MM:SS host tag[pid]: msg
#   6. ISO-8601 / RFC3339 prefix    2026-..T..Z … freeform
#   7. level-bracketed / prefixed   [LEVEL] msg | LEVEL: msg | <ts> LEVEL msg
#   8. logfmt                       k=v k2="v2" …
#   9. embedded JSON object         text … {"k": "v"} … (merged into fields)
#  10. CLF / W3C access             ip - - [ts] "GET / HTTP/1.1" 200 123
#  11. fallback                     data.message = raw, level inferred from keywords
#
# It NEVER raises (every branch is guarded) and ALWAYS returns an event for a
# non-blank line, so a log from a program AgentVision has never seen still lands
# as schema-valid, level-classified, timestamped-where-possible JSON.

class StructuralNormalizerAdapter(LogAdapter):
    name = "structural"
    language = "any"

    # 1. kernel ring-buffer relative time: "[   12.345678] message"
    _KMSG_REL = re.compile(r"^\[\s*(?P<up>\d+\.\d{3,6})\]\s?(?P<msg>.*)$")
    # 2. dmesg ctime: "[Sun Jul 20 14:03:11 2026] message"
    _KMSG_CTIME = re.compile(
        r"^\[(?P<ts>[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+"
        r"\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+\d{4})\]\s?(?P<msg>.*)$")
    # 3. /dev/kmsg: "prio,seq,ts_usec,flags;message"
    _DEVKMSG = re.compile(r"^(?P<pri>\d{1,3}),(?P<seq>\d+),(?P<us>\d+),(?P<flags>[^;,]*);(?P<msg>.*)$")
    # 4. syslog RFC5424
    _SYSLOG5424 = re.compile(
        r"^<(?P<pri>\d{1,3})>(?P<ver>\d)\s+(?P<ts>\S+)\s+(?P<host>\S+)\s+"
        r"(?P<app>\S+)\s+(?P<pid>\S+)\s+(?P<msgid>\S+)\s+(?:\[[^\]]*\]|-)\s*(?P<msg>.*)$")
    # 5a. syslog RFC3164 (with <PRI>)
    _SYSLOG3164 = re.compile(
        r"^<(?P<pri>\d{1,3})>(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+"
        r"(?P<host>\S+)\s+(?P<tag>[^:\[]+)(?:\[(?P<pid>\d+)\])?:\s*(?P<msg>.*)$")
    # 5b. bare BSD syslog (no <PRI>): "Mmm DD HH:MM:SS host tag[pid]: message"
    _BSD = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+"
        r"(?P<host>[\w.\-]+)\s+(?P<tag>[\w.\-/]+)(?:\[(?P<pid>\d+)\])?:\s+(?P<msg>.*)$")
    # 6. ISO-8601 / RFC3339 leading timestamp
    _ISO_PREFIX = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?"
        r"(?:Z|[+-]\d{2}:?\d{2})?)\s+(?P<msg>.*)$")
    # 7. level-bracketed / level-prefixed shapes
    _LVL_BRACKET = re.compile(
        r"^\[(?P<level>TRACE|DEBUG|INFO|INFORMATION|NOTICE|WARN|WARNING|ERROR|ERR|"
        r"CRIT|CRITICAL|FATAL|PANIC|EMERG|ALERT|SEVERE|FINE|FINER|FINEST)\]\s*(?P<msg>.*)$",
        re.IGNORECASE)
    _LVL_PREFIX = re.compile(
        r"^(?P<level>TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|CRITICAL|FATAL|PANIC|SEVERE)"
        r"\s*[:\-]\s+(?P<msg>.*)$")
    # a level word appearing anywhere near the head of the line
    _LVL_WORD = re.compile(
        r"\b(TRACE|DEBUG|INFO|INFORMATION|NOTICE|WARN|WARNING|ERROR|ERR|"
        r"CRIT|CRITICAL|FATAL|PANIC|EMERG|ALERT|SEVERE)\b")
    # 8. logfmt key=value pairs
    _KV = re.compile(r'([\w.\-]+)=("(?:[^"\\]|\\.)*"|\'[^\']*\'|\S+)')
    # 9. embedded JSON object anywhere in the line
    _JSON_OBJ = re.compile(r"\{.*\}")
    # 10. Common Log Format / combined access line
    _CLF = re.compile(
        r'^(?P<ip>\S+)\s+\S+\s+\S+\s+\[(?P<ts>[^\]]+)\]\s+"(?P<req>[A-Z]+ [^"]*)"'
        r'\s+(?P<status>\d{3})\s+(?P<size>\S+)')
    # keyword → level for the final fallback
    _ERR_WORDS = ("panic", "fatal", "segfault", "traceback", "exception",
                  "critical", "emerg", " error", "error:", "[error]", "failed",
                  "failure", "abort")
    _WARN_WORDS = ("warning", " warn", "[warn]", "deprecat")

    def detect(self, sample_lines: list[str]) -> float:
        seen = recog = 0
        for ln in sample_lines:
            s = ln.strip()
            if not s:
                continue
            seen += 1
            try:
                if self._recognized(s):
                    recog += 1
            except Exception:
                pass
        if not seen:
            return 0.0
        # Deliberately capped low: named adapters (→1.0) and generic_ts (→0.6)
        # must always outrank this; a tiny floor keeps it above raw (0.01) so an
        # utterly unknown line still lands here rather than in raw.
        return max(0.02, min(0.3, 0.3 * (recog / seen)))

    def _recognized(self, s: str) -> bool:
        if s[0] == "[" and (self._KMSG_REL.match(s) or self._KMSG_CTIME.match(s)
                            or self._LVL_BRACKET.match(s)):
            return True
        if s[0] == "<" and (self._SYSLOG5424.match(s) or self._SYSLOG3164.match(s)):
            return True
        if self._DEVKMSG.match(s) or self._BSD.match(s) or self._ISO_PREFIX.match(s):
            return True
        if self._LVL_PREFIX.match(s) or self._CLF.match(s):
            return True
        if parse_timestamp(s) is not None or parse_timestamp(s[:40]) is not None:
            return True
        if self._LVL_WORD.search(s[:48]):
            return True
        kv = self._KV.findall(s)
        if len(kv) >= 2:
            return True
        return bool(self._JSON_OBJ.search(s))

    # ── field helpers ────────────────────────────────────────────────────────
    def _kv_fields(self, text: str) -> dict:
        out: dict = {}
        for k, v in self._KV.findall(text):
            if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
                v = v[1:-1]
            out[k] = v
        return out

    def _embedded_json(self, text: str) -> Optional[dict]:
        m = self._JSON_OBJ.search(text)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    def _infer_level(self, text: str) -> str:
        low = text.lower()
        if any(w in low for w in self._ERR_WORDS):
            return "error"
        if any(w in low for w in self._WARN_WORDS):
            return "warn"
        return ""

    def _enrich(self, message: str, fields: dict) -> None:
        """Pull key=value and embedded-json structure out of a free message so
        even a shape we only loosely recognized still yields structured data."""
        obj = self._embedded_json(message)
        if obj:
            for k, v in obj.items():
                fields.setdefault(k, v)
        for k, v in self._kv_fields(message).items():
            if k not in ("http", "https") and k not in fields:
                fields[k] = v

    def parse_line(self, line: str) -> Optional[dict]:
        try:
            return self._parse(line)
        except Exception:
            # The whole point of this adapter is to never lose a line.
            s = line.rstrip("\r\n")
            return self._event(level=self._infer_level(s), message=s,
                               source="", ts_ms=None, raw=line) if s.strip() else None

    def _parse(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if not s.strip():
            return None

        # 1. kernel ring-buffer relative time — monotonic, so NO wall-clock ts_ms
        m = self._KMSG_REL.match(s)
        if m:
            msg = m.group("msg")
            fields = {"uptime": float(m.group("up")), "shape": "kmsg_reltime"}
            src, msg2 = self._split_subsys(msg)
            self._enrich(msg2, fields)
            return self._event(level=self._infer_level(msg), message=msg2,
                               source=src, ts_ms=None, fields=fields, raw=line)

        # 2. dmesg ctime
        m = self._KMSG_CTIME.match(s)
        if m:
            msg = m.group("msg")
            fields = {"shape": "kmsg_ctime"}
            src, msg2 = self._split_subsys(msg)
            self._enrich(msg2, fields)
            return self._event(level=self._infer_level(msg), message=msg2, source=src,
                               ts_ms=parse_timestamp(m.group("ts")), fields=fields, raw=line)

        # 3. /dev/kmsg structured
        m = self._DEVKMSG.match(s)
        if m:
            pri = int(m.group("pri"))
            sev = pri & 0x07
            ts_ms = None
            try:
                ts_ms = float(m.group("us")) / 1000.0  # monotonic usec → ms (best-effort)
            except ValueError:
                pass
            msg = m.group("msg")
            fields = {"shape": "devkmsg", "seq": m.group("seq"),
                      "facility": pri >> 3, "priority": pri}
            self._enrich(msg, fields)
            return self._event(level=_SYSLOG_SEVERITY.get(sev, "INFO"), message=msg,
                               source="kernel", ts_ms=None, fields=fields, raw=line)

        # 4. syslog RFC5424
        m = self._SYSLOG5424.match(s)
        if m:
            pri = int(m.group("pri"))
            fields = {"shape": "syslog5424", "host": m.group("host"),
                      "pid": m.group("pid"), "facility": pri >> 3}
            self._enrich(m.group("msg"), fields)
            return self._event(level=_SYSLOG_SEVERITY.get(pri & 0x07, "INFO"),
                               message=m.group("msg"), source=m.group("app"),
                               ts_ms=parse_timestamp(m.group("ts")), fields=fields, raw=line)

        # 5a. syslog RFC3164 with <PRI>
        m = self._SYSLOG3164.match(s)
        if m:
            pri = int(m.group("pri"))
            fields = {"shape": "syslog3164", "host": m.group("host"),
                      "pid": m.group("pid"), "facility": pri >> 3}
            self._enrich(m.group("msg"), fields)
            return self._event(level=_SYSLOG_SEVERITY.get(pri & 0x07, "INFO"),
                               message=m.group("msg"), source=m.group("tag").strip(),
                               ts_ms=parse_timestamp(m.group("ts")), fields=fields, raw=line)

        # 5b. bare BSD syslog (no <PRI>)
        m = self._BSD.match(s)
        if m:
            msg = m.group("msg")
            fields = {"shape": "bsd_syslog", "host": m.group("host"),
                      "pid": m.group("pid")}
            self._enrich(msg, fields)
            return self._event(level=self._infer_level(msg), message=msg,
                               source=m.group("tag"), ts_ms=parse_timestamp(m.group("ts")),
                               fields=fields, raw=line)

        # 10. Common Log Format (access lines) — before generic ISO so status maps
        m = self._CLF.match(s)
        if m:
            status = int(m.group("status"))
            level = "error" if status >= 500 else "warn" if status >= 400 else "info"
            return self._event(level=level, message=f'{m.group("req")} → {status}',
                               source=m.group("ip"), ts_ms=parse_timestamp(m.group("ts")),
                               fields={"shape": "clf", "status": status,
                                       "request": m.group("req"), "bytes": m.group("size")},
                               raw=line)

        # 7. level-bracketed  "[LEVEL] message"
        m = self._LVL_BRACKET.match(s)
        if m:
            msg = m.group("msg")
            fields = {"shape": "level_bracket"}
            self._enrich(msg, fields)
            return self._event(level=m.group("level"), message=msg, source="",
                               ts_ms=parse_timestamp(msg[:40]), fields=fields, raw=line)

        # 6. ISO / RFC3339 leading timestamp — freeform remainder
        m = self._ISO_PREFIX.match(s)
        if m:
            msg = m.group("msg")
            fields = {"shape": "iso_prefix"}
            lw = self._LVL_WORD.search(msg[:48])
            self._enrich(msg, fields)
            return self._event(level=(lw.group(1) if lw else self._infer_level(msg)),
                               message=msg, source="",
                               ts_ms=parse_timestamp(m.group("ts")), fields=fields, raw=line)

        # 7b. level-prefixed  "LEVEL: message"
        m = self._LVL_PREFIX.match(s)
        if m:
            msg = m.group("msg")
            fields = {"shape": "level_prefix"}
            self._enrich(msg, fields)
            return self._event(level=m.group("level"), message=msg, source="",
                               ts_ms=parse_timestamp(msg[:40]), fields=fields, raw=line)

        # 8. logfmt — a line dominated by key=value pairs. Extract ALL pairs as
        # structured fields even when there is no explicit level/msg key.
        kv = self._kv_fields(s)
        if len(kv) >= 2:
            level = kv.get("level") or kv.get("lvl") or kv.get("severity") or ""
            message = kv.get("msg") or kv.get("message") or ""
            ts_ms = None
            for k in ("ts", "time", "timestamp", "t", "eventtime"):
                if k in kv:
                    ts_ms = parse_timestamp(kv[k])
                    if ts_ms is not None:
                        break
            fields = {"shape": "logfmt"}
            fields.update({k: v for k, v in kv.items()
                           if k not in ("level", "lvl", "severity", "msg", "message")})
            return self._event(level=level or self._infer_level(s),
                               message=message or s, source=kv.get("source", ""),
                               ts_ms=ts_ms, fields=fields, raw=line)

        # 9 + 11. embedded JSON and/or bare timestamp, else keyword fallback
        ts_ms = parse_timestamp(s) or parse_timestamp(s[:40])
        fields = {"shape": "text"}
        obj = self._embedded_json(s)
        if obj:
            fields["shape"] = "embedded_json"
            for k, v in obj.items():
                fields.setdefault(k, v)
        lw = self._LVL_WORD.search(s[:48])
        level = lw.group(1) if lw else self._infer_level(s)
        return self._event(level=level, message=s, source="", ts_ms=ts_ms,
                           fields=fields, raw=line)

    @staticmethod
    def _split_subsys(msg: str) -> tuple[str, str]:
        """A kernel message often reads 'subsys prefix: text' — surface the
        prefix as source without discarding it from the message."""
        m = re.match(r"^([\w.\-]+(?: [\w.\-]+)?):\s+(.*)$", msg)
        if m and len(m.group(1)) <= 32:
            return m.group(1), msg
        return "", msg


# ── Raw text (always matches; the floor of the registry) ─────────────────────────

class RawAdapter(LogAdapter):
    name = "raw"
    language = "any"

    def detect(self, sample_lines: list[str]) -> float:
        # Tiny non-zero floor so it wins only when nothing else does.
        return 0.01

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if not s.strip():
            return None
        # Some raw streams still shout errors — surface them as error category.
        lvl = ""
        low = s.lower()
        if any(w in low for w in ("traceback", "exception", "fatal", "panic",
                                  "segfault", " error", "error:")):
            lvl = "error"
        return self._event(level=lvl, message=s, source="", ts_ms=None, raw=line)


# ── Registry ─────────────────────────────────────────────────────────────────────
# Order matters only as a tiebreak; detection is by confidence score. The most
# specific/structured adapters come first, RAW last (it is the floor).

REGISTRY: list[LogAdapter] = [
    JsonLinesAdapter(),          # covers ALL JSON loggers (see its docstring)
    WindowsEventAdapter(),       # <Event>…</Event> XML (before generic text)
    Log4jAdapter(),
    RustAdapter(),
    DotNetAdapter(),
    NLogAdapter(),
    AgentVisionTextAdapter(),
    SharpEmuAdapter(),
    LoguruAdapter(),
    DockerCriAdapter(),
    SyslogAdapter(),
    HAProxyAdapter(),            # before systemd: haproxy lines share the syslog prefix
    SystemdAdapter(),
    LogcatAdapter(),
    KlogAdapter(),
    GoZapAdapter(),
    RubyLoggerAdapter(),
    MonologAdapter(),
    ElixirAdapter(),
    RedisAdapter(),
    KafkaAdapter(),              # before python_logging: "[ts] LEVEL [ctx] msg (logger)"
    EnvoyAdapter(),
    CILogAdapter(),             # ::error:: — distinctive, no collision
    SanitizerAdapter(),         # ==pid== ASan/valgrind
    AuditdAdapter(),            # before logfmt: type=… msg=audit(…) has k=v pairs
    CassandraAdapter(),         # LEVEL [thread] ts src.java:line - msg
    SpringBootAdapter(),        # before python_logging: "ts LEVEL pid --- [thr] logger : msg"
    RabbitMQAdapter(),          # ts [level] <pid> msg
    HashiCorpAdapter(),         # Consul/Vault: ISO [LEVEL] comp: msg
    RailsAdapter(),             # Completed/Started request lines
    SQLiteAdapter(),            # SQLite-flavored Error: lines (guarded)
    AccessLogAdapter(),
    DevAccessAdapter(),
    NginxErrorAdapter(),
    ApacheErrorAdapter(),
    DatabaseLogAdapter(),
    BuildDiagnosticAdapter(),
    TestResultAdapter(),
    LogfmtAdapter(),
    PythonLoggingAdapter(),
    GenericTimestampAdapter(),
    StructuralNormalizerAdapter(),   # universal fallback: just ABOVE raw
    RawAdapter(),
]

_BY_NAME = {a.name: a for a in REGISTRY}

# These two generic fallbacks are ALWAYS pinned at the very end, in this order,
# so any specific adapter (built-in or family-module) outranks them on a tie.
_TAIL_NAMES = ("structural", "raw")


def register_adapter(adapter: LogAdapter, *, front: bool = False,
                     before: Optional[str] = None) -> None:
    """Add a custom adapter. Idempotent by name (a re-register replaces the old
    instance). The `structural` and `raw` fallbacks are always kept last.

      front=True   → highest priority (index 0), wins ties over every built-in.
      before=NAME  → insert immediately before the named adapter (used by the
                     family modules so a specific syslog-shaped format outranks
                     the generic `syslog`/`systemd` adapters on a 1.0 tie).
      (default)    → insert just before the pinned `structural`/`raw` tail.
    """
    global REGISTRY, _BY_NAME
    rest = [a for a in REGISTRY if a.name != adapter.name and a.name not in _TAIL_NAMES]
    tail = [a for a in REGISTRY if a.name in _TAIL_NAMES]
    # keep the tail in the canonical (structural, raw) order
    tail.sort(key=lambda a: _TAIL_NAMES.index(a.name))
    if front:
        rest.insert(0, adapter)
    elif before:
        idx = next((i for i, a in enumerate(rest) if a.name == before), len(rest))
        rest.insert(idx, adapter)
    else:
        rest.append(adapter)
    REGISTRY = rest + tail
    _BY_NAME = {a.name: a for a in REGISTRY}


def get_adapter(name: str) -> Optional[LogAdapter]:
    return _BY_NAME.get(name)


# ── built-in name protection ──────────────────────────────────────────────────
# register_adapter() is idempotent BY NAME — a re-register REPLACES the prior
# instance. That is the right behaviour for updating an adapter in place, but it
# means a user-supplied adapter that happens to reuse a built-in's name would
# SILENTLY SHADOW the built-in: the registry size and duplicate-name count both
# look healthy while the built-in's format quietly stops being recognised (it
# falls through to `structural`). This actually happened with `sharpemu` — the
# built-in FileLogSink parser was replaced by a user adapter for SharpEmu's
# bracketed channel-tag lines, and two test suites broke.
#
# The package __init__ freezes the built-in name set immediately BEFORE loading
# `user_adapters` (which is imported last), so anything registered up to that
# point is "built-in" and off-limits to a user adapter.
_BUILTIN_NAMES: Optional[frozenset] = None


def snapshot_builtin_names() -> frozenset:
    """Freeze the currently-registered names as the protected BUILT-IN set.
    Called once by connectors.adapters.__init__ right before user_adapters
    loads. Idempotent-safe: a later call re-freezes (the first one wins in
    practice because nothing else registers between the two)."""
    global _BUILTIN_NAMES
    _BUILTIN_NAMES = frozenset(a.name for a in REGISTRY)
    return _BUILTIN_NAMES


def builtin_names() -> frozenset:
    """The protected built-in adapter names. Falls back to *every* currently
    registered name when no snapshot was taken (e.g. user_adapters imported
    directly in a test) — deliberately conservative: better to reject a name
    than to let a built-in be shadowed."""
    if _BUILTIN_NAMES is not None:
        return _BUILTIN_NAMES
    return frozenset(a.name for a in REGISTRY)


def list_adapters() -> list[dict]:
    return [{"name": a.name, "language": a.language} for a in REGISTRY]


def detect_adapter(sample_lines: list[str]) -> tuple[LogAdapter, float, dict]:
    """Score every adapter on a sample and return (winner, confidence, scores).
    RAW guarantees a non-empty result, so this never fails."""
    scores: dict[str, float] = {}
    best: LogAdapter = _BY_NAME["raw"]
    best_score = 0.0
    for a in REGISTRY:
        try:
            s = a.detect(sample_lines)
        except Exception:
            s = 0.0
        scores[a.name] = round(s, 3)
        if s > best_score:
            best_score, best = s, a
    return best, best_score, scores


def detect_adapter_for_file(path: str, sample: int = 60) -> tuple[str, float, dict]:
    """Sample the first `sample` non-empty lines of a file and detect its format.
    Returns (adapter_name, confidence, all_scores). ('raw', 0.01, …) if the file
    is missing/empty."""
    import os
    lines: list[str] = []
    try:
        if not path or not os.path.exists(path):
            return "raw", 0.01, {}
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                if ln.strip():
                    lines.append(ln.rstrip("\r\n"))
                if len(lines) >= sample:
                    break
    except Exception:
        return "raw", 0.01, {}
    adapter, conf, scores = detect_adapter(lines)
    return adapter.name, round(conf, 3), scores


def parse_line(line: str, adapter_name: str = "") -> Optional[dict]:
    """Parse one line with a named adapter (or auto-detect on this single line
    if none given). Convenience for callers that already know the format."""
    a = _BY_NAME.get(adapter_name) if adapter_name else None
    if a is None:
        a, _, _ = detect_adapter([line])
    ev = a.parse_line(line)
    if ev is None and adapter_name != "raw":
        ev = _BY_NAME["raw"].parse_line(line)
    return ev


# ── Family-module adapters (kernel / security / console) ─────────────────────────
# The long tail of named formats lives in the `adapters/` sub-package, one module
# per family, each subclassing LogAdapter and calling register_adapter() at import
# time. Importing the package here wires them into REGISTRY on load. This keeps the
# core file readable and lets families grow in parallel without merge collisions.
def _load_family_adapters() -> None:
    import importlib
    for modname in ("connectors.adapters", "adapters",
                    "python_backend.connectors.adapters"):
        try:
            importlib.import_module(modname)
            return
        except Exception:
            continue


_load_family_adapters()


# ═════════════════════════════════════════════════════════════════════════════════
#  COVERAGE + ROADMAP  (see docs/LOG_ADAPTERS.md for the full registry table)
# ═════════════════════════════════════════════════════════════════════════════════
# SHIPPED — the CORE 41 named text/XML adapters:
#   jsonl, windows_event, log4j, rust, dotnet, nlog, sharpemu, loguru, docker_cri,
#   syslog, haproxy, systemd, logcat, klog, go_zap, ruby, php_monolog, elixir,
#   redis, access_log, dev_access, nginx_error, apache_error, database (pg+mysql),
#   build (gcc/clang/rustc/msvc), test (pytest/go/cargo), envoy, kafka, ci,
#   sanitizer, auditd, cassandra, springboot, rabbitmq, hashicorp, rails, sqlite,
#   logfmt, python_logging, generic_ts, raw.
#
# PLUS the UNIVERSAL STRUCTURAL AUTO-NORMALIZER (`structural`, pinned just above
# `raw`): turns an UNRECOGNIZED line into a schema-valid event by recognizing the
# common structural shapes (kernel ring-buffer, syslog 3164/5424, /dev/kmsg, ISO
# prefix, level-bracket/-prefix, logfmt, embedded-JSON, CLF) and inferring level
# from keywords otherwise. Capped ≤0.3 so it never outranks a named adapter, and
# floored >raw so an unknown log still lands as usable JSON.
#
# PLUS the single `jsonl` adapter transparently normalizes EVERY JSON-based
# logger via field-alias + numeric-level + $date/epoch coercion: pino, winston,
# bunyan, roarr, structlog, loguru-json, zap-json, slog-json, logrus-json,
# logback-json, log4j2-json, Serilog-json, NLog-json, GELF, MongoDB, journald
# `-o json`, Docker json-file, Caddy, Envoy access-json, OTLP-json.
#
# BATCH 1 — 34 family-module adapters in connectors/adapters/ (kernel/security/
# console), auto-registered on load:
#   console : ps5_orbis_klog, orbis_module_tag, orbis_fatal_trap, freebsd_messages
#   kernel  : linux_dmesg, linux_dmesg_ctime, linux_devkmsg, kernel_panic_oops,
#             netfilter_klog, esp32_panic, esp_idf_log, zephyr_log, zephyr_fatal,
#             uboot_boot_log, freertos_logging, macos_unified_log, rsyslog, ftrace
#   security: cef, leef, cisco_asa, cisco_ios, fortigate, checkpoint,
#             pfsense_filterlog, paloalto_panos, suricata_fast, snort_fast,
#             sshd_auth, dnsmasq, isc_dhcpd, zeek_tsv, ossec_wazuh_alerts,
#             selinux_avc
# => 41 core + 34 batch-1 = 75 named adapters (+ the structural normalizer).
#
# BATCH 2 — 37 more family-module adapters in connectors/adapters/, auto-loaded:
#   database : mssql_errorlog, oracle_alert, db2_diag, clickhouse, cockroachdb,
#              scylladb, aerospike, elastic_stack, couchbase_memcached
#   webserver: tomcat_catalina, gunicorn_error, express_morgan_dev, glassfish_odl,
#              weblogic, w3c_access (IIS/CloudFront/WebLogic-ELF)
#   cloud    : aws_alb, aws_s3_access, aws_lambda_text, cloud_init, etcd_capnslog,
#              gcf_text
#   runtime  : jul_2line, ros (ROS1+ROS2), android_crash, android_anr, celery, uwsgi
#   cicd     : jenkins, junit_xml, gdb_backtrace, ansible, azure_devops
#   messaging: zookeeper, nats, mosquitto, emqx, activemq, artemis
# (Formats already served by an existing adapter or the `jsonl` super-adapter were
#  deliberately skipped: gunicorn/morgan-combined & varnishncsa → access_log,
#  django runserver → dev_access, laravel → php_monolog, go slog-text → logfmt,
#  ASP.NET simple console → dotnet, all glog variants → klog, every JSON cloud/DB
#  log → jsonl.)
# => 75 + 37 = 112 named adapters (+ structural normalizer + raw = 114 registry).
#
# Also fixed 3 batch-1 detection gaps (each dropped to `structural` on its own
# catalog sample_line): orbis_fatal_trap (multi-line trap block now detected via
# a newline-aware detector), zeek_tsv (now accepts the space-rendered row form,
# not only TAB), and the Suricata fast-alert catalog format (served by the
# canonical `suricata_fast` adapter, which beats `snort_fast` via the 4-digit year).
#
# BATCH 3 — 53 more family-module adapters in connectors/adapters/, auto-loaded:
#   devtools     : python_traceback, node_stack, strace, cargo_build, vite_build,
#                  webpack_stats, npm_debug, jsonrpc_wire (LSP+DAP), helm_debug
#   observability: fluentbit, fluentd, telegraf, rust_tracing (vector/qdrant),
#                  datadog_agent, fail2ban, airflow_task, structlog_console,
#                  heroku_router
#   network      : bind_named, unbound, coredns, squid_access, squid_cache,
#                  openvpn, cisco_iosxr, mikrotik_routeros, f5_bigip_ltm,
#                  citrix_netscaler
#   os_platform  : windows_evtx_text, windows_wer, apt_history, dpkg_log,
#                  dnf_log, pacman_alpm, sssd_debug
#   virt         : openstack_oslo (default+context+exception forms),
#                  esxi_vmkernel, vmware_vmacore, proxmox_task
#   bigdata/HPC  : spark_log4j, hdfs_log4j, pingcap_unified (TiDB/Milvus),
#                  trino_airlift, slurm (ctld/d/stepd), pbs_torque, openmpi_tag
#   webserver    : websphere (traditional+Liberty), glassfish_ulf, jetty
#   database     : sap_hana, arangodb
#   messaging    : asterisk, freeswitch
# PLUS 6 batch-3 gap fixes routing 8 more catalog formats to existing adapters:
#   hashicorp (±HHMM hclog offsets → consul/terraform), log4j (log4j2
#   ISO8601_OFFSET ts → pulsar), dnsmasq (Pi-hole gravity/blacklist verbs),
#   sanitizer (multi-line valgrind report blocks), logcat (`-v time` + pidless
#   tag forms), ansible (log_path "ts p= u= n= | body" file form).
# => 112 + 53 = 165 named adapters (+ structural + raw = 167 registry).
#
# BATCH 4 — 50 more family-module adapters in connectors/adapters/, auto-loaded:
#   mainframe (NEW): zos_syslog, jes2_hasp (3 JES2 forms), mvs_message
#                    (JESYSMSG + Db2/MQ/IMS/VTAM/EZZ/HZS/CEE/NetView/TopSecret
#                    bare message-id streams), cics_dfh, racf_ich408i,
#                    racf_irradu00 (SMF-80 unload flat records), ibmi_joblog,
#                    ibmi_qhst, aix_errpt (summary+detail), solaris_fma
#                    (fmdump/fmadm), solaris_smf
#   telecom (NEW)  : sip_raw (RFC 3261), asterisk_ami, asterisk_cdr,
#                    asterisk_queue, asterisk_siptrace, pjsip_lib, hl7v2 (ER7),
#                    astm_lis2, mirth_connect
#   backup (NEW)   : veeam_vbr, commvault_log, netbackup_legacy,
#                    spectrum_protect (ANS/ANR/ANE — also z/OS server actlog),
#                    bacula_bareos, pgbackrest
#   bigdata/HPC    : lsf_job, gromacs_md, lammps_thermo, namd_log, mpi_rank
#                    (mpiexec -prepend-rank / srun --label, capped 0.65)
#   os_platform    : windows_dns_debug, windows_dhcp_csv, powershell_transcript,
#                    windows_app_crash (EventID 1000), sysmon_text,
#                    windows_security_text, windows_eventdata_xml (all sysmon
#                    <EventData> fragment exports)
#   kernel/firmware: tfa_bootlog (uvicorn-shape guarded by firmware anchor),
#                    edk2_uefi, coreboot_cbmem (width-5 padded tags), esp32_rom_boot
#   security       : krb5kdc, ds389_access, libreswan_pluto
#   network        : frr (also Quagga), huawei_vrp
#   virt           : ovirt_engine, vdsm_log
#   messaging      : exim_mainlog (also exim-paniclog)
# PLUS 6 batch-4 gap fixes routing 6 more catalog formats to existing adapters:
#   php_monolog (multi-line Laravel envelope+trace, stack captured), rails
#   (Started→Completed request block merged into one event), linux_devkmsg
#   (" KEY=VALUE" continuation lines), zephyr_fatal (whole fault-dump block),
#   ftrace (function_graph gutter), oracle_alert ("ORA-nnnnn msg" without colon).
# Load-order note: backup loads BEFORE mainframe (spectrum_protect's ANS/ANR
# ids also fit the mvs_message grammar; earlier registration wins the tie).
# => 165 + 50 = 215 named adapters (+ structural + raw = 217 registry).
#
# BATCH 5 — 49 more family-module adapters in connectors/adapters/, auto-loaded
# (closes the high-priority TEXT/mixed/csv fallthrough: 71 → 18, and every
# remaining non-binary high-priority FORMAT now has a named owner; 96.0% of
# high-priority catalog samples route named):
#   database   : oracle_listener, oracle_audit (.aud multi-line record),
#                sybase_ase, informix (vocab-gated HH:MM:SS␣␣ shape), firebird
#                (host+ctime header + tab body), neo4j_query, neo4j (query
#                registered BEFORE plain — a query line also fits the plain
#                grammar; also owns neo4j-log4j/debug-log-categories)
#   virt/cloud : esxi_vobd, nutanix_genesis (",mmmZ" UTC fingerprint), ovs_vlog,
#                swift_proxy (tx-id anchored), eventlet_wsgi, pveproxy (numeric
#                DD/MM/YYYY + user@realm CLF), aws_elb_classic, aws_vpc_flow
#                (default v2 + custom formats), azure_functions
#   config/CI  : chef_client, puppet_agent, puppet_apply, terraform_plan,
#                pip_install, pm2, phoenix (Elixir Logger + metadata pairs)
#   profiling (NEW module): flamegraph_folded (also async-profiler/austin/
#                py-spy collapsed), perf_script, bpftrace (hist + stack maps),
#                gdb_mi
#   crash dumps: android_tombstone, android_bugreport, apple_ips (.ips
#                header-JSON + body), + seed dumps replay_seed/lightning_seed
#   security   : modsecurity (serial audit boundaries), f5_asm, pflog_text,
#                dhcpd_leases
#   network    : snmptrapd, tailscaled (component-token gated), netapp_ems,
#                wireguard (before linux_dmesg — rides the dmesg envelope)
#   messaging  : ibm_mq (AMQERR header + AMQnnnnX code), exchange_tracking (csv)
#   mainframe  : ibmi_qsysopr, ibmi_qaudjrn
#   industrial (NEW module): ignition_wrapper (Tanuki), osisoft_pi (2-line
#                records), wonderware (aaLog k="v"), tpm2_eventlog (YAML),
#                conpot (ICS honeypot)
# PLUS 3 batch-5 gap/latent-bug fixes to existing adapters:
#   w3c_access (accepts literal-\t CloudFront rows — escaped shipping — and a
#   REAL bug: sc-bytes could be misread as sc-status; the status column is now
#   anchored after the cs-method column), android_anr (literal-\n + escaped-
#   quote ANR blocks via split_any/block_ratio, whole-block → one error event),
#   uwsgi (verified: the catalog's TEMPLATE entry is served by the existing
#   adapter — inline real sample routes correctly).
# => 215 + 49 = 264 named adapters (+ structural + raw = 266 registry).
#
# NEXT (from docs/log_catalog_master.json — 18 high-priority entries still fall
# through, and NONE of them needs another line adapter):
#   • 15 BINARY formats → need *source readers* in log_sources.py (decode →
#     dicts → jsonl): NetFlow v5/v9, IPFIX, sFlow v5, pflog pcap (DLT 117),
#     utmp/wtmp/btmp, windows-security-evtx, SMF (record header, type 30 ×2,
#     type 80 ×2, 100-102 DB2, 110 CICS), ibmi-cpyaudjrne-outfile
#   • 2 TEMPLATE catalog entries (swift-proxy-access, uwsgi-request-log) whose
#     REAL formats route to swift_proxy/uwsgi — verified inline in the batch-5
#     suite; only the template-description sample_line can't detect (correct)
#   • 1 literal-\n NDJSON sample (Elastic APM intake) — real NDJSON files
#     already route to jsonl line-by-line; a stream splitter belongs with the
#     source-reader work
# For BINARY/streamed telemetry (OTLP gRPC, Windows ETW live, perf, NetFlow/
# IPFIX/sFlow, EVTX/utmp/SMF binary, pflog pcap, ibmi-cpyaudjrne outfiles),
# add a *source reader* in log_sources.py (decode → dicts) and feed them
# through the jsonl adapter — everything converges on the one unified schema.
#
# BATCH 6 — 60 more family-module adapters (opens the MEDIUM tier: medium
# non-binary fallthrough 308 → 223; medium named coverage 256 → 341 of 564):
#   devtools   : gradle_build, gradle_daemon, yarn_berry, yarn_classic, pnpm,
#                uv, idea_log (IntelliJ/Android Studio), jupyter, teamcity
#                (##teamcity svc msgs), git_trace (GIT_TRACE/PACKET/TRACE2
#                human), esbuild, nix_json ('@nix ' envelope)
#   runtime    : aiohttp_access, tornado, puma, sidekiq, lograge, nestjs,
#                ruby_backtrace, aspnet_systemd (<N>Category[eid]), glog4
#                (full-year glog: Doris/StarRocks/Nebula/Typesense), spdlog,
#                zerolog (Go console: cloudflared &co)
#   android (NEW module): logcat_long (-v long 2-line), art_gc,
#                android_instrumentation (am instrument -r), android_dropbox
#   apple (NEW module): macos_log_compact (log show --style compact),
#                macos_asl (<Notice>: ASL CLI), macos_syslog_style (system/
#                install.log incl. hour-only tz), apple_crash_legacy (.crash;
#                also owns spindump/hang reports at WARN)
#   network    : powerdns (auth+recursor), kea_dhcp, keepalived, busybox_syslog
#                (BusyBox syslogd + OpenWrt logread fac.pri), freeradius,
#                dnsmasq_leases
#   observability: nagios, icinga2, zabbix, collectd, netdata, graylog, splunkd
#   cloud      : aws_nlb (tls 2.0 rows), flyio
#   database   : couchdb, ds389_errors
#   webserver  : glassfish_access (all-quoted CLF), wildfly_console
#   security   : falco, defender_detection (rendered 1116/1117 blocks),
#                windows_firewall (pfirewall W3C rows, before w3c_access)
#   os/config/backup/games/profiling/virt: journald_export, apt_term,
#                salt_daemon, rclone, unreal (UE_LOG), perf_stat, vcenter_vpxd
# PLUS 9 batch-6 gap fixes routing 12 more catalog formats to existing
# adapters: celery + ruby (multi-line detect; celery_worker/unicorn_stdout),
# logcat (year/usec/zone/uid threadtime variants), mikrotik_routeros (syslog-
# shipped + bare-HH:MM:SS topics — 3 formats), esxi_vmkernel (ESXi7 2-letter
# levels + 'vmkernel:' tag), weblogic (####-less stdout), ftrace (optional
# flags column = trace-cmd report), sanitizer (helgrind/DRD race phrasing),
# linux_dmesg (optional '<N>' priority prefix + multi-line detect — owns the
# ACPI excerpts, Android last_kmsg/ramoops, and raw qemu/guest consoles).
# Registration-order notes: macos_asl/busybox_syslog/keepalived before syslog;
# lograge/sidekiq/zerolog before logfmt (lograge also refuses at=/dyno= keys —
# heroku_router's); windows_firewall before w3c_access; aiohttp_access/
# glassfish_access before access_log.
# => 264 + 60 = 324 named adapters (+ structural + raw = 326 registry).
#
# BATCH 7 — 62 more family-module adapters (deepens the MEDIUM tier: medium
# non-binary fallthrough 224 → 157; medium named coverage 340 → 407 of 564 =
# 72.2%; plus 4 bonus low-tier formats):
#   bigdata/HPC+science: htcondor_ulog (ULOG ×2 catalog forms), grid_engine
#                (SGE/UGE pipe records ×2), pbs_accounting, pbs_epilogue
#                (Resources Used blocks ×2), slurm_sacct (sacct -p table +
#                scontrol k=v), lsf_daemon (mbatchd/sbatchd ctime+pid+level),
#                openmpi_orte (dashed-banner runtime errors), openfoam
#                (Solving-for residual blocks), amber_mdout (NSTEP records),
#                quantum_espresso (also owns the low-tier DFT SCF entry),
#                gem5 (vocab-gated lowercase info:/warn:/panic: prefixes)
#   cicd/devtools: packer_ui (==> builder steps), pulumi_cli (diff rows),
#                chef_doc (resource action lines), salt_state (ID:/Function:/
#                Result: blocks), git_trace2_perf (' | ' column format)
#   profiling  : callgrind (also owns the low-tier callgrind entry),
#                valgrind_xml, perf_report (# Overhead tables), strace_summary
#   apple      : os_signpost (log stream Signpost lines, category=event),
#                ios_syslog (proc(Library)[pid] <Notice>: — before macos_asl)
#   database   : aurora_audit_csv (16-digit µs epoch rows), redshift_connection
#                + redshift_useractivity (their 2 TEMPLATE catalog twins are
#                verified inline like batch-5's), greenplum_csv (pNNN/thNNN/
#                conNN fingerprint; TEMPLATE catalog sample verified inline),
#                vertica (<LEVEL> node log), foundationdb_xml (<Event Severity=)
#   security   : f5_apm (bare 0149xxxx:sev: errdefs), f5_dos_kv (semicolon k=v),
#                clamav_scan (FOUND lines + SCAN SUMMARY), defender_mplog
#   os_platform: powershell_classic_engine (HostName=/EngineVersion= k=v CRLF
#                blocks), windows_dotnet_crash (EventID 1026 → FATAL crash),
#                windows_app_hang (EventID 1002), powershell_scriptblock (4104)
#   mainframe  : zvm_cp (HCP/DMS message ids), jes3_dlog (dd MMM yyyy console)
#   backup     : amanda (result+program records), netbackup_vxul (V-nnn-nnn),
#                duplicati ([Level-Namespace-EventId] tags), percona_xtrabackup
#   telecom    : threecx ([CMnnnnnn] codes), asterisk_cli (vocab-gated --/==/>
#                verbose), asterisk_cel_csv (quoted CEL event rows)
#   network    : bird (<LEVEL> proto), nfdump (flow prints), sflowtool
#                (FLOW/CNTR rows), sonic_nos (frac-sec syslog + container#prog,
#                before syslog), pihole_ftl ([ts pid] LEVEL:), stunnel
#                (dotted date + LOGn[tid]), kea_leases_csv (header + rows)
#   messaging  : artemis_audit ([AUDIT] AMQ60xxxx), tai64n_multilog (@TAI64N —
#                also owns the low-tier dnscache/tinydns entries),
#                exchange_smtp_csv (before w3c_access: comma #Fields tie)
#   cloud/virt : azure_storage_analytics (1.0;ISO;op;auth;status), cloudfront_
#                realtime (epoch-first TSV, real AND literal \t), kinesis_agent,
#                vmware_python (VCSA pylog), openstack_instance ([instance: uuid])
#   industrial : winccoa (WCCxx (n), y.m.d, area, SEV rows), kepware (TSV events)
# PLUS 3 batch-7 gap fixes routing 3 more catalog formats to existing adapters:
#   rust_tracing (single-segment targets with tracing-fmt level padding →
#   meilisearch/index_scheduler), exim_mainlog (id-less REJECTLOG lines,
#   vocabulary-gated), hl7v2 (MLLP VT/FS-CR framing, raw bytes or literal \xNN).
# Registration-order notes: ios_syslog before macos_asl; sonic_nos before
# syslog; exchange_smtp_csv before w3c_access. Steal-check: 0 of the 68
# batch-7-won catalog samples had a previous NAMED owner (all came off the
# structural/generic_ts fallbacks).
# => 324 + 62 = 386 named adapters (+ structural + raw = 388 registry).
#
# BATCH 8 — 130 more family-module adapters (FINISHES the medium non-binary
# tail: medium named coverage 72.5% -> 92.7%, fallthrough 154 -> 41; low tier
# 36.3% -> 45.3%). A compact `RxAdapter` base + `vocab_detect` helper (in
# adapters/_common.py) package the common "regex + block-aware detect + emit"
# shape so a single-format adapter costs ~6 lines. New adapters, by family:
#   bigdata (science/HPC): ns3, mcnp, mc_iteration, stan_sampler, mpich_hydra,
#             geant4, matlab_diary, r_console, julia_logging, htcondor_daemon,
#             dask_ray
#   mainframe: jes3_iat, netview_netlog, jes2_jesjcl, zowe (apiml + zwe),
#             ibmi_dsplog, solaris_praudit, solaris_svcs (list+xv), sysv_sulog,
#             aix_auditpr
#   os_platform: ftp_xferlog, netlogon_debug, windows_rasclient,
#             windows_dhcpv6_csv, windows_ps_geteventlog, windows_setupapi
#   kernel  : lustre_kernel, qla2xxx, nfsd_kernel (before linux_dmesg), gpfs_mmfs,
#             segger_rtt, vxworks_logmsg, vxworks_exception, libmodbus_debug,
#             coreboot_timestamps, android_lk
#   network : nokia_sros + h3c_comware (before syslog), extreme_exos, nsd_log,
#             tinc_syslog, openvpn_mgmt, dnstap_text (verbose+quiet),
#             dnscrypt_query (tsv+ltsv), pppd_syslog, dhcpcd, ocserv, udhcpc,
#             isc_dhcrelay, coturn
#   telecom : freeswitch_esl, freeswitch_cdr_csv, freeswitch_cdr_xml,
#             freeswitch_sofia, sipp (message+error), ngrep_sip, audiocodes
#   database: mssql_agent, firebird_trace, oracle_sqlnet, progress_openedge,
#             barman, arango_audit
#   webserver: nutanix_prism, zeppelin_log4j, appdynamics, sumologic,
#             payara_notification, resin_jul
#   industrial: niagara_station, siemens_s7, open62541, rapidscada, scadalts_mango
#   profiling: gprof, py_spy_top, perf_sched
#   observability: monit_log, riemann_log
#   android : android_dumpsys, android_batterystats_csv, logcat_monotonic,
#             logcat_thread, logcat_process
#   batch8b (loaded LAST — one module of medium stragglers so its additions
#            never disturb an earlier module's before= ties): gazebo,
#            hf_transformers, freepbx_monolog, nsq_nsqd, rspamd, rsyslogd_internal,
#            syslog_ng_internal, systemd_networkd, networkmanager_dhcp,
#            hivemq_event, exabgp, cisco_meraki, proxmox_backup_task, squid_store,
#            squidguard, varnish (child+panic+backend-health), websphere_trace,
#            websphere_ffdc, softether, stepfunc_dnp3, yarn_rm_audit,
#            shibboleth_idp, brocade_fos, kea_legal, couchbase_ns,
#            windows_etw_tracefmt, vmware_vum, vespa, openvpn_status,
#            symantec_sep_csv, pmta_accounting, neptune_audit, factorytalk,
#            freeradius_detail, multipathd, rmf_postprocessor, x12_edi,
#            freebsd_dmesg_raw, uboot_structured, lio_target (before linux_dmesg),
#            cisco_anyconnect, aws_cdk, orthanc, hpe_3par, networker,
#            hpe_data_protector
# Steal-check: 0 of the 130-won catalog samples had a previous NAMED owner (all
# came off the structural/generic_ts fallbacks); 0 regressions across all 1430
# catalog entries.
# => 386 + 130 = 516 named adapters (+ structural + raw = 518 registry).
#
# BATCH 9 — 133 more adapters in ONE module (adapters/batch9.py) loaded LAST of
# all, FINISHING the LOW non-binary tail: low named coverage 45.0% -> 83.3%
# (whole-block fallthrough 198 -> 60), overall catalog 87.8% named. Because
# detect_adapter breaks ties by REGISTRY order and batch9 loads last, an adapter
# here can only ever GAIN a sample that previously fell to structural/generic_ts
# — never steal a named one (verified: 138 gains, 0 steals, 0 regressions over
# all 1430 catalog entries). New adapters, by family:
#   science/HPC: gaussian_scf, vasp_oszicar, cp2k, openmc
#   debug/profile: gdb_internal, lldb_log, perf_annotate, strace_pid_prefix
#   build/dev: homebrew, poetry, rollup, test_kitchen, vagrant, capistrano,
#             buildkite_job, travis_markers, packer_machine
#   datastore: questdb, orientdb
#   net/proxy/vpn: accel_ppp, cacti_poller, checkmk_cmc, checkpoint_lea,
#             cisco_meraki_flow, dnsdist_verbose, exim_msglog, frp, graphite_carbon,
#             heimdal_kdc, juju_debug, kadmind, knot_dns, knot_resolver, munin,
#             msmtp, netbird, opendnp3, pritunl, racoon, socat, tacacs_accounting,
#             v2ray, xl2tpd, dynatrace_oneagent, openconnect, openvpn_as, wg_quick,
#             wireguard_windows
#   security: clamav, file_integrity (AIDE+Tripwire), simplesamlphp,
#             pingfederate_audit, cas_audit, sophos_sav, mcafee_ens,
#             vcsa_applmgmt_audit, globalprotect_pangps
#   cloud/aws: cfn_init, aws_global_accelerator_flow, aws_transit_gateway_flow,
#             route53_query, openwhisk_activation, rds_mariadb_audit
#   backup: backuppc, restic_text, rsnapshot, arcserve_udp, veritas_bex
#   os: hpux_rc, hpux_shutdownlog, macos_fsck, macos_wifi, macos_coresimulator,
#             macos_mdm, macos_install_history, opendirectoryd, lastcomm
#   firmware/rtos: seabios, rpi_vc_firmware, mbed_os_error, contiki_ng, nordic_nrf,
#             threadx, ti_rtos, switch_diag_crash, uefi_secureboot, qnx_slog2
#   industrial/OT: ge_ifix_alarm, veeder_root_atg, lib60870, opc_ua_trace,
#             openplc_runtime
#   telephony: jitsi_jvb, yate, yealink_syslog, oracle_sbc_sipmsg, pulsar_function,
#             mailman, px4_mavlink
#   middleware/vmware: octopus_server, octopus_task, ovirt_jboss_boot, iris_main,
#             iris_messages, loginsight_ls, vcenter_vpxd_legacy, vmware_section,
#             qpid_dispatch, habitat_sup
#   storage/mail: dell_unity, solaris_fmadump (distinct from mainframe.solaris_fma),
#             hmailserver, procmail, oracle_messaging_mail
#   monitoring/ids: sflowtool_kv (distinct from network.sflowtool CSV), suricata_stats,
#             varnishstat, snort_full, oracle_adrci, infoblox_dns
#   legacy: acf2_report, zvse_console, tru64_evm, tru64_uerf, openldap_auditlog
#   misc: simgrid, samba_winbind, dicom_dcmdump, hl7v2_batch, activemq_audit,
#             acronis, azure_mars, watchguard_firebox
#   (before="syslog": cisco_meraki_flow, kadmind, yealink_syslog, watchguard_firebox,
#            infoblox_dns, simplesamlphp, macos_coresimulator, mailman)
# The 60 LOW entries still on fallbacks are ~26 doc/TEMPLATE/protobuf/JSON samples
# (real format covered elsewhere or uncapturable) + ~34 genuinely-hard cases
# (positional CSV needing a sidecar header, glog variants that would collide,
# freeform RTOS/debugger console spew best treated as raw, demux/enrichment
# pseudo-formats).
# => historically 516 + 133 = 649 named; the live REGISTRY has since grown to
# 658 entries (656 named + structural + raw). len(REGISTRY) is authoritative.
#
# NEXT: the ~41 medium non-binary entries still on the fallbacks are almost all
# TEMPLATE / documentation-only catalog samples (a column DESCRIPTION or a SQL
# SELECT, not a real log line) whose real formats already route to a named
# adapter — redshift connection/useractivity (pipe), greenplum-csv (docs),
# ibmi-*-sql (SELECT statements), snowflake/py-spy-speedscope (→ jsonl),
# cisco-cucm-cdr / swift-storage template rows, acf2/erep/racf-irrdbu00 report
# descriptions, rfc3881 audit XML. The remaining real LOW-tier long tail
# (embedded RTOS consoles, more vendor-network/storage kernel logs, science
# REPLs) is the next line-adapter target; BINARY formats stay with the
# source-reader plan (log_sources.py: NetFlow/IPFIX/sFlow, EVTX/utmp/SMF, pflog
# pcap, ibmi-cpyaudjrne outfiles → decode to dicts → jsonl).
#
# BINARY SOURCE-READER BATCH (connectors/readers.py — the mechanism above,
# executed): 8 pure-stdlib struct readers registered in READER_REGISTRY,
# covering 9 of the catalog's 68 structure=="binary" entries:
#   utmp (utmp-wtmp-btmp + utmp_wtmp_last, glibc 384-byte records; btmp→WARN),
#   lastlog (lastlog-binary, 292-byte per-UID), faillock (faillock-records,
#   linux-pam 64-byte struct tally), wtmpdb (wtmpdb-sqlite: wtmp AND lastlog2
#   tables via stdlib sqlite3), netflow_v5 (netflow-v5-datagram, 24+N×48 BE),
#   pcap (pflog-pcap-dlt117 at container level + any classic pcap, all 4
#   magics), mrt (mrt-bgp-dump at record level, RFC 6396), unified2
#   (snort-unified2, full type-7/104 IDS-event decode).
# Readers emit unified-schema dicts → the jsonl adapter's native passthrough,
# so binary telemetry lands on the SAME timeline as the 656 named text adapters.
# Suite: connectors/test_source_readers.py (synthesized struct.pack samples,
# truncation/garbage never-raise proofs, merge-path normalization).
# The remaining 59 binary entries are DEFERRED with reasons + add-paths in the
# readers.py module docstring: 7 template/stateful network formats (IPFIX,
# NetFlow v9, sFlow v5, BMP, dnstap, SNMP-trap BER, HEP3), EVTX binary-XML,
# 19 z/OS SMF + 4 IMS/CICS EBCDIC record families, 7 IBM i journal/outfiles,
# and 21 external-tool-first containers (perf.data/JFR/ETL/tracev3/ULog/…)
# whose own catalog notes route ingestion through the companion tool's text
# rendering (already covered by existing text adapters).
