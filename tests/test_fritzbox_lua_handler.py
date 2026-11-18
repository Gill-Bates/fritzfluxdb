#!/usr/bin/env python3
#
# tests/test_fritzbox_lua_handler.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import configparser
import logging
import threading
from unittest.mock import Mock

import httpx
import pytest

from app.fritzbox.config import FritzBoxConfig
from app.fritzbox.handler import FritzBoxLuaHandler


def mock_client(respond):
    return httpx.Client(transport=httpx.MockTransport(respond), trust_env=False)


def make_handler():
    parser = configparser.ConfigParser()
    parser["fritzbox"] = {"hostname": "fritz.box", "username": "user", "password": "password"}
    handler = FritzBoxLuaHandler(FritzBoxConfig(parser))
    handler.sid = "1234567890abcdef"
    handler.url = "https://fritz.box"
    return handler


@pytest.mark.parametrize(
    ("challenge", "password", "expected"),
    [
        # example from the AVM login documentation (legacy MD5 challenge)
        ("1234567z", "äbc", "1234567z-9e224a41eeefa284df7bb0f26c2913e2"),
        (
            "2$10000$5A1711$2000$2c3cf9d53a6e1a3a3d4a4a5b6c7d8e9f",
            "password",
            "2c3cf9d53a6e1a3a3d4a4a5b6c7d8e9f$358104c5884bdee6304bd32f1b0125b4933b65ae4d8f17af6c738f17d19bb392",
        ),
    ],
)
def test_login_response_vectors(challenge, password, expected):
    assert FritzBoxLuaHandler._login_response(challenge, password) == expected


@pytest.mark.parametrize(
    "challenge",
    [
        "2$10000$5A1711$2000",             # too few fields
        "2$10000$5A1711$2000$ab$cd",       # too many fields
        "2$ten$5A1711$2000$ab",            # non-numeric iterations
        "2$0$5A1711$2000$ab",              # zero iterations
        "2$99999999$5A1711$2000$ab",       # iterations above the sanity cap
        "2$10000$zz$2000$ab",              # salt is not hex
        "2$10000$$2000$ab",                # empty salt
        "3$10000$5A1711$2000$ab",          # unknown format version
        "1234$5678",                       # '$' without a known prefix
    ],
)
def test_invalid_login_challenge_is_rejected(challenge):
    assert FritzBoxLuaHandler._login_response(challenge, "password") is None


def test_connect_stops_on_an_invalid_challenge(monkeypatch):
    handler = make_handler()
    handler.sid = None
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200, content=b"<SessionInfo><SID>0000000000000000</SID><Challenge>2$bad</Challenge></SessionInfo>"
        )

    session = mock_client(respond)
    monkeypatch.setattr(handler, "_build_session", lambda: setattr(handler, "session", session))
    handler.connect()
    assert handler.sid is None
    assert len(calls) == 1


@pytest.mark.parametrize("status_code", [200, 404, 500])
def test_only_http_200_reaches_the_response_parser(status_code):
    handler = make_handler()
    service = Mock(method="GET", url_path="/data.lua", response_parser=Mock(return_value={"ok": True}))
    service.name = "TestService"
    handler.session = mock_client(lambda request: httpx.Response(status_code, content=b"{}"))
    result = handler.request(service, {})

    if status_code == 200:
        assert result == {"ok": True}
    else:
        assert result is None
        service.response_parser.assert_not_called()


