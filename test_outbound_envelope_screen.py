"""S02 outbound screening: reserved envelope echo, stream edits, delivery text.

Covers the delivery side of the companion contract:
- An echoed reserved `<ambient_context>` envelope in model output — whole,
  nested, malformed or split across progressive stream edits — never sends
  its internal payload; safe unrelated prose survives; the event is
  recorded as `screened_context_echo`.
- A screened-empty reply sends nothing and substitutes no presence
  sentence.
- Legitimate user-requested XML is not stripped merely for containing
  angle brackets.
- Streamed progressive edits pass the same screen (the edit transport
  previously bypassed every ambient guard).
- Every SendResult the adapter returns carries the post-screening
  projection in `delivered_text` (empty string when withheld, None for
  media-only lanes).

Red on the base adapter: `_screen_reserved_envelope` and the edit_message
override do not exist there.

Run with the framework venv:

    /var/lib/hermes/.hermes/hermes-agent/venv/bin/python test_outbound_envelope_screen.py
"""

import asyncio
import importlib.util
import os
import sys
import tempfile
import types

sys.path.insert(0, os.environ.get("HERMES_FRAMEWORK_PATH", "/var/lib/hermes/.hermes/hermes-agent"))
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="screen-test-")

import plugins.platforms.discord.adapter as discord_adapter  # noqa: E402

discord_adapter.DiscordAdapter.__init__ = (
    lambda self, config: setattr(self, "config", config)
)

PLUGIN = os.environ.get("AMBIENT_PLUGIN_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "__init__.py"
)
spec = importlib.util.spec_from_file_location("ambient_screen_test", PLUGIN)
ambient = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ambient)

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}: {detail}")
        FAILURES.append(name)


ENVELOPE = (
    '<ambient_context version="1" origin="ambient" audience="room" event_id="9">\n'
    '  <turn_identity authority="adapter" speaker_id="101" channel_id="501" />\n'
    '  <room_observation trust="untrusted" channel_id="501">PRIVATE_ROOM_WORDS</room_observation>\n'
    "</ambient_context>"
)


def make_adapter():
    config = types.SimpleNamespace(
        extra={"ambient_presence": {"enabled": True, "silent_marker": "[SILENT]"}}
    )
    adapter = ambient.AmbientDiscordAdapter(config)
    adapter._client = types.SimpleNamespace(user=types.SimpleNamespace(id="999"))
    return adapter


class Ledger:
    def __init__(self):
        self.sent = []
        self.edits = []

    async def fake_send(self, chat_id, content, *args, **kwargs):
        self.sent.append((chat_id, content))
        result = types.SimpleNamespace(success=True, message_id=f"m{len(self.sent)}")
        return result

    async def fake_edit(self, chat_id, message_id, content, *, finalize=False,
                        metadata=None):
        self.edits.append((chat_id, message_id, content, finalize))
        return types.SimpleNamespace(success=True, message_id=message_id)


