#!/usr/bin/env python3
#
# tests/test_event_deduplication.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from app.fritzbox import service_definitions
from app.fritzbox.service_definitions import helpers, logs
from app.fritzbox.service_definitions.homeauto import (
    force_int,
    reformat_homeauto_device_list,
)
from app.fritzbox.service_definitions.telephone_list import CallLog
from app.fritzbox.service_handler import FritzBoxLuaService
from app.influxdb.handler import (
    DatabaseConfigError,
    InfluxHandler,
    questdb_event_columns,
    questdb_events_ddl,
    questdb_events_dedup_ddl,
    questdb_expected_columns,
)
from app.utils.measurement import FritzMeasurement
from tests.questdb_fake import fake_questdb, fake_write_client

# Header of the FRITZ!Box call-list CSV export.
CALL_LOG_HEADER = '"Typ";"Datum";"Name";"Rufnummer";"Nebenstelle";"Eigene Rufnummer";"Dauer"'


# The warning rate limiter is process-wide; reset it so no test sees another test's suppressed warning.
@pytest.fixture(autouse=True)
def _reset_warning_rate_limit():
    helpers._last_warning.clear()
    yield
    helpers._last_warning.clear()


def _call_log(rows: list[str]) -> CallLog:
    return CallLog("sep=;\n" + CALL_LOG_HEADER + "\n" + "\n".join(rows))


def test_malformed_call_log_line_is_skipped_without_personal_data(caplog):
    good = '1;"01.01.26 10:00";"Good";"555-0199";"";"";"0:05"'
    raw_line = '1;"invalid-date";"Private Person";"555-0100";'
    log = _call_log([raw_line, good])
    assert [entry.caller_name for entry in log.entries] == ["Good"]
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "Skipped 1 invalid call log line" in messages
    assert "Private Person" not in messages
    assert "555-0100" not in messages


def test_skipped_call_log_lines_are_logged_once_per_interval(caplog):
    caplog.set_level("DEBUG", logger="fritzFlux")
    for _ in range(3):
        _call_log(['1;"invalid-date";"x";"1";'])
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "invalid call log" in r.getMessage()]
    assert len(warnings) == 1


def test_force_int_warns_rate_limited_for_malformed_values_only(caplog):
    caplog.set_level("DEBUG", logger="fritzFlux")
    assert force_int({"a": {"b": "12"}}, "a.b") == 12
    assert force_int({}, "a.b") == 0
    assert force_int({"a": {"b": "  "}}, "a.b") == 0
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    for _ in range(3):
        assert force_int({"a": {"b": "1.5"}}, "a.b", default=7) == 7
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "'a.b'" in warnings[0].getMessage() and "1.5" not in warnings[0].getMessage()


@pytest.mark.parametrize(
    ("current", "minimum", "maximum", "expected"),
    [
        ("7.62-123456", "7.29", "7.62", True),
        ("7.63-123456", "7.29", "7.62", False),
        ("154.07.57", "154.07.00", None, True),
        ("7.28", "7.29", None, False),
    ],
)
def test_os_version_match_ignores_build_suffix(current, minimum, maximum, expected):
    service = FritzBoxLuaService(
        {"name": "x", "os_min_versions": minimum, "os_max_versions": maximum, "value_instances": {}}
    )
    assert service.os_version_match(current) is expected


def test_homeauto_skips_non_dictionary_device_entries():
    payload = {"devicelist": {"device": [None, {"@id": "device-1", "@functionbitmask": "0"}]}}

    assert reformat_homeauto_device_list(payload)["devicelist"]["device"] == [
        {"@id": "device-1", "@functionbitmask": "0", "@devicefunctions": []}
    ]


@pytest.mark.parametrize("metric_name,field", [
    ("ha_heating_battery", "battery"),
    ("ha_heating_errorcode", "errorcode"),
    ("ha_heating_batterylow", "batterylow"),
])
def test_missing_heating_numbers_are_skipped_but_real_zero_is_kept(metric_name, field):
    service = next(service for service in service_definitions.lua_services if service["name"] == "Home Automation")
    metric = service["value_instances"][metric_name]["next"]
    exclude = metric["exclude_filter_function"]
    value = metric["value_function"]

    assert exclude({"hkr": {}}) is True
    assert exclude({"hkr": {field: "invalid"}}) is True
    assert exclude({"hkr": {field: 0}}) is False
    assert value({"hkr": {field: 0}}) == 0


