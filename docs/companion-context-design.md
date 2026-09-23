# Companion context design

**Status: portable source contract, not deployment evidence.** Shared
speaker-scoped companion turns retain zero injected observation snapshots.
The [README](../README.md) describes the source behavior. Verify deployed
revisions and canary results separately. Terms are defined in
[Companion context](../CONTEXT.md).

## Goal

Give the companion enough evidence to answer five questions: who is speaking, where the
conversation is happening, what changed, why this turn was admitted, and which actions and
memories are appropriate for this audience. Keep machine-owned facts out of model guesswork.
Preserve warmth, humor, initiative and the option not to speak. Do not optimize for message
count, artificial variety or apparent intimacy.

## Ownership

| Layer | Owns | Does not own |
|---|---|---|
| Identity and policy | Stable character, relationship boundaries, truthfulness, capability limits | Live speaker, timestamps, copied chat or tool results |
| Admission | Sender and channel authorization, direct/ambient origin, rate limits, event identity | A requirement to emit a sentence |
| Context assembly | Attributed, audience-filtered observations; one immutable bundle per turn | New authority, invented facts or personality |
| Model and tools | Response judgment and permitted actions | Identity authorization, storage scope or budget enforcement |
| Delivery | Intentional silence, actual text/media, errors and receipt accounting | Inventing a persona reply that the model never wrote |
| Memory and improvement | Useful sourced facts, corrections, commitments and reviewed proposals | Automatic promotion of roleplay, logs or suggestions into policy |

Keep one admission owner, one context renderer and one delivery-outcome owner. Keep the
existing profile-local prepared-turn binding rather than adding an orchestrator or a second
memory store. Extract pure rendering, admission and delivery code from the adapter only as
those boundaries change; do not begin with a repository-wide rewrite.

## Response contract

A direct mention, name hit or reply admits a turn. It does not justify canned availability
text. The companion answers an actual question, acknowledges meaningful interaction, uses a
relevant reaction, or ends an exhausted exchange. Empty pings and conversational closure do
not require repeated assurances of presence.

The existing text/media protocol remains. Normalize its result internally to text, media,
reaction, intentional silence or failure. `[SILENT]` is a control outcome, not dialogue.
Remove the replacement of a silent or screened-empty draft with `I'm here.`. Do not replace
it with a random phrase list, a stronger blanket “always answer,” or an extra model call on
every event. A screened-away unsafe draft is not the same outcome as intentional silence.
A genuine generation or delivery failure uses the existing error channel and a truthful
service notice where needed, not a fabricated emotional response.

Record the draft disposition, delivered disposition, event identifier and transformation
reason without retaining private content in metrics. Count limits against the outcome that
actually occurred. Keep the model's conversation consistent with delivered text and actions;
do not insert failed sends as successful assistant turns. Adapt streamed output, edited
messages, voice-only turns and media-only turns as well as final text sends.

Add a typed, server-created turn origin and response policy to the framework boundary.
Origin distinguishes direct, ambient, catch-up, scheduled and resume events. Policy is
`required` or `discretionary`: existing work surfaces default to required; only configured
companion surfaces use discretionary response, including conversational closure. Carry it
through queued terminal turns and stream/final shaping. Never derive it from message text.
The paired framework must supply this field; an older revision cannot infer it.

Use existing final-send obligations and `SendResult` for transport receipts. Add a
row-addressed `SessionDB.record_message_delivery(session_id, row_id, turn_id, outcome)`
writer that merges delivery metadata on the validated assistant row. The adapter supplies
its actual post-screening text/media projection; a plugin must not write raw SQL or match
the newest row by text. Preserve raw draft evidence, project actual delivery on replay,
and leave crash-interrupted receipts unknown rather than claiming success or resending.

## Tagged context

All framework and plugin additions must be visibly delimited. The human's message remains
separate and unchanged. Tags communicate type and provenance; they do not grant trust.

Use one versioned envelope with a small fixed set of block types. The renderer, not an
incoming message, supplies attribute names, producer identifiers and authorization fields.
For example:

```xml
<ambient_context version="1" origin="direct_reply">
  <turn_identity authority="adapter" speaker_id="123" channel_id="456" />
  <reply_observation trust="untrusted" source_message_id="789">
    Escaped quotation from the addressed message.
  </reply_observation>
  <room_observation trust="untrusted" truncated="false">
    Escaped recent conversation, with each speaker attributed.
  </room_observation>
  <memory_observation trust="retrieved" subject_id="123" audience="current_speaker">
    Escaped, sourced assertion; it may be outdated or wrong.
  </memory_observation>
</ambient_context>
```

Required rules:

- Separate trusted turn identity from untrusted names, topics, quotes, room talk, media
  transcripts and retrieved assertions. A stable numeric identity is not authorization to
  disclose everything known about that identity.
- Escape text and attribute values, including closing tags and control characters. Bound
  content before rendering so truncation cannot remove a closing delimiter. Accept Unicode
  without splitting its encoded bytes. Report omission and truncation explicitly.
