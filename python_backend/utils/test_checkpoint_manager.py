"""
Checkpoint = the only thing that could make "nothing is permanently lost" true.
================================================================================
The GUI used to tell the user, on the Clear Data dialog, that every clear writes
a numbered checkpoint they can restore from. The single property under test here
is the one that sentence needed and did not have:

    EVERY BYTE THIS MODULE DESTROYS IS RECOVERABLE FROM THE CHECKPOINT.

It was not. The delete loop iterated the list of files the module MEANT to
archive; every `tar.add` was wrapped in `except: print warning`; the tarball was
never read back. Three mundane routes therefore destroyed data the user had been
told was saved, and one of them was deterministic:

  * a `tar.add` that raised (permission denied, file vanished mid-walk — the
    capture loop writes into these same folders, a locked file on Windows) still
    got unlinked;
  * `_arcname` flattened everything outside AV_ROOT to `<profile>/<basename>`,
    so `log/stats/session.log` and `log/crashes/session.log` were one member
    name — tar kept both, extraction kept one, both originals were deleted;
  * a project with its own `snapshots/` had every *.png in it collected and
    deleted, including images AgentVision never created.

Measured on the fixture below before the fix: 11 files destroyed, 3 of them
unrecoverable. The fix is the same one that closed the 1,500-frame incident in
retention.py — check the DISK for the artefact that proves preservation (the
member inside the tarball) instead of trusting an in-memory list.
"""
from __future__ import annotations
import json
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve()
sys.path[:0] = [str(_HERE.parents[2]), str(_HERE.parents[1])]

from utils import checkpoint_manager as cm      # noqa: E402

_passed = _failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1; print(f"  [ok  ] {label}")
    else:
        _failed += 1; print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


# ── fixture ───────────────────────────────────────────────────────────────────

class Fixture:
    """A throwaway project + AV_ROOT holding every failure shape at once."""

    def __init__(self, tmp: Path, *, unreadable: bool = True):
        self.proj = tmp / "project"
        self.av = tmp / "av_root"
        (self.proj / "snapshots").mkdir(parents=True)
        (self.proj / "log" / "stats" / "day1").mkdir(parents=True)
        (self.proj / "log" / "crashes").mkdir(parents=True)
        (self.av / "snapshots" / "probe").mkdir(parents=True)

        self.w(self.proj / "snapshots" / "frame_00001.png", b"PNG-1" * 40)
        self.w(self.proj / "snapshots" / "frame_00001_frame.json", b'{"seq":1}')
        # The user's own image. AgentVision did not create it.
        self.logo = self.proj / "snapshots" / "company_logo.png"
        self.w(self.logo, b"USER-OWNED" * 40)

        self.log_txt = self.proj / "log" / "log.txt"
        self.w(self.log_txt, b"stdout\n" * 20)
        self.w(self.proj / "log" / "actions.jsonl", b'{"a":1}\n' * 20)
        self.seq = self.proj / "log" / ".av_frame_seq"
        self.w(self.seq, b"42")

        # Same basename in three places — the collision.
        self.stats_log = self.proj / "log" / "stats" / "session.log"
        self.crash_log = self.proj / "log" / "crashes" / "session.log"
        self.nested_log = self.proj / "log" / "stats" / "day1" / "session.log"
        self.w(self.stats_log, b"STATS-A\n" * 10)
        self.w(self.crash_log, b"CRASH-B\n" * 10)
        self.w(self.nested_log, b"NESTED-C\n" * 10)

        # A file tar.add cannot read.
        self.locked = self.proj / "log" / "stats" / "locked.log"
        self.w(self.locked, b"LOCKED-D\n" * 10)
        if unreadable:
            os.chmod(self.locked, 0o000)

        self.w(self.av / "snapshots" / "probe" / "frame_00002.png", b"PNG-2" * 40)

        self.before = self.snapshot()
        self.profile = SimpleNamespace(name="probe", project_root=str(self.proj))

    @staticmethod
    def w(p: Path, body: bytes) -> None:
        p.write_bytes(body)

    def snapshot(self) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for root in (self.proj, self.av):
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    try:
                        out[str(p)] = p.read_bytes()
                    except Exception:
                        out[str(p)] = b"<unreadable>"
        return out

    def run(self):
        cm.AV_ROOT = self.av
        cm.CHECKPOINTS_ROOT = self.av / "checkpoints"
        return cm.save_checkpoint(self.profile, verbose=False)

    def unrecoverable(self, ckpt: str | None) -> list[str]:
        """Paths whose bytes are no longer on disk AND cannot be got back out of
        the checkpoint. Truncation counts as destruction: the path surviving
        empty is no comfort if the content is gone."""
        after = self.snapshot()
        destroyed = [p for p, body in self.before.items() if after.get(p) != body]
        effective: dict[str, bytes] = {}
        dupes: set[str] = set()
        if ckpt and Path(ckpt).exists():
            with tarfile.open(ckpt, "r:gz") as tar:
                for m in tar.getmembers():
                    if not m.isfile():
                        continue
                    if m.name in effective:
                        dupes.add(m.name)
                    f = tar.extractfile(m)
                    if f is not None:
                        effective[m.name] = f.read()      # last member wins
        lost = []
        for p in destroyed:
            arc = cm._arcname(Path(p), "probe", self.proj)
            if effective.get(arc) != self.before[p]:
                lost.append(p)
        self.duplicate_members = sorted(dupes)
        return sorted(lost)

    def cleanup(self) -> None:
        try:
            os.chmod(self.locked, 0o600)
        except Exception:
            pass


