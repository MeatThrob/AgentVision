# Adapters Guide — how AgentVision parses any log, and how you add a format

**Audience: an AI agent using the AgentVision MCP tools.** You do not need to
have seen this project before. Every claim below was checked against the code in
`python_backend/connectors/`, and the examples were run on this machine.

This file replaces nothing. `docs/LOG_ADAPTERS.md` is the *registry* document —
the table of shipped formats and the Python subclassing contract. **This file is
the operating manual**: what an adapter does to your data, how to tell when the
wrong one is being used, and the exact tool calls to fix it.

---

## 0. The 60-second version

1. An **adapter** turns one raw log line into one **normalized event** (`ts`,
   `level`, `category`, `source`, `data.message`).
2. AgentVision picks an adapter by **scoring** every registered adapter against a
   sample of the log. Highest score wins. Nothing is ever dropped — `raw` is the
   floor.
3. **A wrong adapter is worse than no adapter.** It reports high confidence, it
   is not flagged as a fallback, and it silently puts wrong values in `source`
   and `message`.
4. **Never trust an adapter name reported by configuration. Verify
   `data.adapter` on a real event.** That field is written by the adapter that
   actually parsed the line. It is the only witness that cannot lie.

```
av_log_normalized(limit=20)      # look at data.adapter, level, source
av_log_sources()                 # what is configured vs what was detected
av_test_adapter(line="<paste one real line>")   # who claims this line, and why
av_list_adapters(q="<guess>")    # is there already a named adapter for it
```

---

## 1. What an adapter is

An adapter is a small parser with two methods:

| Method | Signature | Job |
|---|---|---|
| `detect` | `detect(sample_lines) -> float` in `[0.0, 1.0]` | "How confident am I that this log is my format?" |
| `parse_line` | `parse_line(line) -> event dict \| None` | Turn one line into a normalized event. `None` = I cannot parse this line. |

Registry today: **658 adapters** and **9 source readers**
(`docker_json`, `faillock`, `lastlog`, `mrt`, `netflow_v5`, `pcap`, `unified2`,
`utmp`, `wtmpdb`). Source readers decode non-line sources (binary, streamed) into
records that then go through the same adapter pipeline.

**Adapters parse logs that already exist. Emitters create logs that do not.** If
a program writes nothing, no adapter can help you — you need an emitter, chosen
in the bridge plan. That distinction is stated verbatim in the bridge catalog
response.

### 1.1 The normalized event

Every adapter produces exactly this shape. Downstream tools
(`av_diagnose`, `av_incidents`, `av_error_moment`, fingerprints, frame↔log
alignment) read only this shape, which is why one format's quirks never leak
into them.

| Field | Type | Meaning | If missing/wrong |
|---|---|---|---|
| `ts` | ISO-8601 string, `""` if unknown | Human-readable UTC timestamp | `""` |
| `ts_ms` | float epoch ms, or `null` | **The time axis.** Frame↔log alignment uses only this. | `null` → the event sorts to the END of the merged timeline |
| `level` | `TRACE\|DEBUG\|INFO\|WARN\|ERROR\|FATAL`, or `""` | Canonical severity | `""` → treated as no severity |
| `category` | `debug\|log\|warn\|error\|event\|…` | **Derived from `level`.** Failure detection keys on `category == "error"` | wrong `category` → failures become invisible |
| `source` | string | Who emitted the line (logger, subsystem, file) | falls back to the adapter's own name |
| `trace_id` | string or `null` | Correlation id, auto-scraped from the line | `null` |
| `frame_seq` | `null` from the adapter | Filled in later by the bridge | — |
| `data.message` | string | The human message | if the regex captured no message, the whole matched line is used |
| `data.adapter` | string | **Which adapter parsed this line.** Your ground truth. | — |
| `raw` | string | The original line, always kept | — |
| `log_label`, `log_path` | string | Added by the merge layer: which source this came from | — |

Two extra fields appear only when content-based escalation fires:
`level_escalated_from` and `escalation_reason`. A weakly-levelled line
(`""`/`INFO`/`DEBUG`/`TRACE`) whose text reads like a failure (`ok=False`,
`errors=3`, `status=500`, `timed out`, …) is raised to **WARN** and never
further, and the inference is recorded rather than hidden. `errors=0` and
`ok=True` are deliberately *not* escalated.

