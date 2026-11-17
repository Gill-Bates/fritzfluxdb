#!/usr/bin/env python3
#
# tools/release_meta.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Resolve the release build plan and validate the build metadata.
`plan` turns the dispatch inputs into the platform matrix and the publish decisions;
`metadata` checks version, tag and main ancestry before anything is built or pushed.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from ci_output import REPO_URL, log, print_header, write_outputs
from release_newest import VERSION_RE

__author__ = "Gill-Bates"
SCRIPT = Path(__file__).resolve()
HELP_URL = f"{REPO_URL}/blob/main/tools/{SCRIPT.name}"
ALL_PLATFORMS = (
    {"arch": "amd64", "runner": "ubuntu-24.04", "platform": "linux/amd64"},
    {"arch": "arm64", "runner": "ubuntu-24.04-arm", "platform": "linux/arm64"},
)
GIT_TIMEOUT_SECONDS = 120


class MetaError(Exception):
    """A failed precondition; ends the run with exit code 1."""


def select_platforms(requested: str) -> list[dict[str, str]]:
    """Return the platforms named in a comma-separated list; empty means all."""
    wanted = [item.strip() for item in requested.split(",") if item.strip()]
    known = {p["platform"] for p in ALL_PLATFORMS}
    unknown = sorted(set(wanted) - known)
    if unknown:
        raise MetaError(f"unknown platform(s): {', '.join(unknown)}")
    return [p for p in ALL_PLATFORMS if not wanted or p["platform"] in wanted]


def plan(push: str, platforms: str, gate: str) -> dict[str, str]:
    """Decide what a run publishes; a tag push carries no inputs and publishes."""
    include = select_platforms(platforms)
    publish = push != "false"
    release = publish and len(include) == len(ALL_PLATFORMS)
    enforce = publish or gate == "true"
    return {
        "matrix": json.dumps({"include": include}, separators=(",", ":")),
        "publish": str(publish).lower(),
        "release": str(release).lower(),
        "enforce_trivy_gate": str(enforce).lower(),
    }


def git(*args: str) -> str:
    """Run git in the checkout and return stdout; a failure is a MetaError."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MetaError(f"git {args[0]} failed: {exc}") from exc
    if result.returncode != 0:
        raise MetaError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def remote_tag_commit(tag: str) -> str:
    """Return the commit a remote tag points at (peeled), or '' when it is absent."""
    peeled = direct = ""
    for line in git("ls-remote", "--tags", "origin").splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/tags/{tag}^{{}}":
            peeled = sha
        elif ref == f"refs/tags/{tag}":
            direct = sha
    return peeled or direct


def read_version(path: Path) -> str:
    if not path.is_file():
        raise MetaError(f"{path} not found")
    with path.open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise MetaError(f"'{version}' in {path} is not a release version")
    return version


def check_tag(version: str, ref_type: str, ref_name: str) -> None:
    """A tag push must name the version declared in pyproject.toml."""
    if ref_type != "tag":
        return
    tag_version = ref_name.removeprefix("v")
    if tag_version != version:
        raise MetaError(
            f"Git tag (v{tag_version}) does not match pyproject.toml ({version})"
        )


def check_release_tag(version: str, build_commit: str) -> None:
    """An existing release tag must point at the commit this run builds."""
    tag = f"v{version}"
    tag_commit = remote_tag_commit(tag)
    if tag_commit and tag_commit != build_commit:
        raise MetaError(
            f"tag {tag} points at {tag_commit}, but this run builds {build_commit}. "
            f"Bump the version in pyproject.toml or dispatch the run on {tag}."
        )


def check_main_ancestry(ref_name: str, sha: str) -> None:
    """Anything that is published must already be contained in main."""
    git("fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main")
    try:
        git("merge-base", "--is-ancestor", sha, "origin/main")
    except MetaError as exc:
        raise MetaError(
            f"{ref_name} points at {sha}, which is not contained in main. "
            "Merge the commit into main first, then publish that commit."
        ) from exc


def metadata(env: dict[str, str], pyproject: Path) -> dict[str, str]:
    version = read_version(pyproject)
    check_tag(version, env.get("GITHUB_REF_TYPE", ""), env.get("GITHUB_REF_NAME", ""))
    sha = env.get("GITHUB_SHA", "")
    if env.get("RELEASE") == "true":
        check_release_tag(version, git("rev-parse", "HEAD").strip())
    if env.get("PUBLISH") == "true":
        check_main_ancestry(env.get("GITHUB_REF_NAME", ""), sha)
    build_date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    log("info", f"Version: {version}, Build date: {build_date}")
    return {"build_date": build_date, "version": version}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").strip().splitlines()[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="Resolve matrix and publish decisions from the inputs.")
    meta = sub.add_parser("metadata", help="Validate version, tag and ancestry.")
    meta.add_argument("--pyproject", default="pyproject.toml", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    print_header(SCRIPT, (__doc__ or "").strip().splitlines()[0], __author__, HELP_URL)
    args = build_parser().parse_args(argv)
    try:
        if args.command == "plan":
            values = plan(
                os.environ.get("PUSH_INPUT", ""),
                os.environ.get("PLATFORMS_INPUT", ""),
                os.environ.get("GATE_INPUT", ""),
            )
        else:
            values = metadata(dict(os.environ), args.pyproject)
        write_outputs(values)
    except (MetaError, ValueError, KeyError, tomllib.TOMLDecodeError) as exc:
        log("error", str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
