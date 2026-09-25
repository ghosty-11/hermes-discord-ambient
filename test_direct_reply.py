"""Direct Discord turns: origin admission, wire-only context, honest receipts.

Covers the S02/S03 cutover: a verified direct turn vouches for a
discretionary TurnOrigin, its private directive rides the pre_llm_call
ephemeral seam (never a synthetic request row), a direct silence token is
delivered as REAL silence with an empty nonphantom receipt, and the stock
silence filters are left untouched for every other seat.

Run with the framework venv:

    /var/lib/hermes/.hermes/hermes-agent/venv/bin/python test_direct_reply.py
"""

import asyncio
import importlib.util
import os
import sys
import tempfile
import types
from unittest.mock import patch


sys.path.insert(0, os.environ.get("HERMES_FRAMEWORK_PATH", "/var/lib/hermes/.hermes/hermes-agent"))
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="direct-reply-test-")

import plugins.platforms.discord.adapter as discord_adapter  # noqa: E402
from gateway.platforms.base import MessageEvent  # noqa: E402
from gateway import response_filters  # noqa: E402


# Avoid opening a live Discord client while retaining the real adapter class.
discord_adapter.DiscordAdapter.__init__ = (
    lambda self, config: setattr(self, "config", config)
)

PLUGIN = os.environ.get("AMBIENT_PLUGIN_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "__init__.py"
)
spec = importlib.util.spec_from_file_location("ambient_direct_reply_test", PLUGIN)
ambient = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ambient)

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


class Author:
    def __init__(self, uid, name):
        self.id = uid
        self.name = name
        self.display_name = name


class Reference:
    def __init__(self, message_id, resolved):
        self.message_id = message_id
        self.resolved = resolved


class RepliedMessage:
    def __init__(self, author, content):
        self.author = author
        self.content = content


class RawMessage:
    def __init__(self, reference, message_id="200", author=None, mentions=None):
        self.reference = reference
        self.id = message_id
        self.content = "good girl"
        self.author = author
        self.mentions = mentions or []
        self.channel = types.SimpleNamespace(id="channel-1")


class HistoryChannel:
    def __init__(self, channel_id, messages):
        self.id = channel_id
        self._messages = messages

    def history(self, *, limit):
        async def rows():
            for message in self._messages[:limit]:
                message.channel = self
                yield message

        return rows()


def make_adapter(*, discretionary=True, owner="social"):
    config = types.SimpleNamespace(
        extra={
            "ambient_presence": {
                "enabled": True,
                "discretionary_replies": discretionary,
                "channels": ["*"],
                "silent_marker": "[SILENT]",
                "text_hygiene": {"resolve_plain_mentions": True},
                "reply_style": {
                    "enabled": True,
                    "standalone_marker": "[STANDALONE]",
                },
            }
        }
    )
    adapter = ambient.AmbientDiscordAdapter(config)
    if owner is not None:
        adapter.set_owner_profile(owner)
    adapter._client = types.SimpleNamespace(user=Author(999, "Muse"))
    return adapter


