#!/usr/bin/env python3
"""
Generates QuestDB (official QuestDB datasource) Grafana dashboards
from the influx2 source dashboards.

Run from the repo root:  python3 tools/build_questdb_dashboards.py
"""

import json
import copy
import os

DS_VAR = "${DS_QUESTDB}"
DS_TYPE = "questdb-questdb-datasource"
DS_NAME = "QuestDB"
FORMAT_TIMESERIES = 0
FORMAT_TABLE = 1
MEASUREMENT = "${measurement}"
TF = "$__timeFilter(timestamp)"
SAMPLE_BY = "$__sampleByInterval"
LOG_TYPE = "^${log_type:regex}$"
TABLES_QUERY = "SELECT table_name FROM tables() WHERE table_name LIKE 'fritzbox%' ORDER BY table_name"
BOX_FILTER = "box ~ '^${boxtag:regex}$'"
BOXES_QUERY = (
    f"SELECT DISTINCT box\n"
    f"FROM {MEASUREMENT}\n"
    f"WHERE {TF}\n"
    f"  AND box IS NOT NULL\n"
    f"ORDER BY box"
)


def ds():
    return {"type": DS_TYPE, "uid": DS_VAR}


def grafana_format(fmt):
    return FORMAT_TABLE if fmt == "table" else FORMAT_TIMESERIES


def target(query, ref="A", fmt="time_series"):
    selected_format = grafana_format(fmt)
    return {
        "datasource": ds(),
        "format": selected_format,
        "queryType": "sql",
        "rawSql": query,
        "refId": ref,
        "selectedFormat": selected_format,
    }


def target_table(query, ref="A"):
    return target(query, ref, "table")


def ts_last(field, alias=None):
    a = alias or field
    return (
        f"SELECT timestamp AS time, {field} AS \"{a}\"\n"
        f"FROM {MEASUREMENT}\n"
        f"WHERE {TF}\n"
        f"  AND {BOX_FILTER}\n"
        f"  AND {field} IS NOT NULL\n"
        f"ORDER BY timestamp DESC\nLIMIT 1"
    )


def ts_agg(*fields):
    cols = ",\n  ".join(f'avg({f}) AS "{f}"' for f in fields)
    null_check = " OR ".join(f"{f} IS NOT NULL" for f in fields)
    return (
        f"SELECT timestamp AS time,\n  {cols}\n"
        f"FROM {MEASUREMENT}\n"
        f"WHERE {TF}\n"
        f"  AND {BOX_FILTER}\n"
        f"  AND ({null_check})\n"
        f"SAMPLE BY {SAMPLE_BY} ALIGN TO CALENDAR"
    )


def ts_agg_by_name(
    field,
    alias,
    aggregate="avg",
    *,
    value_expr=None,
    extra_filter=None,
    metric_expr=None,
):
    expr = value_expr or field
    filters = [
        f"{TF}",
        BOX_FILTER,
        "name IS NOT NULL",
        f"{field} IS NOT NULL",
    ]
    if extra_filter:
        filters.append(extra_filter)
    where_clause = "\n  AND ".join(filters)
    if metric_expr:
        return (
            f"SELECT timestamp AS time, {metric_expr} AS metric, {aggregate}({expr}) AS value\n"
            f"FROM {MEASUREMENT}\n"
            f"WHERE {where_clause}\n"
            f"SAMPLE BY {SAMPLE_BY} ALIGN TO CALENDAR"
        )
    return (
        f"SELECT timestamp AS time, name, {aggregate}({expr}) AS \"{alias}\"\n"
        f"FROM {MEASUREMENT}\n"
        f"WHERE {where_clause}\n"
        f"SAMPLE BY {SAMPLE_BY} ALIGN TO CALENDAR"
    )


def counter_delta_series(fields, aliases, *, time_filter=TF, sample_by=SAMPLE_BY, time_alias="time"):
    lag_cols = [
        f"lag({field}) OVER (PARTITION BY box ORDER BY timestamp) AS previous_{field}"
        for field in fields
    ]
    delta_cols = [
        (
            f"CASE WHEN previous_{field} IS NULL THEN 0\n"
            f"      WHEN {field} >= previous_{field} THEN {field} - previous_{field}\n"
            f"      ELSE 0 END AS {field}_delta"
        )
        for field in fields
    ]
    sum_cols = ",\n  ".join(
        f'sum({field}_delta) AS "{alias}"'
        for field, alias in zip(fields, aliases)
    )
    field_checks = " OR ".join(f"{field} IS NOT NULL" for field in fields)
    return (
        f"SELECT timestamp AS {time_alias},\n  {sum_cols}\n"
        f"FROM (\n"
        f"  SELECT timestamp,\n    " + ",\n    ".join(delta_cols) + "\n"
        f"  FROM (\n"
        f"    SELECT timestamp, box, " + ", ".join(fields + lag_cols) + "\n"
        f"    FROM {MEASUREMENT}\n"
        f"    WHERE {time_filter}\n"
        f"      AND {BOX_FILTER}\n"
        f"      AND ({field_checks})\n"
        f"  )\n"
        f")\n"
        f"SAMPLE BY {sample_by} ALIGN TO CALENDAR"
    )


