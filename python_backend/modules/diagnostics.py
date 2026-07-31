"""
AI-facing diagnostics — turn raw signals into STRUCTURED, model-consumable JSON.
================================================================================
This is the intelligence layer that makes AgentVision "designed to work with
AI": instead of handing a model a raw traceback string and a wall of log lines,
it produces structured objects the model can reason over directly —

  • parse_exception()  raw traceback (any language) → {exception_type, message,
                       language, frames:[{file,line,func}], probable_cause}
  • fingerprint()      stable id that collapses equivalent errors (dedup)
  • probable_cause()   a plain-English root-cause hypothesis
  • state_delta()      key-level added/removed/changed between two state dicts
  • summarize_frame()  a one-line summary + recommended_next + tags + confidence

Pure standard library (no third-party deps) so it is fully unit-testable and
runs identically on Windows and macOS. Nothing here raises into a caller.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional

# ── Fingerprinting ──────────────────────────────────────────────────────────────
# THE single fingerprint implementation — bridge_server._fingerprint delegates
# here, so an emit-time fingerprint always matches a query-time one.

_HEX_RE      = re.compile(r"0x[0-9a-fA-F]+")
_WIN_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s:'\",)\]]+")
_PATH_RE     = re.compile(r"/[^\s:]+")
_NUM_RE      = re.compile(r"\d+")
_WS_RE       = re.compile(r"\s+")


def fingerprint(text: str) -> str:
    """SHA1[:12] of error text normalized so paths/numbers/hex addresses don't
    fragment otherwise-identical errors. Empty string → ''.
    Order matters: hex before numbers (so 0x1d → 0xHEX, not NxN); Windows paths
    before Unix paths. Numbers are normalized even inside identifiers
    (user_42 → user_N) so ids/ports/counters don't fragment the dedup registry."""
    if not text:
        return ""
    norm = _HEX_RE.sub("0xHEX", text)
    norm = _WIN_PATH_RE.sub("/PATH", norm)
    norm = _PATH_RE.sub("/PATH", norm)
    norm = _NUM_RE.sub("N", norm)
    norm = _WS_RE.sub(" ", norm).strip()
    return hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:12]


# ── Probable-cause heuristics ─────────────────────────────────────────────────
# (regex on the exception type OR message) → plain-English hypothesis. Ordered;
# first match wins. Language-agnostic — keyed on universal error vocabulary.

