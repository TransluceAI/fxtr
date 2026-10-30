# 2p1 — New RPC System (overview)

## Goal

Replace the deleted fxtr-RPC bridge in `fxtr_sandboxing` with the new `open_rpc_scope` +
`ServiceRef` system from fxtr2. Reshape the sandbox boundary so that:

- The user-written sandbox entry point is just a service author — it mounts a service and
  hands a ref to a `serve()` callback.
- The host treats every sandbox as a service-spawning capability: `spawn(bundle).resolve()`
  returns a long-lived guest handle whose `connect(config)` method instantiates a fresh
  user service per call. Multiple `connect`s on one sandbox are allowed.
- The "sandbox host runner" is itself a `ServiceRef[SandboxSpawnerProto]`, so a sandbox
  can be given the spawner ref and use it to launch sub-sandboxes — recursively.
- Network transports are first-class capabilities: each is a
  `ServiceRef[NetworkHandleAuthorizer]` that vends single-use tokens. Tokens are redeemed on
  the existing side channel for an actual byte stream — RPC stays out of the data path.

The implementation rides on `open_rpc_scope` + the typed-service machinery from
`fxtr2/rpc/scope_bridge.py` and `fxtr2/services/`. Lifetime nesting and teardown are
expressed entirely via lease bridges — no manual coordination between the spawner and
the in-flight calls.

## Reference: the spawner spike

`v2/examples/service_spawner/service_spawner_example.py` already exercises the host/guest
shape in a single process (memory-stream pair instead of a real sandbox). The spike is
the closest thing to a working spec; this plan is what it takes to graft the spike's
pattern onto the actual eryx + container runtimes, plus four design refinements:

1. **`spawn` returns a long-lived service ref, not a one-shot.** The spike's `spawn(name,
   config)` returns `ServiceRef[Any]` directly (one connect per spawn). The new shape is
   `spawn(bundle) -> ServiceRef[SandboxConnectProto]` and `.connect(config) ->
   ServiceRef[T]` is a separate hop. This matches what `open_rpc_scope` actually
   exposes (the guest's main service stays mounted for the connection's lifetime) and
   lets one sandbox serve multiple `connect`s.
2. **No `call_scope._task_group` reaching.** Both the host- and guest-side runners hold
   their own outer task groups. The spawner system doesn't reach into `FxtrScope`
   internals; `start()` and the bridge-back idiom run on those owned task groups.
3. **Service-type IDs use `system_entities.define_type`** (with a new namespace UUID for
   sandbox-shared types). No `track_builtin` for these.
4. **Bundle is a real entity ref, not a name.** `spawn(bundle)` accepts an
   `EntityRef[Any]` or `FxtrID`; it's normalized internally to
   `EntityRef[TaskBundle | DockerTaskBundle]`. No registry of named entry points.

## Big-picture architecture

### Packages (new and reshaped)

```
fxtr_sandbox_types/                  # NEW. Shared type/protocol definitions.
  pyproject.toml                     #   Pure Python, depends on fxtr2.services
                                     #   (works against either fxtr2 or fxtr2-portable —
                                     #   user picks; see "Open issues" below).
  fxtr_sandbox_types/
    __init__.py
    _namespace.py                    # SHARED_SANDBOX_NS UUID for define_type.
    entities.py                      # PySourcePackage, PyWheelPackage, TaskBundle,
                                     #   DockerImage, SourceMount, DockerTaskBundle.
                                     #   Moved from fxtr_sandboxing/entities.py.
    protocols.py                     # @service_protocol classes:
                                     #   SandboxConnectProto[T] (connect(config: T) -> ServiceRef[Any])
                                     #   SandboxSpawnerProto    (spawn(bundle) -> ServiceRef[SandboxConnectProto[Any]])
                                     #   NetworkHandleAuthorizer (get_transport_token() -> str)
                                     #   — see stage docs for the exact shape per stage.

