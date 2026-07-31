#!/usr/bin/env python3
"""In-process hook behaviour: capture must not change what the target prints.

The bug this suite exists for: `logging.basicConfig()` is a no-op when the root
logger already has a handler, and av_runtime installs one at interpreter startup
— before the target's own call. A program whose WARNING/ERROR lines normally
reach the terminal printed NOTHING under AgentVision, while every record was
captured to JSONL. The human saw a silent program and would reasonably conclude
it never logged: a false read manufactured by the observer.

Each case runs a real subprocess, because the defect only exists in the
startup ORDER (hook first, program second) and an in-process test cannot
reproduce that.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

AV_ROOT = Path(__file__).resolve().parent.parent

PROG = """
import logging
logging.basicConfig({cfg})
log = logging.getLogger("app")
log.info("INFO-LINE")
log.warning("WARN-LINE")
log.error("ERROR-LINE")
"""

BOOT = (
    "from agent_bootstrap.av_runtime import install_all_hooks\n"
    "install_all_hooks()\n"
)


def _run(cfg: str, hooks: str | None, tmp: Path) -> tuple[str, list[dict]]:
    """Run the program (optionally under the hooks) -> (stderr, sink records)."""
    prog = tmp / "prog.py"
    prog.write_text(PROG.format(cfg=cfg))
    log_dir = tmp / "agentvision"
    log_dir.mkdir(exist_ok=True)
    sink = log_dir / "actions.jsonl"
    if sink.exists():
        sink.unlink()

    env = dict(os.environ)
    env.pop("AGENTVISION_HOOKS", None)
    env.pop("AGENTVISION_HOOKED", None)
    env.pop("AGENTVISION_GUARD", None)
    code = prog.read_text()
    if hooks is not None:
        env.update({
            "AGENTVISION_PROJECT": str(tmp),
            "AGENTVISION_LOG_DIR": str(log_dir),
            "AGENTVISION_HOOKS": hooks,
            "PYTHONPATH": str(AV_ROOT),
        })
        code = BOOT + code

    p = subprocess.run([sys.executable, "-c", code], cwd=tmp, env=env,
                       capture_output=True, text=True, timeout=60)
    recs = []
    if sink.exists():
        for line in sink.read_text(errors="replace").splitlines():
            try:
                recs.append(json.loads(line))
            except Exception:
                pass
    return p.stderr, recs


def _msgs(recs: list[dict]) -> str:
    return json.dumps(recs)


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # ── The regression itself ────────────────────────────────────────────
        base, _ = _run("", None, tmp)
        hooked, recs = _run("", "logging_bridge", tmp)

        check("baseline prints WARNING to the terminal",
              "WARN-LINE" in base, base[-200:])
        check("baseline prints ERROR to the terminal",
              "ERROR-LINE" in base, base[-200:])
        check("HOOKED still prints WARNING (basicConfig not neutered)",
              "WARN-LINE" in hooked, hooked[-400:] or "<no stderr at all>")
        check("HOOKED still prints ERROR",
              "ERROR-LINE" in hooked, hooked[-400:] or "<no stderr at all>")
        check("every record still reaches the JSONL sink",
              all(m in _msgs(recs) for m in
                  ("INFO-LINE", "WARN-LINE", "ERROR-LINE")),
              f"{len(recs)} records")

        # The documented cost, asserted so the doc stays true: with no explicit
        # level the root bump to INFO does surface INFO on the program's own
        # handlers. If this ever stops being true, the spec text must change too.
        check("documented cost holds: bare basicConfig() surfaces INFO",
              "INFO-LINE" in hooked, hooked[-400:])

        # ── An explicit level is a deliberate choice, not a default ──────────
        h2, recs2 = _run("level=logging.WARNING", "logging_bridge", tmp)
        check("explicit level=WARNING is respected (no INFO leak)",
              "INFO-LINE" not in h2, h2[-300:])
        check("...and WARNING still prints under that level",
              "WARN-LINE" in h2, h2[-300:])

        # ── The shield must not fire when logging_bridge was not selected ────
        h3, _ = _run("", "stdout_tee", tmp)
        check("hook not selected -> program logging untouched",
              "WARN-LINE" in h3 and "INFO-LINE" not in h3, h3[-300:])

        # ── force=True clears handlers; ours must come back ──────────────────
        h4, recs4 = _run("force=True", "logging_bridge", tmp)
        check("basicConfig(force=True) still prints",
              "WARN-LINE" in h4, h4[-300:])
        check("basicConfig(force=True) -> capture survives handler wipe",
              "WARN-LINE" in _msgs(recs4), f"{len(recs4)} records")

        # ── Idempotent: shielding twice must not nest wrappers ───────────────
        sys.path.insert(0, str(AV_ROOT))
        import logging as _lg

        from agent_bootstrap import av_runtime as rt
        before = _lg.basicConfig
        rt._shield_basicconfig()
        rt._shield_basicconfig()
        check("shield is idempotent (no wrapper nesting)",
              getattr(_lg.basicConfig, "_av_original", None) is not None
              and getattr(_lg.basicConfig, "_av_original") is not
              _lg.basicConfig,
              str(_lg.basicConfig))
        _lg.basicConfig = getattr(before, "_av_original", before)

    fails = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
        if not ok and detail:
            print(f"          {detail}")
    print(f"\n{len(checks) - len(fails)}/{len(checks)} checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
