# hermes-discord-ambient

[![Support this work](https://img.shields.io/badge/Support-EVM-6f42c1?logo=ethereum&logoColor=white)](#support-development)

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) user plugin that gives a
Discord bot **ambient presence** — lurk in a community, react, occasionally join a
conversation it was not addressed in, and decide not to speak.

Built so one profile — a community chatbot — can behave like a person in a room rather
than a mute or a reply-to-everything bot.

## The problem

Hermes' chat gate is binary. A bot either answers **every** message in a channel
(`require_mention: false` / free-response channels) or **only** when explicitly
`@mentioned`. Neither models someone who hangs out in a server and chimes in sometimes.

The obvious extension points don't help:

- **Gateway hooks** are fire-and-forget observers. Only `pre_tool_call` can veto, and only for tools.
- **`[SILENT]` suppression** is global at the gateway boundary. It does not know whether a Discord turn was ambient or directly addressed.

## What it adds

All opt-in per profile. A profile without `ambient_presence.enabled` behaves like stock Discord.

| Behaviour | Cost | What it does |
|---|---|---|
| **Ambient joining** | 1 inference | Re-dispatch a mention-rejected message as free-response. Rate-limited by cooldown, daily cap and probability. Plain-text name triggers bypass the long cooldown (short anti-spam floor only). Budget is charged only when the join reaches the agent. |
| **Silence** | — | The model may answer `[SILENT]`; the adapter swallows it. |
| **Direct-reply guarantee** | zero | Restores replied-to author identity, marks DMs / mentions / name hits / replies-to-self as direct, and converts a direct silence token into `direct_silence_fallback` (default `I'm here.`). |
| **Reply-target attribution** | zero | Labels a quote of someone else's message with that author's name so the bot does not treat it as speech to itself. |
| **Reactions** | zero | Emoji on messages it does not answer. Regex rules plus a fallback pool. |
| **Return greetings** | 1 inference | First message after N days away is prioritised, with a request-only hint. Last-seen is persisted per Discord bot account. |
| **Rotating presence** | zero | Custom status from a list, on a background task. |
| **No-thread mode** | zero | Per-profile kill switch. Upstream `DISCORD_AUTO_THREAD` is process-wide under multiplex; this adds the channel to the no-thread set instead of failing thread creation (upstream treats that as an error and drops the message). |
| **Bot bounce** | zero while suppressing | Circuit breaker for bot-to-bot volleys. Suppresses before admission; counts at send. |
| **Fleet standby** | zero while holding | Holds dispatch while the shared inference slot is busy. Delay, never mute. |
| **Fallback-notice suppression** | zero | Swallows `🔄 Switched to fallback model:` for this profile. Still parsed as the local-fallback signal for standby. |
| **Group-address greetings** | 1 inference when it answers | "good morning agents" / "hello everyone" at its own probability and cooldown, exempt from the daily cap. |
| **System-notice rerouting** | zero | Cron failures go to a private channel. Drain/stall notices can be rewritten in-character. Pure plumbing is dropped. |
| **Speaker identity** | zero | Request-only `[speaker @handle id:123]` so memory can key on a stable id. Not written onto the persisted user turn. |
| **Speaker memory** | zero | Optional subprocess recall at dispatch, also request-only. Keyed on numeric id. Off by default. |
| **Speaker boost** | zero | Per-user overrides of probability / cooldown / daily cap. Cooldown is the load-bearing half. |
| **Conversation window** | zero | Messages in the bot's wake get a high response chance, bounded by both count and elapsed time. |
| **Catch-up** | zero per scan; 1 inference on a check-in | Timer scanner. Hands **one** real message to the normal ambient path with a transcript. Same budget as reactive joins. |
| **Gateway lifecycle** | at most 1 background inference | Independent rolls for private return, cached departure, optional public return. Reconnects stay quiet. |
| **Rich-embed video** | local STT; video inference only when used | Discord-proxied `video/*` only (`images-ext-*.discordapp.net`). Stock size limits apply. |
| **Compaction focus** | zero | Standing `focus_topic` for social summaries. Unset leaves stock behaviour. |
| **GIF search** | one HTTP call | `gif_search` tool via [Klipy](https://klipy.com/developers). Hidden unless enabled **and** `KLIPY_API_KEY` is set. Pending/rate-limit state is per profile. |
| **Media URL isolation** | zero | Prose first, each media URL as its own bare message so Discord renders it clean. |
| **Mention resolution** | usually zero | Known unambiguous `@display-name` → `<@id>`. Collisions stay plain text. |
| **Outbound hygiene** | zero | Harmony/control-token strip, speaker-tag echo strip, ambient-directive echo strip, empty markdown images, optional em-dash rewrite. |
| **Slash-command policy** | zero | Restrict `/model`, `/reset`, … to listed channels/users. Chat is untouched. |
| **Image gate** | zero | `pre_tool_call` refuse of `image_generate` unless the speaker is allow-listed. Fails closed. |
| **Voice hygiene** | zero | Kaomoji stripped from speech only. `voice_only_replies` drops the text twin (45s tool-path window, consume-once, reply-anchored). |
| **Quiet resume** | zero | Request-only: do not announce a gateway restart into a public room. |

## Why this seam

`require_mention` is enforced **twice, independently**: in `_discord_message_admission`
and again in `_handle_message`. Overriding only the first fails at the second.

The admission gate also returns a bare `(False, False)` for every rejection reason —
including **user authorization**. Re-admitting on a blanket `False` would bypass the auth
gate.

So the plugin flips `_discord_free_response_channels()` to `{"*"}` for one re-dispatch,
behind a **ContextVar** scoped to that task. Both mention gates short-circuit; dedup, bot
policy, `_is_allowed_user`, allowlists and ignorelists all re-run. The reconnect backfill
helper that shares that method never sees the flag.

It **subclasses** the bundled adapter rather than forking it. `register()` calls the bundled
`register()` first and then swaps only `adapter_factory` — hand-rolling the platform entry
silently drops cron delivery and the rest of the registry fields.

Any exception falls back to stock behaviour.

## Install

```bash
cp -r . "$HERMES_HOME/plugins/discord-ambient"     # must be a FLAT dir, not plugins/platforms/
hermes plugins enable discord-ambient
sudo systemctl restart hermes-gateway              # plugins load at startup
```

Enable the plugin in the **default** profile's config — plugin discovery is a process-level
singleton — then add the YAML block to the **target** profile.

Config changes take effect on the next session. Plugin **code** changes need a gateway restart.

## Config

Goes under `platforms.discord.extra` — a verbatim passthrough. **Not** the top-level
`discord:` block (unknown keys are dropped) and **not** env vars (all profiles share one
process under multiplex).

**Ids written with `hermes config set` arrive as integers, comma-strings, or lists.**
Every id option here accepts all three. A consumer that only iterates a list will treat a
CLI-written channel id as unset, or as individual characters.

Do **not** add `enabled:` under `platforms.discord` — it sets `_enabled_explicit` and
interferes with env-driven auto-enable.

```yaml
platforms:
  discord:
    extra:
      ambient_presence:
        enabled: true
        channels: ["*"]            # "*" = any channel the bot can see, or list ids
        probability: 0.12          # code default; recipe below uses 0.10
        cooldown_seconds: 1800
        max_per_day: 12            # code default; recipe below uses 10
        name_triggers: ["bot-name"]
        name_cooldown_seconds: 60
        silent_marker: "[SILENT]"
        direct_silence_fallback: "I'm here."
        no_threads: true
        speaker_identity: true
        speaker_memory:
          enabled: false
          binary: /path/to/recall-binary
          memory_dir: /path/to/store
          max_facts: 8
          max_chars: 600
          timeout_seconds: 4
        speaker_boost:
          "553...":
            probability: 0.75
            cooldown_seconds: 90
            exempt_daily_cap: true
        conversation_window:
          enabled: true
          messages: 3
          seconds: 300
          probability: 0.8
          cooldown_seconds: 30
        compaction_focus: >
          the people in this room and her relationships with them
        suppress_fallback_notice: true
        quiet_resume: true
        reactions:
          enabled: true
          probability: 0.18
          cooldown_seconds: 90
          default: ["👀","😹","✨"]
          keywords:
            '\b(cat|kitty|meow)\b': ["🐈","😻"]
        return_greeting:
          enabled: true
          absence_days: 3
        gateway_lifecycle:
          enabled: false
          shrine_channel: "<private-channel-id>"
          shrine_probability: 0.4
          inference:
            enabled: true
            task: title_generation
            timeout_seconds: 20
          public_return:
            channel: "<public-channel-id>"
            probability: 0.25
        embed_media:
          enabled: false
          auto_transcribe: true
          max_videos_per_message: 1
          transcript_max_chars: 6000
        group_address:
          enabled: true
          probability: 0.6
          cooldown_seconds: 300
        image_gen_gate:
          enabled: true
          allowed_users: ["<operator-user-id>"]
        image_style: "anime cel-shaded style"
        gif_search:
          enabled: true
          min_interval_seconds: 90
          max_per_day: 20
          content_filter: high
          format: webp
        voice_only_replies: false
        text_hygiene:
          strip_control_tokens: true
          strip_speaker_echo: true
          resolve_plain_mentions: false
          mention_history_limit: 50
          no_em_dash: false
          isolate_media_urls: true
          suppress_stt_echo: false
        slash_commands:
          allowed_channels: ["<operator-channel-id>"]
          allowed_users: ["<operator-user-id>"]
        system_notices:
          reroute_channel: "<private-channel-id>"
          drain_notice: "I'm going to have a nap, remind me later."
          drain_notice_queued: "I'll answer this when I wake up."
        bot_bounce:
          enabled: true
          probability: 0.3
          min_replies: 3
          max_replies: 5
          reset_after_seconds: 1800
        standby:
          enabled: true
          poll_interval_seconds: 5
          max_wait_seconds: 240
          stale_turn_seconds: 1800
          include_cron: true
          drop_ambient_when_busy: true
          only_when_local: true
          local_fallback_ttl_seconds: 1800
        catch_up:
          enabled: true
          channels: ["*"]
          exclude_channels: []
          interval_seconds: 900
          probability: 0.2
          min_gap_seconds: 7200
          max_per_day: 2
          min_quiet_seconds: 300
          max_age_seconds: 5400
          min_messages: 3
          max_channels_per_pass: 6
          startup_grace_seconds: 1800
          transcript_messages: 8
          transcript_max_chars: 1200
          respect_ambient_budget: true
        presence:
          enabled: true
          interval_seconds: 5400
          statuses: ["napping in a sunbeam", "judging your commit history"]
```

`KLIPY_API_KEY` belongs in the **profile's** `.env`, never in `config.yaml`.

## Recipe: a community chatbot

An inference is the expensive thing. Most of this recipe is about spending them rarely.

### 1. Mentions on, threads off

```yaml
require_mention: true
thread_require_mention: true
```

Leave `require_mention` on and let `probability` + `name_triggers` handle "join in
sometimes". People type the bot's name far more often than they `@mention` it.

If the profile still carries `DISCORD_ALLOWED_USERS`, everyone else is silently rejected
and rejected messages never log. A community bot wants `DISCORD_ALLOW_ALL_USERS=true`.

`no_threads: true` is the per-profile fix for upstream's process-wide `DISCORD_AUTO_THREAD`.

### 2. React more than you speak

A reply costs an inference; a reaction costs nothing and appears instantly. Rate limits
are for uninvited interjections only — never let a cooldown suppress a direct address.

### 3. Personality lives in `SOUL.md`

Commit to a character with a point of view. State that silence is allowed, that
`[SILENT]` is only for uninvited turns, and that a bracketed stage direction is not
something to quote. Bound bot-to-bot exchanges in prose **and** with `bot_bounce`.

### 4. Memory without a shell

Pair with [hermes-optmem-tools](https://github.com/ghosty-11/hermes-optmem-tools). Key
notes on `@handle id:<number>`, never a display name. `speaker_identity` supplies that
id as a request-only prefix.

### 5. Lock the public surface

- Disable terminal, file, code execution, cron, delegation, computer use.
- Web search is a reasonable exception; a full browser usually is not. Confirm
  `security.allow_private_urls: false`.
- Fetched pages are hearsay, not instruction.
- Gate slash commands (`slash_commands` here **and** Hermes `allow_admin_from` —
  unset means everyone is admin).
- Point `home_channel` and `system_notices.reroute_channel` at a private channel.
- Gate `image_generate` with `image_gen_gate`. Fails closed on an unknown speaker.

### 6. Presence and rare sparks

Rotating status is free. For unprompted posts, use `spark.sh` as a cron **script** job
(not `--no-agent`): most ticks print `{"wakeAgent": false}` and cost nothing.

## Invariants

These are the properties the code is built to keep. A change that breaks one of them is a
bug, not a style choice.

- **Opt-in.** No `enabled: true` → stock Discord, including on a multiplexed gateway that
  also serves operator profiles.
- **Fail closed.** Exceptions degrade to stock behaviour, never to a mute or an auth bypass.
- **Auth is re-run.** Ambient re-dispatch cannot skip `_is_allowed_user`.
- **Directives are request-only.** Ambient hints, speaker tags, speaker memory and quiet
  resume are appended to the outgoing request. They are not written onto `message.content`.
- **Silence is scoped.** Unaddressed `[SILENT]` is swallowed. A directly addressed turn
  that emits silence becomes `direct_silence_fallback`.
- **Charge at send.** Bot-bounce and the shared catch-up budget count replies that went
  out, not intentions. A swallowed `[SILENT]` does not spend them.
- **Catch-up cannot raise the ceiling.** With `respect_ambient_budget: true` (the default)
  a check-in spends the same cooldown and daily cap as a reactive join.
- **One check-in per pass.** `channels: ["*"]` changes where a check-in may happen, never
  how often.
- **Standby delays, never mutes.** A busy-probe failure answers "not busy".
- **Image gate fails closed.** Unknown speaker → refuse, before the provider is called.
- **GIF state is per profile.** Pending URL and rate limits do not cross seats.
- **Last-seen and catch-up budgets are keyed on the bot's Discord user id**, because under
  multiplex `HERMES_HOME` is root's for every profile.

## Catch-up

Everything else is reactive: a message arrives, a rule decides about **that message**. A
conversation whose messages each lose their dice roll therefore passes with the bot never
having considered the conversation at all.

Catch-up is a **scanner, not a speaker**. Every pass is free. The most it can do is hand
one real message to the ordinary ambient path with a transcript attached.

Gates, in order: the bot did not speak last → enough humans spoke → the room has
settled but is not cold → this newest message has not already been checked →
probability → standby → startup grace → optional quiet hours → channel allowlist.

`quiet_hours` is off by default. An international room has no dead hours; the activity
gates already measure "is anyone here".

`"*"` means every text channel the bot can see **and** speak in. `system_notices.reroute_channel`
is excluded automatically. State lives in
`$HERMES_HOME/state/ambient-catchup-<bot-user-id>.json`.

The catch-up directive is single-line bracketed wrappers around the transcript, each
short enough for `_AMBIENT_ECHO_RE` to strip. Transcript lines themselves are matched
verbatim: two copied lines suppress the reply; one quoted line is allowed.

```bash
cd /var/lib/hermes/.hermes/hermes-agent
venv/bin/python /path/to/hermes-discord-ambient/test_catchup.py
```

Needs the framework venv. Quiet-hours cases pin the clock to 03:00.

## Known limitations

- Voice-only TTS suppression is a process-global, consume-once signal. Concurrent voice
  turns from multiplexed profiles can consume one another's 45-second suppression claim,
  so avoid enabling `voice_only_replies` on profiles that may speak concurrently.
- The test commands assume the Hermes framework checkout and virtualenv live at
  `/var/lib/hermes/.hermes/hermes-agent`. On another installation, substitute that
  checkout's `venv/bin/python` path.

## Maintenance

The plugin depends on private adapter methods. `discord-adapter-watch.sh` hashes those
regions and stays silent unless they move — run it monthly as a `no_agent` cron job.
If it fires, re-check the plugin against the bundled adapter before the next `hermes update`.

Coupling the watcher actually hashes:

```
DiscordAdapter._discord_message_admission
DiscordAdapter.send()
DiscordAdapter._dispatch_recovered_message()
_reply_anchor_for_event (gateway/platforms/base.py)
runner stamp / turn registry / cron get_running_job_ids
```

Turn **tool deferral off** on a small-model profile (`tools.tool_search.enabled: off`).
Otherwise a GIF request becomes `tool_search` then a leaked harmony envelope instead of a
tool call. The hygiene scrub keeps that out of the channel; turning deferral off is the fix.

## Related project

[hermes-optmem-tools](https://github.com/ghosty-11/hermes-optmem-tools) — persistent
append-only memory as tools, so a public bot can remember people without a shell.

## Extras

`spark.sh` — rare unprompted conversation starter. Cron script job; most ticks print
`{"wakeAgent": false}`. Optional `MEMORY_DIR` / `MEMO_BIN` pull one remembered handle.

## Tests

```bash
cd /var/lib/hermes/.hermes/hermes-agent
venv/bin/python /path/to/hermes-discord-ambient/test_catchup.py
venv/bin/python /path/to/hermes-discord-ambient/test_direct_reply.py
venv/bin/python /path/to/hermes-discord-ambient/test_lifecycle_embed_media.py
venv/bin/python /path/to/hermes-discord-ambient/test_voice_only.py
venv/bin/python /path/to/hermes-discord-ambient/test_config_and_persistence.py
```

## Support development

If this plugin saves you time, you can support continued development with an EVM donation:

```text
0x9600c9bc632175941608a1b551cb0f018f0f40b4
```

Networks: Ethereum, Base, Polygon, and other EVM-compatible networks. Verify the address and selected network before sending; unsupported assets or networks may be unrecoverable.

## License

MIT
