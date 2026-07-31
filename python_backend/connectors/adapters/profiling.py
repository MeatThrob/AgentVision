"""
Profiling / low-level-debug output adapters (BATCH 5)
================================================================================
Text emitted by profilers and debugger wire protocols:

  flamegraph_folded : Brendan Gregg folded stacks (stackcollapse-*.pl output)
                      AND async-profiler `-o collapsed` — `f;g;leaf COUNT`
  perf_script       : `perf script` stack samples — header line + indented
                      `HEXADDR symbol+0xoff (dso)` frames
  bpftrace          : bpftrace map output — @name hist/lhist buckets and
                      @[ stack ]: COUNT aggregations
  gdb_mi            : GDB/MI machine interface records (^done/*stopped/=event/
                      ~"console"/&"log"/(gdb))

All of these are BLOCK/record formats fed either line-by-line or as one
element with embedded (possibly literal-\\n) newlines, so every detect() is
split_any/block-aware like the batch-3/4 crash adapters.
"""
from __future__ import annotations

import re
from typing import Optional

from ._common import (LogAdapter, register_adapter, ratio_detect,
                      split_any, block_ratio)


# ── FlameGraph folded stacks (also async-profiler collapsed) ─────────────────
#   main;compute;inner_loop 89
#   start_thread;thread_native_entry;JavaMain;...;Main.hot 137
class FlamegraphFoldedAdapter(LogAdapter):
    name = "flamegraph_folded"
    language = "any"
    # frames joined by ';', one space, positive integer sample count. Frames
    # never contain '"' or '=' (guards against quoted text and k=v logs);
    # require ≥1 ';' so a plain "word 42" line can never match.
    _RE = re.compile(r'^[^;\s"=]\S*(?:;[^;"=]+)+ (?P<count>\d+)$')

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda ln: block_ratio(ln, lambda x: bool(self._RE.match(x.strip()))))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        stack, _, count = line.strip().rpartition(" ")
        frames = stack.split(";")
        return self._event(level="", message=f"{frames[-1]} × {count}",
                           source="flamegraph", category="event",
                           fields={"leaf": frames[-1], "root": frames[0],
                                   "depth": len(frames), "samples": int(count),
                                   "stack": stack}, raw=line)


# ── perf script (stack samples) ───────────────────────────────────────────────
#   gzip 83220 36556.175587: 241937 cycles:
#        55c83b5a2773 longest_match+0x103 (/usr/bin/gzip)
class PerfScriptAdapter(LogAdapter):
    name = "perf_script"
    language = "any"
    _HEAD = re.compile(
        r"^(?P<comm>\S+)\s+(?P<pid>\d+(?:/\d+)?)\s+(?:\[\d+\]\s+)?"
        r"(?P<time>\d+\.\d+):\s+(?P<period>\d+)\s+(?P<event>[\w\-:.]+):")
    _FRAME = re.compile(r"^\s+[0-9a-f]{4,16}\s+\S+\s+\(\S*\)\s*$")

    def _block_line(self, s: str) -> bool:
        return bool(self._HEAD.match(s.strip()) or self._FRAME.match(s))

    def detect(self, sample_lines):
        # require a sample header somewhere so bare hex tables can't match.
        def ok(el):
            subs = split_any(el)
            return (any(self._HEAD.match(x.strip()) for x in subs)
                    and block_ratio(el, self._block_line))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if len(subs) > 1:                      # whole sample block → one event
            hm = next((self._HEAD.match(x.strip()) for x in subs
                       if self._HEAD.match(x.strip())), None)
            if not hm:
                return None
            g = hm.groupdict()
            frames = [x.strip() for x in subs if self._FRAME.match(x)]
            return self._event(level="", message=f'{g["comm"]} {g["event"]} sample',
                               source=f'perf.{g["comm"]}', category="event",
                               fields={"pid": g["pid"], "perf_time": g["time"],
                                       "period": int(g["period"]),
                                       "event": g["event"],
                                       "frames": frames[:64]}, raw=line)
        m = self._HEAD.match(s.strip())
        if m:
            g = m.groupdict()
            return self._event(level="", message=f'{g["comm"]} {g["event"]} sample',
                               source=f'perf.{g["comm"]}', category="event",
                               fields={"pid": g["pid"], "perf_time": g["time"],
                                       "period": int(g["period"]),
                                       "event": g["event"]}, raw=line)
        if self._FRAME.match(s):
            return self._event(level="", message=s.strip(), source="perf",
                               category="event", fields={"stack_frame": True},
                               raw=line)
        return None


