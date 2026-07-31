#!/usr/bin/env python3
"""Remove AgentVision's own records from a program's action log.

Versions of AgentVision before 2026-07-30 appended the stuck watchdog's
`program.stuck` records straight into the bridged program's actions.jsonl. On one
real project that left the file 95% observer noise:

    total records                    2171
    written by agentvision.watchdog  2072
    written by the program             99
    newest program record         48.8 h old
    newest watchdog record         0.0 h old  (still being appended)

The write has been stopped (records now go to log/observer/<profile>.observer.jsonl,
see /observer/log) and every AgentVision read path already excludes
`agentvision.*` records. So this script is NOT required for correct behaviour —
it is for the case where you, or a tool that is not AgentVision, has to read that
file directly and wants the program's own output back.

DRY RUN BY DEFAULT. Nothing is written without --apply, and --apply keeps the
original alongside the cleaned file as <name>.av_contaminated.bak.

    python3 scripts/decontaminate_log.py <path/to/actions.jsonl>
    python3 scripts/decontaminate_log.py <path/to/actions.jsonl> --apply
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

SELF_PREFIX = "agentvision"


def classify(path: Path) -> dict:
    keep: list[str] = []
    drop: list[str] = []
    unparsed = 0
    newest_prog = 0.0
    newest_self = 0.0
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            unparsed += 1
            keep.append(line)          # never drop what we cannot read
            continue
        ts = rec.get("ts_ms") or 0.0
        if str(rec.get("source") or "").startswith(SELF_PREFIX):
            drop.append(line)
            newest_self = max(newest_self, ts)
        else:
            keep.append(line)
            newest_prog = max(newest_prog, ts)
    now = time.time() * 1000.0
    return {"keep": keep, "drop": drop, "unparsed": unparsed,
            "newest_program_age_h": (now - newest_prog) / 3.6e6 if newest_prog else None,
            "newest_self_age_h": (now - newest_self) / 3.6e6 if newest_self else None}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="the program's actions.jsonl")
    ap.add_argument("--apply", action="store_true",
                    help="actually rewrite the file (keeps a .av_contaminated.bak)")
    args = ap.parse_args()

    path = Path(args.path).expanduser()
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 2

    r = classify(path)
    total = len(r["keep"]) + len(r["drop"])
    pct = (100.0 * len(r["drop"]) / total) if total else 0.0
    print(f"{path}")
    print(f"  total records            {total}")
    print(f"  AgentVision's own        {len(r['drop'])}  ({pct:.1f}%)")
    print(f"  the program's own        {len(r['keep']) - r['unparsed']}")
    if r["unparsed"]:
        print(f"  unparseable (KEPT)       {r['unparsed']}")
    if r["newest_program_age_h"] is not None:
        print(f"  newest program record    {r['newest_program_age_h']:.1f} h old")
    if r["newest_self_age_h"] is not None:
        print(f"  newest AgentVision one   {r['newest_self_age_h']:.1f} h old")

    if not r["drop"]:
        print("  nothing to do — no AgentVision records in this file")
        return 0
    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to rewrite "
              f"(original kept as {path.name}.av_contaminated.bak).")
        return 0

    bak = path.with_suffix(path.suffix + ".av_contaminated.bak")
    shutil.copy2(path, bak)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(r["keep"]) + ("\n" if r["keep"] else ""))
    tmp.replace(path)
    print(f"\nrewritten: {len(r['drop'])} AgentVision record(s) removed")
    print(f"original kept at: {bak}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
