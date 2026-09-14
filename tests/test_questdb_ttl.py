#!/usr/bin/env python3
#
# tests/test_questdb_ttl.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from fritzfluxdb.classes.influxdb.handler import InfluxHandler


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
        "SELECT ttlValue, ttlUnit FROM tables() WHERE table_name = 'fritzbox_AA1';",
        'ALTER TABLE "fritzbox_AA1" SET TTL 30 DAYS;',
    ]


def test_existing_ttl_is_kept():
    handler = make_handler(30, [{"dataset": [[90, "DAY"]]}])

    asyncio.run(handler._ensure_questdb_ttl())

    assert len(handler.queries) == 1


def test_retention_zero_disables_ttl_handling():
    handler = make_handler(0, [])

    asyncio.run(handler._ensure_questdb_ttl())

    assert handler.queries == []


def test_questdb_without_ttl_support_is_tolerated():
    request = httpx.Request("GET", "http://questdb:9000/exec")
    error = httpx.HTTPStatusError("400", request=request, response=httpx.Response(400, request=request))
    handler = make_handler(30, [error])

    asyncio.run(handler._ensure_questdb_ttl())

    assert len(handler.queries) == 1


def test_transport_errors_propagate_for_retry():
    handler = make_handler(30, [httpx.ConnectError("refused")])

    with pytest.raises(httpx.ConnectError):
        asyncio.run(handler._ensure_questdb_ttl())
