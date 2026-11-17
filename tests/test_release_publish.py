#!/usr/bin/env python3
#
# tests/test_release_publish.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Index verification and promotion rules of tools/release_publish.py."""

import json

import pytest
import release_publish as rp

A = "sha256:" + "a" * 64
B = "sha256:" + "b" * 64
MATRIX = json.dumps(
    {
        "include": [
            {"arch": "amd64", "platform": "linux/amd64"},
            {"arch": "arm64", "platform": "linux/arm64"},
        ]
    }
)
INDEX = {
    "manifests": [
        {"platform": {"os": "linux", "architecture": "amd64"}},
        {"platform": {"os": "linux", "architecture": "arm64"}},
        {"platform": {"os": "unknown", "architecture": "unknown"}},
    ]
}


class FakeRegistry:
    """Maps references to digests, serves its index for raw inspects and records created tags."""

    def __init__(self, digests, index=INDEX):
        self.digests = dict(digests)
        self.index = index
        self.created = []

    def docker(self, *args):
        if args[:3] == ("buildx", "imagetools", "inspect"):
            if "--raw" in args:
                return json.dumps(self.index)
            ref = args[3]
            if ref not in self.digests:
                raise rp.NotFoundError("not found")
            return self.digests[ref] + "\n"
        if args[:3] == ("buildx", "imagetools", "create"):
            tag = args[args.index("--tag") + 1]
            digest = args[-1].split("@")[1]
            self.created.append(tag)
            self.digests[tag] = digest
            return ""
        raise AssertionError(args)


