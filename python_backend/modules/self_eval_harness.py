"""
Module 11: Self-Evaluation Harness
After each capture cycle, grades the current coding session state
and produces a structured AgentSelfEval.
"""
from shared.schema.snapshot_schema import (
    AgentEval as AgentSelfEval, TestInfo, PerfInfo, GitInfo, AnomalyInfo
)


def evaluate(
    tests: TestInfo,
    perf: PerfInfo,
    git: GitInfo,
    anomaly: AnomalyInfo,
    prev_tests: TestInfo | None = None,
) -> AgentSelfEval:

    eval_ = AgentSelfEval()

    # ── Confidence scoring ──────────────────────────────────────────
    score = 1.0

    # Test health (biggest weight)
    total = tests.pass_count + tests.fail_count
    if total > 0:
        pass_rate = tests.pass_count / total
        score    *= pass_rate
    else:
        score *= 0.5  # no tests = unknown

    # CPU penalty
    if perf.cpu_percent > 85:
        score *= 0.85
    elif perf.cpu_percent > 70:
        score *= 0.93

    # RAM penalty
    if perf.ram_gb > 14:
        score *= 0.88

    # Anomaly penalty
    if anomaly.detected:
        if anomaly.severity == "critical":
            score *= 0.4
        elif anomaly.severity == "high":
            score *= 0.6
        elif anomaly.severity == "medium":
            score *= 0.8

    # Uncommitted changes penalty (large diffs increase risk)
    if git.unstaged_count > 20:
        score *= 0.85
    elif git.unstaged_count > 10:
        score *= 0.92

    eval_.confidence = round(max(0.0, min(1.0, score)), 2)

    # ── Regression detection ────────────────────────────────────────
    if prev_tests:
        newly_failing = tests.fail_count - prev_tests.fail_count
        if newly_failing > 0:
            eval_.regression_detected = True
            eval_.what_went_wrong = (
                f"{newly_failing} new test failure(s) since last frame. "
                f"Failing: {', '.join(tests.failed_tests[:3])}")

    # ── Rollback suggestion ─────────────────────────────────────────
    if eval_.regression_detected and eval_.confidence < 0.4:
        eval_.needs_rollback = True

    # ── Next action recommendation ──────────────────────────────────
    if anomaly.detected and anomaly.severity in ("critical", "high"):
        eval_.next_action = f"Investigate {anomaly.type}: {anomaly.description}"
    elif eval_.regression_detected:
        eval_.next_action = f"Fix failing tests: {', '.join(tests.failed_tests[:2])}"
    elif tests.fail_count > 0:
        eval_.next_action = f"Resolve {tests.fail_count} failing test(s)"
    elif git.unstaged_count > 0:
        eval_.next_action = f"Review and commit {git.unstaged_count} changed file(s)"
    elif eval_.confidence >= 0.9:
        eval_.next_action = "Session looks healthy — continue development"
    else:
        eval_.next_action = "Monitor for further issues"

    return eval_


def eval_to_overlay(ev: AgentSelfEval) -> list[str]:
    bar_filled = int(ev.confidence * 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    confidence_str = f"Confidence [{bar}] {int(ev.confidence*100)}%"
    lines = [confidence_str]
    if ev.regression_detected:
        lines.append("⚠ REGRESSION DETECTED")
    if ev.needs_rollback:
        lines.append("⚑ ROLLBACK SUGGESTED")
    lines.append(f"→ {ev.next_action[:60]}")
    return lines
