"""
AgentVision — ELICITATION (asking the user, or saying plainly that we could not)
================================================================================

THE HOLE THIS FILLS. Six places in this codebase tell the connected agent to
"ASK THE USER, do not assume" — the capture frame rate, consent to record the
human's keystrokes, which folder holds the code. AgentVision had no mechanism
behind that instruction, so in practice the agent assumed, and a default was
presented to the user afterwards as though it had been a decision.

MCP's answer is `elicitation/create`: the server asks, the client renders a
form, the user answers. It is available in the installed SDK
(`Context.elicit(message, schema)`), and Claude Code supports it.

THE RULE THIS MODULE EXISTS TO KEEP. Elicitation is CLIENT-DEPENDENT. Claude
Desktop does not support it; a stateless HTTP caller has no user at all. So a
call that cannot be made must not look like a call that was made and answered.
Every function here returns an `Answer(value, how, note)`:

    how == "asked"        the user answered; `value` is THEIR choice
    how == "declined"     the user said no       — `value` is the fallback
    how == "cancelled"    the user dismissed it  — `value` is the fallback
    how == "unsupported"  the client declared no elicitation capability
    how == "no_context"   no MCP session at all (HTTP, CLI, a test)
    how == "failed"       the ask was attempted and errored; `note` says how

Only `"asked"` means a human chose. Everything else means AgentVision picked,
and `note` is the sentence that says so out loud. Callers are expected to put
that sentence in their response — that is the whole point of the module, and
the reason it returns a note rather than a bare value.

WHAT IT NEVER DOES. It never raises into a tool call, and it never turns a
missing capability into a silent default. `"unsupported"` is reported only when
the client actually declared its capabilities and elicitation was absent; if
capabilities are unknown, the ask is attempted and a genuine error is reported
as `"failed"` with the error text, not relabelled as "unsupported".

Pure stdlib at import time. `pydantic` and `mcp` are imported lazily inside the
functions that need them, so this module imports cleanly (and is unit-testable)
on an interpreter that has neither.
"""

from __future__ import annotations

from typing import Any, NamedTuple

# ── The six honest answers ────────────────────────────────────────────────────
HOW_ASKED = "asked"
HOW_DECLINED = "declined"
HOW_CANCELLED = "cancelled"
HOW_UNSUPPORTED = "unsupported"
HOW_NO_CONTEXT = "no_context"
HOW_FAILED = "failed"

#: The only value that means a human actually chose. Everything else is a
#: fallback wearing a value's clothes, which is exactly the confusion this
#: project exists to prevent.
HOW_VALUES = (HOW_ASKED, HOW_DECLINED, HOW_CANCELLED,
              HOW_UNSUPPORTED, HOW_NO_CONTEXT, HOW_FAILED)


class Answer(NamedTuple):
    """What came back, how it came back, and the sentence that admits which.

    `note` is not decoration. A caller that drops it turns "AgentVision could
    not ask, so it guessed" back into "1.0 seconds", which is the bug.
    """

    value: Any
    how: str
    note: str
    #: Only set for how == "failed": the actual error, verbatim. A caller that
    #: rewrites `note` must carry this through, or "failed" degrades into an
    #: unexplained shrug — the same shape as the silence this project fights.
    detail: str = ""

    @property
    def chosen_by_user(self) -> bool:
        return self.how == HOW_ASKED

    def as_dict(self) -> dict:
        """The shape to embed in a tool response under a descriptive key."""
        out = {"value": self.value, "how": self.how, "note": self.note,
               "chosen_by_user": self.chosen_by_user}
        if self.detail:
            out["detail"] = self.detail
        return out


# ── Capability probing ────────────────────────────────────────────────────────

def supports_elicitation(ctx: Any) -> bool | None:
    """True / False / None — and None genuinely means "not stated".

    `Context.client_capabilities` is None for a client that declared none (an
    anonymous stateless request). Reporting that as False would assert the
    client cannot do something we never asked it about.
    """
    if ctx is None:
        return False
    try:
        caps = ctx.client_capabilities
    except Exception:
        return None
    if caps is None:
        return None
    try:
        return getattr(caps, "elicitation", None) is not None
    except Exception:
        return None


def supports_subscriptions(ctx: Any) -> bool | None:
    """Whether the client can receive resource-updated pushes.

    There is no client capability for this in the spec — subscription support
    is a protocol-version property, not a declared flag — so this reports what
    can actually be checked: whether a session exists to push over. None means
    unknown, and unknown is reported as unknown.
    """
    if ctx is None:
        return False
    try:
        return ctx.session is not None
    except Exception:
        return None


