"""
CI/CD, build-tool and debugger log adapters (BATCH 2)
================================================================================
Build pipelines, test-report artifacts, and debugger output. (GitHub Actions
`::error::` commands, gcc/clang/rustc/MSVC diagnostics, and pytest/go/cargo test
results are already covered by the core `ci`, `build`, and `test` adapters.)

Formats: jenkins, junit_xml, gdb_backtrace, ansible, azure_devops.
"""
from __future__ import annotations

import re
from typing import Optional

from ._common import (LogAdapter, register_adapter, parse_timestamp, ratio_detect,
                      split_any, block_ratio)


# ── Jenkins Pipeline console log ─────────────────────────────────────────────
#   [Pipeline] stage
#   Running on agent-1 in /home/jenkins/workspace/my-job
#   Finished: SUCCESS
class JenkinsAdapter(LogAdapter):
    name = "jenkins"
    language = "any"
    _PIPELINE = re.compile(r"^\[Pipeline\]\s*(?P<step>.*)$")
    _RUNNING = re.compile(r"^Running on (?P<agent>\S+) in (?P<ws>\S+)$")
    _FINISHED = re.compile(r"^Finished:\s+(?P<result>SUCCESS|FAILURE|UNSTABLE|ABORTED|NOT_BUILT)$")

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda ln: bool(self._PIPELINE.match(ln.strip())
                            or self._RUNNING.match(ln.strip())
                            or self._FINISHED.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n").strip()
        m = self._FINISHED.match(s)
        if m:
            res = m.group("result")
            failed = res in ("FAILURE", "ABORTED")
            level = "error" if failed else "warn" if res == "UNSTABLE" else "info"
            # a failed build → category "error" so the bridge bookmarks it; a
            # green build stays in the neutral "test" bucket.
            return self._event(level=level, message=f"Finished: {res}", source="jenkins",
                               category="error" if failed else "test",
                               fields={"result": res, "passed": res == "SUCCESS"}, raw=line)
        m = self._PIPELINE.match(s)
        if m:
            return self._event(level="", message=f'[Pipeline] {m.group("step")}',
                               source="jenkins.pipeline",
                               fields={"step": m.group("step")}, raw=line)
        m = self._RUNNING.match(s)
        if m:
            return self._event(level="", message=s, source="jenkins",
                               fields={"agent": m.group("agent"), "workspace": m.group("ws")},
                               raw=line)
        return None


# ── JUnit / xUnit XML test report (single-line elements) ─────────────────────
#   <testcase name="test_add" classname="tests.MathTest" time="0.001"/>
#   <testcase name="test_div" classname="tests.MathTest" time="0.02"><failure ...>
class JUnitXmlAdapter(LogAdapter):
    name = "junit_xml"
    language = "any"
    _CASE = re.compile(r"<testcase\b(?P<attrs>[^>]*)>?")
    _SUITE = re.compile(r"<testsuite\b(?P<attrs>[^>]*)>")
    _ATTR = re.compile(r'(\w+)="([^"]*)"')
    _FAIL = re.compile(r"<(failure|error)\b")

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda ln: ("<testcase" in ln or "<testsuite" in ln) and "name=" in ln)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        cm = self._CASE.search(s)
        if cm:
            attrs = dict(self._ATTR.findall(cm.group("attrs")))
            failed = bool(self._FAIL.search(s)) or "</failure>" in s or "</error>" in s
            skipped = "<skipped" in s
            name = attrs.get("name", "?")
            cls = attrs.get("classname", "")
            level = "error" if failed else "info"
            try:
                dur = float(attrs.get("time", "0")) * 1000.0
            except ValueError:
                dur = None
            return self._event(level=level, message=f'{cls}::{name}'.strip(":"),
                               source=cls or "junit",
                               category="error" if failed else "test",
                               fields={"test": name, "classname": cls,
                                       "passed": not failed, "skipped": skipped,
                                       "duration_ms": dur}, raw=line)
        sm = self._SUITE.search(s)
        if sm:
            attrs = dict(self._ATTR.findall(sm.group("attrs")))
            failures = int(attrs.get("failures", "0") or 0)
            errors = int(attrs.get("errors", "0") or 0)
            bad = bool(failures or errors)
            level = "error" if bad else "info"
            return self._event(level=level, message=f'suite {attrs.get("name","?")}',
                               source="junit", category="error" if bad else "test",
                               fields={"tests": attrs.get("tests"), "failures": failures,
                                       "errors": errors}, raw=line)
        return None


