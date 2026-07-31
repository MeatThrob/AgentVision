"""
Examine-before-delete retention: the contract that makes captures worthwhile.
================================================================================
The single property under test is: A FRAME THE AGENT WAS TOLD TO LOOK AT IS NOT
DELETED BEFORE IT IS LOOKED AT. Everything else — the byte budget, the eviction
ordering, the archive, the orphan sweep — exists to keep that promise while
staying inside 5 GiB.

Also pinned here: the backstop is LOUD. When a held frame does expire unexamined
that is a reported loss (`dropped_unexamined`), never a silent truncation, since
"the agent saw everything" is precisely the lie this module must not tell.
"""
from __future__ import annotations
import os, sys, tempfile, time
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path[:0] = [str(_HERE.parents[2]), str(_HERE.parents[1])]

from utils import retention as rt              # noqa: E402

_passed = _failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1; print(f"  [ok  ] {label}")
    else:
        _failed += 1; print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


def _led(**kw) -> rt.Ledger:
    kw.setdefault("budget_bytes", 1000)
    kw.setdefault("mode", "errors")
    kw.setdefault("hold_s", 900.0)
    return rt.Ledger(**kw)


MB = 1024 * 1024

# ── budget parsing ────────────────────────────────────────────────────────────

def test_parse_bytes():
    print("budget strings parse the way a user would write them:")
    check("5GB", rt.parse_bytes("5GB", 0) == 5 * 1024 ** 3)
    check("5G == 5GB", rt.parse_bytes("5G", 0) == rt.parse_bytes("5GB", 0))
    check("500MB", rt.parse_bytes("500MB", 0) == 500 * 1024 ** 2)
    check("raw digits", rt.parse_bytes("1048576", 0) == 1048576)
    check("int passthrough", rt.parse_bytes(2048, 0) == 2048)
    check("lowercase + spaces", rt.parse_bytes(" 2 gb ", 0) == 2 * 1024 ** 3)
    check("fractional", rt.parse_bytes("1.5GB", 0) == int(1.5 * 1024 ** 3))
    check("garbage falls back to default", rt.parse_bytes("banana", 77) == 77)
    check("empty falls back", rt.parse_bytes("", 77) == 77)
    check("negative clamps to >= 0", rt.parse_bytes(-5, 0) == 0)
    check("default budget is 5 GiB", rt.DEFAULT_BUDGET_BYTES == 5 * 1024 ** 3,
          str(rt.DEFAULT_BUDGET_BYTES))


# ── policy ────────────────────────────────────────────────────────────────────

def test_assess_priorities():
    print("frames are classified by how badly an eye is needed:")
    a = rt.assess({"has_error": True, "error_label": "KeyError: height"})
    check("structured error => P_ERROR", a["priority"] == rt.P_ERROR)
    check("error reason carries the label", "KeyError" in a["reason"], a["reason"])
    check("error is flagged as failure", a["failure"] is True)
    check("incident alone => P_ERROR",
          rt.assess({"incident": True})["priority"] == rt.P_ERROR)
    check("freeze event => P_EVENT",
          rt.assess({"event_kinds": ["screen_frozen"]})["priority"] == rt.P_EVENT)
    check("blank event => P_EVENT",
          rt.assess({"event_kinds": ["blank_screen"]})["priority"] == rt.P_EVENT)
    check("on-screen error text => P_EVENT",
          rt.assess({"ocr_error": True})["priority"] == rt.P_EVENT)
    # Real kinds the detector emits. layout_change must NOT read as a failure —
    # it contains "hang" as a substring ("c-hang-e"), so this pins word matching.
    check("layout_change is not a failure (substring 'hang' trap)",
          rt.assess({"event_kinds": ["layout_change"]})["failure"] is False)
    check("screen_idle is not a failure (static but alive)",
          rt.assess({"event_kinds": ["screen_idle"]})["failure"] is False)
    check("on_screen_error IS a failure",
          rt.assess({"event_kinds": ["on_screen_error"]})["failure"] is True)
    check("unknown event kind is not a failure",
          rt.assess({"event_kinds": ["scene_transition"]})["failure"] is False)
    check("plural/suffixed kinds still match ('render_errors')",
          rt.assess({"event_kinds": ["render_errors"]})["failure"] is True)
    big = rt.assess({"change_score": 0.9})
    check("big change => P_BIG", big["priority"] == rt.P_BIG, str(big))
    check("small change => P_CHANGE",
          rt.assess({"change_score": 0.01})["priority"] == rt.P_CHANGE)
    check("changed flag without a score still counts",
          rt.assess({"changed": True})["priority"] == rt.P_CHANGE)
    check("identical frame => P_STATIC",
          rt.assess({"change_score": 0.0})["priority"] == rt.P_STATIC)
    check("empty facts never raise", rt.assess({})["priority"] == rt.P_STATIC)
    check("junk change_score is survivable",
          rt.assess({"change_score": "nope"})["priority"] == rt.P_STATIC)


