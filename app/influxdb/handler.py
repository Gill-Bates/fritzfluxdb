#!/usr/bin/env python3
#
# app/influxdb/handler.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
import hashlib
import logging
import re
import time
import zlib
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from logging import LogRecord

import httpx
from gillbates_influxdb_client import (
    NO_RETRY,
    RETRYABLE_STATUS_CODES,
    AsyncInfluxDBClient,
    InfluxDBConfigError,
    InfluxDBError,
    InfluxDBNotFoundError,
    InfluxDBUnavailableError,
    InfluxDBVersion,
    InfluxDBWriteError,
    escape_measurement,
    escape_tag,
    format_field,
    format_line,
)
from gillbates_influxdb_client import InfluxDBConfig as InfluxClientConfig
from gillbates_influxdb_client import WritePrecision as InfluxPrecision
from gillbates_questdb_client import (
    AsyncQuestDBClient,
    QuestDBConfig,
    QuestDBConfigError,
    QuestDBError,
    QuestDBWriteError,
    compact_ident,
    db_ttl_days,
    format_exception,
    is_permanent_failure,
    quote_ident,
    quote_literal,
)

from app.fritzbox.config import FritzBoxConfig
from app.influxdb.config import (
    QUESTDB_DOWNSAMPLING_DISABLED_PROFILE,
    QUESTDB_DOWNSAMPLING_PROFILES,
    QUESTDB_DOWNSAMPLING_SCHEMA_VERSION,
    QUESTDB_DOWNSAMPLING_STATE_TABLE,
    QUESTDB_ROLLUP_GROUP_COUNT,
    InfluxDBConfig,
    QuestDBDownsamplingProfile,
)
from app.utils.common import PermanentConfigError
from app.utils.log import get_logger
from app.utils.measurement import (
    METRIC_TYPES,
    FritzMeasurement,
    WritePrecision,
    warn_unwritable,
)
from app.utils.settings import reveal

QUESTDB_MINIMUM_MAJOR_VERSION = 10
# Final flush at shutdown; part of the shutdown budget checked in tests/test_docker_contract.py.
BUFFER_FLUSH_TIMEOUT = 10.0


class DatabaseConfigError(PermanentConfigError):
    """The database rejects the configuration (credentials, database, version); restarting cannot fix it."""


def _config_error(exc: InfluxDBConfigError | QuestDBConfigError, label: str) -> DatabaseConfigError:
    """The permanent rejections of a package (HTTP 401/403/404, QuestDB too old) as the app's exit-78 error."""
    if isinstance(exc, InfluxDBNotFoundError):
        detail = f": {exc.body[:200]}" if exc.body else ""
        return DatabaseConfigError(f"{label}: {exc}{detail}")
    if isinstance(exc, InfluxDBConfigError):
        return DatabaseConfigError(
            f"{label} rejected the configured credentials or their permissions (HTTP {exc.status_code})"
        )
    return DatabaseConfigError(str(exc))


async def _as_database_config_error[T](awaitable: Awaitable[T], label: str = "") -> T:
    """The one place where the packages' permanent failures become the app's exit-78 error.

    QuestDBConfigError (HTTP 401/403) with its QuestDBVersionError subclass (server too old or unreadable)
    and InfluxDBConfigError (HTTP 401/403/404) are permanent; every other package or transport error
    keeps its class and is never a config error.
    """
    try:
        return await awaitable
    except (QuestDBConfigError, InfluxDBConfigError) as exc:
        raise _config_error(exc, label) from exc


@dataclass(frozen=True, slots=True)
class _WriteFailure:
    """A rejected write of either package, reduced to what the status handling decides on."""

    status_code: int
    body: str
    retry_after: float | None = None
    retention_drop: bool = False
    payload_rejected: bool = False

    @property
    def too_large(self) -> bool:
        return self.status_code == 413


log = get_logger()


_QUESTDB_TYPE_MAP = {
    bool: "BOOLEAN",
    int: "LONG",
    float: "DOUBLE",
    str: "VARCHAR",
}

_QUESTDB_TAG_COLUMNS = {
    "box": "SYMBOL",
    "id": "SYMBOL",
    "log_type": "SYMBOL",
    "name": "SYMBOL",
    "uid": "SYMBOL",
    "vpn_type": "SYMBOL",
}

_QUESTDB_INTERNAL_COLUMNS = {
    "message": "VARCHAR",
    "fritzfluxdb_setting_timezone": "VARCHAR",
    "wan_interruption_cause": "VARCHAR",
    "wan_interruption_confidence": "VARCHAR",
    "wan_interruption_duration": "LONG",
}

# Events live in `<measurement>_<serial>_events`, where DEDUP UPSERT KEYS collapses a re-read entry; the
# metrics table `<measurement>_<serial>_metrics` cannot use DEDUP because an upsert replaces the whole row.
QUESTDB_METRICS_TABLE_SUFFIX = "_metrics"
QUESTDB_EVENTS_TABLE_SUFFIX = "_events"
_QUESTDB_EVENT_TAG_COLUMNS = {
    "box": "SYMBOL",
    "log_type": "SYMBOL",
    "uid": "SYMBOL",
}
QUESTDB_EVENT_UPSERT_KEYS = ("timestamp", *_QUESTDB_EVENT_TAG_COLUMNS)

# Metrics behind FRITZFLUXDB_INCLUDE_VPN_ADDRESS_METRICS: the generated dashboards
# query these columns unconditionally, so the table must always provide them.
_QUESTDB_OPTIONAL_METRIC_COLUMNS = {
    "vpn_user_remote_address": "VARCHAR",
    "vpn_user_virtual_address": "VARCHAR",
}


class QuestDBMetricClass(StrEnum):
    GAUGE = "gauge"
    COUNTER = "counter"
    STATE = "state"
    EVENT = "event"
    UNKNOWN = "unknown"


_DAEMON_LOG_METRICS = frozenset({"message"})
QUESTDB_EVENT_METRICS = frozenset({"call_list_duration", "message"})
QUESTDB_GAUGE_METRICS = frozenset({
    "active_hosts_downstream",
    "active_hosts_upstream",
    "attenuation_downstream",
    "attenuation_upstream",
    "cable_channel_ds_docsis30_latency",
    "cable_channel_ds_docsis30_mse",
    "cable_num_ds_channels",
    "cable_num_us_channels",
    "cpu_temp",
    "cpu_utilization",
    "downstream_dsl_sync_max",
    "downstreammax",
    "downstreamphysicalmax",
    "dsl_line_length",
    "energy_consumption",
    "ha_battery_percent",
    "ha_heating_absenk",
    "ha_heating_battery",
    "ha_heating_komfort",
    "ha_heating_nextchange_tchange",
    "ha_heating_tist",
    "ha_heating_tsoll",
    "ha_levelcontrol_level",
    "ha_powermeter_power",
    "ha_powermeter_voltage",
    "ha_temperature",
    "ha_temperature_celsius",
    "ha_temperature_offset",
    "linkuptime",
    "maxBitRate_downstream",
    "maxBitRate_upstream",
    "num_active_host",
    "num_passive_host",
    "physical_linkuptime",
    "power_downstream",
    "power_upstream",
    "ram_usage_dynamic",
    "ram_usage_fixed",
    "ram_usage_free",
    "receiverate",
    "sendrate",
    "snr_downstream",
    "snr_upstream",
    "systemuptime",
    "upstream_dsl_sync_max",
    "upstreammax",
    "upstreamphysicalmax",
    "vpn_user_num_active",
    "wlan1_associations",
    "wlan1_channel",
    "wlan2_associations",
    "wlan2_channel",
    "wlan3_associations",
    "wlan3_channel",
    "wlan4_associations",
    "wlan4_channel",
})
QUESTDB_COUNTER_METRICS = frozenset({
    "cable_channel_ds_docsis30_corrected_errors",
    "cable_channel_ds_docsis30_non_corrected_errors",
    "cable_channel_ds_docsis31_non_corrected_errors",
    "crc_errors",
    "errored_seconds",
    "ha_powermeter_energy",
    "lan_totalbytesreceived",
    "lan_totalbytessent",
    "severely_errored_seconds",
    "totalbytesreceived",
    "totalbytessent",
})
QUESTDB_STATE_METRICS = frozenset({
    "active_hosts_ipv4_last_used",
    "active_hosts_is_mesh",
    "cable_channel_ds_docsis30_channel",
    "cable_channel_ds_docsis31_channel",
    "cable_channel_us_docsis30_channel",
    "cable_channel_us_docsis31_channel",
    "ddns_enabled",
    "ha_alert",
    "ha_battery_low",
    "ha_colorcontrol_current_mode",
    "ha_device_present",
    "ha_heating_batterylow",
    "ha_heating_boostactive",
    "ha_heating_boostactiveendtime",
    "ha_heating_devicelock",
    "ha_heating_errorcode",
    "ha_heating_holidayactive",
    "ha_heating_lock",
    "ha_heating_nextchange_endperiod",
    "ha_heating_summeractive",
    "ha_heating_windowopenactiv",
    "ha_heating_windowopenactiveendtime",
    "ha_simpleonoff_state",
    "ha_switch_devicelock",
    "ha_switch_lock",
    "ha_switch_state",
    "upgrade_available",
    "vpn_user_active",
    "vpn_user_connected",
})
_QUESTDB_ROLLUP_DIMENSIONS = ("box", "id", "name", "vpn_type")


