#!/usr/bin/env python3
#
# tests/test_wan_cause_handler.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
import configparser
import logging
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.fritzbox.config import FritzBoxConfig
from app.fritzbox.handler import FritzBoxHandler
from app.fritzbox.tr064.discovery import service_key
from app.fritzbox.wan_watch import WanWatchUnavailable
from app.influxdb.handler import (
    InfluxHandler,
    QuestDBMetricClass,
    questdb_expected_columns,
    questdb_metric_class,
)
from app.main import create_wan_watch_task

NIGHT = datetime(2026, 11, 16, 2, 0, 0, tzinfo=UTC)
NEAR_WINDOW = datetime(2026, 11, 16, 3, 59, 30, tzinfo=UTC)
EVENT_LOGGER = "fritzFlux"


# The handlers below read FRITZBOX_* from the environment; a host value would change their defaults.
pytestmark = pytest.mark.usefixtures("no_fritzbox_env")


class FakeBox:
    """Scripted FRITZ!Box: uptimes follow the clock, so each scenario only sets start times."""

    def __init__(self, origin=NIGHT, dsl_diagnose=True):
        self.origin = origin
        self.t = 0
        self.status = "Connected"
        self.link_start = -72000
        self.router_start = -500000
        self.sync = 1
        self.error = "ERROR_NONE"
        self.prevention_enabled = "1"
        self.prevention_hour = 4
        self.fail = False
        self.calls = []
        actions = {"X_AVM-DE_GetDSLDiagnoseInfo"} if dsl_diagnose else set()
        self.services = {
            "WANPPPConnection1": SimpleNamespace(actions={"GetInfo", "X_AVM_DE_GetAutoDisconnectTimeSpan"}),
            "WANDSLInterfaceConfig1": SimpleNamespace(actions=actions),
        }

    def has_action(self, service, action):
        entry = self.services.get(service_key(service))
        return entry is not None and action in entry.actions

    def call_action(self, service, action):
        self.calls.append((service, action))
        if self.fail:
            raise ConnectionError("box unreachable")
        if (service, action) in {("WANPPPConnection:1", "GetInfo"), ("WANIPConn", "GetStatusInfo")}:
            up = self.status == "Connected"
            return {
                "NewConnectionStatus": self.status,
                "NewUptime": self.t - self.link_start if up else 0,
                "NewLastConnectionError": self.error,
            }
        if (service, action) == ("DeviceInfo", "GetInfo"):
            return {"NewUpTime": self.t - self.router_start}
        if (service, action) == ("Time:1", "GetInfo"):
            return {"NewCurrentLocalTime": (self.origin + timedelta(seconds=self.t)).strftime("%Y-%m-%dT%H:%M:%S")}
        if action == "X_AVM-DE_GetDSLDiagnoseInfo":
            return {"NewX_AVM-DE_DSLSync": self.sync}
        if action == "X_AVM_DE_GetAutoDisconnectTimeSpan":
            return {
                "NewX_AVM-DE_DisconnectPreventionEnable": self.prevention_enabled,
                "NewX_AVM-DE_DisconnectPreventionHour": self.prevention_hour,
            }
        raise AssertionError(f"unexpected call {service}/{action}")


def make_handler(box, link_type="DSL"):
    handler = FritzBoxHandler(FritzBoxConfig(configparser.ConfigParser()))
    handler.session = box
    handler.config.link_type = link_type
    handler.init_successful = True
    return handler


class Scenario:
    def __init__(self, box, link_type="DSL"):
        self.box = box
        self.handler = make_handler(box, link_type)
        self.watcher = self.handler._build_wan_watcher(5.0)
        self.queue = asyncio.Queue()
        self.events = []

    def poll(self, t, **state):
        self.box.t = t
        for key, value in state.items():
            setattr(self.box, key, value)
        events = self.watcher.poll_once(
            lambda: self.box.origin + timedelta(seconds=t), lambda: float(t)
        )
        asyncio.run(self._publish(events))
        self.events += events
        return events

    async def _publish(self, events):
        for event in events:
            await self.handler._publish_wan_event(event, self.queue)

    def measurements(self):
        items = []
        while not self.queue.empty():
            items.append(self.queue.get_nowait())
        return items

    def closed(self):
        return [e for e in self.events if e.kind == "closed"]