# ── bpftrace map output (hist / lhist / stack aggregations) ──────────────────
#   @bytes:
#   [4K, 8K)            24 |@@@@@@@@@@@@@@@@@@@@                    |
#   @[
#       __x64_sys_openat+0
#   ]: 20
class BpftraceAdapter(LogAdapter):
    name = "bpftrace"
    language = "any"
    _MAP_HEAD = re.compile(r'^@[\w"\[\], .:-]*:$')          # @name: / @[comm]:
    _BUCKET = re.compile(                                    # hist/lhist row
        r"^[\[(][^|]{1,40}[\])]\s+\d+\s+\|[@ ]*\|?$")
    _STACK_OPEN = re.compile(r"^@\w*\[\s*$")                 # @[  (stack key)
    _STACK_FRAME = re.compile(r"^\s+[\w.$:<>~-]+\+(?:0x)?[0-9a-f]+\s*$",
                              re.IGNORECASE)
    _STACK_CLOSE = re.compile(r"^\]:\s*\d+$")
    _SCALAR = re.compile(r"^@[\w]*\[[^\]]*\]:\s*\d+$")       # @map[key]: N

    def _block_line(self, s: str) -> bool:
        st = s.rstrip()
        return bool(self._MAP_HEAD.match(st.strip()) or self._BUCKET.match(st.strip())
                    or self._STACK_OPEN.match(st.strip()) or self._STACK_FRAME.match(st)
                    or self._STACK_CLOSE.match(st.strip()) or self._SCALAR.match(st.strip()))

    def detect(self, sample_lines):
        # require an @-anchor somewhere in the element (header/open/scalar) so
        # generic bracketed tables can never match on buckets alone.
        def ok(el):
            subs = split_any(el)
            anchored = any(x.strip().startswith("@") for x in subs)
            return anchored and block_ratio(el, self._block_line)
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if len(subs) > 1:                      # whole map block → one event
            if not any(x.strip().startswith("@") for x in subs):
                return None
            head = subs[0].strip()
            close = next((self._STACK_CLOSE.match(x.strip()) for x in subs
                          if self._STACK_CLOSE.match(x.strip())), None)
            buckets = [x.strip() for x in subs if self._BUCKET.match(x.strip())]
            frames = [x.strip() for x in subs if self._STACK_FRAME.match(x)]
            fields = {"map": head.rstrip(":")}
            if close:
                fields["count"] = int(close.group(0).split(":")[1])
                fields["frames"] = frames[:64]
                msg = f"stack × {fields['count']}"
            else:
                fields["buckets"] = len(buckets)
                msg = f"{head} histogram ({len(buckets)} buckets)"
            return self._event(level="", message=msg, source="bpftrace",
                               category="event", fields=fields, raw=line)
        st = s.strip()
        if not self._block_line(s):
            return None
        return self._event(level="", message=st, source="bpftrace",
                           category="event", raw=line)


