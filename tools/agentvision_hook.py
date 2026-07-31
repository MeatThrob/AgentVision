#!/usr/bin/env python3
"""
AgentVision — Push Mode hook (single entry point for every hook event)
=====================================================================

Claude Code runs this on hook events. It asks the bridge one question — "is there
anything worth telling the agent right now?" — and prints a few lines only if the
answer is yes.

DESIGN CONSTRAINTS, in priority order. Every one of these is a hard rule, because
this code runs on the user's critical path on EVERY prompt:

  1. NEVER break the session. Any failure — bridge down, timeout, bad JSON,
     unreadable stdin, missing dependency — exits 0 with no output. A debugging
     aid must never be able to wedge the tool it is helping with.
  2. NEVER hang. Short socket timeout plus a wall-clock deadline; the bridge is
     on loopback, so anything slow is already wrong.
  3. SILENT unless there is news. The engine decides; this script just relays.
  4. NO stderr in normal operation. stderr on a non-zero exit is surfaced, so
     noise here would spam the user. Set AGENTVISION_HOOK_DEBUG=1 to trace.
  5. stdlib ONLY. This runs under whatever `python3` the user's PATH resolves —
     not necessarily AgentVision's venv. No requests, no flask, no Pillow.

WIRING (installed by `agentvision install-hooks`):
  SessionStart      matchers startup|resume|clear|compact|fork
  UserPromptSubmit  (no matcher)
  PostToolBatch     (no matcher — fires after parallel tools resolve)
  PostToolUse       matcher Edit|Write|MultiEdit|Bash   ← "did my change break it?"
  Stop              (no matcher; OFF unless explicitly enabled)

OUTPUT CONTRACT — verified against the Claude Code hooks reference:
  * Plain stdout on exit 0 is shown to Claude for UserPromptSubmit, PreToolUse and
    PostToolUse. We use plain stdout by default because it is the most broadly
    supported shape. Set AGENTVISION_HOOK_JSON=1 to instead emit the
    {"hookSpecificOutput": {"hookEventName": ..., "additionalContext": ...}} form,
    which the reference documents for UserPromptSubmit / PreToolUse / PostToolUse.
  * Stop is different: it takes {"decision": "block", "reason": ...}. We only ever
    emit that when the backstop is explicitly enabled AND the engine's loop guards
    all pass.
"""

from __future__ import annotations

import json
import os
import sys
import time

DEADLINE_S = float(os.environ.get("AGENTVISION_HOOK_DEADLINE_S", "2.5"))
HTTP_TIMEOUT_S = float(os.environ.get("AGENTVISION_HOOK_TIMEOUT_S", "1.2"))
BRIDGE = os.environ.get("AGENTVISION_BRIDGE_URL", "http://127.0.0.1:7771")
DEBUG = os.environ.get("AGENTVISION_HOOK_DEBUG", "0") not in ("0", "", "false")
USE_JSON_OUT = os.environ.get("AGENTVISION_HOOK_JSON", "0") not in ("0", "", "false")

# Events we act on at all. Anything else exits silently.
INJECT_EVENTS = {"SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolBatch"}
# Second line of defence behind the settings.json matcher.
POST_TOOL_ALLOW = {"Edit", "Write", "MultiEdit", "Bash", "NotebookEdit"}

_T0 = time.monotonic()


def _dbg(msg: str) -> None:
    if DEBUG:
        sys.stderr.write(f"[agentvision-hook] {msg}\n")


def _out_of_time() -> bool:
    return (time.monotonic() - _T0) > DEADLINE_S


