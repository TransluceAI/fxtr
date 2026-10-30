# Scratch: FrozenCacheSlot lazy navigation design

(Earlier exploration of pivot/path-accumulation approaches is superseded by the
design below.)

## Key insight: separate CacheSlot from RunContext

`LiveCacheSlot` currently does double duty:
1. A place to launch runs into, with subslots (user-facing)
2. A container for a running step's internal state (internal cache slots, checkpoints)

These have different navigation patterns and different replay reference types.
Separating them clarifies the frozen navigation design.

### CacheSlot (slot-level)
- User-facing: `run()`, `run_fallible()`, `subslot()`
- Replay reference: another CacheSlot or a `FrozenCacheSlot`
- `FrozenCacheSlot` only needs subslot navigation (path accumulation)
- No internal cache slot concept here

### RunContext (run-level, currently LiveStepContext)
- Created when a step executes in a slot
- Has `cache_slot(key)` for internal cache slots, checkpoint methods, tracker
- Replay reference: another RunContext or a `FrozenRunContext`
- `FrozenRunContext` wraps `FrozenInternalSlots` (or `SlotFinished`)
- Internal cache slot lookup is sync because `FrozenInternalSlots.slots`
  is an in-memory dict of `EntityRef[FrozenSlotNode]`

### How it connects

```
LiveCacheSlot                          FrozenCacheSlot
  _replay_ref ──────────────────────→  (root_ref + subslot_path)
  subslot(key) → child LiveCacheSlot     child_subslot(key) → extends path
                   with _replay_ref ──→  FrozenCacheSlot(same root, path + key)

LiveStepContext                        FrozenRunContext
  _slot: LiveCacheSlot                   _frozen_internals: FrozenInternalSlots
  _replay_ctx ─────────────────────→     (loaded eagerly at creation)
  cache_slot(key):
    inner = self._slot._internal_cache_slot(key)  # creates child LiveCacheSlot
    if self._replay_ctx:
      ref = self._replay_ctx.get_internal_slot_ref(key)  # sync: dict lookup
      inner._replay_ref = FrozenCacheSlot(root_ref=ref, subslot_path=())
    return TracingCacheSlot(inner, ...)
```

## FrozenCacheSlot (subslot-only, simple)

```python
class FrozenCacheSlot:
    _root_ref: EntityRef[FrozenSlotNode]
    _subslot_path: tuple[str, ...]    # deferred, walked on first async access
    _node: FrozenSlotNode | None      # cached after load

    def child_subslot(self, key: str) -> FrozenCacheSlot:
        """Sync: just extends the path."""
        return FrozenCacheSlot(self._root_ref, self._subslot_path + (key,))

    async def _ensure_loaded(self) -> FrozenSlotNode | None:
        if self._node is not None:
            return self._node
        node = await self._root_ref.load()
        for seg in self._subslot_path:
            child_ref = node.subslots.get(seg)
            if child_ref is None:
                return None
            node = await child_ref.load()
        self._node = node
        return self._node

    async def load_state(self) -> SlotState | None:
        node = await self._ensure_loaded()
        if node is None or node.state_ref is None:
            return None
        return await node.state_ref.load()
```

No internal key, no pivots, no base path. Just root + subslot path.

## FrozenRunContext (internal cache slot navigation)

```python
class FrozenRunContext:
    _frozen_internals: FrozenInternalSlots  # eagerly loaded

    def get_internal_slot_ref(self, key: str) -> EntityRef[FrozenSlotNode] | None:
        """Sync: dict lookup in the already-loaded FrozenInternalSlots."""
        return self._frozen_internals.slots.get(key)
```

Created at two points:
1. In `_execute_in_slot` after `probe_state()` finds a Finished reference state:
   load `frozen_internal_slots_ref` → create `FrozenRunContext` → pass to `LiveStepContext`
2. In `replay_check` when recursing: the current slot's reference has Finished state,
   load its `frozen_internal_slots_ref` → create `FrozenRunContext` → use for
   resolving `InternalSlotRef` entries

### When reference state is Partial (not Finished)

No `FrozenInternalSlots` exists. Internal cache slots use the reference's live
`/c/` pins instead. The `LiveStepContext` gets a live reference's `LiveCacheSlot`
child as the replay ref, not a `FrozenRunContext`.

Actually — we could unify this. Both cases produce the same thing from the
consumer's perspective: "given a key, give me a replay reference for the internal
cache slot." The difference is whether the reference is frozen or live:

```python
class RunReplayReference(ABC):
    """Provides replay references for internal cache slots."""

    @abstractmethod
    def get_internal_replay_ref(self, key: str) -> CacheSlot-ish | None: ...

class FrozenRunReference(RunReplayReference):
    """From a SlotFinished with FrozenInternalSlots."""
    _frozen_internals: FrozenInternalSlots

    def get_internal_replay_ref(self, key: str) -> FrozenCacheSlot | None:
        ref = self._frozen_internals.slots.get(key)
        if ref is None:
            return None
        return FrozenCacheSlot(root_ref=ref, subslot_path=())

class LiveRunReference(RunReplayReference):
    """From a SlotPartial with live /c/ pins."""
    _reference_slot: LiveCacheSlot

    def get_internal_replay_ref(self, key: str) -> LiveCacheSlot:
        return self._reference_slot._internal_cache_slot(key)
```

Hmm, but do we actually need the Partial reference case? Let me check the plan...

The plan says (Part B, reject best-effort):
> Reference was Partial → its live /c/ pins provide LiveCacheSlot references
> for internal cache slot children

So yes, both cases exist. But this ABC might be overkill for two cases — could
just be a union type or a simple if/else in LiveStepContext.

## Where _frozen_internals loading happens

### Case 1: _execute_in_slot (step execution with replay reference)

