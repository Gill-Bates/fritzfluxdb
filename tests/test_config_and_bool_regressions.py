#!/usr/bin/env python3
#
# tests/test_config_and_bool_regressions.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
import configparser
import logging
from datetime import UTC, datetime, timedelta

import pytest

from app.fritzbox.config import FritzBoxConfig
from app.influxdb.config import InfluxDBConfig
from app.influxdb.handler import InfluxHandler
from app.utils.common import in_test_mode, parse_bool
from app.utils.measurement import FritzMeasurement


# A host variable overrides the INI values these tests build, so every test starts without any of them.
@pytest.fixture(autouse=True)
def clean_db_env(monkeypatch, no_database_env, no_fritzbox_env):
    return monkeypatch


# Boolean conversion

@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", False), ("1", True), ("false", False), ("true", True), (0, False), (1, True)],
)
def test_typed_bool_measurement_uses_the_boolean_parser(value, expected):
    """A TR-064 'name:bool' metric must not go through bool(), which makes '0' True."""
    measurement = FritzMeasurement("upgrade_available", value, data_type=bool)

    assert measurement.value is expected


def test_typed_bool_measurement_rejects_garbage():
    measurement = FritzMeasurement("upgrade_available", "maybe", data_type=bool)

    assert measurement.value is None


@pytest.mark.parametrize("value", ["false", "0", "", "  ", "no", "off"])
def test_testmode_is_disabled_for_falsy_values(monkeypatch, value):
    monkeypatch.setenv("TESTMODE", value)

    assert in_test_mode() is False


def test_testmode_is_enabled_for_truthy_values(monkeypatch):
    monkeypatch.setenv("TESTMODE", "true")

    assert in_test_mode() is True


def test_parse_bool_rejects_unknown_values():
    with pytest.raises(ValueError, match="invalid boolean value"):
        parse_bool("maybe")


# Config fail-fast

def _fritzbox_ini(**options):
    data = configparser.ConfigParser()
    data.add_section("fritzbox")
    for key, value in {"username": "user", "password": "pw", **options}.items():
        data.set("fritzbox", key, value)
    return data


def _influxdb_ini():
    data = configparser.ConfigParser()
    data.add_section("influxdb")
    data.set("influxdb", "database", "fritzbox")
    return data


def test_unparsable_int_config_is_a_parser_error(monkeypatch):
    monkeypatch.setenv("FRITZBOX_PORT", "abc")

    config = FritzBoxConfig(_fritzbox_ini())

    assert config.parser_error is True


def test_unparsable_bool_config_is_a_parser_error(clean_db_env):
    clean_db_env.setenv("DB_TYPE", "influxdb_v1")
    clean_db_env.setenv("INFLUXDB_HOSTNAME", "localhost")
    clean_db_env.setenv("INFLUXDB_TLS_ENABLED", "perhaps")

    assert InfluxDBConfig(_influxdb_ini()).parser_error is True


# Backend selection

def test_explicit_db_type_wins_over_stale_questdb_hostname(clean_db_env):
    clean_db_env.setenv("DB_TYPE", "influxdb_v1")
    clean_db_env.setenv("QUESTDB_HOSTNAME", "questdb.example.com")
    clean_db_env.setenv("INFLUXDB_HOSTNAME", "influx.example.com")

    config = InfluxDBConfig(_influxdb_ini())

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
    assert config.port == 443


@pytest.mark.parametrize(
    ("hostname", "expected_port"),
    [
        ("https://proxy.example.com:8443", 8443),
        ("http://proxy.example.com", 80),
        ("proxy.example.com", 9000),
    ],
)
def test_questdb_port_comes_from_url_and_9000_is_only_the_fallback(clean_db_env, hostname, expected_port):
    clean_db_env.setenv("DB_TYPE", "questdb")
    clean_db_env.setenv("QUESTDB_HOSTNAME", hostname)

    config = InfluxDBConfig(configparser.ConfigParser())

    assert config.parser_error is False
    assert config.port == expected_port


