#!/usr/bin/env python3
#
# tests/test_wan_watch.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.fritzbox import wan_watch
from app.fritzbox.tr064.discovery import service_key
from app.fritzbox.tr064.exceptions import (
    TR064ActionNotFound,
    TR064Error,
    TR064HttpUnauthorized,
)
from app.fritzbox.wan_watch import (
    WanWatcher,
    WanWatchUnavailable,
    _scpd_has_action,
    parse_box_utc_offset,
    parse_connection_status,
    parse_dsl_sync,
    parse_last_error,
    parse_prevention,
    parse_uptime,
    resolve_action,
)

T0 = datetime(2026, 11, 16, 3, 0, 0, tzinfo=UTC)


def test_parse_connection_status():
    assert parse_connection_status("Connected") is True
    assert parse_connection_status(" connected ") is True
    for text in ("Disconnected", "Connecting", "Unconfigured", "PendingDisconnect"):
        assert parse_connection_status(text) is False
    for other in ("Weird", "", None, ["Connected"], 1):
        assert parse_connection_status(other) is None


def test_parse_uptime():
    assert parse_uptime("123") == 123
    assert parse_uptime(7) == 7
    assert parse_uptime("-5") is None
    assert parse_uptime("x") is None
    assert parse_uptime(None) is None
    assert parse_uptime(True) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1, True), (0, False), ("1", True), ("0", False), ("true", True), ("False", False),
     (True, True), (2, None), ("yes", None), (None, None)],
)
def test_parse_dsl_sync(raw, expected):
    assert parse_dsl_sync(raw) is expected


def test_parse_last_error_pattern():
    assert parse_last_error("ERROR_NONE") == "ERROR_NONE"
    assert parse_last_error(" ERROR_FORCED_DISCONNECT ") == "ERROR_FORCED_DISCONNECT"
    assert parse_last_error("ERROR_ISP_DISCONNECT") == "ERROR_ISP_DISCONNECT"
    assert parse_last_error("error_none") is None
    assert parse_last_error("PPP failed for 203.0.113.9") is None
    assert parse_last_error("ERROR_" + "A" * 41) is None
    assert parse_last_error(None) is None


def test_parse_prevention_hour_and_fallbacks():
    ok = {"NewX_AVM-DE_DisconnectPreventionEnable": "1", "NewX_AVM-DE_DisconnectPreventionHour": "4"}
    window = parse_prevention(ok, 3600)
    assert (window.kind, window.hour, window.utc_offset_s) == ("hour_of_day", 4, 3600)
    assert parse_prevention(ok, None).kind == "lifetime"
    assert parse_prevention(None, 3600).kind == "lifetime"
    off = dict(ok, **{"NewX_AVM-DE_DisconnectPreventionEnable": "0"})
    assert parse_prevention(off, 3600).kind == "lifetime"
    for hour in ("24", "-1", "x", None):
        bad = dict(ok, **{"NewX_AVM-DE_DisconnectPreventionHour": hour})
        assert parse_prevention(bad, 3600).kind == "lifetime"


def test_parse_box_utc_offset():
    before = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)
    after = before + timedelta(seconds=2)
    assert parse_box_utc_offset("2026-11-16T13:00:01", before, after) == 3600
    assert parse_box_utc_offset("2026-11-16T13:00:20+01:00", before, after) == 3600
    assert parse_box_utc_offset("2026-11-16T12:00:01", before, after) == 0
    assert parse_box_utc_offset("2026-11-16T13:07:00", before, after) is None  # not a 15 min multiple
    assert parse_box_utc_offset("2026-11-17T04:00:00", before, after) is None  # more than 14 h
    assert parse_box_utc_offset("garbage", before, after) is None
    assert parse_box_utc_offset(None, before, after) is None


def _has_action(services):
    def has_action(service, action):
        entry = services.get(service_key(service))
        return entry is not None and action in entry.actions

    return has_action


def _session(services=None, **actions):
    return SimpleNamespace(has_action=_has_action(services or {}), call_action=lambda *a, **k: None, actions=actions)


def _services(**by_key):
    return {key: SimpleNamespace(actions={name: object() for name in names}) for key, names in by_key.items()}


