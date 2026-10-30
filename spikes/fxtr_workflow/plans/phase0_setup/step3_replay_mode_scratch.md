# Step 3: Replay Mode — Scratch Notes

## `_subslots` dict refactor

### Problem
Currently `_subslots: dict[str | int, LiveCacheSlot]` mixes two things:
- Subslots: key is the short string key (e.g., `"foo"`)
- Internal cache slots: key is the full pin key (e.g., `"root/c/foo"`)

This makes it hard to enumerate internal cache slots for freezing.

### Solution
Split into two dicts:
```python
_subslots: dict[str, LiveCacheSlot]           # subslots only, short key
_internal_cache_slots: dict[str, LiveCacheSlot]  # internal cache slots only, short key
```

Changes:
- `subslot()` unchanged (already uses short key in `_subslots`)
- `_internal_cache_slot(key)` uses `_internal_cache_slots` dict with short key instead of full pin key
- Frozen tree builder iterates `_internal_cache_slots.items()`

This is a pure internal refactor — no behavior change, existing tests should pass.

## Probe/realize/reject API

### Motivation
Both `_execute_in_slot` and `replay_check` need to:
1. Ask a slot "what state do you have?" (checking reference if empty)
2. Either accept it (realize if from reference) or not

They diverge on rejection: dispatch calls `reject()`, replay_check just returns False.

### API

```python
class LiveCacheSlot:
    # Internal state for probe caching
    _probed_state: SlotState | None
    _probe_source: Literal["local", "reference"] | None  # None = not yet probed

    async def probe_state(self) -> SlotState | None:
        """Determine the slot's effective state.

        Checks local state first. If empty and has replay reference,
        checks the reference. Does NOT modify this slot.
        Caches the result internally (including source tracking).
        """

    async def realize(self) -> None:
        """Commit to the probed state.

        If probed state came from reference: copies the state entity
        (and its full subgraph) from the reference namespace into this
        slot's namespace. If local: no-op.
        """

    async def reject(self, reason: str) -> None:
        """The probed state doesn't match what we need.

        The slot handles all error logic internally based on what it knows:
        - Local state: always raises CacheSlotError (write-once/structural).
        - Reference state + strict: raises CacheSlotError.
        - Reference state + best-effort: drops the replay reference and
          sets up internal cache slot references for re-execution:
            - Reference was Finished: loads frozen tree for FrozenCacheSlot refs
            - Reference was Partial: keeps ref for LiveCacheSlot pin walking
            - Clears the replay reference for this slot's own state

        The reason parameter provides context for error messages.
        """
```

### Key insight: source tracking is internal

The slot already knows whether the probed state came from local storage or from
the reference. Callers don't need to know — they just call `reject()` and either
get an exception (local or strict) or a clean setup for re-execution (best-effort).
This centralizes error management and eliminates source-branching in the caller.

### Usage in replay_check

```python
async def replay_check(slot_deps, current_cache_slot_args, step_slot):
    for tracked_run in slot_deps.runs:
        current_slot = _resolve_slot_ref(tracked_run.run_location, ...)

        state = await current_slot.probe_state()

        if state is None:
            return False

        expected_id = tracked_run.observed_finished_ref.entity_id
        actual_id = ...  # entity ID from slot's internal tracking
        if actual_id != expected_id:
            return False

        # Match! Realize it (copy from reference if needed)
        await current_slot.realize()

        # Recursively check inner deps...
        ...

    return True
```

Note: replay_check never calls `reject()`. If there's a mismatch, it just returns False. The caller (`_execute_in_slot`) decides what to do.

### Usage in _execute_in_slot

