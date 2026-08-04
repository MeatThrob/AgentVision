#!/usr/bin/env python3
"""The portable push channel must be quiet, honest, and free when unused.

WHY IT EXISTS. Push Mode is a Claude Code hook. In Cursor, VS Code, or any
other MCP client, AgentVision's best feature — telling the agent something
broke without being asked — is simply silent. MCP's own answer is a
resource-updated notification, which needs no client-specific hook.

WHY IT IS DANGEROUS. It is the first thing in this server that runs on its own,
without an agent asking for it, so every failure mode is one nobody is watching:

  * CHATTER. The first version fingerprinted the rendered injection text. That
    text embeds a live clock ("LAST WRITE 144s AGO"), so the hash changed on
    every poll: measured 2 announcements in 5 seconds for one unchanged log.
    An ambient channel that chatters is worse than none — it trains the agent
    to ignore it. Identity now comes from `content_fp`, built from the signals
    and the actual log bytes.
  * REPLAY. The fingerprint used to live in a local variable, so a client that
    dropped its stream and re-listened was told the same thing again.
  * DOUBLE-REPORTING. The Claude Code hook and this channel read the same
    engine. An agent told twice cannot tell it was one event.
  * CONSUMPTION. If the poller consumed the ambient delta it would eat the
    lines the hook was about to deliver, and the loss would look like a program
    that went quiet.
  * COST WHEN IDLE. A poller that runs with nobody listening is pure waste.

Each of those has a check below. `_http_get` is replaced with a scripted stub,
so none of this needs a live bridge or a clock to cooperate.
"""
from __future__ import annotations