def client_report(ctx: Any) -> dict:
    """What THIS client can do, and what AgentVision does when it cannot.

    A cold agent should never have to discover a missing capability by having
    something quietly not happen. Every entry pairs the capability with the
    consequence, because "elicitation: false" on its own tells an agent nothing
    it can act on.

    Tri-state throughout: `null` means the client did not say, which is not the
    same as "no" and is never reported as one.
    """
    el = supports_elicitation(ctx)
    sub = supports_subscriptions(ctx)
    out: dict = {
        "elicitation": el,
        "elicitation_means": {
            True: ("AgentVision can put a question to the user directly. Call "
                   "av_capture_start() with no interval and it will ask them "
                   "what frame rate they want."),
            False: ("This client cannot show a prompt. AgentVision will use "
                    "its documented fallbacks and label them as fallbacks — "
                    "look for `capture_rate_choice` / "
                    "`input_recording_consent` in responses, and relay what "
                    "they say. YOU should ask the user in prose instead."),
            None: ("This client did not declare its capabilities. AgentVision "
                   "will TRY to ask and report what actually happened, rather "
                   "than assuming either way."),
        }[el],
        "resource_subscriptions": sub,
        "resource_subscriptions_means": {
            True: ("AgentVision can notify this session when the digest or the "
                   "latest frame changes, so state can reach you without a "
                   "tool call."),
            False: ("No session to push over. Everything here is pull-only: "
                    "call av_ambient() or read agentvision://digest when you "
                    "want to know if something changed."),
            None: "Unknown — treat push as unavailable and poll instead.",
        }[sub],
    }
    try:
        pv = getattr(ctx, "protocol_version", None)
        if pv:
            out["protocol_version"] = str(pv)
    except Exception:
        pass
    if ctx is None:
        # "false" is correct — nothing can be asked — but the stock wording
        # blames a client that is not there. Say what is actually the case.
        out["why_no_client"] = ("this call did not arrive over an MCP session "
                                "(HTTP, the CLI, or a test), so there is no "
                                "client to describe")
        out["elicitation_means"] = (
            "There is no MCP session here, so there is nobody to prompt. "
            "AgentVision will use its documented fallbacks and label them as "
            "fallbacks. This says nothing about what your client supports.")
        out["resource_subscriptions_means"] = (
            "There is no MCP session here to push over. This says nothing "
            "about what your client supports.")
    return out


# ── Schema construction (pydantic, lazily) ────────────────────────────────────

def _single_field_model(model_name: str, field: str, annotation: Any,
                        description: str, **constraints: Any):
    """A one-field pydantic model, which is all the MCP spec permits anyway.

    Elicitation schemas may only contain primitives — no nesting, no unions —
    so a purpose-built single-field model per question is both the simplest and
    the only spec-valid shape.
    """
    from pydantic import Field, create_model
    return create_model(  # type: ignore[call-overload]
        model_name,
        **{field: (annotation, Field(description=description, **constraints))},
    )


# ── The core ask ──────────────────────────────────────────────────────────────

async def ask(ctx: Any, message: str, schema: Any, *, field: str,
              fallback: Any, subject: str) -> Answer:
    """Ask the user one question. Never raises. Always says how it went.

      ctx       the MCP Context (None when there is no session — that is fine)
      message   what the user reads. Say what it controls and what it costs.
      schema    a pydantic model with ONE primitive field
      field     that field's name, used to pull the value back out
      fallback  what AgentVision will use if it cannot ask, or is refused
      subject   a short noun phrase for the note ("the capture rate")
    """
    if ctx is None:
        return Answer(fallback, HOW_NO_CONTEXT,
                      _note(subject, fallback, HOW_NO_CONTEXT))

    supported = supports_elicitation(ctx)
    if supported is False:
        return Answer(fallback, HOW_UNSUPPORTED,
                      _note(subject, fallback, HOW_UNSUPPORTED))

    try:
        result = await ctx.elicit(message=message, schema=schema)
    except Exception as e:                      # transport, capability, timeout
        why = f"{type(e).__name__}: {e}"
        return Answer(fallback, HOW_FAILED,
                      _note(subject, fallback, HOW_FAILED, detail=why), why)

    action = getattr(result, "action", "")
    if action == "accept":
        data = getattr(result, "data", None)
        value = getattr(data, field, None)
        if value is None:
            # Accepted but empty. Calling that an answer would be the same
            # class of lie as calling an unreadable screen a clean one.
            why = f"the client accepted but returned no {field!r}"
            return Answer(fallback, HOW_FAILED,
                          _note(subject, fallback, HOW_FAILED, detail=why), why)
        return Answer(value, HOW_ASKED, _note(subject, value, HOW_ASKED))
    if action == "decline":
        return Answer(fallback, HOW_DECLINED,
                      _note(subject, fallback, HOW_DECLINED))
    if action == "cancel":
        return Answer(fallback, HOW_CANCELLED,
                      _note(subject, fallback, HOW_CANCELLED))
    why = f"unrecognised elicitation action {action!r}"
    return Answer(fallback, HOW_FAILED,
                  _note(subject, fallback, HOW_FAILED, detail=why), why)


