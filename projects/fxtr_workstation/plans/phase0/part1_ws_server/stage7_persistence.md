# Stage 7 — Optional persistence cap

(Originally stage 6; renumbered when the provenance-support stage was
inserted ahead of it.)

Goal: give the workstation a way to checkpoint entities across server
restarts without changing the in-memory-by-default model for the rest
of state. Sessions, RPC connections, and tracked-service registrations
remain ephemeral; only entities can be saved/loaded by name through
workstation-mounted **persistence cap** services.

Design notes that don't fit in the plan body live in
[`stage7_persistence_scratch.md`](./stage7_persistence_scratch.md).
Per-stage implementation log + open issues live in
[`stage7_impl_notes.md`](./stage7_impl_notes.md).

## Status (as of writing)

- **7a (`SystemSource`)** — done. Registration union widened;
  service snapshot partitions sources; tests green.
- **7b (no-prov persistence cap)** — done. End-to-end round-trip
  + restart durability + multi-namespace isolation all green.
- **7c (preserve-prov mode)** — done. ``create_u_node``
  helper added to v2; ``PreserveProvPersistenceImpl`` is real;
  all tests pass including ``test_preserve_prov_restart_durability``
  (xfail dropped). The original diagnosis pointing at
  ``_adopt_inheritance_entry``'s cascade turned out to be wrong;
  the actual root cause was at the cap_ir_arena packet construction
  layer (precomp closure ran on the source arena instead of the
  sub_arena fork, causing a server-side sub_arena liveset to miss
  ids that the client had via precomp). Fixed in v2 — see
  ``v2/plans/phase3/stage8c_user_caps/part5bugfix1_plan.md`` for
  the full diagnosis + the three-layer fix (server-side precomp
  tracking + client-side precomp-value refcounts +
  introduction-refcount abandon).
- **7d (polish)** — not started. Debug-UI tweak for
  ``system_source_kinds``; concurrency + edge-case tests.

## To finish stage 7

