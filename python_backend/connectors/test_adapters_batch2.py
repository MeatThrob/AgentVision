"""
Tests for the 3 batch-1 GAP FIXES + the BATCH-2 family adapters.

Pure stdlib (no pytest). Run:
    python3 python_backend/connectors/test_adapters_batch2.py
Exits non-zero on any failure so it gates CI / packaging.

The hard rule this file enforces (this is exactly how batch-1's gaps slipped
through): for EVERY adapter fixed or added, its OWN sample — pulled from
docs/log_catalog_master.json where a real sample exists, else an inline real
line — must resolve through detect_adapter([sample]) to THAT adapter, i.e. it
must beat the structural/generic_ts/raw fallbacks AND any colliding named
adapter. A resolution to a fallback (or the wrong adapter) is a FAIL.

Plus a full cross-set collision sweep: no fallback ever wins a real named
sample, and the registry tail stays exactly [structural, raw].
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


# ── Load the format catalog (for pulling real sample_lines) ──────────────────
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


# ── 1. The three GAP FIXES (must route on their OWN catalog sample_line) ──────
# orbis_fatal_trap + zeek_tsv route to a same-named adapter; the Suricata
# fast-alert catalog format (catalog name `suricata_fast_log`) is served by the
# canonical adapter `suricata_fast` — a single adapter covers both Suricata and
# Snort fast-alert grammar (per the catalog note), disambiguated by the 4-digit
# year that Suricata carries and Snort does not.
GAP_FIXES = {
    "orbis_fatal_trap":  ("orbis_fatal_trap", "orbis_fatal_trap"),
    "zeek_tsv":          ("zeek_tsv",          "zeek_tsv"),
    "suricata_fast_log": ("suricata_fast_log", "suricata_fast"),
}


# ── 2. BATCH-2 adapters: adapter_name → (catalog_name | None, inline_sample) ─
# When a real, single-record catalog sample_line exists it is used (pulled at
# runtime); otherwise an inline real sample is provided. Multi-line record
# formats keep the catalog's multi-line block as ONE element on purpose.
INLINE = {
    # runtime / debugger / CI formats whose catalog entry is a template, has a
    # differently-shaped name, or does not exist as a dedicated catalog format.
    "uwsgi": ("[pid: 1234|app: 0|req: 1/1] 1.2.3.4 (bob) {40 vars in 1200 bytes} "
              "[Mon Jul 21 15:40:39 2026] GET /uri => generated 1234 bytes in 5 msecs "
              "(HTTP/1.1 200)"),
    "ros": "[INFO] [1620000000.123456789] [my_node]: My message",
    "gdb_backtrace": "#0  0x00007ffff7a3d in raise () at signals.c:42",
    "junit_xml": '<testcase name="test_add" classname="tests.MathTest" time="0.001"/>',
    "w3c_access": "#Fields: date time s-ip cs-method cs-uri-stem sc-status",
}

# adapter_name → catalog_name (real sample pulled from the catalog)
FROM_CATALOG = {
    "mssql_errorlog":       "mssql-errorlog",
    "oracle_alert":         "oracle-alert-log-text",
    "db2_diag":             "db2-db2diag",
    "clickhouse":           "clickhouse-server-log",
    "cockroachdb":          "cockroachdb-crdb-v2",
    "scylladb":             "scylladb-seastar-log",
    "aerospike":            "aerospike-log",
    "elastic_stack":        None,  # inline below (logstash-plain grammar)
    "couchbase_memcached":  "couchbase-memcached-log",
    "tomcat_catalina":      "tomcat-juli-catalina",
    "gunicorn_error":       "gunicorn_error",
    "express_morgan_dev":   "express_morgan_dev",
    "glassfish_odl":        "glassfish-odl",
    "weblogic":             "weblogic-server-log",
    "aws_alb":              "aws-alb-access-log",
    "aws_s3_access":        "aws-s3-server-access-log",
    "aws_lambda_text":      "aws-lambda-platform-text",
    "cloud_init":           "cloud-init-log",
    "etcd_capnslog":        "etcd-capnslog-legacy",
    "gcf_text":             "gcf-execution-text",
    "jul_2line":            "jul_simpleformatter_2line",
    "celery":               "celery-worker-log",
    "jenkins":              "jenkins-pipeline-console",
    "ansible":              "ansible-playbook-stdout",
    "azure_devops":         "azure-devops-pipeline-log",
    "zookeeper":            "zookeeper-log4j",
    "nats":                 "nats-server-log",
    "mosquitto":            "mosquitto-broker-log",
    "emqx":                 "emqx-text-log",
    "activemq":             "activemq-classic-log",
    "artemis":              "artemis-server-log",
    "android_crash":        "android_crash_fatal",
    "android_anr":          "android_anr_trace",
}

INLINE_EXTRA = {
    "elastic_stack": "[2023-01-01T00:00:00,123][INFO ][logstash.agent           ] "
                     "Successfully started Logstash API endpoint {:port=>9600}",
}


def resolve_sample(adapter: str) -> str:
    if adapter in INLINE:
        return INLINE[adapter]
    cn = FROM_CATALOG.get(adapter)
    if cn:
        s = catalog_sample(cn)
        if s:
            return s
    return INLINE_EXTRA.get(adapter, "")


# ── Run the self-routing assertions ──────────────────────────────────────────
print("GAP FIXES — each routes on its OWN catalog sample_line (was structural):")
_routed = 0
for cat_name, (pull, want_adapter) in GAP_FIXES.items():
    sample = catalog_sample(pull)
    check(f"catalog[{cat_name}] present", bool(sample))
    if not sample:
        continue
    winner, conf, _ = la.detect_adapter([sample])
    ok = winner.name == want_adapter
    if ok:
        _routed += 1
    check(f"{cat_name} → {want_adapter}", ok,
          f"got {winner.name} (conf={conf})")
    check(f"{cat_name} not a fallback", winner.name not in _FALLBACKS,
          f"resolved to fallback {winner.name}")

print("\nBATCH-2 — every added adapter routes its own sample to itself:")
BATCH2 = list(FROM_CATALOG) + [a for a in INLINE if a not in FROM_CATALOG]
for adapter in BATCH2:
    sample = resolve_sample(adapter)
    check(f"sample for {adapter} present", bool(sample))
    if not sample:
        continue
    winner, conf, scores = la.detect_adapter([sample])
    ok = winner.name == adapter
    if ok:
        _routed += 1
    check(f"{adapter} → itself", ok, f"got {winner.name} (conf={conf})")
    check(f"{adapter} beats fallbacks", winner.name not in _FALLBACKS,
          f"resolved to fallback {winner.name}")

print(f"\n  >>> {_routed} adapters routed their own sample to themselves "
      f"(3 gaps + {len(BATCH2)} batch-2).")


# ── 3. parse_line sanity + correct level/category on a few error paths ───────
print("\nparse_line schema + error-category mapping:")


def _ev(adapter: str):
    a = la.get_adapter(adapter)
    return a.parse_line(resolve_sample(adapter) if adapter in ("ros", "uwsgi",
                        "gdb_backtrace", "junit_xml", "w3c_access", "elastic_stack")
                        else catalog_sample(FROM_CATALOG.get(adapter, "")) or resolve_sample(adapter))


for adapter in BATCH2:
    a = la.get_adapter(adapter)
    check(f"{adapter} registered", a is not None)

# error/fatal formats must land in an error category so bookmarks fire.
e = la.get_adapter("clickhouse").parse_line(
    "2019.01.11 15:23:25.549505 [ 45 ] {} <Error> Dict: boom")
check("clickhouse <Error> → error", e and e["category"] == "error", str(e and e["category"]))

e = la.get_adapter("oracle_alert").parse_line("ORA-00604: recursive SQL error")
check("oracle ORA- → error", e and e["category"] == "error", str(e and e["category"]))

e = la.get_adapter("gdb_backtrace").parse_line("#0  0x00007ff in raise () at s.c:42")
check("gdb frame → error", e and e["category"] == "error", str(e and e["category"]))

e = la.get_adapter("android_crash").parse_line("E/AndroidRuntime( 915): FATAL EXCEPTION: main")
check("android FATAL → error", e and e["category"] == "error", str(e and e["category"]))

e = la.get_adapter("jenkins").parse_line("Finished: FAILURE")
check("jenkins FAILURE → error", e and e["category"] == "error", str(e and e["category"]))

e = la.get_adapter("junit_xml").parse_line(
    '<testcase name="t" classname="C" time="0.1"><failure>x</failure></testcase>')
check("junit failure → error", e and e["category"] == "error", str(e and e["category"]))

e = la.get_adapter("aws_alb").parse_line(
    'https 2018-07-02T22:23:00.186641Z app/lb/x 1.2.3.4:1 5.6.7.8:80 0.1 0.1 0.1 '
    '503 503 0 57 "GET https://x/ HTTP/1.1" "ua" - -')
check("aws_alb 503 → error", e and e["category"] == "error", str(e and e["category"]))


# ── 4. Full cross-set collision sweep ────────────────────────────────────────
print("\nCross-set collision sweep (no fallback wins any real named sample):")
ALL_SAMPLES = {}
for cat_name, (pull, want) in GAP_FIXES.items():
    ALL_SAMPLES[want] = catalog_sample(pull)
for adapter in BATCH2:
    ALL_SAMPLES[adapter] = resolve_sample(adapter)

fallback_wins = 0
for adapter, sample in ALL_SAMPLES.items():
    if not sample:
        continue
    winner, conf, _ = la.detect_adapter([sample])
    if winner.name in _FALLBACKS:
        fallback_wins += 1
        print(f"    [FALLBACK] {adapter} sample → {winner.name}")
check("no fallback wins any batch-2 / gap sample", fallback_wins == 0,
      f"{fallback_wins} fallback wins")

# tail invariant
tail = [a.name for a in la.REGISTRY[-2:]]
check("registry tail is exactly [structural, raw]", tail == ["structural", "raw"],
      str(tail))

# uniqueness of adapter names
names = [a.name for a in la.REGISTRY]
check("no duplicate adapter names", len(names) == len(set(names)),
      f"{len(names)} entries, {len(set(names))} unique")

print(f"\n  registry size: {len(la.REGISTRY)} adapters "
      f"({len(names) - 2} named + structural + raw)")

# ── Result ────────────────────────────────────────────────────────────────────
print("=" * 70)
if _fails:
    print(f"RESULT: {_fails} FAILURE(S)")
    sys.exit(1)
print("RESULT: all tests passed")
