"""S02/S03 delivery seam: ephemeral context, turn origin, media transcript.

Covers the adapter side of the framework contract:
- `pre_llm_call` returns the one frozen envelope under `ephemeral_context`
  (never `context`), identical bytes on every call within a turn (tool
  continuations included).
- When the framework seam (`agent.turn_context.SUPPORTS_EPHEMERAL_CONTEXT`)
  is absent, ephemeral delivery withholds and the legacy llm_request
  middleware delivers the same envelope as ONE appended request-only row.
- `turn_origin_for_event` supplies the server-created origin: kind
  direct/ambient/catch_up, response policy from config only; mismatched or
  unknown identity withholds (None), ambient-disabled adapters return None.
- Embedded-video transcripts ride the bundle/envelope, never the persisted
  user turn.
- Quiet resume is driven by the typed origin, not phrase matching.

Red on the base adapter: none of these symbols/behaviours exist there.

Run with the framework venv:

    /var/lib/hermes/.hermes/hermes-agent/venv/bin/python test_ephemeral_delivery.py
"""

import asyncio
import importlib.util
import os
import sys
import tempfile
import types

sys.path.insert(0, os.environ.get("HERMES_FRAMEWORK_PATH", "/var/lib/hermes/.hermes/hermes-agent"))
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="ephemeral-delivery-test-")

import plugins.platforms.discord.adapter as discord_adapter  # noqa: E402
from gateway.platforms.base import MessageEvent  # noqa: E402

discord_adapter.DiscordAdapter.__init__ = (
    lambda self, config: setattr(self, "config", config)
)

PLUGIN = os.environ.get("AMBIENT_PLUGIN_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "__init__.py"
)
spec = importlib.util.spec_from_file_location("ambient_ephemeral_test", PLUGIN)
ambient = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ambient)

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}: {detail}")
        FAILURES.append(name)


class Author:
    def __init__(self, uid, name, display=None, bot=False):
        self.id = str(uid)
        self.name = name
        self.display_name = display or name
        self.bot = bot


class Chan:
    def __init__(self, cid, name, guild=None, topic=None):
        self.id = str(cid)
        self.name = name
        self.guild = guild
        self.topic = topic
        self.parent = None


class Guild:
    def __init__(self, gid, name):
        self.id = str(gid)
        self.name = name


HOME = Guild(777, "Home")
GENERAL = Chan(501, "general", guild=HOME, topic="agents hanging out")
DM = Chan(505, None, guild=None)
ALICE = Author(101, "alice", "Alice A")


def _ensure_identity_var():
    """Production parity: AmbientDiscordAdapter.connect() ->
    _install_gateway_turn_scope() creates this shared ContextVar; a
    standalone fixture that never ran connect() must create it the same
    way or the turn-scope gate (correctly) clears the bundle."""
    from gateway import session_context
    import contextvars

    identity = getattr(session_context, "_ambient_turn_identity", None)
    if identity is None:
        identity = contextvars.ContextVar("ambient_turn_identity", default=None)
        session_context._ambient_turn_identity = identity
    return identity


def bind_scope(adapter, bundle, turn_id):
    identity = _ensure_identity_var()
    token = ambient._verified_run_context.set(bundle)
    id_token = identity.set(
        (bundle.get("message_id"), bundle.get("speaker_id"))
        if isinstance(bundle, dict)
        else None
    )
    try:
        ambient._on_pre_llm_call_turn_scope(turn_id=turn_id)
    finally:
        identity.reset(id_token)
        ambient._verified_run_context.reset(token)


def bind_scope_without_attestation(bundle, turn_id):
    """Set the run context with NO identity attestation: the gate must
    clear the bundle, and the ephemeral hook must withhold."""
    token = ambient._verified_run_context.set(bundle)
    try:
        ambient._on_pre_llm_call_turn_scope(turn_id=turn_id)
    finally:
        ambient._verified_run_context.reset(token)



class Message:
    def __init__(self, mid, author, channel, content=""):
        self.id = str(mid)
        self.author = author
        self.channel = channel
        self.content = content
        self.mentions = []
        self.attachments = []
        self.reference = None

    def __repr__(self):
        return f"<Message {self.id} {self.content!r}>"


