#!/usr/bin/env python3
#
# app/fritzbox/wan_watch.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Polls structured WAN/DSL state over TR-064 and feeds the interruption tracker.

Everything is injected (session, link type, clock); the watcher never logs in,
reconnects or closes. No router log text is read or kept.
"""
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from app.fritzbox.model import FritzBoxLinkTypes
from app.fritzbox.tr064.exceptions import (
    TR064ActionNotFound,
    TR064ClientClosed,
    TR064Error,
    TR064ServiceNotFound,
)
from app.fritzbox.wan_cause import (
    ERROR_CODE_PATTERN,
    MAX_RTT_S,
    MAX_UTC_OFFSET_S,
    ForcedDisconnectWindow,
    WanSample,
)
from app.fritzbox.wan_connection import (
    METADATA_REFRESH_S,
    resolve_status_call,
)
from app.utils.log import get_logger

log = get_logger()

DSL_DIAG_SERVICE = "WANDSLInterfaceConfig"
DSL_DIAG_CANDIDATES = ("X_AVM-DE_GetDSLDiagnoseInfo", "X_AVM_DE_GetDSLDiagnoseInfo")
PREVENTION_CANDIDATES = ("X_AVM_DE_GetAutoDisconnectTimeSpan", "X_AVM-DE_GetAutoDisconnectTimeSpan")
DEVICE_CALL = ("DeviceInfo", "GetInfo")
TIME_CALL = ("Time:1", "GetInfo")
PREVENTION_REFRESH_S = 900
WINDOW_RETRY_S = 60
DISCOVERY_CYCLES = 3

_STATUS_UP = {"connected"}
_STATUS_DOWN = {
    "unconfigured", "connecting", "authenticating",
    "pendingdisconnect", "disconnecting", "disconnected",
}
_FALLBACK_NOTICE = (
    "FRITZ!Box does not report a usable disconnect time; using the 24 hour session length as window"
)


class WanWatchUnavailable(Exception):
    """The session is closed or every call failed; the caller backs off."""


class _SessionSwapped(Exception):
    """The shared session was replaced mid-cycle; the cycle is dropped."""


def parse_connection_status(raw: Any) -> bool | None:
    """Single source for the observer and the watcher: case/whitespace tolerant; unknown is never down."""
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    if value in _STATUS_UP:
        return True
    if value in _STATUS_DOWN:
        return False
    return None


def parse_uptime(raw: Any) -> int | None:
    """Non-negative whole seconds; bool, floats and free text are rejected."""
    if isinstance(raw, bool):
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def parse_last_error(raw: Any) -> str | None:
    """Keeps only a fixed ERROR_* code; free text is dropped."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value if ERROR_CODE_PATTERN.match(value) else None


def parse_bool_flag(raw: Any) -> bool | None:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return {0: False, 1: True}.get(raw)
    if isinstance(raw, str):
        return {"0": False, "1": True, "false": False, "true": True}.get(raw.strip().lower())
    return None


def parse_dsl_sync(raw: Any) -> bool | None:
    return parse_bool_flag(raw)


