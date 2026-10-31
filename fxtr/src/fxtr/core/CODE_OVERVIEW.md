# Entities and storage — code overview

How fxtr identifies, serializes, stores, and loads values. This covers the entity core (`core/`)
and the storage engines (`../storage/`). The dataclass layer most entity types are defined with,
and the value types built on it (schemas, arrays, queries), are in
`../entity_defns/CODE_OVERVIEW.md`; how this fits the rest of fxtr is in
`fxtr/CODE_OVERVIEW.md`.

```
fxtr/
├── core/                   foundational types, codec, and loader resolution
│   ├── entity_data.py      the serialized surface: EntityID, data-value aliases,
│   │                       $type tags, FxtrEntityDataStorage
│   ├── entities.py         the typed surface: Entity, BoundID, EntityLoader, UnionEntityLoader,
│   │                       FxtrEntityStorage
│   ├── codec.py            DAG-CBOR / DRISL encode/decode + CID computation (cbrrr-backed)
│   ├── car.py              CAR v1 files of entity blocks (write_car, read_car; gzipped, each
│   │                       block checked against its cid)
│   ├── json.py             recursive JSONValue / JSONObject aliases shared with JSON codecs, plus
│   │                       hashable FrozenJSONValue / FrozenJSONObject counterparts
│   └── loader_registry.py  annotation → loader resolution: the generic
│                           TypeAnnotationHandlerRegistry and the EntityLoaderRegistry
└── storage/                concrete storage engines and shared helpers
    ├── util.py             helpers shared by engines: encode_for_store (validate + encode a value)
    └── memory_storage.py   MemoryStorage (in-process dict)
```

fxtr identifies every value by its content. A value has a canonical serialized
form — a **`FxtrDataValue`** (`core/entity_data.py`), the runtime shape that maps
directly onto DAG-CBOR / DRISL: `None | int | float | bool | str | bytes |
EntityID | list | dict[str, ...]`. Serializing that form and hashing it yields a
content ID, so identical content always gets the same ID.

The core is layered: `core/entity_data.py` is the *serialized* surface (ids, data
values, raw storage), `core/entities.py` is the *typed* surface (entities,
references, loaders), and `core/loader_registry.py` resolves *type annotations*
to loaders. References carry loader objects — never annotations — so the typed
surface never consults the registry; that property keeps the layering acyclic,
and no annotation-taking convenience should be added to `core/entities.py`.

## Identity

**`EntityID`** (`core/entity_data.py`) — an identity: a `cid` (a DASL CID: the
binary `01 <codec> 12 20 <sha-256>` form) and nothing else. It is a class of its
own rather than bare `bytes` so that a reference inside a data value is
distinct from a byte string.

**`$type` tags / type ids** (`core/entity_data.py`) — every stored entity is a
mapping carrying the reserved `TYPE_KEY` (`"$type"`) field holding its *type
id*: a non-empty string, by convention a reverse-domain name
(`org.transluce.fxtr.core.Array`). The string is the whole of what the
framework knows about a type; what it means (a schema, a version) is left to
the program that defines it. `get_entity_type_tag` extracts and validates the
tag; storage engines enforce it on store and on load, raising
`InvalidEntityDataError`.

## Serialization

**`StorableFxtrDataValue`** (`core/entity_data.py`) — the wide input union for
storing: mutable and frozen shapes (and any mix) are both subtypes, via
covariant `Sequence`/`Mapping` members. The design principle is
*wide inputs at the sink, canonical outputs at the source*: store methods
accept any storable shape (all shapes of one logical value store to the same
id), while load methods return the canonical narrow shapes.

**`core/codec.py`** — the content-addressing layer, and the single place that
knows the wire format. `encode` / `decode` serialize data values to and
from strict DAG-CBOR / DRISL (via the `cbrrr` library, converting references
to/from `cbrrr.CID` at the boundary), and `compute_cid` hashes encoded bytes
into a CID; `encode` accepts the full storable union. Traversal is
**canonical**: maps are walked in DAG-CBOR key order (length-first, then
bytewise — `canonical_map_items`), so `iter_entity_ids` yields a value's
direct references in wire order, dependent only on the logical value.

## The storage contract

