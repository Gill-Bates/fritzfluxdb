#!/usr/bin/env python3
#
# tests/test_questdb_downsampling.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
import configparser
import logging
import re
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

import app.influxdb.handler as handler_module
from app.influxdb.config import (
    QUESTDB_DOWNSAMPLING_PROFILES,
    InfluxDBConfig,
)
from app.influxdb.handler import (
    QUESTDB_EVENT_UPSERT_KEYS,
    QUESTDB_MAX_QUERY_URL_BYTES,
    QUESTDB_ROLLUP_GROUP_COUNT,
    InfluxHandler,
    QuestDBMetricClass,
    QuestDBStatementTooLongError,
    _format_exc,
    questdb_events_table_name,
    questdb_exec_query_string,
    questdb_expected_columns,
    questdb_metric_class,
    questdb_metrics_table_name,
    questdb_raw_retention_days,
    questdb_rollup_ddl,
    questdb_rollup_group,
    questdb_rollup_groups,
    questdb_rollup_view_columns,
    questdb_rollup_view_name,
)
from app.utils.measurement import FritzMeasurement, WritePrecision

pytestmark = pytest.mark.usefixtures("no_database_env")


def make_config(monkeypatch, version: str, downsampling: str | None) -> InfluxDBConfig:
    monkeypatch.setenv("DB_TYPE", version)
    if downsampling is not None:
        monkeypatch.setenv("QUESTDB_DOWNSAMPLING", downsampling)

    data = configparser.ConfigParser()
    data.add_section("influxdb")
    data.set("influxdb", "hostname", "localhost")
    if version == "influxdb_v1":
        data.set("influxdb", "database", "fritzbox")
    elif version == "influxdb_v2":
        data.set("influxdb", "token", "token")
        data.set("influxdb", "organization", "org")
        data.set("influxdb", "bucket", "bucket")

    return InfluxDBConfig(data)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_downsampling_missing_or_empty_is_disabled(monkeypatch, value):
    config = make_config(monkeypatch, "questdb", value)

    assert config.downsampling == ""
    assert config.parser_error is False


@pytest.mark.parametrize("profile", ["low", "medium", "high"])
def test_valid_downsampling_profiles(monkeypatch, profile):
    config = make_config(monkeypatch, "questdb", profile.upper())

    assert config.downsampling == profile
    assert config.parser_error is False


def test_invalid_downsampling_profile_is_a_parser_error(monkeypatch):
    config = make_config(monkeypatch, "questdb", "custom")

    assert config.parser_error is True


def test_questdb_oss_rejects_api_token(monkeypatch):
    monkeypatch.setenv("QUESTDB_TOKEN", "old-token")

    config = make_config(monkeypatch, "questdb", None)

    assert config.parser_error is True


@pytest.mark.parametrize("version", ["influxdb_v1", "influxdb_v2"])
def test_influxdb_ignores_questdb_downsampling(monkeypatch, version):
    config = make_config(monkeypatch, version, "invalid")

    assert config.downsampling == ""
    assert config.parser_error is False


@pytest.mark.parametrize(
    ("name", "column_type", "expected"),
    [
        ("cpu_temp", "LONG", QuestDBMetricClass.GAUGE),
        ("totalbytessent", "LONG", QuestDBMetricClass.COUNTER),
        ("ha_powermeter_energy", "DOUBLE", QuestDBMetricClass.COUNTER),
        ("ha_switch_state", "LONG", QuestDBMetricClass.STATE),
        ("message", "VARCHAR", QuestDBMetricClass.EVENT),
        ("future_metric", "LONG", QuestDBMetricClass.UNKNOWN),
    ],
)
def test_metric_classification(name, column_type, expected):
    assert questdb_metric_class(name, column_type) is expected


def test_rollup_ddl_is_reaggregatable_and_excludes_states_and_events():
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    ddl = questdb_rollup_ddl(
        'fritzbox "main"',
        profile,
        90,
        questdb_rollup_group("cpu_temp"),
        {
            "box": "SYMBOL",
            "cpu_temp": "LONG",
            "totalbytessent": "LONG",
            "ha_switch_state": "LONG",
            "message": "VARCHAR",
        },
    )

    assert 'CREATE MATERIALIZED VIEW IF NOT EXISTS "fritzbox ""main""_rollup_1m_g04_v1"' in ddl
    assert 'WITH BASE "fritzbox ""main""" REFRESH IMMEDIATE AS (' in ddl
    assert 'sum(cpu_temp) AS cpu_temp_sum' in ddl
    assert 'count(cpu_temp) AS cpu_temp_count' in ddl
    assert "cpu_temp_min" not in ddl
    assert "cpu_temp_max" not in ddl
    assert "cpu_temp_last" not in ddl
    counter_ddl = questdb_rollup_ddl(
        'fritzbox "main"', profile, 90, questdb_rollup_group("totalbytessent"),
        {"totalbytessent": "LONG", "cpu_temp": "LONG"},
    )
    assert 'last(totalbytessent) AS totalbytessent_last' in counter_ddl
    assert "cpu_temp" not in counter_ddl
    assert "ha_switch_state" not in ddl
    assert "message" not in ddl
    assert "SAMPLE BY 1m ALIGN TO CALENDAR" in ddl
    assert ") PARTITION BY DAY TTL 90 DAYS;" in ddl


def test_zero_total_retention_omits_rollup_ttl_and_keeps_profile_raw_ttl():
    profile = QUESTDB_DOWNSAMPLING_PROFILES["high"]

    ddl = questdb_rollup_ddl("fritzbox", profile, 0, questdb_rollup_group("cpu_temp"), {"cpu_temp": "LONG"})

    assert ddl.endswith(") PARTITION BY DAY;")
    assert questdb_raw_retention_days(profile, 0) == 1


def test_total_retention_caps_profile_raw_ttl():
    profile = QUESTDB_DOWNSAMPLING_PROFILES["low"]

    assert questdb_raw_retention_days(profile, 5) == 5
    assert questdb_raw_retention_days(profile, 90) == 30


def make_handler(profile_name: str = "medium") -> InfluxHandler:
    handler = InfluxHandler.__new__(InfluxHandler)
    handler.version = "questdb"
    handler.config = SimpleNamespace(
        measurement_name="fritzbox_AA1",
        data_retention_days=90,
        downsampling=profile_name,
    )
    handler.client = object()
    handler.questdb_schema_ensured = False
    handler._questdb_downsampling_retry_task = None
    handler._write_lock = asyncio.Lock()
    handler._rollup_failures = {}
    handler._rollup_ready_views = set()
    handler._rollup_incomplete = {}
    return handler


