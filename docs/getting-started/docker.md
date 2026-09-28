# Docker Compose

The repository contains complete examples for each backend:

| Backend | Compose file | Database ports |
| --- | --- | --- |
| InfluxDB v1 | `docker/docker-compose.influx1.yml` | `8086` |
| InfluxDB v2 | `docker/docker-compose.influx2.yml` | `8086` |
| QuestDB | `docker/docker-compose.questdb.yml` | `9000` (HTTP), `9003` (Postgres wire) |

## Run a bundled stack

Create `.env` next to the repository and fill in your own values. Do not commit this file.

```env
FRITZBOX_HOSTNAME=192.168.178.1
FRITZBOX_USERNAME=<fritzbox-user>
FRITZBOX_PASSWORD=<fritzbox-password>
INFLUXDB_USERNAME=<database-user>
INFLUXDB_PASSWORD=<database-password>
```

Then select exactly one backend stack:

```bash
docker compose -f docker/docker-compose.influx1.yml up -d
docker compose -f docker/docker-compose.influx2.yml up -d
docker compose -f docker/docker-compose.questdb.yml up -d
```

The examples use the service name (`influxdb` or `questdb`) as the database hostname on the
internal Compose network. They persist database data in named volumes and apply
`no-new-privileges` to the fritzFluxDB service. The application container has a 20-second stop
grace period so its measurement buffer can drain.

!!! warning "Plaintext credentials"
    The bundled stacks set the corresponding `*_ALLOW_PLAINTEXT_CREDENTIALS` default for their
    private Compose network. Use TLS for any endpoint outside a trusted local network.

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
docker compose -f docker/docker-compose.influx2.yml ps
docker compose -f docker/docker-compose.influx2.yml logs -f fritzfluxdb
docker compose -f docker/docker-compose.influx2.yml down
```

`down` removes containers and networks but keeps named volumes unless `--volumes` is explicitly
used. Treat volume removal as destructive because it removes the bundled database data.
