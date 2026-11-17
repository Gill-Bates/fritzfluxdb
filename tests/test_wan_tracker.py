#!/usr/bin/env python3
#
# tests/test_wan_tracker.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.fritzbox.wan_cause import (
    CauseClass,
    Confidence,
    Detection,
    EvidenceKind,
    ForcedDisconnectWindow,
    WanSample,
)
from app.fritzbox.wan_tracker import WanInterruptionTracker

T0 = datetime(2026, 11, 16, 0, 0, 0, tzinfo=UTC)
LIFETIME_WINDOW = ForcedDisconnectWindow(kind="lifetime")


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def sample(t, *, connected=True, link_start=-72000.0, router_start=-500000.0, sync=None, error=None,
           router=True, mono=None, reliable=True, link=True):
    """Sample at second t; start values are seconds relative to T0 (None: no reading)."""
    mono = t if mono is None else mono
    kwargs = {}
    if router and router_start is not None:
        kwargs.update(
            router_uptime_s=int(t - router_start), router_observed_at=at(t), router_monotonic_s=mono
        )
    return WanSample(
        observed_at=at(t),
        monotonic_s=mono,
        connected=connected,
        link_uptime_s=int(t - link_start) if link and link_start is not None else None,
        start_reliable=reliable,
        dsl_sync=sync,
        last_error=error,
        **kwargs,
    )


def tracker(link_type="DSL", primed=True):
    tr = WanInterruptionTracker(link_type)
    if primed:
        assert tr.observe(sample(0), None) == []
    return tr


def kinds(events):
    return [e.kind for e in events]


def test_down_then_up_with_sync_is_ppp_interruption():
    tr = tracker()
    opened = tr.observe(sample(60, connected=False, link_start=None, sync=True), None)
    assert kinds(opened) == ["opened", "refined"]
    assert opened[0].result.cause is CauseClass.UNKNOWN
    assert opened[1].result.cause is CauseClass.PPP_INTERRUPTION
    closed = tr.observe(sample(120, link_start=100), None)
    assert kinds(closed) == ["closed"]
    event = closed[0]
    assert event.result.cause is CauseClass.PPP_INTERRUPTION
    assert event.result.confidence is Confidence.PROBABLE
    assert event.started_at == at(60)
    assert event.ended_at == at(100)
    assert event.duration_s == 40
    assert not tr.needs_attention


def test_sync_loss_sample_refines_and_is_sticky():
    tr = tracker()
    tr.observe(sample(60, connected=False, link_start=None, sync=True), None)
    refined = tr.observe(sample(65, connected=False, link_start=None, sync=False), None)
    assert refined[0].kind == "refined"
    assert refined[0].result.cause is CauseClass.DSL_SYNC_LOSS
    assert refined[0].result.confidence is Confidence.CONFIRMED
    tr.observe(sample(70, connected=False, link_start=None, sync=True), None)
    closed = tr.observe(sample(80, link_start=75), None)
    assert closed[0].result.cause is CauseClass.DSL_SYNC_LOSS


def test_single_down_sample_with_sync_one_and_zero():
    tr = tracker()
    events = tr.observe(sample(60, connected=False, link_start=None, sync=True), None)
    assert events[-1].result.cause is CauseClass.PPP_INTERRUPTION
    tr2 = tracker()
    events = tr2.observe(sample(60, connected=False, link_start=None, sync=False), None)
    assert events[-1].result.cause is CauseClass.DSL_SYNC_LOSS


def test_closing_sample_dsl_sync_is_not_counted():
    tr = tracker()
    tr.observe(sample(60, connected=False, link_start=None, sync=True), None)
    closed = tr.observe(sample(120, link_start=100, sync=False), None)
    assert closed[0].result.cause is CauseClass.PPP_INTERRUPTION


