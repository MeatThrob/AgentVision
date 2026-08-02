"""
AgentVision MCP server — exposes the bridge as native Claude tools.

This is the AI-agent ergonomic surface for AgentVision. Once configured in
Claude Code (or any MCP-aware client), every diagnostic endpoint becomes a
native tool: get_frame, get_log_range, get_actions_around_frame,
list_bookmarks, get_bookmark, etc.

Generic — works for any program bridged into AgentVision. The bridge already
treats the connected program as a black box (logs + JSONL + screenshots);
this MCP wrapper preserves that genericity.

Run via stdio (default for Claude Code MCP configs):
    python -m python_backend.api.claude_mcp

Or directly:
    python claude_mcp.py

Register with Claude Code. This file imports only the standard library plus
`mcp`, so point Claude Code at the script path directly (no "cwd" needed):
    claude mcp add agentvision -- python /path/to/AgentVision/python_backend/api/claude_mcp.py
On Windows (use the real path to this file):
    claude mcp add agentvision -- python C:\\Users\\<you>\\AgentVision\\python_backend\\api\\claude_mcp.py
(First: pip install mcp — it is an optional dependency.)

The server proxies HTTP requests to the running bridge_server.py instance.
It does NOT spin up its own copy of the bridge — bridge must be running.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import urllib.request
import urllib.parse
import urllib.error

# ── mcp SDK import (version-tolerant) ─────────────────────────────────────────
# The Python MCP SDK renamed its high-level server class between major versions:
#   mcp <  2.0  →  mcp.server.fastmcp.FastMCP
#   mcp >= 2.0  →  mcp.server.mcpserver.MCPServer
# Both expose the same decorator surface we use (.tool()/.prompt()/.resource()/
# .run()), so we try each in turn and REPORT THE REAL ImportError if all fail —
# never a guessed "package not installed" message.
_MCP_IMPORT_ATTEMPTS: list[tuple[str, str]] = []
_MCPServer = None
Context = None          # the per-call session handle; None on an SDK without it
MCP_SDK_FLAVOR = "unknown"

for _mod, _cls, _flavor in (
    ("mcp.server.mcpserver", "MCPServer", "mcp>=2.0 (MCPServer)"),
    ("mcp.server.fastmcp", "FastMCP", "mcp<2.0 (FastMCP)"),
):
    try:
        _m = __import__(_mod, fromlist=[_cls])
        _MCPServer = getattr(_m, _cls)
        # Context rides along in the same module in both flavors. It is what
        # lets a tool ASK the user something instead of assuming — see
        # elicit.py. Absence is survivable; every ask has a fallback.
        Context = getattr(_m, "Context", None)
        MCP_SDK_FLAVOR = _flavor
        break
    except Exception as _e:  # ImportError, AttributeError, and anything the SDK raises
        _MCP_IMPORT_ATTEMPTS.append((f"{_mod}.{_cls}", f"{type(_e).__name__}: {_e}"))

if _MCPServer is None:
    try:
        import mcp as _mcp_pkg
        _where = getattr(_mcp_pkg, "__file__", "?")
        _ver = getattr(_mcp_pkg, "__version__", "unknown")
        _installed = f"'mcp' IS installed (version={_ver}, at {_where})"
    except Exception as _e:
        _installed = f"'mcp' package could NOT be imported at all ({type(_e).__name__}: {_e})"
    print("ERROR: could not load an MCP server class.", file=sys.stderr)
    print(f"  {_installed}", file=sys.stderr)
    print(f"  python: {sys.executable} ({sys.version.split()[0]})", file=sys.stderr)
    for _name, _err in _MCP_IMPORT_ATTEMPTS:
        print(f"  tried {_name} -> {_err}", file=sys.stderr)
    print("  Fix: pip install 'mcp' (or upgrade it) into THIS interpreter.", file=sys.stderr)
    sys.exit(1)

FastMCP = _MCPServer  # back-compat alias for anything importing FastMCP from here

# ── elicitation (asking the user) ─────────────────────────────────────────────
# This file is run BOTH as `python -m python_backend.api.claude_mcp` and as a
# bare script (`python claude_mcp.py`) — the SETUP guides use the second form —
# so the sibling import has to work either way. If it cannot be loaded at all,
# every ask degrades to its documented fallback and SAYS SO; it never becomes a
# silent default.
try:
    from . import elicit as _elicit                    # package import
except ImportError:                                    # pragma: no cover
    try:
        import elicit as _elicit                       # script import
    except Exception as _e:                            # last resort, still honest
        _elicit = None
        print(f"WARNING: elicit.py unavailable ({type(_e).__name__}: {_e}); "
              "AgentVision will use documented fallbacks instead of asking.",
              file=sys.stderr)
except Exception as _e:                                # pragma: no cover
    _elicit = None
    print(f"WARNING: elicit.py failed to import ({type(_e).__name__}: {_e}); "
          "AgentVision will use documented fallbacks instead of asking.",
          file=sys.stderr)


BRIDGE_BASE = os.environ.get("AGENTVISION_BRIDGE_URL", "http://127.0.0.1:7771")
HTTP_TIMEOUT = float(os.environ.get("AGENTVISION_HTTP_TIMEOUT", "5.0"))


def _http_get(path: str, params: dict | None = None) -> dict | list | str:
    url = BRIDGE_BASE.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
    except urllib.error.HTTPError as e:
        # PARSE the error body, as _http_post already did. The bridge answers a
        # missing frame with {error, status, seq, frames_retained,
        # retained_range, reason, next} — which is the whole point of that
        # answer. Returning it as an escaped JSON STRING under `body` buried it:
        # the agent saw {"error": "HTTP 404"} and a wall of backslashes, and the
        # useful part ("no frames have been captured yet — this is an empty
        # recorder, not a missing route") was one unescaping away from nobody.
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        out: dict = {"error": f"HTTP {e.code}", "status": e.code, "url": url}
        try:
            detail = json.loads(raw)
        except Exception:
            detail = None
        if isinstance(detail, dict):
            # The body's own `error` is the specific one; keep the HTTP code too.
            out["http_error"] = out.pop("error")
            out.update(detail)
        elif raw:
            out["body"] = raw[:2000]
        return out
    except urllib.error.URLError as e:
        return {"error": f"URL error: {e.reason}", "url": url,
                "hint": "Is bridge_server.py running on " + BRIDGE_BASE + " ?"}
    except Exception as e:
        return {"error": str(e), "url": url}


def _http_post(path: str, body: dict | None = None) -> Any:
    url = BRIDGE_BASE.rstrip("/") + path
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
    except urllib.error.HTTPError as e:
        # The body carries WHY. /adapter/add returns 422 with the offending regex
        # and the colliding sample; without this the agent sees only "422" and
        # cannot correct its own input.
        detail = {}
        try:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(raw)
            except json.JSONDecodeError:
                detail = {"body": raw[:2000]}
        except Exception:
            pass
        out = {"error": f"HTTP {e.code}: {e.reason}", "status": e.code, "url": url}
        if isinstance(detail, dict):
            out.update(detail)
        else:
            out["body"] = detail
        return out
    except Exception as e:
        return {"error": str(e), "url": url}


# ── async wrappers for the tools that ask the user something ─────────────────
# The HTTP helpers above are blocking urllib. Inside an `async def` tool that
# would hold the event loop for up to HTTP_TIMEOUT seconds, and the event loop
# is what carries the elicitation round-trip back from the client — so the tools
# that ask run their HTTP through a worker thread. Sync tools are unaffected.

async def _a(fn, *args, **kwargs):
    """Run a blocking bridge call off the event loop."""
    try:
        import anyio
    except Exception:                              # no anyio: correctness first
        return fn(*args, **kwargs)
    import functools
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


def _ask_note(target: dict, key: str, answer) -> dict:
    """Attach an Answer to a tool response under `key`, without ever dropping
    the sentence that says whether a human actually chose."""
    if not isinstance(target, dict) or answer is None:
        return target
    try:
        target[key] = answer.as_dict()
    except Exception:
        pass
    return target


# ── MCP server ────────────────────────────────────────────────────────────────
# `instructions` is handed to the client at initialize time, so the token thesis
# is in front of the agent BEFORE it makes its first call — that is what stops
# these tools from sitting unused.
_SERVER_INSTRUCTIONS = """\
AgentVision is a local debug flight recorder for ONE program (the "target"). It
screenshots that program on a timer, parses its logs, and time-aligns the two.
All of it runs on the user's CPU and costs you NOTHING.

=== ALWAYS DO THIS FIRST: av_start_here() ===
It tells you the target, whether the bridge is BUILT, and your exact next call.
If you read nothing else here, call av_start_here() and do what it says.

=== STEP ZERO: IS THE TARGET THE PROGRAM YOU ACTUALLY MEAN? ===
AgentVision watches ONE program at a time -- the ACTIVE PROFILE. It can hold many
profiles and switch between them, but only one is active, and every other tool
here answers about THAT one.

So before anything else, check that av_start_here()'s program is the program you
were asked about. If it is not, you are looking at someone else's bridge and
nothing you build will apply to yours:

  av_list_profiles()               -- does a profile for your program exist?
  av_create_profile(name=..., display_name=..., project_root=...,
                    capture_app=...)  -- if not, make one. project_root is the
                                      folder holding the code.
  av_set_active_profile(name=...)   -- point AgentVision at it.

Only then run the first-bridge sequence below. A cold model that skipped this has
committed a plan against the wrong program and had no way to tell -- BUILT means
"this active profile is built", never "your program is built".

A profile's bridge is sealed once built, but that is PER PROFILE. Adding a new
program is always possible; it is a new profile, not a re-plan.

=== YOU BUILD THE BRIDGE. THIS IS THE PART THAT SURPRISES PEOPLE. ===
AgentVision does NOT decide what to install into a program. YOU do, once, on
first connection. It deliberately refuses to guess, because it would pick the
same logging for a web server and a GPU emulator.

Until you do this, av_capture_start() and av_install_project() are REFUSED.
A refusal returns HTTP 200 with "started": false / "bridge_required": true --
so CHECK THE BODY, not just the status code.

The first-bridge sequence is exactly three calls:

  1. av_bridge_status()    -- is this program already built? If state is BUILT,
                              you are DONE; skip to normal use below.
  2. av_bridge_catalog()   -- every emitter, adapter, and MCP tool you may pick,
                              WITH the target's code evidence and a
                              catalog_token. Read it properly; it is the menu.
  3. av_bridge_commit(plan={...}, ...)  -- your decision. Builds the bridge.

Your plan must contain: catalog_token, emitters (list, may be []), why
({emitter: reason}), rationale (string), and tools ({"primary": [...],
"not_relevant": {tool: reason}}). It is REJECTED if the token is stale, if any
selected emitter has no reason, if you select nearly everything, or if tools is
missing. See av_bridge_commit's own docstring for the full shape.

THIS HAPPENS ONCE PER PROGRAM, EVER. The plan is saved to disk in the target
project. Restarting AgentVision, the bridge server, or your own session does NOT
re-trigger it. If av_bridge_status() says BUILT, never plan again -- use
av_bridge_commit(replan=True) only if you deliberately want to re-decide.

=== NORMAL USE, once the bridge is BUILT ===
  av_diagnose        something is wrong: ranked root-cause hypotheses
  av_log_raw         the RAW log, verbatim, uninterpreted
  av_visual_changes  review a capture run -- NOT by opening frames
  av_error_moment    one-call bundle for a specific failure
  av_incidents       pre-error windows already frozen for you
  av_session_report  wrap up

DO NOT take your own screenshots, grep raw log files, page through frames one at
a time, or hand-correlate a timestamp to a frame. A tool already did it.

=== THE TOKEN RULE -- cheapest sufficient tier first ===
  1. av_visual_changes / av_frame_json  (JSON, no image at all)
  2. av_frame_json(thumbnail=True)      (tiny thumb)
  3. av_frame_region                    (ONLY the pixels that changed)
  4. av_get_frame + read the PNG        (full image -- last resort)
At 10 shots/sec ~99% of consecutive frames are pixel-identical; av_visual_changes
collapses those runs so ten minutes of capture reads in a few hundred tokens.
av_token_report shows what this actually saved.

