"""Every tool is grouped, documented, and honestly described.

These are the checks that would have caught the two gaps found while building
this: 31 tools missing from the catalog groups (invisible to the first-bridge
review) and tool metadata drifting out of sync with the registered tool list.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


def live_tool_names() -> set:
    import api.claude_mcp as m
    return {t.name for t in asyncio.run(m.mcp.list_tools())}


def grouped_names() -> dict:
    from api import bridge_server as bs
    return bs._tool_catalog_groups()


def main() -> int:
    print("tool grouping + metadata")
    live = live_tool_names()
    groups = grouped_names()
    flat = [n for names in groups.values() for n in names]

    check("at least 80 tools registered", len(live) >= 80, f"got {len(live)}")

    ungrouped = sorted(live - set(flat))
    check("every registered tool is in a group", not ungrouped,
          f"{len(ungrouped)} ungrouped: {ungrouped[:8]}")

    phantom = sorted(set(flat) - live)
    check("no group lists a non-existent tool", not phantom, str(phantom))

    dupes = sorted({n for n in flat if flat.count(n) > 1})
    check("no tool appears in two groups", not dupes, str(dupes))

    from api import tool_meta as tm
    meta = tm.load()
    check("metadata loads", bool(meta), "tool_meta.json missing or unreadable")

    undoc = sorted(live - set(meta))
    check("every tool has metadata", not undoc,
          f"{len(undoc)} undocumented: {undoc[:8]}")

    stale = sorted(set(meta) - live)
    check("no metadata for removed tools", not stale, str(stale))

    # A description that says nothing is worse than none: it reads as documented.
    thin = [n for n, m in meta.items()
            if len((m.get("summary") or "").strip()) < 20
            or len((m.get("benefit") or "").strip()) < 20]
    check("no stub summaries or benefits", not thin, str(thin[:6]))

    bad_needs = [n for n, m in meta.items()
                 if not isinstance(m.get("needs"), list) or not m.get("needs")]
    check("every tool declares needs", not bad_needs, str(bad_needs[:6]))

    # A narrow language claim without a mechanism is a guess.
    unjustified = [n for n, m in meta.items()
                   if m.get("languages") != ["any"]
                   and not (m.get("lang_reason") or "").strip()]
    check("narrow language claims are justified", not unjustified,
          str(unjustified[:6]))

    # The whole point: relevance must actually discriminate between program kinds.
    caps_gui = {"none", "log_source_any", "frames_on_disk", "capture_running",
                "accessibility_api", "gui_program", "window_visible"}
    caps_svc = {"none", "log_source_any", "log_source_text"}
    out_gui = sum(1 for n in live
                  if tm.relevance(n, visual=True, gui=True,
                                  capabilities=caps_gui)["verdict"] == "n/a")
    out_svc = sum(1 for n in live
                  if tm.relevance(n, visual=False, gui=False,
                                  capabilities=caps_svc)["verdict"] == "n/a")
    check("a headless service rules out more tools than a GUI", out_svc > out_gui,
          f"gui={out_gui} service={out_svc}")
    check("a GUI program keeps most tools", out_gui < 10, f"ruled out {out_gui}")
    check("a headless service rules out the frame tools", out_svc >= 15,
          f"only {out_svc}")

    for t in ("av_ui_tree", "av_ui_diff"):
        v = tm.relevance(t, visual=False, gui=False, capabilities=caps_svc)
        check(f"{t} is n/a without a GUI", v["verdict"] == "n/a", str(v))

    v = tm.relevance("av_diagnose", visual=False, gui=False, capabilities=caps_svc)
    check("av_diagnose stays available on a headless service",
          v["verdict"] != "n/a", str(v))

    print(f"\n{len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
