#!/usr/bin/env python3
#
# app/fritzbox/wan_cause.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Pure WAN interruption cause logic: start times, correlation, classification.

No I/O, logging or clock access, so every rule is testable on its own.
"""
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Reasoned bounds, not measured values; each covers the clock and rounding error of its source.
START_JITTER_S = 5
MAX_RTT_S = 2
CLOCK_STEP_S = 2
DRIFT_PPM = 100e-6
HOUR_TOLERANCE_S = 300
MIN_SESSION_FOR_HOUR_S = 43200
LIFETIME_S = 86400
LIFETIME_TOLERANCE_S = 900
MAX_UTC_OFFSET_S = 14 * 3600

# \A/\Z instead of ^/$: "$" would also accept a trailing newline, also for callers using match().
ERROR_CODE_PATTERN = re.compile(r"\AERROR_[A-Z_]{1,40}\Z")
ERROR_FORCED_DISCONNECT = "ERROR_FORCED_DISCONNECT"


class CauseClass(StrEnum):
    ROUTER_RESTART = "router_restart"
    DSL_SYNC_LOSS = "dsl_sync_loss"
    PLANNED_DISCONNECT = "planned_disconnect"
    PPP_INTERRUPTION = "ppp_interruption"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    UNKNOWN = "unknown"
    PROBABLE = "probable"
    CONFIRMED = "confirmed"


class DslState(StrEnum):
    SYNC_LOST = "sync_lost"
    SYNC_OK = "sync_ok"
    UNKNOWN = "unknown"


class Detection(StrEnum):
    STATUS_DOWN = "status_down"
    CONNECTION_ERROR = "connection_error"
    START_SHIFT = "start_shift"
    UPTIME_RESET = "uptime_reset"


class EvidenceKind(StrEnum):
    ROUTER_UPTIME_RESET = "router_uptime_reset"
    ROUTER_START_SHIFT = "router_start_shift"
    DSL_SYNC_LOST = "dsl_sync_lost"
    DSL_SYNC_OK = "dsl_sync_ok"
    PPP_DOWN_SEEN = "ppp_down_seen"
    LINK_START_SHIFT = "link_start_shift"
    LINK_UPTIME_RESET = "link_uptime_reset"
    LIFETIME_IN_WINDOW = "lifetime_in_window"
    HOUR_IN_WINDOW = "hour_in_window"
    ROUTER_REPORTS_FORCED_DISCONNECT = "router_reports_forced_disconnect"


def _require_aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("datetime must be timezone-aware")
    return value


class WanSample(BaseModel):
    """One observation of the WAN state; times are midpoints of request and response."""

    model_config = ConfigDict(frozen=True)

    observed_at: datetime
    monotonic_s: float
    connected: bool | None = None
    link_uptime_s: int | None = None
    start_reliable: bool = True
    dsl_sync: bool | None = None
    router_uptime_s: int | None = None
    router_observed_at: datetime | None = None
    router_monotonic_s: float | None = None
    router_reliable: bool = True
    last_error: str | None = None
    # False when the polled WAN service is not PPP, so a down state is no PPP evidence.
    ppp_active: bool = True

    @field_validator("observed_at", "router_observed_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return _require_aware(value)

    @field_validator("link_uptime_s", "router_uptime_s")
    @classmethod
    def _non_negative(cls, value: int | None) -> int | None:
        # A negative counter is a broken reading, not a reset.
        return None if value is not None and value < 0 else value

    @field_validator("last_error")
    @classmethod
    def _error_code_only(cls, value: str | None) -> str | None:
        # Free text from the box is dropped; only fixed codes may influence decisions.
        return value if value is not None and ERROR_CODE_PATTERN.fullmatch(value) else None


class ForcedDisconnectWindow(BaseModel):
    """Where a box-initiated daily disconnect may fall; the hour window itself is exact."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["hour_of_day", "lifetime"]
    hour: int | None = Field(default=None, ge=0, le=23)
    utc_offset_s: int | None = Field(default=None, ge=-MAX_UTC_OFFSET_S, le=MAX_UTC_OFFSET_S)
    lifetime_s: int = Field(default=LIFETIME_S, gt=0)
    tolerance_s: int = Field(default=LIFETIME_TOLERANCE_S, ge=0)

    @model_validator(mode="after")
    def _hour_mode_complete(self) -> "ForcedDisconnectWindow":
        if self.kind == "hour_of_day" and (self.hour is None or self.utc_offset_s is None):
            raise ValueError("hour_of_day needs hour 0..23 and utc_offset_s")
        if self.tolerance_s >= self.lifetime_s:
            raise ValueError("tolerance_s must be smaller than lifetime_s")
        return self


