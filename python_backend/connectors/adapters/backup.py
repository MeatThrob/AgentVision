"""
Enterprise backup-suite log adapters (BATCH 4)
================================================================================
Veeam Backup & Replication, Commvault, Veritas NetBackup legacy debug logs,
IBM Storage/Spectrum Protect (TSM) ANS/ANR message streams, Bacula/Bareos
daemon logs, and pgBackRest. Backup failures are the 03:00 pager classic —
'Fatal error:' / ANSnnnnE / VIOLATION-grade lines all land category=="error".

Load order note: this module registers BEFORE mainframe.py so the ANS/ANR
message-id grammar here outranks the generic MVS message-id adapter on a tie.
"""
from __future__ import annotations

import re
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp,
                      ratio_detect, block_ratio, _MONTHS,
                      two_digit_year, mk_ts, us_date_ts)


# ── Veeam Backup & Replication component logs ─────────────────────────────────
#   [21.07.2026 02:00:01] <01> Info         Job 'SQL-Nightly' has been started …
class VeeamVbrAdapter(LogAdapter):
    name = "veeam_vbr"
    language = "windows"
    _RE = re.compile(
        r"^\[(?P<d>\d{2})\.(?P<mo>\d{2})\.(?P<y>\d{4}) "
        r"(?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})(?:\.(?P<frac>\d+))?\]\s+"
        r"<(?P<tid>\d+)>\s+(?P<lvl>Info|Warning|Error)\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        micro = int((g["frac"] or "0").ljust(6, "0")[:6])
        return self._event(level=g["lvl"], message=g["msg"].strip(),
                           source="veeam",
                           ts_ms=mk_ts(g["y"], g["mo"], g["d"], g["hh"],
                                       g["mi"], g["ss"], micro),
                           fields={"thread": int(g["tid"])}, raw=line)


# ── Commvault process logs (CVD.log / JobManager.log — one shared layout) ─────
#   4056  1a2c  07/21 02:00:01 123456 CvStatsLogger::logStats() - Backup job …
class CommvaultAdapter(LogAdapter):
    name = "commvault_log"
    language = "any"
    _RE = re.compile(
        r"^(?P<pid>\d+)\s+(?P<tid>[0-9a-f]+)\s+"
        r"(?P<mo>\d{2})/(?P<d>\d{2}) (?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})\s+"
        r"(?P<job>\d+|#+|\S{1,10})\s+(?P<msg>\S.*)$")

    def detect(self, sample_lines):
        def hit(ln):
            m = self._RE.match(str(ln).strip())
            # require the module::function() marker so short numeric-prefixed
            # lines from other tools can't fire this adapter.
            return bool(m and re.search(r"\w+::\w+\(\)?", m.group("msg")))
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        from datetime import datetime
        yr = datetime.now().year        # yearless MM/DD — assume current year
        low = g["msg"].lower()
        level = ("error" if ("error" in low or "failed" in low or "failure" in low)
                 else "warn" if "warning" in low else "info")
        fn = re.match(r"^(?P<fn>\w+::\w+)", g["msg"])
        fields = {"pid": int(g["pid"]), "thread": g["tid"]}
        if g["job"].isdigit():
            fields["job_id"] = int(g["job"])
        return self._event(level=level, message=g["msg"].strip(),
                           source=f"commvault.{fn.group('fn')}" if fn else "commvault",
                           ts_ms=mk_ts(yr, g["mo"], g["d"], g["hh"], g["mi"], g["ss"]),
                           trace_id=str(fields.get("job_id")) if "job_id" in fields else None,
                           fields=fields, raw=line)


# ── Veritas NetBackup legacy process debug logs ───────────────────────────────
#   02:00:01.123 [12345] <2> bpbrm main: from client host1: TRV - object not …
class NetBackupLegacyAdapter(LogAdapter):
    name = "netbackup_legacy"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>\d{2}:\d{2}:\d{2}\.\d{3})\s+\[(?P<pid>\d+)\]\s+"
        r"<(?P<v>\d+)>\s+(?P<proc>\S+)\s+(?P<func>\S+):\s*(?P<msg>.*)$")
    # NBU verbosity: <2> info, <4> warn, <8>/<16> error/critical
    _VLVL = {2: "info", 4: "warn", 8: "error", 16: "error", 32: "error"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        v = int(g["v"])
        level = self._VLVL.get(v, "debug" if v < 2 else "info")
        return self._event(level=level, message=g["msg"].strip(),
                           source=f"netbackup.{g['proc']}",
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"pid": int(g["pid"]), "verbosity": v,
                                   "function": g["func"]}, raw=line)


