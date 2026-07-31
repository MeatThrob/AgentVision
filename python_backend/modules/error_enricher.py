"""
Module 8: Error Context Enricher
Collects full context around an error: nearby source code, recent commands,
git diff since last known-good commit, recent logs.
"""
import subprocess
from pathlib import Path
from shared.schema.snapshot_schema import ErrorInfo


def enrich_error(error: ErrorInfo, project_root: str = ".") -> ErrorInfo:
    """Augment an ErrorInfo with source context and enriched likely_cause."""
    if error.file and error.line:
        error.stack_trace = _attach_source_context(
            error.file, error.line, project_root, error.stack_trace)
    if not error.likely_cause:
        error.likely_cause = _infer_from_stack(error.stack_trace)
    return error


def _attach_source_context(filename: str, line: int,
                            root: str, existing_trace: str) -> str:
    """Prepend a ±5 line code window to the stack trace."""
    try:
        candidates = list(Path(root).rglob(Path(filename).name))
        if not candidates:
            return existing_trace
        source = candidates[0].read_text(errors="replace").splitlines()
        start  = max(0, line - 6)
        end    = min(len(source), line + 5)
        numbered = "\n".join(
            f"{'>>>' if i + 1 == line else '   '} {i+1:4}: {source[i]}"
            for i in range(start, end))
        header = f"--- Source context: {filename}:{line} ---\n"
        return header + numbered + "\n\n" + existing_trace
    except Exception:
        return existing_trace


def _infer_from_stack(trace: str) -> str:
    t = trace.lower()
    if "assertionerror" in t:
        return "Assertion mismatch — check expected vs actual values."
    if "attributeerror" in t:
        m = _extract_between(trace, "'", "'", last=True)
        return f"Object has no attribute '{m}' — check for typos or missing init." if m else \
               "Object missing attribute — verify class definition."
    if "typeerror" in t:
        if "argument" in t:
            return "Wrong number of arguments — function signature mismatch."
        return "Type mismatch — incompatible types passed to function."
    if "keyerror" in t:
        return "Dictionary key not found — add .get() or validate key existence."
    if "zerodivisionerror" in t:
        return "Division by zero — add guard before division."
    if "recursionerror" in t:
        return "Infinite recursion — check base case in recursive function."
    if "syntaxerror" in t:
        return "Syntax error — invalid Python syntax; check indentation and brackets."
    if "valueerror" in t:
        return "ValueError — invalid value passed; check input validation."
    if "filenotfounderror" in t or "no such file" in t:
        return "File not found — verify path and working directory."
    return "Review stack trace — cause not automatically identified."


def _extract_between(text: str, start: str, end: str, last: bool = False) -> str:
    parts = text.split(start)
    if len(parts) < 2:
        return ""
    segment = parts[-1] if last else parts[1]
    inner   = segment.split(end)
    return inner[0] if inner else ""


def get_recent_terminal_commands(limit: int = 10) -> list[str]:
    """Read recent shell history lines."""
    for history_file in ["~/.zsh_history", "~/.bash_history"]:
        path = Path(history_file).expanduser()
        if path.exists():
            try:
                lines = path.read_text(errors="replace").splitlines()
                # zsh history lines start with ': <timestamp>:0;'
                cleaned = []
                for l in lines[-50:]:
                    if l.startswith(": ") and ";":
                        l = l.split(";", 1)[-1]
                    cleaned.append(l.strip())
                return [l for l in cleaned if l][-limit:]
            except Exception:
                pass
    return []