_CAUSE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"KeyError", re.I),
     "A dict/map was accessed with a key that does not exist."),
    (re.compile(r"index ?(out of range|error|out of bounds)|IndexOutOfBounds|out_of_range", re.I),
     "A sequence/array was indexed out of bounds."),
    (re.compile(r"NoneType|NullPointer|NullReference|ArgumentNull|nil pointer|nil map|"
                r"undefined is not|cannot read propert(y|ies) of (null|undefined)", re.I),
     "A null/None/undefined value was dereferenced — check it was initialized."),
    (re.compile(r"(Option|Result)::(unwrap|expect)|"
                r"unwrap\(\)`? on (a |an )?`?(None|Err)|"
                r"called `Option|called `Result", re.I),
     "Called .unwrap()/.expect() on a None/Err — the Option/Result had no value; "
     "handle the None/Err case explicitly instead of unwrapping."),
    (re.compile(r"StackOverflow|maximum call stack|RecursionError|"
                r"maximum recursion depth|too much recursion", re.I),
     "Unbounded recursion overflowed the call stack."),
    (re.compile(r"NoMethodError|undefined method|has no attribute|is not a function|"
                r"MethodNotFound|AttributeError", re.I),
     "A method/attribute was called on an object that does not define it — "
     "often a nil/None/undefined receiver or a typo in the name."),
    (re.compile(r"NameError|ReferenceError|is not defined\b|undeclared identifier", re.I),
     "An undefined variable/function name was referenced."),
    (re.compile(r"URIError|malformed URI|URI malformed", re.I),
     "A URI/URL was malformed and could not be parsed/encoded."),
    (re.compile(r"EvalError", re.I),
     "The global eval() was used incorrectly."),
    (re.compile(r"RangeError|invalid array length|out of range", re.I),
     "A value was outside the allowed range (bad length, precision, or index)."),
    (re.compile(r"InvalidOperation|IllegalState|ObjectDisposed", re.I),
     "The object was in an invalid state for the requested operation "
     "(e.g. empty sequence, already closed/disposed, wrong lifecycle phase)."),
    (re.compile(r"IllegalArgument|ArgumentError|ArgumentOutOfRange|"
                r"wrong number of arguments|invalid argument", re.I),
     "A function received an invalid argument (wrong value, type, or count)."),
    (re.compile(r"ClassCast|InvalidCast|cannot be cast|incompatible types", re.I),
     "A value was cast/converted to an incompatible type."),
    (re.compile(r"ConcurrentModification|collection was modified.*enumerat", re.I),
     "A collection was modified while being iterated."),
    (re.compile(r"Unicode(Decode|Encode|Translate)?Error|codec can'?t|"
                r"invalid byte sequence|malformed UTF", re.I),
     "Text encoding/decoding failed — bytes are not valid in the expected encoding."),
    (re.compile(r"EPIPE|broken pipe|ECONNRESET|connection reset|socket hang up", re.I),
     "The connection was closed/reset by the peer mid-operation."),
    (re.compile(r"\bENOTFOUND|getaddrinfo|UnknownHost|name or service not known|"
                r"nodename nor servname", re.I),
     "DNS lookup failed — the hostname could not be resolved."),
    (re.compile(r"KeyboardInterrupt|SIGINT\b", re.I),
     "The process was interrupted (Ctrl-C / SIGINT)."),
    (re.compile(r"NotImplemented", re.I),
     "An unimplemented code path was hit."),
    (re.compile(r"FrozenError|can't modify frozen", re.I),
     "Attempted to mutate a frozen/immutable object."),
    (re.compile(r"OverflowError|arithmetic overflow|integer overflow", re.I),
     "A numeric overflow occurred."),
    (re.compile(r"ModuleNotFound|ImportError|cannot find module|no such file or directory.*node_modules|"
                r"unresolved import|package .* is not installed", re.I),
     "A dependency/module is missing or not installed."),
    (re.compile(r"FileNotFound|ENOENT|no such file", re.I),
     "A file/path that was expected does not exist."),
    (re.compile(r"ConnectionRefused|ECONNREFUSED|connection refused", re.I),
     "A network connection was refused — the service is down or host/port is wrong."),
    (re.compile(r"timeout|timed out|ETIMEDOUT|deadline exceeded", re.I),
     "An operation exceeded its time limit."),
    (re.compile(r"Permission|EACCES|access denied|unauthori[sz]ed|forbidden|\b403\b", re.I),
     "Insufficient permissions / authentication for the operation."),
    (re.compile(r"ZeroDivision|divide by zero|division by zero|/ by zero", re.I),
     "Division by zero."),
    (re.compile(r"AssertionError|assertion failed", re.I),
     "An assertion / invariant check failed."),
    (re.compile(r"OutOfMemory|OOMKill|\bOOM\b|Cannot allocate memory|MemoryError|bad_alloc", re.I),
     "The process ran out of memory."),
    (re.compile(r"TypeError|type mismatch|not assignable|cannot convert", re.I),
     "An operation received a value of the wrong type."),
    (re.compile(r"ValueError|invalid literal|ParseError|SyntaxError|JSONDecodeError|"
                r"unmarshal|deserializ", re.I),
     "A value could not be parsed/validated — likely malformed input or bad format."),
    (re.compile(r"segmentation fault|SIGSEGV|access violation", re.I),
     "A segmentation fault — invalid memory access (common in native/C/C++ code)."),
    (re.compile(r"send on closed channel|close of closed channel|close of nil channel|"
                r"all goroutines are asleep", re.I),
     "A Go channel/goroutine concurrency bug — send/close on a closed or nil "
     "channel, or all goroutines blocked (deadlock)."),
    (re.compile(r"EADDRINUSE|address already in use|port .* in use", re.I),
     "The network address/port is already in use — another process holds it."),
    (re.compile(r"ENOSPC|no space left on device|disk (is )?full|quota exceeded", re.I),
     "Out of disk space (or quota exceeded)."),
    (re.compile(r"EMFILE|too many open files|file descriptor limit", re.I),
     "The process hit the open-file-descriptor limit — leaked/unclosed handles."),
    (re.compile(r"\b429\b|rate limit|too many requests|throttl", re.I),
     "Rate limited — too many requests; back off and retry."),
    (re.compile(r"certificate|SSL|TLS handshake|CERT_|x509|self.?signed", re.I),
     "A TLS/SSL certificate problem (expired, untrusted, or hostname mismatch)."),
    (re.compile(r"context canceled|operation was canceled|context canceled", re.I),
     "The operation was canceled (its context was cancelled — caller aborted or shut down)."),
    (re.compile(r"\b409\b|conflict|already exists|duplicate key|UNIQUE constraint", re.I),
     "A conflict — the resource already exists or violates a uniqueness constraint."),
    (re.compile(r"pool (is )?exhausted|no available connections|connection pool|"
                r"too many connections|QueuePool limit", re.I),
     "A connection/resource pool was exhausted — leaked or too few connections."),
    (re.compile(r"no such (table|column)|relation .* does not exist|unknown column", re.I),
     "A database schema mismatch — a table/column the query expects is missing."),
    (re.compile(r"deadlock|lock .* held|would block", re.I),
     "A locking/concurrency problem — possible deadlock or contention."),
]