# A successful main() appends to GITHUB_OUTPUT; inside CI that would write into the real job outputs.
@pytest.fixture(autouse=True)
def _no_job_outputs(monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setenv("MATRIX", MATRIX)

    def install(digests, tags, index=INDEX):
        fake = FakeRegistry(digests, index)
        monkeypatch.setattr(rp, "docker", fake.docker)
        monkeypatch.setattr(rp, "list_tags", lambda repo: tags)
        return fake

    return install


def promote(version="1.6.2"):
    args = rp.build_parser().parse_args(
        ["promote", "--image", "o/r", "--version", version,
         "--candidate", "candidate-x-1-1", "--digest", A]
    )  # fmt: skip
    return rp.run_promote(args)


def test_platforms_ignore_attestation_entries():
    assert rp.platforms_of(INDEX) == ["linux/amd64", "linux/arm64"]


def test_missing_platform_fails():
    entries = rp.matrix_entries(MATRIX)
    rp.require_platforms(INDEX, entries)
    with pytest.raises(rp.ToolError):
        rp.require_platforms({"manifests": INDEX["manifests"][:1]}, entries)


def test_stable_newest_moves_latest(registry):
    fake = registry({"o/r:candidate-x-1-1": A}, ["1.6.1", "1.6.2-rc.1"])
    result = promote()
    assert result == {"version_tag_added": "true", "latest_moved": "true"}
    assert fake.created == ["o/r:1.6.2", "o/r:latest"]


def test_older_release_keeps_latest(registry):
    fake = registry({"o/r:candidate-x-1-1": A}, ["1.7.0"])
    assert promote("1.6.2")["latest_moved"] == "false"
    assert "o/r:latest" not in fake.created


def test_prerelease_never_moves_latest(registry):
    registry({"o/r:candidate-x-1-1": A}, ["1.6.1"])
    assert promote("1.7.0-rc.1")["latest_moved"] == "false"


def test_existing_version_tag_with_same_digest_is_accepted(registry):
    fake = registry({"o/r:candidate-x-1-1": A, "o/r:1.6.2": A}, ["1.6.2"])
    assert promote()["version_tag_added"] == "false"
    assert "o/r:1.6.2" not in fake.created


def test_existing_version_tag_with_other_digest_fails(registry):
    registry({"o/r:candidate-x-1-1": A, "o/r:1.6.2": B}, ["1.6.2"])
    with pytest.raises(rp.ToolError, match="already exists"):
        promote()


def test_transient_inspect_error_never_creates_the_version_tag(registry, monkeypatch):
    fake = registry({"o/r:candidate-x-1-1": A}, [])
    original = fake.docker

    def flaky(*args):
        if args[:3] == ("buildx", "imagetools", "inspect") and args[3] == "o/r:1.6.2":
            raise rp.ToolError("docker buildx imagetools failed: i/o timeout")
        return original(*args)

    monkeypatch.setattr(rp, "docker", flaky)
    with pytest.raises(rp.ToolError, match="i/o timeout"):
        promote()
    assert fake.created == []


def test_tag_listed_after_the_first_listing_is_not_overwritten(registry, monkeypatch):
    fake = registry({"o/r:candidate-x-1-1": A}, [])
    listings = iter([[], ["1.6.2"]])
    monkeypatch.setattr(rp, "list_tags", lambda repo: next(listings))
    with pytest.raises(rp.ToolError, match="cannot be resolved"):
        promote()
    assert fake.created == []


def test_candidate_digest_change_fails(registry):
    registry({"o/r:candidate-x-1-1": B}, [])
    with pytest.raises(rp.ToolError, match="no longer resolves"):
        promote()


def test_candidate_with_missing_platform_fails(registry):
    registry({"o/r:candidate-x-1-1": A}, [], {"manifests": INDEX["manifests"][:1]})
    with pytest.raises(rp.ToolError, match="index describes"):
        promote()


def test_promote_refuses_a_release_tag_as_candidate():
    args = rp.build_parser().parse_args(
        ["promote", "--image", "o/r", "--version", "1.6.2",
         "--candidate", "latest", "--digest", A]
    )  # fmt: skip
    with pytest.raises(rp.ToolError, match="not a candidate"):
        rp.run_promote(args)


def test_assemble_needs_every_digest(tmp_path, registry):
    registry({}, [])
    (tmp_path / "amd64").write_text(A)
    args = rp.build_parser().parse_args(
        ["assemble", "--image", "o/r", "--digests-dir", str(tmp_path),
         "--work-dir", str(tmp_path / "w")]
    )  # fmt: skip
    with pytest.raises(rp.ToolError, match="no valid image digest"):
        rp.run_assemble(args)


def resolve_argv(platforms="linux/amd64 linux/arm64"):
    return [
        "resolve",
        "--image",
        "giiibates/fritzfluxdb",
        "--version",
        "1.6.1",
        "--platforms",
        platforms,
    ]


def test_resolve_prints_index_and_child_digests(monkeypatch, capsys):
    index = {
        "manifests": [
            {"digest": A, "platform": {"os": "linux", "architecture": "amd64"}},
            {"digest": B, "platform": {"os": "linux", "architecture": "arm64"}},
            {
                "digest": "sha256:" + "c" * 64,
                "platform": {"os": "unknown", "architecture": "unknown"},
            },
        ]
    }
    fake = FakeRegistry({"giiibates/fritzfluxdb:1.6.1": B}, index)
    monkeypatch.setattr(rp, "docker", fake.docker)
    # The fake answers every --raw inspect with its index, so the read by digest needs no entry.
    assert rp.main(resolve_argv()) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "version=1.6.1"
    assert out[1] == f"index_digest={B}"
    matrix = json.loads(out[2].removeprefix("matrix="))
    assert matrix == {
        "include": [
            {"arch": "amd64", "digest": A},
            {"arch": "arm64", "digest": B},
        ]
    }


def test_resolve_fails_on_platform_difference(monkeypatch, capsys):
    fake = FakeRegistry({"giiibates/fritzfluxdb:1.6.1": B})
    monkeypatch.setattr(rp, "docker", fake.docker)
    assert rp.main(resolve_argv("linux/amd64 linux/arm64 linux/arm/v7")) == 1
    assert rp.main(resolve_argv("")) == 1
    assert capsys.readouterr().out == ""


def test_resolve_fails_when_the_version_is_not_published(monkeypatch, capsys):
    fake = FakeRegistry({})
    monkeypatch.setattr(rp, "docker", fake.docker)
    assert rp.main(resolve_argv()) == 1
    assert capsys.readouterr().out == ""


def test_resolve_rejects_a_child_without_digest(monkeypatch):
    index = {
        "manifests": [
            {"platform": {"os": "linux", "architecture": "amd64"}},
            {"platform": {"os": "linux", "architecture": "arm64"}},
        ]
    }
    fake = FakeRegistry({"giiibates/fritzfluxdb:1.6.1": B}, index)
    monkeypatch.setattr(rp, "docker", fake.docker)
    assert rp.main(resolve_argv()) == 1
