#!/usr/bin/env python3
#
# tools/release_publish.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Assemble, verify and promote the multi-arch release index on Docker Hub.
`digest` records a pushed image digest, `assemble` builds and checks the index from
digests, `candidate` publishes it under a candidate tag and reads it back, and
`promote` adds the version tag and moves latest from the verified digest;
`resolve` reads a published version back into its index and per-platform digests.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from ci_output import REPO_URL, log, print_header, write_outputs
from release_newest import (
    REPO_RE,
    VERSION_RE,
    ToolError,
    compare_with_published,
    is_candidate,
    list_tags,
)

__author__ = "Gill-Bates"
SCRIPT = Path(__file__).resolve()
HELP_URL = f"{REPO_URL}/blob/main/tools/{SCRIPT.name}"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ARCH_RE = re.compile(r"^[a-z0-9]+$")
DOCKER_TIMEOUT_SECONDS = 300


class NotFoundError(ToolError):
    """The registry reports that a reference does not exist."""


def docker(*args: str) -> str:
    """Run docker and return stdout; a failure is a ToolError with docker's message."""
    try:
        result = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=DOCKER_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolError(f"docker {args[0]} failed: {exc}") from exc
    if result.returncode != 0:
        message = f"docker {' '.join(args[:3])} failed: {result.stderr.strip()}"
        if "not found" in result.stderr.lower():
            raise NotFoundError(message)
        raise ToolError(message)
    return result.stdout


def image_digest(ref: str) -> str:
    """Return the digest the registry reports for a reference."""
    digest = docker(
        "buildx", "imagetools", "inspect", ref, "--format", "{{.Manifest.Digest}}"
    ).strip()
    if not DIGEST_RE.match(digest):
        raise ToolError(f"unexpected digest for {ref}: '{digest}'")
    return digest


def existing_digest(ref: str) -> str | None:
    """Return the digest of a reference, or None only when the registry says it is absent."""
    try:
        return image_digest(ref)
    except NotFoundError:
        return None


def read_index(ref: str) -> dict[str, Any]:
    """Read the raw index document of a reference from the registry."""
    raw = docker("buildx", "imagetools", "inspect", "--raw", ref)
    try:
        index = json.loads(raw)
    except ValueError as exc:
        raise ToolError(f"{ref} did not return a JSON document: {exc}") from exc
    if not isinstance(index, dict):
        raise ToolError(f"{ref} did not return an index object")
    return index


def matrix_entries(matrix_json: str) -> list[dict[str, str]]:
    try:
        entries = json.loads(matrix_json)["include"]
        if not entries:
            raise ValueError("empty matrix")
        return [
            {"arch": str(e["arch"]), "platform": str(e["platform"])} for e in entries
        ]
    except (ValueError, KeyError, TypeError) as exc:
        raise ToolError(f"MATRIX is not a valid build matrix: {exc}") from exc