def test_questdb_explicit_port_wins_over_url_without_port(clean_db_env):
    clean_db_env.setenv("DB_TYPE", "questdb")
    clean_db_env.setenv("QUESTDB_HOSTNAME", "https://proxy.example.com")
    clean_db_env.setenv("QUESTDB_PORT", "9443")

    assert InfluxDBConfig(configparser.ConfigParser()).port == 9443


@pytest.mark.parametrize(
    "hostname",
    [
        "https://user:secret@db.example/path",
        "https://db.example:invalid",
        "https://bad host.example",
        "ftp://db.example",
        "https://db.example/path?token=secret",
        "db.example/path?token=secret",
        "db.example?token=secret",
        "db.example#secret",
        "user:secret@db.example",
        "bad host.example",
        "db.example[",
        "[127.0.0.1]",
    ],
)
def test_invalid_database_host_is_rejected_without_logging_input(clean_db_env, caplog, hostname):
    clean_db_env.setenv("DB_TYPE", "influxdb_v1")
    clean_db_env.setenv("INFLUXDB_HOSTNAME", hostname)
    caplog.set_level(logging.DEBUG)

    config = InfluxDBConfig(_influxdb_ini())

    assert config.parser_error is True
    assert hostname not in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.parametrize("hostname", ["2001:db8::1", "[2001:db8::1]"])
def test_plain_ipv6_database_hostname_is_accepted(clean_db_env, hostname):
    clean_db_env.setenv("DB_TYPE", "questdb")
    clean_db_env.setenv("QUESTDB_HOSTNAME", hostname)

    config = InfluxDBConfig(configparser.ConfigParser())

    assert config.parser_error is False
    assert config.hostname == hostname


def test_plain_database_hostname_is_trimmed(clean_db_env):
    clean_db_env.setenv("DB_TYPE", "questdb")
    clean_db_env.setenv("QUESTDB_HOSTNAME", "  quest-db  ")

    config = InfluxDBConfig(configparser.ConfigParser())

    assert config.parser_error is False
    assert config.hostname == "quest-db"


def test_invalid_db_type_does_not_raise_in_the_handler(clean_db_env):
    clean_db_env.setenv("DB_TYPE", "mongodb")
    clean_db_env.setenv("INFLUXDB_HOSTNAME", "localhost")

    handler = InfluxHandler(configparser.ConfigParser())

    assert handler.config.parser_error is True


# Writer backoff

def test_backoff_blocks_reconnect_while_the_interval_is_active(clean_db_env, monkeypatch):
    clean_db_env.setenv("DB_TYPE", "influxdb_v1")
    clean_db_env.setenv("INFLUXDB_HOSTNAME", "localhost")

    handler = InfluxHandler(_influxdb_ini())
    handler.client = None
    handler.connection_lost = True
    handler.current_retry_interval = handler.max_retry_interval
    handler.last_write_retry = datetime.now(UTC) - timedelta(seconds=1)
    handler.buffer = [FritzMeasurement("uptime", 1)]

    async def fail(*_args, **_kwargs):
        raise AssertionError("_init_client() must not be called during an active backoff")

    monkeypatch.setattr(handler, "_init_client", fail)

    asyncio.run(handler.write_data())
    assert len(handler.buffer) == 1


# FritzOS version parsing

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
    config = FritzBoxConfig(_fritzbox_ini())
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

    config = FritzBoxConfig(_fritzbox_ini())

    assert config.parser_error is False
    assert config.hostname == expected


def test_hostname_still_rejects_a_url(monkeypatch):
    monkeypatch.setenv("FRITZBOX_HOSTNAME", "http://fritz.box")

    assert FritzBoxConfig(_fritzbox_ini()).parser_error is True


@pytest.mark.parametrize(
    "configured",
    ["fritz.box/path", "fritz.box?x=1", "fritz.box#frag", "user:secret@fritz.box", "user@fritz.box", "//[fritz.box"],
)
def test_hostname_rejects_paths_userinfo_and_malformed_values(monkeypatch, configured):
    monkeypatch.setenv("FRITZBOX_HOSTNAME", configured)
    assert FritzBoxConfig(_fritzbox_ini()).parser_error is True


