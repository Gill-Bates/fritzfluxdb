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

fritzFluxDB writes batches and retries transient `408`, `425`, `429`, and `5xx` responses. It
honours `Retry-After` when present and reduces the batch after a `413` response. Configure data
retention in InfluxDB itself; the application does not create or rewrite retention policies.

## Verify ingestion

Check the application log for a successful backend connection, then query the configured database
for the measurement name described in [Polling and data model](../configuration/data-model.md).
The Grafana dashboards expect an InfluxDB datasource and a measurement variable matching that name.
