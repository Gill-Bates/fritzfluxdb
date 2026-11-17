#!/usr/bin/env python3
#
# tests/test_tr064_lifecycle.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Session lifecycle: generation, controlled close, lazy SCPD loading, secrets, concurrency."""

import configparser
import logging
import pickle
import threading
import time

import httpx
import pytest

from app.fritzbox.handler import SHUTDOWN_DRAIN_TIMEOUT, FritzBoxHandler
from app.fritzbox.tr064.exceptions import (
    TR064ClientClosed,
    TR064Error,
    TR064HttpUnauthorized,
    TR064SoapFault,
)
from tests.fakebox import DigestVerifier, FakeFritzBox, attach_to_handler

WAIT = 10.0


def test_shutdown_rejects_a_late_initial_connection_without_publishing_it(monkeypatch):
    box = FakeFritzBox()
    reached, release = threading.Event(), threading.Event()
    original = box.handler

    def response(request):
        if request.method == "POST" and request.url.path == "/upnp/control/deviceinfo1":
            reached.set()
            assert release.wait(WAIT)
        return original(request)

    box.handler = response
    created = attach_to_handler(monkeypatch, box)
    config = configparser.ConfigParser()
    config.read_dict({"fritzbox": {"hostname": "box", "username": "test", "password": "test"}})
    handler = FritzBoxHandler(config)
    worker = threading.Thread(target=handler.connect)
    worker.start()
    try:
        assert reached.wait(WAIT), "login probe did not reach the simulated box"
        handler.shutting_down = True
        handler.close()
    finally:
        release.set()
        worker.join(WAIT)
    assert not worker.is_alive()
    assert handler.session is None and not handler.init_successful
    assert all(client.closed for client in created)


class SpyTransport(httpx.MockTransport):
    """Counts how often the pool is closed."""

    def __init__(self, handler) -> None:
        super().__init__(handler)
        self.closes = 0

    def close(self) -> None:
        self.closes += 1
        super().close()


def attempt(function):
    try:
        return function()
    except TR064Error as exc:
        return exc


