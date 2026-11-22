#!/usr/bin/env python3
#
# tests/test_preflight.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Startup preflight: rejected credentials and missing targets end the daemon with EX_CONFIG (78)."""
import asyncio
import configparser
import logging
import os
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from app import main as app_main
from app.fritzbox.config import FritzBoxConfig
from app.fritzbox.handler import FritzBoxHandler
from app.influxdb.handler import DatabaseConfigError, InfluxHandler
from app.utils.cli_parser import parse_command_line
from app.utils.measurement import FritzMeasurement
from tests.fakebox import (
    FakeFritzBox,
    attach_to_handler,
    box_for_services,
    deny_all_actions,
    fail_scheme,
)

REAL_CLIENT = httpx.AsyncClient
BUILD = {"dataset": [["Build Information: QuestDB 10.2.0, JDK 25, Commit Hash abc"]]}


@pytest.mark.parametrize(
    ("arguments", "log_level_source"),
    [
        (["--log-level", "INVALID"], "cli"),
        ([], "environment"),
        (["--log-level", "INVALID", "-v"], "cli"),
        (["-v"], "environment"),
    ],
)
def test_invalid_log_level_is_a_permanent_configuration_error(
    monkeypatch, capsys, arguments, log_level_source
):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    if log_level_source == "environment":
        monkeypatch.setenv("LOG_LEVEL", "INVALID")

    parsed_args = parse_command_line("1.0.0", "app", "date", "url", "config.ini", arguments)
    monkeypatch.setattr(app_main, "parse_command_line", lambda *_args: parsed_args)

    with pytest.raises(SystemExit) as error:
        asyncio.run(app_main.main_async())

    assert error.value.code == 78
    assert "Invalid log level: INVALID" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("arguments", "env_value"),
    [([], ""), (["--log-level", ""], None), (["--log-level", ""], "DEBUG")],
)
def test_explicitly_empty_log_level_is_a_permanent_configuration_error(
    monkeypatch, capsys, arguments, env_value
):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    if env_value is not None:
        monkeypatch.setenv("LOG_LEVEL", env_value)

    parsed_args = parse_command_line("1.0.0", "app", "date", "url", "config.ini", arguments)
    monkeypatch.setattr(app_main, "parse_command_line", lambda *_args: parsed_args)

    with pytest.raises(SystemExit) as error:
        asyncio.run(app_main.main_async())

    assert error.value.code == 78
    assert "log level undefined or empty" in capsys.readouterr().err


