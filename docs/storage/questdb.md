# QuestDB

Set `DB_TYPE=questdb` and provide `QUESTDB_HOSTNAME`. The default HTTP endpoint is port `9000`.

```env
DB_TYPE=questdb
QUESTDB_HOSTNAME=questdb.example.invalid
QUESTDB_PORT=9000
QUESTDB_MEASUREMENT_NAME=fritzbox
QUESTDB_DATA_RETENTION_DAYS=365
QUESTDB_TLS_ENABLED=true
```

## Authentication

Use `QUESTDB_USERNAME` and `QUESTDB_PASSWORD` for basic authentication, or `QUESTDB_TOKEN` for
bearer authentication. Do not configure both credential styles unless your QuestDB deployment
requires it. TLS certificate verification remains enabled by default.

## TTL

`QUESTDB_DATA_RETENTION_DAYS` is applied only when a table has no TTL. An existing TTL is never
overwritten by a later application start. Set the value to `0` to disable automatic TTL setup.
To change an existing TTL, use QuestDB administration directly, for example:

```sql
ALTER TABLE "fritzbox_<serial>" SET TTL 365 DAYS;
```

The `<serial>` placeholder is the sanitized FRITZ!Box serial; replace it with the actual table
name. QuestDB TTL support must be available in the server version you run.

!!! note "Exception with an active downsampling profile"
    A raw TTL that fritzfluxdb set itself is adjusted when the downsampling profile changes. Only a
    TTL of unknown origin is left untouched; see [TTL ownership](#ttl-ownership).

## Downsampling

`QUESTDB_DOWNSAMPLING` accepts `low`, `medium`, or `high`. The profiles are:

| Profile | Raw retention | Rollup interval |
| --- | ---: | --- |
| `low` | 30 days | 1 minute |
| `medium` | 7 days | 1 minute |
| `high` | 1 day | 5 minutes |

Raw polling and ingestion are unchanged; QuestDB maintains the rollup through a materialized view
and applies its TTL server-side. Requires QuestDB OSS 8.3.1 or newer.

Use the matching QuestDB dashboards from `utils/grafana/questdb_dashboards/` after ingestion.

### Choosing a profile

With `QUESTDB_DATA_RETENTION_DAYS=365`, one year of history costs roughly:

| Setting | Full resolution | Older data | Disk after 1 year |
| --- | ---: | --- | ---: |
| unset | 365 days | — | ~12.5 GiB |
| `low` | 30 days | 1-minute rollup, 365 days | ~7.8 GiB |
| `medium` | 7 days | 1-minute rollup, 365 days | ~7.0 GiB |
| `high` | 1 day | 5-minute rollup, 365 days | ~1.4 GiB |

The two 1-minute profiles save far less than their raw window suggests: the rollup keeps five
aggregates per gauge, so it costs about half a raw day per day rather than a fraction of one. Only
`high` reduces the total substantially, and only because its 5-minute buckets cut the row count.

!!! note "Basis of these figures"
    Measured on a FRITZ!Box 7590 AX writing about 10 raw rows per minute across 203 columns:
    35 MiB per raw day and 19 MiB per 1-minute rollup day, from QuestDB partition sizes. The
    5-minute figure (about 3.8 MiB per day) is extrapolated from the bucket ratio, not measured.
    Your volume scales with the number of active services, smart home devices and VPN users, so
    treat the table as an order of magnitude.

Logs, call logs, current snapshots and state histories are never downsampled. Their history ends
with the raw window, so `high` leaves one day of log history regardless of the rollup retention.

### Provisioning order

The active profile is published to the append-only status table only after the rollup exists, is
reported valid and current, and the raw TTL has been applied and read back. Grafana routes on that
status, so the `raw_days` it reads always matches the TTL QuestDB enforces. If the raw TTL cannot be
established, no active status is published and the previous state remains in effect.

### TTL ownership

A raw TTL that fritzfluxdb did not set is treated as administrator-owned and is never overwritten.
The profile's raw window is then not applied, and the existing TTL is published as the effective raw
history instead. A TTL that fritzfluxdb set itself is recorded as managed in the status table and is
adjusted when the profile changes, which is what allows an existing installation to reduce its raw
storage. On an upgrade from a version without ownership tracking, an existing TTL counts as
administrator-owned.

!!! warning "Disabling downsampling does not restore the previous raw window"
    Clearing `QUESTDB_DOWNSAMPLING` keeps existing rollup views and the raw TTL that the previous
    profile applied, and Grafana falls back to raw data only. After `medium` the dashboards
    therefore show at most the remaining 7 days, even though older rollup data still exists.
    Raise the TTL in QuestDB yourself to widen the raw window again, for example
    `ALTER TABLE "fritzbox_<serial>" SET TTL 365 DAYS;`. Disabling never deletes data or database
    objects.
