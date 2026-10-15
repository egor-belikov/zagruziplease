#!/usr/bin/env bash
# Хостовый watchdog: API, зависший бот. Запускать из cron каждые 5 минут.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.fantasyvps.yml)
BOT_CONTAINER="zagruziplease_bot"
API_CONTAINER="zagruziplease_api"
LOG_TAG="zagruziplease-healthcheck"
STUCK_RESTART_MINUTES=15

log() { logger -t "$LOG_TAG" "$*"; printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

api_ok() {
  curl -sf --max-time 5 http://127.0.0.1:1984/status >/dev/null 2>&1
}

bot_stuck_since() {
  # `docker logs` keeps lines across `docker restart` (same container, log file
  # isn't rotated), so a plain "--since ${STUCK_RESTART_MINUTES}m" wall-clock
  # window kept matching the *previous* incident's log lines for up to
  # STUCK_RESTART_MINUTES after we already restarted for it — the watchdog
  # then restarted the bot again on every following 5-minute tick even though
  # it had recovered right after the first restart. Bound the window to the
  # container's own last start time so old, already-handled lines drop out.
  local window_epoch started_epoch
  window_epoch=$(( $(date +%s) - STUCK_RESTART_MINUTES * 60 ))
  started_epoch="$(date -d "$(docker inspect "$BOT_CONTAINER" --format='{{.State.StartedAt}}' 2>/dev/null)" +%s 2>/dev/null || echo 0)"
  if [[ "$started_epoch" -gt "$window_epoch" ]]; then
    echo "$started_epoch"
  else
    echo "$window_epoch"
  fi
}

bot_stuck_in_restart() {
  local since count
  since="$(bot_stuck_since)"
  count="$(docker logs "$BOT_CONTAINER" --since "$since" 2>&1 \
    | grep -c 'Session.restart()' || true)"
  [[ "$count" -ge 5 ]] && return 0
  local last_activity
  last_activity="$(docker logs "$BOT_CONTAINER" 2>&1 \
    | grep -v DbCleanup \
    | grep -E 'on_message|VideoUploadTask|Starting \"|Connection lost' \
    | tail -1 || true)"
  if [[ -z "$last_activity" ]]; then
    return 1
  fi
  if docker logs "$BOT_CONTAINER" --since "$since" 2>&1 \
    | grep -q 'Connection lost'; then
    return 0
  fi
  return 1
}

api_child_death_storm() {
  local count
  count="$(docker logs "$API_CONTAINER" --since 5m 2>&1 | grep -c 'Child process.*died' || true)"
  [[ "$count" -ge 10 ]]
}

log "healthcheck start"

if api_child_death_storm || ! api_ok; then
  log "API unhealthy — restarting yt_api (API_WORKERS=1)"
  "${COMPOSE[@]}" up -d yt_api
  sleep 8
  if ! api_ok; then
    log "API still down after restart"
    docker restart "$API_CONTAINER" || true
  fi
fi

if bot_stuck_in_restart; then
  log "bot stuck (Session.restart / connection lost) — restarting yt_bot"
  docker restart "$BOT_CONTAINER" || true
fi

log "healthcheck done"