# ── GDB / LLDB backtrace frames ──────────────────────────────────────────────
#   #0  0x00007ffff7a3d in raise () at signals.c:42
#   frame #1: 0x000000010 myapp`main + 24 at main.c:10        (lldb)
class GdbBacktraceAdapter(LogAdapter):
    name = "gdb_backtrace"
    language = "cpp"
    _GDB = re.compile(
        r"^#(?P<num>\d+)\s+(?:(?P<addr>0x[0-9a-fA-F]+)\s+in\s+)?(?P<func>[\w:~<>@.\-]+)\s*"
        r"\((?P<args>[^)]*)\)(?:\s+at\s+(?P<loc>\S+:\d+))?")
    _LLDB = re.compile(
        r"^\s*(?:\*\s+)?frame #(?P<num>\d+):\s+(?P<addr>0x[0-9a-fA-F]+)\s+"
        r"(?P<mod>\S+)`(?P<func>[^+\n]+?)(?:\s*\+\s*(?P<off>\d+))?(?:\s+at\s+(?P<loc>\S+:\d+))?$")

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda ln: bool(self._GDB.match(ln.rstrip()) or self._LLDB.match(ln.rstrip())))

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        m = self._GDB.match(s)
        if m:
            g = m.groupdict()
            return self._event(level="error", message=s.strip(),
                               source=g["func"], category="error",
                               fields={"frame": int(g["num"]), "func": g["func"],
                                       "addr": g.get("addr"), "location": g.get("loc"),
                                       "debugger": "gdb"}, raw=line)
        m = self._LLDB.match(s)
        if m:
            g = m.groupdict()
            return self._event(level="error", message=s.strip(),
                               source=(g["func"] or "").strip(), category="error",
                               fields={"frame": int(g["num"]), "func": (g["func"] or "").strip(),
                                       "module": g.get("mod"), "location": g.get("loc"),
                                       "debugger": "lldb"}, raw=line)
        return None


# ── Ansible playbook stdout (default callback) ───────────────────────────────
#   TASK [Gathering Facts] *******************************************************
#   ok: [web1]      changed: [web1]      fatal: [web1]: FAILED! => {...}
class AnsibleAdapter(LogAdapter):
    name = "ansible"
    language = "any"
    _BANNER = re.compile(r"^(?P<kind>PLAY RECAP|PLAY|TASK|HANDLER|RUNNING HANDLER)\s+"
                         r"(?:\[(?P<name>.*?)\]\s*)?\*{5,}\s*$")
    _RESULT = re.compile(r"^(?P<res>ok|changed|skipping|failed|fatal|unreachable|ignoring)"
                         r":\s+\[(?P<host>[^\]]+)\](?P<rest>.*)$")
    # log_path file form: "2023-09-11 07:58:11,765 p=699 u=maercu n=ansible | PLAY [x] ***"
    _LOGFILE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+"
        r"p=(?P<pid>\d+)\s+u=(?P<user>\S+)\s+n=(?P<prog>\S+)(?:\s+\S+)*?\s+\|\s+(?P<body>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(
            sample_lines,
            lambda ln: bool(self._BANNER.match(ln.rstrip())
                            or self._RESULT.match(ln.strip())
                            or self._LOGFILE.match(ln.strip())))

    _LVL = {"ok": "info", "changed": "info", "skipping": "debug", "ignoring": "warn",
            "failed": "error", "fatal": "error", "unreachable": "error"}

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        lf = self._LOGFILE.match(s.strip())
        if lf:
            g = lf.groupdict()
            body = g["body"]
            # the body after "| " is the same playbook grammar → reuse it
            bm = self._BANNER.match(body.rstrip())
            rm = self._RESULT.match(body.strip()) if not bm else None
            level = self._LVL.get(rm.group("res"), "info") if rm else ""
            return self._event(level=level, message=body.strip(),
                               source=f'ansible.{g["user"]}',
                               ts_ms=parse_timestamp(g["ts"]),
                               fields={"pid": int(g["pid"]), "user": g["user"],
                                       "phase": bm.group("kind") if bm else None,
                                       "result": rm.group("res") if rm else None},
                               raw=line)
        m = self._BANNER.match(s.rstrip())
        if m:
            return self._event(level="", message=f'{m.group("kind")} [{m.group("name") or ""}]',
                               source="ansible", fields={"phase": m.group("kind"),
                               "name": m.group("name")}, raw=line)
        m = self._RESULT.match(s.strip())
        if m:
            g = m.groupdict()
            return self._event(level=self._LVL.get(g["res"], "info"),
                               message=f'{g["res"]}: [{g["host"]}]{g["rest"]}'.strip(),
                               source=f'ansible.{g["host"]}',
                               fields={"result": g["res"], "host": g["host"]}, raw=line)
        return None


