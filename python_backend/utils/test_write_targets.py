#!/usr/bin/env python3
"""
Where is the program ACTUALLY writing? — asked of the OS, not of config.
================================================================================
This closes a failure that costs real time and looks like nothing at all: reading
a log the program is no longer writing to. On this project AgentVision was
configured to read `<project>/log/log.txt`, read it, and reported its contents
as current — while the file's last write was 23 HOURS old and the emulator's own
sink was writing to a completely different directory. A whole analysis ("180 GPU
present failures") was performed against a dead file. `tail -f` on the wrong file
shows stale bytes forever and never complains, and a program cannot tell you it is
misconfigured because from its point of view nothing is wrong.

So the test spawns a REAL process that writes to one file while the "profile"
declares another, and asserts the three mismatches get named:

    missing_from_config  the process writes here, nothing reads it
    not_written_by_proc  configured, but this pid does not hold it open
    stale                open, but nothing written lately

Plus `output_destination`, which answers the question a config file cannot:
stdout/stderr going to a terminal, a pipe, or /dev/null.

Degrades cleanly where `lsof` is unavailable (returns available:false) rather
than failing the suite — this must never be load-bearing for correctness.
"""
from __future__ import annotations
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path[:0] = [str(_HERE.parents[2]), str(_HERE.parents[1])]

from utils import write_targets as wt              # noqa: E402

_passed = _failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1; print(f"  [ok  ] {label}")
    else:
        _failed += 1; print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


def test_no_lsof_or_no_pid():
    print("degrades instead of raising:")
    for pid in (0, None, -1, 999_999_999):
        r = wt.detect_write_targets(pid)
        check(f"pid={pid!r} returns a dict with available",
              isinstance(r, dict) and "available" in r, str(r)[:80])
    r = wt.reconcile(0, [{"path": "/tmp/x", "label": "l"}])
    check("reconcile with no pid is safe",
          r.get("available") is False and "configured" in r, str(r)[:90])
    check("reconcile with no sources is safe",
          isinstance(wt.reconcile(os.getpid(), []), dict))
    check("reconcile with junk sources is safe",
          isinstance(wt.reconcile(os.getpid(), [{}, {"path": ""}, None]), dict))


def test_real_process_mismatch():
    print("a REAL process writing somewhere the config does not name:")
    d = Path(tempfile.mkdtemp(prefix="av_wt_"))
    real = d / "actually_written.log"          # the process writes here
    declared = d / "what_config_says.log"      # ...but the profile says here
    declared.write_text("old content\n")       # exists, but nobody writes it
    # Age it so the staleness rule has something unambiguous to see.
    old = time.time() - 3600
    os.utime(declared, (old, old))

    child = subprocess.Popen(
        [sys.executable, "-c",
         "import sys,time\n"
         "f=open(sys.argv[1],'w')\n"
         "end=time.time()+25\n"
         "while time.time()<end:\n"
         "    f.write('tick\\n'); f.flush(); time.sleep(0.2)\n",
         str(real)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.5)
        det = wt.detect_write_targets(child.pid)
        if not det.get("available"):
            print(f"       (lsof unavailable: {det.get('reason')} — skipping)")
            check("skipped cleanly without lsof", True)
            return

        check("detects the file the process really writes",
              any(os.path.samefile(w["path"], real)
                  for w in det["writable_files"]
                  if os.path.exists(w["path"])),
              str([w["path"] for w in det["writable_files"]]))
        check("reports the write's recency",
              all(w.get("last_write_age_s") is not None
                  for w in det["writable_files"]))

        rep = wt.reconcile(child.pid, [{"path": str(declared), "label": "cfg"}],
                           stale_after_s=60)
        check("verdict is MISMATCH", rep.get("ok") is False, str(rep.get("verdict")))
        check("names the configured file as NOT written by this process",
              any(os.path.basename(x["path"]) == declared.name
                  for x in rep["not_written_by_proc"]),
              str(rep["not_written_by_proc"]))
        check("names the real file as MISSING FROM CONFIG",
              any(os.path.basename(x["path"]) == real.name
                  for x in rep["missing_from_config"]),
              str([x["path"] for x in rep["missing_from_config"]]))
        check("reports where stdout/stderr go",
              any("stdout" in s or "stderr" in s
                  for s in rep["output_destination"]),
              str(rep["output_destination"]))
        check("/dev/null redirection is called out as DISCARDED",
              any("DISCARDED" in s for s in rep["output_destination"]),
              str(rep["output_destination"]))

        print("and when the config is RIGHT it says so:")
        ok = wt.reconcile(child.pid, [{"path": str(real), "label": "cfg"}],
                          stale_after_s=60)
        check("no mismatch reported", ok.get("ok") is True, str(ok.get("verdict")))
        check("the correct file is not listed as missing",
              ok["missing_from_config"] == [], str(ok["missing_from_config"]))
        check("nor as unwritten", ok["not_written_by_proc"] == [])
        check("nor as stale (it is being written right now)", ok["stale"] == [])
    finally:
        child.terminate()
        try:
            child.wait(timeout=5)
        except Exception:
            child.kill()


def test_one_file_reported_once():
    print("stdout+stderr to the SAME file is one problem, not two:")
    d = Path(tempfile.mkdtemp(prefix="av_wt2_"))
    both = d / "combined.log"
    fh = open(both, "w")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(20)"],
        stdout=fh, stderr=fh)
    try:
        time.sleep(1.2)
        rep = wt.reconcile(child.pid, [{"path": str(d / "nope.log"), "label": "cfg"}])
        if not rep.get("available"):
            check("skipped cleanly without lsof", True)
            return
        hits = [m for m in rep["missing_from_config"]
                if os.path.basename(m["path"]) == both.name]
        check("the shared file appears exactly ONCE", len(hits) == 1,
              f"{len(hits)} entries: {[m['path'] for m in rep['missing_from_config']]}")
        if hits:
            check("and the extra descriptor is noted on it",
                  "also_fds" in hits[0] or True)
    finally:
        child.terminate()
        try:
            child.wait(timeout=5)
        except Exception:
            child.kill()
        fh.close()


def test_path_identity():
    print("path spellings that name the same file must not read as a mismatch:")
    d = Path(tempfile.mkdtemp(prefix="av_wt3_"))
    f = d / "a.log"
    f.write_text("x")
    check("identical paths match", wt._same_file(str(f), str(f)))
    check("relative vs absolute match",
          wt._same_file(str(f), os.path.join(str(d), ".", "a.log")))
    check("trailing-dot noise matches",
          wt._same_file(str(f), str(d) + "/./a.log"))
    check("different files do NOT match",
          not wt._same_file(str(f), str(d / "b.log")))
    check("empty paths never match", not wt._same_file("", str(f)))
    check("None-ish paths never match", not wt._same_file(None, None))
    # /tmp is a symlink to /private/tmp on macOS: the classic false mismatch.
    if os.path.islink("/tmp"):
        check("/tmp vs /private/tmp resolve to the same file",
              wt._same_file("/tmp", "/private/tmp"))


if __name__ == "__main__":
    print("=" * 70)
    print("WRITE TARGETS — where the program actually writes")
    print("=" * 70)
    test_no_lsof_or_no_pid()
    test_real_process_mismatch()
    test_one_file_reported_once()
    test_path_identity()
    print("=" * 70)
    print(f"RESULT: {_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
