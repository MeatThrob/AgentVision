"""
Big-data / analytics / HPC-scheduler log adapters (BATCH 3)
================================================================================
The Hadoop/Spark log4j dialects, the PingCAP unified log format (TiDB, PD,
TiKV, Milvus), Airlift (Trino/Presto), and the HPC batch schedulers
(SLURM, PBS/TORQUE, Open MPI tagged output).

Formats: spark_log4j, hdfs_log4j, pingcap_unified, trino_airlift, slurm,
pbs_torque, openmpi_tag; batch 4 adds the HPC/EDA tool tables lsf_job,
gromacs_md, lammps_thermo, namd_log and the mpi_rank launcher prefix.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp,
                      multiline_ratio_detect, _to_ms, ratio_detect,
                      block_ratio, split_any)


# ── Spark log4j default (yy/MM/dd HH:mm:ss LEVEL class: msg) ─────────────────
#   17/06/09 20:10:40 INFO executor.CoarseGrainedExecutorBackend: Registered signal handlers …
class SparkLog4jAdapter(LogAdapter):
    name = "spark_log4j"
    language = "java"
    _RE = re.compile(
        r"^(?P<yy>\d{2})/(?P<mo>\d{2})/(?P<dy>\d{2}) (?P<hh>\d{2}):(?P<mm>\d{2}):(?P<ss>\d{2})\s+"
        r"(?P<lvl>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+(?P<cls>[\w.$]+):\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        ts_ms = None
        try:
            ts_ms = _to_ms(datetime(2000 + int(g["yy"]), int(g["mo"]), int(g["dy"]),
                                    int(g["hh"]), int(g["mm"]), int(g["ss"])))
        except ValueError:
            pass
        return self._event(level=g["lvl"], message=g["msg"], source=g["cls"],
                           ts_ms=ts_ms, raw=line)


# ── Hadoop/HDFS classic log4j (yymmdd HHMMSS millis LEVEL class: msg) ────────
#   081109 203615 148 INFO dfs.DataNode$PacketResponder: PacketResponder 1 for block …
class HdfsLog4jAdapter(LogAdapter):
    name = "hdfs_log4j"
    language = "java"
    _RE = re.compile(
        r"^(?P<yy>\d{2})(?P<mo>\d{2})(?P<dy>\d{2}) (?P<hh>\d{2})(?P<mm>\d{2})(?P<ss>\d{2})\s+"
        r"(?P<ms>\d+)\s+(?P<lvl>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+"
        r"(?P<cls>[\w.$]+):\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        ts_ms = None
        try:
            ts_ms = _to_ms(datetime(2000 + int(g["yy"]), int(g["mo"]), int(g["dy"]),
                                    int(g["hh"]), int(g["mm"]), int(g["ss"])))
        except ValueError:
            pass
        return self._event(level=g["lvl"], message=g["msg"], source=g["cls"],
                           ts_ms=ts_ms, raw=line)


# ── PingCAP unified log format (TiDB/PD/TiKV, Milvus) ────────────────────────
#   [2018/12/15 14:20:11.015 +08:00] [INFO] [kv.rs:145] [message] [key=value]
class PingCapUnifiedAdapter(LogAdapter):
    name = "pingcap_unified"
    language = "any"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d+ [+-]\d{2}:\d{2})\]\s+"
        r"\[(?P<lvl>TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\]\s+"
        r"\[(?P<src>[^\]]+)\]\s+\[(?P<msg>(?:[^\]\\]|\\.)*)\]\s*(?P<rest>.*)$")
    _FIELD = re.compile(r"\[([\w.\-]+)=((?:[^\]\\]|\\.)*)\]")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    @staticmethod
    def _ts(text: str) -> Optional[float]:
        # "2018/12/15 14:20:11.015 +08:00" → ISO for the shared parser
        m = re.match(r"(\d{4})/(\d{2})/(\d{2}) ([\d:.]+) ([+-]\d{2}:\d{2})", text or "")
        if not m:
            return None
        return parse_timestamp(f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}{m.group(5)}")

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {}
        for k, v in self._FIELD.findall(g.get("rest") or ""):
            fields[k] = v.strip('"')
        return self._event(level=g["lvl"], message=g["msg"].strip('"'),
                           source=g["src"], ts_ms=self._ts(g["ts"]),
                           fields=fields or None, raw=line)


# ── Airlift (Trino / Presto) ─────────────────────────────────────────────────
#   2024-04-08T10:07:48.026+0800 DEBUG main io.trino.util.CompilerUtils Defining class: …
class TrinoAirliftAdapter(LogAdapter):
    name = "trino_airlift"
    language = "java"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+(?:Z|[+-]\d{2}:?\d{2})?)\s+"
        r"(?P<lvl>DEBUG|INFO|WARN|ERROR)\s+(?P<thread>\S+)\s+"
        r"(?P<cls>(?:[\w$]+\.){2,}[\w$]+)\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["lvl"], message=g["msg"], source=g["cls"],
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"thread": g["thread"]}, raw=line)


# ── SLURM daemon logs + slurmstepd job-stdout lines ──────────────────────────
#   [2024-05-12T14:23:01.123] sched: Allocate JobId=12345 NodeList=node[001-004] …
#   slurmstepd: error: *** JOB 1234 ON compute-05 CANCELLED AT 2026-07-20T14:00:00 …
class SlurmAdapter(LogAdapter):
    name = "slurm"
    language = "any"
    _DAEMON = re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3,6})\]\s+"
        r"(?:(?P<lvl>fatal|error|debug\d?|verbose):\s+)?(?P<msg>.*)$")
    _STEPD = re.compile(
        r"^slurm(?:stepd|ctld|dbd|d)(?:-[\w.\-]+)?:\s+"
        r"(?:(?P<lvl>fatal|error|debug\d?|verbose|info):\s+)?(?P<msg>.*)$")

    def detect(self, sample_lines):
        def hit(ln):
            s = str(ln).strip()
            m = self._DAEMON.match(s)
            if m:
                # require a slurm-flavored body so a random "[ISO] text" line
                # from another tool cannot claim the whole file
                body = m.group("msg")
                return bool(re.search(
                    r"\bJobId=|\bjob(?:id)?\s|\blaunch task\b|\bsched(?:uler)?:|"
                    r"\bslurm|\bStepId=|\bpartition\b|_cg\b", body, re.IGNORECASE)
                    or m.group("lvl"))
            return bool(self._STEPD.match(s))
        return multiline_ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._DAEMON.match(s)
        src = "slurm"
        if not m:
            m = self._STEPD.match(s)
            if not m:
                return None
            src = s.split(":", 1)[0]
        g = m.groupdict()
        msg = g["msg"]
        lvl = (g.get("lvl") or "").rstrip("0123456789")
        if not lvl and ("CANCELLED" in msg or "FAILED" in msg or "error" in msg[:40]):
            lvl = "error"
        fields = {}
        jm = re.search(r"\bJobId=(\S+)|\bJOB (\d+)\b", msg)
        if jm:
            fields["job_id"] = jm.group(1) or jm.group(2)
        ts = g.get("ts") if "ts" in g else None
        return self._event(level=lvl or "info", message=msg, source=src,
                           ts_ms=parse_timestamp(ts) if ts else None,
                           fields=fields or None, raw=line)


# ── PBS Pro / TORQUE server & MoM logs ───────────────────────────────────────
#   05/12/2024 14:23:01;0008;PBS_Server;Job;12345.hpc;Job Queued at request of user@host, …
class PbsTorqueAdapter(LogAdapter):
    name = "pbs_torque"
    language = "any"
    _RE = re.compile(
        r"^(?P<mo>\d{2})/(?P<dy>\d{2})/(?P<yr>\d{4}) (?P<hh>\d{2}):(?P<mm>\d{2}):(?P<ss>\d{2});"
        r"(?P<code>[0-9A-Fa-f]{4});\s*(?P<daemon>[\w.\-]+);(?P<obj>\w+);(?P<id>[^;]*);(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        ts_ms = None
        try:
            ts_ms = _to_ms(datetime(int(g["yr"]), int(g["mo"]), int(g["dy"]),
                                    int(g["hh"]), int(g["mm"]), int(g["ss"])))
        except ValueError:
            pass
        low = g["msg"].lower()
        level = ("error" if ("error" in low or "failed" in low or "abort" in low)
                 else "info")
        return self._event(level=level, message=g["msg"], source=g["daemon"],
                           ts_ms=ts_ms,
                           fields={"event_code": g["code"], "object_type": g["obj"],
                                   "object_id": g["id"]}, raw=line)


# ── Open MPI mpirun --tag-output ─────────────────────────────────────────────
#   [1,3]<stdout>:iteration 100 residual=1.2e-06
class OpenMpiTagAdapter(LogAdapter):
    name = "openmpi_tag"
    language = "any"
    _RE = re.compile(
        r"^\[(?P<job>\d+),(?P<rank>\d+)\]<(?P<stream>stdout|stderr)>:\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level="warn" if g["stream"] == "stderr" else "info",
                           message=g["msg"], source=f'mpi.rank{g["rank"]}',
                           fields={"job": int(g["job"]), "rank": int(g["rank"]),
                                   "stream": g["stream"]}, raw=line)


# ── IBM Spectrum LSF job output (bsub -o report blocks) — BATCH 4 ────────────
#   Resource usage summary: / CPU time : 209542.33 sec. / Successfully completed.
class LsfJobAdapter(LogAdapter):
    name = "lsf_job"
    language = "any"
    _METRIC = re.compile(
        r"^\s*(?P<k>CPU time|Max Memory|Average Memory|Max Swap|Max Processes|"
        r"Max Threads|Total Requested Memory|Delta Memory|Run time|"
        r"Turnaround time)\s*:\s*(?P<v>[-\d.]+)?\s*(?P<unit>\S+)?\.?\s*$")
    _MARK = re.compile(
        r"^\s*(Resource usage summary:|Successfully completed\.|"
        r"Exited with exit code (?P<rc>\d+)\.?|Sender: LSF System\b|"
        r"Subject: Job (?P<job>\d+)[:,]|Job was executed on host|"
        r"The output \(if any\) follows:|TERM_\w+:)")

    def detect(self, sample_lines):
        def hit(x):
            s = x.strip()
            return bool(self._METRIC.match(s) or self._MARK.match(s))
        return ratio_detect(sample_lines, lambda ln: block_ratio(ln, hit))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        m = self._METRIC.match(s.strip())
        if m:
            g = m.groupdict()
            fields = {"metric": g["k"].lower().replace(" ", "_")}
            try:
                fields["value"] = float(g["v"]) if g["v"] else None
            except ValueError:
                fields["value"] = g["v"]
            if g.get("unit"):
                fields["unit"] = g["unit"].rstrip(".")
            return self._event(level="info", message=s.strip(), source="lsf",
                               fields=fields, raw=line)
        m = self._MARK.match(s.strip())
        if not m:
            return None
        g = m.groupdict()
        level = "info"
        fields = {}
        if g.get("rc") is not None:
            level = "error"
            fields["exit_code"] = int(g["rc"])
        if s.strip().startswith("TERM_"):
            level = "warn"
        if g.get("job"):
            fields["job_id"] = int(g["job"])
        return self._event(level=level, message=s.strip(), source="lsf",
                           trace_id=str(fields.get("job_id")) if fields.get("job_id") else None,
                           fields=fields or None, raw=line)


# ── GROMACS md.log (mdrun) — BATCH 4 ──────────────────────────────────────────
#   Step / Time two-column blocks + 'Energies (kJ/mol)' tables + banner lines.
class GromacsMdAdapter(LogAdapter):
    name = "gromacs_md"
    language = "any"
    _MARK = re.compile(
        r"^\s*(Step\s+Time\s*$|Energies \(kJ/mol\)|GROMACS:\s|:-\) GROMACS|"
        r"Log file opened on |Started mdrun|Core t \(s\)|Command line:|"
        r"Writing checkpoint, step )")
    _STEPROW = re.compile(r"^\s*(?P<step>\d+)\s+(?P<time>[-\d.]+)\s*$")

    def detect(self, sample_lines):
        # A block is GROMACS when at least one marker line is present.
        return ratio_detect(
            sample_lines,
            lambda ln: any(self._MARK.match(x) for x in split_any(ln)))

    def parse_line(self, line: str) -> Optional[dict]:
        pieces = split_any(line)
        if not pieces:
            return None
        if not any(self._MARK.match(x) for x in pieces):
            # a bare "step time" row between headers still belongs to the file
            m = self._STEPROW.match(pieces[0])
            if m and len(pieces) == 1:
                return self._event(level="debug", message=pieces[0].strip(),
                                   source="gromacs",
                                   fields={"step": int(m.group("step")),
                                           "time_ps": float(m.group("time"))},
                                   raw=line)
            return None
        fields = {}
        for i, x in enumerate(pieces):
            if re.match(r"^\s*Step\s+Time\s*$", x) and i + 1 < len(pieces):
                sm = self._STEPROW.match(pieces[i + 1])
                if sm:
                    fields = {"step": int(sm.group("step")),
                              "time_ps": float(sm.group("time"))}
                break
        head = next((x.strip() for x in pieces if self._MARK.match(x)),
                    pieces[0].strip())
        return self._event(level="info", message=head, source="gromacs",
                           fields=fields or None, raw=line)


# ── LAMMPS log.lammps thermo output — BATCH 4 ─────────────────────────────────
#   Step Temp E_pair … header + whitespace-aligned numeric rows.
class LammpsThermoAdapter(LogAdapter):
    name = "lammps_thermo"
    language = "any"
    _KEYWORDS = ("Temp", "E_pair", "E_mol", "TotEng", "PotEng", "KinEng",
                 "Press", "Volume", "Enthalpy", "Density", "Atoms", "CPU")
    _HEADER = re.compile(r"^\s*Step(\s+[A-Za-z_][\w/\[\]().]*)+\s*$")
    _NUMROW = re.compile(r"^\s*\d+(\s+[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?){3,}\s*$")
    _MARK = re.compile(r"^(LAMMPS \(|Per MPI rank memory allocation|Loop time of|"
                       r"Total wall time:|Reading data file|units\s+\w+)")

    def _classify(self, s: str) -> str:
        if self._MARK.match(s):
            return "mark"
        if self._HEADER.match(s) and any(k in s for k in self._KEYWORDS):
            return "header"
        if self._NUMROW.match(s):
            return "row"
        return ""

    def detect(self, sample_lines):
        strong = weak = seen = 0
        for ln in sample_lines:
            pieces = split_any(ln)
            if not pieces:
                continue
            seen += 1
            kinds = {self._classify(x) for x in pieces}
            kinds.discard("")
            if "header" in kinds or "mark" in kinds:
                strong += 1
            elif "row" in kinds:
                weak += 1
        if not seen:
            return 0.0
        # bare all-numeric rows are only WEAK evidence: enough to beat the
        # structural fallback, never enough to outrank a named adapter.
        return (strong / seen) if strong else (weak / seen) * 0.6

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        kind = ""
        for x in split_any(s):
            kind = self._classify(x)
            if kind:
                s = x
                break
        if not kind:
            return None
        if kind == "header":
            cols = s.split()
            return self._event(level="info", message=s.strip(), source="lammps",
                               fields={"columns": cols}, raw=line)
        if kind == "row":
            vals = s.split()
            fields = {"step": int(vals[0]),
                      "values": [float(v) for v in vals[1:]]}
            return self._event(level="debug", message=s.strip(), source="lammps",
                               fields=fields, raw=line)
        return self._event(level="info", message=s.strip(), source="lammps",
                           raw=line)


# ── NAMD stdout / log (prefix-tagged by design) — BATCH 4 ─────────────────────
#   ENERGY:    1000  1234.5 … / ETITLE: / Info: / TIMING: / WRITING …
class NamdLogAdapter(LogAdapter):
    name = "namd_log"
    language = "any"
    _RE = re.compile(r"^(?P<tag>ENERGY|ETITLE|Info|TIMING|WRITING|OPENING|"
                     r"TCL|WALL|PERFORMANCE|Charm\+\+)(?P<sep>:\s|\s)(?P<msg>.*)$")

    def detect(self, sample_lines):
        def hit(x):
            m = self._RE.match(x.strip())
            # 'Info: …' alone is too generic — require an unambiguous NAMD tag
            # somewhere in the block before Info:/WALL: lines count.
            return bool(m and m.group("tag") in
                        ("ENERGY", "ETITLE", "TIMING", "TCL", "Charm++"))
        def block_hit(el):
            pieces = split_any(el)
            if not pieces:
                return False
            if any(hit(x) for x in pieces):
                return True
            return False
        return ratio_detect(sample_lines, block_hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        g = m.groupdict()
        tag, msg = g["tag"], g["msg"].strip()
        fields = {"tag": tag}
        if tag == "ENERGY":
            vals = msg.split()
            try:
                fields["step"] = int(vals[0])
                fields["values"] = [float(v) for v in vals[1:]]
            except (ValueError, IndexError):
                pass
            return self._event(level="debug", message=s[:200], source="namd",
                               fields=fields, raw=line)
        level = "info" if tag in ("Info", "ETITLE", "TCL", "Charm++") else "debug"
        return self._event(level=level, message=msg or s, source="namd",
                           fields=fields, raw=line)


# ── MPI rank-prefixed stdout (mpiexec -prepend-rank / srun --label) — BATCH 4 ─
#   [2] rank 2 reporting: setup complete      |      3: iteration 100 …
class MpiRankAdapter(LogAdapter):
    name = "mpi_rank"
    language = "any"
    _BRACKET = re.compile(r"^\[(?P<r>\d{1,6})\]\s+(?P<msg>\S.*)$")
    _COLON = re.compile(r"^(?P<r>\d{1,4}):\s(?P<msg>\S.*)$")

    def _hit(self, s: str) -> bool:
        m = self._BRACKET.match(s) or self._COLON.match(s)
        if not m:
            return False
        body = m.group("msg")
        # Disambiguation from other id-prefixed products (coturn "1234: session
        # …: realm …:", SQL Agent "[100] Product … : Process ID"): a body with
        # further ": " separators must talk about ranks to count as MPI.
        if ": " in body and not re.search(r"\brank\b", body, re.IGNORECASE):
            return False
        return True

    def detect(self, sample_lines):
        # The prefix is the only structure and it is short — cap confidence
        # well below any named grammar so this can never steal a real format,
        # while still comfortably beating the structural fallback (≤0.3).
        r = ratio_detect(sample_lines, lambda ln: self._hit(str(ln).strip()))
        return r * 0.65

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if not self._hit(s):
            return None
        m = self._BRACKET.match(s) or self._COLON.match(s)
        if not m:
            return None
        g = m.groupdict()
        msg = g["msg"].strip()
        low = msg.lower()
        level = ("error" if ("error" in low or "abort" in low or "fatal" in low
                             or "desync" in low)
                 else "info")
        return self._event(level=level, message=msg,
                           source=f"mpi.rank{g['r']}",
                           fields={"rank": int(g["r"])}, raw=line)


# ═════════════════════════════════════════════════════════════════════════════
#  BATCH 7 — HPC scheduler text records + science/simulation outputs
# ═════════════════════════════════════════════════════════════════════════════
from ._common import bsd_year_ts, mk_ts, _SYSLOG_SEVERITY  # noqa: E402  (batch-7 helpers)


# ── HTCondor job event log (ULOG) ─────────────────────────────────────────────
#   005 (12345.000.000) 2024-05-12 14:23:01 Job terminated.
#   000 (1234.000.000) 07/20 14:00:01 Job submitted from host: <10.1.1.5:9618>
# Multi-line records are terminated by a bare "..." line.
class HtcondorUlogAdapter(LogAdapter):
    name = "htcondor_ulog"
    language = "any"
    _HEAD = re.compile(
        r"^(?P<code>\d{3})\s+\((?P<cluster>\d+)\.(?P<proc>\d+)\.(?P<sub>\d+)\)\s+"
        r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}|\d{2}/\d{2} \d{2}:\d{2}:\d{2})\s+"
        r"(?P<msg>.*)$")
    # event codes that indicate trouble
    _WARN = {"004", "006", "007", "010", "012", "021", "024"}   # evicted/held/etc.
    _ERR = {"002", "009"}                                        # error / aborted

    def detect(self, sample_lines):
        def ok(el):
            subs = split_any(el)
            return bool(subs) and bool(self._HEAD.match(subs[0].strip()))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        subs = split_any(line)
        if not subs:
            return None
        m = self._HEAD.match(subs[0].strip())
        if not m:
            return None
        g = m.groupdict()
        code = g["code"]
        level = ("error" if code in self._ERR
                 else "warn" if code in self._WARN else "info")
        fields = {"event_code": code,
                  "job": f'{g["cluster"]}.{g["proc"]}.{g["sub"]}'}
        if len(subs) > 1:                       # detail lines of the record
            fields["detail"] = " | ".join(x.strip() for x in subs[1:]
                                          if x.strip() != "...")[:500]
        return self._event(level=level, message=g["msg"],
                           source=f'condor.job{g["cluster"]}.{g["proc"]}',
                           ts_ms=parse_timestamp(g["ts"]), fields=fields, raw=line)


# ── Grid Engine (SGE/UGE/OGE) messages log ────────────────────────────────────
#   05/12/2024 14:23:01|worker|node001|I|job 12345.1 finished on host node001
class GridEngineAdapter(LogAdapter):
    name = "grid_engine"
    language = "any"
    _RE = re.compile(
        r"^(?P<mo>\d{2})/(?P<dy>\d{2})/(?P<yr>\d{4}) (?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})\|"
        r"(?P<comp>[\w.\-]+)\|(?P<host>[\w.\-]+)\|(?P<sev>[IWECP])\|(?P<msg>.*)$")
    _LVL = {"I": "info", "W": "warn", "E": "error", "C": "fatal", "P": "info"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=self._LVL.get(g["sev"], "info"), message=g["msg"],
                           source=f'sge.{g["comp"]}',
                           ts_ms=mk_ts(g["yr"], g["mo"], g["dy"], g["hh"], g["mi"], g["ss"]),
                           fields={"host": g["host"], "component": g["comp"]}, raw=line)


# ── PBS Pro / OpenPBS accounting log ──────────────────────────────────────────
#   07/20/2026 14:00:01;E;1234.pbs;user=alice group=hpc … walltime=01:23:45
class PbsAccountingAdapter(LogAdapter):
    name = "pbs_accounting"
    language = "any"
    _RE = re.compile(
        r"^(?P<mo>\d{2})/(?P<dy>\d{2})/(?P<yr>\d{4}) (?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2});"
        r"(?P<rec>[A-Z]);(?P<jobid>[^;]+);(?P<rest>.*)$")
    _REC = {"Q": "queued", "S": "started", "E": "ended", "D": "deleted",
            "R": "rerun", "A": "aborted", "C": "checkpointed", "T": "restarted",
            "U": "unconfirmed", "K": "removed", "B": "reservation-begin",
            "F": "reservation-end", "L": "license", "Y": "confirmed"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        rec = g["rec"]
        level = "warn" if rec in ("A", "D", "K") else "info"
        fields = {"record": rec, "record_kind": self._REC.get(rec, rec),
                  "job_id": g["jobid"]}
        for k, v in re.findall(r"([\w.]+)=(\S+)", g["rest"]):
            if k in ("user", "group", "jobname", "queue", "Exit_status",
                     "resources_used.walltime", "resources_used.mem",
                     "resources_used.cput", "exec_host"):
                fields[k] = v
        if fields.get("Exit_status") not in (None, "0"):
            level = "warn"
        msg = f'{self._REC.get(rec, rec)} {g["jobid"]}'
        return self._event(level=level, message=msg, source="pbs.accounting",
                           ts_ms=mk_ts(g["yr"], g["mo"], g["dy"], g["hh"], g["mi"], g["ss"]),
                           fields=fields, raw=line)


# ── PBS/TORQUE epilogue resource block (job .o tail) ──────────────────────────
#   Resources Used: cput=00:15:32,mem=2048mb,vmem=4096mb,walltime=00:20:01
class PbsEpilogueAdapter(LogAdapter):
    name = "pbs_epilogue"
    language = "any"
    _RE = re.compile(
        r"^\s*(?P<kind>Resources Used|Resource List)\s*:\s*(?P<body>\S.*)$")

    def _hit(self, s: str) -> bool:
        m = self._RE.match(s)
        return bool(m and re.search(r"\b(cput|walltime|mem|vmem)=", m.group("body")))

    def detect(self, sample_lines):
        def ok(el):
            return any(self._hit(x.strip()) for x in split_any(el))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        for x in split_any(line):
            m = self._RE.match(x.strip())
            if m and re.search(r"\b(cput|walltime|mem|vmem)=", m.group("body")):
                fields = {"kind": m.group("kind")}
                for k, v in re.findall(r"([\w.]+)=([^,\s]+)", m.group("body")):
                    fields[k] = v
                return self._event(level="info", message=x.strip(),
                                   source="pbs.epilogue", fields=fields, raw=line)
        return None


# ── SLURM sacct -p / scontrol show job structured output ─────────────────────
#   JobID|JobName|State|ExitCode|Elapsed
#   1234|train.sh|COMPLETED|0:0|01:23:45
#   JobId=1234 JobName=train.sh UserId=alice(1000) … JobState=RUNNING
class SlurmSacctAdapter(LogAdapter):
    name = "slurm_sacct"
    language = "any"
    _HDR = re.compile(r"^JobID\|[\w|]+\|?$")
    _SCONTROL = re.compile(r"^JobId=\d+\s+JobName=\S+")
    _BAD = ("FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY",
            "PREEMPTED", "BOOT_FAIL", "DEADLINE")

    def detect(self, sample_lines):
        def ok(el):
            subs = split_any(el)
            if not subs:
                return False
            first = subs[0].strip()
            return bool(self._HDR.match(first) or self._SCONTROL.match(first))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        subs = split_any(line)
        if not subs:
            return None
        first = subs[0].strip()
        if self._SCONTROL.match(first):
            fields = {k: v for k, v in re.findall(r"(\w+)=(\S+)", first)}
            state = fields.get("JobState", "")
            level = "error" if state in self._BAD else "info"
            return self._event(level=level,
                               message=f'Job {fields.get("JobId")} {state or "info"}'.strip(),
                               source="slurm.scontrol", fields=fields, raw=line)
        if self._HDR.match(first):
            cols = [c for c in first.split("|") if c]
            fields = {"columns": cols}
            level = "info"
            msg = f"sacct table ({len(cols)} columns)"
            if len(subs) > 1:                  # first data row → the event
                vals = subs[1].strip().split("|")
                row = dict(zip(cols, vals))
                fields.update(row)
                state = row.get("State", "")
                level = "error" if any(state.startswith(b) for b in self._BAD) else "info"
                msg = f'{row.get("JobID", "?")} {row.get("JobName", "")} {state}'.strip()
            return self._event(level=level, message=msg, source="slurm.sacct",
                               fields=fields, raw=line)
        return None


# ── IBM Spectrum LSF daemon log (lsf.log / mbatchd/sbatchd/lim) ───────────────
#   Jul 20 14:00:01 2026 12345 6 10.1 mbatchd: Job <1234>: Done successfully.
class LsfDaemonAdapter(LogAdapter):
    name = "lsf_daemon"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+"
        r"(?P<pid>\d+)\s+(?P<sev>\d)\s+(?P<ver>[\d.]+)\s+"
        r"(?P<daemon>[\w.\-]+):\s*(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(str(ln).strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        sev = int(g["sev"])
        level = _SYSLOG_SEVERITY.get(sev, "DEBUG") if sev <= 7 else "DEBUG"
        return self._event(level=level, message=g["msg"], source=f'lsf.{g["daemon"]}',
                           ts_ms=bsd_year_ts(g["ts"]),
                           fields={"pid": int(g["pid"]), "lsf_level": sev,
                                   "version": g["ver"]}, raw=line)


# ── Open MPI runtime error banner (ORTE/PRTE) ─────────────────────────────────
#   --------------------------------------------------------------------------
#   mpirun noticed that process rank 3 with PID 0 on node compute-04 exited …
class OpenMpiOrteAdapter(LogAdapter):
    name = "openmpi_orte"
    language = "any"
    _VOCAB = re.compile(
        r"mpirun (?:noticed|detected|has exited)|ORTE_ERROR_LOG|"
        r"PRTE has lost communication|MPI_ABORT was invoked|"
        r"ORTE (?:was unable|has lost)|opal_\w+ failed", re.IGNORECASE)
    _RULE = re.compile(r"^-{24,}$")

    def detect(self, sample_lines):
        def ok(el):
            subs = split_any(el)
            return (any(self._VOCAB.search(x) for x in subs)
                    and (any(self._RULE.match(x.strip()) for x in subs)
                         or len(subs) == 1))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        subs = [x for x in split_any(line) if not self._RULE.match(x.strip())]
        hit = next((x for x in subs if self._VOCAB.search(x)), None)
        if hit is None:
            return None
        fields = {}
        rm = re.search(r"rank (\d+)", hit)
        if rm:
            fields["rank"] = int(rm.group(1))
        sm = re.search(r"signal (\d+)\s*\(([^)]+)\)", hit)
        if sm:
            fields["signal"] = int(sm.group(1))
            fields["signal_name"] = sm.group(2)
        return self._event(level="error", message=hit.strip(), source="openmpi.runtime",
                           fields=fields or None, raw=line)


# ── OpenFOAM solver log ───────────────────────────────────────────────────────
#   Time = 0.5
#   smoothSolver:  Solving for Ux, Initial residual = 0.0123, Final residual = 1e-06, No Iterations 3
class OpenFoamAdapter(LogAdapter):
    name = "openfoam"
    language = "any"
    _SOLVE = re.compile(
        r"^(?P<solver>[\w:]+):\s+Solving for (?P<field>\w+), Initial residual = "
        r"(?P<ir>[\d.eE+\-]+), Final residual = (?P<fr>[\d.eE+\-]+), No Iterations (?P<it>\d+)")
    _TIME = re.compile(r"^Time = (?P<t>[\d.eE+\-]+)\s*$")
    _EXEC = re.compile(r"^ExecutionTime = (?P<et>[\d.]+) s\b.*ClockTime")

    def detect(self, sample_lines):
        def ok(el):
            subs = split_any(el)
            strong = any(self._SOLVE.match(x.strip()) or self._EXEC.match(x.strip())
                         for x in subs)
            if not strong:
                return False
            weak = sum(1 for x in subs
                       if self._SOLVE.match(x.strip()) or self._TIME.match(x.strip())
                       or self._EXEC.match(x.strip()))
            return weak / len(subs) >= 0.5
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        subs = split_any(line)
        fields = {}
        msg = None
        for x in subs:
            x = x.strip()
            tm = self._TIME.match(x)
            if tm:
                fields["sim_time"] = float(tm.group("t"))
                continue
            sm = self._SOLVE.match(x)
            if sm:
                g = sm.groupdict()
                fields.update({"solver": g["solver"], "field": g["field"],
                               "initial_residual": float(g["ir"]),
                               "final_residual": float(g["fr"]),
                               "iterations": int(g["it"])})
                msg = msg or x
                continue
            em = self._EXEC.match(x)
            if em:
                fields["execution_time_s"] = float(em.group("et"))
                msg = msg or x
        if not fields:
            return None
        return self._event(level="info", message=msg or subs[0].strip(),
                           source="openfoam", fields=fields, raw=line)


# ── AMBER (pmemd/sander) mdout energy record ─────────────────────────────────
#    NSTEP =        0   TIME(PS) =       0.000  TEMP(K) =   301.23  PRESS = 0.0
class AmberMdoutAdapter(LogAdapter):
    name = "amber_mdout"
    language = "any"
    _RE = re.compile(
        r"^\s*NSTEP =\s*(?P<step>\d+)\s+TIME\(PS\) =\s*(?P<t>[\d.\-]+)"
        r"(?:\s+TEMP\(K\) =\s*(?P<temp>[\d.\-]+))?(?:\s+PRESS =\s*(?P<press>[\d.\-]+))?")

    def detect(self, sample_lines):
        def ok(el):
            return any(self._RE.match(x) for x in split_any(el))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        for x in split_any(line):
            m = self._RE.match(x)
            if m:
                g = m.groupdict()
                fields = {"nstep": int(g["step"]), "time_ps": float(g["t"])}
                if g.get("temp"):
                    fields["temp_k"] = float(g["temp"])
                if g.get("press"):
                    fields["press"] = float(g["press"])
                # trailing energy block lines (Etot/EKtot/EPtot) → fields
                for k, v in re.findall(r"(Etot|EKtot|EPtot)\s*=\s*([\d.eE+\-]+)",
                                       str(line)):
                    fields[k.lower()] = float(v)
                return self._event(level="info", message=x.strip(),
                                   source="amber", fields=fields, raw=line)
        return None


# ── Quantum ESPRESSO pw.x output ──────────────────────────────────────────────
#   !    total energy              =    -155.83729145 Ry
class QuantumEspressoAdapter(LogAdapter):
    name = "quantum_espresso"
    language = "any"
    _ENERGY = re.compile(
        r"^!?\s*total energy\s+=\s+(?P<e>-?[\d.]+)\s+Ry\b")
    _ITER = re.compile(r"^\s*iteration #\s*(?P<n>\d+)\b")
    _PROG = re.compile(r"^\s*Program PWSCF v\.?\S*")

    def _hit(self, s):
        return bool(self._ENERGY.match(s) or self._ITER.match(s)
                    or self._PROG.match(s))

    def detect(self, sample_lines):
        def ok(el):
            return any(self._hit(x) for x in split_any(el))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        for x in split_any(line):
            m = self._ENERGY.match(x)
            if m:
                converged = x.lstrip().startswith("!")
                return self._event(level="info", message=x.strip(), source="pwscf",
                                   fields={"total_energy_ry": float(m.group("e")),
                                           "converged": converged}, raw=line)
            m = self._ITER.match(x)
            if m:
                return self._event(level="info", message=x.strip(), source="pwscf",
                                   fields={"iteration": int(m.group("n"))}, raw=line)
            if self._PROG.match(x):
                return self._event(level="info", message=x.strip(), source="pwscf",
                                   raw=line)
        return None


# ── gem5 simulator console ────────────────────────────────────────────────────
#   info: Entering event queue @ 0.  Starting simulation...
#   warn: … | panic: … | fatal: … | hack: …
# Vocabulary-gated: the lowercase "info:" prefix alone is too generic to own.
class Gem5Adapter(LogAdapter):
    name = "gem5"
    language = "cpp"
    _RE = re.compile(r"^(?P<lvl>info|warn|hack|fatal|panic):\s+(?P<msg>.*)$")
    _VOCAB = re.compile(
        r"event queue|Starting simulation|Global frequency set|gem5|curTick|"
        r"m5_\w+|simulate\(\)|Exiting @ tick")
    _LVL = {"info": "info", "warn": "warn", "hack": "warn",
            "fatal": "fatal", "panic": "fatal"}

    def detect(self, sample_lines):
        def ok(el):
            subs = split_any(el)
            return (any(self._RE.match(x.strip()) for x in subs)
                    and any(self._VOCAB.search(x) for x in subs))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        for x in split_any(line):
            m = self._RE.match(x.strip())
            if m:
                g = m.groupdict()
                return self._event(level=self._LVL.get(g["lvl"], "info"),
                                   message=g["msg"], source="gem5", raw=line)
        return None


# ── Registration ─────────────────────────────────────────────────────────────
# Airlift's "ISO±HHMM LEVEL thread class msg" shares its timestamp+level prefix
# with dnf.log (os_platform, loaded earlier) → insert trino BEFORE dnf_log so
# the stricter dotted-classname grammar wins the 1.0 tie on airlift lines,
# while dnf still wins whole-file dnf samples (airlift can't match DDEBUG or
# undotted bodies).
register_adapter(TrinoAirliftAdapter(), before="dnf_log")
for _a in (SparkLog4jAdapter(), HdfsLog4jAdapter(), PingCapUnifiedAdapter(),
           SlurmAdapter(), PbsTorqueAdapter(), OpenMpiTagAdapter(),
           # batch 4 — HPC/EDA tool tables + rank-prefixed launchers. mpi_rank
           # is capped at 0.65 so it never outranks a named grammar; it only
           # rescues rank-prefixed app output from the structural fallback.
           LsfJobAdapter(), GromacsMdAdapter(), LammpsThermoAdapter(),
           NamdLogAdapter(), MpiRankAdapter(),
           # batch 7 — HPC scheduler text records + science/simulation outputs.
           # lsf_daemon must beat the bare "Mon DD HH:MM:SS YYYY" ctime shapes
           # only via its extra pid/level/version columns (regex-exact, no tie).
           HtcondorUlogAdapter(), GridEngineAdapter(), PbsAccountingAdapter(),
           PbsEpilogueAdapter(), SlurmSacctAdapter(), LsfDaemonAdapter(),
           OpenMpiOrteAdapter(), OpenFoamAdapter(), AmberMdoutAdapter(),
           QuantumEspressoAdapter(), Gem5Adapter()):
    register_adapter(_a)


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH 8 — science/simulation transcripts + HPC daemon/distributed logs
# ══════════════════════════════════════════════════════════════════════════════
from ._common import (RxAdapter, vocab_detect, split_any,  # noqa: E402
                      block_ratio, ratio_detect)


# ── ns-3 network simulator (NS_LOG with prefixes) ─────────────────────────────
#   +2.000000000s 0 UdpEchoClientApplication:Send(0x557...): Sent 1024 bytes …
class Ns3Adapter(RxAdapter):
    name = "ns3"
    language = "cpp"
    _RE = re.compile(
        r"^[+-]?(?P<t>\d+(?:\.\d+)?)s\s+(?P<node>\d+)\s+"
        r"(?P<comp>[A-Za-z]\w*):(?P<func>[A-Za-z]\w*)\(")

    def _fields(self, g, line):
        return {"sim_time_s": float(g["t"]), "node": int(g["node"]),
                "component": g["comp"], "function": g["func"]}

    def _level(self, g, line):
        return ""

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            m = self._RE.match(line.strip())
            ev["source"] = m.group("comp")
            ev["category"] = "event"
        return ev


# ── MCNP / Monte-Carlo transport run summary ──────────────────────────────────
#   1random number seed  =        19073486328125   (leading FORTRAN cc column)
class McnpAdapter(LogAdapter):
    name = "mcnp"
    language = "any"
    _SEED = re.compile(r"^\d?\s*random number seed\s*=\s*(?P<seed>\d+)", re.I)
    _RUN = re.compile(r"\b(nps|ctm|keff|histories|stride)\b", re.I)

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            return any(self._SEED.match(x.strip()) for x in subs) or (
                any("random number seed" in x.lower() for x in subs))
        return vocab_detect(sample_lines, hit, cap=0.8)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        fields = {}
        for x in split_any(s):
            m = self._SEED.match(x.strip())
            if m:
                fields["random_seed"] = int(m.group("seed"))
                break
        return self._event(level="", message=s.strip(), source="mcnp",
                           fields=fields or None, category="log", raw=line)


# ── Monte-Carlo / MCMC iteration-convergence trace ────────────────────────────
#   iter 1000/10000  accept=0.234  mean=-1.2345  stderr=0.0031
class McIterationAdapter(LogAdapter):
    name = "mc_iteration"
    language = "any"
    _RE = re.compile(
        r"^(?P<kind>iter|iteration|step|sample)\s+(?P<n>\d+)\s*/\s*(?P<tot>\d+)\b",
        re.I)
    _METRIC = re.compile(r"\b(accept(?:ance)?|ess|rhat|r_hat|mean|stderr|logp)\b",
                         re.I)

    def detect(self, sample_lines):
        def hit(el):
            for x in split_any(el):
                x = x.strip()
                if self._RE.match(x) and self._METRIC.search(x):
                    return True
            return False
        return vocab_detect(sample_lines, hit, cap=0.85)

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            for x in split_any(line):
                if self._RE.match(x.strip()):
                    m = self._RE.match(x.strip())
                    s = x.strip()
                    break
        if not m:
            return None
        fields = {"iteration": int(m.group("n")), "total": int(m.group("tot"))}
        for k, v in re.findall(r"(\w+)=([-\d.]+)", s):
            try:
                fields[k] = float(v)
            except ValueError:
                pass
        return self._event(level="", message=s, source="mc.sampler",
                           fields=fields, category="event", raw=line)


# ── Stan / CmdStan sampler ─────────────────────────────────────────────────────
#   Iteration:  1000 / 2000 [ 50%]  (Sampling)
class StanSamplerAdapter(RxAdapter):
    name = "stan_sampler"
    language = "any"
    default_source = "stan"
    _RE = re.compile(
        r"^Iteration:\s+(?P<n>\d+)\s*/\s*(?P<tot>\d+)\s*\[\s*(?P<pct>\d+)%\]\s*"
        r"\((?P<phase>Warmup|Sampling)\)")

    def _level(self, g, line):
        return ""

    def _fields(self, g, line):
        return {"iteration": int(g["n"]), "total": int(g["tot"]),
                "percent": int(g["pct"]), "phase": g["phase"]}


# ── MPICH / Hydra process manager ─────────────────────────────────────────────
#   [mpiexec@node01] Sending Ctrl-C to processes as requested
#   [proxy:0:0@host] ...
class MpichHydraAdapter(RxAdapter):
    name = "mpich_hydra"
    language = "any"
    _RE = re.compile(r"^\[(?P<who>(?:mpiexec|proxy)[:\d]*@[\w.\-]+)\]\s+(?P<msg>.*)$")

    def _level(self, g, line):
        return "error" if re.search(r"\berror|fail|abort", g.get("msg", ""), re.I) else ""

    def _fields(self, g, line):
        return {"hydra_source": g["who"]}

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            m = self._RE.match(line.strip())
            if m:
                ev["source"] = m.group("who")
        return ev


# ── Geant4 random-engine status banner ────────────────────────────────────────
#   --------- Ranecu engine status --------- Initial seed (index) = 0 …
class Geant4Adapter(LogAdapter):
    name = "geant4"
    language = "cpp"
    _RE = re.compile(r"-{3,}\s*(?P<engine>[\w ]+?)\s+engine status\s*-{3,}", re.I)

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda el: any(self._RE.search(x) for x in split_any(el)))

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        m = None
        for x in split_any(s):
            m = self._RE.search(x)
            if m:
                break
        if not m:
            return None
        return self._event(level="", message=s.strip(),
                           source="geant4",
                           fields={"engine": m.group("engine").strip()},
                           category="event", raw=line)


# ── MATLAB diary (Command Window transcript) ──────────────────────────────────
#   >> rng(42,'twister')  /  >> x = rand(3,1)  /  x =  /      0.3745 …
class MatlabDiaryAdapter(LogAdapter):
    name = "matlab_diary"
    language = "matlab"
    _PROMPT = re.compile(r"^>>\s")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            return bool(subs) and any(self._PROMPT.match(x) for x in subs)
        return vocab_detect(sample_lines, hit, cap=0.8)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        subs = split_any(s)
        cmd = next((x.strip()[3:] for x in subs if self._PROMPT.match(x)), s.strip())
        return self._event(level="", message=cmd, source="matlab",
                           fields={"block_lines": len(subs)} if len(subs) > 1 else None,
                           category="log", raw=line)


# ── R console / sink() transcript ─────────────────────────────────────────────
#   > set.seed(123)  /  > runif(3)  /  [1] 0.2875775 0.7883051 0.4089769
class RConsoleAdapter(LogAdapter):
    name = "r_console"
    language = "r"
    _PROMPT = re.compile(r"^>\s(?!>)")
    _RESULT = re.compile(r"^\[\d+\]\s")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            has_cmd = any(self._PROMPT.match(x) for x in subs)
            has_res = any(self._RESULT.match(x) for x in subs)
            return has_cmd and (has_res or len(subs) == 1)
        return vocab_detect(sample_lines, hit, cap=0.85)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        subs = split_any(s)
        cmd = next((x.strip()[2:] for x in subs if self._PROMPT.match(x)), s.strip())
        return self._event(level="", message=cmd, source="R",
                           fields={"block_lines": len(subs)} if len(subs) > 1 else None,
                           category="log", raw=line)


# ── Julia @info/@warn/@error logging (box-drawing prefixes) ────────────────────
#   ┌ Info: Starting simulation │   seed = 42 └ @ Main sim.jl:12
class JuliaLoggingAdapter(LogAdapter):
    name = "julia_logging"
    language = "julia"
    _RE = re.compile(r"^┌\s+(?P<level>Info|Warning|Error|Debug):\s*(?P<msg>.*)$")
    _LOC = re.compile(r"└\s+@\s+(?P<mod>\S+)\s+(?P<file>\S+:\d+)")
    _LVL = {"Info": "info", "Warning": "warn", "Error": "error", "Debug": "debug"}

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            return bool(subs) and (self._RE.match(subs[0].strip())
                                   or subs[0].lstrip().startswith("┌ "))
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        subs = split_any(s)
        head = subs[0].strip() if subs else s.strip()
        m = self._RE.match(head)
        if not m:
            return None
        g = m.groupdict()
        fields = {}
        lm = self._LOC.search(s)
        if lm:
            fields = {"module": lm.group("mod"), "location": lm.group("file")}
        return self._event(level=self._LVL.get(g["level"], "info"),
                           message=g["msg"].split("│")[0].strip() or g["msg"],
                           source="julia", fields=fields or None, raw=line)


# ── HTCondor daemon log (SchedLog/StarterLog/MasterLog) ───────────────────────
#   05/12/24 14:23:01 (pid:2841) Sent ad to central manager for user@host
class HtcondorDaemonAdapter(RxAdapter):
    name = "htcondor_daemon"
    language = "any"
    default_source = "htcondor"
    _RE = re.compile(
        r"^(?P<mon>\d{2})/(?P<dy>\d{2})/(?P<yr>\d{2})\s+"
        r"(?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})\s+\(pid:(?P<pid>\d+)\)\s+"
        r"(?P<msg>.*)$")

    def _ts(self, g):
        return mk_ts(2000 + int(g["yr"]), g["mon"], g["dy"], g["hh"], g["mi"], g["ss"])

    def _level(self, g, line):
        return "error" if re.search(r"\berror|fail|abort|denied", g.get("msg", ""), re.I) else ""

    def _fields(self, g, line):
        return {"pid": int(g["pid"])}


# ── Dask / Ray distributed worker logs ────────────────────────────────────────
#   (pid=12345) 2026-07-20 14:00:01,234 - distributed.worker - INFO - Starting …
#   distributed.worker - INFO - Start worker at: tcp://…
#   (pid=12345) Trial run_42 result: reward=0.87 seed=42
class DaskRayAdapter(LogAdapter):
    name = "dask_ray"
    language = "python"
    _RAY = re.compile(r"^\((?:\w[\w ]*\s)?pid=\d+(?:,\s*ip=[\d.]+)?\)\s")
    _DASK = re.compile(
        r"^(?:\(\S[^)]*\)\s+)?(?:[\d\-:, ]+\s+-\s+)?"
        r"distributed\.(?P<comp>worker|scheduler|nanny|core|utils\w*)\s+-\s+"
        r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+-\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        def hit(el):
            for x in split_any(el):
                x = x.strip()
                if self._DASK.match(x) or self._RAY.match(x):
                    return True
            return False
        return vocab_detect(sample_lines, hit, cap=0.9)

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._DASK.match(s)
        if m:
            g = m.groupdict()
            ts = None
            tm = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})", s)
            if tm:
                ts = parse_timestamp(tm.group(1).replace(",", "."))
            return self._event(level=g["level"], message=g["msg"],
                               source=f'distributed.{g["comp"]}', ts_ms=ts, raw=line)
        rm = self._RAY.match(s)
        if rm:
            pid = re.search(r"pid=(\d+)", s)
            return self._event(level="", message=s[rm.end():].strip() or s,
                               source="ray.worker",
                               fields={"pid": int(pid.group(1))} if pid else None,
                               category="log", raw=line)
        return None


for _a in (Ns3Adapter(), McnpAdapter(), McIterationAdapter(), StanSamplerAdapter(),
           MpichHydraAdapter(), Geant4Adapter(), MatlabDiaryAdapter(),
           RConsoleAdapter(), JuliaLoggingAdapter(), HtcondorDaemonAdapter(),
           DaskRayAdapter()):
    register_adapter(_a)
