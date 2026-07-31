"""
BATCH 9 — low-priority straggler adapters (finish the LOW non-binary tail)
================================================================================
A large, well-anchored set of low-priority formats that were still landing on
the structural / generic_ts fallbacks after batch 8b. Kept in ONE module loaded
LAST so its additions never disturb an earlier module's 1.0-confidence `before=`
ties: `detect_adapter` breaks ties by REGISTRY order (first max wins), so an
adapter registered here loses every tie to an earlier named adapter and can only
ever *gain* a sample that previously fell to `structural`/`generic_ts`.

Every adapter anchors on a distinctive token (message-id, bracketed subsystem,
epoch-in-parens, product banner, …) so it wins its own sample outright without
priority placement. The handful that ride a shared syslog silhouette register
`before="syslog"` explicitly.

Pure standard library; same unified-event contract as every other adapter.
"""
from __future__ import annotations

import re
from datetime import datetime

from ._common import (LogAdapter, register_adapter, parse_timestamp,
                      RxAdapter, vocab_detect, ratio_detect, block_ratio,
                      split_any, mk_ts, two_digit_year, us_date_ts,
                      _MONTHS, _to_ms)


# ══════════════════════════════════════════════════════════════════════════════
#  Scientific / HPC compute logs
# ══════════════════════════════════════════════════════════════════════════════

# ── Gaussian quantum-chemistry SCF log ────────────────────────────────────────
#   SCF Done:  E(RB3LYP) =  -76.4089533331     A.U. after   10 cycles
class GaussianScfAdapter(RxAdapter):
    name = "gaussian_scf"
    language = "any"
    default_source = "gaussian"
    _RE = re.compile(
        r"^\s*SCF Done:\s+E\((?P<method>[^)]+)\)\s*=\s*(?P<energy>[-\d.ED+]+)\s+"
        r"A\.U\. after\s+(?P<cycles>\d+) cycles")

    def _level(self, g, line):
        return "info"

    def _fields(self, g, line):
        return {"method": g["method"], "energy_au": g["energy"],
                "cycles": int(g["cycles"])}


# ── VASP OSZICAR convergence trace ────────────────────────────────────────────
#      1 F= -.12345678E+03 E0= -.12345678E+03  d E =-.123456E+03
class VaspOszicarAdapter(RxAdapter):
    name = "vasp_oszicar"
    language = "any"
    default_source = "vasp"
    _RE = re.compile(
        r"^\s*(?P<step>\d+)\s+F=\s*(?P<f>[-.\dE+]+)\s+E0=\s*(?P<e0>[-.\dE+]+)\s+"
        r"d E\s*=\s*(?P<de>[-.\dE+]+)")

    def _level(self, g, line):
        return "info"

    def _fields(self, g, line):
        return {"ionic_step": int(g["step"]), "free_energy": g["f"],
                "E0": g["e0"], "dE": g["de"]}


# ── CP2K electronic-structure SCF iteration table ─────────────────────────────
#     1  OT DIIS     0.15E+00    2.5     0.01234567      -155.8372914500 -1.55E+02
class Cp2kAdapter(RxAdapter):
    name = "cp2k"
    language = "any"
    default_source = "cp2k"
    _RE = re.compile(
        r"^\s*(?P<step>\d+)\s+OT\s+(?P<algo>DIIS|SD|CG|BROY|BFGS|LS)\s+"
        r"(?P<rest>[-.\dE+]+\s+.*)$")

    def _level(self, g, line):
        return "info"

    def _fields(self, g, line):
        return {"scf_step": int(g["step"]), "algo": "OT " + g["algo"]}


# ── OpenMC Monte-Carlo run log (k-effective batch table) ──────────────────────
#    Bat./Gen.  k  Average k \n ======== ... \n   1/1  1.00021
class OpenmcAdapter(LogAdapter):
    name = "openmc"
    language = "any"
    _HDR = re.compile(r"Bat\./Gen\.\s+k")
    _ROW = re.compile(r"^\s*\d+/\d+\s+[-\d.]+(?:\s+[-\d.]+)?")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            if not subs:
                return False
            good = sum(1 for x in subs if self._HDR.search(x) or self._ROW.match(x))
            has_hdr = any(self._HDR.search(x) for x in subs)
            return good >= 1 and (has_hdr or good / len(subs) >= 0.5)
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if not any(self._HDR.search(x) or self._ROW.match(x) for x in subs):
            return None
        row = next((x for x in subs if self._ROW.match(x)), None)
        return self._event(level="info",
                           message=(row.strip() if row else "k-effective batch table"),
                           source="openmc", fields={"block_lines": len(subs)},
                           category="event", raw=line)


# ══════════════════════════════════════════════════════════════════════════════
#  Debuggers / profilers
# ══════════════════════════════════════════════════════════════════════════════

# ── GDB internal 'set debug'/'set logging' output ─────────────────────────────
#   [remote] Sending packet: $g#67  /  infrun: proceed (addr=0x1234, ...)
class GdbInternalAdapter(RxAdapter):
    name = "gdb_internal"
    language = "any"
    default_source = "gdb"
    _RE = re.compile(
        r"^(?:\[(?P<dom>remote|infrun|lin-lwp|linux-nat|target|displaced|jit|nat|"
        r"record|solib|dwarf\d?|frame|py-unwind|expr|overload|remote-fd|aix-thread|"
        r"coff-pe-read|stap-expr|varobj)\]|"
        r"(?P<dom2>infrun|remote|linux-nat|target|displaced|solib):)\s+(?P<msg>.*)$")

    def _level(self, g, line):
        return "debug"

    def _fields(self, g, line):
        return {"debug_domain": g.get("dom") or g.get("dom2")}


# ── LLDB 'log enable' channel output ──────────────────────────────────────────
#   1533628814.123456 0x7fff8abc Process::Resume() sending resume
class LldbLogAdapter(LogAdapter):
    name = "lldb_log"
    language = "any"
    _TS = re.compile(r"^(?P<ts>\d{10}\.\d{6}) (?P<thread>0x[0-9a-f.]+)\s+(?P<msg>.*)$")
    _CHAN = re.compile(r"^(?P<chan>gdb-remote packets|dwarf|kdp-remote|lldb):?\s+.*"
                       r"(?:read packet:|send packet:|\$)")

    def detect(self, sample_lines):
        def hit(x):
            x = x.strip()
            return bool(self._TS.match(x) or self._CHAN.match(x))
        return ratio_detect(sample_lines, lambda el: block_ratio(el, hit))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._TS.match(s)
        if m:
            g = m.groupdict()
            return self._event(level="debug", message=g["msg"], source="lldb",
                               ts_ms=float(g["ts"]) * 1000.0,
                               fields={"thread": g["thread"]}, category="debug", raw=line)
        if self._CHAN.match(s):
            return self._event(level="debug", message=s, source="lldb.gdb-remote",
                               category="debug", raw=line)
        return None


# ── perf annotate --stdio source-annotated assembly ───────────────────────────
#     12.34 :      55c83b5a2776:  cmp    %rdx,%rcx
class PerfAnnotateAdapter(RxAdapter):
    name = "perf_annotate"
    language = "any"
    default_source = "perf.annotate"
    _RE = re.compile(
        r"^\s*(?P<pct>\d+\.\d+|)\s*:\s+(?P<addr>[0-9a-f]{4,16}):\s+(?P<insn>\S.*)$")

    def _level(self, g, line):
        return ""

    def _ts(self, g):
        return None

    def _fields(self, g, line):
        f = {"address": g["addr"], "instruction": g["insn"]}
        if g["pct"]:
            f["sample_pct"] = float(g["pct"])
        return f


# ── strace/ltrace -f multiprocess [pid N] prefix ──────────────────────────────
#   [pid 12456] read(3, "data", 4096) = 4
class StracePidPrefixAdapter(RxAdapter):
    name = "strace_pid_prefix"
    language = "any"
    default_source = "strace"
    _RE = re.compile(
        r"^\[pid\s+(?P<pid>\d+)\]\s+(?P<call>[a-zA-Z_][\w]*)\((?P<args>.*)\)\s*=\s*(?P<ret>-?\w+)")

    def _level(self, g, line):
        return "error" if str(g["ret"]).startswith("-1") else "debug"

    def _ts(self, g):
        return None

    def _fields(self, g, line):
        return {"pid": int(g["pid"]), "syscall": g["call"], "return": g["ret"]}


# ══════════════════════════════════════════════════════════════════════════════
#  Build / dev tooling
# ══════════════════════════════════════════════════════════════════════════════

# ── Homebrew verbose install output ───────────────────────────────────────────
#   ==> Downloading ... / 🍺  /opt/... / Warning: ...
class HomebrewAdapter(LogAdapter):
    name = "homebrew"
    language = "any"
    _STEP = re.compile(r"^==> ")
    _DONE = re.compile(r"^🍺")
    _WARN = re.compile(r"^(Warning|Error): ")

    def _hit(self, x):
        x = x.rstrip()
        return bool(self._STEP.match(x) or self._DONE.match(x) or self._WARN.match(x))

    def detect(self, sample_lines):
        return vocab_detect(sample_lines, lambda el: block_ratio(el, self._hit), cap=0.9)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        subs = split_any(s)
        first = next((x for x in subs if self._hit(x)), None)
        if first is None:
            return None
        f = first.rstrip()
        if self._WARN.match(f):
            lvl = "warn" if f.startswith("Warning") else "error"
            msg = f.split(": ", 1)[-1]
        elif self._DONE.match(f):
            lvl, msg = "info", f
        else:
            lvl, msg = "info", f[4:].strip()
        return self._event(level=lvl, message=msg, source="homebrew", raw=line)


# ── Poetry (Python) verbose dependency output ─────────────────────────────────
class PoetryAdapter(LogAdapter):
    name = "poetry"
    language = "python"
    _RE = re.compile(
        r"^(?:Updating dependencies|Resolving dependencies\.\.\.|"
        r"Package operations:|Writing lock file|\s+[•\-] (?:Installing|Updating|Removing|Downgrading) )")

    def detect(self, sample_lines):
        return vocab_detect(sample_lines,
                            lambda el: block_ratio(el, lambda x: bool(self._RE.match(x))),
                            cap=0.85)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        first = next((x for x in split_any(s) if self._RE.match(x)), None)
        if first is None:
            return None
        pk = re.search(r"[•\-] (Installing|Updating|Removing|Downgrading) (\S+) \(([^)]+)\)", first)
        fields = None
        if pk:
            fields = {"operation": pk.group(1), "package": pk.group(2), "version": pk.group(3)}
        return self._event(level="info", message=first.strip(), source="poetry",
                           fields=fields, raw=line)


# ── Rollup bundler output ─────────────────────────────────────────────────────
class RollupAdapter(LogAdapter):
    name = "rollup"
    language = "javascript"
    _ARROW = re.compile(r"\S+\s+→\s+\S+\.\.\.")
    _CREATED = re.compile(r"^created \S+ in \d+m?s")
    _WARN = re.compile(r"^\(!\)\s+(?P<msg>.*)$")
    _ERR = re.compile(r"^\[!\]\s+(?P<msg>.*)$")

    def _hit(self, x):
        x = x.rstrip()
        return bool(self._ARROW.match(x) or self._CREATED.match(x)
                    or self._WARN.match(x) or self._ERR.match(x))

    def detect(self, sample_lines):
        return vocab_detect(sample_lines, lambda el: block_ratio(el, self._hit), cap=0.85)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        first = next((x for x in split_any(s) if self._hit(x)), None)
        if first is None:
            return None
        f = first.rstrip()
        m = self._ERR.match(f)
        if m:
            return self._event(level="error", message=m.group("msg"), source="rollup",
                               category="error", raw=line)
        m = self._WARN.match(f)
        if m:
            return self._event(level="warn", message=m.group("msg"), source="rollup", raw=line)
        return self._event(level="info", message=f, source="rollup", raw=line)


# ── Test Kitchen (Chef) console banners ───────────────────────────────────────
class TestKitchenAdapter(LogAdapter):
    name = "test_kitchen"
    language = "ruby"
    _MAJOR = re.compile(r"^-----> ")
    _ERR = re.compile(r"^>>>>>> ")
    _WARN = re.compile(r"^\$\$\$\$\$\$ ")
    _VOCAB = re.compile(r"Test Kitchen|Creating|Converging|Verifying|Destroying|"
                        r"Starting|Finished|instance", re.I)

    def _hit(self, x):
        x = x.rstrip()
        return bool((self._MAJOR.match(x) and self._VOCAB.search(x))
                    or self._ERR.match(x) or self._WARN.match(x))

    def detect(self, sample_lines):
        return vocab_detect(sample_lines, lambda el: block_ratio(el, self._hit), cap=0.85)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        first = next((x for x in split_any(s) if self._hit(x)), None)
        if first is None:
            return None
        f = first.rstrip()
        if self._ERR.match(f):
            return self._event(level="error", message=f[7:].strip(), source="test-kitchen",
                               category="error", raw=line)
        if self._WARN.match(f):
            return self._event(level="warn", message=f[7:].strip(), source="test-kitchen", raw=line)
        return self._event(level="info", message=f[7:].strip(), source="test-kitchen", raw=line)


# ── Vagrant VAGRANT_LOG debug output ──────────────────────────────────────────
#   INFO global: Vagrant version: 1.0.5
class VagrantAdapter(RxAdapter):
    name = "vagrant"
    language = "ruby"
    _RE = re.compile(
        r"^\s*(?P<level>DEBUG|INFO|WARN|ERROR)\s+(?P<source>global|vagrant|subprocess|"
        r"ssh|machine|guest|provision|box|plugin|hook|batch|runner|command|environment|"
        r"host|communicator|winrm|synced_folders|action|bootstrap)(?:::[\w:]+)?:\s+(?P<msg>.*)$")


# ── Capistrano SSHKit / airbrussh deploy output ───────────────────────────────
#   INFO [aa11bb22] Running /usr/bin/env mkdir -p ... as deploy@example.com
class CapistranoAdapter(RxAdapter):
    name = "capistrano"
    language = "ruby"
    default_source = "capistrano"
    _RE = re.compile(
        r"^(?P<level>DEBUG|INFO|WARN|ERROR)\s+\[(?P<cmd_id>[0-9a-f]{8})\]\s+"
        r"(?P<verb>Running|Command|Finished|Uploading|Uploaded|Established)\b\s*(?P<msg>.*)$")

    def _fields(self, g, line):
        f = {"command_id": g["cmd_id"], "verb": g["verb"]}
        um = re.search(r"\bas (\S+@\S+)", line)
        if um:
            f["user_host"] = um.group(1)
        return f


# ── Buildkite job log (timestamped) ───────────────────────────────────────────
#   [2025-04-22 21:43:32.739] [CMD] $ echo 'Tests passed!'
class BuildkiteJobAdapter(RxAdapter):
    name = "buildkite_job"
    language = "any"
    default_source = "buildkite"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]\s*"
        r"(?:\[(?P<marker>CMD|GROUP)\]\s*)?(?P<msg>.*)$")

    def _level(self, g, line):
        return ""

    def _fields(self, g, line):
        return {"marker": g["marker"]} if g.get("marker") else None


# ── Travis CI fold / time markers ─────────────────────────────────────────────
#   travis_fold:start:worker_info
class TravisMarkerAdapter(RxAdapter):
    name = "travis_markers"
    language = "any"
    default_source = "travis"
    _RE = re.compile(
        r"^travis_(?P<kind>fold|time):(?P<phase>start|end):(?P<id>[\w.]+)"
        r"(?::start=\d+,finish=\d+,duration=\d+)?\s*$")

    def _level(self, g, line):
        return ""

    def _fields(self, g, line):
        return {"marker": g["kind"], "phase": g["phase"], "marker_id": g["id"]}


# ── Packer -machine-readable output ───────────────────────────────────────────
#   1498365963,,ui,say,Packer v1.0.2
class PackerMachineAdapter(RxAdapter):
    name = "packer_machine"
    language = "any"
    default_source = "packer"
    _RE = re.compile(
        r"^(?P<epoch>\d{10}),(?P<target>[^,]*),(?P<cat>ui|artifact|error|version|"
        r"artifact-count|artifact-id),(?P<sub>[^,]*),(?P<data>.*)$")

    def _ts(self, g):
        return float(g["epoch"]) * 1000.0

    def _level(self, g, line):
        return "error" if g["cat"] == "error" or g["sub"] == "error" else "info"

    def _fields(self, g, line):
        d = g["data"].replace("%!(PACKER_COMMA)", ",")
        return {"target": g["target"], "category": g["cat"], "subtype": g["sub"], "text": d}

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            m = self._m1(line)
            ev["data"]["message"] = m.group("data").replace("%!(PACKER_COMMA)", ",")
        return ev


