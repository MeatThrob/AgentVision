# Token Efficiency: Research Basis for AgentVision's Cheap Path

**Written:** 2026-07-28 · **Applies to:** AgentVision v5.1

This document records the published evidence behind AgentVision's design thesis,
what we adopted from it, what we deliberately did not, and the numbers we
measured ourselves on this machine.

---

## The thesis

AgentVision's competitive advantage is an **asymmetry of cost**.

Capturing at 10 fps, hashing, diffing, OCR-ing, parsing and correlating are
effectively **free** — local CPU on the user's machine. Every token the AI spends
looking at raw pixels or scrolling raw logs is **expensive**. So the program's job
is to do all the cheap heavy lifting up front and hand the agent the smallest
possible high-signal JSON.

The agent should never (a) take screenshots itself, (b) eyeball dozens of
near-identical frames, (c) grep raw logs, or (d) manually correlate a timestamp to
a frame. It asks one question and gets a pre-digested answer, so its tokens go to
**coding**, not to observing.

---

## 1. The thesis is validated by published numbers

### 1.1 Text/JSON vs pixels: ~25× cheaper for a ~4–5 pp accuracy delta

"Do LLMs Need to See Everything?" ([arXiv 2604.17817][1]) compared screen-text
versus screenshot inputs for mobile automation:

| Input | Task success | Cost per task (o4-mini) |
|---|---|---|
| Text only | 26.7–29.3% | **$0.01** |
| Multimodal (screenshots) | 32.0–33.3% | **$2.46** (~25×) |

A ~4–5 percentage-point accuracy gain costs ~25× more. **This is why a tiered
design is the right default, not a compromise:** answer from JSON when JSON
suffices, and spend pixels only where they actually change the answer.

### 1.2 Text-only fails in specific, enumerable ways — so pixels must stay reachable

The same work breaks down *why* text-only fails, and the categories are
actionable rather than vague:

* **System-level failures (42.7%)** — the element simply is not in the
  accessibility tree: hidden search bars, unlabelled buttons, custom-drawn UIs.
* **Agent-level failures (25–30.7%)** — non-textual visual cues: icon **colour**,
  spatial **layout**, progress indicators.
* Incomplete or ambiguous on-screen text.

For a *debugging* tool this matters even more than for automation. Rendering
glitches, layout breakage and visual corruption are precisely category two — they
are invisible to text. So the escalation path to real pixels is a **documented
part of the contract**, never silently dropped.

### 1.3 Temporal visual redundancy is real and large

ReVision ([arXiv 2605.11212][2]) measured **36–56% of visual tokens redundant
between consecutive agent steps** (avg 45.4% of patches unchanged) and trained a
patch-selector for a 46% token reduction.

**The key insight for AgentVision:** those are steps *seconds* apart. At 10 fps
consecutive frames are ~99% identical. So a free block-diff plus a perceptual hash
harvests far **more** redundancy than their trained model — and costs nothing.
`av_visual_changes` is the single highest-value feature in this release.

### 1.4 ReVision's design lesson: never drop without a retrieval handle

ReVision keeps the first frame of a window intact, filters later ones, and notes
the model must be able to **recover** omitted visual information.

We adopted this literally. Every collapsed run in `av_visual_changes` carries
`seq_range`, `ts_range_ms`, `max_change_score` and a union `changed_bbox` — the
handles needed to escalate to the real frame. The response text says escalation is
available, and `min_change=0.0` disables collapsing entirely.

We went one step further after a test caught a real false-negative risk: runs are
typed `identical` (nothing moved) versus `minor_change` (something small moved,
below threshold). A one-line error appearing on screen is never reported as
"nothing happened".

### 1.5 Accessibility-tree-first is the industry recommendation

The 2026 browser-automation landscape review ([Zylos][5]) puts the text-vs-vision
cost differential at **10–20×** and recommends: *default to accessibility tree
extraction, fall back to vision only when necessary.* One cited result: intelligent
DOM pruning cut input tokens **97.9%** while holding F1 at 88.1% with a 0.6B model.

