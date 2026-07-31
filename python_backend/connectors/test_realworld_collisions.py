#!/usr/bin/env python3
"""
Real-world adapter-collision audit
================================================================================
The self-route batch tests prove every adapter wins its OWN catalog sample.
They deliberately do NOT prove the inverse — that a COMMON real-world variant
of a generic language/framework logger isn't stolen by a niche adapter that
merely shares its silhouette. That inverse is exactly how BUG "artemis steals
the bracketed python_logging line" shipped:

    2026-07-22 10:00:01,123 INFO [main] starting up      → artemis  (WRONG)

because ActiveMQ Artemis rides the `TS,ms LEVEL [logger] msg` shape and used
to claim it with NO vocabulary of its own.

This suite feeds REALISTIC variants (bracketed loggers, thread names, MDC
fields, differing timestamp precisions, basicConfig, serilog file sink, Spring
Boot 2/3/3.2 …) of the common generic loggers and asserts each routes to the
CORRECT generic adapter. It also pins:
  • the gated niche adapters still win their OWN catalog samples
    (anti-lobotomy for the vocabulary gates), and
  • the intentional catalog owner-changes that fixing the class produced.

Run:  python3 python_backend/connectors/test_realworld_collisions.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
for _p in (_HERE.parents[1], _HERE.parents[2]):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    from connectors import log_adapters as la
except ImportError:
    import log_adapters as la  # type: ignore

_FALLBACKS = {"structural", "raw", "generic_ts"}

_PASS = _FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


# ── Load the format catalog (for the self-route / owner-change pins) ─────────
_CATALOG = {}
for _p in (_HERE.parents[3] / "docs" / "log_catalog_master.json",
           _HERE.parents[2] / "docs" / "log_catalog_master.json",
           Path("docs/log_catalog_master.json")):
    if _p.exists():
        for _e in json.loads(_p.read_text())["catalog"]:
            _CATALOG[_e["name"]] = _e
        break


def catalog_sample(catalog_name: str) -> str:
    e = _CATALOG.get(catalog_name)
    return e["sample_line"] if e else ""


# ══════════════════════════════════════════════════════════════════════════════
# 1. BUG-1 regression pins — the exact mis-routed line + close variants
# ══════════════════════════════════════════════════════════════════════════════
print("1. BUG-1 pins — bracketed python_logging lines are NOT artemis's")
BUG1 = [
    "2026-07-22 10:00:01,123 INFO [main] starting up",
    "2026-07-22 10:00:01,123 DEBUG [worker-3] tick 42",
    "2026-07-22 10:00:01,123 WARN [pool] queue depth 9000",
    "2026-07-22 10:00:01,123 ERROR [app.core] handler failed",
    "2026-07-22 10:00:01,123 INFO [com.acme.svc.Foo] request handled",
    "2026-07-20 11:03:22,145 INFO [train] Using seed 1337 (np, random, torch, cuda)",
]
for ln in BUG1:
    w, conf, scores = la.detect_adapter([ln])
    check(f"python_logging owns {ln[:46]!r}", w.name == "python_logging",
          f"got {w.name} ({conf})")
    check("  …and artemis does not even score it",
          scores.get("artemis", 0.0) == 0.0, f"artemis={scores.get('artemis')}")


# ══════════════════════════════════════════════════════════════════════════════
# 2. Realistic-variant battery — generic loggers win their OWN common variants
#    (each entry: expected adapter, ONE sample = list of lines as read from disk)
# ══════════════════════════════════════════════════════════════════════════════
print("2. realistic-variant battery — correct generic owner wins")
BATTERY: list[tuple[str, list[str]]] = [
    # -- Python logging ---------------------------------------------------------
    ("python_logging", ["2026-07-22 10:00:01,123 - myapp.web - INFO - request handled in 12ms"]),
    ("python_logging", ["2026-07-22 10:00:01,123 INFO myapp.module: connection established"]),
    ("python_logging", ["[2026-07-22 10:00:01] INFO: server started on port 8080"]),
    ("python_logging", ["2026-07-22 10:00:01,123 WARNING [django.request] Bad Request: /api/x"]),
    ("python_logging", ["2026-07-22 10:00:01 INFO [main] no-millis precision variant"]),
    # (a BARE "ISO-T LEVEL msg" line is intentionally absent: that exact shape
    #  is ALSO serilog's console default — two generic owners, dotnet wins by
    #  registry order; neither is a niche thief.)
    ("python_logging", ["2026-07-22T10:00:01.123456 - werkzeug - INFO - 127.0.0.1 \"GET / HTTP/1.1\" 200"]),
    ("python_logging", ["INFO:root:started"]),                       # basicConfig default
    ("python_logging", ["WARNING:app.web:slow request 1.9s"]),       # basicConfig named
    # a realistic sample MIXING normal lines with one traceback block
    ("python_logging", ["2026-07-22 10:00:01,123 INFO [main] starting",
                        "2026-07-22 10:00:02,000 ERROR [main] boom",
                        "2026-07-22 10:00:03,000 INFO [main] recovered",
                        "2026-07-22 10:00:04,000 INFO [main] steady"]),
    # -- Java log4j / logback ---------------------------------------------------
    ("log4j", ["2026-07-22 10:00:01.123 [main] INFO  com.acme.Foo - message body"]),
    ("log4j", ["2026-07-22 10:00:01,123 [http-nio-8080-exec-1] WARN  o.s.web.servlet.PageNotFound - No mapping for GET /x"]),
    ("log4j", ["12:00:00.123 [pool-1-thread-2] ERROR c.a.Svc - boom"]),
    ("log4j", ["2026-07-22T10:00:01,123+0000 [main] INFO org.apache.pulsar.Broker - started"]),
    # -- Spring Boot (2, 3, 3.2 two-bracket) -------------------------------------
    ("springboot", ["2026-07-21 15:40:39.123  INFO 12345 --- [           main] c.e.demo.DemoApplication                 : Started DemoApplication in 2.1 seconds"]),
    ("springboot", ["2026-07-22T10:00:01.123+02:00  INFO 1234 --- [nio-8080-exec-1] c.a.web.Controller     : Handling request"]),
    ("springboot", ["2026-07-22T10:00:01.123Z  WARN 1234 --- [demo] [nio-8080-exec-1] c.a.web.Controller : deprecated call"]),
    # -- .NET serilog (console + FILE sink default) + MEL ------------------------
    ("dotnet", ["[12:00:01 INF] Application started. Press Ctrl+C to shut down."]),
    ("dotnet", ["2026-07-22 10:00:01.123 +00:00 [INF] Now listening on: http://[::]:5000"]),
    ("dotnet", ["2026-07-22 10:00:01.123 -05:00 [ERR] Unhandled exception in pipeline"]),
    # -- jboss-logmanager family (WildFly server.log + console) ------------------
    ("wildfly_console", ["2022-02-22 16:04:09,053 INFO  [org.jboss.as] (Controller Boot Thread) WFLYSRV0025: WildFly Full 26.0.1.Final started"]),
    ("wildfly_console", ["08:10:40,347 INFO  [org.jboss.as] (Controller Boot Thread) WFLYSRV0025: started"]),
    # -- Go ----------------------------------------------------------------------
    ("go_zap", ["2026-07-22T10:00:01.123Z\tINFO\tserver/main.go:42\tstarting http server"]),
    ("logfmt", ['time="2026-07-22T10:00:01Z" level=info msg="started processing" worker=3']),
    # -- Node (pino / winston / bunyan JSON) --------------------------------------
    ("jsonl", ['{"level":30,"time":1753178401123,"pid":312,"hostname":"web1","msg":"server listening"}']),
    ("jsonl", ['{"timestamp":"2026-07-22T10:00:01.123Z","level":"info","message":"winston hello"}']),
    ("jsonl", ['{"name":"app","hostname":"web1","pid":312,"level":30,"msg":"bunyan hello","time":"2026-07-22T10:00:01.123Z","v":0}']),
    # -- Ruby / Rails -------------------------------------------------------------
    ("ruby", ["I, [2026-07-22T10:00:01.123456 #1234]  INFO -- main: started"]),
    ("rails", ['Started GET "/users" for 127.0.0.1 at 2026-07-22 10:00:01 +0000']),
    # -- PHP Monolog / Laravel ----------------------------------------------------
    ("php_monolog", ['[2026-07-22T10:00:01.123456+00:00] app.INFO: User logged in {"uid":42} []']),
    ("php_monolog", ["[2026-07-22 10:00:01] production.ERROR: Undefined index: foo"]),
    # -- structlog console --------------------------------------------------------
    ("structlog_console", ["2026-07-22 10:00:01 [info     ] request served              path=/api status=200"]),
    # -- Python web/task runtimes -------------------------------------------------
    ("celery", ["[2026-07-22 10:00:01,123: INFO/MainProcess] Task tasks.add[c4a2] succeeded in 0.5s"]),
    ("gunicorn_error", ["[2026-07-22 10:00:01 +0000] [1234] [INFO] Booting worker with pid: 1234"]),
    ("dev_access", ['127.0.0.1 - - [22/Jul/2026 10:00:01] "GET /api HTTP/1.1" 200 -']),
]
for want, lines in BATTERY:
    w, conf, scores = la.detect_adapter(lines)
    check(f"{want:16} owns {lines[0][:52]!r}", w.name == want,
          f"got {w.name} ({conf})")

# No NICHE adapter may tie-or-beat the generic owner on any battery sample
# (fallbacks may score whatever they like — they always lose ties by order).
print("3. no niche adapter ties/beats the generic owner on battery samples")
_reg_index = {a.name: i for i, a in enumerate(la.REGISTRY)}
for want, lines in BATTERY:
    _, _, scores = la.detect_adapter(lines)
    own = scores.get(want, 0.0)
    thieves = [n for n, s in scores.items()
               if n != want and n not in _FALLBACKS
               and (s > own or (s == own and s > 0
                                and _reg_index.get(n, 9999) < _reg_index.get(want, 9999)))]
    check(f"no thief on {lines[0][:44]!r}", not thieves,
          f"{thieves} vs {want}={own}")


# ══════════════════════════════════════════════════════════════════════════════
# 4. Anti-lobotomy — every vocabulary-GATED / widened adapter still wins its
#    OWN catalog sample (the gates must not cost anyone their real format)
# ══════════════════════════════════════════════════════════════════════════════
print("4. gated/widened adapters still win their own catalog samples")
SELF = {
    "artemis":       "artemis-server-log",
    "activemq":      "activemq-classic-log",
    "zookeeper":     "zookeeper-log4j",
    "jul_2line":     "jul_simpleformatter_2line",
    "pip_install":   "pip verbose install log",
    "dotnet":        "aspnet_simple_console",
    "springboot":    "spring_boot_default",
    "celery":        "celery-worker-log",
    "gunicorn_error": "gunicorn_error",
}
for want, cat_name in SELF.items():
    sample = catalog_sample(cat_name)
    if not sample:
        check(f"catalog[{cat_name}] present", False, "sample_line missing")
        continue
    w, conf, _ = la.detect_adapter([sample])
    check(f"{cat_name} → {want}", w.name == want, f"got {w.name} ({conf})")

# vocabulary-less shape-riders must now score ZERO on artemis
_alines = ["2026-07-22 10:00:01,123 INFO [main] starting up",
           "2026-07-22 10:00:01,123 ERROR [com.acme.Foo] boom"]
for ln in _alines:
    check(f"artemis scores 0 on {ln[:40]!r}",
          la.get_adapter("artemis").detect([ln]) == 0.0)

# JUL is CAPPED below strict grammar on header-less "LEVEL: msg" samples …
_jul = la.get_adapter("jul_2line")
check("jul_2line capped ≤0.85 without its date header",
      _jul.detect(["INFO: Using random seed 1337"]) <= 0.85)
# … and at full confidence with its distinctive two-line record
check("jul_2line full confidence with header",
      _jul.detect(["Jul 21, 2026 3:40:39 PM com.myco.Service doWork\nINFO: ok"]) == 1.0)

# pip claims ERROR:/WARNING: lines only when its verb lines anchor the sample
_pip = la.get_adapter("pip_install")
check("pip_install scores 0 on bare ERROR:/WARNING: lines",
      _pip.detect(["ERROR: connection refused", "WARNING: low disk"]) == 0.0)
check("pip_install still owns a real pip session",
      _pip.detect(["Collecting requests",
                   "  Using cached requests-2.31.0-py3-none-any.whl (62 kB)",
                   "WARNING: pip is being invoked by an old script wrapper"]) == 1.0)


# ══════════════════════════════════════════════════════════════════════════════
# 5. Intentional owner-changes from closing the class (regression-pinned)
#    These catalog samples used to be claimed by artemis on shape alone.
# ══════════════════════════════════════════════════════════════════════════════
print("5. catalog owner-change pins (ex-artemis shape-riders)")
OWNER_CHANGES = {
    "Python logging seed / reproducibility init": "python_logging",
    "hadoop-daemon-log4j":  "python_logging",   # generic TS,ms LEVEL owner (like flink/hive)
    "gocd-server-log":      "python_logging",
    "hbase-log4j":          "python_logging",
    "wildfly-server-log":   "wildfly_console",  # jboss-logmanager family
    "keycloak-events":      "wildfly_console",
    "quarkus_console":      "wildfly_console",
}
for cat_name, want in OWNER_CHANGES.items():
    sample = catalog_sample(cat_name)
    if not sample:
        check(f"catalog[{cat_name}] present", False, "sample_line missing")
        continue
    w, conf, _ = la.detect_adapter([sample])
    check(f"{cat_name} → {want}", w.name == want, f"got {w.name} ({conf})")


# ══════════════════════════════════════════════════════════════════════════════
# 6. Hostile inputs through every touched adapter — 0 raises
# ══════════════════════════════════════════════════════════════════════════════
print("6. hostile sweep over the touched adapters — 0 raises")
_HOSTILE = ["", "   ", "\t\n", "\x00\x01\xff bin \x7f", "[", "]", "[]", ":::",
            "INFO:", "ERROR:  ", "2026-07-22 10:00:01,123 ", "a" * 5000,
            "2026-07-22 10:00:01,123 INFO [" + "x" * 2000 + "] y",
            "INFO:" + ":" * 500, "\U0001F600 [main] INFO ‮RTL"]
_raises = 0
for name in ("artemis", "jul_2line", "pip_install", "python_logging",
             "dotnet", "springboot", "wildfly_console"):
    a = la.get_adapter(name)
    if a is None:
        check(f"adapter {name} present", False)
        continue
    for h in _HOSTILE:
        try:
            a.detect([h])
            a.parse_line(h)
        except Exception as exc:  # noqa: BLE001
            _raises += 1
            print(f"    RAISE {name} on {h[:30]!r}: {type(exc).__name__}: {exc}")
check("0 raises across touched adapters × hostile inputs", _raises == 0,
      f"{_raises}")


# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'=' * 70}\nreal-world collision audit: {_PASS} passed, {_FAIL} failed")
sys.exit(1 if _FAIL else 0)
