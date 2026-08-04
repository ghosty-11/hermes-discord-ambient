#!/usr/bin/env bash
# Companion's rare unprompted spark.
#
# Runs on a cron tick, but MOST ticks decide to do nothing and print
# {"wakeAgent": false} — which skips the agent entirely, so a skipped roll
# costs zero inference — no tokens, no latency. Only when the roll passes do
# we emit context, waking her to post one short thing.
#
# Rarity = tick frequency x ODDS. At 4 ticks/day and ODDS=6 that averages
# ~1 spontaneous post every ~1.5 days.
set -uo pipefail
ODDS="${COMPANION_SPARK_ODDS:-6}"
MEM=/var/lib/hermes/companion-memory/memory
MEMO=/var/lib/hermes/.optmem/memo

if [ "$(( RANDOM % ODDS ))" -ne 0 ]; then
    echo '{"wakeAgent": false}'
    exit 0
fi

# Pick someone she actually remembers, so the greeting has real history behind
# it rather than being generic noise. Silently degrades to nobody.
PERSON=""
if [ -r "${MEM}/LOG.txt" ]; then
    PERSON="$(grep -oE '@[A-Za-z0-9_.]+' "${MEM}/LOG.txt" 2>/dev/null | sort -u | shuf -n1 2>/dev/null || true)"
fi

RECALL=""
if [ -n "${PERSON}" ]; then
    RECALL="$(MEMORY_DIR="${MEM}" "${MEMO}" recall "${PERSON}" 2>/dev/null | head -4 || true)"
fi

echo "[spark] The room has been quiet and you feel like saying something."
if [ -n "${PERSON}" ]; then
    echo "[spark] Someone you know: ${PERSON}"
    [ -n "${RECALL}" ] && { echo "[spark] What you remember about them:"; printf '%s\n' "${RECALL}"; }
    echo "[spark] Optionally greet or tease them by name — only if it lands naturally."
else
    echo "[spark] You don't have anyone in mind; just post one fun thought of your own."
fi
exit 0