A real verified event (AbyssEngine's own log line, parsed by its own adapter):

```json
{
  "ts": "",
  "ts_ms": null,
  "category": "debug",
  "level": "DEBUG",
  "source": "Sprite.c",
  "trace_id": null,
  "frame_seq": null,
  "data": {
    "message": "Invalid path extension for 'hero.png'.",
    "adapter": "abyssengine"
  },
  "raw": "[DEBUG] Sprite.c:32 - Invalid path extension for 'hero.png'."
}
```

### 1.2 What does NOT survive parsing

Read this before you design a regex.

- **Extra named groups are discarded.** Only `ts`, `level`, `source`, `msg` are
  used (with `timestamp`→`ts` and `message`→`msg` aliased for you). The
  `abyssengine` adapter captures `(?P<lineno>\d+)`; verified — `lineno` appears
  nowhere in the event above. Capturing a field does not publish it. If you need
  the line number, keep it inside `message`.
- **No `ts` group means no time axis.** `ts_ms` stays `null`. The event still
  appears (untimestamped events survive a `from_ms`/`to_ms` window filter), but
  it sorts to the end of the merged list. Since `av_log_normalized` truncates to
  the most recent `limit` events *after* sorting, a large untimestamped source
  can crowd timestamped events out of the page.
- **A pinned adapter is not a promise.** If it returns `None` for a line, that
  single line is re-parsed by `raw`. A partly-correct regex therefore produces a
  mix of `data.adapter` values across events — which is exactly how you detect it.

---

## 2. How detection works

### 2.1 Scoring

`detect(sample_lines)` for the regex-driven adapters is a **ratio**: of the
non-blank lines in the sample, what fraction matched? 8 of 10 lines matching →
`0.8`. A one-line sample therefore scores only `0.0` or `1.0`, which is why
`av_test_adapter` is a coarse probe and a real file sample is better evidence.

`detect_adapter()` then scores **every** adapter in the registry and keeps the
highest. A per-adapter exception is caught and scored `0.0`, so one broken
adapter cannot break detection.

**Ties go to whichever adapter is registered earlier.** The comparison is
strictly greater-than, so the first adapter to reach the maximum keeps it. This
single line of behaviour is the cause of the whole problem in section 3.

### 2.2 The score ceilings

| Tier | Adapters | Max score | Meaning |
|---|---|---|---|
| Named | the 650-odd format adapters | `1.0` | "this is specifically my format" |
| Named, vocabulary-based | the subset that matches keywords rather than a line grammar | `0.85` (self-capped) | so a strict-grammar adapter always beats a keyword match on the same line |
| Generic timestamped | `generic_ts` | `0.6` (capped) | "there is a timestamp here somewhere" |
| Structural | `structural` | `0.3` (capped, floor `0.02`) | "I recognise a common log *shape*" |
| Floor | `raw` | `0.01` (constant) | "it is text" |

`FALLBACK_ADAPTERS = {"structural", "generic_ts", "raw"}`. When
`av_test_adapter` returns `is_fallback: true`, no adapter specifically
understands the format. That is the signal to call `av_add_adapter`.

**Nothing is ever dropped.** There are two independent safety nets: `raw` always
scores above zero so adapter *selection* never fails, and any line a chosen
adapter returns `None` for is re-parsed by `raw`. Blank lines are skipped.

### 2.3 Where detection actually runs

| Trigger | Sample used | Notes |
|---|---|---|
| A source with `adapter: "auto"` | first 60 lines of the read (which is the last 1 MiB tail of the file) | re-detected on every read |
| `av_log_sources` | first 60 non-empty lines of the file | reports detection separately from what will be used |
| `av_test_adapter(line=…)` | the one line you pass | fast, but a 1-line sample is a weak sample |
| `av_preflight` | first 40 head lines per source | a source pinned to an explicit adapter is trusted, not re-detected |

A source pinned to an explicit adapter name is **never** re-detected. That is
the point of pinning.

---

## 3. Why a wrong adapter is worse than no adapter

### 3.1 The case: `coreboot_cbmem` steals AbyssEngine's log

AbyssEngine is a C/SDL2 game engine. Its logger prints:

```
[DEBUG] Sprite.c:32 - Invalid path extension for 'hero.png'.
[ERROR] Render.c:98 - GL context lost
```

`coreboot_cbmem` is a firmware-log adapter. Its regex accepts a bracketed level
tag followed by text — and `ERROR` and `DEBUG` happen to fit its tag list. So it
matches these lines at **1.0**, the maximum score. So does the custom
`abyssengine` adapter. It is a tie, and `coreboot_cbmem` is registered earlier
(index 133 of 655 vs 652), so `coreboot_cbmem` wins.

Verified live against the running bridge, `av_test_adapter` on the first line:

```json
{
  "adapter": "coreboot_cbmem",
  "confidence": 1.0,
  "is_fallback": false,
  "top_scores": [
    {"adapter": "coreboot_cbmem", "score": 1.0},
    {"adapter": "sharpemu_channel", "score": 1.0},
    {"adapter": "abyssengine", "score": 1.0},
    {"adapter": "structural", "score": 0.3},
    {"adapter": "raw", "score": 0.01}
  ]
}
```

### 3.2 The three outcomes, side by side

Same two lines, read three ways. This was run, not imagined.

| Source config | `data.adapter` | `level` | `source` | `data.message` |
|---|---|---|---|---|
| `adapter: "auto"` (detection wins) | `coreboot_cbmem` | `DEBUG` / `ERROR` | **`coreboot`** | **`Sprite.c:32 - Invalid path extension…`** |
| `adapter: "abyssengine"` (pinned, correct) | `abyssengine` | `DEBUG` / `ERROR` | `Sprite.c` / `Render.c` | `Invalid path extension…` |
| `adapter: "abyssengine_TYPO"` (name does not exist) | **`raw`** | **`""`** (both lines!) | `raw` | the entire original line |

### 3.3 The symptoms you will actually observe

You will not see an error message. Look for these instead:

| What you see | What it means |
|---|---|
| `source` is a word from an unrelated ecosystem (`coreboot`, `kernel`, a vendor name) for a program that has nothing to do with it | wrong adapter claimed the format |
| `file.c:32 - ` still sitting at the front of `data.message` | the winning adapter did not understand the format's own field layout |
| `data.adapter` names a technology your program does not use | wrong adapter, confirmed |
| `level` is `""` on lines that visibly say `[ERROR]` | you are on `raw` — usually a typo'd pin |
| `data.adapter` varies line to line within one source | the pinned adapter matches only some lines; the rest fell to `raw` |
| `av_diagnose` reports healthy while the log is full of failures | `category` is not `"error"`, so failure detection never fired |

### 3.4 Why this is worse than having no adapter at all

- `confidence` is `1.0` and `is_fallback` is `false`. Every automatic check says
  "covered".
- `av_preflight` counts the source as **covered**, not a gap. It only flags
  sources that land on `structural`/`generic_ts`/`raw`.
- Nothing logs a warning. There is no "ambiguous match" report anywhere.
- `raw` would at least have left the complete line in `data.message`. A wrong
  named adapter *rewrites* your fields: it deletes `Sprite.c:32` from where you
  can search for it structurally, and it asserts a `source` that is false.

---

## 4. Diagnosing an adapter problem — the exact sequence

Run these in order. Stop when you know the answer.

### Step 1 — Look at real events first

```
av_log_normalized(limit=20)
```

Check three things on the returned events: `data.adapter`, `level`, `source`.
This is the only step that shows you what actually happened. If `data.adapter`
is right and the fields look sane, there is no adapter problem — stop here.

If it returns `count: 0`, this is not an adapter problem. Go to step 4.

### Step 2 — Compare configured vs detected

```
av_log_sources()
```

Per source you get:

| Field | Meaning |
|---|---|
| `exists` | does the file exist |
| `configured_adapter` | what the profile asked for (`"auto"` or a name) |
| `detected_adapter` | what detection would pick, from the first 60 lines |
| `detect_confidence` | that pick's score |
| `adapter` | **what the merge will use**: `reader:<name>` > explicit non-`auto` name > detected > `null` |

Two traps in this response:

- `adapter` echoes the **configured name without checking that it exists**. A
  typo shows up here as if it were in use, while parsing silently falls to `raw`.
  Step 1's `data.adapter` is what disproves it.
- The response also carries `available_adapters` — all 655 entries, about
  29 KB (~7k tokens). Call it when you need it; do not call it in a loop.

### Step 3 — Ask who claims one line, and who else wanted it

```
av_test_adapter(line="[DEBUG] Sprite.c:32 - Invalid path extension for 'hero.png'.")
```

Read `adapter`, `confidence`, `is_fallback`, `top_scores` (top 8), and `event`.

| Result | Diagnosis | Fix |
|---|---|---|
| `is_fallback: true` | no adapter understands this format | `av_add_adapter` (section 5) |
| `adapter` is a named adapter but `event.source` / `event.data.message` are wrong | a wrong adapter claimed it | `av_add_adapter` with `outrank=<that adapter>` (section 6), **and** pin it (section 7) |
| several `top_scores` are tied at `1.0` | a tie, decided by registration order — fragile | pin your adapter explicitly (section 7) |
| `adapter` is right | not an adapter problem | look at the log content itself |

### Step 4 — Is a named adapter already there?

```
av_list_adapters(q="coreboot", limit=10)
av_list_adapters(family="java", limit=50)
```

`q` is a substring match on the adapter **name**. `family` is an **exact** match
against either the family bucket or the adapter's `language` — `family="c"`,
`family="kernel"`, `family="java"` work; a near-miss silently returns zero
matches. Note the two taxonomies do not line up: the `family` field returned per
adapter is its Python module (`kernel`, `batch9`, `log_adapters`), while the
bridge catalog's `adapters.families` histogram is keyed by **language**
(`any: 359`, `java: 54`, `python: 37`). Both are searchable by `family=`.

### Step 5 — Only if the log looks too quiet or stale

```
av_log_where()
```

This asks the OS which files the process actually holds open for writing, and
reconciles them against the profile. It catches reading a file nothing writes to
any more. Caveats: POSIX only (it shells out to `lsof`), and it is not perfectly
side-effect-free — the first call in a session lazily constructs the bridge's
collector for the active profile.

---

## 5. Adding an adapter with `av_add_adapter`

`av_add_adapter` builds a regex-driven adapter, validates it, registers it into
the live registry, and persists the spec to
`python_backend/connectors/adapters/user_adapters.json` so it survives a
restart. It is **not** blocked by the bridge gate — you can add adapters before
committing a plan, which is the right order (add first, then pin the name in the
plan).

It is idempotent by name: re-adding the same name replaces your previous version.

### 5.1 Every parameter

| Parameter | Required | What it does |
|---|---|---|
| `name` | **yes** | Short stable id. Letters, digits, `_`, `.`, `-` only. Must not be a built-in adapter's name (rejected — it would silently shadow the built-in) and must not be `structural` or `raw` (reserved). |
| `extract_regex` | **yes** | Regex with named groups, applied to ONE line. Recognised groups: `ts`, `level`, `source`, `msg`. `timestamp`→`ts` and `message`→`msg` are aliased for you. All other named groups are captured and then **discarded**. |
| `sample` | **yes** | A real line of this format. The adapter must win this line or the add is rejected. |
| `detect_regex` | no | Separate, usually tighter pattern used only for detection. Defaults to `extract_regex`. |
| `anchor_tokens` | no | List of literal substrings that must ALL appear in the line before the detect regex is even tried. The cheapest way to stop stealing other formats. |
| `family` | no | Informational grouping. Also the default for `default_source`. |
| `language` | no | Ecosystem hint, default `"any"`. Shows up in `av_list_adapters`. |
| `level_map` | no | Raw token → canonical level, e.g. `{"3": "ERROR"}`. Only needed for tokens that are not ordinary level words (see 5.4). |
| `default_level` | no | Level used when the `level` group is absent or empty. |
| `default_source` | no | `source` used when the `source` group is absent. Falls back to `family`, then to the adapter's own name. |
| `category` | no | **Forces** `category` to a fixed value. Dangerous — see 5.4. |
| `match_scope` | no | `"lines"` (default) or `"first"` for multi-line records (only the first physical line must match). |
| `outrank` | no | Name of an existing adapter that claims this format but parses it wrongly. Breaks a tie in your favour. See section 6. |

### 5.2 The five validation gates

The add is rejected (`ok: false`, plus `errors`) unless all of these pass. A
rejection comes back as HTTP 422, surfaced to you as an object containing
`"status": 422`, `"ok": false` and the `errors` list — read `errors`, not the
status.

1. **Structure.** `name` present and legal, `extract.regex` compiles, `sample`
   present.
2. **Name is not a built-in.** Reusing a built-in name would replace it. The
   error suggests `<name>_<family>` or `<name>_user`.
3. **Self-route.** Your adapter must match its own `sample`.
4. **Strictly beats every incumbent on that sample.** Ties lose, because new
   adapters register *after* the built-ins. `outrank=<name>` is the one exception
   and only for an exact tie. Response field `self_route.would_lose_to` names
   who beat you and by how much.
5. **No theft.** Your detect signature is run against every sample line in
   `docs/log_catalog_master.json`. If you would outscore a *named* adapter on
   someone else's sample, you are rejected and shown the offending lines (up to
   8) in `collisions`. Fix by adding `anchor_tokens` or tightening the regex.

