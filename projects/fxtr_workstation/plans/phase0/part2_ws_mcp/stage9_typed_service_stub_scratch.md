# Scratch — stage 9 typed service stub

Working notes for the trickier mechanics. The plan doc itself
sticks to the spec; this is where I park the why-and-how.

## What the layer-2 machinery gives us for free

`fxtr2.services.typed_service` builds, per Protocol method:

- `args_wrapper_cls = OwnerCls__methodname__Args` — a plain
  `@dataclass(frozen=True)` whose fields mirror the user's
  parameter names one-to-one, parameterized by the owning
  class's TypeVars.
- `return_wrapper_cls = OwnerCls__methodname__Result` — a
  single-field (`value: T`) dataclass with the same TypeVars.

Both are exposed on `_MethodSpec` instances stored on
`<proto>._fxtr_service_protocol_metadata.method_specs[name]`.

The `service_dialect.FxtrServicesDefaultDialect` knows how to
encode/decode these wrappers via `get_basic_encoder` /
`get_basic_decoder` (the cached factory in
`service_dialect.py`). The encoder accepts an instance,
returns a dict-of-`FxtrServiceData`-shaped values keyed by user
parameter names; decoder is the inverse. The dialect strips
`EntityRef[T] → FxtrID` and `ServiceRef[T] → ServiceRef[UnknownService]`
on encode, and re-attaches `T` from the field annotation on
decode.

We use exactly this surface from the sandbox; we do **not**
touch `invoke_service_method`, `_encode_fxtr_service_data`,
`ServiceProxyContext`, or any cap-IR plumbing — those are for the
real fxtr2 dispatch flow. Our wire path is the bracketed-list
encoding owned by the MCP, layered around `BasicEncoder`'s output.

## Stub handle subclasses (why two)

Currently `ServiceRefStub` carries just `short_id: str`. Replacing
it with `ServiceRef` requires a sandbox-side `IRHandle`. The
workstation-side wire vocabulary, however, has two distinct
agent-facing forms for "a reference to a callable thing the
session has touched":

- `["service", short_id]` — workstation-known service, dispatched
  via the workstation's service registry by short_id.
- `["callback_handle", slot_key]` / `["callback", slot_n]` — a
  callback this session created. The receive-side service is owned
  by the workstation but the *identity* the workstation looks up
  is the slot, not a short_id.

Both look like `ServiceRef[Something]` from the agent's POV: the
agent passes them as args to `schedule_invoke`, and the wire
walker emits the right bracketed form. But the wire emission
differs, so the handle subtype distinguishes:

```
_RemoteServiceHandleStub(short_id: str)
    → wire walker emits ["service", short_id]

_OwnedCallbackHandleStub(slot_key: str | None, slot_n: int | None)
    → wire walker emits ["callback_handle", slot_key] (same-script)
                     or ["callback", slot_n] (cross-script)
```

Both share:

- `token = STATIC_LIFETIME.token` (the static-lifetime singleton
  from `fxtr2.leases.local_lease`). Per user instruction.
  Operationally never observed since the sandbox never dispatches
  these handles, but a proper LifetimeToken is cleaner than
  raising on access.
- `request` / `dispatch` raise `NotImplementedError`. The sandbox
  never reaches these — every operation either round-trips the
  handle to the wire walker via `schedule_invoke`, or strips it
  out via the dialect, or never touches it (e.g. exporting a
  callback through `export_service` just re-emits the wire shape).
- Both are frozen dataclasses inheriting `IRHandle` so they're
  hashable / cheap to compare.

The decode side is uniform: an inbound `["service", short_id]`
always produces `ServiceRef(_RemoteServiceHandleStub, UnknownService,
sentinel_type_id)` regardless of `target_type`. If the typed
return decoder is asked for `ServiceRef[EventListenerProto]`, it
just re-attaches the annotation — same code path as any other
ServiceRef-typed return. That's why "callback I passed out and
got back" works transparently as a regular remote service ref.

## The CallbackRefStub disappearance

CallbackRefStub gets deleted entirely. The two same-/cross-script
shapes become:

- `create_callback(slot_key)` → `ServiceRef(handle=_OwnedCallbackHandleStub(slot_key=slot_key, slot_n=None), target_type=EventListenerProto, service_type_id=sentinel)`.
- `retrieve_result(slot_key)` for `["callback_open", N]` →
  `ServiceRef(handle=_OwnedCallbackHandleStub(slot_key=None, slot_n=N), target_type=EventListenerProto, service_type_id=sentinel)`.

`_assemble`'s `["callback", N]` branch is unreachable in practice
(workstation services don't hand back this wire shape; only
agent-side scripts emit it). The user said treat the case as
"turn back into a ServiceRef the same as during creation" — fine,
keep `_assemble` symmetric with the encoder's vocabulary; it
just emits the `_OwnedCallbackHandleStub(slot_n=N)` ref.

