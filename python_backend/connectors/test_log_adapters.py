"""
Unit tests for the universal log-adapter framework (pure stdlib, no pytest req).
Run:  python3 python_backend/connectors/test_log_adapters.py
Exits non-zero on any failure so it can gate CI / the packaging step.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from connectors import log_adapters as la  # noqa: E402

_fails = 0


def check(name: str, cond: bool, detail: str = ""):
    global _fails
    status = "ok  " if cond else "FAIL"
    if not cond:
        _fails += 1
    print(f"  [{status}] {name}{'  — ' + detail if detail and not cond else ''}")


# ── Samples for each ecosystem ──────────────────────────────────────────────────
SAMPLES = {
    "jsonl": [
        '{"ts":"2026-07-20T12:00:00.123Z","level":"info","msg":"boot","logger":"app"}',
        '{"time":1770000000000,"level":50,"msg":"pino error","name":"svc"}',
    ],
    "log4j": [
        "2026-07-20 12:00:00.123 [main] INFO  com.acme.Foo - started up",
        "12:00:01.500 [pool-1-thread-2] ERROR c.a.Service - boom happened",
    ],
    "rust": [
        "[2026-07-20T12:00:00Z INFO  my_crate::net] listening on 8080",
        "[2026-07-20T12:00:01Z ERROR my_crate] connection refused",
    ],
    "dotnet": [
        "[12:00:00 INF] Application starting",
        "2026-07-20 12:00:00.123 +00:00 [ERR] Unhandled exception",
        "info: Microsoft.Hosting.Lifetime[0]  Now listening on http://localhost",
    ],
    "syslog": [
        "<34>1 2026-07-20T12:00:00Z myhost myapp 1234 ID47 - disk full",
        "<13>Jul 20 12:00:00 myhost cron[999]: job finished",
    ],
    "logcat": [
        "07-20 12:00:00.123  1234  1256 I ActivityManager: Start proc",
        "07-20 12:00:01.500  1234  1256 E AndroidRuntime: FATAL EXCEPTION",
    ],
    "go_zap": [
        "2026-07-20T12:00:00.123Z\tINFO\tserver/main.go:42\tlistening\t{\"port\":8080}",
        "2026-07-20T12:00:01.000Z\tERROR\tserver/db.go:10\tquery failed",
    ],
    "logfmt": [
        'level=info ts=2026-07-20T12:00:00Z msg="request handled" method=GET status=200',
        'level=error ts=2026-07-20T12:00:01Z msg="db down" component=store',
    ],
    "python_logging": [
        "2026-07-20 12:00:00,123 INFO app.main: server started",
        "2026-07-20 12:00:01,000 - app.db - ERROR - connection lost",
    ],
    "sharpemu": [
        "[23:44:01.123] [INFO] [Cpu] Interpreter.cs:412 block compiled",
        "[23:44:01.500] [ERROR] [Loader] Elf.cs:88 import unresolved: sceGnmSubmit",
    ],
    "ruby": [
        "I, [2026-07-20T12:00:00.123456 #1234]  INFO -- : GET /users 200",
        "E, [2026-07-20T12:00:01.000000 #1234] ERROR -- app: boom",
    ],
    "php_monolog": [
        "[2026-07-20T12:00:00.123456+00:00] app.INFO: request handled",
        "[2026-07-20 12:00:01] app.ERROR: db connection refused",
    ],
    "elixir": [
        "12:00:00.123 [info] Starting Endpoint",
        "12:00:01.500 [error] GenServer terminating",
    ],
    "access_log": [
        '1.2.3.4 - - [20/Jul/2026:12:00:00 +0000] "GET /x HTTP/1.1" 200 1234 "-" "curl/8"',
        '1.2.3.4 - - [20/Jul/2026:12:00:01 +0000] "POST /y HTTP/1.1" 500 12 "-" "curl/8"',
    ],
    "nginx_error": [
        "2026/07/20 12:00:00 [error] 1234#0: *5 connect() failed upstream",
        "2026/07/20 12:00:01 [warn] 1234#0: *6 slow upstream",
    ],
    "apache_error": [
        "[Mon Jul 20 12:00:00.123456 2026] [core:error] [pid 1234] file not found",
        "[Mon Jul 20 12:00:01.000000 2026] [php:warn] [pid 1234] deprecated call",
    ],
    "systemd": [
        "Jul 20 12:00:00 myhost myservice[1234]: started listener",
        "Jul 20 12:00:01 myhost myservice[1234]: error binding socket",
    ],
    "klog": [
        "I0720 12:00:00.123456  1234 server.go:42] serving on :8080",
        "E0720 12:00:01.000000  1234 db.go:10] query failed",
    ],
    "build": [
        "src/main.c:12:5: error: 'x' undeclared (first use in this function)",
        "src/lib.rs:12:9: error[E0382]: borrow of moved value: `s`",
    ],
    "test": [
        "tests/test_api.py::test_login PASSED",
        "--- FAIL: TestLogin (0.01s)",
    ],
    "database": [
        "2026-07-20 12:00:00.123 UTC [1234] LOG:  statement: SELECT 1",
        "2026-07-20T12:00:01.123456Z 12 [ERROR] [MY-000000] Access denied",
    ],
    "loguru": [
        "2026-07-21 10:00:00.123 | INFO     | app.main:run:42 - server started",
        "2026-07-21 10:00:01.500 | ERROR    | app.db:connect:10 - refused",
    ],
    "nlog": [
        "2026-07-21 10:00:00.1234|INFO|My.App.Service|started",
        "2026-07-21 10:00:01.0000|ERROR|My.App.Db|connection failed",
    ],
    "docker_cri": [
        "2026-07-21T10:00:00.123456789Z stdout F app booting",
        "2026-07-21T10:00:01.000000000Z stderr F panic: boom",
    ],
    "redis": [
        "1234:M 21 Jul 2026 10:00:00.123 * Ready to accept connections",
        "1234:M 21 Jul 2026 10:00:01.000 # WARNING overcommit_memory is 0",
    ],
    "dev_access": [
        '[21/Jul/2026 10:00:00] "GET / HTTP/1.1" 200 1234',
        '127.0.0.1 - - [21/Jul/2026 10:00:01] "POST /x HTTP/1.1" 500 -',
    ],
    "haproxy": [
        "Jul 21 10:00:00 lb haproxy[123]: 1.2.3.4:5 [21/Jul/2026:10:00:00.1] fe be/srv 0/0/0/1/1 200 1234 - - ----",
        "Jul 21 10:00:01 lb haproxy[123]: 1.2.3.4:6 [21/Jul/2026:10:00:01.0] fe be/srv 0/0/0/2/2 503 99 - - ----",
    ],
    "windows_event": [
        "<Event xmlns='x'><System><Provider Name='MyApp'/><EventID>1000</EventID><Level>2</Level><TimeCreated SystemTime='2026-07-21T10:00:00.000Z'/></System><EventData><Data>disk error</Data></EventData></Event>",
    ],
    "envoy": [
        '[2026-07-21T10:00:00.000Z] "GET /api HTTP/1.1" 200 - 0 512 3 2 "-" "curl/8" "req-1" "svc" "10.0.0.1:8080"',
        '[2026-07-21T10:00:01.000Z] "POST /x HTTP/2" 503 UF 0 0 10 - "-" "-" "req-2" "svc" "10.0.0.2:8080"',
    ],
    "kafka": [
        "[2026-07-21 10:00:00,123] INFO [KafkaServer id=1] started (kafka.server.KafkaServer)",
        "[2026-07-21 10:00:01,000] ERROR [ReplicaManager broker=1] failed (kafka.server.ReplicaManager)",
    ],
    "auditd": [
        "type=SYSCALL msg=audit(1712345678.123:456): arch=c000003e syscall=59 success=yes exe=\"/bin/ls\"",
        "type=USER_LOGIN msg=audit(1712345679.000:457): success=no acct=root terminal=ssh",
    ],
    "ci": [
        "::error file=app.py,line=10::Something broke",
        "::warning::deprecated call",
    ],
    "cassandra": [
        "INFO  [main] 2026-07-21 10:00:00,123 StorageService.java:1234 - Starting up",
        "ERROR [CompactionExecutor] 2026-07-21 10:00:01,000 CassandraDaemon.java:55 - compaction failed",
    ],
    "springboot": [
        "2026-07-21 10:00:00.123  INFO 12345 --- [           main] c.e.MyApp : Started MyApp in 3.2s",
        "2026-07-21 10:00:01.000 ERROR 12345 --- [http-nio-8080] c.e.Ctrl : request failed",
    ],
    "rabbitmq": [
        "2026-07-21 10:00:00.123 [info] <0.123.0> Server startup complete",
        "2026-07-21 10:00:01.000 [error] <0.456.0> connection dropped",
    ],
    "hashicorp": [
        "2026-07-21T10:00:00.000Z [INFO]  agent: started: version=1.15",
        "2026-07-21T10:00:01.000Z [ERROR] core: seal barrier failed: err=locked",
    ],
    "rails": [
        'Started GET "/users" for 127.0.0.1 at 2026-07-21 10:00:00 +0000',
        "Completed 500 Internal Server Error in 34ms (Views: 20ms | ActiveRecord: 5ms)",
    ],
    "sqlite": [
        'Error: near "SELCT": syntax error',
        "Runtime error: no such table: users",
    ],
    "sanitizer": [
        "==12345==ERROR: AddressSanitizer: heap-use-after-free on address 0x602000000010",
        "==12345== Invalid read of size 4",
    ],
}

# JSON ecosystems that all normalize through the ONE jsonl adapter.
JSON_ECOSYSTEMS = {
    "bunyan":   ('{"time":"2026-07-21T10:00:00.000Z","level":50,"msg":"boom","name":"svc"}', "ERROR", "boom"),
    "mongo":    ('{"t":{"$date":"2026-07-21T10:00:00.000Z"},"s":"E","c":"NETWORK","id":22,"msg":"conn refused"}', "ERROR", "conn refused"),
    "gelf":     ('{"version":"1.1","host":"h","short_message":"disk full","level":3,"timestamp":1784628000.5}', "ERROR", "disk full"),
    "journald": ('{"__REALTIME_TIMESTAMP":"1784628000000000","PRIORITY":"3","MESSAGE":"unit failed","SYSLOG_IDENTIFIER":"sshd"}', "ERROR", "unit failed"),
    "structlog":('{"timestamp":"2026-07-21T10:00:00Z","level":"warning","event":"cache miss","logger":"cache"}', "WARN", "cache miss"),
}


def test_detection():
    print("detection:")
    for expected, lines in SAMPLES.items():
        adapter, conf, scores = la.detect_adapter(lines)
        check(f"{expected} detected (got {adapter.name}, conf={conf})",
              adapter.name == expected, f"scores={scores}")


def test_parse_fields():
    print("parse → unified schema:")
    # log4j
    ev = la.get_adapter("log4j").parse_line(
        "2026-07-20 12:00:00.123 [main] ERROR com.acme.Foo - kaboom")
    check("log4j level→ERROR", ev["level"] == "ERROR")
    check("log4j category→error", ev["category"] == "error")
    check("log4j source=logger", ev["source"] == "com.acme.Foo")
    check("log4j message", ev["data"]["message"] == "kaboom")
    check("log4j ts_ms parsed", isinstance(ev["ts_ms"], float) and ev["ts_ms"] > 1e12)
    check("log4j thread field", ev["data"].get("thread") == "main")

    # rust ERROR maps to category error
    ev = la.get_adapter("rust").parse_line("[2026-07-20T12:00:01Z ERROR my_crate] boom")
    check("rust error category", ev["category"] == "error" and ev["level"] == "ERROR")

    # dotnet serilog short level WRN→WARN
    ev = la.get_adapter("dotnet").parse_line("[12:00:00 WRN] disk low")
    check("dotnet WRN→WARN", ev["level"] == "WARN")

    # M.E.Logging fail→error
    ev = la.get_adapter("dotnet").parse_line("fail: My.Cat[7] request failed")
    check("dotnet fail→error", ev is not None and ev["level"] == "ERROR"
          and ev["data"].get("event_id") == 7)

    # syslog severity 2 (crit) → FATAL, facility computed
    ev = la.get_adapter("syslog").parse_line(
        "<34>1 2026-07-20T12:00:00Z h app 1 ID - hard down")
    check("syslog sev→FATAL", ev["level"] == "FATAL")

    # jsonl numeric pino level 50 → error
    ev = la.get_adapter("jsonl").parse_line(
        '{"time":1770000000000,"level":50,"msg":"x","name":"svc"}')
    check("pino 50→ERROR", ev["level"] == "ERROR" and ev["category"] == "error")
    check("pino source", ev["source"] == "svc")

    # logfmt structured extras retained
    ev = la.get_adapter("logfmt").parse_line(
        'level=info ts=2026-07-20T12:00:00Z msg="ok" method=GET status=200')
    check("logfmt keeps extras", ev["data"].get("method") == "GET"
          and ev["data"].get("status") == "200")


def test_json_ecosystems():
    print("JSON ecosystems → normalized via the jsonl adapter:")
    for name, (line, exp_level, exp_msg) in JSON_ECOSYSTEMS.items():
        ev = la.get_adapter("jsonl").parse_line(line)
        ok = ev is not None and ev.get("level") == exp_level \
            and (ev["data"].get("message") == exp_msg)
        check(f"{name}: level={exp_level}, msg mapped", ok,
              f"got level={ev and ev.get('level')} msg={ev and ev['data'].get('message')}")
        # every JSON ecosystem line must detect AS jsonl (no collision)
        adapter, _c, _s = la.detect_adapter([line])
        check(f"{name}: detected as jsonl", adapter.name == "jsonl")
    # timestamp coercion sanity ($date + microsecond epoch)
    ev = la.get_adapter("jsonl").parse_line(JSON_ECOSYSTEMS["mongo"][0])
    check("mongo $date → ts_ms", isinstance(ev.get("ts_ms"), float) and ev["ts_ms"] > 1e12)
    ev = la.get_adapter("jsonl").parse_line(JSON_ECOSYSTEMS["journald"][0])
    check("journald microsecond epoch → ts_ms",
          isinstance(ev.get("ts_ms"), float) and ev["ts_ms"] > 1e12)


def test_new_adapters_parse():
    print("long-tail adapters → fields:")
    ev = la.get_adapter("loguru").parse_line(
        "2026-07-21 10:00:01.500 | ERROR    | app.db:connect:10 - refused")
    check("loguru error", ev["level"] == "ERROR" and ev["source"] == "app.db:connect:10")
    ev = la.get_adapter("nlog").parse_line("2026-07-21 10:00:01.0|ERROR|My.Db|failed")
    check("nlog error", ev["level"] == "ERROR" and ev["source"] == "My.Db")
    ev = la.get_adapter("docker_cri").parse_line("2026-07-21T10:00:01.0Z stderr F panic")
    check("docker_cri stream", ev["data"]["stream"] == "stderr" and ev["data"]["message"] == "panic")
    ev = la.get_adapter("redis").parse_line("1 M 21 Jul 2026 10:00:01.0 # warn msg".replace("1 M", "1:M"))
    check("redis warn sym", ev["level"] == "WARN")
    ev = la.get_adapter("windows_event").parse_line(SAMPLES["windows_event"][0])
    check("windows_event error", ev["level"] == "ERROR" and ev["data"]["event_id"] == 1000)
    ev = la.get_adapter("sharpemu").parse_line(
        "[23:44:01.500] [ERROR] [Loader] Elf.cs:88 unresolved import: sceGnmSubmit")
    check("sharpemu level", ev["level"] == "ERROR" and ev["category"] == "error")
    check("sharpemu category tag", ev["source"] == "Loader")
    check("sharpemu source_file", ev["data"].get("source_file") == "Elf.cs:88")
    check("sharpemu message", ev["data"]["message"] == "unresolved import: sceGnmSubmit")

    ev = la.get_adapter("access_log").parse_line(
        '1.2.3.4 - - [20/Jul/2026:12:00:01 +0000] "POST /y HTTP/1.1" 500 12 "-" "curl/8"')
    check("access 500→error", ev["level"] == "ERROR" and ev["data"]["status"] == 500)

    ev = la.get_adapter("build").parse_line(
        "src/main.c:12:5: error: 'x' undeclared (first use in this function)")
    check("build error+line", ev["level"] == "ERROR" and ev["data"]["line"] == 12)

    # Envoy access log → status-derived level + fields.
    ev = la.get_adapter("envoy").parse_line(SAMPLES["envoy"][1])
    check("envoy 503→error", ev["level"] == "ERROR" and ev["data"]["status"] == 503)
    check("envoy response_flags", ev["data"]["response_flags"] == "UF", str(ev["data"]))

    # Kafka → level + logger + context.
    ev = la.get_adapter("kafka").parse_line(SAMPLES["kafka"][1])
    check("kafka error+logger", ev["level"] == "ERROR"
          and ev["source"] == "kafka.server.ReplicaManager", str(ev))
    check("kafka context field", ev["data"].get("context") == "ReplicaManager broker=1", str(ev["data"]))
    # Collision guard: a plain Python-logging line must NOT be grabbed by kafka.
    adapter, _c, _s = la.detect_adapter(["2026-07-21 10:00:00,123 INFO app.main: started"])
    check("kafka doesn't steal python_logging", adapter.name == "python_logging", adapter.name)

    # ── Long-tail adapters (this pass) ───────────────────────────────────────
    ev = la.get_adapter("auditd").parse_line(SAMPLES["auditd"][1])
    check("auditd login fail → warn", ev["level"] == "WARN" and ev["data"]["success"] == "no", str(ev))
    ev = la.get_adapter("ci").parse_line(SAMPLES["ci"][0])
    check("ci ::error:: → error + params", ev["level"] == "ERROR"
          and ev["data"].get("file") == "app.py" and ev["data"].get("line") == "10", str(ev))
    ev = la.get_adapter("cassandra").parse_line(SAMPLES["cassandra"][1])
    check("cassandra error + src", ev["level"] == "ERROR" and ev["source"] == "CassandraDaemon.java", str(ev))
    ev = la.get_adapter("springboot").parse_line(SAMPLES["springboot"][1])
    check("springboot error + logger", ev["level"] == "ERROR" and ev["source"] == "c.e.Ctrl", str(ev))
    ev = la.get_adapter("rabbitmq").parse_line(SAMPLES["rabbitmq"][1])
    check("rabbitmq error + pid", ev["level"] == "ERROR" and ev["data"]["erlang_pid"] == "<0.456.0>", str(ev))
    ev = la.get_adapter("hashicorp").parse_line(SAMPLES["hashicorp"][1])
    check("hashicorp error + comp", ev["level"] == "ERROR" and ev["source"] == "core", str(ev))
    ev = la.get_adapter("rails").parse_line(SAMPLES["rails"][1])
    check("rails 500 → error", ev["level"] == "ERROR" and ev["data"]["status"] == 500, str(ev))
    ev = la.get_adapter("sqlite").parse_line(SAMPLES["sqlite"][1])
    check("sqlite error", ev["level"] == "ERROR" and "no such table" in ev["data"]["message"], str(ev))
    ev = la.get_adapter("sanitizer").parse_line(SAMPLES["sanitizer"][0])
    check("asan error", ev["level"] == "ERROR" and "use-after-free" in ev["data"]["message"], str(ev))

    # Collision guards for the new adapters.
    for name in ("auditd", "ci", "cassandra", "springboot", "rabbitmq", "hashicorp",
                 "rails", "sqlite", "sanitizer"):
        adapter, conf, _ = la.detect_adapter(SAMPLES[name])
        check(f"{name} wins detection (no collision)", adapter.name == name, f"got {adapter.name}")

    ev = la.get_adapter("build").parse_line(
        r"src\main.cpp(12): error C2065: 'x': undeclared identifier")
    check("build msvc code", ev is not None and ev["data"].get("code") == "C2065")

    ev = la.get_adapter("test").parse_line("--- FAIL: TestLogin (0.01s)")
    check("test FAIL→error+category", ev["level"] == "ERROR" and ev["category"] == "test")

    ev = la.get_adapter("database").parse_line(
        "2026-07-20T12:00:01.123456Z 12 [ERROR] [MY-000000] Access denied")
    check("mysql error", ev["level"] == "ERROR" and ev["source"] == "mysql")

    ev = la.get_adapter("klog").parse_line("E0720 12:00:01.000000  1234 db.go:10] query failed")
    check("klog E→error", ev["level"] == "ERROR" and ev["source"] == "db.go:10")


def test_local_time_alignment():
    print("local-time alignment (same-machine correlation):")
    # A bare wall-clock ("HH:MM:SS") and the SAME instant as an epoch-ms value
    # must map to the same ms, regardless of the machine's timezone.
    import time, datetime
    now = time.time()
    wall = datetime.datetime.fromtimestamp(now).strftime("%H:%M:%S.%f")[:12]
    got = la.parse_timestamp(wall)
    check("bare wall-clock ≈ epoch now (local)",
          got is not None and abs(got - now * 1000.0) < 2000.0,
          f"got={got} now_ms={now*1000:.0f}")
    # ISO with Z is honored as UTC (not local), independent of the box's tz.
    expect_utc = datetime.datetime(2026, 7, 20, 0, 0, 0,
                                   tzinfo=datetime.timezone.utc).timestamp() * 1000.0
    z = la.parse_timestamp("2026-07-20T00:00:00Z")
    check("ISO Z parsed as UTC", z == expect_utc, f"got={z} expect={expect_utc}")


def test_native_passthrough():
    print("AgentVision-native JSONL passthrough:")
    native = ('{"ts":"2026-07-20T12:00:00.000Z","ts_ms":1.77e12,'
              '"category":"event","source":"sharpemu.pad","data":{"name":"PAD"}}')
    ev = la.get_adapter("jsonl").parse_line(native)
    check("native category preserved", ev["category"] == "event")
    check("native source preserved", ev["source"] == "sharpemu.pad")
    check("native data preserved", ev["data"].get("name") == "PAD")

    # A native record with category=error but NO level field must still be
    # level-filterable (level derived from category) — regression guard.
    nerr = ('{"ts":"2026-07-20T12:00:00.400Z","ts_ms":1.77e12,'
            '"category":"error","source":"sharpemu.fault","data":{"name":"run.fail"}}')
    ev = la.get_adapter("jsonl").parse_line(nerr)
    check("native error → level ERROR", ev["level"] == "ERROR")


def test_trace_id():
    print("trace-id extraction:")
    ev = la.get_adapter("raw").parse_line("request done trace_id=abc123def op=login")
    check("raw trace_id found", ev["trace_id"] == "abc123def")


def test_raw_error_promotion():
    print("raw error promotion:")
    ev = la.get_adapter("raw").parse_line(
        "Traceback (most recent call last):")
    check("raw traceback→error", ev["category"] == "error")
    ev = la.get_adapter("raw").parse_line("just a normal line")
    check("raw normal→log", ev["category"] == "log")


def test_timestamps():
    print("timestamp parsing:")
    checks = {
        "2026-07-20T12:00:00.123Z": True,
        "2026-07-20 12:00:00,123": True,
        "2026/07/20 12:00:00": True,      # go std
        "Jul 20 12:00:00": True,          # syslog 3164
        "12:00:00.123": True,             # bare wall clock
        "not a timestamp": False,
    }
    for s, should in checks.items():
        got = la.parse_timestamp(s)
        check(f"ts {s!r}", (got is not None) == should, f"got={got}")


def test_never_raises_and_always_returns():
    print("robustness:")
    weird = ["", "   ", "\x00\x01garbage", "{not json", "level=", "<999>x"]
    for w in weird:
        try:
            ev = la.parse_line(w)  # auto-detect path
            check(f"parse_line({w!r}) no raise", True)
        except Exception as e:
            check(f"parse_line({w!r}) no raise", False, str(e))


def test_registry_extension():
    print("registry extension:")

    class MyAdapter(la.LogAdapter):
        name = "my_custom"
        language = "custom"

        def detect(self, lines):
            return 1.0 if any(l.startswith("CUSTOM|") for l in lines) else 0.0

        def parse_line(self, line):
            if line.startswith("CUSTOM|"):
                return self._event(level="info", message=line[7:], source="custom")
            return None

    la.register_adapter(MyAdapter(), front=True)
    adapter, conf, _ = la.detect_adapter(["CUSTOM|hello world"])
    check("custom adapter wins", adapter.name == "my_custom")
    check("raw still last", la.REGISTRY[-1].name == "raw")


if __name__ == "__main__":
    print("=" * 70)
    print("log_adapters test suite")
    print("=" * 70)
    test_detection()
    test_parse_fields()
    test_json_ecosystems()
    test_new_adapters_parse()
    test_local_time_alignment()
    test_native_passthrough()
    test_trace_id()
    test_raw_error_promotion()
    test_timestamps()
    test_never_raises_and_always_returns()
    test_registry_extension()
    print("=" * 70)
    if _fails:
        print(f"RESULT: {_fails} FAILURE(S)")
        sys.exit(1)
    print("RESULT: all tests passed")
    sys.exit(0)
