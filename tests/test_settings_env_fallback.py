#!/usr/bin/env python3
#
# tests/test_settings_env_fallback.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import configparser

import pytest
from pydantic import SecretStr

from app.utils.configparser import import_config
from app.utils.settings import ConfigModel


class DemoConfig(ConfigModel):
    config_section_name = "demo"
    host: str = "default-host"
    port: int = 1
    password: SecretStr | None = None
    flag: bool = False


def _ini() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser["demo"] = {"host": "ini-host", "port": "8086", "password": "ini-secret", "flag": "true"}
    return parser


@pytest.mark.parametrize("blank", ["", " ", "\t"])
def test_blank_environment_variable_falls_back_to_the_ini_value(monkeypatch, blank):
    for name in ("HOST", "PORT", "PASSWORD", "FLAG"):
        monkeypatch.setenv(f"DEMO_{name}", blank)
    raw = DemoConfig.collect_raw(_ini())
    assert raw == {"host": "ini-host", "port": "8086", "password": "ini-secret", "flag": "true"}


def test_missing_environment_variable_uses_the_ini_value(monkeypatch):
    for name in ("HOST", "PORT", "PASSWORD", "FLAG"):
        monkeypatch.delenv(f"DEMO_{name}", raising=False)
    assert DemoConfig.collect_raw(_ini())["host"] == "ini-host"


def test_non_blank_environment_variable_wins_over_the_ini_value(monkeypatch):
    monkeypatch.setenv("DEMO_HOST", "env-host")
    monkeypatch.setenv("DEMO_FLAG", "false")
    raw = DemoConfig.collect_raw(_ini())
    assert raw["host"] == "env-host" and raw["flag"] == "false" and raw["port"] == "8086"


def test_blank_values_without_ini_option_leave_the_field_out(monkeypatch):
    monkeypatch.setenv("DEMO_HOST", " ")
    assert "host" not in DemoConfig.collect_raw(configparser.ConfigParser())


def test_ini_percent_characters_are_literal_values(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    path = tmp_path / "percent.ini"
    path.write_text("[demo]\npassword = p%ss\n", encoding="utf-8")

    config = DemoConfig(import_config([str(path)]))

    assert config.password.get_secret_value() == "p%ss"
