#!/usr/bin/env python3
#
# app/fritzbox/handler.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
import hashlib
import logging
import re
import ssl
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx

from app.fritzbox import service_definitions
from app.fritzbox.config import FritzBoxConfig
from app.fritzbox.model import FritzBoxLinkTypes
from app.fritzbox.service_definitions.helpers import warn_rate_limited
from app.fritzbox.service_handler import (
    FritzBoxLuaService,
    FritzBoxTR064Service,
)
from app.fritzbox.tr064.client import TR064_VERIFY_TLS, TR064Client
from app.fritzbox.tr064.deadline import RequestDeadline, read_bounded, request_timeout
from app.fritzbox.tr064.exceptions import (
    TR064AccessDisabled,
    TR064ActionNotFound,
    TR064ClientClosed,
    TR064ConnectError,
    TR064ConnectTimeout,
    TR064Error,
    TR064HttpUnauthorized,
    TR064ProtocolError,
    TR064ServiceNotFound,
    TR064TransportError,
    sanitize_text,
)
from app.fritzbox.tr064.xmlsafe import parse_xml
from app.fritzbox.wan_cause import LIFETIME_S
from app.fritzbox.wan_connection import (
    DSL_STATUS_CALL,
    METADATA_REFRESH_S,
    OTHER_STATUS_CALL,
    resolve_link_type,
    resolve_status_call,
)
from app.fritzbox.wan_tracker import TrackerEvent
from app.fritzbox.wan_watch import (
    WanWatcher,
    WanWatchUnavailable,
    parse_connection_status,
    parse_last_error,
    parse_uptime,
)
from app.utils.common import PermanentConfigError, grab, parse_bool
from app.utils.log import get_logger
from app.utils.measurement import METRIC_TYPES, FritzMeasurement
from app.utils.settings import reveal

log = get_logger()

# Upper bound for waiting on in-flight FritzBox requests during shutdown; a request that
# takes longer is abandoned. Part of the shutdown budget checked in tests/test_docker_contract.py.
SHUTDOWN_DRAIN_TIMEOUT = 5
# Consecutive rejected login attempts during operation before the daemon gives up: a rebooting box
# answers a login with a rejection for a short while, so one rejection proves nothing.
REJECTED_ATTEMPTS_BEFORE_EXIT = 3
REJECTION_CREDENTIALS = "rejected the configured credentials"
REJECTION_ACCESS_DISABLED = "TR-064 access is disabled"

# Pause between failed logins, so concurrent services do not hammer the box and trigger its login block.
LOGIN_RETRY_COOLDOWN = 30.0
# Upper bound for the pause between TR-064 reconnects while the box is unreachable.
MAX_RECONNECT_DELAY = 300.0
# After a reconnect proved the login valid, all-401 cycles are classified as missing action rights
# and not probed again for this long: a login per cycle would add load without new information.
RIGHTS_PROBE_INTERVAL = 900.0
# Outcome of a TR-064 service query in which every attempted action answered 401.
UNAUTHORIZED = object()

# Sanity cap for the PBKDF2 iteration counts the box announces in a login challenge.
MAX_PBKDF2_ITERATIONS = 1_000_000
# Size caps for Lua responses: the box is untrusted on an unverified channel. The login document is
# tiny; data endpoints (call list, hosts, logs) may be large but not unbounded.
MAX_LUA_LOGIN_BYTES = 64 * 1024
MAX_LUA_DATA_BYTES = 16 * 1024 * 1024
# A session id is 16 hex digits; all zeros means "not logged in".
SID_PATTERN = re.compile(r"[0-9A-Fa-f]{16}")
NULL_SID = "0" * 16
_BODY_FRAMING_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})
# Upper bound for the WAN watch back-off while the box does not answer its status calls.
WAN_WATCH_MAX_BACKOFF = 60.0
WAN_WATCH_LOG_INTERVAL = 300.0
# Consecutive transient failures of one service before they are reported above DEBUG. One failure
# is usually a slow or busy box; three cover at least three polling cycles (about three minutes
# at the default interval), so a short hiccup stays silent but a real outage is reported.
SERVICE_FAILURES_BEFORE_REPORT = 3
SERVICE_FAILURE_REPORT_INTERVAL = 300.0

