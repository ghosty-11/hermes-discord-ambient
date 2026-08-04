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
| **Ambient joining** | 1 inference | A message the stock gate rejects for lacking a mention may be re-dispatched as if the channel were free-response. Rate-limited by cooldown, daily cap and probability. Plain-text name triggers (which Discord's @-detection misses entirely) always qualify. |
| **Silence** | — | The model may answer with a sentinel (`[SILENT]`) which the adapter swallows, so it can see a message and decide not to speak. |
| **Reactions** | **zero** | Messages it doesn't answer may still get an emoji reaction, chosen by regex→emoji rules with a fallback pool. On CPU inference this is the difference between a bot that feels present and one that feels laggy — people react far more often than they reply. |
| **Return greetings** | 1 inference | Someone's first message after N days away is prioritised over the dice, with a hint telling the model they've been gone. Last-seen state persists across restarts. |
| **Rotating presence** | **zero** | Custom status rotated from a list on a background task. |
| **No-thread mode** | **zero** | Per-profile kill switch for auto-threading. Upstream reads `DISCORD_AUTO_THREAD` via `os.getenv()` — process-wide — so under multiplex one profile's preference silently overrides every other profile's. This restores per-profile control. |

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
        presence:
          enabled: true
          interval_seconds: 5400
          statuses: ["napping in a sunbeam", "judging your commit history"]
```

Do **not** add `enabled:` under `platforms.discord` — it sets `_enabled_explicit` and
interferes with the env-driven auto-enable.

## Maintenance

The plugin depends on six upstream symbols:

```
DiscordAdapter._discord_free_response_channels()
DiscordAdapter._dispatch_discord_message()
DiscordAdapter.connect(*, is_reconnect)
DiscordAdapter.send()
DiscordAdapter._add_reaction()
DiscordAdapter._auto_create_thread()
self._dedup.discard()
```

`discord-adapter-watch.sh` hashes them and stays silent unless they move — run it monthly
as a `no_agent` cron job delivering to an ops channel. If it fires, re-check the plugin
against the bundled adapter before trusting the next `hermes update`.

## Extras

`companion-spark.sh` — a rare unprompted conversation starter. Runs as a cron **script** job
(not `--no-agent`), and most ticks print `{"wakeAgent": false}` so they cost zero
inference; the rest emit context (optionally a person pulled from
[OptMem](https://github.com/VictorTaelin/OptMem) memory) to wake the agent for one post.

## Status

Working on Hermes Agent 0.20.0. Written against that version's adapter internals — it
touches private methods by necessity, which is what the watcher is for.

MIT.
