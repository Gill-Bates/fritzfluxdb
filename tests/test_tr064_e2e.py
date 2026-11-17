#!/usr/bin/env python3
#
# tests/test_tr064_e2e.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Handler leg of the simulated end-to-end path.

FritzBoxHandler -> native client -> device and SCPD discovery -> digest login ->
SOAP ``DeviceInfo.GetInfo`` -> service queries -> ``FritzMeasurement`` values.
No real FRITZ!Box: ``FakeFritzBox`` answers through ``httpx.MockTransport``.
"""

import configparser
import logging

import pytest

from app.fritzbox.config import FritzBoxConfig
from app.fritzbox.handler import FritzBoxHandler
from tests.fakebox import DigestVerifier, FakeFritzBox, FakeService, attach_to_handler

PASSWORD = "e2e-password-value"
TR64 = "tr64desc.xml"
IGD = "igddesc.xml"


def _config() -> FritzBoxConfig:
    parser = configparser.ConfigParser()
    parser["fritzbox"] = {"hostname": "fritz.box", "username": "user", "password": PASSWORD}
    return FritzBoxConfig(parser)


def _box() -> FakeFritzBox:
    services = [
        FakeService("DeviceInfo1", "DeviceInfo:1", TR64, actions={"GetInfo": (
            ("NewModelName", "out", "string"), ("NewSoftwareVersion", "out", "string"),
            ("NewSerialNumber", "out", "string"), ("NewDescription", "out", "string"),
            ("NewUpTime", "out", "ui4"),
        )}),
        FakeService("Time1", "Time:1", TR64, actions={"GetInfo": (("NewCurrentLocalTime", "out", "string"),)}),
        FakeService("WANCommonIFC1", "WANCommonInterfaceConfig:1", IGD, actions={
            "GetCommonLinkProperties": (
                ("NewWANAccessType", "out", "string"), ("NewLayer1UpstreamMaxBitRate", "out", "ui4"),
                ("NewLayer1DownstreamMaxBitRate", "out", "ui4"),
            ),
            "GetAddonInfos": (("NewByteSendRate", "out", "ui4"), ("NewX_AVM_DE_TotalBytesSent64", "out", "ui8")),
        }),
        FakeService("WANIPConn1", "WANIPConnection:1", IGD, depth=2, actions={"GetStatusInfo": (
            ("NewConnectionStatus", "out", "string"), ("NewUptime", "out", "ui4"),
            ("NewLastConnectionError", "out", "string"),
        )}),
        FakeService("WANPPPConnection1", "WANPPPConnection:1", TR64, depth=2, actions={"GetInfo": (
            ("NewConnectionStatus", "out", "string"), ("NewUptime", "out", "ui4"),
            ("NewExternalIPAddress", "out", "string"),
        )}),
    ]
    box = FakeFritzBox(services, digest=DigestVerifier("user", PASSWORD))
    box.digest_only_posts = True
    box.respond("DeviceInfo1", "GetInfo", {
        "NewModelName": "FRITZ!Box 7590", "NewSoftwareVersion": "7.57", "NewSerialNumber": "3C:A6:2F:00:00:01",
        "NewDescription": "FRITZ!Box 7590 7.57", "NewUpTime": "90000",
    })
    box.respond("Time1", "GetInfo", {"NewCurrentLocalTime": "2026-11-16T10:00:00+01:00"})
    box.respond("WANCommonIFC1", "GetCommonLinkProperties", {
        "NewWANAccessType": "DSL", "NewLayer1UpstreamMaxBitRate": "100000000",
        "NewLayer1DownstreamMaxBitRate": "250000000",
    })
    box.respond("WANCommonIFC1", "GetAddonInfos", {"NewByteSendRate": "1500", "NewX_AVM_DE_TotalBytesSent64": "123456789012"})
    box.respond("WANIPConn1", "GetStatusInfo", {
        "NewConnectionStatus": "Connected", "NewUptime": "3600", "NewLastConnectionError": "ERROR_NONE",
    })
    box.respond("WANPPPConnection1", "GetInfo", {
        "NewConnectionStatus": "Connected", "NewUptime": "3600", "NewExternalIPAddress": "203.0.113.7",
    })
    return box


def _measurements(handler: FritzBoxHandler) -> dict[str, object]:
    return {m.name: m.value for m in handler.current_result_list}


def test_login_and_service_queries_produce_the_documented_metric_values(monkeypatch):
    box = _box()
    created = attach_to_handler(monkeypatch, box)
    handler = FritzBoxHandler(_config())

    handler.connect()

    assert handler.init_successful is True
    assert handler.session is created[0]
    assert handler.config.tls_enabled is True
    assert (handler.config.model, handler.config.fw_version) == ("FRITZ!Box 7590", "7.57")
    assert handler.config.serial_number == "3C:A6:2F:00:00:01"
    assert handler.config.link_type == "DSL"
    # The digest challenge is answered silently: it is no rejected login.
    assert box.digest.anonymous >= 1
    assert box.digest.accepted >= 1
    assert box.digest.bad_response == 0
    assert box.digest.duplicate_nc == []
    assert handler._rejected_attempts == 0
    assert handler.auth_failed is False

    wanted = {"DeviceInfo", "WANCommonIFC", "WANIPConn", "WANPPPConnection:1"}
    for service in (s for s in handler.services if s.name in wanted):
        assert handler._query_service_with_backoff(service) is True

    values = _measurements(handler)
    assert values["systemuptime"] == 90000
    assert values["totalbytessent"] == 123456789012
    assert values["sendrate"] == 1500
    assert values["connection_status"] == "Connected"
    assert values["linkuptime"] == 3600
    assert values["external_ip"] == "203.0.113.7"
    assert {name: type(value) for name, value in values.items() if name in {
        "systemuptime", "totalbytessent", "sendrate", "linkuptime", "connection_status", "external_ip",
    }} == {
        "systemuptime": int, "totalbytessent": int, "sendrate": int, "linkuptime": int,
        "connection_status": str, "external_ip": str,
    }
    assert box.digest.bad_response == 0
    assert box.digest.duplicate_nc == []
    handler.close()


def test_a_wrong_password_is_a_counted_credential_rejection(monkeypatch):
    box = _box()
    created = attach_to_handler(monkeypatch, box)
    parser = configparser.ConfigParser()
    parser["fritzbox"] = {"hostname": "fritz.box", "username": "user", "password": "wrong-password-value"}
    handler = FritzBoxHandler(FritzBoxConfig(parser))

    handler.connect()

    assert handler.auth_failed is True
    assert handler._rejected_attempts == 1
    assert handler.session is None
    assert created[0].closed is True
    assert box.digest.accepted == 0


def test_no_secret_reaches_logs_or_exception_texts(monkeypatch, caplog):
    box = _box()
    attach_to_handler(monkeypatch, box)
    handler = FritzBoxHandler(_config())

    with caplog.at_level(logging.DEBUG):
        handler.connect()
        for service in handler.services:
            handler._query_service_with_backoff(service)
        handler.close()

    text = caplog.text
    assert PASSWORD not in text
    assert "Authorization" not in text
    assert "nonce" not in text.lower()
    assert "cnonce" not in text.lower()
    for record in caplog.records:
        assert PASSWORD not in repr(record.args)
        if record.exc_info:
            assert PASSWORD not in str(record.exc_info[1])


@pytest.mark.parametrize("https_available", [True, False])
def test_the_protocol_that_answered_is_the_one_reflected_in_the_configuration(monkeypatch, https_available):
    from tests.fakebox import fail_scheme

    box = _box()
    if not https_available:
        fail_scheme(box)
    attach_to_handler(monkeypatch, box)
    handler = FritzBoxHandler(_config())

    handler.connect()

    assert handler.init_successful is True
    assert handler.config.tls_enabled is https_available
    assert handler.session.use_tls is https_available
    handler.close()
