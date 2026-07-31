"""
Log lines are RECORDS, not sentences.
================================================================================
This module extracts the structure that is already sitting in a log line and
throws nothing away. It does not summarize, rank, or judge — the raw line is
always preserved verbatim; these are additional facts about it.

WHY IT EXISTS. The bug that took the longest on this project was found by eye,
in raw text, from this shape:

    draw#1 target=0x1240000:3840x2160 ... [tex0=0x5D80000:1280x720 ->UPLOAD]
    ...
    guestgpu.present src=0x5D80000 ok=False        (x180)

The insight is one sentence: **0x5D80000 appears as `tex0` and as `src`, but
NEVER as `target`** — so the address being presented is one nothing ever renders
into, and the present lookup can only ever miss. Every byte needed to say that
was in the log the whole time. Reading a line as prose loses it; reading it as
{key -> value} makes it a query.

So the unit of value here is the ROLE INDEX: for every value (especially a guest
address or handle), which keys did it appear under, and how often. That converts
"read 45,000 lines and notice something" into "ask which addresses are never a
target". No model, no inference — just counting what the program already said.

DESIGN RULES
  * Non-destructive. `extract_fields` returns extra data; it never rewrites,
    re-levels or drops a line.
  * Verbatim values. `tex0=0x5D80000:1280x720px3686400->UPLOAD` keeps its whole
    value string; the embedded address is offered ALONGSIDE, not instead.
  * No severity, no ranking, no opinion. That belongs to the reader.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Iterable, Optional

#: key=value / key:value. The key is a bare identifier (letters, digits, _, #),
#: the value runs to whitespace or a closing bracket. Deliberately greedy on the
#: value so composite values such as `0x5D80000:1280x720px3686400->UPLOAD`
#: survive intact — splitting them here would be the interpretation this module
#: refuses to do.
_KV_RE = re.compile(
    r"(?<![\w.])([A-Za-z][A-Za-z0-9_#.\-]{0,40})\s*[=:]\s*([^\s,;\]\)}]+)")

#: Hex tokens anywhere in the line, with or without the 0x prefix (>=5 digits so
#: ordinary small numbers and short hex-looking words are not swept up).
_HEX_RE = re.compile(r"\b(0[xX][0-9A-Fa-f]{4,16}|[0-9A-Fa-f]{6,16})\b")

#: Bare decimal numbers, kept separate from k=v so metrics can be trended later.
_NUM_RE = re.compile(r"(?<![\w.])(-?\d+(?:\.\d+)?)(?![\w.])")

#: Keys that are almost never an entity role and would only add noise to the
#: role index (sizes, counts, timings). Excluded from ADDRESS roles only — they
#: are still returned in `fields`.
_NON_ROLE_KEYS = frozenset({
    "size", "bytes", "len", "length", "count", "n", "idx", "index", "ms", "us",
    "ns", "s", "sec", "secs", "seconds", "pct", "percent", "width", "height",
    "w", "h", "line", "lineno", "pid", "tid", "port",
})


def normalize_address(tok: str) -> str:
    """Canonical form for a hex token: lower-case, 0x-prefixed, no leading zeros.

    So `0x0000000804000010`, `0x804000010` and `804000010` all compare equal —
    real logs mix all three for the same address, and a role index that treats
    them as different entities is worse than useless.
    """
    t = tok.strip().lower()
    if t.startswith("0x"):
        t = t[2:]
    t = t.lstrip("0") or "0"
    return "0x" + t


def extract_fields(line: str) -> dict:
    """Structure found in one raw line. The line itself is never modified.

    Returns {fields: {k: v}, addresses: [canonical...], numbers: [float...],
    field_addresses: {k: canonical}} where `field_addresses` is the subset of
    fields whose value contains an address — that mapping is what makes the role
    index possible.
    """
    if not line:
        return {"fields": {}, "addresses": [], "numbers": [],
                "field_addresses": {}}
    text = str(line)

    fields: dict[str, str] = {}
    field_addresses: dict[str, str] = {}
    for m in _KV_RE.finditer(text):
        key, val = m.group(1), m.group(2)
        # Last occurrence wins: a line that repeats a key is stating its final
        # value, and picking the first would silently report a stale one.
        fields[key] = val
        hx = _HEX_RE.search(val)
        if hx:
            field_addresses[key] = normalize_address(hx.group(1))

    addresses: list[str] = []
    seen: set[str] = set()
    for m in _HEX_RE.finditer(text):
        a = normalize_address(m.group(1))
        if a not in seen:
            seen.add(a)
            addresses.append(a)

    numbers: list[float] = []
    for m in _NUM_RE.finditer(text):
        try:
            numbers.append(float(m.group(1)))
        except ValueError:
            continue

    return {"fields": fields, "addresses": addresses, "numbers": numbers,
            "field_addresses": field_addresses}


def build_role_index(lines: Iterable, *,
                     key_filter: Optional[set] = None) -> dict:
    """THE QUERY. For every address, which keys did it appear under, how often.

    `lines` accepts raw strings, or the {line, repeat:N} dicts that the raw-log
    reader emits for collapsed runs — a run of 180 identical lines counts as 180
    observations, not one, because the count is the whole point.

    Returns {addresses: {addr: {roles: {key: n}, total: n, first_line, last_line}},
    keys: {key: n}, lines_scanned: n}. Facts only: which address played which
    role, and how many times. What that MEANS is the reader's call.
    """
    per_addr: dict = defaultdict(lambda: {"roles": Counter(), "total": 0,
                                          "first_line": None, "last_line": None})
    key_counts: Counter = Counter()
    scanned = 0

    for item in lines or []:
        if isinstance(item, dict):
            text = str(item.get("line") or "")
            weight = int(item.get("repeat") or 1)
        else:
            text = str(item)
            weight = 1
        if not text.strip():
            continue
        scanned += weight
        ex = extract_fields(text)
        for key, addr in (ex["field_addresses"] or {}).items():
            if key.lower() in _NON_ROLE_KEYS:
                continue
            if key_filter and key not in key_filter:
                continue
            rec = per_addr[addr]
            rec["roles"][key] += weight
            rec["total"] += weight
            if rec["first_line"] is None:
                rec["first_line"] = text[:200]
            rec["last_line"] = text[:200]
            key_counts[key] += weight

    return {
        "addresses": {a: {"roles": dict(r["roles"]), "total": r["total"],
                          "first_line": r["first_line"],
                          "last_line": r["last_line"]}
                      for a, r in per_addr.items()},
        "keys": dict(key_counts),
        "lines_scanned": scanned,
    }


def roles_never(index: dict, key: str) -> list[dict]:
    """Addresses that appear in the index but NEVER under `key`.

    This is the shape of the question that solved the hard bug: "which address is
    presented but is never a render target?" -> roles_never(index, "target").
    Sorted by how often the address was seen, because a thing observed 180 times
    and never once in the expected role is the interesting one.
    """
    out = []
    for addr, rec in (index.get("addresses") or {}).items():
        roles = rec.get("roles") or {}
        if key in roles:
            continue
        out.append({"address": addr, "total": rec.get("total", 0),
                    "roles": roles, "example": rec.get("first_line")})
    out.sort(key=lambda r: -r["total"])
    return out


# ── OCR corroboration ─────────────────────────────────────────────────────────

def corroborate_ocr_tokens(ocr_text: str, log_text: str,
                           limit: int = 12) -> list[dict]:
    """Which machine-shaped tokens READ OFF THE SCREEN also appear in the log.

    Why this exists. OCR is confidently wrong at the character level and there is
    no in-band signal that says when. MEASURED on a real capture: Apple Vision
    read `src=0x5D80000` as `0x5080000` and `0x800790790` as `0x800790796` — a D
    became a 0, a 0 became a 6 — and reported **confidence 1.0 on every line,
    including one that was pure noise**. So confidence cannot gate it, and a
    screen-read address handed to an agent as fact is a false read of exactly the
    kind this project exists to prevent: for the Metal bug it kept citing, OCR
    would have supplied a third address that exists nowhere.

    The one thing AgentVision has that a screenshot alone does not is the
    time-aligned log for the same instant. So: check, don't assert.

    Scoped to HEX, long digit runs and k=v values on purpose. Those are the
    tokens where OCR fails most (dense alphanumerics carry no linguistic
    redundancy for the recognizer to lean on — which is exactly why the hex broke
    and `DeviceLostException` did not), where a wrong value is worthless rather
    than merely imprecise, and where the log is the natural referent. Prose and
    UI labels get no claim in either direction; flagging a title-screen menu item
    as "unverified" would only teach the agent to ignore the flag.

    Reports `appears_in_log`, never `verified`. A corruption can land on another
    real token (`0x5D80000` -> `0xB870000`, both real), so presence is not proof
    — only ABSENCE is informative, and even then it means "this value appears
    nowhere in the aligned window", never "this value is wrong". A clock, an FPS
    counter or a version string legitimately appears in no log.
    """
    if not (ocr_text or "").strip():
        return []
    log = log_text or ""
    log_hex = {normalize_address(m) for m in _HEX_RE.findall(log)}
    log_num = {m for m in _NUM_RE.findall(log)}
    log_low = log.lower()

    seen: set[str] = set()
    out: list[dict] = []
    for line in (ocr_text or "").splitlines():
        for tok in _HEX_RE.findall(line):
            key = normalize_address(tok)
            if key in seen:
                continue
            seen.add(key)
            out.append({"token": tok, "kind": "hex",
                        "appears_in_log": key in log_hex})
        for _k, val in _KV_RE.findall(line):
            if len(val) < 4 or val.lower() in seen:
                continue
            seen.add(val.lower())
            out.append({"token": val, "kind": "value",
                        "appears_in_log": val.lower() in log_low})
        for tok in _NUM_RE.findall(line):
            if len(tok.lstrip("-")) < 4 or tok in seen:
                continue
            seen.add(tok)
            out.append({"token": tok, "kind": "number",
                        "appears_in_log": tok in log_num})
        if len(out) >= limit:
            break
    return out[:limit]
