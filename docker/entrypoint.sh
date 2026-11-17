#!/usr/bin/env bash
#
# docker/entrypoint.sh
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# =============================================================================
# fritzFlux - Watchdog Entrypoint
# =============================================================================
# Runs the fritzFlux daemon and keeps it alive: a crash (non-zero exit) is
# retried with exponential back-off, up to WATCHDOG_MAX_RESTARTS consecutive
# failures. After that the watchdog exits with the daemon's status so the
# container orchestrator (compose `restart: unless-stopped`) takes over -- the failure
# becomes visible instead of being hidden in an endless internal loop.
#
# A clean exit (0) or a normal signal-termination (130 SIGINT / 143 SIGTERM)
# stops the watchdog so container shutdown does not restart the daemon.
# Exit 78 (EX_CONFIG) signals an invalid configuration: restarting cannot fix
# it, so the watchdog parks: it logs once, removes the PID file (the healthcheck
# turns unhealthy) and idles without restarting the daemon. It stays alive so
# the restart policy does not loop; SIGTERM/SIGINT end it at once. After fixing
# the configuration, restart the container explicitly. `fritzflux-cli drop-all`
# and `drop-box` park the watchdog the same way through STOP_FLAG.
#
# The daemon's PID is kept in PID_FILE while it runs; the Compose healthchecks
# probe that PID, so a dead daemon is reported even while the watchdog lives on.
#
# The launched command defaults to the bundled daemon but can be overridden via
# the image CMD or `docker run <image> <cmd...>` / compose `command:`.
#
# Tunables (env, all positive integers):
#   WATCHDOG_RESTART_DELAY        initial back-off seconds              (def 10)
#   WATCHDOG_MAX_RESTART_DELAY    back-off cap seconds                  (def 300)
#   WATCHDOG_MAX_RESTARTS         consecutive crashes before giving up  (def 10)
#   WATCHDOG_BACKOFF_RESET_AFTER  stable runtime (s) that resets back-off (def 3600)
#   WATCHDOG_SHUTDOWN_TIMEOUT     grace seconds before SIGKILL on stop  (def 35)
# =============================================================================

set -eu

# --- helpers ---------------------------------------------------------------
log() {
    # ISO-8601 UTC timestamp so it lines up with the daemon's own logging.
    printf '%s [watchdog] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

is_positive_int() {
    case "$1" in
        ''|*[!0-9]*) return 1 ;;   # empty or contains a non-digit
    esac
    # numeric test, so 0, 00 and 000 are rejected as well
    [ "$1" -gt 0 ] 2>/dev/null
}

require_positive_int() {
    # $1 = variable name (for the message), $2 = value
    if ! is_positive_int "$2"; then
        printf 'Invalid %s: "%s" (expected positive integer)\n' "$1" "$2" >&2
        exit 2
    fi
}

require_positive_int_max() {
    # $1 = variable name, $2 = value, $3 = max
    require_positive_int "$1" "$2"
    if [ "$2" -gt "$3" ]; then
        printf 'Invalid %s: "%s" (expected positive integer <= %s)\n' "$1" "$2" "$3" >&2
        exit 2
    fi
}

# --- configuration & validation --------------------------------------------
RESTART_DELAY="${WATCHDOG_RESTART_DELAY:-10}"
MAX_RESTART_DELAY="${WATCHDOG_MAX_RESTART_DELAY:-300}"
MAX_RESTARTS="${WATCHDOG_MAX_RESTARTS:-10}"
BACKOFF_RESET_AFTER="${WATCHDOG_BACKOFF_RESET_AFTER:-3600}"
SHUTDOWN_TIMEOUT="${WATCHDOG_SHUTDOWN_TIMEOUT:-35}"
# The FRITZFLUXDB_* overrides exist so tests can isolate these files; the app and Compose use the defaults.
PID_FILE="${FRITZFLUXDB_PID_FILE:-/tmp/fritzfluxdb.pid}"
# Created by fritzflux-cli before it stops the daemon for a drop: park instead of
# exiting, so neither the watchdog nor the restart policy re-creates the tables.
STOP_FLAG="${FRITZFLUXDB_STOP_FLAG:-/tmp/fritzfluxdb.stop}"

require_positive_int_max WATCHDOG_RESTART_DELAY      "${RESTART_DELAY}"      3600
require_positive_int_max WATCHDOG_MAX_RESTART_DELAY  "${MAX_RESTART_DELAY}"  86400
require_positive_int_max WATCHDOG_MAX_RESTARTS        "${MAX_RESTARTS}"       1000
require_positive_int_max WATCHDOG_BACKOFF_RESET_AFTER "${BACKOFF_RESET_AFTER}" 604800
require_positive_int_max WATCHDOG_SHUTDOWN_TIMEOUT   "${SHUTDOWN_TIMEOUT}"   300

