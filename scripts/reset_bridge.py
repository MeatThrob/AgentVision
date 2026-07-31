#!/usr/bin/env python3
"""Reset a program's bridge so the next connection is a genuine FIRST bridge.

Why this exists
---------------
The bridge gate fires once per program, by design. That makes it awkward to test:
verifying that a cold agent can onboard correctly needs a program in the
PROVISIONAL state, and the honest way to get one is to wipe the bridge rather
than to keep pointing at ever more throwaway projects.

What "sealed" actually is, on disk:
  <project_root>/agentvision/<profile>/.av_bridge_plan.json   the committed plan
  <project_root>/agentvision/<profile>/.av_preflight_ok       legacy marker
Both live inside the target project's `agentvision/` folder, so removing that
folder returns the program to PROVISIONAL.

Usage
-----
  reset_bridge.py --profile abyss              # look the root up from the profile
  reset_bridge.py --root "/path/to/project"    # or name the project directly
  reset_bridge.py --profile abyss --plan-only  # keep frames, drop only the seal
  reset_bridge.py --profile abyss --dry-run    # show what would go

This DELETES captured frames and logs for that program unless --plan-only is
used. It prints exactly what it is about to remove and refuses to touch anything
outside a directory literally named `agentvision`.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

BASE = os.environ.get("AGENTVISION_BRIDGE_URL", "http://127.0.0.1:7771")


def _profiles() -> dict:
    try:
        with urllib.request.urlopen(BASE + "/profiles", timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as exc:
        print(f"! could not reach the bridge at {BASE} ({exc})")
        print("  pass --root instead of --profile, or start the bridge server.")
        return {}


def _human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n / 1:.0f} {unit}"
        n /= 1024.0
    return f"{n:.0f} B"


def _size(p: Path) -> tuple[int, int]:
    total = files = 0
    for f in p.rglob("*"):
        if f.is_file():
            files += 1
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total, files


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--profile", help="profile name; its project_root is looked up")
    g.add_argument("--root", help="the target project directory")
    ap.add_argument("--plan-only", action="store_true",
                    help="remove only the seal (plan + legacy marker), keep frames")
    ap.add_argument("--full", action="store_true",
                    help="ALSO clear the profile's declared log_sources, so the next "
                         "bridge is a genuinely first-time one. Without this the "
                         "profile keeps any adapter pinning from the previous "
                         "bridge, which quietly assists a cold-start test.")
    ap.add_argument("--dry-run", action="store_true", help="show, do not delete")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation")
    a = ap.parse_args()

    root = a.root
    if a.profile:
        profs = _profiles()
        if a.profile not in profs:
            print(f"! no profile named {a.profile!r}. Known: {sorted(profs)}")
            return 2
        root = (profs[a.profile] or {}).get("project_root") or ""
        if not root:
            print(f"! profile {a.profile!r} has no project_root — pass --root.")
            return 2

    rootp = Path(os.path.expanduser(root)).resolve()
    if not rootp.is_dir():
        print(f"! not a directory: {rootp}")
        return 2

    av = rootp / "agentvision"
    # Never delete anything that is not literally the agentvision sink folder.
    if av.name != "agentvision":
        print(f"! refusing: {av} is not named 'agentvision'")
        return 2
    if not av.exists():
        print(f"nothing to do — {av} does not exist (already a fresh bridge)")
        return 0

    seals = sorted([p for p in av.rglob(".av_bridge_plan.json")] +
                   [p for p in av.rglob(".av_preflight_ok")])
    nbytes, nfiles = _size(av)

    print(f"target project : {rootp}")
    print(f"sink folder    : {av}")
    print(f"contents       : {nfiles} files, {_human(nbytes)}")
    print(f"seal files     : {len(seals)}")
    for s in seals:
        print(f"   - {s.relative_to(rootp)}")

    if a.plan_only:
        if not seals:
            print("\nno seal files present — the bridge is already PROVISIONAL")
            return 0
        print(f"\nWILL DELETE {len(seals)} seal file(s); frames and logs stay.")
    else:
        print(f"\nWILL DELETE THE WHOLE FOLDER: {av}")
        print("   (that includes captured frames and any logs written into it)")

    if a.dry_run:
        print("\n--dry-run: nothing was deleted")
        return 0
    if not a.yes:
        try:
            if input("\nproceed? [y/N] ").strip().lower() not in ("y", "yes"):
                print("aborted")
                return 1
        except EOFError:
            print("aborted (no tty; pass --yes to confirm non-interactively)")
            return 1

    if a.plan_only:
        for s in seals:
            s.unlink()
            print(f"removed {s.relative_to(rootp)}")
    else:
        shutil.rmtree(av)
        print(f"removed {av}")

    if a.full and a.profile:
        # The seal lives in the project; the ADAPTER PINNING lives in the profile.
        # Clearing only the first leaves the next "first bridge" pre-wired with the
        # previous run's adapter choices — which silently assists any cold-start
        # test and hides whether the agent would have gotten the parsing right.
        try:
            # There is no PATCH route, and POST /profiles REPLACES rather than
            # merges (omitted fields silently revert to defaults). So read the
            # whole profile, clear the one field, and write it all back.
            current = dict((_profiles() or {}).get(a.profile) or {})
            if not current:
                raise RuntimeError("profile not readable")
            had = len(current.get("log_sources") or [])
            current["log_sources"] = []
            current.setdefault("name", a.profile)
            req = urllib.request.Request(
                f"{BASE}/profiles", data=json.dumps(current).encode(),
                method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                r.read()
            print(f"cleared {had} declared log_source(s) from profile "
                  f"{a.profile!r} — the next bridge must re-declare and re-pin them")
        except Exception as exc:
            print(f"! could not clear log_sources via the API ({exc})")
            print("  the project folder is still reset; the profile keeps its "
                  "declared sources and any adapter pinning.")

    print("\nThis program is now PROVISIONAL again. The next connection is a real")
    print("first bridge: av_bridge_status() -> av_bridge_catalog() -> av_bridge_commit().")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
