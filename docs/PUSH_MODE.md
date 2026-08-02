# Push Mode

**AgentVision v5.1** · installed with `agentvision install-hooks` · **off until you install it**

---

## The hole Push Mode fills

Everything else in AgentVision is **pull**: the agent asks, AgentVision answers.
That has a structural hole — **the agent has to already suspect something is
wrong.** Two cases where it never will:

1. **Right after a context compaction.** The agent has forgotten AgentVision
   exists. It will not call a tool it no longer remembers.
2. **Right after its own edit.** It made a change, the change crashed the running
   program, and it has no reason to go looking.

Push Mode closes both. A tiny hook injects a few pre-digested lines at the moments
that matter, so the agent learns *"the change you just made crashed the program"*
without spending a tool call.

---

## The three rules

**1. Silent by default.** If nothing meaningful changed, **nothing is injected**.
An ambient channel that chatters is worse than no channel: it burns tokens on
every prompt and trains the agent to ignore it. Silence is the common case and it
costs exactly zero.

**2. Delta-only.** Never re-says what this session was already told. Every signal
is fingerprinted per session; a repeat is suppressed unless it *escalates* in
severity (a notice becoming an alert gets through).

**3. Hard byte caps.** Every tier has a byte ceiling enforced by truncation, so a
pathological program can never blow the context window through the hook.

---

## Tiers, with measured real examples

| Tier | Cap | Measured | When |
|---|---|---|---|
| `silent` | — | **0 bytes** | nothing to say (the common case) |
| `heartbeat` | 220 B | 212 B ≈ **53 tokens** | `SessionStart` only, rate-limited to once per 10 min |
| `notice` | 700 B | 345 B ≈ **87 tokens** | something changed worth knowing |
| `alert` | 1200 B | 412–732 B ≈ **103–183 tokens** | something is broken now |

### heartbeat — 212 bytes
```
[AgentVision] Watching SharpEmu (PS5 emulator, .NET). Visual: screen changing
(32% of the last frame). Nothing needs attention — ask av_start_here /
av_diagnose any time; don't screenshot or grep logs yourself.
```

### notice — 345 bytes
```
[AgentVision] SharpEmu (PS5 emulator, .NET) (profile 'sharpemu')
  - AgentVision cannot find the 'SharpEmu' window — it is screenshotting the full screen instead.
    -> av_capture_status()
  Visual: screen changing (32% of the last frame); changed region 1920x1440 at (160,0); 19 changed moment(s) in the last 200 frames. (latest frame #5884)
```

### alert after an edit — 732 bytes
```
[AgentVision ALERT] SharpEmu (PS5 emulator, .NET) (profile 'sharpemu')
Since your last change:
  - AgentVision froze an incident (error) at frame 5871: NullReferenceException in GuestGpu.Present — the 60.0s BEFORE it is already on disk.
    -> av_error_moment(seq=5871) · av_incidents(id='inc-error-1785261900000')
  - NEW error this session (3x): NullReferenceException: Object reference not set (GuestGpu.Present)
    -> av_error_moment(fingerprint='a1b2c3d4')
  Visual: screen changing (32% of the last frame); changed region 1920x1440 at (160,0); 19 changed moment(s) in the last 200 frames. (latest frame #5884)
  This was pushed to you for free — no tool call was spent. Use the -> calls above rather than re-deriving it.
```

### alert for a hang — the case logs cannot see — 459 bytes
```
[AgentVision ALERT] SharpEmu (PS5 emulator, .NET) (profile 'sharpemu')
  - The screen has been FROZEN for 31.5s (possible hang) since frame 5880 — no log line will tell you this.
    -> av_frame_json(seq=5880) · av_visual_events()
  Visual: screen is STATIC (~31.5s unchanged); 19 changed moment(s) in the last 200 frames. (latest frame #5884)
  This was pushed to you for free — no tool call was spent. Use the -> calls above rather than re-deriving it.
```

Every non-silent injection carries a **`Visual:` sentence** — the thing no log can
tell the agent — sourced from the real visual engine (`change_score`,
`changed_bbox`, freeze duration, blank detection, on-screen error text).