# ── Azure DevOps / Azure Pipelines task log (##[...] and ##vso[...]) ──────────
#   2024-02-28T17:41:15.1315148Z ##[section]Starting: Build solution
#   2024-02-28T17:41:16.0Z ##[error]MSB4126: The specified solution ...
class AzureDevopsAdapter(LogAdapter):
    name = "azure_devops"
    language = "any"
    _RE = re.compile(
        r"^(?:(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s+)?"
        r"##(?:\[(?P<cmd>section|group|endgroup|warning|error|debug|command)\]"
        r"|vso\[(?P<vso>[^\]]*)\])(?P<msg>.*)$")
    _LVL = {"error": "error", "warning": "warn", "debug": "debug"}

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        cmd = g.get("cmd") or ""
        fields = {}
        if g.get("vso"):
            fields["vso_command"] = g["vso"]
        elif cmd:
            fields["logging_command"] = cmd
        return self._event(level=self._LVL.get(cmd, ""), message=g["msg"].strip() or cmd,
                           source="azure-pipelines",
                           ts_ms=parse_timestamp(g["ts"]) if g.get("ts") else None,
                           fields=fields or None, raw=line)


# ═══════════════════════════ BATCH 5 ═════════════════════════════════════════

# ── Chef Infra Client run log ──────────────────────────────────────────────────
#   [2017-05-04T18:56:33+00:00] INFO: *** Chef 12.19.36 ***
class ChefClientAdapter(LogAdapter):
    name = "chef_client"
    language = "ruby"
    _RE = re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2})\]\s+"
        r"(?P<level>DEBUG|INFO|WARN|ERROR|FATAL):\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        return self._event(level=g["level"], message=g["msg"], source="chef",
                           ts_ms=parse_timestamp(g["ts"]), raw=line)


