# Substage 8 — Mashumaro-driven typed services

## Goal

Add a typed layer on top of the substage-6/7 service plumbing so that
service methods can declare typed args / returns (including
`EntityRef[T]`, `ServiceRef[T]`, frozensets / frozendicts / tuples /
plain dataclasses, etc.) and have transparent mashumaro-driven
conversion. Both the impl side and the proxy/resolve side participate;
the lower-level FxtrServiceData wire shape is unchanged.

The substage also adds a typed analog of the runtime
`service_type_id` introspection: a Protocol can declare a reserved
`service_type_id` attribute whose annotation determines what the
proxy exposes (e.g. `EntityRef[MyDescriptor]` instead of a bare
`FxtrID`).

## Locked-in design (from discussion before this plan)

- **`TypedServiceImpl[T]` is its own `CommonServiceImpl[T]` subclass**,
  sibling to `SimpleAsyncServiceImpl[T]` rather than a child of it.
  The two share only the `@service_method`-discovery walk, which we
  pull out into a free helper in `service_cap_common.py`.
- **Naming.** `TypedServiceImpl` / `TypedServiceProxy` /
  `@service_protocol`. The "Unchecked" prefix from the original
  overview is dropped because the decorator now supports
  `service_type_id` / `service_type_validator` for cross-side
  agreement (analogous to how `EntityLoadHandler.supports_entity_type`
  gates entity load handlers).
- **Default dialect = `FxtrServicesDefaultDialect`.** Lives in a new
  `fxtr2/services/service_dialect.py`. Knows about the standard
  vocabulary types — `EntityRef`, `ServiceRef`, `FxtrID`, `frozenset`,
  `set`, `frozendict`, `tuple`. `DataclassEntity` subclasses are
  *not* included — they belong inside `EntityRef[T]`, not inline.
- **Strict in / out vocabulary at the dialect boundary.** On encode,
  the dialect always strips `EntityRef[T] → FxtrID` and
  `ServiceRef[T] → ServiceRef[UnknownService]`. On decode, the
  dialect refuses any input other than `FxtrID` /
  `ServiceRef[UnknownService]` for those slots, and re-attaches
  `T` from the annotation. Lower-level encoding stays untyped; the
  conversion layer is the single source of typed/untyped
  translation.
- **Dialect placement.** Per Protocol via the `@service_protocol(dialect=...)`
  decorator. Per impl via a `_service_dialect` class attribute on
  the `TypedServiceImpl` subclass. Both default to
  `FxtrServicesDefaultDialect`. They are independently overridable
  and not validated against each other — same as how a typed impl
  and proxy can have independent annotations.
- **`service_type_id` exposure on the proxy.** The proxy resolver
  inspects the Protocol class for an attribute named
  `service_type_id`:
  - Annotation `EntityRef[T]` → proxy gets a property
    `service_type_id: EntityRef[T]` returning
    `EntityRef(self._ctx.service_type_id, T)`.
  - Annotation `FxtrID` → proxy gets `service_type_id: FxtrID`
    returning `self._ctx.service_type_id`.
  - Absent → proxy exposes `_service_type_id: FxtrID` (underscore
    prefix marks it as a fallback).
  - Any other annotation (or a `service_type_id` defined as a
    method/property body) → registration error.
- **`service_type_id` source on the impl side.** Subclasses still
  override `_service_type_id(self, scope)` (no class-attribute
  shortcut — too easy to forget; explicit is better). Helpers are
  free to layer on top.
- **`service_type_validator` shape.** Mirrors
  `EntityLoadHandler.supports_entity_type`:

  ```
  async def supports_service_type(
      self,
      service_type_id: FxtrID,
      target_service_type: ServiceTypeAnnotation[T],
  ) -> bool: ...
  ```

  The validator is invoked at resolve time, on the home scope
  (so `service_type_id` is already tracked there — the resolver
  can `EntityRef(service_type_id, ...).load()` if the descriptor
  shape needs inspection). `target_service_type` is the
  parameterized form from the registry's `subquery`, so the
  validator can introspect type vars.
- **Eager codec build.** Per-method `BasicEncoder` / `BasicDecoder`
  instances are built once per `(Protocol_or_ImplClass, dialect,
  parameterized_args)` tuple, cached module-level. Annotations
  mashumaro can't handle raise at build time.
- **Method discovery.**
  - Protocol side: walk all `async def` methods on the Protocol.
    Reject sync methods, classmethods, staticmethods, properties,
    user-defined dunders. The reserved `service_type_id` attribute
    is excluded from method walking.
  - Impl side: walk `@service_method`-tagged methods on the
    `TypedServiceImpl` subclass (same convention as the untyped
    layer, via the shared helper).
- **Method-level type parameters disallowed; class-level supported
  on both sides.**
  - Proxy side: the parameterized form comes from the registry's
    `subquery` (e.g. `MyProto[int]`); codecs are built against
    the substituted annotations.
  - Impl side: a `TypedServiceImpl` subclass with class-level
    type vars must override the new
    `_own_full_type_for_serialization(self)` method to return a
    runtime generic alias (e.g. `MyImpl[int]`). Default
    implementation returns `type(self)`, which is fine for
    non-generic impls. Different instances of the same class are
    allowed to return different parameterized forms — the impl
    is responsible for stashing its type args wherever it likes
    (constructor arg, class attr, etc.). The framework just
    asks for the concrete annotation back.
- **Args/kwargs canonicalization.** Both sides use
  `inspect.Signature.bind` to map (positional, keyword) into bound
  arguments, then split by parameter kind into `args` (positional /
  positional-only / *args) and `kwargs` (keyword / keyword-only /
  **kwargs) for the wire. Per-arg encoding/decoding is keyed on the
  bound parameter name.

## Files

