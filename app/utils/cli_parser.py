#!/usr/bin/env python3
#
# app/utils/cli_parser.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import os
from argparse import ArgumentParser, Namespace, RawDescriptionHelpFormatter
from collections.abc import Sequence
from pathlib import Path


def parse_command_line(
    version: str,
    self_description: str,
    version_date: str,
    url: str,
    default_config_file_path: str,
    argv: Sequence[str] | None = None,
) -> Namespace:
    default_config_file = Path(default_config_file_path).expanduser().resolve()
    description = f"{self_description}\nVersion: {version} ({version_date})\nProject URL: {url}"

    parser = ArgumentParser(
        description=description,
        formatter_class=RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "-c",
        "--config",
        default=[],
        dest="config_file",
        nargs="+",
        action="append",
        help=(
            "Read config data from one or more config files instead of only "
            f"the default path '{default_config_file}'"
        ),
        metavar=default_config_file.name,
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase application log verbosity. Repeat for more detailed debug logs.",
    )

    parser.add_argument(
        "-l",
        "--log-level",
        dest="log_level",
        default=os.environ.get("LOG_LEVEL", "INFO").upper(),
        help="Set log level (default: INFO, or $LOG_LEVEL env var)",
    )

    args = parser.parse_args(argv)

    config_files = [
        config_file
        for group in args.config_file
        for config_file in group
    ]

    # resolve() anchors a relative path at the current working directory
    args.config_file = [str(Path(config_file).expanduser().resolve()) for config_file in config_files]
    return args
