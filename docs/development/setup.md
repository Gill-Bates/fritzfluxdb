# Local development

## Install dependencies

Use Python 3.14 or newer. The runtime dependencies are pinned in `pyproject.toml`.

```bash
python3.14 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install '.[dev]'
```

The shared `gillbates-*` packages are installed from tags of the private `Gill-Bates/python-libraries`
repository, so the install needs git and read access to it. For local work on a package, install it
editable from a checkout (`pip install -e <checkout>/packages/<name>`) after the command above.

Direct starts do not load `.env` automatically. Export the variables in your shell (for example
`set -a; . ./.env; set +a` with a local `.env` holding your own values), or pass an INI file with
`--config`. Never put credentials into tracked files.

## Run the daemon

```bash
python -m app.main --help
python -m app.main --log-level DEBUG
python -m app.main --config /path/to/local.ini
```

The default config path is `app/fritzFlux.ini`; environment-based deployments commonly provide
the values directly instead. `-v` can increase verbosity, but verbose HTTP wire logging is
deliberately disabled to avoid exposing credentials.

## Run the documentation site

```bash
python tools/pyproject-deps.py docs > /tmp/requirements-docs.txt
python -m pip install -r /tmp/requirements-docs.txt
cp CHANGELOG.md docs/changelog.md && cp LICENSE docs/license.md   # Git-ignored build copies
zensical serve -f mkdocs.yml
```

Validate with `zensical build --config-file mkdocs.yml --strict --clean` and `python tools/docs_site_check.py`. The requirements file stays outside the
repository; `requirements*.txt` files must not be committed.
