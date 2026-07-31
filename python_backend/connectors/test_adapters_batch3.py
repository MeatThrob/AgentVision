"""
Tests for the BATCH-3 family adapters + the 6 batch-3 GAP FIXES.

Pure stdlib (no pytest). Run:
    python3 python_backend/connectors/test_adapters_batch3.py
Exits non-zero on any failure so it gates CI / packaging.

The hard rule (same as batch 2): for EVERY adapter added or fixed, its OWN
sample — pulled from docs/log_catalog_master.json — must resolve through
detect_adapter([sample]) to THAT adapter. A resolution to a fallback
(structural/generic_ts/raw) or to the wrong named adapter is a FAIL.

Plus: parse-quality spot checks (failure lines must land category=="error"),
a duplicate-name check, the pinned [structural, raw] tail, a hostile-input
sweep across the WHOLE registry (0 raises), and a full-catalog sweep that
asserts detection never raises on any of the 1430 catalog samples.
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


# ── 1. BATCH-3 adapters: catalog_name → expected adapter ─────────────────────
# Multi-line catalog blocks stay ONE element on purpose (that is exactly what
# detect_adapter gets handed for them).
NEW_ADAPTERS = {
    # devtools
    "python_traceback":                 "python_traceback",
    "node_stack_trace":                 "node_stack",
    "strace syscall line":              "strace",
    "Cargo human build output":         "cargo_build",
    "Vite build output":                "vite_build",
    "webpack stats output":             "webpack_stats",
    "npm debug log":                    "npm_debug",
    "Debug Adapter Protocol (DAP)":     "jsonrpc_wire",
    "LSP JSON-RPC wire stream":         "jsonrpc_wire",
    "helm-debug":                       "helm_debug",
    # observability
    "fluentbit-internal":               "fluentbit",
    "fluentd-internal":                 "fluentd",
    "telegraf-internal":                "telegraf",
    "vector-internal-tracing":          "rust_tracing",
    "qdrant-tracing-log":               "rust_tracing",
    "datadog-agent-log":                "datadog_agent",
    "fail2ban":                         "fail2ban",
    "airflow-task-log":                 "airflow_task",
    "structlog_console":                "structlog_console",
    "heroku-router-logfmt":             "heroku_router",
    # network
    "bind-named-querylog":              "bind_named",
    "unbound-log":                      "unbound",
    "coredns-log-plugin":               "coredns",
    "squid-access-native":              "squid_access",
    "squid-cache-log":                  "squid_cache",
    "openvpn-log":                      "openvpn",
    "cisco-iosxr-syslog":               "cisco_iosxr",
    "mikrotik-routeros-topics-log":     "mikrotik_routeros",
    "f5-bigip-ltm-syslog":              "f5_bigip_ltm",
    "citrix-netscaler-syslog-native":   "citrix_netscaler",
    "citrix-netscaler-appfw-native":    "citrix_netscaler",
    # os_platform
    "windows_evtx_text":                "windows_evtx_text",
    "windows-wer-report-file":          "windows_wer",
    "apt history.log":                  "apt_history",
    "dpkg.log":                         "dpkg_log",
    "dnf.log":                          "dnf_log",
    "pacman / ALPM log":                "pacman_alpm",
    "sssd-debug":                       "sssd_debug",
    # virt / private cloud
    "openstack-oslo-default":           "openstack_oslo",
    "openstack-oslo-context":           "openstack_oslo",
    "openstack-oslo-exception-trace":   "openstack_oslo",
    "esxi-vmkernel":                    "esxi_vmkernel",
    "vmware-vmacore-originator":        "vmware_vmacore",
    "proxmox-task-upid":                "proxmox_task",
    # bigdata / HPC
    "spark-log4j":                      "spark_log4j",
    "hdfs-classic-log4j":               "hdfs_log4j",
    "tidb-unified-log":                 "pingcap_unified",
    "milvus-unified-text-log":          "pingcap_unified",
    "trino-airlift":                    "trino_airlift",
    "SLURM slurmctld log":              "slurm",
    "SLURM slurmd log":                 "slurm",
    "SLURM job stdout (sbatch %j.out)": "slurm",
    "PBS Pro / TORQUE server log":      "pbs_torque",
    "PBS Pro / TORQUE server log (pbs_server)": "pbs_torque",
    "PBS/Torque server_log & mom_log":  "pbs_torque",
    "Open MPI --tag-output":            "openmpi_tag",
    "OpenMPI --tag-output":             "openmpi_tag",
    # app servers (webserver.py additions)
    "websphere-systemout-basic":        "websphere",
    "liberty-messages-log":             "websphere",
    "glassfish-ulf":                    "glassfish_ulf",
    "jetty-stderrlog":                  "jetty",
    # databases (database.py additions)
    "sap-hana-trace":                   "sap_hana",
    "arangodb-server-log":              "arangodb",
    # telephony (messaging.py additions)
    "asterisk-full-log":                "asterisk",
    "freeswitch-logfile":               "freeswitch",
}

# ── 2. GAP FIXES: catalog sample now routes to an EXISTING named adapter ──────
GAP_FIXES = {
    "consul-hclog-text":     "hashicorp",   # ±HHMM offset accepted
    "terraform-hclog-trace": "hashicorp",
    "pulsar-broker-log4j2":  "log4j",       # log4j2 ISO8601_OFFSET ts
    "pihole-dnsmasq-log":    "dnsmasq",     # Pi-hole gravity/blacklist verbs
    "Valgrind Memcheck report": "sanitizer",  # multi-line report block
    "android-logcat-time":   "logcat",      # -v time form
    "android-logcat-tag":    "logcat",      # pidless Studio form
    "ansible-logfile":       "ansible",     # log_path "ts p= u= n= | body" form
}

print("BATCH-3 self-routing — every adapter wins its OWN catalog sample:")
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


# ── 3. Parse-quality spot checks ──────────────────────────────────────────────
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

# targeted level/category semantics
_ev = parse_with("python_traceback", catalog_sample("python_traceback"))
check("python_traceback block → category error",
      bool(_ev) and _ev["category"] == "error"
      and _ev["data"].get("exception") == "ValueError")
_ev = parse_with("node_stack", catalog_sample("node_stack_trace"))
check("node_stack block → category error", bool(_ev) and _ev["category"] == "error")
_ev = parse_with("windows_wer", catalog_sample("windows-wer-report-file"))
check("WER APPCRASH → category error", bool(_ev) and _ev["category"] == "error"
      and _ev["data"].get("AppName") == "WinSCP")
_ev = parse_with("slurm", catalog_sample("SLURM job stdout (sbatch %j.out)"))
check("slurm CANCELLED → category error", bool(_ev) and _ev["category"] == "error")
_ev = parse_with("f5_bigip_ltm", catalog_sample("f5-bigip-ltm-syslog"))
check("f5 err → category error", bool(_ev) and _ev["category"] == "error")
_ev = parse_with("squid_access", catalog_sample("squid-access-native"))
check("squid 200 → category log", bool(_ev) and _ev["category"] == "log"
      and _ev["data"].get("status") == 200)
_ev = parse_with("heroku_router", catalog_sample("heroku-router-logfmt"))
check("heroku 301 → category log + request_id trace",
      bool(_ev) and _ev["category"] == "log" and bool(_ev["trace_id"]))
_ev = parse_with("proxmox_task", catalog_sample("proxmox-task-upid"))
check("proxmox OK → category log", bool(_ev) and _ev["category"] == "log"
      and _ev["data"].get("status") == "OK")
_ev = parse_with("strace", catalog_sample("strace syscall line"))
check("strace ok-return → category debug", bool(_ev) and _ev["category"] == "debug"
      and _ev["data"].get("syscall") == "openat")
_ev = parse_with("openstack_oslo", catalog_sample("openstack-oslo-default"))
check("oslo req-id → trace_id", bool(_ev) and (_ev["trace_id"] or "").startswith("req-"))
_ev = parse_with("logcat", catalog_sample("android-logcat-time"))
check("logcat -v time → pid field", bool(_ev) and _ev["data"].get("pid") == 585)
_ev = parse_with("sanitizer", catalog_sample("Valgrind Memcheck report"))
check("valgrind block → category error", bool(_ev) and _ev["category"] == "error")
_ev = parse_with("ansible", catalog_sample("ansible-logfile"))
check("ansible logfile → parsed ts", bool(_ev) and _ev["ts_ms"] is not None)

# ── 4. Registry invariants ────────────────────────────────────────────────────
print("\nRegistry invariants:")
names = [a.name for a in la.REGISTRY]
check("no duplicate adapter names", len(names) == len(set(names)),
      f"{len(names)} entries, {len(set(names))} unique")
check("tail is exactly [structural, raw]", names[-2:] == ["structural", "raw"],
      f"tail={names[-2:]}")
# batch 3 grew the registry to 167; later batches keep growing it — assert the
# floor here, the exact current size is asserted by the NEWEST batch suite.
check("registry has at least 167 entries (batch-3 floor)", len(names) >= 167,
      f"size={len(names)}")

# every batch-3 adapter must actually be registered
_batch3 = sorted(set(NEW_ADAPTERS.values()))
_missing = [n for n in _batch3 if la.get_adapter(n) is None]
check(f"all {len(_batch3)} batch-3 adapters registered", not _missing,
      f"missing={_missing}")

# ── 5. Collision sweep: no fallback wins any batch-3 sample; the whole catalog
#      never makes detection raise ─────────────────────────────────────────────
print("\nCollision / robustness sweep:")
_fallback_wins = []
for cat_name in {**NEW_ADAPTERS, **GAP_FIXES}:
    s = catalog_sample(cat_name)
    if not s:
        continue
    w, _, _ = la.detect_adapter([s])
    if w.name in _FALLBACKS:
        _fallback_wins.append(cat_name)
check("no fallback wins any batch-3 sample", not _fallback_wins,
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

# ── 6. Hostile / empty input across the WHOLE registry (0 raises) ─────────────
print("\nHostile-input sweep (every adapter, every probe):")
_PROBES = [
    "", " ", "\n", "\r\n", "\t", "\x00\x01\x02", "{", "[", "]", "}", "====",
    "==x==", "== 12 ==", "Content-Length:", "Content-Length: abc", "Traceback",
    "Traceback (most recent call last):", "  File \"", "at ", "    at ",
    "UPID:", "UPID:x", "[1,", "[1,2]<", "Event[", "Event[0]:", "%X-9-Y:",
    "I/O error: retrying", "a=b", "at=info", "Start-Date:", "EventType=",
    "1234567890.123", "[0000000000]", "slurmstepd:", "vite v", "asset ",
    "0 verbose", "install.go:", "(Tue Nov 20 12:18:56 2020)", "<local0.info>",
    "🚀🚀🚀", "ÿþ binary-ish \x7f", "x" * 5000, "=" * 5000, "\\r\\n\\r\\n",
    "openat(", "foo() = ", "[#|", "[#||||||#]", "RP/0/RSP0/CPU0:",
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

# detect() must also survive an empty sample list everywhere
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