#: Cases where the question WAS put to the user. Saying "AgentVision could not
#: ask you" after a decline is itself a false statement — it asked, and was
#: refused — and this module cannot be the one place that gets that wrong.
_WAS_ASKED = (HOW_DECLINED, HOW_CANCELLED)


def _note(subject: str, value: Any, how: str, detail: str = "") -> str:
    """The sentence a caller must pass through. Blunt on purpose."""
    if how == HOW_ASKED:
        return f"{subject}: {value} — you chose this."
    tail = f" ({detail})" if detail else ""
    if how in _WAS_ASKED:
        verb = ("you declined to answer" if how == HOW_DECLINED
                else "you dismissed the question")
        return (f"{subject}: {value} — {verb}, so this is AgentVision's "
                f"default, NOT your choice{tail}.")
    why = {
        HOW_UNSUPPORTED: "this MCP client cannot show a prompt "
                         "(no elicitation capability)",
        HOW_NO_CONTEXT: "there is no interactive session to ask through",
        HOW_FAILED: "the attempt to ask failed",
    }.get(how, how)
    return (f"{subject}: {value} — AgentVision could not ask you, so this is "
            f"its default, NOT your choice. Reason: {why}{tail}.")


# ── The three questions AgentVision actually needs to ask ─────────────────────
# Kept here, together, because the prompt wording IS the user-facing surface and
# deserves to be reviewable in one place.

#: Interval bounds the bridge enforces, in seconds per shot.
MIN_INTERVAL = 0.1
MAX_INTERVAL = 10.0


async def ask_capture_rate(ctx: Any, *, fallback_interval: float,
                           current_interval: float | None = None) -> Answer:
    """How many screenshots per second? Returns an INTERVAL in seconds.

    Asked in shots-per-second because that is how a human thinks about it, and
    converted here, because `interval = 1 / fps` is exactly the kind of
    conversion an agent gets backwards.
    """
    now = ""
    if current_interval:
        try:
            now = (f" It is currently {_fps(current_interval):g} shot(s)/sec "
                   f"({float(current_interval):g}s between shots).")
        except Exception:
            now = ""
    message = (
        "How many screenshots per second should AgentVision take of the program "
        f"you are debugging?{now}\n\n"
        "Range 0.1 to 10. Faster catches quick things — an animation, a race, "
        "the moment of a crash. Slower is enough for long idle waits and keeps "
        "less on disk. Capturing is free local CPU; it costs you nothing per "
        "frame, and no image is sent anywhere unless the agent asks for one."
    )
    schema = _single_field_model(
        "CaptureRate", "shots_per_second", float,
        "Screenshots per second, between 0.1 and 10",
        ge=0.1, le=10.0)
    ans = await ask(ctx, message, schema, field="shots_per_second",
                    fallback=fallback_interval, subject="capture rate")
    if ans.how != HOW_ASKED:
        # Restate the fallback in BOTH units. A note that says "capture rate:
        # 1.0" is ambiguous between one shot per second and one second apart,
        # and those differ by 100x at the ends of the range.
        units = (f"capture rate: {_fps(fallback_interval):g} shot(s)/sec "
                 f"({float(fallback_interval):g}s between shots)")
        if ans.how in _WAS_ASKED:
            verb = ("you declined to answer" if ans.how == HOW_DECLINED
                    else "you dismissed the question")
            return Answer(fallback_interval, ans.how,
                          f"{units} — {verb}, so this is AgentVision's default, "
                          f"NOT your choice{_tail(ans.detail, parens=True)}.",
                          ans.detail)
        return Answer(
            fallback_interval, ans.how,
            f"{units} — AgentVision could not ask you, so this is its default, "
            f"NOT your choice. Reason: {_why(ans.how)}{_tail(ans.detail)}.",
            ans.detail)
    interval = _clamp(1.0 / float(ans.value), MIN_INTERVAL, MAX_INTERVAL)
    return Answer(interval, HOW_ASKED,
                  f"capture rate: {float(ans.value):g} shot(s)/sec "
                  f"({interval:g}s between shots) — you chose this.")