class FritzBoxHandlerBase:
    """
        base class to provide common methods to both FritzBox handler classes
    """

    def __init__(self, config):
        if isinstance(config, FritzBoxConfig):
            self.config = config
        else:
            self.config = FritzBoxConfig(config)

        self.init_successful = False
        self._session_lock = threading.Lock()
        # Outcome of the latest login attempt; the lock serializes writers (worker threads) and the event loop reader.
        self._rejection_lock = threading.Lock()
        self._rejection_reason: str | None = None
        self._streak_reason: str | None = None
        # Set on cancellation: late failures of in-flight requests are expected then.
        self.shutting_down = False
        self._attempt_blocked = False
        self._rejected_attempts = 0
        self.services = []
        self.current_result_list = []
        self.discovery_done = False
        self._transport_error_log_state = {}
        # Worker threads log concurrently; the read-modify-write of the throttle state needs a lock.
        self._transport_error_lock = threading.Lock()

    @property
    def auth_failed(self) -> bool:
        """True when the latest login attempt was rejected for its credentials."""
        return self._rejection_reason == REJECTION_CREDENTIALS

    @property
    def access_disabled(self) -> bool:
        """True when the latest login attempt found TR-064 switched off."""
        return self._rejection_reason == REJECTION_ACCESS_DISABLED

    def _log_auth_failure(self) -> None:
        log.error(
            "Failed to connect to %s '%s' using credentials. Check username and password!",
            self.name, self.config.hostname,
        )

    def request_shutdown(self) -> None:
        """Mark the shutdown when the signal arrives, so a login chain in a worker thread stops early."""
        with self._session_lock:
            self.shutting_down = True

    def _abort_login(self) -> None:
        # An abandoned login is neither a rejection nor an error: the process is going down.
        log.debug("%s login abandoned: shutdown in progress", self.name)

    def _begin_login_attempt(self) -> None:
        with self._rejection_lock:
            self._rejection_reason = None
            self._attempt_blocked = False

    def _reject_login(self, reason: str) -> None:
        with self._rejection_lock:
            self._rejection_reason = reason
            self._streak_reason = reason
            self._rejected_attempts += 1

    def _block_login(self) -> None:
        # A login block says nothing about the credentials: it neither counts nor breaks the streak.
        with self._rejection_lock:
            self._attempt_blocked = True

    def _end_login_attempt(self) -> None:
        # A success or a transport failure breaks the streak; a rejection or a login block leaves it alone.
        with self._rejection_lock:
            if self._rejection_reason is None and not self._attempt_blocked:
                self._rejected_attempts = 0
                self._streak_reason = None

    def _permanent_rejection(self) -> str | None:
        with self._rejection_lock:
            if self._rejected_attempts >= REJECTED_ATTEMPTS_BEFORE_EXIT:
                return self._streak_reason
        return None

    async def _before_cycle(self) -> bool:
        """Hook run before each polling cycle; False skips the queries of this cycle."""
        return True

    def _review_cycle(self, results) -> None:
        """Hook run with the results of one polling cycle."""

    def add_services(self, class_name, service_definition):
        """
        Adds services from config to handler

        Parameters
        ----------
        class_name: FritzBoxTR064Service, FritzBoxLuaService
            the fritzbox service class
        service_definition: list
            list of service definitions
        """

        for fritzbox_service in service_definition:
            new_service = class_name(fritzbox_service)

            new_service.interval = max(new_service.interval, self.config.request_interval)

            self.services.append(new_service)

    def _log_transport_error(self, key, message, *args, interval=300.0):
        if self.shutting_down:
            log.debug(message, *args)
            return
        now = time.monotonic()
        with self._transport_error_lock:
            last_log, suppressed_count = self._transport_error_log_state.get(key, (0.0, 0))
            emit = last_log == 0.0 or now - last_log >= interval
            self._transport_error_log_state[key] = (now, 0) if emit else (last_log, suppressed_count + 1)

        if emit:
            formatted_message = message % args if args else message
            if suppressed_count:
                formatted_message = f"{formatted_message} (suppressed {suppressed_count} repeated message(s))"
            log.error("%s", formatted_message)
            return

        log.debug(message, *args)

    def _report_service_health(self, service, failures_before, result):
        """Escalate a persistent transient failure once, then rate limited; log the recovery once."""
        if result is True:
            if service.outage_reported:
                service.outage_reported = False
                log.info("%s service '%s' recovered after %d failed attempt(s)",
                         self.name, service.name, failures_before)
            return
        if (
            service.transient_failure is None
            or self.shutting_down
            or service.failure_count < SERVICE_FAILURES_BEFORE_REPORT
        ):
            return
        service.outage_reported = True
        warn_rate_limited(
            (self.name, service.name, "service_failing"),
            "%s service '%s' failed %d consecutive time(s), still retrying: %s",
            self.name, service.name, service.failure_count, service.transient_failure,
            interval=SERVICE_FAILURE_REPORT_INTERVAL,
        )

    def _query_service_with_backoff(self, service):
        failures_before = service.failure_count
        service.transient_failure = None
        try:
            result = self.query_service_data(service)
        except Exception:  # noqa: BLE001 - one broken service must not take down the polling loop
            log.exception("Unexpected failure while querying %s service '%s'", self.name, service.name)
            result = False

        if (result is False or result is UNAUTHORIZED) and service.available is True:
            delay = service.set_failed_query_now()
            log.debug(
                "Backing off %s service '%s' for %.0f seconds after %s failed request(s)",
                self.name, service.name, delay, service.failure_count,
            )

        self._report_service_health(service, failures_before, result)
        return result

    @staticmethod
    async def _drain_pending_queries(pending, timeout: float) -> None:
        """Wait for in-flight worker threads after this task was cancelled.

        asyncio.to_thread() cannot interrupt a running request, so returning right
        away would let close() drop the session underneath a live thread.
        """
        try:
            await asyncio.wait_for(asyncio.shield(pending), timeout)
        except TimeoutError:
            log.warning("Timed out waiting for in-flight Fritz!Box requests during shutdown")
        except asyncio.CancelledError:
            log.debug("Shutdown drain of in-flight Fritz!Box requests was interrupted")
        except Exception as exc:  # noqa: BLE001 - a failing request must not break shutdown
            log.debug("In-flight Fritz!Box request failed during shutdown: %s", exc)

    async def task_loop(self, queue):
        service_query_limit = asyncio.Semaphore(4)

        async def query_service(service):
            async with service_query_limit:
                return await asyncio.to_thread(self._query_service_with_backoff, service)

        while True:
            self.current_result_list = []
            services_to_request = []
            if await self._before_cycle():
                services_to_request = [
                    service for service in self.services
                    if self.discovery_done is False or service.should_be_requested() is True
                ]

            pending_queries = asyncio.gather(
                *[
                    query_service(service)
                    for service in services_to_request
                ]
            )

            try:
                results = await asyncio.shield(pending_queries)
            except asyncio.CancelledError:
                self.shutting_down = True
                await self._drain_pending_queries(pending_queries, SHUTDOWN_DRAIN_TIMEOUT)
                raise

            for result in self.current_result_list:
                log.debug("%s measurement collected", result.name)
                await queue.put(result)

            self.discovery_done = True
            self._review_cycle(results)
            reason = self._permanent_rejection()
            if reason is not None:
                raise PermanentConfigError(f"{self.name} '{self.config.hostname}' {reason}")
            await asyncio.sleep(1)


def _has_ssl_cause(exc: BaseException | None) -> bool:
    """checks whether an exception chain contains a TLS error (e.g. failed certificate verification)"""
    while exc is not None:
        if isinstance(exc, ssl.SSLError):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _is_access_disabled(exc: Exception) -> bool:
    """True when the box answers without tr64desc.xml, i.e. 'Allow access for applications' is off."""
    return isinstance(exc, TR064AccessDisabled)


def _is_auth_failure(exc: Exception) -> bool:
    """True for an HTTP 401 after the digest exchange; a SOAP fault 401 or an HTTP 403 is no auth failure."""
    return isinstance(exc, TR064HttpUnauthorized)


