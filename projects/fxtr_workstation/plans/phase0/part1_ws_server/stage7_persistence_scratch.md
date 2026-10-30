# Stage 7 — scratch / working notes

Free-form scratchpad for stage 7. The actual plan lives in
``stage7_persistence.md``.

## Design pillars (settled in chat)

- **Mechanism**: persistence is *just* a workstation-mounted typed
  service. No special builtin RPC method on
  ``WorkstationServiceProto``. Sessions reach it via
  ``InvokeOp(service_short_id=...)``; non-session RPC peers reach it
  via ``get_service_by_external_name``.
- **Multi-namespace**: the user passes a list of already-opened
  ``FxtrNamespace`` instances. The workstation does not own
  namespace lifecycles. We just attach each ``ns.scope`` to
  ``root_scope.include`` and mount one persistence service per
  namespace.
- **One service per namespace**, each with:
  - its own auto-allocated ``s{N}`` short ID;
  - a user-supplied permanent external service name;
  - its own ``service_type_id`` (no-prov vs preserve-prov are
    distinct types), but both speak ``PersistenceProto``.
- **System-sourced registration**: ``Registration.source`` is widened
  to ``RPCConnectionRecord | SystemSource``. ``SystemSource`` carries
  ``kind`` + ``metadata`` for debug visibility.
- **Provenance modes**: ``"no_provenance"`` (defer to namespace) and
  ``"preserve_provenance"`` (lift bare data to ``u``-node, pin the
  prov half via ``extract_provenance_parent``; restore on retrieve via
  ``embed_as_provenance_parent``; reject retrieve of non-prov-tagged
  payloads).
- **Lazy retrieval**: namespace's scope is a parent of
  ``root_scope`` (via ``root_scope.include=[ns.scope, ...]``), which
  is itself a parent of every ``service_scope`` / ``call_scope``. So
  ``namespace.retrieve(into_scope=call_scope, copy_into_scope=False)``
  just lifts via inheritance — the namespace's deep-fetch source
  travels along the chain.
- **Refcount story**: persistence does **not** mint workstation entity
  records itself. Sessions invoking ``retrieve`` get a result FxtrID
  that flows through the existing invoke-result delivery pipeline,
  which mints a ``TrackedEntityRecord`` via
  ``_ensure_entity_record_via_copy``. RPC peers invoking ``retrieve``
  just get a chain-bridged FxtrID; the workstation never sees it
  again. The namespace stays open for the workstation's lifetime, so
  lazy claims remain valid.

## Scope / lifetime topology

```
parent_scope (user's)                          ─┐
                                                ├ both pre-existing parents
each ns.scope (one per FxtrNamespace passed in) ─┘
            │ (root_scope.include = [parent_scope, *ns_scopes])
            ▼
root_scope  (workstation's)
     │
     ├── per-record entity scopes
     ├── per-service-record service_scopes
     │       │
     │       └── (bridges in from per-impl impl_scope)
     ├── per-rpc-connection scopes
     ├── per-session scopes (and their call_scopes when running invokes)
     └── per-persistence-cap impl_scope
```

`impl_scope` for a persistence cap: ``include=[root_scope]`` is
sufficient — root_scope already includes ns.scope so the namespace is
reachable. (Alternative: ``include=[ns.scope, root_scope]`` is
equivalent but redundant.) Mirror the callback pattern.

## SystemSource shape

```python
@dataclass(frozen=True)
class SystemSource:
    """Source for workstation-internal service registrations."""
    kind: str           # e.g. "persistence"
    metadata: dict[str, Any] = field(default_factory=dict)
```

Touch points for widening ``Registration.source`` (call site survey
needed in 7a):

