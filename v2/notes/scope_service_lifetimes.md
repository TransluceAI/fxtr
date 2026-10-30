# Scopes, Services, and Lifetimes

A guide to mounting live services into scopes, managing the resources a service
holds, and moving a service between scopes. This is the *live-capability* layer
that sits on top of scopes; it's the counterpart to
[representing_data_as_entities.md](./representing_data_as_entities.md), which
covers the immutable-data side.

A **service** is a live capability: an impl object mounted into a scope, callable
through a `ServiceRef`. Unlike an entity (immutable, content-addressed), a service
has a *lifetime* — it stays usable while its mount scope is open and stops when
that scope drains.

## Mounting a service

Define a service by writing a `@service_protocol` and a `TypedServiceImpl` for
it, then mount the impl into a scope:

```python
from typing import Protocol
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method

@service_protocol
class GreeterProto(Protocol):
    async def greet(self, name: str) -> str: ...

class Greeter(TypedServiceImpl[GreeterProto]):
    def _target_type_annotation(self): return GreeterProto
    def _service_type_id(self): return get_active_scope().track_builtin(FxtrID.builtin("greeter"))

    @service_method
    async def greet(self, name: str) -> str:
        return f"hello {name}"

async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE]) as scope:
    ref = await scope.mount(Greeter())        # mount is async
    proxy = await ref.resolve(scope)          # resolve → a typed proxy (GreeterProto)
    assert await proxy.greet("world") == "hello world"
```

(There's also an untyped flavor — `UntypedServiceImpl` with `@service_method`,
args/returns shaped as `FxtrServiceData` — when you don't have a fixed protocol;
the lifetime story below is identical.)

`mount` is **async** because it brings up the service's dispatch machinery before
returning the `ServiceRef`. The service is dispatchable as long as `scope` is
open; when `scope` drains, in-flight calls complete first, then the service tears
down. A scope can only host services if it has a task group — i.e. it was entered
as `async with` (not `lowlevel_start_lifespanless()`).

Each call runs in a fresh per-call child scope (so anything the method stores
without copying out is cleaned up when the call returns). The mount scope is
re-authorized for the call's duration, reachable via `_fxtr_get_mount_scope()`
(below) — but the *active* scope during a call is the per-call child, not the
mount scope.

## Holding per-mount resources: `_fxtr_mount_lifespan`

A long-running service often needs resources whose lifetime matches the mount
(an open socket, a background task, entities stored in the mount scope). Manage
them with the `_fxtr_mount_lifespan` hook — a single-yield async context manager
that runs setup before the service goes live and teardown after it drains:

```python
import contextlib

class Connection(TypedServiceImpl[ConnectionProto]):
    def _target_type_annotation(self): return ConnectionProto
    def _service_type_id(self): return get_active_scope().track_builtin(FxtrID.builtin("conn"))

    @contextlib.asynccontextmanager
    async def _fxtr_mount_lifespan(self, scope):
        async with open_socket() as sock:    # setup: authorized + `scope` active
            yield sock                        # the per-mount "lifespan state"
        # teardown: runs after the scope drains and all calls complete (unauthorized)

    @service_method
    async def send(self, msg: str) -> None:
        sock = self._fxtr_get_lifespan_state()       # this mount's socket
        mount_scope = self._fxtr_get_mount_scope()   # authorized for this call
        ...
```

Setup runs **authorized** and with `scope` set as the **active** scope, so it can
store entities / mount sub-services against the mount scope. Teardown runs **after
the token drains** (all calls done) and is **unauthorized** — external-resource
cleanup only.

