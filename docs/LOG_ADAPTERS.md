# AgentVision Log-Adapter Registry

AgentVision is a **two-sided bridge**:

- **INPUT side** (built in): it *reads* a program's logs — in any format, any
  language — and normalizes every line into one unified JSON event schema.
- **OUTPUT side** (auto-installed on first attach): it makes the target program
  *write* those logs, by scaffolding an `agentvision/` folder inside the project
  with the sink files and a per-language emitter. See `python_backend/emitters.py`.

This doc is the registry for the INPUT side. The parser framework lives in
`python_backend/connectors/log_adapters.py`; multi-log merging in
`python_backend/connectors/log_sources.py`.

## The unified event schema

Every adapter turns one raw log line into exactly this shape (a superset of the
records AgentVision's own emitters write), so all downstream code — fingerprints,
bookmarks, outliers, traces, the MCP tools, and the time-aligned frame↔log
correlation — works identically regardless of source language:

```json
{
  "ts": "2026-07-21T10:00:00.123Z",
  "ts_ms": 1784628000123.0,
  "category": "log|debug|warn|error|event|...",
  "level": "TRACE|DEBUG|INFO|WARN|ERROR|FATAL",
  "source": "logger / subsystem / adapter",
  "trace_id": "abc123 | null",
  "frame_seq": null,
  "data": { "message": "...", "adapter": "<name>", "...structured fields": "..." },
  "raw": "<original line>"
}
```

`category` is derived from `level` so an ERROR from **any** ecosystem trips the
bridge's failure detection. Timestamps without a timezone are read as **local**
machine time (same-machine correlation); `Z`/offset are honored as written.

## Detection

Each adapter implements `detect(sample_lines) -> confidence [0..1]` and
`parse_line(line) -> event | None`. `detect_adapter()` scores every adapter on a
sample and picks the highest; `raw` is the guaranteed floor, so detection never
fails. Auto-detection runs per source on attach and on `/log/sources`.

## Shipped adapters (41 named) + JSON coverage

| Adapter | Language / source | Example |
|---|---|---|
| `jsonl` | **all JSON loggers** (see below) | `{"ts":..,"level":"info","msg":".."}` |
| `envoy` | Envoy/proxy access log | `[2026-..Z] "GET /x HTTP/1.1" 200 - ..` |
| `kafka` | Kafka/ZooKeeper server log | `[2026-.. ,000] INFO [Ctx] msg (logger)` |
| `auditd` | Linux audit daemon | `type=SYSCALL msg=audit(1712..:456): success=yes ..` |
| `ci` | GitHub Actions workflow cmds | `::error file=app.py,line=10::broke` |
| `cassandra` | Cassandra (logback) | `ERROR [thread] 2026-.. Foo.java:55 - msg` |
| `springboot` | Spring Boot console | `2026-.. INFO 123 --- [main] c.e.App : msg` |
| `rabbitmq` | RabbitMQ / Erlang | `2026-.. [error] <0.1.0> msg` |
| `hashicorp` | Consul / Vault | `2026-..Z [ERROR] core: msg: k=v` |
| `rails` | Rails request log | `Completed 500 … in 34ms` / `Started GET "/x"` |
| `sqlite` | SQLite errors | `Error: no such table: users` |
| `sanitizer` | ASan/LSan/TSan/UBSan + valgrind | `==123==ERROR: AddressSanitizer: heap-use-after-free` |
| `windows_event` | Windows Event Log (rendered XML) | `<Event>…<Level>2</Level>…</Event>` |
| `log4j` | Java Log4j / Logback / SLF4J | `2026-.. [main] ERROR c.a.Foo - msg` |
| `rust` | Rust env_logger / tracing | `[2026-..Z ERROR my_crate] msg` |
| `dotnet` | .NET Serilog + M.E.Logging | `[12:00:00 ERR] msg` / `fail: Cat[0]` |
| `nlog` | .NET NLog (pipe layout) | `2026-.. 10:00:00.1\|ERROR\|Ns.Cls\|msg` |
| `sharpemu` | SharpEmu FileLogSink | `[10:00:00.1] [ERROR] [Cpu] F.cs:12 msg` |
| `loguru` | Python loguru | `2026-.. \| ERROR \| mod:fn:12 - msg` |
| `docker_cri` | containerd/CRI | `2026-..Z stderr F msg` |
| `syslog` | RFC5424 + RFC3164 | `<34>1 2026-.. host app ..` |
| `haproxy` | HAProxy HTTP log | `Jul 21 .. haproxy[1]: .. 200 1234 ..` |
| `systemd` | journald short | `Jul 21 10:00:00 host unit[1]: msg` |
| `logcat` | Android logcat | `07-21 10:00:00.1 1 2 E Tag: msg` |
| `klog` | Kubernetes klog/glog | `E0721 10:00:00.1 1 f.go:12] msg` |
| `go_zap` | Go zap (console) | `2026-..Z\tERROR\tf.go:1\tmsg` |
| `ruby` | Ruby Logger / Rails | `E, [2026-..T.. #1] ERROR -- : msg` |
| `php_monolog` | PHP Monolog | `[2026-..] app.ERROR: msg` |
| `elixir` | Elixir Logger | `10:00:00.1 [error] msg` |
| `redis` | Redis server | `1:M 21 Jul 2026 10:00:00.1 # msg` |
| `access_log` | nginx/Apache access (CLF) | `1.2.3.4 - - [21/Jul/2026:..] ".." 200 ..` |
| `dev_access` | Django/werkzeug dev server | `[21/Jul/2026 ..] "GET / .." 200 12` |
| `nginx_error` | nginx error log | `2026/07/21 10:00:00 [error] 1#0: msg` |
| `apache_error` | Apache error log | `[Mon Jul 21 .. 2026] [core:error] msg` |
| `database` | PostgreSQL + MySQL | `2026-.. UTC [1] LOG:  msg` |
| `build` | gcc/clang/rustc/MSVC | `main.c:12:5: error: msg` |
| `test` | pytest / go test / cargo | `--- FAIL: TestX (0.0s)` |
| `logfmt` | Heroku / logrus / services | `level=error ts=.. msg="x" k=v` |
| `python_logging` | stdlib `logging` | `2026-.. ERROR app: msg` |
| `generic_ts` | any timestamped text (fallback) | `2026-.. something happened` |
| `raw` | unstructured text (floor) | `anything at all` |

### One `jsonl` adapter → every JSON logger

Rather than one adapter per JSON logger, the `jsonl` adapter normalizes them all
via field aliases, numeric-level scales (pino/bunyan 10–60 **and** syslog/GELF
0–7), and timestamp coercion (epoch s/ms/µs/ns, ISO strings, Mongo `{"$date":…}`,
journald `__REALTIME_TIMESTAMP`). Verified for: **pino, winston, bunyan, roarr,
structlog, loguru-json, zap-json, slog-json, logrus-json, logback-json,
log4j2-json, Serilog-json, NLog-json, GELF, MongoDB, journald `-o json`, Docker
json-file, Caddy, Envoy access-json, OTLP-json**, and AgentVision's own records.

## Multiple logs per program

A profile lists N `log_sources` (`{path, adapter, label}`, `adapter:"auto"` =
detect). `log_sources.read_normalized()` reads them all, normalizes each, and
merges onto the single UTC-ms timeline (`av_log_normalized`). `agentvision run`
also normalizes stdout/stderr of any launched program through these adapters.

## Streamed / structured telemetry — the SOURCE-READER interface

Some sources aren't plain-text logs the line adapters can parse: Docker/
containerd `json-file` logs, OTLP JSON-lines, a binary ring buffer, a socket. A
**`SourceReader`** (in `connectors/log_sources.py`) decodes such a source into
raw records — `str` lines OR pre-decoded `dict`s — which the merge layer then
normalizes through the SAME adapter pipeline (dicts → the `jsonl` adapter,
strings → the named/auto line adapter), so everything still converges on the
unified schema.

```python
class MyReader(log_sources.SourceReader):
    kind = "my_stream"
    def read_records(self, path, *, max_offset=0, tail_bytes=1_048_576):
        # return a list of str lines OR dicts shaped like
        # {ts_ms|ts, category, source, data:{message,…}}
        return [...]

log_sources.register_reader("my_stream", MyReader)
```

Reference it from a profile source with `{"path": …, "reader": "my_stream"}`.
Ships today: **`docker_json`** (Docker/containerd `json-file` →
`{ts, category=stdout|error, source=container.<stream>, data.message}`), tailing
a growing file (bounded by `max_offset`/`tail_bytes`, capture-time-aligned).
`list_readers()` enumerates registered readers.

## Adding a new adapter (the whole contract)

```python
class MyAdapter(LogAdapter):
    name = "myformat"
    language = "mylang"
    def detect(self, sample_lines: list[str]) -> float:
        # fraction of sample lines that match your format (0..1)
        ...
    def parse_line(self, line: str) -> dict | None:
        # return self._event(level=..., message=..., source=..., ts_ms=...,
        #                     fields={...}, raw=line)  or None to skip
        ...

register_adapter(MyAdapter())     # keeps `raw` last automatically
```

Guidelines: make `detect()` specific enough to avoid collisions (aim for a full
regex match, not a keyword); put the message in `data.message`; set `level` so
`category` derives correctly (errors → `category:"error"`); parse a timestamp to
`ts_ms` when present. Run `python3 python_backend/connectors/test_log_adapters.py`
— add a sample to `SAMPLES` (detection) and a parse assertion.

## Roadmap

See the COVERAGE + ROADMAP block at the bottom of `log_adapters.py`. Binary /
streamed telemetry (OTLP gRPC, Windows ETW live) is added as a *source reader*
in `log_sources.py` that decodes to dicts fed through `jsonl`.
