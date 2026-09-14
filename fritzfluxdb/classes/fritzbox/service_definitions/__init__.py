#!/usr/bin/env python3
#
# fritzfluxdb/classes/fritzbox/service_definitions/__init__.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

tr069_services = []
lua_services = []


# These imports register service definitions in the initialized lists above.
import fritzfluxdb.classes.fritzbox.service_definitions.connection_info  # noqa: E402
import fritzfluxdb.classes.fritzbox.service_definitions.homeauto  # noqa: E402
import fritzfluxdb.classes.fritzbox.service_definitions.logs  # noqa: E402
import fritzfluxdb.classes.fritzbox.service_definitions.network_hosts  # noqa: E402
import fritzfluxdb.classes.fritzbox.service_definitions.system_stats  # noqa: E402
import fritzfluxdb.classes.fritzbox.service_definitions.telephone_list  # noqa: E402
import fritzfluxdb.classes.fritzbox.service_definitions.tr069  # noqa: E402
import fritzfluxdb.classes.fritzbox.service_definitions.vpn_data  # noqa: E402, F401
