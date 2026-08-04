"""
Universal Program Connector
----------------------------
Reads runtime data from ANY connected Python program.
Each connector is defined by a ProgramProfile stored in profiles.json.
The active profile is selected by the user in the AgentVision GUI.
"""

from __future__ import annotations
import json
import os
import re
import glob
import subprocess
import psutil
from pathlib import Path
from dataclasses import dataclass, asdict, field


# ── Profile definition ────────────────────────────────────────────────────────

@dataclass
class ProgramProfile:
    name: str = "custom"
    display_name: str = "Custom Program"
    log_file: str = ""
    stats_folder: str = ""
    screenshots_folder: str = ""
    config_folder: str = ""
    state_file: str = ""
    project_root: str = ""
    process_name: str = "python3"
    python_exe: str = "python3"
    test_dir: str = ""
    notes: str = ""
    # ── Screen / crop settings ──────────────────────────────────────────────
    capture_app: str = ""          # App name to capture window of (e.g. "chiaki-ng")
    capture_crop: str = ""         # Custom crop as "x,y,w,h" in screen pixels, or ""
    # ── Diagnostic action log ───────────────────────────────────────────────
    action_log_file: str = ""      # Path to structured JSONL action log (log/actions.jsonl)
    # ── Universal multi-log support ─────────────────────────────────────────
    # AgentVision can watch N logs at once, each in a DIFFERENT format/language,
    # all merged onto the one time-aligned JSON timeline. Each entry is a dict:
    #   {"path": "<file>", "adapter": "auto"|"jsonl"|"log4j"|..., "label": "app"}
    # "adapter":"auto" auto-detects the format via connectors.log_adapters. This
    # is IN ADDITION to the legacy log_file (text) and action_log_file (JSONL)
    # fields above, which are folded in automatically — so old profiles keep
    # working and new ones can declare many heterogeneous sources.
    log_sources: list = field(default_factory=list)
    # Declared/auto-detected primary language of the target (informational; set
    # by `agentvision attach` language sniffing). Purely a hint for humans/AI.
    language: str = ""
    # ── User-input capture opt-in ───────────────────────────────────────────
    # When True, the system-wide input recorder daemon writes every keypress
    # and mouse event into THIS profile's actions.jsonl. Default False so
    # the user's physical keyboard/mouse never pollutes diagnostic data
    # for programs that drive their own input (bots, automation, etc.).
    # Only flip on for profiles where the user IS the agent (manual play,
    # RPA recording, demo capture, etc.).
    capture_user_input: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ProgramProfile":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ── Built-in profiles ─────────────────────────────────────────────────────────

BUILTIN_PROFILES: dict[str, ProgramProfile] = {
    # Cleared 2026-07-31 at the owner's request: no saved programs ship with
    # AgentVision. It previously hardcoded two of HIS OWN projects (a D2R bot and
    # a PS5 emulator) complete with absolute paths under ~ — fine as a
    # personal tool, wrong for something about to be published, and they came
    # BACK on every load no matter how many times profiles.json was cleared,
    # because load_profiles() re-supplies this dict.
    #
    # "custom" stays, and is not a project. It is the neutral empty placeholder
    # the code falls back to when nothing is configured (bridge_server returns
    # "custom" as the default active profile, and reads
    # profiles.get("custom", ProgramProfile())), so removing it would break the
    # no-program-yet path rather than clean anything up. Add real programs with
    # av_create_profile / the GUI; none are built in.
    "custom": ProgramProfile(
        name="custom",
        display_name="Custom Program",
    ),
}


def resolve_action_log_path(profile) -> str:
    """The profile's structured JSONL event log — ONE resolution rule for every
    consumer.

    Explicit `action_log_file` wins (older profiles and per-frame pins depend
    on it). Otherwise the JSONL source the bridge installer actually wired:
    `av_bridge_commit` registers the emitter sinks as log_sources
    `[{path: .../agentvision/actions.jsonl, adapter: jsonl, label: events},
    ...]` and never sets `action_log_file`, so every reader that looked only at
    the legacy field returned nothing on every modern-bridged profile (the
    measured incident is documented on bridge_server._active_action_log_path).
    The same one-field read also lived in the capture shutter, the input
    daemon and the GUI panes — this function exists so the rule cannot drift
    apart again.

    A non-JSONL source is never substituted: a text log handed to a JSONL
    reader parses zero records, which is the same silence in a different hat.
    Among several JSONL sources the `events` label wins — it is the label the
    installer gives the emitter's own sink, so it is the one carrying
    av.bootstrap.* records.

    Accepts a ProgramProfile or the same fields as a plain dict (the input
    daemon reads profiles.json raw)."""
    if profile is None:
        return ""
    if isinstance(profile, dict):
        _get = profile.get
    else:
        def _get(k, d=None):
            return getattr(profile, k, d)
    explicit = str(_get("action_log_file") or "").strip()
    if explicit:
        return os.path.expanduser(explicit)
    srcs = _get("log_sources") or []
    if not isinstance(srcs, list):
        return ""
    jsonl = [s for s in srcs
             if isinstance(s, dict)
             # An entry with no path can never be read; without this test an
             # events-labelled entry whose path is empty would win the label
             # preference below and mask a valid JSONL sibling with "".
             and str(s.get("path") or "").strip()
             and (str(s.get("adapter") or "").lower() == "jsonl"
                  or str(s.get("path") or "").lower().endswith(".jsonl"))]
    if not jsonl:
        return ""
    for s in jsonl:
        if str(s.get("label") or "").lower() == "events":
            return os.path.expanduser(str(s.get("path")))
    return os.path.expanduser(str(jsonl[0].get("path")))


