#!/usr/bin/env python3
"""
AgentVision test aggregator.

Runs every automated suite and reports one consolidated result. The stdlib-only
suites (schema, diagnostics, log adapters, language detection) run anywhere. The
bridge-route suite is included only when its runtime deps (flask/psutil/pillow)
are importable; otherwise it is SKIPPED (not failed), so this passes on a bare
Python and does deeper verification inside a venv.

Usage:  python3 run_all_tests.py
Exit 0 iff every suite that ran passed.
"""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# (label, path, needs_runtime_deps)
SUITES = [
    ("schema",        "shared/schema/test_snapshot_schema.py",        False),
    ("diagnostics",   "python_backend/modules/test_diagnostics.py",   False),
    ("log_adapters",  "python_backend/connectors/test_log_adapters.py", False),
    ("adapters_batch1", "python_backend/connectors/test_adapters_batch1.py", False),
    ("adapters_batch2", "python_backend/connectors/test_adapters_batch2.py", False),
    ("adapters_batch3", "python_backend/connectors/test_adapters_batch3.py", False),
    ("adapters_batch4", "python_backend/connectors/test_adapters_batch4.py", False),
    ("adapters_batch5", "python_backend/connectors/test_adapters_batch5.py", False),
    ("adapters_batch6", "python_backend/connectors/test_adapters_batch6.py", False),
    ("adapters_batch7", "python_backend/connectors/test_adapters_batch7.py", False),
    ("adapters_batch8", "python_backend/connectors/test_adapters_batch8.py", False),
    ("adapters_batch9", "python_backend/connectors/test_adapters_batch9.py", False),
    ("content_severity", "python_backend/connectors/test_content_severity.py", False),
    # Log lines as RECORDS: field extraction + the role index (never=target).
    ("log_fields",     "python_backend/connectors/test_log_fields.py",     False),
    # Where the program ACTUALLY writes (OS fds vs configured sources).
    ("write_targets",  "python_backend/utils/test_write_targets.py",       False),
    ("realworld_collisions",
     "python_backend/connectors/test_realworld_collisions.py", False),
    ("user_adapter_names",
     "python_backend/connectors/test_user_adapter_names.py", False),
    ("log_sources",   "python_backend/connectors/test_log_sources.py", False),
    ("preflight",     "python_backend/connectors/test_preflight.py", False),
    ("source_readers", "python_backend/connectors/test_source_readers.py", False),
    ("langdetect",    "python_backend/connectors/test_langdetect.py", False),
    ("win_input_sim", "python_backend/daemon/test_win_input_sim.py",  False),
    ("linux_platform", "python_backend/utils/test_linux_platform.py", False),
    ("bridge_routes", "python_backend/api/test_bridge_routes.py",     True),
    ("mcp_tools_a",   "python_backend/api/test_mcp_tools_a.py",        True),
    ("mcp_tools_b",   "python_backend/api/test_mcp_tools_b.py",        True),
    # Token-economics engine (v5.1): perceptual hashing / change detection and
    # the JSON-first routes built on it.
    ("visual_engine", "python_backend/utils/test_visual_engine.py",    True),
    ("visual_routes", "python_backend/api/test_visual_routes.py",      True),
    ("flight_recorder", "python_backend/api/test_flight_recorder.py",  True),
    ("ui_tree",       "python_backend/utils/test_ui_tree.py",          True),
    ("ocr_backends",  "python_backend/utils/test_ocr_backends.py",     False),
    # Examine-before-delete retention: the byte budget + the promise that a
    # frame flagged for the agent is not deleted before the agent sees it.
    ("retention",     "python_backend/utils/test_retention.py",         False),
    # "Nothing is permanently lost — every clear creates a numbered checkpoint"
    # is a promise the GUI makes to the user. This suite is what makes it true:
    # nothing is deleted until the member proving it is IN the tarball is found.
    ("checkpoint",    "python_backend/utils/test_checkpoint_manager.py", False),
    # profiles.json is hand-authored and nothing can re-derive it: a failed READ
    # must never authorise a WRITE, and the write must be atomic.
    ("profile_store", "python_backend/connectors/test_profile_store.py",  False),
    # frame.error must see STRUCTURED records, not just a text grep of one log.
    ("structured_errors", "python_backend/test_structured_errors.py",     False),
    ("retention_routes", "python_backend/api/test_retention_routes.py",  True),
    # A frame must contain ONLY the bridged program — never the desktop.
    ("only_the_program", "python_backend/api/test_only_the_program.py",   True),
    # The first-connection contract: the AGENT decides what gets built, from
    # code evidence, and lazy/blanket plans are refused.
    ("bridge_gate",    "python_backend/api/test_bridge_gate.py",           True),
    # Push Mode (v5.1): the ambient engine + the hook installer. Both are
    # stdlib-only, so they run even without flask/pillow.
    ("ambient",       "python_backend/api/test_ambient.py",            False),
    ("push_hooks",    "python_backend/test_push_hooks.py",             False),
    ("push_routes",   "python_backend/api/test_push_routes.py",        True),
    # Read honesty: the tool may not assert what it did not check — per-source
    # freshness/alignability, recovery, post-mortem grading, cross-source
    # disjointness, and a uniform hypothesis shape with an explicit confidence.
    ("read_honesty",  "python_backend/api/test_read_honesty.py",       True),
    # AgentVision may not write into the log it is reading. 95% of one real
    # project's actions.jsonl was AgentVision's own watchdog output.
    ("observer_isolation",
     "python_backend/api/test_observer_isolation.py",                  True),
    # Class-A entries from docs/MCP_TOOL_AUDIT.md — "the tool misleads its
    # caller". One check per closed queue item.
    ("tool_contracts", "python_backend/api/test_tool_contracts.py",    True),
    # "Is the program running?" must not be answered by a bystander process
    # that merely mentions the project path.
    ("process_identity",
     "python_backend/connectors/test_process_identity.py",             True),
    # A frame seq is unique only WITHIN a profile: two programs' frame 1 must
    # not compete for one key in the index.
    ("frame_namespace", "python_backend/api/test_frame_namespace.py",  True),
    # These three existed but were never registered here, so "the suite passes"
    # did not include them. All pass; being unlisted was the only defect. Every
    # test_*.py in the tree is now in this list — checked, not assumed.
    ("onboarding",    "python_backend/api/test_onboarding.py",         True),
    ("tool_meta",     "python_backend/api/test_tool_meta.py",          True),
    ("runtime_hooks", "agent_bootstrap/test_av_runtime_hooks.py",      True),
    ("emitter_detail", "python_backend/api/test_emitter_detail.py",    True),
    ("ocr_honesty",   "python_backend/api/test_ocr_honesty.py",       True),
    ("corrections",   "python_backend/api/test_corrections.py",       False),
    # Asking the user is client-dependent, so "could not ask" must never look
    # like "asked and got this answer".
    ("elicitation",   "python_backend/api/test_elicitation.py",       False),
    # Resources address the same artifacts as the tools; the catalog one has to
    # produce a token av_bridge_commit will actually accept.
    ("resources",     "python_backend/api/test_resources.py",         False),
    # The one thing here that runs without an agent asking it to. Its failure
    # modes are chatter, replay, and double-reporting — all of them invisible.
    ("push_channel",  "python_backend/api/test_push_channel.py",      False),
    # Counts written in prose rot. Thirteen false statements were found in the
    # published tree by grepping after the fact; this makes the suite find them.
    ("doc_counts",    "python_backend/api/test_doc_counts.py",        False),
    # THE WORST BUG FOUND SO FAR: av_bridge_commit wires the emitter sinks as
    # log_sources, and the whole structured-analysis half read action_log_file,
    # which nothing sets. Eleven MCP tools answered emptily about a log full of
    # the evidence they exist to surface.
    ("action_log_wiring",
     "python_backend/api/test_action_log_wiring.py",                    True),
    # `agentvision run -- <cmd>` is the documented front door; it once
    # instrumented the wrong project silently.
    ("run_front_door", "python_backend/test_run_front_door.py",       False),
    ("browser_adapters",
     "python_backend/connectors/test_browser_adapters.py",             False),
]


