#!/usr/bin/env python3
#
# app/utils/log.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
import logging
from logging.handlers import QueueHandler

from gillbates_logging import LineLog

from app.utils.common import do_error_exit

# Log levels accepted by setup_logging() and the --log-level option.
valid_log_levels = ["DEBUG", "INFO", "WARNING", "ERROR"]

LOG_SOURCE = "fritzFlux"
# Line format, stdout/stderr split and sanitising come from the shared logging package.
LINE_LOG = LineLog(LOG_SOURCE)


class DroppingQueueHandler(QueueHandler):
    """Queue handler that never blocks and hands records to the loop thread-safely."""

    def __init__(self, queue, loop=None):
        super().__init__(queue)
        self.loop = loop

    def _put(self, record):
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            pass

    def enqueue(self, record):
        # asyncio.Queue is not thread-safe: records from worker threads go through the loop.
        loop = self.loop
        if loop is None or loop.is_closed():
            self._put(record)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._put(record)
            return
        try:
            loop.call_soon_threadsafe(self._put, record)
        except RuntimeError:
            pass  # loop closed while shutting down


def get_logger():
    """
    Return the shared project logger, for use in every module

    Returns
    -------
    logging.Logger
    """

    return logging.getLogger(LOG_SOURCE)


def setup_logging(log_level=None, log_queue=None):
    """
    Set up logging for the whole program and return the project logger

    Parameters
    ----------
    log_level: str
        valid log level to set logging to
    log_queue: asyncio.Queue
        queue object to write logs to which should be sent to the database
        (InfluxDB or QuestDB)

    Returns
    -------
    logging.Logger
        the configured project logger
    """

    if log_level is None or log_level == "":
        do_error_exit("log level undefined or empty. Check config please.")

    if log_level.upper() not in valid_log_levels:
        do_error_exit(f"Invalid log level: {log_level}")

    numeric_log_level = getattr(logging, log_level.upper(), None)

    # Console output is a root handler installed once by the package; the project logger propagates to it.
    LINE_LOG.setup(log_level)

    logger = get_logger()
    logger.handlers.clear()
    logger.setLevel(numeric_log_level)
    logger.propagate = True
    # httpx logs full request URLs at INFO, and the Lua session id travels in the query string.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if log_queue is not None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        queue_handler = DroppingQueueHandler(log_queue, loop)
        queue_handler.setLevel(logging.INFO)
        logger.addHandler(queue_handler)

    return logger
