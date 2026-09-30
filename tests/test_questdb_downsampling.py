#!/usr/bin/env python3
#
# tests/test_questdb_downsampling.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
import configparser
from types import SimpleNamespace

import pytest

from app.classes.influxdb.config import (
    QUESTDB_DOWNSAMPLING_PROFILES,
    InfluxDBConfig,
)
from app.classes.influxdb.handler import (
    InfluxHandler,
    QuestDBMetricClass,
    questdb_metric_class,
    questdb_raw_retention_days,
    questdb_rollup_ddl,
    questdb_rollup_view_name,
)


def make_config(monkeypatch, version: str, downsampling: str | None) -> InfluxDBConfig:
    for name in (
        "DB_TYPE",
        "INFLUXDB_VERSION",
        "INFLUXDB_DOWNSAMPLING",
        "QUESTDB_HOSTNAME",
        "QUESTDB_HOST",
        "QUESTDB_DOWNSAMPLING",
    ):
        monkeypatch.delenv(name, raising=False)
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

    InfluxDBConfig.parser_error = False
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
        {
            "box": "SYMBOL",
            "cpu_temp": "LONG",
            "totalbytessent": "LONG",
            "ha_switch_state": "LONG",
            "message": "VARCHAR",
        },
    )

    assert 'CREATE MATERIALIZED VIEW IF NOT EXISTS "fritzbox ""main""_rollup_1m_v1"' in ddl
    assert 'WITH BASE "fritzbox ""main""" REFRESH IMMEDIATE AS (' in ddl
    assert 'sum("cpu_temp") AS "cpu_temp_sum"' in ddl
    assert 'count("cpu_temp") AS "cpu_temp_count"' in ddl
    assert 'min("cpu_temp") AS "cpu_temp_min"' in ddl
    assert 'max("cpu_temp") AS "cpu_temp_max"' in ddl
    assert 'last("cpu_temp") AS "cpu_temp_last"' in ddl
    assert 'last("totalbytessent") AS "totalbytessent_last"' in ddl
    assert "ha_switch_state" not in ddl
    assert "message" not in ddl
    assert 'WHERE "cpu_temp" IS NOT NULL OR "totalbytessent" IS NOT NULL' in ddl
    assert "SAMPLE BY 1m ALIGN TO CALENDAR" in ddl
    assert ") PARTITION BY DAY TTL 90 DAYS;" in ddl


def test_zero_total_retention_omits_rollup_ttl_and_keeps_profile_raw_ttl():
    profile = QUESTDB_DOWNSAMPLING_PROFILES["high"]

    ddl = questdb_rollup_ddl("fritzbox", profile, 0, {"cpu_temp": "LONG"})

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
    return handler


def state_row(profile="medium", raw_days=7, rollup_view="fritzbox_AA1_rollup_1m_v1", managed=False):
    return [profile, 1, True, raw_days, "1m", rollup_view, managed]


def test_ready_rollup_is_idempotently_provisioned_and_validated():
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    view_name = questdb_rollup_view_name(handler.config.measurement_name, profile)
    queries = []

    async def fake_exec(query, *, auth=None):
        queries.append(query)
        if "materialized_views()" in query:
            return {"dataset": [[view_name, "fritzbox_AA1", "valid", 12, 12]]}
        if "tables()" in query:
            return {"dataset": [[90, "DAY"]]}
        return {"ddl": "OK"}

    handler._questdb_exec = fake_exec

    assert asyncio.run(handler._ensure_questdb_downsampling(profile)) is True
    assert queries[0].startswith("CREATE MATERIALIZED VIEW IF NOT EXISTS")
    assert "REFRESH IMMEDIATE" in queries[0]
    assert "materialized_views()" in queries[1]
    assert len(queries) == 3


