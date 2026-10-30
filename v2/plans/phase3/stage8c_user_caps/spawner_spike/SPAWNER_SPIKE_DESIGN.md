# Spawner / RPC-aware-entrypoint spike

A self-contained, single-process prototype of the eventual sandboxed
`SandboxSpawnerService` flow. No actual sandbox isolation here — the
"guest process" is just an `async` task in the same event loop, talking
over an in-memory `RPCMessage` stream pair.

## The shape we want

```
host program
   │
   ├─ async with create_spawner(entrypoints) as spawner_ref:
   │     # spawner_ref: ServiceRef[SandboxSpawnerProto], mounted on the
   │     # active scope inside create_spawner
   │
   │     spawner = await spawner_ref.resolve()
   │     async with FxtrScope(mountable=True) as session_scope:
   │         user_ref = await spawner.spawn("echo", config_id)
   │         # ↑ chain-bridged into session_scope
   │         user = await user_ref.resolve()
   │         await user.echo("hello")
   │     # session_scope drains → guest tears down cleanly
```

Two services with the same call shape, both using the same
"bridge-back via task_status.started" idiom.

## The bridge-back idiom

Used in both `GuestMainImpl.connect` and `SandboxSpawnerImpl.spawn`.
The idea: a service method launches a background task that needs to
*build* a ServiceRef in some other scope, then thread that ref back
to the foreground so the foreground can `bridge_from_scope` it into
the call scope and return it.

```python
async def the_method(self, arg) -> ServiceRef[Any]:
    call_scope = FxtrScope.get_active()
    teardown = anyio.Event()
    user_ref, source_scope = await call_scope._task_group.start(
        self._runner, arg, teardown
    )
    helper = await user_ref.bridge_from_scope(source_scope, into_scope=call_scope)
    teardown.set()
    return helper.ref  # framework chain-bridges to caller's scope

async def _runner(self, arg, teardown, *, task_status):
    # set up `source_scope`, build `user_ref` in it, then:
    task_status.started((user_ref, source_scope))
    await teardown.wait()
    # source_scope now starts to drain on context exit, but the bridge
    # the foreground installed in call_scope still holds a clone of
    # source_scope's main lease — drain blocks until the host caller
    # releases the chain-bridged ref. No manual lease wrangling.
```

The teardown event isn't a "tear me down" signal — it's a "go ahead
and start your shutdown sequence; the bridge is in place" signal.
The actual lifetime is governed by the bridge clone, not the event.

For `connect`, `_runner` is the "entrypoint runner": it opens an
isolated mountable `user_scope`, defines a one-shot `serve` callback
that does `task_status.started + await teardown`, and calls
`entrypoint(config, serve)`. The user code mounts whatever they want
on `user_scope` and calls `serve(my_ref)`.

For `spawn`, `_runner` opens a fresh in-memory transport pair, starts
`guest_runtime` on the spawner's outer task group, opens
`open_rpc_scope` on its end, calls `their_main.connect(config)`, and
threads the result back. `source_scope` is the `RPCSession.scope`.

## Lifetime nesting (single spawn)

```
host's user-active scope
└── (at create_spawner entry) host_scope ── mounts spawner_impl
    │
    └── (at spawn call) call_scope_outer (created by RPC dispatcher
        for the spawn method)
        │  ├── chain-bridge inward to spawn's call_scope
        │  └── helper bridges user_ref from session_scope into call_scope
        │
        └── _spawn_runner background task
            ├── transport pair created
            ├── guest_runtime started on spawner.guest_tg
            │   └── guest_host_scope ── mounts GuestMainImpl
            │       └── open_rpc_scope (guest side)
            │           └── (at guest's connect call)
            │               guest_call_scope
            │               └── _entrypoint_runner background task
            │                   └── user_scope ── mounts EchoConfigImpl
            │                       └── await serve  (parked)
            │
            └── open_rpc_scope (host side)
                └── session.scope contains user_ref bridge
                    └── await teardown_event  (parked)
```

## Teardown order (when host's session_scope exits)

1. session_scope drains.
2. Chain-bridge to spawn's call_scope releases.
3. spawn's call_scope drains; the helper's bridge releases its clone
   of session.scope's main lease.
4. session.scope (inside `_spawn_runner`) finishes draining, conn
   tears down, transport closes.
5. Guest sees EOF; guest's `open_rpc_scope` drains.
6. Chain-bridge to guest's `connect` call_scope releases.
7. Guest call_scope drains; `_entrypoint_runner`'s helper releases
   its clone of user_scope's main lease.
8. user_scope inside `_entrypoint_runner` finishes draining; user
   entrypoint returns; runner exits.
9. Guest's host_scope drains; guest_runtime returns.
10. spawner.guest_tg finishes one task. (`_spawn_runner` is on
    call_scope_outer's task group, which exits with call_scope_outer.)

## Things to verify in the spike

- Single spawn → call → exit works.
- Two spawns under back-to-back `FxtrScope(mountable=True)` sub-scopes
  each succeed and tear down cleanly.
- Config FxtrID round-trips correctly through both RPC hops.
- `guest_tg` and `host_scope` exit cleanly when `create_spawner`'s CM
  exits.

## Out of scope for this spike

- Sub-spawning (passing `spawner_ref` *into* a guest as a
  `ServiceRef[SandboxSpawnerProto]` so the guest can spawn its own
  sub-guests).
- Actual process / sandbox isolation.
- Multi-connect on a single guest (the spec allows it, and the impl
  supports it, but the demo doesn't exercise it).

The bridge-back idiom should generalize to all of these — that's the
property we're trying to validate.
