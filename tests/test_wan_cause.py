#!/usr/bin/env python3
#
# tests/test_wan_cause.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.fritzbox.wan_cause import (
    CauseClass,
    Confidence,
    Detection,
    DslState,
    EvidenceKind,
    ForcedDisconnectWindow,
    WanCauseResult,
    WanEvidence,
    WanSample,
    aggregate_dsl_state,
    classify_interruption,
    clock_stepped,
    derive_start_time,
    detect_counter_reset,
    in_forced_window,
    is_clock_step_pair,
    max_observation_gap_s,
    start_shifted_forward,
)

T0 = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)
E = EvidenceKind


def at(h, m=0, s=0):
    return datetime(2026, 11, 16, h, m, s, tzinfo=UTC)


def dsl(**kw):
    return WanEvidence(link_is_dsl=True, **kw)


# Start time derivation

def test_derive_start_time_normal_none_negative_naive():
    assert derive_start_time(T0, 100) == T0 - timedelta(seconds=100)
    assert derive_start_time(T0, None) is None
    assert derive_start_time(T0, -1) is None
    with pytest.raises(ValueError):
        derive_start_time(datetime(2026, 1, 1), 5)


def test_start_shift_threshold_for_60s_gap():
    assert not start_shifted_forward(T0, T0 + timedelta(seconds=4), 60)
    assert start_shifted_forward(T0, T0 + timedelta(seconds=6), 60)
    assert not start_shifted_forward(T0, T0 - timedelta(seconds=30), 60)
    assert not start_shifted_forward(None, T0, 60)


def test_start_shift_tolerance_grows_with_gap_27h():
    gap = 97200
    assert not start_shifted_forward(T0, T0 + timedelta(seconds=12), gap)
    assert start_shifted_forward(T0, T0 + timedelta(seconds=16), gap)


def test_worst_case_measurement_error_does_not_shift():
    # RTT 2 s with opposite midpoint errors (1 s each) plus a 0.99 s floor difference.
    prev = derive_start_time(T0 - timedelta(seconds=1), 1000)
    new = derive_start_time(T0 + timedelta(seconds=60 + 1), 1060 - 1)
    assert (new - prev).total_seconds() < 3.01
    assert not start_shifted_forward(prev, new, 60)


def test_clock_stepped():
    assert not clock_stepped(T0, 100.0, T0 + timedelta(seconds=60), 160.0)
    assert clock_stepped(T0, 100.0, T0 + timedelta(seconds=63.5), 160.0)
    # Suspend: wall advanced 1 h, monotonic nearly nothing.
    assert clock_stepped(T0, 100.0, T0 + timedelta(hours=1), 105.0)


def test_detect_counter_reset():
    start = derive_start_time(T0, 1000)
    later = T0 + timedelta(seconds=60)
    assert detect_counter_reset(1000, start, 1060, derive_start_time(later, 1060), 60) is None
    assert detect_counter_reset(1000, start, 10, derive_start_time(later, 10), 60) is Detection.UPTIME_RESET
    # Interruption fully between two polls: counter still grows, start moved forward.
    assert detect_counter_reset(1000, start, 1050, derive_start_time(later, 1050), 60) is Detection.START_SHIFT
    assert detect_counter_reset(1000, start, None, None, 60) is None


def test_counter_decrease_is_a_reset_even_without_a_derived_start():
    assert detect_counter_reset(1000, None, 2, None, 60) is Detection.UPTIME_RESET
    assert detect_counter_reset(None, None, 2, None, 60) is None


def test_is_clock_step_pair():
    s = Detection.START_SHIFT
    assert is_clock_step_pair(s, s, 10.0, 11.0, False)
    assert not is_clock_step_pair(s, s, 10.0, 90.0, False)
    assert not is_clock_step_pair(Detection.UPTIME_RESET, s, 10.0, 10.0, False)
    assert not is_clock_step_pair(s, s, 10.0, 10.0, True)
    assert not is_clock_step_pair(s, None, 10.0, 0.0, False)


def test_aggregate_dsl_state():
    assert aggregate_dsl_state([]) is DslState.UNKNOWN
    assert aggregate_dsl_state([None, None]) is DslState.UNKNOWN
    assert aggregate_dsl_state([True, True]) is DslState.SYNC_OK
    assert aggregate_dsl_state([True, False, True]) is DslState.SYNC_LOST
    assert aggregate_dsl_state([None, False]) is DslState.SYNC_LOST


