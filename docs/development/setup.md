# Local development

## Install dependencies

Use Python 3.13 or newer. The runtime dependencies are pinned in `pyproject.toml`.

```bash
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
```

Create a local `.env` with placeholders replaced by your own values, or pass an INI file with
`--config`. Never put credentials into tracked files.

## Run the daemon

```bash
python run.py --help
python run.py --log-level DEBUG
python run.py --config /path/to/local.ini
```

The default config path is `app/fritzFlux.ini`; environment-based deployments commonly provide
the values directly instead. `-v` can increase verbosity, but verbose HTTP wire logging is
deliberately disabled to avoid exposing credentials.

## Run the documentation site

```bash
python -m pip install -r docs/requirements-docs.txt
mkdocs serve -f docs/mkdocs.yml
```

The strict build used for validation is `mkdocs build -f docs/mkdocs.yml --strict`.
