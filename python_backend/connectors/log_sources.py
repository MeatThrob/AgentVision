"""
Multi-source log merging — N heterogeneous logs onto ONE time-aligned timeline.
================================================================================
A single program routinely writes to several logs at once: a JSONL event stream,
a plain-text app log, maybe a framework log in a totally different format (a Java
sidecar, a Go helper, a .NET renderer). AgentVision's promise is that Claude sees
ALL of them merged onto the single UTC-ms timeline, each line normalized into the
one unified event schema (see connectors.log_adapters).

This module is pure standard library so it is testable without the bridge and
runs identically on Windows and macOS. It never raises into a caller.

Public API
----------
    effective_sources(profile)                → resolved [{path, adapter, label}]
    read_normalized(profile, window=…, …)     → merged, ts-sorted list[event]
    detect_language(project_root)             → best-guess language string

Every returned event carries an extra "log_label" and "log_path" so Claude can
tell WHICH log a line came from after the merge.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

#: A log untouched for this long is probably NOT this run's output — the process
#: may be writing to stderr, a different path, or may not be running. Reported,
#: never acted on: a genuinely idle program has a quiet log and that is fine.
STALE_LOG_SECONDS = float(os.environ.get("AGENTVISION_STALE_LOG_S", "120"))

try:
    from connectors import log_adapters as la
except Exception:  # when python_backend is on sys.path directly
    import log_adapters as la  # type: ignore


# ── Source resolution ──────────────────────────────────────────────────────────

def effective_sources(profile) -> list[dict]:
    """Resolve the full, de-duplicated list of log sources for a profile.

    Order: explicit profile.log_sources first, then the legacy action_log_file
    (JSONL) and log_file (auto-detected text). Each entry is normalized to
    {"path", "adapter", "label", "kind"}. adapter="auto" means detect-on-read.
    """
    out: list[dict] = []
    seen: set[str] = set()

    def _add(path: str, adapter: str, label: str, reader: str = ""):
        path = (path or "").strip()
        if not path:
            return
        key = os.path.normcase(os.path.abspath(os.path.expanduser(path)))
        if key in seen:
            return
        seen.add(key)
        out.append({"path": path, "adapter": (adapter or "auto").strip() or "auto",
                    "label": label or Path(path).stem, "reader": (reader or "").strip()})

    for s in (getattr(profile, "log_sources", None) or []):
        if isinstance(s, str):
            _add(s, "auto", Path(s).stem)
        elif isinstance(s, dict):
            _add(s.get("path", ""), s.get("adapter", "auto"),
                 s.get("label", ""), s.get("reader", ""))

    # Legacy fields fold in as first-class sources.
    _add(getattr(profile, "action_log_file", "") or "", "jsonl", "actions")
    _add(getattr(profile, "log_file", "") or "", "auto", "log")
    return out


# ── Streamed / binary telemetry: the SOURCE-READER interface ───────────────────
# Not every source is a plain-text log the adapters can parse line-by-line. Some
# are STRUCTURED or ENCODED streams — Docker/containerd json-file logs, OTLP
# JSON-lines, a binary ring buffer, a socket. A SourceReader decodes such a
# source into raw records (str lines OR pre-decoded dicts); the merge layer then
# normalizes each record through the SAME log-adapter pipeline, so everything
# still converges on the one unified event schema.
#
# Contract (implement read_records; keep it bounded + never raise):
#   read_records(path, max_offset, tail_bytes) -> list[str | dict]
#     • a str  → normalized through the auto-detected / named line adapter
#     • a dict → normalized through the `jsonl` adapter (native-passthrough), so a
#                dict shaped like {ts_ms|ts, category, source, data:{message,…}}
#                is emitted as-is; other dicts get field-alias normalization.
# Register with register_reader("<name>", ReaderClass); reference it from a
# profile source as {"path": …, "reader": "<name>"}.

class SourceReader:
    """Base class for a non-plain-text log source (see module docstring)."""
    kind = "base"

    def read_records(self, path: str, *, max_offset: int = 0,
                     tail_bytes: int = 1_048_576) -> list:
        return []

    # Shared helper: read the tail bytes of a growing file as decoded text lines.
    @staticmethod
    def _tail_lines(path: str, max_offset: int, tail_bytes: int) -> list[str]:
        try:
            size = max_offset if max_offset > 0 else os.path.getsize(path)
            start = max(0, size - tail_bytes)
            with open(path, "rb") as f:
                f.seek(start)
                raw = f.read(size - start)
        except Exception:
            return []
        lines = raw.decode("utf-8", errors="replace").splitlines()
        if start > 0 and lines:
            lines = lines[1:]
        return [ln for ln in lines if ln.strip()]


class DockerJsonFileReader(SourceReader):
    """Reads a Docker/containerd `json-file` container log, where each line is
    {"log":"msg\\n","stream":"stdout|stderr","time":"2024-..Z"}. Decodes it into
    unified-schema dicts (ts from `time`, category from `stream`, message from
    `log`) — a shape the jsonl adapter passes straight through. Testable on macOS
    by tailing a growing file; the same reader works for any json-file stream."""
    kind = "docker_json"

    def read_records(self, path, *, max_offset=0, tail_bytes=1_048_576):
        import json as _json
        out = []
        for ln in self._tail_lines(path, max_offset, tail_bytes):
            try:
                rec = _json.loads(ln)
            except Exception:
                continue
            if not isinstance(rec, dict) or "log" not in rec:
                continue
            stream = rec.get("stream", "stdout")
            out.append({
                "ts": rec.get("time", ""),
                "category": "error" if stream == "stderr" else "stdout",
                "source": f"container.{stream}",
                "data": {"message": (rec.get("log") or "").rstrip("\n"),
                         "stream": stream},
            })
        return out


READER_REGISTRY: dict = {
    "docker_json": DockerJsonFileReader,
}


def register_reader(name: str, reader_cls) -> None:
    """Register a SourceReader class under `name` so profiles can reference it
    via {"path": …, "reader": name}. Idempotent."""
    READER_REGISTRY[name] = reader_cls


def list_readers() -> list[str]:
    return sorted(READER_REGISTRY.keys())


# ── Binary source readers (connectors/readers.py) ──────────────────────────────
# Pure-stdlib struct decoders for the catalog's fixed-layout BINARY formats:
# utmp/wtmp/btmp, lastlog, faillock, wtmpdb/lastlog2 (sqlite), NetFlow v5,
# pcap (incl. pflog DLT 117 container), MRT (RFC 6396), Snort unified2.
# readers.py deliberately does NOT import this module (no circular import);
# it exposes BINARY_READERS = {name: class} and we register them here. Its
# module docstring is also the honest DEFERRED roadmap for the binary formats
# that need stateful/template parsers or external tooling (EVTX, SMF, IPFIX…).
try:
    from connectors.readers import BINARY_READERS as _BINARY_READERS
except Exception:  # when python_backend is on sys.path directly
    try:
        from readers import BINARY_READERS as _BINARY_READERS  # type: ignore
    except Exception:
        _BINARY_READERS = {}
for _name, _cls in _BINARY_READERS.items():
    READER_REGISTRY.setdefault(_name, _cls)


# ── Reading one source ──────────────────────────────────────────────────────────

def _read_source(src: dict, *, max_offset: int = 0, tail_bytes: int = 1_048_576,
                 sample: int = 60) -> tuple[list[dict], str]:
    """Read + normalize a single source. Returns (events, resolved_adapter_name).

    If the source declares a `reader`, that SourceReader decodes it into records
    which are normalized through the adapters (dicts → jsonl, strings → the named
    line adapter). Otherwise the file is tailed and parsed line-by-line.

    max_offset>0 bounds the read to bytes [..max_offset] so capture-time
    alignment is preserved. tail_bytes caps how far back we read (1 MiB default)."""
    path = os.path.expanduser(src["path"]) if src.get("path") else ""
    if not path or not os.path.exists(path):
        return [], src.get("adapter", "auto")

    label = src.get("label", "")
    reader_name = src.get("reader", "")

    # ── Streamed/structured source via a registered SourceReader ─────────────
    if reader_name:
        reader_cls = READER_REGISTRY.get(reader_name)
        if reader_cls is not None:
            import json as _json
            jsonl = la.get_adapter("jsonl")
            events: list[dict] = []
            try:
                records = reader_cls().read_records(
                    path, max_offset=max_offset, tail_bytes=tail_bytes)
            except Exception:
                records = []
            for rec in records:
                if isinstance(rec, dict):
                    ev = jsonl.parse_line(_json.dumps(rec, default=str))
                else:
                    ev = la.parse_line(str(rec))
                if ev is None:
                    continue
                ev["log_label"] = label
                ev["log_path"] = path
                la.escalate_by_content(ev)
                events.append(ev)
            return events, f"reader:{reader_name}"

    try:
        size = max_offset if max_offset > 0 else os.path.getsize(path)
        start = max(0, size - tail_bytes)
        with open(path, "rb") as f:
            f.seek(start)
            raw = f.read(size - start)
    except Exception:
        return [], src.get("adapter", "auto")

    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # drop the partial first line

    adapter_name = src.get("adapter", "auto")
    if adapter_name == "auto":
        adapter, _conf, _scores = la.detect_adapter(lines[:sample])
        adapter_name = adapter.name
    adapter = la.get_adapter(adapter_name)
    if adapter is None:
        # Report the adapter that ACTUALLY ran. Echoing the declared name while
        # silently parsing with `raw` made a bad pin undetectable: /bridge/report
        # showed adapter_resolved = the name that never resolved, so the one
        # surface built to reveal a mis-wired bridge confirmed it was fine.
        adapter = la.get_adapter("raw")
        adapter_name = f"raw (declared {adapter_name!r} is not registered)"

    events: list[dict] = []
    for ln in lines:
        if not ln.strip():
            continue
        ev = adapter.parse_line(ln)
        if ev is None:
            ev = la.get_adapter("raw").parse_line(ln)
        if ev is None:
            continue
        ev["log_label"] = label
        ev["log_path"] = path
        # A failure encoded only in the message body (e.g. "ok=False") would
        # otherwise normalize to INFO and be invisible to diagnose.
        la.escalate_by_content(ev)
        events.append(ev)
    return events, adapter_name


# ── Merged, time-aligned read ────────────────────────────────────────────────────

#: Records AgentVision itself emitted. They are NOT the program's output and must
#: not be pushed back at the agent as if they were: measured live, the watchdog
#: alone had written 902 `program.stuck` records (420 KB) into a project's
#: actions.jsonl, which then dominated the raw feed. That write has since been
#: moved to AgentVision's own observer log, but logs written by earlier versions
#: are still contaminated on disk, so this filter stays.
_SELF_EMITTED_MARKERS = ('"source": "agentvision', '"source":"agentvision',
                         "'source': 'agentvision")


def _is_self_emitted(line: str) -> bool:
    return any(m in line for m in _SELF_EMITTED_MARKERS)


def read_raw_delta(profile, offsets: Optional[dict] = None, *,
                   max_bytes_per_source: int = 262_144,
                   collapse_repeats: bool = True,
                   drop_self_emitted: bool = True) -> dict:
    """THE RAW BYTES. Every new line since `offsets`, verbatim, per source.

    This is deliberately NOT read_normalized(). No adapter runs, no level is
    assigned, no category is inferred, nothing is ranked, nothing is dropped for
    being "uninteresting". AgentVision is a transport and alignment layer, not a
    judge of what matters — and the cost of forgetting that was measured on this
    project: the normalized/summarized view reported "health 100, no strong
    failure signals, program looks healthy" while 180 GPU present failures sat in
    the log, and the actual bug was only ever visible in the raw draw lines,
    whose `target=`/`tex0=` structure the summary had flattened into prose.

    The ONE reduction applied is exact-duplicate collapsing, and only because it
    is lossless: 21,982 byte-identical copies of one line (49% of a real boot
    log) carry no more information than that line plus a count. A DISTINCT line
    is never dropped, never re-levelled, never reordered.

      offsets               {path: byte_offset} — read only past this point
      max_bytes_per_source  hard read bound per file (newest bytes win)
      collapse_repeats      lossless run/total collapsing of identical lines

    Returns {"sources": [{label, path, from_offset, new_offset, bytes_new,
    lines, lines_total, lines_emitted, truncated}], "offsets": {path: offset}}
    so the caller can persist offsets and resume exactly where it stopped.
    """
    offsets = offsets or {}
    out_sources: list[dict] = []
    new_offsets: dict = {}

    for src in effective_sources(profile):
        path = os.path.expanduser(src["path"]) if src.get("path") else ""
        if not path or not os.path.exists(path):
            continue
        try:
            size = os.path.getsize(path)
        except Exception:
            continue
        frm = int(offsets.get(src["path"], offsets.get(path, 0)) or 0)
        # A truncated/rotated file (size < offset) means a fresh run: start over
        # rather than silently reporting nothing.
        rotated = frm > size
        if rotated:
            frm = 0
        start = frm
        if size - start > max_bytes_per_source:
            start = size - max_bytes_per_source
        try:
            with open(path, "rb") as f:
                f.seek(start)
                raw = f.read(size - start)
        except Exception:
            continue

        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if start > frm and lines:
            lines = lines[1:]          # dropped a partial line when we skipped
        lines = [ln for ln in lines if ln.strip()]
        self_dropped = 0
        if drop_self_emitted:
            kept_lines = [ln for ln in lines if not _is_self_emitted(ln)]
            self_dropped = len(lines) - len(kept_lines)
            lines = kept_lines
        total = len(lines)

        emitted: list = lines
        if collapse_repeats and total:
            emitted = []
            prev = None
            run = 0
            for ln in lines:
                if ln == prev:
                    run += 1
                    continue
                if prev is not None:
                    emitted.append(prev if run == 1 else
                                   {"line": prev, "repeat": run})
                prev, run = ln, 1
            if prev is not None:
                emitted.append(prev if run == 1 else
                               {"line": prev, "repeat": run})

        # FRESHNESS. A raw log is only evidence if it is THIS run's output. Cost
        # of not checking, measured on this project: an analysis of "180 GPU
        # present failures" was run against a log whose last write was 5 hours
        # earlier — the live process was writing to stderr, not this file — and
        # nothing flagged it. `tail -f` cannot tell you the file is dead either;
        # noticing is exactly the kind of thing a bridge layer should do.
        try:
            age_s = max(0.0, time.time() - os.path.getmtime(path))
        except Exception:
            age_s = None

        new_offsets[src["path"]] = size
        out_sources.append({
            "label": src.get("label", ""), "path": src["path"],
            "adapter_declared": src.get("adapter", "auto"),
            "from_offset": frm, "new_offset": size,
            "bytes_new": max(0, size - frm),
            "last_write_age_s": (None if age_s is None else round(age_s, 1)),
            "stale": (age_s is not None and age_s > STALE_LOG_SECONDS),
            "lines_total": total, "lines_emitted": len(emitted),
            "truncated_head": start > frm, "rotated": rotated,
            "self_emitted_dropped": self_dropped,
            "lines": emitted,
        })

    return {"sources": out_sources, "offsets": new_offsets}


def read_normalized(profile, *, window: Optional[tuple[float, float]] = None,
                    limit: int = 500, level: str = "",
                    label: str = "", max_offsets: Optional[dict] = None
                    ) -> list[dict]:
    """Read EVERY source for the profile, normalize each line, and merge onto one
    ts-sorted timeline (oldest→newest).

      window       (t0_ms, t1_ms) inclusive filter on ts_ms
      limit        cap on returned events (keeps the most recent on overflow)
      level        only events at/above this canonical level (e.g. "WARN")
      label        only events from the source with this label
      max_offsets  {path: byte_offset} — bound each source's read for capture
                   -time alignment (the bridge passes the per-frame snapshot)

    Events without a parseable timestamp (ts_ms is None) sort to the END so they
    never displace time-anchored lines; Claude can still see them.
    """
    max_offsets = max_offsets or {}
    min_rank = _LEVEL_RANK.get(la.canonical_level(level), 0) if level else 0

    merged: list[dict] = []
    for src in effective_sources(profile):
        if label and src.get("label") != label:
            continue
        mo = int(max_offsets.get(src["path"], 0) or 0)
        events, _ = _read_source(src, max_offset=mo)
        for ev in events:
            ms = ev.get("ts_ms")
            if window is not None and ms is not None:
                if ms < window[0] or ms > window[1]:
                    continue
            if min_rank and _LEVEL_RANK.get((ev.get("level") or "").upper(), 0) < min_rank:
                continue
            merged.append(ev)

    # Stable sort: timestamped first (ascending), untimestamped appended in order.
    merged.sort(key=lambda e: (e.get("ts_ms") is None,
                               e.get("ts_ms") if e.get("ts_ms") is not None else 0.0))
    if limit and len(merged) > limit:
        merged = merged[-limit:]
    return merged


_LEVEL_RANK = {"TRACE": 1, "DEBUG": 2, "INFO": 3, "WARN": 4, "ERROR": 5, "FATAL": 6}


# ── Language / framework auto-detection (used by `agentvision attach`) ───────────
# THE ONE shared detector — cli.py and installer.py both import THIS function so
# attach, install, and profile wiring can never disagree. Cheap filesystem sniff;
# best-effort, never raises.
#
# Two-stage strategy:
#   1. MARKER pass — explicit project/build files (they win: they are the
#      strongest signal of the ecosystem).
#   2. EXTENSION CENSUS fallback — count source files per language across the
#      tree and pick the dominant one. This is what catches BARE source trees
#      with no build file at all (e.g. a folder of .cs files → dotnet, a folder
#      of .cpp/.hpp files → cpp), and disambiguates when several markers match.

_LANG_MARKERS = [
    # (language, [marker files/globs relative to root]) — order = tiebreak priority
    ("python",  ["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile"]),
    ("dotnet",  ["*.csproj", "*.sln", "*.fsproj", "*.vbproj", "global.json",
                 "Directory.Build.props", "nuget.config"]),
    ("java",    ["pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle"]),
    ("node",    ["package.json", "tsconfig.json"]),
    ("go",      ["go.mod"]),
    ("rust",    ["Cargo.toml"]),
    ("ruby",    ["Gemfile", "*.gemspec"]),
    ("php",     ["composer.json"]),
    ("android", ["AndroidManifest.xml", "app/build.gradle"]),
    # C/C++ build systems. Makefile is a WEAK signal (many langs use it) so it
    # sits last; a stronger language marker above wins, and the extension census
    # settles genuine C/C++ trees.
    ("cpp",     ["CMakeLists.txt", "meson.build", "*.vcxproj", "conanfile.txt",
                 "conanfile.py", "vcpkg.json", "configure.ac",
                 "Makefile", "makefile", "GNUmakefile"]),
]

# Markers so generic that many languages ship them — they must NOT short-circuit
# detection; the source-file census overrides them.
_WEAK_MARKERS = {"Makefile", "makefile", "GNUmakefile"}

# Source-file extension → language. C# (.cs), F#/VB → dotnet (same emitter/config);
# every C/C++ header + source variant → cpp; TS/JSX/etc → node.
_EXT_LANG = {
    ".py": "python",
    ".cs": "dotnet", ".fs": "dotnet", ".fsx": "dotnet", ".vb": "dotnet",
    ".java": "java", ".kt": "java", ".kts": "java", ".scala": "java", ".groovy": "java",
    ".js": "node", ".mjs": "node", ".cjs": "node", ".ts": "node", ".jsx": "node", ".tsx": "node",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".c++": "cpp", ".cp": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp", ".h++": "cpp",
    ".c": "cpp", ".h": "cpp", ".cu": "cpp", ".cuh": "cpp",
}

_CENSUS_IGNORE = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", "target", "bin",
    "obj", ".idea", ".vscode", "agentvision", ".agentvision", "log", "logs",
    "vendor", "Pods", ".gradle",
}


def _extension_census(root: Path, cap: int = 6000) -> dict[str, int]:
    """Count source files per language across the tree (bounded). Skips build
    output + vendor dirs so the count reflects the project's OWN code."""
    counts: dict[str, int] = {}
    n = 0
    try:
        for dirpath, dirnames, filenames in os.walk(str(root)):
            dirnames[:] = [d for d in dirnames
                           if d not in _CENSUS_IGNORE and not d.startswith(".")]
            for fn in filenames:
                lang = _EXT_LANG.get(os.path.splitext(fn)[1].lower())
                if lang:
                    counts[lang] = counts.get(lang, 0) + 1
                n += 1
                if n >= cap:
                    return counts
    except Exception:
        pass
    return counts