def probable_cause(exception_type: str, message: str) -> str:
    """Best-effort root-cause hypothesis from the type + message. '' if unknown."""
    hay = f"{exception_type} {message}".strip()
    if not hay:
        return ""
    for pat, cause in _CAUSE_RULES:
        if pat.search(hay):
            return cause
    return ""


# ── Multi-language exception parsing ──────────────────────────────────────────

# Python:  File "path", line N, in func
_PY_FRAME = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)')
# Node:    at func (file:line:col)  |  at file:line:col  |  at async func (…)
# Line-anchored so a bare "at file:line:col" frame can never be swallowed into
# the next parenthesized frame. The lazy file + end anchor make Windows drives
# (C:\…) and node: internals (node:net:1595:16) parse correctly.
_NODE_FRAME = re.compile(
    r"^\s*at\s+(?:async\s+)?(?:(?P<func>[^\s(][^(\n]*?)\s+\()?"
    r"(?P<file>[^\n()]+?):(?P<line>\d+):\d+\)?\s*$", re.M)
# Node/browser error header: "TypeError: msg" | bare "Error: msg" |
# "Uncaught RangeError: msg" | "AssertionError [ERR_ASSERTION]: msg"
_NODE_HEAD = re.compile(
    r"^(?:Uncaught\s+)?(?P<type>(?:[A-Za-z_$][\w$]*)?(?:Error|Exception))"
    r"(?:\s*\[(?P<code>[A-Z0-9_]+)\])?:\s*(?P<msg>.*)$")
# Java:    at pkg.Class.method(File.java:line)
_JAVA_FRAME = re.compile(r"at (?P<func>[\w$.]+)\((?P<file>[^:)]+):(?P<line>\d+)\)")
# .NET:    at NS.Class.Method(...) in /path/File.cs:line 42
#          (we structure the file-bearing frames; type-only frames still inform
#           exception_type/message)
_NET_FRAME = re.compile(
    r"at (?P<func>.+?) in (?P<file>.+?):line (?P<line>\d+)", re.M)
# Go:      /path/file.go:line +0x..   (func name is on the PRECEDING line)
_GO_FILE_LINE = re.compile(r"^\s*(?P<file>\S+\.go):(?P<line>\d+)")
_GO_FUNC = re.compile(
    r"^(?:created by )?(?P<fn>[\w./@\-]+(?:\(\*?[\w.]+\))?[\w./@\-]*)\(")
# Ruby:    file:line:in `func'   (optional Windows drive prefix)
_RUBY_FRAME = re.compile(
    r"(?P<file>(?:[A-Za-z]:)?[^\s:]+):(?P<line>\d+):in [`'](?P<func>[^'`]+)['`]")
