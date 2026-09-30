#!/usr/bin/env python3
#
# app/classes/fritzbox/version_check.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Startup check for a newer fritzfluxdb GitHub release."""

from __future__ import annotations

import re
from typing import Any

import httpx

LATEST_RELEASE_API = "https://api.github.com/repos/Gill-Bates/fritzfluxdb/releases/latest"
RELEASES_URL = "https://github.com/Gill-Bates/fritzfluxdb/releases/latest"
TIMEOUT_SECONDS = 3.0

_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?P<pre>[-+.]?[0-9A-Za-z.+-]*)$")

VersionKey = tuple[tuple[int, int, int], int]


def parse_version(raw: Any) -> VersionKey | None:
    """Return a sortable key for a version string, or None if unparsable.

    A suffix such as "-rc1" sorts below the plain release of the same numbers.
    """
    if not isinstance(raw, str):
        return None
    match = _VERSION_RE.match(raw.strip())
    if not match:
        return None
    major, minor, patch = (int(part or 0) for part in match.group(1, 2, 3))
    is_final = 0 if match.group("pre") else 1
    return (major, minor, patch), is_final


def fetch_latest_version() -> str | None:
    """Return the latest release tag, or None on any failure."""
    try:
        response = httpx.get(
            LATEST_RELEASE_API,
            timeout=TIMEOUT_SECONDS,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "fritzfluxdb-version-check",
            },
        )
        response.raise_for_status()
        tag = response.json().get("tag_name")
    except Exception:  # noqa: BLE001 - offline, rate limit or bad JSON must never affect startup
        return None
    return tag if isinstance(tag, str) else None


def build_version_message(running: str, latest_tag: str | None) -> tuple[str, str] | None:
    """Return (text, colour) with colour in {"green", "yellow"}, or None to stay silent."""
    running_key = parse_version(running)
    latest_key = parse_version(latest_tag)
    if running_key is None or latest_key is None:
        return None
    latest = str(latest_tag).strip().removeprefix("v")
    if latest_key > running_key:
        return (
            f"A new version is available: v{latest} (running: v{running.removeprefix('v')}) - {RELEASES_URL}",
            "yellow",
        )
    if latest_key == running_key:
        return "Your version is up to date.", "green"
    return "You are running a pre-release version!", "yellow"


def version_notice(running: str, use_color: bool) -> str | None:
    """Return the ready-to-print notice line (with ANSI codes if requested), or None."""
    message = build_version_message(running, fetch_latest_version())
    if message is None:
        return None
    text, color = message
    if not use_color:
        return text
    code = "\033[92m" if color == "green" else "\033[93m"
    return f"{code}{text}\033[0m"
