#!/usr/bin/env python3
#
# app/fritzbox/tr064/soap.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SOAP envelope building, response and fault parsing, SCPD type conversion.

No SOAP XML leaves this module: callers get plain dicts keyed by the SCPD
out-argument names. Parsing is namespace- and whitespace-agnostic.
"""

import logging
import re
from collections.abc import Mapping
from typing import Any
from xml.etree.ElementTree import Element
from xml.sax.saxutils import escape

from app.fritzbox.tr064.discovery import ActionDef
from app.fritzbox.tr064.exceptions import (
    TR064ActionNotFound,
    TR064Error,
    TR064MalformedXml,
    TR064NotAuthorized,
    TR064ProtocolError,
    TR064SoapFault,
    sanitize_text,
)
from app.fritzbox.tr064.xmlsafe import MAX_SOAP_BYTES, children, local_name, parse_xml

log = logging.getLogger("fritzFlux.tr064")

SOAP_ENVELOPE_NS = "http://schemas.xmlsoap.org/soap/envelope/"
SOAP_ENCODING_NS = "http://schemas.xmlsoap.org/soap/encoding/"
SOAP_CONTENT_TYPE = 'text/xml; charset="utf-8"'

# Fault code the box uses for "invalid action"; it is not an HTTP status.
FAULT_INVALID_ACTION = 401
FAULT_NOT_AUTHORIZED = 606

ArgumentValue = str | int | bool

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")
_SERVICE_TYPE = re.compile(r"[A-Za-z0-9:._\-]+")
_XML_FORBIDDEN = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_INTEGER = re.compile(r"[+-]?[0-9]{1,20}")
# UPnP integer types and their inclusive ranges; ``int`` declares none.
_INT_RANGES: dict[str, tuple[int | None, int | None]] = {
    "i1": (-(2**7), 2**7 - 1),
    "i2": (-(2**15), 2**15 - 1),
    "i4": (-(2**31), 2**31 - 1),
    "int": (None, None),
    "ui1": (0, 2**8 - 1),
    "ui2": (0, 2**16 - 1),
    "ui4": (0, 2**32 - 1),
}
_OMIT = object()


def soap_action_header(service_type: str, action: str) -> str:
    """Return the ``SOAPAction`` value (unquoted, the form the FRITZ!Box has been served so far)."""
    return f"{service_type}#{action}"


def format_argument(value: object) -> str:
    """Return the XML-escaped text of one input argument.

    ``bool`` becomes ``1``/``0``. ``None`` and other types are rejected rather
    than silently turned into ``0``.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if _XML_FORBIDDEN.search(value):
            raise TR064ProtocolError("argument value contains characters invalid in XML")
        return escape(value, {'"': "&quot;", "'": "&apos;"})
    raise TR064ProtocolError(f"unsupported argument value type: {type(value).__name__}")


def build_request(service_type: str, action: ActionDef, arguments: Mapping[str, object] | None) -> bytes:
    """Build the SOAP request body for ``action`` with validated arguments.

    Argument names must be in-arguments of the SCPD action; this closes the
    XML-injection path through names. Every in-argument is required (the SCPD
    knows no optional ones) and elements are written in SCPD order.
    """
    if not _SERVICE_TYPE.fullmatch(service_type) or not _NAME.fullmatch(action.name):
        raise TR064ProtocolError("service type or action name is not usable in a request")
    given = dict(arguments or {})
    parts: list[str] = []
    missing = [argument.name for argument in action.in_arguments if argument.name not in given]
    if missing:
        names = ", ".join(sanitize_text(name, 60) for name in missing)
        raise TR064ProtocolError(f"missing required arguments: {names}")
    for argument in action.in_arguments:
        if not _NAME.fullmatch(argument.name):
            raise TR064ProtocolError("argument name is not usable in a request")
        parts.append(f"<{argument.name}>{format_argument(given.pop(argument.name))}</{argument.name}>")
    if given:
        unknown = ", ".join(sanitize_text(name, 60) for name in sorted(map(str, given)))
        raise TR064ProtocolError(f"arguments not defined by the service description: {unknown}")
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<s:Envelope xmlns:s="{SOAP_ENVELOPE_NS}" s:encodingStyle="{SOAP_ENCODING_NS}"><s:Body>'
        f'<u:{action.name} xmlns:u="{service_type}">{"".join(parts)}</u:{action.name}>'
        "</s:Body></s:Envelope>"
    )
    return envelope.encode("utf-8")


