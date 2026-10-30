# Design: stateless service providers

A stateless service provider turns **service handle entities** into running
services. Handles are content-addressed identities; spawning is reproducible
by contract — a service is either *built* stateless (nix-defined, sandboxed,
hermetic) or *promised* stateless (external, bridged in, implicitly trusted;
intent is to keep these few). The workflow runner consumes only the spawner
capability and is agnostic to which kind backs a handle.

> Naming: this piece is a "provider" (it provides running service instances
> from handles), not a "manager". The package is
> `fxtr2.workflows.stateless_service_provider`.

Two implementations:

- **Basic provider** (`fxtr2.workflows.stateless_service_provider.basic_provider`)
  — external services only, no caching or special logic. The minimum that
  (a) implements the spawner protocol the workflow system needs and (b) lets
  you attach external services.
- **Sandboxed provider** (`projects/fxtr_sandboxing`, new module) — everything
  the basic provider does, plus the ability to convert a nix git-flake
  reference into a `NixStatelessServiceHandle` it can recognize and instantiate
  inside sandboxes.

## Handle types & protocols

Handle *entities* (`stateless_service_provider/handles.py`):

```python
class ServiceHandle[T](Entity, abc.ABC):
    """Empty marker base: 'I identify a stateless service of protocol T'."""
```

Service *protocols* (`stateless_service_provider/protocols.py`):

```python
@service_protocol
class StatelessServiceSpawnerProto(Protocol):
    async def spawn(self, handle: EntityRef[UnknownEntity]) -> ServiceRef[UnknownService]: ...
```

The wire protocol is intentionally untyped (per-method TypeVars aren't
supported by the typed codec *yet* — relaxing this so `spawn` can be typed and
`spawn_resolved` removed is a tracked followup) and intentionally minimal: it
pins down only "an object you can *use* to spawn services by key", not how
externals or hermetic services get registered. A python-side helper recovers
typing from the handle's phantom param:

```python
async def spawn_resolved(spawner, handle_ref: EntityRef[ServiceHandle[T]]) -> T:
    # spawner.spawn(...) -> ref.replace_service_type(T-from-annotation) -> resolve()
```

**Capability split**: the root provider object (registration surface) is
distinct from the spawner sub-service it mints. If you own the provider you can
register services and derive the spawner; if you only hold the spawner you
cannot upgrade it to register anything.

## Basic provider (`basic_provider.py`)

`BasicStatelessServiceProvider` is implemented as an actual fxtr2 service
(`TypedServiceImpl`), so it can be threaded through a workstation or across
RPC. It deliberately exercises the **impl mount lifespan** system: all its
internal state (registration map, the mounted spawner sub-impl, its sub-mount
scope handling) lives in its `_fxtr_mount_lifespan` handler rather than an
external context manager. It mostly does not own async lifespans itself — the
workflow runner (or whatever hosts it) owns the enclosing scope.

Root API:

```python
@service_protocol
class StatelessServiceProviderProto(Protocol):
    async def register_external(
        self, key: EntityRef[UnknownEntity],
        connector: ServiceRef[ExternalConnectorProto],
    ) -> EntityRef[ExternalServiceHandle]: ...
    async def spawner(self) -> ServiceRef[StatelessServiceSpawnerProto]: ...

@service_protocol
class ExternalConnectorProto(Protocol):
    async def connect(self) -> ServiceRef[UnknownService]: ...
```

- **`ExternalServiceHandle(key: EntityRef[UnknownEntity])`** — wraps an
  arbitrary registrant-chosen key entity; the handle entity's id (a function of
  the key id) is the cache identity. Registering the same key in a later
  session against a different-but-equivalent connector is exactly the "promised
  stateless" trust contract.
- **`connect()`** takes no args and returns the service the task should use.
  Statelessness is the registrant's promise. A `wrap_singleton(scope, ref)`
  helper mounts a connector that just returns a known-stateless singleton.
- **`spawn(handle)`**: look up the connector registered for the handle's key →
  `connect()` → return the resulting ref. Unknown key → error. No caching.