def test_concurrent_expired_sessions_trigger_exactly_one_login():
    handler = make_handler()
    stale_sid = handler.sid
    logins = []
    barrier = threading.Barrier(2)

    def fake_connect_locked():
        logins.append(1)
        handler.sid = "fresh-session-id"

    handler._connect_locked = fake_connect_locked

    def respond(request):
        sid = request.url.params.get("sid")
        if sid == stale_sid:
            barrier.wait(timeout=5)  # both threads hold the stale sid before either reacts
            return httpx.Response(403, content=b"")
        return httpx.Response(200, content=b"{}")
    handler.session = mock_client(respond)
    service = Mock(method="GET", url_path="/data.lua", response_parser=Mock(return_value={"ok": True}))
    service.name = "TestService"
    results = []

    threads = [
        threading.Thread(target=lambda: results.append(handler.request(service, {})))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert len(logins) == 1
    assert results == [{"ok": True}, {"ok": True}]


def test_failed_login_pauses_further_login_attempts():
    handler = make_handler()
    handler.sid = None
    attempts = []
    handler._connect_locked = lambda: attempts.append(1)

    handler.connect()
    handler.connect()

    assert len(attempts) == 1


def test_wrong_credentials_are_flagged_as_permanent(monkeypatch):
    handler = make_handler()
    handler.sid = None
    replies = iter([
        b"<SessionInfo><SID>0000000000000000</SID><Challenge>abcd1234</Challenge></SessionInfo>",
        b"<SessionInfo><SID>0000000000000000</SID><BlockTime>0</BlockTime></SessionInfo>",
    ])
    session = mock_client(lambda request: httpx.Response(200, content=next(replies)))
    monkeypatch.setattr(handler, "_build_session", lambda: setattr(handler, "session", session))

    handler.connect()

    assert handler.auth_failed is True
    assert handler.sid is None


SECRET_VALUES = ("+49 170 1234567", "Max Mustermann", "10.20.30.40")


@pytest.mark.parametrize("secret", SECRET_VALUES)
def test_conversion_and_mismatch_logs_never_contain_the_raw_value(caplog, secret):
    from types import SimpleNamespace
    caplog.set_level("DEBUG", logger="fritzFlux")
    handler = make_handler()
    service = SimpleNamespace(is_event=False)
    results = []
    handler.extract_value(service, {"v": secret}, "m_int", {"data_path": "v", "type": int}, None, results)
    handler.extract_value(service, {"v": secret}, "m_list", {"data_path": "v", "type": list}, None, results)
    handler.extract_value(service, {"v": secret}, "m_unknown", {"data_path": "v", "type": bytes}, None, results)
    handler.extract_value(service, {"v": (secret,)}, "m_tuple", {"data_path": "v", "type": tuple}, None, results)
    assert results == []
    assert "Unknown metric" in caplog.text
    assert secret not in caplog.text
    assert "m_int" in caplog.text


def test_measurement_conversion_error_does_not_log_the_value(caplog):
    from app.utils.measurement import FritzMeasurement
    FritzMeasurement("m", "+49 170 1234567", data_type=int)
    FritzMeasurement("m", ["10.20.30.40"])
    assert "1234567" not in caplog.text and "10.20.30.40" not in caplog.text
    assert "Unable to convert measurement 'm' using int: ValueError" in caplog.text


def _request_failing_with_closed_socket(handler):
    def fail(request):
        raise httpx.ReadError("[Errno 9] Bad file descriptor")

    handler.session = mock_client(fail)
    service = Mock(method="POST", url_path="/data.lua", available=True)
    service.name = "VPN Users - WireGuard"
    service.params = {}
    return service


@pytest.mark.parametrize("shutting_down", [True, False])
def test_transport_failure_of_a_request_is_debug_only(caplog, shutting_down):
    caplog.set_level("DEBUG", logger="fritzFlux")
    handler = make_handler()
    handler.shutting_down = shutting_down
    service = _request_failing_with_closed_socket(handler)

    assert handler.request(service, additional_params={}) is None

    records = [r for r in caplog.records if "Unable to perform request" in r.getMessage()]
    assert [r.levelname for r in records] == ["DEBUG"]
    assert service.transient_failure is not None


def _lua_service(name="System logs"):
    from app.fritzbox.service_handler import FritzBoxLuaService

    return FritzBoxLuaService({"name": name, "interval": 60, "os_min_versions": "7.39", "value_instances": {}})


def _failing_handler(monkeypatch, outcome):
    """Handler whose Lua requests fail transiently while outcome['ok'] is False."""
    handler = make_handler()
    handler.discovery_done = True

    def request(service, additional_params, **_kwargs):
        if outcome["ok"]:
            return {}
        service.transient_failure = "ReadError: boom"
        return None

    monkeypatch.setattr(handler, "request", request)
    monkeypatch.setattr(handler, "extract_service_values", lambda *a, **k: None)
    return handler


def _run(handler, service):
    service._next_request_monotonic = None
    return handler._query_service_with_backoff(service)


def test_transient_failures_report_only_after_threshold_then_recovery(caplog, monkeypatch):
    from app.fritzbox import handler as handler_module
    from app.fritzbox.service_definitions import helpers

    helpers._last_warning.clear()
    caplog.set_level("DEBUG", logger="fritzFlux")
    outcome = {"ok": False}
    handler = _failing_handler(monkeypatch, outcome)
    service = _lua_service()
    threshold = handler_module.SERVICE_FAILURES_BEFORE_REPORT

    for _ in range(threshold - 1):
        assert _run(handler, service) is False
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]
    assert any("no data returned" in r.getMessage() and r.levelname == "DEBUG" for r in caplog.records)

    for _ in range(3):
        assert _run(handler, service) is False
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "failed 3 consecutive" in warnings[0].getMessage()
    assert "sid" not in warnings[0].getMessage()

    outcome["ok"] = True
    caplog.clear()
    assert _run(handler, service) is True
    assert _run(handler, service) is True
    infos = [r for r in caplog.records if r.levelno >= logging.INFO]
    assert [r.levelname for r in infos] == ["INFO"]
    assert "recovered after" in infos[0].getMessage()


