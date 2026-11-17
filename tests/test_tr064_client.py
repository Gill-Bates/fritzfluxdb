#!/usr/bin/env python3
#
# tests/test_tr064_client.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""HTTP-level behaviour of the native client: statuses, transport, limits, targets."""

import gzip
import logging

import httpx
import pytest

from app.fritzbox.tr064 import client as client_module
from app.fritzbox.tr064.client import TR064_VERIFY_TLS, TR064Client, request_url
from app.fritzbox.tr064.exceptions import (
    TR064AccessDisabled,
    TR064ActionNotFound,
    TR064ConnectError,
    TR064ConnectTimeout,
    TR064Error,
    TR064HttpError,
    TR064HttpForbidden,
    TR064HttpUnauthorized,
    TR064MalformedXml,
    TR064ProtocolError,
    TR064ResourceMissing,
    TR064ServiceNotFound,
    TR064SoapFault,
    TR064TimeoutError,
    TR064TransportError,
)
from app.fritzbox.tr064.xmlsafe import MAX_DESCRIPTION_BYTES, MAX_SOAP_BYTES
from tests.fakebox import FakeFritzBox, soap_fault

DEVICE_CONTROL = "/upnp/control/deviceinfo1"
TIMEOUT = httpx.Timeout(40.0, connect=10.0)


def call(box: FakeFritzBox):
    return box.tr064_client().call_action("DeviceInfo", "GetInfo")


def chunks(total: int, size: int = 65536):
    sent = 0
    while sent < total:
        piece = min(size, total - sent)
        sent += piece
        yield b" " * piece


# TLS verification is explicit so the documented trust policy cannot change by default.


@pytest.fixture
def client_options(monkeypatch):
    seen: list[dict] = []
    real = httpx.Client

    class Recording(real):
        def __init__(self, **kwargs):
            seen.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(client_module.httpx, "Client", Recording)
    return seen


def test_verify_is_always_passed_explicitly(client_options):
    seen = client_options
    for use_tls, port in ((False, 49000), (True, 49443)):
        TR064Client("fritz.box", port, username="u", password="p", use_tls=use_tls, verify=TR064_VERIFY_TLS, timeout=TIMEOUT).close()
    assert len(seen) == 2
    assert all("verify" in kwargs for kwargs in seen)


def test_tr064_tls_is_unverified_by_design(client_options):
    seen = client_options
    FakeFritzBox().tr064_client(use_tls=True, port=49443, discover=False).close()
    assert TR064_VERIFY_TLS is False
    assert seen[0]["verify"] is False
    assert seen[0]["follow_redirects"] is False
    assert seen[0]["trust_env"] is False


def test_timeouts_are_set_from_config(client_options):
    seen = client_options
    TR064Client("fritz.box", 49000, username="u", password="p", use_tls=False, verify=False, timeout=httpx.Timeout(12.0, connect=3.0)).close()
    timeout = seen[0]["timeout"]
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (3.0, 12.0, 12.0, 12.0)
    limits = seen[0]["limits"]
    assert (limits.max_connections, limits.max_keepalive_connections) == (8, 4)


# URL construction, target validation, redirects


@pytest.mark.parametrize(
    ("kwargs", "prefix"),
    [
        ({"host": "192.168.178.1"}, "http://192.168.178.1:49000/"),
        ({"host": "fritz.box"}, "http://fritz.box:49000/"),
        ({"host": "fd00::1"}, "http://[fd00::1]:49000/"),
        ({"host": "[fd00::1]"}, "http://[fd00::1]:49000/"),
        ({"host": "fritz.box", "use_tls": True, "port": 49443}, "https://fritz.box:49443/"),
        ({"host": "::1", "use_tls": True, "port": 49443}, "https://[::1]:49443/"),
    ],
)
def test_request_urls_for_ipv4_hostname_ipv6_and_ports(kwargs, prefix):
    box = FakeFritzBox()
    client = box.tr064_client(discover=False, **kwargs)
    client.discover()
    assert [str(r.url) for r in box.requests] == [prefix + "igddesc.xml", prefix + "tr64desc.xml"]
    assert client.use_tls is kwargs.get("use_tls", False)


@pytest.mark.parametrize("host", ["", "fritz.box/evil", "a@b", "fritz.box#x", "a b", "[fd00::1", "fritz.box?x"])
def test_hostile_host_is_refused_at_construction(host):
    with pytest.raises(ValueError):
        FakeFritzBox().tr064_client(host=host, discover=False)


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_invalid_port_is_refused(port):
    with pytest.raises(ValueError):
        FakeFritzBox().tr064_client(port=port, discover=False)


@pytest.mark.parametrize("path", ["@evil.example/x", "0@evil.example/x", ":1/x", ".evil.example/x"])
def test_foreign_host_url_is_rejected(path):
    with pytest.raises(TR064ProtocolError):
        request_url("http://fritz.box:49000", path)


