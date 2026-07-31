"""
Regression: a USER adapter must never SHADOW a BUILT-IN adapter.
================================================================================
register_adapter() is idempotent BY NAME — a re-register REPLACES the prior
instance. A user adapter reusing a built-in's name therefore used to silently
disable that built-in (registry size + duplicate count both still looked
healthy, but the built-in's format fell through to `structural`). That really
happened: a user adapter named `sharpemu` (SharpEmu's bracketed channel-tag
lines) replaced the built-in `sharpemu` (SharpEmu's FileLogSink format) and
broke test_log_adapters + test_adapters_batch1.

These tests pin the fix: the add path REJECTS a built-in's name, and two
distinctly-named adapters for the same program coexist happily.
"""
from __future__ import annotations
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path[:0] = [str(_HERE.parents[2]), str(_HERE.parents[1])]

from connectors import log_adapters as la                      # noqa: E402
from connectors.adapters import user_adapters as ua            # noqa: E402
from connectors.adapters.user_adapters import register_from_spec  # noqa: E402

_passed = _failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [ok  ] {label}")
    else:
        _failed += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


# The user's real-world case, reduced to its essentials.
CHANNEL_SAMPLE = "[METAL] guestgpu.present src=0xB820000 ok=True"
FILESINK_SAMPLE = "[23:44:01.123] [INFO] [Cpu] Interpreter.cs:412 block compiled"

CHANNEL_SPEC = {
    "family": "sharpemu", "language": "csharp",
    "detect": {"regex": r"^\s*\[(?:METAL|LOADER|RUNTIME|HLE|GUESTGPU|PAD|FLIP)\]"},
    "extract": {"regex": r"^\s*\[(?P<source>[A-Z][A-Z0-9_-]*)\]\s*(?P<message>.*)$"},
    "default_level": "INFO", "default_source": "sharpemu",
    "match_scope": "lines", "sample": CHANNEL_SAMPLE,
}


def test_builtin_name_is_rejected():
    print("a user adapter may NOT take a built-in's name:")
    builtins = la.builtin_names()
    check("built-in name set is non-empty", len(builtins) > 100, f"{len(builtins)}")
    check("'sharpemu' is a protected built-in", "sharpemu" in builtins)

    res = ua.validate_spec({**CHANNEL_SPEC, "name": "sharpemu"})
    check("rejected", res["ok"] is False)
    joined = " ".join(res["errors"]).lower()
    check("error says BUILT-IN", "built-in" in joined, joined[:160])
    check("error warns about shadowing", "shadow" in joined, joined[:160])
    check("error suggests an alternative name", "sharpemu_" in joined, joined[:160])

    # A few more built-ins, to prove it is the whole set and not a special case.
    for nm in ("python_logging", "jsonl", "linux_dmesg"):
        r = ua.validate_spec({**CHANNEL_SPEC, "name": nm})
        check(f"'{nm}' also rejected", r["ok"] is False)

    # Reserved fallbacks stay rejected too.
    for nm in ("structural", "raw"):
        r = ua.validate_spec({**CHANNEL_SPEC, "name": nm})
        check(f"reserved '{nm}' rejected", r["ok"] is False)


def test_add_adapter_route_layer_rejects():
    print("add_adapter() refuses to register/persist the colliding name:")
    before = [a.name for a in la.REGISTRY]
    res = ua.add_adapter({**CHANNEL_SPEC, "name": "sharpemu"}, persist=False)
    check("add_adapter ok=False", res.get("ok") is False)
    check("not registered", res.get("registered") in (False, None))
    check("not persisted", res.get("persisted") in (False, None))
    after = [a.name for a in la.REGISTRY]
    check("REGISTRY unchanged", before == after,
          f"{len(before)} -> {len(after)}")
    blt = la.get_adapter("sharpemu")
    check("built-in 'sharpemu' still the registered instance",
          blt is not None and type(blt).__name__ == "SharpEmuAdapter",
          type(blt).__name__ if blt else "missing")


def test_distinct_names_coexist():
    print("both SharpEmu formats coexist under distinct names:")
    names = [a.name for a in la.REGISTRY]
    check("no duplicate names in REGISTRY", len(names) == len(set(names)))
    check("built-in 'sharpemu' present", "sharpemu" in names)

    # The built-in's own format must route to the built-in.
    w, c, _ = la.detect_adapter([FILESINK_SAMPLE])
    check("FileLogSink line -> sharpemu", w.name == "sharpemu", f"{w.name} @ {c}")
    check("  ...at full confidence", c >= 0.9, str(c))

    # Coexistence: a DISTINCTLY-named channel adapter owns the channel format
    # while the built-in keeps its own. Registered HERE from CHANNEL_SPEC rather
    # than assumed present — the developer's own user_adapters.json (where a
    # migrated `sharpemu_channel` used to live) is correctly gitignored, so a
    # clean clone has no such adapter and this test must stand on its own.
    session_only = "sharpemu_channel" not in names
    if session_only:
        register_from_spec({**CHANNEL_SPEC, "name": "sharpemu_channel"},
                           persist=False)
    try:
        w2, c2, _ = la.detect_adapter([CHANNEL_SAMPLE])
        check("channel line -> sharpemu_channel", w2.name == "sharpemu_channel",
              f"{w2.name} @ {c2}")
        w3, _, _ = la.detect_adapter([FILESINK_SAMPLE])
        check("built-in STILL owns its own line alongside it",
              w3.name == "sharpemu", w3.name)
    finally:
        if session_only:
            la.REGISTRY = [a for a in la.REGISTRY if a.name != "sharpemu_channel"]
            la._BY_NAME = {a.name: a for a in la.REGISTRY}

    # A distinctly-named user adapter for a genuinely NEW format registers fine
    # and leaves the built-ins alone. (A duplicate of an already-covered format
    # is correctly refused by the 'already covered' guard, so use a novel one.)
    novel = "<<AVTEST>> chan=gpu state=ok detail=frame-presented"
    spec = {
        "name": "avtest_novel_fmt", "family": "test",
        "detect": {"regex": r"^<<AVTEST>> "},
        "extract": {"regex": r"^<<AVTEST>> chan=(?P<source>\S+)\s+(?P<message>.*)$"},
        "default_level": "INFO", "match_scope": "lines", "sample": novel,
    }
    res = ua.validate_spec(spec)
    check("distinct name + novel format validates ok", res["ok"] is True,
          str(res["errors"])[:200])
    try:
        ua.register_from_spec(spec, persist=False)
        w4, c4, _ = la.detect_adapter([novel])
        check("novel line -> avtest_novel_fmt", w4.name == "avtest_novel_fmt",
              f"{w4.name} @ {c4}")
        w5, _, _ = la.detect_adapter([FILESINK_SAMPLE])
        check("built-in unaffected by the new registration",
              w5.name == "sharpemu", w5.name)
    finally:
        la.REGISTRY = [a for a in la.REGISTRY if a.name != "avtest_novel_fmt"]
        la._BY_NAME = {a.name: a for a in la.REGISTRY}


