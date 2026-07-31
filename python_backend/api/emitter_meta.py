#!/usr/bin/env python3
"""Per-emitter decision metadata for the bridge catalog.

Why this exists
---------------
`bridge_plan._emitter_options()` carried ~450 bytes per emitter: `captures`,
`misses`, `cost`, `good_for`. That is enough for a model that already knows what
"tee stdout" means and not nearly enough for the weak, cold model this project
is actually built for. A Haiku-class agent asked to scan an unfamiliar program
and decide which emitters it needs has to answer four questions the thin fields
never addressed:

  • what would I SEE in this codebase that says I need this?   -> code_signals
  • when would picking this be a MISTAKE?                       -> do_not_use_when
  • what does it actually write, and where?                     -> builds_as
  • how do I check afterwards that it worked?                   -> how_to_verify

Size discipline
---------------
The full specs are ~14.7 KB each, 226 KB in total. Serving that inline would
make the catalog unreadable and blow the context of exactly the small models it
is for. So the same pattern as the MCP tool groups: a compact view by default
(~3 KB per emitter, comparable to tool_meta's 2.6 KB), the whole spec only under
`detail=full`, and the catalog states plainly how much it is holding back so a
model can never mistake brevity for completeness.

Truncation is sentence-aware and always marked. A clip that ends mid-clause and
says nothing about it invites a model to act on half a caveat, which is worse
than not showing the field at all.
"""
from __future__ import annotations

import json
from pathlib import Path

_META_PATH = Path(__file__).with_name("emitter_meta.json")

#: Fields kept in the compact view, with a byte budget each. Ordered as a model
#: should read them: what it is, when to pick it, when NOT to, how to confirm.
_COMPACT_BUDGET: dict[str, int] = {
    "one_line": 260,
    "what_it_does": 340,
    "use_when": 360,
    "do_not_use_when": 360,
    "code_signals": 320,
    "captures": 320,
    "misses": 360,
    "cost": 280,
    "builds_as": 280,
    "enforced": 260,
    "how_to_verify": 320,
}
#: Short structural fields passed through whole — they are lists or one-liners.
_PASSTHROUGH = ("id", "languages", "program_kinds", "pairs_with")


def _load() -> dict:
    try:
        with _META_PATH.open(encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"emitters": {}, "recipes": []}


_META = _load()
_EMITTERS: dict = _META.get("emitters") or {}
_RECIPES: list = _META.get("recipes") or []


def _clip(text: str, budget: int) -> str:
    """Cut to `budget` at a sentence boundary, marking the cut.

    A caveat truncated mid-clause reads as a complete instruction, so the marker
    is not cosmetic: it is the difference between "this is all of it" and "ask
    for detail=full before you rely on this".
    """
    s = " ".join(str(text or "").split())
    if len(s) <= budget:
        return s
    head = s[:budget]
    for sep in (". ", "; ", " - ", ", "):
        cut = head.rfind(sep)
        if cut > budget * 0.55:
            return head[:cut + 1].rstrip() + " […detail=full]"
    cut = head.rfind(" ")
    return (head[:cut] if cut > 0 else head).rstrip() + " […detail=full]"


def ids() -> list[str]:
    return sorted(_EMITTERS)


def spec(emitter_id: str) -> dict:
    """The complete spec for one emitter, or {} if unknown."""
    return dict(_EMITTERS.get(emitter_id) or {})


def compact(emitter_id: str) -> dict:
    """Decision fields for one emitter, clipped to the per-field budgets."""
    raw = _EMITTERS.get(emitter_id)
    if not raw:
        return {}
    out: dict = {}
    for key in _PASSTHROUGH:
        if raw.get(key) not in (None, "", [], {}):
            out[key] = raw[key]
    for key, budget in _COMPACT_BUDGET.items():
        val = raw.get(key)
        if val in (None, "", [], {}):
            continue
        if isinstance(val, (list, tuple)):
            val = "; ".join(str(v) for v in val)
        out[key] = _clip(val, budget)
    return out


def hidden_bytes(emitter_ids: list[str] | None = None) -> int:
    """How much spec text the compact view is NOT showing, for disclosure."""
    total = 0
    for eid in (emitter_ids if emitter_ids is not None else ids()):
        raw = _EMITTERS.get(eid) or {}
        total += max(0, len(json.dumps(raw)) - len(json.dumps(compact(eid))))
    return total


def recipes(language: str = "") -> list[dict]:
    """Worked program-shape recipes: "this kind of program -> these emitters".

    A model that cannot map an abstract option list onto the program in front of
    it can often match a shape ("a GUI app with no logging at all"). Filtered by
    language when one is known, since a Java recipe is noise to a Python agent.
    """
    lang = (language or "").lower().strip()
    if not lang:
        return [dict(r) for r in _RECIPES]
    keep = []
    for r in _RECIPES:
        langs = [str(x).lower() for x in (r.get("languages") or [])]
        if not langs or lang in langs or "any" in langs:
            keep.append(dict(r))
    return keep or [dict(r) for r in _RECIPES]


def recipe_index(language: str = "") -> list[dict]:
    """One line per recipe: the shape, and what it concluded.

    The full recipes are 87 KB — more than the rest of the catalog put together —
    so the default view is an index. A model scanning for "which of these is the
    program in front of me?" only needs the shape and the answer; it can then ask
    for detail=full to see how_to_recognise, why_each, deliberately_not, and
    what_would_still_be_invisible for the one that matched.
    """
    out = []
    for r in recipes(language):
        out.append({
            "shape": _clip(r.get("shape") or "", 190),
            "emitters": list(r.get("emitters") or []),
            "visual_capture": _clip(str(r.get("visual_capture") or ""), 90),
        })
    return out


def for_language(language: str) -> list[str]:
    """Emitter ids whose spec claims support for this language."""
    lang = (language or "").lower().strip()
    out = []
    for eid, raw in _EMITTERS.items():
        langs = [str(x).lower() for x in (raw.get("languages") or [])]
        if not langs or "any" in langs or "all" in langs or lang in langs:
            out.append(eid)
    return sorted(out)


def corrections() -> list[str]:
    """Known errors already fixed in this file, kept visible on purpose."""
    return list(_META.get("_corrections") or [])


if __name__ == "__main__":
    print(f"{len(_EMITTERS)} emitters, {len(_RECIPES)} recipes")
    for eid in ids():
        c = compact(eid)
        print(f"  {eid:24} compact={len(json.dumps(c)):5}B  "
              f"full={len(json.dumps(spec(eid))):6}B  "
              f"fields={len(c)}")
    print(f"\nhidden behind detail=full: {hidden_bytes():,} bytes")
