# Comparison with the original project

fritzFluxDB is a fork of [bb-Ricardo/fritzinfluxdb](https://github.com/bb-Ricardo/fritzinfluxdb)
by Ricardo Bartels, which laid the entire foundation for collecting FRITZ!Box metrics into
InfluxDB. This fork modernises the codebase around operational reliability, a smaller
container-first footprint, and QuestDB as a third storage backend.

| | `fritzFluxDB` (this fork) | `fritzinfluxdb` (original) |
|---|---|---|
| **Python** | 3.13 | 3.7+ |
| **Database backends** | InfluxDB v1, InfluxDB v2 **and QuestDB** | InfluxDB v1 and v2 |
| **Database client** | `httpx` — single lightweight HTTP dependency | `influxdb` + `influxdb_client` libraries |
| **Outage logging** | One error on outage, silent retries, one recovery message | Repeated errors per retry |
| **Graceful shutdown** | Buffered measurements are flushed before exit | Buffer discarded on shutdown |
| **HTTP backoff** | Exponential backoff on `429`/`5xx`, honours `Retry-After`, auto-shrinks batch on `413` | Fixed retry interval |
| **Parser robustness** | Hardened against malformed JSON/XML/CSV with descriptive errors | Basic parsing |
| **Measurement identity** | Named after the FRITZ!Box serial — swapping hardware keeps history cleanly separated | Single static measurement |
| **Secret handling** | Credentials masked in logs; refuses to send credentials over plain HTTP to remote hosts | — |
| **Docker image** | Multi-arch (`amd64`/`arm64`), non-root, Tini as PID 1 | Single-arch, runs as root |
| **Timezone correctness** | Log timestamps are timezone-aware | — |

The original still ships some features this fork intentionally dropped, for example automatic
InfluxDB retention-policy creation. If you rely on those, the upstream project may suit you
better.
