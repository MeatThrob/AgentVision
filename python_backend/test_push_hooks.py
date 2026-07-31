#!/usr/bin/env python3
"""
Push Mode installer tests.

This module edits a file the user owns and depends on (`~/.claude/settings.json`),
so the tests are written around one question: **can it damage anything?** They run
entirely against a temporary settings file — the real one is never touched.

Covers:
  * non-destructive merge: unrelated settings keys AND other people's hooks on the
    same events survive
  * idempotence: installing twice does not duplicate entries
  * refresh-in-place: re-installing updates paths without adding a second entry
  * correct wiring, including the `compact` SessionStart matcher
  * Stop hook is NOT installed unless explicitly requested
  * uninstall removes exactly ours and leaves foreign hooks intact
  * timestamped backups on both install and uninstall
  * a malformed settings file is REFUSED, not overwritten
  * the enable flag is independent of installation
  * the hook script itself exists and is syntactically valid

Run:  python3 python_backend/test_push_hooks.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE.parent.parent))

_fails = 0


def check(name, cond, detail=""):
    global _fails
    if not cond:
        _fails += 1
    print(f"  [{'ok ' if cond else 'FAIL'}] {name}"
          f"{'' if cond or not detail else '  — ' + detail}")


FOREIGN = {
    "model": "claude-opus-4-8",
    "someUserSetting": {"nested": [1, 2, 3]},
    "hooks": {
        # Somebody else's hook on an event we also use.
        "UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "/usr/local/bin/their-hook"}]}
        ],
        # An event we never touch.
        "PreToolUse": [
            {"matcher": "Bash",
             "hooks": [{"type": "command", "command": "echo audit"}]}
        ],
    },
}


def main():
    import push_hooks as PH

    work = Path(tempfile.mkdtemp(prefix="av_hooks_"))
    sandbox = work / "settings.json"
    sandbox.write_text(json.dumps(FOREIGN, indent=2))

    # Redirect the installer at the sandbox. The real settings file must never
    # be opened by this test.
    real_settings_path = PH.settings_path
    PH.settings_path = lambda scope="user", project_dir=None: sandbox
    real_flag = PH.state_flag_path
    PH.state_flag_path = lambda: work / "push_mode.json"

    try:
        # ── The hook script must exist and be valid Python ────────────────────
        print("hook script:")
        hs = PH.hook_script_path()
        check("hook script exists", hs.exists(), str(hs))
        import py_compile
        try:
            py_compile.compile(str(hs), doraise=True)
            ok = True
        except Exception as exc:
            ok = False
            print("      compile error:", exc)
        check("hook script compiles", ok)
        src = hs.read_text()
        check("hook script is stdlib-only (no requests/flask/PIL import)",
              not any(f"import {m}" in src for m in ("requests", "flask", "PIL")),
              "must run under the user's bare python3")

        # ── Plan ─────────────────────────────────────────────────────────────
        print("plan:")
        pl = PH.plan()
        events = {e["event"]: e["matcher"] for e in pl["events"]}
        check("wires SessionStart", "SessionStart" in events)
        check("SessionStart includes the COMPACT matcher",
              "compact" in (events.get("SessionStart") or ""),
              events.get("SessionStart"))
        check("wires UserPromptSubmit", "UserPromptSubmit" in events)
        check("wires PostToolBatch", "PostToolBatch" in events)
        check("PostToolUse is scoped to mutating tools",
              events.get("PostToolUse") == "Edit|Write|MultiEdit|Bash",
              str(events.get("PostToolUse")))
        check("Stop is NOT wired by default", "Stop" not in events)
        check("plan warns that stop-blocking needs a second opt-in",
              "AGENTVISION_AMBIENT_STOP_BLOCK" in pl["note"])

        # ── Install ──────────────────────────────────────────────────────────
        print("install (non-destructive merge):")
        res = PH.install(python_exe="python3")
        check("install ok", res.get("ok") is True, json.dumps(res)[:200])
        check("a timestamped backup was made", bool(res.get("backup"))
              and Path(res["backup"]).exists(), str(res.get("backup")))
        check("install reports the restart requirement",
              "RESTART" in json.dumps(res))
        check("install lists every way to disable it",
              len(res.get("disable_without_uninstalling") or []) >= 4)

        d = json.loads(sandbox.read_text())
        check("settings file is still valid JSON", isinstance(d, dict))
        check("unrelated top-level settings survive",
              d.get("model") == "claude-opus-4-8"
              and d.get("someUserSetting") == {"nested": [1, 2, 3]},
              str({k: d.get(k) for k in ("model", "someUserSetting")}))
        ups = d["hooks"]["UserPromptSubmit"]
        cmds = [h.get("command") for g in ups for h in (g.get("hooks") or [])]
        check("a FOREIGN hook on the same event survives",
              "/usr/local/bin/their-hook" in cmds, str(cmds))
        check("our hook was added alongside it",
              any(PH.HOOK_SCRIPT_NAME in str(c) for c in cmds), str(cmds))
        check("an event we do not use is untouched",
              d["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "echo audit")
        ss = d["hooks"]["SessionStart"]
        check("SessionStart matcher written correctly",
              any("compact" in str(g.get("matcher")) for g in ss), str(ss)[:160])
        check("Stop hook NOT installed by default", "Stop" not in d["hooks"])
        ours = [h for g in ss for h in (g.get("hooks") or [])
                if PH.HOOK_SCRIPT_NAME in str(h.get("command"))]
        check("our entry is tagged for clean removal",
              ours and ours[0].get("statusMessage") == PH.STATUS_TAG,
              str(ours[:1]))
        check("our entry declares a timeout", ours and ours[0].get("timeout"),
              str(ours[:1]))
        check("timeout is a plausible SECONDS value (not ms)",
              0 < int(ours[0]["timeout"]) <= 60, str(ours[0].get("timeout")))

        # ── Idempotence ──────────────────────────────────────────────────────
        print("idempotence:")
        before = json.loads(sandbox.read_text())
        res2 = PH.install(python_exe="python3")
        after = json.loads(sandbox.read_text())
        check("second install succeeds", res2.get("ok") is True)
        check("second install REFRESHES rather than adds",
              res2.get("events_refreshed") and not res2.get("events_added"),
              json.dumps({k: res2.get(k) for k in
                          ("events_added", "events_refreshed")}))
        for ev in ("SessionStart", "UserPromptSubmit", "PostToolBatch", "PostToolUse"):
            n = sum(1 for g in after["hooks"][ev]
                    if any(PH.HOOK_SCRIPT_NAME in str(h.get("command"))
                           for h in (g.get("hooks") or [])))
            check(f"exactly one AgentVision entry on {ev}", n == 1, f"found {n}")
        check("foreign hook still present after re-install",
              any("/usr/local/bin/their-hook" == h.get("command")
                  for g in after["hooks"]["UserPromptSubmit"]
                  for h in (g.get("hooks") or [])))

        # ── Status ───────────────────────────────────────────────────────────
        print("status:")
        stt = PH.status()
        check("status reports installed", stt["installed"] is True)
        check("status lists the wired events",
              {x["event"] for x in stt["installed_events"]} >=
              {"SessionStart", "UserPromptSubmit", "PostToolBatch", "PostToolUse"},
              str(stt["installed_events"]))
        check("status reports push_mode_enabled", stt["push_mode_enabled"] is True)
        check("status reports stop_block_active separately",
              stt["stop_block_active"] is False)

        # ── Enable flag is independent of installation ───────────────────────
        print("enable flag:")
        PH.set_enabled(False)
        check("flag can be turned off", PH.get_enabled()["enabled"] is False)
        check("turning it off does NOT uninstall",
              PH.status()["installed"] is True)
        PH.set_enabled(True)
        check("flag can be turned back on", PH.get_enabled()["enabled"] is True)

        # ── Stop hook, explicitly requested ─────────────────────────────────
        print("stop hook (opt-in):")
        res3 = PH.install(python_exe="python3", with_stop_block=True)
        d3 = json.loads(sandbox.read_text())
        check("Stop hook installs only when asked",
              "Stop" in d3["hooks"] and res3.get("stop_block_hook_installed") is True)
        check("installing the Stop hook does NOT activate blocking",
              res3.get("stop_block_active") is False,
              "blocking needs AGENTVISION_AMBIENT_STOP_BLOCK=1 as a second opt-in")

        # ── Uninstall ────────────────────────────────────────────────────────
        print("uninstall:")
        res4 = PH.uninstall()
        check("uninstall ok", res4.get("ok") is True, json.dumps(res4)[:160])
        check("uninstall made a backup too", bool(res4.get("backup"))
              and Path(res4["backup"]).exists())
        d4 = json.loads(sandbox.read_text())
        allcmds = [h.get("command")
                   for groups in (d4.get("hooks") or {}).values()
                   for g in groups for h in (g.get("hooks") or [])]
        check("NO AgentVision hook remains",
              not any(PH.HOOK_SCRIPT_NAME in str(c) for c in allcmds), str(allcmds))
        check("the FOREIGN hook survived uninstall",
              "/usr/local/bin/their-hook" in allcmds, str(allcmds))
        check("the untouched event survived uninstall",
              "echo audit" in allcmds, str(allcmds))
        check("unrelated settings survived uninstall",
              d4.get("model") == "claude-opus-4-8"
              and d4.get("someUserSetting") == {"nested": [1, 2, 3]})
        check("events we solely occupied are cleaned up",
              "SessionStart" not in (d4.get("hooks") or {}),
              str(list((d4.get("hooks") or {}).keys())))
        check("status now reports not-installed", PH.status()["installed"] is False)
        check("uninstall is idempotent", PH.uninstall().get("ok") is True)

        # ── A malformed settings file must be REFUSED, not clobbered ─────────
        print("malformed settings safety:")
        bad = work / "bad.json"
        bad.write_text("{ this is not json ")
        PH.settings_path = lambda scope="user", project_dir=None: bad
        res5 = PH.install(python_exe="python3")
        check("install REFUSES a malformed settings file",
              res5.get("ok") is False and "parse" in str(res5.get("error")).lower(),
              json.dumps(res5)[:200])
        check("the malformed file was NOT overwritten",
              bad.read_text() == "{ this is not json ")
        check("refusal explains what to do", bool(res5.get("hint")))
        res6 = PH.uninstall()
        check("uninstall also refuses a malformed file",
              res6.get("ok") is False)

        # ── A missing settings file is created cleanly ───────────────────────
        print("fresh install (no settings file):")
        fresh = work / "sub" / "settings.json"
        PH.settings_path = lambda scope="user", project_dir=None: fresh
        res7 = PH.install(python_exe="python3")
        check("install creates a missing settings file",
              res7.get("ok") is True and fresh.exists())
        check("no backup claimed when there was nothing to back up",
              res7.get("backup") is None, str(res7.get("backup")))
        d7 = json.loads(fresh.read_text())
        check("fresh file contains only our hooks",
              set((d7.get("hooks") or {}).keys()) ==
              {"SessionStart", "UserPromptSubmit", "PostToolBatch", "PostToolUse"},
              str(list((d7.get("hooks") or {}).keys())))
    finally:
        PH.settings_path = real_settings_path
        PH.state_flag_path = real_flag

    print(f"\n{'=' * 60}")
    print("push_hooks: " + ("ALL PASS" if _fails == 0 else f"{_fails} FAILURE(S)"))
    print("=" * 60)
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
