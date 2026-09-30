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
# Requirements: bash, Python 3.11+ (tomllib), docker, git and flock (util-linux).
# Linux-only: flock is not available on stock macOS.
#
# Usage (from anywhere):
#   docker/build.sh [extra docker build args...]   # build + push (default)
#   PUSH=0 docker/build.sh                          # build only, no push
#   IMAGE=my.reg/repo docker/build.sh              # build+push to another repo
# =============================================================================

set -euo pipefail

# Verify required commands before performing any side effects
for command in docker git date python3 flock; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "ERROR: required command not found: ${command}" >&2
        exit 1
    fi
done

if ! python3 -c 'import tomllib' >/dev/null 2>&1; then
    echo "ERROR: Python 3.11+ with tomllib is required" >&2
    exit 1
fi

# Normalize a boolean option to 1/0; exits on invalid values.
parse_bool() {
    case "$2" in
        1|true|yes) echo 1 ;;
        0|false|no) echo 0 ;;
        *)
            echo "ERROR: $1 must be one of 1,true,yes,0,false,no (got: $2)" >&2
            exit 1
            ;;
    esac
}

# Validate options before any side effect (lock, BUILD_INFO, docker).
PUSH="$(parse_bool PUSH "${PUSH:-1}")"
PRUNE="$(parse_bool PRUNE "${PRUNE:-0}")"

# Resolve repo root relative to this script (docker/ lives under the root).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUILD_INFO="${REPO_ROOT}/BUILD_INFO"

# Dirty state must be captured before the BUILD_INFO backup below is created,
# since that temp file is untracked and not gitignored. BUILD_INFO and the lock
# file are gitignored and do not count.
GIT_DIRTY=0
if git -C "${REPO_ROOT}" rev-parse --git-dir >/dev/null 2>&1; then
    if ! git -C "${REPO_ROOT}" diff --quiet --ignore-submodules HEAD -- \
        || [ -n "$(git -C "${REPO_ROOT}" ls-files --others --exclude-standard)" ]; then
        GIT_DIRTY=1
    fi
else
    GIT_DIRTY=unknown
fi

# File locking to serialize local builds. The lock file is intentionally never
# deleted: flock locks the inode, so removing it would let two builds lock
# different inodes under the same name.
LOCK_FILE="${REPO_ROOT}/.docker-build.lock"
exec 9>"${LOCK_FILE}"

if ! flock -n 9; then
    echo "ERROR: another docker build is already running for this repository" >&2
    exit 1
fi

# Backup existing BUILD_INFO if present, to restore it on script exit
BUILD_INFO_BACKUP=""
if [ -e "${BUILD_INFO}" ]; then
    BUILD_INFO_BACKUP="$(mktemp "${REPO_ROOT}/.BUILD_INFO.backup.XXXXXX")"
    cp -p -- "${BUILD_INFO}" "${BUILD_INFO_BACKUP}"
fi

cleanup_build_info() {
    if [ -n "${BUILD_INFO_BACKUP}" ] && [ -e "${BUILD_INFO_BACKUP}" ]; then
        mv -f -- "${BUILD_INFO_BACKUP}" "${BUILD_INFO}" || true
    else
        rm -f -- "${BUILD_INFO}" || true
    fi
}

trap cleanup_build_info EXIT

# Always (re)generate BUILD_INFO so GIT_SHA / BUILD_DATE are current and the
# version stays in sync with pyproject.toml (the single source of truth,
# same as the CI workflow). APP_VERSION never falls back to a stale value.
PYPROJECT_FILE="${REPO_ROOT}/pyproject.toml"
if [ ! -f "${PYPROJECT_FILE}" ]; then
    echo "ERROR: pyproject.toml not found at ${PYPROJECT_FILE}" >&2
    exit 1
fi

if ! APP_VERSION="$(python3 -c '
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    data = tomllib.load(f)
version = data.get("project", {}).get("version")
if not isinstance(version, str) or not version:
    raise SystemExit(1)
print(version)
' "${PYPROJECT_FILE}")"; then
    echo "ERROR: could not read [project].version from ${PYPROJECT_FILE}" >&2
    exit 1
fi

bash "${SCRIPT_DIR}/validate-version.sh" "${APP_VERSION}"

GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)"
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

{
    printf 'APP_VERSION=%s\n' "${APP_VERSION}"
    printf 'GIT_SHA=%s\n' "${GIT_SHA}"
    printf 'BUILD_DATE=%s\n' "${BUILD_DATE}"
} > "${BUILD_INFO}"

export APP_VERSION GIT_SHA GIT_DIRTY BUILD_DATE

# Fully-qualified image name (registry/repo). Override with IMAGE=... to build
# for a different registry. Defaults to the private registry used for deploys.
IMAGE="${IMAGE:-giiibates/fritzfluxdb}"

# Record the existing image ID for this tag (if any) using inspect to clean it up after the build.
OLD_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "${IMAGE}:dev" 2>/dev/null || true)"

echo "Building ${IMAGE}:dev" >&2
echo "  APP_VERSION=${APP_VERSION} GIT_SHA=${GIT_SHA} GIT_DIRTY=${GIT_DIRTY} BUILD_DATE=${BUILD_DATE}" >&2

# "--build-arg NAME" (without =value) forwards NAME from the environment.
docker build \
    --pull \
    --build-arg APP_VERSION \
    --build-arg GIT_SHA \
    --build-arg GIT_DIRTY \
    --build-arg BUILD_DATE \
    -f "${SCRIPT_DIR}/Dockerfile" \
    -t "${IMAGE}:dev" \
    "$@" \
    "${REPO_ROOT}"

# Push first, so a failed push keeps the previous local :dev image.
# Opt out with PUSH=0 (false/no).
if [ "${PUSH}" = 1 ]; then
    echo "Pushing ${IMAGE}:dev ..." >&2
    docker push "${IMAGE}:dev"
else
    echo "Skipping registry push (PUSH=0)." >&2
fi

# Remove the previous image once the new one is built (and pushed).
if [ -n "${OLD_IMAGE_ID}" ]; then
    NEW_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "${IMAGE}:dev" 2>/dev/null || true)"
    if [ -n "${NEW_IMAGE_ID}" ] && [ "${OLD_IMAGE_ID}" != "${NEW_IMAGE_ID}" ]; then
        echo "Removing old image ${OLD_IMAGE_ID} to avoid leaving dangling images..." >&2
        docker rmi "${OLD_IMAGE_ID}" || true
    fi
fi

# Optional pruning. This only removes dangling images carrying the fritzFluxDB
# title label; it does not clean the BuildKit builder cache (docker builder prune).
if [ "${PRUNE}" = 1 ]; then
    echo "Pruning dangling images with title label fritzFluxDB..." >&2
    docker image prune -f \
        --filter "dangling=true" \
        --filter "label=org.opencontainers.image.title=fritzFluxDB"
fi
