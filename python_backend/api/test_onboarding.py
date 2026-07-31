"""A cold model must be able to onboard from the always-visible text alone.

Every check here corresponds to a real defect found while testing a naive
first-run:

  * `_SERVER_INSTRUCTIONS` — the only text a model sees without opening a file —
    described the OLD flow and never mentioned the bridge gate at all, so a fresh
    model went straight to av_capture_start and hit an unexplained refusal.
  * /start_here returned a static `recommended_workflow` that told the agent to
    run av_preflight "before the first capture" while `bridge_build` in the SAME
    response said capture would be refused until a plan was committed. A
    literal-minded model follows the numbered list.
  * The refusal is HTTP 200, so a model checking only the status code reads it as
    success. It must carry an unmistakable in-body discriminator.

These are onboarding invariants, not style preferences: if one breaks, the
program stops being usable by a model that has never seen it.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

BASE = os.environ.get("AGENTVISION_BRIDGE_URL", "http://127.0.0.1:7771")
FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


def get(path: str):
    try:
        with urllib.request.urlopen(BASE + path, timeout=30) as r:
            return json.loads(r.read().decode())
    except Exception as exc:
        return {"__error__": str(exc)}


def main() -> int:
    print("1. the always-visible server instructions")
    import api.claude_mcp as m
    si = m._SERVER_INSTRUCTIONS
    low = si.lower()

    # The gate is the single biggest surprise for a new model. If the instructions
    # do not name it, nothing else will before the agent hits a refusal.
    check("names the bridge gate", "bridge" in low)
    check("says the AGENT decides, not AgentVision",
          "you build the bridge" in low or "you do" in low)
    check("names av_bridge_catalog", "av_bridge_catalog" in si)
    check("names av_bridge_commit", "av_bridge_commit" in si)
    check("names av_bridge_status", "av_bridge_status" in si)
    check("warns capture is REFUSED until planned",
          "refus" in low, "no mention of refusal")
    # The HTTP-200 refusal is the trap a weak model actually falls into.
    check("warns the refusal is HTTP 200 / check the body",
          "200" in si and ("body" in low or "started" in low))
    check("states it happens ONCE per program",
          "once" in low and ("per program" in low or "ever" in low))
    check("states restarting does NOT re-trigger it",
          "restart" in low, "nothing about restarts")
    check("lists the required plan fields",
          all(k in si for k in ("catalog_token", "emitters", "rationale", "tools")))
    check("still teaches the token rule", "av_visual_changes" in si)
    check("points at the full guide", "AI_START_HERE" in si)
    # Long enough to be complete, short enough to survive in every context window.
    check("is a sane length for every context window",
          1500 < len(si) < 6000, f"{len(si)} chars")

    print("\n2. /start_here is self-consistent and state-aware")
    sh = get("/start_here")
    if sh.get("__error__"):
        print(f"  SKIP bridge not reachable at {BASE} — {sh['__error__']}")
        print("       (start it: .venv/bin/python python_backend/api/bridge_server.py)")
    else:
        check("has an unmissable DO_THIS_NEXT", bool(sh.get("DO_THIS_NEXT")),
              str(sh.get("DO_THIS_NEXT")))
        check("has read_this_first explaining the inversion",
              "you do" in (sh.get("read_this_first") or "").lower(),
              str(sh.get("read_this_first"))[:80])
        wf = sh.get("recommended_workflow") or []
        joined = " ".join(wf)
        bh = (sh.get("state") or {}).get("bridge_build") or {}
        built = bool(bh.get("ok"))

        # The contradiction that made the old response unusable.
        if built:
            check("BUILT: workflow says setup is done",
                  "BUILT" in joined or "do NOT plan again" in joined, joined[:90])
            check("BUILT: does not tell the agent to plan again",
                  "av_bridge_commit" not in joined, joined[:90])
            check("BUILT: DO_THIS_NEXT is not the planning sequence",
                  "av_bridge_catalog" not in (sh.get("DO_THIS_NEXT") or ""),
                  str(sh.get("DO_THIS_NEXT")))
        else:
            check("PROVISIONAL: workflow leads with the gate",
                  "NEVER BEEN BRIDGED" in joined or "av_bridge_status" in joined,
                  joined[:90])
            check("PROVISIONAL: workflow names catalog AND commit",
                  "av_bridge_catalog" in joined and "av_bridge_commit" in joined)
            check("PROVISIONAL: does NOT tell the agent to preflight first",
                  "av_preflight" not in joined,
                  "still teaches the superseded preflight-first flow")
            check("PROVISIONAL: DO_THIS_NEXT is the planning sequence",
                  "av_bridge_catalog" in (sh.get("DO_THIS_NEXT") or ""),
                  str(sh.get("DO_THIS_NEXT")))
        # Match on a phrase unique to each branch. A bare "BUILT" substring is
        # useless here: the PROVISIONAL text legitimately contains "(not BUILT)".
        says_done = "Setup is done" in joined
        says_gate = "NEVER BEEN BRIDGED" in joined
        check("workflow agrees with bridge_build state",
              (says_done and not says_gate) if built else (says_gate and not says_done),
              f"built={built} says_done={says_done} says_gate={says_gate}")

    print("\n3. a refusal is unmistakable in the BODY (it is HTTP 200)")
    st = get("/bridge/status")
    if st.get("__error__"):
        print("  SKIP bridge not reachable")
    elif st.get("sealed"):
        print("  SKIP active profile is already BUILT — cannot observe a refusal.")
        print("       (checked statically instead)")
        src = open(os.path.join(HERE, "bridge_server.py"), encoding="utf-8").read()
        check("refusal carries error=BRIDGE_NOT_BUILT",
              src.count('"error": "BRIDGE_NOT_BUILT"') >= 2,
              "expected on both capture/start and install")
        check("refusal carries DO_THIS_NEXT",
              src.count('"DO_THIS_NEXT": "av_bridge_catalog()') >= 2)
    else:
        req = urllib.request.Request(
            BASE + "/capture/start", method="POST",
            data=json.dumps({}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                code, body = r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            code, body = e.code, json.loads(e.read().decode())
        check("refusal is observable without the status code",
              body.get("ok") is False and body.get("started") is False,
              json.dumps(body)[:100])
        check("refusal has a machine-checkable error key",
              body.get("error") == "BRIDGE_NOT_BUILT", str(body.get("error")))
        check("refusal states the next call",
              "av_bridge_catalog" in (body.get("DO_THIS_NEXT") or ""),
              str(body.get("DO_THIS_NEXT")))
        check("refusal explains it is setup, not breakage",
              "REFUSED" in (body.get("guidance") or ""),
              str(body.get("guidance"))[:80])
        print(f"       (status was {code}; the body is what must carry the signal)")

    print("\n3b. a wrongly-enveloped plan is diagnosed, not mislabelled")
    # Measured on a cold Haiku model driving the HTTP API: sending the plan's
    # fields at the top level produced four confident "field is missing" errors
    # for fields it had demonstrably sent, so it guess-and-checked the envelope.
    src = open(os.path.join(HERE, "bridge_server.py"), encoding="utf-8").read()
    check("commit route detects an unwrapped plan",
          '"error": "PLAN_NOT_WRAPPED"' in src)
    check("unwrapped-plan error says nothing is actually missing",
          "Nothing " in src and "envelope is wrong" in src)
    check("unwrapped-plan error shows the correct envelope",
          '\'{"plan": {"catalog_token"' in src or '{\\"plan\\"' in src
          or 'Send: {"plan"' in src)
    check("unwrapped-plan error points MCP callers at the tool arg",
          "this wrapping is done for you" in src)
    # The plan-field key set must stay in step with what validate_plan requires,
    # or the envelope check silently stops firing for a renamed field.
    import bridge_plan as _bp
    for k in ("catalog_token", "emitters", "why", "rationale", "tools"):
        check(f"envelope check knows about plan.{k}",
              f'"{k}"' in src.split("_PLAN_KEYS")[1][:400]
              if "_PLAN_KEYS" in src else False)

    print("\n3c. the plan's decisions actually TAKE EFFECT (not just recorded)")
    # An adversarial doc review found three plan fields that were collected by the
    # gate and then ignored. A decision the gate demands but never applies is worse
    # than not collecting it, because the saved plan then misdescribes the run.
    check("capture/start honours the plan's visual_capture=False",
          "VISUAL_CAPTURE_DISABLED_BY_PLAN" in src)
    check("the visual_capture gate can be forced through",
          "visual_capture gate skipped" in src or "if not force:" in src)
    check("capture/start applies the plan's interval_seconds",
          'interval_seconds' in src and 'applied_from = "plan"' in src)
    check("the response says WHERE the interval came from",
          '"interval_source"' in src)
    inst = open(os.path.join(os.path.dirname(HERE), "installer.py"),
                encoding="utf-8").read()
    check("InstallReport.to_dict exists so built.install is machine-readable",
          "def to_dict" in inst,
          "without it bridge_server's except-branch always returns prose")
    _td = inst.split("def to_dict")[1].split("def summary")[0] if "def to_dict" in inst else ""
    for k in ("language", "emitter", "counts", "errors", "summary"):
        check(f"install dict exposes {k}", f'"{k}":' in _td)

    print("\n3d. the anti-blanket rule can actually fire")
    # It was dead code: an absolute ">= 6 emitters" threshold, when the largest
    # menu any language offers is 5. A cold model selected 4 of 5 and passed.
    check("threshold is proportional to the menu",
          hasattr(_bp, "blanket_threshold"))
    if hasattr(_bp, "blanket_threshold"):
        for lang in ("python", "node", "java", "c"):
            n = len(_bp._emitter_options(lang))
            t = _bp.blanket_threshold(n)
            check(f"{lang}: {n} offered -> guard fires at {t} (reachable)",
                  t <= n, f"threshold {t} > menu {n} means it can never fire")

    print("\n3e. emitter selection is ENFORCED, not just recorded")
    # Previously plan["emitters"] was stored and ignored: install_all_hooks() armed
    # every hook, so a plan selecting only `lifecycle` still got stdout teeing, a
    # logging handler and the swallowed-exception monitor.
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
    from agent_bootstrap import av_runtime as _rt
    from python_backend import emitters as _em  # noqa: F401  (import shape varies)
    check("runtime reads a hook-selection env var", hasattr(_rt, "_selected_hooks"))
    check("unset selection arms every hook",
          _rt._selected_hooks() == set(_rt._HOOK_IDS))
    os.environ["AGENTVISION_HOOKS"] = "lifecycle"
    try:
        check("a one-hook selection arms exactly that hook",
              _rt._selected_hooks() == {"lifecycle"}, str(_rt._selected_hooks()))
        # A typo must never silently leave a program uninstrumented.
        os.environ["AGENTVISION_HOOKS"] = "nonsense_hook_name"
        check("an unknown hook id falls back to ALL, not none",
              _rt._selected_hooks() == set(_rt._HOOK_IDS), str(_rt._selected_hooks()))
    finally:
        os.environ.pop("AGENTVISION_HOOKS", None)

    print("\n3e2. the token does not punish the agent for doing what it was told")
    # The catalog reports an uncovered format and tells the agent to av_add_adapter.
    # The token digested the WHOLE registry count, so that add invalidated the token
    # the agent was holding: the prescribed catalog -> add_adapter -> commit path
    # ended in a guaranteed stale-token rejection. Measured on a cold run as one of
    # only two failed attempts. It now digests the BUILT-IN count, so a self-added
    # adapter is free while a real change to the option surface still invalidates.
    _cb = {"version": 1, "language_detected": "python",
           "emitters_available": [{"id": "a"}, {"id": "b"}],
           "adapters": {"total": 658, "builtin_total": 655},
           "mcp_tool_groups": {"x": 1, "y": 2}}
    _t0 = _bp.catalog_token(_cb)
    check("adding a USER adapter keeps the token valid",
          _bp.catalog_token(dict(_cb, adapters={"total": 659,
                                                "builtin_total": 655})) == _t0)
    for _lbl, _mut in (("new built-in adapters",
                        {"adapters": {"total": 670, "builtin_total": 666}}),
                       ("a new emitter on offer",
                        {"emitters_available": [{"id": "a"}, {"id": "b"},
                                                {"id": "c"}]}),
                       ("a different language", {"language_detected": "c"}),
                       ("a new tool group",
                        {"mcp_tool_groups": {"x": 1, "y": 2, "z": 3}})):
        check(f"{_lbl} STILL invalidates the token",
              _bp.catalog_token(dict(_cb, **_mut)) != _t0)
    check("the catalog publishes builtin_total for the digest",
          "builtin_total" in _bp._adapter_families())

    print("\n3f. the catalog hands over a fillable plan template")
    # Two cold models each lost an attempt inventing the plan shape. The fourth
    # run, with this template, needed ZERO retries.
    cat = get("/bridge/catalog")
    if cat.get("__error__"):
        print("  SKIP bridge not reachable")
    else:
        t = cat.get("plan_template") or {}
        check("catalog carries plan_template", bool(t))
        check("template's token is the REAL one (copy-paste ready)",
              t.get("catalog_token") == cat.get("catalog_token"),
              f"{t.get('catalog_token')} vs {cat.get('catalog_token')}")
        for k in ("emitters", "why", "rationale", "tools"):
            check(f"template includes required field {k}", k in t)
        # Phrase changed from "AT MOST n" to "HARD CEILING: n of the m offered"
        # after a cold model read the adjacent "most programs need 1-2" as the cap
        # and dropped an emitter its own evidence supported. The assertion is on
        # the NUMBER being inline plus the ceiling/target distinction, not on one
        # exact wording — the wording is allowed to improve.
        _em = json.dumps(t.get("emitters"))
        check("template states the numeric emitter cap inline",
              "HARD CEILING" in _em and any(c.isdigit() for c in _em), str(_em)[:160])
        check("...and separates the ceiling from the typical case",
              "CEILING, not a target" in _em)
        check("catalog states the HTTP envelope",
              "PLAN_NOT_WRAPPED" in (cat.get("if_you_are_calling_over_http") or ""))
        sam = cat.get("select_at_most") or {}
        check("select_at_most gives a number below the reject threshold",
              isinstance(sam.get("emitters"), int)
              and sam["emitters"] < _bp.blanket_threshold(
                  len(cat.get("emitters_available") or [])),
              str(sam))

    print("\n3g. an unparsed log format is DISCOVERED and handed a fix")
    # A cold model spent 14 attempts here. Root causes, all fixed:
    #  - the catalog only echoed DECLARED sources, so a first bridge (nothing
    #    declared yet) reported existing_logs_found: [] with a FATAL-filled log
    #    sitting in the project, and the agent was told to CREATE logging;
    #  - /adapter/add's validator said "spec.extract.regex is required", naming a
    #    nested path that must not exist, which confirmed the wrong body shape.
    from connectors import log_sources as _ls
    check("discovery looks inside the sink folder",
          "SINK_DIRNAME" in open(_ls.__file__, encoding="utf-8").read())
    import tempfile as _tf
    from pathlib import Path as _P
    _d = _P(_tf.mkdtemp())
    for _n in ("requirements.txt", "README.txt", "debug.txt", "app.log"):
        (_d / _n).write_text("x\n")
    (_d / "agentvision").mkdir()
    (_d / "agentvision" / "log.txt").write_text("y\n")
    _found = {os.path.relpath(s["path"], _d) for s in _ls.suggest_log_sources(str(_d))}
    check("discovery finds agentvision/log.txt", "agentvision/log.txt" in _found)
    check("discovery finds a log-named .txt", "debug.txt" in _found)
    # The greedy version of this swept up dependency files and then warned that
    # their "format" was unparsed — noise a weak model would act on.
    check("discovery ignores requirements.txt", "requirements.txt" not in _found)
    check("discovery ignores README.txt", "README.txt" not in _found)

    check("catalog grades format coverage", "_format_coverage" in src)
    check("an uncovered format gets a copy-paste adapter spec",
          "how_to_add_an_adapter" in src)
    check("the adapter route diagnoses a wrapped spec",
          '"error": "SPEC_WRAPPED"' in src)
    # These were grep-over-source checks ("spec.extract.regex is required" not in
    # the file; "if w_score >= own:" in the file). Both are worthless as tests and
    # both misfired for real: a comment QUOTING the old bad message failed the
    # first, and replacing the second's condition with strictly better logic failed
    # it while the behaviour improved. A check on an error message must call the
    # thing and read what comes back.
    from connectors.adapters import user_adapters as _uam
    _RX = r"^<# (?P<level>[A-Z]+) \| (?P<source>[a-z.]+) \| (?P<message>.*) #>$"
    _SAMPLE = "<# FATAL | probe.core | the flange is unseated #>"

    def _errs(spec):
        return " ".join(_uam.validate_spec(spec).get("errors") or [])

    # The measured 13-attempt failure: an error that names a body shape the caller
    # must NOT send. Each of these callers made a DIFFERENT mistake, and the error
    # has to name the one that was actually made — an error describing a mistake
    # the caller did not make argues them away from the real fix.
    check("a misplaced top-level `regex` is diagnosed as misplaced",
          "top-level `regex`" in _errs({"name": "ob1", "regex": _RX,
                                        "sample": _SAMPLE}),
          _errs({"name": "ob1", "regex": _RX, "sample": _SAMPLE})[:120])
    check("`extract` sent as a string says so, not 'required'",
          "STRING" in _errs({"name": "ob2", "extract": _RX, "sample": _SAMPLE}),
          _errs({"name": "ob2", "extract": _RX, "sample": _SAMPLE})[:120])
    check("a near-miss key inside extract is named",
          "`pattern`" in _errs({"name": "ob3", "extract": {"pattern": _RX},
                                "sample": _SAMPLE}),
          _errs({"name": "ob3", "extract": {"pattern": _RX},
                 "sample": _SAMPLE})[:120])
    check("a near-miss key for `sample` is named",
          "`sample_line`" in _errs({"name": "ob4", "extract": {"regex": _RX},
                                    "sample_line": _SAMPLE}),
          _errs({"name": "ob4", "extract": {"regex": _RX},
                 "sample_line": _SAMPLE})[:120])
    # No pattern anywhere is the only case where the generic message is honest.
    check("a genuinely absent pattern still says what is required",
          "extract.regex is required" in _errs({"name": "ob5",
                                                "sample": _SAMPLE}))

    # own_score 1.0 alongside "would_lose_to: 0.3" read as self-contradictory.
    _win = _uam.validate_spec({"name": "ob_win", "extract": {"regex": _RX},
                               "sample": _SAMPLE})["self_route"]
    check("a clear win reports wins_by and NOT would_lose_to",
          "would_lose_to" not in _win and _win.get("wins_by", 0) > 0, str(_win))
    # A TIE is a real loss by default (a new adapter registers last)…
    _tiesample = "2024-01-02 03:04:05 INFO app started"
    _loose = {"name": "ob_tie", "extract": {"regex": "^(?P<message>.*)$"},
              "sample": _tiesample}
    _t1 = _uam.validate_spec(_loose)["self_route"]
    check("a tie with no outrank DOES report would_lose_to",
          "would_lose_to" in _t1, str(_t1))
    # …but not when `outrank` names the incumbent, because placement then decides
    # and the adapter WINS. Reported ok=true + outranks=X + would_lose_to=X at the
    # same time until this was gated on the outrank decision instead of on `>=`.
    _t2 = _uam.validate_spec(dict(_loose, name="ob_tie2",
                                  outrank=_t1["runner_up"]["adapter"]))["self_route"]
    check("a tie WON by outrank does not also claim it would lose",
          "would_lose_to" not in _t2 and "wins_by_placement" in _t2, str(_t2))

    # A spec only had to route its OWN one sample, which is a far weaker claim than
    # "this adapter parses this format". Measured on a cold model given one sample
    # line: it set anchor_tokens to a fragment of that line's MESSAGE, scored 1.00,
    # was accepted, matched 1 of 4 lines, and then read the resulting
    # source="structural" as the tool being broken rather than its adapter.
    _fmt = ["(probe)[INFO]{a.core} :: opened 4 connections",
            "(probe)[FATAL]{a.core} :: handshake refused",
            "(probe)[WARN]{b.spool} :: spool 91% full"]
    _prx = r"^\([a-z]+\)\[(?P<level>[A-Z]+)\]\{(?P<source>[\w.]+)\} :: (?P<message>.*)$"
    _one_line_only = {"name": "ob_anchor", "extract": {"regex": _prx},
                      "detect": {"anchor_tokens": [":: spool"]},
                      "sample": _fmt[2], "also_match": _fmt[:2]}
    _r = _uam.validate_spec(_one_line_only)
    check("an adapter that fits only ONE line is rejected by also_match",
          not _r["ok"] and any("also_match" in e for e in _r["errors"]),
          str(_r["errors"])[:160])
    check("that rejection explains the anchor-from-message cause",
          any("never a word from a message" in e for e in _r["errors"]),
          str(_r["errors"])[:160])
    _r = _uam.validate_spec(dict(_one_line_only, name="ob_anchor2",
                                 detect={"anchor_tokens": [" :: "]}))
    check("the same spec with a STRUCTURAL anchor passes",
          _r["ok"] and _r["self_route"].get("also_match_all_ok") is True,
          str(_r["errors"] or _r["self_route"])[:160])
    check("omitting also_match is still fine (it is additive)",
          _uam.validate_spec({"name": "ob_anchor3", "extract": {"regex": _prx},
                              "sample": _fmt[0]})["ok"])

    print("\n3h. a mis-keyed adapter pin is rejected, not ignored")
    _labels = ["events", "text"]
    _base = {"catalog_token": "T", "emitters": ["lifecycle"],
             "why": {"lifecycle": "nothing marks run boundaries in this project"},
             "rationale": "a small python gui app",
             "tools": {"primary": ["av_diagnose"]}}
    for _bad in ("log", "logfile", "kbapp_custom"):
        _ok, _ = _bp.validate_plan(dict(_base, adapters={_bad: "x"}), "T",
                                   offered=["a"], known_labels=_labels)
        check(f"adapters keyed {_bad!r} is rejected", not _ok)
    for _good in ("text", "events"):
        _ok, _e = _bp.validate_plan(dict(_base, adapters={_good: "auto"}), "T",
                                    offered=["a"], known_labels=_labels)
        check(f"adapters keyed {_good!r} is accepted", _ok, str(_e))
    _ok, _ = _bp.validate_plan(dict(_base), "T", offered=["a"],
                               known_labels=_labels)
    check("omitting adapters entirely is still fine", _ok)

    # The VALUE side of the same defect, which the key-only fix left open: a pin
    # naming an adapter that does not exist validated clean, was written into the
    # profile, and then `_read_source` parsed with `raw` — while still reporting the
    # unresolvable name, so /bridge/report confirmed the mis-wiring was fine.
    _known_ad = ["jsonl", "structural", "raw"]
    _ok, _e = _bp.validate_plan(dict(_base, adapters={"text": "no_such_adapter_xyz"}),
                                "T", offered=["a"], known_labels=_labels,
                                known_adapters=_known_ad)
    check("a pin naming a non-existent adapter is rejected", not _ok, str(_e))
    check("that rejection points at the way to list/add adapters",
          any("av_list_adapters" in x and "av_add_adapter" in x for x in _e), str(_e))
    for _v in ("auto", "jsonl"):
        _ok, _e = _bp.validate_plan(dict(_base, adapters={"text": _v}), "T",
                                    offered=["a"], known_labels=_labels,
                                    known_adapters=_known_ad)
        check(f"a pin of {_v!r} is accepted", _ok, str(_e))
    _ok, _e = _bp.validate_plan(dict(_base, adapters={"text": ""}), "T",
                                offered=["a"], known_labels=_labels,
                                known_adapters=_known_ad)
    check("an empty pin value is rejected", not _ok, str(_e))
    # The reader must never report an adapter it did not use.
    from connectors import log_sources as _lsx
    import tempfile as _tf2
    _p2 = os.path.join(_tf2.mkdtemp(), "x.log")
    open(_p2, "w").write("2024-01-02 03:04:05 INFO app started\n")
    _evs, _res = _lsx._read_source({"path": _p2, "adapter": "no_such_adapter_xyz",
                                    "label": "t"})
    check("an unresolvable pin resolves to raw and SAYS so",
          "raw" in _res and "not registered" in _res, _res)

    print("\n3i. a program that is visibly failing cannot be graded 'healthy'")
    # Measured on SharpEmu: /diagnose returned recent_warnings {count: 180,
    # escalation_reason: "content looks like a failure (ok=false)"} and, in the SAME
    # response, health {score: 100, grade: "healthy", factors: ["no deductions"]}
    # with hypotheses: []. Cause: _detect_failure_records reads ONLY
    # actions.jsonl, while the 180 failures were in the program's TEXT log — so
    # failure detection and warning detection read different data.
    check("a shared text-log failure scanner exists", "_text_log_failures" in src)
    check("health scores that scanner", "text_failures=" in src)
    check("both /digest and /diagnose pass it",
          src.count("text_failures=_txt_fail") >= 2,
          f"only {src.count('text_failures=_txt_fail')} call site(s)")
    check("the all-clear cannot print over a real failure",
          src.index("_txt_fail = _text_log_failures(prof)")
          < src.index("no strong failure signals"),
          "the 'looks healthy' line is computed before the failure scan")
    check("a text-log failure raises a hypothesis",
          "why_not_in_fingerprints" in src)
    # Score the scanner directly: 180 failure-shaped lines must deduct.
    from api import bridge_server as _bs

    class _E:
        window_missing = False
        blank_frame_count = 0

    _hb = _bs._health_block(
        {"program": {"running": True}}, [], [], _E(),
        text_failures={"total": 180, "escalated": 180,
                       "groups": [{"count": 180, "source": "METAL",
                                   "message": "guestgpu.present ok=False",
                                   "escalated": True,
                                   "escalation_reason": "content looks like a "
                                                        "failure (ok=false)"}]})
    check("180 failure-shaped lines drop the score below 'healthy'",
          _hb["score"] < 80 and _hb["grade"] != "healthy",
          f"score {_hb['score']} grade {_hb['grade']}")
    check("the factor names the count and the reason",
          any("180" in f and "ok=false" in f.lower() for f in _hb["factors"]),
          str(_hb["factors"])[:110])
    # And a genuinely clean program must still read healthy — the fix must not
    # simply make everything look broken.
    _clean = _bs._health_block({"program": {"running": True}}, [], [], _E(),
                               text_failures={"total": 0, "groups": []})
    check("a clean program still grades healthy",
          _clean["score"] == 100 and _clean["grade"] == "healthy",
          f"score {_clean['score']}")

    print("\n4. the docs a model is pointed at actually exist")
    docs = os.path.join(os.path.dirname(os.path.dirname(HERE)), "docs")
    for name in ("AI_START_HERE.md", "BRIDGE_PROTOCOL.md",
                 "MCP_TOOLS_REFERENCE.md", "LOGS_AND_EMITTERS.md",
                 "TROUBLESHOOTING.md", "README.md"):
        p = os.path.join(docs, name)
        check(f"docs/{name} exists and is substantial",
              os.path.exists(p) and os.path.getsize(p) > 800,
              f"{os.path.getsize(p) if os.path.exists(p) else 0} bytes")

    print(f"\n{len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
