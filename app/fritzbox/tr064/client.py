#!/usr/bin/env python3
#
# app/fritzbox/tr064/client.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Synchronous native TR-064 client.

The client reports facts only (HTTP status, SOAP fault, transport failure). It
never counts logins, never judges credentials, never retries and never
reconnects; the handler owns all of that. One client is one session and one
discovery generation.
"""

import logging
import ssl
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, NoReturn

import httpx

from app.fritzbox.tr064.deadline import RequestDeadline, read_bounded
from app.fritzbox.tr064.discovery import (
    IGD_DESCRIPTION,
    TR64_DESCRIPTION,
    Discovery,
    Scpd,
    ServiceInfo,
    build_discovery,
    parse_scpd,
    validate_path,
)
from app.fritzbox.tr064.exceptions import (
    TR064ActionNotFound,
    TR064ClientClosed,
    TR064ConnectError,
    TR064ConnectTimeout,
    TR064Error,
    TR064HttpError,
    TR064HttpForbidden,
    TR064HttpUnauthorized,
    TR064ProtocolError,
    TR064ResourceMissing,
    TR064ServiceNotFound,
    TR064TimeoutError,
    TR064TransportError,
)
from app.fritzbox.tr064.soap import (
    SOAP_CONTENT_TYPE,
    build_request,
    fault_from_document,
    parse_response,
    soap_action_header,
)
from app.fritzbox.tr064.xmlsafe import (
    MAX_DESCRIPTION_BYTES,
    MAX_SCPD_BYTES,
    MAX_SOAP_BYTES,
)

log = logging.getLogger("fritzFlux.tr064")

# TR-064 keeps unverified TLS for private box certificates; the accepted trust model
# is documented in docs/development/architecture.md. CA support would change that model.
TR064_VERIFY_TLS: bool = False

MAX_CONNECTIONS = 8
MAX_KEEPALIVE_CONNECTIONS = 4
_NO_BODY_STATUS = frozenset({401, 403})


def _raised_in_httpx_auth(exc: BaseException) -> bool:
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_globals.get("__name__") == "httpx._auth":
            return True
        tb = tb.tb_next
    return False


@dataclass(frozen=True, slots=True)
class _Reply:
    status: int
    media_type: str
    body: bytes


def _origin_of(url: httpx.URL) -> tuple[str, str, int | None]:
    default = {"http": 80, "https": 443}.get(url.scheme)
    return url.scheme, url.host, url.port if url.port is not None else default


def request_url(base_url: str, path: str) -> httpx.URL:
    """Return ``base_url + path`` after proving it targets exactly the configured origin.

    Defense in depth next to the path rules in discovery: userinfo, another
    host, another port or another scheme (``@evil/x``, ``//evil/x``) is refused
    before any request, and so is any credential-bearing traffic to it.
    """
    validate_path(path)
    try:
        base = httpx.URL(base_url)
        url = httpx.URL(base_url + path)
    except httpx.InvalidURL:
        raise TR064ProtocolError("request URL is not valid") from None
    if url.userinfo or _origin_of(url) != _origin_of(base):
        raise TR064ProtocolError("request URL leaves the configured FRITZ!Box origin")
    return url


def _format_host(host: str) -> str:
    bare = host.strip()
    if bare.startswith("[") and bare.endswith("]"):
        bare = bare[1:-1]
    if not bare or any(ch in bare for ch in "/@#?\\[] \t\r\n"):
        raise ValueError("invalid FRITZ!Box host")
    return f"[{bare}]" if ":" in bare else bare


def _raise_for_status(status: int) -> None:
    if 300 <= status < 400:
        raise TR064HttpError(status, "redirects are not followed")
    if status == 401:
        raise TR064HttpUnauthorized()
    if status == 403:
        raise TR064HttpForbidden()


class TR064Client:
    """One TR-064 session against one FRITZ!Box (``discover()`` before use).

    Thread safe: one pooled ``httpx.Client``, immutable discovery data, one lock
    per service for the lazy SCPD load and one digest state per thread.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        username: str,
        password: str,
        use_tls: bool,
        verify: bool | ssl.SSLContext,
        timeout: httpx.Timeout,
        transport: httpx.BaseTransport | None = None,
        generation: int = 0,
    ) -> None:
        if not 0 < port < 65536:
            raise ValueError("invalid FRITZ!Box port")
        self._host = _format_host(host)
        self._port = port
        self._use_tls = use_tls
        self._base_url = f"{'https' if use_tls else 'http'}://{self._host}:{port}"
        try:
            httpx.URL(self._base_url)
        except httpx.InvalidURL:
            raise ValueError("invalid FRITZ!Box host") from None
        self._credentials = (username, password)
        self._generation = generation
        self._local = threading.local()
        self._closed = threading.Event()
        self._state_lock = threading.Lock()
        self._discovery: Discovery | None = None
        self._scpds: dict[str, Scpd] = {}
        self._scpd_locks: dict[str, threading.Lock] = {}
        # No redirects and no proxy variables: credentials must only reach the configured origin.
        self._http = httpx.Client(
            verify=verify,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=MAX_CONNECTIONS, max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS),
            headers={"Accept-Encoding": "identity"},
        )

    def __repr__(self) -> str:
        return f"TR064Client(base_url={self._base_url!r}, generation={self._generation}, closed={self.closed})"

    def __reduce__(self) -> NoReturn:
        raise TypeError("TR064Client cannot be pickled")

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    @property
    def use_tls(self) -> bool:
        return self._use_tls

    def close(self) -> None:
        """Refuse new calls, then close the pool; idempotent and thread safe.

        A request in flight either completes or fails with ``TR064ClientClosed``.
        """
        with self._state_lock:
            if self._closed.is_set():
                return
            self._closed.set()
        self._http.close()

    def _auth(self) -> httpx.DigestAuth:
        # DigestAuth keeps an unsynchronised nonce counter, so each thread owns one.
        auth = getattr(self._local, "auth", None)
        if auth is None:
            auth = self._local.auth = httpx.DigestAuth(*self._credentials)
        return auth

    def _raise_transport(self, exc: Exception) -> NoReturn:
        if self._closed.is_set():
            raise TR064ClientClosed("client was closed during the request") from exc
        if isinstance(exc, httpx.ConnectTimeout):
            raise TR064ConnectTimeout("connection timed out") from exc
        if isinstance(exc, httpx.ConnectError):
            raise TR064ConnectError("connection failed") from exc
        if isinstance(exc, httpx.TimeoutException):
            raise TR064TimeoutError("request timed out") from exc
        if type(exc) is httpx.ProtocolError or isinstance(exc, (NotImplementedError, KeyError, ValueError)):
            # DigestAuth raises these for challenges it cannot answer.
            raise TR064ProtocolError("unsupported digest challenge") from exc
        if isinstance(exc, httpx.DecodingError | httpx.InvalidURL):
            raise TR064ProtocolError(f"invalid response: {type(exc).__name__}") from exc
        raise TR064TransportError(f"transport error: {type(exc).__name__}") from exc

    def _exchange(
        self,
        method: str,
        path: str,
        max_bytes: int,
        *,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> _Reply:
        if self._closed.is_set():
            raise TR064ClientClosed("client is closed")
        url = request_url(self._base_url, path)
        deadline = RequestDeadline(self._http.timeout)
        try:
            with self._stream_authenticated(method, url, content, headers, deadline) as response:
                media_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
                if response.status_code in _NO_BODY_STATUS or 300 <= response.status_code < 400:
                    return _Reply(response.status_code, media_type, b"")
                return _Reply(response.status_code, media_type, read_bounded(response, max_bytes, deadline))
        except TR064Error:
            raise

        except httpx.RequestError as exc:
            self._raise_transport(exc)
        except (NotImplementedError, KeyError, ValueError) as exc:
            # Only a failing digest challenge maps to a typed error; other bugs must surface.
            if not _raised_in_httpx_auth(exc):
                raise
            self._raise_transport(exc)
        except RuntimeError as exc:
            if self._closed.is_set():
                raise TR064ClientClosed("client was closed during the request") from exc
            raise

    @contextmanager
    def _stream_authenticated(
        self, method: str, url: httpx.URL, content: bytes | None, headers: Mapping[str, str] | None,
        deadline: RequestDeadline,
    ) -> Iterator[httpx.Response]:
        request = self._http.build_request(method, url, content=content, headers=headers)
        flow = self._auth().sync_auth_flow(request)
        try:
            request = next(flow)
            while True:
                # The digest round trip sends twice; each send gets only what the deadline has left.
                request.extensions["timeout"] = deadline.timeout_for_next_send(self._http.timeout).as_dict()
                response = self._http.send(request, stream=True, auth=None)
                try:
                    try:
                        request = flow.send(response)
                    except StopIteration:
                        yield response
                        return
                    # Digest needs the challenge headers only; never buffer an intermediate body.
                finally:
                    response.close()
        finally:
            flow.close()

    def _get(self, path: str, max_bytes: int) -> _Reply:
        reply = self._exchange("GET", path, max_bytes)
        _raise_for_status(reply.status)
        return reply

    @staticmethod
    def _document_or_none(reply: _Reply) -> bytes | None:
        # A missing document answers 404 or an html page; both mean "not offered".
        if reply.status in (404, 410):
            return None
        if reply.status != 200:
            raise TR064HttpError(reply.status)
        return None if reply.media_type == "text/html" else reply.body

    def _optional_igd_document(self) -> bytes | None:
        # The old library ignored any failure here; transport and limit errors still propagate.
        reply = self._exchange("GET", "/" + IGD_DESCRIPTION, MAX_DESCRIPTION_BYTES)
        if reply.status != 200 or reply.media_type == "text/html":
            log.debug("Optional %s not offered (HTTP %d)", IGD_DESCRIPTION, reply.status)
            return None
        return reply.body

    def discover(self) -> None:
        """Fetch ``igddesc.xml`` (optional) and ``tr64desc.xml`` (required); no SCPD is loaded.

        ``igddesc.xml`` is skipped on any non-200 status. Raises
        ``TR064AccessDisabled`` when ``tr64desc.xml`` is absent. A repeated
        call on an already discovered client does nothing.
        """
        with self._state_lock:
            if self._discovery is not None:
                return
        igd = self._optional_igd_document()
        tr64 = self._document_or_none(self._get("/" + TR64_DESCRIPTION, MAX_DESCRIPTION_BYTES))
        discovery = build_discovery(igd, tr64)
        with self._state_lock:
            if self._discovery is None:
                self._scpd_locks = {key: threading.Lock() for key in discovery.services}
                self._discovery = discovery
        log.debug("Discovered %d TR-064 services (generation %d)", len(discovery.services), self._generation)

    def _resolve(self, service: str) -> ServiceInfo:
        discovery = self._discovery
        if discovery is None:
            raise TR064Error("discover() has not been called")
        return discovery.resolve(service)

    def _scpd(self, info: ServiceInfo) -> Scpd:
        scpd = self._scpds.get(info.key)
        if scpd is not None:
            return scpd
        with self._scpd_locks[info.key]:
            scpd = self._scpds.get(info.key)
            if scpd is None:
                reply = self._get(info.scpd_url, MAX_SCPD_BYTES)
                body = self._document_or_none(reply)
                if body is None:
                    raise TR064ResourceMissing(404, "service description missing")
                scpd = parse_scpd(body)
                self._scpds[info.key] = scpd
        return scpd

    def has_action(self, service: str, action: str) -> bool:
        """Return whether the SCPD of ``service`` lists ``action`` (False if either is absent).

        Raises ``TR064Error`` when the SCPD cannot be loaded.
        """
        if self._closed.is_set():
            raise TR064ClientClosed("client is closed")
        try:
            info = self._resolve(service)
        except TR064ServiceNotFound:
            return False
        return self._scpd(info).has_action(action)

    def call_action(
        self,
        service: str,
        action: str,
        /,
        arguments: Mapping[str, str | int | bool] | None = None,
    ) -> dict[str, Any]:
        """Call ``action`` of ``service`` and return the converted out-arguments.

        Raises ``TR064ServiceNotFound``/``TR064ActionNotFound`` before any request
        when the discovery data lacks them, and the typed fact otherwise (HTTP
        status, SOAP fault, transport failure).
        """
        if self._closed.is_set():
            raise TR064ClientClosed("client is closed")
        info = self._resolve(service)
        action_def = self._scpd(info).actions.get(action)
        if action_def is None:
            raise TR064ActionNotFound(service, action)
        payload = build_request(info.service_type, action_def, arguments)
        headers = {"Content-Type": SOAP_CONTENT_TYPE, "SOAPAction": soap_action_header(info.service_type, action)}
        reply = self._exchange("POST", info.control_url, MAX_SOAP_BYTES, content=payload, headers=headers)
        _raise_for_status(reply.status)
        if reply.status in (404, 410):
            raise TR064ResourceMissing(reply.status)
        if reply.status == 200:
            return parse_response(reply.body, service, action_def)
        fault = fault_from_document(reply.body, service, action)
        if fault is not None:
            raise fault
        raise TR064HttpError(reply.status)
