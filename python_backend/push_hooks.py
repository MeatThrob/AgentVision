"""
AgentVision — Push Mode hook installer / uninstaller
====================================================

Writes AgentVision's hook entries into a Claude Code settings file, and takes
them back out again cleanly.

WHICH FILE. Hooks live in a DIFFERENT file from MCP servers:
    user scope     ~/.claude/settings.json
    project scope  <project>/.claude/settings.json
`~/.claude.json` (where MCP servers live) is NEVER touched by this module.

THE RULES, because this edits a file the user owns and depends on:
  * Back up (timestamped) before the first write.
  * Merge NON-DESTRUCTIVELY: other people's hooks on the same event are kept,
    other settings keys are untouched, key order is preserved.
  * TAG our entries so uninstall removes exactly ours and nothing else.
  * Re-validate the JSON after writing; on any doubt, restore the backup.
  * Idempotent: installing twice does not duplicate entries.

WIRING (verified against the Claude Code hooks reference):

  event             matcher                    why
  ----------------  -------------------------  ----------------------------------
  SessionStart      startup|resume|clear|      orient at the start of a session
                    compact|fork               — `compact` is the highest-value
                                               one: right after a compaction the
                                               agent has forgotten AgentVision
                                               exists.
  UserPromptSubmit  (none)                     tell it before it answers
  PostToolBatch     (none)                     after a batch of parallel tools
  PostToolUse       Edit|Write|MultiEdit|Bash  "did the change I just made break
                                               the running program?"
  Stop              (none)                     backstop; NOT installed unless
                                               --with-stop-block is passed, and
                                               even then the engine keeps it
                                               disabled unless
                                               AGENTVISION_AMBIENT_STOP_BLOCK=1.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

# Marker used to identify OUR entries on uninstall. It is part of the command
# string itself, so it cannot be lost by a settings-file round trip.
HOOK_SCRIPT_NAME = "agentvision_hook.py"
STATUS_TAG = "AgentVision Push Mode"

# event -> matcher (None means the event takes no matcher)
WIRING: list[tuple[str, str | None]] = [
    ("SessionStart",     "startup|resume|clear|compact|fork"),
    ("UserPromptSubmit", None),
    ("PostToolBatch",    None),
    ("PostToolUse",      "Edit|Write|MultiEdit|Bash"),
]
STOP_WIRING: tuple[str, str | None] = ("Stop", None)

DEFAULT_TIMEOUT_S = 5          # seconds (the hooks reference uses seconds)


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def hook_script_path() -> Path:
    return repo_root() / "tools" / HOOK_SCRIPT_NAME


def settings_path(scope: str = "user", project_dir: str | None = None) -> Path:
    if scope == "project":
        base = Path(project_dir or os.getcwd()).resolve()
        return base / ".claude" / "settings.json"
    return Path.home() / ".claude" / "settings.json"


def state_flag_path() -> Path:
    return repo_root() / "log" / "agentvision_push_mode.json"


def set_enabled(enabled: bool) -> dict:
    """Flip Push Mode without touching the settings file. The hook reads this
    flag first, so the GUI toggle is instant and needs no restart."""
    p = state_flag_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"enabled": bool(enabled),
                             "updated_ms": time.time() * 1000.0}, indent=1))
    return {"ok": True, "enabled": bool(enabled), "flag_file": str(p)}


def get_enabled() -> dict:
    p = state_flag_path()
    try:
        d = json.loads(p.read_text())
        return {"enabled": bool(d.get("enabled", True)), "flag_file": str(p),
                "source": "flag file"}
    except FileNotFoundError:
        return {"enabled": True, "flag_file": str(p),
                "source": "default (no flag file — installing the hook is the opt-in)"}
    except Exception as exc:
        return {"enabled": True, "flag_file": str(p),
                "source": f"default (flag unreadable: {exc})"}


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    txt = path.read_text()
    if not txt.strip():
        return {}
    d = json.loads(txt)          # deliberately NOT caught: refuse to guess
    if not isinstance(d, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return d


def _backup(path: Path) -> str | None:
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(path.name + f".bak.{stamp}")
    shutil.copy2(path, dest)
    return str(dest)


def _is_ours(entry: dict) -> bool:
    """True if a matcher-group entry is one AgentVision installed."""
    for h in entry.get("hooks") or []:
        if HOOK_SCRIPT_NAME in str(h.get("command") or ""):
            return True
    return False


def _our_hook(python_exe: str, timeout_s: int) -> dict:
    return {
        "type": "command",
        "command": f'"{python_exe}" "{hook_script_path()}"',
        "timeout": timeout_s,
        # Documented field, and doubles as a human-visible tag.
        "statusMessage": STATUS_TAG,
    }


def plan(scope: str = "user", project_dir: str | None = None,
         with_stop_block: bool = False) -> dict:
    """What install would do, without doing it."""
    wiring = list(WIRING) + ([STOP_WIRING] if with_stop_block else [])
    return {
        "settings_file": str(settings_path(scope, project_dir)),
        "hook_script": str(hook_script_path()),
        "events": [{"event": e, "matcher": m} for e, m in wiring],
        "stop_block_hook_installed": bool(with_stop_block),
        "note": ("Even with the Stop hook installed, blocking stays OFF unless "
                 "AGENTVISION_AMBIENT_STOP_BLOCK=1 is also set."),
    }


def install(scope: str = "user", project_dir: str | None = None,
            python_exe: str | None = None, timeout_s: int = DEFAULT_TIMEOUT_S,
            with_stop_block: bool = False, dry_run: bool = False) -> dict:
    """Merge AgentVision's hooks into the settings file. Idempotent."""
    path = settings_path(scope, project_dir)
    script = hook_script_path()
    if not script.exists():
        return {"ok": False, "error": f"hook script missing: {script}"}
    python_exe = python_exe or "python3"

    try:
        data = _load(path)
    except Exception as exc:
        return {"ok": False, "error": f"could not parse {path}: {exc}",
                "hint": "fix or move that file first — refusing to overwrite it"}

    wiring = list(WIRING) + ([STOP_WIRING] if with_stop_block else [])
    if dry_run:
        return {"ok": True, "dry_run": True,
                **plan(scope, project_dir, with_stop_block)}

    backup = _backup(path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return {"ok": False, "error": "settings 'hooks' is not an object"}

    added, refreshed = [], []
    for event, matcher in wiring:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            return {"ok": False, "error": f"settings hooks.{event} is not a list"}
        mine = [g for g in groups if isinstance(g, dict) and _is_ours(g)]
        if mine:
            # Refresh in place so an upgrade fixes paths without duplicating.
            for g in mine:
                g["hooks"] = [_our_hook(python_exe, timeout_s)]
                if matcher is not None:
                    g["matcher"] = matcher
                else:
                    g.pop("matcher", None)
            refreshed.append(event)
        else:
            entry: dict = {}
            if matcher is not None:
                entry["matcher"] = matcher
            entry["hooks"] = [_our_hook(python_exe, timeout_s)]
            groups.append(entry)
            added.append(event)

    out = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    try:
        json.loads(out)                        # validate before writing
    except Exception as exc:
        return {"ok": False, "error": f"refused to write invalid JSON: {exc}",
                "backup": backup}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(out)
    try:
        _load(path)                            # validate after writing
    except Exception as exc:
        if backup:
            shutil.copy2(backup, path)
        return {"ok": False, "error": f"post-write validation failed: {exc}",
                "restored_from": backup}

    set_enabled(True)
    return {
        "ok": True, "settings_file": str(path), "backup": backup,
        "events_added": added, "events_refreshed": refreshed,
        "hook_command": _our_hook(python_exe, timeout_s)["command"],
        "stop_block_hook_installed": bool(with_stop_block),
        "stop_block_active": os.environ.get("AGENTVISION_AMBIENT_STOP_BLOCK", "0")
                             not in ("0", "", "false"),
        "RESTART_REQUIRED": ("Claude Code reads hooks at startup — RESTART Claude "
                             "Code for these hooks to take effect."),
        "disable_without_uninstalling": [
            "GUI: the Push Mode toggle",
            "CLI: agentvision push-mode off",
            f"file: set enabled=false in {state_flag_path()}",
            "env: AGENTVISION_PUSH_MODE=0",
        ],
    }


def uninstall(scope: str = "user", project_dir: str | None = None) -> dict:
    """Remove exactly AgentVision's entries, leaving everything else intact."""
    path = settings_path(scope, project_dir)
    if not path.exists():
        return {"ok": True, "settings_file": str(path), "removed": [],
                "note": "settings file does not exist — nothing to remove"}
    try:
        data = _load(path)
    except Exception as exc:
        return {"ok": False, "error": f"could not parse {path}: {exc}"}

    backup = _backup(path)
    hooks = data.get("hooks")
    removed: list[str] = []
    if isinstance(hooks, dict):
        for event in list(hooks.keys()):
            groups = hooks.get(event)
            if not isinstance(groups, list):
                continue
            keep = [g for g in groups
                    if not (isinstance(g, dict) and _is_ours(g))]
            if len(keep) != len(groups):
                removed.append(event)
            if keep:
                hooks[event] = keep
            else:
                # Only drop the event key if WE were the sole occupant.
                hooks.pop(event, None)
        if not hooks:
            data["hooks"] = {}

    out = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    try:
        json.loads(out)
    except Exception as exc:
        return {"ok": False, "error": f"refused to write invalid JSON: {exc}",
                "backup": backup}
    path.write_text(out)
    return {"ok": True, "settings_file": str(path), "backup": backup,
            "events_cleaned": removed,
            "other_hooks_preserved": True,
            "RESTART_REQUIRED": "Restart Claude Code to stop running the hooks."}


def status(scope: str = "user", project_dir: str | None = None) -> dict:
    """Is Push Mode installed, and is it currently on?"""
    path = settings_path(scope, project_dir)
    installed: list[dict] = []
    parse_error = None
    try:
        data = _load(path)
        hooks = data.get("hooks") or {}
        if isinstance(hooks, dict):
            for event, groups in hooks.items():
                if not isinstance(groups, list):
                    continue
                for g in groups:
                    if isinstance(g, dict) and _is_ours(g):
                        installed.append({"event": event,
                                          "matcher": g.get("matcher")})
    except FileNotFoundError:
        pass
    except Exception as exc:
        parse_error = str(exc)

    flag = get_enabled()
    return {
        "settings_file": str(path),
        "settings_exists": path.exists(),
        "settings_parse_error": parse_error,
        "installed": bool(installed),
        "installed_events": installed,
        "push_mode_enabled": flag["enabled"],
        "enable_source": flag["source"],
        "flag_file": flag["flag_file"],
        "hook_script": str(hook_script_path()),
        "hook_script_exists": hook_script_path().exists(),
        "stop_block_active": os.environ.get("AGENTVISION_AMBIENT_STOP_BLOCK", "0")
                             not in ("0", "", "false"),
    }
