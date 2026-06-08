#!/usr/bin/env python3
#
# fritzfluxdb/classes/fritzbox/banner.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Startup banner for fritzfluxdb."""

from __future__ import annotations

import sys

from fritzfluxdb.version import AUTHOR, BUILD_DATE, GIT_SHA, URL, VERSION


def print_banner() -> None:
    """Print the fritzfluxdb startup banner to stdout.

    Version, git sha and build date come from BUILD_INFO (via fritzfluxdb.version).
    Never raises: a cosmetic banner must not be able to take the daemon down.
    """
    try:
        sha_short = GIT_SHA[:7] if GIT_SHA else ""
        built_day = BUILD_DATE.split("T", 1)[0] if BUILD_DATE else ""

        ascii_art = r"""
  __      _ _        __ _               _ _
 / _|_ __(_) |_ ____/ _| |_   ___  ____| | |__
| |_| '__| | __|_  / |_| | | | \ \/ / _` | '_ \
|  _| |  | | |_ / /|  _| | |_| |>  < (_| | |_) |
|_| |_|  |_|\__/___|_| |_|\__,_/_/\_\__,_|_.__/
""".strip("\n")

        version_line = f"v{VERSION}"
        if sha_short:
            version_line += f" ({sha_short})"
        if built_day:
            version_line += f"  -  built {built_day}"

        text_lines = [
            version_line,
            f"(C) 2026 {AUTHOR} - {URL}",
        ]

        ascii_lines = ascii_art.splitlines()
        ascii_width = max((len(line) for line in ascii_lines), default=0)
        text_width = max((len(t) for t in text_lines), default=0)
        master_width = max(ascii_width, text_width)

        left_pad = max((master_width - ascii_width) // 2, 0)
        pad = " " * left_pad
        ascii_centered = "\n".join(pad + line for line in ascii_lines)
        text_centered = [t.center(master_width) for t in text_lines]

        banner = "\n" + "\n".join([ascii_centered, *text_centered]) + "\n"

        # Colourise only when attached to a terminal; plain text in logs/pipes.
        if sys.stdout.isatty():
            cyan, reset = "\033[96m", "\033[0m"
            sys.stdout.write(cyan + banner + reset + "\n")
        else:
            sys.stdout.write(banner + "\n")

        sys.stdout.flush()
    except Exception:
        # Output glitches (closed stdout, encoding, ...) must not break startup.
        pass
