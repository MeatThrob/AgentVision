/* AgentVision browser emitter — the client half of a web app.
 * ============================================================================
 * WHY THIS EXISTS
 * Every other AgentVision emitter appends to a file. A browser cannot: it has no
 * filesystem. So the page collects events and POSTs newline-delimited JSON to the
 * bridge, which appends them to the profile's `text` sink. The wire shape carries
 * `av: "browser"` so connectors/adapters/browser.py::browser_av_json claims it
 * (registered before `jsonl`, which would otherwise take it at 1.0 and flatten
 * the browser-specific fields).
 *
 * WHAT IT CAPTURES, and why each one earns its place
 *   window.onerror                 a throw that reached the top — the classic
 *   unhandledrejection             a rejected promise nobody caught. On a modern
 *                                  app this is the MOST common real failure and
 *                                  it never reaches the server log.
 *   console.error / console.warn   what the app itself decided was wrong
 *   fetch / XHR non-2xx + throws   a 502 the server logged as 502 but the UI
 *                                  swallowed; also CORS/offline, which the
 *                                  server never sees AT ALL
 *   securitypolicyviolation        a CSP block: asset silently absent, page
 *                                  "works", feature is dead
 *   resource load errors           <img>/<script>/<link> that 404'd
 *
 * DELIBERATELY NOT CAPTURED: console.log (volume, no signal), every successful
 * request (that is what the server log is for), keystrokes or form values (they
 * contain user data and this ships to a local file).
 *
 * SAFETY RULES THIS FILE MUST NEVER BREAK
 *   1. Never throw into the host page. Every handler is wrapped.
 *   2. Never log its own failures — a transport error that emitted an event would
 *      loop until the tab died.
 *   3. Bounded queue and bounded payload. A page in an error loop must degrade to
 *      a repeat counter, not fill the disk.
 *   4. Never break the wrapped API: console.error and fetch must behave exactly
 *      as before, including return values and rejections.
 *
 * USAGE — add ONE of these to the page (dev builds only):
 *   <script src="/agentvision/emitters/av_browser.js"
 *           data-av-endpoint="http://127.0.0.1:7771/browser/ingest"></script>
 * or import it first in your entry module:
 *   import "./agentvision/emitters/av_browser.js";
 */
