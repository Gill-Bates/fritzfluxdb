#!/usr/bin/env python3
#
# app/influxdb/config.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import configparser
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any, ClassVar
from urllib.parse import urlsplit

from pydantic import (
    ConfigDict,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.utils.common import parse_bool
from app.utils.log import get_logger
from app.utils.settings import ConfigModel

log = get_logger()
_QUESTDB_NAME = re.compile(r"[A-Za-z0-9_]{1,100}")


@dataclass(frozen=True)
class QuestDBDownsamplingProfile:
    raw_retention_days: int
    rollup_interval: str


QUESTDB_DOWNSAMPLING_PROFILES: dict[str, QuestDBDownsamplingProfile] = {
    "low": QuestDBDownsamplingProfile(raw_retention_days=30, rollup_interval="1m"),
    "medium": QuestDBDownsamplingProfile(raw_retention_days=7, rollup_interval="1m"),
    "high": QuestDBDownsamplingProfile(raw_retention_days=1, rollup_interval="5m"),
}
QUESTDB_DOWNSAMPLING_STATE_TABLE = "_fritzfluxdb_downsampling"
# Version, group count and hash together fix which view holds which column; changing any
# of them is a schema change.
QUESTDB_DOWNSAMPLING_SCHEMA_VERSION = 1
QUESTDB_ROLLUP_GROUP_COUNT = 16
QUESTDB_ROLLUP_HASH = "crc32"
QUESTDB_DOWNSAMPLING_DISABLED_PROFILE = "disabled"


@contextmanager
def _temporary_env(overrides: dict[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _db_label(version: object) -> str:
    return "QuestDB" if str(version).strip().lower() == "questdb" else "InfluxDB"


class InfluxDBConfig(ConfigModel):
    """Parses and validates InfluxDB-compatible writer configuration."""

    model_config = ConfigDict(frozen=True)

    config_section_name: ClassVar[str] = "influxdb"
    label: ClassVar[str] = "InfluxDB"
    version: int | str = 1
    hostname: str | None = None
    port: int = 8086
    tls_enabled: bool = False
    verify_tls: bool = True
    allow_plaintext_credentials: bool = False
    measurement_name: str = "fritzbox"
    # QuestDB only: TTL applied to a table which has none yet, 0 disables it
    data_retention_days: int = 365
    downsampling: str = ""

    # version 1 parameters
    username: str | None = None
    password: SecretStr | None = None
    database: str | None = None

    # version 2 parameters
    token: SecretStr | None = None
    organization: str | None = None
    bucket: str | None = None

    @classmethod
    def prepare(cls, config_data: configparser.ConfigParser) -> tuple[dict[str, Any], bool]:
        pre_error = False

        # Check if database type is questdb (via DB_TYPE env var, version, or QUESTDB_HOSTNAME)
        db_type = os.environ.get("DB_TYPE", "").strip().lower()
        env_version = os.environ.get("INFLUXDB_VERSION", "").strip().lower()

        file_version = ""
        if config_data.has_section(cls.config_section_name):
            file_version = config_data.get(cls.config_section_name, "version", fallback="").strip().lower()

        explicit_version = env_version or file_version

        # Map precise DB_TYPE (influxdb_v1 / influxdb_v2 / questdb) to version strings
        if db_type == "influxdb_v1":
            mapped_version = "1"
        elif db_type == "influxdb_v2":
            mapped_version = "2"
        elif db_type == "questdb":
            mapped_version = "questdb"
        elif db_type:
            log.error("Invalid DB_TYPE '%s'. Use influxdb_v1, influxdb_v2 or questdb.", db_type)
            pre_error = True
            mapped_version = ""
        else:
            mapped_version = explicit_version

        questdb_host_configured = bool(os.environ.get("QUESTDB_HOSTNAME", "").strip())

        # A QUESTDB_* host only selects QuestDB when no backend was chosen explicitly;
        # otherwise a stale variable would silently override DB_TYPE/INFLUXDB_VERSION.
        explicit_backend = bool(db_type) or bool(explicit_version)
        is_questdb = mapped_version == "questdb" or (questdb_host_configured and not explicit_backend)

        if is_questdb:
            mapped_version = "questdb"
        elif questdb_host_configured:
            log.warning(
                "Ignoring QUESTDB_* environment variables: the configured backend is '%s'",
                mapped_version or "influxdb",
            )

        env_overrides: dict[str, str] = {}
        if mapped_version:
            env_overrides["INFLUXDB_VERSION"] = mapped_version

        if is_questdb:
            # Map QUESTDB_* env vars to INFLUXDB_* env vars if using QuestDB
            questdb_mapping = {
                "QUESTDB_HOSTNAME": "INFLUXDB_HOSTNAME",
                "QUESTDB_PORT": "INFLUXDB_PORT",
                "QUESTDB_USERNAME": "INFLUXDB_USERNAME",
                "QUESTDB_PASSWORD": "INFLUXDB_PASSWORD",
                "QUESTDB_TLS_ENABLED": "INFLUXDB_TLS_ENABLED",
                "QUESTDB_VERIFY_TLS": "INFLUXDB_VERIFY_TLS",
                "QUESTDB_MEASUREMENT_NAME": "INFLUXDB_MEASUREMENT_NAME",
                "QUESTDB_DATA_RETENTION_DAYS": "INFLUXDB_DATA_RETENTION_DAYS",
                "QUESTDB_DOWNSAMPLING": "INFLUXDB_DOWNSAMPLING",
                "QUESTDB_ALLOW_PLAINTEXT_CREDENTIALS": "INFLUXDB_ALLOW_PLAINTEXT_CREDENTIALS",
            }
            for q_var, i_var in questdb_mapping.items():
                if q_var in os.environ:
                    env_overrides[i_var] = os.environ[q_var]

        if is_questdb and os.environ.get("QUESTDB_TOKEN", "").strip():
            log.error("QUESTDB_TOKEN is unsupported by QuestDB Open Source; use HTTP basic authentication")
            pre_error = True
        with _temporary_env(env_overrides):
            return cls.collect_raw(config_data), pre_error

    def required_options(self) -> tuple[str, ...]:
        if self.version == 1:
            return ("hostname", "database")
        if self.version == 2:
            return ("hostname", "token", "organization", "bucket")
        return ("hostname",)

    @property
    def db_label(self) -> str:
        return _db_label(self.version)

    @field_validator("version", mode="before")
    @classmethod
    def _normalize_version(cls, value):
        version = str(value).strip().lower()
        if version in {"1", "2"}:
            return int(version)
        if version == "questdb":
            return version
        raise ValueError(f"Invalid database version/type '{version}'.")

    @field_validator("downsampling", mode="before")
    @classmethod
    def _normalize_downsampling(cls, value):
        profile = str(value or "").strip().lower()
        if profile not in {"", *QUESTDB_DOWNSAMPLING_PROFILES}:
            raise ValueError(f"Invalid QuestDB downsampling profile '{profile}'. Use low, medium or high.")
        return profile

    @model_validator(mode="before")
    @classmethod
    def _derive_connection(cls, data: Any, info: ValidationInfo) -> Any:
        """Apply version-dependent defaults and read a hostname given as URL."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        lenient = bool((info.context or {}).get("lenient"))
        try:
            return cls._derive_connection_values(data)
        except ValueError:
            if lenient:
                # A rejected value is rebuilt leniently after logging; keep it out of debug logs.
                data["hostname"] = None
                if str(data.get("version", "")).strip().lower() == "questdb":
                    data.setdefault("port", 9000)
                return data
            raise

    @classmethod
    def _derive_connection_values(cls, data: dict[str, Any]) -> dict[str, Any]:
        is_questdb = str(data.get("version", "")).strip().lower() == "questdb"
        db_label = _db_label("questdb" if is_questdb else "")

        if not is_questdb:
            # a profile only applies to QuestDB
            data["downsampling"] = ""

        # accept a full URL as hostname (e.g. behind a reverse proxy) and
        # derive TLS and port from scheme/URL instead of separate settings
        hostname_str = str(data.get("hostname") or "").strip()
        tls_set_by_url = False
        if "://" in hostname_str:
            try:
                parsed = urlsplit(hostname_str)
                url_port = parsed.port  # raises ValueError for a non-numeric or out-of-range port
            except ValueError:
                raise ValueError(f"Invalid {db_label} hostname URL.") from None
            if parsed.username is not None or parsed.password is not None:
                raise ValueError(f"{db_label} hostname URL must not contain credentials.")
            if parsed.path.strip("/") or parsed.query or parsed.fragment:
                # a sub-path is never applied to the /write and /exec endpoints,
                # so accepting it would silently write to the wrong URL
                raise ValueError(f"{db_label} hostname URL must not contain a path, query or fragment.")
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or any(char.isspace() for char in parsed.hostname)
            ):
                raise ValueError(f"Invalid {db_label} hostname URL.")

            tls = parsed.scheme == "https"
            data["tls_enabled"] = tls
            tls_set_by_url = True
            data["hostname"] = parsed.hostname
            if url_port is not None:
                data["port"] = url_port
            elif "port" not in data:
                data["port"] = 443 if tls else 80
            log.info(
                "Hostname given as URL: using %s to '%s' port %s",
                "HTTPS" if tls else "HTTP", parsed.hostname, data["port"],
            )
        elif hostname_str:
            host_value = hostname_str
            if host_value.startswith("[") and host_value.endswith("]"):
                host_value = host_value[1:-1]
            try:
                address = ip_address(host_value)
                is_ip_address = True
                is_ipv6 = address.version == 6
            except ValueError:
                is_ip_address = False
                is_ipv6 = False
            bracketed_ipv6 = hostname_str.startswith("[") and hostname_str.endswith("]") and is_ipv6
            if (
                any(char.isspace() for char in hostname_str)
                or any(char in hostname_str for char in "/?#@")
                or (":" in hostname_str and not is_ip_address)
                or (("[" in hostname_str or "]" in hostname_str) and not bracketed_ipv6)
            ):
                raise ValueError(f"Invalid {db_label} hostname.")
            data["hostname"] = hostname_str

        # Applied after the URL branch so a URL without a port gets its scheme default, not 9000.
        if is_questdb and "port" not in data:
            data["port"] = 9000

        # port 443 without an explicit scheme implies TLS (e.g. a plain hostname with a reverse proxy)
        try:
            tls_now = parse_bool(data.get("tls_enabled", False))
            port_now = int(data.get("port", 8086))
        except (TypeError, ValueError):
            return data  # the field validators report the unparsable value
        if port_now == 443 and not tls_now and not tls_set_by_url:
            data["tls_enabled"] = True
            log.info("Port 443 implies TLS; enabling HTTPS for '%s'", data.get("hostname"))
        return data

    @field_validator("port")
    @classmethod
    def _validate_port(cls, value: int, info: ValidationInfo) -> int:
        if not (1 <= value <= 65535):
            raise ValueError(f"{cls._label_of(info)} port must be between 1 and 65535, got {value}")
        return value

    @field_validator("measurement_name")
    @classmethod
    def _validate_measurement_name(cls, value: str, info: ValidationInfo) -> str:
        # QuestDB uses the name as a table name; main() derives the same charset from the serial number.
        if str(info.data.get("version")) == "questdb":
            if not _QUESTDB_NAME.fullmatch(value):
                raise ValueError(
                    "QuestDB measurement name must consist of 1 to 100 letters, digits or underscores"
                )
        elif not value.strip() or len(value) > 255 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("InfluxDB measurement name must be 1 to 255 characters without control characters")
        return value

    @field_validator("data_retention_days")
    @classmethod
    def _validate_retention(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"Data retention days must be 0 (disabled) or greater, got {value}")
        return value

    @staticmethod
    def _label_of(info: ValidationInfo) -> str:
        return _db_label(info.data.get("version"))

    @model_validator(mode="after")
    def _check_consistency(self, info: ValidationInfo) -> "InfluxDBConfig":
        if (info.context or {}).get("lenient"):
            return self

        db_label = self.db_label
        problems: list[str] = []

        if self.tls_enabled and not self.verify_tls:
            log.warning(
                "TLS certificate verification is disabled for %s at %s; use only on trusted networks",
                db_label, self.hostname,
            )

        credentials = (self.username, self.password) if self.version == "questdb" else (
            self.username, self.password, self.token
        )
        if (not self.tls_enabled
                and any(bool(v) for v in credentials)
                and self.hostname
                and self.hostname not in {"localhost", "127.0.0.1", "::1"}):
            if self.allow_plaintext_credentials:
                log.warning(
                    "%s credentials are sent over plain HTTP to '%s'; use only on trusted networks",
                    db_label, self.hostname,
                )
            else:
                problems.append(
                    f"{db_label} credentials must not be sent over plain HTTP to '{self.hostname}'. "
                    f"Enable TLS or set {db_label.upper()}_ALLOW_PLAINTEXT_CREDENTIALS=true for trusted networks."
                )

        if self.version in (1, "questdb") and bool(self.username) != bool(self.password):
            if self.version == "questdb":
                problems.append("QuestDB username and password must be defined together or not at all")
            else:
                problems.append(
                    f"Username and password must be defined together or not at all for InfluxDB '{self.version}'"
                )


        if problems:
            raise ValueError("\n".join(problems))
        return self
