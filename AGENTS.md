# AGENTS.md

House rules for anyone, human or agent, who changes this repository. This is the root-level contract for the application. Keep statements aligned with the actual code and workflows; update this file in the same commit whenever a stated contract changes.

## Repository contract

- **Single manifest:** `pyproject.toml` is the only source of truth for the application version and runtime/development dependencies. **Dependency version pins belong only in `pyproject.toml`**, where runtime and development dependencies use `==`. `requirements*.txt`, `VERSION`, and `ruff.toml` must not return (the CI `manifest` job rejects them).
- **Mandatory stack and checks:** **Ruff, pytest, and Pydantic are required.** Ruff checks the Python code, pytest runs the test suite, and Pydantic is the required validation/modeling dependency. Declare and pin the relevant dependencies exclusively in `pyproject.toml` (Ruff and pytest as development dependencies; Pydantic as a runtime dependency). Do not remove or bypass these requirements.
- **Python:** `requires-python = ">=3.13"` in `pyproject.toml`; CI and the Dockerfile use Python 3.13.
- **Version shape:** `X.Y.Z` with an optional prerelease suffix matching `^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$`. This contract is checked by the `manifest` job in `.github/workflows/ci.yml`. The release tag is `v` followed by the `pyproject.toml` version.
- **Layout:** `app/fritzbox/` (FRITZ!Box access), `app/influxdb/` (InfluxDB and QuestDB writing), `app/utils/` (CLI, configuration, logging, version, measurement model, `fritzflux-cli`), `app/main.py`. The repository-root `utils/` holds Grafana dashboards and a QuestDB server configuration example. Use absolute imports (`app.…`).
- **Build metadata:** The Dockerfile writes `/app/BUILD_INFO` (keys `GIT_SHA` and `BUILD_DATE`) from build arguments `VCS_REF` and `BUILD_DATE`; the version comes from `pyproject.toml`. No host-side `BUILD_INFO` is generated, and `.dockerignore` excludes a stray copy.
- **Exit codes:** `78` is a permanent configuration error, including a missing or invalid configuration file, an invalid log level, rejected FRITZ!Box credentials, TR-064 access switched off in the FRITZ!Box, credentials or permissions rejected by InfluxDB or QuestDB (HTTP 401/403, also while running), a missing InfluxDB database, organization or bucket (HTTP 404), and a QuestDB older than version 10 (the watchdog in `docker/entrypoint.sh` parks instead of restarting the daemon and exiting, so the restart policy does not loop; the healthcheck reports unhealthy); `1` is any other failure; an unreachable FRITZ!Box or database is never `78`. An external container restart policy may restart the container independently of the watchdog.

## Language

- Code comments, log messages, and console output: English; comments are at most two lines and explain why rather than what.
- Documentation (`README.md`, `docs/`, `CHANGELOG.md`): English.
- Communication with the maintainer in the IDE: German is fine.

## Dependencies and tooling

- **Latest is greatest:** Use the latest available stable version of external CI helpers and tools. **Do not add version pins, upper bounds, or dependency lock files outside `pyproject.toml`.** In particular, CI-only helper packages such as `psutil` in `.github/scripts/ci_job_setup.py` are installed unpinned. Application runtime and development dependency pins are maintained exclusively in `pyproject.toml`.
- **Supply-chain integrity controls are a deliberate exception to unpinned tool acquisition:** Third-party GitHub Actions reference a reviewed full commit SHA with a human-readable version comment; the Trivy action uses a reviewed immutable revision rather than `version: latest`; `actionlint` is obtained from a checksum-verified release; `zizmor` and the Trivy CLI are installed at an exact version set in the workflow (kept current by Renovate). These are verification/identity controls, not an additional package dependency manifest.
- Never invent an action SHA or a release checksum. Verify it against the publisher's authoritative release information before proposing a change.
- **Trivy scanning is mandatory.** CI must run source secret and configuration scans; image builds/releases must run the container vulnerability scan; the scheduled security workflow must rescan the published image. A missing, failed, disabled, or unverifiable scan must never be counted as a passing security gate. Apply the finding policy configured in CI; do not silently skip or weaken it.

## Checks before handing over

Run checks relevant to the change. Run the local checks below where the tools and inputs are available, together with manifest validation and the applicable mandatory Trivy scans from `ci.yml`:

```bash
python tools/pyproject-deps.py dev > /tmp/req-dev.txt && python tools/pyproject-deps.py > /tmp/req.txt
ruff check .          # Repo configuration and Ruff pin live in pyproject.toml
pytest -q -p no:xdist   # As in CI: one process, random order (pytest-randomly); an empty suite fails
bash -n docker/entrypoint.sh && shellcheck docker/entrypoint.sh
docker build --check -f docker/Dockerfile .
actionlint             # Reads .github/actionlint.yaml
zizmor --no-progress --format=github .github/workflows
```

- **Ruff and pytest are mandatory gates**, not optional advice. Only `ruff check .` is enforced; `ruff format` is not a gate. Add or update tests for behavior changes.
- **Trivy is also mandatory:** run the source and/or image scans applicable to the change using the commands configured in CI. A scan that cannot be performed must be reported as unverified, never marked successful. Do not substitute a made-up local command for the workflow's actual invocation.
- Create virtual environments and temporary files outside the repository; remove them afterwards.

## Pipeline

| Workflow | Role |
| --- | --- |
| `ci.yml` | Gate on branch pushes and PRs to `main`, also called by the release: `manifest`, `workflows` (actionlint), `zizmor`, `container`, `check` (matrix: `ruff` and `pytest` in parallel), `source-scan` (mandatory Trivy secret and misconfiguration scanning). |
| `docker-build.yml` | Tag `v*` or manual dispatch. `meta` → `lint-test` (calls `ci.yml`) → `build` (per architecture: smoke test, mandatory Trivy scan, push) → `create-manifest` (candidate tag) → `attest` → `promote` (version tag, `latest` only for the newest stable) → `cleanup` → `create-release`. |
| `docs-build.yml` | Zensical build (`mkdocs.yml` at the repository root), link check, dependency scan (Trivy), deploy to GitHub Pages from `main`; PRs build only. Also runs weekly and after a completed `docker-build.yml` run. |
| `security.yml` | Mandatory weekly Trivy rescan of the published `latest` image per platform digest. |

- Every job with `steps:` starts with checkout and then `python3 .github/scripts/ci_job_setup.py` (runner banner, temporary directories on `RUNNER_TEMP`). Jobs that call a reusable workflow use the called workflow's setup steps.
- No `${{ }}` expressions inside a `run:` or `script:` body: pass values through `env:`.
- Calling scripts live in `.github/scripts/` and `tools/` (`release_*.py`, `trivy_report.py`, `failure_issue.py`, `ci_output.py`). Logic involving branches or parsing belongs there, with tests, not inline in workflows.
- Never start a release from an agent: no tag pushes, manual `docker-build.yml` dispatch, or changes to repository secrets or environment settings without explicit maintainer authorization.

## Changelog

`CHANGELOG.md` lists user-visible changes per release, newest first, with short bullets that start with a group marker (New, Changed, Fix, or Security, in that order) and run one or two sentences from the end user's point of view. Refactoring, tests, and other internal changes are excluded.

## Git

- Work on the checked-out branch. Do not create a branch, worktree, or merge request unless the maintainer asks.
- Commit only when asked, with an English commit message based on the diff; never push unless asked.
- Do not add `Co-authored-by` trailers.
