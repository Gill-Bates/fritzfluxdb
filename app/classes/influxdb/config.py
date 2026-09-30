#!/usr/bin/env python3
#
# app/classes/influxdb/config.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import configparser
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import urlsplit

from app.classes.common import ConfigBase
from app.log import get_logger

log = get_logger()


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
QUESTDB_DOWNSAMPLING_SCHEMA_VERSION = 1
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


class InfluxDBConfig(ConfigBase):
    """Parses and validates InfluxDB-compatible writer configuration."""

    version: ClassVar[dict] = {
        "type": str,
        "default": "1"
    }
    hostname: ClassVar[dict] = {
        "type": str,
        "alt": "host",
        "default": None
    }
    port: ClassVar[dict] = {
        "type": int,
        "default": 8086
    }
    tls_enabled: ClassVar[dict] = {
        "type": bool,
        "alt": "ssl",
        "default": False
    }
    verify_tls: ClassVar[dict] = {
        "type": bool,
        "alt": "verify_ssl",
        "default": True
    }
    allow_plaintext_credentials: ClassVar[dict] = {
        "type": bool,
        "default": False
    }
    measurement_name: ClassVar[dict] = {
        "type": str,
        "default": "fritzbox"
    }
    # QuestDB only: TTL applied to a table which has none yet, 0 disables it
    data_retention_days: ClassVar[dict] = {
        "type": int,
        "default": 365
    }
    downsampling: ClassVar[dict] = {
        "type": str,
        "default": ""
    }

    # version 1 parameters
    username: ClassVar[dict] = {
        "type": str,
        "default": None
    }
    password: ClassVar[dict] = {
        "type": str,
        "default": None
    }
    database: ClassVar[dict] = {
        "type": str,
        "default": None
    }

    # version 2 parameters
    token: ClassVar[dict] = {
        "type": str,
        "default": None
    }
    organization: ClassVar[dict] = {
        "type": str,
        "default": None
    }
    bucket: ClassVar[dict] = {
        "type": str,
        "default": None
    }

    config_section_name = "influxdb"

    def parse_config(self, config_data: configparser.ConfigParser):

        # Check if database type is questdb (via DB_TYPE env var, version, or QUESTDB_HOSTNAME/QUESTDB_HOST)
        db_type = os.environ.get("DB_TYPE", "").strip().lower()
        env_version = os.environ.get("INFLUXDB_VERSION", "").strip().lower()

        file_version = ""
        if config_data.has_section(self.config_section_name):
            file_version = config_data.get(self.config_section_name, "version", fallback="").strip().lower()

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
            self.parser_error = True
            mapped_version = ""
        else:
            mapped_version = explicit_version

        _questdb_host_vars = frozenset({"QUESTDB_HOSTNAME", "QUESTDB_HOST"})
        questdb_host_configured = any(os.environ.get(var, "").strip() for var in _questdb_host_vars)

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
                "QUESTDB_HOST": "INFLUXDB_HOSTNAME",
                "QUESTDB_PORT": "INFLUXDB_PORT",
                "QUESTDB_USERNAME": "INFLUXDB_USERNAME",
                "QUESTDB_PASSWORD": "INFLUXDB_PASSWORD",
                "QUESTDB_TLS_ENABLED": "INFLUXDB_TLS_ENABLED",
                "QUESTDB_SSL": "INFLUXDB_TLS_ENABLED",
                "QUESTDB_VERIFY_TLS": "INFLUXDB_VERIFY_TLS",
                "QUESTDB_VERIFY_SSL": "INFLUXDB_VERIFY_TLS",
                "QUESTDB_MEASUREMENT_NAME": "INFLUXDB_MEASUREMENT_NAME",
                "QUESTDB_DATA_RETENTION_DAYS": "INFLUXDB_DATA_RETENTION_DAYS",
                "QUESTDB_DOWNSAMPLING": "INFLUXDB_DOWNSAMPLING",
                "QUESTDB_ALLOW_PLAINTEXT_CREDENTIALS": "INFLUXDB_ALLOW_PLAINTEXT_CREDENTIALS",
            }
            for q_var, i_var in questdb_mapping.items():
                if q_var in os.environ:
                    env_overrides[i_var] = os.environ[q_var]

        with _temporary_env(env_overrides):
            super().parse_config(config_data)

            if isinstance(self.version, str):
                version_str = self.version.strip().lower()
                if version_str in {"1", "2"}:
                    self.version = int(version_str)
                else:
                    self.version = version_str

            db_label = "QuestDB" if self.version == "questdb" else "InfluxDB"

            has_port_config = (
                (config_data.has_section(self.config_section_name) and config_data.has_option(self.config_section_name, "port")) or
                bool(os.environ.get("INFLUXDB_PORT", "").strip())
            )
            if self.version == "questdb" and not has_port_config:
                self.port = 9000

            # accept a full URL as hostname (e.g. behind a reverse proxy) and
            # derive TLS and port from scheme/URL instead of separate settings
            hostname_str = str(self.hostname or "").strip()
            tls_set_by_url = False
            if "://" in hostname_str:
                parsed = urlsplit(hostname_str)
                url_path = parsed.path.strip("/")
                if url_path or parsed.query or parsed.fragment:
                    # a sub-path is never applied to the /write and /exec endpoints,
                    # so accepting it would silently write to the wrong URL
                    log.error(
                        "%s hostname URL must not contain a path, query or fragment: '%s'",
                        db_label, hostname_str,
                    )
                    self.parser_error = True
                elif parsed.scheme in {"http", "https"} and parsed.hostname:
                    self.tls_enabled = parsed.scheme == "https"
                    tls_set_by_url = True
                    self.hostname = parsed.hostname
                    if parsed.port is not None:
                        self.port = parsed.port
                    elif not has_port_config:
                        self.port = 443 if self.tls_enabled else 80
                    log.info(
                        "Hostname given as URL: using %s to '%s' port %s",
                        "HTTPS" if self.tls_enabled else "HTTP", self.hostname, self.port,
                    )
                else:
                    log.error("Invalid %s hostname URL '%s'", db_label, hostname_str)
                    self.parser_error = True

            # port 443 without an explicit scheme implies TLS (e.g. a plain hostname with a reverse proxy)
            if self.port == 443 and not self.tls_enabled and not tls_set_by_url:
                self.tls_enabled = True
                log.info("Port 443 implies TLS; enabling HTTPS for '%s'", self.hostname)

            if self.tls_enabled and not self.verify_tls:
                log.warning(
                    "TLS certificate verification is disabled for %s at %s; use only on trusted networks",
                    db_label, self.hostname,
                )

            if self.data_retention_days < 0:
                log.error("Data retention days must be 0 (disabled) or greater, got %s", self.data_retention_days)
                self.parser_error = True

            self.downsampling = str(self.downsampling or "").strip().lower()
            if self.version != "questdb":
                self.downsampling = ""
            elif self.downsampling not in {"", *QUESTDB_DOWNSAMPLING_PROFILES}:
                log.error(
                    "Invalid QuestDB downsampling profile '%s'. Use low, medium or high.",
                    self.downsampling,
                )
                self.parser_error = True

            if not (1 <= self.port <= 65535):
                log.error("%s port must be between 1 and 65535, got %s", db_label, self.port)
                self.parser_error = True

            # an empty hostname is already reported by the mandatory key check
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
                    log.error(
                        "%s credentials must not be sent over plain HTTP to '%s'. "
                        "Enable TLS or set %s_ALLOW_PLAINTEXT_CREDENTIALS=true for trusted networks.",
                        db_label, self.hostname, db_label.upper(),
                    )
                    self.parser_error = True

            # validate data
            mandatory_keys = []
            if self.version == 1:
                mandatory_keys = ["hostname", "database"]

                username_defined = bool(str(self.username or "").strip())
                password_defined = bool(str(self.password or "").strip())
                if username_defined != password_defined:
                    log.error(
                        "Username and password must be defined together or not at all for InfluxDB '%s'",
                        self.version,
                    )
                    self.parser_error = True

            elif self.version == 2:
                mandatory_keys = ["hostname", "token", "organization", "bucket"]
            elif self.version == "questdb":
                mandatory_keys = ["hostname"]

                username_defined = bool(str(self.username or "").strip())
                password_defined = bool(str(self.password or "").strip())
                if username_defined != password_defined:
                    log.error("QuestDB username and password must be defined together or not at all")
                    self.parser_error = True

                if os.environ.get("QUESTDB_TOKEN", "").strip():
                    log.error("QUESTDB_TOKEN is unsupported by QuestDB Open Source; use HTTP basic authentication")
                    self.parser_error = True
            else:
                log.error(f"Invalid database version/type '{self.version}'.")
                self.parser_error = True

            for key in mandatory_keys:
                if getattr(self, key) is None or len(getattr(self, key)) == 0:
                    self.parser_error = True
                    log.error("%s %s not defined", db_label, key)
