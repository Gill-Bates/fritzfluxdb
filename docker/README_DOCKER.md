<p align="center">
  <img src="https://raw.githubusercontent.com/Gill-Bates/fritzfluxdb/refs/heads/main/.github/img/fritz_logo.svg" alt="fritzFluxDB Logo" width="350">
</p>

<h1 align="center">fritzFluxDB</h1>

<p align="center">
  Lightweight daemon that collects metrics from your FRITZ!Box and pushes them into InfluxDB or QuestDB.
</p>

<p align="center">
  <a href="https://github.com/Gill-Bates/fritzfluxdb"><img src="https://img.shields.io/github/v/tag/Gill-Bates/fritzfluxdb?label=version&color=blue" alt="Version"></a>
  <a href="https://hub.docker.com/r/giiibates/fritzfluxdb"><img src="https://img.shields.io/docker/pulls/giiibates/fritzfluxdb" alt="Docker Pulls"></a>
  <a href="https://github.com/Gill-Bates/fritzfluxdb/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT"></a>
  <a href="https://hub.docker.com/r/giiibates/fritzfluxdb/tags"><img src="https://img.shields.io/badge/platform-amd64%20%7C%20arm64-blue?logo=docker&logoColor=white" alt="Multi-arch: amd64, arm64"></a>
</p>

<p align="center">
  <a href="https://gill-bates.github.io/fritzfluxdb/"><img src="https://img.shields.io/badge/Full%20Documentation-Read%20the%20docs-2563eb?style=for-the-badge&logo=readthedocs&logoColor=white" alt="Full documentation"></a>
</p>

---

## Features

- Prefers QuestDB, the author's recommended storage backend; InfluxDB v1 and v2 remain fully supported
- Collects TR-064 & Lua service data from FritzBox
- Supports InfluxDB v1, InfluxDB v2 and QuestDB, with optional server-side downsampling on QuestDB
- Home automation, call logs, VPN, network hosts, system stats
- Multi-arch image (`amd64` / `arm64`), runs as non-root with Tini as PID 1
- Graceful shutdown with measurement buffer flush

## Quick Start

The recommended setup uses QuestDB. Create a `.env` file:

```env
FRITZBOX_HOSTNAME=192.168.178.1
FRITZBOX_USERNAME=admin
FRITZBOX_PASSWORD=your-password
```

Create `docker-compose.yml`:

```yaml
services:
  questdb:
    image: questdb/questdb:latest
    container_name: questdb
    restart: unless-stopped
    ports:
      - "9000:9000"
      - "9003:9003"
    volumes:
      - questdb_data:/var/lib/questdb
    environment:
      - QDB_TELEMETRY_ENABLED=false

  fritzfluxdb:
    image: giiibates/fritzfluxdb:latest
    container_name: fritzfluxdb
    restart: unless-stopped
    depends_on:
      - questdb
    env_file:
      - ./.env
    environment:
      TZ: Europe/Berlin
      LOG_LEVEL: INFO
      DB_TYPE: questdb
      QUESTDB_HOSTNAME: questdb
      QUESTDB_PORT: 9000
      QUESTDB_ALLOW_PLAINTEXT_CREDENTIALS: "true"

volumes:
  questdb_data:
```

Start it:

```bash
docker compose up -d
```

This starts QuestDB and fritzfluxdb on the same Compose network. To use InfluxDB instead, omit the
`questdb` service and set `DB_TYPE=influxdb_v2` (or `influxdb_v1`) plus the `INFLUXDB_*` settings
for an existing, reachable InfluxDB server. Plain HTTP credentials are rejected by default; enable
`INFLUXDB_ALLOW_PLAINTEXT_CREDENTIALS` only on a trusted network, or set `INFLUXDB_TLS_ENABLED=true`
when the endpoint supports HTTPS.

For InfluxDB setup, the full environment variable reference, and downsampling profiles, see
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

> This project is not affiliated with or endorsed by FRITZ.com GmbH, Alt-Moabit 95, 10559 Berlin. FRITZ!Box is a registered trademark of FRITZ.com GmbH.

<p align="center">
  <a href="https://www.buymeacoffee.com/tnsteinerx">
    <img src="https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20beer&emoji=%F0%9F%8D%BA&slug=tnsteinerx&button_colour=FFDD00&font_colour=000000&font_family=Cookie&outline_colour=000000&coffee_colour=ffffff" alt="Buy Me A Coffee">
  </a>
</p>
