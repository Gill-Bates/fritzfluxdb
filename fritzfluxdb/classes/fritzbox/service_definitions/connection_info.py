#!/usr/bin/env python3
#
# fritzfluxdb/classes/fritzbox/service_definitions/connection_info.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from fritzfluxdb.common import grab
from fritzfluxdb.classes.fritzbox.service_definitions import lua_services
from fritzfluxdb.classes.fritzbox.model import FritzBoxLinkTypes

def prepare_json_response_data(response):
    if response.status_code == 404:
        return {}

    if response.status_code != 200:
        raise ValueError(f"unexpected HTTP status {response.status_code} for {response.url}")

    try:
        return response.json()
    except ValueError as exc:
        raise ValueError(
            f"invalid JSON response for {response.url}: {exc}"
        ) from exc

def make_docsis_filter(channel_path: str, docsis_key: str):
    def exclude_when_missing(data) -> bool:
        channels = grab(data, channel_path, fallback={})
        return not isinstance(channels, dict) or docsis_key not in channels
    return exclude_when_missing

exclude_filter_ds_docsis30 = make_docsis_filter("data.channelDs", "docsis30")
exclude_filter_ds_docsis31 = make_docsis_filter("data.channelDs", "docsis31")
exclude_filter_us_docsis30 = make_docsis_filter("data.channelUs", "docsis30")
exclude_filter_us_docsis31 = make_docsis_filter("data.channelUs", "docsis31")

def count_grouped_channels(data, path: str) -> int:
    groups = grab(data, path, fallback={})
    if not isinstance(groups, dict):
        return 0
    return sum(len(group) for group in groups.values() if isinstance(group, list | tuple))

def channel_id_tag(data) -> dict[str, str]:
    channel_id = data.get("channelID") or data.get("channel") or data.get("frequency")
    if channel_id is None or str(channel_id).strip() == "":
        return {"id": "unknown"}
    return {"id": str(channel_id)}

def channel_metric(data_path: str, value_key: str, value_type: type, exclude_filter) -> dict:
    return {
        "data_path": data_path,
        "type": list,
        "next": {
            "type": value_type,
            "value_function": lambda data: data.get(value_key),
            "tags_function": channel_id_tag,
        },
        "exclude_filter_function": exclude_filter,
    }

