#!/usr/bin/env python3
#
# fritzfluxdb/version.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Project metadata and build information for fritzfluxdb.

Static project metadata lives here; the version details (version, git sha,
build date) are read from the BUILD_INFO file that is generated at build time
from the VERSION file and git. Reading never raises - missing build info falls
back to sensible defaults.
"""

from __future__ import annotations

from pathlib import Path

# --- static project metadata -----------------------------------------------
DESCRIPTION = "fritzfluxdb"
URL = "https://github.com/Gill-Bates/fritzfluxdb"
AUTHOR = "Gill-Bates"
LICENSE = "MIT"

# --- build information (from <project-root>/BUILD_INFO) ---------------------
# version.py lives in the package root, so BUILD_INFO sits one level up
# (the project root, which is /app inside the container).
_BUILD_INFO_PATH = Path(__file__).resolve().parent.parent / "BUILD_INFO"


_MAX_BUILD_INFO_BYTES = 16_384


def read_build_info(path: Path = _BUILD_INFO_PATH) -> dict[str, str]:
    """Parse the KEY=VALUE BUILD_INFO file. Returns {} if absent/unreadable."""
    info: dict[str, str] = {}
    try:
        if path.stat().st_size > _MAX_BUILD_INFO_BYTES:
            return info
        content = path.read_text(encoding="utf-8")
    except OSError:
        return info
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        info[key.strip()] = value.strip()
    return info


_BUILD = read_build_info()

VERSION = _BUILD.get("APP_VERSION") or "dev"
GIT_SHA = _BUILD.get("GIT_SHA", "")
BUILD_DATE = _BUILD.get("BUILD_DATE", "")
# Date part of the ISO build timestamp, used for CLI/help output.
VERSION_DATE = BUILD_DATE.split("T", 1)[0] if BUILD_DATE else ""