def last_not_null(field, alias):
    return f'last_not_null({field}) AS "{alias}"'


def last_nonempty_text(field, alias):
    return f'last_not_null(CASE WHEN {field} = \'\' THEN NULL ELSE {field} END) AS "{alias}"'


def bool_text(field, alias):
    return (
        f"last_not_null(CASE WHEN {field} IS NULL THEN NULL "
        f"WHEN {field} THEN 'True' ELSE 'False' END) AS \"{alias}\""
    )


def bool_flag(field, alias):
    return (
        f"last_not_null(CASE WHEN {field} IS NULL THEN NULL "
        f"WHEN {field} THEN 1 ELSE 0 END) AS \"{alias}\""
    )


def current_rate(field, alias):
    return (
        f"SELECT sum({field}) * 8 AS \"{alias}\"\n"
        f"FROM (\n"
        f"  SELECT box, {field}\n"
        f"  FROM {MEASUREMENT}\n"
        f"  WHERE timestamp >= {ago('m', 5)}\n"
        f"    AND {BOX_FILTER}\n"
        f"    AND {field} IS NOT NULL\n"
        f"  LATEST ON timestamp PARTITION BY box\n"
        f")"
    )


def latest_by_name(field, alias, *, hours=12):
    return (
        f"SELECT name AS \"Device\", {field} AS \"{alias}\"\n"
        f"FROM {MEASUREMENT}\n"
        f"WHERE timestamp >= {ago('h', hours)}\n"
        f"  AND {BOX_FILTER}\n"
        f"  AND name IS NOT NULL\n"
        f"  AND {field} IS NOT NULL\n"
        f"LATEST ON timestamp PARTITION BY box, name\n"
        f"ORDER BY name"
    )


def ago(unit, amount):
    return f"dateadd('{unit}', -{amount}, now())"


def wlan_info_select(prefix, band):
    return (
        f"SELECT * FROM (\n"
        f"  SELECT {last_not_null(f'{prefix}_ssid', 'Name')}, {last_not_null(f'{prefix}_status', 'Status')},\n"
        f"  {last_not_null(f'{prefix}_802_11_standard', 'Standard')}, {last_not_null(f'{prefix}_channel', 'Channel')},\n"
        f"  {last_not_null(f'{prefix}_associations', 'Clients')}, '{band}' AS \"Band\"\n"
        f"  FROM {MEASUREMENT}\n"
        f"  WHERE {TF}\n"
        f"    AND {BOX_FILTER}\n"
        f") WHERE \"Name\" IS NOT NULL OR \"Status\" IS NOT NULL OR \"Standard\" IS NOT NULL\n"
        f"  OR \"Channel\" IS NOT NULL OR \"Clients\" IS NOT NULL"
    )


def counter_delta_24h(field, alias):
    return (
        f"SELECT timestamp AS time, sum(delta) OVER (ORDER BY timestamp) AS \"{alias}\"\n"
        f"FROM (\n"
        f"  SELECT timestamp,\n"
        f"    CASE WHEN previous_value IS NULL THEN 0\n"
        f"      WHEN {field} >= previous_value THEN {field} - previous_value\n"
        f"      ELSE 0\n"
        f"    END AS delta\n"
        f"  FROM (\n"
        f"    SELECT timestamp, {field}, lag({field}) OVER (PARTITION BY box ORDER BY timestamp) AS previous_value\n"
        f"    FROM {MEASUREMENT}\n"
        f"    WHERE timestamp >= {ago('d', 1)}\n"
        f"      AND {BOX_FILTER}\n"
        f"      AND {field} IS NOT NULL\n"
        f"  )\n"
        f")"
    )


def patch_inputs(d):
    d["__inputs"] = [
        {"name": "DS_QUESTDB", "label": "QuestDB", "description": "",
         "type": "datasource", "pluginId": DS_TYPE, "pluginName": DS_NAME},
    ]
    d["__requires"] = [
        r for r in d.get("__requires", []) if r.get("id") not in ("influxdb", "postgres")
    ] + [{"type": "datasource", "id": DS_TYPE, "name": DS_NAME, "version": "1.0.0"}]


