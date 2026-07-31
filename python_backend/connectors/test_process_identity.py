#!/usr/bin/env python3
""""Is the program running?" must not be answered by a bystander.

Measured on this machine before the fix, with the real `sharpemu` profile
(process_name="SharpEmu", project_root="~/Developer/demoapp"):

    is_running() -> True   while `cat <root>/log/actions.jsonl` ran
    is_running() -> True   while `less <root>/agentvision/sharpemu/activity.log` ran
    is_running() -> True   while a /bin/zsh -c command merely MENTIONED <root>
    is_running() -> True   with nothing of the program alive at all

The branch responsible: `process_name in cmdline` followed by
`return bool(root and root in cmdline)`. Every shell command, editor, pager and
`ls` that touches the project satisfies both. Downstream, the capture engine gates
on exactly this, which is how 12,921 frames came to be stored for a process that
had exited — every sidecar recording "running": true.

The fix is not another keyword list: it is requiring the process to LOOK like a
launch of the program (argv[0] is not a file-visiting utility, and an argument
names the program rather than a file or folder beside it), and recording WHICH
process matched and by which rule so a wrong claim is reviewable.

Run:  .venv/bin/python python_backend/connectors/test_process_identity.py
"""
from __future__ import annotations
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent))          # python_backend
sys.path.insert(0, str(_HERE.parent.parent.parent))   # repo root

_fails = []


def check(name, cond, detail=""):
    print(f"  [{'ok ' if cond else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        _fails.append(name)


def main():
    from connectors.program_connector import ProgramProfile, ProgramDataReader

    # A project whose folder name is also the program's name — the shape that
    # makes this hard, and the shape of the real profile it was found on.
    root = Path(tempfile.mkdtemp(prefix="av_proc_")) / "demoapp"
    (root / "publish").mkdir(parents=True)
    (root / "log").mkdir()
    (root / "agentvision" / "sharpemu").mkdir(parents=True)
    (root / "log" / "actions.jsonl").write_text("{}\n")
    (root / "publish" / "SharpEmu.dll").write_text("")
    exe_in_project = root / "publish" / "SharpEmu"
    exe_in_project.write_text("")

    prof = ProgramProfile(name="sharpemu", display_name="SharpEmu",
                          process_name="SharpEmu", project_root=str(root))
    r = ProgramDataReader(prof)

    def m(pname, argv, exe=""):
        cmdline = " ".join(argv).lower()
        return r._match_reason(pname.lower(), cmdline, exe, argv)

    print("bystanders that merely mention the project:")
    for label, pname, argv, exe in [
        ("a shell command containing the project path",
         "zsh", ["/bin/zsh", "-c", f"cat {root}/log/actions.jsonl | tail -5"], "/bin/zsh"),
        ("cat on a file inside the project",
         "cat", ["/bin/cat", f"{root}/log/actions.jsonl"], "/bin/cat"),
        ("less on the project's own activity log",
         "less", ["/usr/bin/less", f"{root}/agentvision/sharpemu/activity.log"],
         "/usr/bin/less"),
        ("ls of the FOLDER that shares the program's name",
         "ls", ["/bin/ls", f"{root}/agentvision/sharpemu"], "/bin/ls"),
        ("an editor with a project file open",
         "vim", ["/usr/bin/vim", f"{root}/src/Main.cs"], "/usr/bin/vim"),
        ("tail -f on the program's log",
         "tail", ["/usr/bin/tail", "-f", f"{root}/log/actions.jsonl"], "/usr/bin/tail"),
        ("a python one-liner reading the project",
         "python3", ["/usr/bin/python3", "-c",
                     f"open('{root}/log/actions.jsonl').read()"], "/usr/bin/python3"),
    ]:
        why = m(pname, argv, exe)
        check(f"NOT the program: {label}", why == "", f"matched_by={why!r}")

    print("the real program, in each launch shape:")
    for label, pname, argv, exe, want in [
        ("the project's own binary", "SharpEmu",
         [str(exe_in_project), "--game", "sarah"], str(exe_in_project),
         "exe-under-project-root"),
        ("dotnet running the program's dll", "dotnet",
         ["/usr/local/share/dotnet/dotnet", str(root / "publish" / "SharpEmu.dll")],
         "/usr/local/share/dotnet/dotnet", "runtime+script-argument"),
        ("a process literally named SharpEmu", "SharpEmu",
         ["SharpEmu"], "/elsewhere/SharpEmu", "process-name"),
    ]:
        why = m(pname, argv, exe)
        check(f"IS the program: {label}", why == want, f"matched_by={why!r}")

    print("an interpreted target still matches by its script argument:")
    proj2 = Path(tempfile.mkdtemp(prefix="av_proc2_")) / "webapp"
    proj2.mkdir(parents=True)
    (proj2 / "app.py").write_text("")
    prof2 = ProgramProfile(name="webapp", display_name="WebApp",
                           process_name="app.py", project_root=str(proj2))
    r2 = ProgramDataReader(prof2)
    why = r2._match_reason("python3",
                           f"/usr/bin/python3 {proj2}/app.py".lower(),
                           "/usr/bin/python3",
                           ["/usr/bin/python3", str(proj2 / "app.py")])
    check("python3 <root>/app.py IS the target", why == "runtime+script-argument",
          f"matched_by={why!r}")
    why = r2._match_reason("cat", f"/bin/cat {proj2}/app.py".lower(), "/bin/cat",
                           ["/bin/cat", str(proj2 / "app.py")])
    check("cat <root>/app.py is NOT the target", why == "", f"matched_by={why!r}")

    print("liveness comes with its evidence:")
    ev = r.running_evidence()
    check("running_evidence() reports the keys a caller needs",
          all(k in ev for k in ("running", "pid", "exe", "matched_by", "scanned")),
          str(sorted(ev)))
    check("it scanned the process table", ev.get("scanned", 0) > 0,
          f"scanned={ev.get('scanned')}")
    check("nothing of this fixture is running, so running=False",
          ev.get("running") is False and ev.get("matched_by") is None,
          f"running={ev.get('running')} matched_by={ev.get('matched_by')}")
    check("is_running() agrees with running_evidence()",
          r.is_running() == bool(ev.get("running")))

    print("live end-to-end: a real bystander process must not count:")
    p = subprocess.Popen(["/bin/cat"], stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL)
    try:
        # Give the profile a process_name that a bystander's argv would satisfy.
        live_root = root
        p2 = subprocess.Popen(
            ["/bin/sh", "-c", f"sleep 2 # {live_root}/log/actions.jsonl sharpemu"])
        time.sleep(0.5)
        ev2 = r.running_evidence()
        check("a shell whose argv names the project is not 'the program running'",
              ev2.get("running") is False,
              f"matched pid={ev2.get('pid')} by={ev2.get('matched_by')}")
        p2.wait()
    finally:
        try:
            p.kill()
        except Exception:
            pass

    print()
    if _fails:
        print(f"{len(_fails)} failed: " + "; ".join(_fails))
        return 1
    print("0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