Type-id sentinel: only used to populate `ServiceRef.service_type_id`
since the field is required; never serialized to wire. Define
both type_ids (`WS_MCP_REMOTE_SERVICE_TYPE_ID` /
`WS_MCP_OWNED_CALLBACK_SERVICE_TYPE_ID`, two distinct sentinels
so they sort/print distinctly) in `protocols.py` next to the
existing `MCP_RUNNER_TYPE_ID`, using the existing `WS_MCP_NS`
namespace.

## Sandbox-local EventListenerProto

The host-side `fxtr_workstation.protocols.EventListenerProto` is
in the `fxtr_workstation` package, which isn't in the sandbox's
dep closure (`sandbox_runtime/pyproject.toml` only has
`fxtr-sandbox-rpc-adapter[fxtr2-portable]`). Pulling
`fxtr_workstation` in is heavy and not motivated.

The wire path doesn't need the Protocol's identity to be shared
with the workstation (dialect strips target_type on encode; the
"owned callback" wire form `["callback_handle", k]` doesn't carry
`service_type_id`). What the agent gets out of an
`EventListenerProto`-typed ref is purely Pyright-side type
correctness.

So: declare a sandbox-local `EventListenerProto` in the bridge's
`protocols.py`, matching the host shape (one `notify(payload) -> bool`
+ one `listening() -> bool`). Mark it `@service_protocol` so the
metadata is registered (means `result_type_of` and typed args work
against it from inside scripts). Document the duplication.

Future cleanup: extract `EventListenerProto` into a tiny shared
package (`fxtr_workstation_callback_proto`?) that both host and
sandbox can depend on. Out of scope for stage 9.

## Typed schedule_invoke flow

```
captured = (method_name, raw_args, raw_kwargs)
target_type = service.target_type

if target_type is UnknownService:
    encoded_args = [await _walk(a) for a in raw_args]
    # raw_kwargs guarded as today (rejected as not-yet-supported)
else:
    meta = getattr(target_type, "_fxtr_service_protocol_metadata", None)
    if meta is None:
        raise NotImplementedError(
            "schedule_invoke: typed service target_type must be @service_protocol-decorated"
        )
    spec = meta.method_specs.get(method_name)
    if spec is None:
        raise IRException(category="UnknownMethod", ...)
    parameterization = get_args(target_type) or None
    args_cls = spec.args_wrapper_cls[*parameterization] if parameterization else spec.args_wrapper_cls
    encoder = get_basic_encoder(args_cls, meta.dialect)

    bound = spec.signature.bind(None, *raw_args, **raw_kwargs)  # `None` for self
    bound.apply_defaults()
    wrapper_kwargs = {k: v for k, v in bound.arguments.items() if k != "self"}
    instance = args_cls(**wrapper_kwargs)
    encoded = encoder.encode(instance)
    encoded = {k: v for k, v in encoded.items() if not isinstance(v, _UseRemoteDefault)}
    new_bound = BoundArguments(spec.signature_no_self, encoded)

    # The wire walker accepts the dialect's output vocabulary:
    #   FxtrID, ServiceRef (with stub handle), bytes, list/dict/tuple, scalars.
    # Anything else means the dialect didn't strip it — surface a clean error.
    encoded_args = [await _walk_wire(a) for a in new_bound.args]
    encoded_kwargs = {k: await _walk_wire(v) for k, v in new_bound.kwargs.items()}
```

The wire-walker `_walk_wire` is mostly today's `_walk` with two changes:

1. Recognize `ServiceRef` rather than `ServiceRefStub`. Dispatch on
   the handle subtype: `_OwnedCallbackHandleStub` → `["callback_handle", slot_key]` / `["callback", slot_n]`; `_RemoteServiceHandleStub` → `["service", short_id]`; anything else is a programmer error.
2. `EntityRef` is gone from the dialect output (always stripped to
   FxtrID) — keep a branch that accepts it for the untyped path
   only, or fold EntityRef → FxtrID extraction in both paths
   uniformly (cleaner). Drop the dialect path's CallbackRefStub
   branch (no such class anymore).

The `_walk_wire` for the untyped path still needs to accept raw
user types (`ServiceRef` / `EntityRef` / `FxtrID` / `bytes` /
collections). That's still strictly broader than what the typed
path emits, so a single walker function works for both.

**Variadic `*args` / `**kwargs` keys.** `inspect.BoundArguments`
flattens the variadic into the natural positional/keyword wire
slots — no special handling needed beyond using
`signature_no_self.bind(...)`'s expansion.