def run_threads(targets) -> None:
    threads = [threading.Thread(target=target) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not any(thread.is_alive() for thread in threads), "worker thread did not finish"


def test_generation_and_flags_are_exposed_read_only():
    client = FakeFritzBox().tr064_client(generation=7, use_tls=True, port=49443)
    assert (client.generation, client.use_tls, client.closed) == (7, True, False)
    with pytest.raises(AttributeError):
        client.generation = 8  # type: ignore[misc]


def test_calls_on_a_closed_client_raise_typed_error():
    box = FakeFritzBox()
    client = box.tr064_client()
    client.close()
    sent = len(box.requests)
    for operation in (
        lambda: client.call_action("DeviceInfo", "GetInfo"),
        lambda: client.has_action("DeviceInfo", "GetInfo"),
    ):
        with pytest.raises(TR064ClientClosed):
            operation()
    undiscovered = box.tr064_client(discover=False)
    undiscovered.close()
    with pytest.raises(TR064ClientClosed):
        undiscovered.discover()
    assert len(box.requests) == sent
    assert client.closed is True


def test_close_is_idempotent_and_closes_the_pool_once():
    box = FakeFritzBox()
    transport = SpyTransport(box.handler)
    client = box.tr064_client(transport=transport)
    client.close()
    client.close()
    assert transport.closes == 1


def test_close_refuses_new_requests_and_lets_inflight_finish():
    box = FakeFritzBox()
    client = box.tr064_client()
    entered, release = threading.Event(), threading.Event()

    def hook(request: httpx.Request) -> None:
        if request.method == "POST":
            entered.set()
            if not release.wait(WAIT):
                raise RuntimeError("test did not release the request")

    box.hook = hook
    outcome: list[object] = []
    worker = threading.Thread(target=lambda: outcome.append(attempt(lambda: client.call_action("DeviceInfo", "GetInfo"))))
    worker.start()
    assert entered.wait(WAIT), "request never reached the box"
    client.close()
    with pytest.raises(TR064ClientClosed):
        client.call_action("DeviceInfo", "GetInfo")
    release.set()
    worker.join(WAIT)
    assert not worker.is_alive()
    # The in-flight request either completed or failed typed; it never leaks an untyped error.
    assert len(outcome) == 1
    assert isinstance(outcome[0], (dict, TR064ClientClosed))


def test_shutdown_drain_is_bounded_by_the_existing_timeout():
    assert SHUTDOWN_DRAIN_TIMEOUT == 5
    box = FakeFritzBox()
    client = box.tr064_client()
    entered, release = threading.Event(), threading.Event()
    box.hook = lambda request: (entered.set(), release.wait(WAIT)) if request.method == "POST" else None
    blocked = threading.Thread(target=lambda: attempt(lambda: client.call_action("DeviceInfo", "GetInfo")))
    blocked.start()
    assert entered.wait(WAIT)
    closer = threading.Thread(target=client.close)
    started = time.monotonic()
    closer.start()
    closer.join(SHUTDOWN_DRAIN_TIMEOUT)
    # A running request cannot be cancelled (known limit); close() itself must not wait for it.
    assert not closer.is_alive()
    assert time.monotonic() - started < SHUTDOWN_DRAIN_TIMEOUT
    release.set()
    blocked.join(WAIT)
    assert not blocked.is_alive()


def test_scpd_loaded_once_under_contention():
    box = FakeFritzBox()
    client = box.tr064_client()
    barrier = threading.Barrier(8)
    # Widen the race window: every SCPD answer is slow so all threads pile up on the lock.
    box.hook = lambda request: time.sleep(0.05) if request.url.path.endswith("SCPD.xml") else None
    results: list[object] = []

    def work() -> None:
        barrier.wait(WAIT)
        results.append(attempt(lambda: client.call_action("DeviceInfo", "GetInfo")))

    run_threads([work] * 8)
    assert results == [{}] * 8
    assert [r.url.path for r in box.requests].count("/deviceinfo1SCPD.xml") == 1


def test_failed_scpd_load_is_not_cached():
    box = FakeFritzBox()
    client = box.tr064_client()
    box.raise_transport(httpx.ConnectError, path="/deviceinfo1SCPD.xml", times=1)
    with pytest.raises(TR064Error):
        client.call_action("DeviceInfo", "GetInfo")
    assert client.call_action("DeviceInfo", "GetInfo") == {}


# (alias, serviceId suffix, action, marker field, marker is numeric)
QUERIES = [
    ("DeviceInfo", "DeviceInfo1", "GetInfo", "NewModelName", False),
    ("Time:1", "Time1", "GetInfo", "NewCurrentLocalTime", False),
    ("WANPPPConnection:1", "WANPPPConnection1", "GetInfo", "NewConnectionStatus", False),
    ("WANDSLInterfaceConfig", "WANDSLInterfaceConfig1", "GetInfo", "NewUpstreamCurrRate", True),
]
WATCHER = ("WANIPConn", "WANIPConn1", "GetStatusInfo", "NewConnectionStatus", False)


def marked_box(generation: int) -> FakeFritzBox:
    box = FakeFritzBox()
    for _, service, action, field, numeric in (*QUERIES, WATCHER):
        box.respond(service, action, {field: str(generation) if numeric else f"gen{generation}"})
    return box


def test_four_queries_watcher_and_reconnect_in_parallel():
    box_a, box_b = marked_box(0), marked_box(1)
    session_a = box_a.tr064_client(generation=0)
    holder = {"current": session_a}
    stop, start = threading.Event(), threading.Barrier(6)
    results: list[tuple[int, bool]] = []
    errors: list[BaseException] = []
    fresh = threading.Event()

    def loop(query) -> None:
        alias, _, action, field, numeric = query
        start.wait(WAIT)
        while not stop.is_set():
            session = holder["current"]  # captured once per call, like query_service_data
            try:
                value = session.call_action(alias, action)[field]
            except TR064ClientClosed:
                continue  # typed: the caller re-reads the published session
            except BaseException as exc:  # noqa: BLE001 - anything untyped fails the test below
                errors.append(exc)
                return
            expected = session.generation if numeric else f"gen{session.generation}"
            results.append((session.generation, value == expected))
            if session.generation == 1 and len(results) > 40:
                fresh.set()

    workers = [threading.Thread(target=loop, args=(query,)) for query in (*QUERIES, WATCHER)]
    for worker in workers:
        worker.start()
    # A failed step must still stop the non-daemon workers, or the test process never exits.
    try:
        start.wait(WAIT)
        session_b = box_b.tr064_client(generation=1)  # built and discovered before it is published
        holder["current"] = session_b
        session_a.close()
        assert fresh.wait(WAIT), "no result from the new generation"
    finally:
        stop.set()
        for worker in workers:
            worker.join(WAIT)
    assert not any(worker.is_alive() for worker in workers)
    session_b.close()
    assert errors == []
    assert results
    assert all(consistent for _, consistent in results), "a result carried a value of another generation"
    assert 1 in {generation for generation, _ in results}
    for box in (box_a, box_b):
        scpd_paths = [r.url.path for r in box.requests if r.url.path.endswith("SCPD.xml")]
        assert len(scpd_paths) == len(set(scpd_paths)), "an SCPD was fetched more than once"
    with pytest.raises(TR064ClientClosed):
        session_a.call_action("DeviceInfo", "GetInfo")


def test_no_secret_in_logs_or_exceptions(caplog):
    caplog.set_level(logging.DEBUG)
    password = "pw-Zx9!never-in-logs"
    verifier = DigestVerifier("user", password)
    box = FakeFritzBox(digest=verifier)
    client = box.tr064_client(username="user", password=password)
    box.fault("DeviceInfo1", "GetInfo", 501, "boom")
    box.deny("Time1", "GetInfo", 401)
    texts = [repr(client), str(client)]
    for alias, action in (("DeviceInfo", "GetInfo"), ("Time:1", "GetInfo")):
        error = attempt(lambda alias=alias, action=action: client.call_action(alias, action))
        assert isinstance(error, (TR064SoapFault, TR064HttpUnauthorized))
        texts += [str(error), repr(error)]
    wrong = box.tr064_client(username="user", password="wrong-pw-never-in-logs", discover=False)
    error = attempt(wrong.discover)
    assert isinstance(error, TR064HttpUnauthorized)
    texts += [str(error), repr(wrong)]
    sent_headers = [header for header in (r.headers.get("authorization") for r in box.requests) if header]
    assert sent_headers
    secrets_to_hide = [password, "wrong-pw-never-in-logs", *sent_headers]
    haystack = "\n".join([*texts, caplog.text])
    for secret in secrets_to_hide:
        assert secret not in haystack
    assert "Digest " not in haystack
    assert "username" not in repr(client)
    assert "password" not in repr(client)


def test_client_cannot_be_pickled_with_its_secrets():
    client = FakeFritzBox().tr064_client(password="pw-secret")
    with pytest.raises(TypeError):
        pickle.dumps(client)


def test_results_of_a_superseded_session_are_discarded():
    """Handler leg of R9: a query that outlives its session drops its measurements."""
    import configparser

    from app.fritzbox.config import FritzBoxConfig
    from app.fritzbox.handler import FritzBoxHandler
    from tests.fakebox import FakeFritzBox, box_for_services

    parser = configparser.ConfigParser()
    parser["fritzbox"] = {"hostname": "fritz.box", "username": "user", "password": "password"}
    handler = FritzBoxHandler(FritzBoxConfig(parser))
    service = next(s for s in handler.services if s.name == "WANIPConn")
    box = box_for_services(handler.services)
    box.respond("WANIPConn1", "GetStatusInfo", {"NewConnectionStatus": "Connected", "NewUptime": "5"})
    replacement = FakeFritzBox().tr064_client(generation=2)
    swapped = []

    def swap(request):
        if request.method == "POST" and not swapped:
            swapped.append(handler.session)
            handler.session = replacement

    handler.session = box.tr064_client(generation=1)
    box.hook = swap

    assert handler.query_service_data(service) is None
    assert handler.current_result_list == []
    # The superseded query must not touch the WAN observer either.
    assert handler._wan_connected is None
    assert service.should_be_requested() is True  # a dropped query must not start the interval
    assert swapped[0].generation == 1

    # Control: the same query on a session that stays current keeps its measurements.
    box.hook = None
    handler.session = swapped[0]
    assert handler.query_service_data(service) is True
    assert {m.name for m in handler.current_result_list} == {"connection_status", "linkuptime"}
    assert handler._wan_connected is True
    replacement.close()
    swapped[0].close()


def test_firmware_version_of_a_superseded_session_is_not_applied():
    """The deferred fw_version update follows the same generation check as the measurements."""
    import configparser

    from app.fritzbox.config import FritzBoxConfig
    from app.fritzbox.handler import FritzBoxHandler
    from tests.fakebox import FakeFritzBox, box_for_services
    parser = configparser.ConfigParser()
    parser["fritzbox"] = {"hostname": "fritz.box", "username": "user", "password": "password"}
    handler = FritzBoxHandler(FritzBoxConfig(parser))
    service = next(s for s in handler.services if s.name == "DeviceInfo")
    box = box_for_services(handler.services)
    box.respond("DeviceInfo1", "GetInfo", {"NewSoftwareVersion": "166.07.57"})
    replacement = FakeFritzBox().tr064_client(generation=2)
    swapped = []
    def swap(request):
        if request.method == "POST" and not swapped:
            swapped.append(handler.session)
            handler.session = replacement
    handler.session = box.tr064_client(generation=1)
    box.hook = swap
    handler.query_service_data(service)
    assert handler.config.fw_version is None
    # Control: a session that stays current applies the version.
    box.hook = None
    handler.session = swapped[0]
    handler.query_service_data(service)
    assert handler.config.fw_version == "7.57"
    replacement.close()
    swapped[0].close()