def test_recovery_before_threshold_logs_nothing(caplog, monkeypatch):
    caplog.set_level("DEBUG", logger="fritzFlux")
    outcome = {"ok": False}
    handler = _failing_handler(monkeypatch, outcome)
    service = _lua_service()

    _run(handler, service)
    outcome["ok"] = True
    _run(handler, service)

    assert not [r for r in caplog.records if r.levelno >= logging.INFO]


def test_failure_during_shutdown_is_never_reported(caplog, monkeypatch):
    caplog.set_level("DEBUG", logger="fritzFlux")
    handler = _failing_handler(monkeypatch, {"ok": False})
    handler.shutting_down = True
    service = _lua_service()

    for _ in range(5):
        _run(handler, service)

    assert not [r for r in caplog.records if r.levelno >= logging.INFO]


def test_non_transient_failure_still_logs_error(caplog):
    caplog.set_level("DEBUG", logger="fritzFlux")
    handler = make_handler()
    handler.discovery_done = True
    handler.session = mock_client(lambda request: httpx.Response(500))
    service = _lua_service()
    service.method, service.url_path, service.params = "POST", "/data.lua", {}

    assert handler.query_service_data(service) is False

    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("returned HTTP 500" in m for m in errors)
    assert any("no data returned" in m for m in errors)


def test_repeated_parser_failure_is_logged_once_per_interval(caplog):
    caplog.set_level("DEBUG", logger="fritzFlux")
    handler = make_handler()
    handler.session = mock_client(lambda request: httpx.Response(200, content=b"{}"))
    service = Mock(method="POST", url_path="/data.lua", available=True)
    service.name = "Telephone - Call List"
    service.params = {}

    def bad_parser(_response):
        raise ValueError("unsupported call log header")

    service.response_parser = bad_parser

    for _ in range(5):
        assert handler.request(service, additional_params={}) is None

    errors = [r for r in caplog.records if r.levelname == "ERROR" and "unsupported call log header" in r.getMessage()]
    assert len(errors) == 1


def test_changing_parser_message_stays_throttled_under_one_key(caplog):
    caplog.set_level("DEBUG", logger="fritzFlux")
    handler = make_handler()
    handler.session = mock_client(lambda request: httpx.Response(200, content=b"{}"))
    service = Mock(method="POST", url_path="/data.lua", available=True)
    service.name = "Telephone - Call List"
    service.params = {}
    calls = iter(range(5))

    def bad_parser(_response):
        raise ValueError(f"row {next(calls)} broken")

    service.response_parser = bad_parser
    for _ in range(5):
        assert handler.request(service, additional_params={}) is None
    errors = [r for r in caplog.records if r.levelname == "ERROR" and "request parsing" in r.getMessage()]
    assert len(errors) == 1
    assert len(handler._transport_error_log_state) == 1