### 5.3 Complete worked example

The gap: `[DEBUG] Sprite.c:32 - Invalid path extension for 'hero.png'.` is being
claimed by `coreboot_cbmem`, which sets `source=coreboot` and leaves `Sprite.c:32`
buried in the message.

The format, read off the line: `[` LEVEL `] ` FILE `:` LINE ` - ` MESSAGE.

The distinguishing feature versus firmware logs is the `<file>.c:<line> - `
middle section. Put that in the detect regex so it cannot match anything else.

```
av_add_adapter(
    name="abyssengine",
    family="gamedev",
    language="c",
    extract_regex="^\\[(?P<level>[A-Z]+)\\]\\s+(?P<source>[A-Za-z0-9_.\\-]+\\.(?:c|h|cpp|cc|hpp)):(?P<lineno>\\d+)\\s+-\\s+(?P<message>.*)$",
    detect_regex="^\\[[A-Z]+\\]\\s+[A-Za-z0-9_.\\-]+\\.(?:c|h|cpp|cc|hpp):\\d+\\s+-\\s+",
    sample="[DEBUG] Sprite.c:32 - Invalid path extension for 'hero.png'.",
    outrank="coreboot_cbmem"
)
```

Notes on that call, all load-bearing:

- Backslashes are doubled because the value travels as a JSON string. `\\d` in
  the call becomes `\d` in the regex. Getting this wrong is the single most
  common failure.