def detect_language(project_root: str) -> str:
    """Best-guess primary language of a project. Explicit build/project MARKERS
    win; otherwise the dominant language by source-file count (which catches
    BARE .cs / .cpp / header-only trees with no build file). '' if unknown."""
    root = Path(project_root or "").expanduser()
    if not root.exists():
        return ""
    try:
        names = {p.name for p in root.iterdir()}
    except Exception:
        return ""

    # 1. Marker pass — collect languages by STRONG vs WEAK marker. A weak
    #    marker (a bare Makefile — used by projects of every language) must
    #    never short-circuit; it only settles a tie when no source files and no
    #    stronger marker point elsewhere.
    strong: list[str] = []
    weak: list[str] = []
    for lang, markers in _LANG_MARKERS:
        lang_strong = lang_weak = False
        for m in markers:
            if "*" in m:
                try:
                    hit = any(root.glob(m))
                except Exception:
                    hit = False
            else:
                hit = m in names
            if hit:
                if m in _WEAK_MARKERS:
                    lang_weak = True
                else:
                    lang_strong = True
        if lang_strong and lang not in strong:
            strong.append(lang)
        elif lang_weak and lang not in weak:
            weak.append(lang)

    if len(strong) == 1:
        return strong[0]                       # unambiguous strong marker → done

    census = _extension_census(root)

    if strong:
        # Several strong markers → most source files wins, marker-priority tiebreak.
        if any(census.get(L, 0) for L in strong):
            return max(strong, key=lambda L: (census.get(L, 0), -strong.index(L)))
        return strong[0]

    # 2. No strong markers → dominant language by source-file census. This
    #    catches BARE .cs / .cpp / header-only trees AND correctly demotes a
    #    lone Makefile when the actual source is some other language.
    if census:
        return max(census, key=lambda L: (census[L], L))

    # 3. Only a weak marker, no recognizable source files → last-resort guess.
    if weak:
        return weak[0]
    return ""