fxtr_sandbox_rpc_adapter/            # Renamed/rewritten. Sandbox-side runtime.
  pyproject.toml                     #   Depends on fxtr2-portable + fxtr_sandbox_types.
  fxtr_sandbox_rpc_adapter/
    __init__.py                      # Re-exports `main` and (later) `connect_transport`.
    _main.py                         # async def main(ctx) — looks up the
                                     #   `fxtr.service` entry point, mounts a
                                     #   _GuestMainImpl, opens open_rpc_scope on the
                                     #   adapted ctx stream, blocks on its body
                                     #   until the host disconnects.
    _guest_main.py                   # _GuestMainImpl(entrypoint_fn, guest_tg).
                                     #   `connect(config)` runs the entrypoint on
                                     #   guest_tg, bridges the user's ref into
                                     #   call_scope, returns it.
    _object_stream.py                # _JSONObjectStream — adapts ctx.send/receive into
                                     #   anyio ObjectStream[JSONTypes]. Kept verbatim
                                     #   from the old _adapter.py (modulo location).
    _transport.py                    # (stage 3) connect_transport(token) global —
                                     #   side-channel handshake, returns httpx transport.

fxtr_task_runner/                    # Reduced. In-container shim.
  fxtr_task_runner/
    __main__.py                      # python -m fxtr_task_runner: connect to
                                     #   /run/fxtr/task.sock, build TaskContext, hand
                                     #   off to fxtr_sandbox_rpc_adapter.main(ctx).
                                     #   No more entry-point lookup here.
    context.py                       # TaskContext (unchanged-ish).
    protocol.py                      # length-prefixed JSON framing (unchanged).
    transport.py                     # (stage 3) replaced/reshaped for the
                                     #   token-based side channel.

fxtr_sandboxing/                     # Host-side; entities moved out, rpc_runner gone.
  fxtr_sandboxing/
    spawner.py                       # NEW. SandboxSpawnerImpl,
                                     #   create_sandbox_environment async CM,
                                     #   transport-registry plumbing.
    runner.py                        # WASM low-level launch helpers (no longer
                                     #   user-facing CMs — only used by spawner.py).
    container_runner.py              # Container low-level launch helpers (ditto).
    rpc_runner.py                    # DELETED.
    entities.py                      # DELETED — moved to fxtr_sandbox_types.
    prepare.py / container_prepare.py  # Updated to import entities from
                                     #   fxtr_sandbox_types.
    proxy.py                         # TransportRoute, proxy_connection — kept as-is
                                     #   in stage 1, reshaped in stage 3.
```

### User-facing flow

User-side service (sandbox):

```python
# my_service/__init__.py
from fxtr2.data.entity_ref_scope import get_active_scope, ServiceRef

async def run(config, serve):
    impl = MyServiceImpl(config)               # uses config (FxtrID by default)
    my_ref = get_active_scope().mount(impl)
    await serve(my_ref)                        # returns when host releases
    # post-serve cleanup goes here
```

```toml
[project.entry-points."fxtr.service"]          # renamed from "fxtr.task"
service = "my_service:run"
```

Host-side caller:

```python
async with create_sandbox_environment(cache_dir=...) as env:
    spawner = await env.spawner_ref.resolve()
    sandbox_ref = await spawner.spawn(my_bundle_ref)
    async with FxtrScope(name="session"):           # bound the connect's lifetime
        sandbox = await sandbox_ref.resolve()
        user_ref = await sandbox.connect(my_config)
        user = await user_ref.replace_service_type(MyServiceProto).resolve()
        result = await user.echo("hi")
    # session scope drains => connect's bridges drop => user_scope tears down
