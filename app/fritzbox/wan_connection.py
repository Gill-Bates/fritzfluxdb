#!/usr/bin/env python3
#
# app/fritzbox/wan_connection.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Resolve the active WAN service independently of the physical access type."""

import re

from app.fritzbox.model import FritzBoxModel
from app.fritzbox.tr064.exceptions import (
    TR064ActionNotFound,
    TR064HttpForbidden,
    TR064HttpUnauthorized,
    TR064NotAuthorized,
    TR064ProtocolError,
    TR064ServiceNotFound,
)

DSL_STATUS_CALL = ("WANPPPConnection:1", "GetInfo")
OTHER_STATUS_CALL = ("WANIPConn", "GetStatusInfo")
METADATA_REFRESH_S = 60


def parse_default_connection(value: object) -> str:
    if isinstance(value, str) and len(value) <= 1024:
        match = re.search(r"(?:^|[.:])(WAN(?:PPP|IP)Connection)[.:]?([1-9][0-9]*)\Z", value.strip())
        if match:
            return f"{match[1]}:{match[2]}"
    raise TR064ProtocolError("invalid default WAN connection service")


def resolve_status_call(session, link_type: str | None) -> tuple[str, str]:
    try:
        if session.has_action("Layer3Forwarding", "GetDefaultConnectionService"):
            result = session.call_action("Layer3Forwarding", "GetDefaultConnectionService")
            service = parse_default_connection(result.get("NewDefaultConnectionService"))
            return service, "GetInfo"
    except (TR064HttpUnauthorized, TR064HttpForbidden, TR064NotAuthorized):
        # Reading the default route needs configuration rights that a monitoring user may lack.
        pass
    # Older boxes without Layer3Forwarding keep their compatibility path; failures are retried.
    if link_type == "DSL":
        return DSL_STATUS_CALL
    if session.has_action("WANIPConnection:1", "GetInfo"):
        return "WANIPConnection:1", "GetInfo"
    return OTHER_STATUS_CALL


def resolve_link_type(session, model: str | None) -> str:
    try:
        info = session.call_action("WANCommonInterfaceConfig:1", "GetCommonLinkProperties")
    except (TR064ServiceNotFound, TR064ActionNotFound):
        info = session.call_action("WANCommonIFC", "GetCommonLinkProperties")
    return FritzBoxModel.get_link_type(model, info.get("NewWANAccessType"))
