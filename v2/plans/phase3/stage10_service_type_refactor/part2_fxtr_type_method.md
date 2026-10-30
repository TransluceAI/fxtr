# Stage 10 part 2 — `$fxtr.type` method + proxy refactor

## Motivation

Today every IRHandle mounted by an `FxtrServiceImpl` ships a
`$fxtr.ctx` introspection method but no way to ask the impl "what
type are you?". The runtime type identity (`ServiceRef.service_type_id`)
is sent over the wire as part of every `service_ref` leaf, but:

- The handle itself doesn't expose its own type identity — only the
  surrounding `ServiceRef` carries it.
- Proxies expose `service_type_id` as a synchronous property reading
  the wire-shipped hash — it's a "trust the sender" value, with no
  way for the caller to verify it matches what the impl actually is.
- The typed-service proxy further special-cases this with a reserved
  Protocol attribute (`_PROTO_SERVICE_TYPE_ID_ATTR`), three
  `_ServiceTypeIdSpec` modes (`entity_ref` / `fxtr_id` / `absent`),
  and synthetic wrapper-dataclass codecs to support `EntityRef[T]`
  return shapes. This is bespoke machinery that doesn't compose with
  the broader `@proxy_alias_method` story.

This part adds `$fxtr.type` as a second framework-reserved method
parallel to `$fxtr.ctx`, and routes both proxies' type-id introspection
through it. The wire still ships the type_id on every `ServiceRef`
leaf for the gating fast-path (validators / `service_type_id_check`),
but actual type-loading now goes through a wire dispatch that returns
the impl's authoritative answer.

This part is purely additive on the wire and bookkeeping side: the
"type_id is tracked locally" invariant from
`part6post4_service_ergonomics.md` §2 still holds. Part 3 drops it.

## Scope

This part covers:

- **§2.1.** Add `FXTR_TYPE_METHOD_NAME = "$fxtr.type"` constant +
  cache the impl's type_id on `CommonServiceImpl` at mount time +
  extend `_handle_reserved_invoke` to dispatch the new path.
- **§2.2.** Replace `UnknownServiceProxy.service_type_id` (sync
  property) with an async method that invokes `$fxtr.type` over the
  wire. The wire hash stays accessible via the private
  `proxy._ctx.service_type_id` for advanced users.
- **§2.3.** Strip the typed-service `_PROTO_SERVICE_TYPE_ID_ATTR`
  reserved-name machinery (`_ServiceTypeIdSpec`,
  `_detect_service_type_id`, `service_type_id_spec` field, the
  proxy-build-time property installation, the `_service_type_id`
  fallback). Users opt in to type-id introspection by adding their
  own Protocol method tagged with `@proxy_alias_method("$fxtr.type")`.

Out of scope:

- The "type_id is tracked locally" invariant — still holds. Mount /
  attach / resolve / encode-decode / `bridge_service_from_scope` are
  unchanged.
- The validator-cache / `$fxtr.type`-on-cache-miss change. Stays in
  part 3 because it depends on the new wire encoding.

---

## §2.1 — Impl-side `$fxtr.type` plumbing

### `service_cap_common.py`

Add a constant alongside the existing framework-method constants:

```python
FXTR_TYPE_METHOD_NAME = "$fxtr.type"
"""Wire method name returning the mount scope's runtime type identity.

Takes no args / kwargs. Returns the FxtrID the impl provided via
:meth:`CommonServiceImpl._service_type_id` at mount time. The id is
already tracked in the mount scope, so the per-call return arena
picks it up automatically; the caller-side decode bridges it into
the home scope like any other returned FxtrID.
"""
```

Update the docstring on `FXTR_FRAMEWORK_METHOD_PREFIX` to mention
that two paths are now registered (`$fxtr.ctx`, `$fxtr.type`).

### `CommonServiceImpl`

`_mount_as_fxtr_service` already calls `self._service_type_id()`
once (against the active mount scope). Stash the result on the impl
so the per-call reserved handler can return it without re-tracking:

```python
class CommonServiceImpl[T](FxtrServiceImpl[T]):
    _cached_service_type_id: FxtrID | None = None

    def _mount_as_fxtr_service(self, *, task_group):
        scope = get_active_scope()
        type_id = self._service_type_id()
        self._cached_service_type_id = type_id
        handle = CommonServiceImplHandle(impl=self, scope=scope, task_group=task_group)
        return scope.mount_lowlevel_cap_as_service(handle, self._target_type_annotation(), type_id)
```