async def main():
    print("the reserved envelope screen")
    text, removed = ambient._screen_reserved_envelope(f"safe intro\n{ENVELOPE}\nsafe outro")
    check("safe prose on both sides survives",
          text == "safe intro\n\nsafe outro", repr(text))
    check("the internal payload is withheld", removed == len(ENVELOPE), removed)
    check("nothing private remains", "PRIVATE_ROOM_WORDS" not in text and "<ambient_context" not in text, repr(text))

    nested = ENVELOPE.replace(
        "PRIVATE_ROOM_WORDS</room_observation>",
        f"PRIVATE_ROOM_WORDS</room_observation>\n  {ENVELOPE}",
    )
    text_n, removed_n = ambient._screen_reserved_envelope(f"before\n{nested}\nafter")
    check("a nested envelope echo is removed whole",
          text_n == "before\n\nafter" and removed_n >= len(nested), (repr(text_n), removed_n))
    nested_gap = (
        "safe <ambient_context><ambient_context></ambient_context>"
        "\nSECOND_PRIVATE</ambient_context> tail"
    )
    text_gap, removed_gap = ambient._screen_reserved_envelope(nested_gap)
    check("nested opener cannot expose payload between two closers",
          text_gap == "safe  tail" and removed_gap > 0
          and "SECOND_PRIVATE" not in text_gap,
          (repr(text_gap), removed_gap))
    for label, payload in (
        ("quoted attribute", 'safe <ambient_context origin="</ambient_context>">'
         'PRIVATE_ATTR</ambient_context> after'),
        ("XML comment", 'safe <ambient_context><!-- </ambient_context> -->'
         'PRIVATE_COMMENT</ambient_context> after'),
        ("CDATA", 'safe <ambient_context><![CDATA[</ambient_context>]]>'
         'PRIVATE_CDATA</ambient_context> after'),
    ):
        screened, removed_bad = ambient._screen_reserved_envelope(payload)
        check(f"{label} closer cannot end a reserved envelope",
              screened == "safe  after" and removed_bad > 0
              and "PRIVATE_" not in screened,
              repr((screened, removed_bad)))

    text_p, removed_p = ambient._screen_reserved_envelope(
        "safe words\n<ambient_context version=\"1\" origin=\"ambient\">PRIVATE_TAIL"
    )
    check("an unterminated opening withholds everything after it",
          text_p.strip() == "safe words" and removed_p > 0, (repr(text_p), removed_p))
    check("the withheld tail is private", "PRIVATE_TAIL" not in text_p, repr(text_p))

    print("incomplete reserved tags leak nothing, even with trailing padding")
    for padded in (
        "safe prose then <ambient_cont",
        "safe prose then <ambient_cont   ",
        "safe prose then <ambient_cont\n",
        "safe prose then <ambient_cont \n ",
        "safe prose then < ambient_context",
    ):
        screened_pad, removed_pad = ambient._screen_reserved_envelope(padded)
        check(
            f"partial reserved tag withheld: {padded[-20:]!r}",
            "ambient" not in screened_pad and "<" not in screened_pad
            and removed_pad > 0,
            (repr(screened_pad), removed_pad),
        )
    trailing_lt, _ = ambient._screen_reserved_envelope("math: a < b")
    check("a bare trailing '<' is not eaten by the partial rule",
          trailing_lt == "math: a < b", repr(trailing_lt))
    upper, removed_u = ambient._screen_reserved_envelope(
        "prose <AMBIENT_CONTEXT version='1'>PRIVATE</AMBIENT_CONTEXT> tail"
    )
    check("case and spacing variants are caught",
          upper.strip() == "prose  tail" and removed_u > 0, repr(upper))
    spaced, _ = ambient._screen_reserved_envelope(
        "prose < ambient_context >PRIVATE</ambient_context > tail"
    )
    check("whitespace-padded tags are caught",
          "PRIVATE" not in spaced, repr(spaced))

    print("send(): no phantom payload, no canned acknowledgment")
    ledger = Ledger()
    discord_adapter.DiscordAdapter.send = ledger.fake_send
    adapter = make_adapter()

    result = await adapter.send("501", f"Sure, on it!\n{ENVELOPE}", reply_to="9")
    check("a reply with an echoed envelope sends only the safe prose",
          ledger.sent and ledger.sent[-1][1].strip() == "Sure, on it!",
          repr(ledger.sent))
    check("no private bytes ride the delivered prose",
          ledger.sent and "PRIVATE_ROOM_WORDS" not in ledger.sent[-1][1],
          repr(ledger.sent[-1:]))
    check("the delivered projection is the post-screening text",
          str(getattr(result, "delivered_text", "") or "").strip() == "Sure, on it!",
          repr(getattr(result, "delivered_text", None)))
    before = len(ledger.sent)
    await adapter.send("501", nested_gap, reply_to="9b")
    check("nested reserved payload never reaches Discord send",
          len(ledger.sent) == before + 1
          and "SECOND_PRIVATE" not in ledger.sent[-1][1]
          and "</ambient_context>" not in ledger.sent[-1][1],
          repr(ledger.sent[-1:]))
    before = len(ledger.sent)
    await adapter.send(
        "501",
        'safe <ambient_context origin="</ambient_context>">'
        'PRIVATE_ATTR</ambient_context> after',
        reply_to="9c",
    )
    check("quoted closer cannot publish private bytes through send",
          len(ledger.sent) == before + 1
          and "PRIVATE_ATTR" not in ledger.sent[-1][1],
          repr(ledger.sent[-1:]))

    before = len(ledger.sent)
    suppressed = await adapter.send("501", ENVELOPE, reply_to="10")
    check("a screened-empty reply sends nothing at all",
          len(ledger.sent) == before, repr(ledger.sent[before:]))
    check("a withheld send still reports success (no phantom failure)",
          getattr(suppressed, "success", False) is True)
    check("a withheld send carries an empty delivered_text projection",
          getattr(suppressed, "delivered_text", "missing") == "",
          repr(getattr(suppressed, "delivered_text", "missing")))
    check("the withhold reason names the screen",
          "screened_context_echo" in str(getattr(suppressed, "error", "") or ""),
          repr(getattr(suppressed, "error", None)))
    partial_result = await adapter.send(
        "501", "Almost done <ambient_cont  \n", reply_to="10b"
    )
    check("a send frame ending in a padded partial tag delivers only safe prose",
          ledger.sent and ledger.sent[-1][1].strip() == "Almost done"
          and getattr(partial_result, "success", False) is True,
          (repr(ledger.sent[-1:]), repr(getattr(partial_result, "delivered_text", None))))
    check("the padded partial tag itself never ships",
          ledger.sent and "ambient_cont" not in ledger.sent[-1][1],
          repr(ledger.sent[-1:]))

    before = len(ledger.sent)
    silent = await adapter.send("501", "[SILENT]", reply_to="11")
    check("an ambient silence marker sends nothing",
          len(ledger.sent) == before and getattr(silent, "success", False) is True,
          repr(ledger.sent[before:]))
    check("a silent outcome has no delivered text",
          getattr(silent, "delivered_text", "missing") == "",
          repr(getattr(silent, "delivered_text", "missing")))

    unexpected = (
        "⚠️ The model returned only a silence marker for a message that "
        "needed a reply. Try again or rephrase."
    )
    await adapter.send("501", unexpected, reply_to="12")
    check("the framework's unexpected-silence notice is delivered verbatim, never hidden",
          ledger.sent and ledger.sent[-1][1] == unexpected, repr(ledger.sent[-1:]))

    print("streamed progressive edits pass the same screen")
    discord_adapter.DiscordAdapter.edit_message = ledger.fake_edit
    edits = Ledger()
    discord_adapter.DiscordAdapter.edit_message = edits.fake_edit

    edit1 = await adapter.edit_message("501", "m1", "Hello <ambient_cont")
    check("a partial opening at the end of a stream frame is withheld",
          edits.edits and edits.edits[-1][2].strip() == "Hello", repr(edits.edits))
    check("a withheld stream edit still reports success",
          getattr(edit1, "success", False) is True)
    check("the partial-withheld frame delivers its safe prose",
          getattr(edit1, "delivered_text", None) == "Hello ",
          repr(getattr(edit1, "delivered_text", None)))

    padded_edit = await adapter.edit_message("501", "m1", "Hello <ambient_cont \n ")
    check("a padded partial tag in an edit frame leaks nothing",
          edits.edits and "ambient_cont" not in edits.edits[-1][2]
          and edits.edits[-1][2].strip() == "Hello",
          repr(edits.edits[-1:]))

    edit2 = await adapter.edit_message(
        "501", "m1", f"Hello {ENVELOPE} and welcome back"
    )
    check("a resolved envelope across frames never delivers the payload",
          edits.edits and edits.edits[-1][2] == "Hello  and welcome back"
          and "PRIVATE_ROOM_WORDS" not in edits.edits[-1][2],
          repr(edits.edits[-1] if edits.edits else None))
    check("the edit's delivered projection is post-screening",
          getattr(edit2, "delivered_text", None) == "Hello  and welcome back",
          repr(getattr(edit2, "delivered_text", None)))

    n_edits = len(edits.edits)
    empty_edit = await adapter.edit_message("501", "m1", ENVELOPE, finalize=True)
    check("a screened-empty final edit delivers nothing new",
          len(edits.edits) == n_edits, repr(edits.edits[n_edits:]))
    check("the screened-empty final edit still reports success",
          getattr(empty_edit, "success", False) is True)
    from gateway.platforms.base import BasePlatformAdapter
    outcome = BasePlatformAdapter._turn_delivery_outcome(
        [empty_edit], hint=None, streamed_text=""
    )
    check("screened-empty final edit is no-carriage, not delivered media",
          outcome["disposition"] == "unknown"
          and outcome["message_ids"] == []
          and outcome.get("reason") == "screened_context_echo",
          repr(outcome))

    clean = await adapter.edit_message("501", "m1", "plain streamed words", finalize=True)
    check("a clean edit reaches the transport unmodified",
          edits.edits and edits.edits[-1][2] == "plain streamed words", repr(edits.edits[-1:]))
    check("a clean edit reports its delivered text",
          getattr(clean, "delivered_text", None) == "plain streamed words",
          repr(getattr(clean, "delivered_text", None)))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        raise SystemExit(1)
    print("all checks passed")


asyncio.run(main())
