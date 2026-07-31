"""
Tests for the BATCH-9 low-tier straggler adapters.

Pure stdlib (no pytest). Run:
    python3 python_backend/connectors/test_adapters_batch9.py
Exits non-zero on any failure so it gates CI / packaging.

The hard rule (same as batches 2-8): for EVERY adapter added, its OWN sample —
pulled from docs/log_catalog_master.json — must resolve through
detect_adapter([sample]) to THAT adapter (multi-line samples fed WHOLE, as one
element). A resolution to a fallback (structural/generic_ts/raw) or to the
wrong named adapter is a FAIL.

Batch 9 finishes the LOW non-binary tail: low fallthrough 198 -> 60 (whole-block
feeding; the remainder being template/doc-only catalog samples that carry no
real log line, binary/protobuf formats, or JSON/other formats already covered by
an existing adapter). 133 new named adapters (516 -> 649). Tie-break is by
REGISTRY order and batch9 loads LAST, so it can only ever GAIN a sample that
previously fell to structural/generic_ts — verified below with a 0-steal /
0-regression sweep over all catalog entries.
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


def _slines(sample: str):
    return sample.split("\n") if "\n" in sample else [sample]


# ── catalog_name → expected adapter (128 entries → 125 distinct adapters) ─────
NEW_ADAPTERS = {
    # scientific / HPC
    "Gaussian log SCF": "gaussian_scf",
    "VASP OSZICAR": "vasp_oszicar",
    "CP2K output": "cp2k",
    "OpenMC structured run log": "openmc",
    # debuggers / profilers
    "GDB internal logging": "gdb_internal",
    "LLDB log channels": "lldb_log",
    "perf annotate": "perf_annotate",
    "ltrace/strace -f multiprocess prefix": "strace_pid_prefix",
    "strace/ltrace -f multiprocess prefix": "strace_pid_prefix",
    # build / dev
    "Homebrew verbose output": "homebrew",
    "Poetry verbose output": "poetry",
    "Rollup output": "rollup",
    "test-kitchen": "test_kitchen",
    "vagrant-log": "vagrant",
    "capistrano-airbrussh": "capistrano",
    "buildkite-job-log": "buildkite_job",
    "travis-log-markers": "travis_markers",
    "packer-machine-readable": "packer_machine",
    # datastores
    "questdb-log": "questdb",
    "orientdb-log": "orientdb",
    # networking / proxy / vpn
    "accel-ppp-log": "accel_ppp",
    "cacti-poller-log": "cacti_poller",
    "checkmk-cmc-log": "checkmk_cmc",
    "checkpoint-lea-opsec": "checkpoint_lea",
    "cisco_meraki_flow": "cisco_meraki_flow",
    "dnsdist-verbose-log": "dnsdist_verbose",
    "exim-msglog-spool": "exim_msglog",
    "frp-log": "frp",
    "graphite-carbon-log": "graphite_carbon",
    "heimdal-kdc-log": "heimdal_kdc",
    "juju-debug-log": "juju_debug",
    "kadmind-log": "kadmind",
    "knot-dns-log": "knot_dns",
    "knot-resolver-log": "knot_resolver",
    "munin-log": "munin",
    "msmtp-log": "msmtp",
    "netbird-client-log": "netbird",
    "opendnp3-console-log": "opendnp3",
    "pritunl-log": "pritunl",
    "racoon-ipsec-tools-log": "racoon",
    "socat-log": "socat",
    "tacacs_accounting": "tacacs_accounting",
    "v2ray-xray-log": "v2ray",
    "xl2tpd-log": "xl2tpd",
    "dynatrace-oneagent-log": "dynatrace_oneagent",
    # vpn / security
    "openconnect-client-log": "openconnect",
    "openvpn-access-server-log": "openvpn_as",
    "wg-quick-output": "wg_quick",
    "wireguard-windows-ringlogger": "wireguard_windows",
    "clamav": "clamav",
    "aide_file_integrity": "file_integrity",
    "tripwire_aide": "file_integrity",
    "simplesamlphp-log": "simplesamlphp",
    "pingfederate-audit": "pingfederate_audit",
    "cas-audit": "cas_audit",
    "sophos-sav-on-access-log": "sophos_sav",
    "mcafee-endpoint-security-ens-log": "mcafee_ens",
    "vcsa-applmgmt-audit": "vcsa_applmgmt_audit",
    "globalprotect-pangps-client": "globalprotect_pangps",
    # cloud / aws
    "cfn-init-log": "cfn_init",
    "aws-global-accelerator-flow-log": "aws_global_accelerator_flow",
    "aws-transit-gateway-flow-log": "aws_transit_gateway_flow",
    "route53-public-query-log": "route53_query",
    "openwhisk-activation-log": "openwhisk_activation",
    "aws-rds-mariadb-audit-plugin": "rds_mariadb_audit",
    # backup
    "backuppc-log": "backuppc",
    "restic-text": "restic_text",
    "rsnapshot-log": "rsnapshot",
    "arcserve-udp-activity-log": "arcserve_udp",
    "veritas-backup-exec-job-log": "veritas_bex",
    # os platform
    "hpux-rc-log": "hpux_rc",
    "hpux-shutdownlog": "hpux_shutdownlog",
    "macos-fsck-apfs-log": "macos_fsck",
    "macos-wifi-log": "macos_wifi",
    "macos-coresimulator-log": "macos_coresimulator",
    "macos-mdm-managedclient-log": "macos_mdm",
    "macos-install-history-plist": "macos_install_history",
    "opendirectoryd-log": "opendirectoryd",
    "bsd-acct-lastcomm": "lastcomm",
    # firmware / rtos
    "SeaBIOS debug log": "seabios",
    "raspberrypi_vc_firmware": "rpi_vc_firmware",
    "mbed_os_error": "mbed_os_error",
    "contiki_ng_log": "contiki_ng",
    "nordic_nrf_log": "nordic_nrf",
    "threadx_azrtos": "threadx",
    "ti_rtos_log": "ti_rtos",
    "switch_diag_crash": "switch_diag_crash",
    "UEFI Secure Boot / shim / mokutil": "uefi_secureboot",
    "qnx_slog2": "qnx_slog2",
    # industrial / OT
    "ge-ifix-alarm-history": "ge_ifix_alarm",
    "veeder-root-atg-inventory": "veeder_root_atg",
    "lib60870-iec104-debug": "lib60870",
    "opc-ua-net-stack-trace": "opc_ua_trace",
    "openplc-runtime-log": "openplc_runtime",
    # telephony / voip
    "jitsi-jvb-log": "jitsi_jvb",
    "yate-log": "yate",
    "yealink-phone-syslog": "yealink_syslog",
    "oracle-acme-sbc-sipmsg": "oracle_sbc_sipmsg",
    "pulsar-function-instance-log": "pulsar_function",
    "mailman-post-smtp-log": "mailman",
    # middleware / java / vmware
    "octopus-server-log": "octopus_server",
    "octopus-task-log": "octopus_task",
    "ovirt-jboss-boot-log": "ovirt_jboss_boot",
    "intersystems-iris-main-log": "iris_main",
    "intersystems-iris-messages-log": "iris_messages",
    "vmware-loginsight-ls-log": "loginsight_ls",
    "vcenter-vpxd-legacy-5x": "vcenter_vpxd_legacy",
    "vmware-esxi-section-header": "vmware_section",
    "qpid-dispatch-router-log": "qpid_dispatch",
    "habitat-sup-log": "habitat_sup",
    # storage / mail
    "dell-unity-syslog": "dell_unity",
    "solaris_fmadump": "solaris_fmadump",
    "hmailserver-log": "hmailserver",
    "procmail-logfile": "procmail",
    "oracle-messaging-mail-log": "oracle_messaging_mail",
    # monitoring / metrics / ids
    "sflowtool-keyvalue": "sflowtool_kv",
    "suricata-stats-log": "suricata_stats",
    "opnsense-suricata-alert-log": "suricata_stats",
    "varnishstat-text": "varnishstat",
    "snort-full-alert": "snort_full",
    "oracle-adrci-alert-output": "oracle_adrci",
    "infoblox-nios-dns-syslog": "infoblox_dns",
    # legacy unix / mainframe
    "acf2-acfrpt-report": "acf2_report",
    "zvse-console": "zvse_console",
    "tru64-evm-evmshow": "tru64_evm",
    "tru64-uerf-report": "tru64_uerf",
    "openldap-auditlog-overlay": "openldap_auditlog",
    "watchguard-firebox-syslog": "watchguard_firebox",
    # second wave — more well-anchored low-tier formats
    "SimGrid / DES simulation log (XBT)": "simgrid",
    "samba-winbind-debug": "samba_winbind",
    "dicom-dcmdump-dataset": "dicom_dcmdump",
    "hl7v2-batch-fhs-bhs": "hl7v2_batch",
    "activemq-classic-audit": "activemq_audit",
    "px4_mavlink_statustext": "px4_mavlink",
    "acronis-cyber-protect-log": "acronis",
    "azure-mars-cbengine-log": "azure_mars",
    "tacacs-plus-accounting": "tacacs_accounting",
}

_B9_NAMES = set(NEW_ADAPTERS.values())

_SCHEMA_KEYS = {"ts", "ts_ms", "category", "level", "source", "trace_id",
                "frame_seq", "data", "raw"}


print("1. every batch-9 adapter self-routes on its OWN catalog sample")
for cat_name, expect in NEW_ADAPTERS.items():
    sample = catalog_sample(cat_name)
    if not sample:
        check(f"{cat_name} → {expect}", False, "no catalog sample")
        continue
    winner, conf, _ = la.detect_adapter([sample])          # whole sample, one element
    check(f"{cat_name} → {expect}", winner.name == expect,
          f"got {winner.name} (conf {conf})")


print("\n2. every batch-9 adapter emits a schema-valid event on its sample")
for cat_name, expect in NEW_ADAPTERS.items():
    sample = catalog_sample(cat_name)
    ad = la.get_adapter(expect)
    if not ad or not sample:
        continue
    ev = ad.parse_line(sample)
    if ev is None:
        ev = ad.parse_line(_slines(sample)[0])
    ok = isinstance(ev, dict) and _SCHEMA_KEYS.issubset(ev.keys()) \
        and isinstance(ev.get("data"), dict) and "message" in ev["data"] \
        and "adapter" in ev["data"]
    check(f"{expect} event schema", ok, f"{ev!r}"[:140] if not ok else "")


print("\n3. all expected batch-9 adapter names are registered")
_registered = {a.name for a in la.REGISTRY}
for nm in sorted(_B9_NAMES):
    check(f"registered: {nm}", nm in _registered)


print("\n4. registry integrity")
names = [a.name for a in la.REGISTRY]
check("no duplicate names", len(names) == len(set(names)),
      f"{[n for n in names if names.count(n) > 1]}")
check("registry ≥ 651 entries", len(names) >= 651, f"got {len(names)}")
check("tail is [structural, raw]", names[-2:] == ["structural", "raw"],
      f"got {names[-2:]}")
check("133 distinct batch-9 adapters", len(_B9_NAMES) == 133, f"got {len(_B9_NAMES)}")


print("\n5. collision sweep — no fallback wins a batch-9 sample")
for cat_name, expect in NEW_ADAPTERS.items():
    sample = catalog_sample(cat_name)
    if not sample:
        continue
    winner, _, _ = la.detect_adapter([sample])
    check(f"{cat_name}: not a fallback", winner.name not in _FALLBACKS,
          f"fell to {winner.name}")


print("\n6. 0 steals / 0 regressions vs a batch-9-free baseline (all catalog)")
# Rebuild the registry WITHOUT the batch-9 adapters and diff every catalog
# sample's owner. batch9 must never take a sample a prior NAMED adapter owned
# (steal) and never push a prior NAMED sample to a fallback (regression).
_saved = list(la.REGISTRY)
la.REGISTRY = [a for a in la.REGISTRY if a.name not in _B9_NAMES]
la._BY_NAME = {a.name: a for a in la.REGISTRY}
_before = {}
for _n, _e in _CATALOG.items():
    _s = _e.get("sample_line") or ""
    _before[_n] = la.detect_adapter([_s])[0].name if _s.strip() else None
la.REGISTRY = _saved
la._BY_NAME = {a.name: a for a in la.REGISTRY}

_steals = _regr = 0
for _n, _e in _CATALOG.items():
    _s = _e.get("sample_line") or ""
    if not _s.strip():
        continue
    _b = _before[_n]
    _a = la.detect_adapter([_s])[0].name
    if _b == _a:
        continue
    if _b not in _FALLBACKS and _a in _B9_NAMES:
        _steals += 1
        print(f"    STEAL {_n!r}: {_b} -> {_a}")
    elif _b not in _FALLBACKS and _a in _FALLBACKS:
        _regr += 1
        print(f"    REGRESSION {_n!r}: {_b} -> {_a}")
    elif _b not in _FALLBACKS and _a not in _FALLBACKS:
        _regr += 1
        print(f"    OWNER-CHANGE {_n!r}: {_b} -> {_a}")
check("0 steals of prior-named samples", _steals == 0, f"{_steals}")
check("0 regressions of prior-named samples", _regr == 0, f"{_regr}")


print("\n7. hostile sweep — whole registry, 0 raises")
_HOSTILE = [
    "", "   ", "\t\t\n", "\x00\x01\x02\xff\xfe binary \x7f\x80",
    "\xed\xa0\x80 unicode ‮ RTL \U0001F600 emoji  ",
    "A" * 5000, ("k=v " * 800)[:5000],
    "line1\\nline2\\nline3", "real\nnewlines\nhere",
    '{"truncated": "jso', "﻿BOM line", "\x1b[31mANSI\x1b[0m ERROR x",
    "[remote] x", "ms(1) INFO a - b", "==> x", "🍺 done",
    "[pid 1] read() = 0", "12:00:00 :: x", "racoon: INFO: x",
    "[#] ip", "svcBreak", "----- EVENT INFORMATION -----",
    "# modify 1 dc=x cn=y conn=0", "MAIN.x 1 . y",
    "travis_fold:start:x", "0.0.0: x", "000000.001: x",
    "[**] [1:2:3] x [**]", "<info> app: hi", "[t=0x1] a.b: c",
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
check("0 raises across registry × hostile inputs", _raises == 0, f"{_raises} raises")


print("\n8. full catalog sweep — 0 raises across all entries")
_bad = 0
for _name, _e in _CATALOG.items():
    s = _e.get("sample_line") or ""
    try:
        ad, conf, _ = la.detect_adapter([s])
        ad.parse_line(s)
        la.parse_line(s)
        for _ln in _slines(s):
            la.parse_line(_ln)
    except Exception as exc:       # pragma: no cover
        _bad += 1
        print(f"    RAISE on catalog {_name!r}: {type(exc).__name__}: {exc}")
check(f"0 raises across {len(_CATALOG)} catalog samples", _bad == 0, f"{_bad} raises")


print(f"\n{'=' * 70}")
if _fails:
    print(f"BATCH-9 SUITE: {_fails} FAILURE(S)")
    sys.exit(1)
print("BATCH-9 SUITE: all checks passed")
sys.exit(0)
