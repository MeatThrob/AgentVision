"""
AgentVision source mirror + token-efficient digest for Claude.

Goal: when AgentVision is bridged to a project, Claude (via MCP) shouldn't
have to grep the whole filesystem to find that project's source. Instead:

  1. We keep an up-to-date INDEX of every source file in the project_root
     (path, size, mtime, line count, language, top-level symbols).
  2. We expose three views to Claude over HTTP/MCP:
       /source/tree     full file tree with sizes + line counts (~5 KB)
       /source/digest   compressed code digest: per-file top-level def/class/
                         export signatures only (~10-50 KB for large projects)
       /source/file     one full file's source on demand (paginated)

  3. We keep a real on-disk MIRROR under
        AgentVision/snapshots/<profile>/source_mirror/
     refreshed whenever a file changed (mtime delta). This lets the GUI
     display the source as the user navigates and gives Claude a stable
     read path that doesn't depend on the user's repo location.

The digest is the killer feature for token efficiency:
  - Full source of a mid-size project: ~250K tokens
  - Digest of the same:  ~8K tokens (just signatures + 1-line docstrings)

Claude reads the digest first to orient, then asks for one full file by path.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

# ── Configuration ────────────────────────────────────────────────────────────

# Files we don't want in the index OR mirror — pure noise.
IGNORE_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "dist", "build", "target", "out", ".idea", ".vscode",
    ".next", ".nuxt", ".cache", ".gradle", ".turbo",
    # AgentVision's own outputs inside the project (legacy + current)
    "log", "snapshots", "agentvision", "AgentVision",
    # Common training-data / dataset dirs — huge, never source
    "ocr_training", "yolo_training", "training_data", "datasets",
    "data", "assets", "models", "weights", "checkpoints",
    # Vendored / generated
    "vendor", "vendored", "third_party", "deps", "dependencies",
    "site-packages", "lib-dynload",
}
IGNORE_SUFFIXES = {
    ".pyc", ".pyo", ".swp", ".swo", ".tmp", ".log", ".lock",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
    ".mp3", ".mp4", ".mov", ".wav", ".pdf", ".zip", ".tar",
    ".gz", ".bz2", ".7z", ".dmg", ".so", ".dylib", ".dll",
    ".pkl", ".npy", ".npz", ".onnx", ".pt", ".bin",
    # Source-control / output artifacts
    ".diff", ".patch", ".bak", ".old", ".orig",
    # Large fonts / binary fixtures
    ".ttf", ".otf", ".woff", ".woff2",
    # Compiled / minified
    ".min.js", ".min.css",
}
# Hard cap on per-file lines for the digest. Massive generated files
# (eg. tokenizer dictionaries) get indexed but skip symbol extraction.
DIGEST_MAX_LINES = 5000

# Languages we know how to parse for symbol extraction.
LANG_BY_EXT = {
    ".py":   "python",
    ".pyi":  "python",
    ".js":   "javascript",
    ".jsx":  "javascript",
    ".ts":   "typescript",
    ".tsx":  "typescript",
    ".rs":   "rust",
    ".go":   "go",
    ".java": "java",
    ".rb":   "ruby",
    ".lua":  "lua",
    ".sh":   "shell",
    ".bash": "shell",
    ".html": "html",
    ".css":  "css",
    ".md":   "markdown",
    ".txt":  "text",
    ".json": "json",
    ".yml":  "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".ini":  "ini",
    ".sql":  "sql",
}

# Hard cap on a single file before we treat it as binary-ish (skip digest).
MAX_FILE_BYTES = 2_000_000  # 2 MB


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class FileEntry:
    path: str         # relative to project_root (forward slashes)
    size: int
    mtime_ms: float
    lines: int
    lang: str
    sha1_8: str       # first 8 chars of sha1 — for cache invalidation

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SourceIndex:
    project_root: str
    profile_name: str
    mirror_root: str
    indexed_at_ms: float
    file_count: int
    total_bytes: int
    files: list[FileEntry]

    def to_dict(self) -> dict:
        return {**asdict(self),
                "files": [f.to_dict() for f in self.files]}


# ── Walking + filtering ──────────────────────────────────────────────────────

def _should_skip_dir(name: str) -> bool:
    return name in IGNORE_DIRS or name.startswith(".")


def _should_skip_file(name: str) -> bool:
    if name.startswith("."):
        # Allow common dotfiles that ARE source-like.
        if name not in {".gitignore", ".env.example", ".eslintrc",
                        ".prettierrc", ".dockerignore"}:
            return True
    suffix = Path(name).suffix.lower()
    return suffix in IGNORE_SUFFIXES


def _walk_project(root: Path) -> Iterable[Path]:
    root_str = str(root)
    for dirpath, dirnames, filenames in os.walk(root_str):
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
        for fn in filenames:
            if _should_skip_file(fn):
                continue
            yield Path(dirpath) / fn


# ── Indexing ─────────────────────────────────────────────────────────────────

def _file_entry(path: Path, root: Path) -> FileEntry | None:
    try:
        st = path.stat()
    except OSError:
        return None
    if st.st_size > MAX_FILE_BYTES:
        # Still index it but with no symbol extraction downstream.
        pass
    rel = path.relative_to(root).as_posix()
    suffix = path.suffix.lower()
    lang = LANG_BY_EXT.get(suffix, "other")

    # Cheap line count + sha1 prefix in one pass.
    lines = 0
    h = hashlib.sha1()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                lines += chunk.count(b"\n")
                h.update(chunk)
    except OSError:
        return None
    return FileEntry(
        path=rel, size=st.st_size, mtime_ms=st.st_mtime * 1000.0,
        lines=lines, lang=lang, sha1_8=h.hexdigest()[:8],
    )


def build_index(project_root: Path, profile_name: str,
                mirror_root: Path) -> SourceIndex:
    """Walk the project and produce a fresh SourceIndex."""
    files: list[FileEntry] = []
    total = 0
    for p in _walk_project(project_root):
        entry = _file_entry(p, project_root)
        if entry is None:
            continue
        files.append(entry)
        total += entry.size
    files.sort(key=lambda f: f.path)
    return SourceIndex(
        project_root=str(project_root),
        profile_name=profile_name,
        mirror_root=str(mirror_root),
        indexed_at_ms=time.time() * 1000.0,
        file_count=len(files),
        total_bytes=total,
        files=files,
    )


# ── Mirroring (lazy, mtime-driven) ──────────────────────────────────────────

def sync_mirror(index: SourceIndex, project_root: Path,
                mirror_root: Path) -> dict:
    """Copy every indexed file into the mirror_root, preserving relative
    paths. Skips files whose mirrored copy already has a matching mtime.
    Returns counts: {copied, skipped, removed, bytes}."""
    mirror_root.mkdir(parents=True, exist_ok=True)
    copied = skipped = removed = 0
    bytes_copied = 0

    indexed_paths: set[str] = set()
    for f in index.files:
        indexed_paths.add(f.path)
        src = project_root / f.path
        dst = mirror_root / f.path
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        # Skip if mirror copy mtime matches source — fast path.
        try:
            if dst.exists() and abs(dst.stat().st_mtime - (f.mtime_ms / 1000.0)) < 0.001:
                skipped += 1
                continue
        except OSError:
            pass
        try:
            shutil.copy2(src, dst)
            bytes_copied += f.size
            copied += 1
        except OSError:
            pass

    # ── Remove stale mirror files that no longer exist in source ──────────
    # `indexed_paths` is an in-memory set deciding which files on disk get
    # unlinked — code-identical in shape to the sweep that deleted 1,500 real
    # frames. It is benign here only because of where it points: mirror_root is
    # always AV_ROOT/snapshots/<profile>/source_mirror (never the project), every
    # file under it is a byte copy whose original is still in project_root, and
    # the next refresh re-copies it. The cost of being wrong is a re-copy.
    #
    # Still worth a floor, because "the index came back short" has cheap causes
    # (build_index hit a permission error partway, a subtree was briefly
    # unreadable, the project sits on a flaky or partly-mounted volume) and the
    # symptom would be the whole mirror silently emptying. So: an index that
    # believes the project is empty does not get to empty the mirror.
    #
    # Deliberately NOT added: a per-file "is the original really gone from
    # project_root?" check. It reads well but it is wrong here — a file that
    # still exists yet is now excluded by IGNORE_DIRS/IGNORE_SUFFIXES would then
    # never be pruned, and the mirror would grow forever. Deleting a copy whose
    # original exists costs one re-copy; leaking every newly-ignored file does not
    # self-heal.
    mirror_files = []
    for dirpath, _dirnames, filenames in os.walk(mirror_root):
        for fn in filenames:
            mirror_files.append(Path(dirpath) / fn)

    if not indexed_paths and mirror_files:
        return {"copied": copied, "skipped": skipped, "removed": 0,
                "bytes": bytes_copied,
                "prune_refused": len(mirror_files),
                "prune_refused_reason":
                    f"the fresh index lists 0 files while the mirror holds "
                    f"{len(mirror_files)}; that is an indexing failure, not an "
                    f"empty project — keeping the mirror as it is"}

    for full in mirror_files:
        try:
            rel = full.relative_to(mirror_root).as_posix()
        except ValueError:
            continue
        if rel not in indexed_paths:
            try:
                full.unlink()
                removed += 1
            except OSError:
                pass

    return {"copied": copied, "skipped": skipped, "removed": removed,
            "bytes": bytes_copied}


# ── Digest extraction (the token-efficient view for Claude) ─────────────────

_PY_DOCSTRING_RE = re.compile(r'^\s*[ru]?["\']{3}([^"\']*)', re.MULTILINE)
_JS_FUNC_RE = re.compile(
    r"^(?:export\s+(?:default\s+)?)?(?:async\s+)?"
    r"(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
_JS_ARROW_RE = re.compile(
    r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*="
    r"\s*(?:async\s*)?\([^)]*\)\s*=>",
    re.MULTILINE,
)
_RUST_RE = re.compile(
    r"^(?:pub\s+)?(?:async\s+)?(?:fn|struct|enum|trait|impl|mod|const|static)\s+"
    r"([A-Za-z_][\w]*)", re.MULTILINE)
_GO_RE = re.compile(
    r"^(?:func|type|var|const)\s+(?:\([^)]*\)\s+)?([A-Za-z_][\w]*)", re.MULTILINE)


def _digest_python(text: str) -> dict:
    """Use the AST for accuracy on Python — extract def/class signatures
    and the first line of each docstring."""
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return {"error": f"syntax: {e.msg} at line {e.lineno}"}

    # Module docstring
    mod_doc = ast.get_docstring(tree, clean=True) or ""
    mod_doc = mod_doc.split("\n", 1)[0].strip()

    symbols: list[dict] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            sig = f"def {node.name}({', '.join(args)})"
            doc = (ast.get_docstring(node) or "").split("\n", 1)[0].strip()
            symbols.append({"kind": "func", "name": node.name,
                            "line": node.lineno, "sig": sig, "doc": doc})
        elif isinstance(node, ast.ClassDef):
            bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
            sig = (f"class {node.name}"
                   + (f"({', '.join(bases)})" if bases else ""))
            doc = (ast.get_docstring(node) or "").split("\n", 1)[0].strip()
            methods = [n.name for n in node.body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            symbols.append({"kind": "class", "name": node.name,
                            "line": node.lineno, "sig": sig, "doc": doc,
                            "methods": methods})
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            # Skip — too noisy. Imports are visible in the file itself.
            pass
        elif isinstance(node, ast.Assign):
            # Top-level constants: capture name, drop value (value may be huge)
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id.isupper():
                    symbols.append({"kind": "const", "name": tgt.id,
                                    "line": node.lineno})
    return {"docstring": mod_doc, "symbols": symbols}


def _digest_jsts(text: str) -> dict:
    syms: list[dict] = []
    for m in _JS_FUNC_RE.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        syms.append({"kind": "decl", "name": m.group(1), "line": line,
                     "sig": text[m.start():m.start() + 120].split("\n")[0].strip()})
    for m in _JS_ARROW_RE.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        syms.append({"kind": "arrow", "name": m.group(1), "line": line})
    return {"symbols": syms}


def _digest_regex(text: str, pattern: re.Pattern) -> dict:
    syms: list[dict] = []
    for m in pattern.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        syms.append({"name": m.group(1), "line": line,
                     "sig": text[m.start():m.start() + 120].split("\n")[0].strip()})
    return {"symbols": syms}


def file_digest(file_path: Path, lang: str) -> dict:
    """Return a small dict summarizing one file. Tuned for AI consumption:
    enough to know what's in the file, NOT enough to read its logic."""
    try:
        if file_path.stat().st_size > MAX_FILE_BYTES:
            return {"skipped": "too_large"}
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"error": str(e)}

    if text.count("\n") > DIGEST_MAX_LINES:
        return {"skipped": "too_many_lines", "lines": text.count("\n")}

    if lang == "python":
        return _digest_python(text)
    if lang in ("javascript", "typescript"):
        return _digest_jsts(text)
    if lang == "rust":
        return _digest_regex(text, _RUST_RE)
    if lang == "go":
        return _digest_regex(text, _GO_RE)
    if lang == "markdown":
        # First 200 chars + headings
        headings = re.findall(r"^(#{1,6})\s+(.+)$", text, re.MULTILINE)
        return {"preview": text[:200].strip(),
                "headings": [{"level": len(h), "text": t} for h, t in headings[:30]]}
    if lang in ("json", "yaml", "toml", "ini"):
        return {"preview": text[:300].strip()}
    # Unknown — first nonblank line.
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    return {"first_line": first[:200]}