# ── Profile store ─────────────────────────────────────────────────────────────

PROFILES_FILE = Path(__file__).parent.parent / "profiles.json"

#: Did the last load see the WHOLE file? profiles.json holds hand-authored work
#: — project roots, log_sources with pinned adapters, crop rectangles, notes —
#: and nothing else in the tree can re-derive it. The loader used to convert any
#: read or parse failure into "there are no custom profiles" with a bare
#: `except Exception: pass`, and the very next Save/Delete/Set-Active then wrote
#: that emptiness over the file. A collection that is empty because a load failed
#: must never authorise a write; that is precisely the shape of the incident this
#: pass exists to remove. So the failure is remembered and save refuses.
_LAST_LOAD_OK = True
_LAST_LOAD_ERROR = ""


class ProfileSaveRefused(RuntimeError):
    """Raised instead of overwriting profiles.json with an unsafe view of it."""


def last_load_ok() -> tuple[bool, str]:
    return _LAST_LOAD_OK, _LAST_LOAD_ERROR


def load_profiles() -> dict[str, ProgramProfile]:
    global _LAST_LOAD_OK, _LAST_LOAD_ERROR
    profiles = dict(BUILTIN_PROFILES)
    _LAST_LOAD_OK, _LAST_LOAD_ERROR = True, ""
    if PROFILES_FILE.exists():
        try:
            data = json.loads(PROFILES_FILE.read_text())
        except Exception as exc:
            _LAST_LOAD_OK = False
            _LAST_LOAD_ERROR = f"could not read/parse {PROFILES_FILE}: {exc}"
            print(f"[profiles] WARNING: {_LAST_LOAD_ERROR} — built-in profiles "
                  f"only; saving is BLOCKED until this is resolved so the file "
                  f"is not overwritten with an empty set")
            return profiles
        if not isinstance(data, dict):
            _LAST_LOAD_OK = False
            _LAST_LOAD_ERROR = (f"{PROFILES_FILE} is {type(data).__name__}, "
                                f"expected an object")
            print(f"[profiles] WARNING: {_LAST_LOAD_ERROR} — saving is BLOCKED")
            return profiles
        for name, d in data.items():
            try:
                profiles[name] = ProgramProfile.from_dict(d)
            except Exception as exc:
                # One unreadable entry must not silently vanish on the next save.
                _LAST_LOAD_OK = False
                _LAST_LOAD_ERROR = f"profile {name!r} failed to load: {exc}"
                print(f"[profiles] WARNING: {_LAST_LOAD_ERROR} — saving is "
                      f"BLOCKED so {name!r} is not dropped from the file")
    return profiles


def save_profiles(profiles: dict[str, ProgramProfile], *, force: bool = False):
    """Write profiles.json. Refuses when the last load was incomplete.

    Also writes atomically. The old implementation was a bare `write_text`, which
    truncates in place: a crash or a kill mid-write left truncated JSON — exactly
    the state the loader turned into "there are no custom profiles", which then
    authorised the next save to make it permanent. temp file + os.replace means a
    reader sees either the old file or the new one, never a half of either.

    """
    if not _LAST_LOAD_OK and not force:
        raise ProfileSaveRefused(
            f"refusing to overwrite {PROFILES_FILE}: the last load did not "
            f"fully succeed ({_LAST_LOAD_ERROR}). Fix or move the file first; "
            f"saving now would commit an incomplete view of it. Pass "
            f"force=True only if you intend to replace it wholesale.")

    data = {name: p.to_dict() for name, p in profiles.items()}
    body = json.dumps(data, indent=2)

    # Keep one generation back. Nothing else in the tree can reconstruct this.
    try:
        if PROFILES_FILE.exists():
            PROFILES_FILE.with_suffix(".json.bak").write_text(
                PROFILES_FILE.read_text())
    except Exception:
        pass

    PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROFILES_FILE.with_suffix(".json.tmp")
    tmp.write_text(body)
    os.replace(tmp, PROFILES_FILE)


def profile_output_folder(profile: ProgramProfile, base_root: Path) -> Path:
    """
    Return (and create) the per-program output folder.
    e.g.  <snapburst_save_folder>/agentvision/<profile_name>/
    Always creates it on first call.
    """
    safe_name = re.sub(r"[^\w\-]", "_", profile.name)
    folder = base_root / "agentvision" / safe_name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# ── Live data reader ──────────────────────────────────────────────────────────