@pytest.mark.parametrize("metric_name, field", [
    ("ha_heating_tsoll", "tsoll"),
    ("ha_heating_komfort", "komfort"),
    ("ha_heating_absenk", "absenk"),
])
def test_missing_setpoint_is_skipped_but_explicit_avm_sentinels_pass_through(metric_name, field):
    service = next(service for service in service_definitions.lua_services if service["name"] == "Home Automation")
    metric = service["value_instances"][metric_name]["next"]
    assert metric["exclude_filter_function"]({"hkr": {}}) is True
    assert metric["exclude_filter_function"]({"hkr": {field: "broken"}}) is True
    for sentinel in (253, 254):
        data = {"hkr": {field: str(sentinel)}}
        assert metric["exclude_filter_function"](data) is False
        assert metric["value_function"](data) == sentinel


def make_handler(version: str = "questdb") -> InfluxHandler:
    handler = InfluxHandler.__new__(InfluxHandler)
    handler.version = version
    handler.config = SimpleNamespace(measurement_name="fritzbox_AA1", data_retention_days=0)
    return handler


def _log_service() -> dict:
    return next(
        s for s in service_definitions.lua_services
        if s["name"] == "System logs" and s["os_min_versions"] == "7.39"
    )


def test_events_ddl_creates_wal_table_with_upsert_keys():
    ddl = questdb_events_ddl("fritzbox_AA1_events")
    assert ddl.startswith('CREATE TABLE IF NOT EXISTS "fritzbox_AA1_events"')
    assert "PARTITION BY DAY WAL DEDUP UPSERT KEYS(\"timestamp\", \"box\", \"log_type\", \"uid\")" in ddl


def test_events_dedup_alter_for_existing_table():
    assert questdb_events_dedup_ddl("fritzbox_AA1_events") == (
        'ALTER TABLE "fritzbox_AA1_events" DEDUP ENABLE UPSERT KEYS("timestamp", "box", "log_type", "uid");'
    )


def test_event_columns_are_split_from_metric_columns():
    events = questdb_event_columns()
    metrics = questdb_expected_columns()
    assert {"log_entry", "call_list_type", "call_list_duration", "uid", "log_type"} <= set(events)
    assert not {"log_entry", "call_list_type", "call_list_duration"} & set(metrics)


@pytest.mark.parametrize(
    ("service_name", "path"),
    [("VPN Users", "data.vpnInfo.userConnections"),
     ("VPN Users - IPSec", "data.init.userConnections"),
     ("VPN Users - WireGuard", "data.init.boxConnections")],
)
def test_vpn_num_active_counts_active_flag(service_name, path):
    service = next(service for service in service_definitions.lua_services if service["name"] == service_name)
    count = service["value_instances"]["vpn_user_num_active"]["value_function"]
    data = {"data": {"vpnInfo": {"userConnections": {}}}} if path.startswith("data.vpnInfo") else {
        "data": {"init": {"userConnections": {}, "boxConnections": {}}}
    }
    connections = {"connected-only": {"connected": True, "active": False},
                   "active-only": {"connected": False, "active": True}}
    cursor = data["data"]
    for part in path.split(".")[1:]:
        cursor = cursor[part]
    cursor.update(connections)

    assert count(data) == 1


def test_vpn_identity_tag_prefers_id_and_retains_name():
    service = next(service for service in service_definitions.lua_services if service["name"] == "VPN Users - WireGuard")
    tags = service["value_instances"]["vpn_user_active"]["next"]["tags_function"]

    assert tags({"id": "vpn-1", "name": "Same"}) == {
        "id": "vpn-1", "name": "Same", "vpn_type": "WireGuard",
    }


def test_vpn_identity_tag_falls_back_to_name_when_id_is_unavailable():
    service = next(service for service in service_definitions.lua_services if service["name"] == "VPN Users - WireGuard")
    tags = service["value_instances"]["vpn_user_active"]["next"]["tags_function"]

    assert tags({"name": "Peer"}) == {"name": "Peer", "vpn_type": "WireGuard"}


