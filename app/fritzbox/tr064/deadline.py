#!/usr/bin/env python3
#
# app/fritzbox/tr064/deadline.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Overall wall-clock deadline and size limit for one FRITZ!Box HTTP response.

httpx timeouts apply per phase and per read, so a peer that sends one byte per
read interval never times out. Shutdown must not wait for such a peer, hence
every request carries one deadline that bounds the whole exchange. Both the
TR-064 client and the Lua handler read their bodies through ``read_bounded``.
"""

import time
from collections.abc import Callable

import httpx

from app.fritzbox.tr064.exceptions import TR064ProtocolError

# Worst case after a signal is about twice this value (a byte right before the deadline, then
# one read timeout); that plus SHUTDOWN_RESERVE stays below WATCHDOG_SHUTDOWN_TIMEOUT.
MAX_REQUEST_SECONDS = 10.0


def request_timeout(connect_timeout: float) -> httpx.Timeout:
    """Return the per-phase timeouts derived from ``connect_timeout``, capped at the deadline."""
    limit = min(connect_timeout * 4, MAX_REQUEST_SECONDS)
    return httpx.Timeout(limit, connect=min(connect_timeout, limit))


class RequestDeadline:
    """Wall-clock budget of one request, started on creation."""

    def __init__(self, timeout: httpx.Timeout, clock: Callable[[], float] = time.monotonic) -> None:
        read = MAX_REQUEST_SECONDS if timeout.read is None else timeout.read
        self._clock = clock
        self._end = clock() + min(read, MAX_REQUEST_SECONDS)

    def remaining(self) -> float:
        return self._end - self._clock()

    def check(self) -> None:
        """Raise a read timeout (a transient transport error) once the budget is used up."""
        if self.remaining() <= 0:
            raise httpx.ReadTimeout("overall request deadline exceeded")

    def timeout_for_next_send(self, base: httpx.Timeout) -> httpx.Timeout:
        """Return ``base`` with every phase capped at the remaining budget."""
        self.check()
        remaining = self.remaining()
        return httpx.Timeout(**{
            name: remaining if value is None else min(value, remaining) for name, value in base.as_dict().items()
        })


def read_bounded(response: httpx.Response, max_bytes: int, deadline: RequestDeadline) -> bytes:
    """Read the decoded body up to ``max_bytes``, checking the deadline after every chunk."""
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise TR064ProtocolError(f"response larger than {max_bytes} bytes")
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise TR064ProtocolError(f"response larger than {max_bytes} bytes")
        deadline.check()
    return bytes(body)