# ── Process-identity matching ────────────────────────────────────────────────
# Interpreter / shell names that are FAR too generic to identify a program on
# their own: half the processes on a dev box are "python3" or "node", and
# AgentVision's OWN bridge, MCP server, GUI and daemon are among them. Treating
# a bare name match on these as proof made is_running() return True for a target
# that had already crashed (it was matching AgentVision itself), which in turn
# silenced the ambient "program died" alert. For these names the cmdline MUST
# also point at the target project/script before we call it a match.
_GENERIC_PROCESS_NAMES = {
    "python", "python3", "python2", "pythonw", "py", "py.exe", "python.exe",
    "python3.exe", "node", "nodejs", "node.exe", "deno", "bun",
    "ruby", "perl", "php", "java", "javaw", "dotnet", "mono",
    "sh", "bash", "zsh", "fish", "dash", "ksh", "csh",
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh",
    "sudo", "env", "npx", "npm", "yarn", "pnpm", "uv", "electron",
}

#: Command-line shapes that mean "a tool ACTING ON the project", not the project's
#: own program. Used only when psutil cannot give us an absolute exe path, as a
#: fallback for the same discrimination. Caught live: `dotnet publish` spawned
#: MSBuild/Avalonia build nodes carrying .../<project>/src/... in their args,
#: and AgentVision reported the target program as running because of it — then
#: described the build server's open file descriptors as the emulator's.
_DEV_TOOL_MARKERS = (
    "msbuild", "buildservices", "vbcscompiler", "dotnet build", "dotnet publish",
    "dotnet restore", "dotnet test", "roslyn", "omnisharp",
    "/usr/bin/git", " git ", "grep", "ripgrep", " rg ", "find ", "xargs",
    "clang", "gcc", "cc1", "ld ", "cmake", "ninja", "make ", "gradle", "maven",
    "webpack", "vite", "esbuild", "tsc ", "eslint", "prettier",
    "language-server", "languageserver", "lsp", "code helper", "cursor helper",
)

# AgentVision's own entry points — never the program under test.
_AV_SELF_MARKERS = (
    "bridge_server.py", "claude_mcp.py", "agent_vision_gui.py",
    "input_daemon", "python_backend.cli", "agentvision_hook",
    # The run wrapper by SCRIPT PATH — the invocation the README recommends
    # (`python3 /path/to/AgentVision/python_backend/cli.py run -- ...`). Only
    # the `-m python_backend.cli` module form was listed, so the wrapper's own
    # process matched the profile (its argv carries the target script and its
    # cwd is the project), the window lookup then received the WRAPPER's pid,
    # and every frame was skipped for a window that was on screen. Measured
    # end-to-end on a tkinter target. Path-anchored on purpose: a user
    # project's own cli.py does not live under a folder named python_backend.
    "python_backend/cli.py", "python_backend\\cli.py",
)

#: argv[0] basenames that READ OR VISIT files for a living. _DEV_TOOL_MARKERS
#: caught compilers and build servers, but not the far more common case, measured
#: live on this machine: a plain shell command that merely MENTIONS the project.
#:
#:     /bin/zsh -c '… ~/Developer/<project>/log/actions.jsonl …'
#:
#: satisfies `process_name in cmdline` ("sharpemu") and `project_root in cmdline`,
#: so is_running() returned True — for a shell, for `cat`, for `less`, for the
#: agent's own tooling. Capture then runs against a program that is not there.
#: Verified: with such a shell alive is_running() was True; with only the string
#: "sharpemu" present and no project path, False.
_VISITING_UTILITIES = {
    "sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh",
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh",
    "cat", "less", "more", "head", "tail", "tee", "ls", "dir", "stat", "file",
    "cp", "mv", "rm", "ln", "mkdir", "touch", "chmod", "chown", "du", "df",
    "wc", "sort", "uniq", "cut", "awk", "sed", "tr", "diff", "cmp", "patch",
    "grep", "egrep", "fgrep", "rg", "ag", "ack", "find", "fd", "locate",
    "xargs", "open", "openwith", "qlmanage", "mdfind", "mdls",
    "tar", "zip", "unzip", "gzip", "gunzip", "bzip2", "xz", "7z", "rsync",
    "scp", "curl", "wget", "md5", "md5sum", "shasum", "sha256sum", "xxd", "od",
    "vim", "vi", "nvim", "nano", "emacs", "code", "cursor", "subl", "mate",
    "git", "hg", "svn", "watch", "entr", "fswatch", "trash", "python -c",
}

#: Runtimes that can plausibly BE an interpreted target's process. Used only for
#: the weakest branch of the match, where the program's name appears in the
#: ARGUMENTS rather than in the process name.
_TARGET_RUNTIMES = {
    "python", "python2", "python3", "pythonw", "py", "python.exe", "pythonw.exe",
    "python3.exe", "node", "nodejs", "node.exe", "deno", "bun", "electron",
    "ruby", "perl", "php", "java", "javaw", "dotnet", "mono", "wine", "wine64",
    "wine-preloader", "wineserver", "lua", "luajit", "R", "Rscript", "julia",
}