def test_earlier_schema_failure_does_not_inherit_a_permanent_rollup_failure():
    handler = make_handler()
    handler._rollup_failure_permanent = True

    async def execute(query, *, auth=None):
        return {"dataset": []}

    async def unavailable_events(*, auth=None):
        return False

    handler._questdb_exec = execute
    handler._ensure_questdb_events_table = unavailable_events
    assert asyncio.run(handler._ensure_questdb_schema()) is False
    assert handler._rollup_failure_permanent is False


def queried_view(query):
    return re.search(r"view_name = '([^']+)'", query).group(1)


def view_columns_result(query):
    """Complete column list of the group view named in a table_columns() query."""
    group = int(re.search(r"_g(\d\d)_v1", query).group(1))
    return {"dataset": [[c] for c in questdb_rollup_view_columns(questdb_rollup_groups()[group])]}


def state_row(profile="medium", raw_days=7, rollup_view="fritzbox_AA1_metrics_rollup_1m", managed=False, table_id=None):
    return [profile, 1, True, raw_days, "1m", rollup_view, managed, table_id]


def test_ready_rollup_is_idempotently_provisioned_and_validated():
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    queries = []

    async def fake_exec(query, *, auth=None):
        queries.append(query)
        if "table_columns(" in query:
            return view_columns_result(query)
        if "materialized_views()" in query:
            return {"dataset": [[queried_view(query), "fritzbox_AA1_metrics", "valid", 12, 12]]}
        if "tables()" in query:
            return {"dataset": [[90, "DAY"]]}
        return {"ddl": "OK"}

    handler._questdb_exec = fake_exec

    assert asyncio.run(handler._ensure_questdb_downsampling(profile)) is True
    assert queries[0].startswith("CREATE MATERIALIZED VIEW IF NOT EXISTS")
    assert "REFRESH IMMEDIATE" in queries[0]
    assert "materialized_views()" in queries[1]
    assert len(queries) == 4 * len(questdb_rollup_groups())


def test_pending_initial_refresh_does_not_touch_any_ttl():
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    queries = []

    async def fake_exec(query, *, auth=None):
        queries.append(query)
        if "table_columns(" in query:
            return view_columns_result(query)
        if "materialized_views()" in query:
            return {"dataset": [[queried_view(query), "fritzbox_AA1_metrics", "valid", 3, 8]]}
        return {"ddl": "OK"}

    handler._questdb_exec = fake_exec

    assert asyncio.run(handler._ensure_questdb_downsampling(profile)) is False
    assert len(queries) == 2 * len(questdb_rollup_groups())
    assert all("SET TTL" not in query and "tables()" not in query for query in queries)


@pytest.mark.parametrize(("view_status", "level"), [("refreshing", logging.INFO), ("invalid", logging.WARNING)])
def test_unfinished_rollup_is_logged_by_its_status(caplog, view_status, level):
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]

    async def fake_exec(query, *, auth=None):
        if "materialized_views()" in query:
            return {"dataset": [[queried_view(query), "fritzbox_AA1_metrics", view_status, -1, 8]]}
        return {"ddl": "OK"}

    handler._questdb_exec = fake_exec
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(handler._ensure_questdb_downsampling(profile)) is False

    records = [r for r in caplog.records if "is not fully refreshed" in r.getMessage()]
    assert records
    assert {r.levelno for r in records} == {level}


def test_downsampling_sql_error_is_tolerated():
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]

    async def fake_exec(query, *, auth=None):
        raise RuntimeError("unsupported")

    handler._questdb_exec = fake_exec

    assert asyncio.run(handler._ensure_questdb_downsampling(profile)) is False


def test_profile_change_uses_new_view_without_dropping_old_view():
    low_view = questdb_rollup_view_name("fritzbox", QUESTDB_DOWNSAMPLING_PROFILES["low"], 3)
    high_view = questdb_rollup_view_name("fritzbox", QUESTDB_DOWNSAMPLING_PROFILES["high"], 3)

    assert low_view == "fritzbox_rollup_1m_g03_v1"
    assert high_view == "fritzbox_rollup_5m_g03_v1"
    assert low_view != high_view


def finalize_exec(queries, raw_ttl, state, *, alter_fails=False):
    """Fake /exec that tracks the raw TTL so a verification read sees the applied value."""
    ttl = {"value": raw_ttl[0], "unit": raw_ttl[1]}

    async def fake_exec(query, *, auth=None):
        queries.append(query)
        if "table_columns(" in query and "_rollup_" in query:
            return view_columns_result(query)
        if "table_columns(" in query:
            return {"dataset": [[key] for key in QUESTDB_EVENT_UPSERT_KEYS]}
        if "_events" in query and "ttlValue" in query:
            return {"dataset": [[None, None]]}
        if "_events" in query and "SET TTL" in query:
            return {"ddl": "OK"}
        if "materialized_views()" in query:
            return {"dataset": [[queried_view(query), "fritzbox_AA1_metrics", "valid", 5, 5]]}
        if "LATEST ON timestamp" in query:
            return {"dataset": [state] if state else []}
        if "SELECT table_name FROM tables()" in query:
            return {"dataset": [["_fritzfluxdb_downsampling"]]}
        if query.startswith("SELECT id FROM tables()"):
            return {"dataset": [[42]]}
        if "ttlValue" in query and "_rollup_" in query:
            return {"dataset": [[90, "DAY"]]}
        if "ttlValue" in query:
            return {"dataset": [[ttl["value"], ttl["unit"]]]}
        if "SET TTL" in query and "_rollup_" not in query:
            if alter_fails:
                raise RuntimeError("not permitted")
            ttl["value"] = int(query.split("SET TTL ")[1].split(" DAYS")[0])
            ttl["unit"] = "DAYS"
        return {"ddl": "OK"}

    return fake_exec


