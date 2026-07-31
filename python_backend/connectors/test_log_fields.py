#!/usr/bin/env python3
"""
Log lines as RECORDS: field extraction + the role index.
================================================================================
The property under test is that the hardest bug on this project becomes a QUERY
instead of an observation. Ground truth, from a real 44,937-line SharpEmu run:

    draw#1 target=0x1240000:3840x2160 ... [tex0=0x5D80000:1280x720 ->UPLOAD]
    guestgpu.present src=0x5D80000 ok=False            (x180)

`0x5D80000` is used constantly as `src`/`tex0` and NEVER as `target` — so the
buffer being presented is one nothing ever renders into, and the lookup can only
miss. build_role_index + roles_never must surface exactly that, ranked first.

Also pinned, because these are the ways a role index quietly goes wrong:
  * ADDRESS NORMALIZATION. Real logs spell one address three ways in three lines
    (`0x0000000804000010`, `0x804000010`, `804000010`). Treating those as
    different entities splits the evidence and hides the pattern.
  * WEIGHTED REPEATS. The raw reader collapses 180 identical lines to
    {line, repeat:180}. Counting that as ONE observation would report "seen 1x"
    for the single most repeated failure in the log — the count IS the finding.
  * COMPOSITE VALUES stay verbatim. `tex0=0x5D80000:1280x720px3686400->UPLOAD`
    keeps its whole value; the address is offered alongside, never instead.
  * NO INTERPRETATION. Counts only, never a verdict.
"""
from __future__ import annotations
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path[:0] = [str(_HERE.parents[1]), str(_HERE.parents[2])]

from connectors import log_fields as lf            # noqa: E402

_passed = _failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1; print(f"  [ok  ] {label}")
    else:
        _failed += 1; print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


#: Verbatim lines from the real run.
REAL = [
    "[GGPU] draw#1 target=0x1240000:3840x2160 CLEAR prim=3 vtx=6 idx=6 vbufs=2 "
    "psImg=1 out=100.0% [tex0=0x5D80000:1280x720px3686400->UPLOAD]",
    "[GGPU] draw#2 target=0x3220000:3840x2160 CLEAR prim=3 vtx=6 idx=6 vbufs=2 "
    "psImg=1 out=100.0% [tex0=0x5D80000:1280x720px3686400->UPLOAD]",
    "[METAL] guestgpu.present src=0x5D80000 ok=False",
    "[METAL] guestgpu.present src=0xB840000 ok=True",
    "[GGPU] draw#1817 target=0xD530000:1280x720 load prim=3 vtx=6 idx=6 vbufs=3 "
    "psImg=1 out=15.8% [tex0=0xDCB0000:1280x720px3686400->CHAIN(15.6%)]",
]


def test_extract_fields():
    print("fields are extracted without touching the line:")
    ex = lf.extract_fields(REAL[0])
    f = ex["fields"]
    check("target captured", f.get("target", "").startswith("0x1240000"), str(f.get("target")))
    check("tex0 captured", f.get("tex0", "").startswith("0x5D80000"), str(f.get("tex0")))
    check("composite value kept VERBATIM",
          f.get("tex0") == "0x5D80000:1280x720px3686400->UPLOAD", f.get("tex0"))
    check("plain scalars captured too", f.get("prim") == "3" and f.get("vtx") == "6",
          f"prim={f.get('prim')} vtx={f.get('vtx')}")
    check("percentage value kept", f.get("out") == "100.0%", f.get("out"))
    check("address extracted ALONGSIDE the value, not instead",
          ex["field_addresses"].get("tex0") == "0x5d80000",
          str(ex["field_addresses"]))
    check("target address extracted",
          ex["field_addresses"].get("target") == "0x1240000")
    check("addresses list is populated", "0x5d80000" in ex["addresses"])
    check("numbers list is populated", 3.0 in ex["numbers"])

    print("boolean-ish and text values survive:")
    f2 = lf.extract_fields(REAL[2])["fields"]
    check("ok=False captured verbatim", f2.get("ok") == "False", str(f2))
    check("src captured", f2.get("src") == "0x5D80000")

    print("hostile input never raises:")
    for bad in ("", None, "   ", "=", "a=", "=b", "x" * 5000, "k=v k=v2",
                "no fields here at all", "0x", "==="):
        try:
            r = lf.extract_fields(bad)
            ok = isinstance(r, dict) and "fields" in r
        except Exception as exc:
            ok = False
            print(f"        raised on {bad!r}: {exc}")
        check(f"survives {str(bad)[:26]!r}", ok)
    check("a repeated key reports the LAST value",
          lf.extract_fields("k=v1 k=v2")["fields"].get("k") == "v2")