- ``state.py``:
  - ``Registration`` dataclass (signature change).
  - ``release_registration``: today does
    ``registration.source.registrations.remove(registration)``. Needs
    a guard: only RPC sources have a ``registrations`` list. For
    SystemSource, just skip that step.
  - ``add_rpc_connection`` exit sweep: today drops every reg whose
    ``source is record``; with SystemSource that filter still works
    (system regs aren't owned by an RPC connection record). No change.
- ``service_impl.py``:
  - ``register_service`` constructs ``Registration(source=record, ...)``
    where ``record`` is an RPCConnectionRecord — fine.
  - ``ReleaseCallbackImpl`` (RPC peer's release callback) — fine, it
    just calls ``release_registration``.
- ``state._build_service_record`` snapshot builder:
  - ``registering_connection_ids`` field today is a set of RPC
    connection UUIDs from each registration's source. With
    SystemSource we either:
    1. add a parallel ``system_source_kinds`` field listing the kinds
       of system-sourced regs that hold this record (preferred —
       least invasive for the wire shape and the debug UI), or
    2. switch the field to a tagged union of source descriptors.
    Lean (1) for stage 7. Field is debug-only, so projection logic
    around ``debug=`` flag carries it.
- Tests: ``test_service_registration.py`` constructs ``Registration``
  in fixtures (?) — re-check.

## PersistenceProto

```python
@service_protocol
class PersistenceProto(Protocol):
    async def pin(self, key: str, entity: FxtrID, *, overwrite: bool = False) -> None: ...
    async def unpin(self, key: str) -> None: ...
    async def retrieve(self, key: str) -> FxtrID: ...
    async def try_retrieve(self, key: str) -> FxtrID | None: ...
    async def list_pins(self, prefix: str = "") -> list[str]: ...
```

Type IDs:

```python
PERSISTENCE_NO_PROV_TYPE_ID = define_type(WORKSTATION_NS, "persistence/no_prov")
PERSISTENCE_PRESERVE_PROV_TYPE_ID = define_type(WORKSTATION_NS, "persistence/preserve_prov")
```

Two impls (``NoProvPersistenceImpl`` / ``PreserveProvPersistenceImpl``)
with the same protocol but distinct ``_service_type_id()`` returns.

## NoProvPersistenceImpl method bodies (sketch)

```python
async def pin(self, key, entity, *, overwrite=False):
    call_scope = get_active_scope()
    await self._namespace.pin(key, entity, overwrite=overwrite, scope=call_scope)

async def unpin(self, key):
    await self._namespace.unpin(key)

async def retrieve(self, key) -> FxtrID:
    call_scope = get_active_scope()
    # copy_into_scope=False ⇒ lift via inheritance. Requires call_scope
    # to be a descendant of namespace.scope; root_scope.include arranges
    # this.
    return await self._namespace.retrieve(key, into_scope=call_scope)

async def try_retrieve(self, key) -> FxtrID | None:
    call_scope = get_active_scope()
    return await self._namespace.try_retrieve(key, into_scope=call_scope)

async def list_pins(self, prefix=""):
    return list((await self._namespace.list_pins(prefix)).keys())
```

## PreserveProvPersistenceImpl (stage 7c)

Needs a new helper ``FxtrScope.create_u_node(wrapped_id)`` that wraps
``MemoryArena.create_u_node`` (already exists). Pattern follows
``step_back_p_ctx`` validation (tag check, no-prov-parent check,
tracked-here check).

```python
async def pin(self, key, entity, *, overwrite=False):
    call_scope = get_active_scope()
    if entity._provenance_parent is not None:
        prov = call_scope.extract_provenance_parent(entity)
    else:
        prov = call_scope.create_u_node(entity)
    await self._namespace.pin(key, prov, overwrite=overwrite, scope=call_scope)

async def retrieve(self, key) -> FxtrID:
    call_scope = get_active_scope()
    raw = await self._namespace.retrieve(key, into_scope=call_scope)
    if raw.get_tag() not in ("n", "c", "p", "u"):
        raise ValueError(
            f"persistence cap in preserve_provenance mode: pin {key!r} "
            f"resolves to tag {raw.get_tag()!r}, not a provenance node"
        )
    return call_scope.embed_as_provenance_parent(raw)

# unpin / list_pins / try_retrieve same as no-prov; try_retrieve mirrors retrieve.
```

In stage 7b we ship the ``PreserveProvPersistenceImpl`` class with
every method raising ``NotImplementedError("preserve_provenance mode "
"is not yet implemented (stage 7c)")``. Servers configured with
preserve-prov mode bring up the cap successfully (so the API surface
is stable) — methods just raise on invocation.

## Worker pattern: ``persistence_cap_worker``

Mirrors ``callback_record_worker`` but parks for the workstation's
lifetime, not a per-mutation one.

```python
async def persistence_cap_worker(state, config, *,
        task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED):
    async with FxtrScope(
        isolated=True, mountable=True,
        include=[state.root_scope],
        name=f"persistence_cap/{config.external_name}",
    ) as impl_scope:
        impl = (
            NoProvPersistenceImpl(state, config.namespace, config.external_name)
            if config.mode == "no_provenance"
            else PreserveProvPersistenceImpl(state, config.namespace, config.external_name)
        )
        impl_ref = impl_scope.mount(impl)
        with state.mutation_context():
            tracked = get_or_create_service_record(state, impl_ref, impl_scope)
        await tracked.bridge_ready.wait()
        if tracked.bridge_error is not None:
            with state.mutation_context():
                maybe_release_service_record(state, tracked)
            raise tracked.bridge_error
        # Build and install Registration with SystemSource. External-name
        # collisions raise (caller is the server bring-up, so this
        # surfaces as a startup error).
        with state.mutation_context():
            ext = config.external_name
            if ext in state.service_external_names:
                raise ValueError(f"persistence external_name {ext!r} already registered")
            state.touch("services", tracked.short_id)
            registration = Registration(
                service_key=tracked.service_key,
                source=SystemSource(kind="persistence", metadata={"external_name": ext, "mode": config.mode}),
                external_name=ext,
            )
            tracked.registrations.append(registration)
            state.service_external_names[ext] = registration
        task_status.started()
        await tracked.shutdown_event.wait()
```

## Bring-up ordering (in WorkstationServer.__asynccontextmanager__)

1. Validate ``persistence_namespaces``: external_names unique inside
   the list; namespace ≠ None; mode is one of the two literals.
2. Open ``root_scope`` with ``include=[parent_scope,
   *(ns.scope for ns in passed)]``.
3. Open ``background_tg`` and construct ``WorkstationState``.
4. Build the Starlette app.
5. Spawn each ``persistence_cap_worker`` via
   ``await background_tg.start(persistence_cap_worker, state, config)``
   so the cap is fully ready (bridge bridged, registration installed)
   before we move on.
6. Spawn hypercorn, wait until listening, yield.
7. Shutdown adds: after the existing
   ``state.entity_records.clear()`` step but before the
   ``background_tg`` exits, walk ``state.service_records.values()`` and
   call ``release_registration`` on each remaining
   SystemSource-sourced registration. That fires the per-record
   ``shutdown_event`` and the cap workers wake, drain ``impl_scope``,
   exit. Then ``background_tg.__aexit__`` completes naturally.

## Wire / debug-UI changes

- ``client_protocol`` ``ServiceRecord`` snapshot field
  ``registering_connection_ids: list[UUID]`` stays as-is.
- New parallel field ``system_source_kinds: list[str]`` (sorted, debug-only).
- Both stay behind the ``debug`` projection flag.
- Debug UI: optional small tweak in 7d to render
  ``system_source_kinds`` as a separate column / chip. Defer.

## Test layout

- ``tests/test_persistence_7.py`` — round-trip with one and two
  ``SQLiteFxtrNamespace`` instances; restart test (same temp path,
  fresh server); list_pins / unpin; try_retrieve missing key returns
  None; preserve_provenance mode raises NotImplementedError until 7c.
- 7c additions: ``test_persistence_7c.py`` (or extend the same file)
  covering preserve-prov pin/retrieve round-trip, u-node lift on pin,
  retrieve-of-non-prov-tag error path.
- All tests open namespaces externally (per the design — workstation
  doesn't manage namespace lifecycle), so we don't have to test
  namespace bring-up edge cases ourselves.

## Open follow-ups / things to revisit

- Will the framework correctly serialize ``FxtrID | None`` over the
  wire for ``try_retrieve``? Likely yes — typed_service handles
  ``None`` for any ``T | None`` annotation. Sanity-check during 7b
  tests; if not, fall back to ``retrieve_or_sentinel(key) -> FxtrID``
  with a side-channel "missing" marker, but I don't expect to need it.
- The disconnect-sweep filter: walking
  ``record.entity_registrations``/``registrations`` and matching
  ``source is record`` already excludes SystemSource (since SystemSource
  isn't an RPCConnectionRecord). Verify in 7a.
- ``release_registration`` currently calls
  ``registration.source.registrations.remove(...)``: this is the only
  place that needs an explicit type-check. Cleanly handle with
  ``isinstance(registration.source, RPCConnectionRecord)``.
