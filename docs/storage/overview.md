# Storage backends

The writer speaks HTTP and supports three backend modes:

| Mode | Protocol / port | Credentials |
| --- | --- | --- |
| InfluxDB v1 | InfluxDB HTTP API, normally `8086` | Optional username/password and database |
| InfluxDB v2 | InfluxDB HTTP API, normally `8086` | Organization, bucket, token |
| QuestDB | Influx Line Protocol over HTTP, normally `9000` | Optional basic auth or bearer token |

Choose one backend through `DB_TYPE`. The backend hostname is mandatory; the mode-specific
configuration is validated at startup. A full `https://` hostname derives TLS and a port when no
explicit port is set. Port `443` also implies TLS.

## Which backend should I use?

- Choose InfluxDB v1 for an existing legacy InfluxDB installation.
- Choose InfluxDB v2 for the current InfluxDB bucket/organization model and the Flux dashboards.
- Choose QuestDB when you prefer SQL-oriented access, HTTP line protocol, and optional built-in
  TTL/downsampling profiles.

The project intentionally does not create InfluxDB retention policies automatically. Configure
retention in the database platform according to your storage requirements.
