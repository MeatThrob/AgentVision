"""
Developer-tool log adapters (BATCH 3)
================================================================================
The formats a developer's own terminal produces: language crash traces, build
tools, package managers, syscall traces, and editor/debugger wire protocols.
These are the highest-value formats for AgentVision's debugging mission — a
Python traceback or Node stack becomes a category="error" event that the
bridge's failure detector and bookmarks fire on.

Formats: python_traceback, node_stack, strace, cargo_build, vite_build,
webpack_stats, npm_debug, jsonrpc_wire (LSP + DAP), helm_debug.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp,  # noqa: F401
                      ratio_detect, multiline_ratio_detect, block_ratio,
                      split_any)


def _unescape_wire(s: str) -> str:
    """JSON-RPC wire captures are often pasted with LITERAL \\r\\n escapes
    (copying out of a JSON string). Normalize those to real newlines so the
    same adapter reads both renderings."""
    if "\\r\\n" in s or ("\\n" in s and "\n" not in s):
        return s.replace("\\r\\n", "\r\n").replace("\\n", "\n")
    return s


# ── Python traceback ─────────────────────────────────────────────────────────
#   Traceback (most recent call last):
#     File "/app/main.py", line 42, in <module>
#       do_work()
#   ValueError: boom
class PythonTracebackAdapter(LogAdapter):
    name = "python_traceback"
    language = "python"
    _HEADER = "Traceback (most recent call last):"
    _FRAME = re.compile(r'^\s+File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>.+))?$')
    _EXC = re.compile(r"^(?P<etype>[A-Za-z_][\w.]*(?:Error|Exception|Warning|Interrupt|"
                      r"Exit|StopIteration|StopAsyncIteration))(?::\s*(?P<msg>.*))?$")

    def detect(self, sample_lines):
        # An element hits when it IS a traceback: starts with the header, or is
        # a File-frame line. A bare "SomeError: msg" line alone is deliberately
        # NOT enough (too generic — other logs embed exception names).
        def hit(el):
            s = str(el).strip()
            if s.startswith(self._HEADER):
                return True
            subs = [x for x in str(el).splitlines() if x.strip()]
            frames = sum(1 for x in subs if self._FRAME.match(x))
            return frames >= 1 and (frames / len(subs)) >= 0.3 if subs else False
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if "\n" in s:                      # whole block → one consolidated event
            subs = [x for x in s.splitlines() if x.strip()]
            frames = [self._FRAME.match(x) for x in subs]
            frames = [m for m in frames if m]
            exc = None
            for x in reversed(subs):
                m = self._EXC.match(x.strip()) if not x.startswith(" ") else None
                if m:
                    exc = m
                    break
            msg = (f'{exc.group("etype")}: {exc.group("msg") or ""}'.strip()
                   if exc else "Python traceback")
            last = frames[-1] if frames else None
            return self._event(
                level="error", message=msg, category="error",
                source=last.group("file") if last else "python",
                fields={"exception": exc.group("etype") if exc else None,
                        "frames": len(frames),
                        "file": last.group("file") if last else None,
                        "line": int(last.group("line")) if last else None},
                raw=line)
        if s.strip().startswith(self._HEADER):
            return self._event(level="error", message=s.strip(), category="error",
                               source="python", fields={"marker": "traceback_start"},
                               raw=line)
        m = self._FRAME.match(s)
        if m:
            g = m.groupdict()
            return self._event(level="error", message=s.strip(), category="error",
                               source=g["file"],
                               fields={"file": g["file"], "line": int(g["line"]),
                                       "func": g.get("func")}, raw=line)
        m = self._EXC.match(s.strip())
        if m and not s.startswith(" "):
            g = m.groupdict()
            return self._event(level="error", message=s.strip(), category="error",
                               source="python", fields={"exception": g["etype"]},
                               raw=line)
        return None


# ── Node.js / V8 stack trace ─────────────────────────────────────────────────
#   Error: boom
#       at Object.<anonymous> (/app/server.js:12:9)
#       at Module._compile (node:internal/modules/cjs/loader:1254:14)
class NodeStackAdapter(LogAdapter):
    name = "node_stack"
    language = "node"
    _AT = re.compile(r"^\s*at\s+(?:async\s+)?(?:new\s+)?"
                     r"(?:(?P<func>[\w.$<>\[\] ]+?)\s+\((?P<loc>[^)]*:\d+:\d+|native)\)"
                     r"|(?P<bare>(?:node:)?\S+:\d+:\d+))\s*$")
    _ERR = re.compile(r"^(?P<etype>[A-Z][\w]*(?:Error|Exception))(?::\s*(?P<msg>.*))?$")
    _SRC = re.compile(r"^\S+\.(?:js|mjs|cjs|ts|tsx|jsx):\d+$")
    _THROW = re.compile(r"^\s*throw\s+new?\s")
    _CARET = re.compile(r"^\s*\^+\s*$")

    def detect(self, sample_lines):
        def hit(el):
            subs = [x for x in str(el).splitlines() if x.strip()]
            if not subs:
                return False
            ats = sum(1 for x in subs if self._AT.match(x))
            if ats < 1:
                return False
            other = sum(1 for x in subs
                        if self._ERR.match(x.strip()) or self._SRC.match(x.strip())
                        or self._THROW.match(x) or self._CARET.match(x))
            return (ats + other) / len(subs) >= 0.5
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if "\n" in s:                      # whole block → one consolidated event
            subs = [x for x in s.splitlines() if x.strip()]
            err = next((self._ERR.match(x.strip()) for x in subs
                        if self._ERR.match(x.strip())), None)
            ats = [self._AT.match(x) for x in subs]
            ats = [m for m in ats if m]
            msg = (f'{err.group("etype")}: {err.group("msg") or ""}'.strip()
                   if err else "Node.js stack trace")
            top = ats[0] if ats else None
            return self._event(
                level="error", message=msg, category="error",
                source=(top.group("func") or top.group("bare") or "").strip() if top else "node",
                fields={"exception": err.group("etype") if err else None,
                        "frames": len(ats),
                        "location": (top.group("loc") or top.group("bare")) if top else None},
                raw=line)
        m = self._AT.match(s)
        if m:
            g = m.groupdict()
            return self._event(level="error", message=s.strip(), category="error",
                               source=(g.get("func") or "").strip() or "node",
                               fields={"location": g.get("loc") or g.get("bare"),
                                       "func": (g.get("func") or "").strip() or None},
                               raw=line)
        m = self._ERR.match(s.strip())
        if m:
            return self._event(level="error", message=s.strip(), category="error",
                               source="node", fields={"exception": m.group("etype")},
                               raw=line)
        return None


# ── strace syscall trace ─────────────────────────────────────────────────────
#   openat(AT_FDCWD, "/etc/ld.so.cache", O_RDONLY|O_CLOEXEC) = 3
#   connect(5, {...}, 16) = -1 ECONNREFUSED (Connection refused)
class StraceAdapter(LogAdapter):
    name = "strace"
    language = "any"
    _CALL = re.compile(
        r"^(?:(?P<pid>\d+)\s+)?(?:(?P<ts>\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+)?"
        r"(?P<sys>[a-z_][\w]*)\((?P<args>.*)\)\s*=\s*"
        r"(?P<ret>-?\d+|0x[0-9a-fA-F]+|\?)"
        r"(?:\s+(?P<errno>E[A-Z0-9_]+)\s*\((?P<desc>[^)]*)\))?\s*$")
    _META = re.compile(
        r"^(?:(?P<pid>\d+)\s+)?(?:---\s+(?P<sig>SIG[A-Z0-9]+)\b.*---"
        r"|\+\+\+\s+(?P<exit>exited with \d+|killed by SIG[A-Z0-9]+).*\+\+\+)\s*$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines,
            lambda ln: bool(self._CALL.match(ln.strip()) or self._META.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._CALL.match(s)
        if m:
            g = m.groupdict()
            failed = g["ret"].startswith("-") and g.get("errno")
            level = "warn" if failed else "debug"
            fields = {"syscall": g["sys"], "return": g["ret"]}
            if g.get("errno"):
                fields["errno"] = g["errno"]
                fields["errno_text"] = g.get("desc")
            if g.get("pid"):
                fields["pid"] = int(g["pid"])
            return self._event(level=level, message=s, source=g["sys"],
                               ts_ms=parse_timestamp(g["ts"]) if g.get("ts") else None,
                               fields=fields, raw=line)
        m = self._META.match(s)
        if m:
            g = m.groupdict()
            sig = g.get("sig")
            exit_ = g.get("exit") or ""
            fatal = bool(sig) or "killed" in exit_
            return self._event(level="fatal" if fatal else "info", message=s,
                               source="strace",
                               fields={"signal": sig, "exit": exit_ or None,
                                       "pid": int(g["pid"]) if g.get("pid") else None},
                               raw=line)
        return None


# ── Cargo human build output ─────────────────────────────────────────────────
#      Compiling serde v1.0.195
#   warning: unused variable: `x`
#     --> src/main.rs:4:9
class CargoBuildAdapter(LogAdapter):
    name = "cargo_build"
    language = "rust"
    _STATUS = re.compile(
        r"^\s{0,10}(?P<verb>Compiling|Checking|Building|Finished|Downloading|Downloaded|"
        r"Updating|Adding|Removing|Installing|Installed|Fresh|Running|Doc-tests|"
        r"Documenting|Packaging|Verifying|Uploading)\s+(?P<rest>.*)$")
    # A status verb alone is too generic (FreeBSD periodic mail says "Checking
    # special files…") — require a cargo-ish token in the rest: a crate version
    # ("serde v1.0.195"), target(s)/target/ paths, crates.io index, a backtick
    # command, or a plain X.Y duration/version.
    _CARGOISH = re.compile(r"\bv?\d+\.\d+|\btarget\(s\)|\btarget/|\bcrates?\b|\bindex\b|`")
    _DIAG = re.compile(r"^(?P<lvl>warning|error)(?P<code>\[E\d+\])?:\s*(?P<msg>.*)$")
    _LOC = re.compile(r"^\s*-->\s+(?P<loc>\S+:\d+:\d+)\s*$")

    def _status(self, ln: str):
        m = self._STATUS.match(ln)
        return m if (m and self._CARGOISH.search(m.group("rest"))) else None

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines,
            lambda ln: bool(self._status(ln) or self._DIAG.match(ln.strip())
                            or self._LOC.match(ln)))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        m = self._status(s)
        if m:
            return self._event(level="info", message=f'{m.group("verb")} {m.group("rest")}'.strip(),
                               source="cargo", fields={"phase": m.group("verb")}, raw=line)
        m = self._DIAG.match(s.strip())
        if m:
            g = m.groupdict()
            return self._event(level=g["lvl"], message=g["msg"], source="rustc",
                               fields={"code": (g.get("code") or "").strip("[]") or None},
                               raw=line)
        m = self._LOC.match(s)
        if m:
            return self._event(level="", message=s.strip(), source="rustc",
                               fields={"location": m.group("loc")}, raw=line)
        return None


# ── Vite build output ────────────────────────────────────────────────────────
#   vite v7.1.7 building for production...
#   ✓ 963 modules transformed.
#   dist/assets/index-a1b2c3.js   142.31 kB │ gzip: 45.67 kB
class ViteBuildAdapter(LogAdapter):
    name = "vite_build"
    language = "node"
    _HEAD = re.compile(r"^vite v[\d][\w.\-]*\s+(building|ready|dev server)")
    _TICK = re.compile(r"^[✓✗]\s+(?P<msg>.*)$")
    _ASSET = re.compile(r"^(?P<path>(?:dist|build)/\S+)\s+(?P<size>[\d.,]+\s*[kKmM]?B)\b")
    _MISC = re.compile(r"^(?:\[vite\]|Build failed|error during build|"
                       r"\s*➜\s|transforming|rendering chunks)")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines,
            lambda ln: bool(self._HEAD.match(ln.strip()) or self._TICK.match(ln.strip())
                            or self._ASSET.match(ln.strip()) or self._MISC.match(ln)))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if not s:
            return None
        if self._HEAD.match(s):
            return self._event(level="info", message=s, source="vite", raw=line)
        m = self._TICK.match(s)
        if m:
            failed = s.startswith("✗")
            return self._event(level="error" if failed else "info",
                               message=m.group("msg"), source="vite", raw=line)
        m = self._ASSET.match(s)
        if m:
            return self._event(level="info", message=s, source="vite",
                               fields={"asset": m.group("path"), "size": m.group("size")},
                               raw=line)
        if self._MISC.match(s):
            low = s.lower()
            level = "error" if ("failed" in low or "error" in low) else "info"
            return self._event(level=level, message=s, source="vite", raw=line)
        return None


# ── webpack stats output ─────────────────────────────────────────────────────
#   asset main.js 1.24 MiB [emitted] [minimized] (name: main)
#   webpack 5.89.0 compiled with 1 warning in 3421 ms
#   ERROR in ./src/index.js 5:0-34
class WebpackStatsAdapter(LogAdapter):
    name = "webpack_stats"
    language = "node"
    _ASSET = re.compile(r"^asset\s+\S+\s+[\d.,]+\s*(bytes|[KMG]iB)\b")
    _MODULES = re.compile(r"^(?:orphan|runtime|cacheable|javascript|css|asset)\s+modules\b"
                          r"|^modules by path\b|^\./\S+\s+[\d.,]+\s*(bytes|[KMG]iB)\b")
    _COMPILED = re.compile(r"^webpack\s+\d+\.\d+\.\d+\s+compiled\s+(?P<how>.*?)\s*"
                           r"(?:in\s+(?P<ms>\d+)\s*ms)?$")
    _PROBLEM = re.compile(r"^(?P<lvl>ERROR|WARNING)\s+in\s+(?P<where>.*)$")
    _MODNF = re.compile(r"^Module not found\b")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines,
            lambda ln: bool(self._ASSET.match(ln.strip()) or self._MODULES.match(ln.strip())
                            or self._COMPILED.match(ln.strip())
                            or self._PROBLEM.match(ln.strip()) or self._MODNF.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if not s:
            return None
        m = self._PROBLEM.match(s)
        if m:
            return self._event(level="error" if m.group("lvl") == "ERROR" else "warn",
                               message=s, source="webpack",
                               fields={"module": m.group("where")}, raw=line)
        if self._MODNF.match(s):
            return self._event(level="error", message=s, source="webpack", raw=line)
        m = self._COMPILED.match(s)
        if m:
            how = m.group("how") or ""
            level = ("error" if "error" in how else "warn" if "warning" in how else "info")
            return self._event(level=level, message=s, source="webpack",
                               fields={"duration_ms": int(m.group("ms")) if m.group("ms") else None},
                               raw=line)
        if self._ASSET.match(s) or self._MODULES.match(s):
            return self._event(level="info", message=s, source="webpack", raw=line)
        return None


# ── npm debug log (~/.npm/_logs/*-debug-0.log) ───────────────────────────────
#   0 verbose cli /usr/bin/node /usr/bin/npm
#   189 error code ELIFECYCLE
class NpmDebugAdapter(LogAdapter):
    name = "npm_debug"
    language = "node"
    _RE = re.compile(r"^(?P<seq>\d+)\s+(?P<lvl>silly|verbose|timing|info|notice|http|"
                     r"warn|error)\s+(?P<msg>.*)$")
    _LVL = {"silly": "trace", "verbose": "debug", "timing": "debug", "http": "info",
            "info": "info", "notice": "info", "warn": "warn", "error": "error"}

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=self._LVL.get(g["lvl"], g["lvl"]), message=g["msg"],
                           source="npm", fields={"seq": int(g["seq"])}, raw=line)


# ── JSON-RPC wire stream: LSP + DAP (Content-Length framed) ──────────────────
#   Content-Length: 126\r\n\r\n{"jsonrpc":"2.0","id":1,"method":"textDocument/…"}
#   Content-Length: 82\r\n\r\n{"seq":153,"type":"request","command":"next",…}
class JsonRpcWireAdapter(LogAdapter):
    name = "jsonrpc_wire"
    language = "any"
    _HDR = re.compile(r"Content-Length:\s*\d+", re.IGNORECASE)

    def detect(self, sample_lines):
        def hit(el):
            s = _unescape_wire(str(el))
            if not self._HDR.search(s):
                return False
            return '"jsonrpc"' in s or '"seq"' in s or '"method"' in s or '"command"' in s
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = _unescape_wire(line.rstrip("\r\n"))
        brace = s.find("{")
        if brace < 0:
            if self._HDR.search(s):        # a bare header line in a split stream
                return self._event(level="debug", message=s.strip(),
                                   source="jsonrpc", raw=line)
            return None
        payload = s[brace:]
        rec = None
        try:
            rec = json.loads(payload)
        except Exception:
            pass
        if not isinstance(rec, dict):
            return self._event(level="debug", message=payload[:200],
                               source="jsonrpc", raw=line)
        proto = "lsp" if "jsonrpc" in rec else "dap" if "seq" in rec else "jsonrpc"
        what = rec.get("method") or rec.get("command") or rec.get("event") or rec.get("type", "")
        failed = ("error" in rec) or (rec.get("type") == "response" and rec.get("success") is False)
        fields = {"protocol": proto, "type": rec.get("type"),
                  "method": rec.get("method") or rec.get("command"),
                  "id": rec.get("id", rec.get("seq"))}
        if failed:
            err = rec.get("error") or {}
            fields["error"] = err.get("message") if isinstance(err, dict) else str(err)
        return self._event(level="error" if failed else "debug",
                           message=f"{proto} {what}".strip(), source=proto,
                           fields=fields, raw=line)


# ── Helm --debug output ──────────────────────────────────────────────────────
#   install.go:178: [debug] Original chart version: ""
class HelmDebugAdapter(LogAdapter):
    name = "helm_debug"
    language = "go"
    _RE = re.compile(r"^(?P<file>[\w./\-]+\.go):(?P<line>\d+):\s+"
                     r"\[(?P<lvl>debug|info|warning|error)\]\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["lvl"], message=g["msg"], source=g["file"],
                           fields={"file": g["file"], "line": int(g["line"])}, raw=line)


# ═══════════════════════════ BATCH 5 ═════════════════════════════════════════

# ── pip verbose install log ────────────────────────────────────────────────────
#   Collecting requests
#     Using cached requests-2.31.0-py3-none-any.whl (62 kB)
#   Successfully installed requests-2.31.0 urllib3-2.1.0
class PipInstallAdapter(LogAdapter):
    name = "pip_install"
    language = "python"
    _VERB = re.compile(
        r"^\s*(Collecting |Downloading |Using cached |Installing collected packages|"
        r"Successfully installed |Successfully uninstalled |Requirement already satisfied|"
        r"Building wheels? |Created wheel |Stored in directory|Found existing installation|"
        r"Attempting uninstall|Uninstalling |Preparing metadata |Installing build dependencies|"
        r"Getting requirements to build|Looking in indexes|Obtaining |Running setup\.py|"
        r"Resolved \S+ to commit|Cloning )")
    _ERR = re.compile(r"^\s*(ERROR|WARNING):\s+(?P<msg>.*)$")

    def _block_line(self, s: str) -> bool:
        return bool(self._VERB.match(s) or self._ERR.match(s))

    def detect(self, sample_lines):
        score = ratio_detect(
            sample_lines,
            lambda ln: block_ratio(ln, lambda x: self._block_line(x)))
        if score <= 0.0:
            return 0.0
        # ANCHOR GATE: bare "ERROR:/WARNING: message" lines are shared
        # vocabulary (Bazel, JUL bodies, generic CLIs). Claim them only when
        # the sample also shows pip's own verb lines (Collecting/Downloading/
        # Successfully installed/…); an _ERR-only sample is not a pip log.
        has_verb = any(self._VERB.match(x)
                       for ln in sample_lines for x in split_any(str(ln)))
        return score if has_verb else 0.0

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        st = s.strip()
        m = self._ERR.match(st)
        if m:
            level = "error" if st.startswith("ERROR") else "warn"
            return self._event(level=level, message=m.group("msg"), source="pip",
                               raw=line)
        if not self._VERB.match(s):
            return None
        level = "info"
        fields = None
        pm = re.match(r"^\s*(?:Collecting|Downloading|Using cached)\s+(\S+)", s)
        if pm:
            fields = {"package": pm.group(1)}
        return self._event(level=level, message=st, source="pip",
                           fields=fields, raw=line)


# ── PM2 multiplexed process output ('pm2 logs' / --time) ─────────────────────
#   0|api    | 2026-07-21T15:02:13: Server listening on :3000
class Pm2Adapter(LogAdapter):
    name = "pm2"
    language = "node"
    _RE = re.compile(
        r"^(?P<pid>\d+)\|(?P<app>\S+)\s*\|\s(?P<rest>.*)$")
    _TS = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?):\s+"
        r"(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda ln: block_ratio(ln, lambda x: bool(self._RE.match(x.strip()))))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        rest = g["rest"]
        ts_ms = None
        tm = self._TS.match(rest)
        if tm:
            ts_ms = parse_timestamp(tm.group("ts"))
            rest = tm.group("msg")
        low = rest.lower()
        level = ("error" if any(w in low for w in ("error", "exception", "fatal"))
                 else "")
        return self._event(level=level, message=rest, source=f'pm2.{g["app"]}',
                           ts_ms=ts_ms,
                           fields={"pm2_id": int(g["pid"]), "app": g["app"]},
                           raw=line)


# ── Deterministic-replay seed dump (game engines) ──────────────────────────────
#   [Replay] Recording started seed=0xDEADBEEF tick=0 build=1.4.2
class ReplaySeedAdapter(LogAdapter):
    name = "replay_seed"
    language = "any"
    _SEED = re.compile(r"\bseed=(?P<seed>0x[0-9A-Fa-f]+|\d+)\b")
    _CTX = re.compile(r"(?i)\b(tick|frame)=\d+|\breplay\b|\bchecksum\b|\bdesync\b")

    def detect(self, sample_lines):
        def ok(ln):
            s = ln.strip()
            return bool(self._SEED.search(s) and self._CTX.search(s))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        sm = self._SEED.search(s)
        if not sm:
            return None
        fields = {"seed": sm.group("seed")}
        for key in ("tick", "frame", "build", "checksum"):
            km = re.search(rf"\b{key}=(\S+)", s)
            if km:
                fields[key] = km.group(1)
        level = "error" if re.search(r"(?i)desync|mismatch", s) else "info"
        return self._event(level=level, message=s, source="replay",
                           category="event" if level == "info" else "error",
                           fields=fields, raw=line)


# ── PyTorch Lightning seed_everything ──────────────────────────────────────────
#   Global seed set to 42        /  [rank: 0] Seed set to 42
class LightningSeedAdapter(LogAdapter):
    name = "lightning_seed"
    language = "python"
    _RE = re.compile(
        r"^(?:\[rank:?\s*(?P<rank>\d+)\]\s+)?(?:Global\s+)?[Ss]eed set to (?P<seed>\d+)\s*$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {"seed": int(g["seed"])}
        if g["rank"] is not None:
            fields["rank"] = int(g["rank"])
        return self._event(level="info", message=line.strip(),
                           source="lightning.seed", fields=fields, raw=line)


# ── Registration ─────────────────────────────────────────────────────────────
for _a in (PythonTracebackAdapter(), NodeStackAdapter(), StraceAdapter(),
           CargoBuildAdapter(), ViteBuildAdapter(), WebpackStatsAdapter(),
           NpmDebugAdapter(), JsonRpcWireAdapter(), HelmDebugAdapter(),
           # batch 5
           PipInstallAdapter(), Pm2Adapter(), ReplaySeedAdapter(),
           LightningSeedAdapter()):
    register_adapter(_a)


# ═════════════════════════════════════════════════════════════════════════════
# BATCH 6 — build tools, IDE/daemon logs, package managers, VCS tracing
# ═════════════════════════════════════════════════════════════════════════════
from ._common import split_any  # noqa: E402


# ── Gradle build console output ───────────────────────────────────────────────
#   > Task :app:compileJava
#   BUILD SUCCESSFUL in 12s / FAILURE: Build failed with an exception.
class GradleBuildAdapter(LogAdapter):
    name = "gradle_build"
    language = "java"
    _MARK = re.compile(
        r"^(> Task :|> Configure project :|BUILD (?:SUCCESSFUL|FAILED) in |"
        r"\d+ actionable tasks?: |FAILURE: Build failed|\* What went wrong:|"
        r"\* Where:|\* Try:|> Run with --)")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            if not subs:
                return False
            n = sum(1 for x in subs if self._MARK.match(x.strip()))
            return n >= 1 and n / len(subs) >= 0.5
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if "\n" in s:
            for x in s.splitlines():
                if x.strip() and self._MARK.match(x.strip()):
                    ev = self.parse_line(x)
                    if ev:
                        ev["raw"] = line
                        return ev
            s = s.splitlines()[0] if s.splitlines() else s
        t = s.strip()
        if not t:
            return None
        level, fields = "info", {}
        m = re.match(r"^> Task (:{1}[\w:.\-]+)(?:\s+(\S+))?", t)
        if m:
            fields = {"task": m.group(1)}
            if m.group(2):
                fields["outcome"] = m.group(2)
                if m.group(2) == "FAILED":
                    level = "error"
        elif t.startswith("BUILD FAILED") or t.startswith("FAILURE:"):
            level = "error"
        elif t.startswith("* What went wrong") or t.startswith("* Where:"):
            level = "error"
        return self._event(level=level, message=t, source="gradle",
                           fields=fields or None, raw=line)


# ── Gradle daemon log (Logback: ISO±tz [LEVEL] [logger] message) ─────────────
#   2021-08-12T12:01:50.755+0200 [DEBUG] [org.gradle.internal…] Adding IP addresses
class GradleDaemonAdapter(LogAdapter):
    name = "gradle_daemon"
    language = "java"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[.,]\d+(?:[+-]\d{4}|Z)?)\s+"
        r"\[(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|LIFECYCLE|QUIET)\]\s+"
        r"\[(?P<logger>[\w.$ \-]+)\]\s?(?P<msg>.*)$")
    _LVL = {"LIFECYCLE": "info", "QUIET": "info"}

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=self._LVL.get(g["level"], g["level"]),
                           message=g["msg"], source=g["logger"],
                           ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── Yarn Berry (v2+) output — ➤ YN#### code-prefixed lines ───────────────────
#   ➤ YN0000: ┌ Resolution step
class YarnBerryAdapter(LogAdapter):
    name = "yarn_berry"
    language = "node"
    _RE = re.compile(r"^➤?\s*YN(?P<code>\d{4}):\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            if not subs:
                return False
            n = sum(1 for x in subs if self._RE.match(x.strip()))
            return n >= 1 and n / len(subs) >= 0.5
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if "\n" in s:
            s = next((x for x in s.splitlines() if self._RE.match(x.strip())), s)
        m = self._RE.match(s.strip())
        if not m:
            return None
        code = m.group("code")
        msg = m.group("msg").lstrip("┌│└├─ ").strip()
        # YN0000 = unnamed/info; YN0001 = EXCEPTION; YN0002.. = named problems.
        level = ("error" if code == "0001"
                 else "warn" if code not in ("0000",) else "info")
        if "Done with warnings" in msg:
            level = "warn"
        elif re.search(r"Failed with errors|error", msg, re.IGNORECASE):
            level = "error"
        return self._event(level=level, message=msg or m.group("msg"),
                           source="yarn", fields={"code": f"YN{code}"}, raw=line)


# ── Yarn Classic (v1) yarn-error.log ─────────────────────────────────────────
#   Arguments: / PATH: / Yarn version: / Trace: sections then a JS stack
class YarnClassicAdapter(LogAdapter):
    name = "yarn_classic"
    language = "node"
    _SECTIONS = ("Arguments:", "PATH:", "Yarn version:", "Node version:",
                 "Platform:", "Trace:", "npm manifest:", "yarn manifest:",
                 "Lockfile:")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            heads = sum(1 for x in subs
                        if any(x.strip().startswith(sec) for sec in self._SECTIONS))
            branded = any("yarnpkg.com" in x or "/yarn" in x or "Yarn version:" in x
                          for x in subs)
            return heads >= 1 and branded
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        subs = split_any(s)
        if not subs:
            return None
        err = next((x.strip() for x in subs if x.strip().startswith("Error")), "")
        fields = {}
        for x in subs:
            t = x.strip()
            for sec in ("Yarn version:", "Node version:", "Platform:"):
                if t.startswith(sec):
                    fields[sec[:-1].lower().replace(" ", "_")] = t[len(sec):].strip()
        msg = err or subs[0].strip()
        return self._event(level="error" if err or "Trace:" in s else "info",
                           message=msg, source="yarn", fields=fields or None,
                           raw=line)


# ── pnpm install/add console output ───────────────────────────────────────────
#   Progress: resolved 210, reused 190, downloaded 20, added 210
class PnpmAdapter(LogAdapter):
    name = "pnpm"
    language = "node"
    _MARK = re.compile(
        r"^(Progress: resolved \d+|Packages: [+-]\d+|Already up to date|"
        r"dependencies:$|devDependencies:$|[+\-]{4,}$|"
        r"[+-] [\w@/.\-]+ \d+\.\d+|ERR_PNPM_|WARN\b|Done in [\d.]+m?s)")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            n = sum(1 for x in subs if self._MARK.match(x.strip()))
            return bool(subs) and (
                n >= 2 or any(x.strip().startswith(("Progress: resolved",
                                                    "ERR_PNPM_")) for x in subs))
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if "\n" in s:
            s = next((x for x in s.splitlines() if self._MARK.match(x.strip())),
                     s.splitlines()[0])
        t = s.strip()
        if not t:
            return None
        level = ("error" if t.startswith("ERR_PNPM_") or " ERR_PNPM_" in t
                 else "warn" if t.startswith("WARN") else "info")
        fields = {}
        m = re.match(r"^Progress: resolved (\d+), reused (\d+), downloaded (\d+), added (\d+)", t)
        if m:
            fields = {"resolved": int(m.group(1)), "reused": int(m.group(2)),
                      "downloaded": int(m.group(3)), "added": int(m.group(4))}
        return self._event(level=level, message=t, source="pnpm",
                           fields=fields or None, raw=line)


# ── uv (Astral) pip-workflow output ──────────────────────────────────────────
#   Resolved 34 packages in 12ms / Installed 34 packages in 89ms /  + requests==2.31.0
class UvAdapter(LogAdapter):
    name = "uv"
    language = "python"
    _SUMMARY = re.compile(
        r"^(?P<verb>Resolved|Downloaded|Prepared|Installed|Uninstalled|Audited)\s+"
        r"(?P<n>\d+) packages? in (?P<dur>\S+)$")
    _DIFF = re.compile(r"^\s*(?P<sign>[+-])\s+(?P<pkg>[\w.\-]+)==(?P<ver>\S+)$")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            summ = sum(1 for x in subs if self._SUMMARY.match(x.strip()))
            diff = sum(1 for x in subs if self._DIFF.match(x))
            return (summ >= 2) or (summ >= 1 and diff >= 1)
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if "\n" in s:
            s = next((x for x in s.splitlines() if self._SUMMARY.match(x.strip())),
                     s.splitlines()[0])
        m = self._SUMMARY.match(s.strip())
        if m:
            return self._event(level="info", message=s.strip(), source="uv",
                               fields={"verb": m.group("verb"),
                                       "packages": int(m.group("n")),
                                       "duration": m.group("dur")}, raw=line)
        dm = self._DIFF.match(s)
        if dm:
            return self._event(level="info", message=s.strip(), source="uv",
                               fields={"package": dm.group("pkg"),
                                       "version": dm.group("ver"),
                                       "change": "add" if dm.group("sign") == "+" else "remove"},
                               raw=line)
        if re.match(r"^\s*(error|warning)[:\s]", s, re.IGNORECASE):
            lvl = "error" if s.lstrip().lower().startswith("error") else "warn"
            return self._event(level=lvl, message=s.strip(), source="uv", raw=line)
        return self._event(level="info", message=s.strip(), source="uv", raw=line) \
            if s.strip() else None


# ── IntelliJ-platform idea.log (Android Studio, PyCharm, …) ──────────────────
#   2026-07-20 10:15:01,123 [   4508]   INFO - #c.i.i.StartupUtil - JVM: 17.0.10
class IdeaLogAdapter(LogAdapter):
    name = "idea_log"
    language = "java"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+"
        r"\[\s*(?P<ms>\d+)\]\s+(?P<level>TRACE|DEBUG|FINE|INFO|WARN|ERROR|SEVERE)\s+-\s+"
        r"(?P<logger>#?[\w.$#]+)\s+-\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"],
                           source=g["logger"].lstrip("#"),
                           ts_ms=parse_timestamp(g["ts"]),
                           fields={"uptime_ms": int(g["ms"])}, raw=line)


# ── Jupyter / IPython kernel & server apps ────────────────────────────────────
#   [IPKernelApp] WARNING | No such comm target registered: …
class JupyterAdapter(LogAdapter):
    name = "jupyter"
    language = "python"
    _RE = re.compile(
        r"^\[(?P<app>IPKernelApp|KernelApp|ServerApp|NotebookApp|LabApp|"
        r"JupyterHub|SingleUserNotebookApp)\]\s+"
        r"(?:(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+\|\s?)?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"] or "info", message=g["msg"],
                           source=f'jupyter.{g["app"]}', raw=line)


# ── TeamCity service messages ─────────────────────────────────────────────────
#   ##teamcity[testStarted name='MyTest.test1' captureStandardOutput='true']
class TeamCityAdapter(LogAdapter):
    name = "teamcity"
    language = "any"
    _RE = re.compile(r"^##teamcity\[(?P<name>\w+)(?P<attrs>(?:\s+[\w.]+='(?:[^'|]|\|.)*')*)\s*\]$")
    _ATTR = re.compile(r"([\w.]+)='((?:[^'|]|\|.)*)'")
    _ERRORISH = {"testFailed", "buildProblem", "message"}

    @staticmethod
    def _unescape(v: str) -> str:
        return (v.replace("|n", "\n").replace("|r", "\r").replace("|'", "'")
                 .replace("|]", "]").replace("|[", "[").replace("||", "|"))

    def detect(self, sample_lines):
        return multiline_ratio_detect(
            sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        name = m.group("name")
        attrs = {k: self._unescape(v) for k, v in self._ATTR.findall(m.group("attrs") or "")}
        level = "info"
        if name == "testFailed" or name == "buildProblem":
            level = "error"
        elif name == "message" and attrs.get("status") in ("ERROR", "FAILURE"):
            level = "error"
        elif name == "message" and attrs.get("status") == "WARNING":
            level = "warn"
        msg = attrs.get("text") or attrs.get("name") or attrs.get("description") or name
        return self._event(level=level, message=f"{name}: {msg}", source="teamcity",
                           fields={"event": name, **attrs}, raw=line)


# ── Git trace (GIT_TRACE / GIT_TRACE_PACKET / GIT_TRACE2 human target) ───────
#   19:18:27.281735 common-main.c:42                  version 2.20.1
#   20:14:03.123456 pkt-line.c:80          packet:  fetch> want 0a53e9…
class GitTraceAdapter(LogAdapter):
    name = "git_trace"
    language = "any"
    _RE = re.compile(
        r"^(?P<time>\d{2}:\d{2}:\d{2}\.\d{6})\s+"
        r"(?P<file>[\w.\-]+\.(?:c|h|cc))(?::(?P<lineno>\d+))?\s+(?P<msg>\S.*)$")

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            if not subs:
                return False
            n = sum(1 for x in subs if self._RE.match(x.strip()))
            return n >= 1 and n / len(subs) >= 0.5
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        if "\n" in s:
            s = next((x for x in s.splitlines() if self._RE.match(x.strip())),
                     s.splitlines()[0])
        m = self._RE.match(s.strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {}
        msg = g["msg"]
        em = re.match(r"^(packet|trace|region_enter|region_leave|start|version|"
                      r"cmd_name|def_repo|exit|atexit|error|child_start|child_exit)[:\s]", msg)
        if em:
            fields["trace_event"] = em.group(1)
        level = "error" if msg.startswith("error") else "debug"
        return self._event(level=level, message=msg,
                           source=f'{g["file"]}:{g["lineno"] or ""}'.rstrip(":"),
                           ts_ms=parse_timestamp(g["time"]),
                           fields=fields or None, raw=line)


# ── esbuild console output ────────────────────────────────────────────────────
#   ✘ [ERROR] Could not resolve "./missing"   +  file:line:col code frames
class EsbuildAdapter(LogAdapter):
    name = "esbuild"
    language = "node"
    _DIAG = re.compile(r"^[✘▲]\s+\[(?P<level>ERROR|WARNING)\]\s?(?P<msg>.*)$")
    _SIZE = re.compile(r"^\s*(?P<path>[\w./\-]+\.(?:js|css|mjs|cjs|map))\s+(?P<size>[\d.]+\s?[kmg]?b)\s*$",
                       re.IGNORECASE)

    def detect(self, sample_lines):
        def hit(el):
            subs = split_any(el)
            return any(self._DIAG.match(x.strip()) for x in subs) or (
                len(subs) >= 2 and sum(1 for x in subs if self._SIZE.match(x)) >= 2)
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        subs = split_any(s) if "\n" in s or "\\n" in s else [s]
        for x in subs:
            m = self._DIAG.match(x.strip())
            if m:
                lvl = "error" if m.group("level") == "ERROR" else "warn"
                loc = next((y.strip() for y in subs
                            if re.match(r"^\s*[\w./\-]+:\d+:\d+:", y)), "")
                fields = {"location": loc} if loc else None
                return self._event(level=lvl, message=m.group("msg"),
                                   source="esbuild", fields=fields, raw=line)
        m = self._SIZE.match(s)
        if m:
            return self._event(level="info", message=s.strip(), source="esbuild",
                               fields={"artifact": m.group("path"),
                                       "size": m.group("size")}, raw=line)
        return self._event(level="info", message=s.strip(), source="esbuild",
                           raw=line) if s.strip() else None


# ── Nix internal-json log (`--log-format internal-json`) ─────────────────────
#   @nix {"action":"start","id":…,"level":5,"text":"building …","type":105}
class NixJsonAdapter(LogAdapter):
    name = "nix_json"
    language = "any"
    # nix verbosity levels: 0 error, 1 warn, 2 notice, 3 info, 4+ debug-ish
    _LVL = {0: "error", 1: "warn", 2: "info", 3: "info"}

    def detect(self, sample_lines):
        # The '@nix ' envelope IS the signature; don't insist the payload
        # json.loads (shipped/pasted samples are often ellipsis-truncated).
        def hit(el):
            subs = split_any(el)
            n = sum(1 for x in subs
                    if x.strip().startswith("@nix {") and x.rstrip().endswith("}"))
            return bool(subs) and n >= 1 and n / len(subs) >= 0.5
        return ratio_detect(sample_lines, hit)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if "\n" in s:
            s = next((x.strip() for x in s.splitlines()
                      if x.strip().startswith("@nix ")), s.splitlines()[0].strip())
        if not s.startswith("@nix "):
            return None
        try:
            rec = json.loads(s[5:])
        except Exception:
            # truncated/invalid payload → still surface the event, degraded
            am = re.search(r'"action"\s*:\s*"(\w+)"', s)
            tm = re.search(r'"text"\s*:\s*"([^"]*)"', s)
            return self._event(level="info",
                               message=(tm.group(1) if tm else s[5:]),
                               source="nix",
                               fields={"action": am.group(1)} if am else None,
                               raw=line)
        if not isinstance(rec, dict):
            return None
        action = rec.get("action", "")
        msg = rec.get("text") or rec.get("msg") or action
        lvl = self._LVL.get(rec.get("level"), "debug") \
            if isinstance(rec.get("level"), int) else "info"
        if action == "msg" and isinstance(rec.get("level"), int) and rec["level"] <= 1:
            lvl = self._LVL[rec["level"]]
        fields = {"action": action}
        for k in ("id", "type", "parent"):
            if k in rec:
                fields[k] = rec[k]
        if isinstance(rec.get("fields"), list):
            fields["detail"] = rec["fields"]
        return self._event(level=lvl, message=str(msg), source="nix",
                           fields=fields, raw=line)


# ── Batch-6 registration ──────────────────────────────────────────────────────
for _a in (GradleBuildAdapter(), GradleDaemonAdapter(), YarnBerryAdapter(),
           YarnClassicAdapter(), PnpmAdapter(), UvAdapter(), IdeaLogAdapter(),
           JupyterAdapter(), TeamCityAdapter(), GitTraceAdapter(),
           EsbuildAdapter(), NixJsonAdapter()):
    register_adapter(_a)


# ═════════════════════════════════════════════════════════════════════════════
#  BATCH 7 — GIT_TRACE2 perf target
# ═════════════════════════════════════════════════════════════════════════════
# ' | '-separated columns; two shapes (GIT_TRACE2_PERF_BRIEF on/off):
#   d0 | main | version | | | | | 2.20.1.156.gf9916ae094.dirty
#   19:18:27.283000 | common-main.c:42 | main | start | r0 | 0.001453 | | | git status
class GitTrace2PerfAdapter(LogAdapter):
    name = "git_trace2_perf"
    language = "any"
    _EVENTS = {"version", "start", "exit", "atexit", "signal", "error",
               "cmd_path", "cmd_name", "cmd_mode", "alias", "child_start",
               "child_exit", "child_ready", "exec", "exec_result", "thread_start",
               "thread_exit", "def_param", "def_repo", "region_enter",
               "region_leave", "data", "data_json", "too_many_files"}
    _COL0 = re.compile(r"^(?:d\d+|\d{2}:\d{2}:\d{2}\.\d+)$")

    def _hit(self, s: str) -> bool:
        cols = [c.strip() for c in s.split(" | ")]
        if len(cols) < 6 or not self._COL0.match(cols[0]):
            return False
        return any(c in self._EVENTS for c in cols[1:6])

    def detect(self, sample_lines):
        def ok(el):
            subs = split_any(el)
            hits = sum(1 for x in subs if self._hit(x.strip()))
            return bool(subs) and hits >= 1 and hits / len(subs) >= 0.5
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        for x in split_any(line):
            s = x.strip()
            if not self._hit(s):
                continue
            cols = [c.strip() for c in s.split(" | ")]
            brief = cols[0].startswith("d")     # PERF_BRIEF drops time|file:line
            event = next((c for c in cols[1:6] if c in self._EVENTS), "")
            ts_ms = None if brief else parse_timestamp(cols[0])
            fields = {"event": event, "columns": len(cols)}
            level = "error" if event in ("error", "signal") else "debug"
            msg = cols[-1] or event
            return self._event(level=level, message=msg, source="git.trace2",
                               ts_ms=ts_ms, fields=fields, raw=line)
        return None


register_adapter(GitTrace2PerfAdapter())