def test_assess_interest_ordering():
    print("keep-value orders eviction sensibly:")
    i = lambda f: rt.assess(f)["interest"]
    check("error outranks a big change",
          i({"has_error": True}) > i({"change_score": 0.9}))
    check("failure event outranks a big change",
          i({"event_kinds": ["hang"]}) > i({"change_score": 0.5}))
    check("big change outranks a small one",
          i({"change_score": 0.5}) > i({"change_score": 0.02}))
    check("any change outranks a static frame",
          i({"change_score": 0.02}) > i({"change_score": 0.0}))
    check("interest stays within [0,1]",
          all(0.0 <= i(f) <= 1.0 for f in ({"has_error": True},
                                           {"change_score": 99.0}, {})))


def test_modes_gate_who_gets_pushed():
    print("policy mode decides who gets pushed at the agent:")
    err  = {"has_error": True}
    evt  = {"event_kinds": ["screen_frozen"]}
    chg  = {"change_score": 0.4}
    still = {"change_score": 0.0}
    want = lambda f, m: rt.assess(f, m)["needs_eyes"]

    check("off pushes nothing at all",
          not any(want(f, "off") for f in (err, evt, chg, still)))
    check("errors pushes the error", want(err, "errors"))
    check("errors pushes the failure event", want(evt, "errors"))
    check("errors does NOT push a mere change", not want(chg, "errors"))
    check("errors does NOT push a static frame", not want(still, "errors"))
    check("changes pushes changes too", want(chg, "changes"))
    check("changes still skips static frames", not want(still, "changes"))
    check("all pushes every frame (video-like)",
          all(want(f, "all") for f in (err, evt, chg, still)))
    check("unknown mode falls back to errors",
          want(chg, "wat") == want(chg, "errors"))
    check("None mode is safe", rt.normalize_mode(None) in rt.MODES)


# ── ledger state machine ──────────────────────────────────────────────────────

def test_admit_and_clearance():
    print("clearance: only an examined (or expired) frame may be deleted:")
    led = _led()
    now = 1_000_000.0
    e = led.admit(1, now, [], {"has_error": True}, nbytes=100)
    s = led.admit(2, now, [], {"change_score": 0.0}, nbytes=100)
    check("error frame needs eyes", e["needs_eyes"] is True)
    check("static frame does not (mode=errors)", s["needs_eyes"] is False)
    check("admitted frames are mined on arrival", e["mined"] is True)

    check("a frame nobody must see is immediately clearable",
          led._cleared(led._rec[2], now) is True)
    check("a frame awaiting eyes is NOT clearable",
          led._cleared(led._rec[1], now) is False)

    check("offering alone does not clear it", led.mark_offered([1]) == 1
          and led._cleared(led._rec[1], now) is False)
    check("examining clears it", led.mark_examined(1, "frame_json") is True
          and led._cleared(led._rec[1], now) is True)
    check("re-examining is idempotent", led.mark_examined(1) is False)
    check("examining an unknown seq is safe", led.mark_examined(999) is False)
    check("examined count == 1", led.stats["examined"] == 1)


def test_hold_backstop():
    print("the hold backstop frees a wedged disk but reports the loss:")
    led = _led(hold_s=60.0)
    t0 = 1_000_000.0
    led.admit(1, t0, [], {"has_error": True}, nbytes=100)
    check("still held at 59s", led._cleared(led._rec[1], t0 + 59_000) is False)
    check("cleared at 61s", led._cleared(led._rec[1], t0 + 61_000) is True)

    led2 = _led(hold_s=0.0)
    led2.admit(1, t0, [], {"has_error": True}, nbytes=100)
    check("hold_s=0 means hold forever (never auto-clear)",
          led2._cleared(led2._rec[1], t0 + 10 ** 9) is False)


def test_awaiting_queue_order():
    print("the awaiting queue is ordered by urgency, newest first within a tier:")
    led = _led(mode="all")
    t0 = 1_000_000.0
    led.admit(10, t0 + 0,    [], {"change_score": 0.0}, nbytes=1)
    led.admit(11, t0 + 1000, [], {"change_score": 0.5}, nbytes=1)
    led.admit(12, t0 + 2000, [], {"event_kinds": ["screen_frozen"]}, nbytes=1)
    led.admit(13, t0 + 3000, [], {"has_error": True}, nbytes=1)
    rows = led.awaiting(now_ms=t0 + 4000)
    check("all four are awaiting", len(rows) == 4, str(len(rows)))
    check("error first", rows[0]["seq"] == 13, str([r["seq"] for r in rows]))
    check("then the failure event", rows[1]["seq"] == 12)
    check("static frame last", rows[-1]["seq"] == 10)
    check("rows report age", rows[0]["age_seconds"] >= 0)
    check("rows report time left before expiry",
          rows[0]["hold_expires_in_seconds"] is not None)
    check("limit is honoured", len(led.awaiting(limit=2, now_ms=t0 + 4000)) == 2)

    led.mark_offered([13])
    check("offered is visible in the row",
          led.awaiting(now_ms=t0 + 4000)[0]["offered"] is True)
    check("unoffered_only filters it out",
          13 not in [r["seq"] for r in
                     led.awaiting(unoffered_only=True, now_ms=t0 + 4000)])
    led.mark_examined(13)
    check("examined frames leave the queue",
          13 not in [r["seq"] for r in led.awaiting(now_ms=t0 + 4000)])


# ── eviction ──────────────────────────────────────────────────────────────────

