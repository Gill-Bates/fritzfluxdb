# Testing and releases

## Tests and lint

Install the runtime dependencies, then run the focused or complete test suite from the repository
root:

```bash
pytest
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
mkdocs build -f docs/mkdocs.yml --strict
```

Every page in the navigation must exist. Keep environment examples credential-free and update
configuration pages whenever the environment contract changes.

## Release metadata

The release version is stored in `pyproject.toml`. Container builds may add `BUILD_INFO`, which is read
at runtime for version, commit, and build date metadata. Review `CHANGELOG.md` for user-visible
changes before publishing a new image.