# ── GDB/MI machine interface ──────────────────────────────────────────────────
#   ^done,bkpt={...}   *stopped,reason="breakpoint-hit",...   =thread-created
#   ~"console text\n"  &"log text\n"  (gdb)
class GdbMiAdapter(LogAdapter):
    name = "gdb_mi"
    language = "any"
    _RESULT = re.compile(r"^(?P<tok>\d*)\^(?P<cls>done|running|connected|error|exit)\b,?(?P<rest>.*)$")
    _EXEC = re.compile(r"^(?P<tok>\d*)\*(?P<cls>stopped|running)\b,?(?P<rest>.*)$")
    _NOTIFY = re.compile(r"^=(?P<cls>[a-z][a-z-]+)\b,?(?P<rest>.*)$")
    _STREAM = re.compile(r'^(?P<kind>[~@&])"(?P<text>.*)"?$')
    _PROMPT = re.compile(r"^\(gdb\)\s*$")

    def _block_line(self, s: str) -> bool:
        st = s.strip()
        return bool(self._RESULT.match(st) or self._EXEC.match(st)
                    or self._NOTIFY.match(st) or self._STREAM.match(st)
                    or self._PROMPT.match(st))

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda ln: block_ratio(ln, lambda x: self._block_line(x)))

    @staticmethod
    def _unescape(text: str) -> str:
        return (text.rstrip('"').replace("\\n", " ").replace('\\"', '"')
                .replace("\\t", " ").strip())

    def parse_line(self, line: str) -> Optional[dict]:
        st = line.rstrip("\r\n").strip()
        m = self._RESULT.match(st)
        if m:
            g = m.groupdict()
            level = "error" if g["cls"] == "error" else "info"
            return self._event(level=level, message=st, source="gdb.mi",
                               fields={"record": "result", "class": g["cls"],
                                       "token": g["tok"] or None}, raw=line)
        m = self._EXEC.match(st)
        if m:
            g = m.groupdict()
            reason = ""
            rm = re.search(r'reason="([^"]+)"', g["rest"])
            if rm:
                reason = rm.group(1)
            level = ("error" if "signal" in reason or "fatal" in reason
                     else "warn" if g["cls"] == "stopped" and "exited" in reason
                     else "info")
            return self._event(level=level, message=st, source="gdb.mi",
                               fields={"record": "exec-async", "class": g["cls"],
                                       "reason": reason or None}, raw=line)
        m = self._NOTIFY.match(st)
        if m:
            return self._event(level="", message=st, source="gdb.mi",
                               fields={"record": "notify-async",
                                       "class": m.group("cls")}, raw=line)
        m = self._STREAM.match(st)
        if m:
            kind = {"~": "console", "@": "target", "&": "log"}[m.group("kind")]
            level = "debug" if kind == "log" else ""
            return self._event(level=level, message=self._unescape(m.group("text")),
                               source=f"gdb.{kind}", fields={"record": "stream"},
                               raw=line)
        if self._PROMPT.match(st):
            return self._event(level="", message="(gdb)", source="gdb.mi",
                               category="debug", raw=line)
        return None


# ── Registration ──────────────────────────────────────────────────────────────
for _a in (FlamegraphFoldedAdapter(), PerfScriptAdapter(), BpftraceAdapter(),
           GdbMiAdapter()):
    register_adapter(_a)


# ═════════════════════════════════════════════════════════════════════════════
# BATCH 6
# ═════════════════════════════════════════════════════════════════════════════
from ._common import split_any as _split_any  # noqa: E402


# ── perf stat table ───────────────────────────────────────────────────────────
#        1,234,567      cycles          #    2.345 GHz
#        0.123456789 seconds time elapsed
class PerfStatAdapter(LogAdapter):
    name = "perf_stat"
    language = "linux"
    _ROW = re.compile(
        r"^\s*(?P<val>[\d,]+|\d+\.\d+|<not counted>|<not supported>)\s+"
        r"(?P<event>[\w:.\-/]+(?:\s[\w:.\-/]+)?)\s*(?:#\s*(?P<derived>.*))?$")
    _ELAPSED = re.compile(r"^\s*(?P<secs>\d+\.\d+)\s+seconds time elapsed")
    _HEAD = re.compile(r"^\s*Performance counter stats for ")

    def detect(self, sample_lines):
        def hit(el):
            subs = _split_any(el)
            if not subs:
                return False
            rows = sum(1 for x in subs
                       if (self._ROW.match(x) and ("," in x or "#" in x))
                       or self._ELAPSED.match(x) or self._HEAD.match(x))
            anchored = any(self._ELAPSED.match(x) or self._HEAD.match(x)
                           or re.match(r"^\s*[\d,]{5,}\s+[\w\-]+\s+#", x)
                           for x in subs)
            return anchored and rows / len(subs) >= 0.5
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        subs = _split_any(s)
        if len(subs) > 1:
            # whole table → one summary event with per-event counters
            counters = {}
            elapsed = None
            for x in subs:
                em = self._ELAPSED.match(x)
                if em:
                    elapsed = float(em.group("secs"))
                    continue
                rm = self._ROW.match(x)
                if rm and ("," in x or "#" in x):
                    val = rm.group("val").replace(",", "")
                    try:
                        counters[rm.group("event")] = float(val) if "." in val else int(val)
                    except ValueError:
                        counters[rm.group("event")] = rm.group("val")
            if not counters and elapsed is None:
                return None
            fields = {"counters": counters}
            if elapsed is not None:
                fields["elapsed_s"] = elapsed
            return self._event(level="info",
                               message=f"perf stat: {len(counters)} counters"
                                       + (f", {elapsed}s elapsed" if elapsed else ""),
                               source="perf_stat", fields=fields, raw=line)
        em = self._ELAPSED.match(s)
        if em:
            return self._event(level="info", message=s.strip(), source="perf_stat",
                               fields={"elapsed_s": float(em.group("secs"))}, raw=line)
        rm = self._ROW.match(s)
        if rm and ("," in s or "#" in s):
            fields = {"event": rm.group("event"),
                      "value": rm.group("val").replace(",", "")}
            if rm.group("derived"):
                fields["derived"] = rm.group("derived").strip()
            return self._event(level="info", message=s.strip(), source="perf_stat",
                               fields=fields, raw=line)
        return None