def final(scenario):
    closed = scenario.closed()
    assert len(closed) == 1
    return closed[0].result


def test_router_restart_after_unreachable_box_is_confirmed():
    scenario = Scenario(FakeBox())
    scenario.poll(0)
    scenario.poll(5)
    scenario.box.fail = True
    with pytest.raises(WanWatchUnavailable):
        scenario.poll(10)
    scenario.box.fail = False
    scenario.poll(60, link_start=40, router_start=30)
    result = final(scenario)
    assert (result.cause.value, result.confidence.value) == ("router_restart", "confirmed")


def test_tr064_forced_error_detects_short_disconnect_without_uptime_reset(caplog):
    scenario = Scenario(FakeBox())
    scenario.poll(0)
    with caplog.at_level(logging.INFO, logger=EVENT_LOGGER):
        scenario.poll(5, error="ERROR_FORCED_DISCONNECT")
        scenario.poll(10)
    result = final(scenario)
    assert (result.cause.value, result.confidence.value) == ("planned_disconnect", "probable")
    assert {e.value for e in result.evidence} == {"router_reports_forced_disconnect"}
    assert "cause=planned_disconnect" in caplog.text
    assert [m.value for m in scenario.measurements()] == ["planned_disconnect", "probable", 5]


def test_tr064_forced_error_checks_router_restart_before_classifying():
    scenario = Scenario(FakeBox())
    scenario.poll(0)
    scenario.poll(5, error="ERROR_FORCED_DISCONNECT", router_start=2)
    result = final(scenario)
    assert (result.cause.value, result.confidence.value) == ("router_restart", "confirmed")


def test_router_restart_seen_during_the_outage_is_refined_before_the_close():
    scenario = Scenario(FakeBox())
    scenario.poll(0)
    scenario.poll(5, status="Connecting", router_start=2)
    assert [e.kind for e in scenario.events] == ["opened", "refined"]
    assert scenario.events[1].result.cause.value == "router_restart"
    scenario.poll(30, status="Connected", link_start=25, router_start=2)
    assert final(scenario).confidence.value == "confirmed"


def test_ppp_redial_inside_lifetime_window_is_probably_planned():
    box = FakeBox()
    box.prevention_enabled = "0"
    box.link_start = -86395
    scenario = Scenario(box)
    scenario.poll(0)
    scenario.poll(5)
    scenario.poll(10, status="Disconnected")
    scenario.poll(20, status="Connected", link_start=14)
    result = final(scenario)
    assert (result.cause.value, result.confidence.value) == ("planned_disconnect", "probable")
    assert "lifetime_in_window" in {e.value for e in result.evidence}


def test_ppp_redial_inside_hour_window_is_probably_planned():
    scenario = Scenario(FakeBox(origin=NEAR_WINDOW))
    scenario.poll(0)
    scenario.poll(5)
    scenario.poll(10, status="Disconnected")
    scenario.poll(20, status="Connected", link_start=18)
    result = final(scenario)
    assert (result.cause.value, result.confidence.value) == ("planned_disconnect", "probable")
    assert "hour_in_window" in {e.value for e in result.evidence}


def test_ppp_redial_outside_the_window_is_a_plain_ppp_interruption():
    scenario = Scenario(FakeBox(origin=NIGHT))
    scenario.poll(0)
    scenario.poll(5)
    scenario.poll(10, status="Disconnected")
    scenario.poll(20, status="Connected", link_start=18)
    result = final(scenario)
    assert (result.cause.value, result.confidence.value) == ("ppp_interruption", "probable")


def test_dsl_resync_is_confirmed_and_sticky():
    scenario = Scenario(FakeBox())
    scenario.poll(0)
    scenario.poll(5, status="Connecting", sync=0)
    assert scenario.events[-1].result.cause.value == "dsl_sync_loss"
    scenario.poll(10, status="Connecting", sync=1)
    scenario.poll(30, status="Connected", link_start=25, sync=1)
    result = final(scenario)
    assert (result.cause.value, result.confidence.value) == ("dsl_sync_loss", "confirmed")