**`FxtrEntityDataStorage` / `FxtrEntityStorage`** (`core/entity_data.py` /
`core/entities.py`) — the storage contract, split across the two surfaces.
The raw ABC has async `store_raw_data` (takes any `StorableFxtrDataValue`,
returns an `EntityID`), async `load_raw_data`, and sync `is_known`;
`FxtrEntityStorage` extends it with the typed async `store`/`load` wrappers:
`store(entity)` returns a `BoundID`, and `load` takes a bound id, or any id
plus `as_type=<annotation>` or `with_loader=<loader>` (at most one; either
overrides a bound id's own loader). Store and load are async in the
*interface* so an engine may sit behind a process or network boundary and
delegate the work, ids included; an engine that can complete a store without
waiting simply does (`MemoryStorage`, the write-behind Postgres store) and
may expose sync twins for in-process callers that cannot await
(`MemoryStorage.store_sync` / `store_raw_data_sync`). The structural
invariant: a value referencing another id can only be stored once that id is
known, so any loadable id transitively resolves its whole subgraph. Engines
also enforce the `$type` tag on stored and loaded data.

## Entities, bound ids, and loaders

**`Entity` / `BoundID` / `EntityLoader`**
(`core/entities.py`) — an `Entity` knows how to serialize itself
(`_fxtr_serialize`, to a tagged data value) and which loader deserializes it
(`_fxtr_loader`); `FxtrEntityStorage.store` does the storing, the same way
for every engine, and yields a `BoundID` that bundles the `EntityID` with
that **explicit** loader (`EntityID.bind_type` / `bind_loader` build one from
a bare id). The classmethod `Entity.fxtr_type_id()` reports the type id a
class's instances are tagged with, or `None` when the class does not know it
statically (abstract classes, `UnknownEntity`); tagged dataclasses answer it
from their `fxtr_type`. `EntityLoader` is an ABC with a pure conversion
primitive, `convert(data)` (handed the entity's data in any storable shape,
never loading anything itself), a sync *type-support check*
`supports_type(type_id: str)`, and an optional `static_type_id` property
naming the one type a loader converts when that is known up front (`None` by
default and for union and wildcard loaders). Two symmetric purity rules
follow: serialization never stores and conversion never loads, so the storage
engine is the one place fetching and writing happen, and entities and loaders
work the same against any engine or none. The support check makes loaders
composable: **`UnionEntityLoader`** dispatches a conversion among member
loaders by the data's `$type` tag, requiring exactly one supporter
(disjointness enforced at dispatch time — `NoMatchingVariantError` /
`AmbiguousVariantError`). `StaticTypeLoader` derives the support check and
`static_type_id` from one declared type id, the common case.
`Entity.content_id()` is the id an entity has as a stored entity, computed
locally with the codec: what every store returns for it, and what
content-addressed keys that are never stored — a runner node's identity, a
step request's fingerprint — are.

**`core/loader_registry.py`** — turns type annotations into loaders. The
generic `TypeAnnotationHandlerRegistry` maps keys (classes, PEP-695 type
aliases, `NewType`s) to handlers, resolving annotations by unwrapping
aliases/`Annotated`, substituting type parameters through parameterized
aliases, splitting unions per branch, and walking MROs — with strict conflict
rules: a more specific handler wins only over entries registered with
`allow_implicit_overrides=True`; anything else is an error, unless the
annotation opts out explicitly via the `OverrideFxtrHandlers[...]` sentinel
alias. The entity layer, `EntityLoaderRegistry`, holds `EntityLoaderFactory`
entries (`build_loader(annotation) -> EntityLoader`) and exposes
`loader_for(annotation)`, composing union branches into a
`UnionEntityLoader`; results are cached per annotation so equal annotations
share loader objects. `DEFAULT_ENTITY_LOADER_REGISTRY` is the shared default;
conventions: register at import time, alongside the type's definition, only
for types you own. `EntityID.bind_type(annotation)` is the one
annotation-taking convenience on the id type; it imports
the registry at call time so the core layering stays acyclic in import terms,
and `bind_loader(loader)` pairs an id with an explicit loader.

## The engines

**`MemoryStorage`** (`storage/memory_storage.py`) — the reference storage
engine: a dict from content id to serialized bytes. Everything in the dict is
"known". Its `store_raw_data` is `encode_for_store` (`storage/util.py`) plus a
dict update: `encode_for_store(data, is_known)` is the store-side half of the
contract shared by engines — it validates the `$type` tag, requires every
reference to be known, and returns the value's cid and an `EncodedEntity`
(bytes and the `$type` tag) to write.

## Where to change things

- **The wire format:** `core/codec.py` is the only module that knows it.
- **A new storage engine:** subclass `FxtrEntityStorage` and build its store on
  `storage/util.py`'s `encode_for_store`, as `MemoryStorage` does. The Postgres runner's write-behind store is
  `runner/postgres/entities.py`.
- **How an annotation maps to a loader:** `core/loader_registry.py`; a type registers its own
  loader factory at import time, beside its definition.

## Tests

- `tests/test_codec.py`: the DRISL codec: determinism, round trips, CID format, spec vectors.
- `tests/test_core_car.py`: CAR files: the round trip, gzipped or plain, and the refusals.
- `tests/test_storage.py`: the storage contract: store/load, the known set, `$type` enforcement,
  `UnknownEntity`.
- `tests/test_loader_registry.py`: annotation walking, override rules, `EntityLoaderRegistry`.
