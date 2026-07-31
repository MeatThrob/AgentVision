#!/usr/bin/env python3
"""
The first-connection contract, end to end on a program AgentVision has never seen.

Asserts the whole lifecycle: PROVISIONAL -> catalog -> plan -> BUILT -> never
gated again. And, just as importantly, that the gate cannot be talked past — an
empty plan or one without a catalog token must be refused, because those are
exactly what "AgentVision guessed" looks like from the outside.
"""
import json, os, shutil, sys, tempfile, urllib.request
from pathlib import Path

BR = os.environ.get("AGENTVISION_BRIDGE_URL", "http://127.0.0.1:7771")
ok = bad = 0

# This suite is the ONLY one in run_all_tests.py that needs a LIVE bridge, and it
# used to assume one: with no bridge listening the very first post() raised
# URLError and the aggregator printed `bridge_gate FAIL` under a wall of
# traceback. So a new user who followed SETUP.md and ran the documented
# `python3 run_all_tests.py` got a red suite that meant "you did not start the
# bridge", which is exactly the outcome the aggregator's own _have_deps /
# _have_mcp helpers go out of their way to avoid — its rule is that an
# unavailable precondition is a SKIP, not a failure, because red that means
# "unconfigured" trains people to ignore red. A listening socket is a far heavier
# precondition than a pip install, so it gets the same treatment, loudly: this
# never silently passes, it says what did not run and how to run it.
try:
    urllib.request.urlopen(BR + "/bridge/status", timeout=5).read()
except Exception as _exc:
    print(f"SKIP — no bridge is listening at {BR} ({_exc}).\n"
          f"      The first-connection gate can only be tested against a live\n"
          f"      bridge. Nothing here was verified. To run it:\n"
          f"        python3 python_backend/api/bridge_server.py --no-autocapture\n"
          f"      then re-run this suite (AGENTVISION_BRIDGE_URL overrides the "
          f"address).")
    sys.exit(0)
def check(label, cond, detail=""):
    global ok, bad
    if cond: ok += 1; print(f"  [ok  ] {label}")
    else:    bad += 1; print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))

def get(p):
    return json.loads(urllib.request.urlopen(BR + p, timeout=20).read())
def post(p, body):
    r = urllib.request.Request(BR + p, data=json.dumps(body).encode(),
                               method="POST",
                               headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(r, timeout=30).read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())

# ── a brand-new program with no logging and no history ───────────────────────
work = Path(tempfile.mkdtemp(prefix="av_gate_"))
(work / "svc.py").write_text(
    "import time\n"
    "def handle(r):\n"
    "    try:\n"
    "        return r['payload']\n"
    "    except KeyError:\n"
    "        return None\n"
    "for i in range(3):\n"
    "    handle({}); time.sleep(0.1)\n")
print(f"new program at {work}\n")

prof = {"name": "gatetest", "display_name": "GateTest",
        "project_root": str(work), "process_name": "svc.py",
        "language": "python",
        "action_log_file": str(work / "agentvision" / "actions.jsonl"),
        "log_sources": [{"path": str(work / "agentvision" / "actions.jsonl"),
                         "adapter": "jsonl", "label": "events"}]}
post("/profiles", prof)
r = urllib.request.Request(BR + "/profiles/active",
                           data=json.dumps({"name": "gatetest"}).encode(),
                           method="PUT",
                           headers={"Content-Type": "application/json"})
urllib.request.urlopen(r, timeout=20)

print("1. a program never seen before is PROVISIONAL")
st = get("/bridge/status")
check("state is PROVISIONAL", st["state"] == "PROVISIONAL", st["state"])
check("not sealed", st["sealed"] is False)
check("names what is blocked", "capture/start" in (st.get("blocked") or []),
      str(st.get("blocked")))
check("points at the catalog", st.get("next") == "av_bridge_catalog()")

print("\n2. the HARD gate: capture refuses to start")
c = post("/capture/start", {"interval": 1.0})
check("capture refused", c.get("started") is False and c.get("bridge_required") is True,
      json.dumps(c)[:150])
check("guidance explains WHY AgentVision will not choose",
      "will not" in str(c.get("guidance")), str(c.get("guidance"))[:90])
check("guidance says it happens once",
      "once" in str(c.get("guidance")).lower())

print("\n3. install refuses too — nothing is written into the program on a guess")
i = post("/install", {"project_root": str(work)})
check("install refused", i.get("installed") is False and i.get("bridge_required") is True,
      json.dumps(i)[:140])