```python
async def _execute_in_slot(self, slot, step, raw_args, raw_kwargs, fallible):
    args, kwargs = await resolve_args(raw_args, raw_kwargs)
    cache_key_ref = compute_cache_key(...)

    async with slot._lock:
        state = await slot.probe_state()

        if isinstance(state, SlotFinished):
            if state.cache_key_ref.entity_id == cache_key_ref.entity_id:
                # Cache key matches — check result type and replay check
                result_entity = await state.result_ref.load()

                if isinstance(result_entity, StepError):
                    await slot.realize()
                    raise _reconstruct_fallible_error(result_entity)

                # StepOutput — do replay check
                ...
                check_ok = await replay_check(slot_deps, current_slots, slot)
                if check_ok:
                    await slot.realize()
                    return result_entity.value
                else:
                    await slot.reject(reason="replay_check_failed")
                    # strict/local: raised. best-effort: fall through
            else:
                await slot.reject(reason="cache_key_mismatch")
                # strict/local: raised. best-effort: fall through

        elif isinstance(state, SlotPartial):
            if state.cache_key_ref.entity_id == cache_key_ref.entity_id:
                await slot.realize()  # copy partial + checkpoint if from reference
                # fall through to re-execute
            else:
                await slot.reject(reason="cache_key_mismatch")
                # strict/local: raised. best-effort: fall through

        elif isinstance(state, SlotSplit):
            await slot.reject(reason="structural_mismatch")
            # strict/local: raised. best-effort: fall through

        # state is None or we fell through from a best-effort reject
        # → Transition to Partial and execute
        await slot._save_state(SlotPartial(cache_key_ref=cache_key_ref))

    # ... rest of execution logic ...
```

No source-branching in the caller. `reject()` handles everything internally.

### The step_slot for replay check with references

When the probed state came from a reference, the replay check needs to resolve `InternalSlotRef` entries. These reference internal cache slots of the step's run. The internal cache slots are in the reference's namespace (either as frozen tree nodes for Finished, or as live pins for Partial).

We need a "step_slot-like" object that, when `_internal_cache_slot(key)` is called, returns something that can be probed. Options:

1. **Use the current slot**: The current slot has the replay reference. Its `_internal_cache_slot(key)` could return a child with the reference's internal cache slot as its replay reference. Then probing the child would find the reference state. This is the cleanest because it uses the same probe mechanism.

2. **Use the reference directly**: Pass the reference as step_slot. But then realization would happen in the reference namespace, not the current one.

Option 1 is better. The current slot's `_internal_cache_slot(key)` creates a LiveCacheSlot child. If we set up the child's replay reference to point at the reference's corresponding internal cache slot (LiveCacheSlot for Partial reference, FrozenCacheSlot for Finished reference), then probing/realizing the child naturally copies from reference to current namespace.

This means `_internal_cache_slot` needs to know about the replay reference's internal structure. This is where the "lazy loading of frozen tree" comes in:
- If replay reference is Finished: load frozen_internal_slots_ref, look up key, create FrozenCacheSlot as child's replay reference
- If replay reference is Partial or Split: use `reference._internal_cache_slot(key)` (LiveCacheSlot) as child's replay reference
- If replay reference is Empty or None: no reference for child

So we pass the CURRENT slot as step_slot to replay_check, and the current slot's internal cache slot children have replay references that point to the right place in the reference. Probing those children finds the reference state, and realizing them copies into the current namespace. This is clean.

### Internal cache slot reference resolution (eager)

We don't need a separate `_get_replay_check_slot` function. We pass the current slot as step_slot to replay_check. The current slot's `_internal_cache_slot(key)` returns a child with the right replay reference.

The reference's internal structure is resolved eagerly by `probe_state()`:
- `probe_state()` loads the reference's state. If Finished, it loads the `FrozenInternalSlots` entity and caches it (e.g., as `_cached_ref_internal_slots`).
- `reject()` (best-effort) does the same if not already done, and stores it in `_rejected_internal_ref`.

Then `_internal_cache_slot(key)` (sync) just looks up the cached structure:
- If `_cached_ref_internal_slots` or `_rejected_internal_ref` has a `FrozenInternalSlots`:
  look up key → `FrozenCacheSlot` as child's replay reference
- If the reference was Partial: `reference._internal_cache_slot(key)` (sync) → `LiveCacheSlot` as child's replay reference
- Otherwise: no reference

No deferred mechanism needed. The async loading happens in `probe_state()`/`reject()` which are already async, and the results are cached for synchronous lookup in `_internal_cache_slot`.

## Frozen tree building details

### What gets frozen

Only internal cache slots that were accessed during this execution. These are exactly the entries in the slot's `_internal_cache_slots` dict (after the refactor).

### Recursive freezing

For each internal cache slot in `_internal_cache_slots`:
1. Load its state
2. If it has its own `_subslots`, recursively freeze those into `FrozenSlotNode.subslots`
3. If it has its own `_internal_cache_slots` — these would already be frozen into its own SlotFinished (if it finished). So we don't need to explicitly freeze them; they're captured through the entity subgraph.

