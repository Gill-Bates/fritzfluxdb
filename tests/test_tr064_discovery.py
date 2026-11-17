#!/usr/bin/env python3
#
# tests/test_tr064_discovery.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Device description and SCPD discovery: keys, provenance, ambiguity, path rules."""

import logging

import pytest

from app.fritzbox.tr064.discovery import (
    MAX_ACTIONS,
    MAX_ARGUMENTS,
    MAX_PATH_LENGTH,
    MAX_SERVICES,
    MAX_STATE_VARIABLES,
    build_discovery,
    parse_description,
    parse_scpd,
    service_key,
    validate_path,
)
from app.fritzbox.tr064.exceptions import (
    TR064AccessDisabled,
    TR064MalformedXml,
    TR064ProtocolError,
    TR064ServiceNotFound,
)
from tests.fakebox import FakeFritzBox, FakeService, description_xml, scpd_xml


def discover(box: FakeFritzBox):
    igd = box.fetch("/igddesc.xml") if any(s.document == "igddesc.xml" for s in box.services) else None
    return build_discovery(igd, box.fetch("/tr64desc.xml"))


def raw_description(entries: str) -> bytes:
    return f'<root xmlns="urn:x"><device><serviceList>{entries}</serviceList></device></root>'.encode()


def raw_service(key: str, *, control: str = "/c", scpd: str = "/s", type_name: str = "urn:t:T:1") -> str:
    return (
        f"<service><serviceType>{type_name}</serviceType><serviceId>urn:x:serviceId:{key}</serviceId>"
        f"<controlURL>{control}</controlURL><SCPDURL>{scpd}</SCPDURL></service>"
    )


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("WANIPConn", "WANIPConn1"),
        ("WANCommonIFC", "WANCommonIFC1"),
        ("WANIPConnection:1", "WANIPConnection1"),
        ("Time:1", "Time1"),
        ("DeviceInfo", "DeviceInfo1"),
        ("WANPPPConnection:1", "WANPPPConnection1"),
        ("WANDSLInterfaceConfig", "WANDSLInterfaceConfig1"),
        ("WANCommonInterfaceConfig:1", "WANCommonInterfaceConfig1"),
        ("WLANConfiguration:3", "WLANConfiguration3"),
        ("WLANConfiguration2", "WLANConfiguration2"),
        ("X_AVM-DE_RemoteAccess", "X_AVM-DE_RemoteAccess1"),
        ("WANPPPConnection:2", "WANPPPConnection2"),
    ],
)
def test_service_key_matches_the_old_normalisation(name, key):
    assert service_key(name) == key


@pytest.mark.parametrize("name", ["Foo:", ":1", "Foo:abc", "Foo:1:2", "Foo:\u0661"])
def test_malformed_service_version_is_rejected(name):
    with pytest.raises(TR064ProtocolError):
        service_key(name)


def test_empty_service_name_is_rejected():
    with pytest.raises(TR064ProtocolError):
        service_key("")


# alias -> (key, origin document, serviceType, controlURL)
ALIASES = {
    "WANIPConn": ("WANIPConn1", "igddesc.xml", "urn:schemas-upnp-org:service:WANIPConnection:1", "/igdupnp/control/wanipconn1"),
    "WANCommonIFC": ("WANCommonIFC1", "igddesc.xml", "urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1", "/igdupnp/control/wancommonifc1"),
    "WANIPConnection:1": ("WANIPConnection1", "tr64desc.xml", "urn:dslforum-org:service:WANIPConnection:1", "/upnp/control/wanipconnection1"),
    "Time:1": ("Time1", "tr64desc.xml", "urn:dslforum-org:service:Time:1", "/upnp/control/time1"),
    "DeviceInfo": ("DeviceInfo1", "tr64desc.xml", "urn:dslforum-org:service:DeviceInfo:1", "/upnp/control/deviceinfo1"),
    "WANPPPConnection:1": ("WANPPPConnection1", "tr64desc.xml", "urn:dslforum-org:service:WANPPPConnection:1", "/upnp/control/wanpppconnection1"),
    "WANDSLInterfaceConfig": ("WANDSLInterfaceConfig1", "tr64desc.xml", "urn:dslforum-org:service:WANDSLInterfaceConfig:1", "/upnp/control/wandslinterfaceconfig1"),
}


