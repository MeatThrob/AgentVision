# AgentVision on Linux (Artix / Arch — systemd-free friendly)

Linux is a first-class AgentVision platform: session-aware screen capture
(X11 **and** Wayland), EWMH window enumeration, an evdev input daemon, and a
self-confirming `doctor`. Nothing here assumes systemd — service templates are
provided for **OpenRC**, **runit**, and **dinit** (the three Artix init
choices). None of them are required: `python3 -m python_backend.cli` and the
GUI work exactly as on macOS/Windows.

## 1. Install

```sh
# Python deps
python3 -m pip install -r requirements-linux.txt

# System packages (pacman names — pick what matches your session)
sudo pacman -S --needed ttf-dejavu tk            # fonts + GUI (GUI optional)
# Wayland (Sway/Hyprland/labwc):
sudo pacman -S --needed grim                     # native capture
sudo pacman -S --needed xdg-desktop-portal xdg-desktop-portal-wlr   # portal fallback
# X11 (only if you skip the python `mss` package):
sudo pacman -S --needed maim                     # or scrot / imagemagick
# X11 window tools (fallbacks when python-xlib is absent; xdotool also
# anchors the input daemon's pointer position):
sudo pacman -S --needed xdotool wmctrl

# Input recording: /dev/input needs the `input` group (re-login afterwards)
sudo usermod -aG input $USER
```

Then confirm ON THIS MACHINE — this is the whole point:

```sh
cd /path/to/AgentVision_v5
python3 -m python_backend.cli doctor
```

The doctor reports JSON health checks: `linux_session` (X11/Wayland/headless +
the capture chain it picked), `capture` (a real non-blank grab), `window_enum`
(X11: enumerated windows; Wayland: `ok: null` — impossible by design),
`linux_input_evdev` (devices readable + physical/synthetic classification +,
when `/dev/uinput` is writable, a uinput→evdev injection round-trip proof),
and `emitter_roundtrip`. `"ok": true` at the top = everything usable works.

Install the `agentvision` shim on PATH (plain POSIX bash — works on any init):

```sh
python3 -m python_backend.cli install --dir ~/bin
```

## 2. What Linux can and cannot do (by design)

| Capability            | X11                            | Wayland                                   |
|-----------------------|--------------------------------|-------------------------------------------|
| Full/region capture   | mss → maim → scrot → import    | grim → gnome-screenshot/spectacle → portal |
| Per-window capture    | region grab of window bounds (occlusion-sensitive, like Windows) | **not possible** (compositor security) — falls back to region/full-screen and the bridge surfaces a window-missing warning |
| Window enumeration    | EWMH via python-xlib → wmctrl → xdotool | **not possible** — `find_window` returns `None` gracefully |
| Frontmost app/title   | `_NET_ACTIVE_WINDOW`           | best-effort/empty                          |
| Input recording       | evdev (`/dev/input/event*`), needs `input` group | same (evdev is below the display server)  |

**Wayland tip:** since per-window capture is impossible, set a capture **crop**
(`x,y,w,h`) in the profile for app-scoped frames.

**Physical vs synthetic input (Linux specifics):** macOS tags every event with
the injector PID and Windows with an injected flag; the Linux kernel does not
tag events, so the split is **per device**: real HID hardware = physical, a
uinput-created virtual device (ydotool, python-uinput, evemu) = synthetic.
Two documented consequences: the injector PID is never known
(`source_pid: -1`), and injection that bypasses `/dev/input` entirely — X11
XTEST (xdotool's default mode) or Wayland virtual-pointer protocols — is
invisible to the daemon. Use a uinput-based injector (e.g. ydotool) when you
need the bot's inputs recorded.

## 3. Init service templates (optional)

Templates live here for the bridge (`agentvision-bridge`) and the input daemon
(`agentvision-inputd`), for each init system:

```
dist/linux/openrc/agentvision-bridge      → /etc/init.d/
dist/linux/openrc/agentvision-inputd     → /etc/init.d/
dist/linux/runit/agentvision-bridge/run   → /etc/runit/sv/agentvision-bridge/
dist/linux/runit/agentvision-inputd/run   → /etc/runit/sv/agentvision-inputd/
dist/linux/dinit/agentvision-bridge       → /etc/dinit.d/
dist/linux/dinit/agentvision-inputd       → /etc/dinit.d/
```

**Edit the variables at the top of each file first** (`AV_ROOT`, `AV_PYTHON`,
`AV_USER`, port). Enable with:

```sh
# OpenRC
sudo cp dist/linux/openrc/agentvision-bridge /etc/init.d/ && sudo chmod +x /etc/init.d/agentvision-bridge
sudo rc-update add agentvision-bridge default && sudo rc-service agentvision-bridge start

# runit (Artix-runit)
sudo cp -r dist/linux/runit/agentvision-bridge /etc/runit/sv/
sudo ln -s /etc/runit/sv/agentvision-bridge /run/runit/service/

# dinit (Artix-dinit)
sudo cp dist/linux/dinit/agentvision-bridge /etc/dinit.d/
sudo dinitctl enable agentvision-bridge
```

**Important caveat for system-level services:** screen capture needs the
desktop session's environment (`DISPLAY`/`XAUTHORITY` on X11,
`WAYLAND_DISPLAY`/`XDG_RUNTIME_DIR` on Wayland). A service started at boot —
before/outside your graphical session — will run the bridge fine (HTTP, logs,
JSONL) but `doctor`/capture will report `headless` until those variables reach
it. The templates contain commented `env` lines to pin them for the common
single-seat case (X11 `DISPLAY=:0`; Wayland `WAYLAND_DISPLAY=wayland-1` +
`XDG_RUNTIME_DIR=/run/user/<uid>`). The zero-fuss alternative: start the
bridge from your session autostart (compositor config / `~/.xinitrc`) instead
of system init — everything inherits correctly there. The `agentvision-inputd`
daemon has no display dependency (evdev is below the display server), only the
`input`-group requirement, so it is the more natural one to run from init.

## 4. Files the port touches

Everything OS-specific stays behind `python_backend/utils/platform_shim.py`
(capture, window enum, session detection) and
`python_backend/daemon/input_daemon.py` (`LinuxInputBackend`). The bridge,
GUI, CLI and schema are OS-agnostic. Mock tests that prove the Linux logic on
any OS: `python_backend/utils/test_linux_platform.py` (part of
`run_all_tests.py`).