def test_verbose_mode_overrides_a_valid_configured_log_level(monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    parsed_args = parse_command_line(
        "1.0.0", "app", "date", "url", "config.ini", ["--log-level", "WARNING", "-v"]
    )
    logger = Mock()
    setup = Mock(return_value=logger)
    monkeypatch.setattr(app_main, "parse_command_line", lambda *_args: parsed_args)
    monkeypatch.setattr(app_main, "setup_logging", setup)
    monkeypatch.setattr(app_main, "import_config", Mock(side_effect=RuntimeError("stop")))

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(app_main.main_async())

    assert setup.call_args.args[0] == "WARNING"
    logger.setLevel.assert_called_once_with("DEBUG")


@pytest.fixture
def make_handler(monkeypatch, no_database_env):
    def build(db_type, respond):
        monkeypatch.setenv("DB_TYPE", db_type)
        monkeypatch.setenv("INFLUXDB_HOSTNAME", "db.invalid")
        monkeypatch.setenv("QUESTDB_HOSTNAME", "db.invalid")
        monkeypatch.setenv("INFLUXDB_DATABASE", "fritz")
        monkeypatch.setenv("INFLUXDB_ORGANIZATION", "org")
        monkeypatch.setenv("INFLUXDB_BUCKET", "bucket")
        monkeypatch.setenv("INFLUXDB_TOKEN", "token")
        # db.invalid is a remote plain-HTTP host: the app's credential gate needs this opt-in.
        monkeypatch.setenv("INFLUXDB_ALLOW_PLAINTEXT_CREDENTIALS", "true")

        def client(*args, **kwargs):
            return REAL_CLIENT(*args, **{**kwargs, "transport": httpx.MockTransport(respond)})

        monkeypatch.setattr(httpx, "AsyncClient", client)
        return InfluxHandler(configparser.ConfigParser())

    return build


@pytest.fixture(autouse=True)
def _no_real_fritzbox(monkeypatch):
    # Unit tests must never reach a real box; tests that need a session patch this themselves.
    def refuse(*args, **kwargs):
        raise AssertionError(f"unexpected real TR064Client to {args[:2]!r}")

    monkeypatch.setattr("app.fritzbox.handler.TR064Client", refuse)


def _status(code):
    return lambda request: httpx.Response(code, json=BUILD) if code == 200 else httpx.Response(code, text="nope")


@pytest.mark.parametrize("db_type", ["influxdb_v1", "influxdb_v2", "questdb"])
@pytest.mark.parametrize("code", [401, 403])
def test_preflight_rejects_credentials(make_handler, db_type, code):
    handler = make_handler(db_type, _status(code))
    with pytest.raises(DatabaseConfigError, match="rejected the configured credentials"):
        asyncio.run(handler.preflight())


@pytest.mark.parametrize("db_type", ["influxdb_v1", "influxdb_v2"])
def test_preflight_missing_influx_target_is_a_config_error(make_handler, db_type):
    handler = make_handler(db_type, _status(404))
    with pytest.raises(DatabaseConfigError, match="not found"):
        asyncio.run(handler.preflight())


@pytest.mark.parametrize("db_type", ["influxdb_v1", "influxdb_v2", "questdb"])
def test_preflight_unreachable_server_only_warns(make_handler, db_type, caplog):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    handler = make_handler(db_type, refuse)
    with caplog.at_level(logging.WARNING):
        asyncio.run(handler.preflight())
    assert "preflight postponed" in caplog.text


@pytest.mark.parametrize(("db_type", "ok"), [("influxdb_v1", 204), ("influxdb_v2", 204), ("questdb", 200)])
def test_preflight_passes_with_valid_settings(make_handler, db_type, ok, caplog):
    handler = make_handler(db_type, _status(ok))
    with caplog.at_level(logging.INFO):
        asyncio.run(handler.preflight())
    assert "preflight passed" in caplog.text


@pytest.mark.parametrize(
    ("plaintext_ok", "tls", "verify", "allow_insecure"),
    [(True, False, True, True), (False, True, False, True), (False, True, True, False)],
)
def test_allow_insecure_follows_the_users_deliberate_options(make_handler, plaintext_ok, tls, verify, allow_insecure):
    handler = make_handler("influxdb_v2", _status(204))
    handler.config = handler.config.model_copy(
        update={"allow_plaintext_credentials": plaintext_ok, "tls_enabled": tls, "verify_tls": verify}
    )
    client = handler._new_influx_client()
    assert client.config.allow_insecure is allow_insecure
    assert client.config.https is tls and client.config.verify_certificate is verify
    assert client.config.token == "token" and client.config.timeout == handler.connection_timeout_v2


def test_credentials_over_plain_http_without_the_opt_in_are_a_config_error(make_handler):
    handler = make_handler("influxdb_v2", _status(204))
    handler.config = handler.config.model_copy(update={"allow_plaintext_credentials": False})
    with pytest.raises(DatabaseConfigError, match="plain HTTP"):
        handler._new_influx_client()


def test_influx_probe_sends_an_empty_write(make_handler):
    seen = []

    def respond(request):
        seen.append((request.url.path, request.content))
        return httpx.Response(204)

    asyncio.run(make_handler("influxdb_v2", respond).preflight())
    assert seen == [("/api/v2/write", b"")]


async def _write_once(handler):
    # The client is created inside the loop and closed on every path, also when the write raises.
    if handler.version == "questdb":
        handler._questdb = handler.client = handler._new_questdb_client()
    else:
        handler.client = handler._new_influx_client()
    try:
        await handler.write_data(force=True)
    finally:
        await handler.close()


@pytest.mark.parametrize(("db_type", "code"), [("influxdb_v1", 401), ("influxdb_v2", 403), ("influxdb_v2", 404),
                                                ("questdb", 401)])
def test_runtime_rejection_raises_without_traceback(make_handler, db_type, code, caplog):
    handler = make_handler(db_type, _status(code))
    handler.buffer = [FritzMeasurement("uptime", 1)]
    handler.questdb_schema_ensured = True
    with caplog.at_level(logging.ERROR), pytest.raises(DatabaseConfigError):
        asyncio.run(_write_once(handler))
    assert not any(record.exc_info for record in caplog.records)


def test_reconnect_with_rejected_credentials_closes_the_client(make_handler, monkeypatch):
    handler = make_handler("influxdb_v1", _status(401))
    closed = []
    orig = REAL_CLIENT.aclose

    async def tracking(self):
        closed.append(self)
        await orig(self)

    monkeypatch.setattr(REAL_CLIENT, "aclose", tracking)
    with pytest.raises(DatabaseConfigError):
        asyncio.run(handler._init_client())
    assert handler.client is None
    assert len(closed) == 1


# FRITZ!Box with TR-064 switched off

def _fritzbox_config():
    parser = configparser.ConfigParser()
    parser["fritzbox"] = {"hostname": "fritz.box", "username": "user", "password": "password"}
    return FritzBoxConfig(parser)


def _disabled_box() -> FakeFritzBox:
    """Box with 'Allow access for applications' off: tr64desc.xml answers 404."""
    box = FakeFritzBox()
    box.override("/tr64desc.xml", "<html>not found</html>", status=404, content_type="text/html")
    return box


def _tls_attempts(box: FakeFritzBox) -> list[bool]:
    return list(dict.fromkeys(request.url.scheme == "https" for request in box.requests))


@pytest.mark.parametrize("https_available", [True, False])
def test_disabled_tr064_access_is_flagged_in_both_protocol_attempts(monkeypatch, caplog, https_available):
    box = _disabled_box()
    if not https_available:
        fail_scheme(box)
    created = attach_to_handler(monkeypatch, box)
    handler = FritzBoxHandler(_fritzbox_config())
    with caplog.at_level(logging.ERROR):
        handler.connect()
    assert handler.access_disabled is True
    assert handler.auth_failed is False
    assert "Allow access for applications" in caplog.text
    assert _tls_attempts(box) == ([True] if https_available else [True, False])
    assert all(client.closed for client in created)
    assert handler.session is None


def test_unreachable_box_is_not_a_disabled_access(monkeypatch):
    box = FakeFritzBox()
    box.raise_transport(httpx.ConnectError)
    attach_to_handler(monkeypatch, box)
    handler = FritzBoxHandler(_fritzbox_config())
    handler.connect()
    assert handler.access_disabled is False
    assert handler.init_successful is False


def test_https_failure_without_shutdown_still_falls_back_to_http(monkeypatch):
    box = FakeFritzBox()
    fail_scheme(box)
    attach_to_handler(monkeypatch, box)
    handler = FritzBoxHandler(_fritzbox_config())
    handler.connect()
    assert _tls_attempts(box) == [True, False]
    assert handler.init_successful is True
    assert handler.config.tls_enabled is False


def test_shutdown_during_the_https_attempt_skips_the_http_fallback(monkeypatch, caplog):
    box = FakeFritzBox()
    created = attach_to_handler(monkeypatch, box)
    handler = FritzBoxHandler(_fritzbox_config())

    def signal_and_fail(request):
        handler.shutting_down = True
        raise httpx.ConnectError("simulated connection failure", request=request)

    box.hook = signal_and_fail
    with caplog.at_level(logging.DEBUG):
        handler.connect()
    assert _tls_attempts(box) == [True]
    assert all(client.closed for client in created)
    assert handler.session is None
    assert handler.init_successful is False
    assert handler._rejected_attempts == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_shutdown_request_aborts_the_login_chain_before_the_next_document(monkeypatch, caplog):
    box = FakeFritzBox()
    created = attach_to_handler(monkeypatch, box)
    handler = FritzBoxHandler(_fritzbox_config())
    box.hook = lambda request: handler.request_shutdown()
    with caplog.at_level(logging.DEBUG):
        handler.connect()
    assert len(box.requests) == 1
    assert all(client.closed for client in created)
    assert handler.session is None
    assert handler.init_successful is False
    assert handler._rejected_attempts == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# main() exit codes

class _Closable:
    def __init__(self, *args, **kwargs):
        self.config = SimpleNamespace(parser_error=False, hostname="h")
        self.name = "fake"
        self.init_successful = False
        self.auth_failed = False
        self.access_disabled = False

    def close(self):
        pass

    def connect(self):
        pass


class _FakeDb(_Closable):
    preflight_error: Exception | None = None
    version = 1

    async def preflight(self):
        if self.preflight_error:
            raise self.preflight_error

    async def close(self):
        pass


class _FakeBox(_Closable):
    disabled = False
    rejected = False

    def connect(self):
        self.access_disabled = self.disabled
        self.auth_failed = self.rejected


def _running_init(self, *args, **kwargs):
    self.config = SimpleNamespace(parser_error=False, hostname="h", fw_version="7.50", serial_number=None,
                                  box_tag="t", model="m", link_type="l", wan_watch_interval=0)
    self.name = "fake"
    self.init_successful = True
    self.auth_failed = False
    self.access_disabled = False


@pytest.fixture
def run_main(monkeypatch):
    def run(*, preflight_error=None, disabled=False, rejected=False, running=False):
        db = type("Db", (_FakeDb,), {"preflight_error": preflight_error})
        box = type("Box", (_FakeBox,), {"disabled": disabled, "rejected": rejected})
        monkeypatch.setattr(app_main, "parse_command_line",
                            lambda *a: SimpleNamespace(verbose=0, log_level="INFO", config_file="x"))
        monkeypatch.setattr(app_main, "setup_logging", lambda *a: logging.getLogger("test"))
        monkeypatch.setattr(app_main, "import_config", lambda *a: configparser.ConfigParser())
        monkeypatch.setattr(app_main, "InfluxHandler", db)
        monkeypatch.setattr(app_main, "FritzBoxHandler", box)
        monkeypatch.setattr(app_main, "FritzBoxLuaHandler", _Closable)
        monkeypatch.setattr(app_main, "InfluxLogAndConfigWriter", _Closable)
        if running:
            monkeypatch.setattr(_Closable, "__init__", _running_init)
        return asyncio.run(app_main.main_async())

    return run


def test_main_exits_78_when_the_database_rejects_the_settings(run_main):
    assert run_main(preflight_error=DatabaseConfigError("rejected")) == 78
    assert run_main(preflight_error=DatabaseConfigError("QuestDB 9.3.2 is not supported; version 10 or newer is required")) == 78


def test_main_exits_78_when_tr064_is_disabled(run_main):
    assert run_main(disabled=True) == 78


def test_main_exits_78_and_logs_when_the_box_rejects_the_credentials(run_main, caplog):
    with caplog.at_level(logging.ERROR, logger="test"):
        assert run_main(rejected=True, running=True) == 78
    assert "rejected the configured credentials; not retrying" in caplog.text


def test_preflight_server_error_is_reported_as_unexpected_answer_not_unreachable(make_handler, caplog):
    with caplog.at_level(logging.WARNING):
        asyncio.run(make_handler("influxdb_v1", _status(500)).preflight())
    assert "unexpected answer" in caplog.text
    assert "not reachable" not in caplog.text


def test_questdb_404_on_write_backs_off_instead_of_failing(make_handler):
    handler = make_handler("questdb", _status(404))
    handler.buffer = [FritzMeasurement("uptime", 1)]
    handler.questdb_schema_ensured = True
    asyncio.run(_write_once(handler))
    assert handler.current_retry_interval == handler.max_retry_interval
    assert handler.connection_lost is True


# Unverified HTTPS to the box is the normal case: one notice per path and process

def test_tr064_unverified_tls_logs_no_separate_notice(monkeypatch, caplog):
    attach_to_handler(monkeypatch, FakeFritzBox())
    handler = FritzBoxHandler(_fritzbox_config())
    with caplog.at_level(logging.INFO):
        handler.connect()
    assert handler.init_successful is True
    assert not any("certificate" in r.getMessage() or "TLS unverified" in r.getMessage()
                   for r in caplog.records)
    handler.close()


def test_lua_unverified_tls_warning_is_logged_once_over_reconnects_and_relogins(monkeypatch, caplog):
    from app.fritzbox.handler import FritzBoxLuaHandler

    real_client = httpx.Client
    xml = b"<SessionInfo><SID>0000000000000000</SID><Challenge>1234567z</Challenge></SessionInfo>"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=xml))
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=transport, **kw))
    handler = FritzBoxLuaHandler(_fritzbox_config())
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            handler._login_retry_after = 0.0
            handler.connect()
            handler.close()
    assert sum("TLS unverified" in r.getMessage() for r in caplog.records) == 1