@pytest.mark.parametrize("path", ["/upnp/control?mode=test", "/upnp/../other", "/upnp/%2e%2e/other", "/x#frag", "/a\\b"])
def test_request_url_refuses_unsafe_paths_on_the_same_origin(path):
    with pytest.raises(TR064ProtocolError):
        request_url("http://fritz.box:49000", path)


def test_same_origin_path_is_accepted_and_userinfo_is_refused():
    assert str(request_url("http://fritz.box:49000", "/upnp/control/x")) == "http://fritz.box:49000/upnp/control/x"
    assert str(request_url("https://fritz.box:49443", "/x")) == "https://fritz.box:49443/x"
    with pytest.raises(TR064ProtocolError):
        request_url("http://fritz.box:49000", "@fritz.box:49000/x")


def test_redirect_is_not_followed():
    box = FakeFritzBox()
    box.responders["/tr64desc.xml"] = lambda request: httpx.Response(302, headers={"Location": "http://evil.example/tr64desc.xml"})
    with pytest.raises(TR064HttpError) as info:
        box.tr064_client()
    assert info.value.status == 302
    assert {request.url.host for request in box.requests} == {"fritz.box"}


def test_foreign_control_url_in_a_description_never_reaches_the_network():
    box = FakeFritzBox()
    document = box.fetch("/tr64desc.xml").decode().replace("/upnp/control/deviceinfo1", "@evil.example/x", 1)
    box.override("/tr64desc.xml", document)
    with pytest.raises(TR064ProtocolError):
        box.tr064_client()
    assert {request.url.host for request in box.requests} == {"fritz.box"}


# HTTP status taxonomy (R8 rows 1, 3, 4, 5, 8, 10)


def test_http_401_after_the_exchange_is_unauthorized():
    box = FakeFritzBox()
    box.deny("DeviceInfo1", "GetInfo", 401)
    with pytest.raises(TR064HttpUnauthorized) as info:
        call(box)
    assert info.value.status == 401


def test_http_403_is_forbidden_not_unauthorized():
    box = FakeFritzBox()
    box.deny("DeviceInfo1", "GetInfo", 403)
    with pytest.raises(TR064HttpForbidden) as info:
        call(box)
    assert info.value.status == 403
    assert not isinstance(info.value, TR064HttpUnauthorized)
    assert len(box.calls) == 1  # no retry


def test_http_401_with_xml_body_is_unauthorized():
    box = FakeFritzBox()
    box.responders[DEVICE_CONTROL] = lambda request: httpx.Response(401, content=soap_fault(401, "x").encode(), headers={"Content-Type": "text/xml"})
    with pytest.raises(TR064HttpUnauthorized) as info:
        call(box)
    assert not isinstance(info.value, TR064ActionNotFound)


def test_http_401_status_and_soap_fault_401_are_different_classes():
    http_box, soap_box = FakeFritzBox(), FakeFritzBox()
    http_box.deny("DeviceInfo1", "GetInfo", 401)
    soap_box.fault("DeviceInfo1", "GetInfo", 401)
    with pytest.raises(TR064HttpUnauthorized):
        call(http_box)
    with pytest.raises(TR064ActionNotFound):
        call(soap_box)


def test_control_url_404_is_a_missing_resource():
    box = FakeFritzBox()
    box.override(DEVICE_CONTROL, "<html/>", status=404, content_type="text/html")
    with pytest.raises(TR064ResourceMissing):
        call(box)


def test_unknown_service_raises_before_any_request():
    box = FakeFritzBox()
    client = box.tr064_client()
    sent = len(box.requests)
    with pytest.raises(TR064ServiceNotFound):
        client.call_action("WANPPPConnection:2", "GetInfo")
    assert len(box.requests) == sent


def test_other_soap_faults_ignore_401_or_820_in_the_text():
    box = FakeFritzBox()
    box.fault("DeviceInfo1", "GetInfo", 501, "error 401 and 820 and 606")
    with pytest.raises(TR064SoapFault) as info:
        call(box)
    assert type(info.value) is TR064SoapFault
    assert info.value.code == 501


def test_fault_text_is_sanitised():
    box = FakeFritzBox()
    box.fault("DeviceInfo1", "GetInfo", 501, "first\r\nINJECTED line\x7f\u2028[31m" + "x" * 5000)
    with pytest.raises(TR064SoapFault) as info:
        call(box)
    text = str(info.value)
    assert "\n" not in text
    assert "\r" not in text
    assert "\x7f" not in text
    assert "\u2028" not in text
    assert len(text) < 300