That pruning caveat is the whole game — see the measured trap in §4.3.

### 1.6 Entropy quadtrees for non-uniform screen content

AQuaUI ([arXiv 2605.19260][3]) subdivides a screen by Shannon entropy: uniform
regions stay coarse, visually complex regions recurse. Reported ~50% visual-token
reduction, and it suits GUI screenshots specifically because they pair large flat
panels with small dense text regions.

### 1.7 Why MCP tools go unused — measured

"MCP Tool Descriptions Are Smelly!" ([arXiv 2602.14878][4]) found **97.1% of real
MCP tool descriptions have at least one smell**: unclear purpose (56% of tools),
unstated limitations, missing usage guidelines, opaque parameters, underspecified,
no exemplars. Augmenting descriptions measured **+5.85 pp task success** and
**+15.12% evaluator-level performance**.

Critical caveats from the same paper, which we honoured:

* Augmentation **increased execution steps by 67.46%** and **16.67% of cases
  regressed**. Do not blindly bloat.
* The ablation found **removing examples alone did not degrade performance**.

So our docstring template is **Purpose → When to use / not use → Limitations →
Parameter roles**, with examples kept minimal or absent.

---

## 2. Positioning: AgentVision is the inverse of "agent observability"

Every existing AI-agent observability product — AgentOps, Langfuse, Braintrust,
Arize, Honeycomb Agent Timeline — instruments **the AI agent itself**: LLM calls,
traces, token spend, reasoning chains.

**AgentVision is the inverse: it instruments the program being debugged, for the
benefit of the agent.** No direct competitor was found doing that with
time-aligned visual + log capture.

The consequence for design: what is worth stealing from that space is the
**mechanisms** — flight recorder, session replay, span/trace correlation, turning a
failure into a reproducible case — not the product shape.

---

## 3. Adopt / Consider / Skip

| Idea | Source | Verdict | Status in v5.1 |
|---|---|---|---|
| Perceptual hash (dHash) frame dedup | §1.3, pHash/dHash literature | **ADOPT** | ✅ Two-axis 64-bit dHash, pure Pillow |
| Block-diff change score + changed bbox | §1.3 | **ADOPT** | ✅ 16×16 grid, ~1 ms marginal at 1080p |
| Collapse identical runs, keep retrieval handles | §1.4 | **ADOPT** | ✅ `av_visual_changes`, typed runs |
| Images-as-JSON descriptor (no pixels) | §1.1 | **ADOPT** | ✅ `av_frame_json` |
| Crop to the changed region only | §1.1, §1.2 | **ADOPT** | ✅ `av_frame_region` |
| Tiered escalation JSON → thumb → region → full | §1.1, §1.2 | **ADOPT** | ✅ Documented in every relevant docstring |
| One-call pre-correlated failure bundle | §2 (crash bundlers) | **ADOPT** | ✅ `av_error_moment` |
| Flight recorder: ring buffer + pre-error freeze | Honeycomb [6], §2 | **ADOPT** | ✅ `av_incidents`, rolling window + pinning |
| Session replay / time-travel walk | Replay, rrweb, §2 | **ADOPT** | ✅ `av_replay` |
| Accessibility tree as cheap text | §1.5 | **ADOPT** | ✅ `av_ui_tree` (macOS verified; Win/Linux implemented) |
| Aggressive a11y-tree pruning | §1.5 (97.9%) | **ADOPT** | ✅ 4 pruning rules; 14 → 5 nodes measured |
| Semantic UI diff instead of pixel diff | §1.5 | **ADOPT** | ✅ `av_ui_diff` |
| Entropy quadtree region selection | §1.6 | **ADOPT** | ✅ `content_map`, `bbox='dense'` |
| Honest token accounting | own requirement | **ADOPT** | ✅ `av_token_report`, per-payload `token_math` |
| MCP prompts + resources + annotations | [7] | **ADOPT** | ✅ 3 prompts, 7 annotated resources |
| Docstring template (Purpose/When/Limits/Params) | §1.7 | **ADOPT** | ✅ Applied to new + key existing tools |
| Set-of-Mark: number elements on the image | SoM literature | **CONSIDER** | ⬜ Deferred — needs a11y bboxes to be reliable, which is exactly what is unreliable on custom-drawn UIs. `av_ui_tree` already returns exact coords as text. |
| Learned/model-based patch selection | §1.3 (ReVision) | **SKIP** | Requires a trained model; our free diff harvests more redundancy at 10 fps (§1.3). |
| OpenTelemetry logs/traces/spans + exemplars | OTel spec | **CONSIDER** | ⬜ Deferred. AgentVision already has `trace_id` correlation (`av_trace_timeline`) and a unified event schema; adopting OTLP wire format would help interop but is a large surface with no immediate token win. |
| rrweb-style DOM recording | rrweb | **SKIP** | Web-only; AgentVision is process/OS-agnostic by design. |
| Progressive/tiered image detail on one image | tiling literature | **CONSIDER** | ⬜ Partly covered by thumbnail + region + quadtree. |
| Instrumenting the agent's own LLM calls | AgentOps etc. | **SKIP** | That is the inverse product (§2). Out of scope. |
| Bloating every docstring with examples | §1.7 | **SKIP** | The paper's own ablation found examples did not help, and augmentation raised step count 67% with 16.7% regressions. |

