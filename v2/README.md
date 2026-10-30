# Fxtr Core (v2)

Fxtr is a data layer for tracking provenance and enabling reproducible experiments. It provides content-addressed, typed, immutable storage for semi-structured data, with tools for saving, loading, version-controlling, sharing, querying, and (eventually) visualizing that data. The long-term goal is to be a foundational data and protocol layer for describing both data and computations in a way that supports automatic provenance tracking, hermetic caching, modular re-use, and observability — enabling both humans and agents to navigate the outputs of complex automated pipelines and trace back how any result was produced.

This is the core package, which defines the central components of the system.

The Python package is currently called `fxtr2` (and will likely be renamed prior to public release). It lives in the `v2/` subdirectory of the fxtr repository.

## Goals

Fxtr is designed around a few primary goals:

- **Cross-compatibility and interoperability.** Research outputs should be easy to combine and re-use. Fxtr tries to make the most convenient way to define data types also the way that is best set up for others to consume and build on.

- **Stability and version control.** You should be able to stably refer to a specific version of any piece of data, and have that reference remain valid over time. Because data is content-addressed and immutable, storing the same data always produces the same ID, and the same ID always refers to the same data.

- **Inspectability and provenance.** It should be possible to trace any result back through the process that produced it, and to inspect state at any stage. This creates natural incentives for correctness and makes it easier to audit automated pipelines.

- **Composability.** As you build better primitives, you will want to compose them into larger workflows. Fxtr aims to let you express this composition within the system itself, so that you still get provenance tracking, version control, and type safety at every level.

- **Ease of use.** Fxtr should speed you up rather than slow you down. It should be easy enough to adopt incrementally, with each piece providing immediate value.

## Installation

