#!/usr/bin/env bash
# Rare unprompted "say something" spark for a Hermes chat profile.
#
# Runs on a cron tick, but MOST ticks decide to do nothing and print
# {"wakeAgent": false} — which skips the agent entirely, so a skipped roll
# costs nothing at all: no tokens, no latency. Only when the roll passes do we
# emit context, waking the agent to post one short thing.
#
# Wire it as a cron SCRIPT job (NOT --no-agent), so stdout is injected into the
# agent's prompt:
#   hermes -p <profile> cron create "40 7,13,16 * * *" "<your posting prompt>" \
#       --script spark.sh --name spark --deliver "discord:<channel_id>"
#
# Rarity = tick frequency x SPARK_ODDS. At 3 ticks/day and ODDS=6 that averages
# roughly one spontaneous post every two days.
#
# Config (all optional, via the profile's .env or the job environment):
#   SPARK_ODDS        1-in-N chance a tick actually fires        (default 6)
#   MEMORY_DIR        OptMem memory dir; enables greeting a
#                     remembered person by handle                (default: unset)
#   MEMO_BIN          path to the optmem `memo` binary           (default ~/.optmem/memo)
set -uo pipefail
ODDS="${SPARK_ODDS:-6}"
MEMO="${MEMO_BIN:-${HOME}/.optmem/memo}"

if [ "${ODDS}" -lt 1 ] 2>/dev/null || [ "$(( RANDOM % ODDS ))" -ne 0 ]; then
    echo '{"wakeAgent": false}'
    exit 0
fi

# Pick someone the agent actually remembers, so a greeting carries real history
# instead of being generic noise. Degrades silently to nobody.
PERSON=""
RECALL=""
if [ -n "${MEMORY_DIR:-}" ] && [ -r "${MEMORY_DIR}/LOG.txt" ]; then
    PERSON="$(grep -oE '@[A-Za-z0-9_.]+' "${MEMORY_DIR}/LOG.txt" 2>/dev/null | sort -u | shuf -n1 2>/dev/null || true)"
    if [ -n "${PERSON}" ] && [ -x "${MEMO}" ]; then
        RECALL="$(MEMORY_DIR="${MEMORY_DIR}" "${MEMO}" recall "${PERSON}" 2>/dev/null | head -4 || true)"
    fi
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