@pytest.mark.parametrize(
    ("status", "content_type", "body"),
    [(404, "text/html", "<html/>"), (200, "text/html", "<html>login</html>"), (200, "text/html; charset=utf-8", "<html/>"), (404, "text/xml", "")],
)
def test_missing_tr64desc_is_access_disabled_from_client(status, content_type, body):
    box = FakeFritzBox()
    box.override("/tr64desc.xml", body, status=status, content_type=content_type)
    with pytest.raises(TR064AccessDisabled):
        box.tr064_client()


@pytest.mark.parametrize("body", ["hello", "", "<root><device></root>", "<other/>"])
def test_damaged_tr64desc_is_malformed_not_access_disabled_from_client(body):
    box = FakeFritzBox()
    box.override("/tr64desc.xml", body, status=200, content_type="text/xml")
    with pytest.raises(TR064MalformedXml) as info:
        box.tr064_client()
    assert not isinstance(info.value, TR064AccessDisabled)


def test_http_500_in_discovery_is_http_error_not_connect_error():
    box = FakeFritzBox()
    box.override("/tr64desc.xml", "<html>oops</html>", status=500, content_type="text/html")
    with pytest.raises(TR064HttpError) as info:
        box.tr064_client()
    assert info.value.status == 500
    assert not isinstance(info.value, TR064TransportError)


def test_missing_or_html_igddesc_is_skipped_and_charset_in_content_type_is_fine():
    box = FakeFritzBox()
    box.override("/igddesc.xml", "<html/>", content_type="text/html; charset=utf-8")
    box.override("/tr64desc.xml", box.fetch("/tr64desc.xml").decode(), content_type="text/xml; charset=utf-8")
    client = box.tr064_client()
    assert client.has_action("DeviceInfo", "GetInfo")
    assert not client.has_action("WANIPConn", "GetStatusInfo")  # igd services are absent


@pytest.mark.parametrize("status", [401, 403, 500, 503, 302])
def test_igddesc_with_any_non_200_status_is_skipped(status, caplog):
    box = FakeFritzBox()
    box.responders["/igddesc.xml"] = lambda request: httpx.Response(status, headers={"Location": "http://evil.example/x"} if status == 302 else {})
    with caplog.at_level(logging.DEBUG, logger=client_module.log.name):
        client = box.tr064_client()
    assert client.has_action("DeviceInfo", "GetInfo")
    assert not client.has_action("WANIPConn", "GetStatusInfo")
    assert any("igddesc.xml" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("status", [401, 403, 500, 503, 302])
def test_tr64desc_with_the_same_statuses_still_fails(status):
    box = FakeFritzBox()
    box.responders["/tr64desc.xml"] = lambda request: httpx.Response(status, headers={"Location": "http://evil.example/x"} if status == 302 else {})
    with pytest.raises(TR064HttpError) as info:
        box.tr064_client()
    assert info.value.status == status
    assert not isinstance(info.value, TR064AccessDisabled)


def test_igddesc_transport_and_limit_errors_still_propagate():
    box = FakeFritzBox()
    box.responders["/igddesc.xml"] = lambda request: httpx.Response(200, content=b" " * (MAX_DESCRIPTION_BYTES + 1), headers={"Content-Type": "text/xml"})
    with pytest.raises(TR064ProtocolError, match="larger"):
        box.tr064_client()


def test_calling_before_discover_is_an_error():
    client = FakeFritzBox().tr064_client(discover=False)
    with pytest.raises(TR064Error):
        client.call_action("DeviceInfo", "GetInfo")


def test_discover_twice_does_not_refetch():
    box = FakeFritzBox()
    client = box.tr064_client()
    sent = len(box.requests)
    client.discover()
    assert len(box.requests) == sent


# has_action


def test_has_action_reports_absence_without_raising_and_loads_scpd_once():
    box = FakeFritzBox()
    client = box.tr064_client()
    assert client.has_action("WANPPPConnection:1", "GetInfo") is True
    assert client.has_action("WANPPPConnection:1", "X_Missing") is False
    assert client.has_action("WANPPPConnection:2", "GetInfo") is False
    assert [r.url.path for r in box.requests].count("/wanpppconnection1SCPD.xml") == 1


@pytest.mark.parametrize(("status", "content_type"), [(404, "text/html"), (200, "text/html"), (500, "text/plain")])
def test_has_action_raises_when_the_scpd_cannot_be_loaded(status, content_type):
    box = FakeFritzBox()
    box.override("/wanpppconnection1SCPD.xml", "<html/>", status=status, content_type=content_type)
    client = box.tr064_client()
    with pytest.raises(TR064HttpError):
        client.has_action("WANPPPConnection:1", "GetInfo")
    assert client.has_action("DeviceInfo", "GetInfo")  # another service is not poisoned


# Transport mapping (R8 rows 9, 11, 12)


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (httpx.ConnectError, TR064ConnectError),
        (httpx.ConnectTimeout, TR064ConnectTimeout),
        (httpx.ReadTimeout, TR064TimeoutError),
        (httpx.WriteTimeout, TR064TimeoutError),
        (httpx.PoolTimeout, TR064TimeoutError),
        (httpx.ReadError, TR064TransportError),
        (httpx.RemoteProtocolError, TR064TransportError),
    ],
)
def test_transport_failures_are_typed_and_keep_their_cause(raised, expected):
    box = FakeFritzBox()
    box.raise_transport(raised)
    with pytest.raises(TR064TransportError) as info:
        box.tr064_client()
    assert type(info.value) is expected
    assert isinstance(info.value.__cause__, raised)


