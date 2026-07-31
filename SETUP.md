# AgentVision — Setup Guide (macOS)

> **On Windows?** Use **`SETUP-Windows.md`** and the `.bat` launchers instead —
> this file is the macOS guide. Both OSes run from the same source tree.

These steps get you running on macOS in under 5 minutes.

---

## 1. Install Python 3.11+

Download from https://python.org/downloads — pick the latest macOS installer.

Verify in Terminal:
```bash
python3 --version
```

---

## 2. Unzip AgentVision

Move `AgentVision_mac.zip` to wherever you want the project to live, then double-click to unzip it (or in Terminal):

```bash
unzip AgentVision_mac.zip
cd AgentVision
```

---

## 3. Install Dependencies

In Terminal, from inside the `AgentVision/` folder:

```bash
pip3 install -r requirements.txt
pip3 install requests
```

That installs everything needed:
- `flask` — bridge server (AgentVision's internal API)
- `pillow` — screenshot processing and overlay rendering
- `psutil` — process monitoring
- `requests` — GUI ↔ bridge communication
- `pytest` + `pytest-json-report` — test runner integration

Tkinter (the GUI framework) comes built into Python — no install needed.

---

## 4. Run AgentVision

```bash
python3 python_backend/gui/agent_vision_gui.py
```

The AgentVision control panel window will open.

---

## 5. Connect a Program (Bridge Setup)

1. In the **Profile** tab, paste or browse to your program's project folder in the Drop Zone
2. Click **Auto-Fill** to detect paths automatically
3. Click **↻ Re-Install + Verify** to install the diagnostic hooks into that program
4. Click **▶ Set Active** to make it the active profile
5. Click **▶ Start Bridge** to begin capturing — the bridge runs on `http://127.0.0.1:7771`

---

## 6. Using with Claude

AgentVision is designed to work alongside Claude Code (the Claude CLI tool).

Install Claude Code:
```bash
npm install -g @anthropic-ai/claude-code
```

Then set your Anthropic API key:
```bash
export ANTHROPIC_API_KEY="your-key-here"
```

Get a key at: https://console.anthropic.com

### Register the MCP server — without this you get NO `av_*` tools

This step was missing from earlier versions of this guide, and skipping it is
silent: the bridge runs, the GUI works, and Claude simply has no AgentVision
tools. Install the `mcp` package and register the server:

```bash
pip install mcp
```

```bash
claude mcp add agentvision -- python3 "$(pwd)/python_backend/api/claude_mcp.py"
```

Run that from the AgentVision folder so `$(pwd)` resolves to it. `claude_mcp.py`
imports only the standard library plus `mcp`, so Claude Code can be pointed
straight at the script file — no working directory needed.

Verify it registered:
```bash
claude mcp list
```

Then, inside Claude Code, the first call should be `av_start_here()` — it reports
the target program, whether the bridge is built, and the exact next call.

### Grant Screen Recording permission

macOS blocks window capture until you allow it, and the failure looks like
success: capture "runs" but every frame is empty or shows only the desktop
wallpaper. This is the single most common macOS problem.

**System Settings → Privacy & Security → Screen & System Audio Recording** →
enable the app you launch AgentVision from (Terminal, iTerm, or your IDE).

You must fully **quit and reopen** that app afterwards — macOS only re-reads the
permission at launch. Check it worked with:
```bash
python3 -m python_backend.cli doctor
```
which reports the permission state rather than making you infer it from blank
frames. (There is no `agentvision` command on your PATH unless you make one — the
CLI is invoked as `python3 -m python_backend.cli <subcommand>` from the
AgentVision folder.)

Run Claude Code from your project folder — AgentVision feeds it live screenshots, logs, and structured events so Claude can observe and debug your program in real time.

---

## Folder Structure

```
AgentVision/
├── python_backend/
│   ├── gui/               ← Main GUI (agent_vision_gui.py — run this)
│   ├── api/               ← Bridge server + Claude MCP tools
│   ├── utils/             ← Screenshot, overlay, checkpoint utilities
│   └── shared/            ← Shared schema and data models
├── agent_bootstrap/       ← Auto-instrumentation hooks installed into connected programs
├── requirements.txt       ← Python dependencies
└── SETUP.md               ← This file
```

---

## Troubleshooting

**GUI doesn't open / tkinter error**
→ Reinstall Python from python.org (the Homebrew version sometimes ships without tkinter)

**`ModuleNotFoundError: No module named 'requests'`**
→ Run `pip3 install requests`

**Bridge won't start / port 7771 already in use**
→ Kill any leftover bridge process: `lsof -ti:7771 | xargs kill -9`

**Screenshots going to wrong folder**
→ Make sure the profile has `project_root` set to the correct folder, then click Re-Install + Verify