# Assert on EMITTER ARTEFACTS, not the folder: reading a profile's output path
# creates that directory as a side effect, which is not the same as installing.
check("no emitter files were written",
      not (work / "sitecustomize.py").exists()
      and not (work / "agentvision" / "manifest.json").exists(),
      str([p.name for p in work.rglob("*") if p.is_file()][:6]))

print("\n4. the gate cannot be talked past")
bad_plans = [
    ({}, "empty plan"),
    ({"emitters": ["lifecycle"], "rationale": "x"}, "no catalog_token"),
    ({"emitters": ["lifecycle"], "rationale": "x", "catalog_token": "deadbeef"},
     "wrong catalog_token"),
]
for plan, why in bad_plans:
    r2 = post("/bridge/commit", {"plan": plan})
    check(f"rejected: {why}", r2.get("sealed") is not True and r2.get("errors"),
          json.dumps(r2)[:120])
tok = get("/bridge/catalog")["catalog_token"]
r2 = post("/bridge/commit", {"plan": {"catalog_token": tok, "emitters": []}})
check("rejected: no rationale", r2.get("sealed") is not True, json.dumps(r2)[:120])
r2 = post("/bridge/commit", {"plan": {"catalog_token": tok, "emitters": [],
                                      "rationale": "looks fine"}})
check("rejected: empty emitters without saying why",
      r2.get("sealed") is not True, json.dumps(r2)[:140])
check("still PROVISIONAL after every bad attempt",
      get("/bridge/status")["state"] == "PROVISIONAL")

print("\n5. the catalog presents OPTIONS, pre-selects nothing")
cat = get("/bridge/catalog")
check("emitters are offered", len(cat["emitters_available"]) >= 3,
      str(len(cat["emitters_available"])))
ids = [e["id"] for e in cat["emitters_available"]]
check("includes swallowed_exceptions for python", "swallowed_exceptions" in ids, str(ids))
check("each option states what it captures",
      all(e.get("captures") for e in cat["emitters_available"]))
check("each option states its cost",
      all(e.get("cost") for e in cat["emitters_available"]))
check("adapters are grouped, not dumped",
      "families" in cat["adapters"] and cat["adapters"]["total"] > 500,
      str(cat["adapters"].get("total")))
check("MCP tool groups are reviewable", len(cat["mcp_tool_groups"]) >= 10,
      str(len(cat["mcp_tool_groups"])))
check("tells the agent what it must decide", len(cat["you_must_decide"]) >= 3)
check("warns adapters cannot read logs that do not exist",
      "do not" in cat["adapters"]["note"] and "emitter" in cat["adapters"]["note"],
      cat["adapters"]["note"][:100])
check("capture interval is the USER's call, not assumed",
      "ASK THE USER" in json.dumps(cat["capture_settings"]))
check("carries a token", bool(cat.get("catalog_token")))
check("scanned the actual CODE, not just the language",
      (cat.get("code_evidence") or {}).get("scanned_files", 0) >= 1,
      str((cat.get("code_evidence") or {}).get("scanned_files")))
sig = (cat.get("code_evidence") or {}).get("signals") or {}
check("found the DISCARDS-ERROR signal in svc.py (except KeyError: return None)",
      "discards_error" in sig, str(list(sig)))
check("and counted the handler itself", "exception_handlers" in sig, str(list(sig)))
check("each signal says what it ARGUES FOR",
      all(v.get("argues_for") for v in sig.values()))
check("each signal cites example files", all(v.get("files") for v in sig.values()))
check("tells the agent NOT to install everything",
      any("not select every" in d or "NOT select every" in d
          for d in (cat.get("do_not") or [])), str(cat.get("do_not"))[:90])
check("requires per-selection reasons", "why" in (cat.get("required_in_plan") or {}))

print("\n5b. lazy plans are refused")
lazy = {"catalog_token": cat["catalog_token"], "rationale": "cover all bases",
        "emitters": [e["id"] for e in cat["emitters_available"]],
        "why": {e["id"]: "might help" for e in cat["emitters_available"]}}
check("installing EVERYTHING is rejected",
      post("/bridge/commit", {"plan": lazy}).get("sealed") is not True)
nowhy = {"catalog_token": cat["catalog_token"], "emitters": ["lifecycle"],
         "rationale": "a headless python service"}
