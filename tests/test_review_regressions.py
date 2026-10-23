"""Delivery accounting and dispatch-local recall across real adapter consumers."""
import asyncio
from contextlib import closing
import importlib.util
import os
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest import mock

from gateway.config import Platform
from gateway.platforms.base import SendResult
from plugins.platforms.discord.adapter import DiscordAdapter


_real_sleep = asyncio.sleep


async def _yielding_no_delay(_delay, result=None):
    return await _real_sleep(0, result=result)


class ReviewRegressions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        home = self.enterContext(tempfile.TemporaryDirectory(prefix="ambient-review-"))
        self.enterContext(mock.patch.dict(os.environ, {"HOME": home, "HERMES_HOME": home}))
        path = Path(__file__).resolve().parents[1] / "__init__.py"
        spec = importlib.util.spec_from_file_location("ambient_review_fixture", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        config = types.SimpleNamespace(extra={"ambient_presence": {
            "enabled": True, "channels": ["*"], "speaker_identity": True,
            "speaker_memory": {"enabled": True},
            "text_hygiene": {"isolate_media_urls": True},
        }})
        with mock.patch.object(DiscordAdapter, "__init__", lambda self, c: setattr(self, "config", c)):
            self.adapter = self.module.AmbientDiscordAdapter(config)
        self.adapter._client = types.SimpleNamespace(user=types.SimpleNamespace(id=999))
        self.adapter._refresh_plain_mention_history = mock.AsyncMock()
        self.adapter.platform = Platform.DISCORD
        self.adapter._raw_message_is_direct = lambda message: False
        self.message = types.SimpleNamespace(
            id=700, content="fixture name trigger", author=types.SimpleNamespace(id=101, name="alice"),
            channel=types.SimpleNamespace(id=501, guild=None),
        )

    def _stock_channel(self, failures):
        """Exercise the inherited Discord chunk sender; only the channel is fake."""
        self.adapter.config.extra["missed_message_backfill"] = {"enabled": False}
        self.adapter._reply_to_mode = "off"
        self.adapter._last_self_message_id = {}
        attempted = []
        delivered = []

        async def send(*, content, reference=None):
            attempted.append(content)
            if failures.get(content, 0):
                failures[content] -= 1
                raise ConnectionError("connection reset by fixture")
            message_id = str(900 + len(delivered))
            delivered.append((content, message_id))
            return types.SimpleNamespace(id=message_id)

        channel = types.SimpleNamespace(id=501, send=send)
        self.adapter._resolve_channel = mock.AsyncMock(return_value=channel)
        return attempted, delivered

    def _editable_channel(self):
        """Real Discord send/edit methods against a channel with persistent message state."""
        attempted, delivered = self._stock_channel({})
        channel = self.adapter._resolve_channel.return_value
        visible = {}
        original_send = channel.send

        async def send(**kwargs):
            msg = await original_send(**kwargs)
            visible[str(msg.id)] = kwargs["content"]
            messages[str(msg.id)] = Message(msg.id)
            return messages[str(msg.id)]

        class Message:
            def __init__(self, message_id):
                self.id = str(message_id)
                self.fail_edit = False

            async def edit(self, *, content):
                if self.fail_edit:
                    raise ConnectionError("edit refused")
                visible[self.id] = content

            async def delete(self):
                visible.pop(self.id)

        messages = {}
        channel.send = send
        channel.get_partial_message = lambda mid: messages.setdefault(str(mid), Message(mid))
        self.adapter._last_overflow_preview = {}
        return attempted, delivered, visible, messages

    def _chunked_prose(self):
        prose = "A" * 2000 + "\n" + "B" * 90
        chunks = self.adapter._cap_split_chunks(
            self.adapter.truncate_message(
                self.adapter.format_message(prose), self.adapter.MAX_MESSAGE_LENGTH))
        self.assertEqual(len(chunks), 2)
        return prose, chunks[0], chunks[1]

    def _pending_gif(self, url, anchor="incoming"):
        bucket = self.module._gif_bucket(self.adapter._bot_user_id() or None)
        bucket["pending"] = (url, time.time(), anchor)
        self.addCleanup(lambda: bucket.__setitem__("pending", None))
        return bucket

    def _ledger_event(self):
        from gateway.platforms.event import MessageEvent, MessageType
        from gateway.session import SessionSource
        self.adapter.typed_command_prefix = "!"
        self.adapter.gateway_runner = types.SimpleNamespace()
        return MessageEvent(
            text="fixture request", message_type=MessageType.TEXT,
            source=SessionSource(platform=Platform.DISCORD, chat_id="channel", chat_type="channel"),
            message_id="incoming",
        )

    async def _complete_queued_reply(self, text, session_key):
        """Pass the queued receipt through the real background finalizer and assistant row."""
        from gateway.config import PlatformConfig
        from gateway.platforms.base import BasePlatformAdapter
        from gateway.run import GatewayRunner
        from hermes_state import SessionDB

        BasePlatformAdapter.__init__(
            self.adapter,
            PlatformConfig(enabled=True, extra=self.adapter.config.extra),
            Platform.DISCORD,
        )

        db = SessionDB(db_path=Path(os.environ["HERMES_HOME"]) / f"{session_key}-turn.sqlite")
        try:
            sid = db.create_session(session_key, "test")
            db.append_message(sid, "user", "question")
            db.append_message(sid, "assistant", text)
            row_id = db.get_messages_as_conversation(sid, include_row_ids=True)[-1]["_row_id"]
            event = self._ledger_event()
            runner = object.__new__(GatewayRunner)
            runner._session_db = db
            runner._pop_post_delivery_callback = lambda *_args: None
            runner._delivery_adapter_for = lambda source: self.adapter
            runner._should_send_voice_reply = lambda *args, **kwargs: False
            self.adapter.gateway_runner = runner
            result = {"final_response": text, "turn_id": session_key,
                      "messages": [{"role": "assistant", "content": text, "_row_id": row_id}]}
            ctx = types.SimpleNamespace(
                mute_notification_reply=False, session_key=session_key,
                session_id=sid, stream_consumer_holder=[None], source=event.source,
                _status_thread_metadata={}, event_message_id="incoming",
                inbound_message_id="incoming", run_generation=1)
            await runner._run_agent_deliver_first_response(
                ctx, self.adapter, result, result, None)
            queued = db.get_messages_as_conversation(sid)[-1]["display_metadata"]["delivery"]

            async def handler(current):
                return await runner._hmwa_deliver_turn_response(
                    current, current.source, types.SimpleNamespace(session_id=sid),
                    session_key, 1, result, [], text, None, False)

            self.adapter._message_handler = handler
            self.adapter._active_sessions[session_key] = asyncio.Event()
            await self.adapter._process_message_background(event, session_key)
            message = db.get_messages_as_conversation(sid)[-1]
            self.assertEqual(message["content"], text)
            return queued, message["display_metadata"]["delivery"]
        finally:
            db.close()


    async def test_partial_carriage_charges_real_counters_once_and_records_the_landed_text(self):
        adapter = self.adapter
        now = time.time()
        adapter._bounce_pending["anchor"] = ("channel", "bot", now)
        adapter._bounce_pairs[("channel", "bot")] = {"count": 0, "limit": 8, "last": now}
        adapter._catchup_pending["anchor"] = now
        adapter._direct_pending["anchor"] = now
        before = len(adapter._ambient_hits)

        async def transport(_self, chat_id, content, *args, **kwargs):
            if content.startswith("https://"):
                return SendResult(success=False, error="fixture media refusal")
            return SendResult(success=True, message_id="landed")

        with mock.patch.object(DiscordAdapter, "send", transport):
            result = await adapter.send("channel", "Hello https://example.com/a.gif", reply_to="anchor")
        self.assertFalse(result.success)
        self.assertEqual(adapter._bounce_pairs[("channel", "bot")]["count"], 1)
        self.assertEqual(len(adapter._ambient_hits), before + 1)
        self.assertNotIn("anchor", adapter._bounce_pending)
        self.assertNotIn("anchor", adapter._catchup_pending)
        self.assertNotIn("anchor", adapter._direct_pending)
        adapter._bounce_count_sent("anchor", result)
        adapter._catchup_count_sent("anchor", result)
        self.assertEqual(adapter._bounce_pairs[("channel", "bot")]["count"], 1)
        self.assertEqual(len(adapter._ambient_hits), before + 1)
        self.assertTrue(adapter._is_partial_delivery(result))
        records = []
        adapter.gateway_runner = types.SimpleNamespace(
            record_turn_delivery=lambda address, outcome: records.append(outcome) or True)
        event = types.SimpleNamespace(turn_delivery_address={"session_id": "fixture", "row_id": 1})
        await adapter._finalize_turn_delivery_record(event, [result])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["disposition"], "text")
        self.assertEqual(records[0]["text"], "Hello")
        self.assertEqual(records[0]["message_ids"], ["landed"])
        self.assertTrue(records[0]["partial"])
        self.assertEqual(records[0]["error"], "fixture media refusal")

    async def test_wholly_failed_transport_uses_the_real_retry_path(self):
        attempts = []

        async def transport(_self, chat_id, content, *args, **kwargs):
            attempts.append(content)
            if len(attempts) <= 2:
                return SendResult(success=False, error="send_path_degraded",
                                  retryable=True, error_kind="transient")
            return SendResult(success=True, message_id=f"carriage-{len(attempts)}")

        self.adapter._send_plain_fallback = mock.AsyncMock(
            side_effect=AssertionError("transient failure must not use plain fallback"))
        with mock.patch.object(DiscordAdapter, "send", transport), \
                mock.patch("gateway.platforms.base.asyncio.sleep", new=_yielding_no_delay):
            result = await self.adapter._send_with_retry(
                "channel", "Hello https://example.com/a.gif", max_retries=1, base_delay=0)
        self.assertTrue(result.success)
        self.assertEqual(attempts, ["Hello", "https://example.com/a.gif"] * 2)
        self.adapter._send_plain_fallback.assert_not_awaited()

    async def test_pending_gif_total_failure_receipt_retains_whole_owed_payload(self):
        gif = "https://example.com/pending.gif"
        self._pending_gif(gif)
        attempted, delivered = self._stock_channel({"Hello": 1, gif: 1})
        result = await self.adapter.send("channel", "Hello", reply_to="incoming")
        self.assertFalse(result.success)
        self.assertEqual(attempted, ["Hello", gif])
        self.assertEqual(delivered, [])
        self.assertFalse(result.raw_response["partial_overflow"])
        self.assertEqual(result.raw_response["remaining_content"], f"Hello\n{gif}")
        self.assertIsNone(result.message_id)
        self.assertFalse(result.delivered_text)

    async def test_pending_gif_nonisolated_total_failure_survives_durable_replay(self):
        from gateway import delivery_ledger as ledger
        gif = "https://example.com/pending.gif"
        self.adapter.config.extra["ambient_presence"]["text_hygiene"]["isolate_media_urls"] = False
        bucket = self._pending_gif(gif)
        combined = f"Hello\n{gif}"
        attempted, delivered = self._stock_channel({combined: 3})
        event = self._ledger_event()
        database = Path(os.environ["HERMES_HOME"]) / "combined-gif-replay.sqlite"
        with mock.patch.object(ledger, "_db_path", return_value=database), \
                mock.patch.object(ledger, "ledger_enabled", return_value=True), \
                mock.patch("gateway.platforms.base.asyncio.sleep", new=_yielding_no_delay):
            result, owner = await self.adapter.send_final_ledgered(
                event, "combined-gif-session", "Hello", {}, reply_to="incoming")
            self.assertFalse(result.success)
            self.assertIsNone(bucket["pending"])
            self.assertFalse(self.adapter._is_partial_delivery(result))
            self.assertEqual(result.raw_response["remaining_content"], combined)
            self.assertIsNone(result.message_id)
            self.assertFalse(result.delivered_text)
            rows = ledger.sweep_failed_for_runtime(
                "discord", now=time.time() + 3600, profile="default")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["content"], combined)
            replay = await owner._send_with_retry(
                "channel", rows[0]["content"], reply_to="incoming")
            self.assertTrue(replay.success)
            await self.adapter._finalize_delivery_obligation(
                rows[0]["obligation_id"], replay, event, owner)
            with closing(ledger._connect()) as conn:
                state = conn.execute(
                    "SELECT state FROM delivery_obligations WHERE obligation_id=?",
                    (rows[0]["obligation_id"],),
                ).fetchone()[0]
            self.assertEqual(state, "delivered")
        self.assertEqual(attempted.count(combined), 4)
        self.assertEqual([text for text, _ in delivered if text == combined], [combined])

    async def test_pending_gif_wholly_failed_transport_retries_attached_url(self):
        gif = "https://example.com/pending.gif"
        bucket = self._pending_gif(gif)
        failures = {"Hello": 1, gif: 1}
        attempted, delivered = self._stock_channel(failures)
        self.adapter._send_plain_fallback = mock.AsyncMock(
            side_effect=AssertionError("transient failure must not use plain fallback"))
        with mock.patch("gateway.platforms.base.asyncio.sleep", new=_yielding_no_delay):
            result = await self.adapter._send_with_retry(
                "channel", "Hello", reply_to="incoming", max_retries=1, base_delay=0)
        self.assertTrue(result.success)
        self.assertEqual(attempted, ["Hello", gif, "Hello", gif])
        self.assertEqual([text for text, _ in delivered], ["Hello", gif])
        self.assertIsNone(bucket["pending"])
        await self.adapter.send("channel", "Later", reply_to="incoming")
        self.assertEqual(attempted[-1], "Later")
        self.assertEqual(attempted.count(gif), 2)
        self.adapter._send_plain_fallback.assert_not_awaited()

    async def test_pending_gif_success_is_not_reattached_when_prose_replays(self):
        from gateway import delivery_ledger as ledger
        gif = "https://example.com/pending.gif"
        self._pending_gif(gif)
        attempted, delivered = self._stock_channel({"Hello": 1})
        event = self._ledger_event()
        database = Path(os.environ["HERMES_HOME"]) / "gif-head-replay.sqlite"
        with mock.patch.object(ledger, "_db_path", return_value=database), \
                mock.patch.object(ledger, "ledger_enabled", return_value=True), \
                mock.patch("gateway.platforms.base.asyncio.sleep", new=_yielding_no_delay):
            result, owner = await self.adapter.send_final_ledgered(
                event, "gif-head-session", "Hello", {}, reply_to="incoming")
            self.assertFalse(result.success)
            self.assertEqual(result.delivered_text, gif)
            self.assertEqual(result.raw_response["remaining_content"], "Hello")
            rows = ledger.sweep_failed_for_runtime(
                "discord", now=time.time() + 3600, profile="default")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["content"], "Hello")
            replay = await owner._send_with_retry(
                "channel", rows[0]["content"], reply_to="incoming")
            self.assertTrue(replay.success)
            await self.adapter._finalize_delivery_obligation(
                rows[0]["obligation_id"], replay, event, owner)
            with closing(ledger._connect()) as conn:
                state = conn.execute(
                    "SELECT state FROM delivery_obligations WHERE obligation_id=?",
                    (rows[0]["obligation_id"],),
                ).fetchone()[0]
            self.assertEqual(state, "delivered")
        self.assertEqual(attempted, ["Hello", gif, "Hello"])
        self.assertEqual([text for text, _ in delivered], [gif, "Hello"])

    async def test_actual_stream_total_failure_recovers_prose_and_pending_gif(self):
        from gateway import delivery_ledger as ledger
        from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig

        gif = "https://example.com/pending.gif"
        self._pending_gif(gif)
        attempted, delivered = self._stock_channel({"Hello": 1, gif: 1})
        consumer = GatewayStreamConsumer(
            self.adapter, "channel", StreamConsumerConfig(buffer_only=True),
            initial_reply_to_id="incoming")
        consumer._message_id = "__no_edit__"
        consumer._edit_supported = False
        consumer._fallback_final_send = True
        consumer.on_delta("Hello")
        consumer.finish("Hello")
        await consumer.run()
        handoff = consumer.delivery_handoff_for_final("Hello")
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff.result.raw_response["remaining_content"], f"Hello\n{gif}")
        self.assertFalse(handoff.result.delivered_text)

        event = self._ledger_event()
        event._stream_delivery_handoff = handoff
        database = Path(os.environ["HERMES_HOME"]) / "stream-total-replay.sqlite"
        with mock.patch.object(ledger, "_db_path", return_value=database):
            result, _ = await self.adapter.send_final_ledgered(
                event, "stream-total-session", "Hello", {}, reply_to="incoming")
            with closing(ledger._connect()) as conn:
                row = conn.execute(
                    "SELECT content, original_content, state FROM delivery_obligations").fetchone()
        self.assertEqual(tuple(row), (f"Hello\n{gif}", "Hello", "delivered"))
        self.assertEqual(attempted, ["Hello", gif, "Hello", gif])
        self.assertEqual([text for text, _ in delivered], ["Hello", gif])
        self.assertEqual(result.delivered_text, f"Hello\n{gif}")

    async def test_actual_stream_handoff_recovers_pending_gif_without_resending_prose(self):
        from gateway import delivery_ledger as ledger
        from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig

        gif = "https://example.com/pending.gif"
        self._pending_gif(gif)
        attempted, delivered = self._stock_channel({gif: 1})
        consumer = GatewayStreamConsumer(
            self.adapter, "channel", StreamConsumerConfig(buffer_only=True),
            initial_reply_to_id="incoming")
        consumer._message_id = "__no_edit__"
        consumer._edit_supported = False
        consumer._fallback_final_send = True
        consumer.on_delta("Hello")
        consumer.finish("Hello")
        await consumer.run()
        handoff = consumer.delivery_handoff_for_final("Hello")
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff.result.raw_response["remaining_content"], gif)
        self.assertEqual(handoff.result.delivered_text, "Hello")
        self.assertFalse(consumer.final_content_delivered)

        event = self._ledger_event()
        event._stream_delivery_handoff = handoff
        database = Path(os.environ["HERMES_HOME"]) / "stream-gif-replay.sqlite"
        with mock.patch.object(ledger, "_db_path", return_value=database):
            result, _ = await self.adapter.send_final_ledgered(
                event, "stream-gif-session", "Hello", {}, reply_to="incoming")
            with closing(ledger._connect()) as conn:
                row = conn.execute(
                    "SELECT content, original_content, state FROM delivery_obligations").fetchone()
        self.assertEqual(tuple(row), (gif, "Hello", "delivered"))
        self.assertEqual(attempted, ["Hello", gif, gif])
        self.assertEqual([text for text, _ in delivered], ["Hello", gif])
        outcome = self.adapter._turn_delivery_outcome(
            [result], hint="text", streamed_text=None)
        self.assertEqual(outcome["disposition"], "text")
        self.assertEqual(outcome["text"], f"Hello\n{gif}")
        self.assertEqual(outcome["message_ids"], ["900", "901"])


    async def test_stock_discord_second_chunk_failure_reports_ack_and_exact_remainder(self):
        prose, first, second = self._chunked_prose()
        attempted, delivered = self._stock_channel({second: 1})
        receipt = await DiscordAdapter.send(self.adapter, "channel", prose, reply_to="incoming")
        self.assertFalse(receipt.success)
        self.assertEqual(attempted, [first, second])
        self.assertEqual(delivered, [(first, "900")])
        self.assertEqual(receipt.message_id, "900")
        self.assertEqual(receipt.raw_response["message_ids"], ["900"])
        self.assertEqual(receipt.delivered_text, first)
        self.assertTrue(receipt.raw_response["partial_overflow"])
        self.assertEqual(receipt.raw_response["remaining_content"], second)
        outcome = self.adapter._turn_delivery_outcome(
            [receipt], hint=None, streamed_text=None)
        self.assertEqual(outcome["disposition"], "text")
        self.assertEqual(outcome["text"], first)
        self.assertEqual(outcome["message_ids"], ["900"])
        self.assertTrue(outcome["partial"])
        self.assertEqual(outcome["error"], "send_path_degraded")


    async def test_stock_discord_success_projects_all_acknowledged_chunk_ids(self):
        prose, first, second = self._chunked_prose()
        attempted, delivered = self._stock_channel({})
        receipt = await DiscordAdapter.send(self.adapter, "channel", prose, reply_to="incoming")
        self.assertTrue(receipt.success)
        self.assertEqual(attempted, [first, second])
        self.assertEqual(delivered, [(first, "900"), (second, "901")])
        outcome = self.adapter._turn_delivery_outcome(
            [receipt], hint=None, streamed_text=None)
        self.assertEqual(outcome["disposition"], "text")
        self.assertEqual(outcome["message_ids"], ["900", "901"])
        self.assertEqual(outcome["text"], f"{first}\n{second}")

    async def test_stock_third_chunk_failure_keeps_both_acknowledged_ids(self):
        prose = "A" * 2000 + "\n" + "B" * 2000 + "\n" + "C" * 90
        chunks = self.adapter._cap_split_chunks(
            self.adapter.truncate_message(
                self.adapter.format_message(prose), self.adapter.MAX_MESSAGE_LENGTH))
        self.assertEqual(len(chunks), 3)
        attempted, delivered = self._stock_channel({chunks[2]: 1})
        receipt = await DiscordAdapter.send(self.adapter, "channel", prose, reply_to="incoming")
        self.assertFalse(receipt.success)
        self.assertEqual(attempted, chunks)
        self.assertEqual(delivered, [(chunks[0], "900"), (chunks[1], "901")])
        self.assertEqual(receipt.raw_response["message_ids"], ["900", "901"])
        self.assertEqual(receipt.raw_response["remaining_content"], chunks[2])
        outcome = self.adapter._turn_delivery_outcome(
            [receipt], hint=None, streamed_text=None)
        self.assertEqual(outcome["message_ids"], ["900", "901"])
        self.assertEqual(outcome["text"], f"{chunks[0]}\n{chunks[1]}")
        self.assertTrue(outcome["partial"])


    async def test_composite_prose_edit_preserves_gif_and_replaces_old_prose_on_assistant_row(self):
        from gateway.run_turn import GatewayTurnMixin
        from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
        from hermes_state import SessionDB

        gif = "https://example.com/still-visible.gif"
        attempted, _, visible, _ = self._editable_channel()
        consumer = GatewayStreamConsumer(
            self.adapter, "channel", StreamConsumerConfig(cursor=""))
        self.assertTrue(await consumer._first_send(f"Old prose {gif}", finalize=False))
        self.assertEqual(list(visible), ["900", "901"])
        self.assertTrue(await consumer._edit_existing(
            "Replacement prose", finalize=True, is_turn_final=True))
        owner = consumer.delivery_handoff_for_final("Replacement prose")

        db = SessionDB(db_path=Path(os.environ["HERMES_HOME"]) / "composite-row.sqlite")
        try:
            sid = db.create_session("composite", "test")
            db.append_message(sid, "user", "question")
            db.append_message(sid, "assistant", "Replacement prose")
            row_id = db.get_messages_as_conversation(sid, include_row_ids=True)[-1]["_row_id"]
            event = self._ledger_event()
            event.turn_delivery_address = {
                "session_id": sid, "row_id": row_id, "turn_id": "composite",
                "disposition_hint": "text"}
            runner = GatewayTurnMixin()
            runner._session_db = db
            self.adapter.gateway_runner = runner
            await self.adapter._finalize_turn_delivery_record(event, [owner.result])
            delivery = db.get_messages_as_conversation(sid)[-1]["display_metadata"]["delivery"]
            self.assertEqual(delivery["message_ids"], list(visible))
            self.assertEqual(delivery["text"], "\n".join(visible.values()))
            self.assertNotIn("Old prose", delivery["text"])
            self.assertEqual(attempted, ["Old prose", gif])
        finally:
            db.close()

    async def test_screened_ordinary_final_records_no_carriage_on_assistant_row(self):
        from gateway.config import PlatformConfig
        from gateway.run_turn import GatewayTurnMixin
        from hermes_state import SessionDB

        from gateway.platforms.base import BasePlatformAdapter
        BasePlatformAdapter.__init__(
            self.adapter, PlatformConfig(enabled=True, extra=self.adapter.config.extra),
            Platform.DISCORD)
        attempted, _ = self._stock_channel({})
        text = "[ambient private context]"
        db = SessionDB(db_path=Path(os.environ["HERMES_HOME"]) / "ordinary-screen.sqlite")
        try:
            sid = db.create_session("ordinary-screen", "test")
            db.append_message(sid, "user", "question")
            db.append_message(sid, "assistant", text)
            row_id = db.get_messages_as_conversation(sid, include_row_ids=True)[-1]["_row_id"]
            event = self._ledger_event()
            event.turn_delivery_address = {
                "session_id": sid, "row_id": row_id, "turn_id": "ordinary-screen",
                "disposition_hint": "text"}
            runner = GatewayTurnMixin()
            runner._session_db = db
            self.adapter.gateway_runner = runner
            self.adapter._message_handler = mock.AsyncMock(return_value=text)
            session_key = "agent:main:discord:channel:channel"
            self.adapter._active_sessions[session_key] = asyncio.Event()
            await self.adapter._process_message_background(event, session_key)
            delivery = db.get_messages_as_conversation(sid)[-1]["display_metadata"]["delivery"]
            self.assertEqual(attempted, [])
            self.assertEqual(delivery["disposition"], "unknown")
            self.assertFalse(delivery.get("text"))
            self.assertEqual(delivery["message_ids"], [])
            self.assertIn("suppressed", delivery["reason"])
        finally:
            db.close()

    async def test_screened_interim_edit_leaves_last_visible_ack_after_later_edit_failure(self):
        from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig

        attempted, _, visible, messages = self._editable_channel()
        consumer = GatewayStreamConsumer(
            self.adapter, "channel", StreamConsumerConfig(cursor=""))
        self.assertTrue(await consumer._first_send("Still visible", finalize=False))
        self.assertTrue(await consumer._edit_existing(
            "[ambient private context]", finalize=False, is_turn_final=False))
        self.assertEqual(visible, {"900": "Still visible"})
        messages["900"].fail_edit = True
        self.assertFalse(await consumer._edit_existing(
            "Later replacement", finalize=True, is_turn_final=True))
        owner = consumer.delivery_handoff_for_final("Later replacement")
        outcome = self.adapter._turn_delivery_outcome(
            [owner.result], hint="text", streamed_text=None)
        self.assertEqual(outcome["message_ids"], list(visible))
        self.assertEqual(outcome["text"], "\n".join(visible.values()))
        self.assertTrue(outcome["partial"])
        self.assertEqual(attempted, ["Still visible"])

    async def test_screened_final_deletes_only_preview_and_keeps_independent_gif_on_row(self):
        from gateway.run_turn import GatewayTurnMixin
        from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
        from hermes_state import AsyncSessionDB, SessionDB

        gif = "https://example.com/still-visible.gif"
        attempted, _, visible, _ = self._editable_channel()
        consumer = GatewayStreamConsumer(
            self.adapter, "channel", StreamConsumerConfig(cursor=""))
        self.assertTrue(await consumer._first_send(f"Old prose {gif}", finalize=False))
        self.assertEqual(visible, {"900": "Old prose", "901": gif})
        observed_edits = []
        original_edit = self.adapter.edit_message

        async def capture_edit(*args, **kwargs):
            receipt = await original_edit(*args, **kwargs)
            observed_edits.append(receipt)
            return receipt

        self.adapter.edit_message = capture_edit
        self.assertTrue(await consumer._edit_existing(
            "[ambient private context]", finalize=True, is_turn_final=True))
        self.assertEqual(visible, {"901": gif})
        self.assertEqual(
            observed_edits[-1].raw_response["deleted_message_ids"], ("900",))
        owner = consumer.delivery_handoff_for_final("[ambient private context]")
        db = SessionDB(db_path=Path(os.environ["HERMES_HOME"]) / "screened-delete.sqlite")
        try:
            sid = db.create_session("screened-delete", "test")
            db.append_message(sid, "user", "question")
            db.append_message(sid, "assistant", "[ambient private context]")
            row_id = db.get_messages_as_conversation(sid, include_row_ids=True)[-1]["_row_id"]
            event = self._ledger_event()
            event.turn_delivery_address = {
                "session_id": sid, "row_id": row_id, "turn_id": "screened-delete",
                "disposition_hint": "text"}
            runner = GatewayTurnMixin()
            runner._session_db = AsyncSessionDB(db)
            self.adapter.gateway_runner = runner
            await self.adapter._finalize_turn_delivery_record(event, [owner.result])
            delivery = db.get_messages_as_conversation(sid)[-1]["display_metadata"]["delivery"]
            self.assertEqual(delivery["message_ids"], ["901"])
            self.assertEqual(delivery["text"], gif)
            self.assertNotIn("Old prose", delivery["text"])
            self.assertEqual(attempted, ["Old prose", gif])
        finally:
            db.close()

    async def test_stock_chunk_replay_never_resends_the_visible_head_when_media_fails(self):
        from gateway import delivery_ledger as ledger
        gif = "https://example.com/a.gif"
        prose, first, second = self._chunked_prose()
        attempted, delivered = self._stock_channel({second: 1, gif: 2})
        event = self._ledger_event()
        database = Path(os.environ["HERMES_HOME"]) / "chunk-media-replay.sqlite"
        with mock.patch.object(ledger, "_db_path", return_value=database), \
                mock.patch.object(ledger, "ledger_enabled", return_value=True), \
                mock.patch("gateway.platforms.base.asyncio.sleep", new=_yielding_no_delay):
            result, owner = await self.adapter.send_final_ledgered(
                event, "chunk-media-session", f"{prose} {gif}", {}, reply_to="incoming")
            self.assertFalse(result.success)
            self.assertEqual(result.raw_response["remaining_content"], f"{second}\n{gif}")
            rows = ledger.sweep_failed_for_runtime(
                "discord", now=time.time() + 3600, profile="default")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["content"], f"{second}\n{gif}")
            replay = await owner._send_with_retry(
                "channel", rows[0]["content"], reply_to="incoming")
            self.assertFalse(replay.success)
            self.assertEqual(replay.raw_response["remaining_content"], gif)
            await self.adapter._finalize_delivery_obligation(
                rows[0]["obligation_id"], replay, event, owner)
            rows = ledger.sweep_failed_for_runtime(
                "discord", now=time.time() + 7200, profile="default")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["content"], gif)
            replay = await owner._send_with_retry(
                "channel", rows[0]["content"], reply_to="incoming")
            self.assertTrue(replay.success)
            await self.adapter._finalize_delivery_obligation(
                rows[0]["obligation_id"], replay, event, owner)
            with closing(ledger._connect()) as conn:
                state = conn.execute(
                    "SELECT state FROM delivery_obligations WHERE obligation_id=?",
                    (rows[0]["obligation_id"],),
                ).fetchone()[0]
            self.assertEqual(state, "delivered")
        self.assertEqual(attempted, [first, second, gif, second, gif, gif])
        self.assertEqual([text for text, _ in delivered], [first, second, gif])
        self.assertEqual(attempted.count(first), 1)

    async def test_pending_gif_exhausted_retries_survives_actual_durable_replay(self):
        from gateway import delivery_ledger as ledger
        gif = "https://example.com/pending.gif"
        bucket = self._pending_gif(gif)
        failures = {"Hello": 3, gif: 3}
        attempted, delivered = self._stock_channel(failures)
        event = self._ledger_event()
        database = Path(os.environ["HERMES_HOME"]) / "delivery.sqlite"
        with mock.patch.object(ledger, "_db_path", return_value=database), \
                mock.patch.object(ledger, "ledger_enabled", return_value=True), \
                mock.patch("gateway.platforms.base.asyncio.sleep", new=_yielding_no_delay):
            result, owner = await self.adapter.send_final_ledgered(
                event, "fixture-session", "Hello", {}, reply_to="incoming")
            self.assertFalse(result.success)
            self.assertIsNone(bucket["pending"])
            rows = ledger.sweep_failed_for_runtime(
                "discord", now=time.time() + 3600, profile="default")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["content"], f"Hello\n{gif}")
            failures.clear()
            replay = await owner._send_with_retry(
                "channel", rows[0]["content"], reply_to="incoming")
            self.assertTrue(replay.success)
            await self.adapter._finalize_delivery_obligation(
                rows[0]["obligation_id"], replay, event, owner)
            with closing(ledger._connect()) as conn:
                state = conn.execute(
                    "SELECT state FROM delivery_obligations WHERE obligation_id=?",
                    (rows[0]["obligation_id"],),
                ).fetchone()[0]
            self.assertEqual(state, "delivered")
        self.assertEqual([text for text, _ in delivered if text in ("Hello", gif)], ["Hello", gif])
        self.assertEqual(attempted.count(gif), 4)

    async def test_stock_chunk_partial_durable_replay_preserves_media_outcomes(self):
        from gateway import delivery_ledger as ledger
        gif = "https://example.com/a.gif"
        for media_fails in (False, True):
            with self.subTest(media_fails=media_fails):
                prose, first, second = self._chunked_prose()
                failures = {second: 3}
                if media_fails:
                    failures[gif] = 3
                attempted, delivered = self._stock_channel(failures)
                event = self._ledger_event()
                database = Path(os.environ["HERMES_HOME"]) / f"delivery-{media_fails}.sqlite"
                with mock.patch.object(ledger, "_db_path", return_value=database), \
                        mock.patch.object(ledger, "ledger_enabled", return_value=True), \
                        mock.patch("gateway.platforms.base.asyncio.sleep", new=_yielding_no_delay):
                    result, owner = await self.adapter.send_final_ledgered(
                        event, f"fixture-{media_fails}-session",
                        f"{prose} {gif}", {}, reply_to="incoming")
                    self.assertFalse(result.success)
                    self.assertEqual(result.message_id, "900")
                    self.assertEqual(result.delivered_text,
                                     first if media_fails else f"{first}\n{gif}")
                    self.assertTrue(result.raw_response["partial_overflow"])
                    self.assertEqual(result.raw_response["remaining_content"],
                                     f"{second}\n{gif}" if media_fails else second)
                    outcome = self.adapter._turn_delivery_outcome(
                        [result], hint=None, streamed_text=None)
                    self.assertEqual(outcome["disposition"], "text")
                    self.assertEqual(outcome["text"], result.delivered_text)
                    self.assertEqual(outcome["message_ids"],
                                     ["900"] if media_fails else ["900", "901"])
                    self.assertTrue(outcome["partial"])
                    rows = ledger.sweep_failed_for_runtime(
                        "discord", now=time.time() + 3600, profile="default")
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["content"], result.raw_response["remaining_content"])
                    failures.clear()
                    replay = await owner._send_with_retry(
                        "channel", rows[0]["content"], reply_to="incoming")
                    self.assertTrue(replay.success)
                    await self.adapter._finalize_delivery_obligation(
                        rows[0]["obligation_id"], replay, event, owner)
                    with closing(ledger._connect()) as conn:
                        state = conn.execute(
                            "SELECT state FROM delivery_obligations WHERE obligation_id=?",
                            (rows[0]["obligation_id"],),
                        ).fetchone()[0]
                    self.assertEqual(state, "delivered")
                self.assertEqual(attempted.count(first), 1)
                self.assertEqual([text for text, _ in delivered if text == first], [first])
                self.assertEqual([text for text, _ in delivered if text == second], [second])
                self.assertEqual([text for text, _ in delivered if text == gif], [gif])

    async def test_durable_partial_reply_retries_only_the_uncarried_media(self):
        from gateway import delivery_ledger as ledger
        from gateway.platforms.event import MessageEvent, MessageType
        from gateway.session import SessionSource
        sent = []
        media_attempts = []

        async def transport(_self, chat_id, content, *args, **kwargs):
            sent.append(content)
            if content.startswith("https://"):
                media_attempts.append(content)
                if len(media_attempts) == 1:
                    return SendResult(success=False, error="fixture media refusal")
            return SendResult(success=True, message_id=f"landed-{len(sent)}")

        self.adapter.typed_command_prefix = "!"
        self.adapter.gateway_runner = types.SimpleNamespace()
        event = MessageEvent(
            text="fixture request", message_type=MessageType.TEXT,
            source=SessionSource(platform=Platform.DISCORD, chat_id="channel", chat_type="channel"),
            message_id="incoming",
        )
        database = Path(os.environ["HERMES_HOME"]) / "delivery.sqlite"
        with mock.patch.object(ledger, "_db_path", return_value=database), \
                mock.patch.object(ledger, "ledger_enabled", return_value=True), \
                mock.patch.object(DiscordAdapter, "send", transport):
            result, owner = await self.adapter.send_final_ledgered(
                event, "fixture-session", "Hello https://example.com/a.gif", {}, reply_to="incoming")
            self.assertFalse(result.success)
            rows = ledger.sweep_failed_for_runtime(
                "discord", now=time.time() + 3600, profile="default")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["content"], "https://example.com/a.gif")
            replay = await owner._send_with_retry(
                "channel", rows[0]["content"], reply_to="incoming")
            self.assertTrue(replay.success)
            await self.adapter._finalize_delivery_obligation(
                rows[0]["obligation_id"], replay, event, owner)
            with closing(ledger._connect()) as conn:
                state = conn.execute(
                    "SELECT state FROM delivery_obligations WHERE obligation_id=?",
                    (rows[0]["obligation_id"],),
                ).fetchone()[0]
            self.assertEqual(state, "delivered")
        self.assertEqual(sent, ["Hello", "https://example.com/a.gif", "https://example.com/a.gif"])

    async def test_queued_ambient_carriages_survive_early_followup_return(self):
        from gateway import delivery_ledger as ledger
        from gateway.run import GatewayRunner
        from hermes_state import SessionDB

        gif = "https://example.com/queued.gif"
        for failed in (False, True):
            with self.subTest(failed=failed):
                self._pending_gif(gif)
                carried = []

                async def transport(_self, chat_id, content, *args, **kwargs):
                    if content == gif and failed:
                        return SendResult(success=False, error="media rejected", retry_after=120)
                    carried.append(content)
                    return SendResult(success=True, message_id=f"ack-{len(carried)}",
                                      delivered_text=content)

                database = Path(os.environ["HERMES_HOME"]) / f"queued-{failed}.sqlite"
                db = SessionDB(db_path=Path(os.environ["HERMES_HOME"]) / f"turn-{failed}.sqlite")
                try:
                    sid = db.create_session(f"queued-{failed}", "test")
                    db.append_message(sid, "user", "question")
                    db.append_message(sid, "assistant", "Hello")
                    row_id = db.get_messages_as_conversation(
                        sid, include_row_ids=True)[-1]["_row_id"]
                    event = self._ledger_event()
                    runner = object.__new__(GatewayRunner)
                    runner._session_db = db
                    runner._pop_post_delivery_callback = lambda *_args: None
                    runner._delivery_adapter_for = lambda source: self.adapter
                    runner._should_send_voice_reply = lambda *args, **kwargs: False
                    runner._MAX_INTERRUPT_DEPTH = 8
                    runner._is_goal_continuation_event = lambda *_args: False
                    runner._session_key_for_source = lambda *_args: "queued-ambient"
                    runner._prepare_profile_scoped_inbound_message_text = mock.AsyncMock(
                        return_value=None)
                    self.adapter.gateway_runner = runner
                    result = {"final_response": "Hello", "turn_id": f"queued-{failed}",
                              "messages": [{"role": "assistant", "content": "Hello",
                                            "_row_id": row_id}]}
                    ctx = types.SimpleNamespace(
                        mute_notification_reply=False, session_key="queued-ambient",
                        session_id=sid, stream_consumer_holder=[None], source=event.source,
                        _status_thread_metadata={}, event_message_id="incoming",
                        inbound_message_id="incoming", run_generation=1,
                        _interrupt_depth=0, history=[], persist_user_display_kind=None,
                        reply_expected=None)
                    pending_event = types.SimpleNamespace(
                        source=event.source, message_id="next", reply_expected=None,
                        message_type=None, internal=False, metadata={})
                    with mock.patch.object(ledger, "_db_path", return_value=database), \
                            mock.patch.object(ledger, "ledger_enabled", return_value=True), \
                            mock.patch.object(DiscordAdapter, "send", transport):
                        returned = await runner._run_agent_queued_followup(
                            ctx, self.adapter, "next", pending_event, result, result, None)
                    self.assertIs(returned, result)
                    self.assertEqual(carried, ["Hello"] if failed else ["Hello", gif])
                    delivery = db.get_messages_as_conversation(sid)[-1]["display_metadata"]["delivery"]
                    self.assertEqual(delivery["text"], "Hello" if failed else f"Hello\n{gif}")
                    self.assertEqual(delivery["message_ids"], ["ack-1"] if failed else ["ack-1", "ack-2"])
                    if failed:
                        self.assertEqual(delivery["error"], "media rejected")
                        self.assertTrue(delivery["partial"])
                finally:
                    db.close()

    async def test_first_carriage_timeout_keeps_failed_original_without_inline_replay(self):
        from gateway import delivery_ledger as ledger

        attempted = []

        async def send(*, content, reference=None):
            attempted.append(content)
            raise TimeoutError("accepted before acknowledgment")

        self.adapter.config.extra["missed_message_backfill"] = {"enabled": False}
        self.adapter._reply_to_mode = "off"
        self.adapter._last_self_message_id = {}
        self.adapter._resolve_channel = mock.AsyncMock(
            return_value=types.SimpleNamespace(id=501, send=send))
        session_key = "first-timeout"
        database = Path(os.environ["HERMES_HOME"]) / "first-timeout.sqlite"
        with mock.patch.object(ledger, "_db_path", return_value=database), \
                mock.patch.object(ledger, "ledger_enabled", return_value=True), \
                mock.patch("gateway.platforms.base.asyncio.sleep", new=_yielding_no_delay):
            queued, delivery = await self._complete_queued_reply("Hello", session_key)
            with closing(ledger._connect()) as conn:
                rows = conn.execute(
                    "SELECT obligation_id,content,original_content,state FROM delivery_obligations"
                ).fetchall()
        self.assertEqual(attempted, ["Hello"])
        self.assertEqual(queued["disposition"], "failure")
        self.assertEqual(delivery["disposition"], "failure")
        self.assertTrue(delivery.get("error"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], ledger.compute_obligation_id(session_key, "incoming", "Hello"))
        self.assertEqual(rows[0][2:], ("Hello", "failed"))
        self.assertIn("Hello", rows[0][1])


    async def test_stock_timeout_after_acceptance_keeps_uncertain_chunk_out_of_immediate_replay(self):
        from gateway import delivery_ledger as ledger

        prose = "A" * 2000 + "\n" + "B" * 2000 + "\n" + "C" * 90
        chunks = self.adapter._cap_split_chunks(
            self.adapter.truncate_message(
                self.adapter.format_message(prose), self.adapter.MAX_MESSAGE_LENGTH))
        self.assertEqual(len(chunks), 3)
        self.adapter.config.extra["missed_message_backfill"] = {"enabled": False}
        self.adapter._reply_to_mode = "off"
        self.adapter._last_self_message_id = {}
        attempted, accepted = [], []

        async def send(*, content, reference=None):
            attempted.append(content)
            accepted.append(content)
            if content == chunks[1]:
                raise TimeoutError("read timed out after transport acceptance")
            return types.SimpleNamespace(id=str(900 + len(accepted)))

        self.adapter._resolve_channel = mock.AsyncMock(
            return_value=types.SimpleNamespace(id=501, send=send))
        session_key = "stock-timeout"
        database = Path(os.environ["HERMES_HOME"]) / "stock-timeout.sqlite"
        with mock.patch.object(ledger, "_db_path", return_value=database), \
                mock.patch.object(ledger, "ledger_enabled", return_value=True), \
                mock.patch("gateway.platforms.base.asyncio.sleep", new=_yielding_no_delay):
            queued, delivery = await self._complete_queued_reply(prose, session_key)
            with closing(ledger._connect()) as conn:
                rows = conn.execute(
                    "SELECT obligation_id,content,original_content,state FROM delivery_obligations"
                ).fetchall()
        self.assertEqual(attempted, chunks[:2])
        self.assertEqual(accepted, chunks[:2])
        self.assertEqual(queued["text"], chunks[0])
        self.assertEqual(queued["message_ids"], ["901"])
        self.assertTrue(queued.get("error"))
        self.assertEqual(delivery["text"], chunks[0])
        self.assertEqual(delivery["message_ids"], ["901"])
        self.assertTrue(delivery.get("error"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], ledger.compute_obligation_id(session_key, "incoming", prose))
        self.assertEqual(rows[0][2:], (prose, "failed"))
        self.assertIn(chunks[1], rows[0][1])
        self.assertIn(chunks[2], rows[0][1])

    async def test_ambient_gif_timeout_after_acceptance_is_not_a_known_unsent_gif(self):
        from gateway import delivery_ledger as ledger

        gif = "https://example.com/uncertain.gif"
        self._pending_gif(gif)
        attempted, accepted = [], []

        async def send(*, content, reference=None):
            attempted.append(content)
            accepted.append(content)
            if content == gif:
                raise TimeoutError("read timed out after transport acceptance")
            return types.SimpleNamespace(id=str(900 + len(accepted)))

        self.adapter.config.extra["missed_message_backfill"] = {"enabled": False}
        self.adapter._reply_to_mode = "off"
        self.adapter._last_self_message_id = {}
        self.adapter._resolve_channel = mock.AsyncMock(
            return_value=types.SimpleNamespace(id=501, send=send))
        session_key = "ambient-timeout"
        database = Path(os.environ["HERMES_HOME"]) / "ambient-timeout.sqlite"
        with mock.patch.object(ledger, "_db_path", return_value=database), \
                mock.patch.object(ledger, "ledger_enabled", return_value=True), \
                mock.patch("gateway.platforms.base.asyncio.sleep", new=_yielding_no_delay):
            queued, delivery = await self._complete_queued_reply("Hello", session_key)
            with closing(ledger._connect()) as conn:
                rows = conn.execute(
                    "SELECT obligation_id,content,original_content,state FROM delivery_obligations"
                ).fetchall()
        self.assertEqual(attempted, ["Hello", gif])
        self.assertEqual(accepted, ["Hello", gif])
        self.assertEqual(queued["text"], "Hello")
        self.assertEqual(queued["message_ids"], ["901"])
        self.assertTrue(queued.get("error"))
        self.assertEqual(delivery["text"], "Hello")
        self.assertEqual(delivery["message_ids"], ["901"])
        self.assertTrue(delivery.get("error"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], ledger.compute_obligation_id(session_key, "incoming", "Hello"))
        self.assertEqual(rows[0][2:], ("Hello", "failed"))
        self.assertIn(gif, rows[0][1])

    async def test_activation_refuses_framework_that_replays_complete_partial_replies(self):
        from gateway.platforms import base
        with mock.patch.object(base, "SUPPORTS_PARTIAL_DELIVERY_REPLAY", False, create=True):
            with self.assertRaises(RuntimeError):
                self.module._require_companion_framework()

    async def test_ambient_redispatch_reuses_the_same_admitted_recall(self):
        adapter = self.adapter
        adapter._speaker_tag = lambda message: "[speaker @alice id:101]"
        adapter._speaker_memory = mock.Mock(return_value=None)
        adapter._bounce_pre_dispatch = lambda message: None
        adapter._standby_wait = mock.AsyncMock(return_value=False)
        adapter._returning_after_absence = lambda message: 0
        adapter._basic_ambient_eligible = lambda message: True
        adapter._join_reason = lambda message: "name"
        adapter._ambient_hint_for = lambda message, **kwargs: "fixture ambient hint"
        adapter._note_origin_kind = lambda *args: None
        adapter._dedup = types.SimpleNamespace(discard=lambda key: None)
        attempts = []

        async def stock(_self, message):
            attempts.append(self.module._ambient_hint.get(""))
            return len(attempts) == 2

        with mock.patch.object(DiscordAdapter, "_discord_message_admission", return_value=(True, False)), \
                mock.patch.object(DiscordAdapter, "_dispatch_discord_message", stock):
            self.assertTrue(await adapter._dispatch_inner(self.message))
        self.assertEqual(attempts, ["", "fixture ambient hint"])
        adapter._speaker_memory.assert_called_once_with("101")

    async def test_recovery_restores_speaker_context_after_success_refusal_and_exception(self):
        adapter = self.adapter
        adapter._speaker_tag = lambda message: "[speaker @alice id:101]"
        adapter._speaker_memory = mock.Mock(return_value="fixture private recall")
        adapter._bounce_pre_dispatch = lambda message: None
        adapter._standby_wait = mock.AsyncMock(return_value=False)
        for outcome in (True, False, RuntimeError("fixture dispatch failure")):
            with self.subTest(outcome=repr(outcome)):
                token = self.module._speaker_context.set(("prior caller", "sentinel facts"))
                try:
                    async def recovered(_self, message):
                        if isinstance(outcome, Exception):
                            raise outcome
                        return outcome
                    with mock.patch.object(DiscordAdapter, "_discord_message_admission", return_value=(True, False)), \
                            mock.patch.object(DiscordAdapter, "_dispatch_recovered_message", recovered):
                        if isinstance(outcome, Exception):
                            with self.assertRaises(RuntimeError):
                                await adapter._dispatch_recovered_message(self.message)
                        else:
                            self.assertEqual(await adapter._dispatch_recovered_message(self.message), outcome)
                    self.assertEqual(self.module._speaker_context.get(), ("prior caller", "sentinel facts"))
                finally:
                    self.module._speaker_context.reset(token)
