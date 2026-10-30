# Stage 9 — Stage 4: Service mount-scope context marker introspection

## Goal

Let a caller ask a resolved service "what is your mount scope's context
marker?" without ever shipping that marker on the wire by default.

After Stage 3, provenance flows through service calls, and a service's
mount scope can be a provenanced scope whose context marker is
`FxtrID(".", _provenance_parent=c_node)`. Today nothing exposes that
c-node to the caller — the only way for a caller to learn it would
be for the service to return it explicitly from a user method.
Stage 4 reserves the wire method-name `"$fxtr.ctx"` (no args, no
kwargs, returns the mount scope's `get_context_marker_id()`) and
makes every builtin service impl handle it for free. The reserved
`"$fxtr."` prefix also gives future framework-introspection paths
somewhere to live.

There is no required new surface on the proxy side — a caller
invokes the path by:

- **Untyped:** `await getattr(unknown_proxy, "$fxtr.ctx")()` — uses
  the existing `UnknownServiceProxy.__getattr__` machinery, no
  changes needed.
- **Typed:** declare a Protocol method with whatever name reads
  well, tagged with a new `@proxy_alias_method("$fxtr.ctx")`
  decorator that overrides the wire name — e.g.
  `async def service_mount_context(self) -> FxtrID`. The decorator
  is generic and may be used to alias any wire name, not just
  `$fxtr.ctx`.

Both routes return the live value of the mount scope's
`get_context_marker_id()` at the time of the call.

## Background — what already exists

- **Mount scope's marker.** After Stage 1, every scope has
  `get_context_marker_id() -> FxtrID`, which is either bare
  `FxtrID(".")` for un-provenanced scopes or
  `FxtrID(".", _provenance_parent=c_node)` for scopes constructed
  with `provenance_context=...`. Child scopes inherit the parent's
  marker by default, so a per-call dispatch scope returns the same
  marker as the mount scope.
- **`fxtr_call` / `fxtr_return` wire shape.** A service Call already
  has the machinery for shipping a `FxtrServiceData` return value
  alongside an `fxtr_arena` packet whose precomp closure lets the
  caller sync-decode any prov-bearing FxtrIDs in the response. This
  is exactly what we need to ship a marker — no new wire shape
  required.
- **`CommonServiceImplHandle._run_request_async`.** Every framework
  builtin (UntypedServiceImpl, NoOpService, future TypedServiceImpl)
  routes calls through this single dispatcher. Adding the
  introspection path here gets every concrete builtin for free.

## Design

### Wire model

The marker fetch is just a regular service Call: method name
`"$fxtr.ctx"`, no args, no kwargs. The return packet is the standard
`fxtr_return` shape carrying the marker as the `result` field, with
the c-node provenance parent traveling through the `fxtr_arena`
precomp closure exactly like any other prov-bearing FxtrID.

The framework reserves the `"$fxtr."` method-name prefix — any Call
on a `CommonServiceImplHandle` with a method name that starts with
`"$fxtr."` and isn't recognized by the framework is rejected with
`IRException(category="UnknownMethod")` (or a similar reserved-prefix
category) **before** delegating to `impl._handle_invoke`. This keeps
the namespace clean for future introspection paths and prevents any
collision with user-defined methods that happen to share the prefix.

### Server-side dispatch

The interception happens **inside `_run_user_method`**, the
dispatcher that already calls `impl._handle_invoke` after wiring
up the per-call scope, decoding args, and bridging the input arena.
The full dispatch machinery — wire-arena bridge, derived call scope,
args decode, result encode, return-packet build — already wraps
that one call, so routing reserved-prefix names to a *different*
impl method instead of `_handle_invoke` runs through identical
infrastructure as a regular user method. Subclasses don't see (and
can't accidentally override or forget to delegate) the framework
paths.

Concretely:

- **`CommonServiceImpl`** gains a concrete (non-abstract) helper
  used as the dispatch target for reserved-prefix names:
  ```python
  async def _handle_reserved_invoke(
      self, method_name: str, *args: FxtrServiceData, **kwargs: FxtrServiceData,
  ) -> FxtrServiceData:
      if method_name == "$fxtr.ctx":
          if args or kwargs:
              raise IRException(
                  message="$fxtr.ctx takes no arguments",
                  category="MalformedPacket",
              )
          return get_active_scope().get_context_marker_id()
      raise IRException(
          message=f"unknown framework method {method_name!r}",
          category="UnknownMethod",
      )
  ```
  Subclasses generally don't override it — it's the single
  centralized spot where future `$fxtr.*` paths go. (Strategies
  that *want* to extend the registered set can override and
  super()-call.)

  `get_active_scope()` here is the per-call derived call scope, which
  inherits the mount scope's marker via Stage 1's parent-marker
  inheritance — so its `get_context_marker_id()` is identical to
  `self._scope.get_context_marker_id()`. We use the active-scope
  form because it stays correct even if a future change moves the
  marker source.

- **`_run_user_method`** picks which method to call based on the
  wire `method_name`:
  ```python
  with call_scope.as_active_scope():
      if method_name.startswith(FXTR_FRAMEWORK_METHOD_PREFIX):
          result = await impl._handle_reserved_invoke(
              method_name, *decoded_args, **decoded_kwargs
          )
      else:
          result = await impl._handle_invoke(
              method_name, *decoded_args, **decoded_kwargs
          )
  ```

`SimpleAsyncServiceImpl._handle_invoke` and
`TypedServiceImpl._handle_invoke` are unchanged — they only ever
see user method names. That's the entire server-side change. The
`IRTagged(_FXTR_RETURN_TAG, …)` packet built by `_run_user_method`
(with the marker as the `result` field, the c-node prov parent
flowing through the standard fxtr_arena precomp) is identical in
shape to any other method return.

### Reserved-prefix enforcement

The reserved-prefix check is purely inside `_run_user_method` (per
the above). Whether the call comes from the wire or from an
in-process caller, that one site decides which impl method runs,
and `_handle_reserved_invoke` is the authoritative handler for
every reserved-prefix path.

A user can't accidentally register a `@service_method`-decorated
method that collides with the reserved prefix because `$fxtr.ctx`
isn't a valid Python identifier (you can't write
`def $fxtr.ctx(self): ...`). So no extra registration-time guard is
needed. If a user goes out of their way to register a colliding name
via `setattr` shenanigans, the dispatcher-level routing shadows it
silently (the reserved name never reaches `_handle_invoke`) — that's
a deliberate and obvious thing for the user to be doing, and not
worth defending against further.