**`USE_REMOTE_DEFAULT`** rides through encode as the sentinel,
gets stripped before the BoundArguments split (matching the
typed proxy's behavior). Wire walker never sees it.

## Typed retrieve_result flow

```
val = await host.read_state_slot(slot_key)
... # pre-decode handling: KeyError on absent, LookupError on
    # invoke_pending, RuntimeError on invoke_error, special tags
    # (exported_entity / exported_service / callback_open) handled
    # explicitly as today — these return Python values (FxtrID,
    # remote ServiceRef, owned-callback ServiceRef) not raw payload
    # vocabulary.
# tag == "invoke_result":
unescaped = await _unescape_payload(payload, lookup_entity)
# unescaped is now FxtrServiceData-shaped:
#   FxtrIDs (resolved via lookup_entity), bare ServiceRefs (UnknownService
#   with _RemoteServiceHandleStub), owned-callback ServiceRefs
#   (UnknownService with _OwnedCallbackHandleStub), bytes,
#   nested dicts/lists, scalars.

if result_type is None:
    return unescaped
if is_result_wrapper_marker(result_type):
    decoder = get_basic_decoder(result_type, FxtrServicesDefaultDialect,
                                pre_decoder_func=_wrap_result_value)
    decoded = decoder.decode(unescaped)
    return decoded.value
else:
    return decode_from_service_data(unescaped, result_type)
```

`is_result_wrapper_marker` checks for our class-level marker
attribute (`_fxtr_ws_mcp_result_wrapper = True`). The marker is
set by `result_type_of`, not by `typed_service.py`, so we don't
need to touch the upstream code.

`retrieve_callback_notification` mirrors this: `_unescape_payload`
+ optional decode via plain annotation. The wrapper-class path
is less obviously useful here (a callback payload doesn't have a
"protocol return type" — it's the workstation's `notify(payload)`
arg) but it's harmless to support uniformly.

## result_type_of(cls, method) sentinel

```
def result_type_of(target_type, method_name):
    parameterization = get_args(target_type) or None
    proto = get_origin(target_type) or target_type
    meta = getattr(proto, "_fxtr_service_protocol_metadata", None)
    if meta is None: raise NotImplementedError(...)
    spec = meta.method_specs.get(method_name)
    if spec is None: raise IRException(category="UnknownMethod", ...)
    cls = (spec.return_wrapper_cls[*parameterization]
           if parameterization else spec.return_wrapper_cls)
    setattr(cls, "_fxtr_ws_mcp_result_wrapper", True)
    return cls
```

Generic alias caveat: `cls[*parameterization]` produces a
`types.GenericAlias`, not a class. `setattr` doesn't work on
GenericAlias. Workaround options:

- Keep a parallel sentinel-set keyed by the wrapper-class
  identity, and `is_result_wrapper_marker` checks membership.
- Wrap the alias in a small holder dataclass and have
  `retrieve_result` unwrap it before handing to the codec.

Holder dataclass is the simplest, will use that:

```
@dataclasses.dataclass(frozen=True)
class _ResultTypeSentinel:
    wrapper_cls: Any   # the (possibly parameterized) return wrapper

def result_type_of(target_type, method_name) -> _ResultTypeSentinel:
    ...
    return _ResultTypeSentinel(wrapper_cls=cls)
```

`retrieve_result` checks `isinstance(result_type, _ResultTypeSentinel)`,
extracts `.wrapper_cls`, builds the decoder.

## What about callbacks-as-arg-of-typed-method

The protocol declares `cb: ServiceRef[EventListenerProto]`. The
agent passes an owned-callback ServiceRef. The dialect strips
target_type → `ServiceRef[UnknownService]`. The wire walker sees
a ServiceRef, dispatches on `handle` subtype: it's
`_OwnedCallbackHandleStub`, so it emits `["callback_handle", k]`
or `["callback", N]`. Done. No pre-walk substitution gymnastics,
no custom dialect strategy.

## What about callbacks-as-return-of-typed-method

User said this is "somewhat misguided", workstation services don't
emit `["callback", N]` on the return side. The `_assemble` branch
for `["callback", N]` stays present for completeness (decodes to
the owned-callback ServiceRef shape) but isn't expected on the
hot path. No special handling in the typed decoder.

## Test plan sketch

- New unit test fixture: a typed protocol in `demo_pkg` (mirroring
  the one in `run_demo_workstation.py`) so the agent_workdir
  scripts can `from demo_pkg.typed_protos import DelayedEchoProto`.
  Or just import the protocol from `run_demo_workstation` if
  reachable from the sandbox (likely not — workstation code isn't
  in the sandbox). Probably cleanest: move
  `DelayedEchoProto` / `CallbackTesterProto` definitions into
  `demo_pkg` so both host (run_demo_workstation.py) and sandbox
  (scenarios.py) can import. Confirm with user before doing the
  move.

- Coverage:
  - get_service / lookup_service_by_name with typed annotation
    returns ServiceRef[T] (pyright-asserted).
  - schedule_invoke against typed proto with EntityRef-shaped arg
    runs through BasicEncoder (auto-strips EntityRef → FxtrID).
  - retrieve_result with result_type_of(P, "method") returns the
    correct Python value (e.g. EntityRef[Note]).
  - retrieve_result with raw annotation (`ServiceRef[Foo]`,
    `EntityRef[Note]`) round-trips correctly.
  - Owned callback ref passed as a typed arg: same-script and
    cross-script shapes both emit the right wire form.
  - Untyped path still works (UnknownService default).
  - Error: target_type is a non-`@service_protocol` class →
    NotImplementedError.
  - Error: typed method name not on the protocol → IRException
    with `category="UnknownMethod"`.