# ══════════════════════════════════════════════════════════════════════════════
#  Datastores
# ══════════════════════════════════════════════════════════════════════════════

# ── QuestDB server log ────────────────────────────────────────────────────────
#   2023-01-19T12:01:01.190906Z I i.q.c.p.WriterPool >> [table=trades, thread=12]
class QuestdbAdapter(RxAdapter):
    name = "questdb"
    language = "java"
    default_source = "questdb"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s+(?P<lvl>[DIECA])\s+"
        r"(?P<cls>i\.q\.[\w.$]*)\s+(?P<msg>.*)$")
    _LVL = {"D": "debug", "I": "info", "E": "error", "C": "fatal", "A": "info"}

    def _level(self, g, line):
        return self._LVL.get(g["lvl"], "info")

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            ev["source"] = self._m1(line).group("cls")
        return ev


# ── OrientDB server log (colon-separated millis) ──────────────────────────────
#   2025-09-10 09:20:00:001 INFO  Backup started FULL_BACKUP [OBackupTask]
class OrientdbAdapter(RxAdapter):
    name = "orientdb"
    language = "java"
    default_source = "orientdb"
    _RE = re.compile(
        r"^(?P<yr>\d{4})-(?P<mo>\d{2})-(?P<dy>\d{2}) (?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2}):(?P<ms>\d{3})\s+"
        r"(?P<level>FINE|FINER|FINEST|CONFIG|INFO|WARNI?N?G?|SEVE?RE?|DEBUG|ERROR)\s+(?P<msg>.*?)(?:\s+\[(?P<cls>[\w.$]+)\])?$")
    _LVL = {"FINE": "debug", "FINER": "debug", "FINEST": "trace", "CONFIG": "info",
            "WARNI": "warn", "WARNING": "warn", "WARN": "warn",
            "SEVER": "error", "SEVERE": "error", "SEVE": "error", "SEVR": "error"}

    def _ts(self, g):
        return mk_ts(g["yr"], g["mo"], g["dy"], g["hh"], g["mi"], g["ss"], int(g["ms"]) * 1000)

    def _level(self, g, line):
        return self._LVL.get(g["level"], g["level"])

    def _fields(self, g, line):
        return {"java_class": g["cls"]} if g.get("cls") else None


# ══════════════════════════════════════════════════════════════════════════════
#  Networking daemons / proxies / VPN
# ══════════════════════════════════════════════════════════════════════════════

# ── accel-ppp server log ──────────────────────────────────────────────────────
#   [2023-01-15 12:00:00]:  info: ppp0: connect: ...
class AccelPppAdapter(RxAdapter):
    name = "accel_ppp"
    language = "any"
    default_source = "accel-ppp"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]:\s+"
        r"(?P<level>info|warn|error|debug|msg):\s+(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])


# ── Cacti poller log ──────────────────────────────────────────────────────────
#   2023/01/01 12:00:00 - POLLER: Poller[1] Maximum runtime ...
class CactiPollerAdapter(RxAdapter):
    name = "cacti_poller"
    language = "php"
    default_source = "cacti"
    _RE = re.compile(
        r"^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) - "
        r"(?P<cat>POLLER|SYSTEM|SPINE|CMDPHP|WEBLOG|AUTH|DSDEBUG|RRDUTIL|RECACHE|RPT|RPTDBG|RPTP)\b:?\s*(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _fields(self, g, line):
        return {"category": g["cat"]}


# ── Checkmk Micro Core / web log ──────────────────────────────────────────────
#   2023-01-01 12:00:00 [4] [client 1] request: ...
class CheckmkCmcAdapter(RxAdapter):
    name = "checkmk_cmc"
    language = "any"
    default_source = "checkmk"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(?P<prio>\d)\] \[(?P<ctx>[^\]]+)\]\s*(?P<msg>.*)$")
    _PRIO = {"0": "fatal", "1": "fatal", "2": "fatal", "3": "error",
             "4": "warn", "5": "info", "6": "info", "7": "debug"}

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return self._PRIO.get(g["prio"], "info")

    def _fields(self, g, line):
        return {"priority": int(g["prio"]), "context": g["ctx"]}


# ── Check Point OPSEC LEA pipe-delimited export ───────────────────────────────
#   loc=1234|filename=fw.log|fileid=...|time=21Mar2024 17:32:32|action=accept|...
class CheckpointLeaAdapter(LogAdapter):
    name = "checkpoint_lea"
    language = "any"
    _RE = re.compile(r"^loc=\d+\|filename=[^|]*\|fileid=\d+\|time=")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: bool(self._RE.match(str(el).strip())))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        if not self._RE.match(s):
            return None
        kv = {}
        for pair in s.split("|"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                kv[k] = v
        ts_ms = parse_timestamp(kv.get("time", "").replace("Mar", " Mar ")) \
            or bsd_lea_time(kv.get("time", ""))
        action = kv.get("action", "")
        lvl = "warn" if action in ("drop", "reject", "block") else "info"
        return self._event(level=lvl,
                           message=f'{action} {kv.get("src","")}:{kv.get("s_port","")} -> '
                                   f'{kv.get("dst","")}:{kv.get("service","")}'.strip(),
                           source="checkpoint.lea", ts_ms=ts_ms,
                           fields={k: v for k, v in kv.items()
                                   if k in ("action", "src", "dst", "service", "proto",
                                            "rule", "orig", "i/f_name", "i/f_dir", "product")},
                           category="event", raw=line)


def bsd_lea_time(t):
    m = re.match(r"^(\d{1,2})([A-Z][a-z]{2})(\d{4}) (\d{2}):(\d{2}):(\d{2})$", t or "")
    if m and m.group(2) in _MONTHS:
        dy, mon, yr, hh, mi, ss = m.groups()
        return mk_ts(yr, _MONTHS[mon], dy, hh, mi, ss)
    return None


# ── Cisco Meraki syslog (double-timestamp, syslog-prefixed) ───────────────────
#   Jan 24 12:50:00 1611234600.000 MX84 flows src=... dst=...
class CiscoMerakiFlowAdapter(RxAdapter):
    name = "cisco_meraki_flow"
    language = "any"
    default_source = "meraki"
    _RE = re.compile(
        r"^(?P<syslog>[A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2}) "
        r"(?P<epoch>\d{10}\.\d+) (?P<dev>\S+) "
        r"(?P<role>flows|urls|ids-alerts|security_event|events|airmarshal_events)\s+(?P<msg>.*)$")

    def _ts(self, g):
        return float(g["epoch"]) * 1000.0

    def _level(self, g, line):
        return "warn" if g["role"] in ("ids-alerts", "security_event") else "info"

    def _fields(self, g, line):
        return {"device": g["dev"], "role": g["role"]}


# ── PowerDNS dnsdist verbose query log ────────────────────────────────────────
#   Got query for utexas.edu|A from 128.83.10.5:0 (DoH, 53 bytes), relayed to ...
class DnsdistVerboseAdapter(LogAdapter):
    name = "dnsdist_verbose"
    language = "cpp"
    _RE = re.compile(
        r"^(?P<verb>Got query for|Packet cache (?:hit|miss)|Got answer from|"
        r"Got TCP connection from|Got timeout)\b(?P<rest>.*)$")

    def detect(self, sample_lines):
        return vocab_detect(sample_lines,
                            lambda el: block_ratio(el, lambda x: bool(self._RE.match(x.strip()))),
                            cap=0.85)

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        fields = {}
        q = re.search(r"query for (\S+?)\|(\w+) from ([\d.:a-fA-F\[\]]+)", s)
        if q:
            fields = {"qname": q.group(1), "qtype": q.group(2), "client": q.group(3)}
        return self._event(level="info", message=s, source="dnsdist",
                           fields=fields or None, raw=line)


# ── Exim per-message msglog spool file ────────────────────────────────────────
#   2026-07-20 09:10:11 Received from bob@example.com H=mail.example.com ...
class EximMsglogAdapter(LogAdapter):
    name = "exim_msglog"
    language = "any"
    _TS = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?P<msg>.*)$")
    _VOCAB = re.compile(r"Received from|\bH=|\bP=|\bS=\d|\bR=|\bT=|defer|Completed|"
                        r"frozen|SMTP error|=> |-> |\*\* ")

    def detect(self, sample_lines):
        def hit(x):
            m = self._TS.match(x.strip())
            return bool(m) and self._VOCAB.search(m.group("msg")) is not None
        return vocab_detect(sample_lines, lambda el: block_ratio(el, hit), cap=0.8)

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._TS.match(s)
        if not m or not self._VOCAB.search(m.group("msg")):
            return None
        msg = m.group("msg")
        lvl = "error" if re.search(r"\*\*|frozen|error", msg) else (
            "warn" if "defer" in msg else "info")
        return self._event(level=lvl, message=msg, source="exim.msglog",
                           ts_ms=parse_timestamp(m.group("ts")), category="event", raw=line)


# ── frp fast reverse proxy log ────────────────────────────────────────────────
#   2023-01-15 12:00:00.123 [I] [service.go:301] login to server success, ...
class FrpAdapter(RxAdapter):
    name = "frp"
    language = "go"
    default_source = "frp"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) \[(?P<lvl>[TDIWE])\] "
        r"\[(?P<loc>[\w./]+\.go:\d+)\]\s*(?P<msg>.*)$")
    _LVL = {"T": "trace", "D": "debug", "I": "info", "W": "warn", "E": "error"}

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return self._LVL.get(g["lvl"], "info")

    def _fields(self, g, line):
        return {"source_loc": g["loc"]}


# ── Graphite carbon (Twisted) log ─────────────────────────────────────────────
#   01/01/2023 12:00:00 :: MetricLineReceiver connection ...
class GraphiteCarbonAdapter(RxAdapter):
    name = "graphite_carbon"
    language = "python"
    default_source = "carbon"
    _RE = re.compile(
        r"^(?P<dy>\d{2})/(?P<mo>\d{2})/(?P<yr>\d{4}) (?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2}) :: (?P<msg>.*)$")

    def _ts(self, g):
        return mk_ts(g["yr"], g["mo"], g["dy"], g["hh"], g["mi"], g["ss"])

    def _level(self, g, line):
        return "error" if re.search(r"error|exception|traceback", g.get("msg", ""), re.I) else "info"


# ── Heimdal KDC log ───────────────────────────────────────────────────────────
#   2024-01-12T10:11:12 AS-REQ jdoe@EXAMPLE.ORG from IPv4:192.0.2.10 for ...
class HeimdalKdcAdapter(RxAdapter):
    name = "heimdal_kdc"
    language = "any"
    default_source = "heimdal.kdc"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s+(?P<req>AS-REQ|TGS-REQ)\s+"
        r"(?P<client>\S+@\S+)\s+from\s+(?P<transport>IPv4|IPv6):(?P<addr>\S+)\s+for\s+(?P<service>\S+)")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return "info"

    def _fields(self, g, line):
        return {"request": g["req"], "client_principal": g["client"],
                "address": g["addr"], "service_principal": g["service"]}


# ── Juju controller / agent debug-log ─────────────────────────────────────────
#   machine-0: 01:56:55 DEBUG juju.worker.dependency "..." manifold worker started
class JujuDebugAdapter(RxAdapter):
    name = "juju_debug"
    language = "go"
    default_source = "juju"
    _RE = re.compile(
        r"^(?P<entity>machine-\d+|unit-[\w-]+-\d+|controller-\d+): (?P<time>\d{2}:\d{2}:\d{2}) "
        r"(?P<level>TRACE|DEBUG|INFO|WARNING|ERROR) (?P<module>juju[\w.]*|unit\.[\w.-]+)\s*(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["time"])

    def _fields(self, g, line):
        return {"entity": g["entity"], "module": g["module"]}


# ── MIT Kerberos kadmind log ──────────────────────────────────────────────────
#   Jul 30 23:20:01 kdc1 kadmind[10560](Notice): Request: kadm5_init, ...
class KadmindAdapter(RxAdapter):
    name = "kadmind"
    language = "any"
    default_source = "kadmind"
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2}) (?P<host>\S+) "
        r"kadmind\[(?P<pid>\d+)\]\((?P<level>Notice|info|Error|Warning)\):\s+(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _fields(self, g, line):
        return {"host": g["host"], "pid": int(g["pid"])}


# ── Knot DNS server (knotd) log ───────────────────────────────────────────────
#   2026-07-20T10:20:30+0200 info: [example.com.] zone loaded, serial ...
class KnotDnsAdapter(RxAdapter):
    name = "knot_dns"
    language = "any"
    default_source = "knotd"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{4}|Z)?)\s+"
        r"(?P<level>critical|error|warning|notice|info|debug):\s+"
        r"(?:\[(?P<zone>[\w.\-]+\.)\]\s+)?(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _fields(self, g, line):
        return {"zone": g["zone"]} if g.get("zone") else None


# ── Knot Resolver (kresd) log ─────────────────────────────────────────────────
#   [system] error while loading config: ...
class KnotResolverAdapter(RxAdapter):
    name = "knot_resolver"
    language = "any"
    default_source = "kresd"
    _GROUPS = ("system", "cache", "io", "net", "tls", "gnutls", "plan", "iterat",
               "valdtr", "resolv", "select", "zoncut", "cookie", "statis", "rules",
               "prlayr", "dnssec", "hint", "dnstap", "devel", "http", "nsrep",
               "reqdbg", "worker", "groupc", "timjmp", "xdp", "doh", "policy")
    _RE = re.compile(
        r"^(?:\[(?P<reqid>[\d.]+)\])?\[(?P<group>" + "|".join(_GROUPS) + r")\]\s*(?P<msg>.*)$")

    def _level(self, g, line):
        return "error" if re.search(r"\berror\b|fail", g.get("msg", ""), re.I) else "info"

    def _fields(self, g, line):
        f = {"log_group": g["group"]}
        if g.get("reqid"):
            f["request_id"] = g["reqid"]
        return f

    def detect(self, sample_lines):
        return vocab_detect(sample_lines,
                            lambda el: block_ratio(el, self._hit), cap=0.85)


# ── Munin update/graph log ────────────────────────────────────────────────────
#   2023/01/01 00:05:01 [INFO]: Starting munin-update
class MuninAdapter(RxAdapter):
    name = "munin"
    language = "perl"
    default_source = "munin"
    _RE = re.compile(
        r"^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) "
        r"\[(?P<level>DEBUG|INFO|WARNING|ERROR|PERROR|FATAL)\]:?\s*(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return {"PERROR": "error"}.get(g["level"], g["level"])


# ── msmtp client logfile ──────────────────────────────────────────────────────
#   Aug 11 23:50:40 host=smtp.googlemail.com tls=on ... exitcode=EX_OK
class MsmtpAdapter(LogAdapter):
    name = "msmtp"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2}) (?P<rest>host=\S+ .*exitcode=EX_\w+)\s*$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: bool(self._RE.match(str(el).strip())))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        rest = m.group("rest")
        kv = dict(re.findall(r"(\w+)=('[^']*'|\S+)", rest))
        exit_ok = kv.get("exitcode") == "EX_OK"
        return self._event(level="info" if exit_ok else "error",
                           message=f'{kv.get("from","")} -> {kv.get("recipients","")}'
                                   f' {kv.get("smtpstatus","")}'.strip(),
                           source="msmtp", ts_ms=parse_timestamp(m.group("ts")),
                           fields={k: v.strip("'") for k, v in kv.items()
                                   if k in ("host", "from", "recipients", "smtpstatus",
                                            "tls", "auth", "mailsize", "exitcode")},
                           category="event" if exit_ok else "error", raw=line)


# ── NetBird client/agent log ──────────────────────────────────────────────────
#   2023-01-15T12:00:00Z INFO client/internal/engine.go:397: adding peer ...
class NetbirdAdapter(RxAdapter):
    name = "netbird"
    language = "go"
    default_source = "netbird"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?) "
        r"(?P<level>TRACE|DEBUG|INFO|WARN|WARNING|ERROR)\s+"
        r"(?P<loc>[\w./\-]+\.go:\d+):\s+(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _fields(self, g, line):
        return {"source_loc": g["loc"]}