def patch_measurement_var(v, label="FritzBox"):
    v.update({
        "current": {"selected": False, "text": "", "value": ""},
        "datasource": ds(),
        "definition": TABLES_QUERY,
        "hide": 0,
        "includeAll": False,
        "label": label,
        "multi": False,
        "name": "measurement",
        "options": [],
        "query": TABLES_QUERY,
        "refresh": 1,
        "sort": 1,
        "type": "query",
    })


def patch_boxtag_var(v, *, include_all=True):
    current = (
        {"selected": True, "text": "All", "value": "$__all"}
        if include_all
        else {"selected": False, "text": "fritz.box", "value": "fritz.box"}
    )
    v.update({
        "current": current,
        "datasource": ds(),
        "definition": BOXES_QUERY,
        "hide": 0,
        "includeAll": include_all,
        "allValue": ".*" if include_all else "",
        "multi": False,
        "name": "boxtag",
        "options": [],
        "query": BOXES_QUERY,
        "refresh": 1,
        "regex": "",
        "sort": 1,
        "type": "query",
    })


def patch_timezone_var(v):
    q = (
        f"SELECT fritzfluxdb_setting_timezone\n"
        f"FROM {MEASUREMENT}\n"
        f"WHERE timestamp >= {ago('d', 2)}\n"
        f"  AND {BOX_FILTER}\n"
        f"  AND fritzfluxdb_setting_timezone IS NOT NULL\n"
        f"ORDER BY timestamp DESC LIMIT 1"
    )
    v.update({
        "current": {"selected": False, "text": "Europe/Berlin", "value": "Europe/Berlin"},
        "datasource": ds(),
        "definition": q,
        "hide": 0,
        "includeAll": False,
        "multi": False,
        "name": "timezone",
        "options": [],
        "query": q,
        "refresh": 1,
        "sort": 0,
        "type": "query",
    })


# ---------------------------------------------------------------------------
# System dashboard
# ---------------------------------------------------------------------------