# ── IBM Storage / Spectrum Protect / TSM client & server (ANS/ANR/ANE ids) ────
#   07/21/2026 02:00:01 ANS1898I ***** Processed    56,000 files *****
class SpectrumProtectAdapter(LogAdapter):
    name = "spectrum_protect"
    language = "any"
    _RE = re.compile(
        r"^(?:(?P<date>\d{2}/\d{2}/\d{2,4})\s+(?P<time>\d{2}:\d{2}:\d{2})\s+)?"
        r"(?P<msgid>AN[SRE]\d{4}(?P<suf>[IWES]))\s+(?P<msg>\S.*)$")
    _SUF = {"I": "info", "W": "warn", "E": "error", "S": "fatal"}

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda ln: block_ratio(ln, lambda x: bool(self._RE.match(x.strip()))))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        ts_ms = None
        if g.get("date"):
            ts_ms = us_date_ts(g["date"], g["time"])
        return self._event(level=self._SUF.get(g["suf"], "info"),
                           message=f"{g['msgid']} {g['msg'].strip()}",
                           source="spectrum_protect",
                           ts_ms=ts_ms, fields={"message_id": g["msgid"]},
                           raw=line)


# ── Bacula / Bareos daemon message log ────────────────────────────────────────
#   21-Jul 02:00 backup-dir JobId 123: Start Backup JobId 123, Job=NightlyEtc…
class BaculaBareosAdapter(LogAdapter):
    name = "bacula_bareos"
    language = "any"
    _RE = re.compile(
        r"^(?P<d>\d{2})-(?P<mon>[A-Z][a-z]{2})(?:-(?P<y>\d{4}))? "
        r"(?P<hh>\d{2}):(?P<mi>\d{2})(?::(?P<ss>\d{2}))?\s+"
        r"(?P<daemon>\S+)\s+JobId (?P<job>\d+):\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda ln: block_ratio(ln, lambda x: bool(self._RE.match(x.strip()))))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"].strip()
        low = msg.lower()
        level = ("fatal" if low.startswith("fatal error") or "fatal error:" in low
                 else "error" if low.startswith("error") or " error:" in low
                 else "warn" if low.startswith("warning") else "info")
        ts_ms = None
        if g["mon"] in _MONTHS:
            from datetime import datetime
            yr = int(g["y"]) if g.get("y") else datetime.now().year
            ts_ms = mk_ts(yr, _MONTHS[g["mon"]], g["d"], g["hh"], g["mi"],
                          g.get("ss") or 0)
        return self._event(level=level, message=msg,
                           source=f"bacula.{g['daemon']}", ts_ms=ts_ms,
                           trace_id=g["job"],
                           fields={"job_id": int(g["job"]),
                                   "daemon": g["daemon"]}, raw=line)


# ── pgBackRest ────────────────────────────────────────────────────────────────
#   P00   INFO: WAL segment 0000000100000000000000A1 successfully archived …
class PgBackRestAdapter(LogAdapter):
    name = "pgbackrest"
    language = "any"
    _RE = re.compile(
        r"^(?:(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+)?"
        r"P(?P<p>\d{2})\s+(?P<lvl>INFO|WARN|ERROR|DETAIL|DEBUG|TRACE):\s*"
        r"(?P<msg>.*)$")
    _LVL = {"DETAIL": "debug"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=self._LVL.get(g["lvl"], g["lvl"]),
                           message=g["msg"].strip(), source="pgbackrest",
                           ts_ms=parse_timestamp(g["ts"] or ""),
                           fields={"process": int(g["p"])}, raw=line)


# ── Registration ──────────────────────────────────────────────────────────────
register_adapter(VeeamVbrAdapter())
register_adapter(CommvaultAdapter())
register_adapter(NetBackupLegacyAdapter())
register_adapter(SpectrumProtectAdapter())
register_adapter(BaculaBareosAdapter())
register_adapter(PgBackRestAdapter())


# ═════════════════════════════════════════════════════════════════════════════
# BATCH 6
# ═════════════════════════════════════════════════════════════════════════════
from ._common import multiline_ratio_detect as _ml_detect  # noqa: E402


# ── rclone text log ───────────────────────────────────────────────────────────
#   2026/07/21 02:00:01 INFO  : file.txt: Copied (new)
class RcloneAdapter(LogAdapter):
    name = "rclone"
    language = "go"
    _RE = re.compile(
        r"^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\s+"
        r"(?P<level>DEBUG|INFO|NOTICE|ERROR)\s*:\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return _ml_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"]
        fields = None
        om = re.match(r"^(?P<obj>[^:]{1,200}):\s+(?P<act>.*)$", msg)
        if om and not msg.startswith(("There ", "Failed ")):
            fields = {"object": om.group("obj").strip(), "action": om.group("act")}
        return self._event(level={"NOTICE": "info"}.get(g["level"], g["level"]),
                           message=msg, source="rclone",
                           ts_ms=parse_timestamp(g["ts"]), fields=fields, raw=line)


register_adapter(RcloneAdapter())


# ═════════════════════════════════════════════════════════════════════════════
#  BATCH 7 — Amanda, NetBackup VxUL (vxlogview), Duplicati, Percona XtraBackup
# ═════════════════════════════════════════════════════════════════════════════
from ._common import split_any as _split_any  # noqa: E402