# Rust:    thread 'main' panicked at 'msg', src/main.rs:4:5   (pre-1.65)
#          thread 'main' panicked at src/main.rs:4:5:\nmsg    (1.65+)
_RUST_OLD = re.compile(r"panicked at '(?P<msg>[^']*)',\s*(?P<file>\S+?):(?P<line>\d+)")
_RUST_NEW = re.compile(r"panicked at (?P<file>\S+?):(?P<line>\d+):\d+:\s*\n(?P<msg>[^\n]*)")
_RUST_BT  = re.compile(r"^\s*\d+:\s*(?P<fn>\S+)\s*\n\s*at\s+(?P<file>\S+?):(?P<line>\d+)", re.M)
# PHP:     Uncaught Type: msg in /path/file.php:7  +  "#0 file(line): func()"
_PHP_HEAD  = re.compile(r"Uncaught\s+(?P<type>[\w\\]+):\s*(?P<msg>.*?)(?:\s+in\s+(?P<file>\S+?):(?P<line>\d+))?\s*$", re.M)
_PHP_FRAME = re.compile(r"^#\d+\s+(?P<file>[^\s(]+)\((?P<line>\d+)\):\s*(?P<func>[^\n]+)", re.M)
# C/C++: gdb/glog backtrace "#N 0x.. in func(args) at file:line" (0x prefix
# optional; func may be missing for inlined/stripped frames).
_CPP_GDB = re.compile(
    r"^#\d+\s+(?:0x[0-9a-fA-F]+\s+in\s+)?(?P<func>.+?)\s+at\s+"
    r"(?P<file>\S+):(?P<line>\d+)", re.M)
# A C/C++ source path anywhere (used to disambiguate SIGSEGV/gdb dumps from Java).
_CPP_EXT = re.compile(r"\.(?:c|cc|cpp|cxx|c\+\+|h|hpp|hh|hxx)\b")
# glog symbolized crash frame: "    @     0x7f8b in doWork()"  (no file:line).
_CPP_GLOG = re.compile(r"^\s*@\s+0x[0-9a-fA-F]+\s+(?:in\s+)?(?P<func>\S.*?)\s*$", re.M)
# glog crash header cue (symbol-only dumps carry no C/C++ file extension).
_CPP_GLOG_HDR = re.compile(
    r"(?:received by PID \d+|\*\*\* (?:SIGSEGV|SIGABRT|Aborted)|^\s*@\s+0x[0-9a-fA-F]+\s+\S)", re.M)


def _mk_frames(matches, limit: int = 25) -> list[dict]:
    out = []
    for m in matches:
        g = m.groupdict()
        if not g.get("file"):
            continue
        try:
            ln = int(g.get("line") or 0)
        except (TypeError, ValueError):
            ln = 0
        out.append({"file": g.get("file", ""), "line": ln,
                    "func": (g.get("func") or "").strip()})
        if len(out) >= limit:
            break
    return out