- `^` and `$` anchor the extract regex to the whole line.
- `(?P<lineno>…)` is captured to keep the regex honest about the format, but its
  value is thrown away. The line number does not reach the event.
- The detect regex omits the message tail entirely. Detection only needs the
  distinguishing prefix; a shorter detect pattern is a stricter one here.
- `outrank` is required in this specific case because the tie is at `1.0`.

On success you get:

```json
{
  "ok": true,
  "adapter_name": "abyssengine",
  "registered": true,
  "persisted": true,
  "errors": [],
  "self_route": {
    "sample": "[DEBUG] Sprite.c:32 - Invalid path extension for 'hero.png'.",
    "own_score": 1.0,
    "would_lose_to": {"adapter": "coreboot_cbmem", "score": 1.0},
    "outranks": "coreboot_cbmem"
  },
  "collisions": []
}
```

Then verify, and do not skip this:

```
av_test_adapter(line="[DEBUG] Sprite.c:32 - Invalid path extension for 'hero.png'.")
av_preflight()
```

Verified result of that adapter on the two-line log: `level=DEBUG/ERROR`,
`source=Sprite.c/Render.c`, `message` free of the `file:line` prefix.

### 5.4 Four traps that produce a quietly wrong adapter

All four were reproduced on this machine.

**Trap 1 — `category` silences your errors.** `category` *overrides* the
level-derived value. With `category="event"`, a line whose level is `ERROR`
comes out as:

```json
{"level": "ERROR", "category": "event", "data": {"message": "boom"}}
```

Failure detection keys on `category == "error"`, so this failure is invisible to
`av_diagnose`, incidents and push mode. **Do not set `category` unless the format
genuinely has no severity dimension** (an access log, a metrics line).

**Trap 2 — numeric levels need `level_map`.** Ordinary level *words* are already
canonicalised for you, in any case: `WARNING`→`WARN`, `err`→`ERROR`,
`severe`→`ERROR`, `warn`→`WARN`. So `level_map={"WARNING": "WARN"}` is
redundant. But a numeric severity is not a word:

| Spec | Result |
|---|---|
| no `level_map` | `level: "3"`, `category: "log"` — an error that reads as normal |
| `level_map={"3": "ERROR"}` | `level: "ERROR"`, `category: "error"` — correct |

Use `level_map` for numeric severities and for odd tokens (`*E`, `<3>`, `E/`).

**Trap 3 — `default_level` masks lines your regex missed.** If the `level` group
fails to capture on some lines, those lines silently take `default_level`. Set
`default_level="INFO"` on a format that has real errors and you have hidden them.
Prefer leaving it empty and letting `level` be `""`; content escalation can still
raise a genuine failure to `WARN`.

**Trap 4 — no `source` group and no `default_source`.** `source` then becomes the
adapter's own name, which looks like a real subsystem but is not. Set
`default_source` (or `family`) deliberately.