# ── Puppet agent/server log (logdest file / console form) ─────────────────────
#   2022-02-10 01:44:21 -0800 Puppet (info): Computing checksum on file …
class PuppetAgentAdapter(LogAdapter):
    name = "puppet_agent"
    language = "ruby"
    _RE = re.compile(
        r"^(?:(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(?P<off>[+-]\d{4})\s+)?"
        r"Puppet \((?P<level>debug|info|notice|warning|err|error|alert|emerg|crit)\):\s+"
        r"(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        ts_ms = None
        if g["ts"]:
            ts_ms = parse_timestamp(f'{g["ts"].replace(" ", "T", 1)}'
                                    f'{g["off"][:3]}:{g["off"][3:]}' if g["off"]
                                    else g["ts"])
        return self._event(level=g["level"], message=g["msg"], source="puppet",
                           ts_ms=ts_ms, raw=line)


# ── Puppet apply resource-event lines ──────────────────────────────────────────
#   Notice: /Stage[main]/Main/File[/tmp/test]/ensure: defined content as '…'
class PuppetApplyAdapter(LogAdapter):
    name = "puppet_apply"
    language = "ruby"
    _RE = re.compile(
        r"^(?P<level>Notice|Info|Warning|Error|Debug):\s+"
        r"(?P<res>/Stage\[[^\]]+\]\S*?):\s+(?P<msg>.*)$")

    def detect(self, sample_lines):
        return ratio_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        level = {"Notice": "info"}.get(g["level"], g["level"])
        return self._event(level=level, message=g["msg"], source=g["res"],
                           fields={"resource": g["res"]}, raw=line)


# ── Terraform plan/apply human-readable output ─────────────────────────────────
#   Plan: 1 to add, 0 to change, 0 to destroy.
#   Apply complete! Resources: 1 added, 0 changed, 0 destroyed.
#     # aws_instance.web will be created
class TerraformPlanAdapter(LogAdapter):
    name = "terraform_plan"
    language = "any"
    _PLAN = re.compile(r"^Plan:\s+\d+ to add,\s+\d+ to change,\s+\d+ to destroy\.$")
    _DONE = re.compile(r"^(Apply|Destroy) complete! Resources:\s+\d+")
    _RES = re.compile(r"^\s*# (?P<addr>\S+) (?:will be|must be|has been) "
                      r"(?P<verb>created|destroyed|updated in-place|replaced|read)")
    _HEAD = re.compile(r"^(Terraform will perform the following actions|"
                       r"Terraform used the selected providers|"
                       r"No changes\. |Refreshing state\.\.\.|"
                       r"Terraform planned the following actions)")
    _DIFF = re.compile(r'^\s*[-+~](?:/[-+~])?\s+(?:resource "|\w+\s*[={]|")')

    def _anchor(self, s: str) -> bool:
        st = s.rstrip()
        return bool(self._PLAN.match(st.strip()) or self._DONE.match(st.strip())
                    or self._RES.match(st) or self._HEAD.match(st.strip()))

    def detect(self, sample_lines):
        # an element is claimed when it carries a terraform anchor line and is
        # dominated by anchor/diff-body lines (a full plan block qualifies; one
        # stray "Plan:" line inside a foreign log does not dominate its block).
        def ok(el):
            subs = split_any(el)
            return (any(self._anchor(x) for x in subs)
                    and block_ratio(el, lambda x: self._anchor(x)
                                    or bool(self._DIFF.match(x)), threshold=0.4))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        st = s.strip()
        m = self._PLAN.match(st)
        if m:
            nums = re.findall(r"\d+", st)
            add, change, destroy = (int(x) for x in nums[:3])
            level = "warn" if destroy else "info"
            return self._event(level=level, message=st, source="terraform",
                               fields={"add": add, "change": change,
                                       "destroy": destroy}, raw=line)
        if self._DONE.match(st):
            return self._event(level="info", message=st, source="terraform",
                               raw=line)
        m = self._RES.match(s)
        if m:
            verb = m.group("verb")
            level = "warn" if verb in ("destroyed", "replaced") else "info"
            return self._event(level=level, message=st, source="terraform",
                               fields={"resource": m.group("addr"),
                                       "action": verb}, raw=line)
        if self._HEAD.match(st):
            return self._event(level="info", message=st, source="terraform",
                               raw=line)
        if self._DIFF.match(s):
            return self._event(level="", message=st, source="terraform",
                               category="debug", fields={"diff": True}, raw=line)
        return None


# ── Registration ─────────────────────────────────────────────────────────────
# junit_xml's <testcase name=".." time=".."/> attributes read as logfmt k="v"
# pairs (time= is a logfmt trigger key) → register it BEFORE logfmt so the
# specific XML grammar wins the confidence tie.
register_adapter(JUnitXmlAdapter(), before="logfmt")
for _a in (JenkinsAdapter(), GdbBacktraceAdapter(),
           AnsibleAdapter(), AzureDevopsAdapter(),
           # batch 5
           ChefClientAdapter(), PuppetAgentAdapter(), PuppetApplyAdapter(),
           TerraformPlanAdapter()):
    register_adapter(_a)


# ═════════════════════════════════════════════════════════════════════════════
# BATCH 6
# ═════════════════════════════════════════════════════════════════════════════
from ._common import multiline_ratio_detect as _ml_detect  # noqa: E402


# ── SaltStack daemon log (master/minion) ──────────────────────────────────────
#   2024-01-15 10:23:45,123 [salt.minion      ][INFO    ][12345] Minion is ready…
class SaltDaemonAdapter(LogAdapter):
    name = "salt_daemon"
    language = "python"
    _RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+"
        r"\[(?P<logger>[\w.]+)\s*\]\[(?P<level>TRACE|DEBUG|INFO|WARNING|ERROR|CRITICAL)\s*\]"
        r"(?:\[(?P<pid>\d+)\])?\s?(?P<msg>.*)$")

    def detect(self, sample_lines):
        return _ml_detect(sample_lines, lambda ln: bool(self._RE.match(ln.strip())))

    def parse_line(self, line: str) -> Optional[dict]:
        m = self._RE.match(line.rstrip("\r\n").strip())
        if not m:
            return None
        g = m.groupdict()
        fields = {}
        if g["pid"]:
            fields["pid"] = int(g["pid"])
        return self._event(level=g["level"], message=g["msg"], source=g["logger"],
                           ts_ms=parse_timestamp(g["ts"]), fields=fields or None,
                           raw=line)