Fxtr uses [uv](https://docs.astral.sh/uv/) for dependency management. From the `v2/` directory:

```bash
uv sync
```

To run the test suite:

```bash
uv run pytest -n auto
```

## Core Concepts

### Entities

An **entity** is the fundamental unit of data in fxtr. Conceptually, an entity is a JSON-like dictionary that:

- Is **immutable** — once stored, it never changes.
- Is **content-addressed** — its identity is determined by a hash of its contents. Storing the same data twice always produces the same ID.
- Has a **type** (`$fxtr.type` field) — a `FxtrID` that identifies what kind of data this is and what contract it follows.
- Can **link to other entities** — via strong or weak references, forming a graph of related data.

The easiest way to define entity types is with `DataclassEntity`:

```python
import dataclasses
import uuid
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity, Child, ChildRef, EntityRef
from fxtr2.data.system_entities import define_type

# Namespace UUID prevents type ID collisions between independent projects
MY_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef0123456789")

@dataclasses.dataclass(frozen=True)
class Experiment(DataclassEntity, fxtr_type=define_type(MY_NAMESPACE, "experiment")):
    model_name: str
    temperature: float
    results: tuple[ChildRef[Result], ...]   # lazy references to child entities

@dataclasses.dataclass(frozen=True)
class Result(DataclassEntity, fxtr_type=define_type(MY_NAMESPACE, "result")):
    score: float
    output: str
```

Each type is identified by a `FxtrID` derived from your namespace UUID and a type name. Using UUIDs means different projects can independently define types without risk of collision.

### FxtrID and EntityRef

A **`FxtrID`** is an opaque, content-based identifier for an entity. You get one when you store an entity, and you use it to load the entity back.

An **`EntityRef[T]`** bundles a `FxtrID` together with a target type annotation `T`, so that when you load the reference, fxtr knows what Python type to deserialize into. This is what you normally work with:

```python
# Store an entity — returns an EntityRef
ref: EntityRef[Result] = Result(score=0.95, output="hello").store()

# Load it back
result: Result = await ref.load()
```

Entity references can appear as fields on other entities. There are four flavors, varying along two axes — **link strength** (strong or weak) and **loading behavior** (lazy or auto-loaded):

| Type | Link Strength | Loading | Description |
|------|---------------|---------|-------------|
| `EntityRef[T]` | weak | lazy (`.load()`) | Default reference to another entity |
| `ChildRef[T]` | strong | lazy (`.load()`) | Ownership/containment reference |
| `LinkedEntity[T]` | weak | auto-loaded | Inline — loaded automatically with the parent |
| `Child[T]` | strong | auto-loaded | Inline child — loaded automatically with the parent |

Strong vs. weak links are a semantic distinction (ownership vs. association). Both keep the referenced entity alive in storage. Auto-loaded references (`Child[T]`, `LinkedEntity[T]`) are convenient for small, always-needed data; lazy references (`ChildRef[T]`, `EntityRef[T]`) are better for large or optional sub-graphs.

### Scopes

A **`FxtrScope`** governs the lifetime of entities in memory. Scopes serve two main purposes: **limiting memory usage** (entities tracked only in a scope are cleaned up when it exits) and **maintaining the complete-subgraph invariant for external storage** (ensuring that entities you create that reference lazily-loaded data from a namespace don't outlive that namespace).

Scopes guarantee that:

- Anything stored into a scope can be loaded from that scope.
- Loading an entity also makes all of its referenced children loadable.
- Child scopes inherit from their parents — anything accessible in a parent scope is accessible in a child scope.
- When a scope exits, entities that were only tracked in that scope become inaccessible (and can eventually be garbage-collected from memory).

```python
from fxtr2.data.entity_ref_scope import FxtrScope

async with FxtrScope() as scope:
    ref = Result(score=0.9, output="test").store()
    loaded = await ref.load()  # works — ref is in scope

# After the scope exits, `ref` can no longer be loaded from memory
# (unless it was also stored somewhere persistent)
```

There is a **global scope** that is always active, so for simple usage you don't need to create scopes explicitly — just call `.store()` and `.load()`. By default, new scopes inherit from the currently-active scope, and entering a scope makes it the active scope for code running inside it. Most operations on `Entity`, `EntityRef`, and `FxtrScope` use the active scope by default, but allow you to explicitly specify a different scope. To get the active scope explicitly, use `FxtrScope.get_active()`.

**Scope patterns for moving data around:**

- **Lifting** (`ref.lift(scope)`) brings an entity from a parent scope into a child scope, making it explicitly tracked there. This is a lightweight operation since the data is already accessible via inheritance.
- **Copying** (`ref.copy_to(scope)`) copies an entity and its entire transitive subgraph into another scope. This is needed when moving data from a child scope to a parent or sibling scope, because the target scope doesn't inherit from the source. Copies are async because they may need to load entities from the source. Pinning an entity to a namespace also triggers a copy automatically.
- **Isolation** (`FxtrScope(isolated=True)`) creates a scope that does *not* inherit from the active scope and does not become the active scope. You can explicitly store values into it or pull them out. This is useful for background work that shouldn't affect the main scope.
- **Including** (`FxtrScope(include=[other_scope])`) explicitly attaches another scope as a parent, giving access to its entities. This is useful when you need to access entities from a namespace that was opened in another async task, or opened with `isolate_scope=True`.

Opening a namespace also enters a scope in that context by default, which ensures that entities referencing lazily-loaded namespace data don't outlive the namespace.

### Namespaces

A **namespace** is a persistent, named partition of storage. Namespaces let you give entities human-readable names (called **pins**) and persist them to disk. A namespace is backed by either SQLite or git-tracked TSV files (over a repo-level entity store).

```python
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace

# Store an entity and pin it under a name
async with SQLiteFxtrNamespace("path/to/store.sqlite", "my_namespace") as ns:
    ref = Experiment(model_name="gpt-4o", temperature=0.7, results=()).store()
    await ns.pin("experiments:baseline", ref)

# Later (even in a different process), open the same namespace and retrieve it
async with SQLiteFxtrNamespace("path/to/store.sqlite", "my_namespace") as ns:
    retrieved_ref = await ns.retrieve("experiments:baseline", Experiment)
    experiment = await retrieved_ref.load()
```

**`SQLiteFxtrNamespace`** stores everything in a local SQLite database with append-only segment files for entity data. Good for local development and batch processing.

**`TsvFxtrNamespace`** stores pin-name-to-ID mappings as git-tracked TSV files inside a git repository, with a shared repo-level entity store (SQLite) for the actual entity data. This means pin names are version-controlled alongside your code:

```python
from fxtr2.data.fxtr_context_api import TsvFxtrNamespace

async with TsvFxtrNamespace(Path("path/to/git/repo")) as ns:
    await ns.pin("experiments:v2", ref)
    # The pin mapping is now a TSV file you can git-commit
```

### Querying with Indexes

Namespaces support **indexing** and **SQL queries** over pinned entities. You register indexers that describe how to walk through the entity graph to extract queryable values, then query those values with SQL:

```python
from fxtr2.data.store_indexing.indexers import TypedPathValueIndexer
from fxtr2.data.store_indexing.indexing_types import QueryView

# Register an indexer that extracts model_name from experiments
await ns.register_indexer(
    TypedPathValueIndexer.parse(f"!(as {EXP_TYPE}).model_name: str"),
    key_prefix="experiments:",
)

# Query with SQL
rows = await ns.sql(
    query="SELECT model, COUNT(*) as n FROM data GROUP BY model",
    views={
        "data": QueryView(
            "experiments:",
            model=TypedPathValueIndexer.query(f"!(as {EXP_TYPE}).model_name: str"),
        ),
    },
)
```

Path expressions like `"!(as '<type_id>').rollouts[$r: int]!.scores[$m: str]: float"` describe how to walk through entity references (`!` dereferences a link), iterate over collections (`[$r: int]`), and extract values (`: float`). Variables like `$r` and `$m` become columns in the generated SQL, enabling joins across different entity types.

See [`examples/store_indexing/`](examples/store_indexing/) for a complete example with multiple entity types, cross-entity joins, and both `sql()` and `iter_matches()` queries.

## Data Integrity Guarantees

Fxtr provides several guarantees about data:

**Content addressing.** Identical data always gets the same ID. If two people independently store the same entity, they get the same `FxtrID`. This makes deduplication automatic and enables hermetic caching.

**Immutability.** Once an entity is stored, its contents never change. The `FxtrID` is a permanent, stable reference.

**Complete subgraphs.** If you have a valid `FxtrID` in scope, you are guaranteed to be able to load that entity *and* everything it transitively references. Fxtr enforces this at the storage level: you cannot store an entity that references an ID that isn't already tracked. This means a single `FxtrID` is effectively a handle to an entire subgraph of data.

**Scope-based lifetime management.** Scopes control when entities are accessible. Within a scope, any entity that has been stored or loaded is guaranteed to remain loadable. When you `copy_to` or `lift` an entity into an outer scope, fxtr ensures the complete subgraph is available in that scope. When a scope exits, only entities that have been copied out survive.

**Roundtrip guarantee.** For any correctly-implemented entity, storing it and loading it back produces an entity that, when stored again, yields the same `FxtrID`. The serialized representation is deterministic.

## Type System

Every entity has a `$fxtr.type` field containing a `FxtrID` that identifies the entity's type. The type ID is itself a content-addressed entity — you can load it to inspect the type's definition:

```python
# Load raw CBOR to inspect the type field
raw = await scope.load_cbor(some_entity_id)
type_id = raw["$fxtr.type"].inner_id

# Load the type definition
type_entity = await EntityRef(type_id, FxtrBasicType).load()
print(type_entity.type_name)  # e.g. "experiment"
```

Currently, type definitions just carry a name (via `FxtrBasicType`). But because types are themselves entities, they can carry arbitrary data — schemas, links to related types, lists of implemented traits, default visualization instructions, and more. This extensibility is a key part of fxtr's design and is central to the planned type system features described below.

### Schema vs. Python representation

An important design principle: fxtr's type system is intended to enforce contracts on the *serialized* representation of entities (the CBOR/JSON-like dict), which may or may not be mirrored into Python. When you use `DataclassEntity`, the Python dataclass implicitly defines serializers and deserializers, so there is a natural correspondence between the Python fields and the serialized form. But fxtr does not require this one-to-one mapping:

- You can load any entity as `UnknownEntity`, which wraps the raw CBOR dict without interpreting its fields. This is useful for inspection, generic tooling, and forward-compatibility with types you don't have Python definitions for.
- You can register custom deserialization handlers for type aliases, allowing you to load the same serialized data as different Python types depending on context. The only requirement is that *storing* the resulting object produces the same serialized form (roundtrip guarantee).
- This flexibility will become particularly important as the trait system develops: you might want a Python class representing "any entity that implements this trait", exposing the trait-specified attributes while storing unrecognized fields in an `_extra_fields` property.

This is a significant improvement over fxtr v1, which structurally required a one-to-one mapping between serialized entities and their Python classes, with no way to customize the decoding logic.

### Union types

Union types are handled automatically. If you have entities of different types stored under the same namespace prefix, you can load them with a union type annotation:

```python
ref = EntityRef(some_id, Experiment | Result)
loaded = await ref.load()  # dispatches based on $fxtr.type
```

## Compatibility with Fxtr v1

Fxtr v2 includes a compatibility layer for running fxtr v1 task graphs on v2 storage. This is currently the primary way experiment pipelines use fxtr, and bridges the gap while the v2 task system is being built.

Key components:

- **`LegacyCompat[T]`**: Wraps v1 entity objects so they can be stored in v2's content-addressed system, preserving original v1 CIDs for verification.
- **`TransitionalSessionManager`**: Drop-in replacement for v1's `SessionManager` that runs v1-style tasks but stores results in v2 namespaces. Supports caching, dependency ordering, and concurrent execution.
- **`TaskWithPredecessors` / `RunWithPredecessors`**: V2 wrappers for v1 tasks and runs that explicitly track predecessor relationships as entity references, enabling the complete-subgraph guarantee.

See [`notes/migrating_to_transitional_sm.md`](notes/migrating_to_transitional_sm.md) for a migration guide. This compatibility layer will be phased out as v2's native task system matures.

## Future Work

The current system handles data storage, typed references, and querying. The following areas are planned or in progress:

### Protocols and Typed Computations

Currently fxtr tracks the types of *data* but not the types of *computations*. A major goal is to extend the type system to also describe protocols for services and call targets — essentially "what this function expects and what it returns" — so that computational processes can be described as data and version-controlled.

The idea is that a service would declare its own `$fxtr.type` for negotiation, and callers would check whether the service's declared type satisfies the protocol they need, without both sides needing to coordinate in advance. Combined with the trait system (below), this would allow you to describe a service like "a numeric judge for this type of input" as a typed, version-controlled entity, and then look up whether any given service satisfies that protocol by inspecting its type.

### Subtyping and Traits

The type system is planned to support subtyping relationships inspired by Rust traits and Haskell typeclasses. The key insight is that content-addressed IDs enable a decentralized "orphan rule":

- A type can declare that it implements a trait by including the trait's hash in its type definition.
- A trait can retroactively declare that existing types or traits implement it by referencing their hashes.
- Because hashes uniquely identify the definition, there is no ambiguity about ordering or ownership, and no compiler is needed to resolve them jointly.
- At runtime, fxtr's complete-subgraph guarantee means you can always check whether a type implements a trait by loading both type entities and checking both directions. This can be cached for efficiency.

This is intended to work for both data and services. For data, you might define a trait like `NumericJudgeResult` that implies the type has a `score` field containing a numeric score. For services, you might define a trait like `NumericJudge[T]` that says the service has a `run_judge(input: T) -> NumericJudgeResult` method, where the runtime value of the type parameter `T` is attached as a field inside the service's `$fxtr.type`. A consumer could then inspect a service's type to determine both that it implements the `NumericJudge` protocol and what type of input it judges — and similarly inspect the output data's type to confirm it has the expected `score` field.

### Provenance Tracking

Fxtr's data model includes infrastructure for automatic provenance tracking via the `$fxtr.ctx` field and provenance node types (`n`, `c`, `p`, `u`). When fully built out, this will allow entities to be automatically tagged with context about how they were produced — which task generated them, what configuration was used, what execution context they ran in — without the application code needing to manually thread this information.

The ID system already supports this: a `FxtrID` can carry an optional `_provenance_parent` that tracks the provenance-level identity alongside the data-level identity, transparent to equality comparisons. The scope system currently supports passing around entities that have attached provenance (when created manually via the low-level arena system), but does not yet attach provenance automatically or inspect `_provenance_parent`. The intent is that eventually there will be provenance-aware scopes that automatically attach provenance metadata as entities are stored and loaded, and tracked computations would run inside one of these scopes.

### Capability-Based RPC

The `fxtr2/rpc/` module contains a work-in-progress capability-passing RPC system (Cap-IR), intended to allow implementations of fxtr protocols to be sent over the wire without losing their type information. This is an extensible variant of [Cap'n'Web](https://github.com/cloudflare/capnweb/blob/main/protocol.md) that will replace the more rigid interface system in fxtr v1. See [`fxtr2/rpc/LOCAL_CODE_OVERVIEW.md`](fxtr2/rpc/LOCAL_CODE_OVERVIEW.md) for details.

### Native Task System

The v1 task/plan/session system (currently accessed via the compatibility layer) already supports content-addressed result caching, but is difficult to use and lacks a good user API. The plan is to rebuild this natively in v2, integrating with the much more powerful v2 storage and scope system, using the type system for describing task contracts, and using the provenance system for tracing results back to the tasks that produced them.

## Project Structure

For a detailed breakdown of every module and file, see [`CODE_OVERVIEW.md`](CODE_OVERVIEW.md). Key directories:

| Directory | Description |
|-----------|-------------|
| `fxtr2/data/entity_core.py` | `FxtrID`, CBOR encoding, node type definitions |
| `fxtr2/data/entity_ref_scope.py` | `Entity`, `EntityRef`, `FxtrScope` — the core API |
| `fxtr2/data/fxtr_context_api.py` | `SQLiteFxtrNamespace`, `TsvFxtrNamespace` — persistent storage |
| `fxtr2/conversion/converters/mashumaro_entities.py` | `DataclassEntity`, `Child`, `ChildRef`, etc. |
| `fxtr2/data/store_indexing/` | Index construction, path expressions, SQL query generation |
| `fxtr2/data/sqlite_store/` | High-performance SQLite + segment file storage backend |
| `fxtr2/data/tsv_namespace/` | TSV-file fxtr namespace (git-tracked TSV files + repo-level SQLite entity store) |
| `fxtr2/rpc/` | Capability-passing RPC system (WIP) |
| `fxtr2/legacy_compat/` | Fxtr v1 compatibility layer |

## Learning More

- **[`examples/simple_store_and_load.py`](examples/simple_store_and_load.py)** — Guided tour of the core features: defining entities, storing and loading, scopes, namespaces, type inspection, and SQL queries. **Start here.**
- **[`CODE_OVERVIEW.md`](CODE_OVERVIEW.md)** — Detailed description of every module, including internal concepts like arenas, node types, and CBOR encoding.
- **[`notes/roundtrip_serdes.md`](notes/roundtrip_serdes.md)** — Deep dive into the serialization/deserialization design, including union types, inline entities, and the two-phase deserialization process.
- **[`notes/on_protocols_negotiation_contracts.md`](notes/on_protocols_negotiation_contracts.md)** — Design document on fxtr's approach to protocols, schemas, and API contracts.
- **[`notes/migrating_to_transitional_sm.md`](notes/migrating_to_transitional_sm.md)** — Guide for migrating from fxtr v1's SessionManager to v2's TransitionalSessionManager.
- **[`examples/store_indexing/`](examples/store_indexing/)** — Complete example with multiple entity types, cross-entity joins, and both `sql()` and `iter_matches()` queries.
- **`tests/`** — The test suite (1300+ tests) is a good source of usage examples for specific features.
