#!/usr/bin/env python3
#
# app/fritzbox/service_definitions/helpers.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#



import threading
import time

from app.utils.log import get_logger

log = get_logger()

_warning_lock = threading.Lock()
_last_warning: dict[tuple, float] = {}


def warn_rate_limited(key: tuple, message: str, *args, interval: float = 300.0) -> bool:
    """Log a warning at most once per interval and key; below that it stays at debug level."""
    now = time.monotonic()
    with _warning_lock:
        last = _last_warning.get(key)
        emit = last is None or now - last >= interval
        if emit:
            _last_warning[key] = now
    if emit:
        log.warning(message, *args)
    else:
        log.debug(message, *args)
    return emit


def response_path(response) -> str:
    """Return the request path without query parameters that may carry a session id."""
    try:
        request_url = response.request.url
    except (AttributeError, RuntimeError):
        return "<unknown>"
    return getattr(request_url, "path", "<unknown>")


def parse_optional_json_response(response):
    """Parse an optional FritzBox JSON endpoint; 404 means no data."""

    if response.status_code == 404:
        return {}
    return parse_required_json_response(response)


def parse_required_json_response(response):
    """Parse a FritzBox JSON endpoint that is always expected to exist; any non-200 is an error."""
    path = response_path(response)

    if response.status_code != 200:
        raise ValueError(f"unexpected HTTP status {response.status_code} for {path}")

    try:
        return response.json()
    except ValueError as exc:
        raise ValueError(f"invalid JSON response for {path}: {exc}") from exc


def parse_required_text_response(response) -> str:
    """Return the body of a FritzBox endpoint that must answer with 200."""
    if response.status_code != 200:
        raise ValueError(f"unexpected HTTP status {response.status_code} for {response_path(response)}")

    return response.text
