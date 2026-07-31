"""
Module 12: Environment Reproducibility Snapshot
Captures everything needed to recreate a failure environment exactly.
Saved as a sidecar .env.json alongside each snapshot.
"""
import sys
import os
import platform
import subprocess
import json
import hashlib
from pathlib import Path
from datetime import datetime


def capture_full_env(project_root: str = ".") -> dict:
    """
    Returns a dict that fully describes the environment.
    Save this to reproduce any bug exactly.
    """
    snap: dict = {
        "captured_at": datetime.now().isoformat(),
        "system": _system_info(),
        "python": _python_info(),
        "git": _git_info(project_root),
        "dependencies": _dependencies(),
        "filesystem": _filesystem_snapshot(project_root),
        "docker": _docker_info(),
        "env_vars": _safe_env_vars(),
    }
    return snap


def _system_info() -> dict:
    return {
        "os":        platform.system(),
        "os_version": platform.version(),
        "platform":  platform.platform(),
        "machine":   platform.machine(),
        "processor": platform.processor(),
        "hostname":  platform.node(),
    }


def _python_info() -> dict:
    return {
        "version":    sys.version,
        "executable": sys.executable,
        "prefix":     sys.prefix,
        "path":       sys.path[:5],
    }


def _git_info(root: str) -> dict:
    def g(args):
        try:
            r = subprocess.run(["git", "-C", root] + args,
                               capture_output=True, text=True, timeout=5)
            return r.stdout.strip()
        except Exception:
            return ""
    return {
        "commit":   g(["rev-parse", "HEAD"]),
        "branch":   g(["rev-parse", "--abbrev-ref", "HEAD"]),
        "remotes":  g(["remote", "-v"]),
        "status":   g(["status", "--short"]),
        "stash":    g(["stash", "list"]),
    }


def _dependencies() -> dict:
    deps: dict = {}
    # pip
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "list", "--format=json"],
                           capture_output=True, text=True, timeout=10)
        packages = json.loads(r.stdout)
        deps["pip"] = {p["name"]: p["version"] for p in packages}
    except Exception:
        deps["pip"] = {}
    # node / npm
    try:
        r = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=3)
        deps["node"] = r.stdout.strip()
    except Exception:
        deps["node"] = ""
    return deps


def _filesystem_snapshot(root: str) -> dict:
    snap: dict = {"root": root, "files": {}}
    try:
        for p in Path(root).rglob("*.py"):
            if any(x in str(p) for x in ["__pycache__", ".git", "venv"]):
                continue
            content = p.read_bytes()
            snap["files"][str(p.relative_to(root))] = {
                "size": len(content),
                "md5":  hashlib.md5(content).hexdigest(),
                "mtime": p.stat().st_mtime,
            }
    except Exception:
        pass
    return snap


def _docker_info() -> dict:
    try:
        r = subprocess.run(["docker", "ps", "--format", "json"],
                           capture_output=True, text=True, timeout=3)
        containers = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
        return {"running_containers": containers}
    except Exception:
        return {"running_containers": []}


_SAFE_PREFIXES = ("PATH", "HOME", "USER", "SHELL", "LANG", "TERM",
                  "PYTHONPATH", "VIRTUAL_ENV", "CONDA_", "NODE_")

def _safe_env_vars() -> dict:
    return {k: v for k, v in os.environ.items()
            if any(k.startswith(p) for p in _SAFE_PREFIXES)}


def save_env_snapshot(project_root: str, output_path: str):
    data = capture_full_env(project_root)
    Path(output_path).write_text(json.dumps(data, indent=2, default=str))
