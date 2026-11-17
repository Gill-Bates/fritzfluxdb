#!/usr/bin/env python3
#
# app/utils/common.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import os
import sys
from functools import cache


def do_error_exit(log_text):
    """
    log a permanent configuration error and exit with EX_CONFIG (78)

    Parameters
    ----------
    log_text : str
        the text to log as error
    """

    print(f"Error: {log_text}", file=sys.stderr)
    # 78 keeps the entrypoint watchdog from restarting a daemon that cannot start with this config
    raise SystemExit(78)


def grab(structure=None, path: str | None = None, separator=".", fallback=None):
    """
        Get data from a nested dict/list/object structure with a
        separator-delimited path. If a part of the path is not
        present, the value of fallback (default: None) is returned.

        example:
            data = {"rows": [{"distance": {"value": 152193}}]}
            grab(data, "rows.0.distance.value")  # -> 152193

        Parameters
        ----------
        structure: dict, list, object
            object structure to extract data from
        path: str
            nested path to extract
        separator: str
            path separator to use. Helpful if a path element
            contains the default (.) separator.
        fallback: object
            data to return if no match was found (any type)

        Returns
        -------
        object
            the path element (any type, including numbers, bool or an object
            attribute) if found, otherwise fallback
    """

    if structure is None or path is None:
        return fallback

    current = structure
    for attribute in path.split(separator):
        try:
            if isinstance(current, list):
                current = current[int(attribute)]
            elif isinstance(current, dict):
                key = next(
                    (k for k in current if str(k).lower() == attribute.lower()),
                    None,
                )
                if key is None:
                    return fallback
                current = current[key]
            else:
                current = getattr(current, attribute)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            return fallback

        if current is None:
            return fallback

    return current


_TRUE_WORDS = frozenset({"true", "t", "1", "yes", "on"})
_FALSE_WORDS = frozenset({"false", "f", "0", "no", "off"})


class PermanentConfigError(Exception):
    """A counterpart rejects the configuration for good; the daemon exits with 78 instead of retrying."""


@cache
def in_test_mode():
    """read TESTMODE once per process; the result is cached"""

    # bool() would make "0" and "false" truthy, so parse the value explicitly
    test_mode = os.environ.get("TESTMODE", "").strip().lower() in _TRUE_WORDS
    if test_mode:
        print("Running in TESTMODE")

    return test_mode


def parse_bool(value) -> bool:
    """Convert the boolean encodings used by FritzBox endpoints and config values.

    bool() must never be used for these: it turns "0" and "false" into True.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_WORDS:
            return True
        if normalized in _FALSE_WORDS:
            return False

    raise ValueError(f"invalid boolean value: {value!r}")
