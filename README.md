<p align="center">
  <img src=".github/img/fritz_logo.svg" alt="fritzFluxDB Logo" width="350">
</p>

<h1 align="center">fritzFluxDB</h1>

<p align="center">
  Lightweight daemon that collects metrics from your FRITZ!Box and pushes them into InfluxDB or QuestDB.
</p>

<p align="center">
  <a href="https://github.com/Gill-Bates/fritzfluxdb/releases">
    <img src="https://img.shields.io/github/v/tag/Gill-Bates/fritzfluxdb?label=version&color=blue" alt="Latest Version">
  </a>
  <a href="https://github.com/Gill-Bates/fritzfluxdb/actions/workflows/ci.yml">
    <img src="https://github.com/Gill-Bates/fritzfluxdb/actions/workflows/ci.yml/badge.svg" alt="CI">
  </a>
  <a href="https://github.com/Gill-Bates/fritzfluxdb/actions/workflows/docker-build.yml">
    <img src="https://github.com/Gill-Bates/fritzfluxdb/actions/workflows/docker-build.yml/badge.svg" alt="Docker Build">
  </a>
  <a href="https://hub.docker.com/r/giiibates/fritzfluxdb">
    <img src="https://img.shields.io/docker/pulls/giiibates/fritzfluxdb" alt="Docker Pulls">
  </a>
  <a href="https://github.com/Gill-Bates/fritzfluxdb/blob/main/LICENSE">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT">
  </a>
  <a href="https://hub.docker.com/r/giiibates/fritzfluxdb/tags">
    <img src="https://img.shields.io/badge/platform-amd64%20%7C%20arm64-blue?logo=docker&logoColor=white" alt="Multi-arch: amd64, arm64">
  </a>
</p>

<p align="center">
  <a href="#quick-start"><img src="https://img.shields.io/badge/Quick%20Start-168B75?style=for-the-badge&logo=docker&logoColor=white" alt="Quick Start"></a>
  <a href="https://gill-bates.github.io/fritzfluxdb/"><img src="https://img.shields.io/badge/Documentation-34495E?style=for-the-badge&logo=readthedocs&logoColor=white" alt="Documentation"></a>
</p>

## Features

| Area | Detail |
| --- | --- |
| Backends | InfluxDB v1, InfluxDB v2, QuestDB 10+ (recommended; optional server-side downsampling) |
| Data | TR-064 and Lua services, home automation, call list, router log, VPN, network hosts, system statistics |
| Container | Multi-arch (`amd64`/`arm64`), non-root, Tini as PID 1, restart watchdog, buffer flush on shutdown, `fritzflux-cli` for QuestDB checks and cleanup |
| Dashboards | System, call log and router log dashboards for every backend, plus home automation for InfluxDB v2 and QuestDB, in `utils/grafana/` |

<p align="center">
  <img src=".github/img/dashboard_1.png" alt="Grafana system dashboard showing FritzBox connection, CPU, RAM and traffic panels" width="800">
</p>

## Quick Start

> **Version 2.0.0 starts with fresh database schemas.** Use a new QuestDB instance, InfluxDB
> database or bucket; nothing is migrated and existing data is not touched. Home-automation devices
> are identified by their AIN in the `id` tag, and `name` stays a display tag.


```bash
test -e .env || cat .env.example .env.questdb.example > .env
# edit .env with your FRITZ!Box settings
docker compose -f docker/docker-compose.questdb.yml up -d
```

The Compose file starts QuestDB together with fritzfluxdb. For InfluxDB, build `.env` from
`.env.example` plus `.env.influxdb.example` and start `docker/docker-compose.influx2.yml` (or
`docker-compose.influx1.yml`); these files start fritzfluxdb only, so an existing InfluxDB must be
reachable from the container. For a local Python run, see
[Installation](https://gill-bates.github.io/fritzfluxdb/getting-started/installation/) and
[Docker Compose](https://gill-bates.github.io/fritzfluxdb/getting-started/docker/).

## Documentation

- [Installation](https://gill-bates.github.io/fritzfluxdb/getting-started/installation/)
- [Environment variables](https://gill-bates.github.io/fritzfluxdb/configuration/environment/)
- [Storage backends](https://gill-bates.github.io/fritzfluxdb/storage/overview/)
- [Grafana dashboards](https://gill-bates.github.io/fritzfluxdb/monitoring/grafana/)
- [Local development](https://gill-bates.github.io/fritzfluxdb/development/setup/)
- [Changelog](CHANGELOG.md)

## License

[MIT](LICENSE) — © 2026 Gill-Bates

---

> **Disclaimer:** This project is an independent open-source tool and is not affiliated with, endorsed by, or in any way associated with FRITZ.com GmbH, Alt-Moabit 95, 10559 Berlin, or the FRITZ!Box product line. FRITZ!Box is a registered trademark of FRITZ.com GmbH.

<p align="center">
  <a href="https://www.buymeacoffee.com/tnsteinerx">
    <img src="https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20beer&emoji=%F0%9F%8D%BA&slug=tnsteinerx&button_colour=FFDD00&font_colour=000000&font_family=Cookie&outline_colour=000000&coffee_colour=ffffff" alt="Buy Me A Coffee">
  </a>
</p>