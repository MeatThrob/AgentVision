"""
Tests for the BATCH-5 family adapters + the batch-5 GAP FIXES.

Pure stdlib (no pytest). Run:
    python3 python_backend/connectors/test_adapters_batch5.py
Exits non-zero on any failure so it gates CI / packaging.

The hard rule (same as batches 2-4): for EVERY adapter added or fixed, its OWN
sample — pulled from docs/log_catalog_master.json (or an inline REAL sample
where the catalog entry is a template) — must resolve through
detect_adapter([sample]) to THAT adapter. A resolution to a fallback
(structural/generic_ts/raw) or to the wrong named adapter is a FAIL.

Plus: NEGATIVE ownership guards (formats batch 5 must NOT steal), parse-quality
spot checks, registry invariants (266 entries, pinned tail), a hostile-input
sweep across the WHOLE registry (0 raises), and the full 1430-catalog sweep.

Batch 5 closed the high-priority TEXT/mixed/csv fallthrough: 71 → 18, and all
18 remainders are either BINARY (source-reader work, not line adapters), the
two TEMPLATE-sample catalog entries (their real formats route — verified inline
here), or the literal-\\n NDJSON sample (real NDJSON already routes to jsonl
line-by-line).
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


# ── 1. BATCH-5 adapters: catalog_name → expected adapter ─────────────────────
NEW_ADAPTERS = {
    # databases
    "oracle-listener-log":              "oracle_listener",
    "oracle-audit-aud":                 "oracle_audit",
    "oracle-audit-trail-aud":           "oracle_audit",
    "sybase-ase-errorlog":              "sybase_ase",
    "informix-online-log":              "informix",
    "firebird-server-log":              "firebird",
    "neo4j-plain-log":                  "neo4j",
    "neo4j-log4j-log":                  "neo4j",
    "neo4j-debug-log-categories":       "neo4j",
    "neo4j-query-log":                  "neo4j_query",
    # virt / private cloud / storage-cloud
    "esxi-vobd":                        "esxi_vobd",
    "nutanix-genesis-python":           "nutanix_genesis",
    "ovs-vlog-file":                    "ovs_vlog",
    "openstack-eventlet-wsgi-access":   "eventlet_wsgi",
    "proxmox-pveproxy-access":          "pveproxy",
    # public cloud
    "aws-elb-classic-access-log":       "aws_elb_classic",
    "aws-vpc-flow-log-default":         "aws_vpc_flow",
    "aws-vpc-flow-log-custom":          "aws_vpc_flow",
    "aws_vpc_flow":                     "aws_vpc_flow",
    "azure-functions-host":             "azure_functions",
    # config-management / CI / package tools
    "chef-client-log":                  "chef_client",
    "puppet-agent-syslog":              "puppet_agent",
    "puppet-apply-notice":              "puppet_apply",
    "terraform-plan-human":             "terraform_plan",
    "pip verbose install log":          "pip_install",
    "pm2_prefixed":                     "pm2",
    # profiling / debug wire
    "FlameGraph folded (generic)":      "flamegraph_folded",
    "async-profiler collapsed stacks":  "flamegraph_folded",
    "austin sample format":             "flamegraph_folded",
    "py-spy record raw / folded":       "flamegraph_folded",
    "perf script (stack samples)":      "perf_script",
    "bpftrace histogram (hist/lhist)":  "bpftrace",
    "bpftrace stack aggregation":       "bpftrace",
    "GDB/MI machine interface":         "gdb_mi",
    # seeds / determinism dumps
    "Deterministic replay seed dump (game)":  "replay_seed",
    "Deterministic-replay seed/input dump":   "replay_seed",
    "PyTorch Lightning seed_everything":      "lightning_seed",
    "PyTorch Lightning / seed_everything":    "lightning_seed",
    # crash dumps
    "android-tombstone":                "android_tombstone",
    "android_tombstone":                "android_tombstone",
    "android-bugreport":                "android_bugreport",
    "apple-ips-crash-report":           "apple_ips",
    # web frameworks
    "phoenix_request":                  "phoenix",
    # security / network
    "modsecurity_serial":               "modsecurity",
    "modsecurity-audit-log":            "modsecurity",
    "f5-bigip-asm-kv-request":          "f5_asm",
    "f5_asm":                           "f5_asm",
    "pflog-tcpdump-text":               "pflog_text",
    "isc-dhcpd-leases-file":            "dhcpd_leases",
    "net-snmp-snmptrapd-log":           "snmptrapd",
    "tailscaled-log":                   "tailscaled",
    "netapp-ontap-event-log-show":      "netapp_ems",
    "wireguard-kernel-dmesg":           "wireguard",
    # messaging / mail / midrange
    "ibm-mq-amqerr-text":               "ibm_mq",
    "exchange-message-tracking-csv":    "exchange_tracking",
    "ibmi-qsysopr-message-queue":       "ibmi_qsysopr",
    "ibmi-qaudjrn-security-audit":      "ibmi_qaudjrn",
    # industrial / SCADA
    "ignition-wrapper-log":             "ignition_wrapper",
    "osisoft-pi-message-log":           "osisoft_pi",
    "wonderware-archestra-aalog":       "wonderware",
    "TPM2 measured-boot event log":     "tpm2_eventlog",
    "conpot-ics-honeypot-log":          "conpot",
}

# gap fixes routing catalog formats to EXISTING adapters:
#   • w3c_access — accepts literal-\t CloudFront data rows (escaped shipping)
#   • android_anr — literal-\n + escaped-quote ANR blocks (split_any/block_ratio)
GAP_FIXES = {
    "cloudfront-standard-w3c":          "w3c_access",
    "android-anr-traces":               "android_anr",
}

# TEMPLATE catalog entries (sample_line is a format description, not a real
# line) → verified with an inline REAL sample instead.
INLINE_SAMPLES = {
    "swift_proxy": ("swift-proxy-access",
        "1.2.3.4 4.5.6.7 26/Apr/2026/17/46/40 GET /v1/AUTH_test/container/obj "
        "HTTP/1.0 200 - curl/7.24.0 - - - 12 - "
        "tx123456789abcdef123456-0123456789 - 0.0200 - - "
        "1406795669.64 1406795669.68 0 -"),
    "uwsgi": ("uwsgi-request-log",
        "[pid: 3423|app: 0|req: 5/6] 192.168.1.10 () {40 vars in 810 bytes} "
        "[Mon Jul 21 09:00:00 2026] GET /api/items => generated 1234 bytes in "
        "5 msecs (HTTP/1.1 200) 4 headers in 120 bytes (1 switches on core 0)"),
}

print("\n── 1. batch-5 adapters route their own catalog samples ──")
for cat_name, adapter_name in sorted({**NEW_ADAPTERS, **GAP_FIXES}.items()):
    sample = catalog_sample(cat_name)
    if not sample:
        check(f"{cat_name} → {adapter_name}", False, "catalog sample missing")
        continue
    got, conf, _ = la.detect_adapter([sample])
    check(f"{cat_name} → {adapter_name}", got.name == adapter_name,
          f"got {got.name} ({conf:.2f})")

print("\n── 1b. TEMPLATE catalog entries route via inline REAL samples ──")
for adapter_name, (cat_name, sample) in sorted(INLINE_SAMPLES.items()):
    got, conf, _ = la.detect_adapter([sample])
    check(f"{cat_name} (inline) → {adapter_name}", got.name == adapter_name,
          f"got {got.name} ({conf:.2f})")

# ── 2. NEGATIVE ownership guards — formats batch 5 must NOT steal ─────────────
print("\n── 2. negative ownership guards ──")
NEGATIVE = [
    # (description, sample, expected owner)
    ("elixir line w/o metadata stays with elixir",
     "12:00:00.123 [info] plain elixir line", "elixir"),
    ("bare wall-clock + ONE space stays off informix",
     "11:46:35 something happened", None),          # any owner but informix
    ("aws ALB line stays with aws_alb",
     'https 2018-07-02T22:23:00.186641Z app/my-lb/50dc6c495c0c9188 '
     '192.168.131.39:2817 10.0.0.1:80 0.086 0.048 0.037 200 200 0 57 '
     '"GET https://www.example.com HTTP/1.1"', "aws_alb"),
    ("plain Go log without tailscale component stays off tailscaled",
     "2023/01/15 12:00:00 starting server on :8080", None),
    ("CLF access log stays with access_log",
     '1.2.3.4 - - [20/Jul/2026:12:00:00 +0000] "GET /x HTTP/1.1" 200 1234',
     "access_log"),
    ("logfmt stays with logfmt",
     'level=info ts=2026-07-20T12:00:00Z msg="thing" seed=42', "logfmt"),
    ("wonderware-free k=v CSV stays off f5_asm",
     'hostname="a",client_ip="1.2.3.4"', None),
    ("java thread dump w/o ANR header stays off android_anr",
     '"main" prio=5 tid=1 RUNNABLE', None),
    ("pg log stays with database",
     "2026-07-20 12:00:00.123 UTC [1234] LOG:  statement: SELECT 1",
     "database"),
    ("dnsmasq DHCPACK stays with dnsmasq",
     "Jul 20 12:00:00 dnsmasq-dhcp[123]: DHCPACK(eth0) 192.168.1.10 "
     "aa:bb:cc:dd:ee:ff host", "dnsmasq"),
    ("nlog pipe layout stays with nlog",
     "2026-07-21 10:00:00.1234|INFO|My.Class|message", "nlog"),
]
for desc, sample, owner in NEGATIVE:
    got, conf, _ = la.detect_adapter([sample])
    if owner is None:
        # must not be claimed by ANY batch-5 adapter
        b5 = set(NEW_ADAPTERS.values())
        check(desc, got.name not in b5, f"stolen by {got.name} ({conf:.2f})")
    else:
        check(desc, got.name == owner, f"got {got.name} ({conf:.2f})")

# ── 3. Parse-quality spot checks ──────────────────────────────────────────────
print("\n── 3. parse-quality spot checks ──")


def parse_with(adapter_name, sample):
    a = la.get_adapter(adapter_name)
    return a.parse_line(sample) if a else None


ev = parse_with("oracle_listener", catalog_sample("oracle-listener-log"))
check("oracle_listener: establish rc=0 → info + ts",
      bool(ev) and ev["level"] == "INFO" and ev["ts_ms"] is not None
      and ev["data"].get("listener_event") == "establish", str(ev)[:160])

ev = parse_with("oracle_audit", catalog_sample("oracle-audit-aud"))
check("oracle_audit: block → one record w/ ACTION + DATABASE USER fields",
      bool(ev) and "ACTION" in ev["data"] and "DATABASE USER" in ev["data"]
      and ev["ts_ms"] is not None, str(ev)[:160])

ev = parse_with("sybase_ase", catalog_sample("sybase-ase-errorlog"))
check("sybase_ase: spid + facility parsed",
      bool(ev) and ev["data"].get("spid") == 36
      and ev["data"].get("facility") == "server", str(ev)[:160])

ev = parse_with("firebird", catalog_sample("firebird-server-log"))
check("firebird: block → error event w/ host source",
      bool(ev) and ev["category"] == "error" and ev["source"] == "X8000",
      str(ev)[:160])

ev = parse_with("neo4j_query", catalog_sample("neo4j-query-log"))
check("neo4j_query: duration + ERROR level",
      bool(ev) and ev["level"] == "ERROR"
      and ev["data"].get("duration_ms") == 7364, str(ev)[:160])

ev = parse_with("aws_vpc_flow", catalog_sample("aws-vpc-flow-log-default"))
check("aws_vpc_flow: ACCEPT default row → info + epoch ts",
      bool(ev) and ev["level"] == "INFO" and ev["data"].get("action") == "ACCEPT"
      and ev["ts_ms"] == 1418530010000.0, str(ev)[:160])

ev = parse_with("aws_elb_classic", catalog_sample("aws-elb-classic-access-log"))
check("aws_elb_classic: 200 → info, request captured",
      bool(ev) and ev["level"] == "INFO"
      and ev["data"].get("elb_status") == "200", str(ev)[:160])

ev = parse_with("azure_functions", catalog_sample("azure-functions-host"))
check("azure_functions: Information + function name + invocation id trace",
      bool(ev) and ev["level"] == "INFO"
      and ev["data"].get("function") == "approval"
      and ev["trace_id"] is not None, str(ev)[:160])

ev = parse_with("terraform_plan", catalog_sample("terraform-plan-human"))
check("terraform_plan: Plan summary parsed w/ add/change/destroy",
      bool(ev) and ev["data"].get("add") == 1 and ev["data"].get("destroy") == 0,
      str(ev)[:160])

ev = parse_with("flamegraph_folded", "main;compute;inner_loop 89")
check("flamegraph_folded: leaf/depth/samples",
      bool(ev) and ev["data"].get("leaf") == "inner_loop"
      and ev["data"].get("depth") == 3 and ev["data"].get("samples") == 89,
      str(ev)[:160])

ev = parse_with("bpftrace", catalog_sample("bpftrace stack aggregation"))
check("bpftrace: stack block → count 20 + frames",
      bool(ev) and ev["data"].get("count") == 20
      and len(ev["data"].get("frames") or []) == 3, str(ev)[:160])

ev = parse_with("gdb_mi", '*stopped,reason="signal-received",signal-name="SIGSEGV"')
check("gdb_mi: signal stop → error",
      bool(ev) and ev["category"] == "error", str(ev)[:160])

ev = parse_with("android_tombstone", catalog_sample("android-tombstone"))
check("android_tombstone: block → FATAL crash w/ SIGSEGV + pid",
      bool(ev) and ev["level"] == "FATAL" and ev["category"] == "error"
      and ev["data"].get("signal") == "SIGSEGV"
      and ev["data"].get("pid") == 17946, str(ev)[:160])

ev = parse_with("android_anr", catalog_sample("android-anr-traces"))
check("android_anr (gap fix): literal-\\n block → one error event w/ cmdline",
      bool(ev) and ev["category"] == "error"
      and ev["data"].get("cmdline") == "com.example.app", str(ev)[:160])

ev = parse_with("apple_ips", catalog_sample("apple-ips-crash-report"))
check("apple_ips: header+body → FATAL w/ EXC_CRASH + incident trace",
      bool(ev) and ev["level"] == "FATAL"
      and ev["data"].get("exception_type") == "EXC_CRASH"
      and ev["trace_id"] == "F91836EE-...", str(ev)[:160])

ev = parse_with("phoenix", catalog_sample("phoenix_request"))
check("phoenix: request block → GET /articles → 200 w/ request_id trace",
      bool(ev) and ev["trace_id"] == "F1ssWNn62Xas"
      and ev["data"].get("status") == 200, str(ev)[:160])

ev = parse_with("modsecurity", catalog_sample("modsecurity_serial"))
check("modsecurity: audit entry → error w/ rule id 942100",
      bool(ev) and ev["category"] == "error"
      and ev["data"].get("rule_id") == "942100", str(ev)[:160])

ev = parse_with("f5_asm", catalog_sample("f5-bigip-asm-kv-request"))
check("f5_asm: illegal request → warn w/ support_id trace",
      bool(ev) and ev["level"] == "WARN"
      and ev["trace_id"] == "3161892955527053449", str(ev)[:160])

ev = parse_with("pflog_text", catalog_sample("pflog-tcpdump-text"))
check("pflog_text: block in → warn w/ rule + iface",
      bool(ev) and ev["level"] == "WARN"
      and ev["data"].get("interface") == "em0", str(ev)[:160])

ev = parse_with("dhcpd_leases", catalog_sample("isc-dhcpd-leases-file"))
check("dhcpd_leases: inline lease → ip/state/mac/hostname",
      bool(ev) and ev["data"].get("ip") == "192.168.1.150"
      and ev["data"].get("binding_state") == "active"
      and ev["data"].get("hostname") == "workstation1", str(ev)[:160])

ev = parse_with("wireguard", catalog_sample("wireguard-kernel-dmesg"))
check("wireguard: handshake line → info w/ peer + endpoint",
      bool(ev) and ev["level"] == "INFO" and ev["data"].get("peer") == 3
      and ev["data"].get("endpoint") == "203.0.113.7:51820", str(ev)[:160])

ev = parse_with("ibm_mq", catalog_sample("ibm-mq-amqerr-text"))
check("ibm_mq: AMQ9207E → error w/ code + qmgr",
      bool(ev) and ev["level"] == "ERROR"
      and ev["data"].get("message_code") == "AMQ9207"
      and ev["data"].get("qmgr") == "QM1", str(ev)[:160])

ev = parse_with("exchange_tracking", catalog_sample("exchange-message-tracking-csv"))
check("exchange_tracking: RECEIVE row → info w/ recipient + msg-id trace",
      bool(ev) and ev["data"].get("event_id") == "RECEIVE"
      and ev["data"].get("recipient") == "jane@contoso.com"
      and ev["trace_id"] == "<abc@contoso.com>", str(ev)[:160])

ev = parse_with("ibmi_qsysopr", catalog_sample("ibmi-qsysopr-message-queue"))
check("ibmi_qsysopr: CPF0907 → ERROR w/ msgid + job + ts",
      bool(ev) and ev["level"] == "ERROR"
      and ev["data"].get("message_id") == "CPF0907"
      and ev["ts_ms"] is not None, str(ev)[:160])

ev = parse_with("osisoft_pi", catalog_sample("osisoft-pi-message-log"))
check("osisoft_pi: 2-line record → one event w/ '>>' message",
      bool(ev) and ev["data"].get("message_id") == 7
      and ev["data"]["message"].startswith("Connection accepted"),
      str(ev)[:160])

ev = parse_with("wonderware", catalog_sample("wonderware-archestra-aalog"))
check("wonderware: LogFlag Warning → warn w/ Message text",
      bool(ev) and ev["level"] == "WARN"
      and ev["data"]["message"] == "ScanGroup poll overrun", str(ev)[:160])

ev = parse_with("tpm2_eventlog", catalog_sample("TPM2 measured-boot event log"))
check("tpm2_eventlog: block → PCR 0 EV_S_CRTM_VERSION",
      bool(ev) and ev["data"].get("PCRIndex") == "0"
      and ev["data"].get("EventType") == "EV_S_CRTM_VERSION", str(ev)[:160])

ev = parse_with("conpot", catalog_sample("conpot-ics-honeypot-log"))
check("conpot: new modbus connection → warn (honeypot touch)",
      bool(ev) and ev["level"] == "WARN"
      and ev["data"].get("protocol") == "modbus", str(ev)[:160])

ev = parse_with("swift_proxy", INLINE_SAMPLES["swift_proxy"][1])
check("swift_proxy: inline row → GET → 200 w/ tx trace",
      bool(ev) and ev["data"].get("status") == 200
      and (ev["trace_id"] or "").startswith("tx"), str(ev)[:160])

ev = parse_with("pm2", "0|api    | 2026-07-21T15:02:13: Server listening on :3000")
check("pm2: prefixed line → app captured + ts",
      bool(ev) and ev["data"].get("app") == "api" and ev["ts_ms"] is not None,
      str(ev)[:160])

ev = parse_with("w3c_access", catalog_sample("cloudfront-standard-w3c"))
check("w3c_access (gap fix): literal-\\t CloudFront row parses w/ status 200",
      bool(ev) and ev["data"].get("status") == 200 and ev["ts_ms"] is not None,
      str(ev)[:160])

# levels/categories that MUST fire the bridge's failure detector
print("\n── 3b. error-category invariants ──")
for desc, adapter_name, sample in [
    ("sybase 'Error:' line", "sybase_ase",
     "00:0002:00000:00036:2019/03/26 01:41:39.17 server  Error: 1105, Severity: 17, State: 2"),
    ("ovs EMER", "ovs_vlog",
     "2016-03-14T20:19:04.741Z|00002|dpdk|EMER|Cannot init EAL"),
    ("netapp ERROR event", "netapp_ems", catalog_sample("netapp-ontap-event-log-show")),
    ("chef FATAL", "chef_client",
     "[2017-05-04T18:56:33+00:00] FATAL: Chef::Exceptions::ChildConvergeError"),
    ("puppet apply Error", "puppet_apply",
     "Error: /Stage[main]/Main/File[/tmp/x]/ensure: change from absent failed"),
]:
    ev = parse_with(adapter_name, sample)
    check(f"{desc} → category error",
          bool(ev) and ev["category"] == "error", str(ev)[:160])

# ── 4. Registry invariants ────────────────────────────────────────────────────
print("\n── 4. registry invariants ──")
names = [a.name for a in la.REGISTRY]
check("no duplicate adapter names", len(names) == len(set(names)),
      f"{len(names)} entries, {len(set(names))} unique")
check("tail is exactly [structural, raw]", names[-2:] == ["structural", "raw"],
      f"tail={names[-2:]}")
check("registry grew to 266 (264 named + structural + raw)", len(names) >= 266,
      f"size={len(names)}")

_batch5 = sorted(set(NEW_ADAPTERS.values()) | set(INLINE_SAMPLES) - {"uwsgi"})
_missing = [n for n in _batch5 if la.get_adapter(n) is None]
check(f"all {len(_batch5)} batch-5 adapters registered", not _missing,
      f"missing: {_missing}")
check("neo4j_query registered before neo4j (tie-break)",
      names.index("neo4j_query") < names.index("neo4j"))
check("wireguard registered before linux_dmesg (tie-break)",
      names.index("wireguard") < names.index("linux_dmesg"))

# ── 5. Hostile / empty input across the WHOLE registry (0 raises) ─────────────
print("\n── 5. hostile-input sweep ──")
_PROBES = [
    "", " ", "\t", "\x00\x01\xff\xfe binary\x7f garbage",
    "日本語テキスト émojis 🚀 → ← ∞", "A" * 5120,
    "x=1\\ny=2\\nz=3", "line one\\nline two\\nline three",
    "2026-07-21T", "[truncat", "<134>", "{", "}", '{"a":',
    "\\n\\n\\n", "|||||", "---", "@[", "]: ", "lease 1.2.3.4 {",
    "--abcdef12-A--", "^done,", "*stopped", "Timestamp=\"", "seed=",
]
_raises = 0
for _a in la.REGISTRY:
    for _p in _PROBES:
        try:
            _a.detect([_p])
            _a.detect([])
            _a.parse_line(_p)
        except Exception as _exc:
            _raises += 1
            print(f"    RAISE {_a.name} on {_p[:24]!r}: {_exc}")
check(f"{len(la.REGISTRY)} adapters × {len(_PROBES)} probes: 0 raises",
      _raises == 0, f"{_raises} raises")

# ── 6. Full catalog sweep — every sample, 0 raises, fallthrough accounting ────
print("\n── 6. full catalog sweep ──")
if _CATALOG:
    _sweep_raises = 0
    _high_fall = []
    for _name, _e in _CATALOG.items():
        try:
            _got, _conf, _ = la.detect_adapter([_e["sample_line"]])
            _ad = la.get_adapter(_got.name)
            for _sub in str(_e["sample_line"]).splitlines()[:5]:
                _ad.parse_line(_sub)
            _ad.parse_line(_e["sample_line"])
        except Exception as _exc:
            _sweep_raises += 1
            print(f"    RAISE on {_name}: {_exc}")
            continue
        if _e.get("priority") == "high" and _got.name in _FALLBACKS:
            _high_fall.append((_name, _e.get("structure")))
    check(f"catalog sweep over {len(_CATALOG)} formats: 0 raises",
          _sweep_raises == 0, f"{_sweep_raises} raises")
    _nonbin = [n for n, s in _high_fall if s != "binary"]
    # the only surviving non-binary high-priority fallthrough must be the two
    # TEMPLATE entries (formats verified inline above) + the literal-\n NDJSON
    # sample (real NDJSON routes to jsonl line-by-line).
    _allowed = {"swift-proxy-access", "uwsgi-request-log",
                "Elastic APM intake NDJSON"}
    check("non-binary high-priority fallthrough ⊆ {templates, NDJSON-sample}",
          set(_nonbin) <= _allowed, f"unexpected: {sorted(set(_nonbin) - _allowed)}")
    check(f"high-priority fallthrough is ≤ 18 total (was 71 before batch 5)",
          len(_high_fall) <= 18, f"{len(_high_fall)}: {_high_fall}")
else:
    check("catalog file found", False, "docs/log_catalog_master.json missing")

# ── result ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
if _fails:
    print(f"RESULT: {_fails} FAILURE(S)")
    sys.exit(1)
print("RESULT: all tests passed")
