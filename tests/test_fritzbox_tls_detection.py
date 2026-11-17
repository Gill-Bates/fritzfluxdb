#!/usr/bin/env python3
#
# tests/test_fritzbox_tls_detection.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import configparser
import contextlib
import ssl
from datetime import UTC, datetime
from unittest.mock import Mock

import httpx
import pytest

from app.fritzbox.config import FritzBoxConfig
from app.fritzbox.handler import FritzBoxHandler, FritzBoxLuaHandler, _is_auth_failure
from app.fritzbox.tr064.exceptions import (
    TR064ActionNotFound,
    TR064HttpError,
    TR064HttpForbidden,
    TR064HttpUnauthorized,
    TR064NotAuthorized,
    TR064ProtocolError,
    TR064SoapFault,
)
from tests.fakebox import (
    FakeFritzBox,
    FakeService,
    attach_to_handler,
    default_services,
    fail_scheme,
)


def make_config(**options):
    parser = configparser.ConfigParser()
    parser["fritzbox"] = {
        "hostname": "fritz.box",
        "username": "user",
        "password": "password",
        **options,
    }
    return FritzBoxConfig(parser)


def test_removed_tls_settings_do_not_override_detection(monkeypatch):
    monkeypatch.setenv("FRITZBOX_TLS_ENABLED", "false")
    monkeypatch.setenv("FRITZBOX_VERIFY_TLS", "true")

    config = make_config()

    assert config.tls_enabled is None
    assert not hasattr(config, "verify_tls")


def _device_box() -> FakeFritzBox:
    """Fake box whose DeviceInfo answers model, firmware and serial like a real box."""
    services = [s for s in default_services() if s.name != "DeviceInfo1"]
    services.append(FakeService("DeviceInfo1", "DeviceInfo:1", "tr64desc.xml", actions={
        "GetInfo": (
            ("NewModelName", "out", "string"),
            ("NewSoftwareVersion", "out", "string"),
            ("NewSerialNumber", "out", "string"),
        ),
    }))
    box = FakeFritzBox(services)
    box.respond("DeviceInfo1", "GetInfo", {
        "NewModelName": "FRITZ!Box", "NewSoftwareVersion": "7.50", "NewSerialNumber": "123",
    })
    return box


def _attempts(box: FakeFritzBox) -> list[tuple[bool, int | None]]:
    """(uses TLS, port) of each distinct connection attempt, in order."""
    return list(dict.fromkeys((r.url.scheme == "https", r.url.port) for r in box.requests))


@pytest.mark.parametrize("https_available", [True, False])
def test_tr064_detects_available_protocol(monkeypatch, https_available):
    config = make_config()
    box = _device_box()
    if not https_available:
        fail_scheme(box)
    created = attach_to_handler(monkeypatch, box)
    handler = FritzBoxHandler(config)

    handler.connect()

    assert handler.init_successful
    assert config.tls_enabled is https_available
    assert handler.session is created[-1]
    assert handler.session.use_tls is https_available
    assert config.model == "FRITZ!Box"
    assert config.fw_version == "7.50"
    assert config.serial_number == "123"
    assert _attempts(box) == ([(True, 49443)] if https_available else [(True, 49443), (False, 49000)])
    handler.close()


class _LuaClient:
    """httpx.Client stand-in that records URLs and serves a legacy-challenge login."""

    def __init__(self, urls, https_error=None):
        self.urls = urls
        self.https_error = https_error
        self.timeout = httpx.Timeout(10)

    def close(self):
        pass

    @contextlib.contextmanager
    def stream(self, method, url, *, params=None, timeout=None):
        self.urls.append(url)
        if url.startswith("https:") and self.https_error is not None:
            raise self.https_error
        if params is None:
            body = b"<SessionInfo><SID>0000000000000000</SID><Challenge>test</Challenge></SessionInfo>"
        else:
            body = b"<SessionInfo><SID>1234567890abcdef</SID></SessionInfo>"
        yield httpx.Response(200, content=body, request=httpx.Request(method, url))


@pytest.mark.parametrize("https_available", [True, False])
def test_lua_detects_protocol_without_certificate_verification(monkeypatch, https_available):
    config = make_config()
    urls = []
    client_options = []
    error = None if https_available else httpx.ConnectError("HTTPS unavailable")
    monkeypatch.setattr(
        "app.fritzbox.handler.httpx.Client",
        lambda **kwargs: client_options.append(kwargs) or _LuaClient(urls, error),
    )
    monkeypatch.setattr(FritzBoxLuaHandler, "_legacy_login_response", lambda *_: "response")
    handler = FritzBoxLuaHandler(config)

    handler.connect()

    assert handler.init_successful
    assert all(options["verify"] is False for options in client_options)
    assert urls[0] == "https://fritz.box/login_sid.lua"
    assert handler.url == ("https://fritz.box" if https_available else "http://fritz.box")
    assert config.tls_enabled is https_available