Full guide for AI agents: docs/AI_START_HERE.md in the AgentVision repo."""

try:
    from mcp.types import ToolAnnotations as _ToolAnnotations
except Exception:                                    # older/newer SDK shape
    _ToolAnnotations = None

try:
    from mcp.types import Annotations as _Annotations
except Exception:
    _Annotations = None


def _res_ann(priority: float, assistant_only: bool = True):
    """Resource annotations (audience + priority) when the SDK supports them.
    Clients use these to rank/attach context automatically."""
    if _Annotations is None:
        return None
    try:
        aud = ["assistant"] if assistant_only else ["assistant", "user"]
        return _Annotations(audience=aud, priority=priority)
    except Exception:
        return None


def _ro(title: str):
    """Read-only tool annotation when the installed SDK supports it."""
    if _ToolAnnotations is None:
        return None
    try:
        return _ToolAnnotations(title=title, read_only_hint=True,
                                destructive_hint=False, idempotent_hint=True)
    except Exception:
        return None


# ── PORTABLE PUSH: resource-updated notifications ─────────────────────────────
# Push Mode (tools/agentvision_hook.py) is a CLAUDE CODE hook. In Cursor, VS
# Code, or any other MCP client, AgentVision's best feature — telling the agent
# something broke without being asked — is simply silent. MCP's own answer is a
# resource-updated notification, which needs no client-specific hook.
#
# THREE RULES, each guarding a way this could go wrong:
#
#  1. NO SUBSCRIBERS, NO WORK. The poller is started by the first client that
#     opens a subscription stream and stopped when the last one closes. A stdio
#     server with nobody listening does nothing at all.
#  2. IT NEVER CONSUMES. It polls /ambient with force=1, which by design skips
#     mark_surfaced, the raw-log offset commit, and mark_offered. If it consumed,
#     it would eat the very lines the Claude Code hook was about to deliver, and
#     the loss would look like a program that went quiet.
#  3. IT STANDS DOWN FOR THE OTHER CHANNEL. If another ambient session was
#     injected recently, the hook is working and this channel says nothing —
#     an agent told twice cannot tell it was one event.
#
# LIMITATION, stated rather than discovered: this reaches `subscriptions/listen`
# streams. The SDK's high-level server does not implement the older
# `resources/subscribe` request (it advertises resources.subscribe=false), so a
# client that only speaks that older form is NOT reached by this. Such a client
# is pull-only, exactly as it is today; nothing regresses, but nothing improves.
_PUSH_ENABLED = os.environ.get("AGENTVISION_SUBSCRIBE_PUSH", "1") not in (
    "0", "", "false", "no")
_PUSH_INTERVAL_S = max(2.0, float(os.environ.get(
    "AGENTVISION_SUBSCRIBE_POLL_S", "10")))
#: While another channel has injected within this window, stay quiet.
_PUSH_QUIET_MS = float(os.environ.get("AGENTVISION_SUBSCRIBE_QUIET_MS", "120000"))
#: Its own ambient session id, so its bookkeeping cannot collide with a hook's.
_PUSH_SESSION_ID = "mcp-resource-subscription"

#: Everything this channel has done, reported through av_capabilities. A push
#: channel that fails silently is the same shape as one that has nothing to say.
_push_state: dict = {
    "enabled": _PUSH_ENABLED, "listeners": 0, "running": False,
    "polls": 0, "published": 0, "quiet_for_other_channel": 0,
    "errors": 0, "last_error": "", "last_published_uri": "",
    "poll_interval_s": _PUSH_INTERVAL_S,
    #: Survives a task restart on purpose — see _push_loop.
    "last_fingerprint": None,
}

try:
    from mcp.server.subscriptions import (InMemorySubscriptionBus as _InMemBus,
                                          ResourceUpdated as _ResourceUpdated)
except Exception:                                    # SDK without subscriptions
    _InMemBus = _ResourceUpdated = None


class _PollWhileSubscribed:
    """A SubscriptionBus that runs AgentVision's poller only while listened to.

    Wraps the SDK's in-memory bus rather than replacing it: fan-out semantics
    stay the SDK's problem. All this adds is a listener count and the lifecycle
    it implies.
    """

    def __init__(self) -> None:
        self._inner = _InMemBus()
        self._count = 0
        self._task = None

    async def publish(self, event) -> None:
        await self._inner.publish(event)

    def subscribe(self, listener):
        off_inner = self._inner.subscribe(listener)
        self._count += 1
        _push_state["listeners"] = self._count
        self._start()
        stopped = False

        def unsubscribe() -> None:
            nonlocal stopped
            if not stopped:
                stopped = True
                self._count = max(0, self._count - 1)
                _push_state["listeners"] = self._count
                if self._count == 0:
                    self._stop()
            off_inner()

        return unsubscribe

    def _start(self) -> None:
        if not _PUSH_ENABLED or self._task is not None:
            return
        try:
            import asyncio
            self._task = asyncio.ensure_future(_push_loop(self))
            _push_state["running"] = True
        except Exception as e:                        # no loop: stay pull-only
            _push_state["last_error"] = f"could not start poller: {e}"

    def _stop(self) -> None:
        task, self._task = self._task, None
        _push_state["running"] = False
        if task is not None:
            try:
                task.cancel()
            except Exception:
                pass


async def _push_loop(bus) -> None:
    """Poll what AgentVision would say, and announce only genuine changes.

    The last announced fingerprint lives in `_push_state`, NOT in a local, so
    a client that drops its stream and re-listens does not get the same fact
    announced again. It comes from the bridge's `content_fp`, which is derived
    from the signals and the actual log bytes — hashing the rendered text
    announced the same unchanged log twice in five seconds, because that text
    carries a live "LAST WRITE 144s AGO" clock.
    """
    import asyncio
    while True:
        try:
            await asyncio.sleep(_PUSH_INTERVAL_S)
            amb = await _a(_http_get, "/ambient",
                           {"force": "1", "session_id": _PUSH_SESSION_ID})
            _push_state["polls"] += 1
            if not isinstance(amb, dict) or amb.get("error"):
                if isinstance(amb, dict) and amb.get("error"):
                    _push_state["errors"] += 1
                    _push_state["last_error"] = str(amb.get("error"))[:200]
                continue
            if not amb.get("inject") or amb.get("tier") in ("silent", "heartbeat"):
                # A heartbeat is "still watching, all normal". Waking a client
                # for that is exactly the chatter that trains agents to ignore
                # a channel.
                continue
            fingerprint = amb.get("content_fp")
            if not fingerprint:
                # An older bridge without content_fp. Say so rather than fall
                # back to hashing the text, which is the chatter bug.
                _push_state["last_error"] = (
                    "the bridge did not return content_fp; this channel cannot "
                    "tell a repeat from a change, so it is staying quiet")
                continue
            if fingerprint == _push_state.get("last_fingerprint"):
                continue
            other = amb.get("other_channel_ms_ago")
            if other is not None and other < _PUSH_QUIET_MS:
                _push_state["quiet_for_other_channel"] += 1
                _push_state["last_fingerprint"] = fingerprint
                continue
            _push_state["last_fingerprint"] = fingerprint
            uris = ["agentvision://digest"]
            if amb.get("incident_ids"):
                uris.append("agentvision://incidents")
            for uri in uris:
                await bus.publish(_ResourceUpdated(uri=uri))
                _push_state["published"] += 1
                _push_state["last_published_uri"] = uri
        except asyncio.CancelledError:
            _push_state["running"] = False
            raise
        except Exception as e:                        # a poller must not die
            _push_state["errors"] += 1
            _push_state["last_error"] = f"{type(e).__name__}: {e}"[:200]


_SUBSCRIPTIONS = _PollWhileSubscribed() if _InMemBus is not None else None

try:
    mcp = FastMCP("agentvision", instructions=_SERVER_INSTRUCTIONS,
                  version="5.1", subscriptions=_SUBSCRIPTIONS)
except TypeError:                                    # SDK without those kwargs
    try:
        mcp = FastMCP("agentvision", instructions=_SERVER_INSTRUCTIONS,
                      version="5.1")
    except TypeError:
        mcp = FastMCP("agentvision")
    _push_state["enabled"] = False
    _push_state["last_error"] = ("this MCP SDK does not accept a subscription "
                                 "bus; resource-updated push is unavailable")


@mcp.tool()
def av_status() -> dict:
    """AgentVision bridge status: active profile, frame count, the `capture_rate`
    envelope (current shots/sec + supported range + the guidance to ASK THE USER
    how many screenshots per second they want), a `preflight` hint, and the
    `token_rule` reminder. Call this to confirm the bridge is live — for a fuller
    orientation call av_start_here instead.

    THE TOKEN RULE: capture, perceptual hashing and diffing are FREE local CPU;
    your tokens are not. Prefer av_visual_changes / av_frame_json over raw
    frames; escalate to pixels (av_frame_region) only when JSON is insufficient.

    THE FORCE: before starting capture on a NEW program, call av_preflight; if it
    reports gaps, call av_add_adapter for each missing debug-log type, then start.
    The `preflight.ok` field tells you whether that coverage check has passed for
    the active program yet.

    INVESTIGATING A FAILURE? Call av_diagnose FIRST — it returns ranked root-cause
    hypotheses with evidence and the exact follow-up tool calls."""
    return _http_get("/status")


@mcp.tool()
def av_preflight(project_root: str = "", language: str = "",
                 sample_lines: list | None = None,
                 log_paths: list | None = None,
                 accept_gaps: bool = False) -> dict:
    """PRE-FLIGHT log-coverage check — RUN THIS BEFORE THE FIRST CAPTURE ON A NEW
    PROGRAM. It verifies AgentVision can SPECIFICALLY parse the program's
    log/debug-log data (not just fall back to the generic normalizer), which is
    what makes the frames-plus-logs diagnosis actually work.

    It detects the target's language, samples the real first lines of every
    configured log source, routes them through the adapter detector, and flags any
    source whose lines only resolve to a generic FALLBACK (structural / generic_ts
    / raw) as a `gap` — a debug-log type AgentVision does not yet understand.

    Returns {ready, language, covered:[{source,adapter,confidence}],
    gaps:[{source_or_format, sample, current_fallback, suggestion}], pending,
    recommended_actions, directive}.

    IF `gaps` IS NON-EMPTY: call av_add_adapter once per gap (build the adapter
    from the gap's `sample`), then call av_preflight again until ready:true. Only
    then start capture. Args let you check an explicit project_root / language /
    raw sample_lines / log_paths instead of the active profile. accept_gaps=true
    records the coverage as accepted (sets the marker) even with gaps present."""
    body: dict = {"project_root": project_root, "language": language,
                  "accept_gaps": accept_gaps}
    if sample_lines is not None:
        body["sample_lines"] = sample_lines
    if log_paths is not None:
        body["log_paths"] = log_paths
    return _http_post("/preflight", body)


@mcp.tool()
def av_add_adapter(name: str, extract_regex: str, sample: str,
                   detect_regex: str = "", anchor_tokens: list | None = None,
                   family: str = "", language: str = "any",
                   level_map: dict | None = None, default_level: str = "",
                   default_source: str = "", category: str = "",
                   match_scope: str = "lines", outrank: str = "",
                   also_match: list | None = None) -> dict:
    """RUNTIME SELF-EXTENSION — add a log adapter so AgentVision can specifically
    parse a debug-log format it currently only handles with a generic fallback.
    Use this to CLOSE each gap av_preflight reports, BEFORE starting capture.

    Build the adapter from the gap's sample line:
      • extract_regex — a regex with named groups over ONE log line. Recognized
        groups: (?P<ts>…) timestamp, (?P<level>…) level word, (?P<source>…)
        logger/subsystem, (?P<message>…) the human message. `message`→msg and
        `timestamp`→ts are aliased for you.
      • sample — a REAL line of this format (from the gap). The adapter MUST route
        this line to itself or the add is rejected.
      • also_match — MORE real lines from the same file, and they are ENFORCED:
        the pattern must match every one. Pass the list the catalog gives you in
        existing_logs_found[].how_to_add_an_adapter.example_body.also_match. One
        sample only proves the adapter fits ONE LINE: a cold run anchored on a
        fragment of that line's message, scored 1.00, and shipped an adapter that
        parsed 1 line in 4 while everything else fell to a fallback.
      • detect_regex / anchor_tokens — optional, tighter detection signature. If
        the adapter would STEAL another format's catalog sample, the add is
        rejected with the offending sample — add anchor_tokens (literal substrings
        that must appear) or tighten the regex so it only matches its own format.
      • level_map — raw token → canonical level (e.g. {"WARNING":"WARN"}).
      • match_scope — "lines" (default) or "first" for multi-line records.
  • outrank — the name of an existing adapter that claims this format but parses
    it WRONG. Placement breaks a 1.0 tie in your favour (registers immediately
    before it). Needed because "the format is already covered" is not the same as
    "covered correctly": a C engine logging `[DEBUG] Sprite.c:32 - msg` was being
    claimed by `coreboot_cbmem`, which buried the file:line in the message. It
    only breaks TIES — it cannot rescue a weaker pattern.

    The adapter is registered LIVE and PERSISTED (survives restart). Idempotent by
    name. Returns {ok, adapter_name, persisted, registered, errors, self_route,
    collisions}. On ok=true, re-run av_preflight — the gap should now be covered."""
    spec: dict = {
        "name": name,
        "family": family,
        "language": language,
        "extract": {"regex": extract_regex},
        "sample": sample,
        "match_scope": match_scope,
        # Placement request — breaks a 1.0 tie against a named incumbent.
        "outrank": outrank,
    }
    detect: dict = {}
    if detect_regex:
        detect["regex"] = detect_regex
    if anchor_tokens:
        detect["anchor_tokens"] = anchor_tokens
    if detect:
        spec["detect"] = detect
    if level_map:
        spec["level_map"] = level_map
    if default_level:
        spec["default_level"] = default_level
    if default_source:
        spec["default_source"] = default_source
    if category:
        spec["category"] = category
    if also_match:
        spec["also_match"] = list(also_match)
    return _http_post("/adapter/add", spec)


@mcp.tool()
def av_latest_frame() -> dict:
    """Fetch the newest SnapshotFrame (what is on screen NOW) — screenshot +
    the program's exact state at the shutter instant. This is the EXPENSIVE tier.

    ⚠️ CHEAPER FIRST: av_read_screen reads the on-screen text as JSON, and
    av_frame_json(seq) describes a frame (perceptual hash, changed region,
    on-screen text, aligned logs) with NO image at all. Use THIS tool when you
    genuinely need to look at the whole picture — reading the PNG costs
    hundreds-to-thousands of visual tokens.

    DO NOT call this repeatedly to survey a run — that is what av_visual_changes
    is for.

    When you do use it: look at the image at `annotated_image` (or
    `_ai.image_path`) — do not reason from the JSON alone. The `_ai` block gives
    the shutter timestamp, the exact time-aligned log window, the paired
    _frame.json, the capture rate, the visual change summary, a `CHEAPER_PATH`
    block, and the follow-up calls to correlate image↔logs
    (av_actions_around_frame, av_log_normalized, av_state_at). If `_ai.WARNING`
    is present the frame is blank/black — do not describe visual content."""
    return _http_get("/latest")


@mcp.tool()
def av_get_frame(seq: int) -> dict:
    """Fetch SnapshotFrame N by sequence number — the full screenshot plus the
    exact, time-aligned program state at that moment. EXPENSIVE tier: this is the
    last resort of the frame-inspection ladder.

    ⚠️ TRY THESE FIRST — they are far cheaper and usually enough:
      av_frame_json(seq)    the frame as JSON, no image bytes
      av_frame_region(seq)  ONLY the pixels that changed, downscaled
      av_error_moment(seq)  the whole failure bundle, pre-correlated

    DO NOT loop this over a range of sequences; use av_visual_changes.

    Like av_latest_frame, the `_ai` block tells you the image path, the shutter
    time, the bounded log window, the visual change summary, and how to pull the
    actions/logs that line up with this exact frame. Read the image together
    with the JSON."""
    return _http_get(f"/frame/{int(seq)}")


@mcp.tool()
def av_actions_around_frame(seq: int, window_secs: float = 5.0) -> dict:
    """Return all structured action records within ±window_secs of frame N's
    shutter timestamp — the log/actions that line up with that exact screenshot.
    This is the core correlation move: see something in the frame image, then
    call this to learn what the program logged/did at that instant. For a
    format-normalized view across MULTIPLE logs use av_log_normalized; to prove
    the alignment is exact use av_frame_alignment."""
    return _http_get(f"/frame/{int(seq)}/actions", {"window_secs": window_secs})


@mcp.tool()
def av_log_range(from_ms: float, to_ms: float,
                 category: str | None = None,
                 source: str | None = None,
                 limit: int = 500) -> dict:
    """Time-range query against the active program's structured action log.
    Filter by category (key/move/cast/event/...) and/or source substring."""
    return _http_get("/log/range", {
        "from_ms": from_ms, "to_ms": to_ms,
        "category": category, "source": source, "limit": limit,
    })


@mcp.tool()
def av_list_bookmarks() -> dict:
    """List all auto-detected failure bookmarks across the active program's
    JSONL. Triggers: any record with category='error', data.name='run.fail',
    or source containing 'fail'/'error'/'crash'.

    Also returns `visual_bookmarks` — moments the SCREEN flagged (freeze, blank
    frame, big layout change, on-screen error text), detected for free at capture
    time. A hang usually leaves NO log record, so the visual list catches
    failures this log-driven one cannot. av_visual_events has the detectors and
    thresholds; av_error_moment(seq) bundles any of them."""
    return _http_get("/bookmarks")


@mcp.tool()
def av_get_bookmark(bookmark_id: str) -> dict:
    """Fetch the full context bundle for a bookmark: 30s before + trigger +
    10s after, plus the closest matching frame_seq."""
    return _http_get(f"/bookmark/{urllib.parse.quote(bookmark_id, safe='')}")


@mcp.tool()
def av_program_log(lines: int = 40) -> dict:
    """Tail the bridged program's primary text log."""
    return _http_get("/program/log", {"lines": lines})


@mcp.tool()
def av_program_status() -> dict:
    """Whether the bridged program process is running, plus profile info."""
    return _http_get("/program/status")


@mcp.tool()
def av_list_profiles() -> dict:
    """List all configured program profiles in AgentVision."""
    return _http_get("/profiles")


@mcp.tool()
def av_active_profile() -> dict:
    """Get the currently active profile (which program AV is bridged to)."""
    return _http_get("/profiles/active")


@mcp.tool()
def av_capture_status() -> dict:
    """Capture-loop status AND the screenshot-rate envelope you must act on.

    Returns: engine_running, capturing, interval (seconds/shot), shots_per_second,
    frame_count, a `health` block (last_latency_ms, blank_frame_count,
    window_missing, last_warning), and a `rate` block with the FULL supported
    range in BOTH seconds/shot and shots-per-second plus a `guidance` string.

    IMPORTANT — the screenshot cadence is user-configurable and most models
    under-use it. At the START or CONTINUATION of EVERY project you should ASK
    THE USER how many screenshots per second they want, present the full
    supported range (typically 0.1–10 shots/sec), then apply it with
    av_capture_set_interval(interval = 1 / desired_fps). Read the `rate.guidance`
    field and follow it."""
    return _http_get("/capture/status")


@mcp.tool()
def av_codebase_map() -> dict:
    """Return the bridged program's codebase map (modules, dependencies)."""
    return _http_get("/codebase-map")


# ── v3 surfaces ──────────────────────────────────────────────────────────────

@mcp.tool()
def av_events_schema() -> dict:
    """Discover the event vocabulary the bridged program emits.
    Returns static category docs PLUS auto-discovered (category, event-name)
    pairs scanned from the active JSONL. Read this once per session."""
    return _http_get("/events/schema")


@mcp.tool()
def av_errors_by_fingerprint(fp: str = "") -> dict:
    """Without fp: histogram of all failure fingerprints (recurring bugs).
    With fp: every record sharing that fingerprint, oldest→newest."""
    return _http_get("/errors/by-fingerprint", {"fp": fp or None})


@mcp.tool()
def av_trace_timeline(trace_id: str) -> dict:
    """All JSONL records and frames belonging to one logical action span.
    Use this to reason end-to-end about a single Pindle kill, login attempt,
    nav route, etc."""
    return _http_get(f"/trace/{urllib.parse.quote(trace_id, safe='')}/timeline")


@mcp.tool()
def av_state_at(at_ms: float) -> dict:
    """Return the program's wide-event state nearest the given epoch ms.
    One call gives you HP/MP/position/target/run_id at any moment in time."""
    return _http_get("/wide", {"at_ms": at_ms})


# ── Universal multi-language log tools ──────────────────────────────────────
# AgentVision normalizes ANY program's logs — in ANY language/format — into one
# unified JSON event schema and merges N logs onto a single time-aligned
# timeline. These are the tools for that.

@mcp.tool()
def av_log_sources() -> dict:
    """List the log sources AgentVision is watching for the active program, and
    the AUTO-DETECTED format of each (jsonl, log4j, serilog, python_logging,
    logfmt, syslog, logcat, rust, go_zap, …). Call this first to confirm 'the
    log is ready' for whatever language the target is written in.

    Each source's `adapter` field is the adapter the merge ACTUALLY uses for
    it (explicit config > reader:<name> > auto-detected; None only when the
    file is missing); configured_adapter / detected_adapter / detect_confidence
    give the breakdown. Also returns the full list of available adapters. If a
    source's detected_adapter looks wrong, set an explicit adapter on that
    source in the profile."""
    return _http_get("/log/sources")


@mcp.tool()
def av_log_normalized(from_ms: float | None = None, to_ms: float | None = None,
                      level: str = "", label: str = "", limit: int = 500) -> dict:
    """Merged, time-aligned, format-normalized view across ALL of the active
    program's logs at once — no matter what language/format each one is in.

    Every line becomes a unified event: {ts, ts_ms, level, category, source,
    data.message, trace_id, log_label, log_path, …}. Use this instead of
    av_program_log when the target writes several logs, or writes in a
    non-JSONL format (Java/Log4j, .NET/Serilog, Go, Rust, Python logging,
    syslog, Android logcat, plain text — all handled).

      from_ms/to_ms  epoch-ms window. DEFAULT (both omitted): NO time filter —
                     you get the most recent `limit` events by count, so
                     history with old timestamps is never silently hidden.
                     Pass a tight window around a frame's shutter_ms to see
                     exactly what all logs said at that screenshot; pass only
                     from_ms for "since t", only to_ms for "up to t".
      level          minimum level filter (e.g. 'WARN' → WARN/ERROR/FATAL only)
      label          restrict to one source (see av_log_sources labels)
      limit          max events (keeps most recent)."""
    params = {"level": level or None, "label": label or None, "limit": limit}
    if from_ms is not None:
        params["from_ms"] = from_ms
    if to_ms is not None:
        params["to_ms"] = to_ms
    return _http_get("/log/normalized", params)


@mcp.tool()
def av_frame_alignment(seq: int) -> dict:
    """Prove the image↔log time-alignment for a frame is exact. Recomputes,
    from the frame's own capture metadata, whether every log record bounded into
    that frame's context truly predates the shutter. Returns aligned=True with
    leaked_after_shutter=0 for a healthy frame. Use this when you need to fully
    trust that 'the log shown lines up with the screenshot' for a specific
    moment — the user considers this correctness critical."""
    return _http_get(f"/frame/{int(seq)}/alignment")


@mcp.tool()
def av_bookmark_outliers(bookmark_id: str) -> dict:
    """Honeycomb BubbleUp-style: rank numeric fields by how much their mean
    in the 30s before the failure deviates from session baseline.
    The top-z field is usually the smoking gun."""
    return _http_get(f"/bookmark/{urllib.parse.quote(bookmark_id, safe='')}/outliers")


@mcp.tool()
def av_new_errors_this_session() -> dict:
    """Failure fingerprints first seen in the current bridge session — these
    are bugs that JUST appeared, the highest-priority signal."""
    return _http_get("/anomalies/new")


@mcp.tool()
def av_frame_overlay(seq: int) -> dict:
    """Structured overlay layer for a frame: detections, OCR reads, path
    waypoints, walks, casts — all from JSONL records matching this frame.
    Lets you reason 'saw monster HERE, walked THERE' without pixel parsing."""
    return _http_get(f"/frame/{int(seq)}/overlay")


@mcp.tool()
def av_frame_annotate(seq: int, message: str, level: str = "info",
                      tags: list[str] | None = None) -> dict:
    """Leave a note on a frame for your future self. Persists to disk next to
    the frame JSON, returned by subsequent fetches and av_frame_annotations."""
    return _http_post(f"/frame/{int(seq)}/annotate",
                      {"message": message, "level": level, "tags": tags or []})


@mcp.tool()
def av_frame_annotations(seq: int) -> dict:
    """Read all annotations previously left on this frame."""
    return _http_get(f"/frame/{int(seq)}/annotate")


# ── Source mirror tools (READ THESE FIRST when starting a session) ──────────

@mcp.tool()
def av_source_refresh(mirror: bool = False) -> dict:
    """Re-walk the bridged project's source tree and rebuild the cached
    index/light/digest/tree files. Pass mirror=True to also copy the source
    files into AgentVision's snapshots/<profile>/source_mirror/ dir."""
    return _http_post(f"/source/refresh{'?mirror=1' if mirror else ''}")


@mcp.tool()
def av_source_light() -> dict:
    """Token-frugal map of EVERY file in the bridged project — path, lang,
    line count, and a 1-line summary per file. Designed to be the FIRST
    thing you read when you need to understand the project. Typical size:
    5-15K tokens for a few-hundred-file repo."""
    return _http_get("/source/light")


@mcp.tool()
def av_source_tree() -> dict:
    """Hierarchical file tree of the bridged project. Use to navigate
    folder structure visually before drilling into files."""
    return _http_get("/source/tree")


@mcp.tool()
def av_source_digest(prefix: str = "") -> dict:
    """Full per-file digest: every top-level def/class/const with signatures
    and 1-line docstrings. Heavier than av_source_light — use prefix='src/'
    or similar to scope to one subdir. Empty prefix returns whole project."""
    return _http_get("/source/digest", {"prefix": prefix or None})


@mcp.tool()
def av_source_file(path: str, from_line: int = 1,
                   to_line: int | None = None) -> dict:
    """Read ONE source file from the bridged project. Path is relative to
    project_root (e.g. 'src/main.py'). Use from_line/to_line for pagination
    on large files. This is the surgical read tool — call it after orienting
    with av_source_light or av_source_digest."""
    params = {"path": path, "from_line": from_line}
    if to_line is not None:
        params["to_line"] = to_line
    return _http_get("/source/file", params)


@mcp.tool()
def av_source_search(q: str, case: bool = False, limit: int = 200) -> dict:
    """Grep across the bridged project's source. Returns matches as
    {path, line, text}. Use this to locate where a symbol/string lives
    BEFORE calling av_source_file. Case-insensitive by default."""
    return _http_get("/source/search", {
        "q": q, "case": "1" if case else "0", "limit": limit,
    })


@mcp.tool()
def av_source_list() -> dict:
    """Flat list of every indexed file path with size + lines + language.
    Like a `find . -type f` for the bridged project, with metadata."""
    return _http_get("/source/list")


# ── Parity tools — every GUI tab now has a matching MCP surface ─────────────
#
# Mission: anything a human can see in the AgentVision GUI, Claude can fetch
# via this MCP. No information asymmetry. The tools below close every gap
# identified by the audit on 2026-04-30.

@mcp.tool()
def av_program_stats(lines: int = 200) -> dict:
    """Every `key: value` pair from the newest stats_*.log in the profile's
    stats_folder. Pairs with av_program_log for narrative + numbers.

    `lines` limits the read to the last N lines (0 = the whole file); it used to
    be accepted and ignored. The parser also used to keep only nine hardcoded
    Diablo-II-bot keys and drop everything else — it now returns all of them,
    with those nine still present under their old names. An empty result means
    the profile's stats_folder has no stats_*.log, not that the program emits no
    metrics."""
    return _http_get("/program/stats", {"lines": lines})


@mcp.tool()
def av_program_crop() -> dict:
    """Capture-window / crop bounds for the bridged program — what region
    of the screen AgentVision is screenshotting. Useful when a frame's
    contents look offset or clipped."""
    return _http_get("/program/crop")


@mcp.tool()
def av_debug_log(lines: int = 100) -> dict:
    """Tail AgentVision's OWN debug log. Use this to diagnose bridge
    issues: missing frames, source-mirror failures, daemon problems,
    capture errors. The 'meta' log — what the bridge is doing TO debug
    the bridged program."""
    return _http_get("/debug/log", {"lines": lines})


@mcp.tool()
def av_observer_log(limit: int = 200, profile: str = "") -> dict:
    """AgentVision's OWN observations about the bridged program, as JSONL records:
    stuck-watchdog verdicts (`program.stuck`), process start/exit transitions, and
    any waypoints you left with av_log_push.

    These are kept OUT of the program's own log on purpose. They used to be
    appended into it, where they outnumbered the program's records 20:1 and made a
    48-hour-dead process look live. Treat `program_silent_s` in a STORED record as
    a historical observation; for the current figure use av_log_sources() or
    av_diagnose(), which recompute it at read time."""
    q = {"limit": limit}
    if profile:
        q["profile"] = profile
    return _http_get("/observer/log", q)


@mcp.tool()
def av_run_tests() -> dict:
    """Trigger AgentVision's bridged-program test runner. Returns
    pass/fail summary + collected output."""
    return _http_post("/run-tests")


@mcp.tool()
def av_log_push(message: str, category: str = "note",
                source: str = "claude.note",
                data: dict | None = None) -> dict:
    """Leave a structured waypoint in AgentVision's OWN observer log — e.g.
    'started investigating bug X', 'reproduced at frame 42'.

    It does NOT go into the program's actions.jsonl, deliberately: a note from the
    observer is not the program's output, and mixing the two is how one real
    project's action log ended up 95% AgentVision. Read notes back with
    av_observer_log(). (This docstring used to promise actions.jsonl,
    av_log_range and av_actions_around_frame; the handler wrote a plain line to
    activity.log and dropped category/source/data on the floor.)"""
    body = {
        "message": message,
        "category": category,
        "source": source,
        "data": data or {},
    }
    return _http_post("/log", body)


@mcp.tool()
def av_create_profile(name: str, profile: dict) -> dict:
    """Create or update a program profile (works for ANY language — the target
    need not be Python). `profile` keys: display_name, project_root, log_file,
    action_log_file, capture_app, capture_crop, process_name, capture_user_input,
    language, and log_sources — a list of {path, adapter, label} for watching
    MULTIPLE logs at once (adapter='auto' auto-detects the format). The bridge
    expects these fields flat, so they are merged with `name` here."""
    body = {"name": name}
    if isinstance(profile, dict):
        body.update(profile)
    return _http_post("/profiles", body)


@mcp.tool()
def av_set_active_profile(name: str) -> dict:
    """Switch the bridge's active profile. All subsequent reads (frames,
    logs, source) target this profile's project. The input daemon
    re-reads the active profile within ~2 s and routes events accordingly."""
    return _http_put("/profiles/active", {"name": name})


@mcp.tool()
def av_delete_profile(name: str) -> dict:
    """Delete a non-built-in profile. Refuses the ACTIVE profile (409) and any
    built-in (400); switch active first with av_set_active_profile. Returns
    {"deleted": true|false} — check that field, not just the status code."""
    return _http_delete(f"/profiles/{urllib.parse.quote(name, safe='')}")


@mcp.tool()
async def av_capture_start(interval: float | None = None, force: bool = False,
                           ctx: Context = None) -> dict:
    """Start the auto-capture loop (periodic screenshot + time-aligned frame).

    THE FORCE: before starting capture on a NEW program, call av_preflight; if it
    reports gaps, call av_add_adapter for each missing debug-log type, then start.
    On the FIRST start for a program whose preflight has not run, this returns
    {"preflight_required": true, "preflight": {…}, "started": false} instead of
    starting — run av_preflight (and av_add_adapter for any gaps) first, then call
    this again. Passing force=true starts anyway and accepts the current coverage
    (the GUI's Start button does this).

    `interval` is SECONDS PER SHOT (e.g. 0.25 = 4 shots/sec, 1.0 = 1 shot/sec,
    0.1 = 10 shots/sec, the fastest supported).

    LEAVE `interval` OUT and AgentVision asks the user directly — it puts the
    question and the supported range in front of them and uses their answer. It
    only does that where the client supports MCP elicitation; where it does not,
    the profile's existing rate is used and the response says, in
    `capture_rate_choice`, that nobody was asked. Read that field before telling
    the user what rate you are running at. Pass `interval` explicitly only when
    the user has ALREADY told you what they want."""
    rate_answer = None
    if interval is None and _elicit is not None:
        # Reading the current rate is a courtesy (it lets the prompt say what
        # the rate is now); failing to read it must not cancel the question.
        current = None
        try:
            st = await _a(_http_get, "/status")
            if isinstance(st, dict):
                cr = st.get("capture_rate") or {}
                current = cr.get("interval_seconds") or cr.get("interval")
                current = float(current) if current else None
        except Exception:
            current = None
        rate_answer = await _elicit.ask_capture_rate(
            ctx, fallback_interval=float(current or 1.0),
            current_interval=current)
        if rate_answer.chosen_by_user:
            interval = float(rate_answer.value)
    body: dict = {}
    if interval is not None:
        body["interval"] = interval
    if force:
        body["force"] = True
    out = await _a(_http_post, "/capture/start", body)
    return _ask_note(out, "capture_rate_choice", rate_answer)


@mcp.tool()
def av_capture_stop() -> dict:
    """Stop the auto-capture loop. Bridge stays up; just no new frames."""
    return _http_post("/capture/stop")


@mcp.tool()
async def av_capture_set_interval(interval: float | None = None,
                                  ctx: Context = None) -> dict:
    """Set the capture cadence in SECONDS PER SHOT. Takes effect on the next tick.

    Convert the user's shots-per-second to interval: interval = 1 / fps
    (4 shots/sec → 0.25, 2 → 0.5, 10 → 0.1). Range: interval 0.1s (10 shots/sec,
    fastest) up. Prefer a faster rate when debugging something that changes
    quickly (animation, a race, a crash) and a slower rate for long idle waits.

    CALL IT WITH NO ARGUMENT to have AgentVision ask the user instead of
    guessing. Where the client cannot show a prompt the rate is left unchanged
    and `capture_rate_choice` says so — it is never quietly reset to a default.
    The response includes the applied rate and the `rate` guidance envelope."""
    rate_answer = None
    if interval is None:
        if _elicit is None:
            return {"error": "no interval given and AgentVision cannot ask "
                             "(elicit.py unavailable)",
                    "changed": False,
                    "fix": "pass interval explicitly, e.g. 0.25 for 4 shots/sec"}
        st = await _a(_http_get, "/status")
        current = None
        if isinstance(st, dict):
            cr = st.get("capture_rate") or {}
            current = cr.get("interval_seconds") or cr.get("interval")
        rate_answer = await _elicit.ask_capture_rate(
            ctx, fallback_interval=float(current or 1.0),
            current_interval=float(current) if current else None)
        if not rate_answer.chosen_by_user:
            # Nobody chose. Writing the fallback would look like a change the
            # user asked for; leaving it alone and saying why does not.
            return {"changed": False,
                    "interval_seconds": rate_answer.value,
                    "capture_rate_choice": rate_answer.as_dict(),
                    "next": "pass interval explicitly to set it anyway "
                            "(interval = 1 / shots_per_second)"}
        interval = float(rate_answer.value)
    out = await _a(_http_put, "/capture/interval", {"interval": float(interval)})
    return _ask_note(out, "capture_rate_choice", rate_answer)


@mcp.tool()
def av_daemon_status() -> dict:
    """Status of the system-wide input recorder daemon: running, pid,
    which profile its sink belongs to, and whether the per-profile
    capture_user_input opt-in is on. Use this to answer
    'why aren't keys/clicks showing up in the JSONL?'"""
    return _http_get("/daemon/status")


# ── HTTP helpers for PUT/DELETE — needed by parity tools above ──────────────

def _http_put(path: str, body: dict | None = None) -> Any:
    url = BRIDGE_BASE.rstrip("/") + path
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
    except Exception as e:
        return {"error": str(e), "url": url}


def _http_delete(path: str) -> Any:
    url = BRIDGE_BASE.rstrip("/") + path
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
    except Exception as e:
        return {"error": str(e), "url": url}


# ── Master orientation tool ──────────────────────────────────────────────────
# When I (Claude) sit down to debug, calling this ONCE gives the full
# picture: bridge+daemon health, active profile, capture status, latest
# frame seq, recent errors, and source-mirror availability. One round-trip
# instead of six.

@mcp.tool()
def av_selftest() -> dict:
    """Runtime SELF-CONFIRMATION on the TARGET machine — proves the runtime
    paths actually work here, not just in theory. Returns a JSON health report:
    screen capture is non-blank, window enumeration works, the OS input hooks
    FIRE (on Windows this spawns a SendInput probe and confirms the low-level
    hook callback observed it), and daemon status. On Windows this is the
    definitive 'confirmed working' proof at first run. Each check is
    ok:true|false|null (null = not applicable on this OS). For the full report
    including the emitter auto-injection round-trip, run `agentvision doctor`
    on the host."""
    return _http_get("/selftest")


@mcp.tool()
def av_digest() -> dict:
    """AI TRIAGE digest — call this FIRST. One compact, ranked JSON that tells you
    what to look at and in what order: an `attention` list (worked top-down, each
    item naming the drill-in tool), the latest frame's summary/recommended_next/
    tags/confidence, top recurring errors (deduped by fingerprint) with counts,
    brand-new error fingerprints this session, capture health + shots/sec, and
    image↔log alignment health, plus a `visual` block (freeze / blank / layout /
    on-screen-error events found for free at capture time). Token-light by design
    — read it to orient, then drill in with the named tools
    (av_errors_by_fingerprint, av_visual_changes, av_error_moment, …). Prefer
    this over calling 6 status tools.

    For the visual side, drill in with av_visual_changes (JSON, cheap) rather
    than av_latest_frame — escalate to pixels only when you actually need them."""
    return _http_get("/digest")


@mcp.tool()
def av_overview() -> dict:
    """One-shot orientation snapshot for the start of a debugging session.
    Bundles bridge status, daemon status, capture loop + rate envelope, the
    program status, active profile, the watched log sources (with each one's
    auto-detected format), the latest frame pointer, and recent error
    fingerprints — so you get oriented in ONE round-trip.

    THE TOKEN RULE: AgentVision already did the expensive observing for free on
    local CPU. Prefer av_visual_changes / av_frame_json over raw frames; escalate
    to pixels (av_frame_region) only when JSON is insufficient. av_start_here is
    the shortest version of this orientation and states the workflow explicitly.

    ACT ON `capture.rate.guidance`: at the start/continuation of a project, ASK
    THE USER their preferred screenshots-per-second and set it before capturing.
    Confirm `log_sources` shows the target's logs are detected/ready.

    THE FORCE: before starting capture on a NEW program, call av_preflight; if it
    reports gaps, call av_add_adapter for each missing debug-log type, then start.
    The `preflight` block here tells you whether that coverage check has passed
    for the active program; if `preflight.ok` is false, run av_preflight next."""
    out: dict = {}
    out["bridge"]    = _http_get("/status")
    out["daemon"]    = _http_get("/daemon/status")
    out["capture"]   = _http_get("/capture/status")
    out["program"]   = _http_get("/program/status")
    out["profile"]   = _http_get("/profiles/active")
    out["log_sources"] = _http_get("/log/sources")
    # Surface the one-line preflight hint front-and-center (also inside bridge).
    try:
        out["preflight"] = (out["bridge"] or {}).get("preflight") \
            if isinstance(out.get("bridge"), dict) else None
    except Exception:
        out["preflight"] = None
    try:
        # /latest/pointer, NOT /latest: fetching the whole frame here counted as
        # a full-frame read (inflating av_token_report's own savings figure) and
        # marked the frame examined, which is what allows retention to delete it.
        # Orientation must not spend either of those on the agent's behalf.
        latest = _http_get("/latest/pointer")
        out["latest_frame_seq"] = (latest or {}).get("sequence") \
            if isinstance(latest, dict) else None
    except Exception:
        out["latest_frame_seq"] = None
    out["new_errors_this_session"] = _http_get("/anomalies/new")
    out["next"] = ("When investigating a failure, call av_diagnose for ranked "
                   "root-cause hypotheses; av_timeline to see everything around a "
                   "bad frame; av_search to grep the logs.")
    return out


# ── Analysis / Search / Investigation tools (v5) ──────────────────────────────
# The high-value diagnostic surface. Each proxies a token-bounded bridge route
# and tells you WHEN to reach for it.

@mcp.tool()
def av_diagnose() -> dict:
    """CALL THIS FIRST when investigating a failure. THE FLAGSHIP root-cause tool.

    Deterministically correlates the recent window — deduped structured errors
    (by fingerprint, with counts + first/last seen), active anomalies, the latest
    state_delta + perf, program liveness, capture health, and the tail of
    WARN/ERROR log events — into a RANKED list of hypotheses. No LLM is involved;
    it is the correlation you then reason over.

    Returns {hypotheses:[{summary, confidence, evidence:[frame seqs / log
    timestamps / error fingerprints], probable_cause, recommended_next:[exact tool
    calls]}], health, top_signals, latest_state_delta, latest_perf,
    recent_warnings, counts}. Hypotheses are ranked severity × recency × recurrence
    — work them top-down and follow each one's recommended_next.

    PAIR IT WITH THE VISUAL SIDE: av_visual_events catches hangs, blank screens
    and on-screen error text that leave NO log record, and av_error_moment(seq)
    bundles any single failure end-to-end. Neither costs vision tokens."""
    return _http_get("/diagnose")


@mcp.tool()
def av_timeline(from_ms: float | None = None, to_ms: float | None = None,
                limit: int = 200) -> dict:
    """The 'what happened here' view — use it to see EVERYTHING around a bad frame
    or moment. A unified, ts-sorted, token-bounded merge of frames (seq + summary
    + error/anomaly), normalized log events from ALL sources (input daemon
    included), and auto-detected failure bookmarks. Each row is compact
    {ts_ms, kind, source, line}; kind ∈ {frame, log, bookmark}.

      from_ms/to_ms  epoch-ms window. DEFAULT (both omitted): the most recent
                     `limit` rows by count. Pass a tight window around a frame's
                     shutter_ms to see exactly what surrounded it.
      limit          max rows (default 200, cap 1000; keeps most recent)."""
    params = {"limit": limit}
    if from_ms is not None:
        params["from_ms"] = from_ms
    if to_ms is not None:
        params["to_ms"] = to_ms
    return _http_get("/timeline", params)


@mcp.tool()
def av_search(q: str, regex: bool = False, level: str = "", category: str = "",
              source: str = "", trace_id: str = "",
              from_ms: float | None = None, to_ms: float | None = None,
              limit: int = 100) -> dict:
    """The AI's grep across the normalized event stream + frame summaries. Use it
    to find where a symbol/message/error appears without pulling whole logs.

      q          substring (default) or regex (set regex=True) — case-insensitive.
      level      minimum canonical level (e.g. 'WARN' → WARN/ERROR/FATAL only)
      category   exact category match (error/event/metric/…)
      source     substring match on the event source
      trace_id   restrict to one logical-action span
      from_ms/to_ms  epoch-ms time window (omit for the recent stream)
      limit      max matches (default 100, cap 500).

    Returns compact matches, each with ts + which source/frame it came from."""
    params = {"q": q, "regex": "1" if regex else "0", "level": level or None,
              "category": category or None, "source": source or None,
              "trace_id": trace_id or None, "limit": limit}
    if from_ms is not None:
        params["from_ms"] = from_ms
    if to_ms is not None:
        params["to_ms"] = to_ms
    return _http_get("/search", params)


@mcp.tool()
def av_wait_for(condition: str = "", regex: str = "", level: str = "",
                category: str = "", source: str = "", timeout: float = 30.0,
                poll_interval: float = 1.0) -> dict:
    """Block (server-side, bounded) until a condition appears — call it AFTER
    triggering an action to catch the result (reproduce → wait → observe). Polls
    internally and ALWAYS returns by the cap; it never hangs.

      condition  'log' (default), 'error_fingerprint', 'anomaly', or 'frame'.
      regex/level/category/source  the log match (condition='log'). regex is
                 case-insensitive over message+raw; level is a minimum level.
      timeout    seconds to wait (default 30, HARD CAP 120).
      poll_interval  internal poll cadence (default 1s).

    'error_fingerprint' matches any failure fingerprint not seen at call time;
    'anomaly' matches a newly-detected anomaly; 'frame' matches the next captured
    frame. Returns {matched, condition, waited_ms, matched_event|matched_frame}."""
    body = {"condition": condition or None, "regex": regex or None,
            "level": level or None, "category": category or None,
            "source": source or None, "timeout": timeout,
            "poll_interval": poll_interval}
    return _http_post("/wait_for", {k: v for k, v in body.items() if v is not None})


@mcp.tool()
def av_diff(a: int, b: int) -> dict:
    """What changed between two FRAMES a and b (a<b). Returns the net state_delta
    aggregated across the intervening frames, new/resolved errors (by
    fingerprint), anomaly change, perf delta (cpu/rss/threads), and the
    frame-metadata delta. Use it to pin the frame where things started going
    wrong. Compact. For a diff between two MOMENTS (not frames), use av_state_diff."""
    return _http_get("/diff", {"a": int(a), "b": int(b)})


@mcp.tool()
def av_state_diff(a_ms: float, b_ms: float) -> dict:
    """State-field diff between two MOMENTS in time, using the nearest 'wide' state
    records. Returns added/removed/changed leaf keys (dotted paths) between the two
    instants — e.g. what player.hp / connection.state / queue.depth did between
    a_ms and b_ms. Bounded. For a frame-to-frame diff use av_diff."""
    return _http_get("/state_diff", {"a_ms": a_ms, "b_ms": b_ms})


@mcp.tool()
def av_metrics(window: int = 50) -> dict:
    """Perf/resource trend + error/capture rate over the last `window` frames
    (default 50, cap 2000). Returns cpu/rss/threads as latest/min/max/avg, the
    error-rate over the window, the current capture rate, and blank-frame counts.
    Use it to spot a memory climb, a thread leak, a CPU spike, or a rising error
    rate at a glance. Small JSON."""
    return _http_get("/metrics", {"window": int(window)})


@mcp.tool()
async def av_capabilities(ctx: Context = None) -> dict:
    """The AI's 'what can I do here'. What AgentVision can do RIGHT NOW: platform,
    capture backend, log-adapter + source-reader counts, active profile + language,
    capture/daemon status, and the catalog of analysis tools grouped by purpose
    (start / cheap_visual_path / orient / diagnose / investigate / frames / logs /
    capture / source). Token-bounded — counts and a curated tool list, never the
    full adapter dump.

    ALSO reports `your_client` — what the MCP client YOU are running in supports,
    and what AgentVision does instead where it does not. Two things vary by
    client and change how you should behave: whether AgentVision can put a
    question to the user itself (elicitation), and whether it can push state to
    you without being asked. Where it cannot ask, YOU must ask in prose.

    Read `token_rule` and the `cheap_visual_path` group: those are the tools that
    let you observe the program without spending vision tokens. av_start_here is
    the shorter, workflow-oriented version of this."""
    out = await _a(_http_get, "/capabilities")
    if isinstance(out, dict) and _elicit is not None:
        try:
            out["your_client"] = _elicit.client_report(ctx)
            out["your_client"]["mcp_sdk"] = MCP_SDK_FLAVOR
            # Counted from the live registry, never written down: a hardcoded
            # number here would drift the moment a resource is added, and this
            # response is exactly where an agent would trust it.
            n_fixed = len(await mcp.list_resources())
            n_tmpl = len(await mcp.list_resource_templates())
            out["your_client"]["resources"] = (
                f"{n_fixed} fixed + {n_tmpl} templated URIs under "
                f"agentvision:// — read agentvision://catalog instead of "
                f"calling av_bridge_catalog() when you want the option set out "
                f"of the transcript")
            # A push channel that fails silently looks exactly like one with
            # nothing to say. Publish its counters so the difference is visible.
            out["your_client"]["push_channel"] = dict(_push_state)
        except Exception:
            pass
    return out


@mcp.tool()
def av_test_adapter(line: str) -> dict:
    """Probe log-format coverage: route ONE raw log line through the adapter
    detector and see {adapter, confidence, top_scores, parsed event, is_fallback}.
    Use it to check whether a format is specifically understood before wiring a
    profile — is_fallback=true means no adapter parses it, so consider
    av_add_adapter. Pairs with av_preflight/av_list_adapters."""
    return _http_get("/adapter/test", {"line": line})


@mcp.tool()
def av_list_adapters(family: str = "", q: str = "", limit: int = 50,
                     offset: int = 0) -> dict:
    """Paginated, filterable list of the log adapters (hundreds) — NEVER the full
    raw dump. Use it to discover what formats AgentVision already parses.

      family   matches the adapter's family module OR its language (e.g. 'kernel',
               'security', 'network', 'java', 'go')
      q        substring on the adapter name
      limit    page size (default 50, cap 200); offset for paging.

    Returns {total, matched, families:{family:count}, adapters:[{name, language,
    family}]}."""
    return _http_get("/adapters", {"family": family or None, "q": q or None,
                                   "limit": limit, "offset": offset})


@mcp.tool()
def av_install_project(project_root: str, profile_name: str = "",
                       language: str = "",
                       install_python_hook: bool = True,
                       force: bool = False) -> dict:
    """UNIVERSAL auto-bridge: install AgentVision's OUTPUT side into a project of
    ANY language, with zero code changes. This is the "other side" of the
    bridge — AgentVision already READS logs; this makes the target program WRITE
    them into a self-contained `agentvision/` folder inside the project.

    On call it: (1) detects the language (or uses `language`), (2) scaffolds
    `agentvision/` with the sink files (actions.jsonl, log.txt), live state.json,
    a manifest, and a per-language OUTPUT EMITTER under `agentvision/emitters/`,
    and (3) wires zero-effort logging:
      • Python  — project-root sitecustomize.py (true autoload, no env needed)
      • Node.js — av_emit.js auto-loaded via NODE_OPTIONS=--require
      • Ruby    — av_emit.rb auto-loaded via RUBYOPT=-r
      • Java    — logback JSON appender config drop-in
      • .NET    — Serilog JSON file-sink config drop-in
      • Go/Rust/C++/shell/other — the `agentvision run -- <cmd>` stdout/stderr
        tee (normalized via the log adapters) is the zero-effort default; an
        optional structured snippet is also dropped.

    GATED, and the gate returns HTTP 200: until the bridge is planned and sealed
    for this program (av_bridge_catalog -> av_bridge_commit), this REFUSES and
    returns {"installed": false, "error": "BRIDGE_NOT_BUILT"} rather than
    installing. Check `installed`, not the status code. `force=true` installs the
    default set anyway — this parameter previously did not exist in the tool at
    all, so an agent hitting the gate had no way through it and only a docstring
    that read as unconditional.

    Returns the install report incl. detected `language` and `emitter` (kind,
    files, how_to_load). The universal front door to actually launch the bridged
    program is `agentvision run -- <cmd>`. Follow with av_install_verify."""
    return _http_post("/install", {
        "project_root":        project_root,
        "profile_name":        profile_name,
        "language":            language,
        "install_python_hook": install_python_hook,
        "force":               bool(force),
    })


@mcp.tool()
def av_install_verify(project_root: str, timeout: float = 6.0) -> dict:
    """Verify the OUTPUT side of the bridge is live, for ANY language. Reads the
    scaffolded agentvision/manifest.json to learn the language + emitter, then:
      • python/node/ruby — spawns a tiny probe with the emitter's auto-load env
        and confirms an av.<lang>.* event landed in agentvision/actions.jsonl
        (mode="spawn").
      • java/.net/go/rust/… — does a STATIC check (emitter files present + sink
        writable), since those only emit when the real program runs (mode="static";
        launch via `agentvision run` for live events).

    Returns: {verified, mode, language, events_seen, last_event, sink, stderr}.
    verified=True means the program can now emit diagnostic data to AgentVision."""
    return _http_post("/install/verify", {
        "project_root": project_root,
        "timeout":      timeout,
    })


# ── Investigation wrap-up / OCR / error→source / standing watches (v5 batch B) ─

@mcp.tool()
def av_session_report(from_ms: float | None = None,
                      to_ms: float | None = None) -> dict:
    """WRAP UP or HAND OFF an investigation — call this when you are done digging
    and want one shareable diagnostic report. It COMPOSES the existing internals
    (no recompute): digest health, av_diagnose hypotheses + top signals, a trimmed
    timeline of the KEY moments (errors/warns/anomalies/failure-bookmarks), top
    errors deduped by fingerprint, new-this-session fingerprints, the frames worth
    looking at (seqs), and a capture/alignment coverage summary.

    Returns compact JSON PLUS a `markdown` field — a readable ~4-8KB report string
    you (or the user) can save as the write-up. Pass from_ms/to_ms to scope the
    timeline + frames-of-interest to a window; omit for the whole session."""
    params: dict = {}
    if from_ms is not None:
        params["from_ms"] = from_ms
    if to_ms is not None:
        params["to_ms"] = to_ms
    return _http_get("/session_report", params or None)


@mcp.tool()
def av_ocr_frame(seq: int) -> dict:
    """Read the on-screen TEXT of captured frame N cheaply — OCR it into JSON
    instead of describing the image with vision tokens. Use it to pull UI labels,
    error dialogs, and on-screen stack traces the image contains.

    Returns {available, engine, text (bounded ~8KB), lines:[{text, bbox, conf}],
    word_count}. OCR is an OPTIONAL capability: if tesseract/pytesseract isn't
    installed it returns {available:false, reason, install_hint} gracefully —
    fall back to reading the image directly in that case.

    ACCURACY — READ THIS BEFORE QUOTING A VALUE. This text is a machine
    transcription, not the pixels. It is reliable for prose and UI labels and
    UNRELIABLE character by character on dense alphanumerics. MEASURED on a real
    capture: `src=0x5D80000` came back as `0x5080000` and `0x800790790` as
    `0x800790796` — a D read as 0, a 0 as 6 — and one whole line was noise.
    `conf` does NOT help: every one of those lines reported conf 1.0, including
    the noise. So never state a hex address, flag, id or count from OCR alone.
    Corroborate it against the time-aligned log (av_log_range / av_log_entities),
    or crop the region and look at the pixels (av_frame_region). An empty `text`
    means the frame was not readable — it does NOT mean the screen was blank or
    error-free.

    RELATED: av_frame_json(seq) returns this OCR text ALONGSIDE the frame's
    perceptual hash, change score, changed region and time-aligned logs in one
    call — prefer it when you want to understand a moment rather than only read
    its text. For a whole run, av_visual_changes(include_ocr=True)."""
    return _http_get(f"/frame/{int(seq)}/ocr")


@mcp.tool()
def av_read_screen() -> dict:
    """Read what is on screen RIGHT NOW as text — OCR the latest captured frame
    into JSON (see av_ocr_frame) instead of spending vision tokens describing it.
    The quick 'what does the screen say' tool. Degrades gracefully to
    {available:false, reason, install_hint} when tesseract/pytesseract is absent.

    DO NOT reach for av_latest_frame (a full image) just to read text — this is
    the cheap answer. If you also want the change score, changed region and the
    logs that line up with 'now', use av_frame_json on the latest sequence
    (av_start_here reports it).

    ACCURACY: same caveat as av_ocr_frame, and it matters more here because this
    is the tool you reach for casually. The text is a transcription, correct for
    prose and unreliable character by character on hex, ids, flags and counts —
    measured, with the OCR engine reporting full confidence while wrong. Empty
    text means UNREADABLE, not blank and not error-free. Confirm any exact value
    against the log before you act on it."""
    return _http_get("/read_screen")


@mcp.tool()
def av_source_at_error(fingerprint: str = "", frames: int = 5,
                       context: int = 4) -> dict:
    """Jump from an ERROR straight to the CODE. Takes a structured error's frames
    [{file,line}] and pulls the mirrored source AROUND each frame (a few lines of
    context each, >> marks the error line), so you see the failing code without
    hunting the filesystem.

      fingerprint  a specific error's fingerprint (from av_diagnose /
                   av_errors_by_fingerprint); omit to use the LATEST frame's error.
      frames       max stack frames to resolve (default 5, cap 10)
      context      lines of context above/below each frame (default 4).

    Returns {error:{type,message,fingerprint,probable_cause}, frame_seq,
    frames:[{file,line,func,found,resolved_path,code_context:[...]}]}. found=false
    means that file isn't in the source mirror / not on disk."""
    return _http_get("/source_at_error", {"fingerprint": fingerprint or None,
                                          "frames": frames, "context": context})


@mcp.tool()
def av_baseline(ts_ms: float | None = None, profile: str = "") -> dict:
    """Stamp a 'since' marker for the active program — call it BEFORE you
    reproduce something, so watches and 'what changed' queries can bound to
    everything after this instant. Omit ts_ms to mark NOW. The marker is persisted
    (survives a bridge restart). Pair with av_watch + av_watches(since_baseline=1).
    Returns {ok, profile, baseline_ms, baseline_iso}."""
    body: dict = {}
    if ts_ms is not None:
        body["ts_ms"] = ts_ms
    if profile:
        body["profile"] = profile
    return _http_post("/baseline", body)


@mcp.tool()
def av_watch(name: str, kind: str = "", regex: str = "", level: str = "",
             category: str = "", source: str = "", fingerprint: str = "",
             anomaly_type: str = "") -> dict:
    """Set a TRIPWIRE before reproducing — register a standing condition under a
    name; the bridge accumulates matches as events flow, and you check them later
    with av_watches. Perfect for 'catch what appears when I do X'.

      name          required label for this watch
      kind          'log' | 'error_fingerprint' | 'anomaly' (inferred if omitted)
      regex/level/category/source   the log match (kind='log'); regex is
                    case-insensitive, level is a minimum canonical level
      fingerprint   a specific failure fingerprint to catch (kind='error_fingerprint')
      anomaly_type  an anomaly type to catch, e.g. 'screen_stuck' (kind='anomaly';
                    any anomaly if omitted).

    Idempotent by name. Watches are IN-MEMORY and evaluated on query. Returns
    {ok, watch, watch_count}."""
    body = {"name": name, "kind": kind or None, "regex": regex or None,
            "level": level or None, "category": category or None,
            "source": source or None, "fingerprint": fingerprint or None,
            "anomaly_type": anomaly_type or None}
    return _http_post("/watch", {k: v for k, v in body.items() if v is not None})


@mcp.tool()
def av_watches(since_baseline: bool = False, clear: bool = False) -> dict:
    """Check the standing watches you set with av_watch — lists each watch with
    its NEW hits (bounded per watch). Call it AFTER reproducing to see what tripped.

      since_baseline  scan from the active profile's av_baseline marker instead of
                      each watch's own registration time (great with av_baseline).
      clear           remove all watches after reporting (returns them one last
                      time with hits).

    Returns {watch_count, baseline_ms, watches:[{name, kind, hit_count, hits[...]}],
    cleared}."""
    return _http_get("/watches", {"since_baseline": "1" if since_baseline else None,
                                  "clear": "1" if clear else None})


# ══════════════════════════════════════════════════════════════════════════════
# THE CHEAP PATH — token-bounded JSON instead of pixels
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool(annotations=_ro("Start here"))
async def av_start_here(ctx: Context = None) -> dict:
    """READ THIS FIRST, before any other AgentVision tool.

    Tells you: what AgentVision is watching right now, WHETHER THIS PROGRAM'S
    BRIDGE IS BUILT, whether capture/daemon are alive, whether OCR is available,
    and — most importantly — `DO_THIS_NEXT`, a single unambiguous next call.

    IF YOU READ NOTHING ELSE: call this, then do what `DO_THIS_NEXT` says.

    The one thing that surprises new agents: AgentVision does NOT choose what
    logging to build into a program — YOU do, once, on first connection. Until
    you commit a plan, av_capture_start() is REFUSED (HTTP 200 with
    "error": "BRIDGE_NOT_BUILT", so check the body, not the status code).
    `recommended_workflow` in the response already accounts for this: on an
    unbuilt program it walks you through av_bridge_status → av_bridge_catalog →
    av_bridge_commit; on a built one it does not, because setup never repeats.

    WHEN: at the start of any session where you might need to observe a running
    program — even if you are not sure AgentVision is set up yet (this tells you).

    ALSO returns `your_client`: whether AgentVision can ask the user a question
    itself in the client you are running in, and whether it can push state to
    you unprompted. Where it cannot ask, asking is YOUR job — check that block
    before you assume a default rate or a consent you never obtained.

    DO NOT: start taking your own screenshots or grepping logs before calling
    this. Capture is very likely already running and already parsed."""
    out = await _a(_http_get, "/start_here")
    if isinstance(out, dict) and _elicit is not None:
        try:
            out["your_client"] = _elicit.client_report(ctx)
        except Exception:
            pass
    return out


@mcp.tool(annotations=_ro("Visual changes"))
def av_visual_changes(from_ms: float | None = None, to_ms: float | None = None,
                      min_change: float | None = None, limit: int = 50,
                      include_ocr: bool = False) -> dict:
    """THE TOKEN SAVER — review a whole capture run WITHOUT looking at an image.

    Returns only the moments the screen actually changed, each as one compact row
    {seq, ts_ms, change_score, changed_bbox, dhash, one_line_summary}. Runs of
    visually identical frames collapse into {no_change, seq_range, frames}. At 10
    shots/sec roughly 99% of consecutive frames are identical, so this turns ten
    minutes of capture into a few hundred tokens.

    WHEN: any time you want to know "what did the screen do?" — after reproducing
    a bug, after a click, while waiting for a long operation.

    DO NOT use av_get_frame / av_latest_frame in a loop to survey a run; that
    spends hundreds-to-thousands of visual tokens per frame for information this
    call gives you for almost nothing.

      from_ms/to_ms  optional epoch-ms window (omit for the whole session)
      min_change     0-1; fraction of the screen that must change to count
      limit          max rows, most recent kept (default 50, cap 500)
      include_ocr    add a short OCR snippet per changed moment (needs tesseract)

    NEXT: av_frame_json(seq) to describe one moment, av_frame_region(seq) for the
    changed pixels, av_error_moment(seq) for a full failure bundle."""
    return _http_get("/visual_changes", {
        "from_ms": from_ms, "to_ms": to_ms,
        "min_change": min_change, "limit": limit,
        "include_ocr": "1" if include_ocr else None})


@mcp.tool(annotations=_ro("Frame as JSON"))
def av_frame_json(seq: int, thumbnail: bool = False, thumb_width: int = 64,
                  ocr: bool = True, logs: int = 6) -> dict:
    """IMAGES AS JSON — one frame fully described with NO full image in it.

    Returns {seq, ts_ms, size, dhash, change_score, changed_bbox, structural
    (mean_luma/contrast/is_blank/text_rows/dominant_colors), one_line_summary,
    ocr_text, aligned_logs, error?, visual_events?} plus `token_math` comparing
    its own cost to the full frame's.

    WHEN: you want to know what was on screen at a specific moment. This is the
    DEFAULT way to inspect a frame — try it before you ever open the PNG.

    DO NOT reach for av_get_frame (full image) until this has proved
    insufficient. Escalate in this order: av_frame_json → thumbnail=True →
    av_frame_region (changed pixels) → av_get_frame (full image).

      thumbnail    include a tiny base64 thumb (OFF by default — even 64px costs
                   real visual tokens, whereas this descriptor costs almost none)
      thumb_width  thumb width in px (16-256)
      ocr          include on-screen text (silently skipped without tesseract)
      logs         how many time-aligned log lines to include (0-50)"""
    return _http_get(f"/frame/{int(seq)}/json", {
        "thumbnail": "1" if thumbnail else "0",
        "thumb_width": thumb_width,
        "ocr": "1" if ocr else "0",
        "logs": logs})


@mcp.tool(annotations=_ro("Frame region"))
def av_frame_region(seq: int, bbox: str = "changed", max_dim: int = 900,
                     ocr: bool = True) -> dict:
    """Serve ONLY a crop of a frame — by default exactly the region that CHANGED.

    Returns the crop base64-encoded in `image_b64` (decode to view), its own OCR
    text, and `token_math` showing the tokens saved versus the full frame.

    WHEN: av_frame_json told you something changed and you need to actually SEE
    it — a dialog, an error banner, a rendering glitch.

    DO NOT send yourself a full 4K screenshot when the interesting change was a
    400x120 error box. That is the single most common way to waste vision tokens.

      bbox     'changed' (default — the diff region), 'full', or 'x,y,w,h'
      max_dim  long-edge cap for the served crop (default 900)
      ocr      OCR the crop itself (needs tesseract)"""
    return _http_get(f"/frame/{int(seq)}/region", {
        "bbox": bbox, "max_dim": max_dim, "ocr": "1" if ocr else "0"})


@mcp.tool(annotations=_ro("Error moment bundle"))
def av_error_moment(fingerprint: str = "", seq: int | None = None,
                    window_secs: float = 6.0, include_image: bool = False,
                    max_dim: int = 900) -> dict:
    """THE ONE-CALL FAILURE BUNDLE — "show me the error moment", pre-correlated.

    In a single response: the structured error (type / message / probable_cause /
    stack frames), the frame captured at that exact shutter, the changed region
    and its OCR, the on-screen text, the time-aligned log window merged from ALL
    of the program's log sources, the state_delta, and the source code around
    each stack frame.

    WHEN: you know (or suspect) a specific failure and want everything about it.

    DO NOT hand-assemble this from av_get_frame + av_ocr_frame +
    av_log_normalized + av_state_diff + av_source_at_error — that is 5-6 calls
    for what this returns in one, and it forces you to do the timestamp↔frame
    correlation the bridge already did.

      fingerprint    a specific failure fingerprint (av_errors_by_fingerprint)
      seq            a specific frame instead
      (neither)      the latest error
      window_secs    half-width of the log window (default 6 s)
      include_image  include the changed region's pixels (OFF by default to keep
                     the bundle cheap; av_frame_region gets them on demand)"""
    return _http_get("/error_moment", {
        "fingerprint": fingerprint or None,
        "seq": seq if seq is not None else None,
        "window_secs": window_secs,
        "include_image": "1" if include_image else None,
        "max_dim": max_dim})


@mcp.tool(annotations=_ro("Visual events"))
def av_visual_events(type: str = "", limit: int = 50) -> dict:
    """Auto-detected VISUAL events — the screen-side equivalent of log bookmarks,
    found for free at capture time so you can jump straight to what matters.

    Detectors: `screen_frozen` (nothing changed for N seconds — a hang),
    `blank_screen` (sudden black/blank frame), `layout_change` (a large fraction
    of the screen repainted — new view or dialog), `on_screen_error` (OCR found
    error/exception/traceback text; needs tesseract). Consecutive repeats of one
    type collapse into ONE event with seq_range + frames, so a 30-second freeze
    is a single bookmark rather than 300.

    WHEN: "did anything visually go wrong?" — especially for hangs, which leave
    no log line at all and are invisible to log-only analysis.

      type   filter to one detector name
      limit  max events, most recent kept

    NEXT: av_error_moment(seq=<seq>) or av_frame_region(seq=<seq>)."""
    return _http_get("/visual_events", {"type": type or None, "limit": limit})


@mcp.tool(annotations=_ro("Frames awaiting your eyes"))
def av_frames_awaiting(limit: int = 25) -> dict:
    """Frames AgentVision is HOLDING FOR YOU — captured, flagged, not yet looked at.

    Screenshots are only worth taking if you hear about them, so retention will
    not delete a flagged frame until it has been examined. This is the list of
    what it is still holding, most urgent first, with the reason each was flagged
    and how long before its hold expires and the pixels are reclaimed.

    WHEN: after AgentVision pushes "N frames are waiting on your eyes", or any
    time you want to know what visual evidence exists that you have not used.

    Work them CHEAPEST-FIRST — any of these releases the frame:
      av_frame_json(seq)                 JSON descriptor, no pixels at all
      av_frame_json(seq, thumbnail=True) a tiny thumb
      av_frame_region(seq)               only the pixels that changed
      av_get_frame(seq)                  the full image — last resort
      av_examine_ack(seqs=[...])         release without looking

    Note: av_visual_changes releases ORDINARY frames (a JSON row is a legitimate
    look) but deliberately does NOT release failure-aligned ones — a one-line
    summary is not an inspection of a crash.

    NEXT: av_retention() for the disk budget and whether anything was ever lost
    unexamined."""
    return _http_get("/frames_awaiting", {"limit": limit})


@mcp.tool(annotations={"title": "Release examined frames", "readOnlyHint": False})
def av_examine_ack(seqs: list[int] | str = "", all: bool = False) -> dict:
    """Release frames you are DONE with, so their full-resolution pixels can be
    reclaimed.

    Use this when the JSON already answered your question and you do not need the
    image. It is how you keep the disk bounded honestly instead of making the
    recorder guess whether you were finished.

      seqs  the frames to release, e.g. [4412, 4413]
      all   True releases everything currently awaiting

    Descriptors and thumbnails are kept permanently either way — acking only
    gives up the full-resolution pixels.

    NEXT: av_frames_awaiting() to confirm the queue is clear."""
    body: dict = {}
    if all:
        body["all"] = 1
    if seqs:
        body["seqs"] = seqs
    return _http_post("/examine_ack", body)


@mcp.tool(annotations=_ro("Bridge status (built?)"))
def av_bridge_status() -> dict:
    """IS THIS PROGRAM'S BRIDGE BUILT? Check this before anything else on a new program.

    A program AgentVision has never seen starts PROVISIONAL: capture and emitter
    installation are REFUSED until you review the catalog and commit a plan.

    This is deliberate. AgentVision owns 658 log adapters, 9 binary readers, 90
    tools and a per-language emitter library — but it cannot know which of those a
    program needs, because that depends on what the code IS and DOES. Left to
    itself it scaffolds the same fixed set for a web server and a GPU emulator and
    reports success either way, so a wrong bridge is indistinguishable from a
    right one. You decide; AgentVision supplies.

    THIS HAPPENS ONCE PER PROGRAM. After you commit, the logging is built in and
    every later connection proceeds immediately — you will not be asked again.

    NEXT: if PROVISIONAL → av_bridge_catalog()."""
    return _http_get("/bridge/status")


@mcp.tool(annotations=_ro("Bridge catalog (review first)"))
async def av_bridge_catalog(ctx: Context = None) -> dict:
    """EVERY option AgentVision can build into this program. Nothing pre-selected.

    Read this before building a bridge on a new program. It lists:
      emitters_available   what logging can be ADDED, what each captures, its cost
      adapters             658 parsers grouped by family (drill in with
                           av_list_adapters) — these READ logs that already exist
      source_readers       binary formats (utmp, pcap, netflow, …)
      mcp_tool_groups      all 90 tools in 19 groups, each entry carrying what
                           the tool returns, what it NEEDS, its token cost and
                           a relevance verdict for THIS program
      capture_settings     frame rate — ASK THE USER, do not assume (or call
                           av_capture_start() with no interval and AgentVision
                           asks them for you)
      you_must_decide      the actual decisions this program needs from you

    Key distinction the catalog spells out: ADAPTERS parse logs that exist,
    EMITTERS create logs that do not. A program with no logging needs an emitter
    first — pinning an adapter to a file nothing writes gives you nothing.

    Returns a `catalog_token`. av_bridge_commit REJECTS a plan without a matching
    token, so reviewing the options is a mechanical precondition, not a suggestion.

    IF THE PROFILE HAS NO project_root, the code scan opens nothing and
    `code_evidence` is empty — an absence of INPUT, not an absence of signals,
    and committing on it is the blind guess this gate exists to prevent. In that
    case this tool asks the user where the code lives and returns the answer
    under `project_root_needed`. It does NOT apply it: pointing a profile at a
    folder is a change to the user's configuration, so the response names the
    exact call to make. Re-fetch this catalog afterwards — the token you have
    describes a scan of nothing.

    NEXT: av_bridge_commit(plan={...}) using the token."""
    cat = await _a(_http_get, "/bridge/catalog")
    if not isinstance(cat, dict):
        return cat
    ev = cat.get("code_evidence")
    rootless = (isinstance(ev, dict)
                and "project_root" in str(ev.get("error") or ""))
    if rootless and _elicit is not None:
        try:
            ans = await _elicit.ask_project_root(ctx)
            block = ans.as_dict()
            block["apply_with"] = (
                f'av_create_profile(name="<program>", '
                f'project_root="{ans.value}")' if ans.chosen_by_user
                else 'av_create_profile(name="<program>", project_root="...")')
            block["then"] = ("call av_bridge_catalog() again — this catalog's "
                             "token describes a scan that opened NO files")
            cat["project_root_needed"] = block
        except Exception:
            pass
    return cat


@mcp.tool(annotations={"title": "Commit bridge plan (builds it)",
                       "readOnlyHint": False})
async def av_bridge_commit(plan: dict, replan: bool = False,
                           ctx: Context = None) -> dict:
    """COMMIT your plan — this is what actually builds the bridge.

    AgentVision builds exactly what you name here and nothing else.

      plan = {
        "catalog_token": "<from av_bridge_catalog>",   # required, proves review
        "emitters":  ["lifecycle", "uncaught_exceptions", ...],  # required; [] ok
        "adapters":  {"<log label>": "jsonl" | "auto" | "<adapter name>"},
                     # THIS IS HOW A LOG GETS READ. A log the program already
                     # writes appears in catalog.existing_logs_found with
                     # declared:false and is NOT read until you pin its label
                     # here. Keys must come from catalog.adapter_pin_labels;
                     # values must be "auto" or a real adapter name.
        "capture":   {"interval_seconds": 1.0},        # after ASKING the user
        "visual_capture": true|false,                  # false for headless
        "rationale": "why this set fits THIS program", # required, for audit
        "why": {"<emitter id>": "the code signal that justifies it"},
                                     # REQUIRED whenever emitters is non-empty,
                                     # >=15 chars each, one entry per emitter
        "tools": {                   # required — the tool half of the review
          "primary":      ["av_diagnose", ...],   # what you'd reach for here
          "not_relevant": {"av_ui_tree": "headless, no accessibility tree"}
        }
      }
      replan  re-decide a program whose bridge is already built

    REJECTED if: the token is missing or stale, `emitters` is absent, there is no
    rationale, `emitters` is [] without saying why in the rationale, `why` lacks
    an entry for any selected emitter, or `tools` is missing. Each of those would
    seal a bridge nobody actually chose — the exact failure this gate prevents.

    `tools` records RELEVANCE, not installation: every tool stays callable on
    every program. Listing 25+ in `primary` is rejected as a copy of the catalog.
    Read mcp_tool_groups[*].tools in the catalog first — each entry carries what
    the tool returns, what it `needs`, and a per-program relevance verdict.

    `emitters: []` is a legitimate answer for a program that already logs well —
    say so in the rationale, AND pin that existing log in `adapters`. Selecting no
    emitters and pinning nothing is refused (error BRIDGE_WOULD_READ_NOTHING):
    it seals a bridge with no log sources, so every log tool answers emptily
    forever while the status says BUILT. If frames-only really is the decision,
    set visual_capture=true and say "visual only" in the rationale.

    A `replan` may also RE-PIN an already-declared source — that is how you switch
    a log to an adapter you have since written for it.

    THE ONE EMITTER THAT NEEDS A HUMAN. `user_input` runs a SYSTEM-WIDE recorder
    of the user's keystrokes and mouse clicks — not scoped to the window being
    debugged. Selecting it makes this tool ask the person at the keyboard, and
    an explicit "no" REMOVES it from the plan; the rest of the plan is built
    unchanged. Where the client cannot show a prompt, your selection stands (so
    nothing silently overrides your decision) and `input_recording_consent`
    records that nobody consented. Tell the user what that field says.

    On success the selected emitters are scaffolded into the project, the plan is
    persisted with your rationale, and the gate never fires for this program again.

    NEXT: av_capture_start()."""
    consent = None
    emitters = list((plan or {}).get("emitters") or [])
    if "user_input" in emitters and _elicit is not None:
        try:
            # fallback=True: the agent already chose this emitter. In a client
            # that cannot ask, refusing it would override a stated decision on
            # the strength of a question nobody heard.
            consent = await _elicit.ask_input_recording_consent(ctx,
                                                                fallback=True)
            if consent.how == _elicit.HOW_ASKED and not consent.value:
                plan = dict(plan)
                plan["emitters"] = [e for e in emitters if e != "user_input"]
                why = dict(plan.get("why") or {})
                why.pop("user_input", None)
                plan["why"] = why
        except Exception:
            consent = None
    out = await _a(_http_post, "/bridge/commit",
                   {"plan": plan, "replan": bool(replan)})
    if consent is not None and isinstance(out, dict):
        block = consent.as_dict()
        if consent.how == _elicit.HOW_ASKED and not consent.value:
            block["effect"] = ("user_input was REMOVED from the committed plan "
                               "— the user declined. Everything else was built "
                               "as you specified.")
        elif consent.how != _elicit.HOW_ASKED:
            block["effect"] = ("user_input was built AS YOU SELECTED IT, but "
                               "nobody consented to it. Say so, and offer to "
                               "replan without it.")
        out["input_recording_consent"] = block
    return out


@mcp.tool(annotations=_ro("Raw log, verbatim"))
def av_log_raw(session_id: str = "default", all: bool = False,
               from_offset: int = -1, cap_bytes: int = 0,
               peek: bool = False) -> dict:
    """THE PROGRAM'S OWN OUTPUT, verbatim. No levels, no ranking, no filtering.

    Use this when you want what the program actually said rather than
    AgentVision's reading of it — and ALWAYS reach for it when a summary looks
    suspiciously clean. On this project av_diagnose reported "health 100, no
    strong failure signals, program looks healthy" while 180 GPU present
    failures sat in these exact bytes, and the real bug was only ever visible in
    their raw `target=`/`tex0=` fields, which the summary had flattened to prose.

    The ONLY reduction is lossless: consecutive byte-identical lines collapse to
    {line, repeat:N} — 49% of a real boot log was one line repeated 21,982
    times, and that collapse is what makes reading raw output affordable at all.
    No distinct line is ever dropped, re-levelled or reordered. Records
    AgentVision itself wrote into the log are excluded (its own watchdog had
    written 902 of them), and each source reports `stale` / `last_write_age_s`
    so a log the program is NOT actually writing to cannot be mistaken for live
    output.

      all          ignore offsets, return the whole retained tail
      from_offset  start at this byte offset (use the value a push reported)
      peek         do not advance this session's read position
      cap_bytes    byte budget; oldest lines are dropped first and reported

    NEXT: av_log_entities(never='target') to query these lines as records."""
    params: dict = {"session_id": session_id}
    if all:
        params["all"] = 1
    if from_offset >= 0:
        params["from_offset"] = from_offset
    if cap_bytes:
        params["cap_bytes"] = cap_bytes
    if peek:
        params["peek"] = 1
    return _http_get("/log/raw", params)


@mcp.tool(annotations=_ro("Where the program writes"))
def av_log_where() -> dict:
    """WHERE IS THE PROGRAM ACTUALLY WRITING? Asked of the OS, not of config.

    A configured log path is a guess. This inspects the file descriptors the
    process really holds open and reconciles them against the profile's declared
    sources.

    CALL THIS THE MOMENT A LOG LOOKS TOO QUIET, or when a push warns that a
    source is stale. It catches the failure a log reader cannot detect about
    itself: reading a file the program stopped writing to. On this project an
    entire analysis ("180 GPU present failures") was performed against a log
    whose last write was 23 hours old, because the emulator's own sink wrote to a
    different directory — and nothing complained, because `tail -f` on the wrong
    file happily shows stale bytes forever.

    Returns:
      missing_from_config  the process writes here and nothing is reading it
      not_written_by_proc  configured, but this pid does not hold it open
      stale                open, but nothing written recently
      output_destination   where stdout/stderr actually go — a terminal, a pipe,
                           or /dev/null (DISCARDED). No log path can tell you this.

    NEXT: fix the profile's log_sources, or relaunch the program so its output
    lands where the profile expects."""
    return _http_get("/log/where")


@mcp.tool(annotations=_ro("Log role index"))
def av_log_entities(never: str = "", address: str = "", key: str = "",
                    limit: int = 25) -> dict:
    """Query the raw log as RECORDS: which address played which role, how often.

    Log lines are structured data — `target=0x1240000 tex0=0x5D80000 ok=False`
    is a record, not a sentence. This counts, for every address the log mentions,
    which field names it appeared under.

    THE HIGH-VALUE QUERY IS `never`. The hardest bug on this project was solved
    by noticing that one address appeared as `tex0` and `src` but NEVER as
    `target` — meaning the buffer being presented was one nothing ever rendered
    into, so the lookup could only ever miss. `av_log_entities(never='target')`
    asks that directly and ranked it first out of 44,937 raw lines:

        0x5d80000  seen 185x  roles={'tex0': 4, 'src': 181}

    WHEN: any "why does this handle/address/id misbehave" question; after a
    summary says everything is fine but you do not believe it; to find a value
    used in every position except the one you expect.

      never    list addresses that never appear under this key  <- start here
      address  roles for one address (any hex spelling; 0-padding is normalized)
      key      restrict to these field names (csv)
      limit    max rows

    This returns COUNTS, not conclusions. Whether a pattern is a bug is yours to
    decide — which is the point: nothing was filtered out on your behalf."""
    params: dict = {"limit": limit}
    if never:
        params["never"] = never
    if address:
        params["address"] = address
    if key:
        params["key"] = key
    return _http_get("/log/entities", params)


@mcp.tool(annotations=_ro("Retention + disk budget"))
def av_retention() -> dict:
    """The examine-before-delete contract: disk budget, what is held, what was lost.

    AgentVision bounds its own disk use by BYTES (5 GB by default), not by a
    clock. The old rule deleted every frame older than 60 seconds — shorter than
    one reasoning turn, so a frame could be announced to you and then deleted
    before you could fetch it. Now a frame flagged for examination is protected
    until you examine it (or its hold expires), and only already-served frames
    are evicted, cheapest-value first.

    Reports: budget used vs. 5 GB, how many frames need eyes, how many are still
    awaiting, what was evicted/archived, and `integrity.dropped_unexamined` —
    frames the policy wanted you to see that expired first. If that is above
    zero, the capture rate is too high for the budget, or you are not examining
    fast enough; it is reported rather than hidden because silently losing
    evidence is the one failure this system exists to prevent.

    The `mode` dial decides who gets pushed at you: `errors` (default) flags only
    failure-aligned frames; `changes` adds every visible change; `all` flags every
    frame so you get video-like continuity. Pair `all` with a slower capture
    interval — you do not need 24 fps to perceive motion.

    WHEN: "is AgentVision filling my disk?", "did I miss any frames?", or before
    changing the capture rate.

    NEXT: av_frames_awaiting() · av_examine_ack(seqs=[...])."""
    return _http_get("/retention")


@mcp.tool(annotations=_ro("Token report"))
def av_token_report() -> dict:
    """PROOF, not a claim: what the cheap path saved this session.

    Reports the free capture-side work (frames captured, frames visually
    unchanged, perceptual-hash dedup ratio, per-frame analysis cost against the
    capture budget) versus what you actually paid for, plus a MEASURED
    comparison on a real frame: full-image base64 vs av_frame_json vs a
    changed-region crop. The estimation method is stated plainly in the payload —
    these are estimates from the documented image-token rule, not tokenizer
    output.

    WHEN: to sanity-check that you are using the cheap path, or when the user
    asks whether AgentVision is actually saving anything."""
    return _http_get("/token_report")


@mcp.tool(annotations=_ro("Incidents (flight recorder)"))
def av_incidents(id: str = "", limit: int = 25) -> dict:
    """THE FLIGHT RECORDER — the failure moments AgentVision already froze for you.

    PURPOSE: the instant a failure signature appears (structured error, screen
    freeze, blank screen, on-screen error text) the recorder FREEZES the preceding
    window plus a short tail as an "incident", and those frames become the LAST
    thing eviction will ever touch. Returns the incident list with {id, kind,
    trigger_seq, trigger_ms, detail, window_ms, frame_count} plus recorder stats;
    with `id` it returns that incident's frame rows.

    WHEN TO USE: the moment you learn something failed. The run-up to the failure
    is ALREADY on disk — you do not need to reproduce the bug to see what led to
    it. Check this before asking the user to re-run anything.

    NOT FOR: live state (use av_start_here / av_frame_json) or log-only failures
    with no visual component (av_diagnose covers those).

    LIMITATIONS: only the last N incidents are kept (default 25) and only the
    configured window (default 60 s before, 5 s after) is frozen. Frames are NOT
    deleted on a timer — disk is bounded by bytes and a frame flagged for your
    eyes is held until examined (av_retention) — but an unpinned frame CAN be
    evicted once the budget is tight and it has served its purpose. If the
    recorder is disabled (AGENTVISION_RECORDER=0) the list is always empty.

      id     a specific incident id to expand (from a previous listing)
      limit  max incidents to list, most recent kept"""
    return _http_get("/incidents", {"id": id or None, "limit": limit})


@mcp.tool(annotations=_ro("Replay"))
def av_replay(from_ms: float | None = None, to_ms: float | None = None,
              incident: str = "", step: int = 1, limit: int = 40,
              logs: int = 3) -> dict:
    """TIME-TRAVEL — step through what already happened, without re-running it.

    PURPOSE: returns a bounded, ordered walk of the moments that CHANGED, each
    paired with the log lines that line up with it: [{step, seq, ts_ms,
    change_score, summary, logs, error}]. It is the recorded past replayed as
    JSON — no image bytes.

    WHEN TO USE: to understand a sequence ("what happened between the click and
    the crash?"), or to walk a frozen incident end to end.

    NOT FOR: a single moment (av_frame_json is cheaper) or a full failure bundle
    (av_error_moment already correlates everything for one instant).

    LIMITATIONS: only frames still retained (see av_retention) or pinned by an
    incident can be replayed; unchanged frames are skipped by design.

      from_ms/to_ms  epoch-ms window; omit for the whole retained session
      incident       replay one frozen incident by id (overrides the window)
      step           take every Nth changed moment (thin a long run)
      limit          max steps returned (most recent kept)
      logs           log lines attached per step (0-20)"""
    return _http_get("/replay", {
        "from_ms": from_ms, "to_ms": to_ms, "incident": incident or None,
        "step": step, "limit": limit, "logs": logs})


@mcp.tool(annotations=_ro("UI / accessibility tree"))
def av_ui_tree(app: str = "", flat: bool = True, max_nodes: int = 0,
               compare_to_seq: int | None = None) -> dict:
    """THE CHEAPEST AND MOST PRECISE WAY TO READ A SCREEN — the window's
    ACCESSIBILITY TREE as JSON.

    PURPOSE: returns the target window's UI elements with EXACT text, roles and
    pixel bboxes — no OCR guessing, no vision tokens. Published browser-automation
    comparisons put text-vs-vision at 10-20x cheaper, and pruned trees have been
    measured cutting input tokens ~98% while holding task accuracy. Returns
    {available, backend, element_count, prune_stats, elements|tree,
    cost{est_tokens, cheaper_than_screenshot, verdict}}.

    WHEN TO USE: to read labels, button states, field values, dialog text, or to
    get the exact coordinates of a control. Try it BEFORE av_frame_json when the
    question is "what does the UI say / where is that element?".

    WHEN NOT TO USE — this is the important part, and `available:false` here is a
    legitimate answer, not a failure:
      * Custom-drawn UIs expose NO tree: games, emulators, canvas/WebGL apps,
        Dear ImGui, most SDL/OpenGL windows. Expect available:false or a
        near-empty tree (`likely_custom_drawn:true`), then use av_frame_json /
        av_ocr_frame / av_frame_region.
      * Questions about icon COLOUR, spatial LAYOUT correctness, progress
        indicators, or rendering corruption are NOT answerable from a tree at
        all — those need pixels (av_frame_region).
      * Unlabelled controls are missing from the tree even when visible.

    LIMITATIONS: macOS requires the Accessibility permission (the GUI's
    Permissions tab and `agentvision doctor` grant it) and the optional pyobjc
    packages; Windows prefers comtypes UIA and falls back to a coarser win32
    enumeration; Linux needs pyatspi plus a running at-spi2. Traversal is
    deadline-bounded (2.5 s default) so a wedged app cannot stall the bridge, and
    the tree is pruned + node-capped — check `truncated` and `cost.verdict`,
    because on a deep list-heavy window the tree CAN cost more than a screenshot.

      app             window/app name (defaults to the profile's capture_app)
      flat            flat {d, role, text, bbox} rows (cheaper) vs a nested tree
      max_nodes       override the node cap (0 = leave the default)
      compare_to_seq  compare the cost against that frame's screenshot"""
    return _http_get("/ui_tree", {
        "app": app or None, "flat": "1" if flat else "0",
        "max_nodes": max_nodes or None,
        "compare_to_seq": compare_to_seq if compare_to_seq is not None else None})


@mcp.tool(annotations=_ro("UI semantic diff"))
def av_ui_diff(app: str = "", wait_ms: float = 1000.0) -> dict:
    """SEMANTIC screen diff — what changed in the UI, in WORDS not rectangles.

    PURPOSE: snapshots the UI tree, waits `wait_ms`, snapshots again, and reports
    {appeared, disappeared, changed, summary} — e.g. "changed: AXStaticText
    'Ready' -> 'Error: timeout'", "disappeared: AXButton 'Start'". Element
    matching is position-quantised so a 1-px reflow is not reported as a change.

    WHEN TO USE: to find out what a click/action actually did to the UI, or to
    watch for a specific control appearing or a label changing. Much more
    meaningful than a pixel diff because it names the element.

    NOT FOR: apps with no accessibility tree (returns available:false — use
    av_visual_changes for the pixel-level answer), or for a change that already
    happened in the past (this samples live; av_visual_changes is historical).

    LIMITATIONS: it BLOCKS for wait_ms, so keep the wait short. Same per-OS
    availability caveats as av_ui_tree.

      app      window/app name (defaults to the profile's capture_app)
      wait_ms  gap between the two snapshots (default 1000, max 10000)"""
    return _http_get("/ui_diff", {"app": app or None, "wait_ms": wait_ms})


@mcp.tool(annotations=_ro("Ambient (push) view"))
def av_ambient(session_id: str = "default", event: str = "manual",
               force: bool = False) -> dict:
    """PUSH MODE — what AgentVision would tell you RIGHT NOW, unprompted.

    PURPOSE: this is the pull-mode view of the push channel. It returns
    {tier, inject, text, bytes, est_tokens, signals_used, suppressed, reason} —
    the exact few lines a Push Mode hook would inject into the conversation.
    Tiers are silent / heartbeat / notice / alert, each byte-capped.

    WHEN TO USE: to see whether AgentVision is currently trying to tell you
    something, without reading a digest; or to check what Push Mode is doing if
    the user has it enabled.

    NOT FOR: diagnosis (av_diagnose), reviewing a run (av_visual_changes), or a
    specific failure (av_error_moment). This is a one-glance "anything urgent?".

    LIMITATIONS: SILENT BY DEFAULT and DELTA-ONLY — it deliberately returns
    inject=false when nothing has changed since this session was last told, so an
    empty answer is the healthy answer, not a failure. Repeats are suppressed per
    session_id unless they escalate in severity; pass force=True to see what it
    WOULD say ignoring suppression and rate limits (byte caps still apply).

      session_id  delta tracking is per session
      event       the hook event name to simulate (e.g. 'SessionStart')
      force       bypass rate-limiting + already-surfaced suppression"""
    return _http_get("/ambient", {"session_id": session_id, "event": event,
                                  "force": "1" if force else None})


# ── MCP PROMPTS ───────────────────────────────────────────────────────────────
# Prompts are discoverable, user-invocable workflows. They encode the order of
# operations so the agent does not have to rediscover it each session.

@mcp.prompt(title="Diagnose the running program")
def diagnose_running_program(symptom: str = "") -> str:
    """Full triage workflow for a program that is misbehaving, in the cheap order."""
    sym = f"\n\nReported symptom: {symptom}" if symptom else ""
    return f"""Diagnose the program AgentVision is watching. Follow this order and
do NOT take your own screenshots or grep raw logs — AgentVision has already
captured and parsed everything.{sym}

1. av_start_here — confirm what is being watched and that capture is alive.
2. av_diagnose — read the ranked root-cause hypotheses and their evidence.
3. av_visual_changes — see what the screen actually did. Identical frames are
   already collapsed; do not open frames one by one.
4. av_visual_events — check for hangs (screen_frozen), blank screens, and
   on-screen error text. A hang often leaves NO log line.
5. av_error_moment — for the specific failure you are now focused on. This one
   call already bundles the error, the frame, the changed pixels, the on-screen
   text, the merged log window, the state delta, and the source code.
6. Only if the JSON is genuinely insufficient: av_frame_region(seq) for the
   changed pixels, and av_get_frame(seq) for a full image as a last resort.

Then state the root cause, the evidence you used, and the fix. Spend your
remaining effort on the CODE, not on observing."""


@mcp.prompt(title="Review a capture run cheaply")
def review_capture_run(from_ms: str = "", to_ms: str = "") -> str:
    """Survey everything that happened on screen without opening images."""
    win = ""
    if from_ms or to_ms:
        win = f"\nRestrict to the window from_ms={from_ms or 'start'} to_ms={to_ms or 'now'}."
    return f"""Review what happened on screen during the AgentVision capture run.{win}

Use av_visual_changes first — it returns only the moments the screen changed and
collapses runs of identical frames, so a long run costs a few hundred tokens.
For any moment that looks interesting, use av_frame_json(seq) to read it as JSON
(including on-screen text). Escalate to av_frame_region(seq) only when you need
to actually look at the pixels that changed, and to a full image only if a crop
is insufficient.

Summarise the run as a short timeline of what changed and when, and flag anything
that looks like a hang, a blank screen, or an on-screen error."""


@mcp.prompt(title="Before the first capture")
def before_first_capture(project_root: str = "") -> str:
    """The FORCE: verify log coverage before capturing a new program."""
    root = f" The project root is {project_root}." if project_root else ""
    return f"""AgentVision is about to observe a NEW program.{root}

Run av_preflight FIRST. It checks that AgentVision can specifically parse this
program's logs rather than falling back to the generic normalizer — which is what
makes the frames-plus-logs correlation actually work.

If preflight reports gaps, call av_add_adapter for each missing debug-log format
(give it a real sample line), then re-run av_preflight until it is clean.

Only then start capture with av_capture_start — call it with NO interval and
AgentVision will ask the user how many screenshots per second they want, and
tell you in `capture_rate_choice` whether it managed to."""


# ── MCP RESOURCES ─────────────────────────────────────────────────────────────
# Resources let the agent pull context WITHOUT spending a tool call, and let the
# client attach them to the conversation automatically.
#
# FIXED URIs below are the "what is happening now" views. TEMPLATED URIs (further
# down) address ONE artifact by its identifier — a specific frame, a specific
# incident. That distinction matters for cost: a tool call carries its whole
# result into the transcript whether or not the agent needed all of it, while a
# resource is fetched by the client only when something actually wants it. The
# 185 KB first-connection catalog is the clearest case, and it is why
# `agentvision://catalog` exists alongside av_bridge_catalog().

@mcp.resource("agentvision://start_here", name="AgentVision: start here",
              mime_type="application/json", annotations=_res_ann(1.0, False),
              description="Orientation: what is being watched, state, workflow, "
                          "and the token rule. Read this before anything else.")
def _res_start_here() -> str:
    return json.dumps(_http_get("/start_here"), indent=2)


@mcp.resource("agentvision://digest", name="AgentVision: triage digest",
              mime_type="application/json", annotations=_res_ann(0.9, False),
              description="Ranked triage digest — health score, attention list, "
                          "top errors, capture health, visual events.")
def _res_digest() -> str:
    return json.dumps(_http_get("/digest"), indent=2)


@mcp.resource("agentvision://capabilities", name="AgentVision: capabilities",
              mime_type="application/json", annotations=_res_ann(0.5),
              description="What AgentVision can do right now: platform, capture "
                          "backend, adapter counts, and the tool catalog.")
def _res_capabilities() -> str:
    return json.dumps(_http_get("/capabilities"), indent=2)


@mcp.resource("agentvision://frame/latest.json",
              name="AgentVision: latest frame as JSON",
              mime_type="application/json", annotations=_res_ann(0.8),
              description="The most recent frame described as JSON (perceptual "
                          "hash, change score, changed region, on-screen text) "
                          "with NO image bytes — the cheap way to see 'now'.")
def _res_latest_frame_json() -> str:
    # Pointer first: this resource returns the JSON DESCRIPTION of the frame, so
    # counting a full-frame read (and marking the frame examined) on the way to
    # finding its number would misreport what the caller actually consumed.
    latest = _http_get("/latest/pointer")
    if isinstance(latest, dict) and latest.get("sequence") is not None:
        return json.dumps(_http_get(f"/frame/{int(latest['sequence'])}/json"),
                          indent=2)
    return json.dumps(latest, indent=2)


@mcp.resource("agentvision://visual_changes",
              name="AgentVision: visual changes",
              mime_type="application/json", annotations=_res_ann(0.8),
              description="Only the moments the screen changed, with identical "
                          "runs collapsed. Review a whole run for a few hundred "
                          "tokens.")
def _res_visual_changes() -> str:
    return json.dumps(_http_get("/visual_changes", {"limit": 50}), indent=2)


@mcp.resource("agentvision://token_report",
              name="AgentVision: token report",
              mime_type="application/json", annotations=_res_ann(0.3, False),
              description="Measured accounting of what the cheap path saved this "
                          "session, with the estimation method stated.")
def _res_token_report() -> str:
    return json.dumps(_http_get("/token_report"), indent=2)


@mcp.resource("agentvision://incidents",
              name="AgentVision: frozen incidents",
              mime_type="application/json", annotations=_res_ann(0.95, False),
              description="Failure windows the flight recorder already froze — "
                          "the seconds BEFORE each failure, still on disk.")
def _res_incidents() -> str:
    return json.dumps(_http_get("/incidents"), indent=2)


@mcp.resource("agentvision://catalog",
              name="AgentVision: first-connection catalog",
              mime_type="application/json", annotations=_res_ann(0.7, False),
              description="Every emitter, adapter, reader and tool AgentVision "
                          "could build into this program, with the code evidence "
                          "scanned from it and a catalog_token. Same bytes as "
                          "av_bridge_catalog(); read it here to keep it out of "
                          "the transcript until something needs it.")
def _res_catalog() -> str:
    # Deliberately the SAME endpoint the tool uses, so the catalog_token this
    # returns is valid for av_bridge_commit. A parallel implementation here
    # would eventually drift, and a stale token is rejected with no clue why.
    return json.dumps(_http_get("/bridge/catalog"), indent=2)


# ── TEMPLATED RESOURCES — address ONE artifact ────────────────────────────────

@mcp.resource("agentvision://frame/{seq}.json",
              name="AgentVision: one frame as JSON",
              mime_type="application/json", annotations=_res_ann(0.6),
              description="Frame {seq} described as JSON — perceptual hash, "
                          "change score, changed region, on-screen text — with "
                          "NO image bytes. The cheap way to inspect a specific "
                          "moment av_visual_changes pointed you at.")
def _res_frame_json(seq: str) -> str:
    try:
        n = int(str(seq).strip())
    except (TypeError, ValueError):
        return json.dumps({"error": f"frame sequence must be an integer, "
                                    f"got {seq!r}"}, indent=2)
    return json.dumps(_http_get(f"/frame/{n}/json"), indent=2)


@mcp.resource("agentvision://frame/{seq}/region",
              name="AgentVision: the changed pixels of one frame",
              mime_type="application/json", annotations=_res_ann(0.5),
              description="Only the region of frame {seq} that CHANGED, "
                          "base64-encoded in `image_b64`, with the token math "
                          "for what that crop cost versus the full frame. "
                          "Escalate here when the JSON is not enough.")
def _res_frame_region(seq: str) -> str:
    try:
        n = int(str(seq).strip())
    except (TypeError, ValueError):
        return json.dumps({"error": f"frame sequence must be an integer, "
                                    f"got {seq!r}"}, indent=2)
    return json.dumps(_http_get(f"/frame/{n}/region"), indent=2)


@mcp.resource("agentvision://incident/{incident_id}",
              name="AgentVision: one frozen incident",
              mime_type="application/json", annotations=_res_ann(0.9, False),
              description="One failure window the flight recorder froze: the "
                          "trigger, the frames either side of it, and the exact "
                          "follow-up calls. Ids come from agentvision://incidents.")
def _res_incident(incident_id: str) -> str:
    ident = str(incident_id or "").strip()
    if not ident:
        return json.dumps({"error": "no incident id given",
                           "hint": "list them at agentvision://incidents"},
                          indent=2)
    return json.dumps(_http_get("/incidents", {"id": ident}), indent=2)


@mcp.resource("agentvision://log/raw{?from_offset}",
              name="AgentVision: the program's own output, verbatim",
              mime_type="application/json", annotations=_res_ann(0.85, False),
              description="What the program actually printed. No levels, no "
                          "ranking, no filtering; identical consecutive lines "
                          "collapse losslessly to {line, repeat:N}. Optional "
                          "from_offset starts at a byte offset. Reading this "
                          "resource PEEKS — it never advances a session's read "
                          "position, so it is safe to re-read.")
def _res_log_raw(from_offset: str = "") -> str:
    # peek=1 is not a detail. A resource is addressable and re-readable by
    # definition; if reading one consumed the log cursor, a client that
    # auto-attached it would silently eat the lines av_log_raw was about to
    # hand the agent, and the loss would look like a program that went quiet.
    params: dict = {"session_id": "resource", "peek": 1}
    off = str(from_offset or "").strip()
    if off:
        try:
            n = int(off)
        except ValueError:
            return json.dumps({"error": f"from_offset must be an integer byte "
                                        f"offset, got {from_offset!r}"}, indent=2)
        # A negative offset is refused, not quietly honoured. The bridge would
        # seek() past the start of the file, fail, and DROP that source from the
        # result — an empty log that looks exactly like a program gone quiet,
        # which is the confident silence this whole project exists to prevent.
        # av_log_raw guards this too (its `from_offset >= 0`); the resource must
        # not be the weaker door.
        if n < 0:
            return json.dumps({"error": f"from_offset must be a non-negative "
                                        f"byte offset, got {n}",
                               "hint": "omit from_offset to read the whole "
                                       "retained tail"}, indent=2)
        params["from_offset"] = n
    else:
        params["all"] = 1
    return json.dumps(_http_get("/log/raw", params), indent=2)


def main() -> None:
    """Entry point for stdio MCP transport (Claude Code default)."""
    mcp.run()


if __name__ == "__main__":
    main()