async def main():
    adapter = make_adapter()
    replied = RepliedMessage(adapter._client.user, "photo response")
    raw = RawMessage(Reference("199", replied))
    event = MessageEvent(
        text="good girl",
        raw_message=raw,
        message_id="200",
        reply_to_message_id="199",
        reply_to_text="photo response",
        source=types.SimpleNamespace(chat_type="group"),
    )

    print("\n-- reply ownership survives normalization --")
    adapter._annotate_reply_context(event)
    check("reply author id is preserved", event.reply_to_author_id == "999")
    check("reply author name is preserved", event.reply_to_author_name == "Muse")
    check("reply to this bot is marked as own", event.reply_to_is_own_message is True)
    check("reply to this bot is a direct turn", adapter._event_is_direct(event) is True)
    origin = adapter.turn_origin_for_event(event)
    check(
        "the adapter vouches for a discretionary direct social turn",
        getattr(origin, "event_id", None) == "200"
        and getattr(origin, "kind", None) == "direct"
        and getattr(origin, "response_policy", None) == "discretionary",
        repr(origin),
    )
    root_adapter = make_adapter(owner=None)
    check(
        "unprofiled work surface stays required even with ambient presence enabled",
        root_adapter.turn_origin_for_event(event) is None,
    )
    unapproved = make_adapter(discretionary=False)
    check(
        "a profile without the explicit social-turn opt-in stays required",
        unapproved.turn_origin_for_event(event) is None,
    )
    check(
        "a suppressed send cannot claim text was delivered",
        getattr(adapter._suppressed_result(), "delivered_text", None) == "",
    )
    check(
        "own reply text is not relabeled as another person",
        event.reply_to_text == "photo response",
        repr(event.reply_to_text),
    )

    other_event = MessageEvent(
        text="good point",
        raw_message=RawMessage(
            Reference("198", RepliedMessage(Author(123, "Alice"), "earlier text")),
            message_id="201",
        ),
        message_id="201",
        reply_to_message_id="198",
        reply_to_text="earlier text",
        source=types.SimpleNamespace(chat_type="group"),
    )
    adapter._annotate_reply_context(other_event)
    check(
        "replying to another person is not marked as own",
        other_event.reply_to_is_own_message is False,
    )
    check(
        "replying to another person is not promoted to a direct turn",
        adapter._event_is_direct(other_event) is False,
    )
    check(
        "reply context names the other person",
        other_event.reply_to_text == "Alice wrote: earlier text",
        repr(other_event.reply_to_text),
    )

    print("\n-- direct context rides the wire-only ephemeral seam --")
    seen = {}

    async def handler(scoped_event):
        # The private direct directive now leaves through pre_llm_call, on
        # the framework's ephemeral_context seam — never as a synthetic
        # request row the way the old llm_request injection did.
        # The gateway normally binds this scope after matching the inbound
        # event's speaker and message id; this adapter-level test supplies it.
        turn_id = "direct-test-200"
        with ambient._turn_scopes_lock:
            ambient._turn_scopes[turn_id] = {
                "speaker_id": "42", "message_id": "200",
                "direct_address": True,
            }
        try:
            seen["ephemeral"] = ambient._on_pre_llm_call_ambient_context(
                user_message=scoped_event.text, platform="discord", turn_id=turn_id,
            )
        finally:
            with ambient._turn_scopes_lock:
                ambient._turn_scopes.pop(turn_id, None)
        seen["request"] = ambient._on_llm_request_ambient_hint(
            request={"messages": [{"role": "user", "content": scoped_event.text}]},
            platform="discord",
        )
        seen["suppressed"] = response_filters.is_intentional_silence_agent_result(
            {"failed": False}, "[SILENT]"
        )
        return None

    adapter.set_message_handler(handler)
    await adapter._message_handler(event)
    ephemeral = (seen.get("ephemeral") or {}).get("ephemeral_context", "")
    check(
        "the direct directive rides pre_llm_call ephemeral_context",
        "direct address" in ephemeral.lower(),
        repr(ephemeral[:120]),
    )
    check(
        "the private block is wrapped for outbound screening",
        ephemeral.startswith("<ambient_context>")
        and ephemeral.rstrip().endswith("</ambient_context>"),
        repr(ephemeral[:60]),
    )
    check(
        "llm_request no longer appends private synthetic rows",
        seen.get("request") is None,
        repr(seen.get("request")),
    )
    check(
        "the silence filters are left stock — no adapter monkey-patch remains",
        not hasattr(ambient, "_install_direct_silence_filter")
        and seen.get("suppressed") is True,
        repr(seen.get("suppressed")),
    )
    from gateway.turn_origin import (  # noqa: E402
        RESPONSE_POLICY_REQUIRED,
        turn_origin_allows_silence,
    )

    check(
        "silence permission follows the server-signed origin, not scope",
        turn_origin_allows_silence(origin and origin.as_dict()) is True
        and turn_origin_allows_silence(
            {"kind": "direct", "response_policy": RESPONSE_POLICY_REQUIRED}
        ) is False
        and turn_origin_allows_silence(None) is False,
    )

    print("\n-- reply placement guidance is request-only --")
    original_cfg = ambient._ambient_cfg
    ambient._ambient_cfg = lambda key, default=None: (
        {
            "enabled": True,
            "standalone_marker": "[STANDALONE]",
        }
        if key == "reply_style"
        else default
    )
    try:
        request = {"messages": [{"role": "user", "content": "hello"}]}
        patched = ambient._on_llm_request_ambient_hint(request=request)
        messages = ((patched or {}).get("request") or {}).get("messages", [])
        check(
            "the model receives standalone-placement guidance",
            bool(messages)
            and "[STANDALONE]" in messages[-1].get("content", "")
            and "room-wide" in messages[-1].get("content", "").lower(),
            repr(messages),
        )
        check(
            "reply-placement guidance does not mutate the persisted request",
            len(request["messages"]) == 1,
            repr(request["messages"]),
        )
        cron_patched = ambient._on_llm_request_ambient_hint(
            request={"messages": [{"role": "user", "content": "hello"}]},
            platform="cron",
        )
        check(
            "a cron turn is not told about Discord reply placement",
            cron_patched is None,
            repr(cron_patched),
        )
        discord_patched = ambient._on_llm_request_ambient_hint(
            request={"messages": [{"role": "user", "content": "hello"}]},
            platform="discord",
        )
        discord_messages = (
            ((discord_patched or {}).get("request") or {}).get("messages", [])
        )
        check(
            "a Discord turn still receives the placement guidance",
            bool(discord_messages)
            and "[STANDALONE]" in discord_messages[-1].get("content", ""),
            repr(discord_messages),
        )
    finally:
        ambient._ambient_cfg = original_cfg

    print("\n-- outbound direct silence is real silence --")
    sent = []

    async def fake_send(self, chat_id, content, *args, **kwargs):
        sent.append((chat_id, content, kwargs.get("reply_to")))
        return types.SimpleNamespace(success=True, message_id="sent-1")

    discord_adapter.DiscordAdapter.send = fake_send
    adapter._direct_note_event(event)
    result = await adapter.send("channel-1", "[SILENT]", reply_to="200")
    check("nothing is sent for a direct silence token", not sent, repr(sent))
    check(
        "the suppressed send reports an honest empty receipt",
        getattr(result, "success", False) is True
        and getattr(result, "delivered_text", None) == ""
        and "suppressed" in str(getattr(result, "error", "")),
        repr(result),
    )
    check(
        "the completed direct marker closes its pending entry",
        "200" not in adapter._direct_pending,
        repr(adapter._direct_pending),
    )
    await adapter.send("channel-1", "[SILENT]", reply_to="200")
    check("a repeated silence still sends nothing", not sent, repr(sent))
    private_echo = "PRIVATE_ECHO_CANARY"
    with patch.object(ambient.logger, "warning") as warning:
        screened = await adapter.send(
            "channel-1",
            f"<ambient_context>{private_echo}</ambient_context> Hello.",
            reply_to="echo-1",
        )
    logged = repr([call.args for call in warning.call_args_list])
    check("private context never enters echo-warning logs",
          private_echo not in logged, logged[:160])
    check("screened reply reports only its delivered words",
          sent[-1][1] == "Hello."
          and getattr(screened, "delivered_text", None) == "Hello.")

    # A companion profile sends one ledgered final, not a prose send plus
    # untracked URL sends. The pending GIF must still ride that same receipt.
    adapter.config.extra["ambient_presence"]["text_hygiene"]["isolate_media_urls"] = False
    sent.clear()
    media_line = "Nice one. https://example.com/cat.gif"
    media_result = await adapter.send("channel-1", media_line, reply_to="media-1")
    check("media URL and prose use one tracked delivery",
          sent == [("channel-1", media_line, "media-1")]
          and getattr(media_result, "delivered_text", None) == media_line,
          repr(sent))
    sent.clear()
    gif_url = "https://example.com/pending.gif"
    gif_bucket = ambient._gif_bucket(adapter._bot_user_id() or None)
    gif_bucket["pending"] = (gif_url, ambient.time.time())
    gif_result = await adapter.send("channel-1", "Cat approval.", reply_to="gif-1")
    check("pending GIF is attached to the same ledgered message",
          len(sent) == 1
          and gif_url in sent[0][1]
          and getattr(gif_result, "delivered_text", None) == sent[0][1],
          repr(sent))
    check("pending GIF is consumed once", gif_bucket.get("pending") is None)

    from gateway.platforms.base import BasePlatformAdapter, SendResult  # noqa: E402

    async def failing_send(self, chat_id, content, *args, **kwargs):
        sent.append((chat_id, content, kwargs.get("reply_to")))
        return SendResult(success=False, error="simulated Discord reject")

    discord_adapter.DiscordAdapter.send = failing_send
    old_spoke = adapter._travel_spoke
    travel_spoke = []
    adapter._travel_spoke = lambda *_args: travel_spoke.append(True)
    try:
        sent.clear()
        failed = await adapter.send(
            "channel-1", "Not sent. https://example.com/no.gif",
            reply_to="fail-1",
        )
        outcome = BasePlatformAdapter._turn_delivery_outcome(
            [failed], hint=None, streamed_text=None,
        )
        check("failed media reply has one failed receipt and no visible text",
              len(sent) == 1 and not failed.success
              and outcome["disposition"] == "failure"
              and not outcome["message_ids"], repr(outcome))
        check("a failed platform send never counts as room speech",
              not travel_spoke, repr(travel_spoke))
    finally:
        adapter._travel_spoke = old_spoke
        discord_adapter.DiscordAdapter.send = fake_send

    print("\n-- Discord replies are default; room-wide remarks may stand alone --")
    await adapter.send("channel-1", "A direct answer.", reply_to="placement-1")
    check(
        "ordinary output keeps its Discord reply reference",
        sent[-1] == ("channel-1", "A direct answer.", "placement-1"),
        repr(sent[-1]),
    )

    accounted = []
    original_bounce_count = adapter._bounce_count_sent
    original_catchup_count = adapter._catchup_count_sent
    original_direct_count = adapter._direct_count_sent
    adapter._bounce_count_sent = lambda anchor, result: accounted.append(("bounce", anchor))
    adapter._catchup_count_sent = lambda anchor, result: accounted.append(("catchup", anchor))
    adapter._direct_count_sent = lambda anchor, result: accounted.append(("direct", anchor))
    try:
        await adapter.send(
            "channel-1",
            "[STANDALONE] The whole room needed to hear that.",
            reply_to="placement-2",
        )
    finally:
        adapter._bounce_count_sent = original_bounce_count
        adapter._catchup_count_sent = original_catchup_count
        adapter._direct_count_sent = original_direct_count
    check(
        "the standalone marker is stripped and the Discord reference is omitted",
        sent[-1]
        == ("channel-1", "The whole room needed to hear that.", None),
        repr(sent[-1]),
    )
    check(
        "standalone delivery still accounts against the original inbound anchor",
        accounted
        == [
            ("bounce", "placement-2"),
            ("catchup", "placement-2"),
            ("direct", "placement-2"),
        ],
        repr(accounted),
    )

    await adapter.send(
        "channel-1",
        "The whole room needed to hear that.\n\n[STANDALONE]",
        reply_to="placement-2b",
    )
    check(
        "a marker placed after the remark is stripped, not delivered",
        sent[-1] == ("channel-1", "The whole room needed to hear that.", None),
        repr(sent[-1]),
    )
    await adapter.send(
        "channel-1",
        "Moons have phases. **[standalone]**",
        reply_to="placement-2c",
    )
    check(
        "a dressed trailing marker is stripped in any case",
        sent[-1] == ("channel-1", "Moons have phases.", None),
        repr(sent[-1]),
    )

    adapter._direct_pending["placement-3"] = ambient.time.time()
    before = len(sent)
    await adapter.send("channel-1", "[STANDALONE]", reply_to="placement-3")
    check(
        "a marker-only direct turn closes silently, with no fallback",
        len(sent) == before
        and "placement-3" not in adapter._direct_pending,
        (repr(sent[before:]), repr(adapter._direct_pending)),
    )
    await adapter.send("channel-1", "[STANDALONE]", reply_to="placement-4")
    check(
        "a marker-only ambient turn stays silent",
        len(sent) == before,
        repr(sent[before:]),
    )

    print("\n-- only verified postures vouch for a discretionary origin --")
    plain = MessageEvent(
        text="room chatter",
        raw_message=RawMessage(None, message_id="300"),
        message_id="300",
        source=types.SimpleNamespace(chat_type="group"),
    )
    check(
        "an unknown posture vouches for nothing (gateway required default)",
        adapter.turn_origin_for_event(plain) is None,
    )
    adapter._note_origin_kind("300", "ambient")
    ambient_origin = adapter.turn_origin_for_event(plain)
    check(
        "a noted ambient join is vouchable as discretionary",
        getattr(ambient_origin, "kind", None) == "ambient"
        and getattr(ambient_origin, "response_policy", None) == "discretionary"
        and getattr(ambient_origin, "event_id", None) == "300",
        repr(ambient_origin),
    )
    adapter._origin_kinds["300"] = ("catch_up", ambient.time.time() - 1801)
    check(
        "a stale origin kind expires instead of granting silence",
        adapter.turn_origin_for_event(plain) is None,
        repr(adapter._origin_kinds),
    )

    print("\n-- known Discord handles become real mentions --")
    speaker = Author(123, "alice.account")
    speaker.display_name = "Alice"
    inbound = RawMessage(None, author=speaker)
    adapter._remember_discord_identities(inbound)
    await adapter.send("channel-1", "Thanks, @Alice!", reply_to="202")
    check(
        "a known display name is rendered as a Discord user mention",
        sent[-1][1] == "Thanks, <@123>!",
        repr(sent[-1]),
    )
    await adapter.send("channel-1", "Hello, @unknown!", reply_to="203")
    check(
        "an unknown handle is left as ordinary text",
        sent[-1][1] == "Hello, @unknown!",
        repr(sent[-1]),
    )
    older = Author(321, "garden.account")
    older.display_name = "GardenMuse"
    history_channel = HistoryChannel(
        "channel-1", [RawMessage(None, author=older)],
    )
    adapter._client = types.SimpleNamespace(
        user=Author(999, "Muse"),
        get_channel=lambda channel_id: history_channel,
    )
    await adapter.send(
        "channel-1", "The room saved a story for @GardenMuse!", reply_to="203b",
    )
    check(
        "an ambient name from recent channel history becomes a real mention",
        sent[-1][1] == "The room saved a story for <@321>!",
        repr(sent[-1]),
    )
    collision = Author(456, "second.account")
    collision.display_name = "Alice"
    adapter._remember_discord_identities(RawMessage(None, author=collision))
    await adapter.send("channel-1", "Thanks, @Alice!", reply_to="204")
    check(
        "a reused display name is left ambiguous instead of guessed",
        sent[-1][1] == "Thanks, @Alice!",
        repr(sent[-1]),
    )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        raise SystemExit(1)
    print("all checks passed")


asyncio.run(main())