def test_connect_errors_are_typed_for_the_https_probe():
    box = FakeFritzBox()
    box.raise_transport(httpx.ConnectError)
    with pytest.raises(TR064ConnectError):
        box.tr064_client(use_tls=True, port=49443)
    assert [str(r.url) for r in box.requests] == ["https://fritz.box:49443/igddesc.xml"]


def test_read_timeout_is_not_a_connect_error():
    box = FakeFritzBox()
    box.raise_transport(httpx.ReadTimeout)
    with pytest.raises(TR064TimeoutError) as info:
        box.tr064_client()
    assert not isinstance(info.value, (TR064ConnectError, TR064ConnectTimeout))


def test_transport_error_in_a_query_is_a_transport_error():
    box = FakeFritzBox()
    client = box.tr064_client()
    box.raise_transport(httpx.ConnectError, path=DEVICE_CONTROL, times=1)
    with pytest.raises(TR064TransportError):
        client.call_action("DeviceInfo", "GetInfo")
    assert client.call_action("DeviceInfo", "GetInfo") == {}  # the client is not poisoned


# Size limits are enforced while streaming (R5)


def test_oversized_declared_length_is_rejected():
    box = FakeFritzBox()
    box.responders["/tr64desc.xml"] = lambda request: httpx.Response(200, content=b"<root>" + b" " * MAX_DESCRIPTION_BYTES)
    with pytest.raises(TR064ProtocolError):
        box.tr064_client()


def test_oversized_chunked_body_is_cut_off_while_streaming():
    box = FakeFritzBox()
    box.responders["/tr64desc.xml"] = lambda request: httpx.Response(200, content=chunks(MAX_DESCRIPTION_BYTES + 1))
    with pytest.raises(TR064ProtocolError):
        box.tr064_client()


def test_oversized_soap_answer_is_rejected():
    box = FakeFritzBox()
    box.responders[DEVICE_CONTROL] = lambda request: httpx.Response(200, content=chunks(MAX_SOAP_BYTES + 1))
    with pytest.raises(TR064ProtocolError):
        call(box)


def test_digest_challenge_body_is_closed_without_reading():
    class UnreadableBody(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            raise AssertionError("Digest challenge body must not be consumed")
            yield b""

        def close(self):
            self.closed = True

    body = UnreadableBody()
    requests = []

    def respond(request):
        requests.append(request)
        if "authorization" not in request.headers:
            return httpx.Response(401, headers={
                "WWW-Authenticate": 'Digest realm="box", nonce="test", qop="auth"',
                "Content-Length": str(MAX_SOAP_BYTES * 10),
            }, stream=body)
        return httpx.Response(403)

    client = TR064Client("box", 49000, username="u", password="p", use_tls=False,
                         verify=False, timeout=TIMEOUT, transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(TR064HttpForbidden):
            client.discover()
        assert body.closed
        assert len(requests) == 3  # The optional IGD and required TR-064 documents share the digest nonce.
    finally:
        client.close()


def test_gzip_bomb_is_limited_by_the_decoded_size():
    box = FakeFritzBox()
    bomb = gzip.compress(b" " * (MAX_DESCRIPTION_BYTES * 4))
    assert len(bomb) < MAX_DESCRIPTION_BYTES
    box.responders["/tr64desc.xml"] = lambda request: httpx.Response(200, content=bomb, headers={"Content-Encoding": "gzip", "Content-Type": "text/xml"})
    with pytest.raises(TR064ProtocolError):
        box.tr064_client()


def test_requests_ask_for_identity_encoding():
    box = FakeFritzBox()
    box.tr064_client()
    assert all(request.headers["Accept-Encoding"] == "identity" for request in box.requests)


def test_no_secret_is_logged_at_debug(caplog):
    caplog.set_level(logging.DEBUG)
    box = FakeFritzBox()
    box.deny("DeviceInfo1", "GetInfo", 403)
    client = box.tr064_client(password="pw-Zx9-never-log")
    with pytest.raises(TR064HttpForbidden) as info:
        client.call_action("DeviceInfo", "GetInfo")
    assert "pw-Zx9-never-log" not in caplog.text
    assert "pw-Zx9-never-log" not in str(info.value)