@pytest.mark.parametrize("samples", [[True, None, True], [True, None, None, True], [None, True]])
def test_unreadable_dsl_sample_during_outage_is_never_sync_ok(samples):
    assert aggregate_dsl_state(samples) is DslState.UNKNOWN


# Forced-disconnect window

HOUR4 = ForcedDisconnectWindow(kind="hour_of_day", hour=4, utc_offset_s=0)
LIFETIME = ForcedDisconnectWindow(kind="lifetime")
DAY = 86400
# Wide enough for the window arithmetic tests; the gap limit has its own test.
WIDE = 7200


def test_hour_window_inside_edges_outside():
    assert in_forced_window(HOUR4, at(3, 59, 0), at(3, 59, 58), DAY, max_interval_s=WIDE) is E.HOUR_IN_WINDOW
    # Single 300 s widening: 03:54:59 no, 03:55:00 yes.
    assert in_forced_window(HOUR4, at(3, 49), at(3, 54, 59), DAY, max_interval_s=WIDE) is None
    assert in_forced_window(HOUR4, at(3, 49), at(3, 55, 0), DAY, max_interval_s=WIDE) is E.HOUR_IN_WINDOW
    assert in_forced_window(HOUR4, at(5, 5, 1), at(5, 10), DAY, max_interval_s=WIDE) is None
    assert in_forced_window(HOUR4, at(5, 5, 0), at(5, 10), DAY, max_interval_s=WIDE) is E.HOUR_IN_WINDOW


def test_hour_window_outage_start_outside_end_inside_gives_no_match():
    # Only last good (02:00) and first down (02:01) matter, never the closing sample.
    assert in_forced_window(HOUR4, at(2, 0), at(2, 1), DAY, max_interval_s=WIDE) is None


def test_hour_window_short_session_and_unknown_lifetime():
    assert in_forced_window(HOUR4, at(3, 59), at(4, 0), 43199, max_interval_s=WIDE) is None
    assert in_forced_window(HOUR4, at(3, 59), at(4, 0), 43200, max_interval_s=WIDE) is E.HOUR_IN_WINDOW
    assert in_forced_window(HOUR4, at(3, 59), at(4, 0), None, max_interval_s=WIDE) is None


def test_hour_window_midnight_crossing_with_offset():
    window = ForcedDisconnectWindow(kind="hour_of_day", hour=0, utc_offset_s=3600)
    # 23:30 UTC is 00:30 box-local on the next day.
    assert in_forced_window(window, at(23, 29), at(23, 31), DAY, max_interval_s=WIDE) is E.HOUR_IN_WINDOW
    assert in_forced_window(window, at(12, 0), at(12, 1), DAY, max_interval_s=WIDE) is None


def test_lifetime_window_bounds():
    assert in_forced_window(LIFETIME, at(1), at(2), DAY, max_interval_s=WIDE) is E.LIFETIME_IN_WINDOW
    assert in_forced_window(LIFETIME, at(1), at(2), DAY + 900, max_interval_s=WIDE) is E.LIFETIME_IN_WINDOW
    assert in_forced_window(LIFETIME, at(1), at(2), DAY + 901, max_interval_s=WIDE) is None
    assert in_forced_window(LIFETIME, at(1), at(2), DAY - 901, max_interval_s=WIDE) is None
    assert in_forced_window(LIFETIME, at(1), at(2), None, max_interval_s=WIDE) is None


def test_window_evidence_needs_a_closely_observed_transition():
    gap = max_observation_gap_s(5)
    assert gap == 12
    # 03:50 last good, 04:10 first down: overlaps the window but was not observed for 20 min.
    assert in_forced_window(HOUR4, at(3, 50), at(4, 10), DAY, max_interval_s=gap) is None
    assert in_forced_window(HOUR4, at(4, 10), at(4, 10, 12), DAY, max_interval_s=gap) is E.HOUR_IN_WINDOW
    assert in_forced_window(HOUR4, at(4, 10), at(4, 10, 13), DAY, max_interval_s=gap) is None
    assert in_forced_window(HOUR4, at(4, 10, 5), at(4, 10), DAY, max_interval_s=gap) is None
    assert in_forced_window(LIFETIME, at(2), at(1), DAY, max_interval_s=gap) is None


