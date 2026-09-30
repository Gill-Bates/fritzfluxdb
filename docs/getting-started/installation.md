# Installation

## Prerequisites

- A FRITZ!Box reachable from the host running fritzFluxDB.
- A database endpoint: InfluxDB v1, InfluxDB v2, or QuestDB.
- Docker Engine with Docker Compose for the container deployment, or Python 3.13+ for local runs.
- A Grafana instance if you want to use the supplied dashboards.

The FRITZ!Box account must be allowed to access the TR-064 services used by the application.
Lua-based collection is enabled only when the reported FRITZ!OS major version is at least 7.

## Choose a deployment

| Situation | Recommended path |
| --- | --- |
| Home server or NAS | [Docker Compose](docker.md) |
| Existing InfluxDB/QuestDB | Application-only container with your own Compose service |
| Development or debugging | [Local development](../development/setup.md) |

## Configuration order

1. Choose the backend and create its database, bucket, or table policy.
2. Create a local `.env` from `.env.example` and either `.env.influxdb.example` or
   `.env.questdb.example`, then enter the FRITZ!Box and backend settings.
3. Start the daemon and inspect its logs for successful connection messages.
4. Import the matching [Grafana dashboards](../monitoring/grafana.md), if required.
5. Confirm that new timestamps and the expected measurement/table name are receiving data.

## Updating

Pull the desired image tag and recreate the container. Keep the database storage volume and review
the [changelog](../changelog.md) before changing versions. Database schema and retention changes
are deliberately not performed by an automatic InfluxDB retention-policy migration.
