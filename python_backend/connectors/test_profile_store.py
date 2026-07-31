"""
profiles.json is hand-authored work, and a failed READ used to authorise a WRITE.
================================================================================
profiles.json holds things nothing else can re-derive: project roots, log_sources
with pinned adapters, crop rectangles, capture targets, and human-written notes.

`load_profiles()` swallowed every read/parse failure with `except Exception: pass`
and returned BUILT-INS ONLY; `cli._load_profiles()` returned `{}` on the same
failure. Neither reported it, and neither consulted the disk again. The next
Save, Delete or Set-Active then wrote that view over the file — so one unrelated
failure to load permanently erased every profile the user had configured.

That is the same defect as the 1,500-frame incident with the roles swapped: a
collection is empty because a LOAD failed, and the emptiness is trusted over the
file that is still sitting on disk.

Also pinned here: the write is atomic. The old bare `write_text` truncates in
place, so a crash mid-write left truncated JSON — which is exactly the state the
loader turned into "there are no custom profiles".

Every check below runs against a temp file. The real profiles.json is never
touched.
"""
from __future__ import annotations
import json
import shutil
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path[:0] = [str(_HERE.parents[2]), str(_HERE.parents[1])]

from connectors import program_connector as pc     # noqa: E402

_passed = _failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1; print(f"  [ok  ] {label}")
    else:
        _failed += 1; print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


class TempStore:
    """Point the module's PROFILES_FILE at a scratch file."""

    def __init__(self, body: str | None = None):
        self.dir = Path(tempfile.mkdtemp(prefix="av_profiles_"))
        self.path = self.dir / "profiles.json"
        if body is not None:
            self.path.write_text(body)
        self._saved = pc.PROFILES_FILE

    def __enter__(self):
        pc.PROFILES_FILE = self.path
        return self

    def __exit__(self, *a):
        pc.PROFILES_FILE = self._saved
        pc._LAST_LOAD_OK, pc._LAST_LOAD_ERROR = True, ""
        shutil.rmtree(self.dir, ignore_errors=True)


_GOOD = json.dumps({
    "myproj": {"name": "myproj", "display_name": "My Project",
               "project_root": "/tmp/myproj",
               "log_sources": [{"path": "/tmp/myproj/log/a.jsonl",
                                "adapter": "jsonl", "label": "events"}],
               "notes": "hand-written notes that nothing can regenerate"},
}, indent=2)


def test_a_good_file_round_trips():
    print("the ordinary path still works:")
    with TempStore(_GOOD) as s:
        profs = pc.load_profiles()
        check("the custom profile loaded", "myproj" in profs)
        check("load is reported as clean", pc.last_load_ok()[0] is True)
        pc.save_profiles(profs)
        again = json.loads(s.path.read_text())
        check("it survived the round trip", "myproj" in again, str(list(again)))
        check("its notes survived",
              "hand-written" in again["myproj"].get("notes", ""))


def test_unparseable_file_blocks_the_save():
    print("truncated JSON must not become 'there are no profiles':")
    with TempStore('{"myproj": {"name": "myproj", "project_') as s:
        before = s.path.read_text()
        profs = pc.load_profiles()
        check("the load is reported as FAILED", pc.last_load_ok()[0] is False,
              str(pc.last_load_ok()))
        check("and the reason is available", "parse" in pc.last_load_ok()[1]
              or "Expecting" in pc.last_load_ok()[1], pc.last_load_ok()[1])
        refused = False
        try:
            pc.save_profiles(profs)
        except pc.ProfileSaveRefused:
            refused = True
        check("saving is REFUSED, not silently completed", refused)
        check("the file on disk is untouched", s.path.read_text() == before)


