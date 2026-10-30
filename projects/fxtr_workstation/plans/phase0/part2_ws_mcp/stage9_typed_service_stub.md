# Stage 9 — Typed service stub

Goal: light up the typed half of the agent-facing service surface
that was sketched in `sketch.md` but punted on through stage 8.
After this stage, scripts can pass a `@service_protocol`-decorated
Protocol to `get_service` / `lookup_service_by_name` and get a
`ServiceRef[T]` whose method calls are typechecked, whose args
are auto-converted on the way out, and whose results can be
auto-converted back via `retrieve_result`.

The sandbox currently exposes `ServiceRefStub` (sandbox-only,
untyped) and `CallbackRefStub` (sandbox-only, two flavors). Both
get replaced with the real `fxtr2` `ServiceRef[T]` class, backed by
sandbox-local `IRHandle` stubs. The dialect-driven typed encoding
in `fxtr2.services.typed_service` is reused without modification —
we only consume its `BasicEncoder` / `BasicDecoder` codec cache and
the per-method `_MethodSpec` records exposed via the protocol's
`_fxtr_service_protocol_metadata` attribute.

See `stage9_typed_service_stub_scratch.md` for the working notes
and the why-it-looks-like-this background.

## Agent-visible surface changes

```
def get_service[T](
    short_id: str,
    service_type: ServiceTypeAnnotation[T] = UnknownService,
) -> ServiceRef[T]

async def lookup_service_by_name[T](
    name: str,
    service_type: ServiceTypeAnnotation[T] = UnknownService,
) -> ServiceRef[T]

async def schedule_invoke(
    slot_key: str,
    service: ServiceRef[T],
    action: Callable[[T], Any],
) -> None

async def retrieve_result[T](
    slot_key: str,
    result_type: ServiceTypeAnnotation[T] | _ResultTypeSentinel | None = None,
) -> T | Any

async def retrieve_callback_notification[T](
    slot_key: str,
    notif_index: int,
    result_type: ServiceTypeAnnotation[T] | None = None,
) -> T | Any

def create_callback(slot_key: str) -> ServiceRef[EventListenerProto]

def result_type_of[_T_proto, _R, **_P](
    target_type: type[_T_proto],
    method: Callable[Concatenate[_T_proto, _P], Awaitable[_R]],
) -> _ResultTypeSentinel[_R]
# usage: result_type_of(MyProto, MyProto.echo)
# or:    result_type_of(MyProto[int], MyProto.echo)
# Pyright infers _R from the method's return annotation;
# Concatenate[_T_proto, _P] binding lets generic Protocols
# substitute through.

def export_service(slot_key: str, service: ServiceRef[Any]) -> None
def get_id_for_service(service: ServiceRef[Any]) -> str
```

Things that are removed entirely:

- `ServiceRefStub` (replaced by `ServiceRef`).
- `CallbackRefStub` (replaced by `ServiceRef[EventListenerProto]`
  with an owned-callback handle stub).

No back-compat aliases — the user has confirmed the rip-and-replace.
The `examples/basic_test_demo/agent_workdir/preamble.py` and
`demo_pkg/scenarios.py` get updated alongside.

## Sandbox-local stub types

Three new module-level definitions in
`sandbox/fxtr_ws_mcp_sandbox_bridge/fxtr_ws_mcp_sandbox_bridge/protocols.py`:

```
WS_MCP_REMOTE_SERVICE_TYPE_ID = system_entities.define_type(
    WS_MCP_NS, "ws_mcp_remote_service")
WS_MCP_OWNED_CALLBACK_TYPE_ID = system_entities.define_type(
    WS_MCP_NS, "ws_mcp_owned_callback")

class _RemoteServiceHandleStub(IRHandle):
    short_id: str
    @property
    def token(self) -> LifetimeToken[StaticBrand]:
        return STATIC_LIFETIME.token
    async def request(...): raise NotImplementedError
    def dispatch(...): raise NotImplementedError

class _OwnedCallbackHandleStub(IRHandle):
    slot_key: str | None
    slot_n: int | None
    # __post_init__ enforces "exactly one of {slot_key, slot_n} set"
    # token / request / dispatch same as _RemoteServiceHandleStub

@service_protocol
class EventListenerProto(Protocol):
    async def notify(self, payload: FxtrServiceData) -> bool: ...
    async def listening(self) -> bool: ...
```

