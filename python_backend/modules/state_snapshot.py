"""
Module 3: State Snapshot System
Captures full machine + editor state at the moment of a frame.
"""
import os
import sys
import platform
import subprocess
from pathlib import Path
from shared.schema.snapshot_schema import EnvInfo


# Keys safe to expose in snapshots (exclude tokens, passwords, keys)
_SAFE_ENV_PREFIXES = (
    "PATH", "HOME", "USER", "SHELL", "LANG", "TERM",
    "PYTHONPATH", "VIRTUAL_ENV", "CONDA_", "NODE_", "NVM_",
)


def collect_env(project_root: str = ".") -> EnvInfo:
    info = EnvInfo()
    info.python_version = sys.version.split()[0]
    info.os_version     = platform.platform()
    # SHELL on POSIX; COMSPEC (cmd.exe) is the closest analogue on Windows.
    info.shell          = os.environ.get("SHELL") or os.environ.get("COMSPEC", "")
    info.cwd            = os.getcwd()

    # Active git branch
    try:
        r = subprocess.run(["git", "-C", project_root, "rev-parse",
                            "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=3)
        info.active_branch = r.stdout.strip()
    except Exception:
        info.active_branch = ""

    # Safe env vars
    info.env_vars = {
        k: v for k, v in os.environ.items()
        if any(k.startswith(p) for p in _SAFE_ENV_PREFIXES)
    }

    # pip packages (names only, no versions for brevity)
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "list", "--format=columns"],
                           capture_output=True, text=True, timeout=5)
        info.pip_packages = [
            line.split()[0] for line in r.stdout.splitlines()[2:]
            if line.strip()
        ]
    except Exception:
        info.pip_packages = []

    # Docker
    try:
        r = subprocess.run(["docker", "ps", "-q"],
                           capture_output=True, text=True, timeout=2)
        info.docker_running = bool(r.stdout.strip())
    except Exception:
        info.docker_running = False

    return info


def collect_ui_state(project_root: str = ".") -> dict:
    """
    Collect editor/UI state (frontmost app + newest project file).
    Cross-platform via utils.platform_shim (AppleScript on macOS, Win32
    GetForegroundWindow on Windows). Gracefully degrades on failure.
    """
    info = {"focused_app": "", "focused_file": ""}

    # Frontmost app
    try:
        from utils.platform_shim import frontmost_app
        info["focused_app"] = frontmost_app()
    except Exception:
        try:
            from python_backend.utils.platform_shim import frontmost_app
            info["focused_app"] = frontmost_app()
        except Exception:
            pass

    # Recent file in project (fallback: newest modified .py)
    try:
        py_files = sorted(
            Path(project_root).rglob("*.py"),
            key=lambda p: p.stat().st_mtime,
            reverse=True)
        if py_files:
            info["focused_file"] = str(py_files[0].relative_to(project_root))
    except Exception:
        pass

    return info