# ── opendnp3 ConsoleLogger ────────────────────────────────────────────────────
#   ms(1667241735775) INFO    manager - Detected concurrency of 8
class Opendnp3Adapter(RxAdapter):
    name = "opendnp3"
    language = "cpp"
    _RE = re.compile(
        r"^ms\((?P<epoch>\d{10,})\)\s+(?P<level>TRACE|DEBUG|INFO|WARN|ERROR)\s+"
        r"(?P<logger>\S+)\s+-\s+(?P<msg>.*)$")

    def _ts(self, g):
        return float(g["epoch"])

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            ev["source"] = self._m1(line).group("logger")
        return ev


# ── Pritunl VPN server log ────────────────────────────────────────────────────
#   [apollo][2023-01-15 12:00:00,123][INFO] Starting vpn server
class PritunlAdapter(RxAdapter):
    name = "pritunl"
    language = "python"
    default_source = "pritunl"
    _RE = re.compile(
        r"^\[(?P<host>[^\]]+)\]\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\]"
        r"\[(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\]\s*(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"].replace(",", "."))

    def _fields(self, g, line):
        return {"host": g["host"]}


# ── racoon IKEv1 daemon (ipsec-tools) log ─────────────────────────────────────
#   racoon: INFO: ISAKMP-SA established ...
class RacoonAdapter(RxAdapter):
    name = "racoon"
    language = "any"
    default_source = "racoon"
    _RE = re.compile(
        r"^racoon: (?P<level>INFO|ERROR|WARNING|DEBUG|DEBUG2|NOTIFY):\s+(?P<msg>.*)$")

    def _level(self, g, line):
        return {"NOTIFY": "info", "DEBUG2": "debug"}.get(g["level"], g["level"])


# ── socat relay log ───────────────────────────────────────────────────────────
#   2023/01/15 12:00:00 socat[9876] E connect(5, ...): Connection refused
class SocatAdapter(RxAdapter):
    name = "socat"
    language = "any"
    default_source = "socat"
    _RE = re.compile(
        r"^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) socat\[(?P<pid>\d+)\] "
        r"(?P<lvl>[DINWEF]) (?P<msg>.*)$")
    _LVL = {"D": "debug", "I": "info", "N": "info", "W": "warn", "E": "error", "F": "fatal"}

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return self._LVL.get(g["lvl"], "info")

    def _fields(self, g, line):
        return {"pid": int(g["pid"])}


# ── TACACS+ accounting log (tac_plus / Cisco ACS; space- or tab-delimited) ────
#   Oct 10 12:04:01 10.1.1.1 fred tty1 203.0.113.7 start task_id=1 service=shell ...
#   Jan 24 12:55:00\talice\ttty2\t203.0.113.5\tstop\ttask_id=7\t...
class TacacsAccountingAdapter(LogAdapter):
    name = "tacacs_accounting"
    language = "any"
    # Two field counts exist: tac_plus (NAS-ip user tty rem-addr) and the tab
    # ACS form (user tty rem-addr). Capture the variable middle fields lazily,
    # then the start/stop/update record type and the task_id-bearing AVPs.
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2})[\t ]+"
        r"(?P<mid>\S.*?)[\t ]+(?P<rec>start|stop|update)\b[\t ]+(?P<avps>.*task_id=.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: bool(self._RE.match(str(el).strip())))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        g = m.groupdict()
        avps = dict(re.findall(r"(\w+)=([^\t]+?)(?=\t\w+=|\t*$)", g["avps"]))
        cmd = avps.get("cmd", "")
        mid = re.split(r"[\t ]+", g["mid"])
        nas = mid[0]
        user = next((f for f in mid if "." not in f and ":" not in f), mid[-1] if mid else "")
        return self._event(level="info", message=(cmd or g["rec"]),
                           source=user or nas,
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"fields": mid, "record_type": g["rec"],
                                   "task_id": avps.get("task_id"),
                                   "service": avps.get("service"), "command": cmd or None},
                           category="event", raw=line)


# ── V2Ray / Xray access log ───────────────────────────────────────────────────
#   2023/01/15 12:00:00 203.0.113.5:50624 accepted tcp:example.com:443 [socks -> direct]
class V2rayAdapter(RxAdapter):
    name = "v2ray"
    language = "go"
    default_source = "v2ray"
    _RE = re.compile(
        r"^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) (?P<client>[\d.:a-fA-F\[\]]+:\d+) "
        r"(?P<verdict>accepted|rejected) (?P<dest>[\w.:\-]+) \[(?P<inbound>[^\]]+) -> (?P<outbound>[^\]]*)\]")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return "warn" if g["verdict"] == "rejected" else "info"

    def _fields(self, g, line):
        return {"client": g["client"], "verdict": g["verdict"], "destination": g["dest"],
                "inbound": g["inbound"], "outbound": g["outbound"]}


# ── xl2tpd L2TP daemon log ────────────────────────────────────────────────────
#   xl2tpd[1234]: control_finish: Connection established to ...
class Xl2tpdAdapter(RxAdapter):
    name = "xl2tpd"
    language = "any"
    default_source = "xl2tpd"
    _RE = re.compile(r"^xl2tpd\[(?P<pid>\d+)\]:\s+(?P<func>[a-z_][\w]*):\s+(?P<msg>.*)$")

    def _level(self, g, line):
        return "error" if re.search(r"error|fail|refused|unable", g.get("msg", ""), re.I) else "info"

    def _fields(self, g, line):
        return {"pid": int(g["pid"]), "function": g["func"]}


# ── Dynatrace OneAgent log ────────────────────────────────────────────────────
#   2023-01-01 12:00:00.123 UTC [c0ffee01] info    [native] Agent version ...
class DynatraceOneagentAdapter(RxAdapter):
    name = "dynatrace_oneagent"
    language = "any"
    default_source = "dynatrace.oneagent"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) UTC \[(?P<ctx>[0-9a-f]+)\] "
        r"(?P<level>finest|finer|fine|info|warning|severe)\s+\[(?P<module>[^\]]+)\]\s*(?P<msg>.*)$")
    _LVL = {"finest": "trace", "finer": "debug", "fine": "debug", "info": "info",
            "warning": "warn", "severe": "error"}

    def _ts(self, g):
        return parse_timestamp(g["ts"] + "Z")

    def _level(self, g, line):
        return self._LVL.get(g["level"], "info")

    def _fields(self, g, line):
        return {"context": g["ctx"], "module": g["module"]}


# ══════════════════════════════════════════════════════════════════════════════
#  VPN / security
# ══════════════════════════════════════════════════════════════════════════════

# ── OpenConnect client stderr ─────────────────────────────────────────────────
#   Established DTLS connection (using GnuTLS). ...
class OpenconnectAdapter(LogAdapter):
    name = "openconnect"
    language = "any"
    _RE = re.compile(
        r"^(?:Connected to HTTPS on|SSL negotiation with|Established DTLS connection|"
        r"Got CONNECT response:|Session authentication will expire at|"
        r"Connected as |Configured as |Established EAP-|Attempting to connect|"
        r"POST https://|Got HTTP response:|Failed to |Server certificate)\b")

    def detect(self, sample_lines):
        return vocab_detect(sample_lines,
                            lambda el: block_ratio(el, lambda x: bool(self._RE.match(x.strip()))),
                            cap=0.8)

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        if not self._RE.match(s):
            return None
        lvl = "error" if s.startswith("Failed") else "info"
        return self._event(level=lvl, message=s, source="openconnect", raw=line)


# ── OpenVPN Access Server (Twisted-wrapped) log ───────────────────────────────
#   2019-12-13 10:19:19+0100 [-] OVPN 0 OUT: '...'
class OpenvpnAsAdapter(RxAdapter):
    name = "openvpn_as"
    language = "python"
    default_source = "openvpnas"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{4}) "
        r"\[(?P<src>-|stdout#\w+|stderr#\w+|[\w.]+)\]\s+(?P<msg>.*)$")

    def _ts(self, g):
        t = g["ts"]
        return parse_timestamp(t[:19] + t[19].replace("+", "+").replace("-", "-")
                               + t[20:22] + ":" + t[22:]) if len(t) >= 24 else parse_timestamp(t)

    def _fields(self, g, line):
        return {"twisted_source": g["src"]}


# ── wg-quick up/down script command echo ──────────────────────────────────────
#   [#] ip link add wg0 type wireguard
class WgQuickAdapter(LogAdapter):
    name = "wg_quick"
    language = "any"
    _RE = re.compile(r"^\[#\]\s+(?P<cmd>(?:ip|wg|wg-quick|resolvconf|iptables|ip6tables|"
                     r"nft|sysctl|nmcli|route|ifconfig)\b.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: block_ratio(el, lambda x: bool(self._RE.match(x.strip()))))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        return self._event(level="debug", message=m.group("cmd"), source="wg-quick",
                           fields={"command": m.group("cmd")}, category="debug", raw=line)


# ── WireGuard for Windows ringlogger export ───────────────────────────────────
#   2021-01-20 10:57:18.116: [TUN] [mytunnel] Starting WireGuard/0.3.4 ...
class WireguardWinAdapter(RxAdapter):
    name = "wireguard_windows"
    language = "any"
    default_source = "wireguard"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}): \[(?P<comp>TUN|MGR)\] "
        r"\[(?P<tunnel>[^\]]+)\] (?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _fields(self, g, line):
        return {"component": g["comp"], "tunnel": g["tunnel"]}


# ── ClamAV clamd/clamscan detection line ──────────────────────────────────────
#   Mon May  1 12:00:00 2023 -> /home/alice/x.exe: Win.Trojan.Agent-123456 FOUND
class ClamavAdapter(LogAdapter):
    name = "clamav"
    language = "any"
    _RE = re.compile(
        r"^(?:(?P<ts>[A-Z][a-z]{2} [A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2} \d{4}) -> )?"
        r"(?P<path>.+?): (?P<verdict>\S+ FOUND|OK|Empty file|Access denied)\s*$")

    def detect(self, sample_lines):
        def hit(x):
            return bool(self._RE.match(x.strip())) and (" FOUND" in x or x.rstrip().endswith(("OK", "file")))
        return vocab_detect(sample_lines, lambda el: block_ratio(el, hit), cap=0.85)

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        g = m.groupdict()
        found = g["verdict"].endswith("FOUND")
        sig = g["verdict"][:-6].strip() if found else None
        return self._event(level="error" if found else "info",
                           message=f'{g["path"]}: {g["verdict"]}', source="clamav",
                           ts_ms=parse_timestamp(g["ts"]) if g.get("ts") else None,
                           fields={"path": g["path"], "signature": sig,
                                   "found": found}, category="error" if found else "event",
                           raw=line)


# ── AIDE / Tripwire file-integrity report block ───────────────────────────────
class FileIntegrityAdapter(LogAdapter):
    name = "file_integrity"
    language = "any"
    _SECTION = re.compile(r"^(Added|Removed|Changed)( entries)?:\s*(?P<path>\S.*)?$")
    _MASK = re.compile(r"^(?P<mask>[fpdlcbsD][+.:_ ]{6,})\s+(?P<path>/\S+)")
    _FILE = re.compile(r"^File:\s+/\S+")

    def _hit(self, x):
        x = x.rstrip()
        return bool(self._SECTION.match(x) or self._MASK.match(x) or self._FILE.match(x))

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            if not subs:
                return False
            has_verb = any(self._SECTION.match(x.rstrip()) for x in subs)
            good = sum(1 for x in subs if self._hit(x))
            return has_verb and good >= 1
        return vocab_detect(sample_lines, hit, cap=0.85)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        subs = split_any(s)
        actions = []
        for x in subs:
            m = self._SECTION.match(x.rstrip())
            if m:
                actions.append(m.group(1))
        if not actions and not any(self._hit(x) for x in subs):
            return None
        changed = any(a in ("Added", "Removed", "Changed") for a in actions)
        return self._event(level="warn" if changed else "info",
                           message="file-integrity report: " + ", ".join(actions or ["change"]),
                           source="file-integrity",
                           fields={"actions": actions, "block_lines": len(subs)},
                           category="event", raw=line)


# ── SimpleSAMLphp log ─────────────────────────────────────────────────────────
#   Jul 20 11:03:01 simplesamlphp INFO [b3a5d2f1c4] AuthState: return URL is ...
class SimplesamlphpAdapter(RxAdapter):
    name = "simplesamlphp"
    language = "php"
    default_source = "simplesamlphp"
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2}) \S+ "
        r"(?P<level>EMERG|ALERT|CRIT|ERR|ERROR|WARNING|NOTICE|INFO|DEBUG|STAT) "
        r"\[(?P<trackid>[0-9a-f]{6,})\]\s*(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return {"STAT": "info"}.get(g["level"], g["level"])

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            ev["trace_id"] = self._m1(line).group("trackid")
        return ev


# ── PingFederate audit.log (pipe-delimited, tid: field) ───────────────────────
#   2024-11-28 05:58:55,832| tid:aBcD1234| AUTHN_ATTEMPT| jdoe| ...
class PingfederateAuditAdapter(LogAdapter):
    name = "pingfederate_audit"
    language = "java"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\|\s*tid:(?P<tid>\S+)\|\s*(?P<rest>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: bool(self._RE.match(str(el).strip())))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        cols = [c.strip() for c in m.group("rest").split("|")]
        event = cols[0] if cols else ""
        status = cols[8] if len(cols) > 8 else ""
        lvl = "warn" if status and status.lower() not in ("success", "") else "info"
        return self._event(level=lvl, message=f'{event} {status}'.strip(),
                           source="pingfederate.audit",
                           ts_ms=parse_timestamp(m.group("ts").replace(",", ".")),
                           trace_id=m.group("tid"),
                           fields={"event": event, "subject": cols[1] if len(cols) > 1 else None,
                                   "status": status}, category="event", raw=line)


# ── Apereo CAS audit trail (inspektr) multi-line block ────────────────────────
class CasAuditAdapter(LogAdapter):
    name = "cas_audit"
    language = "java"
    _BEGIN = re.compile(r"^Audit trail record BEGIN")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            return bool(subs) and self._BEGIN.match(subs[0].strip()) is not None
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if not subs or not self._BEGIN.match(subs[0].strip()):
            return None
        kv = {}
        for x in subs:
            m = re.match(r"^(WHO|WHAT|ACTION|APPLICATION|WHEN|CLIENT IP ADDRESS|SERVER IP ADDRESS):\s*(.*)$", x.strip())
            if m:
                kv[m.group(1)] = m.group(2)
        action = kv.get("ACTION", "")
        lvl = "warn" if "FAIL" in action.upper() else "info"
        return self._event(level=lvl, message=action or "CAS audit event",
                           source="cas.audit", ts_ms=parse_timestamp(kv.get("WHEN", "")),
                           fields={"who": kv.get("WHO"), "action": action,
                                   "application": kv.get("APPLICATION"),
                                   "client_ip": kv.get("CLIENT IP ADDRESS")},
                           category="event", raw=line)