check("a plan with no per-emitter reasons is rejected",
      post("/bridge/commit", {"plan": nowhy}).get("sealed") is not True)

# The tool half of the review. A plan that decided about emitters but never
# looked at the 90 tools is exactly the half-done review this gate exists for.
_base = {"catalog_token": cat["catalog_token"], "emitters": ["lifecycle"],
         "why": {"lifecycle": "no existing_logging signal anywhere in this project"},
         "rationale": "a headless python service with no logging at all"}
check("a plan that never considered the tools is rejected",
      post("/bridge/commit", {"plan": _base}).get("sealed") is not True)
check("naming every tool is rejected as a copy of the catalog",
      post("/bridge/commit", {"plan": dict(_base, tools={
          "primary": [f"av_t{i}" for i in range(30)]})}).get("sealed") is not True)
check("an empty tool list with no explanation is rejected",
      post("/bridge/commit", {"plan": dict(_base, tools={"primary": []})}
           ).get("sealed") is not True)
# Checked in-process, NOT through /bridge/commit: a valid plan would seal this
# profile's bridge and make every later commit test a no-op ("already_sealed").
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge_plan as _bp_direct
_ok_tools, _errs_tools = _bp_direct.validate_plan(
    dict(_base, tools={"primary": [], "note": "reviewed all 12 groups; this program "
                       "is log-only so no visual tool earns a place"}),
    cat["catalog_token"])
check("an empty tool list WITH a reason is allowed", _ok_tools, str(_errs_tools))
check("a bare list instead of an object is rejected",
      not _bp_direct.validate_plan(dict(_base, tools=["av_diagnose"]),
                                   cat["catalog_token"])[0])
check("not_relevant must carry reasons, not just names",
      not _bp_direct.validate_plan(
          dict(_base, tools={"primary": ["av_diagnose"],
                             "not_relevant": ["av_ui_tree"]}),
          cat["catalog_token"])[0])

print("\n6. a real plan seals AND builds")
plan = {"catalog_token": cat["catalog_token"],
        "emitters": ["lifecycle", "swallowed_exceptions"],
        "adapters": {"events": "jsonl"},
        "capture": {"interval_seconds": 1.0},
        "visual_capture": False,
        "rationale": ("headless python service whose only error path is a broad "
                      "`except KeyError` returning None — swallowed exceptions ARE "
                      "the failure mode; no GUI signal so visual capture is off"),
        "why": {"swallowed_exceptions": ("discards_error signal in svc.py — the "
                                        "KeyError is caught and turned into None, "
                                        "so nothing else would ever report it"),
                "lifecycle": ("no existing_logging signal at all, so run "
                              "boundaries are the only way to know a run happened")},
        "tools": {"primary": ["av_diagnose", "av_log_raw", "av_search",
                              "av_session_report"],
                  "not_relevant": {"av_ui_tree": "headless — no accessibility tree",
                                   "av_visual_changes": "visual capture is off"}}}
res = post("/bridge/commit", {"plan": plan})
check("committed", res.get("ok") is True and res.get("sealed") is True,
      json.dumps(res)[:160])
check("only the SELECTED emitters recorded",
      res["plan"]["emitters"] == plan["emitters"], str(res["plan"]["emitters"]))
check("rationale persisted for audit", "swallowed" in res["plan"]["rationale"])
check("per-selection reasons persisted too",
      set(res["plan"]["why"]) == set(plan["emitters"]), str(res["plan"].get("why")))
check("decided_by is the agent", res["plan"]["decided_by"] == "agent")
check("it actually built into the program", (work / "agentvision").exists(),
      str(list(work.iterdir())))
check("reports what it did", bool(res["built"].get("actions")),
      str(res["built"]))

print("\n7. now BUILT — and never gated again")
st = get("/bridge/status")
check("state is BUILT", st["state"] == "BUILT", st["state"])
# This plan set visual_capture=False (headless service), and that decision is now
# ENFORCED rather than merely recorded — capture used to run anyway, burning disk
# on screenshots the plan had already ruled out. So the correct assertion is that
# the bridge gate no longer blocks, while the agent's own visual decision does.
c = post("/capture/start", {"interval": 1.0})
check("the BRIDGE gate no longer blocks capture",
      c.get("bridge_required") is not True, json.dumps(c)[:140])
check("the plan's visual_capture=False IS honoured",
      c.get("error") == "VISUAL_CAPTURE_DISABLED_BY_PLAN", json.dumps(c)[:140])