def test_schema_shortens_raw_ttl_only_after_rollup_is_current(monkeypatch):
    handler = make_handler()
    queries = []
    monkeypatch.setattr(
        "app.influxdb.handler.questdb_expected_columns",
        lambda: {"cpu_temp": "LONG"},
    )
    handler._questdb_exec = finalize_exec(queries, (0, None), None)

    assert asyncio.run(handler._ensure_questdb_schema()) is True
    raw_ttl_query = 'ALTER TABLE "fritzbox_AA1_metrics" SET TTL 7 DAYS;'
    assert raw_ttl_query in queries
    assert queries.index(raw_ttl_query) > next(i for i, query in enumerate(queries) if "materialized_views()" in query)
    insert_index = next(i for i, query in enumerate(queries) if 'INSERT INTO "_fritzfluxdb_downsampling"' in query)
    assert insert_index > queries.index(raw_ttl_query)
    assert "'medium', 1, true, 7, '1m', 'fritzbox_AA1_metrics_rollup_1m', true" in queries[insert_index]


def test_table_names_derive_from_one_metrics_suffix():
    assert questdb_metrics_table_name("fritzbox_50E63634FEA8") == "fritzbox_50E63634FEA8_metrics"
    assert questdb_events_table_name("fritzbox_50E63634FEA8") == "fritzbox_50E63634FEA8_events"
    assert questdb_metrics_table_name("fritzbox") == "fritzbox_metrics"
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    view = questdb_rollup_view_name(questdb_metrics_table_name("fritzbox_50E63634FEA8"), profile, 4)
    assert view == "fritzbox_50E63634FEA8_metrics_rollup_1m_g04_v1"


def test_schema_state_ttl_and_ownership_use_the_metrics_table_only(monkeypatch):
    handler = make_handler()
    queries = []
    monkeypatch.setattr("app.influxdb.handler.questdb_expected_columns", lambda: {"cpu_temp": "LONG"})
    handler._questdb_exec = finalize_exec(queries, (0, None), None)
    assert asyncio.run(handler._ensure_questdb_schema()) is True
    assert any(q.startswith('CREATE TABLE IF NOT EXISTS "fritzbox_AA1_metrics" ') for q in queries)
    assert any(q.startswith('CREATE TABLE IF NOT EXISTS "fritzbox_AA1_events" ') for q in queries)
    assert not any('"fritzbox_AA1"' in q or "'fritzbox_AA1'" in q for q in queries)
    # The ownership id and the status row are keyed by the metrics table.
    assert "SELECT id FROM tables() WHERE table_name = 'fritzbox_AA1_metrics';" in queries
    assert any("measurement = 'fritzbox_AA1_metrics'" in q for q in queries)
    assert any("now(), 'fritzbox_AA1_metrics'," in q for q in queries if q.startswith("INSERT INTO"))


def test_ttl_of_a_foreign_status_row_id_is_not_adopted_for_the_metrics_table():
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    queries = []
    # The managed flag is only trusted when the stored id equals the metrics table id (42).
    handler._questdb_exec = finalize_exec(
        queries, (30, "DAY"), state_row(profile="low", raw_days=30, managed=True, table_id=7)
    )
    assert asyncio.run(handler._finalize_questdb_downsampling("medium", profile)) is True
    assert not any('ALTER TABLE "fritzbox_AA1_metrics" SET TTL' in q for q in queries)


def test_administrator_raw_ttl_is_kept_and_published_as_effective_history():
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    queries = []
    handler._questdb_exec = finalize_exec(queries, (90, "DAY"), None)

    assert asyncio.run(handler._finalize_questdb_downsampling("medium", profile)) is True
    assert not any('ALTER TABLE "fritzbox_AA1_metrics" SET TTL' in query for query in queries)
    assert any(
        'INSERT INTO "_fritzfluxdb_downsampling"' in query
        and "'medium', 1, true, 90, '1m', 'fritzbox_AA1_metrics_rollup_1m', false" in query
        for query in queries
    )


def test_managed_raw_ttl_is_shortened_on_profile_change():
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    queries = []
    handler._questdb_exec = finalize_exec(
        queries,
        (30, "DAY"),
        state_row(profile="low", raw_days=30, managed=True, table_id=42),
    )

    assert asyncio.run(handler._finalize_questdb_downsampling("medium", profile)) is True
    assert 'ALTER TABLE "fritzbox_AA1_metrics" SET TTL 7 DAYS;' in queries
    assert any(
        'INSERT INTO "_fritzfluxdb_downsampling"' in query
        and "'medium', 1, true, 7, '1m', 'fritzbox_AA1_metrics_rollup_1m', true" in query
        for query in queries
    )


def test_failed_raw_ttl_change_publishes_no_active_state():
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    queries = []
    handler._questdb_exec = finalize_exec(queries, (0, None), None, alter_fails=True)

    assert asyncio.run(handler._finalize_questdb_downsampling("medium", profile)) is False
    assert not any('INSERT INTO "_fritzfluxdb_downsampling"' in query for query in queries)


def test_disabled_downsampling_keeps_legacy_ttl_path(monkeypatch):
    handler = make_handler(profile_name="")
    queries = []
    monkeypatch.setattr("app.influxdb.handler.questdb_expected_columns", dict)

    async def fake_exec(query, *, auth=None):
        queries.append(query)
        if "SELECT table_name FROM tables()" in query:
            return {"dataset": [["_fritzfluxdb_downsampling"]]}
        if "LATEST ON timestamp" in query:
            return {"dataset": [["disabled", 1, False, 0, "", "", False, None]]}
        if "tables()" in query:
            return {"dataset": [[90, "DAY"]]}
        return {"ddl": "OK"}

    handler._questdb_exec = fake_exec

    assert asyncio.run(handler._ensure_questdb_schema()) is True
    assert not any("MATERIALIZED VIEW" in query for query in queries)
    assert not any("ALTER TABLE" in query and "SET TTL" in query for query in queries)


def test_disabling_publishes_versioned_state_without_changing_ttl():
    handler = make_handler(profile_name="")
    queries = []

    async def fake_exec(query, *, auth=None):
        queries.append(query)
        if "SELECT table_name FROM tables()" in query:
            return {"dataset": [["_fritzfluxdb_downsampling"]]}
        if "LATEST ON timestamp" in query:
            return {"dataset": [state_row(managed=True)]}
        return {"ddl": "OK"}

    handler._questdb_exec = fake_exec

    assert asyncio.run(handler._set_questdb_downsampling_state(
        enabled=False,
        profile_name="disabled",
        raw_ttl_managed=True,
    )) is True
    assert any(
        'INSERT INTO "_fritzfluxdb_downsampling"' in query
        and "'disabled', 1, false, 0, '', '', true" in query
        for query in queries
    )
    assert not any("SET TTL" in query for query in queries)


