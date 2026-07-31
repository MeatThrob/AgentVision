"""
Tests for the BATCH-6 family adapters + the batch-6 GAP FIXES.

Pure stdlib (no pytest). Run:
    python3 python_backend/connectors/test_adapters_batch6.py
Exits non-zero on any failure so it gates CI / packaging.

The hard rule (same as batches 2-5): for EVERY adapter added or fixed, its OWN
sample — pulled from docs/log_catalog_master.json — must resolve through
detect_adapter([sample]) to THAT adapter. A resolution to a fallback
(structural/generic_ts/raw) or to the wrong named adapter is a FAIL.

Batch 6 opens the MEDIUM tier: medium-priority non-binary fallthrough went
308 → ~223 (60 new adapters + 9 gap fixes = 85 formats newly named).
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


# ── 1. BATCH-6 adapters: catalog_name → expected adapter ─────────────────────
NEW_ADAPTERS = {
    # build tools / IDE / package managers / VCS tracing (devtools)
    "Gradle build console output":       "gradle_build",
    "Gradle daemon log":                 "gradle_daemon",
    "Yarn Berry (v2+) output":           "yarn_berry",
    "Yarn Classic error log":            "yarn_classic",
    "pnpm output":                       "pnpm",
    "uv output / log":                   "uv",
    "android-studio-idea-log":           "idea_log",
    "Jupyter/IPython kernel log":        "jupyter",
    "teamcity-service-messages":         "teamcity",
    "GIT_TRACE2 normal target":          "git_trace",
    "Legacy GIT_TRACE / packet / curl":  "git_trace",
    "esbuild output / metafile":         "esbuild",
    "nix internal-json log":             "nix_json",
    # app runtimes / web servers / workers (runtime)
    "aiohttp_access":                    "aiohttp_access",
    "tornado_access":                    "tornado",
    "puma_stdout":                       "puma",
    "sidekiq-log":                       "sidekiq",
    "sidekiq_log":                       "sidekiq",
    "rails_lograge":                     "lograge",
    "nestjs_logger":                     "nestjs",
    "ruby_backtrace":                    "ruby_backtrace",
    "aspnet_systemd_console":            "aspnet_systemd",
    "apache-doris-be-glog":              "glog4",
    "nebulagraph-glog":                  "glog4",
    "typesense-glog":                    "glog4",
    "memgraph-spdlog":                   "spdlog",
    "cloudflared-tunnel-log":            "zerolog",
    # android (new family module)
    "android-logcat-long":               "logcat_long",
    "android-art-gc-line":               "art_gc",
    "android-instrumentation-status":    "android_instrumentation",
    "android-dropbox":                   "android_dropbox",
    # apple (new family module)
    "macos-log-show-compact":            "macos_log_compact",
    "macos-asl-syslog-cli":              "macos_asl",
    "macos_log_syslog_style":            "macos_syslog_style",
    "macos-install-log":                 "macos_syslog_style",
    "apple-crash-report-legacy":         "apple_crash_legacy",
    "apple-spindump-report":             "apple_crash_legacy",   # hang report, warn
    # cloud
    "aws-nlb-access-log":                "aws_nlb",
    "flyio-log-line":                    "flyio",
    # network
    "powerdns-auth-log":                 "powerdns",
    "powerdns-recursor-log":             "powerdns",
    "kea-dhcp6-log":                     "kea_dhcp",
    "keepalived-vrrp-log":               "keepalived",
    "busybox_syslogd":                   "busybox_syslog",
    "openwrt_logread":                   "busybox_syslog",
    "freeradius-log":                    "freeradius",
    "dnsmasq-leases-file":               "dnsmasq_leases",
    # monitoring servers (observability)
    "nagios-core-log":                   "nagios",
    "icinga2-mainlog":                   "icinga2",
    "zabbix-daemon-log":                 "zabbix",
    "collectd-logfile":                  "collectd",
    "netdata-error-log":                 "netdata",
    "graylog-server-log":                "graylog",
    "splunkd-log":                       "splunkd",
    # databases / directory
    "couchdb-log":                       "couchdb",
    "389ds-errors":                      "ds389_errors",
    # java servers (webserver)
    "glassfish-access-log":              "glassfish_access",
    "wildfly-console-log":               "wildfly_console",
    # virt
    "vcenter-vpxd-event-record":         "vcenter_vpxd",
    # security / windows
    "falco":                             "falco",
    "windows_defender_av":               "defender_detection",
    "windows_defender_operational":      "defender_detection",
    "windows-firewall-pfirewall":        "windows_firewall",
    "windows_firewall_log":              "windows_firewall",
    "windows_firewall_pfirewall":        "windows_firewall",
    # os platform / config mgmt / backup / games / profiling
    "journald_export":                   "journald_export",
    "apt term.log":                      "apt_term",
    "salt-daemon-log":                   "salt_daemon",
    "rclone-text-log":                   "rclone",
    "Unreal Engine log (UE_LOG)":        "unreal",
    "perf stat":                         "perf_stat",
}

# gap fixes routing catalog formats to EXISTING adapters:
#   • celery         — multi-line-aware detect
#   • ruby           — multi-line-aware detect (unicorn/puma Logger stdout)
#   • logcat         — threadtime + year/usec/zone/uid modifier variants
#   • mikrotik_routeros — syslog-shipped + bare-HH:MM:SS topic lines
#   • esxi_vmkernel  — ESXi7 2-letter level codes + literal 'vmkernel:' tag
#   • weblogic       — angle-field stdout without the '####' sigil
#   • ftrace         — flags column optional (trace-cmd report layout)
#   • sanitizer      — helgrind/DRD race phrasing
#   • linux_dmesg    — optional '<N>' klog-priority prefix + multi-line detect
GAP_FIXES = {
    "celery_worker":                     "celery",
    "unicorn_stdout":                    "ruby",
    "android-logcat-modifier-variants":  "logcat",
    "mikrotik_routeros":                 "mikrotik_routeros",
    "mikrotik-dhcp-log":                 "mikrotik_routeros",
    "mikrotik-firewall-log":             "mikrotik_routeros",
    "vmware_vmkernel":                   "esxi_vmkernel",
    "weblogic-stdout":                   "weblogic",
    "trace-cmd report":                  "ftrace",
    "Valgrind Helgrind/DRD race report": "sanitizer",
    "ACPI kernel/firmware messages":     "linux_dmesg",
    "android_logcat_kernel":             "linux_dmesg",
}

print("\n── 1. batch-6 adapters route their own catalog samples ──")
for cat_name, adapter_name in sorted({**NEW_ADAPTERS, **GAP_FIXES}.items()):
    sample = catalog_sample(cat_name)
    if not sample:
        check(f"{cat_name} → {adapter_name}", False, "catalog sample missing")
        continue
    got, conf, _ = la.detect_adapter([sample])
    check(f"{cat_name} → {adapter_name}", got.name == adapter_name,
          f"got {got.name} ({conf:.2f})")

# ── 2. NEGATIVE ownership guards — formats batch 6 must NOT steal ─────────────
print("\n── 2. negative ownership guards ──")
NEGATIVE = [
    # (description, sample, expected owner | None = just not-batch-6)
    ("heroku router logfmt stays with heroku_router",
     catalog_sample("heroku-router-logfmt"), "heroku_router"),
    ("plain logfmt stays with logfmt",
     'level=info ts=2026-07-20T12:00:00Z msg="thing" seed=42', "logfmt"),
    ("CLF access log stays with access_log",
     '1.2.3.4 - - [20/Jul/2026:12:00:00 +0000] "GET /x HTTP/1.1" 200 1234',
     "access_log"),
    ("standard 4-digit glog stays with klog",
     "I0720 12:00:00.123456  1234 controller.go:12] reconciling", "klog"),
    ("original single-line celery stays with celery",
     "[2015-01-21 22:18:10,710: INFO/MainProcess] Connected to redis://",
     "celery"),
    ("ftrace WITH flags column stays with ftrace",
     "          <idle>-0     [000] d.h. 12345.678901: sched_switch: prev_comm=swapper",
     "ftrace"),
    ("single-line Ruby Logger stays with ruby",
     "I, [2026-07-20T12:00:00.123456 #1234]  INFO -- app: booted", "ruby"),
    ("plain '[pid] text' without puma markers stays off puma",
     "[12345] something happened here", None),
    ("freebsd periodic 'Removing stale files' stays off apt_term",
     "Removing stale files from /var/preserve:", None),
    ("plain syslog 3164 line stays off busybox/macos_asl",
     "Jul 20 12:00:00 myhost myapp[123]: started ok", None),
    ("fluent-bit bracket log stays with fluentbit",
     "[2024/01/15 10:30:00] [ info] [engine] started (pid=1)", "fluentbit"),
    ("rust tracing console stays with rust_tracing",
     "2026-07-20T12:00:00.123456Z  INFO myapp::server: listening addr=0.0.0.0:8080",
     "rust_tracing"),
    ("php monolog stays with php_monolog",
     "[2026-07-20T12:00:00.123456+00:00] app.ERROR: boom {} {}", "php_monolog"),
    ("log4j '[thread] LEVEL logger - msg' stays with log4j",
     "2026-07-20 12:00:00,123 [main] INFO com.example.App - started", "log4j"),
    ("dmesg without <N> prefix stays with linux_dmesg",
     "[    3.123456] usb 1-1: new high-speed USB device number 2", "linux_dmesg"),
    ("ASan report stays with sanitizer",
     "==1234==ERROR: AddressSanitizer: heap-use-after-free on address 0x60",
     "sanitizer"),
    ("go slash-date WITHOUT level token stays off rclone",
     "2023/01/15 12:00:00 starting server on :8080", None),
    ("W3C extended log with #Fields stays with w3c_access",
     "#Fields: date time cs-method cs-uri-stem sc-status\n"
     "2026-07-20 12:00:00 GET /index.html 200", "w3c_access"),
]
_B6 = set(NEW_ADAPTERS.values())
for desc, sample, owner in NEGATIVE:
    got, conf, _ = la.detect_adapter([sample])
    if owner is None:
        check(desc, got.name not in _B6, f"stolen by {got.name} ({conf:.2f})")
    else:
        check(desc, got.name == owner, f"got {got.name} ({conf:.2f})")

# ── 3. Parse-quality spot checks ──────────────────────────────────────────────
print("\n── 3. parse-quality spot checks ──")


def parse_with(adapter_name, sample):
    a = la.get_adapter(adapter_name)
    return a.parse_line(sample) if a else None


ev = parse_with("gradle_build", catalog_sample("Gradle build console output"))
check("gradle_build: block → task event", bool(ev) and "task" in ev["data"]
      and ev["data"]["task"] == ":app:compileJava", str(ev)[:160])

ev = parse_with("gradle_build", "FAILURE: Build failed with an exception.")
check("gradle_build: FAILURE → error category",
      bool(ev) and ev["category"] == "error", str(ev)[:160])

ev = parse_with("gradle_daemon", catalog_sample("Gradle daemon log").splitlines()[0])
check("gradle_daemon: DEBUG + logger + tz-aware ts",
      bool(ev) and ev["level"] == "DEBUG" and ev["ts_ms"] is not None
      and "org.gradle" in ev["source"], str(ev)[:160])

ev = parse_with("yarn_berry", "➤ YN0001: │ Error: ENOENT")
check("yarn_berry: YN0001 → error w/ code",
      bool(ev) and ev["category"] == "error"
      and ev["data"].get("code") == "YN0001", str(ev)[:160])

ev = parse_with("yarn_classic", catalog_sample("Yarn Classic error log"))
check("yarn_classic: block → error w/ Error: message",
      bool(ev) and ev["category"] == "error"
      and ev["data"]["message"].startswith("Error:"), str(ev)[:160])

ev = parse_with("pnpm", catalog_sample("pnpm output"))
check("pnpm: Progress counters parsed",
      bool(ev) and ev["data"].get("resolved") == 210
      and ev["data"].get("added") == 210, str(ev)[:160])

ev = parse_with("uv", "Resolved 34 packages in 12ms")
check("uv: summary verb + package count",
      bool(ev) and ev["data"].get("verb") == "Resolved"
      and ev["data"].get("packages") == 34, str(ev)[:160])

ev = parse_with("idea_log", catalog_sample("android-studio-idea-log"))
check("idea_log: INFO + logger + ts",
      bool(ev) and ev["level"] == "INFO" and ev["ts_ms"] is not None
      and ev["source"] == "c.i.i.StartupUtil", str(ev)[:160])

ev = parse_with("jupyter", catalog_sample("Jupyter/IPython kernel log"))
check("jupyter: WARNING | pipe level parsed",
      bool(ev) and ev["level"] == "WARN"
      and ev["source"] == "jupyter.IPKernelApp", str(ev)[:160])

ev = parse_with("teamcity", "##teamcity[testFailed name='T.x' message='boom|n']")
check("teamcity: testFailed → error + |n unescape",
      bool(ev) and ev["category"] == "error"
      and ev["data"].get("message") == "boom\n", str(ev)[:160])

ev = parse_with("git_trace", catalog_sample("GIT_TRACE2 normal target").splitlines()[0])
check("git_trace: debug level + file:line source + ts",
      bool(ev) and ev["level"] == "DEBUG" and ev["ts_ms"] is not None
      and ev["source"].startswith("common-main.c"), str(ev)[:160])

ev = parse_with("esbuild", catalog_sample("esbuild output / metafile"))
check("esbuild: ✘ [ERROR] block → error w/ location",
      bool(ev) and ev["category"] == "error"
      and "src/index.ts:3:20:" in str(ev["data"].get("location", "")), str(ev)[:160])

ev = parse_with("nix_json", '@nix {"action":"msg","level":0,"text":"build failed"}')
check("nix_json: level-0 msg → error",
      bool(ev) and ev["category"] == "error"
      and ev["data"]["message"] == "build failed", str(ev)[:160])

ev = parse_with("aiohttp_access", catalog_sample("aiohttp_access"))
check("aiohttp_access: 200 + request + ts",
      bool(ev) and ev["data"].get("status") == 200 and ev["ts_ms"] is not None,
      str(ev)[:160])

ev = parse_with("tornado", catalog_sample("tornado_access"))
check("tornado: yymmdd ts + access fields",
      bool(ev) and ev["ts_ms"] is not None and ev["data"].get("status") == 200
      and ev["data"].get("duration_ms") == 12.34, str(ev)[:160])

ev = parse_with("puma", "[12345] - Worker 0 (PID: 12346) booted in 0.01s, phase: 0")
check("puma: worker line → worker + worker_pid",
      bool(ev) and ev["data"].get("worker") == 0
      and ev["data"].get("worker_pid") == 12346, str(ev)[:160])

ev = parse_with("sidekiq", catalog_sample("sidekiq_log"))
check("sidekiq: class/jid → fields + jid as trace",
      bool(ev) and ev["data"].get("class") == "HardWorker"
      and ev["trace_id"] == "9f8e7d", str(ev)[:160])

ev = parse_with("lograge", catalog_sample("rails_lograge"))
check("lograge: method/path/status parsed",
      bool(ev) and ev["data"].get("controller") == "ArticlesController"
      and ev["data"].get("status") == "200", str(ev)[:160])

ev = parse_with("nestjs", catalog_sample("nestjs_logger"))
check("nestjs: LOG → info w/ context + locale ts",
      bool(ev) and ev["level"] == "INFO" and ev["ts_ms"] is not None
      and ev["data"].get("context") == "NestFactory", str(ev)[:160])

ev = parse_with("ruby_backtrace", catalog_sample("ruby_backtrace"))
check("ruby_backtrace: head frame → error w/ exception class",
      bool(ev) and ev["category"] == "error" and ev["source"] == "RuntimeError"
      and ev["data"].get("line") == 7, str(ev)[:160])

ev = parse_with("aspnet_systemd", catalog_sample("aspnet_systemd_console"))
check("aspnet_systemd: <6> → info w/ category source",
      bool(ev) and ev["level"] == "INFO"
      and ev["source"] == "Microsoft.Hosting.Lifetime", str(ev)[:160])

ev = parse_with("glog4", catalog_sample("typesense-glog"))
check("glog4: full-year date parsed to ts",
      bool(ev) and ev["level"] == "INFO" and ev["ts_ms"] is not None
      and "typesense_server_utils.cpp" in ev["source"], str(ev)[:160])

ev = parse_with("zerolog", catalog_sample("cloudflared-tunnel-log"))
check("zerolog: INF + trailing kv split",
      bool(ev) and ev["level"] == "INFO"
      and ev["data"].get("connIndex") == "0", str(ev)[:160])

ev = parse_with("logcat_long", catalog_sample("android-logcat-long"))
check("logcat_long: 2-line record → one debug event w/ pid/tid",
      bool(ev) and ev["level"] == "DEBUG" and ev["data"].get("pid") == 172
      and ev["source"] == "dalvikvm", str(ev)[:160])

ev = parse_with("android_instrumentation",
                catalog_sample("android-instrumentation-status"))
check("android_instrumentation: block → test field",
      bool(ev) and ev["data"].get("test") == "useAppContext", str(ev)[:160])

ev = parse_with("android_dropbox", catalog_sample("android-dropbox"))
check("android_dropbox: anr tag → error + bytes",
      bool(ev) and ev["category"] == "error"
      and ev["data"].get("bytes") == 13566, str(ev)[:160])

ev = parse_with("macos_log_compact", catalog_sample("macos-log-show-compact"))
check("macos_log_compact: Df → info w/ subsystem",
      bool(ev) and ev["level"] == "INFO"
      and ev["data"].get("subsystem") == "com.apple.loginwindow", str(ev)[:160])

ev = parse_with("macos_asl", catalog_sample("macos-asl-syslog-cli"))
check("macos_asl: <Notice> → info w/ launchd source",
      bool(ev) and ev["level"] == "INFO"
      and ev["source"] == "com.apple.xpc.launchd", str(ev)[:160])

ev = parse_with("macos_syslog_style", catalog_sample("macos-install-log"))
check("macos_syslog_style: hour-only tz normalized → ts",
      bool(ev) and ev["ts_ms"] is not None
      and ev["source"] == "softwareupdated", str(ev)[:160])

ev = parse_with("apple_crash_legacy", catalog_sample("apple-crash-report-legacy"))
check("apple_crash_legacy: .crash → FATAL w/ exception type + pid",
      bool(ev) and ev["level"] == "FATAL" and ev["category"] == "error"
      and ev["data"].get("pid") == 1234
      and "EXC_BAD_ACCESS" in ev["data"].get("exception_type", ""), str(ev)[:160])

ev = parse_with("apple_crash_legacy", catalog_sample("apple-spindump-report"))
check("apple_crash_legacy: spindump → WARN hang report (not a crash)",
      bool(ev) and ev["level"] == "WARN"
      and ev["data"].get("report_kind") == "spindump", str(ev)[:160])

ev = parse_with("aws_nlb", catalog_sample("aws-nlb-access-log"))
check("aws_nlb: tls row → info w/ cipher + ts",
      bool(ev) and ev["level"] == "INFO" and ev["ts_ms"] is not None
      and ev["data"].get("tls_version") == "tlsv12", str(ev)[:160])

ev = parse_with("flyio", catalog_sample("flyio-log-line"))
check("flyio: region + machine id parsed",
      bool(ev) and ev["data"].get("region") == "lhr"
      and ev["data"].get("machine") == "5683606c41098e", str(ev)[:160])

ev = parse_with("powerdns", catalog_sample("powerdns-recursor-log"))
check("powerdns: recursor question → qname/qtype",
      bool(ev) and ev["data"].get("qname") == "www.exampledomain.com"
      and ev["data"].get("qtype") == "A", str(ev)[:160])

ev = parse_with("kea_dhcp", catalog_sample("kea-dhcp6-log"))
check("kea_dhcp: DHCP6_LEASE_ADVERT message id",
      bool(ev) and ev["level"] == "INFO"
      and ev["data"].get("message_id") == "DHCP6_LEASE_ADVERT", str(ev)[:160])

ev = parse_with("keepalived", catalog_sample("keepalived-vrrp-log"))
check("keepalived: MASTER transition → instance + state",
      bool(ev) and ev["data"].get("instance") == "VI_1"
      and ev["data"].get("state") == "MASTER", str(ev)[:160])

ev = parse_with("busybox_syslog", catalog_sample("openwrt_logread"))
check("busybox_syslog: openwrt ctime row → warn + facility",
      bool(ev) and ev["level"] == "WARN" and ev["ts_ms"] is not None
      and ev["data"].get("facility") == "kern", str(ev)[:160])

ev = parse_with("freeradius", catalog_sample("freeradius-log"))
check("freeradius: Login OK → info w/ user",
      bool(ev) and ev["level"] == "INFO" and ev["data"].get("user") == "bob"
      and ev["ts_ms"] is not None, str(ev)[:160])

ev = parse_with("dnsmasq_leases", catalog_sample("dnsmasq-leases-file"))
check("dnsmasq_leases: row → ip/mac/hostname + expiry ts",
      bool(ev) and ev["data"].get("ip") == "142.174.150.208"
      and ev["data"].get("hostname") == "M61480"
      and ev["ts_ms"] == 1108086503000.0, str(ev)[:160])

ev = parse_with("nagios", catalog_sample("nagios-core-log"))
check("nagios: CRITICAL service alert → error w/ host/service",
      bool(ev) and ev["category"] == "error" and ev["data"].get("host") == "web01"
      and ev["data"].get("service") == "HTTP", str(ev)[:160])

ev = parse_with("icinga2", catalog_sample("icinga2-mainlog"))
check("icinga2: information/facility → info + ts",
      bool(ev) and ev["level"] == "INFO" and ev["ts_ms"] is not None
      and ev["source"] == "icinga2.ApiListener", str(ev)[:160])

ev = parse_with("zabbix", catalog_sample("zabbix-daemon-log"))
check("zabbix: 'database is down' → error w/ pid + ts",
      bool(ev) and ev["category"] == "error" and ev["data"].get("pid") == 12345
      and ev["ts_ms"] is not None, str(ev)[:160])

ev = parse_with("graylog", catalog_sample("graylog-server-log"))
check("graylog: INFO [ServerBootstrap] parsed",
      bool(ev) and ev["level"] == "INFO"
      and ev["source"] == "graylog.ServerBootstrap", str(ev)[:160])

ev = parse_with("splunkd", catalog_sample("splunkd-log"))
check("splunkd: MM-DD-YYYY date + component + thread",
      bool(ev) and ev["ts_ms"] is not None
      and ev["data"].get("component") == "TailReader"
      and ev["data"].get("thread") == "12345 tailreader0", str(ev)[:160])

ev = parse_with("couchdb", catalog_sample("couchdb-log"))
check("couchdb: [notice] → info w/ erlang node source",
      bool(ev) and ev["level"] == "INFO"
      and ev["source"] == "couchdb@127.0.0.1", str(ev)[:160])

ev = parse_with("ds389_errors", catalog_sample("389ds-errors"))
check("ds389_errors: ERR → error w/ function + zone-aware ts",
      bool(ev) and ev["category"] == "error"
      and ev["data"].get("function") == "oc_check_required"
      and ev["ts_ms"] is not None, str(ev)[:160])

ev = parse_with("glassfish_access", catalog_sample("glassfish-access-log"))
check("glassfish_access: quoted CLF → 200 + anonymous user",
      bool(ev) and ev["data"].get("status") == 200
      and ev["data"].get("user") is None, str(ev)[:160])

ev = parse_with("wildfly_console", catalog_sample("wildfly-console-log"))
check("wildfly_console: WFLYSRV code + thread",
      bool(ev) and ev["level"] == "INFO"
      and ev["data"].get("message_id") == "WFLYSRV0025"
      and ev["data"].get("thread") == "Controller Boot Thread", str(ev)[:160])

ev = parse_with("vcenter_vpxd", catalog_sample("vcenter-vpxd-event-record"))
check("vcenter_vpxd: VmMessageErrorEvent → error w/ event type",
      bool(ev) and ev["category"] == "error"
      and ev["data"].get("event_type") == "vim.event.VmMessageErrorEvent",
      str(ev)[:160])

ev = parse_with("falco", catalog_sample("falco"))
check("falco: Warning rule → warn w/ container fields",
      bool(ev) and ev["level"] == "WARN"
      and ev["data"].get("container_id") == "abc123"
      and ev["data"].get("k8s.pod") == "web-1", str(ev)[:160])

ev = parse_with("defender_detection", catalog_sample("windows_defender_operational"))
check("defender_detection: Severe detection → error w/ name + path",
      bool(ev) and ev["category"] == "error"
      and "Wacatac" in ev["data"].get("name", "")
      and ev["data"].get("action") == "Quarantine", str(ev)[:160])

ev = parse_with("windows_firewall", catalog_sample("windows-firewall-pfirewall"))
check("windows_firewall: DROP row → warn w/ 5-tuple",
      bool(ev) and ev["level"] == "WARN" and ev["data"].get("action") == "DROP"
      and ev["data"].get("dst_port") == "53"
      and ev["data"].get("path") == "RECEIVE", str(ev)[:160])

ev = parse_with("journald_export", catalog_sample("journald_export"))
check("journald_export: record → MESSAGE + µs→ms ts",
      bool(ev) and ev["data"]["message"] == "Started Session 3"
      and ev["ts_ms"] == 1753020191123.456, str(ev)[:160])

ev = parse_with("apt_term", catalog_sample("apt term.log"))
check("apt_term: Log started → ts parsed",
      bool(ev) and ev["ts_ms"] is not None, str(ev)[:160])

ev = parse_with("salt_daemon", catalog_sample("salt-daemon-log"))
check("salt_daemon: [salt.minion][INFO][pid] parsed",
      bool(ev) and ev["level"] == "INFO" and ev["source"] == "salt.minion"
      and ev["data"].get("pid") == 12345, str(ev)[:160])

ev = parse_with("rclone", catalog_sample("rclone-text-log"))
check("rclone: object + action split",
      bool(ev) and ev["data"].get("object") == "file.txt"
      and ev["data"].get("action") == "Copied (new)", str(ev)[:160])

ev = parse_with("unreal", catalog_sample("Unreal Engine log (UE_LOG)"))
check("unreal: LogTemp Warning → warn w/ frame + ts",
      bool(ev) and ev["level"] == "WARN" and ev["source"] == "LogTemp"
      and ev["ts_ms"] is not None and ev["data"].get("frame") == 0, str(ev)[:160])

ev = parse_with("perf_stat", catalog_sample("perf stat"))
check("perf_stat: table → counters dict w/ cycles",
      bool(ev) and ev["data"].get("counters", {}).get("cycles") == 1234567,
      str(ev)[:160])

# gap-fix parse checks
ev = parse_with("logcat", catalog_sample("android-logcat-modifier-variants"))
check("logcat (gap fix): year+usec+zone+uid variant parsed w/ ts",
      bool(ev) and ev["level"] == "INFO" and ev["ts_ms"] is not None
      and ev["data"].get("uid") == "10056", str(ev)[:160])

ev = parse_with("mikrotik_routeros", catalog_sample("mikrotik-dhcp-log"))
check("mikrotik (gap fix): bare-time dhcp topics parsed",
      bool(ev) and ev["data"].get("topics") == "dhcp,info"
      and ev["ts_ms"] is not None, str(ev)[:160])

ev = parse_with("esxi_vmkernel", catalog_sample("vmware_vmkernel"))
check("esxi_vmkernel (gap fix): In(182) vmkernel: form parsed",
      bool(ev) and ev["level"] == "INFO"
      and ev["data"].get("cpu") == 0, str(ev)[:160])

ev = parse_with("weblogic", catalog_sample("weblogic-stdout"))
check("weblogic (gap fix): stdout w/o #### → BEA id + info",
      bool(ev) and ev["data"].get("message_id") == "BEA-000360", str(ev)[:160])

ev = parse_with("ftrace", catalog_sample("trace-cmd report"))
check("ftrace (gap fix): flag-less trace-cmd row parsed",
      bool(ev) and ev["source"] == "sched_wakeup"
      and ev["data"].get("pid") == 4262, str(ev)[:160])

ev = parse_with("sanitizer", catalog_sample("Valgrind Helgrind/DRD race report"))
check("sanitizer (gap fix): helgrind race → error",
      bool(ev) and ev["category"] == "error", str(ev)[:160])

ev = parse_with("linux_dmesg", catalog_sample("android_logcat_kernel"))
check("linux_dmesg (gap fix): <6>[ts] prefix → INFO level",
      bool(ev) and ev["level"] == "INFO"
      and ev["data"].get("uptime") == 12.345678, str(ev)[:160])

ev = parse_with("ruby", catalog_sample("unicorn_stdout"))
check("ruby (gap fix): unicorn 2-line block → first record parsed",
      bool(ev) and ev["level"] == "INFO" and ev["data"].get("pid") == 12345,
      str(ev)[:160])

# levels/categories that MUST fire the bridge's failure detector
print("\n── 3b. error-category invariants ──")
for desc, adapter_name, sample in [
    ("sidekiq ERROR line", "sidekiq",
     "2026-07-21T15:40:39.123Z pid=1 tid=abc ERROR: job raised exception"),
    ("zerolog FTL", "zerolog", "2023-01-15T12:00:00Z FTL cannot bind listener"),
    ("splunkd ERROR", "splunkd",
     "01-01-2023 12:00:00.123 +0000 ERROR TailReader - failed to open file"),
    ("wildfly ERROR", "wildfly_console",
     "08:10:41,000 ERROR [org.jboss.msc] (thread) WFLYCTL0013: Operation failed"),
    ("kea ERROR", "kea_dhcp",
     "ERROR [kea-dhcp6.dhcp6/1.2] DHCP6_PACKET_PARSE_FAIL failed to parse"),
    ("nix level-0", "nix_json", '@nix {"action":"msg","level":0,"text":"boom"}'),
    ("unreal Error verbosity", "unreal",
     "[2026.07.20-14.00.01:123][  7]LogNet: Error: connection lost"),
    ("defender Severe", "defender_detection",
     catalog_sample("windows_defender_av")),
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
check("registry grew to 326 (324 named + structural + raw)", len(names) >= 326,
      f"size={len(names)}")

_batch6 = sorted(set(NEW_ADAPTERS.values()))
_missing = [n for n in _batch6 if la.get_adapter(n) is None]
check(f"all {len(_batch6)} batch-6 adapters registered", not _missing,
      f"missing: {_missing}")
for early, late in [("macos_asl", "syslog"), ("busybox_syslog", "syslog"),
                    ("keepalived", "syslog"), ("lograge", "logfmt"),
                    ("sidekiq", "logfmt"), ("zerolog", "logfmt"),
                    ("windows_firewall", "w3c_access"),
                    ("aiohttp_access", "access_log"),
                    ("glassfish_access", "access_log")]:
    check(f"{early} registered before {late} (tie-break)",
          names.index(early) < names.index(late))

# ── 5. Hostile / empty input across the WHOLE registry (0 raises) ─────────────
print("\n── 5. hostile-input sweep ──")
_PROBES = [
    "", " ", "\t", "\x00\x01\xff\xfe binary\x7f garbage",
    "日本語テキスト émojis 🚀 → ← ∞", "A" * 5120,
    "x=1\\ny=2\\nz=3", "line one\\nline two\\nline three",
    "2026-07-21T", "[truncat", "<134>", "{", "}", '{"a":', "@nix {",
    "\\n\\n\\n", "|||||", "---", "@[", "]: ", "##teamcity[", "➤ YN",
    "> Task", "==31==", "[ 04-01", "INSTRUMENTATION_", "Process:",
    "__CURSOR=", "Event [1] [1-1]", "tls 2.0", "0 (1.0.0) 2026",
    "1,234,567", "seconds time elapsed", "[Nest] x", "I2023 bad",
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
    _med_fall = 0
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
        if _got.name in _FALLBACKS:
            if _e.get("priority") == "high":
                _high_fall.append((_name, _e.get("structure")))
            elif _e.get("priority") == "medium" and _e.get("structure") != "binary":
                _med_fall += 1
    check(f"catalog sweep over {len(_CATALOG)} formats: 0 raises",
          _sweep_raises == 0, f"{_sweep_raises} raises")
    check("high-priority fallthrough unchanged (≤ 18, batch-5 floor)",
          len(_high_fall) <= 18, f"{len(_high_fall)}: {_high_fall}")
    check(f"medium non-binary fallthrough ≤ 225 (was 308 before batch 6)",
          _med_fall <= 225, f"{_med_fall}")
else:
    check("catalog file found", False, "docs/log_catalog_master.json missing")

# ── result ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
if _fails:
    print(f"RESULT: {_fails} FAILURE(S)")
    sys.exit(1)
print("RESULT: all tests passed")
