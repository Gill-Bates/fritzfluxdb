#!/usr/bin/env python3
#
# tests/test_release_newest.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Version comparison, Docker Hub paging and tag cleanup of tools/release_newest.py."""

import json

import check_manifest as cm
import pytest
import release_newest as rn


@pytest.mark.parametrize(
    ("lower", "higher"),
    [
        ("1.9.0", "1.10.0"),
        ("1.6.2-rc.1", "1.6.2"),
        ("1.6.2-rc.1", "1.6.2-rc.2"),
        ("1.6.2-rc.2", "1.6.2-rc.10"),
        ("1.6.2-alpha", "1.6.2-beta"),
        ("1.6.2-rc.1", "1.6.2-rc.1.1"),
        ("1.6.2-1", "1.6.2-rc"),
        ("1.6.2", "1.6.3-rc.1"),
    ],
)
def test_version_order(lower, higher):
    assert rn.parse_version(lower) < rn.parse_version(higher)


@pytest.mark.parametrize(
    "tag",
    [
        "latest",
        "1.6.2-amd64",
        "1.6.2-rc.1-arm64",
        "candidate-1.6.2",
        "1.6",
        "v1.6.2",
        "main",
    ],
)
def test_other_tags_are_not_versions(tag):
    assert rn.parse_version(tag) is None


def test_newest_stable_release():
    tags = ["1.5.0", "1.6.0-rc.1", "latest", "1.6.1-amd64", "candidate-x"]
    out = rn.compare_with_published("1.6.0", tags)
    assert out["is_newest"] == "true"
    assert out["is_newest_stable"] == "true"
    assert out["is_prerelease"] == "false"
    assert out["latest_published"] == "1.6.0-rc.1"
    assert out["published_versions"] == "2"


def test_prerelease_is_lower_than_its_own_release():
    out = rn.compare_with_published("1.6.2-rc.1", ["1.6.2", "1.6.1"])
    assert out["is_newest"] == "false"
    assert out["is_prerelease"] == "true"
    assert out["latest_published"] == "1.6.2"


def test_older_release_is_not_newest():
    out = rn.compare_with_published("1.5.1", ["1.6.0", "1.5.0"])
    assert out["is_newest"] == "false"
    assert out["is_newest_stable"] == "false"


def test_newer_prerelease_does_not_block_stable():
    out = rn.compare_with_published("1.6.2", ["2.0.0-rc.1", "1.6.1"])
    assert out["is_newest"] == "false"
    assert out["is_newest_stable"] == "true"


def test_republishing_the_same_version_ignores_itself():
    out = rn.compare_with_published("1.6.2", ["1.6.2", "1.6.1"])
    assert out["is_newest"] == "true"


def test_no_tags_means_newest():
    out = rn.compare_with_published("1.0.0", [])
    assert out["is_newest"] == "true"
    assert out["latest_published"] == ""


def test_fragment_tag_is_no_release_version():
    with pytest.raises(rn.ToolError):
        rn.compare_with_published("1.6.2-amd64", [])


def page(names, next_url=None):
    body = {"results": [{"name": n} for n in names], "next": next_url}
    return 200, json.dumps(body).encode()


def test_list_tags_follows_pagination(monkeypatch):
    second = f"{rn.HUB}/v2/repositories/o/r/tags?page=2"
    answers = {1: page(["1.0.0"], second), 2: page(["1.1.0"])}
    calls = []

    def fake(method, url, headers=None, body=None):
        calls.append(url)
        return answers[len(calls)]

    monkeypatch.setattr(rn, "request", fake)
    assert rn.list_tags("o/r") == ["1.0.0", "1.1.0"]
    assert calls[1] == second


def test_first_page_404_means_no_tags(monkeypatch):
    monkeypatch.setattr(rn, "request", lambda *a, **k: (404, b"{}"))
    assert rn.list_tags("o/r") == []


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_other_status_on_first_page_fails(monkeypatch, status):
    monkeypatch.setattr(rn, "request", lambda *a, **k: (status, b"{}"))
    with pytest.raises(rn.ToolError, match=str(status)):
        rn.list_tags("o/r")


def test_404_on_later_page_fails(monkeypatch):
    second = f"{rn.HUB}/v2/repositories/o/r/tags?page=2"
    answers = iter([page(["1.0.0"], second), (404, b"{}")])
    monkeypatch.setattr(rn, "request", lambda *a, **k: next(answers))
    with pytest.raises(rn.ToolError, match="404"):
        rn.list_tags("o/r")


def test_foreign_next_link_is_refused(monkeypatch):
    answers = iter([page(["1.0.0"], "https://evil.example/next")])
    monkeypatch.setattr(rn, "request", lambda *a, **k: next(answers))
    with pytest.raises(rn.ToolError, match="outside"):
        rn.list_tags("o/r")


def test_malformed_page_fails(monkeypatch):
    monkeypatch.setattr(rn, "request", lambda *a, **k: (200, b'{"oops": 1}'))
    with pytest.raises(rn.ToolError, match="Unexpected"):
        rn.list_tags("o/r")


def test_request_refuses_foreign_host():
    with pytest.raises(rn.ToolError):
        rn.request("GET", "https://example.com/x")


