# Testing and releases

## Tests and lint

Install the runtime, test, and lint dependencies from `pyproject.toml`, then run the focused or
complete test suite from the repository root:

```bash
python -m pip install '.[dev]'
pytest -q -p no:xdist
ruff check .
```

The Docker shell entrypoint can be syntax-checked without starting a container:

```bash
bash -n docker/entrypoint.sh
```

Dashboard JSON should remain valid after edits:

```bash
find utils/grafana -name '*.json' -print0 | xargs -0 -n1 jq empty
```

## Documentation validation

```bash
zensical build --config-file mkdocs.yml --strict --clean
python tools/docs_site_check.py
```

Every page in the navigation must exist. Keep environment examples credential-free and update
configuration pages whenever the environment contract changes.

## Release metadata

The release version is stored in `pyproject.toml`; the application reads it from there. A source
checkout has no `BUILD_INFO` file, so it reports no commit and no build date.

| Item | Value |
| --- | --- |
| Build args | `APP_VERSION` (image label only), `VCS_REF` (commit), `BUILD_DATE` (UTC, ISO 8601) |
| `/app/BUILD_INFO` | Written by the Dockerfile with the keys `GIT_SHA` (value of `VCS_REF`) and `BUILD_DATE`; read at runtime for the banner. |
| OCI labels | `org.opencontainers.image.revision` is `VCS_REF`, `...created` is `BUILD_DATE`, `...version` is `APP_VERSION`. |
| Working-tree state | Not recorded: there is no dirty marker. |

Review `CHANGELOG.md` for user-visible changes before publishing a new image.
A version has the shape `MAJOR.MINOR.PATCH` with an optional pre-release suffix, for example
`2.0.0` or `1.7.0-rc1`. The job `manifest` in `ci.yml` enforces it.
`docs/changelog.md` and `docs/license.md` are copies that the docs build creates from `CHANGELOG.md`
and `LICENSE`; they are ignored by Git and must not be committed.

## Release flow
A release is built by `docker-build.yml` when a tag `vX.Y.Z` is pushed. The job `meta` checks these
conditions before anything is built:

- The tag version equals the `version` in `pyproject.toml`; `v2.0.0` requires `2.0.0`.
- The version has the shape described above.
- The tagged commit is contained in `main`. Every run that publishes is checked, also a manual one.
- An existing tag `vX.Y.Z` points at the commit that is built.

| Job | What it does |
| --- | --- |
| `meta` | Resolves the platforms and the publish decision, then validates version, tag, and `main` ancestry. |
| `lint-test` | Calls `ci.yml`, the same gate as for pull requests. |
| `build` | Builds the `amd64` and `arm64` images, scans each with Trivy, and pushes the per-architecture tags. |
| `create-manifest` | Assembles the multi-arch index and publishes it only as the tag `candidate-<version>-<run_id>-<attempt>`. |
| `attest` | Attaches a build provenance attestation to the index digest. |
| `promote` | Reads the candidate back from the registry, adds the version tag, and moves `latest`. |
| `cleanup` | Removes the per-architecture and candidate tags after a successful promotion. |
| `create-release` | Creates the GitHub Release as a draft, attaches the Trivy reports, checks the asset list, and publishes it. |

The Trivy gate fails a publishing run on a HIGH or CRITICAL finding with an available fix that `.trivyignore` does not
exempt. A run with `push=false` enforces it only when `enforce_trivy_gate` is `true`. A report
without proof that the OS and Python packages were scanned counts as a scanner failure.

`promote` never overwrites a version tag: an existing tag must already carry the candidate digest.
`latest` moves only when the version is stable and the newest stable tag in the registry.

The jobs `build`, `create-manifest`, `attest`, `promote`, `cleanup`, and `create-release` reference
the GitHub environment `release`.

A failed job can be rerun from the Actions page for 30 days: the image digests and Trivy reports
that later jobs read are kept as artifacts for that time. After that window, start a new run for the
same tag with `workflow_dispatch`. A rerun of `create-release` on an already published release only
checks its assets.

The inputs of a manual `workflow_dispatch` run are `push`, `platforms`, and `enforce_trivy_gate`.
Only a run with both platforms promotes version tags and creates the GitHub Release.

## CI workflows and scripts
| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `ci.yml` | Every branch push, pull requests to `main`, manual, and as release gate | Jobs `manifest`, `workflows` (actionlint, shellcheck), `zizmor`, `container` (Dockerfile and shell scripts), `check (ruff)`, `check (pytest)`, and `source-scan` (Trivy secrets and misconfiguration). |
| `docker-build.yml` | Tag `v*`, manual | The release flow above. |
| `docs-build.yml` | Docs changes on `main` and in pull requests, manual, after a release, weekly | Jobs `build`, `security-scan` (Trivy over the frozen docs dependencies), `deploy` to GitHub Pages, and `report-failure` for the weekly run. |
| `security.yml` | Weekly, manual | Resolves the newest stable release on Docker Hub and rescans both platform digests with Trivy. |

`ruff` and `pytest` run as two parallel entries of the matrix job `check`. A failed weekly run
opens or updates one tracking issue instead of one issue per run. Every job starts with a checkout
and then `.github/scripts/ci_job_setup.py`, which prints the runner banner and points the temporary
directories at the job temp directory. The helper lives in the repository, so the checkout is the
only step that runs before the banner. The banner installs the latest `psutil`
wheel, deliberately unpinned, into the job temp directory to list the private addresses.

| Script | Used by | Purpose |
| --- | --- | --- |
| `.github/scripts/ci_job_setup.py` | All jobs | Runner banner and temporary directory setup. |
| `.github/scripts/create-release.js` | `create-release` | Creates the GitHub Release and checks its assets. |
| `tools/release_meta.py` | `meta` | Build plan, version, tag, and `main` ancestry checks. |
| `tools/release_publish.py` | `build`, `create-manifest`, `promote`, `security.yml` | Digest handling, candidate index, promotion, and digest resolution. |
| `tools/release_newest.py` | `cleanup`, `security.yml` | Lists Docker Hub tags, finds the newest stable version, and deletes build tags. |
| `tools/trivy_report.py` | `ci.yml`, `docker-build.yml`, `docs-build.yml`, `security.yml` | Evaluates a Trivy JSON report and proves the expected scan coverage. |
| `tools/failure_issue.py` | `security.yml`, `docs-build.yml` | Opens or updates the tracking issue of a failed run. |
| `tools/pyproject-deps.py` | `ci.yml`, `docs-build.yml` | Prints a dependency group of `pyproject.toml` as a requirements list. |

Every tool prints its usage with `--help`. Workflows are checked with actionlint and zizmor; the
exceptions of zizmor are listed with reasons in `.github/zizmor.yml`.