class FritzBoxHandler(FritzBoxHandlerBase):

    name = "FritzBox TR-064"

    def __init__(self, config):

        super().__init__(config)

        self.session: TR064Client | None = None
        # Client of a login in progress; request_shutdown() closes it so the next request fails fast.
        self._candidate: TR064Client | None = None
        # Strictly increasing per connect attempt; makes sessions distinguishable in logs and tests.
        self._generation = 0
        self._reconnect_after = 0.0
        self._reconnect_delay = LOGIN_RETRY_COOLDOWN
        self._suspect_check_after = 0.0
        self._wan_connected: bool | None = None
        self._wan_uptime: int | None = None
        self._wan_renewal_notice_sent = False
        self._wan_watch_log_state: dict[str, float] = {}
        self._wan_status_call: tuple[str, str] | None = None
        self._wan_metadata_after = 0.0
        self.add_services(FritzBoxTR064Service, service_definitions.tr064_services)

    def _observe_wan_connection(self, result: dict) -> None:
        connected = parse_connection_status(result.get("NewConnectionStatus"))
        uptime = parse_uptime(result.get("NewUptime"))

        if connected is False and self._wan_connected is not False:
            # Only a fixed ERROR_* code is logged; free text from the box could forge log lines.
            reason = parse_last_error(result.get("NewLastConnectionError"))
            suffix = f" (router's last connection error: {sanitize_text(reason)})" if reason not in (None, "ERROR_NONE") else ""
            log.warning("FRITZ!Box WAN connection is down%s", suffix)
        elif connected is True and self._wan_connected is False:
            log.info("FRITZ!Box WAN connection restored")
            self._wan_renewal_notice_sent = False
        elif (connected is True and self._wan_connected is True and uptime is not None
              and self._wan_uptime is not None and uptime < self._wan_uptime):
            log.warning("FRITZ!Box WAN connection restarted between status polls")
            self._wan_renewal_notice_sent = False

        if (
            self.config.link_type == FritzBoxLinkTypes.DSL
            and connected is True
            and uptime is not None
            and 0 <= LIFETIME_S - uptime <= 300
            and not self._wan_renewal_notice_sent
        ):
            estimate = datetime.now(UTC) + timedelta(seconds=LIFETIME_S - uptime)
            log.info("WAN connection may renew after 24 hours, around %s", estimate.isoformat())
            self._wan_renewal_notice_sent = True

        if connected is not None:
            self._wan_connected = connected
        if uptime is not None:
            self._wan_uptime = uptime

    def _flag_access_disabled(self, exc) -> bool:
        if not _is_access_disabled(exc):
            return False
        self._reject_login(REJECTION_ACCESS_DISABLED)
        log.error(
            "TR-064 access is disabled on the FRITZ!Box '%s'; enable 'Allow access for applications' "
            "under Home Network > Network > Network Settings", self.config.hostname,
        )
        return True

    def connect(self):

        if self.shutting_down:
            return
        if self.init_successful is True:
            return

        self._begin_login_attempt()
        try:
            self._open_session()
        finally:
            self._candidate = None
            self._end_login_attempt()
        if self.init_successful is not True and self.session is not None:
            self.session.close()
            self.session = None

    def _wan_watch_log(self, key: str, message: str) -> None:
        # Fixed texts only; the rate limit keeps a failing signal from flooding the log.
        now = time.monotonic()
        if now - self._wan_watch_log_state.get(key, -WAN_WATCH_LOG_INTERVAL) < WAN_WATCH_LOG_INTERVAL:
            return
        self._wan_watch_log_state[key] = now
        level = logging.WARNING if key.endswith(("_err", "_auth")) else logging.INFO
        log.log(level, "%s", message)

    @staticmethod
    def _log_wan_event(event: TrackerEvent) -> None:
        result = event.result
        if event.kind == "opened":
            log.info("FRITZ!Box WAN interruption detected, cause not yet known")
        elif event.kind == "refined":
            log.info("FRITZ!Box WAN interruption cause: %s (%s)", result.cause.value, result.confidence.value)
        else:
            duration = "an unknown time" if event.duration_s is None else f"{event.duration_s} s"
            log.info(
                "FRITZ!Box WAN interruption ended after %s: cause=%s, confidence=%s, evidence=%s",
                duration, result.cause.value, result.confidence.value,
                ",".join(e.value for e in result.evidence) or "none",
            )

    async def _publish_wan_event(self, event: TrackerEvent, queue) -> None:
        self._log_wan_event(event)
        if event.kind != "closed":
            return
        # Three fields with one timestamp end up in one line; the start is timestamp - duration.
        fields = (
            ("wan_interruption_cause", event.result.cause.value, str),
            ("wan_interruption_confidence", event.result.confidence.value, str),
            ("wan_interruption_duration", event.duration_s, int),
        )
        for name, value, data_type in fields:
            if value is None:
                # An unknown duration is never stored as a measured one.
                continue
            await queue.put(FritzMeasurement(
                name, value, data_type=data_type, box_tag=self.config.box_tag, timestamp=event.observed_at,
            ))

    def _build_wan_watcher(self, interval: float) -> WanWatcher:
        # Providers are read per cycle, so a reconnect or a late link type never leaves a stale value.
        return WanWatcher(
            session_provider=lambda: self.session,
            link_type_provider=lambda: self.config.link_type,
            is_auth_failure=_is_auth_failure,
            log_once=self._wan_watch_log,
            poll_interval_s=interval,
            status_call_provider=lambda: self._wan_status_call,
        )

    async def wan_watch_loop(self, queue):
        """Slim fast poll of the WAN state; never logs in, reconnects or ends on its own."""
        interval = float(self.config.wan_watch_interval)
        watcher: WanWatcher | None = None
        failures = 0
        while True:
            delay = interval
            if self.init_successful is True:
                if watcher is None:
                    watcher = self._build_wan_watcher(interval)
                pending = asyncio.ensure_future(
                    asyncio.to_thread(watcher.poll_once, lambda: datetime.now(UTC), time.monotonic)
                )
                try:
                    events = await asyncio.shield(pending)
                except asyncio.CancelledError:
                    self.shutting_down = True
                    await self._drain_pending_queries(pending, SHUTDOWN_DRAIN_TIMEOUT)
                    raise
                except WanWatchUnavailable:
                    self._log_transport_error(
                        ("wan_watch", "unavailable"), "WAN watch unavailable: session closed or requests failed; backing off"
                    )
                    events = []
                    failures += 1
                except Exception as exc:  # noqa: BLE001 - the task must not end: main would exit 1
                    self._log_transport_error(
                        ("wan_watch", type(exc).__name__), "WAN watch cycle failed: %s", type(exc).__name__
                    )
                    events = []
                    failures += 1
                else:
                    failures = 0
                for event in events:
                    await self._publish_wan_event(event, queue)
                if failures:
                    delay = min(interval * 2 ** min(failures, 6), WAN_WATCH_MAX_BACKOFF)
            await asyncio.sleep(delay)

    def _reconnect(self) -> None:
        """Counted login attempt after every action was denied; its probe decides what the 401s mean."""
        if time.monotonic() < self._reconnect_after:
            return

        previous = self.session
        self._begin_login_attempt()
        try:
            self._open_session(reconnect=True)
        finally:
            self._candidate = None
            self._end_login_attempt()

        now = time.monotonic()
        current = self.session
        if previous is not current and previous is not None:
            previous.close()
        if self.init_successful is not True:
            if current is not None:
                current.close()
            self.session = None
        if self.init_successful is True:
            self._reconnect_delay = LOGIN_RETRY_COOLDOWN
            self._suspect_check_after = now + RIGHTS_PROBE_INTERVAL
            log.warning(
                "%s '%s' accepts the login, so the denied actions are missing rights of the FRITZ!Box user; "
                "grant the user the required rights", self.name, self.config.hostname,
            )
            return

        if self.auth_failed or self.access_disabled:
            self._reconnect_delay = LOGIN_RETRY_COOLDOWN
        else:
            self._reconnect_delay = min(self._reconnect_delay * 2, MAX_RECONNECT_DELAY)
        self._reconnect_after = now + self._reconnect_delay

    async def _before_cycle(self) -> bool:
        if self.init_successful is True:
            if time.monotonic() >= self._wan_metadata_after and self.session is not None:
                await asyncio.to_thread(self._refresh_wan_metadata, self.session)
            return True
        await asyncio.to_thread(self._reconnect)
        return self.init_successful is True

    def _refresh_wan_metadata(self, session) -> None:
        self._wan_metadata_after = time.monotonic() + METADATA_REFRESH_S
        if self.config.link_type is None:
            try:
                link_type = resolve_link_type(session, self.config.model)
                if self.session is session:
                    self.config.link_type = link_type
            except TR064Error as exc:
                self._log_transport_error(("wan_metadata", "link"), "Unable to determine FritzBox link type: %s", exc)
        try:
            status_call = resolve_status_call(session, self.config.link_type)
            if self.session is session:
                self._wan_status_call = status_call
        except TR064Error as exc:
            self._log_transport_error(("wan_metadata", "service"), "Unable to determine active WAN service: %s", exc)

    def _review_cycle(self, results) -> None:
        # A cycle in which every queried action answered 401 and none succeeded makes the session suspect.
        queried = [result for result in results if result is not None]
        if not queried or any(result is not UNAUTHORIZED for result in queried):
            return
        if self.init_successful is not True or time.monotonic() < self._suspect_check_after:
            return
        log.warning("%s '%s' denied every queried action; verifying the login", self.name, self.config.hostname)
        self.init_successful = False
        self._reconnect_after = 0.0

    def request_shutdown(self) -> None:
        super().request_shutdown()
        candidate = self._candidate
        if candidate is not None:
            candidate.close()

    def _track_candidate(self, client: TR064Client) -> None:
        with self._session_lock:
            stop = self.shutting_down
            if not stop:
                self._candidate = client
        if stop:
            client.close()
            raise TR064ClientClosed("shutdown in progress")

    def _create_session(self, use_tls: bool, port: int) -> TR064Client:
        """Build and discover a client; the caller publishes it only after the login probe."""
        self._generation += 1
        timeout = request_timeout(self.config.connect_timeout)
        client = TR064Client(
            self.config.hostname,
            port,
            username=self.config.username,
            password=reveal(self.config.password),
            use_tls=use_tls,
            verify=TR064_VERIFY_TLS,
            timeout=timeout,
            generation=self._generation,
        )
        try:
            self._track_candidate(client)
            client.discover()
        except BaseException:
            client.close()
            raise
        return client

    def _open_session(self, reconnect=False):

        log.debug(f"Initiating new {self.name} session")

        # Always probe HTTPS first; the default plain port maps to the default TLS port
        port = self.config.port
        if port == FritzBoxConfig.DEFAULT_PORT:
            port = FritzBoxConfig.DEFAULT_TR064_HTTPS_PORT

        try:
            candidate = self._create_session(True, port)
            self.config.tls_enabled = True
        except TR064AccessDisabled as exc:
            self._flag_access_disabled(exc)
            return
        except TR064ClientClosed:
            self._abort_login()
            return
        except (TR064ConnectError, TR064ConnectTimeout) as exc:
            if self.shutting_down:
                self._abort_login()
                return
            # Only a failed connection setup falls back to HTTP; an answering box never downgrades.
            (log.debug if reconnect else log.warning)(
                "FritzBox '%s' TR-064 HTTPS unavailable (%s); falling back to plain HTTP",
                self.config.hostname, exc,
            )
            self.config.tls_enabled = False
            try:
                candidate = self._create_session(False, self.config.port)
            except TR064AccessDisabled as exc2:
                self._flag_access_disabled(exc2)
                return
            except TR064ClientClosed:
                self._abort_login()
                return
            except TR064Error as exc2:
                # An unreachable box is expected during outages; a traceback would only add noise.
                log.error(f"Failed to connect to FritzBox via TR-064 '{exc2}'")
                return
            except Exception:
                log.exception("Unexpected error while creating FritzBox TR-064 session")
                return
        except TR064Error as exc:
            log.error(f"Failed to connect to FritzBox via TR-064 '{exc}'")
            return
        except Exception:
            log.exception("Unexpected error while creating FritzBox TR-064 session")
            return

        try:
            device_info = candidate.call_action("DeviceInfo", "GetInfo")
        except TR064ClientClosed:
            candidate.close()
            self._abort_login()
            return
        except TR064Error as e:
            candidate.close()
            if _is_auth_failure(e):
                self._reject_login(REJECTION_CREDENTIALS)
                self._log_auth_failure()
            else:
                log.error(f"Failed to connect to {self.name} '{self.config.hostname}': {e}")

            return
        except Exception as e:  # noqa: BLE001 - an untyped error is a client bug; the login attempt must still end cleanly
            candidate.close()
            log.error(f"Failed to connect to {self.name} '{self.config.hostname}': {type(e).__name__}")
            return

        # Publish only a client whose login was proven, so the watcher never sees a rejected session.
        with self._session_lock:
            publish = not self.shutting_down
            if publish:
                self.session = candidate
        if not publish:
            candidate.close()
            return
        self._wan_status_call = None
        if not reconnect and isinstance(device_info, dict):
            self.config.model = device_info.get("NewModelName")
            self.config.fw_version = device_info.get("NewSoftwareVersion")
            self.config.serial_number = device_info.get("NewSerialNumber") or None

        self._refresh_wan_metadata(candidate)

        if reconnect:
            log.debug("Re-established %s session", self.name)
            with self._session_lock:
                self.init_successful = not self.shutting_down and self.session is candidate
            return

        # cross-check the configured time zone against Time:1 -> GetInfo
        try:
            utc_before = datetime.now(UTC)
            time_info = candidate.call_action("Time:1", "GetInfo")
            utc_after = datetime.now(UTC)
            local_str = (time_info or {}).get("NewCurrentLocalTime", "")
            if local_str:
                # deliberately naive: the offset against UTC is what we want to measure
                local_dt = datetime.strptime(local_str[:19], "%Y-%m-%dT%H:%M:%S")
                # Use midpoint of the two UTC samples to minimise request latency error
                utc_mid = utc_before + (utc_after - utc_before) / 2
                raw_offset = local_dt - utc_mid.replace(tzinfo=None)
                # Round to nearest minute (Fritz!Box offsets are always whole minutes)
                total_seconds = round(raw_offset.total_seconds() / 60) * 60
                sign = "+" if total_seconds >= 0 else "-"
                abs_s = abs(total_seconds)
                detected = f"UTC{sign}{abs_s // 3600:02d}:{(abs_s % 3600) // 60:02d}"

                # Only compare: replacing the configured IANA zone with a fixed offset
                # would shift historical timestamps across a DST boundary by one hour.
                configured_offset = utc_mid.astimezone(self.config.timezone).utcoffset()
                if configured_offset is None or round(configured_offset.total_seconds()) != total_seconds:
                    log.warning(
                        "Fritz!Box reports local time %s, but the configured time zone '%s' resolves to "
                        "a different offset; check FRITZBOX_TIMEZONE",
                        detected, self.config.timezone,
                    )
                else:
                    log.debug("Fritz!Box local time offset %s matches configured time zone '%s'",
                              detected, self.config.timezone)
        except Exception as exc:  # noqa: BLE001 - the time zone cross-check is best effort
            log.debug("Unable to verify Fritz!Box time zone against the configured value: %s", exc)

        proto = "HTTPS" if self.config.tls_enabled else "HTTP"
        log.info(f"Successfully established {self.name} session ({proto})")

        with self._session_lock:
            self.init_successful = not self.shutting_down and self.session is candidate

    def close(self):
        with self._session_lock:
            session, self.session = self.session, None
            was_initialized = self.init_successful
            self.init_successful = False
        if session is not None:
            session.close()
        if was_initialized:
            log.info(f"Closed {self.name} connection")

    def query_service_data(self, service):

        def service_invalid_log(log_message):
            if service.link_type is not None and service.link_type != self.config.link_type:
                log.debug("%s (only available on %s connections)", log_message, service.link_type)
            else:
                log.info("%s", log_message)

        if not isinstance(service, FritzBoxTR064Service):
            log.error("Query service must be of type 'FritzBoxTR064Service'")
            return

        if self.discovery_done is True and service.should_be_requested() is False:
            return

        # One session per query: a reconnect meanwhile supersedes it and its results are dropped below.
        session = self.session
        if session is None:
            log.debug("No %s session for service '%s'", self.name, service.name)
            return False

        successful_request = False
        transient_failure = False
        denied_actions = 0
        collected = []
        wan_results: list[dict] = []
        queried = False
        pending_fw_version = None

        # all values of one service query share a timestamp so the writer can bundle them into one line
        query_timestamp = datetime.now(UTC)

        for action in service.actions:

            if service.available is False:
                break

            if self.discovery_done is True and action.available is False:
                log.debug(f"Skipping disabled service action: {service.name} - {action.name}")
                continue

            try:
                call_result = session.call_action(service.name, action.name, action.params)
            except TR064ServiceNotFound:
                if self.discovery_done is False:
                    label = service.description or service.name
                    service_invalid_log(f"'{label}' not supported by this Fritz!Box — metrics skipped")
                    service.available = False
                continue
            except TR064ActionNotFound:
                if self.discovery_done is False:
                    label = service.description or service.name
                    service_invalid_log(f"'{label}' action '{action.name}' not supported by this Fritz!Box — metrics skipped")
                    action.available = False
                continue
            except TR064HttpUnauthorized:
                # A 401 means missing rights for this action or a changed password; the cycle review tells which.
                denied_actions += 1
                self._log_transport_error(
                    (self.name, service.name, action.name, "forbidden"),
                    "%s '%s' answered 401 for action '%s' of service '%s'; the FRITZ!Box user may lack the "
                    "right for it, or the credentials changed (checked automatically)",
                    self.name, self.config.hostname, action.name, service.name,
                )
                continue
            except (TR064TransportError, TR064ClientClosed) as e:
                # Transient: debug only here; a persistent failure is reported by the service health check.
                transient_failure = True
                service.transient_failure = f"{type(e).__name__}: {e}"
                log.debug("%s request for service '%s' action '%s' failed: %s",
                          self.name, service.name, action.name, service.transient_failure)
                continue
            except TR064Error as e:
                # HTTP 403 stays transient (no relogin): it cannot tell an expired session from a missing right.
                transient_failure = True
                self._log_transport_error(
                    (self.name, service.name, action.name, type(e).__name__),
                    "Failed to connect to %s '%s': %s",
                    self.name, self.config.hostname, e,
                )
                continue
            except Exception as e:  # noqa: BLE001 - one bad service must not stop the polling loop
                # An untyped error is a bug, not a box fact: back off, log rarely
                transient_failure = True
                self._log_transport_error(
                    (self.name, service.name, action.name, type(e).__name__),
                    "Unable to request %s data: %s", self.name, e,
                )
                continue

            if call_result is None:
                continue

            successful_request = True
            status_call = getattr(self, "_wan_status_call", None) or (
                DSL_STATUS_CALL if self.config.link_type == FritzBoxLinkTypes.DSL else OTHER_STATUS_CALL
            )
            if (service.name, action.name) == status_call:
                wan_results.append(call_result)
            debug_msg = f"Request {self.name} service '{service.name}' returned successfully: {action.name}"
            if len(action.params) > 0:
                debug_msg += f" (parameters: {', '.join(sorted(action.params))})"
            log.debug(debug_msg)

            queried = True

            for key, value in call_result.items():
                log.debug("%s result field available: %s (%s)", self.name, key, type(value).__name__)
                metric_name = service.value_instances.get(key)

                if metric_name is not None:

                    data_type = None

                    # support setting a data type by appending it to the metric name with a colon
                    if ":" in metric_name:
                        metric_data_type = metric_name.split(":")[1]
                        metric_name = metric_name.split(":")[0]

                        data_type = METRIC_TYPES.get(metric_data_type)

                        if data_type is None:
                            log.warning(f"Unknown data type '{metric_data_type}' for metric '{key}' "
                                        f"in service '{service.name}'")
                            continue

                    collected.append(
                        FritzMeasurement(metric_name, value, box_tag=self.config.box_tag, data_type=data_type,
                                         timestamp=query_timestamp)
                    )

            # special case: update firmware version when requested
            if service.name == "DeviceInfo" and action.name == "GetInfo":
                fw_version = call_result.get("NewSoftwareVersion")
                if fw_version:
                    pending_fw_version = fw_version

        if self.discovery_done is False and True not in [x.available for x in service.actions]:
            service_invalid_log(f"All actions for service '{service.name}' are unavailable. Disabling service.")
            service.available = False

        if self.session is not session:
            log.debug("Dropped %d result(s) of service '%s': the session was replaced during the query",
                      len(collected), service.name)
            return None

        # State changes only after the generation check, so a replaced session leaves no trace.
        for wan_result in wan_results:
            self._observe_wan_connection(wan_result)
        if queried:
            service.set_last_query_now()
        if pending_fw_version:
            self.config.fw_version = pending_fw_version
        self.current_result_list.extend(collected)

        if successful_request is True:
            return True

        if transient_failure is True:
            return False

        if denied_actions > 0:
            return UNAUTHORIZED

        return None