1. Complete 7d: debug-UI surfacing of ``system_source_kinds`` and
   the invalid-mode-at-startup edge-case test. Steps enumerated
   in the "7d — Polish" section below. (The earlier
   concurrency / namespace-closed-mid-call items were dropped —
   they're not contract behavior, and pinning them in tests
   wasn't worth the maintenance.)

## Settled design (pillars)

- **Persistence is a service.** No new builtin RPC method on
  ``WorkstationServiceProto``. The workstation mounts one persistence
  service per configured namespace; clients reach it via the existing
  short-ID / external-name service paths. Sessions invoke it through
  ``InvokeOp``; non-session RPC peers invoke it after
  ``get_service_by_external_name``. This keeps the surface uniform and
  reuses the existing invoke-result delivery / refcount machinery.
- **Multi-namespace, externally-opened.** ``WorkstationServer``
  accepts a list of ``PersistenceNamespaceConfig``s. Each carries an
  already-opened ``FxtrNamespace`` plus a permanent external service
  name plus a provenance mode. The workstation does **not** manage
  namespace lifecycle — the user is responsible for opening / closing
  them. The workstation lists each ``ns.scope`` in
  ``root_scope.include`` so lazy retrieval works through inheritance.
- **One service per namespace** with its own ``s{N}`` short ID and the
  user-supplied permanent external name. No-prov and preserve-prov
  caps both speak the same ``PersistenceProto`` but have distinct
  ``service_type_id``s so callers (or the debug UI) can tell them
  apart.
- **System-sourced registrations.** ``Registration.source`` is widened
  from ``RPCConnectionRecord`` to ``RPCConnectionRecord | SystemSource``.
  ``SystemSource`` carries ``kind`` + free-form ``metadata``. This is
  the first user of system-sourced registrations; future built-ins can
  reuse the shape.
- **Refcount story (no special-casing).** The persistence impl never
  mints workstation entity records itself. Sessions invoking
  ``retrieve`` get a result FxtrID that the existing invoke-result
  delivery worker copies into a per-record scope as usual. RPC peers
  get a chain-bridged FxtrID; the workstation never sees it again.
  Lazy claims stay valid because the namespace is open for the
  workstation's lifetime.

## Out of scope for stage 7

- Indexer registration / SQL queries (the underlying namespace
  supports them, but we don't expose them through the cap).
- Workstation-managed namespace lifecycle (config-driven open / close).
- Filesystem-watch / cross-process pin synchronization. ``retrieve``
  forwards directly to the namespace; no snapshotting on the
  workstation side.
- Auto-attaching provenance to user-built entities (a fxtr-core
  feature still under design).

## Sub-stage breakdown

Sub-stages are roughly additive; we pause for review at each
boundary. 7a is an enabling refactor with no persistence yet; 7b
brings up the no-prov caps end-to-end (and locks in the API shape
for preserve-prov so 7c is a pure impl swap-in); 7c lands the
preserve-prov impl; 7d is polish.

- **7a — `SystemSource`.** Widen ``Registration.source`` and update
  the few sites that currently assume it's an ``RPCConnectionRecord``.
  No persistence machinery yet. Existing tests stay green; one tiny
  unit test exercises the new union directly.
- **7b — Multi-namespace, no-prov persistence end-to-end.** New
  ``PersistenceProto`` + ``NoProvPersistenceImpl`` +
  ``persistence_cap_worker`` + ``WorkstationServer`` config wiring.
  ``PreserveProvPersistenceImpl`` ships as a stub class that raises
  ``NotImplementedError`` from every method, so the API and the
  config shape are stable. Tests cover one-namespace round-trip,
  two-namespace independence, server-restart durability, and the
  preserve-prov stub raising on invocation.
- **7c — Preserve-prov mode.** Add ``FxtrScope.create_u_node`` (thin
  wrapper around ``MemoryArena.create_u_node``, mirroring
  ``step_back_p_ctx``). Implement ``PreserveProvPersistenceImpl``.
  Tests for the lift-to-u-tag path on pin and the
  ``embed_as_provenance_parent`` path on retrieve (plus the rejection
  of non-prov-tagged payloads on retrieve).
- **7d — Polish.** Debug-UI tweak to surface ``system_source_kinds``
  on the services table (one chip per kind alongside the existing
  connection-ID badges). One edge-case test — invalid mode value
  rejected at startup. (Duplicate external_name across configs is
  already covered in 7b's tests. Concurrent pin / retrieve and
  namespace-closed-mid-call were considered and dropped: they're
  not contract behavior, and pinning them in tests wasn't worth
  the maintenance.)

## 7a — `SystemSource`

### Moving parts

- **`state.py`**:
  - New ``@dataclass(frozen=True) SystemSource(kind: str, metadata:
    dict[str, Any])``.
  - ``Registration.source: RPCConnectionRecord | SystemSource``
    (widened type).
  - ``release_registration``: today calls
    ``registration.source.registrations.remove(registration)``
    unconditionally. Guard with
    ``isinstance(registration.source, RPCConnectionRecord)`` and
    skip the source-side list maintenance for ``SystemSource``.
  - ``_build_service_record`` snapshot builder: add a new debug-only
    field ``system_source_kinds: list[str]`` (sorted, deduped, only
    populated when at least one registration's source is
    ``SystemSource``). ``registering_connection_ids`` continues to
    list only the ``RPCConnectionRecord`` sources. Add the new field
    to ``_DEBUG_ONLY_FIELDS`` for the projection filter.
  - ``add_rpc_connection`` exit sweep already filters by
    ``reg.source is record``; that naturally excludes SystemSource —
    no change needed, but call out in the comment.

- **`client_protocol.py`**:
  - ``ServiceRecord`` snapshot type gains
    ``system_source_kinds: list[str]`` (same default-factory pattern
    as the other debug-only fields).

- **`debug_ui/`**: no changes in 7a (column is unused). 7d revisits.

### Tests

- ``tests/test_state_system_source.py`` (new, small): construct a
  ``Registration`` with ``SystemSource`` source by hand, call
  ``release_registration`` and assert it removes the registration
  from ``tracked.registrations`` + ``service_external_names`` without
  touching any RPC connection.
- Existing ``test_service_registration.py`` / ``test_smoke.py`` /
  etc. should remain green — they all go through
  ``WorkstationServiceImpl.register_service`` which uses
  ``RPCConnectionRecord`` sources.

### Implementation steps

- [x] **7a.1** — Add ``SystemSource`` dataclass; widen
      ``Registration.source`` annotation. Update ``release_registration``
      with the ``isinstance(...,RPCConnectionRecord)`` guard. Also
      added ``describe_registration_source`` helper used by the three
      external-name conflict messages in ``service_impl.py`` so they
      render uniformly across source kinds. Doc note in
      ``notes/service_lifecycle.md`` updated.
- [x] **7a.2** — Snapshot builder partitions sources: RPC connection
      IDs in ``registering_connection_ids``, kinds in
      ``system_source_kinds`` (sorted, deduped). Both fields debug-only
      via ``_DEBUG_ONLY_FIELDS``. No wire-protocol type changes —
      records are ``list[dict[str, Any]]`` already.
- [x] **7a.3** — ``tests/test_state_system_source.py`` (4 tests:
      frozen-construct, describe-format, mixed RPC + system release,
      solo-system release drops record). Existing suite stays green
      (287 tests).
- [x] **7a.4** — Updated CODE_OVERVIEW.md (Registration.source union;
      ``SystemSource``; ``describe_registration_source`` helper;
      ``system_source_kinds`` field; new test file listing).

## 7b — Multi-namespace no-prov persistence

### Moving parts

- **`protocols.py`**:
  - ``PERSISTENCE_NO_PROV_TYPE_ID``,
    ``PERSISTENCE_PRESERVE_PROV_TYPE_ID`` — both via
    ``define_type(WORKSTATION_NS, ...)``.
  - ``PersistenceProto`` (``@service_protocol``) with five methods:
    ``pin(key, entity, *, overwrite=False) -> None``,
    ``unpin(key)``, ``retrieve(key) -> FxtrID``,
    ``try_retrieve(key) -> FxtrID | None``,
    ``list_pins(prefix="") -> list[str]``.

- **New `persistence.py`** (workstation package):
  - ``@dataclass(frozen=True) PersistenceNamespaceConfig`` —
    ``external_name: str``, ``namespace: FxtrNamespace``,
    ``mode: Literal["no_provenance", "preserve_provenance"] =
    "no_provenance"``.
  - ``NoProvPersistenceImpl(TypedServiceImpl[PersistenceProto])`` —
    ``_service_type_id`` returns ``PERSISTENCE_NO_PROV_TYPE_ID``;
    methods are thin wrappers around ``self._namespace.{pin, unpin,
    retrieve, try_retrieve, list_pins}``. Each method captures
    ``call_scope = get_active_scope()`` and threads it into the
    namespace call (via ``scope=`` for pin or ``into_scope=`` for
    retrieve / try_retrieve, with ``copy_into_scope=False`` so the
    transfer is a pure lift via inheritance).
  - ``PreserveProvPersistenceImpl(TypedServiceImpl[PersistenceProto])`` —
    ``_service_type_id`` returns ``PERSISTENCE_PRESERVE_PROV_TYPE_ID``;
    every method body is ``raise NotImplementedError("preserve_provenance "
    "mode is not yet implemented (stage 7c)")``.
  - ``async def persistence_cap_worker(state, config, *, task_status)``:
    opens an ``isolated=True, mountable=True`` ``impl_scope`` with
    ``include=[state.root_scope]``, mounts the appropriate impl,
    runs ``get_or_create_service_record`` to mint the
    ``TrackedServiceRecord`` (which spawns its own
    ``service_record_worker``), awaits ``bridge_ready``, builds and
    installs a ``Registration(source=SystemSource(kind="persistence",
    metadata={"external_name": ..., "mode": ...}),
    external_name=...)``, signals readiness via ``task_status.started()``,
    parks on ``tracked.shutdown_event``. Mirrors
    ``callback_record_worker`` in shape.

- **`server.py`**:
  - ``WorkstationServer.__init__`` accepts
    ``persistence_namespaces: list[PersistenceNamespaceConfig] = []``.
  - At startup, validate the list (external_name uniqueness; mode in
    the two known values); reject duplicates with a clear message.
  - In ``__asynccontextmanager__``, build ``root_scope`` with
    ``include=[parent_scope, *(c.namespace.scope for c in configs)]``.
  - After ``state`` is constructed, spawn one
    ``persistence_cap_worker`` per config via
    ``await background_tg.start(...)``. Bring-up errors propagate out
    of ``__aenter__`` cleanly (the rest of the server is also still
    being constructed; the existing ``async with`` unwind handles
    teardown).
  - Shutdown: after the existing ``state.entity_records.clear()``
    step and before ``background_tg.__aexit__`` unwinds, walk
    ``state.service_records.values()`` and
    ``release_registration`` on every remaining registration whose
    source is ``SystemSource``. That fires each cap's
    ``shutdown_event``; the cap workers wake, drain their
    ``impl_scope``s, exit; ``background_tg`` then finishes.

### Lifetime / scope details

- Lazy retrieval reaches the namespace via:
  ``call_scope.include = [service_scope, ...]``;
  ``service_scope.include = [root_scope]``;
  ``root_scope.include = [parent_scope, *ns_scopes]``. So
  ``namespace.retrieve(into_scope=call_scope, copy_into_scope=False)``
  passes ``_resolve_into_scope``'s included-in check and the lift
  succeeds.
- For sessions invoking ``retrieve``: the result FxtrID flows
  through the existing invoke-result delivery
  (``deliver_invoke_result_to_slot`` →
  ``_deliver_result_as_event`` → per-record scope via
  ``_ensure_entity_record_via_copy``). The session ends up with a
  normal ``TrackedEntityRecord`` bound to the call.
- For RPC peers invoking ``retrieve``: the framework chain-bridges
  the FxtrID across the wire from ``call_scope``. The workstation
  never mints a record. Lifetime claim travels with the bridge.

### Tests (`tests/test_persistence_7.py`, new)

All using ``SQLiteFxtrNamespace`` with a ``tmp_path``-rooted SQLite
file (test fixture opens / closes the namespace ourselves).

- **Single-namespace round-trip.** Open server with one config; a
  session invokes ``pin("k", entity)`` then ``retrieve("k")``,
  asserts the FxtrID matches.
- **Restart durability.** Open server A, pin, close. Open server B
  with the same SQLite path, ``retrieve``, assert content matches.
- **Two-namespace independence.** Two configs, two SQLite files,
  pin same key in both with different entities; ``retrieve`` from
  each returns the right one. List_pins reflects each namespace
  independently.
- **`unpin` + `try_retrieve`.** ``try_retrieve`` returns ``None``
  after ``unpin``; ``list_pins`` reflects the removal.
- **`overwrite=False` rejects re-pin** (existing namespace
  behavior); ``overwrite=True`` succeeds.
- **Preserve-prov stub raises.** A config with
  ``mode="preserve_provenance"`` brings up successfully (cap is
  reachable via ``get_service_by_external_name``); invoking ``pin``
  / ``retrieve`` raises ``NotImplementedError``.
- **External-name conflict at startup.** Two configs sharing an
  external_name → server bring-up raises before yielding.

### Implementation steps

- [x] **7b.1** — ``protocols.py``: ``PERSISTENCE_NO_PROV_TYPE_ID`` /
      ``PERSISTENCE_PRESERVE_PROV_TYPE_ID`` + ``PersistenceProto``
      (5 methods: ``pin``, ``unpin``, ``retrieve``, ``try_retrieve``,
      ``list_pins``).
- [x] **7b.2** — ``persistence.py``: ``PersistenceMode`` Literal,
      ``PersistenceNamespaceConfig`` (frozen),
      ``NoProvPersistenceImpl`` (real impl) +
      ``PreserveProvPersistenceImpl`` (NotImplementedError stub) +
      ``_build_persistence_impl`` dispatcher +
      ``persistence_cap_worker`` (mirrors ``callback_record_worker``
      pattern: opens isolated mountable ``impl_scope`` with
      ``include=[root_scope]``, mounts impl, runs
      ``get_or_create_service_record``, awaits bridge, installs
      ``Registration(source=SystemSource, external_name=...)``,
      signals ``task_status.started()``, parks on
      ``shutdown_event``).
- [x] **7b.3** — ``server.py``: accepts ``persistence_namespaces``;
      validates external-name uniqueness + mode at construction;
      includes each ``ns.scope`` in ``root_scope.include``; awaits
      ``background_tg.start(persistence_cap_worker, ...)`` per
      config so caps are fully ready before yield; shutdown sweep
      releases every ``SystemSource``-sourced ``Registration`` so
      cap workers can wake from their park and let
      ``background_tg.__aexit__`` complete.
- [x] **7b.4** — Updated CODE_OVERVIEW.md: server-side config and
      scope topology change; new ``persistence.py`` Module
      Breakdown entry; ``PersistenceProto`` mentioned in
      ``protocols.py``; new test file listing.
- [x] **7b.5** — Tests in ``tests/test_persistence_7.py`` (7
      tests): pin/retrieve round-trip, ``unpin``/``try_retrieve``,
      ``overwrite=False/True``, restart durability across server
      lifetimes, two-namespace independence (same key, different
      entities, isolated ``list_pins``), preserve-prov stub raises
      on every method, duplicate external-name rejected at
      ``WorkstationServer`` construction. All 294 tests green.

## 7c — Preserve-provenance mode

### Moving parts

- **`v2/fxtr2/data/entity_ref_scope.py`**: new
  ``FxtrScope.create_u_node(wrapped_id: FxtrID) -> FxtrID`` mirroring
  ``step_back_p_ctx``'s validation pattern (no provenance parent on
  input; tracked here; calls
  ``self._main_arena.create_u_node(self._main_lease.clone(), wrapped_id)``
  and returns the result). Returns a bare ``u``-tagged FxtrID with no
  ``_provenance_parent`` of its own — caller pairs it with
  ``embed_as_provenance_parent`` to layer.

- **`persistence.py`**: replace ``PreserveProvPersistenceImpl``'s
  ``NotImplementedError`` bodies with real impls:
  - ``pin``: if ``entity._provenance_parent is None``, lift to
    ``call_scope.create_u_node(entity)``; otherwise extract via
    ``call_scope.extract_provenance_parent(entity)``. Pin the
    resulting bare prov FxtrID.
  - ``retrieve``: ``namespace.retrieve(...)`` returns a bare FxtrID;
    if its tag isn't in ``("n", "c", "p", "u")``, raise — this
    mode's invariant is "every pin is a provenance node". Otherwise
    ``return call_scope.embed_as_provenance_parent(raw)`` so the
    caller gets a layered ``(data, prov)`` ID.
  - ``try_retrieve``: same as ``retrieve`` but returns ``None`` on
    not-found; the prov-tag check still applies on hits.
  - ``unpin`` / ``list_pins``: identical to no-prov.

### Tests (extend `tests/test_persistence_7.py` or split)

- **u-node lift on pin.** Pin a bare ``d``-tagged entity; assert
  the namespace stores a ``u``-tagged node; retrieve via the same
  cap returns a layered ``(d, u(d))`` FxtrID; the inner data is
  reachable.
- **Already-prov pin / retrieve.** Pin a layered ``(d, n_prov)``
  entity; namespace stores the bare ``n``-prov node; retrieve
  returns the same layered FxtrID.
- **Reject non-prov retrieve.** Manually pin a ``d``-tagged entity
  via the namespace's API directly (bypassing the cap), then
  retrieve via the preserve-prov cap → raises
  with a clear message.
- **No-prov ↔ preserve-prov are independent.** Same workstation
  with both kinds of caps; cross-cap retrieves don't accidentally
  collide.

### Implementation steps

- [x] **7c.1** — Added ``FxtrScope.create_u_node`` to
      ``v2/fxtr2/data/entity_ref_scope.py`` mirroring the
      ``step_back_p_ctx`` validation pattern (no prov-parent;
      tracked here; ``self._main_arena.create_u_node(lease,
      wrapped)``). Tests in
      ``v2/tests/test_entity_ref_scope/test_provenance_context.py
      ::TestCreateUNode`` (4 tests). v2 suite green (2323).
- [x] **7c.2** — Replaced ``PreserveProvPersistenceImpl`` stub
      bodies with real impls. Pin path: ``_layer_for_storage``
      handles layered (``extract_provenance_parent``), bare-data
      (``create_u_node``), and rejects bare-prov-tag inputs as
      ambiguous. Retrieve path: ``_restore_layered`` validates the
      tag and calls ``embed_as_provenance_parent``. ``unpin`` /
      ``list_pins`` pass through unchanged. The retrieve path
      relies on the documented v2 invariant that the
      ``unwraps_to`` cascade in ``_adopt_inheritance_entry`` pulls
      the data half along when lifting from a foreign source —
      **see "Remaining work" below**.
- [x] **7c.3** — Updated CODE_OVERVIEW.md (preserve-prov now
      functional; ``FxtrScope.create_u_node`` referenced; test
      file additions).
- [x] **7c.4** — Tests for the preserve-prov cap added to
      ``tests/test_persistence_7.py``: u-lift on bare-data pin,
      already-layered round-trip, bare-prov-tag rejection, and
      restart durability. All restart-durability cases now pass;
      the original ``xfail`` was dropped after the v2-side fix
      landed (see "Remaining work / fix history" below).

### Remaining work / fix history

The preserve-prov retrieve path's restart-durability test
``test_preserve_prov_restart_durability`` was originally
``xfail(strict=True)`` against an alleged cascade bug in
``MemoryStore._adopt_inheritance_entry``. The actual root cause
turned out to be at the cap_ir_arena layer: the wire-packet
builder was running the precomp closure on the *source* arena
instead of the sub_arena fork that was about to ship as the wire
``fxtr_arena``, so the server's wrapped sub_arena's liveset
didn't include precomp values that the client could legitimately
sync-discover. The fix landed in v2 in three layers:

1. ``compute_precomp_closure`` renamed to
   ``compute_and_track_precomp_closure`` and retargeted to run
   on the sub_arena.
2. Precomp values count toward client-side refcounts so abandons
   don't overshoot.
3. Wire abandon ships introduction-refcounts (cap-IR
   ``DiscardMessage``-style) for race safety.

See ``v2/plans/phase3/stage8c_user_caps/part5bugfix1_plan.md``
for the full diagnosis. ``stage7_impl_notes.md`` ("Upstream v2
bug — fixed") summarizes the original incorrect diagnosis.

## 7d — Polish

- [x] **7d.1** — Debug UI: ``debug_ui/src/tables/ServicesTable``
      surfaces ``system_source_kinds`` as ``system:{kind}`` chips
      rendered before the connection-ID badges in the
      ``registered by`` cell. ``ServiceRecord`` in ``ws_client.ts``
      gains a ``system_source_kinds: string[]`` field. The
      subscription already runs with ``debug=true`` (stage 4), so
      no plumbing change was needed.
- [x] **7d.3** — Edge-case test: invalid ``mode`` value rejected at
      ``WorkstationServer`` construction
      (``test_invalid_mode_rejected_at_construction``). Mode
      validation already lives in
      ``WorkstationServer._validate_persistence_configs`` — no impl
      change. (Duplicate ``external_name`` was already covered by
      ``test_duplicate_external_name_rejected_at_construction``.)
- [x] **7d.4** — Updated CODE_OVERVIEW.md: ``test_persistence_7.py``
      listing reflects 7c restart-durability green + 7d invalid-mode
      test; the persistence module breakdown drops the obsolete
      "Known limitation (upstream v2 cascade bug)" paragraph in
      favor of a short "fixed upstream" note pointing at
      ``stage7_impl_notes.md``; the debug UI section gains a
      stage-7d entry describing the new ``system_source_kinds`` chip.

## Notes / open follow-ups

- **`try_retrieve` returning `FxtrID | None`** across the wire:
  expected to work via typed_service's standard ``T | None``
  handling, but verify in 7b's first test pass; if it breaks,
  fall back to a sentinel encoding.
- **Concurrent re-pin race.** If two sessions race ``pin("k",
  ...)`` on the same key with ``overwrite=True``, the underlying
  namespace serializes via its own session lock; either order is
  acceptable. No special workstation-side handling needed and not
  pinned by a test — not part of the cap's contract.
- **Provenance-node retrieval via no-prov cap.** Pinning a layered
  FxtrID via the no-prov cap silently drops the prov half (the
  namespace stores the bare data ID; future retrieve returns the
  bare data without prov). Document this in the protocol docstring
  in 7b — it's a deliberate behavior, not a bug.