def test_router_decrease_confirms_router_restart():
    tr = tracker()
    tr.observe(sample(60, connected=False, link_start=None, sync=True), None)
    refined = tr.observe(sample(120, connected=False, link_start=None, router_start=100), None)
    assert refined[0].result.cause is CauseClass.ROUTER_RESTART
    assert refined[0].result.confidence is Confidence.CONFIRMED
    closed = tr.observe(sample(180, link_start=150, router_start=100), None)
    assert closed[0].result.cause is CauseClass.ROUTER_RESTART
    assert closed[0].result.evidence == (EvidenceKind.ROUTER_UPTIME_RESET,)


def test_router_decrease_in_opening_sample_applies_at_once():
    tr = tracker()
    events = tr.observe(sample(60, connected=False, link_start=None, router_start=30), None)
    assert kinds(events) == ["opened", "refined"]
    assert events[1].result.cause is CauseClass.ROUTER_RESTART


def test_router_shift_only_is_probable_never_confirmed():
    tr = tracker()
    # Counter grows but the derived start moved forward: a restart hidden between polls.
    closed = tr.observe(sample(60, router_start=-500000 + 40), None)
    assert kinds(closed) == ["closed"]
    assert closed[0].result.cause is CauseClass.ROUTER_RESTART
    assert closed[0].result.confidence is Confidence.PROBABLE
    assert closed[0].detected_by is Detection.START_SHIFT


def test_router_counter_decrease_between_polls_closes_confirmed():
    tr = tracker()
    closed = tr.observe(sample(60, router_start=50), None)
    # uptime 10 < 500000: decrease
    assert closed[0].result.confidence is Confidence.CONFIRMED
    assert closed[0].detected_by is Detection.UPTIME_RESET


def test_missing_dsl_sample_during_outage_prevents_sync_ok_claim():
    tr = tracker()
    tr.observe(sample(5, connected=False, sync=True), None)
    tr.observe(sample(10, connected=False, sync=None), None)
    closed = tr.observe(sample(15, link_start=12, sync=True), None)
    assert closed[0].result.cause is CauseClass.UNKNOWN
    assert EvidenceKind.DSL_SYNC_OK not in closed[0].result.evidence


def test_interruption_between_polls_via_link_start_shift_is_unknown():
    tr = tracker()
    closed = tr.observe(sample(60, link_start=-72000 + 30), None)
    assert kinds(closed) == ["closed"]
    event = closed[0]
    assert event.result.cause is CauseClass.UNKNOWN
    assert EvidenceKind.LINK_START_SHIFT in event.result.evidence
    assert event.started_at == at(0)
    # The derived start lies before the last good sample, so the closing sample time is the bound.
    assert event.ended_at == at(60)
    assert event.detected_by is Detection.START_SHIFT
    # Baseline moved: the next steady sample does not report again.
    assert tr.observe(sample(120, link_start=-72000 + 30), None) == []


def test_link_uptime_decrease_closes_unknown_on_dsl():
    tr = tracker()
    closed = tr.observe(sample(60, link_start=58), None)
    assert closed[0].result.cause is CauseClass.UNKNOWN
    assert closed[0].result.evidence == (EvidenceKind.LINK_UPTIME_RESET,)
    assert closed[0].detected_by is Detection.UPTIME_RESET
    assert closed[0].ended_at == at(58)


def test_multi_poll_outage_yields_exactly_one_interruption():
    tr = tracker()
    events = []
    for t in range(60, 300, 5):
        events += tr.observe(sample(t, connected=False, link_start=None, sync=True), None)
    events += tr.observe(sample(300, link_start=280), None)
    assert kinds(events).count("opened") == 1
    assert kinds(events).count("closed") == 1


def test_unreliable_sample_neither_shifts_nor_rebases():
    tr = tracker()
    assert tr.observe(sample(60, link_start=30, reliable=False), None) == []
    # Baseline stays the original one, so a real steady sample is still compared with it.
    assert tr.observe(sample(120, link_start=-72000), None) == []
    closed = tr.observe(sample(180, link_start=100), None)
    assert kinds(closed) == ["closed"]


