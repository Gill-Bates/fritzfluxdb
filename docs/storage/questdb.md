# QuestDB

Set `DB_TYPE=questdb` and provide `QUESTDB_HOSTNAME`. The default HTTP endpoint is port `9000`. A hostname given as URL takes its port from the URL, or
`80`/`443` when the URL has none.

```env
DB_TYPE=questdb
QUESTDB_HOSTNAME=questdb.example.invalid
QUESTDB_PORT=9000
QUESTDB_MEASUREMENT_NAME=fritzbox
QUESTDB_DATA_RETENTION_DAYS=365
QUESTDB_TLS_ENABLED=true
```

## Table names
Once the FRITZ!Box serial is known, fritzfluxdb writes two tables per FRITZ!Box:
`fritzbox_<serial>_metrics` for metrics, for example `fritzbox_50E63634FEA8_metrics`, and
`fritzbox_<serial>_events` for router log and call entries. `<serial>` is the serial with every
character other than `A-Za-z0-9_` replaced by `_`. `QUESTDB_MEASUREMENT_NAME` names the tables
`<name>_metrics` and `<name>_events` only while no serial is available. The rollup views are named
`fritzbox_<serial>_metrics_rollup_<interval>_gNN_v1`.

fritzfluxdb never touches tables with other names. Tables from earlier builds stay in the database; drop them and their rollup views manually when no longer needed.

## Requirements

QuestDB 10 or newer is required. At startup fritzfluxdb runs `SELECT build();` and stops with exit
code `78` (permanent configuration error, the container parks as `unhealthy`) when the server is
older or its version cannot be read. The same request also stops the daemon with `78` when QuestDB
answers `401` or `403` because the credentials are wrong. A server that is merely unreachable is
retried and does not stop the daemon.

## Authentication

QuestDB Open Source supports HTTP basic authentication with `http.user` and `http.password`
(or `QDB_HTTP_USER` and `QDB_HTTP_PASSWORD`) on the QuestDB server. Set `QUESTDB_USERNAME`
and `QUESTDB_PASSWORD` to the same values in fritzfluxdb. The Compose example passes these
variables only to fritzfluxdb; it does not enable authentication on the QuestDB service.
Configure the server separately before relying on them.
QuestDB REST API tokens require QuestDB Enterprise and are not supported by this OSS setup.
TLS certificate verification remains enabled by default.

## Admin CLI

The image includes the QuestDB admin CLI `fritzflux-cli`. Run it in the running fritzfluxdb
container; it uses the same QuestDB endpoint and HTTP basic credentials as the daemon.
Commands are positional words: write `doctor`, not `--doctor`. Use `--help` for the command list
and examples. Options such as `--apply`, `--from`, and `--to` follow the command they belong to.
The CLI only acts on a box table whose status row in `_fritzfluxdb_downsampling` carries the
QuestDB id of that very table. After a table was dropped and re-created, the
table appears in `list` again once fritzfluxdb has started and written a new status row.

```bash
docker compose -f docker/docker-compose.questdb.yml exec fritzfluxdb fritzflux-cli --help
docker compose -f docker/docker-compose.questdb.yml exec fritzfluxdb fritzflux-cli doctor
docker compose -f docker/docker-compose.questdb.yml exec fritzfluxdb fritzflux-cli list
```

`doctor` checks connectivity and reports QuestDB state. `list` shows the metrics table of each FRITZ!Box and its
rollup views. Use a table name from `list` for the following commands:

```bash
docker compose -f docker/docker-compose.questdb.yml exec fritzfluxdb sh
fritzflux-cli --help
fritzflux-cli partitions fritzbox_AA1234567890_metrics
fritzflux-cli drop-box fritzbox_AA1234567890_metrics
fritzflux-cli drop-all
fritzflux-cli drop-partitions fritzbox_AA1234567890_metrics --from 2026-09-01 --to 2026-09-08
fritzflux-cli refresh fritzbox_AA1234567890_metrics --full
fritzflux-cli drop-box fritzbox_AA1234567890_metrics --apply
```

Replace `fritzbox_AA1234567890_metrics` with a table name returned by `list`. Run these commands after
opening the interactive shell shown above. Mutating commands show a preview by default. Add
`--apply` to execute the previewed change without a further prompt. `drop-all` and `drop-box` pause the running daemon first so it cannot re-create the tables,
then stop it after the drop. The container keeps running, so your shell stays open and no restart
policy re-creates the tables; the healthcheck reports unhealthy. Restart the container to start the
daemon again with a fresh schema.

For `drop-partitions`, `--to` is exclusive; both dates must align with
whole UTC daily partitions. The active newest partition cannot be dropped. The operation rebuilds
dependent rollups so deleted raw metrics do not remain in their aggregates. A full refresh can take
time and is scheduled asynchronously by QuestDB.

