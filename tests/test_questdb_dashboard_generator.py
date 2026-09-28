#!/usr/bin/env python3
#
# tests/test_questdb_dashboard_generator.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import json
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


def test_measurement_selector_excludes_rollup_views():
    assert "NOT LIKE '%_rollup_%_v%'" in dashboards.TABLES_QUERY


def test_router_requires_enabled_schema_v1_status_and_known_profile():
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
        assert dashboards.rollup_view_name(profile) in profile_query
    assert raw_days_variable["current"]["value"] == "0"
    assert "THEN raw_days" in raw_days_variable["query"]
    assert variable["hide"] == 2
    assert variable["refresh"] == 2
    for profile_name, profile in dashboards.QUESTDB_DOWNSAMPLING_PROFILES.items():
        assert f"${{downsampling_profile:raw}}' = '{profile_name}'" in router_query
        assert dashboards.rollup_view_name(profile) in router_query
    assert "dateadd('d', -${downsampling_raw_days:raw}, now())" in router_query
    assert "FROM tables()" in router_query
    assert "UNION ALL" in router_query
    assert "WHERE table_name = '${measurement}'" in router_query


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
    cases = (
        (
            "fritzbox_system_dashboard.json",
            "01_fritzbox_system_dashboard.json",
            dashboards.build_system,
        ),
        (
            "fritzbox_call_log_dashboard.json",
            "02_fritzbox_call_log_dashboard.json",
            dashboards.build_calllog,
        ),
        (
            "fritzbox_logs_dashboard.json",
            "03_fritzbox_logs_dashboard.json",
            dashboards.build_logs,
        ),
        (
            "fritzbox_home_automation_dashboard.json",
            "04_fritzbox_home_automation_dashboard.json",
            dashboards.build_homeauto,
        ),
    )

    for source_name, generated_name, builder in cases:
        source = load_dashboard(INFLUX_DIR, source_name)
        generated = load_dashboard(QUESTDB_DIR, generated_name)
        assert generated == builder(source)
