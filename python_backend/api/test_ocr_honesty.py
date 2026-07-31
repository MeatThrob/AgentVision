#!/usr/bin/env python3
"""OCR must not assert what it did not read, nor conflate silence with clean.

Two measured defects, both live before this suite existed.

FALSE ASSERTION. On a real capture Apple Vision read `src=0x5D80000` as
`0x5080000` and `0x800790790` as `0x800790796` — a D as 0, a 0 as 6 — and one
line came back as pure noise. Its own confidence score was **1.0 on every one of
them**, so there is no in-band gate. The push nonetheless quoted the text as
`Screen reads: "..."`, and neither OCR tool docstring caveated accuracy. For the
Metal bug this project keeps citing, that path would have handed the agent a
third address existing nowhere.

FALSE OMISSION, the more dangerous half. `ocr_error: False` meant both "OCR read
the screen and saw no error" and "OCR read nothing at all". MEASURED: 32 of 72
single-keyword error lines at 12-18px on a 2560x1440 frame produce EMPTY OCR. The
second case flowed to retention.assess() as a clean static frame — priority
P_STATIC, interest 0.05, first in the eviction queue — so the frame showing an
unreadable error dialog was the first thing deleted.

A third, rarer path: OCR can destroy the keyword while still reading the line. In
192 rendered error lines it happened once, and by SPLITTING the word
("Unhandled" -> "Unhand led"), not by substituting characters.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
for p in (str(REPO), str(REPO / "python_backend"), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from connectors.log_fields import corroborate_ocr_tokens      # noqa: E402
from utils import visual_engine as ve                          # noqa: E402
from utils.retention import assess                             # noqa: E402


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    # ── Keyword matching survives OCR word-splitting ───────────────────────
    check("split word still matches ('Unhand led' -> unhandled)",
          bool(ve.scan_error_text("Unhand led\nstate transition")))
    check("hyphen/underscore splits also match",
          bool(ve.scan_error_text("fa-tal condition"))
          and bool(ve.scan_error_text("excep_tion raised")))
    for clean in ("all systems nominal", "Main Menu   Continue   Options",
                  "loading assets 42%", "connected to 127.0.0.1"):
        check(f"no false positive on {clean[:28]!r}",
              not ve.scan_error_text(clean))
    for real in ("Render error at stage 4", "Fatal condition in device layer",
                 "Unhandled exception in worker", "Kernel panic in gpu driver"):
        check(f"still catches {real[:28]!r}", bool(ve.scan_error_text(real)))

    # ── Tri-state: silence is not cleanliness ──────────────────────────────
    for label, text, avail, want in (
            ("error visible", "Fatal error in renderer", True, ve.OCR_ERROR_FOUND),
            ("read, no error", "Main Menu  Continue", True, ve.OCR_NO_ERROR),
            ("returned empty", "", True, ve.OCR_UNREADABLE),
            ("whitespace only", "   \n  ", True, ve.OCR_UNREADABLE),
            ("OCR unavailable", "", False, ve.OCR_UNREADABLE)):
        state, _ = ve.classify_ocr_error(text, avail)
        check(f"classify: {label} -> {want}", state == want, state)
    check("the three states are distinct values",
          len({ve.OCR_ERROR_FOUND, ve.OCR_NO_ERROR, ve.OCR_UNREADABLE}) == 3)

    # ── Retention must not evict what it could not read ────────────────────
    base = {"change_score": 0.0, "changed": False, "kinds": []}
    clean = assess({**base, "ocr_error": False, "ocr_state": ve.OCR_NO_ERROR})
    blind = assess({**base, "ocr_error": False, "ocr_state": ve.OCR_UNREADABLE})
    errd = assess({**base, "ocr_error": True, "ocr_state": ve.OCR_ERROR_FOUND})

    def val(a, k):
        return a.get(k) if isinstance(a, dict) else getattr(a, k, None)

    check("a genuinely clean static frame stays cheapest",
          float(val(clean, "interest")) <= 0.10, str(val(clean, "interest")))
    check("an UNREADABLE static frame is no longer cheapest",
          float(val(blind, "interest")) > float(val(clean, "interest")),
          f"blind={val(blind,'interest')} clean={val(clean,'interest')}")
    check("...but is NOT promoted to error priority (we saw no error)",
          float(val(blind, "interest")) < float(val(errd, "interest")),
          f"blind={val(blind,'interest')} err={val(errd,'interest')}")
    check("a real on-screen error still outranks both",
          float(val(errd, "interest")) >= 0.8, str(val(errd, "interest")))
    # A missing ocr_state must behave exactly as before — no silent change for
    # callers that predate the field.
    legacy = assess({**base, "ocr_error": False})
    check("absent ocr_state is backwards-compatible",
          float(val(legacy, "interest")) == float(val(clean, "interest")),
          f"legacy={val(legacy,'interest')}")

    # ── Corroboration: check, never assert ─────────────────────────────────
    ocr = ("IMEIAL! guestgpu-present src=0x5080000 ok=ralse\n"
           "[CPU] arm64Blk=7626 frames=1024 build v5.0.2 clock 14:32:07")
    log = "src=0x5D80000 ok=False arm64Blk=7626 frames=1024"
    toks = {t["token"]: t["appears_in_log"] for t in
            corroborate_ocr_tokens(ocr, log)}
    check("the corrupted hex is flagged as not-in-log",
          toks.get("0x5080000") is False, json.dumps(toks))
    check("the corrupted flag value is flagged", toks.get("ralse") is False)
    check("correct values are corroborated",
          toks.get("7626") is True and toks.get("1024") is True, json.dumps(toks))
    check("it reports appears_in_log, never 'verified'",
          all("verified" not in t for t in
              json.dumps(corroborate_ocr_tokens(ocr, log)).split()))
    # Address spellings must normalise or the check produces spurious misses.
    check("hex spelling variants normalise",
          corroborate_ocr_tokens("addr 0x0000000804000010",
                                 "used 804000010")[0]["appears_in_log"] is True)
    check("empty OCR yields no claims at all",
          corroborate_ocr_tokens("", "anything") == [])
    check("prose is not flagged (no claim either way)",
          not [t for t in corroborate_ocr_tokens("Continue   Options", log)],
          "UI labels must not be graded against the log")

    # ── The advertised caveats must actually be present ────────────────────
    mcp = (REPO / "python_backend/api/claude_mcp.py").read_text()
    for tool in ("av_ocr_frame", "av_read_screen"):
        seg = mcp.split(f"def {tool}(", 1)[-1][:2600]
        check(f"{tool} docstring warns about ACCURACY",
              "ACCURACY" in seg, seg[:80])
        check(f"{tool} says empty != clean",
              "UNREADABLE" in seg or "not readable" in seg)
    amb = (REPO / "python_backend/api/ambient.py").read_text()
    check("push no longer quotes OCR as verbatim 'Screen reads:'",
          'Screen reads: "' not in amb)
    # Matched without the closing paren: the string is now an f-string that
    # interpolates the SOURCE FRAME when the snippet is not from the frame being
    # described ("approximate from frame #100, NOT the frame below").
    check("push names the transcription step",
          "Screen OCR (approximate" in amb)
    check("push attributes OCR to the frame it came from",
          "NOT the frame below" in amb and "ocr_seq" in amb)
    check("push says unreadable is not clean",
          "not the same as the" in amb)

    # ── Serving a frame: never downscale text away, always declare ─────────
    import tempfile
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    tmp = Path(tempfile.mkdtemp(prefix="av-serve-"))

    def _font(sz):
        try:
            return ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", sz)
        except Exception:
            return ImageFont.load_default()

    texty = Image.new("RGB", (2560, 1440), (24, 26, 30))
    dr = ImageDraw.Draw(texty)
    for i in range(14):
        dr.text((40, 100 + i * 22), f"[METAL] guestgpu.present src=0x{i:07X} ok=False",
                font=_font(13), fill=(200, 204, 210))
    texty.save(tmp / "texty.png")

    pic = Image.new("RGB", (2560, 1440))
    dp = ImageDraw.Draw(pic)
    for y in range(1440):
        dp.line([(0, y), (2560, y)], fill=(30 + y // 24, 40 + y // 18, 90 + y // 16))
    dp.ellipse([700, 400, 1900, 1100], fill=(210, 180, 120))
    pic.filter(ImageFilter.GaussianBlur(1.2)).save(tmp / "pic.png")

    p_text = ve.serve_plan(str(tmp / "texty.png"), 2560, 1440)
    p_pic = ve.serve_plan(str(tmp / "pic.png"), 2560, 1440)
    check("a TEXT-BEARING frame is served at full resolution",
          p_text["serve"] == "full", json.dumps(p_text)[:140])
    check("a PICTORIAL frame is served downscaled",
          p_pic["serve"] == "downscaled", json.dumps(p_pic)[:140])
    check("the downscale is a real saving (16x)",
          p_pic.get("saves_tokens", 0) > 4000, str(p_pic.get("saves_tokens")))
    check("both plans DECLARE what was decided and why",
          bool(p_text.get("reason")) and bool(p_pic.get("reason")))
    check("both report the full-resolution cost",
          p_text.get("tokens_full") == p_pic.get("tokens_full") == 4784,
          f"{p_text.get('tokens_full')} / {p_pic.get('tokens_full')}")
    check("text density separates the two by a clear margin",
          p_text["text_density"] > p_pic["text_density"] * 2,
          f"{p_text['text_density']:.4f} vs {p_pic['text_density']:.4f}")
    check("an unmeasurable frame defaults to FULL, never to cheap",
          ve.serve_plan(str(tmp / "does-not-exist.png"), 2560, 1440)["serve"]
          == "full")
    check("an already-small frame is never downscaled further",
          ve.serve_plan(str(tmp / "pic.png"), 640, 360)["serve"] == "full")

    # ── NO OCR ENGINE AT ALL: the off-mac case ─────────────────────────────
    # On Linux/Windows the OCR backend is tesseract, which is OPTIONAL. With no
    # engine, EVERY frame classifies could_not_read. Before the needs_eyes fix
    # that made every frame in mode=changes/all await examination — measured, 50
    # of 60 held — so the disk filled to the 900s hold backstop on a machine
    # where nothing was wrong. Invisible on a Mac, where Apple Vision is built in.
    from utils.retention import Ledger
    st_absent, hits_absent = ve.classify_ocr_error("", available=False)
    check("no OCR engine -> could_not_read (not 'clean')",
          st_absent == ve.OCR_UNREADABLE and not hits_absent, st_absent)
    blind_facts = {"change_score": 0.0, "changed": False, "kinds": [],
                   "ocr_error": False, "ocr_state": st_absent}
    for mode in ("errors", "changes", "all"):
        a = assess(blind_facts, mode=mode)
        check(f"mode={mode}: an unreadable frame is NOT held for examination",
              a["needs_eyes"] is False,
              f"needs_eyes={a['needs_eyes']} pri={a['priority']}")
        check(f"mode={mode}: ...but is still ranked above a clean static frame",
              float(a["interest"]) > 0.05, str(a["interest"]))

    tmpd = Path(tempfile.mkdtemp(prefix="av-noengine-"))
    led = Ledger()
    led.configure(budget_bytes=1_000_000, mode="changes")
    for i in range(60):
        f = tmpd / f"frame_{i:05d}.png"
        f.write_bytes(b"x" * 250_000)
        led.admit(seq=i, ts_ms=1000.0 + i, paths=[str(f)], facts=blind_facts)
    pl = led.plan_eviction()
    check("60 unreadable frames over budget -> disk IS reclaimable",
          len(pl.get("evict") or []) > 0, str(len(pl.get("evict") or [])))
    check("...and nothing is left awaiting examination",
          len(led.awaiting() or []) == 0, str(len(led.awaiting() or [])))
    check("...and the budget is actually met",
          int(pl.get("unmet_bytes") or 0) == 0, str(pl.get("unmet_bytes")))

    # A frame OCR never looked at must not be treated as blind either.
    untouched = assess({"change_score": 0.0, "changed": False, "kinds": [],
                        "ocr_error": False, "ocr_state": ""}, mode="all")
    clean_ref = assess({"change_score": 0.0, "changed": False, "kinds": [],
                        "ocr_error": False, "ocr_state": ve.OCR_NO_ERROR},
                       mode="all")
    check("'no OCR opinion' behaves like a clean frame, not a blind one",
          untouched["interest"] == clean_ref["interest"],
          f"{untouched['interest']} vs {clean_ref['interest']}")

    fails = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
        if not ok and detail:
            print(f"          {detail}")
    print(f"\n{len(checks) - len(fails)}/{len(checks)} checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
