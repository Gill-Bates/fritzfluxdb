# Architecture

fritzFluxDB is a Python 3.13 asynchronous daemon. It runs from source in the container rather
than installing the application as a console package.

```text
run.py
└── app/main.py
    ├── FritzBoxHandler          TR-064 service polling
    ├── FritzBoxLuaHandler       Lua polling on FRITZ!OS 7+
    ├── InfluxLogAndConfigWriter log and configuration measurements
    └── InfluxHandler            InfluxDB v1/v2 or QuestDB writer
```

## Repository layout

| Path | Responsibility |
| --- | --- |
| `app/classes/fritzbox/` | FRITZ!Box connections, parsing, and service definitions. |
| `app/classes/influxdb/` | Backend configuration, HTTP writes, buffering, retry, and retention handling. |
| `app/classes/common.py` | Shared configuration and measurement primitives. |
| `app/cli_parser.py` | `--config`, verbosity, and log-level command-line parsing. |
| `docker/entrypoint.sh` | Watchdog, restart backoff, signal forwarding, and exit handling. |
| `utils/grafana/` | Backend-specific dashboard JSON files. |
| `tests/` | Unit and integration-style tests for parsers, storage, retry, and release behaviour. |

## Runtime flow

At startup the daemon loads `.env`, parses optional INI configuration, validates all handlers, and
connects to the FRITZ!Box. Producers put measurements into an asyncio queue. The database writer
consumes that queue, batches writes, and applies retry/backoff rules. Shutdown cancels producers,
drains the queue, then closes the writer and FRITZ!Box connections.

The serial number is used to derive the measurement/table name after the initial connection. This
is why dashboards must permit a configurable name.