def test_repeated_state_does_not_append_duplicate_row():
    handler = make_handler()
    queries = []

    async def fake_exec(query, *, auth=None):
        queries.append(query)
        if "LATEST ON timestamp" in query:
            return {"dataset": [state_row(managed=True)]}
        return {"ddl": "OK"}

    handler._questdb_exec = fake_exec

    assert asyncio.run(handler._set_questdb_downsampling_state(
        enabled=True,
        profile_name="medium",
        raw_retention_days=7,
        raw_ttl_managed=True,
        rollup_interval="1m",
        rollup_view="fritzbox_AA1_metrics_rollup_1m",
    )) is True
    assert "measurement SYMBOL, profile SYMBOL, schema_version LONG" in queries[0]
    assert not any("ADD COLUMN IF NOT EXISTS raw_ttl_managed" in query for query in queries)
    assert any("LATEST ON timestamp PARTITION BY measurement" in query for query in queries)
    assert not any(query.startswith("INSERT INTO") for query in queries)


def _state_with_table_id(stored_id, actual_id):
    handler = make_handler()
    queries = []

    async def fake_exec(query, *, auth=None):
        queries.append(query)
        if "LATEST ON timestamp" in query:
            return {"dataset": [[*state_row(managed=True)[:7], stored_id]]}
        if query.startswith("SELECT id FROM tables()"):
            return {"dataset": [[actual_id]]}
        return {"ddl": "OK"}

    handler._questdb_exec = fake_exec
    assert asyncio.run(handler._set_questdb_downsampling_state(
        enabled=True,
        profile_name="medium",
        raw_retention_days=7,
        raw_ttl_managed=True,
        rollup_interval="1m",
        rollup_view="fritzbox_AA1_metrics_rollup_1m",
    )) is True
    return [query for query in queries if query.startswith("INSERT INTO")]


def test_state_row_is_rewritten_with_the_id_of_a_recreated_table():
    # A row without id (id unreadable when written) and a row of a dropped predecessor get a fresh row.
    for stored_id in (None, 10):
        inserts = _state_with_table_id(stored_id, 42)
        assert len(inserts) == 1
        assert inserts[0].endswith("true, 42);")


def test_state_row_with_matching_table_id_is_not_duplicated():
    assert _state_with_table_id(42, 42) == []


def test_owned_object_follows_configured_retention_and_unlimited_removes_ttl(caplog):
    handler = make_handler()
    queries = []

    async def fake_exec(query, *, auth=None):
        queries.append(query)
        return {"dataset": [[90, "DAY"]]}

    handler._questdb_exec = fake_exec

    with caplog.at_level("WARNING"):
        assert asyncio.run(handler._set_questdb_table_ttl("fritzbox_AA1_events", 7)) is True
    assert 'ALTER TABLE "fritzbox_AA1_events" SET TTL 7 DAYS;' in queries
    assert "90 DAY to 7 day(s)" in caplog.text

    queries.clear()
    assert asyncio.run(handler._set_questdb_table_ttl("fritzbox_AA1_events", 90)) is True
    assert len(queries) == 1  # already equal: no ALTER

    assert asyncio.run(handler._set_questdb_table_ttl("v", 0, materialized_view=True)) is True
    assert queries[-1] == 'ALTER MATERIALIZED VIEW "v" SET TTL 0 HOURS;'


def test_rollup_rejects_unclassified_numeric_only_schema():
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]

    with pytest.raises(ValueError, match="explicitly classified metric"):
        questdb_rollup_ddl("fritzbox", profile, 90, 0, {"future_numeric": "LONG"})


def test_bounded_retry_completes_after_async_refresh():
    handler = make_handler()
    handler.downsampling_retry_attempts = 3
    handler.downsampling_retry_interval = 0
    results = iter([False, True])
    attempts = []

    async def fake_ensure(*, auth=None):
        attempts.append((handler.config.downsampling, "1m"))
        return next(results)

    handler._ensure_questdb_schema = fake_ensure

    async def exercise():
        handler._schedule_questdb_downsampling_retry()
        task = handler._questdb_downsampling_retry_task
        assert task is not None
        await task

    asyncio.run(exercise())

    assert attempts == [("medium", "1m"), ("medium", "1m")]
    assert handler.questdb_schema_ensured is True
    assert handler._questdb_downsampling_retry_task is None


def test_bounded_retry_stops_after_configured_attempts():
    handler = make_handler()
    handler.downsampling_retry_attempts = 2
    handler.downsampling_retry_interval = 0
    attempts = []

    async def fake_ensure(*, auth=None):
        attempts.append(handler.config.downsampling)
        return False

    handler._ensure_questdb_schema = fake_ensure

    asyncio.run(handler._retry_questdb_downsampling_setup())

    assert attempts == ["medium", "medium"]
    assert handler.questdb_schema_ensured is False


def test_rollup_ttl_mismatch_after_retention_change_is_aligned():
    handler = make_handler()
    handler.config.data_retention_days = 365
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    queries = []

    async def fake_exec(query, *, auth=None):
        queries.append(query)
        if "table_columns(" in query:
            return view_columns_result(query)
        if "materialized_views()" in query:
            return {"dataset": [[queried_view(query), "fritzbox_AA1_metrics", "valid", 5, 5]]}
        if "ttlValue" in query:
            return {"dataset": [[30, "DAY"]]}
        return {"ddl": "OK"}

    handler._questdb_exec = fake_exec

    asyncio.run(handler._finalize_questdb_downsampling("medium", profile))
    assert any(q.startswith("ALTER MATERIALIZED VIEW") and q.endswith("SET TTL 365 DAYS;") for q in queries)


def test_failed_schema_step_is_repaired_by_the_retry_without_reconnecting(monkeypatch):
    handler = make_handler()
    handler.downsampling_retry_attempts = 2
    handler.downsampling_retry_interval = 0
    monkeypatch.setattr("app.influxdb.handler.questdb_expected_columns", lambda: {"cpu_temp": "LONG"})
    queries = []
    inner = finalize_exec(queries, (0, None), None)
    outage = {"active": True}

    async def fake_exec(query, *, auth=None):
        if outage["active"] and "ADD COLUMN IF NOT EXISTS \"cpu_temp\"" in query:
            outage["active"] = False
            raise RuntimeError("temporary failure")
        return await inner(query, auth=auth)

    handler._questdb_exec = fake_exec

    async def exercise():
        with pytest.raises(RuntimeError):
            await handler._ensure_questdb_schema()
        handler._schedule_questdb_downsampling_retry()
        await handler._questdb_downsampling_retry_task

    asyncio.run(exercise())

    assert handler.questdb_schema_ensured is True
    assert sum("ADD COLUMN IF NOT EXISTS \"cpu_temp\"" in query for query in queries) == 1
    assert any(query.startswith("INSERT INTO") for query in queries)


