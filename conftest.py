#!/usr/bin/env python3
#
# conftest.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Make the repository root importable for the test suite.

`tests/` is not a package, so pytest only puts `tests/` itself on sys.path.
Running `python -m pytest` happens to work because that adds the working
directory, but the plain `pytest` entry point used by CI does not. An empty
conftest.py in the repository root makes pytest prepend this directory, so
`import fritzfluxdb` resolves either way.
"""
