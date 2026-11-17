#!/usr/bin/env python3
#
# tools/pyproject-deps.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Print a pyproject.toml dependency group as a pip requirements list."""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def collect(group: str | None, pyproject: Path) -> list[str]:
    """Return runtime dependencies or the named optional dependency group."""
    with pyproject.open("rb") as project_file:
        project = tomllib.load(project_file).get("project", {})

    if group is None:
        dependencies = project.get("dependencies")
        label = "[project] dependencies"
    else:
        dependencies = project.get("optional-dependencies", {}).get(group)
        label = f"[project.optional-dependencies] {group}"

    if dependencies is None:
        groups = sorted(project.get("optional-dependencies", {}))
        raise SystemExit(f"{pyproject}: no {label}. Known extras: {', '.join(groups) or 'none'}")
    return list(dependencies)


def main() -> int:
    """Write one dependency per line to stdout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", nargs="?", help="optional dependency group; omit for runtime dependencies")
    parser.add_argument("--pyproject", type=Path, default=_PYPROJECT)
    args = parser.parse_args()

    for requirement in collect(args.group, args.pyproject):
        print(requirement)
    return 0


if __name__ == "__main__":
    sys.exit(main())