def test_no_eviction_under_high_water():
    print("nothing is deleted while there is headroom (this is the whole fix):")
    led = _led(budget_bytes=1000)
    t0 = 1_000_000.0
    for i in range(5):
        led.admit(i, t0 + i, [], {"change_score": 0.0}, nbytes=100)
    plan = led.plan_eviction(now_ms=t0 + 10 ** 6)          # way past 60s
    check("500/1000 bytes => no eviction", plan["evict"] == [], str(plan))
    check("plan says why", "high-water" in plan["reason"], plan["reason"])
    check("age alone never evicts — the clock is not the trigger",
          led.known(0) is True)
    check("zero budget is a no-op, not a purge",
          _led(budget_bytes=0).plan_eviction(now_ms=t0)["evict"] == [])


def test_eviction_order_and_protection():
    print("under pressure: cheap frames go, awaiting frames are protected:")
    led = _led(budget_bytes=1000, mode="errors", hold_s=900.0)
    t0 = 1_000_000.0
    # 6 static frames (clearable) + 2 error frames (awaiting eyes) = 800 bytes.
    for i in range(6):
        led.admit(i, t0 + i, [], {"change_score": 0.0}, nbytes=100)
    led.admit(100, t0 + 10, [], {"has_error": True}, nbytes=100)
    led.admit(101, t0 + 11, [], {"has_error": True}, nbytes=100)
    # Push over the 90% high-water mark.
    led.admit(200, t0 + 12, [], {"change_score": 0.0}, nbytes=200)

    plan = led.plan_eviction(now_ms=t0 + 20_000)
    check("eviction triggered over high-water", len(plan["evict"]) > 0, str(plan))
    victims = [r["seq"] for r in plan["evict"]]
    check("no frame awaiting examination is evicted",
          100 not in victims and 101 not in victims, str(victims))
    check("every victim is tier 0 (already served)",
          all(r["tier"] == 0 for r in plan["evict"]), str(plan["evict"]))
    check("oldest cheap frame goes first", victims[0] == 0, str(victims))
    check("it stops at the low-water target",
          plan["projected_bytes"] <= plan["target_bytes"], str(plan))

    # Once examined, the error frames become ordinary eviction candidates.
    led.mark_examined(100); led.mark_examined(101)
    plan2 = led.plan_eviction(now_ms=t0 + 20_000)
    check("examined error frames become evictable, but last (highest interest)",
          all(r["interest"] <= plan2["evict"][-1]["interest"]
              for r in plan2["evict"]), str(plan2["evict"]))


def test_pinned_frames_are_last():
    print("incident-pinned frames are the absolute last to go:")
    led = _led(budget_bytes=1000)
    t0 = 1_000_000.0
    for i in range(10):
        led.admit(i, t0 + i, [], {"change_score": 0.0}, nbytes=100)
    led.set_pinned([0, 1])                       # the OLDEST are pinned
    plan = led.plan_eviction(now_ms=t0 + 20_000)
    victims = [r["seq"] for r in plan["evict"]]
    check("pinned frames survive despite being oldest",
          0 not in victims and 1 not in victims, str(victims))
    check("unpinning re-exposes them", led.set_pinned([0], False) == 1)


def test_expired_unexamined_is_reported_not_hidden():
    print("a held frame that expires is counted as a real loss:")
    led = _led(budget_bytes=1000, hold_s=10.0)
    t0 = 1_000_000.0
    for i in range(9):
        led.admit(i, t0 + i, [], {"has_error": True}, nbytes=100)
    led.admit(500, t0 + 20, [], {"change_score": 0.0}, nbytes=200)
    plan = led.plan_eviction(now_ms=t0 + 60_000)      # all past the 10s hold
    tiers = {r["tier"] for r in plan["evict"]}
    check("expired-unexamined frames are tier 1, not tier 0", 1 in tiers, str(tiers))
    res = led.commit_eviction(plan, deleter=lambda p: True)
    check("dropped_unexamined is counted", res["dropped_unexamined"] > 0, str(res))
    rep = led.report(now_ms=t0 + 60_000)
    check("report surfaces it", rep["integrity"]["dropped_unexamined"] > 0)
    check("integrity.ok goes false", rep["integrity"]["ok"] is False)
    check("report explains the remedy",
          "capture rate" in rep["integrity"]["meaning"])


