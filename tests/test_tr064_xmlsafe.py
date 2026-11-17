#!/usr/bin/env python3
#
# tests/test_tr064_xmlsafe.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Bounded XML parsing: size, depth, element count, DTD and entities, error text."""

import pytest
from defusedxml.ElementTree import fromstring

from app.fritzbox.tr064.exceptions import (
    TR064MalformedXml,
    TR064ProtocolError,
    sanitize_text,
)
from app.fritzbox.tr064.xmlsafe import (
    MAX_DESCRIPTION_BYTES,
    MAX_SCPD_BYTES,
    MAX_SOAP_BYTES,
    MAX_XML_DEPTH,
    MAX_XML_ELEMENTS,
    child_text,
    local_name,
    parse_xml,
)

LIMIT = 1000


def nested(depth: int) -> bytes:
    return b"<a>" * depth + b"</a>" * depth


def padded(size: int) -> bytes:
    """A well-formed document of exactly ``size`` bytes."""
    head, tail = b"<a>", b"</a>"
    return head + b" " * (size - len(head) - len(tail)) + tail


def test_limit_constants_match_the_specification():
    assert MAX_DESCRIPTION_BYTES == 512 * 1024
    assert MAX_SCPD_BYTES == 1024 * 1024
    assert MAX_SOAP_BYTES == 1024 * 1024
    assert MAX_XML_DEPTH == 32
    assert MAX_XML_ELEMENTS == 20_000


def test_limit_size_accepts_the_limit_and_rejects_one_byte_more():
    assert parse_xml(padded(LIMIT), max_bytes=LIMIT).tag == "a"
    with pytest.raises(TR064ProtocolError, match="larger than"):
        parse_xml(padded(LIMIT + 1), max_bytes=LIMIT)


def test_limit_depth_accepts_32_and_rejects_33():
    assert parse_xml(nested(MAX_XML_DEPTH), max_bytes=10_000).tag == "a"
    with pytest.raises(TR064ProtocolError, match="nesting"):
        parse_xml(nested(MAX_XML_DEPTH + 1), max_bytes=10_000)


def test_limit_elements_accepts_20000_and_rejects_20001():
    def flat(total: int) -> bytes:
        """A document with exactly ``total`` elements: the root plus ``total - 1`` children."""
        return b"<r>" + b"<e/>" * (total - 1) + b"</r>"

    accepted = parse_xml(flat(MAX_XML_ELEMENTS), max_bytes=1_000_000)
    assert sum(1 for _ in accepted.iter()) == MAX_XML_ELEMENTS == 20_000
    over = flat(MAX_XML_ELEMENTS + 1)
    assert sum(1 for _ in fromstring(over).iter()) == 20_001
    with pytest.raises(TR064ProtocolError, match="elements"):
        parse_xml(over, max_bytes=1_000_000)


@pytest.mark.parametrize(
    "document",
    [
        b"<!DOCTYPE a><a/>",
        b'<!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>',
        b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><a>&x;</a>',
        b'<!DOCTYPE a [<!ENTITY a "x"><!ENTITY b "&a;&a;&a;&a;">]><a>&b;</a>',
    ],
)
def test_limit_doctype_and_entities_are_rejected_not_expanded(document):
    with pytest.raises(TR064ProtocolError, match="forbidden XML construct") as info:
        parse_xml(document, max_bytes=10_000)
    assert not isinstance(info.value, TR064MalformedXml)


@pytest.mark.parametrize("document", [b"", b"not xml", b"<a><b></a>", b"\xff\xfe<a/>", b"<a>&undefined;</a>"])
def test_malformed_xml_is_a_distinct_protocol_error(document):
    with pytest.raises(TR064MalformedXml):
        parse_xml(document, max_bytes=10_000)


def test_helpers_ignore_namespaces_and_whitespace():
    root = parse_xml(b'<n:r xmlns:n="urn:x"><n:v> 5 </n:v></n:r>', max_bytes=1000)
    assert local_name(root.tag) == "r"
    assert child_text(root, "v") == "5"
    assert child_text(root, "missing") is None


def test_error_text_is_bounded_and_free_of_control_characters():
    text = sanitize_text("a\r\nFORGED \x1b[31m line\x00" + "x" * 500)
    assert "\n" not in text and "\r" not in text and "\x1b" not in text and "\x00" not in text
    assert len(text) <= 203
