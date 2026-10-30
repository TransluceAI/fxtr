# Entity Caching in Fxtr - Design notes

In low-level fxtr: Everything is CBOR. Could cache cbor bytes, possibly, but not much gains from this.

In higher-level fxtr: Entities get deserialized into objects. We might want to cache those objects. This works via the EntityRef abstraction.

Every Entity python type has a _fxtr_immutable flag which indicates that it is immutable. If an entity declares itself as such, we are free to cache it, and return the same entity. However, there are a few complications:
- Loading an EntityRef has side effects: it also grants ownership to linked entities
- Some EntityRefs, when loaded, recursively load their children.
- We may re-create the same EntityRef multiple times, and ideally these would share the cache.

## Previous design:
- EntityRef had a converter as part of its definition
- Each EntityRef stored a cached Entity, which it would use to load itself again
- EntityRefs checked if their own ID was safe to cache; if so they returned the cached entity
- Entities could load themselves recursively by freely reading/writing ACTIVE_STORE

## Flaws in the design:
- Multiple equal EntityRefs would not necessarily share an identity, especially if loaded by independent entry points, meaning they would not share a cache of immutable entities
- If an entity recursively loaded more entities, we would not check if those later entities were safe to cache, potentially leaving the scope in an inconsistent state due to caching
- Converters were part of the definition of the EntityRef, and determined its identity, even though logically the EntityRef is supposed to be keyed by a target type; __orig_class__ was used as a proxy for the original type but incompletely

## Modified design
- Change EntityRef to store the target _type_ rather than the converter, and build converter only when needed
- EntityRef global weak cache: Always at most one EntityRef for a given type at a time
- Entity loading log: Track all scope loads while loading an EntityRef; replay them as cached loads
- Explicit serialization/deserialization scopes: _to_fxtr_cbor and _from_fxtr_cbor take explicit scope to load from or store into, and these scopes are restricted to only allow the respective operations needed
- (minor: remove Entity from FxtrSendable, as it causes problems with recursive entities and isn't used significantly)
- (minor: rename `EntityRef.ref` to `EntityRef.entity_id`)

## Implementation steps
- [x] Rename `EntityRef.ref` to `EntityRef.entity_id`
- [x] Modify `EntityRef` to use `target_type`
- [x] Fix tests to refer to `EntityRef.entity_id` rather than `EntityRef.ref` and to use `target_type`
- [x] Implement rest of modified design: global cache, loading log, explicit ser/des scopes, remove Entity from FxtrSendable
- [x] Update notes/roundtrip_serdes.md to describe the new modified design
- [x] Update broken tests to test new design; possibly fix issues we encounter along the way
- [x] Update CODE_OVERVIEW so that it is up to date
- [x] Add new tests for new aspects not previously covered:
    - Global cache of entity refs:
        - loading two EntityRef with same immutable target type gives same cached result (same python ID)
        - loading EntityRef(x, target_type=Foo) and EntityRef(x, target_type=(Foo|Bar)) gives same result (same python ID) in either order
        - (note: make sure global cache is empty before running each test and fail if not; this indicates a test isolation failure. the weakref cleanup should ensure it is empty already, but we should be sure)
        - loading EntityRef(x, target_type=Foo1) and EntityRef(x, target_type=Foo2) gives _different_ results if Foo1 and Foo2 are different instances of the class that x deserializes to (and each is an instance of its expected type)
    - Interaction of caching and recursive loading:
        - if entity A contains entity B as a `Child` (loaded recursively), and B contains an EntityRef to C, and we load A, both B and C should become tracked in the store
        - then if we abandon B and C in the store, B and C should not be tracked
        - then if we load entity A again:
            - we should get the _same_ object back (it is cached)
            - but we should again have B and C tracked in the store (we replay the loads)
    - (anything else to add?)