# ── Sophos Anti-Virus on-access scan log ──────────────────────────────────────
#   2022-07-11T20:33:24.000Z sophos_scanner: Virus/spyware '...' detected in ...
class SophosSavAdapter(RxAdapter):
    name = "sophos_sav"
    language = "any"
    default_source = "sophos"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)\s+sophos\w*:\s+(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return "error" if re.search(r"detected|quarantin|virus|spyware|threat", g.get("msg", ""), re.I) else "info"

    def _fields(self, g, line):
        f = {}
        t = re.search(r"'([^']+)' detected in (\S+)", g.get("msg", ""))
        if t:
            f = {"threat": t.group(1), "path": t.group(2)}
        return f or None


# ── McAfee/Trellix ENS on-access activity log (tab, US-locale ts) ─────────────
#   7/11/2022 8:33:24 PM<TAB>Blocked by Access Protection rule<TAB>CORP\jdoe<TAB>...
class McafeeEnsAdapter(LogAdapter):
    name = "mcafee_ens"
    language = "any"
    _RE = re.compile(
        r"^(?P<date>\d{1,2}/\d{1,2}/\d{4}) (?P<time>\d{1,2}:\d{2}:\d{2} [AP]M)\t(?P<rest>.+)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: bool(self._RE.match(str(el).rstrip("\r\n"))))

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        m = self._RE.match(s)
        if not m:
            return None
        cols = m.group("rest").split("\t")
        action = cols[0] if cols else ""
        threat = cols[3] if len(cols) > 3 else None
        return self._event(level="warn" if re.search(r"block|detect|threat", action, re.I) else "info",
                           message=action, source="mcafee.ens",
                           ts_ms=us_date_ts(m.group("date"), m.group("time")),
                           fields={"action": action, "user": cols[1] if len(cols) > 1 else None,
                                   "path": cols[2] if len(cols) > 2 else None,
                                   "threat": threat}, category="event", raw=line)


# ── VMware vCenter Appliance applmgmt authorization audit ─────────────────────
#   2022-02-11T09:01:44.734: INFO Authorization Result: User=..., priv=..., authorized=True
class VcsaApplmgmtAdapter(RxAdapter):
    name = "vcsa_applmgmt_audit"
    language = "any"
    default_source = "vcsa.applmgmt"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+): (?P<level>INFO|WARN|ERROR|DEBUG) "
        r"Authorization Result:\s+(?P<rest>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _fields(self, g, line):
        f = {}
        for k in ("User", "priv", "authorized"):
            mm = re.search(rf"{k}=([^,]+)", g["rest"])
            if mm:
                f[k.lower()] = mm.group(1).strip()
        return f


# ── Palo Alto GlobalProtect client (PanGPS/PanGPA) log ────────────────────────
#   (T1234)Debug(123): 01/15/23 12:00:00:123 Network discovery event received
class GlobalprotectPangpsAdapter(RxAdapter):
    name = "globalprotect_pangps"
    language = "any"
    default_source = "globalprotect.client"
    _RE = re.compile(
        r"^\(T(?P<tid>\d+)\)(?P<level>Debug|Info|Warning|Error|Dump)\((?P<line>\d+)\): "
        r"(?P<mo>\d{2})/(?P<dy>\d{2})/(?P<yr>\d{2}) (?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2}):(?P<ms>\d{3}) (?P<msg>.*)$")

    def _ts(self, g):
        return mk_ts(2000 + int(g["yr"]), g["mo"], g["dy"], g["hh"], g["mi"], g["ss"], int(g["ms"]) * 1000)

    def _fields(self, g, line):
        return {"thread": int(g["tid"]), "source_line": int(g["line"])}


# ══════════════════════════════════════════════════════════════════════════════
#  Cloud / AWS
# ══════════════════════════════════════════════════════════════════════════════

# ── AWS CloudFormation cfn-init helper log ────────────────────────────────────
#   2016-05-27 00:46:20,008 [INFO] Command berk succeeded
class CfnInitAdapter(RxAdapter):
    name = "cfn_init"
    language = "python"
    default_source = "cfn-init"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
        r"\[(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\]\s+(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"].replace(",", "."))


# ── AWS Global Accelerator flow log ───────────────────────────────────────────
#   aga-flow-log 1.0 123456789012 arn:aws:globalaccelerator::...
class AwsGlobalAcceleratorFlowAdapter(LogAdapter):
    name = "aws_global_accelerator_flow"
    language = "any"
    _RE = re.compile(r"^aga-flow-log \S+ \d+ arn:aws:globalaccelerator")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: bool(self._RE.match(str(el).strip())))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        if not self._RE.match(s):
            return None
        f = s.split()
        ts_ms = None
        if len(f) > 12 and f[12].isdigit():
            ts_ms = float(f[12]) * 1000.0
        return self._event(level="info",
                           message=f'{f[4]}:{f[5]} -> {f[6]}:{f[7]}' if len(f) > 7 else s,
                           source="aws.global-accelerator", ts_ms=ts_ms,
                           fields={"version": f[1], "account": f[2],
                                   "client_ip": f[4] if len(f) > 4 else None,
                                   "endpoint_ip": f[6] if len(f) > 6 else None},
                           category="event", raw=line)


# ── AWS Transit Gateway flow log (v6) ─────────────────────────────────────────
#   6 123456789012 tgw-... tgw-attach-... ... OK IPv4 100 - - ingress
class AwsTransitGatewayFlowAdapter(LogAdapter):
    name = "aws_transit_gateway_flow"
    language = "any"
    _RE = re.compile(r"^\d+ \d{12} tgw-[0-9a-f]+ tgw-attach-[0-9a-f]+ ")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: bool(self._RE.match(str(el).strip())))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        if not self._RE.match(s):
            return None
        f = s.split()
        status = f[-4] if len(f) >= 4 else ""
        ts_ms = None
        for tok in f:
            if tok.isdigit() and len(tok) == 10:
                ts_ms = float(tok) * 1000.0
                break
        return self._event(level="warn" if status in ("NODATA", "SKIPDATA") else "info",
                           message=f'tgw flow {status}', source="aws.transit-gateway",
                           ts_ms=ts_ms,
                           fields={"tgw_id": f[2], "tgw_attachment_id": f[3],
                                   "log_status": status,
                                   "flow_direction": f[-1] if f else None},
                           category="event", raw=line)


# ── AWS Route 53 public hosted-zone query log ─────────────────────────────────
#   1.0 2017-12-13T08:15:50.235Z Z123412341234 example.com A NOERROR UDP JFK5 ...
class Route53QueryAdapter(RxAdapter):
    name = "route53_query"
    language = "any"
    default_source = "route53"
    _RE = re.compile(
        r"^(?P<ver>\d+\.\d+) (?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z) "
        r"(?P<zone>Z[A-Z0-9]+) (?P<qname>\S+) (?P<qtype>\w+) (?P<rcode>\w+) "
        r"(?P<proto>UDP|TCP) (?P<edge>[A-Z]{3}\d+) (?P<resolver>\S+)(?: (?P<ecs>\S+))?\s*$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return "warn" if g["rcode"] not in ("NOERROR", "NODATA") else "info"

    def _fields(self, g, line):
        return {"hosted_zone": g["zone"], "qname": g["qname"], "qtype": g["qtype"],
                "rcode": g["rcode"], "protocol": g["proto"], "edge_location": g["edge"],
                "resolver_ip": g["resolver"]}


# ── Apache OpenWhisk activation log ───────────────────────────────────────────
#   2016-09-21T12:03:35.619234386Z stdout: Hello World
class OpenwhiskActivationAdapter(RxAdapter):
    name = "openwhisk_activation"
    language = "any"
    default_source = "openwhisk"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z) (?P<stream>stdout|stderr): (?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return "error" if g["stream"] == "stderr" else "info"

    def _fields(self, g, line):
        return {"stream": g["stream"]}


# ── RDS MySQL/MariaDB Audit Plugin (SERVER_AUDIT) CSV ─────────────────────────
#   20221024 18:51:12,ip-...,rdsadmin,localhost,12,0,QUERY,mysql,'SELECT 1',0
class RdsMariadbAuditAdapter(LogAdapter):
    name = "rds_mariadb_audit"
    language = "any"
    _RE = re.compile(
        r"^(?P<date>\d{8}) (?P<time>\d{2}:\d{2}:\d{2}),(?P<host>[^,]*),(?P<user>[^,]*),"
        r"(?P<chost>[^,]*),(?P<connid>\d+),(?P<qid>\d+),"
        r"(?P<op>CONNECT|QUERY|DISCONNECT|FAILED_CONNECT|TABLE|WRITE|READ|CREATE|DROP|ALTER),")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: bool(self._RE.match(str(el).strip())))

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        m = self._RE.match(s.strip())
        if not m:
            return None
        g = m.groupdict()
        d = g["date"]
        ts_ms = parse_timestamp(f"{d[:4]}-{d[4:6]}-{d[6:8]} {g['time']}")
        lvl = "warn" if g["op"] == "FAILED_CONNECT" else "info"
        return self._event(level=lvl, message=f'{g["op"]} by {g["user"]}',
                           source="rds.audit", ts_ms=ts_ms,
                           fields={"user": g["user"], "host": g["host"],
                                   "operation": g["op"], "connection_id": int(g["connid"])},
                           category="event", raw=line)


# ══════════════════════════════════════════════════════════════════════════════
#  Backup
# ══════════════════════════════════════════════════════════════════════════════

# ── BackupPC server LOG ───────────────────────────────────────────────────────
#   2026-07-21 02:00:01 full backup started for directory /etc (baseline ...)
class BackuppcAdapter(RxAdapter):
    name = "backuppc"
    language = "perl"
    default_source = "backuppc"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
        r"(?P<msg>(?:full|incr|incremental)? ?backup (?:started|complete|aborted).*|"
        r"Backup aborted.*|Aborting backup.*|admin.*|Running .*|Finished .*|"
        r"Started .* on .*|Reading .*|Got fatal error.*|removing .*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        m = g.get("msg", "")
        return "error" if re.search(r"abort|fatal|fail", m, re.I) else "info"


# ── restic default console summary output ─────────────────────────────────────
#   Files:          25 new,    10 changed,   965 unmodified
class ResticTextAdapter(LogAdapter):
    name = "restic_text"
    language = "any"
    _SUMMARY = re.compile(r"^(?P<kind>Files|Dirs):\s+(?P<new>\d+) new,\s+(?P<chg>\d+) changed,\s+(?P<un>\d+) unmodified")
    _SNAP = re.compile(r"^snapshot [0-9a-f]{8} saved")
    _REPO = re.compile(r"^repository [0-9a-f]{8} opened")
    _ADDED = re.compile(r"^Added to the repository:")

    def _hit(self, x):
        x = x.rstrip()
        return bool(self._SUMMARY.match(x) or self._SNAP.match(x) or self._REPO.match(x) or self._ADDED.match(x))

    def detect(self, sample_lines):
        return vocab_detect(sample_lines, lambda el: block_ratio(el, self._hit), cap=0.85)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        first = next((x for x in split_any(s) if self._hit(x)), None)
        if first is None:
            return None
        f = {}
        m = self._SUMMARY.match(first.rstrip())
        if m:
            f = {"kind": m.group("kind"), "new": int(m.group("new")),
                 "changed": int(m.group("chg")), "unmodified": int(m.group("un"))}
        return self._event(level="info", message=first.strip(), source="restic",
                           fields=f or None, category="event", raw=line)


# ── rsnapshot logfile ─────────────────────────────────────────────────────────
#   [21/Jul/2026:02:00:01] /usr/bin/rsnapshot daily: started
class RsnapshotAdapter(RxAdapter):
    name = "rsnapshot"
    language = "perl"
    default_source = "rsnapshot"
    _RE = re.compile(
        r"^\[(?P<dy>\d{2})/(?P<mon>[A-Z][a-z]{2})/(?P<yr>\d{4}):(?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})\] "
        r"(?P<msg>(?:\S*rsnapshot\b.*|/\S+.*|ERROR:.*|WARNING:.*))$")

    def _ts(self, g):
        if g["mon"] not in _MONTHS:
            return None
        return mk_ts(g["yr"], _MONTHS[g["mon"]], g["dy"], g["hh"], g["mi"], g["ss"])

    def _level(self, g, line):
        m = g.get("msg", "")
        if m.startswith("ERROR"):
            return "error"
        if m.startswith("WARNING"):
            return "warn"
        return "info"


# ── Arcserve UDP activity log (CSV export) ────────────────────────────────────
#   Information,7/21/2026 2:00:01 AM,host1,"...",Backup,123
class ArcserveUdpAdapter(RxAdapter):
    name = "arcserve_udp"
    language = "any"
    default_source = "arcserve"
    _RE = re.compile(
        r"^(?P<sev>Information|Warning|Error|Critical),(?P<date>\d{1,2}/\d{1,2}/\d{4}) "
        r"(?P<time>\d{1,2}:\d{2}:\d{2} [AP]M),(?P<node>[^,]*),(?P<msg>\".*?\"|[^,]*),(?P<jtype>[^,]*),(?P<jid>\d+)\s*$")
    _LVL = {"Information": "info", "Warning": "warn", "Error": "error", "Critical": "fatal"}

    def _ts(self, g):
        return us_date_ts(g["date"], g["time"])

    def _level(self, g, line):
        return self._LVL.get(g["sev"], "info")

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            m = self._m1(line)
            ev["data"]["message"] = m.group("msg").strip('"')
            ev["source"] = m.group("node") or "arcserve"
            ev["trace_id"] = m.group("jid")
            ev["data"]["job_type"] = m.group("jtype")
        return ev


# ── Veritas Backup Exec job log (XML) ─────────────────────────────────────────
class VeritasBexAdapter(LogAdapter):
    name = "veritas_bex"
    language = "any"
    _RE = re.compile(r"<job_log>|<machine_name>|<backup_set>|<completed_status>")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: str(el).lstrip().startswith("<job_log>")
                            or ("<machine_name>" in str(el) and "<job_name>" in str(el)))

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        if not self._RE.search(s):
            return None
        def x(tag):
            m = re.search(rf"<{tag}>([^<]*)</{tag}>", s)
            return m.group(1) if m else None
        status = x("completed_status")
        return self._event(level="error" if status not in (None, "0", "1") else "info",
                           message=f'Backup Exec job {x("job_name") or ""}'.strip(),
                           source="veritas.bex",
                           fields={"machine": x("machine_name"), "job_name": x("job_name"),
                                   "completed_status": status}, category="event", raw=line)


# ══════════════════════════════════════════════════════════════════════════════
#  OS platform (HP-UX, macOS, BSD)
# ══════════════════════════════════════════════════════════════════════════════

# ── HP-UX /etc/rc.log ─────────────────────────────────────────────────────────
#   Output from "/sbin/rc2.d/S560SnmpMaster start":
class HpuxRcAdapter(LogAdapter):
    name = "hpux_rc"
    language = "any"
    _HDR = re.compile(r'^Output from "(?P<script>\S+) (?P<action>start|stop)":\s*$')
    _EXIT = re.compile(r"^EXIT CODE:\s+(?P<code>-?\d+)")

    def detect(self, sample_lines):
        def hit(x):
            x = x.rstrip()
            return bool(self._HDR.match(x) or self._EXIT.match(x))
        return ratio_detect(sample_lines, lambda el: block_ratio(el, hit, threshold=0.4))

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        m = self._HDR.match(s.strip())
        if m:
            return self._event(level="info", message=f'Output from {m.group("script")} {m.group("action")}',
                               source="hpux.rc", fields={"script": m.group("script"),
                                                         "action": m.group("action")}, raw=line)
        m = self._EXIT.match(s.strip())
        if m:
            code = int(m.group("code"))
            return self._event(level="info" if code == 0 else "error",
                               message=f"EXIT CODE: {code}", source="hpux.rc",
                               fields={"exit_code": code}, raw=line)
        return None


# ── HP-UX /etc/shutdownlog ────────────────────────────────────────────────────
#   12:00  Mon Jul 20, 2026.  Reboot:  (by hpuxbox!root)
class HpuxShutdownAdapter(RxAdapter):
    name = "hpux_shutdownlog"
    language = "any"
    default_source = "hpux.shutdown"
    _RE = re.compile(
        r"^(?P<hh>\d{2}):(?P<mi>\d{2})\s+(?P<wd>[A-Z][a-z]{2}) (?P<mon>[A-Z][a-z]{2})\s+(?P<dy>\d{1,2}), (?P<yr>\d{4})\.\s+"
        r"(?P<event>Reboot|Halt|reboot|halt)[^:]*:\s*(?P<msg>.*)$")

    def _ts(self, g):
        if g["mon"] not in _MONTHS:
            return None
        return mk_ts(g["yr"], _MONTHS[g["mon"]], g["dy"], g["hh"], g["mi"], "0")

    def _level(self, g, line):
        return "warn" if "panic" in line.lower() else "info"

    def _fields(self, g, line):
        return {"event": g["event"]}


# ── macOS fsck_apfs / fsck_hfs log ────────────────────────────────────────────
#   /dev/rdisk3s1: fsck_apfs started at ...
class MacosFsckAdapter(RxAdapter):
    name = "macos_fsck"
    language = "any"
    _RE = re.compile(r"^(?P<dev>/dev/rdisk\d+s?\d*): (?P<msg>.*)$")

    def _level(self, g, line):
        m = g.get("msg", "")
        return "error" if m.startswith("**") and "CLEAN" not in m and "QUICKCHECK" not in m else "info"

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            ev["source"] = self._m1(line).group("dev")
        return ev

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            good = sum(1 for x in subs if self._RE.match(x.strip()))
            return good >= 1 and good / max(1, len(subs)) >= 0.5 and (
                "fsck" in str(el) or "FILESYSTEM" in str(el) or "**" in str(el))
        return vocab_detect(sample_lines, hit, cap=0.85)