def test_delete_list_accepts_only_fragment_and_candidate_tags():
    assert rn.tags_to_delete("1.6.2", "amd64,arm64", ["candidate-1.6.2"]) == [
        "1.6.2-amd64",
        "1.6.2-arm64",
        "candidate-1.6.2",
    ]
    for bad in ("1.6.2", "latest", "1.6.2-rc.1", "bad tag"):
        with pytest.raises(rn.ToolError):
            rn.tags_to_delete("1.6.2", "", [bad])


def fake_hub(monkeypatch, statuses):
    seen = []

    def fake(method, url, headers=None, body=None):
        seen.append((method, url))
        if url.endswith("/users/login"):
            return 200, b'{"token": "jwt"}'
        return statuses[url.rsplit("/", 2)[-2]], b""

    monkeypatch.setattr(rn, "request", fake)
    monkeypatch.setenv("DOCKERHUB_USERNAME", "user")
    monkeypatch.setenv("DOCKERHUB_TOKEN", "secret")
    return seen


def test_delete_counts_404_as_skipped(monkeypatch):
    fake_hub(monkeypatch, {"1.6.2-amd64": 204, "1.6.2-arm64": 404})
    deleted, skipped, failed = rn.delete_tags("o/r", ["1.6.2-amd64", "1.6.2-arm64"])
    assert (deleted, skipped, failed) == (["1.6.2-amd64"], ["1.6.2-arm64"], [])


def test_delete_failure_exit_codes(monkeypatch, capsys):
    fake_hub(monkeypatch, {"1.6.2-amd64": 500, "1.6.2-arm64": 204})
    argv = ["delete", "--version", "1.6.2"]
    assert rn.main(argv) == 1
    assert "failed=1.6.2-amd64" in capsys.readouterr().out
    assert rn.main([*argv, "--warn"]) == 0


def test_delete_without_credentials(monkeypatch, capsys):
    monkeypatch.delenv("DOCKERHUB_USERNAME", raising=False)
    monkeypatch.delenv("DOCKERHUB_TOKEN", raising=False)
    argv = ["delete", "--version", "1.6.2"]
    assert rn.main(argv) == 1
    assert rn.main([*argv, "--warn"]) == 0
    assert "failed=1.6.2-amd64,1.6.2-arm64" in capsys.readouterr().out


def test_check_prints_data_on_stdout_and_header_on_stderr(monkeypatch, capsys):
    monkeypatch.setattr(rn, "list_tags", lambda repo: ["1.5.0"])
    assert rn.main(["check", "--version", "1.6.0"]) == 0
    captured = capsys.readouterr()
    assert captured.err.splitlines()[0] == "Script: release_newest.py"
    assert captured.out.splitlines()[:3] == [
        "version=1.6.0",
        "is_prerelease=false",
        "is_newest=true",
    ]
    assert all("=" in line for line in captured.out.splitlines())


def test_check_rejects_bad_input(capsys):
    assert rn.main(["check", "--version", "latest"]) == 1
    assert rn.main(["check", "--version", "1.0.0", "--repo", "../x"]) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        (["1.9.0", "1.10.0", "1.10.1-rc.1", "latest"], "1.10.0"),
        (["1.6.2", "1.6.2-amd64", "candidate-1.7.0-1-1", "1.7.0-rc.1"], "1.6.2"),
    ],
)
def test_newest_stable_skips_prereleases_and_build_tags(tags, expected):
    assert rn.newest_stable(tags) == expected


@pytest.mark.parametrize("tags", [[], ["latest", "1.6.2-rc.1", "1.6.2-amd64"]])
def test_newest_stable_fails_without_a_stable_tag(tags):
    with pytest.raises(rn.ToolError):
        rn.newest_stable(tags)


def test_latest_prints_data_and_fails_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(rn, "list_tags", lambda repo: ["1.5.0", "1.6.0-rc.1"])
    assert rn.main(["latest"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "version=1.5.0",
        "published_tags=2",
    ]
    monkeypatch.setattr(rn, "list_tags", lambda repo: ["1.6.0-rc.1"])
    assert rn.main(["latest"]) == 1
    assert capsys.readouterr().out == ""
    assert rn.main(["latest", "--repo", "../x"]) == 1


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("1.6.2-amd64", True),
        ("1.7.0-rc1-arm64", True),
        ("latest-amd64", False),
        ("foo-386", False),
        ("1.7.0-rc.1", False),
    ],
)
def test_fragment_needs_a_version_before_the_architecture(tag, expected):
    assert rn.is_fragment(tag) is expected


def test_fragment_never_ranks_but_a_plain_prerelease_does():
    assert rn.parse_version("1.7.0-rc1-386") is None
    assert rn.parse_version("1.7.0-rc.1") is not None


@pytest.mark.parametrize(
    "version",
    ["1.6.2", "1.6.2-rc.1", "1.6.2-1", "1.6", "v1.6.2", "1.6.2-", "1.6.2-rc..1", "1.6.2+b", "١.٦.٢"],
)
def test_every_version_check_accepts_the_same_versions(version):
    expected = bool(rn.VERSION_RE.match(version))
    assert bool(cm.VERSION_RE.fullmatch(version)) is expected
