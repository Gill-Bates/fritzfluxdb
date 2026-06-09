#!/usr/bin/env python3
#
# fritzfluxdb/classes/fritzbox/service_definitions/logs.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from datetime import datetime
from zoneinfo import ZoneInfo

from fritzfluxdb.classes.fritzbox.service_definitions import lua_services

LOCAL_TZ = ZoneInfo("Europe/Berlin")


def parse_legacy_log_timestamp(data) -> datetime:
    if not isinstance(data, list) or len(data) < 2:
        raise ValueError(f"invalid legacy log entry: {data!r}")
    return datetime.strptime(f"{data[0]} {data[1]}", "%d.%m.%y %H:%M:%S").replace(tzinfo=LOCAL_TZ)


def parse_modern_log_timestamp(data) -> datetime:
    date_value = data.get("date") if isinstance(data, dict) else None
    time_value = data.get("time") if isinstance(data, dict) else None
    if not date_value or not time_value:
        raise ValueError(f"invalid log entry timestamp: {data!r}")
    return datetime.strptime(f"{date_value} {time_value}", "%d.%m.%y %H:%M:%S").replace(tzinfo=LOCAL_TZ)


def prepare_json_response_data(response):
    """
    handler to prepare returned json data for parsing
    """

    if response.status_code != 200:
        raise ValueError(f"unexpected HTTP status {response.status_code} for {response.url}")

    try:
        return response.json()
    except ValueError as exc:
        raise ValueError(f"invalid JSON response for {response.url}: {exc}") from exc


lua_services.append(
    {
        "name": "System logs",
        "os_min_versions": "7.29",
        "os_max_versions": "7.38",
        "method": "POST",
        "params": {
            "filter": 1,
            "page": "log",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "track": True,
        "interval": 60,
        "value_instances": {
            "log_entry": {
                "data_path": "data.log",
                "type": list,
                "next": {
                    # data struct type: list
                    "type": str,
                    "tags": {
                        "log_type": "System"
                    },
                    "timestamp_function": parse_legacy_log_timestamp,
                    "value_function": lambda data: data[2],
                    "tags_function": None
                }
            }
        }
    })

lua_services.append(
    {
        "name": "System logs",
        "os_min_versions": "7.39",
        "method": "POST",
        "params": {
            "filter": "sys",
            "page": "log",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "track": True,
        "interval": 60,
        "value_instances": {
            "log_entry": {
                "data_path": "data.log",
                "type": list,
                "next": {
                    # data struct type: list
                    "type": str,
                    "tags": {
                        "log_type": "System"
                    },
                    "timestamp_function": parse_modern_log_timestamp,
                    "value_function": lambda data: data.get("msg"),
                    "tags_function": None
                }
            }
        }
    })

lua_services.append(
    {
        "name": "Internet connection logs",
        "os_min_versions": "7.29",
        "os_max_versions": "7.38",
        "method": "POST",
        "params": {
            "filter": 2,
            "page": "log",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "track": True,
        "interval": 61,
        "value_instances": {
            "log_entry": {
                "data_path": "data.log",
                "type": list,
                "next": {
                    # data struct type: list
                    "type": str,
                    "tags": {
                        "log_type": "Internet connection"
                    },
                    "timestamp_function": parse_legacy_log_timestamp,
                    "value_function": lambda data: data[2],
                    "tags_function": None
                }
            }
        }
    })

lua_services.append(
    {
        "name": "Internet connection logs",
        "os_min_versions": "7.39",
        "method": "POST",
        "params": {
            "filter": "net",
            "page": "log",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "track": True,
        "interval": 61,
        "value_instances": {
            "log_entry": {
                "data_path": "data.log",
                "type": list,
                "next": {
                    # data struct type: list
                    "type": str,
                    "tags": {
                        "log_type": "Internet connection"
                    },
                    "timestamp_function": parse_modern_log_timestamp,
                    "value_function": lambda data: data.get("msg"),
                    "tags_function": None
                }
            }
        }
    })

lua_services.append(
    {
        "name": "Telephony logs",
        "os_min_versions": "7.29",
        "os_max_versions": "7.38",
        "method": "POST",
        "params": {
            "filter": 3,
            "page": "log",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "track": True,
        "interval": 62,
        "value_instances": {
            "log_entry": {
                "data_path": "data.log",
                "type": list,
                "next": {
                    # data struct type: list
                    "type": str,
                    "tags": {
                        "log_type": "Telephony"
                    },
                    "timestamp_function": parse_legacy_log_timestamp,
                    "value_function": lambda data: data[2],
                    "tags_function": None
                }
            }
        }
    })

lua_services.append(
    {
        "name": "Telephony logs",
        "os_min_versions": "7.39",
        "method": "POST",
        "params": {
            "filter": "fon",
            "page": "log",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "track": True,
        "interval": 62,
        "value_instances": {
            "log_entry": {
                "data_path": "data.log",
                "type": list,
                "next": {
                    # data struct type: list
                    "type": str,
                    "tags": {
                        "log_type": "Telephony"
                    },
                    "timestamp_function": parse_modern_log_timestamp,
                    "value_function": lambda data: data.get("msg"),
                    "tags_function": None
                }
            }
        }
    })

lua_services.append(
    {
        "name": "WLAN logs",
        "os_min_versions": "7.29",
        "os_max_versions": "7.38",
        "method": "POST",
        "params": {
            "filter": 4,
            "page": "log",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "track": True,
        "interval": 63,
        "value_instances": {
            "log_entry": {
                "data_path": "data.log",
                "type": list,
                "next": {
                    # data struct type: list
                    "type": str,
                    "tags": {
                        "log_type": "WLAN"
                    },
                    "timestamp_function": parse_legacy_log_timestamp,
                    "value_function": lambda data: data[2],
                    "tags_function": None
                }
            }
        }
    })

lua_services.append(
    {
        "name": "WLAN logs",
        "os_min_versions": "7.39",
        "method": "POST",
        "params": {
            "filter": "wlan",
            "page": "log",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "track": True,
        "interval": 63,
        "value_instances": {
            "log_entry": {
                "data_path": "data.log",
                "type": list,
                "next": {
                    # data struct type: list
                    "type": str,
                    "tags": {
                        "log_type": "WLAN"
                    },
                    "timestamp_function": parse_modern_log_timestamp,
                    "value_function": lambda data: data.get("msg"),
                    "tags_function": None
                }
            }
        }
    })

lua_services.append(
    {
        "name": "USB Devices logs",
        "os_min_versions": "7.29",
        "os_max_versions": "7.38",
        "method": "POST",
        "params": {
            "filter": 5,
            "page": "log",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "track": True,
        "interval": 64,
        "value_instances": {
            "log_entry": {
                "data_path": "data.log",
                "type": list,
                "next": {
                    # data struct type: list
                    "type": str,
                    "tags": {
                        "log_type": "USB Devices"
                    },
                    "timestamp_function": parse_legacy_log_timestamp,
                    "value_function": lambda data: data[2],
                    "tags_function": None
                }
            }
        }
    })

lua_services.append(
    {
        "name": "USB Devices logs",
        "os_min_versions": "7.39",
        "method": "POST",
        "params": {
            "filter": "usb",
            "page": "log",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "track": True,
        "interval": 64,
        "value_instances": {
            "log_entry": {
                "data_path": "data.log",
                "type": list,
                "next": {
                    # data struct type: list
                    "type": str,
                    "tags": {
                        "log_type": "USB Devices"
                    },
                    "timestamp_function": parse_modern_log_timestamp,
                    "value_function": lambda data: data.get("msg"),
                    "tags_function": None
                }
            }
        }
    })