def test_log_uid_is_stable_and_differs_per_message():
    tags = _log_service()["value_instances"]["log_entry"]["next"]["tags_function"]
    first = tags({"date": "01.01.26", "time": "10:00:00", "msg": "A"})
    again = tags({"date": "01.01.26", "time": "10:00:00", "msg": "A"})
    other = tags({"date": "01.01.26", "time": "10:00:00", "msg": "B"})
    assert first == again
    assert first["uid"] != other["uid"]


def test_log_timestamp_comes_from_the_entry_not_the_poll_time():
    assert logs.parse_modern_log_timestamp({"date": "01.01.26", "time": "10:00:00"}) == datetime(2026, 1, 1, 10, 0, 0)


def test_two_messages_in_the_same_second_stay_two_series():
    handler = make_handler()
    ts = datetime(2026, 1, 1, 10, 0, 0).astimezone()
    measurements = [
        FritzMeasurement("log_entry", text, data_type=str, box_tag="b", timestamp=ts,
                         additional_tags={"log_type": "System", "uid": uid}, is_event=True)
        for text, uid in (("A", "u1"), ("B", "u2"), ("A", "u1"))
    ]
    lines, _ = handler.bundle_measurements(measurements)
    assert len({line.split(" ")[0] for line in lines}) == 2
    assert all(line.startswith("fritzbox_AA1_events,") for line in lines)


def test_events_route_to_events_table_only_on_questdb():
    event = FritzMeasurement("log_entry", "A", data_type=str, box_tag="b", is_event=True)
    metric = FritzMeasurement("cpu_temp", 1, data_type=int, box_tag="b")
    questdb_lines, _ = make_handler("questdb").bundle_measurements([event, metric])
    influx_lines, _ = make_handler(2).bundle_measurements([event, metric])
    assert sorted(line.split(",")[0] for line in questdb_lines) == ["fritzbox_AA1_events", "fritzbox_AA1_metrics"]
    assert {line.split(",")[0] for line in influx_lines} == {"fritzbox_AA1"}


def test_event_schema_enables_dedup_only_when_keys_differ():
    handler = make_handler()
    for current, expect_alter in (([], True), ([["timestamp"], ["box"], ["log_type"], ["uid"]], False)):
        queries = []

        async def fake_exec(query, _current=current, _queries=queries):
            _queries.append(query)
            if "table_columns(" in query:
                return {"dataset": _current}
            if "ttlValue" in query:
                return {"dataset": [[None, None]]}
            return {"ddl": "OK"}

        handler._questdb = fake_questdb(fake_exec)
        assert asyncio.run(handler._ensure_questdb_events_table()) is True
        assert any("DEDUP ENABLE UPSERT KEYS" in q for q in queries) is expect_alter


def test_in_memory_tracking_is_gone():
    assert not any(s.get("track") for s in service_definitions.lua_services)
    assert all(s.get("event") for s in service_definitions.lua_services if s["name"].endswith("logs"))
    service = FritzBoxLuaService(_log_service())
    assert service.is_event is True
    assert not hasattr(service, "tracked_measurements")
    assert not hasattr(service, "skip_tracked_measurement")


def _call_entry(uid: str, ts: datetime) -> list[FritzMeasurement]:
    return [
        FritzMeasurement(f"call_list_{n}", "x", data_type=str, box_tag="b", timestamp=ts,
                         additional_tags={"uid": uid}, is_event=True)
        for n in range(7)
    ]


def test_batch_never_cuts_through_an_event():
    handler = make_handler()
    ts = datetime(2026, 1, 1, 10, 0, 0).astimezone()
    handler.buffer = _call_entry("u1", ts) + _call_entry("u2", ts)
    handler.current_measurements_per_write = 10
    assert handler._batch_size_keeping_events_whole() == 14
    handler.current_measurements_per_write = 7
    assert handler._batch_size_keeping_events_whole() == 7
    handler.current_measurements_per_write = 1
    assert handler._batch_size_keeping_events_whole() == 7
    lines, _ = handler.bundle_measurements(handler.buffer[:14])
    assert len(lines) == 2 and all(line.count("call_list_") == 7 for line in lines)


def test_batch_limit_is_unchanged_for_plain_metrics():
    handler = make_handler()
    handler.buffer = [FritzMeasurement(f"m{i}", i, data_type=int, box_tag="b") for i in range(20)]
    handler.current_measurements_per_write = 10
    assert handler._batch_size_keeping_events_whole() == 10