`EventListenerProto` is a sandbox-local duplicate of
`fxtr_workstation.protocols.EventListenerProto`. The wire never
carries the workstation's identity for an owned callback (the
`["callback_handle", k]` / `["callback", N]` shapes don't include
a `service_type_id`), so the class-identity divergence is purely
cosmetic. Tracked as future cleanup: extract into a small shared
package if it ever bites.

Plus two helpers used at construction time:

```
def _make_remote_service_ref[T](
    short_id: str,
    target_type: ServiceTypeAnnotation[T],
) -> ServiceRef[T]

def _make_owned_callback_ref(
    *,
    slot_key: str | None = None,
    slot_n: int | None = None,
) -> ServiceRef[EventListenerProto]
```

Both populate `service_type_id` with the corresponding sandbox
sentinel (never serialized; required by the `ServiceRef` dataclass).

## Per-flow design

### `get_service` / `lookup_service_by_name`

Pure wrappers that build a remote-service-handle stub and return
`ServiceRef[T]` via `_make_remote_service_ref`. Default
`service_type=UnknownService` so existing untyped callsites
continue to compile. Examples / docs / demos pass the type
explicitly.

### `create_callback` / cross-script callback retrieve

```
create_callback(slot_key)
    queues CreateCallbackOp(slot_key) as today
    returns _make_owned_callback_ref(slot_key=slot_key)

retrieve_result for ["callback_open", N]
    returns _make_owned_callback_ref(slot_n=N)
```

The host-side mutation builder and existing wire shapes don't
change — the value crossing the agent/sandbox boundary just looks
like a typed `ServiceRef` instead of a `CallbackRefStub`.

### `schedule_invoke` — typed-arg encoding

After the recording proxy captures `(method_name, raw_args, raw_kwargs)`:

```
if service.target_type is UnknownService:
    # current behavior: walk raw_args directly through _walk_wire
    encoded_args = [await _walk_wire(a) for a in raw_args]
    encoded_kwargs = {…}  # current code rejects kwargs; keep that
else:
    meta = getattr(service.target_type, "_fxtr_service_protocol_metadata", None)
    if meta is None:
        raise NotImplementedError("target_type must be @service_protocol-decorated")
    spec = meta.method_specs.get(method_name)
    if spec is None:
        raise IRException(category="UnknownMethod", …)
    args_cls = spec.args_wrapper_cls[*get_args(service.target_type)]
    encoder = get_basic_encoder(args_cls, meta.dialect)

    bound = spec.signature.bind(None, *raw_args, **raw_kwargs)   # None for self
    bound.apply_defaults()
    wrapper_kwargs = {k: v for k, v in bound.arguments.items() if k != "self"}
    instance = args_cls(**wrapper_kwargs)
    encoded = encoder.encode(instance)
    encoded = {k: v for k, v in encoded.items() if not isinstance(v, _UseRemoteDefault)}
    new_bound = BoundArguments(spec.signature_no_self, encoded)

    encoded_args   = [await _walk_wire(a) for a in new_bound.args]
    encoded_kwargs = {k: await _walk_wire(v) for k, v in new_bound.kwargs.items()}
```

`_walk_wire` accepts the union of (untyped-path raw types) ∪
(dialect-output vocabulary):