@pytest.mark.parametrize("alias", sorted(ALIASES))
def test_used_aliases_resolve_to_documented_documents(alias):
    key, origin, service_type, control_url = ALIASES[alias]
    info = discover(FakeFritzBox()).resolve(alias)
    assert (info.key, info.origin, info.service_type, info.control_url) == (key, origin, service_type, control_url)


def test_alias_absent_from_both_documents_is_not_found():
    with pytest.raises(TR064ServiceNotFound) as info:
        discover(FakeFritzBox()).resolve("WANCommonInterfaceConfig:1")
    assert info.value.wanted == "WANCommonInterfaceConfig1"


def test_nested_services_are_found_at_every_level():
    discovery = discover(FakeFritzBox())
    assert {"DeviceInfo1", "WANPPPConnection1", "WANIPConn1", "WANCommonIFC1"} <= set(discovery.services)


def test_same_type_name_in_two_namespaces_is_two_services():
    discovery = discover(FakeFritzBox())
    igd, tr64 = discovery.resolve("WANIPConn"), discovery.resolve("WANIPConnection:1")
    assert igd.key != tr64.key
    assert igd.service_type.endswith(":WANIPConnection:1") and tr64.service_type.endswith(":WANIPConnection:1")
    assert igd.service_type != tr64.service_type


def test_same_service_type_under_different_keys_keeps_both():
    xml = raw_description(raw_service("A1") + raw_service("B1"))
    assert set(parse_description(xml, "tr64desc.xml")) == {"A1", "B1"}


def test_key_collision_prefers_tr64desc(caplog):
    igd = raw_description(raw_service("Same1", control="/igd", type_name="urn:igd:S:1"))
    tr64 = raw_description(raw_service("Same1", control="/tr64", type_name="urn:tr64:S:1"))
    with caplog.at_level(logging.DEBUG, logger="fritzFlux.tr064"):
        info = build_discovery(igd, tr64).resolve("Same")
    assert (info.control_url, info.origin, info.service_type) == ("/tr64", "tr64desc.xml", "urn:tr64:S:1")
    assert "urn:igd:S:1" in caplog.text and "urn:tr64:S:1" in caplog.text


def test_key_repeated_inside_one_document_keeps_the_last_entry():
    xml = raw_description(raw_service("D1", control="/first") + raw_service("D1", control="/last"))
    assert parse_description(xml, "tr64desc.xml")["D1"].control_url == "/last"


def test_explicit_version_never_falls_back():
    discovery = discover(FakeFritzBox())
    assert discovery.resolve("WANPPPConnection:1").key == "WANPPPConnection1"
    with pytest.raises(TR064ServiceNotFound) as info:
        discovery.resolve("WANPPPConnection:2")
    assert info.value.wanted == "WANPPPConnection2"


def test_missing_igddesc_is_not_an_error():
    box = FakeFritzBox([s for s in FakeFritzBox().services if s.document == "tr64desc.xml"])
    assert discover(box).resolve("DeviceInfo").key == "DeviceInfo1"
    discovery = build_discovery(None, box.fetch("/tr64desc.xml"))
    with pytest.raises(TR064ServiceNotFound):
        discovery.resolve("WANIPConn")


def test_non_xml_igddesc_is_skipped():
    box = FakeFritzBox()
    discovery = build_discovery(b"<html><body>no</body></html", box.fetch("/tr64desc.xml"))
    assert "WANIPConn1" not in discovery.services and "DeviceInfo1" in discovery.services


def test_missing_tr64desc_is_access_disabled():
    with pytest.raises(TR064AccessDisabled):
        build_discovery(None, None)


@pytest.mark.parametrize("body", [b"<html><body>Login</body></html", b"", b"plain text", b"<other/>", b"<root><device></root>"])
def test_damaged_tr64desc_is_malformed_not_access_disabled(body):
    # A broken body proves nothing about the switch in the FRITZ!Box; it must not become exit 78.
    with pytest.raises(TR064MalformedXml) as info:
        build_discovery(None, body)
    assert not isinstance(info.value, TR064AccessDisabled)


def test_well_formed_tr64desc_without_services_is_an_empty_discovery():
    assert build_discovery(None, b"<root><device/></root>").services == {}


