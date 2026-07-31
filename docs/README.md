# AgentVision docs — index

## Are you an AI model who has never used this program? Read these 5 lines.

1. Read **[AI_START_HERE.md](AI_START_HERE.md)** and nothing else. Everything else in this folder is reference you fetch later.
2. **The bridge gate:** AgentVision refuses to guess what logging to build into a program, so YOU decide it once, on first connection, with three calls — `av_bridge_status()` → `av_bridge_catalog()` → `av_bridge_commit(plan={...})`.
3. Until that plan is committed, `av_capture_start()` and `av_install_project()` are **REFUSED** — and a refusal is **HTTP 200** with `"started": false, "bridge_required": true`, so read the response **body**, never just the status code.
4. The gate fires **once per program, ever**. The plan is saved inside the target project, so restarting AgentVision, the bridge server, or your own session does not re-trigger it.
5. If you are already past the gate and something is broken, go straight to `av_diagnose()`, then the **TROUBLESHOOTING.md** row below.

The first-bridge sequence, complete and copy-pasteable:

```python
av_bridge_status()      # -> {"state": "PROVISIONAL"|"BUILT", "sealed": bool, "plan": ..., "note": ...}
av_bridge_catalog()     # -> emitters_available, adapters, mcp_tool_groups, code_evidence, catalog_token
av_bridge_commit(plan={
    "catalog_token": "PASTE_THE_catalog_token_FROM_THE_CALL_ABOVE",
    "emitters": ["lifecycle", "uncaught_exceptions"],
    "why": {
        "lifecycle": "code_evidence shows no exit logging; need to know if a run happened at all",
        "uncaught_exceptions": "code_evidence.threads is non-zero, so worker crashes never reach main"
    },
    "rationale": "Python CLI that prints but never logs; wants crash visibility, not full hooks.",
    "capture": {"interval_seconds": 1.0},
    "visual_capture": False,
    "tools": {
        "primary": ["av_diagnose", "av_log_raw", "av_error_moment"],
        "not_relevant": {"av_ui_tree": "headless program, there is no accessibility tree"},
        "note": "Headless target, so the whole visual group is skipped."
    }
})
```

If `av_bridge_status()` already says `BUILT`, **stop** — do not plan again. Use
`av_bridge_commit(plan={...}, replan=True)` only when you deliberately want to re-decide.

---

## Files in `docs/`

