# AgentVision

**A debug flight recorder for AI coding agents.** It watches one program you are
debugging — screenshots its window on a timer, parses its logs with 658 format
adapters, and time-aligns the two — then exposes the result to an AI agent as
94 MCP tools. A second, machine-wide flight recorder catches what no
per-program tool survives: full-PC crashes (thermal, PSU, CPU — see
[the hardware black box](docs/HARDWARE_BLACKBOX.md)).

Everything runs locally on your own CPU. Capturing, hashing, diffing and parsing
are free; the only expensive resource is the agent's context, so AgentVision
spends its own compute to keep that small.

```
your program ──┬── window screenshots ──┐
               │                        ├── one time-aligned timeline ── 94 av_* MCP tools ── your AI agent
               └── logs (658 adapters) ─┘
the machine ───── temps/fans/rails/watts, fsync'd ── crash capsule + verdict ──┘
```

---

## Why this exists

An AI agent debugging a running program is working blind. It can read your source
and it can run commands, but it cannot see the window, and it only learns what a
log says if it thinks to go and read the right file at the right moment.

AgentVision gives it both halves at once, on one timeline: what the program
printed, and what the screen looked like when it printed it.

The hard part is not collecting that. It is handing it over without corrupting it.
Long contexts measurably degrade — every frontier model loses accuracy as input
grows, well before its window fills, and a model that takes a wrong turn in a
multi-turn conversation tends not to recover. A debug tool that invents one detail
is worse than no tool, because the agent commits to it. So the governing rule
here is:

> **AgentVision may not assert anything it did not verify.**

That rule is not aspirational. It is enforced in code and in 60 test suites, most
of which exist because a specific version of this tool once said "healthy" while
the program was failing.

## What that looks like in practice

- **A summary never replaces evidence.** Collapsing 21,982 identical log lines to
  `line [x21982]` is allowed — nothing is lost. Dropping, re-levelling or ranking a
  *distinct* line is not. Raw output is always available and is never withheld.
- **Silence is distinguished from absence.** "OCR read the screen and found no
  error" and "OCR could not read the screen" are different facts, tracked
  separately, because conflating them once made a frame showing an error dialog
  the first thing deleted.
- **Machine-read values are corroborated, not quoted.** OCR misreads
  `0x5D80000` as `0x5080000` and reports full confidence while doing it, so
  screen-read hex, ids and counts are checked against the time-aligned log and
  marked `appears_in_log: true|false` — never `verified`.
- **Retractions are as loud as claims.** If AgentVision told your agent the
  program died and it is now running, it says so, quotes its own earlier words,
  and states a counted observation — and stays silent when it merely stopped
  looking.
- **Truncation is always reported.** A scan that did not finish says so, because
  "signal absent" and "file never opened" must not look the same.
- **A default is never dressed up as a decision.** Some choices are the user's —
  the frame rate, consent to record their keystrokes — so AgentVision asks them
  directly where the MCP client supports it. Where it cannot ask, it uses its
  documented fallback and labels it as one: only `how: "asked"` means a human
  chose.

## The part that surprises people

**AgentVision does not decide what to install into your program. Your agent does,
once.** On first connection it refuses to guess and returns a catalog: every
emitter, adapter and tool it could use, alongside real evidence scanned from your
code (71 signals — does it read keypresses, own a render loop, swallow
exceptions, drive a GPU from Python, run in a browser). The agent commits a plan
naming what it chose and why. AgentVision builds exactly that.

It refuses lazy plans: a stale catalog token, a selection with no reason, or
"install everything" is rejected. The gate fires **once per program, ever**.

---

## Install

Requires Python 3.11+.

```bash
pip install -r requirements.txt
```

Then follow the guide for your platform — each covers registering the MCP server
with Claude Code and granting the OS screen-capture permission, both of which
fail *silently* if skipped:

- **macOS** — [`SETUP.md`](SETUP.md)
- **Windows** — [`SETUP-Windows.md`](SETUP-Windows.md)
- **Linux** — [`dist/linux/`](dist/linux) (X11 and Wayland notes, init templates)

