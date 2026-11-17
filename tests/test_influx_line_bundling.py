#!/usr/bin/env python3
#
# tests/test_influx_line_bundling.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.influxdb.handler import InfluxHandler
from app.utils.measurement import FritzMeasurement

TIMESTAMP = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
TS = int(TIMESTAMP.timestamp())


def make_handler(version="questdb"):
    handler = InfluxHandler.__new__(InfluxHandler)
    handler.version = version
    handler.config = SimpleNamespace(measurement_name="fritzbox")
    return handler


def measurement(name, value, tags=None, timestamp=TIMESTAMP):
    return FritzMeasurement(name, value, box_tag="fritz.box", additional_tags=tags, timestamp=timestamp)


def test_fields_with_same_series_and_timestamp_share_one_line():
    lines, valid = make_handler().bundle_measurements([
        measurement("sendrate", 10),
        measurement("receiverate", 20),
        measurement("connection_status", "Connected"),
    ])

    assert lines == [f'fritzbox_metrics,box=fritz.box sendrate=10i,receiverate=20i,connection_status="Connected" {TS}']
    assert len(valid) == 3


def test_different_tags_or_timestamps_stay_on_separate_lines():
    lines, _ = make_handler().bundle_measurements([
        measurement("active_hosts_name", "laptop", tags={"uid": "a"}),
        measurement("active_hosts_name", "phone", tags={"uid": "b"}),
        measurement("active_hosts_mac", "00:11", tags={"uid": "a"}),
        measurement("cpu_temp", 50, timestamp=TIMESTAMP.replace(second=1)),
    ])

    assert lines == [
        f'fritzbox_metrics,box=fritz.box,uid=a active_hosts_name="laptop",active_hosts_mac="00:11" {TS}',
        f'fritzbox_metrics,box=fritz.box,uid=b active_hosts_name="phone" {TS}',
        f"fritzbox_metrics,box=fritz.box cpu_temp=50i {TS + 1}",
    ]


def test_duplicate_field_starts_a_new_line_instead_of_overwriting():
    lines, valid = make_handler().bundle_measurements([
        measurement("log_entry", "first", tags={"log_type": "System"}),
        measurement("log_entry", "second", tags={"log_type": "System"}),
    ])

    assert lines == [
        f'fritzbox_metrics,box=fritz.box,log_type=System log_entry="first" {TS}',
        f'fritzbox_metrics,box=fritz.box,log_type=System log_entry="second" {TS}',
    ]
    assert len(valid) == 2


def test_unwritable_values_are_dropped_without_affecting_the_line():
    lines, valid = make_handler().bundle_measurements([
        measurement("totalbytessent", 2 ** 64),
        measurement("cpu_temp", float("nan")),
        measurement("cpu_utilization", 7),
    ])

    assert lines == [f"fritzbox_metrics,box=fritz.box cpu_utilization=7i {TS}"]
    assert [m.name for m in valid] == ["cpu_utilization"]


def test_questdb_replaces_dots_in_field_names_but_influxdb_keeps_them():
    wlan = measurement("wlan1_802.11_standard", "ax")

    questdb_lines, _ = make_handler("questdb").bundle_measurements([wlan])
    influx_lines, _ = make_handler(2).bundle_measurements([wlan])

    assert questdb_lines == [f'fritzbox_metrics,box=fritz.box wlan1_802_11_standard="ax" {TS}']
    assert influx_lines == [f'fritzbox,box=fritz.box wlan1_802.11_standard="ax" {TS}000000000']
    assert wlan.name == "wlan1_802.11_standard"


def test_single_measurement_matches_to_line_protocol():
    point = measurement("linkuptime", 3600)

    lines, _ = make_handler(1).bundle_measurements([point])

    # InfluxDB is written with precision=ns
    assert lines == [point.to_line_protocol("fritzbox") + "000000000"]


def test_empty_and_none_tags_are_omitted():
    lines, _ = make_handler().bundle_measurements([
        measurement("rate", 1, tags={"name": "", "gone": None, "uid": "a"}),
    ])
    assert lines == [f"fritzbox_metrics,box=fritz.box,uid=a rate=1i {TS}"]


@pytest.mark.parametrize(
    ("value", "escaped"),
    [
        ("dir\\", "dir\\\\"),
        ("dir\\\\", "dir\\\\\\\\"),
        ("a\\b", "a\\\\b"),
        ("a,b", "a\\,b"),
        ("a=b", "a\\=b"),
        ("a b", "a\\ b"),
        ("a\\ b", "a\\\\\\ b"),
    ],
)
def test_tag_values_are_escaped_not_stripped(value, escaped):
    lines, _ = make_handler().bundle_measurements([measurement("rate", 1, tags={"path": value})])
    assert lines == [f"fritzbox_metrics,box=fritz.box,path={escaped} rate=1i {TS}"]


def test_tag_values_differing_only_in_trailing_backslashes_stay_distinct():
    series = {
        measurement("rate", 1, tags={"d": value}).line_protocol_series("fritzbox")
        for value in ("device", "device\\", "device\\\\")
    }
    assert len(series) == 3


