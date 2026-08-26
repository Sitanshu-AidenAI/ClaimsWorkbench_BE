#!/usr/bin/env bash
#
# The whole backend, from one command: API, Celery worker, Celery beat.
#
# It exists because the three are not optional relative to each other. Mailbox
# intake is a beat schedule executed by a worker; an API running without them
# looks completely healthy and collects nothing, which is exactly how a mailbox
# once went twenty-two hours without being read. Starting them by hand in three
# terminals makes "did I restart everything?" a question you have to remember to
# ask, and the answer is invisible when it is wrong.
#
# Ctrl-C stops all three. Restarting the backend is therefore one gesture, which
# is the point: settings are read once per process, so a `.env` edit that reaches
# the API but not the worker is a split-brain that takes hours to spot.
#
# Usage:  make dev   (or ./scripts/dev.sh)

set -euo pipefail
set -m  # job control, so each child becomes its own process group we can signal

cd "$(dirname "$0")/.."

RUN="${RUN:-uv run}"
CELERY_APP="app.workers.celery_app.celery_app"
CONCURRENCY="${CWB_DEV_CONCURRENCY:-4}"
BEAT_SCHEDULE="${CWB_DEV_BEAT_SCHEDULE:-.dev/celerybeat-schedule}"

c_reset=$'\033[0m'; c_api=$'\033[36m'; c_worker=$'\033[35m'; c_beat=$'\033[33m'; c_warn=$'\033[31m'

pgids=()

cleanup() {
    trap - INT TERM EXIT
    printf '\n%s[dev]%s stopping\n' "$c_warn" "$c_reset"
    for pgid in "${pgids[@]:-}"; do
        [ -n "$pgid" ] || continue
        kill -TERM "-$pgid" 2>/dev/null || kill -TERM "$pgid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

start() {
    local name=$1 colour=$2; shift 2
    ( "$@" 2>&1 | sed -u "s/^/${colour}[${name}]${c_reset} /" ) &
    pgids+=("$!")
}

# --- Preflight ------------------------------------------------------------
# Postgres and Redis are not optional either, and a worker that cannot reach
# Redis fails in a way that reads like a Celery problem rather than a missing
# container.
if ! docker compose ps --status running --services 2>/dev/null | grep -qx redis; then
    printf '%s[dev]%s redis is not running — start the stack first:  make up\n' "$c_warn" "$c_reset"
    exit 1
fi

# Applied before anything boots, and deliberately not left to the developer. A
# worker holds the ORM mapping it imported, so a process started against an
# out-of-date schema fails every task on a column that does or does not exist —
# which is a database error that looks nothing like the migration you forgot.
printf '%s[dev]%s applying migrations\n' "$c_api" "$c_reset"
$RUN alembic upgrade head

mkdir -p "$(dirname "$BEAT_SCHEDULE")"
# The schedule database is state from the *previous* run. Beat keeps each entry's
# last-run time in it, and a stale file is one of the few ways a correctly
# configured schedule still does not fire.
rm -f "$BEAT_SCHEDULE" "$BEAT_SCHEDULE"-*

start api    "$c_api"    $RUN python main.py
start worker "$c_worker" $RUN celery -A "$CELERY_APP" worker --loglevel=info --concurrency="$CONCURRENCY"
start beat   "$c_beat"   $RUN celery -A "$CELERY_APP" beat --loglevel=info --schedule="$BEAT_SCHEDULE"

printf '%s[dev]%s api + worker + beat running — Ctrl-C stops all three\n' "$c_api" "$c_reset"
wait
