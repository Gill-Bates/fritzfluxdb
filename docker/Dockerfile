FROM python:3.13-slim-trixie AS base
SHELL ["/bin/sh", "-eu", "-c"]

# --- Build-time metadata (populated from the CI build args) -----------------
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

# --- Runtime environment ---------------------------------------------------
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Berlin \
    APP_HOME=/app

WORKDIR ${APP_HOME}

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini tzdata \
    && rm -rf /var/lib/apt/lists/*

# --- Python dependencies (cached layer) ------------------------------------
COPY requirements.txt ./
RUN pip install -r requirements.txt \
    && pip check

# --- Application sources ----------------------------------------------------
# run.py + BUILD_INFO (BUILD_INFO* tolerates the file being absent; the banner
# falls back to a "dev" version in that case).
COPY run.py BUILD_INFO* ./
COPY fritzfluxdb ./fritzfluxdb
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# --- Build-time sanity check + privilege drop (single layer) ---------------
RUN chmod 0555 /usr/local/bin/entrypoint.sh \
    && python -m compileall -q /app/run.py /app/fritzfluxdb \
    && python -c "import fritzfluxdb.main" \
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

# tini as PID 1 -> entrypoint watchdog -> CMD.
# CMD is the default command the watchdog supervises; override it (e.g.
# `docker run <image> python /app/run.py --help`) to run one-off commands.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python", "/app/run.py", "-d"]