# ── macOS wifi.log ────────────────────────────────────────────────────────────
#   Tue Jul 20 12:01:32.123 <airportd[160]> _doAutoJoin: ...
class MacosWifiAdapter(RxAdapter):
    name = "macos_wifi"
    language = "any"
    default_source = "wifi"
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2} [A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2}\.\d{3}) "
        r"<(?P<proc>[\w.\-]+)\[(?P<pid>\d+)\]>\s+(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            m = self._RE.match(line.rstrip("\r\n").strip())
            ev["source"] = m.group("proc")
            ev["data"]["pid"] = int(m.group("pid"))
        return ev


# ── macOS CoreSimulator.log ───────────────────────────────────────────────────
#   Jul 20 12:10:44 host CoreSimulatorService[496] <Notice> [com.apple.CoreSimulator]: ...
class MacosCoresimulatorAdapter(RxAdapter):
    name = "macos_coresimulator"
    language = "any"
    default_source = "CoreSimulator"
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2}) (?P<host>\S+) "
        r"(?P<proc>CoreSimulator\w*)\[(?P<pid>\d+)\] <(?P<level>\w+)> "
        r"\[(?P<subsys>com\.apple\.[\w.]+)\]:\s*(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _fields(self, g, line):
        return {"host": g["host"], "pid": int(g["pid"]), "subsystem": g["subsys"]}


# ── macOS MDM ManagedClient log ───────────────────────────────────────────────
#   2025-07-20 09:12:33.412 ManagedClient[406:1a2f] [com.apple.ManagedClient:...] ...
class MacosMdmAdapter(RxAdapter):
    name = "macos_mdm"
    language = "any"
    default_source = "ManagedClient"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) (?P<proc>ManagedClient|mdmclient)"
        r"\[(?P<pid>\d+):(?P<tid>[0-9a-f]+)\] \[(?P<subsys>com\.apple\.[\w.:]+)\]\s*(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _fields(self, g, line):
        return {"pid": int(g["pid"]), "thread": g["tid"], "subsystem": g["subsys"]}


# ── macOS InstallHistory.plist dict entry (XML) ───────────────────────────────
class MacosInstallHistoryAdapter(LogAdapter):
    name = "macos_install_history"
    language = "any"
    _RE = re.compile(r"<key>packageIdentifiers</key>|<key>displayName</key>.*<key>processName</key>")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: "<key>packageIdentifiers</key>" in str(el)
                            and "<key>date</key>" in str(el))

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        if "<key>packageIdentifiers</key>" not in s:
            return None
        def val(key, tag):
            m = re.search(rf"<key>{key}</key><{tag}>([^<]*)</{tag}>", s)
            return m.group(1) if m else None
        dm = re.search(r"<key>date</key><date>([^<]*)</date>", s)
        return self._event(level="info", message=val("displayName", "string") or "install event",
                           source="macos.installhistory",
                           ts_ms=parse_timestamp(dm.group(1)) if dm else None,
                           fields={"display_name": val("displayName", "string"),
                                   "process": val("processName", "string")},
                           category="event", raw=line)


# ── macOS opendirectoryd.log ──────────────────────────────────────────────────
#   2024-07-20 11:00:00.123456-0700 - opendirectoryd (build 796.100)[456] [session] - ...
class OpendirectorydAdapter(RxAdapter):
    name = "opendirectoryd"
    language = "any"
    default_source = "opendirectoryd"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+[+-]\d{4}) - "
        r"opendirectoryd \(build [^)]+\)\[(?P<pid>\d+)\] \[(?P<tag>[^\]]+)\] - (?P<msg>.*)$")

    def _ts(self, g):
        t = g["ts"]
        return parse_timestamp(t[:-2] + ":" + t[-2:])

    def _level(self, g, line):
        return "error" if re.search(r"error|fail|denied", g.get("msg", ""), re.I) else "info"

    def _fields(self, g, line):
        return {"pid": int(g["pid"]), "subsystem": g["tag"]}


# ── BSD/SysV process accounting via lastcomm ──────────────────────────────────
#   cc       -    alice    ttyp1      0.05 secs Mon Jul 20 14:02
class LastcommAdapter(LogAdapter):
    name = "lastcomm"
    language = "any"
    _RE = re.compile(
        r"^(?P<cmd>\S+)\s+(?P<flags>[SFDX\-]+)\s+(?P<user>\S+)\s+(?P<tty>\S+)\s+"
        r"(?P<secs>\d+\.\d{2}) secs (?P<start>[A-Z][a-z]{2} [A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2})\s*$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: bool(self._RE.match(str(el).rstrip())))

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        m = self._RE.match(s)
        if not m:
            return None
        g = m.groupdict()
        return self._event(level="info", message=f'{g["cmd"]} by {g["user"]} ({g["secs"]}s)',
                           source="acct", ts_ms=parse_timestamp(g["start"]),
                           fields={"command": g["cmd"], "flags": g["flags"], "user": g["user"],
                                   "tty": g["tty"], "cpu_seconds": float(g["secs"])},
                           category="event", raw=line)


# ══════════════════════════════════════════════════════════════════════════════
#  Firmware / RTOS / embedded
# ══════════════════════════════════════════════════════════════════════════════

# ── SeaBIOS debug log ─────────────────────────────────────────────────────────
class SeabiosAdapter(LogAdapter):
    name = "seabios"
    language = "firmware"
    _BANNER = re.compile(r"^SeaBIOS \(version ")
    _VOCAB = re.compile(r"^(found |Booting from|Scan for|WARNING|EFI |init |Sending |"
                        r"drive |detected |Returned|enter |UHCI|EHCI|XHCI|virtio|PCI)", re.I)

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            return any(self._BANNER.match(x.strip()) for x in subs) or (
                bool(subs) and sum(1 for x in subs if self._VOCAB.match(x.strip())) / len(subs) >= 0.5)
        return vocab_detect(sample_lines, hit, cap=0.7)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        first = next((x for x in split_any(s)
                      if self._BANNER.match(x.strip()) or self._VOCAB.match(x.strip())), None)
        if first is None:
            return None
        return self._event(level="warn" if first.strip().startswith("WARNING") else "info",
                           message=first.strip(), source="seabios", raw=line)


# ── Raspberry Pi VideoCore firmware boot log ──────────────────────────────────
#   000000.024: brfs: File read: /mfs/sd/config.txt
class RpiVcAdapter(RxAdapter):
    name = "rpi_vc_firmware"
    language = "firmware"
    _RE = re.compile(r"^(?P<ts>\d{6}\.\d{3}): (?P<msg>.*)$")

    def _ts(self, g):
        try:
            return float(g["ts"]) * 1000.0
        except (TypeError, ValueError):
            return None

    def _level(self, g, line):
        return "error" if re.search(r"error|fail", g.get("msg", ""), re.I) else "info"

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            msg = ev["data"]["message"]
            sm = re.match(r"^(\w+):", msg)
            if sm:
                ev["source"] = "videocore." + sm.group(1)
            else:
                ev["source"] = "videocore"
        return ev


# ── Arm Mbed OS mbed_error() fatal dump ───────────────────────────────────────
class MbedOsErrorAdapter(LogAdapter):
    name = "mbed_os_error"
    language = "cpp"
    _BEGIN = re.compile(r"^\+\+ MbedOS Error Info \+\+")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            return bool(subs) and self._BEGIN.match(subs[0].strip()) is not None
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if not subs or not self._BEGIN.match(subs[0].strip()):
            return None
        kv = {}
        for x in subs:
            for k in ("Error Status", "Code", "Module", "Error Message", "Location", "Error Value"):
                m = re.search(rf"{k}:\s*(\S+(?:[^\n]*?)?)(?=\s+\w+:|$)", x)
                if m and k not in kv:
                    kv[k] = m.group(1).strip()
        return self._event(level="fatal",
                           message=kv.get("Error Message", "MbedOS fatal error"),
                           source="mbed-os", trace_id=kv.get("Error Status"),
                           fields={"code": kv.get("Code"), "module": kv.get("Module"),
                                   "location": kv.get("Location"),
                                   "error_status": kv.get("Error Status")},
                           category="crash", raw=line)


# ── Contiki-NG logging module ─────────────────────────────────────────────────
#   [INFO: Main      ] Starting Contiki-NG release/v4.8
class ContikiNgAdapter(RxAdapter):
    name = "contiki_ng"
    language = "c"
    _RE = re.compile(
        r"^\[(?P<level>INFO|WARN|ERR|DBG|ANNO):\s+(?P<module>[\w\-]+)\s*\]\s*(?P<msg>.*)$")
    _LVL = {"INFO": "info", "WARN": "warn", "ERR": "error", "DBG": "debug", "ANNO": "info"}

    def _level(self, g, line):
        return self._LVL.get(g["level"], "info")

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            ev["source"] = self._m1(line).group("module")
        return ev


# ── Nordic nRF5 SDK NRF_LOG ───────────────────────────────────────────────────
#   <info> app: Fast advertising started.
class NordicNrfAdapter(RxAdapter):
    name = "nordic_nrf"
    language = "c"
    _RE = re.compile(r"^<(?P<level>info|warning|error|debug)>\s+(?P<module>[\w\-]+):\s*(?P<msg>.*)$")

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            ev["source"] = self._m1(line).group("module")
        return ev


# ── Azure RTOS / ThreadX console + fault dump ─────────────────────────────────
class ThreadxAdapter(LogAdapter):
    name = "threadx"
    language = "c"
    _TX = re.compile(r"^\[ThreadX\]\s+(?P<msg>.*)$")
    _FAULT = re.compile(r"^(?P<kind>HardFault|BusFault|MemManage|UsageFault):\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        def hit(x):
            x = x.strip()
            return bool(self._TX.match(x) or self._FAULT.match(x))
        return vocab_detect(sample_lines, lambda el: block_ratio(el, hit), cap=0.8)

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._FAULT.match(s)
        if m:
            regs = dict(re.findall(r"(\w+)=(0x[0-9A-Fa-f]+)", m.group("msg")))
            return self._event(level="fatal", message=f'{m.group("kind")}: {m.group("msg")}',
                               source="threadx", fields=regs or None, category="crash", raw=line)
        m = self._TX.match(s)
        if m:
            return self._event(level="info", message=m.group("msg"), source="threadx", raw=line)
        return None


# ── TI-RTOS / SYS/BIOS xdc.runtime Log ────────────────────────────────────────
#   [t=0x0000000012345678] ti.sysbios.knl.Task: LM_switch: oldtsk: 0x20001000
class TiRtosAdapter(RxAdapter):
    name = "ti_rtos"
    language = "c"
    _RE = re.compile(r"^\[t=0x(?P<ticks>[0-9a-fA-F]+)\]\s+(?P<module>[\w.]+):\s+(?P<msg>.*)$")

    def _level(self, g, line):
        return ""

    def _fields(self, g, line):
        return {"ticks": int(g["ticks"], 16)}

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            ev["source"] = self._m1(line).group("module")
        return ev


# ── Nintendo Switch libnx diagAbort / svcBreak crash ──────────────────────────
class SwitchDiagAdapter(LogAdapter):
    name = "switch_diag_crash"
    language = "cpp"
    _RE = re.compile(r"svcBreak|diagAbort|nnMain abort|libnx result \d{4}-\d{4}|"
                     r"module=0x[0-9a-fA-F]+ desc=0x[0-9a-fA-F]+")

    def detect(self, sample_lines):
        def hit(el):
            return any(self._RE.search(x) for x in split_any(el))
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        if not any(self._RE.search(x) for x in split_any(s)):
            return None
        res = re.search(r"(\d{4}-\d{4})", s)
        reason = re.search(r"reason=(0x[0-9a-fA-F]+(?:\s*\([^)]*\))?)", s)
        return self._event(level="fatal", message=s.strip().splitlines()[0] if s.strip() else "libnx abort",
                           source="libnx", trace_id=res.group(1) if res else None,
                           fields={"result_code": res.group(1) if res else None,
                                   "reason": reason.group(1) if reason else None},
                           category="crash", raw=line)


# ── UEFI Secure Boot / shim / mokutil status ──────────────────────────────────
class UefiSecurebootAdapter(LogAdapter):
    name = "uefi_secureboot"
    language = "firmware"
    _RE = re.compile(
        r"(?:UEFI )?Secure Boot is (?:enabled|disabled)|SecureBoot:\s*[01]\b|"
        r"MokList(?:RT)?\b|Verification failed:|Security Violation|"
        r"SHIM_VERBOSE|shim version")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            good = sum(1 for x in subs if self._RE.search(x))
            return good >= 1 and good / max(1, len(subs)) >= 0.5
        return vocab_detect(sample_lines, hit, cap=0.8)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        first = next((x for x in split_any(s) if self._RE.search(x)), None)
        if first is None:
            return None
        failed = re.search(r"Verification failed|Security Violation|disabled", first, re.I)
        return self._event(level="warn" if failed else "info", message=first.strip(),
                           source="shim", raw=line)


# ── QNX Neutrino slog2info output ─────────────────────────────────────────────
#   Jul 20 14:03:11.123    kernel.9000                    5    0  message text
class QnxSlog2Adapter(RxAdapter):
    name = "qnx_slog2"
    language = "any"
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2}\.\d{3})\s+(?P<buf>[\w.]+)\s+"
        r"(?P<sev>[0-7])\s+(?P<code>\d+)\s+(?P<msg>.*)$")
    _SEV = {"0": "fatal", "1": "fatal", "2": "fatal", "3": "error",
            "4": "warn", "5": "info", "6": "info", "7": "debug"}

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return self._SEV.get(g["sev"], "info")

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            m = self._m1(line)
            ev["source"] = m.group("buf")
            ev["data"]["code"] = int(m.group("code"))
        return ev


# ══════════════════════════════════════════════════════════════════════════════
#  Industrial / OT
# ══════════════════════════════════════════════════════════════════════════════

# ── GE iFIX alarm/event history ───────────────────────────────────────────────
#   01/09/19 10:22:33.1 [FIX] NODE1 FT101 HIHI 105.00 Flow high-high
class GeIfixAlarmAdapter(RxAdapter):
    name = "ge_ifix_alarm"
    language = "any"
    default_source = "ifix"
    _RE = re.compile(
        r"^(?P<mo>\d{2})/(?P<dy>\d{2})/(?P<yr>\d{2})\s+(?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})\.(?P<t>\d)\s+"
        r"\[(?P<subsys>FIX|SCADA|\w+)\]\s+(?P<node>\S+)\s+(?P<tag>\S+)\s+"
        r"(?P<status>HIHI|HI|LO|LOLO|COMM|OK|ACK)\s+(?P<value>[\d.\-]+)\s*(?P<msg>.*)$")

    def _ts(self, g):
        return mk_ts(two_digit_year(g["yr"]), g["mo"], g["dy"], g["hh"], g["mi"], g["ss"], int(g["t"]) * 100000)

    def _level(self, g, line):
        return "warn" if g["status"] in ("HIHI", "HI", "LO", "LOLO", "COMM") else "info"

    def _fields(self, g, line):
        return {"node": g["node"], "tag": g["tag"], "alarm_status": g["status"], "value": g["value"]}


# ── Veeder-Root TLS ATG inventory report ──────────────────────────────────────
#   I20100 \n JAN 9, 2019 10:22 AM \n ...IN-TANK INVENTORY...
class VeederRootAtgAdapter(LogAdapter):
    name = "veeder_root_atg"
    language = "any"
    _CMD = re.compile(r"^I\d{5}\s*$")
    _VOCAB = re.compile(r"IN-TANK INVENTORY|TANK PRODUCT VOLUME|DELIVERY|LEAK TEST|ALARM")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            return bool(subs) and (self._CMD.match(subs[0].strip())
                                   or any(self._VOCAB.search(x) for x in subs))
        return vocab_detect(sample_lines, hit, cap=0.85)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if not subs:
            return None
        cmd = subs[0].strip() if self._CMD.match(subs[0].strip()) else None
        if cmd is None and not any(self._VOCAB.search(x) for x in subs):
            return None
        return self._event(level="info", message=cmd or "ATG report",
                           source="veeder-root",
                           fields={"function_code": cmd, "block_lines": len(subs)},
                           category="event", raw=line)