def _state_dir() -> str:
    """Where the Push Mode enable flag lives. Next to this script's repo by
    default so the GUI and the CLI agree without needing the bridge."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.environ.get("AGENTVISION_STATE_DIR", os.path.join(os.path.dirname(here), "log"))


def push_mode_enabled() -> tuple[bool, str]:
    """Master switch, checked before any network call.

    Precedence: env override, then the on-disk flag, then default-ON (installing
    the hook IS the opt-in, so a present hook with no flag file should work)."""
    env = os.environ.get("AGENTVISION_PUSH_MODE")
    if env is not None:
        return (env not in ("0", "", "false")), "env AGENTVISION_PUSH_MODE"
    path = os.path.join(_state_dir(), "agentvision_push_mode.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return bool(d.get("enabled", True)), f"flag file {path}"
    except FileNotFoundError:
        return True, "default (no flag file)"
    except Exception as exc:
        _dbg(f"flag file unreadable ({exc}) — defaulting ON")
        return True, "default (flag unreadable)"


def _get(path: str, params: dict) -> dict | None:
    """One short GET against the bridge. Returns None on ANY problem."""
    import urllib.parse
    import urllib.request
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{BRIDGE.rstrip('/')}{path}?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:
        _dbg(f"bridge unreachable or slow: {type(exc).__name__}: {exc}")
        return None


#: Events where PLAIN stdout is NOT added to the agent's context and the
#: structured form is required. Observed empirically over a long session: every
#: SessionStart and UserPromptSubmit injection arrived, while not one of the
#: hundreds of PostToolUse firings did — the bridge logged the /ambient call and
#: marked the signal "already surfaced", so the message was consumed AND then
#: permanently suppressed. That double fault silently disabled the single most
#: valuable push moment ("did my change just break it?") for a whole session.
#: For these events we always emit hookSpecificOutput.additionalContext.
_NEEDS_JSON_OUT = {"PostToolUse", "PostToolBatch", "PreToolUse"}


def _emit_context(event: str, text: str) -> None:
    """Print the injection in the shape that actually reaches the agent.

    SessionStart takes plain stdout. PostToolUse/PostToolBatch need the
    structured additionalContext form or the text is dropped. AGENTVISION_HOOK_JSON
    forces the structured form everywhere except SessionStart.
    """
    if not text:
        return
    want_json = (event in _NEEDS_JSON_OUT
                 or (USE_JSON_OUT and event != "SessionStart"))
    if want_json:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {"hookEventName": event,
                                   "additionalContext": text}}))
    else:
        sys.stdout.write(text)
    sys.stdout.write("\n")


def main() -> int:
    # ── stdin ────────────────────────────────────────────────────────────────
    try:
        raw = sys.stdin.read()
    except Exception as exc:
        _dbg(f"could not read stdin: {exc}")
        return 0
    if not raw.strip():
        _dbg("empty stdin — nothing to do")
        return 0
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
    except Exception as exc:
        _dbg(f"bad stdin JSON: {exc}")
        return 0

    event = str(payload.get("hook_event_name") or "").strip()
    session_id = str(payload.get("session_id") or "default")
    _dbg(f"event={event} session={session_id[:12]}")

    enabled, why = push_mode_enabled()
    if not enabled:
        _dbg(f"push mode disabled ({why})")
        return 0

    # ── Stop backstop (separate contract, off unless enabled) ────────────────
    if event == "Stop":
        # `stop_hook_active` is NOT documented in the hooks reference; pass it
        # through if the harness happens to provide it, as defence in depth.
        active = payload.get("stop_hook_active")
        d = _get("/ambient", {"session_id": session_id, "event": "Stop",
                              "stop_check": "1",
                              "stop_hook_active": "1" if active else None})
        if not d or not d.get("block"):
            _dbg(f"stop: not blocking ({(d or {}).get('reason')})")
            return 0
        sys.stdout.write(json.dumps({"decision": "block",
                                     "reason": d.get("reason") or
                                     "AgentVision detected an unresolved failure."}))
        sys.stdout.write("\n")
        _dbg(f"stop: BLOCKING ({d.get('kind')})")
        return 0

    if event not in INJECT_EVENTS:
        _dbg(f"event {event!r} not wired — silent")
        return 0

    # PostToolUse is only interesting for tools that CHANGE something.
    if event == "PostToolUse":
        tool = str(payload.get("tool_name") or "")
        if tool and tool not in POST_TOOL_ALLOW:
            _dbg(f"PostToolUse for {tool!r} — not a mutating tool, silent")
            return 0

    if _out_of_time():
        _dbg("deadline exceeded before the bridge call — silent")
        return 0

    d = _get("/ambient", {"session_id": session_id, "event": event})
    if not d:
        return 0                      # bridge down => silent, session unaffected
    if not d.get("inject"):
        _dbg(f"nothing to say ({d.get('reason')})")
        return 0

    _emit_context(event, str(d.get("text") or ""))
    _dbg(f"injected tier={d.get('tier')} bytes={d.get('bytes')}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:            # absolute last resort
        _dbg(f"unhandled: {type(exc).__name__}: {exc}")
        sys.exit(0)
