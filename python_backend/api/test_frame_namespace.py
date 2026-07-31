#!/usr/bin/env python3
"""A frame sequence number identifies a frame only WITHIN a profile.

`_frames` and `_frames_on_disk` are keyed by sequence number alone, and startup
hydrated every profile into them, so two programs' frame 1 were the same key.
Whichever profile lost the dict race had that frame silently unreachable:
`/frame/1` answered with the other program's image, and every derived read had to
re-filter by profile afterwards to undo the mixing.

Measured on this machine: profiles `a game bot` and `sharpemu` both own a frame 1
(a game bot has exactly one frame; sharpemu has 12,921).

The index now holds ONE profile — the active one — and the rest are recorded in
`_foreign` so they stay addressable and countable. /frames/collisions is where
the full picture lives.

Run:  .venv/bin/python python_backend/api/test_frame_namespace.py
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE.parent.parent))
sys.path.insert(0, str(_HERE.parent.parent.parent))

_fails = []


def check(name, cond, detail=""):
    print(f"  [{'ok ' if cond else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        _fails.append(name)


def _write_frame(folder: Path, seq: int, label: str, ts_ms: float):
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"frame_{seq:05d}"
    (folder / f"{stem}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (folder / f"{stem}_frame.json").write_text(json.dumps({
        "sequence": seq, "timestamp_ms": ts_ms,
        "timestamp": "2026-07-30T00:00:00.000Z",
        "summary": f"{label} frame {seq}",
        "program": {"name": label, "running": True},
        "capture_meta": {}, "tags": [label],
    }))
    return folder / f"{stem}_frame.json"


def main():
    from connectors.program_connector import (ProgramProfile, save_profiles,
                                              load_profiles)

    tmp = Path(tempfile.mkdtemp(prefix="av_ns_"))
    roots = {"nsalpha": tmp / "alpha", "nsbeta": tmp / "beta"}
    for r in roots.values():
        r.mkdir(parents=True)

    keep = load_profiles()
    profs = dict(keep)
    for name, root in roots.items():
        profs[name] = ProgramProfile(
            name=name, display_name=name.upper(), project_root=str(root),
            process_name=f"nonexistent_{name}_xyz")
    save_profiles(profs)

    now = time.time() * 1000.0
    paths = {}
    for i, (name, root) in enumerate(roots.items()):
        folder = root / "agentvision" / name
        # BOTH profiles own a frame 1 and a frame 2 — the exact collision.
        paths[name] = [_write_frame(folder, 1, name, now - 5000 + i),
                       _write_frame(folder, 2, name, now - 4000 + i)]
    # …and one frame ONLY alpha owns, to test the cross-profile 404 below.
    _write_frame(roots["nsalpha"] / "agentvision" / "nsalpha", 3, "nsalpha",
                 now - 3000)

    os.environ["AGENTVISION_ACTIVE_PROFILE"] = "nsalpha"
    import bridge_server as bs
    # PUT /profiles/active persists the choice to python_backend/active_profile.txt,
    # which is REAL user state — a test must not leave the bridge pointing at its
    # own fixture. Remember it and put it back.
    _active_file = Path(bs.__file__).resolve().parent.parent / "active_profile.txt"
    _prev_active = (_active_file.read_text().strip()
                    if _active_file.exists() else "")
    bs._active_profile_name = "nsalpha"
    bs._collector = None
    with bs._lock:
        bs._frames.clear()
        bs._frames_on_disk.clear()
        bs._latest_frame = None
    bs._hydrate_frame_index()
    client = bs.app.test_client()

    print("with nsalpha active:")
    idx = {**{s: p for s, p in bs._frames_on_disk.items()},
           **{s: (f.get("json_sidecar") or "") for s, f in bs._frames.items()}}
    check("frame 1 in the index is ALPHA's frame 1",
          "alpha" in str(idx.get(1, "")), str(idx.get(1)))
    check("BETA's frame 1 is held separately, not lost",
          ("nsbeta", 1) in bs._foreign, str(sorted(bs._foreign)[:4]))
    r = client.get("/frames/collisions")
    col = r.get_json() or {}
    check("/frames/collisions -> 200", r.status_code == 200, str(r.status_code))
    check("the collision is REPORTED rather than resolved by dict order",
          col.get("colliding_total", 0) >= 2
          and "nsbeta" in (col.get("colliding_seqs_by_profile") or {}),
          json.dumps(col.get("colliding_seqs_by_profile")))
    r = client.get("/frame/1")
    body = r.get_json() or {}
    check("GET /frame/1 returns ALPHA's frame",
          r.status_code == 200 and "nsalpha" in json.dumps(body.get("summary")),
          f"HTTP {r.status_code} summary={body.get('summary')!r}")

    print("after switching the active profile to nsbeta:")
    r = client.put("/profiles/active", json={"name": "nsbeta"})
    sw = r.get_json() or {}
    check("the switch reports rebuilding the frame index",
          r.status_code == 200 and sw.get("frames_indexed", 0) >= 2,
          f"HTTP {r.status_code} frames_indexed={sw.get('frames_indexed')}")
    idx2 = {**{s: p for s, p in bs._frames_on_disk.items()},
            **{s: (f.get("json_sidecar") or "") for s, f in bs._frames.items()}}
    check("frame 1 in the index is now BETA's frame 1",
          "beta" in str(idx2.get(1, "")), str(idx2.get(1)))
    check("ALPHA's frames are now the foreign ones",
          ("nsalpha", 1) in bs._foreign and ("nsbeta", 1) not in bs._foreign,
          str(sorted(bs._foreign)[:4]))
    r = client.get("/frame/1")
    body = r.get_json() or {}
    check("the SAME seq now addresses BETA's frame, and says so",
          r.status_code == 200 and "nsbeta" in json.dumps(body.get("summary")),
          f"HTTP {r.status_code} summary={body.get('summary')!r}")

    print("a cross-profile seq 404 explains itself:")
    r = client.get("/frame/3")            # nsbeta active; only nsalpha owns a 3
    check("requesting another profile's seq is a 404, not the wrong image",
          r.status_code == 404, f"HTTP {r.status_code}")
    body = r.get_json() or {}
    check("the 404 names the profile that owns it and the switch to make",
          "nsalpha" in (body.get("seq_exists_in_profiles") or [])
          and "av_set_active_profile" in (body.get("fix") or ""),
          json.dumps(body)[:200])

    # ── the frame COUNTER is global, but Clear Data is per-profile ───────────
    # `/capture/reset-counter` set `_auto_engine.frame_count = 0` and stopped
    # there. The GUI calls it after clearing ONE profile's folder, so: clear
    # alpha, switch to beta, and the next shutter writes frame_00001.png over
    # beta's frame 1, taking its sidecar, thumb and diff with it. The machinery
    # to prevent that (resume_counter_from_disk, whose own docstring describes
    # this bug) had exactly one caller: process start. It is now called by the
    # reset route and by the profile switch, so the DISK decides the counter.
    print("resetting the counter cannot re-number over frames that still exist:")
    r = client.post("/capture/reset-counter")
    body = r.get_json() or {}
    check("/capture/reset-counter -> 200", r.status_code == 200, str(r.status_code))
    check("the counter was re-seeded from disk, not left at 0",
          bs._auto_engine.frame_count >= 3,
          f"frame_count={bs._auto_engine.frame_count}")
    check("so the next frame cannot land on an existing one",
          body.get("next_frame", 0) > 3, str(body.get("next_frame")))

    # An _archive/*.webp is the ONLY surviving pixels of an evicted frame, and it
    # uses the same frame numbering. The high-water scan reads it too, so a later
    # eviction cannot re-archive over it.
    # Numbered ABOVE the current high-water (the scan covers every configured
    # profile, so a fixed number could pass on somebody else's frames).
    bs._auto_engine.frame_count = 0
    baseline = bs._auto_engine.resume_counter_from_disk()
    archived_seq = baseline + 5000
    arch = roots["nsbeta"] / "agentvision" / "nsbeta" / "_archive"
    arch.mkdir(parents=True, exist_ok=True)
    (arch / f"frame_{archived_seq:05d}.webp").write_bytes(b"ONLY-SURVIVING-PIXELS")
    bs._auto_engine.frame_count = 0
    after = bs._auto_engine.resume_counter_from_disk()
    check("an archived webp raises the high-water mark too",
          after == archived_seq,
          f"baseline={baseline} archived={archived_seq} after={after}")
    # And prove the archive is the only reason: remove it, re-scan, drop back.
    (arch / f"frame_{archived_seq:05d}.webp").unlink()
    bs._auto_engine.frame_count = 0
    check("and it was the archive doing it, not something else",
          bs._auto_engine.resume_counter_from_disk() == baseline,
          f"expected {baseline}")

    # ── cleanup: restore the real persisted state this test moved ────────────
    save_profiles(keep)
    if _prev_active:
        _active_file.write_text(_prev_active)
        bs._active_profile_name = _prev_active
    with bs._lock:
        bs._frames.clear()
        bs._frames_on_disk.clear()
        bs._foreign.clear()
        bs._latest_frame = None
    check("the test restored the bridge's persisted active profile",
          (_active_file.read_text().strip() if _active_file.exists() else "")
          == _prev_active, f"{_prev_active!r}")

    print()
    if _fails:
        print(f"{len(_fails)} failed: " + "; ".join(_fails))
        return 1
    print("0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