Wait, but what if an internal cache slot has a Partial state? Its internal sub-slots wouldn't be frozen (they're live pins). Do we freeze those too?

I think we freeze whatever state exists. If it's Partial, we freeze the Partial state. The sub-slots of a Partial are in an indeterminate state (the run didn't complete), so it's not clear we should freeze them. But the user said we freeze "exactly the slots that were actually run, observed, or realized during the run that finished."

I think "observed" means "accessed during this step's execution." So if the step accessed `cache_slot(0).subslot("a")`, that subslot was observed and should be frozen.

But subslots of internal cache slots would be in the internal cache slot's own `_subslots` dict, and we'd walk those recursively. So:

```python
def _build_frozen_node(slot: LiveCacheSlot, scope: FxtrScope) -> EntityRef[FrozenSlotNode]:
    state_ref = slot.state_ref  # already loaded during execution
    subslots = {}
    for key, child in slot._subslots.items():
        subslots[key] = _build_frozen_node(child, scope)
    return FrozenSlotNode(
        state_ref=state_ref,
        subslots=frozendict(subslots),
    ).store(scope)
```

And at the top level:
```python
internal_slots = {}
for key, child in step_slot._internal_cache_slots.items():
    internal_slots[key] = _build_frozen_node(child, scope)
frozen = FrozenInternalSlots(slots=frozendict(internal_slots)).store(scope)
```

### Pin cleanup

After freezing, delete pins for the frozen slots:
```python
def _collect_pins_to_delete(slot: LiveCacheSlot) -> list[str]:
    pins = [slot.pin_key]
    # Also checkpoint pin
    pins.append(f"{slot.pin_key}/ckpt")
    for child in slot._subslots.values():
        pins.extend(_collect_pins_to_delete(child))
    return pins

for key, child in step_slot._internal_cache_slots.items():
    for pin in _collect_pins_to_delete(child):
        await namespace.unpin(pin)
```

This deletes exactly the pins we froze, nothing more.

## Internal cache slot reference states

When does an internal cache slot get a replay reference, and what kind?

| Reference state | Internal cache slot replay reference |
|---|---|
| None (no reference) | None |
| Empty (reference exists but has no state) | None |
| Partial | LiveCacheSlot from reference._internal_cache_slot(key) |
| Split | None (structural mismatch — Split means it's subslotted, not running a step) |
| Finished | FrozenCacheSlot from reference's frozen_internal_slots |

Wait, what about Split? If the reference is Split, that means `subslot()` was called on the reference, not `run()`. So there are no internal cache slots. The reference's internal slots would be in the subslots, not here. So yes, no reference for internal cache slots.

Actually, Split for the REFERENCE slot means the reference was used for subslots. The current slot is trying to `run()` in it. That's a structural mismatch (run vs. split), which would be caught and rejected. After rejection, there's no reference to set up.

## Replay reference for subslots of internal cache slots

When a FrozenCacheSlot is the replay reference for an internal cache slot, and the step does `cache_slot(k).subslot(j)`:
1. `cache_slot(k)` returns a LiveCacheSlot with FrozenCacheSlot reference
2. `.subslot(j)` on that LiveCacheSlot creates a child LiveCacheSlot
3. The child's replay reference should be the FrozenCacheSlot's `.subslot(j)`
4. FrozenCacheSlot.subslot(j) looks up key j in FrozenSlotNode.subslots

This works naturally with the propagation mechanism.

## Orphan pin fallback for FrozenCacheSlot

If `FrozenCacheSlot._internal_cache_slot(key)` doesn't find the key in the frozen tree, optionally check the original namespace for a pin at the orphan path. This would be:
```python
def _internal_cache_slot(self, key):
    # First check frozen tree
    if self._node.state is SlotFinished:
        frozen_internal = load(self._node.state.frozen_internal_slots_ref)
        if key in frozen_internal.slots:
            node = load(frozen_internal.slots[key])
            return FrozenCacheSlot(node, ...)

    # Fallback: check orphan pins in original namespace
    if self._namespace is not None:
        orphan_key = f"{self._orphan_pin_prefix}/c/{key}"
        # Return a LiveCacheSlot backed by the reference namespace
        # (read-only, since it's an orphan)
        ...
```

This is the "nice to have" fallback. It lets stale internal slots from a previous partial run be revived if they happen to be needed.

For v1, I'd skip this and just return an empty FrozenCacheSlot (no state, no subslots) when the key isn't in the frozen tree.
