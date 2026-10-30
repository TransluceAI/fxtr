# fxtr_workflow_2 — code overview

**Status: partial prototype, NOT current.** This is a parked, self-contained spike
of a workflow system that was built on top of `fxtr2` and then removed from the main
`v2/` codebase. Nothing depends on this package; it depends on `fxtr2` (for the entity
/ service / scope machinery) only so its code and tests still run. It is kept as a
historical record of the design and a working partial implementation — a future
workflow effort may revisit the ideas here from a clean slate rather than continuing
this code directly.

## What it is

A durable, trace-based task/workflow layer on top of fxtr2's service-capability system:
**tasks** (observably pure, possibly-nondeterministic computations bound to args) are run
into named **slots** of a **realization map** (results cache) with operations recorded to a
separate append-only **op log**, executed by **task functions** which are stateless
services spawned from content-addressed **service handle** entities.

What's implemented here: the task-facing protocols + error types (`task_types.py`); the
external-only **stateless service provider** (`stateless_service_provider/`); the
realization-map / op-log **entities** (`entities.py`); **cache-key canonicalization**
(`cache_key.py`); and the single-level + nesting **runner** (`runner.py`). Not
implemented: **slot deletion**, and the planned nix-sandboxed service provider.

## Layout

```
fxtr_workflow_2/                 # the package (depends on fxtr2)
├── __init__.py                  # re-exports
├── _ns.py                       # WORKFLOWS_NS namespace UUID
├── task_types.py                # task-facing service protocols (TaskSlot/StepContext/Fn, RunGroup) + error types
├── entities.py                  # realization-map / op-log entities (RunGroup, WorkflowTaskInvocation/Complete, slot placeholder, op-log events)
├── cache_key.py                 # canonicalize args -> FrozenFxtrCbor + build_invocation (deterministic, provenance-blind cache key; slot args keyed by path)
├── runner.py                    # WorkflowRunner: run groups, slot capabilities, nested task execution (spawn/run/cache/persist + op log)
├── stateless_service_provider/  # turn service-handle entities into running services
│   ├── handles.py               # ServiceHandle[T] marker + ExternalServiceHandle
│   ├── protocols.py             # spawner/connector base protocols + spawn_resolved
│   └── basic_provider.py        # BasicStatelessServiceProvider (external-only) + registration proto + capability split
└── LOCAL_CODE_OVERVIEW.md       # detailed module-by-module documentation

plans/                           # historical design docs + implementation plans (verbatim copies)
tests/                           # pytest suite (anyio); passes against fxtr2
```

See `fxtr_workflow_2/LOCAL_CODE_OVERVIEW.md` for the detailed design and `plans/` for the
original design discussion and implementation plans (kept as a historical record).

## Running the tests

```
uv run pytest
```