register_adapter(SaltDaemonAdapter())


# ═════════════════════════════════════════════════════════════════════════════
#  BATCH 7 — IaC / config-management console outputs
# ═════════════════════════════════════════════════════════════════════════════
from ._common import split_any as _split_any  # noqa: E402


# ── Packer build console ──────────────────────────────────────────────────────
#   ==> amazon-ebs: Waiting for instance to become ready...
#       amazon-ebs: Instance ID: i-0123456789abcdef0
#   Build 'amazon-ebs' finished after 5 minutes 33 seconds.
class PackerUiAdapter(LogAdapter):
    name = "packer_ui"
    language = "any"
    _STEP = re.compile(r"^==> (?P<builder>[\w.\-]+): (?P<msg>.*)$")
    _DETAIL = re.compile(r"^\s{4}(?P<builder>[\w.\-]+): (?P<msg>.*)$")
    _BUILD = re.compile(r"^(?:==> )?Builds? '(?P<name>[^']+)' (?P<what>finished|errored)\b(?P<rest>.*)$")

    def detect(self, sample_lines):
        def ok(el):
            subs = _split_any(el)
            hits = sum(1 for x in subs
                       if self._STEP.match(x) or self._BUILD.match(x.strip()))
            return bool(subs) and hits >= 1 and hits / len(subs) >= 0.5
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\r\n")
        for x in _split_any(s) or [s]:
            m = self._STEP.match(x) or self._DETAIL.match(x)
            if m:
                g = m.groupdict()
                low = g["msg"].lower()
                level = "error" if ("error" in low or "failed" in low) else "info"
                return self._event(level=level, message=g["msg"],
                                   source=f'packer.{g["builder"]}', raw=line)
            m = self._BUILD.match(x.strip())
            if m:
                g = m.groupdict()
                level = "error" if g["what"] == "errored" else "info"
                return self._event(level=level, message=x.strip(),
                                   source=f'packer.{g["name"]}', raw=line)
        return None