---

## Push is cheaper than pull — measured

Measured on the live bridge against a real 5884-frame session:

| | Est tokens | Tool calls | Requires the agent to suspect anything? |
|---|---|---|---|
| **PUSH: the whole alert** | **103** | **0** | **No** |
| PULL: `av_diagnose` alone | 191 | 1 | Yes |
| PULL: `av_incidents` | 147 | 1 | Yes |
| PULL: `av_digest` | 536 | 1 | Yes |
| PULL: `av_start_here` | 671 | 1 | Yes |
| PULL: `av_error_moment` | 613 | 1 | Yes |
| PULL: realistic discovery path (`av_digest` → `av_error_moment`) | **1149** | 2 | Yes |

So the same knowledge costs **1.9× more** via the cheapest single pull and **11×
more** via a realistic discovery path — and both require the agent to go looking.

**The bigger win is the silent case.** If an agent instead *polled* `av_diagnose`
on every prompt to stay current, that is ~191 tokens × every prompt, forever. Push
Mode costs **0** on every prompt where nothing is wrong, and only speaks when
there is genuinely news. Over a 50-prompt session with one real failure:

* polling: ~9 550 est tokens
* Push Mode: ~103 est tokens

*(Estimates use ~4 characters per token, the same method as `av_token_report`;
they are estimates, not tokenizer output.)*

**Latency:** ~89 ms per prompt end-to-end, measured with the real hook script
against a live bridge — of which ~31 ms is bare `python3` interpreter startup. The
bridge side is ~26 ms cold and **0.1 ms** warm thanks to a 1.2 s state cache, so
back-to-back hooks (`PostToolBatch` immediately followed by `PostToolUse`) are
essentially free.

---

## What is wired

| Event | Matcher | Why |
|---|---|---|
| `SessionStart` | `startup\|resume\|clear\|compact\|fork` | Orient at the start. **`compact` is the highest-value hook in the set** — right after a compaction is precisely when the agent has forgotten AgentVision exists. |
| `UserPromptSubmit` | *(none)* | Tell it before it answers. |
| `PostToolBatch` | *(none)* | After a batch of parallel tools resolves. |
| `PostToolUse` | `Edit\|Write\|MultiEdit\|Bash` | The did-my-change-break-it loop. Non-mutating tools are ignored twice over: by the matcher, and again inside the script. |
| `Stop` | *(none)* | Backstop. **Not installed unless you pass `--with-stop-block`, and even then blocking stays off** — see below. |

---

## The other client problem, and the second channel

Everything above is a **Claude Code hook**. In Cursor, VS Code, or any other MCP
client there is no hook mechanism, so all of it is silent — AgentVision's best
feature simply does not exist there.

MCP's own answer is a **resource-updated notification**: the server tells the
client that `agentvision://digest` has changed, and the client reads it if it
wants to. No client-specific hook, no injected text, no bytes spent unless the
client actually fetches. AgentVision implements this in the MCP server
(`python_backend/api/claude_mcp.py`) and it is subject to the same three rules
above, plus three more that exist because this is the only thing in AgentVision
that runs **without an agent asking it to**:

**4. No subscribers, no work.** The poller is started by the first client that
opens a subscription stream and stopped when the last one closes. A server with
nobody listening does nothing at all — zero polls.

**5. It never consumes.** It polls `/ambient` with `force=1`, which by design
skips `mark_surfaced`, the raw-log offset commit, and `mark_offered`. If it
consumed, it would eat the very lines the Claude Code hook was about to deliver,
and that loss would look like a program that had gone quiet.

**6. It stands down for the other channel.** If another ambient session was
injected in the last two minutes, the hook is demonstrably working and this
channel says nothing. An agent told twice cannot tell it was one event. The
count of times it stood down is reported, so "quiet because the hook has it" is
distinguishable from "quiet because it is broken".

### What it announces, and what it does not

