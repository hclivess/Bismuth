#!/usr/bin/env bash
#
# bismuth-watchdog.sh — self-healing 24h monitor for the bismuth-node systemd service.
#
# Runs as a transient systemd unit (systemd-run, see below). Every CHECK_EVERY seconds it samples the
# node via the REST API and appends a line to $LOG. If the node is STUCK — height not advancing while it
# is meaningfully BEHIND a real consensus — for STUCK_LIMIT consecutive checks, it restarts the service
# (which re-dials peers fresh and re-runs the startup cross-integrity check), but NEVER more often than
# RESTART_COOLDOWN seconds, so it can't churn the peer cooldowns. Exits after RUN_FOR seconds (24h).
#
# Start it with:
#   systemd-run --unit=bismuth-watchdog --collect \
#     --description="Bismuth node 24h self-healing watchdog" \
#     /root/bismuth-claude/Bismuth/scripts/bismuth-watchdog.sh
# Watch it with:   journalctl -u bismuth-watchdog -f   (or: tail -f /root/bismuth-watchdog.log)
# Stop early with: systemctl stop bismuth-watchdog
#
set -uo pipefail

LOG=/root/bismuth-watchdog.log
API=http://127.0.0.1:5659/api/status
CHECK_EVERY=600          # 10 min
RUN_FOR=86400            # 24 h
STUCK_LIMIT=4            # ~40 min of no progress while behind before acting
BEHIND_MIN=20            # only "stuck" if this far behind consensus
RESTART_COOLDOWN=3600    # at most one restart per hour (avoid peer-cooldown churn)
SERVICE=bismuth-node

_get(){ echo "$1" | python3 -c "import sys,json;print(json.load(sys.stdin).get('$2') or 0)" 2>/dev/null || echo 0; }
_now(){ date -u +%s; }
_ts(){ date -u +%FT%TZ; }

start=$(_now); prevH=0; stuck=0; lastrestart=$(_now)   # seed cooldown from start: ~1h grace before first restart
echo "$(_ts) watchdog START (24h; check ${CHECK_EVERY}s, restart-on-stuck>=${STUCK_LIMIT}, cooldown ${RESTART_COOLDOWN}s)" >> "$LOG"

while [ $(( $(_now) - start )) -lt "$RUN_FOR" ]; do
  R=$(curl -sS --max-time 8 "$API" 2>/dev/null || true)
  H=$(_get "$R" blocks); C=$(_get "$R" consensus); N=$(_get "$R" connections)
  behind=$(( C - H )); st="ok"; action="none"

  if [ -z "$R" ]; then
    stuck=$((stuck+1)); st="down:$stuck"            # API unreachable (or still starting up) -> a stuck tick
  elif [ "$H" -le "$prevH" ] && [ "$behind" -gt "$BEHIND_MIN" ] && [ "$C" -gt 0 ]; then
    stuck=$((stuck+1)); st="stuck:$stuck"
  else
    stuck=0                                         # advancing (or caught up) -> healthy
  fi

  if [ "$stuck" -ge "$STUCK_LIMIT" ] && [ $(( $(_now) - lastrestart )) -gt "$RESTART_COOLDOWN" ]; then
    systemctl restart "$SERVICE" >/dev/null 2>&1 && { action="RESTART"; lastrestart=$(_now); stuck=0; }
  fi

  printf '%s h=%s c=%s behind=%s conns=%s %s action=%s\n' "$(_ts)" "${H:-?}" "${C:-?}" "$behind" "${N:-?}" "$st" "$action" >> "$LOG"
  [ "$H" -gt 0 ] && prevH=$H
  sleep "$CHECK_EVERY"
done

echo "$(_ts) watchdog DONE (24h elapsed)" >> "$LOG"
