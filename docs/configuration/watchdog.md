# Docker watchdog

The image entrypoint (`docker/entrypoint.sh`) supervises the daemon. Its reaction depends on the
daemon's exit status:

| Daemon exit status | Watchdog reaction |
| --- | --- |
| `0`, `129`, `130`, `143` | Clean exit or signal termination: the watchdog stops with status `0`. |
| `78` | Permanent configuration error: the watchdog [parks](#parked-state-after-a-configuration-error). |
| Any other non-zero | Restart with exponential backoff until `WATCHDOG_MAX_RESTARTS` consecutive failures; then the watchdog exits with the last status so Compose or another orchestrator applies its restart policy. |

## Settings

| Variable | Default | Valid range / meaning |
| --- | --- | --- |
| `WATCHDOG_RESTART_DELAY` | `10` | Initial delay in seconds; max `3600`. |
| `WATCHDOG_MAX_RESTART_DELAY` | `300` | Backoff cap in seconds; max `86400`. |
| `WATCHDOG_MAX_RESTARTS` | `10` | Consecutive failures before giving up; max `1000`. |
| `WATCHDOG_BACKOFF_RESET_AFTER` | `3600` | Stable runtime in seconds before counters reset; max `604800`. |
| `WATCHDOG_SHUTDOWN_TIMEOUT` | `35` | Grace period before SIGKILL; max `300`. |

All values must be positive integers (`0`, `00`, ... are rejected; leading zeros are read as
decimal). The daemon needs up to 25 seconds to shut down (5 s for in-flight FRITZ!Box requests, 10 s to
drain the queue, 10 s for the final database flush) plus a 5 second reserve, so keep the shutdown
timeout above 30 and the Compose `stop_grace_period` (40 s) above the shutdown timeout. If the initial delay
exceeds the cap, the cap is used.

Python cannot cancel a running worker thread. FRITZ!Box requests check an elapsed-time budget
of at most 10 seconds (the lower of four times `connect_timeout` and 10 s) while reading the
response, so continuously trickling bodies eventually time out. A read already in progress can
add its remaining read timeout; discovery or login can also involve several sequential requests.
The watchdog owns the hard process deadline and sends SIGKILL when it expires. The daemon uses
neither `os._exit` nor a replacement executor; a source run without the watchdog has no hard
process-exit guarantee for permanently stuck I/O.

## Parked state after a configuration error

Exit status `78` means the configuration is invalid, so a restart cannot help. This includes
credentials or permissions rejected by the FRITZ!Box or the database, a missing InfluxDB database,
organization or bucket, and TR-064 access switched off in the FRITZ!Box (see
[Startup preflight](../configuration/data-model.md#startup-preflight)). The watchdog logs
the error once, removes the daemon PID file and idles without using CPU. The retry counter and
backoff do not apply. The watchdog stays alive on purpose: if it exited, `restart: unless-stopped`
would restart the container in a loop.

| Aspect | Behaviour |
| --- | --- |
| Container state | Running; the Compose healthcheck probes the PID file, so it reports `unhealthy`. |
| `docker stop`, `SIGTERM`, `SIGINT` | End the parked watchdog immediately with status `0`. |
| Corrected `.env` | Not applied automatically. Restart explicitly, for example `docker compose restart fritzfluxdb`. |
