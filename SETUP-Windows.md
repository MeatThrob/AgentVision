# AgentVision — Windows Setup (5 minutes)

For the full "what it is / how it works" explanation, see **`HOW_IT_WORKS.md`**.

---

## 1. Install Python 3.11+

Download from <https://python.org/downloads>. In the installer, **tick
"Add python.exe to PATH."**

Verify in a new Command Prompt:
```bat
py -3 --version
```

---

## 2. Unzip AgentVision

Right-click `AgentVision_windows.zip` → **Extract All…** → pick a folder such as
`C:\Users\<you>\AgentVision`.

---

## 3. Install dependencies

Double-click **`install-dependencies.bat`**.

It installs everything AgentVision needs:
- `flask` — the bridge server (internal API on `127.0.0.1:7771`)
- `mss` — fast screen/region capture
- `pywin32` — window enumeration + foreground window
- `pillow` — screenshot processing + overlay rendering
- `psutil` — process monitoring
- `requests` — GUI ↔ bridge communication
- `pytest` + `pytest-json-report` — test-runner integration

(Tkinter, the GUI framework, ships with Python — no install.)

---

## 4. Run AgentVision

Double-click **`Start AgentVision.bat`**. The control-panel window opens.

*(No GUI? Use `Start Bridge (headless).bat` to run just the capture server.)*

---

## 5. Connect a program (bridge setup)

1. **Profile tab** → set **Capture App** to the window you want to watch. Click
   **Select App** to pick from a list of open windows, or type a title
   substring / process name (e.g. `Notepad`, `notepad.exe`). A ready-made
   GUI's **Profile** tab lets you point it at any program you already have running — Notepad is a good first target (capture_app `Notepad`).
2. *(Optional)* set **log_file** / **action_log_file** to your program's logs so
   frames correlate with output.
3. Click **Set Active**.
4. Click **Start Bridge** — capture begins on `http://127.0.0.1:7771`.

You'll see live frames in the GUI. Try the built-in demo: open **Notepad**,
create a profile for a running program, Set Active, Start Bridge — the captured
frames show the Notepad window.

---

## 6. Using it with Claude (optional)

AgentVision works alongside **Claude Code**. Install it:
```bat
npm install -g @anthropic-ai/claude-code
```

The MCP bridge needs the `mcp` package (it is optional, so not in
`requirements-windows.txt` — install it only if you use Claude Code):
```bat
py -3 -m pip install "mcp>=2.0,<3"
```

Register the MCP server with Claude Code. `claude_mcp.py` imports only the
standard library plus `mcp`, so point Claude Code straight at the script file
(no `cwd` needed):
```bat
claude mcp add agentvision -- python C:\Users\<you>\AgentVision\python_backend\api\claude_mcp.py
```

With the bridge running, run Claude Code from your project folder — the `av_*`
tools feed it live screenshots, logs, and events so it can observe and debug
your program in real time. Claude should call `av_status` first, then
`av_latest_frame`.

---

## Windows notes

- **No screen-capture permission is required.** Unlike macOS, Windows lets a
  normal user process capture the desktop and install global input hooks.
- **Anti-cheat games** may block third-party capture of their window — use
  windowed/borderless mode for those. Everything else works out of the box.
- **Command-line use:** `py -3 -m python_backend.cli status` (also `attach`,
  `run`, `daemon start|stop|status`, `install`).

---

## Diagnosing a PC that crashes / reboots / powers off on its own

This is a separate tool from the debugger above, and it does **not** need the
MCP server, a project, or screen capture — only `psutil`. It records
machine-wide hardware telemetry (temps, fan RPM, voltage rails, watts, CPU
load) to a crash-proof log and, after the machine comes back, tells you whether
the last crash looks like **overheating**, a **failing power supply**, a **CPU
machine-check**, a **driver bluescreen**, or **RAM** — with the evidence and
what to try. Full guide: [`docs/HARDWARE_BLACKBOX.md`](docs/HARDWARE_BLACKBOX.md).

Quick start on the machine that crashes:

1. `py -3 -m pip install psutil`
2. **Recommended:** install [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
   and turn on *Options → Remote Web Server → Run*. Windows exposes almost no
   sensors on its own; this is what gives the recorder temperatures, fan RPM
   and **voltage rails** (without them it can't tell a thermal shutdown from a
   PSU dropout). Run it as administrator so it can read every sensor.
3. Record from now on (leave it running; it survives a hard power cut because
   it fsyncs every sample):
   ```bat
   py -3 -m python_backend.modules.hw_blackbox --run
   ```
   To have it start automatically at logon, see the `schtasks` recipe in the
   guide.
4. After the next crash, read the verdict:
   ```bat
   py -3 -m python_backend.modules.hw_blackbox --report
   ```
   `--inventory` shows what your machine's sensors can and cannot report before
   you rely on it.