@pytest.mark.parametrize("version", [1, 2])
def test_influx_unrepresentable_trailing_backslash_does_not_poison_the_batch(version, caplog):
    lines, valid = make_handler(version).bundle_measurements([
        measurement("bad", 1, tags={"path": "private\\"}),
        measurement("other", 2),
    ])
    assert len(lines) == 1
    assert [m.name for m in valid] == ["other"]
    assert "ending with a backslash" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.parametrize("tags", [{"d": "a\nb"}, {"d": "a\rb"}, {"k\\": "v"}, {"": "v"}])
def test_unwritable_tags_drop_the_measurement(tags):
    lines, valid = make_handler().bundle_measurements([
        measurement("rate", 1, tags=tags),
        measurement("other", 2),
    ])
    assert lines == [f"fritzbox_metrics,box=fritz.box other=2i {TS}"]
    assert [m.name for m in valid] == ["other"]


def test_tag_helpers_omit_missing_names():
    from app.fritzbox.service_definitions.homeauto import _device_tags
    from app.fritzbox.service_definitions.network_hosts import host_uid_name_tag

    assert _device_tags({"name": None}) == {}
    assert _device_tags({"name": "Plug"}) == {"name": "Plug"}
    assert _device_tags({"@identifier": "08761 0000434", "name": "Plug"}) == {
        "id": "08761 0000434", "name": "Plug"}
    assert host_uid_name_tag({"UID": "x", "name": ""}) == {"uid": "x"}


def test_log_records_from_worker_threads_are_queued_on_the_loop_thread():
    import asyncio
    import logging
    import threading

    from app.utils.log import DroppingQueueHandler

    async def scenario():
        queue = asyncio.Queue()
        handler = DroppingQueueHandler(queue, asyncio.get_running_loop())
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
        thread = threading.Thread(target=handler.enqueue, args=(record,))
        thread.start()
        await asyncio.to_thread(thread.join)
        await asyncio.sleep(0)
        return queue.qsize()

    assert asyncio.run(scenario()) == 1


def test_log_record_after_the_event_loop_closed_is_handled_without_error():
    import asyncio
    import logging

    from app.utils.log import DroppingQueueHandler

    loop = asyncio.new_event_loop()
    queue = asyncio.Queue()
    handler = DroppingQueueHandler(queue, loop)
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
    loop.close()
    handler.enqueue(record)  # closed loop: queued directly
    assert queue.qsize() == 1

    class RacingLoop:
        """Reports open, then refuses the hand-over like a loop that closes in between."""

        def is_closed(self):
            return False

        def call_soon_threadsafe(self, *_):
            raise RuntimeError("Event loop is closed")

    DroppingQueueHandler(queue, RacingLoop()).enqueue(record)  # must not raise
    assert queue.qsize() == 1


def _log_measurements(handler_version, records):
    from app.influxdb.handler import InfluxLogAndConfigWriter
    writer = InfluxLogAndConfigWriter.__new__(InfluxLogAndConfigWriter)
    writer.config = SimpleNamespace(box_tag="fritz.box")
    measurements = [writer.format_log_record(record) for record in records]
    return make_handler(handler_version).bundle_measurements(measurements)


def _record(message, created):
    import logging
    record = logging.LogRecord("fritzFlux", logging.INFO, __file__, 1, message, None, None)
    record.created = created
    record.relativeCreated = created * 1000
    return record


@pytest.mark.parametrize("version", [1, 2])
def test_same_second_daemon_log_records_stay_distinct_points_in_influxdb(version):
    base = TIMESTAMP.timestamp()
    lines, valid = _log_measurements(version, [
        _record("same text", base + 0.1), _record("same text", base + 0.2), _record("other", base + 0.3)])
    assert len(valid) == 3
    assert len(lines) == 3
    assert len({line.rsplit(" ", 1)[1] for line in lines}) == 3
    assert all("uid=" not in line.split(" ", 1)[0] for line in lines)


def test_repeated_formatting_of_a_record_gives_the_same_event_id():
    from app.influxdb.handler import InfluxLogAndConfigWriter
    writer = InfluxLogAndConfigWriter.__new__(InfluxLogAndConfigWriter)
    writer.config = SimpleNamespace(box_tag="fritz.box")
    record = _record("x", TIMESTAMP.timestamp() + 0.5)
    first, second = writer.format_log_record(record), writer.format_log_record(record)
    assert first.additional_tags["uid"] == second.additional_tags["uid"]
    assert first.additional_tags["uid"] != writer.format_log_record(_record("x", TIMESTAMP.timestamp() + 0.6)).additional_tags["uid"]


def test_same_second_daemon_log_records_are_separate_rows_in_the_questdb_main_table():
    base = TIMESTAMP.timestamp()
    lines, valid = _log_measurements("questdb", [_record("same text", base + 0.1), _record("same text", base + 0.2)])
    assert len(valid) == 2
    assert len(lines) == 2
    assert all(line.startswith("fritzbox_metrics,box=fritz.box,log_type=fritzFlux ") for line in lines)


