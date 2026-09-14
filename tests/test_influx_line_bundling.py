#!/usr/bin/env python3
#
# tests/test_influx_line_bundling.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from datetime import UTC, datetime
from types import SimpleNamespace

from fritzfluxdb.classes.common import FritzMeasurement
from fritzfluxdb.classes.influxdb.handler import InfluxHandler

TIMESTAMP = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
TS = int(TIMESTAMP.timestamp())


def make_handler(version="questdb"):
    handler = InfluxHandler.__new__(InfluxHandler)
    handler.version = version
    handler.config = SimpleNamespace(measurement_name="fritzbox")
    return handler


def measurement(name, value, tags=None, timestamp=TIMESTAMP):
    return FritzMeasurement(name, value, box_tag="fritz.box", additional_tags=tags, timestamp=timestamp)


def test_fields_with_same_series_and_timestamp_share_one_line():
    lines, valid = make_handler().bundle_measurements([
        measurement("sendrate", 10),
        measurement("receiverate", 20),
        measurement("connection_status", "Connected"),
    ])

    assert lines == [f'fritzbox,box=fritz.box sendrate=10i,receiverate=20i,connection_status="Connected" {TS}']
    assert len(valid) == 3


def test_different_tags_or_timestamps_stay_on_separate_lines():
    lines, _ = make_handler().bundle_measurements([
        measurement("active_hosts_name", "laptop", tags={"uid": "a"}),
        measurement("active_hosts_name", "phone", tags={"uid": "b"}),
        measurement("active_hosts_mac", "00:11", tags={"uid": "a"}),
        measurement("cpu_temp", 50, timestamp=TIMESTAMP.replace(second=1)),
    ])

    assert lines == [
        f'fritzbox,box=fritz.box,uid=a active_hosts_name="laptop",active_hosts_mac="00:11" {TS}',
        f'fritzbox,box=fritz.box,uid=b active_hosts_name="phone" {TS}',
        f"fritzbox,box=fritz.box cpu_temp=50i {TS + 1}",
    ]


def test_duplicate_field_starts_a_new_line_instead_of_overwriting():
    lines, valid = make_handler().bundle_measurements([
        measurement("log_entry", "first", tags={"log_type": "System"}),
        measurement("log_entry", "second", tags={"log_type": "System"}),
    ])

    assert lines == [
        f'fritzbox,box=fritz.box,log_type=System log_entry="first" {TS}',
        f'fritzbox,box=fritz.box,log_type=System log_entry="second" {TS}',
    ]
    assert len(valid) == 2


def test_unwritable_values_are_dropped_without_affecting_the_line():
    lines, valid = make_handler().bundle_measurements([
        measurement("totalbytessent", 2 ** 64),
        measurement("cpu_temp", float("nan")),
        measurement("cpu_utilization", 7),
    ])

    assert lines == [f"fritzbox,box=fritz.box cpu_utilization=7i {TS}"]
    assert [m.name for m in valid] == ["cpu_utilization"]


def test_questdb_replaces_dots_in_field_names_but_influxdb_keeps_them():
    wlan = measurement("wlan1_802.11_standard", "ax")

    questdb_lines, _ = make_handler("questdb").bundle_measurements([wlan])
    influx_lines, _ = make_handler(2).bundle_measurements([wlan])

    assert questdb_lines == [f'fritzbox,box=fritz.box wlan1_802_11_standard="ax" {TS}']
    assert influx_lines == [f'fritzbox,box=fritz.box wlan1_802.11_standard="ax" {TS}']
    assert wlan.name == "wlan1_802.11_standard"


def test_single_measurement_matches_to_line_protocol():
    point = measurement("linkuptime", 3600)

    lines, _ = make_handler(1).bundle_measurements([point])

    assert lines == [point.to_line_protocol("fritzbox")]
