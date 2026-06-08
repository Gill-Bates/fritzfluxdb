#!/usr/bin/env python3
#
# fritzfluxdb/cli_parser.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

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
        "-d",
        "--daemon",
        action="store_true",
        help="Run with daemon-oriented logging output",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase application log verbosity. Repeat for more detailed debug logs.",
    )

    args = parser.parse_args(argv)

    config_files = [
        config_file
        for group in args.config_file
        for config_file in group
    ]

    fixed_config_files: list[str] = []
    for config_file in config_files:
        path = Path(config_file).expanduser()
        if path != default_config_file and not path.is_absolute():
            path = Path.cwd() / path

        fixed_config_files.append(str(path.resolve()))

    args.config_file = fixed_config_files
    return args
