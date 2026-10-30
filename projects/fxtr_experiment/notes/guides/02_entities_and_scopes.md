# Entities and scopes

This guide covers the fxtr2 data layer as seen by an fxtr_experiment
user: how to define your experiment's data types, how storing/loading
works, and the small number of scope rules you need to follow. The
fuller references are `v2/notes/representing_data_as_entities.md` (the
entity-authoring guide this one adapts) and
`v2/notes/scope_service_lifetimes.md` (the service/lifetime side,
summarized in the next guide).

## Entities: immutable, content-addressed, typed data

An **entity** is the fundamental unit of data in fxtr. Conceptually it
is a JSON-like record that:

- is **immutable** — once stored, it never changes;
- is **content-addressed** — its identity (`FxtrID`) is a hash of its
  contents, so storing the same data twice yields the same id, and an
  id is a permanent, stable reference;
- has a **type** — a globally unique `FxtrID` naming the contract the
  data follows;
- can **reference other entities**, forming a DAG.

The key invariant to internalize is the **complete-subgraph
invariant**: if you hold a valid `FxtrID`, you can load that entity
*and everything it transitively references*. The store enforces this
on the way in (you cannot store an entity referencing an id that isn't
already tracked), so one id is effectively a handle to a whole closure
of data — much like a git commit hash pins its whole tree and history.

In fxtr_experiment, entities are what live in data cells, what task
functions return as outputs, and what handle keys are. The workflow
state db never copies entity bytes around; it records `FxtrID`s, and a
side-by-side **entity store** (a sqlite file) holds the closures.

## Defining an entity type

Every entity type is a frozen dataclass extending `DataclassEntity`,
registered under a namespace UUID (pick one per project and reuse it)
plus a name:

```python
import dataclasses
import uuid

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data.entity_ref_scope import EntityRef
from fxtr2.data.system_entities import define_type

MY_NS = uuid.UUID("e47ac10b-58cc-4372-a567-0e02b2c3d479")  # your project's

@dataclasses.dataclass(frozen=True)
class JudgeConfig(DataclassEntity, fxtr_type=define_type(MY_NS, "judge_config")):
    rubric: str
    model: str

@dataclasses.dataclass(frozen=True)
class JudgeResult(DataclassEntity, fxtr_type=define_type(MY_NS, "judge_result")):
    score: float
    reasoning: str
    # provenance: refs back to what this result is "about" (see below)
    transcript: EntityRef["Transcript"]
    judge_config: EntityRef[JudgeConfig]
```

`entity.store()` serializes to canonical CBOR, computes the content
hash, and returns an `EntityRef[T]`; `await ref.load()` gets the object
back. Identical data always produces the same id (the roundtrip
guarantee: `store → load → store` is id-stable).

The authoring rules that matter in practice (full detail with examples
in `v2/notes/representing_data_as_entities.md`):

- **Use `EntityRef[T]` for typed references** to other entities; use a
  bare `FxtrID` only for opaque identity you don't control (e.g. handle
  keys you're just passing along). Never stringify ids into `str`
  fields "for storage" — that breaks reference tracking and the
  complete-subgraph guarantee.
- **Prefer immutable collections**: `tuple[T, ...]` over `list`,
  `frozendict[str, T]` over `dict`.
- **Avoid `Any` fields** — define explicit union type aliases instead
  (and put `frozendict` before `tuple` in unions).
- For structured sub-records that never need their own identity, use
  `DataclassFxtrSubpart` (serialized inline, still understands
  `EntityRef`/`FxtrID` fields).

### Best practice: results refer back to what they are "about"

Where the framework records *observations* (what a task read) for
staleness tracking, your **data itself** should carry *semantic
provenance*: an output entity should hold `EntityRef`s to the exact
input entities it was computed from. In the behavior demo, a
`Transcript` carries refs to the `SubjectModelConfig`, `UserSimConfig`,
and `RolloutConfig` it was rolled out under; a `JudgeResult` carries a
ref to its `Transcript`; the analysis table carries per-row refs to the
judgements each number was averaged over.