SYSTEM_QUERIES = {
    # Current UP/DOWN
    (47, "A"): (
        f"SELECT timestamp AS time,\n"
        f"  avg(sendrate)*8 AS \"UP\", avg(receiverate)*8 AS \"DOWN\",\n"
        f"  avg(downstreammax) AS \"DOWN Max\", avg(downstream_dsl_sync_max)*1000 AS \"DOWN DSL Sync\",\n"
        f"  avg(downstreamphysicalmax) AS \"DOWN Physical Max\", avg(upstreammax) AS \"UP netto Max\",\n"
        f"  avg(upstream_dsl_sync_max)*1000 AS \"UP DSL Sync\", avg(upstreamphysicalmax) AS \"UP Physical Max\"\n"
        f"FROM {MEASUREMENT}\n"
        f"WHERE {TF}\n"
        f"  AND {BOX_FILTER}\n"
        f"  AND (sendrate IS NOT NULL OR receiverate IS NOT NULL\n"
        f"       OR downstreammax IS NOT NULL OR downstream_dsl_sync_max IS NOT NULL\n"
        f"       OR upstreammax IS NOT NULL OR upstream_dsl_sync_max IS NOT NULL)\n"
        f"SAMPLE BY {SAMPLE_BY} ALIGN TO CALENDAR",
        "time_series",
    ),
    (47, "B"): (
        counter_delta_series(
            ["crc_errors", "errored_seconds", "severely_errored_seconds"],
            ["CRC Errors", "Errored Seconds", "Severely Errored Seconds"],
        ),
        "time_series",
    ),
    # Log entries (logs panel)
    (26, "A"): (
        f"SELECT timestamp AS time, log_entry AS line, log_type\n"
        f"FROM {MEASUREMENT}\n"
        f"WHERE {TF}\n"
        f"  AND {BOX_FILTER}\n"
        f"  AND log_entry IS NOT NULL AND log_type != 'FritzInfluxDB'\n"
        f"ORDER BY timestamp DESC LIMIT 5",
        "time_series",
    ),
    # Stat / gauge panels – last value
    (10, "A"): (ts_last("upgrade_available", "Upgrade Available"), "table"),
    (9,  "A"): (ts_last("linkuptime", "Link Uptime (s)"), "table"),
    (11, "A"): (
        current_rate("receiverate", "receiverate"),
        "table",
    ),
    (12, "A"): (
        current_rate("sendrate", "sendrate"),
        "table",
    ),
    (30, "A"): (ts_last("cpu_temp", "CPU Temp (°C)"), "table"),
    (33, "A"): (ts_last("cpu_utilization", "CPU %"), "table"),
    (34, "A"): (
        f"SELECT timestamp AS time,\n"
        f"  ram_usage_fixed AS \"RAM Fixed\", ram_usage_dynamic AS \"RAM Dynamic\"\n"
        f"FROM {MEASUREMENT}\n"
        f"WHERE {TF}\n"
        f"  AND {BOX_FILTER}\n"
        f"  AND (ram_usage_fixed IS NOT NULL OR ram_usage_dynamic IS NOT NULL)\n"
        f"ORDER BY timestamp DESC LIMIT 1",
        "table",
    ),
    (37, "A"): (ts_last("num_active_host", "Active Hosts"), "table"),
    (13, "A"): (ts_last("systemuptime", "System Uptime (s)"), "table"),
    # 24-h up/download delta
    (16, "A"): (counter_delta_24h("totalbytessent", "Upload Last 24h"), "time_series"),
    (15, "A"): (counter_delta_24h("totalbytesreceived", "Download Last 24h"), "time_series"),
    # Daily traffic timeseries
    (2, "B"): (
        counter_delta_series(
            ["totalbytessent", "totalbytesreceived"],
            ["Upload", "Download"],
            time_filter=f"timestamp >= {ago('d', 7)}",
            sample_by="1d",
        ),
        "time_series",
    ),
    (17, "A"): (
        counter_delta_series(
            ["totalbytessent", "totalbytesreceived"],
            ["Upload", "Download"],
        ),
        "time_series",
    ),
    (14, "A"): (
        counter_delta_series(
            ["totalbytessent", "totalbytesreceived"],
            ["Upload", "Download"],
            time_filter=f"timestamp >= {ago('d', 7)}",
            sample_by="1d",
            time_alias="day",
        ),
        "table",
    ),
    (8, "A"): (
        f"SELECT last(totalbytessent) AS \"Upload Since Connection\"\n"
        f"FROM {MEASUREMENT}\n"
        f"WHERE timestamp >= {ago('d', 1)}\n"
        f"  AND {BOX_FILTER}\n"
        f"  AND totalbytessent IS NOT NULL",
        "table",
    ),
    (3, "A"): (
        f"SELECT last(totalbytesreceived) AS \"Download Since Connection\"\n"
        f"FROM {MEASUREMENT}\n"
        f"WHERE timestamp >= {ago('d', 1)}\n"
        f"  AND {BOX_FILTER}\n"
        f"  AND totalbytesreceived IS NOT NULL",
        "table",
    ),
    # WLAN
    (55, "A"): (ts_agg("wlan1_associations", "wlan2_associations", "wlan3_associations"), "time_series"),
    (56, "A"): (ts_agg("wlan1_channel", "wlan2_channel", "wlan3_channel"), "time_series"),
    (60, "A"): (
        f"{wlan_info_select('wlan1', '2.4 GHz')}\n"
        f"UNION ALL\n"
        f"{wlan_info_select('wlan2', '5 GHz')}\n"
        f"UNION ALL\n"
        f"{wlan_info_select('wlan3', 'Guest')}",
        "table",
    ),
    # System infos
    (45, "A"): (ts_agg("cpu_utilization"), "time_series"),
    (49, "A"): (ts_agg("ram_usage_fixed"), "time_series"),
    (49, "B"): (ts_agg("ram_usage_dynamic"), "time_series"),
    (49, "C"): (ts_agg("ram_usage_free"), "time_series"),
    (51, "A"): (ts_agg("cpu_temp"), "time_series"),
    (53, "A"): (ts_last("energy_consumption", "Energy (W)"), "table"),
    (20, "B"): (
        f"SELECT {last_not_null('external_ip', 'IPv4')}, {last_not_null('external_ipv6', 'IPv6')},\n"
        f"  {last_not_null('ipv6_prefix', 'IPv6 Prefix')}, {last_not_null('ipv6_prefix_length', 'IPv6 Prefix Length')},\n"
        f"  {last_not_null('model', 'Model')}, {last_not_null('serialnumber', 'Serial')},\n"
        f"  {last_not_null('softwareversion', 'SW Version')}, {last_not_null('last_auth_error', 'Last Auth Error')},\n"
        f"  {last_nonempty_text('myfritz_host_name', 'MyFritz Hostname')}\n"
        f"FROM {MEASUREMENT}\n"
        f"WHERE timestamp >= {ago('d', 1)}\n"
        f"  AND {BOX_FILTER}",
        "table",
    ),
    (74, "B"): (
        f"SELECT {last_not_null('physicallinktype', 'Link Type')}, {last_not_null('dsl_line_length', 'DSL Line Length')},\n"
        f"  {last_not_null('dsl_dslam_vendor', 'DSL DSLAM Vendor')}, {last_not_null('dsl_dslam_sw_version', 'DSL Model Version')},\n"
        f"  {last_not_null('dsl_line_mode', 'DSL Line Mode')}, {last_not_null('cable_cmts_vendor', 'Cable Vendor')},\n"
        f"  {last_not_null('cable_line_mode', 'Cable Line Mode')}, {last_not_null('cable_modem_version', 'Cable Modem Version')},\n"
        f"  {last_not_null('cable_num_ds_channels', 'Cable Downstream Channels')}, {last_not_null('cable_num_us_channels', 'Cable Upstream Channels')},\n"
        f"  {last_not_null('connection_status', 'IP Connection Status')}, {last_not_null('last_connection_error', 'Last Connection Error')},\n"
        f"  {last_not_null('remote_pop', 'Remote Pop')}, {last_not_null('physical_connection_status', 'Link Connection Status')}\n"
        f"FROM {MEASUREMENT}\n"
        f"WHERE timestamp >= {ago('d', 1)}\n"
        f"  AND {BOX_FILTER}",
        "table",
    ),
    # VPN / DynDNS
    (64, "A"): (ts_agg("vpn_user_num_active"), "time_series"),
    (68, "A"): (
        f"SELECT {last_not_null('vpn_type', 'VPN Type')}, {last_not_null('vpn_user_num_active', 'VPN User Active')}\n"
        f"FROM {MEASUREMENT}\n"
        f"WHERE timestamp >= {ago('d', 1)}\n"
        f"  AND {BOX_FILTER}\n"
        f"  AND (vpn_type IS NOT NULL OR vpn_user_num_active IS NOT NULL)",
        "table",
    ),
    (62, "A"): (
        f"SELECT {last_not_null('ddns_domain', 'Domain')},\n"
        f"  {bool_text('ddns_enabled', 'Enabled')},\n"
        f"  {last_not_null('ddns_mode', 'Mode')}, {last_not_null('ddns_provider_name', 'Provider')},\n"
        f"  {last_not_null('ddns_status_ipv4', 'Status IPv4')}, {last_not_null('ddns_status_ipv6', 'Status IPv6')}\n"
        f"FROM {MEASUREMENT}\n"
        f"WHERE timestamp >= {ago('d', 1)}\n"
        f"  AND {BOX_FILTER}\n"
        f"  AND (ddns_domain IS NOT NULL OR ddns_enabled IS NOT NULL OR ddns_mode IS NOT NULL\n"
        f"       OR ddns_provider_name IS NOT NULL OR ddns_status_ipv4 IS NOT NULL OR ddns_status_ipv6 IS NOT NULL)",
        "table",
    ),
    (66, "A"): (
        f"SELECT name AS \"Name\", vpn_type AS \"Type\",\n"
        f"  {bool_text('vpn_user_active', 'Active')}, {bool_text('vpn_user_connected', 'Connected')},\n"
        f"  {last_not_null('vpn_user_virtual_address', 'Virtual Address')}, {last_not_null('vpn_user_remote_address', 'Remote Address')}\n"
        f"FROM {MEASUREMENT}\n"
        f"WHERE timestamp >= {ago('d', 1)}\n"
        f"  AND {BOX_FILTER}\n"
        f"  AND name IS NOT NULL\n"
        f"  AND (vpn_user_active IS NOT NULL OR vpn_user_connected IS NOT NULL OR vpn_user_virtual_address IS NOT NULL OR vpn_user_remote_address IS NOT NULL)\n"
        f"GROUP BY name, vpn_type\n"
        f"ORDER BY name",
        "table",
    ),
    # Network hosts
    (72, "A"): (
        f"SELECT timestamp AS time, active_hosts_name AS \"Name\",\n"
        f"  active_hosts_mac AS \"MAC\", active_hosts_ipv4 AS \"IPv4\",\n"
        f"  active_hosts_type AS \"Type\", active_hosts_parent AS \"Parent\",\n"
        f"  active_hosts_port AS \"Port\", active_hosts_additional_text AS \"Info\",\n"
        f"  active_hosts_ipv4_last_used AS \"Last seen\"\n"
        f"FROM {MEASUREMENT}\n"
        f"WHERE timestamp >= {ago('h', 1)}\n"
        f"  AND {BOX_FILTER}\n"
        f"  AND active_hosts_name IS NOT NULL\n"
        f"ORDER BY timestamp DESC",
        "table",
    ),
    (73, "A"): (
        f"SELECT timestamp AS time, passive_hosts_name AS \"Name\",\n"
        f"  passive_hosts_mac AS \"MAC\", passive_hosts_ipv4 AS \"IPv4\",\n"
        f"  passive_hosts_port AS \"Port\"\n"
        f"FROM {MEASUREMENT}\n"
        f"WHERE timestamp >= {ago('h', 1)}\n"
        f"  AND {BOX_FILTER}\n"
        f"  AND passive_hosts_name IS NOT NULL\n"
        f"ORDER BY timestamp DESC",
        "table",
    ),
}