def test_commit_keeps_history():
    print("eviction reclaims pixels but keeps the JSON + thumb history:")
    d = Path(tempfile.mkdtemp())
    stem = "frame_00007"
    png   = d / f"{stem}.png";          png.write_bytes(b"x" * 500)
    ann   = d / f"{stem}_annotated.png"; ann.write_bytes(b"x" * 500)
    diff  = d / f"{stem}.diff";         diff.write_bytes(b"d" * 10)
    side  = d / f"{stem}_frame.json";   side.write_text('{"sequence":7}')
    thumb = d / f"{stem}_thumb.png";    thumb.write_bytes(b"t" * 20)

    group = rt.stem_group(d, stem)
    check("stem_group finds the whole family (incl. the orphan-maker)",
          len(group) == 5, str([Path(p).name for p in group]))

    led = _led(budget_bytes=1000)
    rec = led.admit(7, 1_000_000.0, group, {"change_score": 0.0},
                    folder=str(d), stem=stem)
    expect = 500 + 500 + 10 + len('{"sequence":7}') + 20
    check("admitted bytes == real footprint of the whole group",
          rec["bytes"] == expect, f"{rec['bytes']} != {expect}")

    plan = {"evict": [{"seq": 7, "tier": 0, "bytes": rec["bytes"],
                       "interest": 0.05, "archive": False}]}
    res = led.commit_eviction(plan)
    check("bytes were reclaimed", res["bytes_reclaimed"] >= 1000,
          str(res["bytes_reclaimed"]))
    check("full PNG deleted", not png.exists())
    check("annotated copy deleted too (the old leak)", not ann.exists())
    check(".diff deleted too (also never pruned before)", not diff.exists())
    check("sidecar JSON KEPT — permanent history", side.exists())
    check("thumbnail KEPT — history stays visual", thumb.exists())
    check("record dropped from the ledger", led.known(7) is False)


def test_low_disk_floor():
    print("a nearly-full disk evicts even when the budget has headroom:")
    led = _led(budget_bytes=10_000)
    t0 = 1_000_000.0
    for i in range(10):
        led.admit(i, t0 + i, [], {"change_score": 0.0}, nbytes=100)
    calm = led.plan_eviction(now_ms=t0, free_bytes=rt.MIN_FREE_BYTES * 4)
    check("plenty of free disk => no eviction", calm["evict"] == [])
    tight = led.plan_eviction(now_ms=t0, free_bytes=1)
    check("below the free-disk floor => evict anyway", len(tight["evict"]) > 0)
    check("and the reason names the floor", "free disk" in tight["reason"],
          tight["reason"])


# ── the emergency path has a floor ────────────────────────────────────────────
# It did not. One truthy `low_disk` set target = used*0.5 and unlocked EVERY
# tier, and _recorder_prune runs once per captured frame with no cooldown.
# Measured on a fixture with real files: six passes (0.6 s at 10 fps) destroyed
# 29 of 30 incident-pinned frames and all 30 never-seen frames, and reported
# dropped_unexamined=0 for all of it.

def _emergency_ledger(n_pinned: int = 10, n_unseen: int = 40):
    """Frames that all need eyes: some pinned, the rest never examined."""
    led = _led(budget_bytes=10_000, hold_s=900.0)
    t0 = 1_000_000.0
    for i in range(n_pinned + n_unseen):
        led.admit(i, t0 + i, [], {"has_error": True}, nbytes=100)
    led.set_pinned(range(n_pinned))
    return led, t0


def test_pinned_evidence_is_never_spent_even_on_a_full_disk():
    print("an incident's frozen evidence is not currency for a full disk:")
    led, t0 = _emergency_ledger()
    plan = led.plan_eviction(now_ms=t0, free_bytes=1)
    check("the emergency path did engage", plan["low_disk"] is True)
    tiers = {r["tier"] for r in plan["evict"]}
    check("tier 3 appears in NO emergency plan", 3 not in tiers, str(tiers))
    check("and the plan says so", 3 in plan["protected_from_eviction"],
          str(plan["protected_from_eviction"]))
    # The decisive case: EVERYTHING is pinned, so tier 3 is the only currency
    # available. The old code spent it; there must be no plan at all now.
    only_pinned = _led(budget_bytes=1000, hold_s=900.0)
    for i in range(20):
        only_pinned.admit(i, t0 + i, [], {"has_error": True}, nbytes=100)
    only_pinned.set_pinned(range(20))
    p2 = only_pinned.plan_eviction(now_ms=t0, free_bytes=1)
    check("a ledger of nothing but evidence plans NO evictions",
          p2["evict"] == [], str(len(p2["evict"])))
    check("and reports the shortfall instead of taking it",
          p2["unmet_bytes"] > 0, str(p2["unmet_bytes"]))
    check("it reports how many it withheld", plan["pinned_withheld"] == 10,
          str(plan["pinned_withheld"]))


def test_commit_rechecks_the_pin_not_the_plan():
    print("a stale plan row cannot spend a frame that is pinned NOW:")
    led = _led(budget_bytes=1000)
    d = Path(tempfile.mkdtemp())
    png = d / "frame_00001.png"; png.write_bytes(b"x" * 100)
    led.admit(1, 1_000_000.0, [str(png)], {"has_error": True},
              folder=str(d), stem="frame_00001")
    # plan built BEFORE the incident froze this frame
    plan = {"evict": [{"seq": 1, "tier": 0, "bytes": 100, "interest": 0.0,
                       "archive": False}]}
    led.set_pinned([1])
    res = led.commit_eviction(plan)
    check("nothing was evicted", res["evicted"] == 0, str(res))
    check("it is reported as refused, not silently skipped",
          res["refused_pinned"] == 1, str(res))
    check("the pixels are still on disk", png.exists())


