#!/usr/bin/env python3
#
# fritzfluxdb/main.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# -*- coding: utf-8 -*-

import re
import os
import signal
import asyncio
import sys
from pathlib import Path

from fritzfluxdb.cli_parser import parse_command_line
from fritzfluxdb.log import setup_logging
from fritzfluxdb.configparser import import_config
from fritzfluxdb.classes.fritzbox.handler import FritzBoxHandler, FritzBoxLuaHandler
from fritzfluxdb.classes.influxdb.handler import InfluxHandler, InfluxLogAndConfigWriter
from fritzfluxdb.classes.fritzbox.banner import print_banner
from fritzfluxdb.version import DESCRIPTION, URL, VERSION, VERSION_DATE

default_config = str(Path(__file__).with_name('fritzFlux.ini'))

async def main_async() -> int:
    if sys.version_info < (3, 13):
        print("Error: Python version 3.13 or higher required!", file=sys.stderr)
        return 1

    args = parse_command_line(VERSION, DESCRIPTION, VERSION_DATE, URL, default_config)

    log_queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
    effective_log_level = "DEBUG" if args.verbose > 0 else args.log_level
    log = setup_logging(effective_log_level, args.daemon, log_queue)
    log.propagate = False
    log.info(f"Starting {DESCRIPTION} v{VERSION} ({VERSION_DATE})")

    config = import_config(args.config_file, default_config)

    if args.verbose >= 2:
        log.warning("Verbose HTTP debugging is disabled to avoid leaking credentials or tokens")

    influx_connection = InfluxHandler(config, user_agent=f"{DESCRIPTION}/{VERSION}")
    fritzbox_connection = FritzBoxHandler(config)
    fritzbox_lua_connection = FritzBoxLuaHandler(fritzbox_connection.config)
    influx_log_writer = InfluxLogAndConfigWriter(fritzbox_connection.config, log_queue)

    handler_list = [
        influx_connection,
        fritzbox_connection,
        fritzbox_lua_connection,
        influx_log_writer
    ]

    for handler in handler_list:
        if getattr(handler.config, 'parser_error', False):
            # EX_CONFIG: permanent configuration error, restarts won't fix it
            return 78

    log.info("Successfully parsed config")

    queue: asyncio.Queue = asyncio.Queue(maxsize=100_000)
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def signal_handler(sig):
        log.info(f"Received exit signal {sig.name}...")
        shutdown_event.set()

    try:
        for fb_signal in [signal.SIGHUP, signal.SIGTERM, signal.SIGINT]:
            loop.add_signal_handler(fb_signal, lambda s=fb_signal: signal_handler(s))
    except (NotImplementedError, RuntimeError) as exc:
        log.error("Unable to register POSIX signal handlers: %s", exc)
        return 1

    log.info("Starting main loop")

    exit_code = 0
    tasks: list[asyncio.Task] = []

    try:
        fritzbox_connection.connect()

        fw_version = fritzbox_connection.config.fw_version
        try:
            fw_major = int(str(fw_version).split(".", maxsplit=1)[0])
        except (TypeError, ValueError):
            fw_major = 0

        if fw_major >= 7:
            fritzbox_lua_connection.connect()
        else:
            log.info("Disabling queries via Lua. Fritz!OS version must be at least 7.XX")
            handler_list.remove(fritzbox_lua_connection)

        serial = fritzbox_connection.config.serial_number
        if serial:
            safe_serial = "fritzbox_" + re.sub(r"[^A-Za-z0-9_]", "_", serial)
            influx_connection.config.measurement_name = safe_serial
            if influx_connection.version == "questdb":
                log.info("Using QuestDB table '%s'", safe_serial)
            else:
                log.info("Using InfluxDB measurement '%s'", safe_serial)
        else:
            if influx_connection.version == "questdb":
                log.warning("FritzBox serial number unavailable, using default table name")
            else:
                log.warning("FritzBox serial number unavailable, using default measurement name")

        init_errors = False
        for handler in handler_list:
            if handler is influx_connection:
                continue
            if not handler.init_successful:
                log.error(f"Initializing connection to {handler.name} failed")
                init_errors = True

        if init_errors:
            exit_code = 1
            return exit_code

        log.info(f"Successfully connected to "
                 f"FritzBox '{fritzbox_connection.config.hostname}' ({fritzbox_connection.config.box_tag}) "
                 f"Model: {fritzbox_connection.config.model} ({fritzbox_connection.config.link_type}) - "
                 f"FW: {fritzbox_connection.config.fw_version}")

        # Split consumer (influx) from producers for ordered graceful shutdown
        producer_tasks: list[asyncio.Task] = []
        influx_task: asyncio.Task | None = None

        for handler in handler_list:
            task = asyncio.create_task(
                handler.task_loop(queue),
                name=f"{handler.name}.task_loop",
            )
            if handler is influx_connection:
                influx_task = task
            else:
                producer_tasks.append(task)
            tasks.append(task)

        shutdown_task = asyncio.create_task(shutdown_event.wait(), name="shutdown.wait")

        done, pending = await asyncio.wait(
            [*tasks, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        if shutdown_task in done:
            # Graceful ordered shutdown: stop producers → drain queue → stop writer
            for task in producer_tasks:
                task.cancel()
            await asyncio.gather(*producer_tasks, return_exceptions=True)

            try:
                async with asyncio.timeout(15):
                    await queue.join()
            except TimeoutError:
                log.warning("Timed out waiting for measurement queue to drain; some data may be lost")

            if influx_task is not None:
                influx_task.cancel()
                await asyncio.gather(influx_task, return_exceptions=True)
        else:
            # Unexpected background task completion — treat as failure
            exit_code = 1
            for task in done:
                if task.cancelled():
                    log.error("Background task '%s' was cancelled unexpectedly", task.get_name())
                    continue
                exc = task.exception()
                if exc is None:
                    log.error("Background task '%s' exited unexpectedly", task.get_name())
                else:
                    log.exception("Background task '%s' failed", task.get_name(), exc_info=exc)

            log.info("Cancelling outstanding tasks...")
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    except Exception as e:
        exit_code = 1
        log.exception(f"Exception in main loop: {e}")
    finally:
        fritzbox_connection.close()
        fritzbox_lua_connection.close()
        await influx_connection.close()
        if exit_code == 0:
            log.info(f"Successfully shutdown {DESCRIPTION}")
        else:
            log.info(f"Shutdown completed after failure for {DESCRIPTION}")

    return exit_code

def main() -> None:
    if not os.environ.get("FRITZFLUXDB_SKIP_BANNER"):
        print_banner()
    try:
        raise SystemExit(asyncio.run(main_async()))
    except KeyboardInterrupt:
        raise SystemExit(0)

if __name__ == "__main__":
    main()