def parse_exception(block: str, language: str = "") -> Optional[dict]:
    """Parse a raw traceback/stack (any language) into a structured dict:
        {exception_type, message, language, frames:[{file,line,func}], probable_cause}
    Returns None if the text does not look like an exception. `language` may be
    passed as a hint; otherwise it is inferred."""
    if not block or not block.strip():
        return None
    text = block.strip()
    lines = text.splitlines()
    lang = (language or "").lower()

    # ── Infer language from cues if not given ────────────────────────────────
    if not lang:
        if "Traceback (most recent call last)" in text or _PY_FRAME.search(text):
            lang = "python"
        elif re.search(r"^\s*panic:", text, re.M):
            lang = "go"          # bare "panic:" is Go even without a goroutine dump
        elif "panicked at" in text:
            lang = "rust"
        elif re.search(r"^PHP (Fatal error|Warning|Parse error|Notice)", text, re.M) \
                or ("Uncaught" in text and re.search(r"^#\d+ ", text, re.M)):
            lang = "php"
        elif ("terminate called after throwing an instance of" in text
              or (_CPP_GDB.search(text) and _CPP_EXT.search(text))
              or (re.search(r"SIGSEGV|Segmentation fault|SIGABRT|core dumped", text)
                  and (_CPP_EXT.search(text) or _CPP_GLOG_HDR.search(text)))
              or (_CPP_GLOG.search(text) and _CPP_GLOG_HDR.search(text))):
            lang = "cpp"
        elif _JAVA_FRAME.search(text) or "Exception in thread" in text:
            lang = "java"
        elif re.search(r":\d+:in [`']", text):
            lang = "ruby"
        elif re.search(r"^\s*at .+ in .+:line \d+", text, re.M) or re.search(r"\bSystem\.\w+Exception", text):
            lang = "dotnet"
        elif re.search(r"^\s*at .+:\d+:\d+", text, re.M) or re.search(r"^\w*Error:", text):
            lang = "node"

    exc_type = ""
    message = ""
    frames: list[dict] = []
    cause_hint = ""   # extra text probable_cause may scan (unknown-language only)
    pc_override = ""  # a language branch may set the probable_cause directly

    if lang == "python":
        frames = _mk_frames(_PY_FRAME.finditer(text))
        # The exception line is the FIRST non-indented line AFTER the last
        # 'File "…", line N' frame — structurally exact, so it also handles
        # bare exceptions (KeyboardInterrupt), custom classes without an
        # Error/Exception suffix, and chained tracebacks (last chain wins).
        last_file_idx = -1
        for i, raw in enumerate(lines):
            if _PY_FRAME.search(raw):
                last_file_idx = i

        def _parse_py_exc_line(s: str) -> tuple[str, str]:
            mm = re.match(r"([A-Za-z_][\w.]*)\s*:\s*(.*)$", s)
            if mm:
                return mm.group(1).split(".")[-1], mm.group(2).strip()
            if re.fullmatch(r"[A-Za-z_][\w.]*", s):
                return s.split(".")[-1], ""
            return "", ""

        if last_file_idx >= 0:
            for j in range(last_file_idx + 1, len(lines)):
                raw = lines[j]
                if not raw.strip() or raw[:1] in (" ", "\t"):
                    continue
                exc_type, message = _parse_py_exc_line(raw.strip())
                # Multi-line messages (e.g. ValidationError): append the
                # continuation lines until a blank line, capped.
                if exc_type:
                    for cont in lines[j + 1:]:
                        if not cont.strip() or len(message) > 300:
                            break
                        message += " " + cont.strip()
                    message = message[:400]
                break
        if not exc_type:
            # No frames (or unparseable tail) — fall back to scanning upward
            # for an "ExcType: message" line.
            for raw in reversed(lines):
                s = raw.strip()
                if not s or s.startswith("File ") or raw[:1] in (" ", "\t"):
                    continue
                t, m2 = _parse_py_exc_line(s)
                if t and (t.endswith(("Error", "Exception", "Warning", "Interrupt", "Exit"))
                          or "." in s.split(":", 1)[0]):
                    exc_type, message = t, m2
                    break

    elif lang == "node":
        frames = _mk_frames(_NODE_FRAME.finditer(text))
        # The header is not always line 0: an uncaught exception dump starts
        # with "path.js:12", the offending code line, and a caret line before
        # "TypeError: …". Scan for the first header-looking line.
        hm = None
        for raw in lines:
            s = raw.strip()
            if not s or s.startswith("at "):
                continue
            hm = _NODE_HEAD.match(s)
            if hm:
                break
        if hm:
            exc_type, message = hm.group("type"), hm.group("msg").strip()
        else:
            message = lines[0].strip()

    elif lang == "java":
        frames = _mk_frames(_JAVA_FRAME.finditer(text))
        head = lines[0].strip()
        head = re.sub(r'^Exception in thread "[^"]*"\s*', "", head)
        # The deepest "Caused by:" is the actual root cause — prefer it.
        cbs = re.findall(r"^\s*Caused by:\s*(.+)$", text, re.M)
        if cbs:
            head = cbs[-1].strip()
        mm = re.match(r"([\w.$]+):\s*(.*)$", head)
        if mm:
            exc_type, message = mm.group(1).split(".")[-1], mm.group(2)
        else:
            exc_type = head.split(".")[-1] if head else ""

    elif lang == "go":
        exc_type = "panic"
        mm = re.search(r"^\s*panic:\s*(.*)$", text, re.M)
        message = mm.group(1).strip() if mm else ""
        # Frames: "pkg.func(args)" on one line, "\t/path/file.go:N +0x…" on the
        # next — pair them so frames carry the function name.
        prev = ""
        for raw in lines:
            fm = _GO_FILE_LINE.match(raw)
            if fm:
                pm = _GO_FUNC.match(prev.strip())
                frames.append({"file": fm.group("file"),
                               "line": int(fm.group("line")),
                               "func": pm.group("fn") if pm else ""})
                if len(frames) >= 25:
                    break
            if raw.strip():
                prev = raw

    elif lang == "ruby":
        frames = _mk_frames(_RUBY_FRAME.finditer(text))
        head = lines[0].strip()
        # Strip the "file:line:in `func': " prefix so message is the message.
        head = re.sub(r"^(?:[A-Za-z]:)?\S+?:\d+:in [`'][^'`]*['`]:\s*", "", head)
        mm = re.match(r"(?P<msg>.*?)\s*\((?P<type>[\w:]+)\)\s*$", head)
        if mm:
            exc_type, message = mm.group("type").split("::")[-1], mm.group("msg").strip()
        else:
            message = head

    elif lang == "dotnet":
        frames = _mk_frames(_NET_FRAME.finditer(text))
        head = lines[0].strip()
        head = re.sub(r"^Unhandled exception\.\s*", "", head)
        mm = re.match(r"([\w.]+(?:Exception|Error)):\s*(.*)$", head)
        if mm:
            exc_type, message = mm.group(1).split(".")[-1], mm.group(2)
        else:
            message = head
        # Inner exceptions ("---> Type: msg") — the LAST one is the root cause.
        if "--->" in text:
            seg = text.split("--->")[-1]
            im = re.match(r"\s*([\w.]+(?:Exception|Error)):\s*([^\r\n]*)", seg)
            if im:
                exc_type = im.group(1).split(".")[-1]
                message = im.group(2).strip()
        # Keep the outer message from dragging the inner header along.
        message = message.split("--->")[0].strip()

    elif lang == "rust":
        exc_type = "panic"
        om = _RUST_OLD.search(text)
        nm = _RUST_NEW.search(text)
        if om:
            message = om.group("msg")
            frames.append({"file": om.group("file"), "line": int(om.group("line")), "func": ""})
        elif nm:
            message = nm.group("msg").strip()
            frames.append({"file": nm.group("file"), "line": int(nm.group("line")), "func": ""})
        else:
            pm2 = re.search(r"panicked at (.*)", text)
            message = pm2.group(1).strip() if pm2 else lines[0].strip()
        for bm in _RUST_BT.finditer(text):
            frames.append({"file": bm.group("file"), "line": int(bm.group("line")),
                           "func": bm.group("fn")})
            if len(frames) >= 25:
                break
        # unwrap()/expect() on an `Err` value: <inner> — surface the INNER error
        # and, if it maps to a known cause, prefer that over the generic unwrap hint.
        im = re.search(r"on an? [`']?Err[`']? value:\s*(?P<inner>.+)$", message)
        if im:
            inner = im.group("inner").strip()
            inner_cause = probable_cause("", inner)
            pc_override = ("Unwrapped an Err Result — the operation returned an error "
                           f"instead of a value. Inner error: {inner[:200]}"
                           + (f" ({inner_cause})" if inner_cause else ""))

    elif lang == "php":
        hm = _PHP_HEAD.search(text)
        if hm:
            exc_type = hm.group("type").split("\\")[-1]
            message = hm.group("msg").strip()
            if hm.group("file"):
                frames.append({"file": hm.group("file"), "line": int(hm.group("line")), "func": ""})
        else:
            # "PHP Fatal error: msg in /file.php on line 7" (non-exception fatals)
            fm2 = re.search(r"PHP (?:Fatal error|Parse error|Warning|Notice):\s*(?P<msg>.*?)"
                            r"(?:\s+in\s+(?P<file>\S+?)(?::| on line )(?P<line>\d+))?\s*$",
                            text, re.M)
            if fm2:
                exc_type = "FatalError"
                message = fm2.group("msg").strip()
                if fm2.group("file"):
                    frames.append({"file": fm2.group("file"), "line": int(fm2.group("line")), "func": ""})
            else:
                message = lines[0].strip()
        for pf in _PHP_FRAME.finditer(text):
            frames.append({"file": pf.group("file"), "line": int(pf.group("line")),
                           "func": pf.group("func").strip()})
            if len(frames) >= 25:
                break

    elif lang == "cpp":
        tm = re.search(r"terminate called after throwing an instance of '(?P<t>[^']+)'", text)
        if tm:
            exc_type = tm.group("t").split("::")[-1]
        elif re.search(r"SIGSEGV|Segmentation fault", text):
            exc_type = "SIGSEGV"
        elif re.search(r"SIGABRT|abort", text):
            exc_type = "SIGABRT"
        wm = re.search(r"what\(\):\s*(.*)", text)
        if wm:
            message = wm.group(1).strip()
        else:
            sm = re.search(r"([^\n]*(?:SIGSEGV|Segmentation fault|SIGABRT)[^\n]*)", text)
            message = sm.group(1).strip() if sm else lines[0].strip()
        # gdb backtrace frames "#N 0x.. in func at file:line" (carry file:line).
        frames = _mk_frames(_CPP_GDB.finditer(text))
        # glog symbolized frames "@ 0x.. func" carry NO file:line — still emit a
        # frame with the function so the AI gets the call stack.
        if len(frames) < 25:
            for gm in _CPP_GLOG.finditer(text):
                fn = gm.group("func").strip()
                if fn and not fn.startswith("0x"):
                    frames.append({"file": "", "line": 0, "func": fn})
                    if len(frames) >= 25:
                        break

    else:
        # Unknown language — surface the first non-empty line as the message,
        # but let probable_cause scan a wider window (the real cause is often
        # on line 2+, e.g. Rust's "index out of bounds" detail line).
        message = lines[0].strip() if lines else ""
        cause_hint = text[:400]
        lang = lang or "unknown"

    return {
        "exception_type": exc_type,
        "message": message,
        "language": lang,
        "frames": frames,
        "probable_cause": pc_override or probable_cause(exc_type, cause_hint or message),
    }