class FritzBoxLuaHandler(FritzBoxHandlerBase):
    _result_lock = threading.Lock()

    name = "FritzBox Lua"

    def __init__(self, config):
        super().__init__(config)

        self.url = None  # built lazily in connect() after TR-064 auto-detect resolves tls_enabled
        self.sid = None
        self._unverified_tls_warned = False
        # worker threads share this handler; one login at a time, others reuse its sid
        self._connect_lock = threading.Lock()
        self._login_retry_after = 0.0
        self._block_logged = False
        self._block_seconds = 0.0

        # created lazily in connect() once the TLS mode is resolved
        self.session = None

        self.add_services(FritzBoxLuaService, service_definitions.lua_services)

    def _build_session(self) -> None:
        # The client is shared by worker threads and its TLS settings depend only on
        # ca_file, so it is created once and never closed while others may use it.
        if self.session is not None:
            return

        ca_file = self.config.ca_file
        if ca_file:
            # trust only the configured certificate / CA, not the system store
            verify = ssl.create_default_context(cafile=ca_file)
        else:
            verify = False
            if not self._unverified_tls_warned:
                self._unverified_tls_warned = True
                log.warning("TLS unverified (TR-064; Lua without FRITZBOX_CA_FILE)")

        # trust_env=False: the sid travels as a parameter and must never reach a proxy from the environment.
        candidate = httpx.Client(
            verify=verify,
            trust_env=False,
            timeout=request_timeout(self.config.connect_timeout),
        )
        with self._session_lock:
            publish = not self.shutting_down
            if publish:
                self.session = candidate
        if not publish:
            candidate.close()

    @staticmethod
    def _fetch(session: httpx.Client, method: str, url: str, max_bytes: int, **kwargs) -> httpx.Response:
        """Read a response up to ``max_bytes`` decoded bytes within the overall deadline."""
        deadline = RequestDeadline(session.timeout)
        with session.stream(method, url, timeout=deadline.timeout_for_next_send(session.timeout), **kwargs) as response:
            body = read_bounded(response, max_bytes, deadline)
            # The body is already decoded, so the framing headers must not describe it again.
            headers = [(k, v) for k, v in response.headers.multi_items() if k.lower() not in _BODY_FRAMING_HEADERS]
            return httpx.Response(response.status_code, headers=headers, content=body, request=response.request)

    def _login_document(self, login_url: str, params: dict | None = None):
        response = self._fetch(self.session, "GET", login_url, MAX_LUA_LOGIN_BYTES, params=params)
        return parse_xml(response.content, max_bytes=MAX_LUA_LOGIN_BYTES)

    def _base_url(self, use_tls: bool) -> str:
        host = self.config.hostname
        if ":" in host:
            # an IPv6 literal needs brackets inside a URL
            host = f"[{host}]"
        return f"{'https' if use_tls else 'http'}://{host}"

    def connect(self):
        with self._connect_lock:
            # double check: another thread may have logged in while this one waited
            if self.sid is not None:
                return
            if time.monotonic() < self._login_retry_after:
                return
            self._begin_login_attempt()
            self._block_seconds = 0.0
            try:
                self._connect_locked()
            finally:
                self._end_login_attempt()
            if self._attempt_blocked is False:
                self._block_logged = False
            if self.sid is None:
                self._login_retry_after = time.monotonic() + max(LOGIN_RETRY_COOLDOWN, self._block_seconds)

    def _invalidate_sid(self, stale_sid) -> None:
        with self._connect_lock:
            # only drop the session id this caller used, not a fresh one from another login
            if self.sid == stale_sid:
                self.sid = None

    def _connect_locked(self):
        if self.shutting_down:
            return

        # Build URL now — TR-064 connect() may have resolved tls_enabled from None to True/False
        use_tls = self.config.tls_enabled is not False

        # A self-signed box certificate is unverified without FRITZBOX_CA_FILE.
        # Connection failures allow HTTP fallback; configured-CA verification failures abort.
        self._build_session()
        if self.session is None:
            return

        self.url = self._base_url(use_tls)

        login_url = f"{self.url}/login_sid.lua"

        log.debug(f"Initiating new {self.name} session")

        try:
            dom = self._login_document(login_url)
            sid = dom.findtext('./SID')
            challenge = dom.findtext('./Challenge')
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            if use_tls and self.config.ca_file and _has_ssl_cause(exc):
                log.error(f"Certificate verification against FRITZBOX_CA_FILE failed for {self.name}: {exc}")
                return

            if self.shutting_down:
                self._abort_login()
                return

            if use_tls:
                # HTTPS port unreachable — fall back to HTTP
                log.warning(
                    "FritzBox '%s' Lua HTTPS unreachable; falling back to plain HTTP.",
                    self.config.hostname,
                )
                use_tls = False
                self._build_session()
                self.url = self._base_url(False)
                login_url = f"{self.url}/login_sid.lua"
                try:
                    dom = self._login_document(login_url)
                    sid = dom.findtext('./SID')
                    challenge = dom.findtext('./Challenge')
                except (httpx.HTTPError, TR064ProtocolError) as exc2:
                    log.error(f"Unable to parse {self.name} login response after HTTP fallback: {exc2}")
                    return
            else:
                log.error(f"Unable to parse {self.name} login response: {exc}")
                return
        except (httpx.HTTPError, TR064ProtocolError) as exc:
            log.error(f"Unable to parse {self.name} login response: {exc}")
            return

        if sid != NULL_SID:
            log.error(f"Unexpected {self.name} session id: {sanitize_text(sid)}")
            return

        if not challenge:
            log.error(f"Missing {self.name} login challenge")
            return

        if self.shutting_down:
            self._abort_login()
            return

        login_response = self._login_response(challenge, reveal(self.config.password))
        if login_response is None:
            log.error(f"Unsupported or invalid {self.name} login challenge format")
            return

        login_params = {
            "username": self.config.username,
            "response": login_response,
        }

        try:
            dom = self._login_document(login_url, login_params)
            sid = dom.findtext('./SID')
            block_time = dom.findtext('./BlockTime')
        except (httpx.HTTPError, TR064ProtocolError) as exc:
            log.error(f"Unable to parse {self.name} login response: {exc}")
            return

        if block_time and block_time != "0":
            self._block_login()
            try:
                self._block_seconds = float(block_time)
            except ValueError:
                self._block_seconds = 0.0
            if not self._block_logged:
                self._block_logged = True
                log.error(f"Failed to connect to {self.name} '{self.config.hostname}'. "
                          f"Logins blocked for '{block_time}' seconds!")
            return

        if sid == NULL_SID:
            self._reject_login(REJECTION_CREDENTIALS)
            self._log_auth_failure()
            return

        if sid is None or not SID_PATTERN.fullmatch(sid):
            log.error(f"Invalid {self.name} session id in login response")
            return

        self.config.tls_enabled = use_tls
        proto = "HTTPS" if self.config.tls_enabled else "HTTP"
        log.info(f"Successfully established {self.name} session ({proto})")

        with self._session_lock:
            if not self.shutting_down and self.session is not None:
                self.sid = sid
                self.init_successful = True

    def request(self, service_to_request, additional_params, *, retry_on_auth: bool = True):

        if self.sid is None:
            self.connect()
        sid = self.sid
        url = self.url
        if sid is None:
            return None

        # sid last: service parameters must not override the session id.
        params = {**additional_params, "sid": sid} if isinstance(additional_params, dict) else {"sid": sid}

        call_attributes = {}

        if service_to_request.method == "POST":
            call_attributes["data"] = params
        else:
            call_attributes["params"] = params

        data_url = f"{url}{service_to_request.url_path}"

        session = self.session
        if session is None:
            return None
        try:
            response = self._fetch(session, service_to_request.method, data_url, MAX_LUA_DATA_BYTES, **call_attributes)
        except TR064ProtocolError as exc:
            self._log_transport_error(
                (self.name, service_to_request.name, "too_large"),
                "%s response for '%s' rejected: %s", self.name, service_to_request.name, exc,
            )
            return None
        except (httpx.HTTPError, OSError) as exc:
            # Transient: debug only here; a persistent failure is reported by the service health check.
            service_to_request.transient_failure = f"{type(exc).__name__}: {exc}"
            log.debug("Unable to perform request to '%s': %s", data_url, exc)
            return None

        # invalidate session on auth-related status codes before parsing
        if response.status_code in [303, 401, 403]:
            self._invalidate_sid(sid)
            if retry_on_auth:
                return self.request(service_to_request, additional_params, retry_on_auth=False)
            return None

        # check for HTML response indicating an expired/invalid session
        if response.content[:100].lstrip().lower().startswith(b"<html"):
            self._invalidate_sid(sid)
            if retry_on_auth:
                return self.request(service_to_request, additional_params, retry_on_auth=False)
            return None

        if response.status_code != 200:
            log.error(
                "%s request '%s' returned HTTP %s",
                self.name,
                service_to_request.name,
                response.status_code,
            )
            return None

        try:
            result = service_to_request.response_parser(response)
        except Exception as e:  # noqa: BLE001 - service-supplied response parser is a plugin boundary
            self._log_transport_error(
                (self.name, service_to_request.name, "parse", type(e).__name__),
                "%s request parsing for '%s' failed: %s",
                self.name, service_to_request.name, e,
            )
            return None

        log.debug(f"{self.name} request successful")

        return result

    def close(self):
        with self._session_lock:
            session, self.session = self.session, None
            was_initialized = self.init_successful
            self.sid = None
            self.init_successful = False
        if session is not None:
            session.close()
        if was_initialized:
            log.info(f"Closed {self.name} connection")

    @staticmethod
    def _legacy_login_response(challenge: str, password: str) -> str:
        # AVM mandates MD5; usedforsecurity=False avoids ValueError on FIPS-mode hosts
        md5 = hashlib.md5(usedforsecurity=False)
        md5.update(challenge.encode("utf-16le"))
        md5.update("-".encode("utf-16le"))
        md5.update(password.encode("utf-16le"))
        return f"{challenge}-{md5.hexdigest()}"

    @staticmethod
    def _login_response(challenge: str, password: str) -> str | None:
        """Answer a legacy (MD5) or PBKDF2 (`2$<iter1>$<salt1>$<iter2>$<salt2>`) challenge.

        Returns None for a malformed or unknown challenge format.
        """
        if not challenge.startswith("2$"):
            if "$" in challenge:
                return None
            return FritzBoxLuaHandler._legacy_login_response(challenge, password)

        parts = challenge.split("$")
        if len(parts) != 5:
            return None

        try:
            iterations1, iterations2 = int(parts[1]), int(parts[3])
            salt1, salt2 = bytes.fromhex(parts[2]), bytes.fromhex(parts[4])
        except ValueError:
            return None

        if not (0 < iterations1 <= MAX_PBKDF2_ITERATIONS and 0 < iterations2 <= MAX_PBKDF2_ITERATIONS):
            return None

        if not salt1 or not salt2:
            return None

        hash1 = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt1, iterations1)
        hash2 = hashlib.pbkdf2_hmac("sha256", hash1, salt2, iterations2)
        return f"{parts[4]}${hash2.hex()}"

    def extract_value(self, service, data, metric_name, metric_params, query_timestamp=None, result_list=None):
        if result_list is None:
            result_list = self.current_result_list

        data_path = metric_params.get("data_path")
        data_type = metric_params.get("type")
        data_next = metric_params.get("next")
        data_tags = metric_params.get("tags")
        value_function = metric_params.get("value_function")
        tags_function = metric_params.get("tags_function")                      # needs to return a dict
        timestamp_function = metric_params.get("timestamp_function")            # needs to return a datetime
        exclude_filter_function = metric_params.get("exclude_filter_function")  # needs to return a bool

        metric_value = None
        timestamp = query_timestamp

        if callable(exclude_filter_function):
            try:
                if exclude_filter_function(data) is True:
                    return
            except Exception as exc:  # noqa: BLE001 - service-supplied exclude filter is a plugin boundary
                log.error(f"Exclude filter for metric '{metric_name}' failed: {type(exc).__name__}")
                return

        if data_path is not None and value_function is not None:
            log.error("Attributes 'data_path' and 'value_function' can't be defined for the same entry "
                      f"at the same time: {metric_params}")
            return

        # first we try to use the value_function
        if value_function is not None:
            try:
                metric_value = value_function(data)
            except Exception as exc:  # noqa: BLE001 - service-supplied value function is a plugin boundary
                log.error(f"Value function for metric '{metric_name}' failed: {type(exc).__name__}")
                return

        elif data_path is not None:
            metric_value = grab(data, data_path, fallback="" if data_type is str else None)

        # always apply tags_function when present, regardless of static tags
        metric_tags = dict(data_tags or {})
        if callable(tags_function):
            try:
                generated_tags = tags_function(data)
            except Exception as exc:  # noqa: BLE001 - service-supplied tags function is a plugin boundary
                log.error(f"Tags function for metric '{metric_name}' failed: {type(exc).__name__}")
                return
            if not isinstance(generated_tags, dict):
                log.error(f"tags_function for metric '{metric_name}' did not return a dict")
                return
            metric_tags.update(generated_tags)

        if callable(timestamp_function):
            try:
                timestamp = timestamp_function(data)

                # make timestamp time zone aware if time zone is missing
                if timestamp.tzinfo is None or timestamp.tzinfo.utcoffset(timestamp) is None:
                    timestamp = timestamp.replace(tzinfo=self.config.timezone)

            except Exception as exc:  # noqa: BLE001 - service-supplied timestamp function is a plugin boundary
                log.error(f"Timestamp function for metric '{metric_name}' failed: {type(exc).__name__}")
                return

        if metric_value is None:
            log.error(f"Unable to extract '{metric_name}' from {type(data).__name__} response")
            log.debug("Unextractable '%s' source data of type %s", metric_name, type(data).__name__)
            return

        if data_type in [int, float, bool, str]:
            try:
                if data_type is bool:
                    metric_value = parse_bool(metric_value)
                else:
                    metric_value = data_type(metric_value)
            except Exception as e:  # noqa: BLE001 - Fritz!Box may return any garbage for a typed metric
                # Never log the value or the exception text: both can carry phone numbers or VPN addresses.
                log.error(f"Unable to convert {self.name} value for '{metric_name}' "
                          f"to '{data_type}': {type(e).__name__}")
                return

            metric = FritzMeasurement(metric_name, metric_value, data_type=data_type, box_tag=self.config.box_tag,
                                      additional_tags=metric_tags, timestamp=timestamp,
                                      is_event=service.is_event)

            result_list.append(metric)
            return

        if not isinstance(data_type, type):
            log.error(f"Invalid data type for metric '{metric_name}': {data_type!r}")
            return

        if not isinstance(metric_value, data_type):
            log.error(f"FritzBox metric type '{data_type}' for '{metric_name}' "
                      f"does not match '{type(metric_value)}' data")
            return

        if data_type is list and data_next is not None:
            for next_metric_value in metric_value:
                self.extract_value(service, next_metric_value, metric_name, data_next, query_timestamp, result_list)

            return

        if data_type is dict and data_next is not None:
            for next_metric_value in metric_value.values():
                self.extract_value(service, next_metric_value, metric_name, data_next, query_timestamp, result_list)

            return

        log.error(f"Unknown metric '{data_path}' from '{type(data).__name__}' data, with type '{type(metric_value)}' "
                  f"and defined type '{data_type}'")

    def query_service_data(self, service):

        if not isinstance(service, FritzBoxLuaService):
            log.error("Query service must be of type 'FritzBoxLuaService'")
            return

        if self.discovery_done is True and service.should_be_requested() is False:
            return

        service_and_version_name = f"{service.name} " \
                                   f"(Fritz!OS {service.os_min_versions} - {service.os_max_versions or 'latest'})"
        if self.discovery_done is False:
            if service.os_version_match(self.config.fw_version) is False:
                log.debug(f"FritzOS version {self.config.fw_version} not compatible with "
                          f"supported versions for '{service.name}': "
                          f"{service.os_min_versions} - {service.os_max_versions or 'latest'}")
                service.available = False
                return

            if service.link_type is not None and self.config.link_type != service.link_type:
                log.debug(
                    "Skipping '%s' metrics: requires %s connection (this device uses %s)",
                    service.name, service.link_type, self.config.link_type,
                )
                service.available = False
                return

        result = self.request(service, additional_params=service.params)

        if result is None:
            if self.shutting_down:
                log.debug("%s service '%s' returned no data during shutdown", self.name, service.name)
                return False
            if service.transient_failure is not None:
                log.debug("Unable to request %s service '%s', no data returned", self.name, service.name)
            else:
                log.error(f"Unable to request {self.name} service '{service.name}', no data returned")
            return False

        log.debug(f"Request {self.name} service '{service_and_version_name}' returned successfully")

        service.set_last_query_now()

        # all values of one service query share a timestamp so the writer can bundle them into one line
        query_timestamp = datetime.now(UTC)

        self.extract_service_values(service, result, query_timestamp)

        return True

    def extract_service_values(self, service, result, query_timestamp):
        """Extracts every value instance of a service into the result list."""
        # Services run in parallel threads: extract into a private list and merge it in one step,
        # so neither the event grouping nor other services' measurements can be disturbed.
        local_results: list = []
        for metric_name, metric_params in service.value_instances.items():
            self.extract_value(service, result, metric_name, metric_params, query_timestamp, local_results)

        if service.is_event:
            # The loop yields all entries of one metric before the next metric. Regroup per event
            # (stable, so field order is kept) so the writer can keep an event within one batch.
            local_results.sort(key=lambda m: (m.timestamp, sorted((k, str(v)) for k, v in m.tags.items())))

        with self._result_lock:
            self.current_result_list.extend(local_results)
