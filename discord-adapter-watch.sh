#!/usr/bin/env bash
# Upstream-drift watcher for the discord-ambient plugin.
#
# The plugin subclasses the bundled Discord adapter and depends on the upstream
# symbols listed in its module docstring (UPSTREAM COUPLING). Beyond bare
# signatures, bot-bounce counting depends on two upstream BEHAVIOURS:
#   * every live reply is sent with reply_to=<inbound message id>
#     (base.py _reply_anchor_for_event returns event.message_id for Discord)
#   * send() returns ONE SendResult per call, even when chunk-splitting
# If `hermes update` changes any of these, the ambient gate or the bounce
# counter can silently stop working (counts stop accruing -> the breaker never
# trips -> runaway volleys return with no other signal). This hashes those
# regions monthly and speaks ONLY when they move — empty stdout = silent tick.
set -uo pipefail
HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
A="${HERMES_ADAPTER:-${HERMES_HOME}/hermes-agent/plugins/platforms/discord/adapter.py}"
B="${HERMES_BASE:-${HERMES_HOME}/hermes-agent/gateway/platforms/base.py}"
STATE="${HERMES_HOME}/state/discord-adapter-watch.state"
mkdir -p "$(dirname "${STATE}")"

[ -r "${A}" ] || { echo "discord-adapter-watch: bundled adapter not found at ${A} — the ambient plugin is probably broken."; exit 0; }
[ -r "${B}" ] || { echo "discord-adapter-watch: gateway base not found at ${B} — the ambient plugin is probably broken."; exit 0; }

# Admission gate body (mention/auth/dedup seam the ambient re-dispatch relies on).
sig_admission="$(awk '/def _discord_message_admission/,/^    def _dispatch_discord_message/' "${A}" | sha256sum | cut -c1-16)"
# Whole send() method body: guards BOTH the signature (reply_to param) and the
# one-SendResult-per-call chunking contract that bounce counting assumes.
sig_send="$(awk '/^    async def send\(/{f=1; print; next} f{ if (/^    async def /) exit; print }' "${A}" | sha256sum | cut -c1-16)"
# Reply anchor: Discord replies must keep carrying reply_to=event.message_id.
sig_anchor="$(awk '/^def _reply_anchor_for_event/{f=1; print; next} f{ if (/^def /) exit; print }' "${B}" | sha256sum | cut -c1-16)"
# Backfill dispatcher: the plugin overrides it to keep the bounce gate on the
# recovered-message path; if its shape moves, re-check the override.
sig_recovered="$(awk '/^    async def _dispatch_recovered_message/{f=1; print; next} f{ if (/^    async def /) exit; print }' "${A}" | sha256sum | cut -c1-16)"
# Reply routing identity (reply goes to the message's effective channel).
sig_chatid="$(grep -c "chat_id=str(effective_channel.id)" "${A}")"
sig_dedup="$(grep -c "_dedup\.\(contains\|is_duplicate\)" "${A}")"
# Standby busy-probe couplings (all optional in the plugin — it degrades to
# "not busy" if they vanish — but a silent vanish means standby silently stops
# deferring, so the drift still deserves a ping).
R="${HERMES_RUN:-${HERMES_HOME}/hermes-agent/gateway/run.py}"
C="${HERMES_CRON:-${HERMES_HOME}/hermes-agent/cron/scheduler.py}"
# NB: grep -c prints its count even when it exits 1 (zero matches) — never
# chain `|| echo 0` onto it or a legitimate 0 captures as "0\n0" and corrupts
# the state line.
if [ -r "${R}" ]; then sig_runnerstamp="$(grep -c "adapter.gateway_runner = self" "${R}")"; else sig_runnerstamp=0; fi
sig_runnerdecl="$(grep -c "gateway_runner" "${B}")"
if [ -r "${R}" ]; then sig_turnts="$(grep -c "_running_agents_ts" "${R}")"; else sig_turnts=0; fi
if [ -r "${C}" ]; then sig_cronids="$(grep -A6 "def get_running_job_ids" "${C}" | sha256sum | cut -c1-16)"; else sig_cronids=none; fi
ver="$(cd "${HERMES_HOME}/hermes-agent" 2>/dev/null && git describe --tags --always 2>/dev/null || echo unknown)"
now="admission=${sig_admission} send=${sig_send} anchor=${sig_anchor} recovered=${sig_recovered} chatid_sites=${sig_chatid} dedup_calls=${sig_dedup} runner_stamp=${sig_runnerstamp} runner_decl=${sig_runnerdecl} turn_ts=${sig_turnts} cronids=${sig_cronids}"

prev=""
[ -f "${STATE}" ] && prev="$(head -1 "${STATE}")"
printf '%s\nhermes=%s checked=%s\n' "${now}" "${ver}" "$(date -Iseconds)" > "${STATE}"

if [ -z "${prev}" ]; then
    exit 0   # first run: record baseline, say nothing
fi
if [ "${now}" != "${prev}" ]; then
    echo "⚠ discord-ambient plugin: upstream adapter changed (hermes ${ver})."
    echo "  was: ${prev}"
    echo "  now: ${now}"
    echo "  Re-check ${HERMES_HOME}/plugins/discord-ambient/__init__.py against the bundled adapter before the next hermes update."
fi
exit 0