def test_request_after_close_returns_none_without_touching_the_session():
    handler = make_handler()
    handler.session = None
    service = Mock(method="POST", url_path="/data.lua")

    assert handler.request(service, additional_params={}) is None


LOGIN_CHALLENGE = b"<SessionInfo><SID>0000000000000000</SID><Challenge>abcd1234</Challenge></SessionInfo>"
GOOD_SID = "0123456789abcdef"


def _login_with(handler, monkeypatch, *replies):
    replies = iter(replies)
    seen = []

    def respond(request):
        seen.append(request)
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(200, content=reply)

    session = mock_client(respond)
    monkeypatch.setattr(handler, "_build_session", lambda: setattr(handler, "session", session))
    handler.sid = None
    handler.connect()
    return seen


def _session_info(sid):
    body = f"<SessionInfo><SID>{sid}</SID><BlockTime>0</BlockTime></SessionInfo>" if sid is not None else (
        "<SessionInfo><BlockTime>0</BlockTime></SessionInfo>"
    )
    return body.encode()


def test_lua_client_ignores_proxy_environment(monkeypatch):
    import http.server

    class Box(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            self.send_response(200)
            self.end_headers()
            self.wfile.write(LOGIN_CHALLENGE)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Box)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # The proxy points at a closed port: a request that honored it would fail.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    handler = make_handler()
    try:
        handler._build_session()
        root = handler._login_document(f"http://127.0.0.1:{server.server_port}/login_sid.lua")
        assert root.findtext("./Challenge") == "abcd1234"
    finally:
        handler.close()
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"<SessionInfo>" + b"x" * (70 * 1024) + b"</SessionInfo>", "larger than"),
        (b"<SessionInfo><SID>", "malformed"),
        (b'<!DOCTYPE x [<!ENTITY a "b">]><SessionInfo><SID>&a;</SID></SessionInfo>', "forbidden"),
    ],
)
def test_unsafe_login_documents_are_rejected(monkeypatch, caplog, body, reason):
    handler = make_handler()
    _login_with(handler, monkeypatch, body, b"")
    assert handler.sid is None
    assert handler.init_successful is not True
    assert reason in caplog.text


def test_normal_login_sets_the_session(monkeypatch):
    handler = make_handler()
    seen = _login_with(handler, monkeypatch, LOGIN_CHALLENGE, _session_info(GOOD_SID))
    assert handler.sid == GOOD_SID
    assert handler.init_successful is True
    assert len(seen) == 2


def test_http_fallback_login_succeeds_and_is_bounded(monkeypatch):
    handler = make_handler()
    seen = _login_with(
        handler, monkeypatch, httpx.ConnectError("refused"), LOGIN_CHALLENGE, _session_info(GOOD_SID)
    )
    assert [r.url.scheme for r in seen] == ["https", "http", "http"]
    assert handler.sid == GOOD_SID
    handler = make_handler()
    seen = _login_with(handler, monkeypatch, httpx.ConnectError("refused"), b"<SessionInfo>" + b"x" * 70000)
    assert [r.url.scheme for r in seen] == ["https", "http"]
    assert handler.sid is None


@pytest.mark.parametrize("reply", [httpx.ConnectError("refused"), LOGIN_CHALLENGE])
def test_shutdown_during_the_login_sends_no_further_request(monkeypatch, caplog, reply):
    handler = make_handler()
    seen = []

    def respond(request):
        seen.append(request)
        handler.shutting_down = True
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(200, content=reply)

    session = mock_client(respond)
    monkeypatch.setattr(handler, "_build_session", lambda: setattr(handler, "session", session))
    handler.sid = None
    with caplog.at_level(logging.DEBUG):
        handler.connect()
    assert len(seen) == 1
    assert handler.sid is None
    assert handler._rejected_attempts == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("sid", ["0" * 15, "0123456789abcdeg", "0123456789abcdef0", None, ""])
