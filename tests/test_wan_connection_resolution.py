#!/usr/bin/env python3
#
# tests/test_wan_connection_resolution.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Default-route selection and recovery from unavailable WAN metadata."""

import asyncio
import configparser
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.fritzbox.handler import FritzBoxHandler
from app.fritzbox.tr064.exceptions import TR064ProtocolError
from app.fritzbox.wan_connection import parse_default_connection, resolve_status_call
from app.fritzbox.wan_watch import WanWatcher
from tests.fakebox import FakeFritzBox, FakeService, attach_to_handler, default_services


@pytest.mark.parametrize("value", [
    "1.WANPPPConnection.1", "1.WANConnectionDevice.1.WANPPPConnection.1",
    "urn:WANPPPConnection-com:serviceId:WANPPPConnection1",
])
def test_default_service_formats(value):
    assert parse_default_connection(value) == "WANPPPConnection:1"


@pytest.mark.parametrize("value", [None, "", "WANPPPConnection0", "WANPPPConnection1/evil", "x" * 1025])
def test_invalid_default_service_is_rejected(value):
    with pytest.raises(TR064ProtocolError):
        parse_default_connection(value)


def route_box(value="1.WANPPPConnection.1"):
    services = default_services() + [
        FakeService("Layer3Forwarding1", "Layer3Forwarding:1", "tr64desc.xml", actions={
            "GetDefaultConnectionService": (("NewDefaultConnectionService", "out", "string"),),
        }),
        FakeService("WANCommonInterfaceConfig1", "WANCommonInterfaceConfig:1", "tr64desc.xml", actions={
            "GetCommonLinkProperties": (("NewWANAccessType", "out", "string"),),
        }),
    ]
    box = FakeFritzBox(services)
    box.respond("Layer3Forwarding1", "GetDefaultConnectionService", {"NewDefaultConnectionService": value})
    box.respond("WANCommonInterfaceConfig1", "GetCommonLinkProperties", {"NewWANAccessType": "DSL"})
    return box


@pytest.mark.parametrize(("value", "physical", "expected"), [
    ("1.WANPPPConnection.1", "Ethernet", "WANPPPConnection:1"),
    ("1.WANIPConnection.1", "DSL", "WANIPConnection:1"),
    ("1.WANPPPConnection.2", "Fiber", "WANPPPConnection:2"),
])
def test_default_route_overrides_physical_access(value, physical, expected):
    client = route_box(value).tr064_client()
    try:
        assert resolve_status_call(client, physical) == (expected, "GetInfo")
    finally:
        client.close()


def test_failed_initial_metadata_is_retried_by_polling_loop(monkeypatch):
    box = route_box()
    path = "/upnp/control/wancommoninterfaceconfig1"

    def unavailable(request):
        raise httpx.ReadTimeout("temporary metadata failure", request=request)

    box.responders[path] = unavailable
    attach_to_handler(monkeypatch, box)
    handler = FritzBoxHandler(configparser.ConfigParser())
    try:
        handler.connect()
        assert handler.init_successful
        assert handler.config.link_type is None
        del box.responders[path]
        handler._wan_metadata_after = 0
        assert asyncio.run(handler._before_cycle())
        assert handler.config.link_type == "DSL"
        assert handler._wan_status_call == ("WANPPPConnection:1", "GetInfo")
    finally:
        handler.close()


@pytest.mark.parametrize("status", [401, 403])
def test_monitoring_user_without_route_permissions_keeps_collecting(status):
    box = route_box()
    box.responders["/upnp/control/layer3forwarding1"] = lambda request: httpx.Response(status)
    client = box.tr064_client()
    try:
        assert resolve_status_call(client, "DSL") == ("WANPPPConnection:1", "GetInfo")
    finally:
        client.close()


def test_watcher_detects_pppoe_interruption_over_ethernet():
    box = route_box()
    clock = {"t": 0}
    origin = datetime(2026, 11, 17, tzinfo=UTC)
    client = box.tr064_client()
    watcher = WanWatcher(lambda: client, lambda: "Ethernet", lambda exc: False)

    def poll(t, status, uptime):
        clock["t"] = t
        box.respond("WANPPPConnection1", "GetInfo", {"NewConnectionStatus": status, "NewUptime": str(uptime)})
        return watcher.poll_once(lambda: origin + timedelta(seconds=clock["t"]), lambda: float(clock["t"]))

    try:
        assert poll(0, "Connected", 1000) == []
        assert poll(5, "Disconnected", 0)[0].kind == "opened"
        assert poll(10, "Connected", 2)[0].kind == "closed"
        assert ("WANIPConn1", "GetStatusInfo") not in box.calls
    finally:
        client.close()


def test_route_lookup_transient_failure_is_retried(monkeypatch):
    box = route_box()
    path = "/upnp/control/layer3forwarding1"

    def unavailable(request):
        raise httpx.ReadTimeout("temporary routing failure", request=request)

    box.responders[path] = unavailable
    attach_to_handler(monkeypatch, box)
    handler = FritzBoxHandler(configparser.ConfigParser())
    try:
        handler.connect()
        assert handler.init_successful
        assert handler._wan_status_call is None
        del box.responders[path]
        handler._wan_metadata_after = 0
        assert asyncio.run(handler._before_cycle())
        assert handler._wan_status_call == ("WANPPPConnection:1", "GetInfo")
    finally:
        handler.close()


def test_service_change_refreshes_selection_without_comparing_unrelated_counters():
    box = route_box()
    box.respond("WANPPPConnection1", "GetInfo", {"NewConnectionStatus": "Connected", "NewUptime": "1000"})
    box.respond("WANIPConnection1", "GetInfo", {"NewConnectionStatus": "Connected", "NewUptime": "2"})
    # The default fake IP service needs the status fields for this route-switch scenario.
    ip = next(service for service in box.services if service.name == "WANIPConnection1")
    ip.actions["GetInfo"] += (("NewConnectionStatus", "out", "string"), ("NewUptime", "out", "ui4"))
    clock = {"t": 0}
    origin = datetime(2026, 11, 17, tzinfo=UTC)
    client = box.tr064_client()
    watcher = WanWatcher(lambda: client, lambda: "Ethernet", lambda exc: False)

    def poll(t):
        clock["t"] = t
        return watcher.poll_once(lambda: origin + timedelta(seconds=clock["t"]), lambda: float(clock["t"]))

    try:
        assert poll(0) == []
        box.respond("Layer3Forwarding1", "GetDefaultConnectionService", {
            "NewDefaultConnectionService": "1.WANIPConnection.1",
        })
        assert poll(61) == []
        assert ("WANIPConnection1", "GetInfo") in box.calls
    finally:
        client.close()