register_adapter(PerfStatAdapter())


# ═════════════════════════════════════════════════════════════════════════════
#  BATCH 7 — valgrind callgrind/XML, perf report, strace -c summary
# ═════════════════════════════════════════════════════════════════════════════


# ── Valgrind callgrind profile format ─────────────────────────────────────────
#   # callgrind format
#   events: Ir
#   fl=file.c / fn=main / cfn=work / calls=1 24 / "16 12000" cost rows
class CallgrindAdapter(LogAdapter):
    name = "callgrind"
    language = "any"
    _MAGIC = re.compile(r"^# callgrind format\s*$")
    _HDR = re.compile(r"^(version|creator|cmd|pid|part|desc|events|summary|totals):")
    _POS = re.compile(r"^(fl|fn|cfn|cfl|fi|fe|ob|cob|calls)=")

    def detect(self, sample_lines):
        def ok(el):
            subs = split_any(el)
            magic = any(self._MAGIC.match(x.strip()) for x in subs)
            hdr = sum(1 for x in subs if self._HDR.match(x.strip()))
            pos = sum(1 for x in subs if self._POS.match(x.strip()))
            return magic or (hdr >= 1 and pos >= 2) or pos >= 3
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        subs = split_any(line)
        if not subs:
            return None
        fields = {}
        fns = []
        for x in subs:
            s = x.strip()
            m = re.match(r"^(events|creator|cmd|pid)\s*:\s*(.*)$", s)
            if m:
                fields[m.group(1)] = m.group(2)
            m = re.match(r"^fn=(?:\(\d+\)\s*)?(.+)$", s)
            if m:
                fns.append(m.group(1))
        if not fields and not fns and not any(
                self._MAGIC.match(x.strip()) or self._POS.match(x.strip())
                for x in subs):
            return None
        if fns:
            fields["functions"] = fns[:20]
        msg = f'callgrind profile ({fields.get("events", "?")} events'
        msg += f', {len(fns)} functions)' if fns else ')'
        return self._event(level="info", message=msg, source="callgrind",
                           fields=fields, raw=line)


# ── Valgrind --xml=yes output ─────────────────────────────────────────────────
#   <valgrindoutput><protocolversion>4</protocolversion><tool>memcheck</tool>
#   <error><kind>InvalidWrite</kind><what>…</what>…</error>…</valgrindoutput>
class ValgrindXmlAdapter(LogAdapter):
    name = "valgrind_xml"
    language = "any"
    _TOOL = re.compile(r"<tool>([^<]+)</tool>")
    _KIND = re.compile(r"<kind>([^<]+)</kind>")
    _WHAT = re.compile(r"<what>([^<]+)</what>|<text>([^<]+)</text>")
    _FN = re.compile(r"<fn>([^<]+)</fn>")

    def _hit(self, el: str) -> bool:
        return ("<valgrindoutput" in el
                or ("<error>" in el and "<kind>" in el)
                or "<protocolversion>" in el)

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: self._hit(str(el)))

    def parse_line(self, line: str) -> Optional[dict]:
        s = str(line)
        if not self._hit(s):
            return None
        tool = self._TOOL.search(s)
        kind = self._KIND.search(s)
        what = self._WHAT.search(s)
        fields = {"tool": tool.group(1) if tool else None}
        if kind:
            fields["kind"] = kind.group(1)
        fn = self._FN.search(s)
        if fn:
            fields["function"] = fn.group(1)
        level = "error" if kind or "<error>" in s else "info"
        msg = (what.group(1) or what.group(2)) if what else \
            (f'valgrind {fields.get("kind") or "output"}')
        return self._event(level=level, message=msg,
                           source=f'valgrind.{fields.get("tool") or "xml"}',
                           fields=fields, raw=line)


