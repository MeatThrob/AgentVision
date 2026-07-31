"""
Module 6: Structured UI Recorder
Captures semantic UI state via AppleScript + Accessibility APIs.
Produces a JSON-serialisable dict that AI can parse without vision.
"""
import subprocess
import json
# UIInfo removed from schema — ui_recorder returns plain dicts


def capture_ui_structure() -> dict:
    """
    Returns structured UI info:
    {
      "focused_app": "Visual Studio Code",
      "window_title": "agent.py — project",
      "open_tabs": ["main.py", "agent.py"],
      "menu_bar_items": ["File", "Edit", ...],
      "focused_element": "editor",
      "terminal_visible": true,
      "terminal_last_line": "pytest failed",
      "notifications": []
    }
    """
    result = {
        "focused_app": "",
        "window_title": "",
        "open_tabs": [],
        "menu_bar_items": [],
        "focused_element": "",
        "terminal_visible": False,
        "terminal_last_line": "",
        "notifications": [],
    }

    try:
        from utils import platform_shim
    except Exception:
        try:
            from python_backend.utils import platform_shim
        except Exception:
            platform_shim = None

    # Frontmost app + window title — cross-platform via platform_shim
    # (AppleScript on macOS, Win32 GetForegroundWindow on Windows).
    if platform_shim is not None:
        try:
            result["focused_app"]  = platform_shim.frontmost_app()
            result["window_title"] = platform_shim.front_window_title()
        except Exception:
            pass

    # Terminal last-line scraping was macOS-only (AppleScript into
    # Terminal.app / iTerm2). There is no portable equivalent on Windows, so
    # this field is left empty there — the bridge's own tailing of the target
    # program's log_file is the cross-platform way to see recent output.
    if platform_shim is not None and getattr(platform_shim, "IS_MAC", False):
        for term_app in ["Terminal", "iTerm2", "iTerm"]:
            script = f'''
            tell application "{term_app}"
                try
                    set txt to contents of selected tab of front window
                    set lastLine to last paragraph of txt
                    return lastLine
                end try
            end tell
            '''
            try:
                r = subprocess.run(["osascript", "-e", script],
                                   capture_output=True, text=True, timeout=2)
                line = r.stdout.strip()
                if line:
                    result["terminal_visible"]   = True
                    result["terminal_last_line"] = line[-200:]
                    break
            except Exception:
                continue

    return result


def ui_to_overlay_lines(ui: dict) -> list[str]:
    lines = []
    if ui.get("focused_app"):
        lines.append(f"App: {ui['focused_app']}")
    if ui.get("window_title"):
        lines.append(f"Win: {ui['window_title'][:50]}")
    if ui.get("terminal_last_line"):
        lines.append(f"Term: {ui['terminal_last_line'][:60]}")
    return lines
