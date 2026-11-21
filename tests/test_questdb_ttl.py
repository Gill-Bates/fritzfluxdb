#!/usr/bin/env python3
#
# tests/test_questdb_ttl.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from gillbates_questdb_client import QuestDBError, QuestDBQueryError, QuestDBStatementTooLongError

from app.influxdb.handler import DatabaseConfigError, InfluxHandler
from tests.questdb_fake import fake_questdb, status_error

TTL_QUERY = "SELECT ttlValue, ttlUnit FROM tables() WHERE table_name = 'fritzbox_AA1_metrics';"


def make_handler(retention_days, responses):
    handler = InfluxHandler.__new__(InfluxHandler)
    handler.version = "questdb"
    handler.config = SimpleNamespace(measurement_name="fritzbox_AA1", data_retention_days=retention_days)
    handler.queries = []

    async def fake_exec(query):
        handler.queries.append(query)
        response = responses.pop(0)
        return response

    handler._questdb = fake_questdb(fake_exec)
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


def _init_handler(answer):
    handler = InfluxHandler.__new__(InfluxHandler)
    handler.version = "questdb"
    handler.name = "QuestDB"
    handler.user_agent = None
    handler.base_url = "http://questdb:9000"
    handler.questdb_schema_ensured = False
    handler.questdb_version = None
    handler._questdb_downsampling_retry_task = None
    handler.connection_lost = False
    handler.last_connection_warning = None
    handler.buffer = []
    handler.connection_warning_interval = 60
    handler.config = SimpleNamespace(
        measurement_name="fritzbox_AA1", hostname="questdb", verify_tls=True, username=None, password=None,
        downsampling=None,
    )
    handler._new_questdb_client = lambda: fake_questdb(lambda query: answer)
    return handler


OK_BUILD = {"dataset": [["Build Information: QuestDB 10.0.1, JDK 25, Commit Hash abc"]]}


def test_schema_failure_schedules_background_retry_without_downsampling():
    handler = _init_handler(OK_BUILD)

    async def failing_schema():
        raise RuntimeError("schema failed")

    scheduled = []
    handler._ensure_questdb_schema = failing_schema
    handler._schedule_questdb_downsampling_retry = lambda: scheduled.append(True)
    assert asyncio.run(handler._init_client()) is True
    assert scheduled == [True]
    assert handler.questdb_schema_ensured is False
    assert handler.questdb_version == "10.0.1"
    asyncio.run(handler._close_client())


def fetch_version(answer):
    handler = InfluxHandler.__new__(InfluxHandler)
    return asyncio.run(handler._fetch_questdb_version(fake_questdb(lambda query: answer)))


def test_current_questdb_version_is_accepted():
    assert fetch_version({"dataset": [["Build Information: QuestDB 10.2.0, JDK 25"]]}) == "10.2.0"


@pytest.mark.parametrize("answer", [
    {"dataset": [["Build Information: QuestDB 9.3.2, JDK 21"]]},
    {"dataset": []},
    {"dataset": [["Build Information: something else"]]},
    status_error(401),
    status_error(403),
])
def test_unusable_version_or_rejected_credentials_are_permanent(answer):
    """Exit 78: server older than 10, unreadable build(), credentials or permissions rejected."""
    with pytest.raises(DatabaseConfigError):
        fetch_version(answer)


@pytest.mark.parametrize(("answer", "expected"), [
    ("<html>maintenance</html>", httpx.DecodingError),
    (httpx.ConnectError("down"), httpx.ConnectError),
    (httpx.ReadTimeout("slow"), httpx.ReadTimeout),
    (status_error(500), httpx.HTTPStatusError),
    (status_error(404), httpx.HTTPStatusError),
    (status_error(429), httpx.HTTPStatusError),
])
def test_unreachable_or_misbehaving_questdb_is_never_a_config_error(answer, expected):
    with pytest.raises(expected) as caught:
        fetch_version(answer)
    assert not isinstance(caught.value, DatabaseConfigError)


def test_rejected_build_statement_is_not_a_config_error():
    with pytest.raises(QuestDBQueryError) as caught:
        fetch_version(RuntimeError("function build() not found"))
    assert not isinstance(caught.value, DatabaseConfigError)


def test_rejected_build_statement_during_init_retries_instead_of_failing():
    handler = _init_handler(RuntimeError("function build() not found"))
    assert asyncio.run(handler._init_client()) is False
    assert handler.client is None
    assert handler._questdb is None
    assert handler.connection_lost is True


@pytest.mark.parametrize("code", [401, 403])
def test_credentials_rejected_while_running_are_a_config_error(code):
    handler = make_handler(30, [status_error(code)])
    with pytest.raises(DatabaseConfigError):
        asyncio.run(handler._ensure_questdb_ttl())


@pytest.mark.parametrize("error", [
    QuestDBQueryError("sql error"),
    QuestDBStatementTooLongError("too long"),
    status_error(400),
    status_error(404),
    status_error(500),
    status_error(503),
])
def test_statement_failures_are_not_config_errors(error):
    handler = make_handler(0, [])
    handler._questdb = fake_questdb(lambda query: error)
    with pytest.raises((QuestDBError, httpx.HTTPStatusError)) as caught:
        asyncio.run(handler._questdb_exec("SELECT 1;"))
    assert not isinstance(caught.value, DatabaseConfigError)


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

    handler._questdb = fake_questdb(lambda query: {"ddl": "OK"})
    handler._ensure_questdb_events_table = noop
    handler._questdb_raw_ttl_is_managed = managed
    handler._ensure_questdb_ttl = ensure_ttl
    handler._set_questdb_downsampling_state = publish
    assert asyncio.run(handler._ensure_questdb_schema()) is True
    assert published["raw_ttl_managed"] is True


def test_questdb_version_error_on_reconnect_closes_the_clients(monkeypatch):
    handler = _init_handler(status_error(403))
    handler.questdb_schema_ensured = True
    closed = []
    orig = httpx.AsyncClient.aclose

    async def tracking_aclose(self):
        closed.append(self)
        await orig(self)

    monkeypatch.setattr(httpx.AsyncClient, "aclose", tracking_aclose)
    with pytest.raises(DatabaseConfigError):
        asyncio.run(handler._init_client())
    assert handler.client is None
    assert handler._questdb is None
    assert len(closed) == 2
