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


async def main() -> int:
    from mcp import ClientSession, StdioServerParameters, stdio_client

    expect = DEFAULT_EXPECT
    for a in sys.argv[1:]:
        if a.startswith("--expect="):
            expect = [x.strip() for x in a.split("=", 1)[1].split(",") if x.strip()]

    env = dict(os.environ)
    env["PYTHONPATH"] = REPO + os.pathsep + env.get("PYTHONPATH", "")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "python_backend.api.claude_mcp"],
        env=env,
        cwd=REPO,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
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

            print("\nALL TOOLS:\n  " + "\n  ".join(names))
            if missing:
                print(f"\nFAIL: missing {missing}")
                return 1
            print("\nHANDSHAKE PASS")
            return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
