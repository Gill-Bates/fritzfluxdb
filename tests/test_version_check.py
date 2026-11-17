#!/usr/bin/env python3
#
# tests/test_version_check.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Tests for the startup version check (no network access)."""

from __future__ import annotations

import httpx
import pytest

from app.fritzbox import banner, version_check
from app.fritzbox.version_check import (
    build_version_message,
    fetch_latest_version,
    parse_version,
)


def test_newer_release_is_yellow_with_link():
    text, color = build_version_message("1.6.2", "v1.10.0")
    assert color == "yellow"
    assert "v1.10.0" in text and "(running: v1.6.2)" in text
    assert version_check.RELEASES_URL in text


def test_semantic_not_string_comparison():
    assert build_version_message("1.9.0", "v1.10.0")[1] == "yellow"
    text, _ = build_version_message("1.10.0", "v1.9.0")
    assert "pre-release" not in text
    assert "v1.10.0" in text


def test_same_version_is_green():
    assert build_version_message("1.6.2", "v1.6.2") == ("Your version is up to date.", "green")


def test_running_newer_gets_a_neutral_notice():
    text, color = build_version_message("1.7.0", "v1.6.2")
    assert "pre-release" not in text
    assert "v1.7.0" in text
    assert color == "yellow"


def test_rc_sorts_below_final():
    assert parse_version("1.6.2-rc1") < parse_version("1.6.2")


@pytest.mark.parametrize(
    ("lower", "higher"),
    [
        ("1.2.0-rc1", "1.2.0-rc2"),
        ("1.2.0-rc2", "1.2.0-rc10"),
        ("1.2.0-rc.1", "1.2.0-rc.2"),
        ("1.2.0-alpha", "1.2.0-beta"),
        ("1.2.0", "1.2.1-rc1"),
    ],
)
def test_prerelease_stages_are_ordered_by_semver_precedence(lower, higher):
    assert parse_version(lower) < parse_version(higher)


def test_build_metadata_does_not_change_precedence():
    assert parse_version("1.2.0+build5") == parse_version("1.2.0")


def test_a_newer_stable_release_is_offered_to_a_running_release_candidate():
    text, color = build_version_message("1.2.0-rc1", "v1.2.0")
    assert text.startswith("A new version is available: v1.2.0")
    assert color == "yellow"


@pytest.mark.parametrize("running,latest", [("dev", "v1.0.0"), ("1.0.0", "nightly"), ("1.0.0", None), ("1.0.0", 5)])
def test_unparsable_is_silent(running, latest):
    assert build_version_message(running, latest) is None


class _Resp:
    def __init__(self, payload=None, error=None):
        self._payload, self._error = payload, error

    def raise_for_status(self):
        if self._error:
            raise self._error

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_fetch_success_sets_user_agent_and_timeout(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs)
        return _Resp({"tag_name": "v2.0.0"})

    monkeypatch.setattr(version_check.httpx, "get", fake_get)
    assert fetch_latest_version() == "v2.0.0"
    assert seen["headers"]["User-Agent"] and seen["timeout"] <= 5


def _raise_offline(*_args, **_kwargs):
    raise httpx.ConnectError("offline")


@pytest.mark.parametrize(
    "fake_get",
    [
        _raise_offline,
        lambda *a, **k: _Resp(error=httpx.HTTPStatusError("403", request=None, response=None)),
        lambda *a, **k: _Resp(payload=ValueError("bad json")),
    ],
    ids=["offline", "http-error", "bad-json"],
)
def test_fetch_errors_return_none(monkeypatch, fake_get):
    monkeypatch.setattr(version_check.httpx, "get", fake_get)
    assert fetch_latest_version() is None


def test_fetch_non_dict_json_returns_none(monkeypatch):
    monkeypatch.setattr(version_check.httpx, "get", lambda *a, **k: _Resp(["x"]))
    assert fetch_latest_version() is None


def test_notice_color_only_when_requested(monkeypatch):
    monkeypatch.setattr(version_check, "fetch_latest_version", lambda: "v1.0.0")
    assert version_check.version_notice("1.0.0", False) == "Your version is up to date."
    assert version_check.version_notice("1.0.0", True).startswith("\033[92m")


def test_banner_prints_plain_notice_without_tty(monkeypatch, capsys):
    monkeypatch.setattr(version_check, "fetch_latest_version", lambda: "v99.0.0")
    banner.print_banner()
    out = capsys.readouterr().out
    assert "A new version is available: v99.0.0" in out
    assert "\033[93m" not in out


def test_banner_stays_plain_with_no_color_on_a_tty(monkeypatch, capsys):
    monkeypatch.setattr(version_check, "fetch_latest_version", lambda: "v99.0.0")
    monkeypatch.setattr(banner.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    banner.print_banner()
    assert "\033[" not in capsys.readouterr().out


def test_banner_is_colored_on_a_tty_without_no_color(monkeypatch, capsys):
    monkeypatch.setattr(version_check, "fetch_latest_version", lambda: "v99.0.0")
    monkeypatch.setattr(banner.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    banner.print_banner()
    assert "\033[96m" in capsys.readouterr().out


def test_release_urls_derive_from_the_project_url():
    from app.utils.version import URL

    assert version_check.RELEASES_URL == f"{URL}/releases/latest"
    assert version_check.LATEST_RELEASE_API.startswith("https://api.github.com/repos/")
    assert version_check.LATEST_RELEASE_API.endswith("/releases/latest")
    assert URL.removeprefix("https://github.com/") in version_check.LATEST_RELEASE_API
