#!/usr/bin/env python3
#
# app/fritzbox/tr064/discovery.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Device description and SCPD models, service resolution, request path rules.

Services are keyed by the last segment of
the ``serviceId``, never by ``serviceType``. The discovered ``serviceType`` is
what the SOAP layer later uses for the namespace and the ``SOAPAction``.
"""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal
from urllib.parse import unquote
from xml.etree.ElementTree import Element

from app.fritzbox.tr064.exceptions import (
    TR064AccessDisabled,
    TR064MalformedXml,
    TR064ProtocolError,
    TR064ServiceNotFound,
)
from app.fritzbox.tr064.xmlsafe import (
    MAX_DESCRIPTION_BYTES,
    MAX_SCPD_BYTES,
    child_text,
    children,
    local_name,
    parse_xml,
)

log = logging.getLogger("fritzFlux.tr064")

MAX_SERVICES = 100
MAX_ACTIONS = 500
MAX_ARGUMENTS = 50
MAX_STATE_VARIABLES = 1000
MAX_NAME_LENGTH = 128
MAX_PATH_LENGTH = 256

IGD_DESCRIPTION = "igddesc.xml"
TR64_DESCRIPTION = "tr64desc.xml"

Origin = Literal["igddesc.xml", "tr64desc.xml"]

_PATH_FORBIDDEN = re.compile(r"[\x00-\x20\x7f-\U0010ffff\\@#?]")
_MAX_DECODE_ROUNDS = 3


def validate_path(path: str) -> str:
    """Return ``path`` when it is a plain absolute path on the configured origin.

    Absolute URLs, scheme-relative URLs (``//host``), userinfo, query, fragment,
    backslashes, control characters and dot segments are rejected, also when
    percent-encoded (repeatedly), so a hostile description cannot redirect
    authenticated requests or climb out of the service tree.
    """
    if not path or len(path) > MAX_PATH_LENGTH:
        raise TR064ProtocolError("service path empty or too long")
    if not path.startswith("/") or path.startswith("//"):
        raise TR064ProtocolError("service path must start with exactly one '/'")
    candidate = path
    for _ in range(_MAX_DECODE_ROUNDS + 1):
        if _PATH_FORBIDDEN.search(candidate):
            raise TR064ProtocolError("service path contains forbidden characters")
        if any(segment in (".", "..") for segment in candidate.split("/")):
            raise TR064ProtocolError("service path contains dot segments")
        try:
            decoded = unquote(candidate, errors="strict")
        except UnicodeDecodeError:
            raise TR064ProtocolError("service path contains invalid percent-encoding") from None
        if decoded == candidate:
            return path
        candidate = decoded
    raise TR064ProtocolError("service path is encoded too deeply")


def service_key(name: str) -> str:
    """Return the discovery key for an application service name.

    ``WANPPPConnection:1`` becomes ``WANPPPConnection1``; a name without a
    trailing digit gets ``1``. An explicit version is never mapped elsewhere.
    """
    if not name:
        raise TR064ProtocolError("empty service name")
    if ":" in name:
        base, number = name.split(":", 1)
        if not base or not (number.isascii() and number.isdigit()):
            raise TR064ProtocolError("invalid service name version")
        return base + number
    return name if name[-1].isdigit() else name + "1"


@dataclass(frozen=True, slots=True)
class ServiceInfo:
    """One service from a device description."""

    key: str
    service_type: str
    service_id: str
    control_url: str
    scpd_url: str
    origin: Origin


@dataclass(frozen=True, slots=True)
class Argument:
    """One SCPD action argument with its resolved data type."""

    name: str
    direction: Literal["in", "out"]
    data_type: str


@dataclass(frozen=True, slots=True)
class ActionDef:
    """One SCPD action."""

    name: str
    arguments: tuple[Argument, ...]

    @property
    def in_arguments(self) -> tuple[Argument, ...]:
        return tuple(arg for arg in self.arguments if arg.direction == "in")

    @property
    def out_arguments(self) -> tuple[Argument, ...]:
        return tuple(arg for arg in self.arguments if arg.direction == "out")


@dataclass(frozen=True, slots=True)
class Scpd:
    """Parsed service description: the actions of one service."""

    actions: Mapping[str, ActionDef] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "actions", MappingProxyType(dict(self.actions)))

    def has_action(self, name: str) -> bool:
        return name in self.actions


@dataclass(frozen=True, slots=True)
class Discovery:
    """Immutable result of one discovery run."""

    services: Mapping[str, ServiceInfo]

    def __post_init__(self) -> None:
        object.__setattr__(self, "services", MappingProxyType(dict(self.services)))

    def resolve(self, name: str) -> ServiceInfo:
        """Return the service for an application name or raise ``TR064ServiceNotFound``."""
        key = service_key(name)
        try:
            return self.services[key]
        except KeyError:
            raise TR064ServiceNotFound(name, key) from None


def _read_service(element: Element, origin: Origin) -> ServiceInfo | None:
    values = {name: child_text(element, name) for name in ("serviceType", "serviceId", "controlURL", "SCPDURL")}
    if not all(values.values()):
        log.debug("Skipping incomplete service entry in %s", origin)
        return None
    service_id = values["serviceId"]
    key = service_id.rsplit(":", 1)[-1]
    if not key:
        log.debug("Skipping service entry with empty key in %s", origin)
        return None
    if len(service_id) > MAX_NAME_LENGTH or len(values["serviceType"]) > MAX_NAME_LENGTH:
        raise TR064ProtocolError("serviceId or serviceType too long")
    return ServiceInfo(
        key=key,
        service_type=values["serviceType"],
        service_id=service_id,
        control_url=validate_path(values["controlURL"]),
        scpd_url=validate_path(values["SCPDURL"]),
        origin=origin,
    )


def parse_description(data: bytes, origin: Origin) -> dict[str, ServiceInfo]:
    """Parse one device description into services keyed by ``serviceId`` suffix.

    Walks ``device``/``deviceList``/``serviceList`` at any nesting depth (bounded
    by the XML depth limit). A repeated key inside one document keeps the last
    entry.
    """
    root = parse_xml(data, max_bytes=MAX_DESCRIPTION_BYTES)
    if local_name(root.tag) != "root":
        raise TR064MalformedXml("not a device description")
    services: dict[str, ServiceInfo] = {}
    devices = children(root, "device")
    while devices:
        device = devices.pop()
        for service_list in children(device, "serviceList"):
            for entry in children(service_list, "service"):
                info = _read_service(entry, origin)
                if info is None:
                    continue
                if info.key in services:
                    log.debug("Service key %s repeated in %s; last entry wins", info.key, origin)
                services[info.key] = info
                if len(services) > MAX_SERVICES:
                    raise TR064ProtocolError(f"more than {MAX_SERVICES} services in {origin}")
        for device_list in children(device, "deviceList"):
            devices.extend(children(device_list, "device"))
    return services


def build_discovery(igd_document: bytes | None, tr64_document: bytes | None) -> Discovery:
    """Merge both descriptions into one ``Discovery``.

    ``igd_document`` is optional: ``None`` or a document that is not XML is
    skipped. ``tr64_document`` is mandatory: only ``None`` (the box does not
    offer it) means TR-064 is switched off (``TR064AccessDisabled``). A damaged
    or unexpected body raises ``TR064MalformedXml`` and limit or DTD violations
    ``TR064ProtocolError``: a broken answer proves nothing about the setting.
    On a key collision ``tr64desc.xml`` wins.
    """
    services: dict[str, ServiceInfo] = {}
    if igd_document is not None:
        try:
            services.update(parse_description(igd_document, IGD_DESCRIPTION))
        except TR064MalformedXml:
            log.debug("%s is not a device description; skipped", IGD_DESCRIPTION)
    if tr64_document is None:
        raise TR064AccessDisabled("tr64desc.xml is not available")
    tr64_services = parse_description(tr64_document, TR64_DESCRIPTION)
    for key, info in tr64_services.items():
        previous = services.get(key)
        if previous is not None:
            log.debug("Service key %s in both descriptions (%s, %s); tr64desc wins", key, previous.service_type, info.service_type)
        services[key] = info
    if len(services) > MAX_SERVICES:
        raise TR064ProtocolError(f"more than {MAX_SERVICES} services discovered")
    return Discovery(services=services)


def parse_scpd(data: bytes) -> Scpd:
    """Parse a service description document into actions with typed arguments."""
    root = parse_xml(data, max_bytes=MAX_SCPD_BYTES)
    if local_name(root.tag) != "scpd":
        raise TR064MalformedXml("not a service description")
    state_types: dict[str, str] = {}
    variable_count = 0
    for table in children(root, "serviceStateTable"):
        for variable in children(table, "stateVariable"):
            variable_count += 1
            if variable_count > MAX_STATE_VARIABLES:
                raise TR064ProtocolError(f"more than {MAX_STATE_VARIABLES} state variables")
            name, data_type = child_text(variable, "name"), child_text(variable, "dataType")
            if name:
                state_types[name] = (data_type or "string").lower()
    actions: dict[str, ActionDef] = {}
    action_count = 0
    for action_list in children(root, "actionList"):
        for entry in children(action_list, "action"):
            action_count += 1
            if action_count > MAX_ACTIONS:
                raise TR064ProtocolError(f"more than {MAX_ACTIONS} actions")
            action = _read_action(entry, state_types)
            if action is not None:
                actions[action.name] = action
    return Scpd(actions=actions)


def _read_action(entry: Element, state_types: Mapping[str, str]) -> ActionDef | None:
    name = child_text(entry, "name")
    if not name:
        return None
    if len(name) > MAX_NAME_LENGTH:
        raise TR064ProtocolError("action name too long")
    arguments: list[Argument] = []
    argument_count = 0
    for argument_list in children(entry, "argumentList"):
        for argument in children(argument_list, "argument"):
            argument_count += 1
            if argument_count > MAX_ARGUMENTS:
                raise TR064ProtocolError(f"more than {MAX_ARGUMENTS} arguments in one action")
            arg_name = child_text(argument, "name")
            direction = (child_text(argument, "direction") or "").lower()
            if not arg_name or direction not in ("in", "out"):
                continue
            if len(arg_name) > MAX_NAME_LENGTH:
                raise TR064ProtocolError("argument name too long")
            related = child_text(argument, "relatedStateVariable") or ""
            # Unresolvable variables stay strings: the caller must not guess numbers.
            arguments.append(Argument(arg_name, direction, state_types.get(related, "string")))
    return ActionDef(name=name, arguments=tuple(arguments))

