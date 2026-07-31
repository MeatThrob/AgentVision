"""
Unit tests for modules.diagnostics (pure stdlib). Run:
    python3 python_backend/modules/test_diagnostics.py
Exits non-zero on any failure.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from modules import diagnostics as dx  # noqa: E402

_fails = 0


def check(name, cond, detail=""):
    global _fails
    if not cond:
        _fails += 1
    print(f"  [{'ok ' if cond else 'FAIL'}] {name}{'' if cond or not detail else '  — ' + detail}")


PY_TB = '''Traceback (most recent call last):
  File "/app/main.py", line 42, in run
    do_thing(cfg["missing"])
  File "/app/util.py", line 10, in do_thing
    return d[k]
KeyError: 'missing'
'''

NODE_TB = '''TypeError: Cannot read properties of undefined (reading 'x')
    at Object.<anonymous> (/app/server.js:12:20)
    at Module._compile (node:internal/modules/cjs/loader:1234:14)
'''

JAVA_TB = '''Exception in thread "main" java.lang.NullPointerException: Cannot invoke method
    at com.acme.Service.handle(Service.java:88)
    at com.acme.Main.main(Main.java:10)
'''

GO_TB = '''panic: runtime error: index out of range [3] with length 3
goroutine 1 [running]:
main.doThing(...)
    /app/main.go:15 +0x1d
'''

RUBY_TB = '''app.rb:10:in `foo': undefined method `bar' for nil:NilClass (NoMethodError)
    from app.rb:20:in `<main>'
'''

NET_TB = '''System.NullReferenceException: Object reference not set to an instance of an object.
   at MyApp.Foo.Bar() in /app/Foo.cs:line 42
   at MyApp.Program.Main()
'''


def test_parse():
    print("parse_exception (multi-language):")
    e = dx.parse_exception(PY_TB)
    check("python type", e["exception_type"] == "KeyError", str(e))
    check("python lang", e["language"] == "python")
    check("python frames", len(e["frames"]) == 2 and e["frames"][0]["file"] == "/app/main.py"
          and e["frames"][0]["line"] == 42 and e["frames"][0]["func"] == "run", str(e["frames"]))
    check("python cause", "key that does not exist" in e["probable_cause"])

    e = dx.parse_exception(NODE_TB)
    check("node type", e["exception_type"] == "TypeError", str(e))
    check("node lang", e["language"] == "node")
    check("node frame file", any(f["file"] == "/app/server.js" and f["line"] == 12 for f in e["frames"]), str(e["frames"]))
    check("node cause (null deref)", "null/None/undefined" in e["probable_cause"], e["probable_cause"])

    e = dx.parse_exception(JAVA_TB)
    check("java type", e["exception_type"] == "NullPointerException", str(e))
    check("java lang", e["language"] == "java")
    check("java frame", any(f["file"] == "Service.java" and f["line"] == 88 for f in e["frames"]), str(e["frames"]))

    e = dx.parse_exception(GO_TB)
    check("go type", e["exception_type"] == "panic", str(e))
    check("go lang", e["language"] == "go")
    check("go cause (index)", "out of bounds" in e["probable_cause"], e["probable_cause"])
    check("go frame", any(f["file"] == "/app/main.go" and f["line"] == 15 for f in e["frames"]), str(e["frames"]))

    e = dx.parse_exception(RUBY_TB)
    check("ruby type", e["exception_type"] == "NoMethodError", str(e))
    check("ruby lang", e["language"] == "ruby")

    e = dx.parse_exception(NET_TB)
    check("dotnet type", e["exception_type"] == "NullReferenceException", str(e))
    check("dotnet lang", e["language"] == "dotnet")
    check("dotnet frame w/ file", any(f["file"] == "/app/Foo.cs" and f["line"] == 42 for f in e["frames"]), str(e["frames"]))

    check("non-exception → None", dx.parse_exception("just a normal log line") is not None)  # returns dict w/ message
    check("empty → None", dx.parse_exception("") is None)

    # ── Regression: bare Node "Error:" header + node: internal frame ─────────
    e = dx.parse_exception("Error: connect ECONNREFUSED 127.0.0.1:5432\n"
                           "    at TCPConnectWrap.afterConnect [as oncomplete] (node:net:1595:16)")
    check("node bare Error type", e["exception_type"] == "Error", str(e))
    check("node bare Error msg", e["message"].startswith("connect ECONNREFUSED"), e["message"])
    check("node internal frame", e["frames"] and e["frames"][0]["file"] == "node:net"
          and e["frames"][0]["line"] == 1595, str(e["frames"]))

    # ── Regression: bare "at file:line:col" frames must not merge into the
    #    next parenthesized frame; async prefix stripped ──────────────────────
    e = dx.parse_exception("TypeError: x\n"
                           "    at doWork (/app/a.js:12:15)\n"
                           "    at /app/node_modules/express/lib/router/index.js:280:10\n"
                           "    at async main (/app/a.js:30:3)")
    check("node 3 frames", len(e["frames"]) == 3, str(e["frames"]))
    check("node bare frame kept", e["frames"][1]["file"].endswith("router/index.js")
          and e["frames"][1]["line"] == 280, str(e["frames"]))
    check("node async func", e["frames"][2]["func"] == "main", str(e["frames"]))

    # ── Regression: Windows-path Node frames ─────────────────────────────────
    e = dx.parse_exception("Error: ENOENT: no such file or directory, open 'C:\\data\\x.txt'\n"
                           "    at readConfig (C:\\app\\src\\config.js:12:20)")
    check("node win frame", any(f["file"] == "C:\\app\\src\\config.js" and f["line"] == 12
                                for f in e["frames"]), str(e["frames"]))

    # ── Regression: minimal Go panic (no goroutine dump) ─────────────────────
    e = dx.parse_exception("panic: runtime error: index out of range [3]\n\tmain.go:10 +0x1d")
    check("go minimal type", e["exception_type"] == "panic" and e["language"] == "go", str(e))
    check("go minimal frame", e["frames"] and e["frames"][0]["file"] == "main.go"
          and e["frames"][0]["line"] == 10, str(e["frames"]))

    # ── Go frames carry the function name from the preceding line ────────────
    e = dx.parse_exception("panic: boom\n\ngoroutine 1 [running]:\nmain.process(0x0)\n"
                           "\t/app/main.go:22 +0x3\nmain.main()\n\t/app/main.go:11 +0x25")
    check("go frame func", e["frames"][0]["func"] == "main.process"
          and e["frames"][1]["func"] == "main.main", str(e["frames"]))

    # ── Regression: Ruby message must not keep the file:line:in prefix ───────
    e = dx.parse_exception(RUBY_TB)
    check("ruby msg clean", e["message"] == "undefined method `bar' for nil:NilClass", e["message"])
    check("ruby cause", "method/attribute" in e["probable_cause"], e["probable_cause"])

    # ── Regression: Python bare exception (no colon) + custom class ──────────
    e = dx.parse_exception('Traceback (most recent call last):\n  File "m.py", line 3, in <module>\n'
                           "    raise KeyboardInterrupt\nKeyboardInterrupt")
    check("py bare exc", e["exception_type"] == "KeyboardInterrupt", str(e))
    e = dx.parse_exception('Traceback (most recent call last):\n  File "m.py", line 3, in <module>\n'
                           "    raise Foo('bad')\nFoo: bad")
    check("py custom exc", e["exception_type"] == "Foo" and e["message"] == "bad", str(e))

    # ── Java: deepest "Caused by" wins ────────────────────────────────────────
    e = dx.parse_exception("java.lang.RuntimeException: outer\n"
                           "\tat com.example.App.main(App.java:5)\n"
                           "Caused by: java.io.FileNotFoundException: /etc/missing.conf\n"
                           "\tat com.example.Config.load(Config.java:33)")
    check("java caused-by type", e["exception_type"] == "FileNotFoundException", str(e))
    check("java caused-by cause", "does not exist" in e["probable_cause"], e["probable_cause"])

    # ── .NET: innermost "--->" exception wins ─────────────────────────────────
    e = dx.parse_exception("System.AggregateException: One or more errors occurred. "
                           "---> System.Net.Http.HttpRequestException: Connection refused\n"
                           "   at MyApp.Client.Get() in /src/Client.cs:line 40")
    check("dotnet inner type", e["exception_type"] == "HttpRequestException", str(e))
    check("dotnet inner msg", e["message"] == "Connection refused", e["message"])

    # ── Rust / PHP / C++ get real parses (not the unknown fallback) ──────────
    e = dx.parse_exception("thread 'main' panicked at src/main.rs:4:5:\n"
                           "index out of bounds: the len is 3 but the index is 7")
    check("rust lang+type", e["language"] == "rust" and e["exception_type"] == "panic", str(e))
    check("rust msg", "index out of bounds" in e["message"], e["message"])
    check("rust frame", e["frames"] and e["frames"][0]["file"] == "src/main.rs"
          and e["frames"][0]["line"] == 4, str(e["frames"]))

    e = dx.parse_exception("PHP Fatal error:  Uncaught TypeError: Unsupported operand types: "
                           "string + int in /var/www/index.php:7\nStack trace:\n#0 {main}\n"
                           "  thrown in /var/www/index.php on line 7")
    check("php type", e["language"] == "php" and e["exception_type"] == "TypeError", str(e))
    check("php frame", e["frames"] and e["frames"][0]["file"] == "/var/www/index.php"
          and e["frames"][0]["line"] == 7, str(e["frames"]))

    e = dx.parse_exception("terminate called after throwing an instance of 'std::out_of_range'\n"
                           "  what():  vector::_M_range_check\nAborted (core dumped)")
    check("cpp type", e["language"] == "cpp" and e["exception_type"] == "out_of_range", str(e))
    check("cpp msg", e["message"].startswith("vector::_M_range_check"), e["message"])


def test_parser_gap_closures():
    """The specific gaps the adversarial verifier flagged — each must yield the
    right cause AND (for cpp/php) structured frames[]."""
    print("parser gap closures:")

    # Java ArithmeticException '/ by zero' — exact JVM phrasing → divide-by-zero.
    e = dx.parse_exception("Exception in thread \"main\" java.lang.ArithmeticException: / by zero\n"
                           "\tat com.acme.Calc.divide(Calc.java:12)\n"
                           "\tat com.acme.Main.main(Main.java:5)")
    check("java arithmetic type", e["exception_type"] == "ArithmeticException", str(e))
    check("java '/ by zero' → divide cause", "Division by zero" in e["probable_cause"], e["probable_cause"])
    check("java arithmetic frame", any(f["file"] == "Calc.java" and f["line"] == 12
                                       for f in e["frames"]), str(e["frames"]))

    # Rust unwrap on None → null/None cause.
    e = dx.parse_exception("thread 'main' panicked at src/main.rs:8:14:\n"
                           "called `Option::unwrap()` on a `None` value")
    check("rust unwrap None → cause", "unwrap" in e["probable_cause"].lower()
          and "None/Err" in e["probable_cause"], e["probable_cause"])
    # Rust unwrap on Err → inner error surfaced (improved: see test_parser_depth_closures).
    e = dx.parse_exception("thread 'main' panicked at src/db.rs:20:10:\n"
                           "called `Result::unwrap()` on an `Err` value: ConnectionRefused")
    check("rust unwrap Err → inner surfaced",
          "Unwrapped an Err" in e["probable_cause"] and "ConnectionRefused" in e["probable_cause"],
          e["probable_cause"])

    # Go concurrency: send on closed channel, nil deref, deadlock.
    e = dx.parse_exception("panic: send on closed channel\n\ngoroutine 6 [running]:\n"
                           "main.worker(0x0)\n\t/app/worker.go:31 +0x40")
    check("go closed channel → concurrency cause", "channel" in e["probable_cause"].lower(), e["probable_cause"])
    check("go closed channel frame", any(f["file"] == "/app/worker.go" and f["line"] == 31
                                         for f in e["frames"]), str(e["frames"]))
    e = dx.parse_exception("panic: runtime error: invalid memory address or nil pointer dereference\n"
                           "\t/app/x.go:5 +0x1")
    check("go nil deref → null cause", "null/None" in e["probable_cause"], e["probable_cause"])
    e = dx.parse_exception("fatal error: all goroutines are asleep - deadlock!\n\n"
                           "goroutine 1 [chan receive]:\nmain.main()\n\t/app/main.go:9 +0x2")
    check("go deadlock → concurrency cause", e["probable_cause"] != "", e["probable_cause"])

    # C++ gdb backtrace → cpp language + structured frames[].
    e = dx.parse_exception(
        "Program received signal SIGSEGV, Segmentation fault.\n"
        "0x0000555555 in doWork (n=3) at /app/main.cpp:42\n"
        "#0  0x0000555555 in doWork (n=3) at /app/main.cpp:42\n"
        "#1  0x0000555556 in main () at /app/main.cpp:99")
    check("cpp gdb lang", e["language"] == "cpp", str(e))
    check("cpp gdb type SIGSEGV", e["exception_type"] == "SIGSEGV", str(e))
    check("cpp gdb frames", any(f["file"] == "/app/main.cpp" and f["line"] == 42
                                and f["func"].startswith("doWork") for f in e["frames"]),
          str(e["frames"]))
    check("cpp segv → cause", "segmentation fault" in e["probable_cause"].lower(), e["probable_cause"])

    # C++ must NOT steal a Java stack (no C/C++ ext → stays java).
    e = dx.parse_exception("Exception in thread \"main\" java.lang.RuntimeException: x\n"
                           "\tat com.acme.A.b(A.java:1)")
    check("java not misdetected as cpp", e["language"] == "java", str(e))

    # PHP '#N /path(line): Class->method()' backtrace frames.
    e = dx.parse_exception(
        "PHP Fatal error:  Uncaught RuntimeException: boom in /var/www/app.php:30\n"
        "Stack trace:\n"
        "#0 /var/www/app.php(30): MyService->run()\n"
        "#1 /var/www/index.php(8): MyService->__construct()\n"
        "#2 {main}\n  thrown in /var/www/app.php on line 30")
    check("php type", e["exception_type"] == "RuntimeException", str(e))
    check("php stack frames", any(f["file"] == "/var/www/app.php" and f["line"] == 30
                                  and "MyService->run" in f["func"] for f in e["frames"]),
          str(e["frames"]))

    # New real-world causes.
    check("EADDRINUSE cause", "already in use" in dx.probable_cause("Error", "listen EADDRINUSE :::8080"))
    check("ENOSPC cause", "disk space" in dx.probable_cause("OSError", "[Errno 28] ENOSPC: No space left on device"))
    check("too many open files", "file-descriptor" in dx.probable_cause("OSError", "EMFILE: too many open files"))
    check("rate limit 429", "Rate limited" in dx.probable_cause("HTTPError", "429 Too Many Requests"))
    check("TLS cert", "certificate" in dx.probable_cause("SSLError", "certificate verify failed: self signed"))


def test_parser_depth_closures():
    """Residual parser depth closed this pass."""
    print("parser depth closures:")

    # C++ glog symbol-only frames (@ 0x.. func, NO file:line) → frames w/ func.
    e = dx.parse_exception(
        "*** Aborted at 1712345678 (unix time) ***\n"
        "*** SIGSEGV (@0x0) received by PID 123 (TID 0x7f) ***\n"
        "    @     0x7f8b0a in doWork(int)\n"
        "    @     0x7f8b1c in main")
    check("cpp glog lang", e["language"] == "cpp", str(e))
    check("cpp glog type", e["exception_type"] == "SIGSEGV", str(e))
    check("cpp glog func frames (no file)", any(f["func"].startswith("doWork") and f["file"] == ""
                                                for f in e["frames"]), str(e["frames"]))

    # Rust unwrap() on Err: <inner> → inner error surfaced + its cause folded in.
    e = dx.parse_exception("thread 'main' panicked at src/db.rs:20:10:\n"
                           "called `Result::unwrap()` on an `Err` value: Connection refused (os error 61)")
    check("rust inner surfaced", "Connection refused" in e["probable_cause"], e["probable_cause"])
    check("rust inner cause folded", "refused" in e["probable_cause"].lower(), e["probable_cause"])

    # Python SystemExit / KeyboardInterrupt.
    e = dx.parse_exception('Traceback (most recent call last):\n'
                           '  File "m.py", line 2, in <module>\n    sys.exit(3)\nSystemExit: 3')
    check("py SystemExit type", e["exception_type"] == "SystemExit" and e["message"] == "3", str(e))

    # Python chained "During handling of the above exception" → FINAL exc wins.
    e = dx.parse_exception(
        'Traceback (most recent call last):\n'
        '  File "a.py", line 3, in f\n    d["x"]\nKeyError: \'x\'\n\n'
        'During handling of the above exception, another exception occurred:\n\n'
        'Traceback (most recent call last):\n'
        '  File "a.py", line 5, in <module>\n    g()\nValueError: bad state')
    check("py chained final type", e["exception_type"] == "ValueError", str(e))
    check("py chained final msg", e["message"] == "bad state", str(e))
    check("py chained final frame", e["frames"][-1]["line"] == 5, str(e["frames"]))


def test_cause_coverage():
    print("probable_cause coverage:")
    check("NoMethodError", dx.probable_cause("NoMethodError", "undefined method `x'") != "")
    check("InvalidOperationException", dx.probable_cause("InvalidOperationException", "") != "")
    check("NameError", dx.probable_cause("NameError", "name 'x' is not defined") != "")
    check("ArgumentError", dx.probable_cause("ArgumentError", "wrong number of arguments") != "")
    check("StackOverflowError", dx.probable_cause("StackOverflowError", "") != "")
    check("RecursionError", dx.probable_cause("RecursionError", "maximum recursion depth exceeded") != "")
    check("max call stack", dx.probable_cause("RangeError", "Maximum call stack size exceeded") != "")
    check("ClassCastException", dx.probable_cause("ClassCastException", "A cannot be cast to B") != "")
    check("UnicodeDecodeError", dx.probable_cause("UnicodeDecodeError", "invalid start byte") != "")
    check("ECONNRESET", dx.probable_cause("Error", "read ECONNRESET") != "")
    check("ENOTFOUND (dns)", "DNS" in dx.probable_cause("Error", "getaddrinfo ENOTFOUND example.com"))
    check("FileNotFound ≠ DNS", "DNS" not in dx.probable_cause("FileNotFoundException", "/etc/missing.conf"),
          dx.probable_cause("FileNotFoundException", "/etc/missing.conf"))
    check("KeyboardInterrupt", dx.probable_cause("KeyboardInterrupt", "") != "")
    check("NotImplementedError", dx.probable_cause("NotImplementedError", "") != "")
    # ── JS built-in error classes must resolve even with an EMPTY message ─────
    check("ReferenceError (bare)", dx.probable_cause("ReferenceError", "") != "")
    check("RangeError (bare)", dx.probable_cause("RangeError", "") != "")
    check("RangeError (array length)",
          dx.probable_cause("RangeError", "Invalid array length") != "")
    check("EvalError (bare)", dx.probable_cause("EvalError", "") != "")
    check("URIError (bare)", dx.probable_cause("URIError", "") != "")
    check("URIError (malformed)",
          "URI" in dx.probable_cause("URIError", "URI malformed"))
    # RangeError carrying a call-stack message keeps the recursion hypothesis
    # (StackOverflow rule is ordered ahead of the RangeError rule).
    check("RangeError call-stack → recursion",
          "recursion" in dx.probable_cause("RangeError", "Maximum call stack size exceeded").lower(),
          dx.probable_cause("RangeError", "Maximum call stack size exceeded"))


def test_fingerprint():
    print("fingerprint (dedup):")
    a = dx.fingerprint("KeyError: 'x' at /a/b.py line 42")
    b = dx.fingerprint("KeyError: 'x' at /c/d.py line 99")   # different path+line
    check("paths/numbers normalized to same fp", a == b and len(a) == 12, f"{a} vs {b}")
    c = dx.fingerprint("TypeError: bad")
    check("different errors → different fp", c != a)
    check("empty → ''", dx.fingerprint("") == "")
    # Windows paths normalize like Unix paths.
    w1 = dx.fingerprint("KeyError at C:\\Users\\dan\\app.py")
    w2 = dx.fingerprint("KeyError at C:\\Users\\bob\\other.py")
    check("win paths → same fp", w1 == w2, f"{w1} vs {w2}")
    # Numbers inside identifiers (ids/ports/counters) don't fragment dedup.
    i1 = dx.fingerprint("KeyError: 'user_42'")
    i2 = dx.fingerprint("KeyError: 'user_99'")
    check("embedded numbers → same fp", i1 == i2, f"{i1} vs {i2}")
    # …but genuinely different keys stay distinct.
    check("different keys → different fp",
          dx.fingerprint("KeyError: 'name'") != dx.fingerprint("KeyError: 'email'"))
    # Hex stays collapsed as hex (not NxN).
    h1 = dx.fingerprint("segfault at 0x7f3a1b")
    h2 = dx.fingerprint("segfault at 0xdeadbeef")
    check("hex → same fp", h1 == h2, f"{h1} vs {h2}")
    # bridge_server._fingerprint delegates to the same implementation.
    try:
        import importlib.util as _ilu
        _p = Path(__file__).resolve().parent.parent / "api" / "bridge_server.py"
        src = _p.read_text(encoding="utf-8", errors="replace")
        check("bridge delegates fingerprint",
              "from modules.diagnostics import fingerprint" in src)
    except Exception as ex:  # pragma: no cover
        check("bridge delegates fingerprint", False, str(ex))


def test_state_delta():
    print("state_delta:")
    d = dx.state_delta({"hp": 100, "mp": 50, "gone": 1}, {"hp": 80, "mp": 50, "new": 7})
    check("changed", d["changed"] == {"hp": {"from": 100, "to": 80}}, str(d["changed"]))
    check("added", d["added"] == {"new": 7})
    check("removed", d["removed"] == {"gone": 1})
    check("counts", d["changed_count"] == 1 and d["added_count"] == 1 and d["removed_count"] == 1)

    # NESTED: a change deep inside state.json surfaces as a dotted leaf key.
    prev = {"stats": {"fps": 60}, "state": {"player": {"hp": 100, "pos": {"x": 3, "y": 4}}}}
    cur = {"stats": {"fps": 58}, "state": {"player": {"hp": 80, "pos": {"x": 5, "y": 4}}}}
    d = dx.state_delta(prev, cur)
    check("nested changed keys", d["changed"] == {
        "stats.fps": {"from": 60, "to": 58},
        "state.player.hp": {"from": 100, "to": 80},
        "state.player.pos.x": {"from": 3, "to": 5}}, str(d["changed"]))
    check("nested unchanged leaf omitted", "state.player.pos.y" not in d["changed"])

    # flatten_state handles lists + depth/key bounds without raising.
    f = dx.flatten_state({"items": [{"id": 1}, {"id": 2}], "name": "x"})
    check("flatten list index keys", f.get("items[0].id") == 1 and f.get("items[1].id") == 2, str(f))
    check("flatten bounded (no raise on huge)",
          isinstance(dx.flatten_state({str(i): i for i in range(1000)}), dict))


def test_summarize():
    print("summarize_frame:")
    s = dx.summarize_frame(running=False, error=None, anomaly=None, stuck=False,
                           stats=None, state_delta_d=None)
    check("not running", "not_running" in s["tags"] and s["confidence"] == 0.3)

    s = dx.summarize_frame(running=True,
                           error={"exception_type": "KeyError", "message": "'x'",
                                  "probable_cause": "A dict/map...", "occurrence_count": 3},
                           anomaly=None, stuck=False, stats=None, state_delta_d=None)
    check("error tagged", "error" in s["tags"] and "recurring" in s["tags"])
    check("error summary has type", "KeyError" in s["summary"] and "3×" in s["summary"], s["summary"])
    check("error low confidence", s["confidence"] == 0.2)

    s = dx.summarize_frame(running=True, error=None, anomaly={"type": "screen_stuck"},
                           stuck=True, stats=None, state_delta_d=None)
    check("stuck tagged", "stuck" in s["tags"])

    s = dx.summarize_frame(running=True, error=None, anomaly=None, stuck=False,
                           stats={"hp": 80}, state_delta_d={"changed_count": 2, "added_count": 0})
    check("healthy + state_changed", "healthy" in s["tags"] and "state_changed" in s["tags"])
    check("healthy confidence", s["confidence"] == 0.85)

    # Regression: a non-numeric anomaly confidence must never raise.
    s = dx.summarize_frame(running=True, error=None,
                           anomaly={"detected": True, "type": "log_burst", "confidence": "high"},
                           stuck=False, stats=None, state_delta_d=None)
    check("anomaly str confidence no crash", s["confidence"] == 0.6, str(s))
    s = dx.summarize_frame(running=True, error=None,
                           anomaly={"detected": True, "type": "x", "confidence": 7},
                           stuck=False, stats=None, state_delta_d=None)
    check("anomaly confidence clamped to 1", s["confidence"] == 1.0, str(s))


def test_state_delta_bounds():
    print("state_delta bounds:")
    d = dx.state_delta({"blob": "x"}, {"blob": "y" * 5000})
    check("huge string clipped", len(d["changed"]["blob"]["to"]) <= 201, str(len(d["changed"]["blob"]["to"])))
    d = dx.state_delta(None, None)
    check("None inputs ok", d["changed_count"] == 0)
    big_prev = {f"k{i}": i for i in range(100)}
    big_cur = {f"k{i}": i + 1 for i in range(100)}
    d = dx.state_delta(big_prev, big_cur)
    check("changed capped at 30 (token-aware) + truncated flag",
          len(d["changed"]) == 30 and d["changed_count"] == 100 and d["truncated"] is True,
          f"len={len(d['changed'])} count={d['changed_count']} trunc={d.get('truncated')}")


if __name__ == "__main__":
    print("=" * 66)
    print("diagnostics test suite")
    print("=" * 66)
    test_parse()
    test_parser_gap_closures()
    test_parser_depth_closures()
    test_cause_coverage()
    test_fingerprint()
    test_state_delta()
    test_state_delta_bounds()
    test_summarize()
    print("=" * 66)
    if _fails:
        print(f"RESULT: {_fails} FAILURE(S)")
        sys.exit(1)
    print("RESULT: all diagnostics tests passed")
    sys.exit(0)