def test_host_clock_step_rebases_without_event():
    tr = tracker()
    # Wall +70 s for a monotonic +60 s: the start comparison is skipped.
    assert tr.observe(sample(70, mono=60, link_start=-72000, router=False), None) == []
    assert tr.observe(sample(130, mono=120, link_start=-72000, router=False), None) == []


def test_clock_step_does_not_hide_counter_decrease():
    tr = tracker()
    closed = tr.observe(sample(70, mono=60, link_start=65, router=False), None)
    assert kinds(closed) == ["closed"]


def test_simultaneous_equal_shift_on_link_and_router_is_clock_step():
    tr = tracker()
    # Both derived starts moved by the same 30 s with growing counters: host clock, not WAN.
    assert tr.observe(sample(60, link_start=-72000 + 30, router_start=-500000 + 30), None) == []
    assert tr.observe(sample(120, link_start=-72000 + 30, router_start=-500000 + 30), None) == []


def test_connected_none_never_opens_and_never_sets_ppp_down():
    tr = tracker()
    assert tr.observe(sample(60, connected=None, link_start=None), None) == []
    assert not tr.needs_attention
    opened = tr.observe(sample(120, connected=False, link_start=None, sync=True), None)
    assert kinds(opened) == ["opened", "refined"]
    assert opened[0].started_at == at(120)
    # Unknown status samples with sync ok are no observed PPP down, so a later reset carries no PPP evidence.
    tr2 = tracker()
    assert tr2.observe(sample(60, connected=None, link_start=None, sync=True), None) == []
    assert tr2.observe(sample(65, connected=None, link_start=None, sync=True), None) == []
    closed = tr2.observe(sample(120, link_start=100), None)
    assert closed[0].result.cause is CauseClass.UNKNOWN
    assert EvidenceKind.PPP_DOWN_SEEN not in closed[0].result.evidence


def test_down_sample_without_dsl_reading_opens_unknown_without_refinement():
    tr = tracker()
    events = tr.observe(sample(60, connected=False, link_start=None, sync=None), None)
    # sync None on a down sample: DSL state unknown => row 6 unknown.
    assert kinds(events) == ["opened"]
    assert events[0].result.cause is CauseClass.UNKNOWN


def test_unreachable_flag_then_router_reset_closes_router_restart():
    tr = tracker()
    tr.note_unreachable()
    assert tr.needs_attention
    assert tr.wants_router_uptime(sample(60, router=False), 1)
    closed = tr.observe(sample(3600, link_start=3500, router_start=3000), None)
    assert closed[0].result.cause is CauseClass.ROUTER_RESTART
    assert closed[0].result.confidence is Confidence.CONFIRMED
    assert not tr.needs_attention


def test_unreachable_never_opens_an_interruption():
    tr = tracker()
    tr.note_unreachable()
    assert tr.observe(sample(60), None) == []
    assert not tr.needs_attention


def test_restart_reading_is_consumed_by_one_interruption():
    tr = tracker()
    tr.observe(sample(60, connected=False, link_start=None, router_start=30), None)
    closed = tr.observe(sample(120, link_start=100, router_start=30), None)
    assert closed[0].result.cause is CauseClass.ROUTER_RESTART
    assert tr.observe(sample(180, link_start=100, router_start=30), None) == []


def test_restart_reading_during_outage_is_consumed_without_closing_router_uptime():
    tr = tracker()
    tr.observe(sample(60, connected=False, link_start=None, router_start=30), None)
    closed = tr.observe(sample(120, link_start=100, router=False), None)

    assert kinds(closed) == ["closed"]
    assert closed[0].result.cause is CauseClass.ROUTER_RESTART
    assert tr.observe(sample(180, link_start=100, router_start=30), None) == []


