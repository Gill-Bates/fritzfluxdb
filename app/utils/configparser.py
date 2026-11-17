#!/usr/bin/env python3
#
# app/utils/configparser.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import configparser
import os

from app.utils.common import do_error_exit
from app.utils.log import get_logger

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

    if default_config_file is not None and not config_files and os.path.exists(default_config_file):
        config_files.append(default_config_file)

    config_file_errors = False
    for f in config_files:
        if not os.path.exists(f):
            log.error(f'Config file "{f}" not found')
            config_file_errors = True
            continue

        if not os.path.isfile(f):
            log.error(f'Config file "{f}" is not an actual file')
            config_file_errors = True
            continue

        if not os.access(f, os.R_OK):
            log.error(f'Config file "{f}" not readable')
            config_file_errors = True

    if config_file_errors:
        do_error_exit("Unable to open config file.")

    config = configparser.ConfigParser(interpolation=None)

    if not config_files:
        return config

    # ConfigParser.read() silently skips an unopenable file, which would yield a partial
    # configuration; later files override earlier ones.
    for filename in config_files:
        try:
            with open(filename, encoding="utf-8") as source:
                config.read_file(source, source=filename)
        except (OSError, UnicodeError, configparser.Error) as e:
            do_error_exit(f'Config Error in "{filename}": {e}')

    log.info("Done reading config files")

    return config
