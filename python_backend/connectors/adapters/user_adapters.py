"""
USER (runtime-added) log adapters — AgentVision's self-extension surface
================================================================================
THE FORCE, part 2. When pre-flight (connectors.log_sources.preflight_report)
finds a program whose logs only route to a generic fallback, the AI closes the
gap by ADDING an adapter at runtime via the /adapter/add route (av_add_adapter).
That new adapter must (a) take effect immediately and (b) SURVIVE A RESTART.

This module is the persistence layer for exactly that. User adapters are stored
as compact JSON specs in `user_adapters.json` beside this file; each spec is
turned back into a live `RxAdapter` subclass by `build_adapter()` and wired into
the shared REGISTRY by `register_from_spec()`. The package __init__ imports this
module LAST (after every built-in family, even batch9) so a user adapter can
NEVER disturb a built-in on a 1.0-confidence tie: `detect_adapter` breaks ties by
REGISTRY order (first max wins) and these load last, so a user adapter can only
ever GAIN a sample that previously fell to structural / generic_ts / raw — which
is precisely the coverage gap it was added to close.

Spec shape (all but name/extract optional):
    {
      "name":        "myfmt",                 # short stable id, unique
      "family":      "myservice",             # informational grouping
      "language":    "python",               # ecosystem hint (default "any")
      "detect":      {"regex": "^...",         # detection signature (defaults to
                       "anchor_tokens": ["X"]}, #   the extract regex); ALL anchor
                                                #   tokens must appear in the line
      "extract":     {"regex": "^(?P<ts>...)\\s+(?P<level>\\w+)\\s+(?P<message>.*)$"},
      "level_map":   {"WARNING": "WARN"},      # raw token → canonical level
      "default_level":  "INFO",
      "default_source": "myservice",
      "category":    "",                       # fixed category (e.g. "event")
      "match_scope": "lines",                 # "lines" | "first" (multi-line recs)
      "sample":      "2026-... INFO hello"     # a real line this must own
    }

Field-extraction named groups understood by the RxAdapter base: `ts`, `level`,
`source`, `msg`. For ergonomics `message`→`msg` and `timestamp`→`ts` are aliased
automatically, so a spec may use the friendlier names.

Pure standard library; never raises into import.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional

from ._common import LogAdapter, register_adapter, RxAdapter, parse_timestamp

# The generic fallbacks a user adapter is allowed to displace (never a named one).
FALLBACK_ADAPTERS = {"structural", "generic_ts", "raw"}

_STORE = Path(__file__).with_name("user_adapters.json")

# names that can never be taken by a user adapter (pinned registry tail + reserved)
_RESERVED = {"structural", "raw"}


# ── persistence ──────────────────────────────────────────────────────────────

#: Did the last _load_store() see the whole file? user_adapters.json is the
#: user's own hand-editable data (load_all() says so explicitly), and adding one
#: adapter REWRITES the whole store from this list. A read/parse failure used to
#: come back as `[]`, so one JSON typo in the file turned the next add into "the
#: store contains exactly the adapter I just added" — every other user adapter
#: gone. The atomic write below does not help with that at all: atomicity
#: guarantees the WRONG CONTENT lands intact.
_LAST_STORE_LOAD_OK = True
_LAST_STORE_LOAD_ERROR = ""


class UserAdapterStoreUnreadable(RuntimeError):
    """Raised instead of replacing user_adapters.json with a partial view."""


def store_load_ok() -> tuple[bool, str]:
    return _LAST_STORE_LOAD_OK, _LAST_STORE_LOAD_ERROR


def _load_store() -> list:
    """Return the persisted list of user-adapter specs (or [])."""
    global _LAST_STORE_LOAD_OK, _LAST_STORE_LOAD_ERROR
    _LAST_STORE_LOAD_OK, _LAST_STORE_LOAD_ERROR = True, ""
    if not _STORE.exists():
        return []
    try:
        data = json.loads(_STORE.read_text(encoding="utf-8"))
    except Exception as exc:
        _LAST_STORE_LOAD_OK = False
        _LAST_STORE_LOAD_ERROR = f"{_STORE.name} could not be parsed: {exc}"
        print(f"[user_adapters] WARNING: {_LAST_STORE_LOAD_ERROR} — persisting "
              f"is BLOCKED so your other adapters are not replaced")
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return list(data.get("adapters", []))
    _LAST_STORE_LOAD_OK = False
    _LAST_STORE_LOAD_ERROR = (f"{_STORE.name} holds a {type(data).__name__}, "
                              f"expected a list or an object")
    print(f"[user_adapters] WARNING: {_LAST_STORE_LOAD_ERROR} — persisting is "
          f"BLOCKED")
    return []


def _save_store(specs: list, *, force: bool = False) -> None:
    if not _LAST_STORE_LOAD_OK and not force:
        raise UserAdapterStoreUnreadable(
            f"refusing to rewrite {_STORE}: {_LAST_STORE_LOAD_ERROR}. The whole "
            f"store is replaced by this write, so saving now would discard every "
            f"adapter the file still contains. Repair or move it first.")
    # Keep one generation back: the specs are hand-authored.
    try:
        if _STORE.exists():
            _STORE.with_suffix(".json.bak").write_text(
                _STORE.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass
    tmp = _STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(specs, indent=2), encoding="utf-8")
    tmp.replace(_STORE)          # atomic on POSIX + Windows


def list_specs() -> list:
    return _load_store()


# ── spec → live adapter ────────────────────────────────────────────────────────

_ALIASES = (("message", "msg"), ("timestamp", "ts"))


def _normalize_pattern(pattern: str) -> str:
    """Alias the friendly named groups (message→msg, timestamp→ts) onto the names
    the RxAdapter base reads, unless the target name is already present."""
    for friendly, canonical in _ALIASES:
        if f"(?P<{friendly}>" in pattern and f"(?P<{canonical}>" not in pattern:
            pattern = pattern.replace(f"(?P<{friendly}>", f"(?P<{canonical}>")
    return pattern


#: Keys a model plausibly reaches for instead of the real one. Used only to make
#: an error name the ACTUAL mistake; nothing is silently accepted under an alias.
_REGEX_NEAR_MISSES = ("pattern", "regexp", "re", "rx", "expr", "expression",
                      "format", "parse", "extract_regex", "line_regex")
_SAMPLE_NEAR_MISSES = ("sample_line", "example", "example_line", "line",
                       "sample_lines", "samples", "test_line")

_GOOD_BODY = ('{"name": "my_fmt", "extract": {"regex": '
              '"^(?P<level>[A-Z]+) (?P<message>.*)$"}, "sample": "INFO it works"}')


def _found_near_miss(d: dict, candidates) -> str:
    """The first near-miss key actually present in `d`, or ""."""
    if not isinstance(d, dict):
        return ""
    lower = {str(k).lower(): str(k) for k in d}
    for c in candidates:
        if c in lower:
            return lower[c]
    return ""


def _missing_regex_error(spec: dict) -> str:
    """Say what is wrong with THIS body, not what is wrong with the average body.

    The original message was a flat "spec.extract.regex is required", which named
    a nested path that must not exist and so confirmed the wrong envelope — a cold
    model burned 13 attempts on it. Replacing the text with a fixed lecture about
    `extract` being top-level only moved the problem: a caller who sent
    `{"regex": ...}` at the top level, or `extract` as a plain string, was told to
    do the one thing it had already done. An error that describes a mistake the
    caller did not make is the expensive kind, because it argues them AWAY from
    the real fix. So diagnose the body that actually arrived.
    """
    tail = f" Correct shape: {_GOOD_BODY}"
    extract = spec.get("extract") if isinstance(spec, dict) else None

    if isinstance(extract, str):
        return ("`extract` was sent as a STRING, but it must be an OBJECT with a "
                "`regex` key. You almost certainly meant: "
                f'"extract": {{"regex": {json.dumps(extract[:120])}}}.' + tail)
    if isinstance(extract, (list, tuple)):
        return ("`extract` was sent as a LIST, but it must be an OBJECT with a "
                "single `regex` key holding one pattern." + tail)
    if isinstance(extract, dict) and extract:
        near = _found_near_miss(extract, _REGEX_NEAR_MISSES)
        if near:
            return (f"`extract` is present but has no `regex` key — it has "
                    f"`{near}`. Rename it: \"extract\": {{\"regex\": ...}}. The "
                    f"field-extraction pattern is read from `regex` only." + tail)
        return (f"`extract` is present but has no `regex` key (found: "
                f"{sorted(str(k) for k in extract)[:6]}). The field-extraction "
                f"pattern must be at extract.regex." + tail)

    # No usable `extract` at all. Is the pattern sitting somewhere else?
    if isinstance(spec, dict):
        near = _found_near_miss(spec, ("regex",) + _REGEX_NEAR_MISSES)
        if near:
            val = spec.get(near)
            shown = json.dumps(str(val)[:120]) if isinstance(val, str) else "..."
            return (f"The pattern was sent as a top-level `{near}` key. It belongs "
                    f"INSIDE `extract`: \"extract\": {{\"regex\": {shown}}}. "
                    f"Nothing else about the body needs to change." + tail)
    return ("extract.regex is required — the field-extraction pattern, as "
            "`regex` inside a top-level `extract` object." + tail)


def build_adapter(spec: dict) -> LogAdapter:
    """Build (do NOT register) a live RxAdapter subclass from a spec.
    Raises ValueError on a structurally invalid spec (bad/absent regex, no name)."""
    if not isinstance(spec, dict):
        raise ValueError("spec must be an object")
    name = str(spec.get("name", "")).strip()
    if not name:
        raise ValueError("name is required — a TOP-LEVEL field of the request body")
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", name):
        raise ValueError("name may contain only letters, digits, _.- ")
    if name in _RESERVED:
        raise ValueError(f"'{name}' is a reserved fallback adapter name")

    extract = spec.get("extract") or {}
    extract_pat = extract.get("regex") if isinstance(extract, dict) else None
    if not extract_pat:
        raise ValueError(_missing_regex_error(spec))
    try:
        extract_re = re.compile(_normalize_pattern(str(extract_pat)))
    except re.error as e:
        raise ValueError(f"extract.regex is not a valid regex: {e}")

    detect_cfg = spec.get("detect") or {}
    detect_pat = detect_cfg.get("regex") if isinstance(detect_cfg, dict) else None
    if detect_pat:
        try:
            detect_re = re.compile(str(detect_pat))
        except re.error as e:
            raise ValueError(f"detect.regex is not a valid regex: {e}")
    else:
        detect_re = extract_re
    anchors = [str(t) for t in (detect_cfg.get("anchor_tokens") or []) if str(t).strip()]

    lvl_map = {str(k): str(v) for k, v in (spec.get("level_map") or {}).items()}
    default_level = str(spec.get("default_level") or "")
    default_source = str(spec.get("default_source") or spec.get("family") or "")
    fixed_category = str(spec.get("category") or "")
    scope = spec.get("match_scope") or "lines"
    if scope not in ("lines", "first"):
        scope = "lines"
    language = str(spec.get("language") or "any")

    class _UserAdapter(RxAdapter):
        _RE = extract_re
        _DETECT_RE = detect_re
        _ANCHORS = tuple(anchors)
        _LVL_MAP = lvl_map
        match_scope = scope
        default_level = ""            # set below (class attr shadow)
        default_source = ""
        fixed_category = ""

        def _hit(self, x: str) -> bool:
            s = x.strip()
            if self._ANCHORS and not all(tok in s for tok in self._ANCHORS):
                return False
            return bool(self._DETECT_RE.match(s)) or bool(self._DETECT_RE.search(s))

    _UserAdapter.name = name
    _UserAdapter.language = language
    _UserAdapter.default_level = default_level
    _UserAdapter.default_source = default_source
    _UserAdapter.fixed_category = fixed_category
    return _UserAdapter()


# ── validation (self-route + collision guard), non-mutating ─────────────────────

def _best_excluding(line: str, exclude: str) -> tuple:
    """(name, score) of the best-scoring CURRENTLY-registered adapter for `line`,
    ignoring any adapter named `exclude`. Uses the live REGISTRY."""
    from connectors import log_adapters as la  # late import: avoid import cycle
    best_name, best = "raw", 0.0
    for a in la.REGISTRY:
        if a.name == exclude:
            continue
        try:
            s = a.detect([line])
        except Exception:
            s = 0.0
        if s > best:
            best, best_name = s, a.name
    return best_name, best


def _catalog_samples() -> list:
    """Every catalog sample_line (for the collision guard), or [] if unavailable."""
    here = Path(__file__).resolve()
    for cand in (here.parents[3] / "docs" / "log_catalog_master.json",
                 here.parents[2] / "docs" / "log_catalog_master.json",
                 Path("docs/log_catalog_master.json")):
        try:
            if cand.exists():
                data = json.loads(cand.read_text(encoding="utf-8"))
                return [e.get("sample_line", "") for e in data.get("catalog", [])
                        if e.get("sample_line")]
        except Exception:
            continue
    return []


def validate_spec(spec: dict) -> dict:
    """Validate a spec WITHOUT registering it. Returns:
        {ok, errors:[…], adapter_name, self_route, collisions:[…]}
    ok is True only when the built adapter (a) matches + wins its own `sample`
    and (b) would not STEAL any existing catalog sample from a NAMED adapter."""
    from connectors import log_adapters as la
    errors: list[str] = []
    try:
        cand = build_adapter(spec)
    except ValueError as e:
        return {"ok": False, "errors": [str(e)], "adapter_name":
                str(spec.get("name", "")) if isinstance(spec, dict) else "",
                "self_route": None, "collisions": []}

    name = cand.name

    # ── name-collision guard ─────────────────────────────────────────────────
    # register_adapter() is idempotent BY NAME, so reusing a built-in's name
    # would REPLACE (silently shadow) that built-in rather than add alongside
    # it. Reject it and name a concrete alternative. Re-using a name that is
    # already a USER adapter is fine — that is an in-place update.
    if name in _RESERVED:
        errors.append(f"'{name}' is a reserved fallback name — choose another.")
    else:
        try:
            _builtins = la.builtin_names()
        except Exception:
            _builtins = frozenset()
        if name in _builtins and name not in {
                str(s.get("name", "")) for s in _load_store()}:
            errors.append(
                f"'{name}' is the name of a BUILT-IN adapter — adding it would "
                f"silently shadow the built-in (adapters are keyed by name, so "
                f"yours would replace it and the built-in's format would stop "
                f"being recognised). Pick a distinct name, e.g. "
                f"'{name}_{str(spec.get('family') or 'custom').strip().lower() or 'custom'}' "
                f"or '{name}_user'. Both adapters can then coexist.")

    sample = str(spec.get("sample", "") or "")
    self_route = {"sample": sample[:300]}
    if not sample.strip():
        near = _found_near_miss(spec if isinstance(spec, dict) else {},
                               _SAMPLE_NEAR_MISSES)
        if near:
            errors.append(
                f"`sample` is required, and you sent `{near}` instead — rename it. "
                f"It must be ONE real log line as a string (not a list), because "
                f"the adapter is rejected unless that exact line routes to it.")
        else:
            errors.append("sample is required (a real line the adapter must own) — "
                          "a TOP-LEVEL field of the request body")
    else:
        own = 0.0
        try:
            own = cand.detect([sample])
        except Exception:
            own = 0.0
        self_route["own_score"] = round(own, 3)
        if own <= 0.0:
            errors.append("extract/detect pattern does not match the provided sample")
        else:
            w_name, w_score = _best_excluding(sample, name)
            # `runner_up` is always informative; `would_lose_to` is a WARNING and
            # must only appear when a loss is real. It used to be set on every
            # successful add, so a response could read own_score 1.0 alongside
            # "would_lose_to: structural 0.3" — a self-contradiction that a cold
            # model flagged as the response being untrustworthy.
            #
            # Gating it on `w_score >= own` is still not enough, because a TIE is
            # not a loss when `outrank` names the incumbent: placement then decides
            # and the new adapter wins. Measured: adding a second adapter with
            # outrank= on an exact 1.0/1.0 tie returned ok=true, outranks=<name>
            # AND would_lose_to=<same name> 1.0 — the same self-contradiction the
            # first fix was for. So decide the outrank question FIRST and report
            # the warning only when the candidate genuinely does not win.
            self_route["runner_up"] = {"adapter": w_name,
                                       "score": round(w_score, 3)}
            outrank = str(spec.get("outrank") or "").strip()
            # A tie the caller asked to break by placement, and can: own >= w.
            placement_wins = bool(outrank) and outrank == w_name and own >= w_score
            if own > w_score:
                self_route["wins_by"] = round(own - w_score, 3)
            elif placement_wins:
                self_route["wins_by"] = 0.0
                self_route["wins_by_placement"] = (
                    f"tie at {round(own, 3)} broken by outrank='{outrank}' — this "
                    f"adapter registers AHEAD of it, so the sample routes here")
            else:
                self_route["would_lose_to"] = {"adapter": w_name,
                                               "score": round(w_score, 3)}
            # Placement decides ties. By default a new adapter registers LAST among
            # named ones, so it must STRICTLY beat every incumbent.
            #
            # `outrank` changes that, and it exists because "already covered" was
            # being reported for formats that are covered WRONGLY. Live example: a
            # C engine logging `[DEBUG] Sprite.c:32 - msg` was claimed by
            # `coreboot_cbmem` at 1.0, which put the file:line inside the message
            # and set source=coreboot. A tie then handed the format to the
            # incumbent and there was no way for the agent to install a correct,
            # more specific adapter for its own program — the exact case
            # register_adapter(before=...) was built for, never exposed.
            if outrank and outrank == w_name:
                if own < w_score:
                    errors.append(
                        f"outrank='{outrank}' but '{w_name}' still scores higher "
                        f"({round(w_score, 3)} > {round(own, 3)}) — placement only "
                        f"breaks a TIE, it cannot rescue a weaker pattern.")
                else:
                    self_route["outranks"] = outrank
            elif own <= w_score:
                errors.append(
                    f"sample would not route to '{name}': existing adapter "
                    f"'{w_name}' scores {round(w_score, 3)} >= {round(own, 3)}. "
                    f"If that adapter parses this format INCORRECTLY, pass "
                    f"outrank='{w_name}' to register ahead of it; otherwise the "
                    f"format is already covered or the pattern is too loose.")

    # ── also_match: the OTHER real lines must match too ──────────────────────
    # A spec only ever had to route its own single `sample`, which is a much weaker
    # claim than "this adapter parses this format". Measured on a cold model handed
    # one sample line: it set anchor_tokens to a fragment of that line's MESSAGE
    # (":: spool"), scored own 1.00, was accepted, and then matched 1 line in 4 —
    # the rest fell to `structural`, and the model concluded the tool was broken
    # rather than its adapter. The catalog's how_to_add_an_adapter recipe now hands
    # over these extra lines pre-filled, and they are binding.
    also = spec.get("also_match") or []
    if isinstance(also, str):
        also = [also]
    also = [str(x) for x in also if str(x).strip()][:8]
    if also:
        missed = []
        for ln in also:
            try:
                if cand.detect([ln]) <= 0.0:
                    missed.append(ln)
            except Exception:
                missed.append(ln)
        self_route["also_match_checked"] = len(also)
        if missed:
            errors.append(
                f"{len(missed)} of {len(also)} also_match line(s) do NOT match this "
                f"pattern, so it parses one line rather than this format. First "
                f"miss: {missed[0][:120]!r}. Usual cause: an anchor_token or a "
                f"literal in the regex taken from ONE message body — anchors must "
                f"be structure present in every line (a bracket, a separator, a "
                f"prefix), never a word from a message.")
        else:
            self_route["also_match_all_ok"] = True

    # collision guard — only lines the candidate actually matches need checking.
    collisions: list[dict] = []
    _outranked = str(spec.get("outrank") or "").strip()
    for line in _catalog_samples():
        try:
            cs = cand.detect([line])
        except Exception:
            cs = 0.0
        if cs <= 0.0:
            continue
        winner, score, _ = la.detect_adapter([line])
        if cs > score and winner.name not in FALLBACK_ADAPTERS and winner.name != name:
            collisions.append({"sample": line[:200], "would_steal_from": winner.name,
                               "their_score": round(score, 3),
                               "candidate_score": round(cs, 3)})
            if len(collisions) >= 8:
                break
    if collisions:
        errors.append(
            f"'{name}' would STEAL {len(collisions)} sample(s) from existing named "
            "adapter(s) — tighten the detect signature (anchor_tokens / stricter "
            "regex) so it only matches its own format.")

    return {"ok": not errors, "errors": errors, "adapter_name": name,
            "self_route": self_route, "collisions": collisions}


# ── register (+ optionally persist) ────────────────────────────────────────────

def register_from_spec(spec: dict, *, persist: bool = False) -> LogAdapter:
    """Build + register a user adapter into the live REGISTRY (idempotent by
    name — a re-register replaces the prior instance). Persists the spec when
    persist=True so it is reloaded on the next import/restart."""
    ad = build_adapter(spec)
    _outrank = str(spec.get("outrank") or "").strip()
    if _outrank:
        # Immediately BEFORE the incumbent, not front: front would also outrank
        # every unrelated specific adapter, which is a much bigger claim than the
        # caller made.
        register_adapter(ad, before=_outrank)
    else:
        register_adapter(ad)                     # default slot: just above tail
    if persist:
        specs = [s for s in _load_store()
                 if str(s.get("name", "")) != str(spec.get("name", ""))]
        specs.append(spec)
        _save_store(specs)
    return ad


def add_adapter(spec: dict, *, persist: bool = True) -> dict:
    """Validate, then (on success) register + persist a user adapter. Returns a
    JSON-able result: {ok, adapter_name, persisted, errors, self_route,
    collisions, registered}. This is the single entry point the /adapter/add
    route calls, so the route stays a thin wrapper."""
    result = validate_spec(spec)
    result["persisted"] = False
    result["registered"] = False
    if not result["ok"]:
        return result
    try:
        register_from_spec(spec, persist=persist)
        result["registered"] = True
        result["persisted"] = bool(persist)
    except UserAdapterStoreUnreadable as e:
        # The adapter IS live for this session — registration happens before the
        # store is touched. Only the persist was refused, and saying "registration
        # failed" would be a lie in both directions. Report it exactly.
        result["ok"] = False
        result["registered"] = True
        result["persisted"] = False
        result["errors"].append(str(e))
        result["session_only"] = True
        result["fix"] = ("repair or move "
                         "python_backend/connectors/adapters/user_adapters.json, "
                         "then add the adapter again to persist it")
    except Exception as e:                       # pragma: no cover - defensive
        result["ok"] = False
        result["errors"].append(f"registration failed: {e}")
    return result


def load_all() -> int:
    """Register every persisted user adapter. Called at import (package __init__
    loads this module LAST). Returns how many loaded. Never raises.

    SHADOW PROTECTION: a persisted spec whose name collides with a BUILT-IN
    adapter is registered under a suffixed name (`<name>_user`) instead of
    replacing the built-in — a legacy or hand-edited store can therefore never
    silently disable a built-in format. The rename is announced on stderr; the
    file on disk is left untouched (it is the user's data)."""
    from connectors import log_adapters as _la
    try:
        builtins = _la.builtin_names()
    except Exception:                            # pragma: no cover - defensive
        builtins = frozenset()

    n = 0
    for spec in _load_store():
        try:
            name = str(spec.get("name", "") or "")
            if name and name in builtins:
                safe = f"{name}_user"
                i = 2
                while safe in builtins:          # pragma: no cover - unlikely
                    safe, i = f"{name}_user{i}", i + 1
                print(f"[user_adapters] {name!r} collides with a BUILT-IN adapter — "
                      f"registering it as {safe!r} instead so the built-in keeps "
                      f"working. Rename it in user_adapters.json to silence this.",
                      file=sys.stderr)
                spec = {**spec, "name": safe}
            register_adapter(build_adapter(spec))
            n += 1
        except Exception as e:                   # pragma: no cover - defensive
            print(f"[user_adapters] spec {spec.get('name')!r} failed to load: {e}",
                  file=sys.stderr)
    return n


# Wire persisted adapters into the REGISTRY on import.
load_all()