#: The canonical sink folder name the installer creates inside a target project.
#: Duplicated from emitters.SINK_DIRNAME rather than imported: this module is used
#: by the pure-stdlib preflight path, and importing emitters here would drag the
#: installer's dependencies into it.
SINK_DIRNAME = "agentvision"


def suggest_log_sources(project_root: str) -> list[dict]:
    """Suggest likely log files for a freshly attached project. Looks in the
    conventional log/ dir and common filenames. Returns [{path, adapter, label}]
    with adapter='auto' so the format is detected on first read."""
    root = Path(project_root or "").expanduser()
    if not root.exists():
        return []
    out: list[dict] = []
    candidates = [
        root / "log" / "actions.jsonl",
        root / "log" / "log.txt",
        root / "logs" / "app.log",
        root / "log" / "app.log",
        # The canonical sink folder. Omitting it meant a program whose logs were
        # ALREADY in agentvision/ was reported as having no logs at all, so the
        # agent was told to install an emitter for output that already existed.
        root / SINK_DIRNAME / "log.txt",
        root / SINK_DIRNAME / "actions.jsonl",
        root / SINK_DIRNAME / "raw_output.txt",
    ]
    # *.log / *.jsonl anywhere log-ish. `.txt` needs care: globbing it at the
    # project root swept up requirements.txt / README.txt / LICENSE.txt and then
    # reported them as logs whose format nothing parses — pure noise, and the kind
    # a weak model would act on. So a .txt only counts when its NAME looks like a
    # log, or when it sits inside a dedicated log directory.
    LOG_NAME_HINTS = ("log", "debug", "output", "trace", "err", "stderr", "stdout",
                      "console", "verbose", "diag")
    LOG_DIRS = (root / "log", root / "logs", root / SINK_DIRNAME)
    for d in (root,) + LOG_DIRS:
        if not (d.exists() and d.is_dir()):
            continue
        try:
            for pat in ("*.log", "*.jsonl"):
                candidates.extend(sorted(d.glob(pat))[:20])
            for p in sorted(d.glob("*.txt"))[:20]:
                if d in LOG_DIRS or any(h in p.stem.lower() for h in LOG_NAME_HINTS):
                    candidates.append(p)
        except Exception:
            pass
    seen: set[str] = set()
    for c in candidates:
        try:
            if c.exists() and c.is_file():
                key = os.path.normcase(str(c))
                if key in seen:
                    continue
                seen.add(key)
                adapter = "jsonl" if c.suffix == ".jsonl" else "auto"
                out.append({"path": str(c), "adapter": adapter, "label": c.stem})
        except Exception:
            pass
    return out


