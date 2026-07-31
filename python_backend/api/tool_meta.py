"""Per-tool metadata: what each MCP tool does, needs, and is good for.

Why this exists
---------------
AgentVision has ~90 tools. On a first bridge the agent must decide which of them
are worth anything for THIS program, and a bare tool name does not support that
decision: `av_ui_tree` is indispensable on a Tk app and completely useless on a
headless C binary, but nothing in the name says so.

Each entry was derived by reading the tool body and its route handler — not from
the docstrings, which in several cases were measurably wrong about their own
behaviour (see `code_note`). Where the two disagreed, the handler won.

`needs` is the load-bearing field. It is what makes relevance mechanical rather
than a vibe: a tool that needs `accessibility_api` cannot help a program with no
window, and a tool that needs `adapter_with_file_line` is dead weight if the log
format carries no source location.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

META_PATH = Path(__file__).with_name("tool_meta.json")

# A need this program cannot satisfy makes every tool requiring it irrelevant.
# Mapping is deliberately conservative: only rule a tool OUT when the program
# genuinely cannot meet the precondition, never merely "probably won't".
_GUI_ONLY_NEEDS = {"accessibility_api", "window_visible", "gui_program",
                   "input_daemon"}
_VISUAL_NEEDS = {"frames_on_disk", "capture_running", "ocr_backend"} | _GUI_ONLY_NEEDS


@lru_cache(maxsize=1)
def load() -> dict:
    try:
        return json.loads(META_PATH.read_text(errors="replace"))
    except Exception:
        return {}


def for_tool(name: str) -> dict:
    return load().get(name, {})


def documented_count() -> int:
    return len(load())


def summary_line(name: str) -> str:
    m = for_tool(name)
    return (m.get("summary") or "").strip()


def _lang_ok(meta: dict, language: str) -> bool:
    langs = [str(x).lower() for x in (meta.get("languages") or ["any"])]
    if "any" in langs or not language:
        return True
    return language.lower() in langs


def relevance(name: str, *, language: str = "", visual: bool = True,
              gui: bool = True, capabilities: Optional[Iterable[str]] = None
              ) -> dict:
    """Is this tool worth anything for a program with these properties?

    Returns {verdict, reason}. `verdict` is one of:
      core      — applicable and needs nothing special
      useful    — applicable, but gated on a capability listed in `blocked_on`
      n/a       — cannot work here; the reason names the specific blocker

    `capabilities` is the set of `needs` tokens this program CAN satisfy. Anything
    not listed is treated as unsatisfied only when it is a hard structural need
    (a window, an accessibility API); soft needs just downgrade to `useful`.
    """
    meta = for_tool(name)
    if not meta:
        return {"verdict": "unknown", "reason": "no metadata for this tool"}

    needs = set(meta.get("needs") or ["none"])
    have = set(capabilities or [])

    if not _lang_ok(meta, language):
        return {"verdict": "n/a",
                "reason": f"language {language or '?'} not in "
                          f"{meta.get('languages')} — {meta.get('lang_reason') or ''}".strip()}

    if not visual and (needs & _VISUAL_NEEDS):
        blocker = sorted(needs & _VISUAL_NEEDS)[0]
        return {"verdict": "n/a",
                "reason": f"needs {blocker}, but visual capture is off for this program"}

    if not gui and (needs & _GUI_ONLY_NEEDS):
        blocker = sorted(needs & _GUI_ONLY_NEEDS)[0]
        return {"verdict": "n/a",
                "reason": f"needs {blocker}, which a non-GUI program cannot provide"}

    unmet = sorted(n for n in needs if n != "none" and n not in have)
    if unmet and have:
        return {"verdict": "useful", "reason": "available once " + ", ".join(unmet),
                "blocked_on": unmet}
    return {"verdict": "core", "reason": "no unmet precondition"}


def compact(name: str) -> dict:
    """The 4 fields worth spending catalog tokens on, per tool."""
    m = for_tool(name)
    if not m:
        return {"tool": name, "summary": "(undocumented)"}
    return {"tool": name, "summary": m.get("summary", ""),
            "needs": m.get("needs", []), "cost": m.get("cost", "")}


def detail(name: str) -> dict:
    """Everything known about one tool — for the review pass, not routine use."""
    m = for_tool(name)
    if not m:
        return {"tool": name, "error": "undocumented"}
    out = {"tool": name}
    out.update(m)
    return out


def known_defects() -> dict:
    """{tool: code_note} for every tool with a recorded defect or caveat.

    Surfaced deliberately: a tool whose docstring lies about its own behaviour is
    worse than a missing tool, because the agent trusts it.
    """
    return {k: v["code_note"] for k, v in load().items()
            if (v.get("code_note") or "").strip()}
