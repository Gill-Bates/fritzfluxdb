#!/usr/bin/env python3
#
# app/main.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
import logging
import os
import re
import signal
import sys
from collections.abc import Callable
from pathlib import Path

from app.fritzbox.banner import print_banner
from app.fritzbox.handler import FritzBoxHandler, FritzBoxLuaHandler
from app.influxdb.handler import (
    DatabaseConfigError,
    InfluxHandler,
    InfluxLogAndConfigWriter,
    questdb_events_table_name,
    questdb_metrics_table_name,
)
from app.utils.cli_parser import parse_command_line
from app.utils.common import PermanentConfigError
from app.utils.configparser import import_config
from app.utils.log import get_logger, setup_logging
from app.utils.version import DESCRIPTION, URL, VERSION, VERSION_DATE

default_config = str(Path(__file__).with_name('fritzFlux.ini'))

# Requests, queue drain, flush and reserve must fit inside the watchdog timeout,
# below Compose's stop grace period (see tests/test_docker_contract.py).
QUEUE_DRAIN_TIMEOUT = 10
SHUTDOWN_RESERVE = 5
# How long a signal received during a blocking connect waits for that connect to finish.
INIT_ABORT_TIMEOUT = 3


def _credentials_rejected(log: logging.Logger) -> int:
    """Log the permanent credential rejection and return its exit code (78)."""
    log.error("FritzBox rejected the configured credentials; not retrying")
    return 78


def create_wan_watch_task(handler: FritzBoxHandler, queue: asyncio.Queue) -> asyncio.Task | None:
    """Own task for the fast WAN poll; a producer like the handler loops, off when the interval is 0."""
    if handler.config.wan_watch_interval <= 0:
        return None
    return asyncio.create_task(handler.wan_watch_loop(queue), name="FritzBox WAN watch")


def _log_finished_workers(log: logging.Logger, done: set[asyncio.Task], shutdown_task: asyncio.Task) -> bool:
    """Log every worker that finished and return True if there was at least one.

    A worker only finishes on its own through a failure, so this is evaluated even when the
    shutdown signal arrived in the same asyncio.wait() round.
    """
    finished = [task for task in done if task is not shutdown_task]
    for task in finished:
        if task.cancelled():
            log.error("Background task '%s' was cancelled unexpectedly", task.get_name())
            continue
        exc = task.exception()
        if exc is None:
            log.error("Background task '%s' exited unexpectedly", task.get_name())
        else:
            log.error("Background task '%s' failed", task.get_name(), exc_info=exc)
    return bool(finished)


async def _connect_unless_stopped(connect: Callable[[], None], shutdown_event: asyncio.Event) -> bool:
    """Run a blocking connect in a thread; return False if a shutdown signal arrived first.

    Task.cancel() cannot stop a running thread. The watchdog owns the hard process deadline;
    request timeouts allow normal executor shutdown to finish without a forced Python exit.
    """
    work = asyncio.ensure_future(asyncio.to_thread(connect))
    stop = asyncio.ensure_future(shutdown_event.wait())
    try:
        await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
        if not work.done():
            await asyncio.wait({work}, timeout=INIT_ABORT_TIMEOUT)
    finally:
        stop.cancel()
    if not work.done():
        # A late failure of the abandoned thread must not surface as an unretrieved exception.
        work.add_done_callback(lambda fut: fut.cancelled() or fut.exception())
        return False
    work.result()
    return not shutdown_event.is_set()