---

## 6. `outrank=` — and the restart caveat you must know

### 6.1 What it does

New adapters are appended near the end of the registry, so on a tie the older
adapter wins. `outrank="<name>"` inserts yours **immediately before** that named
adapter instead, which flips the tie to you.

Use it when — and only when — **all** of these hold:

- `av_test_adapter` shows a named adapter winning your format, and
- that adapter's parse is wrong (`source` is nonsense, fields buried), and
- your score **equals** theirs.

It cannot rescue a weaker pattern. If the incumbent scores higher you get:
`"outrank='X' but 'X' still scores higher (1.0 > 0.8) — placement only breaks a
TIE"`. Fix the pattern instead.

It is deliberately *not* "register first": inserting before one named incumbent
is a smaller claim than outranking every specific adapter in the registry.

### 6.2 The caveat: outrank placement does not survive a restart

**Verified on this machine, twice.** The persisted spec keeps its
`"outrank": "coreboot_cbmem"`, but the loader that replays persisted specs at
import registers them in the default slot without re-applying the placement. In
a fresh process, and on the currently running bridge server, the AbyssEngine line
resolves to `coreboot_cbmem` again — `source: "coreboot"`, `file:line` back
inside the message.

Consequence, and it is the most important sentence in this file:

> **`outrank` fixes the current session. Pinning the adapter on the log source
> fixes it permanently. Always do both.**

Pin it as described in section 7. A pinned source is never re-detected, so the
tie never gets a chance to be resolved wrongly again.

---

## 7. Pinning an adapter to a source

Two mechanisms. They are not interchangeable.

### 7.1 In the bridge plan — `plan.adapters`

`plan.adapters` maps a **source label** to an adapter name (or `"auto"`). It is
recorded in the sealed plan at
`<project_root>/agentvision/<profile_name>/.av_bridge_plan.json`.

Only these keys are read at commit time:

| Key | Source it pins | Default if you omit it |
|---|---|---|
| `"events"` | `agentvision/actions.jsonl` | `jsonl` |
| `"text"` | `agentvision/log.txt` | `auto` |
| `"stdout"` | fallback key used for either of the above | — |

Any other key is stored in the plan and **never applied**. So is every key, if
any of these are true:

- `plan.emitters` is empty (no install runs, so no sources are wired), or
- the project root is not a directory, or
- that file does not exist yet, or
- **that path is already listed in the profile's `log_sources`** — commit skips
  sources it did not add.

The last one is easy to hit. Verified on the AbyssEngine plan: it recorded
`"adapters": {"events": "jsonl", "text": "abyssengine"}`, but the commit's
`built.actions` contains no `registered log sources:` entry, so the pin came from
the profile, not from the plan. **Treat `plan.adapters` as the declaration of
intent, and the profile as the thing that takes effect.** Verify with
`av_log_sources()` afterwards.

A complete, valid plan that pins a custom adapter:

```
av_bridge_commit(plan={
    "catalog_token": "8834252a07355b23",
    "emitters": ["run_wrapper", "lifecycle"],
    "why": {
        "run_wrapper": "prints_only signal: src/common/Logging.c printf()s to stdout and writes no file, and a native binary has no in-process hook to install",
        "lifecycle": "an SDL game loop that segfaults otherwise shows up only as a non-zero exit code, which nothing records"
    },
    "adapters": {"events": "jsonl", "text": "abyssengine"},
    "capture": {"interval_seconds": 1.0},
    "visual_capture": true,
    "rationale": "Compiled C/SDL2 engine: capture must happen at the process boundary, and it renders a real window so visual capture earns its place.",
    "tools": {
        "primary": ["av_diagnose", "av_log_normalized", "av_error_moment", "av_visual_changes"],
        "not_relevant": {
            "av_run_tests": "hardcodes python -m pytest; this project builds with make",
            "av_ui_tree": "SDL draws its own widgets, so the accessibility API sees one opaque window"
        },
        "note": "picked for a compiled GUI game with a stdout-only log"
    }
})
```

Replace `catalog_token` with the value from your own `av_bridge_catalog()` call —
a stale token is rejected. The gate fires once per program ever; on an
already-sealed program this returns `{"already_sealed": true}` and changes
nothing unless you pass `replan=true`.

### 7.2 On the profile — `log_sources` (the durable pin)

This is what the reader actually obeys:

```json
{"path": "/abs/path/agentvision/log.txt", "adapter": "abyssengine", "label": "text"}
```

`adapter: "auto"` means detect on every read. Any other value is used verbatim.

**Two warnings before you write a profile:**

1. **A profile POST replaces the whole profile — it does not merge.** Any field
   you omit resets to its dataclass default, so a partial update wipes
   `project_root`, `capture_app`, `process_name`, `language` and the rest. Read
   the current profile first (`av_active_profile()` or `av_list_profiles()`) and
   send **every** field back with only `log_sources` changed.
2. **A misspelled adapter name fails silently.** The reader resolves an unknown
   name to `raw`, while `av_log_sources` keeps reporting the name you typed.
   Verified: both lines of the sample log came back with `level: ""` and
   `data.adapter: "raw"`. Always confirm with `av_log_normalized` afterwards.

Also note that profiles and the active profile are **global bridge state**. Other
sessions share them.

```
av_create_profile(name="abyss", profile={
    "display_name": "AbyssEngine",
    "project_root": "~/projects/AbyssEngine",
    "process_name": "abyss",
    "python_exe": "python3",
    "capture_app": "AbyssEngine",
    "capture_crop": "",
    "log_file": "",
    "action_log_file": "",
    "stats_folder": "",
    "screenshots_folder": "",
    "config_folder": "",
    "state_file": "",
    "test_dir": "",
    "notes": "",
    "language": "c",
    "capture_user_input": false,
    "log_sources": [
        {"path": "~/projects/AbyssEngine/agentvision/actions.jsonl",
         "adapter": "jsonl", "label": "events"},
        {"path": "~/projects/AbyssEngine/agentvision/log.txt",
         "adapter": "abyssengine", "label": "text"}
    ]
})
```

Beyond `log_sources`, two legacy fields are folded in automatically as sources:
`action_log_file` (always adapter `jsonl`, label `actions`) and `log_file`
(always `auto`, label `log`). Duplicate paths are de-duplicated by absolute path,
with `log_sources` taking precedence. You cannot pin an adapter on the legacy
fields — declare the path in `log_sources` instead.

---

## 8. Regex pitfalls specific to log formats