# ── Amanda / Zmanda per-run log (log.YYYYMMDDHHMMSS.N / amdump) ───────────────
#   SUCCESS dumper myhost /home 20260721020001 1 [sec 120.5 kb 1048576 …]
class AmandaAdapter(LogAdapter):
    name = "amanda"
    language = "any"
    _RE = re.compile(
        r"^(?P<res>START|FINISH|SUCCESS|PARTIAL|FAIL|STRANGE|INFO|WARNING|ERROR|"
        r"STATS|DONE|CHUNK|PART|DISK)\s+"
        r"(?P<prog>taper|dumper|chunker|planner|driver|amdump|amflush|amvault|"
        r"amanda|reporter)\b\s*(?P<rest>.*)$")
    _LVL = {"FAIL": "error", "ERROR": "error", "PARTIAL": "warn",
            "STRANGE": "warn", "WARNING": "warn"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {"result": g["res"], "program": g["prog"]}
        sm = re.search(r"\[sec ([\d.]+) kb (\d+) kps ([\d.]+)", g["rest"])
        if sm:
            fields.update({"sec": float(sm.group(1)), "kb": int(sm.group(2)),
                           "kps": float(sm.group(3))})
        toks = g["rest"].split()
        if len(toks) >= 2 and not toks[0].startswith("["):
            fields["host"] = toks[0]
            fields["disk"] = toks[1]
        return self._event(level=self._LVL.get(g["res"], "info"),
                           message=f'{g["res"]} {g["prog"]} {g["rest"]}'.strip()[:300],
                           source=f'amanda.{g["prog"]}', fields=fields, raw=line)


# ── Veritas NetBackup unified logging rendered by vxlogview ───────────────────
#   07/21/26 02:00:01.123 [nbpem] V-116-234 [JobScheduler::enqueue] job 123456 …
class NetbackupVxulAdapter(LogAdapter):
    name = "netbackup_vxul"
    language = "any"
    _RE = re.compile(
        r"^(?P<date>\d{2}/\d{2}/\d{2,4}) (?P<time>\d{2}:\d{2}:\d{2}\.\d{3})\s+"
        r"\[(?P<proc>[\w.\-]+)\]\s+(?P<code>V-\d+-\d+)\s+"
        r"(?:\[(?P<fn>[^\]]+)\]\s+)?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        low = g["msg"].lower()
        level = ("error" if any(w in low for w in ("error", "fail", "fatal"))
                 else "info")
        fields = {"message_code": g["code"], "process": g["proc"]}
        if g.get("fn"):
            fields["function"] = g["fn"]
        return self._event(level=level, message=g["msg"],
                           source=f'netbackup.{g["proc"]}',
                           ts_ms=us_date_ts(g["date"], g["time"].split(".")[0]),
                           fields=fields, raw=line)


# ── Duplicati 2 --log-file output ─────────────────────────────────────────────
#   2026-07-21 02:00:01 +02 - [Information-Duplicati.Library.Main.Controller-StartingOperation]: msg
class DuplicatiAdapter(LogAdapter):
    name = "duplicati"
    language = "dotnet"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?P<tz>[+-]\d{2}(?::?\d{2})?) - "
        r"\[(?P<lvl>Information|Warning|Error|Profiling|Verbose|Retry|DryRun)-"
        r"(?P<src>[\w.]+)-(?P<eid>\w+)\]:\s*(?P<msg>.*)$")
    _LVL = {"Information": "info", "Warning": "warn", "Error": "error",
            "Profiling": "debug", "Verbose": "debug", "Retry": "warn",
            "DryRun": "info"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        tz = g["tz"] if ":" in g["tz"] or len(g["tz"]) > 3 else g["tz"] + ":00"
        return self._event(level=self._LVL.get(g["lvl"], "info"), message=g["msg"],
                           source=g["src"],
                           ts_ms=parse_timestamp(f'{g["ts"]}{tz}'),
                           fields={"event_id": g["eid"]}, raw=line)


# ── Percona XtraBackup stderr log (2.x classic era) ───────────────────────────
#   xtrabackup: Transaction log of lsn (26970807) to (137343534) was copied.
#   160906 10:19:17 innobackupex: Starting the backup operation
# (The 8.x era "ISO 0 [Note] [MY-…] [Xtrabackup]" form is jsonl/mysql-shaped and
#  already routes via the `database` adapter's MySQL grammar when applicable.)
class PerconaXtrabackupAdapter(LogAdapter):
    name = "percona_xtrabackup"
    language = "any"
    _TOOL = re.compile(
        r"^(?:(?P<d>\d{6}) (?P<t>\d{2}:\d{2}:\d{2}) )?"
        r"(?P<tool>xtrabackup|innobackupex)(?:\s+\S+)?:\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._TOOL.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._TOOL.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        ts_ms = None
        if g.get("d"):
            d = g["d"]
            ts_ms = mk_ts(two_digit_year(d[0:2]), d[2:4], d[4:6],
                          *g["t"].split(":"))
        low = g["msg"].lower()
        level = ("error" if "error" in low or "failed" in low
                 else "info")
        if "completed ok" in low:
            level = "info"
        return self._event(level=level, message=g["msg"], source=g["tool"],
                           ts_ms=ts_ms, raw=line)


for _a in (AmandaAdapter(), NetbackupVxulAdapter(), DuplicatiAdapter(),
           PerconaXtrabackupAdapter()):
    register_adapter(_a)
