#!/usr/bin/env python3
"""
Pre-flight FORCE tests — log-coverage check + runtime adapter self-extension.
================================================================================
Proves the final AgentVision feature: before the first bridge start the AI can
verify a program's log/debug-log coverage EXISTS, and — where it doesn't — ADD
the missing adapter at runtime so it persists.

Covered here (stdlib core, runs anywhere):
  • preflight on an all-covered profile           → ready:true
  • preflight on an unknown/custom-format profile  → gap listed with its fallback
  • av_add_adapter round-trip: gap → add(persist) → preflight ready:true
  • av_add_adapter that collides with an existing adapter → rejected
  • user_adapters PERSIST across a fresh import (subprocess re-import)
  • registry integrity with a user adapter loaded (tail structural,raw; no dups)

Plus (only when flask is importable) the FORCE at the route layer:
  • first /capture/start for a fresh profile → preflight_required (not started)
  • /capture/start with force=true            → starts (marker written)
  • a second /capture/start (marker present)  → starts without force
  • /preflight and /adapter/add routes end-to-end

Run:  python3 python_backend/connectors/test_preflight.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve()
for _p in (_HERE.parents[1], _HERE.parents[2]):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from connectors import log_sources as ls           # noqa: E402
from connectors import log_adapters as la           # noqa: E402
from connectors.adapters import user_adapters as ua  # noqa: E402

_PASS = _FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
    print(f"  [{'ok ' if cond else 'FAIL'}] {name}{'' if cond or not detail else '  — ' + detail}")


class _Prof:
    """Minimal ProgramProfile stand-in (stdlib, no dataclass needed)."""
    def __init__(self, **kw):
        self.name = kw.get("name", "t")
        self.language = kw.get("language", "")
        self.project_root = kw.get("project_root", "")
        self.log_sources = kw.get("log_sources", [])
        self.action_log_file = kw.get("action_log_file", "")
        self.log_file = kw.get("log_file", "")


# A distinctive custom format that no built-in adapter parses — it only routes to
# the structural fallback. This is the "debug-log type AgentVision lacks".
_GAP_LINES = (
    "GAMELOG||2026-07-21T10:00:00.100||INFO||engine::boot||subsystems online\n"
    "GAMELOG||2026-07-21T10:00:01.200||WARN||engine::gpu||shader recompile\n"
    "GAMELOG||2026-07-21T10:00:02.300||ERROR||engine::gpu||device lost\n"
)
_GAP_SAMPLE = "GAMELOG||2026-07-21T10:00:00.100||INFO||engine::boot||subsystems online"
_GAP_ADAPTER = "gamelog_pipe_test"


def _gap_spec() -> dict:
    return {
        "name": _GAP_ADAPTER,
        "family": "engine",
        "language": "cpp",
        "detect": {"regex": r"^GAMELOG\|\|", "anchor_tokens": ["GAMELOG||"]},
        "extract": {"regex": (r"^GAMELOG\|\|(?P<ts>[^|]+)\|\|(?P<level>[A-Z]+)\|\|"
                              r"(?P<source>[^|]+)\|\|(?P<message>.*)$")},
        "sample": _GAP_SAMPLE,
    }


# ── 1. covered profile ─────────────────────────────────────────────────────────

def test_covered_ready():
    print("1. preflight on an all-covered profile → ready:true")
    d = tempfile.mkdtemp()
    text = os.path.join(d, "app.log")
    open(text, "w").write(
        "2026-07-21 10:00:00,100 INFO mymod: started ok\n"
        "2026-07-21 10:00:01,200 ERROR mymod: boom\n")
    js = os.path.join(d, "events.jsonl")
    open(js, "w").write(
        '{"ts_ms":1784628000000,"category":"event","source":"a","data":{"name":"tick"}}\n')
    prof = _Prof(name="cov", language="python", project_root=d,
                 log_sources=[{"path": text, "adapter": "auto", "label": "app"},
                              {"path": js, "adapter": "jsonl", "label": "events"}])
    r = ls.preflight_report(prof)
    check("ready True", r["ready"] is True, str(r["gaps"]))
    check("no gaps", not r["gaps"], str(r["gaps"]))
    check("app→python_logging covered",
          any(c["source"] == "app" and c["adapter"] == "python_logging"
              for c in r["covered"]), str(r["covered"]))
    check("language detected python", r["language"] == "python", str(r["language"]))
    check("language adapters listed", len(r["language_specific_adapters_available"]) > 0)


# ── 2. unknown/custom-format profile ────────────────────────────────────────────

def test_gap_detected():
    print("2. preflight on an unknown/custom-format profile → gap listed")
    d = tempfile.mkdtemp()
    gap = os.path.join(d, "game.log")
    open(gap, "w").write(_GAP_LINES)
    prof = _Prof(name="gap", language="cpp", project_root=d,
                 log_sources=[{"path": gap, "adapter": "auto", "label": "game"}])
    r = ls.preflight_report(prof)
    check("ready False", r["ready"] is False)
    check("one gap", len(r["gaps"]) == 1, str(r["gaps"]))
    g = r["gaps"][0] if r["gaps"] else {}
    check("gap source is game", g.get("source_or_format") == "game", str(g))
    check("fallback is structural/generic_ts/raw",
          g.get("current_fallback") in ls.FALLBACK_ADAPTERS, str(g))
    check("gap carries a sample", bool(g.get("sample")), str(g))
    check("recommended_actions mention av_add_adapter",
          any("av_add_adapter" in a for a in r["recommended_actions"]))


# ── 3. round-trip: gap → add(persist) → ready ───────────────────────────────────

def test_add_adapter_roundtrip():
    print("3. av_add_adapter round-trip: gap → add(persist) → preflight ready:true")
    d = tempfile.mkdtemp()
    gap = os.path.join(d, "game.log")
    open(gap, "w").write(_GAP_LINES)
    prof = _Prof(name="rt", language="cpp", project_root=d,
                 log_sources=[{"path": gap, "adapter": "auto", "label": "game"}])
    # before: gap
    check("pre: not ready", ls.preflight_report(prof)["ready"] is False)
    # add + persist
    res = ua.add_adapter(_gap_spec(), persist=True)
    check("add ok", res["ok"] is True, str(res["errors"]))
    check("registered live", res["registered"] is True)
    check("persisted", res["persisted"] is True)
    check("adapter in live REGISTRY",
          any(a.name == _GAP_ADAPTER for a in la.REGISTRY))
    check("store file contains spec",
          any(s.get("name") == _GAP_ADAPTER for s in ua.list_specs()))
    # after: ready
    r2 = ls.preflight_report(prof)
    check("post: ready True", r2["ready"] is True, str(r2["gaps"]))
    check("game now covered by gamelog adapter",
          any(c["source"] == "game" and c["adapter"] == _GAP_ADAPTER
              for c in r2["covered"]), str(r2["covered"]))
    # and it actually parses the level correctly
    ev = la.get_adapter(_GAP_ADAPTER).parse_line(
        "GAMELOG||2026-07-21T10:00:02.300||ERROR||engine::gpu||device lost")
    check("adapter parses level ERROR", ev and ev["level"] == "ERROR", str(ev))
    check("adapter parses source", ev and ev["source"] == "engine::gpu", str(ev))
    check("adapter parses message", ev and ev["data"]["message"] == "device lost", str(ev))


# ── 4. collision → rejected ─────────────────────────────────────────────────────

def test_collision_rejected():
    print("4. av_add_adapter that collides with an existing adapter → rejected")
    # (a) a spec whose sample is already owned by python_logging → self-route reject
    stealer = {
        "name": "py_stealer_test",
        "extract": {"regex": r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+)\s+"
                             r"(?P<level>\w+)\s+(?P<message>.*)$"},
        "sample": "2026-07-21 10:00:00,123 INFO mymod: started ok",
    }
    res = ua.add_adapter(stealer, persist=False)
    check("stealer rejected", res["ok"] is False)
    check("stealer not registered", not any(a.name == "py_stealer_test" for a in la.REGISTRY))
    check("stealer error explains", bool(res["errors"]), str(res))

    # (b) a catch-all `.+` adapter — wins its own novel sample but its loose detect
    # STEALS catalog samples from named adapters → collision-guard reject. Only
    # assertable when the catalog is present (it ships in docs/).
    has_catalog = bool(ua._catalog_samples())
    catchall = {
        "name": "catchall_test",
        "extract": {"regex": r"^(?P<message>.+)$"},
        "sample": "zzz totally novel line that nothing else owns 42",
    }
    res2 = ua.add_adapter(catchall, persist=False)
    if has_catalog:
        check("catch-all rejected", res2["ok"] is False)
        check("catch-all reports collisions", len(res2["collisions"]) > 0,
              str(res2.get("collisions"))[:200])
        check("catch-all not registered",
              not any(a.name == "catchall_test" for a in la.REGISTRY))
    else:
        print("     (catalog absent — catch-all collision assertion skipped)")


# ── 5. persist across a fresh import ────────────────────────────────────────────

def test_persist_fresh_import():
    print("5. user_adapters PERSIST across a fresh import (subprocess)")
    # test 3 persisted _GAP_ADAPTER to the store; a brand-new interpreter must
    # load it from user_adapters.json into the REGISTRY on import.
    code = (
        "from connectors import log_adapters as la;"
        "print('FOUND' if any(a.name==%r for a in la.REGISTRY) else 'MISSING');"
        "names=[a.name for a in la.REGISTRY];"
        "print('TAIL', names[-2], names[-1]);"
        "print('DUPS', len(names)-len(set(names)))" % _GAP_ADAPTER
    )
    proc = subprocess.run([sys.executable, "-c", code],
                          cwd=str(_HERE.parents[1]),   # python_backend
                          capture_output=True, text=True, timeout=60)
    out = proc.stdout
    check("fresh import found persisted adapter", "FOUND" in out, out + proc.stderr)
    check("fresh import tail is structural,raw", "TAIL structural raw" in out, out)
    check("fresh import no dups", "DUPS 0" in out, out)


# ── 6. registry integrity with user adapter loaded ──────────────────────────────

def test_registry_integrity():
    print("6. registry integrity with a user adapter loaded")
    names = [a.name for a in la.REGISTRY]
    check("tail is (structural, raw)", names[-2:] == ["structural", "raw"], str(names[-2:]))
    dups = sorted({n for n in names if names.count(n) > 1})
    check("no duplicate adapter names", not dups, str(dups))
    check("user adapter present but not in tail", _GAP_ADAPTER in names[:-2])


# ── 7. route-layer FORCE (flask only) ───────────────────────────────────────────

def test_routes_force():
    try:
        import flask  # noqa: F401
    except Exception:
        print("7. route FORCE — SKIP (flask not importable; run in a venv)")
        return

    print("7. route-layer FORCE: preflight_required → force starts → marker sticks")
    from connectors.program_connector import (ProgramProfile, save_profiles,
                                              load_profiles, BUILTIN_PROFILES)
    work = tempfile.mkdtemp()
    text = os.path.join(work, "app.log")
    open(text, "w").write("2026-07-21 10:00:00,100 INFO mymod: started ok\n")
    p = load_profiles()
    p["pf_rt"] = ProgramProfile(
        name="pf_rt", display_name="PreflightRT", project_root=work,
        process_name="nonexistent_xyz", language="python",
        log_sources=[{"path": text, "adapter": "auto", "label": "app"}])
    save_profiles({k: v for k, v in p.items()
                   if k not in BUILTIN_PROFILES or k == "pf_rt"})
    os.environ["AGENTVISION_ACTIVE_PROFILE"] = "pf_rt"

    import api.bridge_server as bs
    bs._active_profile_name = "pf_rt"
    bs._collector = None
    # ensure a clean slate: no marker yet for this profile
    prof = bs._active_profile_obj()
    marker = bs._preflight_marker_path(prof)
    try:
        if marker.exists():
            marker.unlink()
    except Exception:
        pass
    client = bs.app.test_client()

    # TWO gates now stand in front of a first capture, in this order:
    #   1. BRIDGE  — has the agent reviewed the catalog and chosen what to build?
    #   2. PREFLIGHT — can AgentVision parse the logs that resulted?
    # The bridge gate is deliberately first: "which logs should exist" precedes
    # "can we read them". This test is about gate 2, so seal gate 1 first — its
    # own behaviour is covered end-to-end in the bridge-gate lifecycle test.
    import bridge_plan as _bp
    _plan_dir = bs._plan_folder(prof)
    r0 = client.post("/capture/start", json={})
    check("bridge gate fires FIRST on a never-bridged program",
          r0.get_json().get("bridge_required") is True,
          str(r0.get_json())[:120])
    _cat = bs._bridge_catalog_body()
    _bp.write_plan(_plan_dir,
                   {"emitters": ["lifecycle"], "rationale": "test fixture",
                    "catalog_token": _cat["catalog_token"]},
                   catalog_token_value=_cat["catalog_token"])
    # Sealing writes the preflight marker too (the plan supersedes it), so remove
    # it again — gate 2 is what this test is here to exercise.
    try:
        if marker.exists():
            marker.unlink()
    except Exception:
        pass

    # first start, no marker, no force → preflight_required, NOT started
    r = client.post("/capture/start", json={})
    d = r.get_json()
    check("first start blocked", d.get("started") is False and d.get("preflight_required") is True,
          str(d)[:200])
    check("preflight verdict attached", isinstance(d.get("preflight"), dict)
          and "ready" in d["preflight"], str(d.get("preflight"))[:120])

    # /preflight route returns a verdict (this profile is fully covered → ready)
    r = client.post("/preflight", json={})
    d = r.get_json()
    check("/preflight ready True (covered profile)", d.get("ready") is True, str(d.get("gaps")))
    check("/preflight wrote marker", d.get("marker_written") is True)
    check("marker file exists", marker.exists())

    # now capture/start proceeds (marker present) with no force
    r = client.post("/capture/start", json={})
    d = r.get_json()
    check("start proceeds after preflight", d.get("started") is True, str(d)[:200])
    bs._auto_engine.stop()

    # /status surfaces the preflight hint
    r = client.get("/status")
    d = r.get_json()
    check("/status has preflight hint", isinstance(d.get("preflight"), dict)
          and d["preflight"].get("ok") is True, str(d.get("preflight"))[:160])

    # force path on a DIFFERENT fresh profile (no preflight run) still starts —
    # this is the GUI Start-Bridge behavior.
    work2 = tempfile.mkdtemp()
    gap2 = os.path.join(work2, "weird.log")
    open(gap2, "w").write(_GAP_LINES.replace(_GAP_ADAPTER, "x"))  # unknown fmt
    p = load_profiles()
    p["pf_force"] = ProgramProfile(
        name="pf_force", display_name="ForceRT", project_root=work2,
        process_name="nonexistent_xyz",
        log_sources=[{"path": gap2, "adapter": "auto", "label": "weird"}])
    save_profiles({k: v for k, v in p.items()
                   if k not in BUILTIN_PROFILES or k in ("pf_rt", "pf_force")})
    bs._active_profile_name = "pf_force"
    os.environ["AGENTVISION_ACTIVE_PROFILE"] = "pf_force"
    prof2 = bs._active_profile_obj()
    m2 = bs._preflight_marker_path(prof2)
    try:
        if m2.exists():
            m2.unlink()
    except Exception:
        pass
    r = client.post("/capture/start", json={"force": True})
    d = r.get_json()
    check("GUI force path starts despite gap", d.get("started") is True, str(d)[:200])
    check("force wrote marker", m2.exists())
    bs._auto_engine.stop()

    # /adapter/add route end-to-end (reject a bad spec cleanly)
    r = client.post("/adapter/add", json={"name": "bad", "extract": {"regex": "("}})
    check("/adapter/add rejects bad regex with 422", r.status_code == 422, str(r.status_code))
    check("/adapter/add returns errors", bool((r.get_json() or {}).get("errors")))

    # cleanup: remove the throwaway profiles from profiles.json
    p = load_profiles()
    save_profiles({k: v for k, v in p.items()
                   if k not in ("pf_rt", "pf_force") and k not in BUILTIN_PROFILES})


def main() -> int:
    # Preserve the real user-adapter store; tests write to it and must not leave
    # residue that would perturb other suites (e.g. realworld_collisions).
    store = ua._STORE
    backup = store.read_bytes() if store.exists() else None
    try:
        test_covered_ready()
        test_gap_detected()
        test_add_adapter_roundtrip()
        test_collision_rejected()
        test_persist_fresh_import()
        test_registry_integrity()
        test_routes_force()
    finally:
        try:
            if backup is None:
                if store.exists():
                    store.unlink()
            else:
                store.write_bytes(backup)
        except Exception as e:
            print(f"  [warn] could not restore user-adapter store: {e}")

    print(f"\n{'=' * 60}\npreflight tests: {_PASS} passed, {_FAIL} failed\n{'=' * 60}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
