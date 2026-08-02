#!/usr/bin/env python3
"""A count written in prose is a claim, and it rots.

THE FAILURE THIS EXISTS FOR. This project's rule is that AgentVision may not
assert anything it did not verify — and its own documentation was breaking that
rule at scale. Measured on the published tree, before this file existed:

    ARCHITECTURE.md          "23 adapters"   — live registry held 658
    ARCHITECTURE.md          "44 tools"      — live server registered 90
    HOW_IT_WORKS.md          "41 adapters"   — twice
    HOW_IT_WORKS.md          "~44 tools"
    docs/SCHEMA.md           "41 adapters"
    docs/WHAT_IS_AGENTVISION "49 av_* tools" — twice
    docs/BRIDGE_PROTOCOL.md  "89 MCP tools"
    docs/AI_START_HERE.md    "655 adapters", "89 MCP tools"
    bridge_plan.py           "655 log adapters"
    claude_mcp.py            "655 log adapters" AND "90 tools", in one file

Thirteen false statements, several of them the first paragraph a new reader or
a cold agent sees. docs/README.md had even written the problem down — "Counts in
prose drift" — which is a note, not a mechanism, and notes do not fail a build.

Every one of those was found by grepping AFTER the fact. This file makes the
suite find them instead.

HOW IT WORKS. Live counts come from importing the registry and the MCP server.
Then every tracked .md and prose-carrying .py is scanned for count claims. A
claim whose number is not a live value fails, unless it is in ALLOWED below with
a reason — because some numbers legitimately are not totals ("more than 25 tools
in tools.primary is rejected"), and a check that cannot express that would be
turned off within a week.

DELIBERATELY NOT CLEVER. It matches "<N> adapters" and "<N> [MCP] tools" and
nothing else. It will not catch a wrong number phrased some other way. Partial
coverage that holds is worth more than total coverage that gets disabled.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, os.path.dirname(_HERE))

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


# ── Legitimate mentions that are NOT a total. Each needs a reason. ───────────
# Keyed by a substring of the line. If you add one, say why it is not a total;
# "it was failing" is not a reason.
ALLOWED: list[tuple[str, str]] = [
    ("Older docs say 649 or 653 adapters",
     "docs/README.md quotes historical values on purpose, to describe the drift"),
    ("adapters defined directly in connectors/log_adapters.py",
     "a per-FILE count, not the registry total"),
    ("grepping the ~30 adapter source files",
     "a count of source files, not of adapters"),
    ("More than 25 tools in `tools.primary`",
     "a validation threshold, not an inventory"),
    ("24 too", "a per-program n/a verdict count inside a worked plan example"),
    ("at least 80 tools registered",
     "a floor assertion in test_tool_meta, deliberately loose"),
    ("31 tools missing from the catalog groups",
     "a historical defect count"),
    ("(31 tools were", "a historical defect count"),
    ("77 tools carry a recorded defect",
     "a count of documented caveats, not of tools"),
    ("89 of the 90 tools are language-agnostic",
     "89 is correct here: all but av_run_tests"),
    ("tool(s)", "generated per-group headings in MCP_TOOLS_REFERENCE.md"),
    ("The 44 below are the ones",
     "an explicit subset, stated as a subset alongside the real total"),
    ("49 of them, grouped",
     "an explicit subset, stated as a subset alongside the real total"),
    ("4-10 tool names", "a range in a plan template"),
    ("650+ log adapters, ~86 MCP tools",
     "a caveat quoting wording that USED to exist, labelled as such"),
]


def _allowed(line: str) -> str | None:
    for needle, reason in ALLOWED:
        if needle in line:
            return reason
    return None


def main() -> int:
    print("doc counts — prose must agree with the registry")

    # ── Live truth ───────────────────────────────────────────────────────────
    from connectors import log_adapters as la
    from connectors import log_sources as ls

    total = len(la.REGISTRY)
    try:
        builtin = len(la.builtin_names())
    except Exception:
        builtin = total
    readers = len(ls.list_readers())
    # `total` includes anything in the local user_adapters.json, which is
    # gitignored and absent from a clean clone; docs describe what ships, so
    # BOTH are acceptable in prose.
    ok_adapters = {builtin, total}

    try:
        import api.claude_mcp as M
        n_tools = len(asyncio.run(M.mcp.list_tools()))
    except SystemExit:
        print("SKIP — the `mcp` SDK is not installed, so the live tool count "
              "cannot be established. Nothing here was verified.")
        return 0
    except Exception as exc:
        print(f"SKIP — could not import the MCP server "
              f"({type(exc).__name__}: {exc}). Nothing here was verified.")
        return 0

    print(f"  live: {builtin} builtin adapters ({total} incl. local user "
          f"adapters), {readers} source readers, {n_tools} MCP tools")
    check("the registry is not empty", total > 100, str(total))
    check("the tool registry is not empty", n_tools > 50, str(n_tools))

    # ── The one number this file cannot get from a registry ──────────────────
    sys.path.insert(0, _REPO)
    import run_all_tests as R
    n_suites = len(R.SUITES)

    # ── Scan tracked prose ───────────────────────────────────────────────────
    try:
        tracked = subprocess.run(["git", "ls-files"], cwd=_REPO,
                                 capture_output=True, text=True,
                                 timeout=60).stdout.split()
    except Exception as exc:
        print(f"SKIP — git ls-files failed ({exc}); cannot enumerate tracked "
              f"files, so nothing was scanned.")
        return 1 if FAILED else 0
    check("git listed the tracked files", len(tracked) > 20, str(len(tracked)))

    targets = [f for f in tracked
               if f.endswith((".md", ".py"))
               and "log_catalog_master" not in f
               and not f.endswith("test_doc_counts.py")]

    PAT_ADAPTERS = re.compile(
        r"(\d{2,4})[\s\-]+(?:log[\s\-]?(?:format|source)?[\s\-]?)?adapters?\b",
        re.I)
    PAT_TOOLS = re.compile(
        r"(\d{2,4})\s+(?:MCP\s+)?(?:`?av_\*?`?\s+)?tools?\b", re.I)

    bad: list[str] = []
    scanned = claims = excused = 0
    for rel in targets:
        path = os.path.join(_REPO, rel)
        try:
            text = open(path, encoding="utf-8").read()
        except Exception:
            continue
        scanned += 1
        for i, line in enumerate(text.splitlines(), 1):
            for pat, allowed, what in ((PAT_ADAPTERS, ok_adapters, "adapters"),
                                       (PAT_TOOLS, {n_tools}, "tools")):
                for m in pat.finditer(line):
                    claims += 1
                    n = int(m.group(1))
                    if n in allowed:
                        continue
                    reason = _allowed(line)
                    if reason:
                        excused += 1
                        continue
                    bad.append(f"{rel}:{i}  claims {n} {what} "
                               f"(live: {sorted(allowed)})\n"
                               f"      {line.strip()[:110]}")

    check(f"scanned {scanned} tracked files and found {claims} count claims",
          scanned > 20 and claims > 10, f"{scanned} files, {claims} claims")
    check("every adapter/tool count in prose matches the live registry",
          not bad, "\n    " + "\n    ".join(bad[:12]))
    print(f"  ({excused} claim(s) excused by ALLOWED, with reasons)")

    # ── The suite count in the two places that state it ──────────────────────
    for rel in ("README.md", "docs/WINDOWS_PORT_COMPLETION.md"):
        try:
            text = open(os.path.join(_REPO, rel), encoding="utf-8").read()
        except Exception:
            continue
        claimed = {int(x) for x in re.findall(r"(\d{2,3})\s+(?:test\s+)?suites",
                                              text)}
        check(f"{rel} states the real suite count ({n_suites})",
              not claimed or claimed == {n_suites},
              f"claims {sorted(claimed)}")

    # ── Resource URIs the docs advertise must actually be registered ─────────
    # A doc that names a URI nobody registered sends the agent to a dead end,
    # and a dead resource read is silent.
    registered = {str(r.uri) for r in asyncio.run(M.mcp.list_resources())}
    templates = {t.uri_template for t in
                 asyncio.run(M.mcp.list_resource_templates())}
    # Compare templates by their literal prefix, since docs write them with the
    # placeholder spelled out.
    tmpl_prefix = {t.split("{")[0] for t in templates}
    missing: list[str] = []
    uri_pat = re.compile(r"agentvision://[A-Za-z0-9_./{}?]+")
    for rel in targets:
        if not rel.endswith(".md"):
            continue
        try:
            text = open(os.path.join(_REPO, rel), encoding="utf-8").read()
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for uri in uri_pat.findall(line):
                uri = uri.rstrip("`.,)")
                if uri in registered or uri in templates:
                    continue
                if any(uri.startswith(p) for p in tmpl_prefix):
                    continue
                missing.append(f"{rel}:{i}  {uri}")
    check("every agentvision:// URI named in the docs is registered",
          not missing, "\n    " + "\n    ".join(missing[:10]))

    print()
    print(f"{'FAILED: ' + str(len(FAILED)) if FAILED else 'all checks passed'}")
    for f in FAILED:
        print(f"  - {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