```
fxtr2/services/
    service_cap_common.py        — extract _walk_service_methods helper
    untyped_service.py           — call the helper instead of inline walk
    service_dialect.py           — NEW: FxtrServicesDefaultDialect + strategies
    typed_service.py             — NEW: @service_protocol, TypedServiceImpl,
                                   TypedServiceProxy factory + cache
    LOCAL_CODE_OVERVIEW.md       — extend for the typed layer

tests/test_services/
    test_typed_service.py        — NEW: end-to-end typed-service tests
    test_service_dialect.py      — NEW: focused tests on the default dialect
                                   strategies (encode/decode round-trip,
                                   strict-vocabulary refusals)
```

`CODE_OVERVIEW.md` gets a new entry under `fxtr2/services/`.

---

## §1 — `_walk_service_methods` helper

**Where.** `service_cap_common.py`.

**Why.** `SimpleAsyncServiceImpl` and `TypedServiceImpl` share the
`@service_method`-tagged-method-on-MRO discovery convention but
otherwise have nothing in common. Pulling the walk out into a free
helper keeps both classes thin and lets the typed layer add its own
extra validation (reject sync methods etc.) without inheriting.

**Shape.**

```
def walk_service_methods(
    cls_or_instance: type | object,
    *,
    require_async: bool = True,
    forbid_dunders: bool = True,
) -> Mapping[str, Callable[..., Awaitable[FxtrServiceData]]]:
    ...
```

- Walks `cls.__mro__` (or `type(instance).__mro__`), collecting
  `vars(cls)` entries with `_SERVICE_METHOD_MARKER` set.
- Subclass methods take precedence over inherited ones.
- If `cls_or_instance` is an instance, returns bound methods;
  otherwise returns the unbound functions (typed-impl validation
  uses the unbound form, dispatch uses the bound form).
- If `require_async=True`, raises a `ServiceProtocolError` for any
  tagged method that is not `async def`.
- If `forbid_dunders=True`, raises for any tagged dunder name.

`SimpleAsyncServiceImpl._service_exposed_methods` becomes a
two-line wrapper that calls `walk_service_methods(self,
require_async=True, forbid_dunders=True)`.

---

## §2 — Default service dialect (`service_dialect.py`)

`FxtrServicesDefaultDialect` is a `mashumaro.Dialect` subclass whose
`serialization_strategy` covers:

| Type annotation | Encode → | Decode ← |
|---|---|---|
| `FxtrID` | passthrough (it *is* FxtrServiceData) | passthrough |
| `EntityRef[T]` | `value.entity_id: FxtrID` (drops `T`) | `EntityRef(value, T)` (re-attaches `T`) |
| `ServiceRef[T]` | `dataclasses.replace(value, target_type=UnknownService)` | `dataclasses.replace(value, target_type=T)` |
| `frozenset[X]` | `list(value)` | `frozenset(value)` |
| `set[X]` | `list(value)` | `set(value)` |
| `frozendict[K, V]` | `dict(value)` | `frozendict(value)` |
| `tuple[X, Y, ...]` | `list(value)` | `tuple(value)` |

Mashumaro's built-in handling of dataclasses, primitives, lists,
dicts, unions, optionals stays as-is — the dialect only adds
strategies for our extras.

**Strategy implementation.**

- `EntityRef` and `ServiceRef` decoders use
  `mashumaro.types.GenericSerializableType` (same trick as
  `_InlineEntityDeserializationHelper` in `mashumaro_entities.py`)
  so the strategy receives the type arg `T` at decode time.
- The encode side strips type metadata unconditionally.
- The decode side performs strict-vocabulary validation:
  - `EntityRef[T]` decoder: input must be `FxtrID` (or the wire's
    tagged form mashumaro hands us). Refuse `EntityRef`
    instances; `_NotFxtrServiceDataError`-style exception with a
    clear message — the lower layer should never produce that
    shape.
  - `ServiceRef[T]` decoder: input must be
    `ServiceRef[UnknownService]`. Refuse anything else.
- Set / frozenset / frozendict / tuple strategies validate
  homogeneity etc. only as needed for round-tripping (we don't
  need `_validate_homogeneous_set` here — that's a CBOR
  canonicalization concern, not a service-call concern).

**Why a single dialect class instead of a registry.** Service
calls are point-to-point; there's no canonicalization need.
Mashumaro `Dialect`s compose via `Dialect.merge`, so users who
want to extend the defaults can subclass and add strategies.

**Tests.** `test_service_dialect.py` covers each strategy
independently: encode-then-decode roundtrip, decoder strict
refusals, generic substitution, dialect merging.

---

## §3 — `@service_protocol` decorator

**Surface.**

```
def service_protocol[Proto](
    proto: type[Proto] | None = None,
    *,
    dialect: type[Dialect] = FxtrServicesDefaultDialect,
    service_type_id: FxtrID | None = None,
    service_type_validator: ServiceTypeValidator[Proto] | None = None,
) -> ...:
    ...
```

Used as `@service_protocol` (no args) or `@service_protocol(...)`.

**Storage.** Attaches a frozen-dataclass `_ServiceProtocolMetadata`
to the class as `_fxtr_service_protocol_metadata`:

```
@dataclass(frozen=True)
class _ServiceProtocolMetadata:
    dialect: type[Dialect]
    service_type_id: FxtrID | None
    service_type_validator: ServiceTypeValidator | None
    method_specs: Mapping[str, _ProtocolMethodSpec]
    service_type_id_kind: Literal["entity_ref", "fxtr_id", "absent"]
    service_type_id_target: ServiceTypeAnnotation[Any] | None  # T for EntityRef[T]
```

**Validation at decoration time.**