def _questdb_metric_name(name: str) -> str:
    return str(name).replace(".", "_")


def _collect_metric_columns(metric_name: str, metric_params: object, columns: dict[str, str]) -> None:
    if isinstance(metric_params, str):
        try:
            name, type_name = metric_params.rsplit(":", maxsplit=1)
        except ValueError:
            return
        data_type = METRIC_TYPES.get(type_name)
        sql_type = _QUESTDB_TYPE_MAP.get(data_type)
        if sql_type is not None:
            columns[_questdb_metric_name(name)] = sql_type
        return

    if not isinstance(metric_params, dict):
        return

    next_metric = metric_params.get("next")
    if next_metric is not None:
        _collect_metric_columns(metric_name, next_metric, columns)
        return

    sql_type = _QUESTDB_TYPE_MAP.get(metric_params.get("type"))
    if sql_type is not None:
        columns[_questdb_metric_name(metric_name)] = sql_type


def questdb_metrics_table_name(measurement_name: str) -> str:
    return f"{measurement_name}{QUESTDB_METRICS_TABLE_SUFFIX}"


def questdb_events_table_name(measurement_name: str) -> str:
    return f"{measurement_name}{QUESTDB_EVENTS_TABLE_SUFFIX}"


def questdb_event_columns() -> dict[str, str]:
    """Columns of the events table: the identifying tags plus every metric of an event service."""
    from app.fritzbox import service_definitions

    columns = dict(_QUESTDB_EVENT_TAG_COLUMNS)
    for service in service_definitions.lua_services:
        if not service.get("event"):
            continue
        for metric_name, metric_params in service.get("value_instances", {}).items():
            _collect_metric_columns(metric_name, metric_params, columns)
    return dict(sorted(columns.items()))


def questdb_events_ddl(table_name: str) -> str:
    keys = ", ".join(quote_ident(key) for key in QUESTDB_EVENT_UPSERT_KEYS)
    tag_columns = ", ".join(f"{quote_ident(name)} {sql_type}" for name, sql_type in _QUESTDB_EVENT_TAG_COLUMNS.items())
    return (
        f"CREATE TABLE IF NOT EXISTS {quote_ident(table_name)} (timestamp TIMESTAMP, {tag_columns}) "
        f"TIMESTAMP(timestamp) PARTITION BY DAY WAL DEDUP UPSERT KEYS({keys});"
    )


def questdb_events_dedup_ddl(table_name: str) -> str:
    keys = ", ".join(quote_ident(key) for key in QUESTDB_EVENT_UPSERT_KEYS)
    return f"ALTER TABLE {quote_ident(table_name)} DEDUP ENABLE UPSERT KEYS({keys});"


def questdb_expected_columns() -> dict[str, str]:
    from app.fritzbox import service_definitions

    columns = {
        **_QUESTDB_TAG_COLUMNS,
        **_QUESTDB_INTERNAL_COLUMNS,
        **_QUESTDB_OPTIONAL_METRIC_COLUMNS,
    }

    for service in service_definitions.tr064_services:
        for metric_params in service.get("value_instances", {}).values():
            _collect_metric_columns("", metric_params, columns)

    for service in service_definitions.lua_services:
        if service.get("event"):
            continue  # event columns belong to the events table
        for metric_name, metric_params in service.get("value_instances", {}).items():
            _collect_metric_columns(metric_name, metric_params, columns)

    return dict(sorted(columns.items()))


def questdb_metric_class(metric_name: str, column_type: str | None = None) -> QuestDBMetricClass:
    """Classify known columns conservatively for server-side aggregation."""
    name = _questdb_metric_name(metric_name)
    if name in QUESTDB_EVENT_METRICS:
        return QuestDBMetricClass.EVENT
    if name in QUESTDB_STATE_METRICS:
        return QuestDBMetricClass.STATE
    if column_type == "BOOLEAN":
        return QuestDBMetricClass.STATE
    if name in QUESTDB_COUNTER_METRICS:
        return QuestDBMetricClass.COUNTER
    if name in QUESTDB_GAUGE_METRICS:
        return QuestDBMetricClass.GAUGE
    return QuestDBMetricClass.UNKNOWN


def questdb_rollup_group(column_name: str) -> int:
    """Stable rollup group of a metric column (CRC32, never the per-process randomized hash())."""
    return zlib.crc32(_questdb_metric_name(column_name).encode()) % QUESTDB_ROLLUP_GROUP_COUNT


def questdb_rollup_groups(columns: dict[str, str] | None = None) -> dict[int, dict[str, str]]:
    """Gauge and counter columns per rollup group; groups without a column are omitted.

    The single source of truth for handler, admin CLI and dashboard generator.
    """
    expected = questdb_expected_columns() if columns is None else columns
    groups: dict[int, dict[str, str]] = {}
    for name, column_type in sorted(expected.items()):
        if questdb_metric_class(name, column_type) in {QuestDBMetricClass.GAUGE, QuestDBMetricClass.COUNTER}:
            groups.setdefault(questdb_rollup_group(name), {})[name] = column_type
    return dict(sorted(groups.items()))


def questdb_rollup_view_columns(members: dict[str, str]) -> list[str]:
    """Output columns a group view must expose for its member metrics."""
    names = []
    for name, column_type in members.items():
        if questdb_metric_class(name, column_type) is QuestDBMetricClass.GAUGE:
            names.extend((f"{name}_sum", f"{name}_count"))
        else:
            names.append(f"{name}_last")
    return names


def questdb_rollup_view_prefix(table_name: str, profile: QuestDBDownsamplingProfile) -> str:
    return f"{table_name}_rollup_{profile.rollup_interval}"


def questdb_rollup_view_name(table_name: str, profile: QuestDBDownsamplingProfile, group: int) -> str:
    # The dashboard generator (tools/build_questdb_dashboards.py) derives the same name.
    return f"{questdb_rollup_view_prefix(table_name, profile)}_g{group:02d}_v{QUESTDB_DOWNSAMPLING_SCHEMA_VERSION}"


def questdb_raw_retention_days(profile: QuestDBDownsamplingProfile, total_retention_days: int) -> int:
    if total_retention_days > 0:
        return min(profile.raw_retention_days, total_retention_days)
    return profile.raw_retention_days


def questdb_rollup_ddl(
        table_name: str,
        profile: QuestDBDownsamplingProfile,
        retention_days: int,
        group: int,
        columns: dict[str, str] | None = None) -> str:
    expected = questdb_expected_columns() if columns is None else columns
    members = questdb_rollup_groups(expected).get(group)
    if not members:
        raise ValueError(f"QuestDB rollup group {group} has no explicitly classified metric")

    projections = ["timestamp"]
    projections.extend(quote_ident(name) for name in _QUESTDB_ROLLUP_DIMENSIONS if name in expected)
    alias = compact_ident
    for name, column_type in members.items():
        column = compact_ident(name)
        if questdb_metric_class(name, column_type) is QuestDBMetricClass.GAUGE:
            # The dashboards read gauges as sum / count only; min, max and last are left out so
            # the statement, which travels in a GET URL, stays small.
            projections.append(f"sum({column}) AS {alias(f'{name}_sum')}")
            projections.append(f"count({column}) AS {alias(f'{name}_count')}")
        else:
            projections.append(f"last({column}) AS {alias(f'{name}_last')}")

    view_name = questdb_rollup_view_name(table_name, profile, group)
    select_list = ", ".join(projections)
    ttl_clause = f" TTL {int(retention_days)} DAYS" if retention_days > 0 else ""
    # Single line without indentation: the statement is sent in the URL, every byte counts.
    return (
        f"CREATE MATERIALIZED VIEW IF NOT EXISTS {quote_ident(view_name)} "
        f"WITH BASE {quote_ident(table_name)} REFRESH IMMEDIATE AS ("
        f"SELECT {select_list} FROM {quote_ident(table_name)} "
        f"SAMPLE BY {profile.rollup_interval} ALIGN TO CALENDAR"
        f") PARTITION BY DAY{ttl_clause};"
    )