| File (lines) | What it answers | Read it when | Do NOT read it when |
|---|---|---|---|
| **AI_START_HERE.md** | The one-page operating manual: the gate, the three calls, the token rule, your first move. | Always, first, on any new session. | Never skip it. Fallback if missing: call `av_start_here()` — the same guidance is returned live, and the MCP server sends a condensed copy at connect time. |
| **BRIDGE_PROTOCOL.md** | The exact plan schema, every rejection rule, and what happens on commit. | Your `av_bridge_commit` call was rejected, or you want the full field list before calling. | Routine debugging after the bridge is BUILT. Fallback if missing: the `av_bridge_commit` docstring in `python_backend/api/claude_mcp.py` plus `validate_plan()` in `python_backend/bridge_plan.py`. |
| **MCP_TOOLS_REFERENCE.md** (1799) | Per-tool reference for all **90 tools in 19 groups**: what it returns, its `needs`, its token cost, its known caveat. | You need one specific tool's arguments, preconditions, or cost. | You are browsing. It is ~1800 lines — grep it for a tool name, do not read it end to end. **Generated** from `python_backend/api/tool_meta.json` by `scripts/gen_tools_ref.py`; never hand-edit it, your edit will be overwritten. |
| **LOGS_AND_EMITTERS.md** | Emitters — the things that **create** logs that do not exist yet — and which one each language gets. | Choosing `plan["emitters"]`, or the target program produces no output at all. | The program already logs well. Fallback if missing: `_emitter_options()` in `python_backend/bridge_plan.py` lists every option with its `captures`, `misses`, `cost`, and `builds_as`. |
| **ADAPTERS_GUIDE.md** | How to pick, test, and write an adapter, including beating a wrong incumbent with `outrank=`. | A log **exists** but parses wrong (wrong `source`, wrong level, unparsed lines). | The log does not exist yet — that is an emitter problem, not an adapter problem. Fallback if missing: `LOG_ADAPTERS.md` plus `python_backend/connectors/log_adapters.py`. |
| **LOG_ADAPTERS.md** (164) | The unified event schema every adapter must output, and how the registry is organised. | You need the exact normalized event shape, or you are writing an adapter by hand. | You just want to list adapters — call `av_list_adapters()` instead. **Stale:** it says emitters are "auto-installed on first attach". That is no longer true; the bridge gate installs them, and only what your plan named. |
| **TROUBLESHOOTING.md** | Symptom → cause → fix for the failures that look like bugs but are not. | A tool returned something impossible, capture will not start, or a log looks empty. | Before calling `av_diagnose()` — do that first, it is cheaper. Fallback if missing: `MCP_TOOL_AUDIT.md`, then `av_diagnose()` and `av_log_where()`. |
| **MCP_TOOL_AUDIT.md** (135) | Every known tool defect: **77 tools carry a recorded defect or caveat, 12 of them Class A** (the tool actively misleads its caller). | A tool's answer contradicts what you observe, or looks suspiciously clean. | For normal use. Do not copy findings from it into other docs — cite this file instead. |
| **WHAT_IS_AGENTVISION.md** (752) | The definitive project explanation: the universal debug log, the screenshot engine, and the screenshot↔log time-alignment. | A human asked what this project is, or you need to understand time-alignment deeply. | You are trying to get work done. It is 752 lines and predates the bridge gate. |
| **AGENT_INSTRUCTIONS.md** (142) | A paste-ready block for a project's `CLAUDE.md` that teaches a future session how to use AgentVision. | You are setting a project up for other agents. | You are the agent doing the work now — that is `AI_START_HERE.md`. Its pasted block predates the bridge gate and does not mention it. |
| **PUSH_MODE.md** (272) | The **push** channel: hooks that inject a few digested lines when the target breaks, with no tool call spent. | You want alerts without polling, or an unexplained `[AgentVision ALERT]` block appeared in your context. | Push mode is off until someone runs `agentvision install-hooks`; it is not part of the normal pull workflow. |
| **SCHEMA.md** (192) | The JSON contract: the frame (`_frame.json`), the unified event, and the digest. `schema_version` is `2.0.0`. | You are reading a raw frame file or writing code against a payload shape. | You are consuming tools normally — the tools already return the shapes described here. |
| **RESEARCH_TOKEN_EFFICIENCY.md** (292) | The published evidence and local measurements behind the cheap-path design (JSON before pixels). | Someone challenges the design, or you are writing about why it works. | You are debugging. It contains no operational instructions. |
| `log_catalog_master.json` (1.2 MB) | Raw source data for the format catalog. | Effectively never. | **Do not open it.** It is 1.2 MB of JSON and will blow your context. Query adapters with `av_list_adapters()`. |

## Files in the repo root

| File (lines) | What it answers | Read it when | Do NOT read it when |
|---|---|---|---|
| **`../ARCHITECTURE.md`** (129) | The 3-layer model: target program → bridge server on `http://127.0.0.1:7771` → MCP server → agent. | You need to know which process does what, or you are calling an HTTP route directly. | You only use `av_*` tools. Written 2026-07-11; its route count is stale and it predates the bridge gate. |
| **`../HOW_IT_WORKS.md`** (492) | Narrative walkthrough of the whole system, end to end. | You want the story rather than a reference table. | You have a specific question — a row above answers it in fewer tokens. Predates the bridge gate. |
| **`../SETUP.md`** (120) | macOS install steps for a **human**: Python, unzip, launch, screen-recording permission. | The user asks how to install or start AgentVision on a Mac. | You are an agent debugging a program. It is not a usage guide. |
| **`../SETUP-Windows.md`** (102) | The same install steps for Windows, using the `.bat` launchers. | The user is on Windows. | You are on macOS or Linux. Linux setup lives in `../dist/linux/README.md`. |

---

## Two warnings that apply to the whole folder

**1. Most of these docs predate the bridge gate.** Verified by grep: `WHAT_IS_AGENTVISION.md`,
`AGENT_INSTRUCTIONS.md`, `LOG_ADAPTERS.md`, `SCHEMA.md`, `PUSH_MODE.md`,
`RESEARCH_TOKEN_EFFICIENCY.md`, `../ARCHITECTURE.md`, `../HOW_IT_WORKS.md`, `../SETUP.md` and
`../SETUP-Windows.md` contain **zero** mentions of the gate. Where any of them implies AgentVision
installs logging for you automatically, it is wrong. The gate is authoritative. Trust
`AI_START_HERE.md`, `BRIDGE_PROTOCOL.md`, and the code in `python_backend/bridge_plan.py`.

**2. Counts in prose drift.** Older docs say 649 or 653 adapters. The live registry, checked by
importing it, holds **658 adapters and 9 source readers** (`av_capabilities()` reports the same),
and there are **90 MCP tools in 19 groups**. When a number matters, get it from `av_capabilities()`
or `av_bridge_catalog()` rather than from a sentence in a doc.