# ── lib60870 / libiec61850 protocol debug ─────────────────────────────────────
#   Received I frame: N(S) = 1 N(R) = 2
class Lib60870Adapter(LogAdapter):
    name = "lib60870"
    language = "c"
    _RE = re.compile(
        r"^(?:CS104_Connection: |CS104 SLAVE: |MMS_SERVER: )|"
        r"^(?:Received (?:I|U|S) frame|Send (?:TESTFR|STARTDT|STOPDT)_(?:ACT|CON))")

    def detect(self, sample_lines):
        return vocab_detect(sample_lines,
                            lambda el: block_ratio(el, lambda x: bool(self._RE.match(x.strip()))),
                            cap=0.8)

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        if not self._RE.match(s):
            return None
        comp = "iec104"
        if s.startswith("MMS_SERVER"):
            comp = "iec61850.mms"
        elif s.startswith("CS104"):
            comp = "iec104.cs104"
        return self._event(level="debug", message=s, source=comp, category="debug", raw=line)


# ── OPC-UA .NET stack trace log ───────────────────────────────────────────────
#   10:22:33.123 - Channel 1 in Connected state.
class OpcUaTraceAdapter(RxAdapter):
    name = "opc_ua_trace"
    language = "dotnet"
    default_source = "opc-ua"
    _RE = re.compile(r"^(?P<ts>\d{2}:\d{2}:\d{2}\.\d{3}) - (?P<msg>.*)$")
    _VOCAB = re.compile(r"Channel \d|Session |Certificate |Endpoint |Subscription |"
                        r"Service|Secure|Connect|Publish|token")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return "error" if re.search(r"error|fault|reject|bad", g.get("msg", ""), re.I) else "info"

    def detect(self, sample_lines):
        def hit(x):
            m = self._RE.match(x.strip())
            return bool(m) and self._VOCAB.search(m.group("msg")) is not None
        return vocab_detect(sample_lines, lambda el: block_ratio(el, hit), cap=0.8)


# ── OpenPLC v3 runtime log ────────────────────────────────────────────────────
#   OpenPLC Runtime starting...
class OpenplcAdapter(LogAdapter):
    name = "openplc_runtime"
    language = "cpp"
    _RE = re.compile(
        r"^(?:OpenPLC Runtime (?:starting|initialized|stopping)|"
        r"Server: (?:Modbus|DNP3|EtherNet/IP)|Issued .* command|"
        r"Interactive Server: .*|Connecting to |Loading .*)\b")

    def detect(self, sample_lines):
        return vocab_detect(sample_lines,
                            lambda el: block_ratio(el, lambda x: bool(self._RE.match(x.strip()))),
                            cap=0.8)

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        if not self._RE.match(s):
            return None
        return self._event(level="info", message=s, source="openplc", raw=line)


# ══════════════════════════════════════════════════════════════════════════════
#  Telephony / VoIP / conferencing
# ══════════════════════════════════════════════════════════════════════════════

# ── Jitsi Videobridge / Jicofo (java.util.logging one-line) ───────────────────
#   JVB 2026-01-05 10:26:12.123 INFO: [23] HealthChecker.run#171: ...
class JitsiJvbAdapter(RxAdapter):
    name = "jitsi_jvb"
    language = "java"
    _RE = re.compile(
        r"^(?P<comp>JVB|Jicofo|JICOFO) (?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) "
        r"(?P<level>SEVERE|WARNING|INFO|CONFIG|FINE|FINER|FINEST): \[(?P<thread>\d+)\] "
        r"(?P<loc>[\w.$]+#\d+): (?P<msg>.*)$")
    _LVL = {"SEVERE": "error", "WARNING": "warn", "INFO": "info", "CONFIG": "info",
            "FINE": "debug", "FINER": "debug", "FINEST": "trace"}

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return self._LVL.get(g["level"], "info")

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            m = self._m1(line)
            ev["source"] = m.group("comp").lower()
            ev["data"]["location"] = m.group("loc")
            ev["data"]["thread"] = int(m.group("thread"))
        return ev


# ── Yate telephony engine log ─────────────────────────────────────────────────
#   2026-01-05_10:26:12.123456 <sip:WARN> Could not classify call ...
class YateAdapter(RxAdapter):
    name = "yate"
    language = "cpp"
    _RE = re.compile(
        r"^(?:(?P<ts>\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2}\.\d+) )?"
        r"<(?P<facility>[\w./\-]+):(?P<level>FAIL|GOON|STUB|WARN|MILD|CALL|NOTE|INFO|ALL|CONF|TEST)>\s*(?P<msg>.*)$")
    _LVL = {"FAIL": "error", "GOON": "info", "STUB": "debug", "WARN": "warn",
            "MILD": "warn", "CALL": "info", "NOTE": "info", "INFO": "info",
            "ALL": "trace", "CONF": "info", "TEST": "debug"}

    def _ts(self, g):
        return parse_timestamp(g["ts"].replace("_", " ")) if g.get("ts") else None

    def _level(self, g, line):
        return self._LVL.get(g["level"], "info")

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            ev["source"] = "yate." + self._m1(line).group("facility")
        return ev


# ── Yealink IP phone syslog ───────────────────────────────────────────────────
#   <134>Jan  5 10:26:12 sua [634]: SUA <6+info  > [000] send REGISTER to ...
class YealinkSyslogAdapter(RxAdapter):
    name = "yealink_syslog"
    language = "any"
    default_source = "yealink"
    _RE = re.compile(
        r"^(?:<\d+>)?(?P<ts>[A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2}) \S+ \[?\d*\]?:?\s*"
        r"(?P<mod>SUA|ACC|DSK|GUI|TR9|WEB|AUT|NET) <\d+\+(?P<level>\w+)\s*>\s*(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _fields(self, g, line):
        return {"module": g["mod"]}


# ── Oracle (Acme Packet) SBC sipmsg.log ───────────────────────────────────────
#   Jan  5 10:26:12.123 On [0:0]10.0.0.20:5060 received from 203.0.113.10:5060
class OracleSbcSipmsgAdapter(RxAdapter):
    name = "oracle_sbc_sipmsg"
    language = "any"
    default_source = "acme.sbc"
    match_scope = "first"
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2}\.\d{3}) On \[(?P<slot>\d+:\d+)\]"
        r"(?P<local>[\d.]+:\d+) (?P<dir>received from|sent to) (?P<peer>[\d.]+:\d+)\s*$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return "info"

    def _fields(self, g, line):
        return {"slot_port": g["slot"], "local": g["local"], "direction": g["dir"], "peer": g["peer"]}


# ── Apache Pulsar Functions instance log ──────────────────────────────────────
#   12:05:33,408 INFO  [pulsar-function-thread] [instance: 0] JavaInstanceRunnable - ...
class PulsarFunctionAdapter(RxAdapter):
    name = "pulsar_function"
    language = "java"
    _RE = re.compile(
        r"^(?P<time>\d{2}:\d{2}:\d{2},\d{3}) (?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+"
        r"\[(?P<thread>[^\]]+)\] \[instance: (?P<inst>\d+)\] (?P<logger>\S+) - (?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["time"].replace(",", "."))

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            m = self._m1(line)
            ev["source"] = m.group("logger")
            ev["data"]["instance"] = int(m.group("inst"))
            ev["data"]["thread"] = m.group("thread")
        return ev


# ── GNU Mailman 2.x post/smtp log ─────────────────────────────────────────────
#   Jul 20 09:05:12 2026 (3402) <abc123@example.com> smtp to mylist for 213 recips, ...
class MailmanAdapter(RxAdapter):
    name = "mailman"
    language = "python"
    default_source = "mailman"
    _RE = re.compile(
        r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<dy>\d{1,2}) (?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2}) (?P<yr>\d{4}) "
        r"\((?P<pid>\d+)\) (?P<msg>.*)$")

    def _ts(self, g):
        if g["mon"] not in _MONTHS:
            return None
        return mk_ts(g["yr"], _MONTHS[g["mon"]], g["dy"], g["hh"], g["mi"], g["ss"])

    def _fields(self, g, line):
        f = {"pid": int(g["pid"])}
        mid = re.search(r"<([^>]+@[^>]+)>", g.get("msg", ""))
        if mid:
            f["message_id"] = mid.group(1)
        return f


# ══════════════════════════════════════════════════════════════════════════════
#  Middleware / Java / VMware
# ══════════════════════════════════════════════════════════════════════════════

# ── Octopus Deploy server log (NLog) ──────────────────────────────────────────
#   2017-01-06 14:06:36.5768   1234      5  INFO  "Web server is ready ..."
class OctopusServerAdapter(RxAdapter):
    name = "octopus_server"
    language = "dotnet"
    default_source = "octopus.server"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{4})(?:\s+[+-]\d{2}:\d{2})?\s+"
        r"(?P<pid>\d+)\s+(?P<tid>\d+)\s+(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _fields(self, g, line):
        return {"process_id": int(g["pid"]), "thread_id": int(g["tid"])}


# ── Octopus Deploy deployment task log ────────────────────────────────────────
#   14:06:36   Info     |         Acquiring packages
class OctopusTaskAdapter(RxAdapter):
    name = "octopus_task"
    language = "dotnet"
    default_source = "octopus.task"
    _RE = re.compile(
        r"^(?P<time>\d{2}:\d{2}:\d{2})\s+(?P<level>Verbose|Info|Warning|Error|Fatal)\s+\|\s*(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["time"])

    def _fields(self, g, line):
        indent = len(line) - len(line.lstrip())
        raw = self._m1(line)
        depth = len(raw.group("msg")) - len(raw.group("msg").lstrip()) if raw else 0
        return {"tree_depth": depth}


# ── oVirt engine boot.log (WildFly/JBoss bootstrap) ───────────────────────────
#   03:46:19,238 INFO  [org.jboss.as.server] WFLYSRV0039: ...
class OvirtJbossBootAdapter(RxAdapter):
    name = "ovirt_jboss_boot"
    language = "java"
    _RE = re.compile(
        r"^(?P<time>\d{2}:\d{2}:\d{2},\d{3}) (?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+"
        r"\[(?P<cat>[\w.$]+)\] (?P<code>(?:WFLY|JBAS|WFLYSRV|ISPN)\w*\d+): (?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["time"].replace(",", "."))

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            m = self._m1(line)
            ev["source"] = m.group("cat")
            ev["data"]["message_code"] = m.group("code")
        return ev


# ── InterSystems IRIS container supervisor (iris-main.log) ────────────────────
#   18:09:02 2026-07-14 [INFO] Starting InterSystems IRIS instance IRIS...
class IrisMainAdapter(RxAdapter):
    name = "iris_main"
    language = "any"
    default_source = "iris-main"
    _RE = re.compile(
        r"^(?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2}) (?P<yr>\d{4})-(?P<mo>\d{2})-(?P<dy>\d{2}) "
        r"\[(?P<level>INFO|WARN|WARNING|ERROR|DEBUG|FATAL)\] (?P<msg>.*)$")

    def _ts(self, g):
        return mk_ts(g["yr"], g["mo"], g["dy"], g["hh"], g["mi"], g["ss"])


# ── InterSystems IRIS messages.log (console.log) ──────────────────────────────
#   07/21/26-12:00:00:123 (1234) 0 [Utility.Event] Journal file switched ...
class IrisMessagesAdapter(RxAdapter):
    name = "iris_messages"
    language = "any"
    default_source = "iris"
    _RE = re.compile(
        r"^(?P<mo>\d{2})/(?P<dy>\d{2})/(?P<yr>\d{2})-(?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2}):(?P<ms>\d{3}) "
        r"\((?P<pid>\d+)\) (?P<sev>[0-3]) \[(?P<cat>[\w.]+)\] (?P<msg>.*)$")
    _SEV = {"0": "info", "1": "warn", "2": "error", "3": "fatal"}

    def _ts(self, g):
        return mk_ts(two_digit_year(g["yr"]), g["mo"], g["dy"], g["hh"], g["mi"], g["ss"], int(g["ms"]) * 1000)

    def _level(self, g, line):
        return self._SEV.get(g["sev"], "info")

    def _fields(self, g, line):
        return {"pid": int(g["pid"]), "category": g["cat"]}


# ── VMware vRealize/Aria Log Insight runtime.log ──────────────────────────────
#   [2022-02-11 09:00:00,123 pool-3-thread-1 INFO com.vmware.loginsight...] Daemon started
class LoginsightLsAdapter(RxAdapter):
    name = "loginsight_ls"
    language = "java"
    default_source = "loginsight"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) (?P<thread>\S+) "
        r"(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL) (?P<cls>[\w.$]+)\] (?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"].replace(",", "."))

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            m = self._m1(line)
            ev["source"] = m.group("cls")
            ev["data"]["thread"] = m.group("thread")
        return ev


# ── VMware vCenter/ESXi 4.x-5.x legacy log format ─────────────────────────────
#   2013-04-01T10:30:00.213Z [7F3AAB90 info 'Default' opID=...] [VpxLRO] -- ...
class VcenterVpxdLegacyAdapter(RxAdapter):
    name = "vcenter_vpxd_legacy"
    language = "cpp"
    default_source = "vpxd"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z) \[(?P<tid>[0-9A-Fa-f]+) "
        r"(?P<level>verbose|info|warning|error|panic|trivia)\s+'(?P<comp>[^']*)'(?: opID=(?P<opid>\S+))?\] (?P<msg>.*)$")
    _LVL = {"verbose": "trace", "trivia": "trace", "warning": "warn", "panic": "fatal"}

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return self._LVL.get(g["level"], g["level"])

    def _fields(self, g, line):
        return {"thread_id": g["tid"], "component": g["comp"], "op_id": g.get("opid")}


# ── VMware log rotation / section header ──────────────────────────────────────
#   ... Section for VMware ESX hostd, pid=2100216
class VmwareSectionAdapter(LogAdapter):
    name = "vmware_section"
    language = "any"
    _RE = re.compile(r"Section for (?P<product>[\w /]+?), pid=(?P<pid>\d+)")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: bool(self._RE.search(str(el))))

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        m = self._RE.search(s)
        if not m:
            return None
        tsm = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)", s)
        return self._event(level="info", message=f'Section for {m.group("product")}',
                           source="vmware", ts_ms=parse_timestamp(tsm.group(1)) if tsm else None,
                           fields={"product": m.group("product"), "pid": int(m.group("pid"))},
                           category="event", raw=line)


# ── Apache Qpid Dispatch Router / skupper-router ──────────────────────────────
#   Tue Jun  7 14:39:52 2016 SERVER (info) Operational, 4 Threads Running
class QpidDispatchAdapter(RxAdapter):
    name = "qpid_dispatch"
    language = "any"
    default_source = "qdrouterd"
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2} [A-Z][a-z]{2}\s+\d+ [\d:]{8} \d{4}|"
        r"\d{4}-\d{2}-\d{2} [\d:.]+ [+-]\d{4}) "
        r"(?P<module>SERVER|ROUTER|ROUTER_CORE|AGENT|CONTAINER|POLICY|HTTP|CONN_MGR|MESSAGE|"
        r"PROTOCOL|TCP_ADAPTOR|FLOW_LOG) \((?P<level>info|notice|warning|error|critical|debug|trace)\)\s*(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _fields(self, g, line):
        return {"module": g["module"]}


# ── Chef Habitat Supervisor output ────────────────────────────────────────────
#   redis.default(O): 1:M 07 Aug 15:29:12.000 * Ready to accept connections
class HabitatSupAdapter(RxAdapter):
    name = "habitat_sup"
    language = "rust"
    default_source = "hab-sup"
    _RE = re.compile(
        r"^(?P<svc>hab-sup|[\w.\-]+\.[\w\-]+)\((?P<key>MR|SR|O|E|UCW|HK|CS|SC|GS|FL|WK)\):\s*(?P<msg>.*)$")

    def _level(self, g, line):
        return "error" if g["key"] == "E" else "info"

    def _fields(self, g, line):
        return {"log_key": g["key"]}

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            ev["source"] = self._m1(line).group("svc")
        return ev


# ══════════════════════════════════════════════════════════════════════════════
#  Storage / mail
# ══════════════════════════════════════════════════════════════════════════════

# ── Dell EMC Unity syslog (CEM) ───────────────────────────────────────────────
#   2019-06-24 08:07:27 unity01 spa cem_svc: 14:60a Notice The DNS client ...
class DellUnityAdapter(RxAdapter):
    name = "dell_unity"
    language = "any"
    default_source = "dell.unity"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?P<array>\S+) (?P<sp>spa|spb) "
        r"(?P<svc>cem\w*): (?P<eid>[0-9a-f]+:[0-9a-f]+) "
        r"(?P<sev>Critical|Error|Warning|Notice|Info)\s+(?P<msg>.*)$")
    _LVL = {"Critical": "fatal", "Error": "error", "Warning": "warn", "Notice": "info", "Info": "info"}

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return self._LVL.get(g["sev"], "info")

    def _fields(self, g, line):
        return {"array": g["array"], "storage_processor": g["sp"], "event_id": g["eid"]}


