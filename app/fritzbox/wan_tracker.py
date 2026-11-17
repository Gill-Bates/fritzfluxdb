#!/usr/bin/env python3
#
# app/fritzbox/wan_tracker.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""WAN interruption state machine: samples in, lifecycle events out.

Pure: no I/O, logging or clock; every time comes from the injected samples.
Used by one task only, so there is no locking.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.fritzbox.wan_cause import (
    ERROR_FORCED_DISCONNECT,
    START_JITTER_S,
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
)

ROUTER_EVERY_N = 6

_UNKNOWN = WanCauseResult(cause=CauseClass.UNKNOWN, confidence=Confidence.UNKNOWN)


class TrackerEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["opened", "refined", "closed"]
    result: WanCauseResult
    started_at: datetime
    ended_at: datetime | None = None
    # None: the outage lies somewhere in an observation gap, so started_at is only its lower bound.
    duration_s: int | None = None
    detected_by: Detection
    # Time of the sample that produced the event; nothing is backdated.
    observed_at: datetime


@dataclass
class _Base:
    """Counter baseline: start time derived from a reliable connected sample."""

    start: datetime
    uptime_s: int
    observed_at: datetime
    monotonic_s: float
    last_good_at: datetime | None = None


@dataclass
class _Open:
    started_at: datetime
    started_mono_s: float
    detected_by: Detection
    last_good_at: datetime | None
    previous_lifetime_s: float | None
    dsl_samples: list[bool | None] = field(default_factory=list)
    ppp_down_seen: bool = False
    forced_error_seen: bool = False
    router_reset_seen: bool = False
    router_shift_seen: bool = False
    last_router_reading: _Base | None = None
    last_result: WanCauseResult = _UNKNOWN


def _shift_s(previous: datetime, new: datetime) -> float:
    return (new - previous).total_seconds()