# refIds that should be dropped (replaced by single query)
SKIP_REFS = {
    (62, "B"), (62, "C"), (62, "D"), (62, "E"), (62, "F"),
    (20, "A"), (74, "A"), (2, "A"),
    (60, "B"), (60, "C"),
}


def build_system(src):
    d = copy.deepcopy(src)
    patch_inputs(d)
    d["uid"] = "fritzbox-system-questdb"
    d["title"] = "FRITZ!Box Router System (QuestDB)"

    for ann in d["annotations"]["list"]:
        if ann.get("datasource", {}).get("type") == "influxdb":
            ann["enable"] = True
            ann["datasource"] = ds()
            ann["target"] = {
                "format": FORMAT_TABLE,
                "queryType": "sql",
                "rawSql": (
                    f"SELECT timestamp AS time, log_entry AS text, log_type AS tags\n"
                    f"FROM {MEASUREMENT}\n"
                    f"WHERE {TF}\n"
                    f"  AND {BOX_FILTER}\n"
                    f"  AND log_entry IS NOT NULL\n"
                    f"  AND log_type = 'Internet connection'\n"
                    f"ORDER BY timestamp DESC"
                ),
                "selectedFormat": FORMAT_TABLE,
            }

    new_vars = []
    for v in d["templating"]["list"]:
        if v["name"] == "measurement":
            patch_measurement_var(v)
            new_vars.append(v)
        elif v["name"] == "boxtag":
            patch_boxtag_var(v, include_all=False)
            new_vars.append(v)
        elif v["name"] == "timezone":
            patch_timezone_var(v)
            new_vars.append(v)
    d["templating"]["list"] = new_vars

    def patch_panels(panels):
        for p in panels:
            if p.get("type") == "row":
                patch_panels(p.get("panels", []))
                continue
            if "datasource" in p:
                p["datasource"] = ds()
            pid = p.get("id")
            new_targets = []
            source_targets = p.get("targets", [])
            if not source_targets and pid in {panel_id for panel_id, _ in SYSTEM_QUERIES}:
                source_targets = [{"refId": "A"}]
            for t in source_targets:
                ref = t.get("refId", "A")
                if (pid, ref) in SKIP_REFS:
                    continue
                key = (pid, ref)
                if key in SYSTEM_QUERIES:
                    sql, fmt = SYSTEM_QUERIES[key]
                    new_targets.append(target(sql, ref, fmt))
                else:
                    # fallback: keep structure, replace query
                    new_targets.append(target_table("SELECT 1 LIMIT 0", ref))
            p["targets"] = new_targets
            if pid in {20, 60, 62, 66, 68, 74}:
                p.setdefault("options", {})["showHeader"] = True
                p["transformations"] = []

    patch_panels(d["panels"])
    return d