c2 = post("/capture/start", {"interval": 1.0, "force": True})
check("force=true overrides the agent's own visual decision",
      c2.get("started") is True, json.dumps(c2)[:140])
check("an explicit interval beats the plan's",
      c2.get("interval") == 1.0 and c2.get("interval_source") == "request",
      f"{c2.get('interval')} from {c2.get('interval_source')}")
post("/capture/stop", {})
again = post("/bridge/commit", {"plan": plan})
check("a second commit is a no-op, not a re-gate",
      again.get("already_sealed") is True, json.dumps(again)[:120])
check("replan=true is the explicit way to re-decide",
      post("/bridge/commit", {"plan": plan, "replan": True}).get("sealed") is True)

print("\n8. the plan's adapter PINS actually wire the sources they name")
# Measured on a purpose-built rig, all three on a plan that was accepted as valid:
#   * adapters={"<discovered label>": "<a custom adapter just added for it>"} sealed
#     ok=true and registered only "events -> jsonl, text -> auto" — the pin was
#     dropped and the FATAL-filled log it named was never registered at all;
#   * the same pin with emitters: [] ("already logs well" — the conclusion the
#     catalog's own log discovery exists to make possible) sealed BUILT with
#     log_sources == [], i.e. a flight recorder wired to nothing, permanently,
#     with every state surface reporting success;
#   * a pin naming an adapter that does not exist validated clean and then parsed
#     with `raw` while reporting the name that never resolved.
# The catalog was taught to FIND undeclared logs and the validator to reject a
# mis-keyed pin, but nothing connected either to what the bridge ends up reading.


def _mkprog(tag: str, with_log: bool):
    w = Path(tempfile.mkdtemp(prefix=f"av_wire_{tag}_"))
    (w / "app.py").write_text("import tkinter as tk\ntk.Tk().mainloop()\n")
    if with_log:
        (w / "log").mkdir()
        (w / "log" / "widgetd.log").write_text(
            "<~ INFO ~ widget.core ~ widgetd starting ~>\n"
            "<~ FATAL ~ widget.core ~ could not seat the flange ~>\n"
            "<~ FATAL ~ widget.seat ~ retry 1 of 3 failed ~>\n")
        # A SECOND log in a format that IS covered (stdlib logging, 1.00). Both
        # files must be graded, and graded DIFFERENTLY — see the covered/uncovered
        # checks below for the bug that made this discrimination necessary.
        (w / "log" / "audit.log").write_text(
            "2024-01-02 03:04:05,123 INFO  root: audit opened\n"
            "2024-01-02 03:04:06,001 ERROR root: audit write failed\n")
    post("/profiles", {"name": f"wiretest{tag}", "display_name": f"WireTest{tag}",
                       "project_root": str(w), "language": "python",
                       "log_sources": []})
    urllib.request.urlopen(urllib.request.Request(
        BR + "/profiles/active", data=json.dumps({"name": f"wiretest{tag}"}).encode(),
        method="PUT", headers={"Content-Type": "application/json"}), timeout=20)
    return w


def _plan(cat_body, **kw):
    p = {"catalog_token": cat_body["catalog_token"], "emitters": ["lifecycle"],
         "why": {"lifecycle": "gui_toolkit signal and nothing marks run boundaries"},
         "rationale": "a small tkinter widget daemon with its own log",
         "visual_capture": True, "capture": {"interval_seconds": 1.0},
         "tools": {"primary": ["av_diagnose", "av_error_moment", "av_search"],
                   "not_relevant": {"av_ocr_frame": "the log carries the text"}}}
    p.update(kw)
    return p


def _declared():
    return {(s.get("label"), s.get("adapter_declared"))
            for s in (get("/bridge/report").get("log_sources") or [])}


w2 = _mkprog("a", with_log=True)
c2 = get("/bridge/catalog")
_found = {e.get("label"): e for e in (c2.get("existing_logs_found") or [])}
check("catalog finds the UNDECLARED log already in the project",
      "widgetd" in _found, str(list(_found)))
check("and says no adapter covers its format",
      _found.get("widgetd", {}).get("covered") is False,
      str(_found.get("widgetd", {}).get("detected_adapter")))
