# fxtr — code overview

fxtr is a configuration and orchestration system for modular, reproducible experiments: a user
writes steps and workflows over typed, content-addressed arrays, and fxtr runs them as durable
jobs on Postgres, caches their results, and serves them to the experiment viewer. It depends on
nothing else in this repository; `behaviors` and every example and personal project depend on it.
Setup and development commands are in the root `README.md`. The public API as a project uses it is
documented on the docs site, `docs/fxtr/`, whose pages the fxtr skill (`.agents/skills/fxtr/`)
links and ships copies of in the Claude Code and Codex plugins (repository tooling in
`scripts/skills/`).

The core install needs no database: the Postgres runner is the `runner` extra, and the viewer's
backend is the `debug-web` extra on top of it.

## Components

The package is organized around these responsibilities. `array_sql.py` connects concrete arrays
and experiment handles, and the Postgres runner implements the client interfaces defined in
`project/`.

1. **Entities and storage** (`core/`, `storage/`; `core/CODE_OVERVIEW.md`). Every value has a
   canonical DAG-CBOR form and a content ID; an `Entity` serializes itself to that form, a
   `BoundID` pairs an `EntityID` with the loader that turns data back into an object, and a storage
   engine stores and loads by id. Engines: `MemoryStorage` and the Postgres runner's write-behind
   store.
2. **Entity definitions, arrays, and queries** (`entity_defns/`, `duckdb_engine/`, `array_sql.py`;
   `entity_defns/CODE_OVERVIEW.md`). The dataclass layer most entity types are defined with
   (`TaggedDataclass`, `DataclassEntity`, the strict cattrs converter), `Schema` (the value type
   system), `Array` (a sparse named array: named dims, one value schema, rows or an Arrow table),
   and array queries (a query builder and `fxtr.sql`, both run by DuckDB over Arrow).
3. **Experiment definitions** (`experiment/`; below). Steps and workflows, their signatures, the
   decorators and the registry, and `ArrayHandle`, the lazy array a workflow body schedules over.
4. **The runner** (`runner/`; `runner/CODE_OVERVIEW.md`). How a body runs: the run-session
   protocols between a body and the host that owns experiment state, nodes, the driver, and
   determinism verification. `runner/postgres/` (`runner/postgres/CODE_OVERVIEW.md`) is the
   host: all experiment state in Postgres, jobs run by the process that launches them, and the step
   cache. `runner/postgres/debug_web/` (`runner/postgres/debug_web/CODE_OVERVIEW.md`) is the
   read-only backend of the experiment viewer in `frontend/`.
5. **The project** (`project/`; `project/CODE_OVERVIEW.md`). What a user's code is part of:
   locating and configuring a project, pinning the code a launch runs, `ProjectClient` (what user
   code holds), the launch helpers, `fxtr new`, and the `fxtr` command line.
   It imports nothing that needs the `runner` extra.

## Source tree

```
fxtr/
├── pyproject.toml          package metadata, runtime dependencies, the `runner` and `debug-web`
│                           extras, the `fxtr` script, pytest config, and Pyright scope
├── LICENSE, NOTICE        Apache License 2.0 and copyright notice (behaviors/ carries the same)
├── CHANGELOG.md            release notes, a section per version
├── hatch_build.py          build hook: ships the built viewer (frontend/dist) in the sdist and
│                           wheel as debug_web/_frontend, and the TypeScript packages renderers
│                           use (ts/, staged by project/ts_packages.py) as _ts; a wheel built from
│                           an sdist ships the sdist's copies; a non-editable build lacking either
│                           directory fails
├── src/
│   └── fxtr/
│       ├── __init__.py     exports sql as fxtr.sql
│       ├── array_sql.py    fxtr.sql (entity_defns/CODE_OVERVIEW.md)
│       ├── core/           entities, ids, the codec, the storage contract
│       │                   (core/CODE_OVERVIEW.md)
│       ├── storage/        MemoryStorage (core/CODE_OVERVIEW.md)
│       ├── entity_defns/   the dataclass layer, schemas, arrays, query descriptions
│       │                   (entity_defns/CODE_OVERVIEW.md)
│       ├── duckdb_engine/  running queries in DuckDB (entity_defns/CODE_OVERVIEW.md)
│       ├── experiment/     experiment definitions
│       │   ├── annotations.py  reading array types off a body's annotations (AnnotatedType)
│       │   ├── handles.py  ArrayHandle (a lazy array inside a workflow) and ArrayHandleQuery (a
│       │   │               query over handles, a node once collected)
│       │   ├── signatures.py  Signature/FixedSignature and run_output_type, the typing rule for
│       │   │               running over dims
│       │   ├── registry.py FunctionRegistry (steps and workflows by name) and the GLOBAL_REGISTRY
│       │   │               the decorators fill at import time
│       │   ├── steps.py    StepContext, Step, the @step decorator
│       │   └── workflows.py  WorkflowContext, Workflow, the @workflow decorator, pathname and
│       │                   cache address formatting
│       ├── runner/         running steps and workflows (runner/CODE_OVERVIEW.md, with
│       │                   postgres/ and postgres/debug_web/ each having their own)
│       └── project/        the project, its client, and the CLI (project/CODE_OVERVIEW.md)
└── tests/                  see "Tests" below
```

