"""Generate docs/MCP_TOOLS_REFERENCE.md from tool_meta.json + the live groups.

Generated, never hand-written: a hand-written tool list drifts from the code the
moment a tool is added, and a reference that lies is worse than none.
"""
import json, sys, asyncio
from pathlib import Path
# Derived, not hardcoded: this pointed at one developer's home
# directory, so the script only ran on his machine.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python_backend"))
from api import bridge_server as bs
from api import tool_meta as tm

groups = bs._tool_catalog_groups()
meta = tm.load()
import api.claude_mcp as m
live = {t.name: t for t in asyncio.run(m.mcp.list_tools())}

GROUP_BLURB = {
 "start": "Call this before anything else.",
 "bridge_setup": "FIRST-CONNECTION ONLY. This is how a program gets bridged. Read this group first if state is PROVISIONAL.",
 "cheap_visual_path": "The token-efficient way to see what happened. Use in this order.",
 "raw_logs": "The program's log, verbatim and uninterpreted. AgentVision never hides a line from you.",
 "diagnose": "Something is wrong and you want ranked hypotheses, not a haystack.",
 "investigate": "You have a lead and want to follow it.",
 "flight_recorder": "The window BEFORE a failure, frozen automatically.",
 "frames": "Individual screenshots. Prefer cheap_visual_path first.",
 "ui_tree": "The OS accessibility tree — exact widget text, no OCR guessing. GUI programs only.",
 "logs": "Log source wiring and adapter management.",
 "capture": "Start/stop/tune the screenshot timer.",
 "source": "The target program's own source code, indexed.",
 "retention": "Disk budget and the examine-before-delete queue.",
 "profiles": "Which program AgentVision is pointed at.",
 "health": "Is AgentVision itself working?",
 "program": "Target-process stats and cropping.",
 "bookmarks": "Saved moments and frame annotations.",
 "orient": "Quick situational awareness.",
 "wrap_up": "Summarise the session.",
}
ORDER = ["start","bridge_setup","orient","raw_logs","diagnose","cheap_visual_path",
         "flight_recorder","investigate","frames","ui_tree","logs","capture",
         "source","retention","health","program","profiles","bookmarks","wrap_up"]

L = ["# AgentVision MCP tool reference",
     "",
     "**Generated from `python_backend/api/tool_meta.json`. Do not edit by hand —",
     f"run `scripts/gen_tools_ref.py`.** {len(meta)} tools in {len(groups)} groups.",
     "",
     "Every description here was derived by reading the tool's implementation and",
     "its HTTP handler, not its docstring. Where the two disagreed, the handler won",
     "and the disagreement is recorded as a caveat.",
     "",
     "## How to read an entry", "",
     "- **needs** — hard preconditions. A tool whose `needs` your target cannot",
     "  satisfy will not help you. `none` means it always works.",
     "- **cost** — `free`/`low` return JSON. `high` returns a full image; reach for",
     "  it last (see the token rule in `AI_START_HERE.md`).",
     "- **languages** — `any` unless the tool depends on a language mechanism.",
     "- **caveat** — a known defect or limit found in the code. Trust this over the",
     "  tool's own docstring.",
     "",
     "### Precondition tokens", "",
     "| token | meaning |", "|---|---|",
     "| `none` | no precondition |",
     "| `capture_running` | the screenshot timer must be started |",
     "| `frames_on_disk` | at least one frame captured |",
     "| `log_source_any` / `_events` / `_text` | a declared, existing log source |",
     "| `adapter_with_level` | the adapter must extract a severity level |",
     "| `adapter_with_file_line` | the adapter must extract `file:line` |",
     "| `source_index` | the target's source has been indexed |",
     "| `ocr_backend` | an OCR engine is available |",
     "| `accessibility_api` | OS accessibility permission + a real window |",
     "| `gui_program` / `window_visible` | the target must have a window |",
     "| `input_daemon` | the input-recording daemon must be running |",
     "| `bridge_sealed` | the bridge must already be BUILT |",
     ""]

seen = set()
for g in ORDER + [k for k in groups if k not in ORDER]:
    if g not in groups: continue
    names = groups[g]
    L += [f"## `{g}` — {len(names)} tool(s)", ""]
    if GROUP_BLURB.get(g): L += [f"*{GROUP_BLURB[g]}*", ""]
    for n in names:
        seen.add(n)
        d = meta.get(n, {})
        L += [f"### `{n}`", ""]
        if d.get("summary"): L += [f"**Returns:** {d['summary']}", ""]
        if d.get("use_when"): L += [f"**Use when:** {d['use_when']}", ""]
        if d.get("does"):     L += [f"{d['does']}", ""]
        if d.get("benefit"):  L += [f"**Why not do it by hand:** {d['benefit']}", ""]
        bits = []
        if d.get("needs"): bits.append("needs: " + ", ".join(f"`{x}`" for x in d["needs"]))
        if d.get("cost"):  bits.append(f"cost: `{d['cost']}`")
        if d.get("languages"): bits.append("languages: " + ", ".join(f"`{x}`" for x in d["languages"]))
        if d.get("program_kinds"): bits.append("program kinds: " + ", ".join(d["program_kinds"]))
        if bits: L += ["  \n".join(bits), ""]
        if d.get("lang_reason"): L += [f"**Language limit:** {d['lang_reason']}", ""]
        if d.get("not_for"):  L += [f"**Not for:** {d['not_for']}", ""]
        if (d.get("code_note") or "").strip():
            L += [f"> ⚠ **Caveat (from the code):** {d['code_note']}", ""]

missing = sorted(set(live) - seen)
if missing:
    L += ["## Ungrouped", "", "These are registered but in no group — a bug if you see any:", ""]
    L += [f"- `{n}`" for n in missing] + [""]

(ROOT / "docs" / "MCP_TOOLS_REFERENCE.md").write_text("\n".join(L))
print(f"wrote docs/MCP_TOOLS_REFERENCE.md — {len(seen)} tools, {len(groups)} groups, {len(missing)} ungrouped")
