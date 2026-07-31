#!/usr/bin/env python3
"""
Push Mode route + hook-script contract tests.

`test_ambient.py` proves the engine's logic in isolation; this proves the wiring:
the /ambient route against real synthetic frames, and the actual hook SCRIPT
driven with real hook-shaped JSON on stdin for every wired event.

The hook-script half matters most: it is the code that runs on the user's
critical path on every prompt, so its contract is "never break the session,
never hang, never write to stderr, print nothing unless there is news."

Covers:
  /ambient           silent on a healthy state; alert once a failure exists;
                     delta-suppression; force=1; caps reported; build_ms present
  /ambient/reset     makes it speak again
  /ambient stop_check disabled by default, and reports why
  hook script        exits 0 and prints NOTHING when the bridge is down
                     exits 0 and prints NOTHING on malformed/hostile stdin
                     exits 0 and prints NOTHING for unwired events
                     prints the injection for a wired event
                     honours the Push Mode off switch
                     PostToolUse ignores non-mutating tools
                     never writes to stderr in normal operation
                     Stop never blocks with the default config

Requires flask/psutil/pillow. Run:
    python3 python_backend/api/test_push_routes.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
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


HOOK = _HERE.parent.parent.parent / "tools" / "agentvision_hook.py"
SIZE = (800, 600)


def run_hook(payload: dict, env_over: dict | None = None,
             bridge: str = "http://127.0.0.1:9", timeout: float = 20.0):
    """Run the real hook script with `payload` on stdin. Default bridge URL points
    at a closed port so the default is the bridge-down case."""
    env = dict(os.environ)
    env["AGENTVISION_BRIDGE_URL"] = bridge
    env.pop("AGENTVISION_PUSH_MODE", None)
    env["AGENTVISION_HOOK_TIMEOUT_S"] = "1.0"
    env["AGENTVISION_HOOK_DEADLINE_S"] = "3.0"
    if env_over:
        env.update(env_over)
    p = subprocess.run([sys.executable, str(HOOK)],
                       input=json.dumps(payload), capture_output=True,
                       text=True, env=env, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def main():
    work = tempfile.mkdtemp(prefix="av_pushroutes_")
    av = os.path.join(work, "agentvision")
    os.makedirs(av)
    actions = os.path.join(av, "actions.jsonl")
    base_ms = 1784628000000.0
    with open(actions, "w") as f:
        for i in range(6):
            f.write(json.dumps({
                "ts_ms": base_ms + i * 1000, "ts": "2026-07-21T10:00:00.000Z",
                "category": "event", "level": "INFO", "source": "app",
                "data": {"message": f"tick {i}"}}) + "\n")

    from connectors.program_connector import (ProgramProfile, save_profiles,
                                              load_profiles, BUILTIN_PROFILES)
    p = load_profiles()
    p["pmt"] = ProgramProfile(
        name="pmt", display_name="PushTest", action_log_file=actions,
        project_root=work, process_name="nonexistent_xyz",
        log_sources=[{"path": actions, "adapter": "jsonl", "label": "events"}])
    save_profiles({k: v for k, v in p.items()
                   if k not in BUILTIN_PROFILES or k == "pmt"})

    # Remove this test's profile on exit. Without it the suite PERSISTED "pmt"
    # into the shipped python_backend/profiles.json, so the repo accumulated
    # test-only profiles pointing at long-deleted temp dirs — and they would ship.
    # atexit rather than a finally block so cleanup also runs when a check raises.
    import atexit as _atexit

    def _av_drop_test_profile() -> None:
        try:
            _p = load_profiles()
            if "pmt" in _p:
                del _p["pmt"]
                save_profiles({k: v for k, v in _p.items()
                               if k not in BUILTIN_PROFILES})
        except Exception:
            pass

    _atexit.register(_av_drop_test_profile)
    os.environ["AGENTVISION_ACTIVE_PROFILE"] = "pmt"
    os.environ["AGENTVISION_AMBIENT_STOP_STATE"] = os.path.join(work, "stop.json")

    import api.bridge_server as bs
    from api import ambient as A
    from utils import visual_engine as ve
    A.STOP_STATE_PATH = os.path.join(work, "stop.json")
    bs._active_profile_name = "pmt"
    bs._collector = None
    client = bs.app.test_client()

    def gj(path):
        r = client.get(path)
        return r.status_code, r.get_json()

    from PIL import Image, ImageDraw

    def add_frame(seq, ts_ms, err=None, blank=False, running=False):
        img = os.path.join(av, f"frame_{seq:05d}.png")
        if blank:
            Image.new("RGB", SIZE, (0, 0, 0)).save(img)
        else:
            im = Image.new("RGB", SIZE, (26, 28, 36))
            d = ImageDraw.Draw(im)
            for y in range(0, SIZE[1], 25):
                d.line([(0, y), (SIZE[0], y)], fill=(80, 86, 100))
            d.rectangle([20, 20, 20 + (seq * 7) % 500, 90], fill=(210, 215, 225))
            im.save(img)
        res = ve.analyze_with_health(img)
        vis = res["visual"]; vis.pop("_grid", None)
        fr = {"sequence": seq, "timestamp_ms": ts_ms,
              "timestamp": "2026-07-21T10:00:00.000Z",
              "annotated_image": img, "json_sidecar": img.replace(".png", "_f.json"),
              "program": {"running": running},
              "capture_meta": {"shutter_ms": ts_ms, "visual": vis,
                               "black_frame": bool(res["health"].get("is_blank")),
                               "window_found": True},
              "state_delta": {}, "error": err or {}}
        with bs._lock:
            bs._frames[seq] = fr
            bs._latest_frame = fr
        return fr

    # ── /ambient: silent on a healthy-looking state ─────────────────────────
    print("/ambient — silent by default:")
    with bs._lock:
        bs._frames.clear(); bs._incidents.clear(); bs._visual_events.clear()
        bs._pinned_seqs.clear()
    A.MEMORY.reset()
    bs._ambient_cache["state"] = None
    # A genuinely quiet state: the frames agree the target process is not running
    # (it genuinely is not, in a test), and the capture engine is reported live.
    # Anything else would be a REAL signal, not noise — verified by asserting
    # build_signals() is empty below.
    bs._auto_engine.running = True
    bs._auto_engine.capturing = True
    bs._auto_engine.window_missing = False
    for i in range(1, 6):
        add_frame(i, base_ms + i * 1000.0, running=False)
    quiet_state = bs._ambient_state(use_cache=False)
    check("the quiet fixture really produces NO signals",
          A.build_signals(quiet_state) == [],
          str([(x["kind"], x["tier"]) for x in A.build_signals(quiet_state)]))
    sc, d = gj("/ambient?session_id=r1&event=UserPromptSubmit")
    check("/ambient 200", sc == 200)
    # CONTRACT CHANGE: silence is about AgentVision's OWN signals. New raw log
    # lines are the program talking, and they are delivered regardless — a layer
    # that decides the agent does not need to see program output is how 180 GPU
    # present failures got summarized as "health 100, looks healthy". So a
    # healthy state is silent ONLY when the program also logged nothing new.
    if d.get("tier") == "raw":
        check("healthy + new raw lines -> raw-only injection",
              d.get("inject") is True and d.get("signal_kinds") == ["raw_log"],
              str(d)[:160])
        check("and it says why it spoke",
              "raw output is never withheld" in str(d.get("reason")),
              str(d.get("reason")))
    else:
        check("healthy state + no new log lines -> inject=false",
              d.get("inject") is False, str(d)[:160])
        check("silent means zero bytes", d.get("bytes") == 0)
    check("caps are self-describing", set((d.get("caps") or {}).keys())
          == {"heartbeat", "notice", "alert"}, str(d.get("caps")))
    check("build_ms is reported", "build_ms" in d)
    check("explains itself", bool(d.get("reason")))
    check("payload documents what it is", "push" in str(d.get("what_this_is")).lower())

    # ── /ambient: an incident produces an ALERT once ─────────────────────────
    print("/ambient — alert on a real failure:")
    bs._freeze_incident("error", 5, base_ms + 5000.0, "KeyError: 'cfg'",
                        extra={"fingerprint": "fp-push-1"})
    bs._ambient_cache["state"] = None
    A.MEMORY.reset()
    sc, d = gj("/ambient?session_id=r2&event=PostToolUse")
    check("a frozen incident -> alert", d.get("tier") == "alert"
          and d.get("inject") is True, str(d)[:200])
    check("text mentions the incident", "froze an incident" in (d.get("text") or ""))
    check("text points at av_error_moment", "av_error_moment" in (d.get("text") or ""))
    check("text contains the Visual: sentence", "Visual:" in (d.get("text") or ""),
          (d.get("text") or "")[:200])
    # The tier cap bounds AgentVision's PROSE. The raw-log block is appended
    # after it with its own separate budget, deliberately: the program's actual
    # output must never be squeezed out to make room for commentary about it.
    prose = (d.get("text") or "").split("--- RAW LOG")[0]
    check("alert PROSE respects the byte cap",
          len(prose.encode("utf-8")) <= A.ALERT_CAP + 8,
          f"{len(prose.encode('utf-8'))} > {A.ALERT_CAP}")
    check("total stays within prose cap + raw cap",
          d.get("bytes", 0) <= A.ALERT_CAP + bs.RAW_PUSH_CAP + 200,
          f"{d.get('bytes')} > {A.ALERT_CAP}")
    check("est_tokens is reported", (d.get("est_tokens") or 0) > 0)
    txt_first = d.get("text")

    bs._ambient_cache["state"] = None
    sc, d2 = gj("/ambient?session_id=r2&event=PostToolUse")
    check("the incident is SUPPRESSED on the second call",
          any("already surfaced" in x.get("why", "")
              for x in (d2.get("suppressed") or [])),
          json.dumps(d2.get("suppressed"))[:200])
    check("the second call does not repeat the incident text",
          "froze an incident" not in (d2.get("text") or ""),
          (d2.get("text") or "")[:120])
    # It may legitimately surface a DIFFERENT fresh signal once; it must then
    # converge to silence rather than chattering forever.
    silent_by = None
    for attempt in range(6):
        bs._ambient_cache["state"] = None
        _sc, dn = gj("/ambient?session_id=r2&event=PostToolUse")
        if dn.get("inject") is False:
            silent_by = attempt + 1
            break
    check("the channel CONVERGES to silence within a few calls",
          silent_by is not None, "still injecting after 6 calls — chatty channel")
    sc, d3 = gj("/ambient?session_id=r3&event=PostToolUse")
    check("a different session IS told", d3.get("inject") is True)
    sc, d4 = gj("/ambient?session_id=r2&event=PostToolUse&force=1")
    check("force=1 shows what it would say", d4.get("inject") is True
          and d4.get("forced") is True)
    # Only the PROSE must be reproducible. The raw tail legitimately differs
    # between calls because the program keeps logging, and force= peeks without
    # consuming so a preview cannot blind the next real injection.
    # rstrip: splitting on the raw-block marker leaves its leading indent as a
    # trailing blank line, which is a harness artifact, not a content difference.
    _prose = lambda t: (t or "").split("--- RAW LOG")[0].rstrip()
    check("forced PROSE matches the original",
          _prose(d4.get("text")) == _prose(txt_first),
          _prose(d4.get("text"))[:120])

    # ── /ambient/reset ──────────────────────────────────────────────────────
    print("/ambient/reset:")
    r = client.post("/ambient/reset", json={"session_id": "r2"})
    check("reset 200", r.status_code == 200)
    bs._ambient_cache["state"] = None
    sc, d5 = gj("/ambient?session_id=r2&event=PostToolUse")
    check("after reset the session is told again", d5.get("inject") is True)

    # ── Stop check is disabled by default ───────────────────────────────────
    print("/ambient stop_check:")
    sc, sd = gj("/ambient?session_id=r9&event=Stop&stop_check=1")
    check("stop_check 200", sc == 200)
    check("stop blocking is DISABLED by default", sd.get("block") is False,
          str(sd)[:180])
    check("stop_check reports enabled=false", sd.get("enabled") is False)
    check("stop_check lists the only eligible kinds",
          set(sd.get("eligible_kinds") or []) ==
          {"crash", "fatal", "hang", "program_died"}, str(sd.get("eligible_kinds")))
    check("stop_check reports the budget", sd.get("max_stop_blocks") is not None)

    # ── The real hook SCRIPT ────────────────────────────────────────────────
    print("hook script — never breaks the session:")
    check("hook script exists", HOOK.exists(), str(HOOK))
    for ev in ("SessionStart", "UserPromptSubmit", "PostToolUse",
               "PostToolBatch", "Stop"):
        rc, out, err = run_hook({"hook_event_name": ev, "session_id": "h1",
                                 "tool_name": "Edit"})
        check(f"{ev}: exit 0 with the bridge DOWN", rc == 0, f"rc={rc}")
        check(f"{ev}: prints nothing with the bridge down", out.strip() == "",
              out[:80])
        check(f"{ev}: writes nothing to stderr", err.strip() == "", err[:120])

    print("hook script — hostile input:")
    for label, raw in (("empty", ""), ("not json", "not json"),
                       ("array", "[]"), ("null", "null"),
                       ("no event", '{"x":1}'),
                       ("unknown event", '{"hook_event_name":"Nope"}')):
        env = dict(os.environ)
        env["AGENTVISION_BRIDGE_URL"] = "http://127.0.0.1:9"
        pr = subprocess.run([sys.executable, str(HOOK)], input=raw,
                            capture_output=True, text=True, env=env, timeout=20)
        check(f"{label}: exit 0", pr.returncode == 0, f"rc={pr.returncode}")
        check(f"{label}: no stdout", pr.stdout.strip() == "", pr.stdout[:60])

    # ── The hook against a LIVE bridge (subprocess, real HTTP) ──────────────
    print("hook script — live bridge:")
    import threading
    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", 0, bs.app, threaded=True)
    port = srv.server_port
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        A.MEMORY.reset()
        bs._ambient_cache["state"] = None
        rc, out, err = run_hook({"hook_event_name": "PostToolUse",
                                 "session_id": "live-h", "tool_name": "Edit"},
                                bridge=base)
        check("live: exit 0", rc == 0, f"rc={rc} err={err[:100]}")
        check("live: an alert IS printed", "[AgentVision ALERT]" in out, out[:160])
        check("live: the Visual sentence is present", "Visual:" in out)
        check("live: no stderr", err.strip() == "", err[:120])
        print(f"      injected {len(out.encode())} bytes "
              f"(~{len(out.encode())//4} est tokens)")

        quiet_at = None
        for attempt in range(6):
            bs._ambient_cache["state"] = None
            rc, out2, _ = run_hook({"hook_event_name": "PostToolUse",
                                    "session_id": "live-h", "tool_name": "Edit"},
                                   bridge=base)
            if "froze an incident" in out2:
                check("live: the incident is never repeated", False, out2[:120])
                break
            if out2.strip() == "":
                quiet_at = attempt + 1
                break
        check("live: repeated calls go SILENT (no chatter)", quiet_at is not None,
              "still injecting after 6 calls")

        # Non-mutating tool must be ignored even if the matcher let it through.
        A.MEMORY.reset(); bs._ambient_cache["state"] = None
        rc, out3, _ = run_hook({"hook_event_name": "PostToolUse",
                                "session_id": "live-h2", "tool_name": "Read"},
                               bridge=base)
        check("live: PostToolUse ignores a non-mutating tool (Read)",
              out3.strip() == "", out3[:100])

        # The off switch must win.
        A.MEMORY.reset(); bs._ambient_cache["state"] = None
        rc, out4, _ = run_hook({"hook_event_name": "PostToolUse",
                                "session_id": "live-h3", "tool_name": "Edit"},
                               env_over={"AGENTVISION_PUSH_MODE": "0"},
                               bridge=base)
        check("live: AGENTVISION_PUSH_MODE=0 silences everything",
              out4.strip() == "", out4[:100])

        # Stop must not block with the default config, even on a live bridge
        # with a real incident present.
        rc, out5, err5 = run_hook({"hook_event_name": "Stop",
                                   "session_id": "live-h4"}, bridge=base)
        check("live: Stop does NOT block by default",
              rc == 0 and out5.strip() == "", f"rc={rc} out={out5[:80]}")

        # JSON output mode, when requested.
        A.MEMORY.reset(); bs._ambient_cache["state"] = None
        rc, out6, _ = run_hook({"hook_event_name": "UserPromptSubmit",
                                "session_id": "live-h5"},
                               env_over={"AGENTVISION_HOOK_JSON": "1"},
                               bridge=base)
        if out6.strip():
            try:
                j = json.loads(out6)
                ok = (j.get("hookSpecificOutput", {}).get("hookEventName")
                      == "UserPromptSubmit"
                      and bool(j["hookSpecificOutput"].get("additionalContext")))
            except Exception:
                ok = False
            check("live: JSON mode emits hookSpecificOutput.additionalContext",
                  ok, out6[:160])
        else:
            check("live: JSON mode produced output to check", False,
                  "expected an injection")
    finally:
        srv.shutdown()

    print(f"\n{'=' * 60}")
    print("push_routes: " + ("ALL PASS" if _fails == 0 else f"{_fails} FAILURE(S)"))
    print("=" * 60)
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
