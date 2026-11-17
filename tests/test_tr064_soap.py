#!/usr/bin/env python3
#
# tests/test_tr064_soap.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SOAP envelope, escaping, response and fault parsing, type conversion."""

import pytest
from defusedxml.ElementTree import fromstring

from app.fritzbox.tr064.exceptions import (
    TR064ActionNotFound,
    TR064HttpError,
    TR064HttpUnauthorized,
    TR064NotAuthorized,
    TR064ProtocolError,
    TR064ServiceNotFound,
    TR064SoapFault,
)
from app.fritzbox.tr064.soap import _OMIT, convert_value
from tests.fakebox import FakeFritzBox, FakeService, soap_fault

DEVICE_CONTROL = "/upnp/control/deviceinfo1"
CONV = FakeService(
    "Conv1",
    "Conv:1",
    "tr64desc.xml",
    actions={
        "Get": (
            ("NewI4", "out", "i4"),
            ("NewUi4", "out", "ui4"),
            ("NewUi4Neg", "out", "ui4"),
            ("NewUi8", "out", "ui8"),
            ("NewBoolOne", "out", "boolean"),
            ("NewBoolZero", "out", "boolean"),
            ("NewBoolBad", "out", "boolean"),
            ("NewDt", "out", "dateTime"),
            ("NewEmptyNum", "out", "ui4"),
            ("NewEmptyStr", "out", "string"),
            ("NewUuid", "out", "uuid"),
            ("NewFloat", "out", "float"),
            ("NewStr", "out", "string"),
            ("NewBadInt", "out", "i4"),
            ("NewUnderscore", "out", "i4"),
            ("NewHuge", "out", "ui4"),
            ("NewMissing", "out", "ui4"),
        ),
        "Set": (("NewText", "in", "string"), ("NewFlag", "in", "boolean"), ("NewNum", "in", "ui4"), ("NewResult", "out", "string")),
    },
)


def conv_box() -> FakeFritzBox:
    return FakeFritzBox([CONV])


def posts(box: FakeFritzBox):
    return [r for r in box.requests if r.method == "POST"]


def test_envelope_namespace_and_soapaction_use_the_discovered_service_type():
    box = FakeFritzBox()
    client = box.tr064_client()
    client.call_action("DeviceInfo", "GetInfo")
    request = posts(box)[-1]
    body = request.content.decode()
    assert request.url.path == DEVICE_CONTROL
    assert request.headers["SOAPAction"] == "urn:dslforum-org:service:DeviceInfo:1#GetInfo"
    assert request.headers["Content-Type"] == 'text/xml; charset="utf-8"'
    assert '<u:GetInfo xmlns:u="urn:dslforum-org:service:DeviceInfo:1">' in body
    assert 'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"' in body
    assert 's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"' in body
    assert fromstring(request.content) is not None


def test_same_type_name_in_two_namespaces_is_called_with_its_own_service_type():
    box = FakeFritzBox()
    client = box.tr064_client()
    client.call_action("WANIPConn", "GetStatusInfo")
    assert posts(box)[-1].headers["SOAPAction"] == "urn:schemas-upnp-org:service:WANIPConnection:1#GetStatusInfo"
    client.call_action("WANIPConnection:1", "GetInfo")
    assert posts(box)[-1].headers["SOAPAction"] == "urn:dslforum-org:service:WANIPConnection:1#GetInfo"


def test_arguments_are_escaped_typed_and_written_in_scpd_order():
    box = conv_box()
    client = box.tr064_client()
    client.call_action("Conv", "Set", {"NewNum": 7, "NewFlag": True, "NewText": "a&b<c>\"d'"})
    body = posts(box)[-1].content.decode()
    assert "<NewText>a&amp;b&lt;c&gt;&quot;d&apos;</NewText><NewFlag>1</NewFlag><NewNum>7</NewNum>" in body
    root = fromstring(posts(box)[-1].content)
    text = next(node.text for node in root.iter() if node.tag == "NewText")
    assert text == "a&b<c>\"d'"
    client.call_action("Conv", "Set", {"NewText": "", "NewFlag": False, "NewNum": 0})
    assert "<NewFlag>0</NewFlag><NewNum>0</NewNum>" in posts(box)[-1].content.decode()


@pytest.mark.parametrize(
    "arguments",
    [
        {"NewUnknown": "x"},
        {"NewResult": "x"},  # an out-argument is not an input
        {"NewText</u:Set><x/>": "x"},
        {"NewText": None},
        {"NewNum": 1.5},
        {"NewText": b"bytes"},
        {"NewText": "bad\x00char"},
        {"NewText": "lone\ud800surrogate"},
        {},  # SCPD knows no optional input arguments: every one is required
        {"NewText": "x", "NewFlag": True},  # NewNum missing
        {"NewFlag": False, "NewNum": 1},  # NewText missing
        {"NewText": "x", "NewFlag": True, "NewNum": 1, "NewExtra": 2},
    ],
)
def test_invalid_arguments_are_rejected_before_any_request(arguments):
    box = conv_box()
    client = box.tr064_client()
    assert client.has_action("Conv", "Set")  # loads the SCPD so only the POST is left to observe
    sent = len(box.requests)
    with pytest.raises(TR064ProtocolError):
        client.call_action("Conv", "Set", arguments)
    assert len(box.requests) == sent
    assert not box.calls


