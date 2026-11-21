#!/usr/bin/env python3
#
# tests/questdb_fake.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#
"""A QuestDB /exec endpoint behind httpx.MockTransport, so tests exercise the real package client."""

import inspect
from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

import httpx
from gillbates_questdb_client import AsyncQuestDBClient, QuestDBConfig


def fake_questdb(respond: Callable, *, config: QuestDBConfig | None = None) -> AsyncQuestDBClient:
    """Client whose /exec answers come from `respond(query)`.

    `respond` returns the JSON body (a dict), a plain string (a non-JSON 200 answer) or an exception: an `httpx.HTTPStatusError` becomes
    that HTTP status, a `RuntimeError` a 400 with the message as SQL error, any other `httpx.HTTPError`
    is raised from the transport (connection lost).
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlsplit(str(request.url)).query)["query"][0]
        answer = respond(query)
        if inspect.isawaitable(answer):
            answer = await answer
        if isinstance(answer, str):
            return httpx.Response(200, text=answer, request=request)
        if isinstance(answer, httpx.HTTPStatusError):
            return httpx.Response(answer.response.status_code, request=request)
        if isinstance(answer, RuntimeError):
            return httpx.Response(400, json={"error": str(answer), "position": 0}, request=request)
        if isinstance(answer, BaseException):
            raise answer
        return httpx.Response(200, json=answer, request=request)

    return AsyncQuestDBClient(config or QuestDBConfig(), transport=httpx.MockTransport(handler))


def status_error(code: int) -> httpx.HTTPStatusError:
    """An answer for `fake_questdb` that makes /exec reply with HTTP `code`."""
    request = httpx.Request("GET", "http://questdb:9000/exec")
    return httpx.HTTPStatusError(str(code), request=request, response=httpx.Response(code, request=request))
