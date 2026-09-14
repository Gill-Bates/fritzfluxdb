#!/usr/bin/env python3
#
# tests/test_fritzbox_shared_parsers.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from dataclasses import dataclass

import pytest

from fritzfluxdb.classes.fritzbox.service_definitions import (
    connection_info,
    logs,
    network_hosts,
    system_stats,
    vpn_data,
)
from fritzfluxdb.classes.fritzbox.service_definitions.helpers import (
    parse_fritzbox_bool,
    parse_optional_json_response,
    parse_required_json_response,
)


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
def test_parse_fritzbox_bool(value, expected):
    assert parse_fritzbox_bool(value) is expected


@pytest.mark.parametrize("value", [None, "maybe", 1.5])
def test_parse_fritzbox_bool_rejects_unknown_values(value):
    with pytest.raises(ValueError, match="invalid boolean value"):
        parse_fritzbox_bool(value)


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


def test_required_json_parser_parses_successful_responses():
    payload = {"data": {"active": []}}
    assert parse_required_json_response(Response(200, payload)) == payload


@pytest.mark.parametrize("response", [Response(500, {}), Response(200, ValueError("bad JSON"))])
def test_required_json_parser_rejects_invalid_responses(response):
    with pytest.raises(ValueError):
        parse_required_json_response(response)