def _have_deps() -> bool:
    for mod in ("flask", "psutil", "PIL"):
        try:
            __import__(mod)
        except Exception:
            return False
    return True


def _have_mcp() -> bool:
    """Is the `mcp` package importable?

    Three suites import api/claude_mcp.py, which sys.exit(1)s without it. They
    were reporting FAIL on a clean clone — the aggregator breaking its own rule
    that a missing optional dependency is a SKIP, not a failure. A red suite that
    means "you did not pip install something" trains people to ignore red.
    """
    try:
        __import__("mcp")
        return True
    except Exception:
        return False


#: Suites that cannot even import without `mcp`.
MCP_SUITES = {"tool_meta", "tool_contracts", "onboarding"}


def _bridge_up() -> bool:
    """Is a bridge listening where the gate suite expects one?"""
    import urllib.request
    url = os.environ.get("AGENTVISION_BRIDGE_URL", "http://127.0.0.1:7771")
    try:
        urllib.request.urlopen(url + "/bridge/status", timeout=5).read()
        return True
    except Exception:
        return False


#: Suites needing a LIVE bridge, not just importable packages.
#:
#: Reported here rather than left to the suite itself, because a suite that
#: self-skips by exiting 0 is indistinguishable from one that passed — this
#: aggregator grades purely on returncode. bridge_gate did exactly that for one
#: run of this file and printed `bridge_gate PASS` having verified nothing, which
#: is the same "success it did not establish" that most of these suites exist to
#: catch. So the precondition is checked HERE and printed as a SKIP with its
#: reason, exactly like _have_deps and _have_mcp.
BRIDGE_SUITES = {"bridge_gate"}


def main() -> int:
    have = _have_deps()
    have_mcp = _have_mcp()
    bridge = _bridge_up()
    results = []
    for label, rel, needs in SUITES:
        path = ROOT / rel
        if not path.exists():
            results.append((label, "MISSING"))
            continue
        if needs and not have:
            results.append((label, "SKIP (no flask/psutil/pillow — run in a venv)"))
            continue
        if label in MCP_SUITES and not have_mcp:
            results.append((label, "SKIP (needs the `mcp` package: pip install mcp)"))
            continue
        if label in BRIDGE_SUITES and not bridge:
            results.append((label, "SKIP (needs a LIVE bridge: run "
                                   "`python3 python_backend/api/bridge_server.py "
                                   "--no-autocapture` first)"))
            continue
        print(f"\n{'#' * 70}\n# {label}: {rel}\n{'#' * 70}")
        rc = subprocess.call([sys.executable, str(path)], cwd=str(ROOT))
        results.append((label, "PASS" if rc == 0 else "FAIL"))

    print(f"\n{'=' * 70}\nSUMMARY\n{'=' * 70}")
    failed = 0
    for label, status in results:
        print(f"  {label:16} {status}")
        if status == "FAIL" or status == "MISSING":
            failed += 1
    print("=" * 70)
    if failed:
        print(f"OVERALL: {failed} suite(s) failed")
        return 1
    print("OVERALL: all suites passed (skipped suites not counted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
