#!/usr/bin/env python3
#
# tests/fakebox.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared FRITZ!Box fixture for the native TR-064 tests.

``FakeFritzBox`` serves realistic device descriptions, SCPDs and SOAP answers
through ``httpx.MockTransport``. The layout follows the two-description
structure of a real box (``igddesc.xml`` and ``tr64desc.xml``); it is an
assumption from the library structure, not a capture of a real device.
"""

import hashlib
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

import httpx

TR64_NS = "urn:dslforum-org:service:"
IGD_NS = "urn:schemas-upnp-org:service:"


@dataclass(frozen=True)
class FakeService:
    """One service as it appears in a description document."""

    name: str  # serviceId suffix, e.g. WANPPPConnection1
    type_name: str  # serviceType name incl. version, e.g. WANPPPConnection:1
    document: str  # "igddesc.xml" or "tr64desc.xml"
    depth: int = 1  # number of device levels above the service
    actions: dict[str, tuple[tuple[str, str, str], ...]] = field(default_factory=dict)

    @property
    def service_type(self) -> str:
        return (IGD_NS if self.document == "igddesc.xml" else TR64_NS) + self.type_name

    @property
    def service_id(self) -> str:
        if self.document == "igddesc.xml":
            return f"urn:upnp-org:serviceId:{self.name}"
        return f"urn:{self.name[:-1]}-com:serviceId:{self.name}"

    @property
    def control_url(self) -> str:
        prefix = "/igdupnp/control/" if self.document == "igddesc.xml" else "/upnp/control/"
        return prefix + self.name.lower()

    @property
    def scpd_url(self) -> str:
        return f"/{self.name.lower()}SCPD.xml"


# Action arguments: (name, direction, dataType). The related state variable is named after the argument.
def default_services() -> list[FakeService]:
    return [
        FakeService("DeviceInfo1", "DeviceInfo:1", "tr64desc.xml", actions={
            "GetInfo": (("NewModelName", "out", "string"), ("NewUpTime", "out", "ui4")),
        }),
        FakeService("Time1", "Time:1", "tr64desc.xml", actions={
            "GetInfo": (("NewCurrentLocalTime", "out", "string"),),
        }),
        FakeService("WANPPPConnection1", "WANPPPConnection:1", "tr64desc.xml", depth=2, actions={
            "GetInfo": (("NewUptime", "out", "ui4"), ("NewConnectionStatus", "out", "string")),
            "SetEnable": (("NewEnable", "in", "boolean"),),
        }),
        FakeService("WANDSLInterfaceConfig1", "WANDSLInterfaceConfig:1", "tr64desc.xml", depth=2, actions={
            "GetInfo": (("NewUpstreamCurrRate", "out", "ui4"),),
        }),
        FakeService("WANIPConnection1", "WANIPConnection:1", "tr64desc.xml", depth=2, actions={
            "GetInfo": (("NewExternalIPAddress", "out", "string"),),
        }),
        FakeService("WANIPConn1", "WANIPConnection:1", "igddesc.xml", depth=2, actions={
            "GetStatusInfo": (("NewUptime", "out", "ui4"), ("NewConnectionStatus", "out", "string")),
        }),
        FakeService("WANCommonIFC1", "WANCommonInterfaceConfig:1", "igddesc.xml", depth=1, actions={
            "GetCommonLinkProperties": (("NewLayer1UpstreamMaxBitRate", "out", "ui4"),),
        }),
    ]


def service_xml(service: FakeService) -> str:
    return (
        "<service>"
        f"<serviceType>{service.service_type}</serviceType>"
        f"<serviceId>{service.service_id}</serviceId>"
        f"<controlURL>{service.control_url}</controlURL>"
        f"<eventSubURL>{service.control_url}/event</eventSubURL>"
        f"<SCPDURL>{service.scpd_url}</SCPDURL>"
        "</service>"
    )


def description_xml(services: list[FakeService]) -> str:
    """Build a device description; ``depth`` places a service in nested devices."""
    levels: dict[int, str] = {}
    for service in services:
        levels[service.depth] = levels.get(service.depth, "") + service_xml(service)
    inner = ""
    for depth in sorted(levels, reverse=True):
        wrapped = f"<serviceList>{levels[depth]}</serviceList>"
        if inner:
            wrapped += f"<deviceList><device>{inner}</device></deviceList>"
        inner = wrapped
    return (
        '<?xml version="1.0"?>\n<root xmlns="urn:dslforum-org:device-1-0">'
        f"<specVersion><major>1</major><minor>0</minor></specVersion><device><deviceType>fake</deviceType>{inner}</device></root>"
    )


def scpd_xml(service: FakeService) -> str:
    variables: dict[str, str] = {}
    actions = ""
    for action, arguments in service.actions.items():
        listed = ""
        for name, direction, data_type in arguments:
            variables[name] = data_type
            listed += (
                f"<argument><name>{name}</name><direction>{direction}</direction>"
                f"<relatedStateVariable>{name}</relatedStateVariable></argument>"
            )
        actions += f"<action><name>{action}</name><argumentList>{listed}</argumentList></action>"
    table = "".join(f"<stateVariable><name>{n}</name><dataType>{t}</dataType></stateVariable>" for n, t in variables.items())
    return (
        '<?xml version="1.0"?><scpd xmlns="urn:dslforum-org:service-1-0">'
        f"<actionList>{actions}</actionList><serviceStateTable>{table}</serviceStateTable></scpd>"
    )


def soap_response(service_type: str, action: str, values: dict[str, str]) -> str:
    body = "".join(f"<{name}>{escape(value)}</{name}>" for name, value in values.items())
    return (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action}Response xmlns:u="{service_type}">{body}</u:{action}Response></s:Body></s:Envelope>'
    )


def soap_fault(code: int, description: str, *, pretty: bool = False) -> str:
    nl = "\n" if pretty else ""
    return (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>{nl}<s:Fault>'
        f"<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>{nl}"
        f'<UPnPError xmlns="urn:dslforum-org:control-1-0"><errorCode>{code}</errorCode>'
        f"<errorDescription>{escape(description)}</errorDescription></UPnPError>{nl}</detail></s:Fault></s:Body></s:Envelope>"
    )


_HASHES = {"MD5": hashlib.md5, "SHA-256": hashlib.sha256}
_AUTH_FIELDS = re.compile(r'(\w+)=("[^"]*"|[^,\s]*)')


class DigestVerifier:
    """Real HTTP digest verifier (MD5 or SHA-256): issues nonces and checks the response hash.

    Counters tell a test what the client did: ``anonymous`` (challenge round),
    ``stale`` (unknown nonce), ``bad_response`` (wrong credentials, realm, uri or qop)
    and ``accepted``; ``duplicate_nc`` lists replayed (nonce, nc) pairs.
    """

    def __init__(
        self,
        username: str,
        password: str,
        *,
        realm: str = "HTTPS Access",
        algorithm: str = "MD5",
        qop: str | None = "auth",
    ) -> None:
        self.username, self.password, self.realm = username, password, realm
        self.algorithm, self.qop = algorithm, qop
        self.anonymous = self.stale = self.bad_response = self.accepted = 0
        self.duplicate_nc: list[tuple[str, str]] = []
        self._nonces: set[str] = set()
        self._seen: set[tuple[str, str]] = set()
        self._lock = threading.RLock()

    def expire_nonces(self) -> None:
        """Invalidate every issued nonce, as a box does after its nonce lifetime."""
        with self._lock:
            self._nonces.clear()

    def _digest(self, text: str) -> str:
        return _HASHES[self.algorithm](text.encode()).hexdigest()

    def challenge(self) -> httpx.Response:
        nonce = secrets.token_hex(16)
        with self._lock:
            self._nonces.add(nonce)
        parts = [f'realm="{self.realm}"', f'nonce="{nonce}"']
        if self.qop:
            parts.append(f'qop="{self.qop}"')
        parts.append(f"algorithm={self.algorithm}")
        return httpx.Response(
            401,
            content=b"<html>unauthorized</html>",
            headers={"Content-Type": "text/html", "WWW-Authenticate": "Digest " + ", ".join(parts)},
        )

    def check(self, request: httpx.Request) -> httpx.Response | None:
        """Return a challenge response, or ``None`` when the request is authorised."""
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("digest "):
            with self._lock:
                self.anonymous += 1
            return self.challenge()
        fields = {k: v.strip('"') for k, v in _AUTH_FIELDS.findall(header[7:])}
        nonce, nc = fields.get("nonce", ""), fields.get("nc", "")
        with self._lock:
            known = nonce in self._nonces
            if not known:
                self.stale += 1
        if not known:
            return self.challenge()
        offered_qops = {value.strip() for value in self.qop.split(",")} if self.qop else {None}
        valid_qop = fields.get("qop") in offered_qops
        if fields.get("qop") == "auth":
            valid_qop = valid_qop and bool(re.fullmatch(r"[0-9a-fA-F]{8}", nc)) and int(nc, 16) > 0
            valid_qop = valid_qop and bool(fields.get("cnonce"))
        if not valid_qop or fields.get("realm") != self.realm or fields.get("uri") != request.url.raw_path.decode():
            with self._lock:
                self.bad_response += 1
            return self.challenge()
        ha1 = self._digest(f"{self.username}:{self.realm}:{self.password}")
        ha2 = self._digest(f"{request.method}:{request.url.raw_path.decode()}")
        if fields.get("qop") == "auth":
            expected = self._digest(f"{ha1}:{nonce}:{nc}:{fields.get('cnonce', '')}:auth:{ha2}")
        else:
            expected = self._digest(f"{ha1}:{nonce}:{ha2}")
        if fields.get("username") != self.username or fields.get("response") != expected:
            with self._lock:
                self.bad_response += 1
            return self.challenge()
        with self._lock:
            if nc and (nonce, nc) in self._seen:
                self.duplicate_nc.append((nonce, nc))
                return self.challenge()
            self._seen.add((nonce, nc))
            self.accepted += 1
        return None


class FakeFritzBox:
    """In-memory FRITZ!Box answering through ``httpx.MockTransport``."""

    def __init__(self, services: list[FakeService] | None = None, *, digest: DigestVerifier | None = None) -> None:
        self.services = list(default_services() if services is None else services)
        self.digest = digest
        self.digest_only_posts = False  # a real box serves its descriptions without a login
        self.requests: list[httpx.Request] = []
        self.calls: list[tuple[str, str]] = []  # (service name, action) of every SOAP POST
        self.responses: dict[str, tuple[int, str, str]] = {}  # path -> (status, content type, body)
        self.responders: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.hook: Callable[[httpx.Request], None] | None = None  # runs first; use to block or delay
        self._results: dict[tuple[str, str], str] = {}
        self._denied: dict[tuple[str, str], int] = {}
        self._transport_rules: list[tuple[str | None, type[httpx.TransportError], int | None]] = []
        self._transport_lock = threading.Lock()

    def drop(self, name: str) -> None:
        """Remove a service (by serviceId suffix) from the box."""
        self.services = [s for s in self.services if s.name != name]

    def override(self, path: str, body: str, *, status: int = 200, content_type: str = "text/xml") -> None:
        """Serve a fixed answer for ``path`` instead of the generated one."""
        self.responses[path] = (status, content_type, body)

    def respond(self, service: str, action: str, values: dict[str, str]) -> None:
        """Answer ``service``.``action`` with ``values`` (compact XML)."""
        svc = next((s for s in self.services if s.name == service), None)
        if svc is None:
            raise KeyError(f"FakeFritzBox offers no service {service!r}")
        self._results[(service, action)] = soap_response(svc.service_type, action, values)

    def respond_xml(self, service: str, action: str, xml: str) -> None:
        """Answer ``service``.``action`` with a hand-written body (pretty XML, bad XML, ...)."""
        self._results[(service, action)] = xml

    def fault(self, service: str, action: str, code: int, description: str = "fault", *, pretty: bool = False) -> None:
        """Answer ``service``.``action`` with a SOAP fault (HTTP 500)."""
        self._results[(service, action)] = soap_fault(code, description, pretty=pretty)

    def deny(self, service: str, action: str, status: int = 401) -> None:
        """Answer ``service``.``action`` with a bare HTTP ``status`` (no challenge) for an authorised user."""
        self._denied[(service, action)] = status

    def raise_transport(self, exc_type: type[httpx.TransportError], *, path: str | None = None, times: int | None = None) -> None:
        """Raise ``exc_type`` for requests to ``path`` (all paths if ``None``), ``times`` times (always if ``None``)."""
        with self._transport_lock:
            self._transport_rules.append((path, exc_type, times))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if self.hook is not None:
            self.hook(request)
        with self._transport_lock:
            for index, (rule_path, exc_type, times) in enumerate(self._transport_rules):
                if rule_path in (None, path) and times != 0:
                    if times is not None:
                        self._transport_rules[index] = (rule_path, exc_type, times - 1)
                    raise exc_type("simulated transport failure", request=request)
        if self.digest is not None and (request.method == "POST" or not self.digest_only_posts) and (
            challenge := self.digest.check(request)
        ) is not None:
            return challenge
        if path in self.responders:
            return self.responders[path](request)
        if path in self.responses:
            status, content_type, body = self.responses[path]
            return httpx.Response(status, content=body.encode(), headers={"Content-Type": content_type})
        if request.method == "POST":
            for service in self.services:
                if path == service.control_url:
                    return self._soap(service, request)
        for document in ("igddesc.xml", "tr64desc.xml"):
            if path == "/" + document:
                listed = [s for s in self.services if s.document == document]
                return self._xml(description_xml(listed)) if listed or document == "tr64desc.xml" else httpx.Response(404, text="")
        for service in self.services:
            if path == service.scpd_url:
                return self._xml(scpd_xml(service))
        return httpx.Response(404, content=b"<html>not found</html>", headers={"Content-Type": "text/html"})

    def _soap(self, service: FakeService, request: httpx.Request) -> httpx.Response:
        action = request.headers.get("soapaction", "").strip('"').rpartition("#")[2]
        key = (service.name, action)
        self.calls.append(key)
        if key in self._denied:
            return httpx.Response(self._denied[key], content=b"<html>denied</html>", headers={"Content-Type": "text/html"})
        if action not in service.actions:
            return self._soap_xml(soap_fault(401, "Invalid Action"), 500)
        body = self._results.get(key)
        if body is None:
            body = soap_response(service.service_type, action, {})
        return self._soap_xml(body, 500 if "<s:Fault>" in body else 200)

    @staticmethod
    def _soap_xml(body: str, status: int) -> httpx.Response:
        return httpx.Response(status, content=body.encode(), headers={"Content-Type": 'text/xml; charset="utf-8"'})

    @staticmethod
    def _xml(body: str) -> httpx.Response:
        return httpx.Response(200, content=body.encode(), headers={"Content-Type": "text/xml"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def client(self, base_url: str = "http://fritz.box:49000") -> httpx.Client:
        return httpx.Client(transport=self.transport(), base_url=base_url)

    def fetch(self, path: str) -> bytes:
        """GET ``path`` through the transport and return the body bytes."""
        with self.client() as client:
            return client.get(path).content

    def tr064_client(
        self,
        *,
        host: str = "fritz.box",
        port: int = 49000,
        username: str = "user",
        password: str = "secret",
        use_tls: bool = False,
        generation: int = 0,
        discover: bool = True,
        transport: httpx.BaseTransport | None = None,
    ):
        """Return a native ``TR064Client`` wired to this box (discovered unless ``discover=False``)."""
        from app.fritzbox.tr064.client import TR064_VERIFY_TLS, TR064Client

        client = TR064Client(
            host,
            port,
            username=username,
            password=password,
            use_tls=use_tls,
            verify=TR064_VERIFY_TLS,
            timeout=httpx.Timeout(40.0, connect=10.0),
            transport=transport or self.transport(),
            generation=generation,
        )
        if discover:
            client.discover()
        return client


def fail_scheme(box: FakeFritzBox, scheme: str = "https", exc_type: type[httpx.TransportError] = httpx.ConnectError) -> None:
    """Make every request over ``scheme`` fail in the connection phase (e.g. HTTPS port closed)."""

    def hook(request: httpx.Request) -> None:
        if request.url.scheme == scheme:
            raise exc_type("simulated connection failure", request=request)

    box.hook = hook


def attach_to_handler(monkeypatch, box: FakeFritzBox) -> list:
    """Route the handler's native ``TR064Client`` through ``box``; return the clients it creates.

    The real client class runs unchanged; only the transport is injected.
    """
    from app.fritzbox import handler as handler_module
    from app.fritzbox.tr064.client import TR064Client

    created: list[TR064Client] = []

    def build(*args, **kwargs) -> TR064Client:
        client = TR064Client(*args, transport=box.transport(), **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(handler_module, "TR064Client", build)
    return created


_IGD_TYPES = {"WANIPConn": "WANIPConnection:1", "WANCommonIFC": "WANCommonInterfaceConfig:1"}
_METRIC_TYPES = {"int": "ui4", "bool": "boolean", "float": "string", "str": "string"}


def box_for_services(services) -> FakeFritzBox:
    """Fake box offering every service and action the handler's service definitions query.

    ``WANIPConn`` and ``WANCommonIFC`` come from ``igddesc.xml`` like on a real box;
    each action returns the fields its service maps to metrics, typed by the metric suffix.
    """
    from app.fritzbox.tr064.discovery import service_key

    offered = []
    for service in services:
        arguments = tuple(
            (field_name, "out", _METRIC_TYPES.get(metric.partition(":")[2], "string"))
            for field_name, metric in service.value_instances.items()
        )
        igd = service.name in _IGD_TYPES
        type_name = _IGD_TYPES.get(service.name) or (service.name if ":" in service.name else service.name + ":1")
        offered.append(FakeService(
            service_key(service.name), type_name, "igddesc.xml" if igd else "tr64desc.xml",
            actions={action.name: arguments for action in service.actions},
        ))
    return FakeFritzBox(offered)


def deny_all_actions(box: FakeFritzBox, *, status: int = 401, except_for: tuple[str, str] | None = None) -> None:
    """Answer every SOAP action of ``box`` but ``except_for`` with a bare HTTP ``status`` (descriptions stay readable)."""
    for service in box.services:
        for action in service.actions:
            if (service.name, action) != except_for:
                box.deny(service.name, action, status)
