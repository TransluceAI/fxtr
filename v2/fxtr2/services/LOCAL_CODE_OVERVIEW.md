# Services Local Code Overview

Detailed documentation for the user-facing service-capability layer. For
a high-level overview, see the root `CODE_OVERVIEW.md`; for a usage-oriented
guide (mounting, lifespans, moving services between scopes), see
`notes/scope_service_lifetimes.md`.

## What this package provides

The `fxtr2.services` package is the *user-facing* live-capability layer
on top of the lower-level `fxtr2.leases` Cap-IR machinery. Where
`fxtr2.leases` gives you typed `IRHandle`s and `Lease`s for individual
capabilities, this package gives you:

- A user-level wire shape for "call a method by name on a remote
  object, with arguments / return values that may include FxtrIDs and
  ServiceRefs."
- An ABC stack for *implementing* services. Two strategies share the
  base `CommonServiceImpl[T]`:
  - `SimpleAsyncServiceImpl[T]` → `UntypedServiceImpl` — dynamic
    vocabulary; method dispatch by tagged-method name; args/returns
    must be `FxtrServiceData`-shaped.
  - `TypedServiceImpl[T]` — typed vocabulary; per-method args / returns
    pass through a mashumaro-driven conversion layer with the impl's
    own annotations as the source of truth.
- An `UnknownServiceProxy` that reflects ``ServiceRef[UnknownService]``
  refs into a Python object with `__getattr__`-driven async methods,
  for callers who don't statically know the protocol.
- A `@service_protocol` decorator that registers a typed `Protocol`
  class. The matching proxy is generated dynamically from the
  Protocol's method annotations using the same dialect-driven
  conversion as the impl side, but with independent annotations
  (impl and proxy aren't required to be identical — only
  wire-compatible).
- The standard explicit-subscription contract (`Subscriber` /
  `UnsubscribeHandle` / `EventSource` in `subscription_types.py`) and
  `SubscriberHub`, a reusable broadcast helper for implementing the
  source side (`subscription_util.py`).

`FxtrScope`-side bookkeeping (mount / attach / resolve / cap-bridge
storage) lives in `fxtr2/data/entity_ref_scope.py` alongside `FxtrScope`
itself; this package is purely the dispatch / vocabulary layer that
sits *on top* of that bookkeeping.

## File overview

