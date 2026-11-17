#!/usr/bin/env python3
#
# tests/test_request_deadline.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The overall per-request deadline of the TR-064 client and the Lua handler (CR-01 / B9)."""

import time

import httpx
import pytest

from app.fritzbox.handler import FritzBoxLuaHandler
from app.fritzbox.tr064 import deadline as deadline_module
from app.fritzbox.tr064.client import TR064Client
from app.fritzbox.tr064.deadline import RequestDeadline, read_bounded, request_timeout
from app.fritzbox.tr064.exceptions import (
    TR064ProtocolError,
    TR064TimeoutError,
    TR064TransportError,
)

SECRET = "s3cret-pass"


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _Trickle(httpx.SyncByteStream):
    """Endless body: one byte per pause, so no per-read httpx timeout ever fires."""

    def __init__(self, pause: float = 0.02) -> None:
        self.pause = pause

    def __iter__(self):
        while True:
            time.sleep(self.pause)
            yield b"x"


def _trickling_transport() -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(200, stream=_Trickle()))


@pytest.fixture
def short_deadline(monkeypatch):
    monkeypatch.setattr(deadline_module, "MAX_REQUEST_SECONDS", 0.3)


def test_request_timeout_is_capped_at_the_overall_deadline():
    assert request_timeout(1).read == 4
    capped = request_timeout(60)
    assert capped.read == deadline_module.MAX_REQUEST_SECONDS
    assert capped.connect == deadline_module.MAX_REQUEST_SECONDS
    assert request_timeout(3).connect == 3


def test_deadline_caps_every_phase_of_the_next_send_at_the_remaining_budget():
    clock = _Clock()
    deadline = RequestDeadline(httpx.Timeout(8.0, connect=2.0), clock)
    clock.now += 3
    capped = deadline.timeout_for_next_send(httpx.Timeout(8.0, connect=2.0)).as_dict()
    assert capped == {"connect": 2.0, "read": 5.0, "write": 5.0, "pool": 5.0}
    clock.now += 5
    with pytest.raises(httpx.ReadTimeout):
        deadline.timeout_for_next_send(httpx.Timeout(8.0))


def test_read_bounded_stops_a_body_that_outlasts_the_deadline_and_the_size_limit_still_holds():
    clock = _Clock()

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(100):
                clock.now += 1
                yield b"x"

    deadline = RequestDeadline(httpx.Timeout(5.0), clock)
    with pytest.raises(httpx.ReadTimeout, match="overall request deadline"):
        read_bounded(httpx.Response(200, stream=Body()), 1000, deadline)

    deadline = RequestDeadline(httpx.Timeout(5.0), clock)
    with pytest.raises(TR064ProtocolError, match="larger than 3 bytes"):
        read_bounded(httpx.Response(200, content=b"abcd"), 3, deadline)
    assert read_bounded(httpx.Response(200, content=b"abc"), 3, deadline) == b"abc"


def test_client_reports_a_trickling_response_as_a_transient_timeout_without_secrets(short_deadline):
    client = TR064Client(
        "box", 49000, username="u", password=SECRET, use_tls=False, verify=False,
        timeout=request_timeout(10), transport=_trickling_transport(),
    )
    started = time.monotonic()
    try:
        with pytest.raises(TR064TimeoutError) as caught:
            client._exchange("GET", "/tr64desc.xml", 1_000_000)
    finally:
        client.close()
    assert isinstance(caught.value, TR064TransportError)
    assert time.monotonic() - started < 3
    assert SECRET not in str(caught.value)


def test_lua_fetch_stops_a_trickling_response_with_an_httpx_timeout(short_deadline):
    session = httpx.Client(transport=_trickling_transport(), timeout=request_timeout(10))
    started = time.monotonic()
    try:
        with pytest.raises(httpx.ReadTimeout) as caught:
            FritzBoxLuaHandler._fetch(session, "GET", "http://box/login_sid.lua", 1_000_000, params={"sid": SECRET})
    finally:
        session.close()
    assert time.monotonic() - started < 3
    assert SECRET not in str(caught.value)
