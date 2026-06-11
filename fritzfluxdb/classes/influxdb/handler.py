#!/usr/bin/env python3
#
# fritzfluxdb/classes/influxdb/handler.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
import time
from datetime import UTC, datetime
from logging import LogRecord
import re
from email.utils import parsedate_to_datetime
from ipaddress import ip_address

import httpx

from fritzfluxdb.classes.influxdb.config import InfluxDBConfig
from fritzfluxdb.log import get_logger
from fritzfluxdb.classes.common import FritzMeasurement, WritePrecision
from fritzfluxdb.classes.fritzbox.config import FritzBoxConfig


def _format_url_host(hostname: str) -> str:
    try:
        if ip_address(hostname).version == 6:
            return f"[{hostname}]"
    except ValueError:
        pass
    return hostname

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

_QUESTDB_DASHBOARD_COMPATIBILITY_COLUMNS = {
    "ddns_status": "VARCHAR",
    "dsl_line_type": "VARCHAR",
    "myfritz_host_name": "VARCHAR",
    "vpn_user_remote_address": "VARCHAR",
    "vpn_user_virtual_address": "VARCHAR",
}


def _questdb_identifier(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _questdb_metric_name(name: str) -> str:
    return str(name).replace(".", "_")


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
    from fritzfluxdb.classes.fritzbox import service_definitions

    columns = {
        **_QUESTDB_TAG_COLUMNS,
        **_QUESTDB_INTERNAL_COLUMNS,
        **_QUESTDB_DASHBOARD_COMPATIBILITY_COLUMNS,
    }

    for service in service_definitions.tr069_services:
        for metric_params in service.get("value_instances", {}).values():
            _collect_metric_columns("", metric_params, columns)

    for service in service_definitions.lua_services:
        for metric_name, metric_params in service.get("value_instances", {}).items():
            _collect_metric_columns(metric_name, metric_params, columns)

    return dict(sorted(columns.items()))

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
    _retryable_status_codes = {408, 425, 429, 500, 502, 503, 504}

    connection_warning_interval = 60

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
        self.client: httpx.AsyncClient | None = None
        self.init_successful = False
        self.connection_lost = False
        self.last_connection_warning: datetime | None = None
        self.out_of_retention_period_range = False
        self.last_retention_warning = None
        self.buffer = list()
        self.current_retry_interval = self.retry_interval
        self.last_write_retry = None
        self.retention_buffer_sorted = False
        self.current_max_measurements_buffer_warning = self.max_measurements_buffer_warning
        self.current_measurements_per_write = self.max_measurements_per_write
        self.user_agent = user_agent
        self._write_lock = asyncio.Lock()

        proto = "https" if self.config.tls_enabled else "http"
        host = _format_url_host(self.config.hostname)
        self.base_url = f"{proto}://{host}:{self.config.port}"

    def connect(self) -> None:
        raise RuntimeError("InfluxHandler initialization is asynchronous; use task_loop()")

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

    async def close(self) -> None:
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
                except Exception as exc:
                    log.debug("Failed to parse QuestDB version response: %s", exc)
                    self.questdb_version = "unknown version"
                try:
                    await self._ensure_questdb_schema(auth=auth)
                except Exception as exc:
                    log.error("Failed to ensure QuestDB schema for '%s': %s", self.config.measurement_name, exc)
            else:
                resp = await self.client.get("/ping")
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            now = datetime.now(UTC)
            if not self.connection_lost:
                log.error(
                    "%s '%s' unreachable: %s — buffering data until connection is restored",
                    self.name, self.config.hostname, exc,
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

    async def _ensure_questdb_schema(self, *, auth=None) -> None:
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

    def convert_measurement(self, measurement: FritzMeasurement) -> str:
        if not isinstance(measurement, FritzMeasurement):
            log.error("Measurement needs to be a 'FritzMeasurement' but got '%s'", type(measurement))
            return ""
        if self.version == "questdb" and "." in measurement.name:
            # QuestDB rejects dots in column names (e.g. "802.11" in wlan metric names)
            original_name = measurement.name
            measurement.name = original_name.replace(".", "_")
            result = measurement.to_line_protocol(self.config.measurement_name)
            measurement.name = original_name
            return result
        return measurement.to_line_protocol(self.config.measurement_name)

    def permitted_to_write_data(self):
        if self.last_write_retry is None:
            return True
        if self.current_retry_interval > self.max_retry_interval:
            self.current_retry_interval = self.max_retry_interval
        if (datetime.now(UTC) - self.last_write_retry).total_seconds() >= self.current_retry_interval:
            return True
        return False

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
        if self.client is None:
            if not await self._init_client():
                return
        if not force and not self.permitted_to_write_data():
            return
        if len(self.buffer) == 0:
            return

        if self.out_of_retention_period_range and not self.retention_buffer_sorted:
            self.buffer.sort(key=lambda m: m.timestamp, reverse=True)
            self.retention_buffer_sorted = True

        local_buffer = self.buffer[:self.current_measurements_per_write]

        data_lines: list[str] = []
        valid_measurements: list[FritzMeasurement] = []
        invalid_count = 0
        for measurement in local_buffer:
            line = self.convert_measurement(measurement)
            if line:
                data_lines.append(line)
                valid_measurements.append(measurement)
            else:
                invalid_count += 1
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
                    log.error(
                        "Retryable %s write failure for '%s': %s: %.500s — retrying in %ss",
                        self.name,
                        self.config.hostname,
                        resp.status_code,
                        exception_message,
                        self.current_retry_interval,
                    )
                    self.connection_lost = True
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
            now = datetime.now(UTC)
            if not self.connection_lost:
                log.error("%s '%s' unreachable: %s — buffering data until connection is restored",
                          self.name, self.config.hostname, exc)
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
                    len(self.buffer),
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
            if self.connection_lost and self.client is None:
                # Transport error: use a fixed backoff step instead of a second
                # doubling (HTTP-level errors already doubled the interval above).
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

    async def check_buffer(self):
        length = len(self.buffer)
        max_length = self.max_measurements_buffer_size
        percent_buffer_usage = 100 / max_length * length
        if length > max_length:
            log.critical(f"{self.name} measurement buffer length '{length}' exceeded the maximum. Discarding old measurements.")
            self.buffer[:] = self.buffer[0 - max_length:]
        elif percent_buffer_usage >= self.current_max_measurements_buffer_warning:
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
                await self.check_buffer()
                await asyncio.sleep(0.1 if self.out_of_retention_period_range else 1)
        finally:
            await self._flush_buffer_before_close()
            await self.close()


class InfluxLogAndConfigWriter:
    name = "InfluxLogAndConfigWriter"
    log_record_type = "fritzFlux"

    def __init__(self, config: FritzBoxConfig, log_queue: asyncio.Queue):
        if not isinstance(config, FritzBoxConfig):
            raise ValueError("param 'config' needs to be a 'FritzBoxConfig' object")
        if not isinstance(log_queue, asyncio.Queue):
            raise ValueError("param 'log_queue' needs to be a 'asyncio.Queue' object")

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