def test_emergency_passes_are_rate_limited():
    print("consecutive emergency passes cannot compound the same decision:")
    led, t0 = _emergency_ledger()
    first = led.plan_eviction(now_ms=t0, free_bytes=1)
    check("first pass plans work", len(first["evict"]) > 0)
    again = led.plan_eviction(now_ms=t0 + 100.0, free_bytes=1)   # 0.1 s later
    check("a pass 0.1 s later plans nothing", again["evict"] == [],
          str(len(again["evict"])))
    check("and says why", "cooling" in again["reason"], again["reason"])
    later = led.plan_eviction(
        now_ms=t0 + rt.EMERGENCY_COOLDOWN_S * 1000.0 + 1.0, free_bytes=1)
    check("after the cooldown it may act again", len(later["evict"]) > 0)


def test_unseen_frames_are_capped_per_emergency_pass():
    print("a frame the agent still owes eyes to is spent in bounded numbers:")
    led, t0 = _emergency_ledger(n_pinned=0, n_unseen=200)
    plan = led.plan_eviction(now_ms=t0, free_bytes=1)
    n2 = sum(1 for r in plan["evict"] if r["tier"] == 2)
    check("no more than the per-pass cap", n2 <= rt.EMERGENCY_UNSEEN_PER_PASS,
          f"{n2} > {rt.EMERGENCY_UNSEEN_PER_PASS}")
    check("the rest are reported as withheld", plan["unseen_withheld"] > 0,
          str(plan["unseen_withheld"]))


def test_unseen_pixels_need_a_real_archive_copy_on_disk():
    print("tier-2 pixels go only when a compressed copy PROVABLY exists:")
    d = Path(tempfile.mkdtemp())
    led = _led(budget_bytes=10_000)
    png = d / "frame_00002.png"
    png.write_bytes(b"not a decodable image")        # archive_frame will fail
    led.admit(2, 1_000_000.0, [str(png)], {"has_error": True},
              folder=str(d), stem="frame_00002")
    plan = {"evict": [{"seq": 2, "tier": 2, "bytes": 100, "interest": 0.85,
                       "archive": True}]}
    res = led.commit_eviction(plan, archive_dir=d / "_archive")
    check("archiving failed, so nothing was evicted", res["evicted"] == 0, str(res))
    check("counted as kept_unarchived", res["kept_unarchived"] == 1, str(res))
    check("the only pixels there are survive", png.exists())
    check("the record survives too, so it can still be examined",
          led.known(2) is True)

    # Now the same frame with an image that CAN be archived.
    try:
        from PIL import Image
    except Exception:
        print("  [skip] pillow unavailable — archive-succeeds half not checked")
        return
    d2 = Path(tempfile.mkdtemp())
    led2 = _led(budget_bytes=10_000)
    png2 = d2 / "frame_00003.png"
    Image.new("RGB", (48, 48), (10, 20, 30)).save(str(png2), format="PNG")
    led2.admit(3, 1_000_000.0, [str(png2)], {"has_error": True},
               folder=str(d2), stem="frame_00003")
    plan2 = {"evict": [{"seq": 3, "tier": 2, "bytes": 100, "interest": 0.85,
                        "archive": True}]}
    res2 = led2.commit_eviction(plan2, archive_dir=d2 / "_archive")
    check("with a verified copy the pixels may go", res2["evicted"] == 1, str(res2))
    check("and the copy is really on disk",
          any((d2 / "_archive").glob("frame_00003.*")))
    check("the tier-2 loss is REPORTED, not silent",
          res2["dropped_unseen"] == 1, str(res2))


def test_authored_annotations_are_never_deleted():
    print("_annotations.json is authored by hand and has no archive copy:")
    d = Path(tempfile.mkdtemp())
    stem = "frame_00011"
    png = d / f"{stem}.png";                png.write_bytes(b"x" * 200)
    notes = d / f"{stem}_annotations.json"
    notes.write_text('{"note":"why this frame matters"}')
    side = d / f"{stem}_frame.json";        side.write_text("{}")
    led = _led(budget_bytes=1000)
    led.admit(11, 1_000_000.0, rt.stem_group(d, stem), {"change_score": 0.0},
              folder=str(d), stem=stem)
    led.commit_eviction({"evict": [{"seq": 11, "tier": 0, "bytes": 200,
                                    "interest": 0.05, "archive": False}]})
    check("pixels reclaimed", not png.exists())
    check("authored annotations KEPT", notes.exists())
    check("sidecar KEPT", side.exists())


def test_free_disk_floor_cannot_exceed_the_disk():
    print("a floor bigger than the volume is a typo, not an emergency:")
    led = _led(budget_bytes=10_000)
    t0 = 1_000_000.0
    for i in range(10):
        led.admit(i, t0 + i, [], {"change_score": 0.0}, nbytes=100)
    total = rt.MIN_FREE_BYTES * 2          # floor = 50% of the volume
    free = int(rt.MIN_FREE_BYTES * 0.9)    # under the raw floor, healthy disk
    plan = led.plan_eviction(now_ms=t0, free_bytes=free, total_bytes=total)
    check("the floor was capped against the volume",
          plan.get("free_disk_floor_capped") is True, str(plan.get("reason")))
    check("so a healthy disk is not treated as an emergency",
          plan["evict"] == [], str(len(plan["evict"])))
    # Without total_bytes the floor is trusted as configured.
    plan2 = led.plan_eviction(now_ms=t0, free_bytes=free)
    check("omitting total_bytes trusts the configured floor",
          plan2["low_disk"] is True)