def make_adapter(presence=None):
    cfg = {
        "enabled": True,
        "channels": ["*"],
        "probability": 0,
        "speaker_identity": True,
        "room_context": {"enabled": True, "recent_messages": 2},
    }
    if presence:
        cfg.update(presence)
    adapter = ambient.AmbientDiscordAdapter(
        types.SimpleNamespace(extra={"ambient_presence": cfg})
    )
    adapter._client = types.SimpleNamespace(user=Author(999, "agent"))
    return adapter


def event_for(message, user_id="101"):
    return MessageEvent(
        text=message.content,
        message_id=message.id,
        user_id=user_id,
        source=types.SimpleNamespace(chat_type="dm", user_id=user_id),
        raw_message=None,
    )


async def main():
    print("ephemeral delivery: one frozen envelope per turn")
    adapter = make_adapter()
    direct = Message(1, ALICE, DM, "what are you up to?")
    bundle = adapter._prepare_turn_context(direct)
    bind_scope(adapter, bundle, "t-direct")

    ambient._EPHEMERAL_OVERRIDE = True
    try:
        r1 = ambient._on_pre_llm_call_ambient_context(
            turn_id="t-direct", platform="discord"
        )
        check("pre_llm_call returns an ephemeral_context result",
              isinstance(r1, dict) and isinstance(r1.get("ephemeral_context"), str),
              repr(r1))
        check("the result never carries a persistent context key",
              isinstance(r1, dict) and "context" not in r1, repr(r1))
        env = r1["ephemeral_context"]
        check("the envelope is the reserved tagged bundle",
              env.startswith("<ambient_context ") and env.rstrip().endswith("</ambient_context>"),
              env[:60])
        r2 = ambient._on_pre_llm_call_ambient_context(
            turn_id="t-direct", platform="discord"
        )
        check(
            "tool continuations and retries see byte-identical context",
            r2["ephemeral_context"] == env,
            (len(r2["ephemeral_context"]), len(env)),
        )
        # Missing attestation withholds even with the seam present: the
        # turn-scope gate clears the unattested bundle, so no envelope
        # exists to deliver. The gate itself is NOT weakened.
        bind_scope_without_attestation(bundle, "t-unattested")
        unattested = ambient._on_pre_llm_call_ambient_context(
            turn_id="t-unattested", platform="discord"
        )
        check("a run context without identity attestation withholds context",
              not (isinstance(unattested, dict)
                   and unattested.get("ephemeral_context")),
              repr(unattested))
        check("the envelope labels the direct origin",
              'origin="direct"' in env, env[:200])
        check("the envelope carries the discretionary policy",
              'policy="discretionary"' in env, env[:400])
        check("the human request body never enters the envelope",
              "what are you up to" not in env, env)
        check("no private recall block is produced by the adapter",
              "memory_observation" not in env and "[known " not in env, env)
    finally:
        ambient._EPHEMERAL_OVERRIDE = None

    print("seam absent: withhold, warn, append no provider row")
    import logging as _logging

    captured: list[str] = []

    class _Capture(_logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    _handler = _Capture()
    ambient.logger.addHandler(_handler)
    ambient._EPHEMERAL_OVERRIDE = False
    try:
        withheld = ambient._on_pre_llm_call_ambient_context(
            turn_id="t-direct", platform="discord"
        )
        check("absent seam withholds the ephemeral projection",
              not (isinstance(withheld, dict) and withheld.get("ephemeral_context")),
              repr(withheld))
        check("an availability warning fires for the withheld turn",
              any("seam unavailable" in msg and "WITHHELD" in msg for msg in captured),
              captured[-3:])
        again = ambient._on_pre_llm_call_ambient_context(
            turn_id="t-direct", platform="discord"
        )
        check("a second call in the same turn does not warn again",
              not (isinstance(again, dict) and again.get("ephemeral_context"))
              and sum(1 for m in captured if "WITHHELD" in m) == 1,
              captured)
        request = {"messages": [{"role": "user", "content": "what are you up to?"}]}
        out = ambient._on_llm_request_bind_turn_scope(
            request=request, turn_id="t-direct", platform="discord"
        )
        check("no fallback row is appended on any path",
              out is None and len(request["messages"]) == 1,
              (repr(out), request))
        check("the task state still binds from the verified scope",
              ambient._gif_profile_context.get("") == bundle.get("gif_profile"),
              (ambient._gif_profile_context.get(""), bundle.get("gif_profile")))
    finally:
        ambient._EPHEMERAL_OVERRIDE = None
        ambient.logger.removeHandler(_handler)


    print("turn origin: adapter-supplied, config-derived policy, identity-gated")
    adapter = make_adapter()


    dm_turn = Message(2, ALICE, DM, "hello?")
    adapter._prepare_turn_context(dm_turn)
    origin = adapter.turn_origin_for_event(event_for(dm_turn))
    check("a direct turn reports kind direct",
          origin == {"event_id": "2", "kind": "direct", "response_policy": "discretionary"},
          repr(origin))

    hint_token = ambient._ambient_hint.set("[ambient: chime in only if short]")
    try:
        ambient_turn = Message(3, ALICE, GENERAL, "anyone tried the new build?")
        adapter._prepare_turn_context(ambient_turn)
    finally:
        ambient._ambient_hint.reset(hint_token)
    origin_amb = adapter.turn_origin_for_event(event_for(ambient_turn))
    check("an ambient join reports kind ambient",
          isinstance(origin_amb, dict) and origin_amb["kind"] == "ambient",
          repr(origin_amb))

    kind_token = ambient._turn_kind.set("catch_up")
    try:
        catchup_turn = Message(4, ALICE, GENERAL, "missed this earlier")
        adapter._prepare_turn_context(catchup_turn)
    finally:
        ambient._turn_kind.reset(kind_token)
    origin_catch = adapter.turn_origin_for_event(event_for(catchup_turn))
    check("a catch-up dispatch reports kind catch_up",
          isinstance(origin_catch, dict) and origin_catch["kind"] == "catch_up",
          repr(origin_catch))

    forged = event_for(dm_turn, user_id="202")
    check("a mismatched speaker cannot borrow another turn's origin",
          adapter.turn_origin_for_event(forged) is None,
          repr(adapter.turn_origin_for_event(forged)))
    unknown = event_for(Message(99, ALICE, DM, "never prepared"))
    check("an unprepared message id yields no origin",
          adapter.turn_origin_for_event(unknown) is None)

    stock = make_adapter({"enabled": False})
    stock_turn = Message(5, ALICE, DM, "work surface")
    stock._prepare_turn_context(stock_turn)
    check("an ambient-disabled (work) surface yields no origin",
          stock.turn_origin_for_event(event_for(stock_turn)) is None)

    strict = make_adapter({"response_policy": "required"})
    strict_turn = Message(6, ALICE, DM, "policy from config")
    strict._prepare_turn_context(strict_turn)
    origin_req = strict.turn_origin_for_event(event_for(strict_turn))
    check("response policy comes from config, not message text",
          isinstance(origin_req, dict) and origin_req["response_policy"] == "required",
          repr(origin_req))

    print("media transcripts ride the bundle, never the persisted user turn")
    adapter = make_adapter(
        {"embed_media": {
            "enabled": True, "auto_transcribe": True, "transcript_max_chars": 200,
        }}
    )

    import tools.transcription_tools as transcription  # noqa: E402
    real_transcribe = transcription.transcribe_audio
    transcription.transcribe_audio = lambda path: {
        "success": True,
        "transcript": "UNTRUSTED_VIDEO_WORDS " + "detail " * 400,
    }
    sent_rows = []
    base_send = discord_adapter.DiscordAdapter.send

    async def capture_send(self, chat_id, content, *a, **kw):
        sent_rows.append((chat_id, content))
        return types.SimpleNamespace(success=True, message_id="m1")

    discord_adapter.DiscordAdapter.send = capture_send
    try:
        media_token = ambient._media_transcripts.set([])
        try:
            event = MessageEvent(
                text="look at this",
                message_id="7",
                source=types.SimpleNamespace(chat_type="group", user_id="101"),
                raw_message=types.SimpleNamespace(
                    _ambient_embedded_video=True, content="look at this"
                ),
                media_urls=["/tmp/fake/video.mp4"],
                media_types=["video/mp4"],
            )
            await adapter._transcribe_embedded_video_event(event)
            check("the persisted user turn is not rewritten with the transcript",
                  event.text == "look at this", repr(event.text[:120]))
            staged = ambient._media_transcripts.get([])
            check("the transcript is staged for request-only delivery",
                  bool(staged) and "UNTRUSTED_VIDEO_WORDS" in staged[0]["text"],
                  repr(staged)[:200])
            check("transcript staging reports truncation",
                  bool(staged) and staged[0].get("truncated") is True,
                  repr(staged)[:200])

            msg = Message(7, ALICE, GENERAL, "look at this")
            msg.attachments = [types.SimpleNamespace(url="x")]
            bundle_m = adapter._prepare_turn_context(msg)
            env_m = bundle_m.get("envelope") or ""
            check("the envelope carries the untrusted transcript block",
                  "media_transcript" in env_m and "UNTRUSTED_VIDEO_WORDS" in env_m,
                  env_m[:400])
            check("the transcript block is labelled untrusted",
                  'trust="untrusted"' in env_m, env_m[:400])
        finally:
            ambient._media_transcripts.reset(media_token)
    finally:
        transcription.transcribe_audio = real_transcribe
        discord_adapter.DiscordAdapter.send = base_send

    print("quiet resume follows the typed origin, not phrases")
    original_cfg = ambient._ambient_cfg
    ambient._ambient_cfg = lambda key, default=None: (
        True if key == "quiet_resume" else default
    )
    try:
        staged = ambient._on_pre_llm_call_quiet_resume(
            user_message="completely ordinary text with no marker phrases",
            conversation_history=[],
            turn_origin={"kind": "resume", "response_policy": "required"},
        )
        check("a typed resume origin stages the directive without phrase matching",
              staged is None and ambient._quiet_resume_pending.get(False) is True,
              (repr(staged), ambient._quiet_resume_pending.get(False)))

        ambient._quiet_resume_pending.set(False)
        no_origin = ambient._on_pre_llm_call_quiet_resume(
            user_message="completely ordinary text with no marker phrases",
            conversation_history=[],
        )
        check("without a typed origin, ordinary text stages nothing",
              no_origin is None and ambient._quiet_resume_pending.get(False) is False,
              repr(ambient._quiet_resume_pending.get(False)))

        ambient._quiet_resume_pending.set(False)
        legacy = ambient._on_pre_llm_call_quiet_resume(
            user_message="The previous turn was interrupted. "
            "The gateway is now back online.",
            conversation_history=[],
        )
        check("a user typing the resume phrases stages nothing",
              legacy is None and ambient._quiet_resume_pending.get(False) is False,
              repr(ambient._quiet_resume_pending.get(False)))

        ambient._quiet_resume_pending.set(True)
        bind_scope(adapter, None, "t-resume")
        ambient._EPHEMERAL_OVERRIDE = True
        try:
            r = ambient._on_pre_llm_call_ambient_context(
                turn_id="t-resume", platform="discord",
                turn_origin={"kind": "resume", "response_policy": "required"},
            )
            env_r = (r or {}).get("ephemeral_context") or ""
            check("a resume turn gets an explicitly synthetic envelope",
                  env_r.startswith("<ambient_context ") and 'origin="resume"' in env_r,
                  env_r[:120])
            check("the resume directive is typed, not attributed to a human",
                  'kind="quiet_resume"' in env_r and "gateway restarted" in env_r,
                  env_r[:400])
            check("the pending resume flag is consumed exactly once",
                  ambient._quiet_resume_pending.get(False) is False,
                  repr(ambient._quiet_resume_pending.get(False)))
        finally:
            ambient._EPHEMERAL_OVERRIDE = None
            ambient._quiet_resume_pending.set(False)
    finally:
        ambient._ambient_cfg = original_cfg

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        raise SystemExit(1)
    print("all checks passed")


asyncio.run(main())
