#!/usr/bin/env python3
#
# tests/test_trivy_report.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Report validation, coverage proof and verdict of tools/trivy_report.py."""

import json

import pytest
import trivy_report as tr


def os_result(vulns=()):
    result = {
        "Target": "img (debian 13.7)",
        "Class": "os-pkgs",
        "Type": "debian",
        "Packages": [{"Name": "libc6", "Version": "2.41"}],
    }
    if vulns:
        result["Vulnerabilities"] = list(vulns)
    return result


def lang_result(vulns=()):
    result = {
        "Target": "Python",
        "Class": "lang-pkgs",
        "Type": "python-pkg",
        "Packages": [{"Name": "requests", "Version": "2.32.0"}],
    }
    if vulns:
        result["Vulnerabilities"] = list(vulns)
    return result


def vuln(severity="HIGH", vid="CVE-2026-0001", pkg="libc6", version="2.41"):
    return {
        "VulnerabilityID": vid,
        "PkgName": pkg,
        "InstalledVersion": version,
        "Severity": severity,
    }


def write(tmp_path, results=None, **top):
    report = {"SchemaVersion": 2, "ArtifactType": "container_image", **top}
    if results is not None:
        report["Results"] = results
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def run(path, *extra, classes=("os-pkgs", "lang-pkgs")):
    argv = ["status", str(path)]
    for cls in classes:
        argv += ["--expect-class", cls]
    return tr.main([*argv, *extra])


def test_clean_image_report(tmp_path, capsys):
    path = write(tmp_path, [os_result(), lang_result()])
    assert run(path) == 0
    captured = capsys.readouterr()
    assert captured.out == "✅ No HIGH/CRITICAL findings with a fix available\n"
    assert captured.err.splitlines()[0] == "Script: trivy_report.py"
    assert "Covered os-pkgs: img (debian 13.7), 1 packages" in captured.err


def test_findings_are_counted_but_do_not_fail_by_default(tmp_path, capsys):
    path = write(
        tmp_path,
        [
            os_result([vuln("CRITICAL"), vuln("HIGH"), vuln("MEDIUM")]),
            lang_result([vuln("HIGH", "CVE-2026-0002", "requests")]),
        ],
    )
    assert run(path) == 0
    assert capsys.readouterr().out == "⚠️ 1 CRITICAL, 2 HIGH with a fix available\n"
    assert run(path, "--fail-on-findings") == 1


def test_verdict_file_and_summary_block(tmp_path, capsys):
    path = write(tmp_path, [os_result([vuln()]), lang_result()])
    verdict = tmp_path / "x.status"
    assert (
        run(
            path,
            "--verdict-file",
            str(verdict),
            "--summary-title",
            "Trivy (amd64)",
            "--list",
            "5",
        )
        == 0
    )
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "### Trivy (amd64)"
    assert out[1] == "⚠️ 0 CRITICAL, 1 HIGH with a fix available"
    assert "```" in out
    assert "HIGH\tCVE-2026-0001\tlibc6\t2.41" in out
    assert verdict.read_text(encoding="utf-8") == out[1] + "\n"


def test_listing_is_capped(tmp_path, capsys):
    vulns = [vuln(vid=f"CVE-2026-{i:04d}") for i in range(10)]
    path = write(tmp_path, [os_result(vulns), lang_result()])
    run(path, "--summary-title", "t", "--list", "3")
    assert (
        sum(1 for ln in capsys.readouterr().out.splitlines() if ln.startswith("HIGH\t"))
        == 3
    )


def test_missing_report_is_scanner_failure(tmp_path, capsys):
    verdict = tmp_path / "x.status"
    assert (
        run(
            tmp_path / "nope.json",
            "--verdict-file",
            str(verdict),
            "--summary-title",
            "T",
        )
        == 2
    )
    captured = capsys.readouterr()
    assert (
        captured.out
        == "### T\n❌ Scan evaluation failed: report missing or unreadable (FileNotFoundError)\n"
    )
    assert verdict.read_text(encoding="utf-8").startswith("❌ Scan evaluation failed")


@pytest.mark.parametrize("schema", ["2", None, True, 2.5])
def test_schema_version_must_be_integer(tmp_path, schema):
    path = tmp_path / "r.json"
    path.write_text(
        json.dumps({"SchemaVersion": schema, "Results": []}), encoding="utf-8"
    )
    assert run(path) == 2


def test_non_object_and_invalid_json_fail(tmp_path):
    path = tmp_path / "r.json"
    path.write_text("[]", encoding="utf-8")
    assert run(path) == 2
    path.write_text("{not json", encoding="utf-8")
    assert run(path) == 2


def test_empty_report_has_no_coverage(tmp_path):
    assert run(write(tmp_path)) == 2
    assert run(write(tmp_path, [])) == 2


def test_listed_target_without_packages_is_no_proof(tmp_path, capsys):
    bare = {"Target": "Python", "Class": "lang-pkgs", "Vulnerabilities": [vuln()]}
    assert run(write(tmp_path, [os_result(), bare])) == 2
    assert "list-all-pkgs" in capsys.readouterr().err


def test_missing_expected_class_fails(tmp_path):
    assert run(write(tmp_path, [os_result()])) == 2
    assert run(write(tmp_path, [os_result()]), classes=("os-pkgs",)) == 0