def make_writer():
    handler = InfluxHandler.__new__(InfluxHandler)
    handler.version = "questdb"
    handler.name = "QuestDB"
    handler.config = SimpleNamespace(measurement_name="fritzbox", username=None, hostname="db")
    handler.buffer = []
    handler.last_write_retry = None
    handler.out_of_retention_period_range = False
    handler.retention_buffer_sorted = False
    handler.connection_lost = False
    handler.last_connection_warning = None
    handler.current_retry_interval = handler.retry_interval
    handler.current_measurements_per_write = 10
    handler.posts = []

    class Client:
        async def post(self, url, *, params, content, auth):
            handler.posts.append((params, content))
            return SimpleNamespace(status_code=204)

    handler.client = Client()
    return handler


@pytest.mark.parametrize(
    ("precision", "written"),
    [(WritePrecision.S, True), (WritePrecision.MS, False), (WritePrecision.US, False)],
)
def test_writer_accepts_only_second_precision(precision, written):
    handler = make_writer()
    handler.buffer.append(FritzMeasurement("cpu_temp", 5, box_tag="AA1", timestamp_precision=precision))

    asyncio.run(handler._write_data_unlocked())

    assert [params for params, _ in handler.posts] == ([{"precision": "s"}] if written else [])
    assert handler.buffer == []


def test_shutdown_reports_buffered_measurements_that_were_never_persisted():
    handler = make_writer()
    handler.max_measurements_buffer_warning = 80
    handler.current_max_measurements_buffer_warning = 80
    handler._questdb_downsampling_retry_task = None
    handler._write_lock = asyncio.Lock()
    handler.client = None
    queue = asyncio.Queue()
    queue.put_nowait(FritzMeasurement("cpu_temp", 5, box_tag="AA1"))

    async def unreachable_write(*, force=False):
        return None

    handler.write_data = unreachable_write

    async def exercise():
        task = asyncio.create_task(handler.task_loop(queue))
        await asyncio.wait_for(queue.join(), timeout=2)  # only confirms hand-over to the buffer
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    with patch.object(handler_module.log, "error") as error:
        asyncio.run(exercise())

    assert len(handler.buffer) == 1
    assert "unwritten" in error.call_args.args[0]
    assert error.call_args.args[1] == 1


def questdb_url_config(monkeypatch, hostname="localhost", env=None):
    monkeypatch.setenv("DB_TYPE", "questdb")
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    data = configparser.ConfigParser()
    data.add_section("influxdb")
    data.set("influxdb", "hostname", hostname)
    return InfluxDBConfig(data)


@pytest.mark.parametrize(
    "hostname",
    ["https://database.example:invalid", "https://database.example:70000", "https://[::1", "http://[::1]:x"],
)
def test_invalid_url_port_is_a_controlled_configuration_error(monkeypatch, hostname):
    assert questdb_url_config(monkeypatch, hostname).parser_error is True


def test_valid_url_port_is_applied(monkeypatch):
    config = questdb_url_config(monkeypatch, "https://database.example:8443")

    assert config.parser_error is False
    assert (config.hostname, config.port, config.tls_enabled) == ("database.example", 8443, True)


@pytest.mark.parametrize("name", ["QUESTDB_HOST", "QUESTDB_SSL", "QUESTDB_VERIFY_SSL"])
def test_removed_questdb_aliases_are_ignored(monkeypatch, name):
    config = questdb_url_config(monkeypatch, env={name: "other"})

    assert config.hostname == "localhost"
    assert (config.tls_enabled, config.verify_tls) == (False, True)


def test_removed_ini_aliases_are_ignored():
    data = configparser.ConfigParser()
    data.read_dict({"influxdb": {"host": "alias.db", "ssl": "true", "verify_ssl": "false"}})

    assert InfluxDBConfig.collect_raw(data) == {}


ROLLUP_URL_TARGET_BYTES = 4_500


def synthetic_columns(count):
    return {f"metric_{index:04d}_value_xx": "DOUBLE" for index in range(count)}


@pytest.mark.parametrize("count", [56, 200, 500])
def test_every_group_statement_stays_small_regardless_of_column_count(monkeypatch, count):
    columns = synthetic_columns(count)
    monkeypatch.setattr(handler_module, "QUESTDB_GAUGE_METRICS", frozenset(columns))
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    groups = questdb_rollup_groups(columns)

    sizes = [
        len(questdb_exec_query_string(questdb_rollup_ddl("fritzbox_50E63634FEA8", profile, 365, group, columns)))
        for group in groups
    ]

    assert sum(len(members) for members in groups.values()) == count
    assert max(sizes) <= ROLLUP_URL_TARGET_BYTES < QUESTDB_MAX_QUERY_URL_BYTES


def test_real_columns_fit_far_below_the_hard_limit_and_a_long_measurement_name():
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    table = "fritzbox_" + "A" * 120

    for group in questdb_rollup_groups():
        ddl = questdb_rollup_ddl(table, profile, 365, group)
        assert "\n" not in ddl
        assert len(questdb_exec_query_string(ddl)) < 2_500


def test_group_assignment_is_stable_complete_and_without_duplicates():
    # Fixed expectations: a changed hash or group count would silently orphan existing views.
    assert QUESTDB_ROLLUP_GROUP_COUNT == 16
    assert {name: questdb_rollup_group(name) for name in ("cpu_temp", "totalbytessent", "sendrate", "wlan1_channel")} == {
        "cpu_temp": 4, "totalbytessent": 13, "sendrate": 10, "wlan1_channel": 14,
    }
    columns = questdb_expected_columns()
    groups = questdb_rollup_groups()
    members = [name for group in groups.values() for name in group]
    expected = [
        name for name, column_type in columns.items()
        if questdb_metric_class(name, column_type) in {QuestDBMetricClass.GAUGE, QuestDBMetricClass.COUNTER}
    ]
    assert sorted(members) == sorted(expected)
    assert len(members) == len(set(members))
    assert all(questdb_rollup_group(name) == group for group, names in groups.items() for name in names)
    assert all(0 <= group < QUESTDB_ROLLUP_GROUP_COUNT for group in groups)


