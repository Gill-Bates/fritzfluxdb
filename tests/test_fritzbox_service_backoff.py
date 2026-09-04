#
# tests/test_fritzbox_service_backoff.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from fritzfluxdb.classes.fritzbox.service_handler import FritzBoxService


def make_service(interval=10):
    return FritzBoxService({
        "name": "TestService",
        "interval": interval,
        "value_instances": {},
    })


def test_failed_query_backs_off_until_next_retry():
    service = make_service()

    assert service.should_be_requested() is True

    assert service.set_failed_query_now() == 10.0
    assert service.failure_count == 1
    assert service.should_be_requested() is False

    service._next_request_monotonic = 0.0
    assert service.should_be_requested() is True

    assert service.set_failed_query_now() == 20.0
    assert service.failure_count == 2
    assert service.should_be_requested() is False


def test_successful_query_resets_backoff_and_uses_service_interval():
    service = make_service(interval=30)

    assert service.set_failed_query_now() == 30.0
    assert service.failure_count == 1

    service.set_last_query_now()

    assert service.failure_count == 0
    assert service.should_be_requested() is False

    service._next_request_monotonic = 0.0
    assert service.should_be_requested() is True
