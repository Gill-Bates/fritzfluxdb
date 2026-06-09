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
# container orchestrator (compose `restart: always`) takes over -- the failure
# becomes visible instead of being hidden in an endless internal loop.
#
# A clean exit (0) or a normal signal-termination (130 SIGINT / 143 SIGTERM)
# stops the watchdog so container shutdown does not restart the daemon.
#
# The launched command defaults to the bundled daemon but can be overridden via
# the image CMD or `docker run <image> <cmd...>` / compose `command:`.
#
# Tunables (env, all positive integers):
#   WATCHDOG_RESTART_DELAY        initial back-off seconds              (def 10)
#   WATCHDOG_MAX_RESTART_DELAY    back-off cap seconds                  (def 300)
#   WATCHDOG_MAX_RESTARTS         consecutive crashes before giving up  (def 10)
#   WATCHDOG_BACKOFF_RESET_AFTER  stable runtime (s) that resets back-off (def 3600)
#   WATCHDOG_SHUTDOWN_TIMEOUT     grace seconds before SIGKILL on stop  (def 15)
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
        0) return 1 ;;             # zero is not allowed
        *) return 0 ;;
    esac
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
SHUTDOWN_TIMEOUT="${WATCHDOG_SHUTDOWN_TIMEOUT:-15}"

require_positive_int_max WATCHDOG_RESTART_DELAY      "${RESTART_DELAY}"      3600
require_positive_int_max WATCHDOG_MAX_RESTART_DELAY  "${MAX_RESTART_DELAY}"  86400
require_positive_int_max WATCHDOG_MAX_RESTARTS        "${MAX_RESTARTS}"       1000
require_positive_int_max WATCHDOG_BACKOFF_RESET_AFTER "${BACKOFF_RESET_AFTER}" 604800
require_positive_int_max WATCHDOG_SHUTDOWN_TIMEOUT   "${SHUTDOWN_TIMEOUT}"   300

# Keep the initial back-off within the cap.
if [ "${RESTART_DELAY}" -gt "${MAX_RESTART_DELAY}" ]; then
    RESTART_DELAY="${MAX_RESTART_DELAY}"
fi

delay="${RESTART_DELAY}"
restart_count=0
child_pid=""
terminating=0

# --- default command (overridable via CMD / docker args) -------------------
# NOTE: launched via run.py (not an installed console-script) because the
# project is intentionally run from source -- see Dockerfile rationale.
if [ "$#" -eq 0 ]; then
    set -- python /app/run.py -d
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

    # `wait` returns when the child exits OR a trapped signal fires.
    set +e
    wait "${child_pid}"
    status=$?
    set -e
    child_pid=""

    # Shutdown was requested via signal -> the trap already handled exit.
    if [ "${terminating}" -eq 1 ]; then
        log "watchdog stopping"
        exit 0
    fi

    # Treat a normal exit and signal-terminations (SIGINT/SIGTERM) as clean,
    # so an externally stopped daemon is not misread as a crash.
    case "${status}" in
        0|129|130|143)
            log "daemon exited cleanly (status ${status}), stopping watchdog"
            exit 0
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
    sleep "${delay}"

    # Exponential back-off, capped at MAX_RESTART_DELAY.
    delay=$((delay * 2))
    if [ "${delay}" -gt "${MAX_RESTART_DELAY}" ]; then
        delay="${MAX_RESTART_DELAY}"
    fi
done
