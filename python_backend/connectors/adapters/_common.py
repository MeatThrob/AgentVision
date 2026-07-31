"""
Shared imports + helpers for the family adapter modules.
Pure standard library; works whether the package is imported as
`connectors.adapters` (bridge/tests) or `adapters` (connectors dir on path).
"""
from __future__ import annotations

import re
import json
from datetime import datetime, timezone
from typing import Optional

# Re-export the core framework symbols the family modules build on. Try every
# import path the project uses so this works in all launch contexts.
_la = None
for _name in ("connectors.log_adapters", "log_adapters",
              "python_backend.connectors.log_adapters"):
    try:
        _la = __import__(_name, fromlist=["*"])
        break
    except Exception:
        continue
if _la is None:  # pragma: no cover - impossible in a wired project
    raise ImportError("cannot locate log_adapters from family adapter module")

LogAdapter = _la.LogAdapter
register_adapter = _la.register_adapter
parse_timestamp = _la.parse_timestamp
canonical_level = _la.canonical_level
_SYSLOG_SEVERITY = _la._SYSLOG_SEVERITY
_MONTHS = _la._MONTHS
_to_ms = _la._to_ms


def bsd_year_ts(text: str) -> Optional[float]:
    """Parse a 'Mon DD YYYY HH:MM:SS' timestamp (Cisco ASA style) → epoch ms,
    else fall back to the shared parse_timestamp (handles the yearless syslog
    and ISO forms). Naive → local time (same-machine correlation)."""
    m = re.match(r"([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{4})\s+"
                 r"(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?", text or "")
    if m and m.group(1) in _MONTHS:
        mon, dy, yr, hh, mm, ss, frac = m.groups()
        micro = int((frac or "0").ljust(6, "0")[:6])
        try:
            return _to_ms(datetime(int(yr), _MONTHS[mon], int(dy), int(hh),
                                   int(mm), int(ss), micro))
        except ValueError:
            return None
    return parse_timestamp(text or "")


# a compact "count matching lines / seen non-blank lines" detector body used by
# most adapters; pass a predicate(line)->bool.
def ratio_detect(sample_lines, predicate) -> float:
    hits = seen = 0
    for ln in sample_lines:
        s = ln.strip() if isinstance(ln, str) else ""
        if not s:
            continue
        seen += 1
        try:
            if predicate(ln):
                hits += 1
        except Exception:
            pass
    return (hits / seen) if seen else 0.0


def multiline_ratio_detect(sample_lines, predicate, threshold: float = 0.5) -> float:
    """Like ratio_detect, but for MULTI-LINE record formats (crash blocks, DB
    diagnostic records, JUL 2-line records). A caller — or the format catalog —
    may hand the whole block as ONE element with embedded newlines, or a line at
    a time. Count an element as a hit when at least `threshold` of its non-blank
    sub-lines satisfy `predicate`, so detect_adapter([whole_block]) still
    resolves to the intended adapter instead of falling to the structural
    normalizer."""
    def ok(ln):
        subs = [x for x in str(ln).splitlines() if x.strip()]
        if not subs:
            return False
        hits = sum(1 for x in subs if predicate(x))
        return hits >= 1 and (hits / len(subs)) >= threshold
    return ratio_detect(sample_lines, ok)


# ── Literal-\n aware block splitting (BATCH 4) ────────────────────────────────
# Escaped log shipping (and several catalog samples) carry the TWO-CHAR "\n" /
# "\r\n" sequences instead of real newlines. Splitting on both lets a block
# adapter recognize the record either way.
_LITERAL_NL = re.compile(r"\\r\\n|\\n")


def split_any(text) -> list:
    """Split text into logical lines on real newlines AND literal \\n / \\r\\n
    two-char escape sequences. Blank pieces are dropped."""
    out = []
    for part in str(text).splitlines():
        for piece in _LITERAL_NL.split(part):
            if piece.strip():
                out.append(piece)
    return out


def block_ratio(element, predicate, threshold: float = 0.5) -> bool:
    """True when ≥ threshold of an element's logical lines (split_any) satisfy
    `predicate`. The multi-line building block for detect() bodies."""
    subs = split_any(element)
    if not subs:
        return False
    hits = 0
    for x in subs:
        try:
            if predicate(x):
                hits += 1
        except Exception:
            pass
    return hits >= 1 and (hits / len(subs)) >= threshold


# ── Assorted timestamp shapes used by the batch-4 families ────────────────────

def two_digit_year(yy) -> int:
    """00-69 → 2000s, 70-99 → 1900s (the POSIX pivot)."""
    yy = int(yy)
    return 2000 + yy if yy < 70 else 1900 + yy


def mk_ts(yr, mo, dy, hh, mi, ss, micro=0) -> Optional[float]:
    """Build an epoch-ms from numeric parts; None instead of raising.
    Naive → local time (same-machine correlation, like the core parser)."""
    try:
        return _to_ms(datetime(int(yr), int(mo), int(dy), int(hh), int(mi),
                               int(ss), int(micro)))
    except Exception:
        return None


