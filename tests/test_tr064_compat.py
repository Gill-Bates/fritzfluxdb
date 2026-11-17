#!/usr/bin/env python3
#
# tests/test_tr064_compat.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Native client against the values fritzconnection 1.15.1 returned (R4/R7, A.9).

``GOLDEN`` was recorded once by running the real library against a local server
that served the fixture below; the library is not imported here. The XML layout
is an assumption from the library structure, not a capture of a real box.
"""

import ast

import pytest

from app.fritzbox.tr064.exceptions import TR064ActionNotFound, TR064ServiceNotFound
from app.utils.measurement import FritzMeasurement
from tests.fakebox import FakeFritzBox, FakeService

TR64 = "tr64desc.xml"
IGD = "igddesc.xml"

# (service, action) -> {field: (type name, repr)} as returned by fritzconnection 1.15.1, or ("EXC", class name).
GOLDEN: dict[tuple[str, str], dict[str, tuple[str, str]] | tuple[str, str]] = {
    ("DeviceInfo", "GetInfo"): {
        "NewModelName": ("str", "'FRITZ!Box 7590'"),
        "NewSoftwareVersion": ("str", "'7.57'"),
        "NewSerialNumber": ("str", "'3C:A6:2F:00:00:01'"),
        "NewDescription": ("str", "'FRITZ!Box 7590 7.57'"),
        "NewUpTime": ("int", "90000"),
    },
    ("Time:1", "GetInfo"): {"NewCurrentLocalTime": ("str", "'2026-11-16T10:00:00+01:00'")},
    ("WANCommonIFC", "GetCommonLinkProperties"): {
        "NewWANAccessType": ("str", "'DSL'"),
        "NewLayer1UpstreamMaxBitRate": ("int", "100000000"),
        "NewLayer1DownstreamMaxBitRate": ("int", "250000000"),
        "NewPhysicalLinkStatus": ("str", "'Up'"),
    },
    ("WANCommonIFC", "GetAddonInfos"): {
        "NewByteSendRate": ("int", "1500"),
        "NewX_AVM_DE_TotalBytesSent64": ("str", "'123456789012'"),
    },
    ("WANIPConn", "GetStatusInfo"): {
        "NewConnectionStatus": ("str", "'Connected'"),
        "NewUptime": ("int", "3600"),
        "NewLastConnectionError": ("str", "'ERROR_NONE'"),
    },
    ("WANPPPConnection:1", "GetInfo"): {
        "NewUptime": ("str", "''"),
        "NewConnectionStatus": ("str", "'Connected'"),
        "NewX_AVM-DE_DSLSync": ("bool", "True"),
        "NewDT": ("datetime", "datetime.datetime(2026, 11, 16, 10, 0)"),
        "NewBig": ("str", "'123456789012'"),
        "NewFlag": ("str", "'maybe'"),
        "NewEnable": ("bool", "False"),
        "NewX_AVM-DE_DisconnectPreventionHour": ("int", "4"),
    },
    ("WANDSLInterfaceConfig", "GetInfo"): {
        "NewUpstreamCurrRate": ("int", "50000"),
        "NewDownstreamCurrRate": ("int", "250000"),
    },
    ("WANDSLInterfaceConfig", "X_AVM-DE_GetDSLDiagnoseInfo"): {"NewX_AVM-DE_DSLSync": ("bool", "True")},
    ("WANDSLInterfaceConfig", "X_AVM_DE_GetDSLDiagnoseInfo"): ("EXC", "AttributeError"),
    ("WANPPPConnection:2", "GetInfo"): ("EXC", "FritzServiceError"),
}

# The only intended differences (A.9/D4): None means the field is omitted instead of returned.
DEVIATIONS: dict[tuple[str, str], dict[str, tuple[str, str] | None]] = {
    ("WANPPPConnection:1", "GetInfo"): {
        "NewUptime": None,  # empty text for a numeric type
        "NewDT": ("str", "'2026-11-16T10:00:00'"),  # consumers slice strings; a datetime would break them
        "NewFlag": None,  # boolean text other than 0/1
    },
}
NEW_EXCEPTIONS = {
    ("WANDSLInterfaceConfig", "X_AVM_DE_GetDSLDiagnoseInfo"): TR064ActionNotFound,
    ("WANPPPConnection:2", "GetInfo"): TR064ServiceNotFound,
}


def compat_box() -> FakeFritzBox:
    """Box covering every service alias and SCPD data type the application reads."""
    services = [
        FakeService("DeviceInfo1", "DeviceInfo:1", TR64, actions={"GetInfo": (
            ("NewModelName", "out", "string"), ("NewSoftwareVersion", "out", "string"),
            ("NewSerialNumber", "out", "string"), ("NewDescription", "out", "string"),
            ("NewUpTime", "out", "ui4"),
        )}),
        FakeService("Time1", "Time:1", TR64, actions={"GetInfo": (("NewCurrentLocalTime", "out", "string"),)}),
        FakeService("WANCommonIFC1", "WANCommonInterfaceConfig:1", IGD, actions={
            "GetCommonLinkProperties": (
                ("NewWANAccessType", "out", "string"), ("NewLayer1UpstreamMaxBitRate", "out", "ui4"),
                ("NewLayer1DownstreamMaxBitRate", "out", "ui4"), ("NewPhysicalLinkStatus", "out", "string"),
            ),
            "GetAddonInfos": (
                ("NewByteSendRate", "out", "ui4"), ("NewX_AVM_DE_TotalBytesSent64", "out", "ui8"),
            ),
        }),
        FakeService("WANIPConn1", "WANIPConnection:1", IGD, depth=2, actions={"GetStatusInfo": (
            ("NewConnectionStatus", "out", "string"), ("NewUptime", "out", "ui4"),
            ("NewLastConnectionError", "out", "string"),
        )}),
        FakeService("WANPPPConnection1", "WANPPPConnection:1", TR64, depth=2, actions={
            "GetInfo": (
                ("NewUptime", "out", "ui4"), ("NewConnectionStatus", "out", "string"),
                ("NewX_AVM-DE_DSLSync", "out", "boolean"), ("NewDT", "out", "dateTime"),
                ("NewBig", "out", "ui8"), ("NewFlag", "out", "boolean"), ("NewEnable", "out", "boolean"),
                ("NewX_AVM-DE_DisconnectPreventionHour", "out", "ui2"),
            ),
        }),
        FakeService("WANDSLInterfaceConfig1", "WANDSLInterfaceConfig:1", TR64, depth=2, actions={
            "GetInfo": (("NewUpstreamCurrRate", "out", "ui4"), ("NewDownstreamCurrRate", "out", "ui4")),
            "X_AVM-DE_GetDSLDiagnoseInfo": (("NewX_AVM-DE_DSLSync", "out", "boolean"),),
            "X_AVM_DE_GetDSLDiagnoseInfo": (("NewX_AVM-DE_DSLSync", "out", "boolean"),),
        }),
    ]
    box = FakeFritzBox(services)
    box.respond("DeviceInfo1", "GetInfo", {
        "NewModelName": "FRITZ!Box 7590", "NewSoftwareVersion": "7.57", "NewSerialNumber": "3C:A6:2F:00:00:01",
        "NewDescription": "FRITZ!Box 7590 7.57", "NewUpTime": "90000",
    })
    box.respond("Time1", "GetInfo", {"NewCurrentLocalTime": "2026-11-16T10:00:00+01:00"})
    box.respond("WANCommonIFC1", "GetCommonLinkProperties", {
        "NewWANAccessType": "DSL", "NewLayer1UpstreamMaxBitRate": "100000000",
        "NewLayer1DownstreamMaxBitRate": "250000000", "NewPhysicalLinkStatus": "Up",
    })
    box.respond("WANCommonIFC1", "GetAddonInfos", {"NewByteSendRate": "1500", "NewX_AVM_DE_TotalBytesSent64": "123456789012"})
    box.respond("WANIPConn1", "GetStatusInfo", {
        "NewConnectionStatus": "Connected", "NewUptime": "3600", "NewLastConnectionError": "ERROR_NONE",
    })
    box.respond("WANPPPConnection1", "GetInfo", {
        "NewUptime": "", "NewConnectionStatus": "Connected", "NewX_AVM-DE_DSLSync": "1",
        "NewDT": "2026-11-16T10:00:00", "NewBig": "123456789012", "NewFlag": "maybe", "NewEnable": "0",
        "NewX_AVM-DE_DisconnectPreventionHour": "4",
    })
    box.respond("WANDSLInterfaceConfig1", "GetInfo", {"NewUpstreamCurrRate": "50000", "NewDownstreamCurrRate": "250000"})
    box.respond("WANDSLInterfaceConfig1", "X_AVM-DE_GetDSLDiagnoseInfo", {"NewX_AVM-DE_DSLSync": "1"})
    box.fault("WANDSLInterfaceConfig1", "X_AVM_DE_GetDSLDiagnoseInfo", 401, "Invalid Action")
    return box


def _native(service: str, action: str) -> dict:
    client = compat_box().tr064_client()
    try:
        return client.call_action(service, action)
    finally:
        client.close()


def _described(result: dict) -> dict[str, tuple[str, str]]:
    return {key: (type(value).__name__, repr(value)) for key, value in result.items()}


def _expected_native(key: tuple[str, str]) -> dict[str, tuple[str, str]]:
    expected = dict(GOLDEN[key])
    for field_name, replacement in DEVIATIONS.get(key, {}).items():
        if replacement is None:
            del expected[field_name]
        else:
            expected[field_name] = replacement
    return expected


@pytest.mark.parametrize("key", list(GOLDEN))
def test_native_matches_recorded_fritzconnection_results(key):
    golden = GOLDEN[key]
    if isinstance(golden, tuple):
        with pytest.raises(NEW_EXCEPTIONS[key]):
            _native(*key)
        return
    assert _described(_native(*key)) == _expected_native(key)


def test_documented_deviations_only():
    # Every listed deviation really differs from the recorded value, so the list cannot go stale.
    for key, deviations in DEVIATIONS.items():
        for field_name, replacement in deviations.items():
            assert replacement != GOLDEN[key][field_name]
    assert set(NEW_EXCEPTIONS) == {key for key, value in GOLDEN.items() if isinstance(value, tuple)}


def _recorded_value(type_name: str, text: str):
    return {"int": int, "bool": lambda t: t == "True", "str": ast.literal_eval}[type_name](text)


@pytest.mark.parametrize("key", [key for key, value in GOLDEN.items() if isinstance(value, dict)])
def test_measurement_values_keep_their_python_type(key):
    native = _native(*key)
    for field_name, (type_name, text) in GOLDEN[key].items():
        if field_name in DEVIATIONS.get(key, {}):
            continue
        old = _recorded_value(type_name, text)
        data_types = {type(old), str}
        if isinstance(old, str) and old.isdigit():
            data_types.add(int)  # the application writes such counters with ':int'
        for data_type in data_types:
            before = FritzMeasurement(field_name, old, data_type=data_type).value
            after = FritzMeasurement(field_name, native[field_name], data_type=data_type).value
            assert (type(after), after) == (type(before), before)


def test_deviating_numeric_text_yields_no_measurement_instead_of_a_logged_error(caplog):
    # fritzconnection returned '' and FritzMeasurement logged a conversion error for it.
    native = _native("WANPPPConnection:1", "GetInfo")
    assert "NewUptime" not in native
    assert FritzMeasurement("linkuptime", "", data_type=int).value is None
    assert "NewFlag" not in native
