#!/usr/bin/env python3
"""
UI / accessibility-tree tests.

The tree backends are per-OS and OPTIONAL, so this suite is built so it passes
everywhere: the OS-independent logic (pruning, flattening, semantic diffing, the
honest cost verdict, graceful unavailability) is tested unconditionally against
synthetic trees, and the live backend is exercised only when it is actually
importable on this machine — reported as SKIP otherwise, never as a failure.

That split matters: `available:false` is a legitimate answer for custom-drawn UIs
(games, emulators, canvas apps), so the fallback contract is itself under test.

Run:  python3 python_backend/utils/test_ui_tree.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent))
sys.path.insert(0, str(_HERE.parent.parent.parent))

_fails = 0


def check(name, cond, detail=""):
    global _fails
    if not cond:
        _fails += 1
    print(f"  [{'ok ' if cond else 'FAIL'}] {name}"
          f"{'' if cond or not detail else '  — ' + detail}")


def skip(name, why):
    print(f"  [skip] {name}  — {why}")


def main():
    from utils import ui_tree as ut

    # ── Backend probe never raises ───────────────────────────────────────────
    print("backend probe:")
    b = ut.backends_available()
    check("probe returns a dict with platform", isinstance(b, dict) and b.get("platform"))
    check("probe names a backend or explains why not",
          bool(b.get("backend")) or bool(b.get("reason")), json.dumps(b))
    check("probe reports importability as a bool",
          isinstance(b.get("importable"), bool))
    if not b.get("importable"):
        check("an unimportable backend explains how to install it",
              bool(b.get("reason")), json.dumps(b))

    # ── Graceful unavailability is part of the contract ──────────────────────
    print("unavailability contract:")
    u = ut._unavailable("synthetic reason")
    check("unavailable payload says available:false", u.get("available") is False)
    check("unavailable payload names a fallback tool",
          "av_frame_json" in (u.get("fallback") or ""))
    check("unavailable payload explains custom-drawn UIs",
          "custom-drawn" in (u.get("note") or "").lower()
          or "Custom-drawn" in (u.get("note") or ""))
    check("unavailable payload includes the backend probe",
          isinstance(u.get("backends"), dict))

    # ── Pruning ──────────────────────────────────────────────────────────────
    print("pruning:")
    raw = [{
        "role": "AXWindow", "text": "Main", "bbox": [0, 0, 800, 600],
        "children": [
            # A chain of text-free single-child containers: should collapse away.
            {"role": "AXGroup", "text": None, "bbox": [0, 0, 800, 600], "children": [
                {"role": "AXSplitGroup", "text": None, "bbox": [0, 0, 800, 600],
                 "children": [
                     {"role": "AXButton", "text": "Start", "bbox": [10, 10, 80, 24]},
                     {"role": "AXStaticText", "text": "Ready", "bbox": [10, 50, 120, 18]},
                 ]},
            ]},
            # Invisible / zero-area: dropped.
            {"role": "AXStaticText", "text": None, "bbox": [0, 0, 0, 0]},
            # Text-free non-meaningful VISIBLE leaf: dropped as a silent leaf
            # (AXStaticText is neither scaffolding nor actionable, so it is the
            # rule-3 case rather than the container case).
            {"role": "AXStaticText", "text": None, "bbox": [300, 10, 40, 10]},
            # Text-free scaffolding leaf: dropped by the container rule.
            {"role": "AXCell", "text": None, "bbox": [5, 5, 20, 20]},
            # Text-free but MEANINGFUL: kept (an agent can act on it).
            {"role": "AXButton", "text": None, "bbox": [200, 10, 40, 24]},
        ]}]
    pruned, stats = ut.prune(raw)
    flat = ut.flatten(pruned)
    roles = [r["role"] for r in flat]
    texts = [r.get("text") for r in flat if r.get("text")]
    check("real content survives pruning",
          "Start" in texts and "Ready" in texts, str(texts))
    check("text-free single-child containers are collapsed",
          "AXSplitGroup" not in roles and stats["dropped_empty_containers"] >= 1,
          f"{roles} {stats}")
    check("zero-area nodes are dropped", stats["dropped_invisible"] >= 1, str(stats))
    check("text-free non-meaningful leaves are dropped",
          stats["dropped_silent_leaves"] >= 1, f"{roles} {stats}")
    check("text-free scaffolding leaves are dropped too",
          "AXCell" not in roles and "AXStaticText" not in
          [r["role"] for r in flat if not r.get("text")], f"{roles}")
    check("text-free MEANINGFUL roles are kept",
          roles.count("AXButton") == 2, str(roles))
    check("prune reports how many nodes it kept",
          stats.get("nodes_kept") == len(flat), f"{stats.get('nodes_kept')} vs {len(flat)}")
    check("pruning strictly shrinks the tree", len(flat) < ut._count(raw),
          f"{len(flat)} vs {ut._count(raw)}")

    # Node cap.
    big = [{"role": "AXButton", "text": f"b{i}", "bbox": [0, i, 10, 10]}
           for i in range(ut.MAX_NODES + 50)]
    pruned_big, stats_big = ut.prune(big)
    check("node cap is enforced", ut._count(pruned_big) <= ut.MAX_NODES,
          str(ut._count(pruned_big)))
    check("over-cap drops are reported", stats_big["dropped_over_cap"] >= 1,
          str(stats_big))

    # Depth cap.
    deep = {"role": "AXButton", "text": "leaf", "bbox": [0, 0, 5, 5]}
    for _ in range(ut.MAX_DEPTH + 8):
        deep = {"role": "AXGroup", "text": "g", "bbox": [0, 0, 50, 50],
                "children": [deep]}
    pruned_deep, _ = ut.prune([deep])
    def _depth(ns, d=0):
        return max([_depth(n.get("children") or [], d + 1) for n in ns] + [d])
    check("depth cap is enforced", _depth(pruned_deep) <= ut.MAX_DEPTH + 1,
          str(_depth(pruned_deep)))
    check("pruning an empty tree is safe", ut.prune([]) == ([], ut.prune([])[1]))

    # ── flatten shape ────────────────────────────────────────────────────────
    print("flatten:")
    check("flat rows carry depth, role and (when present) text/bbox",
          all(("d" in r and "role" in r) for r in flat), json.dumps(flat[:2]))
    check("flat rows omit empty fields to save tokens",
          all(("text" in r) == bool(r.get("text")) for r in flat))
    check("flat rows do NOT carry verbose slash-paths",
          all("path" not in r for r in flat),
          "full paths were a large share of the payload for deep trees")

    # ── Semantic diff ────────────────────────────────────────────────────────
    print("semantic diff:")
    before = [{"role": "AXWindow", "text": "App", "bbox": [0, 0, 400, 300],
               "children": [
                   {"role": "AXStaticText", "text": "Ready", "bbox": [10, 10, 60, 16]},
                   {"role": "AXButton", "text": "Start", "bbox": [10, 40, 60, 24]},
               ]}]
    after = [{"role": "AXWindow", "text": "App", "bbox": [0, 0, 400, 300],
              "children": [
                  {"role": "AXStaticText", "text": "Error: timeout",
                   "bbox": [10, 10, 60, 16]},
                  {"role": "AXButton", "text": "Retry", "bbox": [200, 40, 60, 24]},
              ]}]
    d = ut.diff_trees(before, after)
    check("a changed label is reported as CHANGED, not add+remove",
          any(c.get("from") == "Ready" and c.get("to") == "Error: timeout"
              for c in d["changed"]), json.dumps(d["changed"]))
    check("a moved+renamed element is an appear/disappear pair",
          d["counts"]["appeared"] >= 1 and d["counts"]["disappeared"] >= 1,
          json.dumps(d["counts"]))
    check("diff produces a human-readable summary",
          any("Error: timeout" in s for s in d["summary"]), json.dumps(d["summary"]))
    check("identical trees diff to identical=True",
          ut.diff_trees(before, before)["identical"] is True)
    check("identical trees report zero counts",
          ut.diff_trees(before, before)["counts"]
          == {"appeared": 0, "disappeared": 0, "changed": 0})
    # A 1-px reflow must NOT be reported (position is quantised).
    nudged = json.loads(json.dumps(before))
    nudged[0]["children"][0]["bbox"] = [11, 10, 60, 16]
    check("a 1-px reflow is not reported as a change",
          ut.diff_trees(before, nudged)["identical"] is True,
          json.dumps(ut.diff_trees(before, nudged)["counts"]))
    check("diffing empty trees is safe",
          ut.diff_trees([], [])["identical"] is True)
    check("diff lists are bounded",
          all(len(ut.diff_trees([], [{"role": "AXButton", "text": f"b{i}",
                                      "bbox": [0, i, 5, 5]}
                                     for i in range(200)])[k]) <= 50
              for k in ("appeared", "disappeared", "changed")))

    # ── Live backend (only if importable here) ───────────────────────────────
    print("live backend:")
    if not b.get("importable"):
        skip("live capture", f"{b.get('backend')} not importable: "
                             f"{str(b.get('reason'))[:80]}")
        r = ut.capture_tree("definitely-not-a-real-app-xyz")
        check("capture_tree degrades gracefully instead of raising",
              r.get("available") is False and bool(r.get("fallback")))
        check("a graceful failure still reports its cost_ms", "cost_ms" in r)
    else:
        r = ut.capture_tree("", flat=True, frame_size=(2560, 1440))
        check("capture_tree returns a dict with available",
              isinstance(r, dict) and "available" in r)
        if r.get("available"):
            check("element_count is reported", (r.get("element_count") or 0) >= 1)
            check("raw vs pruned counts are both reported",
                  "raw_element_count" in r and "pruned_away" in r,
                  json.dumps({k: r.get(k) for k in
                              ("element_count", "raw_element_count", "pruned_away")}))
            # NOT "pruned_away > 0": this reads the LIVE frontmost window, and a
            # minimal tree (a terminal can expose ~5 nodes) legitimately has
            # nothing prunable. Asserting otherwise made the suite depend on
            # which window happened to have focus. Pin the INVARIANT instead —
            # the arithmetic must always reconcile.
            raw = r.get("raw_element_count") or 0
            kept = r.get("element_count") or 0
            pruned = r.get("pruned_away") or 0
            check("pruning accounting reconciles (raw == kept + pruned_away)",
                  raw == kept + pruned, f"{raw} != {kept} + {pruned}")
            check("pruning never invents nodes", kept <= raw, f"{kept} > {raw}")
            check("pruned_away is never negative", pruned >= 0, str(pruned))
            check("node cap respected", (r.get("element_count") or 0) <= ut.MAX_NODES)
            check("traversal is deadline-bounded",
                  (r.get("cost_ms") or 0) < ut.TREE_DEADLINE_MS + 2000,
                  f"{r.get('cost_ms')}ms vs deadline {ut.TREE_DEADLINE_MS}ms")
            elems = r.get("elements") or []
            check("elements carry exact bboxes (not OCR guesses)",
                  any(e.get("bbox") for e in elems),
                  "no element had a bbox — the AXValue unwrap is broken")
            check("elements carry real text",
                  any(e.get("text") for e in elems), json.dumps(elems[:3]))
            cost = r.get("cost") or {}
            check("cost verdict is reported honestly",
                  "est_tokens" in cost and "cheaper_than_screenshot" in cost,
                  json.dumps(cost))
            check("cost verdict states the method",
                  "estimate" in (cost.get("method") or "").lower())
            print(f"    live: {r['element_count']} elems from "
                  f"{r['raw_element_count']} raw in {r['cost_ms']}ms; "
                  f"{cost.get('est_tokens')} est tokens vs "
                  f"{cost.get('est_visual_tokens_of_the_screenshot')} for the "
                  f"screenshot ({cost.get('ratio_vs_screenshot')}x)")
        else:
            skip("live tree content", f"no tree here: {str(r.get('reason'))[:80]}")
            check("unavailable live result still names a fallback",
                  bool(r.get("fallback")))
        # A nonsense app name must be a clean miss, not an exception.
        miss = ut.capture_tree("definitely-not-a-real-app-xyz")
        check("nonexistent app -> clean available:false",
              miss.get("available") is False and bool(miss.get("reason")),
              json.dumps({k: miss.get(k) for k in ("available", "reason")})[:160])

    print(f"\n{'=' * 60}")
    print("ui_tree: " + ("ALL PASS" if _fails == 0 else f"{_fails} FAILURE(S)"))
    print("=" * 60)
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
