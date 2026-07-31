"""
Tests for the BATCH-7 family adapters + the batch-7 GAP FIXES.

Pure stdlib (no pytest). Run:
    python3 python_backend/connectors/test_adapters_batch7.py
Exits non-zero on any failure so it gates CI / packaging.

The hard rule (same as batches 2-6): for EVERY adapter added or fixed, its OWN
sample — pulled from docs/log_catalog_master.json — must resolve through
detect_adapter([sample]) to THAT adapter (multi-line samples fed WHOLE, as one
element). A resolution to a fallback (structural/generic_ts/raw) or to the
wrong named adapter is a FAIL.

Batch 7 deepens the MEDIUM tier: medium-priority non-binary fallthrough went
224 -> 157 (62 new adapters + 3 gap fixes = 67 medium formats newly named, of
564 medium non-binary total -> 72.2% named), plus 4 bonus low-tier formats
(QE/DFT logs, callgrind, dnscache/tinydns TAI64N).
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent))          # python_backend
sys.path.insert(0, str(_HERE.parent.parent.parent))   # repo root
sys.path.insert(0, str(_HERE.parent))                 # connectors

from connectors import log_adapters as la  # noqa: E402

_fails = 0
_FALLBACKS = {"structural", "generic_ts", "raw"}


def check(name: str, cond: bool, detail: str = ""):
    global _fails
    status = "ok  " if cond else "FAIL"
    if not cond:
        _fails += 1
    print(f"  [{status}] {name}{'  — ' + detail if detail and not cond else ''}")


# ── Load the format catalog ───────────────────────────────────────────────────
_CATALOG_PATH = None
for _p in (_HERE.parents[3] / "docs" / "log_catalog_master.json",
           _HERE.parents[2] / "docs" / "log_catalog_master.json",
           Path("docs/log_catalog_master.json")):
    if _p.exists():
        _CATALOG_PATH = _p
        break

_CATALOG = {}
if _CATALOG_PATH:
    for _e in json.loads(_CATALOG_PATH.read_text())["catalog"]:
        _CATALOG[_e["name"]] = _e


def catalog_sample(catalog_name: str) -> str:
    e = _CATALOG.get(catalog_name)
    return e["sample_line"] if e else ""


# ── 1. BATCH-7 adapters: catalog_name → expected adapter ─────────────────────
NEW_ADAPTERS = {
    # HPC schedulers + science/simulation outputs (bigdata)
    "HTCondor job event log (ULOG)":              "htcondor_ulog",
    "HTCondor job/event log":                     "htcondor_ulog",
    "Grid Engine messages/job output (SGE/UGE/OGE)": "grid_engine",
    "Grid Engine messages/output":                "grid_engine",
    "PBS Pro / OpenPBS accounting log":           "pbs_accounting",
    "PBS epilogue resource block (.o)":           "pbs_epilogue",
    "PBS epilogue resource block (job .o file)":  "pbs_epilogue",
    "SLURM sacct/scontrol structured":            "slurm_sacct",
    "LSF lsf.log / bsub job output":              "lsf_daemon",
    "OpenMPI runtime error/ORTE/PRTE":            "openmpi_orte",
    "OpenFOAM solver log":                        "openfoam",
    "AMBER mdout":                                "amber_mdout",
    "Quantum ESPRESSO pw.x output":               "quantum_espresso",
    "Quantum ESPRESSO / DFT SCF log":             "quantum_espresso",   # bonus (low tier)
    "gem5 simout / console":                      "gem5",
    # IaC / config management / CI consoles (cicd + devtools)
    "packer-ui":                                  "packer_ui",
    "pulumi-cli":                                 "pulumi_cli",
    "chef-run-doc-output":                        "chef_doc",
    "salt-state-output":                          "salt_state",
    "GIT_TRACE2 perf target":                     "git_trace2_perf",
    # profilers / valgrind (profiling)
    "Valgrind Callgrind format":                  "callgrind",
    "Valgrind Callgrind output":                  "callgrind",          # bonus (low tier)
    "Valgrind XML output":                        "valgrind_xml",
    "perf report (text/stdio)":                   "perf_report",
    "strace -c summary table":                    "strace_summary",
    # apple
    "os_signpost unified log":                    "os_signpost",
    "ios-idevicesyslog":                          "ios_syslog",
    # databases / warehouses (database)
    "aws-aurora-mysql-audit-csv":                 "aurora_audit_csv",
    "aws-redshift-connection-log":                "redshift_connection",
    "aws-redshift-useractivity-log":              "redshift_useractivity",
    "vertica-log":                                "vertica",
    "foundationdb-trace-xml":                     "foundationdb_xml",
    # security
    "f5-bigip-apm-syslog":                        "f5_apm",
    "f5-bigip-dos-kv-semicolon":                  "f5_dos_kv",
    "clamav-clamscan-stdout-summary":             "clamav_scan",
    "microsoft-defender-mplog-text":              "defender_mplog",
    # rendered Windows event bodies (os_platform)
    "powershell-classic-engine-400-800":          "powershell_classic_engine",
    "windows-dotnet-runtime-1026":                "windows_dotnet_crash",
    "windows-application-hang-1002":              "windows_app_hang",
    "windows_powershell_operational":             "powershell_scriptblock",
    # mainframe
    "zvm-cp-console":                             "zvm_cp",
    "jes3-dlog":                                  "jes3_dlog",
    # backup
    "amanda-logfile":                             "amanda",
    "netbackup-vxul-vxlogview":                   "netbackup_vxul",
    "duplicati-log-file":                         "duplicati",
    "percona-xtrabackup-log":                     "percona_xtrabackup",
    # telecom / PBX
    "3cx-activity-log":                           "threecx",
    "asterisk-cli-verbose":                       "asterisk_cli",
    "asterisk-cel-csv":                           "asterisk_cel_csv",
    # network
    "bird-log":                                   "bird",
    "nfdump-line":                                "nfdump",
    "sflowtool-line-csv":                         "sflowtool",
    "sonic-nos-syslog":                           "sonic_nos",
    "pihole-ftl-log":                             "pihole_ftl",
    "stunnel-log":                                "stunnel",
    "kea-leases4-csv":                            "kea_leases_csv",
    # messaging / mail
    "artemis-audit-log":                          "artemis_audit",
    "qmail-send-multilog":                        "tai64n_multilog",
    "dnscache-tai64n-log":                        "tai64n_multilog",    # bonus (low tier)
    "tinydns-tai64n-log":                         "tai64n_multilog",    # bonus (low tier)
    "exchange-smtp-protocol-log":                 "exchange_smtp_csv",
    # cloud
    "azure-storage-analytics-log":                "azure_storage_analytics",
    "cloudfront-realtime-tsv":                    "cloudfront_realtime",
    "kinesis-agent-log":                          "kinesis_agent",
    # virt
    "vmware-python-service-log":                  "vmware_python",
    "openstack-instance-tag":                     "openstack_instance",
    # industrial / SCADA
    "winccoa-pvss-log":                           "winccoa",
    "kepware-kepserverex-event-log":              "kepware",
}

# gap fixes routing catalog formats to EXISTING adapters:
#   • rust_tracing  — accepts single-segment targets with tracing-fmt level
#                     padding (meilisearch / index_scheduler)
#   • exim_mainlog  — accepts the id-less REJECTLOG form (vocabulary-gated)
#   • hl7v2         — strips MLLP VT/FS-CR wire framing (raw bytes AND the
#                     literal \x0b/\x1c\x0d escape text)
GAP_FIXES = {
    "meilisearch-tracing-log":                    "rust_tracing",
    "exim-rejectlog":                             "exim_mainlog",
    "hl7v2-mllp-frame":                           "hl7v2",
}

print("\n1. batch-7 adapters resolve their own catalog samples")
_B7_SAMPLES = []
for cat_name, adapter_name in {**NEW_ADAPTERS, **GAP_FIXES}.items():
    sample = catalog_sample(cat_name)
    if not sample:
        check(f"{cat_name} (catalog entry present)", False, "sample missing")
        continue
    _B7_SAMPLES.append((cat_name, adapter_name, sample))
    ad, conf, scores = la.detect_adapter([sample])
    check(f"{cat_name} → {adapter_name}", ad.name == adapter_name,
          f"got {ad.name}@{conf:.2f}")
    if ad.name == adapter_name:
        ev = ad.parse_line(sample)
        check(f"{cat_name} parses", ev is not None and isinstance(ev, dict))


print("\n2. no fallback wins a batch-7 sample")
for cat_name, adapter_name, sample in _B7_SAMPLES:
    ad, _, _ = la.detect_adapter([sample])
    check(f"{cat_name} not fallback", ad.name not in _FALLBACKS,
          f"fell to {ad.name}")


print("\n3. inline real samples (template catalog entries + extra shapes)")
INLINE = [
    # greenplum-csv-log's catalog sample is a column-name TEMPLATE (correctly
    # undetectable); a REAL row routes:
    ("greenplum_csv",
     '2020-01-01 00:00:00.000000 EST,"gpadmin","testdb",p12345,th123456789,'
     '"[local]",,2020-01-01 00:00:00 EST,0,con10,cmd3,seg-1,,,,,"LOG","00000",'
     '"statement: SELECT 1",,,,,,"SELECT 1",0,,"postgres.c",1234,'),
    # scontrol show job k=v form of slurm_sacct
    ("slurm_sacct",
     "JobId=1234 JobName=train.sh UserId=alice(1000) GroupId=hpc(2000) "
     "JobState=FAILED ExitCode=1:0"),
    # kea lease DATA row (catalog sample is the header)
    ("kea_leases_csv",
     "192.168.1.100,aa:bb:cc:dd:ee:ff,01:aa:bb:cc:dd:ee:ff,3600,1626778872,"
     "1,0,0,laptop,0,,0"),
    # HL7 MLLP frame with RAW control bytes (catalog carries the escape text)
    ("hl7v2",
     "\x0bMSH|^~\\&|MegaReg|XYZHospC|SuperOE|XYZImgCtr|20060529090131-0500||"
     "ADT^A01^ADT_A01|01052901|P|2.5\x1c\x0d"),
    # CloudFront realtime row with REAL tabs (catalog carries literal \t)
    ("cloudfront_realtime",
     "1573840000.762\t192.0.2.100\t0.001\t200\t392\tGET\thttps\t"
     "d111111abcdef8.cloudfront.net\t/index.html"),
    # HTCondor multi-line record with detail + "..." terminator
    ("htcondor_ulog",
     "009 (4321.000.000) 2026-07-20 15:00:00 Job was aborted by the user.\n"
     "\tvia condor_rm (by user alice)\n..."),
    # packer detail line (4-space indent) inside a step block
    ("packer_ui",
     "==> amazon-ebs: Provisioning with shell script...\n"
     "    amazon-ebs: Reading /etc/os-release"),
    # exchange SMTP #Fields header line
    ("exchange_smtp_csv",
     "#Fields: date-time,connector-id,session-id,sequence-number,"
     "local-endpoint,remote-endpoint,event,data,context"),
    # percona xtrabackup era-A dated line
    ("percona_xtrabackup", "160906 10:19:17 innobackupex: Starting the backup operation"),
    # gem5 fatal
    ("gem5", "fatal: Unable to find destination for [0x0:0x1000] on system.membus\n"
             "info: Entering event queue @ 0."),
]
for adapter_name, sample in INLINE:
    ad, conf, _ = la.detect_adapter([sample])
    check(f"inline → {adapter_name}", ad.name == adapter_name,
          f"got {ad.name}@{conf:.2f}")
    if ad.name == adapter_name:
        check(f"inline {adapter_name} parses", ad.parse_line(sample) is not None)


print("\n4. parse-correctness spot checks (level / category / ts / fields)")


def _ev(name, sample):
    return la.get_adapter(name).parse_line(sample)


ev = _ev("htcondor_ulog",
         "009 (4321.000.000) 2026-07-20 15:00:00 Job was aborted by the user.")
check("htcondor 009 aborted → error", ev and ev["level"] == "ERROR"
      and ev["category"] == "error")
check("htcondor job id field", ev and ev["data"].get("job") == "4321.0.0"
      or ev["data"].get("job") == "4321.000.000")

ev = _ev("grid_engine", "05/12/2024 14:23:01|worker|node001|E|job 7 failed")
check("grid_engine E → error", ev and ev["level"] == "ERROR")
check("grid_engine ts parsed", ev and ev["ts_ms"] is not None)

ev = _ev("pbs_accounting", "07/20/2026 14:00:01;A;99.pbs;user=alice group=hpc")
check("pbs_accounting A(abort) → warn", ev and ev["level"] == "WARN")

ev = _ev("slurm_sacct", "JobId=1234 JobName=x UserId=a(1) JobState=FAILED ExitCode=1:0")
check("slurm scontrol FAILED → error", ev and ev["level"] == "ERROR")

ev = _ev("openmpi_orte",
         "--------------------------------------------------------------------------\n"
         "mpirun noticed that process rank 3 with PID 0 on node c04 exited on "
         "signal 11 (Segmentation fault).")
check("openmpi_orte → error + rank/signal", ev and ev["level"] == "ERROR"
      and ev["data"].get("rank") == 3 and ev["data"].get("signal") == 11)

ev = _ev("openfoam", "smoothSolver:  Solving for Ux, Initial residual = 0.0123, "
                     "Final residual = 1e-06, No Iterations 3")
check("openfoam residual fields", ev and ev["data"].get("final_residual") == 1e-06
      and ev["data"].get("iterations") == 3)

ev = _ev("windows_dotnet_crash",
         "Application: MyApp.exe\r\nException Info: System.NullReferenceException: x\r\n"
         "   at MyApp.Program.Main(String[] args)")
check("dotnet 1026 → fatal + crash category", ev and ev["level"] == "FATAL"
      and ev["category"] == "crash")

ev = _ev("windows_app_hang",
         "The program Explorer.EXE version 10.0 stopped interacting with Windows "
         "and was closed.\r\nHang type: Top level window is idle")
check("app hang → error + crash category", ev and ev["level"] == "ERROR"
      and ev["category"] == "crash" and ev["data"].get("hang_type"))

ev = _ev("clamav_scan", "/tmp/x.exe: Win.Test.EICAR_HDB-1 FOUND")
check("clamav FOUND → error", ev and ev["level"] == "ERROR")

ev = _ev("f5_dos_kv", catalog_sample("f5-bigip-dos-kv-semicolon"))
check("f5_dos severity=3 → error", ev and ev["level"] == "ERROR"
      and ev["data"].get("dos_attack_id") == "2843816221")

ev = _ev("foundationdb_xml", '<Event Severity="40" Time="1578010020.5" '
                             'Type="Net2RunLoopError" Machine="10.0.0.1:4500" />')
check("fdb Severity 40 → error", ev and ev["level"] == "ERROR")
check("fdb epoch Time → ts", ev and ev["ts_ms"] == 1578010020500.0)

ev = _ev("bird", "2019-06-19 17:47:03.822 <ERR> bgp1: Connection lost")
check("bird <ERR> → error", ev and ev["level"] == "ERROR")

ev = _ev("aurora_audit_csv", catalog_sample("aws-aurora-mysql-audit-csv"))
check("aurora µs epoch → 2022 ts", ev and ev["ts"].startswith("2022-03"))

ev = _ev("tai64n_multilog", "@4000000052fafd8d373b5dbc delivery 9: failure")
check("tai64n decodes to 2014 + failure → error",
      ev and ev["level"] == "ERROR" and (ev["ts"] or "").startswith("2014-"))

ev = _ev("redshift_connection", catalog_sample("aws-redshift-connection-log"))
check("redshift conn fields", ev and ev["data"].get("username") == "awsuser"
      and ev["data"].get("dbname") == "dev")

ev = _ev("duplicati", "2026-07-21 02:00:01 +02 - [Error-Duplicati.Library.Main-Failed]: boom")
check("duplicati Error tag → error + tz honored", ev and ev["level"] == "ERROR"
      and ev["ts"] == "2026-07-21T00:00:01.000Z")

ev = _ev("sonic_nos", catalog_sample("sonic-nos-syslog"))
check("sonic container#program split", ev and ev["data"].get("container") == "swss"
      and ev["data"].get("program") == "orchagent")

ev = _ev("gem5", "panic: Tried to read unmapped address 0xdeadbeef.\n"
                 "info: Entering event queue @ 0.")
check("gem5 panic → fatal/error category", ev and ev["level"] == "FATAL"
      and ev["category"] == "error")

ev = _ev("os_signpost", catalog_sample("os_signpost unified log"))
check("os_signpost → event category + subsystem", ev and ev["category"] == "event"
      and ev["data"].get("subsystem") == "com.acme.app")

ev = _ev("valgrind_xml", catalog_sample("Valgrind XML output"))
check("valgrind_xml kind → error", ev and ev["level"] == "ERROR"
      and ev["data"].get("kind") == "InvalidWrite")

ev = _ev("exim_mainlog", catalog_sample("exim-rejectlog"))
check("exim rejectlog → warn + reject source", ev and ev["level"] == "WARN"
      and ev["source"] == "exim.reject")


print("\n5. negative guards — generic shapes stay with their owners")
# a Microsoft.Extensions.Logging line must stay with the core dotnet adapter,
# NOT the vocabulary-gated gem5 lowercase 'info:' prefix
ad, _, _ = la.detect_adapter(["info: Microsoft.Hosting.Lifetime[0] Application started."])
check("MEL 'info:' line stays dotnet", ad.name == "dotnet", f"got {ad.name}")
# a bare 'Time = 0.5' block (no residual line) must NOT be claimed by openfoam
ad, _, _ = la.detect_adapter(["Time = 0.5\nTime = 1.0\nTime = 1.5"])
check("bare 'Time =' block not openfoam", ad.name != "openfoam", f"got {ad.name}")
# generic 'ERROR: text' stays with the fallbacks, not any batch-7 adapter
_B7_NAMES = set(NEW_ADAPTERS.values())
ad, _, _ = la.detect_adapter(["ERROR: something went wrong"])
check("generic ERROR line not batch-7", ad.name not in _B7_NAMES, f"got {ad.name}")
# plain macOS ASL (no parenthesized library) stays with macos_asl, not ios_syslog
ad, _, _ = la.detect_adapter(
    ["Jul 20 12:34:56 alices-macbook-pro com.apple.xpc.launchd[1] <Notice>: hi"])
check("plain ASL stays macos_asl", ad.name == "macos_asl", f"got {ad.name}")
# double-colon rust_tracing form still routes (gap fix must not regress it)
ad, _, _ = la.detect_adapter(
    ["2023-01-01T00:00:00.000000Z  INFO vector::app: Log level is enabled. level=\"info\""])
check("rust_tracing :: form still routes", ad.name == "rust_tracing", f"got {ad.name}")
# a bare ISO+level+word line WITHOUT the level padding stays generic
ad, _, _ = la.detect_adapter(["2023-01-01T00:00:00.000Z INFO server: started"])
check("unpadded single-segment line stays generic", ad.name != "rust_tracing",
      f"got {ad.name}")
# exim mainlog (id-carrying) still routes after the rejectlog fix
ad, _, _ = la.detect_adapter(
    ["2002-10-31 08:57:53 16ZCW1-0005MB-00 <= kryten@dwarf.fict.example U=exim P=local S=5678"])
check("exim mainlog still routes", ad.name == "exim_mainlog", f"got {ad.name}")


print("\n6. registry integrity")
names = [a.name for a in la.REGISTRY]
check("no duplicate names", len(names) == len(set(names)))
check("registry ≥ 388 entries", len(names) >= 388, f"got {len(names)}")
check("tail is [structural, raw]", names[-2:] == ["structural", "raw"],
      f"got {names[-2:]}")
check("every batch-7 adapter registered",
      _B7_NAMES.issubset(set(names)),
      f"missing {_B7_NAMES - set(names)}")


print("\n7. hostile sweep — whole registry, 0 raises")
_HOSTILE = [
    "", "   ", "\t\t\n",
    "\x00\x01\x02\xff\xfe binary \x7f\x80",
    "\xed\xa0\x80 unicode ‮ RTL \U0001F600 emoji  ",
    "A" * 5000, ("k=v " * 800)[:5000],
    "line1\\nline2\\nline3", "real\nnewlines\nhere",
    '{"truncated": "jso', '<Event Severity="', "005 (12", "@40000000",
    "1.0;", "FLOW,", "==>", "    +  ", "% time", "# Overhead",
    "Resources Used:", "JobID|", "d0 | ", "xtrabackup:", "HCP",
    "﻿BOM line", "\x1b[31mANSI\x1b[0m ERROR x",
    "05/12/2024 14:23:01|", "authenticated |",
    "'2023-06-26T18:02:47Z UTC [", "Creating Scriptblock text (",
]
_raises = 0
for a in la.REGISTRY:
    for h in _HOSTILE:
        try:
            a.detect([h])
            a.detect([h, h])
            a.parse_line(h)
        except Exception as exc:   # pragma: no cover - the point is 0 of these
            _raises += 1
            print(f"    RAISE {a.name} on {h[:30]!r}: {type(exc).__name__}: {exc}")
check("0 raises across registry × hostile inputs", _raises == 0,
      f"{_raises} raises")


print("\n8. full catalog sweep — 0 raises across all entries")
_bad = 0
for _name, _e in _CATALOG.items():
    s = _e.get("sample_line") or ""
    try:
        ad, conf, _ = la.detect_adapter([s])
        ad.parse_line(s)
        la.parse_line(s)
    except Exception as exc:       # pragma: no cover
        _bad += 1
        print(f"    RAISE on catalog {_name!r}: {type(exc).__name__}: {exc}")
check(f"0 raises across {len(_CATALOG)} catalog samples", _bad == 0,
      f"{_bad} raises")


print(f"\n{'=' * 70}")
if _fails:
    print(f"BATCH-7 SUITE: {_fails} FAILURE(S)")
    sys.exit(1)
print("BATCH-7 SUITE: all checks passed")
sys.exit(0)