### Proxy-side surface

The framework wire name `$fxtr.ctx` isn't a legal Python attribute
name, so by construction it can't collide with a normal method
declaration on a Protocol or proxy. We lean into that: rather than
adding any auto-magic, we expose two minimal accepted routes for
invoking the framework path. Either route is just a regular call to
the same `invoke_service_method(ctx, "$fxtr.ctx")`; neither involves
caching or any new always-present surface.

#### Untyped path: `getattr` on `UnknownServiceProxy`

`UnknownServiceProxy.__getattr__` already routes any non-dunder name
to `invoke_service_method`:

```python
def __getattr__(self, name: str) -> Callable[..., Awaitable[FxtrServiceData]]:
    if name.startswith("__") and name.endswith("__"):
        raise AttributeError(name)
    return functools.partial(invoke_service_method, self._ctx, name)
```

`"$fxtr.ctx"` passes the dunder check, so the existing implementation
already supports:

```python
marker = await getattr(proxy, "$fxtr.ctx")()
```

No code change to `UnknownServiceProxy` is needed — the only thing
this stage does on the untyped side is documenting that this is the
accepted route and adding a test that exercises it.

#### Typed path: `@proxy_alias_method(wire_name)` decorator

For typed services, the Protocol author opts in by declaring a
normal Python method with whatever name reads well, and tagging it
with a new `@proxy_alias_method` decorator that overrides the wire
name. The decorator is a generic wire-name alias — it has nothing
specifically to do with `$fxtr.ctx`; that's just its first user.

```python
from fxtr2.services import proxy_alias_method, service_protocol

@service_protocol
class MyService(Protocol):
    @proxy_alias_method("$fxtr.ctx")
    async def service_mount_context(self) -> FxtrID: ...

    async def some_method(self, x: int) -> str: ...
```

Implementation: the decorator stashes the wire name on the function
(e.g. `func.__fxtr_proxy_wire_name__ = wire_name`); the existing
`_make_proxy_method` in `typed_service.py` reads that attribute (if
present) and uses it as the `method_name` argument to
`invoke_service_method` instead of the Python method name. That's a
one-line lookup change in `_make_proxy_method` plus the small
decorator function.