| Tier | Announced? |
|---|---|
| `silent` | no |
| `heartbeat` | **no** — "still watching, all normal" is not worth waking a client for |
| `notice` / `alert` / `raw` | yes, as `agentvision://digest`; an incident also announces `agentvision://incidents` |

Repeats are suppressed on `content_fp` — a fingerprint built from the signals
and the actual log bytes. **Not** from the rendered text: that text embeds a live
clock (`LAST WRITE 144s AGO`), and hashing it announced the same unchanged log
twice in five seconds. The fingerprint survives a client dropping and re-opening
its stream, so a reconnect is not told the same thing again.

### Seeing whether it is working

`av_capabilities()` returns `your_client.push_channel`:

```json
{"enabled": true, "listeners": 1, "running": true, "polls": 42,
 "published": 3, "quiet_for_other_channel": 7, "errors": 0, "last_error": ""}
```

A channel that fails silently looks exactly like one with nothing to say, which
is the failure this whole project is about — so every counter is published,
including the reason for the last error.

### Limitation, stated rather than discovered

This reaches clients that open a `subscriptions/listen` stream. The MCP SDK's
high-level server does not implement the older `resources/subscribe` request —
it advertises `resources.subscribe: false` — so a client that only speaks that
older form is **not** reached. Such a client is pull-only, exactly as it is
today: nothing regresses, but nothing improves either.

### Turning it off

`AGENTVISION_SUBSCRIBE_PUSH=0` disables it entirely.
`AGENTVISION_SUBSCRIBE_POLL_S` (default 10) sets the poll interval, minimum 2.
`AGENTVISION_SUBSCRIBE_QUIET_MS` (default 120000) is how long another active
channel keeps it quiet.

---

## The Stop backstop is OFF, and here is why

A `Stop` hook can *block* the agent from ending its turn. Used well that is a
backstop: "you are about to stop, but the program you were fixing is still
crashed." Used badly it **traps the user in a loop they cannot exit** — which is
far worse than a tool that occasionally needs a nudge.

The Claude Code hooks reference documents that `Stop` can block, but **documents
no loop-guard field on the Stop payload and no retry cap** — its own
recommendation is to implement your own counter. Because the harness-level guard
is not guaranteed, **Push Mode ships with the Stop hook not installed and
blocking disabled.**

If you opt in (`agentvision install-hooks --with-stop-block` *and*
`AGENTVISION_AMBIENT_STOP_BLOCK=1`), five independent guards apply, **all** of
which must pass before a single block is issued:

1. **Disabled by default** — two separate opt-ins are required.
2. **Harness re-entry flag honoured** if it exists (defence in depth; not relied on).
3. **Only genuinely critical kinds may block** — `crash`, `fatal`, `hang`,
   `program_died`. A merely-degraded health score **can never** block. That is the
   difference between a backstop and a trap.
4. **A persistent per-session budget** (`max_stop_blocks`, default **1**; `0`
   disables). It is written to disk, so a bridge restart mid-session cannot reset
   it — which is exactly the situation in which a loop would otherwise form.
5. **The signal is marked surfaced *before* the block is returned**, so the very
   same incident can never justify a second block.

All five are asserted in `python_backend/api/test_ambient.py`, including the
negative cases: a degraded score never blocks, a notice-tier signal never blocks,
the budget survives an in-memory reset, and with a budget of 3 it fires exactly
3 times across 10 distinct new incidents and then never again.

---

## Install / uninstall

```bash
agentvision install-hooks              # user scope: ~/.claude/settings.json
agentvision install-hooks --dry-run    # show the plan, change nothing
agentvision install-hooks --scope project --project /path/to/repo
agentvision install-hooks --with-stop-block    # opt into the Stop hook

agentvision uninstall-hooks            # removes exactly AgentVision's entries
agentvision push-mode status
```

Hooks live in **`~/.claude/settings.json`** (or `<project>/.claude/settings.json`).
This is a *different file* from `~/.claude.json`, where MCP servers live —
Push Mode never touches that file.

The installer:

* takes a **timestamped backup** before every write
* **merges non-destructively** — your other settings keys and *other people's
  hooks on the same events* are preserved