class InfluxHandler:
    name = "InfluxDB"
    _questdb: AsyncQuestDBClient | None = None
    connection_timeout_v1 = 2
    connection_timeout_v2 = 5
    max_measurements_buffer_size = 1_000_000
    max_measurements_per_write = 1_000
    max_measurements_buffer_warning = 80
    retry_interval = 5
    max_retry_interval = 120
    retention_warning_interval = 300

    connection_warning_interval = 60
    downsampling_retry_attempts = 30
    downsampling_retry_interval = 10

    # Immutable class defaults keep instances built without __init__ (test doubles) valid.
    _pending_config_error: DatabaseConfigError | None = None
    # True when every rollup group that is not ready failed permanently; retrying cannot help.
    _rollup_failure_permanent: bool = False

    def __init__(self, config, user_agent: str | None = None):
        self.config = InfluxDBConfig(config)
        try:
            self.version = int(self.config.version)
        except (ValueError, TypeError):
            self.version = str(self.config.version).lower()

        if self.version not in {1, 2, "questdb"}:
            # InfluxDBConfig already reported this; keep the object constructible so
            # main() can exit with EX_CONFIG instead of an unhandled ValueError
            if not self.config.parser_error:
                log.error("Unsupported database version/type: %s", self.version)
                self.config.mark_parser_error()
            self.version = 1

        if self.version == "questdb":
            self.name = "QuestDB"
        else:
            self.name = f"InfluxDB v{self.version}"

        self.questdb_version: str | None = None
        self.questdb_schema_ensured = False
        # The write client of the active backend; for QuestDB it is the same object as `_questdb`.
        self.client: AsyncInfluxDBClient | AsyncQuestDBClient | None = None
        self.init_successful = False
        self.connection_lost = False
        self.last_connection_warning: datetime | None = None
        self.out_of_retention_period_range = False
        self.last_retention_warning = None
        self.buffer = []
        self.current_retry_interval = self.retry_interval
        self.last_write_retry = None
        self.retention_buffer_sorted = False
        self.current_max_measurements_buffer_warning = self.max_measurements_buffer_warning
        self.current_measurements_per_write = self.max_measurements_per_write
        self.user_agent = user_agent
        self._write_lock = asyncio.Lock()
        self._questdb_downsampling_retry_task: asyncio.Task | None = None
        self._pending_config_error = None
        # Failure reason and permanence per rollup view.
        self._rollup_failures: dict[str, tuple[str, bool]] = {}
        self._rollup_ready_views: set[str] = set()
        # Missing expected columns per existing rollup view, kept to report each view once.
        self._rollup_incomplete: dict[str, tuple[str, ...]] = {}
        self._rollup_failure_permanent = False

    def append_measurement(self, measurement: FritzMeasurement) -> None:
        if len(self.buffer) >= self.max_measurements_buffer_size:
            drop_count = min(len(self.buffer), self._batch_size_keeping_events_whole(self.max_measurements_per_write))
            del self.buffer[:drop_count]
            log.warning(
                "%s measurement buffer is full; dropping %s oldest measurement(s)",
                self.name,
                drop_count,
            )
        self.buffer.append(measurement)
        if self.out_of_retention_period_range:
            self.retention_buffer_sorted = False

    def _note_connection_lost(self, exc: BaseException) -> None:
        """Log a throttled 'unreachable' notice and mark the connection as lost.

        Shared by _init_client and _write_data_unlocked: the first call after a
        transition logs at error level, subsequent calls are throttled to a
        warning every connection_warning_interval seconds, and calls in between
        are logged at debug level only.
        """
        now = datetime.now(UTC)
        if not self.connection_lost:
            log.error(
                "%s '%s' unreachable: %s — buffering data until connection is restored",
                self.name, self.config.hostname, format_exception(exc),
            )
            self.last_connection_warning = now
        elif (
            self.last_connection_warning is None
            or (now - self.last_connection_warning).total_seconds() >= self.connection_warning_interval
        ):
            log.warning(
                "%s '%s' still unreachable — %s measurement(s) buffered, retrying...",
                self.name, self.config.hostname, len(self.buffer),
            )
            self.last_connection_warning = now
        else:
            log.debug("%s '%s' still unreachable, retrying...", self.name, self.config.hostname)
        self.connection_lost = True

    async def close(self) -> None:
        retry_task = self._questdb_downsampling_retry_task
        if retry_task is not None and not retry_task.done():
            retry_task.cancel()
            with suppress(asyncio.CancelledError):
                await retry_task
        async with self._write_lock:
            await self._close_client()

    def _new_influx_client(self) -> AsyncInfluxDBClient:
        """Client for InfluxDB 1.x/2.x writes and the startup probe; retries stay with the write loop."""
        cfg = self.config
        # The app's own gate (InfluxDBConfig) has already decided about plain HTTP with credentials;
        # a deliberate opt-in or disabled certificate verification is passed on as allow_insecure.
        allow_insecure = bool(cfg.allow_plaintext_credentials or (cfg.tls_enabled and not cfg.verify_tls))
        try:
            client_config = InfluxClientConfig(
                host=cfg.hostname,
                port=cfg.port,
                https=cfg.tls_enabled,
                version=InfluxDBVersion.V1 if self.version == 1 else InfluxDBVersion.V2,
                token=reveal(cfg.token) or "",
                organization=cfg.organization or "",
                bucket=cfg.bucket or "",
                database=cfg.database or "",
                username=cfg.username or "",
                password=reveal(cfg.password) or "",
                verify_certificate=cfg.verify_tls,
                timeout=self.connection_timeout_v1 if self.version == 1 else self.connection_timeout_v2,
                user_agent=self.user_agent or "",
                allow_insecure=allow_insecure,
            )
        except ValueError as exc:
            raise DatabaseConfigError(f"{self.name}: invalid connection settings: {exc}") from exc
        return AsyncInfluxDBClient(client_config, retry=NO_RETRY)

    def _new_questdb_client(self) -> AsyncQuestDBClient:
        """Client for /exec and ILP writes."""
        return AsyncQuestDBClient(
            QuestDBConfig(
                host=self.config.hostname,
                port=self.config.port,
                https=self.config.tls_enabled,
                username=self.config.username,
                password=reveal(self.config.password),
                verify_certificate=self.config.verify_tls,
                timeout=self.connection_timeout_v2,
            ),
            minimum_major=QUESTDB_MINIMUM_MAJOR_VERSION,
            user_agent=self.user_agent,
        )

    async def _close_client(self) -> None:
        questdb, self._questdb = self._questdb, None
        client, self.client = self.client, None
        if questdb is not None:
            await questdb.aclose()
        if client is not None and client is not questdb:
            await client.aclose()

    async def _init_client(self) -> bool:
        if self.version == "questdb":
            self._questdb = self.client = self._new_questdb_client()
        else:
            self.client = self._new_influx_client()

        try:
            if self.version in (1, 2):
                await _as_database_config_error(self.client.probe(), self.name)
            elif self.version == "questdb":
                self.questdb_version = await self._fetch_questdb_version(self._questdb)
                # ILP auto-creates columns afterwards, so the schema is ensured once and only
                # retried (inline, then in the background) until it first succeeds.
                if not self.questdb_schema_ensured:
                    try:
                        self.questdb_schema_ensured = await self._ensure_questdb_schema()
                    except DatabaseConfigError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - schema setup is retried in the background
                        log.error("Failed to ensure QuestDB schema for '%s': %s",
                                  self._questdb_metrics_table, format_exception(exc))
                    if not self.questdb_schema_ensured and not self._rollup_failure_permanent:
                        self._schedule_questdb_downsampling_retry()
        except (httpx.HTTPError, QuestDBError, InfluxDBError) as exc:
            # A rejected build() statement is no QuestDB answer to trust: retry, never exit 78.
            self._note_connection_lost(exc)
            self.init_successful = False
            await self._close_client()
            return False
        except DatabaseConfigError:
            # Permanent: close the clients so the failed reconnect does not leak them.
            await self._close_client()
            raise

        self.init_successful = True
        if self.version == "questdb" and self.questdb_version:
            log.info("Connection to %s [v%s] established", self.name, self.questdb_version)
        else:
            log.info("Connection to %s established", self.name)
        return True

    async def _fetch_questdb_version(self, questdb: AsyncQuestDBClient) -> str:
        """Query build() and enforce the minimum. Transport errors propagate as httpx.HTTPError."""
        return await _as_database_config_error(questdb.fetch_version())

    async def preflight(self) -> None:
        """Startup gate: raises DatabaseConfigError for rejected settings, tolerates an unreachable server."""
        try:
            if self.version == "questdb":
                async with self._new_questdb_client() as questdb:
                    self.questdb_version = await self._fetch_questdb_version(questdb)
            else:
                async with self._new_influx_client() as client:
                    await _as_database_config_error(client.probe(), self.name)
        except (httpx.HTTPStatusError, InfluxDBError) as exc:
            # A server that answered, whatever the status, is reachable but not what the settings promise.
            if isinstance(exc, httpx.HTTPStatusError):
                answered, body = True, exc.response.text
            else:
                answered, body = exc.status_code is not None, exc.body
            if answered:
                log.warning("%s preflight postponed, unexpected answer: %s: %.200s",
                            self.name, format_exception(exc), body)
            else:
                log.warning("%s preflight postponed, server not reachable: %s", self.name, format_exception(exc))
            return
        except (httpx.HTTPError, QuestDBError) as exc:
            log.warning("%s preflight postponed, server not reachable: %s", self.name, format_exception(exc))
            return
        log.info("%s preflight passed for '%s'", self.name, self.config.hostname)

    async def _questdb_call[T](self, call: Callable[[AsyncQuestDBClient], Awaitable[T]]) -> T:
        """Run one package call; the single /exec boundary of the handler."""
        if self._questdb is None:
            raise RuntimeError("QuestDB client is not initialized")
        return await _as_database_config_error(call(self._questdb))

    async def _questdb_exec(self, query: str) -> list[list]:
        return await self._questdb_call(lambda q: q.execute(query))

    @property
    def _questdb_metrics_table(self) -> str:
        return questdb_metrics_table_name(self.config.measurement_name)

    async def _ensure_questdb_schema(self) -> bool:
        # A previous rollup failure must not suppress retries of an earlier schema-setup step.
        self._rollup_failure_permanent = False
        table_name = self._questdb_metrics_table
        table_ident = quote_ident(table_name)

        await self._questdb_exec(
            f"CREATE TABLE IF NOT EXISTS {table_ident} (timestamp TIMESTAMP) "
            "TIMESTAMP(timestamp) PARTITION BY DAY;"
        )

        columns = questdb_expected_columns()
        for column_name, column_type in columns.items():
            column_ident = quote_ident(column_name)
            await self._questdb_exec(
                f"ALTER TABLE {table_ident} ADD COLUMN IF NOT EXISTS {column_ident} {column_type};"
            )

        log.debug("Ensured QuestDB schema for %s with %s expected columns", table_name, len(columns))

        if not await self._ensure_questdb_events_table():
            return False

        profile_name = self.config.downsampling
        if not profile_name:
            # A TTL is applied whenever the table has none, so a retention enabled after a first
            # start with 0 (or a failed first attempt) still takes effect; an existing TTL stays.
            previously_managed = await self._questdb_raw_ttl_is_managed()
            raw_ttl_managed = await self._ensure_questdb_ttl() or previously_managed
            return await self._set_questdb_downsampling_state(
                enabled=False,
                profile_name=QUESTDB_DOWNSAMPLING_DISABLED_PROFILE,
                raw_ttl_managed=raw_ttl_managed
            )

        profile = QUESTDB_DOWNSAMPLING_PROFILES[profile_name]
        return await self._finalize_questdb_downsampling(profile_name, profile)

    async def _ensure_questdb_events_table(self) -> bool:
        """Creates the events table with DEDUP and repairs an existing one. False: TTL not verified."""
        events_table = questdb_events_table_name(self.config.measurement_name)
        events_ident = quote_ident(events_table)

        await self._questdb_exec(questdb_events_ddl(events_table))
        for column_name, column_type in questdb_event_columns().items():
            await self._questdb_exec(
                f"ALTER TABLE {events_ident} ADD COLUMN IF NOT EXISTS "
                f"{quote_ident(column_name)} {column_type};"
            )

        # A table auto-created through ILP carries no keys; inspect first
        # so an unchanged table is not altered on every start.
        current = await self._questdb_exec(
            f"SELECT \"column\" FROM table_columns({quote_literal(events_table)}) WHERE upsertKey;"
        )
        current_keys = {row[0] for row in current}
        if current_keys != set(QUESTDB_EVENT_UPSERT_KEYS):
            await self._questdb_exec(questdb_events_dedup_ddl(events_table))
            log.info("Enabled QuestDB DEDUP on events table '%s'", events_table)

        return await self._set_questdb_table_ttl(events_table, self.config.data_retention_days)

    async def _finalize_questdb_downsampling(
            self,
            profile_name: str,
            profile: QuestDBDownsamplingProfile) -> bool:
        table_name = self._questdb_metrics_table
        raw_retention_days = questdb_raw_retention_days(profile, self.config.data_retention_days)
        log.info(
            "QuestDB downsampling profile: %s (raw %s day(s), rollup %s / %s day(s))",
            profile_name,
            raw_retention_days,
            profile.rollup_interval,
            self.config.data_retention_days,
        )
        if not await self._ensure_questdb_downsampling(profile):
            return False

        # The raw TTL is applied and verified before the status is published, so Grafana never
        # reads a raw window that QuestDB does not actually enforce.
        applied = await self._apply_questdb_raw_ttl(
            table_name,
            raw_retention_days,
            previously_managed=await self._questdb_raw_ttl_is_managed()
        )
        if applied is None:
            return False
        effective_raw_days, raw_ttl_managed = applied

        return await self._set_questdb_downsampling_state(
            enabled=True,
            profile_name=profile_name,
            raw_retention_days=effective_raw_days,
            raw_ttl_managed=raw_ttl_managed,
            rollup_interval=profile.rollup_interval,
            rollup_view=questdb_rollup_view_prefix(table_name, profile)
        )

    def _schedule_questdb_downsampling_retry(self) -> None:
        task = self._questdb_downsampling_retry_task
        if task is None or task.done():
            self._questdb_downsampling_retry_task = asyncio.create_task(
                self._retry_questdb_downsampling_setup()
            )

    async def _retry_questdb_downsampling_setup(self) -> None:
        try:
            for attempt in range(1, self.downsampling_retry_attempts + 1):
                await asyncio.sleep(self.downsampling_retry_interval)
                if self.client is None:
                    return
                try:
                    # The write lock keeps a concurrent write failure from replacing
                    # self.client mid-provisioning.
                    async with self._write_lock:
                        if self.client is None:
                            return
                        # The full schema path, so a table or column that failed to be created
                        # is repaired as well, not only the rollup finalization.
                        ready = await self._ensure_questdb_schema()
                except DatabaseConfigError as exc:
                    # Surfaced by the next write, which ends the daemon with exit code 78.
                    self._pending_config_error = exc
                    return
                except Exception as exc:  # noqa: BLE001 - reconnect will get another bounded retry sequence
                    log.warning(
                        "QuestDB downsampling retry %s/%s failed: %s",
                        attempt, self.downsampling_retry_attempts, format_exception(exc),
                    )
                    continue
                if ready:
                    self.questdb_schema_ensured = True
                    log.info("QuestDB downsampling provisioning completed after async refresh")
                    return
                if self._rollup_failure_permanent:
                    log.error(
                        "QuestDB downsampling provisioning failed permanently (%s); retries stopped, "
                        "raw TTL remains unchanged. Fix the cause and restart.",
                        self._rollup_failure_reason,
                    )
                    return
            log.error(
                "QuestDB downsampling did not become ready after %s retries; raw TTL remains unchanged",
                self.downsampling_retry_attempts,
            )
        finally:
            self._questdb_downsampling_retry_task = None

    @property
    def _rollup_failure_reason(self) -> str:
        return "; ".join(sorted({reason for reason, _ in self._rollup_failures.values()}))

    async def _ensure_questdb_downsampling(self, profile: QuestDBDownsamplingProfile) -> bool:
        """Provision every rollup group view; one failing view does not block the others."""
        table_name = self._questdb_metrics_table
        groups = questdb_rollup_groups()
        if not groups:
            log.error("QuestDB rollup has no explicitly classified metric; downsampling is not provisioned")
            return False
        ready = self._rollup_ready_views
        for group in groups:
            view_name = questdb_rollup_view_name(table_name, profile, group)
            if view_name in ready:
                continue  # already created, valid and checked: only missing groups are retried
            failure = self._rollup_failures.get(view_name)
            if failure and failure[1]:
                continue  # permanent failure: resending the identical statement cannot succeed
            if await self._ensure_questdb_rollup_view(table_name, profile, group):
                ready.add(view_name)
        pending = [
            name for name in (questdb_rollup_view_name(table_name, profile, g) for g in groups)
            if name not in ready
        ]
        self._rollup_failure_permanent = bool(pending) and all(
            self._rollup_failures.get(name, ("", False))[1] for name in pending
        )
        log.info("QuestDB rollup groups ready: %s of %s", len(ready), len(groups))
        return len(ready) == len(groups)

    async def _ensure_questdb_rollup_view(
            self,
            table_name: str,
            profile: QuestDBDownsamplingProfile,
            group: int) -> bool:
        view_name = questdb_rollup_view_name(table_name, profile, group)
        try:
            await self._questdb_exec(
                questdb_rollup_ddl(table_name, profile, self.config.data_retention_days, group)
            )
            status = await self._questdb_exec(
                "SELECT view_name, base_table_name, view_status, "
                "refresh_base_table_txn, base_table_txn FROM materialized_views() "
                f"WHERE view_name = {quote_literal(view_name)};"
            )
        except DatabaseConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - optional provisioning must not stop raw ingestion
            reason = format_exception(exc)
            # The retry loop repeats this every interval: report a repeating failure once at ERROR.
            previous = self._rollup_failures.get(view_name)
            level = logging.DEBUG if previous and previous[0] == reason else logging.ERROR
            self._rollup_failures[view_name] = (reason, is_permanent_failure(exc))
            log.log(level, "Unable to provision QuestDB rollup view '%s' (CREATE MATERIALIZED VIEW): %s",
                    view_name, reason)
            return False
        self._rollup_failures.pop(view_name, None)

        rows = status
        if not rows:
            log.warning("QuestDB rollup view '%s' is not visible yet; raw TTL is unchanged", view_name)
            return False

        actual_view, actual_base, view_status, refresh_txn, base_txn = rows[0][:5]
        refresh_complete = (
            str(actual_view) == view_name
            and str(actual_base) == table_name
            and str(view_status).lower() == "valid"
            and isinstance(refresh_txn, int)
            and isinstance(base_txn, int)
            and refresh_txn >= base_txn
        )
        if not refresh_complete:
            # A view that is still building is the normal state after (re-)creation.
            level = logging.INFO if str(view_status).lower() == "refreshing" else logging.WARNING
            log.log(
                level,
                "QuestDB rollup view '%s' is not fully refreshed (status=%s, refresh txn=%s, base txn=%s); "
                "raw TTL is unchanged",
                view_name, view_status, refresh_txn, base_txn,
            )
            return False

        if not await self._view_columns_complete(table_name, view_name, group):
            return False

        if not await self._set_questdb_table_ttl(
                view_name,
                self.config.data_retention_days,
                materialized_view=True):
            return False
        log.debug("QuestDB rollup view '%s' is valid and current", view_name)
        return True

    async def _view_columns_complete(self, table_name: str, view_name: str, group: int) -> bool:
        """False when an existing group view lacks expected columns; never drops or alters it.

        An incomplete view is not ready, so the raw TTL is not shortened and no status is
        published. A raw TTL that was shortened earlier is deliberately not extended: that
        could not restore expired rows and would grow storage without notice.
        """
        expected = questdb_rollup_view_columns(questdb_rollup_groups()[group])
        try:
            result = await self._questdb_exec(
                f"SELECT \"column\" FROM table_columns({quote_literal(view_name)});")
        except DatabaseConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - unverified columns keep the group not ready
            log.warning("Unable to compare columns of QuestDB rollup view '%s': %s; raw TTL is unchanged",
                        view_name, format_exception(exc))
            return False
        present = {row[0] for row in result}
        missing = tuple(sorted(set(expected) - present))
        if not missing:
            self._rollup_incomplete.pop(view_name, None)
            return True
        repeated = self._rollup_incomplete.get(view_name) == missing
        self._rollup_incomplete[view_name] = missing
        if repeated:
            log.debug("QuestDB rollup view '%s' still lacks columns %s", view_name, ", ".join(missing))
            return False
        horizon = await self._raw_history_horizon(table_name, missing)
        log.error(
            "QuestDB rollup view '%s' (table '%s', group %02d) lacks columns %s, so the group is not ready: "
            "the raw retention is not shortened any further and a raw TTL shortened earlier is not extended "
            "automatically (disk risk). %s Every further day without a rebuild moves this limit forward. "
            "Fix: DROP MATERIALIZED VIEW \"%s\"; then restart fritzfluxdb; the view is rebuilt from the "
            "remaining raw data.",
            view_name, table_name, group, ", ".join(missing), horizon, view_name,
        )
        return False

    async def _raw_history_horizon(self, table_name: str, missing: tuple[str, ...]) -> str:
        """Describe how far back the affected metrics can still be rebuilt from raw data."""
        parts = []
        try:
            existing = await self._read_questdb_table_ttl(table_name)
            days = db_ttl_days(*existing) if existing and existing[0] else None
            if days:
                parts.append(f"The raw TTL is {days} day(s), so history of these metrics older than "
                             f"now minus {days} day(s) is no longer recoverable.")
            else:
                parts.append("The raw table has no TTL (or an unknown unit).")
            for column in sorted({re.sub(r"_(sum|count|last)$", "", name) for name in missing})[:5]:
                result = await self._questdb_exec(
                    f"SELECT min(timestamp) FROM {quote_ident(table_name)} "
                    f"WHERE {quote_ident(column)} IS NOT NULL;")
                rows = result
                if rows and rows[0] and rows[0][0]:
                    parts.append(f"Oldest raw value of '{column}': {rows[0][0]}; older history is not recoverable.")
        except DatabaseConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - the horizon is advisory; the missing columns are reported anyway
            parts.append(f"The raw data horizon could not be read ({format_exception(exc)}).")
        return " ".join(parts)

    async def _read_questdb_downsampling_state(self) -> list | None:
        """Latest status row for this measurement, or None when it is unavailable.

        Creates the status table if missing so a caller can rely on the columns being present.
        """
        state_table = quote_ident(QUESTDB_DOWNSAMPLING_STATE_TABLE)
        try:
            await self._questdb_exec(
                f"CREATE TABLE IF NOT EXISTS {state_table} ("
                "timestamp TIMESTAMP, measurement SYMBOL, profile SYMBOL, schema_version LONG, "
                "enabled BOOLEAN, raw_days LONG, rollup_interval SYMBOL, rollup_view SYMBOL, "
                "raw_ttl_managed BOOLEAN, table_id LONG) "
                "TIMESTAMP(timestamp) PARTITION BY DAY;"
            )
            current = await self._questdb_exec(
                "SELECT profile, schema_version, enabled, raw_days, rollup_interval, rollup_view, "
                f"raw_ttl_managed, table_id FROM {state_table} "
                f"WHERE measurement = {quote_literal(self._questdb_metrics_table)} "
                "LATEST ON timestamp PARTITION BY measurement;"
            )
        except DatabaseConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - state reporting must not stop raw ingestion
            log.error(
                "Unable to read QuestDB downsampling state for '%s': %s",
                self._questdb_metrics_table, format_exception(exc),
            )
            return None

        return list(current[0][:8]) if current else []

    async def _questdb_table_id(self) -> int | None:
        """QuestDB's id of the current raw table, which changes when a table is re-created."""
        try:
            ids = await self._questdb_call(lambda q: q.table_ids([self._questdb_metrics_table]))
        except DatabaseConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - the id is an ownership hint, not required
            log.debug("Unable to read the QuestDB table id: %s", format_exception(exc))
            return None
        value = ids.get(self._questdb_metrics_table)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    async def _questdb_raw_ttl_is_managed(self) -> bool:
        """True only if a previous run recorded the raw TTL as set by fritzfluxdb.

        An unreadable status or table id counts as administrator-owned, so an existing TTL of unknown
        origin is never replaced; a status row of a dropped predecessor table carries another id.
        """
        state = await self._read_questdb_downsampling_state()
        if not state:
            return False
        table_id = await self._questdb_table_id()
        return table_id is not None and state[7] == table_id and state[6] is True

    async def _set_questdb_downsampling_state(
            self,
            *,
            enabled: bool,
            profile_name: str = "",
            raw_retention_days: int = 0,
            raw_ttl_managed: bool = False,
            rollup_interval: str = "",
            rollup_view: str = "") -> bool:
        measurement = self._questdb_metrics_table
        current = await self._read_questdb_downsampling_state()
        if current is None:
            return False

        expected = [
            profile_name,
            QUESTDB_DOWNSAMPLING_SCHEMA_VERSION,
            enabled,
            raw_retention_days,
            rollup_interval,
            rollup_view,
            raw_ttl_managed,
        ]
        # The stored table id binds the status row to this table instance (see questdb_admin).
        table_id = await self._questdb_table_id()
        stored_id = current[7] if current else None
        if current[:7] == expected and (table_id is None or stored_id == table_id):
            return True

        try:
            await self._questdb_exec(
                f"INSERT INTO {quote_ident(QUESTDB_DOWNSAMPLING_STATE_TABLE)} "
                "(timestamp, measurement, profile, schema_version, enabled, raw_days, "
                "rollup_interval, rollup_view, raw_ttl_managed, table_id) VALUES ("
                f"now(), {quote_literal(measurement)}, {quote_literal(profile_name)}, "
                f"{QUESTDB_DOWNSAMPLING_SCHEMA_VERSION}, {'true' if enabled else 'false'}, "
                f"{int(raw_retention_days)}, "
                f"{quote_literal(rollup_interval)}, {quote_literal(rollup_view)}, "
                f"{'true' if raw_ttl_managed else 'false'}, "
                f"{'NULL' if table_id is None else table_id});"
            )
        except DatabaseConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - state reporting must not stop raw ingestion
            log.error("Unable to update QuestDB downsampling state for '%s': %s", measurement, format_exception(exc))
            return False

        if enabled:
            log.info(
                "Published active QuestDB downsampling state for '%s' (raw %s day(s))",
                measurement, raw_retention_days,
            )
        else:
            log.warning(
                "QuestDB downsampling is disabled for '%s'; existing rollups and a raw TTL shortened by an "
                "earlier profile are retained, so Grafana shows only the remaining raw window",
                measurement,
            )
        return True

    async def _ensure_questdb_ttl(self) -> bool:
        """
        Applies data_retention_days as table TTL, but only to a table without a TTL: a TTL
        configured by the database admin always wins. Transport errors propagate; `_init_client`
        logs them and schedules the background schema retry. Returns True when this call set the
        TTL, which makes it a fritzfluxdb-managed TTL.
        """
        retention_days = self.config.data_retention_days
        if not retention_days:
            return False

        table_name = self._questdb_metrics_table
        existing = await self._read_questdb_table_ttl(table_name)
        if existing is None:
            return False
        if existing[0]:
            log.info("QuestDB table '%s' keeps its existing TTL of %s %s", table_name, *existing)
            return False

        return await self._alter_questdb_table_ttl(table_name, retention_days)

    async def _read_questdb_table_ttl(self, table_name: str) -> tuple[int, str] | None:
        """Current TTL as (value, unit), or None when it cannot be determined."""
        try:
            existing = await self._questdb_call(lambda q: q.read_ttl(table_name))
        except (httpx.HTTPStatusError, QuestDBError, RuntimeError) as exc:
            log.warning(
                "Unable to read TTL of QuestDB table '%s'; "
                "data retention is not applied: %s",
                table_name, format_exception(exc),
            )
            return None

        if existing is None:
            log.warning("QuestDB table '%s' not found; data retention is not applied", table_name)
        return existing

    async def _apply_questdb_raw_ttl(
            self,
            table_name: str,
            requested_days: int,
            *,
            previously_managed: bool) -> tuple[int, bool] | None:
        """Resolve the raw TTL for a downsampling profile.

        Returns the verified (effective_raw_days, managed) pair, or None when the raw TTL
        could not be established — the caller then publishes no status at all. A TTL that
        fritzfluxdb did not set belongs to the administrator and is kept, so the effective
        value is published instead of the profile's requested one.
        """
        existing = await self._read_questdb_table_ttl(table_name)
        if existing is None:
            return None

        ttl_value, ttl_unit = existing
        existing_days = db_ttl_days(ttl_value, ttl_unit)
        if ttl_value and existing_days == int(requested_days):
            return int(requested_days), previously_managed

        if ttl_value and not previously_managed:
            if existing_days is None:
                log.warning(
                    "QuestDB table '%s' keeps its administrator TTL of %s %s; raw history is "
                    "reported as unavailable because the unit cannot be expressed in days",
                    table_name, ttl_value, ttl_unit,
                )
                return 0, False
            log.warning(
                "QuestDB table '%s' keeps its administrator TTL of %s day(s) instead of the "
                "profile's %s day(s); change it in QuestDB to reduce raw storage",
                table_name, existing_days, requested_days,
            )
            return existing_days, False

        if ttl_value:
            log.info(
                "Replacing the fritzfluxdb-managed TTL of QuestDB table '%s' (%s %s) with %s day(s)",
                table_name, ttl_value, ttl_unit, requested_days,
            )
        if not await self._alter_questdb_table_ttl(table_name, requested_days):
            return None

        verified = await self._read_questdb_table_ttl(table_name)
        if verified is None or db_ttl_days(*verified) != int(requested_days):
            log.warning(
                "QuestDB raw TTL of '%s' is not %s day(s) after the change; "
                "no downsampling status is published",
                table_name, requested_days,
            )
            return None
        return int(requested_days), True

    async def _set_questdb_table_ttl(
            self,
            table_name: str,
            retention_days: int,
            *,
            materialized_view: bool = False) -> bool:
        """Makes an object owned by fritzfluxdb (events table, rollup view) follow retention_days.

        A differing TTL is replaced; 0 (unlimited) removes it. The raw table never goes
        through here because it may carry an administrator TTL.
        """
        existing = await self._read_questdb_table_ttl(table_name)
        if existing is None:
            return False

        ttl_value, ttl_unit = existing
        target = max(int(retention_days), 0)
        current_days = db_ttl_days(ttl_value, ttl_unit) if ttl_value else 0
        if current_days == target:
            return True
        if ttl_value:
            log.warning(
                "Changing the TTL of QuestDB %s '%s' from %s %s to %s; a shorter TTL deletes older partitions",
                "rollup view" if materialized_view else "table", table_name, ttl_value, ttl_unit,
                f"{target} day(s)" if target else "unlimited",
            )
        return await self._alter_questdb_table_ttl(
            table_name, target, materialized_view=materialized_view)

    async def _alter_questdb_table_ttl(
            self,
            table_name: str,
            retention_days: int,
            *,
            materialized_view: bool = False) -> bool:
        try:
            await self._questdb_call(
                lambda q: q.set_ttl(table_name, retention_days, materialized_view=materialized_view)
            )
        except (httpx.HTTPStatusError, QuestDBError, RuntimeError) as exc:
            log.warning(
                "Unable to set TTL of %s day(s) on QuestDB table '%s': %s",
                retention_days, table_name, format_exception(exc),
            )
            return False

        log.info("Set TTL of QuestDB table '%s' to %s", table_name,
                 f"{retention_days} day(s)" if int(retention_days) > 0 else "unlimited")
        return True

    def _bundle_candidates(self, measurements: list):
        """Measurements that can be written at all, with their field name; the rest is logged and skipped."""
        for measurement in measurements:
            if not isinstance(measurement, FritzMeasurement):
                log.error("Measurement needs to be a 'FritzMeasurement' but got '%s'", type(measurement))
                continue

            if measurement.timestamp_precision != WritePrecision.S:
                # The write requests always declare precision=s; a ms/us timestamp would be
                # stored in the wrong unit, so it is rejected instead of sent.
                log.error(
                    "Dropping %s measurement '%s': timestamp precision '%s' is unsupported, only '%s' is written",
                    self.name, measurement.name, measurement.timestamp_precision, WritePrecision.S,
                )
                continue

            yield measurement

    def bundle_measurements(self, measurements: list) -> tuple[list[str], list[FritzMeasurement]]:
        """
        Converts measurements into line protocol, merging all fields which share the same
        series (measurement + tags) and timestamp into a single line.

        QuestDB stores every line as its own row with a slot for each table column, so one
        line per field multiplies the storage footprint. InfluxDB treats a merged line exactly
        like the separate lines it replaces. A field name that already exists on a pending line
        starts a new line instead of overwriting the earlier value.

        Returns the line protocol lines and the measurements that could be converted.
        Only second-precision timestamps are accepted. QuestDB is written with precision=s, InfluxDB
        with precision=ns so events can carry a deterministic sub-second offset.
        """
        if self.version == "questdb":
            return self._bundle_questdb(measurements)
        return self._bundle_influx(measurements)

    def _bundle_questdb(self, measurements: list) -> tuple[list[str], list[FritzMeasurement]]:
        # The QuestDB flavor of line protocol (doubled backslashes in tags, text line breaks) stays
        # here: no package offers it and InfluxDB reads the same bytes differently.
        lines: list[tuple[str, int, dict[str, str]]] = []
        open_lines: dict[tuple[str, int], list[int]] = {}
        valid_measurements: list[FritzMeasurement] = []

        for measurement in self._bundle_candidates(measurements):
            # QuestDB rejects dots in column names (e.g. "802.11" in wlan metric names)
            field_name = measurement.name.replace(".", "_")

            field = measurement.line_protocol_field(field_name)
            if not field:
                continue

            table_name = self._questdb_metrics_table
            # Daemon log lines keep the main table's `message` column; its uid would only add
            # unbounded symbol cardinality there, and no DEDUP can merge the rows.
            daemon_log = measurement.is_event and measurement.name in _DAEMON_LOG_METRICS
            if measurement.is_event and not daemon_log:
                table_name = questdb_events_table_name(self.config.measurement_name)
            try:
                series = measurement.line_protocol_series(
                    table_name, exclude_tags=frozenset({"uid"}) if daemon_log else frozenset())
            except ValueError as exc:
                log.error("Dropping %s measurement '%s': %s", self.name, measurement.name, exc)
                continue
            key = (series, measurement.line_protocol_timestamp())
            line_indexes = open_lines.setdefault(key, [])
            for index in line_indexes:
                if field_name not in lines[index][2]:
                    lines[index][2][field_name] = field
                    break
            else:
                line_indexes.append(len(lines))
                lines.append((key[0], key[1], {field_name: field}))

            valid_measurements.append(measurement)

        data_lines = [f"{series} {','.join(fields.values())} {timestamp}" for series, timestamp, fields in lines]
        return data_lines, valid_measurements

    def _bundle_influx(self, measurements: list) -> tuple[list[str], list[FritzMeasurement]]:
        """InfluxDB lines: the app picks series, fields and timestamps, the package escapes and formats them."""
        lines: list[tuple[str, dict[str, str], int, dict[str, object]]] = []
        open_lines: dict[tuple[str, tuple, int], list[int]] = {}
        valid_measurements: list[FritzMeasurement] = []

        for measurement in self._bundle_candidates(measurements):
            field_name = measurement.name
            value = measurement.value
            if value is None:
                continue
            if isinstance(value, str):
                # Line breaks stay visible as the two characters \n and \r, as stored before.
                value = value.replace("\r", "\\r").replace("\n", "\\n")
            try:
                escape_tag(field_name)
            except ValueError:
                log.error("Dropping %s measurement '%s': the field name cannot be written", self.name, field_name)
                continue
            try:
                format_field(value)
            except (ValueError, TypeError) as exc:
                warn_unwritable(measurement.name, str(exc))
                continue

            # Daemon log lines and events carry no uid tag here; the offset below tells events apart.
            event_uid = measurement.is_event
            tags = self._influx_tags(measurement, frozenset({"uid"}) if event_uid else frozenset())
            if tags is None:
                continue
            table_name = measurement.measurement if measurement.measurement is not None else self.config.measurement_name
            try:
                escape_measurement(table_name)
                if not table_name:
                    raise ValueError("empty measurement name")
            except ValueError:
                log.error("Dropping %s measurement '%s': its measurement name cannot be written", self.name, field_name)
                continue
            timestamp = (measurement.line_protocol_timestamp() * 1_000_000_000
                         + (self._event_offset_ns(measurement) if event_uid else 0))
            key = (table_name, tuple(sorted(tags.items())), timestamp)
            line_indexes = open_lines.setdefault(key, [])
            for index in line_indexes:
                if field_name not in lines[index][3]:
                    lines[index][3][field_name] = value
                    break
            else:
                line_indexes.append(len(lines))
                lines.append((table_name, tags, timestamp, {field_name: value}))

            valid_measurements.append(measurement)

        data_lines = [
            format_line(table_name, tags, fields, timestamp, precision=InfluxPrecision.NS)
            for table_name, tags, timestamp, fields in lines
        ]
        return data_lines, valid_measurements

    def _influx_tags(self, measurement: FritzMeasurement, exclude: frozenset[str]) -> dict[str, str] | None:
        """Tag set of a point, or None (logged) when a tag cannot be written; the point is then dropped.

        A tag that InfluxDB cannot store drops the whole measurement, never just the tag, so a series
        never silently changes identity. The messages never carry the tag value.
        """
        tags = {
            str(k): str(v) for k, v in measurement.tags.items()
            if k not in exclude and v is not None and str(v) != ""
        }
        problem = None
        for key, value in tags.items():
            if "\r" in key or "\n" in key or "\r" in value or "\n" in value or not key or "\\" in key:
                problem = "a tag contains a line break, is empty or has a backslash in its key"
            elif value.endswith("\\"):
                problem = "InfluxDB cannot represent a tag value ending with a backslash"
            else:
                try:
                    escape_tag(key)
                    escape_tag(value)
                except ValueError:
                    problem = "InfluxDB cannot represent a backslash before a separator in a tag"
            if problem:
                log.error("Dropping %s measurement '%s': %s", self.name, measurement.name, problem)
                return None
        return tags

    @staticmethod
    def _event_offset_ns(measurement: FritzMeasurement) -> int:
        """Deterministic sub-second offset from the event uid.

        InfluxDB identifies a point by series and timestamp. Without a uid tag (unbounded series
        cardinality), two different events in the same second are told apart by this offset, while
        a re-read event maps to the identical point and is overwritten.
        """
        uid = (measurement.additional_tags or {}).get("uid")
        if not uid:
            return 0
        return int(hashlib.sha256(str(uid).encode("UTF-8")).hexdigest()[:12], 16) % 1_000_000_000

    @staticmethod
    def _event_group_key(measurement: FritzMeasurement) -> tuple:
        return (tuple(sorted(measurement.tags.items())), measurement.timestamp)

    def _batch_size_keeping_events_whole(self, size: int | None = None) -> int:
        """Batch length that never cuts through the measurements of one event.

        A call-list entry is several measurements with the same tags and timestamp. QuestDB
        upserts a partial row over the complete one, so the entry must reach the server in one
        request. The batch grows past the limit up to the end of the entry.
        """
        if size is None:
            size = self.current_measurements_per_write
        buffer = self.buffer
        while 0 < size < len(buffer):
            previous, following = buffer[size - 1], buffer[size]
            if not (previous.is_event and following.is_event
                    and self._event_group_key(previous) == self._event_group_key(following)):
                break
            size += 1
        return size

    def _double_retry_interval(self) -> None:
        self.current_retry_interval = min(self.current_retry_interval * 2, self.max_retry_interval)

    def permitted_to_write_data(self):
        if self.last_write_retry is None:
            return True
        self.current_retry_interval = min(self.current_retry_interval, self.max_retry_interval)
        return (datetime.now(UTC) - self.last_write_retry).total_seconds() >= self.current_retry_interval

    async def _flush_buffer_before_close(self, timeout: float = BUFFER_FLUSH_TIMEOUT) -> None:
        if not self.buffer:
            return
        try:
            async with asyncio.timeout(timeout):
                while self.buffer:
                    before = len(self.buffer)
                    await self.write_data(force=True)
                    if len(self.buffer) >= before:
                        break
        except TimeoutError:
            log.warning(
                "Timed out while flushing %s buffered %s measurement(s) before shutdown",
                len(self.buffer),
                self.name,
            )
        except PermanentConfigError:
            raise
        except Exception:  # noqa: BLE001 - shutdown must still report the loss and close the client
            log.exception("Final flush of %s measurement(s) failed", self.name)
        finally:
            if self.buffer:
                # task_done() on the input queue only confirms hand-over to this buffer, so
                # anything left here was never persisted.
                log.error(
                    "Shutting down with %s unwritten %s measurement(s); they are lost "
                    "because the buffer is memory-only",
                    len(self.buffer),
                    self.name,
                )

    async def write_data(self, *, force: bool = False):
        async with self._write_lock:
            await self._write_data_unlocked(force=force)

    async def _write_data_unlocked(self, *, force: bool = False):
        if self._pending_config_error is not None:
            raise self._pending_config_error
        # the backoff must gate the reconnect too, otherwise a lost connection is
        # re-dialled on every loop iteration and the retry interval has no effect
        if not force and not self.permitted_to_write_data():
            return
        if len(self.buffer) == 0:
            return
        if self.client is None and not await self._init_client():
            # a failed reconnect counts as a write attempt so the backoff advances
            self.last_write_retry = datetime.now(UTC)
            self._double_retry_interval()
            return

        if self.out_of_retention_period_range and not self.retention_buffer_sorted:
            self.buffer.sort(key=lambda m: m.timestamp, reverse=True)
            self.retention_buffer_sorted = True

        local_buffer = self.buffer[:self._batch_size_keeping_events_whole()]

        data_lines, valid_measurements = self.bundle_measurements(local_buffer)
        invalid_count = len(local_buffer) - len(valid_measurements)
        if invalid_count:
            log.error("Dropping %s invalid %s measurement(s) from current batch", invalid_count, self.name)
            # Remove invalid entries from the buffer immediately so they are not
            # retried on the next write attempt after a retryable failure.
            self.buffer[:len(local_buffer)] = valid_measurements
            local_buffer = valid_measurements

        if not data_lines:
            return

        write_successful = False
        _interval_updated = False
        self.last_write_retry = datetime.now(UTC)

        failure: _WriteFailure | None = None
        splits = 0
        try:
            if self.version == "questdb":
                await self.client.write("\n".join(data_lines), precision="s")
            else:
                # One request per batch keeps an event whole; the package halves a 413 on its own.
                result = await self.client.write(data_lines, precision=InfluxPrecision.NS, batch_size=len(data_lines))
                splits = result.splits
            write_successful = True
        except (QuestDBConfigError, InfluxDBConfigError) as exc:
            raise _config_error(exc, self.name) from exc
        except InfluxDBUnavailableError as exc:
            if exc.status_code is None:
                self._note_connection_lost(exc)
                await self._close_client()
            else:
                failure = _WriteFailure(exc.status_code, exc.body, exc.retry_after)
        except InfluxDBWriteError as exc:
            failure = _WriteFailure(
                exc.status_code or 0, exc.body,
                retention_drop=exc.retention_drop, payload_rejected=exc.payload_rejected,
            )
        except QuestDBWriteError as exc:
            failure = _WriteFailure(exc.status_code, exc.body, exc.retry_after)
        except httpx.HTTPError as exc:
            self._note_connection_lost(exc)
            await self._close_client()
        except Exception:
            log.exception("Unexpected %s writer failure", self.name)
            raise

        if failure is not None:
            _interval_updated = await self._handle_write_failure(failure, local_buffer)

        if len(self.buffer) == 0:
            self.out_of_retention_period_range = False
            self.retention_buffer_sorted = False
            self.current_measurements_per_write = self.max_measurements_per_write

        if write_successful:
            if self.connection_lost:
                log.info(
                    "%s '%s' connection restored — flushing %s buffered measurements",
                    self.name,
                    self.config.hostname,
                    len(self.buffer) - len(local_buffer),
                )
            self.last_connection_warning = None
            log.debug("Successfully wrote %s measurements to %s", len(local_buffer), self.name)
            del self.buffer[:len(local_buffer)]
            self.connection_lost = False
            self.last_write_retry = None
            self.current_retry_interval = self.retry_interval
            self.out_of_retention_period_range = False
            self.retention_buffer_sorted = False
            if splits:
                # The server limit was hit inside the package; keep the next batch below it.
                self.set_num_current_measurements_to_write(max(1, self.current_measurements_per_write // 2))
            else:
                self.set_num_current_measurements_to_write(self.current_measurements_per_write * 4)
        else:
            if self.connection_lost and self.client is None and not _interval_updated:
                # Transport error: interval not yet updated above; double it now.
                self._double_retry_interval()

    async def _handle_write_failure(self, failure: _WriteFailure, local_buffer: list) -> bool:
        """Decide drop, shrink, back-off or retry for a rejected write; True when the interval was updated."""
        status = failure.status_code
        message = failure.body
        interval_updated = False
        if failure.retention_drop:
            # Partial write: older points were dropped server-side and cannot be retried,
            # so the batch is discarded; the log is throttled against old backlogs.
            now = datetime.now(UTC)
            if (self.last_retention_warning is None
                    or (now - self.last_retention_warning).total_seconds()
                    >= self.retention_warning_interval):
                log.warning(
                    "%s '%s' is discarding measurements older than its "
                    "retention policy; these points cannot be stored "
                    "(suppressing similar notices for %ss)",
                    self.name,
                    self.config.hostname,
                    self.retention_warning_interval,
                )
                self.last_retention_warning = now
            else:
                log.debug(
                    "%s dropped %s measurement(s) outside the retention policy",
                    self.name,
                    len(local_buffer),
                )
            self.out_of_retention_period_range = True
            del self.buffer[:len(local_buffer)]
            self.current_retry_interval = self.retry_interval
            self.last_write_retry = None
        elif failure.payload_rejected:
            log.error(
                "Dropping %s non-retryable %s measurement(s): %s: %.500s",
                len(local_buffer),
                self.name,
                status,
                message,
            )
            del self.buffer[:len(local_buffer)]
            self.last_write_retry = None
        elif failure.too_large:
            if len(local_buffer) <= self._batch_size_keeping_events_whole(1):
                # Halving cannot shrink the request any further: it is one measurement
                # or one event that must stay whole. Dropping is logged, never silent.
                log.error(
                    "Dropping %s %s measurement(s) (one event or line, cannot be split) "
                    "that exceed the server request limit: %.500s",
                    len(local_buffer),
                    self.name,
                    message,
                )
                del self.buffer[:len(local_buffer)]
                self.last_write_retry = None
            else:
                new_batch = max(1, self.current_measurements_per_write // 2)
                log.error(
                    "%s write payload too large (%s measurements); reducing batch size to %s",
                    self.name,
                    self.current_measurements_per_write,
                    new_batch,
                )
                self.set_num_current_measurements_to_write(new_batch)
                self._double_retry_interval()
        elif status in RETRYABLE_STATUS_CODES:
            if failure.retry_after is not None:
                self.current_retry_interval = int(min(failure.retry_after, self.max_retry_interval))
            else:
                self._double_retry_interval()
            interval_updated = True
            log.error(
                "Retryable %s write failure for '%s': %s: %.500s — retrying in %ss",
                self.name,
                self.config.hostname,
                status,
                message,
                self.current_retry_interval,
            )
            self.connection_lost = True
            await self._close_client()
        elif status == 404:
            # InfluxDB answers 404 only for a missing target (raised as a config error before);
            # a QuestDB write endpoint that is not served right now may come back.
            log.error("Failed to write to %s '%s': %s: %.500s — backing off to max interval",
                      self.name, self.config.hostname, status, message)
            self.current_retry_interval = self.max_retry_interval
            self.connection_lost = True
        elif 400 <= status < 500:
            log.error(
                "Non-retryable %s client/config write failure for '%s': %s: %.500s — "
                "backing off to max interval",
                self.name,
                self.config.hostname,
                status,
                message,
            )
            self.current_retry_interval = self.max_retry_interval
            self.connection_lost = True
        else:
            log.error(
                "Failed to write to %s '%s': %s: %.500s",
                self.name,
                self.config.hostname,
                status,
                message,
            )
        return interval_updated

    def set_num_current_measurements_to_write(self, num_measurements: int):
        if not isinstance(num_measurements, int):
            return
        if num_measurements < 1:
            self.current_measurements_per_write = 1
        elif num_measurements >= self.max_measurements_per_write:
            self.current_measurements_per_write = self.max_measurements_per_write
        else:
            self.current_measurements_per_write = num_measurements

    def check_buffer(self) -> None:
        """Warn about buffer fill level; overflow itself is handled in append_measurement()."""
        length = len(self.buffer)
        max_length = self.max_measurements_buffer_size
        percent_buffer_usage = 100 / max_length * length
        if percent_buffer_usage >= self.current_max_measurements_buffer_warning:
            log.warning(
                "%s measurement buffer usage is %.1f%% (%s/%s)",
                self.name,
                percent_buffer_usage,
                length,
                max_length,
            )
            self.current_max_measurements_buffer_warning = min(
                self.current_max_measurements_buffer_warning + 5,
                100,
            )
        elif percent_buffer_usage < self.max_measurements_buffer_warning:
            self.current_max_measurements_buffer_warning = self.max_measurements_buffer_warning

    async def task_loop(self, queue: asyncio.Queue):
        try:
            while True:
                drained = 0
                while drained < self.max_measurements_per_write:
                    try:
                        measurement = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    try:
                        self.append_measurement(measurement)
                    finally:
                        queue.task_done()
                    drained += 1

                await self.write_data()
                self.check_buffer()
                await asyncio.sleep(0.1 if self.out_of_retention_period_range else 1)
        finally:
            try:
                await self._flush_buffer_before_close()
            finally:
                await self.close()


class InfluxLogAndConfigWriter:
    name = "InfluxLogAndConfigWriter"
    log_record_type = "fritzFlux"

    def __init__(self, config: FritzBoxConfig, log_queue: asyncio.Queue):
        if not isinstance(config, FritzBoxConfig):
            raise TypeError("param 'config' needs to be a 'FritzBoxConfig' object")
        if not isinstance(log_queue, asyncio.Queue):
            raise TypeError("param 'log_queue' needs to be a 'asyncio.Queue' object")

        self.config = config
        self.log_queue = log_queue
        self.init_successful = True

    def format_log_record(self, log_record):
        if not isinstance(log_record, LogRecord):
            return None
        log_timestamp = datetime.fromtimestamp(log_record.created, UTC)
        log_msg = f"{log_record.levelname}: {log_record.getMessage()}"
        # Same-second records share series and timestamp, so each carries its own event id.
        identity = "|".join(str(part) for part in (
            log_record.created, log_record.relativeCreated, log_record.process,
            log_record.thread, log_record.name, log_msg))
        uid = hashlib.sha256(identity.encode("UTF-8", "replace")).hexdigest()[:32]
        return FritzMeasurement("message", log_msg,
                                box_tag=self.config.box_tag,
                                additional_tags={"log_type": self.log_record_type, "uid": uid},
                                data_type=str,
                                timestamp=log_timestamp,
                                timestamp_precision=WritePrecision.S,
                                is_event=True)

    async def task_loop(self, output_queue: asyncio.Queue):
        max_log_records_per_tick = 1_000
        config_write_interval = 3600  # write config settings once per hour
        last_config_write = 0.0

        while True:
            now = time.monotonic()
            if now - last_config_write >= config_write_interval:
                last_config_write = now
                tz_name = str(getattr(self.config, "timezone", "Europe/Berlin"))
                await output_queue.put(FritzMeasurement(
                    "fritzfluxdb_setting_timezone",
                    tz_name,
                    box_tag=self.config.box_tag,
                    data_type=str,
                    timestamp_precision=WritePrecision.S,
                ))

            await self._forward_pending(output_queue, max_log_records_per_tick)
            await asyncio.sleep(1)

    async def _forward_pending(self, output_queue: asyncio.Queue, limit: int | None = None) -> None:
        """Move queued log records to the measurement queue, at most `limit` of them."""
        forwarded = 0
        while limit is None or forwarded < limit:
            try:
                log_record = self.log_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            forwarded += 1

            try:
                formatted_log_record = self.format_log_record(log_record)
                if formatted_log_record is not None:
                    await output_queue.put(formatted_log_record)
            finally:
                self.log_queue.task_done()

    async def drain(self, output_queue: asyncio.Queue) -> None:
        """Forward every log record still queued; used on shutdown after task_loop was stopped."""
        await self._forward_pending(output_queue)
