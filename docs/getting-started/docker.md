# Docker Compose

The InfluxDB examples start only the fritzfluxdb application. The QuestDB example also starts
QuestDB itself.

| Backend | Compose file | Services |
| --- | --- | --- |
| InfluxDB v1 | `docker/docker-compose.influx1.yml` | `fritzfluxdb`; connect to an existing InfluxDB |
| InfluxDB v2 | `docker/docker-compose.influx2.yml` | `fritzfluxdb`; connect to an existing InfluxDB |
| QuestDB | `docker/docker-compose.questdb.yml` | `fritzfluxdb`, `questdb`; ports `9000` (HTTP), `9003` (health check) |

## Configure a backend

Create `.env` in the repository root from the basic template and exactly one backend template.
The bundled Compose files read this root `.env`. Fill in your own values and do not commit it.

```bash
test -e .env || cat .env.example .env.influxdb.example > .env  # InfluxDB v1 or v2
# For QuestDB, use .env.questdb.example instead of .env.influxdb.example when creating .env.
```

The basic template contains daemon and FRITZ!Box settings. The selected backend template sets
`DB_TYPE` and contains only that backend's settings. For example, the combined file may contain:

```env
FRITZBOX_HOSTNAME=192.168.178.1
FRITZBOX_USERNAME=<fritzbox-user>
FRITZBOX_PASSWORD=<fritzbox-password>
DB_TYPE=influxdb_v2
INFLUXDB_HOSTNAME=<reachable-influxdb-host>
INFLUXDB_ORGANIZATION=<organization>
INFLUXDB_BUCKET=<bucket>
INFLUXDB_TOKEN=<token>
```

Set `INFLUXDB_TLS_ENABLED=true` for an HTTPS endpoint. For an HTTP endpoint on a trusted network,
set `INFLUXDB_ALLOW_PLAINTEXT_CREDENTIALS=true`; otherwise the application refuses to send the token.

For InfluxDB, ensure the configured endpoint is reachable from the application container. If
InfluxDB runs in a different Compose project, attach both projects to a shared Docker network and
use its service name, or use another hostname reachable from the container. The InfluxDB examples
do not expose ports or create database volumes. The QuestDB example includes the `questdb` service
on the same Compose network and persists its data in a named volume.

Then start the application and, for QuestDB, its bundled database:

```bash
docker compose -f docker/docker-compose.influx1.yml up -d
docker compose -f docker/docker-compose.influx2.yml up -d
docker compose -f docker/docker-compose.questdb.yml up -d
```

The QuestDB example uses the service name `questdb` as the database hostname and persists its data
in a named volume. All examples apply `no-new-privileges` to the fritzfluxdb service. The
application container has a 20-second stop grace period so its measurement buffer can drain.

!!! warning "Plaintext credentials"
    The QuestDB bundle allows credentials over HTTP only on its private Compose network. InfluxDB
    Compose examples default to refusing credentials over plaintext because the server is external.
    Set `INFLUXDB_ALLOW_PLAINTEXT_CREDENTIALS=true` only when the HTTP endpoint is on a trusted
    network. Use TLS elsewhere.

## Application-only container

For an external database, define only the application service and supply the settings in `.env`:

```yaml
services:
  fritzfluxdb:
    image: giiibates/fritzfluxdb:latest
    restart: unless-stopped
    env_file: ./.env
    environment:
      TZ: Europe/Berlin
      LOG_LEVEL: INFO
```

Use a versioned image tag for repeatable deployments. The image supports `linux/amd64` and
`linux/arm64`, runs without root privileges, and starts `/app/run.py` through the watchdog.

## Inspect and stop

```bash
docker compose -f docker/docker-compose.questdb.yml ps
docker compose -f docker/docker-compose.questdb.yml logs -f fritzfluxdb
docker compose -f docker/docker-compose.questdb.yml down
```

`down` removes containers and networks but keeps the QuestDB named volume unless `--volumes` is
explicitly used. Treat volume removal as destructive because it removes the bundled QuestDB data.
