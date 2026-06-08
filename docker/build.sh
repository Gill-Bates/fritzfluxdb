#!/usr/bin/env bash
#
# docker/build.sh
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# =============================================================================
# fritzFlux - local image build
# =============================================================================
# Builds the container image and feeds the OCI metadata (version / git sha /
# build date) from the repo-root BUILD_INFO file into the Dockerfile ARGs, so
# a local build produces correctly labelled images instead of "unknown".
#
# BUILD_INFO is a plain KEY=VALUE file:
#   APP_VERSION=1.2.4
#   GIT_SHA=<sha>
#   BUILD_DATE=<iso-8601>
#
# By default the freshly built image is PUSHED to the registry (requires
# `docker login`). Skip the push with PUSH=0.
#
# Usage (from anywhere):
#   docker/build.sh [extra docker build args...]   # build + push (default)
#   PUSH=0 docker/build.sh                          # build only, no push
#   IMAGE=my.reg/repo docker/build.sh              # build+push to another repo
# =============================================================================

set -eu

# Resolve repo root relative to this script (docker/ lives under the root).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUILD_INFO="${REPO_ROOT}/BUILD_INFO"

# Always (re)generate BUILD_INFO so GIT_SHA / BUILD_DATE are current and the
# version stays in sync with the VERSION file (the single source of truth,
# same as the CI workflow). APP_VERSION never falls back to a stale value.
VERSION_FILE="${REPO_ROOT}/VERSION"
if [ ! -f "${VERSION_FILE}" ]; then
    echo "ERROR: VERSION file not found at ${VERSION_FILE}" >&2
    exit 1
fi

# Strip all whitespace (trailing newline, CRLF on Windows, stray spaces).
APP_VERSION="$(tr -d '[:space:]' < "${VERSION_FILE}")"
GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)"
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

{
    printf 'APP_VERSION=%s\n' "${APP_VERSION}"
    printf 'GIT_SHA=%s\n' "${GIT_SHA}"
    printf 'BUILD_DATE=%s\n' "${BUILD_DATE}"
} > "${BUILD_INFO}"

export APP_VERSION GIT_SHA BUILD_DATE

# Fully-qualified image name (registry/repo). Override with IMAGE=... to build
# for a different registry. Defaults to the private registry used for deploys.
IMAGE="${IMAGE:-giiibates/fritzfluxdb}"

echo "Building ${IMAGE}:${APP_VERSION} (+ :latest)" >&2
echo "  APP_VERSION=${APP_VERSION} GIT_SHA=${GIT_SHA} BUILD_DATE=${BUILD_DATE}" >&2

# "--build-arg NAME" (without =value) forwards NAME from the environment.
docker build \
    --build-arg APP_VERSION \
    --build-arg GIT_SHA \
    --build-arg BUILD_DATE \
    -f "${SCRIPT_DIR}/Dockerfile" \
    -t "${IMAGE}:${APP_VERSION}" \
    -t "${IMAGE}:latest" \
    "$@" \
    "${REPO_ROOT}"

# Push to the registry by default; opt out with PUSH=0 (false/no).
case "${PUSH:-1}" in
    0|false|no)
        echo "Skipping registry push (PUSH=${PUSH:-})." >&2
        ;;
    *)
        echo "Pushing ${IMAGE}:${APP_VERSION} and ${IMAGE}:latest ..." >&2
        docker push "${IMAGE}:${APP_VERSION}"
        docker push "${IMAGE}:latest"
        ;;
esac