The example project's package (`example_projects/basic_project/src`) is on the tests' import path
(pytest's `pythonpath`, mirrored in Pyright's `extraPaths`), so fxtr's tests run the example
experiments.

## Experiment definitions

`experiment/` is the vocabulary for describing an experiment as a DAG of *steps* and *workflows*
over *handles*, every handle being a lazy array with a known `ArrayType`.

- `steps.py`: a `Step` wraps an `async def body(context: StepContext,
  **inputs)` with a `name` and a `Signature`; `@step(inputs={...}, output=...)`
  (array type specs) or `@step(signature=...)` builds one. The name is the
  whole of a step's code identity to the runner: cached results are matched
  by name and inputs, and a step whose behaviour changed keeps matching until
  its cache addresses are cleared. Steps are nominal objects, so an
  undecorated function cannot be scheduled. The decorators register every
  step and workflow they build in `registry.py`'s `GLOBAL_REGISTRY` by name,
  so importing a module is what makes its functions runnable; a name is
  ambiguous only when looked up, and a function defined in `__main__` must
  be given `name=` since its inferred qualified name would not be stable. `Step.invoke(context, arrays)` is
  how the runner calls a step: it checks the inputs against the signature,
  retypes each input to the type its annotation declares (so its values
  *present* as annotated — a scalar `id` as a `BoundID`, a struct as its
  TypedDict `dict`; see `present_value` — since the schema an input arrives
  with lost its loaders and presentation at the wire), unwraps zero-dim inputs
  to bare values, wraps a bare result into a scalar array when the output type
  has no dims, and checks the result type and returns it retyped to the output,
  so the step fixes its own result to the type it declared (the scheduling node
  separately matches the possibly-stacked type it precomputed — the seam for an
  eventual process boundary between step logic and `run_step`). The
  decorator derives an `ArgConversion` per parameter from the body's annotations
  (resolved at decoration time): its `view` (the annotation's array type) drives
  the retype, done through the shared `input_view_type` (the annotation's schema,
  with the input's own dims when it names none, else a `FixedSignature`'s
  declared input); its `kind` then says whether the body wants the array whole
  (`Array[...]`), a loaded entity (a bare entity class), or the plain presented
  value (everything else — `BoundID[T]`, `EntityID`, a leaf, a TypedDict). A step
  parameter annotated `ArrayHandle[...]` is refused at decoration: a step receives
  concrete arrays and queries them locally. A
  scalar `id` output may still be returned as an `Entity` (stored, its id used)
  or a bound id. The `step` docstring states what each desugars to. Calling a
  `Step` directly passes arguments through untouched.

  **Static types.** `Step[P, R, V]` has two value type parameters: `R`, what
  the body returns (and calling the step directly gives), and `V`, the values
  of the array it produces (what `invoke` gives, and so the `ArrayHandle[V]`
  that `run_step` returns). They differ only for an entity result, which is
  stored and produced as its id: `R = T` gives `V = BoundID[T]`, and
  `T | None` gives `BoundID[T] | None`. The overloads on the decorator's
  `__call__` (`_StepDecorator`) work out `V` from `R`; array-returning bodies
  (`_ArrayStepBody`) are matched first because `Array` is itself an entity.
  Code that only needs the produced type writes `Step[Any, Any, V]`.
- `signatures.py`: a `Signature` maps the `ArrayType` of each named input to
  the output `ArrayType`, synchronously, raising `SignatureError`;
  `FixedSignature` is the fixed-types case (same dims in any order, assignable
  schema). `run_output_type(signature, inputs, map_over, replicas)` is the
  single scheduling rule, shaped like xarray's `apply_ufunc`: the mapped dims
  are resolved against the inputs (an input lacking one is broadcast), each
  input minus the mapped dims must fit the signature, and the result has the
  mapped dims, then the replica dims (`replica_dims`: new dims with explicit
  values, which must not be input dims), then the signature's output dims.
  `resolve_signature` normalizes a decorator's signature arguments: `inputs`
  plus `output`, a `signature` object, or — when neither is given — the body's
  annotations. Annotations alongside `inputs`/`output` are *checked* against
  it by function subtyping: annotated dims must match exactly, an input's
  declared schema must be assignable to the annotated one (`model: EntityID`
  accepts a declared typed id), and the result's annotated schema must be
  assignable to the declared one (`-> str` fits a declared `str | None`, but
  `-> EntityID` does not fit a declared typed id, since nothing would catch a
  wrong id). Deriving from annotations instead is all-or-nothing.
- `annotations.py`: how a body declares its own array types, as Python types.
  The value schema comes from the annotation through `schema_from_annotation`
  (for an `Array[X]` or `ArrayHandle[X]` argument, from `X`), with two forms
  read only at the top level of an argument or result to match the invoke
  conventions: a bare entity class `T` means `id_of(T)` and `T | None` means
  `optional(id_of(T))`. The dims come from a bare string in `Annotated`, and
  only on an `Array`/`ArrayHandle` (`Annotated[ArrayHandle[BoundID[Bio]],
  "[bio: str]"]`); these are the only annotations whose string metadata fxtr
  reads, a string elsewhere belongs to another tool. A value has dims `()`; a
  bare `Array[X]` declares its schema but not its dims, so a derived signature
  needs the string. `Any`, `object`, or no annotation declares nothing; any
  other non-schema annotation (`dict[str, Any]`, an array inside a union) is
  a `TypeError` at decoration. What no Python type expresses goes in the
  decorator's `inputs=`/`output=`. `declared_array_type` reads one annotation
  into an `AnnotatedType` (dims and schema, either possibly unknown) and
  `declared_signature_types` reads a whole body. `unwrap_annotation` (used by
  both the step and workflow argument classifiers) strips `Annotated` metadata
  and `| None` to the type an annotation is really about, expanding PEP 695
  `type` aliases (`expand_type_alias`) so an alias like `ScalarHandle[X]`
  reads as what it names.
- `handles.py`: `ArrayHandle[V]`, a lazy array whose `ArrayType` is known before it
  is computed, with `type`, `async observe()`, `item()`, `expect_type`, `retype`,
  and query shortcuts. A handle's type is what the workflow
  declared for it, presentation included: `retype` re-declares it (the
  handle's type must be assignable to the new one; implementations supply
  `_retyped`), `expect_type` with a schema does the same after its equality
  check, and observing a handle yields the array retyped to the handle's type.
  In a workflow, a `retype` that changes only presentation names the same node,
  and one that changes the stored type records a query node at once.
  `ScalarHandle[X]` is a `type` alias for `Annotated[ArrayHandle[X], "[]"]` (the
  no-dims handle annotation, spelled once); `array.py` has the parallel
  `ScalarArray[X]` for a step's observed input.
  `handle.query()` starts an `ArrayHandleQuery` (as do the handle's `filter`, `select`,
  `limit`, `aggregate`, `join`, `sel`, `rename`, and `field` shortcuts, mirroring `Array`'s),
  the workflow's counterpart of
  `ArrayQuery`: `filter`, `select`, `limit`, `aggregate`, `join`, `sel`, `rename`,
  `field`, and `retype` type-check at once and add nothing to the workflow's graph, and
  `collect()` records the whole chain as one query node and returns the handle to it
  (`fxtr.sql` over handles returns one too). What is recorded is exactly what was collected,
  so it does not depend on how the body's tasks interleave; a shared part runs once only if
  the body collects it and builds on its handle. A query passed where a node is needed
  without being collected is refused. Only handles from the same attempt may be combined.
  What a workflow selects can feed a step without the rows crossing to it.
- `workflows.py`: `WorkflowContext` (an `FxtrEntityStorage`) declares the
  scheduling API: `add_source_array`, `combine`, `stack`, `run_step`,
  `run_child_workflow`, the last two taking `map_over`, `join`, and `replicas`
  (new dims along which the run is repeated with the same inputs, invisible to
  the body; only the cache address differs). A `Workflow` wraps an `async def body(context, **args) ->
  ArrayHandle | value`, built by `@workflow(...)` with the same arguments as `@step`;
  workflows may `await handle.observe()` mid-body, so the DAG is dynamic.
  `Workflow.invoke(context, handles)` is how the runner calls a workflow, mirroring
  `Step.invoke`: it checks inputs against the signature and hands each to the body per a
  `WorkflowArgConversion` derived from the body's annotations — the handle itself
  (`ArrayHandle[...]`, or unannotated/`Any`, the default), the awaited observed array
  (`Array[...]`), or the awaited scalar value (a bare entity class loads through the context,
  everything else arrives presented) — each retyped to the annotation first (shared
  `input_view_type`). The body returns a handle (checked assignable to the output) or a bare
  value; a bare value is registered as a scalar source array under the reserved
  `RETURN_NODE_NAME` (`"$return"`) at the declared output type (for an `id` output an `Entity` is
  stored and a bound id unwrapped to its id) — the workflow's analog of a step fixing its own
  output. Workflow pathnames and cache address prefixes are `/`-joined (`join_path`),
  and mapping appends `[dim:value,...]` with JSON-encoded values
  (`append_mapped_key`), e.g. `root/sample[prompt:3,split:"a"]`; a step's
  cache address prefix defaults to the workflow's prefix joined with the step's
  name. `Workflow[P, R, V]` has the same `R`/`V` split as `Step`, worked out by
  the `_WorkflowDecorator` overloads (`run_child_workflow` returns
  `ArrayHandle[V]`).

## Tests

Tests that need Postgres use the `database` fixture of `tests/conftest.py` (a `Database` on a
throwaway schema) and are skipped unless `FXTR_TEST_DATABASE_URL` is set; `conftest.py` also sets
the anyio backend and a per-test timeout. Each group's owner describes its files:

- `test_codec.py`, `test_core_car.py`, `test_storage.py`, `test_loader_registry.py`: entities and
  storage (`core/CODE_OVERVIEW.md`).
- `test_dataclass_*.py`, `test_schema.py`, `test_array.py`, `test_arrow_arrays.py`,
  `test_dual_backed_array.py`, the query tests (`test_query*.py`, `test_local_*.py`,
  `test_deep_array_queries.py`, `test_duckdb_executor.py`, `test_*array_query_boundaries.py`,
  `test_user_array_sql*.py`), and their shared arrays (`array_testing.py`,
  `deep_array_testing.py`, `future_annotations_testing.py`): entity definitions, arrays, and
  queries (`entity_defns/CODE_OVERVIEW.md`).
- `test_experiment_registry.py`: registration from the decorators, ambiguous names, the
  `__main__` naming rule. `test_experiment_signatures.py`: signatures, `run_output_type`, the
  decorators, annotation-declared signatures, `Step.invoke`, path and cache address formatting.
  `test_experiment_result_types.py`: static (`assert_type`) checks of the `R`/`V` types the
  decorator overloads give and `run_step`/`run_child_workflow` return.
- `test_runner_*.py` other than the Postgres ones, `test_workflow_queries.py`,
  `test_semantics_reference.py` (the reference examples of a correct execution, run on every
  backend in `reference_backends.py`), and `runs_testing.py` / `remote_testing.py`: the runner
  (`runner/CODE_OVERVIEW.md`).
- `test_runner_postgres_*.py` and `postgres_testing.py`: the Postgres runner
  (`runner/postgres/CODE_OVERVIEW.md`); `test_debug_web_*.py` and
  `test_runner_postgres_debug_web.py`: the viewer backend
  (`runner/postgres/debug_web/CODE_OVERVIEW.md`).
- `test_project_*.py`: the project (`project/CODE_OVERVIEW.md`).
- `test_example_*.py`: the example project's experiments on
  Postgres, and an opt-in launch of a copy of the project through `uv run`
  (`example_projects/basic_project/CODE_OVERVIEW.md`).
- `test_smoke.py`: the package imports.

## Where to change things

- **The wire format, ids, or the storage contract:** `core/CODE_OVERVIEW.md`.
- **A value shape, an array operation, or a query operation:**
  `entity_defns/CODE_OVERVIEW.md`.
- **What a step or workflow body declares or receives:** `experiment/` (the decorators, the
  signatures, the argument conversions); how the runner calls a body is `Step.invoke` /
  `Workflow.invoke`.
- **What a workflow body can schedule:** the run-session protocol (`runner/session.py`) and
  everything implementing it; `runner/CODE_OVERVIEW.md`.
- **Durable state, jobs, the step cache, resuming, cancelling:**
  `runner/postgres/CODE_OVERVIEW.md`.
- **What the viewer can read:** `runner/postgres/debug_web/CODE_OVERVIEW.md`, with the
  viewer itself in `frontend/CODE_OVERVIEW.md`.
- **Configuration, launching, the command line, new projects:**
  `project/CODE_OVERVIEW.md`.
- **What the sdist and wheel carry:** `hatch_build.py` and `pyproject.toml`.