async def ask_input_recording_consent(ctx: Any, *,
                                      fallback: bool = False) -> Answer:
    """Consent to record the human's keystrokes and clicks.

    This emitter runs a SYSTEM-WIDE input daemon, so an explicit "no" must be
    obeyed. What happens when nobody CAN be asked is the caller's decision, and
    the two live callers legitimately differ:

      fallback=False  a fresh install picking a safe default
      fallback=True   `av_bridge_commit`, where the agent already selected the
                      emitter and refusing it in a client that cannot show a
                      prompt would silently override a stated decision

    Either way, `how != "asked"` means nobody consented, and the note says so.
    """
    message = (
        "AgentVision can record your keystrokes and mouse clicks so the agent "
        "can see what you did just before a failure.\n\n"
        "This is SYSTEM-WIDE while capture is running — it does not stop at the "
        "window being debugged. Everything stays on this machine, in this "
        "project's log files. Only what the agent explicitly reads reaches a "
        "model.\n\n"
        "Record your input?"
    )
    schema = _single_field_model(
        "InputRecordingConsent", "record_input", bool,
        "Yes to record keystrokes and clicks system-wide while capturing")
    ans = await ask(ctx, message, schema, field="record_input",
                    fallback=bool(fallback), subject="input recording")
    if ans.how == HOW_ASKED:
        return Answer(bool(ans.value), HOW_ASKED,
                      f"input recording: {'ON — you consented' if ans.value else 'OFF — you declined'}.")
    state = "ON" if fallback else "OFF"
    if ans.how in _WAS_ASKED:
        # Asked and refused. NOBODY CONSENTED still holds — a dismissal is not
        # a yes — but claiming the question was never put would be false.
        return Answer(bool(fallback), ans.how,
                      f"input recording: {state}, and NOBODY CONSENTED — "
                      f"{_why(ans.how)}. This is AgentVision's fallback, not "
                      f"your answer{_tail(ans.detail, parens=True)}.", ans.detail)
    return Answer(bool(fallback), ans.how,
                  f"input recording: {state}, and NOBODY CONSENTED — AgentVision "
                  f"could not ask you. This is its fallback, not your answer. "
                  f"({_why(ans.how)}{_tail(ans.detail)})", ans.detail)


async def ask_project_root(ctx: Any, *, fallback: str = "") -> Answer:
    """Which folder holds the code? Only asked when nothing else knows."""
    message = (
        "Which folder holds the source code of the program you want AgentVision "
        "to watch?\n\n"
        "It reads that folder to work out what logging the program already has "
        "and what is worth adding. Without it, the code scan reports zero "
        "signals — not because the program has none, but because nothing was "
        "opened. Give an absolute path."
    )
    schema = _single_field_model(
        "ProjectRoot", "project_root", str,
        "Absolute path to the folder containing the program's source code")
    ans = await ask(ctx, message, schema, field="project_root",
                    fallback=fallback, subject="project root")
    if ans.how == HOW_ASKED:
        return Answer(str(ans.value).strip(), HOW_ASKED,
                      f"project root: {str(ans.value).strip()} — you chose this.")
    return Answer(fallback, ans.how,
                  f"project root: not set. The code scan will report zero "
                  f"signals because no folder was opened — that is an absence "
                  f"of input, not an absence of signals. "
                  f"({_why(ans.how)}{_tail(ans.detail)})", ans.detail)


# ── small helpers ─────────────────────────────────────────────────────────────

def _why(how: str) -> str:
    return {
        HOW_DECLINED: "you declined",
        HOW_CANCELLED: "you dismissed the question",
        HOW_UNSUPPORTED: "this MCP client cannot show a prompt",
        HOW_NO_CONTEXT: "there is no interactive session to ask through",
        HOW_FAILED: "the attempt to ask failed",
    }.get(how, how)


def _tail(detail: str, parens: bool = False) -> str:
    if not detail:
        return ""
    return f" ({detail})" if parens else f": {detail}"


def _fps(interval: float) -> float:
    try:
        return 1.0 / float(interval) if float(interval) else 0.0
    except Exception:
        return 0.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)
