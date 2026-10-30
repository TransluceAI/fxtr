## Terms
You can read README.md for a definition of some terms used in this project.

## Env setup
The repo is organized into `projects/` (active, self-contained projects), `spikes/`
(dead spikes kept for reference), `examples/`, and `nix/` (nix infrastructure), with
repo-scoped config + the TS pnpm workspace hoisted to the root. Key python packages:
- `projects/fxtr_v1/pyproject.toml`, `projects/fxtr_v1/src/fxtr`: The original `fxtr` package (v1). Original version of the project, but with some design issues.
- `v2/pyproject.toml`, `v2/fxtr2`: Second iteration of the project. Will gradually adopt pieces of fxtr 1 until it becomes the new main project. (Still at `v2/` for now; a later step relocates it to `projects/fxtr2/`.)
- `spikes/thingwatcher/pyproject.toml`, `spikes/thingwatcher/thingwatcher`: Debug UI tool, will be used by fxtr 2.
- Other active projects live under `projects/` (e.g. `fxtr_workstation`, `fxtr_sandboxing`, `fxtr_workflow`, `fxtr_report_builder`).

Each repo uses `uv` to manage its packages. You can use `uv run` to run things. Alternatively you can use `.venv/bin/activate` to activate the virtual environment. From there you can use `pytest` to run tests in the `tests/`.

Depending on the task you may need to work with some subset of these files.

*Important documentation / comment rule:* Docs should be self-contained and refer to current state.
Plan files (and commit messages) are for historical reference. As such, in any CODE_OVERVIEW.md or
docstring/comment, do NOT include unqualified references to historical events like "stage 3" or
"part 2" or plan-relative parts like "chunk 5" or "design note 3". The only two cases where
references like this can be kept is if they are:
- Fully qualified references to the _specific file_ where the term is defined, if this is important
  for understanding the design rationale, por
- TODO comments with the temporary TODO specifically referencing future work that is already
  planned, with the requirement that this TODO be removed when implementing that part.

Additionally, these docs should NOT refer to historical changes, or new things replacing old things
that have been removed. (It is OK to reference other parts of the codebase that are still there,
but don't say stuff like "This replaces the old X".) General rule: include exactly the information
that someone _new_ would want to use to understand the _current_ state of the code.
