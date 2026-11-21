#!/usr/bin/env python3
#
# app/fritzbox/banner.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#
"""Startup banner for fritzfluxdb, rendered by the shared logging package."""

from __future__ import annotations

import gillbates_logging

_LOGO = r"""
  __      _ _        __ _               _ _
 / _|_ __(_) |_ ____/ _| |_   ___  ____| | |__
| |_| '__| | __|_  / |_| | | | \ \/ / _` | '_ \
|  _| |  | | |_ / /|  _| | |_| |>  < (_| | |_) |
|_| |_|  |_|\__/___|_| |_|\__,_/_/\_\__,_|_.__/
"""


def print_banner() -> None:
    """Print the fritzfluxdb startup banner and the release notice to stdout.

    Version, git sha and build date come from BUILD_INFO (via app.utils.version).
    Never raises: a cosmetic banner must not be able to take the daemon down.
    """
    try:
        from app.utils.version import (
            AUTHOR,
            BUILD_DATE,
            GIT_SHA,
            REPO_SLUG,
            URL,
            VERSION,
        )

        version_line = f"v{VERSION}"
        if GIT_SHA:
            version_line += f" ({GIT_SHA[:7]})"
        if BUILD_DATE:
            version_line += f"  -  built {BUILD_DATE.split('T', 1)[0]}"
        gillbates_logging.print_banner(
            _LOGO,
            version_line,
            [f"(C) 2026 {AUTHOR} - {URL}"],
            release_repo=REPO_SLUG,
            version=VERSION,
        )
    except Exception:  # noqa: BLE001, S110 - a cosmetic banner must never take the daemon down
        pass