# ── State diffing ─────────────────────────────────────────────────────────────

def flatten_state(obj, prefix: str = "", *, max_depth: int = 6,
                  max_keys: int = 400) -> dict:
    """Flatten a nested dict/list into dotted leaf keys — e.g.
    {"player": {"hp": 100, "pos": {"x": 3}}} → {"player.hp": 100, "player.pos.x": 3};
    lists become "items[0]", "items[1]". Only scalar leaves are kept (nested
    containers are recursed, scalars are compared). Bounded by depth + key count
    so a huge state.json can never blow up the frame. Never raises."""
    out: dict = {}

    def _rec(o, pfx, depth):
        if len(out) >= max_keys:
            return
        if depth > max_depth:
            out[pfx or "?"] = str(o)[:200]
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if len(out) >= max_keys:
                    return
                key = f"{pfx}.{k}" if pfx else str(k)
                _rec(v, key, depth + 1)
        elif isinstance(o, (list, tuple)):
            for i, v in enumerate(o):
                if len(out) >= max_keys or i >= 100:
                    return
                _rec(v, f"{pfx}[{i}]", depth + 1)
        else:
            # scalar leaf (or None) — clip long strings
            out[pfx or "?"] = (o[:200] + "…") if isinstance(o, str) and len(o) > 200 else o

    try:
        _rec(obj or {}, prefix, 0)
    except Exception:
        pass
    return out


