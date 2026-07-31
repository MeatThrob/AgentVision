"""
Tests for the BATCH-4 family adapters + the batch-4 GAP FIXES.

Pure stdlib (no pytest). Run:
    python3 python_backend/connectors/test_adapters_batch4.py
Exits non-zero on any failure so it gates CI / packaging.

The hard rule (same as batches 2-3): for EVERY adapter added or fixed, its OWN
sample — pulled from docs/log_catalog_master.json (or an inline REAL sample
where the catalog entry is a template) — must resolve through
detect_adapter([sample]) to THAT adapter. A resolution to a fallback
(structural/generic_ts/raw) or to the wrong named adapter is a FAIL.

Plus: NEGATIVE ownership guards (formats batch 4 must NOT steal), parse-quality
spot checks, registry invariants (217 entries, pinned tail), a hostile-input
sweep across the WHOLE registry (0 raises), and the full 1430-catalog sweep.
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


# ── 1. BATCH-4 adapters: catalog_name → expected adapter ─────────────────────
NEW_ADAPTERS = {
    # mainframe / midrange / legacy UNIX
    "zos-syslog-hardcopy":               "zos_syslog",
    "jes2-hasp-messages":                "jes2_hasp",
    "jes2-jesmsglg":                     "jes2_hasp",
    "jes2-jesmsglg-joblog":              "jes2_hasp",
    "jes2-jesysmsg":                     "mvs_message",
    "db2-zos-master-log":                "mvs_message",
    "cics-dfh-message-log":              "cics_dfh",
    "cics-msgusr-transient-data":        "cics_dfh",
    "racf-ich408i-console":              "racf_ich408i",
    "racf-ich408i-violation":            "racf_ich408i",
    "racf-irradu00-smf-unload":          "racf_irradu00",
    "smf-irradu00-unload":               "racf_irradu00",
    "ibmi-joblog":                       "ibmi_joblog",
    "ibmi-qpjoblog-spool":               "ibmi_joblog",
    "ibmi-qhst-history-log":             "ibmi_qhst",
    "aix-errpt-summary":                 "aix_errpt",
    "aix-errpt-detail":                  "aix_errpt",
    "solaris-fmadm-faulty":              "solaris_fma",
    "solaris-fmdump-faultlog":           "solaris_fma",
    "solaris-smf-service-log":           "solaris_smf",
    # telephony / VoIP / healthcare
    "sip-raw-message":                   "sip_raw",
    "asterisk-ami-events":               "asterisk_ami",
    "asterisk-cdr-csv":                  "asterisk_cdr",
    "asterisk-queue-log":                "asterisk_queue",
    "asterisk-pjsip-logger-trace":       "asterisk_siptrace",
    "pjsip-library-log":                 "pjsip_lib",
    "hl7v2-er7-message":                 "hl7v2",
    "astm-e1394-lis2":                   "astm_lis2",
    "mirth-connect-server-log":          "mirth_connect",
    # enterprise backup
    "veeam-vbr-component-log":           "veeam_vbr",
    "commvault-cvd-log":                 "commvault_log",
    "netbackup-legacy-debug-log":        "netbackup_legacy",
    "spectrum-protect-client-sched-log": "spectrum_protect",
    "bacula-bareos-daemon-log":          "bacula_bareos",
    "pgbackrest-log":                    "pgbackrest",
    # HPC / EDA tool tables + rank-prefixed launchers
    "LSF job resource-usage summary":            "lsf_job",
    "LSF job resource-usage summary (bsub -o)":  "lsf_job",
    "GROMACS md.log":                    "gromacs_md",
    "LAMMPS log.lammps thermo output":   "lammps_thermo",
    "LAMMPS log.lammps thermo table":    "lammps_thermo",
    "NAMD ENERGY output":                "namd_log",
    "NAMD standard output / log":        "namd_log",
    "MPICH / Hydra rank-prefixed output": "mpi_rank",
    "srun --label task-prefixed output": "mpi_rank",
    # Windows server / crash / security text renderings
    "windows-dns-debug-log":             "windows_dns_debug",
    "windows-dhcp-audit-csv":            "windows_dhcp_csv",
    "powershell-transcript-file":        "powershell_transcript",
    "windows-application-error-1000":    "windows_app_crash",
    "sysmon_evtx_text":                  "sysmon_text",
    "windows_security_evtx_text":        "windows_security_text",
    "sysmon-dns-query-xml":              "windows_eventdata_xml",
    "sysmon-network-connect-xml":        "windows_eventdata_xml",
    "sysmon-processaccess-xml":          "windows_eventdata_xml",
    # firmware / embedded boot consoles
    "ARM Trusted Firmware-A (TF-A) log": "tfa_bootlog",
    "EDK2 / UEFI DEBUG serial log":      "edk2_uefi",
    "coreboot cbmem console":            "coreboot_cbmem",
    "esp32_rom_boot":                    "esp32_rom_boot",
    # security / directory / auth
    "krb5kdc-log":                       "krb5kdc",
    "389ds-access":                      "ds389_access",
    "libreswan-pluto-log":               "libreswan_pluto",
    # network / telco
    "frr-log":                           "frr",
    "huawei-vrp-syslog":                 "huawei_vrp",
    # virtualization management
    "ovirt-engine-log":                  "ovirt_engine",
    "ovirt-vdsm-v4-log":                 "vdsm_log",
    # mail
    "exim-mainlog":                      "exim_mainlog",
}

# ── 2. GAP FIXES: catalog sample now routes to an EXISTING named adapter ──────
GAP_FIXES = {
    "laravel_log":                  "php_monolog",   # multi-line envelope+trace
    "rails_request":                "rails",         # Started→Completed block
    "oracle-alert-log-text-legacy": "oracle_alert",  # "ORA-nnnnn msg" no colon
    "linux_devkmsg":                "linux_devkmsg",  # " KEY=VALUE" continuation
    "zephyr_fatal":                 "zephyr_fatal",  # whole fault-dump block
    "ftrace function_graph":        "ftrace",        # funcgraph gutter form
}

# ── 3. BONUS coverage: same grammar, extra catalog names (regression-pinned) ──
BONUS = {
    "exim-paniclog":                  "exim_mainlog",
    "krb5_kdc":                       "krb5kdc",
    "quagga-zebra-log":               "frr",
    "spectrum-protect-server-actlog": "spectrum_protect",
    "vtam-ist-messages":              "mvs_message",
    "mq-zos-csq-console":             "mvs_message",
    "ims-dfs-messages":               "mvs_message",
    "zos-commserver-ezz-messages":    "mvs_message",
    "topsecret-tss-audit":            "mvs_message",
    "sysmon-file-create-xml":         "windows_eventdata_xml",
    "sysmon-image-load-xml":          "windows_eventdata_xml",
    "HPC job epilog / resource summary": "lsf_job",
    "srun / mpirun per-rank prefixed lines": "mpi_rank",
    "linux_kmsg_raw":                 "linux_devkmsg",
    "ftrace trace (raw text)":        "ftrace",
    "symfony_monolog_channel":        "php_monolog",
}

# ── 4. NEGATIVE guards: batch 4 must NOT have stolen these ────────────────────
MUST_NOT_STEAL = {
    "uvicorn_access":  "jul_2line",   # "INFO:     …" is uvicorn, not TF-A
    "uvicorn_default": "jul_2line",
    "ROS 2 console log (rcutils)": "ros",       # "[INFO] […]" is ROS, not coreboot
    "coredns-log-plugin": "coredns",             # "[INFO] plugin/…" likewise
    "android-logcat-time": "logcat",
    "redis-server-log":   "redis",               # "1234:M …" is redis, not mpi
}

print("BATCH-4 self-routing — every adapter wins its OWN catalog sample:")
_ok_route = 0
for cat_name, want in {**NEW_ADAPTERS, **GAP_FIXES}.items():
    sample = catalog_sample(cat_name)
    if not sample:
        check(f"catalog[{cat_name}] present", False, "sample_line missing")
        continue
    winner, conf, scores = la.detect_adapter([sample])
    good = winner.name == want
    if good:
        _ok_route += 1
    check(f"{cat_name} → {want}", good,
          f"got {winner.name} ({conf}); fallback={winner.name in _FALLBACKS}")
_total_route = len(NEW_ADAPTERS) + len(GAP_FIXES)
print(f"  ({_ok_route}/{_total_route} samples route to their own adapter)")

print("\nBonus coverage — same grammar, extra catalog formats:")
for cat_name, want in BONUS.items():
    sample = catalog_sample(cat_name)
    if not sample:
        check(f"catalog[{cat_name}] present", False, "sample_line missing")
        continue
    winner, conf, _ = la.detect_adapter([sample])
    check(f"{cat_name} → {want}", winner.name == want,
          f"got {winner.name} ({conf})")

print("\nNegative guards — ownership that must NOT have moved:")
for cat_name, want in MUST_NOT_STEAL.items():
    sample = catalog_sample(cat_name)
    if not sample:
        continue                       # some names differ per catalog rev
    winner, conf, _ = la.detect_adapter([sample])
    check(f"{cat_name} still → {want}", winner.name == want,
          f"got {winner.name} ({conf})")


# ── 5. Parse-quality spot checks ──────────────────────────────────────────────
print("\nParse quality — failures land category=='error', schema is unified:")
_SCHEMA_KEYS = {"ts", "ts_ms", "category", "level", "source", "trace_id",
                "frame_seq", "data", "raw"}


def parse_with(adapter_name: str, sample: str):
    a = la.get_adapter(adapter_name)
    if a is None:
        return None
    ev = a.parse_line(sample)
    if ev is None and "\n" in sample:      # per-line format shipped as a block
        for sub in sample.splitlines():
            if sub.strip():
                ev = a.parse_line(sub)
                if ev is not None:
                    break
    return ev


for cat_name, want in {**NEW_ADAPTERS, **GAP_FIXES}.items():
    sample = catalog_sample(cat_name)
    if not sample:
        continue
    ev = parse_with(want, sample)
    check(f"parse[{cat_name}]", isinstance(ev, dict) and _SCHEMA_KEYS <= set(ev)
          and "message" in ev["data"] and "adapter" in ev["data"],
          f"event={type(ev).__name__}")

# targeted level/category/field semantics — catalog samples
_ev = parse_with("zos_syslog", catalog_sample("zos-syslog-hardcopy"))
check("zos_syslog Julian ts parsed + job trace",
      bool(_ev) and _ev["ts_ms"] is not None and _ev["trace_id"] == "STC04829")
_ev = parse_with("racf_ich408i", catalog_sample("racf-ich408i-violation"))
check("RACF violation → category error + user field",
      bool(_ev) and _ev["category"] == "error"
      and _ev["data"].get("user") == "JSMITH")
_ev = parse_with("ibmi_joblog", catalog_sample("ibmi-qpjoblog-spool"))
check("IBM i Escape message → category error",
      bool(_ev) and _ev["category"] == "error"
      and _ev["data"].get("message_type") == "Escape")
_ev = parse_with("aix_errpt", catalog_sample("aix-errpt-summary"))
check("AIX errpt PERM → category error + mmddhhmmyy ts",
      bool(_ev) and _ev["category"] == "error" and _ev["ts_ms"] is not None)
_ev = parse_with("solaris_fma", catalog_sample("solaris-fmdump-faultlog"))
check("FMA fault → warn + uuid trace_id",
      bool(_ev) and _ev["category"] == "warn"
      and (_ev["trace_id"] or "").count("-") == 4)
_ev = parse_with("sip_raw", catalog_sample("sip-raw-message"))
check("SIP INVITE → Call-ID trace_id + method field",
      bool(_ev) and _ev["data"].get("method") == "INVITE"
      and "atlanta.example.com" in (_ev["trace_id"] or ""))
_ev = parse_with("asterisk_cdr", catalog_sample("asterisk-cdr-csv"))
check("CDR ANSWERED → category log + uniqueid trace",
      bool(_ev) and _ev["category"] == "log"
      and _ev["trace_id"] == "1656419172.1")
_ev = parse_with("asterisk_queue", catalog_sample("asterisk-queue-log"))
check("queue CONNECT → epoch ts", bool(_ev) and _ev["ts_ms"] == 1656419091000.0)
_ev = parse_with("hl7v2", catalog_sample("hl7v2-er7-message"))
check("HL7 ADT^A01 → control-id trace + ts",
      bool(_ev) and _ev["trace_id"] == "01052901" and _ev["ts_ms"] is not None)
_ev = parse_with("veeam_vbr", catalog_sample("veeam-vbr-component-log"))
check("Veeam Info → category log + EU-date ts",
      bool(_ev) and _ev["category"] == "log" and _ev["ts_ms"] is not None)
_ev = parse_with("windows_dns_debug", catalog_sample("windows-dns-debug-log"))
check("Windows DNS qname decoded", bool(_ev)
      and _ev["data"].get("qname") == "example.com"
      and _ev["category"] == "log")
_ev = parse_with("windows_app_crash", catalog_sample("windows-application-error-1000"))
check("EventID-1000 → category error + exception code",
      bool(_ev) and _ev["category"] == "error"
      and _ev["data"].get("exception_code") == "0xc00001ad")
_ev = parse_with("sysmon_text", catalog_sample("sysmon_evtx_text"))
check("Sysmon text → UtcTime ts + ProcessGuid trace",
      bool(_ev) and _ev["ts_ms"] is not None and bool(_ev["trace_id"]))
_ev = parse_with("windows_security_text", catalog_sample("windows_security_evtx_text"))
check("Security 4625 text → category warn",
      bool(_ev) and _ev["category"] == "warn")
_ev = parse_with("windows_eventdata_xml", catalog_sample("sysmon-dns-query-xml"))
check("EventData XML → QueryName field",
      bool(_ev) and _ev["data"].get("QueryName") == "login.malicious.example")
_ev = parse_with("krb5kdc", catalog_sample("krb5kdc-log"))
check("krb5kdc ISSUE → category log + principal",
      bool(_ev) and _ev["category"] == "log"
      and _ev["data"].get("principal", "").endswith("@WEDGIE.ORG"))
_ev = parse_with("ovirt_engine", catalog_sample("ovirt-engine-log"))
check("oVirt correlation-id → trace_id",
      bool(_ev) and (_ev["trace_id"] or "").startswith("e3bc976c"))
_ev = parse_with("exim_mainlog", catalog_sample("exim-mainlog"))
check("Exim <= arrival → msgid trace_id",
      bool(_ev) and _ev["trace_id"] == "16ZCW1-0005MB-00"
      and _ev["data"].get("event") == "arrival")
_ev = parse_with("php_monolog", catalog_sample("laravel_log"))
check("Laravel block → ERROR + stack captured",
      bool(_ev) and _ev["category"] == "error"
      and _ev["data"].get("stack_frames", 0) >= 1)
_ev = parse_with("rails", catalog_sample("rails_request"))
check("Rails block → merged verb+status",
      bool(_ev) and _ev["data"].get("status") == 200
      and _ev["data"].get("verb") == "GET")
_ev = parse_with("linux_devkmsg", catalog_sample("linux_devkmsg"))
check("devkmsg block → SUBSYSTEM continuation field",
      bool(_ev) and _ev["data"].get("subsystem") == "usb")
_ev = parse_with("zephyr_fatal", catalog_sample("zephyr_fatal"))
check("Zephyr fault block → fatal + registers",
      bool(_ev) and _ev["category"] == "error" and _ev["level"] == "FATAL"
      and "registers" in _ev["data"])
_ev = parse_with("ftrace", catalog_sample("ftrace function_graph"))
check("funcgraph → duration_us field",
      bool(_ev) and _ev["data"].get("duration_us") == 1.381)
_ev = parse_with("oracle_alert", "ORA-01555 caused by SQL statement below")
check("legacy ORA line (no colon) → category error",
      bool(_ev) and _ev["category"] == "error"
      and _ev["data"].get("ora_code") == "ORA-01555")

# targeted semantics — inline REAL failure samples (level → category=error)
_ev = parse_with("spectrum_protect",
                 "07/21/2026 02:00:05 ANS1301E Server detected system error")
check("TSM ANS…E → category error", bool(_ev) and _ev["category"] == "error")
_ev = parse_with("bacula_bareos",
                 "21-Jul 02:05 backup-dir JobId 123: Fatal error: Network error with FD during Backup")
check("Bacula Fatal error → category error",
      bool(_ev) and _ev["category"] == "error" and _ev["level"] == "FATAL")
_ev = parse_with("pgbackrest",
                 "P00  ERROR: [082]: unable to find a valid repository")
check("pgBackRest ERROR → category error", bool(_ev) and _ev["category"] == "error")
_ev = parse_with("lsf_job", "Exited with exit code 137.")
check("LSF non-zero exit → category error",
      bool(_ev) and _ev["category"] == "error"
      and _ev["data"].get("exit_code") == 137)
_ev = parse_with("mvs_message",
                 "IEF450I PAYROLL STEP1 - ABEND=S0C4 U0000 REASON=00000010")
check("MVS ABEND → category error (fatal)",
      bool(_ev) and _ev["category"] == "error"
      and _ev["data"].get("abend_code") == "S0C4")
_ev = parse_with("cics_dfh", catalog_sample("cics-msgusr-transient-data"))
check("CICS ASRA abend → category error + abend code",
      bool(_ev) and _ev["category"] == "error"
      and _ev["data"].get("abend_code") == "ASRA")
_ev = parse_with("windows_dhcp_csv",
                 "14,07/21/26,02:00:00,Scope Full,10.0.0.0,,,")
check("DHCP scope-full → category warn", bool(_ev) and _ev["category"] == "warn")
_ev = parse_with("esp32_rom_boot",
                 "rst:0xc (RTC_SW_CPU_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)")
check("ESP32 SW reset banner parses", bool(_ev)
      and _ev["data"].get("reset_cause") == "RTC_SW_CPU_RESET")
_ev = parse_with("esp32_rom_boot",
                 "rst:0x10 (RTCWDT_RTC_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)")
check("ESP32 watchdog reset → category error",
      bool(_ev) and _ev["category"] == "error")
_ev = parse_with("krb5kdc",
                 "Jul 30 23:20:01 kdc krb5kdc[10544](info): AS_REQ (3 etypes {16 3 1}) "
                 "192.168.1.83(88): CLIENT_NOT_FOUND: nosuch@WEDGIE.ORG for krbtgt/WEDGIE.ORG@WEDGIE.ORG")
check("krb5kdc CLIENT_NOT_FOUND → category error",
      bool(_ev) and _ev["category"] == "error")
_ev = parse_with("mpi_rank", "[2] rank 2 reporting: setup complete")
check("mpi_rank → rank field", bool(_ev) and _ev["data"].get("rank") == 2)
_ev = parse_with("tfa_bootlog", "ERROR:   Error initializing runtime service opteed_fast")
check("TF-A ERROR line → category error", bool(_ev) and _ev["category"] == "error")
_ev = parse_with("coreboot_cbmem", "[ERROR]  Memory init failed")
check("coreboot [ERROR] → category error", bool(_ev) and _ev["category"] == "error")
_ev = parse_with("asterisk_cdr",
                 '"","1000","2565551212","default","x","PJSIP/1000-1","","Dial","x",'
                 '"2026-01-05 10:26:12","","2026-01-05 10:26:14",2,0,"FAILED",'
                 '"DOCUMENTATION","1656419199.9",""')
check("CDR FAILED → category error", bool(_ev) and _ev["category"] == "error")
_ev = parse_with("solaris_smf",
                 '[ May  5 09:32:20 Method "start" exited with status 96 ]')
check("SMF exit 96 → category error", bool(_ev) and _ev["category"] == "error"
      and _ev["data"].get("exit_status") == 96)

# ── 6. Registry invariants ────────────────────────────────────────────────────
print("\nRegistry invariants:")
names = [a.name for a in la.REGISTRY]
check("no duplicate adapter names", len(names) == len(set(names)),
      f"{len(names)} entries, {len(set(names))} unique")
check("tail is exactly [structural, raw]", names[-2:] == ["structural", "raw"],
      f"tail={names[-2:]}")
# batch 4 grew the registry to 217; later batches keep growing it — assert the
# floor (same convention as the batch-3 suite after batch 4 landed).
check("registry has at least 217 entries (batch-4 floor)", len(names) >= 217,
      f"size={len(names)}")

_batch4 = sorted(set(NEW_ADAPTERS.values()))
_missing = [n for n in _batch4 if la.get_adapter(n) is None]
check(f"all {len(_batch4)} batch-4 adapters registered", not _missing,
      f"missing={_missing}")

# ── 7. Collision sweep: no fallback wins any batch-4 sample; the whole catalog
#      never makes detection raise ─────────────────────────────────────────────
print("\nCollision / robustness sweep:")
_fallback_wins = []
for cat_name in {**NEW_ADAPTERS, **GAP_FIXES, **BONUS}:
    s = catalog_sample(cat_name)
    if not s:
        continue
    w, _, _ = la.detect_adapter([s])
    if w.name in _FALLBACKS:
        _fallback_wins.append(cat_name)
check("no fallback wins any batch-4 sample", not _fallback_wins,
      str(_fallback_wins))

_raised = 0
for e in _CATALOG.values():
    s = e.get("sample_line")
    if not isinstance(s, str) or not s:
        continue
    try:
        w, _, _ = la.detect_adapter([s])
        w.parse_line(s)
        la.parse_line(s)
    except Exception as exc:   # pragma: no cover
        _raised += 1
        print(f"    RAISE on catalog[{e['name']}]: {exc!r}")
check(f"full-catalog sweep ({len(_CATALOG)} formats): 0 raises", _raised == 0)

# ── 8. Hostile / empty input across the WHOLE registry (0 raises) ─────────────
print("\nHostile-input sweep (every adapter, every probe):")
_PROBES = [
    "", " ", "\n", "\r\n", "\t", "\x00\x01\x02", "{", "[", "]", "}", "====",
    "MSH|", "MSH|^~\\&|", "1H|", "1H|\\^&", "$HASP", "$HASP395", "N 4000000",
    "ICH408I USER(", "ICH408I USER", "ACCESS   SUCCESS", "CPF1124",
    "CPF1124   Information", "LABEL:", "LABEL: X", "Date/Time,Event",
    "[ May", "[ May  5 09:32:13 ]", "ENERGY:", "ENERGY: x", "ETITLE:",
    "Step", "   Step   Time", "0 1 2 3 4", "1:", "1: ", "[1]", "[1] ",
    "rst:", "rst:0x", "NOTICE:", "NOTICE:  ", "DEBUG [", "DEBUG [DXE]",
    "[INFO ]", "[INFO]", "<EventData>", "<EventData></EventData>",
    "Faulting application name:", "Event:", "Event: X\\r\\n\\r\\n",
    "Resource usage summary:", "CPU time :", "P00", "P00 INFO:",
    "\"\",\"a\"", '","' * 20, "|" * 30, "0|api", "1234:M", "13.03.14",
    "13.03.14 JOB1", "IEF403I", "IEF403I x", "DFHSI1517", "DSNL004I",
    "ANS1898I", "07/21/2026 02:00:01", "21-Jul 02:00", "[21.07.2026",
    "krb5kdc[1](x):", "pluto[1]:", "%%01", "Aug 16 2015 10:56:41 H %%01A/9/B(l):",
    "INVITE sip: SIP/2.0", "SIP/2.0 200", "<--- Received SIP",
    "2020/03/23 15:29:01 BGP:", "conn=1 op=2", "🚀🚀🚀", "ÿþ \x7f",
    "x" * 5000, "=" * 5000, "\\n\\n\\n", "\\r\\n" * 100, "9" * 5000,
    "[" + "9" * 4999, "Started GET", "Completed 200",
]
_h_raised = 0
for a in la.REGISTRY:
    for p in _PROBES:
        try:
            a.detect([p])
            a.detect([p, p])
            a.parse_line(p)
        except Exception as exc:   # pragma: no cover
            _h_raised += 1
            print(f"    RAISE {a.name} on {p[:30]!r}: {exc!r}")
check(f"{len(la.REGISTRY)} adapters × {len(_PROBES)} probes: 0 raises",
      _h_raised == 0)

_e_raised = 0
for a in la.REGISTRY:
    try:
        a.detect([])
    except Exception as exc:   # pragma: no cover
        _e_raised += 1
        print(f"    RAISE {a.name} on []: {exc!r}")
check("detect([]) never raises", _e_raised == 0)

# ── Result ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
if _fails:
    print(f"RESULT: {_fails} FAILURE(S)")
    sys.exit(1)
print("RESULT: all tests passed")