```
probe_state() → finds Finished state from reference
  → eagerly loads FrozenInternalSlots (async, already in async context)
  → creates FrozenRunReference
  → passes to LiveStepContext constructor

LiveStepContext.cache_slot(key):
  inner = self._slot._internal_cache_slot(key)  # LiveCacheSlot child
  if self._run_replay_ref:
    replay = self._run_replay_ref.get_internal_replay_ref(key)  # sync!
    inner._replay_ref = replay  # FrozenCacheSlot or LiveCacheSlot
  return TracingCacheSlot(inner, ...)
```

The key point: `get_internal_replay_ref` is sync because `FrozenInternalSlots`
is already loaded. For the frozen case it returns a `FrozenCacheSlot(root_ref, ())`
which is just wrapping an EntityRef — no loading needed.

### Case 2: replay_check (recursive, resolving InternalSlotRef)

```python
async def replay_check(slot_deps, current_cache_slot_args, step_slot, run_replay_ref):
    for tracked_run in slot_deps.runs:
        # ... resolve slot refs ...
        # When we recurse, we need a run_replay_ref for the inner step.
        # The inner step's slot is current_slot (resolved from tracked_run.run_location).
        # Its replay reference's state should be SlotFinished (we just verified the ID matches).
        # We need to load that state's frozen_internal_slots_ref.

        if current_state is SlotFinished and inner_deps.runs:
            # Need to build a FrozenRunReference for the inner step
            inner_run_ref = await _build_run_replay_ref(current_slot)
            # ... recurse with inner_run_ref ...
```

`_build_run_replay_ref` loads the state from the slot's replay reference,
gets `frozen_internal_slots_ref`, creates `FrozenRunReference`. This is async
and happens naturally in the async replay_check context.

Wait — actually, in replay_check the `step_slot` is a `LiveCacheSlot` (the current
namespace's slot). We're checking whether the cached result is still valid. The
`InternalSlotRef` resolves against the current slot's live internal cache slots.
There's no frozen reference involved in _resolve_slot_ref during replay_check for
the current execution — the frozen reference is only relevant when we want to
**copy** data from a reference namespace.

Let me re-read... In replay_check today (Part A, no replay references):
- `_resolve_slot_ref(InternalSlotRef, ...)` → `step_slot._internal_cache_slot(key)`
  → navigates live pins in the current namespace

In Part B with replay references:
- The current slot might have a replay reference. Its internal cache slots
  (live children) might have replay references (frozen or live).
- `_resolve_slot_ref` still returns a `LiveCacheSlot` child.
- That child's `_replay_ref` might be a `FrozenCacheSlot` (set up by whoever
  created the child).

But who sets up the internal cache slot children's replay refs during replay_check?
In `_execute_in_slot`, it's done by `LiveStepContext.cache_slot()`. In replay_check,
`_resolve_slot_ref` calls `step_slot._internal_cache_slot(key)` directly — it
bypasses LiveStepContext.

So for replay_check, we'd need to set up the replay ref on the child LiveCacheSlot
that `_internal_cache_slot` returns. Options:

1. Have `_internal_cache_slot` on `LiveCacheSlot` automatically propagate
   `_frozen_internals` → child replay ref, if available. But `_frozen_internals`
   lives on the step context, not the slot...

2. Pass a `run_replay_ref` to `_resolve_slot_ref` and have it set up the child.

3. Have `replay_check` accept a `RunReplayReference` and handle the setup.

Option 3 seems cleanest — `replay_check` already takes `step_slot`, adding
a `run_replay_ref` parameter is natural.

```python
async def replay_check(
    slot_deps, current_cache_slot_args, step_slot, run_replay_ref=None
):
    for tracked_run in slot_deps.runs:
        current_slot = _resolve_slot_ref(
            tracked_run.run_location, current_cache_slot_args, step_slot,
            run_replay_ref=run_replay_ref,
        )
        # ... check state ...
        # When recursing:
        inner_run_ref = None  # build from current_slot's reference if applicable
        await replay_check(inner_deps, inner_args, current_slot, inner_run_ref)
```

And `_resolve_slot_ref` for `InternalSlotRef`:
```python
slot = step_slot._internal_cache_slot(ref.cache_slot_key)
if run_replay_ref:
    replay = run_replay_ref.get_internal_replay_ref(ref.cache_slot_key)
    slot._replay_ref = replay
for seg in ref.subslot_path:
    slot = slot.subslot(seg)  # propagates replay ref via FrozenCacheSlot path accumulation
return slot
```

This works! And it's explicit — no hidden state, no "someone must remember to pre-load."

## Summary of the design

1. **FrozenCacheSlot**: root_ref + subslot_path. Sync child_subslot extends path.
   Async _ensure_loaded walks the chain. Used as replay reference on CacheSlots.

2. **FrozenRunContext** (or RunReplayReference): wraps FrozenInternalSlots (for
   Finished references) or a LiveCacheSlot (for Partial references). Provides
   sync `get_internal_replay_ref(key)`. Used by LiveStepContext and replay_check.

3. **LiveStepContext**: holds a `RunReplayReference`. `cache_slot(key)` creates
   child LiveCacheSlot, sets its `_replay_ref` from the run reference, wraps
   in TracingCacheSlot.

4. **replay_check**: takes an additional `run_replay_ref` parameter. Passes it
   to `_resolve_slot_ref` for InternalSlotRef resolution. Builds inner
   `RunReplayReference`s when recursing.

5. **`_resolve_slot_ref`**: takes optional `run_replay_ref`. For InternalSlotRef,
   creates child LiveCacheSlot and sets its replay ref from the run reference.
   For ForeignSlotRef, subslot navigation propagates replay refs automatically
   via FrozenCacheSlot path accumulation.