# The coverage probe called detect_adapter() with a single STRING instead of the
# list of lines it takes, so every adapter scored the input CHARACTER by
# character: the answer was `structural` 0.02 for any input whatsoever. Every
# discovered log was therefore reported as "NO adapter specifically parses this
# format", with a recipe for writing a redundant custom one — the wild-goose chase
# the probe exists to PREVENT. Only a log that IS covered can catch this, so the
# fixture ships one on purpose.
check("a COVERED format is graded covered (probe is not stuck on 'uncovered')",
      _found.get("audit", {}).get("covered") is True,
      f"audit.log -> {_found.get('audit', {}).get('detected_adapter')} "
      f"@ {_found.get('audit', {}).get('detect_confidence')}")
check("and the right adapter is named for it",
      _found.get("audit", {}).get("detected_adapter") == "python_logging",
      str(_found.get("audit", {}).get("detected_adapter")))
check("an uncovered format gets the copy-paste adapter recipe",
      "how_to_add_an_adapter" in _found.get("widgetd", {}))
# One sample line is not enough to write an adapter FOR A FORMAT, only for that
# line — a cold model handed one line anchored on its message text and shipped an
# adapter that matched 1 line in 4. The recipe therefore ships extra real lines,
# and av_add_adapter enforces them.
_recipe = (_found.get("widgetd", {}).get("how_to_add_an_adapter") or {})
check("the recipe hands over MORE real lines than the one sample",
      len((_recipe.get("example_body") or {}).get("also_match") or []) >= 1,
      str((_recipe.get("example_body") or {}).get("also_match")))
check("and warns anchors must not come from a message body",
      any("never a word from a message" in r or "NOT a word from one message" in r
          or "not a word from one message" in r
          for r in (_recipe.get("rules") or [])
          ) or "NOT a word from one message" in json.dumps(_recipe),
      json.dumps(_recipe.get("rules"))[:200])
check("a covered format does NOT get told to write an adapter",
      "how_to_add_an_adapter" not in _found.get("audit", {}),
      str(list(_found.get("audit", {}))))
check("its label is offered as a pinnable label",
      "widgetd" in (c2.get("adapter_pin_labels") or []),
      str(c2.get("adapter_pin_labels")))

# A pin naming a non-existent adapter must not seal.
r8 = post("/bridge/commit", {"plan": _plan(c2, adapters={"widgetd": "no_such_ad_xyz"})})
check("a pin naming an unregistered adapter is refused",
      r8.get("sealed") is not True
      and any("not a registered adapter" in e for e in (r8.get("errors") or [])),
      json.dumps(r8)[:150])

# The "already logs well" plan: no emitters, pin the logs that already exist. One
# pinned BY NAME (so the named pin is proved to run) and one on "auto".
# Deliberately a built-in adapter, not one added here: av_add_adapter persists to a
# global store with no remove route, so a suite that adds an adapter permanently
# alters the shipped registry.
# NOTE the rationale below. `widgetd` is a format no adapter specifically parses,
# and the adapter-coverage gate now REFUSES a plan that pins "auto" over such a
# source without saying so — pinning "auto" and moving on is exactly the silent
# downgrade that gate exists to catch, because the agent would then read
# structurally-parsed lines believing they were properly understood. Acknowledging
# the fallback in the rationale is the intended way through, so this test asserts
# the acknowledgement path rather than the old seal-anyway behaviour.
r8 = post("/bridge/commit", {"plan": _plan(
    c2, emitters=[], why={},
    rationale=("mailerd already logs well: log/audit.log is stdlib logging and "
               "log/widgetd.log carries FATAL lines, so no emitter is needed. "
               "widgetd has no specific adapter — I accept the generic "
               "structural fallback for it, since the FATAL token still lands."),
    adapters={"audit": "python_logging", "widgetd": "auto"})})
check("an emitters=[] plan that pins existing logs SEALS",
      r8.get("sealed") is True, json.dumps(r8)[:200])
check("and BOTH pinned logs are actually REGISTERED as sources",
      {("audit", "python_logging"), ("widgetd", "auto")} <= _declared(),
      str(_declared()))
_ev = get("/log/normalized?limit=20").get("events") or []
check("the bridge now READS the named-pin log, and that adapter RAN "
      "(source extracted, not the fallback's own name)",
      any(e.get("level") == "ERROR" and e.get("source") == "root" for e in _ev),
      f"{len(_ev)} events: " + str([(e.get('level'), e.get('source'))
                                    for e in _ev][:6]))
check("and reads the auto-pinned log too",
      any((e.get("data") or {}).get("message", "").find("flange") >= 0
          or "flange" in (e.get("raw") or "") for e in _ev),
      str([(e.get('level'), e.get('source')) for e in _ev][:6]))