def test_incomplete_service_entries_are_skipped():
    xml = raw_description(
        "<service><serviceType>urn:t:T:1</serviceType><serviceId>urn:x:serviceId:NoUrl1</serviceId></service>" + raw_service("Ok1")
    )
    assert set(parse_description(xml, "tr64desc.xml")) == {"Ok1"}


@pytest.mark.parametrize(
    "path",
    [
        "", "relative", "//evil.example/x", "http://192.168.178.1:49000/x", "/a@evil.example/x", "/a b", "/a\\b",
        "/a#frag", "/a\nb", "/" + "x" * MAX_PATH_LENGTH,
        "/upnp/control?mode=test", "/upnp/control?", "/upnp/../other", "/upnp/./x", "/..", "/upnp/..",
        "/upnp/%2e%2e/other", "/upnp/%2E%2E/other", "/upnp/.%2e/other", "/upnp/%252e%252e/other",
        "/upnp/%25252e%25252e/other", "/upnp/..%2fother", "/upnp/%5c..%5cother", "/upnp/a%5cb",
        "/upnp/a%ff", "/upnp/a%c3", "/a/\u00e9", "/a/\u2024\u2024/x", "/upnp/a%00b", "/upnp/a%0ab", "/upnp/a%3fb", "/upnp/a%23b", "/upnp/a%40b", "/upnp/a\x7fb", "/upnp/a\tb",
    ],
)
def test_foreign_or_malformed_paths_are_rejected(path):
    with pytest.raises(TR064ProtocolError):
        validate_path(path)


@pytest.mark.parametrize("path", ["/upnp/control/wanpppconn1", "/deviceinfoSCPD.xml", "/upnp/a.b/c..d", "/upnp/%41b", "/igdupnp/control/WANIPConn1"])
def test_ordinary_avm_paths_are_accepted(path):
    assert validate_path(path) == path


def test_path_at_the_length_limit_is_accepted():
    path = "/" + "x" * (MAX_PATH_LENGTH - 1)
    assert validate_path(path) == path


@pytest.mark.parametrize("field", ["control", "scpd"])
def test_description_with_hostile_url_is_rejected(field):
    entry = raw_service("Evil1", **{field: "@evil.example/x"})
    with pytest.raises(TR064ProtocolError):
        parse_description(raw_description(entry), "tr64desc.xml")


def test_limit_services_accepts_100_and_rejects_101():
    def many(count: int) -> bytes:
        return raw_description("".join(raw_service(f"S{i}") for i in range(count)))

    assert len(parse_description(many(MAX_SERVICES), "tr64desc.xml")) == MAX_SERVICES
    with pytest.raises(TR064ProtocolError, match="services"):
        parse_description(many(MAX_SERVICES + 1), "tr64desc.xml")


def test_limit_services_apply_to_the_merged_result():
    def many(prefix: str, count: int) -> bytes:
        return raw_description("".join(raw_service(f"{prefix}{i}") for i in range(count)))

    with pytest.raises(TR064ProtocolError, match="services"):
        build_discovery(many("I", 60), many("T", 60))


def test_overlong_service_id_is_rejected():
    with pytest.raises(TR064ProtocolError, match="too long"):
        parse_description(raw_description(raw_service("K" * 200)), "tr64desc.xml")


def test_scpd_actions_arguments_and_types():
    service = next(s for s in FakeFritzBox().services if s.name == "WANPPPConnection1")
    scpd = parse_scpd(scpd_xml(service).encode())
    assert scpd.has_action("GetInfo") and scpd.has_action("SetEnable") and not scpd.has_action("Nope")
    get_info = scpd.actions["GetInfo"]
    assert [(a.name, a.data_type) for a in get_info.out_arguments] == [("NewUptime", "ui4"), ("NewConnectionStatus", "string")]
    assert [(a.name, a.direction, a.data_type) for a in scpd.actions["SetEnable"].in_arguments] == [("NewEnable", "in", "boolean")]


def test_scpd_unresolvable_argument_type_stays_string():
    xml = (
        b"<scpd><actionList><action><name>A</name><argumentList><argument><name>X</name>"
        b"<direction>out</direction><relatedStateVariable>Gone</relatedStateVariable></argument></argumentList></action></actionList></scpd>"
    )
    assert parse_scpd(xml).actions["A"].out_arguments[0].data_type == "string"


def test_scpd_wrong_root_is_malformed():
    with pytest.raises(TR064ProtocolError):
        parse_scpd(b"<other/>")