# ---------------------------------------------------------------------------
# Logs dashboard
# ---------------------------------------------------------------------------

def build_logs(src):
    d = copy.deepcopy(src)
    patch_inputs(d)
    d["uid"] = "fritzbox-router-logs-questdb"
    d["title"] = "FRITZ!Box Router Logs (QuestDB)"

    for v in d["templating"]["list"]:
        if v["name"] == "measurement":
            patch_measurement_var(v)
        elif v["name"] == "boxtag":
            patch_boxtag_var(v)
        elif v["name"] == "log_type":
            v["datasource"] = ds()
            q = (
                f"SELECT DISTINCT log_type FROM {MEASUREMENT}\n"
                f"WHERE {TF}\n"
                f"  AND {BOX_FILTER}\n"
                f"  AND log_type IS NOT NULL ORDER BY log_type"
            )
            v["definition"] = q
            v["query"] = q

    sql = (
        "SELECT timestamp AS \"Date/Time\", log_type AS \"Log Type\", log_entry AS \"Entry\"\n"
        + f"FROM {MEASUREMENT}\n"
        + f"WHERE {TF}\n"
        + f"  AND {BOX_FILTER}\n"
        + "  AND log_entry IS NOT NULL\n"
        + "  AND log_type ~ '^${log_type:regex}$'\n"
        + "ORDER BY timestamp DESC"
    )

    for p in d["panels"]:
        if "datasource" in p:
            p["datasource"] = ds()
        p["targets"] = [target_table(sql, "A")]

    return d


# ---------------------------------------------------------------------------
# Call log dashboard
# ---------------------------------------------------------------------------

