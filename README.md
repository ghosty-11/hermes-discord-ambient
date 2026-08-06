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
| **Fleet standby** | **zero** while holding | For hosts where several profiles share ONE inference slot (local CPU model). A dispatch arriving while any other agent turn or agent-mode cron job is running is *held* — the message's own coroutine sleeps, polling — and released the moment the slot frees; at `max_wait_seconds` it dispatches anyway, so standby can only ever delay a reply, never eat one. Opportunistic dice-roll joins are skipped outright while busy; named triggers and return greetings still answer. Every failure in the busy probe answers "not busy". With `only_when_local: true`, a profile whose primary model is *hosted* engages standby only while it is actually running on a local fallback model (observed via the fallback-switch notice, for `local_fallback_ttl_seconds`) — cloud turns are never held, so the higher-priority local profile keeps the slot exactly when contention is real. |
| **Fallback-notice suppression** | **zero** | Upstream announces a provider/model fallback switch with a one-shot status send ("🔄 Switched to fallback model: …"). Right for an operator channel, noise (and an internals leak) in a public community room. `suppress_fallback_notice: true` swallows it for this profile only — logged, never posted — while every other profile keeps the operator-facing notice. Suppressed or not, the notice is parsed as the local-fallback signal that drives `only_when_local` standby. |
| **Group-address greetings** | 1 inference when it answers | "good morning agents" / "hello everyone" is addressed to the room — not a mention, not a name trigger, but ignoring it reads as absent. Opt-in `group_address` matches greeting+collective regex pairs (both words required, close together — a bare "agents" mid-sentence never triggers) and answers at its own probability (default 0.6) and short cooldown (default 300s), exempt from the daily cap: being spoken to is not inserting herself. |
| **System-notice rerouting** | **zero** | Cron delivery failures (`⚠️ Cron 'x' failed: …`) and the cron wrapper header are posted to whatever channel the job delivers to — for a social profile, the community room, where the bot's own plumbing does not belong. `system_notices.reroute_channel` sends them to a private channel instead: rerouted, never dropped, so the operator still sees every failure. Pairs with `cron.wrap_response: false`, which removes the `Cronjob Response / job_id / "to stop this job"` framing from normal deliveries. |
| **Speaker identity** | **zero** | Upstream labels every inbound message with the author's *display name* (hardcoded, no config). Display names are per-guild, user-editable and freely reused — an agent writing durable notes keyed on one will merge two people, or lose someone the day they rename. Worse, an agent instructed to "record the user id" **cannot comply**: the id never reaches the model. `speaker_identity: true` prepends a compact `[speaker @handle id:123]` to the dispatched text (once, also on re-dispatch and backfill), so memory notes can key on the stable handle and numeric id. **Telling the agent never to echo the tag is not enough** — models infer "messages start with a speaker tag, I am writing a message, so mine starts with one too" and open a reply with a tag naming *themselves*. Ours did it with the id copied verbatim out of the example in its own instructions, despite two separate files forbidding it: a strong structural pattern beats a prose prohibition on a small model. The tag is injected by this plugin, so `strip_speaker_echo` (default true) removes it on the way out. Keep the prompt guidance too — belt and braces — but do not rely on it. |
| **GIF search** | **zero** (one HTTP call) | Registers a `gif_search` tool returning an embeddable GIF URL from [Klipy](https://klipy.com/developers) — the successor to Tenor, whose API Google discontinued 2026-06-30. A tool rather than the bundled `gif-search` skill, because that skill drives curl+jq at a shell prompt and a public persona profile has no terminal (nor should it); Discord auto-embeds a bare URL, so a URL is all the agent needs. `content_filter: high` by default (the agent cannot preview what it posts), per-profile rate limits, and the tool is hidden entirely unless the profile sets `gif_search.enabled` **and** has `KLIPY_API_KEY` in its `.env`. Serves **animated WebP, not GIF**, by default: GIF is capped at a 256-colour palette, so gradients and dark scenes band into visible black blocks — WebP is 24-bit, roughly a third the bytes, and still embeds as an *image* (autoplays and loops inline, no player chrome), while `mp4`/`webm` are smaller still but render as a video embed. One trap worth knowing: the API's `format_filter` parameter reads like a search facet but actually strips the response to that single rendition, so requesting `format_filter=gif` makes every other format vanish from the payload. Omit it. |
| **Media URL isolation** | **zero** | Discord hides the raw URL and renders only the media when a message's *entire* content is one media link — that is the whole reason a GIF from the built-in picker looks clean, and it is not something the API can be asked for. Models never post a bare URL; they wrap it in chatter, so a GIF reply arrives as visible link + text + embed underneath. `isolate_media_urls` (default true) sends the prose first, keeping the reply anchor, then each media URL as its own bare message. Matching is per whitespace token rather than one regex over the message, because a greedy URL pattern swallows trailing punctuation and the next word; trailing sentence punctuation is trimmed, query strings are ignored when testing the extension, and a message that is *already* a bare URL is left untouched. Bot-bounce is charged once for the pair, not once per message. |
| **Outbound text hygiene** | **zero** | Three scrubs on the way out (the third, speaker-tag echo, is described in the Speaker identity row above). **Control-token leakage** (always on): models trained on OpenAI's *harmony* format — gpt-oss and its many free-tier rebadges — express a tool call as `<|channel|>commentary to=functions.name<|message|>{…}`. When the serving endpoint fails to parse that back into a structured call, the raw control text falls through as ordinary content and the bot posts its own plumbing to the room. The envelope is parsed, not string-stripped (token-by-token removal leaves the channel name and the argument JSON visible): a `final` payload survives, a `commentary`/`analysis` payload or anything carrying a `to=functions.` recipient is suppressed outright and logged. **Em dashes** (opt-in, `no_em_dash`): every model reaches for them and they read as machine-written, which is exactly the tell an in-character persona should not have — rewritten to a spaced hyphen so the intended clause boundary survives, skipping fenced code, and leaving unspaced en dashes alone because those are numeric ranges. |
| **Slash-command policy** | **zero** | Upstream shares ONE gate between chat admission and slash authorization, so an answer-everyone community profile also hands `/model`, `/reset`, … to every stranger — and under a multiplexed gateway the per-profile allow-all env flag may not even resolve on the interaction path, leaving the operator rejected while everyone chats freely. `slash_commands.allowed_channels` / `allowed_users` restrict slash invocations to explicit ids (matching invocations authorize directly; everything else gets the stock ephemeral rejection + admin alert). Chat is untouched. |

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

### Fleet standby: hold at dispatch, probe the runner

On a one-slot host, "run both turns" means "run both turns slowly" — the model server
serializes them request-by-request and the human watching Discord sees minutes of nothing.
Hermes has no per-profile pause or priority (the documented `/platform pause` only touches
the reconnect retry queue), so standby lives in the same pre-admission seam as the bounce
breaker: the dispatch coroutine simply waits, bounded, before entering the stock path.

The busy probe reads three things, all optional, all failing toward "not busy": the gateway
runner's live turn registry (`gateway_runner` is stamped on every adapter; entries appear
synchronously at turn start within the gateway — the gate is best-effort, and two messages
arriving in the same instant can both slip through, which matches the delay-never-mute
contract), the cron scheduler's
running-job set minus jobs whose `no_agent` flag marks them as plain scripts (a backup
script holds no slot; the id→flag map is cached on the jobs.json mtimes), and the async
delegation counter. The profile's own running turns count as busy on purpose: on one slot,
its second conversation should queue behind its first exactly like everyone else's work.
A wedged registry entry is aged out by `stale_turn_seconds`, and the hold itself is capped
by `max_wait_seconds` — the two bounds mean a stuck-on busy signal degrades to "replies
arrive a few minutes late", never to a mute bot. A held id is dedup-claimed for the
duration of the hold (and released just before dispatch), so the missed-message backfill
scan cannot replay a message that is merely parked. Known small prints, both accepted: a
held message can be answered after a newer one that arrived once the slot freed; holds
that bunch several bot messages together can make the bounce breaker jump straight from
counting to suppression, skipping the goodbye; and a gateway shutdown mid-hold drops the
held message the same way it drops any in-flight turn (backfill recovers it if enabled).

