#!/usr/bin/env python3
#
# tests/test_fritzbox_shared_parsers.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from dataclasses import dataclass

import httpx
import pytest

from app.fritzbox.service_definitions import (
    connection_info,
    logs,
    network_hosts,
    system_stats,
    vpn_data,
)
from app.fritzbox.service_definitions.helpers import (
    parse_optional_json_response,
    parse_required_json_response,
    parse_required_text_response,
)
from app.utils.common import parse_bool


@dataclass
class Response:
    status_code: int
    payload: object
    url: str = "http://fritz.box/data.lua"

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        (" yes ", True),
        ("OFF", False),
    ],
)
def test_parse_bool(value, expected):
    assert parse_bool(value) is expected


@pytest.mark.parametrize("value", [None, "maybe", 1.5])
def test_parse_bool_rejects_unknown_values(value):
    with pytest.raises(ValueError, match="invalid boolean value"):
        parse_bool(value)


@pytest.mark.parametrize("field", ["channelID", "channel", "frequency"])
def test_channel_id_zero_is_preserved(field):
    assert connection_info.channel_id_tag({field: 0, "other": 10}) == {"id": "0"}
    assert connection_info.channel_id_tag({"channelID": 0, "channel": 20}) == {"id": "0"}


def test_optional_json_parser_is_shared_by_optional_endpoints():
    assert connection_info.prepare_json_response_data is parse_optional_json_response
    assert vpn_data.prepare_json_response_data is parse_optional_json_response


def test_optional_json_parser_returns_empty_data_for_404():
    assert parse_optional_json_response(Response(404, {"ignored": True})) == {}


def test_optional_json_parser_parses_successful_responses():
    payload = {"data": {"connected": True}}
    assert parse_optional_json_response(Response(200, payload)) == payload


@pytest.mark.parametrize("response", [Response(500, {}), Response(200, ValueError("bad JSON"))])
def test_optional_json_parser_rejects_invalid_responses(response):
    with pytest.raises(ValueError):
        parse_optional_json_response(response)


def test_required_json_parser_is_shared_by_required_endpoints():
    assert network_hosts.prepare_json_response_data is parse_required_json_response
    assert system_stats.prepare_json_response_data is parse_required_json_response
    assert logs.prepare_json_response_data is parse_required_json_response


def test_required_json_parser_rejects_404():
    # unlike the optional parser, a missing endpoint is an error here, not empty data
    with pytest.raises(ValueError, match="unexpected HTTP status 404"):
        parse_required_json_response(Response(404, {}))


@pytest.mark.parametrize("parser", [parse_required_json_response, parse_required_text_response])
def test_parser_errors_do_not_include_lua_session_id(parser):
    request = httpx.Request("GET", "https://fritz.box/data.lua?sid=sensitive-session-id")
    response = httpx.Response(500, request=request)

    with pytest.raises(ValueError) as error:
        parser(response)

    assert "/data.lua" in str(error.value)
    assert "sensitive-session-id" not in str(error.value)


def test_parser_error_without_request_uses_unknown_path():
    response = httpx.Response(500)

    with pytest.raises(ValueError, match="unexpected HTTP status 500 for <unknown>"):
        parse_required_json_response(response)


def test_required_json_parser_parses_successful_responses():
    payload = {"data": {"active": []}}
    assert parse_required_json_response(Response(200, payload)) == payload


@pytest.mark.parametrize("response", [Response(500, {}), Response(200, ValueError("bad JSON"))])
def test_required_json_parser_rejects_invalid_responses(response):
    with pytest.raises(ValueError):
        parse_required_json_response(response)


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (logs.parse_legacy_log_message, ["date", "time"]),
        (logs.parse_legacy_log_timestamp, ["date", "time"]),
        (logs.parse_modern_log_message, {"date": "16.11.26", "caller": "Private Person 555-0100"}),
        (logs.parse_modern_log_timestamp, {"date": "16.11.26", "msg": "Private Person 555-0100"}),
    ],
)
def test_malformed_router_log_errors_omit_raw_data(parser, payload):
    with pytest.raises(ValueError) as error:
        parser(payload)

    assert "Private Person" not in str(error.value)
    assert "555-0100" not in str(error.value)


@pytest.mark.parametrize(
    ("txt", "expected"),
    [("5 GHZ, 50 / 836 Mbit/s", 50), ("166 / 150 BIT/s", 166)],
)
def test_active_host_text_selection_is_case_insensitive(txt, expected):
    payload = {"properties": [{"txt": txt}]}

    assert network_hosts.get_active_host_details(payload, "downstream", 0) == expected


def test_log_timestamp_parsers_share_the_naive_format_and_reject_bad_payloads():
    from datetime import datetime

    from app.fritzbox.service_definitions.logs import (
        parse_legacy_log_timestamp,
        parse_modern_log_timestamp,
    )
    expected = datetime(2026, 11, 16, 12, 30, 5)
    assert parse_legacy_log_timestamp(["16.11.26", "12:30:05", "text"]) == expected
    assert parse_modern_log_timestamp({"date": "16.11.26", "time": "12:30:05"}) == expected
    with pytest.raises(ValueError):
        parse_legacy_log_timestamp(["16.11.26"])
    with pytest.raises(ValueError):
        parse_modern_log_timestamp({"date": "16.11.26"})


def test_metric_type_mapping_is_shared_and_immutable():
    from app.utils.measurement import METRIC_TYPES

    assert dict(METRIC_TYPES) == {"str": str, "int": int, "float": float, "bool": bool}
    assert METRIC_TYPES.get("decimal") is None
    with pytest.raises(TypeError):
        METRIC_TYPES["bytes"] = bytes  # type: ignore[index]


def test_questdb_columns_follow_the_shared_type_mapping():
    from app.influxdb.handler import _collect_metric_columns

    columns: dict[str, str] = {}
    for suffix in ("str", "int", "float", "bool", "decimal"):
        _collect_metric_columns("ignored", f"metric_{suffix}:{suffix}", columns)
    assert columns == {
        "metric_str": "VARCHAR", "metric_int": "LONG", "metric_float": "DOUBLE", "metric_bool": "BOOLEAN",
    }
