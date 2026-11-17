#!/usr/bin/env python3
#
# tests/test_tr064_helper_contracts.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Concurrency and negative authentication cases provided by the simulated box."""

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.fritzbox.tr064.discovery import Discovery, Scpd
from app.fritzbox.tr064.exceptions import TR064TimeoutError
from tests.fakebox import DigestVerifier, FakeFritzBox


def test_transport_rule_is_consumed_once_across_parallel_requests():
    box = FakeFritzBox()
    client = box.tr064_client()
    box.raise_transport(httpx.ReadTimeout, path="/upnp/control/deviceinfo1", times=1)

    def request(_):
        try:
            client.call_action("DeviceInfo", "GetInfo")
        except TR064TimeoutError:
            return "timeout"
        return "ok"

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(request, range(64)))
        assert outcomes.count("timeout") == 1
        assert outcomes.count("ok") == 63
    finally:
        client.close()


def test_discovery_and_action_maps_are_immutable_snapshots():
    original = {}
    discovery = Discovery(original)
    scpd = Scpd(original)
    original["late"] = object()
    assert not discovery.services and not scpd.actions
    with pytest.raises(TypeError):
        discovery.services["new"] = object()
    with pytest.raises(TypeError):
        scpd.actions["new"] = object()


@pytest.mark.parametrize(("qop", "nc", "cnonce", "realm", "uri", "server_qop"), [
    ("auth", "1", "client", "HTTPS Access", "/control", "auth"),
    ("auth", "gggggggg", "client", "HTTPS Access", "/control", "auth"),
    ("auth", "00000001", "", "HTTPS Access", "/control", "auth"),
    ("auth", "00000001", "client", "wrong", "/control", "auth"),
    ("auth", "00000001", "client", "HTTPS Access", "/other", "auth"),
    ("auth", "00000001", "client", "HTTPS Access", "/control", None),
])
def test_digest_fixture_rejects_malformed_fields_even_with_a_matching_hash(qop, nc, cnonce, realm, uri, server_qop):
    verifier = DigestVerifier("user", "password", qop=server_qop)
    challenge = verifier.challenge()
    nonce = re.search(r'nonce="([^"]+)"', challenge.headers["www-authenticate"])[1]

    def digest(value):
        return hashlib.md5(value.encode()).hexdigest()

    ha1 = digest("user:HTTPS Access:password")
    ha2 = digest("POST:/control")
    response = digest(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
    request = httpx.Request("POST", "http://box/control", headers={"Authorization": (
        f'Digest username="user", realm="{realm}", nonce="{nonce}", uri="{uri}", '
        f'qop={qop}, nc={nc}, cnonce="{cnonce}", response="{response}"'
    )})
    assert verifier.check(request).status_code == 401
    assert verifier.accepted == 0
