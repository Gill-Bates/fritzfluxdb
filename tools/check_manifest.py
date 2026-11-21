#!/usr/bin/env python3
#
# tools/check_manifest.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Check the release version shape and exact dependency pins in pyproject.toml."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

from ci_output import REPO_URL, log, print_header

__author__ = "Gill-Bates"
SCRIPT = Path(__file__).resolve()
HELP_URL = f"{REPO_URL}/blob/main/tools/{SCRIPT.name}"
DEFAULT_PYPROJECT = SCRIPT.parent.parent / "pyproject.toml"

VERSION_RE = re.compile(
    r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
# name, optional extras, exactly one `==` clause without wildcard, optional environment marker
PIN_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
    r"(?:\s*\[[^\]]*\])?"
    r"\s*==\s*[A-Za-z0-9][A-Za-z0-9.!+_-]*"
    r"\s*(?:;.*)?"
)


# Shared gillbates-* package pinned by the git tag of its own release in python-libraries.
LIB_PIN_RE = re.compile(
    r"(?P<name>gillbates-[a-z0-9-]+) @ git\+https://github\.com/Gill-Bates/python-libraries\.git"
    r"@(?P=name)-v[0-9]+\.[0-9]+\.[0-9]+#subdirectory=packages/[a-z0-9-]+"
)


def is_pinned(requirement: str) -> bool:
    """True for `name==version` or a gillbates-* release tag; a marker never counts as a pin."""
    requirement = requirement.strip()
    return PIN_RE.fullmatch(requirement) is not None or LIB_PIN_RE.fullmatch(requirement) is not None


def find_problems(project: dict) -> list[str]:
    """Return one message per invalid version or unpinned dependency."""
    problems = []
    version = project.get("version", "")
    if not VERSION_RE.fullmatch(version):
        problems.append(f"Invalid release version: {version!r}")
    groups = {"runtime": project.get("dependencies", [])}
    groups.update(project.get("optional-dependencies", {}))
    for group, requirements in groups.items():
        unpinned = [item for item in requirements if not is_pinned(item)]
        if unpinned:
            problems.append(f"Unpinned {group} dependencies: {unpinned}")
    return problems


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    print_header(SCRIPT, (__doc__ or "").strip().splitlines()[0], __author__, HELP_URL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pyproject", type=Path, default=DEFAULT_PYPROJECT)
    args = parser.parse_args(argv)
    with args.pyproject.open("rb") as handle:
        project = tomllib.load(handle).get("project", {})
    problems = find_problems(project)
    for problem in problems:
        log("error", problem)
    if problems:
        return 1
    log("info", f"Version {project['version']}; runtime and optional dependency groups are pinned.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