## Install

```bash
cp -r . "$HERMES_HOME/plugins/discord-ambient"     # must be a FLAT dir, not plugins/platforms/
hermes plugins enable discord-ambient
sudo systemctl restart hermes-gateway              # plugins load at startup
```

Then add config to the **target profile's** `config.yaml` (see below). Note the plugin must
be enabled in the *default* profile's config — plugin discovery is a process-level singleton.

**Config changes take effect on the next session; plugin CODE changes need a gateway
restart.** Editing the plugin without restarting is the most common "my change did
nothing" report — the running gateway holds the imported module.

## Config

Goes under `platforms.discord.extra` — a verbatim passthrough. **Not** the top-level
`discord:` block (whitelisted; unknown keys are silently dropped) and **not** env vars (all
profiles share one process under multiplex, so `os.getenv` would leak settings across them).

**Ids written with `hermes config set` arrive as INTEGERS, not strings** — the CLI coerces
numeric values, and a bracketed list arrives as a *string*. Every id option here accepts a
YAML list, a comma-separated string, or a bare scalar, so either form works; if you write
your own consumers, normalize all three or a policy will silently read as "unset" and fall
back to stock behaviour with no error in the log.

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
        speaker_identity: true     # prepend [speaker @handle id:123] so memories
                                   # key on stable ids, not mutable display names
        suppress_fallback_notice: true  # swallow "🔄 Switched to fallback model: ..."
                                   # for THIS profile only (logged instead);
                                   # other profiles keep the operator-facing notice
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
        group_address:
          enabled: true            # answer "good morning agents"-style room greetings
          probability: 0.6         # she's addressed, but so is everyone — roll for it
          cooldown_seconds: 300    # own short floor; exempt from the daily cap
          # patterns: [...]        # optional regex overrides (lowercased content)
        # hint: "..."             # optional; overrides the ambient-join hint
                                   # text sent with a re-dispatched message.
                                   # {marker} = the [SILENT] sentinel.
        gif_search:                # needs KLIPY_API_KEY in the PROFILE's .env
          enabled: true
          min_interval_seconds: 90   # a GIF is punctuation, not a personality
          max_per_day: 20
          content_filter: high       # off | low | medium | high
          format: webp               # webp (default) | gif | mp4 | webm
          sizes: [md, sm, hd]        # first rendition that exists wins
          attach_if_omitted: true    # default true — adapter posts the GIF itself
          attach_window_seconds: 180 # how long a fetched GIF stays attachable
          pool: 8                    # results requested from Klipy per search
          pick_from: 5               # choose randomly among the top N of those
          timeout_seconds: 12        # Klipy HTTP timeout
        voice_only_replies: false  # when a voice reply goes out, drop the text
                                   # twin the runner sends straight after it
        text_hygiene:              # scrub replies on the way out
          strip_control_tokens: true  # default true — leaked harmony envelopes
          strip_speaker_echo: true    # default true — the bot echoing its own tag
          no_em_dash: false           # rewrite "—" to " - " outside code fences
          isolate_media_urls: true    # default true — GIF gets its own message
          suppress_stt_echo: false    # drop the gateway's 🎙️ "<transcript>" echo
                                      # for THIS profile (see below)
        slash_commands:            # restrict /model, /reset, ... (chat unaffected)
          allowed_channels: ["<operator-channel-id>"]   # list, "a,b", or a bare id
          allowed_users: ["<operator-user-id>"]
        system_notices:            # keep agent plumbing out of the community room
          reroute_channel: "<private-channel-id>"   # cron failures land here instead
          # patterns: ["⚠️ Cron '", "Cronjob Response:"]   # optional override
        bot_bounce:
          enabled: true
          min_replies: 3             # limit rolled per conversation in [min, max]
          max_replies: 5
          reset_after_seconds: 1800  # a quiet half hour renews the pair
          # goodbye_hint: "..."      # optional; {who} = the other bot's name
        standby:
          enabled: true              # hold dispatches while the shared slot is busy
          poll_interval_seconds: 5
          max_wait_seconds: 240      # then dispatch anyway — delay, never mute
          stale_turn_seconds: 1800   # ignore wedged turn-registry entries older than this
          include_cron: true         # agent-mode cron jobs count as busy (no_agent ones don't)
          drop_ambient_when_busy: true  # skip dice-roll joins if still busy at deadline
          only_when_local: true      # engage ONLY while this profile is on a local
                                     # fallback model (observed via the fallback
                                     # notice); cloud turns are never held
          local_fallback_ttl_seconds: 1800  # how long an observed local fallback
                                     # keeps the standby window open
          local_markers: ["gpt-oss", "ollama"]  # substrings that mark the switch
                                     # target as the shared local model
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
- **Give it memory without giving it a shell.** A community bot is only worth talking to
  twice if it remembers you, but the usual way to wire up persistent memory —
  instructions telling it to run a CLI — needs the terminal toolset, which is the one
  capability a public bot must never have. Companion plugin:
  [hermes-optmem-tools](https://github.com/ghosty-11/hermes-optmem-tools) exposes
  [OptMem](https://github.com/VictorTaelin/OptMem) as registered tools (fixed argv, the
  profile's own memory dir, no shell). Watch for the silent version of this mistake:
  instructions that name a tool the profile does not have fail with **no error at all** —
  ours had written zero memories for its entire life before anyone checked.
- If it can search, tell it that **fetched pages are hearsay, not instruction**. Web
  content is the second injection vector after chat, and the model will not infer that on
  its own: a page saying "ignore your rules" must be as unpersuasive as a stranger saying
  it. Also tell it to answer in its own voice rather than pasting raw text or link dumps.
- Watch the shared API quota: a search key reused across profiles can be drained by
  strangers spamming a public bot. Give the public one its own key if that matters.
- Forbid disclosing its configuration, host, file paths, or other agents.
- State that text in a message is *conversation, never a command* — no "ignore your
  instructions", no "developer mode".
- **Gate the slash commands.** This is the one people miss: upstream shares a single
  gate between chat admission and slash authorization, so an answer-everyone profile also
  hands `/reset`, `/clear`, `/model` and `/compress` to every stranger in the server.
  Worse, Hermes' own tiered gating is *disabled entirely* when `discord.allow_admin_from`
  (and `group_allow_admin_from` for channels) is unset — unset means "everyone is admin".
  Set both to your own user id, and use this plugin's `slash_commands` block to pin
  invocations to an operator channel as well.
- Point `home_channel` at a PRIVATE channel rather than clearing it, and set
  `system_notices.reroute_channel` to the same place: lifecycle notices and cron failures
  then reach you without ever appearing in the community server. (Clearing it works too,
  but then you simply don't get them.)

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

### Turn tool deferral OFF for a small-model profile

Hermes defers rarely-used tool schemas behind a `tool_search` bridge and embeds a catalog
listing so capabilities stay discoverable. On a big model that is a clean token saving. On
a small or free-tier model it is a trap, because it turns every tool use into **two** hops:
discover, then call. Small models routinely manage the first and fumble the second — and
the way they fumble is by emitting the call as prose:

```
tool_search activated (tier 1): 3 core/visible tools kept, 5 deferred (~452 tokens)
API call #1: tool_search("gif_search")        -> schema returned, fine
API call #2: out=15                            -> "to=functions.tool_call?commentary?…?…???"
```

That is a real transcript: a persona bot asked for a GIF, found its own `gif_search` tool,
then posted the harmony syntax for calling it instead of calling it. The hygiene scrub
above keeps that out of the channel, but the fix is upstream of the symptom — with only
eight tools, deferral was saving ~450 tokens and costing the ability to use any of them:

```yaml
tools:
  tool_search:
    enabled: off      # auto | on | off
```

Rule of thumb: leave `auto` for a capable model with dozens of tools; set `off` whenever
the whole toolset would fit in context anyway. Check which way it went in the log — the
`tool_search activated` line names the count it deferred.

## Companion plugin

[hermes-optmem-tools](https://github.com/ghosty-11/hermes-optmem-tools) — persistent
append-only memory as tools, for the same reason this plugin exists: a bot that lives in a
public room should feel present and remember people while holding no dangerous capability.

## Extras

`spark.sh` — a rare unprompted conversation starter. Runs as a cron **script** job
(not `--no-agent`), and most ticks print `{"wakeAgent": false}` so they cost zero
inference; the rest emit context (optionally a person pulled from
[OptMem](https://github.com/VictorTaelin/OptMem) memory) to wake the agent for one post.

## Status

Working on Hermes Agent 0.20.0. Written against that version's adapter internals — it
touches private methods by necessity, which is what the watcher is for.

MIT.

## Voice hygiene (v1.11.0)

Three problems that only show up once an agent can speak, all per-profile except
where noted.

### `suppress_stt_echo` — the setting Hermes cannot give you per profile

When a voice message arrives, the gateway posts `🎙️ "<transcript>"` so you can check
STT quality. That is useful on an operator surface and noise on a public one.

Upstream has `stt.echo_transcripts`, but it is **not per-profile in practice**:
`_should_echo_stt_transcripts()` reads the `GatewayRunner`'s config, which is resolved
process-wide from the root `config.yaml`. Setting it on a profile does nothing; setting it
at the root silences every profile. Under a multiplexed gateway there is no supported way
to have it on for one agent and off for another.

This plugin intercepts the echo in `send()` and drops it for profiles that ask, which is
the only place the decision can be made per profile.

### `voice_only_replies` — no duplicate text after speech

`_send_voice_reply()` sends the audio and then the text reply. For an operator agent that
is a feature — the text is a transcript. For a personality bot it is the same sentence
twice.

With this on, one text send is suppressed per voice send, inside a 20-second window, and
the mark is **consumed** — so a genuine follow-up message a moment later still gets
through. The failure direction is deliberately "a text twin leaks", never "the agent goes
mute".

### `text_hygiene.strip_media_narration` — the model narrating its own tool result

A model that calls the `tts` tool gets back `MEDIA:<path>` and may write that into its
reply as prose:

```
[Media: AUDIO:/var/lib/hermes/.hermes/cache/audio/tts_20260807_014127.mp3]

I'm doing wonderful, darling! ...
```

Two problems: it is noise, and it puts a **host filesystem path into a public channel**,
leaking the HERMES_HOME layout — the base adapter has `_log_safe_path` precisely because
that matters.

Default **on**. Only the *bracketed narration* is stripped; a bare `MEDIA:<path>` is the
real directive the send pipeline consumes to deliver audio, and removing that would
silence the agent rather than tidy it.

With `voice_only_replies` also on, a reply that is nothing but a narration plus the spoken
words is suppressed entirely — the audio already IS the reply.

### `text_hygiene.strip_kaomoji` — kaomoji are not emoji

Hermes strips emoji before TTS (`_EMOJI_RE`), but that targets pictograph codepoints.
Kaomoji like `(=^･ω･^=)` are ordinary punctuation and letters, so they pass straight
through and get read aloud as several seconds of punctuation soup.

This wraps `tools.tts_text_normalize.prepare_spoken_text` — the **common chokepoint**, used
by the tts tool, the runner's auto voice reply, and the adapter path alike — so kaomoji are
removed from the **speech script only** — they stay in the posted text, where they are
half the personality.

**This one is process-wide**, not per-profile, and honestly so: `_send_voice_reply`
imports that function inside its body, so patching the module attribute affects every
profile. Nobody wants kaomoji read aloud, so a global is the truthful shape rather than a
config key that pretends otherwise.

Detection is a candidate regex **plus an explicit predicate**, not one clever pattern. The
first attempt used `re.VERBOSE` with a multi-line character class — in which whitespace is
*not* ignored — so it silently included a literal space and deleted `(no errors)` from a
status report. Ordinary parentheses must survive; when in doubt the filter keeps the text,
because a missed kaomoji is a second of odd audio while a false positive silently deletes
real words from what the agent says out loud.

### Two paths to speech, and why both need handling

Worth stating because the first version of this release only handled one:

| Path | Trigger | Audio delivered by |
|---|---|---|
| **Runner** | user sends a voice message; gateway auto-replies in voice | `adapter.send_voice()` |
| **Tool** | model calls the `tts` tool because it was asked to speak | a `MEDIA:` directive inside `send()` |

`voice_only_replies` and the kaomoji filter each have to cover both. v1.11.0 hooked only
`send_voice()` and only `_strip_markdown_for_tts`, so an agent that was *asked* to speak
still posted a duplicate text reply and still had its kaomoji read aloud. v1.11.1 covers
the tool path too.

### The ordering, measured rather than assumed (v1.12.0)

Instrumenting the real thing settled what two rounds of reading the source did not:

```
02:02:30      TTS audio saved                      <- speech generated
02:02:44.443  ambient.voice: text send  marks=[]   <- TEXT GOES OUT FIRST
02:02:44.749  Delivering 1 non-image MEDIA attachment
02:02:44.749  ambient.voice: send_voice voice_only=True
```

The text precedes the audio by ~300 ms, so **any mark set when audio is delivered is
always too late** — which is why v1.11.x never suppressed anything. The TTS *call*,
however, happens ~14 seconds earlier, and that is early enough to act on.

So `voice_only_replies` now keys off "speech was generated for this turn", recorded by
wrapping `text_to_speech_tool`.

**Scope (v1.12.1).** The flag lives in module state, but it is **armed only by profiles
that have `voice_only_replies` on**. Config resolution is profile-correct inside the TTS
tool — proven by the tool picking Edge for Companion and Piper for Assistant on the same gateway
— so `_profile_wants_voice_only()` can check at arming time. A profile without the setting
never arms it and therefore can never cause another profile's message to be dropped.

Together with **consume-once** (at most one text send is ever affected) and logging on
every suppression, cross-profile interference is closed rather than merely made unlikely.

**Window: 45s**, and picked from measurement, not taste. Across five real turns the gap
between the TTS call and the text send was 2.7s / 5.6s / 12.8s / 15.4s / 35.4s — dominated
by how long the model takes to finish generating *after* calling the tool, which on a
free-tier model is wildly variable:

| window | catches |
|---|---|
| 10s | 2/5 |
| 15s | 3/5 |
| 20s | 4/5 |
| 30s | 4/5 |
| **45s** | **5/5** |

Shrinking the window — the intuitive way to reduce accidental drops — would instead bring
the duplicate text straight back. Once arming is profile-scoped, a wider window costs
nothing.

## Stopping the text being generated, not just hidden (v1.13.0)

`voice_only_replies` suppresses prose in `send()` — but by then the tokens are already
spent. Suppression is cosmetic: it fixes what the user sees, not what was paid for.

So for profiles with `voice_only_replies` on, the `tts` tool's JSON result gains two
fields:

```json
{"success": true, "media_tag": "MEDIA:/…/tts_x.mp3",
 "reply_complete": true,
 "instruction": "This audio IS your complete reply. Output no text after this …"}
```

A tool result sits in context at exactly the point the model chooses its next move, which
is a far stronger place to say this than a system-prompt rule the model read thousands of
tokens ago. Added as **new fields** — delivery reads `media_tag`, so attachment handling is
untouched.

`voice_only_replies` stays on as the backstop for when the model ignores it, which a
free-tier model sometimes will. The two are complementary: the hint should make suppression
rare, and every suppression is logged, so *how often it fires is the measure of whether the
hint is working*.

### On cost

Worth being accurate about the size of the problem. On a free-lane profile a dropped
message costs no money — only free-tier quota and a little latency. And the prose is
generated in the **same completion** as the post-tool turn, so the model would emit
something regardless; the waste is tens of tokens, not an extra call. The hint is still
worth having, because it is nearly free and it makes the whole mechanism quieter.
