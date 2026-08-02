#!/usr/bin/env python3
"""MCP resources must address the same artifacts the tools do, and cost less.

WHY THESE EXIST. A resource is not a second copy of a tool. It is a URI the
CLIENT fetches when something actually needs it, instead of a payload that lands
in the transcript whether or not it was wanted — which for the 200 KB
first-connection catalog is the difference between a context cost paid once on
demand and one paid every time the tool is called.

That only holds if three things stay true, and each has a specific way to break:

  1. `agentvision://catalog` must return the SAME catalog_token as
     av_bridge_catalog(). A separate implementation would drift, and the failure
     lands far away: av_bridge_commit rejects the plan for a stale token, giving
     no hint that the token came from a different code path. (Checked live in
     test_bridge_gate.py, which already requires a running bridge.)

  2. Reading a resource must be REPEATABLE. `agentvision://log/raw` maps onto a
     stream with a per-session read cursor; if reading the resource advanced it,
     a client that auto-attaches resources would silently consume the lines
     av_log_raw was about to hand the agent, and the loss would look like a
     program that had gone quiet.

  3. A bad path parameter must produce an explanation, not a traceback and not a
     confident 404 about the wrong thing.

These run offline: `_http_get` is replaced with a recorder, so what is asserted
is the mapping from URI to bridge call — which is the only logic the resource
layer actually contains.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("MCP resources — addressable, repeatable, honest when wrong")
    try:
        import api.claude_mcp as M
    except SystemExit:
        print("SKIP — the `mcp` SDK is not installed, so no resource can be "
              "registered. Nothing here was verified. Fix: pip install mcp")
        return 0
    except Exception as exc:
        print(f"SKIP — could not import the MCP server "
              f"({type(exc).__name__}: {exc}). Nothing here was verified.")
        return 0

    fixed = {str(r.uri): r for r in asyncio.run(M.mcp.list_resources())}
    tmpl = {t.uri_template: t for t in
            asyncio.run(M.mcp.list_resource_templates())}

    # ── Registration: the URIs the docs and tool responses point at ──────────
    for uri in ("agentvision://start_here", "agentvision://digest",
                "agentvision://capabilities", "agentvision://frame/latest.json",
                "agentvision://visual_changes", "agentvision://token_report",
                "agentvision://incidents", "agentvision://catalog"):
        check(f"{uri} is registered", uri in fixed, str(sorted(fixed)))
    for t in ("agentvision://frame/{seq}.json",
              "agentvision://frame/{seq}/region",
              "agentvision://incident/{incident_id}",
              "agentvision://log/raw{?from_offset}"):
        check(f"{t} is a TEMPLATE, not a fixed URI", t in tmpl,
              str(sorted(tmpl)))

    # A resource with no description is a URI the agent has no reason to open.
    thin = [u for u, r in list(fixed.items()) + [(k, v) for k, v in tmpl.items()]
            if len((getattr(r, "description", "") or "").strip()) < 40]
    check("every resource explains what it is for", not thin, str(thin))

    mimes = {getattr(r, "mime_type", None)
             for r in list(fixed.values()) + list(tmpl.values())}
    check("every resource declares application/json",
          mimes == {"application/json"}, str(mimes))

    # ── The mapping from URI to bridge call ──────────────────────────────────
    calls: list = []

    def _rec(path, params=None):
        calls.append((path, dict(params or {})))
        return {"stub": True, "path": path}

    real_get, M._http_get = M._http_get, _rec

    def read(uri: str):
        calls.clear()
        out = asyncio.run(M.mcp.read_resource(uri))
        return json.loads(list(out)[0].content)

    try:
        read("agentvision://catalog")
        check("agentvision://catalog hits the SAME endpoint as the tool",
              calls == [("/bridge/catalog", {})], str(calls))

        read("agentvision://frame/42.json")
        check("frame/{seq}.json resolves the sequence into the path",
              calls == [("/frame/42/json", {})], str(calls))

        read("agentvision://frame/42/region")
        check("frame/{seq}/region resolves the sequence into the path",
              calls == [("/frame/42/region", {})], str(calls))

        read("agentvision://incident/inc-7")
        check("incident/{id} passes the id as a query param",
              calls == [("/incidents", {"id": "inc-7"})], str(calls))

        # ── THE ONE THAT MATTERS: a resource read must not consume the log ───
        read("agentvision://log/raw")
        path, params = calls[0]
        check("log/raw reads the raw endpoint", path == "/log/raw", str(calls))
        check("...and PEEKS, so re-reading returns the same bytes",
              params.get("peek") == 1, str(params))
        check("...under its own session id, not the agent's 'default'",
              params.get("session_id") not in (None, "", "default"),
              str(params))
        check("...and with no offset it returns the whole retained tail",
              params.get("all") == 1, str(params))

        read("agentvision://log/raw?from_offset=8192")
        _, params = calls[0]
        check("an explicit from_offset is passed through as an int",
              params.get("from_offset") == 8192
              and isinstance(params.get("from_offset"), int), str(params))
        check("...and it does not ALSO ask for everything",
              "all" not in params, str(params))
        check("...and it still peeks", params.get("peek") == 1, str(params))

        # ── Bad parameters explain themselves and touch nothing ─────────────
        body = read("agentvision://frame/abc.json")
        check("a non-numeric frame sequence is an explained error",
              "error" in body and "abc" in body["error"], str(body))
        check("...and no bridge call is made on the way to failing",
              calls == [], str(calls))

        body = read("agentvision://frame/abc/region")
        check("the same is true for the region template",
              "error" in body and calls == [], str(body) + str(calls))

        body = read("agentvision://log/raw?from_offset=banana")
        check("a non-numeric from_offset is refused, not silently ignored",
              "error" in body and "banana" in body["error"], str(body))
        check("...and no partial read is issued", calls == [], str(calls))

        body = read("agentvision://incident/")
        check("an empty incident id says how to find a real one",
              "error" in body and "incidents" in json.dumps(body), str(body))
    finally:
        M._http_get = real_get

    # ── The frame 404 the resources surface must name what DOES exist ───────
    # Flask's stock 404 ("check your spelling") is a claim about a URL the
    # caller never typed; the true fact is which frames are retained.
    try:
        from api import bridge_server as bs
    except Exception as exc:
        print(f"  --   frame-404 checks skipped ({type(exc).__name__}: {exc})")
        bs = None

    if bs is not None:
        with bs.app.test_request_context():
            resp, code = bs._no_such_frame(7)
            body = resp.get_json()
        check("a missing frame 404s", code == 404, str(code))
        check("...carrying the `status` field every error body here has",
              body.get("status") == 404, str(body))
        check("...naming the sequence asked for", body.get("seq") == 7, str(body))
        check("...and how many frames are actually retained",
              "frames_retained" in body, str(body))
        check("...and it distinguishes an empty recorder from a pruned frame",
              "not a missing route" in (body.get("reason") or ""),
              str(body.get("reason")))
        check("...and gives the next call to make",
              bool(body.get("next")), str(body))

        # With frames present, the three cases must read differently, because
        # "ahead of the newest" and "pruned" are different diagnoses.
        saved = dict(bs._frames)
        try:
            bs._frames.clear()
            for s in (10, 11, 12):
                bs._frames[s] = {"sequence": s}
            with bs.app.test_request_context():
                ahead = bs._no_such_frame(99)[0].get_json()
                behind = bs._no_such_frame(2)[0].get_json()
                gap = bs._no_such_frame(11)[0].get_json()
            check("a seq past the newest says so and names the newest",
                  "ahead of" in ahead["reason"] and "12" in ahead["reason"],
                  ahead["reason"])
            check("a seq below the oldest says PRUNED, not 'never existed'",
                  "pruned" in behind["reason"] and "10" in behind["reason"],
                  behind["reason"])
            check("the retained range is reported",
                  ahead.get("retained_range") == [10, 12],
                  str(ahead.get("retained_range")))
            # 11 IS present, so asking about it is a programming error, not a
            # user-facing case — but the helper must not claim it was pruned.
            check("a seq inside the range is not mislabelled as pruned",
                  "pruned" not in gap["reason"], gap["reason"])
        finally:
            bs._frames.clear()
            bs._frames.update(saved)

    print()
    print(f"{'FAILED: ' + str(len(FAILED)) if FAILED else 'all checks passed'}")
    for f in FAILED:
        print(f"  - {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
