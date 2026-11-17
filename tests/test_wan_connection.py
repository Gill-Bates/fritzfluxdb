#!/usr/bin/env python3
#
# tests/test_wan_connection.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""WAN outage messages follow the FRITZ!Box status, not backend write errors."""

import logging
from types import SimpleNamespace

from app.fritzbox.handler import FritzBoxHandler
from app.fritzbox.service_handler import FritzBoxTR064Service
from tests.fakebox import FakeFritzBox


def _observer():
    observer = FritzBoxHandler.__new__(FritzBoxHandler)
    observer._wan_connected = None
    observer._wan_uptime = None
    observer._wan_renewal_notice_sent = False
    observer.config = SimpleNamespace(link_type="DSL")
    return observer


def test_wan_down_and_recovery_are_reported_once(caplog):
    observer = _observer()
    with caplog.at_level(logging.INFO, logger="fritzFlux"):
        observer._observe_wan_connection({"NewConnectionStatus": "Connected", "NewUptime": "120"})
        observer._observe_wan_connection({"NewConnectionStatus": "Disconnected", "NewUptime": "0"})
        observer._observe_wan_connection({"NewConnectionStatus": "Disconnected", "NewUptime": "0"})
        observer._observe_wan_connection({"NewConnectionStatus": "Connected", "NewUptime": "5"})

    assert [record.getMessage() for record in caplog.records] == [
        "FRITZ!Box WAN connection is down",
        "FRITZ!Box WAN connection restored",
    ]


def test_wan_restart_between_polls_is_reported_afterward(caplog):
    observer = _observer()
    with caplog.at_level(logging.INFO, logger="fritzFlux"):
        observer._observe_wan_connection({"NewConnectionStatus": "Connected", "NewUptime": "3600"})
        observer._observe_wan_connection({"NewConnectionStatus": "Connected", "NewUptime": "5"})
        observer._observe_wan_connection({"NewConnectionStatus": "Connected", "NewUptime": "65"})

    assert [record.getMessage() for record in caplog.records] == [
        "FRITZ!Box WAN connection restarted between status polls",
    ]


def test_missing_or_invalid_wan_data_does_not_infer_an_outage(caplog):
    observer = _observer()
    with caplog.at_level(logging.INFO, logger="fritzFlux"):
        observer._observe_wan_connection({"NewConnectionStatus": "Connected", "NewUptime": "3600"})
        observer._observe_wan_connection({"NewConnectionStatus": "Unknown", "NewUptime": "invalid"})
        observer._observe_wan_connection({"NewConnectionStatus": [], "NewUptime": None})
        observer._observe_wan_connection({})
        observer._observe_wan_connection({"NewConnectionStatus": "Connected", "NewUptime": "3660"})

    assert caplog.records == []


def test_24_hour_renewal_estimate_is_reported_once(caplog):
    observer = _observer()
    with caplog.at_level(logging.INFO, logger="fritzFlux"):
        for uptime in (86090, 86150, 86210):
            observer._observe_wan_connection({"NewConnectionStatus": "Connected", "NewUptime": str(uptime)})

    notices = [record.getMessage() for record in caplog.records if "may renew after 24 hours" in record.getMessage()]
    assert len(notices) == 1
    assert "around 20" in notices[0]


def test_status_poll_reports_router_outage_and_keeps_measurements(caplog):
    observer = _observer()
    observer.config = SimpleNamespace(box_tag="box", link_type="DSL")
    observer.discovery_done = False
    observer.current_result_list = []
    box = FakeFritzBox()
    box.respond("WANPPPConnection1", "GetInfo", {"NewConnectionStatus": "Disconnected", "NewUptime": "0"})
    observer.session = box.tr064_client()
    service = FritzBoxTR064Service({
        "name": "WANPPPConnection:1", "actions": ["GetInfo"],
        "value_instances": {"NewConnectionStatus": "connection_status:str", "NewUptime": "linkuptime:int"},
    })

    with caplog.at_level(logging.INFO, logger="fritzFlux"):
        assert observer.query_service_data(service) is True

    assert "FRITZ!Box WAN connection is down" in caplog.text
    assert {(item.name, item.value) for item in observer.current_result_list} == {
        ("connection_status", "Disconnected"), ("linkuptime", 0),
    }
    observer.session.close()


def test_router_error_code_is_reported_without_guessing_the_cause(caplog):
    observer = _observer()
    with caplog.at_level(logging.WARNING, logger="fritzFlux"):
        observer._observe_wan_connection({
            "NewConnectionStatus": "PendingDisconnect",
            "NewLastConnectionError": "ERROR_ISP_DISCONNECT",
        })

    assert [record.getMessage() for record in caplog.records] == [
        "FRITZ!Box WAN connection is down (router's last connection error: ERROR_ISP_DISCONNECT)",
    ]


def test_observer_and_watcher_share_status_and_uptime_edge_cases(caplog):
    observer = _observer()
    with caplog.at_level(logging.INFO, logger="fritzFlux"):
        observer._observe_wan_connection({"NewConnectionStatus": " connected ", "NewUptime": "3600"})
        # bool is not an uptime and a lower-case/padded status is still a known state
        observer._observe_wan_connection({"NewConnectionStatus": "DISCONNECTED ", "NewUptime": True})
    assert [record.getMessage() for record in caplog.records] == ["FRITZ!Box WAN connection is down"]
    assert observer._wan_uptime == 3600


def test_float_uptime_is_not_accepted():
    observer = _observer()
    observer._observe_wan_connection({"NewConnectionStatus": "Connected", "NewUptime": 12.5})
    assert observer._wan_uptime is None


def test_untrusted_last_connection_error_is_not_logged(caplog):
    observer = _observer()
    forged = "ERROR_X\n2026-01-01 CRITICAL forged line"
    with caplog.at_level(logging.WARNING, logger="fritzFlux"):
        observer._observe_wan_connection({"NewConnectionStatus": "Disconnected", "NewLastConnectionError": forged})
        observer._wan_connected = None
        observer._observe_wan_connection({"NewConnectionStatus": "Disconnected", "NewLastConnectionError": "ERROR_" + "A" * 500})
    assert [record.getMessage() for record in caplog.records] == [
        "FRITZ!Box WAN connection is down",
        "FRITZ!Box WAN connection is down",
    ]