def test_normalize_name_keys():
    assert service_key("WANPPPConnection:1") == "WANPPPConnection1"
    assert service_key("WANDSLInterfaceConfig") == "WANDSLInterfaceConfig1"


def _session_that_cannot_load_the_scpd():
    def has_action(service, action):
        raise TR064Error("SCPD could not be loaded")

    return SimpleNamespace(has_action=has_action)


def test_scpd_has_action_uses_normalized_keys():
    session = _session(_services(WANPPPConnection1=["X_AVM_DE_GetAutoDisconnectTimeSpan"]))
    assert _scpd_has_action(session, "WANPPPConnection:1", "X_AVM_DE_GetAutoDisconnectTimeSpan") is True
    assert _scpd_has_action(session, "WANPPPConnection:1", "Other") is False
    assert _scpd_has_action(session, "WANDSLInterfaceConfig", "X") is False  # service not offered
    assert _scpd_has_action(_session_that_cannot_load_the_scpd(), "WANPPPConnection:1", "X") is None


def test_resolve_action_candidates():
    second = _session(_services(WANPPPConnection1=["X_AVM-DE_GetAutoDisconnectTimeSpan"]))
    assert resolve_action(second, "WANPPPConnection:1", wan_watch.PREVENTION_CANDIDATES) == (
        "X_AVM-DE_GetAutoDisconnectTimeSpan"
    )
    none = _session(_services(WANPPPConnection1=["GetInfo"]))
    assert resolve_action(none, "WANPPPConnection:1", wan_watch.PREVENTION_CANDIDATES) is None
    blind = _session_that_cannot_load_the_scpd()
    assert resolve_action(blind, "WANPPPConnection:1", wan_watch.PREVENTION_CANDIDATES) == (
        wan_watch.PREVENTION_CANDIDATES[0]
    )


# poll_once


class FakeClock:
    def __init__(self):
        self.wall = T0
        self.mono = 1000.0
        self.step = 0.0

    def now(self):
        self.wall += timedelta(seconds=self.step)
        self.mono += self.step
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


class FakeTracker:
    def __init__(self, link_type):
        self.link_type = link_type
        self.samples = []
        self.windows = []
        self.unreachable = []
        self.needs_attention = False
        self.router_every = 6
        self.forced_error = None
        self.baseline_resets = 0

    def is_new_forced_error(self, last_error):
        return last_error is not None and last_error == self.forced_error

    def wants_router_uptime(self, partial, index):
        return partial.connected is not True or index % self.router_every == 0

    def observe(self, sample, window):
        self.samples.append(sample)
        self.windows.append(window)
        return ["event"]

    def note_unreachable(self):
        self.unreachable.append(True)

    def reset_link_baseline(self):
        self.baseline_resets += 1


DIAG = "X_AVM-DE_GetDSLDiagnoseInfo"
PREV = "X_AVM_DE_GetAutoDisconnectTimeSpan"


class FakeBox:
    """Scripted TR-064 box; behaviors can be changed between cycles."""

    def __init__(self, with_actions=True):
        self.calls = []
        self.status = {"NewConnectionStatus": "Connected", "NewUptime": "3600", "NewLastConnectionError": "ERROR_NONE"}
        self.dsl = {"NewX_AVM-DE_DSLSync": "1"}
        self.prevention = {
            "NewX_AVM-DE_DisconnectPreventionEnable": "1", "NewX_AVM-DE_DisconnectPreventionHour": "4",
        }
        self.local_time = "2026-11-16T04:00:00"
        self.errors = {}
        self.services = _services(
            WANPPPConnection1=["GetInfo", PREV] if with_actions else ["GetInfo"],
            WANDSLInterfaceConfig1=[DIAG] if with_actions else [],
        )
        self.has_action = _has_action(self.services)

    def call_action(self, service, action, **kwargs):
        self.calls.append((service, action))
        error = self.errors.get((service, action)) or self.errors.get("*")
        if error:
            raise error
        if action in ("GetInfo", "GetStatusInfo") and service in (
            "WANPPPConnection:1", "WANPPPConnection:2", "WANIPConnection:1", "WANIPConn",
        ):
            return dict(self.status)
        if action == DIAG:
            return dict(self.dsl)
        if action == PREV:
            return dict(self.prevention)
        if service == "Time:1":
            return {"NewCurrentLocalTime": self.local_time}
        if service == "DeviceInfo":
            return {"NewUpTime": "90000"}
        raise AssertionError(f"unexpected call {service} {action}")

    def count(self, action):
        return sum(1 for _, name in self.calls if name == action)