@pytest.mark.parametrize(
    ("port", "https_available", "expected"),
    [
        (49000, True, [(True, 49443)]),
        (50000, True, [(True, 50000)]),
        (50000, False, [(True, 50000), (False, 50000)]),
    ],
)
def test_tr064_port_selection(monkeypatch, port, https_available, expected):
    config = make_config(port=str(port))
    box = FakeFritzBox()
    if not https_available:
        fail_scheme(box)
    attach_to_handler(monkeypatch, box)
    handler = FritzBoxHandler(config)

    handler.connect()

    assert _attempts(box) == expected
    handler.close()


@pytest.mark.parametrize("https_available", [True, False])
def test_lua_brackets_ipv6_hosts_in_urls(monkeypatch, https_available):
    config = make_config(hostname="2001:db8::1")
    urls = []
    error = None if https_available else httpx.ConnectError("HTTPS unavailable")
    monkeypatch.setattr("app.fritzbox.handler.httpx.Client", lambda **_: _LuaClient(urls, error))

    handler = FritzBoxLuaHandler(config)
    handler.connect()

    assert handler.init_successful
    assert urls[0] == "https://[2001:db8::1]/login_sid.lua"
    assert handler.url == ("https://[2001:db8::1]" if https_available else "http://[2001:db8::1]")


def test_lua_trusts_only_the_configured_ca_file(monkeypatch, tmp_path):
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("placeholder")
    context = object()
    contexts = []
    client_options = []
    monkeypatch.setattr(
        "app.fritzbox.handler.ssl.create_default_context",
        lambda **kwargs: contexts.append(kwargs) or context,
    )
    config = make_config(ca_file=str(ca_file))
    monkeypatch.setattr(
        "app.fritzbox.handler.httpx.Client",
        lambda **kwargs: client_options.append(kwargs) or _LuaClient([]),
    )

    FritzBoxLuaHandler(config).connect()

    assert contexts == [{"cafile": str(ca_file)}, {"cafile": str(ca_file)}]
    assert client_options[0]["verify"] is context


def test_lua_failed_certificate_verification_does_not_fall_back_to_http(monkeypatch, tmp_path):
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("placeholder")
    urls = []
    error = httpx.ConnectError("certificate verify failed")
    error.__cause__ = ssl.SSLCertVerificationError("self-signed certificate")
    monkeypatch.setattr("app.fritzbox.handler.ssl.create_default_context", lambda **_: object())
    config = make_config(ca_file=str(ca_file))
    monkeypatch.setattr("app.fritzbox.handler.httpx.Client", lambda **_: _LuaClient(urls, error))

    handler = FritzBoxLuaHandler(config)
    handler.connect()

    assert not handler.init_successful
    assert urls == ["https://fritz.box/login_sid.lua"]


def test_lua_warns_once_that_the_certificate_is_not_verified(monkeypatch):
    log = Mock()
    monkeypatch.setattr("app.fritzbox.handler.log", log)
    monkeypatch.setattr("app.fritzbox.handler.httpx.Client", lambda **_: _LuaClient([]))

    handler = FritzBoxLuaHandler(make_config())
    handler._build_session()
    handler._build_session()

    log.warning.assert_called_once()


def test_missing_ca_file_is_a_config_error(tmp_path):
    assert make_config(ca_file=str(tmp_path / "missing.pem")).parser_error is True


def test_invalid_ca_bundle_is_a_config_error(tmp_path):
    ca_file = tmp_path / "invalid.pem"
    ca_file.write_text("not a certificate", encoding="utf-8")

    assert make_config(ca_file=str(ca_file)).parser_error is True


def test_timezone_check_uses_the_offset_in_effect_at_the_measured_instant(monkeypatch):
    # 01:30 UTC on 2026-03-29 is already CEST (+02:00), but 01:30 as a naive local time is still CET.
    fixed_now = datetime(2026, 3, 29, 1, 30, tzinfo=UTC)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    log = Mock()
    box = FakeFritzBox()
    box.respond("Time1", "GetInfo", {"NewCurrentLocalTime": "2026-03-29T03:30:00+02:00"})
    attach_to_handler(monkeypatch, box)
    monkeypatch.setattr("app.fritzbox.handler.datetime", FixedDatetime)
    monkeypatch.setattr("app.fritzbox.handler.log", log)

    handler = FritzBoxHandler(make_config(timezone="Europe/Berlin"))
    handler.connect()

    assert handler.init_successful
    log.warning.assert_not_called()
    handler.close()



@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TR064HttpUnauthorized(), True),
        (TR064HttpForbidden(), False),
        (TR064HttpError(500, "connection to 192.168.178.1:401 failed"), False),
        (TR064SoapFault(401, "Invalid Action"), False),
        (TR064NotAuthorized(606, "Action not authorized"), False),
        (TR064ActionNotFound("DeviceInfo", "GetInfo"), False),
        (TR064ProtocolError("status 401 in the text"), False),
    ],
)
def test_only_an_http_401_counts_as_rejected_credentials(exc, expected):
    assert _is_auth_failure(exc) is expected
