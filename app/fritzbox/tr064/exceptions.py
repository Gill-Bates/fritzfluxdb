#!/usr/bin/env python3
#
# app/fritzbox/tr064/exceptions.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Typed errors of the native TR-064 client.

The client only reports facts (HTTP status, SOAP fault, transport failure).
Classification such as "credentials rejected" stays with the handler.
Text that originates from the FRITZ!Box is sanitised before it enters a message.
"""

MAX_TEXT_LENGTH = 200


def sanitize_text(value: object, limit: int = MAX_TEXT_LENGTH) -> str:
    """Return ``value`` as one bounded line without control characters.

    Box-supplied text is attacker-controlled on an unverified channel, so it
    must not forge log lines.
    """
    text = "".join(" " if ch.isspace() else ch for ch in str(value) if ch.isprintable() or ch.isspace())
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "..."


class TR064Error(Exception):
    """Base class of every error raised by the native TR-064 client."""


class TR064TransportError(TR064Error):
    """Connection or read problem; the original ``httpx`` error is the cause."""


class TR064ConnectError(TR064TransportError):
    """Connection could not be established (includes TLS handshake failure)."""


class TR064ConnectTimeout(TR064TransportError):
    """Connection or TLS handshake timed out."""


class TR064TimeoutError(TR064TransportError):
    """Read, write or pool timeout after the connection was established."""


class TR064ClientClosed(TR064Error):
    """The client was closed, before the call or while it was in flight."""


class TR064HttpError(TR064Error):
    """Unexpected HTTP status or content type."""

    def __init__(self, status: int, message: str = "") -> None:
        self.status = status
        detail = f": {sanitize_text(message)}" if message else ""
        super().__init__(f"HTTP {status}{detail}")


class TR064HttpUnauthorized(TR064HttpError):
    """HTTP 401 after the digest exchange (not a SOAP fault)."""

    def __init__(self, message: str = "") -> None:
        super().__init__(401, message)


class TR064HttpForbidden(TR064HttpError):
    """HTTP 403."""

    def __init__(self, message: str = "") -> None:
        super().__init__(403, message)


class TR064ResourceMissing(TR064HttpError):
    """HTTP 404 or 410 for a description or SCPD document."""


class TR064AccessDisabled(TR064Error):
    """``tr64desc.xml`` is absent: TR-064 is switched off in the FRITZ!Box."""


class TR064ProtocolError(TR064Error):
    """Document or request violates the protocol or a safety limit."""

    def __init__(self, message: str) -> None:
        super().__init__(sanitize_text(message))


class TR064MalformedXml(TR064ProtocolError):
    """The body is not well-formed XML (or not the expected document)."""


class TR064ServiceNotFound(TR064Error):
    """The requested service key is not offered by the FRITZ!Box."""

    def __init__(self, service: str, wanted: str) -> None:
        self.service = sanitize_text(service)
        self.wanted = sanitize_text(wanted)
        super().__init__(f"unknown service: {self.service!r} (key {self.wanted!r})")


class TR064ActionNotFound(TR064Error):
    """The action is not in the SCPD, or the box answered SOAP fault 401."""

    def __init__(self, service: str, action: str) -> None:
        self.service = sanitize_text(service)
        self.action = sanitize_text(action)
        super().__init__(f"unknown action: {self.service!r}.{self.action!r}")


class TR064SoapFault(TR064Error):
    """Any other SOAP fault; ``code`` is the UPnP error code when present."""

    def __init__(self, code: int | None, description: str = "") -> None:
        self.code = code
        self.description = sanitize_text(description)
        super().__init__(f"SOAP fault {code}: {self.description}")


class TR064NotAuthorized(TR064SoapFault):
    """SOAP fault 606."""