- Walk the Protocol's methods (`inspect.getmembers` or `vars`),
  reject sync methods / staticmethods / classmethods / properties
  / user-defined dunders / methods named `service_type_id` (it's
  reserved).
- For each method, inspect its `inspect.signature` and `typing.get_type_hints(...)`.
- Reject method-level type parameters (any `TypeVar` not bound on
  the class).
- For each parameter annotation and the return annotation, build a
  `BasicEncoder(annotation, default_dialect=dialect)` /
  `BasicDecoder(annotation, default_dialect=dialect)` via the
  cached factory (§4). Mashumaro errors at build time if the
  annotation isn't supported.
- Store the per-method spec (`_ProtocolMethodSpec(signature,
  arg_codecs_by_name, return_codec)`) in the metadata.
- Inspect `typing.get_type_hints(Proto).get("service_type_id")`.
  Decide the `service_type_id_kind`:
  - `EntityRef[T]` → `"entity_ref"`, target = `T`.
  - `FxtrID` → `"fxtr_id"`, target = None.
  - Absent → `"absent"`, target = None.
  - Anything else → registration error.
  Reject if `service_type_id` exists *as a method/property body*
  rather than just an annotation.
- Mutually-exclusive args check: error if both `service_type_id`
  and `service_type_validator` are passed.

**Resolver registration.** Calls
`register_service_resolver(Proto, _make_typed_resolver(metadata))`.

The decorator returns the Protocol class unchanged (apart from the
attached metadata).

**`ServiceTypeValidator` shape.**

```
class ServiceTypeValidator[T](Protocol):
    async def __call__(
        self,
        service_type_id: FxtrID,
        target_service_type: ServiceTypeAnnotation[T],
    ) -> bool: ...
```

Modelled on `EntityLoadHandler.supports_entity_type`. Called by
the resolver after the home scope has lifted `service_type_id`,
so the validator can `EntityRef(service_type_id, ...).load()` if
needed.

---

## §4 — Per-method codecs via dynamic wrapper dataclasses

