"""
Universal output-emitter framework — the "other side" of the AgentVision bridge.
================================================================================
AgentVision's model is a TWO-SIDED bridge:

  • INPUT side  (built into AgentVision) — it READS logs: the log-adapter
    registry + log_sources + the MCP tools. Already universal.
  • OUTPUT side (this module) — on first attach, AgentVision INSTALLS the output
    logging INTO the target program, by scaffolding a self-contained
    `agentvision/` folder inside that project holding the sink files AND a
    language-appropriate EMITTER that makes the program write there. So ANY
    program, in ANY language, is bridged with ZERO code changes.

Every emitter — whatever the language — writes the SAME unified JSONL event
schema that the rest of AgentVision reads (see agent_bootstrap/av_runtime.py,
cli.py, connectors/log_adapters.py):

    {"ts","ts_ms","category","source","state","run_id","trace_id",
     "frame_seq","coords","data":{...}}

into `<project>/agentvision/actions.jsonl` (structured) and plain text into
`<project>/agentvision/log.txt`.

────────────────────────────────────────────────────────────────────────────────
How each language is bridged (honest about the mechanism)
────────────────────────────────────────────────────────────────────────────────
  kind = "autoload"     TRUE zero-config: the interpreter loads the emitter on a
                        normal launch with no env var and no code change.
                          • Python — `sitecustomize.py` at the project root is
                            auto-imported when Python runs from the project dir.
  kind = "env_autoload" In-process, zero CODE change, but the emitter loads only
                        when the launcher sets one env var. `agentvision run`
                        sets it automatically, so it is zero-effort in practice.
                          • Node.js — NODE_OPTIONS=--require .../av_emit.js
                          • Ruby    — RUBYOPT=-r .../av_emit.rb
  kind = "config"       Config drop-in: a ready logger config that routes output
                        to the sink; the app must be told to use it (one flag),
                        else the `agentvision run` tee still bridges it.
                          • Java  — logback / log4j2 JSON appender
                          • .NET  — Serilog JSON file sink
  kind = "tee"          The zero-effort default for compiled languages: launch
                        via `agentvision run -- <cmd>`; stdout/stderr is teed and
                        normalized through the adapters into the sink. An
                        OPTIONAL structured snippet is also dropped for richer
                        output if the developer wants it.
                          • Go, Rust, C/C++, and anything else.

`agentvision run -- <cmd>` is the universal front door: it injects the right
env for autoload/env_autoload languages AND tees stdout/stderr for everything,
so a single command bridges any program.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Canonical output folder + sink file names created inside every bridged project.
SINK_DIRNAME = "agentvision"
ACTIONS_SINK = "actions.jsonl"
TEXT_SINK = "log.txt"
EMITTERS_SUBDIR = "emitters"

# Which detected language maps to which emitter builder. Languages not listed
# fall back to the tee bridge (kind="tee"), which works for anything.
SUPPORTED = (
    "python", "node", "ruby", "java", "dotnet",
    "go", "rust", "cpp", "php", "elixir", "shell",
)


@dataclass
class EmitterSpec:
    """Everything the installer + `agentvision run` need to wire one language."""
    language: str
    kind: str                       # autoload | env_autoload | config | tee
    # files to write under <project>/agentvision/  (relpath -> content)
    files: dict[str, str] = field(default_factory=dict)
    # env vars that make the in-process emitter auto-load; `agentvision run`
    # injects these. Values may contain the placeholder {emitter} → absolute
    # path of the emitter file (resolved by the caller).
    run_env: dict[str, str] = field(default_factory=dict)
    autoload: bool = False          # True = zero-config on a normal launch
    verify: str = "tee"             # spawn_python|spawn_node|spawn_ruby|emitter_present|tee
    load_instructions: str = ""
    notes: str = ""
    #: True when the agent's emitter selection actually gates what arms at runtime
    #: (in-process hook languages). False means one artifact is produced whatever
    #: was selected, so the plan records a preference rather than a build — the
    #: distinction the commit report must not blur.
    selection_enforced: bool = False


# ══════════════════════════════════════════════════════════════════════════════
#  Per-language emitter source (each writes the unified schema at RUNTIME; the
#  sink path is resolved relative to the emitter file or via AGENTVISION_SINK, so
#  the whole agentvision/ folder is portable — no absolute paths baked in).
# ══════════════════════════════════════════════════════════════════════════════

_NODE_EMITTER = r'''// AgentVision Node.js emitter — auto-loaded via NODE_OPTIONS=--require.
// Writes the unified AgentVision JSONL event schema with ZERO code changes.
'use strict';
const fs = require('fs');
const path = require('path');
const util = require('util');

if (!global.__AGENTVISION_NODE_HOOKED__) {
  global.__AGENTVISION_NODE_HOOKED__ = true;

  const SINK = process.env.AGENTVISION_SINK ||
               path.join(__dirname, '..', 'actions.jsonl');
  const SEQ = path.join(path.dirname(SINK), '.av_frame_seq');
  const RUN_ID = 'av-' + Date.now() + '-' + process.pid;
  let seqCache = [0, null];

  function frameSeq() {
    const now = Date.now();
    if (now - seqCache[0] < 1000) return seqCache[1];
    let v = null;
    try { v = parseInt(String(fs.readFileSync(SEQ, 'utf8')).trim(), 10);
          if (isNaN(v)) v = null; } catch (e) {}
    seqCache = [now, v];
    return v;
  }

  function emit(category, source, data) {
    const now = Date.now();
    const rec = { ts: new Date(now).toISOString(), ts_ms: now,
      category: category, source: source, state: null, run_id: RUN_ID,
      trace_id: null, frame_seq: frameSeq(), coords: null, data: data };
    try { fs.appendFileSync(SINK, JSON.stringify(rec) + '\n'); } catch (e) {}
  }
  global.__agentvisionEmit = emit;   // programs MAY call this for rich events

  const orig = { log: console.log, info: console.info, warn: console.warn,
                 error: console.error, debug: console.debug };
  const wrap = (fn, channel, level) => function () {
    const args = Array.prototype.slice.call(arguments);
    try { fn.apply(console, args); } catch (e) {}
    try {
      const msg = args.map(a => typeof a === 'string' ? a : util.inspect(a)).join(' ');
      emit(channel, 'av.node.console.' + level, { level: level, message: msg, text: msg });
    } catch (e) {}
  };
  console.log = wrap(orig.log, 'log', 'info');
  console.info = wrap(orig.info, 'log', 'info');
  console.warn = wrap(orig.warn, 'warn', 'warn');
  console.error = wrap(orig.error, 'error', 'error');
  console.debug = wrap(orig.debug, 'debug', 'debug');

  process.on('uncaughtException', function (err) {
    emit('exception', 'av.node.uncaughtException',
         { type: err && err.name, message: err && err.message, stack: err && err.stack });
    if (process.listenerCount('uncaughtException') === 1) {
      try { orig.error.call(console, (err && err.stack) || String(err)); } catch (e) {}
      process.exit(1);   // preserve default crash behaviour
    }
  });
  process.on('unhandledRejection', function (reason) {
    emit('exception', 'av.node.unhandledRejection',
         { message: String(reason), stack: reason && reason.stack });
  });

  emit('process', 'av.node.start',
       { pid: process.pid, argv: process.argv, node: process.version, cwd: process.cwd() });
  process.on('exit', function (code) {
    emit('process', 'av.node.exit', { pid: process.pid, code: code });
  });
}
'''

_RUBY_EMITTER = r'''# AgentVision Ruby emitter — auto-loaded via RUBYOPT=-r.
# Writes the unified AgentVision JSONL event schema with ZERO code changes.
unless $__agentvision_ruby_hooked
  $__agentvision_ruby_hooked = true
  require 'json'

  _sink = ENV['AGENTVISION_SINK'] ||
          File.join(File.dirname(File.expand_path(__FILE__)), '..', 'actions.jsonl')
  _seq = File.join(File.dirname(_sink), '.av_frame_seq')
  _run_id = "av-#{(Time.now.to_f * 1000).to_i}-#{Process.pid}"
  _seq_cache = [0.0, nil]

  _frame_seq = lambda do
    now = Time.now.to_f
    return _seq_cache[1] if now - _seq_cache[0] < 1.0
    v = (Integer(File.read(_seq).strip) rescue nil)
    _seq_cache = [now, v]
    v
  end

  _emit = lambda do |category, source, data|
    now = Time.now
    rec = { 'ts' => now.utc.strftime('%Y-%m-%dT%H:%M:%S.') + format('%03dZ', now.usec / 1000),
            'ts_ms' => (now.to_f * 1000), 'category' => category, 'source' => source,
            'state' => nil, 'run_id' => _run_id, 'trace_id' => nil,
            'frame_seq' => _frame_seq.call, 'coords' => nil, 'data' => data }
    begin
      File.open(_sink, 'a') { |f| f.write(JSON.generate(rec) + "\n") }
    rescue StandardError
    end
  end
  $agentvision_emit = _emit   # programs MAY call this for rich events

  _tee = Class.new do
    def initialize(orig, channel, emit); @orig = orig; @channel = channel; @emit = emit; @buf = ''; end
    def write(s)
      @orig.write(s); @buf += s.to_s
      while (i = @buf.index("\n"))
        line = @buf[0...i]; @buf = @buf[(i + 1)..-1] || ''
        @emit.call(@channel, "av.ruby.#{@channel}", { 'message' => line, 'text' => line }) unless line.empty?
      end
      s.to_s.length
    end
    def flush; @orig.flush; end
    def method_missing(m, *a, &b); @orig.send(m, *a, &b); end
    def respond_to_missing?(m, i = false); @orig.respond_to?(m, i); end
  end
  $stdout = _tee.new($stdout, 'stdout', _emit)
  $stderr = _tee.new($stderr, 'stderr', _emit)

  _emit.call('process', 'av.ruby.start',
             { 'pid' => Process.pid, 'argv' => ARGV, 'ruby' => RUBY_VERSION, 'cwd' => Dir.pwd })
  at_exit { _emit.call('process', 'av.ruby.exit', { 'pid' => Process.pid }) }
end
'''

_GO_EMITTER = r'''// AgentVision Go emitter (OPTIONAL — the zero-effort default for Go is the
// `agentvision run -- ./yourprogram` stdout/stderr tee). Drop this in and call
// agentvision.Emit(...) for richer structured events, or wire the slog handler.
//
//   import av "<yourmodule>/agentvision/emitters"
//   av.Emit("event", "myapp.boot", map[string]any{"name": "started"})
//
// Writes the unified AgentVision JSONL schema to agentvision/actions.jsonl.
package emitters

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

var (
	mu    sync.Mutex
	sink  = resolveSink()
	runID = fmt.Sprintf("av-%d-%d", time.Now().UnixMilli(), os.Getpid())
)

func resolveSink() string {
	if s := os.Getenv("AGENTVISION_SINK"); s != "" {
		return s
	}
	if wd, err := os.Getwd(); err == nil {
		return filepath.Join(wd, "agentvision", "actions.jsonl")
	}
	return "agentvision/actions.jsonl"
}

// Emit writes one unified-schema event to the AgentVision sink.
func Emit(category, source string, data map[string]any) {
	now := time.Now()
	rec := map[string]any{
		"ts": now.UTC().Format("2006-01-02T15:04:05.000Z"), "ts_ms": now.UnixMilli(),
		"category": category, "source": source, "state": nil, "run_id": runID,
		"trace_id": nil, "frame_seq": nil, "coords": nil, "data": data,
	}
	b, err := json.Marshal(rec)
	if err != nil {
		return
	}
	mu.Lock()
	defer mu.Unlock()
	if f, err := os.OpenFile(sink, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644); err == nil {
		defer f.Close()
		f.Write(append(b, '\n'))
	}
}
'''

_SHELL_EMITTER = r'''#!/usr/bin/env bash
# AgentVision shell emitter — `source` this file to get an `av_log` function,
# or just launch your script via `agentvision run -- ./script.sh` (the tee
# bridges stdout/stderr with zero effort). Writes the unified JSONL schema.
: "${AGENTVISION_SINK:=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)/actions.jsonl}"
_AV_RUN_ID="av-$(date +%s000)-$$"
av_log() {
  # usage: av_log <category> <source> <message>
  local cat="${1:-log}" src="${2:-av.shell}" msg="${3:-}"
  local ts_ms; ts_ms=$(date +%s000)
  local ts;    ts=$(date -u +%Y-%m-%dT%H:%M:%S.000Z)
  # JSON-escape the message (backslash, quote, control chars via python if present)
  local esc
  if command -v python3 >/dev/null 2>&1; then
    esc=$(printf '%s' "$msg" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
  else
    esc="\"${msg//\"/\\\"}\""
  fi
  printf '{"ts":"%s","ts_ms":%s,"category":"%s","source":"%s","state":null,"run_id":"%s","trace_id":null,"frame_seq":null,"coords":null,"data":{"message":%s,"text":%s}}\n' \
    "$ts" "$ts_ms" "$cat" "$src" "$_AV_RUN_ID" "$esc" "$esc" >> "$AGENTVISION_SINK"
}
'''

# Java logback JSON appender — reference via -Dlogback.configurationFile or <include>.
_JAVA_LOGBACK = r'''<?xml version="1.0" encoding="UTF-8"?>
<!-- AgentVision logback config. Route your app's logs to the AgentVision sink:
     java -Dlogback.configurationFile=agentvision/emitters/logback-agentvision.xml ...
     (or <include> this appender from your existing logback.xml). Each line is
     one unified-schema JSON event. Requires logback-classic on the classpath.
     If you'd rather change nothing, launch via `agentvision run` (stdout tee). -->
<configuration>
  <appender name="AGENTVISION" class="ch.qos.logback.core.FileAppender">
    <file>${AGENTVISION_SINK:-agentvision/actions.jsonl}</file>
    <append>true</append>
    <encoder>
      <pattern>{"ts":"%d{yyyy-MM-dd'T'HH:mm:ss.SSS'Z'}","ts_ms":%d{UNIX_MILLIS},"category":"%.-1level","level":"%level","source":"%logger","state":null,"run_id":null,"trace_id":null,"frame_seq":null,"coords":null,"data":{"message":"%replace(%msg){'\"','\\\"'}","thread":"%thread","logger":"%logger"}}%n</pattern>
    </encoder>
  </appender>
  <root level="INFO">
    <appender-ref ref="AGENTVISION"/>
  </root>
</configuration>
'''

# .NET Serilog JSON file sink config + snippet.
_DOTNET_SERILOG = r'''{
  "_comment": "AgentVision .NET config. Add Serilog + Serilog.Sinks.File and either load this appsettings section or use the snippet below. Writes one unified-schema JSON event per line to the AgentVision sink. If you'd rather change nothing, launch via `agentvision run` (stdout tee).",
  "Serilog": {
    "Using": [ "Serilog.Sinks.File" ],
    "MinimumLevel": "Information",
    "WriteTo": [
      {
        "Name": "File",
        "Args": {
          "path": "agentvision/actions.jsonl",
          "rollingInterval": "Infinite",
          "outputTemplate": "{{\"ts\":\"{Timestamp:yyyy-MM-ddTHH:mm:ss.fffZ}\",\"ts_ms\":0,\"category\":\"log\",\"level\":\"{Level}\",\"source\":\"{SourceContext}\",\"state\":null,\"run_id\":null,\"trace_id\":null,\"frame_seq\":null,\"coords\":null,\"data\":{{\"message\":\"{Message:lj}\"}}}}{NewLine}"
        }
      }
    ]
  },
  "_csharp_snippet": "Log.Logger = new LoggerConfiguration().ReadFrom.Configuration(config).CreateLogger();  // reads the Serilog section above"
}
'''

_RUST_NOTE = r'''AgentVision — Rust bridging
===========================
Zero-effort default: launch via `agentvision run -- ./target/debug/yourapp`.
stdout/stderr is teed and normalized (env_logger / tracing formats are all
recognized by AgentVision's adapters) into agentvision/actions.jsonl.

For richer STRUCTURED output, use tracing with a JSON writer pointed at the sink:

    use std::fs::OpenOptions;
    let sink = std::env::var("AGENTVISION_SINK")
        .unwrap_or_else(|_| "agentvision/actions.jsonl".into());
    let file = OpenOptions::new().create(true).append(true).open(sink).unwrap();
    tracing_subscriber::fmt().json().with_writer(move || file.try_clone().unwrap()).init();

(The `jsonl` adapter passes tracing-json through; the `rust` adapter parses
env_logger text. Either way it lands on the AgentVision timeline.)
'''

_CPP_NOTE = r'''AgentVision — C/C++ bridging
============================
Zero-effort default: launch via `agentvision run -- ./yourapp`. stdout/stderr is
teed and normalized into agentvision/actions.jsonl (glog/spdlog/plain formats
are recognized by AgentVision's adapters).

For richer STRUCTURED output with spdlog, add a basic_file_sink pointed at the
AgentVision sink and use a JSON-shaped pattern, e.g.:

    auto sink = std::make_shared<spdlog::sinks::basic_file_sink_mt>(
        std::getenv("AGENTVISION_SINK") ? std::getenv("AGENTVISION_SINK")
                                        : "agentvision/actions.jsonl", true);
    sink->set_pattern("{\"ts_ms\":%E000,\"category\":\"log\",\"level\":\"%l\",\"source\":\"%n\",\"data\":{\"message\":\"%v\"}}");
'''


# ══════════════════════════════════════════════════════════════════════════════
#  Builder
# ══════════════════════════════════════════════════════════════════════════════

def _emitters_rel(fname: str) -> str:
    return f"{EMITTERS_SUBDIR}/{fname}"


# Aliases → canonical language key, so an explicit override like "csharp" or "c"
# maps to the right emitter (the detector already returns dotnet/cpp for these,
# but callers may pass the human name).
_LANG_ALIASES = {
    "csharp": "dotnet", "c#": "dotnet", "cs": "dotnet", "fsharp": "dotnet",
    "f#": "dotnet", "vb": "dotnet", ".net": "dotnet", "net": "dotnet",
    "c": "cpp", "c++": "cpp", "cxx": "cpp", "cc": "cpp", "objc": "cpp",
    "objective-c": "cpp", "cuda": "cpp",
    "javascript": "node", "typescript": "node", "ts": "node", "js": "node",
    "nodejs": "node", "golang": "go", "rs": "rust", "rb": "ruby",
    "kotlin": "java", "scala": "java", "jvm": "java", "py": "python",
}


#: Emitter ids that map to an in-process Python hook (see
#: agent_bootstrap.av_runtime._HOOK_IDS). Selecting a subset of these is
#: meaningful; anything else in a plan is a no-op for the runtime.
HOOK_SELECTABLE = ("stdout_tee", "logging_bridge", "uncaught_exceptions",
                   "swallowed_exceptions", "lifecycle")


def hooks_env(selected: Optional[list] = None) -> dict:
    """AGENTVISION_HOOKS for a plan's emitter selection, or {} for "all".

    Returning {} on an empty/unknown selection is deliberate: an empty env value
    means "arm everything" in the runtime, so a program is never left silently
    uninstrumented because a plan named an id this build does not implement.
    """
    want = [s for s in (selected or []) if s in HOOK_SELECTABLE]
    if not want or len(want) == len(HOOK_SELECTABLE):
        return {}
    return {"AGENTVISION_HOOKS": ",".join(sorted(want))}


#: Languages whose emitter is ONE artifact regardless of what was selected: a
#: logging-framework config drop-in, or the stdout/stderr tee for a compiled
#: binary. There is no in-process hook to gate, so the ids a plan lists are facets
#: of that single artifact rather than separate builds.
_ONE_ARTIFACT_LANGS = ("java", "dotnet", "csharp", "go", "rust", "cpp", "c",
                       "shell", "", "unknown")

#: Languages whose emitter ACTUALLY reads AGENTVISION_HOOKS at runtime. Only
#: Python has the gate: install_all_hooks() consults _selected_hooks() per hook.
#: Node and Ruby were previously reported as `enforced: true` here purely because
#: they are not in _ONE_ARTIFACT_LANGS, but av_emit.js / av_emit.rb contain no
#: AGENTVISION_HOOKS check at all — everything in those files is always on. That
#: told the agent its exclusions were honoured when they were not, which is the
#: one thing this project must never do: report a capability it does not have.
_HOOK_GATED_LANGS = ("python", "py", "python3")


def hooks_fail_open(language: str, selected: Optional[list] = None) -> dict | None:
    """Disclose an over-install: hooks that arm even though the plan omitted them.

    The runtime treats an empty AGENTVISION_HOOKS as "arm everything", so that a
    program is never left silently uninstrumented by a plan naming an id this
    build does not implement. Deliberate, and kept — but it means a plan that
    selects, say, only `config_dropin` still gets all five in-process hooks.

    selection_report() cannot show this: it iterates the ids the plan NAMED, and
    the leak is entirely about ids it did NOT name. Returning None means the
    plan's exclusions really are honoured.
    """
    lang = _LANG_ALIASES.get((language or "").lower().strip(),
                             (language or "").lower().strip())
    if lang not in _HOOK_GATED_LANGS:
        return None
    want = [s for s in (selected or []) if s in HOOK_SELECTABLE]
    if want:
        return None                      # a real subset -> env is set -> gated
    extra = sorted(HOOK_SELECTABLE)
    return {
        "will_also_arm": extra,
        "why": ("this plan names no in-process hook id, so AGENTVISION_HOOKS is "
                "left unset, and the runtime reads unset as \"arm everything\" "
                "rather than \"arm nothing\" — a program is never left silently "
                "uninstrumented because a plan named an id this build lacks"),
        "so": ("these WILL be live and writing to agentvision/actions.jsonl even "
               "though you did not select them. If that is wrong for this "
               "program, say so in your rationale; if it is fine, you get more "
               "than you asked for, not less"),
        "to_verify": ("read the av.bootstrap.start record in "
                      "agentvision/actions.jsonl: hooks_armed lists what is "
                      "actually live and hooks_selection_source will say "
                      "\"default (all)\""),
    }


def selection_report(language: str, selected: Optional[list] = None) -> list[dict]:
    """Answer the agent's emitter selection ID BY ID.

    `selection_enforced: false` was honest but blunt: it told an agent that its
    whole selection had not been enforced, without saying which parts were
    inherent to the single artifact anyway and which named something this language
    cannot do at all. Those are very different, and only the second is a mistake
    the agent should correct. Deliberately NOT solved by generating one artifact
    per id: for logback/Serilog that means fragmenting a single appender config,
    and for a tee there is nothing to fragment — the sink bytes are identical
    either way, so it would buy nothing on the read side and add a per-language
    matrix of generated configs to maintain.
    """
    lang = _LANG_ALIASES.get((language or "").lower().strip(),
                             (language or "").lower().strip())
    out: list[dict] = []
    for sid in (selected or []):
        if sid == "user_input":
            # NOT an in-process hook in any language, so it used to fall through to
            # the generic "maps to no runtime hook, selecting it changes nothing"
            # branch — which is FALSE and a cold model called it out as a
            # contradiction. Selecting it does something specific: the installer
            # sets capture_user_input on the profile. What it does NOT have is an
            # AGENTVISION_HOOKS gate, because the recorder is a separate daemon.
            out.append({
                "id": sid, "enforced": True,
                "how": ("enforced, but by the DAEMON — not by AGENTVISION_HOOKS. "
                        "Selecting it sets capture_user_input=true on this "
                        "profile; the input daemon then re-reads that flag every "
                        "2s and drops every physical event while it is false. "
                        "BOTH halves are required: the flag AND a running daemon "
                        "(`agentvision daemon start`). If the daemon is not "
                        "running you get SILENCE, not an error — check "
                        "av_daemon_status() before reading an empty input log as "
                        "'the human pressed nothing'.")})
        elif sid in HOOK_SELECTABLE and lang in _HOOK_GATED_LANGS:
            out.append({"id": sid, "enforced": True,
                        "how": "armed at runtime via AGENTVISION_HOOKS"})
        elif sid in HOOK_SELECTABLE and lang not in _ONE_ARTIFACT_LANGS:
            # Node / Ruby: an in-process emitter exists, but it has no gate.
            out.append({
                "id": sid, "enforced": False,
                "how": (f"recorded only — the {lang} emitter is a single "
                        "always-on file (av_emit.js / av_emit.rb) with no "
                        "AGENTVISION_HOOKS check, so this id is a facet of it "
                        "and your exclusions are NOT applied. Everything that "
                        "file hooks is live regardless of what you selected")})
        elif lang in _ONE_ARTIFACT_LANGS:
            out.append({
                "id": sid, "enforced": False,
                "how": (f"recorded only — {lang or 'this language'} builds ONE "
                        "artifact (config drop-in or stdout/stderr tee); this id "
                        "is a facet of it, not a separate build")})
        else:
            out.append({
                "id": sid, "enforced": False,
                "how": ("recorded only — this id maps to no runtime hook in this "
                        "build, so selecting it changes nothing. See "
                        "av_bridge_catalog: each option's `builds_as` says what "
                        "it actually produces")})
    return out


def build_emitter(language: str, av_root: str | Path, project_root: str | Path,
                  selected: Optional[list] = None) -> EmitterSpec:
    """Return the EmitterSpec for a detected language. Unknown → tee bridge.

    `selected` is the agent's chosen emitter ids from its bridge plan. For
    languages with in-process hooks it becomes AGENTVISION_HOOKS, so only the
    chosen hooks arm at runtime. For config/tee languages there is one artifact
    either way, and the selection is recorded rather than enforced — the emitter
    spec says which, via `selection_enforced`.
    """
    lang = (language or "").lower().strip()
    lang = _LANG_ALIASES.get(lang, lang)
    _hooks = hooks_env(selected)

    if lang == "python":
        return EmitterSpec(
            language="python", kind="autoload", autoload=True, verify="spawn_python",
            run_env=dict(_hooks),
            selection_enforced=bool(_hooks),
            files={_emitters_rel("README.txt"):
                   "Python hooks load from the project root via PYTHONPATH.\n"
                   "\n"
                   "USE: agentvision run -- python your_script.py\n"
                   "\n"
                   "A project-root sitecustomize.py is also installed, but do NOT rely\n"
                   "on it: CPython's `site` imports sitecustomize BEFORE the script's\n"
                   "own directory is added to sys.path, so a project-local copy is never\n"
                   "found for `python your_script.py`. MEASURED on Homebrew python 3.14\n"
                   "AND on a clean venv: 0 events without PYTHONPATH, 2 events with it.\n"
                   "It only helps when the project dir is already on PYTHONPATH — which\n"
                   "is exactly what `agentvision run` does for you.\n"},
            # The old text called the sitecustomize path a "BONUS zero-config" that was
            # "reliable on stock/venv Pythons" and blamed Homebrew for shadowing it. That
            # was wrong in a way that cost real debugging time: it does not work on ANY
            # CPython for `python script.py`, because site imports sitecustomize before
            # cwd joins sys.path. Verified against both interpreters above.
            load_instructions="USE `agentvision run -- python your_script.py` — this is the "
                              "supported path and it sets PYTHONPATH so the in-process hooks "
                              "load. NOTE: a plain `python your_script.py` does NOT emit. The "
                              "installed sitecustomize.py cannot change that (CPython imports "
                              "sitecustomize before the script's directory is on sys.path — "
                              "measured on Homebrew python and a clean venv alike); it only "
                              "takes effect when the project dir is already on PYTHONPATH. "
                              "av_install_verify reports `autoloads` so this is never guessed.",
            notes="In-process hooks (logging→category, stdout/stderr tee, uncaught/thread/"
                  "unraisable exceptions, lifecycle). Requires `agentvision run` (or the "
                  "emitter env) — sitecustomize autoload does NOT work for a plain "
                  "`python script.py` on any CPython.")

    if lang == "node":
        return EmitterSpec(
            language="node", kind="env_autoload", verify="spawn_node",
            files={_emitters_rel("av_emit.js"): _NODE_EMITTER},
            run_env={"NODE_OPTIONS": "--require {emitter}"},
            load_instructions="Launch via `agentvision run -- node app.js` (auto-sets "
                              "NODE_OPTIONS), or export NODE_OPTIONS=\"--require "
                              "<proj>/agentvision/emitters/av_emit.js\" yourself.",
            notes="Patches console + uncaughtException/unhandledRejection + lifecycle.")

    if lang == "ruby":
        return EmitterSpec(
            language="ruby", kind="env_autoload", verify="spawn_ruby",
            files={_emitters_rel("av_emit.rb"): _RUBY_EMITTER},
            run_env={"RUBYOPT": "-r{emitter}"},
            load_instructions="Launch via `agentvision run -- ruby app.rb` (auto-sets "
                              "RUBYOPT), or export RUBYOPT=\"-r<proj>/agentvision/"
                              "emitters/av_emit.rb\" yourself.",
            notes="Tees $stdout/$stderr + process lifecycle via at_exit.")

    if lang == "java":
        return EmitterSpec(
            language="java", kind="config", verify="emitter_present",
            files={_emitters_rel("logback-agentvision.xml"): _JAVA_LOGBACK},
            load_instructions="Point your app at the config: java "
                              "-Dlogback.configurationFile=<proj>/agentvision/emitters/"
                              "logback-agentvision.xml ...  OR just `agentvision run -- "
                              "java -jar app.jar` (stdout tee bridges it).",
            notes="Config drop-in (logback JSON appender). Tee is the no-wiring fallback.")

    if lang == "dotnet":
        return EmitterSpec(
            language="dotnet", kind="config", verify="emitter_present",
            files={_emitters_rel("serilog-agentvision.json"): _DOTNET_SERILOG},
            load_instructions="Load the Serilog section (appsettings) or the snippet in "
                              "serilog-agentvision.json, OR `agentvision run -- dotnet "
                              "run` (stdout tee bridges it).",
            notes="Config drop-in (Serilog JSON file sink). Tee is the no-wiring fallback.")

    if lang == "go":
        return EmitterSpec(
            language="go", kind="tee", verify="tee",
            files={_emitters_rel("av_emit.go"): _GO_EMITTER},
            load_instructions="`agentvision run -- ./yourprogram` — stdout/stderr is teed "
                              "+ normalized (zero effort). Optionally import "
                              "agentvision/emitters (av_emit.go) and call Emit() for rich events.",
            notes="Compiled: tee bridge by default; optional Go structured emitter.")

    if lang == "rust":
        return EmitterSpec(
            language="rust", kind="tee", verify="tee",
            files={_emitters_rel("RUST_README.txt"): _RUST_NOTE},
            load_instructions="`agentvision run -- ./target/debug/yourapp` — tee bridges "
                              "env_logger/tracing output. See RUST_README.txt for a "
                              "structured tracing-json option.",
            notes="Compiled: tee bridge by default.")

    if lang == "cpp":
        return EmitterSpec(
            language="cpp", kind="tee", verify="tee",
            files={_emitters_rel("CPP_README.txt"): _CPP_NOTE},
            load_instructions="`agentvision run -- ./yourapp` — tee bridges glog/spdlog/"
                              "plain output. See CPP_README.txt for a structured spdlog option.",
            notes="Compiled: tee bridge by default.")

    if lang == "shell":
        return EmitterSpec(
            language="shell", kind="tee", verify="tee",
            files={_emitters_rel("av_emit.sh"): _SHELL_EMITTER},
            load_instructions="`agentvision run -- ./script.sh` — tee bridges output. Or "
                              "`source agentvision/emitters/av_emit.sh` and call av_log.",
            notes="Shell: tee bridge by default; optional av_log function.")

    if lang == "php":
        return EmitterSpec(
            language="php", kind="tee", verify="tee",
            files={_emitters_rel("PHP_README.txt"):
                   "AgentVision — PHP bridging\n==========================\n"
                   "Zero-effort default: `agentvision run -- php app.php` (stdout tee; "
                   "Monolog output is recognized by the php_monolog adapter). For "
                   "structured output, add a Monolog StreamHandler pointed at "
                   "$_ENV['AGENTVISION_SINK'] ?? 'agentvision/actions.jsonl'.\n"},
            load_instructions="`agentvision run -- php app.php` — tee bridges Monolog/plain output.",
            notes="Interpreted but no zero-arg autoload; tee bridge by default.")

    # Fallback: anything else → the universal tee bridge.
    return EmitterSpec(
        language=lang or "unknown", kind="tee", verify="tee",
        load_instructions="`agentvision run -- <your command>` — stdout/stderr is teed "
                          "and normalized into agentvision/actions.jsonl for ANY program.",
        notes="Unknown language: universal tee bridge (works for anything).")
