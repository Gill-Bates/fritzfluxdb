FROM python:3.13-slim AS base
SHELL ["/bin/sh", "-eu", "-c"]

# --- Runtime environment ---------------------------------------------------
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Berlin \
    APP_HOME=/app

WORKDIR ${APP_HOME}

# upgrade first: picks up Debian security fixes already published for the
# base image's packages (e.g. perl-base, libsqlite3, gzip, libpcre2) that
# python:3.13-slim-trixie has not yet rebuilt with, so the Trivy release gate
# does not fail on CVEs Debian already fixed upstream.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends tini tzdata \
    && rm -rf /var/lib/apt/lists/*

# --- Python package (cached layer) ------------------------------------------
# pyproject.toml + app/ are installed as the "fritzfluxdb" distribution into
# site-packages - not also copied to /app/app - so the app exists exactly
# once in the image and app/version.py can read its own version back from
# the installed metadata (importlib.metadata) instead of parsing
# pyproject.toml again at runtime. README.md is required by pyproject.toml's
# readme= field. pip is removed once installed: the daemon never installs
# anything at runtime, and pip's bundled third-party copies (pip/_vendor) are
# the only source of CVE findings in this image. Dropping it also keeps the
# runtime surface smaller. Dependency consistency is verified here, at build
# time, while pip is still present.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir . \
    && pip check \
    && python -m pip uninstall -y pip \
    && rm -rf ./app

# --- Remaining application sources ------------------------------------------
# run.py + BUILD_INFO (BUILD_INFO* tolerates the file being absent; the banner
# falls back to a "dev" version in that case). Copied after the pip install so
# a change to either does not invalidate the dependency layer above.
COPY run.py BUILD_INFO* ./
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# --- Build-time sanity check + privilege drop (single layer) ---------------
# app/ now lives in site-packages (installed above), not under /app, so its
# path is resolved via the import system rather than assumed as /app/app.
RUN chmod 0555 /usr/local/bin/entrypoint.sh \
    && python -c "import app.main" \
    && python -m compileall -q /app/run.py "$(python -c 'import app, os; print(os.path.dirname(app.__file__))')" \
    && groupadd --gid 10001 fritzfluxdb \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin fritzfluxdb \
    && chmod -R a-w /app
USER 10001:10001

# --- Watchdog tuning (overridable at runtime) ------------------------------
ENV WATCHDOG_RESTART_DELAY=10 \
    WATCHDOG_MAX_RESTART_DELAY=300 \
    WATCHDOG_MAX_RESTARTS=10 \
    WATCHDOG_BACKOFF_RESET_AFTER=3600 \
    WATCHDOG_SHUTDOWN_TIMEOUT=15

# --- Build-time metadata (populated from the CI build args) -----------------
# Declared last on purpose: every RUN after an ARG sees it as an environment
# variable, so a per-build value (GIT_SHA, BUILD_DATE) declared earlier would
# invalidate the apt and pip layers on every build.
ARG APP_VERSION="unknown"
ARG GIT_SHA="unknown"
ARG BUILD_DATE="unknown"

LABEL org.opencontainers.image.title="fritzFluxDB" \
      org.opencontainers.image.description="Tiny daemon fetching FritzBox data into InfluxDB" \
      org.opencontainers.image.source="https://github.com/Gill-Bates/fritzfluxdb" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="MIT"

# tini as PID 1 -> entrypoint watchdog -> CMD.
# CMD is the default command the watchdog supervises; override it (e.g.
# `docker run <image> python /app/run.py --help`) to run one-off commands.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python", "/app/run.py"]