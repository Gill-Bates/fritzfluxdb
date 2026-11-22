#!/usr/bin/env python3
#
# tests/test_integration_dedup.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Event deduplication against real InfluxDB 1.x/2.x and QuestDB throwaway containers.

Opt-in: set FRITZFLUXDB_INTEGRATION=1. Every container is started here with a random name and a
loopback port, uses no volume and is removed afterwards. Images are never pulled; a missing
image or Docker skips the tests.
"""
import asyncio
import csv
import hashlib
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from urllib.parse import urlsplit

import httpx
import pytest
from gillbates_questdb_client import AsyncQuestDBClient, QuestDBConfig

from app.influxdb.handler import questdb_events_table_name
from app.utils.measurement import FritzMeasurement
from tests.test_event_deduplication import _real_call_list_buffer, make_handler

pytestmark = pytest.mark.skipif(
    os.environ.get("FRITZFLUXDB_INTEGRATION") != "1" or shutil.which("docker") is None,
    reason="set FRITZFLUXDB_INTEGRATION=1 and provide Docker to run the database integration tests",
)
TS = datetime(2026, 1, 1, 10, 0, 0).astimezone()
TOKEN = "integration-token"
# The database releases every integration module runs against; images are never pulled.
INFLUXDB_V1_IMAGE, INFLUXDB_V2_IMAGE, QUESTDB_IMAGE = "influxdb:1.11", "influxdb:2.7", "questdb/questdb:10.2.0"
CALL_FIELDS = ("type", "caller_name", "caller_number", "caller_location", "extension", "number_called", "duration")


def _docker(*args: str) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True, timeout=120).stdout.strip()


@pytest.fixture(scope="module")
def container_factory():
    started: list[str] = []

    def start(image: str, port: int, env: dict[str, str] | None = None) -> str:
        if subprocess.run(["docker", "image", "inspect", image], capture_output=True, check=False, timeout=120).returncode:
            pytest.skip(f"image {image} is not available locally")
        name = f"fff-it-{uuid.uuid4().hex[:8]}"
        env_args = [arg for key, value in (env or {}).items() for arg in ("-e", f"{key}={value}")]
        _docker("run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1::{port}", *env_args, image)
        started.append(name)
        host_port = _docker("port", name, str(port)).splitlines()[0].rsplit(":", 1)[1]
        return f"http://127.0.0.1:{host_port}"

    yield start
    for name in started:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False, timeout=120)


def _wait(url: str, params: dict | None = None) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, params=params, timeout=3).status_code < 300:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    pytest.fail(f"{url} did not become ready")


def _log_events(*texts: str) -> list[FritzMeasurement]:
    return [
        FritzMeasurement("log_entry", text, data_type=str, box_tag="b", timestamp=TS, is_event=True,
                         additional_tags={"log_type": "System",
                                          "uid": hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()})
        for text in texts
    ]


def _lines(version, measurements) -> str:
    return "\n".join(make_handler(version).bundle_measurements(measurements)[0])


# InfluxDB
@pytest.fixture(scope="module")
def influx1(container_factory):
    url = container_factory(INFLUXDB_V1_IMAGE, 8086)
    _wait(f"{url}/ping")
    httpx.post(f"{url}/query", params={"q": "CREATE DATABASE fritz"}).raise_for_status()
    return url


@pytest.fixture(scope="module")
def influx2(container_factory):
    env = {
        "DOCKER_INFLUXDB_INIT_MODE": "setup", "DOCKER_INFLUXDB_INIT_USERNAME": "admin",
        "DOCKER_INFLUXDB_INIT_PASSWORD": "integration-pass", "DOCKER_INFLUXDB_INIT_ORG": "o",
        "DOCKER_INFLUXDB_INIT_BUCKET": "b", "DOCKER_INFLUXDB_INIT_ADMIN_TOKEN": TOKEN,
    }
    url = container_factory(INFLUXDB_V2_IMAGE, 8086, env)
    _wait(f"{url}/health")
    return url


def _influx_write(version, url, payload):
    if version == 1:
        resp = httpx.post(f"{url}/write", params={"db": "fritz", "precision": "ns"}, content=payload)
    else:
        resp = httpx.post(f"{url}/api/v2/write", params={"org": "o", "bucket": "b", "precision": "ns"},
                          content=payload, headers={"Authorization": f"Token {TOKEN}"})
    assert resp.status_code == 204, resp.text


def _influx_count(version, url, field) -> int:
    if version == 1:
        data = httpx.get(f"{url}/query", params={"db": "fritz", "q": f"SELECT count({field}) FROM fritzbox_AA1"}).json()
        series = data["results"][0].get("series")
        return series[0]["values"][0][1] if series else 0
    flux = (f'from(bucket:"b") |> range(start:0) |> filter(fn:(r)=>r._field=="{field}") '
            "|> group() |> count()")
    resp = httpx.post(f"{url}/api/v2/query", params={"org": "o"}, content=flux, headers={
        "Authorization": f"Token {TOKEN}", "Content-Type": "application/vnd.flux", "Accept": "application/csv"})
    rows = [line for line in resp.text.splitlines() if line.strip() and not line.startswith(",result")]
    return int(rows[-1].split(",")[-1]) if rows else 0


@pytest.mark.parametrize("version", [1, 2])
def test_influx_repeated_events_are_stored_once(version, influx1, influx2):
    url = influx1 if version == 1 else influx2
    calls = _real_call_list_buffer(5)
    _influx_write(version, url, _lines(version, calls))
    # a restarted daemon re-reads the same entries and rebuilds the measurements from scratch
    _influx_write(version, url, _lines(version, _real_call_list_buffer(5)))
    for field in CALL_FIELDS:
        assert _influx_count(version, url, f"call_list_{field}") == 5


@pytest.mark.parametrize("version", [1, 2])
def test_influx_same_second_logs(version, influx1, influx2):
    url = influx1 if version == 1 else influx2
    for _ in range(2):
        _influx_write(version, url, _lines(version, _log_events("alpha", "beta", "gamma", "gamma")))
    # alpha and beta stay distinct; the two identical gamma messages are one event by design
    assert _influx_count(version, url, "log_entry") == 3


# QuestDB
@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("tag", ["a\\b", "a\\\\b", "a\\ b,=x"])
def test_influx_preserves_backslashes_in_tag_and_field_values(version, tag, influx1, influx2):
    url = influx1 if version == 1 else influx2
    value = 'c\\d "quoted"'
    measurement = FritzMeasurement("escape_probe", value, data_type=str, box_tag="b", timestamp=TS,
                                   additional_tags={"path": tag})
    _influx_write(version, url, _lines(version, [measurement]))
    if version == 1:
        response = httpx.get(f"{url}/query", params={"db": "fritz", "q": 'SELECT "path", "escape_probe" FROM "fritzbox_AA1"'})
        response.raise_for_status()
        series = response.json()["results"][0]["series"][0]
        rows = [dict(zip(series["columns"], row, strict=True)) for row in series["values"]]
        assert any(row.get("path") == tag and row.get("escape_probe") == value for row in rows), rows
    else:
        query = ('from(bucket: "b") |> range(start: 0) '
                 '|> filter(fn: (r) => r._measurement == "fritzbox_AA1" and r._field == "escape_probe") '
                 '|> keep(columns: ["path", "_value"])')
        response = httpx.post(f"{url}/api/v2/query", params={"org": "o"}, content=query,
                             headers={"Authorization": f"Token {TOKEN}", "Content-Type": "application/vnd.flux", "Accept": "application/csv"})
        response.raise_for_status()
        rows = list(csv.DictReader(line for line in response.text.splitlines() if line and not line.startswith("#")))
        assert any(row.get("path") == tag and row.get("_value") == value for row in rows), rows


# QuestDB
@pytest.fixture(scope="module")
def questdb(container_factory):
    url = container_factory(QUESTDB_IMAGE, 9000)
    _wait(f"{url}/exec", {"query": "select 1"})
    return url


def _qdb_query(url, sql):
    resp = httpx.get(f"{url}/exec", params={"query": sql}).json()
    assert "error" not in resp, resp
    return resp.get("dataset", [])


def _qdb_write(url, payload):
    assert httpx.post(f"{url}/write", params={"precision": "s"}, content=payload).status_code == 204


def _qdb_wait_rows(url, table, expected, where="true"):
    deadline = time.monotonic() + 30
    count = None
    while time.monotonic() < deadline:
        try:
            count = _qdb_query(url, f'select count() from "{table}" where {where}')[0][0]
        except AssertionError:
            count = None  # table not yet visible while the WAL applies
        if count == expected:
            return
        time.sleep(0.5)
    pytest.fail(f"{table}: expected {expected} rows, saw {count}")


def _qdb_wait_upsert_keys(url, table, expected):
    # ALTER on a WAL table is applied asynchronously; under load the metadata lags behind.
    deadline = time.monotonic() + 30
    keys = None
    while time.monotonic() < deadline:
        keys = {row[0] for row in _qdb_query(url, f"select \"column\" from table_columns('{table}') where upsertKey")}
        if keys == expected:
            return
        time.sleep(0.5)
    pytest.fail(f"{table}: expected upsert keys {sorted(expected)}, saw {sorted(keys or ())}")


def _qdb_handler(url, measurement):
    handler = make_handler("questdb")
    handler.config.measurement_name = measurement
    handler.config.data_retention_days = 0
    parts = urlsplit(url)
    handler._questdb = handler.client = AsyncQuestDBClient(QuestDBConfig(host=parts.hostname, port=parts.port))
    return handler


def _ensure_events_table(handler):
    async def run():
        try:
            return await handler._ensure_questdb_events_table()
        finally:
            await handler.client.aclose()
    return asyncio.run(run())


def test_questdb_events_table_is_a_wal_table_with_the_upsert_keys(questdb):
    handler = _qdb_handler(questdb, "it_keys")
    assert _ensure_events_table(handler) is True
    table = questdb_events_table_name("it_keys")
    _qdb_wait_upsert_keys(questdb, table, {"timestamp", "box", "log_type", "uid"})
    assert _qdb_query(questdb, f"select walEnabled from tables() where table_name = '{table}'") == [[True]]


def test_questdb_repeated_events_are_stored_once_with_all_columns(questdb):
    handler = _qdb_handler(questdb, "it_repeat")
    assert _ensure_events_table(handler) is True
    table = questdb_events_table_name("it_repeat")
    for _ in range(2):  # second pass models a restart: freshly built measurements, same events
        handler = _qdb_handler(questdb, "it_repeat")
        _qdb_write(questdb, "\n".join(handler.bundle_measurements(
            _real_call_list_buffer(5) + _log_events("alpha", "beta", "gamma", "gamma"))[0]))
    # 5 calls (log_type NULL, in the upsert key) + 3 distinct log events
    _qdb_wait_rows(questdb, table, 8)
    time.sleep(2)
    _qdb_wait_rows(questdb, table, 8)
    nulls = " or ".join(f"call_list_{field} is null" for field in CALL_FIELDS if field != "duration")
    _qdb_wait_rows(questdb, table, 0, f"log_type is null and ({nulls} or call_list_duration is null)")


def test_questdb_partial_upsert_replaces_the_row_and_loses_columns(questdb):
    # Characterises why one event must travel in one request: the upsert writes the whole row.
    handler = _qdb_handler(questdb, "it_partial")
    assert _ensure_events_table(handler) is True
    table = questdb_events_table_name("it_partial")
    calls = _real_call_list_buffer(1)
    _qdb_write(questdb, "\n".join(handler.bundle_measurements(calls)[0]))
    _qdb_wait_rows(questdb, table, 1, "call_list_caller_name is not null")
    _qdb_write(questdb, "\n".join(handler.bundle_measurements(calls[:1])[0]))
    _qdb_wait_rows(questdb, table, 1, "call_list_caller_name is null and call_list_type is not null")


def test_questdb_existing_table_gets_dedup_but_keeps_old_duplicates(questdb):
    measurement = "it_legacy"
    table = questdb_events_table_name(measurement)
    handler = _qdb_handler(questdb, measurement)
    payload = "\n".join(handler.bundle_measurements(_log_events("alpha"))[0])
    for _ in range(2):  # ILP auto-created the table and wrote duplicates
        _qdb_write(questdb, payload)
        time.sleep(1)
    _qdb_wait_rows(questdb, table, 2)
    assert _ensure_events_table(handler) is True
    _qdb_wait_upsert_keys(questdb, table, {"timestamp", "box", "log_type", "uid"})
    _qdb_write(questdb, payload)
    time.sleep(2)
    # enabling DEDUP does not remove the earlier duplicates, and a new repeat adds nothing
    _qdb_wait_rows(questdb, table, 2)