No impl-side change is needed for `$fxtr.ctx` specifically: when the
wire name lands on the receiver, `_handle_invoke`'s prefix check
short-circuits to `_handle_framework_introspection` before any
typed-method lookup runs, so the `TypedServiceImpl` subclass doesn't
need to (and shouldn't) define a body for the aliased method. The
Protocol declaration is purely a type-checker-visible contract for
callers.

If a future caller wants to use `@proxy_alias_method` for a non-
framework wire name, the impl side would need to dispatch on that
name — but that's outside this stage. The decorator is generic; this
stage's only built-in user is `$fxtr.ctx`.

### Tracking on the home scope

When `invoke_service_method` returns the marker, the existing wire
return path (`_install_returned_resources` etc.) lifts the new
FxtrIDs into `home_scope`'s main arena. So after one fetch:

- `home_scope.is_tracked(marker._provenance_parent)` is True.
- Subsequent calls to `home_scope.unwrap_provenance(marker._provenance_parent)`
  succeed sync, because the precomp closure that arrived with the
  return arena populated `home_scope`'s sync caches.

No special handling is needed in this stage — we get this for free
from Stage 3's wire arena machinery.

### What "the mount scope's marker" means in the chain

For a service mounted directly on scope A, the marker comes from A.
For a service whose handle has been bridged into other scopes (via
the wire, `bridge_service_from_scope`, or any chain converter), the
marker still comes from A — every dispatch on the bridged handle
ultimately reaches the same `CommonServiceImplHandle` registered on
A, which opens its per-call derived call scope as a child of A.
That call scope inherits A's marker (Stage 1's parent inheritance),
so `get_active_scope().get_context_marker_id()` inside
`_handle_framework_introspection` returns A's marker. Bridges are
transparent to the introspection call by construction.

If a user attaches a *custom* `IRHandle` (not a
`CommonServiceImplHandle`) as a service via
`attach_bridged_lowlevel_cap`, that handle is responsible for
implementing `"$fxtr.ctx"` itself. If it doesn't, the call raises
`UnknownMethod` on the wire, which proxies surface as an
`IRException`. This is acceptable — custom-handle authors are already
responsible for the wire contract; if they want introspection, they
implement it.

## Out of scope

- Caching on the proxy. Each `await proxy.service_mount_context()`
  is a fresh roundtrip. If/when we measure this as a hot path we can
  add memoization without changing the public API.
- Generalizing to other framework-reserved paths
  (`$fxtr.type_id`, `$fxtr.handle_id`, etc.). The reserved-prefix
  enforcement is set up so we can add new paths without further
  framework refactoring, but no new paths are added in this stage.
- Persisting the marker through `FxtrNamespace`. Same rule as the
  rest of Stage 9: namespaces drop provenance.
- Async-property syntactic sugar. The proxy-side surface is a plain
  `async def` method.

## Implementation steps

1. [x] **Add `_handle_reserved_invoke` on `CommonServiceImpl`.**
   Concrete async method in `fxtr2/services/service_cap_common.py`
   next to `CommonServiceImpl`. Handles `"$fxtr.ctx"` (returns
   `get_active_scope().get_context_marker_id()` after empty-payload
   validation) and rejects any other `"$fxtr.*"` name with
   `IRException(category="UnknownMethod")`.

2. [x] **Centralize the reserved-prefix dispatch in `_run_user_method`.**
   At the single `await impl._handle_invoke(...)` site, branch on
   `method_name.startswith(FXTR_FRAMEWORK_METHOD_PREFIX)` — when
   matched, await `impl._handle_reserved_invoke(...)` instead;
   otherwise call `impl._handle_invoke(...)` as today. No changes
   needed to `SimpleAsyncServiceImpl._handle_invoke` or
   `TypedServiceImpl._handle_invoke` — they only ever see user
   method names. (Subclasses can't accidentally shadow / forget the
   framework paths because the dispatcher never hands them to
   `_handle_invoke`.)

3. [x] **Add `@proxy_alias_method(wire_name: str)` decorator.** New
   public symbol in `fxtr2/services/typed_service.py`. (Skipped
   the `__init__.py` re-export — the `services` package never
   re-exports through `__init__.py`; users import via
   `from fxtr2.services.typed_service import proxy_alias_method`,
   matching every other typed-service public symbol.)
   Implementation is one function:
   ```python
   def proxy_alias_method(wire_name: str) -> Callable[[F], F]:
       def deco(func: F) -> F:
           func.__fxtr_proxy_wire_name__ = wire_name  # type: ignore[attr-defined]
           return func
       return deco
   ```
   Empty-string validation at decoration time; no isinstance check
   (the type annotation is enough — pyright was flagging the
   isinstance branch as unreachable, and the codebase doesn't use
   `# type: ignore[unreachable]` anywhere).

4. [x] **Read the wire-name override in `_make_proxy_method`.**
   The override is captured into a new `_MethodSpec.wire_name`
   field at `_build_method_wrappers` time (default = `name`), and
   `_make_proxy_method` passes `spec.wire_name` to
   `invoke_service_method`. The Python attribute name still drives
   `__name__` / `__qualname__` and how callers reach the method.
   `_walk_protocol_methods` and `walk_service_methods` both walk
   `vars(klass).items()` and return the function reference
   unchanged, so the `__fxtr_proxy_wire_name__` attribute survives
   into `_build_method_wrappers`.