| Pitfall | Why it bites here | Do this |
|---|---|---|
| **Not anchoring** | Detection tries `match` **and then `search`**. An unanchored pattern therefore matches mid-line. Verified: `ZZQ (?P<level>\w+) (?P<message>.*)` scores **1.0** on `garbage prefix ZZQ INFO hi`. That is how you steal other formats and get rejected. | Start `detect_regex` with `^`. Anchor `extract_regex` with `^…$`. |
| **Greedy groups eating the next field** | `(?P<source>.*):(?P<lineno>\d+)` on `a.c:12: msg` grabs too much because `.*` runs to the last colon. | Use a character class that cannot cross the separator: `[A-Za-z0-9_.\-]+`. Keep `.*` for the message only, and put it last. |
| **Optional fields written as optional groups** | `(?:\[(?P<level>\w+)\])?` makes every line match, including other formats' lines. Your score hits 1.0 on things you do not own, and the collision check rejects you. | Prefer two adapters, or make the *mandatory* part of the format the detect signature and let `extract_regex` be the permissive one. |
| **Level case and spelling** | Levels are canonicalised case-insensitively: `warning`/`WARN`/`Wrn`→`WARN`, `err`/`severe`→`ERROR`. A `level_map` for those is dead weight. | Match `[A-Za-z]+` for the level word and let canonicalisation do its job. Use `level_map` only for numbers and symbols. |
| **Width-padded level tags** | Some formats pad to a fixed width (`"INFO "`, `"WARN "`). Padding inside the brackets is a real discriminator between formats. | If your format pads, match the pad. If it does not, `\s+` after `]` is enough. |
| **Under-escaped JSON** | `\d` in a JSON string is not a valid escape; you must send `\\d`. A silently mangled pattern that still compiles is the worst case. | Write `\\d`, `\\s`, `\\[`, `\\.`. Then confirm with `av_test_adapter` before trusting it. |
| **`-` inside a character class** | `[A-Za-z0-9_.-]` works only because `-` is last. Elsewhere it means a range and can raise or silently widen. | Put `-` last, or escape it: `[A-Za-z0-9_.\\-]`. |
| **Multi-line records** | Java stack traces and panics span lines. With `match_scope: "lines"` a block scores by the fraction of its lines that match, so a 1-line header plus 20 frames scores badly. | Use `match_scope="first"`: only the first line of a record must match. |
| **Anchoring on the timestamp alone** | Every timestamped format matches, and you land in a fight you cannot win — `generic_ts` already covers that ground at `0.6`. | Anchor on something only your program writes: a fixed prefix, a subsystem tag, a `file.c:NN` shape. |
| **Assuming the whole file is one format** | Detection samples the first 40–60 lines. A banner or a config dump before the real log skews it. | Pass a representative middle line to `av_test_adapter`, and pin the adapter rather than relying on `auto`. |
| **Forgetting the message group** | If `msg` captures nothing, the *entire matched line* becomes the message — including the level and timestamp you meant to strip. | Always include `(?P<message>.*)$` (or `msg`). |

---

## 9. Quick reference

| I need to… | Call |
|---|---|
| See which adapter actually parsed real events | `av_log_normalized(limit=20)` → `data.adapter` |
| See configured vs detected per source | `av_log_sources()` |
| Classify one line and see the runners-up | `av_test_adapter(line="…")` |
| Search the registry | `av_list_adapters(q="…")` / `av_list_adapters(family="…")` |
| Check every source is specifically covered | `av_preflight()` |
| Add a format | `av_add_adapter(name=…, extract_regex=…, sample=…)` |
| Beat a wrong incumbent on a tie | same call, plus `outrank="<incumbent>"` |
| Make the fix survive restart | pin `{"path":…,"adapter":"<name>","label":…}` in the profile's `log_sources` |
| Find out whether the program even writes there | `av_log_where()` |

**Related files**

- `docs/LOG_ADAPTERS.md` — the shipped-format registry table and the Python
  subclassing contract for adapters written in code rather than added at runtime.
- `docs/SCHEMA.md` — the event schema in the wider bridge context.
- `docs/MCP_TOOLS_REFERENCE.md` — every tool. **Generated** from
  `python_backend/api/tool_meta.json` by `scripts/gen_tools_ref.py`; never
  hand-edit it.
- `docs/MCP_TOOL_AUDIT.md` — 77 known tool defects, 12 of them Class-A. Check it
  before trusting a tool's docstring.

**Source of truth in code**

| Concern | File |
|---|---|
| Adapter base class, registry, detection, level/timestamp canonicalisation | `python_backend/connectors/log_adapters.py` |
| Source resolution, per-source read, merge onto one timeline | `python_backend/connectors/log_sources.py` |
| Runtime-added adapters: spec → adapter, validation, persistence | `python_backend/connectors/adapters/user_adapters.py` |
| Persisted user specs | `python_backend/connectors/adapters/user_adapters.json` |
| Routes `/adapter/add`, `/adapter/test`, `/adapters`, `/log/sources` | `python_backend/api/bridge_server.py` |
| Plan validation and the `adapters` field | `python_backend/bridge_plan.py` |