def build_digest(index: SourceIndex, project_root: Path) -> dict:
    """Build the FULL per-file digest — every symbol, every signature.
    Used when Claude needs deep navigation; bigger token cost."""
    files_out: list[dict] = []
    for f in index.files:
        if f.lang in ("other", "text"):
            files_out.append({"path": f.path, "lang": f.lang, "lines": f.lines,
                              "size": f.size})
            continue
        d = file_digest(project_root / f.path, f.lang)
        files_out.append({
            "path": f.path, "lang": f.lang,
            "lines": f.lines, "size": f.size,
            **d,
        })
    return {
        "project_root": index.project_root,
        "profile": index.profile_name,
        "indexed_at_ms": index.indexed_at_ms,
        "file_count": index.file_count,
        "total_bytes": index.total_bytes,
        "files": files_out,
    }


def build_light_digest(index: SourceIndex, project_root: Path) -> dict:
    """Token-frugal version: every file gets ONE line — path, lang, lines,
    plus a 1-line summary (Python module docstring or first nonblank line).

    Target: ~50-150 bytes per file. Designed so Claude can hold the whole
    project map in <5K tokens for projects up to a few hundred files.
    """
    files_out: list[dict] = []
    for f in index.files:
        entry: dict = {"path": f.path, "lang": f.lang, "lines": f.lines}
        if f.lang == "python":
            try:
                src = (project_root / f.path).read_text(
                    encoding="utf-8", errors="replace")
                # Try AST module-docstring; fall back to first nonblank.
                try:
                    doc = ast.get_docstring(ast.parse(src), clean=True)
                except SyntaxError:
                    doc = None
                summary = (doc or "").split("\n", 1)[0].strip()
                if not summary:
                    summary = next((ln.strip() for ln in src.splitlines()
                                    if ln.strip() and not ln.lstrip().startswith("#")), "")
                if summary:
                    entry["summary"] = summary[:140]
                # Top-level symbol counts only — not names.
                try:
                    tree = ast.parse(src)
                    funcs = sum(1 for n in tree.body
                                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
                    classes = sum(1 for n in tree.body if isinstance(n, ast.ClassDef))
                    if funcs or classes:
                        entry["sym"] = {"f": funcs, "c": classes}
                except SyntaxError:
                    pass
            except OSError:
                pass
        elif f.lang == "markdown":
            try:
                first = next((ln.strip() for ln in
                              (project_root / f.path).read_text(
                                  encoding="utf-8", errors="replace").splitlines()
                              if ln.strip()), "")
                if first:
                    entry["summary"] = first[:140]
            except OSError:
                pass
        files_out.append(entry)
    return {
        "project_root": index.project_root,
        "profile": index.profile_name,
        "indexed_at_ms": index.indexed_at_ms,
        "file_count": index.file_count,
        "total_bytes": index.total_bytes,
        "files": files_out,
    }


# ── File-tree view (for the Tree GUI tab) ───────────────────────────────────

def build_tree(index: SourceIndex) -> dict:
    """Hierarchical tree {name, type, children?, size?, lines?} for UI display."""
    root_node = {"name": Path(index.project_root).name, "type": "dir",
                 "path": "", "children": []}
    by_path: dict[str, dict] = {"": root_node}

    def ensure_dir(path: str) -> dict:
        if path in by_path:
            return by_path[path]
        parent_path = str(Path(path).parent.as_posix()) if "/" in path else ""
        if parent_path == ".":
            parent_path = ""
        parent = ensure_dir(parent_path)
        node = {"name": Path(path).name, "type": "dir",
                "path": path, "children": []}
        parent["children"].append(node)
        by_path[path] = node
        return node

    for f in index.files:
        parent_path = str(Path(f.path).parent.as_posix()) if "/" in f.path else ""
        if parent_path == ".":
            parent_path = ""
        parent = ensure_dir(parent_path)
        parent["children"].append({
            "name": Path(f.path).name, "type": "file", "path": f.path,
            "size": f.size, "lines": f.lines, "lang": f.lang,
        })

    # Sort children: dirs first then files, both alpha
    def sort_node(n: dict) -> None:
        if "children" in n:
            n["children"].sort(key=lambda c: (c["type"] != "dir", c["name"]))
            for c in n["children"]:
                sort_node(c)

    sort_node(root_node)
    return root_node


# ── Top-level orchestrator ──────────────────────────────────────────────────

def refresh(project_root: Path, profile_name: str,
            mirror_base: Path, do_mirror: bool = True) -> dict:
    """One call: walk + (optionally) mirror + write index + return summary.

    Files written under `<mirror_base>/<profile>/`:
      - source_index.json   — full SourceIndex (paths, sizes, mtimes)
      - source_digest.json  — Claude-facing token-efficient view
      - source_tree.json    — hierarchical tree for GUI
      - source_mirror/      — copy of every indexed file (if do_mirror)
    """
    profile_dir = mirror_base / profile_name
    mirror_root = profile_dir / "source_mirror"
    profile_dir.mkdir(parents=True, exist_ok=True)

    index = build_index(project_root, profile_name, mirror_root)

    sync = {"copied": 0, "skipped": 0, "removed": 0, "bytes": 0}
    if do_mirror:
        sync = sync_mirror(index, project_root, mirror_root)

    light = build_light_digest(index, project_root)
    digest = build_digest(index, project_root)
    tree = build_tree(index)

    (profile_dir / "source_index.json").write_text(
        json.dumps(index.to_dict(), indent=2))
    (profile_dir / "source_light.json").write_text(
        json.dumps(light, indent=2))
    (profile_dir / "source_digest.json").write_text(
        json.dumps(digest, indent=2))
    (profile_dir / "source_tree.json").write_text(
        json.dumps(tree, indent=2))

    return {
        "ok": True,
        "project_root": str(project_root),
        "profile": profile_name,
        "mirror_root": str(mirror_root),
        "file_count": index.file_count,
        "total_bytes": index.total_bytes,
        "light_bytes": len(json.dumps(light)),
        "digest_bytes": len(json.dumps(digest)),
        "tree_bytes": len(json.dumps(tree)),
        "mirror": sync,
        "indexed_at_ms": index.indexed_at_ms,
    }
