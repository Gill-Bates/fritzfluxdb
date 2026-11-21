#!/usr/bin/env python3
#
# tests/test_shutdown_stress.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Exercise all shutdown phases together with simulated stalled dependencies."""

import asyncio
import configparser
import logging
import signal
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import app.main as app_main
from app.fritzbox.handler import FritzBoxHandler
from app.fritzbox.service_handler import FritzBoxTR064Service
from app.influxdb.handler import InfluxHandler
from app.utils.measurement import FritzMeasurement


def test_shutdown_with_hung_box_full_queue_and_slow_database(monkeypatch, caplog):
    release_box = threading.Event()
    box_finished = threading.Event()
    state = {}
    config = configparser.ConfigParser()
    config.read_dict({
        "fritzbox": {"hostname": "box", "username": "test", "password": "test"},
        "influxdb": {"hostname": "database", "database": "metrics"},
    })
    monkeypatch.setattr(app_main, "QUEUE_DRAIN_TIMEOUT", 0.03)
    monkeypatch.setattr("app.fritzbox.handler.SHUTDOWN_DRAIN_TIMEOUT", 0.03)
    monkeypatch.setattr(app_main, "parse_command_line", lambda *args: SimpleNamespace(
        log_level="INFO", verbose=0, config_file=None,
    ))
    monkeypatch.setattr(app_main, "import_config", lambda *args: config)
    monkeypatch.setattr(app_main, "setup_logging", lambda *args: logging.getLogger("fritzFlux"))

    async def scenario():
        loop = asyncio.get_running_loop()
        callbacks = {}
        monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback, *args: callbacks.__setitem__(sig, lambda: callback(*args)))
        box_started, write_started, queue_full = asyncio.Event(), asyncio.Event(), asyncio.Event()
        box = FritzBoxHandler(config)
        box.config.fw_version = "6.90"
        box.config.wan_watch_interval = 0
        box.init_successful = True
        box.connect = Mock()
        box.services = [FritzBoxTR064Service({"name": "stalled", "actions": ["GetInfo"]})]

        def query(_):
            state["box_started"] = True
            loop.call_soon_threadsafe(box_started.set)
            try:
                release_box.wait()
            finally:
                box_finished.set()

        monkeypatch.setattr(box, "query_service_data", query)
        database = InfluxHandler(config)
        database.preflight = AsyncMock()
        marker = FritzMeasurement("queued", 1)
        database.append_measurement(marker)

        async def slow_write(*, force=False):
            if force:
                state["final_flush"] = True
            write_started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(database, "write_data", slow_write)

        async def final_flush():
            await InfluxHandler._flush_buffer_before_close(database, timeout=0.03)

        monkeypatch.setattr(database, "_flush_buffer_before_close", final_flush)

        class LogProducer:
            name = "log producer"
            init_successful = True

            def __init__(self, config, log_queue):
                self.config = config

            async def task_loop(self, queue):
                state["queue"] = queue
                while not queue.full():
                    queue.put_nowait(marker)
                queue_full.set()
                await queue.put(marker)

            async def drain(self, queue):
                pass

        lua = SimpleNamespace(config=box.config, name="Lua", init_successful=True, close=Mock(), request_shutdown=Mock())
        monkeypatch.setattr(app_main, "FritzBoxHandler", lambda config: box)
        monkeypatch.setattr(app_main, "FritzBoxLuaHandler", lambda config: lua)
        monkeypatch.setattr(app_main, "InfluxHandler", lambda *args, **kwargs: database)
        monkeypatch.setattr(app_main, "InfluxLogAndConfigWriter", LogProducer)

        async def stop_when_stalled():
            await asyncio.gather(box_started.wait(), write_started.wait(), queue_full.wait())
            assert state["queue"].full()
            callbacks[signal.SIGTERM]()

        trigger = asyncio.create_task(stop_when_stalled())
        try:
            result = await asyncio.wait_for(app_main.main_async(), timeout=2)
            assert result == 0
            await asyncio.wait_for(trigger, timeout=0.5)
            assert not release_box.is_set()
            assert state.get("final_flush") is True
            assert state["queue"].full()
            return result
        finally:
            release_box.set()
            trigger.cancel()
            await asyncio.gather(trigger, return_exceptions=True)

    try:
        with caplog.at_level(logging.WARNING, logger="fritzFlux"):
            assert asyncio.run(scenario()) == 0
        assert "Timed out waiting for in-flight Fritz!Box requests" in caplog.text
        assert "Timed out waiting for measurement queue to drain" in caplog.text
        assert "Timed out while flushing" in caplog.text
        assert "unwritten" in caplog.text
    finally:
        release_box.set()
        if state.get("box_started"):
            assert box_finished.wait(2), "simulated box worker did not stop"
