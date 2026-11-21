FROM python:3.14-slim AS base
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
# python:3.14-slim-trixie has not yet rebuilt with, so the Trivy release gate
# does not fail on CVEs Debian already fixed upstream.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends tini tzdata \
    && rm -rf /var/lib/apt/lists/*

# --- Python package (cached layer) ------------------------------------------
# pyproject.toml + app/ are installed as the "fritzfluxdb" distribution into
# site-packages - not also copied to /app/app - so the app exists exactly
# once in the image and app/utils/version.py reads the release version from the
# pyproject.toml copied into APP_HOME. README.md is required by pyproject.toml's
# readme= field. pip is removed once installed: the daemon never installs
# anything at runtime, and pip's bundled third-party copies (pip/_vendor) are
# the only source of CVE findings in this image. Dropping it also keeps the
# runtime surface smaller. Dependency consistency is verified here, at build
# time, while pip is still present.
# The gillbates-* packages come from tags of the private Gill-Bates/python-libraries
# repository: the token is a BuildKit secret (never ARG/ENV, so it stays out of layers
# and image history) handed to git through a credential helper that exists only for this
# command. git is installed and purged within this RUN, so the image carries neither git
# nor the token.
COPY pyproject.toml README.md ./
COPY app ./app
RUN --mount=type=secret,id=python_libraries_token,required=true \
    apt-get update \
    && apt-get install -y --no-install-recommends git \
    && GIT_TERMINAL_PROMPT=0 GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=credential.helper GIT_CONFIG_VALUE_0= \
       GIT_CONFIG_KEY_1=credential.helper \
       GIT_CONFIG_VALUE_1='!f() { echo username=x-access-token; echo "password=$(cat /run/secrets/python_libraries_token)"; }; f' \
       pip install --no-cache-dir . \
    && pip check \
    && python -c "import gillbates_logging, gillbates_questdb_client, gillbates_shutdown_handler" \
    && apt-get purge -y --auto-remove git \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall -y pip \
    && rm -rf ./app

# --- Entrypoint watchdog ----------------------------------------------------
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# --- Build-time sanity check + privilege drop (single layer) ---------------
# app/ now lives in site-packages (installed above), not under /app, so its
# path is resolved via the import system rather than assumed as /app/app.
RUN chmod 0555 /usr/local/bin/entrypoint.sh \
    && python -c "import app.main" \
    && python -m compileall -q "$(python -c 'import app, os; print(os.path.dirname(app.__file__))')" \
    && groupadd --gid 10001 fritzfluxdb \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin fritzfluxdb \
    && chmod -R a-w /app

# --- Greeting for interactive shells (docker exec -it ... bash|sh) ---------
# bash reads /etc/bash.bashrc, sh reads the file named by ENV; the daemon is
# unaffected because both only apply to interactive shells.
RUN printf '%s\n' 'case $- in *i*) printf "\nWelcome to FritzFluxDB - run fritzflux-cli --help for details.\n\n" ;; esac' \
      > /etc/fritzfluxdb-welcome.sh \
    && printf '%s\n' '. /etc/fritzfluxdb-welcome.sh' >> /etc/bash.bashrc
ENV ENV=/etc/fritzfluxdb-welcome.sh

# --- Watchdog tuning (overridable at runtime) ------------------------------
ENV WATCHDOG_RESTART_DELAY=10 \
    WATCHDOG_MAX_RESTART_DELAY=300 \
    WATCHDOG_MAX_RESTARTS=10 \
    WATCHDOG_BACKOFF_RESET_AFTER=3600 \
    WATCHDOG_SHUTDOWN_TIMEOUT=35

# --- Build-time metadata (populated from the CI build args) -----------------
# Declared last on purpose: every RUN after an ARG sees it as an environment
# variable, so a per-build value (VCS_REF, BUILD_DATE) declared earlier would
# invalidate the apt and pip layers on every build. BUILD_INFO is written here,
# inside the image, so no host file has to be generated and cleaned up; the
# banner reads it via app/utils/version.py.
ARG APP_VERSION="dev"
ARG VCS_REF="dev"
ARG BUILD_DATE=""
LABEL org.opencontainers.image.title="fritzFluxDB" \
      org.opencontainers.image.description="Tiny daemon fetching FritzBox data into InfluxDB or QuestDB" \
      org.opencontainers.image.source="https://github.com/Gill-Bates/fritzfluxdb" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="MIT"
RUN printf 'GIT_SHA=%s\nBUILD_DATE=%s\n' "${VCS_REF}" "${BUILD_DATE}" > /app/BUILD_INFO \
    && chmod 0444 /app/BUILD_INFO
USER 10001:10001
# tini as PID 1 -> entrypoint watchdog -> CMD.
# CMD is the default command the watchdog supervises; override it (e.g.
# `docker run <image> python -m app.main --help`) to run one-off commands.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "app.main"]