def test_malformed_session_id_is_not_accepted(monkeypatch, sid):
    handler = make_handler()
    _login_with(handler, monkeypatch, LOGIN_CHALLENGE, _session_info(sid))
    assert handler.sid is None
    assert handler.init_successful is not True
    assert handler.auth_failed is False


def test_unexpected_session_id_is_not_logged(monkeypatch, caplog):
    handler = make_handler()
    with caplog.at_level(logging.DEBUG):
        _login_with(handler, monkeypatch, _session_info("0123456789abcdef"))
    assert handler.sid is None
    assert "0123456789abcdef" not in caplog.text
    assert "Unexpected" in caplog.text


@pytest.mark.parametrize(("block", "expected"), [
    ("120", 120.0), ("inf", 0.0), ("nan", 0.0), ("-5", 0.0), ("1e999", 0.0),
    ("999999", 3600.0), ("abc\nFORGED", 0.0),
])
def test_block_time_is_validated_and_capped(monkeypatch, caplog, block, expected):
    handler = make_handler()
    body = f"<SessionInfo><SID>{'0' * 16}</SID><BlockTime>{block}</BlockTime></SessionInfo>".encode()
    with caplog.at_level(logging.DEBUG):
        _login_with(handler, monkeypatch, LOGIN_CHALLENGE, body)
    assert handler._block_seconds == expected
    assert "FORGED" not in caplog.text
    assert "inf" not in caplog.text.replace("info", "")


def test_null_session_id_is_a_credentials_rejection(monkeypatch):
    handler = make_handler()
    _login_with(handler, monkeypatch, LOGIN_CHALLENGE, _session_info("0" * 16))
    assert handler.sid is None
    assert handler.auth_failed is True


def test_oversized_data_response_is_rejected_without_reaching_the_parser(monkeypatch, caplog):
    import app.fritzbox.handler as module

    monkeypatch.setattr(module, "MAX_LUA_DATA_BYTES", 1024)
    handler = make_handler()
    handler.session = mock_client(lambda request: httpx.Response(200, content=b"x" * 2048))
    service = Mock(method="GET", url_path="/data.lua", response_parser=Mock())
    service.name = "TestService"
    assert handler.request(service, {}) is None
    service.response_parser.assert_not_called()
    assert "larger than 1024 bytes" in caplog.text


def test_compressed_bomb_is_limited_by_the_decoded_size(monkeypatch):
    import gzip

    import app.fritzbox.handler as module

    monkeypatch.setattr(module, "MAX_LUA_DATA_BYTES", 1024)
    handler = make_handler()
    packed = gzip.compress(b"0" * 100_000)
    handler.session = mock_client(
        lambda request: httpx.Response(200, content=packed, headers={"content-encoding": "gzip"})
    )
    service = Mock(method="GET", url_path="/data.lua", response_parser=Mock())
    service.name = "TestService"
    assert handler.request(service, {}) is None
    service.response_parser.assert_not_called()


def test_decoded_response_reaches_the_parser_intact():
    import gzip

    handler = make_handler()
    packed = gzip.compress(b'{"ok": true}')
    handler.session = mock_client(
        lambda request: httpx.Response(200, content=packed, headers={"content-encoding": "gzip"})
    )
    service = Mock(method="GET", url_path="/data.lua", response_parser=lambda response: response.json())
    service.name = "TestService"
    assert handler.request(service, {}) == {"ok": True}


def test_service_parameters_cannot_override_the_session_id():
    handler = make_handler()
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, content=b"{}")

    handler.session = mock_client(respond)
    service = Mock(method="GET", url_path="/data.lua", response_parser=Mock(return_value={}))
    service.name = "TestService"
    handler.request(service, {"sid": "ffffffffffffffff", "page": "x"})
    assert seen[0].url.params["sid"] == "1234567890abcdef"
    assert seen[0].url.params["page"] == "x"
