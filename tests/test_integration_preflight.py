#!/usr/bin/env python3
#
# tests/test_integration_preflight.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Startup preflight against real InfluxDB 1.x/2.x and QuestDB throwaway containers.

Opt-in like tests/test_integration_dedup.py: set FRITZFLUXDB_INTEGRATION=1; images are never pulled.
"""
import asyncio
import configparser
import time
from urllib.parse import urlparse

import httpx
import pytest

from app.influxdb.handler import DatabaseConfigError, InfluxHandler
from tests.test_integration_dedup import (  # noqa: F401 - shared pytest fixture and opt-in marker
    INFLUXDB_V1_IMAGE,
    INFLUXDB_V2_IMAGE,
    QUESTDB_IMAGE,
    container_factory,
    pytestmark,
)

PASSWORD = "integration-pass"
TOKEN = "integration-token"


def _wait_until(check, what: str) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    pytest.fail(f"{what} did not become ready")


@pytest.fixture(scope="module")
def influx1(container_factory):  # noqa: F811
    env = {"INFLUXDB_HTTP_AUTH_ENABLED": "true", "INFLUXDB_ADMIN_USER": "admin",
           "INFLUXDB_ADMIN_PASSWORD": PASSWORD, "INFLUXDB_DB": "fritz"}
    url = container_factory(INFLUXDB_V1_IMAGE, 8086, env)
    _wait_until(lambda: "fritz" in httpx.get(
        f"{url}/query", params={"q": "SHOW DATABASES", "u": "admin", "p": PASSWORD}, timeout=3).text, "InfluxDB 1")
    return url


@pytest.fixture(scope="module")
def influx2(container_factory):  # noqa: F811
    env = {
        "DOCKER_INFLUXDB_INIT_MODE": "setup", "DOCKER_INFLUXDB_INIT_USERNAME": "admin",
        "DOCKER_INFLUXDB_INIT_PASSWORD": PASSWORD, "DOCKER_INFLUXDB_INIT_ORG": "o",
        "DOCKER_INFLUXDB_INIT_BUCKET": "b", "DOCKER_INFLUXDB_INIT_ADMIN_TOKEN": TOKEN,
    }
    url = container_factory(INFLUXDB_V2_IMAGE, 8086, env)
    _wait_until(lambda: httpx.get(f"{url}/health", timeout=3).status_code == 200, "InfluxDB 2")
    return url


@pytest.fixture(scope="module")
def questdb(container_factory):  # noqa: F811
    url = container_factory(QUESTDB_IMAGE, 9000, {"QDB_HTTP_USER": "admin", "QDB_HTTP_PASSWORD": PASSWORD})
    _wait_until(lambda: httpx.get(f"{url}/exec", params={"query": "select 1"}, auth=("admin", PASSWORD),
                                  timeout=3).status_code == 200, "QuestDB")
    return url


@pytest.fixture
def preflight(monkeypatch, no_database_env):
    previous: list[str] = []

    def run(db_type: str, url: str, **settings: str) -> None:
        # no_database_env cleared the host values; a setting from an earlier call must not leak into this one.
        for key in previous:
            monkeypatch.delenv(key, raising=False)
        parsed = urlparse(url)
        env = {"DB_TYPE": db_type, "INFLUXDB_HOSTNAME": parsed.hostname, "INFLUXDB_PORT": str(parsed.port),
               "INFLUXDB_ALLOW_PLAINTEXT_CREDENTIALS": "true", **settings}
        previous[:] = env
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        asyncio.run(InfluxHandler(configparser.ConfigParser()).preflight())

    return run


def test_influx1_preflight(preflight, influx1):
    good = {"INFLUXDB_USERNAME": "admin", "INFLUXDB_PASSWORD": PASSWORD, "INFLUXDB_DATABASE": "fritz"}
    preflight("influxdb_v1", influx1, **good)
    with pytest.raises(DatabaseConfigError, match="rejected"):
        preflight("influxdb_v1", influx1, **{**good, "INFLUXDB_PASSWORD": "wrong"})
    with pytest.raises(DatabaseConfigError, match="not found"):
        preflight("influxdb_v1", influx1, **{**good, "INFLUXDB_DATABASE": "missing"})


def test_influx2_preflight(preflight, influx2):
    good = {"INFLUXDB_TOKEN": TOKEN, "INFLUXDB_ORGANIZATION": "o", "INFLUXDB_BUCKET": "b"}
    preflight("influxdb_v2", influx2, **good)
    with pytest.raises(DatabaseConfigError, match="rejected"):
        preflight("influxdb_v2", influx2, **{**good, "INFLUXDB_TOKEN": "wrong"})
    with pytest.raises(DatabaseConfigError, match="not found"):
        preflight("influxdb_v2", influx2, **{**good, "INFLUXDB_BUCKET": "missing"})
    with pytest.raises(DatabaseConfigError, match="not found"):
        preflight("influxdb_v2", influx2, **{**good, "INFLUXDB_ORGANIZATION": "missing"})


def test_questdb_preflight(preflight, questdb):
    good = {"INFLUXDB_USERNAME": "admin", "INFLUXDB_PASSWORD": PASSWORD}
    preflight("questdb", questdb, **good)
    with pytest.raises(DatabaseConfigError, match="rejected"):
        preflight("questdb", questdb, **{**good, "INFLUXDB_PASSWORD": "wrong"})
