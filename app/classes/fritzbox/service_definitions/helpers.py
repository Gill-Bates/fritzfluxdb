#!/usr/bin/env python3
#
# app/classes/fritzbox/service_definitions/helpers.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from app.common import parse_bool


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
    return parse_bool(value)


def parse_required_text_response(response) -> str:
    """Return the body of a FritzBox endpoint that must answer with 200."""
    if response.status_code != 200:
        url = getattr(response, "url", "<unknown>")
        raise ValueError(f"unexpected HTTP status {response.status_code} for {url}")

    return response.text
