#!/usr/bin/env python3
#
# app/fritzbox/service_definitions/logs.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import hashlib
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.fritzbox.service_definitions import lua_services
from app.fritzbox.service_definitions.helpers import (
    parse_required_json_response as prepare_json_response_data,
)


def _parse_log_datetime(date_value, time_value) -> datetime:
    # Fritz!Box log entries carry no zone; the handler attaches config.timezone later
    return datetime.strptime(f"{date_value} {time_value}", "%d.%m.%y %H:%M:%S")


def parse_legacy_log_timestamp(data) -> datetime:
    if not isinstance(data, list) or len(data) < 3:
        raise ValueError("invalid legacy log entry")
    return _parse_log_datetime(data[0], data[1])


def parse_legacy_log_message(data) -> str:
    if not isinstance(data, list) or len(data) < 3:
        raise ValueError("invalid legacy log entry")
    return str(data[2])


def parse_modern_log_timestamp(data) -> datetime:
    date_value = data.get("date") if isinstance(data, dict) else None
    time_value = data.get("time") if isinstance(data, dict) else None
    if not date_value or not time_value:
        raise ValueError("invalid log entry timestamp")
    return _parse_log_datetime(date_value, time_value)


def parse_modern_log_message(data) -> str:
    if not isinstance(data, dict) or data.get("msg") is None:
        raise ValueError("invalid log entry message")
    return str(data["msg"])


def _log_uid_function(value_function: Callable[[Any], str]) -> Callable[[Any], dict[str, str]]:
    # Two different messages can share one second; the message hash keeps their series apart
    # while a repeated poll of the same entry produces the identical point (database dedup).
    def tags(data: Any) -> dict[str, str]:
        digest = hashlib.md5(value_function(data).encode("UTF-8"), usedforsecurity=False)
        return {"uid": digest.hexdigest()}

    return tags


def _build_log_service(
    name: str,
    log_type: str,
    filter_value: int | str,
    interval: int,
    os_min_versions: str,
    os_max_versions: str | None,
    timestamp_function: Callable[[Any], datetime],
    value_function: Callable[[Any], str],
) -> dict:
    service: dict = {
        "name": name,
        "os_min_versions": os_min_versions,
        "method": "POST",
        "params": {
            "filter": filter_value,
            "page": "log",
            "lang": "de",
        },
        "response_parser": prepare_json_response_data,
        "event": True,
        "interval": interval,
        "value_instances": {
            "log_entry": {
                "data_path": "data.log",
                "type": list,
                "next": {
                    "type": str,
                    "tags": {"log_type": log_type},
                    "timestamp_function": timestamp_function,
                    "value_function": value_function,
                    "tags_function": _log_uid_function(value_function),
                },
            },
        },
    }
    if os_max_versions is not None:
        service["os_max_versions"] = os_max_versions
    return service


# (name, log_type, legacy_filter, modern_filter, interval)
_LOG_DEFINITIONS: list[tuple[str, str, int, str, int]] = [
    ("System logs",              "System",             1, "sys",  60),
    ("Internet connection logs", "Internet connection", 2, "net",  61),
    ("Telephony logs",           "Telephony",           3, "fon",  62),
    ("WLAN logs",                "WLAN",                4, "wlan", 63),
    ("USB Devices logs",         "USB Devices",         5, "usb",  64),
]

for _name, _log_type, _legacy_filter, _modern_filter, _interval in _LOG_DEFINITIONS:
    lua_services.append(_build_log_service(
        name=_name,
        log_type=_log_type,
        filter_value=_legacy_filter,
        interval=_interval,
        os_min_versions="7.29",
        os_max_versions="7.38",
        timestamp_function=parse_legacy_log_timestamp,
        value_function=parse_legacy_log_message,
    ))
    lua_services.append(_build_log_service(
        name=_name,
        log_type=_log_type,
        filter_value=_modern_filter,
        interval=_interval,
        os_min_versions="7.39",
        os_max_versions=None,
        timestamp_function=parse_modern_log_timestamp,
        value_function=parse_modern_log_message,
    ))