def state_delta(prev: dict, cur: dict) -> dict:
    """Key-level diff between two program-state dicts — NESTED-aware. Both sides
    are flattened to dotted leaf keys (see flatten_state) so a change deep inside
    state.json (e.g. "player.pos.x") is surfaced precisely. Returns
    {added, removed, changed, *_count, truncated}. Bounded/token-aware."""
    fp = flatten_state(prev)
    fc = flatten_state(cur)
    added = {k: fc[k] for k in fc.keys() - fp.keys()}
    removed = {k: fp[k] for k in fp.keys() - fc.keys()}
    changed = {}
    for k in fc.keys() & fp.keys():
        if fc[k] != fp[k]:
            changed[k] = {"from": fp[k], "to": fc[k]}
    def _cap(d, n=30):
        return dict(list(d.items())[:n])
    truncated = (len(added) > 30 or len(removed) > 30 or len(changed) > 30)
    return {"added": _cap(added), "removed": _cap(removed), "changed": _cap(changed),
            "changed_count": len(changed), "added_count": len(added),
            "removed_count": len(removed), "truncated": truncated}


# ── Frame summarization (the AI triage layer) ─────────────────────────────────

def _safe_conf(v, default: float) -> float:
    """Coerce any confidence value ('high', None, '0.7', 0.9) to a 0..1 float —
    this module must never raise into a caller."""
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


