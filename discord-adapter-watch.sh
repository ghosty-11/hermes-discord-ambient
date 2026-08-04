#!/usr/bin/env bash
# Upstream-drift watcher for the discord-ambient plugin.
#
# The plugin subclasses the bundled Discord adapter and depends on exactly
# three upstream symbols. If `hermes update` changes any of them, the ambient
# gate can silently stop working (or worse, mis-admit). This hashes those
# symbols monthly and speaks ONLY when they move — empty stdout = silent tick.
set -uo pipefail
HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
A="${HERMES_ADAPTER:-${HERMES_HOME}/hermes-agent/plugins/platforms/discord/adapter.py}"
STATE="${HERMES_HOME}/state/discord-adapter-watch.state"
mkdir -p "$(dirname "${STATE}")"

[ -r "${A}" ] || { echo "discord-adapter-watch: bundled adapter not found at ${A} — the ambient plugin is probably broken."; exit 0; }

# Hash the admission gate body, the send signature line, and the dedup API use.
sig_admission="$(awk '/def _discord_message_admission/,/^    def _dispatch_discord_message/' "${A}" | sha256sum | cut -c1-16)"
sig_send="$(grep -n "async def send" "${A}" | head -1 | sha256sum | cut -c1-16)"
sig_dedup="$(grep -c "_dedup\.\(contains\|is_duplicate\)" "${A}")"
ver="$(cd "${HERMES_HOME}/hermes-agent" 2>/dev/null && git describe --tags --always 2>/dev/null || echo unknown)"
now="admission=${sig_admission} send=${sig_send} dedup_calls=${sig_dedup}"

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