---

## 4. What we measured ourselves

All numbers from this machine (macOS, Apple Silicon), reproducible via
`run_all_tests.py`.

### 4.1 Per-frame analysis cost — the free-work claim

Decoding the PNG is ~90% of visual-analysis cost, and the capture loop **already**
decoded it for the blank/black health check. So the analysis shares that single
decode (`visual_engine.analyze_with_health`):

| Frame size | Health check alone | Health + full visual analysis | **Marginal** |
|---|---|---|---|
| 1280×800 | 4.89 ms | 5.72 ms | **0.82 ms** |
| 1920×1080 | 9.42 ms | 10.73 ms | **1.31 ms** |
| 2560×1440 | 16.10 ms | 16.84 ms | **0.73 ms** |
| 3840×2160 | 29.18 ms | 39.68 ms | **10.51 ms** |

Budget at 10 shots/sec is 100 ms/frame. Perceptual hashing + change detection +
structural summary costs **~1 ms** at ordinary resolutions. The health verdict is
byte-identical to the pre-existing `platform_shim.image_health` (asserted in
`test_visual_engine.py`), so nothing regressed.

### 4.2 Payload comparison — the whole point

Claude's documented image cost is `ceil(w/28) × ceil(h/28)` visual tokens, capped
per model tier (high-res tier: 2576 px long edge / 4784 tokens). Text estimated at
~4 chars/token. Both are **estimates**, stated as such everywhere they appear.

| What you send | 1280×800 frame | Ratio |
|---|---|---|
| Full frame as an image | **1334 est visual tokens** | 1.0× |
| `av_frame_json` descriptor (no image) | **~350–480 est tokens** | ~0.3× |
| Changed-region crop | proportional to the crop | ≪ 1× |
| 12-frame run via `av_visual_changes` | **3239 chars ≈ 810 est tokens** | vs 16 008 for 12 full frames |

**Honest caveat:** on a *small* image the descriptor can cost more than the image
(a 480×360 frame is only 234 visual tokens). The cheap path pays off at real screen
resolutions. `av_token_report` reports the ratio rather than asserting a win.

### 4.3 The accessibility-tree trap — measured, and mitigated

The §1.5 pruning warning is not theoretical. Our first implementation with a
400-node budget produced:

* **Finder, 400 nodes → 63 493 chars ≈ 15 873 est tokens.**
* A 4K screenshot is **4784** tokens.

**The unpruned tree cost 3.3× MORE than the screenshot it was meant to replace.**

Fixes applied: four pruning rules (drop invisible/zero-area; promote children of
text-free containers; drop text-free leaves with no actionable role; node cap),
depth cap, a 150-node default budget, and dropping verbose slash-paths from flat
rows. After that:

