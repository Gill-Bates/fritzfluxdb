#!/usr/bin/env python3
#
# tests/test_tr064_avm_fixtures.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Discovery and SOAP parsing against AVM-style documents that FakeFritzBox did not produce.

The files under ``tests/fixtures/avm`` are hand-written approximations of the
AVM format (default namespaces, ``eventSubURL``, nested ``deviceList``,
``sendEvents`` attributes), not recordings from a real FRITZ!Box.
"""

from pathlib import Path

import pytest

from app.fritzbox.tr064.discovery import build_discovery, parse_scpd, validate_path
from app.fritzbox.tr064.exceptions import TR064ProtocolError
from app.fritzbox.tr064.soap import build_request, parse_response

FIXTURES = Path(__file__).parent / "fixtures" / "avm"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture(scope="module")
def discovery():
    return build_discovery(None, load("tr64desc.xml"))


def test_default_namespace_and_nested_devices_yield_all_services(discovery):
    assert set(discovery.services) == {"DeviceInfo1", "Hosts1", "WLANConfiguration1", "WANCommonIFC1"}
    wan = discovery.resolve("WANCommonIFC")
    assert wan.service_type == "urn:dslforum-org:service:WANCommonInterfaceConfig:1"
    assert (wan.control_url, wan.scpd_url, wan.origin) == ("/upnp/control/wancommonifconfig1", "/wancommonifconfigSCPD.xml", "tr64desc.xml")
    assert discovery.resolve("WLANConfiguration:1").control_url == "/upnp/control/wlanconfig1"


def test_every_path_in_the_description_passes_the_path_rules(discovery):
    for info in discovery.services.values():
        assert validate_path(info.control_url) == info.control_url
        assert validate_path(info.scpd_url) == info.scpd_url


@pytest.mark.parametrize(
    ("document", "action", "outs"),
    [
        ("deviceinfoSCPD.xml", "GetInfo", {"NewManufacturerName": "string", "NewUpTime": "ui4"}),
        ("wlanconfigSCPD.xml", "GetInfo", {"NewEnable": "boolean", "NewChannel": "ui1", "NewSSID": "string"}),
        ("wlanconfigSCPD.xml", "GetTotalAssociations", {"NewTotalAssociations": "ui2"}),
        ("wancommonifconfigSCPD.xml", "GetCommonLinkProperties", {"NewLayer1DownstreamMaxBitRate": "ui4"}),
        ("hostsSCPD.xml", "GetGenericHostEntry", {"NewLeaseTimeRemaining": "i4", "NewActive": "boolean"}),
    ],
)
def test_scpd_types_are_resolved_through_related_state_variables(document, action, outs):
    scpd = parse_scpd(load(document))
    resolved = {a.name: a.data_type for a in scpd.actions[action].out_arguments}
    assert {name: resolved[name] for name in outs} == outs


def test_input_arguments_of_the_fixtures_are_typed():
    hosts = parse_scpd(load("hostsSCPD.xml")).actions["GetGenericHostEntry"]
    assert [(a.name, a.data_type) for a in hosts.in_arguments] == [("NewIndex", "ui2")]
    wlan = parse_scpd(load("wlanconfigSCPD.xml")).actions["SetEnable"]
    assert [(a.name, a.data_type) for a in wlan.in_arguments] == [("NewEnable", "boolean")]


def test_request_for_a_fixture_action_follows_the_discovered_service_type(discovery):
    action = parse_scpd(load("hostsSCPD.xml")).actions["GetGenericHostEntry"]
    body = build_request(discovery.resolve("Hosts").service_type, action, {"NewIndex": 3}).decode()
    assert '<u:GetGenericHostEntry xmlns:u="urn:dslforum-org:service:Hosts:1"><NewIndex>3</NewIndex>' in body
    with pytest.raises(TR064ProtocolError, match="missing"):
        build_request("urn:dslforum-org:service:Hosts:1", action, {})


SOAP_ANSWER = (
    '<?xml version="1.0"?>\n'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">\n'
    "<s:Body>\n"
    '<u:GetGenericHostEntryResponse xmlns:u="urn:dslforum-org:service:Hosts:1">\n'
    "<NewIPAddress>192.168.178.20</NewIPAddress>\n"
    "<NewMACAddress>AA:BB:CC:DD:EE:FF</NewMACAddress>\n"
    "<NewLeaseTimeRemaining>-1</NewLeaseTimeRemaining>\n"
    "<NewActive>1</NewActive>\n"
    "<NewHostName>laptop</NewHostName>\n"
    "</u:GetGenericHostEntryResponse>\n"
    "</s:Body>\n"
    "</s:Envelope>\n"
)


def test_prefixed_pretty_printed_answer_is_converted_by_scpd_type():
    action = parse_scpd(load("hostsSCPD.xml")).actions["GetGenericHostEntry"]
    assert parse_response(SOAP_ANSWER.encode(), "Hosts", action) == {
        "NewIPAddress": "192.168.178.20",
        "NewMACAddress": "AA:BB:CC:DD:EE:FF",
        "NewLeaseTimeRemaining": -1,
        "NewActive": True,
        "NewHostName": "laptop",
    }


def test_out_of_range_channel_in_an_answer_is_omitted():
    action = parse_scpd(load("wlanconfigSCPD.xml")).actions["GetInfo"]
    answer = SOAP_ANSWER.replace("GetGenericHostEntryResponse", "GetInfoResponse").replace(
        "<NewIPAddress>192.168.178.20</NewIPAddress>", "<NewChannel>300</NewChannel><NewSSID>home</NewSSID>"
    )
    assert parse_response(answer.encode(), "WLANConfiguration", action) == {"NewSSID": "home"}
