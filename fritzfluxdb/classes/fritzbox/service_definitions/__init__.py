#
# fritzfluxdb/classes/fritzbox/service_definitions/__init__.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

tr069_services = []
lua_services = []

import fritzfluxdb.classes.fritzbox.service_definitions.connection_info
import fritzfluxdb.classes.fritzbox.service_definitions.homeauto
import fritzfluxdb.classes.fritzbox.service_definitions.logs
import fritzfluxdb.classes.fritzbox.service_definitions.network_hosts
import fritzfluxdb.classes.fritzbox.service_definitions.system_stats
import fritzfluxdb.classes.fritzbox.service_definitions.telephone_list
import fritzfluxdb.classes.fritzbox.service_definitions.tr069
import fritzfluxdb.classes.fritzbox.service_definitions.vpn_data  # noqa: F401
