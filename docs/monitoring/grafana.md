# Grafana dashboards

The repository ships importable dashboards under `utils/grafana/`:

| Directory | Backend | Dashboards |
| --- | --- | --- |
| `influx1_dashboards/` | InfluxDB v1 | System, call logs, router logs |
| `influx2_dashboards/` | InfluxDB v2 | System, call logs, router logs, home automation |
| `questdb_dashboards/` | QuestDB | System, call logs, router logs, home automation |

## Import workflow

1. Create a Grafana datasource for the selected backend.
2. Import the JSON files from the matching directory.
3. Map the dashboard datasource variable to the datasource where prompted:
   `DS_INFLUXDB` for InfluxDB exports or `DS_QUESTDB` for QuestDB exports.
4. Set the measurement variable to the serial-derived name, for example
   `fritzbox_<sanitized-serial>`.
5. Select a time range after the first successful collection cycle.

The InfluxDB dashboards use the datasource type `influxdb`; QuestDB dashboards use the datasource
type supported by the included QuestDB queries. The JSON files are source artifacts: edit or
regenerate them in `utils/grafana/`, not in an exported copy stored by Grafana.

## Dashboard coverage

- System: CPU, memory, uptime, temperatures, connection and traffic.
- Call logs: incoming and outgoing telephone entries.
- Router logs: FRITZ!Box log entries grouped by log type.
- Home automation: supported temperature, heating, battery, and device-state metrics.

If a dashboard is empty, verify the datasource, time range, measurement/table variable, and that
the matching collector is supported by the FRITZ!Box model and firmware.
