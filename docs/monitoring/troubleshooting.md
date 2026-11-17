# Troubleshooting

## Container is unhealthy or stops

Inspect the logs first (use the Compose file of your backend):

```bash
docker compose -f docker/docker-compose.questdb.yml logs --tail=200 fritzfluxdb
```

| Symptom | Cause | Action |
| --- | --- | --- |
| Running, `unhealthy`, log says `permanent configuration error` | The daemon exited with status `78`; the watchdog is [parked](../configuration/watchdog.md#parked-state-after-a-configuration-error). The log line above names the offending option, or a configuration file that cannot be read; see [Validation](../configuration/environment.md#validation). | Fix `.env`, then `docker compose restart fritzfluxdb`. |
| Log says `rejected the configured credentials`, `not found`, or `TR-064 access is disabled` | Startup preflight: the FRITZ!Box or database rejected the credentials, the InfluxDB database, organization or bucket is missing, or TR-064 is switched off. See [Startup preflight](../configuration/data-model.md#startup-preflight). | Correct the setting (during operation, the FRITZ!Box rejections apply after 3 consecutive rejected login attempts, also when the password was changed and every TR-064 action answers `401`; a `401` on only some actions, or on all of them while the login is accepted, means the user lacks the right for them and never stops the daemon), or enable **Home Network > Network > Network Settings > Allow access for applications** in the FRITZ!Box, then restart the container. |
| Exits after repeated restarts, with the status of the last daemon failure (usually `1`) | The daemon failed `WATCHDOG_MAX_RESTARTS` times in a row. | Read the last error in the log and fix its cause. |

## No measurements arrive

Check the following in order:

1. The FRITZ!Box host and credentials are correct and reachable from the container.
2. The configured database host resolves from the container. In the bundled QuestDB example it is
   `questdb`; the InfluxDB Compose examples require an existing endpoint and network connection.
3. The database, bucket, organization, or table permissions allow writes.
4. TLS settings match the endpoint and certificate trust.
5. The measurement/table name includes the sanitized FRITZ!Box serial when available.

## Repeated backend warnings

The writer retries transient HTTP failures with backoff. Repeated warnings usually indicate
reachability, TLS, authentication, or capacity problems. Persistent failures can eventually fill the
bounded buffer and drop the oldest data. Retry rules are listed in
[InfluxDB](../storage/influxdb.md#retention-and-writes).

## Grafana shows no data

Confirm the datasource type and UID mapping, then use the actual serial-derived measurement name.
The supplied dashboards do not invent data or query a fixed `fritzbox` name after a serial has
been detected.

## FRITZ!Box data is missing

Lua collection is disabled for FRITZ!OS versions below 7. Some services are model- or firmware-
dependent. Look for service-specific warnings and compare the observed cadence with
[Polling and data model](../configuration/data-model.md).
