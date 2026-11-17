#!/usr/bin/env python3
#
# app/fritzbox/tr064/xmlsafe.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Bounded, DTD-free XML parsing for FRITZ!Box documents.

The box is untrusted input while TLS is unverified or the HTTP fallback is
active. Parsing goes through ``defusedxml`` (no DTD, no entities) and the
decoded size, nesting depth and element count are capped.
"""

from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from app.fritzbox.tr064.exceptions import TR064MalformedXml, TR064ProtocolError

MAX_DESCRIPTION_BYTES = 512 * 1024
MAX_SCPD_BYTES = 1024 * 1024
MAX_SOAP_BYTES = 1024 * 1024
MAX_XML_DEPTH = 32
MAX_XML_ELEMENTS = 20_000


def local_name(tag: object) -> str:
    """Return the tag name without its ``{namespace}`` prefix."""
    text = tag if isinstance(tag, str) else ""
    return text.rpartition("}")[2]


def children(element: Element, name: str) -> list[Element]:
    """Return the direct children whose local name is ``name``."""
    return [child for child in element if local_name(child.tag) == name]


def child_text(element: Element, name: str) -> str | None:
    """Return the stripped text of the first direct child ``name``, or ``None``."""
    for child in element:
        if local_name(child.tag) == name:
            return (child.text or "").strip()
    return None


def _check_shape(root: Element, max_depth: int, max_elements: int) -> None:
    # Iterative walk: a recursive one would itself be a depth hazard.
    count = 0
    stack: list[tuple[Element, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        count += 1
        if depth > max_depth:
            raise TR064ProtocolError(f"XML nesting deeper than {max_depth}")
        if count > max_elements:
            raise TR064ProtocolError(f"XML has more than {max_elements} elements")
        stack.extend((child, depth + 1) for child in element)


def parse_xml(
    data: bytes,
    *,
    max_bytes: int,
    max_depth: int = MAX_XML_DEPTH,
    max_elements: int = MAX_XML_ELEMENTS,
) -> Element:
    """Parse ``data`` into an element tree within the safety limits.

    Raises ``TR064ProtocolError`` for a limit, DTD or entity violation and its
    subclass ``TR064MalformedXml`` when the bytes are not well-formed XML.
    """
    if len(data) > max_bytes:
        raise TR064ProtocolError(f"XML document larger than {max_bytes} bytes")
    try:
        root = fromstring(data, forbid_dtd=True)
    except DefusedXmlException as exc:
        raise TR064ProtocolError(f"forbidden XML construct: {type(exc).__name__}") from None
    except (ParseError, ValueError, UnicodeError) as exc:
        raise TR064MalformedXml(f"malformed XML: {type(exc).__name__}") from None
    _check_shape(root, max_depth, max_elements)
    return root