# ── Pulumi CLI diff/progress rows ─────────────────────────────────────────────
#       +  aws:s3:Bucket my-bucket creating
class PulumiCliAdapter(LogAdapter):
    name = "pulumi_cli"
    language = "any"
    _ROW = re.compile(
        r"^\s*(?P<op>[+\-~]{1,2})\s{2,}(?P<type>[\w-]+:[\w/-]+:[\w.-]+)\s+"
        r"(?P<name>\S+)\s+(?P<status>creating|created|updating|updated|deleting|"
        r"deleted|replacing|replaced|refreshing|read|failed)(?P<rest>.*)$")

    def detect(self, sample_lines):
        def ok(el):
            return any(self._ROW.match(x) for x in _split_any(el))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        for x in _split_any(line):
            m = self._ROW.match(x)
            if m:
                g = m.groupdict()
                level = "error" if (g["status"] == "failed" or "error" in g["rest"].lower()) else "info"
                return self._event(level=level,
                                   message=f'{g["op"]} {g["type"]} {g["name"]} {g["status"]}',
                                   source="pulumi",
                                   fields={"op": g["op"], "resource_type": g["type"],
                                           "resource": g["name"], "status": g["status"]},
                                   raw=line)
        return None


# ── Chef Infra Client converge output (doc formatter) ─────────────────────────
#     * file[/tmp/name_of_file] action create (up to date)
class ChefDocAdapter(LogAdapter):
    name = "chef_doc"
    language = "any"
    _RES = re.compile(
        r"^\s*\* (?P<rtype>[\w:]+)\[(?P<rname>[^\]]+)\] action (?P<action>\S+)"
        r"\s*(?P<result>\(.*\))?\s*$")
    _FOOT = re.compile(
        r"^Chef (?:Infra )?Client (?P<what>finished|failed)[.,]? (?P<rest>.*)$")

    def detect(self, sample_lines):
        def ok(el):
            return any(self._RES.match(x) or self._FOOT.match(x.strip())
                       for x in _split_any(el))
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        for x in _split_any(line):
            m = self._RES.match(x)
            if m:
                g = m.groupdict()
                result = (g.get("result") or "").strip("() ")
                return self._event(level="info",
                                   message=f'{g["rtype"]}[{g["rname"]}] {g["action"]}'
                                           + (f" ({result})" if result else ""),
                                   source="chef",
                                   fields={"resource": f'{g["rtype"]}[{g["rname"]}]',
                                           "action": g["action"],
                                           "result": result or None}, raw=line)
            m = self._FOOT.match(x.strip())
            if m:
                level = "error" if m.group("what") == "failed" else "info"
                return self._event(level=level, message=x.strip(), source="chef",
                                   raw=line)
        return None


# ── SaltStack state/highstate console output (nested outputter) ───────────────
#             ID: install_nginx
#       Function: pkg.installed
#         Result: True
class SaltStateAdapter(LogAdapter):
    name = "salt_state"
    language = "any"
    _KEY = re.compile(
        r"^\s*(?P<k>ID|Function|Result|Comment|Started|Duration|Changes|Name)"
        r":\s?(?P<v>.*)$")
    _NEEDED = {"ID", "Function", "Result"}

    def detect(self, sample_lines):
        def ok(el):
            keys = {m.group("k") for m in
                    (self._KEY.match(x) for x in _split_any(el)) if m}
            return len(keys & self._NEEDED) >= 2
        return ratio_detect(sample_lines, ok)

    def parse_line(self, line: str) -> Optional[dict]:
        kv = {}
        for x in _split_any(line):
            m = self._KEY.match(x)
            if m and m.group("k") not in kv:
                kv[m.group("k")] = m.group("v").strip()
        if len(set(kv) & self._NEEDED) < 2:
            return None
        result = kv.get("Result", "")
        level = "error" if result == "False" else "info"
        sid = kv.get("ID", "?")
        fn = kv.get("Function", "")
        return self._event(level=level,
                           message=f'{sid} ({fn}) Result: {result or "?"}',
                           source="salt.state",
                           fields={k.lower(): v for k, v in kv.items()}, raw=line)


for _a in (PackerUiAdapter(), PulumiCliAdapter(), ChefDocAdapter(),
           SaltStateAdapter()):
    register_adapter(_a)
