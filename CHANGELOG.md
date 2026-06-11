## [v1.2] - 2026-06-11

- ``Fix`` QuestDB-Dashboards zeigen jetzt wieder die gleichen Nutzer-Metriken wie die InfluxDB-Dashboards, inklusive Home-Automation-Heizung, Call-Log-Details, VPN-Adressfeldern, MyFritz-Hostname und den aktuellen DSL-Download-/Upload-Werten.
- ``Fix`` Fehlende FritzBox-Felder werden in QuestDB vorab angelegt, damit Dashboards nicht mehr mit `Invalid column` abbrechen, nur weil ein optionaler Wert noch nie geschrieben wurde.
- ``Fix`` Der doppelte FritzBox-Quickfilter wurde in den QuestDB-Dashboards entfernt; außerdem sind die Call-Log-Tabellen jetzt wieder vollständig.

- ``New`` **QuestDB support.** QuestDB can now be used as storage backend alongside InfluxDB v1/v2 — configured via `QUESTDB_*` variables and a ready-to-use `docker-compose.questdb.yml`.
- ``New`` The database backend is now selected with a single `DB_TYPE` variable (`influxdb_v1`, `influxdb_v2` or `questdb`). Invalid values are rejected with a clear error instead of silently falling back to InfluxDB v1. `INFLUXDB_VERSION` keeps working but is deprecated.
- ``New`` The database hostname may now be a full URL (e.g. `https://influx.example.com` behind a reverse proxy) — protocol and port are detected automatically. Port `443` always enables TLS.
- ``New`` Credentials and tokens are no longer sent over unencrypted HTTP to remote hosts by default. For trusted home networks this can be allowed explicitly with `INFLUXDB_ALLOW_PLAINTEXT_CREDENTIALS=true` (or `QUESTDB_…`); the bundled compose files set this for their internal network.
- ``New`` Unraid app template with separate, clearly grouped settings for InfluxDB v1/v2 and QuestDB.
- ``New`` Per-database Docker Compose bundles (`docker-compose.influx1.yml`, `docker-compose.influx2.yml`, `docker-compose.questdb.yml`) including database service, healthcheck and log rotation.
- ``Fix`` Configuration errors now stop the container immediately with a clear message instead of triggering pointless restart loops — a broken configuration cannot be fixed by retrying.
- ``Fix`` A configured but empty `QUESTDB_HOSTNAME` no longer forces the daemon into QuestDB mode and no longer breaks InfluxDB setups (affected the Unraid template).
- ``Fix`` FritzOS lab/beta versions with build suffixes (e.g. `7.62-123456`) are now recognised correctly instead of disabling services.
- ``Fix`` The FritzBox Lua client now uses `httpx` like the rest of the application — the implicit dependency on `requests` is gone.
- ``Fix`` Improved error messages: hostname, port and credential problems are reported individually and precisely at startup.
- ``Fix`` Timezone suffix in Fritz!Box time responses (e.g. `+02:00`) no longer causes a log warning during timezone auto-detection.
- ``Fix`` QuestDB column names containing dots (e.g. WLAN `802.11` metrics) are now sanitised automatically — dots are replaced with underscores before writing.
- ``Fix`` Cable-specific services (e.g. cable channel info) are no longer logged as warnings on DSL devices — the message is suppressed after the first discovery pass.
- ``New`` `LOG_LEVEL` environment variable controls log verbosity (`INFO` by default, `DEBUG`, `WARNING` or `ERROR`). Can also be set with the `-l` CLI flag.
- ``New`` FritzBox serial number is now used as the InfluxDB measurement / QuestDB table name with a `fritzbox_` prefix (e.g. `fritzbox_AA1234567890`).



<details markdown="1">
<summary>Previous versions...</summary>

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


## [v1.0] - 2026-06-08
``New`` Initial commit