def test_influx_events_have_no_uid_tag_and_distinct_stable_subsecond_points():
    ts = datetime(2026, 1, 1, 10, 0, 0).astimezone()
    handler = make_handler(2)

    def line(text, uid):
        point = FritzMeasurement("log_entry", text, data_type=str, box_tag="b", timestamp=ts,
                                 additional_tags={"log_type": "System", "uid": uid}, is_event=True)
        return handler.bundle_measurements([point])[0][0]

    a, b = line("A", "u1"), line("B", "u2")
    assert "uid" not in a
    assert a == line("A", "u1")
    assert a.split(" ")[0] == b.split(" ")[0]
    assert a.rsplit(" ", 1)[1] != b.rsplit(" ", 1)[1]
    second = int(ts.timestamp()) * 1_000_000_000
    assert 0 <= int(a.rsplit(" ", 1)[1]) - second < 1_000_000_000


def test_questdb_events_keep_whole_second_timestamp_and_uid_tag():
    ts = datetime(2026, 1, 1, 10, 0, 0).astimezone()
    point = FritzMeasurement("log_entry", "A", data_type=str, box_tag="b", timestamp=ts,
                             additional_tags={"uid": "u1"}, is_event=True)
    line = make_handler("questdb").bundle_measurements([point])[0][0]
    assert "uid=u1" in line
    assert line.endswith(f" {int(ts.timestamp())}")


def _real_call_list_buffer(entries: int) -> list[FritzMeasurement]:
    """Buffer produced by the real extraction path of the call-list service."""
    from datetime import UTC

    from app.fritzbox.handler import FritzBoxLuaHandler

    rows = [f'"1";"01.01.26 10:{i % 60:02d}";"N{i}";"0{i}";"Ext";"123";"0:0{i % 10}"' for i in range(entries)]
    entries_list = _call_log(rows).entries
    service = FritzBoxLuaService(next(
        s for s in service_definitions.lua_services if s["name"] == "Phone call list"))
    handler = FritzBoxLuaHandler.__new__(FritzBoxLuaHandler)
    handler.name = "test"
    handler.config = SimpleNamespace(box_tag="b", timezone=UTC)
    handler.current_result_list = []
    handler.extract_service_values(service, entries_list, datetime.now(UTC))
    return handler.current_result_list


def test_real_call_list_extraction_keeps_each_event_contiguous_through_the_writer():
    handler = make_handler()
    handler.buffer = _real_call_list_buffer(200)
    assert len(handler.buffer) > 1_000
    for limit in (1_000, 500, 250, 7, 3, 1):  # halving after HTTP 413 included
        handler.current_measurements_per_write = limit
        pending = list(handler.buffer)
        while pending:
            handler.buffer = pending
            size = handler._batch_size_keeping_events_whole()
            lines, _ = handler.bundle_measurements(pending[:size])
            assert all(line.count("call_list_") == 7 for line in lines), (limit, size)
            pending = pending[size:]


