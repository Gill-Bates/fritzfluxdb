## [v1.1] - 2026-06-09

- ``New`` Updated base image to Python 3.13 on Debian Trixie.
- ``New`` All data is now written to a single InfluxDB measurement named after the FritzBox serial number. Replacing a FritzBox automatically creates a new measurement, keeping historical data cleanly separated. The `box` tag remains as a human-readable label.
- ``New`` HTTPS is now used by default without requiring configuration. fritzfluxdb tries HTTPS first (accepting the FritzBox self-signed certificate) and only falls back to plain HTTP if the port is unreachable. A warning is shown when falling back. Set `ssl = true` to enforce HTTPS, or `ssl = false` to always use HTTP without a warning.
- ``New`` The startup banner is now suppressed on watchdog-triggered restarts and only shown once per container start.
- ``Fix`` Metrics with dynamic tags (VPN users, network hosts, smart home devices) now correctly carry their identifying tags in InfluxDB — previously these tags were silently dropped.
- ``Fix`` Boolean metric values are now written as `true`/`false` as required by InfluxDB — previously they were written as Python's `True`/`False` and rejected or misinterpreted.
- ``Fix`` Millisecond timestamp precision was incorrectly truncated; timestamps are now stored with the correct precision.
- ``Fix`` FritzBox log entries now include timezone information, preventing timestamp mismatches in Grafana for non-UTC setups.
- ``Fix`` Metrics with integer values outside the signed 64-bit range (e.g. after an AVM byte counter glitch) are now silently dropped instead of causing a write error.
- ``Fix`` Background task failures now result in a non-zero exit code, allowing the watchdog or container orchestrator to detect and restart the process. Previously a failed background worker would be silently ignored.
- ``Fix`` On graceful shutdown, producer tasks are stopped first, the measurement queue is drained, and the InfluxDB writer is stopped last — reducing the risk of data loss on container stop.
- ``Fix`` Connections to FritzBox and InfluxDB are now properly closed on shutdown in all error scenarios.
- ``Fix`` Configuration secrets (passwords, tokens) are now masked in log output even when part of a longer key name (e.g. `influxdb_password`, `api_token`).
- ``Fix`` Invalid port numbers and missing credentials in the configuration now produce a clear error on startup instead of a confusing runtime failure.
- ``Fix`` Parsing of malformed or unexpected responses from FritzBox (JSON, XML, call logs) now produces descriptive error messages instead of silent failures.
- ``Fix`` When InfluxDB is unavailable, only a single error is logged at the moment of the outage. Subsequent retries are silent. Once the connection is restored, one info message confirms recovery and reports how many buffered measurements are being flushed.
- ``Fix`` InfluxDB connection errors no longer produce Python stack traces in the log output.


<details markdown="1">
<summary>Previous versions...</summary>

## [v1.0] - 2026-06-08
``New`` Initial commit