def test_daemon_started_during_outage_has_no_lifetime_and_no_planned():
    tr = tracker(primed=False)
    tr.observe(sample(60, connected=False, link_start=None, sync=True), LIFETIME_WINDOW)
    closed = tr.observe(sample(120, link_start=100), LIFETIME_WINDOW)
    assert closed[0].result.cause is CauseClass.PPP_INTERRUPTION


def test_planned_probable_in_lifetime_window():
    # Link start -86390 s: the session is just under 24 h old when the link drops at t=5.
    tr = WanInterruptionTracker("DSL")
    tr.observe(sample(0, link_start=-86390), LIFETIME_WINDOW)
    events = tr.observe(sample(5, connected=False, link_start=None, sync=True), LIFETIME_WINDOW)
    assert events[-1].result.cause is CauseClass.PLANNED_DISCONNECT
    assert events[-1].result.confidence is Confidence.PROBABLE
    closed = tr.observe(sample(30, link_start=20), LIFETIME_WINDOW)
    assert closed[0].result.cause is CauseClass.PLANNED_DISCONNECT


def test_not_planned_when_lifetime_is_off():
    tr = WanInterruptionTracker("DSL")
    tr.observe(sample(0, link_start=-10000), LIFETIME_WINDOW)
    events = tr.observe(sample(5, connected=False, link_start=None, sync=True), LIFETIME_WINDOW)
    assert events[-1].result.cause is CauseClass.PPP_INTERRUPTION


def test_24h_uptime_alone_never_classifies():
    tr = WanInterruptionTracker("DSL")
    assert tr.observe(sample(0, link_start=-86400), LIFETIME_WINDOW) == []
    assert tr.observe(sample(60, link_start=-86400), LIFETIME_WINDOW) == []
    # DSL state unknown on a down sample => unknown even at 24 h.
    events = tr.observe(sample(65, connected=False, link_start=None, sync=None), LIFETIME_WINDOW)
    assert events[-1].result.cause is CauseClass.UNKNOWN


def _hour_window():
    return ForcedDisconnectWindow(kind="hour_of_day", hour=4, utc_offset_s=0)


def _day_tracker(last_good, down):
    """Tracker with a 20 h session whose last good sample and first down sample are given (seconds of day)."""
    tr = WanInterruptionTracker("DSL")
    base = -72000 + last_good
    tr.observe(sample(last_good, link_start=base), _hour_window())
    return tr, tr.observe(sample(down, connected=False, link_start=None, sync=True), _hour_window())


def test_hour_window_disconnect_instant_inside():
    # 03:59:50 good, 03:59:58 down.
    _, events = _day_tracker(3 * 3600 + 59 * 60 + 50, 3 * 3600 + 59 * 60 + 58)
    assert events[-1].result.cause is CauseClass.PLANNED_DISCONNECT
    assert EvidenceKind.HOUR_IN_WINDOW in events[-1].result.evidence


def test_hour_window_outage_starting_outside_is_ppp_interruption():
    # Good 03:49:55, down 03:50:00, up 04:10: only the interval before the outage counts.
    tr, events = _day_tracker(3 * 3600 + 49 * 60 + 55, 3 * 3600 + 50 * 60)
    assert events[-1].result.cause is CauseClass.PPP_INTERRUPTION
    closed = tr.observe(sample(4 * 3600 + 10 * 60, link_start=4 * 3600 + 9 * 60), _hour_window())
    assert closed[0].result.cause is CauseClass.PPP_INTERRUPTION


def test_hour_window_result_independent_of_closing_sample_time():
    for close in (3600 * 4 + 120, 3600 * 9):
        tr, _ = _day_tracker(2 * 3600, 2 * 3600 + 5)
        closed = tr.observe(sample(close, link_start=close - 20), _hour_window())
        assert closed[0].result.cause is CauseClass.PPP_INTERRUPTION