# ── PRE-FLIGHT log-coverage check ───────────────────────────────────────────────
# THE FORCE (stdlib core). Before AgentVision's first bridge start for a program,
# the AI must PROVE the program already emits log/debug-log data AgentVision can
# specifically parse — and, if not, ADD the missing adapter(s) first. This is the
# pure, flask-free engine behind the /preflight route + av_preflight tool.
#
# A source is "covered" when its real log lines route to a SPECIFIC named adapter.
# A source is a GAP when its lines only route to a generic FALLBACK — `structural`
# (universal normalizer), `generic_ts` (any-timestamped-text), or `raw` (last
# resort). A fallback still yields schema-valid events, but it means the format is
# NOT specifically understood, so level/field extraction is weak — exactly the
# coverage the AI is being forced to close with av_add_adapter.

# The generic fallbacks: routing here means "not specifically covered".
FALLBACK_ADAPTERS = {"structural", "generic_ts", "raw"}


def _first_lines(path: str, n: int = 40) -> list[str]:
    """First `n` non-blank lines of a file (bounded read), or [] on any error."""
    out: list[str] = []
    try:
        if not path or not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                if ln.strip():
                    out.append(ln.rstrip("\r\n"))
                if len(out) >= n:
                    break
    except Exception:
        return []
    return out