- Omit absent blocks. Do not inject a paragraph explaining that nothing was retrieved.
  Distinguish absent evidence from a failed lookup when the failure changes the answer.
- Render instructions once in stable policy. Dynamic blocks describe the admitted event;
  they do not repeat a reply-placement manual, silence policy or persona biography.
- Label synthetic catch-up, scheduled and resume events as such. Never attribute a generated
  prompt or a transcript quote to a human's fresh request. Detect resume from authenticated
  lifecycle metadata, not a phrase that anyone can type.
- Cover reply quotes, room history, travel, return greetings, catch-up, voice transcription,
  media descriptions, automatic recall, tool errors and compaction summaries. Do not claim
  “all context is tagged” after changing only the ambient middleware.
- Keep tool-return content in its tool-result envelope; do not re-inject it as a new user
  instruction. Escape any untrusted payload nested in a textual result wrapper.
- Extend outbound screening to the renderer's reserved envelope tags, including nested,
  partial and chunk-split echoes. Never send the private payload before detecting the
  envelope. Withhold the internal block, preserve unrelated safe prose, and record
  `screened_context_echo`; if no prose remains, do not substitute a presence sentence.
  Do not strip arbitrary user-requested XML merely because it contains angle brackets.

## Budgets and selection

Start with a proposed **4,096-byte UTF-8 dynamic-context cap**, including wrappers, excluding
original user content and native media payloads. This is an initial trial limit, not a
measured optimum. Allocate at most 512 bytes to identity/origin, 512 to reply context,
1,024 to current-room observations, 1,024 to memory, 256 to travel and 768 to delimiters and
omission notices. Unused space may be borrowed without exceeding the aggregate cap.

Prioritize the reply being addressed, fresh room evidence, explicit boundaries and unfinished
commitments. Include memory only when it changes the response. Drop travel and redundant
history before losing those facts. Deduplicate by source event identity, not text similarity.
Do not silently truncate the human's current request to make room for injected material.
Record bytes by producer and elapsed lookup time; use the provider tokenizer when available
rather than asserting a fixed byte-to-token conversion.

No model is needed for identity binding, permission checks, byte limits or delivery accounting.
Use the active conversational model for social judgment. Do not add a classifier call to each
passive message. Keep existing bounded admission controls until outcome evidence supports a
change. Keyword reactions must not turn grief, sarcasm or a quoted word into a celebratory
reaction; optional semantic reactions belong to an already-admitted turn, not a new inference
pipeline.

## Cache and privacy contract

Three independent properties must be measured: stable system/tool serialization, reusable
conversation prefixes, and provider-reported cached tokens. Passing one does not prove the
others. Tail-appending a request-only row does not alter the system prompt,
but its position moves after tool results and loses reuse from that point.

Freeze the authorized bundle once at the actual turn boundary. Reuse its bytes for retries
and tool iterations. Place it at the originating user-turn boundary in the wire projection,
not after each newly appended tool result. Preserve the framework's required tool-call/result
adjacency and support both chat `messages` and Responses `input` representations.

The framework's native `api_content` replays safe material on other profiles.
On a speaker-scoped companion profile, no plugin observation uses that channel:
discard `pre_llm_call.context` and bare-string hook results before stringification
or spill, and skip native memory-provider prefetch even if configured.
Server-authored one-shot gateway notices are an explicit, separate exception.
They remain replayable; human conversation history is unchanged.

The private placement path uses a **narrow framework seam**.
`pre_llm_call` results carry `ephemeral_context`; `TurnContext` collects it
once as `ephemeral_user_context`. The framework applies it only to the wire
copy of the current user row in `build_api_messages`, after
replay-safe composition and before transport conversion. Use that function's current-turn
identity/index, not a search for the last `role=user` row. Support copied multimodal text
parts too. Never pass this field to the sidecar stamper, database, summary or export paths.
Expose the server-created turn origin in the same hook payload. If this seam is unavailable,
withhold private context with a compatibility error; never fall back to persistent `context`.

**Zero retained companion observations is an enforced invariant.** On a
Discord turn, gate it on the trusted profile's `optmem.speaker_scoped` setting.
If that config read fails, withhold replayable plugin context and prefetch
rather than assume private-profile authority. Keep public room/travel observations in their
separately bounded state; recheck audience before each request and remove
records on confirmed permission revocation. No dynamic observation enters
historical `api_content`, so there is no retained projection to expire or
replay after reload/compaction. A future retained producer requires a
separate decision, row-owned metadata, audience revalidation, expiry and
eviction checks before it can be enabled. The earlier unimplemented trial
proposed 30-minute expiry and an eight-snapshot/16 KiB trigger retaining
four snapshots/8 KiB; those figures are not active policy.

**Private recall is never replay-safe on a shared profile.** Keep
speaker-specific recall out of durable `api_content`, the human transcript
and shared compaction summaries. Freeze its wire projection for the active
turn; the next speaker gets a newly authorized projection, not the prior
speaker's bytes. Do not trade audience privacy for cache reuse. Unknown
scope means omit, not infer from display names or model-supplied ids.

