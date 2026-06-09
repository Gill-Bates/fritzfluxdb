#!/usr/bin/env python3
#
# fritzfluxdb/configparser.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import os
import configparser

from fritzfluxdb.log import get_logger
from fritzfluxdb.common import do_error_exit

log = get_logger()


def import_config(filenames: list | None, default_config_file: str | None = None):
    """
    Read config ini files in the given order and return configparser object

    Parameters
    ----------
    filenames: list
        list of paths of ini files to parse
    default_config_file: str
        path to default ini file

    Returns
    -------
    configparser.ConfigParser(): configparser object
    """

    # work on a local copy so the caller's list is never mutated
    config_files = list(filenames or [])

    # check if default config file actually exists and add it to the list
    if default_config_file is not None and not config_files:
        if os.path.exists(default_config_file):
            config_files.append(default_config_file)

    # check if config file exists
    config_file_errors = False
    for f in config_files:
        # check if file exists
        if not os.path.exists(f):
            log.error(f'Config file "{f}" not found')
            config_file_errors = True
            continue

        # check if it's an actual file
        if not os.path.isfile(f):
            log.error(f'Config file "{f}" is not an actual file')
            config_file_errors = True
            continue

        # check if config file is readable
        if not os.access(f, os.R_OK):
            log.error(f'Config file "{f}" not readable')
            config_file_errors = True
            continue

    if config_file_errors:
        do_error_exit("Unable to open config file.")

    config = configparser.ConfigParser()

    if not config_files:
        return config

    try:
        config.read(config_files)
    except configparser.Error as e:
        do_error_exit(f"Config Error: {e}")

    log.info("Done reading config files")

    return config

# EOF