def _watcher(box, link_type="DSL", session_ref=None, **kwargs):
    state = {"session": session_ref if session_ref is not None else box, "link": link_type}
    watcher = WanWatcher(
        lambda: state["session"],
        lambda: state["link"],
        lambda exc: isinstance(exc, TR064HttpUnauthorized),
        log_once=kwargs.pop("log_once", lambda key, message: logging.getLogger("wan").info(message)),
        tracker_factory=kwargs.pop("tracker_factory", FakeTracker),
    )
    return watcher, state


def test_dsl_sample_has_midpoint_times_and_dsl_state():
    box, clock = FakeBox(), FakeClock()
    watcher, _ = _watcher(box)
    assert watcher.poll_once(clock.now, clock.monotonic) == ["event"]
    sample = watcher._tracker.samples[0]
    assert sample.connected is True
    assert sample.link_uptime_s == 3600
    assert sample.dsl_sync is True
    assert sample.start_reliable is True
    assert sample.last_error == "ERROR_NONE"
    assert sample.monotonic_s == pytest.approx(clock.mono)
    assert watcher._tracker.windows[0].kind == "hour_of_day"


def test_slow_response_makes_start_unreliable():
    box, clock = FakeBox(), FakeClock()
    clock.step = 1.5  # each now()/monotonic pair advances 1.5 s -> RTT 1.5 s per call
    watcher, _ = _watcher(box)
    original = box.call_action

    def slow(service, action, **kwargs):
        if action == "GetInfo":
            clock.advance(3)
        return original(service, action, **kwargs)

    box.call_action = slow
    watcher.poll_once(clock.now, clock.monotonic)
    assert watcher._tracker.samples[0].start_reliable is False
    assert watcher._tracker.samples[0].connected is True


def test_non_dsl_skips_diagnose_and_window():
    box, clock = FakeBox(), FakeClock()
    watcher, _ = _watcher(box, link_type="Ethernet")
    watcher.poll_once(clock.now, clock.monotonic)
    assert ("WANIPConn", "GetStatusInfo") in box.calls
    assert box.count(DIAG) == 0 and box.count(PREV) == 0
    assert watcher._tracker.windows[0] is None
    assert watcher._tracker.samples[0].dsl_sync is None


def test_diagnose_only_in_discovery_or_interruption():
    box, clock = FakeBox(), FakeClock()
    watcher, _ = _watcher(box)
    for _ in range(6):
        clock.advance(5)
        watcher.poll_once(clock.now, clock.monotonic)
    assert box.count(DIAG) == 1  # first healthy cycle succeeded, then never again while healthy
    box.status["NewConnectionStatus"] = "Disconnected"
    clock.advance(5)
    watcher.poll_once(clock.now, clock.monotonic)
    assert box.count(DIAG) == 2
    assert watcher._tracker.samples[-1].connected is False
    assert watcher._tracker.samples[-1].dsl_sync is True


def test_new_forced_error_poll_queries_dsl_sync_while_connected():
    box, clock = FakeBox(), FakeClock()
    watcher, _ = _watcher(box)
    for _ in range(3):
        clock.advance(5)
        watcher.poll_once(clock.now, clock.monotonic)
    assert box.count(DIAG) == 1
    watcher._tracker.forced_error = "ERROR_FORCED_DISCONNECT"
    box.status["NewLastConnectionError"] = "ERROR_FORCED_DISCONNECT"
    box.dsl = {"NewX_AVM-DE_DSLSync": "0"}
    clock.advance(5)
    watcher.poll_once(clock.now, clock.monotonic)
    assert box.count(DIAG) == 2
    assert watcher._tracker.samples[-1].connected is True
    assert watcher._tracker.samples[-1].dsl_sync is False