This costs almost nothing (refs are hashes; the complete-subgraph
invariant means they're always loadable) and buys a lot: any result,
viewed in isolation — in the dashboard, in an exported repo, in a
report — is self-describing and drill-downable, independent of the
grid coordinates it happens to live at. The framework's observation
records answer "is this still current?"; your in-data refs answer
"what exactly is this a result *of*?". Follow the demo's pattern:
capture the refs at read time and thread them into the entity you
return.

## Types are contracts

An entity's type id (`define_type(NS, "name")`) is meant to name a
*specific contract* about the data — what fields mean, what consumers
may assume. Two practical consequences:

- Renaming or re-namespacing a type gives you a *different* type;
  renderers and consumers dispatch on the type id, so keep the
  namespace UUID and name stable once data exists.
- Share type definitions in a small dependency-light package that both
  your task-fn code and any renderer/type-entity tooling can import
  (the demo puts them in
  `examples/fxtr_experiment_demo/agent_workdir/packages/behavior_research_types/`).

## Scopes: who can load what, and for how long

A **scope** (`FxtrScope`) is an async context manager gating access to
a group of resources — both entities and services. The rules:

- Anything stored into a scope can be loaded from that scope; loading
  an entity makes everything it references loadable too.
- Child scopes inherit from parents (a child can see the parent's
  entities, not vice versa).
- When a scope exits, entities tracked only there are released.

Scopes exist to make lifetimes locally reasonable (no global leaks, no
dangling references) and to decouple *where* data lives from *how* you
use it — the same `EntityRef.load()` works whether the entity is in
memory, in the session's sqlite store, or served remotely.

### The good news: inside task code you mostly don't think about this

fxtr_experiment arranges the scopes for you. Inside a **task fn** or
**step fn**, the active scope is the per-run (or per-step) scope the
framework set up, and the sanctioned user-code style is simply:

```python
ref = MyEntity(...).store()      # stores into the ambient run scope
obj = await EntityRef(some_id, MyEntity).load()
```

Values you `read()` from views are loadable; ids you return as outputs
are persisted durably by the framework (the session guarantees an id is
recorded only after its closure is in the entity store — the "liveness
invariant" — so nothing recorded ever dangles).

### Where you do think about it: host-side driver code

If you write host-side code that talks to a workflow capability (like
the demo's driver services), two membership rules matter:

1. **Entities you pass into capability calls must be loadable in the
   capability's home scope.** The demo stores driver-constructed
   entities into the runner scope before writing them into cells:
   `cfg.store(runner.scope)`.
2. **Ids returned by capability calls are loadable in the runner
   scope** (the capability re-attaches every id it serves), but *not*
   automatically in your own unrelated scope — copy the closure across
   if you need to return it elsewhere
   (`await my_scope.copy_from_scope(some_id, runner.scope)`).

See `examples/fxtr_experiment_demo/behavior/behavior_demo_helper_service.py`
(`_export`, and the "Membership:" comments) for the worked pattern.

**NEEDS CONFIRMATION**: is this the right level of scope guidance for a
casual user — i.e. "bare store/load inside tasks; store-into-runner-scope
and copy-out in drivers" — or are there other scope rules a demo-builder
will hit in practice (e.g. around `include=[...]` for service type
entities living in `GLOBAL_SCOPE`)?

## Saving entities outside the workflow

The workflow session owns its own entity store, and normally you never
touch it directly. But entities are ordinary fxtr2 data, so you *can*
persist any id you hold to your own store — e.g. to pin a result
somewhere independent of the workflow:

```python
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace

async with SQLiteFxtrNamespace("sqlite+aiosqlite:///mydb.sqlite", "myns") as ns:
    await ns.pin("best_result", ref)          # persists the whole closure
    later = await ns.retrieve("best_result", MyEntity)
```

There is also a git-native TSV namespace (pin mappings in tracked TSV
files over a repo entity store) and an HTTP-served namespace — see
`v2/CODE_OVERVIEW.md`. For workflow state itself, use the export flow
(guide 7) rather than hand-pinning.

**NEEDS CONFIRMATION**: is `SQLiteFxtrNamespace` still the recommended
"your own external store" surface to point users at, and is the
snippet's API current?
