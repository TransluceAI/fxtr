# fxtr_experiment_task — code overview

The **task-side half** of `fxtr_experiment`: everything a sandbox guest
needs to serve a task fn, and nothing else. Its own distribution
(`fxtr-experiment-task`) so a guest can install it without the runner —
the state db, ops engine, executor, and sandbox host machinery, which
drag in sqlite and the podman/eryx stack.

The runner package is the parent directory (`../`, dist
`fxtr-experiment`); it depends on this one with `[frames]`. **Imports run
runner → task only, never back** — which is structural, not a convention:
a guest installs this dist alone, so the runner dist is not there to
import.

**Where to start reading**: `../CODE_OVERVIEW.md` — the runner's overview
is the map of the whole system and carries the deep per-module detail for
these modules too. This file is the packaging-level orientation: what is
on this side of the cut, and why.

## Layout

```
fxtr_experiment_task/
├── __init__.py       # docstring only
├── _ns.py            # the project's namespace UUID (leaf; the runner imports this too)
├── errors.py         # error taxonomy + the error-poisoning contract
├── model.py          # canonical data model: axes/coords/paths, templates, grid configs,
│                     #   records + the frame schema law (validate_frame_schema)
├── views.py          # user-facing view calculus
├── view_spec.py      # runtime view specs + canonicalization (the concept reference)
├── task_api.py       # task/step fn contracts (plain protocols over views)
├── runner_api.py     # task↔runner wire model + capability protos
├── view_runtime.py   # task-side view runtime: shadows, backend seam, step context, @step_fn
├── task_adapter.py   # @task_fn adapter + output normalization — the guest entry point
└── tables.py         # the frame runtime (polars); behind the [frames] extra
```

## The two boundaries this package exists to keep

**No runner side.** Nothing here imports `fxtr_experiment.*`. The cut was
made to fall cleanly: the pieces the frame runtime used to borrow from the
runner's encoders now live task-side (`EncodingError` in `errors.py`,
`FrameRow` + `validate_frame_schema` in `model.py`), and identity minting
stays runner-side (`frame_encoding.canonical_frame_from_ipc`) because by
doctrine a guest never canonicalizes — the declared schema is the host's
authority.

**Polars behind `[frames]`.** `tables.py` is the sole eager-polars module,
and every task-side reach into it is *function-local* (`view_runtime`'s
frame ops, `task_adapter`'s frame-output normalization). So an entity-only
guest installs without the extra and never loads polars — which matters
because polars has no wasm wheel, and non-container guests are the reason
this package exists.

## Dependencies

`fxtr2`, `frozendict`, `mashumaro`; `[frames]` adds `polars`.
Deliberately *not* `fxtr-sandbox-types`: nothing here imports it — a
guest's `run(request)` entrypoint does, and declares it itself.

## Tests

No test directory of its own. The runner package's suite (`../tests/`)
covers these modules, in the one shared venv.
