#!/usr/bin/env python3
"""Asking the user must never be indistinguishable from guessing.

THE FAILURE THIS EXISTS FOR. Six places in this codebase told the agent to
"ASK THE USER, do not assume" while giving it no way to ask. The fix — MCP
elicitation — has a failure mode of exactly the same shape as the bug it
replaces: elicitation is client-dependent, so on Claude Desktop, or over plain
HTTP, or in a test, the ask cannot happen. If the value that comes back then
looks like the value a user chose, AgentVision has gone from "no mechanism" to
"a mechanism that lies", which is strictly worse.

So every check here is about the SEAM, not the happy path:
  * a value nobody chose is labelled as one nobody chose;
  * "the client can't ask" and "the ask blew up" stay distinguishable, and the
    error text survives;
  * "unknown capability" is never rounded down to "unsupported";
  * an explicit human "no" to system-wide input recording is OBEYED, and a
    non-answer never counts as consent.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from api import elicit as E                                     # noqa: E402

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


# ── Fakes that model a real client, including the ways clients misbehave ──────

class _Caps:
    def __init__(self, elicitation):
        self.elicitation = elicitation


class _Data:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Result:
    def __init__(self, action, data=None):
        self.action = action
        if data is not None:
            self.data = data


class _Ctx:
    """A stand-in MCP Context. `caps=None` models a client that declared none."""

    def __init__(self, caps=..., result=None, boom=None, session=object()):
        self._caps = _Caps(object()) if caps is ... else caps
        self._result = result
        self._boom = boom
        self.session = session
        self.asked: list = []

    @property
    def client_capabilities(self):
        return self._caps

    async def elicit(self, message, schema):
        self.asked.append((message, schema))
        if self._boom:
            raise self._boom
        return self._result


def run(coro):
    return asyncio.run(coro)


DISCLAIMERS = ("NOT your choice", "NOBODY CONSENTED", "not set")


def _disclaims(note: str) -> bool:
    """Does this note admit that AgentVision, not the user, decided?"""
    return any(d.lower() in note.lower() for d in DISCLAIMERS)


def main() -> int:
    print("elicitation — asking, and admitting when we could not")

    # ── The six answers are the whole contract ────────────────────────────────
    check("HOW_VALUES covers every branch ask() can return",
          set(E.HOW_VALUES) == {"asked", "declined", "cancelled",
                                "unsupported", "no_context", "failed"},
          str(E.HOW_VALUES))
    check("only 'asked' counts as the user having chosen",
          E.Answer(1, E.HOW_ASKED, "").chosen_by_user
          and not any(E.Answer(1, h, "").chosen_by_user
                      for h in E.HOW_VALUES if h != E.HOW_ASKED))

    # ── No session at all: the CLI, an HTTP caller, this test ────────────────
    a = run(E.ask_capture_rate(None, fallback_interval=1.0))
    check("no ctx -> no_context, fallback returned",
          a.how == E.HOW_NO_CONTEXT and a.value == 1.0, f"{a.how} {a.value}")
    check("...and the note says the value is not the user's choice",
          _disclaims(a.note), a.note)

    # ── Declared capabilities, elicitation absent: Claude Desktop today ──────
    ctx = _Ctx(caps=_Caps(None))
    a = run(E.ask_capture_rate(ctx, fallback_interval=2.0))
    check("client without elicitation -> unsupported",
          a.how == E.HOW_UNSUPPORTED and a.value == 2.0, f"{a.how} {a.value}")
    check("...and it does NOT waste a round trip asking anyway",
          ctx.asked == [], str(ctx.asked))
    check("...and the note still disclaims the choice", _disclaims(a.note), a.note)

    # ── THE TRAP: unknown capabilities must not be reported as unsupported ───
    # A stateless client declares nothing. Calling that "this client cannot ask"
    # asserts something never established — the exact move this project bans.
    check("capabilities=None reads as UNKNOWN, not False",
          E.supports_elicitation(_Ctx(caps=None)) is None,
          str(E.supports_elicitation(_Ctx(caps=None))))
    ctx = _Ctx(caps=None, result=_Result("accept", _Data(shots_per_second=5.0)))
    a = run(E.ask_capture_rate(ctx, fallback_interval=1.0))
    check("...so the ask IS attempted, and succeeds",
          a.how == E.HOW_ASKED and len(ctx.asked) == 1, f"{a.how}")

    # ── The happy path, and the arithmetic that is easy to invert ────────────
    ctx = _Ctx(result=_Result("accept", _Data(shots_per_second=4.0)))
    a = run(E.ask_capture_rate(ctx, fallback_interval=1.0, current_interval=1.0))
    check("4 shots/sec becomes interval 0.25s, not 4",
          a.how == E.HOW_ASKED and abs(a.value - 0.25) < 1e-9, str(a.value))
    check("the note states BOTH units so 1.0 cannot be misread",
          "shot(s)/sec" in a.note and "between shots" in a.note, a.note)
    check("the question tells the user the range and that it is free",
          "0.1" in ctx.asked[0][0] and "10" in ctx.asked[0][0]
          and "free" in ctx.asked[0][0].lower(), ctx.asked[0][0][:120])
    for fps, want in ((10.0, 0.1), (0.1, 10.0)):
        ctx = _Ctx(result=_Result("accept", _Data(shots_per_second=fps)))
        got = run(E.ask_capture_rate(ctx, fallback_interval=1.0)).value
        check(f"{fps} shots/sec clamps inside the supported range ({want}s)",
              abs(got - want) < 1e-9, str(got))

    # ── Decline and cancel are different facts and stay different ────────────
    for action, how in (("decline", E.HOW_DECLINED), ("cancel", E.HOW_CANCELLED)):
        a = run(E.ask_capture_rate(_Ctx(result=_Result(action)),
                                   fallback_interval=1.5))
        check(f"'{action}' -> {how}, fallback kept",
              a.how == how and a.value == 1.5, f"{a.how} {a.value}")
        check(f"...and the '{action}' note disclaims the choice",
              _disclaims(a.note), a.note)
        # THE ASK HAPPENED. Saying "AgentVision could not ask you" after the
        # user was asked and refused is itself a false statement, and this
        # module cannot be the one place that gets that wrong.
        check(f"...and does NOT claim it could not ask ('{action}')",
              "could not ask" not in a.note, a.note)
        for fn, kw in ((E.ask_input_recording_consent, {}),
                       (E.ask_project_root, {})):
            b = run(fn(_Ctx(result=_Result(action)), **kw))
            check(f"...same for {fn.__name__} on '{action}'",
                  "could not ask" not in b.note, b.note)

    a = run(E.ask_capture_rate(_Ctx(caps=_Caps(None)), fallback_interval=1.5))
    check("but an UNASKABLE client really does say it could not ask",
          "could not ask" in a.note, a.note)

    # ── A broken ask must not masquerade as an unsupported one ───────────────
    ctx = _Ctx(boom=RuntimeError("stdio closed"))
    a = run(E.ask_capture_rate(ctx, fallback_interval=1.0))
    check("a raising client -> failed, NOT unsupported",
          a.how == E.HOW_FAILED, a.how)
    check("...and the real error survives into detail",
          "stdio closed" in a.detail, a.detail)
    check("...and into the note the caller shows the user",
          "stdio closed" in a.note, a.note)
    check("...and as_dict carries it too",
          "stdio closed" in str(a.as_dict().get("detail")), str(a.as_dict()))

    # ── Accepted-but-empty: the client said yes and sent nothing ─────────────
    a = run(E.ask_capture_rate(_Ctx(result=_Result("accept", _Data())),
                               fallback_interval=1.0))
    check("accept with no value is failed, not asked",
          a.how == E.HOW_FAILED and a.value == 1.0, f"{a.how} {a.value}")
    check("...and says what the client did", "returned no" in a.detail, a.detail)

    a = run(E.ask_capture_rate(_Ctx(result=_Result("weird")),
                               fallback_interval=1.0))
    check("an unrecognised action is failed and quotes the action",
          a.how == E.HOW_FAILED and "weird" in a.detail, a.detail)

    # ── ask() NEVER raises, whatever the client does ─────────────────────────
    class _Hostile:
        @property
        def client_capabilities(self):
            raise RuntimeError("boom")

        async def elicit(self, message, schema):
            raise RuntimeError("boom")

    try:
        a = run(E.ask_capture_rate(_Hostile(), fallback_interval=1.0))
        check("a hostile ctx cannot raise into the tool call",
              a.how == E.HOW_FAILED, a.how)
    except Exception as exc:
        check("a hostile ctx cannot raise into the tool call", False, repr(exc))

    # ── Consent: an explicit no is obeyed; a non-answer is never consent ─────
    a = run(E.ask_input_recording_consent(
        _Ctx(result=_Result("accept", _Data(record_input=True)))))
    check("an explicit yes records input", a.value is True and a.chosen_by_user)
    a = run(E.ask_input_recording_consent(
        _Ctx(result=_Result("accept", _Data(record_input=False)))))
    check("an explicit no is obeyed", a.value is False and a.chosen_by_user)
    check("...and the note says the user declined",
          "declined" in a.note.lower(), a.note)

    a = run(E.ask_input_recording_consent(_Ctx(caps=_Caps(None))))
    check("unaskable + safe default -> OFF", a.value is False, str(a.value))
    check("...and the note refuses to imply consent",
          "NOBODY CONSENTED" in a.note, a.note)
    a = run(E.ask_input_recording_consent(_Ctx(caps=_Caps(None)), fallback=True))
    check("unaskable + agent already selected it -> selection stands",
          a.value is True and a.how == E.HOW_UNSUPPORTED, f"{a.value} {a.how}")
    check("...but the note STILL says nobody consented",
          "NOBODY CONSENTED" in a.note, a.note)
    check("...and never claims the user chose",
          not a.chosen_by_user)

    # The wording is the safeguard here, so it gets asserted like code.
    ctx = _Ctx(result=_Result("accept", _Data(record_input=True)))
    run(E.ask_input_recording_consent(ctx))
    q = ctx.asked[0][0]
    check("the consent prompt says SYSTEM-WIDE in as many words",
          "SYSTEM-WIDE" in q, q[:160])
    check("...and that it stays on this machine", "this machine" in q, q[:200])

    # ── project_root: an unopened folder is not an absent signal ─────────────
    a = run(E.ask_project_root(_Ctx(result=_Result("accept",
                                                   _Data(project_root=" /x/y ")))))
    check("an answered path is stripped", a.value == "/x/y", repr(a.value))
    a = run(E.ask_project_root(_Ctx(caps=_Caps(None))))
    check("unaskable project_root keeps the empty fallback",
          a.value == "" and a.how == E.HOW_UNSUPPORTED, f"{a.value!r} {a.how}")
    check("...and the note names the real consequence, not just 'no root'",
          "absence of input" in a.note, a.note)

    # ── The schema the client will actually render must be spec-valid ────────
    # MCP permits primitives only. A nested or union field is accepted by
    # pydantic and rejected at the wire, i.e. it fails in a live client and
    # nowhere else.
    try:
        from mcp_types._v2025_11_25 import PrimitiveSchemaDefinition
    except Exception:
        PrimitiveSchemaDefinition = None
    if PrimitiveSchemaDefinition is None:
        print("  --   spec-schema check skipped (mcp_types not importable)")
    else:
        for label, coro_fn, kwargs in (
            ("capture rate", E.ask_capture_rate, {"fallback_interval": 1.0}),
            ("consent", E.ask_input_recording_consent, {}),
            ("project root", E.ask_project_root, {}),
        ):
            ctx = _Ctx(result=_Result("decline"))
            run(coro_fn(ctx, **kwargs))
            schema = ctx.asked[0][1].model_json_schema()
            props = schema.get("properties") or {}
            ok = bool(props)
            for prop in props.values():
                try:
                    PrimitiveSchemaDefinition.model_validate(prop)
                except Exception as exc:
                    ok = False
                    print(f"       {label}: {exc}")
            check(f"the {label} schema is spec-valid (primitives only)", ok,
                  str(props))
            check(f"the {label} field is documented for the user",
                  all((p.get("description") or "").strip() for p in props.values()),
                  str(props))

    # ── The MCP wiring, checked against the live tool objects ────────────────
    try:
        import api.claude_mcp as M
    except SystemExit:
        print("  --   MCP wiring checks skipped (mcp SDK not installed)")
        M = None
    except Exception as exc:
        print(f"  --   MCP wiring checks skipped ({type(exc).__name__}: {exc})")
        M = None

    if M is not None:
        tools = {t.name: t for t in asyncio.run(M.mcp.list_tools())}
        for name in ("av_capture_start", "av_capture_set_interval",
                     "av_bridge_catalog", "av_bridge_commit"):
            check(f"{name} is registered", name in tools)
            if name not in tools:
                continue
            props = set((tools[name].input_schema.get("properties") or {}))
            # ctx is injected by the SDK. If it leaked into the schema the
            # agent would try to supply it, and every call would fail.
            check(f"{name} does not expose ctx to the agent",
                  "ctx" not in props, str(sorted(props)))
            fn = getattr(M, name)
            check(f"{name} is async (it can await an ask)",
                  inspect.iscoroutinefunction(fn))

        check("av_capture_set_interval no longer requires an interval",
              "interval" not in
              (tools["av_capture_set_interval"].input_schema.get("required") or []),
              str(tools["av_capture_set_interval"].input_schema.get("required")))

        # The consent gate: only an explicit human "no" may edit the agent's plan.
        posted: list = []

        def _fake_post(path, body=None):
            posted.append((path, body))
            return {"ok": True, "state": "BUILT"}

        real_post, M._http_post = M._http_post, _fake_post
        try:
            plan = {"emitters": ["user_input", "lifecycle"],
                    "why": {"user_input": "keypress handler seen",
                            "lifecycle": "no start/stop markers"}}

            posted.clear()
            out = run(M.av_bridge_commit(
                plan=dict(plan),
                ctx=_Ctx(result=_Result("accept", _Data(record_input=False)))))
            sent = (posted[-1][1]["plan"]["emitters"] if posted else [])
            check("an explicit NO removes user_input from the committed plan",
                  "user_input" not in sent and "lifecycle" in sent, str(sent))
            check("...and drops its `why` entry so the plan stays consistent",
                  "user_input" not in (posted[-1][1]["plan"].get("why") or {}),
                  str(posted[-1][1]["plan"].get("why")))
            check("...and the response says what was removed and why",
                  "REMOVED" in str(out.get("input_recording_consent", {})),
                  str(out.get("input_recording_consent")))

            posted.clear()
            out = run(M.av_bridge_commit(plan=dict(plan),
                                         ctx=_Ctx(caps=_Caps(None))))
            sent = posted[-1][1]["plan"]["emitters"]
            check("an UNASKABLE client does not override the agent's selection",
                  "user_input" in sent, str(sent))
            blk = out.get("input_recording_consent") or {}
            check("...but the response states that nobody consented",
                  "NOBODY CONSENTED" in str(blk.get("note")), str(blk))
            check("...and tells the agent to offer a replan",
                  "replan" in str(blk.get("effect", "")).lower(), str(blk))
            check("...and never marks it as chosen by the user",
                  blk.get("chosen_by_user") is False, str(blk))

            posted.clear()
            out = run(M.av_bridge_commit(plan={"emitters": ["lifecycle"]},
                                         ctx=_Ctx(caps=_Caps(None))))
            check("a plan without user_input asks nothing at all",
                  "input_recording_consent" not in out, str(out))
        finally:
            M._http_post = real_post

        # The interval gate: a non-answer must not be written as a change.
        put: list = []
        real_put, real_get = M._http_put, M._http_get
        M._http_put = lambda p, b=None: (put.append((p, b)), {"ok": True})[1]
        M._http_get = lambda p, params=None: {"capture_rate": {"interval_seconds": 2.0}}
        try:
            put.clear()
            out = run(M.av_capture_set_interval(ctx=_Ctx(caps=_Caps(None))))
            check("an unanswerable set_interval writes NOTHING",
                  not put and out.get("changed") is False, str(put) + str(out))
            check("...and says so instead of reporting a new rate",
                  "NOT your choice" in
                  str((out.get("capture_rate_choice") or {}).get("note")),
                  str(out.get("capture_rate_choice")))

            put.clear()
            run(M.av_capture_set_interval(
                ctx=_Ctx(result=_Result("accept", _Data(shots_per_second=2.0)))))
            check("an answered set_interval writes the converted value",
                  put and abs(put[-1][1]["interval"] - 0.5) < 1e-9, str(put))

            put.clear()
            run(M.av_capture_set_interval(interval=0.4, ctx=_Ctx()))
            check("an explicit interval is honoured without asking",
                  put and put[-1][1]["interval"] == 0.4, str(put))
        finally:
            M._http_put, M._http_get = real_put, real_get

    print()
    print(f"{'FAILED: ' + str(len(FAILED)) if FAILED else 'all checks passed'}")
    for f in FAILED:
        print(f"  - {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
