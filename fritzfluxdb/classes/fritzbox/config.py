#!/usr/bin/env python3
#
# fritzfluxdb/classes/fritzbox/config.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import configparser
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fritzfluxdb.log import get_logger
from fritzfluxdb.classes.common import ConfigBase

log = get_logger()


class FritzBoxConfig(ConfigBase):
    """
        class which defines the FritzBox config options
    """

    hostname = {
        "type": str,
        "alt": "host",
        "default": "192.168.178.1"
    }
    username = {
        "type": str,
        "default": None
    }
    password = {
        "type": str,
        "default": None
    }
    port = {
        "type": int,
        "default": 49000
    }
    tls_enabled = {
        "type": bool,
        "alt": "ssl",
        "default": None
    }
    verify_tls = {
        "type": bool,
        "alt": "verify_ssl",
        "default": True
    }
    connect_timeout = {
        "type": int,
        "alt": "timeout",
        "default": 10
    }
    request_interval = {
        "type": int,
        "alt": "interval",
        "default": 10
    }
    box_tag = {
        "type": str,
        "default": "fritz.box"
    }
    timezone = {
        "type": str,
        "default": "Europe/Berlin"
    }

    config_section_name = "fritzbox"

    DEFAULT_TR064_HTTP_PORT = 49000
    DEFAULT_TR064_HTTPS_PORT = 49443
    MAX_CONNECT_TIMEOUT = 300
    MAX_REQUEST_INTERVAL = 3600

    def __init__(self, config_data):

        self.tls_auto = False  # set after tls_enabled has been parsed
        super().__init__(config_data)

        self._fw_version = None
        self.model = None
        self.link_type = None
        self.serial_number = None

    def parse_config(self, config_data: configparser.ConfigParser):

        self.parser_error = False

        super().parse_config(config_data)

        self._validate_hostname()

        min_request_interval = self.__class__.request_interval.get("default")
        if getattr(self, "request_interval") < min_request_interval:
            log.info(f"Setting minimum FritzBox request interval to {min_request_interval} seconds")
            self.request_interval = min_request_interval

        # validate data
        for key in ["username", "password"]:
            if getattr(self, key) is None or len(getattr(self, key)) == 0:
                self.parser_error = True
                log.error(f"FritzBox {key} not defined")

        # validate port range
        if not 1 <= self.port <= 65535:
            log.error(f"FritzBox port {self.port} is invalid, must be between 1 and 65535")
            self.parser_error = True

        # validate connect_timeout
        if self.connect_timeout < 1:
            log.error("FritzBox connect_timeout must be at least 1 second")
            self.parser_error = True

        if self.connect_timeout > self.MAX_CONNECT_TIMEOUT:
            log.error("FritzBox connect_timeout must not exceed %s seconds", self.MAX_CONNECT_TIMEOUT)
            self.parser_error = True

        if self.request_interval > self.MAX_REQUEST_INTERVAL:
            log.error("FritzBox request_interval must not exceed %s seconds", self.MAX_REQUEST_INTERVAL)
            self.parser_error = True

        # noinspection PyBroadException
        try:
            self.timezone = ZoneInfo(str(self.timezone))
        except ZoneInfoNotFoundError as e:
            log.error(f"Defined FritzBox time zone '{self.timezone}' is invalid/unknown: {e}")
            self.parser_error = True

        # record whether HTTPS was requested by the user or should be auto-detected
        self.tls_auto = (self.tls_enabled is None)

        # set TR-069 TLS port if explicitly enabled and port is still the default
        if self.tls_enabled is True and self.port == self.DEFAULT_TR064_HTTP_PORT:
            self.port = self.DEFAULT_TR064_HTTPS_PORT

        if self.tls_enabled is True and not self.verify_tls:
            log.warning(f"TLS certificate verification is disabled for {self.hostname}; use only on trusted networks")

    def _validate_hostname(self) -> None:
        hostname = str(self.hostname or "").strip()

        if not hostname:
            log.error("FritzBox hostname must not be empty")
            self.parser_error = True
            return

        parsed = urlsplit(hostname)
        if parsed.scheme or parsed.netloc or parsed.path != hostname or parsed.query or parsed.fragment:
            log.error("FritzBox hostname must be a hostname or IP address, not a URL: %r", self.hostname)
            self.parser_error = True
            return

        if any(c.isspace() for c in hostname):
            log.error("FritzBox hostname must not contain whitespace: %r", self.hostname)
            self.parser_error = True
            return

        self.hostname = hostname

    @property
    def fw_version(self):
        return self._fw_version

    @fw_version.setter
    def fw_version(self, version):
        parts = str(version or "").split(".")

        try:
            if len(parts) >= 3:
                self._fw_version = f"{int(parts[1])}.{int(parts[2])}"
            elif len(parts) == 2:
                self._fw_version = f"{int(parts[0])}.{int(parts[1])}"
            else:
                raise ValueError
        except ValueError:
            log.warning(f"Unable to parse FritzOS version: {version!r}")
            self._fw_version = None
