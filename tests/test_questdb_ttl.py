#!/usr/bin/env python3
#
# tests/test_questdb_ttl.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.influxdb.handler import (
    InfluxHandler,
    QuestDBVersionError,
    parse_questdb_version,
)

TTL_QUERY = "SELECT ttlValue, ttlUnit FROM tables() WHERE table_name = 'fritzbox_AA1_metrics';"


def make_handler(retention_days, responses):
    handler = InfluxHandler.__new__(InfluxHandler)
    handler.version = "questdb"
    handler.config = SimpleNamespace(measurement_name="fritzbox_AA1", data_retention_days=retention_days)
    handler.queries = []

    async def fake_exec(query, *, auth=None):
        handler.queries.append(query)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    handler._questdb_exec = fake_exec
    return handler


def test_ttl_is_set_on_table_without_ttl():
    handler = make_handler(30, [{"dataset": [[0, "HOUR"]]}, {"ddl": "OK"}])

    asyncio.run(handler._ensure_questdb_ttl())

    assert handler.queries == [
        TTL_QUERY,
        'ALTER TABLE "fritzbox_AA1_metrics" SET TTL 30 DAYS;',
    ]


def test_existing_ttl_is_kept():
    handler = make_handler(30, [{"dataset": [[90, "DAY"]]}])

    asyncio.run(handler._ensure_questdb_ttl())

    assert handler.queries == [TTL_QUERY]


def test_retention_zero_disables_ttl_handling():
    handler = make_handler(0, [])

    asyncio.run(handler._ensure_questdb_ttl())

    assert handler.queries == []


def test_questdb_without_ttl_support_is_tolerated():
    request = httpx.Request("GET", "http://questdb:9000/exec")
    error = httpx.HTTPStatusError("400", request=request, response=httpx.Response(400, request=request))
    handler = make_handler(30, [error])

    asyncio.run(handler._ensure_questdb_ttl())

    assert handler.queries == [TTL_QUERY]


def test_transport_errors_propagate_for_retry():
    handler = make_handler(30, [httpx.ConnectError("refused")])

    with pytest.raises(httpx.ConnectError):
        asyncio.run(handler._ensure_questdb_ttl())


def test_schema_failure_schedules_background_retry_without_downsampling(monkeypatch):
    handler = InfluxHandler.__new__(InfluxHandler)
    handler.version = "questdb"
    handler.name = "QuestDB"
    handler.user_agent = None
    handler.base_url = "http://questdb:9000"
    handler.questdb_schema_ensured = False
    handler.questdb_version = None
    handler._questdb_downsampling_retry_task = None
    handler.config = SimpleNamespace(
        measurement_name="fritzbox_AA1", verify_tls=True, username=None, password=None, downsampling=None,
    )

    class Client:
        def __init__(self, **_):
            pass

        async def get(self, *_, **__):
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {"dataset": [["Build Information: QuestDB 10.0.1, JDK 25, Commit Hash abc"]]})

    async def failing_schema(*, auth=None):
        raise RuntimeError("schema failed")

    scheduled = []
    monkeypatch.setattr("app.influxdb.handler.httpx.AsyncClient", Client)
    handler._ensure_questdb_schema = failing_schema
    handler._schedule_questdb_downsampling_retry = lambda *, auth=None: scheduled.append(True)

    assert asyncio.run(handler._init_client()) is True
    assert scheduled == [True]
    assert handler.questdb_schema_ensured is False


def _version_handler(response):
    handler = InfluxHandler.__new__(InfluxHandler)
    handler.version = "questdb"

    class Client:
        async def get(self, *_, **__):
            if isinstance(response, Exception):
                raise response
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: response)

    return handler, Client()


@pytest.mark.parametrize("text, expected", [
    ("Build Information: QuestDB 10.0.1, JDK 25, Commit Hash abc", (10, 0, 1)),
    ("Build Information: QuestDB 9.3, JDK 21", (9, 3, 0)),
])
def test_parse_questdb_version(text, expected):
    assert parse_questdb_version(text) == expected


def test_old_questdb_version_is_rejected():
    handler, client = _version_handler({"dataset": [["Build Information: QuestDB 9.3.2, JDK 21"]]})
    with pytest.raises(QuestDBVersionError):
        asyncio.run(handler._fetch_questdb_version(client, None))


def test_unreadable_questdb_version_is_rejected():
    handler, client = _version_handler({"dataset": []})
    with pytest.raises(QuestDBVersionError):
        asyncio.run(handler._fetch_questdb_version(client, None))


def test_current_questdb_version_is_accepted():
    handler, client = _version_handler({"dataset": [["Build Information: QuestDB 10.2.0, JDK 25"]]})
    assert asyncio.run(handler._fetch_questdb_version(client, None)) == "10.2.0"


def test_non_json_answer_is_transient_not_a_version_error():
    def not_json():
        raise ValueError("Expecting value")

    handler = InfluxHandler.__new__(InfluxHandler)
    handler.version = "questdb"

    class Client:
        async def get(self, *_, **__):
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=not_json, request=None)

    with pytest.raises(httpx.HTTPError):
        asyncio.run(handler._fetch_questdb_version(Client(), None))


def test_unreachable_questdb_is_not_a_version_error():
    handler, client = _version_handler(httpx.ConnectError("down"))
    with pytest.raises(httpx.ConnectError):
        asyncio.run(handler._fetch_questdb_version(client, None))


def test_disabled_downsampling_applies_ttl_even_when_status_table_exists():
    handler = make_handler(30, [])
    handler.config.downsampling = ""
    published = {}

    async def noop(*args, **kwargs):
        return True

    async def managed(**kwargs):
        return False

    async def ensure_ttl(**kwargs):
        return True

    async def publish(**kwargs):
        published.update(kwargs)
        return True

    handler._questdb_exec = noop
    handler._ensure_questdb_events_table = noop
    handler._questdb_raw_ttl_is_managed = managed
    handler._ensure_questdb_ttl = ensure_ttl
    handler._set_questdb_downsampling_state = publish

    assert asyncio.run(handler._ensure_questdb_schema()) is True
    assert published["raw_ttl_managed"] is True


def test_questdb_version_error_on_reconnect_closes_the_client(monkeypatch):
    handler = InfluxHandler.__new__(InfluxHandler)
    handler.version = "questdb"
    handler.base_url = "http://questdb.invalid:9000"
    handler.user_agent = None
    handler.questdb_schema_ensured = True
    handler.config = SimpleNamespace(username=None, verify_tls=True)
    closed = []

    async def too_old(client, auth):
        handler.opened = client
        raise QuestDBVersionError("too old")

    handler._fetch_questdb_version = too_old
    orig = httpx.AsyncClient.aclose

    async def tracking_aclose(self):
        closed.append(self)
        await orig(self)

    monkeypatch.setattr(httpx.AsyncClient, "aclose", tracking_aclose)

    with pytest.raises(QuestDBVersionError):
        asyncio.run(handler._init_client())
    assert handler.client is None
    assert closed == [handler.opened]