def test_no_window_means_no_evidence():
    assert in_forced_window(None, at(1), at(2), DAY, max_interval_s=WIDE) is None


# Classification: one test per decision-table row or scenario

def test_router_restart_confirmed_by_counter_decrease():
    r = classify_interruption(dsl(router_reset=True, dsl_state=DslState.SYNC_OK, ppp_down_seen=True))
    assert (r.cause, r.confidence) == (CauseClass.ROUTER_RESTART, Confidence.CONFIRMED)
    assert E.ROUTER_UPTIME_RESET in r.evidence


def test_dsl_resync_confirmed_and_outranks_router_shift():
    r = classify_interruption(dsl(dsl_state=DslState.SYNC_LOST, router_shift=True, ppp_down_seen=True))
    assert (r.cause, r.confidence) == (CauseClass.DSL_SYNC_LOSS, Confidence.CONFIRMED)


def test_router_reset_outranks_dsl_sync_loss():
    r = classify_interruption(dsl(router_reset=True, dsl_state=DslState.SYNC_LOST))
    assert r.cause is CauseClass.ROUTER_RESTART


def test_router_start_shift_alone_is_only_probable():
    r = classify_interruption(WanEvidence(router_shift=True))
    assert (r.cause, r.confidence) == (CauseClass.ROUTER_RESTART, Confidence.PROBABLE)
    assert E.ROUTER_UPTIME_RESET not in r.evidence


@pytest.mark.parametrize("match", [E.HOUR_IN_WINDOW, E.LIFETIME_IN_WINDOW])
def test_ppp_redial_inside_window_is_planned_probable(match):
    r = classify_interruption(
        dsl(dsl_state=DslState.SYNC_OK, ppp_down_seen=True, window_match=match)
    )
    assert (r.cause, r.confidence) == (CauseClass.PLANNED_DISCONNECT, Confidence.PROBABLE)
    assert match in r.evidence


def test_ppp_redial_outside_window_is_plain_ppp_interruption():
    r = classify_interruption(dsl(dsl_state=DslState.SYNC_OK, ppp_down_seen=True))
    assert (r.cause, r.confidence) == (CauseClass.PPP_INTERRUPTION, Confidence.PROBABLE)


def test_router_reported_forced_disconnect_is_probable_without_other_signals():
    ok = classify_interruption(
        dsl(dsl_state=DslState.SYNC_OK, ppp_down_seen=True, router_reports_forced_disconnect=True)
    )
    assert (ok.cause, ok.confidence) == (CauseClass.PLANNED_DISCONNECT, Confidence.PROBABLE)
    alone = classify_interruption(dsl(router_reports_forced_disconnect=True))
    assert (alone.cause, alone.confidence) == (CauseClass.PLANNED_DISCONNECT, Confidence.PROBABLE)
    assert alone.evidence == (E.ROUTER_REPORTS_FORCED_DISCONNECT,)
    no_down = classify_interruption(
        dsl(dsl_state=DslState.SYNC_OK, router_reports_forced_disconnect=True)
    )
    assert (no_down.cause, no_down.confidence) == (CauseClass.PLANNED_DISCONNECT, Confidence.PROBABLE)


def test_interruption_between_polls_via_start_shift_only_is_unknown():
    # Link start shifted, no down sample, no DSL sample inside: nothing to attribute it to.
    r = classify_interruption(WanEvidence(link_is_dsl=True, extra=(E.LINK_START_SHIFT,)))
    assert (r.cause, r.confidence) == (CauseClass.UNKNOWN, Confidence.UNKNOWN)
    assert r.evidence == (E.LINK_START_SHIFT,)


def test_end_to_end_hidden_interruption_detected_by_shift_then_unknown():
    prev_start = derive_start_time(T0, 5000)
    now = T0 + timedelta(seconds=60)
    new_start = derive_start_time(now, 5030)  # a 30 s outage hid between the polls
    assert detect_counter_reset(5000, prev_start, 5030, new_start, 60) is Detection.START_SHIFT
    r = classify_interruption(WanEvidence(link_is_dsl=True, extra=(E.LINK_START_SHIFT,)))
    assert r.cause is CauseClass.UNKNOWN


