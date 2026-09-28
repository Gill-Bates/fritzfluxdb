#!/usr/bin/env bash
#
# docker/validate-version.sh
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# Validate the release version used by local builds and CI.

set -eu

VERSION="${1:-}"
if ! printf '%s\n' "${VERSION}" | grep -qEx '[0-9]+\.[0-9]+(\.[0-9]+)?(-[a-zA-Z0-9.]+)?'; then
    echo "ERROR: Invalid version format: '${VERSION}' (expected: X.Y, X.Y.Z, X.Y-suffix, or X.Y.Z-suffix)" >&2
    exit 1
fi