@pytest.mark.parametrize(
    ("new_link_start", "gap", "code"),
    [(7, 10, "link_uptime_reset"), (-72000 + 30, 65, "link_start_shift")],
)
def test_interruption_fully_between_two_polls_is_detected_via_start_shift(new_link_start, gap, code):
    # A growing counter needs a gap longer than the shift, hence the slow poll in the second case.
    scenario = Scenario(FakeBox())
    scenario.poll(0)
    scenario.poll(5)
    scenario.poll(gap, link_start=new_link_start)
    closed = scenario.closed()
    assert len(closed) == 1
    assert closed[0].result.cause.value == "unknown"
    assert code in {e.value for e in closed[0].result.evidence}
    scenario.poll(gap + 5, link_start=new_link_start)
    assert len(scenario.closed()) == 1


def test_ambiguous_dsl_state_stays_unknown():
    scenario = Scenario(FakeBox(dsl_diagnose=False))
    scenario.poll(0)
    scenario.poll(5, status="Disconnected")
    scenario.poll(20, status="Connected", link_start=18)
    result = final(scenario)
    assert (result.cause.value, result.confidence.value) == ("unknown", "unknown")


def test_non_dsl_link_never_claims_a_planned_disconnect():
    box = FakeBox()
    box.link_start = -86395
    scenario = Scenario(box, link_type="Cable")
    scenario.poll(0)
    scenario.poll(5)
    scenario.poll(10, status="Disconnected")
    scenario.poll(20, status="Connected", link_start=18)
    result = final(scenario)
    assert (result.cause.value, result.confidence.value) == ("unknown", "unknown")


def test_24_hour_uptime_alone_is_no_event_and_no_measurement(caplog):
    box = FakeBox()
    box.link_start = -86400
    scenario = Scenario(box)
    with caplog.at_level(logging.INFO, logger=EVENT_LOGGER):
        for t in range(0, 70, 5):
            scenario.poll(t)
    assert scenario.events == []
    assert scenario.measurements() == []
    assert "interruption" not in caplog.text


def test_24_hour_uptime_with_unknown_dsl_state_is_unknown():
    box = FakeBox(dsl_diagnose=False)
    box.prevention_enabled = "0"
    box.link_start = -86395
    scenario = Scenario(box)
    scenario.poll(0)
    scenario.poll(5, status="Disconnected")
    scenario.poll(20, status="Connected", link_start=18)
    assert final(scenario).cause.value == "unknown"


def test_lifecycle_logs_use_fixed_codes_only(caplog):
    box = FakeBox()
    box.error = "ERROR_FORCED_DISCONNECT"
    scenario = Scenario(box)
    with caplog.at_level(logging.INFO, logger=EVENT_LOGGER):
        scenario.poll(0)
        scenario.poll(5, status="Disconnected")
        scenario.poll(20, status="Connected", link_start=18)
    lines = [r.getMessage() for r in caplog.records if "interruption" in r.getMessage()]
    assert lines == [
        "FRITZ!Box WAN interruption detected, cause not yet known",
        "FRITZ!Box WAN interruption cause: ppp_interruption (probable)",
        "FRITZ!Box WAN interruption ended after 13 s: cause=ppp_interruption, "
        "confidence=probable, evidence=dsl_sync_ok,ppp_down_seen",
    ]
    for forbidden in ("NewUptime", "NewConnectionStatus", "192.168", "box unreachable", "Disconnected"):
        assert forbidden not in caplog.text


def test_closed_interruption_emits_three_measurements_in_one_line():
    scenario = Scenario(FakeBox())
    scenario.poll(0)
    scenario.poll(5, status="Disconnected")
    scenario.poll(20, status="Connected", link_start=18)
    items = scenario.measurements()
    assert [m.name for m in items] == [
        "wan_interruption_cause", "wan_interruption_confidence", "wan_interruption_duration",
    ]
    assert [m.value for m in items] == ["ppp_interruption", "probable", 13]
    assert [type(m.value) for m in items] == [str, str, int]
    assert {m.box_tag for m in items} == {"fritz.box"}
    assert {m.timestamp for m in items} == {NIGHT + timedelta(seconds=20)}
    handler = InfluxHandler.__new__(InfluxHandler)
    handler.version = "questdb"
    handler.config = SimpleNamespace(measurement_name="fritzbox")
    lines, valid = handler.bundle_measurements(items)
    assert len(lines) == 1
    assert len(valid) == 3
    assert 'wan_interruption_cause="ppp_interruption"' in lines[0]
    assert "wan_interruption_duration=13i" in lines[0]


