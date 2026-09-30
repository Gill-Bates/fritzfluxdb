#!/usr/bin/env python3
#
# app/version.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Project metadata and build information for fritzfluxdb.

Static project metadata lives here; the release version is read from
pyproject.toml, with installed distribution metadata as a fallback. The git
sha and build date come from the BUILD_INFO file generated at build time.
Reading never raises - missing metadata/files fall back to sensible defaults.
"""

from __future__ import annotations

import os
import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

# --- static project metadata -----------------------------------------------
DESCRIPTION = "fritzfluxdb"
URL = "https://github.com/Gill-Bates/fritzfluxdb"
AUTHOR = "Gill-Bates"

# BUILD_INFO sits next to run.py at the project root. In a source checkout that is the
# package's parent, but the container installs the package into site-packages, so the
# project root is unrelated to __file__ there and APP_HOME points at it instead.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _build_info_candidates() -> tuple[Path, ...]:
    return tuple(root / "BUILD_INFO" for root in _project_roots())


def read_project_version() -> str:
    """Read the project version from pyproject.toml or installed metadata."""
    for root in _project_roots():
        try:
            with (root / "pyproject.toml").open("rb") as project_file:
                version = tomllib.load(project_file).get("project", {}).get("version")
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if isinstance(version, str) and version.strip():
            return version.strip()

    try:
        return _pkg_version("fritzfluxdb")
    except PackageNotFoundError:
        return "dev"


def _project_roots() -> tuple[Path, ...]:
    """Return project metadata roots for source and container installations."""
    roots = [_PROJECT_ROOT]
    app_home = os.environ.get("APP_HOME")
    if app_home:
        roots.append(Path(app_home))
    return tuple(roots)


_MAX_BUILD_INFO_BYTES = 16_384


def read_build_info(path: Path | None = None) -> dict[str, str]:
    """Parse the KEY=VALUE BUILD_INFO file. Returns {} if absent/unreadable.

    Without an explicit path the known project-root locations are tried in order.
    """
    if path is not None:
        return _parse_build_info(path)
    for candidate in _build_info_candidates():
        info = _parse_build_info(candidate)
        if info:
            return info
    return {}


def _parse_build_info(path: Path) -> dict[str, str]:
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

VERSION = read_project_version()
GIT_SHA = _BUILD.get("GIT_SHA", "")
BUILD_DATE = _BUILD.get("BUILD_DATE", "")
# Date part of the ISO build timestamp, used for CLI/help output.
VERSION_DATE = BUILD_DATE.split("T", 1)[0] if BUILD_DATE else ""
