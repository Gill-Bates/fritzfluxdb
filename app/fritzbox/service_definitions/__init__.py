#!/usr/bin/env python3
#
# app/fritzbox/service_definitions/__init__.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

tr064_services = []
lua_services = []


# These imports register service definitions in the initialized lists above.
import app.fritzbox.service_definitions.connection_info
import app.fritzbox.service_definitions.homeauto
import app.fritzbox.service_definitions.logs
import app.fritzbox.service_definitions.network_hosts
import app.fritzbox.service_definitions.system_stats
import app.fritzbox.service_definitions.telephone_list
import app.fritzbox.service_definitions.tr064
import app.fritzbox.service_definitions.vpn_data  # noqa: F401
