# AgentVision — paste-ready agent instructions

Copy the block below into your project's `CLAUDE.md` (or any agent's system
prompt). It tells a Claude session how to use AgentVision, and — just as
importantly — what **not** to do.

The AgentVision MCP server also sends a condensed version of this automatically at
connection time (`instructions` on the MCP handshake), so a session that has the
server configured already gets the gist. Pasting this into `CLAUDE.md` makes it
stickier and adds the project-specific detail.

---

## Copy from here

```markdown
## Debugging with AgentVision

AgentVision is a local debug flight recorder for the program I am working on. It
screenshots that program on a timer, parses every log it declares, and time-aligns
the two. All of that runs on my CPU and costs you NOTHING.

**Your tokens should go to CODING, not to observing.** Do not do the observing
yourself:

- ❌ Do NOT take your own screenshots — capture is already running.
- ❌ Do NOT grep or tail raw logs — they are already parsed, merged and normalized.
- ❌ Do NOT page through frames one at a time — `av_visual_changes` collapses them.
- ❌ Do NOT hand-correlate a timestamp to a frame — `av_error_moment` did it.
- ❌ Do NOT ask me to reproduce a bug before checking `av_incidents` — the seconds
     before each failure are already frozen on disk.

### FIRST, THE PART THAT SURPRISES EVERYONE: you build the bridge

AgentVision does **not** decide what logging to install into a program. **You do,
once, on first connection.** It refuses to guess, because guessing means picking
the same logging for a web server and a GPU emulator.

Until you commit a plan, `av_capture_start` and `av_install_project` are
**REFUSED**. The refusal is **HTTP 200** with `"error": "BRIDGE_NOT_BUILT"` and
`"started": false` — so **read the body, not the status code**.

```
av_bridge_status()    # PROVISIONAL or BUILT? If BUILT you are done — skip this.
av_bridge_catalog()   # the menu: emitters, adapters, tools + this program's own
                      # code evidence, and the catalog_token you need next
av_bridge_commit(plan={
    "catalog_token": "<from the catalog>",
    "emitters":  ["lifecycle"],          # [] is fine if it already logs well
    "why":       {"lifecycle": "nothing marks run boundaries in this project"},
    "rationale": "one line on why this set fits THIS program",
    "tools":     {"primary": ["av_diagnose", "av_log_raw"],
                  "not_relevant": {"av_ui_tree": "headless, no window"}}
})
```

**This happens once per program, ever.** The plan is saved inside the target
project (`agentvision/<profile>/.av_bridge_plan.json`). Restarting AgentVision,
the bridge server, or your own session does **not** re-trigger it. If
`av_bridge_status` says `BUILT`, **never plan that program again**.

Full detail: `docs/BRIDGE_PROTOCOL.md`. New to AgentVision entirely?
`docs/AI_START_HERE.md`.

### Workflow, once the bridge is BUILT

1. `av_start_here` — orient. What is being watched, is the bridge built, is
   capture alive. **Read this first, and do what its `DO_THIS_NEXT` says.**
2. `av_log_raw` — the raw log, verbatim and uninterpreted. AgentVision never
   hides a line from you; when you want ground truth, this is it.
3. `av_diagnose` — when something is wrong. Ranked root-cause hypotheses with
   evidence and the exact follow-up calls.
4. `av_incidents` — failure windows the recorder already froze (including the run-up).
5. `av_visual_changes` — review what the screen did, instead of browsing frames.
6. `av_error_moment` — one call for a specific failure: the error, the frame, the
   changed pixels, the on-screen text, the merged log window, the state delta and
   the source code.
7. `av_replay` — step through a sequence without re-running anything.
8. `av_session_report` — wrap up or hand off.

### The token rule: cheapest sufficient tier first

Sending a full screenshot costs hundreds-to-thousands of visual tokens. Published
comparisons put text-vs-pixels at ~25× cheaper for a few points of accuracy, so
escalate deliberately:

1. `av_ui_tree` — exact element text + coordinates as JSON (cheapest and most
   precise, when the app exposes an accessibility tree).
2. `av_visual_changes` / `av_frame_json` — JSON only, no image bytes.
3. `av_frame_json(seq, thumbnail=True)` — a tiny thumbnail.
4. `av_frame_region(seq)` — ONLY the pixels that changed (or `bbox='dense'` for the
   most information-dense region).
5. `av_get_frame(seq)` + read the PNG — full image, last resort.

**Escalate to pixels when the question genuinely needs them** — icon colour,
spatial layout, progress indicators, rendering glitches and visual corruption are
invisible to text, and a debugging tool must be able to see them. Just do not start
there.

`av_token_report` shows what this actually saved, with the estimation method
stated.

### If Push Mode is on

You may see lines beginning `[AgentVision]` or `[AgentVision ALERT]` appear
unprompted — at session start, after a compaction, before a prompt, or after an
Edit/Write/Bash. That is AgentVision telling you something for FREE; no tool call
was spent. Treat it as high-signal:

- It is silent unless something genuinely changed, so if it spoke, read it.
- It never repeats itself, so a thing said once still applies.
- Follow the `->` calls it names rather than re-deriving the same facts.
- An alert after your own edit means your change is the prime suspect.

### Things that are easy to miss

- A **hang leaves no log line**. `av_visual_events` detects `screen_frozen` from the
  screen itself; log-only analysis cannot.
- `av_ui_tree` returning `available:false` or `likely_custom_drawn:true` is a
  legitimate answer, not a failure — games, emulators and canvas/WebGL apps expose
  no tree. Fall back to `av_frame_json` / `av_ocr_frame` / `av_frame_region`.
- OCR is optional. If tesseract is not installed, `ocr_text` is `null` with a
  reason — read the image or the UI tree instead.
- Ask me how many screenshots per second I want before starting capture
  (`av_status.capture_rate` shows the supported range).
- `av_ambient(force=True)` shows what Push Mode would say right now, if you want
  to check the ambient channel deliberately.
```

## To here

---

## Optional: project-specific additions

Add whatever is true for your project, for example:

```markdown
- AgentVision profile for this project: `<profile name>`
- Start the bridge with: `<command>`
- The program's logs live at: `<paths>`
- This app draws its own UI (game/emulator/canvas), so `av_ui_tree` will be empty —
  go straight to `av_frame_json` / `av_frame_region`.
```

---

## MCP prompts

If your client surfaces MCP prompts (slash commands in many clients), the server
also exposes three ready-made workflows, so you may not need to explain the process
at all:

| Prompt | What it does |
|---|---|
| `diagnose_running_program` | Full triage workflow in the cheap order; takes an optional symptom. |
| `review_capture_run` | Survey everything that happened on screen without opening images. |
| `before_first_capture` | The pre-flight log-coverage check for a new program. |

## MCP resources

The server exposes these as resources, so a client can attach them as context
without spending a tool call:

| URI | Contents |
|---|---|
| `agentvision://start_here` | Orientation + workflow + token rule |
| `agentvision://digest` | Ranked triage digest |
| `agentvision://incidents` | Failure windows already frozen |
| `agentvision://visual_changes` | Only the moments the screen changed |
| `agentvision://frame/latest.json` | The latest frame as JSON (no image bytes) |
| `agentvision://capabilities` | What AgentVision can do right now |
| `agentvision://token_report` | Measured accounting of the savings |
