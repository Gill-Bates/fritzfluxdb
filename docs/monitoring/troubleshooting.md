# Troubleshooting

## Container exits immediately

Inspect the logs first:

```bash
docker compose logs --tail=200 fritzfluxdb
```

Exit status `78` means the application rejected its configuration. Check `DB_TYPE`, the matching
hostname, required database fields, port range, and TLS/plaintext credential settings. A value that
cannot be parsed as a number or boolean, a username without a password, and a hostname URL with a
path also end here; see [Validation](../configuration/environment.md#validation). The log line above
the exit names the offending option. The watchdog does not retry status `78` because restarting
cannot fix a static configuration error.

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
reachability, TLS, authentication, or capacity problems. A `413` response is handled by shrinking
the write batch; persistent failures can eventually fill the bounded buffer and drop oldest data.

## Grafana shows no data

Confirm the datasource type and UID mapping, then use the actual serial-derived measurement name.
The supplied dashboards do not invent data or query a fixed `fritzbox` name after a serial has
been detected.

## FRITZ!Box data is missing

Lua collection is disabled for FRITZ!OS versions below 7. Some services are model- or firmware-
dependent. Look for service-specific warnings and compare the observed cadence with
[Polling and data model](../configuration/data-model.md).