`drop-all` removes every box table that has a status row for that very table, together with its
rollup views and the fritzfluxdb downsampling status table. `drop-all` and `drop-box` leave the
`fritzbox_<serial>_events` tables in place; drop them in QuestDB if you no longer need them.
Review the preview before `--apply`.

QuestDB Open Source cannot delete individual rows such as one attached network device. Its HTTP
password is configured on the QuestDB server, and REST API token management requires Enterprise;
neither operation is provided by this CLI. Take a backup before applying deletions.

## TTL

`QUESTDB_DATA_RETENTION_DAYS` is applied to the raw table only when it has no TTL; an existing TTL
is never overwritten by a later application start. Set the value to `0` for unlimited retention.
To change the raw-table TTL, use QuestDB administration directly, for example:

```sql
ALTER TABLE "fritzbox_<serial>_metrics" SET TTL 365 DAYS;
```

The `<serial>` placeholder is the sanitized FRITZ!Box serial; replace it with the actual table
name.

Retention behavior in detail:

- The raw table `fritzbox_<serial>_metrics` and `fritzbox_<serial>_events` use `PARTITION BY DAY`; QuestDB drops whole partitions only,
  so data can remain up to one day longer than the TTL. The TTL is always set in days.
- The events table and the rollup views belong to fritzfluxdb and follow
  `QUESTDB_DATA_RETENTION_DAYS`: at each start a differing TTL is replaced and the change is logged as
  WARNING with old and new value. A shorter value deletes older partitions.
- `0` means unlimited: an existing TTL on these objects is removed with `SET TTL 0 HOURS`, which
  QuestDB accepts for tables and materialized views alike.
- The raw table keeps its own rule: a TTL fritzfluxdb did not set is never changed.
- Rollup views get the full `QUESTDB_DATA_RETENTION_DAYS`. The raw table gets the profile window,
  capped at `QUESTDB_DATA_RETENTION_DAYS` when that is set.