def test_hour_window_edge_is_widened_once_only():
    # Interval end 03:54:59 does not reach 04:00; 03:55:00 does.
    _, no_match = _day_tracker(3 * 3600 + 54 * 60 + 54, 3 * 3600 + 54 * 60 + 59)
    assert no_match[-1].result.cause is CauseClass.PPP_INTERRUPTION
    _, match = _day_tracker(3 * 3600 + 54 * 60 + 55, 3 * 3600 + 55 * 60)
    assert match[-1].result.cause is CauseClass.PLANNED_DISCONNECT


def test_gap_longer_than_one_poll_gives_no_window_evidence():
    # A 58 s hole between last good and first down sample was not observed as one transition.
    _, events = _day_tracker(3 * 3600 + 59 * 60, 3 * 3600 + 59 * 60 + 58)
    assert events[-1].result.cause is CauseClass.PPP_INTERRUPTION


def test_forced_error_new_in_interruption_counts():
    tr = WanInterruptionTracker("DSL")
    tr.observe(sample(0, link_start=-10000, error="ERROR_NONE"), LIFETIME_WINDOW)
    events = tr.observe(
        sample(5, connected=False, link_start=None, sync=True, error="ERROR_FORCED_DISCONNECT"),
        LIFETIME_WINDOW,
    )
    assert events[-1].result.cause is CauseClass.PLANNED_DISCONNECT
    assert EvidenceKind.ROUTER_REPORTS_FORCED_DISCONNECT in events[-1].result.evidence


def test_stale_forced_error_is_ignored():
    tr = WanInterruptionTracker("DSL")
    tr.observe(sample(0, link_start=-10000, error="ERROR_FORCED_DISCONNECT"), LIFETIME_WINDOW)
    events = tr.observe(
        sample(5, connected=False, link_start=None, sync=True, error="ERROR_FORCED_DISCONNECT"),
        LIFETIME_WINDOW,
    )
    assert events[-1].result.cause is CauseClass.PPP_INTERRUPTION


def test_forced_error_without_good_baseline_is_ignored():
    tr = WanInterruptionTracker("DSL")
    events = tr.observe(
        sample(5, connected=False, link_start=None, sync=True, error="ERROR_FORCED_DISCONNECT"),
        LIFETIME_WINDOW,
    )
    assert events[-1].result.cause is CauseClass.PPP_INTERRUPTION


def test_new_forced_error_classifies_without_dsl_sync_reading():
    tr = WanInterruptionTracker("DSL")
    tr.observe(sample(0, link_start=-10000, error="ERROR_NONE"), None)
    events = tr.observe(
        sample(5, connected=False, link_start=None, sync=None, error="ERROR_FORCED_DISCONNECT"), None
    )
    assert events[-1].result.cause is CauseClass.PLANNED_DISCONNECT
    assert events[-1].result.evidence == (EvidenceKind.ROUTER_REPORTS_FORCED_DISCONNECT,)


def test_forced_disconnect_between_polls_needs_no_uptime_reading():
    tr = WanInterruptionTracker("DSL")
    tr.observe(sample(0, link=False, router=False, error="ERROR_NONE"), None)
    events = tr.observe(sample(5, link=False, router=False, error="ERROR_FORCED_DISCONNECT"), None)
    assert kinds(events) == ["closed"]
    assert events[0].result.cause is CauseClass.PLANNED_DISCONNECT
    assert events[0].result.confidence is Confidence.PROBABLE
    assert events[0].result.evidence == (EvidenceKind.ROUTER_REPORTS_FORCED_DISCONNECT,)
    assert events[0].detected_by is Detection.CONNECTION_ERROR
    assert events[0].duration_s == 5
    assert tr.observe(sample(10, link=False, router=False, error="ERROR_FORCED_DISCONNECT"), None) == []
    tr.observe(sample(15, link=False, router=False, error="ERROR_NONE"), None)
    assert len(tr.observe(sample(20, link=False, router=False, error="ERROR_FORCED_DISCONNECT"), None)) == 1


