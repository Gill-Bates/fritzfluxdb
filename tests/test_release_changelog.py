#!/usr/bin/env python3
#
# tests/test_release_changelog.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Changelog lookup of the release script (.github/scripts/create-release.js)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "create-release.js"

CHANGELOG = """\
# Changelog

## [v1.5.0-rc.1] - 2026-09-01

- rc notes

## [v1.5.0] - 2026-09-14

- stable notes

<details markdown="1">
<summary>Previous versions...</summary>

## [v1.4] - 2026-09-04

- old notes

</details>
"""


def extract(version: str, changelog: str = CHANGELOG):
    node = shutil.which("node")
    assert node, "node is required to test the release script"
    code = (
        "const { extractVersionSection } = require(process.argv[1]);"
        "process.stdout.write(JSON.stringify("
        "extractVersionSection(process.argv[2], process.argv[3])));"
    )
    result = subprocess.run(
        [node, "-e", code, str(SCRIPT), changelog, version],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.5.0", "- stable notes"),
        ("1.5.0-rc.1", "- rc notes"),  # prerelease suffix is significant
        ("1.5", "- stable notes"),  # normalized match
        ("1.4", "- old notes"),  # last section inside <details>
        ("1.4.0", "- old notes"),
        ("1.6.0", None),
    ],
)
def test_extract_version_section(version, expected):
    assert extract(version) == expected


def test_exact_heading_wins_over_earlier_normalized_one():
    changelog = "## [1.4] - a\n\n- equivalent\n\n## [v1.4.0] - b\n\n- exact\n"
    assert extract("1.4.0", changelog) == "- exact"
