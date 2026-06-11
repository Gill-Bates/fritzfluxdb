<p align="center">
  <img src="https://raw.githubusercontent.com/Gill-Bates/fritzfluxdb/refs/heads/main/.github/img/fritz_logo.svg" alt="fritzFluxDB Logo" width="350">
</p>


# fritzFluxDB

Lightweight daemon that collects metrics from your AVM FritzBox and pushes them into InfluxDB or QuestDB.

[![GitHub](https://img.shields.io/github/v/tag/Gill-Bates/fritzfluxdb?label=version&color=blue)](https://github.com/Gill-Bates/fritzfluxdb)
[![Docker Pulls](https://img.shields.io/docker/pulls/giiibates/fritzfluxdb)](https://hub.docker.com/r/giiibates/fritzfluxdb)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/Gill-Bates/fritzfluxdb/blob/main/LICENSE)

---

## Features

- Collects TR-064 & Lua service data from FritzBox
- Supports InfluxDB v1, InfluxDB v2 and QuestDB
- Home automation, call logs, VPN, network hosts, system stats
- Multi-arch image (`amd64` / `arm64`)
- Runs as non-root with Tini as PID 1
- Graceful shutdown with measurement buffer flush

---

## Quick Start

### 1. `.env` file

```env
FRITZBOX_HOSTNAME=192.168.178.1
FRITZBOX_USERNAME=admin
FRITZBOX_PASSWORD=your-password

DB_TYPE=influxdb_v2
INFLUXDB_HOSTNAME=influxdb
INFLUXDB_PORT=8086
INFLUXDB_ORGANIZATION=my-org
INFLUXDB_BUCKET=fritzflux
INFLUXDB_TOKEN=your-token
# allow sending the token over plain HTTP inside your trusted home network
INFLUXDB_ALLOW_PLAINTEXT_CREDENTIALS=true
```

For QuestDB use instead:

```env
DB_TYPE=questdb
QUESTDB_HOSTNAME=questdb
QUESTDB_PORT=9000
```

### 2. `docker-compose.yml`

```yaml
services:
  fritzfluxdb:
    image: giiibates/fritzfluxdb:latest
    container_name: fritzfluxdb
    restart: unless-stopped
    env_file:
      - ./.env
    environment:
      TZ: Etc/UTC
      LOG_LEVEL: INFO
```

### 3. Start

```bash
docker compose up -d
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `FRITZBOX_HOSTNAME` | `192.168.178.1` | FritzBox IP or hostname |
| `FRITZBOX_USERNAME` | — | FritzBox login user |
| `FRITZBOX_PASSWORD` | — | FritzBox login password |
| `DB_TYPE` | `influxdb_v2` | Database backend: `influxdb_v1`, `influxdb_v2` or `questdb` |
| `INFLUXDB_HOSTNAME` | — | InfluxDB host (or full URL, e.g. `https://influx.example.com`) |
| `INFLUXDB_PORT` | `8086` | InfluxDB port (`443` enables TLS automatically) |
| `INFLUXDB_ORGANIZATION` | — | InfluxDB v2 organization |
| `INFLUXDB_BUCKET` | `fritzflux` | InfluxDB bucket / database |
| `INFLUXDB_TOKEN` | — | InfluxDB v2 auth token |
| `INFLUXDB_TLS_ENABLED` | `false` | Enable TLS for InfluxDB connection |
| `INFLUXDB_ALLOW_PLAINTEXT_CREDENTIALS` | `false` | Allow credentials/token over plain HTTP (trusted networks only) |
| `QUESTDB_HOSTNAME` | — | QuestDB host (or full URL); `QUESTDB_*` mirrors the `INFLUXDB_*` options |
| `QUESTDB_PORT` | `9000` | QuestDB ILP/HTTP port |
| `LOG_LEVEL` | `INFO` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `TZ` | `Etc/UTC` | Container timezone |
| `WATCHDOG_RESTART_DELAY` | `10` | Initial reconnect delay in seconds |
| `WATCHDOG_MAX_RESTART_DELAY` | `300` | Maximum reconnect delay in seconds |

---

## Grafana Dashboards

Pre-built dashboards are available in the [GitHub repository](https://github.com/Gill-Bates/fritzfluxdb/tree/main/grafana):

- **System Dashboard** — CPU, memory, uptime, temperatures, traffic
- **Call Log Dashboard** — Incoming/outgoing calls
- **Logs Dashboard** — FritzBox system logs
- **Home Automation Dashboard** — Smart home device metrics (InfluxDB v2 only)

---

## Links

- [GitHub Repository](https://github.com/Gill-Bates/fritzfluxdb)
- [Changelog](https://github.com/Gill-Bates/fritzfluxdb/blob/main/CHANGELOG.md)

---

> This project is not affiliated with or endorsed by AVM GmbH. FRITZ!Box is a registered trademark of AVM GmbH.