# Rejected login attempts during operation: counted, never timed

REJECTED = b"<SessionInfo><SID>0000000000000000</SID><BlockTime>0</BlockTime></SessionInfo>"
ACCEPTED = b"<SessionInfo><SID>0123456789abcdef</SID><BlockTime>0</BlockTime></SessionInfo>"
CHALLENGE = b"<SessionInfo><SID>0000000000000000</SID><Challenge>1234567z</Challenge></SessionInfo>"


def _lua_handler(monkeypatch, outcomes):
    """Lua handler whose box answers each login with the next entry of `outcomes`."""
    from app.fritzbox.handler import FritzBoxLuaHandler

    attempts = []

    def respond(request):
        if "response" not in request.url.params:
            return httpx.Response(200, content=CHALLENGE)
        attempts.append(1)
        return httpx.Response(200, content=outcomes.pop(0))

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    handler = FritzBoxLuaHandler(_fritzbox_config())
    handler.services = []
    return handler, attempts


def _relogin(handler):
    handler._login_retry_after = 0.0
    handler.connect()
    handler.close()


def _run_task_loop(handler, queue_size=10):
    async def scenario():
        await asyncio.wait_for(handler.task_loop(asyncio.Queue(queue_size)), timeout=5)

    asyncio.run(scenario())