def test_parallel_services_do_not_lose_or_interleave_measurements(monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC

    from app.fritzbox.handler import FritzBoxLuaHandler

    class SlowHandler(FritzBoxLuaHandler):
        def extract_value(self, *args, **kwargs):
            super().extract_value(*args, **kwargs)
            time.sleep(0.001)  # force thread interleaving between metrics

    original_tags = FritzMeasurement.tags

    def slow_tags(self):
        time.sleep(0.0002)  # widens the read-sort-write window of a shared-list implementation
        return original_tags.fget(self)

    monkeypatch.setattr(FritzMeasurement, "tags", property(slow_tags))
    rows = [f'"1";"01.01.26 10:{i % 60:02d}";"N{i}";"0{i}";"Ext";"123";"0:01"' for i in range(60)]
    calls = _call_log(rows).entries
    logs_data = {"data": {"log": [{"date": "01.01.26", "time": f"10:00:{i:02d}", "msg": f"m{i}"} for i in range(50)]}}

    def by_name(name, os_min="7.29"):
        return FritzBoxLuaService(next(
            s for s in service_definitions.lua_services if s["name"] == name and s["os_min_versions"] == os_min))

    metric = FritzBoxLuaService({
        "name": "m", "os_min_versions": "7.29",
        "value_instances": {f"metric_{i}": {"type": int, "value_function": lambda d: d} for i in range(30)},
    })
    handler = SlowHandler.__new__(SlowHandler)
    handler.name = "test"
    handler.config = SimpleNamespace(box_tag="b", timezone=UTC)
    handler.current_result_list = []
    now = datetime.now(UTC)
    jobs = [(by_name("Phone call list"), calls), (by_name("System logs", "7.39"), logs_data), (metric, 5)]
    with ThreadPoolExecutor(3) as pool:
        for future in [pool.submit(handler.extract_service_values, s, d, now) for s, d in jobs]:
            future.result()

    result = handler.current_result_list
    assert len(result) == 60 * 7 + 50 + 30
    # the writer needs every event contiguous in the full list, not only among call-list items
    positions: dict[str, list[int]] = {}
    for index, measurement in enumerate(result):
        if measurement.name.startswith("call_list_"):
            positions.setdefault(measurement.additional_tags["uid"], []).append(index)
    assert len(positions) == 60
    for indexes in positions.values():
        assert indexes == list(range(indexes[0], indexes[0] + 7))


def test_buffer_overflow_drop_does_not_cut_an_event():
    handler = make_handler()
    handler.max_measurements_buffer_size = 21
    handler.max_measurements_per_write = 10
    handler.max_measurements_buffer_warning = 80
    handler.out_of_retention_period_range = False
    ts = datetime(2026, 1, 1, 10, 0, 0).astimezone()
    handler.buffer = _call_entry("u1", ts) + _call_entry("u2", ts) + _call_entry("u3", ts)
    handler.append_measurement(_call_entry("u4", ts)[0])
    # the 10-measurement drop limit ends inside u2, so u1 and u2 go together and u3 stays whole
    assert [m.additional_tags["uid"] for m in handler.buffer[:7]] == ["u3"] * 7
    assert len(handler.buffer) == 8


# Event identity
def test_log_uid_is_independent_of_the_python_process():
    # md5 of the message text: pinned so a hash-seed or algorithm change cannot silently re-key history
    tags = _log_service()["value_instances"]["log_entry"]["next"]["tags_function"]
    assert tags({"date": "01.01.26", "time": "10:00:00", "msg": "A"})["uid"] == "7fc56270e7a70fa81a5935b72eacbe29"


def test_influx_event_offset_is_pinned_across_restarts():
    # sha256-based, not hash(): the same uid must map to the same nanosecond after a restart
    point = FritzMeasurement("log_entry", "A", data_type=str, box_tag="b",
                             additional_tags={"uid": "u1"}, is_event=True)
    assert InfluxHandler._event_offset_ns(point) == InfluxHandler._event_offset_ns(point) == 71374379


def test_identical_messages_in_the_same_second_collapse_to_one_event():
    # documented limit: the FRITZ!Box log carries no entry id, so such entries are indistinguishable
    tags = _log_service()["value_instances"]["log_entry"]["next"]["tags_function"]
    first = tags({"date": "01.01.26", "time": "10:00:00", "msg": "same"})
    second = tags({"date": "01.01.26", "time": "10:00:00", "msg": "same"})
    assert first == second


def test_call_list_polled_twice_yields_identical_points_on_every_backend():
    for version in ("questdb", 1, 2):
        handler = make_handler(version)
        first, _ = handler.bundle_measurements(_real_call_list_buffer(5))
        second, _ = handler.bundle_measurements(_real_call_list_buffer(5))
        assert first == second
        assert len(first) == 5 and len(set(first)) == 5


def test_call_list_entries_with_the_same_timestamp_stay_distinct():
    rows = ['"1";"01.01.26 10:00";"A";"1";"Ext";"123";"0:01"', '"1";"01.01.26 10:00";"B";"2";"Ext";"123";"0:02"']
    first, second = _call_log(rows).entries
    assert first.date_time == second.date_time
    assert first.hash != second.hash


def test_call_list_identity_changes_when_name_or_duration_change():
    # documented limit: the uid is the md5 of the whole CSV row, so a later edit creates a new event
    def uid(name, duration="0:01"):
        return _call_log([f'"1";"01.01.26 10:00";"{name}";"1";"Ext";"123";"{duration}"']).entries[0].hash
    assert uid("A") == uid("A")
    assert uid("A") != uid("B")
    assert uid("A") != uid("A", duration="0:02")


def test_call_list_events_have_no_log_type_tag_but_the_events_table_keys_it():
    handler = make_handler()
    lines, _ = handler.bundle_measurements(_real_call_list_buffer(1))
    assert "log_type" not in lines[0].split(" ")[0]
    assert "log_type" in questdb_event_columns()


def test_questdb_upsert_line_carries_every_call_list_field():
    lines, valid = make_handler().bundle_measurements(_real_call_list_buffer(3))
    assert len(valid) == 21 and len(lines) == 3
    for line in lines:
        assert all(f"call_list_{name}=" in line for name in (
            "type", "caller_name", "caller_number", "caller_location", "extension", "number_called", "duration"))


# Writer error handling
def _Response(status_code, text=""):
    return httpx.Response(status_code, text=text)


def _client(version, outcomes):
    """Write client of the real package; `payloads` records each request body in line protocol order."""
    outcomes, payloads = list(outcomes), []

    def respond(request):
        payloads.append(request.content.decode().rstrip("\n"))
        return outcomes.pop(0)

    client = fake_write_client(version, respond)
    client.payloads = payloads
    return client


def _writer(version, outcomes, buffer):
    handler = make_handler(version)
    handler.name = "Test"
    handler.config = SimpleNamespace(
        measurement_name="fritzbox_AA1", data_retention_days=0, username=None, password=None,
        hostname="h", organization="o", bucket="b", database="d")
    handler.client = _client(version, outcomes)
    handler.buffer = list(buffer)
    handler._write_lock = asyncio.Lock()
    handler.connection_lost = False
    handler.last_connection_warning = None
    handler.last_write_retry = None
    handler.retry_interval = handler.current_retry_interval = 5
    handler.max_retry_interval = 120
    handler.out_of_retention_period_range = False
    handler.retention_buffer_sorted = False
    handler.last_retention_warning = None
    handler.max_measurements_per_write = handler.current_measurements_per_write = 1_000
    return handler


def test_413_for_an_unsplittable_event_drops_it_whole_and_says_so(caplog):
    entry = _call_entry("u1", datetime(2026, 1, 1, 10, 0, 0).astimezone())
    other = _call_entry("u2", datetime(2026, 1, 1, 10, 1, 0).astimezone())
    handler = _writer("questdb", [_Response(413, "too big"), _Response(204)], entry + other)
    handler.current_measurements_per_write = 1
    asyncio.run(handler._write_data_unlocked(force=True))
    assert len(handler.buffer) == 7  # exactly the first event is gone, the second is untouched
    assert handler.buffer[0].tags["uid"] == "u2"
    assert "Dropping 7" in caplog.text and "cannot be split" in caplog.text
    asyncio.run(handler._write_data_unlocked(force=True))
    assert handler.buffer == []
    assert handler.client.payloads[1].count("call_list_") == 7  # complete row, no partial upsert


def test_413_halves_the_batch_while_it_can_still_shrink():
    metrics = [FritzMeasurement(f"m{i}", i, data_type=int, box_tag="b") for i in range(8)]
    handler = _writer("questdb", [_Response(413)], metrics)
    handler.current_measurements_per_write = 8
    asyncio.run(handler._write_data_unlocked(force=True))
    assert len(handler.buffer) == 8 and handler.current_measurements_per_write == 4


def _metrics(count):
    # One second apart, so every measurement is its own line.
    return [
        FritzMeasurement(f"m{i}", i, data_type=int, box_tag="b", timestamp=datetime(2026, 1, 1, 10, 0, i).astimezone())
        for i in range(count)
    ]


@pytest.mark.parametrize("version", [1, 2])
def test_influx_retention_drop_discards_the_batch_without_a_retry(version):
    body = "partial write: points beyond retention policy dropped=2"
    handler = _writer(version, [_Response(400, body)], _metrics(3))
    asyncio.run(handler._write_data_unlocked(force=True))
    assert handler.buffer == [] and handler.last_write_retry is None


@pytest.mark.parametrize("version", [1, 2])
def test_influx_payload_the_server_will_never_accept_is_dropped(version, caplog):
    handler = _writer(version, [_Response(400, "field type conflict: input field is int")], _metrics(3))
    asyncio.run(handler._write_data_unlocked(force=True))
    assert handler.buffer == [] and "non-retryable" in caplog.text


@pytest.mark.parametrize("version", ["questdb", 1, 2])
def test_retryable_status_keeps_the_buffer_and_follows_retry_after(version):
    handler = _writer(version, [httpx.Response(429, headers={"Retry-After": "17"})], _metrics(3))
    asyncio.run(handler._write_data_unlocked(force=True))
    assert len(handler.buffer) == 3 and handler.client is None and handler.connection_lost is True
    assert handler.current_retry_interval == 17


@pytest.mark.parametrize("retry_after", ["9" * 400, "3600", "Wed, 21 Oct 2015 07:28:00 GMT"])
def test_retry_after_is_capped_at_the_maximum_interval(retry_after):
    handler = _writer("questdb", [httpx.Response(503, headers={"Retry-After": retry_after})], _metrics(1))
    asyncio.run(handler._write_data_unlocked(force=True))
    expected = 0 if retry_after.endswith("GMT") else handler.max_retry_interval
    assert handler.current_retry_interval == expected


@pytest.mark.parametrize("version", ["questdb", 1, 2])
def test_a_success_status_other_than_the_documented_one_is_no_confirmed_write(version):
    handler = _writer(version, [_Response(202)], _metrics(2))
    asyncio.run(handler._write_data_unlocked(force=True))
    # QuestDB confirms a write with 200 or 204, InfluxDB with 204 only; anything else keeps the data.
    assert len(handler.buffer) == 2


@pytest.mark.parametrize("version", [1, 2])
def test_influx_413_is_halved_by_the_package_and_the_next_batch_stays_small(version):
    handler = _writer(version, [_Response(413), _Response(204), _Response(204)], _metrics(8))
    handler.current_measurements_per_write = 8
    asyncio.run(handler._write_data_unlocked(force=True))
    assert handler.buffer == [] and len(handler.client.payloads) == 3
    assert handler.current_measurements_per_write == 4


@pytest.mark.parametrize("version", [1, 2])
def test_influx_413_on_a_single_unsplittable_line_drops_it_whole_and_says_so(version, caplog):
    handler = _writer(version, [_Response(413, "too big")], _metrics(1))
    asyncio.run(handler._write_data_unlocked(force=True))
    assert handler.buffer == [] and "cannot be split" in caplog.text


@pytest.mark.parametrize(("version", "code"), [(1, 401), (2, 403), (1, 404), (2, 404), ("questdb", 401), ("questdb", 403)])
def test_rejected_credentials_or_target_while_running_end_in_a_config_error(version, code):
    handler = _writer(version, [_Response(code, "nope")], _metrics(2))
    handler.name = "InfluxDB"
    with pytest.raises(DatabaseConfigError):
        asyncio.run(handler._write_data_unlocked(force=True))
    assert len(handler.buffer) == 2


@pytest.mark.parametrize("answer", [
    httpx.ConnectError("down"), httpx.ReadTimeout("slow"), httpx.Response(500), httpx.Response(503),
    httpx.Response(429), httpx.Response(408), httpx.Response(302), httpx.Response(418),
])
@pytest.mark.parametrize("version", ["questdb", 1, 2])
def test_an_unreachable_or_misbehaving_server_is_never_a_config_error(version, answer):
    handler = _writer(version, [answer], _metrics(2))
    asyncio.run(handler._write_data_unlocked(force=True))
    assert len(handler.buffer) == 2


def test_retry_after_a_network_error_sends_the_identical_payload(monkeypatch):
    events = _real_call_list_buffer(3)
    for version in ("questdb", 1, 2):
        handler = _writer(version, [httpx.ConnectError("down")], events)
        monkeypatch.setattr(handler, "_note_connection_lost", lambda exc, h=handler: setattr(h, "connection_lost", True))
        asyncio.run(handler._write_data_unlocked(force=True))
        assert len(handler.buffer) == 21 and handler.client is None
        handler.client = _client(version, [_Response(204)])
        asyncio.run(handler._write_data_unlocked(force=True))
        assert handler.buffer == []
        assert handler.client.payloads == ["\n".join(handler.bundle_measurements(events)[0])]


def test_unsupported_call_log_header_is_an_error_not_an_empty_log():
    with pytest.raises(ValueError, match="unsupported call log header"):
        CallLog('sep=;\n"Kind";"Date"\n1;"01.01.26 10:00"')


@pytest.mark.parametrize("data", ["", "sep=;", "sep=;\n", "sep=;\n" + CALL_LOG_HEADER, "sep=;\n\n  \n"])
def test_empty_call_log_stays_valid(data):
    assert CallLog(data).entries == []