def test_results_must_be_list_of_objects(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(
        json.dumps({"SchemaVersion": 2, "Results": {"a": 1}}), encoding="utf-8"
    )
    assert run(path) == 2


def config_result(failures=0, items=()):
    result = {
        "Target": "docker/Dockerfile",
        "Class": "config",
        "Type": "dockerfile",
        "MisconfSummary": {"Successes": 20, "Failures": failures},
    }
    if items:
        result["Misconfigurations"] = list(items)
    return result


def test_config_clean_scan_is_proven_by_summary(tmp_path, capsys):
    path = write(tmp_path, [config_result()], ArtifactType="repository")
    assert run(path, classes=("config",)) == 0
    assert "Covered config: docker/Dockerfile" in capsys.readouterr().err


def test_config_failures_count_and_passes_do_not(tmp_path, capsys):
    items = [
        {"ID": "DS-0002", "Severity": "HIGH", "Status": "FAIL"},
        {"ID": "DS-0026", "Severity": "HIGH", "Status": "PASS"},
        {"ID": "DS-0001", "Severity": "LOW", "Status": "FAIL"},
    ]
    path = write(tmp_path, [config_result(1, items)])
    assert run(path, classes=("config",)) == 0
    assert capsys.readouterr().out.startswith("⚠️ 0 CRITICAL, 1 HIGH")


def test_config_without_summary_fails(tmp_path):
    path = write(tmp_path, [{"Target": "x", "Class": "config"}])
    assert run(path, classes=("config",)) == 2


def test_secret_scan_needs_input_proof_not_results(tmp_path, capsys):
    path = write(tmp_path, ArtifactType="repository")
    scan_dir = tmp_path / "src"
    scan_dir.mkdir()
    assert run(path, classes=("secret",)) == 2
    assert run(path, "--input", str(scan_dir), classes=("secret",)) == 2
    (scan_dir / "a.txt").write_text("x", encoding="utf-8")
    log_file = tmp_path / "trivy.log"
    log_file.write_text("INFO [secret] Secret scanning is enabled\n", encoding="utf-8")
    capsys.readouterr()
    args = ("--input", str(scan_dir), "--log-file", str(log_file))
    assert run(path, *args, classes=("secret",)) == 0
    assert capsys.readouterr().out.startswith("✅")


def test_secret_scan_without_proof_of_the_scanner_fails(tmp_path):
    path = write(tmp_path, [], ArtifactType="filesystem")
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    scan = ("--input", str(tmp_path))
    # No log at all, and a log without the enabled line, both prove nothing.
    assert run(path, *scan, classes=("secret",)) == 2
    log_file = tmp_path / "trivy.log"
    log_file.write_text("INFO [misconfig] scanning\n", encoding="utf-8")
    assert run(path, *scan, "--log-file", str(log_file), classes=("secret",)) == 2


def test_secret_findings_are_counted(tmp_path, capsys):
    secret = {
        "Target": "a.env",
        "Class": "secret",
        "Secrets": [{"RuleID": "aws", "Severity": "CRITICAL"}],
    }
    path = write(tmp_path, [secret])
    target = tmp_path / "f.txt"
    target.write_text("x", encoding="utf-8")
    log_file = tmp_path / "trivy.log"
    log_file.write_text("INFO [secret] Secret scanning is enabled\n", encoding="utf-8")
    argv = ("--input", str(target), "--log-file", str(log_file), "--fail-on-findings")
    assert run(path, *argv, classes=("secret",)) == 1
    assert capsys.readouterr().out.startswith("⚠️ 1 CRITICAL, 0 HIGH")


def test_summary_block_values_cannot_close_the_code_fence(tmp_path, capsys):
    path = write(tmp_path, [os_result([vuln(pkg="a```\n## x")]), lang_result()])
    run(path, "--summary-title", "t", "--list", "5")
    assert capsys.readouterr().out.count("```") == 2


def test_expect_class_is_required(tmp_path):
    with pytest.raises(SystemExit) as exc:
        tr.main(["status", str(write(tmp_path, [os_result()]))])
    assert exc.value.code == 2


def frozen_result(packages=1):
    return {
        "Target": "requirements.txt",
        "Class": "lang-pkgs",
        "Type": "pip",
        "Packages": [{"Name": "jinja2", "Version": "3.1.6"}] * packages,
    }


def test_expected_target_with_packages_is_proven(tmp_path, capsys):
    path = write(tmp_path, [frozen_result()])
    assert run(path, "--expect-target", "requirements.txt", classes=("lang-pkgs",)) == 0
    assert capsys.readouterr().out.startswith("✅")


@pytest.mark.parametrize(
    "results",
    [
        [lang_result()],
        [frozen_result(packages=0)],
        [{**frozen_result(), "Target": "docs/requirements.txt"}],
    ],
)
def test_expected_target_missing_or_empty_fails(tmp_path, results):
    path = write(tmp_path, results)
    assert run(path, "--expect-target", "requirements.txt", classes=("lang-pkgs",)) == 2


@pytest.mark.parametrize(
    "line",
    [
        "WARN No enabled scanners found",
        "WARN [misconfig] Falling back to embedded checks",
        "warn: falling back to embedded checks",
    ],
)
def test_bad_scanner_log_lines_fail_the_scan(tmp_path, line):
    log_file = tmp_path / "trivy.log"
    log_file.write_text(f"INFO ok\n{line}\n", encoding="utf-8")
    path = write(tmp_path, [config_result()])
    assert run(path, "--log-file", str(log_file), classes=("config",)) == 2


def test_clean_scanner_log_passes_and_missing_log_fails(tmp_path):
    log_file = tmp_path / "trivy.log"
    log_file.write_text("INFO [misconfig] scanning is enabled\n", encoding="utf-8")
    path = write(tmp_path, [config_result()])
    assert run(path, "--log-file", str(log_file), classes=("config",)) == 0
    assert run(path, "--log-file", str(tmp_path / "gone.log"), classes=("config",)) == 2


def test_failing_on_findings_logs_the_verdict_as_error(tmp_path, capsys):
    path = write(tmp_path, [os_result([vuln()]), lang_result()])
    assert run(path, "--fail-on-findings") == 1
    assert "1 HIGH" in capsys.readouterr().err