def _fixture(fn):
    """Run fn(fix) inside a fresh temp dir, restoring module globals after."""
    av_root, ckpt_root = cm.AV_ROOT, cm.CHECKPOINTS_ROOT
    tmp = Path(tempfile.mkdtemp(prefix="av_ckpt_test_"))
    fix = Fixture(tmp)
    try:
        return fn(fix)
    finally:
        fix.cleanup()
        cm.AV_ROOT, cm.CHECKPOINTS_ROOT = av_root, ckpt_root
        shutil.rmtree(tmp, ignore_errors=True)


# ── the promise ───────────────────────────────────────────────────────────────

def test_nothing_destroyed_is_unrecoverable():
    print("the GUI's promise — every destroyed byte is in the checkpoint:")

    def body(fix: Fixture):
        ckpt = fix.run()
        check("a checkpoint was written", bool(ckpt) and Path(ckpt).exists())
        lost = fix.unrecoverable(ckpt)
        check("NOTHING was destroyed without a recoverable copy",
              lost == [], f"{len(lost)} unrecoverable: {lost}")
        check("no two files share one archive member name",
              fix.duplicate_members == [], str(fix.duplicate_members))
    _fixture(body)


def test_unarchivable_file_is_not_deleted():
    print("a file tar.add could not read is kept, not deleted:")

    def body(fix: Fixture):
        fix.run()
        check("unreadable file still on disk", fix.locked.exists())
        check("its bytes are unchanged",
              fix.locked.stat().st_size == len(b"LOCKED-D\n" * 10))
    _fixture(body)


def test_colliding_basenames_are_distinct_members():
    print("same basename in stats/ and crashes/ archives to distinct members:")

    def body(fix: Fixture):
        ckpt = fix.run()
        with tarfile.open(ckpt, "r:gz") as tar:
            names = [m.name for m in tar.getmembers() if m.isfile()]
        check("three session.log members, all distinct",
              len([n for n in names if n.endswith("session.log")]) == 3
              and len(set(names)) == len(names), str(sorted(names)))
        bodies = {}
        with tarfile.open(ckpt, "r:gz") as tar:
            for m in tar.getmembers():
                if m.name.endswith("session.log"):
                    bodies[m.name] = tar.extractfile(m).read()
        check("each collision victim kept its OWN content",
              sorted(bodies.values()) == sorted(
                  [b"STATS-A\n" * 10, b"CRASH-B\n" * 10, b"NESTED-C\n" * 10]))
    _fixture(body)


