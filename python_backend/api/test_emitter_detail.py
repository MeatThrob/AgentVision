#!/usr/bin/env python3
"""The emitter half of the bridge catalog: detail, honesty, and enforcement.

Each check here corresponds to a defect that was real and measured, not a
hypothetical:

  * The researched emitter specs existed but nothing served them — the catalog
    still returned the 449-byte stubs, so the model they were written for could
    never read a word of them.
  * The merge then let a 10-byte hand-written "negligible" overwrite a 1,319-byte
    measured cost, silently undoing the fix.
  * `user_input` was a real capability with no emitter id, reachable only through
    a GUI toggle, so no plan could ask for the one signal that answers "did the
    keypress even arrive?".
  * selection_report told Node/Ruby agents `enforced: true` for hooks whose
    emitter contains no gate at all.
  * A plan naming no hook ids still armed all five, and nothing said so.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
for p in (str(REPO), str(REPO / "python_backend"), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import bridge_plan as bp                                   # noqa: E402
import emitter_meta as em                                  # noqa: E402
from emitters import hooks_fail_open, selection_report      # noqa: E402

#: The 12 signal names that predate the vocabulary work. A new signal colliding
#: with one of these would silently shadow it in the scanner's dict.
_EXISTING_SIGNALS = {
    "exception_handlers", "discards_error", "logs_in_handler", "threads",
    "async", "subprocess", "existing_logging", "prints_only", "gui_toolkit",
    "web_service", "network_io", "file_io",
}


#: A tiny fixture tree, NOT the repo. catalog() scans project_root, and 68
#: signals over AgentVision's own 3.6 MB costs ~28 s — a price the suite would
#: pay on every run for evidence no check here looks at.
_FIXTURE = Path(tempfile.mkdtemp(prefix="av-emitter-detail-"))
(_FIXTURE / "app.py").write_text(
    "import logging\n"
    "log = logging.getLogger(__name__)\n"
    "def main():\n"
    "    try:\n"
    "        log.info('hi')\n"
    "    except Exception:\n"
    "        pass\n")


class _Profile:
    display_name = "TestProg"
    name = "testprog"
    language = "python"
    project_root = str(_FIXTURE)


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    prof = _Profile()
    full = bp.catalog(prof, "python")
    compact = bp.catalog(prof, "python", full_detail=False)
    opts = {o["id"]: o for o in full["emitters_available"]}

    # ── The specs must actually be REACHABLE ────────────────────────────────
    check("catalog serves full detail by DEFAULT (one-time cost)",
          len(json.dumps(full)) > len(json.dumps(compact)) * 2,
          f"full={len(json.dumps(full))} compact={len(json.dumps(compact))}")
    check("detail=compact still available for callers that want it",
          len(json.dumps(compact)) < len(json.dumps(full)))
    check("asking for either detail level does NOT change catalog_token",
          full["catalog_token"] == compact["catalog_token"],
          f"{full['catalog_token'][:12]} vs {compact['catalog_token'][:12]}")

    # The merge regression: a thin hand-written value must never clobber a
    # richer researched one.
    for eid, field, floor in (("logging_bridge", "cost", 800),
                              ("stdout_tee", "captures", 400),
                              ("lifecycle", "misses", 400)):
        val = str(opts.get(eid, {}).get(field, ""))
        check(f"{eid}.{field} carries the researched text, not the stub",
              len(val) >= floor, f"{len(val)} bytes: {val[:70]!r}")

    # The three fields a weak model could not answer from captures/misses/cost.
    for eid in sorted(opts):
        missing = [f for f in ("code_signals", "do_not_use_when", "how_to_verify")
                   if not str(opts[eid].get(f, "")).strip()]
        check(f"{eid} answers scan/avoid/verify", not missing, str(missing))

    # ── user_input: selectable, and selecting it must DO something ──────────
    check("user_input is offered as an emitter id", "user_input" in opts,
          str(sorted(opts)))
    check("user_input is offered regardless of language",
          all("user_input" in {o["id"] for o in
                               bp._emitter_options(lang)}
              for lang in ("python", "go", "typescript", "java", "rust", "")),
          "it watches the OS input stream, not the process")
    ui = opts.get("user_input", {})
    check("user_input states it writes NOTHING into the project",
          "nothing" in str(ui.get("builds_as", "")).lower(),
          str(ui.get("builds_as", ""))[:90])
    check("user_input flags the privacy cost",
          "privacy" in json.dumps(ui).lower())
    check("user_input warns that a stopped daemon reads as silence",
          "daemon" in str(ui.get("note", "") + str(ui.get("how_to_verify", ""))).lower())

    # `user_input` names a profile flag; that flag must be a real field, or the
    # emitter is decoration.
    try:
        from connectors.program_connector import ProgramProfile
        has_flag = hasattr(ProgramProfile(name="x", display_name="x"),
                           "capture_user_input")
    except Exception as exc:                      # pragma: no cover
        try:
            from connectors.program_connector import ProgramProfile
            has_flag = "capture_user_input" in getattr(
                ProgramProfile, "__annotations__", {})
        except Exception:
            has_flag = False
            check("ProgramProfile import", False, str(exc)[:120])
    check("capture_user_input is a real ProgramProfile field", has_flag)

    # user_input has no AGENTVISION_HOOKS gate, so it used to fall through to
    # "maps to no runtime hook, selecting it changes nothing" — false, since the
    # installer sets capture_user_input on the profile. A cold model flagged the
    # contradiction against the catalog on a real project.
    for lang in ("python", "node", "java", ""):
        ui_rep = selection_report(lang, ["user_input"])[0]
        check(f"user_input on {lang or 'unknown'}: never says it changes nothing",
              "changes nothing" not in ui_rep["how"], ui_rep["how"][:90])
        check(f"...and names the DAEMON as the gate ({lang or 'unknown'})",
              "DAEMON" in ui_rep["how"] and "capture_user_input" in ui_rep["how"])
        check(f"...and warns silence is not 'no input' ({lang or 'unknown'})",
              "av_daemon_status" in ui_rep["how"])

    # The advertised emitter ceiling must match what the validator enforces, and
    # must be computed from the list actually SERVED. A cold model read a stricter
    # advertised cap and dropped an emitter its own evidence supported.
    for lang in ("python", "node", "go"):
        served = bp._emitter_options(lang, also_present=["node", "python"])
        tmpl = bp.catalog(_Profile(), lang)["plan_template"]["emitters"][0]
        ceiling = bp.blanket_threshold(len(bp.catalog(_Profile(), lang)
                                          ["emitters_available"])) - 1
        check(f"{lang}: advertised ceiling matches the served list",
              f"HARD CEILING: {ceiling} " in tmpl, tmpl[:110])
        check(f"{lang}: ceiling is distinguished from the typical case",
              "CEILING, not a target" in tmpl)

    # ── Enforcement must be reported truthfully, per language ───────────────
    node = selection_report("node", ["stdout_tee"])[0]
    check("node hooks are NOT reported as enforced (av_emit.js has no gate)",
          node["enforced"] is False, json.dumps(node))
    ruby = selection_report("ruby", ["lifecycle"])[0]
    check("ruby hooks are NOT reported as enforced",
          ruby["enforced"] is False, json.dumps(ruby))
    py = selection_report("python", ["stdout_tee"])[0]
    check("python hooks ARE enforced (install_all_hooks gates each one)",
          py["enforced"] is True, json.dumps(py))
    java = selection_report("java", ["logging_bridge"])[0]
    check("one-artifact languages stay 'recorded only'",
          java["enforced"] is False, json.dumps(java))

    # The fail-open leak: disclosed when it applies, silent when it does not.
    leak = hooks_fail_open("python", ["config_dropin"])
    check("plan naming no hook ids -> over-install is DISCLOSED",
          bool(leak) and len(leak.get("will_also_arm") or []) == 5,
          json.dumps(leak)[:160] if leak else "None")
    check("...and it says how to confirm against the start record",
          bool(leak) and "hooks_armed" in json.dumps(leak))
    check("plan naming a real subset -> no leak, stays silent",
          hooks_fail_open("python", ["stdout_tee", "lifecycle"]) is None)
    check("non-gated language -> not applicable",
          hooks_fail_open("node", []) is None)

    # ── Recipes are worked examples, never a lookup table ──────────────────
    detail = compact.get("_emitter_detail") or {}
    idx = detail.get("worked_recipes_index") or []
    check("compact view carries a recipe INDEX, not 87 KB of recipes",
          0 < len(json.dumps(idx)) < 6000, f"{len(json.dumps(idx))} bytes")
    check("full view carries the complete recipes",
          len(full.get("_worked_recipes") or []) >= 5,
          str(len(full.get("_worked_recipes") or [])))
    guidance = str(detail.get("how_to_use_the_index", "")).lower()
    check("index explicitly forbids treating recipes as a lookup table",
          "do not treat this as a lookup table" in guidance, guidance[:110])
    check("index tells the model to DERIVE from code_signals",
          "derive" in guidance and "code_signals" in guidance)
    check("index says matching no recipe is normal, not unsupported",
          "not unsupported" in guidance or "is normal" in guidance)

    # ── The corrections must stay visible, not be quietly absorbed ─────────
    corr = em.corrections()
    check("spec corrections are recorded and surfaced", len(corr) >= 5,
          f"{len(corr)} corrections")
    blob = json.dumps(em._EMITTERS)
    check("no spec still claims user_input has no emitter id",
          "NO EMITTER ID" not in blob and "no plan can select it" not in blob)
    check("no spec still claims logging_bridge silences the program",
          "becomes a NO-OP" not in blob)
    check("logging_bridge cost records the basicConfig fix",
          "_shield_basicconfig" in str(em.spec("logging_bridge").get("cost", "")))

    # ── Clipping must be marked, never silent ─────────────────────────────
    clipped = [eid for eid in em.ids()
               if any("[…detail=full]" in str(v)
                      for v in em.compact(eid).values())]
    check("clipped fields are marked […detail=full]", bool(clipped),
          f"{len(clipped)} emitters have a marked clip")
    check("compact view is genuinely smaller than the spec",
          all(len(json.dumps(em.compact(e))) < len(json.dumps(em.spec(e)))
              for e in em.ids()))
    check("hidden_bytes is reported so brevity is never mistaken for completeness",
          em.hidden_bytes() > 10_000, f"{em.hidden_bytes():,}")

    # ── Signal-name hygiene, for the vocabulary landing next ──────────────
    names = [s[0] for s in bp._CODE_SIGNALS]
    check("no duplicate signal names in _CODE_SIGNALS",
          len(names) == len(set(names)),
          str([n for n in names if names.count(n) > 1]))
    check("every signal declares what it argues for",
          all(str(s[3]).strip() for s in bp._CODE_SIGNALS))
    check("the 12 baseline signals are all still present",
          _EXISTING_SIGNALS.issubset(set(names)),
          str(sorted(_EXISTING_SIGNALS - set(names))))

    # ── The compositional vocabulary ───────────────────────────────────────
    import re as _re
    check("the vocabulary loaded (12 baseline + 56)", len(names) >= 68,
          f"{len(names)} signals")
    broken = []
    for entry in bp._CODE_SIGNALS:
        try:
            _re.compile(entry[1], _re.MULTILINE | _re.IGNORECASE)
        except Exception as exc:
            broken.append(f"{entry[0]}: {exc}")
    check("every signal regex compiles", not broken, str(broken[:3]))

    rich = [e for e in bp._CODE_SIGNALS if len(e) > 4]
    check("vocabulary signals carry argues_against",
          all(str(e[4].get("argues_against") or "").strip() for e in rich),
          str([e[0] for e in rich
               if not str(e[4].get("argues_against") or "").strip()][:3]))
    check("vocabulary signals carry false_positive_traps",
          all(str(e[4].get("false_positive_traps") or "").strip() for e in rich))
    check("vocabulary signals declare their languages",
          all(e[4].get("languages") for e in rich))
    check("14 signals declare what they supersede",
          sum(1 for e in rich if e[4].get("supersedes")) >= 14)

    # The blind spots must ship WITH the evidence, or silence reads as absence.
    limits = str(bp._vocab_limits())
    check("known_limits is published to the agent", len(limits) > 500)
    # A limit that has been closed must stop being advertised as open, or the
    # published blind-spot list becomes its own false statement.
    closed = [e[0] for e in bp._CODE_SIGNALS
              if len(e) > 4 and e[4].get("closes_known_limit")]
    # Three of the four closed limits were closed BY a signal; the fourth
    # (extensionless files) was closed by the scanner's _NAME_LANG table, so no
    # signal carries that marker and none should be invented to satisfy a count.
    check("signals that closed a published limit say so", len(closed) >= 3,
          str(closed))
    check("...and the limits text marks them CLOSED",
          limits.count("CLOSED") >= 4, str(limits.count("CLOSED")))
    for gone in ("GPU work driven from Python (cupy",
                 "MOBILE UI IS INVISIBLE.",
                 "(3) EXTENSIONLESS FILES."):
        check(f"stale limit removed: {gone[:34]!r}", gone not in limits)
    check("the still-open limits are stated plainly",
          "STILL OPEN" in limits)

    # The polyglot regression: a plurality of .py must not hide the browser half.
    poly = Path(tempfile.mkdtemp(prefix="av-poly-"))
    (poly / "a.py").write_text("import logging\n")
    (poly / "b.py").write_text("x = 1\n")
    (poly / "c.py").write_text("y = 2\n")
    (poly / "ui.tsx").write_text(
        "export default () => <input onKeyDown={e => console.log(e)} />;\n")
    ev = bp.code_signals(str(poly))
    check("a .tsx file is actually opened now",
          ev["scanned_files"] == 4, str(ev["scanned_files"]))
    check("languages_present reports BOTH halves",
          set(ev["languages_present"]) == {"python", "node"},
          str(ev["languages_present"]))
    offered = {o["id"] for o in bp._emitter_options(
        ev["primary_language"], also_present=ev["languages_present"])}
    check("browser_events IS offered despite primary_language=python",
          "browser_events" in offered, str(sorted(offered)))

    # Extensionless launch/schedule files were structurally invisible.
    (poly / "crontab").write_text("0 3 * * * /usr/bin/run\n")
    ev2 = bp.code_signals(str(poly), use_cache=False)
    check("extensionless files (crontab) are scanned",
          ev2["scanned_files"] == 5, str(ev2["scanned_files"]))

    # Truncation must be visible, and a partial scan must never be cached.
    tr = bp.code_signals(str(REPO / "python_backend"), max_total_bytes=200_000,
                         use_cache=False)
    check("a truncated scan says so", tr["scan"]["complete"] is False,
          json.dumps(tr["scan"])[:120])
    check("...and explains that absence is not evidence",
          "do NOT read its absence" in str(tr["scan"].get("so", "")))
    check("scan reports its own duration and byte count",
          "seconds" in tr["scan"] and "bytes_read" in tr["scan"])

    # ── The cache must never serve stale evidence ──────────────────────────
    # A time-to-live would have: edit a file, re-fetch inside the window, and the
    # agent plans against code that no longer exists with no way to notice.
    cdir = Path(tempfile.mkdtemp(prefix="av-cache-"))
    (cdir / "one.py").write_text("import logging\n")
    c1 = bp.code_signals(str(cdir))
    c2 = bp.code_signals(str(cdir))
    check("an unchanged tree is served from cache", c1 is c2)
    (cdir / "two.tsx").write_text("export default () => <div/>;\n")
    c3 = bp.code_signals(str(cdir))
    check("ADDING a file invalidates the cache",
          c3 is not c1 and c3["scanned_files"] == 2,
          str(c3["scanned_files"]))
    (cdir / "one.py").write_text("import logging\nimport os\nx = 1\n")
    c4 = bp.code_signals(str(cdir))
    check("EDITING a file invalidates the cache", c4 is not c3)
    (cdir / "two.tsx").unlink()
    c5 = bp.code_signals(str(cdir))
    check("DELETING a file invalidates the cache",
          c5 is not c4 and c5["scanned_files"] == 1,
          str(c5["scanned_files"]))
    check("a truncated scan is never cached",
          bp.code_signals(str(REPO / "python_backend"),
                          max_total_bytes=200_000)["scan"]["complete"] is False)

    fails = [c for c in checks if not c[1]]
    for name, ok, detail_s in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
        if not ok and detail_s:
            print(f"          {detail_s}")
    print(f"\n{len(checks) - len(fails)}/{len(checks)} checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
