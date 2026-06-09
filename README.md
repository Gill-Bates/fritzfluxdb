<p align="center">
  <img src=".github/img/fritz_logo.svg" alt="fritzFluxDB Logo" width="350">
</p>

<h1 align="center">fritzFluxDB</h1>

<p align="center">
  Lightweight daemon that collects metrics from your AVM FritzBox and pushes them into InfluxDB.
</p>

<p align="center">
  <a href="https://github.com/Gill-Bates/fritzfluxdb/releases">
    <img src="https://img.shields.io/github/v/tag/Gill-Bates/fritzfluxdb?label=version&color=blue" alt="Latest Version">
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
  <img src="https://img.shields.io/badge/python-3.13%2B-yellow" alt="Python 3.13+">
</p>

> [!NOTE]
> **Built on the shoulders of giants.** This project is a fork of and would not exist without
> [**bb-Ricardo/fritzinfluxdb**](https://github.com/bb-Ricardo/fritzinfluxdb) by **Ricardo Bartels**.
> The original laid the entire foundation for collecting FritzBox metrics into InfluxDB —
> a huge thank you for the years of work behind it. 🙏


## 📋 Table of Contents

- [Why this fork?](#-why-this-fork)
- [Features](#-features)
- [Quick Start](#-quick-start)
- [Configuration](#%EF%B8%8F-configuration)
- [Grafana Dashboards](#-grafana-dashboards)
- [Local Development](#-local-development)
- [License](#-license)

---

## 🔀 Why this fork?

The original project is excellent and battle-tested. This fork modernises the codebase and
focuses on **operational reliability** and a **smaller, container-first footprint**.

| | `fritzFluxDB` (this fork) | `fritzinfluxdb` (original) |
|---|---|---|
| **Python** | 3.13 | 3.7+ |
| **InfluxDB client** | `httpx` — single lightweight HTTP dependency | `influxdb` + `influxdb_client` libraries |
| **Outage logging** | One error on outage, silent retries, one recovery message | Repeated errors per retry |
| **Graceful shutdown** | Buffered measurements are flushed before exit | Buffer discarded on shutdown |
| **HTTP backoff** | Exponential backoff on `429`/`5xx`, honours `Retry-After`, auto-shrinks batch on `413` | Fixed retry interval |
| **Parser robustness** | Hardened against malformed JSON/XML/CSV with descriptive errors | Basic parsing |
| **Measurement identity** | Named after the FritzBox serial — swapping hardware keeps history cleanly separated | Single static measurement |
| **Secret handling** | Credentials masked in logs; refuses to send credentials over plain HTTP to remote hosts | — |
| **Docker image** | Multi-arch (`amd64`/`arm64`), non-root, Tini as PID 1 | Single-arch, runs as root |
| **Timezone correctness** | Log timestamps are timezone-aware | — |

> The original still ships features this fork intentionally dropped (e.g. automatic
> retention-policy creation). If you rely on those, the upstream project may suit you better.

---

## ✨ Features

-  Collects TR-064 & Lua service data from FritzBox
-  Supports InfluxDB v1 and v2
-  Home automation metrics (smart home devices)
-  Call logs & telephone data
-  VPN, network hosts, connection info
-  Multi-arch Docker image (`amd64` / `arm64`)
-  Runs as non-root with Tini as PID 1

---

## 🚀 Quick Start

### 1. Create a `.env` file

```env
FRITZBOX_HOSTNAME=192.168.178.1
FRITZBOX_USERNAME=admin
FRITZBOX_PASSWORD=your-secret-password

INFLUXDB_VERSION=2
INFLUXDB_HOSTNAME=influxdb
INFLUXDB_PORT=8086
INFLUXDB_ORGANIZATION=my-org
INFLUXDB_BUCKET=fritzflux
INFLUXDB_TOKEN=your-influxdb-token
```

### 2. Create `docker-compose.yml`

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

### 3. Run

```bash
docker compose up -d
```

That's it. Metrics will start flowing into your InfluxDB.

---

## ⚙️ Configuration

All settings are passed via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `FRITZBOX_HOSTNAME` | `192.168.178.1` | FritzBox IP or hostname |
| `FRITZBOX_USERNAME` | — | FritzBox login user |
| `FRITZBOX_PASSWORD` | — | FritzBox login password |
| `INFLUXDB_VERSION` | `2` | InfluxDB version (`1` or `2`) |
| `INFLUXDB_HOSTNAME` | — | InfluxDB host |
| `INFLUXDB_PORT` | `8086` | InfluxDB port |
| `INFLUXDB_ORGANIZATION` | — | InfluxDB v2 organization |
| `INFLUXDB_BUCKET` | `fritzflux` | InfluxDB bucket / database |
| `INFLUXDB_TOKEN` | — | InfluxDB auth token |
| `INFLUXDB_TLS_ENABLED` | `false` | Enable TLS for InfluxDB |
| `LOG_LEVEL` | `INFO` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `TZ` | `Europe/Berlin` | Container timezone |

---

## 📊 Grafana Dashboards

Pre-built dashboards are included in the `grafana/` directory:

- **System Dashboard** — CPU, memory, uptime, temperatures
- **Call Log Dashboard** — Incoming/outgoing calls
- **Logs Dashboard** — FritzBox system logs
- **Home Automation Dashboard** — Smart home device metrics (InfluxDB v2)

Import the JSON files from `grafana/influx2_dashboards/` (or `influx1_dashboards/`) into your Grafana instance.

---

## 🛠 Local Development

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python run.py -c setup.conf
```

### Build Docker image locally

```bash
./docker/build.sh
```

---

## 📄 License

[MIT](LICENSE) — © 2026 Gill-Bates

---

> **Disclaimer:** This project is an independent open-source tool and is not affiliated with, endorsed by, or in any way associated with AVM GmbH or the FRITZ!Box product line. FRITZ!Box is a registered trademark of AVM GmbH.