def build_calllog(src):
    d = copy.deepcopy(src)
    patch_inputs(d)
    d["uid"] = "fritzbox-call-logs-questdb"
    d["title"] = "FRITZ!Box Call Logs (QuestDB)"

    for v in d["templating"]["list"]:
        if v["name"] == "measurement":
            patch_measurement_var(v)
        elif v["name"] == "boxtag":
            patch_boxtag_var(v)

    sql = (
        "SELECT timestamp AS \"Time\",\n"
        "  last_not_null(call_list_type) AS \"Call Type\",\n"
        "  last_not_null(call_list_caller_number) AS \"Number\",\n"
        "  last_not_null(call_list_caller_name) AS \"Name\",\n"
        "  last_not_null(call_list_duration) AS \"Duration\",\n"
        "  last_not_null(call_list_extension) AS \"Extension\",\n"
        "  last_not_null(call_list_number_called) AS \"Number called\"\n"
        + f"FROM {MEASUREMENT}\n"
        + f"WHERE {TF}\n"
        + f"  AND {BOX_FILTER}\n"
        + "  AND (\n"
        + "    call_list_type IS NOT NULL OR\n"
        + "    call_list_caller_number IS NOT NULL OR\n"
        + "    call_list_caller_name IS NOT NULL OR\n"
        + "    call_list_duration IS NOT NULL OR\n"
        + "    call_list_extension IS NOT NULL OR\n"
        + "    call_list_number_called IS NOT NULL\n"
        + "  )\n"
        + "GROUP BY timestamp, uid\n"
        + "ORDER BY timestamp DESC"
    )

    for p in d["panels"]:
        if "datasource" in p:
            p["datasource"] = ds()
        p["targets"] = [target_table(sql, "A")]
        p["transformations"] = [
            {
                "id": "sortBy",
                "options": {
                    "fields": {},
                    "sort": [{"desc": True, "field": "Time"}],
                },
            },
            {
                "id": "filterByValue",
                "options": {
                    "filters": [
                        {
                            "config": {
                                "id": "equal",
                                "options": {"value": "undefined"},
                            },
                            "fieldName": "Call Type",
                        }
                    ],
                    "match": "any",
                    "type": "exclude",
                },
            },
            {
                "id": "organize",
                "options": {
                    "excludeByName": {},
                    "indexByName": {
                        "Time": 0,
                        "Number": 1,
                        "Name": 2,
                        "Call Type": 3,
                        "Duration": 4,
                        "Extension": 5,
                        "Number called": 6,
                    },
                    "renameByName": {},
                },
            },
        ]

    return d


# ---------------------------------------------------------------------------
# Home Automation dashboard
# ---------------------------------------------------------------------------

HA_QUERIES = {
    (2, "A"): (latest_by_name("ha_temperature", "Temperature"), "table"),
    (24, "A"): (
        ts_agg_by_name("ha_temperature", "Temperature", "median", metric_expr="name"),
        "time_series",
    ),
    (22, "A"): (
        ts_agg_by_name(
            "ha_heating_tist",
            "Temp ist",
            "median",
            extra_filter="ha_heating_tist < 253",
            metric_expr="name || ' (actual)'",
        ),
        "time_series",
    ),
    (22, "B"): (
        ts_agg_by_name(
            "ha_heating_tsoll",
            "Temp soll",
            "first",
            extra_filter="ha_heating_tsoll < 253",
            metric_expr="name || ' (set)'",
        ),
        "time_series",
    ),
    (20, "A"): (
        latest_by_name("ha_heating_boostactive", "Boost"),
        "table",
    ),
    (20, "C"): (
        latest_by_name("ha_heating_summeractive", "Summer"),
        "table",
    ),
    (20, "B"): (
        latest_by_name("ha_heating_holidayactive", "Holiday"),
        "table",
    ),
    (20, "I"): (
        latest_by_name("ha_heating_lock", "Locked"),
        "table",
    ),
    (20, "D"): (
        latest_by_name("ha_heating_devicelock", "Device Locked"),
        "table",
    ),
    (20, "E"): (
        latest_by_name("ha_heating_tist", "Temp ist"),
        "table",
    ),
    (20, "F"): (
        latest_by_name("ha_heating_tsoll", "Temp soll"),
        "table",
    ),
    (20, "G"): (
        latest_by_name("ha_heating_komfort", "Temp komfort"),
        "table",
    ),
    (20, "H"): (
        latest_by_name("ha_heating_absenk", "Temp absenk"),
        "table",
    ),
    (8, "A"): (
        latest_by_name("ha_heating_battery", "Battery"),
        "table",
    ),
    (18, "A"): (
        ts_agg_by_name(
            "ha_heating_windowopenactiv",
            "Window State",
            "first",
            value_expr="CASE WHEN ha_heating_windowopenactiv != 0 THEN 1 ELSE 0 END",
            metric_expr="name",
        ),
        "time_series",
    ),
    (6, "A"): (
        ts_agg_by_name("ha_powermeter_power", "power", "avg", metric_expr="name || ' power'"),
        "time_series",
    ),
    (6, "B"): (
        ts_agg_by_name("ha_powermeter_voltage", "voltage", "avg", metric_expr="name || ' voltage'"),
        "time_series",
    ),
    (6, "C"): (
        ts_agg_by_name("ha_powermeter_energy", "energy", "avg", metric_expr="name || ' energy'"),
        "time_series",
    ),
    (28, "A"): (
        f"SELECT name AS \"Name\", ha_device_present AS \"Device Present\",\n"
        f"  ha_devicefunctions AS \"Functions\", ha_fw_version AS \"Firmware Version\",\n"
        f"  ha_hun_fun_interfaces AS \"HAN-FAN Functions\", ha_hun_fun_unittype AS \"HAN-FAN Unit Type\",\n"
        f"  ha_manufacturer AS \"Manufacturer\", ha_product_name AS \"Product Name\"\n"
        f"FROM {MEASUREMENT}\n"
        f"WHERE timestamp >= {ago('h', 12)}\n"
        f"  AND {BOX_FILTER}\n"
        f"  AND name IS NOT NULL\n"
        f"LATEST ON timestamp PARTITION BY box, name\n"
        f"ORDER BY name",
        "table",
    ),
    (34, "A"): (
        f"SELECT name AS \"Name\", ha_product_name AS \"Product Name\",\n"
        f"  ha_battery_low AS \"Battery Low\", ha_battery_percent AS \"Battery Level\",\n"
        f"  ha_simpleonoff_state AS \"Device State\", ha_switch_state AS \"Switch State\",\n"
        f"  ha_switch_mode AS \"Switch Mode\", ha_switch_lock AS \"Switch Lock\",\n"
        f"  ha_switch_devicelock AS \"Switch Device Lock\"\n"
        f"FROM {MEASUREMENT}\n"
        f"WHERE timestamp >= {ago('h', 12)}\n"
        f"  AND {BOX_FILTER}\n"
        f"  AND name IS NOT NULL\n"
        f"LATEST ON timestamp PARTITION BY box, name\n"
        f"ORDER BY name",
        "table",
    ),
    (32, "A"): (
        ts_agg_by_name(
            "ha_switch_state",
            "Switch State",
            "first",
            value_expr="CASE WHEN ha_switch_state != 0 THEN 1 ELSE 0 END",
            metric_expr="name",
        ),
        "time_series",
    ),
    (4, "A"): (
        ts_agg_by_name(
            "ha_simpleonoff_state",
            "Device State",
            "first",
            value_expr="CASE WHEN ha_simpleonoff_state != 0 THEN 1 ELSE 0 END",
            metric_expr="name",
        ),
        "time_series",
    ),
    (33, "A"): (
        ts_agg_by_name(
            "ha_alert",
            "Alert State",
            "first",
            value_expr="CASE WHEN ha_alert != 0 THEN 1 ELSE 0 END",
            metric_expr="name",
        ),
        "time_series",
    ),
}


