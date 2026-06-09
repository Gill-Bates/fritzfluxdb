#!/usr/bin/env python3
#
# fritzfluxdb/classes/fritzbox/service_definitions/system_stats.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from fritzfluxdb.classes.fritzbox.service_definitions import lua_services

read_interval = 150


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


def missing_data_key(key: str):
    def exclude(data) -> bool:
        payload = data.get("data")
        return not isinstance(payload, dict) or key not in payload
    return exclude


def is_lan_energy_entry(data: dict) -> bool:
    return "lan" in data


lua_services.append(
    {
        "name": "System Stats",
        "os_min_versions": "7.29",
        "method": "POST",
        "params": {
            "page": "ecoStat",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "interval": read_interval,
        "value_instances": {
            "cpu_temp": {
                "data_path": "data.cputemp.series.0.-1",
                "type": int,
                # Cable FritzBox with FritzOS 8.00 got these stats removed
                "exclude_filter_function": missing_data_key("cputemp")
            },
            "cpu_utilization": {
                "data_path": "data.cpuutil.series.0.-1",
                "type": int,
                # Cable FritzBox with FritzOS 8.00 got these stats removed
                "exclude_filter_function": missing_data_key("cpuutil")
            },
            "ram_usage_fixed": {
                "data_path": "data.ramusage.series.0.-1",
                "type": int,
                # Cable FritzBox with FritzOS 8.00 got these stats removed
                "exclude_filter_function": missing_data_key("ramusage")
            },
            "ram_usage_dynamic": {
                "data_path": "data.ramusage.series.1.-1",
                "type": int,
                # Cable FritzBox with FritzOS 8.00 got these stats removed
                "exclude_filter_function": missing_data_key("ramusage")
            },
            "ram_usage_free": {
                "data_path": "data.ramusage.series.2.-1",
                "type": int,
                # Cable FritzBox with FritzOS 8.00 got these stats removed
                "exclude_filter_function": missing_data_key("ramusage")
            }
        }
    })

lua_services.append(
    {
        "name": "Energy Stats",
        "os_min_versions": "7.29",
        "method": "POST",
        "params": {
            "page": "energy",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "interval": read_interval,
        "value_instances": {
            "energy_consumption": {
                "data_path": "data.drain",
                "type": list,
                "next": {
                    # data struct type: dict
                    "type": int,
                    "tags_function": lambda data: {"name": data.get("name")},
                    "value_function": lambda data: data.get("actPerc"),
                    "exclude_filter_function": is_lan_energy_entry
                },
                # Cable FritzBox with FritzOS 8.00 got these stats removed
                "exclude_filter_function": missing_data_key("drain")
            }
        }
    })
