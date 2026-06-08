#!/usr/bin/env python3
#
# fritzfluxdb/classes/influxdb/handler.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
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

    def __init__(self, config, user_agent: str | None = None):
        self.config = InfluxDBConfig(config)
        self.version = int(self.config.version)
        self.client: httpx.AsyncClient | None = None
        self.init_successful = False
        self.connection_lost = False
        self.out_of_retention_period_range = False
        self.last_retention_warning = None
        self.buffer = list()
        self.current_retry_interval = self.retry_interval
        self.last_write_retry = None
        self.current_max_measurements_buffer_warning = self.max_measurements_buffer_warning
        self.current_measurements_per_write = self.max_measurements_per_write
        self.user_agent = user_agent

        proto = "https" if self.config.tls_enabled else "http"
        self.base_url = f"{proto}://{self.config.hostname}:{self.config.port}"

    def connect(self):
        # We handle async initialization inside task_loop now
        self.init_successful = True

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
        except httpx.HTTPError:
            log.exception("Failed to connect to InfluxDB")
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

    def convert_measurement(self, measurement: FritzMeasurement) -> str:
        if not isinstance(measurement, FritzMeasurement):
            log.error(f"Measurement needs to be a 'FritzMeasurement' but got '{type(measurement)}'")
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

    async def write_data(self):
        if not self.permitted_to_write_data():
            return
        if len(self.buffer) == 0:
            return

        if self.out_of_retention_period_range:
            self.buffer = sorted(self.buffer, key=lambda m: m.timestamp, reverse=True)

        local_buffer = self.buffer[:self.current_measurements_per_write]
        data_lines = [self.convert_measurement(x) for x in local_buffer]
        data_lines = [x for x in data_lines if x]
        
        if not data_lines:
            log.error("Dropping %s invalid InfluxDB measurements", len(local_buffer))
            del self.buffer[:len(local_buffer)]
            return

        payload = "\n".join(data_lines)
        write_successful = False
        self.last_write_retry = datetime.now(UTC)
        auth = None

        try:
            if self.version == 1:
                auth = (self.config.username, self.config.password) if self.config.username else None
                params = {"db": self.config.database, "precision": "s"}
                resp = await self.client.post("/write", params=params, content=payload, auth=auth)
            else:
                params = {"org": self.config.organisation, "bucket": self.config.bucket, "precision": "s"}
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
                else:
                    log.error(f"Failed to write to InfluxDB '{self.config.hostname}': {resp.status_code}: {exception_message}")
        except httpx.HTTPError:
            self.connection_lost = True
            log.exception("Failed to write to InfluxDB '%s'", self.config.hostname)
        except Exception:
            log.exception("Unexpected InfluxDB writer failure")
            raise

        if len(self.buffer) == 0:
            self.out_of_retention_period_range = False
            self.current_measurements_per_write = self.max_measurements_per_write

        if write_successful:
            if self.connection_lost:
                log.info(f"Connection to influxDB '{self.config.hostname}' restored.")
            log.debug("Successfully wrote %s measurements to InfluxDB", len(local_buffer))
            del self.buffer[:len(local_buffer)]
            self.connection_lost = False
            self.last_write_retry = None
            self.current_retry_interval = self.retry_interval
            self.out_of_retention_period_range = False
            self.set_num_current_measurements_to_write(self.current_measurements_per_write * 4)
        else:
            if self.connection_lost:
                self.current_retry_interval *= 2

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
        while not await self._init_client():
            await asyncio.sleep(self.retry_interval)

        try:
            while True:
                drained = 0
                while (
                    drained < self.max_measurements_per_write
                    and len(self.buffer) < self.max_measurements_buffer_size
                ):
                    try:
                        measurement = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                    self.buffer.append(measurement)
                    drained += 1
                
                await self.write_data()
                await self.check_buffer()
                await asyncio.sleep(0.1 if self.out_of_retention_period_range else 1)
        finally:
            await self.close()


class InfluxLogAndConfigWriter:
    name = "InfluxLogAndConfigWriter"
    log_measurement_name = "log_entry"
    log_record_type = "fritzFlux"
    timezone_measurement_name = "fritzFlux_setting_timezone"
    timezone_setting_write_interval = 60 * 60 * 12

    def __init__(self, config: FritzBoxConfig, log_queue: asyncio.Queue):
        if not isinstance(config, FritzBoxConfig):
            raise ValueError("param 'config' needs to be a 'FritzBoxConfig' object")
        if not isinstance(log_queue, asyncio.Queue):
            raise ValueError("param 'log_queue' needs to be a 'asyncio.Queue' object")

        self.config = config
        self.log_queue = log_queue
        self.last_timezone_setting_write = None
        self.init_successful = True

    def format_log_record(self, log_record):
        if not isinstance(log_record, LogRecord):
            return None
        log_timestamp = datetime.fromtimestamp(log_record.created, UTC)
        log_msg = "{levelname}: {message}".format(**log_record.__dict__)
        return FritzMeasurement("message", log_msg,
                                measurement=self.log_measurement_name,
                                box_tag=self.config.box_tag,
                                additional_tags={"log_type": self.log_record_type},
                                data_type=str,
                                timestamp=log_timestamp,
                                timestamp_precision=WritePrecision.S)

    def get_timezone_setting_measurement(self):
        return FritzMeasurement("timezone", self.config.timezone,
                                measurement=self.timezone_measurement_name,
                                box_tag=self.config.box_tag,
                                data_type=str)

    def is_time_to_write_timezone_setting(self):
        if self.last_timezone_setting_write is None:
            return True
        if (datetime.now(UTC) - self.last_timezone_setting_write).total_seconds() >= self.timezone_setting_write_interval:
            return True
        return False

    async def task_loop(self, output_queue: asyncio.Queue):
        max_log_records_per_tick = 1_000
        
        while True:
            for _ in range(max_log_records_per_tick):
                try:
                    log_record = self.log_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                formatted_log_record = self.format_log_record(log_record)
                if formatted_log_record is not None:
                    await output_queue.put(formatted_log_record)

            if self.is_time_to_write_timezone_setting():
                timezone_measurement = self.get_timezone_setting_measurement()
                await output_queue.put(timezone_measurement)
                self.last_timezone_setting_write = datetime.now(UTC)

            await asyncio.sleep(1)

# EOF