class WanEvidence(BaseModel):
    """Facts gathered for one interruption; the classifier reads nothing else."""

    model_config = ConfigDict(frozen=True)

    link_is_dsl: bool = False
    router_reset: bool = False
    router_shift: bool = False
    dsl_state: DslState = DslState.UNKNOWN
    ppp_down_seen: bool = False
    window_match: EvidenceKind | None = None
    router_reports_forced_disconnect: bool = False
    extra: tuple[EvidenceKind, ...] = ()

    @field_validator("window_match")
    @classmethod
    def _window_kind(cls, value: EvidenceKind | None) -> EvidenceKind | None:
        if value not in (None, EvidenceKind.HOUR_IN_WINDOW, EvidenceKind.LIFETIME_IN_WINDOW):
            raise ValueError("window_match must be a window evidence kind")
        return value


class WanCauseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    cause: CauseClass
    confidence: Confidence
    evidence: tuple[EvidenceKind, ...] = ()

    @model_validator(mode="after")
    def _conservative(self) -> "WanCauseResult":
        # Guards the "never overclaim" rules even if the classifier is changed later.
        if self.cause is CauseClass.PLANNED_DISCONNECT and self.confidence is not Confidence.PROBABLE:
            raise ValueError("planned_disconnect is never more than probable")
        if (self.cause is CauseClass.UNKNOWN) != (self.confidence is Confidence.UNKNOWN):
            raise ValueError("unknown cause and unknown confidence belong together")
        if (
            self.cause is CauseClass.ROUTER_RESTART
            and self.confidence is Confidence.CONFIRMED
            and EvidenceKind.ROUTER_UPTIME_RESET not in self.evidence
        ):
            raise ValueError("confirmed router_restart needs router_uptime_reset")
        return self


def derive_start_time(observed_at: datetime, uptime_s: int | None) -> datetime | None:
    """Start time of a counter: measurement time minus uptime."""
    if uptime_s is None or uptime_s < 0:
        return None
    _require_aware(observed_at)
    return observed_at - timedelta(seconds=uptime_s)


def start_shifted_forward(
    previous_start: datetime | None,
    new_start: datetime | None,
    gap_s: float,
    tolerance_s: float = START_JITTER_S,
    drift_ppm: float = DRIFT_PPM,
) -> bool:
    """True if the start moved forward by more than jitter plus clock drift over the gap."""
    if previous_start is None or new_start is None:
        return False
    shift_s = (new_start - previous_start).total_seconds()
    return shift_s > tolerance_s + max(gap_s, 0.0) * drift_ppm


def clock_stepped(
    prev_wall: datetime,
    prev_mono: float,
    wall: datetime,
    mono: float,
    tolerance_s: float = CLOCK_STEP_S,
) -> bool:
    """True if the wall clock jumped relative to the monotonic clock (NTP step, suspend)."""
    wall_delta = (wall - prev_wall).total_seconds()
    return abs(wall_delta - (mono - prev_mono)) > tolerance_s


def detect_counter_reset(
    previous_uptime_s: int | None,
    previous_start: datetime | None,
    uptime_s: int | None,
    start: datetime | None,
    gap_s: float,
    tolerance_s: float = START_JITTER_S,
) -> Detection | None:
    """A decreased counter is a reset; a start shifted forward with a growing counter is a hidden one."""
    # The counter decrease needs no wall clock, so an underivable start must not hide it.
    if previous_uptime_s is not None and uptime_s is not None and uptime_s < previous_uptime_s:
        return Detection.UPTIME_RESET
    if uptime_s is None or start is None:
        return None
    if start_shifted_forward(previous_start, start, gap_s, tolerance_s):
        return Detection.START_SHIFT
    return None


def is_clock_step_pair(
    link_detection: Detection | None,
    router_detection: Detection | None,
    link_shift_s: float,
    router_shift_s: float,
    status_down_seen: bool,
    tolerance_s: float = START_JITTER_S,
) -> bool:
    """Equal start shifts on both counters without any reset or outage point to a host clock step."""
    if status_down_seen:
        return False
    if link_detection is not Detection.START_SHIFT or router_detection is not Detection.START_SHIFT:
        return False
    # A real reboot makes the link start lag the router start by the sync time.
    return abs(link_shift_s - router_shift_s) <= tolerance_s