def test_persisted_store_is_collision_free():
    print("the persisted store carries no built-in names:")
    builtins = la.builtin_names()
    bad = [s.get("name") for s in ua.list_specs() if s.get("name") in builtins]
    check("no persisted spec shadows a built-in", not bad, f"colliding: {bad}")


def test_an_unreadable_store_is_not_replaced():
    """Adding ONE adapter rewrites the WHOLE store from an in-memory list.

    `_load_store()` swallowed every read/parse failure and returned `[]`, so a
    single JSON typo in the user's hand-edited user_adapters.json turned the next
    add into "the store contains exactly the adapter I just added" — every other
    adapter gone. The atomic temp+replace does not help: atomicity only
    guarantees the WRONG CONTENT lands intact. (Live exposure: this machine's
    store holds 2 hand-authored specs.)
    """
    import json as _json, shutil as _shutil, tempfile as _tempfile
    print("a store that cannot be read is not the store's replacement:")
    tmp = Path(_tempfile.mkdtemp(prefix="av_ua_store_"))
    store = tmp / "user_adapters.json"
    saved = ua._STORE
    try:
        ua._STORE = store
        store.write_text('[{"name":"one"},{"name":"two"},{"name":"three"},')
        before = store.read_text()
        specs = ua._load_store()
        check("a broken store loads as empty", specs == [], str(specs))
        check("but the failure is REMEMBERED", ua.store_load_ok()[0] is False,
              str(ua.store_load_ok()))
        refused = False
        try:
            ua._save_store([{"name": "four"}])
        except ua.UserAdapterStoreUnreadable:
            refused = True
        check("so rewriting the store is REFUSED", refused)
        check("and the file is byte-identical", store.read_text() == before)

        # add_adapter must report this precisely: the adapter is live for this
        # session, it just is not persisted. Saying "registration failed" would
        # be wrong in both directions. Use a genuinely novel format, or the
        # already-covered guard rejects it before the store is ever reached.
        novel = "<<AVSTOREPROBE>> chan=gpu state=ok"
        res = ua.add_adapter({
            "name": "avtest_store_probe", "family": "test",
            "detect": {"regex": r"^<<AVSTOREPROBE>> "},
            "extract": {"regex": r"^<<AVSTOREPROBE>> chan=(?P<source>\S+)\s+"
                                 r"(?P<message>.*)$"},
            "default_level": "INFO", "match_scope": "lines", "sample": novel,
        })
        try:
            check("the spec itself was valid", not res.get("errors")
                  or all("refusing to rewrite" in e for e in res["errors"]),
                  str(res.get("errors"))[:200])
            check("add_adapter reports registered but NOT persisted",
                  res.get("registered") is True and res.get("persisted") is False,
                  str({k: res.get(k) for k in ("registered", "persisted", "ok")}))
            check("and flags it session-only", res.get("session_only") is True,
                  str(res.get("session_only")))
            check("the store STILL holds the original three",
                  store.read_text() == before)
        finally:
            la.REGISTRY = [a for a in la.REGISTRY
                           if a.name != "avtest_store_probe"]
            la._BY_NAME = {a.name: a for a in la.REGISTRY}

        # The healthy path must keep working, and keep a generation back.
        store.write_text(_json.dumps([{"name": "one"}, {"name": "two"}]))
        ua._save_store(ua._load_store() + [{"name": "three"}])
        names = [s["name"] for s in _json.loads(store.read_text())]
        check("a readable store still persists normally",
              names == ["one", "two", "three"], str(names))
        check("and one generation is kept",
              store.with_suffix(".json.bak").exists())
    finally:
        ua._STORE = saved
        ua._LAST_STORE_LOAD_OK, ua._LAST_STORE_LOAD_ERROR = True, ""
        _shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    print("=" * 66)
    print("USER-ADAPTER NAME COLLISION")
    print("=" * 66)
    test_builtin_name_is_rejected()
    test_add_adapter_route_layer_rejects()
    test_distinct_names_coexist()
    test_persisted_store_is_collision_free()
    test_an_unreadable_store_is_not_replaced()
    print("=" * 66)
    print(f"RESULT: {_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