# ── Solaris/illumos FMA fault manager (fmdump) ────────────────────────────────
#   Jul 20 14:03:11.1234 ena@0x... fault.io.disk.predictive-failure
class SolarisFmaAdapter(RxAdapter):
    name = "solaris_fmadump"        # distinct from mainframe.py 'solaris_fma' (UUID/MSG-ID rows)
    language = "any"
    default_source = "fmd"
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2}\.\d+)\s+(?P<ena>\S+)\s+"
        r"(?P<cls>(?:fault|ereport|defect|list|upset|resource)\.[\w.\-]+)\s*(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        return "error" if g["cls"].startswith(("fault", "defect")) else "warn"

    def _fields(self, g, line):
        return {"ena": g["ena"], "fault_class": g["cls"]}


# ── hMailServer log (CSV-with-quotes) ─────────────────────────────────────────
#   "SMTPD"\t3456\t78\t"2026-07-20 10:00:00.123"\t"203.0.113.7"\t"SENT: 220 ..."
class HmailserverAdapter(LogAdapter):
    name = "hmailserver"
    language = "any"
    _RE = re.compile(
        r'^"(?P<comp>SMTPD|IMAPD|POP3D|TCPIP|DEBUG|ERROR|APPLICATION|AWSTATS|SMTPDELIVERY)"\s*[,\t]\s*'
        r'(?P<tid>\d+)\s*[,\t]\s*(?P<sid>\d+)\s*[,\t]\s*"(?P<ts>[^"]+)"\s*[,\t]\s*'
        r'"(?P<ip>[^"]*)"\s*[,\t]\s*"(?P<payload>.*)"\s*$')

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: bool(self._RE.match(str(el).rstrip())))

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        m = self._RE.match(s)
        if not m:
            return None
        g = m.groupdict()
        lvl = "error" if g["comp"] == "ERROR" else "info"
        return self._event(level=lvl, message=g["payload"], source=f'hmail.{g["comp"].lower()}',
                           ts_ms=parse_timestamp(g["ts"]), trace_id=g["sid"],
                           fields={"component": g["comp"], "thread_id": int(g["tid"]),
                                   "session_id": int(g["sid"]), "remote_ip": g["ip"]},
                           category="event", raw=line)


# ── procmail MDA LOGFILE (3-line stanza) ──────────────────────────────────────
#   From bob@example.com  Sun Jul 20 09:07:31 2026 / Subject: ... / Folder: ...
class ProcmailAdapter(LogAdapter):
    name = "procmail"
    language = "any"
    _FROM = re.compile(r"^From (?P<sender>\S+@\S+)\s+(?P<ts>[A-Z][a-z]{2} [A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2} \d{4})\s*$")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            return bool(subs) and self._FROM.match(subs[0].strip()) is not None
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if not subs or not self._FROM.match(subs[0].strip()):
            return None
        g = self._FROM.match(subs[0].strip()).groupdict()
        subj = folder = None
        for x in subs[1:]:
            sm = re.match(r"^\s*Subject:\s*(.*)$", x)
            fm = re.match(r"^\s*Folder:\s*(\S+)\s+(\d+)\s*$", x)
            if sm:
                subj = sm.group(1)
            if fm:
                folder = fm.group(1)
        return self._event(level="info", message=f'mail from {g["sender"]}',
                           source="procmail", ts_ms=parse_timestamp(g["ts"]),
                           fields={"sender": g["sender"], "subject": subj, "folder": folder},
                           category="event", raw=line)


# ── Oracle/Sun Messaging Server mail.log_current ──────────────────────────────
#   19-Jul-2026 23:16:26.29 tcp_intranet tcp_local EE 1 bob@... rfc822;... smtp;250 ...
class OracleMessagingMailAdapter(RxAdapter):
    name = "oracle_messaging_mail"
    language = "any"
    default_source = "sun.messaging"
    _RE = re.compile(
        r"^(?P<dy>\d{1,2})-(?P<mon>[A-Z][a-z]{2})-(?P<yr>\d{4}) (?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})\.(?P<frac>\d+) "
        r"(?P<src_ch>\S+) (?P<dst_ch>\S+) (?P<action>[EDRJ]{1,2}) (?P<size>\d+) (?P<from>\S+) rfc822;(?P<rest>.*)$")

    def _ts(self, g):
        if g["mon"] not in _MONTHS:
            return None
        return mk_ts(g["yr"], _MONTHS[g["mon"]], g["dy"], g["hh"], g["mi"], g["ss"],
                     int(g["frac"].ljust(6, "0")[:6]))

    def _level(self, g, line):
        return "warn" if "R" in g["action"] else "info"

    def _fields(self, g, line):
        return {"source_channel": g["src_ch"], "destination_channel": g["dst_ch"],
                "action_code": g["action"], "size": int(g["size"]), "envelope_from": g["from"]}


# ══════════════════════════════════════════════════════════════════════════════
#  Monitoring / metrics / IDS
# ══════════════════════════════════════════════════════════════════════════════

# ── sflowtool default key/value decode ────────────────────────────────────────
#   unixSecondsUTC 991362247 / datagramVersion 2 / agent 10.0.0.254 / ...
class SflowtoolAdapter(LogAdapter):
    name = "sflowtool_kv"           # distinct from network.py 'sflowtool' (CSV -l form)
    language = "any"
    _KEYS = {"unixSecondsUTC", "datagramVersion", "agent", "agentSubId", "packetSequenceNo",
             "sysUpTime", "samplesInPacket", "startDatagram", "endDatagram", "startSample",
             "endSample", "sampleType", "sampleSequenceNo", "sourceId", "meanSkipCount",
             "samplePool", "dropEvents", "inputPort", "outputPort", "flowBlock_tag",
             "srcIP", "dstIP", "IPProtocol", "srcPort", "dstPort"}
    _KV = re.compile(r"^(?P<key>\w+)\s+(?P<val>.+)$")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            good = 0
            for x in subs:
                m = self._KV.match(x.strip())
                if m and m.group("key") in self._KEYS:
                    good += 1
                elif x.strip() in ("startDatagram", "endDatagram", "startSample", "endSample"):
                    good += 1
            return good >= 1 and good / max(1, len(subs)) >= 0.5
        return vocab_detect(sample_lines, hit, cap=0.85)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        subs = split_any(s)
        kv = {}
        for x in subs:
            m = self._KV.match(x.strip())
            if m and m.group("key") in self._KEYS:
                kv[m.group("key")] = m.group("val").strip()
        if not kv:
            return None
        ts_ms = None
        if "unixSecondsUTC" in kv and kv["unixSecondsUTC"].isdigit():
            ts_ms = float(kv["unixSecondsUTC"]) * 1000.0
        return self._event(level="info", message="sFlow sample",
                           source="sflowtool", ts_ms=ts_ms, fields=kv,
                           category="metrics", raw=line)


# ── Suricata stats.log human-readable counters ────────────────────────────────
class SuricataStatsAdapter(LogAdapter):
    name = "suricata_stats"
    language = "any"
    _DATE = re.compile(r"^Date: .* \(uptime: ")
    _COUNTER = re.compile(r"^(?P<counter>[\w.]+)\s+\|\s+(?P<tm>[^|]+?)\s+\|\s+(?P<val>\d+)\s*$")
    _HDR = re.compile(r"^Counter\s+\|\s+TM Name\s+\|\s+Value")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            has_date = any(self._DATE.match(x.strip()) for x in subs)
            good = sum(1 for x in subs if self._COUNTER.match(x.strip()) or self._HDR.match(x.strip()))
            return (has_date and good >= 0) or good >= 1
        return vocab_detect(sample_lines, lambda el: (
            any(self._DATE.match(x.strip()) or self._COUNTER.match(x.strip()) or self._HDR.match(x.strip())
                for x in split_any(el))), cap=0.85)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        subs = split_any(s)
        date_m = next((self._DATE.match(x.strip()) for x in subs if self._DATE.match(x.strip())), None)
        rows = [self._COUNTER.match(x.strip()).group("counter") for x in subs if self._COUNTER.match(x.strip())]
        if not date_m and not rows and not any(self._HDR.match(x.strip()) for x in subs):
            return None
        ts_ms = None
        for x in subs:
            dm = re.match(r"^Date: (\d{1,2}/\d{1,2}/\d{4}) -- (\d{2}:\d{2}:\d{2})", x.strip())
            if dm:
                ts_ms = us_date_ts(dm.group(1), dm.group(2))
                break
        return self._event(level="info", message="Suricata stats snapshot",
                           source="suricata.stats", ts_ms=ts_ms,
                           fields={"counters": len(rows), "block_lines": len(subs)},
                           category="metrics", raw=line)


# ── varnishstat -1 counter dump ───────────────────────────────────────────────
#   MAIN.cache_hit                     95         0.00 Cache hits
class VarnishstatAdapter(RxAdapter):
    name = "varnishstat"
    language = "any"
    default_source = "varnishstat"
    _RE = re.compile(
        r"^(?P<ns>MAIN|MGT|MEMPOOL|SMA|SMF|VBE|LCK)\.(?P<counter>[A-Za-z0-9_.]+)\s+"
        r"(?P<value>\d+)\s+(?P<rate>[0-9.]+|\.)\s+(?P<desc>.+)$")

    def _level(self, g, line):
        return ""

    def _ts(self, g):
        return None

    def _fields(self, g, line):
        return {"namespace": g["ns"], "counter": f'{g["ns"]}.{g["counter"]}',
                "value": int(g["value"]), "description": g["desc"].strip()}

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            ev["category"] = "metrics"
            ev["data"]["message"] = ev["data"]["counter"] + "=" + str(ev["data"]["value"])
        return ev


# ── Snort alert_full multi-line record ────────────────────────────────────────
class SnortFullAdapter(LogAdapter):
    name = "snort_full"
    language = "any"
    _HDR = re.compile(r"^\[\*\*\] \[(?P<gid>\d+):(?P<sid>\d+):(?P<rev>\d+)\] (?P<msg>.*?) \[\*\*\]\s*$")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            return bool(subs) and self._HDR.match(subs[0].strip()) is not None
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if not subs or not self._HDR.match(subs[0].strip()):
            return None
        g = self._HDR.match(subs[0].strip()).groupdict()
        prio = None
        classif = None
        for x in subs:
            pm = re.search(r"\[Priority: (\d+)\]", x)
            cm = re.search(r"\[Classification: ([^\]]+)\]", x)
            if pm:
                prio = int(pm.group(1))
            if cm:
                classif = cm.group(1)
        return self._event(level="warn", message=g["msg"], source="snort",
                           fields={"gid": int(g["gid"]), "sid": int(g["sid"]),
                                   "rev": int(g["rev"]), "priority": prio,
                                   "classification": classif}, category="event", raw=line)


# ── Infoblox NIOS DNS syslog (BIND-derived) ───────────────────────────────────
#   30-Apr-2013 13:35:02.187 client 10.120.20.32#42386: query: foo.com IN A + (...)
class InfobloxDnsAdapter(RxAdapter):
    name = "infoblox_dns"
    language = "any"
    default_source = "infoblox.dns"
    _RE = re.compile(
        r"^(?P<dy>\d{1,2})-(?P<mon>[A-Z][a-z]{2})-(?P<yr>\d{4}) (?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})\.(?P<ms>\d{3}) "
        r"client (?P<client>[\d.]+#\d+): query: (?P<qname>\S+) (?P<qclass>\w+) (?P<qtype>\w+)\s*(?P<flags>[+\-\w ]*?)(?:\s*\((?P<addr>[\d.]+)\))?\s*$")

    def _ts(self, g):
        if g["mon"] not in _MONTHS:
            return None
        return mk_ts(g["yr"], _MONTHS[g["mon"]], g["dy"], g["hh"], g["mi"], g["ss"], int(g["ms"]) * 1000)

    def _level(self, g, line):
        return "info"

    def _fields(self, g, line):
        return {"client": g["client"], "qname": g["qname"], "qclass": g["qclass"], "qtype": g["qtype"]}


# ── Oracle ADRCI show alert / show incident output ────────────────────────────
#   3808              ORA 603                   2010-06-18 21:35:49.322161 -07:00
class OracleAdrciAdapter(RxAdapter):
    name = "oracle_adrci"
    language = "any"
    default_source = "oracle.adrci"
    _RE = re.compile(
        r"^(?P<incident>\d+)\s+ORA (?P<code>\d+)(?:\s+\[[^\]]*\])?\s+"
        r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s*(?P<tz>[+-]\d{2}:\d{2})?\s*$")

    def _ts(self, g):
        return parse_timestamp(g["ts"] + (g["tz"] or ""))

    def _level(self, g, line):
        return "error"

    def _fields(self, g, line):
        return {"incident_id": int(g["incident"]), "problem_key": f'ORA {g["code"]}'}


# ── WatchGuard Firebox Fireware syslog ────────────────────────────────────────
#   Feb 25 12:49:50 firebox1 80BF00F4 (2024-02-25T12:49:50) firewall: msg_id="3000-0148" Allow ...
class WatchguardFireboxAdapter(RxAdapter):
    name = "watchguard_firebox"
    language = "any"
    default_source = "watchguard"
    _RE = re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2}) (?P<host>\S+) (?P<serial>[0-9A-F]{8}) "
        r"\((?P<iso>[^)]+)\) (?P<proc>\w+): msg_id=\"(?P<msgid>\d+-\d+)\" (?P<disp>\w+)\s+(?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["iso"]) or parse_timestamp(g["ts"])

    def _level(self, g, line):
        return "warn" if g["disp"] in ("Deny", "Drop", "Block") else "info"

    def _fields(self, g, line):
        return {"host": g["host"], "serial": g["serial"], "msg_id": g["msgid"], "disposition": g["disp"]}


# ══════════════════════════════════════════════════════════════════════════════
#  Legacy UNIX / mainframe
# ══════════════════════════════════════════════════════════════════════════════

# ── Broadcom (CA) ACF2 ACFRPT report / console message ────────────────────────
#   ACF01004 JSMITH PAYROLL.MASTER.DATA DATA SET ACCESS VIOLATION
class Acf2Adapter(RxAdapter):
    name = "acf2_report"
    language = "any"
    default_source = "acf2"
    _RE = re.compile(r"^(?P<msgid>ACF\d{5})\s+(?P<msg>.*)$")

    def _level(self, g, line):
        return "warn" if re.search(r"VIOLATION|DENIED|FAIL|INVALID", g.get("msg", ""), re.I) else "info"

    def _fields(self, g, line):
        return {"message_id": g["msgid"]}


# ── IBM z/VSE console log ─────────────────────────────────────────────────────
#   1Q47I   BG PAYROLL  EOJ NO RC=0000  DURATION 00:00:12
class ZvseConsoleAdapter(RxAdapter):
    name = "zvse_console"
    language = "any"
    default_source = "zvse"
    _RE = re.compile(
        r"^(?P<msgid>\d[A-Z]\d{2}[A-Z])\s+(?P<partition>BG|F[1-9]|FA|FB)\s+(?P<msg>.*)$")

    def _level(self, g, line):
        sev = g["msgid"][-1]
        return {"I": "info", "W": "warn", "E": "error", "A": "warn", "D": "info"}.get(sev, "info")

    def _fields(self, g, line):
        return {"message_id": g["msgid"], "partition": g["partition"]}


# ── Tru64 EVM evmshow rendered output ─────────────────────────────────────────
#   2026/07/20 14:02:15 [3] The system is shutting down
class Tru64EvmAdapter(RxAdapter):
    name = "tru64_evm"
    language = "any"
    default_source = "tru64.evm"
    _RE = re.compile(
        r"^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) \[(?P<prio>\d{1,3})\] (?P<msg>.*)$")

    def _ts(self, g):
        return parse_timestamp(g["ts"])

    def _level(self, g, line):
        p = int(g["prio"])
        return "error" if p >= 300 else ("warn" if p >= 200 else "info")

    def _fields(self, g, line):
        return {"priority": int(g["prio"])}