def build_homeauto(src):
    d = copy.deepcopy(src)
    patch_inputs(d)
    d["uid"] = "fritzbox-home-automation-questdb"
    d["title"] = "FRITZ!Box Home Automation (QuestDB)"
    for v in d["templating"]["list"]:
        if v["name"] == "measurement":
            patch_measurement_var(v)
        elif v["name"] == "boxtag":
            patch_boxtag_var(v)

    def patch_panels(panels):
        filtered = []
        for p in panels:
            if p.get("type") == "row":
                patch_panels(p.get("panels", []))
                filtered.append(p)
                continue
            if "datasource" in p:
                p["datasource"] = ds()
            new_targets = []
            for t in p.get("targets", []):
                ref = t.get("refId", "A")
                key = (p.get("id"), ref)
                if key in HA_QUERIES:
                    sql, fmt = HA_QUERIES[key]
                    new_targets.append(target(sql, ref, fmt))
                else:
                    new_targets.append(target_table("SELECT 1 LIMIT 0", ref))
            p["targets"] = new_targets
            if p.get("id") in {20, 28, 34, 8}:
                p.setdefault("options", {})["showHeader"] = True
                p["transformations"] = []
            filtered.append(p)
        panels[:] = filtered

    patch_panels(d["panels"])
    return d


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    repo_root = os.path.dirname(os.path.dirname(__file__))
    grafana_dir = os.path.join(repo_root, "grafana")
    base = os.path.join(grafana_dir, "influx2_dashboards")
    out  = os.path.join(grafana_dir, "questdb_dashboards")
    os.makedirs(out, exist_ok=True)

    pairs = [
        ("fritzbox_system_dashboard.json",         build_system),
        ("fritzbox_logs_dashboard.json",            build_logs),
        ("fritzbox_call_log_dashboard.json",        build_calllog),
        ("fritzbox_home_automation_dashboard.json", build_homeauto),
    ]

    for fname, builder in pairs:
        with open(os.path.join(base, fname)) as f:
            src = json.load(f)
        result = builder(src)
        dest = os.path.join(out, fname)
        with open(dest, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"Written: {dest}")
