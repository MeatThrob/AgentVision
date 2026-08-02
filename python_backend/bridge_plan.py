"""
The bridge is not built until the AGENT decides what to build.
================================================================================
AgentVision is a toolbox, not a judge. It owns 658 log adapters, 9 binary source
readers, 90 MCP tools and a per-language emitter/hook library — but it cannot
know which of those a given program needs, because that depends on what the code
IS and what it DOES, and nothing in a directory listing tells you that.

So the first connection to a program does NOT produce a working bridge. It
produces a CATALOG for the agent to read, and the bridge stays provisional until
the agent commits a plan naming what should be built. AgentVision then builds
exactly that and nothing else.

WHY THIS ISN'T THE OLD BEHAVIOUR. `av_install_project` used to sniff the language
and scaffold a fixed emitter set — the same hooks for every Python program,
whether it was a web server or a GPU emulator. That is AgentVision guessing, and
it guessed the same way every time. Worse, it reported success either way, so a
wrong-but-installed bridge was indistinguishable from a right one.

ENFORCEMENT, not trust. `catalog()` returns a `catalog_token` derived from the
catalog's own contents. `validate_plan()` REJECTS a plan that does not carry a
matching token. An agent therefore cannot commit a plan it never fetched the
options for — the requirement "review the available logs and tools first" is a
mechanical precondition rather than a docstring nobody reads.

FIRST CONNECTION ONLY. Once a plan is committed the bridge is SEALED and the
selected logging is built into the target program. Every later connection finds
it already built and proceeds immediately — the gate never fires twice for the
same program. Re-planning is possible but explicit (`replan=True`).
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

PLAN_FILENAME = ".av_bridge_plan.json"
#: Legacy marker from when the gate only checked log-format coverage. A profile
#: that passed the old gate is treated as sealed so upgrading AgentVision cannot
#: brick a bridge that is already working — the new gate is for NEW programs.
LEGACY_MARKER = ".av_preflight_ok"

PLAN_VERSION = 1


# ── Catalog ───────────────────────────────────────────────────────────────────

#: Extension labels that describe data, not a runtime. Excluded from the
#: primary_language vote so a config-heavy repo is still offered an emitter.
_NON_RUNTIME_LANGS = {"config", "markup", "html", "css", "data", "docs"}


def _emitter_options(language: str, full: bool = False,
                     also_present: Optional[list] = None) -> list[dict]:
    """What CAN be built into a program of this language, and what each captures.

    Presented as options with their cost/coverage, never pre-selected: the choice
    between "just tee stdout" and "full in-process hooks" depends on whether the
    program already logs well, which only the agent can judge from the code.

    `also_present` carries EVERY runtime language found in the project, and the
    per-language options are unioned over all of them. Keying only on the scalar
    primary language was a real defect, measured: a Django+React repo (3 .py,
    2 .tsx) elects primary_language=python, so browser_events — the only emitter
    that can see a client-side failure — was never offered, no matter how much
    browser evidence the scan turned up. A polyglot program does not stop being
    polyglot because one language has more files, and this menu must not force a
    plurality vote to decide what the agent is allowed to ask for.
    """
    lang = (language or "").lower()
    langs = {lang} | {str(x).lower() for x in (also_present or []) if str(x).strip()}
    # `misses` is as important as `captures`: an emitter chosen for what it does
    # not cover is how a bridge ends up blind to its program's actual failure
    # mode. `builds_as` names what the installer ACTUALLY produces, because the
    # installer emits one language-appropriate emitter rather than one artifact
    # per id — several ids below are facets of the same emitter, and a plan that
    # believed otherwise would be recording a build that never happened.
    common = [
        {"id": "stdout_tee", "captures": "stdout + stderr, line by line",
         "cost": "none — the program is unchanged",
         "good_for": "any program that already prints something useful",
         "misses": ("anything the program never prints — a silent early exit, a "
                    "caught-and-discarded exception, a hang with no output"),
         "languages": "interpreted languages loaded in-process",
         "builds_as": "the language emitter's tee path",
         "note": ("for a COMPILED program use run_wrapper instead: there is no "
                  "in-process hook to install in a native binary")},
        {"id": "lifecycle", "captures": "process start/exit, argv, pid, exit code",
         "cost": "negligible",
         "good_for": "knowing whether a run happened at all, and how it ended",
         "misses": "everything between start and exit",
         "languages": "all",
         "builds_as": ("a facet of the language emitter, not a separate file — "
                       "for compiled programs the run_wrapper reports the exit "
                       "code, so selecting both is not additive")},
    ]
    inproc = [
        {"id": "uncaught_exceptions",
         "captures": "uncaught exceptions + thread exceptions + shutdown errors",
         "cost": "negligible",
         "good_for": "anything that can crash",
         "misses": ("every exception the program catches — which on code with "
                    "broad excepts is most of them; pair with "
                    "swallowed_exceptions"),
         "languages": "python, node, ruby",
         "builds_as": "sitecustomize / preload shim hook"},
        {"id": "logging_bridge",
         "captures": "the language's own logging framework, mapped to levels",
         "cost": "negligible",
         "good_for": "programs that already log but to nowhere useful",
         "misses": ("bare print()/printf output, and anything logged before the "
                    "bridge is installed"),
         "languages": "python (logging), node, ruby, java (logback), .NET (Serilog)",
         "builds_as": "logging handler attached at import, or a config drop-in"},
        {"id": "swallowed_exceptions",
         "captures": "exceptions the program CATCHES and hides (try/except: pass)",
         "cost": "near zero via sys.monitoring; needs Python 3.12+",
         "good_for": "code with broad excepts — the bugs that never crash",
         "misses": ("non-exception failure: a wrong value returned, a branch never "
                    "taken. Also silent on Python < 3.12, where sys.monitoring "
                    "EXCEPTION_HANDLED does not exist"),
         "languages": "python 3.12+",
         "builds_as": "sys.monitoring EXCEPTION_HANDLED hook (tool id 4)"},
    ]
    per_lang = {
        "python": inproc,
        "node":   [e for e in inproc if e["id"] != "swallowed_exceptions"],
        "ruby":   [e for e in inproc if e["id"] != "swallowed_exceptions"],
    }
    opts = list(common)
    _seen = {o["id"] for o in opts}
    for _l in sorted(langs):
        for _o in per_lang.get(_l, []):
            if _o["id"] not in _seen:
                _seen.add(_o["id"])
                opts.append(_o)
    if langs & {"java", "dotnet", "csharp"}:
        opts.append({
            "id": "config_dropin",
            "captures": "structured JSON logs via the ecosystem's own appender",
            "cost": "a config file drop-in; needs the program restarted",
            "good_for": "JVM/.NET services that already use logback/Serilog",
            "misses": ("anything written outside the logging framework, and "
                       "everything before config load"),
            "languages": "java, .NET",
            "builds_as": "a logback/Serilog config file in the project"})
    # A web front end runs in a BROWSER, and the browser is where a large share of
    # its user-visible failure happens: a null deref in a component, a rejected
    # fetch, a CSP block. None of that reaches the server log — the server returns
    # 200 and the page is broken. Before this option existed, `language:
    # "typescript"` offered only the Node emitter, whose mechanism is
    # NODE_OPTIONS=--require: a SERVER-side hook that can never load in a browser.
    # So a React/Vue/Svelte app was offered an emitter that could not run.
    if langs & {"node", "javascript", "typescript", "ts", "js"}:
        opts.append({
            "id": "browser_events",
            "captures": ("in the BROWSER: uncaught errors, unhandled promise "
                         "rejections, console.error/warn, failed fetch/XHR "
                         "(including CORS and offline, which the server never "
                         "sees), CSP violations, and resource load failures"),
            "cost": ("one <script> tag, dev builds only; the page POSTs batched "
                     "NDJSON to the bridge"),
            "good_for": ("any app whose UI runs in a browser — this is the ONLY "
                         "emitter that sees the client half"),
            "misses": ("anything before the script loads, and anything in a "
                       "different tab or a web worker"),
            "languages": "browser (any framework: React, Vue, Svelte, plain JS)",
            "builds_as": ("agentvision/emitters/av_browser.js, loaded by a "
                          "<script> tag or an import in your entry module; it "
                          "POSTs to /browser/ingest, which appends to the text "
                          "sink where the browser_av_json adapter parses it"),
            "note": ("a browser cannot append to a file, so this emitter is the "
                     "one that needs the bridge REACHABLE from the page "
                     "(127.0.0.1:7771 by default). If the page is served from "
                     "another host, set data-av-endpoint on the script tag."),
        })

    if langs & {"go", "rust", "cpp", "c", "shell", "", "unknown"}:
        opts.append({
            "id": "run_wrapper",
            "captures": ("stdout/stderr via `agentvision run -- <cmd>`, "
                         "normalized, plus the exit code"),
            "cost": "launch through the wrapper; no code or build change",
            "good_for": "compiled programs where in-process hooks are not possible",
            "misses": ("anything not written to stdout/stderr — a segfault leaves "
                       "only the exit status, and output buffered at crash time "
                       "can be lost entirely"),
            "languages": "go, rust, c/c++, shell, anything",
            "builds_as": "the tee emitter + CPP_README; ONLY takes effect if the "
                         "program is actually launched through `agentvision run`"})

    # The human's real keyboard and mouse. Language-agnostic — it watches the OS,
    # not the process — and the ONLY emitter that can show input the program never
    # handled, which is exactly the evidence a dead-click or ignored-keypress bug
    # needs. It was reachable before only through a GUI toggle, so no plan could
    # ever select it and an agent debugging "my keypress does nothing" had no way
    # to ask for the one signal that settles it. Never a default: it records the
    # human system-wide, so it must be chosen out loud and justified.
    opts.append({
        "id": "user_input",
        "captures": ("real keyboard + mouse events, system-wide, timestamped into "
                     "the active profile's action log — including input the "
                     "program NEVER received"),
        "cost": ("privacy-sensitive: records the human, not the program, and not "
                 "only in the target window. Needs an OS permission "
                 "(macOS Accessibility) and a daemon process"),
        "good_for": ("dead clicks, ignored keypresses, focus loss, 'I pressed it "
                     "and nothing happened' — the class of bug where the program's "
                     "own logs are empty BECAUSE the event never arrived"),
        "misses": ("input delivered by other means (synthetic events, remote "
                   "control), and everything while the daemon is stopped or the "
                   "profile flag is off — both fail CLOSED and are counted"),
        "languages": "any — this watches the OS input stream, not the process",
        "builds_as": ("NOTHING is written into the project and the program is not "
                      "restarted. Selecting it sets capture_user_input=true on the "
                      "profile; the daemon (`agentvision daemon start`) must also "
                      "be running. The daemon re-reads the flag every 2s"),
        "note": ("ask the human before selecting this on a machine you do not own. "
                 "If the daemon is not running you get silence, not an error — "
                 "check av_daemon_status() rather than trusting an empty log."),
    })
    return _merge_emitter_detail(opts, lang, full=full)


def _merge_emitter_detail(opts: list[dict], lang: str,
                          full: bool = False) -> list[dict]:
    """Overlay the researched per-emitter spec onto the structural option list.

    The list above decides WHICH ids a language is offered — that is a property of
    what the installer can actually build. api/emitter_meta.py supplies the
    decision detail for each one: code_signals (what in the codebase implies this
    emitter), do_not_use_when (when picking it is a mistake), and how_to_verify
    (how to confirm it worked afterwards). Those three are the questions a weak
    model could not answer from `captures`/`misses`/`cost` alone.

    Overlay rather than replace: the structural entry knows language-specific
    truths the generic spec does not, so a hand-written value always wins and the
    spec only fills gaps. Compact by default — the full specs are 226 KB.
    """
    try:
        from api import emitter_meta as _em
    except Exception:
        try:
            from python_backend.api import emitter_meta as _em
        except Exception:
            return opts
    out = []
    for opt in opts:
        eid = str(opt.get("id") or "")
        rich = _em.spec(eid) if full else _em.compact(eid)
        if not rich:
            out.append(opt)
            continue
        merged = dict(rich)
        # THE ANSWER FOR *THIS* LANGUAGE, up front and in one line. The `enforced`
        # field is a thorough multi-language paragraph that opens with "PYTHON:
        # gated at runtime" — measured: a cold model bridging an ELECTRON app read
        # that opening clause, assumed its selection was enforced, and only learned
        # otherwise from the commit response afterwards. Thoroughness that has to
        # be read to the end is not an answer.
        try:
            from emitters import selection_report as _sr
        except Exception:
            try:
                from python_backend.emitters import selection_report as _sr
            except Exception:
                _sr = None
        if _sr is not None and eid:
            try:
                rep = _sr(lang, [eid])
                if rep:
                    merged["enforced_here"] = (
                        f"{lang or 'this language'}: "
                        + ("YOUR SELECTION IS ENFORCED — " if rep[0].get("enforced")
                           else "NOT ENFORCED — ")
                        + str(rep[0].get("how") or ""))
            except Exception:
                pass
        for key, hand in opt.items():
            if key not in merged:
                merged[key] = hand      # good_for / note — spec has no such field
                continue
            # Both have it. `merged.update(opt)` used to let the hand-written value
            # win unconditionally, which silently defeated the whole point: a
            # 10-byte "negligible" replaced logging_bridge's 1,319-byte measured
            # cost, including the correction about console output. Keep whichever
            # actually says more — measured across all 9 emitters the spec is
            # richer on every content field, and the two exceptions are both
            # `languages`, where the hand-written prose beats a bare list.
            if len(str(hand)) > len(str(merged[key])):
                merged[key] = hand
        out.append(merged)
    return out


def emitter_detail_note(lang: str, opts: list[dict]) -> dict:
    """State how much emitter spec text the compact view is withholding.

    Silent truncation is the failure mode this project exists to prevent: a model
    that cannot tell a summary from a complete answer will act on the summary.
    Keyed with a leading underscore because catalog_token() excludes underscore
    keys — asking for detail must never invalidate the token the agent is about to
    commit with. (That exact bug was introduced and caught once already.)
    """
    try:
        from api import emitter_meta as _em
    except Exception:
        try:
            from python_backend.api import emitter_meta as _em
        except Exception:
            return {}
    ids = [str(o.get("id") or "") for o in (opts or [])]
    hidden = _em.hidden_bytes([i for i in ids if i in _em.ids()])
    if hidden <= 0:
        return {}
    return {
        "fields_clipped": ("each field above is cut at a sentence boundary and is "
                           "marked […detail=full] where that happened"),
        "hidden_bytes": hidden,
        "get_the_rest": ("GET /bridge/catalog?detail=full — same catalog_token, so "
                         "asking does not invalidate the plan you are about to "
                         "commit"),
        "what_is_in_it": ("benefit_examples (worked before/after for concrete "
                          "program kinds), lang_reason (why each language is or is "
                          "not supported), the full caveat and enforced text, the "
                          "untruncated code_signals list, and the FULL worked "
                          "recipes below (87 KB: how_to_recognise, why_each, "
                          "deliberately_not, what_would_still_be_invisible)"),
        # Index only. Embedding the full recipes here made the compact catalog
        # LARGER than detail=full, which is the opposite of the point.
        "worked_recipes_index": _em.recipe_index(lang),
        "how_to_use_the_index": (
            "DO NOT treat this as a lookup table. Ten shapes cannot cover the "
            "combinations real programs actually come in, and the closest-looking "
            "recipe is routinely the wrong answer: a Python program that reads "
            "keypresses AND serves HTTP AND embeds a C extension matches three of "
            "these partially and none of them correctly. Derive instead — read "
            "code_evidence.signals, then match those signals against each option's "
            "`code_signals` field and take the UNION of what they imply, minus "
            "anything that option's `do_not_use_when` rules out. These recipes are "
            "worked examples of that derivation, useful for seeing the reasoning "
            "applied end-to-end. They are not a menu of supported program types, "
            "and a program matching none of them is normal, not unsupported."),
        "spec_corrections": _em.corrections(),
    }


def _adapter_families(limit_families: int = 40) -> dict:
    """Adapter families + counts, NOT 658 individual names.

    A flat dump would be both unreadable and self-defeating: the point is for the
    agent to choose deliberately, and nobody chooses deliberately from 658
    undifferentiated strings. Families narrow it; av_list_adapters drills in.
    """
    try:
        from connectors import log_adapters as la
    except Exception:
        from python_backend.connectors import log_adapters as la  # type: ignore
    fams: dict[str, int] = {}
    for a in la.REGISTRY:
        fam = getattr(a, "family", "") or getattr(a, "language", "") or "other"
        fams[str(fam)] = fams.get(str(fam), 0) + 1
    top = dict(sorted(fams.items(), key=lambda kv: -kv[1])[:limit_families])
    # `builtin_total` is what catalog_token digests, NOT `total`. See catalog_token
    # for why: `total` counts user adapters, so the agent adding one — which the
    # catalog explicitly asks it to do when a format is uncovered — invalidated the
    # token it was holding, guaranteeing one wasted commit in the prescribed
    # catalog -> add_adapter -> commit path. Measured on a cold run: exactly that.
    try:
        _builtin_total = len(la.builtin_names())
    except Exception:
        _builtin_total = len(la.REGISTRY)
    return {"total": len(la.REGISTRY), "builtin_total": _builtin_total,
            "families": top,
            "drill_in": "av_list_adapters(family=..., q=...) for the names",
            "note": ("adapters PARSE logs that already exist; emitters CREATE logs "
                     "that do not. A program with no logging needs an emitter "
                     "first — an adapter alone has nothing to read.")}


#: (label, regex, what it argues FOR, which emitter/setting it bears on).
#: These are the properties of a codebase that actually change which hooks are
#: worth installing — the "how the code is built" half of the decision. Reported
#: as EVIDENCE with counts and example files, never as a conclusion: two programs
#: with identical signals can still want different bridges, and the agent is the
#: one who can read the code and say why.
_CODE_SIGNALS = [
    # NOT "broad_except". The first version of this only matched `except:` and
    # `except Exception:` — and so missed `except KeyError: return None`, which
    # hides a failure every bit as completely. Whether the clause is broad is a
    # style question; whether it DISCARDS is the diagnostic one.
    ("exception_handlers",
     r"except\b[^\n]*:|catch\s*\([^)]*\)|rescue\b|\bcatch\s*\{",
     "places where errors are intercepted at all",
     "context for the two signals below — a high count with no logging is a "
     "program that eats its own failures"),
    ("discards_error",
     r"except\b[^\n]*:\s*(?:#[^\n]*)?\n\s*(?:pass|return\s+None|return\s*$|continue)\b"
     r"|catch\s*\([^)]*\)\s*\{\s*\}"
     r"|rescue\b[^\n]*\n\s*(?:nil|next)\b",
     "handlers whose body throws the error away (pass / return None / continue / "
     "empty block) — the failure becomes invisible to every other hook",
     "swallowed_exceptions — this is the strongest possible case for it"),
    ("logs_in_handler",
     r"except\b[^\n]*:\s*(?:#[^\n]*)?\n\s*(?:logger?\.|logging\.|self\.log|"
     r"console\.(?:error|warn)|print\()",
     "handlers that DO report the error",
     "logging_bridge — routing what it already reports may be enough; full "
     "in-process hooks could be redundant"),
    ("threads", r"\bthreading\.|\bThread\(|concurrent\.futures|"
                r"worker_threads|std::thread|go func\(|Task\.Run",
     "concurrency — a crash on a worker never reaches the main excepthook",
     "uncaught_exceptions (its thread hook)"),
    ("async", r"\basync def\b|\bawait\b|asyncio\.|Promise\.|\.then\(",
     "async work — failures surface as unhandled rejections, not tracebacks",
     "uncaught_exceptions"),
    ("subprocess", r"subprocess\.|os\.system|child_process|Process\.Start|"
                   r"exec\.Command|popen",
     "it launches other programs, whose output is lost unless captured",
     "stdout_tee, and `agentvision run` for the child"),
    ("existing_logging", r"\blogging\.|getLogger|log4j|logback|Serilog|"
                        r"winston|console\.(log|error|warn)|fmt\.Print|"
                        r"println!|NSLog|slf4j",
     "the program ALREADY logs",
     "logging_bridge (route what exists) — full hooks may be redundant"),
    ("prints_only", r"^\s*print\(|^\s*puts\b|^\s*echo\b",
     "it prints but does not log properly",
     "stdout_tee — cheap and immediately useful here"),
    ("gui_toolkit", r"tkinter|PyQt|PySide|kivy|wxPython|Avalonia|WinForms|"
                    r"electron|SwiftUI|Cocoa|GLFW|SDL|imgui",
     "it has a GUI — the screen shows state no log contains",
     "visual_capture: true"),
    ("web_service", r"flask|django|fastapi|express\(|http\.createServer|"
                    r"ASP\.NET|gin\.|actix|rocket::",
     "it is a service — likely headless",
     "visual_capture: false; prefer log/structured hooks"),
    ("network_io", r"requests\.|urllib|httpx|aiohttp|fetch\(|axios|"
                  r"HttpClient|net/http|reqwest",
     "network calls — timeouts and non-2xx are a common silent failure",
     "content-severity is already on; consider watches on status= codes"),
    ("file_io", r"\bopen\(|fs\.readFile|File\.Open|ioutil\.|std::fstream",
     "file I/O — permission/missing-path errors are often swallowed",
     "swallowed_exceptions"),
]

# The 59-signal compositional vocabulary (signal_vocab.py). Appended rather than
# replacing: 14 of the new signals SUPERSEDE one of the coarse originals above and
# say so in their `supersedes` field, but the originals are what six months of
# committed plans were reasoned against, and silently retiring them would change
# the meaning of every stored plan. Both fire; the new one is sharper and the
# agent is told which it refines.
try:
    from signal_vocab import SIGNALS as _VOCAB_SIGNALS
except Exception:                                  # pragma: no cover
    try:
        from python_backend.signal_vocab import SIGNALS as _VOCAB_SIGNALS
    except Exception:
        # A thin catalog is recoverable; a broken import is not. Degrade to the
        # original 12 rather than take the whole scan down.
        _VOCAB_SIGNALS = []
_CODE_SIGNALS = _CODE_SIGNALS + list(_VOCAB_SIGNALS)

#: Source extensions worth scanning, mapped to a language label.
_EXT_LANG = {
    ".py": "python", ".js": "node", ".mjs": "node", ".ts": "node",
    ".rb": "ruby", ".java": "java", ".cs": "dotnet", ".go": "go",
    ".rs": "rust", ".c": "c", ".cc": "cpp", ".cpp": "cpp", ".h": "cpp",
    ".sh": "shell", ".swift": "swift", ".kt": "kotlin", ".php": "php",
    # ── Browser source. Their absence was the single worst blind spot: a React
    # app's components live in .tsx/.jsx and NOTHING opened them, so a front end
    # scanned as though it did not exist.
    ".tsx": "node", ".jsx": "node", ".vue": "node", ".svelte": "node",
    ".cjs": "node", ".mts": "node", ".cts": "node",
    ".html": "html", ".htm": "html",
    # ── Native and platform sources the signal families reach into.
    ".m": "objc", ".mm": "objc",
    ".hpp": "cpp", ".hh": "cpp", ".cxx": "cpp",
    ".lua": "lua", ".gd": "gdscript", ".dart": "dart", ".ps1": "powershell",
    ".bash": "shell", ".zsh": "shell",
    # ── Config. NOT a runtime (see _NON_RUNTIME_LANGS) — these are read because
    # a program's launch surface and schedule live here, not in its source:
    # package.json scripts, pyproject [project.scripts], compose `command:`,
    # a k8s CronJob `schedule:`, a systemd `ExecStart=`.
    ".json": "config", ".toml": "config", ".yml": "config", ".yaml": "config",
    ".ini": "config", ".cfg": "config", ".service": "config", ".cron": "config",
}

#: Files with NO extension that still decide how a program is launched or
#: scheduled. _EXT_LANG is keyed on suffix, so these were structurally invisible:
#: a project with a real nightly crontab reported no schedule at all.
_NAME_LANG = {
    "crontab": "config", "makefile": "config", "dockerfile": "config",
    "procfile": "config", "justfile": "config", "rakefile": "ruby",
    "gemfile": "ruby", "brewfile": "config", "vagrantfile": "ruby",
    "jenkinsfile": "config", ".env": "config", ".envrc": "config",
    ".bashrc": "shell", ".profile": "shell",
}

_SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist",
              "build", "target", "vendor", ".tox", "site-packages", "agentvision",
              ".idea", ".vscode", "obj", "bin"}


def _vocab_limits() -> str:
    """What the vocabulary knows it cannot see, published verbatim.

    Every signal here is a text scanner with no parser, so there are whole classes
    of program behaviour it is structurally blind to — an error converted to a
    default value, a mobile UI, a route registered in a loop. An agent that does
    not know the blind spots will read silence as absence, so the blind spots ship
    with the evidence.
    """
    try:
        from signal_vocab import KNOWN_LIMITS
    except Exception:
        try:
            from python_backend.signal_vocab import KNOWN_LIMITS
        except Exception:
            return ""
    return KNOWN_LIMITS


#: (key) -> (tree_fingerprint, result). code_signals() is the expensive half of
#: catalog() — ~28 s on a 3.6 MB repo — and catalog() is fetched more than once
#: per bridge (compact, full, any replan). Scanning an unchanged tree three times
#: is pure waste.
#:
#: Keyed on a CONTENT FINGERPRINT rather than a time-to-live. A TTL would have
#: served stale evidence to anyone who edited a file and re-fetched inside the
#: window, and stale evidence is the specific failure this whole subsystem exists
#: to prevent: the agent would plan against code that no longer exists and have no
#: way to know. Fingerprinting costs a stat walk (milliseconds) against a 28 s
#: scan, so correctness here is nearly free.
_SIGNAL_CACHE: dict[tuple, tuple[tuple, dict]] = {}
#: Cap the entry count so a long-lived server watching many projects cannot grow
#: this without bound. These results are ~10-50 KB each.
_SIGNAL_CACHE_MAX = 16


def _skipped(path: Path, root: Path) -> bool:
    """Is `path` inside a build/vendor directory *within the project*?

    Matched against the path RELATIVE to the project root, and that relativity
    is the whole point. This used to test `path.parts`, i.e. the ABSOLUTE path,
    so a skip-listed name anywhere ABOVE the root blanked the entire scan.
    Measured on one file copied to three locations differing only in an ancestor
    directory name: `plainname/proj` -> 1 file, signals
    [exception_handlers, discards_error, error_to_default_value];
    `build/proj` -> 0 files, no signals; `agentvision/proj` -> the same nothing.
    Both reported `scan.complete: true`, so "no signal" was indistinguishable
    from "never opened" — the one confusion this scan's own docstring promises
    never to create.

    It also degraded the menu, not just the evidence: languages_present went
    empty, so `swallowed_exceptions`, `uncaught_exceptions` and `logging_bridge`
    were not offered at all and could not be chosen for a Python program. Real
    paths that trip it include ~/build/..., ~/dist/..., ~/bin/..., /opt/vendor/...
    and, worst of all, anything under a folder called `agentvision`.

    Kept as one helper because both the fingerprint and the scan must skip the
    SAME files: if they disagree, the cache key stops describing what was read.
    """
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    return any(p in _SKIP_DIRS for p in parts)


def _tree_fingerprint(root: Path, max_files: int) -> tuple:
    """(file count, newest mtime, total size) over the files the scan would read.

    Cheap enough to run on every call and strong enough that an edit, an added
    file or a deleted one all change it. Deliberately NOT a hash of contents:
    that would cost a full read, which is most of what the cache is avoiding.
    """
    n = 0
    newest = 0.0
    size = 0
    try:
        for path in root.rglob("*"):
            if n >= max_files:
                break
            if _skipped(path, root):
                continue
            if not (_EXT_LANG.get(path.suffix.lower())
                    or _NAME_LANG.get(path.name.lower())):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            n += 1
            size += st.st_size
            if st.st_mtime > newest:
                newest = st.st_mtime
    except Exception:
        return ()                      # unreadable tree -> never cache
    return (n, round(newest, 3), size)


def code_signals(project_root, *, max_files: int = 400,
                 max_bytes_per_file: int = 400_000,
                 max_total_bytes: int = 5_000_000,
                 max_seconds: float = 120.0,
                 use_cache: bool = True) -> dict:
    """Read the program and report HOW IT IS BUILT.

    This exists because a catalog of options without evidence about the code makes
    a relevant choice impossible and a lazy one undetectable — "install
    everything" and "install the right things" look identical on the way in. With
    signal counts and example files, a plan can be checked against the program it
    claims to fit.

    Bounded on purpose (file count, bytes, skip-list): this runs on a first
    connection and must not turn into a repo-wide scan of node_modules.

    COST, measured, so nobody has to rediscover it. 68 signals over 3.6 MB of
    Python takes ~22 s on an M2 — roughly 9 s/MB, linear, with no hot spot: the
    five most expensive signals are 27% of the total and the pattern set is
    simply large. Three deliberate choices follow from that.

    * The byte budget is the real bound and it is deterministic. A wall-clock
      limit alone would make the same repo scan differently depending on machine
      load, so `max_seconds` is only a runaway backstop.
    * Truncation is always REPORTED (`scan.complete`). A partial scan whose gaps
      are invisible is worse than a slow one, because "signal absent" and "file
      never opened" become indistinguishable and an agent reads the first.
    * The result is cached, because catalog() is fetched more than once per
      bridge and re-scanning an unchanged tree buys nothing.

    Slow is acceptable here in a way it is nowhere else in AgentVision: this runs
    ONCE per program, on the user's own CPU, and it decides what every later call
    can see.
    """
    root = Path(project_root)
    if not root.is_dir():
        return {"scanned": 0, "error": f"not a directory: {project_root}"}

    import time as _time
    _ck = (str(root.resolve()), max_files, max_bytes_per_file, max_total_bytes)
    _fp = _tree_fingerprint(root, max_files) if use_cache else ()
    if use_cache and _fp:
        _hit = _SIGNAL_CACHE.get(_ck)
        if _hit and _hit[0] == _fp:
            return _hit[1]

    import re as _re
    # 5-tuples carry argues_against / confidence / traps / languages; the original
    # 12 are 4-tuples and still work unchanged.
    compiled = []
    for _entry in _CODE_SIGNALS:
        _name, _rx, _why, _arg = _entry[0], _entry[1], _entry[2], _entry[3]
        _extra = dict(_entry[4]) if len(_entry) > 4 else {}
        _langs = {str(x).lower() for x in (_extra.get("languages") or [])}
        try:
            compiled.append((_name,
                             _re.compile(_rx, _re.MULTILINE | _re.IGNORECASE),
                             _why, _arg, _extra, _langs))
        except Exception:
            # A signal that will not compile must not take the whole scan with it.
            continue
    hits: dict[str, dict] = {}
    langs: dict[str, int] = {}
    scanned = 0
    biggest: list[tuple[int, str]] = []
    total_bytes = 0
    stopped_early = ""
    _t0 = _time.time()

    for path in root.rglob("*"):
        if scanned >= max_files:
            stopped_early = (f"hit the {max_files}-file cap")
            break
        # Scan cost is linear in bytes, so this is the lever that actually bounds
        # it. Reported rather than silent: a truncated scan whose gaps are unknown
        # would let an agent read "signal absent" as "feature absent", which is
        # the same confident silence this project exists to prevent.
        if total_bytes >= max_total_bytes:
            stopped_early = (f"hit the {max_total_bytes // 1_000_000} MB byte "
                             f"budget after {scanned} files")
            break
        # Wall-clock ceiling. The byte budget bounds a big repo, but 68 signals
        # over pathological content could still outrun it, and this runs inside a
        # request handler where an unbounded scan reads to the caller as a hang.
        if scanned and (scanned & 7) == 0 and (_time.time() - _t0) > max_seconds:
            stopped_early = (f"hit the {max_seconds:.0f}s time budget after "
                             f"{scanned} files")
            break
        if not path.is_file():
            continue
        if _skipped(path, root):
            continue
        lang = _EXT_LANG.get(path.suffix.lower()) or _NAME_LANG.get(path.name.lower())
        if not lang:
            continue
        langs[lang] = langs.get(lang, 0) + 1
        try:
            if path.stat().st_size > max_bytes_per_file:
                continue
            text = path.read_text(errors="replace")
        except Exception:
            continue
        scanned += 1
        total_bytes += len(text)
        biggest.append((len(text.splitlines()), str(path.relative_to(root))))
        for name, rx, why, arg, extra, sig_langs in compiled:
            # Language gating. Two reasons, and correctness is the bigger one:
            # running a Qt keyPressEvent pattern over a .yml or a Go regex over a
            # .py invites false positives that no amount of tuning removes. It is
            # also the difference between a scan that finishes and one that
            # doesn't — 56 large alternations over every byte of every file was
            # measured at 33s on 3.5 MB, against 2.6s for the original 12.
            if sig_langs and lang not in sig_langs:
                continue
            found = rx.findall(text)
            if not found:
                continue
            rec = hits.get(name)
            if rec is None:
                rec = {"count": 0, "files": [], "means": why, "argues_for": arg}
                # Only carry non-empty extras, so a 4-tuple signal does not gain
                # a row of blank fields that reads as "checked, nothing to say".
                for k in ("argues_against", "confidence", "false_positive_traps",
                          "supersedes", "supersedes_why"):
                    if str(extra.get(k) or "").strip():
                        rec[k] = extra[k]
                hits[name] = rec
            rec["count"] += len(found)
            rel = str(path.relative_to(root))
            if rel not in rec["files"] and len(rec["files"]) < 5:
                rec["files"].append(rel)

    biggest.sort(reverse=True)
    # primary_language is a PLURALITY VOTE, and that makes it fragile in exactly
    # the polyglot projects it matters most for. Two guards:
    #
    # 1. Non-runtime labels never win. Markup and config files outnumber source in
    #    plenty of real repos, and a project that elected "config" as its primary
    #    language would be offered no in-process emitter at all.
    # 2. The winner is reported alongside EVERY language present, because a
    #    Django+React repo is legitimately both and a scalar cannot say so. Callers
    #    deciding what to OFFER must use languages_present, not this field — a
    #    plurality of .py files is not evidence that the browser half does not
    #    exist.
    _cacheable = not stopped_early
    runtime = {k: v for k, v in langs.items() if k not in _NON_RUNTIME_LANGS}
    vote = runtime or langs
    primary = max(vote.items(), key=lambda kv: (kv[1], kv[0]))[0] if vote else "unknown"
    tied = sorted(k for k, v in vote.items() if v == vote.get(primary))
    out = {
        "scanned_files": scanned,
        "languages_by_file_count": dict(sorted(langs.items(),
                                               key=lambda kv: -kv[1])),
        "primary_language": primary,
        #: Every runtime language with at least one file. THIS is what emitter
        #: offering must key on; primary_language is a summary for humans.
        "languages_present": sorted(runtime),
        "primary_language_is_tied": tied if len(tied) > 1 else [],
        "primary_language_note": (
            "a plurality vote over source files, with config/markup excluded. In a "
            "polyglot project it is NOT the whole answer — read languages_present. "
            "A repo of 30 .py files and 2 .tsx files is still a browser program in "
            "part, and its client-side failures are invisible to any Python hook."),
        "largest_files": [{"lines": n, "file": f} for n, f in biggest[:8]],
        "signals": hits,
        "scan": {
            "bytes_read": total_bytes,
            "seconds": round(_time.time() - _t0, 2),
            "signals_available": len(compiled),
            "complete": not stopped_early,
            **({"stopped_early": stopped_early,
                "so": ("part of this project was NEVER READ. A signal missing "
                       "from the list above may simply be in a file the scan did "
                       "not reach — do NOT read its absence as evidence. Re-run "
                       "code_signals with a higher max_files / max_total_bytes, "
                       "or point the profile at the subdirectory that matters.")}
               if stopped_early else {}),
        },
        "known_limits": _vocab_limits(),
        "how_to_use_this": (
            "Each signal names what it MEANS and what it argues for. Match your "
            "selections to these — a plan that installs swallowed_exceptions with "
            "no broad_except signal, or omits it with hundreds, is not a decision "
            "about THIS program. Absence is evidence too: no gui_toolkit signal "
            "means visual capture is probably wasted — but read `scan.complete` "
            "first, because absence proves nothing about a file that was never "
            "opened. Signals COMPOSE: take the union of what fired, then subtract "
            "what each one's argues_against rules out. There is no table of "
            "program types here on purpose."),
    }
    # Never cache a truncated scan. It would pin a partial answer to a
    # fingerprint that looks perfectly valid, and the next caller could not tell
    # without re-reading `scan.complete`.
    if use_cache and _cacheable and _fp:
        if len(_SIGNAL_CACHE) >= _SIGNAL_CACHE_MAX:
            _SIGNAL_CACHE.clear()
        _SIGNAL_CACHE[_ck] = (_fp, out)
    return out


def catalog(profile, language: str = "", *, tool_groups: Optional[dict] = None,
            readers: Optional[list] = None, existing_logs: Optional[list] = None,
            full_detail: bool = True) -> dict:
    """Everything the agent must review before the bridge can be built.

    FULL detail by default, deliberately. Everywhere else in AgentVision the rule
    is to spend as few tokens as possible, because the steady state is a loop:
    frames, log ranges and diagnoses are read over and over for as long as the
    program runs, so verbosity there is a recurring tax.

    The catalog is the opposite shape. It is read ONCE per program, ever — the
    bridge is not rebuilt on restart — and the decision it feeds determines what
    every later call can possibly see. Under-informing the model here to save a
    few thousand tokens buys nothing and risks the one choice that cannot be
    cheaply corrected: an emitter set that never captures the failure. So this
    call pays full price on purpose, and `?detail=compact` exists for callers that
    genuinely want the short form.
    """
    lang = (language or getattr(profile, "language", "") or "").lower()
    # Scan FIRST: what the code actually contains decides which emitters are even
    # offered. Doing this the other way round is how a full-stack project got a
    # Python-only menu.
    # NEVER fall back to the current directory. `or "."` meant that a profile with
    # no project_root — which includes `custom`, the placeholder AgentVision SHIPS
    # WITH — scanned whatever the bridge server's cwd happened to be. In practice
    # that is AgentVision's own tree: measured 161 files, 3.9 MB, 56 SECONDS, and
    # the result was returned as `code_evidence` about the user's program. So a
    # brand-new user's very first av_bridge_catalog() described AgentVision's
    # shape (GUI toolkit, web service, threads) as if it were theirs, and any plan
    # built on it would instrument for the wrong program entirely.
    #
    # Confident, wrong, and slow. An absent project_root is a question to ask, not
    # a directory to guess.
    _root = str(getattr(profile, "project_root", "") or "").strip()
    if _root:
        evidence = code_signals(_root)
    else:
        evidence = {
            "scanned_files": 0,
            "error": "no project_root is configured for this profile",
            "signals": {},
            "what_to_do": (
                "set project_root to the folder holding the program's code, then "
                "call av_bridge_catalog() again — av_create_profile(project_root="
                "...) for a new program, or edit the active profile. Until then "
                "there is NO code evidence, and a plan committed now would be "
                "the blind guess this gate exists to prevent."),
            "why_not_guessed": (
                "AgentVision will not substitute its own working directory here. "
                "It used to, and that meant scanning ITSELF and presenting the "
                "result as evidence about your program."),
        }
    present = list(evidence.get("languages_present") or [])
    lang = lang or str(evidence.get("primary_language") or "").lower()
    if lang in ("unknown", "none"):
        lang = ""
    emitters = _emitter_options(lang, full=full_detail, also_present=present)
    body = {
        "version": PLAN_VERSION,
        "program": getattr(profile, "display_name", "") or getattr(profile, "name", ""),
        "language_detected": lang or "unknown",
        "what_this_is": (
            "The options AgentVision can build into THIS program. Nothing here is "
            "chosen yet — that is your call, based on what the code is and does. "
            "AgentVision deliberately does not pick: it would pick the same set "
            "for a web server and a GPU emulator."),
        "emitters_available": emitters,
        "adapters": _adapter_families(),
        "source_readers": readers or [],
        "mcp_tool_groups": tool_groups or {},
        "existing_logs_found": existing_logs or [],
        # The evidence half. Without this the agent is choosing from a menu with
        # no idea what the diner ordered.
        "code_evidence": evidence,
        "capture_settings": {
            "interval_seconds": "0.1 - 10; ASK THE USER, do not assume",
            "how_to_ask": ("call av_capture_start() with NO interval and "
                           "AgentVision puts the question to the user itself "
                           "(MCP elicitation) and uses their answer. Where the "
                           "client cannot show a prompt it falls back and says "
                           "so in `capture_rate_choice` — read that field "
                           "before telling the user what rate you are running."),
            "note": ("frame rate is a cost lever, not a detail: 10 fps on a "
                     "static UI is waste, 1 fps on an animation misses the bug"),
        },
        "you_must_decide": [
            "which emitters to build in (or none, if the program already logs well)",
            "which adapters to pin for each log source (or 'auto')",
            # Discovery is only worth having if it can be ACTED on. The catalog
            # reported undeclared logs while the commit path had no way to wire
            # one, so the honest answer ("it already logs, just read that file")
            # produced a bridge that read nothing. State the mechanism here,
            # because this is the text the agent always sees.
            "WHICH EXISTING LOGS TO READ: an entry in existing_logs_found with "
            "declared=false is NOT read until you pin it — adapters={\"<its "
            "label>\": \"auto\"}. That is the whole mechanism for 'this program "
            "already logs, use that instead of installing an emitter'.",
            "the capture interval — after asking the user",
            "whether visual capture is useful at all for this program (a headless "
            "service needs none; a GUI needs it)",
        ],
        "do_not": [
            "do NOT select every emitter — each one must answer to something in "
            "code_evidence. An everything-plan is the same guess AgentVision used "
            "to make, just made by you.",
            "do NOT pin an adapter to a log file nothing writes — check "
            "existing_logs_found and av_log_where() first.",
            "do NOT commit a plan with no emitters AND no adapter pins when "
            "existing_logs_found is non-empty: that bridge reads nothing at all "
            "and is refused (error BRIDGE_WOULD_READ_NOTHING).",
            "do NOT enable visual capture for a headless service, or skip it for "
            "a GUI: code_evidence.signals tells you which this is.",
        ],
        "required_in_plan": {
            "emitters": "list of ids you are choosing",
            "why": ("{emitter_id: reason} — one line per selection, tied to a "
                    "code_evidence signal. This is what makes the plan a "
                    "diagnosis instead of a checkbox."),
            "rationale": "one line on the program as a whole",
            "tools": ("{'primary': [...], 'not_relevant': {tool: why not}}. "
                      "PRIMARY MEANS: the handful you would reach for FIRST on "
                      "this program, in order — a shortlist, not a prediction and "
                      "not an availability list. Aim for 4-10; more than 25 is "
                      "rejected as a copy of the catalog. NOT_RELEVANT MEANS: "
                      "tools you deliberately ruled out, each with the reason. "
                      "Listing every remaining tool is NOT expected — 3-8 of the "
                      "most tempting-but-wrong ones is the useful answer. Every "
                      "tool stays callable either way; this records judgement, it "
                      "does not restrict anything. Read mcp_tool_groups[*].tools: "
                      "each entry carries what the tool returns, what it NEEDS, "
                      "its token cost, and a per-program `verdict`. A tool whose "
                      "verdict is already 'n/a' does not need to be repeated in "
                      "not_relevant — it is filtered for you."),
            "visual_capture": ("true/false — YOU set this explicitly; it is not "
                               "inferred. If code_evidence has a gui_toolkit "
                               "signal, that argues for true. A headless service "
                               "or CLI tool should be false, which also makes "
                               "every frame tool irrelevant."),
            "adapters": (
                "{source_label: adapter_name | 'auto'}. YOU MUST VERIFY THAT THE "
                "ADAPTER IS APPROPRIATE — do not assume, and do not trust a name. "
                "There are 658 adapters and several will happily claim a format "
                "they parse WRONGLY at full confidence: a C engine logging "
                "'[DEBUG] Sprite.c:32 - msg' was taken by `coreboot_cbmem` at 1.00, "
                "which buried the file:line inside the message and set "
                "source=coreboot. Nothing errored; the data was simply wrong from "
                "then on.\n"
                "HOW TO VERIFY, per log source: take a REAL line from the file and "
                "call av_test_adapter(line=<that line>). Then check three things in "
                "the reply — (1) is_fallback is false, (2) `level` matches what the "
                "line actually says, (3) `source` is the program's own module/"
                "subsystem and NOT the adapter's own name. If `source` comes back "
                "as 'structural' or as the adapter's name, the format is not "
                "understood: error-fingerprint grouping and av_source_at_error will "
                "both be wrong even though every call still succeeds.\n"
                "'auto' is a fine and usually correct answer — but it is only "
                "correct once you have SEEN what auto-detection picks. Verifying "
                "is the point; the value you write down is secondary."),
        },
        "how_to_commit": ("av_bridge_commit(plan={...}) — copy plan_template below, "
                          "fill it in, send it. The token proves these options "
                          "were read."),
        # A COPYABLE SKELETON, not prose. Two cold models were measured losing an
        # attempt each to inventing the plan shape — one guessed the HTTP envelope,
        # both had to be told the field set. A template costs a few hundred tokens
        # and removes a whole round trip.
        "plan_template": {
            "catalog_token": "<filled in below with the real token>",
            # The number is inlined here, not only in select_at_most: a cold model
            # was measured reading the template verbatim while never noticing the
            # separate limit field, and reporting "no numeric cap was explicit".
            # "most programs need 1-2" used to sit on the same line as the cap, and
            # a cold model read the two as one number: it reported "2 is the hard
            # max" and dropped uncaught_exceptions on a program that wanted it.
            # The ceiling and the typical case are now separated, and the typical
            # case is named as a habit rather than a rule.
            "emitters": [
                f"<pick from emitters_available[].id. HARD CEILING: "
                f"{blanket_threshold(len(emitters)) - 1} of the "
                f"{len(emitters)} offered — more than that is rejected as a "
                f"blanket guess. That is a CEILING, not a target: pick every "
                f"emitter you can tie to a code_evidence signal and no others, "
                f"which is commonly 1-3 but is however many the evidence "
                f"supports>"],
            "why": {"<emitter id>": "<the code_evidence signal that justifies it, "
                                    "15+ chars, name the signal>"},
            "rationale": "<one line: what this program IS and what its real "
                         "failure mode is>",
            "visual_capture": "<true if code_evidence has gui_toolkit, else false>",
            "capture": {"interval_seconds":
                        "<ASK THE USER; 0.1-10 — or omit and let "
                        "av_capture_start() ask them for you>"},
            # Keyed by the source LABEL, and the valid labels are listed in
            # `adapter_pin_labels` below. A cold model guessed "log" when the real
            # label was "text"; the pin was then silently ignored and the source
            # fell back to "auto" with no warning.
            "adapters": {"<one of adapter_pin_labels>": "auto"},
            "tools": {
                "primary": ["<4-10 tool names you would reach for FIRST here>"],
                "not_relevant": {"<tool name>": "<why it cannot help this program>"},
            },
        },
        # The exact keys plan.adapters accepts. Without this the label had to be
        # inferred from existing_logs_found, and a wrong guess was accepted
        # silently rather than rejected.
        "adapter_pin_labels": sorted({
            str(e.get("label")) for e in (existing_logs or [])
            if e.get("label")} | {"events", "text"}),
        "select_at_most": {
            "emitters": blanket_threshold(len(emitters)) - 1,
            "why_a_limit": (
                "Selecting nearly every emitter is rejected. It is the same blanket "
                "guess AgentVision used to make, just with your name on it. Most "
                "programs need 1-2. A signal seen ONCE is evidence of almost "
                "nothing — weigh counts in code_evidence, do not just check for "
                "non-zero."),
            "tools_primary": 25,
        },
        "if_you_are_calling_over_http": (
            "POST /bridge/commit expects the plan NESTED: "
            '{"plan": {...}, "replan": false}. Sending the plan fields at the top '
            "level returns error=PLAN_NOT_WRAPPED. The MCP tool "
            "av_bridge_commit(plan={...}) does this wrapping for you."),
    }
    # Underscore key: catalog_token() skips these, so `?detail=full` cannot change
    # the token and invalidate a plan the agent is mid-way through writing.
    if not full_detail:
        note = emitter_detail_note(lang, emitters)
        if note:
            body["_emitter_detail"] = note
    else:
        try:
            from api import emitter_meta as _em
        except Exception:
            try:
                from python_backend.api import emitter_meta as _em
            except Exception:
                _em = None
        if _em is not None:
            body["_worked_recipes"] = _em.recipes(lang)
            body["_detail_level"] = {
                "serving": "FULL — every emitter spec and worked recipe in full",
                "why": ("this call happens ONCE per program and decides what all "
                        "later calls can see, so it is not token-budgeted. Read it "
                        "properly rather than skimming."),
                "not_a_precedent": ("the steady-state tools ARE budgeted — "
                                    "av_log_range, av_get_frame, av_diagnose and "
                                    "friends return compact results because they "
                                    "are called repeatedly for as long as the "
                                    "program runs. Do not expect this verbosity "
                                    "again, and do not read its absence there as "
                                    "missing information."),
                "compact_form": "GET /bridge/catalog?detail=compact",
            }
    body["catalog_token"] = catalog_token(body)
    # Put the REAL token into the template so it is copy-paste rather than
    # copy-paste-then-remember-to-substitute. Done after the digest is computed;
    # catalog_token() ignores plan_template, so this cannot affect the token.
    body["plan_template"]["catalog_token"] = body["catalog_token"]
    return body


def catalog_token(catalog_body: dict) -> str:
    """Stable digest of the catalog's decision-relevant content.

    Deliberately excludes volatile fields, so a token stays valid while the agent
    is thinking but changes if the actual OPTIONS change (a new emitter, a new
    adapter family) — in which case the agent should look again before committing.

    It digests the BUILT-IN adapter count, not the total. The total includes user
    adapters, so av_add_adapter invalidated the token — and av_add_adapter is what
    the catalog tells the agent to do when it reports a format as uncovered. The
    prescribed path (catalog -> add the missing adapter -> commit) therefore ended
    in a guaranteed stale-token rejection and a forced re-read; measured on a cold
    run as one of only two failed attempts. An adapter the agent added itself is
    not the options changing under it, which is the only thing this guard is for.
    """
    _ad = catalog_body.get("adapters") or {}
    material = {
        "version": catalog_body.get("version"),
        "language": catalog_body.get("language_detected"),
        "emitters": sorted(e.get("id", "") for e in
                           catalog_body.get("emitters_available") or []),
        "adapter_total": _ad.get("builtin_total", _ad.get("total")),
        # Underscore keys are PRESENTATION metadata, not options — `_detail`
        # appears only in the compact rendering. Including them made the token
        # differ between `/bridge/catalog` and `/bridge/catalog?detail=full`, so
        # an agent that read the fuller form was rejected on commit: the catalog
        # prescribing a path that cannot succeed. That exact shape has already
        # bitten this project once (adding an adapter used to invalidate the token
        # the catalog told you to add it with), so it is filtered here rather than
        # left to whoever adds the next presentation key.
        "tool_groups": sorted(k for k in (catalog_body.get("mcp_tool_groups") or {})
                              if not str(k).startswith("_")),
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ── Plan persistence ──────────────────────────────────────────────────────────

def plan_path(output_folder) -> Path:
    return Path(output_folder) / PLAN_FILENAME


def read_plan(output_folder) -> Optional[dict]:
    try:
        p = plan_path(output_folder)
        if p.exists():
            return json.loads(p.read_text(errors="replace"))
    except Exception:
        pass
    return None


def legacy_sealed(output_folder) -> bool:
    """Did this program pass the OLD coverage-only gate?

    Honoured so that installing this version cannot brick a bridge someone is
    already using. The new gate exists to stop AgentVision guessing on programs
    it has never seen — not to invalidate working setups.
    """
    try:
        return (Path(output_folder) / LEGACY_MARKER).exists()
    except Exception:
        return False


def is_sealed(output_folder) -> bool:
    plan = read_plan(output_folder)
    if plan and plan.get("sealed"):
        return True
    return legacy_sealed(output_folder)


def blanket_threshold(n_offered: int) -> int:
    """How many emitters constitute "just install everything" for this language.

    The original rule was an absolute `>= 6`, which was DEAD CODE: the largest
    menu any language gets is 5 (python), so the anti-blanket guard could never
    fire on a real program. A cold model was measured selecting 4 of 5 for a small
    script, justifying each with a signal seen once — exactly the blanket guess
    the gate exists to stop — and it passed.

    Proportional to the menu, with a floor of 3 so a 3-item menu still permits a
    genuine two-emitter answer.
    """
    if n_offered <= 0:
        return 6                      # unknown menu: fall back to the old absolute
    return max(3, -(-n_offered * 3 // 4))          # ceil(0.75 * n)


def validate_plan(plan: dict, expected_token: str,
                  offered: Optional[list] = None,
                  known_labels: Optional[list] = None,
                  known_adapters: Optional[list] = None,
                  uncovered_labels: Optional[list] = None) -> tuple[bool, list[str]]:
    """Is this a real decision, or an empty object that would seal a blind bridge?

    The token check is the load-bearing one: it is what makes "review the options
    first" impossible to skip.
    """
    errs: list[str] = []
    if not isinstance(plan, dict):
        return False, ["plan must be an object"]

    token = str(plan.get("catalog_token") or "")
    if not token:
        errs.append("catalog_token is missing — call av_bridge_catalog() first and "
                    "pass the token it returns; the bridge is not sealed on trust")
    elif token != expected_token:
        errs.append(f"catalog_token {token!r} does not match the current catalog "
                    f"({expected_token!r}) — the available options changed, "
                    f"re-read av_bridge_catalog() before committing")

    # A decision has to actually decide something. An empty plan is the blind
    # bridge this whole mechanism exists to prevent.
    emitters = plan.get("emitters")
    if emitters is None:
        errs.append("plan.emitters is required — a list of emitter ids to build "
                    "in, or [] with a reason if the program already logs well")
    elif not isinstance(emitters, list):
        errs.append("plan.emitters must be a list")

    if not str(plan.get("rationale") or "").strip():
        errs.append("plan.rationale is required — one line on WHY this set fits "
                    "this program, so the choice is auditable later")

    if emitters == [] and "already" not in str(plan.get("rationale", "")).lower() \
            and "no log" not in str(plan.get("rationale", "")).lower():
        errs.append("plan.emitters is empty — if that is deliberate, say so in "
                    "rationale (e.g. 'already logs well: ...'), because an empty "
                    "set is indistinguishable from a forgotten one")

    # ── PER-SELECTION JUSTIFICATION ──────────────────────────────────────────
    # The point of handing the decision to the agent is that the choice is made
    # against the actual code. A plan can satisfy every check above and still be
    # a checkbox exercise, so each selected emitter must carry its own reason.
    # This is the difference between "the agent decided" and "the agent clicked".
    if isinstance(emitters, list) and emitters:
        why = plan.get("why")
        if not isinstance(why, dict):
            errs.append("plan.why is required: {emitter_id: reason} with one line "
                        "per selected emitter, each tied to something in "
                        "catalog.code_evidence — without it the selection cannot "
                        "be told apart from installing things at random")
        else:
            missing = [e for e in emitters if not str(why.get(e, "")).strip()]
            if missing:
                errs.append(f"plan.why is missing a reason for: {missing} — every "
                            f"emitter must answer to something in the code")
            thin = [e for e in emitters
                    if 0 < len(str(why.get(e, "")).strip()) < 15]
            if thin:
                errs.append(f"plan.why entries for {thin} are too short to be a "
                            f"reason — say which code signal justifies each")

    # Selecting EVERYTHING is the lazy failure mode this gate exists to stop: it
    # reproduces exactly the blanket install that made AgentVision's own guessing
    # wrong, only with the agent's name on it.
    try:
        from bridge_plan import _CODE_SIGNALS  # noqa: F401  (self-import guard)
    except Exception:
        pass
    n_offered = len(offered or [])
    limit = blanket_threshold(n_offered)
    if isinstance(emitters, list) and len(emitters) >= limit:
        of = f" of the {n_offered} on offer" if n_offered else ""
        errs.append(
            f"plan selects {len(emitters)} emitters{of} — that is close to "
            f"everything available. Installing the lot is the same blanket guess "
            f"this gate exists to prevent; keep what the code evidence actually "
            f"supports. Each emitter must answer to a signal that is actually "
            f"PRESENT in strength, not merely non-zero: a signal seen once is "
            f"evidence of almost nothing. Pick the smallest set that covers this "
            f"program's real failure mode (usually 1-2), and say what you are "
            f"deliberately NOT capturing in the rationale.")

    errs.extend(_validate_tools(plan))
    errs.extend(_validate_adapter_labels(plan, known_labels, known_adapters))
    errs.extend(_validate_adapter_coverage(plan, uncovered_labels))
    return (not errs), errs


def _validate_adapter_coverage(plan: dict,
                               uncovered: Optional[list] = None) -> list[str]:
    """A source AgentVision KNOWS it cannot parse may not be passed over silently.

    Deliberately narrow. It does NOT demand proof of verification for every source
    — an agent can always claim it looked, so that check would be theatre. It fires
    only where AgentVision has already MEASURED the problem: `_format_coverage`
    sampled real lines from a discovered log and nothing but a generic fallback
    claimed the format.

    That state needs a gate precisely BECAUSE everything still appears to work.
    `level` usually survives the fallback, but `source` degrades to the fallback's
    own name, so av_errors_by_fingerprint groups every error together and
    av_source_at_error has nothing to jump to — while every call keeps returning
    200. Silent partial correctness is the failure mode this project keeps digging
    out.

    Two ways to satisfy it: pin an adapter for that label (having written one with
    av_add_adapter), or state in the rationale that the weaker parse is accepted.
    Either is a fine engineering answer. Neither being present is not.
    """
    labels = [str(x) for x in (uncovered or []) if str(x).strip()]
    if not labels:
        return []
    # Only complain about a source the plan actually intends to READ. A plan that
    # names no emitters and pins no adapters is either a bridge that would read
    # nothing (BRIDGE_WOULD_READ_NOTHING says so, and says it better) or a declared
    # visual-only bridge, where an unparsed log is irrelevant because nobody is
    # parsing it. Firing here anyway made this gate PREEMPT those two specific,
    # more useful errors — the agent got told to acknowledge a fallback when its
    # real problem was that its bridge had no source at all.
    if not (plan.get("emitters") or []) and not (plan.get("adapters") or {}):
        return []
    pinned = {str(k) for k, v in (plan.get("adapters") or {}).items()
              if str(v or "").strip() and str(v).lower() != "auto"}
    text = (str(plan.get("rationale") or "") + " "
            + str(plan.get("adapters_note") or "")).lower()
    # An acknowledgement must actually name the situation, not merely be wordy.
    ACK = ("accept", "weaker", "fallback", "unparsed", "structural",
           "no adapter", "raw only", "degraded")
    if any(w in text for w in ACK):
        return []
    unhandled = [l for l in labels if l not in pinned]
    if not unhandled:
        return []
    return [
        f"log source(s) {unhandled} are in a format NO adapter specifically parses "
        f"— AgentVision sampled real lines and only a generic fallback claimed "
        f"them. Do ONE of: (a) write an adapter with av_add_adapter(...) and pin it "
        f"in plan.adapters under that label, or (b) say in plan.rationale that you "
        f"accept the weaker parse. Sealing with neither hides a source whose "
        f"`source` field will be the fallback's own name on every line, which "
        f"silently breaks av_errors_by_fingerprint grouping and av_source_at_error "
        f"while every call keeps succeeding. VERIFY FIRST: "
        f"av_test_adapter(line=<a real line from that file>) and check is_fallback, "
        f"level, and source."]


def _validate_adapter_labels(plan: dict, known: Optional[list] = None,
                             known_adapters: Optional[list] = None) -> list[str]:
    """plan.adapters is {source LABEL: adapter NAME} — reject a wrong key OR value.

    Silently ignoring a wrong key is the worst option: the agent believes it pinned
    a parser, the source quietly stays on "auto", and the mistake only shows up
    later as a misparse nobody is looking for. Measured on a cold model that pinned
    "log" when the label was "text".

    The VALUE needs the same treatment and did not have it. A pin of
    {"text": "no_such_adapter"} validated clean, was written into the profile, and
    then `_read_source` fell back to `raw` — while still REPORTING the name that
    never resolved, so /bridge/report showed adapter_resolved: no_such_adapter.
    That is the same defect as the mis-keyed pin (a decision quietly not taken),
    one field to the right, and it is strictly harder to notice.
    """
    ad = plan.get("adapters")
    if not isinstance(ad, dict) or not ad:
        return []
    errs = []
    allowed = {str(x) for x in (known or [])} | {"events", "text", "stdout"}
    bad = [k for k in ad if str(k) not in allowed]
    if bad:
        errs.append(
            f"plan.adapters has unknown source label(s) {bad} — valid labels for "
            f"this program are {sorted(allowed)}. The key is the log source's "
            f"LABEL (see catalog.adapter_pin_labels), not the adapter name and not "
            f"a filename. A wrong key would be ignored and the source would stay "
            f"on 'auto', so it is rejected instead.")
    if known_adapters:
        names = {str(a) for a in known_adapters}
        for label, value in ad.items():
            v = str(value).strip()
            if not v:
                errs.append(f"plan.adapters[{label!r}] is empty — use \"auto\" to "
                            f"detect the format, or a real adapter name.")
            elif v != "auto" and v not in names:
                near = sorted(n for n in names
                              if v.lower() in n.lower() or n.lower() in v.lower())
                errs.append(
                    f"plan.adapters[{label!r}] = {v!r} is not a registered adapter. "
                    f"Use \"auto\", or a name from av_list_adapters(); add your own "
                    f"first with av_add_adapter if this format has none."
                    + (f" Closest existing: {near[:5]}." if near else ""))
    return errs


def _validate_tools(plan: dict) -> list[str]:
    """Check the MCP-tool half of the decision.

    Tools are not installed into the program the way emitters are — all of them
    stay callable. What this records is which ones are WORTH calling here, so a
    later session (and the GUI) can see that the question was actually asked.

    Deliberately not a hard requirement when omitted-with-reason: forcing a tool
    list on every plan would just produce a rubber-stamped copy of the catalog,
    which is the same laziness the emitter checks exist to catch.
    """
    errs: list[str] = []
    tools = plan.get("tools")
    if tools is None:
        return ["plan.tools is required — {'primary': [...], 'not_relevant': "
                "{tool: reason}} naming the tools worth calling for THIS program. "
                "Pass {'primary': [], 'note': '...'} if you truly reviewed them and "
                "have no preference, but say so explicitly."]
    if not isinstance(tools, dict):
        return ["plan.tools must be an object, not a bare list — it needs both the "
                "tools you chose and (at least briefly) what you ruled out"]

    primary = tools.get("primary")
    if primary is None:
        errs.append("plan.tools.primary is required — the handful of tools you would "
                    "actually reach for on this program, in order")
    elif not isinstance(primary, list):
        errs.append("plan.tools.primary must be a list of tool names")
    elif len(primary) > 25:
        errs.append(f"plan.tools.primary lists {len(primary)} tools — that is a copy "
                    f"of the catalog, not a choice. Name the ones you would reach "
                    f"for first; the rest stay callable regardless.")

    if not primary and not str(tools.get("note") or "").strip():
        errs.append("plan.tools.primary is empty and no note explains why — an empty "
                    "list is indistinguishable from a skipped review")

    nr = tools.get("not_relevant")
    if nr is not None and not isinstance(nr, dict):
        errs.append("plan.tools.not_relevant must be {tool: reason} — the reason is "
                    "the point; a bare list records no thinking")
    return errs


def write_plan(output_folder, plan: dict, *, catalog_token_value: str,
               built: Optional[dict] = None) -> dict:
    record = {
        "version": PLAN_VERSION,
        "sealed": True,
        "sealed_at": datetime.now().isoformat(),
        "catalog_token": catalog_token_value,
        "decided_by": "agent",
        "emitters": plan.get("emitters") or [],
        "adapters": plan.get("adapters") or {},
        "capture": plan.get("capture") or {},
        "visual_capture": plan.get("visual_capture"),
        "rationale": plan.get("rationale") or "",
        # Per-selection reasons, kept so a later session can see WHY this bridge
        # looks the way it does instead of re-deriving it.
        "why": plan.get("why") or {},
        # Which MCP tools are worth calling here. Unlike emitters these are not
        # built INTO the program — every tool stays callable — so this is a
        # relevance record, not a restriction. Kept because the alternative is
        # 89 undifferentiated tools and an agent guessing which apply.
        "tools": plan.get("tools") or {},
        "built": built or {},
    }
    p = plan_path(output_folder)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(record, indent=2))
    return record


def status(output_folder, profile) -> dict:
    """Is the bridge built, and if not, what is missing."""
    plan = read_plan(output_folder)
    legacy = legacy_sealed(output_folder)
    sealed = bool((plan and plan.get("sealed")) or legacy)
    out = {
        "sealed": sealed,
        "state": "BUILT" if sealed else "PROVISIONAL",
        "program": getattr(profile, "display_name", "") or getattr(profile, "name", ""),
        "plan": plan,
        "sealed_by_legacy_marker": legacy and not (plan and plan.get("sealed")),
    }
    if sealed:
        out["note"] = ("This program's bridge is already built — the first-connection "
                       "review happened once and does not repeat. Everything works "
                       "normally from here.")
        if out["sealed_by_legacy_marker"]:
            out["note"] += (" (Sealed by the older coverage-only preflight marker; "
                            "call av_bridge_commit(replan=True) to record a real "
                            "agent plan for it.)")
    else:
        out["note"] = ("FIRST CONNECTION — the bridge is NOT built yet. AgentVision "
                       "will not guess which logs and tools this program needs. "
                       "Review av_bridge_catalog(), then av_bridge_commit(plan=...).")
        out["blocked"] = ["capture/start", "install (emitters)"]
        out["next"] = "av_bridge_catalog()"
        # Told here because this is the call immediately BEFORE the catalog, and a
        # cold Haiku reported both of these as things it had to learn the hard way:
        # that the full catalog "would have been unusable" and that it only knew a
        # short form existed because it had been told out of band, and that two of
        # its three commit attempts died on shape errors the refusals explained
        # only after the fact.
        out["about_the_catalog"] = (
            "it is LARGE on purpose — read once per program, ever, and it decides "
            "what every later call can possibly see. If you want the short form "
            "first, av_bridge_catalog(detail='compact') returns the same options "
            "and the SAME catalog_token, so asking for either does not invalidate "
            "the plan you are about to commit.")
        out["plan_gotchas"] = [
            "WRAP IT: {'plan': {...}}. Sending the plan's fields at the top level "
            "is rejected as PLAN_NOT_WRAPPED.",
            "A plan with no emitters AND no adapter pins is rejected as "
            "BRIDGE_WOULD_READ_NOTHING. That is not a complaint about you — it "
            "means the bridge as described would have nothing to read. Pick at "
            "least one emitter, or pin an adapter to a log that already exists.",
            "Every selected emitter needs a reason in `why`, and `tools` must be "
            "present with a `primary` shortlist.",
        ]
    return out