def test_new_metric_does_not_move_existing_columns():
    before = {name: questdb_rollup_group(name) for name in questdb_expected_columns()}
    after = {name: questdb_rollup_group(name) for name in {**questdb_expected_columns(), "new_gauge": "DOUBLE"}}

    assert all(after[name] == group for name, group in before.items())


def test_provisioning_creates_every_group_view_and_one_failure_does_not_block_the_others(caplog):
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    groups = questdb_rollup_groups()
    failing = questdb_rollup_view_name(handler._questdb_metrics_table, profile, next(iter(groups)))
    created = []

    async def fake_exec(query, *, auth=None):
        if query.startswith("CREATE MATERIALIZED VIEW"):
            created.append(re.search(r'EXISTS "([^"]+)"', query).group(1))
            if f'"{failing}"' in query:
                raise RuntimeError("view rejected")
        if "table_columns(" in query:
            return view_columns_result(query)
        if "materialized_views()" in query:
            return {"dataset": [[queried_view(query), "fritzbox_AA1_metrics", "valid", 5, 5]]}
        if "ttlValue" in query:
            return {"dataset": [[90, "DAY"]]}
        return {"ddl": "OK"}

    handler._questdb_exec = fake_exec
    with caplog.at_level("DEBUG"):
        assert asyncio.run(handler._ensure_questdb_downsampling(profile)) is False
        assert asyncio.run(handler._ensure_questdb_downsampling(profile)) is False

    assert created.count(failing) == 2
    assert len(set(created)) == len(groups)
    errors = [r.getMessage() for r in caplog.records
              if r.levelname == "ERROR" and "Unable to provision" in r.getMessage()]
    assert len(errors) == 1 and failing in errors[0]
    assert handler._rollup_failure_permanent is False
    assert set(handler._rollup_failures) == {failing}


def test_oversized_statement_is_rejected_before_sending():
    with pytest.raises(QuestDBStatementTooLongError):
        questdb_exec_query_string("SELECT " + "x" * QUESTDB_MAX_QUERY_URL_BYTES)


def test_http_status_error_log_text_omits_url_and_statement():
    request = httpx.Request("GET", "https://db.example/exec?query=CREATE+MATERIALIZED+VIEW+secret")
    response = httpx.Response(431, request=request)
    exc = httpx.HTTPStatusError("Client error '431' for url '" + str(request.url) + "'", request=request, response=response)

    text = _format_exc(exc)

    assert text == "HTTPStatusError: HTTP 431 Request Header Fields Too Large"
    assert "query" not in text and "db.example" not in text


def test_questdb_exec_sends_statement_with_bounded_query_string():
    handler = make_handler()
    seen = {}

    async def fake_get(path, params=None, auth=None):
        seen["path"], seen["params"] = path, params
        return httpx.Response(200, json={"ddl": "OK"}, request=httpx.Request("GET", "http://q"))

    handler.client = SimpleNamespace(get=fake_get)

    asyncio.run(handler._questdb_exec("SELECT 'a b';"))

    assert seen["path"] == "/exec"
    assert seen["params"] == "query=SELECT+%27a+b%27%3B"


def test_repeated_rollup_failure_logs_error_once_and_is_permanent_on_431(caplog):
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    request = httpx.Request("GET", "https://db.example/exec?query=x")

    async def fake_exec(query, *, auth=None):
        raise httpx.HTTPStatusError("boom", request=request, response=httpx.Response(431, request=request))

    handler._questdb_exec = fake_exec
    with caplog.at_level("DEBUG"):
        assert asyncio.run(handler._ensure_questdb_downsampling(profile)) is False
        assert asyncio.run(handler._ensure_questdb_downsampling(profile)) is False

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == len(questdb_rollup_groups())
    assert "HTTP 431" in errors[0].getMessage() and "db.example" not in errors[0].getMessage()
    assert handler._rollup_failure_permanent is True


def test_questdb_exec_reports_sql_error_of_http_400():
    handler = make_handler()

    async def fake_get(path, params=None, auth=None):
        return httpx.Response(400, json={"error": "table does not exist", "position": 7},
                              request=httpx.Request("GET", "http://q/exec?query=secret"))

    handler.client = SimpleNamespace(get=fake_get)

    with pytest.raises(RuntimeError, match="table does not exist at position 7"):
        asyncio.run(handler._questdb_exec("SELECT 1"))


def test_oversized_rollup_logs_kind_length_and_view_without_url(caplog, monkeypatch):
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    monkeypatch.setattr(handler_module, "QUESTDB_MAX_QUERY_URL_BYTES", 100)

    async def fail(*args, **kwargs):
        raise AssertionError("an oversized statement must not be sent")

    handler.client = SimpleNamespace(get=fail)
    with caplog.at_level("DEBUG"):
        assert asyncio.run(handler._ensure_questdb_downsampling(profile)) is False

    message = next(r.getMessage() for r in caplog.records if r.levelname == "ERROR")
    view_name = questdb_rollup_view_name(handler._questdb_metrics_table, profile, next(iter(questdb_rollup_groups())))
    assert view_name in message and "CREATE MATERIALIZED VIEW" in message
    assert "bytes URL-encoded, limit is 100" in message
    assert "SELECT" not in message and "http" not in message
    assert handler._rollup_failure_permanent is True


def test_schema_contract_constants_are_fixed():
    from app.influxdb import config

    assert (config.QUESTDB_DOWNSAMPLING_SCHEMA_VERSION, config.QUESTDB_ROLLUP_GROUP_COUNT,
            config.QUESTDB_ROLLUP_HASH) == (1, 16, "crc32")


def test_unfavorable_distribution_hits_the_hard_limit_with_a_clear_message(monkeypatch):
    columns = {f"very_long_metric_name_for_the_worst_case_{i:03d}": "DOUBLE" for i in range(100)}
    monkeypatch.setattr(handler_module, "QUESTDB_GAUGE_METRICS", frozenset(columns))
    monkeypatch.setattr(handler_module, "questdb_rollup_group", lambda name: 0)
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    ddl = questdb_rollup_ddl("fritzbox_AA1_metrics", profile, 365, 0, columns)

    with pytest.raises(QuestDBStatementTooLongError, match="request is .* limit is 8000"):
        questdb_exec_query_string(ddl)