def test_forced_error_first_seen_on_recovery_is_used():
    tr = WanInterruptionTracker("DSL")
    tr.observe(sample(0, error="ERROR_NONE"), None)
    tr.observe(sample(5, connected=False, link=False, error="ERROR_NONE"), None)
    closed = tr.observe(sample(10, link_start=8, error="ERROR_FORCED_DISCONNECT"), None)
    assert kinds(closed) == ["closed"]
    assert closed[0].result.cause is CauseClass.PLANNED_DISCONNECT
    assert tr.observe(sample(15, link_start=8, error="ERROR_FORCED_DISCONNECT"), None) == []


def test_forced_error_present_at_startup_never_creates_an_event():
    tr = WanInterruptionTracker("DSL")
    for t in (0, 5, 10):
        assert tr.observe(sample(t, error="ERROR_FORCED_DISCONNECT"), None) == []


def test_new_forced_error_does_not_override_router_restart_or_sync_loss():
    tr = WanInterruptionTracker("DSL")
    tr.observe(sample(0, error="ERROR_NONE"), None)
    closed = tr.observe(sample(5, router_start=2, link_start=4, error="ERROR_FORCED_DISCONNECT"), None)
    assert closed[0].result.cause is CauseClass.ROUTER_RESTART
    tr = WanInterruptionTracker("DSL")
    tr.observe(sample(0, error="ERROR_NONE"), None)
    events = tr.observe(sample(5, connected=False, sync=False, error="ERROR_FORCED_DISCONNECT"), None)
    assert events[-1].result.cause is CauseClass.DSL_SYNC_LOSS


def test_new_forced_error_with_sync_loss_in_same_poll_is_sync_loss():
    tr = WanInterruptionTracker("DSL")
    tr.observe(sample(0, error="ERROR_NONE"), None)
    events = tr.observe(sample(5, sync=False, error="ERROR_FORCED_DISCONNECT"), None)
    assert kinds(events) == ["closed"]
    assert events[0].result.cause is CauseClass.DSL_SYNC_LOSS


def test_new_forced_error_with_sync_ok_in_same_poll_stays_planned():
    tr = WanInterruptionTracker("DSL")
    tr.observe(sample(0, error="ERROR_NONE"), None)
    events = tr.observe(sample(5, sync=True, error="ERROR_FORCED_DISCONNECT"), None)
    assert events[0].result.cause is CauseClass.PLANNED_DISCONNECT


def test_out_of_order_and_duplicate_samples_are_ignored():
    tr = tracker()
    tr.observe(sample(60), None)
    assert tr.observe(sample(60, connected=False, link_start=None), None) == []
    assert tr.observe(sample(30, connected=False, link_start=None), None) == []
    assert not tr.needs_attention


def test_non_dsl_link_never_classifies_planned_or_ppp():
    tr = WanInterruptionTracker("Cable")
    tr.observe(sample(0, link_start=-86390), LIFETIME_WINDOW)
    events = tr.observe(sample(5, connected=False, link_start=None, sync=True), LIFETIME_WINDOW)
    assert events[-1].result.cause is CauseClass.UNKNOWN
    closed = tr.observe(sample(30, link_start=20), LIFETIME_WINDOW)
    assert closed[0].result.cause is CauseClass.UNKNOWN


def test_non_dsl_router_restart_still_classified():
    tr = WanInterruptionTracker("Cable")
    tr.observe(sample(0), None)
    closed = tr.observe(sample(60, router_start=40), None)
    assert closed[0].result.cause is CauseClass.ROUTER_RESTART


