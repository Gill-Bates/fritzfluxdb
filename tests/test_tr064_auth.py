#!/usr/bin/env python3
#
# tests/test_tr064_auth.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Digest authentication through httpx against a real digest verifier."""

import logging
import threading

import httpx
import pytest

from app.fritzbox.tr064.exceptions import TR064HttpUnauthorized, TR064ProtocolError
from tests.fakebox import DigestVerifier, FakeFritzBox

USER, PASSWORD = "fritz-user", "pw-correct-horse"


def digest_box(**kwargs) -> tuple[FakeFritzBox, DigestVerifier]:
    verifier = DigestVerifier(USER, PASSWORD, **kwargs)
    return FakeFritzBox(digest=verifier), verifier


def client_for(box: FakeFritzBox, **kwargs):
    return box.tr064_client(username=USER, password=kwargs.pop("password", PASSWORD), **kwargs)


def auth_headers(box: FakeFritzBox) -> list[str | None]:
    return [request.headers.get("authorization") for request in box.requests]


def test_digest_challenge_is_not_a_failure_and_not_reported(caplog):
    caplog.set_level(logging.DEBUG)
    box, verifier = digest_box()
    client = client_for(box)  # discovery already passed the challenge silently
    assert client.call_action("DeviceInfo", "GetInfo") == {}
    assert verifier.anonymous == 1
    assert verifier.bad_response == 0
    # The recorded request objects are mutated by the auth retry, so only the POST is a safe witness.
    assert box.requests[-1].method == "POST"
    assert (auth_headers(box)[-1] or "").startswith("Digest ")
    assert verifier.accepted >= 1
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert not [record for record in caplog.records if record.name.startswith("fritzFlux") and "401" in record.getMessage()]


def test_the_last_challenge_is_reused_without_a_new_round_trip():
    box, verifier = digest_box()
    client = client_for(box)
    before = len(box.requests)
    client.call_action("DeviceInfo", "GetInfo")
    client.call_action("Time:1", "GetInfo")
    assert len(box.requests) - before == 4  # two SCPD loads plus two actions, no 401 in between
    assert verifier.anonymous == 1
    assert verifier.stale == 0


def test_expired_nonce_is_answered_transparently():
    box, verifier = digest_box()
    client = client_for(box)
    verifier.expire_nonces()
    assert client.call_action("DeviceInfo", "GetInfo") == {}
    assert verifier.stale == 1
    assert verifier.bad_response == 0


def test_wrong_password_ends_in_a_single_final_401():
    box, verifier = digest_box()
    with pytest.raises(TR064HttpUnauthorized) as info:
        client_for(box, password="wrong", discover=False).discover()
    assert info.value.status == 401
    # igddesc.xml (optional, skipped on 401) and tr64desc.xml each: challenge + one answer, no retry
    assert len(box.requests) == 4
    assert verifier.bad_response == 3  # the second document replays the stored challenge
    assert verifier.accepted == 0


def test_client_never_retries_a_rejected_request():
    box, verifier = digest_box()
    client = client_for(box, password="wrong", discover=False)
    for _ in range(3):
        sent = len(box.requests)
        with pytest.raises(TR064HttpUnauthorized):
            client.discover()
        assert len(box.requests) - sent == 4  # two documents, each: challenge + answer or pre-emptive answer + one replay
    assert verifier.anonymous + verifier.bad_response == len(box.requests) == 12


@pytest.mark.parametrize(
    "options",
    [
        {"qop": None},
        {"algorithm": "SHA-256"},
        {"qop": "auth,auth-int"},
        {"realm": "F!Box SOAP-Auth"},
    ],
    ids=["no-qop", "sha-256", "auth-and-auth-int", "other-realm"],
)
def test_supported_challenge_variants_authenticate(options):
    box, verifier = digest_box(**options)
    assert client_for(box).call_action("DeviceInfo", "GetInfo") == {}
    assert verifier.bad_response == 0
    assert verifier.accepted >= 3


@pytest.mark.parametrize("options", [{"qop": "auth-int"}, {"algorithm": "FOO"}], ids=["auth-int-only", "unknown-algorithm"])
def test_unsupported_challenge_is_a_protocol_error(options):
    box, _ = digest_box(**options)
    with pytest.raises(TR064ProtocolError, match="digest"):
        client_for(box)


def test_malformed_challenge_header_is_a_protocol_error():
    box = FakeFritzBox()
    box.responders["/igddesc.xml"] = lambda request: httpx.Response(401, headers={"WWW-Authenticate": "Digest realm"})
    with pytest.raises(TR064ProtocolError, match="digest"):
        box.tr064_client()


def test_digest_is_used_over_plain_http_and_the_password_never_goes_out_in_clear():
    box, _ = digest_box()
    client_for(box, use_tls=False)
    sent = [header for header in auth_headers(box) if header]
    assert sent
    assert all(PASSWORD not in header for header in sent)
    assert all(PASSWORD not in str(request.url) and PASSWORD.encode() not in request.content for request in box.requests)


def test_digest_state_is_per_thread():
    box, verifier = digest_box()
    client = client_for(box)
    client.has_action("DeviceInfo", "GetInfo")
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def work() -> None:
        try:
            barrier.wait(10)
            for _ in range(5):
                client.call_action("DeviceInfo", "GetInfo")
        except BaseException as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert verifier.duplicate_nc == []
    assert verifier.bad_response == 0
    # one challenge round per thread (the discovering thread already had its own)
    assert verifier.anonymous <= 9
    assert verifier.stale == 0