def test_a_single_bad_entry_blocks_the_save():
    print("one unreadable entry must not drop the others:")
    body = json.dumps({"good": {"name": "good"}, "bad": "not-an-object"})
    with TempStore(body) as s:
        before = s.path.read_text()
        profs = pc.load_profiles()
        check("the load is reported as FAILED", pc.last_load_ok()[0] is False,
              str(pc.last_load_ok()))
        refused = False
        try:
            pc.save_profiles(profs)
        except pc.ProfileSaveRefused:
            refused = True
        check("saving is REFUSED", refused)
        check("'bad' is still in the file", "bad" in s.path.read_text())
        check("nothing was rewritten", s.path.read_text() == before)


def test_force_is_the_only_way_past():
    print("an explicit force can still replace the file wholesale:")
    with TempStore('{"broken') as s:
        pc.load_profiles()
        pc.save_profiles({}, force=True)
        check("force wrote", s.path.read_text().strip() == "{}",
              s.path.read_text())


def test_deleting_a_profile_is_still_allowed():
    print("a clean load followed by a deliberate removal is not blocked:")
    two = json.dumps({"a": {"name": "a"}, "b": {"name": "b"}})
    with TempStore(two) as s:
        profs = pc.load_profiles()
        profs.pop("b", None)
        pc.save_profiles({k: v for k, v in profs.items()
                          if k not in pc.BUILTIN_PROFILES})
        on_disk = json.loads(s.path.read_text())
        check("'b' is gone as asked", "b" not in on_disk, str(list(on_disk)))
        check("'a' is still there", "a" in on_disk, str(list(on_disk)))


def test_write_is_atomic_and_keeps_one_generation():
    print("the write cannot leave a half-written file, and keeps a .bak:")
    with TempStore(_GOOD) as s:
        profs = pc.load_profiles()
        pc.save_profiles(profs)
        bak = s.path.with_suffix(".json.bak")
        check("a .bak of the previous content exists", bak.exists(), str(bak))
        check("the .bak parses", isinstance(json.loads(bak.read_text()), dict))
        check("no .tmp was left behind",
              not s.path.with_suffix(".json.tmp").exists())


def test_missing_file_is_not_a_failure():
    print("no profiles.json yet is a clean state, not an error:")
    with TempStore(None) as s:
        profs = pc.load_profiles()
        check("built-ins are present", len(profs) > 0)
        check("load is clean", pc.last_load_ok()[0] is True)
        pc.save_profiles(profs)
        check("and saving works", s.path.exists())


# ── the CLI keeps its own copy of this logic; it needs the same rule ──────────

def test_cli_loader_has_the_same_rule():
    print("cli.py's duplicate implementation refuses too:")
    try:
        import cli as cli_mod
    except Exception as exc:
        print(f"  [skip] cli import unavailable: {exc}")
        return
    d = Path(tempfile.mkdtemp(prefix="av_cli_profiles_"))
    saved = cli_mod.PROFILES_FILE
    try:
        cli_mod.PROFILES_FILE = d / "profiles.json"
        cli_mod.PROFILES_FILE.write_text('{"oops": ')
        before = cli_mod.PROFILES_FILE.read_text()
        got = cli_mod._load_profiles()
        check("a broken file loads as empty", got == {})
        refused = False
        try:
            cli_mod._save_profiles(got)
        except RuntimeError:
            refused = True
        check("but saving that emptiness is REFUSED", refused)
        check("the file is untouched",
              cli_mod.PROFILES_FILE.read_text() == before)
    finally:
        cli_mod.PROFILES_FILE = saved
        cli_mod._LAST_LOAD_OK, cli_mod._LAST_LOAD_ERROR = True, ""
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    print("=" * 70)
    print("PROFILE STORE — A FAILED READ MAY NOT AUTHORISE A WRITE")
    print("=" * 70)
    test_a_good_file_round_trips()
    test_unparseable_file_blocks_the_save()
    test_a_single_bad_entry_blocks_the_save()
    test_force_is_the_only_way_past()
    test_deleting_a_profile_is_still_allowed()
    test_write_is_atomic_and_keeps_one_generation()
    test_missing_file_is_not_a_failure()
    test_cli_loader_has_the_same_rule()
    print("=" * 70)
    print(f"RESULT: {_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