def test_wants_router_uptime_rules():
    tr = tracker(primed=False)
    assert tr.wants_router_uptime(sample(0, router=False), 1)  # no router baseline yet
    tr.observe(sample(0), None)
    assert not tr.wants_router_uptime(sample(60, router=False), 1)
    assert tr.wants_router_uptime(sample(60, router=False), 6)
    assert tr.wants_router_uptime(sample(60, connected=False, router=False, link_start=None), 2)
    assert tr.wants_router_uptime(sample(60, link_start=30, router=False), 2)  # link anomaly


def test_down_after_close_opens_a_new_interruption():
    tr = tracker()
    tr.observe(sample(60, connected=False, link_start=None, sync=True), None)
    tr.observe(sample(120, link_start=100), None)
    events = tr.observe(sample(180, connected=False, link_start=None, sync=False), None)
    assert kinds(events) == ["opened", "refined"]
    assert events[0].started_at == at(180)


def test_tracker_events_are_frozen():
    event = tracker().observe(sample(60, connected=False, link_start=None), None)[0]
    with pytest.raises(ValidationError):
        event.kind = "closed"


STEP_BACK_S = -3600.0


def stepped_sample(real_t, **kwargs):
    """Sample whose wall clock was stepped back 1 h at real second 30; mono and counters run on."""
    wall = real_t + (STEP_BACK_S if real_t >= 30 else 0.0)
    kwargs.setdefault("link_start", wall - (real_t + 72000))
    kwargs.setdefault("router", False)
    return sample(wall, mono=real_t, **kwargs)


def test_wall_clock_step_back_does_not_drop_samples():
    tr = tracker()
    assert tr.observe(stepped_sample(35), None) == []
    assert tr.observe(stepped_sample(40), None) == []
    events = tr.observe(stepped_sample(45, connected=False, link_start=None), None)
    assert kinds(events)[0] == "opened"
    assert tr.needs_attention


def test_down_right_after_clock_correction_opens_without_window_evidence():
    tr = WanInterruptionTracker("DSL")
    tr.observe(sample(0, link_start=-(86400 - 30)), None)
    events = tr.observe(stepped_sample(35, connected=False, link_start=None, sync=True), LIFETIME_WINDOW)
    assert kinds(events)[0] == "opened"
    assert all(e.result.cause is not CauseClass.PLANNED_DISCONNECT for e in events)
    closed = tr.observe(stepped_sample(40, link_start=-3600 - 86400 + 40), LIFETIME_WINDOW)
    assert kinds(closed) == ["closed"]
    assert closed[0].result.cause is not CauseClass.PLANNED_DISCONNECT


def test_normal_progress_and_equal_monotonic_samples():
    tr = tracker()
    assert tr.observe(sample(5), None) == []
    assert tr.observe(sample(10, mono=5), None) == []
    assert tr.observe(sample(10), None) == []


@pytest.mark.parametrize("step_s", [-3600, 3600])
def test_open_interruption_duration_uses_monotonic_clock_across_time_steps(step_s):
    tr = tracker()
    tr.observe(sample(5, connected=False, link=False, sync=True), None)
    tr.observe(sample(20 + step_s, mono=20, connected=False, link=False, sync=True, router=False), None)
    closed = tr.observe(sample(60 + step_s, mono=60, link_start=57 + step_s, router=False), None)[0]
    assert closed.duration_s == 52
    assert closed.ended_at == at(57 + step_s)
    assert closed.started_at == at(5 + step_s)
    assert closed.started_at <= closed.ended_at <= closed.observed_at
    assert (closed.ended_at - closed.started_at).total_seconds() == closed.duration_s


@pytest.mark.parametrize("step_s", [-3600, 3600])
def test_closed_start_follows_the_closing_clock_while_opened_keeps_the_observed_time(step_s):
    # Lifecycle contract (data-model.md): only the opening log keeps the wall time seen then.
    tr = tracker()
    opened = tr.observe(sample(5, connected=False, link=False, sync=True), None)[0]
    closed = tr.observe(sample(60 + step_s, mono=60, link_start=57 + step_s, router=False), None)[0]
    assert opened.started_at == at(5)
    assert closed.started_at == at(5 + step_s)
    assert closed.started_at <= closed.ended_at <= closed.observed_at


