#!/usr/bin/env python3
#
# app/utils/measurement.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from contextlib import suppress
from datetime import UTC, datetime, timedelta
from math import isfinite
from types import MappingProxyType
from typing import ClassVar

from app.utils.common import parse_bool
from app.utils.log import get_logger

log = get_logger()

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# Metric names already reported as unwritable; keeps a persistent NaN from logging every poll.
_unwritable_warned: set[tuple[str, str]] = set()
_UNWRITABLE_WARNED_MAX = 1000


def _warn_unwritable(name: str, reason: str) -> None:
    key = (name, reason)
    if key in _unwritable_warned or len(_unwritable_warned) >= _UNWRITABLE_WARNED_MAX:
        return
    _unwritable_warned.add(key)
    log.warning("Dropping value of measurement '%s': %s (reported once)", name, reason)
# Single source for the type suffix of a metric name ("name:int"); unknown names map to nothing.
METRIC_TYPES = MappingProxyType({"str": str, "int": int, "float": float, "bool": bool})


class WritePrecision:
    MS = "ms"
    S = "s"
    US = "us"

    VALID: ClassVar[set] = {MS, S, US}


class FritzMeasurement:
    """
        This class holds measurements which should be sanitized to this specification
        https://docs.influxdata.com/influxdb/v2.1/reference/syntax/line-protocol/
    """

    default_box_tag_key = "box"
    default_timestamp_precision = WritePrecision.S

    __slots__ = (
        "additional_tags",
        "box_tag",
        "is_event",
        "measurement",
        "name",
        "timestamp",
        "timestamp_precision",
        "value",
    )

    def __init__(self, key, value,
                 data_type=None, box_tag=None,
                 additional_tags=None, timestamp=None,
                 timestamp_precision=None, measurement=None, is_event=False):

        self.name = str(key)
        self.box_tag = str(box_tag) if box_tag is not None else None
        self.value = None

        # Optional override: this point goes to its own measurement instead of the shared one.
        self.measurement = str(measurement) if measurement is not None else None

        # Events (log lines, call list) are idempotent by tags + timestamp and get their own QuestDB table.
        self.is_event = bool(is_event)

        if data_type is not None:
            try:
                # bool("0") / bool("false") would be True, so route bool through the parser
                self.value = parse_bool(value) if data_type is bool else data_type(value)
            except (TypeError, ValueError) as exc:
                if value is not None:
                    log.error(
                        "Unable to convert measurement '%s' using %s: %s",
                        self.name,
                        getattr(data_type, "__name__", data_type),
                        type(exc).__name__,
                    )
        else:
            self.value = self.sanitize_value(value)

        if not isinstance(timestamp, datetime):
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
            log.error(f"Returned value for '{self.name}' has incompatible type '{type(value)}', "
                      f"returning '0'")
            return 0

        value = value.strip()

        if "." in value:
            with suppress(ValueError):
                return float(value)

        with suppress(ValueError):
            return int(value)

        return value

    @property
    def tags(self):

        tags = {}
        if self.box_tag is not None:
            tags[self.default_box_tag_key] = self.box_tag

        if self.additional_tags is not None:
            tags = {**tags, **self.additional_tags}

        return tags

    # Int fields are signed 64-bit; glitched AVM byte counters near 2**64 would poison the batch.
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

    @classmethod
    def _escape_tag(cls, value: object, *, is_key: bool = False, escape_backslashes: bool = True) -> str:
        """Escape a tag key or value; a line break (or a backslash in a key) is rejected."""
        text = str(value)
        if "\r" in text or "\n" in text or (is_key and ("\\" in text or not text)):
            raise ValueError(f"tag {'key' if is_key else 'value'} {text!r} cannot be written")
        if not is_key and not escape_backslashes and text.endswith("\\"):
            raise ValueError("InfluxDB cannot represent a tag value ending with a backslash")
        # QuestDB decodes doubled backslashes; InfluxDB keeps backslashes before ordinary letters.
        return cls._escape_line_token(text.replace("\\", "\\\\") if not is_key and escape_backslashes else text)

    @staticmethod
    def _escape_field_string(value: str) -> str:
        normalized = value.replace("\r", "\\r").replace("\n", "\\n")
        return '"' + normalized.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def line_protocol_series(
        self, measurement_name: str, exclude_tags: frozenset[str] = frozenset(), *, escape_tag_backslashes: bool = True,
    ) -> str:
        """
            returns the escaped measurement name plus tag set, which identifies the series of this point
        """

        tags_str = ""
        tags = {
            k: v for k, v in self.tags.items()
            if k not in exclude_tags and v is not None and str(v) != ""
        }
        if tags:
            # Escaping rules: https://docs.influxdata.com/influxdb/v2/reference/syntax/line-protocol/#special-characters
            escaped_tags = [
                f"{self._escape_tag(k, is_key=True)}={self._escape_tag(v, escape_backslashes=escape_tag_backslashes)}"
                for k, v in sorted(tags.items())
            ]
            tags_str = "," + ",".join(escaped_tags)

        # escape measurement name (per-measurement override wins over the default)
        effective_measurement = self.measurement if self.measurement is not None else measurement_name
        escaped_meas = self._escape_line_token(effective_measurement, escape_equals=False)

        return f"{escaped_meas}{tags_str}"

    def line_protocol_field(self, field_name: str | None = None) -> str:
        """
            returns the escaped 'key=value' field of this point, or an empty string if the value can't be written
        """

        if self.value is None:
            return ""

        fk = self._escape_line_token(self.name if field_name is None else field_name)
        if isinstance(self.value, bool):
            fv = "true" if self.value else "false"
        elif isinstance(self.value, int):
            if not (self.INT64_MIN <= self.value <= self.INT64_MAX):
                _warn_unwritable(self.name, "integer outside the 64-bit range")
                return ""
            fv = f"{self.value}i"
        elif isinstance(self.value, float):
            if not isfinite(self.value):
                _warn_unwritable(self.name, "non-finite float")
                return ""
            fv = repr(self.value)
        elif isinstance(self.value, str):
            fv = self._escape_field_string(self.value)
        else:
            fv = f"{self.value}"

        return f"{fk}={fv}"

    def line_protocol_timestamp(self) -> int:

        # timedelta division is exact; float seconds * 1e6 can be off by one microsecond
        elapsed = self.timestamp - _EPOCH
        if self.timestamp_precision == WritePrecision.MS:
            return elapsed // timedelta(milliseconds=1)
        if self.timestamp_precision == WritePrecision.US:
            return elapsed // timedelta(microseconds=1)

        return elapsed // timedelta(seconds=1)

    def to_line_protocol(self, measurement_name: str) -> str:

        field = self.line_protocol_field()
        if not field:
            return ""

        return f"{self.line_protocol_series(measurement_name)} {field} {self.line_protocol_timestamp()}"
