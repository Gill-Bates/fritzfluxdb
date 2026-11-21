#!/usr/bin/env python3
#
# tests/conftest.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared test setup."""

import logging
import os
import sys
from pathlib import Path

import pytest

from app.utils.common import in_test_mode

# The CI tools import each other as top-level modules, as they do when CI runs them by path.
_TOOLS_DIR = str(Path(__file__).resolve().parents[1] / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)


# setup_logging reconfigures these process-wide loggers; restore them so no test depends on run order.
_PROJECT_LOGGERS = ("fritzFlux", "httpx", "httpcore")


@pytest.fixture(autouse=True)
def _restore_project_loggers():
    saved = [
        (logger, logger.level, list(logger.handlers), logger.propagate, logger.disabled)
        for logger in map(logging.getLogger, _PROJECT_LOGGERS)
    ]
    root = logging.getLogger()
    root_level, root_handlers = root.level, list(root.handlers)
    yield
    # LineLog.setup() configures the root logger once; keep its level from leaking into other tests.
    root.setLevel(root_level)
    root.handlers[:] = root_handlers
    for logger, level, handlers, propagate, disabled in saved:
        logger.setLevel(level)
        logger.handlers[:] = handlers
        logger.propagate = propagate
        logger.disabled = disabled


# in_test_mode is a process-wide @cache of TESTMODE; clear it around every test so no result leaks.
@pytest.fixture(autouse=True)
def _reset_test_mode_cache():
    in_test_mode.cache_clear()
    yield
    in_test_mode.cache_clear()


# ConfigModel reads {SECTION}_{NAME}; InfluxDBConfig also reads DB_TYPE and maps QUESTDB_*.
_DATABASE_ENV_PREFIXES = ("INFLUXDB_", "QUESTDB_")


@pytest.fixture
def no_database_env(monkeypatch):
    """Remove every variable InfluxDBConfig reads, so a host value cannot change a result."""
    for name in list(os.environ):
        if name == "DB_TYPE" or name.startswith(_DATABASE_ENV_PREFIXES):
            monkeypatch.delenv(name)


@pytest.fixture
def no_fritzbox_env(monkeypatch):
    """Remove every FRITZBOX_* variable, so a host value cannot change a FritzBoxConfig default."""
    for name in list(os.environ):
        if name.startswith("FRITZBOX_"):
            monkeypatch.delenv(name)


# -q hides the report header that carries the seed; print it again so a red CI run is reproducible.
def pytest_terminal_summary(terminalreporter, config):
    seed = config.getoption("randomly_seed", default=None)
    if seed is not None:
        terminalreporter.write_line(f"Reproduce with: pytest -q -p no:xdist -p randomly --randomly-seed={seed}")
