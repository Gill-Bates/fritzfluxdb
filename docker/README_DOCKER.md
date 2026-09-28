<p align="center">
  <img src="https://raw.githubusercontent.com/Gill-Bates/fritzfluxdb/refs/heads/main/.github/img/fritz_logo.svg" alt="fritzFluxDB Logo" width="350">
</p>

# fritzFluxDB

Lightweight daemon that collects metrics from your AVM FritzBox and pushes them into InfluxDB or QuestDB.

[![GitHub](https://img.shields.io/github/v/tag/Gill-Bates/fritzfluxdb?label=version&color=blue)](https://github.com/Gill-Bates/fritzfluxdb)
[![Docker Pulls](https://img.shields.io/docker/pulls/giiibates/fritzfluxdb)](https://hub.docker.com/r/giiibates/fritzfluxdb)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/Gill-Bates/fritzfluxdb/blob/main/LICENSE)

📖 [**Full documentation**](https://gill-bates.github.io/fritzfluxdb/)

---

## Features

- Collects TR-064 & Lua service data from FritzBox
- Supports InfluxDB v1, InfluxDB v2 and QuestDB, with optional server-side downsampling on QuestDB
- Home automation, call logs, VPN, network hosts, system stats
- Multi-arch image (`amd64` / `arm64`), runs as non-root with Tini as PID 1
- Graceful shutdown with measurement buffer flush

## Quick Start

Create a `.env` file:

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
```

Create `docker-compose.yml`:

```yaml
services:
  fritzfluxdb:
    image: giiibates/fritzfluxdb:latest
    container_name: fritzfluxdb
    restart: unless-stopped
    env_file:
      - ./.env
    environment:
      TZ: Europe/Berlin
      LOG_LEVEL: INFO
```

Start it:

```bash
docker compose up -d
```

For QuestDB, InfluxDB v1, the full environment variable reference, and downsampling profiles, see
[**Installation**](https://gill-bates.github.io/fritzfluxdb/getting-started/installation/) and
[**Environment variables**](https://gill-bates.github.io/fritzfluxdb/configuration/environment/)
in the documentation.

## Grafana Dashboards

Example dashboards for every backend ship in the [repository](https://github.com/Gill-Bates/fritzfluxdb/tree/main/utils/grafana)
— system, call log, router log, and home automation. See
[**Grafana dashboards**](https://gill-bates.github.io/fritzfluxdb/monitoring/grafana/) for the
import workflow.

## Links

- [Documentation](https://gill-bates.github.io/fritzfluxdb/)
- [GitHub Repository](https://github.com/Gill-Bates/fritzfluxdb)
- [Changelog](https://github.com/Gill-Bates/fritzfluxdb/blob/main/CHANGELOG.md)
- [License: MIT](https://github.com/Gill-Bates/fritzfluxdb/blob/main/LICENSE)

---

> This project is not affiliated with or endorsed by AVM GmbH. FRITZ!Box is a registered trademark of AVM GmbH.

<p align="center">
  <a href="https://www.buymeacoffee.com/tnsteinerx">
    <img src="https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20beer&emoji=%F0%9F%8D%BA&slug=tnsteinerx&button_colour=FFDD00&font_colour=000000&font_family=Cookie&outline_colour=000000&coffee_colour=ffffff" alt="Buy Me A Coffee">
  </a>
</p>