def test_archive_bytes_cannot_make_the_target_unreachable():
    print("an archive bigger than the target does not plan away every frame:")
    led = _led(budget_bytes=10_000)
    t0 = 1_000_000.0
    for i in range(20):
        led.admit(i, t0 + i, [], {"has_error": True}, nbytes=100)
    led.mark_examined_many(range(20))            # all cleared => tier 0
    # Archive alone exceeds the low-water target: frames cannot pay that debt.
    led.archive_bytes = int(10_000 * rt.LOW_WATER) + 500
    plan = led.plan_eviction(now_ms=t0)
    check("the plan flags that the archive is over target",
          plan.get("archive_over_target") is True, str(plan.get("archive_bytes")))
    check("and names the overflow so the caller can say so",
          plan.get("archive_overflow_bytes", 0) > 0,
          str(plan.get("archive_overflow_bytes")))
    check("frames are not spent to pay the archive's debt",
          plan["evict"] == [], str(len(plan["evict"])))


# ── orphan sweep ──────────────────────────────────────────────────────────────

def test_sweep_orphans():
    print("the orphan sweep reclaims what the old pruner could never see:")
    d = Path(tempfile.mkdtemp())
    for i in (1, 2, 3):
        (d / f"frame_{i:05d}_annotated.png").write_bytes(b"x" * 100)
    (d / "frame_00009.png").write_bytes(b"x" * 100)
    (d / "frame_00009_frame.json").write_text("{}")
    (d / "activity.log").write_text("keep me")
    old = time.time() - 3600
    for p in d.glob("frame_*"):
        os.utime(p, (old, old))

    res = rt.sweep_orphans(d, live_stems={"frame_00009"}, min_age_s=60)
    check("sweep succeeded", res["ok"] is True, str(res))
    check("3 orphan groups removed", res["removed"] == 3, str(res["removed"]))
    check("bytes freed reported", res["bytes_freed"] == 300, str(res))
    check("live frame untouched", (d / "frame_00009.png").exists())
    check("live sidecar untouched", (d / "frame_00009_frame.json").exists())
    check("non-frame files are never touched", (d / "activity.log").exists())

    (d / "frame_00050_annotated.png").write_bytes(b"y" * 10)
    fresh = rt.sweep_orphans(d, live_stems=set(), min_age_s=300)
    check("a just-written file is spared (may be mid-write)",
          (d / "frame_00050_annotated.png").exists(), str(fresh))

    dry = rt.sweep_orphans(d, live_stems=set(), min_age_s=0, dry_run=True)
    check("dry_run reports without deleting",
          dry["removed"] >= 1 and (d / "frame_00050_annotated.png").exists())
    check("missing folder degrades instead of raising",
          rt.sweep_orphans("/nonexistent/av/dir", set())["ok"] is False)


def test_sweep_never_deletes_a_frame_with_a_sidecar():
    """AN ORPHAN IS A FILE WITH NO SIDECAR, not a file the ledger forgot.

    The sweep used to trust the ledger alone, so any upstream failure to admit a
    frame turned that frame into an "orphan". It happened: one misplaced gate in
    the hydrator and the sweep permanently deleted the newest 1,500 frames of a
    real capture — 7,501 files, 270 MB — because their stems were not re-admitted.
    Verified against the old implementation: with an empty ledger it removed all
    7 files of the fixture below, including the intact frame. It now removes 3.
    """
    print("a frame with a sidecar is never an orphan, whatever the ledger knows:")
    d = Path(tempfile.mkdtemp())
    for name in ("frame_00007.png", "frame_00007_annotated.png",
                 "frame_00007.diff", "frame_00007_frame.json"):
        (d / name).write_bytes(b"x" * 10)          # complete frame
    for name in ("frame_00008.png", "frame_00008_annotated.png",
                 "frame_00008.diff"):
        (d / name).write_bytes(b"x" * 10)          # true orphan: no sidecar
    old = time.time() - 3600
    for p in d.glob("frame_*"):
        os.utime(p, (old, old))

    res = rt.sweep_orphans(d, live_stems=set(), min_age_s=60)   # EMPTY ledger
    check("the complete frame survives an empty ledger",
          (d / "frame_00007_frame.json").exists()
          and (d / "frame_00007.png").exists()
          and (d / "frame_00007_annotated.png").exists()
          and (d / "frame_00007.diff").exists(),
          str(sorted(p.name for p in d.iterdir())))
    check("the sidecar-less orphan is still reclaimed",
          not (d / "frame_00008.png").exists() and res["removed"] == 3,
          f"removed={res['removed']}")
    check("the reason is reported, not silent",
          res.get("kept_because_sidecar_exists") == 4,
          str(res.get("kept_because_sidecar_exists")))


def _orphan_dir(n: int, live: int = 0) -> Path:
    """n sidecar-less old `frame_*` files (genuine orphans), plus `live`
    complete frames that must never be touched."""
    d = Path(tempfile.mkdtemp())
    old = time.time() - 3600
    for i in range(n):
        p = d / f"frame_{i:05d}_annotated.png"
        p.write_bytes(b"x" * 10)
        os.utime(p, (old, old))
    for i in range(live):
        stem = f"frame_9{i:04d}"
        for suffix in (".png", "_frame.json"):
            p = d / f"{stem}{suffix}"
            p.write_bytes(b"x" * 10)
            os.utime(p, (old, old))
    return d