5. [x] **Tests** (in `tests/test_services/test_service_mount_context.py`):
   - **Untyped happy-path on a provenanced scope.** Mount an
     `UntypedServiceImpl` (or `NoOpService`) on a scope built with
     `provenance_context=c1`. Resolve as `UnknownServiceProxy`.
     Call `await getattr(proxy, "$fxtr.ctx")()`. Assert the
     returned `FxtrID` has `get_tag() == "."` and
     `_provenance_parent == c1`.
   - **Untyped on un-provenanced scope.** Same setup, default
     scope; assert the marker is the bare `"."` builtin
     (`_provenance_parent is None`).
   - **User method coexists with the framework path.** Sanity-check
     that a normal `@service_method` on the same impl still
     dispatches alongside the framework path (added beyond the
     plan as a regression guard).
   - **Typed alias roundtrip.** A test Protocol with
     `@proxy_alias_method("$fxtr.ctx") async def
     service_mount_context(self) -> FxtrID`. Mount a
     `TypedServiceImpl` subclass that does *not* implement
     `service_mount_context`. Resolve, call
     `await proxy.service_mount_context()`, assert the marker
     matches the mount scope's marker. Also covers the
     un-provenanced typed case.
   - **Reserved-prefix rejection on the wire.** Use
     `getattr(proxy, "$fxtr.foo")()` and
     `invoke_service_method(ctx, "$fxtr.nope")` directly; assert
     `IRException(category="UnknownMethod")` on both untyped and
     typed dispatch paths.
   - **Bridged dispatch reaches the mount scope's marker.** Mount
     on scope A (provenanced with `c1`), bridge into a sibling
     scope B via `bridge_service_from_scope`, resolve in B, fetch
     the marker, assert it still reflects `c1`.
   - **Empty-payload enforcement.** Pass a positional and a
     keyword arg to `$fxtr.ctx`; assert
     `IRException(category="MalformedPacket")` for each.
   - **Tracking on the home scope.** After one fetch, assert
     `home_scope.is_tracked(marker._provenance_parent)` is True
     and `home_scope._main_arena.unwrap_provenance(...)` succeeds
     synchronously and returns the ground-`.` data id.

6. [x] **Update `CODE_OVERVIEW.md` and `fxtr2/services/LOCAL_CODE_OVERVIEW.md`.**
   Mentioned the reserved `$fxtr.` prefix on the receiver
   (`CommonServiceImpl._handle_reserved_invoke`, with dispatch
   centralized in `_run_user_method`), `@proxy_alias_method` as the
   typed-side opt-in (with the `_MethodSpec.wire_name` plumbing),
   and the `getattr(proxy, "$fxtr.ctx")()` route for the untyped
   side.

## Files touched

- `fxtr2/services/service_cap_common.py` — new
  `FXTR_FRAMEWORK_METHOD_PREFIX` / `FXTR_CTX_METHOD_NAME` constants;
  `CommonServiceImpl._handle_reserved_invoke`; reserved-prefix
  dispatch branch in `_run_user_method`.
- `fxtr2/services/typed_service.py` — `@proxy_alias_method`
  decorator + `_PROXY_WIRE_NAME_ATTR` constant; `_MethodSpec.wire_name`
  field; `_build_method_wrappers` reads the wire-name override;
  `_make_proxy_method` passes `spec.wire_name` to
  `invoke_service_method`.
- `tests/test_services/test_service_mount_context.py` — new file.
- `CODE_OVERVIEW.md`, `fxtr2/services/LOCAL_CODE_OVERVIEW.md`.

Note: `fxtr2/services/untyped_service.py` does **not** need to
change — the existing `__getattr__` already supports
`getattr(proxy, "$fxtr.ctx")()`. `SimpleAsyncServiceImpl._handle_invoke`
and `TypedServiceImpl._handle_invoke` are likewise unchanged — the
dispatch happens at the `_run_user_method` layer above them.

## Open questions / follow-ups

- **Generalizing the reserved-prefix dispatch.** Today this stage
  hard-codes `$fxtr.ctx`. If/when we add a second introspection
  path, refactor to a small handler registry keyed on method name.
  Don't speculatively build that registry now — wait for a second
  caller.
- **`@proxy_alias_method` for non-framework wire names.** The
  decorator is generic, but the only built-in wire user is
  `$fxtr.ctx`. If a Protocol author aliases to a non-framework
  string (e.g. `@proxy_alias_method("legacy_name")`), the impl
  side currently has no matching dispatch — the call would land
  in the regular method-name lookup under the alias, which the
  impl doesn't implement under that name. We could add a parallel
  impl-side decorator if a use case appears; not worth building
  now.
- **Custom IRHandle authors.** Anyone hand-rolling an `IRHandle`
  and attaching it as a service is currently free to ignore
  `"$fxtr.ctx"`. We could publish a small mixin
  (`FrameworkIntrospectionMixin`) that handles it; deferred until
  a concrete use case appears.
