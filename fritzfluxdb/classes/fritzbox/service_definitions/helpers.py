#!/usr/bin/env python3
#
# fritzfluxdb/classes/fritzbox/service_definitions/helpers.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#


def parse_optional_json_response(response):
    """Parse an optional FritzBox JSON endpoint; 404 means no data."""
    url = getattr(response, "url", "<unknown>")

    if response.status_code == 404:
        return {}
    if response.status_code != 200:
        raise ValueError(f"unexpected HTTP status {response.status_code} for {url}")

    try:
        return response.json()
    except ValueError as exc:
        raise ValueError(f"invalid JSON response for {url}: {exc}") from exc


def parse_required_json_response(response):
    """Parse a FritzBox JSON endpoint that is always expected to exist; any non-200 is an error."""
    url = getattr(response, "url", "<unknown>")

    if response.status_code != 200:
        raise ValueError(f"unexpected HTTP status {response.status_code} for {url}")

    try:
        return response.json()
    except ValueError as exc:
        raise ValueError(f"invalid JSON response for {url}: {exc}") from exc


def parse_fritzbox_bool(value) -> bool:
    """Convert the boolean encodings returned by FritzBox JSON endpoints."""
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
