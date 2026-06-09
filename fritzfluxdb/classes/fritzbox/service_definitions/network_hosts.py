#!/usr/bin/env python3
#
# fritzfluxdb/classes/fritzbox/service_definitions/network_hosts.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import re

from fritzfluxdb.classes.fritzbox.service_definitions import lua_services

# precompile active_host_txt_regex
#  used this neat tool: https://regex101.com/r/ut4KdU/1
# Needs to match following strings:
#   166 / 150 Mbit/s
#   2,4 GHz, 50 / 836 Mbit/s
#   5 GHz, 50 / 836 Mbit/s
#   2,4 GHz
#   5 GHz
active_host_txt_regex = re.compile(
    r"^(?:(?P<frequency>[0-9,]+) GHz(?:, )?)?"
    r"(?:(?P<downstream>\d+) / (?P<upstream>\d+) .*bit.*)?$"
)


def nested_value(data: dict, key: str, child_key: str, fallback=None):
    value = data.get(key)
    if not isinstance(value, dict):
        return fallback
    return value.get(child_key, fallback)


def get_active_host_details(data, desired_value: str, fallback_value):

    property_list = data.get("properties")

    if not isinstance(property_list, list):
        property_list = list()

    txt_list = [
        item.get("txt")
        for item in property_list
        if isinstance(item, dict) and isinstance(item.get("txt"), str)
    ]

    if desired_value == "additional_text":
        return ", ".join(txt_list)

    if desired_value == "is_mesh":
        return "Mesh" in txt_list

    regex_matches = active_host_txt_regex.fullmatch(
        next((x for x in txt_list if "GHz" in x or "bit" in x), "")
    )

    if regex_matches is None:
        return fallback_value

    value = regex_matches.groupdict(fallback_value).get(desired_value, fallback_value)
    if desired_value in {"downstream", "upstream"}:
        return int(value) if str(value).isdigit() else fallback_value

    return value


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