# ── Tru64 uerf error-report formatter ─────────────────────────────────────────
#   ----- EVENT INFORMATION -----
class Tru64UerfAdapter(LogAdapter):
    name = "tru64_uerf"
    language = "any"
    _RE = re.compile(r"^-{3,}\s*(?P<section>EVENT INFORMATION|UNIT INFORMATION|"
                     r"DEVICE INFORMATION|ERROR INFORMATION|[A-Z ]+INFORMATION)\s*-{3,}\s*$")
    _ENTRY = re.compile(r"^\*{3}\s*ENTRY\s+\d+")

    def detect(self, sample_lines):
        def hit(x):
            x = x.strip()
            return bool(self._RE.match(x) or self._ENTRY.match(x))
        return ratio_detect(sample_lines, lambda el: block_ratio(el, hit, threshold=0.4))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if m:
            return self._event(level="info", message=f'uerf section: {m.group("section").strip()}',
                               source="tru64.uerf", fields={"section": m.group("section").strip()},
                               category="event", raw=line)
        if self._ENTRY.match(s):
            return self._event(level="info", message=s, source="tru64.uerf",
                               category="event", raw=line)
        return None


# ── OpenLDAP slapo-auditlog overlay (LDIF change record) ──────────────────────
#   # modify 1196797577 dc=example,dc=com cn=admin,dc=example,dc=com conn=0
class OpenldapAuditlogAdapter(RxAdapter):
    name = "openldap_auditlog"
    language = "any"
    default_source = "slapd.auditlog"
    _RE = re.compile(
        r"^# (?P<op>add|modify|delete|modrdn) (?P<epoch>\d{9,10}) (?P<suffix>\S+) "
        r"(?P<modifier>\S+) conn=(?P<conn>\d+)")

    def _ts(self, g):
        return float(g["epoch"]) * 1000.0

    def _level(self, g, line):
        return "info"

    def _fields(self, g, line):
        return {"op": g["op"], "suffix": g["suffix"], "modifier": g["modifier"],
                "conn": int(g["conn"])}


# ══════════════════════════════════════════════════════════════════════════════
#  Second wave — more well-anchored low-tier formats
# ══════════════════════════════════════════════════════════════════════════════

# ── SimGrid / DES simulation log (XBT default format) ─────────────────────────
#   [host-01:worker:(2) 12.345678] app/main.c:42: Task done
class SimgridAdapter(RxAdapter):
    name = "simgrid"
    language = "cpp"
    default_source = "simgrid"
    _RE = re.compile(
        r"^\[(?P<host>[\w.\-]+):(?P<proc>[\w.\-]+):\((?P<pid>\d+)\) (?P<simtime>[\d.]+)\] "
        r"(?P<loc>[\w./\-]+:\d+): (?P<msg>.*)$")

    def _level(self, g, line):
        return ""

    def _ts(self, g):
        return None

    def _fields(self, g, line):
        return {"host": g["host"], "process": g["proc"], "pid": int(g["pid"]),
                "sim_time": float(g["simtime"]), "source_loc": g["loc"]}


# ── Samba winbindd / smbd classic debug log ───────────────────────────────────
#   [2024/07/20 11:00:00.123456,  3] path:line(func)
class SambaWinbindAdapter(LogAdapter):
    name = "samba_winbind"
    language = "c"
    _HDR = re.compile(
        r"^\[(?P<yr>\d{4})/(?P<mo>\d{2})/(?P<dy>\d{2}) (?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})\.(?P<us>\d{6}),\s*"
        r"(?P<dbg>\d{1,2})\]\s+(?P<loc>\S+):(?P<lineno>\d+)\((?P<func>[^)]+)\)\s*$")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            return bool(subs) and self._HDR.match(subs[0].strip()) is not None
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line):
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if not subs or not self._HDR.match(subs[0].strip()):
            return None
        g = self._HDR.match(subs[0].strip()).groupdict()
        body = " ".join(x.strip() for x in subs[1:]) if len(subs) > 1 else g["func"]
        dbg = int(g["dbg"])
        lvl = "error" if re.search(r"error|fail|denied", body, re.I) else (
            "debug" if dbg >= 3 else "info")
        return self._event(level=lvl, message=body, source=g["func"],
                           ts_ms=mk_ts(g["yr"], g["mo"], g["dy"], g["hh"], g["mi"], g["ss"], int(g["us"])),
                           fields={"debug_level": dbg, "source_file": g["loc"],
                                   "line": int(g["lineno"]), "function": g["func"]},
                           category="debug", raw=line)


# ── DICOM dcmdump dataset dump ────────────────────────────────────────────────
#   (0008,0060) CS [CT]                     #   2, 1 Modality
class DicomDcmdumpAdapter(RxAdapter):
    name = "dicom_dcmdump"
    language = "any"
    default_source = "dicom"
    _RE = re.compile(
        r"^\s*(?P<group>[0-9a-fA-F]{4}),(?P<elem>[0-9a-fA-F]{4})\)\s+(?P<vr>[A-Z]{2})\s+"
        r"(?P<value>\[[^\]]*\]|=\S+|\(no value available\)|\S.*?)\s*#\s*(?P<len>-?\d+),\s*(?P<vm>\d+|1-n)\s+(?P<kw>\S+)\s*$")

    def _level(self, g, line):
        return ""

    def _ts(self, g):
        return None

    def _fields(self, g, line):
        return {"tag": f'({g["group"]},{g["elem"]})', "vr": g["vr"], "keyword": g["kw"]}

    def parse_line(self, line):
        # the leading "(" is stripped by RxAdapter's .strip()? no — restore it
        s = line.rstrip("\r\n")
        subs = split_any(s) or [s]
        m = None
        for x in subs:
            m = self._RE.match(x.strip().lstrip("("))
            if m:
                break
        if not m:
            return None
        g = m.groupdict()
        return self._event(level="", message=f'{g["kw"]} {g["value"]}'.strip(),
                           source="dicom",
                           fields={"tag": f'({g["group"]},{g["elem"]})', "vr": g["vr"],
                                   "keyword": g["kw"]}, category="event", raw=line)

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda el: block_ratio(el, lambda x: bool(self._RE.match(x.strip().lstrip("(")))))


# ── HL7 v2 batch/file envelope (FHS/BHS/BTS/FTS) ──────────────────────────────
#   BHS|^~\&|
class Hl7BatchAdapter(LogAdapter):
    name = "hl7v2_batch"
    language = "any"
    _RE = re.compile(r"^(?P<seg>FHS|BHS|BTS|FTS)\|")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: block_ratio(
            el, lambda x: bool(self._RE.match(x.strip()))))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        seg = m.group("seg")
        kind = {"FHS": "file header", "BHS": "batch header",
                "BTS": "batch trailer", "FTS": "file trailer"}[seg]
        return self._event(level="info", message=f"HL7 v2 {kind}", source="hl7.batch",
                           fields={"segment": seg}, category="event", raw=line)


# ── Apache ActiveMQ Classic audit.log (no timestamp; 3 pipe fields) ───────────
#   INFO  | User admin requested action purge on queue TEST.QUEUE | qtp1234-56
class ActivemqAuditAdapter(LogAdapter):
    name = "activemq_audit"
    language = "java"
    _RE = re.compile(r"^(?P<level>INFO|WARN|ERROR)\s+\|\s+(?P<msg>.+?)\s+\|\s+(?P<thread>[^|]+)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda el: block_ratio(
            el, lambda x: bool(self._RE.match(x.strip()))))

    def parse_line(self, line):
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"], source="activemq.audit",
                           fields={"thread": g["thread"].strip()}, category="event", raw=line)


# ── PX4 / ArduPilot console + MAVLink STATUSTEXT ──────────────────────────────
#   INFO  [commander] Takeoff detected
class Px4MavlinkAdapter(RxAdapter):
    name = "px4_mavlink"
    language = "cpp"
    _RE = re.compile(r"^(?P<level>INFO|WARN|ERROR|DEBUG)\s{1,3}\[(?P<mod>[\w\-]+)\]\s+(?P<msg>\S.*)$")

    def parse_line(self, line):
        ev = super().parse_line(line)
        if ev:
            ev["source"] = self._m1(line).group("mod")
        return ev


# ── Acronis Cyber Protect / True Image agent log ──────────────────────────────
#   2026-07-21T02:00:01:123+02:00 3EA0 I00640000: Backup activity 'Entire PC' started.
class AcronisAdapter(RxAdapter):
    name = "acronis"
    language = "any"
    default_source = "acronis"
    _RE = re.compile(
        r"^(?P<yr>\d{4})-(?P<mo>\d{2})-(?P<dy>\d{2})T(?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2}):(?P<ms>\d{3})"
        r"(?P<tz>[+-]\d{2}:\d{2})? (?P<tid>[0-9A-Fa-f]{2,8}) (?P<sev>[IWEF])(?P<code>[0-9A-Fa-f]{8}): (?P<msg>.*)$")
    _LVL = {"I": "info", "W": "warn", "E": "error", "F": "fatal"}

    def _ts(self, g):
        base = mk_ts(g["yr"], g["mo"], g["dy"], g["hh"], g["mi"], g["ss"], int(g["ms"]) * 1000)
        return base

    def _level(self, g, line):
        return self._LVL.get(g["sev"], "info")

    def _fields(self, g, line):
        return {"thread": g["tid"], "event_code": g["sev"] + g["code"]}


# ── Azure Backup MARS agent (CBEngineCurr.errlog) ─────────────────────────────
#   3EA0 0F2C 07/21 02:00:01.123 03 backupasync.cpp(1234) [00000000] WARNING Failed: Hr: = [0x...]
class AzureMarsAdapter(RxAdapter):
    name = "azure_mars"
    language = "any"
    default_source = "azure.mars"
    _RE = re.compile(
        r"^(?P<tid>[0-9A-Fa-f]{2,8}) (?P<pid>[0-9A-Fa-f]{2,8}) (?P<mo>\d{2})/(?P<dy>\d{2}) "
        r"(?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})\.(?P<ms>\d{3}) \d+ "
        r"(?P<file>\S+\(\d+\)) \[(?P<ctx>[0-9A-Fa-f]+)\] (?P<level>NORMAL|WARNING|FATAL|ERROR)\s+(?P<msg>.*)$")
    _LVL = {"NORMAL": "info", "WARNING": "warn", "FATAL": "fatal", "ERROR": "error"}

    def _ts(self, g):
        now = datetime.now()
        return mk_ts(now.year, g["mo"], g["dy"], g["hh"], g["mi"], g["ss"], int(g["ms"]) * 1000)

    def _level(self, g, line):
        return self._LVL.get(g["level"], "info")

    def _fields(self, g, line):
        f = {"thread": g["tid"], "pid": g["pid"], "source_file": g["file"]}
        hr = re.search(r"Hr:\s*=?\s*\[?(0x[0-9A-Fa-f]+)\]?", g.get("msg", ""))
        if hr:
            f["hresult"] = hr.group(1)
        return f


# ══════════════════════════════════════════════════════════════════════════════
#  Registration
# ══════════════════════════════════════════════════════════════════════════════

_ADAPTERS = [
    # scientific/HPC
    GaussianScfAdapter(), VaspOszicarAdapter(), Cp2kAdapter(), OpenmcAdapter(),
    # debuggers/profilers
    GdbInternalAdapter(), LldbLogAdapter(), PerfAnnotateAdapter(), StracePidPrefixAdapter(),
    # build/dev
    HomebrewAdapter(), PoetryAdapter(), RollupAdapter(), TestKitchenAdapter(),
    VagrantAdapter(), CapistranoAdapter(), BuildkiteJobAdapter(), TravisMarkerAdapter(),
    PackerMachineAdapter(),
    # datastores
    QuestdbAdapter(), OrientdbAdapter(),
    # networking/proxy/vpn
    AccelPppAdapter(), CactiPollerAdapter(), CheckmkCmcAdapter(), CheckpointLeaAdapter(),
    CiscoMerakiFlowAdapter(), DnsdistVerboseAdapter(), EximMsglogAdapter(), FrpAdapter(),
    GraphiteCarbonAdapter(), HeimdalKdcAdapter(), JujuDebugAdapter(), KadmindAdapter(),
    KnotDnsAdapter(), KnotResolverAdapter(), MuninAdapter(), MsmtpAdapter(), NetbirdAdapter(),
    Opendnp3Adapter(), PritunlAdapter(), RacoonAdapter(), SocatAdapter(),
    TacacsAccountingAdapter(), V2rayAdapter(), Xl2tpdAdapter(), DynatraceOneagentAdapter(),
    # vpn/security
    OpenconnectAdapter(), OpenvpnAsAdapter(), WgQuickAdapter(), WireguardWinAdapter(),
    ClamavAdapter(), FileIntegrityAdapter(), SimplesamlphpAdapter(), PingfederateAuditAdapter(),
    CasAuditAdapter(), SophosSavAdapter(), McafeeEnsAdapter(), VcsaApplmgmtAdapter(),
    GlobalprotectPangpsAdapter(),
    # cloud/aws
    CfnInitAdapter(), AwsGlobalAcceleratorFlowAdapter(), AwsTransitGatewayFlowAdapter(),
    Route53QueryAdapter(), OpenwhiskActivationAdapter(), RdsMariadbAuditAdapter(),
    # backup
    BackuppcAdapter(), ResticTextAdapter(), RsnapshotAdapter(), ArcserveUdpAdapter(),
    VeritasBexAdapter(),
    # os platform
    HpuxRcAdapter(), HpuxShutdownAdapter(), MacosFsckAdapter(), MacosWifiAdapter(),
    MacosCoresimulatorAdapter(), MacosMdmAdapter(), MacosInstallHistoryAdapter(),
    OpendirectorydAdapter(), LastcommAdapter(),
    # firmware/rtos
    SeabiosAdapter(), RpiVcAdapter(), MbedOsErrorAdapter(), ContikiNgAdapter(),
    NordicNrfAdapter(), ThreadxAdapter(), TiRtosAdapter(), SwitchDiagAdapter(),
    UefiSecurebootAdapter(), QnxSlog2Adapter(),
    # industrial/OT
    GeIfixAlarmAdapter(), VeederRootAtgAdapter(), Lib60870Adapter(), OpcUaTraceAdapter(),
    OpenplcAdapter(),
    # telephony/voip
    JitsiJvbAdapter(), YateAdapter(), OracleSbcSipmsgAdapter(), PulsarFunctionAdapter(),
    MailmanAdapter(),
    # middleware/java/vmware
    OctopusServerAdapter(), OctopusTaskAdapter(), OvirtJbossBootAdapter(), IrisMainAdapter(),
    IrisMessagesAdapter(), LoginsightLsAdapter(), VcenterVpxdLegacyAdapter(),
    VmwareSectionAdapter(), QpidDispatchAdapter(), HabitatSupAdapter(),
    # storage/mail
    DellUnityAdapter(), SolarisFmaAdapter(), HmailserverAdapter(), ProcmailAdapter(),
    OracleMessagingMailAdapter(),
    # monitoring/metrics/ids
    SflowtoolAdapter(), SuricataStatsAdapter(), VarnishstatAdapter(), SnortFullAdapter(),
    OracleAdrciAdapter(),
    # legacy unix/mainframe
    Acf2Adapter(), ZvseConsoleAdapter(), Tru64EvmAdapter(), Tru64UerfAdapter(),
    OpenldapAuditlogAdapter(),
    # second wave
    SimgridAdapter(),
    SambaWinbindAdapter(), DicomDcmdumpAdapter(), Hl7BatchAdapter(),
    ActivemqAuditAdapter(), Px4MavlinkAdapter(), AcronisAdapter(), AzureMarsAdapter(),
]

for _a in _ADAPTERS:
    register_adapter(_a)

# These ride a shared syslog / BIND silhouette; register before="syslog" so a
# 1.0-confidence Cisco-Meraki / kadmind / Yealink / WatchGuard / Infoblox line
# outranks the generic syslog envelope on a tie (they still load AFTER every
# earlier module, so any earlier specific adapter keeps priority).
for _a, _before in ((CiscoMerakiFlowAdapter(), "syslog"),
                    (KadmindAdapter(), "syslog"),
                    (YealinkSyslogAdapter(), "syslog"),
                    (WatchguardFireboxAdapter(), "syslog"),
                    (InfobloxDnsAdapter(), "syslog"),
                    (SimplesamlphpAdapter(), "syslog"),
                    (MacosCoresimulatorAdapter(), "syslog"),
                    (MailmanAdapter(), "syslog")):
    register_adapter(_a, before=_before)