def test_limit_actions_arguments_and_state_variables():
    def scpd(actions: int = 1, arguments: int = 0, variables: int = 0) -> bytes:
        arg = "".join(f"<argument><name>N{i}</name><direction>in</direction></argument>" for i in range(arguments))
        acts = "".join(f"<action><name>A{i}</name><argumentList>{arg}</argumentList></action>" for i in range(actions))
        table = "".join(f"<stateVariable><name>V{i}</name><dataType>ui4</dataType></stateVariable>" for i in range(variables))
        return f"<scpd><actionList>{acts}</actionList><serviceStateTable>{table}</serviceStateTable></scpd>".encode()

    assert len(parse_scpd(scpd(actions=MAX_ACTIONS)).actions) == MAX_ACTIONS
    assert len(parse_scpd(scpd(arguments=MAX_ARGUMENTS)).actions["A0"].arguments) == MAX_ARGUMENTS
    assert parse_scpd(scpd(variables=MAX_STATE_VARIABLES)).actions
    for kwargs in ({"actions": MAX_ACTIONS + 1}, {"arguments": MAX_ARGUMENTS + 1}, {"variables": MAX_STATE_VARIABLES + 1}):
        with pytest.raises(TR064ProtocolError):
            parse_scpd(scpd(**kwargs))


def _scpd_document(*, action_lists=(), tables=()):
    acts = "".join(f"<actionList>{a}</actionList>" for a in action_lists)
    tabs = "".join(f"<serviceStateTable>{t}</serviceStateTable>" for t in tables)
    return f"<scpd>{acts}{tabs}</scpd>".encode()


def _actions(prefix: str, count: int) -> str:
    return "".join(f"<action><name>{prefix}{i}</name></action>" for i in range(count))


def _variables(prefix: str, count: int) -> str:
    return "".join(f"<stateVariable><name>{prefix}{i}</name></stateVariable>" for i in range(count))


def test_limit_actions_are_counted_across_all_action_lists():
    half = MAX_ACTIONS // 2
    assert len(parse_scpd(_scpd_document(action_lists=[_actions("A", half), _actions("B", half)])).actions) == MAX_ACTIONS
    with pytest.raises(TR064ProtocolError, match="actions"):
        parse_scpd(_scpd_document(action_lists=[_actions("A", half), _actions("B", half), _actions("C", 1)]))
    with pytest.raises(TR064ProtocolError, match="actions"):
        parse_scpd(_scpd_document(action_lists=[_actions("A", MAX_ACTIONS - 1)] * 2))


def test_limit_state_variables_are_counted_across_all_tables():
    half = MAX_STATE_VARIABLES // 2
    parse_scpd(_scpd_document(tables=[_variables("A", half), _variables("B", half)]))
    with pytest.raises(TR064ProtocolError, match="state variables"):
        parse_scpd(_scpd_document(tables=[_variables("A", half), _variables("B", half), _variables("C", 1)]))


def test_limit_arguments_are_counted_across_all_argument_lists_of_one_action():
    def argument_list(prefix: str, count: int) -> str:
        items = "".join(f"<argument><name>{prefix}{i}</name><direction>in</direction></argument>" for i in range(count))
        return f"<argumentList>{items}</argumentList>"

    half = MAX_ARGUMENTS // 2
    ok = f"<action><name>A</name>{argument_list('X', half)}{argument_list('Y', half)}</action>"
    assert len(parse_scpd(_scpd_document(action_lists=[ok])).actions["A"].arguments) == MAX_ARGUMENTS
    bad = f"<action><name>A</name>{argument_list('X', half)}{argument_list('Y', half + 1)}</action>"
    with pytest.raises(TR064ProtocolError, match="arguments"):
        parse_scpd(_scpd_document(action_lists=[bad]))
    # The limit is per action, not per document.
    two = "".join(f"<action><name>A{i}</name>{argument_list('X', MAX_ARGUMENTS)}</action>" for i in range(2))
    assert len(parse_scpd(_scpd_document(action_lists=[two])).actions) == 2


def test_description_helpers_render_a_nested_layout():
    services = [FakeService("A1", "A:1", "tr64desc.xml", depth=3), FakeService("B1", "B:1", "tr64desc.xml", depth=1)]
    assert set(parse_description(description_xml(services).encode(), "tr64desc.xml")) == {"A1", "B1"}
