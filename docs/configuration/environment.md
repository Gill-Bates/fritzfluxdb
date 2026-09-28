# Environment variables

Environment variables are the primary container configuration surface. `run.py` loads a local
`.env` file when `python-dotenv` is available; Docker Compose supplies variables through `env_file`
and `environment`. Values shown as `<placeholder>` are intentionally not real credentials.

## General

| Variable | Default | Description |
| --- | --- | --- |
| `DB_TYPE` | `influxdb_v1` | `influxdb_v1`, `influxdb_v2`, or `questdb`. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |
| `TZ` | `Europe/Berlin` | Container and log timezone. |

## FRITZ!Box

| Variable | Default | Description |
| --- | --- | --- |
| `FRITZBOX_HOSTNAME` | `192.168.178.1` | IP address, hostname, or reachable endpoint host. |
| `FRITZBOX_USERNAME` | — | FRITZ!Box login user. |
| `FRITZBOX_PASSWORD` | — | FRITZ!Box login password. |
| `FRITZBOX_PORT` | `49000` | TR-064 port. |
| `FRITZBOX_TLS_ENABLED` | auto | Enable or disable HTTPS explicitly. |
| `FRITZBOX_VERIFY_TLS` | `true` | Verify the FRITZ!Box certificate. |
| `FRITZBOX_CONNECT_TIMEOUT` | `10` | Connection timeout in seconds. |
| `FRITZBOX_REQUEST_INTERVAL` | `10` | Minimum per-service request interval in seconds; it can only increase built-in intervals. |
| `FRITZBOX_BOX_TAG` | `fritz.box` | Tag used to identify the box. |
| `FRITZBOX_TIMEZONE` | `Europe/Berlin` | Fallback timezone for FRITZ!Box log timestamps. |

## InfluxDB

Used for `DB_TYPE=influxdb_v1` or `DB_TYPE=influxdb_v2`.

| Variable | Default | Description |
| --- | --- | --- |
| `INFLUXDB_HOSTNAME` | — | Host or full `http(s)://` URL. |
| `INFLUXDB_PORT` | `8086` | HTTP(S) port; `443` implies TLS. |
| `INFLUXDB_TLS_ENABLED` | `false` | Use HTTPS. A full HTTPS URL also enables it. |
| `INFLUXDB_VERIFY_TLS` | `true` | Verify the server certificate. |
| `INFLUXDB_ALLOW_PLAINTEXT_CREDENTIALS` | `false` | Permit credentials over HTTP to a non-local host. Prefer TLS. |
| `INFLUXDB_MEASUREMENT_NAME` | `fritzbox` | Base measurement name; a serial-specific name normally replaces it. |
| `INFLUXDB_DATABASE` | — | InfluxDB v1 database. |
| `INFLUXDB_USERNAME` / `INFLUXDB_PASSWORD` | — | Optional paired v1 credentials. |
| `INFLUXDB_ORGANIZATION` | — | InfluxDB v2 organization. |
| `INFLUXDB_BUCKET` | — | InfluxDB v2 bucket. |
| `INFLUXDB_TOKEN` | — | InfluxDB v2 token. |

## QuestDB

Used for `DB_TYPE=questdb`. QuestDB settings are mapped to the equivalent writer internally.

| Variable | Default | Description |
| --- | --- | --- |
| `QUESTDB_HOSTNAME` | — | Host or full `http(s)://` URL. |
| `QUESTDB_PORT` | `9000` | Influx Line Protocol over HTTP port. |
| `QUESTDB_TLS_ENABLED` | `false` | Use HTTPS. |
| `QUESTDB_VERIFY_TLS` | `true` | Verify the server certificate. |
| `QUESTDB_ALLOW_PLAINTEXT_CREDENTIALS` | `false` | Permit credentials over HTTP to a non-local host. |
| `QUESTDB_MEASUREMENT_NAME` | `fritzbox` | Base table name; a serial-specific name normally replaces it. |
| `QUESTDB_DATA_RETENTION_DAYS` | `365` | Initial TTL for a table without a TTL; `0` disables automatic TTL. |
| `QUESTDB_DOWNSAMPLING` | empty | Optional profile: `low`, `medium`, or `high`. |
| `QUESTDB_USERNAME` / `QUESTDB_PASSWORD` | — | Optional basic authentication. |
| `QUESTDB_TOKEN` | — | Optional bearer token authentication. |

## Security rules

The application refuses to send non-local credentials over plain HTTP unless the matching
`*_ALLOW_PLAINTEXT_CREDENTIALS=true` switch is set. Keep that override limited to a trusted
private network. The application masks credentials in logs and disables verbose HTTP debugging
that could expose tokens.

## Optional metrics

| Variable | Default | Description |
| --- | --- | --- |
| `FRITZFLUXDB_INCLUDE_VPN_ADDRESS_METRICS` | unset | Include VPN address metrics when set to a non-empty value. |
