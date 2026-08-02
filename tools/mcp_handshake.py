#!/usr/bin/env python3
"""Real stdio MCP handshake against AgentVision's own MCP server.

Launches `python -m python_backend.api.claude_mcp` as a child over stdio,
performs initialize + list_tools (+ list_prompts / list_resources when the
server advertises them) and prints a verification summary.

Usage:
    .venv/bin/python tools/mcp_handshake.py [--expect av_diagnose,av_preflight,...]
"""

from __future__ import annotations

import asyncio
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_EXPECT = [
    "av_diagnose", "av_preflight", "av_session_report", "av_read_screen",
    "av_timeline", "av_start_here", "av_visual_changes", "av_frame_json",
    "av_frame_region", "av_error_moment", "av_token_report",
]


def _json_body(result):
    """The JSON a tool returned, out of the MCP content envelope."""
    import json
    for c in getattr(result, "content", []) or []:
        if getattr(c, "type", "") == "text":
            try:
                return json.loads(c.text)
            except Exception:
                return {"_raw": c.text[:400]}
    return {}


async def main() -> int:
    from mcp import ClientSession, StdioServerParameters, stdio_client
    import mcp_types as _T

    expect = DEFAULT_EXPECT
    for a in sys.argv[1:]:
        if a.startswith("--expect="):
            expect = [x.strip() for x in a.split("=", 1)[1].split(",") if x.strip()]

    failures: list[str] = []
    asked: list[str] = []
    elicit_mode = {"action": "accept"}

    async def elicit_cb(context, params):
        """Stand in for the human. Records that the question was actually put."""
        asked.append(getattr(params, "message", ""))
        if elicit_mode["action"] == "accept":
            return _T.ElicitResult(action="accept",
                                   content={"shots_per_second": 4.0})
        return _T.ElicitResult(action=elicit_mode["action"])

    env = dict(os.environ)
    env["PYTHONPATH"] = REPO + os.pathsep + env.get("PYTHONPATH", "")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "python_backend.api.claude_mcp"],
        env=env,
        cwd=REPO,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write,
                                 elicitation_callback=elicit_cb) as session:
            init = await session.initialize()
            info = getattr(init, "serverInfo", None) or getattr(init, "server_info", None)
            print(f"initialize OK  server={getattr(info, 'name', '?')} "
                  f"version={getattr(info, 'version', '?')} "
                  f"protocol={getattr(init, 'protocolVersion', None) or getattr(init, 'protocol_version', '?')}")
            instr = (getattr(init, "instructions", None)
                     or getattr(session, "instructions", None) or "")
            print(f"instructions: {len(str(instr))} chars"
                  + (f"  first line: {str(instr).splitlines()[0][:70]}" if instr else " (NONE)"))
            caps = getattr(init, "capabilities", None)
            print(f"capabilities: tools={bool(getattr(caps, 'tools', None))} "
                  f"prompts={bool(getattr(caps, 'prompts', None))} "
                  f"resources={bool(getattr(caps, 'resources', None))}")

            tools = (await session.list_tools()).tools
            names = sorted(t.name for t in tools)
            print(f"TOOL COUNT: {len(names)}")

            missing = [e for e in expect if e not in names]
            for e in expect:
                print(f"  {'OK  ' if e in names else 'MISS'} {e}")

            try:
                prompts = (await session.list_prompts()).prompts
                print(f"PROMPT COUNT: {len(prompts)}  " +
                      ", ".join(p.name for p in prompts))
            except Exception as e:
                print(f"prompts: unavailable ({type(e).__name__}: {e})")

            try:
                res = (await session.list_resources()).resources
                print(f"RESOURCE COUNT: {len(res)}  " +
                      ", ".join(str(r.uri) for r in res))
            except Exception as e:
                print(f"resources: unavailable ({type(e).__name__}: {e})")

            try:
                tl = await session.list_resource_templates()
                tmpl = getattr(tl, "resourceTemplates", None) or \
                    getattr(tl, "resource_templates", [])
                print(f"TEMPLATE COUNT: {len(tmpl)}  " +
                      ", ".join(t.uriTemplate if hasattr(t, "uriTemplate")
                                else t.uri_template for t in tmpl))
            except Exception as e:
                print(f"resource templates: unavailable ({type(e).__name__}: {e})")

            # ── ELICITATION, over the real wire ──────────────────────────────
            # This client DOES support elicitation, which no unit test can
            # simulate: the round trip goes out over stdio and comes back. The
            # thing being checked is not that a value arrives, but that the
            # response says HOW it arrived — a default presented as a decision
            # is the failure this whole mechanism exists to prevent.
            print()
            for label, action, expect_how, expect_changed in (
                    ("accept 4 shots/sec", "accept", "asked", True),
                    ("user declines", "decline", "declined", False)):
                elicit_mode["action"] = action
                asked.clear()
                r = _json_body(await session.call_tool(
                    "av_capture_set_interval", {}))
                ch = r.get("capture_rate_choice") or {}
                got_how = ch.get("how")
                ok = got_how == expect_how and bool(asked)
                if expect_changed:
                    ok = ok and abs(float(r.get("interval_seconds")
                                          or r.get("interval") or 0) - 0.25) < 1e-9
                else:
                    ok = ok and r.get("changed") is False
                print(f"  {'OK  ' if ok else 'FAIL'} elicitation / {label}: "
                      f"how={got_how} asked={bool(asked)}")
                print(f"        {ch.get('note')}")
                if not ok:
                    failures.append(f"elicitation/{label}")
                if action == "accept":
                    # Put it back: this is the user's real profile.
                    await session.call_tool("av_capture_set_interval",
                                            {"interval": 1.0})

            r = _json_body(await session.call_tool("av_start_here", {}))
            yc = r.get("your_client") or {}
            ok = yc.get("elicitation") is True
            print(f"  {'OK  ' if ok else 'FAIL'} your_client reports "
                  f"elicitation={yc.get('elicitation')} for a client that "
                  f"demonstrably has it")
            if not ok:
                failures.append("your_client.elicitation")

            print("\nALL TOOLS:\n  " + "\n  ".join(names))
            if missing:
                print(f"\nFAIL: missing {missing}")
                return 1
            if failures:
                print(f"\nFAIL: {', '.join(failures)}")
                return 1
            print("\nHANDSHAKE PASS")
            return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
