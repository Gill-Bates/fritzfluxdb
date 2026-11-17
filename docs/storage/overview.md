# Storage backends

The writer speaks HTTP and supports three backend modes.

!!! warning "Fresh database required"
    Version 2.0.0 starts with fresh database schemas (QuestDB downsampling schema `v1`) and
    identifies home-automation devices by their AIN in the `id` tag; `name` is only a display
    tag. Point it at a new QuestDB instance, InfluxDB database or bucket. Existing data is
    neither migrated nor deleted.

Choose one backend through `DB_TYPE`. The backend hostname is mandatory; the mode-specific
configuration is validated at startup. A full `https://` hostname derives TLS and a port when no
explicit port is set. Port `443` also implies TLS.

!!! tip "Recommended backend: QuestDB"
    Only QuestDB offers server-side downsampling with materialized rollup views, managed TTL
    retention, the `fritzflux-cli` admin tool, and Grafana dashboards that switch between raw data
    and rollups. The bundled Compose file also starts QuestDB. `DB_TYPE` defaults to `influxdb_v1`,
    so select QuestDB explicitly.

## Backend differences

| | InfluxDB v1 | InfluxDB v2 | QuestDB |
| --- | --- | --- | --- |
| Protocol / port | InfluxDB HTTP API, `8086` | InfluxDB HTTP API, `8086` | Influx Line Protocol over HTTP, `9000` |
| Credentials | Optional username/password, database | Organization, bucket, token | Optional HTTP basic authentication |
| Use when | Existing legacy installation | Current bucket/organization model | New installations (recommended) |
| Dashboards | `influx1_dashboards/` | `influx2_dashboards/` | `questdb_dashboards/` |
| Write precision | `ns` | `ns` | `s` |
| Metrics | Measurement `fritzbox_<serial>` | Same as v1 | Table `fritzbox_<serial>_metrics`, see [QuestDB](questdb.md#table-names) |
| Log and call entries | Metrics measurement; sub-second offset from the entry `uid` | Same as v1 | Table `fritzbox_<serial>_events` with DEDUP, see [QuestDB](questdb.md#deduplication-of-log-and-call-entries) |
| Retention | Configure in InfluxDB | Configure in InfluxDB | `QUESTDB_DATA_RETENTION_DAYS`, optional [downsampling](questdb.md#downsampling) |
| `fritzflux-cli` | No | No | Yes |

The project does not create InfluxDB retention policies. Configure retention in the database
platform according to your storage requirements.