def test_unfinished_interruption_is_logged_but_never_stored():
    scenario = Scenario(FakeBox())
    scenario.poll(0)
    scenario.poll(5, status="Disconnected")
    assert [e.kind for e in scenario.events][:1] == ["opened"]
    assert scenario.measurements() == []


def test_schema_v1_has_the_three_columns_and_no_rollup_class():
    columns = questdb_expected_columns()
    assert columns["wan_interruption_cause"] == "VARCHAR"
    assert columns["wan_interruption_confidence"] == "VARCHAR"
    assert columns["wan_interruption_duration"] == "LONG"
    for name in ("wan_interruption_cause", "wan_interruption_confidence", "wan_interruption_duration"):
        assert questdb_metric_class(name, columns[name]) is QuestDBMetricClass.UNKNOWN


# Configuration


def _ini(**values):
    data = configparser.ConfigParser()
    data.add_section("fritzbox")
    for key, value in values.items():
        data.set("fritzbox", key, str(value))
    return data


def test_wan_watch_interval_default_and_bounds():
    assert FritzBoxConfig(_ini()).wan_watch_interval == 5
    assert FritzBoxConfig(_ini(wan_watch_interval=0)).wan_watch_interval == 0
    assert FritzBoxConfig(_ini(wan_watch_interval=2)).wan_watch_interval == 2
    assert FritzBoxConfig(_ini(wan_watch_interval=60)).wan_watch_interval == 60
    for bad in (1, 61, -3, "abc"):
        assert FritzBoxConfig(_ini(wan_watch_interval=bad)).parser_error is True


def test_wan_watch_interval_from_environment(monkeypatch):
    monkeypatch.setenv("FRITZBOX_WAN_WATCH_INTERVAL", "20")
    assert FritzBoxConfig(_ini()).wan_watch_interval == 20
    monkeypatch.setenv("FRITZBOX_WAN_WATCH_INTERVAL", "1")
    assert FritzBoxConfig(_ini()).parser_error is True


def test_main_adds_the_task_only_when_enabled():

    async def scenario():
        queue = asyncio.Queue()
        off = FritzBoxHandler(FritzBoxConfig(_ini(wan_watch_interval=0)))
        on = FritzBoxHandler(FritzBoxConfig(_ini(wan_watch_interval=5)))
        assert create_wan_watch_task(off, queue) is None
        task = create_wan_watch_task(on, queue)
        assert task is not None
        assert task.get_name() == "FritzBox WAN watch"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


# Loop behavior


class _Stop(Exception):
    pass


def run_loop(handler, hooks, watcher=None):
    """Run wan_watch_loop with hooks[i] in place of the i-th sleep; stop after the last hook, return the delays."""
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)
        index = len(delays) - 1
        if index >= len(hooks):
            raise _Stop
        hooks[index]()

    if watcher is not None:
        handler._build_wan_watcher = lambda interval: watcher

    async def scenario():
        original = asyncio.sleep
        asyncio.sleep = fake_sleep
        try:
            await handler.wan_watch_loop(asyncio.Queue())
        except _Stop:
            pass
        finally:
            asyncio.sleep = original

    asyncio.run(scenario())
    return delays


def test_loop_skips_until_init_and_never_logs_in():
    box = FakeBox()
    handler = make_handler(box)
    handler.init_successful = False
    handler.connect = lambda: pytest.fail("login helper called")
    handler._reconnect = lambda: pytest.fail("login helper called")
    delays = run_loop(handler, [lambda: None, lambda: None])
    assert delays == [5.0, 5.0, 5.0]
    assert box.calls == []


def test_loop_waits_for_the_link_type_and_recreates_the_tracker_on_change():
    box = FakeBox()
    handler = make_handler(box, link_type=None)
    handler.config.link_type = None
    seen = []
    watcher = handler._build_wan_watcher(5.0)
    handler._build_wan_watcher = lambda interval: watcher

    def set_dsl():
        seen.append(len(box.calls))
        handler.config.link_type = "DSL"

    def change_to_cable():
        seen.append(len(box.calls))
        box.services = {}
        handler.config.link_type = "Cable"

    run_loop(handler, [lambda: seen.append(len(box.calls)), set_dsl, lambda: None, change_to_cable, lambda: None])
    assert seen[0] == 0
    assert seen[1] == 0
    assert seen[2] > 0
    assert watcher._link_type == "Cable"


