#!/usr/bin/env python3
"""
Browser-runtime adapters, and proof they do not steal server-side web logs.

Before connectors/adapters/browser.py existed, EVERY browser-side format fell to
the `structural` fallback at confidence 0.02 — measured. That was the largest gap
in AgentVision's web support: on a web app a big share of user-visible failure
never reaches the server log at all (a null deref in a component, a rejected
fetch, a CSP block), and `structural` flattened `source` to "structural" for all
of it, which is exactly the field that says WHICH module broke.

The second half of this suite is the important half. Browser and server formats
look alike — `GET https://host/p 502 (Bad Gateway)` versus an access-log line, a
browser stack frame versus a Node one — so every added adapter is a chance to
steal a format that already worked. `test_realworld_collisions.py` guards the
whole registry; this guards the specific pairs these adapters could plausibly
break.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connectors import log_adapters as la  # noqa: E402

FALLBACKS = {"structural", "generic_ts", "raw"}
FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


def route(lines):
    if isinstance(lines, str):
        lines = [lines]
    ad, conf, _ = la.detect_adapter(lines)
    return getattr(ad, "name", "?"), conf, ad


def parsed(ad, line):
    return ad.parse_line(line) or {}


def main() -> int:
    print("1. browser formats are specifically parsed, not left to a fallback")
    cases = [
        ("console TypeError",
         "Uncaught TypeError: Cannot read properties of null (reading 'map')\n"
         "    at Cart.tsx:42:17",
         "browser_console"),
        ("unhandled rejection",
         "Uncaught (in promise) Error: 502 Bad Gateway  at fetchCart (api.ts:88)",
         "browser_promise_rejection"),
        ("CSP violation",
         "Refused to load the script 'https://evil/x.js' because it violates the "
         'following Content Security Policy directive: "script-src \'self\'"',
         "browser_csp"),
        ("network failure",
         "GET https://api.example.com/cart 502 (Bad Gateway)",
         "browser_network"),
        ("AgentVision page emitter",
         '{"av":"browser","ts_ms":1785000000000,"level":"ERROR",'
         '"category":"error","source":"Cart.tsx","message":"null map",'
         '"data":{"stack":"x"},"url":"https://x/cart"}',
         "browser_av_json"),
    ]
    for label, line, want in cases:
        name, conf, _ = route(line.split("\n"))
        check(f"{label} -> {want}", name == want, f"got {name} @{conf:.2f}")
        check(f"{label} is not a fallback", name not in FALLBACKS, name)

    print("\n2. the fields that make a browser error triageable survive")
    # `source` must be the throwing MODULE. structural flattened this, which is
    # what broke av_errors_by_fingerprint grouping and av_source_at_error.
    _, _, ad = route(["Uncaught (in promise) Error: 502 Bad Gateway  "
                      "at fetchCart (api.ts:88)"])
    ev = parsed(ad, "Uncaught (in promise) Error: 502 Bad Gateway  "
                    "at fetchCart (api.ts:88)")
    check("rejection source is the module, not the adapter",
          ev.get("source") == "api.ts", str(ev.get("source")))
    check("rejection is flagged as unhandled",
          (ev.get("data") or {}).get("unhandled_rejection") is True)
    check("rejection level is ERROR", ev.get("level") == "ERROR")

    _, _, ad = route(["GET https://api.example.com/cart 502 (Bad Gateway)"])
    ev = parsed(ad, "GET https://api.example.com/cart 502 (Bad Gateway)")
    check("network status is extracted as an int",
          (ev.get("data") or {}).get("status") == 502)
    check("network source is the host",
          ev.get("source") == "api.example.com", str(ev.get("source")))
    check("5xx is ERROR", ev.get("level") == "ERROR")
    _, _, ad4 = route(["GET https://api.example.com/cart 404 (Not Found)"])
    check("4xx is WARN, not ERROR",
          parsed(ad4, "GET https://api.example.com/cart 404 (Not Found)")
          .get("level") == "WARN")

    csp = ("Refused to load the script 'https://evil/x.js' because it violates "
           'the following Content Security Policy directive: "script-src \'self\'"')
    _, _, ad = route([csp])
    ev = parsed(ad, csp)
    check("CSP blocked URL is extracted",
          (ev.get("data") or {}).get("blocked_url") == "https://evil/x.js",
          str((ev.get("data") or {}).get("blocked_url")))
    # A CSP block usually leaves the page running, so it is a WARN — calling it
    # ERROR would inflate the health deduction for a non-fatal condition.
    check("CSP is WARN, not ERROR", ev.get("level") == "WARN")

    print("\n3. a multi-line console block scores high and attributes each frame")
    block = ["Uncaught TypeError: Cannot read properties of null (reading 'map')",
             "    at Cart.tsx:42:17",
             "    at renderList (List.tsx:88:9)"]
    name, conf, ad = route(block)
    check("multi-line block routes to browser_console", name == "browser_console")
    # A real console error is message + stack across several lines. If the adapter
    # claimed only the first, detection scored 1/N and the frames — the only lines
    # carrying a source location — fell to structural.
    check("detection is not diluted by the stack lines", conf >= 0.9, f"{conf:.2f}")
    check("frame 1 attributes to Cart.tsx",
          parsed(ad, block[1]).get("source") == "Cart.tsx")
    check("frame 2 attributes to List.tsx",
          parsed(ad, block[2]).get("source") == "List.tsx")

    print("\n4. SERVER-side web logs must be untouched (the anti-theft half)")
    server = [
        ("nginx/apache combined access",
         '127.0.0.1 - - [30/Jul/2026:10:12:03 +0000] "GET /api/items?page=2 '
         'HTTP/1.1" 500 1234 "https://x/y" "Mozilla/5.0"'),
        ("apache error",
         "[Thu Jul 30 10:12:03.123456 2026] [php:error] [pid 1234] "
         "[client 1.2.3.4:52] PHP Fatal error: x"),
        ("django dev server",
         '[30/Jul/2026 10:12:03] "GET /admin/ HTTP/1.1" 500 145'),
        ("flask/werkzeug",
         '127.0.0.1 - - [30/Jul/2026 10:12:03] "POST /login HTTP/1.1" 401 -'),
        ("caddy structured json",
         '{"level":"error","ts":1785000000.1,"logger":"http.handler",'
         '"msg":"upstream down","status":502}'),
        ("plain unified jsonl",
         '{"ts_ms":1785000000000,"level":"INFO","message":"hello"}'),
    ]
    for label, line in server:
        name, conf, _ = route([line])
        check(f"{label} not claimed by a browser adapter",
              not name.startswith("browser_"), f"stolen by {name} @{conf:.2f}")
        check(f"{label} still specifically parsed", name not in FALLBACKS, name)

    print("\n5. neighbouring stack formats keep their own adapters")
    py = ["Traceback (most recent call last):",
          '  File "/app/svc.py", line 12, in handle',
          "    return r['payload']",
          "KeyError: 'payload'"]
    check("python traceback still wins", route(py)[0] == "python_traceback",
          route(py)[0])
    node = ["TypeError: Cannot read property 'x' of undefined",
            "    at Object.<anonymous> (/srv/app/index.js:10:5)",
            "    at Module._compile (node:internal/modules/cjs/loader:1105:14)"]
    check("node server stack still wins", route(node)[0] == "node_stack",
          route(node)[0])
    # A relative path with no parenthesised reason is an access-log fragment, not
    # a browser console line. browser_network must be strict enough to pass.
    check("a bare relative request line is NOT taken as browser network",
          not route(["GET /api/items 502"])[0].startswith("browser_"),
          route(["GET /api/items 502"])[0])

    print("\n6. the page emitter ships and declares what it captures")
    js = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))),
        "agent_bootstrap", "av_browser.js")
    check("agent_bootstrap/av_browser.js exists", os.path.exists(js))
    if os.path.exists(js):
        src = open(js, encoding="utf-8").read()
        for hook in ("unhandledrejection", "securitypolicyviolation",
                     "window.fetch", "XMLHttpRequest", "sendBeacon"):
            check(f"emitter hooks {hook}", hook in src)
        # It must not re-enter its own instrumentation, or a transport error
        # becomes an infinite loop that only ends when the tab dies.
        check("emitter keeps a native fetch reference", "nativeFetch" in src)
        check("emitter is idempotent", "__AGENTVISION_BROWSER__" in src)
        check("emitter bounds its queue", "MAX_QUEUE" in src)
        check("emitter emits the av:'browser' marker the adapter needs",
              'av: "browser"' in src)

    print("\n7. the ingest route: a browser cannot append to a file")
    # Every other emitter writes to disk. The browser has no filesystem, so the
    # page POSTs NDJSON and the route appends it. Two things must hold or the
    # emitter fails SILENTLY: CORS (else the same-origin policy blocks it and
    # nothing surfaces) and the av:"browser" marker check (else the route is an
    # open door for injecting lines into a program's log).
    bs_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "api", "bridge_server.py")
    src = open(bs_path, encoding="utf-8", errors="replace").read()
    check("POST /browser/ingest exists", '"/browser/ingest"' in src)
    check("it handles the OPTIONS preflight", '"POST", "OPTIONS"' in src)
    check("it sends Access-Control-Allow-Origin",
          "Access-Control-Allow-Origin" in src)
    check("make_response is imported (used by the CORS helper)",
          "make_response" in src.split("\n")[25] or
          "abort, make_response" in src or "make_response," in src,
          "a NameError here would only appear at runtime")
    check("records without av:'browser' are rejected",
          "missing_av_browser_marker" in src)
    check("the batch and body are bounded",
          "_BROWSER_MAX_BATCH" in src and "_BROWSER_MAX_BODY" in src)
    check("arrival time is stamped so a page cannot claim the future",
          "received_ms" in src)
    # Authorship: browser events ARE the program's output, so unlike POST /log
    # they belong in the program's sink, not the observer log.
    check("browser events go to the program sink, not the observer log",
          '"agentvision" / "log.txt"' in src or
          'Path(root) / "agentvision" / "log.txt"' in src)

    print("\n8. the catalog offers the browser emitter to web languages only")
    import bridge_plan as bp
    web_ids = [e["id"] for e in bp._emitter_options("typescript")]
    check("typescript is offered browser_events", "browser_events" in web_ids,
          str(web_ids))
    check("node is offered browser_events",
          "browser_events" in [e["id"] for e in bp._emitter_options("node")])
    for lang in ("c", "dotnet", "python", "go"):
        ids = [e["id"] for e in bp._emitter_options(lang)]
        check(f"{lang} is NOT offered browser_events",
              "browser_events" not in ids, str(ids))
    ev = [e for e in bp._emitter_options("typescript")
          if e["id"] == "browser_events"]
    if ev:
        e = ev[0]
        # The old failure mode was offering a frontend the Node emitter, whose
        # NODE_OPTIONS mechanism cannot load in a browser. The option must say
        # what it actually needs.
        check("it states that the bridge must be reachable from the page",
              "reachable" in (e.get("note") or "").lower(), str(e.get("note"))[:70])
        check("it names the file it builds", "av_browser.js" in (e.get("builds_as") or ""))
        check("it is honest about what it misses", bool(e.get("misses")))

    print(f"\n{len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