- `FxtrID` → `_maybe_register_entity` + `["entity_handle", handle]`.
- `EntityRef` → same (untyped path only; dialect strips this away).
- `ServiceRef` → dispatch on `handle`:
  - `_RemoteServiceHandleStub` → `["service", handle.short_id]`.
  - `_OwnedCallbackHandleStub(slot_key)` → `["callback_handle", slot_key]`.
  - `_OwnedCallbackHandleStub(slot_n)` → `["callback", slot_n]`.
  - else: programmer error (unknown handle subtype).
- `bytes` / collections / scalars: as today.

Kwargs were previously rejected with "not yet supported". Both
the typed *and* untyped paths now accept kwargs and walk them
through `_walk_wire` like positional args. This requires plumbing
a `kwargs: dict[str, FxtrServiceData]` field through
`ScheduleInvokeOp`, the host-side mutation builder, and the wire
`InvokeOp` shape so the workstation's invoke worker sees them.
Workstation-side typed dispatch already handles `**kwargs`
natively — verify before plumbing.

### `retrieve_result` — uniform two-stage decode

The flow splits into a tag-keyed stage 1 (resolve slot value to
FxtrServiceData) and a tag-agnostic stage 2 (optional type
conversion). Stage 1 raises terminal errors for the
`invoke_pending` / `invoke_error` cases; stage 2 applies
`result_type` (with a slot-tag-specific default when the user
didn't supply one).

```
val = await host.read_state_slot(slot_key)
if val is None: raise KeyError(slot_key)

# Stage 1: produce a canonical FxtrServiceData value, or raise
# terminal errors for non-value tags.
#   ["invoke_pending", n]    → raise LookupError(slot_key)
#   ["invoke_error", msg]    → raise RuntimeError(...)
#   ["exported_entity", sid] → FxtrID (via host.lookup_entity)
#   ["exported_service", sid]→ ServiceRef[UnknownService] (_RemoteServiceHandleStub)
#   ["callback_open", N]     → ServiceRef[UnknownService] (_OwnedCallbackHandleStub(slot_n=N))
#   ["invoke_result", payload]→ await _unescape_payload(payload, lookup_entity)
fsd = await _resolve_slot_to_fxtr_service_data(val)

# Stage 2: if result_type wasn't supplied, fall back to a
# slot-tag-specific default so the agent gets the same shape they
# get today.
#   ["callback_open", _]     → ServiceRef[EventListenerProto]
#   anything else            → None  (return fsd unchanged)
effective_type = result_type if result_type is not None \
                 else _slot_default_result_type(val)

if effective_type is None:
    return fsd
if isinstance(effective_type, _ResultTypeSentinel):
    decoder = get_basic_decoder(
        effective_type.wrapper_cls,
        FxtrServicesDefaultDialect,
        pre_decoder_func=_wrap_result_value,
    )
    return decoder.decode(fsd).value
return decode_from_service_data(fsd, effective_type)
```

`_unescape_payload` outputs canonical FxtrServiceData:

- `["service", short_id]` → bare
  `ServiceRef(_RemoteServiceHandleStub(short_id), UnknownService,
  WS_MCP_REMOTE_SERVICE_TYPE_ID)`.
- `["callback", N]` → bare ServiceRef shape with
  `_OwnedCallbackHandleStub(slot_n=N)` and
  `target_type=UnknownService` (the dialect strategy requires
  UnknownService input on decode; the user re-types it via
  `result_type=ServiceRef[EventListenerProto]` etc., or relies
  on the `callback_open` slot-tag default applying at the top
  level).
- All other branches as today.

`retrieve_callback_notification` mirrors retrieve_result without
the per-tag dispatch: payload is always `invoke_result`-shaped, so
stage 1 is just `_unescape_payload(payload, lookup_entity)` and
stage 2 applies `result_type` (no slot-default).

Uniform handling means `retrieve_result(slot, EntityRef[Note])`
on an `exported_entity` slot wraps the FxtrID via the dialect's
EntityRef strategy; `retrieve_result(slot, ServiceRef[Foo])` on
an `exported_service` slot re-attaches `Foo`; etc. The default
shape (no `result_type`) is what it was prior to this stage —
new behavior is purely additive.

### `result_type_of`

```
_T_RESULT = TypeVar("_T_RESULT")

@dataclasses.dataclass(frozen=True)
class _ResultTypeSentinel(Generic[_T_RESULT]):
    wrapper_cls: Any  # the (possibly parameterized) return wrapper
    # _T_RESULT is phantom — exists only so pyright can flow the
    # return type from method through to retrieve_result.

def result_type_of[_T_proto, _R, **_P](
    target_type: type[_T_proto],
    method: Callable[Concatenate[_T_proto, _P], Awaitable[_R]],
) -> _ResultTypeSentinel[_R]:
    parameterization = get_args(target_type) or None
    proto = get_origin(target_type) or target_type
    meta = getattr(proto, "_fxtr_service_protocol_metadata", None)
    if meta is None: raise NotImplementedError(...)
    # Sanity-check that `method` is actually on `proto`.
    method_name = method.__name__
    if getattr(proto, method_name, None) is not method:
        raise ValueError("method doesn't belong to proto")
    spec = meta.method_specs.get(method_name)
    if spec is None: raise IRException(category="UnknownMethod", ...)
    wrapper_cls = (spec.return_wrapper_cls[*parameterization]
                   if parameterization else spec.return_wrapper_cls)
    return _ResultTypeSentinel(wrapper_cls=wrapper_cls)
```

Two-arg shape so that:

- **Pyright** infers `_R` from `method`'s return annotation via
  standard generic inference. The `Concatenate[_T_proto, _P]`
  binding pins `_T_proto` to the method's `self`, which lets pyright
  substitute through generic Protocols automatically — passing
  `MyProto[int]` plus `MyProto.echo` (whose return is `T_inner`)
  unifies `T_inner = int` and yields `_R = int`. The phantom
  `_R` flows through `_ResultTypeSentinel[_R]` out to
  `retrieve_result[T](slot, sentinel: _ResultTypeSentinel[T]) -> T`.

- **Runtime** uses `target_type` to find the right parameterized
  return wrapper, then uses `method.__name__` to key into the
  metadata's method specs. `getattr(proto, method.__name__) is
  method` is the sanity check that catches accidental cross-Protocol
  method passing.

Returns the parameterized return-wrapper-dataclass form in a thin
opaque generic holder. `retrieve_result` recognizes the holder,
builds a decoder, plucks `.value` out. The holder lets us avoid
trying to `setattr` markers onto `GenericAlias` instances (which
doesn't work).

`_ResultTypeSentinel` is exposed alongside the other builtins so
user code can build it directly if it ever wants to (e.g. when the
return type isn't on a Protocol). For v1 the only documented way
to build one is `result_type_of`.

## Files touched

```
sandbox/fxtr_ws_mcp_sandbox_bridge/fxtr_ws_mcp_sandbox_bridge/
    protocols.py    add stub IRHandles, sentinel type_ids,
                    EventListenerProto, _make_* helpers,
                    _ResultTypeSentinel; delete ServiceRefStub
                    and CallbackRefStub
    builtins.py     replace ServiceRefStub/CallbackRefStub usage;
                    add typed branch in schedule_invoke; add
                    typed branch in retrieve_result and
                    retrieve_callback_notification; add
                    result_type_of; update _walk into _walk_wire;
                    update _unescape_payload + _assemble

fxtr_workstation_mcp/fxtr_workstation_mcp/
    mutation_builder.py  add kwargs field to InvokeOp emission so
                        kwargs reach the workstation invoke worker
fxtr_workstation_mcp/fxtr_workstation_mcp/server.py
                    update the doc string that lists ServiceRefStub
fxtr_workstation_mcp/tests/
                    new test file (e.g. test_typed_service_e2e.py)
                    covering the bullet list in scratch.md;
                    update any existing tests that import
                    ServiceRefStub / CallbackRefStub

examples/basic_test_demo/run_demo_workstation.py
                    import DelayedEchoProto / CallbackTesterProto /
                    EventListenerProto from demo_pkg instead of
                    declaring locally
examples/basic_test_demo/agent_workdir/packages/demo_pkg/src/demo_pkg/
                    typed_protos.py  new — DelayedEchoProto,
                        CallbackTesterProto, EventListenerProto
                        moved here (importable from both host
                        and sandbox)
                    scenarios.py    add typed variants of existing
                        scenarios, using the moved protos
examples/basic_test_demo/agent_workdir/preamble.py
                    update imports (result_type_of); typed_protos
                    imports happen lazily inside scenarios

projects/fxtr_workstation_agent_plugin/CODE_OVERVIEW.md
                    update the "Handle namespace" section + add a
                    short "Typed services" subsection under
                    "Sandbox interaction"
```

CODE_OVERVIEW for the workstation server itself (`projects/fxtr_workstation/CODE_OVERVIEW.md`) doesn't need changes — this is a sandbox-bridge feature only.

## Implementation steps

- [x] **Stub IRHandle subclasses + sentinels** in
      `sandbox_bridge/protocols.py`. Define
      `_RemoteServiceHandleStub`, `_OwnedCallbackHandleStub`,
      `WS_MCP_REMOTE_SERVICE_TYPE_ID`,
      `WS_MCP_OWNED_CALLBACK_TYPE_ID`. Both handles use
      `STATIC_LIFETIME.token`; `request` / `dispatch` raise
      `NotImplementedError`. Add a unit test that constructs each
      and confirms `ServiceRef(handle=stub, target_type=…, service_type_id=…)`
      builds cleanly and is hashable.
- [x] **EventListenerProto** sandbox-local declaration in the same
      file. Add a comment explaining the duplication.
- [x] **`_make_remote_service_ref` / `_make_owned_callback_ref`**
      helpers in `protocols.py`.
- [x] **Delete `ServiceRefStub` and `CallbackRefStub`** from
      `protocols.py`. Update `QueuedOp` doc references.
- [x] **Update `builtins.py` to use the new types**: imports,
      `get_service` / `lookup_service_by_name` signatures with the
      `service_type` parameter, `export_service` /
      `get_id_for_service` accepting `ServiceRef`,
      `create_callback` returning an owned-callback `ServiceRef`,
      `retrieve_result` returning owned-callback / remote `ServiceRef`
      in the appropriate branches. Rename the `_walk` helper to
      `_walk_wire`; update its `ServiceRef` / `ServiceRefStub` /
      `CallbackRefStub` branches per the scratch doc. Update
      `_unescape_payload` / `_assemble` similarly. Update Channel
      Notifications scope: `_collect_entity_ids` already handles
      the right tag set, no changes there.
- [x] **Update `CODE_OVERVIEW.md`** to reflect the new
      sandbox-side types (Handle namespace + Sandbox interaction
      sections).
- [x] **`result_type_of` + `_ResultTypeSentinel`** in
      `builtins.py`. Add to the builtins mapping returned from
      `build_builtins`. Document in the module docstring.
- [x] **Plumb kwargs through the host-side wire op.** Add a
      `kwargs: dict[str, FxtrServiceData]` field to
      `ScheduleInvokeOp` and pass it through `mutation_builder` so
      the workstation-side `InvokeOp` carries kwargs. Verify the
      workstation invoke worker already accepts kwargs on the wire
      (the typed-service impl side uses `inspect_sig.bind(*args, **kwargs)`,
      so this should be a no-op on the server but worth confirming).
- [x] **Typed `schedule_invoke` branch** in `builtins.py`. Reuses
      `get_basic_encoder` from `fxtr2.services.service_dialect`
      and the protocol's `_fxtr_service_protocol_metadata`. Errors:
      `NotImplementedError` for non-`@service_protocol` target_type;
      `IRException(category="UnknownMethod")` for missing method
      names. **Both** typed and untyped paths now accept kwargs;
      the untyped path's `if raw_kwargs: raise ValueError(...)`
      rejection is removed.
- [x] **Uniform two-stage `retrieve_result`** in `builtins.py`.
      Stage 1 `_resolve_slot_to_fxtr_service_data(val)` returns
      canonical FxtrServiceData (FxtrID / bare ServiceRef /
      unescaped tree) or raises terminal errors. Stage 2 applies
      `result_type` (with a slot-tag default that re-types
      `callback_open` to `ServiceRef[EventListenerProto]`).
      Handles `_ResultTypeSentinel` via the wrapper +
      `_wrap_result_value` decoder pre-func; handles raw
      annotation via `decode_from_service_data`.
- [x] **Typed `retrieve_callback_notification`** in `builtins.py`.
      Same as `retrieve_result` stage 2 (no per-tag dispatch
      needed — payload is always invoke_result-shaped).
- [x] **Update `CODE_OVERVIEW.md`** with a "Typed services"
      subsection summarizing the encode/decode flow.
- [x] **Move demo protos into demo_pkg.** Create
      `demo_pkg/typed_protos.py` with `DelayedEchoProto`,
      `CallbackTesterProto`, `EventListenerProto`. Update
      `run_demo_workstation.py` to import from there. Confirms the
      protos are reachable from both host and sandbox.
- [x] **Update `examples/basic_test_demo/agent_workdir`** preamble +
      scenarios to use typed `lookup_service_by_name` /
      `schedule_invoke` / `retrieve_result` against
      `DelayedEchoProto` (and `CallbackTesterProto` for the
      callback scenarios). Existing untyped scenarios stay too so
      we still cover the default-`UnknownService` path.
- [x] **New tests** in
      `fxtr_workstation_mcp/tests/test_typed_service_e2e.py`
      covering each bullet from the scratch doc's "Coverage"
      section. Existing tests should continue to pass unchanged
      apart from any `ServiceRefStub` / `CallbackRefStub` imports
      that need updating.
- [x] **Final `CODE_OVERVIEW.md` pass**: confirm Handle namespace
      and Sandbox interaction sections describe the final state;
      confirm the "Outstanding TODOs" list is current.

## Things that intentionally don't change

- The wire shape between the MCP and the workstation. Typed
  encoding is purely sandbox-internal — by the time the host's
  `ScheduleInvokeOp` reaches `mutation_builder`, the args are
  already in the bracketed-list form.
- The `_RecordingProxy` in `builtins.py`. The typed path adds
  encoding *after* the proxy captures the call; the proxy itself
  stays a no-op recorder.
- The `fxtr2.services.typed_service` module. We consume its
  metadata via the existing `_fxtr_service_protocol_metadata`
  attribute and its codec cache; no changes upstream.
- Untyped scripts. `target_type=UnknownService` keeps the existing
  walk path verbatim.
- Channel notification rendering. `<event …>` blocks stay the same.

## Risks / things to revisit

- **Duplicate `EventListenerProto`.** Sandbox-local class identity
  diverges from the host class. Operationally harmless but
  cosmetically ugly; can be collapsed by extracting into a tiny
  shared package.
- **Variadic `*args` typed-path handling.** The typed branch uses
  `spec.signature.bind(...)` which routes `*args` through the
  wrapper field. `inspect.BoundArguments` flattens it back out on
  the wire-walk side. Round-trip looks fine on paper; needs a
  test that explicitly passes a `*args`-using method through it.
- **GenericAlias setattr** — addressed via `_ResultTypeSentinel`
  holder. If we ever need to mark the underlying class directly
  (e.g. for a different use case), revisit.
- **`USE_REMOTE_DEFAULT` in kwargs.** Stripped before the
  BoundArguments split same as the typed proxy does. Test that a
  typed method with optional kwargs round-trips: omit the kwarg,
  confirm the wire doesn't carry it.