def summarize_frame(*, running: bool, error: Optional[dict], anomaly: Optional[dict],
                    stuck: bool, stats: Optional[dict], state_delta_d: Optional[dict],
                    action_count_delta: int = 0) -> dict:
    """Produce {summary, recommended_next, tags, confidence} for a frame.
    Rule-based and deterministic (auditable, no model call). The AI reads these
    to triage fast, then drills into the structured error/anomaly/log fields."""
    tags: list[str] = []
    error = error or {}
    anomaly = anomaly or {}
    sd = state_delta_d or {}

    if not running:
        tags.append("not_running")
        return {
            "summary": "Target program is NOT running (crashed, exited, or not started).",
            "recommended_next": ("Check program.log_at_capture for the last lines before "
                                 "exit and av_program_status; if it crashed, see error/."),
            "tags": tags, "confidence": 0.3,
        }

    if error and (error.get("message") or error.get("exception_type")):
        tags.append("error")
        etype = error.get("exception_type") or "error"
        emsg = (error.get("message") or "")[:120]
        cause = error.get("probable_cause") or ""
        occ = error.get("occurrence_count") or 1
        if occ > 1:
            tags.append("recurring")
        conf = 0.2
        summary = f"{etype}: {emsg}".strip(": ")
        if occ > 1:
            summary += f"  (seen {occ}× this session)"
        rec = "Inspect error.frames (file:line) and error.probable_cause"
        if cause:
            rec += f" — likely: {cause}"
        rec += ". Correlate with av_actions_around_frame(seq) for what led up to it."
        return {"summary": summary or "An error was detected in the log.",
                "recommended_next": rec, "tags": tags, "confidence": conf}

    if stuck or (anomaly.get("type") == "screen_stuck"):
        tags.append("stuck")
        return {
            "summary": (anomaly.get("description")
                        or "Screen unchanged for several consecutive frames — possibly hung."),
            "recommended_next": ("Compare recent frames' images; check if the program is "
                                 "waiting (av_program_log) or genuinely frozen (av_program_status)."),
            "tags": tags, "confidence": 0.5,
        }

    if anomaly.get("detected"):
        tags.append("anomaly")
        return {
            "summary": anomaly.get("description") or f"Anomaly: {anomaly.get('type')}",
            "recommended_next": "Review anomaly.evidence and the surrounding log window.",
            "tags": tags, "confidence": _safe_conf(anomaly.get("confidence"), 0.6),
        }

    # Healthy — note any meaningful state movement so the AI knows it's live.
    tags.append("healthy")
    moved = (sd.get("changed_count", 0) + sd.get("added_count", 0)) if sd else 0
    if moved:
        tags.append("state_changed")
        summary = f"Running normally; {moved} state field(s) changed since last frame."
    elif action_count_delta:
        summary = f"Running normally; {action_count_delta:+d} new action(s) since last frame."
    else:
        summary = "Running normally; no notable change since the last frame."
    return {"summary": summary,
            "recommended_next": ("No action needed. Skim program.stats / recent_actions; "
                                 "raise the capture rate if you need finer detail."),
            "tags": tags, "confidence": 0.85}