Class-level `None` default so two distinct `CommonServiceImpl`
instances don't share cache state. Mount populates it; the reserved
handler reads it.

(Aside: this caches the Python `FxtrID` object identity. The mount
scope already tracks the id; the per-call scope inherits from the
mount scope, so the return-arena path picks the id up via the
ordinary lift on encode.)

### `_handle_reserved_invoke`

Add the second branch:

```python
@final
async def _handle_reserved_invoke(self, method_name, *args, **kwargs):
    if method_name == FXTR_CTX_METHOD_NAME:
        if args or kwargs:
            raise IRException(
                message=f"{FXTR_CTX_METHOD_NAME} takes no args or kwargs",
                category="MalformedPacket",
            )
        return get_active_scope().get_context_marker_id()
    if method_name == FXTR_TYPE_METHOD_NAME:
        if args or kwargs:
            raise IRException(
                message=f"{FXTR_TYPE_METHOD_NAME} takes no args or kwargs",
                category="MalformedPacket",
            )
        if self._cached_service_type_id is None:
            # Defensive: should be unreachable because every
            # dispatch goes through a handle, and the handle is only
            # built inside ``_mount_as_fxtr_service`` after the
            # cache was populated.
            raise IRException(
                message="service_type_id was not cached at mount time",
                category="InternalError",
            )
        return self._cached_service_type_id
    raise IRException(
        message=f"unknown framework method {method_name!r}",
        category="UnknownMethod",
    )
```

Update the docstring listing registered paths.

### Tests for §2.1

Add a test in `tests/test_services/test_untyped_service.py` (or a
new `test_framework_methods.py`) that asserts `$fxtr.type` returns
the registered impl's type_id, both for an `UntypedServiceImpl`
subclass (builtin `untyped_service`) and a `TypedServiceImpl`
subclass with a custom `_service_type_id`. Use the raw-invoke route
(`await getattr(unknown_proxy, "$fxtr.type")()`) and assert equality
to the impl's expected id.

Edge tests:

- `$fxtr.type` with args / kwargs returns
  `IRException(category="MalformedPacket")`.
- An unrecognised `$fxtr.foo` still raises
  `IRException(category="UnknownMethod")` (existing test should
  already cover this; verify it does).

---

## §2.2 — `UnknownServiceProxy.service_type_id` becomes an async method

### `untyped_service.py`

Replace the existing property:

```python
@property
def service_type_id(self) -> FxtrID:
    return self._ctx.service_type_id
```

with an async method that invokes `$fxtr.type` over the wire:

```python
async def service_type_id(self) -> FxtrID:
    """Look up the impl's authoritative runtime type identity by wire dispatch.

    Equivalent to ``await getattr(proxy, "$fxtr.type")()`` — invokes
    the framework-reserved ``$fxtr.type`` method on the bound handle
    and returns the FxtrID the impl declared at mount time. The id
    is bridged into the home scope's main arena via the ordinary
    return-arena path, so callers can :meth:`EntityRef.load` it
    afterwards if it carries a stored type-descriptor entity.

    Advanced callers who want the unverified wire hash (the
    ``service_type_id`` shipped alongside the original
    :class:`ServiceRef`) can read ``self._ctx.service_type_id``
    directly. The wire hash is fast (no dispatch) but
    impl-controlled hash-equal-only — there's no guarantee the
    impl's actual identity matches it. For verified introspection,
    use the async method.
    """
    return await invoke_service_method(
        self._ctx, FXTR_TYPE_METHOD_NAME,
    )
```

The docstring on the class itself is updated:

- Drop the "the `service_type_id` property always wins" caveat —
  it's now a method, but Python's normal MRO still applies (an
  impl-side `@service_method`-tagged method named
  `service_type_id` would still be unreachable through the proxy).
- Add a sentence: "Type-id introspection goes through a wire
  dispatch — call `await proxy.service_type_id()`. The unverified
  wire hash is on `proxy._ctx.service_type_id` for advanced users."

### Tests for §2.2

`tests/test_services/test_untyped_service.py`:

- Update any test that does `proxy.service_type_id` (sync access) to
  `await proxy.service_type_id()`.
- Add a test that asserts `proxy._ctx.service_type_id ==
  await proxy.service_type_id()` for a normal impl (proves the
  hash and the wire response agree on a well-behaved peer).

`tests/test_services/test_bridge_service_from_scope.py` and any
others that assert on `proxy.service_type_id` get the same await
treatment.

