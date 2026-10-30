hey claude, can you help me think through something? I have this entity serialization system in @fxtr2/data/entity_serdes.py ; you can read more about the overall codebase in the @CODE_OVERVIEW.md . right now serialization and deserialization of entities is both synchronous and doesn't get access to the scope that it is being (de)serialized in. but, you could de-serialize to a EntityRef, which could then be loaded. I'm wondering if there would be an easy way to expand this system to allow the deserializer itself to do an async load from the scope where its parent's _from_fxtr_cbor is being invoked from. roughly essentially that would mean a few major changes:
- we need to plumb in the active scope through everything that is involved in loading from an EntityRef, so:
    - into the "converter" of an EntityRef, so that the converter receives the scope
    - into _from_fxtr_cbor
- we ideally want some way for mashumaro to trigger an async load operation when doing `_from_fxtr_cbor` on a DataclassEntity. a simple way to do this would be via a mechanism similar to the current deep_load although perhaps somewhat different. what I'm imagining would be something like:
    - have a module-level contextvar that handles recursive (de)serialization, which can be in one of three states during deferred loading:
        - disabled: value is None
        - collecting: value is a RecursiveLoadCollecting(collected: list[tuple[EntityRef, TypeAnnotation]]) (where TypeAnnotation is just Any but it means the value is a type annotation)
        - rebuilding: value is a RecursiveLoadRebuilding(rebuilt: list[tuple[EntityRef, TypeAnnotation, Entity]], next_index: int)
    - have a special type annotation serialization strategy similar to the current EntityRefSerializationStrategy that, when deserializing:
        - grabs the module-level contextvar
        - if its disabled, throws an exception
        - if it is in collecting mode, it deserializes like an EntityRef but then pushes that EntityRef into the collected list instead of returning it, and then returns a sentinel object of some sentinel type DeferredReadPlaceholder that wraps the EntityRef (this sentinel object is just some frozen dataclass)
        - if it is in rebuilding mode, it checks the value at the given index, and makes sure it both refers to the same FxtrID and has the same type annotation. It then increments the index and returns the extracted entity (from the prev index)
    - define _from_fxtr_cbor to first try to build the value while setting the module-level contextvar to a `RecursiveLoadCollecting`, then see if the RecursiveLoadCollecting collected anything. if it didn't, just return the value. if it did, load all collected refs in parallel, then re-run the loading in RecursiveLoadRebuilding mode.
- we also need to plumb the scope into everything involved in storing an Entity, including `_to_fxtr_cbor`, to enable the symmetric handling on the other side
- then for `_to_fxtr_cbor` on DataclassEntity, we set the same recursive (de)serialization contextvar to a new `RecursiveStore(scope)` value. we don't have to do two phases because storing is synchronous; instead, when we encounter a RecursiveStore, we briefly re-set the contextvar to None in a block, and in that block we do a store of the given entity (with the given type annotation), take its EntityRef, and pull out the FxtrID

some other things we could also do:
- to make this work nicely, we introduce two new type aliases `InlineChild[T] = T` (a strong link) and `InlineEntity[T] = T` (a weak link), which can be used inside a DataclassEntity and trigger the appropriate (de)serialization logic
- add tests to cover both custom entities that load/store other entities and DataclassEntity subclasses that use these type aliases
- lets also rename the `StrongLinkedEntityRef` to `ChildRef` and `StrongLinkedFxtrID` to `ChildFxtrID`, and remove the unnecessary `WeakLinkedFxtrID` and `WeakLinkedEntityRef` aliases since they are identical to the default behavior, and also remove the `Annotated` serialization strategies because it turns out these are fragile and don't trigger when you might expect. we can update the tests to match; no need to worry about consumers outside this repo.

does this make sense? can you read the code and overview docs and ask me any questions to clarify how this should work and what needs to be added? then please write a plan in @plans/phase2/stage3_recursive_entitites.md

~~~

