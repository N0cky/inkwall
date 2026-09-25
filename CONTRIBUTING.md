# Contributing

Thanks for taking the time to contribute to Inkwall.

## Development Setup

1. Create a virtual environment.
2. Install dependencies from `app/requirements.txt`.
3. Copy `config/settings.env.example` to `config/settings.env`.
4. Start the local dev server with `python app/server.py`.

## Project Structure

- `app/` contains the framework core
- `modules/` contains self-contained content modules
- `templates/` contains the web UI templates
- `tests/` contains the current smoke and unit tests
- `docs/modules.md` explains how to build a new module

## Before Opening a PR

Please make sure the following pass locally (CI runs the same on Python 3.12 and 3.13, then builds the image and starts it as on Unraid):

1. `ruff check app modules tests wsgi.py` (`pip install ruff`; `ruff.toml` limits it to syntax errors and undefined or unused names)
2. `python -m compileall -q app modules tests wsgi.py`
3. `python -m unittest discover -s tests -t .` (the `-t .` matters: it loads `tests/` as a package so the test isolation in `tests/__init__.py` applies and your local `config/settings.env` is never touched)
4. Template smoke test:

```bash
python -c "from app.server import app; [app.jinja_env.get_template(name) for name in ['base.html','anzeige.html','inhalte.html','geraet.html','system.html']]; print('TEMPLATES_OK')"
```

## Coding Notes

- Keep new features modular and prefer module hooks over server special cases.
- Avoid committing generated files, logs, caches, local secrets, or virtualenv contents.
- If you add a new module, also update `docs/modules.md` and `config/settings.env.example` when relevant.
- If behavior changes are user-facing, add a short note to `CHANGELOG.md`.
- `app/requirements.txt` pins every package, including the indirect ones, so each build gets the same versions. To update: install the direct packages (top block) into a fresh virtual environment, run the tests, and copy the result of `pip freeze` into the second block.

## Releasing

Every change that reaches users gets a version (`MAJOR.MINOR.PATCH`, still `0.x`):

- new features or changed behaviour → next minor version (`0.3.0` → `0.4.0`)
- only fixes → next patch version (`0.3.0` → `0.3.1`)

Steps:

1. `APP_VERSION` in `app/config.py`
2. In `CHANGELOG.md`, turn the collected `## [Unreleased]` entries into `## [x.y.z] - YYYY-MM-DD` and keep an empty `## [Unreleased]` above
3. Commit, then tag and push: `git tag -a vX.Y.Z -m "Inkwall X.Y.Z"` and `git push origin main vX.Y.Z`

The tag starts *Docker Publish*: CI first, then the images `X.Y.Z`, `X.Y` and `latest`. The firmware has its own version (`FIRMWARE_VERSION` in `esp32/Inkwall/Inkwall.ino`).

## Reporting Issues

When filing a bug report, include:

- what you expected
- what happened instead
- relevant log lines
- whether you are running locally or via Docker
- which module was active