def test_pending_initial_refresh_does_not_touch_any_ttl():
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    view_name = questdb_rollup_view_name(handler.config.measurement_name, profile)
    queries = []

    async def fake_exec(query, *, auth=None):
        queries.append(query)
        if "materialized_views()" in query:
            return {"dataset": [[view_name, "fritzbox_AA1", "valid", 3, 8]]}
        return {"ddl": "OK"}

    handler._questdb_exec = fake_exec

    assert asyncio.run(handler._ensure_questdb_downsampling(profile)) is False
    assert len(queries) == 2
    assert all("SET TTL" not in query and "tables()" not in query for query in queries)


def test_downsampling_sql_error_is_tolerated():
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]

    async def fake_exec(query, *, auth=None):
        raise RuntimeError("unsupported")

    handler._questdb_exec = fake_exec

    assert asyncio.run(handler._ensure_questdb_downsampling(profile)) is False


def test_profile_change_uses_new_view_without_dropping_old_view():
    low_view = questdb_rollup_view_name("fritzbox", QUESTDB_DOWNSAMPLING_PROFILES["low"])
    high_view = questdb_rollup_view_name("fritzbox", QUESTDB_DOWNSAMPLING_PROFILES["high"])

    assert low_view == "fritzbox_rollup_1m_v1"
    assert high_view == "fritzbox_rollup_5m_v1"
    assert low_view != high_view


def finalize_exec(queries, raw_ttl, state, *, view_name, alter_fails=False):
    """Fake /exec that tracks the raw TTL so a verification read sees the applied value."""
    ttl = {"value": raw_ttl[0], "unit": raw_ttl[1]}

    async def fake_exec(query, *, auth=None):
        queries.append(query)
        if "materialized_views()" in query:
            return {"dataset": [[view_name, "fritzbox_AA1", "valid", 5, 5]]}
        if "LATEST ON timestamp" in query:
            return {"dataset": [state] if state else []}
        if "SELECT table_name FROM tables()" in query:
            return {"dataset": [["_fritzfluxdb_downsampling"]]}
        if "ttlValue" in query and view_name in query:
            return {"dataset": [[90, "DAY"]]}
        if "ttlValue" in query:
            return {"dataset": [[ttl["value"], ttl["unit"]]]}
        if "SET TTL" in query and view_name not in query:
            if alter_fails:
                raise RuntimeError("not permitted")
            ttl["value"] = int(query.split("SET TTL ")[1].split(" DAYS")[0])
            ttl["unit"] = "DAYS"
        return {"ddl": "OK"}

    return fake_exec


def test_schema_shortens_raw_ttl_only_after_rollup_is_current(monkeypatch):
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    view_name = questdb_rollup_view_name(handler.config.measurement_name, profile)
    queries = []
    monkeypatch.setattr(
        "app.classes.influxdb.handler.questdb_expected_columns",
        lambda: {"cpu_temp": "LONG"},
    )
    handler._questdb_exec = finalize_exec(queries, (0, None), None, view_name=view_name)

    assert asyncio.run(handler._ensure_questdb_schema()) is True
    raw_ttl_query = 'ALTER TABLE "fritzbox_AA1" SET TTL 7 DAYS;'
    assert raw_ttl_query in queries
    assert queries.index(raw_ttl_query) > next(i for i, query in enumerate(queries) if "materialized_views()" in query)
    insert_index = next(i for i, query in enumerate(queries) if 'INSERT INTO "_fritzfluxdb_downsampling"' in query)
    assert insert_index > queries.index(raw_ttl_query)
    assert "'medium', 1, true, 7, '1m', 'fritzbox_AA1_rollup_1m_v1', true" in queries[insert_index]


def test_administrator_raw_ttl_is_kept_and_published_as_effective_history():
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    view_name = questdb_rollup_view_name(handler.config.measurement_name, profile)
    queries = []
    handler._questdb_exec = finalize_exec(queries, (90, "DAY"), None, view_name=view_name)

    assert asyncio.run(handler._finalize_questdb_downsampling("medium", profile)) is True
    assert not any('ALTER TABLE "fritzbox_AA1" SET TTL' in query for query in queries)
    assert any(
        'INSERT INTO "_fritzfluxdb_downsampling"' in query
        and "'medium', 1, true, 90, '1m', 'fritzbox_AA1_rollup_1m_v1', false" in query
        for query in queries
    )


