# Docker watchdog

The image entrypoint supervises the daemon and restarts unexpected failures with exponential
backoff. A clean exit, normal signal termination, and configuration error stop supervision.

| Variable | Default | Valid range / meaning |
| --- | --- | --- |
| `WATCHDOG_RESTART_DELAY` | `10` | Initial delay in seconds; max `3600`. |
| `WATCHDOG_MAX_RESTART_DELAY` | `300` | Backoff cap in seconds; max `86400`. |
| `WATCHDOG_MAX_RESTARTS` | `10` | Consecutive failures before giving up; max `1000`. |
| `WATCHDOG_BACKOFF_RESET_AFTER` | `3600` | Stable runtime in seconds before counters reset; max `604800`. |
| `WATCHDOG_SHUTDOWN_TIMEOUT` | `15` | Grace period before SIGKILL; max `300`. |

All values must be positive integers. If the initial delay exceeds the cap, the cap is used.
After the maximum consecutive failures, the last status is returned so Compose or another
orchestrator can apply its restart policy.