async def main_async() -> int:
    if sys.version_info < (3, 13):  # noqa: UP036 -- guards against running on an older interpreter despite the declared target-version
        print("Error: Python version 3.13 or higher required!", file=sys.stderr)
        return 1

    args = parse_command_line(VERSION, DESCRIPTION, VERSION_DATE, URL, default_config)

    log_queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
    log = setup_logging(args.log_level, log_queue)
    if args.verbose > 0:
        log.setLevel("DEBUG")
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

    try:
        await influx_connection.preflight()
    except DatabaseConfigError as exc:
        log.error("%s", exc)
        return 78

    queue: asyncio.Queue = asyncio.Queue(maxsize=100_000)
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def signal_handler(sig):
        log.info(f"Received exit signal {sig.name}...")
        shutdown_event.set()
        # Login chains in worker threads stop at their next request, not after the whole fallback.
        fritzbox_connection.request_shutdown()
        fritzbox_lua_connection.request_shutdown()

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
        # connect() blocks on network I/O; keep the loop free so SIGTERM is handled promptly
        if not await _connect_unless_stopped(fritzbox_connection.connect, shutdown_event):
            log.info("Shutdown requested during startup; skipping further initialization")
            return exit_code
        if fritzbox_connection.access_disabled:
            exit_code = 78
            return exit_code
        if fritzbox_connection.auth_failed:
            exit_code = _credentials_rejected(log)
            return exit_code

        fw_version = fritzbox_connection.config.fw_version
        try:
            fw_major = int(str(fw_version).split(".", maxsplit=1)[0])
        except (TypeError, ValueError):
            fw_major = 0

        if fw_major >= 7:
            if not await _connect_unless_stopped(fritzbox_lua_connection.connect, shutdown_event):
                log.info("Shutdown requested during startup; skipping further initialization")
                return exit_code
            if fritzbox_lua_connection.auth_failed:
                exit_code = _credentials_rejected(log)
                return exit_code
        else:
            log.info("Disabling queries via Lua. Fritz!OS version must be at least 7.XX")
            handler_list.remove(fritzbox_lua_connection)

        serial = fritzbox_connection.config.serial_number
        if serial:
            safe_serial = "fritzbox_" + re.sub(r"[^A-Za-z0-9_]", "_", serial)
            influx_connection.config = influx_connection.config.model_copy(update={"measurement_name": safe_serial})
            if influx_connection.version == "questdb":
                log.info("Using QuestDB tables '%s' and '%s'",
                         questdb_metrics_table_name(safe_serial), questdb_events_table_name(safe_serial))
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
        watch_task = create_wan_watch_task(fritzbox_connection, queue)
        if watch_task is not None:
            producer_tasks.append(watch_task)
            tasks.append(watch_task)

        shutdown_task = asyncio.create_task(shutdown_event.wait(), name="shutdown.wait")

        done, pending = await asyncio.wait(
            [*tasks, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # A worker that ended on its own is a failure, also when the shutdown signal
        # completed in the same round.
        if _log_finished_workers(log, done, shutdown_task):
            exit_code = 1
            if any(t.done() and not t.cancelled() and isinstance(t.exception(), PermanentConfigError) for t in done):
                exit_code = 78

        if shutdown_task in done:
            for task in producer_tasks:
                task.cancel()
            await asyncio.gather(*producer_tasks, return_exceptions=True)

            # Nobody consumes the queue once the writer is gone, so draining it would only wait.
            if influx_task is not None and not influx_task.done():
                try:
                    async with asyncio.timeout(QUEUE_DRAIN_TIMEOUT):
                        # The log writer was cancelled with the producers; hand over what it left.
                        await influx_log_writer.drain(queue)
                        # A writer that dies during the drain never empties the queue; do not wait for it.
                        join_task = asyncio.create_task(queue.join())
                        try:
                            await asyncio.wait([join_task, influx_task], return_when=asyncio.FIRST_COMPLETED)
                        finally:
                            join_task.cancel()
                except TimeoutError:
                    log.warning("Timed out waiting for measurement queue to drain; some data may be lost")

            if influx_task is not None:
                influx_task.cancel()
                await asyncio.gather(influx_task, return_exceptions=True)
                # A writer that ended before the signal is already reported above.
                if influx_task not in done and not influx_task.cancelled():
                    writer_error = influx_task.exception()
                    if isinstance(writer_error, PermanentConfigError):
                        log.error("%s", writer_error)
                        exit_code = 78
                    elif writer_error is not None:
                        log.error("Background task '%s' failed", influx_task.get_name(), exc_info=writer_error)
                        exit_code = 1
        else:
            log.info("Cancelling outstanding tasks...")
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    except Exception:
        exit_code = 1
        log.exception("Exception in main loop")
    finally:
        await _close_connections(fritzbox_connection, fritzbox_lua_connection, influx_connection)
        if exit_code == 0:
            log.info(f"Successfully shutdown {DESCRIPTION}")
        else:
            log.info(f"Shutdown completed after failure for {DESCRIPTION}")

    return exit_code


async def _close_connections(
    fritzbox_connection: FritzBoxHandler,
    fritzbox_lua_connection: FritzBoxLuaHandler,
    influx_connection: InfluxHandler,
) -> None:
    """Close every connection; a failing close is logged and never skips the others."""
    log = get_logger()
    fritzbox_connection.shutting_down = True
    fritzbox_lua_connection.shutting_down = True
    for label, closer in (
        ("FritzBox TR-064", fritzbox_connection.close),
        ("FritzBox Lua", fritzbox_lua_connection.close),
    ):
        try:
            closer()
        except Exception:
            log.exception("Closing the %s connection failed", label)
    try:
        await influx_connection.close()
    except Exception:
        log.exception("Closing the database connection failed")


def main() -> None:
    if not os.environ.get("FRITZFLUXDB_SKIP_BANNER"):
        print_banner()
    try:
        code = asyncio.run(main_async())
    except KeyboardInterrupt:
        code = 0
    raise SystemExit(code)

if __name__ == "__main__":
    main()
