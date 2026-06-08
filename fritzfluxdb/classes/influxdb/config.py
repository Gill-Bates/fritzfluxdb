#!/usr/bin/env python3
#
# fritzfluxdb/classes/influxdb/config.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import configparser

from fritzfluxdb.log import get_logger
from fritzfluxdb.classes.common import ConfigBase

log = get_logger()


class InfluxDBConfig(ConfigBase):
    """
        class which defines the InfluxDB config options
    """

    version = {
        "type": int,
        "default": 1
    }
    hostname = {
        "type": str,
        "alt": "host",
        "default": None
    }
    port = {
        "type": int,
        "default": 8086
    }
    tls_enabled = {
        "type": bool,
        "alt": "ssl",
        "default": False
    }
    verify_tls = {
        "type": bool,
        "alt": "verify_ssl",
        "default": True
    }
    measurement_name = {
        "type": str,
        "default": "fritzbox"
    }
    data_retention_days = {
        "type": int,
        "default": 365
    }

    # version 1 parameters
    username = {
        "type": str,
        "default": None
    }
    password = {
        "type": str,
        "default": None
    }
    database = {
        "type": str,
        "default": None
    }

    # version 2 parameters
    token = {
        "type": str,
        "default": None
    }
    organisation = {
        "type": str,
        "default": None
    }
    bucket = {
        "type": str,
        "default": None
    }

    config_section_name = "influxdb"

    def parse_config(self, config_data: configparser.ConfigParser):

        super().parse_config(config_data)
        
        if self.tls_enabled and not self.verify_tls:
            log.warning(f"TLS certificate verification is disabled for InfluxDB at {self.hostname}; use only on trusted networks")

        # validate data
        mandatory_keys = list()
        if self.version == 1:
            mandatory_keys = ["hostname", "database"]

            if [getattr(self, "username"), getattr(self, "password")].count(None) == 1:
                log.error(f"Username and password must be defined together or not at all for InfluxDB '{self.version}'")
                self.parser_error = True

        elif self.version == 2:
            mandatory_keys = ["hostname", "token", "organisation", "bucket"]
        else:
            log.error(f"Invalid InfluxDB version '{self.version}'.")
            self.parser_error = True

        for key in mandatory_keys:
            if getattr(self, key) is None or len(getattr(self, key)) == 0:
                self.parser_error = True
                log.error(f"InfluxDB {key} not defined")
