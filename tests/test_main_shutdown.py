#!/usr/bin/env python3
#
# tests/test_main_shutdown.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
import configparser
import logging
import signal
from unittest.mock import Mock

import pytest

from app.fritzbox.config import FritzBoxConfig
from app.influxdb.handler import InfluxLogAndConfigWriter
from app.main import _log_finished_workers


def test_worker_failure_is_reported_when_the_shutdown_signal_completes_in_the_same_round():
    async def scenario():
        async def failing_worker():
            raise RuntimeError("worker broke")

        shutdown_event = asyncio.Event()
        shutdown_event.set()
        worker = asyncio.create_task(failing_worker(), name="broken.task_loop")
        shutdown_task = asyncio.create_task(shutdown_event.wait(), name="shutdown.wait")
        # Both finish before wait() returns; the worker failure must not hide behind the shutdown.
        done, _ = await asyncio.wait([worker, shutdown_task], return_when=asyncio.FIRST_COMPLETED)
        assert {worker, shutdown_task} == done
        log = Mock(spec=logging.Logger)
        return _log_finished_workers(log, done, shutdown_task), log

    failed, log = asyncio.run(scenario())

    assert failed is True
    log.error.assert_called_once()
    assert log.error.call_args.args[1] == "broken.task_loop"


def test_plain_shutdown_is_not_a_failure():
    async def scenario():
        shutdown_event = asyncio.Event()
        shutdown_event.set()
        shutdown_task = asyncio.create_task(shutdown_event.wait(), name="shutdown.wait")
        done, _ = await asyncio.wait([shutdown_task])
        log = Mock(spec=logging.Logger)
        return _log_finished_workers(log, done, shutdown_task), log

    failed, log = asyncio.run(scenario())

    assert failed is False
    log.error.assert_not_called()


def test_log_writer_drain_forwards_records_left_in_the_log_queue():
    async def scenario():
        log_queue: asyncio.Queue = asyncio.Queue()
        output_queue: asyncio.Queue = asyncio.Queue()
        writer = InfluxLogAndConfigWriter(FritzBoxConfig(configparser.ConfigParser()), log_queue)
        for message in ("first", "last"):
            log_queue.put_nowait(logging.LogRecord("fritzFlux", logging.INFO, __file__, 1, message, None, None))
        log_queue.put_nowait("not a log record")

        await writer.drain(output_queue)
        return log_queue, output_queue

    log_queue, output_queue = asyncio.run(scenario())

    assert log_queue.empty()
    assert [m.value for m in (output_queue.get_nowait(), output_queue.get_nowait())] == ["INFO: first", "INFO: last"]
    assert output_queue.empty()


def test_unexpected_exception_in_a_service_query_is_a_backoff_not_a_crash():
    from app.fritzbox.handler import FritzBoxHandler
    from app.fritzbox.service_handler import FritzBoxService

    handler = FritzBoxHandler(FritzBoxConfig(configparser.ConfigParser()))
    service = FritzBoxService({"name": "Broken", "value_instances": {}})
    handler.query_service_data = Mock(side_effect=RuntimeError("boom"))

    assert handler._query_service_with_backoff(service) is False
    assert service.failure_count == 1


def test_configuration_errors_exit_with_ex_config():
    import pytest

    from app.utils.common import do_error_exit

    with pytest.raises(SystemExit) as error:
        do_error_exit("broken")

    assert error.value.code == 78


def test_failing_close_does_not_skip_the_other_connections(caplog):
    from unittest.mock import AsyncMock

    from app.main import _close_connections

    box = Mock()
    box.close.side_effect = RuntimeError("box")
    lua = Mock()
    database = Mock()
    database.close = AsyncMock(side_effect=RuntimeError("db"))

    with caplog.at_level(logging.ERROR, logger="fritzFlux"):
        asyncio.run(_close_connections(box, lua, database))

    lua.close.assert_called_once()
    database.close.assert_awaited_once()
    assert "Closing the FritzBox TR-064 connection failed" in caplog.text
    assert "Closing the database connection failed" in caplog.text


def test_cancellation_during_close_is_not_swallowed():
    from unittest.mock import AsyncMock

    import pytest

    from app.main import _close_connections

    database = Mock()
    database.close = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_close_connections(Mock(), Mock(), database))


def test_connect_is_abandoned_after_the_shutdown_signal_without_follow_up_initialization(monkeypatch):
    import threading

    import app.main as app_main

    release = threading.Event()
    monkeypatch.setattr(app_main, "INIT_ABORT_TIMEOUT", 0.05)

    async def scenario():
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        loop.call_later(0.05, event.set)
        try:
            return await app_main._connect_unless_stopped(release.wait, event)
        finally:
            release.set()

    try:
        assert asyncio.run(scenario()) is False
    finally:
        release.set()


def test_connect_that_finishes_before_a_signal_reports_success_and_propagates_errors():
    import pytest

    import app.main as app_main

    async def ok():
        return await app_main._connect_unless_stopped(lambda: None, asyncio.Event())

    async def failing():
        def boom():
            raise RuntimeError("connect failed")
        return await app_main._connect_unless_stopped(boom, asyncio.Event())

    assert asyncio.run(ok()) is True
    with pytest.raises(RuntimeError, match="connect failed"):
        asyncio.run(failing())