def test_sweep_has_a_blast_radius_cap():
    print("one sweep cannot remove an unbounded number of files:")
    d = _orphan_dir(40, live=60)      # 40 orphans among 160 frame files
    res = rt.sweep_orphans(d, live_stems=set(), min_age_s=60, max_removals=10)
    check("removals stop at the cap", res["removed"] == 10, str(res["removed"]))
    check("the remainder is reported, not forgotten",
          res["capped_remaining"] == 30, str(res.get("capped_remaining")))
    check("the 30 uncapped orphans are still on disk",
          len(list(d.glob("frame_0*_annotated.png"))) == 30,
          str(len(list(d.glob("frame_0*_annotated.png")))))
    check("and no complete frame was touched",
          len(list(d.glob("frame_9*_frame.json"))) == 60)
    check("the default cap exists and is finite",
          0 < rt.SWEEP_MAX_REMOVALS < 10 ** 9, str(rt.SWEEP_MAX_REMOVALS))


def test_sweep_refuses_when_almost_everything_looks_like_an_orphan():
    print("'every file is an orphan' is a bookkeeping failure — refuse it:")
    d = _orphan_dir(40)                     # 40/40 orphans = 100%
    res = rt.sweep_orphans(d, live_stems=set(), min_age_s=60)
    check("nothing was removed", res["removed"] == 0, str(res["removed"]))
    check("it says it refused", res.get("refused") == 40, str(res.get("refused")))
    check("and explains why", "bookkeeping failure" in (res.get("reason") or ""),
          str(res.get("reason")))
    check("all 40 files are still there", len(list(d.glob("frame_*"))) == 40)
    # A directory that is mostly LIVE frames with a few strays sweeps normally.
    d2 = _orphan_dir(3)
    for i in range(30):
        stem = f"frame_1{i:04d}"
        (d2 / f"{stem}.png").write_bytes(b"x" * 10)
        (d2 / f"{stem}_frame.json").write_text("{}")
    res2 = rt.sweep_orphans(d2, live_stems=set(), min_age_s=60)
    check("a few strays among real frames are still reclaimed",
          res2["removed"] == 3, str(res2))


def test_sweep_writes_down_what_it_removed():
    print("every sweep records the paths it removed, before removing them:")
    d = _orphan_dir(5)
    rt.sweep_orphans(d, live_stems=set(), min_age_s=60)
    man = d / rt.SWEEP_MANIFEST_NAME
    check("a manifest exists", man.exists(), str(man))
    if not man.exists():
        return
    import json as _json
    rows = [_json.loads(x) for x in man.read_text().splitlines() if x.strip()]
    check("it names the files, not just a count",
          any(len(r.get("removing") or []) == 5 for r in rows), str(rows)[:300])
    check("the manifest itself is not a frame file, so it is never swept",
          not rt._FRAME_FILE_RE.match(rt.SWEEP_MANIFEST_NAME))


# ── push surface ──────────────────────────────────────────────────────────────

def test_push_sentence():
    print("the push line is compact, names seqs, and says what to call:")
    check("no rows => no injection", rt.push_sentence([]) == "")
    rows = [{"seq": 41, "priority": 0, "reason": "KeyError: height",
             "failure": True, "hold_expires_in_seconds": 120.0},
            {"seq": 42, "priority": 3, "reason": "visual change (12%)",
             "failure": False, "hold_expires_in_seconds": 300.0}]
    s = rt.push_sentence(rows)
    check("states the count", "2 frame" in s, s)
    check("states the seq span", "41-42" in s, s)
    check("surfaces the failure reason", "KeyError" in s, s)
    check("warns about expiry", "expires in 120s" in s, s)
    check("names the cheapest tool first", "av_frame_json(41)" in s, s)
    check("offers the release path", "av_examine_ack" in s, s)
    check("stays short enough to inject every turn", len(s) < 320, str(len(s)))
    one = rt.push_sentence([rows[1]])
    check("single frame reads naturally", "1 frame " in one and "42" in one, one)


# ── report ────────────────────────────────────────────────────────────────────

def test_report_shape():
    print("the report explains the policy to whoever reads it:")
    led = _led(budget_bytes=5 * 1024 ** 3, mode="errors")
    t0 = 1_000_000.0
    led.admit(1, t0, [], {"has_error": True}, nbytes=MB)
    led.admit(2, t0, [], {"change_score": 0.0}, nbytes=MB)
    led.mark_offered([1])
    rep = led.report(now_ms=t0 + 1000)
    for k in ("policy", "budget", "frames", "totals", "integrity"):
        check(f"report has '{k}'", k in rep)
    check("all four modes are documented",
          set(rep["policy"]["modes_available"]) == set(rt.MODES), str(rep["policy"]))
    check("budget is human-readable", "GB" in rep["budget"]["human"],
          rep["budget"]["human"])
    check("used bytes tracked", rep["budget"]["used_bytes"] == 2 * MB)
    check("counts frames needing eyes", rep["frames"]["need_eyes"] == 1)
    check("counts frames awaiting", rep["frames"]["awaiting_examination"] == 1)
    check("counts deletable frames", rep["frames"]["cleared_deletable"] == 1)
    check("integrity ok when nothing was lost", rep["integrity"]["ok"] is True)
    check("budget note says deletion is not clock-driven",
          "not by a clock" in rep["budget"]["note"])

    check("configure changes the mode live",
          led.configure(mode="all")["mode"] == "all")
    check("configure changes the budget live",
          led.configure(budget_bytes=123)["budget_bytes"] == 123)
    check("configure rejects a bad mode safely",
          led.configure(mode="nope")["mode"] == "errors")