def _argv0_base(argv, cmdline: str) -> str:
    """Basename of argv[0], lowercased. Falls back to the first cmdline token."""
    first = ""
    if argv:
        first = str(argv[0] or "")
    if not first:
        first = (cmdline or "").split(" ", 1)[0]
    first = first.strip().strip('"').lower()
    return first.replace("\\", "/").rsplit("/", 1)[-1]


class ProgramDataReader:
    """
    Reads live runtime data from the connected program.
    All methods return empty/default on failure — never raises.
    """

    def __init__(self, profile: ProgramProfile):
        self.profile = profile

    # ── Log file ──────────────────────────────────────────────────────────────

    def tail_log(self, lines: int = 40, max_offset: int = 0) -> list[str]:
        """Tail the program's primary text log.
        max_offset>0: only read bytes [..max_offset], so capture-time alignment
        is preserved (no lines that arrived after the shutter)."""
        path = Path(self.profile.log_file)
        if not path.exists():
            return []
        try:
            with path.open("rb") as f:
                size = max_offset if max_offset > 0 else os.path.getsize(path)
                # Read just the tail window — 64KB is enough for ~500 typical lines
                start = max(0, size - 65536)
                f.seek(start)
                chunk = f.read(size - start)
            text = chunk.decode("utf-8", errors="replace")
            # First (partial) line is dropped if we didn't start at byte 0
            split = text.splitlines()
            if start > 0 and split:
                split = split[1:]
            return split[-lines:]
        except Exception:
            return []

    def tail_actions(self, lines: int = 20, max_offset: int = 0,
                     path_override: str = "") -> list[dict]:
        """Tail of the program's structured action log (JSONL). Generic — works
        for any bridged program that emits one JSON record per line.
        max_offset>0: only read bytes [..max_offset] (capture-time alignment).
        path_override: read this path instead of the active profile's (used when
        a frame was captured under a different profile and pinned its own path)."""
        # Fallback goes through the shared resolver: modern-bridged profiles
        # keep the JSONL sink in log_sources, and the legacy one-field read
        # returned [] for them (legacy callers that pass no offsets/pin).
        path_str = path_override or resolve_action_log_path(self.profile)
        if not path_str:
            return []
        path = Path(path_str)
        if not path.exists():
            return []
        try:
            with path.open("rb") as f:
                size = max_offset if max_offset > 0 else os.path.getsize(path)
                # 256KB tail window — JSONL records are small, this covers ~1000
                start = max(0, size - 262144)
                f.seek(start)
                chunk = f.read(size - start)
            text = chunk.decode("utf-8", errors="replace")
            split = text.splitlines()
            if start > 0 and split:
                split = split[1:]   # drop possibly-partial first line
            out: list[dict] = []
            for raw in split[-lines:]:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except Exception:
                    # Malformed line — skip silently, never raise into caller
                    continue
            return out
        except Exception:
            return []

    def last_error_from_log(self) -> tuple[str, str]:
        lines = self.tail_log(100)
        error_lines: list[str] = []
        in_traceback = False
        for line in lines:
            if "Traceback" in line or "ERROR" in line or "CRITICAL" in line:
                in_traceback = True
            # A blank line terminates the block WITHOUT being captured — the
            # normal shape of a real log (traceback followed by a blank line).
            # Appending it first would make error_lines[-1] == "" and blank the
            # message, which gates off ALL v2 error enrichment downstream.
            if in_traceback and line.strip() == "":
                break
            if in_traceback:
                error_lines.append(line)
        if not error_lines:
            for line in reversed(lines):
                if "ERROR" in line or "CRITICAL" in line:
                    return line.strip(), line.strip()
        block = "\n".join(error_lines)
        message = error_lines[-1].strip() if error_lines else ""
        return message, block

    def last_log_activity(self, n: int = 6) -> list[str]:
        lines = self.tail_log(60)
        result = []
        for line in reversed(lines):
            if " INFO " in line or " WARNING " in line:
                clean = re.sub(r'^\[.*?\]\s+\w+\s+', '', line).strip()
                if clean:
                    result.append(clean)
                if len(result) >= n:
                    break
        return list(reversed(result))

    # ── Stats ─────────────────────────────────────────────────────────────────

    def latest_stats(self, lines: int = 0) -> dict:
        """Parsed `key: value` pairs from the newest stats_*.log.

        `lines` limits the read to the last N lines of that file (0 = whole file).
        The route already accepted a `lines` parameter and the MCP tool already
        exposed one; neither reached here, so it was a parameter that did nothing.
        """
        folder = Path(self.profile.stats_folder)
        if not folder.exists():
            return {}
        files = sorted(folder.glob("stats_*.log"), key=lambda p: p.stat().st_mtime)
        if not files:
            return {}
        try:
            text = files[-1].read_text(errors="replace")
            if lines and lines > 0:
                text = "\n".join(text.splitlines()[-int(lines):])
            return _parse_stats_block(text)
        except Exception:
            return {}

    # ── Screenshots ───────────────────────────────────────────────────────────

    def latest_program_screenshot(self) -> str:
        folder = Path(self.profile.screenshots_folder)
        if not folder.exists():
            return ""
        try:
            shots = sorted(
                [p for p in folder.rglob("*.png")],
                key=lambda p: p.stat().st_mtime,
                reverse=True)
            return str(shots[0]) if shots else ""
        except Exception:
            return ""

    # ── Config ────────────────────────────────────────────────────────────────

    def read_config_summary(self) -> dict:
        folder = Path(self.profile.config_folder)
        if not folder.exists():
            return {}
        summary: dict = {}
        try:
            for ini_file in folder.glob("*.ini"):
                summary[ini_file.name] = _parse_ini_summary(ini_file)
            for json_file in folder.glob("*.json"):
                try:
                    data = json.loads(json_file.read_text(errors="replace"))
                    if isinstance(data, dict):
                        summary[json_file.name] = {
                            k: str(v)[:80] for k, v in list(data.items())[:10]}
                except Exception:
                    pass
        except Exception:
            pass
        return summary

    # ── State file ────────────────────────────────────────────────────────────

    def read_state(self) -> dict:
        path = Path(self.profile.state_file)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(errors="replace"))
        except Exception:
            return {}

    # ── Process ───────────────────────────────────────────────────────────────

    def _process_matches(self, pname: str, cmdline: str, exe: str = "",
                         argv=None, cwd: str = "") -> bool:
        """Does this process look like the profile's TARGET program?

        True/False form of _match_reason(); see that for the rules. `argv` is the
        raw psutil cmdline LIST when available — the joined string cannot be
        re-split safely once a path contains a space, and argv[0] is what tells a
        launched program apart from a utility that was merely handed its path.
        """
        return bool(self._match_reason(pname, cmdline, exe, argv, cwd))

    @staticmethod
    def _safe_cwd(proc) -> str:
        """The process's working directory, or "" if it cannot be read.

        Deliberately NOT requested via process_iter's attrs: cwd is not a cheap
        field — it costs a syscall per process and raises AccessDenied for
        anything not ours — and the scan walks ~500 processes. It is fetched only
        after the cheap rules have already failed, so the cost is paid once for a
        plausible candidate rather than 500 times for certainties.
        """
        try:
            return proc.cwd() or ""
        except Exception:
            return ""

    def _matches_proc(self, proc, pname: str, cmdline: str, exe: str,
                      argv) -> str:
        """_match_reason, retried with the working directory if the cheap rules
        found nothing. Returns the reason, or ""."""
        why = self._match_reason(pname, cmdline, exe, argv)
        if why:
            return why
        root = (self.profile.project_root or "").strip()
        if not root:
            return ""
        return self._match_reason(pname, cmdline, exe, argv,
                                  cwd=self._safe_cwd(proc))

    def _match_reason(self, pname: str, cmdline: str, exe: str = "",
                      argv=None, cwd: str = "") -> str:
        """WHY this process was taken for the target — "" when it was not.

        Rules (in order):
          1. Never AgentVision's own processes — the bridge/MCP/GUI/daemon are
             python too, so a "python3" profile would otherwise match us.
          2. The project_root in the cmdline is evidence only when this process is
             RUNNING FROM the project, not merely OPERATING ON it (see below).
          3. A process_name match counts ONLY when the name is specific
             (e.g. "SharpEmu", "notepad.exe", "app.py"). For a generic
             interpreter name (python3/node/java/sh/…) the name alone proves
             nothing, so we additionally require the project_root or the
             configured python_exe/test script to appear in the cmdline.
          4. When the name appears ONLY in the arguments, the process must also
             LOOK like a launch of it: argv[0] must not be a file-visiting
             utility or shell, and some argument must name the program itself
             rather than a file or directory that merely lives beside it.

        Returning the reason rather than a bare bool is the point: a liveness
        claim that cannot say WHY is indistinguishable from a wrong one, and this
        function has been wrong in production. Never raises.
        """
        pname = (pname or "").lower()
        cmdline = (cmdline or "").lower()
        exe = (exe or "").lower()
        name = (self.profile.process_name or "").strip().lower()
        root = (self.profile.project_root or "").strip().lower()

        # 1. exclude ourselves
        av_root = str(Path(__file__).resolve().parent.parent.parent).lower()
        if any(m in cmdline for m in _AV_SELF_MARKERS):
            # …unless the user is genuinely debugging AgentVision itself.
            if not (root and root != av_root and root in cmdline):
                return ""

        # 2. RUNNING FROM the project vs OPERATING ON it.
        #
        # `root in cmdline` used to return True on its own, commented
        # "unambiguous". It is not: EVERY tool that works on a project carries the
        # project path in its arguments — compilers, build servers, editors, git,
        # grep. Caught live: `dotnet publish` spawned MSBuild/Avalonia build nodes
        # whose args contained .../<project>/src/..., so AgentVision declared
        # the target program "running" and then faithfully described the BUILD
        # SERVER's open file descriptors as if they were the emulator's. That
        # happens precisely during development — when AgentVision is in use.
        #
        # Worse, the project NAME leaks the same way: a project called "sharpemu"
        # puts the string "sharpemu" in every tool's command line, so matching
        # process_name against the cmdline hit the build server too.
        #
        # The real discriminator is the EXECUTABLE. psutil's exe() is the absolute
        # binary path, which is why cmdline alone cannot answer this: the program
        # may well have been launched by a relative path from inside the project.
        if exe and root and exe.startswith(root):
            return "exe-under-project-root"   # the binary IS in the project

        # A tool visiting the project is never the target, whatever its args say.
        if any(t in cmdline for t in _DEV_TOOL_MARKERS):
            return ""

        # …and neither is a shell or a file-reading utility that was handed the
        # project's path. This is the case that actually fired in production.
        argv0 = _argv0_base(argv, cmdline)
        visiting = argv0 in _VISITING_UTILITIES

        if not name:
            # No name to go on: the project path is the only evidence left, and it
            # is acceptable ONLY because the dev-tool shapes were just excluded.
            if visiting:
                return ""
            return "project-path-only" if (root and root in cmdline
                                           and not exe) else ""

        # 3. name match, with a specificity gate.
        base = pname.rsplit("/", 1)[-1]
        generic = name in _GENERIC_PROCESS_NAMES or base in _GENERIC_PROCESS_NAMES

        if name in pname:
            # The process is literally NAMED this — the strongest signal short of
            # the exe path. A generic interpreter name still needs corroboration.
            if not generic:
                return "process-name"
            return "generic-name+project-path" if (root and root in cmdline) else ""

        if name in cmdline:
            # 4. Named only in the ARGUMENTS. Legitimate for an interpreted target
            # (process_name="app.py" under python3) — and also exactly how a
            # `cat`, `less`, `ls` or shell command that mentions the project earns
            # a false "the program is running". Two extra conditions, both about
            # the SHAPE of the process rather than the presence of a string:
            #   * argv[0] is not a shell/utility that reads files for a living;
            #   * some argument NAMES the program (basename == process_name, or
            #     process_name plus an extension) and is not a directory.
            if visiting:
                return ""
            # "Running FROM the project" has TWO forms, and only one was handled.
            # An absolute launch puts the root in the cmdline. But the commonest
            # shape for a script — `cd project && python main.py` — puts NOTHING
            # absolute in the cmdline at all:
            #
            #     name : Python      argv: ['/opt/.../MacOS/Python', 'main.py']
            #
            # Measured on a real Tk app: process_name="main.py" matched when the
            # script was passed as an absolute path and NOT when launched from
            # inside the project, so capture produced zero frames and reported
            # zero skips. The working directory is exactly the missing evidence,
            # so it is consulted here rather than inferring from the arguments.
            #
            # This cannot reintroduce the false positives rule 4 exists to stop:
            # the dev-tool shapes and the file-visiting utilities were BOTH already
            # excluded above, and _argv_names_target still has to hold — so
            # `cd project && cat main.py` remains a non-match on argv[0] alone.
            from_project = bool(root and root in cmdline)
            if not from_project and root and cwd:
                c = os.path.normpath(cwd).lower()
                r = os.path.normpath(root).lower()
                from_project = c == r or c.startswith(r + os.sep)
            if not from_project:
                return ""
            if self._argv_names_target(argv, cmdline, name):
                if argv0 in _TARGET_RUNTIMES:
                    return ("runtime+script-argument" if (root in cmdline)
                            else "runtime+script-argument-in-cwd")
                return "script-argument" if (root in cmdline) else "script-in-cwd"
            return ""

        return ""

    @staticmethod
    def _argv_names_target(argv, cmdline: str, name: str) -> bool:
        """Does some ARGUMENT name the program itself, rather than a file or
        directory that merely sits next to it?

        `ls /path/to/project/sharpemu` has an argument whose basename IS
        "sharpemu" — but it is a directory, so it is a listing of the program's
        folder, not a launch of it. `cat …/log/actions.jsonl` names a log file.
        `python3 app.py` and `dotnet SharpEmu.dll` name the program.
        """
        toks = [str(t) for t in (argv or [])[1:]] or (cmdline or "").split()[1:]
        for tok in toks:
            t = tok.strip().strip('"').lower()
            b = t.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            if b != name and not b.startswith(name + "."):
                continue
            try:
                if os.path.isdir(tok):
                    continue         # the program's folder is not the program
            except Exception:
                pass
            return True
        return False

    def running_evidence(self) -> dict:
        """Liveness WITH its evidence: which process, and which rule matched.

        is_running() used to answer a bare True/False, and it was wrong in a way
        nothing could see afterwards: 12,921 frames were stored for a program
        that had emitted nothing for over a day, every sidecar recording
        `"running": true` with no cpu/ram beside it. Whatever matched, the frames
        cannot now be traced back to it. This returns the match so that a wrong
        liveness claim is diagnosable at the time it is made.
        """
        out = {"running": False, "pid": None, "process_name": None, "exe": None,
               "matched_by": None, "scanned": 0,
               "basis": "psutil scan of every process, profile matcher"}
        for p in psutil.process_iter(["name", "cmdline", "exe"]):
            out["scanned"] += 1
            try:
                argv = p.info.get("cmdline") or []
                pname = (p.info["name"] or "").lower()
                cmdline = " ".join(argv).lower()
                why = self._matches_proc(p, pname, cmdline,
                                         p.info.get("exe") or "", argv)
                if why:
                    out.update({"running": True, "pid": p.pid,
                                "process_name": p.info.get("name"),
                                "exe": p.info.get("exe") or None,
                                "matched_by": why})
                    return out
            except Exception:
                pass
        return out

    def is_running(self) -> bool:
        """Bare liveness. Prefer running_evidence() when the answer will be
        stored or shown — it carries which process matched and why."""
        return bool(self.running_evidence()["running"])

    def process_cpu_ram(self) -> tuple[float, float]:
        for p in psutil.process_iter(["name", "cmdline", "exe", "cpu_percent", "memory_info"]):
            try:
                argv    = p.info.get("cmdline") or []
                pname   = (p.info["name"] or "").lower()
                cmdline = " ".join(argv).lower()
                if self._matches_proc(p, pname, cmdline,
                                     p.info.get("exe") or "", argv):
                    cpu = p.cpu_percent(interval=0.1)
                    ram = (p.memory_info().rss / 1_073_741_824
                           if p.memory_info() else 0.0)
                    return round(cpu, 1), round(ram, 2)
            except Exception:
                pass
        return 0.0, 0.0

    def process_perf(self) -> dict:
        """Structured per-frame resource metrics for the target process, so the
        AI can correlate a visual/log symptom with CPU/RAM/thread pressure.
        Returns {found, pid, cpu_percent, rss_mb, ram_gb, num_threads,
        num_fds?, status}. All-defaults when the process isn't found; never
        raises. Cross-platform (psutil)."""
        name = (self.profile.process_name or "").lower()
        root = (self.profile.project_root or "").lower()
        out = {"found": False, "pid": None, "cpu_percent": 0.0, "rss_mb": 0.0,
               "ram_gb": 0.0, "num_threads": 0, "status": ""}
        if not name and not root:
            return out
        for p in psutil.process_iter(["name", "cmdline", "exe"]):
            try:
                argv = p.info.get("cmdline") or []
                pname = (p.info["name"] or "").lower()
                cmdline = " ".join(argv).lower()
                if self._matches_proc(p, pname, cmdline,
                                     p.info.get("exe") or "", argv):
                    mi = p.memory_info()
                    rss = getattr(mi, "rss", 0) if mi else 0
                    out.update({
                        "found": True,
                        "pid": p.pid,
                        "cpu_percent": round(p.cpu_percent(interval=0.1), 1),
                        "rss_mb": round(rss / 1_048_576, 1),
                        "ram_gb": round(rss / 1_073_741_824, 3),
                        "num_threads": p.num_threads(),
                        "status": p.status(),
                    })
                    try:            # num_fds is POSIX-only; skip on Windows
                        out["num_fds"] = p.num_fds()
                    except Exception:
                        pass
                    return out
            except Exception:
                pass
        return out

    # ── Crop / capture helpers ────────────────────────────────────────────────

    def get_capture_crop(self) -> tuple[int, int, int, int] | None:
        """
        Parse profile.capture_crop ("x,y,w,h") → (x, y, w, h) ints, or None.
        """
        crop_str = (self.profile.capture_crop or "").strip()
        if not crop_str:
            return None
        try:
            parts = [int(v.strip()) for v in crop_str.split(",")]
            if len(parts) == 4:
                return tuple(parts)  # type: ignore[return-value]
        except Exception:
            pass
        return None

    def get_window_id(self):
        """
        Find the window ID of the largest real window owned by capture_app.
        Delegates to utils.platform_shim, which uses Quartz
        CGWindowListCopyWindowInfo on macOS (returns a CGWindowNumber that
        `screencapture -l<wid>` can grab) and win32gui EnumWindows on Windows
        (returns an HWND whose bounds mss captures). On macOS this works
        regardless of window position, size, overlap, or mode; on Windows it is
        a screen-region grab of the window bounds, so it does NOT survive
        occlusion (an overlapping window's pixels would be captured) or
        minimization. Returns the id or None if not found.
        """
        try:
            from python_backend.utils import platform_shim
        except Exception:
            from utils import platform_shim  # when python_backend is on sys.path

        # Pass the PID of the process the matcher already identified. Without it
        # the lookup has only the owner name to go on, and `Python` / `java` /
        # `node` / `Electron` are shared by a whole ecosystem — including
        # AgentVision's own processes — so it could select another app's window,
        # or a DEAD window record left by a previous run of the same program.
        # Measured: a relaunched tkinter app produced 19 consecutive
        # "could not create image from window" errors because the largest
        # name-matching record belonged to an already-exited run.
        pid = None
        try:
            ev = self.running_evidence()
            if ev.get("running"):
                pid = ev.get("pid")
            elif ev.get("pid"):
                pid = ev.get("pid")
        except Exception:
            pid = None
        w = platform_shim.find_window(self.profile.capture_app or "", pid=pid)
        if w:
            return w.get("wid")
        # The matched process may be a LAUNCHER — the run wrapper, a shell, an
        # npm/gradle front — whose CHILD owns the actual window. A descendant
        # of the matched process is still the strong identity (it is inside
        # that process's own tree), so walk the children before giving up.
        # This is NOT the forbidden name-only fallback: every candidate pid
        # here is vouched for by the process the matcher identified.
        if pid:
            try:
                kids = psutil.Process(pid).children(recursive=True)
            except Exception:
                kids = []
            for k in kids:
                w = platform_shim.find_window(self.profile.capture_app or "",
                                              pid=k.pid)
                if w:
                    return w.get("wid")
        # No window for that process or its tree. Falling back to a name-only
        # search would reintroduce exactly the wrong-window bug, so report
        # nothing instead: a skipped frame is honest, a photograph of someone
        # else's window is not.
        return None

    def get_window_bounds(self) -> tuple[int, int, int, int] | None:
        """Kept for /program/crop API endpoint — returns x,y,w,h for the
        capture_app window (Quartz on macOS, win32gui on Windows)."""
        try:
            from python_backend.utils import platform_shim
        except Exception:
            from utils import platform_shim
        return platform_shim.get_window_bounds(self.profile.capture_app or "")

    # ── Overlay summary ───────────────────────────────────────────────────────

    def overlay_summary(self) -> list[str]:
        lines: list[str] = []
        running = self.is_running()
        lines.append(
            f"{self.profile.display_name}  {'● RUNNING' if running else '○ STOPPED'}")
        stats = self.latest_stats()
        if stats:
            if stats.get("games"):
                lines.append(f"  Games: {stats['games']}  "
                             f"XP/hr: {stats.get('xp_per_hour','?')}")
            if stats.get("session_length"):
                lines.append(f"  Session: {stats['session_length']}")
            if stats.get("time_to_level"):
                lines.append(f"  TTL: {stats['time_to_level']}")
        activity = self.last_log_activity(3)
        for a in activity:
            lines.append(f"  {a[:65]}")
        err_msg, _ = self.last_error_from_log()
        if err_msg:
            lines.append(f"  ERR: {err_msg[:65]}")
        return lines


