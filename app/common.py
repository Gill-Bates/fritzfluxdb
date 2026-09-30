#!/usr/bin/env python3
#
# app/common.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import os
import sys
from functools import cache


def do_error_exit(log_text):
    """
    log an error and exit with return code 1

    Parameters
    ----------
    log_text : str
        the text to log as error
    """

    print(f"Error: {log_text}", file=sys.stderr)
    raise SystemExit(1)


def grab(structure=None, path: str | None = None, separator=".", fallback=None):
    """
        get data from a complex object/json structure with a
        "." separated path information. If a part of a path
        is not not present then this function returns the
        value of fallback (default: "None").

        example structure:
            data_structure = {
              "rows": [{
                "elements": [{
                  "distance": {
                    "text": "94.6 mi",
                    "value": 152193
                  },
                  "status": "OK"
                }]
              }]
            }
        example path:
            "rows.0.elements.0.distance.value"
        example return value:
            15193

        Parameters
        ----------
        structure: dict, list, object
            object structure to extract data from
        path: str
            nested path to extract
        separator: str
            path separator to use. Helpful if a path element
            contains the default (.) separator.
        fallback: dict, list, str, int
            data to return if no match was found

        Returns
        -------
        str, dict, list
            the desired path element if found, otherwise None
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


@cache
def in_test_mode():
    """read TESTMODE once per process; the result is cached"""

    # bool() would make "0" and "false" truthy, so parse the value explicitly
    test_mode = os.environ.get("TESTMODE", "").strip().lower() in {"1", "true", "t", "yes", "on"}
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
        if normalized in {"true", "t", "1", "yes", "on"}:
            return True
        if normalized in {"false", "f", "0", "no", "off"}:
            return False

    raise ValueError(f"invalid boolean value: {value!r}")