def test_router_uptime_every_nth_cycle_and_on_anomaly():
    box, clock = FakeBox(), FakeClock()
    watcher, _ = _watcher(box)
    for _ in range(6):
        clock.advance(5)
        watcher.poll_once(clock.now, clock.monotonic)
    assert box.count("GetInfo") >= 6
    router = [s.router_uptime_s for s in watcher._tracker.samples]
    assert router[:5] == [None] * 5 and router[5] == 90000
    box.status["NewConnectionStatus"] = "Disconnected"
    watcher.poll_once(clock.now, clock.monotonic)
    assert watcher._tracker.samples[-1].router_uptime_s == 90000


def test_discovery_error_disables_diagnose_permanently():
    box, clock = FakeBox(), FakeClock()
    box.errors[("WANDSLInterfaceConfig", DIAG)] = TR064ActionNotFound("WANDSLInterfaceConfig", DIAG)
    watcher, _ = _watcher(box)
    watcher.poll_once(clock.now, clock.monotonic)
    box.status["NewConnectionStatus"] = "Disconnected"
    watcher.poll_once(clock.now, clock.monotonic)
    assert box.count(DIAG) == 1
    assert watcher._tracker.samples[-1].dsl_sync is None


def test_error_after_success_never_disables_signal():
    box, clock = FakeBox(), FakeClock()
    watcher, _ = _watcher(box)
    watcher.poll_once(clock.now, clock.monotonic)
    box.status["NewConnectionStatus"] = "Disconnected"
    box.errors[("WANDSLInterfaceConfig", DIAG)] = TR064ActionNotFound("WANDSLInterfaceConfig", DIAG)
    watcher.poll_once(clock.now, clock.monotonic)
    assert watcher._tracker.samples[-1].dsl_sync is None
    del box.errors[("WANDSLInterfaceConfig", DIAG)]
    watcher.poll_once(clock.now, clock.monotonic)
    assert box.count(DIAG) == 3
    assert watcher._tracker.samples[-1].dsl_sync is True


def test_non_fritz_error_with_820_text_does_not_disable_signal():
    box, clock = FakeBox(), FakeClock()
    watcher, _ = _watcher(box)
    box.errors[("WANDSLInterfaceConfig", DIAG)] = RuntimeError("transport failure 820")

    watcher.poll_once(clock.now, clock.monotonic)
    del box.errors[("WANDSLInterfaceConfig", DIAG)]
    watcher.poll_once(clock.now, clock.monotonic)

    assert box.count(DIAG) == 2
    assert watcher._tracker.samples[-1].dsl_sync is True


def test_auth_failure_gives_none_without_unreachable():
    box, clock = FakeBox(), FakeClock()
    watcher, _ = _watcher(box)
    box.errors[("WANDSLInterfaceConfig", DIAG)] = TR064HttpUnauthorized()
    watcher.poll_once(clock.now, clock.monotonic)
    assert watcher._tracker.samples[0].dsl_sync is None
    assert watcher._tracker.unreachable == []


def test_all_calls_failing_notes_unreachable_and_raises():
    box, clock = FakeBox(), FakeClock()
    box.errors["*"] = ConnectionError("boom")
    watcher, _ = _watcher(box)
    with pytest.raises(WanWatchUnavailable):
        watcher.poll_once(clock.now, clock.monotonic)
    assert len(watcher._tracker.unreachable) == 1
    assert watcher._tracker.samples == []


def test_session_none_link_none_and_swap_skip_silently(caplog):
    box, clock = FakeBox(), FakeClock()
    watcher, state = _watcher(box)
    state["session"] = None
    assert watcher.poll_once(clock.now, clock.monotonic) == []
    state["session"], state["link"] = box, None
    assert watcher.poll_once(clock.now, clock.monotonic) == []
    assert box.calls == []
    state["link"] = "DSL"
    original = box.call_action

    def swapping(service, action, **kwargs):
        state["session"] = object()
        raise RuntimeError("closed")

    box.call_action = swapping
    with caplog.at_level(logging.INFO):
        assert watcher.poll_once(clock.now, clock.monotonic) == []
    assert caplog.records == []
    box.call_action = original