# ── Parsers ───────────────────────────────────────────────────────────────────

#: The ORIGINAL nine keys this parser recognised, kept as aliases so anything
#: reading them still works. They are Diablo-II-bot specific — session length,
#: games played, xp/hour — and they were the ONLY keys returned: every other
#: `key: value` line in a program's stats file was silently dropped, while the
#: tool advertised "numeric metrics, counters, gauges" for any program.
_STATS_ALIASES = (
    ("session_length", "session_length"), ("avg_game", "avg_game_length"),
    ("current_level", "level"), ("xp_per_hour", "xp_per_hour"),
    ("xp_per_game", "xp_per_game"), ("time_needed", "time_to_level"),
    ("games_needed", "games_to_level"), ("xp_gained", "xp_gained"),
)


def _parse_stats_block(text: str) -> dict:
    """Parse `key: value` lines from a program's stats output — ALL of them.

    Box-drawing frames are skipped (these files are often rendered tables). Keys
    are lowercased with spaces as underscores. The nine legacy bot-specific keys
    are additionally emitted under their old alias names so existing readers do
    not break.
    """
    result: dict = {}
    for line in text.splitlines():
        # Strip table borders instead of discarding the line. The old test was
        # `line.startswith("│")`, which threw away every row of a boxed table —
        # the very shape these files are usually printed in.
        line = line.strip().strip("│┃|").strip()
        if not line or line[0] in "╭├╰┌└┐┘─═":
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower().replace(" ", "_")
        val = val.strip()
        if not key:
            continue
        result[key] = val
        if key == "games":
            result["games"] = val
            continue
        for needle, alias in _STATS_ALIASES:
            if needle in key:
                result[alias] = val
                break
    return result


def _parse_ini_summary(path: Path) -> dict:
    result: dict = {}
    try:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if line.startswith(("#", ";", "[")) or "=" not in line:
                continue
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()[:80]
    except Exception:
        pass
    return result