def compact_ts(s: str) -> Optional[float]:
    """YYYYMMDDHHMMSS(.frac)(±ZZZZ) — HL7 v2 TS / ASTM / PowerShell transcript
    'Start time:' — → epoch ms (offset honored when present, else local)."""
    m = re.match(r"^\s*(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})?(?:\.(\d+))?"
                 r"\s*([+-]\d{4})?", s or "")
    if not m:
        return None
    yr, mo, dy, hh, mi, ss, frac, tz = m.groups()
    micro = int((frac or "0").ljust(6, "0")[:6])
    try:
        if tz:
            from datetime import timedelta, timezone as _tz
            sign = 1 if tz[0] == "+" else -1
            tzinfo = _tz(sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])))
            return datetime(int(yr), int(mo), int(dy), int(hh), int(mi),
                            int(ss or 0), micro, tzinfo).timestamp() * 1000.0
        return _to_ms(datetime(int(yr), int(mo), int(dy), int(hh), int(mi),
                               int(ss or 0), micro))
    except Exception:
        return None


def us_date_ts(date_str: str, time_str: str) -> Optional[float]:
    """US 'MM/DD/YY(YY)' + 'HH:MM:SS(.frac| AM/PM)' pair → epoch ms."""
    dm = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$", date_str or "")
    tm = re.match(r"^\s*(\d{1,2}):(\d{2}):(\d{2})(?:[.,](\d+))?\s*([AP]M)?\s*$",
                  time_str or "", re.IGNORECASE)
    if not dm or not tm:
        return None
    mo, dy, yr = dm.groups()
    hh, mi, ss, frac, ampm = tm.groups()
    yr = two_digit_year(yr) if len(yr) == 2 else int(yr)
    hh = int(hh)
    if ampm:
        if ampm.upper() == "PM" and hh != 12:
            hh += 12
        elif ampm.upper() == "AM" and hh == 12:
            hh = 0
    micro = int((frac or "0").ljust(6, "0")[:6])
    return mk_ts(yr, mo, dy, hh, mi, ss, micro)


# ── Compact regex-adapter base (BATCH 8) ──────────────────────────────────────
# Most single-shape formats need only: a regex with optional named groups
# (ts/level/source/msg), a detect() that is block-aware, and the standard
# parse. RxAdapter packages that so a format costs ~6 lines. Multi-line record
# formats set match_scope="first" (only the first logical line must match —
# crash blocks, report records); line formats keep the default "lines"
# (block_ratio over every logical line).

class RxAdapter(LogAdapter):
    _RE: "re.Pattern" = None            # subclasses MUST set
    match_scope = "lines"               # "lines" | "first"
    default_level = ""                  # used when no <level> group / empty
    default_source = ""                 # used when no <source> group / empty
    fixed_category = ""                 # e.g. "event" for access-style logs
    _LVL_MAP: dict = {}                 # optional token → canonical translation

    def _hit(self, x: str) -> bool:
        return bool(self._RE.match(x.strip()))

    def _m1(self, line):
        """First matching logical sub-line's regex match object, or None.
        Robust for parse-overrides that read a named group off the matched line
        even when the WHOLE multi-line block is handed in as one string (the
        naive self._RE.match(line) would miss it and crash on .group)."""
        for x in (split_any(line) or [str(line)]):
            m = self._RE.match(x.strip())
            if m:
                return m
        return None

    def detect(self, sample_lines):
        if self.match_scope == "first":
            def ok(el):
                subs = split_any(el)
                return bool(subs) and self._hit(subs[0])
            return ratio_detect(sample_lines, ok)
        return ratio_detect(sample_lines,
                            lambda el: block_ratio(el, self._hit))

    # subclass hooks
    def _ts(self, g: dict) -> Optional[float]:
        ts = g.get("ts")
        return parse_timestamp(ts) if ts else None

    def _fields(self, g: dict, line: str) -> Optional[dict]:
        return None

    def _level(self, g: dict, line: str) -> str:
        lvl = g.get("level") or self.default_level
        return self._LVL_MAP.get(lvl, self._LVL_MAP.get(str(lvl).lower(), lvl)) \
            if self._LVL_MAP else lvl

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        subs = split_any(s) or [s]
        m = None
        for x in subs:                       # first matching logical line
            m = self._RE.match(x.strip())
            if m:
                break
        if not m:
            return None
        g = m.groupdict()
        msg = g.get("msg")
        if msg is None or msg == "":
            msg = (m.string or "").strip()
        fields = self._fields(g, line)
        if len(subs) > 1:
            fields = dict(fields or {})
            fields.setdefault("block_lines", len(subs))
        return self._event(level=self._level(g, line) or "", message=msg,
                           source=(g.get("source") or self.default_source
                                   or self.name),
                           ts_ms=self._ts(g), category=self.fixed_category,
                           fields=fields, raw=line)


def vocab_detect(sample_lines, predicate, cap: float = 0.85) -> float:
    """ratio_detect for VOCABULARY-anchored adapters (no strict line grammar):
    score is capped below 1.0 so a strict-grammar adapter always outranks a
    vocabulary match on the same sample."""
    return min(cap, ratio_detect(sample_lines, predicate))
