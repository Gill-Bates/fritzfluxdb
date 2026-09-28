#!/usr/bin/env python3
#
# app/classes/influxdb/handler.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
import re
import time
from contextlib import suppress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from ipaddress import ip_address
from logging import LogRecord
from typing import ClassVar

import httpx

from app.classes.common import FritzMeasurement, WritePrecision
from app.classes.fritzbox.config import FritzBoxConfig
from app.classes.influxdb.config import (
    QUESTDB_DOWNSAMPLING_DISABLED_PROFILE,
    QUESTDB_DOWNSAMPLING_PROFILES,
    QUESTDB_DOWNSAMPLING_SCHEMA_VERSION,
    QUESTDB_DOWNSAMPLING_STATE_TABLE,
    InfluxDBConfig,
    QuestDBDownsamplingProfile,
)
from app.log import get_logger


def _format_url_host(hostname: str) -> str:
    try:
        if ip_address(hostname).version == 6:
            return f"[{hostname}]"
    except ValueError:
        pass
    return hostname


def _format_exc(exc: BaseException) -> str:
    """Render an exception for logging, including its type name.

    Several httpx transport errors (e.g. ConnectError, ReadTimeout) have an
    empty str(), which would otherwise produce uninformative messages like
    'unreachable: '. Always surface the exception class so the cause is visible.
    """
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__

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
}

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