| File | Purpose |
|---|---|
| `service_cap_common.py` | Wire constants (``fxtr_call`` / ``fxtr_return`` / ``service_ref`` / ``fxtr_arena`` slot fields / dep-ret-ref slot fields, plus `FXTR_FRAMEWORK_METHOD_PREFIX` / `FXTR_CTX_METHOD_NAME` / `FXTR_TYPE_METHOD_NAME` for the reserved `$fxtr.*` introspection paths), the `UnknownService` wire-vocabulary alias, the recursive `FxtrServiceData` vocabulary + prov-aware encode/decode/validation helpers, the `_build_fxtr_arena_packet` / `_parse_fxtr_arena_packet` helpers for the self-describing `fxtr_arena` shape, the `CommonServiceImpl[T]` ABC (with concrete `_handle_reserved_invoke` dispatching `$fxtr.ctx` and `$fxtr.type` — the latter from the impl-instance `_cached_service_type_id` populated at mount time) and its `CommonServiceImplHandle` (server-side dispatch — `_run_user_method` routes reserved-prefix names to `_handle_reserved_invoke` and bypasses subclasses' `_handle_invoke`), the `walk_service_methods` helper + `@service_method` marker shared by both untyped and typed strategies, the `_ChainConverterHandle` and `_build_chain_bridge` machinery (returning capabilities whose lifetime is gated by the call scope), the caller-side install pair `_install_return_arena` (pre-decode arena bridge) + `_install_return_handles` (post-decode handle bridges + unused-entry validation), and the `invoke_service_method` proxy-side entry point. |
| `untyped_service.py` | The dynamic-vocabulary layer: re-exports `@service_method`, defines `SimpleAsyncServiceImpl[T]` (uses `walk_service_methods` for `_handle_invoke`), `UntypedServiceImpl` (= `SimpleAsyncServiceImpl[UnknownService]` with `_service_type_id` returning `FxtrID.builtin("untyped_service")`), and `UnknownServiceProxy` (registered against `UnknownService` in the service-resolver registry; `service_type_id()` is an async method that wire-dispatches `$fxtr.type` and returns the impl's runtime type identity in *loadable* form — bridged into the home scope so it can be `EntityRef.load`-ed. The same id as an opaque hash is also on `proxy._ctx.service_type_id` for callers that don't need to load it). |
| `service_dialect.py` | The typed-service mashumaro dialect: `FxtrServicesDefaultDialect` plus per-type strategies for `EntityRef[T]` / `ServiceRef[T]` (using `GenericSerializableType` helpers that strip on encode and re-attach `T` on decode), `FxtrID` (passthrough), `frozenset` / `set` / `frozendict` (collection conversions), `FxtrServiceData` (passthrough — opt-out of conversion for slots typed as untyped wire data), and `OverrideThisTypeBeforeUsing` (a `Never` stub alias whose strategy fails encode/decode until callers substitute a real type via `ServiceRef.replace_service_type`). Registers the matching failing `ServiceResolver` for that alias. Plus the `(annotation, dialect) → BasicEncoder/Decoder` codec cache (`get_basic_encoder` / `get_basic_decoder`) and the user-facing converters `encode_to_service_data(value, annotated_type, *, dialect=...)` / `decode_from_service_data(value, annotated_type, *, dialect=...)` for converting dynamically against a runtime-chosen annotation. Re-exports `FxtrServiceData` and `NotFxtrServiceDataError` from `service_cap_common` for convenience. |
| `typed_service.py` | The typed-service layer: `@service_protocol` decorator for registering a `Protocol` class, `TypedServiceImpl[T]` ABC for impl bases, the dynamic wrapper-dataclass codec build (per-method `_Args[*type_params]` / `_Result[*type_params]` plain dataclasses generated via `dataclasses.make_dataclass` and threaded through the dialect's codec cache), and the proxy-class factory that dynamically builds a class with one async method per Protocol method. Both proxy and impl drive their wire shape via `inspect.BoundArguments`: encode the wrapper, strip `USE_REMOTE_DEFAULT`, hand the dict to a fresh `BoundArguments`, ship `.args` / `.kwargs` as natural positional / keyword wire slots. The `@proxy_alias_method(wire_name)` decorator overrides the wire method-name a Protocol method ships when invoked (e.g. so a Python-named `service_mount_context` can target the framework `$fxtr.ctx` path, or a `service_type_id` async method can target `$fxtr.type`); the override is captured in `_MethodSpec.wire_name` and consumed by `_make_proxy_method`. No special-cased reserved Protocol attribute — type-id introspection is opt-in per Protocol via the alias decorator on a regular Protocol method whose return annotation drives the typed decoder. **Protocol subclassing**: `@service_protocol(allow_protocol_subtypes=True)` marks a Protocol as a subclassable base (registers its resolver with `allow_implicit_overrides=True`); decorated subclasses register their own resolvers (most-specific wins at resolve), undecorated subclasses fall back to the base's resolver with the base *method vocabulary* — but TypeVar bindings made via inheritance are honored (`_proxy_parameterization` recovers them from the inheritance chain plus any use-site args, so `class Sub(Base[int])` resolves with the same typed codecs as `Base[int]`). Inherited methods get genuine TypeVar substitution: the method walk reports each method's defining class, annotations are resolved against the *defining* class, and `_typevar_subst_map` composes per-hop parameterization maps from `types.get_original_bases` (keyed on TypeVar objects, so same-name TypeVars don't alias; an unparameterized base hop leaves the base's TypeVars unsubstituted → bound fallback). The same machinery covers `TypedServiceImpl` subclasses inheriting `@service_method`-tagged methods from generic impl bases. |
| `subscription_types.py` | The explicit-subscription protocol primitives: `Subscriber[T]` (`on_event(payload) -> None` — no return value, no in-band "stop" signal), `UnsubscribeHandle` (`unsubscribe()` — best-effort, also the release signal), and `EventSource[T]` (`subscribe(subscriber) -> handle`). Contract: subscribing for a duration obligates calling `unsubscribe` when done (e.g. in a `finally`); sources must additionally drop subscriptions whose `on_event` raises (crash backstop). Declares **no** type ids — type identity belongs to impls. `Subscriber` / `EventSource` are marked `allow_protocol_subtypes=True`: protocols that want to *be* event sources subclass `EventSource[<payload>]` and inherit `subscribe` with the substituted typed param (re-declaring it purely for a docstring is fine — override wins with identical annotations). `T` is unbounded with a documented contract — it must *serialize to* `FxtrServiceData` under the dialect in play (wire conversion is asymmetric: sender's annotation drives encode, receiver impl's drives decode). `SubscriberHub` keeps its `FxtrServiceData` bound (it genuinely stores wire values). |
| `subscription_util.py` | `SubscriberHub[T]` — reusable broadcast helper for `EventSource` implementors. Constructed with `(task_group, mode)` — the hub has **no scope of its own**: events carry their residency via origin-scope leases. `publish(value, origin_scope=...)` is sync (default origin = the active scope; `None` for entity-free payloads, usable from unauthorized contexts); it captures one `clone_authorization()` of the origin and each live subscription's pending event holds its own clone, consumed into a transient per-delivery scope (`open_with_explicit_leases` parented on `{sub_scope, origin}`) at delivery time or discarded when superseded/dropped — so payload entities deliver zero-copy even after the origin drained, and "events are held only until delivered, then dropped" is structural. `subscribe(subscriber_ref, source_scope, seed=..., seed_origin=...)` spawns two tasks per subscription (via `task_group.start`, so the subscription is fully established before it returns): a **worker** owning an isolated `sub_scope` (`include=[GLOBAL_SCOPE]` only — subscriptions pin nothing of the source's; *hold the source ref to keep the source, hold the handle to keep your subscription*) with the subscriber's bridge and the delivery loop (`"coalesce"` latest-value skip-ahead / `"queue"` in-order FIFO — unbounded v1, bounds/backpressure TODO), and a **holder** owning an isolated `hold_scope` hosting the mounted `_HubUnsubscribeHandleImpl`, which parks in `hold_scope`'s natural drain after bridging the handle out — the drain completing *is* handle-drop detection (peer released or died → teardown without waiting for a failed delivery). `seed` values are pushed before the entry is appended (no-await no-dup-no-miss window) and precede the live stream. Subscriptions are not deduplicated. Teardown is forceful (`force_close` of `sub_scope`) on every exit path; any delivery failure (`on_event` raised, broken origin lease) kills the whole subscription (crash backstop — subscribers resubscribe for a fresh view) and cancels the holder so the peer's handle breaks. `aclose()` flushes every live subscription (unbounded — bounded close = cancel the owning task group) then cancels holders+workers; `publish`/`subscribe` raise once it has begun. Defines `SUBSCRIPTION_UTIL_NS` + `HUB_UNSUBSCRIBE_HANDLE_TYPE_ID` for the handle impl it ships. |
| `cap_ir_arena.py` | Adapter that lets a `ReadableArena` cross a Cap-IR dispatch boundary — `ArenaIRHandleAdapter` (server-side wrapper exposing an arena as an `IRHandle`, with introduction-refcount bookkeeping in `_total_introductions`), `CapIRArenaRemoteState` + `CapIRArena` (client-side `ReadableArena` that dispatches to the remote handle, mirroring introduction counts in `_remote_refcounts` and refcounting precomp values in `_refcounts`), and the `_encode_fxtr_id` / `_decode_fxtr_id` / `compute_and_track_precomp_closure` / `collect_precomp_ids` / `ingest_precomp` / `_decode_flat_data` wire helpers. Used by `service_cap_common.py` to bridge the `fxtr_arena` slot in service-call packets. |

## ABC stack

```
FxtrServiceImpl[T]              # in fxtr2/data/entity_ref_scope.py
    └── CommonServiceImpl[T]    # in service_cap_common.py
        ├── SimpleAsyncServiceImpl[T]  # in untyped_service.py
        │   └── UntypedServiceImpl     # in untyped_service.py
        └── TypedServiceImpl[T]        # in typed_service.py
```

`SimpleAsyncServiceImpl` and `TypedServiceImpl` are siblings — neither
inherits from the other. They share only the `@service_method`
discovery convention via the free `walk_service_methods` helper in
`service_cap_common.py`.

Each layer adds one piece:

- **`FxtrServiceImpl[T]`** is the bare ABC with one abstract method,
  `async _mount_as_fxtr_service(scope)` (no `task_group` — the impl creates its
  own dispatch task group inside the lifespan it attaches). Lives next to
  `FxtrScope` because mount registration is a scope-internal operation.
- **`CommonServiceImpl[T]`** locks in the wire shape: subclasses
  implement `_handle_invoke(method_name, *args, **kwargs)` (per-call
  entry), `_target_type_annotation()` (the type the resulting
  `ServiceRef[T]` carries), and `_service_type_id()` (mints /
  looks up the runtime type identity in the active mount scope's main arena
  and returns its `FxtrID`). The provided `async _mount_as_fxtr_service`
  attaches a **framework lifespan** to the scope (`await
  scope.attach_lifespan_to_scope(...)`): the lifespan runs the user
  `_fxtr_mount_lifespan(scope)` setup (authorized + with `scope` active), opens
  the impl's **own** dispatch task group nested inside it, builds a
  `CommonServiceImplHandle(impl, scope, dispatch_tg, lifespan_state)`, registers
  via `scope.mount_lowlevel_cap_as_service(handle, target_type, type_id)`, and
  yields the `ServiceRef` (returned by `attach_lifespan_to_scope` once setup
  completes). `CommonServiceImpl` also provides the no-op-default
  `_fxtr_mount_lifespan` async-CM hook (yields per-mount *lifespan state*) plus
  the `_fxtr_get_mount_scope()` / `_fxtr_get_lifespan_state()` /
  `_fxtr_get_input_scope()` accessors, which read the per-call
  `(handle, input_scope)` record from the module-level
  `_CURRENT_DISPATCH_CONTEXT` contextvar (`_dispatch_body` binds it around the
  invoke; all raise outside a dispatch). Each dispatch runs in a scope *pair*:
  a lifespanless **input scope** (parent: `GLOBAL_SCOPE` only) owning the
  wire-input leases, the bridged-in `fxtr_arena`, and everything decoded from
  them, and the **call scope** (parents: mount scope + input scope; has the
  task group) the user method runs in. Retaining
  `_fxtr_get_input_scope().clone_authorization()` keeps the call's data
  loadable past the dispatch *without pinning the mount token* (it pins the
  callers' arg exports instead) — the sanctioned alternative to retaining a
  call-scope lease, which holds `Lease[mount.main]` via the call token and
  would deadlock graceful teardown. The unretained input scope closes with
  the call token (`discard_base_authorization` fires right after the
  response send).
- **`SimpleAsyncServiceImpl[T]`** owns the `@service_method`
  discovery convention: `_handle_invoke` walks the MRO looking for
  tagged async methods, raises `IRException(category="UnknownMethod")`
  on misses. Subclasses still supply `_target_type_annotation` and
  `_service_type_id`.
- **`UntypedServiceImpl`** = `SimpleAsyncServiceImpl[UnknownService]`
  with `_target_type_annotation` returning `UnknownService` and
  `_service_type_id` returning
  `scope.track_builtin(FxtrID.builtin("untyped_service"))` — all
  untyped impls share the same type identity, so peers can recognise
  "this is an untyped service" without per-impl coordination.
- **`TypedServiceImpl[T]`** layers per-method mashumaro conversion
  on top of `CommonServiceImpl`. Subclasses tag methods with
  `@service_method` (same as untyped), declare a class-level
  `_service_dialect` (defaults to `FxtrServicesDefaultDialect`), and
  override `_target_type_annotation` / `_service_type_id`. Generic
  impls additionally override `_own_full_type_for_serialization` to
  return a parameterized form (e.g. `MyImpl[int]`) so their
  TypeVar-bearing method annotations can be substituted at mount
  time. `__init_subclass__` validates method shape and runs an
  eager unparameterized codec build to surface annotation errors at
  class-definition time; mount-time then builds per-instance codecs
  bound to the concrete parameterization.

## Framework method-name prefix (`$fxtr.`)

The wire method-name prefix `$fxtr.` is reserved for framework
introspection. The constants live in `service_cap_common.py`:

- `FXTR_FRAMEWORK_METHOD_PREFIX = "$fxtr."` — any wire method-name
  starting with this is intercepted before user dispatch.
- `FXTR_CTX_METHOD_NAME = "$fxtr.ctx"` — takes no args/kwargs;
  returns the live value of the mount scope's
  `get_context_marker_id()` (a `FxtrID`).
- `FXTR_TYPE_METHOD_NAME = "$fxtr.type"` — takes no args/kwargs;
  returns the impl's runtime type identity. The impl's
  `_service_type_id()` is invoked once at mount time and cached on
  the `CommonServiceImpl` instance as `_cached_service_type_id`;
  the dispatch path returns the cached value (which is already
  tracked in the mount scope, so the per-call return arena bridges
  it into the home scope automatically).

`CommonServiceImpl._handle_reserved_invoke(method_name, *args,
**kwargs)` is a concrete async method that handles every registered
path and raises `IRException(category="UnknownMethod")` for
unrecognized `$fxtr.*` names. The dispatch happens in
`_run_user_method`: when the wire `method_name` starts with
`FXTR_FRAMEWORK_METHOD_PREFIX`, the dispatcher calls
`impl._handle_reserved_invoke(...)` and skips `impl._handle_invoke`
entirely. Subclasses don't see (and can't accidentally override or
forget to delegate) the framework paths. No registration-time guard
is needed either — `$fxtr.foo` isn't a syntactically valid Python
identifier, so user `@service_method`-tagged methods can't collide.

Caller-side accepted routes for invoking the framework paths:

- **Untyped, raw route:** `await getattr(unknown_proxy, "$fxtr.ctx")()`
  / `await getattr(unknown_proxy, "$fxtr.type")()`. The existing
  `UnknownServiceProxy.__getattr__` accepts any non-dunder string
  and delegates to `invoke_service_method`.
- **Untyped, typed accessor:** `await unknown_proxy.service_type_id()`
  is an async method on `UnknownServiceProxy` that wire-dispatches
  `$fxtr.type` and returns the impl's type identity in *loadable*
  form (bridged into the home scope's main arena, suitable for
  `EntityRef.load`). Advanced users that just need the type_id as an
  opaque hash — no loading required — can read
  `proxy._ctx.service_type_id` directly: same `FxtrID`, no wire
  round-trip, but not tracked locally.
- **Typed:** apply `@proxy_alias_method(wire_name)` to a Protocol
  method declaration. The decorator stashes
  `__fxtr_proxy_wire_name__` on the function; `_make_proxy_method`
  reads it and uses `wire_name` as the `method_name` argument to
  `invoke_service_method` (the Python attribute name still drives
  the proxy method's display name and how callers reach it). For
  type-id introspection, declare an explicit Protocol method with
  the alias — typically:

  ```python
  @service_protocol
  class MyService(Protocol):
      @proxy_alias_method("$fxtr.type")
      async def service_type_id(self) -> FxtrID: ...
      # or, with a typed descriptor entity:
      # async def service_type_id(self) -> EntityRef[MyTypeDescriptor]: ...
  ```

  The Protocol method's return annotation drives the typed decoder
  (a bare `FxtrID` rides through unchanged; an `EntityRef[T]` is
  re-attached on decode by the dialect's strategy). The decorator
  is generic — any non-empty wire name works — but the only
  first-class supported targets today are `$fxtr.*` paths.

The marker's c-node provenance parent (when the mount scope was
constructed with `provenance_context=...`) rides through the
ordinary `fxtr_return` arena precomp closure, so the home scope
ends up tracking it after one fetch and can then sync-unwrap it.
No special wire shape is needed. The `$fxtr.type` return likewise
rides through the return-arena like any other `FxtrID` — no special
casing.

## Typed services: how the wire substitution works

Per method, the typed layer dynamically generates two plain
`@dataclass`-frozen wrapper classes (NOT `DataClassDictMixin` — the
mixin compiles its codec eagerly at class-creation, before the
dialect can be supplied):

- `OwnerCls__methodname__Args` with one field per signature parameter.
  Field names mirror the user's parameter names one-to-one — the
  user-spelled `*args` / `**kwargs` names become regular fields with
  type `tuple[anno, ...]` / `dict[str, anno]` and a default of `()` /
  `{}`. There's no reserved-name swap; the wrapper is purely a
  per-side construct, never appears on the wire as a structure.
- `OwnerCls__methodname__Result` with a single field `value` for the
  return annotation.

Both wrappers share the `__parameters__` (TypeVars) of the owning
class. At mount/resolve time, the parameterized form
(e.g. `_Args[int]`) is passed to `BasicEncoder` /
`BasicDecoder`, and mashumaro's existing TypeVar propagation walks
the wrapper's fields substituting `T → int` everywhere — including
inside `EntityRef[T]` / `ServiceRef[T]` strategy helpers (which use
`GenericSerializableType` to receive the substituted args at
encode/decode time).

**Wire shape.** A typed service call ships *natural* positional /
keyword args — the same `args=[…]` / `kwargs={…}` slots untyped
calls use, just with values that have been driven through the typed
codec. The mechanism:

- **Proxy.** `sig.bind(self, *user_args, **user_kwargs).apply_defaults()`
  → encode the wrapper instance → strip `USE_REMOTE_DEFAULT` entries
  → hand the resulting field-keyed dict to a fresh
  `inspect.BoundArguments(sig_no_self, encoded)` → ship its
  `.args` / `.kwargs` directly via `invoke_service_method`. The
  `BoundArguments` split walks the signature in declaration order,
  routing positional-only and pre-`*args` params to the wire
  positional slot and the rest to wire keyword. A missing entry in
  the middle (because its `USE_REMOTE_DEFAULT` was stripped) makes
  it switch from `.args` to `.kwargs` cleanly, so a missing
  positional-or-keyword slot naturally falls through to keyword.
- **Impl.** `impl_sig.bind(*wire_args, **wire_kwargs)` collects the
  wire into a name-keyed dict by impl parameter names → decode the
  args wrapper from that dict → strip `USE_REMOTE_DEFAULT` → call
  `method(*new_bound.args, **new_bound.kwargs)` using a fresh
  `BoundArguments`.
- **Return.** The single-field `_Result` wrapper rides through
  `post_encoder_func` (encode side strips the `value` envelope) and
  `pre_decoder_func` (decode side re-wraps), so the wire ships the
  bare return value.

This bind/encode/strip/split pattern means the wrapper field names
never appear on the wire — only the user's parameter names appear, and
only in the keyword slot for keyword-passable params. Cross-side
variadic-rename tolerance is automatic: `*items` on the proxy and
`*things` on the impl agree because the variadic content travels as
bare positional args, no name attached.

Strict in/out vocabulary at the dialect boundary: encode strips
`EntityRef[T] → FxtrID` and `ServiceRef[T] → ServiceRef[UnknownService]`;
decode refuses anything other than the bare form and re-attaches `T`.
The conversion layer is the single source of typed/untyped
translation; the lower `service_cap_common` encode/decode chain stays
untyped.

Typed Protocols can mix typed slots and untyped slots in the same
signature by annotating individual parameters / returns as
`FxtrServiceData` (the recursive vocabulary type alias). The dialect
registers it as a passthrough strategy, so the value flows through
unchanged in both directions — useful when an impl wants to receive
a value untyped on the wire and decode it dynamically against an
annotation chosen at runtime via
`encode_to_service_data` / `decode_from_service_data` (in
`service_dialect.py`). The optional-parameter widening uses
`_UseRemoteDefault | anno` (sentinel first), not `anno | _UseRemoteDefault`,
so mashumaro's `pack_union` runs its `issubclass(get_type_origin(arg),
Collection)` check on `_UseRemoteDefault` (a real class) and skips
the check for any same-packer follow-on member. This keeps
`anno = FxtrServiceData` working without special-casing — its
`TypeAliasType` origin would otherwise crash the check.

## Typed services: default handling

Defaults are applied at *call* time, not stored on the wrapper
dataclass:

- **Proxy side.** `inspect.bind(...).apply_defaults()` resolves
  Protocol-side defaults to concrete values before the wrapper is
  instantiated. The wrapper class never holds a Protocol's actual
  default — for an optional Protocol param the wrapper field's
  default is the sentinel `USE_REMOTE_DEFAULT`, never a mutable
  literal. Mutable Protocol defaults (`x: list[int] = [1, 2]`)
  therefore aren't a problem.
- **Impl side.** The wire may omit a field entirely; the impl
  wrapper field's `USE_REMOTE_DEFAULT` default fires and mashumaro
  decodes accordingly. The dispatcher strips `USE_REMOTE_DEFAULT`
  entries before the call, so Python's normal call machinery
  applies the impl's own defaults for omitted args.

`USE_REMOTE_DEFAULT` (in `service_cap_common`) is a singleton typed
as `Any` (so users can write `x: int = USE_REMOTE_DEFAULT` without
pyright complaining). Use as a Protocol-side default to mean *"don't
ship this; let the impl decide"*.

The sentinel only makes sense at top-level named-parameter positions.
Placing it inside `*args` content / `**kwargs` content / nested in a
collection produces a wire value that fails the
`FxtrServiceData`-vocabulary check in
`invoke_service_method` — that layer's
`NotFxtrServiceDataError` (a `TypeError`) is the natural rejection,
no extra typed-service validation needed.

Layouts that `inspect.BoundArguments` can't route cleanly (e.g.
`USE_REMOTE_DEFAULT` for a positional-only param followed by a
concrete value in a later positional-only) surface as a
`MalformedArgs` `IRException` at the impl side's `bind` step — Python
itself raises `"got some positional-only arguments passed as keyword
arguments"` and the typed-service dispatch wraps it.

Forward-compat is missing-fields-only: an impl can grow new
parameters with defaults and old proxies keep working (their
shorter wire packets satisfy the impl wrapper because new fields
default to `USE_REMOTE_DEFAULT`). The reverse direction is
deliberately a hard error: the impl-side wrapper sets
`Config.forbid_extra_keys = True`, so a proxy that ships a field
the impl doesn't know about gets a clean `ExtraKeysError` rather
than silently-dropped data.

## The wire shape

Two packets, both `IRTagged`-wrapped:

- **Input** — `IRTagged("fxtr_call", {"args", "kwargs", "fxtr_arena", "leases"})`.
  - `args`: lowered positional args (`FxtrServiceData` tree). Each
    `ServiceRef` leaf encodes as
    `IRTagged("service_ref", {"handle": IRHandle, "type_id": <data-id bytes>})`.
    `type_id` is bare `bytes` — no `IRTagged` envelope, no provenance,
    no fxtr_arena coverage. It's wire-portable identity, not
    arena-tracked content; consumers that need the loadable form
    dispatch ``$fxtr.type``.
  - `kwargs`: lowered keyword args.
  - `fxtr_arena`: optional self-describing Mapping
    `{"handle": IRHandle, "tracked_ids": [<tagged FxtrID>, ...],
    "initial_precomp": [<precomp entries>]}` wrapping a fork of the
    caller's main arena, covering every `FxtrID` / `EntityRef`
    referenced in `args` / `kwargs` (NOT including ServiceRef
    `service_type_id`s — those are wire-portable identity and ride
    in the `service_ref` payload directly). The precomp closure
    travels with the handle so prov-bearing FxtrIDs (those carrying a
    `_provenance_parent`) can be sync-decoded by the receiver
    without a `load_flat` round-trip. `None` if no IDs.
  - `leases`: flat `list[Lease]` covering every distinct subject
    token referenced in the packet (handle tokens from `args` /
    `kwargs` plus `fxtr_arena.handle.token` if present), de-duplicated.
- **Return** — `IRTagged("fxtr_return", {"result", "fxtr_arena", "new_indep_leases", "new_dep_leases"})`.
  - `result`: lowered return value.
  - `fxtr_arena`: optional self-describing Mapping (same shape as
    the input slot) for any new `FxtrID`s in `result` that the
    caller doesn't already have. Arg-side IDs that the receiver
    echoes back are not re-shipped — the caller already has them in
    its home scope.
  - `new_indep_leases`: list of fresh `Lease`s on tokens whose
    bridges in the dispatch scope are Independent (the lease is
    self-sustaining and rides directly on the wire).
  - `new_dep_leases`: list of `{token, key_lease, converter,
    input_tokens}` dicts for tokens whose bridges in the dispatch
    scope are Dependent — a chain converter handle, plus the
    materials the caller needs to invoke it. The caller-side
    `_install_return_handles` builds a `DependentPipelinedBridge`
    per entry.

`FxtrServiceData` is a recursive type alias covering primitives
(`None`, `bool`, `int`, `float`, `str`, `bytes`), nested mappings (str
keys) / sequences, and the three reference types (`FxtrID`,
`EntityRef[Any]`, `ServiceRef[Any]`). `EntityRef`'s and `ServiceRef`'s
target-type annotations are dropped on encode (they're metadata for
the typed-service layer; the wire is untyped). FxtrIDs encode as
`IRTagged("fxtr_id", bytes)` for bare IDs or
`IRTagged("fxtr_id_with_prov", prov_parent_bytes)` for layered ones
(the data half is implicit, recovered on the receiver via
`FxtrScope.embed_as_provenance_parent`). `ServiceRef`'s
`service_type_id` ships as raw data-id `bytes` inside the
`service_ref` payload's `type_id` field — no envelope, no provenance,
no fxtr_arena coverage. The data half is what travels; equality on
`FxtrID` ignores `_provenance_parent`, so a layered impl-side id
round-trips equal to its bare wire form. Consequence: a service call
whose only ID-bearing args are ServiceRefs (no other FxtrID /
EntityRef leaves) doesn't carry an `fxtr_arena` at all.

## Server-side dispatch flow

`CommonServiceImplHandle._run_request_async` validates the packet
shape and spawns `_dispatch_body` via the handle's **own dispatch task
group** (`dispatch_tg`, nested inside the framework lifespan — *not* the
mount scope's task group, which hosts only lifespan tasks). The body:

1. Drains `packet["leases"]` into the arg-token lease map via
   `_take_input_leases`.
2. Clones the dispatch's outer lease (`Lease[mount.main]`) twice: one clone is
   the per-call child scope's **parent** lease (so the child's main token is
   *derived from* the mount scope's — breaks the resolve-into-mount-scope cycle
   and makes the mount-scope drain wait for in-flight calls); the other
   re-authorizes the mount scope for the call (so `_fxtr_get_mount_scope()`
   returns an authorized scope) and is discarded when the call ends.
3. Builds the per-call child scope via
   `call_scope.open_with_explicit_leases(parent_leases={mount_scope: …},
   extra_derived_leases=<arg-ServiceRef leases>)` — arg tokens that collide with
   the mount token are tolerated/discarded by entry (chunk-2.5 duplicate
   tolerance); the old explicit dedup is gone.
4. Around `_run_user_method`: re-authorizes the mount scope
   (`with mount_scope.authorize(reauth_clone)`) and binds
   `_CURRENT_DISPATCH_HANDLE` to the handle. `_run_user_method` sets the
   **call scope active** (not the mount scope — §5.3 asymmetry), **bridges the
   incoming `fxtr_arena` into the call scope first** (case-3 via
   `derived.subtake`, with the wire-supplied `initial_precomp` pre-populating the
   sync provenance caches), decodes args (using
   `call_scope.embed_as_provenance_parent` to resolve any prov-bearing FxtrIDs),
   bridges in ServiceRef args (`bridge=None` against the ancestor entry), invokes
   `impl._handle_invoke`, encodes the result (recovering the call scope's key via
   `call_scope._current_key_lease()` for dependent-return entries), and builds
   the return packet.
5. On error, wraps as `IRException` (via `wrap_exception` from
   `fxtr2.leases.leased_cap_common` for plain Python exceptions, so
   the original traceback rides into `IRException.traceback_info`
   and survives the wire hop; user-thrown `IRException`s pass
   through unchanged) and ships an `("err", exc)` tuple over the
   result stream.

The dispatch task stays alive past the request's resolution so any
returned ServiceRefs the caller installs remain dispatchable through
the chain converters. On mount-scope `aclose`, the token-drain step waits for
in-flight calls (they hold `Lease[mount.main]`); only then does the closing
event fire, `dispatch_tg` close, and the user `_fxtr_mount_lifespan` teardown
run (post-drain, unauthorized).

## Caller-side install flow

`invoke_service_method`:

1. Validates and encodes args/kwargs.
2. Forks an input arena (if needed) via `ctx.fork_arena_handle_for_ids`
   (which lifts the IDs into the home scope, forks a sub-arena, and
   computes a precomp closure over the tracked IDs); assembles the
   self-describing `fxtr_arena` packet via `_build_fxtr_arena_packet`
   and mints a lease on the arena's token.
3. For each ServiceRef arg, mints a lease via the home scope's
   bridge for that handle.
4. Builds the call packet, dispatches, awaits the result.
5. **Bridges the return `fxtr_arena` into the home scope first** (via
   `_install_return_arena`, picking the right bridge from the
   wire-supplied `new_indep_leases` / `new_dep_leases` or an existing
   home-scope entry), then decodes `result` (using
   `home_scope.embed_as_provenance_parent` to resolve any prov-bearing
   FxtrIDs), then installs handle bridges via
   `_install_return_handles`:
   - Tokens already known to the home scope (e.g. the caller's own
     home_main, input ServiceRef tokens): no install — the wire
     omitted those.
   - Tokens in `new_indep_leases`: install a fresh
     `IndependentLocalBridge`.
   - Tokens in `new_dep_leases`: build a chain
     `DependentPipelinedBridge` via `_build_chain_bridge`. Inputs
     come from the caller's own bridges (snapshotted into a merged
     dict) plus a `base = Lease[home_main]` at mint time. Each call
     dispatches the wire converter and pipelines the result.

