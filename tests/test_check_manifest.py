#!/usr/bin/env python3
#
# tests/test_check_manifest.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Version shape and exact-pin rules of tools/check_manifest.py."""

import check_manifest as cm
import pytest


@pytest.mark.parametrize(
    "requirement",
    [
        "httpx==0.28.1",
        "pydantic==2.12.0",
        "uvicorn[standard]==0.30.0",
        'tomli==2.0.1; python_version < "3.11"',
        "pytest-randomly==5.1.0",
        "gillbates-logging @ git+https://github.com/Gill-Bates/python-libraries.git@gillbates-logging-v0.10.0#subdirectory=packages/logging",
    ],
)
def test_exact_pins_pass(requirement):
    assert cm.is_pinned(requirement)


@pytest.mark.parametrize(
    "requirement",
    [
        "httpx",
        "httpx>=0.27",
        "httpx~=0.27",
        'httpx>=0.27; python_version == "3.13"',
        'httpx; python_version == "3.13"',
        "httpx==0.27.*",
        "httpx==0.27,<1",
        "httpx @ https://example.invalid/httpx.whl",
        "httpx===0.27",
        "gillbates-logging @ git+https://github.com/Gill-Bates/python-libraries.git@main#subdirectory=packages/logging",
        "gillbates-logging @ git+https://github.com/Gill-Bates/python-libraries.git@gillbates-logging-v0.10#subdirectory=packages/logging",
        "gillbates-logging @ git+https://github.com/other/python-libraries.git@gillbates-logging-v0.10.0#subdirectory=packages/logging",
    ],
)
def test_loose_requirements_fail(requirement):
    assert not cm.is_pinned(requirement)


def test_find_problems_reports_version_and_groups():
    project = {
        "version": "1.2",
        "dependencies": ["httpx==0.28.1"],
        "optional-dependencies": {"dev": ['ruff>=1; python_version == "3.13"']},
    }
    problems = cm.find_problems(project)
    assert any("Invalid release version" in p for p in problems)
    assert any("Unpinned dev" in p for p in problems)


def test_valid_project_has_no_problems():
    project = {
        "version": "2.0.0-rc.1",
        "dependencies": ["httpx==0.28.1"],
        "optional-dependencies": {"dev": ["ruff==0.9.0"]},
    }
    assert cm.find_problems(project) == []


def test_repository_manifest_is_valid():
    assert cm.main(["--pyproject", str(cm.DEFAULT_PYPROJECT)]) == 0