(function () {
  "use strict";

  if (window.__AGENTVISION_BROWSER__) return;   // idempotent, like the other emitters
  window.__AGENTVISION_BROWSER__ = true;

  var script = document.currentScript;
  var ENDPOINT =
    (script && script.getAttribute("data-av-endpoint")) ||
    window.AGENTVISION_ENDPOINT ||
    "http://127.0.0.1:7771/browser/ingest";
  var SESSION =
    (script && script.getAttribute("data-av-session")) || "default";

  var MAX_QUEUE = 200;        // events held before the oldest are dropped
  var MAX_MSG = 2000;         // chars per message
  var FLUSH_MS = 1500;
  var DEDUPE_CAP = 50;        // distinct signatures tracked for repeat-collapsing

  var queue = [];
  var seen = Object.create(null);   // signature -> count, so a loop collapses
  var dropped = 0;
  var timer = null;
  var sending = false;

  // Keep native references BEFORE anything is wrapped, so the transport can
  // never re-enter our own instrumentation (safety rule 2).
  var nativeFetch = window.fetch && window.fetch.bind(window);
  var NativeXHR = window.XMLHttpRequest;
  var nativeConsole = {
    error: window.console && console.error && console.error.bind(console),
    warn: window.console && console.warn && console.warn.bind(console)
  };

  function clip(s, n) {
    s = (s === undefined || s === null) ? "" : String(s);
    return s.length > n ? s.slice(0, n) + "…[clipped]" : s;
  }

  // "at Cart.tsx:42:17" / "at fn (https://host/a.js:1:2)" -> the throwing module.
  var FRAME = /\bat\s+(?:[\w$.<>\[\]]+\s+\()?((?:[\w.\-\/]+\.(?:tsx?|jsx?|mjs|cjs|vue|svelte))|(?:https?:\/\/[^\s):]+)):(\d+)(?::(\d+))?\)?/;

  function frameOf(stack) {
    try {
      var m = FRAME.exec(String(stack || ""));
      if (!m) return {};
      var file = m[1];
      var base = file.split("/").pop() || file;
      return { source_file: file, source_base: base,
               source_line: parseInt(m[2], 10),
               source_col: m[3] ? parseInt(m[3], 10) : undefined };
    } catch (e) { return {}; }
  }

  function push(level, category, source, message, data) {
    try {
      var sig = level + "|" + category + "|" + source + "|" + clip(message, 200);
      if (seen[sig] !== undefined) {
        // Repeat: bump the count instead of queueing another copy. Lossless in
        // the sense the retention work established — the count is preserved.
        seen[sig]++;
        return;
      }
      if (Object.keys(seen).length < DEDUPE_CAP) seen[sig] = 1;

      if (queue.length >= MAX_QUEUE) { queue.shift(); dropped++; }
      queue.push({
        av: "browser",
        ts: new Date().toISOString(),
        ts_ms: Date.now(),
        level: level,
        category: category,
        source: source || "browser",
        message: clip(message, MAX_MSG),
        data: data || {},
        url: location.href,
        user_agent: navigator.userAgent,
        viewport: window.innerWidth + "x" + window.innerHeight
      });
      schedule();
    } catch (e) { /* rule 1: never throw into the page */ }
  }

  function schedule() {
    if (timer !== null) return;
    try { timer = setTimeout(flush, FLUSH_MS); } catch (e) { timer = null; }
  }

  function flush(useBeacon) {
    timer = null;
    if (sending || !queue.length) return;
    var batch = queue.splice(0, queue.length);
    // Attach repeat counts for anything that fired more than once.
    for (var i = 0; i < batch.length; i++) {
      var sig = batch[i].level + "|" + batch[i].category + "|" +
                batch[i].source + "|" + clip(batch[i].message, 200);
      if (seen[sig] > 1) batch[i].data.repeat = seen[sig];
    }
    if (dropped) {
      batch.push({ av: "browser", ts: new Date().toISOString(), ts_ms: Date.now(),
                   level: "WARN", category: "log", source: "agentvision.browser",
                   message: dropped + " event(s) dropped: queue full (cap " +
                            MAX_QUEUE + ")",
                   data: { dropped: dropped }, url: location.href });
      dropped = 0;
    }
    seen = Object.create(null);

    var body = batch.map(function (e) { return JSON.stringify(e); }).join("\n");

    // On unload, sendBeacon is the only transport that survives.
    if (useBeacon && navigator.sendBeacon) {
      try { navigator.sendBeacon(ENDPOINT, new Blob([body], { type: "application/x-ndjson" })); }
      catch (e) { /* nothing more we can do */ }
      return;
    }
    sending = true;
    try {
      if (nativeFetch) {
        nativeFetch(ENDPOINT, {
          method: "POST", body: body, mode: "cors", keepalive: true,
          headers: { "Content-Type": "application/x-ndjson" }
        })["catch"](function () { /* rule 2: silent */ })
         .then(function () { sending = false; }, function () { sending = false; });
      } else {
        var x = new NativeXHR();
        x.open("POST", ENDPOINT, true);
        x.setRequestHeader("Content-Type", "application/x-ndjson");
        x.onloadend = function () { sending = false; };
        x.send(body);
      }
    } catch (e) { sending = false; }
  }

  // ── 1. uncaught errors ────────────────────────────────────────────────────
  window.addEventListener("error", function (ev) {
    try {
      // A resource that failed to load fires `error` on the ELEMENT, with no
      // ev.error — different failure, different message.
      if (ev && ev.target && ev.target !== window && ev.target.tagName) {
        var el = ev.target;
        var src = el.src || el.href || "";
        push("ERROR", "network", (el.tagName || "").toLowerCase(),
             "failed to load " + (el.tagName || "").toLowerCase() + ": " + src,
             { resource_url: src, tag: el.tagName });
        return;
      }
      var err = ev && ev.error;
      var loc = frameOf(err && err.stack);
      push("ERROR", "error", loc.source_base || "browser",
           (err && (err.name + ": " + err.message)) ||
             (ev && ev.message) || "uncaught error",
           { error_class: (err && err.name) || "", uncaught: true,
             stack: clip(err && err.stack, MAX_MSG),
             file: (ev && ev.filename) || loc.source_file,
             line: (ev && ev.lineno) || loc.source_line,
             col: (ev && ev.colno) || loc.source_col });
    } catch (e) {}
  }, true);

  // ── 2. unhandled promise rejections ───────────────────────────────────────
  window.addEventListener("unhandledrejection", function (ev) {
    try {
      var r = ev && ev.reason;
      var isErr = r && typeof r === "object" && "message" in r;
      var loc = frameOf(isErr ? r.stack : "");
      push("ERROR", "error", loc.source_base || "browser",
           "Uncaught (in promise) " +
             (isErr ? (r.name + ": " + r.message) : clip(r, 400)),
           { unhandled_rejection: true,
             error_class: (isErr && r.name) || typeof r,
             stack: clip(isErr && r.stack, MAX_MSG) });
    } catch (e) {}
  });

  // ── 3. console.error / console.warn ───────────────────────────────────────
  function wrapConsole(name, level) {
    var orig = nativeConsole[name];
    if (!orig) return;
    console[name] = function () {
      try {
        var parts = [];
        for (var i = 0; i < arguments.length; i++) {
          var a = arguments[i];
          if (a instanceof Error) parts.push(a.name + ": " + a.message);
          else if (typeof a === "object") {
            try { parts.push(JSON.stringify(a)); } catch (e2) { parts.push("[object]"); }
          } else parts.push(String(a));
        }
        var stack = "";
        for (var j = 0; j < arguments.length; j++) {
          if (arguments[j] instanceof Error) { stack = arguments[j].stack; break; }
        }
        var loc = frameOf(stack);
        push(level, level === "ERROR" ? "error" : "warn",
             loc.source_base || "console", parts.join(" "),
             { console: name, stack: clip(stack, MAX_MSG) });
      } catch (e) {}
      // rule 4: the wrapped API must behave exactly as before.
      return orig.apply(console, arguments);
    };
  }
  wrapConsole("error", "ERROR");
  wrapConsole("warn", "WARN");

  // ── 4. fetch failures (non-2xx and network/CORS throws) ──────────────────
  if (nativeFetch) {
    window.fetch = function (input, init) {
      var method = (init && init.method) ||
                   (input && input.method) || "GET";
      var url = (input && input.url) || String(input);
      var t0 = Date.now();
      return nativeFetch(input, init).then(function (res) {
        try {
          if (!res.ok) {
            push(res.status >= 500 ? "ERROR" : "WARN", "network",
                 (function () { try { return new URL(url, location.href).host; }
                                catch (e) { return "browser"; } })(),
                 method + " " + url + " " + res.status +
                   " (" + (res.statusText || "") + ")",
                 { http_method: method, url: url, status: res.status,
                   reason: res.statusText || "", duration_ms: Date.now() - t0 });
          }
        } catch (e) {}
        return res;                       // rule 4
      }, function (err) {
        try {
          // A thrown fetch is CORS, DNS, offline, or an abort — the server-side
          // log has NO record of this class of failure at all.
          push("ERROR", "network", "fetch",
               method + " " + url + " failed: " +
                 ((err && err.message) || "network error"),
               { http_method: method, url: url, network_error: true,
                 error_class: (err && err.name) || "",
                 duration_ms: Date.now() - t0 });
        } catch (e) {}
        throw err;                        // rule 4: preserve the rejection
      });
    };
  }

  // ── 5. XMLHttpRequest failures ────────────────────────────────────────────
  if (NativeXHR && NativeXHR.prototype && NativeXHR.prototype.open) {
    var origOpen = NativeXHR.prototype.open;
    var origSend = NativeXHR.prototype.send;
    NativeXHR.prototype.open = function (m, u) {
      try { this.__av = { method: m, url: u, t0: Date.now() }; } catch (e) {}
      return origOpen.apply(this, arguments);
    };
    NativeXHR.prototype.send = function () {
      var self = this;
      try {
        self.addEventListener("loadend", function () {
          try {
            var a = self.__av || {};
            // Endpoint traffic is ours; logging it would loop (rule 2).
            if (String(a.url || "").indexOf(ENDPOINT) === 0) return;
            if (self.status === 0) {
              push("ERROR", "network", "xhr",
                   (a.method || "GET") + " " + a.url + " failed (no response)",
                   { http_method: a.method, url: a.url, network_error: true });
            } else if (self.status >= 400) {
              push(self.status >= 500 ? "ERROR" : "WARN", "network", "xhr",
                   (a.method || "GET") + " " + a.url + " " + self.status +
                     " (" + (self.statusText || "") + ")",
                   { http_method: a.method, url: a.url, status: self.status,
                     reason: self.statusText || "",
                     duration_ms: Date.now() - (a.t0 || Date.now()) });
            }
          } catch (e) {}
        });
      } catch (e) {}
      return origSend.apply(this, arguments);
    };
  }

  // ── 6. CSP violations ─────────────────────────────────────────────────────
  document.addEventListener("securitypolicyviolation", function (ev) {
    try {
      push("WARN", "security", "csp",
           "Refused to load '" + (ev.blockedURI || "") +
             "' because it violates the Content Security Policy directive: \"" +
             (ev.violatedDirective || "") + "\"",
           { blocked_url: ev.blockedURI || "",
             directive: ev.violatedDirective || "",
             effective_directive: ev.effectiveDirective || "",
             document_url: ev.documentURI || "" });
    } catch (e) {}
  });

  // ── flush on the way out ──────────────────────────────────────────────────
  window.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") flush(true);
  });
  window.addEventListener("pagehide", function () { flush(true); });

  // Announce, so `hooks_armed`-style verification is possible from the log alone
  // rather than inferred from silence.
  push("INFO", "process", "agentvision.browser",
       "browser emitter armed (session=" + SESSION + ")",
       { endpoint: ENDPOINT, session: SESSION,
         captures: ["window.onerror", "unhandledrejection", "console.error",
                    "console.warn", "fetch", "xhr", "csp", "resource_error"] });
  flush();
})();