def test_a_file_that_changes_after_archiving_is_kept():
    print("a file that changed after it was archived is not deleted:")
    # Verified directly against _verify_archived: the archive copy is no longer
    # this file, so this file is not the archive's to spend.
    tmp = Path(tempfile.mkdtemp(prefix="av_ckpt_grow_"))
    try:
        live = tmp / "growing.log"
        live.write_bytes(b"first\n")
        ckpt = tmp / "c.tar.gz"
        with tarfile.open(ckpt, "w:gz") as tar:
            tar.add(live, arcname="p/growing.log")
        live.write_bytes(b"first\nsecond appended after the archive\n")
        verified, unverified = cm._verify_archived(ckpt, [(live, "p/growing.log")])
        check("not verified", verified == [], str(verified))
        check("reason names the change",
              len(unverified) == 1 and "changed after archiving" in unverified[0][1],
              str(unverified))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_unopenable_checkpoint_verifies_nothing():
    print("a checkpoint that cannot be re-opened authorises no deletion:")
    tmp = Path(tempfile.mkdtemp(prefix="av_ckpt_bad_"))
    try:
        f = tmp / "x.log"
        f.write_bytes(b"data")
        bad = tmp / "truncated.tar.gz"
        bad.write_bytes(b"this is not a gzip stream")
        verified, unverified = cm._verify_archived(bad, [(f, "p/x.log")])
        check("verified set is empty", verified == [])
        check("reason says the checkpoint could not be re-opened",
              len(unverified) == 1
              and "could not be re-opened" in unverified[0][1],
              str(unverified))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── ownership ─────────────────────────────────────────────────────────────────

def test_only_agentvision_snapshots_are_touched():
    print("a project's own images in snapshots/ are not AgentVision's to take:")

    def body(fix: Fixture):
        ckpt = fix.run()
        check("the user's company_logo.png is still on disk", fix.logo.exists())
        check("and its bytes are untouched",
              fix.logo.read_bytes() == b"USER-OWNED" * 40)
        with tarfile.open(ckpt, "r:gz") as tar:
            names = [m.name for m in tar.getmembers()]
        check("it was not even collected into the archive",
              not any("company_logo" in n for n in names), str(names))
    _fixture(body)


def test_frame_counter_is_archived_but_never_removed():
    print(".av_frame_seq survives — deleting it makes the next run overwrite:")

    def body(fix: Fixture):
        ckpt = fix.run()
        check("counter still on disk with its value", fix.seq.exists()
              and fix.seq.read_bytes() == b"42")
        with tarfile.open(ckpt, "r:gz") as tar:
            names = [m.name for m in tar.getmembers()]
        check("but it IS in the checkpoint",
              any(n.endswith(".av_frame_seq") for n in names), str(names))
    _fixture(body)


def test_live_logs_are_truncated_not_unlinked():
    print("live logs are truncated, so the program's open fd keeps working:")

    def body(fix: Fixture):
        fix.run()
        check("log.txt still exists", fix.log_txt.exists())
        check("and is empty", fix.log_txt.stat().st_size == 0,
              str(fix.log_txt.stat().st_size))
    _fixture(body)


# ── auditability ──────────────────────────────────────────────────────────────

def test_removal_manifest_records_what_actually_happened():
    print("a manifest of what was REMOVED is written next to the checkpoint:")

    def body(fix: Fixture):
        ckpt = fix.run()
        side = Path(ckpt).with_name("checkpoint_00001.removed.json")
        check("manifest sidecar exists", side.exists(), str(side))
        if not side.exists():
            return
        data = json.loads(side.read_text())
        check("it lists deleted paths",
              len(data["verified_in_archive_then_deleted"]) > 0)
        check("it lists truncated paths",
              len(data["verified_in_archive_then_truncated"]) == 2,
              str(data["verified_in_archive_then_truncated"]))
        kept = data["kept_because_not_provably_archived"]
        check("it names the kept file AND the reason",
              any("locked.log" in k["path"] and "Permission" in k["reason"]
                  for k in kept), str(kept))
    _fixture(body)