1. I think the naming is a bit vague but I'm not sure the alternatives are better. If you're bothered by "inline", I guess an alternative could just be `Child[T]` (strong) and `LinkedEntity[T]` (weak), since python type annotations already make it clear that it is "inline" when deserialized which is all that inline is supposed to mean. what do you think?
2. hm that's a good point. we could use ACTIVE_SCOPE, but if so we need to be really careful that we override ACTIVE_SCOPE at any place where _from_fxtr_cbor/_to_fxtr_cbor is called, so that the manually-provided scope given to `load` or `store_entity` is used instead of whatever other scope happened to be active at the time. although, there is already going to be a breaking change because `_from_fxtr_cbor` will have to become async in general (it's just DataclassEntity that does the sync wrapper). although, hm, maybe we can patch this by allowing `_from_fxtr_cbor` to either return the type `T` or return `Awaitable[T]`, and then the caller can await it if it turns out to be async?
3. I think we just accept that users who use these placeholders will need to expect to see them during validation. if they do __post_init__ stuff they need to make sure they don't error out when seeing the placeholders. this should be fine for now.
4. yes, recursive loads could involve many nested load phases, that's fine
5. lets fail fast, keep it simple.
6. it is just a somewhat tricky mashumaro behavior, you need to literally use this annotated form for it to detect it. I just think it's unnecessary to have two ways to do it if both require exactly specifying a specific form in the type annotation rather than doing deeper type inference. (essentially, mashumaro is looking for exact matches of the annotated type and doesn't seem to do much beyond this. basically I just want to simplify)
7. ah we can just reuse ACTIVE_SCOPE here too but we do need to be careful to override it when calling _to_fxtr_cbor internally.
8. I think the converter may have to become async, in case it triggers more loads? since the converter is mostly internal-facing I lean toward having the converter take the scope as an argument and internally override the ACTIVE_SCOPE when implemented, just to make it clear it is parameterized by the scope. then the logic for building converters can also handle the ACTIVE_SCOPE setup and awaiting business.


---

(fxtr1 code overview gen for entity_core)

(fxtr1 code overview gen for session_manager)

---

hey Claude, can you help me think through something? I have two versions of the fxtr library, a v1 that I built a while back and a v2 that I am now building to be more flexible. v1 has a task management system and sits on top of a system of "entities" that have very specific CBOR serialization rules. v2 is supposed to be more flexible, it also has entities and CBOR serialization, but it is designed so that the CBOR-serialized form is the canonical form, and it is up to the user to encode how they serialize and deserialize types. I'd like to implement a compatibility layer.

the way I'm imagining this working is that there would be a submodule `fxtr2.data.legacy_compat`, which has a subclass of new-fxtr's Entity base class which delegates to old-fxtr's CBOR serialization logic, including the typespec system. lets call it `LegacyCompat[T]`, and say that it wraps a legacy Entity object as its value, but then hooks into the recursive custom entity (de)serialization system to change how it gets serialized according to the fxtr2 system.

the way the serialization should work should be that the fxtr2-serialized form of a fxtr1 entity is computed as follows:
- take the fxtr1 entity
- convert it to CBOR and also collect all of the children that it wants to store and refer to via their DAG-CBOR CIDs
- compute its own DAG-CBOR CID in the normal fxtr1 way
- recursively take all things being linked to by this fxtr1 entity, convert them via this procedure, and store them, saving their fxtr2 FxtrIDs
- now, take its CBOR, and replace every DAG-CBOR CID with the corresponding fxtr2 FxtrID as a strong link. this is the _payload_.
- now create the fxtr2 entity as a basic bare entity following fxtr2 rules, where
    - the $fxtr.ctx is the active context (ground .) as normal
    - the $fxtr.type is the builtin id "!compat.legacy_v1"
    - the field "data" is the DAG-CBOR of the entity after doing the replacement procedure
    - the field "legacy_cid" is the DAG-CBOR CID of the original fxtr1 entity (based on unconverted CBOR)
- store this into the scope

deserialization should then be the reverse of this:
- extract the fxtr2 entity data
- walk all children and load them recursively using this procedure. make sure all of them load as "!compat.legacy_v1"
- take their CIDs and substitute them into the fxtr2 data in place of the original strong links
- use the fxtr1 entity deserialization hooks to recreate the fxtr1 entity
- finally, compare the stored legacy_cid with the actual CID of the restored object; if it doesn't match, raise an exception

does this make sense? can you read the code and overview docs and ask me any questions to clarify how this should work and what needs to be added? then please write a plan in @plans/phase2/stage4_fxtr1_compat_entities.md

note that fxtr1's object_model/core.py is _very_ long; you should never try to read the whole file, but instead just look at parts you need. if you think you'll need to do something across this file please ask me first.

---

(fxtr2 version of task, run, provenance, anywhere where a builtin refers to some data we expect to have but uses a cid to do it)
