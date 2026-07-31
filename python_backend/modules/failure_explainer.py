"""
Module 5: Test Failure Explainer
Runs pytest (or unittest), captures structured failure output,
and enriches it with a plain-English cause for the AI agent.
"""
import subprocess
import sys
import json
import re
from pathlib import Path
from shared.schema.snapshot_schema import TestInfo, ErrorInfo


def run_tests(project_root: str = ".", timeout: int = 60,
              python_exe: str = "") -> tuple[TestInfo, list[ErrorInfo]]:
    """
    Runs pytest with JSON report plugin if available, falls back to plain output.
    Returns TestInfo + list of ErrorInfo (one per failure).

    HONEST FAILURE. A run that never happened must not look like a clean run:
    no interpreter, pytest not installed, or a timeout all set `ran=False` with
    an `error` instead of returning 0/0/0 (which reads as "all tests passed").
    `python_exe` selects the interpreter — bare "python" is absent on stock
    macOS, so the default is the interpreter running the bridge (sys.executable),
    which is guaranteed to exist and to have AgentVision's own deps.
    """
    test_info = TestInfo()
    errors: list[ErrorInfo] = []

    exe = python_exe or sys.executable or "python3"
    report_path = Path(project_root) / ".agentvision_test_report.json"

    # Try pytest-json-report first
    try:
        result = subprocess.run(
            [exe, "-m", "pytest",
             "--tb=short",
             f"--json-report",
             f"--json-report-file={report_path}",
             "--quiet"],
            capture_output=True, text=True, timeout=timeout,
            cwd=project_root)
    except FileNotFoundError as exc:
        test_info.ran = False
        test_info.error = (f"test runner did not start: interpreter '{exe}' not "
                           f"found ({exc}). Set the profile's python_exe to a "
                           f"real interpreter.")
        return test_info, errors
    except subprocess.TimeoutExpired:
        test_info.ran = False
        test_info.error = (f"pytest exceeded the {timeout}s timeout and was "
                           f"killed before producing a result — no pass/fail "
                           f"counts are available.")
        return test_info, errors

    if report_path.exists():
        errors = _parse_json_report(report_path, test_info)
        try:
            report_path.unlink()
        except Exception:
            pass
    else:
        # No JSON report. Distinguish "pytest ran but the plugin was absent" from
        # "pytest never ran at all" — the second must NOT read as 0 failures.
        combined = (result.stdout or "") + (result.stderr or "")
        # pytest exit 5 == no tests collected (a real, clean outcome).
        if "No module named pytest" in combined or (
                result.returncode == 4 and "pytest" in combined.lower()
                and "usage" in combined.lower()):
            test_info.ran = False
            test_info.error = (
                "pytest is not installed for the selected interpreter "
                f"('{exe}'), so no tests were run. Install it (pip install "
                "pytest pytest-json-report) or point the profile's python_exe at "
                "an interpreter that has it.")
            test_info.failure_detail = combined[-2000:]
            return test_info, errors
        # Fallback: parse plain pytest output
        errors = _parse_plain_output(result.stdout + result.stderr, test_info)

    return test_info, errors


def _parse_json_report(path: Path, info: TestInfo) -> list[ErrorInfo]:
    errors: list[ErrorInfo] = []
    try:
        data = json.loads(path.read_text())
        summary = data.get("summary", {})
        info.pass_count  = summary.get("passed",  0)
        info.fail_count  = summary.get("failed",  0)
        info.skip_count  = summary.get("skipped", 0)
        info.duration_ms = round(data.get("duration", 0) * 1000, 1)

        for test in data.get("tests", []):
            if test.get("outcome") == "failed":
                info.failed_tests.append(test.get("nodeid", ""))
                call = test.get("call", {})
                longrepr = call.get("longrepr", "")
                file_, line_ = _extract_location(longrepr)
                errors.append(ErrorInfo(
                    file=file_,
                    line=line_,
                    message=test.get("nodeid", ""),
                    stack_trace=longrepr,
                    likely_cause=_infer_cause(longrepr),
                ))
    except Exception:
        pass
    return errors


def _parse_plain_output(output: str, info: TestInfo) -> list[ErrorInfo]:
    errors: list[ErrorInfo] = []

    # Summary line: "3 failed, 52 passed, 1 warning in 4.22s"
    m = re.search(r"(\d+) failed", output)
    if m:
        info.fail_count = int(m.group(1))
    m = re.search(r"(\d+) passed", output)
    if m:
        info.pass_count = int(m.group(1))
    m = re.search(r"(\d+) skipped", output)
    if m:
        info.skip_count = int(m.group(1))

    info.failure_detail = output[-2000:] if len(output) > 2000 else output

    # Extract FAILED lines
    for line in output.splitlines():
        if line.startswith("FAILED "):
            info.failed_tests.append(line[7:].split(" - ")[0].strip())

    # Build one ErrorInfo from the whole block
    if info.fail_count > 0:
        file_, line_ = _extract_location(output)
        errors.append(ErrorInfo(
            file=file_,
            line=line_,
            message=f"{info.fail_count} test(s) failed",
            stack_trace=output[-1500:],
            likely_cause=_infer_cause(output),
        ))

    return errors


def _extract_location(text: str) -> tuple[str, int]:
    """Parse 'file.py:42:' style location from traceback."""
    m = re.search(r'([\w/._-]+\.py):(\d+)', text)
    if m:
        return m.group(1), int(m.group(2))
    return "", 0


def _infer_cause(traceback: str) -> str:
    """Rule-based cause inference for common failure patterns."""
    t = traceback.lower()
    if "assertionerror" in t:
        return "Assertion failed — expected value doesn't match actual output."
    if "attributeerror" in t:
        return "AttributeError — object missing expected attribute or method."
    if "typeerror" in t:
        return "TypeError — wrong argument type or number of arguments."
    if "keyerror" in t:
        return "KeyError — dictionary lookup failed; key not present."
    if "indexerror" in t:
        return "IndexError — list/sequence index out of range."
    if "none" in t and "nonetype" in t:
        return "NoneType error — a function returned None unexpectedly."
    if "importerror" in t or "modulenotfounderror" in t:
        return "ImportError — missing module or incorrect import path."
    if "timeout" in t:
        return "Timeout — operation exceeded time limit."
    if "connectionrefused" in t or "connectionerror" in t:
        return "Connection refused — service may be down or wrong port."
    return "Unknown cause — review stack trace for details."
