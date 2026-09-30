#!/usr/bin/env python3
#
# tests/test_fritzbox_tls_detection.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import configparser
from unittest.mock import Mock

import httpx
import pytest
from requests.exceptions import ConnectionError as RequestsConnectionError

from app.classes.fritzbox.config import FritzBoxConfig
from app.classes.fritzbox.handler import FritzBoxHandler, FritzBoxLuaHandler


def make_config():
    parser = configparser.ConfigParser()
    parser["fritzbox"] = {
        "hostname": "fritz.box",
        "username": "user",
        "password": "password",
    }
    return FritzBoxConfig(parser)


def test_removed_tls_settings_do_not_override_detection(monkeypatch):
    monkeypatch.setenv("FRITZBOX_TLS_ENABLED", "false")
    monkeypatch.setenv("FRITZBOX_VERIFY_TLS", "true")

    config = make_config()

    assert config.tls_enabled is None
    assert not hasattr(config, "verify_tls")


@pytest.mark.parametrize("https_available", [True, False])
def test_tr069_detects_available_protocol(monkeypatch, https_available):
    config = make_config()
    attempts = []
    session = Mock()
    session.call_action.side_effect = lambda service, action: (
        {"NewModelName": "FRITZ!Box", "NewSoftwareVersion": "7.50", "NewSerialNumber": "123"}
        if service == "DeviceInfo" else {}
    )

    def create_connection(**kwargs):
        attempts.append((kwargs["use_tls"], kwargs["port"]))
        if kwargs["use_tls"] and not https_available:
            raise RequestsConnectionError("HTTPS port unavailable")
        return session

    monkeypatch.setattr("app.classes.fritzbox.handler.FritzConnection", create_connection)
    handler = FritzBoxHandler(config)

    handler.connect()

    assert handler.init_successful
    assert config.tls_enabled is https_available
    assert attempts == ([(True, 49443)] if https_available else [(True, 49443), (False, 49000)])


@pytest.mark.parametrize("https_available", [True, False])
def test_lua_detects_protocol_without_certificate_verification(monkeypatch, https_available):
    config = make_config()
    urls = []
    client_options = []

    class Client:
        def __init__(self, **kwargs):
            client_options.append(kwargs)

        def close(self):
            pass

        def get(self, url, *, params=None):
            urls.append(url)
            if url.startswith("https:") and not https_available:
                raise httpx.ConnectError("HTTPS unavailable")
            if params is None:
                return Mock(content=b"<SessionInfo><SID>0000000000000000</SID><Challenge>test</Challenge></SessionInfo>")
            return Mock(content=b"<SessionInfo><SID>1234567890abcdef</SID></SessionInfo>")

    monkeypatch.setattr("app.classes.fritzbox.handler.httpx.Client", Client)
    monkeypatch.setattr(FritzBoxLuaHandler, "_legacy_login_response", lambda *_: "response")
    handler = FritzBoxLuaHandler(config)

    handler.connect()

    assert handler.init_successful
    assert all(options["verify"] is False for options in client_options)
    assert urls[0] == "https://fritz.box/login_sid.lua"
    assert handler.url == ("https://fritz.box" if https_available else "http://fritz.box")
    assert config.tls_enabled is https_available
