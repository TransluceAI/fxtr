## Terms
You can read README.md for a definition of some terms used in this project.

## Env setup
This repo contains a few different python packages:
- `pyproject.toml`, `fxtr`: The root pyproject.toml. Original version of the project, but with some design issues.
- `v2/pyproject.toml`, `v2/fxtr2`: Second iteration of the project. Will gradually adopt pieces of fxtr 1 until it becomes the new main project.
- `extras/thingwatcher/pyproject.toml`, `extras/thingwatcher/thingwatcher`: Debug UI tool, will be used by fxtr 2.

Each repo uses `uv` to manage its packages. You can use `uv run` to run things. Alternatively you can use `.venv/bin/activate` to activate the virtual environment. From there you can use `pytest` to run tests in the `tests/`.

Depending on the task you may need to work with some subset of these files.
