#!/usr/bin/env python3
#
# tests/test_questdb_dashboard_generator.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import json
import re
from pathlib import Path

from tools import build_questdb_dashboards as dashboards

REPO_ROOT = Path(__file__).resolve().parents[1]
INFLUX_DIR = REPO_ROOT / "utils" / "grafana" / "influx2_dashboards"
QUESTDB_DIR = REPO_ROOT / "utils" / "grafana" / "questdb_dashboards"


def load_dashboard(directory: Path, name: str) -> dict:
    with (directory / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def panels_by_id(dashboard: dict) -> dict[int, dict]:
    result = {}

    def collect(panels):
        for panel in panels:
            if panel.get("type") == "row":
                collect(panel.get("panels", []))
            elif "id" in panel:
                result[panel["id"]] = panel

    collect(dashboard["panels"])
    return result


def variables_by_name(dashboard: dict) -> dict[str, dict]:
    return {variable["name"]: variable for variable in dashboard["templating"]["list"]}


def _like_matches(pattern: str, name: str) -> bool:
    regex = "".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in pattern)
    return re.fullmatch(regex, name) is not None


def test_measurement_selector_lists_only_metrics_tables():
    patterns = re.findall(r"LIKE '([^']+)'", dashboards.TABLES_QUERY)
    listed = [
        name for name in (
            "fritzbox_50E63634FEA8_metrics",
            "fritzbox_50E63634FEA8_events",
            "fritzbox_50E63634FEA8_metrics_rollup_1m_g00_v1",
            "fritzbox_50E63634FEA8",
            "other_50E63634FEA8_metrics",
        )
        if all(_like_matches(pattern, name) for pattern in patterns)
    ]
    assert listed == ["fritzbox_50E63634FEA8_metrics"]
    # The picked value is the box name; the suffix is stripped and both tables derive from it.
    assert dashboards.TABLES_QUERY.startswith("SELECT left(table_name, length(table_name) - 8)")
    assert dashboards.MEASUREMENT == "${measurement}_metrics"
    assert dashboards.EVENTS == "${measurement}_events"


