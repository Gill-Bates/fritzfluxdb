#!/usr/bin/env python3
#
# tools/release_newest.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Compare a release version with the Docker Hub tags and clean up build tags.

`check` prints key=value lines for GITHUB_OUTPUT, `latest` prints the highest stable
release tag, and `delete` removes per-architecture fragment and candidate tags.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ci_output import REPO_URL, log, print_header

__author__ = "Gill-Bates"
SCRIPT = Path(__file__).resolve()
HELP_URL = f"{REPO_URL}/blob/main/tools/{SCRIPT.name}"

HUB = "https://hub.docker.com"
DEFAULT_REPO = "giiibates/fritzfluxdb"
DEFAULT_ARCHES = "amd64,arm64"
ARCH_NAMES = (
    "amd64",
    "arm64",
    "arm",
    "armv6",
    "armv7",
    "386",
    "ppc64le",
    "s390x",
    "riscv64",
)
CANDIDATE_PREFIX = "candidate-"
MAX_PAGES = 200
TIMEOUT_SECONDS = 30

VERSION_RE = re.compile(
    r"^([0-9]+)\.([0-9]+)\.([0-9]+)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
REPO_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$")
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


class ToolError(Exception):
    """A failure that ends the run with exit code 1."""


def is_fragment(tag: str) -> bool:
    """True for a per-architecture build tag such as 1.6.2-amd64 or 1.7.0-rc1-arm64.

    The part before the architecture must itself be a version, so only tags this
    pipeline creates match. A prerelease whose whole suffix is an architecture
    name, such as 1.7.0-arm, cannot be told apart from a fragment and counts as one.
    """
    for arch in ARCH_NAMES:
        suffix = f"-{arch}"
        if tag.endswith(suffix) and VERSION_RE.match(tag[: -len(suffix)]):
            return True
    return False


def is_candidate(tag: str) -> bool:
    return tag.startswith(CANDIDATE_PREFIX)


def parse_version(tag: str) -> tuple[Any, ...] | None:
    """Return a sortable key for a release tag, or None for any other tag.

    Numeric parts compare as numbers; a prerelease sorts below its own release and
    its dot-separated identifiers follow the SemVer order (numeric below text).
    """
    if is_fragment(tag) or is_candidate(tag):
        return None
    match = VERSION_RE.match(tag)
    if match is None:
        return None
    major, minor, patch, pre = match.groups()
    numbers = (int(major), int(minor), int(patch))
    if pre is None:
        return (*numbers, 1, ())
    ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (*numbers, 0, ids)


def compare_with_published(version: str, tags: list[str]) -> dict[str, str]:
    """Compute the check outputs for `version` against the published tags."""
    own = parse_version(version)
    if own is None:
        raise ToolError(f"'{version}' is not a release version")
    others = {
        t: k for t in tags if t != version and (k := parse_version(t)) is not None
    }
    stable = {t: k for t, k in others.items() if k[3] == 1}
    is_prerelease = own[3] == 0
    greatest = max(others, key=others.__getitem__) if others else ""
    return {
        "version": version,
        "is_prerelease": str(is_prerelease).lower(),
        "is_newest": str(all(own > k for k in others.values())).lower(),
        "is_newest_stable": str(
            not is_prerelease and all(own > k for k in stable.values())
        ).lower(),
        "latest_published": greatest,
        "published_versions": str(len(others)),
    }


def newest_stable(tags: list[str]) -> str:
    """Return the highest stable release tag; no stable tag at all is an error."""
    stable = {t: k for t in tags if (k := parse_version(t)) is not None and k[3] == 1}
    if not stable:
        raise ToolError("No stable release tag is published")
    return max(stable, key=stable.__getitem__)


def request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, bytes]:
    """Send one HTTPS request; an HTTP error status is returned, not raised."""
    if not url.startswith(f"{HUB}/"):
        raise ToolError(f"Refusing a request outside {HUB}")
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ToolError(f"{method} {url} failed: {exc}") from exc


def list_tags(repo: str) -> list[str]:
    """Return every tag name of a public repository; no tags at all is an empty list."""
    url: str | None = f"{HUB}/v2/repositories/{repo}/tags?page_size=100"
    names: list[str] = []
    for page in range(1, MAX_PAGES + 1):
        if url is None:
            return names
        status, payload = request("GET", url, {"Accept": "application/json"})
        if status == 404 and page == 1:
            return []
        if status != 200:
            raise ToolError(
                f"Docker Hub answered HTTP {status} for page {page} of {repo}"
            )
        try:
            data = json.loads(payload)
            names.extend(str(item["name"]) for item in data["results"])
            url = data.get("next")
        except (ValueError, KeyError, TypeError) as exc:
            raise ToolError(
                f"Unexpected Docker Hub response on page {page}: {exc}"
            ) from exc
        if url is not None and not str(url).startswith(f"{HUB}/"):
            raise ToolError("Docker Hub returned a next-page link outside its own host")
    raise ToolError(f"More than {MAX_PAGES} tag pages for {repo}; refusing to continue")


