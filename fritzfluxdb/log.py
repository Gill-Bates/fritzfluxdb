#!/usr/bin/env python3
#
# fritzfluxdb/log.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import logging
from logging.handlers import QueueHandler
import sys
import asyncio

from fritzfluxdb.common import do_error_exit

# define valid log levels
valid_log_levels = ["DEBUG", "INFO", "WARNING", "ERROR"]


class DroppingQueueHandler(QueueHandler):
    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            pass


def get_logger():
    """
    common function to retrieve common log handler in project files

    Returns
    -------
    log handler
    """

    return logging.getLogger("fritzFlux")


def setup_logging(log_level=None, run_as_daemon=False, log_queue=None):
    """
    Set up logging for the whole program and return a log handler

    Parameters
    ----------
    log_level: str
        valid log level to set logging to
    run_as_daemon: bool
        define if tool is running as daemon to omit log time stamp
    log_queue: asyncio.Queue
        queue object to write logs to which should be sent to InfluxDB

    Returns
    -------
    log handler to use for logging
    """

    if log_level is None or log_level == "":
        do_error_exit("log level undefined or empty. Check config please.")

    # check set log level against self defined log level array
    if log_level.upper() not in valid_log_levels:
        do_error_exit(f"Invalid log level: {log_level}")

    numeric_log_level = getattr(logging, log_level.upper(), None)

    # Always prefix a timestamp: `docker logs` does not add one on its own, so
    # omitting it (even in daemon mode) would leave entries without any time.
    # run_as_daemon is kept for API compatibility but no longer hides the time.
    log_format = logging.Formatter(
        '%(asctime)s - %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    # create logger instance
    logger = get_logger()

    logger.setLevel(numeric_log_level)

    # add handler to write logs to stdout
    log_stream = logging.StreamHandler(sys.stdout)
    log_stream.setFormatter(log_format)
    logger.addHandler(log_stream)

    # add handler to write logs to InfluxDB log queue
    queue_handler = DroppingQueueHandler(log_queue)
    queue_handler.setLevel(logging.INFO)
    logger.addHandler(queue_handler)

    return logger

# EOF