def test_lua_alternating_rejection_and_success_never_ends_the_producer(monkeypatch):
    handler, _ = _lua_handler(monkeypatch, [REJECTED, ACCEPTED, REJECTED])
    for _ in range(3):
        _relogin(handler)
    assert handler.auth_failed is True
    cycles = []

    async def scenario():
        task = asyncio.create_task(handler.task_loop(asyncio.Queue()))
        await asyncio.sleep(0.3)
        cycles.append(task.done())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert cycles == [False]


def test_lua_consecutive_rejections_end_the_producer(monkeypatch):
    from app.fritzbox import handler as fb
    from app.utils.common import PermanentConfigError

    handler, _ = _lua_handler(monkeypatch, [REJECTED] * fb.REJECTED_ATTEMPTS_BEFORE_EXIT)
    for _ in range(fb.REJECTED_ATTEMPTS_BEFORE_EXIT):
        _relogin(handler)
    with pytest.raises(PermanentConfigError):
        _run_task_loop(handler)


def test_lua_cooldown_is_respected_and_only_real_attempts_count(monkeypatch):
    from app.fritzbox import handler as fb

    clock = [1000.0]
    monkeypatch.setattr(fb, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    handler, attempts = _lua_handler(monkeypatch, [REJECTED] * 10)

    for _ in range(fb.REJECTED_ATTEMPTS_BEFORE_EXIT - 1):
        handler.connect()
        handler.connect()  # inside the cooldown: no login happens
        assert handler._permanent_rejection() is None
        clock[0] += fb.LOGIN_RETRY_COOLDOWN
    assert len(attempts) == fb.REJECTED_ATTEMPTS_BEFORE_EXIT - 1

    handler.connect()
    assert len(attempts) == fb.REJECTED_ATTEMPTS_BEFORE_EXIT
    assert handler._permanent_rejection() is not None


BLOCKED = b"<SessionInfo><SID>0000000000000000</SID><BlockTime>120</BlockTime></SessionInfo>"


def test_lua_login_block_neither_counts_nor_breaks_the_rejection_chain(monkeypatch):
    from app.utils.common import PermanentConfigError

    handler, _ = _lua_handler(monkeypatch, [REJECTED, BLOCKED, REJECTED, BLOCKED, REJECTED])
    for _ in range(5):
        _relogin(handler)
    with pytest.raises(PermanentConfigError):
        _run_task_loop(handler)


def test_lua_cooldown_after_a_login_block_lasts_at_least_the_block_time(monkeypatch):
    from app.fritzbox import handler as fb

    clock = [1000.0]
    monkeypatch.setattr(fb, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    handler, attempts = _lua_handler(monkeypatch, [BLOCKED, REJECTED])
    handler.connect()
    assert handler._login_retry_after == 1000.0 + 120
    clock[0] += 119
    handler.connect()
    assert len(attempts) == 1
    clock[0] += 1
    handler.connect()
    assert len(attempts) == 2
    assert handler._login_retry_after == clock[0] + fb.LOGIN_RETRY_COOLDOWN


def test_lua_login_block_at_startup_is_temporary_and_logged_once_per_phase(monkeypatch, caplog):
    handler, _ = _lua_handler(monkeypatch, [BLOCKED, BLOCKED])
    with caplog.at_level(logging.ERROR):
        for _ in range(2):
            handler._login_retry_after = 0.0
            handler.connect()
    assert handler.auth_failed is False
    assert handler._permanent_rejection() is None
    assert handler.init_successful is False
    assert sum("Logins blocked for '120' seconds" in r.getMessage() for r in caplog.records) == 1


def _full_box() -> FakeFritzBox:
    """Box that offers every service the handler queries."""
    return box_for_services(FritzBoxHandler(_fritzbox_config()).services)


def test_tr064_unauthorized_single_actions_never_end_the_producer(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    box = _full_box()
    handler = FritzBoxHandler(_fritzbox_config())
    forbidden = handler.services[0]
    for action in forbidden.actions:
        box.deny("WANCommonIFC1", action.name)
    handler.session = box.tr064_client()
    for _ in range(25):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(handler._query_service_with_backoff, handler.services))
        assert handler._permanent_rejection() is None
    assert handler.auth_failed is False
    served = [name for name, _ in box.calls if name != "WANCommonIFC1"]
    assert len(served) >= 25 * (len(handler.services) - 1)

    handler.services = []
    handler.init_successful = True

    async def scenario():
        task = asyncio.create_task(handler.task_loop(asyncio.Queue()))
        await asyncio.sleep(0.3)
        done = task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return done

    assert asyncio.run(scenario()) is False
    handler.close()


@pytest.mark.parametrize(
    "fault_text",
    ["transport failed with code 820", "HTTP 401 and 820 in the box text"],
)
def test_transport_exception_containing_820_does_not_disable_tr064_action(fault_text):
    box = _full_box()
    handler = FritzBoxHandler(_fritzbox_config())
    service = handler.services[0]
    action = service.actions[0]
    for each in service.actions:
        box.fault("WANCommonIFC1", each.name, 820, fault_text)
    handler.session = box.tr064_client()

    assert handler.query_service_data(service) is False

    assert service.available is True
    assert action.available is True
    handler.session.close()


def _tr064_handler(monkeypatch, box):
    created = attach_to_handler(monkeypatch, box)
    handler = FritzBoxHandler(_fritzbox_config())
    handler.services = []
    return handler, created


class _StopLoop(Exception):
    pass


def _tr064_operating(monkeypatch, box, cycles):
    """Operating TR-064 handler with one always-due service and a fake clock advanced by task_loop's sleep."""
    from app.fritzbox import handler as fb

    clock = [1000.0]
    monkeypatch.setattr(fb, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
        if len(sleeps) >= cycles:
            raise _StopLoop

    monkeypatch.setattr(fb.asyncio, "sleep", fake_sleep)
    handler, created = _tr064_handler(monkeypatch, box)
    service = FritzBoxHandler(_fritzbox_config()).services[0]
    service.should_be_requested = lambda: True
    handler.services = [service]
    handler.discovery_done = True
    handler.init_successful = True
    return handler, created


def _denying_box() -> FakeFritzBox:
    box = _full_box()
    deny_all_actions(box)
    return box


def test_tr064_password_change_during_operation_ends_the_producer_after_counted_reconnects(monkeypatch):
    from app.fritzbox import handler as fb
    from app.utils.common import PermanentConfigError

    box = _denying_box()
    handler, created = _tr064_operating(monkeypatch, box, cycles=500)
    handler.session = box.tr064_client()
    with pytest.raises(PermanentConfigError, match="rejected the configured credentials"):
        _run_task_loop(handler)
    assert len(created) == fb.REJECTED_ATTEMPTS_BEFORE_EXIT
    assert all(client.closed for client in created)


def test_tr064_access_disabled_on_reconnect_is_counted(monkeypatch):
    from app.utils.common import PermanentConfigError

    handler, _ = _tr064_operating(monkeypatch, _disabled_box(), cycles=500)
    handler.session = _denying_box().tr064_client()
    with pytest.raises(PermanentConfigError, match="TR-064 access is disabled"):
        _run_task_loop(handler)


def test_tr064_partly_denied_actions_never_trigger_a_reconnect(monkeypatch):
    box = _full_box()
    handler, created = _tr064_operating(monkeypatch, box, cycles=60)
    second = FritzBoxHandler(_fritzbox_config()).services[1]
    second.should_be_requested = lambda: True
    handler.services.append(second)
    for action in handler.services[0].actions:
        box.deny("WANCommonIFC1", action.name)

    handler.session = box.tr064_client()
    with pytest.raises(_StopLoop):
        _run_task_loop(handler)
    assert created == []
    assert handler.init_successful is True
    assert handler._permanent_rejection() is None
    handler.close()


def test_tr064_denied_actions_with_valid_login_are_classified_as_missing_rights_and_throttled(monkeypatch, caplog):
    from app.fritzbox import handler as fb

    box = _full_box()
    deny_all_actions(box, except_for=("DeviceInfo1", "GetInfo"))
    cycles = int(fb.RIGHTS_PROBE_INTERVAL / 2)
    handler, created = _tr064_operating(monkeypatch, box, cycles=cycles)
    handler.session = box.tr064_client()
    with caplog.at_level(logging.WARNING), pytest.raises(_StopLoop):
        _run_task_loop(handler)
    assert len(created) == 1
    assert handler._permanent_rejection() is None
    assert sum("missing rights" in r.getMessage() for r in caplog.records) == 1
    assert handler.init_successful is True
    handler.close()


def test_tr064_unreachable_box_during_reconnect_is_not_counted_and_retried_with_backoff(monkeypatch):
    unreachable = FakeFritzBox()
    unreachable.raise_transport(httpx.ConnectError)
    handler, created = _tr064_operating(monkeypatch, unreachable, cycles=700)
    handler.session = _denying_box().tr064_client()
    with pytest.raises(_StopLoop):
        _run_task_loop(handler)
    # two protocol attempts per reconnect; delays 60, 120, 240, 300 s keep the retries sparse
    assert 3 <= len(created) / 2 <= 6
    assert handler._rejected_attempts == 0
    assert handler._permanent_rejection() is None
    assert all(client.closed for client in created)


def test_tr064_reconnect_retries_missing_wan_metadata_without_replacing_identity(monkeypatch, caplog):
    box = FakeFritzBox()
    box.respond("DeviceInfo1", "GetInfo", {"NewModelName": "X"})
    handler, created = _tr064_operating(monkeypatch, box, cycles=5)
    previous = FakeFritzBox().tr064_client()
    handler.session = previous
    handler.init_successful = False
    handler.config.model = "Original"
    with caplog.at_level(logging.INFO):
        handler._reconnect()
    assert handler.init_successful is True
    assert handler.config.model == "Original"
    assert box.calls == [
        ("DeviceInfo1", "GetInfo"),
        ("WANCommonIFC1", "GetCommonLinkProperties"),
    ]
    assert handler.session is created[0]
    assert previous.closed is True
    assert not any("Successfully established" in r.getMessage() for r in caplog.records)
    handler.close()


def test_tr064_failed_reconnect_closes_replaced_sessions(monkeypatch):
    box = FakeFritzBox()
    box.fault("DeviceInfo1", "GetInfo", 501, "probe failed")
    created = attach_to_handler(monkeypatch, box)
    handler = FritzBoxHandler(_fritzbox_config())
    previous = FakeFritzBox().tr064_client()
    handler.session = previous
    handler.init_successful = False

    handler._reconnect()

    assert previous.closed is True
    assert created[0].closed is True
    assert handler.session is None

    handler._reconnect_after = 0.0
    handler._reconnect()

    assert len(created) == 2
    assert created[1].closed is True
    assert handler.session is None


def test_tr064_failed_startup_probe_closes_created_session_and_never_publishes_it(monkeypatch):
    box = FakeFritzBox()
    box.deny("DeviceInfo1", "GetInfo")
    created = attach_to_handler(monkeypatch, box)
    handler = FritzBoxHandler(_fritzbox_config())
    visible = []

    def watch(request):
        if request.method == "POST":
            visible.append(handler.session)

    box.hook = watch

    handler.connect()

    assert visible == [None]
    assert created[0].closed is True
    assert handler.session is None
    assert handler.init_successful is False
    assert handler.auth_failed is True


def test_tr064_session_is_published_after_the_probe_with_a_rising_generation(monkeypatch):
    created = attach_to_handler(monkeypatch, FakeFritzBox())
    handler = FritzBoxHandler(_fritzbox_config())
    handler.connect()
    assert handler.session is created[0]
    first = handler.session.generation
    handler.close()
    handler.connect()
    assert handler.session.generation == first + 1
    handler.close()


@pytest.mark.parametrize("status", [403, 500])
def test_tr064_http_403_or_500_on_a_query_is_transient_and_never_counted(monkeypatch, status):
    box = _full_box()
    handler, _ = _tr064_operating(monkeypatch, box, cycles=3)
    service = handler.services[0]
    for action in service.actions:
        box.deny("WANCommonIFC1", action.name, status)
    handler.session = box.tr064_client()

    assert handler._query_service_with_backoff(service) is False

    assert handler._rejected_attempts == 0
    assert handler.auth_failed is False
    assert service.failure_count == 1
    handler.session.close()


def test_tr064_http_403_on_the_startup_probe_is_no_credential_rejection(monkeypatch):
    box = FakeFritzBox()
    box.deny("DeviceInfo1", "GetInfo", 403)
    created = attach_to_handler(monkeypatch, box)
    handler = FritzBoxHandler(_fritzbox_config())

    handler.connect()

    assert handler.auth_failed is False
    assert handler._rejected_attempts == 0
    assert handler.init_successful is False
    assert created[0].closed is True


def test_tr064_soap_fault_401_disables_the_action_not_the_login(monkeypatch):
    box = _full_box()
    handler, _ = _tr064_operating(monkeypatch, box, cycles=3)
    handler.discovery_done = False
    service = handler.services[0]
    box.fault("WANCommonIFC1", service.actions[0].name, 401, "Invalid Action")
    handler.session = box.tr064_client()

    handler.query_service_data(service)

    assert service.actions[0].available is False
    assert service.actions[1].available is True
    assert service.available is True
    assert handler._rejected_attempts == 0
    handler.session.close()


def test_tr064_unknown_service_is_switched_off_during_discovery(monkeypatch):
    box = _full_box()
    box.drop("WANCommonIFC1")
    handler, _ = _tr064_operating(monkeypatch, box, cycles=3)
    handler.discovery_done = False
    service = handler.services[0]
    handler.session = box.tr064_client()

    handler.query_service_data(service)

    assert service.available is False
    handler.session.close()


@pytest.mark.parametrize("code", [606, 501, 820])
def test_tr064_other_soap_faults_are_transient_and_keep_the_action(monkeypatch, code):
    box = _full_box()
    handler, _ = _tr064_operating(monkeypatch, box, cycles=3)
    service = handler.services[0]
    for action in service.actions:
        box.fault("WANCommonIFC1", action.name, code, "fault")
    handler.session = box.tr064_client()

    assert handler.query_service_data(service) is False

    assert all(action.available for action in service.actions)
    assert service.available is True
    assert handler._rejected_attempts == 0
    handler.session.close()


def test_tr064_transport_error_in_a_query_backs_the_service_off(monkeypatch):
    box = _full_box()
    handler, _ = _tr064_operating(monkeypatch, box, cycles=3)
    service = handler.services[0]
    handler.session = box.tr064_client()
    box.raise_transport(httpx.ReadError)

    assert handler._query_service_with_backoff(service) is False

    assert service.failure_count == 1
    assert handler._rejected_attempts == 0
    handler.session.close()


def test_tr064_https_protocol_error_does_not_fall_back(monkeypatch):
    box = FakeFritzBox()
    box.override("/tr64desc.xml", "<html>boom</html>", status=500, content_type="text/html")
    attach_to_handler(monkeypatch, box)
    handler = FritzBoxHandler(_fritzbox_config())

    handler.connect()

    assert _tls_attempts(box) == [True]
    assert handler.init_successful is False
    assert handler.auth_failed is False
    assert handler.access_disabled is False


def test_tr064_read_timeout_during_the_https_probe_does_not_fall_back(monkeypatch):
    box = FakeFritzBox()
    box.raise_transport(httpx.ReadTimeout, path="/tr64desc.xml")
    attach_to_handler(monkeypatch, box)
    handler = FritzBoxHandler(_fritzbox_config())

    handler.connect()

    assert _tls_attempts(box) == [True]
    assert handler.init_successful is False
    assert handler._rejected_attempts == 0


def test_tr064_closed_session_during_the_shutdown_is_logged_at_debug_level(monkeypatch, caplog):
    box = _full_box()
    handler, _ = _tr064_operating(monkeypatch, box, cycles=3)
    service = handler.services[0]
    session = handler.session = box.tr064_client()
    session.close()
    handler.shutting_down = True

    with caplog.at_level(logging.DEBUG):
        assert handler.query_service_data(service) is False

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("TR064ClientClosed" in r.getMessage() for r in caplog.records)


def test_tr064_debug_log_names_the_action_parameters_but_not_their_values(monkeypatch, caplog):
    from app.fritzbox.service_handler import FritzBoxTR064Service

    box = FakeFritzBox()
    handler, _ = _tr064_operating(monkeypatch, box, cycles=3)
    service = FritzBoxTR064Service({
        "name": "WANPPPConnection:1",
        "actions": [{"name": "SetEnable", "params": {"NewEnable": "secret-value"}}],
        "value_instances": {},
    })
    handler.session = box.tr064_client()

    with caplog.at_level(logging.DEBUG):
        handler.query_service_data(service)

    assert "(parameters: NewEnable)" in caplog.text
    assert "secret-value" not in caplog.text
    handler.session.close()


def test_main_exits_78_when_a_producer_ends_with_a_permanent_error(run_main, monkeypatch):
    from app.utils.common import PermanentConfigError

    async def rejected(self, queue):
        raise PermanentConfigError("rejected")

    async def idle(self, queue):
        await asyncio.sleep(60)

    monkeypatch.setattr(_FakeBox, "task_loop", rejected, raising=False)
    monkeypatch.setattr(_FakeDb, "task_loop", idle, raising=False)
    monkeypatch.setattr(_Closable, "task_loop", idle, raising=False)
    monkeypatch.setattr(_FakeBox, "init_successful", True, raising=False)
    assert run_main(running=True) == 78


def test_main_exits_78_when_the_writer_dies_during_the_shutdown_drain(run_main, monkeypatch):
    import signal
    import time

    async def stop_with_pending_data(self, queue):
        await queue.put("pending")
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(60)

    async def idle(self, queue):
        await asyncio.sleep(60)

    async def writer_rejected(self, queue):
        await asyncio.sleep(0.3)
        raise DatabaseConfigError("rejected")

    async def drained(self, queue):
        pass

    monkeypatch.setattr(app_main, "QUEUE_DRAIN_TIMEOUT", 30)
    monkeypatch.setattr(_FakeBox, "task_loop", stop_with_pending_data, raising=False)
    monkeypatch.setattr(_FakeDb, "task_loop", writer_rejected, raising=False)
    monkeypatch.setattr(_Closable, "task_loop", idle, raising=False)
    monkeypatch.setattr(_Closable, "drain", drained, raising=False)
    monkeypatch.setattr(_FakeBox, "init_successful", True, raising=False)
    started = time.monotonic()
    assert run_main(running=True) == 78
    assert time.monotonic() - started < 5


def test_unreachable_box_on_the_http_fallback_logs_no_traceback(monkeypatch, caplog):
    box = FakeFritzBox()
    box.raise_transport(httpx.ConnectError)
    attach_to_handler(monkeypatch, box)
    handler = FritzBoxHandler(_fritzbox_config())

    with caplog.at_level(logging.DEBUG):
        handler._open_session(reconnect=True)

    assert not any(record.exc_info for record in caplog.records)
    assert any("Failed to connect to FritzBox via TR-064" in record.getMessage() for record in caplog.records)