def test_missing_argument_is_named_in_the_error_and_nothing_is_sent():
    box = conv_box()
    client = box.tr064_client()
    assert client.has_action("Conv", "Set")
    with pytest.raises(TR064ProtocolError, match="missing") as info:
        client.call_action("Conv", "Set", {"NewText": "x"})
    assert "NewFlag" in str(info.value) and "NewNum" in str(info.value)
    assert not box.calls


def test_action_without_input_arguments_needs_none():
    box = conv_box()
    box.tr064_client().call_action("Conv", "Get")
    assert posts(box)[-1].content.decode().endswith("<u:Get xmlns:u=\"urn:dslforum-org:service:Conv:1\"></u:Get></s:Body></s:Envelope>")


def test_unknown_action_and_service_are_client_side_errors_without_a_post():
    box = FakeFritzBox()
    client = box.tr064_client()
    with pytest.raises(TR064ActionNotFound):
        client.call_action("DeviceInfo", "NoSuchAction")
    with pytest.raises(TR064ServiceNotFound):
        client.call_action("WANPPPConnection:2", "GetInfo")
    assert not box.calls


def test_conversion_follows_the_scpd_type_and_omits_what_is_not_plausible():
    box = conv_box()
    box.respond(
        "Conv1",
        "Get",
        {
            "NewI4": "-5",
            "NewUi4": "7",
            "NewUi4Neg": "-1",
            "NewUi8": "123456789012",
            "NewBoolOne": "1",
            "NewBoolZero": "0",
            "NewBoolBad": "maybe",
            "NewDt": "2026-11-16T10:00:00+01:00",
            "NewEmptyNum": "",
            "NewEmptyStr": "",
            "NewUuid": "uuid:abc-123",
            "NewFloat": "1.5",
            "NewStr": " keep ",
            "NewBadInt": "12abc",
            "NewUnderscore": "1_0",
            "NewHuge": "9" * 30,
            "NewNotInScpd": "ignored",
        },
    )
    result = box.tr064_client().call_action("Conv", "Get")
    assert result == {
        "NewI4": -5,
        "NewUi4": 7,
        "NewUi8": "123456789012",
        "NewBoolOne": True,
        "NewBoolZero": False,
        "NewDt": "2026-11-16T10:00:00+01:00",
        "NewEmptyStr": "",
        "NewUuid": "abc-123",
        "NewFloat": "1.5",
        "NewStr": " keep ",
    }
    assert type(result["NewBoolOne"]) is bool
    assert type(result["NewI4"]) is int
    assert "NewMissing" not in result


INT_RANGES = {
    "i1": (-(2**7), 2**7 - 1),
    "i2": (-(2**15), 2**15 - 1),
    "i4": (-(2**31), 2**31 - 1),
    "ui1": (0, 2**8 - 1),
    "ui2": (0, 2**16 - 1),
    "ui4": (0, 2**32 - 1),
}


@pytest.mark.parametrize("kind", sorted(INT_RANGES))
def test_integer_types_accept_exactly_their_range(kind):
    low, high = INT_RANGES[kind]
    for value in (low, high, 0 if low <= 0 else low + 1):
        assert convert_value(kind, str(value)) == value
    for value in (low - 1, high + 1):
        assert convert_value(kind, str(value)) is _OMIT
    assert convert_value(kind.upper(), str(high)) == high
    assert convert_value(kind, f" {high} ") == high


def test_int_has_no_declared_range_and_keeps_its_old_behaviour():
    assert convert_value("int", str(2**40)) == 2**40
    assert convert_value("int", "-5") == -5
    assert convert_value("int", "x") is _OMIT


@pytest.mark.parametrize("kind", ["i1", "ui1", "ui4", "int"])
@pytest.mark.parametrize("text", ["", " ", "1.0", "0x10", "1e3", "--1", "١٢"])
def test_integer_types_omit_empty_and_non_integer_text(kind, text):
    assert convert_value(kind, text) is _OMIT


def test_integer_leading_sign_and_zeros_follow_the_range():
    assert convert_value("ui1", "+255") == 255
    assert convert_value("ui1", "0000255") == 255
    assert convert_value("ui1", "-0") == 0
    assert convert_value("ui1", "0256") is _OMIT


def test_boolean_conversion_is_strict():
    assert convert_value("boolean", " 1 ") is True
    assert convert_value("boolean", "0") is False
    for text in ("", "2", "true", "-1"):
        assert convert_value("boolean", text) is _OMIT


def test_out_of_range_response_value_is_omitted_from_the_result():
    box = FakeFritzBox(
        [FakeService("R1", "R:1", "tr64desc.xml", actions={"Get": (("NewByte", "out", "ui1"), ("NewOk", "out", "ui1"))})]
    )
    box.respond("R1", "Get", {"NewByte": "256", "NewOk": "255"})
    assert box.tr064_client().call_action("R", "Get") == {"NewOk": 255}


