"""
Module 4: Automated Diff Analyzer
Computes git diff since last snapshot and produces a human-readable summary.
"""
import subprocess
from pathlib import Path
from shared.schema.snapshot_schema import GitInfo


def collect_git_info(project_root: str = ".") -> GitInfo:
    info = GitInfo()
    root = Path(project_root)

    # Branch
    info.branch = _git(root, ["rev-parse", "--abbrev-ref", "HEAD"])

    # Short commit hash
    info.commit_hash = _git(root, ["rev-parse", "--short", "HEAD"])

    # Files changed (unstaged)
    unstaged = _git(root, ["diff", "--name-only"])
    info.files_changed = [f for f in unstaged.splitlines() if f.strip()]
    info.unstaged_count = len(info.files_changed)

    # Staged
    staged = _git(root, ["diff", "--cached", "--name-only"])
    info.staged_count = len([f for f in staged.splitlines() if f.strip()])

    # Short diff summary (stat)
    stat = _git(root, ["diff", "--stat"])
    if stat:
        # Trim to last 2 lines (summary line)
        lines = [l for l in stat.splitlines() if l.strip()]
        info.diff_summary = "\n".join(lines[-3:]) if lines else ""

    return info


def get_full_diff(project_root: str = ".", max_lines: int = 200) -> str:
    """Full unified diff for writing to .diff sidecar file."""
    raw = _git(Path(project_root), ["diff"])
    lines = raw.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... (+{len(lines)-max_lines} lines truncated)"]
    return "\n".join(lines)


def diff_summary_overlay(info: GitInfo) -> list[str]:
    """Returns overlay lines for the screenshot."""
    if not info.files_changed and info.staged_count == 0:
        return ["Git: clean"]
    lines = [f"Git [{info.branch}] {info.commit_hash}"]
    if info.unstaged_count:
        lines.append(f"  Unstaged: {info.unstaged_count} file(s)")
    if info.staged_count:
        lines.append(f"  Staged:   {info.staged_count} file(s)")
    for f in info.files_changed[:4]:
        lines.append(f"  ~ {f}")
    if len(info.files_changed) > 4:
        lines.append(f"  …+{len(info.files_changed)-4} more")
    return lines


def _git(root: Path, args: list[str]) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(root)] + args,
            capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except Exception:
        return ""