* **tags** its entries (`statusMessage: "AgentVision Push Mode"` plus the script
  path), so uninstall removes exactly its own and nothing else
* is **idempotent** — installing twice refreshes in place rather than duplicating
* **re-validates the JSON** after writing and restores the backup on any doubt
* **refuses** to touch a settings file it cannot parse, rather than overwriting it

> ⚠️ **Restart Claude Code after installing or uninstalling.** Hooks are read at
> startup.

---

## Every way to turn it off

1. **GUI** — the **📡 Push Mode** button → *Enable / Disable*. Instant, no restart.
2. **CLI** — `agentvision push-mode off`
3. **Env** — `AGENTVISION_PUSH_MODE=0`
4. **Flag file** — set `enabled: false` in `log/agentvision_push_mode.json`
5. **Remove entirely** — `agentvision uninstall-hooks` (then restart Claude Code)
6. **Stop the bridge** — with no bridge, every hook exits silently

Options 1–4 leave the hooks installed but mute; the script checks the switch
*before* making any network call.

---

## Failure behaviour — it cannot break your session

The hook script's contract, all of it verified in
`python_backend/api/test_push_routes.py`:

| Situation | Behaviour |
|---|---|
| Bridge down | exit 0, no output, no stderr |
| Bridge slow | 1.2 s socket timeout + 2.5 s wall-clock deadline, then exit 0 silently |
| Malformed / empty / hostile stdin | exit 0, no output |
| Unwired event | exit 0, no output |
| Push Mode disabled | exit 0, no output, **no network call** |
| Any unhandled exception | caught at top level, exit 0 |

It writes to **stderr only** when `AGENTVISION_HOOK_DEBUG=1`, because stderr on a
non-zero exit is surfaced to the user. It imports **stdlib only** — no `requests`,
no `flask`, no `Pillow` — because it runs under whatever `python3` the user's PATH
resolves to, not necessarily AgentVision's venv.

---

## Seeing what it will say

* **GUI** — the **📡 Push Mode** window shows a live preview of the next
  injection, the wired events, and the kill switch.
* **Tool** — `av_ambient(force=True)` returns exactly what a hook would inject.
* **HTTP** — `GET /ambient?force=1`
* **Reset the memory** so it speaks again — `POST /ambient/reset`

`force=1` bypasses the already-said suppression and the rate limit (byte caps
still apply), which is why the preview can show you a message a real hook would
currently withhold.

---

## Tuning

| Env var | Default | Meaning |
|---|---|---|
| `AGENTVISION_PUSH_MODE` | *(on)* | Master switch |
| `AGENTVISION_AMBIENT_HEARTBEAT_CAP` | 220 | Byte cap |
| `AGENTVISION_AMBIENT_NOTICE_CAP` | 700 | Byte cap |
| `AGENTVISION_AMBIENT_ALERT_CAP` | 1200 | Byte cap |
| `AGENTVISION_AMBIENT_GAP_HEARTBEAT_MS` | 600000 | Min gap between heartbeats |
| `AGENTVISION_AMBIENT_GAP_NOTICE_MS` | 45000 | Min gap between notices |
| `AGENTVISION_AMBIENT_GAP_ALERT_MS` | 10000 | Min gap between alerts |
| `AGENTVISION_AMBIENT_TTL_MS` | 1200 | State cache — makes burst hooks free |
| `AGENTVISION_AMBIENT_STOP_BLOCK` | 0 | Enable Stop blocking (second opt-in) |
| `AGENTVISION_AMBIENT_MAX_STOP_BLOCKS` | 1 | Persistent per-session block budget |
| `AGENTVISION_HOOK_TIMEOUT_S` | 1.2 | Socket timeout |
| `AGENTVISION_HOOK_DEADLINE_S` | 2.5 | Wall-clock deadline |
| `AGENTVISION_HOOK_JSON` | 0 | Emit `hookSpecificOutput.additionalContext` instead of plain stdout |
| `AGENTVISION_HOOK_DEBUG` | 0 | Trace to stderr |
