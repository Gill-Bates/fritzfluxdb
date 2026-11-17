#!/usr/bin/env python3
#
# app/fritzbox/config.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import os
import re
import ssl
from ipaddress import ip_address
from typing import ClassVar
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ConfigDict, Field, PrivateAttr, SecretStr, field_validator

from app.utils.log import get_logger
from app.utils.settings import ConfigModel

log = get_logger()


def _default_timezone() -> ZoneInfo:
    return ZoneInfo("Europe/Berlin")


class FritzBoxConfig(ConfigModel):
    """
        FritzBox config options, read from the environment and the INI section
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, validate_assignment=False)

    config_section_name: ClassVar[str] = "fritzbox"
    label: ClassVar[str] = "FritzBox"
    DEFAULT_PORT: ClassVar[int] = 49000
    DEFAULT_TR064_HTTPS_PORT: ClassVar[int] = 49443
    DEFAULT_REQUEST_INTERVAL: ClassVar[int] = 10
    MAX_CONNECT_TIMEOUT: ClassVar[int] = 300
    MAX_REQUEST_INTERVAL: ClassVar[int] = 3600
    MIN_WAN_WATCH_INTERVAL: ClassVar[int] = 2
    MAX_WAN_WATCH_INTERVAL: ClassVar[int] = 60

    hostname: str = "192.168.178.1"
    username: str | None = None
    password: SecretStr | None = None
    port: int = DEFAULT_PORT
    connect_timeout: int = 10
    request_interval: int = DEFAULT_REQUEST_INTERVAL
    # seconds between WAN status polls for the interruption cause; 0 disables the watcher
    wan_watch_interval: int = 5
    box_tag: str = "fritz.box"
    timezone: ZoneInfo = Field(default_factory=_default_timezone)
    # optional PEM file (CA or the box certificate) to verify the HTTPS Lua endpoint
    ca_file: str | None = None

    # runtime state, not config options: tls_enabled is None until the handlers have
    # probed HTTPS/HTTP, then True/False for the protocol that actually works
    tls_enabled: bool | None = Field(default=None, exclude=True)
    model: str | None = Field(default=None, exclude=True)
    link_type: str | None = Field(default=None, exclude=True)
    serial_number: str | None = Field(default=None, exclude=True)
    _fw_version: str | None = PrivateAttr(default=None)

    def required_options(self) -> tuple[str, ...]:
        return ("username", "password")

    @field_validator("hostname")
    @classmethod
    def _validate_hostname(cls, value: str) -> str:
        hostname = value.strip()

        if not hostname:
            raise ValueError("FritzBox hostname must not be empty")

        # an IPv6 literal (bracketed or bare) is a valid host but would trip the URL
        # check below, because urlsplit() reads its first group as a scheme
        literal = hostname[1:-1] if hostname.startswith("[") and hostname.endswith("]") else hostname
        try:
            is_ipv6_literal = ip_address(literal).version == 6
        except ValueError:
            is_ipv6_literal = False

        if is_ipv6_literal:
            return literal

        # userinfo may carry credentials, so the value is deliberately not echoed
        if "@" in hostname:
            raise ValueError("FritzBox hostname must be a hostname or IP address without user information")

        try:
            parsed = urlsplit(hostname)
        except ValueError as exc:
            raise ValueError(f"FritzBox hostname {value!r} is invalid: {exc}") from None

        if (parsed.scheme or parsed.netloc or parsed.path != hostname or parsed.query or parsed.fragment
                or any(c in hostname for c in "/?#")):
            raise ValueError(f"FritzBox hostname must be a hostname or IP address, not a URL or path: {value!r}")

        if any(c.isspace() for c in hostname):
            raise ValueError(f"FritzBox hostname must not contain whitespace: {value!r}")

        return hostname

    @field_validator("port")
    @classmethod
    def _validate_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError(f"FritzBox port {value} is invalid, must be between 1 and 65535")
        return value

    @field_validator("connect_timeout")
    @classmethod
    def _validate_connect_timeout(cls, value: int) -> int:
        if value < 1:
            raise ValueError("FritzBox connect_timeout must be at least 1 second")
        if value > cls.MAX_CONNECT_TIMEOUT:
            raise ValueError(f"FritzBox connect_timeout must not exceed {cls.MAX_CONNECT_TIMEOUT} seconds")
        return value

    @field_validator("request_interval")
    @classmethod
    def _validate_request_interval(cls, value: int) -> int:
        if value > cls.MAX_REQUEST_INTERVAL:
            raise ValueError(f"FritzBox request_interval must not exceed {cls.MAX_REQUEST_INTERVAL} seconds")
        if value < cls.DEFAULT_REQUEST_INTERVAL:
            log.info(f"Setting minimum FritzBox request interval to {cls.DEFAULT_REQUEST_INTERVAL} seconds")
            return cls.DEFAULT_REQUEST_INTERVAL
        return value

    @field_validator("wan_watch_interval")
    @classmethod
    def _validate_wan_watch_interval(cls, value: int) -> int:
        if value != 0 and not cls.MIN_WAN_WATCH_INTERVAL <= value <= cls.MAX_WAN_WATCH_INTERVAL:
            raise ValueError(
                f"FritzBox wan_watch_interval must be 0 (off) or between "
                f"{cls.MIN_WAN_WATCH_INTERVAL} and {cls.MAX_WAN_WATCH_INTERVAL} seconds"
            )
        return value

    @field_validator("timezone", mode="before")
    @classmethod
    def _validate_timezone(cls, value):
        if isinstance(value, ZoneInfo):
            return value
        try:
            # ValueError covers non-normalized keys like '../etc/localtime'
            return ZoneInfo(str(value))
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"Defined FritzBox time zone '{value}' is invalid/unknown: {e}") from None

    @field_validator("ca_file")
    @classmethod
    def _validate_ca_file(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not os.path.isfile(value):
            raise ValueError(f"FritzBox ca_file '{value}' does not exist or is not a file")
        try:
            ssl.create_default_context(cafile=value)
        except (OSError, ssl.SSLError) as exc:
            raise ValueError(f"FritzBox ca_file '{value}' is not a valid readable CA bundle: {exc}") from None
        return value

    @property
    def fw_version(self):
        return self._fw_version

    @fw_version.setter
    def fw_version(self, version):
        # AVM reports e.g. '113.07.90', '7.90' or '113.07.90-123456' (lab/build suffix),
        # so take the leading digits of each dot-separated component
        parts = []
        for chunk in str(version or "").strip().split("."):
            match = re.match(r"\d+", chunk)
            if match is None:
                break
            parts.append(match.group())

        if len(parts) >= 3:
            self._fw_version = f"{int(parts[1])}.{int(parts[2])}"
        elif len(parts) == 2:
            self._fw_version = f"{int(parts[0])}.{int(parts[1])}"
        else:
            log.warning(f"Unable to parse FritzOS version: {version!r}")
            self._fw_version = None
