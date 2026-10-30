# Chunk 5 — the task-fn service boundary

Scope: everything between the runner and user task code, per
`runner_api.py`.

- The task-side **adapter**: wraps a user `TaskFn` into a
  `WorkflowTaskFnProto` service impl — builds views from pushed
  `ViewGrant`s + the capability ref, constructs `TaskContext`
  (`own_internals` from the internals grant), calls `run(ctx, **args)`,
  normalizes the return to per-output ids.
- Mounting: run capability impl mounted per run scope; `@service_protocol`
  decoration of the runner_api protocols (validate the signatures against
  the typed-service dialect — first real test of the wire vocabulary).
- Minimal binding registry + spawner: `bind_handle(handle, connector)`
  with release capability; task-fn resolution at demand time (read task_fn
  cell → binding → spawn → `WorkflowTaskFnProto` ref).
- Scope tree: runner scope → per-run scope; entity flow across the
  boundary (return arena → persist-then-record).

Tests: adapter arg/view construction (received names, vmapped invisibility),
single deterministic task invoked through the mounted boundary against a
fake ops engine, binding lifecycle (hold/drop/release).

Notes/log:
- Done (`task_service.py`, `tests/test_task_service.py`, 4 tests).
- **The wire protocols are now real `@service_protocol`s** (runner_api
  decorated); mashumaro's codec builder validated every signature at
  import. Two findings, both worth review:
  1. **`open_channel` cannot return a bare protocol** — services return
     ServiceRefs; `StepCapabilityProto.open_channel` now returns
     ``ServiceRef[UnknownService]`` (retype to FxtrChannelWriter; plumbing
     was deferred anyway).
  2. **Int-keyed mappings are not legal FxtrServiceData** — wire mapping
     keys must be str, so ``ViewSel.bound: frozendict[int, CoordValue]``
     crosses as sorted ``[position, value]`` pairs via a field-level
     ``SerializationStrategy`` (in-memory shape unchanged; zero ripple to
     Shadow/registry/engine).
- The resolved run-capability *typed proxy structurally satisfies the
  ViewBackend seam* — views run straight over it, no adapter shim.
- Error transit: engine-raised workflow errors cross the boundary as
  ``IRException`` with the original class name as ``category`` (fxtr2's
  dispatch wrapping rule; now actually asserted in a test). This transit
  form is **informational only** — task-visible tracebacks, test
  assertions. Nothing durable is ever classified from it: raise-time
  poisoning persistence and the errored/interrupted split happen
  runner-side, where the original typed exception is available. (The
  first version of this note said chunk 6's executor would classify from
  the category; the 2026-07-04 review rejected that — durable state must
  never be derived from errors that crossed the wire, and the poisoning
  contract requires runner-side persistence anyway, since task code may
  catch the error entirely and return normally.)
- Binding registry per the decided contract: double-bind raises, release
  idempotent, rebind-after-release OK, misses are ``HandleUnboundError``.
  The wire release *capability* is chunk 8.
- `TaskOutputError` added (adapter return-normalization violations;
  errored-classified).

Review round (2026-07-04) — fixes squashed into this chunk:

- **Service type ids** were ``FxtrID.builtin("wf3/...")``; now they are
  registrant-controlled ``define_type(WORKFLOWS3_NS, ...)`` ids like any
  other fxtr2 service (wf2 ``basic_provider`` pattern). ``_service_type_id``
  returns the module-level constant; the old per-mount
  ``track_builtin`` calls are gone (``define_type`` stores the type entity
  into ``GLOBAL_SCOPE``).
- **Scope discipline** (`v2/notes/scope_discipline.md`): the per-run scope
  is now ``isolated=True`` with an explicit include of a required
  ``runner_scope`` argument (the execution-semantics scope tree: runner
  scope → per-run scope); refs travel together with the scope they are
  resolvable in (``connector_scope`` / ``into_scope`` params, membership
  rationale comments); the only active-scope use left is the documented
  service-method dispatch exception inside ``TaskFnAdapter.run_task``.
  ``run_task_over_engine``'s returned result ids are documented as *bare
  ids* (their closures die with the run scope), with a TODO deferring
  principled result persistence to the chunk 6 design round.
- **BindingRegistry** originally bound the service ref directly ("v0
  binds the service directly"), which cannot work: a ref received through
  a service call is resolvable only via the call's input scope, which
  closes when the call returns, and nothing held the target service
  alive. Bindings now hold *connectors* (``ConnectorProto`` — wf2's
  ``ExternalConnectorProto`` is the prior art, as ``user_api.bind_handle``
  always specified), bridged into the registry's runner scope at bind
  time (``bridge_service_from_scope(..., keepalive_via=
  "independent_lease")``, with the same-scope ``is_tracked`` shortcut),
  and :meth:`spawn` produces a fresh service per use via
  ``connect()``. ``wrap_singleton`` covers the known-stateless case.
  Double-bind now raises ``ValueError`` (a registration error, not a
  not-ready dependency, so ``HandleUnboundError`` was the wrong class).