_CONNECTION_INFO_SERVICES = [
    {
        "name": "DSL Info",
        "os_min_versions": "7.29",
        "method": "POST",
        "params": {
            "page": "dslOv",
            "xhrId": "all",
            "xhr": 1,
            "useajax": 1
        },
        "link_type": FritzBoxLinkTypes.DSL,
        "response_parser": prepare_json_response_data,
        "interval": 600,
        "value_instances": {
            "dsl_line_length": {"data_path": "data.connectionData.lineLength", "type": int},
            "dsl_dslam_vendor": {"data_path": "data.connectionData.dslamId", "type": str},
            "dsl_dslam_sw_version": {"data_path": "data.connectionData.version", "type": str},
            "dsl_line_mode": {"data_path": "data.connectionData.line.0.mode", "type": str}
        }
    },
    {
        "name": "Cable Info",
        "os_min_versions": "7.29",
        "method": "POST",
        "params": {
            "page": "docOv",
            "xhrId": "all",
            "xhr": 1
        },
        "link_type": FritzBoxLinkTypes.Cable,
        "response_parser": prepare_json_response_data,
        "interval": 600,
        "value_instances": {
            "cable_cmts_vendor": {"data_path": "data.connectionData.externApValue", "type": str},
            "cable_modem_version": {"data_path": "data.connectionData.version", "type": str},
            "cable_line_mode": {"data_path": "data.connectionData.line.0.mode", "type": str},
            "cable_num_ds_channels": {
                "type": int,
                "value_function": lambda data: count_grouped_channels(data, "data.connectionData.dsFreqs.values"),
            },
            "cable_num_us_channels": {
                "type": int,
                "value_function": lambda data: count_grouped_channels(data, "data.connectionData.usFreqs.values"),
            }
        }
    },
    {
        "name": "Cable Channel Info",
        "os_min_versions": "7.29",
        "os_max_versions": "7.57",
        "method": "POST",
        "params": {
            "page": "docInfo",
            "xhrId": "all",
            "xhr": 1
        },
        "link_type": FritzBoxLinkTypes.Cable,
        "response_parser": prepare_json_response_data,
        "interval": 600,
        "value_instances": {
            "cable_channel_ds_docsis31_type": channel_metric("data.channelDs.docsis31", "type", str, exclude_filter_ds_docsis31),
            "cable_channel_ds_docsis31_power_level": channel_metric("data.channelDs.docsis31", "powerLevel", str, exclude_filter_ds_docsis31),
            "cable_channel_ds_docsis31_channel": channel_metric("data.channelDs.docsis31", "channel", int, exclude_filter_ds_docsis31),
            "cable_channel_ds_docsis31_frequency": channel_metric("data.channelDs.docsis31", "frequency", str, exclude_filter_ds_docsis31),
            
            "cable_channel_ds_docsis30_type": channel_metric("data.channelDs.docsis30", "type", str, exclude_filter_ds_docsis30),
            "cable_channel_ds_docsis30_power_level": channel_metric("data.channelDs.docsis30", "powerLevel", str, exclude_filter_ds_docsis30),
            "cable_channel_ds_docsis30_channel": channel_metric("data.channelDs.docsis30", "channel", int, exclude_filter_ds_docsis30),
            "cable_channel_ds_docsis30_frequency": channel_metric("data.channelDs.docsis30", "frequency", str, exclude_filter_ds_docsis30),
            "cable_channel_ds_docsis30_latency": channel_metric("data.channelDs.docsis30", "latency", float, exclude_filter_ds_docsis30),
            "cable_channel_ds_docsis30_mse": channel_metric("data.channelDs.docsis30", "mse", float, exclude_filter_ds_docsis30),
            "cable_channel_ds_docsis30_corrected_errors": channel_metric("data.channelDs.docsis30", "corrErrors", int, exclude_filter_ds_docsis30),
            "cable_channel_ds_docsis30_non_corrected_errors": channel_metric("data.channelDs.docsis30", "nonCorrErrors", int, exclude_filter_ds_docsis30),
            
            "cable_channel_us_docsis31_type": channel_metric("data.channelUs.docsis31", "type", str, exclude_filter_us_docsis31),
            "cable_channel_us_docsis31_power_level": channel_metric("data.channelUs.docsis31", "powerLevel", str, exclude_filter_us_docsis31),
            "cable_channel_us_docsis31_channel": channel_metric("data.channelUs.docsis31", "channel", int, exclude_filter_us_docsis31),
            "cable_channel_us_docsis31_frequency": channel_metric("data.channelUs.docsis31", "frequency", str, exclude_filter_us_docsis31),
            "cable_channel_us_docsis31_multiplex": channel_metric("data.channelUs.docsis31", "multiplex", str, exclude_filter_us_docsis31),
            
            "cable_channel_us_docsis30_type": channel_metric("data.channelUs.docsis30", "type", str, exclude_filter_us_docsis30),
            "cable_channel_us_docsis30_power_level": channel_metric("data.channelUs.docsis30", "powerLevel", str, exclude_filter_us_docsis30),
            "cable_channel_us_docsis30_channel": channel_metric("data.channelUs.docsis30", "channel", int, exclude_filter_us_docsis30),
            "cable_channel_us_docsis30_frequency": channel_metric("data.channelUs.docsis30", "frequency", str, exclude_filter_us_docsis30),
            "cable_channel_us_docsis30_multiplex": channel_metric("data.channelUs.docsis30", "multiplex", str, exclude_filter_us_docsis30),
        }
    },
    {
        "name": "Cable Channel Info",
        "os_min_versions": "7.58",
        "method": "POST",
        "params": {
            "page": "docInfo",
            "xhrId": "all",
            "xhr": 1
        },
        "link_type": FritzBoxLinkTypes.Cable,
        "response_parser": prepare_json_response_data,
        "interval": 600,
        "value_instances": {
            # DOCSIS 3.1 down stream
            "cable_channel_ds_docsis31_power_level": channel_metric("data.channelDs.docsis31", "powerLevel", str, exclude_filter_ds_docsis31),
            "cable_channel_ds_docsis31_non_corrected_errors": channel_metric("data.channelDs.docsis31", "nonCorrErrors", int, exclude_filter_ds_docsis31),
            "cable_channel_ds_docsis31_modulation": channel_metric("data.channelDs.docsis31", "modulation", str, exclude_filter_ds_docsis31),
            "cable_channel_ds_docsis31_plc": channel_metric("data.channelDs.docsis31", "plc", str, exclude_filter_ds_docsis31),
            "cable_channel_ds_docsis31_mer": channel_metric("data.channelDs.docsis31", "mer", str, exclude_filter_ds_docsis31),
            "cable_channel_ds_docsis31_fft": channel_metric("data.channelDs.docsis31", "fft", str, exclude_filter_ds_docsis31),
            "cable_channel_ds_docsis31_frequency": channel_metric("data.channelDs.docsis31", "frequency", str, exclude_filter_ds_docsis31),
            
            # DOCSIS 3.0 down stream
            "cable_channel_ds_docsis30_power_level": channel_metric("data.channelDs.docsis30", "powerLevel", str, exclude_filter_ds_docsis30),
            "cable_channel_ds_docsis30_non_corrected_errors": channel_metric("data.channelDs.docsis30", "nonCorrErrors", int, exclude_filter_ds_docsis30),
            "cable_channel_ds_docsis30_modulation": channel_metric("data.channelDs.docsis30", "modulation", str, exclude_filter_ds_docsis30),
            "cable_channel_ds_docsis30_corrected_errors": channel_metric("data.channelDs.docsis30", "corrErrors", int, exclude_filter_ds_docsis30),
            "cable_channel_ds_docsis30_latency": channel_metric("data.channelDs.docsis30", "latency", float, exclude_filter_ds_docsis30),
            "cable_channel_ds_docsis30_mse": channel_metric("data.channelDs.docsis30", "mse", float, exclude_filter_ds_docsis30),
            "cable_channel_ds_docsis30_frequency": channel_metric("data.channelDs.docsis30", "frequency", str, exclude_filter_ds_docsis30),
            
            # DOCSIS 3.1 up stream
            "cable_channel_us_docsis31_power_level": channel_metric("data.channelUs.docsis31", "powerLevel", str, exclude_filter_us_docsis31),
            "cable_channel_us_docsis31_modulation": channel_metric("data.channelUs.docsis31", "modulation", str, exclude_filter_us_docsis31),
            "cable_channel_us_docsis31_activesub": channel_metric("data.channelUs.docsis31", "activesub", int, exclude_filter_us_docsis31),
            "cable_channel_us_docsis31_fft": channel_metric("data.channelUs.docsis31", "fft", str, exclude_filter_us_docsis31),
            "cable_channel_us_docsis31_frequency": channel_metric("data.channelUs.docsis31", "frequency", str, exclude_filter_us_docsis31),
            
            # DOCSIS 3.0 up stream
            "cable_channel_us_docsis30_power_level": channel_metric("data.channelUs.docsis30", "powerLevel", str, exclude_filter_us_docsis30),
            "cable_channel_us_docsis30_modulation": channel_metric("data.channelUs.docsis30", "modulation", str, exclude_filter_us_docsis30),
            "cable_channel_us_docsis30_multiplex": channel_metric("data.channelUs.docsis30", "multiplex", str, exclude_filter_us_docsis30),
            "cable_channel_us_docsis30_frequency": channel_metric("data.channelUs.docsis30", "frequency", str, exclude_filter_us_docsis30),
        }
    }
]

_registered_names = {
    (service.get("name"), service.get("os_min_versions"), service.get("os_max_versions"))
    for service in lua_services
}

for service in _CONNECTION_INFO_SERVICES:
    key = (
        service.get("name"),
        service.get("os_min_versions"),
        service.get("os_max_versions"),
    )
    if key not in _registered_names:
        lua_services.append(service)