def _platform_manifests(index: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """Return (arch, os/arch, manifest) per platform child; attestation entries (unknown) are skipped."""
    children = []
    for manifest in index.get("manifests") or []:
        spec = manifest.get("platform") or {}
        arch = spec.get("architecture")
        if arch and arch != "unknown":
            children.append((arch, f"{spec.get('os')}/{arch}", manifest))
    return children


def platforms_of(index: dict[str, Any]) -> list[str]:
    """List os/arch of an index; attestation entries (unknown/unknown) are ignored."""
    return sorted(name for _, name, _ in _platform_manifests(index))


def require_platforms(index: dict[str, Any], entries: list[dict[str, str]]) -> None:
    expected = sorted(e["platform"] for e in entries)
    actual = platforms_of(index)
    if expected != actual:
        raise ToolError(
            f"index describes {actual or 'no platform'}, expected {expected}"
        )


def child_digests(index: dict[str, Any]) -> list[dict[str, str]]:
    """Return arch, platform and digest of every real platform child of an index."""
    children = []
    for arch, name, manifest in _platform_manifests(index):
        digest = str(manifest.get("digest", ""))
        if not DIGEST_RE.match(digest):
            raise ToolError(f"child {name} has no valid digest")
        children.append({"arch": arch, "platform": name, "digest": digest})
    return children


def run_resolve(args: argparse.Namespace) -> dict[str, str]:
    """Resolve a published version; its platforms must equal the expected list."""
    ref = f"{args.image}:{args.version}"
    digest = image_digest(ref)
    index = read_index(f"{args.image}@{digest}")
    expected = sorted(args.platforms.split())
    actual = platforms_of(index)
    if not expected or expected != actual:
        raise ToolError(
            f"{ref} describes {actual or 'no platform'}, expected {expected}"
        )
    children = child_digests(index)
    log("info", f"{ref} -> {digest}")
    for child in children:
        log("info", f"  {child['platform']} -> {child['digest']}")
    matrix = [{"arch": c["arch"], "digest": c["digest"]} for c in children]
    return {
        "version": args.version,
        "index_digest": digest,
        "matrix": json.dumps({"include": matrix}, separators=(",", ":")),
    }


def run_digest(args: argparse.Namespace) -> dict[str, str]:
    digest = image_digest(args.ref)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(digest, encoding="utf-8")
    log("info", f"{args.ref} -> {digest}")
    return {}


def run_assemble(args: argparse.Namespace) -> dict[str, str]:
    entries = matrix_entries(os.environ.get("MATRIX", ""))
    sources = []
    for entry in entries:
        if not ARCH_RE.match(entry["arch"]):
            raise ToolError(f"'{entry['arch']}' is not an architecture name")
        file = Path(args.digests_dir) / entry["arch"]
        digest = file.read_text(encoding="utf-8").strip() if file.is_file() else ""
        if not DIGEST_RE.match(digest):
            raise ToolError(f"no valid image digest for {entry['platform']}")
        sources.append(f"{args.image}@{digest}")
        log("info", f"  {entry['platform']}  {digest}")
    # The index is built with --dry-run first, so nothing public changes before the check.
    index = json.loads(docker("buildx", "imagetools", "create", "--dry-run", *sources))
    require_platforms(index, entries)
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    (work / "sources.txt").write_text("\n".join(sources) + "\n", encoding="utf-8")
    (work / "index.json").write_text(json.dumps(index), encoding="utf-8")
    log("info", f"OK: index describes {' '.join(platforms_of(index))}")
    return {}


def run_candidate(args: argparse.Namespace) -> dict[str, str]:
    if not is_candidate(args.candidate):
        raise ToolError(f"'{args.candidate}' is not a candidate tag")
    entries = matrix_entries(os.environ.get("MATRIX", ""))
    work = Path(args.work_dir)
    sources = work.joinpath("sources.txt").read_text(encoding="utf-8").split()
    verified = json.loads(work.joinpath("index.json").read_text(encoding="utf-8"))
    ref = f"{args.image}:{args.candidate}"
    docker("buildx", "imagetools", "create", "--tag", ref, *sources)
    # The registry's copy is what gets verified, not this job's own output.
    published = read_index(ref)
    if published != verified:
        raise ToolError(f"{ref} differs from the verified index")
    require_platforms(published, entries)
    digest = image_digest(ref)
    log("info", f"Candidate {ref} -> {digest}")
    return {"candidate": args.candidate, "index_digest": digest}


def ensure_tag(image: str, tag: str, digest: str, tags: list[str]) -> bool:
    """Point a tag at the digest; an existing tag must already carry it. True if added."""
    ref = f"{image}:{tag}"
    current = existing_digest(ref)
    if current is None and (tag in tags or tag in list_tags(image)):
        raise ToolError(f"{ref} is listed in the registry but cannot be resolved")
    if current is not None:
        if current != digest:
            raise ToolError(f"{ref} already exists with {current}, expected {digest}")
        log("info", f"{ref} already carries {digest}")
        return False
    push_tag(image, tag, digest)
    return True


def push_tag(image: str, tag: str, digest: str) -> None:
    """Point a tag at the digest and read it back from the registry."""
    ref = f"{image}:{tag}"
    docker("buildx", "imagetools", "create", "--tag", ref, f"{image}@{digest}")
    if image_digest(ref) != digest:
        raise ToolError(f"{ref} does not resolve to {digest} after the push")
    log("info", f"Pushed {ref} -> {digest}")


def run_promote(args: argparse.Namespace) -> dict[str, str]:
    if not DIGEST_RE.match(args.digest):
        raise ToolError(f"'{args.digest}' is not a digest")
    if not is_candidate(args.candidate):
        raise ToolError(f"'{args.candidate}' is not a candidate tag")
    entries = matrix_entries(os.environ.get("MATRIX", ""))
    candidate_ref = f"{args.image}:{args.candidate}"
    # Everything below is read from the registry again; earlier job outputs are hints.
    if image_digest(candidate_ref) != args.digest:
        raise ToolError(f"{candidate_ref} no longer resolves to {args.digest}")
    require_platforms(read_index(candidate_ref), entries)
    tags = list_tags(args.image)
    outcome = compare_with_published(args.version, tags)
    added = ensure_tag(args.image, args.version, args.digest, tags)
    moved = False
    if outcome["is_newest_stable"] == "true":
        move_latest(args.image, args.digest)
        moved = True
    else:
        log(
            "info",
            f"Skipping 'latest' for {args.version}: not the newest stable release",
        )
    return {"version_tag_added": str(added).lower(), "latest_moved": str(moved).lower()}


def move_latest(image: str, digest: str) -> None:
    """Move latest to the digest; unlike a version tag it is meant to move."""
    push_tag(image, "latest", digest)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").strip().splitlines()[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    digest = sub.add_parser(
        "digest", help="Write the registry digest of REF to a file."
    )
    digest.add_argument("--ref", required=True)
    digest.add_argument("--out", required=True)
    assemble = sub.add_parser("assemble", help="Build and check the index (dry run).")
    assemble.add_argument("--image", required=True)
    assemble.add_argument("--digests-dir", required=True)
    assemble.add_argument("--work-dir", required=True)
    candidate = sub.add_parser(
        "candidate", help="Publish the index as a candidate tag."
    )
    candidate.add_argument("--image", required=True)
    candidate.add_argument("--candidate", required=True)
    candidate.add_argument("--work-dir", required=True)
    promote = sub.add_parser("promote", help="Add the version tag and move latest.")
    promote.add_argument("--image", required=True)
    promote.add_argument("--version", required=True)
    promote.add_argument("--candidate", required=True)
    promote.add_argument("--digest", required=True)
    resolve = sub.add_parser(
        "resolve", help="Resolve a published version to its platform digests."
    )
    resolve.add_argument("--image", required=True)
    resolve.add_argument("--version", required=True)
    resolve.add_argument(
        "--platforms", required=True, help="Space-separated os/arch list to expect"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    print_header(SCRIPT, (__doc__ or "").strip().splitlines()[0], __author__, HELP_URL)
    args = build_parser().parse_args(argv)
    handlers = {
        "digest": run_digest,
        "assemble": run_assemble,
        "candidate": run_candidate,
        "promote": run_promote,
        "resolve": run_resolve,
    }
    try:
        if platform.system() != "Linux":
            raise ToolError("this script needs a Linux runner with docker")
        image = getattr(args, "image", None)
        if image is not None and not REPO_RE.match(image):
            raise ToolError(f"'{image}' is not a Docker Hub repository name")
        if getattr(args, "version", None) and not VERSION_RE.match(args.version):
            raise ToolError(f"'{args.version}' is not a release version")
        write_outputs(handlers[args.command](args))
    except (ToolError, OSError, ValueError) as exc:
        log("error", str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