- **Bridging pattern** for callers in other scopes/processes: same mechanism
  as the workstation backend's external services —
  `scope.bridge_service_from_scope(ref, source_scope,
  keepalive_via="independent_lease")`.

## Sandboxed provider (`fxtr_sandboxing`, new module)

> Flag: the sandboxed provider builds on `fxtr_sandboxing`'s existing
> spawn/connect machinery, which this design has not re-read in full. The
> sketch below is the intended shape; **the precise wiring into
> `fxtr_sandboxing` is worth another look when we write the part-5 plan**, and
> some details may shift then.

Same protocol surface as the basic provider (external registration included),
plus nix-built hermetic services. Holds a sandbox environment (created at
runtime, on the existing `create_sandbox_environment` machinery), a nix backend
+ git repo root (like the spawner does today), and supports **both runtimes**
(wasm/eryx and container) — decision 7.

### Handle + provenance

**One entity type per spawn kind, strictly 1-1 with the nix attrset.** Rather
than a single handle with a `runtime` discriminator + always-present fields, we
map each nix string discriminator to its own entity type and carry *exactly*
the fields that case has — no redundant fields. So the wasm handle carries
`entry_point` (it's part of the wasm attrset / sitePackages closure) and the
container handle does **not** (the entrypoint is baked into the image):

```python
# binding values are EntityRef[NixStatelessServiceHandle], where
NixStatelessServiceHandle = NixWasmStatelessServiceHandle | NixContainerStatelessServiceHandle

class NixWasmStatelessServiceHandle(DataclassEntity):      # ServiceHandle[...]
    drv_hash: str                  # sitePackages closure derivation hash
    entry_point: str               # "module:function" — part of the wasm attrset
    config: EntityRef[UnknownEntity] | None
    bindings: dict[str, EntityRef[NixStatelessServiceHandle]]

class NixContainerStatelessServiceHandle(DataclassEntity):  # ServiceHandle[...]
    drv_hash: str                  # nix2container image manifest derivation hash
    config: EntityRef[UnknownEntity] | None                 # entrypoint is baked
    bindings: dict[str, EntityRef[NixStatelessServiceHandle]]  # into the image
```

Each handle entity's FxtrID — covering its drv hash, (wasm) entry point, config
entity, and binding handles recursively — is the cache identity. General
principle: string discriminators (`kind` / `runtime`) map to distinct entity
types, each faithfully describing only the fields its discriminated case
actually has.

At registration time, each handle entity is given a **provenance parent**
c-node carrying `{git_commit, flake_path, flake_attr}` (the attr recursing into
the binding path for nested handles). Because the workflow cache key strips
provenance parents (runner decision 2), this provenance is automatically *not*
consulted for caching — it exists for debugging and state tracking. Possible
extensions (followups): use it as a check that a binding was actually
authorized; and use the *handle entity itself* as the provenance source for
services spawned statelessly, rather than the host's config-walk approximation
(see note at the end).

### Nix attrset schema (helpers in root `nix/`)

A stateless service is configured in nix as a *new* attrset kind alongside the
existing `NixWasmServiceDefn` / `NixContainerServiceDefn`:

```nix
{
  kind = "fxtr-stateless-service";
  runtime = "wasm" | "container";
  service = <derivation>;        # nix2container image manifest (container)
                                 # or sitePackages closure (wasm)
  entryPoint = "module:function";
  config = { ... };              # plain JSON-able value; stored as an entity
                                 # at registration time
  bindings = { <name> = <attrset of this same shape>; ... };
}
```

The `nix/` helpers produce this shape from a user package + entrypoint. The
provider never trusts nix2container internals — it only reads the manifest path
via the derivation, as the existing spawner does.

### Registration walk

`register_flake_service(git_commit, flake_attr, flake_path=None) ->
EntityRef[NixStatelessServiceHandle]`:

1. Depth-first walk of the attrset via `nix eval` (same
   `--no-update-lock-file` git-flake-ref conventions as `nix.py` /
   `spawner.py`), recursing into `bindings` first.
2. Per node: read `kind` / `runtime` and dispatch on it to the matching handle
   entity type; read the fields that case carries (`entryPoint` for wasm,
   `config`); get the principal derivation's `drvPath` hash; store the config
   value as an entity; mint the handle entity + its provenance c-node.
3. Record `drv_hash → (git_commit, flake_attr_path, flake_path)` in the
   provider's **source map** — the state needed to actually build & spawn
   later. (A handle is only spawnable on a provider that registered it, v0.)

### Spawning

`spawn(handle)` dispatches on handle type:

- `ExternalServiceHandle` → as in the basic provider.
- `NixWasmStatelessServiceHandle` / `NixContainerStatelessServiceHandle`:
  1. Look up the source map by `drv_hash` (unknown → error).
  2. Get-or-create a running sandbox for this drv hash from the
     **spawned-sandbox cache**, honoring `max_guests_per_sandbox`: each cached
     sandbox counts its `connect()` calls; once the budget is used, the next
     guest gets a freshly spawned sandbox. (Deferred refinement, tracked as
     TODO: count *concurrently active* guests via the connect-result tokens,
     i.e. `max_concurrent_guests_per_sandbox`, plus sandbox pooling.)
  3. `connect()` with a composite config (the existing
     `SandboxConnectProto.connect(config)` path — see guest seam below):
     `{config: <EntityRef>, bindings: {name: <EntityRef[handle]>, ...},
     spawner: <ServiceRef[StatelessServiceSpawnerProto]>}`.
  4. Return the resulting service ref.
- `release_cached_sandboxes()` drops the cache so new requests spawn fresh
  guests (the workflow-level "make sure I get a clean service" knob).

The spawner passed to guests is the provider's own spawner sub-service in v0
(a guest can therefore request any registered handle, not just its declared
bindings — restricting this, e.g. via the provenance-authorization check, is a
followup). This is what makes bindings work: every handle gives exactly enough
information to instantiate a fresh impl, and the guest resolves its declared
parents on demand by calling `spawner.spawn(binding_handle)`.

### Guest seam — reuse, no new wire kind (OQ-8)

We reuse **both** the existing sandbox connect protocol
(`SandboxSpawnerProto` → `SandboxConnectProto.connect(config)`, RPC bridging,
per-connect `SandboxProvenanceContext`) **and** the existing guest-side
adapter. The "stateless service" layer is expressed *in* the connect config —
no new defn/protocol kind, no bespoke entrypoint wrapper.

Mechanism: expose a `StatelessServiceConfig` dataclass (in
`fxtr_sandbox_rpc_adapter` or a sibling) with the three fields:

```python
@dataclass
class StatelessServiceConfig:
    config: EntityRef[UnknownEntity] | None
    bindings: Mapping[str, EntityRef[UnknownEntity]]   # binding name -> handle
    spawner: ServiceRef[StatelessServiceSpawnerProto]
```

A user entrypoint annotated as receiving a `StatelessServiceConfig` then just
works through the existing connect path — the guest decodes the composite
config into this dataclass and calls the user entrypoint directly. For workflow
task functions, the returned service implements `TaskFnProto`; the runner's
slot services (step ctx, `TaskStorage` args) reach the task fn through its
`run` args as usual — those ServiceRefs bridge through the existing sandbox RPC
path unchanged.

### Construction & no-nix mode

`SandboxedStatelessServiceProvider(cache_dir=..., git_repo_path=...,
max_guests_per_sandbox=N)`. "A stateless service provider without any nix
backend" is just the basic provider in `fxtr2.workflows` — no degraded mode of
the sandboxed one is needed.

## Implementation staging

- **Part 1**: `handles.py` + `protocols.py` + `basic_provider.py` with tests:
  registration, spawn, capability split (spawner ref can't register), singleton
  helper, handle entity round-trips, mount-lifespan behavior.
- **Part 5**: the sandboxed provider + nix helpers + the `StatelessServiceConfig`
  seam, with integration tests in `fxtr_sandboxing` (gated like the existing
  sandbox tests). Both runtimes exercised; bindings resolved guest-side via the
  spawner; `max_guests_per_sandbox` rollover; `release_cached_sandboxes`.
  (Re-read `fxtr_sandboxing`'s connect machinery first.)
