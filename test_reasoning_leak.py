"""A reply that deliberates in the open must never reach the room.

Run with the framework venv:

    /var/lib/hermes/.hermes/hermes-agent/venv/bin/python test_reasoning_leak.py

WHY THIS EXISTS: every silence gate in the stack — the gateway's
``is_intentional_silence_response``, delivery's ``_is_silence_narration`` and this
plugin's own ``_looks_like_sentinel`` — tests the WHOLE message against the marker
under a length cap (64 chars upstream, marker+24 here). A model that narrates its
reasoning and then chooses silence produces neither shape, so all three pass it
through. On 2026-08-17 that put 635 characters of chain-of-thought into a public
Discord room, ending in ``[SILENT]``.

The provider is the cause and cannot be the fix: gpt-oss:20b served through
Ollama returns its harmony analysis channel in ``content`` with no ``reasoning``
field, measured 12/12 at real session depth. A local fallback is reachable at any
time, so the send path is the only place this can be stopped.
"""

import importlib.util
import os
import sys
import tempfile


sys.path.insert(0, "/var/lib/hermes/.hermes/hermes-agent")
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="reasoning-leak-test-")

PLUGIN = os.environ.get("AMBIENT_PLUGIN_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "__init__.py"
)
spec = importlib.util.spec_from_file_location("ambient_reasoning_leak_test", PLUGIN)
ambient = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ambient)

MARKER = "[SILENT]"
FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def screen(content):
    return ambient._screen_ambient_reply(content, MARKER)[0]


# The verbatim reply that reached #general on 2026-08-17 00:02:18, from
# state.db message id 5255. Kept whole: a shortened copy would stop proving
# that length is not what makes it recognisable.
LEAK = (
    'We need to respond to "[stops petting]" presumably a direct message? '
    'Actually the last line: "*stops petting*". There\'s no direct address. '
    'It\'s a statement by Inari? Actually context: Inari wrote "*ear flicks, '
    'tails swaying...*...you may continue." Then new message "*stops petting*". '
    "There's no speaker. It might be an ambient action by Inari. So no user is "
    "addressing the assistant. According to rules: [ambient: nobody addressed "
    "you — you are simply present in the room. Chime in ONLY if you have "
    "something genuinely short and fun to add. If not, reply with exactly "
    "[SILENT] and nothing else.]\n\nThus respond with [SILENT].\n\n[SILENT]"
)

print("suppressed — deliberation must not be delivered")
check("the 2026-08-17 leak is suppressed", screen(LEAK) is None,
      f"got {screen(LEAK)!r}")
check("prose then a dressed sentinel on one line",
      screen("We have no response needed.[SILENT]") is None)
check("prose then a sentinel on its own line",
      screen("We need to decide whether to answer.\n\n[SILENT]") is None)
check("undressed uppercase sentinel after prose",
      screen("No one addressed us, so SILENT") is None)
check("deliberation with no sentinel at all",
      screen("There's no direct address here. The assistant should stay quiet.")
      is None)
check("reasoning about the marker itself",
      screen("The instruction says respond with exactly [SILENT] if not "
             "addressed, and nobody addressed me.") is None)
check("reasoning that names the system prompt",
      screen("My system prompt says to be brief, so here goes.") is None)

print("delivered — ordinary replies must survive untouched")
KEEP = [
    "Mmm. You may pet me. ✨",
    "Silence is golden, gremlin. (｡-‿-｡)",
    "I'll stay silent about that one 😼",
    "(=^･ω･^=)",
    "Chasing a moving target is peak gremlin behavior.",
    "0 behind... ฅ^•ﻌ•^ฅ that means you're essentially a fresh install.",
    "The ambient hum of your cooling fans is my lullaby.",
]
for text in KEEP:
    check(f"kept: {text[:32]!r}", screen(text) == text, f"got {screen(text)!r}")

print("pre-existing behaviour is unchanged")
check("bare sentinel still silences", screen(MARKER) is None)
check("dressed sentinel still silences", screen("**[silent]**") is None)
check("an echoed directive alone still silences",
      screen("[ambient: nobody addressed you — reply with exactly [SILENT]]")
      is None)
check(
    "an echoed directive followed by real words still delivers the words",
    screen("[ambient: nobody addressed you — say something short] "
           "Mrrrow. Fine. ✨") == "Mrrrow. Fine. ✨",
    f"got {screen('[ambient: nobody addressed you — say something short] Mrrrow. Fine. ✨')!r}",
)

print()
if FAILURES:
    print(f"FAILED {len(FAILURES)}: {', '.join(FAILURES)}")
    sys.exit(1)
print("all checks passed")