def test_new_session_rediscovers_signals_and_refreshes_window_keeping_tracker():
    first, clock = FakeBox(with_actions=False), FakeClock()
    second = FakeBox(with_actions=True)
    watcher, state = _watcher(first)
    watcher.poll_once(clock.now, clock.monotonic)
    tracker = watcher._tracker
    assert first.count(DIAG) == first.count(PREV) == 0
    assert watcher._window is not None

    state["session"] = second
    clock.advance(5)
    watcher.poll_once(clock.now, clock.monotonic)

    assert watcher._tracker is tracker
    assert second.count(DIAG) == 1
    assert second.count(PREV) == 1
    assert watcher._diag.succeeded and watcher._prevention.succeeded
    assert watcher._healthy_cycles == 1


def test_new_session_reenables_signals_disabled_by_discovery_faults():
    first, clock = FakeBox(), FakeClock()
    first.errors[("WANDSLInterfaceConfig", DIAG)] = TR064ActionNotFound("WANDSLInterfaceConfig", DIAG)
    first.errors[("WANPPPConnection:1", PREV)] = TR064ActionNotFound("WANPPPConnection:1", PREV)
    watcher, state = _watcher(first)
    watcher.poll_once(clock.now, clock.monotonic)
    assert watcher._diag.disabled and watcher._prevention.disabled
    second = FakeBox()
    state["session"] = second
    clock.advance(5)
    watcher.poll_once(clock.now, clock.monotonic)
    assert second.count(DIAG) == second.count(PREV) == 1
    assert not watcher._diag.disabled and not watcher._prevention.disabled
    assert watcher._diag.succeeded and watcher._prevention.succeeded


def test_new_session_discards_successful_but_stale_disconnect_window():
    first, clock = FakeBox(), FakeClock()
    watcher, state = _watcher(first)
    watcher.poll_once(clock.now, clock.monotonic)
    assert watcher._window.hour == 4
    second = FakeBox()
    second.prevention["NewX_AVM-DE_DisconnectPreventionHour"] = "8"
    state["session"] = second
    clock.advance(5)
    watcher.poll_once(clock.now, clock.monotonic)
    assert second.count(PREV) == 1
    assert watcher._window.hour == 8


def test_open_interruption_history_survives_session_replacement():
    from app.fritzbox.wan_tracker import WanInterruptionTracker

    first, clock = FakeBox(), FakeClock()
    watcher, state = _watcher(first, tracker_factory=lambda link: WanInterruptionTracker(link))
    watcher.poll_once(clock.now, clock.monotonic)
    first.status["NewConnectionStatus"] = "Disconnected"
    clock.advance(5)
    watcher.poll_once(clock.now, clock.monotonic)
    tracker = watcher._tracker
    assert tracker.needs_attention

    second = FakeBox()
    second.status["NewConnectionStatus"] = "Disconnected"
    state["session"] = second
    clock.advance(5)
    watcher.poll_once(clock.now, clock.monotonic)

    assert watcher._tracker is tracker
    assert tracker.needs_attention
    assert second.count(DIAG) == 1
    assert second.count(PREV) == 1

    second.status["NewConnectionStatus"] = "Connected"
    second.status["NewUptime"] = "1"
    clock.advance(5)
    events = watcher.poll_once(clock.now, clock.monotonic)
    assert [event.kind for event in events] == ["closed"]


def test_link_type_change_recreates_tracker():
    box, clock = FakeBox(), FakeClock()
    watcher, state = _watcher(box)
    watcher.poll_once(clock.now, clock.monotonic)
    first = watcher._tracker
    watcher.poll_once(clock.now, clock.monotonic)
    assert watcher._tracker is first
    state["link"] = "Ethernet"
    watcher.poll_once(clock.now, clock.monotonic)
    assert watcher._tracker is not first
    assert watcher._tracker.link_type == "Ethernet"


def test_window_refresh_at_most_every_900_seconds():
    box, clock = FakeBox(), FakeClock()
    watcher, _ = _watcher(box)
    watcher.poll_once(clock.now, clock.monotonic)
    for _ in range(10):
        clock.advance(5)
        watcher.poll_once(clock.now, clock.monotonic)
    assert box.count(PREV) == 1
    assert [c for c in box.calls if c[0] == "Time:1"] == [("Time:1", "GetInfo")]
    clock.advance(900)
    watcher.poll_once(clock.now, clock.monotonic)
    assert box.count(PREV) == 2