@pytest.mark.parametrize(
    "evidence",
    [
        WanEvidence(ppp_down_seen=True, dsl_state=DslState.SYNC_OK, window_match=E.LIFETIME_IN_WINDOW),
        dsl(ppp_down_seen=True, dsl_state=DslState.UNKNOWN, window_match=E.LIFETIME_IN_WINDOW),
        dsl(ppp_down_seen=False, dsl_state=DslState.SYNC_OK, window_match=E.LIFETIME_IN_WINDOW),
        dsl(),
    ],
)
def test_ambiguous_evidence_is_unknown(evidence):
    r = classify_interruption(evidence)
    assert (r.cause, r.confidence) == (CauseClass.UNKNOWN, Confidence.UNKNOWN)


def test_24h_uptime_alone_is_never_a_forced_disconnect():
    # A ~24 h session only becomes evidence via in_forced_window, which needs a real down sample.
    window = in_forced_window(LIFETIME, at(1), at(2), DAY, max_interval_s=WIDE)
    assert window is E.LIFETIME_IN_WINDOW
    for evidence in (
        dsl(window_match=window),
        dsl(window_match=window, dsl_state=DslState.UNKNOWN, ppp_down_seen=True),
        dsl(window_match=window, dsl_state=DslState.SYNC_OK),
        WanEvidence(window_match=window, ppp_down_seen=True),
    ):
        assert classify_interruption(evidence).cause is CauseClass.UNKNOWN


def test_short_session_redial_is_ppp_interruption():
    window = in_forced_window(LIFETIME, at(1), at(2), 3 * 3600, max_interval_s=WIDE)
    r = classify_interruption(dsl(dsl_state=DslState.SYNC_OK, ppp_down_seen=True, window_match=window))
    assert r.cause is CauseClass.PPP_INTERRUPTION


def test_non_dsl_link_only_reaches_router_rows_or_unknown():
    assert classify_interruption(WanEvidence(dsl_state=DslState.SYNC_LOST)).cause is CauseClass.UNKNOWN
    assert classify_interruption(WanEvidence(router_reset=True)).cause is CauseClass.ROUTER_RESTART


# Models

def test_result_validators():
    with pytest.raises(ValidationError):
        WanCauseResult(cause=CauseClass.PLANNED_DISCONNECT, confidence=Confidence.CONFIRMED)
    with pytest.raises(ValidationError):
        WanCauseResult(cause=CauseClass.UNKNOWN, confidence=Confidence.PROBABLE)
    with pytest.raises(ValidationError):
        WanCauseResult(cause=CauseClass.ROUTER_RESTART, confidence=Confidence.CONFIRMED, evidence=(E.ROUTER_START_SHIFT,))


def test_models_are_frozen_and_sample_is_sanitised():
    with pytest.raises(ValidationError):
        WanSample(observed_at=datetime(2026, 1, 1), monotonic_s=1.0)
    s = WanSample(observed_at=T0, monotonic_s=1.0, link_uptime_s=-4, last_error="free text 10.0.0.1")
    assert s.link_uptime_s is None and s.last_error is None
    assert WanSample(observed_at=T0, monotonic_s=1.0, last_error="ERROR_FORCED_DISCONNECT").last_error
    with pytest.raises(ValidationError):
        s.connected = True
    with pytest.raises(ValidationError):
        ForcedDisconnectWindow(kind="hour_of_day", hour=24, utc_offset_s=0)
    with pytest.raises(ValidationError):
        ForcedDisconnectWindow(kind="hour_of_day", hour=3)


def test_error_code_with_a_trailing_newline_is_dropped():
    assert WanSample(observed_at=T0, monotonic_s=1.0, last_error="ERROR_FORCED_DISCONNECT\n").last_error is None


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "lifetime", "lifetime_s": 0},
        {"kind": "lifetime", "tolerance_s": -1},
        {"kind": "lifetime", "lifetime_s": 900, "tolerance_s": 900},
        {"kind": "hour_of_day", "hour": 4, "utc_offset_s": 14 * 3600 + 1},
        {"kind": "hour_of_day", "hour": 4, "utc_offset_s": -14 * 3600 - 1},
    ],
)
def test_implausible_window_values_are_rejected(fields):
    with pytest.raises(ValidationError):
        ForcedDisconnectWindow(**fields)
