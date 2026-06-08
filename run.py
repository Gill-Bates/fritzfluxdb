#!/usr/bin/env python3
#
# run.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# -*- coding: utf-8 -*-

import sys

# Add the current directory to the Python path,
# so that the fritzFlux module can be found.
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv
    # Load variables from .env if present
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
except ImportError:
    pass

from fritzfluxdb.main import main

if __name__ == "__main__":
    main()