# ── perf report --stdio ───────────────────────────────────────────────────────
#   # Overhead  Command  Shared Object      Symbol
#       41.20%  gzip     gzip               [.] longest_match
class PerfReportAdapter(LogAdapter):
    name = "perf_report"
    language = "any"
    _HDR = re.compile(r"^#\s*(?:Children\s+Self|Overhead)\b")
    _ROW = re.compile(
        r"^\s*(?P<pct>\d{1,3}\.\d{2})%\s+(?P<rest>\S.*)$")
    _SYM = re.compile(r"\[([.kgHu])\]\s+(?P<sym>.+)$")

    def detect(self, sample_lines):
        def ok(el):
            subs = split_any(el)
            hdr = any(self._HDR.match(x.strip()) for x in subs)
            rows = sum(1 for x in subs
                       if self._ROW.match(x) and self._SYM.search(x))
            return hdr or rows >= 2
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        subs = split_any(line)
        best = None
        for x in subs:
            m = self._ROW.match(x)
            if m:
                best = (m, x)
                break
        if best is None:
            if any(self._HDR.match(x.strip()) for x in subs):
                return self._event(level="info", message="perf report table",
                                   source="perf.report", raw=line)
            return None
        m, x = best
        fields = {"overhead_pct": float(m.group("pct"))}
        sm = self._SYM.search(x)
        if sm:
            fields["symbol"] = sm.group("sym").strip()
            fields["symbol_type"] = sm.group(1)
        toks = m.group("rest").split()
        if toks:
            fields["command"] = toks[0]
        return self._event(level="info", message=x.strip(), source="perf.report",
                           fields=fields, raw=line)


# ── strace -c summary table ───────────────────────────────────────────────────
#   % time     seconds  usecs/call     calls    errors syscall
#    40.00    0.001234          61        20         2 openat
class StraceSummaryAdapter(LogAdapter):
    name = "strace_summary"
    language = "any"
    _HDR = re.compile(r"^%\s*time\s+seconds\s+usecs/call\s+calls\s+errors\s+syscall\s*$")
    _ROW = re.compile(
        r"^\s*(?P<pct>\d{1,3}\.\d{2})\s+(?P<sec>\d+\.\d+)\s+(?P<upc>\d+)\s+"
        r"(?P<calls>\d+)\s+(?P<errs>\d*)\s+(?P<sc>[\w_]+)\s*$")

    def detect(self, sample_lines):
        def ok(el):
            subs = split_any(el)
            return (any(self._HDR.match(x.strip()) for x in subs)
                    or sum(1 for x in subs if self._ROW.match(x)) >= 3)
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        subs = split_any(line)
        rows = [self._ROW.match(x) for x in subs]
        rows = [m for m in rows if m]
        if not rows:
            if any(self._HDR.match(x.strip()) for x in subs):
                return self._event(level="info", message="strace -c summary",
                                   source="strace.summary", raw=line)
            return None
        top = rows[0].groupdict()
        errors = sum(int(m.group("errs") or 0) for m in rows)
        fields = {"top_syscall": top["sc"], "top_pct": float(top["pct"]),
                  "syscalls": len(rows), "errors": errors}
        level = "warn" if errors else "info"
        return self._event(level=level,
                           message=f'strace summary: top {top["sc"]} {top["pct"]}%'
                                   + (f", {errors} errors" if errors else ""),
                           source="strace.summary", fields=fields, raw=line)


for _a in (CallgrindAdapter(), ValgrindXmlAdapter(), PerfReportAdapter(),
           StraceSummaryAdapter()):
    register_adapter(_a)


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH 8 — gprof flat profile, py-spy top, perf sched timehist
# ══════════════════════════════════════════════════════════════════════════════
import re as _re8  # noqa: E402
from ._common import block_ratio as _block_ratio8, split_any as _sa8  # noqa: E402