## Cross-tree bridging (`bridge_service_from_scope`)

Standalone primitive on `FxtrScope` (lives in
`fxtr2/data/entity_ref_scope.py` alongside the other scope methods,
not in this package):

```python
await dest.bridge_service_from_scope(ref, source_scope, keepalive_via=…)
# or, defaulting into_scope to the active scope:
await ref.bridge_from_scope(source_scope, keepalive_via=…)
```

`ref.service_type_id` is **not** copied into `dest` (it's wire-portable
identity); consumers wanting the loadable form dispatch `$fxtr.type`. Strict on
duplicate. **`keepalive_via` is required** and declares how `source` is kept
alive relative to `dest`. The contract is about **block nesting** (async-task
`async with` nesting), not the scope-parent relationship — `source` must never be
forced to finish closing before `dest`:

- `"independent_lease"` — `dest` holds a standing `Lease[ref.handle.token]`
  (wrapped `IndependentLocalBridge` / `IndependentPipelinedBridge`, minted via
  `source_scope`'s bridge graph) until `dest` drains, so `source` closes *after*
  `dest`; `source` must not be block-nested inside `dest`.
- `"concurrent_child_bridge"` — `dest` and `source` close *together* (closing
  `dest` cascades `source` closed). Requires `dest` a direct scope-parent of
  `source`, `ref` mounted on `source` itself, and `source` with no extra
  non-parent derived inputs (all checked); the two must drain in **concurrent
  tasks**. The RPC service-return shape. Block nesting is unverifiable — a caller
  contract.

See `FxtrScope.bridge_service_from_scope`'s docstring for the full contract +
allowed/disallowed shapes, and `_build_independent_lease_bridge` /
`_build_concurrent_child_bridge` for the mechanics. The user-facing guide to
mounting, lifespans, and moving services between scopes is
`notes/scope_service_lifetimes.md`.

## cap_ir_arena.py — bridging a `ReadableArena` across Cap-IR

Lets a `ReadableArena` cross a Cap-IR dispatch boundary so fxtr entity
data flows through `IRHandle` round-trips the same way live capabilities
do. Three main classes plus the wire-encoding helpers.

- **`ArenaIRHandleAdapter[B]`** — server-side wrapper. Subclasses
  `IRHandleLocalImplBase[B]`; its token aliases the wrapped arena's
  token. Two request methods (no `GetItem`, no wire-fork):
  - `Call("load_flat", <tagged FxtrID>)` — runs `arena.load_flat`,
    plus a closed precomp-closure walk over the loaded node + every
    dep. Returns `{"entity_id": <tagged>, "entity_bytes": bytes |
    None, "deps": [...], "precomp": [...]}`. Also calls
    `register_emission` to bump `_total_introductions` for every id
    mentioned in the reply.
  - `Call("abandon", [{"id": <tagged>, "count": int}, ...])` —
    decrements `_total_introductions[id]` by `count` for each entry
    and abandons on the wrapped arena when the counter reaches zero.
    The per-id `count` is the introduction-refcount the *client*
    observed, protecting against an abandon-vs-reintroduction race
    (cap-IR `DiscardMessage`-style).

  Public hook `register_emission(ids)` is called by the wire-packet
  builders before shipping any payload that mentions arena-side IDs:
  the initial `fxtr_arena` packet (in
  `service_cap_common._emit_call_result_packet` /
  `entity_ref_scope.fork_arena_handle_for_ids`) and every `load_flat`
  reply (internally). Each unique id contributes +1; this is what
  Layer 1 of `part5bugfix1_plan.md` calls "the server-side promise
  to keep tracking until the client abandons".

  No wire `copy_to_new_arena` method: forking is a purely local op on
  the client (see `CapIRArena.copy_to_new_arena` below). When all
  client-side `CapIRArena` views drop their references, the IRHandle
  dies and the wrapped `MemoryArena` is GC'd via the store's existing
  finalize.

- **`CapIRArenaRemoteState[B_Z]`** — internal shared state for one
  logical remote arena, used by N sibling `CapIRArena` views. Owns:
  - The lifetime/lease conversion machinery (`_z_token`, `_derived`,
    `_x_token`, `_bridge_state`).
  - The wire `IRHandle`.
  - The refcounted liveset (`_refcounts: dict[FxtrID, int]`) — sum of
    (sibling arenas tracking this ID) + (precomp entries citing this
    ID as a `unwraps_to` / `ctx_node` / `step_back_p_ctx` value).
    `_drop_to_zero(nid)` cascades through the precomp entries keyed
    by `nid`, decrementing those values' refcounts and recursing if
    any hit zero — see Layer 2 of `part5bugfix1_plan.md`.
  - The three precomp caches (`_precomp_unwraps_to` /
    `_precomp_ctx_node` / `_precomp_step_back_p_ctx`) serving sync
    provenance reads.
  - The introduction refcount mirror
    (`_remote_refcounts: dict[FxtrID, int]`) — tracks how many
    distinct wire emissions have introduced each id from the server.
    Bumped per id at construction (covering `initial_tracked` ∪
    `initial_precomp` keys/values) and at every `load_flat` reply
    (covering the loaded id, deps, and precomp keys/values). Shipped
    on wire abandon as the per-id `count` field, then reset to zero;
    the server decrements its `_total_introductions[id]` by exactly
    that amount.

  Responsibilities: `make_y_lease` runs the Z→Y conversion
  (mode-dispatching on direct / derived / with_converter); `request_remote`
  combines that with `_remote.dispatch` + await; `acquire(ids)` /
  `release(z_lease, ids)` move per-ID refcounts, with `release`
  dispatching a single batched wire `abandon` for any IDs that hit
  zero. When the refcount map empties, bridge at-rest leases are
  discarded automatically (`_close_at_rest`).

- **`CapIRArena[B_Z]`** — client-side `ReadableArena[B_Z]`, a thin
  share of one `CapIRArenaRemoteState`. Holds only `_remote_state +
  _liveset + _closed`; public methods validate the lease + that the ID
  is in the local liveset, then delegate to the shared state. Three
  classmethod factories (each constructs a fresh remote state internally):
  - **`direct[B](*, token, remote, initial_tracked, initial_precomp=())`** —
    mode 1: Z = X = Y = B; Z-lease passes straight through.
  - **`derived[OutBrand, XBrand](*, derived, x_token, remote, initial_tracked, initial_precomp=())`** —
    mode 2: derived passthrough, Y = X.
  - **`with_converter[OutBrand, BaseBrand, YBrand](*, derived, base_token, remote, bridge, initial_tracked, initial_precomp=())`** —
    mode 3: bridge-driven Z→Y conversion. Takes ownership of the
    `bridge`'s at-rest leases.

  `initial_precomp` is an optional precomp closure (same shape as
  `compute_precomp_closure`'s output); supplying it pre-populates the
  sync provenance caches so prov-bearing FxtrIDs (ones carrying a
  `_provenance_parent`) can be sync-decoded without a `load_flat`
  round-trip. The service-cap path uses this to ship a self-describing
  arena packet (handle + tracked_ids + initial_precomp).

  `copy_to_new_arena(lease, node_ids)` is a **purely local** op:
  returns a sibling `CapIRArena` referencing the same `_remote_state`,
  with refcounts bumped for the requested IDs. No wire dispatch; no
  `IRFuture`-wrapped remote. Forks-of-forks just compose by sharing
  the same state.

  `abandon(lease, node_id)` removes from the local liveset and
  decrements the shared refcount; only when the refcount hits zero
  does a wire abandon get dispatched (batched). `close()` releases
  this arena's share of refcounts without dispatching a wire abandon
  — the IRHandle is held alive by the shared state until the last
  sibling closes, at which point the wrapped server-side `MemoryArena`
  is reclaimed via Python GC.

  Provenance reads (`unwrap_provenance` / `c_node_to_context_data` /
  `p_node_to_pathroot` / `step_back_p_ctx`) are served from the shared
  remote state's precomp caches — populated by every `load_flat` reply,
  plus any `initial_precomp` shipped at construction time.

  `load(...)` is implemented for d/n nodes (CBOR-decode the bytes);
  raises `NotImplementedError` for derived nodes (use `load_flat` +
  bridge via `lazily_attach_from_arena`).

- Wire helpers:
  - `_encode_fxtr_id` / `_decode_fxtr_id` — tagged-bytes form for
    **bare** FxtrIDs (asserts no `_provenance_parent`). Used by every
    arena-internal path (precomp, load_flat, abandon).
  - `encode_fxtr_id_with_prov` / `decode_fxtr_id_with_prov` —
    service-path encoding that handles **layered** FxtrIDs. The
    encoder dispatches on `_provenance_parent` and emits either
    `IRTagged(FXTR_ID_TAG, bytes)` (bare) or `IRTagged(FXTR_ID_WITH_PROV_TAG,
    prov_parent_bytes)`. The decoder takes an `embed_prov` callable
    (typically `FxtrScope.embed_as_provenance_parent`) which validates
    the prov ID is tracked in the receiving scope and returns the
    layered `FxtrID(data_id, _provenance_parent=prov_id)`.
  - `compute_and_track_precomp_closure(arena, lease, seed_ids)` —
    BFS-walks every visited id in `arena`'s liveset (the underlying
    `MemoryArena`/`SQLiteNamespaceArena` sync provenance reads
    auto-track via `_track_from_store_or_none` /
    `_track_from_precomputed`), returns the IRValue entry list. **The
    walk must run on the arena that the wire packet's
    `ArenaIRHandleAdapter` will wrap**, not on the source — that's
    what makes invariant (c) hold (server tracks ⊇ client tracks).
    See Layer 1 of `part5bugfix1_plan.md`.
  - `collect_precomp_ids(precomp)` — extract the union of every key
    + every value cited by a precomp entry list. Used by both
    sides for introduction-refcount registration.
  - `_decode_flat_data` (reply parser), `ingest_precomp` (cache
    populator; called from `CapIRArenaRemoteState._ingest_precomp`,
    which adds refcount-bookkeeping on top).

## Where to look next

- For scope-side bookkeeping (mount / attach / resolve / cap-bridge
  storage / bridge_service_from_scope):
  `fxtr2/data/entity_ref_scope.py`.
- For the lease taxonomy underlying the cap-bridges:
  `fxtr2/leases/lease_bridges.py`.
- For the lower-level Cap-IR protocol the wire shape rides on:
  `fxtr2/leases/leased_cap_ir.py` and `fxtr2/leases/leased_cap_common.py`.
- For the chain-converter design notes:
  `plans/phase3/stage8c_user_caps/lifetime_conversion_mechanics.md`
  and `part6_plan.md`.