def test_managed_raw_ttl_is_shortened_on_profile_change():
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    view_name = questdb_rollup_view_name(handler.config.measurement_name, profile)
    queries = []
    handler._questdb_exec = finalize_exec(
        queries,
        (30, "DAY"),
        state_row(profile="low", raw_days=30, managed=True),
        view_name=view_name,
    )

    assert asyncio.run(handler._finalize_questdb_downsampling("medium", profile)) is True
    assert 'ALTER TABLE "fritzbox_AA1" SET TTL 7 DAYS;' in queries
    assert any(
        'INSERT INTO "_fritzfluxdb_downsampling"' in query
        and "'medium', 1, true, 7, '1m', 'fritzbox_AA1_rollup_1m_v1', true" in query
        for query in queries
    )


def test_failed_raw_ttl_change_publishes_no_active_state():
    handler = make_handler()
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
    view_name = questdb_rollup_view_name(handler.config.measurement_name, profile)
    queries = []
    handler._questdb_exec = finalize_exec(queries, (0, None), None, view_name=view_name, alter_fails=True)

    assert asyncio.run(handler._finalize_questdb_downsampling("medium", profile)) is False
    assert not any('INSERT INTO "_fritzfluxdb_downsampling"' in query for query in queries)


def test_disabled_downsampling_keeps_legacy_ttl_path(monkeypatch):
    handler = make_handler(profile_name="")
    queries = []
    monkeypatch.setattr("app.classes.influxdb.handler.questdb_expected_columns", dict)

    async def fake_exec(query, *, auth=None):
        queries.append(query)
        if "SELECT table_name FROM tables()" in query:
            return {"dataset": [["_fritzfluxdb_downsampling"]]}
        if "LATEST ON timestamp" in query:
            return {"dataset": [["disabled", 1, False, 0, "", "", False]]}
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
        rollup_view="fritzbox_AA1_rollup_1m_v1",
    )) is True
    assert "measurement SYMBOL, profile SYMBOL, schema_version LONG" in queries[0]
    assert "ADD COLUMN IF NOT EXISTS raw_ttl_managed BOOLEAN" in queries[1]
    assert any("LATEST ON timestamp PARTITION BY measurement" in query for query in queries)
    assert not any(query.startswith("INSERT INTO") for query in queries)


def test_existing_administrative_raw_ttl_is_never_replaced():
    handler = make_handler()
    queries = []

    async def fake_exec(query, *, auth=None):
        queries.append(query)
        return {"dataset": [[90, "DAY"]]}

    handler._questdb_exec = fake_exec

    assert asyncio.run(handler._set_questdb_table_ttl("fritzbox_AA1", 7)) is True
    assert len(queries) == 1


def test_rollup_rejects_unclassified_numeric_only_schema():
    profile = QUESTDB_DOWNSAMPLING_PROFILES["medium"]

    with pytest.raises(ValueError, match="explicitly classified metric"):
        questdb_rollup_ddl("fritzbox", profile, 90, {"future_numeric": "LONG"})


def test_bounded_retry_completes_after_async_refresh():
    handler = make_handler()
    handler.downsampling_retry_attempts = 3
    handler.downsampling_retry_interval = 0
    results = iter([False, True])
    attempts = []

    async def fake_finalize(profile_name, profile, *, auth=None):
        attempts.append((profile_name, profile.rollup_interval))
        return next(results)

    handler._finalize_questdb_downsampling = fake_finalize

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

    async def fake_finalize(profile_name, profile, *, auth=None):
        attempts.append(profile_name)
        return False

    handler._finalize_questdb_downsampling = fake_finalize

    asyncio.run(handler._retry_questdb_downsampling_setup())

    assert attempts == ["medium", "medium"]
    assert handler.questdb_schema_ensured is False