def aggregate_dsl_state(samples: Sequence[bool | None]) -> DslState:
    """Any lost-sync reading wins; sync_ok needs every sample of the interruption to be readable.

    A failed reading during an outage may hide the sync loss, so it never counts as synchronized.
    """
    if any(s is False for s in samples):
        return DslState.SYNC_LOST
    if not samples or any(s is None for s in samples):
        return DslState.UNKNOWN
    return DslState.SYNC_OK


def max_observation_gap_s(poll_interval_s: float) -> float:
    """Longest last-good-to-first-down interval that still counts as one observed transition."""
    return poll_interval_s + MAX_RTT_S + START_JITTER_S


def in_forced_window(
    window: ForcedDisconnectWindow | None,
    interval_start: datetime,
    interval_end: datetime,
    previous_lifetime_s: float | None,
    *,
    max_interval_s: float,
) -> EvidenceKind | None:
    """Window evidence for the interval between last good and first down sample.

    `max_interval_s` comes from `max_observation_gap_s()`; a longer or inverted interval was not
    observed reliably and yields no evidence.
    """
    if window is None or previous_lifetime_s is None:
        return None
    _require_aware(interval_start)
    _require_aware(interval_end)
    interval_s = (interval_end - interval_start).total_seconds()
    if interval_s < 0 or interval_s > max_interval_s:
        return None
    if window.kind == "lifetime":
        if abs(previous_lifetime_s - window.lifetime_s) <= window.tolerance_s:
            return EvidenceKind.LIFETIME_IN_WINDOW
        return None
    if previous_lifetime_s < MIN_SESSION_FOR_HOUR_S:
        return None
    # The only widening is applied to the interval, never to the exact hour window.
    offset = timedelta(seconds=window.utc_offset_s or 0)
    start = interval_start.astimezone(UTC) + offset - timedelta(seconds=HOUR_TOLERANCE_S)
    end = interval_end.astimezone(UTC) + offset + timedelta(seconds=HOUR_TOLERANCE_S)
    day = start.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    while day <= end:
        window_start = day + timedelta(hours=window.hour or 0)
        if window_start <= end and start <= window_start + timedelta(hours=1):
            return EvidenceKind.HOUR_IN_WINDOW
        day += timedelta(days=1)
    return None


def classify_interruption(evidence: WanEvidence) -> WanCauseResult:
    """Conservative decision table; the first matching row wins, otherwise unknown."""
    if evidence.router_reset:
        return WanCauseResult(
            cause=CauseClass.ROUTER_RESTART,
            confidence=Confidence.CONFIRMED,
            evidence=(EvidenceKind.ROUTER_UPTIME_RESET,),
        )
    if evidence.link_is_dsl and evidence.dsl_state is DslState.SYNC_LOST:
        return WanCauseResult(
            cause=CauseClass.DSL_SYNC_LOSS,
            confidence=Confidence.CONFIRMED,
            evidence=(EvidenceKind.DSL_SYNC_LOST,),
        )
    if evidence.router_shift:
        return WanCauseResult(
            cause=CauseClass.ROUTER_RESTART,
            confidence=Confidence.PROBABLE,
            evidence=(EvidenceKind.ROUTER_START_SHIFT,),
        )
    ppp_after_sync = (
        evidence.link_is_dsl and evidence.dsl_state is DslState.SYNC_OK and evidence.ppp_down_seen
    )
    if evidence.router_reports_forced_disconnect and not ppp_after_sync:
        return WanCauseResult(
            cause=CauseClass.PLANNED_DISCONNECT,
            confidence=Confidence.PROBABLE,
            evidence=(EvidenceKind.ROUTER_REPORTS_FORCED_DISCONNECT,),
        )
    if ppp_after_sync:
        codes = [EvidenceKind.DSL_SYNC_OK, EvidenceKind.PPP_DOWN_SEEN]
        if evidence.window_match is not None:
            codes.append(evidence.window_match)
        if evidence.router_reports_forced_disconnect:
            codes.append(EvidenceKind.ROUTER_REPORTS_FORCED_DISCONNECT)
        planned = evidence.window_match is not None or evidence.router_reports_forced_disconnect
        return WanCauseResult(
            cause=CauseClass.PLANNED_DISCONNECT if planned else CauseClass.PPP_INTERRUPTION,
            confidence=Confidence.PROBABLE,
            evidence=tuple(codes),
        )
    return WanCauseResult(
        cause=CauseClass.UNKNOWN,
        confidence=Confidence.UNKNOWN,
        evidence=evidence.extra,
    )