def test_numbering_never_overwrites_an_existing_archive():
    print("freeing space by deleting an old checkpoint must not doom the next:")
    tmp = Path(tempfile.mkdtemp(prefix="av_ckpt_num_"))
    try:
        for i in (1, 2, 3):
            (tmp / f"checkpoint_{i:05d}.tar.gz").write_text(f"ARCHIVE-{i}")
        (tmp / "checkpoint_00001.tar.gz").unlink()      # the natural thing to do
        nxt = cm._next_checkpoint_number(tmp)
        check("the next number is past the highest, not the count",
              nxt == 4, str(nxt))
        check("so it does not name an existing archive",
              not (tmp / f"checkpoint_{nxt:05d}.tar.gz").exists())
        # A hand-named archive must not shift the numbering onto a real one.
        (tmp / "checkpoint_manual.tar.gz").write_text("hand-made")
        check("a non-numeric name is ignored, not counted",
              cm._next_checkpoint_number(tmp) == 4,
              str(cm._next_checkpoint_number(tmp)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_an_existing_archive_is_never_truncated():
    print("the filesystem, not the numbering, is the final guarantee:")

    def body(fix: Fixture):
        cm.AV_ROOT = fix.av
        cm.CHECKPOINTS_ROOT = fix.av / "checkpoints"
        folder = cm.CHECKPOINTS_ROOT / "probe"
        folder.mkdir(parents=True, exist_ok=True)
        # Squat on the name the next checkpoint would take.
        squat = folder / "checkpoint_00001.tar.gz"
        squat.write_text("SOMEONE ELSE'S GOOD ARCHIVE")
        ckpt = cm.save_checkpoint(fix.profile, verbose=False)
        check("the squatted archive is byte-identical afterwards",
              squat.read_text() == "SOMEONE ELSE'S GOOD ARCHIVE",
              squat.read_text()[:40])
        check("and the new checkpoint took a different name",
              ckpt is None or Path(ckpt).name != "checkpoint_00001.tar.gz",
              str(ckpt))
    _fixture(body)


def test_archive_only_run_destroys_nothing():
    print("the automatic on-close path is archive-only — it destroys nothing:")

    def body(fix: Fixture):
        cm.AV_ROOT = fix.av
        cm.CHECKPOINTS_ROOT = fix.av / "checkpoints"
        ckpt = cm.save_checkpoint(fix.profile, verbose=False,
                                  delete_originals=False)
        check("a checkpoint was still written", bool(ckpt) and Path(ckpt).exists())
        after = fix.snapshot()
        changed = [p for p, b in fix.before.items() if after.get(p) != b]
        check("not one byte on disk changed", changed == [], str(changed))
    _fixture(body)


def test_save_all_checkpoints_defaults_to_archive_only():
    print("save_all_checkpoints (the on-close bulk sweep) deletes nothing:")

    def body(fix: Fixture):
        cm.AV_ROOT = fix.av
        cm.CHECKPOINTS_ROOT = fix.av / "checkpoints"
        cm.save_all_checkpoints({"probe": fix.profile}, verbose=False)
        after = fix.snapshot()
        changed = [p for p, b in fix.before.items() if after.get(p) != b]
        check("not one byte on disk changed", changed == [], str(changed))
    _fixture(body)


def test_second_checkpoint_does_not_overwrite_the_first():
    print("numbering is sequential — a second save adds, never replaces:")

    def body(fix: Fixture):
        first = fix.run()
        # re-create some data so there is something to archive again
        (fix.proj / "snapshots" / "frame_00003.png").write_bytes(b"PNG-3" * 40)
        second = fix.run()
        check("first checkpoint still exists", Path(first).exists())
        check("second has a new number", first != second, f"{first} == {second}")
    _fixture(body)


if __name__ == "__main__":
    print("=" * 70)
    print("CHECKPOINT — VERIFY THE ARCHIVE BEFORE DESTROYING THE ORIGINAL")
    print("=" * 70)
    test_nothing_destroyed_is_unrecoverable()
    test_unarchivable_file_is_not_deleted()
    test_colliding_basenames_are_distinct_members()
    test_a_file_that_changes_after_archiving_is_kept()
    test_unopenable_checkpoint_verifies_nothing()
    test_only_agentvision_snapshots_are_touched()
    test_frame_counter_is_archived_but_never_removed()
    test_live_logs_are_truncated_not_unlinked()
    test_removal_manifest_records_what_actually_happened()
    test_numbering_never_overwrites_an_existing_archive()
    test_an_existing_archive_is_never_truncated()
    test_archive_only_run_destroys_nothing()
    test_save_all_checkpoints_defaults_to_archive_only()
    test_second_checkpoint_does_not_overwrite_the_first()
    print("=" * 70)
    print(f"RESULT: {_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
