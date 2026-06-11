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

# Verify required commands before performing any side effects
for command in docker git date; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "ERROR: required command not found: ${command}" >&2
        exit 1
    fi
done

# Resolve repo root relative to this script (docker/ lives under the root).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUILD_INFO="${REPO_ROOT}/BUILD_INFO"

# File locking to serialize local builds
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
    # Release the file descriptor and remove the lock file
    exec 9>&- || true
    rm -f "${LOCK_FILE}" || true
}

trap cleanup_build_info EXIT

# Always (re)generate BUILD_INFO so GIT_SHA / BUILD_DATE are current and the
# version stays in sync with the VERSION file (the single source of truth,
# same as the CI workflow). APP_VERSION never falls back to a stale value.
VERSION_FILE="${REPO_ROOT}/VERSION"
if [ ! -f "${VERSION_FILE}" ]; then
    echo "ERROR: VERSION file not found at ${VERSION_FILE}" >&2
    exit 1
fi

# Read single line, trim CR, and validate version strictly (without silent aggressive truncation)
if ! IFS= read -r APP_VERSION < "${VERSION_FILE}" && [ -z "${APP_VERSION}" ]; then
    echo "ERROR: VERSION file is empty or cannot be read" >&2
    exit 1
fi
APP_VERSION="${APP_VERSION%$'\r'}"

case "${APP_VERSION}" in
    ""|*[[:space:]]*)
        echo "ERROR: VERSION must contain a single non-empty version without whitespace" >&2
        exit 1
        ;;
esac

case "${APP_VERSION}" in
    *[!0-9A-Za-z.+_-]*)
        echo "ERROR: VERSION contains unsupported characters: ${APP_VERSION}" >&2
        exit 1
        ;;
esac

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

# Record the existing image ID for this tag (if any) using inspect to clean it up after the build.
OLD_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "${IMAGE}:testing" 2>/dev/null || true)"

echo "Building ${IMAGE}:testing" >&2
echo "  APP_VERSION=${APP_VERSION} GIT_SHA=${GIT_SHA} BUILD_DATE=${BUILD_DATE}" >&2

# "--build-arg NAME" (without =value) forwards NAME from the environment.
docker build \
    --pull \
    --build-arg APP_VERSION \
    --build-arg GIT_SHA \
    --build-arg BUILD_DATE \
    -f "${SCRIPT_DIR}/Dockerfile" \
    -t "${IMAGE}:testing" \
    "$@" \
    "${REPO_ROOT}"

# Remove the old/dangling image if a new image was built successfully and its ID changed.
if [ -n "${OLD_IMAGE_ID}" ]; then
    NEW_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "${IMAGE}:testing" 2>/dev/null || true)"
    if [ -n "${NEW_IMAGE_ID}" ] && [ "${OLD_IMAGE_ID}" != "${NEW_IMAGE_ID}" ]; then
        echo "Removing old image ${OLD_IMAGE_ID} to avoid leaving dangling images..." >&2
        docker rmi "${OLD_IMAGE_ID}" || true
    fi
fi

# Optional and explicit/safe image pruning of dangling fritzFluxDB builder/images
case "${PRUNE:-0}" in
    1|true|yes)
        echo "Pruning dangling images with title label fritzFluxDB..." >&2
        docker image prune -f \
            --filter "dangling=true" \
            --filter "label=org.opencontainers.image.title=fritzFluxDB"
        ;;
    0|false|no)
        ;;
    *)
        echo "ERROR: PRUNE must be one of 1,true,yes,0,false,no" >&2
        exit 1
        ;;
esac

# Push to the registry by default (preserving direct push behavior); opt out with PUSH=0 (false/no).
case "${PUSH:-1}" in
    1|true|yes)
        echo "Pushing ${IMAGE}:testing ..." >&2
        docker push "${IMAGE}:testing"
        ;;
    0|false|no)
        echo "Skipping registry push (PUSH=${PUSH})." >&2
        ;;
    *)
        echo "ERROR: invalid PUSH value: ${PUSH}" >&2
        exit 1
        ;;
esac