```

### Lifetime layering

Outer → inner, just like the spawner spike:

```
host application scope
  └─ create_sandbox_environment (transport-listener task group + spawner mount)
       └─ spawn() call_scope (caller's scope opening the spawn)
            └─ _spawn_runner task on env's task group
                 └─ open_rpc_scope conn to one guest process
                      └─ guest's GuestMain mounted; connect() spawns:
                           └─ _entrypoint_runner task on guest_tg
                                └─ user_scope (mountable, isolated=False)
                                     └─ user's serve() awaiting teardown_event
```

Drain on each level releases its bridges; the next level above unblocks. No explicit
cross-task coordination needed.

## Stages

The work is sized into four sequential stages so we can validate the design incrementally
without waiting for the whole tower to come up. Each stage produces a working,
test-covered subset.

### Stage 1 — basic eryx flow

Establish the new shape end-to-end on WASM only, without transports or nested spawn.

- Expand `fxtr2-portable` to ship the full pure-Python fxtr2 surface (services, leases,
  rpc, scope_bridge — anything that doesn't pull a C extension).
- Create `fxtr_sandbox_types` package; move `entities.py`; add `SandboxConnectProto[T]` +
  `SandboxSpawnerProto` (spawner stubbed to NotImplementedError on transport features).
- Rewrite `fxtr_sandbox_rpc_adapter` around `open_rpc_scope` + `_GuestMainImpl`.
- Update eryx `_runner.py` to call `fxtr_sandbox_rpc_adapter.main(TaskContext())`.
- Build `SandboxSpawnerImpl` + `create_sandbox_environment` for WASM only; container
  branch raises `NotImplementedError`.
- Move existing examples + their tests into `examples/old_broken/` and
  `tests/old_broken/`. Add one new `rpc_task` demo + small test set proving the new shape
  (spawn → connect → echo, plus spawn-once-connect-twice).
- Stub network-transport surfaces with `NotImplementedError`.

See `stage1_basic_eryx.md` for the detailed plan.

### Stage 2 — containers

Make the container path work alongside WASM, sharing the spawner shape.

- Update `fxtr_task_runner.__main__` to delegate to `fxtr_sandbox_rpc_adapter.main(ctx)`
  (drop entry-point lookup from the task runner).
- Update `container_runner.py` so `SandboxSpawnerImpl.spawn` can dispatch on bundle
  type. `DockerTaskBundle` opens a per-spawn UDS, runs the container, hands the
  resulting JSON-typed stream to `open_rpc_scope`.
- Restore a container demo (probably the spawner-spike pattern but in a real container).
- Tests for the container path mirroring stage 1's eryx tests, skipped without a runtime.

See `stage2_containers.md` for the sketch.

### Stage 3 — network transports as services

Replace the route-name-keyed transport system with token-keyed capabilities.

- Add `NetworkHandleAuthorizer` impls owned by `create_sandbox_environment`'s
  transport registry. Each call to `get_transport_token()` mints a fresh unguessable
  token and registers it in the env's token table.
- Reshape the side channel:
  - **Eryx**: `_transport_connect(token)` instead of `_transport_connect(name)`. Host
    looks the token up in the env-level registry, runs `proxy_connection()` against
    the resolved `TransportRoute`. Token is consumed on connect.
  - **Container**: routing header on the multiplexed UDS becomes
    `{"token": "..."}` instead of `{"transport": "..."}`. Same redemption semantics.
- Add `fxtr_sandbox_rpc_adapter.connect_transport(token)` global. Reads a contextvar
  that `fxtr_sandbox_rpc_adapter.main` populates with the runtime-specific connect
  callable (eryx callback wrapper / UDS dialer). Returns an httpx transport.
- Tests covering: token-mint, single-use enforcement, end-to-end HTTP via
  `connect_transport`, header injection / path filtering still working at the host.

See `stage3_transports.md` for the sketch.

### Stage 4 — nested spawn + polish

Use the now-complete shape to validate the recursive case and port the most useful old
demos.

- Demo: WASM A spawns WASM B, B does an external HTTP call via a NetworkHandleAuthorizer
  bridged in via `config`, A aggregates and returns. Test asserts everything tears down
  cleanly when the outer caller releases the result.
- Container variant of the same flow (or a simpler container demo if that's enough).
- Pick 1–2 of the old `examples/old_broken/` demos and port them onto the new service
  shape (e.g. one "yaml-style" example using a wheel dep, one editable-dep example).
- Integration tests that exercise both transports + nested spawn together.

See `stage4_nested_and_polish.md` for the sketch.

## Cross-cutting decisions captured here

These were settled during design discussion and aren't repeated in each stage doc.

- **Entry-point group renamed `fxtr.service`.** Reflects the new "register a service" model.
- **`_bridge_back_and_signal` is inlined** at both call sites (host-side spawner, guest-side
  GuestMain). Host and guest don't share Python code, so the helper has no real reuse.
- **Spawner does not touch `FxtrScope` internals.** `create_sandbox_environment` opens an
  outer task group; `SandboxSpawnerImpl` holds a reference to it and uses
  `task_group.start(...)`. Same on the guest side: `fxtr_sandbox_rpc_adapter.main` opens a
  guest task group and the `_GuestMainImpl` holds it.
- **Service type IDs via `system_entities.define_type` only — never `track_builtin`.**
  Each package defines its own UUID namespace as needed (`SHARED_SANDBOX_NS` in
  `fxtr_sandbox_types/_namespace.py` for cross-process sandbox protocols; example
  packages get their own UUIDs for their own protocols). `define_type` is called at
  module top-level so the resulting basic-type entity lands in the global scope and
  any isolated runtime scope that inherits from global picks it up automatically — no
  per-call tracking helper.
- **`config` is decoded against the user protocol's annotation from day one.**
  `SandboxConnectProto` is generic on `T`; the user's entrypoint declares its own
  config type (`FxtrID`, `EntityRef[SomeConfigObject]`, a typed dataclass, etc.) and
  the adapter routes through
  `fxtr2.services.service_dialect.encode_to_service_data` /
  `decode_from_service_data` (already landed). Stage 1 exercises this with an
  `EntityRef[SomeConfigObject]` config to prove the conversion path.
- **`run_rpc()` is removed.** Callers go through `SandboxSpawnerImpl` exclusively.
- **Bundle parameter shape.** `spawn` accepts `EntityRef[Any] | FxtrID`. Internally
  normalized to `EntityRef[TaskBundle | DockerTaskBundle]`; the user's annotation is
  ignored.
- **Tokens are unguessable, single-use.** Implementation: 32 bytes of `secrets.token_hex`
  + per-env in-memory dict. Consume on side-channel connect.
- **Old examples and tests go to `examples/old_broken/` + `tests/old_broken/`.** They stop
  running but remain as a reference; we port the useful ones in stage 4.

## Open issues / things to revisit

- **`fxtr_sandbox_types` deps.** Resolved: ship as extras
  (`fxtr_sandbox_types[fxtr2]` and `fxtr_sandbox_types[fxtr2-portable]`), base deps
  empty, with a README note explaining the pairing.
- **Transport routing for nested spawns ("just works" via env-level registry).**
  Nested sandboxes aren't actually nested at the runtime layer: every sandbox a given
  `Environment` spawns is a sibling under that env, regardless of who asked for the
  spawn. So if the outer sandbox sends an inner sandbox a `NetworkHandleAuthorizer`
  ref, the inner sandbox can mint a token and dial its own (env-owned) side channel
  with it — the outer sandbox isn't on the data path at all. Capability-style
  permissions still hold because the inner sandbox can only redeem authorizer refs
  that have been explicitly bridged into it. Stage 3 implements this directly; no
  proxying-up logic needed.
- **`_GuestMainImpl` outliving the connection.** The spawner spike re-mounts a fresh
  GuestMain per connection (`open_rpc_scope` body is a single `pass`). For multi-connect
  to work, the GuestMain must live across all `connect()`s for one spawn — which it does
  naturally inside `open_rpc_scope`'s `user_scope`. Confirm during stage 1 that the
  guest's main service really stays callable for as long as the host holds `sandbox_ref`.
- **Top-level `__init__.py` re-exports.** What goes on `fxtr_sandboxing` vs.
  `fxtr_sandbox_types` top-level imports. Defer to stage 1 implementation.

## Update CODE_OVERVIEW.md

Each stage's plan ends with a "Update CODE_OVERVIEW.md" step. The cross-package layout
shifts in stage 1; later stages will mostly be incremental edits to the existing
sections.