# CR-01: the connect phase must end with the process, not only hand control back to main_async().
_BLOCKED_CONNECT_DRIVER = """
import asyncio, configparser, os, signal, sys
from unittest.mock import AsyncMock, Mock

import app.main as m
from app.fritzbox.config import FritzBoxConfig
from app.fritzbox.handler import FritzBoxHandler, FritzBoxLuaHandler

kind, port, connect_timeout = sys.argv[1], int(sys.argv[2]), sys.argv[3]
ini = configparser.ConfigParser()
ini.add_section("fritzbox")
for key, value in {"username": "u", "password": "p", "hostname": "127.0.0.1",
                   "port": str(port), "connect_timeout": connect_timeout}.items():
    ini.set("fritzbox", key, value)
config = FritzBoxConfig(ini)
# The Lua handler builds its URL without a port; point it at the silent test server.
FritzBoxLuaHandler._base_url = lambda self, tls: f"{'https' if tls else 'http'}://127.0.0.1:{port}"
tr064, lua = FritzBoxHandler(config), FritzBoxLuaHandler(config)


async def scenario():
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    # Mirrors the signal handler of main_async(), which also tells both handlers about the shutdown.
    loop.add_signal_handler(
        signal.SIGTERM, lambda: (stop.set(), tr064.request_shutdown(), lua.request_shutdown())
    )
    print("ready", flush=True)
    try:
        return await m._connect_unless_stopped((tr064 if kind == "tr064" else lua).connect, stop)
    finally:
        await m._close_connections(tr064, lua, Mock(close=AsyncMock()))


completed = asyncio.run(scenario())
sys.exit(3 if completed else 0)
"""


def _silent_peer(conn, stop):
    stop.wait()


def _trickling_peer(conn, stop):
    # One byte per 0.3 s keeps every httpx read timeout (4 s here) from firing, so only the deadline can end it.
    try:
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/xml\r\n\r\n")
        while not stop.wait(0.3):
            conn.sendall(b" ")
    except OSError:
        return


def _tls_hangs_http_trickles_peer(conn, stop):
    import socket

    # A TLS ClientHello starts with 0x16: stay silent, so the HTTPS attempt ends only at its connect timeout.
    try:
        first = conn.recv(1, socket.MSG_PEEK)
    except OSError:
        return
    if first == b"\x16":
        stop.wait()
        return
    _trickling_peer(conn, stop)


def _assert_process_ends_after_sigterm(kind, peer, *, connect_timeout=1, limit=None):
    import socket
    import subprocess
    import sys
    import threading
    import time
    from pathlib import Path

    import app.main as app_main

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept_connections():
        while True:
            try:
                conn = server.accept()[0]
            except OSError:
                return
            held.append(conn)
            threading.Thread(target=peer, args=(conn, stop), daemon=True).start()

    threading.Thread(target=accept_connections, daemon=True).start()
    root = Path(__file__).resolve().parents[1]
    try:
        # The with block closes the pipes and reaps the child on every path, also after a failed assert.
        with subprocess.Popen(
            [sys.executable, "-c", _BLOCKED_CONNECT_DRIVER, kind, str(server.getsockname()[1]), str(connect_timeout)],
            cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as proc:
            try:
                assert proc.stdout.readline().strip() == "ready"
                time.sleep(0.5)
                signalled = time.monotonic()
                proc.send_signal(signal.SIGTERM)
                # The process end is measured, not the return of main_async(); connect_timeout is 1 s in the driver.
                try:
                    _, stderr = proc.communicate(timeout=20)
                except subprocess.TimeoutExpired:
                    pytest.fail(f"process still alive 20 s after SIGTERM during a blocked {kind} connect")
                elapsed = time.monotonic() - signalled
            finally:
                proc.kill()
    finally:
        stop.set()
        server.close()
        for sock in held:
            sock.close()
    assert proc.returncode == 0, stderr
    assert elapsed < (app_main.INIT_ABORT_TIMEOUT + 8 if limit is None else limit)


@pytest.mark.parametrize("kind", ["tr064", "lua"])
def test_sigterm_during_a_blocked_connect_ends_the_process_within_the_shutdown_budget(kind):
    _assert_process_ends_after_sigterm(kind, _silent_peer)


@pytest.mark.parametrize("kind", ["tr064", "lua"])
def test_sigterm_while_the_peer_trickles_data_ends_the_process_within_the_shutdown_budget(kind):
    _assert_process_ends_after_sigterm(kind, _trickling_peer)


@pytest.mark.parametrize("kind", ["tr064", "lua"])
def test_sigterm_during_the_https_attempt_does_not_start_the_http_fallback(kind):
    # Without the shutdown check the trickling HTTP attempt would run for its whole 8 s deadline after the signal.
    _assert_process_ends_after_sigterm(kind, _tls_hangs_http_trickles_peer, connect_timeout=2, limit=5)


def test_application_code_has_no_forced_exit_fallback():
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = [str(path) for path in app_dir.rglob("*.py") if "os._exit" in path.read_text(encoding="utf-8")]
    assert offenders == []


def test_http_clients_bound_every_phase_of_a_request(monkeypatch):
    import configparser

    from app.fritzbox.handler import FritzBoxHandler, FritzBoxLuaHandler

    ini = configparser.ConfigParser()
    ini.add_section("fritzbox")
    for key, value in {"username": "u", "password": "p", "connect_timeout": "2"}.items():
        ini.set("fritzbox", key, value)
    config = FritzBoxConfig(ini)
    monkeypatch.setattr("app.fritzbox.handler.TR064Client.discover", lambda self: None)

    tr064_client = FritzBoxHandler(config)._create_session(True, 49443)
    lua = FritzBoxLuaHandler(config)
    try:
        lua._build_session()
        for timeout in (tr064_client._http.timeout, lua.session.timeout):
            phases = (timeout.connect, timeout.read, timeout.write, timeout.pool)
            assert None not in phases
            assert max(phases) <= config.connect_timeout * 4
    finally:
        tr064_client.close()
        lua.close()