# RE-PINNING on a replan. A replan is exactly when the agent has learned enough to
# choose a better parser. The pin was dropped: an already-declared source no longer
# appears as an undeclared log in the catalog, so it never entered the wiring
# candidates. A cold model hit this, was told "no log source for them exists" about
# a source /bridge/report was listing, and recorded it as a limitation of the tool.
r8 = post("/bridge/commit", {"replan": True, "plan": _plan(
    get("/bridge/catalog"), emitters=[], why={},
    rationale=("mailerd already logs well; re-deciding the audit parser now that "
               "the format is known"),
    adapters={"audit": "structural"})})
check("a replan can RE-PIN an already-declared source",
      r8.get("sealed") is True
      and any("audit" in x for x in ((r8.get("built") or {})
                                     .get("adapters_repinned") or [])),
      json.dumps((r8.get("built") or {}).get("adapters_repinned"))
      + " / unapplied=" + json.dumps((r8.get("built") or {})
                                     .get("adapters_unapplied")))
check("the re-pin is what the profile now declares",
      ("audit", "structural") in _declared(), str(_declared()))
check("and a re-pin is NOT reported as unapplied",
      "audit" not in (((r8.get("built") or {}).get("adapters_unapplied") or {})
                      .get("labels") or []),
      json.dumps((r8.get("built") or {}).get("adapters_unapplied")))

# A plan that leaves nothing readable must not seal — that state is unrecoverable
# by the gate, because the gate never fires again.
w3 = _mkprog("b", with_log=True)
c3 = get("/bridge/catalog")
r8 = post("/bridge/commit", {"plan": _plan(
    c3, emitters=[], why={},
    rationale="widgetd already logs well, nothing to add here")})
check("a plan that would read NOTHING is refused",
      r8.get("error") == "BRIDGE_WOULD_READ_NOTHING", json.dumps(r8)[:160])
check("the refusal names the logs it could have pinned",
      any(x.get("label") == "widgetd" for x in (r8.get("logs_you_could_pin") or [])),
      json.dumps(r8.get("logs_you_could_pin"))[:150])
check("and it stays PROVISIONAL", get("/bridge/status")["state"] == "PROVISIONAL")
# …unless reading nothing is the actual decision, said out loud.
r8 = post("/bridge/commit", {"plan": _plan(
    c3, emitters=[], why={},
    rationale="closed-source widget: visual only, there is no log to read")})
check("a DECLARED visual-only bridge is allowed to have no log sources",
      r8.get("sealed") is True, json.dumps(r8)[:160])
check("a discovered log left unpinned is reported, not passed over silently",
      any(x.get("label") == "widgetd"
          for x in ((r8.get("built") or {}).get("discovered_not_wired") or [])),
      json.dumps((r8.get("built") or {}).get("discovered_not_wired"))[:150])

# restore. ORDER MATTERS: switch the active profile back FIRST, then delete the
# fixtures. `wiretestb` is left ACTIVE by _wire_profile, and deleting the active
# profile is now refused (409) — as it must be, since every later profile lookup
# would fall back to a blank default. The deletes below are wrapped in
# try/except, so with the old ordering the refusal was swallowed and the fixture
# profile leaked into the user's profiles.json (observed).
# "custom" — the neutral placeholder, NOT a real program. This used to name the
# repo owner's own PS5-emulator profile, so the suite could only clean up on his
# machine: once that profile was removed the restore 404'd and every later check
# in this file cascaded.
r = urllib.request.Request(BR + "/profiles/active",
                           data=json.dumps({"name": "custom"}).encode(),
                           method="PUT", headers={"Content-Type": "application/json"})
urllib.request.urlopen(r, timeout=20)

for _t in ("a", "b"):
    try:
        urllib.request.urlopen(urllib.request.Request(
            BR + f"/profiles/wiretest{_t}", method="DELETE"), timeout=20)
    except Exception as _e:
        print(f"  [warn] could not delete wiretest{_t}: {_e}")
shutil.rmtree(w2, ignore_errors=True)
shutil.rmtree(w3, ignore_errors=True)
urllib.request.urlopen(urllib.request.Request(
    BR + "/profiles/gatetest", method="DELETE"), timeout=20)
shutil.rmtree(work, ignore_errors=True)

print(f"\n=== {ok} passed / {bad} failed ===")
sys.exit(1 if bad else 0)
