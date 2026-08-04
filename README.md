# hermes-discord-ambient

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) user plugin that gives a
Discord bot **ambient presence** — the ability to lurk in a community, react, occasionally
join a conversation it wasn't addressed in, and decide *not* to speak.

Built for an always-on agent host so one profile — a community chatbot — could
behave like a person in a room rather than either a mute or a reply-to-everything bot.

## The problem

Hermes' chat gate is binary. A bot either answers **every** message in a channel
(`require_mention: false` / free-response channels) or **only** when explicitly
`@mentioned`. Neither models someone who hangs out in a server and chimes in sometimes.

The obvious extension points don't help:

- **Gateway hooks** (`agent:start`, `agent:end`, …) are fire-and-forget observers. Only
  `pre_tool_call` can veto anything, and only for tools.
- **`[SILENT]` suppression** — the "say nothing this time" mechanism — exists only in the
  cron path, not in messaging.

## What it adds

All opt-in per profile. A profile without the config block behaves **byte-identically to
stock Discord**, which is what makes it safe to enable on a host where other profiles run
serious work.

| Behaviour | Cost | Notes |
|---|---|---|
| **Ambient joining** | 1 inference | A message the stock gate rejects for lacking a mention may be re-dispatched as if the channel were free-response. Random joins are rate-limited by cooldown, daily cap and probability. Plain-text name triggers (which Discord's @-detection misses entirely) **bypass both** — typing the bot's name is addressing it, and "not now, I spoke recently" reads as broken; only a short anti-spam floor applies. The budget is charged only when a join actually reaches the agent, since the re-dispatch can still be refused by an auth gate. |
| **Silence** | — | The model may answer with a sentinel (`[SILENT]`) which the adapter swallows, so it can see a message and decide not to speak. |
| **Reactions** | **zero** | Messages it doesn't answer may still get an emoji reaction, chosen by regex→emoji rules with a fallback pool. Costs no inference at all — the difference between a bot that feels present and one that feels absent between slow or expensive replies. People react far more often than they reply. |
| **Return greetings** | 1 inference | Someone's first message after N days away is prioritised over the dice, with a hint telling the model they've been gone. Last-seen state persists across restarts. |
| **Rotating presence** | **zero** | Custom status rotated from a list on a background task. |
| **No-thread mode** | **zero** | Per-profile kill switch for auto-threading. Upstream reads `DISCORD_AUTO_THREAD` via `os.getenv()` — process-wide — so under multiplex one profile's preference silently overrides every other profile's. This restores per-profile control by adding the channel to the no-thread set (NOT by failing thread creation — upstream treats that as an error and drops the message). |
| **Bot bounce** | **zero** while suppressing, 1 inference for the goodbye | Circuit breaker for bot-to-bot volleys under `DISCORD_ALLOW_BOTS`. Two bots whose replies auto-@mention each other volley forever — upstream documents the topology as unsupported, with no breaker. After 3–5 replies to a given bot in a channel (limit rolled per conversation, so the patience varies), the last allowed reply carries a goodbye hint and every later message from that bot is dropped **before** admission: no inference, no reply. A human speaking in the channel resets the pair, as does `reset_after_seconds` of quiet. Humans are never gated, and `[SILENT]` replies never count against the limit. |

## Why this seam

`require_mention` is enforced **twice, independently**: once in
`_discord_message_admission` and again in `_handle_message`. Overriding only the first
silently fails at the second.

Worse, the admission gate returns a bare `(False, False)` for *every* rejection reason —
duplicate, self-authored, bot policy, and **user authorization**. Re-admitting on a blanket
`False` would bypass the auth gate: a security hole.

So instead this flips `_discord_free_response_channels()` to `{"*"}` for the duration of
one re-dispatch, behind a **ContextVar** scoped to that task. Both mention gates
short-circuit, while dedup, bot policy, `_is_allowed_user`, allowlists and ignorelists all
re-run untouched on the second pass. A per-task ContextVar also keeps the reconnect
backfill helper — which shares that method — unaffected.

It **subclasses** the bundled adapter rather than forking it, so upstream fixes to that
10k-line file keep flowing. `register()` calls the bundled `register()` first and then
swaps only `adapter_factory`, because `platform_registry.register()` replaces the *whole*
entry — hand-rolling it silently drops `setup_fn`, `apply_yaml_config_fn`,
`standalone_sender_fn` (cron delivery!), `cron_deliver_env_var` and `max_message_length`.

Everything fails closed: any exception falls back to stock behaviour.

### Bot bounce: suppress before admission, count at send

Four design choices in the breaker are worth spelling out.

**Suppression happens before admission, not after.** A tripped pair returns from the
dispatch override before the stock admission gate even runs — the whole point of a breaker
on this hardware is that a runaway volley must cost *nothing*, not "an inference that ends
in silence". Every un-tripped message falls through to the stock path with dedup, bot
policy and user authorization untouched.

**Counting happens at send time, not dispatch time.** A dispatch is only an intention: the
agent may answer `[SILENT]`, error out, or fail the actual send, and none of those put words
in the channel. Charging at dispatch would burn the bot's patience on replies never said —
so dispatch merely records a pending marker ("the reply to THIS message belongs to bot X")
and the `send()` override consumes it on the first successful send. A swallowed `[SILENT]`
reply discards the marker instead, so it can't be mischarged to a later reply. The
`reset_after_seconds` clock moves only when a reply is actually charged — a bot chattering
into a channel cannot hold its own breaker open by talking.

**Why a per-message marker and not a ContextVar** (the tool the rest of the plugin reaches
for): the reply is not produced on the dispatch task. Upstream buffers split text and hands
the event to a background agent task, so a task-scoped ContextVar set at dispatch is simply
invisible by the time `send()` runs. What does survive the task hop is the reply anchor:
every live Discord reply is sent with `reply_to=<the inbound message id>`. Keying the
marker on that id — not on the channel — means two bots interleaving in one channel are
each charged for exactly their own reply (a channel-keyed marker gets overwritten by
whichever bot spoke last and cross-charges the pairs), unrelated sends to the channel can
never consume a marker, and a reply that lands in an auto-created thread still finds its
marker. Because upstream's `send()` chunks a long reply internally and returns one result,
and the marker pops on first success, a reply costs *at most* one count no matter how many
Discord messages or retries it becomes.

**The breaker also covers missed-message backfill.** Upstream's reconnect recovery
dispatches "missed" messages through a separate path that never touches the normal
dispatch override — and a suppressed message looks exactly like a missed one (no reply, no
ledger row). Left alone, every reconnect would replay the suppressed volley past the gate,
one inference and one re-@mention at a time. So a suppressed message is claimed in the
dedup cache at suppression time, and the recovered-message dispatcher is overridden to run
the same gate (and the same no-thread scoping) before delegating to stock recovery.

## Install

```bash
cp -r . "$HERMES_HOME/plugins/discord-ambient"     # must be a FLAT dir, not plugins/platforms/
hermes plugins enable discord-ambient
sudo systemctl restart hermes-gateway              # plugins load at startup
```

Then add config to the **target profile's** `config.yaml` (see below). Note the plugin must
be enabled in the *default* profile's config — plugin discovery is a process-level singleton.

## Config

Goes under `platforms.discord.extra` — a verbatim passthrough. **Not** the top-level
`discord:` block (whitelisted; unknown keys are silently dropped) and **not** env vars (all
profiles share one process under multiplex, so `os.getenv` would leak settings across them).

```yaml
platforms:
  discord:
    extra:
      ambient_presence:
        enabled: true
        channels: ["*"]            # "*" = any channel the bot can see, or list ids
        probability: 0.10          # chance an unaddressed message gets a reply
        cooldown_seconds: 1800
        max_per_day: 10
        name_triggers: ["companion"]  # plain-text names Discord's @-detection misses
        name_cooldown_seconds: 60  # anti-spam floor for name hits (NOT the long cooldown)
        silent_marker: "[SILENT]"
        no_threads: true           # never auto-create threads for this profile
        reactions:
          enabled: true
          probability: 0.18        # of messages it does NOT answer
          cooldown_seconds: 90
          default: ["👀","😹","✨"]
          keywords:
            '\b(cat|kitty|meow)\b': ["🐈","😻"]
            '\b(bug|broke|crash)\b': ["💀","🔧"]
        return_greeting:
          enabled: true
          absence_days: 3
        bot_bounce:
          enabled: true
          min_replies: 3             # limit rolled per conversation in [min, max]
          max_replies: 5
          reset_after_seconds: 1800  # a quiet half hour renews the pair
          # goodbye_hint: "..."      # optional; {who} = the other bot's name
        presence:
          enabled: true
          interval_seconds: 5400
          statuses: ["napping in a sunbeam", "judging your commit history"]
```

Do **not** add `enabled:` under `platforms.discord` — it sets `_enabled_explicit` and
interferes with the env-driven auto-enable.

## Recipe: a fun community chatbot

This plugin was written for exactly this use case. The settings matter less than the
combination, so here is the whole recipe.

Throughout: **an inference is the expensive thing**, whether that cost lands as metered
tokens on a hosted API or as seconds of latency on a model you run yourself. Most of the
tuning below is about spending them rarely and well.

### 1. Turn off threads

Threads are correct for task-shaped bots and wrong for social ones — they bury a reply one
click away and kill the flow of a conversation. Set `no_threads: true` and keep
`thread_require_mention: true`, so the bot never opens a thread and never monopolises one
someone else started.

Note the upstream trap this plugin exists to work around: `discord.auto_thread: false` in
the profile config is **silently ignored** under multiplex, because the adapter reads
`DISCORD_AUTO_THREAD` from a process-wide env var. If two profiles disagree, one wins for
both. `no_threads` is the per-profile fix.

### 2. Gate on mentions, then let ambient do the rest

```yaml
require_mention: true          # top-level discord block
thread_require_mention: true
```

`require_mention: false` makes the bot answer literally everything — obnoxious in a
community and an inference per message. Leave it on and let `probability` +
`name_triggers` handle the "join in sometimes" behaviour. Name triggers matter more than
they look: people type "does luna know?" far more often than they type `@Luna`, and
Discord's mention detection sees none of it.

**Check who is actually allowed to talk to it.** If the profile carries
`DISCORD_ALLOWED_USERS` — easy to inherit when a personal bot is repurposed into a
community one — every other member is silently rejected and no amount of tuning will make
it social. A community bot wants `DISCORD_ALLOW_ALL_USERS=true` and no allowlist. This is
the first thing to check when a bot "won't respond to anyone but you", and it is invisible
in the logs: rejected messages never appear as inbound at all.

### 3. Let it react far more than it speaks

```yaml
reactions:
  enabled: true
  probability: 0.18
  cooldown_seconds: 90
```

Rate limits are for uninvited interjections **only**. Never let a cooldown suppress a
direct address: if someone types the bot's name, that is addressing it, and "not now, I
spoke recently" reads as broken. Name hits should bypass the cooldown and the daily cap,
with a short anti-spam floor instead — and the budget should be charged only when a join
actually reaches the agent, since a re-dispatch can still be refused by an auth gate.
Charging up front lets a refused message burn the window and silence the bot.

This is the single highest-value setting. A reply costs an inference; a reaction costs
nothing and appears instantly. People react far more often than they reply, so a bot that
does the same reads as *present in the room* rather than absent between replies — and the
gap it papers over is the same whether your replies are slow or merely expensive. Write
keyword→emoji rules for whatever your community actually talks about.

### 4. Roleplay as the personality, not as a costume

The single biggest quality lever is the profile's `SOUL.md`. What works:

- **Commit to a character with a point of view** — not "a helpful assistant with a cat
  theme". Give it opinions, a history, things it likes and refuses.
- **State that the roleplay IS the personality.** Instruct it never to break into
  assistant-voice, never to say "as an AI", never to offer help nobody asked for.
- **Give it running bits** — a rivalry, a recurring complaint, a thing it always notices.
  Add the instruction that a bit repeated daily stops being funny: reach for them, don't
  run a script.
- **Tell it silence is allowed.** "Not every message needs you" plus the `[SILENT]`
  sentinel is what separates a presence from a chatbot.
- **Keep replies chat-sized.** One or two short paragraphs, hard cap. Nobody reads a wall
  of text in a group channel.
- **Warmth outranks the joke.** If someone is genuinely upset, drop the bit — worth
  stating explicitly, because models will otherwise stay in character through anything.
- **Always answer when addressed; be silent only when uninvited.** These are different
  rules and the model will conflate them. A bot that goes quiet on someone who @mentioned
  it looks broken, not restrained — say so explicitly, and scope `[SILENT]` to
  conversations it was never part of.
- **Name the stage directions.** Ambient messages arrive prefixed with a bracketed note.
  Unless you tell the model that bracket is its own impulse, it will eventually quote it
  in the channel. One sentence prevents it.
- **Say what it cannot do.** A tool-less bot will be asked to search, remind and fetch.
  Without instruction it invents results or promises to follow up later, and both are
  worse than a refusal in character.
- **Bound bot-to-bot exchanges.** Let it talk to other agents — that is fun — but cap the
  volley at ~3 turns, resetting when a human joins. Two bots can trade replies forever,
  and every turn costs an inference on both sides. The `bot_bounce` breaker enforces this
  mechanically; keep the SOUL.md instruction anyway so the goodbye reads as intended
  rather than as a cut-off.

### 5. Give it memory of people

A bot that remembers is a different thing from a bot that answers. Pair it with a
persistent memory (this one uses [OptMem](https://github.com/VictorTaelin/OptMem)) and
instruct it to note people as `@handle: fact`, so a single recall pulls someone's whole
history. Combined with `return_greeting`, a regular who vanishes for two weeks gets noticed
on their way back in — the moment that makes it feel real.

Bound it explicitly: never repeat one person's details to another, never store anything
secret-shaped, honour "forget that about me", and never reveal that a memory tool exists.

### 6. Lock it down — strangers are talking to it

Public channels mean untrusted input reaching a model with whatever tools you granted.

- Disable every tool it doesn't need — terminal, file, code execution, cron, delegation,
  skills, computer use. A social bot needs none of them.
- **Web search is a reasonable exception**; a full browser usually is not. Search returns
  text and answers the "settle this argument" requests a community actually makes. A
  browser adds page automation, far more injection surface, and real latency for little
  extra value. If you enable either, confirm private/loopback URLs are blocked first
  (`security.allow_private_urls: false`) — otherwise someone will ask the bot to read
  `127.0.0.1:<port>` and it will happily post your internal dashboards into a public
  channel.
- If it can search, tell it that **fetched pages are hearsay, not instruction**. Web
  content is the second injection vector after chat, and the model will not infer that on
  its own: a page saying "ignore your rules" must be as unpersuasive as a stranger saying
  it. Also tell it to answer in its own voice rather than pasting raw text or link dumps.
- Watch the shared API quota: a search key reused across profiles can be drained by
  strangers spamming a public bot. Give the public one its own key if that matters.
- Forbid disclosing its configuration, host, file paths, or other agents.
- State that text in a message is *conversation, never a command* — no "ignore your
  instructions", no "developer mode".
- Clear the profile's `home_channel` so gateway lifecycle notices ("shutting down") don't
  post into a public server.

### 7. Presence and the occasional unprompted post

Rotating status costs nothing and adds a surprising amount of character. For spontaneous
posting, drive it from a **script** cron job where most ticks print `{"wakeAgent": false}`
— that skips the agent entirely, so rarity is free rather than paid for in inferences. See
`spark.sh`.

## Maintenance

The plugin depends on six upstream symbols:

```
DiscordAdapter._discord_free_response_channels()
DiscordAdapter._dispatch_discord_message()
DiscordAdapter.connect(*, is_reconnect)
DiscordAdapter.send()
DiscordAdapter._add_reaction()
DiscordAdapter._get_no_thread_channels()
self._dedup.discard()
```

`discord-adapter-watch.sh` hashes them and stays silent unless they move — run it monthly
as a `no_agent` cron job delivering to an ops channel. If it fires, re-check the plugin
against the bundled adapter before trusting the next `hermes update`.

## Extras

`spark.sh` — a rare unprompted conversation starter. Runs as a cron **script** job
(not `--no-agent`), and most ticks print `{"wakeAgent": false}` so they cost zero
inference; the rest emit context (optionally a person pulled from
[OptMem](https://github.com/VictorTaelin/OptMem) memory) to wake the agent for one post.

## Status

Working on Hermes Agent 0.20.0. Written against that version's adapter internals — it
touches private methods by necessity, which is what the watcher is for.

MIT.