Use existing verified source/message/turn binding. Reject stale queued bundles, mismatched
senders, canceled turns and cross-profile lookups. Missing optional context may leave ordinary
chat available; missing authorization must withhold the affected private data or action.
Middleware's fail-open exception policy is not an authorization boundary.

## Memory, procedures and continuity

Keep one canonical memory store. Separate conversational memory, disposable room observations
and model-visible request projections by purpose, not by inventing competing memory systems.
Enforce subject and audience on every route: automatic recall, explicit recall, note writes,
whole-store wake, consolidation and session-history search. A model-supplied profile name,
user identifier or mention is a query, never permission. Scope before retrieval and again
before returning a result; preserve corrections and deletion/revocation through replay and
compaction. Legacy unscoped facts stay unavailable to broader audiences until reviewed.

Legacy lines without verified author/audience provenance are quarantined from automatic
and public-tool recall until reviewed. A numeric subject id does not establish who supplied
the fact or where it may be repeated. Preserve the original record; an authorized review
creates a scoped successor. This can reduce historical recall temporarily and must be
reported as unavailable evidence, not fabricated continuity.

Bound public note writes before backend execution: proposed initial limits are two facts
per turn, eight per verified speaker per UTC (Coordinated Universal Time) day and 64 per profile per UTC day. Count
successful unique writes only; an identical event/fact retry is idempotent. Unreviewed
third-party claims cannot cross the asserting speaker's audience. Return a typed budget
refusal, do not retry in the same turn, and never claim a failed note was remembered.

Put universal response/privacy rules in the small stable seat contract. Load occasional
procedures through read-only, allowlisted skill access only after their identity, tool
requirements and memory rules are reconciled. Do not enable a mutable skill-management
surface merely to make the index appear. An enabled skill is not useful until the actual
model can discover and read it on the intended surface.

Keep sentiment and relationship inferences tentative. Retain explicit preferences, boundaries
and commitments with their source. Do not retain every greeting, convert roleplay into
biography, or pressure people to return. Reflection produces bounded proposed corrections,
not automatic persona rewrites, new skills or public posts. Reuse existing audit and memory
owners; no additional always-on reflection agent is required.

Diagnostics use an allowlist of content-free fields before formatting: tool name, status,
sizes, duration and token counters. Do not log clipped arguments, recalled facts or arbitrary
object stringification as though clipping were redaction. Count skill exposure from the
rendered surface, not the configured library. Media-chain readiness must follow configured,
profile-local routes rather than requiring credentials for an unused fallback; readiness
checks must not generate media or claim that credential presence proves provider health.

## Acceptance and rollout

Use observed incidents, hold the model fixed within a comparison, and record silence,
failure and delivered output separately. Required discriminating cases:

| Case | Required result |
|---|---|
| Direct question, greeting, closure and repeated ping | Appropriate response or intentional silence; no adapter-authored presence sentence |
| Silent, screened-empty, media-only and failed-send drafts | Distinct delivery outcomes; no phantom success or duplicate post |
| Tool continuation and retry | Same authorized context bytes at the same turn boundary; stable prior prefix |
| Next speaker, changed audience or missing attestation | No prior private recall; deliberate cache break recorded |
| Malicious topic/name/quote containing closing tags | Still data inside an intact escaped envelope |
| Context cap and multibyte boundary | Total bound holds; delimiters and human request remain intact |
| Session search with another profile or private session identifier | Refused before a protected store is opened |
| Memory write for another person; broad recall; housekeeping read | Denied or limited by trusted subject/audience, not model arguments |
| Restart, compaction and permission revocation | Replay preserves applicable provenance and cannot resurrect revoked facts |
| Two profiles interleaved, including voice/GIF paths | No identity, pending-media or delivery-state crossing |
| Ambient off; private work profile outside selected rooms | Stock behavior and privileged boundaries unchanged |
| Echoed internal envelope across stream chunks | Private payload is never sent; safe unrelated prose survives; no fallback sentence |
| Long session and replay after reload | No injected observation snapshot appears in historical `api_content` or native provider prefetch; original human messages remain |
| Legacy unscoped fact and excessive note writes | No unreviewed disclosure, no quota bypass or false claim of a successful save |
| Diagnostic payload and skill-exposure fixtures | No sensitive fragments or object-stringification bypass; exposure matches the rendered surface |
| Configured media hop available without fallback credentials | Accurate readiness, no generation call and no authorization expansion |

Prove deterministic checks red against the relevant defective behavior before changing it.
Then exercise the actual isolated adapter/request path, not only a copied renderer. Run the
coupled ambient and memory-policy suites after integration. A provider comparison follows in
an authorized canary; record model/rung, prompt and cache tokens, latency, request count,
compaction events and semantic outcome. Do not promise a universal hit-rate improvement.

Deploy coupled producers and consumers together through their existing manifests. Restart,
credential, permission, retention and public-publishing changes require their own approval.
Keep a revision-addressed rollback for each package and profile configuration. If the privacy
boundary fails, stop the canary; do not restore a known unsafe disclosure route as rollback.
