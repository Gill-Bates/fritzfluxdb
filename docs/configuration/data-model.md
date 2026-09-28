# Polling and data model

fritzFluxDB runs independent asynchronous handlers for FRITZ!Box data, Lua data, log/config data,
and the selected database writer. The writer buffers measurements and retries transient HTTP
failures with exponential backoff. A `413` response causes the write batch to shrink.

## Collection cadence

| Data family | Built-in cadence |
| --- | --- |
| TR-064 service data | 60 seconds |
| Lua service data and smart home data | 60 seconds |
| Active network hosts and connection details | 10 minutes |
| System statistics | 150 seconds |
| FRITZ!Box log/call data | 60 seconds |

`FRITZBOX_REQUEST_INTERVAL` is a lower bound for requests. It can slow a service down but never
overrides a longer service-specific interval.

## Measurement and table names

The default base name is `fritzbox`. Once the FRITZ!Box serial is available, the daemon uses
`fritzbox_<serial>` after replacing non-alphanumeric characters with underscores. This keeps data
from replaced hardware separated while preserving history for the same box.

The same logical name is an InfluxDB measurement or a QuestDB table. The supplied dashboards use a
configurable measurement variable because the serial-specific name is discovered at runtime.

## Failure and shutdown behaviour

- A missing or invalid configuration returns exit status `78` and is not retried by the Docker
  watchdog.
- Transient backend failures are retried and logged with connection-loss/recovery messages.
- On `SIGINT`, `SIGTERM`, or `SIGHUP`, producers stop first, the measurement queue drains for up
  to 15 seconds, and the writer then closes.
- A full buffer drops the oldest measurements so the daemon remains bounded in memory.