def _language_adapter_index() -> dict:
    """Map language → sorted list of the SPECIFIC (non-fallback) adapter names
    that target it, from the live REGISTRY. `any`-language adapters are folded in
    under a shared "any" key so callers can spot cross-cutting formats too."""
    idx: dict[str, list[str]] = {}
    for a in la.list_adapters():
        name = a.get("name", "")
        if name in FALLBACK_ADAPTERS:
            continue
        lang = a.get("language", "") or "any"
        idx.setdefault(lang, []).append(name)
    for k in idx:
        idx[k] = sorted(idx[k])
    return idx


def preflight_report(profile, *, project_root: str = "", language: str = "",
                     sample_lines: Optional[list] = None,
                     log_paths: Optional[list] = None, sample: int = 40) -> dict:
    """Assess whether the log/debug-log coverage a program needs already EXISTS.

    Inputs (any subset; the profile supplies the rest):
      profile        a ProgramProfile-like object (its log_sources / log_file /
                     action_log_file are the sources checked) — may be None.
      project_root   overrides where language is detected from.
      language       overrides the detected language.
      sample_lines   raw log lines to assess directly (no file needed) — checked
                     as one synthetic "inline" source.
      log_paths      explicit list of log-file paths to assess instead of / in
                     addition to the profile's own sources.
      sample         how many head lines to sample per file.

    Returns a compact, AI-actionable verdict:
      {ready, language, profile, source_count,
       covered:[{source,adapter,confidence,label}],
       gaps:[{source_or_format, sample, current_fallback, confidence, suggestion}],
       pending:[{source,reason}],            # source configured but no lines yet
       language_specific_adapters_available:[…],
       recommended_actions:[…], directive}

    ready == there is at least one assessable source AND none of the assessed
    sources fell to a fallback. A source that exists but is empty (program not run
    yet) is `pending`, not a gap — it can't yet prove OR disprove coverage, so it
    does not block a first start on its own; a configured-but-fallback source DOES.
    """
    # ── resolve language ─────────────────────────────────────────────────────
    lang = (language or "").strip()
    if not lang:
        lang = (getattr(profile, "language", "") or "").strip()
    root = (project_root or getattr(profile, "project_root", "") or "").strip()
    if not lang and root:
        try:
            lang = detect_language(root) or ""
        except Exception:
            lang = ""

    # ── assemble the sources to assess ───────────────────────────────────────
    sources: list[dict] = []
    if profile is not None and not log_paths and not sample_lines:
        try:
            sources = list(effective_sources(profile))
        except Exception:
            sources = []
    for p in (log_paths or []):
        if isinstance(p, str) and p.strip():
            sources.append({"path": p.strip(), "adapter": "auto",
                            "label": Path(p).stem})
        elif isinstance(p, dict) and p.get("path"):
            sources.append({"path": p["path"], "adapter": p.get("adapter", "auto"),
                            "label": p.get("label", Path(p["path"]).stem)})

    covered: list[dict] = []
    gaps: list[dict] = []
    pending: list[dict] = []

    def _assess_lines(lines: list, label: str, configured: str = "auto") -> None:
        if not lines:
            return
        # Respect an explicitly-configured adapter; else auto-detect.
        if configured and configured != "auto":
            name, conf = configured, 1.0
            scores = {}
        else:
            ad, conf, scores = la.detect_adapter([str(x) for x in lines[:sample]])
            name = ad.name
        sample_line = str(lines[0])[:300]
        if name in FALLBACK_ADAPTERS:
            gaps.append({
                "source_or_format": label,
                "sample": sample_line,
                "current_fallback": name,
                "confidence": round(conf, 3),
                "suggestion": (
                    f"'{label}' only routes to the generic '{name}' fallback — no "
                    "adapter specifically parses this format. Add one with "
                    "av_add_adapter (name/family, a detect regex, an extract regex "
                    "with named groups ts/level/source/message, and this sample)."),
            })
        else:
            covered.append({"source": label, "adapter": name,
                            "confidence": round(conf, 3), "label": label})

    # inline sample lines (no file)
    if sample_lines:
        _assess_lines(list(sample_lines), "inline", "auto")

    for s in sources:
        path = os.path.expanduser(s.get("path", "")) if s.get("path") else ""
        label = s.get("label") or (Path(path).stem if path else "source")
        configured = s.get("adapter", "auto")
        reader = s.get("reader", "")
        if reader:
            # A SourceReader decodes structured/binary streams and re-normalizes
            # through jsonl — treat as specifically covered.
            covered.append({"source": label, "adapter": f"reader:{reader}",
                            "confidence": 1.0, "label": label})
            continue
        if not path or not os.path.exists(path):
            pending.append({"source": label, "path": s.get("path", ""),
                            "reason": "log file does not exist yet"})
            continue
        lines = _first_lines(path, sample)
        if not lines:
            pending.append({"source": label, "path": s.get("path", ""),
                            "reason": "log file is empty (program has not logged yet)"})
            continue
        _assess_lines(lines, label, configured)

    # ── cross-reference language ↔ adapter families ──────────────────────────
    lang_index = _language_adapter_index()
    lang_adapters = sorted(set(lang_index.get(lang, []))) if lang else []
    covered_names = {c["adapter"] for c in covered}
    language_covered = bool(lang) and any(n in set(lang_adapters) for n in covered_names)

    assessable = len(covered) + len(gaps)
    ready = assessable > 0 and not gaps

    # ── recommended actions ──────────────────────────────────────────────────
    actions: list[str] = []
    for g in gaps:
        actions.append(
            f"av_add_adapter for '{g['source_or_format']}' "
            f"(currently '{g['current_fallback']}'): sample = {g['sample'][:120]!r}")
    if assessable == 0 and not pending:
        actions.append(
            "No log sources are configured for this profile. Install the output "
            "side (av_install_project) or add log_sources to the profile, then "
            "re-run av_preflight.")
    if pending and assessable == 0:
        actions.append(
            "Log sources are configured but empty/not-yet-written — coverage can't "
            "be proven until the program logs. Start the program (or launch via "
            "`agentvision run`), then re-run av_preflight; add adapters for any gaps.")
    if lang and gaps and not language_covered:
        actions.append(
            f"Detected language '{lang}'. Specific adapters available for it: "
            f"{', '.join(lang_adapters) or '(none named)'}. If the target's format "
            "matches one, set it explicitly on the source; otherwise add a new one.")
    if ready:
        actions.append("Coverage is complete — safe to start the bridge.")

    directive = (
        "FORCE: before the FIRST bridge start on a NEW program, ensure it already "
        "emits log/debug-log data AgentVision can specifically parse. If `gaps` is "
        "non-empty, call av_add_adapter for EACH missing debug-log type BEFORE "
        "starting capture; then re-run this until ready:true (or start with "
        "force=true to accept the gaps).")

    return {
        "ready": ready,
        "language": lang or None,
        "profile": getattr(profile, "name", None),
        "source_count": len(sources) + (1 if sample_lines else 0),
        "assessable_source_count": assessable,
        "covered": covered,
        "gaps": gaps,
        "pending": pending,
        "language_specific_adapters_available": lang_adapters,
        "language_covered": language_covered,
        "recommended_actions": actions,
        "directive": directive,
    }
