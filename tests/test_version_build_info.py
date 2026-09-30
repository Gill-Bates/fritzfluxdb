#!/usr/bin/env python3
#
# tests/test_version_build_info.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from pathlib import Path

from app import version
from app.classes.fritzbox.banner import print_banner


def write_build_info(directory: Path, sha: str = "abc123") -> Path:
    path = directory / "BUILD_INFO"
    path.write_text(f"GIT_SHA={sha}\nBUILD_DATE=2026-09-28T14:36:39Z\n", encoding="utf-8")
    return path


def test_explicit_path_is_parsed(tmp_path):
    info = version.read_build_info(write_build_info(tmp_path))

    assert info == {"GIT_SHA": "abc123", "BUILD_DATE": "2026-09-28T14:36:39Z"}


def test_app_home_is_used_when_the_package_lives_elsewhere(tmp_path, monkeypatch):
    # Mirrors the container layout: the package is installed into site-packages, so
    # BUILD_INFO is not next to it and only APP_HOME points at the project root.
    site_packages = tmp_path / "site-packages"
    (site_packages / "app").mkdir(parents=True)
    app_home = tmp_path / "app"
    app_home.mkdir()
    write_build_info(app_home, sha="deadbeef")
    monkeypatch.setattr(version, "_PROJECT_ROOT", site_packages)
    monkeypatch.setenv("APP_HOME", str(app_home))

    assert version.read_build_info()["GIT_SHA"] == "deadbeef"


def test_missing_build_info_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "_PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("APP_HOME", raising=False)

    assert version.read_build_info() == {}


def test_project_version_comes_from_pyproject(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "9.8.7"\n', encoding="utf-8")
    monkeypatch.setattr(version, "_PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("APP_HOME", raising=False)
    monkeypatch.setattr(version, "_pkg_version", lambda _: "installed-version")

    assert version.read_project_version() == "9.8.7"


def test_project_version_falls_back_to_installed_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "_PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("APP_HOME", raising=False)
    monkeypatch.setattr(version, "_pkg_version", lambda _: "installed-version")

    assert version.read_project_version() == "installed-version"


def test_project_version_uses_app_home_for_container_layout(tmp_path, monkeypatch):
    app_home = tmp_path / "app"
    app_home.mkdir()
    (app_home / "pyproject.toml").write_text('[project]\nversion = "4.3.2"\n', encoding="utf-8")
    monkeypatch.setattr(version, "_PROJECT_ROOT", tmp_path / "site-packages")
    monkeypatch.setenv("APP_HOME", str(app_home))
    monkeypatch.setattr(version, "_pkg_version", lambda _: "installed-version")

    assert version.read_project_version() == "4.3.2"


def test_startup_banner_uses_project_version(capsys, monkeypatch):
    monkeypatch.setattr("app.classes.fritzbox.banner.version_notice", lambda *_: None)
    print_banner()

    assert f"v{version.read_project_version()}" in capsys.readouterr().out


def test_oversized_build_info_is_ignored(tmp_path):
    path = tmp_path / "BUILD_INFO"
    path.write_text("GIT_SHA=x\n" + "#" * version._MAX_BUILD_INFO_BYTES, encoding="utf-8")

    assert version.read_build_info(path) == {}
