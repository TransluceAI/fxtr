# Open questions

Numbered for stable reference. Active questions first; resolved ones (answers
folded into the design docs) kept at the bottom for the record.

---

## Active

### OQ-6 (carried): Indexer shape for `depends_on`

The part-4 dependents check needs rows of `(depending pin, dep_path,
dep_value)` from `WorkflowTaskComplete.depends_on: dict[str, EntityRef[...]]`.
Whether the existing path-expression indexers can emit *dict keys* as a column
needs verification (`path_parse` supports `[*]` / `[$var]` over mappings — to
confirm for key-as-value emission).

**Status**: verify at part-4 impl time; if dict-key emission isn't supported,
either extend the indexer path language or change the representation to an
indexable list of `{path, value}` pairs. **Daniel: discuss before implementing
a fallback.** (This is the only genuinely-open question; everything else below
is resolved.)

---

## Resolved (folded into the design docs)

- **NQ-1: bare prov-tag result with no prov parent.** Resolved: treat as data
  and `u`-wrap it (don't reject). Keys on the `_provenance_parent` sidecar, not
  the type tag. (`PreserveProvPersistenceImpl` rejecting this is arguably a bug
  there; we don't copy it.) See workflow_runner.md "Result provenance".
- **NQ-2: recovering a slot path from a ServiceRef arg.** Resolved: cap-IR
  bounce-back returns a round-tripped handle with its original identity, so a
  mount-registry lookup works; fallback is a private undocumented
  `_internal_id()` (UUID) on the slot handle. See workflow_runner.md cache-key
  canonicalization.
- **NQ-3: run-group cancellation.** Resolved: deferred to a late stage. v0 has
  only full-runner cancellation (job task group cancelled, reacting as
  gracefully as possible, writing `cancelled` events). Per-group `cancel()`
  comes later.
- **NQ-4: op-log key format.** Resolved: `"{ts}-{seq}"` with a monotonic
  per-runner sequence counter (either approach was fine).
- **NQ-5: runner as a service in v0.** Resolved (revised during impl): the
  runner *and* run groups are plain-python async context managers, not services.
  `runner.run_group(name, desc)` is an async CM (records open/close op-log events
  on enter/exit while authorized); the first service a caller touches is the
  `TaskSlotProto` from `get_storage`. `RunGroupProto` / `WorkflowRunnerProto`
  remain defined only as the eventual RPC surface, unmounted in v0. (This
  supersedes the earlier "mount RunGroupProto + expose start_run_group" plan —
  the CM avoids emitting close events from an unauthorized mount-lifespan
  teardown.)
- **OQ-7 (old): nix handle fields.** Resolved: *one entity type per spawn
  kind*, strictly 1-1 with the nix attrset — `NixWasmStatelessServiceHandle`
  (carries `entry_point`) and `NixContainerStatelessServiceHandle` (no
  `entry_point`; baked into the image). No redundant fields. See
  stateless_service_provider.md "Handle + provenance".

- **OQ-1 (old): old `Incomplete` record on overwrite.** Resolved by the
  two-namespace redesign: only `Complete` is stored in the realization map;
  claims/incomplete are in-memory + recorded as op-log events. No `previous`
  chain on `Complete`; failure history lives in the op log.
- **OQ-2 (old): result provenance node.** Resolved: always store a
  provenance-layer node, keying on the `_provenance_parent` sidecar (not the
  type tag). See decision 7 / workflow_runner.md "Result provenance". (The
  bare-prov-tag edge case is NQ-1 above.)
- **OQ-3 (old): args as bare ids vs refs.** Resolved: keep **full
  `EntityRef`s** (closures travel + stay loadable), strip provenance parents
  for the key. See decision 6.
- **OQ-4 (old): typed task-fn authoring sugar.** Resolved: not needed — author
  with the existing `TypedServiceImpl` (typed impl, untyped wire). See decision
  9.
- **OQ-5 (old): `TaskStepContextWire` encoding.** Moot: `step_ctx` is a bare
  `ServiceRef[TaskStepContextProto]` and slot args are bare ServiceRefs, so
  there is no special wire shape. See decision 4.
- **OQ-8 (old): guest seam.** Resolved: reuse the existing `SandboxConnectProto`
  *and* guest adapter; express the stateless-service layer via a
  `StatelessServiceConfig(config, bindings, spawner)` dataclass arg. See
  decision 10. (Part-5 wiring worth another look at plan time.)
- **OQ-9 (old): claim race window.** Resolved: in-memory claims; conflict
  detected on save; best-effort for v0, CAS primitive is a followup. See
  decision 3 + "Claims, teardown & multi-runner posture".
- **OQ-10 (old): running into the map root.** Resolved by construction:
  `get_storage(path)` requires a nonempty path, so every storage is rooted at
  least one slot deep. See decision 5.
- **OQ-11 (old): who caches spawned services.** Resolved: runner spawns per
  job; the provider owns lifecycle/caching. See decision 9.