---

## §2.3 — Strip typed-service `_PROTO_SERVICE_TYPE_ID_ATTR`

### `typed_service.py`

Delete:

- The `_PROTO_SERVICE_TYPE_ID_ATTR = "service_type_id"` constant.
- The `_ServiceTypeIdSpec` dataclass.
- The `_detect_service_type_id(proto)` function.
- The `service_type_id_spec` field on `_ServiceProtocolMetadata`.
- The "Reserved Protocol attribute `service_type_id` …" paragraph
  in the module docstring.
- The skip-clause in `_walk_protocol_methods` that special-cases the
  reserved name (`if name == _PROTO_SERVICE_TYPE_ID_ATTR: continue`).
  After this part, a Protocol method named `service_type_id` is just
  a regular service method.
- The `_service_type_id` fallback property and the `service_type_id`
  property block in `_build_proxy_class`'s namespace dict.

After these deletions, the only places mentioning type_id in
`typed_service.py` are:

- The decorator's `service_type_id` / `service_type_validator` kwargs
  (still present; gate the resolver). Their semantics don't change
  in this part — they still compare against `context.service_type_id`
  (the wire hash). Part 3 reworks the validator path.
- The `service_type_id_check` field on `_ServiceProtocolMetadata`
  (the kwarg's storage; unchanged).
- The resolver's gate body (lines 904-919; unchanged in this part).

### User-facing replacement: `@proxy_alias_method("$fxtr.type")`

Users who want the type-id-as-method on their typed Protocol now
declare it explicitly:

```python
@service_protocol
class MyService(Protocol):
    @proxy_alias_method("$fxtr.type")
    async def service_type_id(self) -> FxtrID: ...
    # ... other service methods ...
```

If the Protocol's annotation is `EntityRef[T]` rather than `FxtrID`,
the user writes the EntityRef return type and the typed-service
codec handles the conversion automatically — that's just the
`@proxy_alias_method` machinery composing with the existing typed
codec, no special-casing required:

```python
@proxy_alias_method("$fxtr.type")
async def my_type(self) -> EntityRef[MyTypeDescriptor]: ...
```

Document this pattern in `fxtr2/services/LOCAL_CODE_OVERVIEW.md`.

### Tests for §2.3

`tests/test_services/test_typed_service.py`:

- Drop tests that depend on the reserved-attribute behavior. Search
  for any Protocol declaration with `service_type_id: ...` as an
  annotated attribute (not a method). Each one either:
  - Becomes a `@proxy_alias_method("$fxtr.type")` method on the
    Protocol (if the test's intent was to exercise typed-id
    introspection); the test body changes from sync property
    access to `await proxy.service_type_id()`.
  - Gets dropped (if the test was specifically about the reserved
    annotation's three modes).
- Add a positive test for the new pattern: declare a Protocol with
  `@proxy_alias_method("$fxtr.type") async def service_type_id(self)
  -> FxtrID: ...`, mount an impl whose `_service_type_id()` returns
  a known builtin id, resolve the proxy, `await
  proxy.service_type_id()`, assert equality.
- Add a coverage test for the `EntityRef[T]` shape: declare
  `async def my_type(self) -> EntityRef[SomeDescriptor]: ...` with
  the alias, mount an impl that stores a `SomeDescriptor` and
  returns its EntityRef from `_service_type_id()`, assert that
  `(await proxy.my_type()).load()` succeeds.

### Caveat: handle the `$fxtr.type` alias for typed services on impl

For the typed proxy's `@proxy_alias_method("$fxtr.type")` method to
work, the wire hop has to land on `_handle_reserved_invoke` (not on
the typed service's per-method codec). This already happens by
default — `_run_user_method` routes any `method_name.startswith(
FXTR_FRAMEWORK_METHOD_PREFIX)` to `_handle_reserved_invoke`,
bypassing the impl's `_handle_invoke`. So the typed-service codec
layer is never consulted for the `$fxtr.type` request. The Protocol-
side return type (`FxtrID` or `EntityRef[T]`) drives only the
proxy-side decoder; the wire shape is the bare `FxtrID` returned
by the framework handler.

There's one mild wrinkle: a typed proxy method declared with
`@proxy_alias_method("$fxtr.type")` and a return annotation of
`EntityRef[T]` requires the proxy's per-method decoder to convert
the bare-FxtrID wire return to `EntityRef[T]`. This is already
handled by `FxtrServicesDefaultDialect`'s `EntityRef` strategy
(strip on encode, re-attach `T` on decode), so no extra work is
needed.

---

## §2.4 — Documentation

### `fxtr2/services/LOCAL_CODE_OVERVIEW.md`

- Update the "Framework method-name prefix (`$fxtr.`)" section
  (lines 102-146 today): list both `$fxtr.ctx` and `$fxtr.type`
  with their semantics. Show both the untyped (raw `__getattr__`)
  and typed (`@proxy_alias_method("$fxtr.type")`) accept routes.
- Update the file overview row for `untyped_service.py`: the
  `service_type_id` property is gone; `service_type_id` is now an
  async method that invokes `$fxtr.type`.
- Update the file overview row for `typed_service.py`: drop the
  "reserved Protocol attribute `service_type_id`" paragraph.

### `CODE_OVERVIEW.md`

- The wire-shape section near the "Framework method-name prefix"
  bullet — update to mention both `$fxtr.ctx` and `$fxtr.type`.
- Anywhere that mentions `UnknownServiceProxy.service_type_id` or
  the typed-service reserved-attribute machinery: rewrite for the
  new shape.

### Plan-doc cross-references

`plans/phase3/stage8c_user_caps/part6post4_service_ergonomics.md`
§2 introduced the `service_type_id` property on
`UnknownServiceProxy`. Add a one-line "Superseded by stage10
part2: property is now an async method dispatching `$fxtr.type`"
note next to the property's introduction.

## §2.5 — Checklist

- [x] Add `FXTR_TYPE_METHOD_NAME = "$fxtr.type"` constant in
      `service_cap_common.py`.
- [x] Add `_cached_service_type_id` class attr to
      `CommonServiceImpl`; populate in `_mount_as_fxtr_service`.
- [x] Extend `_handle_reserved_invoke` with a `$fxtr.type` branch.
- [x] Add tests for `$fxtr.type` on untyped + typed impls (raw
      invoke route). (Covered by the rewritten
      `TestUnknownServiceProxyTypeId` and
      `TestServiceTypeIdProperty` test classes — they exercise both
      the typed-method route and the raw invoke route end-to-end via
      proxy resolves.)
- [x] Convert `UnknownServiceProxy.service_type_id` from a property
      to an async method that invokes `$fxtr.type`.
- [x] Update `UnknownServiceProxy` docstring (no more "property
      always wins" caveat; new "wire dispatch" wording).
- [x] Migrate every test usage of `proxy.service_type_id` (sync) to
      `await proxy.service_type_id()`.
- [x] Delete `_PROTO_SERVICE_TYPE_ID_ATTR`, `_ServiceTypeIdSpec`,
      `_detect_service_type_id`, `service_type_id_spec` field, the
      reserved-name skip in `_walk_protocol_methods`, and the
      `service_type_id` / `_service_type_id` namespace entries in
      `_build_proxy_class`.
- [x] Migrate typed-service tests to use the new
      `@proxy_alias_method("$fxtr.type")` pattern; add positive +
      `EntityRef[T]` coverage tests. (`WithEntityRefSTIProto` and
      `WithFxtrIdSTIProto` rewritten; `TestServiceTypeIdProperty`
      covers `EntityRef[T]` decode + `FxtrID` direct-return + the
      "no alias method, no proxy attribute" negative.)
- [x] Update `fxtr2/services/LOCAL_CODE_OVERVIEW.md` framework-method
      section + file overview rows.
- [x] Update `CODE_OVERVIEW.md` services subsection.
- [x] Add "Superseded" notes at the relevant spots in part6post4.
- [x] Run the full test suite (`uv run pytest -n auto` from `v2/`).
      Confirm green. (2310 passed, 1 rerun.)

## Notes

- Part 2 doesn't change any wire-shape constants other than adding
  `FXTR_TYPE_METHOD_NAME` to the dispatch table. The `service_ref`
  leaf still ships `(handle, type_id)` and the type_id still rides
  through `fxtr_arena` (until part 3 changes that).
- The "advanced users can read `proxy._ctx.service_type_id`" escape
  hatch is intentional: the wire hash is sometimes useful as a
  lightweight pre-check (e.g. in custom resolvers), and we don't
  need to lock it behind a private prefix more than `_ctx` already
  does.
- Subclasses overriding `_handle_reserved_invoke` (none today,
  but the seam is documented as available) need to handle the new
  path. The default impl covers both registered names; subclasses
  that want to extend should `super()` for the existing paths.
