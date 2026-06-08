#!/usr/bin/env python3
#
# fritzfluxdb/classes/fritzbox/service_definitions/vpn_data.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from fritzfluxdb.common import grab
from fritzfluxdb.classes.fritzbox.service_definitions import lua_services

INCLUDE_VPN_ADDRESS_METRICS = False

def prepare_json_response_data(response):
    if response.status_code != 200:
        return None

    try:
        return response.json()
    except ValueError as exc:
        raise ValueError(f"invalid JSON response for {response.url}: {exc}") from exc

def has_dict_at(path: str):
    return lambda data: not isinstance(grab(data, path), dict)

def parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "t", "1", "yes", "on"}:
            return True
        if normalized in {"false", "f", "0", "no", "off"}:
            return False
    return False

def count_connected(data, path: str) -> int:
    connections = grab(data, path, fallback={})
    if not isinstance(connections, dict):
        return 0

    return sum(
        1
        for connection in connections.values()
        if isinstance(connection, dict) and parse_bool(connection.get("connected"))
    )

def vpn_user_tags(vpn_type: str):
    def build_tags(data) -> dict[str, str]:
        name = data.get("name")
        if name is None or str(name).strip() == "":
            raise ValueError("missing VPN user name")
        return {"name": str(name), "vpn_type": vpn_type}
    return build_tags

def vpn_connection_metric(path: str, source_key: str, value_type: type, vpn_type: str) -> dict:
    return {
        "data_path": path,
        "type": dict,
        "next": {
            "type": value_type,
            "value_function": lambda data: data.get(source_key),
            "tags_function": vpn_user_tags(vpn_type),
        },
        "exclude_filter_function": has_dict_at(path),
    }

def add_vpn_address_metrics(
    value_instances: dict,
    *,
    connection_path: str,
    vpn_type: str,
    virtual_key: str,
    remote_key: str,
) -> None:
    if not INCLUDE_VPN_ADDRESS_METRICS:
        return

    value_instances["vpn_user_virtual_address"] = vpn_connection_metric(
        connection_path, virtual_key, str, vpn_type
    )
    value_instances["vpn_user_remote_address"] = vpn_connection_metric(
        connection_path, remote_key, str, vpn_type
    )

_VPN_SERVICES = []

# VPN Users 7.29-7.38
vi_729 = {
    "vpn_user_connected": vpn_connection_metric("data.vpnInfo.userConnections", "connected", bool, "IPSec"),
    "vpn_user_active": vpn_connection_metric("data.vpnInfo.userConnections", "active", bool, "IPSec"),
    "vpn_user_num_active": {
        "type": int,
        "value_function": lambda data: count_connected(data, "data.vpnInfo.userConnections"),
        "tags": {"vpn_type": "IPSec"},
        "exclude_filter_function": has_dict_at("data.vpnInfo.userConnections"),
    },
}
add_vpn_address_metrics(
    vi_729,
    connection_path="data.vpnInfo.userConnections",
    vpn_type="IPSec",
    virtual_key="virtualAddress",
    remote_key="remoteAddress"
)

_VPN_SERVICES.append({
    "name": "VPN Users",
    "os_min_versions": "7.29",
    "os_max_versions": "7.38",
    "method": "POST",
    "params": {
        "page": "shareVpn",
        "xhrId": "all",
        "xhr": 1
    },
    "link_type": None,  # valid for any WAN link type
    "response_parser": prepare_json_response_data,
    "interval": 60,
    "value_instances": vi_729
})

# VPN Users - IPSec 7.39+
vi_739_ipsec = {
    "vpn_user_connected": vpn_connection_metric("data.init.userConnections", "connected", bool, "IPSec"),
    "vpn_user_active": vpn_connection_metric("data.init.userConnections", "active", bool, "IPSec"),
    "vpn_user_num_active": {
        "type": int,
        "value_function": lambda data: count_connected(data, "data.init.userConnections"),
        "tags": {"vpn_type": "IPSec"},
        "exclude_filter_function": has_dict_at("data.init.userConnections"),
    },
}
add_vpn_address_metrics(
    vi_739_ipsec,
    connection_path="data.init.userConnections",
    vpn_type="IPSec",
    virtual_key="virtualAddress",
    remote_key="remoteAddress"
)

_VPN_SERVICES.append({
    "name": "VPN Users - IPSec",
    "os_min_versions": "7.39",
    "method": "POST",
    "params": {
        "page": "shareVpn",
        "xhrId": "all",
        "xhr": 1
    },
    "link_type": None,  # valid for any WAN link type
    "response_parser": prepare_json_response_data,
    "interval": 60,
    "value_instances": vi_739_ipsec
})

# VPN Users - WireGuard 7.39+
vi_739_wg = {
    "vpn_user_connected": vpn_connection_metric("data.init.boxConnections", "connected", bool, "WireGuard"),
    "vpn_user_active": vpn_connection_metric("data.init.boxConnections", "active", bool, "WireGuard"),
    "vpn_user_num_active": {
        "type": int,
        "value_function": lambda data: count_connected(data, "data.init.boxConnections"),
        "tags": {"vpn_type": "WireGuard"},
        "exclude_filter_function": has_dict_at("data.init.boxConnections"),
    },
}
add_vpn_address_metrics(
    vi_739_wg,
    connection_path="data.init.boxConnections",
    vpn_type="WireGuard",
    virtual_key="virtualAddress",
    remote_key="remoteAddress"
)

_VPN_SERVICES.append({
    "name": "VPN Users - WireGuard",
    "os_min_versions": "7.39",
    "method": "POST",
    "params": {
        "page": "shareWireguard",
        "xhrId": "all",
        "xhr": 1
    },
    "link_type": None,  # valid for any WAN link type
    "response_parser": prepare_json_response_data,
    "interval": 60,
    "value_instances": vi_739_wg
})

_registered = {
    (
        service.get("name"),
        service.get("os_min_versions"),
        service.get("os_max_versions"),
    )
    for service in lua_services
}

for service in _VPN_SERVICES:
    key = (
        service.get("name"),
        service.get("os_min_versions"),
        service.get("os_max_versions"),
    )
    if key not in _registered:
        lua_services.append(service)

