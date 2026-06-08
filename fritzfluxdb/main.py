#!/usr/bin/env python3
#
# fritzfluxdb/main.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# -*- coding: utf-8 -*-

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
    log = setup_logging("DEBUG" if args.verbose > 0 else "INFO", args.daemon, log_queue)
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
            return 1

    log.info("Successfully parsed config")

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

    init_errors = False
    for handler in handler_list:
        if handler is influx_connection:
            continue
        if not handler.init_successful:
            log.error(f"Initializing connection to {handler.name} failed")
            init_errors = True

    if init_errors:
        return 1

    log.info(f"Successfully connected to "
             f"FritzBox '{fritzbox_connection.config.hostname}' ({fritzbox_connection.config.box_tag}) "
             f"Model: {fritzbox_connection.config.model} ({fritzbox_connection.config.link_type}) - "
             f"FW: {fritzbox_connection.config.fw_version}")

    queue: asyncio.Queue = asyncio.Queue(maxsize=100_000)
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def signal_handler(sig):
        log.info(f"Received exit signal {sig.name}...")
        shutdown_event.set()

    for fb_signal in [signal.SIGHUP, signal.SIGTERM, signal.SIGINT]:
        loop.add_signal_handler(fb_signal, lambda s=fb_signal: signal_handler(s))

    log.info("Starting main loop")
    
    tasks: list[asyncio.Task] = []
    
    try:
        for handler in handler_list:
            tasks.append(asyncio.create_task(handler.task_loop(queue)))
            
        # Wait until shutdown is triggered
        await shutdown_event.wait()
        log.info("Cancelling outstanding tasks...")
        for task in tasks:
            task.cancel()
            
        await asyncio.gather(*tasks, return_exceptions=True)
            
    except Exception as e:
        log.exception(f"Exception in main loop: {e}")
        return 1
    finally:
        fritzbox_connection.close()
        fritzbox_lua_connection.close()
        await influx_connection.close()
        log.info(f"Successfully shutdown {DESCRIPTION}")
        
    return 0

def main() -> None:
    print_banner()
    try:
        raise SystemExit(asyncio.run(main_async()))
    except KeyboardInterrupt:
        raise SystemExit(0)

if __name__ == "__main__":
    main()