def convert_value(data_type: str, text: str) -> Any:
    """Convert one out-argument by its SCPD ``dataType``; return ``_OMIT`` if not plausible.

    ``ui8``/``i8`` and ``dateTime`` stay strings: the application converts or
    slices them itself and a ``datetime`` would break that.
    """
    kind = data_type.lower()
    if kind in _INT_RANGES:
        stripped = text.strip()
        if not _INTEGER.fullmatch(stripped):
            return _OMIT
        number = int(stripped)
        low, high = _INT_RANGES[kind]
        if (low is not None and number < low) or (high is not None and number > high):
            return _OMIT
        return number
    if kind == "boolean":
        stripped = text.strip()
        return {"0": False, "1": True}.get(stripped, _OMIT)
    if kind == "uuid":
        return text.removeprefix("uuid:")
    return text


def _soap_body(root: Element) -> Element:
    if local_name(root.tag) != "Envelope":
        raise TR064MalformedXml("not a SOAP envelope")
    bodies = children(root, "Body")
    if not bodies:
        raise TR064MalformedXml("SOAP envelope has no body")
    return bodies[0]


def _fault_error(fault: Element, service: str, action: str) -> TR064Error:
    code: int | None = None
    description = ""
    string = ""
    for node in fault.iter():
        name = local_name(node.tag)
        text = (node.text or "").strip()
        if name == "errorCode" and code is None and _INTEGER.fullmatch(text):
            code = int(text)
        elif name == "errorDescription" and not description:
            description = text
        elif name == "faultstring" and not string:
            string = text
    if code == FAULT_INVALID_ACTION:
        return TR064ActionNotFound(service, action)
    if code == FAULT_NOT_AUTHORIZED:
        return TR064NotAuthorized(code, description or string)
    return TR064SoapFault(code, description or string)


def fault_from_document(data: bytes, service: str, action: str) -> TR064Error | None:
    """Return the typed fault carried by ``data``, or ``None`` if it holds no fault.

    A body that is not a parseable SOAP envelope yields ``None`` so the caller
    reports the HTTP status instead.
    """
    try:
        body = _soap_body(parse_xml(data, max_bytes=MAX_SOAP_BYTES))
    except TR064ProtocolError:
        return None
    faults = children(body, "Fault")
    return _fault_error(faults[0], service, action) if faults else None


def parse_response(data: bytes, service: str, action: ActionDef) -> dict[str, Any]:
    """Parse a successful SOAP answer into ``{out-argument: value}``.

    A fault inside an HTTP 200 answer is raised as its typed error. Anything
    that is not the expected ``<Action>Response`` fails closed. Out-arguments
    that are not in the SCPD are ignored; unconvertible values are omitted.
    """
    body = _soap_body(parse_xml(data, max_bytes=MAX_SOAP_BYTES))
    faults = children(body, "Fault")
    if faults:
        raise _fault_error(faults[0], service, action.name)
    responses = children(body, f"{action.name}Response")
    if not responses:
        raise TR064MalformedXml("SOAP body has no response element for the action")
    out_types = {argument.name: argument.data_type for argument in action.out_arguments}
    result: dict[str, Any] = {}
    for node in responses[0]:
        name = local_name(node.tag)
        data_type = out_types.get(name)
        if data_type is None:
            continue
        value = convert_value(data_type, node.text or "")
        if value is _OMIT:
            log.debug("Omitting out-argument %s: not a valid %s", sanitize_text(name, 60), sanitize_text(data_type, 20))
            continue
        result[name] = value
    return result
