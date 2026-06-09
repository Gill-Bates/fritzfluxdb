#!/usr/bin/env python3
#
# fritzfluxdb/classes/influxdb/handler.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
import time
from datetime import UTC, datetime
from logging import LogRecord

import httpx

from fritzfluxdb.classes.influxdb.config import InfluxDBConfig
from fritzfluxdb.log import get_logger
from fritzfluxdb.classes.common import FritzMeasurement, WritePrecision
from fritzfluxdb.classes.fritzbox.config import FritzBoxConfig

log = get_logger()

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
        self.version = int(self.config.version)
        if self.version not in {1, 2}:
            raise ValueError(f"Unsupported InfluxDB version: {self.version}")
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

        proto = "https" if self.config.tls_enabled else "http"
        self.base_url = f"{proto}://{self.config.hostname}:{self.config.port}"

    def connect(self) -> None:
        # Async initialization happens inside task_loop via _init_client()
        pass

    def append_measurement(self, measurement: FritzMeasurement) -> None:
        if len(self.buffer) >= self.max_measurements_buffer_size:
            del self.buffer[0]
            log.warning("InfluxDB measurement buffer is full; dropping oldest measurement")
        self.buffer.append(measurement)
        if self.out_of_retention_period_range:
            self.retention_buffer_sorted = False

    async def close(self) -> None:
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
            else:
                resp = await self.client.get("/ping")
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            now = datetime.now(UTC)
            if not self.connection_lost:
                log.error(
                    "InfluxDB '%s' unreachable: %s — buffering data until connection is restored",
                    self.config.hostname, exc,
                )
                self.last_connection_warning = now
            elif (
                self.last_connection_warning is None
                or (now - self.last_connection_warning).total_seconds() >= self.connection_warning_interval
            ):
                log.warning(
                    "InfluxDB '%s' still unreachable — %s measurement(s) buffered, retrying...",
                    self.config.hostname, len(self.buffer),
                )
                self.last_connection_warning = now
            else:
                log.debug("InfluxDB '%s' still unreachable, retrying...", self.config.hostname)
            self.connection_lost = True
            self.init_successful = False
            await self.client.aclose()
            self.client = None
            return False

        self.init_successful = True
        log.info("Connection to InfluxDB %s established", self.version)
        return True

    @staticmethod
    def _is_retention_drop(message: str) -> bool:
        """True if an InfluxDB write response reports points dropped because they
        fall outside the retention policy (a non-retryable partial write).

        Matches the wording across InfluxDB versions, e.g.
        'points beyond retention policy' and
        'partial write: dropped N points outside retention policy ...
         violates a Retention Policy Lower Bound'.
        Deliberately does NOT match other partial writes (e.g. field type
        conflicts), which must still surface as errors.
        """
        msg = (message or "").lower()
        return "retention policy" in msg and (
            "partial write" in msg
            or "dropped" in msg
            or "beyond retention policy" in msg
        )

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
                "Timed out while flushing %s buffered InfluxDB measurement(s) before shutdown",
                len(self.buffer),
            )

    async def write_data(self, *, force: bool = False):
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
            log.error("Dropping %s invalid InfluxDB measurement(s) from current batch", invalid_count)
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
            else:
                params = {"org": self.config.organization, "bucket": self.config.bucket, "precision": "s"}
                resp = await self.client.post("/api/v2/write", params=params, content=payload)

            if resp.status_code in (200, 204):
                write_successful = True
            else:
                exception_message = resp.text
                if self._is_retention_drop(exception_message):
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
                            "InfluxDB '%s' is discarding measurements older than its "
                            "retention policy; these points cannot be stored "
                            "(suppressing similar notices for %ss)",
                            self.config.hostname,
                            self.retention_warning_interval,
                        )
                        self.last_retention_warning = now
                    else:
                        log.debug(
                            "InfluxDB dropped %s measurement(s) outside the retention policy",
                            len(local_buffer),
                        )
                    self.out_of_retention_period_range = True
                    del self.buffer[:len(local_buffer)]
                    self.current_retry_interval = self.retry_interval
                    self.last_write_retry = None
                elif self._is_non_retryable_write_error(resp.status_code, exception_message):
                    log.error(
                        "Dropping %s non-retryable InfluxDB measurement(s): %s: %.500s",
                        len(local_buffer),
                        resp.status_code,
                        exception_message,
                    )
                    del self.buffer[:len(local_buffer)]
                    self.last_write_retry = None
                elif resp.status_code == 413:
                    if self.current_measurements_per_write == 1:
                        log.error(
                            "Dropping oversized InfluxDB measurement (single line exceeds server limit): %.500s",
                            exception_message,
                        )
                        del self.buffer[0]
                        self.last_write_retry = None
                    else:
                        new_batch = max(1, self.current_measurements_per_write // 2)
                        log.error(
                            "InfluxDB write payload too large (%s measurements); reducing batch size to %s",
                            self.current_measurements_per_write,
                            new_batch,
                        )
                        self.set_num_current_measurements_to_write(new_batch)
                        self.current_retry_interval = min(
                            self.current_retry_interval * 2, self.max_retry_interval
                        )
                elif resp.status_code in self._retryable_status_codes:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and retry_after.isdecimal():
                        self.current_retry_interval = min(int(retry_after), self.max_retry_interval)
                    else:
                        self.current_retry_interval = min(
                            self.current_retry_interval * 2, self.max_retry_interval
                        )
                    log.error(
                        "Retryable InfluxDB write failure for '%s': %s: %.500s — retrying in %ss",
                        self.config.hostname,
                        resp.status_code,
                        exception_message,
                        self.current_retry_interval,
                    )
                    self.connection_lost = True
                elif resp.status_code in {401, 403, 404}:
                    log.error(
                        "Non-retryable InfluxDB auth/config failure for '%s': %s: %.500s — "
                        "backing off to max interval",
                        self.config.hostname,
                        resp.status_code,
                        exception_message,
                    )
                    self.current_retry_interval = self.max_retry_interval
                    self.connection_lost = True
                else:
                    log.error(
                        "Failed to write to InfluxDB '%s': %s: %.500s",
                        self.config.hostname,
                        resp.status_code,
                        exception_message,
                    )
        except httpx.HTTPError as exc:
            now = datetime.now(UTC)
            if not self.connection_lost:
                log.error("InfluxDB '%s' unreachable: %s — buffering data until connection is restored",
                          self.config.hostname, exc)
                self.last_connection_warning = now
            elif (
                self.last_connection_warning is None
                or (now - self.last_connection_warning).total_seconds() >= self.connection_warning_interval
            ):
                log.warning(
                    "InfluxDB '%s' still unreachable — %s measurement(s) buffered, retrying...",
                    self.config.hostname, len(self.buffer),
                )
                self.last_connection_warning = now
            else:
                log.debug("InfluxDB '%s' still unreachable, retrying...", self.config.hostname)
            self.connection_lost = True
            if self.client is not None:
                await self.client.aclose()
                self.client = None
        except Exception:
            log.exception("Unexpected InfluxDB writer failure")
            raise

        if len(self.buffer) == 0:
            self.out_of_retention_period_range = False
            self.retention_buffer_sorted = False
            self.current_measurements_per_write = self.max_measurements_per_write

        if write_successful:
            if self.connection_lost:
                log.info(
                    "InfluxDB '%s' connection restored — flushing %s buffered measurements",
                    self.config.hostname,
                    len(self.buffer),
                )
            self.last_connection_warning = None
            log.debug("Successfully wrote %s measurements to InfluxDB", len(local_buffer))
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
            log.critical(f"InfluxDB measurement buffer length '{length}' exceeded the maximum. Discarding old measurements.")
            self.buffer[:] = self.buffer[0 - max_length:]
        elif percent_buffer_usage >= self.current_max_measurements_buffer_warning:
            log.warning(
                "InfluxDB measurement buffer usage is %.1f%% (%s/%s)",
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
