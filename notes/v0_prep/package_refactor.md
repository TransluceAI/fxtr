# Refactor plan

Need to create a package `fxtr` in `lib/fxtr/`. This is where this code will move to. Fxtr package should be set up similarly to `behaviors` package, both in its own pyproject.toml and in the root monorepo pyproject.toml.

Most code under `lib/behaviors/behaviors/staging` should move to `lib/fxtr/fxtr` and be importable under `fxtr` instead of `behaviors.staging`

Code under `lib/behaviors/behaviors/staging/examples` should move to `lib/fxtr/examples`

Code under `lib/behaviors/behaviors/staging/notes` should move to `lib/fxtr/notes`

Code under `lib/behaviors/behaviors/tests` should move to `lib/fxtr/tests/`

Behaviors package should still exist but it should be empty

All imports inside `behaviors/staging` should be fixed to point to the right thing. Code outside the behaviors package does not have to change.
