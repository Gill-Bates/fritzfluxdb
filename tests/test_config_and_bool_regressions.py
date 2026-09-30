#!/usr/bin/env python3
#
# tests/test_config_and_bool_regressions.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
import configparser
from datetime import UTC, datetime, timedelta

import pytest

from app.classes.common import FritzMeasurement
from app.classes.fritzbox.config import FritzBoxConfig
from app.classes.influxdb.config import InfluxDBConfig
from app.classes.influxdb.handler import InfluxHandler
from app.common import in_test_mode, parse_bool

_DB_ENV_VARS = (
    "DB_TYPE",
    "INFLUXDB_VERSION",
    "INFLUXDB_HOSTNAME",
    "INFLUXDB_PORT",
    "INFLUXDB_USERNAME",
    "INFLUXDB_PASSWORD",
    "INFLUXDB_TOKEN",
    "INFLUXDB_ORGANIZATION",
    "INFLUXDB_BUCKET",
    "INFLUXDB_TLS_ENABLED",
    "INFLUXDB_ALLOW_PLAINTEXT_CREDENTIALS",
    "QUESTDB_HOSTNAME",
    "QUESTDB_HOST",
    "QUESTDB_USERNAME",
    "QUESTDB_PASSWORD",
    "QUESTDB_TOKEN",
    "QUESTDB_DOWNSAMPLING",
)


@pytest.fixture
def clean_db_env(monkeypatch):
    for name in _DB_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    InfluxDBConfig.parser_error = False
    return monkeypatch


# --- boolean conversion ----------------------------------------------------

@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", False), ("1", True), ("false", False), ("true", True), (0, False), (1, True)],
)
def test_typed_bool_measurement_uses_the_boolean_parser(value, expected):
    """A TR-069 'name:bool' metric must not go through bool(), which makes '0' True."""
    measurement = FritzMeasurement("upgrade_available", value, data_type=bool)

    assert measurement.value is expected


def test_typed_bool_measurement_rejects_garbage():
    measurement = FritzMeasurement("upgrade_available", "maybe", data_type=bool)

    assert measurement.value is None


@pytest.mark.parametrize("value", ["false", "0", "", "  ", "no", "off"])
def test_testmode_is_disabled_for_falsy_values(monkeypatch, value):
    monkeypatch.setenv("TESTMODE", value)
    in_test_mode.cache_clear()

    assert in_test_mode() is False
    in_test_mode.cache_clear()


def test_testmode_is_enabled_for_truthy_values(monkeypatch):
    monkeypatch.setenv("TESTMODE", "true")
    in_test_mode.cache_clear()

    assert in_test_mode() is True
    in_test_mode.cache_clear()


def test_parse_bool_rejects_unknown_values():
    with pytest.raises(ValueError, match="invalid boolean value"):
        parse_bool("maybe")


# --- config fail-fast ------------------------------------------------------

def _fritzbox_config_data() -> configparser.ConfigParser:
    data = configparser.ConfigParser()
    data.add_section("fritzbox")
    data.set("fritzbox", "username", "user")
    data.set("fritzbox", "password", "secret")
    return data


def test_unparsable_int_config_is_a_parser_error(monkeypatch):
    monkeypatch.setenv("FRITZBOX_PORT", "abc")

    config = FritzBoxConfig(_fritzbox_config_data())

    assert config.parser_error is True


def test_unparsable_bool_config_is_a_parser_error(clean_db_env):
    clean_db_env.setenv("DB_TYPE", "influxdb_v1")
    clean_db_env.setenv("INFLUXDB_HOSTNAME", "localhost")
    clean_db_env.setenv("INFLUXDB_TLS_ENABLED", "perhaps")

    data = configparser.ConfigParser()
    data.add_section("influxdb")
    data.set("influxdb", "database", "fritzbox")

    assert InfluxDBConfig(data).parser_error is True


# --- backend selection -----------------------------------------------------

def test_explicit_db_type_wins_over_stale_questdb_hostname(clean_db_env):
    clean_db_env.setenv("DB_TYPE", "influxdb_v1")
    clean_db_env.setenv("QUESTDB_HOSTNAME", "questdb.example.com")
    clean_db_env.setenv("INFLUXDB_HOSTNAME", "influx.example.com")

    data = configparser.ConfigParser()
    data.add_section("influxdb")
    data.set("influxdb", "database", "fritzbox")

    config = InfluxDBConfig(data)

    assert config.version == 1
    assert config.hostname == "influx.example.com"
    assert config.parser_error is False