- On QuestDB Enterprise, non-zero table TTL is rejected; use a
  [storage policy](https://questdb.com/docs/concepts/storage-policy/) there. Clearing TTL to zero is supported.

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

Raw polling and ingestion are unchanged; QuestDB maintains the rollup through materialized views
and applies their TTL server-side.

QuestDB's `/exec` endpoint accepts GET only (POST answers 405), so every statement travels in the
URL. To keep it short regardless of how many metrics exist, the rollup is split into up to 16 views
per box table and interval, named `<table>_rollup_<interval>_gNN_v1` (`<table>` is the metrics table), for example
`fritzbox_50E63634FEA8_metrics_rollup_1m_g04_v1`. Each gauge or counter column belongs to the group
`crc32(column name) % 16`; the assignment never changes, so new metrics do not move existing ones.
Groups without a column get no view. A gauge keeps `_sum` and `_count` (the dashboards compute the
average), a counter keeps `_last`. A statement is about 0.5 to 1.1 kB today (67 gauge and counter columns in 16 groups). One above
8,000 bytes is not sent and is logged as an error with the view name.

The views are created independently: if one fails, the others are still created, and each failure is
logged once at ERROR (repeats at DEBUG). The raw TTL is shortened only after every view is valid and has all expected columns; a view that
lacks a column (for example a metric added later) is reported once and counts as not ready, and a
raw TTL shortened earlier is not extended again. A size rejection (413, 414, 431) of one view is not
resent until the next start, while the other views are still retried.
The dashboards read each metric from its group view and merge panels that mix groups on the time
column. Retention (TTL) applies to every group view.
Every gauge and counter column is in its group view even when no dashboard reads it. State, text
and event columns are not rolled up.

!!! info "Schema contract"
    The group count (16), the hash (CRC32 of the column name) and the schema version (`_v1`) are a
    fixed contract (`app/influxdb/config.py`, pinned by a test). Columns are never re-hashed within a
    schema version. The 8,000-byte limit counts the whole request target (`/exec?query=...`). An oversized
    group is rejected with an error instead of split, because splitting would change which view
    holds a column. Today the largest group leaves more than 6 kB of reserve.

Partial setups are tracked per group: fritzfluxdb logs `QuestDB rollup groups ready: x of y`, retries
only the missing groups and publishes the downsampling status and the shorter raw TTL only when all
groups are ready. At every start the columns of each existing group view are compared with the
expected metrics; a missing one is reported once per view and keeps that group not ready until
the view is rebuilt.

**Cost of the group views** (QuestDB 10.2.0 in Docker, 24 h at 1-minute resolution, 67 real
gauge and counter columns, one box): create and initial refresh about 4.7 s, refresh delay after an
insert about 0.16 s, container memory about 650 MiB, and about 4.7 GB apparent disk size of the
views. The disk figure is the preallocated size reported by `table_storage()`, not the data volume;
each view repeats the dimension columns.

!!! note "A metric that appears later"
    `CREATE ... IF NOT EXISTS` never changes an existing view. A column that is added to the base
    table after its group view was created is missing there, so dashboards show no rollup data for
    that metric (raw data is unaffected). Drop that group view and restart fritzfluxdb to rebuild it
    from the base table: `DROP MATERIALIZED VIEW "<table>_rollup_<interval>_gNN_v1";`. fritzfluxdb
    detects this at start and logs the view, group and missing columns. History before the rebuild
    is restored only as far as the raw table still holds it (raw retention).

Use the matching QuestDB dashboards from `utils/grafana/questdb_dashboards/` after ingestion.

### Choosing a profile

With `QUESTDB_DATA_RETENTION_DAYS=365`, one year of history costs roughly:

| Setting | Full resolution | Older data | Disk after 1 year |
| --- | ---: | --- | ---: |
| unset | 365 days | — | ~12.5 GiB |
| `low` | 30 days | 1-minute rollup, 365 days | ~7.8 GiB |
| `medium` | 7 days | 1-minute rollup, 365 days | ~7.0 GiB |
| `high` | 1 day | 5-minute rollup, 365 days | ~1.4 GiB |

The two 1-minute profiles save far less than their raw window suggests: the rollup keeps two
aggregates per gauge (`_sum` and `_count`; the average is `_sum / _count`), so it costs about half a raw day per day rather than a fraction of one. Only
`high` reduces the total substantially, and only because its 5-minute buckets cut the row count.

!!! note "Basis of these figures"
    Measured on a FRITZ!Box 7590 AX writing about 10 raw rows per minute across 203 columns:
    35 MiB per raw day and 19 MiB per 1-minute rollup day, from QuestDB partition sizes. The
    5-minute figure (about 3.8 MiB per day) is extrapolated from the bucket ratio, not measured.
    Your volume scales with the number of active services, smart home devices and VPN users, so
    treat the table as an order of magnitude.

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
storage.

!!! warning "Disabling downsampling does not restore the previous raw window"
    Clearing `QUESTDB_DOWNSAMPLING` keeps existing rollup views and the raw TTL that the previous
    profile applied, and Grafana falls back to raw data only. After `medium` the dashboards
    therefore show at most the remaining 7 days, even though older rollup data still exists.
    Raise the TTL in QuestDB yourself to widen the raw window again, for example
    `ALTER TABLE "fritzbox_<serial>_metrics" SET TTL 365 DAYS;`. Disabling never deletes data or database
    objects.

## Deduplication of log and call entries

Router log lines and the call list are written to the table `fritzbox_<serial>_events` (for example
`fritzbox_50E63634FEA8_events`), not to the metrics table. The table is created with
`DEDUP UPSERT KEYS(timestamp, box, log_type, uid)`; DEDUP is enabled on an existing events table at
startup. An entry that is read again, also after a restart, overwrites its own row. The metrics
table has no DEDUP on purpose: an upsert replaces a whole row and would erase metrics of other
services written in the same second.

| Aspect | QuestDB | InfluxDB v1/v2 |
| --- | --- | --- |
| Where events live | Table `fritzbox_<serial>_events` | Metrics measurement |
| Row identity | `timestamp`, `box`, `log_type`, `uid` (hash of the message or call row) | Measurement, tags and timestamp; no `uid` tag, because it would make the series cardinality unbounded |
| Same-second entries | Separated by `uid` | Separated by a deterministic sub-second offset derived from the `uid`; written with `precision=ns` |
| Metrics write precision | `s` | `ns` |
| Retention | `QUESTDB_DATA_RETENTION_DAYS` (follows the setting); never downsampled | Retention of the database |

Every poll sends the full log and call list again; the database discards the repeats, so the cost
is network and write load, not storage.

| Guarantee or limit | Detail |
| --- | --- |
| Whole rows | A call-list entry (seven fields) is never split across requests, also after an HTTP 413, because a partial upsert would replace the complete row. An entry too large for one request is dropped whole and logged. |
| Call-list `log_type` | Call-list rows have `log_type` NULL; NULL is part of the key and deduplicates like any value. |
| Existing duplicates | DEDUP applies from the moment it is enabled. Duplicates already in an events table are not removed. |
| Identical log lines | A log message has no id in the FRITZ!Box: identical text in the same second is stored once. |
| Edited calls | The call `uid` hashes the whole CSV row; a later change of name or duration creates a second row. |
| Retries | A write that failed is repeated with the same rows and stays idempotent. |
| InfluxDB offset collisions | The offset is a 48-bit hash reduced to 10^9 values. Two different events of the same `box`/`log_type` and second collide with a probability of about n^2/(2*10^9) for n events in that second (below 10^-5 for 100 events); the later one overwrites the earlier one. |

Logs and call logs follow the retention above. Current snapshots and state histories stay in the
metrics table and are never downsampled, so their history ends with the raw window: `high` leaves
one day of them regardless of the rollup retention.

