"""Tests for the startup version check (no network access)."""

from __future__ import annotations

import httpx
import pytest

from app.classes.fritzbox import banner, version_check
from app.classes.fritzbox.version_check import build_version_message, fetch_latest_version, parse_version


def test_newer_release_is_yellow_with_link():
    text, color = build_version_message("1.6.2", "v1.10.0")
    assert color == "yellow"
    assert "v1.10.0" in text and "(running: v1.6.2)" in text
    assert version_check.RELEASES_URL in text


def test_semantic_not_string_comparison():
    assert build_version_message("1.9.0", "v1.10.0")[1] == "yellow"
    assert build_version_message("1.10.0", "v1.9.0")[0] == "You are running a pre-release version!"


def test_same_version_is_green():
    assert build_version_message("1.6.2", "v1.6.2") == ("Your version is up to date.", "green")


def test_running_newer_is_prerelease_notice():
    assert build_version_message("1.7.0", "v1.6.2") == ("You are running a pre-release version!", "yellow")


def test_rc_sorts_below_final():
    assert parse_version("1.6.2-rc1") < parse_version("1.6.2")


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