def test_questdb_hostname_alone_still_selects_questdb(clean_db_env):
    clean_db_env.setenv("QUESTDB_HOSTNAME", "questdb.example.com")

    config = InfluxDBConfig(configparser.ConfigParser())

    assert config.version == "questdb"
    assert config.hostname == "questdb.example.com"


def test_questdb_username_without_password_is_a_parser_error(clean_db_env):
    clean_db_env.setenv("DB_TYPE", "questdb")
    clean_db_env.setenv("QUESTDB_HOSTNAME", "localhost")
    clean_db_env.setenv("QUESTDB_USERNAME", "admin")

    assert InfluxDBConfig(configparser.ConfigParser()).parser_error is True


def test_questdb_username_and_password_together_are_accepted(clean_db_env):
    clean_db_env.setenv("DB_TYPE", "questdb")
    clean_db_env.setenv("QUESTDB_HOSTNAME", "localhost")
    clean_db_env.setenv("QUESTDB_USERNAME", "admin")
    clean_db_env.setenv("QUESTDB_PASSWORD", "quest")

    assert InfluxDBConfig(configparser.ConfigParser()).parser_error is False


def test_hostname_url_with_path_is_rejected(clean_db_env):
    clean_db_env.setenv("DB_TYPE", "questdb")
    clean_db_env.setenv("QUESTDB_HOSTNAME", "https://proxy.example.com/questdb")

    assert InfluxDBConfig(configparser.ConfigParser()).parser_error is True


def test_hostname_url_without_path_is_accepted(clean_db_env):
    clean_db_env.setenv("DB_TYPE", "questdb")
    clean_db_env.setenv("QUESTDB_HOSTNAME", "https://proxy.example.com/")

    config = InfluxDBConfig(configparser.ConfigParser())

    assert config.parser_error is False
    assert config.hostname == "proxy.example.com"
    assert config.tls_enabled is True


def test_invalid_db_type_does_not_raise_in_the_handler(clean_db_env):
    clean_db_env.setenv("DB_TYPE", "mongodb")
    clean_db_env.setenv("INFLUXDB_HOSTNAME", "localhost")

    handler = InfluxHandler(configparser.ConfigParser())

    assert handler.config.parser_error is True


# --- writer backoff --------------------------------------------------------

def test_backoff_blocks_reconnect_while_the_interval_is_active(clean_db_env, monkeypatch):
    clean_db_env.setenv("DB_TYPE", "influxdb_v1")
    clean_db_env.setenv("INFLUXDB_HOSTNAME", "localhost")

    data = configparser.ConfigParser()
    data.add_section("influxdb")
    data.set("influxdb", "database", "fritzbox")

    handler = InfluxHandler(data)
    handler.client = None
    handler.connection_lost = True
    handler.current_retry_interval = handler.max_retry_interval
    handler.last_write_retry = datetime.now(UTC) - timedelta(seconds=1)
    handler.buffer = [FritzMeasurement("uptime", 1)]

    async def fail(*_args, **_kwargs):
        raise AssertionError("_init_client() must not be called during an active backoff")

    monkeypatch.setattr(handler, "_init_client", fail)

    asyncio.run(handler.write_data())


# --- FritzOS version parsing ----------------------------------------------

@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("113.07.90", "7.90"),
        ("113.07.90-123456", "7.90"),
        ("7.90", "7.90"),
        ("7.90-123456", "7.90"),
        ("113.07.90-98765 Labor", "7.90"),
        ("", None),
        (None, None),
        ("unknown", None),
    ],
)
def test_fw_version_tolerates_build_and_lab_suffixes(reported, expected):
    config = FritzBoxConfig(_fritzbox_config_data())
    config.fw_version = reported

    assert config.fw_version == expected


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("192.168.178.1", "192.168.178.1"),
        ("fritz.box", "fritz.box"),
        ("fd00::1", "fd00::1"),
        ("[fd00::1]", "fd00::1"),
    ],
)
def test_hostname_accepts_names_and_ipv6_literals(monkeypatch, configured, expected):
    monkeypatch.setenv("FRITZBOX_HOSTNAME", configured)

    config = FritzBoxConfig(_fritzbox_config_data())

    assert config.parser_error is False
    assert config.hostname == expected


def test_hostname_still_rejects_a_url(monkeypatch):
    monkeypatch.setenv("FRITZBOX_HOSTNAME", "http://fritz.box")

    assert FritzBoxConfig(_fritzbox_config_data()).parser_error is True