def test_fallback_window_and_single_fixed_log():
    box, clock = FakeBox(with_actions=False), FakeClock()
    messages = []
    watcher, _ = _watcher(box, log_once=lambda key, message: messages.append(message))
    for _ in range(3):
        clock.advance(5)
        watcher.poll_once(clock.now, clock.monotonic)
    assert box.count(PREV) == 0 and box.count(DIAG) == 0
    assert {w.kind for w in watcher._tracker.windows} == {"lifetime"}
    assert messages.count(wan_watch._FALLBACK_NOTICE) == 1
    assert "WAN watch signals: dsl_diagnose=no, disconnect_window=no" in messages


def test_unmeasurable_offset_falls_back_to_lifetime():
    box, clock = FakeBox(), FakeClock()
    box.local_time = "not a time"
    watcher, _ = _watcher(box)
    watcher.poll_once(clock.now, clock.monotonic)
    assert watcher._tracker.windows[0].kind == "lifetime"


def test_no_sensitive_data_in_logs(caplog):
    box, clock = FakeBox(), FakeClock()
    box.status["NewLastConnectionError"] = "PPP login failed for +491701234567 from 203.0.113.9"
    box.errors[("WANDSLInterfaceConfig", DIAG)] = RuntimeError("secret 198.51.100.7")
    watcher, _ = _watcher(box)
    with caplog.at_level(logging.INFO):
        watcher.poll_once(clock.now, clock.monotonic)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "203.0.113.9" not in text and "198.51.100.7" not in text and "+4917" not in text
    assert watcher._tracker.samples[0].last_error is None
    assert "RuntimeError" in text


def _watcher_with_call(box, call_ref):
    state = {"session": box, "link": "DSL"}
    watcher = WanWatcher(
        lambda: state["session"],
        lambda: state["link"],
        lambda exc: False,
        log_once=lambda key, message: None,
        tracker_factory=FakeTracker,
        status_call_provider=lambda: call_ref["call"],
    )
    return watcher


def test_disconnect_window_follows_the_active_ppp_service():
    box, clock = FakeBox(), FakeClock()
    box.services = _services(WANPPPConnection2=["GetInfo", PREV], WANDSLInterfaceConfig1=[DIAG])
    box.has_action = _has_action(box.services)
    watcher = _watcher_with_call(box, {"call": ("WANPPPConnection:2", "GetInfo")})
    watcher.poll_once(clock.now, clock.monotonic)
    assert ("WANPPPConnection:2", PREV) in box.calls
    assert ("WANPPPConnection:1", PREV) not in box.calls
    assert watcher._tracker.samples[-1].ppp_active is True


def test_active_ip_service_yields_no_ppp_window_and_no_ppp_evidence():
    box, clock = FakeBox(), FakeClock()
    watcher = _watcher_with_call(box, {"call": ("WANIPConnection:1", "GetInfo")})
    watcher.poll_once(clock.now, clock.monotonic)
    assert box.count(PREV) == 0
    assert watcher._tracker.windows[-1] is None
    assert watcher._tracker.samples[-1].ppp_active is False


def test_service_switch_keeps_the_tracker_and_resets_only_its_baseline():
    box, clock = FakeBox(), FakeClock()
    call_ref = {"call": ("WANPPPConnection:1", "GetInfo")}
    watcher = _watcher_with_call(box, call_ref)
    watcher.poll_once(clock.now, clock.monotonic)
    tracker = watcher._tracker
    call_ref["call"] = ("WANIPConnection:1", "GetInfo")
    clock.advance(5)
    watcher.poll_once(clock.now, clock.monotonic)
    assert watcher._tracker is tracker
    assert tracker.baseline_resets == 1
    assert tracker.windows[-1] is None
    call_ref["call"] = ("WANPPPConnection:1", "GetInfo")
    clock.advance(5)
    watcher.poll_once(clock.now, clock.monotonic)
    assert tracker.windows[-1] is not None