import asyncio
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
    print("portable push — quiet, honest, and free when nobody is listening")

    # ── content_fp: the fix for the chatter bug, checked at the source ───────
    from api import ambient as A

    def _state(age_s: int, lines: list[str]) -> dict:
        return {"raw_log": {"sources": [
            {"label": "text", "last_write_age_s": age_s, "stale": age_s > 60,
             "lines": [{"line": ln} for ln in lines]}]}}

    same_lines = ["ERROR widget offline", "FATAL unrecoverable"]
    fp_a = A.content_fp("raw", [], _state(144, same_lines))
    fp_b = A.content_fp("raw", [], _state(146, same_lines))
    check("content_fp ignores the ticking clock in the rendered text",
          fp_a == fp_b, f"{fp_a} != {fp_b}")
    fp_c = A.content_fp("raw", [], _state(146, same_lines + ["ERROR a new one"]))
    check("...but changes when the program actually says something new",
          fp_c != fp_b, f"{fp_c} == {fp_b}")
    fp_d = A.content_fp("alert", [{"fp": "abc123"}], {})
    fp_e = A.content_fp("alert", [{"fp": "def456"}], {})
    check("...and distinguishes different signals", fp_d != fp_e)
    check("...and is order-independent for the same signal set",
          A.content_fp("alert", [{"fp": "a"}, {"fp": "b"}], {})
          == A.content_fp("alert", [{"fp": "b"}, {"fp": "a"}], {}))

    # ── other_channel_ms_ago: the seam that stops double-reporting ───────────
    mem = A.SessionMemory()
    check("with one session, there is no OTHER channel",
          mem.other_channel_ms_ago("hook") is None)
    mem.record_injection("hook", "alert", 100)
    check("a session still does not count itself",
          mem.other_channel_ms_ago("hook") is None,
          str(mem.other_channel_ms_ago("hook")))
    ago = mem.other_channel_ms_ago("subscriber")
    check("but another session's injection IS visible, with an age",
          ago is not None and ago < 5000, str(ago))

    # ── The poller ───────────────────────────────────────────────────────────
    try:
        import api.claude_mcp as M
    except SystemExit:
        print("SKIP — the `mcp` SDK is not installed. Nothing here was "
              "verified. Fix: pip install 'mcp>=2.0,<3'")
        return 1 if FAILED else 0
    except Exception as exc:
        print(f"SKIP — could not import the MCP server "
              f"({type(exc).__name__}: {exc}). Nothing here was verified.")
        return 1 if FAILED else 0

    if M._SUBSCRIPTIONS is None:
        print("  --   poller checks skipped: this SDK has no subscription bus")
        print()
        print(f"{'FAILED: ' + str(len(FAILED)) if FAILED else 'all checks passed'}")
        return 1 if FAILED else 0

    bus = M._SUBSCRIPTIONS
    reply: dict = {}
    seen: list = []

    def _stub(path, params=None):
        seen.append((path, dict(params or {})))
        return dict(reply)

    def _reset():
        seen.clear()
        M._push_state.update({"polls": 0, "published": 0, "errors": 0,
                              "quiet_for_other_channel": 0, "last_error": "",
                              "last_fingerprint": None})

    async def run_for(seconds: float) -> list:
        got: list = []
        off = bus.subscribe(lambda ev: got.append(ev))
        await asyncio.sleep(seconds)
        off()
        await asyncio.sleep(0.05)
        return got

    real_get, M._http_get = M._http_get, _stub
    real_interval, M._PUSH_INTERVAL_S = M._PUSH_INTERVAL_S, 0.05
    real_quiet, M._PUSH_QUIET_MS = M._PUSH_QUIET_MS, 120000.0
    try:
        # 1. Idle: nobody listening, nothing happens at all.
        _reset()
        asyncio.run(asyncio.sleep(0.3))
        check("with no subscriber the poller does not run",
              M._push_state["polls"] == 0 and not M._push_state["running"],
              f"polls={M._push_state['polls']}")
        check("...and it reports zero listeners",
              M._push_state["listeners"] == 0, str(M._push_state["listeners"]))

        # 2. A real change is announced exactly once.
        _reset()
        reply.clear()
        reply.update({"inject": True, "tier": "alert", "content_fp": "FP-1",
                      "text": "something broke", "other_channel_ms_ago": None})
        got = asyncio.run(run_for(0.35))
        check("a subscriber starts the poller", M._push_state["polls"] >= 2,
              str(M._push_state["polls"]))
        check("a real signal is announced", len(got) == 1,
              f"{len(got)} events: {[getattr(e, 'uri', e) for e in got]}")
        check("...as a resource-updated event for the digest",
              got and str(getattr(got[0], "uri", "")) == "agentvision://digest",
              str([getattr(e, "uri", e) for e in got]))
        check("...and it is announced ONCE, not once per poll",
              M._push_state["published"] == 1, str(M._push_state["published"]))

        # 3. THE CHATTER BUG: unchanged content, across a fresh subscription.
        got = asyncio.run(run_for(0.35))
        check("a re-listening client is NOT re-told the same thing",
              got == [] and M._push_state["published"] == 1,
              f"{len(got)} events, published={M._push_state['published']}")

        # 4. New content is announced again.
        reply["content_fp"] = "FP-2"
        got = asyncio.run(run_for(0.35))
        check("genuinely new content IS announced", len(got) == 1,
              str(len(got)))

        # 5. The poller must never consume the ambient delta.
        forced = [p for (path, p) in seen if path == "/ambient"]
        check("every poll is FORCED, so it consumes nothing",
              forced and all(p.get("force") == "1" for p in forced),
              str(forced[:2]))
        check("...and uses its own session id, never 'default'",
              all(p.get("session_id") not in (None, "", "default")
                  for p in forced), str(forced[:2]))

        # THE RAW TIER POINTS AT THE RAW LOG. `raw` means AgentVision had
        # nothing to say — the program printed new lines. The digest may not
        # have changed at all (new INFO lines move no error counts), and
        # "digest updated" when it did not update is a false assertion
        # delivered as a push. The notification must name the resource that
        # actually changed.
        _reset()
        reply.clear()
        reply.update({"inject": True, "tier": "raw", "content_fp": "FP-RAW",
                      "text": "new program output", "other_channel_ms_ago": None})
        got = asyncio.run(run_for(0.35))
        check("the raw tier announces the RAW LOG, not the digest",
              got and str(getattr(got[0], "uri", "")) == "agentvision://log/raw",
              str([getattr(e, "uri", e) for e in got]))
        check("...and does not ALSO claim the digest changed",
              all(str(getattr(e, "uri", "")) != "agentvision://digest"
                  for e in got), str([getattr(e, "uri", e) for e in got]))

        # An incident rides along regardless of tier — frozen failure windows
        # are always worth a pointer.
        _reset()
        reply.update({"content_fp": "FP-RAW-INC", "incident_ids": ["inc-1"]})
        got = asyncio.run(run_for(0.35))
        check("an incident is announced alongside the raw log",
              {str(getattr(e, "uri", "")) for e in got}
              == {"agentvision://log/raw", "agentvision://incidents"},
              str([getattr(e, "uri", e) for e in got]))
        reply.pop("incident_ids", None)

        # 6. Silence and heartbeats must not wake anyone.
        for tier, inject in (("silent", False), ("heartbeat", True)):
            _reset()
            reply.update({"inject": inject, "tier": tier,
                          "content_fp": f"FP-{tier}"})
            got = asyncio.run(run_for(0.3))
            check(f"a '{tier}' tier wakes nobody", got == [], str(got))

        # 7. Stand down while the other channel is delivering.
        _reset()
        reply.update({"inject": True, "tier": "alert", "content_fp": "FP-3",
                      "other_channel_ms_ago": 500.0})
        got = asyncio.run(run_for(0.3))
        check("it stays quiet while the Claude Code hook is active",
              got == [] and M._push_state["published"] == 0, str(got))
        check("...and COUNTS the times it stood down, rather than vanishing",
              M._push_state["quiet_for_other_channel"] >= 1,
              str(M._push_state["quiet_for_other_channel"]))

        _reset()
        reply["other_channel_ms_ago"] = 999999.0
        reply["content_fp"] = "FP-4"
        got = asyncio.run(run_for(0.3))
        check("a LONG-idle other channel does not silence it forever",
              len(got) == 1, str(len(got)))

        # 8. An older bridge with no content_fp: quiet, and says why.
        _reset()
        reply.update({"inject": True, "tier": "alert",
                      "other_channel_ms_ago": None})
        reply.pop("content_fp", None)
        got = asyncio.run(run_for(0.3))
        check("without content_fp it stays quiet rather than guessing",
              got == [], str(got))
        check("...and says so instead of failing silently",
              "content_fp" in M._push_state["last_error"],
              M._push_state["last_error"])

        # 9. A broken bridge is counted, and the poller survives it.
        _reset()
        reply.clear()
        reply.update({"error": "URL error: Connection refused"})
        got = asyncio.run(run_for(0.3))
        check("a bridge error is counted, not swallowed",
              M._push_state["errors"] >= 1, str(M._push_state["errors"]))
        check("...with the real reason kept",
              "Connection refused" in M._push_state["last_error"],
              M._push_state["last_error"])
        check("...and the poller keeps polling instead of dying",
              M._push_state["polls"] >= 2, str(M._push_state["polls"]))

        def _boom(path, params=None):
            raise RuntimeError("stub exploded")

        M._http_get = _boom
        _reset()
        got = asyncio.run(run_for(0.3))
        check("an exception in the poll loop does not kill the loop",
              M._push_state["errors"] >= 2, str(M._push_state["errors"]))
        M._http_get = _stub

        # 10. The last subscriber leaving stops the work.
        _reset()
        reply.clear()
        reply.update({"inject": False, "tier": "silent", "content_fp": "x"})
        asyncio.run(run_for(0.2))
        polls_at_stop = M._push_state["polls"]
        asyncio.run(asyncio.sleep(0.3))
        check("the last unsubscribe stops the poller",
              M._push_state["polls"] == polls_at_stop
              and not M._push_state["running"],
              f"{polls_at_stop} -> {M._push_state['polls']}")
    finally:
        M._http_get = real_get
        M._PUSH_INTERVAL_S = real_interval
        M._PUSH_QUIET_MS = real_quiet
        M._push_state["last_fingerprint"] = None

    # ── The channel's state must be visible, or a dead one looks like a quiet one
    for key in ("enabled", "listeners", "running", "polls", "published",
                "quiet_for_other_channel", "errors", "last_error",
                "poll_interval_s"):
        check(f"push state reports `{key}`", key in M._push_state)

    print()
    print(f"{'FAILED: ' + str(len(FAILED)) if FAILED else 'all checks passed'}")
    for f in FAILED:
        print(f"  - {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
