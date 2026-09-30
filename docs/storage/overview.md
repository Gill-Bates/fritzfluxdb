# Storage backends

The writer speaks HTTP and supports three backend modes:

| Mode | Protocol / port | Credentials |
| --- | --- | --- |
| InfluxDB v1 | InfluxDB HTTP API, normally `8086` | Optional username/password and database |
| InfluxDB v2 | InfluxDB HTTP API, normally `8086` | Organization, bucket, token |
| QuestDB | Influx Line Protocol over HTTP, normally `9000` | Optional HTTP basic authentication (Open Source) |

Choose one backend through `DB_TYPE`. The backend hostname is mandatory; the mode-specific
configuration is validated at startup. A full `https://` hostname derives TLS and a port when no
explicit port is set. Port `443` also implies TLS.

!!! tip "Recommended backend: QuestDB"
    fritzFluxDB prefers QuestDB, and QuestDB is the author's recommended storage backend. Only
    QuestDB offers optional server-side downsampling with materialized rollup views, managed TTL
    retention, the `fritzflux-cli` admin tool, and Grafana dashboards that switch between raw data
    and rollups automatically. The bundled Compose file also starts QuestDB itself. InfluxDB v1 and
    v2 remain supported. `DB_TYPE` still defaults to `influxdb_v1`, so select QuestDB explicitly.

## Which backend should I use?

- Choose QuestDB (recommended) when you prefer SQL-oriented access, HTTP line protocol, and optional
  built-in TTL/downsampling profiles.
- Choose InfluxDB v1 for an existing legacy InfluxDB installation.
- Choose InfluxDB v2 for the current InfluxDB bucket/organization model and the Flux dashboards.

The project intentionally does not create InfluxDB retention policies automatically. Configure
retention in the database platform according to your storage requirements.