def test_request_target_counts_the_exec_path():
    sql = "x" * (QUESTDB_MAX_QUERY_URL_BYTES - len("query=") - len("/exec?"))
    assert questdb_exec_query_string(sql)
    with pytest.raises(QuestDBStatementTooLongError):
        questdb_exec_query_string(sql + "x")


def test_group_reserve_against_the_limit_is_reported():
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    sizes = [
        len("/exec?") + len(questdb_exec_query_string(questdb_rollup_ddl("fritzbox_" + "A" * 120, profile, 365, g)))
        for g in questdb_rollup_groups()
    ]
    assert QUESTDB_MAX_QUERY_URL_BYTES - max(sizes) > 6_000


def test_only_missing_groups_are_retried_and_progress_is_logged(caplog):
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    groups = questdb_rollup_groups()
    first = next(iter(groups))
    failing = {"on": True}
    created = []

    async def fake_exec(query, *, auth=None):
        if query.startswith("CREATE MATERIALIZED VIEW"):
            view = re.search(r'EXISTS "([^"]+)"', query).group(1)
            created.append(view)
            if failing["on"] and view.endswith(f"_g{first:02d}_v1"):
                raise RuntimeError("down")
        if "table_columns(" in query:
            return view_columns_result(query)
        if "materialized_views()" in query:
            return {"dataset": [[queried_view(query), "fritzbox_AA1_metrics", "valid", 5, 5]]}
        if "ttlValue" in query:
            return {"dataset": [[90, "DAY"]]}
        return {"ddl": "OK"}

    handler._questdb_exec = fake_exec
    with caplog.at_level("INFO"):
        assert asyncio.run(handler._ensure_questdb_downsampling(profile)) is False
        failing["on"] = False
        assert asyncio.run(handler._ensure_questdb_downsampling(profile)) is True

    assert len(created) == len(groups) + 1
    messages = [r.getMessage() for r in caplog.records]
    assert f"QuestDB rollup groups ready: {len(groups) - 1} of {len(groups)}" in messages
    assert f"QuestDB rollup groups ready: {len(groups)} of {len(groups)}" in messages


def test_view_missing_a_column_is_not_ready_keeps_raw_ttl_and_recovers(caplog):
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    groups = questdb_rollup_groups()
    group = next(iter(groups))
    lacking = questdb_rollup_view_columns(groups[group])[-1]
    state = {"complete": False}
    queries = []
    inner = finalize_exec(queries, (7, "DAY"), None)
    handler._rollup_ready_views.clear()

    async def fake_exec(query, *, auth=None):
        if "table_columns(" in query and f"_g{group:02d}_v1" in query and not state["complete"]:
            return {"dataset": [[c] for c in questdb_rollup_view_columns(groups[group]) if c != lacking]}
        if "min(timestamp)" in query:
            return {"dataset": [["2026-11-08T10:00:00.000000Z"]]}
        return await inner(query, auth=auth)

    handler._questdb_exec = fake_exec
    with caplog.at_level("INFO"):
        assert asyncio.run(handler._finalize_questdb_downsampling("medium", profile)) is False
        assert asyncio.run(handler._finalize_questdb_downsampling("medium", profile)) is False

    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1  # reported once per view and run
    assert lacking in errors[0] and "DROP MATERIALIZED VIEW" in errors[0] and f"group {group:02d}" in errors[0]
    assert "not shortened any further" in errors[0] and "not extended" in errors[0]
    assert "raw TTL is 7 day(s)" in errors[0] and "2026-11-08T10:00:00.000000Z" in errors[0]
    assert "not recoverable" in errors[0] and "Every further day" in errors[0]
    assert not any("ALTER TABLE" in q and "SET TTL" in q and "rollup" not in q and "_events" not in q for q in queries)
    assert len(handler._rollup_ready_views) == len(groups) - 1
    assert not any('ALTER TABLE "fritzbox_AA1_metrics" SET TTL' in q or "INSERT INTO" in q for q in queries)

    state["complete"] = True
    assert asyncio.run(handler._finalize_questdb_downsampling("medium", profile)) is True
    assert len(handler._rollup_ready_views) == len(groups)
    assert any('INSERT INTO "_fritzfluxdb_downsampling"' in q for q in queries)


def test_permanent_group_failure_is_skipped_while_other_groups_are_retried():
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    groups = questdb_rollup_groups()
    ordered = list(groups)
    perm, temp = ordered[0], ordered[1]
    sent = []
    state = {"temp_fails": True}
    request = httpx.Request("GET", "https://db.example/exec?query=x")

    async def fake_exec(query, *, auth=None):
        if query.startswith("CREATE MATERIALIZED VIEW"):
            view = re.search(r'EXISTS "([^"]+)"', query).group(1)
            sent.append(view)
            if view.endswith(f"_g{perm:02d}_v1"):
                raise httpx.HTTPStatusError("x", request=request, response=httpx.Response(431, request=request))
            if view.endswith(f"_g{temp:02d}_v1") and state["temp_fails"]:
                raise RuntimeError("down")
        if "table_columns(" in query:
            return view_columns_result(query)
        if "materialized_views()" in query:
            return {"dataset": [[queried_view(query), "fritzbox_AA1_metrics", "valid", 5, 5]]}
        if "ttlValue" in query:
            return {"dataset": [[90, "DAY"]]}
        return {"ddl": "OK"}

    handler._questdb_exec = fake_exec
    assert asyncio.run(handler._ensure_questdb_downsampling(profile)) is False
    assert handler._rollup_failure_permanent is False  # the temporary failure is still worth retrying
    state["temp_fails"] = False
    assert asyncio.run(handler._ensure_questdb_downsampling(profile)) is False

    perm_sends = [v for v in sent if v.endswith(f"_g{perm:02d}_v1")]
    temp_sends = [v for v in sent if v.endswith(f"_g{temp:02d}_v1")]
    assert len(perm_sends) == 1 and len(temp_sends) == 2
    assert len(handler._rollup_ready_views) == len(groups) - 1
    assert handler._rollup_failure_permanent is True  # only a permanent failure is left


def test_permanent_group_failure_keeps_raw_ttl_unchanged():
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    queries = []
    inner = finalize_exec(queries, (90, "DAY"), None)
    request = httpx.Request("GET", "https://db.example/exec?query=x")

    async def fake_exec(query, *, auth=None):
        if query.startswith("CREATE MATERIALIZED VIEW") and "_g00_v1" in query:
            raise httpx.HTTPStatusError("x", request=request, response=httpx.Response(431, request=request))
        return await inner(query, auth=auth)

    handler._questdb_exec = fake_exec
    assert asyncio.run(handler._finalize_questdb_downsampling("medium", profile)) is False
    assert not any('SET TTL' in q and '"fritzbox_AA1_metrics"' in q for q in queries)
    assert not any("INSERT INTO" in q for q in queries)


