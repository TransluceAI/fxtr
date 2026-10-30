# Step 1 Core System - Scratch Notes

## Scope for phase 1
- External steps only (no managed)
- Cache slots: Empty/Split/Partial/Finished states
- No replay references
- No checkpoints (save_checkpoint / load_checkpoint)
- No run_fallible
- Resumption: finished slots reused, partial runs re-execute from scratch (their finished sub-slots provide caching)

## Storage model thoughts

Use fxtr2's SQLiteFxtrNamespace for persistent cache slot state. Each cache slot maps
to a key prefix in the namespace. Need to store:
- Slot state (empty/split/partial/finished)
- Cache key (FxtrID derived from hashing step identity + resolved args)
- Result data (for finished runs)
- What sub-slots exist

Options for storage layout:
1. One pin per piece of metadata: `<prefix>/state`, `<prefix>/result`, etc.
2. One entity per slot containing all metadata, with links to sub-entities

Option 2 is cleaner but means updating the entity on every state transition
(and entities are content-addressed so the ID changes). Option 1 is more
granular but many pins.

Going with option 1 for now - it's simpler to reason about and update
incrementally. We can consolidate later.

Pin scheme:
- `<prefix>` → entity with {state, cache_key_id, step_identity}
- `<prefix>/result` → entity with the serialized result (only for finished)
- Subslots at `<prefix>/sub/<key>` (for Split mode)
- Internal cache slots at `<prefix>/int/<key>` (for Partial/Finished runs)

## Cache key computation

Cache key = FxtrID of a "cache key entity" that encodes:
- Step handle identity (for external steps: the registered key entity ID)
- All resolved args, serialized to CBOR

Cache slots in args are excluded from the cache key (they're compared separately
via the replay check). Step handles are included by their identity.

For phase 1 (no replay references), the cache-replay check for finished runs is:
just compare the cache key. The cache-slot-arg check is deferred until we have
replay references, since in phase 1 the same cache slots are passed in every time
(they come from the same namespace).

Wait - actually the sketch says even without replay references, if you pass cache
slots as args to a step, and one of those slots' contents changed (because another
part of the workflow filled it differently), you need to detect that. But in phase 1
without replay references, the cache tree is append-only within a single namespace.
So if a slot was finished before, it stays finished with the same result. The only
case where it could differ is if the workflow logic itself changed between runs, but
we don't detect that in phase 1 (that's what replay references are for).

So for phase 1: cache key match on a finished slot → reuse the result. Simple.

## Execution model

TopLevelEntrypoint owns an anyio task group. When run_uncached or cache_slot.run is
called:
1. Spawn a task in the task group
2. Return a Pending backed by an internal future
3. The task:
   a. Deep-walks args to resolve all Pending values
   b. Computes cache key from resolved args
   c. For cached run: checks slot state
      - Finished with matching key → return cached result immediately
      - Finished with different key → error (write-once violation)
      - Partial with matching key → re-execute (sub-slots provide caching)
      - Partial with different key → error (write-once violation)
      - Empty → proceed with new execution
      - Split → error (can't run in a split slot)
   d. Transitions slot to Partial
   e. Creates StepContext, calls step impl
   f. On success → transitions to Finished, stores result
   g. On failure → slot stays Partial, propagate error

## Error propagation in phase 1

The sketch specifies that uncaught exceptions propagate as BaseException subclass
and "poison" containing steps. This means:

1. Step raises exception → wrapped in StepAbortError(BaseException)
2. StepAbortError propagates up through the call stack
3. If a containing step catches it and returns normally, the runtime still treats
   that step as failed (slot stays Partial)

Implementation: StepContext tracks whether any sub-step abort was raised during
this step's execution. On step completion, if abort_seen is True, we don't
transition to Finished regardless of whether the step returned a value.

Actually, simpler: StepAbortError is a BaseException, so normal `except Exception`
won't catch it. If someone explicitly catches BaseException and swallows it... we
can handle that edge case later. For the spike, just letting it propagate as
BaseException is probably sufficient.

## Pending implementation

Pending wraps an anyio Event + a result slot. When the task completes, it sets
the result and fires the event. __await__ waits on the event and returns the
result. If the task failed, __await__ re-raises the exception.

For forwarding: when a Pending is passed as an arg to another step, the runtime
awaits it during arg resolution. The step itself never sees a Pending in its args.

## Result serialization

For phase 1, results are ConcreteTypes but we need to serialize them. Cache slots
and step handles in results are tricky. For now:
- Primitives (str, int, float, bool, bytes, None) → CBOR directly
- Lists/dicts → recursive CBOR
- Cache slots in results → not supported in phase 1 (error)
- Step handles in results → not supported in phase 1 (error)

We can relax this later.

## Arg serialization for cache keys

Similar to results, but:
- Cache slots are excluded (not part of cache key)
- Step handles are included by their identity (FxtrID of the key entity for external handles)
- Primitives, lists, dicts → CBOR

## What about concurrent runs in the same slot?

The sketch says same-key concurrent runs block. Different-key concurrent runs error.
For phase 1, we need some locking per cache slot. An anyio Lock per slot should work.
When a second caller arrives at a locked slot:
- Wait for the lock
- Then check the slot state (it should be Finished now)
- Do the normal cache-replay check
