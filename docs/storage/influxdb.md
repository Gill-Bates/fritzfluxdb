# InfluxDB

## InfluxDB v1

Set `DB_TYPE=influxdb_v1` and provide `INFLUXDB_HOSTNAME` plus `INFLUXDB_DATABASE`. Username and
password are optional, but if one is set the other must also be set.

```env
DB_TYPE=influxdb_v1
INFLUXDB_HOSTNAME=influxdb.example.invalid
INFLUXDB_PORT=8086
INFLUXDB_DATABASE=<database-name>
INFLUXDB_USERNAME=<username>
INFLUXDB_PASSWORD=<password>
INFLUXDB_TLS_ENABLED=true
```

## InfluxDB v2

Set `DB_TYPE=influxdb_v2` and provide the organization, bucket, and token.

```env
DB_TYPE=influxdb_v2
INFLUXDB_HOSTNAME=influxdb.example.invalid
INFLUXDB_PORT=8086
INFLUXDB_ORGANIZATION=<organization>
INFLUXDB_BUCKET=<bucket>
INFLUXDB_TOKEN=<token>
INFLUXDB_TLS_ENABLED=true
```

## Retention and writes

| Response | Behaviour |
| --- | --- |
| `408`, `425`, `429`, `500`, `502`, `503`, `504` | Retried with exponential backoff; `Retry-After` is honoured, capped at the maximum retry interval. |
| `413` | The batch is halved and retried. A single event that is still too large (for example a call-list entry) is dropped whole and logged. |
| Buffer full | The oldest measurements are dropped. |

Log and call entries are written with `precision=ns`, see
[Deduplication](questdb.md#deduplication-of-log-and-call-entries). Configure data retention in
InfluxDB itself; the application does not create or rewrite retention policies.

## Verify ingestion

Check the application log for a successful backend connection, then query the configured database
for the measurement name described in [Polling and data model](../configuration/data-model.md).
The Grafana dashboards expect an InfluxDB datasource and a measurement variable matching that name.