def parse_box_utc_offset(local_str: Any, utc_before: datetime, utc_after: datetime) -> int | None:
    """Box UTC offset in seconds via the midpoint technique; None unless plausible."""
    if not isinstance(local_str, str) or len(local_str) < 19:
        return None
    try:
        local = datetime.strptime(local_str[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    mid = utc_before + (utc_after - utc_before) / 2
    raw_s = (local - mid.astimezone(UTC).replace(tzinfo=None)).total_seconds()
    offset = round(raw_s / 60) * 60
    if abs(offset) > MAX_UTC_OFFSET_S or offset % 900 != 0:
        return None
    return offset


def parse_prevention(result: Mapping[str, Any] | None, utc_offset_s: int | None) -> ForcedDisconnectWindow:
    """Hour window only when enabled, valid and the offset was measured; else the lifetime fallback."""
    if result:
        enabled = parse_bool_flag(result.get("NewX_AVM-DE_DisconnectPreventionEnable"))
        hour = parse_uptime(result.get("NewX_AVM-DE_DisconnectPreventionHour"))
        if enabled is True and hour is not None and hour <= 23 and utc_offset_s is not None:
            return ForcedDisconnectWindow(kind="hour_of_day", hour=hour, utc_offset_s=utc_offset_s)
    return ForcedDisconnectWindow(kind="lifetime")


def _scpd_has_action(session: Any, service: str, name: str) -> bool | None:
    """True/False per presence; None only when the SCPD cannot be loaded."""
    try:
        return session.has_action(service, name)
    except TR064Error:
        return None


def resolve_action(session: Any, service: str, candidates: Sequence[str]) -> str | None:
    """First candidate present in the SCPD; the first one when introspection is impossible."""
    checks = [(name, _scpd_has_action(session, service, name)) for name in candidates]
    for name, present in checks:
        if present:
            return name
    if all(present is None for _, present in checks):
        return candidates[0]
    return None


def _is_unsupported(exc: Exception) -> bool:
    return isinstance(exc, TR064ActionNotFound | TR064ServiceNotFound)


class _Signal:
    """Availability of one optional signal; only discovery errors disable it for good."""

    def __init__(self) -> None:
        self.disabled = False
        self.succeeded = False


class WanWatcher:
    def __init__(
        self,
        session_provider: Callable[[], Any | None],
        link_type_provider: Callable[[], str | None],
        is_auth_failure: Callable[[Exception], bool],
        log_once: Callable[[str, str], None] | None = None,
        tracker_factory: Callable[[str], Any] | None = None,
        poll_interval_s: float = 5.0,
        status_call_provider: Callable[[], tuple[str, str] | None] | None = None,
    ) -> None:
        self._session_provider = session_provider
        self._link_type_provider = link_type_provider
        self._is_auth_failure = is_auth_failure
        self._log_once = log_once or (lambda _key, message: log.info(message))
        self._poll_interval_s = poll_interval_s
        self._status_call_provider = status_call_provider
        self._status_session: Any = None
        self._status_call: tuple[str, str] | None = None
        self._status_refresh_after = 0.0
        self._tracker_factory = tracker_factory or self._default_tracker
        self._link_type: str | None = None
        self._tracker: Any = None
        self._window: ForcedDisconnectWindow | None = None
        self._window_refreshed_mono: float | None = None
        self._window_retry_after = 0.0
        self._resolved: tuple[Any, str | None, str | None] | None = None
        self._prevention_service: str | None = None
        self._diag = _Signal()
        self._prevention = _Signal()
        self._cycle = 0
        self._healthy_cycles = 0
        self._announced: set[str] = set()

    def _default_tracker(self, link_type: str) -> Any:
        from app.fritzbox.wan_tracker import WanInterruptionTracker
        return WanInterruptionTracker(link_type, poll_interval_s=self._poll_interval_s)

    def _announce(self, key: str, message: str) -> None:
        if key not in self._announced:
            self._announced.add(key)
            self._log_once(key, message)

    def _in_discovery(self, signal: _Signal) -> bool:
        return not signal.succeeded and self._healthy_cycles <= DISCOVERY_CYCLES

    def _call(self, session: Any, counters: dict[str, int], service: str, action: str,
              now_fn: Callable[[], datetime], monotonic_fn: Callable[[], float]
              ) -> tuple[Mapping[str, Any] | None, datetime, float, float, Exception | None]:
        """Timed call; returns result, midpoint wall/monotonic time, RTT and the error."""
        wall_before, mono_before = now_fn(), monotonic_fn()
        try:
            result = session.call_action(service, action)
            error: Exception | None = None
        except Exception as exc:
            result, error = None, exc
        wall_after, mono_after = now_fn(), monotonic_fn()
        if error is not None:
            # A closed client is no statement about the box, even if shutdown left it published.
            if self._session_provider() is not session:
                log.debug("WAN watch dropped a cycle: session was replaced")
                raise _SessionSwapped from None
            if isinstance(error, TR064ClientClosed):
                raise error
            log.debug("WAN watch call failed: %s", error)
            if self._is_auth_failure(error):
                self._log_once("wan_watch_auth", "WAN watch request was rejected (HTTP 401)")
            else:
                counters["attempted"] += 1
                counters["failed"] += 1
        else:
            # A late success of a replaced session must not reach the tracker either.
            if self._session_provider() is not session:
                log.debug("WAN watch dropped a cycle: session was replaced")
                raise _SessionSwapped from None
            counters["attempted"] += 1
        wall_mid = wall_before + (wall_after - wall_before) / 2
        mono_mid = mono_before + (mono_after - mono_before) / 2
        return (result if isinstance(result, Mapping) else None), wall_mid, mono_mid, mono_after - mono_before, error

    def _signal_call(self, session: Any, counters: dict[str, int], signal: _Signal, name: str,
                     service: str, action: str | None, now_fn: Callable[[], datetime],
                     monotonic_fn: Callable[[], float]) -> tuple[Mapping[str, Any] | None, bool]:
        """Optional signal call; returns result and whether a transient error occurred."""
        if signal.disabled or action is None:
            return None, False
        result, _, _, _, error = self._call(session, counters, service, action, now_fn, monotonic_fn)
        if error is None:
            signal.succeeded = True
            return result, False
        if _is_unsupported(error) and self._in_discovery(signal):
            signal.disabled = True
            self._announce(f"wan_watch_{name}_off", f"WAN watch signal {name} is not offered by this FRITZ!Box")
        elif not self._is_auth_failure(error):
            self._log_once(f"wan_watch_{name}_err", f"WAN watch signal {name} failed: {type(error).__name__}")
        return None, True

    def _resolve(self, session: Any, prevention_service: str | None) -> tuple[str | None, str | None]:
        if self._resolved is not None and self._resolved[0] is session and (
            self._prevention_service != prevention_service
        ):
            # Only the active PPP service changed: its disconnect window is not the old one's.
            self._prevention = _Signal()
            self._window = None
            self._window_refreshed_mono = None
            self._window_retry_after = 0.0
            self._resolved = None
        if self._resolved is None or self._resolved[0] is not session:
            if self._resolved is not None:
                self._diag = _Signal()
                self._prevention = _Signal()
                self._window = None
                self._window_refreshed_mono = None
                self._window_retry_after = 0.0
                self._cycle = 0
                self._announced.clear()
                self._healthy_cycles = 0
            diag = resolve_action(session, DSL_DIAG_SERVICE, DSL_DIAG_CANDIDATES)
            prevention = (
                resolve_action(session, prevention_service, PREVENTION_CANDIDATES) if prevention_service else None
            )
            self._resolved = (session, diag, prevention)
            self._prevention_service = prevention_service
            self._log_once(
                "wan_watch_signals",
                f"WAN watch signals: dsl_diagnose={'yes' if diag else 'no'}, "
                f"disconnect_window={'yes' if prevention else 'no'}",
            )
        return self._resolved[1], self._resolved[2]

    def _refresh_window(self, session: Any, counters: dict[str, int], service: str, prevention: str | None,
                        now_fn: Callable[[], datetime], monotonic_fn: Callable[[], float]) -> None:
        mono_now = monotonic_fn()
        if mono_now < self._window_retry_after:
            return
        if (self._window is not None and self._window_refreshed_mono is not None
                and mono_now - self._window_refreshed_mono < PREVENTION_REFRESH_S):
            return
        before = now_fn()
        time_result, _, _, _, time_error = self._call(session, counters, *TIME_CALL, now_fn, monotonic_fn)
        after = now_fn()
        offset = parse_box_utc_offset((time_result or {}).get("NewCurrentLocalTime"), before, after)
        result, transient = self._signal_call(
            session, counters, self._prevention, "disconnect_window", service,
            prevention, now_fn, monotonic_fn,
        )
        # A one-off Time:1 failure must not pin the lifetime fallback for a whole refresh interval.
        time_transient = (
            time_error is not None and not _is_unsupported(time_error) and not self._is_auth_failure(time_error)
        )
        if transient or time_transient:
            self._window_retry_after = mono_now + WINDOW_RETRY_S
            if self._window is None:
                self._window = ForcedDisconnectWindow(kind="lifetime")
            return
        window = parse_prevention(result, offset)
        if window.kind == "lifetime":
            self._announce("wan_watch_fallback", _FALLBACK_NOTICE)
        self._window = window
        self._window_retry_after = 0.0
        self._window_refreshed_mono = mono_now

    def poll_once(self, now_fn: Callable[[], datetime], monotonic_fn: Callable[[], float]) -> list[Any]:
        session = self._session_provider()
        link_type = self._link_type_provider()
        if session is None or link_type is None:
            return []
        try:
            return self._poll(session, link_type, now_fn, monotonic_fn)
        except _SessionSwapped:
            return []
        except TR064ClientClosed as exc:
            if self._session_provider() is not session:
                return []
            self._tracker.note_unreachable()
            raise WanWatchUnavailable("published TR-064 session is closed") from exc

    def _poll(self, session: Any, link_type: str, now_fn: Callable[[], datetime],
              monotonic_fn: Callable[[], float]) -> list[Any]:
        if link_type != self._link_type or self._tracker is None:
            self._link_type = link_type
            self._tracker = self._tracker_factory(link_type)
            self._window = None
            self._window_refreshed_mono = None
        is_dsl = link_type == FritzBoxLinkTypes.DSL
        counters = {"attempted": 0, "failed": 0}

        previous_call = self._status_call
        supplied_call = self._status_call_provider() if self._status_call_provider else None
        if supplied_call is not None:
            self._status_call = supplied_call
        elif session is not self._status_session or monotonic_fn() >= self._status_refresh_after:
            self._status_call = resolve_status_call(session, link_type)
            self._status_session = session
            self._status_refresh_after = monotonic_fn() + METADATA_REFRESH_S
        if previous_call is not None and self._status_call != previous_call:
            # Counters of different WAN services cannot share a baseline; an open interruption survives.
            self._tracker.reset_link_baseline()
        service, action = self._status_call
        ppp_active = service.upper().startswith("WANPPPCONNECTION")
        status, wall, mono, rtt, _ = self._call(session, counters, service, action, now_fn, monotonic_fn)
        connected = parse_connection_status((status or {}).get("NewConnectionStatus"))
        uptime = parse_uptime((status or {}).get("NewUptime"))
        last_error = parse_last_error((status or {}).get("NewLastConnectionError"))
        if is_dsl:
            # Disconnect-window evidence belongs to the active PPP service only.
            diag, prevention = self._resolve(session, service if ppp_active else None)
        if connected is True:
            self._healthy_cycles += 1
        self._cycle += 1

        dsl_sync: bool | None = None
        window: ForcedDisconnectWindow | None = None
        if is_dsl:
            if ppp_active:
                self._refresh_window(session, counters, service, prevention, now_fn, monotonic_fn)
                window = self._window
            needs_attention = bool(getattr(self._tracker, "needs_attention", False))
            # The poll that first shows a forced-disconnect code needs the sync state as well.
            new_forced = self._tracker.is_new_forced_error(last_error)
            if connected is not True or needs_attention or new_forced or self._in_discovery(self._diag):
                result, _ = self._signal_call(
                    session, counters, self._diag, "dsl_diagnose", DSL_DIAG_SERVICE,
                    diag, now_fn, monotonic_fn,
                )
                dsl_sync = parse_dsl_sync((result or {}).get("NewX_AVM-DE_DSLSync"))

        if counters["attempted"] and counters["failed"] == counters["attempted"]:
            self._tracker.note_unreachable()
            raise WanWatchUnavailable("all WAN watch calls failed")

        partial = WanSample(
            observed_at=wall,
            monotonic_s=mono,
            connected=connected,
            link_uptime_s=uptime,
            start_reliable=uptime is not None and rtt <= MAX_RTT_S,
            dsl_sync=dsl_sync,
            last_error=last_error,
            ppp_active=ppp_active,
        )
        sample = partial
        if self._tracker.wants_router_uptime(partial, self._cycle):
            device, r_wall, r_mono, r_rtt, _ = self._call(
                session, counters, *DEVICE_CALL, now_fn, monotonic_fn,
            )
            router_uptime = parse_uptime((device or {}).get("NewUpTime"))
            if router_uptime is not None:
                sample = partial.model_copy(update={
                    "router_uptime_s": router_uptime,
                    "router_observed_at": r_wall,
                    "router_monotonic_s": r_mono,
                    "router_reliable": r_rtt <= MAX_RTT_S,
                })
        return list(self._tracker.observe(sample, window))
