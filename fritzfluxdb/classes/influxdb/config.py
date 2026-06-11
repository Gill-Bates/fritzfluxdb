#!/usr/bin/env python3
#
# fritzfluxdb/classes/influxdb/config.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import os
import configparser
from contextlib import contextmanager
from collections.abc import Iterator
from urllib.parse import urlsplit

from fritzfluxdb.log import get_logger
from fritzfluxdb.classes.common import ConfigBase

log = get_logger()


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

    version = {
        "type": str,
        "default": "1"
    }
    hostname = {
        "type": str,
        "alt": "host",
        "default": None
    }
    port = {
        "type": int,
        "default": 8086
    }
    tls_enabled = {
        "type": bool,
        "alt": "ssl",
        "default": False
    }
    verify_tls = {
        "type": bool,
        "alt": "verify_ssl",
        "default": True
    }
    allow_plaintext_credentials = {
        "type": bool,
        "default": False
    }
    measurement_name = {
        "type": str,
        "default": "fritzbox"
    }
    data_retention_days = {
        "type": int,
        "default": 365
    }

    # version 1 parameters
    username = {
        "type": str,
        "default": None
    }
    password = {
        "type": str,
        "default": None
    }
    database = {
        "type": str,
        "default": None
    }

    # version 2 parameters
    token = {
        "type": str,
        "default": None
    }
    organization = {
        "type": str,
        "default": None
    }
    bucket = {
        "type": str,
        "default": None
    }

    config_section_name = "influxdb"

    def parse_config(self, config_data: configparser.ConfigParser):

        # Check if database type is questdb (via DB_TYPE env var, version, or QUESTDB_HOSTNAME)
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
        is_questdb = (
            mapped_version == "questdb" or
            any(os.environ.get(var, "").strip() for var in _questdb_host_vars)
        )

        if is_questdb:
            mapped_version = "questdb"

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
                "QUESTDB_TOKEN": "INFLUXDB_TOKEN",
                "QUESTDB_TLS_ENABLED": "INFLUXDB_TLS_ENABLED",
                "QUESTDB_SSL": "INFLUXDB_TLS_ENABLED",
                "QUESTDB_VERIFY_TLS": "INFLUXDB_VERIFY_TLS",
                "QUESTDB_VERIFY_SSL": "INFLUXDB_VERIFY_TLS",
                "QUESTDB_MEASUREMENT_NAME": "INFLUXDB_MEASUREMENT_NAME",
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
                if parsed.scheme in {"http", "https"} and parsed.hostname:
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
                    log.error("Invalid %s hostname URL '%s'",
                              "QuestDB" if self.version == "questdb" else "InfluxDB", hostname_str)
                    self.parser_error = True

            # port 443 without an explicit scheme implies TLS (e.g. a plain hostname with a reverse proxy)
            if self.port == 443 and not self.tls_enabled and not tls_set_by_url:
                self.tls_enabled = True
                log.info("Port 443 implies TLS; enabling HTTPS for '%s'", self.hostname)

            if self.tls_enabled and not self.verify_tls:
                log.warning(f"TLS certificate verification is disabled for {self.version if self.version == 'questdb' else 'InfluxDB'} at {self.hostname}; use only on trusted networks")

            if not (1 <= self.port <= 65535):
                log.error("%s port must be between 1 and 65535, got %s", "QuestDB" if self.version == "questdb" else "InfluxDB", self.port)
                self.parser_error = True

            if not self.tls_enabled and any(bool(v) for v in (self.username, self.password, self.token)):
                # an empty hostname is already reported by the mandatory key check
                if self.hostname and self.hostname not in {"localhost", "127.0.0.1", "::1"}:
                    if self.allow_plaintext_credentials:
                        log.warning(
                            "%s credentials are sent over plain HTTP to '%s'; use only on trusted networks",
                            "QuestDB" if self.version == "questdb" else "InfluxDB",
                            self.hostname,
                        )
                    else:
                        log.error(
                            "%s credentials must not be sent over plain HTTP to '%s'. "
                            "Enable TLS or set %s_ALLOW_PLAINTEXT_CREDENTIALS=true for trusted networks.",
                            "QuestDB" if self.version == "questdb" else "InfluxDB",
                            self.hostname,
                            "QUESTDB" if self.version == "questdb" else "INFLUXDB",
                        )
                        self.parser_error = True

            # validate data
            mandatory_keys = list()
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
            else:
                log.error(f"Invalid database version/type '{self.version}'.")
                self.parser_error = True

            for key in mandatory_keys:
                if getattr(self, key) is None or len(getattr(self, key)) == 0:
                    self.parser_error = True
                    log.error(f"{'QuestDB' if self.version == 'questdb' else 'InfluxDB'} {key} not defined")
