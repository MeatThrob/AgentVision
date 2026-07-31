"""
Browser-runtime log adapters — the half of a web app that runs on the client.
================================================================================
AgentVision had strong SERVER-side web coverage and none at all for the browser.
Measured before this module existed, every one of these fell through to the
`structural` fallback at confidence 0.02:

    Uncaught TypeError: Cannot read properties of null (reading 'map')
        at Cart.tsx:42:17
    Uncaught (in promise) Error: 502 Bad Gateway  at fetchCart (api.ts:88)
    Refused to load the script 'https://evil/x.js' because it violates ...
    GET https://api.example.com/cart 502 (Bad Gateway)

That matters more for a web app than for anything else AgentVision watches,
because on a web app a large share of user-visible failure never reaches the
server log at all: a null deref in a component, a rejected fetch, a CSP block. The
server returns 200 and the page is broken.

`structural` does recover a level, so these were not invisible — but it reported
`source: "structural"` for every line, which collapses the one field that makes
browser errors triageable: WHICH module threw. Recovering `source=Cart.tsx` and
`line=42` is what lets av_source_at_error jump to the code.

Formats: browser_console, browser_promise_rejection, browser_csp, browser_network,
browser_av_json (AgentVision's own page emitter).

NOTE ON ORDERING: these register before the generic fallbacks but must not steal
server-side lines. `browser_network` is the risky one — a bare
`GET /x 502 (Bad Gateway)` resembles an access-log fragment — so it requires the
absolute-URL + parenthesised-reason shape that only the browser console emits.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp,
                      ratio_detect)

#: `at fn (file:line:col)` / `at file:line:col` — the V8/SpiderMonkey stack frame.
_FRAME = re.compile(
    r"\bat\s+(?:(?P<fn>[\w$.<>\[\]]+)\s+\()?"
    r"(?P<file>(?:[\w.\-/]+\.(?:tsx?|jsx?|mjs|cjs|vue|svelte|html))"
    r"|(?:https?://[^\s):]+))"
    r":(?P<line>\d+)(?::(?P<col>\d+))?\)?")

_JS_ERRORS = ("TypeError", "ReferenceError", "RangeError", "SyntaxError",
              "URIError", "EvalError", "AggregateError", "DOMException", "Error")


def _first_frame(text: str) -> dict:
    """Source location of the topmost stack frame, if the line carries one."""
    m = _FRAME.search(text)
    if not m:
        return {}
    out = {"source_file": m.group("file"), "source_line": int(m.group("line"))}
    if m.group("col"):
        out["source_col"] = int(m.group("col"))
    if m.group("fn"):
        out["function"] = m.group("fn")
    return out


def _basename(path: str) -> str:
    return (path or "").rsplit("/", 1)[-1] or path


# ── Uncaught console error / warning with a JS error class ───────────────────
#   Uncaught TypeError: Cannot read properties of null (reading 'map')
#       at Cart.tsx:42:17
class BrowserConsoleAdapter(LogAdapter):
    name = "browser_console"
    language = "browser"
    _RE = re.compile(
        r"^(?:(?P<ts>\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+)?"
        r"(?P<unc>Uncaught\s+)?"
        r"(?P<cls>" + "|".join(_JS_ERRORS) + r")"
        r"(?::\s*(?P<msg>.*))?$", re.S)

    #: A bare continuation frame: `    at Cart.tsx:42:17`. A real console error is
    #: multi-line — the message on one line, the stack on the following ones — and
    #: the pipeline feeds lines individually. Claiming the frames too is what makes
    #: detection score 1.0 on a real block instead of 1/N, and it keeps the frames
    #: out of `structural`, where they would carry no source at all.
    _FRAME_ONLY = re.compile(r"^\s+at\s+\S")

    def detect(self, sample_lines):
        def ok(ln: str) -> bool:
            s = ln.strip()
            if not s:
                return False
            if self._FRAME_ONLY.match(ln.rstrip("\r\n")) and _FRAME.search(ln):
                return True
            # A bare "Error: x" is far too common in other ecosystems; require
            # either the browser's "Uncaught" prefix or a JS/TS source frame.
            if not self._RE.match(s.split("\n", 1)[0]):
                return False
            return s.startswith("Uncaught") or bool(_FRAME.search(s))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")

        # Continuation frame — carries the source location and nothing else.
        if self._FRAME_ONLY.match(s) and _FRAME.search(s):
            loc = _first_frame(s)
            return {
                "ts": "", "ts_ms": None,
                "level": "ERROR", "category": "error",
                "source": _basename(loc.get("source_file", "")) or "browser",
                "message": s.strip(),
                "data": {"adapter": self.name, "stack_frame": True, **loc},
            }

        m = self._RE.match(s.strip().split("\n", 1)[0])
        if not m:
            return None
        head = s.strip()
        if not (head.startswith("Uncaught") or _FRAME.search(s)):
            return None
        loc = _first_frame(s)
        fields = {"error_class": m.group("cls"), "uncaught": bool(m.group("unc"))}
        fields.update(loc)
        return {
            "ts": m.group("ts") or "",
            "ts_ms": parse_timestamp(m.group("ts")) if m.group("ts") else None,
            "level": "ERROR",
            "category": "error",
            # The throwing module, not the adapter — this is the field
            # `structural` was flattening.
            "source": _basename(loc.get("source_file", "")) or "browser",
            "message": (m.group("msg") or "").strip() or m.group("cls"),
            "data": {"adapter": self.name, **fields},
        }


# ── Unhandled promise rejection ──────────────────────────────────────────────
#   Uncaught (in promise) Error: 502 Bad Gateway  at fetchCart (api.ts:88)
class BrowserPromiseRejectionAdapter(LogAdapter):
    name = "browser_promise_rejection"
    language = "browser"
    _RE = re.compile(
        r"^Uncaught\s+\(in promise\)\s*"
        r"(?P<cls>[A-Za-z_$][\w$]*)?(?::\s*)?(?P<msg>.*)$", re.S)

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda ln: ln.strip().startswith("Uncaught (in promise)"))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        m = self._RE.match(s.strip())
        if not m:
            return None
        loc = _first_frame(s)
        return {
            "ts": "", "ts_ms": None,
            "level": "ERROR", "category": "error",
            "source": _basename(loc.get("source_file", "")) or "browser",
            "message": (m.group("msg") or "").strip(),
            "data": {"adapter": self.name, "unhandled_rejection": True,
                     "error_class": m.group("cls") or "", **loc},
        }


# ── Content-Security-Policy violation ────────────────────────────────────────
#   Refused to load the script 'https://evil/x.js' because it violates the
#   following Content Security Policy directive: "script-src 'self'"
class BrowserCspAdapter(LogAdapter):
    name = "browser_csp"
    language = "browser"
    _RE = re.compile(
        r"^Refused to (?P<verb>load|apply|connect to|frame|execute)\s+"
        r"(?:the\s+)?(?P<what>[\w\- ]+?)?\s*'?(?P<url>[^'\s]+)?'?\s*"
        r"because it violates(?: the following)?\s*"
        r"(?:Content Security Policy directive:?\s*)?(?P<directive>.*)$", re.S)

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda ln: ln.strip().startswith("Refused to")
            and "Content Security Policy" in ln)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._RE.match(s)
        if not m:
            return None
        directive = (m.group("directive") or "").strip().strip('"')
        return {
            "ts": "", "ts_ms": None,
            # A CSP block is a real functional failure (the asset did not load),
            # but the page usually keeps running — WARN, not ERROR.
            "level": "WARN", "category": "security",
            "source": "csp",
            "message": s,
            "data": {"adapter": self.name, "blocked_url": m.group("url") or "",
                     "resource": (m.group("what") or "").strip(),
                     "directive": directive, "verb": m.group("verb")},
        }


# ── Failed network request as the console prints it ──────────────────────────
#   GET https://api.example.com/cart 502 (Bad Gateway)
class BrowserNetworkAdapter(LogAdapter):
    name = "browser_network"
    language = "browser"
    # Deliberately strict: absolute URL AND a parenthesised reason. A relative
    # path or a missing reason is far more likely to be a server access log.
    _RE = re.compile(
        r"^(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+"
        r"(?P<url>https?://\S+)\s+"
        r"(?P<status>[1-5]\d{2})\s*\((?P<reason>[^)]*)\)\s*$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines,
                            lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        status = int(m.group("status"))
        host = re.sub(r"^https?://([^/]+).*$", r"\1", m.group("url"))
        return {
            "ts": "", "ts_ms": None,
            "level": "ERROR" if status >= 500 else
                     "WARN" if status >= 400 else "INFO",
            "category": "network",
            "source": host,
            "message": f"{m.group('method')} {m.group('url')} "
                       f"{status} ({m.group('reason')})",
            "data": {"adapter": self.name, "http_method": m.group("method"),
                     "url": m.group("url"), "status": status,
                     "reason": m.group("reason"), "host": host},
        }


# ── AgentVision's own page emitter (see agent_bootstrap/av_browser.js) ──────
# The browser cannot append to a file, so the page emitter POSTs newline-
# delimited JSON. Native passthrough: it already speaks the unified schema.
class BrowserAvJsonAdapter(LogAdapter):
    name = "browser_av_json"
    language = "browser"

    def detect(self, sample_lines):
        def ok(ln: str) -> bool:
            s = ln.strip()
            if not (s.startswith("{") and s.endswith("}")):
                return False
            try:
                r = json.loads(s)
            except Exception:
                return False
            return isinstance(r, dict) and r.get("av") == "browser"
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        try:
            r = json.loads(line.strip())
        except Exception:
            return None
        if not (isinstance(r, dict) and r.get("av") == "browser"):
            return None
        d = dict(r.get("data") or {})
        d["adapter"] = self.name
        for k in ("url", "user_agent", "viewport", "kind"):
            if r.get(k) is not None:
                d.setdefault(k, r[k])
        return {
            "ts": r.get("ts") or "",
            "ts_ms": r.get("ts_ms"),
            "level": (r.get("level") or "INFO").upper(),
            "category": r.get("category") or "log",
            "source": r.get("source") or "browser",
            "message": r.get("message") or "",
            "data": d,
        }


# ── Frontend build/type errors (Next.js, CRA, Vite, tsc) ────────────────────
#   Failed to compile.
#   ./app/page.tsx:12:5
#   Type error: Property 'x' does not exist on type 'Props'.
#
# Measured before this adapter existed: the block was claimed by `openconnect`
# — a VPN client — and openconnect.parse_line() then returned source=None,
# level=None, message=''. An adapter that wins a format and extracts nothing is
# worse than the fallback, which at least recovers a level. Same class as the
# coreboot_cbmem/AbyssEngine misroute.
class FrontendBuildErrorAdapter(LogAdapter):
    name = "frontend_build_error"
    language = "browser"
    _HEAD = re.compile(r"^(?:Failed to compile\.?|Build error occurred|"
                       r"error during build:)\s*$", re.I)
    _LOC = re.compile(r"^\.{0,2}/?(?P<file>[\w.\-/\[\]@]+"
                      r"\.(?:tsx?|jsx?|mjs|cjs|vue|svelte|css|scss))"
                      r":(?P<line>\d+)(?::(?P<col>\d+))?\s*$")
    _KIND = re.compile(r"^(?P<kind>Type error|Syntax error|Module not found|"
                       r"SyntaxError|TypeError|Error):\s*(?P<msg>.*)$", re.I)

    def detect(self, sample_lines):
        def ok(ln: str) -> bool:
            s = ln.strip()
            return bool(self._HEAD.match(s) or self._LOC.match(s)
                        or self._KIND.match(s))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        if self._HEAD.match(s):
            return {"ts": "", "ts_ms": None, "level": "ERROR",
                    "category": "build", "source": "build",
                    "message": s, "data": {"adapter": self.name,
                                           "build_failed": True}}
        m = self._LOC.match(s)
        if m:
            return {"ts": "", "ts_ms": None, "level": "ERROR",
                    "category": "build",
                    "source": _basename(m.group("file")),
                    "message": s,
                    "data": {"adapter": self.name,
                             "source_file": m.group("file"),
                             "source_line": int(m.group("line")),
                             **({"source_col": int(m.group("col"))}
                                if m.group("col") else {})}}
        m = self._KIND.match(s)
        if m:
            return {"ts": "", "ts_ms": None, "level": "ERROR",
                    "category": "build", "source": "build",
                    "message": m.group("msg").strip() or s,
                    "data": {"adapter": self.name,
                             "error_kind": m.group("kind")}}
        return None


# Register most-specific first. browser_av_json is native passthrough and must
# outrank the generic `jsonl` adapter, which would otherwise claim it at 1.0 and
# drop the browser-specific fields.
register_adapter(BrowserAvJsonAdapter(), before="jsonl")
register_adapter(FrontendBuildErrorAdapter())
register_adapter(BrowserPromiseRejectionAdapter())
register_adapter(BrowserCspAdapter())
register_adapter(BrowserNetworkAdapter())
register_adapter(BrowserConsoleAdapter())