@pytest.mark.parametrize("step_s", [-3600, 3600])
def test_between_poll_interruption_uses_monotonic_bounds_across_time_steps(step_s):
    tr = tracker()
    tr.observe(sample(5, router=False), None)
    closed = tr.observe(sample(10 + step_s, mono=10, link_start=7 + step_s, router=False), None)[0]
    assert closed.duration_s == 2
    assert closed.started_at == at(5 + step_s)
    assert closed.ended_at == at(7 + step_s)
    assert closed.started_at <= closed.ended_at <= closed.observed_at


@pytest.mark.parametrize("error", [None, "invalid", "ERROR_BAD\nforged"])
def test_latest_connected_status_bounds_forced_event_even_without_error_or_uptimes(error):
    tr = WanInterruptionTracker("DSL")
    tr.observe(sample(0, link=False, router=False, error="ERROR_NONE"), None)
    for t in (3600, 3605):
        assert tr.observe(sample(t, link=False, router=False, error=error), None) == []
    closed = tr.observe(sample(3610, link=False, router=False, error="ERROR_FORCED_DISCONNECT"), None)[0]
    assert closed.duration_s == 5
    assert closed.started_at == at(3605)
    assert closed.result.cause is CauseClass.PLANNED_DISCONNECT


def test_unobserved_gap_has_no_measured_duration_but_keeps_the_last_observation():
    tr = WanInterruptionTracker("DSL")
    tr.observe(sample(0, link=False, router=False, error="ERROR_NONE"), None)
    tr.observe(sample(100, link=False, router=False), None)
    closed = tr.observe(sample(3700, link=False, router=False, error="ERROR_FORCED_DISCONNECT"), None)[0]
    assert closed.duration_s is None
    assert closed.started_at == at(100)
    assert closed.detected_by is Detection.CONNECTION_ERROR


def test_reset_after_a_long_observation_gap_has_no_measured_duration():
    tr = tracker()
    # Two hours without a valid status, then a reset counter: the outage lies somewhere in between.
    for t in (600, 3600, 7000):
        assert tr.observe(sample(t, connected=None, link_start=None), None) == []
    closed = tr.observe(sample(7200, link_start=7190), None)
    assert kinds(closed) == ["closed"]
    assert closed[0].duration_s is None
    assert closed[0].started_at == at(0)


def test_reset_between_two_adjacent_polls_keeps_a_measured_bound():
    tr = tracker()
    closed = tr.observe(sample(5, link_start=3), None)
    assert closed[0].duration_s == 3


def test_service_change_keeps_the_open_interruption_and_closes_it_measured():
    tr = tracker()
    opened = tr.observe(sample(60, connected=False, link_start=None, sync=True), None)
    assert kinds(opened) == ["opened", "refined"]
    tr.reset_link_baseline()
    assert tr.needs_attention
    closed = tr.observe(sample(90, link_start=85), None)
    assert kinds(closed) == ["closed"]
    assert closed[0].started_at == at(60)
    assert closed[0].duration_s == 25


def test_service_change_while_idle_drops_the_old_services_counters():
    tr = tracker()
    tr.reset_link_baseline()
    # A smaller uptime counter of the new service must not look like a reset of the old one.
    assert tr.observe(sample(5, link_start=-10), None) == []


def test_down_state_of_a_non_ppp_service_is_no_ppp_evidence():
    tr = tracker()
    down = sample(60, connected=False, link_start=None, sync=True).model_copy(update={"ppp_active": False})
    opened = tr.observe(down, None)
    assert kinds(opened) == ["opened"]
    closed = tr.observe(sample(120, link_start=100), None)
    assert closed[0].result.cause is CauseClass.UNKNOWN