# ── gprof flat profile ────────────────────────────────────────────────────────
#     %   cumulative   self ...  /  time seconds seconds calls ... name  /  33.34 …
class GprofAdapter(LogAdapter):
    name = "gprof"
    language = "any"
    _HDR = _re8.compile(r"cumulative\s+self")
    _FLAT = _re8.compile(r"^Flat profile:")
    _ROW = _re8.compile(
        r"^\s*(?P<pct>\d+\.\d+)\s+(?P<cum>\d+\.\d+)\s+(?P<self>\d+\.\d+)\s+"
        r"(?P<calls>\d+)?.*\s(?P<name>\S+)\s*$")

    def detect(self, sample_lines):
        def hit(el):
            subs = _sa8(el)
            return any(self._FLAT.match(x) or self._HDR.search(x) for x in subs)
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        for x in _sa8(s):
            m = self._ROW.match(x)
            if m:
                g = m.groupdict()
                return self._event(level="", message=f'{g["name"]} {g["pct"]}%',
                                   source="gprof",
                                   fields={"percent": float(g["pct"]),
                                           "self_seconds": float(g["self"]),
                                           "function": g["name"]},
                                   category="event", raw=line)
        return self._event(level="", message=s.strip(), source="gprof",
                           fields={"header": True}, raw=line)


# ── py-spy top ─────────────────────────────────────────────────────────────────
#   %Own   %Total  OwnTime  TotalTime  Function (filename:line)  /   45.00  90.00 …
class PySpyTopAdapter(LogAdapter):
    name = "py_spy_top"
    language = "python"
    _HDR = _re8.compile(r"^\s*%Own\s+%Total\s+OwnTime\s+TotalTime\s+Function")
    _ROW = _re8.compile(
        r"^\s*(?P<own>\d+\.\d+)\s+(?P<total>\d+\.\d+)\s+(?P<ot>[\d.]+m?s)\s+"
        r"(?P<tt>[\d.]+m?s)\s+(?P<func>.*\S)\s*$")

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda el: any(self._HDR.match(x) for x in _sa8(el)))

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        for x in _sa8(s):
            m = self._ROW.match(x)
            if m and not self._HDR.match(x):
                g = m.groupdict()
                return self._event(level="", message=g["func"], source="py-spy",
                                   fields={"own_pct": float(g["own"]),
                                           "total_pct": float(g["total"]),
                                           "own_time": g["ot"], "total_time": g["tt"]},
                                   category="event", raw=line)
        return self._event(level="", message=s.strip(), source="py-spy",
                           fields={"header": True}, raw=line)


# ── perf sched timehist ────────────────────────────────────────────────────────
#            time    cpu  task name  wait time  sch delay  run time
#      12345.678 [0000]  gzip[83220]     0.012      0.003     2.456
class PerfSchedAdapter(LogAdapter):
    name = "perf_sched"
    language = "any"
    _HDR = _re8.compile(r"\btask name\b.*\b(wait time|run time|sch delay)\b")
    _ROW = _re8.compile(
        r"^\s*(?P<ts>\d+\.\d+)\s+\[(?P<cpu>\d+)\]\s+(?P<task>\S+\[\d+\])\s+"
        r"(?P<wait>[\d.]+)\s+(?P<delay>[\d.]+)\s+(?P<run>[\d.]+)\s*$")

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda el: any(self._HDR.search(x) for x in _sa8(el)))

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        for x in _sa8(s):
            m = self._ROW.match(x)
            if m:
                g = m.groupdict()
                return self._event(level="", message=f'{g["task"]} run={g["run"]}ms',
                                   source="perf.sched",
                                   fields={"cpu": int(g["cpu"]), "task": g["task"],
                                           "wait_ms": float(g["wait"]),
                                           "sched_delay_ms": float(g["delay"]),
                                           "run_ms": float(g["run"]),
                                           "uptime": float(g["ts"])},
                                   category="event", raw=line)
        return self._event(level="", message=s.strip(), source="perf.sched",
                           fields={"header": True}, raw=line)


for _a in (GprofAdapter(), PySpyTopAdapter(), PerfSchedAdapter()):
    register_adapter(_a)
