# Followups / tracked extensions

Deferred work that the v0 design intentionally punts on. Roughly ordered by
how soon we expect to want them.

1. **Provenance plumbing through the workflow system.** v0 deliberately
   strips provenance *parents* for cache keying (keys are provenance-blind;
   args keep full closures but no lineage). Result provenance *is* preserved
   (the result slot holds a provenance-layer node), but arg provenance lineage
   is not threaded through. Eventually: trace provenance of args end-to-end.
   Related: the sketch's "TODO: we will want to change the provenance system"
   around the stateless service provider — Daniel's intent is to use the
   handle entity itself as the provenance source for statelessly-spawned
   services (instead of the host's config-walk approximation), so entities
   created inside a job carry a more faithful history. Clarify when we pick
   this up.

2. **Slot relocation / rebasing.** Moving a subtree of task storage to a new
   location in (or across) realization maps requires rewriting the
   root-relative slot paths recorded in invocation entities in an
   invariant-preserving way. Safe in principle because task logic can never
   read the *path* of its storage slot args.

3. **Best-effort replay from one realization map into another.** On cache-key
   mismatch (or wholesale), replay compatible realizations into a new map.
   Args are recorded as full `EntityRef`s (decision 6), so their closures
   travel with the map and replay has the data it needs (modulo stripped
   provenance).

3b. **Event-sourcing redesign.** The op log is "just an event log" in v0. A
   later redesign could make the realization map a *derived* view of the op
   log (rebuildable by replaying events), rather than a separately-maintained
   store.

3c. **Compare-and-swap namespace primitive.** v0 multi-runner safety is
   best-effort (conflict detected on save). A CAS primitive on the namespace
   API would let a runner atomically claim/write a slot and close the
   two-runners-claim-the-same-empty-slot window (OQ-9 / NQ-2-adjacent).

3d. **Run-group cancellation.** A `RunGroupProto.cancel()` mechanism (NQ-3) —
   cancel a group's requester tasks and any now-orphaned jobs, writing
   `cancelled` op-log events. May land in v0 or be deferred.

3e. **TSV export of the realization map.** v0 stores in SQLite; TSV becomes an
   export/lockfile format for version-controlling a realization map alongside
   the repo.

4. **Checkpointing partial progress** (`checkpoint_store` /
   `checkpoint_retrieve` on the step context). Kept as commented
   placeholders on `TaskStepContext` in v1.

5. **Streaming output from tasks** via the step context, surfaced by the
   root workflow runner — depends on the output channel system (stage13).
   v0 already has `step_ctx.log(message: str)`, which the runner just
   forwards to python logging; this followup gives those logs (and richer
   streams) a real saved/streamed home.

6. **Restricted guest spawner / binding authorization.** v0 hands guests the
   provider's full spawner sub-service; restrict to declared bindings,
   possibly using the handles' provenance c-nodes as an authorization check.

7. **`max_concurrent_guests_per_sandbox` + sandbox pooling.** v0 uses a
   simple total-connects budget per sandbox (`max_guests_per_sandbox`).
   Refinement: watch connect-result tokens to count *active* guests, reuse
   sandboxes unless under heavy concurrent load, and keep a warm pool to
   spin guests back up. (Tracked as TODO in the provider impl.)

8. **Typed `spawn` + typed task-fn authoring sugar.** `spawn` is untyped on
   the wire today (per-method TypeVars unsupported by the typed codec), so
   `spawn_resolved` recovers typing python-side; relax the codec so `spawn`
   can be typed and the helper removed. Likewise any further task-fn authoring
   sugar beyond plain `TypedServiceImpl`.

9. **Coherence validity check on demand.** The `depends_on` invariant is
   enforced at delete time; an explicit `runner.check_coherence()` walking
   the map and reporting violations would help after manual surgery /
   crashed multi-runner scenarios.

9b. **Reject forged storage-slot placeholders.** Part 3 encodes a `TaskStorage`
   arg into the cache key as a private `_FxtrWorkflowStorageSlotPlaceholder(path)`
   entity. A task fn could in principle pass a hand-built placeholder as a real
   arg to inject an arbitrary slot path into a peer's cache key. The runner
   should detect and reject placeholder entities arriving as ordinary args
   (they may only appear via the runner's own slot-arg substitution).

9c. **Bounded / scope-close-driven slot registry.** The runner's `_slots`
   registry (`dict[ServiceKey, str]`, used to recognize a `TaskStorage` slot
   arg by handle identity) grows for the runner's whole lifetime and retains the
   slot handles — and thus their now-closed scopes. For a long-lived runner that
   opens many run groups this is a slow memory creep (not a hang: the scopes are
   drained, just not GC'd). Clean entries when a slot's mount scope closes (e.g.
   on run-group close / job teardown), or bound the registry another way.

10. **(Sidenote from sketch, not this stage)** BH detecting both
    remote entities declared in nix flakes and tsv-pinned entities in git
    stores, ensuring they travel alongside the repo.