# Normalise leading zeros (010 -> 10); the arithmetic below would read them as octal.
RESTART_DELAY=$((10#${RESTART_DELAY}))
MAX_RESTART_DELAY=$((10#${MAX_RESTART_DELAY}))
MAX_RESTARTS=$((10#${MAX_RESTARTS}))
BACKOFF_RESET_AFTER=$((10#${BACKOFF_RESET_AFTER}))
SHUTDOWN_TIMEOUT=$((10#${SHUTDOWN_TIMEOUT}))

# Keep the initial back-off within the cap.
if [ "${RESTART_DELAY}" -gt "${MAX_RESTART_DELAY}" ]; then
    RESTART_DELAY="${MAX_RESTART_DELAY}"
fi

delay="${RESTART_DELAY}"
restart_count=0
child_pid=""
terminating=0

# --- default command (overridable via CMD / docker args) -------------------
# Started as a module: the app package is installed into site-packages.
if [ "$#" -eq 0 ]; then
    set -- python -m app.main
fi

# --- graceful shutdown -----------------------------------------------------
# Forward the signal, then wait up to SHUTDOWN_TIMEOUT seconds for the daemon
# to stop on its own before escalating to SIGKILL.
forward_signal() {
    terminating=1
    if [ -n "${child_pid}" ] && kill -0 "${child_pid}" 2>/dev/null; then
        log "received shutdown signal, forwarding SIGTERM to daemon process group (pid ${child_pid})"
        kill -TERM "-${child_pid}" 2>/dev/null || true

        remaining="${SHUTDOWN_TIMEOUT}"
        while kill -0 "${child_pid}" 2>/dev/null && [ "${remaining}" -gt 0 ]; do
            sleep 1
            remaining=$((remaining - 1))
        done

        if kill -0 "${child_pid}" 2>/dev/null; then
            log "daemon did not stop within ${SHUTDOWN_TIMEOUT}s, sending SIGKILL to process group"
            kill -KILL "-${child_pid}" 2>/dev/null || true
        fi

        wait "${child_pid}" 2>/dev/null || true
    fi
    exit 0
}

# The daemon treats SIGHUP, SIGTERM and SIGINT as shutdown signals -> mirror that.
trap forward_signal TERM INT HUP
parked_pid=""
cleanup() {
    if [ -n "${parked_pid}" ]; then
        kill "${parked_pid}" 2>/dev/null || true
    fi
    rm -f "${PID_FILE}" "${STOP_FLAG}"
}
trap cleanup EXIT
# Idle without restarting the daemon; a backgrounded sleep + wait lets a
# trapped signal interrupt the idle state immediately.
park() {
    log "$1"
    while true; do
        sleep 86400 &
        parked_pid=$!
        wait "${parked_pid}" 2>/dev/null || true
    done
}

log "starting fritzFlux watchdog (delay=${RESTART_DELAY}s cap=${MAX_RESTART_DELAY}s max_restarts=${MAX_RESTARTS})"
log "command: $1"

while true; do
    log "launching daemon"
    started_at="$(date +%s)"

    # Suppress the startup banner on restarts so the log is not cluttered.
    # Unset when restart_count was reset (stable run) so the banner reappears.
    if [ "${restart_count}" -gt 0 ]; then
        export FRITZFLUXDB_SKIP_BANNER=1
    else
        unset FRITZFLUXDB_SKIP_BANNER
    fi

    # Run in a separate session so shutdown can target the whole process group.
    setsid "$@" &
    child_pid=$!
    printf '%s\n' "${child_pid}" > "${PID_FILE}"

    # `wait` returns when the child exits OR a trapped signal fires.
    set +e
    wait "${child_pid}"
    status=$?
    set -e
    rm -f "${PID_FILE}"
    child_pid=""

    # Shutdown was requested via signal -> the trap already handled exit.
    if [ "${terminating}" -eq 1 ]; then
        log "watchdog stopping"
        exit 0
    fi

    # The daemon handles SIGTERM itself and usually exits 0, so the status cannot tell.
    if [ -e "${STOP_FLAG}" ]; then
        rm -f "${STOP_FLAG}"
        park "daemon stopped by fritzflux-cli; parked, not restarting. Restart the container to start it again"
    fi
    # Treat a normal exit and signal-terminations (SIGINT/SIGTERM) as clean,
    # so an externally stopped daemon is not misread as a crash.
    case "${status}" in
        0|129|130|143)
            log "daemon exited cleanly (status ${status}), stopping watchdog"
            exit 0
            ;;
        78)
            # EX_CONFIG: retrying cannot succeed; park instead of exiting so the
            # restart policy does not loop. A backgrounded sleep + wait lets a
            # trapped signal interrupt the idle state immediately.
            park "daemon reported a permanent configuration error (status 78); parked, not restarting. Fix the configuration and restart the container explicitly"
            ;;
    esac

    # If the daemon ran stably for a while, the previous failures are unlikely
    # related -> reset the back-off and failure counter for fast recovery.
    ended_at="$(date +%s)"
    runtime=$((ended_at - started_at))
    if [ "${runtime}" -ge "${BACKOFF_RESET_AFTER}" ]; then
        log "daemon ran ${runtime}s before failing; resetting back-off and counter"
        delay="${RESTART_DELAY}"
        restart_count=0
        unset FRITZFLUXDB_SKIP_BANNER
    fi

    restart_count=$((restart_count + 1))
    if [ "${restart_count}" -ge "${MAX_RESTARTS}" ]; then
        log "daemon failed ${restart_count} times in a row (last status ${status}); giving up so the orchestrator can take over"
        exit "${status}"
    fi

    log "daemon exited with status ${status}; restart ${restart_count}/${MAX_RESTARTS} in ${delay}s"
    # Backgrounded sleep + wait lets a trapped signal cut the back-off short.
    sleep "${delay}" &
    parked_pid=$!
    wait "${parked_pid}" 2>/dev/null || true
    parked_pid=""

    # Exponential back-off, capped at MAX_RESTART_DELAY.
    delay=$((delay * 2))
    if [ "${delay}" -gt "${MAX_RESTART_DELAY}" ]; then
        delay="${MAX_RESTART_DELAY}"
    fi
done