**Mechanism.** For each method on a Protocol or `TypedServiceImpl`
subclass, we dynamically generate two
`mashumaro.DataClassDictMixin` dataclasses — one for args, one
for the return value — *parameterized by the same TypeVar
objects as the owning class*. Mashumaro's existing TypeVar
propagation then handles substitution end-to-end when we build a
`BasicEncoder` / `BasicDecoder` against the parameterized form.
This sidesteps having to walk-and-substitute annotations
ourselves (which has no clean stdlib API and mashumaro's
internals aren't public).

This same mechanism powers both the proxy side (§5) and the impl
side (§6) — the only difference is which class's TypeVars get
substituted into the wrappers.

**Generated wrapper shape.** For
`async def foo(self, x: T, y: list[T], *items: T2, **extras: int) -> dict[str, T]`:

```
class _Foo_Args[T, T2](DataClassDictMixin):
    x: T
    y: list[T]
    _var_positional: tuple[T2, ...]   # only if *items is present
    _var_keyword: dict[str, int]       # only if **extras is present
    class Config(BaseConfig):
        dialect = <the configured dialect>

class _Foo_Result[T](DataClassDictMixin):
    value: dict[str, T]
    class Config(BaseConfig):
        dialect = <the configured dialect>
```

- Field names follow the parameter names from the owning class's
  signature, with two reserved names for variadic forms:
  `_var_positional` (tuple) and `_var_keyword` (dict). These
  reserved names are normalized — proxy and impl sides use the
  same names regardless of how the user spelled `*args` /
  `**kwargs` in their respective signatures, so cross-side
  rename-tolerance comes for free.
- `Generic[*cls.__type_params__]` (PEP 695) — the wrappers share
  the *same* TypeVar objects as the owning class, so passing the
  parameterized form (`_Foo_Args[int, str]`) to mashumaro
  substitutes correctly throughout.
- The `Config.dialect` slot is how the configured dialect
  reaches mashumaro's codec build — same hook
  `DataclassEntity.Config.serialization_strategy` uses today.

**Construction.** Use `types.new_class(name, (DataClassDictMixin,
Generic[*cls.__type_params__]), exec_body=...)` followed by
`@dataclass`-style field annotation injection (or
`dataclasses.make_dataclass` if it gracefully accepts a
`Generic[...]` base — to confirm in the spike). Either way the
result must be `Generic` *and* a `DataClassDictMixin` subclass.

**On-the-wire shape.** Each side encodes the bound call (args +
kwargs combined into a dict) by calling
`args_codec.encode(_Foo_Args(**bound_dict))`. Mashumaro
serializes that to a `dict[str, FxtrServiceData]`, which we ship
in the wire packet's `kwargs` slot. The wire's `args` slot is
always empty for typed services. Symmetric on the receive side.

The return value is wrapped in `_Foo_Result(value=...)` for
encoding and unwrapped after decoding via mashumaro's
`pre_decoder_func` / `post_encoder_func` hooks (so the wire ships
the bare value, not a `{"value": ...}` blob).

**Caches.** Two module-level caches:

```
@functools.cache
def get_method_args_codec(
    cls: type, method_name: str, parameterization: tuple[type, ...] | None,
    dialect: type[Dialect],
) -> BasicEncoder | BasicDecoder: ...

@functools.cache
def get_method_return_codec(
    cls: type, method_name: str, parameterization: tuple[type, ...] | None,
    dialect: type[Dialect],
) -> BasicEncoder | BasicDecoder: ...
```

(In practice we'll have `get_*_encoder` / `get_*_decoder` pairs.)

`parameterization` is `get_args(concrete_type)` if the owning
class has TypeVars, else `None`. The cache key is hashable: a
class object, a string, a tuple of types, and a dialect class.

A separate inner cache (`get_method_wrappers(cls, method_name)`)
memoizes the *unparameterized* wrapper-dataclass pair per (class,
method); the codec caches above then build encoders/decoders
against `wrapper[*parameterization]` if any.

**`__init_subclass__` / decorator-time validation.** When a
Protocol or impl class is defined, we walk its service methods
and call `get_method_wrappers(cls, method_name)` once per method
— this exercises mashumaro's annotation parsing and surfaces
unsupported annotations / missing forward refs eagerly, even
before any concrete parameterization is supplied. The codecs
themselves are built lazily on first use (mount or resolve)
because they require the parameterization.

**Spike checkpoint.** Before locking this in, validate three
things in a small spike:

1. `types.new_class` with `Generic[*cls.__type_params__]` produces
   a class that mashumaro recognizes as parameterizable
   (`BasicEncoder(GeneratedClass[int])` substitutes correctly).
2. `pre_decoder_func` / `post_encoder_func` cleanly strip the
   `Result` wrapper without surprising fall-through behaviour.
3. The configured dialect threads through to nested
   `EntityRef[T]` / `ServiceRef[T]` strategies correctly when
   mashumaro descends into the wrapper's fields.

If any of (1) / (2) / (3) doesn't work, fall back to the
deep-walk approach in a follow-up sub-substage. Document the
result in `part8_plan.md` ("Spike notes") at the bottom.

---

## §5 — `TypedServiceProxy` factory

**Where.** `typed_service.py`.

**What.** A factory that, given a parameterized Protocol type and a
`ServiceProxyContext`, returns a proxy instance whose methods
match the Protocol's signatures.

**Build cache.**

```
@functools.cache
def _build_proxy_class(
    proto: ServiceTypeAnnotation[Any],
    metadata: _ServiceProtocolMetadata,
) -> type[Any]: ...
```

Key on `(proto_subquery, metadata)`. The factory:

1. Computes `parameterization = get_args(proto_subquery) or None`.
2. For each method in the Protocol, fetches the args codec
   (`get_method_args_encoder(Proto, method_name, parameterization, metadata.dialect)`)
   and return codec
   (`get_method_return_decoder(Proto, method_name, parameterization, metadata.dialect)`)
   from §4's caches.
3. Generates an `async def` wrapper bound to those codecs:
   ```
   async def foo_wrapper(self, *args, **kwargs):
       bound = method_sig.bind(self, *args, **kwargs)
       bound.apply_defaults()
       wrapper_kwargs = {k: v for k, v in bound.arguments.items() if k != "self"}
       # Reserved-name routing for *args / **kwargs:
       if has_var_positional:
           wrapper_kwargs["_var_positional"] = wrapper_kwargs.pop(var_positional_name, ())
       if has_var_keyword:
           wrapper_kwargs["_var_keyword"] = wrapper_kwargs.pop(var_keyword_name, {})
       encoded = args_encoder.encode(_Foo_Args(**wrapper_kwargs))
       result = await invoke_service_method(
           self._ctx, method_name, **encoded
       )
       return return_decoder.decode(result)
   ```
4. The wire's `args` slot is always empty for typed services
   (encoded args ride entirely in `kwargs`). The receiver knows
   to expect this because it's the typed-service convention; the
   underlying `invoke_service_method` doesn't care which slot
   things land in.
5. If `metadata.service_type_id_kind == "entity_ref"`, generates
   a `service_type_id` property returning
   `EntityRef(self._ctx.service_type_id, metadata.service_type_id_target)`.
6. If `service_type_id_kind == "fxtr_id"`, generates a property
   returning `self._ctx.service_type_id`.
7. Always exposes `_service_type_id: FxtrID` as a fallback (the
   underscore-prefix form), so callers can introspect even when
   the Protocol doesn't declare an annotation.
8. Exposes `__repr__` consistent with `UnknownServiceProxy`.

The class inherits `object`; `__init__` takes the
`ServiceProxyContext` and stashes it as `self._ctx`.

**Resolver function (built by `_make_typed_resolver(metadata)`).**

```
async def _resolver(
    context: ServiceProxyContext,
    target_type: ServiceTypeAnnotation[Proto],
) -> Proto:
    # 1. Validate service_type_id if metadata says so.
    if metadata.service_type_id is not None:
        if context.service_type_id != metadata.service_type_id:
            raise ServiceTypeIdMismatchError(...)
    elif metadata.service_type_validator is not None:
        # ServiceProxyContext guarantees service_type_id is
        # tracked locally before resolver is invoked.
        ok = await metadata.service_type_validator(
            context.service_type_id, target_type,
        )
        if not ok:
            raise ServiceTypeIdMismatchError(...)
    # 2. Build / fetch the parameterized proxy class and
    #    instantiate.
    cls = _build_proxy_class(target_type, metadata)
    return cls(context)
```

`ServiceTypeIdMismatchError` is a new exception type on
`fxtr2.services.typed_service`.

---

## §6 — `TypedServiceImpl[T]`

**Where.** `typed_service.py`.

**Shape.**

```
class TypedServiceImpl[T](CommonServiceImpl[T]):
    _service_dialect: ClassVar[type[Dialect]] = FxtrServicesDefaultDialect

    @abc.abstractmethod
    def _service_type_id(self, scope: FxtrScope) -> FxtrID: ...

    @abc.abstractmethod
    def _target_type_annotation(self) -> ServiceTypeAnnotation[T]: ...

    def _own_full_type_for_serialization(self) -> ServiceTypeAnnotation[Self]:
        return type(self)

    async def _handle_invoke(
        self, method_name: str, *args: FxtrServiceData, **kwargs: FxtrServiceData
    ) -> FxtrServiceData: ...
```

**`_own_full_type_for_serialization`.** Returns the concrete
runtime type used to introspect this instance's `@service_method`
annotations. Default: `type(self)`. A generic subclass like
`class MyImpl[T](TypedServiceImpl[Proto[T]])` whose annotations
reference `T` must override to return a parameterized alias
(`MyImpl[int]`); how the instance recovers its type args is its
own business (constructor arg, class attribute set per-instance,
etc.). Different instances of the same class may return different
parameterized forms.

Validation at codec-build time (see below): the returned
annotation must be either `type(self)` or a `types.GenericAlias`
whose origin is `type(self)` and whose args bind every class-level
`TypeVar`. Anything else is a `ServiceProtocolError`.

**`__init_subclass__` validation.** When a subclass is defined:

- Walk `walk_service_methods(cls, require_async=True, forbid_dunders=True)`
  to surface tagged-method shape errors at class-definition time.
- For each tagged method, validate the signature shape: reject
  method-level type parameters; record `inspect.Signature` and
  the bookkeeping needed to build the wrapper dataclasses (the
  field-name → annotation mapping, plus var-positional /
  var-keyword param names if any).
- Eagerly call `get_method_wrappers(cls, method_name)` (§4) per
  tagged method so unsupported annotations fail at
  class-definition time.
- Codec build (`get_method_args_decoder` /
  `get_method_return_encoder`) is **deferred** to mount time —
  it requires the per-instance parameterization from
  `_own_full_type_for_serialization`.

**Mount-time codec build (override `_mount_as_fxtr_service`).**

`TypedServiceImpl._mount_as_fxtr_service(scope, *, task_group)`:

1. `concrete_type = self._own_full_type_for_serialization()`.
2. Validate `concrete_type` against `type(self)` (origin match;
   all class-level TypeVars bound). Compute
   `parameterization = get_args(concrete_type) or None`.
3. For each `@service_method` discovered in `__init_subclass__`,
   fetch the per-method args decoder and return encoder from
   §4's caches keyed on `(cls, method_name, parameterization,
   cls._service_dialect)`.
4. Stash the per-instance method specs (`{method_name:
   _ImplMethodSpec(signature, args_decoder, return_encoder,
   var_positional_name, var_keyword_name)}`) on `self` for
   `_handle_invoke` to use.
5. Delegate to `super()._mount_as_fxtr_service(...)` for the
   actual handle / scope registration.

Two instances of the same generic impl class with different
parameterizations populate two separate cache entries; instances
that share parameterization share codecs.

**`_handle_invoke` flow.**

1. Look up `method_name` in the per-instance method specs. If
   missing, raise `IRException(category="UnknownMethod", ...)`.
2. The wire packet's `args` should be empty (typed-service
   convention); `kwargs` carries the encoded wrapper-dataclass
   dict. If `args` is non-empty, raise
   `IRException(category="MalformedArgs", ...)` (a non-typed
   peer accidentally hitting a typed impl).
3. Decode `kwargs` via `spec.args_decoder.decode(kwargs)` →
   yields a `_Foo_Args` instance. Any decode error →
   `IRException(category="MalformedArgs", ...)`.
4. Build the call dict from the wrapper instance:
   `call_kwargs = {field.name: getattr(decoded, field.name) for field in fields(decoded)}`.
   Reverse the reserved-name routing: pop `_var_positional` /
   `_var_keyword` and re-route into the impl's actual
   `*args_param_name` / `**kwargs_param_name` slots from
   `spec.signature`.
5. `bound = spec.signature.bind(self, **call_kwargs_with_var_routed)`.
6. Invoke `await method(*bound.args, **bound.kwargs)` —
   `BoundArguments.args` / `.kwargs` split by parameter kind so
   positional-only / keyword-only / `*args` / `**kwargs` are all
   preserved correctly.
7. Encode the typed return value through
   `spec.return_encoder.encode(_Foo_Result(value=ret))`. Encode
   error → `IRException(category="MalformedReturn", ...)`. The
   `post_encoder_func` strips the `Result` wrapper so the wire
   sees just the encoded `value`.
8. Return the encoded `FxtrServiceData`.

The dispatch path inside `service_cap_common._dispatch_body` is
unchanged: it calls `_handle_invoke` with the decoded
`FxtrServiceData` args/kwargs from the wire packet.

**Mounting.** Inherited from `CommonServiceImpl._mount_as_fxtr_service`.
No change needed — the typed layer just plugs in via the
`_handle_invoke` override.

---

## §7 — Reserved `service_type_id` property

The reserved attribute lives only on the *Protocol* (not on the
impl). The impl supplies the value via `_service_type_id(scope)`
at mount time. The proxy resolver, when building the proxy class,
emits the matching property:

- `_kind == "entity_ref"`:
  ```
  @property
  def service_type_id(self) -> EntityRef[T]:
      return EntityRef(self._ctx.service_type_id, T)
  ```
  where `T` is the substituted target type from the Protocol's
  annotation.
- `_kind == "fxtr_id"`:
  ```
  @property
  def service_type_id(self) -> FxtrID:
      return self._ctx.service_type_id
  ```
- `_kind == "absent"`: only the underscore-prefix form is
  exposed.

`_service_type_id: FxtrID` is *always* exposed on the proxy as
a fallback for callers who want the bare ID regardless of
Protocol annotation.

---

## §8 — Tests

**`tests/test_services/test_service_dialect.py`** — focused unit
tests:

- Each strategy round-trips through `BasicEncoder` /
  `BasicDecoder`.
- `EntityRef[Note]` encode strips to `FxtrID`; decode of `FxtrID`
  re-attaches `Note`; decode of an `EntityRef` (instead of
  `FxtrID`) is rejected.
- `ServiceRef[Proto]` encode → `ServiceRef[UnknownService]`;
  decode of `ServiceRef[UnknownService]` re-attaches `Proto`;
  decode of `ServiceRef[Proto]` (un-stripped) is rejected.
- `frozenset` / `set` / `frozendict` / `tuple` round-trips,
  including nested under unions / optionals.
- Generic substitution: `EntityRef[T]` substituted to
  `EntityRef[Note]` produces the right decoder.
- Custom dialect that adds a strategy works (sanity check on the
  extension story).

**`tests/test_services/test_typed_service.py`** — end-to-end:

- Same `service_layout` fixture pattern as
  `test_untyped_service.py` (same_scope / child_scope /
  sibling_attach).
- A `_NoteProto(Protocol)` decorated with `@service_protocol()`.
- Impl `_NoteService(TypedServiceImpl[_NoteProto])` mounted;
  proxy resolved; round-trips for:
  - Plain primitives (`int`, `str`, lists, etc.).
  - `EntityRef[Note]` arg + return.
  - `ServiceRef[OtherProto]` arg + return.
  - `frozenset[FxtrID]`, `tuple[int, str]`, `frozendict[str, int]`.
  - Nested dataclass arg with mixed fields.
  - Unions / optionals.
- Exception propagation: impl raises a Python exception →
  `IRException(category=...)` on the proxy side, traceback
  preserved.
- `service_type_id` property:
  - Protocol declares `service_type_id: EntityRef[NoteDescriptor]`;
    impl stores a `NoteDescriptor` and returns its FxtrID;
    proxy's `service_type_id` is an `EntityRef[NoteDescriptor]`
    that loads correctly.
  - Protocol declares `service_type_id: FxtrID`; proxy returns
    bare `FxtrID`.
  - Protocol omits the attribute; proxy exposes `_service_type_id`
    only.
- `service_type_id` mismatch via `service_type_id=` decorator
  arg → `ServiceTypeIdMismatchError` at resolve.
- `service_type_validator=` async function — both pass and fail
  cases. Validator can `EntityRef.load()` the descriptor (proves
  service_type_id is tracked locally before validator runs).
- Dialect override — Protocol decorated with a custom dialect
  that adds a strategy; impl uses the same dialect via
  `_service_dialect = MyDialect`; round-trip works.
- Independent dialects — Protocol uses default dialect, impl
  uses a dialect that re-encodes some type differently;
  encode/decode disagrees → typed mismatch surfaces as
  `IRException`. Sanity check on "no warning, just runtime
  error".
- Class-level generics on the Protocol:
  `_BoxProto[T](Protocol)` with `async def get(self) -> T:` →
  resolve against `_BoxProto[int]`, proxy's `get` returns int.
- Class-level generics on the impl: `_BoxImpl[T](TypedServiceImpl[_BoxProto[T]])`
  whose constructor stashes a `_type_arg: type` and overrides
  `_own_full_type_for_serialization` to return
  `_BoxImpl[self._type_arg]`. Two instances with different
  type args mounted on the same scope round-trip independently.
- Impl whose `_own_full_type_for_serialization` returns a
  shape that doesn't match `type(self)` raises
  `ServiceProtocolError` at mount time.
- Impl with class-level TypeVars but no override raises
  `ServiceProtocolError` at mount time (not all TypeVars bound).
- Method-level generics rejected at decoration time.
- `@service_method` on a sync method on a `TypedServiceImpl`
  subclass rejected at class definition time.
- Args/kwargs handling: positional-only, keyword-only, *args,
  **kwargs all round-trip cleanly.

---

## §9 — Documentation

- New `fxtr2/services/LOCAL_CODE_OVERVIEW.md` section: "Typed
  services". Covers the ABC stack (now `FxtrServiceImpl[T]` →
  `CommonServiceImpl[T]` → {`SimpleAsyncServiceImpl`,
  `TypedServiceImpl`}), the dialect shape, the
  `service_type_id` reserved attribute, and the cross-side
  agreement story.
- `CODE_OVERVIEW.md`: extend the `fxtr2/services/` row to mention
  `service_dialect.py` and `typed_service.py`.

---

## Implementation order

0. **Spike: dynamic generic dataclass + mashumaro TypeVar
   propagation.** Build a tiny scratch script that:
   - Generates a `_Foo_Args[T]` dataclass via `types.new_class`
     with shared TypeVars from a fake "owner class".
   - Runs `BasicEncoder(_Foo_Args[int], default_dialect=MyDialect).encode(...)`
     and confirms `EntityRef[T]` substitutes to `EntityRef[int]`
     correctly through the dialect's strategy.
   - Tries `pre_decoder_func` / `post_encoder_func` to strip a
     `Result` wrapper; confirms the wire sees the bare value.
   Land the spike findings as a "Spike notes" section at the
   bottom of this plan before continuing. If the spike fails,
   pivot to the deep-walk approach and revise §4–§6.
1. Pull `walk_service_methods` out into `service_cap_common.py`;
   migrate `SimpleAsyncServiceImpl` to use it. Suite stays green.
2. Land `service_dialect.py` with `FxtrServicesDefaultDialect`
   and the strategies. Land `test_service_dialect.py`. Suite
   stays green.
3. Land the wrapper-dataclass machinery + codec caches (§4) and
   the `_ServiceProtocolMetadata` dataclass.
4. Land `@service_protocol` decorator (§3) — including the
   eager `get_method_wrappers` walk for shape validation.
5. Land `_build_proxy_class` factory and the resolver (§5).
6. Land `TypedServiceImpl` (§6) with `__init_subclass__`
   validation, `_own_full_type_for_serialization`, and
   mount-time codec build.
7. Land `service_type_id` reserved-attribute logic (§7) on the
   proxy side.
8. Land `test_typed_service.py` end-to-end tests.
9. Update `LOCAL_CODE_OVERVIEW.md` for `fxtr2/services/`.
10. Update `CODE_OVERVIEW.md`.

Each numbered step leaves the suite green.

---

## Checklist

- [x] Spike the dynamic-Generic-dataclass + mashumaro TypeVar
      propagation flow (see Implementation order step 0). Record
      findings in "Spike notes" below. *Result: all three risk
      items pass; plain `@dataclass` (not `DataClassDictMixin`) +
      parameterized fields + `__parameters__` is the working
      shape.*
- [x] Extract `walk_service_methods` into `service_cap_common.py`;
      migrate `SimpleAsyncServiceImpl`. *Helper accepts class or
      instance; validates async + forbids dunders. The
      `_SERVICE_METHOD_MARKER` constant moved to
      `service_cap_common` so both untyped and typed layers
      share it.*
- [x] `fxtr2/services/service_dialect.py`:
      `FxtrServicesDefaultDialect`, `EntityRef` / `ServiceRef`
      generic strategies, `frozenset` / `set` / `frozendict` /
      `tuple` strategies, codec cache. Also added
      `ServiceRef.replace_service_type` since the dialect's
      `ServiceRef[T]` strategy needs it.
- [x] `tests/test_services/test_service_dialect.py`. *20 tests
      green; covers strict-vocabulary refusals, T substitution
      per parameterization, nested collections / unions, codec
      caching, dialect extension via subclass.*
- [x] `_ServiceProtocolMetadata` + `@service_protocol` decorator
      (in `typed_service.py`).
- [x] `_build_proxy_class` factory + `_make_typed_resolver`.
- [x] `ServiceTypeIdMismatchError` exception type.
- [x] `TypedServiceImpl[T]` ABC (in `typed_service.py`) with
      `__init_subclass__` signature validation +
      `_own_full_type_for_serialization` hook + mount-time
      codec build + `_handle_invoke`.
- [x] Reserved `service_type_id` annotation handling on the
      proxy side.
- [x] `tests/test_services/test_typed_service.py`. *56 tests
      across same_scope / child_scope / sibling_attach layouts
      covering primitives, EntityRef/ServiceRef args/returns,
      collections, service_type_id properties (EntityRef[T] /
      FxtrID / absent), service_type_id gating (equality +
      validator), impl-side class-level generics via
      `_own_full_type_for_serialization`, registration errors
      (sync method, dunder, method-level TypeVar, mutually-
      exclusive args), and impl-generic mount-time validation.*
- [x] Update `fxtr2/services/LOCAL_CODE_OVERVIEW.md`. *Updated
      ABC diagram (sibling `SimpleAsyncServiceImpl` / `TypedServiceImpl`
      under `CommonServiceImpl`); added new "Typed services: how
      the wire substitution works" section explaining the wrapper-
      dataclass machinery and strict in/out vocabulary.*
- [x] Update `CODE_OVERVIEW.md`. *Added
      `service_dialect.py` and `typed_service.py` to the file
      listing; updated `service_cap_common.py` description to mention
      `walk_service_methods`.*

---

## Open follow-ups (deferred)

- **`@checked_service_protocol`-style cross-side handshake.**
  The current design supports `service_type_id` /
  `service_type_validator` for resolve-time gating, but doesn't
  implement a cross-side type-identity handshake on the wire.
  If we later want stronger guarantees (e.g. the wire validates
  that the peer's `service_type_id` matches what the local
  Protocol declared), the validator hook is where that
  negotiation would live.
- **Streaming / async generators as service methods.** Out of
  scope for this substage. The current dispatch path is
  request-response; streaming would need a different transport
  shape entirely.
- **Dialect-aware repr / debugging.** Not a substage-8 problem,
  but the typed proxy / impl could expose a richer `repr` that
  names the Protocol + dialect. Add when convenient.

---

## Spike notes

Spike script: `plans/phase3/stage8c_user_caps/part8_spike.py`. All
checks pass. Key findings:

- **Don't inherit from `DataClassDictMixin`.** That mixin compiles
  its unpacker eagerly in `__init_subclass__`, which runs before
  the dialect can be supplied. The wrapper must be a *plain*
  `@dataclass` (or built via `make_dataclass`) — `BasicEncoder` /
  `BasicDecoder(default_dialect=...)` then compile against the
  dialect at codec-build time. Confirmed: a plain dataclass +
  `BasicEncoder` works correctly with the dialect's strategies.
- **`make_dataclass` works for the wrapper build.** Pass
  `bases=(Generic[*type_params],)` to share TypeVar objects with
  the owning class. `types.new_class` + post-decoration via
  `@dataclass` also works as an alternative path.
- **`__parameters__`, not `__type_params__`.** A `make_dataclass`-
  built class exposes its TypeVars on `__parameters__` (the
  pre-PEP-695 attribute); `__type_params__` is empty. The
  implementation should read `__parameters__`.
- **The fields must be parameterized at the field level.** A
  field `ref: FakeEntityRef[T]` (using the wrapper's own TypeVar)
  is what propagates substitution. A bare `ref: FakeEntityRef`
  loses the type info — mashumaro has nothing to substitute.
- **The "external type" must be a plain class, not a dataclass.**
  When mashumaro encounters a nested `@dataclass` field, it
  descends into the dataclass's own fields directly, bypassing
  the dialect's strategy. The real `EntityRef` / `ServiceRef`
  in fxtr2 are already plain classes (not dataclasses) for this
  same reason.
- **`Generic[T]` + `GenericSerializableType` together for the
  helper.** The strategy's `serialize` returns a helper instance;
  the helper's classmethod `_deserialize(value, types)` receives
  the `[T]` from the field annotation. The helper stores the
  resolved target type as a regular field (not a class-level
  attr — frozen dataclasses can't be mutated post-construction,
  so a field is the natural way).
- **`pre_decoder_func` / `post_encoder_func` work cleanly.**
  Tested by wrapping a single-field "Result" dataclass and
  stripping `{"value": x}` → `x` on encode and re-wrapping on
  decode. No surprises.
- **Variadic fields (`tuple[FakeEntityRef[T], ...]`,
  `dict[str, FakeEntityRef[T]]`) round-trip with the right T
  substituted at every leaf.** Confirms that the
  `_var_positional` / `_var_keyword` reserved-name pattern from
  §4 will work for `*args` / `**kwargs` parameter encoding.

Implications for the substage 8 implementation:

- §4 stays as written (wrapper-dataclass approach), with the
  clarifications: plain `@dataclass`, parameterized fields, and
  `__parameters__`.
- The default dialect's `EntityRef` / `ServiceRef` strategies
  follow the existing fxtr2 helper pattern (`Generic[T]` +
  `GenericSerializableType`), with the helper carrying the
  resolved target type as a field.
- No fallback to the deep-walk approach is needed.

---

## Post-implementation cleanup notes

After substage 8 landed, a follow-up review caught two real bugs in
the dispatch path and triggered a broader simplification:

**Bugs fixed.** The original impl-side `_handle_invoke` did
`impl_sig.bind(**call_kwargs)`, which:

1. Failed for any method with positional-only parameters
   (`got some positional-only arguments passed as keyword arguments`).
2. Failed for `*args` if the method had no `**kwargs`
   (`got an unexpected keyword argument 'items'`).
3. *Silently mis-routed* `*args` content into the `**kwargs` dict when
   the method had both `*args` and `**kwargs` — the variadic content
   landed in the wrong slot at runtime. The original test suite missed
   this because the only `*args` coverage was proxy-side
   `USE_REMOTE_DEFAULT` rejection, which never reached the impl.

**Redesign** (replaces parts of §4 / §5 / §6). The bind-then-route
gymnastics on each side are gone. The wrapper-dataclass machinery
stays — it's still the channel for mashumaro TypeVar substitution —
but it's no longer the wire shape, and the variadic reserved-name
swap (`_var_positional` / `_var_keyword`) is gone too. New shape:

- **Wrapper field names mirror the user's parameter names
  one-to-one.** The user-spelled `*args` / `**kwargs` names become
  regular fields with type `tuple[anno, ...]` / `dict[str, anno]`
  and a default of `()` / `{}`. There's no proxy-side or impl-side
  rename step.
- **Proxy.** `sig.bind(self, *user_args, **user_kwargs).apply_defaults()`
  → encode the wrapper instance → strip `USE_REMOTE_DEFAULT` entries
  from the encoded dict → hand the result to a fresh
  `inspect.BoundArguments(sig_no_self, encoded)` → ship its
  `.args` / `.kwargs` directly via `invoke_service_method`. The
  `BoundArguments` split walks the signature in declaration order,
  routing positional-only and pre-`*args` content to the wire's
  positional slot and the rest to the keyword slot. A missing entry
  (`USE_REMOTE_DEFAULT` stripped) makes the split flip cleanly from
  `.args` to `.kwargs` at that point.
- **Impl.** `impl_sig.bind(*wire_args, **wire_kwargs)` collects the
  wire by impl parameter names → `args_decoder.decode(dict(bound.arguments))`
  → strip `USE_REMOTE_DEFAULT` from the decoded fields → call
  `method(*new_bound.args, **new_bound.kwargs)` using a fresh
  `BoundArguments`.

**Rename tolerance.** Variadic-rename tolerance across proxy and
impl now comes for free, not from the reserved-name swap. The
variadic content travels as bare wire positional/keyword args; no
field name appears on the wire. So `*items` on the proxy and
`*things` on the impl agree by Python's calling conventions, not
by a swap convention.

**Validation.** All of the proxy-side validation we briefly added
(`_validate_positional_default_layout`, `_split_decoded_for_bind`,
the in-line nested-`USE_REMOTE_DEFAULT` checks) is gone. Two
natural error paths catch what's left:

- `USE_REMOTE_DEFAULT` nested inside `*args` content / `**kwargs`
  values / collections fails the `FxtrServiceData` vocabulary check
  in `invoke_service_method` (`_NotFxtrServiceDataError`, a
  `TypeError`). The error names the path and the sentinel.
- `USE_REMOTE_DEFAULT` shapes that `inspect.BoundArguments` can't
  faithfully represent (e.g. mid-positional-only) ship a wire shape
  the impl side's `bind` rejects with `got some positional-only
  arguments passed as keyword arguments` or
  `got an unexpected keyword argument 'X'`. Wrapped as
  `MalformedArgs` `IRException` by the typed-service dispatch.

**`_MethodSpec` slimmed down.** Dropped `var_positional_name`,
`var_keyword_name`, and `optional_param_names` (all unused by the
new design). Added `signature_no_self` (precomputed from
`signature` so we don't reconstruct it per call).

**Tests.** Added round-trip coverage for the previously broken
cases (positional-only, plain `*args`, plain `**kwargs`, and
`*args` + `**kwargs` together). Tests of the original "reject
nested `USE_REMOTE_DEFAULT`" behaviour now expect `TypeError`
rather than `IRException` (the rejection moved from the typed
layer to the `FxtrServiceData` layer).

**Net diff.** `typed_service.py` shrank from ~1167 → ~1100 lines
(deleted `_validate_positional_default_layout`,
`_split_decoded_for_bind`, `_validate_no_nested_use_remote_default`,
`_contains_use_remote_default`, plus the inline rename / nested
validation in `_proxy_method`). The wrapper machinery, the
`@service_protocol` decorator, the dialect, the proxy-class /
resolver factories, the `service_type_id` reserved-attribute
handling, and the `__init_subclass__` validation are all
unchanged.