Optional but recommended: `pip install -e .` gives you the `agentvision`
command (`agentvision doctor`, `agentvision run -- <cmd>`), which works from any
directory. Without it, invoke the CLI **by its script path**:

```bash
python3 /path/to/AgentVision/python_backend/cli.py doctor
```

`python3 -m python_backend.cli` also works, but only from the AgentVision folder
or with `PYTHONPATH` set — and that is the wrong directory for
`agentvision run`, which has to be run where your program is so it can find the
project it belongs to. The script-path form has neither constraint.

Ships with **no saved programs** — you add your own.

## If you are an AI agent reading this

Call `av_start_here()`. It reports the target program, whether the bridge is
built, and your exact next call. Then read
[`docs/AI_START_HERE.md`](docs/AI_START_HERE.md) and
[`docs/BRIDGE_PROTOCOL.md`](docs/BRIDGE_PROTOCOL.md) — the first-connection
contract is written for you, including what to do when the active profile is not
the program you were asked about.

Alongside the 94 tools there are 12 `agentvision://` resource URIs, so heavy
artifacts — the ~200 KB catalog, a single frame, one frozen incident — can be
addressed rather than pasted into your context.

## Other MCP clients

Everything works anywhere MCP does. Two things vary by client, and
`av_start_here()` reports both under `your_client` rather than leaving you to
find out by having something quietly not happen:

- **Asking the user.** Where the client supports elicitation, AgentVision puts
  the question to them itself. Where it does not, it says so.
- **Being told without asking.** [Push mode](docs/PUSH_MODE.md) is a Claude Code
  hook. Other clients get the same state through resource-updated notifications
  — same silence-by-default rules, and it stands down when the hook is already
  doing the job.

## Documentation

| | |
|---|---|
| [What it is, in depth](docs/WHAT_IS_AGENTVISION.md) | the whole design, code-grounded |
| [AI start here](docs/AI_START_HERE.md) | cold-start guide for an agent |
| [Bridge protocol](docs/BRIDGE_PROTOCOL.md) | the first-connection contract |
| [Logs & emitters](docs/LOGS_AND_EMITTERS.md) | what to build into a program, and why |
| [Adapters guide](docs/ADAPTERS_GUIDE.md) | the 658 log formats, and adding your own |
| [MCP tools reference](docs/MCP_TOOLS_REFERENCE.md) | all 94 tools (generated from code) |
| [Hardware black box](docs/HARDWARE_BLACKBOX.md) | diagnosing full-machine crashes (thermal / PSU / CPU) |
| [Push mode](docs/PUSH_MODE.md) | how state reaches an agent without being asked |
| [Token efficiency research](docs/RESEARCH_TOKEN_EFFICIENCY.md) | the measurements behind the design |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | when it is not working |

## Tests

```bash
python3 run_all_tests.py
```

60 suites. They are worth reading as documentation of real failures: each one
tends to encode a specific way this tool once misled its caller.

One of them, `bridge_gate`, tests the first-connection contract and so needs a
live bridge; it SKIPs (loudly, saying how to run it) if none is listening. To
include it:

```bash
python3 python_backend/api/bridge_server.py --no-autocapture &
python3 run_all_tests.py
```

## Platform support

| | capture | input recording | notes |
|---|---|---|---|
| **macOS** | window backing store — survives occlusion *and* minimisation | ✅ | best supported; the platform it is developed on |
| **Windows** | `mss` region grab of the window rect | ✅ | occlusion-sensitive; blind to minimised windows |
| **Linux/X11** | region grab | ✅ (evdev) | |
| **Linux/Wayland** | portal-based | ✅ (evdev) | no per-window enumeration — a compositor restriction |

The Windows and Linux ports are built and unit-tested but have not been re-run
end to end since a recent large refactor. macOS is the verified path.

If you are on Windows and want to help close that gap, start with
[`docs/WINDOWS_PORT_COMPLETION.md`](docs/WINDOWS_PORT_COMPLETION.md) — it lists
what is done, the one highest-risk path to test first, and the traps that already
cost time so you do not rediscover them.

## Licence

MIT — see [LICENSE](LICENSE).
