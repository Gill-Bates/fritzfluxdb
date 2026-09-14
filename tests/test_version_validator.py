#!/usr/bin/env python3
#
# tests/test_version_validator.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "docker" / "validate-version.sh"


@pytest.mark.parametrize("version", ["1.5", "1.5.0", "1.5.0-rc.1"])
def test_version_validator_accepts_release_versions(version):
    result = subprocess.run(["bash", SCRIPT, version], check=False, capture_output=True, text=True)
    assert result.returncode == 0


@pytest.mark.parametrize("version", ["", "release", "1", "1.2.3.4", "1.5.0 rc1"])
def test_version_validator_rejects_invalid_versions(version):
    result = subprocess.run(["bash", SCRIPT, version], check=False, capture_output=True, text=True)
    assert result.returncode != 0