def test_session_set_to_none_between_cycles_does_not_end_the_loop():
    box = FakeBox()
    handler = make_handler(box)

    def drop_session():
        handler.session = None

    def restore():
        handler.session = box

    delays = run_loop(handler, [drop_session, lambda: None, restore, lambda: None])
    assert delays[:4] == [5.0, 5.0, 5.0, 5.0]


def test_back_off_on_unavailable_and_reset_after_success():
    class Watcher:
        def __init__(self):
            self.results = [WanWatchUnavailable(), WanWatchUnavailable(), []]

        def poll_once(self, now_fn, monotonic_fn):
            item = self.results.pop(0) if self.results else []
            if isinstance(item, Exception):
                raise item
            return item

    handler = make_handler(FakeBox())
    delays = run_loop(handler, [lambda: None] * 4, watcher=Watcher())
    assert delays[:4] == [10.0, 20.0, 5.0, 5.0]


def test_back_off_is_capped():
    class Watcher:
        def poll_once(self, now_fn, monotonic_fn):
            raise WanWatchUnavailable

    handler = make_handler(FakeBox())
    delays = run_loop(handler, [lambda: None] * 10, watcher=Watcher())
    assert max(delays) == 60.0


def test_unexpected_exception_does_not_end_the_task(caplog):
    class Watcher:
        def poll_once(self, now_fn, monotonic_fn):
            raise RuntimeError("secret detail 192.168.1.1")

    handler = make_handler(FakeBox())
    with caplog.at_level(logging.INFO, logger=EVENT_LOGGER):
        delays = run_loop(handler, [lambda: None] * 3, watcher=Watcher())
    assert len(delays) == 4
    assert "RuntimeError" in caplog.text
    assert "secret detail" not in caplog.text
    assert "192.168.1.1" not in caplog.text


def test_cancellation_during_an_in_flight_poll_drains_it():
    started = threading.Event()
    release = threading.Event()
    finished = []

    class Watcher:
        def poll_once(self, now_fn, monotonic_fn):
            started.set()
            release.wait(5)
            finished.append(True)
            return []

    handler = make_handler(FakeBox())
    handler._build_wan_watcher = lambda interval: Watcher()

    async def scenario():
        task = asyncio.create_task(handler.wan_watch_loop(asyncio.Queue()))
        # Bounded, so a poll that never starts fails the test instead of hanging the suite.
        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set(), "the poll never started"
        task.cancel()
        asyncio.get_running_loop().call_later(0.1, release.set)
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert finished == [True]
    assert handler.shutting_down is True


def test_unavailable_watcher_is_logged_once_per_interval(caplog):
    class Watcher:
        def poll_once(self, now_fn, monotonic_fn):
            raise WanWatchUnavailable

    handler = make_handler(FakeBox())
    with caplog.at_level(logging.INFO, logger=EVENT_LOGGER):
        run_loop(handler, [lambda: None] * 5, watcher=Watcher())
    assert caplog.text.count("WAN watch unavailable") == 1


def test_unknown_duration_is_logged_and_never_stored_as_measured(caplog):
    from datetime import UTC, datetime

    from app.fritzbox.wan_cause import CauseClass, Confidence, Detection, WanCauseResult
    from app.fritzbox.wan_tracker import TrackerEvent

    at = datetime(2026, 11, 16, tzinfo=UTC)
    event = TrackerEvent(
        kind="closed", result=WanCauseResult(cause=CauseClass.UNKNOWN, confidence=Confidence.UNKNOWN),
        started_at=at, ended_at=at, duration_s=None, detected_by=Detection.UPTIME_RESET, observed_at=at,
    )
    handler = make_handler(None)
    queue = asyncio.Queue()
    with caplog.at_level("INFO"):
        asyncio.run(handler._publish_wan_event(event, queue))
    names = [queue.get_nowait().name for _ in range(queue.qsize())]
    assert "wan_interruption_duration" not in names
    assert "wan_interruption_cause" in names
    assert "ended after an unknown time" in caplog.text