# every 2 minutes
lua_services.append(
    {
        "name": "Active network hosts",
        "os_min_versions": "7.29",
        "method": "POST",
        "params": {
            "page": "netDev",
            "useajax": 1,
            "xhrId": "all",
            "xhr": 1,
            "initial": True
        },
        "response_parser": prepare_json_response_data,
        "interval": 120,
        "value_instances": {
            "active_hosts_name": {
                "data_path": "data.active",
                "type": list,
                "next": {
                    # data struct type: dict
                    "type": str,
                    "tags_function": lambda data: {"uid": data.get("UID")},
                    "value_function": lambda data: data.get("name")
                }
            },
            "active_hosts_mac": {
                "data_path": "data.active",
                "type": list,
                "next": {
                    # data struct type: dict
                    "type": str,
                    "tags_function": lambda data: {"uid": data.get("UID")},
                    "value_function": lambda data: data.get("mac")
                }
            },
            "active_hosts_type": {
                "data_path": "data.active",
                "type": list,
                "next": {
                    # data struct type: dict
                    "type": str,
                    "tags_function": lambda data: {"uid": data.get("UID")},
                    "value_function": lambda data: data.get("type")
                }
            },
            "active_hosts_parent": {
                "data_path": "data.active",
                "type": list,
                "next": {
                    # data struct type: dict
                    "type": str,
                    "tags_function": lambda data: {"uid": data.get("UID")},
                    "value_function": lambda data: nested_value(data, "parent", "name")
                }
            },
            "active_hosts_port": {
                "data_path": "data.active",
                "type": list,
                "next": {
                    # data struct type: dict
                    "type": str,
                    "tags_function": lambda data: {"uid": data.get("UID")},
                    "value_function": lambda data: data.get("port")
                }
            },
            "active_hosts_ipv4": {
                "data_path": "data.active",
                "type": list,
                "next": {
                    # data struct type: dict
                    "type": str,
                    "tags_function": lambda data: {"uid": data.get("UID")},
                    "value_function": lambda data: nested_value(data, "ipv4", "ip")
                }
            },
            "active_hosts_ipv4_last_used": {
                "data_path": "data.active",
                "type": list,
                "next": {
                    # data struct type: dict
                    "type": int,
                    "tags_function": lambda data: {"uid": data.get("UID"), "name": data.get("name")},
                    "value_function": lambda data: nested_value(data, "ipv4", "lastused", 0)
                }
            },
            "active_hosts_additional_text": {
                "data_path": "data.active",
                "type": list,
                "next": {
                    # data struct type: dict
                    "type": str,
                    "tags_function": lambda data: {"uid": data.get("UID"), "name": data.get("name")},
                    "value_function": lambda data: get_active_host_details(data, "additional_text", "")
                }
            },
            "active_hosts_is_mesh": {
                "data_path": "data.active",
                "type": list,
                "next": {
                    # data struct type: dict
                    "type": bool,
                    "tags_function": lambda data: {"uid": data.get("UID"), "name": data.get("name")},
                    "value_function": lambda data: get_active_host_details(data, "is_mesh", False)
                }
            },
            "active_hosts_frequency": {
                "data_path": "data.active",
                "type": list,
                "next": {
                    # data struct type: dict
                    "type": str,
                    "tags_function": lambda data: {"uid": data.get("UID"), "name": data.get("name")},
                    "value_function": lambda data: get_active_host_details(data, "frequency", "")
                }
            },
            "active_hosts_downstream": {
                "data_path": "data.active",
                "type": list,
                "next": {
                    # data struct type: dict
                    "type": int,
                    "tags_function": lambda data: {"uid": data.get("UID"), "name": data.get("name")},
                    "value_function": lambda data: get_active_host_details(data, "downstream", 0)
                }
            },
            "active_hosts_upstream": {
                "data_path": "data.active",
                "type": list,
                "next": {
                    # data struct type: dict
                    "type": int,
                    "tags_function": lambda data: {"uid": data.get("UID"), "name": data.get("name")},
                    "value_function": lambda data: get_active_host_details(data, "upstream", 0)
                }
            },
            "num_active_host": {
                "type": int,
                "value_function": lambda data: len(data.get("data", {}).get("active", []))
            }
        }
    }
)

# every 10 minutes
lua_services.append({
        "name": "Passive network hosts",
        "os_min_versions": "7.29",
        "method": "POST",
        "params": {
            "page": "netDev",
            "useajax": 1,
            "xhrId": "cleanup",
            "xhr": 1,
        },
        "response_parser": prepare_json_response_data,
        "interval": 600,
        "value_instances": {
            "passive_hosts_name": {
                "data_path": "data.passive",
                "type": list,
                "next": {
                    # data struct type: dict
                    "type": str,
                    "tags_function": lambda data: {"uid": data.get("UID")},
                    "value_function": lambda data: data.get("name")
                }
            },
            "passive_hosts_mac": {
                "data_path": "data.passive",
                "type": list,
                "next": {
                    # data struct type: dict
                    "type": str,
                    "tags_function": lambda data: {"uid": data.get("UID")},
                    "value_function": lambda data: data.get("mac")
                }
            },
            "passive_hosts_port": {
                "data_path": "data.passive",
                "type": list,
                "next": {
                    # data struct type: dict
                    "type": str,
                    "tags_function": lambda data: {"uid": data.get("UID")},
                    "value_function": lambda data: data.get("port")
                }
            },
            "passive_hosts_ipv4": {
                "data_path": "data.passive",
                "type": list,
                "next": {
                    # data struct type: dict
                    "type": str,
                    "tags_function": lambda data: {"uid": data.get("UID")},
                    "value_function": lambda data: nested_value(data, "ipv4", "ip")
                }
            },
            "num_passive_host": {
                "type": int,
                "value_function": lambda data: len(data.get("data", {}).get("passive", [])),
            }
        }
    }
)
