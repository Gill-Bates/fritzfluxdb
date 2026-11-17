#!/usr/bin/env python3
#
# tests/test_homeauto_identity.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import re
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from app.fritzbox import service_definitions
from app.fritzbox.service_definitions import homeauto
from app.influxdb.handler import InfluxHandler
from app.utils.measurement import FritzMeasurement


def _service() -> dict:
    return next(s for s in service_definitions.lua_services if s["name"] == "Home Automation")


def _ha_metrics() -> dict:
    return {k: v for k, v in _service()["value_instances"].items() if k.startswith("ha_")}


def _handler(version: str) -> InfluxHandler:
    handler = InfluxHandler.__new__(InfluxHandler)
    handler.version = version
    handler.config = SimpleNamespace(measurement_name="fritzbox_AA1", data_retention_days=0)
    return handler


def test_every_home_automation_metric_tags_the_ain_as_id():
    metrics = _ha_metrics()
    assert len(metrics) > 20
    for name, definition in metrics.items():
        tags = definition["next"]["tags_function"]({"@identifier": "11657 0123456", "name": "Lamp"})
        assert tags == {"id": "11657 0123456", "name": "Lamp"}, name


def test_id_tag_is_kept_for_a_device_without_name():
    tags_function = _ha_metrics()["ha_battery_percent"]["next"]["tags_function"]
    assert tags_function({"@identifier": "11657 0123456", "name": ""}) == {"id": "11657 0123456"}
    assert tags_function({"@identifier": "11657 0123456"}) == {"id": "11657 0123456"}
    assert tags_function({"name": "Lamp"}) == {"name": "Lamp"}


def test_testmode_homeauto_sample_is_packaged_and_readable(monkeypatch):
    monkeypatch.setenv("TESTMODE", "1")
    monkeypatch.setattr(homeauto, "test_data", None)
    parsed = homeauto.prepare_response_data(SimpleNamespace(status_code=500, url="unused", content=b""))

    devices = parsed["devicelist"]["device"]
    assert devices[0]["@identifier"] == "00000 0000001"
    assert devices[0]["name"] == "Test Switch"


def test_homeauto_parser_error_omits_lua_session_id():
    request = httpx.Request("GET", "https://fritz.box/data.lua?sid=sensitive-session-id")
    response = httpx.Response(500, request=request)

    with pytest.raises(ValueError) as error:
        homeauto.prepare_response_data(response)

    assert "/data.lua" in str(error.value)
    assert "sensitive-session-id" not in str(error.value)


@pytest.mark.parametrize("version", [2, "questdb"])
@pytest.mark.parametrize("name", ["Same", ""])
def test_devices_with_equal_or_empty_names_stay_distinct_series(version, name):
    handler = _handler(version)
    tags_function = _ha_metrics()["ha_temperature"]["next"]["tags_function"]
    ts = datetime(2026, 1, 1, 10, 0, 0).astimezone()
    measurements = [
        FritzMeasurement("ha_temperature", 21.5, data_type=float, box_tag="b", timestamp=ts,
                         additional_tags=tags_function({"@identifier": ain, "name": name}))
        for ain in ("11657 0000001", "11657 0000002")
    ]
    lines, _ = handler.bundle_measurements(measurements)
    assert len({re.split(r"(?<!\\) ", line)[0] for line in lines}) == 2