def _questdb_identifier(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _questdb_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _questdb_metric_name(name: str) -> str:
    return str(name).replace(".", "_")


_QUESTDB_TTL_UNIT_HOURS = {"HOUR": 1, "DAY": 24, "WEEK": 24 * 7, "MONTH": 24 * 30, "YEAR": 24 * 365}


def _questdb_ttl_days(ttl_value, ttl_unit) -> int | None:
    """Whole days covered by a QuestDB TTL, or None for an unsupported unit.

    Months and years are approximated (30/365 days); the value is only used to compare
    and publish the effective raw history, never to compute a retention boundary.
    """
    try:
        value = int(ttl_value or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return 0

    hours_per_unit = _QUESTDB_TTL_UNIT_HOURS.get(str(ttl_unit or "").upper().rstrip("S"))
    if hours_per_unit is None:
        return None
    return value * hours_per_unit // 24


def _questdb_type_from_python(data_type: type | None) -> str | None:
    return _QUESTDB_TYPE_MAP.get(data_type)


def _collect_metric_columns(metric_name: str, metric_params: object, columns: dict[str, str]) -> None:
    if isinstance(metric_params, str):
        try:
            name, type_name = metric_params.rsplit(":", maxsplit=1)
        except ValueError:
            return
        data_type = {
            "bool": bool,
            "int": int,
            "float": float,
            "str": str,
        }.get(type_name)
        sql_type = _questdb_type_from_python(data_type)
        if sql_type is not None:
            columns[_questdb_metric_name(name)] = sql_type
        return

    if not isinstance(metric_params, dict):
        return

    next_metric = metric_params.get("next")
    if next_metric is not None:
        _collect_metric_columns(metric_name, next_metric, columns)
        return

    sql_type = _questdb_type_from_python(metric_params.get("type"))
    if sql_type is not None:
        columns[_questdb_metric_name(metric_name)] = sql_type


def questdb_expected_columns() -> dict[str, str]:
    from app.classes.fritzbox import service_definitions

    columns = {
        **_QUESTDB_TAG_COLUMNS,
        **_QUESTDB_INTERNAL_COLUMNS,
        **_QUESTDB_OPTIONAL_METRIC_COLUMNS,
    }

    for service in service_definitions.tr069_services:
        for metric_params in service.get("value_instances", {}).values():
            _collect_metric_columns("", metric_params, columns)

    for service in service_definitions.lua_services:
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


def questdb_rollup_view_name(table_name: str, profile: QuestDBDownsamplingProfile) -> str:
    # The schema-version suffix makes definition changes additive; profile switches keep old
    # views intact. Derived from QUESTDB_DOWNSAMPLING_SCHEMA_VERSION so a schema bump changes the
    # view name here and in the dashboard generator (tools/build_questdb_dashboards.py) together.
    return f"{table_name}_rollup_{profile.rollup_interval}_v{QUESTDB_DOWNSAMPLING_SCHEMA_VERSION}"


def questdb_raw_retention_days(profile: QuestDBDownsamplingProfile, total_retention_days: int) -> int:
    if total_retention_days > 0:
        return min(profile.raw_retention_days, total_retention_days)
    return profile.raw_retention_days


def questdb_rollup_ddl(
        table_name: str,
        profile: QuestDBDownsamplingProfile,
        retention_days: int,
        columns: dict[str, str] | None = None) -> str:
    expected = questdb_expected_columns() if columns is None else columns
    projections = ["timestamp"]
    projections.extend(_questdb_identifier(name) for name in _QUESTDB_ROLLUP_DIMENSIONS if name in expected)

    for name, column_type in sorted(expected.items()):
        metric_class = questdb_metric_class(name, column_type)
        column = _questdb_identifier(name)
        if metric_class is QuestDBMetricClass.GAUGE:
            for aggregate, suffix in (
                    ("sum", "sum"), ("count", "count"), ("min", "min"),
                    ("max", "max"), ("last", "last")):
                projections.append(f"{aggregate}({column}) AS {_questdb_identifier(f'{name}_{suffix}')}")
        elif metric_class is QuestDBMetricClass.COUNTER:
            projections.append(f"last({column}) AS {_questdb_identifier(f'{name}_last')}")

    source_columns = [
        _questdb_identifier(name)
        for name, column_type in sorted(expected.items())
        if questdb_metric_class(name, column_type) in {QuestDBMetricClass.GAUGE, QuestDBMetricClass.COUNTER}
    ]
    if not source_columns:
        raise ValueError("QuestDB rollup requires at least one explicitly classified metric")
    source_filter = " OR ".join(f"{column} IS NOT NULL" for column in source_columns)

    view_name = questdb_rollup_view_name(table_name, profile)
    select_list = ",\n        ".join(projections)
    ttl_clause = f" TTL {int(retention_days)} DAYS" if retention_days > 0 else ""
    return (
        f"CREATE MATERIALIZED VIEW IF NOT EXISTS {_questdb_identifier(view_name)} "
        f"WITH BASE {_questdb_identifier(table_name)} REFRESH IMMEDIATE AS (\n"
        f"    SELECT\n        {select_list}\n"
        f"    FROM {_questdb_identifier(table_name)}\n"
        f"    WHERE {source_filter}\n"
        f"    SAMPLE BY {profile.rollup_interval} ALIGN TO CALENDAR\n"
        f") PARTITION BY DAY{ttl_clause};"
    )

class InfluxHandler:
    name = "InfluxDB"
    connection_timeout_v1 = 2
    connection_timeout_v2 = 5
    max_measurements_buffer_size = 1_000_000
    max_measurements_per_write = 1_000
    max_measurements_buffer_warning = 80
    retry_interval = 5
    max_retry_interval = 120
    retention_warning_interval = 300
    _retryable_status_codes: ClassVar[set] = {408, 425, 429, 500, 502, 503, 504}

    connection_warning_interval = 60
    downsampling_retry_attempts = 30
    downsampling_retry_interval = 10

    def __init__(self, config, user_agent: str | None = None):
        self.config = InfluxDBConfig(config)
        try:
            self.version = int(self.config.version)
        except (ValueError, TypeError):
            self.version = str(self.config.version).lower()

        if self.version not in {1, 2, "questdb"}:
            raise ValueError(f"Unsupported database version/type: {self.version}")

        if self.version == "questdb":
            self.name = "QuestDB"
        else:
            self.name = f"InfluxDB v{self.version}"

        self.questdb_version: str | None = None
        self.questdb_schema_ensured = False
        self.client: httpx.AsyncClient | None = None
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

        proto = "https" if self.config.tls_enabled else "http"
        host = _format_url_host(self.config.hostname)
        self.base_url = f"{proto}://{host}:{self.config.port}"

    def append_measurement(self, measurement: FritzMeasurement) -> None:
        if len(self.buffer) >= self.max_measurements_buffer_size:
            drop_count = min(len(self.buffer), self.max_measurements_per_write)
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
                self.name, self.config.hostname, _format_exc(exc),
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
            if self.client is not None:
                await self.client.aclose()
                self.client = None

    async def _init_client(self) -> bool:
        timeout = self.connection_timeout_v1 if self.version == 1 else self.connection_timeout_v2
        headers = {}
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        if self.version == 2:
            headers["Authorization"] = f"Token {self.config.token}"
        elif self.version == "questdb" and self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"

        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            verify=self.config.verify_tls,
            headers=headers
        )

        try:
            if self.version == 1:
                auth = (self.config.username, self.config.password) if self.config.username else None
                resp = await self.client.get("/ping", auth=auth)
                resp.raise_for_status()
            elif self.version == "questdb":
                auth = (self.config.username, self.config.password) if self.config.username else None
                resp = await self.client.get("/exec", params={"query": "SELECT build();"}, auth=auth)
                resp.raise_for_status()
                try:
                    data = resp.json()
                    build_str = data["dataset"][0][0]
                    match = re.search(r"QuestDB\s+([0-9a-zA-Z\.\-]+)", build_str)
                    if match:
                        self.questdb_version = match.group(1)
                    else:
                        self.questdb_version = build_str
                except Exception as exc:  # noqa: BLE001 - version string is cosmetic, any parse failure is tolerated
                    log.debug("Failed to parse QuestDB version response: %s", exc)
                    self.questdb_version = "unknown version"
                # Schema only needs to be ensured once per process; QuestDB's ILP
                # /write auto-creates columns afterwards. Re-running it on every
                # reconnect would replay dozens of ALTER TABLE statements during a
                # connection flap, so only retry until it first succeeds.
                if not self.questdb_schema_ensured:
                    try:
                        self.questdb_schema_ensured = await self._ensure_questdb_schema(auth=auth)
                    except Exception as exc:  # noqa: BLE001 - schema setup is retried on the next reconnect
                        log.error("Failed to ensure QuestDB schema for '%s': %s",
                                  self.config.measurement_name, _format_exc(exc))
                    if not self.questdb_schema_ensured and self.config.downsampling:
                        self._schedule_questdb_downsampling_retry(auth=auth)
            else:
                resp = await self.client.get("/ping")
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            self._note_connection_lost(exc)
            self.init_successful = False
            await self.client.aclose()
            self.client = None
            return False

        self.init_successful = True
        if self.version == "questdb" and self.questdb_version:
            log.info("Connection to %s [v%s] established", self.name, self.questdb_version)
        else:
            log.info("Connection to %s established", self.name)
        return True

    async def _questdb_exec(self, query: str, *, auth=None) -> dict:
        if self.client is None:
            raise RuntimeError("QuestDB client is not initialized")
        resp = await self.client.get("/exec", params={"query": query}, auth=auth)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"{data.get('error')} at position {data.get('position')}")
        return data

    async def _ensure_questdb_schema(self, *, auth=None) -> bool:
        table_name = self.config.measurement_name
        table_ident = _questdb_identifier(table_name)

        await self._questdb_exec(
            f"CREATE TABLE IF NOT EXISTS {table_ident} (timestamp TIMESTAMP) "
            "TIMESTAMP(timestamp) PARTITION BY DAY;",
            auth=auth,
        )

        columns = questdb_expected_columns()
        for column_name, column_type in columns.items():
            column_ident = _questdb_identifier(column_name)
            await self._questdb_exec(
                f"ALTER TABLE {table_ident} ADD COLUMN IF NOT EXISTS {column_ident} {column_type};",
                auth=auth,
            )

        log.debug("Ensured QuestDB schema for %s with %s expected columns", table_name, len(columns))

        profile_name = self.config.downsampling
        if not profile_name:
            state_exists = await self._questdb_downsampling_state_table_exists(auth=auth)
            raw_ttl_managed = False
            if not state_exists:
                # Preserve the legacy retention path once when upgrading an installation.
                raw_ttl_managed = await self._ensure_questdb_ttl(auth=auth)
            else:
                raw_ttl_managed = await self._questdb_raw_ttl_is_managed(auth=auth)
            return await self._set_questdb_downsampling_state(
                enabled=False,
                profile_name=QUESTDB_DOWNSAMPLING_DISABLED_PROFILE,
                raw_ttl_managed=raw_ttl_managed,
                auth=auth,
            )

        profile = QUESTDB_DOWNSAMPLING_PROFILES[profile_name]
        return await self._finalize_questdb_downsampling(profile_name, profile, auth=auth)

    async def _finalize_questdb_downsampling(
            self,
            profile_name: str,
            profile: QuestDBDownsamplingProfile,
            *,
            auth=None) -> bool:
        table_name = self.config.measurement_name
        raw_retention_days = questdb_raw_retention_days(profile, self.config.data_retention_days)
        log.info(
            "QuestDB downsampling profile: %s (raw %s day(s), rollup %s / %s day(s))",
            profile_name,
            raw_retention_days,
            profile.rollup_interval,
            self.config.data_retention_days,
        )
        if not await self._ensure_questdb_downsampling(profile, auth=auth):
            return False

        # The raw TTL is applied and verified before the status is published, so Grafana never
        # reads a raw window that QuestDB does not actually enforce.
        applied = await self._apply_questdb_raw_ttl(
            table_name,
            raw_retention_days,
            previously_managed=await self._questdb_raw_ttl_is_managed(auth=auth),
            auth=auth,
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
            rollup_view=questdb_rollup_view_name(table_name, profile),
            auth=auth,
        )

    def _schedule_questdb_downsampling_retry(self, *, auth=None) -> None:
        task = self._questdb_downsampling_retry_task
        if task is None or task.done():
            self._questdb_downsampling_retry_task = asyncio.create_task(
                self._retry_questdb_downsampling_setup(auth=auth)
            )

    async def _retry_questdb_downsampling_setup(self, *, auth=None) -> None:
        profile_name = self.config.downsampling
        profile = QUESTDB_DOWNSAMPLING_PROFILES[profile_name]
        try:
            for attempt in range(1, self.downsampling_retry_attempts + 1):
                await asyncio.sleep(self.downsampling_retry_interval)
                if self.client is None:
                    return
                try:
                    # Shares _write_lock with write_data(): without it, a concurrent write
                    # failure could close/replace self.client between this check and the
                    # queries below, breaking mid-provisioning instead of surfacing as one
                    # of the caught exceptions.
                    async with self._write_lock:
                        if self.client is None:
                            return
                        ready = await self._finalize_questdb_downsampling(profile_name, profile, auth=auth)
                except Exception as exc:  # noqa: BLE001 - reconnect will get another bounded retry sequence
                    log.warning(
                        "QuestDB downsampling retry %s/%s failed: %s",
                        attempt, self.downsampling_retry_attempts, _format_exc(exc),
                    )
                    continue
                if ready:
                    self.questdb_schema_ensured = True
                    log.info("QuestDB downsampling provisioning completed after async refresh")
                    return
            log.error(
                "QuestDB downsampling did not become ready after %s retries; raw TTL remains unchanged",
                self.downsampling_retry_attempts,
            )
        finally:
            self._questdb_downsampling_retry_task = None

    async def _ensure_questdb_downsampling(self, profile: QuestDBDownsamplingProfile, *, auth=None) -> bool:
        table_name = self.config.measurement_name
        view_name = questdb_rollup_view_name(table_name, profile)
        try:
            await self._questdb_exec(
                questdb_rollup_ddl(table_name, profile, self.config.data_retention_days),
                auth=auth,
            )
            status = await self._questdb_exec(
                "SELECT view_name, base_table_name, view_status, "
                "refresh_base_table_txn, base_table_txn FROM materialized_views() "
                f"WHERE view_name = {_questdb_literal(view_name)};",
                auth=auth,
            )
        except Exception as exc:  # noqa: BLE001 - optional provisioning must not stop raw ingestion
            log.error("Unable to provision QuestDB rollup view '%s': %s", view_name, _format_exc(exc))
            return False

        rows = status.get("dataset") or []
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
            log.warning(
                "QuestDB rollup view '%s' is not fully refreshed (status=%s, refresh txn=%s, base txn=%s); "
                "raw TTL is unchanged",
                view_name, view_status, refresh_txn, base_txn,
            )
            return False

        if not await self._set_questdb_table_ttl(
                view_name,
                self.config.data_retention_days,
                materialized_view=True,
                auth=auth):
            return False
        log.debug("QuestDB rollup view '%s' is valid and current", view_name)
        return True

    async def _questdb_downsampling_state_table_exists(self, *, auth=None) -> bool:
        existing = await self._questdb_exec(
            "SELECT table_name FROM tables() "
            f"WHERE table_name = {_questdb_literal(QUESTDB_DOWNSAMPLING_STATE_TABLE)};",
            auth=auth,
        )
        return bool(existing.get("dataset") or [])

    async def _read_questdb_downsampling_state(self, *, auth=None) -> list | None:
        """Latest status row for this measurement, or None when it is unavailable.

        Creates the status table (and adds raw_ttl_managed to a table written by an earlier
        build) so a caller can rely on the columns being present.
        """
        state_table = _questdb_identifier(QUESTDB_DOWNSAMPLING_STATE_TABLE)
        try:
            await self._questdb_exec(
                f"CREATE TABLE IF NOT EXISTS {state_table} ("
                "timestamp TIMESTAMP, measurement SYMBOL, profile SYMBOL, schema_version LONG, "
                "enabled BOOLEAN, raw_days LONG, rollup_interval SYMBOL, rollup_view SYMBOL, "
                "raw_ttl_managed BOOLEAN) "
                "TIMESTAMP(timestamp) PARTITION BY DAY;",
                auth=auth,
            )
            await self._questdb_exec(
                f"ALTER TABLE {state_table} ADD COLUMN IF NOT EXISTS raw_ttl_managed BOOLEAN;",
                auth=auth,
            )
            current = await self._questdb_exec(
                "SELECT profile, schema_version, enabled, raw_days, rollup_interval, rollup_view, "
                f"raw_ttl_managed FROM {state_table} "
                f"WHERE measurement = {_questdb_literal(self.config.measurement_name)} "
                "LATEST ON timestamp PARTITION BY measurement;",
                auth=auth,
            )
        except Exception as exc:  # noqa: BLE001 - state reporting must not stop raw ingestion
            log.error(
                "Unable to read QuestDB downsampling state for '%s': %s",
                self.config.measurement_name, _format_exc(exc),
            )
            return None

        rows = current.get("dataset") or []
        return list(rows[0][:7]) if rows else []

    async def _questdb_raw_ttl_is_managed(self, *, auth=None) -> bool:
        """True only if a previous run recorded the raw TTL as set by fritzfluxdb.

        An unreadable status or a status from before TTL ownership tracking counts as
        administrator-owned, so an existing TTL of unknown origin is never replaced.
        """
        state = await self._read_questdb_downsampling_state(auth=auth)
        return bool(state) and bool(state[6])

    async def _set_questdb_downsampling_state(
            self,
            *,
            enabled: bool,
            profile_name: str = "",
            raw_retention_days: int = 0,
            raw_ttl_managed: bool = False,
            rollup_interval: str = "",
            rollup_view: str = "",
            auth=None) -> bool:
        measurement = self.config.measurement_name
        current = await self._read_questdb_downsampling_state(auth=auth)
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
        if current == expected:
            return True

        try:
            await self._questdb_exec(
                f"INSERT INTO {_questdb_identifier(QUESTDB_DOWNSAMPLING_STATE_TABLE)} "
                "(timestamp, measurement, profile, schema_version, enabled, raw_days, "
                "rollup_interval, rollup_view, raw_ttl_managed) VALUES ("
                f"now(), {_questdb_literal(measurement)}, {_questdb_literal(profile_name)}, "
                f"{QUESTDB_DOWNSAMPLING_SCHEMA_VERSION}, {'true' if enabled else 'false'}, "
                f"{int(raw_retention_days)}, "
                f"{_questdb_literal(rollup_interval)}, {_questdb_literal(rollup_view)}, "
                f"{'true' if raw_ttl_managed else 'false'});",
                auth=auth,
            )
        except Exception as exc:  # noqa: BLE001 - state reporting must not stop raw ingestion
            log.error("Unable to update QuestDB downsampling state for '%s': %s", measurement, _format_exc(exc))
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

    async def _ensure_questdb_ttl(self, *, auth=None) -> bool:
        """
        Applies data_retention_days as table TTL, but only to a table without a TTL: a TTL
        configured by the database admin always wins. Transport errors propagate so the
        schema setup is retried on the next reconnect. Returns True when this call set the
        TTL, which makes it a fritzfluxdb-managed TTL.
        """
        retention_days = self.config.data_retention_days
        if not retention_days:
            return False

        table_name = self.config.measurement_name
        existing = await self._read_questdb_table_ttl(table_name, auth=auth)
        if existing is None:
            return False
        if existing[0]:
            log.info("QuestDB table '%s' keeps its existing TTL of %s %s", table_name, *existing)
            return False

        return await self._alter_questdb_table_ttl(table_name, retention_days, auth=auth)

    async def _read_questdb_table_ttl(self, table_name: str, *, auth=None) -> tuple[int, str] | None:
        """Current TTL as (value, unit), or None when it cannot be determined."""
        try:
            data = await self._questdb_exec(
                f"SELECT ttlValue, ttlUnit FROM tables() WHERE table_name = {_questdb_literal(table_name)};",
                auth=auth,
            )
        except (httpx.HTTPStatusError, RuntimeError, ValueError) as exc:
            log.warning(
                "Unable to read TTL of QuestDB table '%s' (requires QuestDB 8.2.2+); "
                "data retention is not applied: %s",
                table_name, _format_exc(exc),
            )
            return None

        rows = data.get("dataset") or []
        if not rows:
            log.warning("QuestDB table '%s' not found; data retention is not applied", table_name)
            return None

        try:
            ttl_value = int(rows[0][0] or 0)
        except (TypeError, ValueError):
            ttl_value = 0
        return ttl_value, str(rows[0][1] or "")

    async def _apply_questdb_raw_ttl(
            self,
            table_name: str,
            requested_days: int,
            *,
            previously_managed: bool,
            auth=None) -> tuple[int, bool] | None:
        """Resolve the raw TTL for a downsampling profile.

        Returns the verified (effective_raw_days, managed) pair, or None when the raw TTL
        could not be established — the caller then publishes no status at all. A TTL that
        fritzfluxdb did not set belongs to the administrator and is kept, so the effective
        value is published instead of the profile's requested one.
        """
        existing = await self._read_questdb_table_ttl(table_name, auth=auth)
        if existing is None:
            return None

        ttl_value, ttl_unit = existing
        existing_days = _questdb_ttl_days(ttl_value, ttl_unit)
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
        if not await self._alter_questdb_table_ttl(table_name, requested_days, auth=auth):
            return None

        verified = await self._read_questdb_table_ttl(table_name, auth=auth)
        if verified is None or _questdb_ttl_days(*verified) != int(requested_days):
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
            materialized_view: bool = False,
            auth=None) -> bool:
        """Applies retention_days only to an object without a TTL; an existing TTL is kept."""
        existing = await self._read_questdb_table_ttl(table_name, auth=auth)
        if existing is None:
            return False

        ttl_value, ttl_unit = existing
        if int(retention_days) <= 0:
            # Unlimited retention never drops or alters an existing TTL.
            return True

        if ttl_value:
            if _questdb_ttl_days(ttl_value, ttl_unit) == int(retention_days):
                log.debug("QuestDB table '%s' already has the requested TTL of %s day(s)", table_name, retention_days)
            else:
                log.info("QuestDB table '%s' keeps its existing TTL of %s %s", table_name, ttl_value, ttl_unit)
            return True

        return await self._alter_questdb_table_ttl(
            table_name,
            retention_days,
            materialized_view=materialized_view,
            auth=auth,
        )

    async def _alter_questdb_table_ttl(
            self,
            table_name: str,
            retention_days: int,
            *,
            materialized_view: bool = False,
            auth=None) -> bool:
        try:
            object_type = "MATERIALIZED VIEW" if materialized_view else "TABLE"
            await self._questdb_exec(
                f"ALTER {object_type} {_questdb_identifier(table_name)} SET TTL {int(retention_days)} DAYS;",
                auth=auth,
            )
        except (httpx.HTTPStatusError, RuntimeError, ValueError) as exc:
            log.warning(
                "Unable to set TTL of %s day(s) on QuestDB table '%s': %s",
                retention_days, table_name, _format_exc(exc),
            )
            return False

        log.info("Set TTL of QuestDB table '%s' to %s day(s)", table_name, retention_days)
        return True

    @staticmethod
    def _is_retention_drop(status_code: int, message: str) -> bool:
        """True if an InfluxDB write response reports points dropped because they
        fall outside the retention policy (a non-retryable partial write).

        Matches the wording across InfluxDB versions, e.g.
        'points beyond retention policy' and
        'partial write: dropped N points outside retention policy ...
         violates a Retention Policy Lower Bound'.
         Deliberately does NOT match other partial writes (e.g. field type
         conflicts), which must still surface as errors.
        """
        if status_code not in {400, 422}:
            return False
        msg = (message or "").lower()
        return "retention policy" in msg and (
            "partial write" in msg
            or "dropped" in msg
            or "beyond retention policy" in msg
        )

    @staticmethod
    def _parse_retry_after(value: str | None) -> int | None:
        if not value:
            return None
        if value.isdecimal():
            return int(value)
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0, int((retry_at - datetime.now(UTC)).total_seconds()))

    @staticmethod
    def _is_non_retryable_write_error(status_code: int, message: str) -> bool:
        msg = (message or "").lower()
        return (
            status_code == 400
            and (
                "field type conflict" in msg
                or "unable to parse" in msg
                or "partial write" in msg
            )
        )

    def bundle_measurements(self, measurements: list) -> tuple[list[str], list[FritzMeasurement]]:
        """
        Converts measurements into line protocol, merging all fields which share the same
        series (measurement + tags) and timestamp into a single line.

        QuestDB stores every line as its own row with a slot for each table column, so one
        line per field multiplies the storage footprint. InfluxDB treats a merged line exactly
        like the separate lines it replaces. A field name that already exists on a pending line
        starts a new line instead of overwriting the earlier value.

        Returns the line protocol lines and the measurements that could be converted.
        """
        lines: list[tuple[str, int, dict[str, str]]] = []
        open_lines: dict[tuple[str, int], list[int]] = {}
        valid_measurements: list[FritzMeasurement] = []

        for measurement in measurements:
            if not isinstance(measurement, FritzMeasurement):
                log.error("Measurement needs to be a 'FritzMeasurement' but got '%s'", type(measurement))
                continue

            field_name = measurement.name
            if self.version == "questdb":
                # QuestDB rejects dots in column names (e.g. "802.11" in wlan metric names)
                field_name = field_name.replace(".", "_")

            field = measurement.line_protocol_field(field_name)
            if not field:
                continue

            key = (measurement.line_protocol_series(self.config.measurement_name),
                   measurement.line_protocol_timestamp())
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

    def permitted_to_write_data(self):
        if self.last_write_retry is None:
            return True
        self.current_retry_interval = min(self.current_retry_interval, self.max_retry_interval)
        return (datetime.now(UTC) - self.last_write_retry).total_seconds() >= self.current_retry_interval

    async def _flush_buffer_before_close(self, timeout: float = 10.0) -> None:
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

    async def write_data(self, *, force: bool = False):
        async with self._write_lock:
            await self._write_data_unlocked(force=force)

    async def _write_data_unlocked(self, *, force: bool = False):
        if self.client is None and not await self._init_client():
            return
        if not force and not self.permitted_to_write_data():
            return
        if len(self.buffer) == 0:
            return

        if self.out_of_retention_period_range and not self.retention_buffer_sorted:
            self.buffer.sort(key=lambda m: m.timestamp, reverse=True)
            self.retention_buffer_sorted = True

        local_buffer = self.buffer[:self.current_measurements_per_write]

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

        payload = "\n".join(data_lines)
        write_successful = False
        _interval_updated = False
        self.last_write_retry = datetime.now(UTC)

        try:
            if self.version == 1:
                auth = (self.config.username, self.config.password) if self.config.username else None
                params = {"db": self.config.database, "precision": "s"}
                resp = await self.client.post("/write", params=params, content=payload, auth=auth)
            elif self.version == "questdb":
                auth = (self.config.username, self.config.password) if self.config.username else None
                params = {"precision": "s"}
                resp = await self.client.post("/write", params=params, content=payload, auth=auth)
            else:
                params = {"org": self.config.organization, "bucket": self.config.bucket, "precision": "s"}
                resp = await self.client.post("/api/v2/write", params=params, content=payload)

            if resp.status_code in (200, 204):
                write_successful = True
            else:
                exception_message = resp.text
                if self._is_retention_drop(resp.status_code, exception_message):
                    # InfluxDB did a *partial write*: points within the retention
                    # window were stored, points older than the retention policy
                    # were permanently dropped server-side. Neither can be retried,
                    # so discard the whole batch instead of looping on it forever.
                    # Throttle the log so old backlogs don't flood the output.
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
                elif self._is_non_retryable_write_error(resp.status_code, exception_message):
                    log.error(
                        "Dropping %s non-retryable %s measurement(s): %s: %.500s",
                        len(local_buffer),
                        self.name,
                        resp.status_code,
                        exception_message,
                    )
                    del self.buffer[:len(local_buffer)]
                    self.last_write_retry = None
                elif resp.status_code == 413:
                    if self.current_measurements_per_write == 1:
                        log.error(
                            "Dropping oversized %s measurement (single line exceeds server limit): %.500s",
                            self.name,
                            exception_message,
                        )
                        del self.buffer[0]
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
                        self.current_retry_interval = min(
                            self.current_retry_interval * 2, self.max_retry_interval
                        )
                elif resp.status_code in self._retryable_status_codes:
                    retry_after_seconds = self._parse_retry_after(resp.headers.get("Retry-After"))
                    if retry_after_seconds is not None:
                        self.current_retry_interval = min(retry_after_seconds, self.max_retry_interval)
                    else:
                        self.current_retry_interval = min(
                            self.current_retry_interval * 2, self.max_retry_interval
                        )
                    _interval_updated = True
                    log.error(
                        "Retryable %s write failure for '%s': %s: %.500s — retrying in %ss",
                        self.name,
                        self.config.hostname,
                        resp.status_code,
                        exception_message,
                        self.current_retry_interval,
                    )
                    self.connection_lost = True
                    await self.client.aclose()
                    self.client = None
                elif resp.status_code in {401, 403, 404}:
                    log.error(
                        "Non-retryable %s auth/config failure for '%s': %s: %.500s — "
                        "backing off to max interval",
                        self.name,
                        self.config.hostname,
                        resp.status_code,
                        exception_message,
                    )
                    self.current_retry_interval = self.max_retry_interval
                    self.connection_lost = True
                elif 400 <= resp.status_code < 500:
                    log.error(
                        "Non-retryable %s client/config write failure for '%s': %s: %.500s — "
                        "backing off to max interval",
                        self.name,
                        self.config.hostname,
                        resp.status_code,
                        exception_message,
                    )
                    self.current_retry_interval = self.max_retry_interval
                    self.connection_lost = True
                else:
                    log.error(
                        "Failed to write to %s '%s': %s: %.500s",
                        self.name,
                        self.config.hostname,
                        resp.status_code,
                        exception_message,
                    )
        except httpx.HTTPError as exc:
            self._note_connection_lost(exc)
            if self.client is not None:
                await self.client.aclose()
                self.client = None
        except Exception:
            log.exception("Unexpected %s writer failure", self.name)
            raise

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
            self.set_num_current_measurements_to_write(self.current_measurements_per_write * 4)
        else:
            if self.connection_lost and self.client is None and not _interval_updated:
                # Transport error: interval not yet updated above; double it now.
                self.current_retry_interval = min(
                    self.current_retry_interval * 2, self.max_retry_interval
                )

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
            await self._flush_buffer_before_close()
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
        return FritzMeasurement("message", log_msg,
                                box_tag=self.config.box_tag,
                                additional_tags={"log_type": self.log_record_type},
                                data_type=str,
                                timestamp=log_timestamp,
                                timestamp_precision=WritePrecision.S)

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

            for _ in range(max_log_records_per_tick):
                try:
                    log_record = self.log_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                try:
                    formatted_log_record = self.format_log_record(log_record)
                    if formatted_log_record is not None:
                        await output_queue.put(formatted_log_record)
                finally:
                    self.log_queue.task_done()

            await asyncio.sleep(1)

# EOF