**Don't keep a standing lease on the scope past your `yield`** (e.g. don't stash
`scope.clone_authorization()` and hold it). Teardown only runs once the framework
can guarantee no potential caller still holds a lease on the scope — it waits for
the scope's token to fully drain before firing. A standing lease keeps the token
from ever draining, so teardown never runs: the scope hangs on close. (Re-acquire
the mount scope per call via `_fxtr_get_mount_scope()` instead, which is
authorized only for that call's duration.)

**Stash per-mount state nowhere on `self`.** Carry it as the value you `yield`
(the "lifespan state"), and read it back in methods via
`self._fxtr_get_lifespan_state()`; reach the mount scope via
`self._fxtr_get_mount_scope()`. This keeps the same impl instance safe to mount
into two scopes — each mount gets its own independent state. (Both accessors
raise if called outside a dispatch.)

## Moving a service between scopes

A `ServiceRef` is resolvable only from scopes that can reach its handle. There are
three ways to make a service reachable from another scope, in increasing order of
"how independent are the two lifetimes."

**Recommendation: always `resolve` in the scope where you will *use* the
service, not the scope where it was mounted.** Resolving binds the proxy to a
home scope, and a call's arguments / returns are tracked against *that* scope. If
you resolve in the using scope, entities you pass in or get back line up with the
data you're already working with there.

### 1. Implicit, via scope inheritance (lifting)

A service mounted on a scope is automatically resolvable from any **descendant**
scope — `resolve` walks `self → parents` for the handle and installs a local
entry on first contact. No explicit step; just resolve in the child:

```python
async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE]) as parent:
    ref = await parent.mount(NoteWriter())
    async with FxtrScope(isolated=True, include=[parent]) as child:
        note = Note(body="from the child").store(child)   # data lives in `child`
        proxy = await ref.resolve(child)                  # resolve in the using scope
        await proxy.save(note)                            # arg tracked against `child`
```

Use this whenever the consumer is a child of the mount scope. Nothing to keep
alive explicitly — the child structurally keeps the parent alive already.

### 2. Cross-tree, `keepalive_via="independent_lease"`

When the consumer is **not** a descendant of the mount scope (a sibling tree),
bridge the ref across explicitly:

```python
await dest.bridge_service_from_scope(ref, source_scope, keepalive_via="independent_lease")
```

`dest` takes a standing lease on the service, so the **source closes after dest**.
The two scopes' lifetimes are independent except for that ordering constraint.

### 3. Cross-tree, `keepalive_via="concurrent_child_bridge"`

When you want a service mounted on a *child-of-dest* scope to be usable from
`dest` itself — and you want them to **close together** rather than dest-then-
source — use the concurrent bridge. This is the RPC service-return shape: a
service mounted on a per-call child scope, handed back to the caller.

```python
await dest.bridge_service_from_scope(ref, source, keepalive_via="concurrent_child_bridge")
```

`dest` holds no standing lease on the service; instead the service is gated on
`dest` so that closing `dest` cascades the source closed. It requires (and checks)
that `dest` is a direct scope-parent of `source`, that `ref` is mounted on
`source` itself, and that `source` has no extra non-parent derived inputs.

## The keep-alive contract (why `keepalive_via` is required)

Bridging declares a lifetime relationship, and the wrong arrangement deadlocks.
The rule both modes share: **`source` must never be forced to finish closing
before `dest`.** Two *independent* notions:

- **scope parent** — does `source` derive from / get included by `dest`? Known
  statically; the bridge checks it.
- **block nesting** — is `source`'s `async with` lexically nested inside `dest`'s
  *in the same task*? A per-task runtime property — **not** checkable, so it's a
  caller contract.

A scope blocked inside its own drain can't let an enclosing scope drain, so:

- `independent_lease` (source closes *after* dest): `source` must **not** be
  block-nested inside `dest`. Fine: unrelated scopes run concurrently, or `dest`
  nested inside `source` (`async with source: async with dest:`).
- `concurrent_child_bridge` (source and dest close *together*): `source` derives
  from `dest`, so it can't be outside `dest`, and nesting it inside `dest`
  self-deadlocks → the two **must drain in concurrent tasks** (e.g. `source` lives
  in a spawned task; dest's drain cascades it closed).

If you bridge a child into its parent and then close them by nesting (`async with
parent: async with child:`), it will hang — that's the contract asserting itself,
not a bug. Run them concurrently.

**A task group *inside* a scope is not concurrent enough.** A task group's
`async with` doesn't return until all its tasks finish, so a scope that wraps a
task group never reaches the end of its own `async with` body — and so never even
*starts* draining — until that task group has joined. Tasks spawned in a task
group nested inside `dest` therefore run entirely within `dest`'s still-open body,
before `dest` begins to close — not concurrently with its drain. To get genuine
concurrency, the task group must enclose *both* scopes, so `source` and `dest`
drain from sibling tasks (neither waits on the other).

## Debugging scope and lease hangs

A hung drain is usually a *lease wedge*: some holder of the draining scope's token never
releases. The tools in `fxtr2/debug/scope_report.py` exist to name the wedge — reach for
them **before** building behavioral reproductions, because the task-level views lie
("every task idle, nothing on the wire" is exactly what a lease cycle looks like, and
also what designed parking looks like).

- **Structure first.** `dump_scope_report()` prints every parked drain, the outstanding
  leases blocking it (holder-labeled where minted by the independent-lease bridge), each
  structural blocker scope with its parent edges classified (explicit `include=` vs
  implicit active-scope capture; same-task vs fork-inherited), and a hazard line for the
  implicit + fork-inherited class. Wrap hang-prone test bodies in
  `fail_after_and_report_hung_scopes(seconds)` so a wedge prints its own diagnosis at the
  deadline; wrap a long-running main loop in `report_hung_scopes_on_signal()` to get the
  same via `kill -USR1 <pid>`. Rerun with `FXTR_SCOPE_REPORT_TB=1` to add
  who-took-this-lease and who-created-this-scope tracebacks.
- **Designed parking is not a wedge.** A dispatch call scope parked in `__aexit__` while
  its caller holds returned dependent refs is the *designed* state (that is how returned
  refs stay alive), and a spawn-serving task living permanently in a CM exit is normal.
  The question is never "is something parked" but "what is the parked drain waiting on,
  and will that holder ever release".
- **Ambient context is an input your repro must copy.** A repro naturally duplicates the
  *visible* topology (scopes, bridges, call order), but every task start and non-isolated
  scope creation also consumes an invisible input: the ambient scope context at that
  point, which in a harness defaults to something different than in the real system.
  When reproducing a lifetime bug, find the task-start / non-isolated-scope sites along
  the real path and give each one the ambient context the real system has there,
  deliberately (`as_active_scope()` around the start) rather than by harness default. To
  check you got it right, compare `dump_scope_report()` output between the real system
  and the repro: the parent-edge classifications (implicit vs `include=`, same-task vs
  fork-inherited) must match, or the repro has a different lifetime graph than the bug.
- **One-sided RPC closes mutually wait.** Closing one side of an `open_rpc_scope` pair
  while the peer stays open parks: each side holds the other's bootstrap main-service
  lease. Peers must tear down concurrently.
