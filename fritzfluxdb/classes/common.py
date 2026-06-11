#!/usr/bin/env python3
#
# fritzfluxdb/classes/common.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from datetime import datetime, UTC
from math import isfinite
import configparser
import os

from fritzfluxdb.common import do_error_exit
from fritzfluxdb.log import get_logger

log = get_logger()


class WritePrecision(object):
    MS = "ms"
    S = "s"
    US = "us"

    VALID = {MS, S, US}


class FritzMeasurement:
    """
        This class holds measurements which should be sanitized to this specification
        https://docs.influxdata.com/influxdb/v2.1/reference/syntax/line-protocol/
    """

    default_box_tag_key = "box"
    default_timestamp_precision = WritePrecision.S

    __slots__ = ("name", "value", "box_tag", "timestamp", "additional_tags",
                 "timestamp_precision", "measurement")

    def __init__(self, key, value,
                 data_type=None, box_tag=None,
                 additional_tags=None, timestamp=None,
                 timestamp_precision=None, measurement=None):

        # name and primary tag should always be present
        self.name = str(key)
        self.box_tag = str(box_tag) if box_tag is not None else None
        self.value = None

        # Optional override for the InfluxDB measurement name. When set, this
        # data point is written to its own measurement instead of the shared
        # metrics measurement (used e.g. for log entries and config settings).
        self.measurement = str(measurement) if measurement is not None else None

        if data_type is not None:
            try:
                self.value = data_type(value)
            except (TypeError, ValueError) as exc:
                if value is not None:
                    log.error(
                        "Unable to convert measurement '%s' value %r using %s: %s",
                        self.name,
                        value,
                        getattr(data_type, "__name__", data_type),
                        exc,
                    )
        else:
            self.value = self.sanitize_value(value)

        if timestamp is None or not isinstance(timestamp, datetime):
            self.timestamp = datetime.now(UTC)
        elif timestamp.tzinfo is None or timestamp.utcoffset() is None:
            log.warning("FritzMeasurement '%s' received naive timestamp; assuming UTC", self.name)
            self.timestamp = timestamp.replace(tzinfo=UTC)
        else:
            self.timestamp = timestamp.astimezone(UTC)

        self.update_timestamp_precision(timestamp_precision)

        self.additional_tags = None

        if isinstance(additional_tags, dict):
            if self.box_tag is not None and self.default_box_tag_key in additional_tags:
                log.warning(
                    "additional_tags must not override reserved tag '%s'; key ignored",
                    self.default_box_tag_key,
                )
                additional_tags = {k: v for k, v in additional_tags.items() if k != self.default_box_tag_key}
            self.additional_tags = dict(additional_tags)

    def __repr__(self):
        return f"{self.timestamp}: {self.name}={self.value} ({self.tags})"

    def update_timestamp_precision(self, precision=None):

        if self.timestamp is None:
            return

        if precision is None:
            precision = self.default_timestamp_precision

        if precision not in WritePrecision.VALID:
            raise ValueError(f"invalid timestamp precision '{precision}'")

        if precision == WritePrecision.MS:
            self.timestamp = self.timestamp.replace(
                microsecond=(self.timestamp.microsecond // 1_000) * 1_000
            )
        elif precision == WritePrecision.S:
            self.timestamp = self.timestamp.replace(microsecond=0)

        self.timestamp_precision = precision

    def sanitize_value(self, value):

        if value is None:
            return None

        if isinstance(value, (int, bool, float)):
            return value

        if not isinstance(value, str):
            log.error(f"Returned value '{value}' for '{self.name}' has incompatible type '{type(value)}', "
                      f"returning '0'")
            return 0

        if "." in value:
            # noinspection PyBroadException
            try:
                # try to convert value to float
                return float(value)
            except Exception:
                pass

        # noinspection PyBroadException
        try:
            # try to convert value to int
            return int(value)
        except Exception:
            pass

        return value.strip()

    @property
    def tags(self):

        tags = dict()
        if self.box_tag is not None:
            tags[self.default_box_tag_key] = self.box_tag

        if self.additional_tags is not None:
            tags = {**tags, **self.additional_tags}

        return tags

    # InfluxDB integer fields are signed 64-bit. The FritzBox occasionally
    # reports glitched byte counters (an AVM-side under-/overflow yielding a
    # near-2**64 value) that exceed this range. Such a value cannot be written
    # as an int field and would otherwise poison the whole write batch.
    INT64_MIN = -(2 ** 63)
    INT64_MAX = 2 ** 63 - 1

    @staticmethod
    def _escape_line_token(value: object, *, escape_equals: bool = True) -> str:
        escaped = (
            str(value)
            .replace("\r", "\\r")
            .replace("\n", "\\n")
            .replace(",", "\\,")
            .replace(" ", "\\ ")
        )
        if escape_equals:
            escaped = escaped.replace("=", "\\=")
        return escaped

    @staticmethod
    def _escape_field_string(value: str) -> str:
        normalized = value.replace("\r", "\\r").replace("\n", "\\n")
        return '"' + normalized.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def to_line_protocol(self, measurement_name: str) -> str:
        if self.value is None:
            return ""

        # format tags
        tags_str = ""
        tags = self.tags
        if tags:
            # properly escape keys and values: https://docs.influxdata.com/influxdb/v2/reference/syntax/line-protocol/#special-characters
            escaped_tags = [
                f"{self._escape_line_token(k)}={self._escape_line_token(v)}"
                for k, v in sorted(tags.items())
            ]
            tags_str = "," + ",".join(escaped_tags)

        # escape measurement name (per-measurement override wins over the default)
        effective_measurement = self.measurement if self.measurement is not None else measurement_name
        escaped_meas = self._escape_line_token(effective_measurement, escape_equals=False)

        # format fields
        fk = self._escape_line_token(self.name)
        if isinstance(self.value, bool):
            fv = "true" if self.value else "false"
        elif isinstance(self.value, int):
            if not (self.INT64_MIN <= self.value <= self.INT64_MAX):
                return ""
            fv = f"{self.value}i"
        elif isinstance(self.value, float):
            if not isfinite(self.value):
                return ""
            fv = repr(self.value)
        elif isinstance(self.value, str):
            fv = self._escape_field_string(self.value)
        else:
            fv = f"{self.value}"
        fields_str = f"{fk}={fv}"

        # format timestamp
        ts = int(self.timestamp.timestamp())
        if self.timestamp_precision == WritePrecision.MS:
            ts = int(self.timestamp.timestamp() * 1000)
        elif self.timestamp_precision == WritePrecision.US:
            ts = int(self.timestamp.timestamp() * 1000000)
        elif self.timestamp_precision == WritePrecision.S:
            ts = int(self.timestamp.timestamp())

        return f"{escaped_meas}{tags_str} {fields_str} {ts}"

    def __hash__(self) -> int:
        return hash((self.name, self.value, self.box_tag, self.timestamp))


class ConfigBase:
    """
        Base class to parse config data
    """

    sensitive_keys = {"password", "token", "secret", "key"}

    not_config_vars = [
        "config_section_name",
        "__module__",
        "__doc__"
    ]

    parser_error = False

    def __init__(self, config_data: configparser.ConfigParser):

        if not isinstance(config_data, configparser.ConfigParser):
            do_error_exit("config data is not a config parser object")

        self.parse_config(config_data)

    @staticmethod
    def to_bool(value):
        """
            converts a string to a boolean
        """
        valid = {
             'true': True, 't': True, '1': True,
             'false': False, 'f': False, '0': False,
             }

        if isinstance(value, bool):
            return value

        elif isinstance(value, str):
            if value.lower() in valid:
                return valid[value.lower()]

        raise ValueError

    def parse_config(self, config_data):
        """
            generic method to parse config data and also takes care of reading equivalent env var
        """

        config_section_name = getattr(self.__class__, "config_section_name", None)

        if config_section_name is None:
            raise KeyError(f"Class '{self.__class__.__name__}' is missing 'config_section_name' attribute")

        for config_option in [x for x in vars(self.__class__) if x not in self.__class__.not_config_vars]:

            var_config = getattr(self.__class__, config_option)

            if not isinstance(var_config, dict):
                continue

            var_type = var_config.get("type", str)
            var_alt = var_config.get("alt")
            var_default = var_config.get("default")

            config_value = config_data.get(config_section_name, config_option, fallback=None)
            if config_value is None and var_alt is not None:
                config_value = config_data.get(config_section_name, var_alt, fallback=None)

            config_value = os.environ.get(f"{config_section_name}_{config_option}".upper(), config_value)

            # treat empty / whitespace-only strings as absent (e.g. Unraid sends "" for unset fields)
            if isinstance(config_value, str) and not config_value.strip():
                config_value = None

            if config_value is not None and var_type is bool:
                try:
                    config_value = self.to_bool(config_value)
                except ValueError:
                    log.error(f"Unable to parse '{config_value}' for '{config_option}' as bool")
                    config_value = var_default

            elif config_value is not None and var_type is int:
                try:
                    config_value = int(config_value)
                except ValueError:
                    log.error(f"Unable to parse '{config_value}' for '{config_option}' as int")
                    config_value = var_default

            else:
                if config_value is None:
                    config_value = var_default

            debug_value = config_value
            if any(k in config_option.lower() for k in self.sensitive_keys):
                debug_value = "***"

            log.debug("Config: %s.%s = %s", config_section_name, config_option, debug_value)

            setattr(self, config_option, config_value)
