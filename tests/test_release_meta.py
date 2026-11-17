#!/usr/bin/env python3
#
# tests/test_release_meta.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Build plan and metadata checks of tools/release_meta.py."""

import json
import re
from pathlib import Path

import ci_output as co
import pytest
import release_meta as rm


def test_tag_push_publishes_and_releases_both_platforms():
    result = rm.plan("", "", "")
    assert (result["publish"], result["release"]) == ("true", "true")
    assert result["enforce_trivy_gate"] == "true"
    assert len(json.loads(result["matrix"])["include"]) == 2


def test_single_platform_dispatch_publishes_without_release():
    result = rm.plan("true", "linux/arm64", "")
    assert (result["publish"], result["release"]) == ("true", "false")
    assert json.loads(result["matrix"])["include"][0]["arch"] == "arm64"


def test_unpublished_run_enforces_gate_only_on_request():
    assert rm.plan("false", "", "")["enforce_trivy_gate"] == "false"
    assert rm.plan("false", "", "true")["enforce_trivy_gate"] == "true"
    assert rm.plan("false", "", "")["release"] == "false"


def test_unknown_platform_fails():
    with pytest.raises(rm.MetaError):
        rm.plan("true", "linux/riscv64", "")


def test_mixed_known_and_unknown_platforms_fail_completely():
    with pytest.raises(rm.MetaError, match="linux/unknown"):
        rm.plan("true", "linux/amd64,linux/unknown", "")


def test_tag_must_match_version():
    rm.check_tag("1.6.2", "tag", "v1.6.2")
    rm.check_tag("1.6.2", "branch", "feature/x")
    with pytest.raises(rm.MetaError):
        rm.check_tag("1.6.2", "tag", "v1.6.3")


def test_existing_release_tag_must_point_at_build_commit(monkeypatch):
    refs = "aaa\trefs/tags/v1.6.2\nbbb\trefs/tags/v1.6.2^{}\n"
    monkeypatch.setattr(rm, "git", lambda *a: refs)
    assert rm.remote_tag_commit("v1.6.2") == "bbb"
    rm.check_release_tag("1.6.2", "bbb")
    with pytest.raises(rm.MetaError):
        rm.check_release_tag("1.6.2", "ccc")
    monkeypatch.setattr(rm, "git", lambda *a: "")
    rm.check_release_tag("1.6.2", "ccc")


def write_pyproject(tmp_path, version):
    path = tmp_path / "pyproject.toml"
    path.write_text(f'[project]\nname = "x"\nversion = "{version}"\n')
    return path


def test_ancestry_is_checked_for_every_publishing_run(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(rm, "git", lambda *a: calls.append(a[0]) or "")
    env = {"PUBLISH": "true", "RELEASE": "false", "GITHUB_SHA": "abc"}
    values = rm.metadata(env, write_pyproject(tmp_path, "1.6.2"))
    assert "merge-base" in calls
    assert values["version"] == "1.6.2"
    calls.clear()
    rm.metadata(
        {"PUBLISH": "false", "RELEASE": "false"}, write_pyproject(tmp_path, "1.6.2")
    )
    assert calls == []


def test_ancestry_failure_names_the_ref(monkeypatch, tmp_path):
    def fake_git(*args):
        if args[0] == "merge-base":
            raise rm.MetaError("not an ancestor")
        return ""

    monkeypatch.setattr(rm, "git", fake_git)
    env = {"PUBLISH": "true", "GITHUB_SHA": "abc", "GITHUB_REF_NAME": "feature/x"}
    with pytest.raises(rm.MetaError, match="not contained in main"):
        rm.metadata(env, write_pyproject(tmp_path, "1.6.2"))


def test_bad_version_is_refused(tmp_path):
    with pytest.raises(rm.MetaError):
        rm.read_version(write_pyproject(tmp_path, "1.6"))


def test_outputs_refuse_multiline_values(monkeypatch, tmp_path):
    target = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(target))
    co.write_outputs({"a": "1"})
    assert target.read_text() == "a=1\n"
    with pytest.raises(ValueError):
        co.write_outputs({"a": "1\nb=2"})


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
TRIVY_USES = re.compile(r"^\s+uses:\s*(aquasecurity/trivy-action@\S+)", re.MULTILINE)
TRIVY_POLICY_KEYS = ("severity", "ignore-unfixed", "vuln-type", "scanners")


def trivy_image_scan(workflow: str) -> dict[str, str]:
    """Return the action reference and policy inputs of the image scan step."""
    text = (WORKFLOWS / workflow).read_text()
    steps = re.split(r"^\s+- name: ", text, flags=re.MULTILINE)
    scans = [s for s in steps if "aquasecurity/trivy-action@" in s and "image-ref:" in s]
    assert len(scans) == 1, f"{workflow}: expected one Trivy image scan step"
    step = scans[0]
    found = {"action": TRIVY_USES.search(step).group(1)}
    for key in TRIVY_POLICY_KEYS:
        match = re.search(rf"^\s+{key}:\s*(.+?)\s*$", step, re.MULTILINE)
        assert match, f"{workflow}: missing Trivy input {key}"
        found[key] = match.group(1).strip("'\"")
    return found


def test_trivy_image_policy_is_identical_in_release_and_rescan():
    release = trivy_image_scan("docker-build.yml")
    rescan = trivy_image_scan("security.yml")
    assert release == rescan
    assert release["severity"] == "CRITICAL,HIGH"
    assert release["ignore-unfixed"] == "true"
    assert release["scanners"] == "vuln"
    assert release["vuln-type"] == "os,library"


def test_docs_rebuild_follows_only_a_successful_tag_push_release():
    text = (WORKFLOWS / "docs-build.yml").read_text()
    conditions = re.findall(r"if: >-\n((?:\s{6}.*\n)+)", text)
    gated = [c for c in conditions if "workflow_run.conclusion" in c]
    assert len(gated) == 2
    for condition in gated:
        assert "github.event.workflow_run.event == 'push'" in condition
        assert "startsWith(github.event.workflow_run.head_branch, 'v')" in condition
