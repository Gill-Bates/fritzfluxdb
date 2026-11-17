#!/usr/bin/env python3
#
# tests/test_integration_questdb_dashboards.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Generated QuestDB dashboards against a real QuestDB 10.2.0 container.

Opt-in like tests/test_integration_dedup.py (FRITZFLUXDB_INTEGRATION=1 and Docker). Every query of
every generated dashboard must run against the provisioned raw table, events table and group views.
"""
import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.influxdb import handler as qh
from app.influxdb.config import QUESTDB_DOWNSAMPLING_PROFILES
from tests.test_integration_dedup import QUESTDB_IMAGE, container_factory, pytestmark  # noqa: F401

DASHBOARDS = Path(__file__).resolve().parents[1] / "utils" / "grafana" / "questdb_dashboards"
PROFILE = QUESTDB_DOWNSAMPLING_PROFILES["medium"]
COLUMNS = qh.questdb_expected_columns()
GAUGES = [n for n, t in COLUMNS.items() if qh.questdb_metric_class(n, t) is qh.QuestDBMetricClass.GAUGE]
COUNTERS = [n for n, t in COLUMNS.items() if qh.questdb_metric_class(n, t) is qh.QuestDBMetricClass.COUNTER]
SENDRATE_GROUP = qh.questdb_rollup_group("sendrate")


@pytest.fixture(scope="module")
def questdb(container_factory):  # noqa: F811
    import tests.test_integration_dedup as base
    url = container_factory(QUESTDB_IMAGE, 9000)
    base._wait(f"{url}/exec", {"query": "select 1"})
    return url


def _ok(url, sql):
    body = httpx.get(f"{url}/exec", params={"query": sql}, timeout=120).json()
    assert "error" not in body, (body.get("error"), sql[:300])
    return body


def _wait_rows(url, table, minimum):
    count = None
    for _ in range(120):
        body = httpx.get(f"{url}/exec", params={"query": f"SELECT count() FROM {table}"}, timeout=30).json()
        count = body["dataset"][0][0] if "dataset" in body else None
        if count is not None and count >= minimum:
            return
        time.sleep(1)
    pytest.fail(f"{table} did not reach {minimum} rows, saw {count}")


def _build(url, box):
    """Metrics table (named after the box) with 30 hourly days of constant values and all v1 group views on top."""
    table = qh.questdb_metrics_table_name(box)
    _ok(url, f"CREATE TABLE {table} (timestamp TIMESTAMP, "
             + ", ".join(f'"{n}" {t}' for n, t in COLUMNS.items())
             + ") TIMESTAMP(timestamp) PARTITION BY DAY")
    columns = ", ".join(f'"{n}"' for n in GAUGES + COUNTERS)
    values = ", ".join(["1000.0"] * len(GAUGES) + ["x"] * len(COUNTERS))
    _ok(url, f"INSERT INTO {table}(timestamp, box, name, {columns}) SELECT ts, 'fb1', 'dev', {values} "
             "FROM (SELECT dateadd('h', cast(-(720 - x) as int), now()) ts, x FROM long_sequence(720))")
    for group in qh.questdb_rollup_groups():
        _ok(url, qh.questdb_rollup_ddl(table, PROFILE, 0, group))
    _wait_rows(url, f"{table}_rollup_1m_g{SENDRATE_GROUP:02d}_v1", 700)
    events = qh.questdb_events_table_name(box)
    _ok(url, qh.questdb_events_ddl(events))
    for name, column_type in qh.questdb_event_columns().items():
        _ok(url, f'ALTER TABLE "{events}" ADD COLUMN IF NOT EXISTS "{name}" {column_type}')
    time.sleep(3)


def _substitute(sql, variables, window):
    sql = sql.replace("$__timeFilter(timestamp)", f"timestamp BETWEEN '{window[0]}' AND '{window[1]}'")
    sql = sql.replace("$__sampleByInterval", "1h").replace("$__fromTime", f"cast('{window[0]}' as timestamp)")
    sql = re.sub(r"\$\{\w+:regex\}", "fb1", sql)
    for _ in range(3):
        sql = re.sub(r"\$\{(\w+)(?::raw)?\}", lambda m: variables.get(m.group(1), m.group(0)), sql)
    return sql


def _dashboard_queries(url, table):
    """Yield (dashboard, panel title, rendered SQL) for every query with live-resolved variables."""
    now = datetime.now(UTC).replace(tzinfo=None)
    fmt = "%Y-%m-%dT%H:%M:%S.000000Z"
    window = [(now - timedelta(days=20)).strftime(fmt), now.strftime(fmt)]
    for path in sorted(DASHBOARDS.glob("*.json")):
        dashboard = json.loads(path.read_text())
        variables = {"measurement": table, "downsampling_profile": "medium", "downsampling_raw_days": "7"}
        for variable in dashboard["templating"]["list"]:
            if variable["name"] in variables or variable.get("type") != "query":
                continue
            query = variable["query"]
            if isinstance(query, dict):
                query = query.get("rawSql") or query.get("query", "")
            body = _ok(url, _substitute(query, variables, window))
            value = body["dataset"][0][0] if body["dataset"] else ""
            variables[variable["name"]] = str(value).lower() if isinstance(value, bool) else str(value)

        def walk(panels):
            for panel in panels:
                yield panel
                yield from walk(panel.get("panels", []))

        for panel in walk(dashboard["panels"]):
            for target in panel.get("targets", []):
                if target.get("rawSql"):
                    yield path.name, panel.get("title"), _substitute(target["rawSql"], variables, window)


def test_every_dashboard_query_runs_against_the_provisioned_schema(questdb):
    table = "fritzbox_itdash"
    _build(questdb, table)
    checked = set()
    for dashboard, title, sql in _dashboard_queries(questdb, table):
        assert "${" not in sql, (dashboard, title, sql[:300])
        body = _ok(questdb, sql)
        checked.add(dashboard)
        if title == "Current UP/DOWN" and "sendrate" in sql:
            names = [c["name"] for c in body["columns"]]
            times = [row[0] for row in body["dataset"]]
            assert len(times) == len(set(times)), "a bucket appears twice"
            # sendrate is shown in bit/s: 1000 byte/s in every bucket
            assert {row[names.index("UP")] for row in body["dataset"]} == {8000}
    assert checked == {path.name for path in DASHBOARDS.glob("*.json")}
