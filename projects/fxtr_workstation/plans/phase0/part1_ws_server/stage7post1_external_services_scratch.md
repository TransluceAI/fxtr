# Stage 7 post-1 — design scratch

Working notes that don't fit in the plan body. Conclusions made it
in; this file is the trail.

## Why post-1 and not stage 8

Stage 7 already laid every pillar this needs:

- ``SystemSource`` registrations (7a).
- ``service_record_worker`` happily bridging from any source scope.
- ``WorkstationServer`` accepts a list of pre-opened user-supplied
  configs at construction, validates them, and spawns one worker
  per config via ``background_tg.start(...)``.
- A shutdown sweep that releases system-sourced registrations.

The only delta is "skip the namespace dance and let users hand us a
service ref + scope directly". So this is a sibling refinement of
stage 7's ideas, not a new phase.

## Bridge model — why bridge to root_scope, not include

For persistence, the namespace's scope goes into
``root_scope.include``. That's because the cap uses
``namespace.retrieve(into_scope=call_scope, copy_into_scope=False)``
which requires inheritance reachability for the lazy lift.

External services are different: callers reach them through the
ordinary ``service_scope`` bridge that ``service_record_worker``
sets up. They don't need the user's scope in the inheritance chain —
they need the *service ref itself* reachable from a scope the
workstation owns.

Two ways to get there:

1. **Bridge ``user_ref`` from ``user_scope`` directly into
   ``service_scope``** — pin user_scope to service_scope's lifetime.
2. **Bridge ``user_ref`` from ``user_scope`` into ``root_scope``
   first, then mint the record using ``(user_ref, root_scope)``** —
   pin user_scope to root_scope's lifetime; service_scope's worker
   does its own root_scope→service_scope bridge after that.

The user picked (2). Justification: root_scope is the workstation's
canonical "this exists for as long as the workstation does" anchor,
and pinning external services there matches the persistence cap
pattern visually (both end up "attached to root_scope"). It also
sidesteps a hypothetical edge case where service_scope drains
(e.g. last registration drops): with (1), user_scope drains too;
with (2), user_scope stays alive until workstation shutdown
regardless of registration churn. System-sourced registrations
don't churn today, but the looser coupling is friendlier.

Mechanics: ``state.root_scope.bridge_service_from_scope(ref,
user_scope)`` registers the ref under root_scope's known-handles
and clones a Lease that pins user_scope.aclose until root_scope
drains. After that, ``get_or_create_service_record(state, ref,
state.root_scope)`` is the standard call — service_record_worker
opens service_scope with ``include=[root_scope]``, bridges from
root_scope, and parks. **Strictly: the source-scope arg passed to
``get_or_create_service_record`` should be ``state.root_scope``,
not ``user_scope``** — the bridge is what makes root_scope the
authoritative owner.

## Lifetime ordering on shutdown

There's no per-external-service worker (see "Why no worker" in the
plan). Just:

1. Workstation shutdown: ``__aexit__`` walks every service record
   and releases each ``SystemSource``-sourced registration (7b
   logic, unchanged).
2. Each external service's ``service_record_worker`` (the standard
   one spawned by ``get_or_create_service_record``) wakes from
   ``shutdown_event``, drains its service_scope (releases the
   bridge from root_scope; service_scope's pin on root_scope ends).
3. ``background_tg`` finishes.
4. ``WorkstationServer`` drains root_scope (releases the bridges
   to user_scopes; user_scopes' pins end).
5. The user's outer ``async with FxtrScope(...) as user_scope``
   block can finally drain.

Same as persistence ordering, with one extra hop replacing the
``include`` chain (root_scope → user_scope bridge instead of
``root_scope.include = [..., user_scope]``).

## Config shape

User said "tuple[ServiceRef[Any], FxtrScope]" but I'm pretty sure
that was descriptive of the data, not a literal API ask. Consistent
with ``PersistenceNamespaceConfig``, this gets a frozen dataclass:

```
@dataclass(frozen=True)
class ExternalServiceConfig:
    external_name: str
    service: ServiceRef[Any]
    scope: FxtrScope
```

Three reasons over a literal tuple:

- Named fields read better at use sites.
- Easier to extend later (kind override, description, etc.) without
  breaking the constructor positional shape.
- Consistent with the persistence config; in 7d we surfaced the
  shared validation in ``WorkstationServer._validate_persistence_configs``,
  and the same pattern slots in here.

If the user prefers the tuple, swap is a 5-line change.

## SystemSource kind = "external"

User said "external". Hardcoded. The metadata dict carries
``external_name``, mirroring the persistence shape (``mode`` is
omitted — there's no mode here). Future override-able kind is a
trivial config-field addition.

Debug UI: stage 7d's ``system_source_kinds`` chip will now show
``system:external`` alongside ``system:persistence``. No code
change needed.

## Combined external-name validation

Persistence + external-services share the workstation's
external-name namespace (it's the same
``state.service_external_names`` dict at runtime). The construction-
time validation in ``WorkstationServer`` should walk both lists in
one pass and reject any duplicate across both. Refactor
``_validate_persistence_configs`` into a single
``_validate_system_service_configs`` that handles both.

## Test plan sketch

- **Round-trip.** Mount an `EchoProto` impl in a user-owned scope;
  hand it as an external service; RPC peer resolves and invokes.
- **Two external services co-exist.** Distinct external_names,
  distinct scopes, both reachable. Shutdown clean.
- **External + persistence in the same server.** One of each,
  distinct external names; both visible in the services snapshot;
  ``system_source_kinds`` shows the right kinds per record.
- **Combined external-name uniqueness.** External + persistence
  with the same ``external_name`` ⇒ rejected at construction (this
  exercises the unified validator path).
- **External vs external duplicate** ⇒ also rejected at construction.

Don't bother with concurrency / shutdown-mid-call (matches the 7d
trim — not contract behavior).

## Things I'm deliberately not doing

- **Entities.** User said no. Doing this later would widen
  ``EntityRegistration.source`` (still ``RPCConnectionRecord`` only
  today) and add a parallel mounting flow. The 7a impl-notes already
  flag this as the trigger. Out of scope.
- **Unifying with the persistence-cap worker.** They're parallel
  for now. Persistence has a "build impl, mount it on its own
  ``impl_scope``" prelude that external-services skips entirely;
  the post-bridge / record-mint / SystemSource-registration tail is
  identical. If the duplication starts hurting, factor out a shared
  helper that takes ``(state, source_ref, source_scope, external_name,
  kind, metadata)`` and does mint + register + park. Note in plan,
  not done now.
- **Override-able kind / extra metadata fields.** Punted.