def test_pretty_printed_and_compact_responses_give_the_same_result():
    box = FakeFritzBox()
    box.respond("DeviceInfo1", "GetInfo", {"NewModelName": "FRITZ!Box 7590", "NewUpTime": "90000"})
    compact = box.tr064_client().call_action("DeviceInfo", "GetInfo")
    pretty = (
        '<?xml version="1.0"?>\n<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">\n  <s:Body>\n'
        '    <u:GetInfoResponse xmlns:u="urn:dslforum-org:service:DeviceInfo:1">\n'
        "      <NewModelName>FRITZ!Box 7590</NewModelName>\n      <NewUpTime>90000</NewUpTime>\n"
        "    </u:GetInfoResponse>\n  </s:Body>\n</s:Envelope>\n"
    )
    box.respond_xml("DeviceInfo1", "GetInfo", pretty)
    assert box.tr064_client().call_action("DeviceInfo", "GetInfo") == compact == {"NewModelName": "FRITZ!Box 7590", "NewUpTime": 90000}


@pytest.mark.parametrize("pretty", [False, True], ids=["compact", "pretty"])
def test_soap_fault_401_is_action_not_found_not_unauthorized(pretty):
    box = FakeFritzBox()
    box.fault("DeviceInfo1", "GetInfo", 401, "Invalid Action", pretty=pretty)
    with pytest.raises(TR064ActionNotFound) as info:
        box.tr064_client().call_action("DeviceInfo", "GetInfo")
    assert not isinstance(info.value, TR064HttpUnauthorized)
    assert not isinstance(info.value, TR064SoapFault)


@pytest.mark.parametrize("pretty", [False, True], ids=["compact", "pretty"])
@pytest.mark.parametrize("code", [402, 501, 713, 714, 820, 12345])
def test_other_soap_faults_carry_their_code(code, pretty):
    box = FakeFritzBox()
    box.fault("DeviceInfo1", "GetInfo", code, "boom", pretty=pretty)
    with pytest.raises(TR064SoapFault) as info:
        box.tr064_client().call_action("DeviceInfo", "GetInfo")
    assert info.value.code == code
    assert not isinstance(info.value, (TR064NotAuthorized, TR064ActionNotFound))


def test_soap_606_is_a_distinct_type_but_transient_in_the_handler():
    box = FakeFritzBox()
    box.fault("DeviceInfo1", "GetInfo", 606, "Action not authorized")
    with pytest.raises(TR064NotAuthorized) as info:
        box.tr064_client().call_action("DeviceInfo", "GetInfo")
    # Not an HTTP error and not "action missing": the handler keeps it on the transient branch (D2).
    assert isinstance(info.value, TR064SoapFault)
    assert info.value.code == 606
    assert not isinstance(info.value, (TR064HttpError, TR064ActionNotFound))


def test_fault_without_error_code_is_a_generic_fault():
    box = FakeFritzBox()
    xml = (
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>'
        "<faultcode>s:Server</faultcode><faultstring>broken</faultstring></s:Fault></s:Body></s:Envelope>"
    )
    box.respond_xml("DeviceInfo1", "GetInfo", xml)
    with pytest.raises(TR064SoapFault) as info:
        box.tr064_client().call_action("DeviceInfo", "GetInfo")
    assert info.value.code is None
    assert "broken" in info.value.description


def test_fault_inside_http_200_is_still_a_fault():
    box = FakeFritzBox()
    box.override(DEVICE_CONTROL, soap_fault(501, "x"), status=200)
    with pytest.raises(TR064SoapFault) as info:
        box.tr064_client().call_action("DeviceInfo", "GetInfo")
    assert info.value.code == 501


@pytest.mark.parametrize(
    ("status", "body", "content_type"),
    [
        (500, "<html>internal error</html>", "text/html"),
        (500, "not xml at all", "text/plain"),
        (502, "", "text/plain"),
        (500, "<a><b/></a>", "text/xml"),
    ],
)
def test_http_error_without_a_fault_is_an_http_error(status, body, content_type):
    box = FakeFritzBox()
    box.override(DEVICE_CONTROL, body, status=status, content_type=content_type)
    with pytest.raises(TR064HttpError) as info:
        box.tr064_client().call_action("DeviceInfo", "GetInfo")
    assert info.value.status == status
    assert not isinstance(info.value, TR064SoapFault)


@pytest.mark.parametrize(
    "body",
    [
        "ok",
        "",
        "<html><body>login</body></html>",
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body/></s:Envelope>',
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><u:OtherResponse xmlns:u="x"/></s:Body></s:Envelope>',
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><s:Envelope xmlns:s="x"><s:Body/></s:Envelope>',
    ],
)
def test_unexpected_response_fails_the_probe(body):
    box = FakeFritzBox()
    box.override(DEVICE_CONTROL, body, status=200)
    with pytest.raises(TR064ProtocolError):
        box.tr064_client().call_action("DeviceInfo", "GetInfo")
