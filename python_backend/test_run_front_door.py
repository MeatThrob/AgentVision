#!/usr/bin/env python3
"""`agentvision run -- <cmd>` is the documented front door. It must not instrument
the wrong project, and it must never do so silently.

THE BUG THIS EXISTS FOR. `_resolve_project_for_command` hunted for a
`.agentvision_attached` marker — a file the CURRENT bridge flow never writes. A
project bridged through `av_bridge_commit` has `agentvision/manifest.json`
instead, so the resolver found nothing, silently fell back to the CWD, and
instrumented THAT. Measured on a freshly bridged project: running the wrapper
from the wrong directory produced **0 records**; from the target directory, 14.
The program printed normally and exited 0 both times, and nothing said which
project had been watched.

That failure shape is the one this project cares about most: an empty log reads
as "the program logged nothing", not as "AgentVision was looking somewhere else".
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for p in (str(REPO), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import cli                                                    # noqa: E402


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    # .resolve() matters on macOS: tempfile gives /var/folders/... while the
    # resolver returns the real /private/var/folders/... path. Comparing the two
    # unresolved fails for a reason that has nothing to do with the code — the
    # same /tmp -> /private/tmp symlink trap that has bitten this repo before.
    root = Path(tempfile.mkdtemp(prefix="av-frontdoor-")).resolve()
    proj = root / "target"
    (proj / "agentvision").mkdir(parents=True)
    (proj / "app.py").write_text("print('hello')\n")
    (proj / "agentvision" / "manifest.json").write_text(json.dumps({
        "version": 2, "name": "t",
        "emitters_requested": ["stdout_tee", "lifecycle"],
    }))
    elsewhere = root / "elsewhere"
    elsewhere.mkdir()

    # ── The marker the current flow actually writes must be recognised ──────
    check("agentvision/manifest.json is a project marker",
          "agentvision/manifest.json" in cli._PROJECT_MARKERS,
          str(cli._PROJECT_MARKERS))
    check("the legacy attach marker still works",
          ".agentvision_attached" in cli._PROJECT_MARKERS)

    # ── Resolution from the SCRIPT PATH, with the cwd somewhere unrelated ───
    prev = os.getcwd()
    try:
        os.chdir(elsewhere)
        got = cli._resolve_project_for_command(
            ["python3", str(proj / "app.py")], quiet=True)
        check("resolves the project from the script path, not the cwd",
              got == proj, f"{got} != {proj}")

        # A relative script path from inside the project must also work.
        os.chdir(proj)
        got2 = cli._resolve_project_for_command(["python3", "app.py"], quiet=True)
        check("resolves from inside the project too", got2 == proj, str(got2))

        # A nested subdirectory must walk UP to the project root.
        nested = proj / "src" / "deep"
        nested.mkdir(parents=True)
        (nested / "run.py").write_text("print('x')\n")
        got3 = cli._resolve_project_for_command(
            ["python3", str(nested / "run.py")], quiet=True)
        check("walks up from a nested script to the project root",
              got3 == proj, str(got3))

        # ── No project anywhere: falls back to cwd, and SAYS SO ─────────────
        os.chdir(elsewhere)
        bare = root / "bare"
        bare.mkdir()
        (bare / "x.py").write_text("print(1)\n")
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); import cli;"
             "print(cli._resolve_project_for_command(['python3', %r]))"
             % (str(HERE), str(bare / "x.py"))],
            capture_output=True, text=True, cwd=str(elsewhere), timeout=60)
        check("an unbridged command falls back to the cwd",
              str(elsewhere) in proc.stdout, proc.stdout.strip()[:120])
        check("...and the fallback is ANNOUNCED, not silent",
              "no bridged project found" in proc.stderr,
              proc.stderr.strip()[:160] or "<nothing on stderr>")
        check("...and the warning names the directory it settled on",
              str(elsewhere) in proc.stderr, proc.stderr.strip()[:160])
        check("...and warns that nothing will be recorded for another project",
              "Nothing will be recorded" in proc.stderr)
    finally:
        os.chdir(prev)

    # ── quiet=True must stay silent, for callers that resolve twice ─────────
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        cli._resolve_project_for_command(["python3", "/nonexistent/zz.py"],
                                         quiet=True)
    check("quiet=True suppresses the warning", buf.getvalue() == "",
          buf.getvalue()[:100])

    fails = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
        if not ok and detail:
            print(f"          {detail}")
    print(f"\n{len(checks) - len(fails)}/{len(checks)} checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