def login() -> str:
    """Return a Docker Hub JWT for the credentials in the environment."""
    username = os.environ.get("DOCKERHUB_USERNAME", "")
    token = os.environ.get("DOCKERHUB_TOKEN", "")
    if not username or not token:
        raise ToolError("DOCKERHUB_USERNAME and DOCKERHUB_TOKEN must be set")
    body = json.dumps({"username": username, "password": token}).encode()
    status, payload = request(
        "POST",
        f"{HUB}/v2/users/login",
        {"Content-Type": "application/json", "Accept": "application/json"},
        body,
    )
    if status != 200:
        raise ToolError(f"Docker Hub login answered HTTP {status}")
    try:
        jwt = json.loads(payload)["token"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ToolError("Docker Hub login returned no token") from exc
    if not isinstance(jwt, str) or not jwt:
        raise ToolError("Docker Hub login returned no token")
    return jwt


def tags_to_delete(version: str, arches: str, extra: list[str]) -> list[str]:
    """Build the delete list; only fragment and candidate tags are ever accepted."""
    wanted = [f"{version}-{arch}" for arch in arches.split(",") if arch]
    wanted += extra
    for tag in wanted:
        if not TAG_RE.match(tag):
            raise ToolError(f"'{tag}' is not a valid tag name")
        if not (is_fragment(tag) or is_candidate(tag)):
            raise ToolError(
                f"Refusing to delete '{tag}': not a fragment or candidate tag"
            )
    return list(dict.fromkeys(wanted))


def delete_tags(repo: str, tags: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Delete the tags; return (deleted, skipped, failed). A 404 counts as skipped."""
    deleted: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    jwt = login()
    for tag in tags:
        url = f"{HUB}/v2/repositories/{repo}/tags/{tag}/"
        try:
            status, _ = request("DELETE", url, {"Authorization": f"JWT {jwt}"})
        except ToolError as exc:
            log("error", str(exc))
            failed.append(tag)
            continue
        if status in (200, 202, 204):
            deleted.append(tag)
        elif status == 404:
            skipped.append(tag)
        else:
            log("error", f"Deleting {tag} answered HTTP {status}")
            failed.append(tag)
    return deleted, skipped, failed


def emit(values: dict[str, str]) -> None:
    for key, value in values.items():
        print(f"{key}={value}")


def run_check(args: argparse.Namespace) -> int:
    emit(compare_with_published(args.version, list_tags(args.repo)))
    return 0


def run_latest(args: argparse.Namespace) -> int:
    tags = list_tags(args.repo)
    emit({"version": newest_stable(tags), "published_tags": str(len(tags))})
    return 0


def run_delete(args: argparse.Namespace) -> int:
    tags = tags_to_delete(args.version, args.arches, args.tag)
    try:
        deleted, skipped, failed = delete_tags(args.repo, tags)
    except ToolError as exc:
        if not args.warn:
            raise
        log("warning", f"Tag cleanup skipped: {exc}")
        deleted, skipped, failed = [], [], tags
    emit(
        {
            "deleted": ",".join(deleted),
            "skipped": ",".join(skipped),
            "failed": ",".join(failed),
        }
    )
    if failed and not args.warn:
        return 1
    if failed:
        log("warning", f"Could not delete: {', '.join(failed)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").strip().splitlines()[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, helptext in (
        ("check", "Print whether VERSION is the newest published release."),
        ("delete", "Delete fragment and candidate tags of VERSION."),
    ):
        cmd = sub.add_parser(name, help=helptext)
        cmd.add_argument(
            "--version", required=True, help="Release version, e.g. 1.6.2-rc.1"
        )
        cmd.add_argument("--repo", default=DEFAULT_REPO, help="Docker Hub repository")
    latest = sub.add_parser("latest", help="Print the highest stable release tag.")
    latest.add_argument("--repo", default=DEFAULT_REPO, help="Docker Hub repository")
    delete = sub.choices["delete"]
    delete.add_argument(
        "--arches", default=DEFAULT_ARCHES, help="Comma-separated architectures"
    )
    delete.add_argument(
        "--tag", action="append", default=[], help="Extra candidate tag"
    )
    delete.add_argument(
        "--warn", action="store_true", help="Report errors as warnings, exit 0"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    print_header(SCRIPT, (__doc__ or "").strip().splitlines()[0], __author__, HELP_URL)
    args = build_parser().parse_args(argv)
    try:
        version = getattr(args, "version", None)
        if version is not None and not VERSION_RE.match(version):
            raise ToolError(f"'{version}' is not a release version")
        if not REPO_RE.match(args.repo):
            raise ToolError(f"'{args.repo}' is not a Docker Hub repository name")
        handlers = {"check": run_check, "latest": run_latest, "delete": run_delete}
        return handlers[args.command](args)
    except ToolError as exc:
        log("error", str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