# Pydantic models

def test_fritzbox_defaults():
    config = FritzBoxConfig(_fritzbox_ini())

    assert (config.hostname, config.port, config.connect_timeout) == ("192.168.178.1", 49000, 10)
    assert (config.request_interval, config.box_tag, str(config.timezone)) == (10, "fritz.box", "Europe/Berlin")
    assert config.parser_error is False


def test_source_priority_is_env_then_ini_key_then_default(monkeypatch):
    assert FritzBoxConfig(_fritzbox_ini(hostname="key.box")).hostname == "key.box"

    monkeypatch.setenv("FRITZBOX_HOSTNAME", "env.box")
    assert FritzBoxConfig(_fritzbox_ini(hostname="key.box")).hostname == "env.box"


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("port", "70000", "FritzBox port 70000 is invalid"),
        ("port", "abc", "Unable to parse 'abc' for 'port' as int"),
        ("connect_timeout", "0", "connect_timeout must be at least 1 second"),
        ("request_interval", "99999", "request_interval must not exceed 3600 seconds"),
        ("timezone", "Mars/Base", "time zone 'Mars/Base' is invalid/unknown"),
    ],
)
def test_fritzbox_validation_errors_are_logged_as_lines_without_traceback(caplog, option, value, message):
    config = FritzBoxConfig(_fritzbox_ini(**{option: value}))

    assert config.parser_error is True
    assert message in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    # the rejected option falls back to its default, the object stays usable
    assert config.hostname == "192.168.178.1"


def test_fritzbox_missing_credentials_are_a_parser_error(caplog):
    data = configparser.ConfigParser()

    assert FritzBoxConfig(data).parser_error is True
    assert "FritzBox username not defined" in caplog.text
    assert "FritzBox password not defined" in caplog.text


def test_fritzbox_password_is_a_secret_and_masked_in_debug_output(caplog):
    caplog.set_level("DEBUG")
    config = FritzBoxConfig(_fritzbox_ini(password="hunter2"))

    assert "hunter2" not in repr(config)
    assert "hunter2" not in caplog.text
    assert "Config: fritzbox.password = ***" in caplog.text
    assert config.password.get_secret_value() == "hunter2"


def test_influxdb_config_is_frozen_and_masks_secrets(clean_db_env, caplog):
    caplog.set_level("DEBUG")
    clean_db_env.setenv("DB_TYPE", "influxdb_v2")
    clean_db_env.setenv("INFLUXDB_HOSTNAME", "localhost")
    clean_db_env.setenv("INFLUXDB_TOKEN", "s3cret-token")
    clean_db_env.setenv("INFLUXDB_ORGANIZATION", "org")
    clean_db_env.setenv("INFLUXDB_BUCKET", "bucket")

    config = InfluxDBConfig(configparser.ConfigParser())

    assert config.parser_error is False
    assert config.version == 2
    assert config.token.get_secret_value() == "s3cret-token"
    assert "s3cret-token" not in repr(config) + caplog.text
    with pytest.raises(ValueError, match="frozen"):
        config.hostname = "other"
    assert config.model_copy(update={"measurement_name": "x"}).measurement_name == "x"


def test_influxdb_plaintext_credentials_stay_blocked(clean_db_env, caplog):
    clean_db_env.setenv("DB_TYPE", "influxdb_v1")
    clean_db_env.setenv("INFLUXDB_HOSTNAME", "db.example")
    clean_db_env.setenv("INFLUXDB_DATABASE", "fritz")
    clean_db_env.setenv("INFLUXDB_USERNAME", "u")
    clean_db_env.setenv("INFLUXDB_PASSWORD", "p")

    assert InfluxDBConfig(configparser.ConfigParser()).parser_error is True
    assert "must not be sent over plain HTTP" in caplog.text

    clean_db_env.setenv("INFLUXDB_ALLOW_PLAINTEXT_CREDENTIALS", "true")
    assert InfluxDBConfig(configparser.ConfigParser()).parser_error is False