def test_final_flush_failure_is_logged_and_reports_the_loss(caplog):
    import asyncio
    import logging
    from unittest.mock import AsyncMock

    handler = InfluxHandler.__new__(InfluxHandler)
    handler.name = "InfluxDB"
    handler.buffer = [object()]
    handler.write_data = AsyncMock(side_effect=RuntimeError("down"))

    with caplog.at_level(logging.ERROR, logger="fritzFlux"):
        asyncio.run(handler._flush_buffer_before_close())

    assert "Final flush" in caplog.text
    assert "unwritten" in caplog.text


def test_config_error_in_final_flush_reports_the_loss_and_propagates(caplog):
    import asyncio
    import logging
    from unittest.mock import AsyncMock

    import pytest

    from app.influxdb.handler import DatabaseConfigError

    handler = InfluxHandler.__new__(InfluxHandler)
    handler.name = "InfluxDB"
    handler.buffer = [object()]
    handler.write_data = AsyncMock(side_effect=DatabaseConfigError("rejected"))

    with caplog.at_level(logging.ERROR, logger="fritzFlux"), pytest.raises(DatabaseConfigError):
        asyncio.run(handler._flush_buffer_before_close())

    assert "unwritten" in caplog.text


def test_task_loop_closes_the_client_when_the_final_flush_raises_a_config_error():
    import asyncio
    from unittest.mock import AsyncMock

    import pytest

    from app.influxdb.handler import DatabaseConfigError

    handler = InfluxHandler.__new__(InfluxHandler)
    handler.name = "InfluxDB"
    handler.buffer = [object()]
    handler.max_measurements_per_write = 1
    handler.out_of_retention_period_range = False
    handler.check_buffer = lambda: None
    handler.close = AsyncMock()
    calls = []

    async def write_data(force=False):
        calls.append(force)
        if force:
            raise DatabaseConfigError("denied")

    handler.write_data = write_data

    async def scenario():
        task = asyncio.create_task(handler.task_loop(asyncio.Queue()))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(DatabaseConfigError):
            await task

    asyncio.run(scenario())
    handler.close.assert_awaited_once()


def _loop_handler(write_data):
    from unittest.mock import AsyncMock

    handler = InfluxHandler.__new__(InfluxHandler)
    handler.name = "QuestDB"
    handler.buffer = [object()]
    handler.max_measurements_per_write = 1
    handler.out_of_retention_period_range = False
    handler.check_buffer = lambda: None
    handler.close = AsyncMock()
    handler.write_data = write_data
    return handler


def test_task_loop_closes_the_client_after_a_generic_final_flush_failure():
    import asyncio

    async def write_data(force=False):
        if force:
            raise RuntimeError("down")

    handler = _loop_handler(write_data)

    async def scenario():
        task = asyncio.create_task(handler.task_loop(asyncio.Queue()))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    handler.close.assert_awaited_once()


def test_cancellation_during_the_final_flush_propagates_and_still_closes_the_client():
    import asyncio

    async def write_data(force=False):
        if force:
            raise asyncio.CancelledError

    handler = _loop_handler(write_data)

    async def scenario():
        task = asyncio.create_task(handler.task_loop(asyncio.Queue()))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    handler.close.assert_awaited_once()


def test_setup_logging_keeps_http_client_loggers_at_warning():
    import logging

    from app.utils.log import setup_logging

    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    setup_logging("DEBUG")
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_retry_interval_doubles_up_to_the_maximum():
    handler = make_handler()
    handler.current_retry_interval = InfluxHandler.retry_interval
    seen = []
    for _ in range(8):
        handler._double_retry_interval()
        seen.append(handler.current_retry_interval)
    assert seen[:3] == [10, 20, 40]
    assert seen[-1] == InfluxHandler.max_retry_interval


def test_rollup_state_is_per_instance_and_declared_in_init(monkeypatch):
    import configparser

    monkeypatch.setenv("DB_TYPE", "questdb")
    monkeypatch.setenv("QUESTDB_HOSTNAME", "questdb.invalid")
    first, second = InfluxHandler(configparser.ConfigParser()), InfluxHandler(configparser.ConfigParser())
    first._rollup_failures["view"] = ("reason", True)
    first._rollup_ready_views.add("view")
    assert second._rollup_failures == {} and second._rollup_ready_views == set()
    assert first._rollup_failure_permanent is False and first._pending_config_error is None


def test_unwritable_values_are_dropped_with_one_warning_per_metric(caplog, monkeypatch):
    import logging

    from app.utils import measurement as measurement_module

    # A private set keeps the process-wide warn-once state of other tests out, also when an assert fails.
    monkeypatch.setattr(measurement_module, "_unwritable_warned", set())
    nan_point = FritzMeasurement("cpu_temp", float("nan"))
    with caplog.at_level(logging.WARNING, logger="fritzFlux"):
        for _ in range(3):
            assert nan_point.line_protocol_field() == ""
        assert FritzMeasurement("big", 2**70).line_protocol_field() == ""
    assert caplog.text.count("'cpu_temp'") == 1
    assert "non-finite float" in caplog.text
    assert "64-bit range" in caplog.text