def test_address_normalization():
    print("one address spelled three ways is ONE entity:")
    forms = ["0x0000000804000010", "0x804000010", "804000010", "0X804000010"]
    norm = {lf.normalize_address(x) for x in forms}
    check("all four normalize identically", len(norm) == 1, str(norm))
    check("canonical form is lower-case 0x-prefixed, unpadded",
          norm.pop() == "0x804000010")
    check("zero is preserved", lf.normalize_address("0x00000000") == "0x0")
    # Real proof: two lines writing the same address differently must merge.
    idx = lf.build_role_index([
        "[LOADER] Segment 0: VAddr=0x0000000804000000",
        "[RUNTIME] Registered module base=0x804000000",
    ])
    check("padded and unpadded spellings merge into one entity",
          len(idx["addresses"]) == 1, str(list(idx["addresses"])))
    rec = idx["addresses"]["0x804000000"]
    check("and both roles are attributed to it",
          set(rec["roles"]) == {"VAddr", "base"}, str(rec["roles"]))


def test_role_index_and_the_real_bug():
    print("THE BUG, as a query:")
    # Weight the present line the way the raw reader delivers it: one collapsed
    # run standing for 180 observations.
    lines = REAL[:2] + [{"line": REAL[2], "repeat": 180}] + REAL[3:]
    idx = lf.build_role_index(lines)
    check("collapsed runs count as N observations, not 1",
          idx["lines_scanned"] >= 180, str(idx["lines_scanned"]))

    rec = idx["addresses"]["0x5d80000"]
    check("0x5d80000 seen ~182x", rec["total"] >= 180, str(rec["total"]))
    check("its roles are src + tex0", set(rec["roles"]) == {"src", "tex0"},
          str(rec["roles"]))
    check("src count reflects the repeat weight", rec["roles"]["src"] == 180,
          str(rec["roles"]))
    check("it is NEVER a target", "target" not in rec["roles"])

    never = lf.roles_never(idx, "target")
    check("roles_never finds it", any(r["address"] == "0x5d80000" for r in never))
    check("and RANKS IT FIRST (most-observed anomaly leads)",
          never[0]["address"] == "0x5d80000",
          str([(r["address"], r["total"]) for r in never[:3]]))
    check("a real target is correctly EXCLUDED from the answer",
          all(r["address"] != "0x1240000" for r in never),
          "0x1240000 is a target and must not be listed")
    check("the row carries an example line for context",
          bool(never[0].get("example")))

    print("the index reports keys it saw:")
    check("target is a known key", idx["keys"].get("target", 0) >= 3, str(idx["keys"]))
    check("src is a known key", idx["keys"].get("src", 0) >= 180)


def test_noise_control():
    print("size/count keys are excluded from the ROLE index (still in fields):")
    idx = lf.build_role_index(["blit size=0x1000 width=1280 height=720 dst=0xAABBCC"])
    roles = {k for r in idx["addresses"].values() for k in r["roles"]}
    check("'size' is not treated as an entity role", "size" not in roles, str(roles))
    check("a real role IS kept", "dst" in roles, str(roles))
    check("but the field itself is still extracted",
          lf.extract_fields("blit size=0x1000")["fields"].get("size") == "0x1000")

    print("short hex-looking tokens are not swept up as addresses:")
    ex = lf.extract_fields("prim=3 vtx=6 idx=6 ok=True")
    check("no bogus addresses from small numbers", ex["addresses"] == [],
          str(ex["addresses"]))

    print("key_filter narrows the index:")
    idx2 = lf.build_role_index(REAL, key_filter={"target"})
    roles2 = {k for r in idx2["addresses"].values() for k in r["roles"]}
    check("only the requested key is indexed", roles2 == {"target"}, str(roles2))

    print("empty / junk input:")
    e = lf.build_role_index([])
    check("empty input -> empty index", e["addresses"] == {} and e["lines_scanned"] == 0)
    check("None input is safe", lf.build_role_index(None)["addresses"] == {})
    check("blank lines are skipped",
          lf.build_role_index(["", "   ", "\t"])["lines_scanned"] == 0)
    check("roles_never on an empty index returns nothing",
          lf.roles_never(e, "target") == [])
    check("roles_never with a nonsense key returns everything seen",
          len(lf.roles_never(idx2, "no_such_key")) == len(idx2["addresses"]))


if __name__ == "__main__":
    print("=" * 70)
    print("LOG FIELDS / ROLE INDEX")
    print("=" * 70)
    test_extract_fields()
    test_address_normalization()
    test_role_index_and_the_real_bug()
    test_noise_control()
    print("=" * 70)
    print(f"RESULT: {_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