| Window | Raw nodes | Pruned | Payload | Est tokens | vs 2560×1440 screenshot |
|---|---|---|---|---|---|
| Finder (file list) | 600 | 150 | 10 680 chars | **2670** | 0.56× (1.8× cheaper) |
| Terminal | 14 | 5 | 753 chars | **81–189** | 0.02–0.04× (**25–59× cheaper**) |

`av_ui_tree` returns `cost.cheaper_than_screenshot` and a plain-English
`cost.verdict`, so the tool **tells you when it is not the cheap option** instead
of quietly costing more.

### 4.4 Entropy quadtree tuning — a non-obvious finding

τ is in **bits of Shannon entropy** over a luma histogram, and the useful range for
*screens* is far lower than for photographs. A 1920×1080 frame with a dense
360×240 block of error text measured **0.41 bits globally**. Sweep:

| τ | Regions | Dense | Coverage | Densest region |
|---|---|---|---|---|
| 0.20 | 40 | 22 | 8.5% | the text block |
| **0.35** | **34** | **18** | **7.0%** | **the text block** ✅ |
| 0.60+ | 1 | 0 | 0.0% | never subdivides — useless |

Default τ = **0.35**. A photographic value (3+) never subdivides a UI screenshot.
Documented in-code so nobody "corrects" it upward.

### 4.5 Two real bugs the tests caught

1. **Classic dHash has a blind axis.** A horizontally-uniform screen (full-width
   bars — common in terminals and splash screens) has left == right everywhere and
   hashed to `0000000000000000`, so visibly different screens **collided**. Fixed
   with a two-axis hash (32 horizontal bits + 32 vertical bits, same 64-bit width).
   Regression-tested.

2. **A 2% change threshold silently swallowed small errors.** On a 1280×800 screen
   a 90×60 patch is ~1.6% of the grid — below the original `MIN_CHANGE = 0.02`, so
   an appearing error banner collapsed into "no change". Fixed by lowering the
   default to 0.008 (~2 grid cells) **and** by never discarding sub-threshold
   frames — they collapse into a typed `minor_change` run that keeps its
   escalation handles.

---

## Sources

[1]: https://arxiv.org/abs/2604.17817
[2]: https://arxiv.org/abs/2605.11212
[3]: https://arxiv.org/pdf/2605.19260
[4]: https://arxiv.org/abs/2602.14878
[5]: https://zylos.ai/research/2026-04-05-browser-automation-ai-agents-2026-landscape/
[6]: https://www.honeycomb.io/blog/agent-timeline-flight-recorder-for-your-ai-agents
[7]: https://workos.com/blog/mcp-features-guide

1. **"Do LLMs Need to See Everything?"** — arXiv [2604.17817][1] — screen text vs
   screenshots; ~25× cost differential; failure taxonomy.
2. **ReVision** — arXiv [2605.11212][2] — temporal visual redundancy between agent
   steps (36–56%); keep-first-frame + recoverability design lesson.
3. **AQuaUI** — arXiv [2605.19260][3] — entropy-quadtree visual-token reduction
   (~50%) for non-uniform GUI screenshots.
4. **"MCP Tool Descriptions Are Smelly!"** — arXiv [2602.14878][4] — 97.1% of tool
   descriptions have a smell; +5.85 pp from augmentation; the 67.46%-more-steps and
   16.67%-regression caveats.
5. **Browser automation for AI agents: 2026 landscape** — [Zylos][5] —
   accessibility-tree-first recommendation; 10–20× differential; 97.9% DOM-pruning
   result.
6. **Agent Timeline: a flight recorder for your AI agents** — [Honeycomb][6] —
   flight-recorder mechanism.
7. **MCP features guide** — [WorkOS][7] — prompts, resources, and resource
   annotations (audience / priority / lastModified).

Also consulted for the token arithmetic: the Claude vision documentation's
`ceil(w/28) × ceil(h/28)` visual-token rule and per-tier caps (standard: 1568 px /
1568 tokens; high-resolution, Claude 4.7+: 2576 px / 4784 tokens).