class WanInterruptionTracker:
    def __init__(
        self, link_type: str, start_jitter_s: float = START_JITTER_S, poll_interval_s: float = 5.0
    ) -> None:
        self._is_dsl = link_type == "DSL"
        self._max_gap_s = max_observation_gap_s(poll_interval_s)
        self._jitter = start_jitter_s
        self._link: _Base | None = None
        self._router: _Base | None = None
        self._open: _Open | None = None
        self._unreachable = False
        self._last_mono_s: float | None = None
        self._last_error: str | None = None
        self._last_connected: WanSample | None = None

    @property
    def needs_attention(self) -> bool:
        return self._open is not None or self._unreachable

    def note_unreachable(self) -> None:
        # Never opens an interruption: an unreachable box says nothing about the WAN.
        self._unreachable = True

    def reset_link_baseline(self) -> None:
        """Forget link counters after the active WAN service changed; an open interruption stays open."""
        self._link = None
        self._last_connected = None
        self._last_error = None

    def wants_router_uptime(self, partial: WanSample, sample_index: int) -> bool:
        if (
            partial.connected is not True or self.needs_attention or self._router is None
            or self._is_new_forced_error(partial)
        ):
            return True
        if sample_index % ROUTER_EVERY_N == 0:
            return True
        return self._link_check(partial)[0] is not None

    # -- baselines ---------------------------------------------------------

    def _link_reading(self, sample: WanSample) -> _Base | None:
        if sample.connected is not True or not sample.start_reliable or sample.link_uptime_s is None:
            return None
        start = derive_start_time(sample.observed_at, sample.link_uptime_s)
        if start is None:
            return None
        return _Base(start, sample.link_uptime_s, sample.observed_at, sample.monotonic_s)

    def _router_reading(self, sample: WanSample) -> _Base | None:
        if (
            sample.router_uptime_s is None
            or sample.router_observed_at is None
            or sample.router_monotonic_s is None
            or not sample.router_reliable
        ):
            return None
        start = derive_start_time(sample.router_observed_at, sample.router_uptime_s)
        if start is None:
            return None
        return _Base(start, sample.router_uptime_s, sample.router_observed_at, sample.router_monotonic_s)

    def _compare(self, base: _Base | None, reading: _Base | None) -> Detection | None:
        if base is None or reading is None:
            return None
        gap = (reading.observed_at - base.observed_at).total_seconds()
        stepped = clock_stepped(base.observed_at, base.monotonic_s, reading.observed_at, reading.monotonic_s)
        # A host clock step invalidates start comparisons; a decreased counter stays valid.
        return detect_counter_reset(
            base.uptime_s, None if stepped else base.start, reading.uptime_s, reading.start, gap, self._jitter
        )

    def _link_check(self, sample: WanSample) -> tuple[Detection | None, _Base | None]:
        reading = self._link_reading(sample)
        return self._compare(self._link, reading), reading

    def _router_check(self, sample: WanSample) -> tuple[Detection | None, _Base | None]:
        reading = self._router_reading(sample)
        return self._compare(self._router, reading), reading

    # -- classification ----------------------------------------------------

    def _result(self, state: _Open, window: ForcedDisconnectWindow | None) -> WanCauseResult:
        window_match = None
        if self._is_dsl and state.last_good_at is not None:
            window_match = in_forced_window(
                window,
                state.last_good_at,
                state.started_at,
                state.previous_lifetime_s,
                max_interval_s=self._max_gap_s,
            )
        extra: list[EvidenceKind] = []
        if state.ppp_down_seen:
            extra.append(EvidenceKind.PPP_DOWN_SEEN)
        return classify_interruption(
            WanEvidence(
                link_is_dsl=self._is_dsl,
                router_reset=state.router_reset_seen,
                router_shift=state.router_shift_seen and not state.router_reset_seen,
                dsl_state=aggregate_dsl_state(state.dsl_samples) if self._is_dsl else DslState.UNKNOWN,
                ppp_down_seen=state.ppp_down_seen,
                window_match=window_match,
                router_reports_forced_disconnect=state.forced_error_seen,
                extra=tuple(extra),
            )
        )

    def _record(self, state: _Open, sample: WanSample, down: bool) -> None:
        state.dsl_samples.append(sample.dsl_sync)
        if down and sample.ppp_active:
            state.ppp_down_seen = True
        if self._is_new_forced_error(sample):
            state.forced_error_seen = True
        self._record_router(state, sample)

    def is_new_forced_error(self, last_error: str | None) -> bool:
        # A code already present at startup or left over from an earlier event is not new evidence.
        return (
            last_error == ERROR_FORCED_DISCONNECT
            and self._last_error is not None
            and self._last_error != ERROR_FORCED_DISCONNECT
        )

    def _is_new_forced_error(self, sample: WanSample) -> bool:
        return self.is_new_forced_error(sample.last_error)

    def _record_router(self, state: _Open, sample: WanSample) -> _Base | None:
        detection, reading = self._router_check(sample)
        if reading is not None:
            state.last_router_reading = reading
        if detection is Detection.UPTIME_RESET:
            state.router_reset_seen = True
        elif detection is Detection.START_SHIFT:
            state.router_shift_seen = True
        return reading

    def _refine(self, state: _Open, window: ForcedDisconnectWindow | None, sample: WanSample) -> list[TrackerEvent]:
        result = self._result(state, window)
        if (result.cause, result.confidence) == (state.last_result.cause, state.last_result.confidence):
            return []
        state.last_result = result
        return [
            TrackerEvent(
                kind="refined",
                result=result,
                started_at=state.started_at,
                detected_by=state.detected_by,
                observed_at=sample.observed_at,
            )
        ]

    # -- main entry --------------------------------------------------------

    def observe(self, sample: WanSample, window: ForcedDisconnectWindow | None) -> list[TrackerEvent]:
        # Ordered by the monotonic clock: a wall-clock step back (NTP) must not drop live samples.
        if self._last_mono_s is not None and sample.monotonic_s <= self._last_mono_s:
            return []
        self._last_mono_s = sample.monotonic_s
        try:
            if self._open is not None:
                return self._observe_open(sample, window)
            if sample.connected is False:
                return self._open_interruption(sample, window)
            if sample.connected is True:
                return self._observe_idle_connected(sample)
            return []
        finally:
            if sample.connected is True:
                self._last_connected = sample
            if sample.last_error is not None and sample.connected is not None:
                self._last_error = sample.last_error

    def _open_interruption(self, sample: WanSample, window: ForcedDisconnectWindow | None) -> list[TrackerEvent]:
        link = self._link
        state = _Open(
            started_at=sample.observed_at,
            started_mono_s=sample.monotonic_s,
            detected_by=Detection.STATUS_DOWN,
            last_good_at=link.last_good_at if link else None,
            previous_lifetime_s=_shift_s(link.start, sample.observed_at) if link else None,
        )
        self._open = state
        opened = TrackerEvent(
            kind="opened",
            result=_UNKNOWN,
            started_at=state.started_at,
            detected_by=state.detected_by,
            observed_at=sample.observed_at,
        )
        # The opening sample is evaluated like any later down sample.
        self._record(state, sample, down=True)
        return [opened, *self._refine(state, window, sample)]

    def _observe_open(self, sample: WanSample, window: ForcedDisconnectWindow | None) -> list[TrackerEvent]:
        state = self._open
        assert state is not None
        if sample.connected is not True:
            self._record(state, sample, down=sample.connected is False)
            return self._refine(state, window, sample)
        router_reading = self._record_router(state, sample)
        if self._is_new_forced_error(sample):
            state.forced_error_seen = True
        link_reading = self._link_reading(sample)
        result = self._result(state, window)
        event = self._closed_event(sample, state.started_mono_s, link_reading, state.detected_by, result)
        self._open = None
        self._unreachable = False
        # Consume a reset seen during the outage even when the closing sample lacks router uptime.
        self._link = self._with_good(link_reading, sample)
        self._router = router_reading or state.last_router_reading or self._router
        return [event]

    @staticmethod
    def _with_good(reading: _Base | None, sample: WanSample) -> _Base | None:
        if reading is not None:
            reading.last_good_at = sample.observed_at
        return reading

    def _observe_idle_connected(self, sample: WanSample) -> list[TrackerEvent]:
        link_detection, link_reading = self._link_check(sample)
        router_detection, router_reading = self._router_check(sample)
        previous_link, previous_router = self._link, self._router
        events: list[TrackerEvent] = []
        clock_pair = (
            link_detection is Detection.START_SHIFT
            and router_detection is Detection.START_SHIFT
            and previous_link is not None
            and previous_router is not None
            and link_reading is not None
            and router_reading is not None
            and is_clock_step_pair(
                link_detection,
                router_detection,
                _shift_s(previous_link.start, link_reading.start),
                _shift_s(previous_router.start, router_reading.start),
                False,
                self._jitter,
            )
        )
        if self._is_new_forced_error(sample) or (
            (link_detection is not None or router_detection is not None) and not clock_pair
        ):
            events.append(self._close_at_once(sample, link_detection, link_reading, router_detection))
        self._unreachable = False
        self._advance_baselines(sample, link_reading, router_reading)
        return events

    def _advance_baselines(
        self, sample: WanSample, link_reading: _Base | None, router_reading: _Base | None
    ) -> None:
        if link_reading is not None:
            self._link = self._with_good(link_reading, sample)
        elif self._link is not None:
            # An unreliable sample keeps the start but still proves the link was up.
            self._link.last_good_at = sample.observed_at
        if router_reading is not None:
            self._router = router_reading

    def _close_at_once(
        self,
        sample: WanSample,
        link_detection: Detection | None,
        link_reading: _Base | None,
        router_detection: Detection | None,
    ) -> TrackerEvent:
        """An event missed between polls: the duration is an observation bound, not a measured outage."""
        if self._last_connected is not None:
            started_mono_s = self._last_connected.monotonic_s
        elif self._router is not None:
            started_mono_s = self._router.monotonic_s
        else:
            started_mono_s = sample.monotonic_s
        detections = (link_detection, router_detection)
        if self._is_new_forced_error(sample):
            detected_by = Detection.CONNECTION_ERROR
        else:
            detected_by = Detection.UPTIME_RESET if Detection.UPTIME_RESET in detections else Detection.START_SHIFT
        extra: list[EvidenceKind] = []
        if link_detection is Detection.UPTIME_RESET:
            extra.append(EvidenceKind.LINK_UPTIME_RESET)
        elif link_detection is Detection.START_SHIFT:
            extra.append(EvidenceKind.LINK_START_SHIFT)
        result = classify_interruption(
            WanEvidence(
                link_is_dsl=self._is_dsl,
                router_reset=router_detection is Detection.UPTIME_RESET,
                router_shift=router_detection is Detection.START_SHIFT,
                dsl_state=aggregate_dsl_state([sample.dsl_sync]) if self._is_dsl and sample.dsl_sync is not None
                else DslState.UNKNOWN,
                router_reports_forced_disconnect=self._is_new_forced_error(sample),
                extra=tuple(extra),
            )
        )
        return self._closed_event(
            sample, started_mono_s, link_reading if link_detection is not None else None, detected_by, result,
            duration_known=sample.monotonic_s - started_mono_s <= self._max_gap_s,
        )

    @staticmethod
    def _closed_event(
        sample: WanSample, started_mono_s: float, link_reading: _Base | None,
        detected_by: Detection, result: WanCauseResult, duration_known: bool = True,
    ) -> TrackerEvent:
        ended_mono_s = sample.monotonic_s
        if link_reading is not None:
            link_start_mono_s = link_reading.monotonic_s - link_reading.uptime_s
            if started_mono_s <= link_start_mono_s < ended_mono_s:
                ended_mono_s = link_start_mono_s
        duration_s = ended_mono_s - started_mono_s
        # Anchor both ends to the closing clock, so clock corrections cannot invert the interval.
        ended_at = sample.observed_at - timedelta(seconds=sample.monotonic_s - ended_mono_s)
        started_at = ended_at - timedelta(seconds=duration_s)
        return TrackerEvent(
            kind="closed",
            result=result,
            started_at=started_at,
            ended_at=ended_at,
            duration_s=round(duration_s) if duration_known else None,
            detected_by=detected_by,
            observed_at=sample.observed_at,
        )