def test_thumbnail_and_archive():
    print("thumbnail + archive (skipped cleanly without Pillow):")
    try:
        from PIL import Image
    except Exception:
        check("Pillow absent => helpers degrade, never raise",
              rt.write_thumbnail("/x.png", "/y.png")["ok"] is False
              and rt.archive_frame("/x.png", "/tmp")["ok"] is False)
        return
    d = Path(tempfile.mkdtemp())
    src = d / "frame_00001.png"
    Image.new("RGB", (1280, 800), (30, 60, 90)).save(src)

    th = rt.write_thumbnail(src, d / "frame_00001_thumb.png", width=96)
    check("thumbnail written", th["ok"] is True, str(th))
    check("thumbnail is 96px wide", th["size"][0] == 96, str(th["size"]))
    check("aspect ratio preserved", th["size"][1] == 60, str(th["size"]))
    check("thumbnail is tiny", th["bytes"] < 8000, str(th["bytes"]))

    ar = rt.archive_frame(src, d / "_archive")
    check("archive written", ar["ok"] is True, str(ar))
    check("archive is a compressed format", ar["format"] in ("webp", "jpeg"),
          str(ar))
    check("archive is much smaller than the PNG",
          ar["bytes"] < rt._size(src) or ar["bytes"] < 40_000,
          f"{ar['bytes']} vs {rt._size(src)}")
    check("archive lands in its own folder", Path(ar["path"]).parent.name == "_archive")
    check("bad input degrades", rt.archive_frame("/nope.png", d)["ok"] is False)
    check("bad thumb input degrades", rt.write_thumbnail("/nope.png", d / "t.png")["ok"] is False)


def test_archive_respects_its_share():
    print("the archive cannot starve live frames:")
    led = _led(budget_bytes=1000)
    led.archive_bytes = int(1000 * rt.ARCHIVE_BUDGET_FRACTION) + 1
    d = Path(tempfile.mkdtemp())
    led.admit(1, 1_000_000.0, [], {"has_error": True}, nbytes=100,
              folder=str(d), stem="frame_00001")
    plan = {"evict": [{"seq": 1, "tier": 0, "bytes": 100, "interest": 1.0,
                       "archive": True}]}
    res = led.commit_eviction(plan, archive_dir=d / "_archive",
                              deleter=lambda p: True)
    check("over its share => nothing archived", res["archived"] == 0, str(res))
    check("eviction still proceeded", res["evicted"] == 1)


def test_module_singleton():
    print("the process-wide ledger exists and is configurable:")
    check("LEDGER is a Ledger", isinstance(rt.LEDGER, rt.Ledger))
    check("defaults to the 5 GiB budget",
          rt.LEDGER.budget_bytes == 5 * 1024 ** 3, str(rt.LEDGER.budget_bytes))
    check("defaults to mode=errors", rt.LEDGER.mode == "errors", rt.LEDGER.mode)
    check("low water is below high water", rt.LOW_WATER < rt.HIGH_WATER)
    check("live_stems reflects tracked frames",
          isinstance(rt.LEDGER.live_stems(), set))


if __name__ == "__main__":
    print("=" * 70); print("RETENTION — EXAMINE BEFORE DELETE"); print("=" * 70)
    test_parse_bytes()
    test_assess_priorities()
    test_assess_interest_ordering()
    test_modes_gate_who_gets_pushed()
    test_admit_and_clearance()
    test_hold_backstop()
    test_awaiting_queue_order()
    test_no_eviction_under_high_water()
    test_eviction_order_and_protection()
    test_pinned_frames_are_last()
    test_expired_unexamined_is_reported_not_hidden()
    test_commit_keeps_history()
    test_low_disk_floor()
    test_pinned_evidence_is_never_spent_even_on_a_full_disk()
    test_commit_rechecks_the_pin_not_the_plan()
    test_emergency_passes_are_rate_limited()
    test_unseen_frames_are_capped_per_emergency_pass()
    test_unseen_pixels_need_a_real_archive_copy_on_disk()
    test_authored_annotations_are_never_deleted()
    test_free_disk_floor_cannot_exceed_the_disk()
    test_archive_bytes_cannot_make_the_target_unreachable()
    test_sweep_orphans()
    test_sweep_never_deletes_a_frame_with_a_sidecar()
    test_sweep_has_a_blast_radius_cap()
    test_sweep_refuses_when_almost_everything_looks_like_an_orphan()
    test_sweep_writes_down_what_it_removed()
    test_push_sentence()
    test_report_shape()
    test_thumbnail_and_archive()
    test_archive_respects_its_share()
    test_module_singleton()
    print("=" * 70)
    print(f"RESULT: {_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
