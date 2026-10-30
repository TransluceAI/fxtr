# Step 1 Post-2: Learnings

Implementation-specific notes from the entity reorganization. For general entity design guidance, see `v2/notes/representing_data_as_entities.md`.

## `DataclassFxtrSubpart` discovery

The tracking types (`ForeignSlotRef`, `InternalSlotRef`, etc.) were originally `DataclassEntity` with `Child[T]` wrappers. When demoting them to plain inline types, using mashumaro's `DataClassDictMixin` failed because it can't serialize `EntityRef` or `FxtrID`. The fix was `DataclassFxtrSubpart`, which carries fxtr2's serialization strategies.

## `LiveCacheSlot` state: `EntityRef` vs loaded entity

The original implementation stored the loaded entity directly (`_loaded_state`) and re-stored it to get its ID (in `build_slot_deps` and `replay_check`). Switching to `_state_ref: EntityRef[SlotState]` avoids the re-store, but introduced a bug: `replay_check` read `state_ref` on a freshly constructed `LiveCacheSlot` (from `_resolve_slot_ref`) before `_load_state()` had been called, so it was always `None`. The fix was to call `_load_state()` before reading `state_ref`.

## `frozendict` union ordering

When defining `FrozenCacheKeyData`, the initial ordering had `tuple` before `frozendict`. This caused mashumaro to serialize frozendicts as tuples of their keys, producing identical content hashes for different data. The fix was putting `frozendict` before `tuple` in the union. This is documented in the general guide.