def _managed_check(state, table_id_result, *, id_query_fails=False):
    handler = make_handler()
    async def fake_exec(query, *, auth=None):
        if "LATEST ON timestamp" in query:
            return {"dataset": [state] if state else []}
        if query.startswith("SELECT id FROM tables()"):
            if id_query_fails:
                raise RuntimeError("unreadable")
            return {"dataset": [[table_id_result]] if table_id_result is not None else []}
        return {"ddl": "OK"}
    handler._questdb_exec = fake_exec
    return asyncio.run(handler._questdb_raw_ttl_is_managed())


def test_managed_ttl_flag_of_a_dropped_predecessor_table_is_not_inherited():
    assert _managed_check(state_row(managed=True, table_id=10), 42) is False


def test_managed_ttl_flag_counts_only_with_matching_id_and_managed_state():
    assert _managed_check(state_row(managed=True, table_id=42), 42) is True
    assert _managed_check(state_row(managed=False, table_id=42), 42) is False
    assert _managed_check(state_row(managed=True, table_id=None), 42) is False
    assert _managed_check(state_row(managed=True, table_id=42), None) is False
    assert _managed_check(state_row(managed=True, table_id=42), 42, id_query_fails=True) is False
    assert _managed_check(None, 42) is False


def test_administrator_ttl_of_a_recreated_table_is_not_shortened():
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    queries = []
    handler._questdb_exec = finalize_exec(
        queries, (30, "DAY"), state_row(profile="low", raw_days=30, managed=True, table_id=10))
    assert asyncio.run(handler._finalize_questdb_downsampling("medium", profile)) is True
    assert not any("SET TTL" in q and "_rollup_" not in q and "_events" not in q for q in queries)


def test_fresh_state_table_is_created_with_table_id_and_schema_version_1():
    handler = make_handler()
    queries = []
    async def fake_exec(query, *, auth=None):
        queries.append(query)
        return {"dataset": []}
    handler._questdb_exec = fake_exec
    assert asyncio.run(handler._read_questdb_downsampling_state()) == []
    assert queries[0].startswith('CREATE TABLE IF NOT EXISTS "_fritzfluxdb_downsampling"')
    assert "schema_version LONG" in queries[0] and "table_id LONG" in queries[0]
    assert queries[1].startswith("SELECT profile, schema_version") and "table_id FROM" in queries[1]
    assert len(queries) == 2 and not any("ALTER TABLE" in query for query in queries)


def test_all_downsampling_schema_versions_are_1():
    profile = QUESTDB_DOWNSAMPLING_PROFILES["low"]
    assert questdb_rollup_view_name("fritzbox_AA1_metrics", profile, 0) == "fritzbox_AA1_metrics_rollup_1m_g00_v1"
    queries = []
    handler = make_handler()
    async def fake_exec(query, *, auth=None):
        queries.append(query)
        return {"dataset": []}
    handler._questdb_exec = fake_exec
    asyncio.run(handler._set_questdb_downsampling_state(enabled=False))
    insert = next(q for q in queries if q.startswith("INSERT INTO"))
    assert "'', 1, false" in insert
def _http_handler(responses):
    handler = make_handler()
    async def fake_get(path, params=None, auth=None):
        status, body = responses(params)
        return httpx.Response(status, json=body, request=httpx.Request("GET", "http://q"))
    handler.client = SimpleNamespace(get=fake_get)
    handler.name = "QuestDB"
    return handler


@pytest.mark.parametrize("status", [401, 403])
def test_ddl_rejected_with_401_or_403_is_a_permanent_config_error(status):
    handler = _http_handler(lambda params: (status, {}))
    with pytest.raises(handler_module.DatabaseConfigError):
        asyncio.run(handler._questdb_exec("CREATE TABLE x (a INT);"))


def test_permission_error_during_schema_setup_is_not_swallowed():
    handler = _http_handler(lambda params: (403, {}))
    with pytest.raises(handler_module.DatabaseConfigError):
        asyncio.run(handler._ensure_questdb_schema())
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    with pytest.raises(handler_module.DatabaseConfigError):
        asyncio.run(handler._ensure_questdb_downsampling(profile))


def test_rejected_credentials_in_background_retry_surface_on_next_write():
    handler = make_handler()
    handler.downsampling_retry_attempts = 3
    handler.downsampling_retry_interval = 0
    async def rejected(*, auth=None):
        raise handler_module.DatabaseConfigError("rejected")
    handler._ensure_questdb_schema = rejected
    asyncio.run(handler._retry_questdb_downsampling_setup())
    with pytest.raises(handler_module.DatabaseConfigError):
        asyncio.run(handler._write_data_unlocked(force=True))


def test_503_stays_retryable_and_400_reports_the_sql_error_without_config_error():
    handler = _http_handler(lambda params: (503, {}))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(handler._questdb_exec("SELECT 1;"))
    handler = _http_handler(lambda params: (400, {"error": "bad syntax", "position": 3}))
    with pytest.raises(RuntimeError, match="bad syntax"):
        asyncio.run(handler._questdb_exec("SELEC 1;"))


# A blank name counts as unset and is therefore not an error.
@pytest.mark.parametrize(
    ("name", "error"),
    [("", False), ("has space", True), ("dash-name", True), ("dot.name", True), ("new\nline", True), ("x" * 101, True)],
)
def test_questdb_measurement_name_must_be_a_plain_table_name(monkeypatch, name, error):
    monkeypatch.setenv("QUESTDB_MEASUREMENT_NAME", name)
    assert make_config(monkeypatch, "questdb", None).parser_error is error


def test_influxdb_measurement_name_rejects_control_characters(monkeypatch):
    monkeypatch.setenv("INFLUXDB_MEASUREMENT_NAME", "bad\nname")
    assert make_config(monkeypatch, "influxdb_v1", None).parser_error is True


def test_valid_measurement_names_are_accepted(monkeypatch):
    monkeypatch.setenv("QUESTDB_MEASUREMENT_NAME", "fritzbox_AA_1")
    config = make_config(monkeypatch, "questdb", None)
    assert config.parser_error is False
    assert config.measurement_name == "fritzbox_AA_1"