def test_generated_dashboards_have_no_stale_unsuffixed_table_references():
    for path in sorted(QUESTDB_DIR.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\$\{measurement\}(?!_metrics|_events)", text), path.name
        assert not re.search(r"(FROM|JOIN)\s+\"?fritzbox", text), path.name


def test_event_dashboards_read_events_and_metrics_dashboards_read_metrics():
    for name in ("02_fritzbox_call_log_dashboard.json", "03_fritzbox_logs_dashboard.json"):
        assert "FROM ${measurement}_events" in path_text(name)
    for name in ("01_fritzbox_system_dashboard.json", "04_fritzbox_home_automation_dashboard.json"):
        assert "FROM ${measurement}_metrics" in path_text(name)
    assert "FROM ${measurement}_events" not in path_text("04_fritzbox_home_automation_dashboard.json")


def path_text(name: str) -> str:
    return json.dumps(load_dashboard(QUESTDB_DIR, name))


def test_router_requires_enabled_schema_status_and_known_profile():
    profile_variable = dashboards.downsampling_profile_variable()
    raw_days_variable = dashboards.downsampling_raw_days_variable()
    profile_query = profile_variable["query"]
    variable = dashboards.source_variable("metric_source")
    router_query = variable["query"]

    assert profile_variable["current"]["value"] == "disabled"
    assert f'FROM "{dashboards.QUESTDB_DOWNSAMPLING_STATE_TABLE}"' in profile_query
    assert (
        f"enabled = true AND schema_version = {dashboards.QUESTDB_DOWNSAMPLING_SCHEMA_VERSION}"
        in profile_query
    )
    for profile_name, profile in dashboards.QUESTDB_DOWNSAMPLING_PROFILES.items():
        assert f"profile = '{profile_name}'" in profile_query
        assert f"raw_days <= {profile.raw_retention_days}" in profile_query
        assert f"rollup_interval = '{profile.rollup_interval}'" in profile_query
        assert dashboards.rollup_view_prefix(profile) in profile_query
    assert raw_days_variable["current"]["value"] == "0"
    assert "THEN raw_days" in raw_days_variable["query"]
    assert variable["hide"] == 2
    assert variable["refresh"] == 2
    for profile_name, profile in dashboards.QUESTDB_DOWNSAMPLING_PROFILES.items():
        assert f"${{downsampling_profile:raw}}' = '{profile_name}'" in router_query
        assert dashboards.rollup_view_prefix(profile) in router_query
    assert "dateadd('d', -${downsampling_raw_days:raw}, now())" in router_query
    assert "FROM tables()" in router_query
    assert "UNION ALL" in router_query
    assert "WHERE table_name = '${measurement}_metrics'" in router_query


def test_system_historical_queries_route_gauges_and_counters():
    source = load_dashboard(INFLUX_DIR, "fritzbox_system_dashboard.json")
    dashboard = dashboards.build_system(source)
    panels = panels_by_id(dashboard)
    variables = variables_by_name(dashboard)

    assert variables["metric_source"]["refresh"] == 2
    assert variables["metric_source_1d"]["refresh"] == 2
    assert variables["metric_source_7d"]["refresh"] == 2

    current_up_down = panels[47]["targets"][0]["rawSql"]
    assert "FROM ${metric_source:raw}" in current_up_down
    assert "sum(sendrate${metric_source_gauge_sum_suffix:raw})" in current_up_down
    assert "${metric_source_gauge_count_aggregate:raw}" in current_up_down

    error_counters = panels[47]["targets"][1]["rawSql"]
    assert "crc_errors${metric_source_counter_suffix:raw}" in error_counters

    daily_counters = panels[2]["targets"][0]["rawSql"]
    assert "FROM ${metric_source_7d:raw}" in daily_counters
    assert "totalbytessent${metric_source_7d_counter_suffix:raw}" in daily_counters


def test_counter_deltas_look_back_per_counter_over_non_null_rows():
    # Verified against QuestDB 10.2.0: with plain lag() the sample rows (up on 10:00 and 10:02,
    # down on 10:01 and 10:03) gave 0/0; IGNORE NULLS gives 20/60.
    sql = dashboards.counter_delta_series(
        ["totalbytessent", "totalbytesreceived"], ["Upload", "Download"]
    )
    for field in ("totalbytessent", "totalbytesreceived"):
        assert f"lag({field}" in sql
    assert sql.count("IGNORE NULLS OVER (PARTITION BY box ORDER BY timestamp)") == 2


def test_current_stats_logs_and_home_automation_states_stay_raw():
    system_source = load_dashboard(INFLUX_DIR, "fritzbox_system_dashboard.json")
    system = dashboards.build_system(system_source)
    system_panels = panels_by_id(system)
    source_panels = panels_by_id(system_source)
    assert "FROM ${measurement}" in system_panels[9]["targets"][0]["rawSql"]
    assert "FROM ${measurement}" in system_panels[13]["targets"][0]["rawSql"]
    assert "FROM ${measurement}" in system_panels[26]["targets"][0]["rawSql"]
    for panel_id in (9, 13):
        assert system_panels[panel_id]["gridPos"] == source_panels[panel_id]["gridPos"]
        assert system_panels[panel_id]["options"] == source_panels[panel_id]["options"]

    home = dashboards.build_homeauto(
        load_dashboard(INFLUX_DIR, "fritzbox_home_automation_dashboard.json")
    )
    home_panels = panels_by_id(home)
    for panel_id in (4, 18, 32, 33):
        assert "FROM ${measurement}" in home_panels[panel_id]["targets"][0]["rawSql"]
        assert "metric_source" not in home_panels[panel_id]["targets"][0]["rawSql"]

    temperature = home_panels[24]["targets"][0]["rawSql"]
    assert "FROM ${metric_source:raw}" in temperature
    assert "sum(ha_temperature${metric_source_gauge_sum_suffix:raw})" in temperature

    energy = home_panels[6]["targets"][2]["rawSql"]
    assert "last(ha_powermeter_energy${metric_source_counter_suffix:raw})" in energy
    assert "ha_powermeter_energy${metric_source_gauge_sum_suffix:raw}" not in energy


def test_generated_dashboards_match_generator_output():
    for source_name, generated_name, builder in dashboards.DASHBOARDS:
        source = load_dashboard(INFLUX_DIR, source_name)
        generated = load_dashboard(QUESTDB_DIR, generated_name)
        assert generated == builder(source)


ROLLUP_PREFIX = "fritzbox_rollup_1m"
ROLLUP_SUFFIX = f"_v{dashboards.QUESTDB_DOWNSAMPLING_SCHEMA_VERSION}"


def _render_rollup(sql):
    for name, value in {
        "_group_open": "_g", "_group_close": ROLLUP_SUFFIX,
        "_gauge_sum_suffix": "_sum", "_gauge_count_suffix": "_count",
        "_gauge_count_aggregate": "sum", "_counter_suffix": "_last",
    }.items():
        sql = re.sub(r"\$\{metric_source(?:_1d|_7d)?" + name + r":raw\}", value, sql)
    return re.sub(r"\$\{metric_source(?:_1d|_7d)?:raw\}", ROLLUP_PREFIX, sql)


def test_dashboards_reference_only_existing_group_views_and_columns():
    from app.influxdb.handler import questdb_rollup_group, questdb_rollup_groups

    groups = questdb_rollup_groups()
    checked = 0
    for path in sorted(QUESTDB_DIR.glob("*.json")):
        for panel_query in re.findall(r'"rawSql": "((?:[^"\\]|\\.)*)"', path.read_text(encoding="utf-8")):
            if "metric_source" not in panel_query:
                continue
            sql = _render_rollup(json.loads(f'"{panel_query}"'))
            views = {int(g) for g in re.findall(rf"{ROLLUP_PREFIX}_g(\d\d){ROLLUP_SUFFIX}\b", sql)}
            columns = set(re.findall(r"\b([a-z0-9_]+)_(?:sum|count|last)\b", sql))
            assert views and views <= set(groups), (path.name, views)
            for column in columns:
                group = questdb_rollup_group(column)
                assert column in groups[group], (path.name, column)
                assert group in views, (path.name, column)
            checked += 1
    assert checked > 5


def test_multi_group_queries_merge_on_time_and_single_group_queries_do_not():
    cross = dashboards.ts_agg("sendrate", "receiverate", "upstreammax")
    single = dashboards.ts_agg("sendrate")

    assert "UNION ALL" in cross and "GROUP BY time" in cross
    assert "UNION ALL" not in single
    assert dashboards.routed_source("sendrate", "metric_source") in single


def test_generated_dashboards_use_schema_version_1_only():
    for path in sorted(QUESTDB_DIR.glob("*.json")):
        text = path.read_text(encoding="utf-8").lower()
        assert "legacy" not in text and "_v2" not in text, path.name
        assert "schema_version = 2" not in text, path.name
def test_home_automation_queries_identify_devices_by_id():
    sql = json.dumps(load_dashboard(QUESTDB_DIR, "04_fritzbox_home_automation_dashboard.json"))
    assert "PARTITION BY box, id" in sql
    assert "PARTITION BY box, name" not in sql
    assert "name IS NOT NULL" not in sql
def test_home_automation_generator_helpers_use_id():
    assert "PARTITION BY box, id" in dashboards.latest_by_name("ha_temperature", "Temperature")
    series = dashboards.ts_agg_by_name("ha_temperature", "Temperature", metric_expr=dashboards.DEVICE_LABEL)
    assert "id IS NOT NULL" in series and "name IS NOT NULL" not in series
