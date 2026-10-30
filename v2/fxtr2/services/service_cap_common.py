"""Common service-capability plumbing — wire shape, dispatch, packet code.

This module owns everything that's *common* to every concrete service
implementation strategy:

- The wire-shape constants (``IRTagged("fxtr_call", …)`` /
  ``IRTagged("fxtr_return", …)`` and the dependent-ret-ref slot
  field names) and the recursive :data:`FxtrServiceData` vocabulary.
- The encode / decode helpers that convert between
  :data:`FxtrServiceData` Python values and ``IRValue`` trees.
- The :class:`CommonServiceImpl` ABC: declares
  ``_handle_invoke`` (per-call entry point) and
  ``_target_type_annotation`` (for the resulting ``ServiceRef`` to
  carry), and provides a concrete ``_mount_as_fxtr_service`` that
  wraps the impl in a :class:`CommonServiceImplHandle` and registers
  it on the mount scope.
- The dispatch path: :class:`CommonServiceImplHandle` validates the
  request shape and spawns :func:`_dispatch_body`, which opens a
  per-call derived child scope, decodes args, calls
  ``impl._handle_invoke``, encodes the return, and (if needed) emits
  a chain-converter handle for the caller.
- The chain-converter machinery (:class:`_ChainConverterHandle`,
  :func:`_build_chain_bridge`) and the caller-side install code
  (:func:`_install_returned_resources` and friends).
- :func:`invoke_service_method` — the proxy-side entry point: build a
  call packet, dispatch, decode the return, install bridges.

The "what does my impl actually do with a method invocation" knob is
``CommonServiceImpl._handle_invoke``. Subclasses (e.g.
:class:`~fxtr2.services.untyped_service.SimpleAsyncServiceImpl` for
``@service_method`` discovery, future typed-service layers for
mashumaro-driven conversion) plug in there.
"""

from __future__ import annotations

import abc
import contextlib
import contextvars
import dataclasses
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Iterator,
    Mapping,
    Sequence,
)
from typing import Any, ParamSpec, TypeVar, cast, final

import anyio
import anyio.abc
import anyio.streams.memory

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import (
    GLOBAL_SCOPE,
    EntityRef,
    FxtrScope,
    FxtrServiceImpl,
    ServiceKey,
    ServiceProxyContext,
    ServiceRef,
    ServiceTypeAnnotation,
    _identity_dependent_local_converter,
    get_active_scope,
)
from fxtr2.leases.lease_bridges import (
    DependentLocalBridge,
    DependentLocalBridgeConverter,
    DependentPipelinedBridge,
    DependentPipelinedBridgeConverter,
    IndependentLocalBridge,
    IndependentPipelinedBridge,
    LocalOrPipelinedBridge,
)
from fxtr2.leases.lease_types import Lease, LifetimeToken
from fxtr2.leases.leased_cap_common import IRHandleLocalImplBase, wrap_exception
from fxtr2.leases.leased_cap_ir import (
    Call,
    GetItem,
    IRCallArg,
    IRException,
    IRHandle,
    IRTagged,
    IRValue,
    UnwrapForPipelining,
)
from fxtr2.leases.local_lease import DerivedLocalLifetime, LocalLifetimeToken
from fxtr2.services.cap_ir_arena import (
    FXTR_ID_TAG,
    FXTR_ID_WITH_PROV_TAG,
    ArenaIRHandleAdapter,
    CapIRArena,
    collect_precomp_ids,
    compute_and_track_precomp_closure,
    decode_fxtr_id_with_prov,
    encode_fxtr_id_with_prov,
)

# =============================================================================
# USE_REMOTE_DEFAULT — sentinel for typed-service default-handling
# =============================================================================


class _UseRemoteDefault:
    """Singleton sentinel: 'don't ship this arg; let the impl side default it'.

    See :data:`USE_REMOTE_DEFAULT` for the public exposure.
    """

    __slots__ = ()

    _singleton: "_UseRemoteDefault | None" = None

    def __new__(cls) -> "_UseRemoteDefault":
        if cls._singleton is None:
            cls._singleton = super().__new__(cls)
        return cls._singleton

    def __repr__(self) -> str:
        return "USE_REMOTE_DEFAULT"

    def __reduce__(self) -> str:
        # pickle/unpickle as the singleton
        return "USE_REMOTE_DEFAULT"


USE_REMOTE_DEFAULT: Any = _UseRemoteDefault()
"""Sentinel default for typed-service Protocol/impl method parameters.

Use as a parameter default to mean *"if the caller doesn't supply this
arg, don't send anything for it on the wire — let the impl side apply
its own default"*. Typical usage on a Protocol::

    @service_protocol
    class P(Protocol):
        async def foo(
            self,
            x: int,                              # required, no default
            y: str = "client default",           # Protocol's default applied at call time
            z: int = USE_REMOTE_DEFAULT,         # delegated to impl's default
        ) -> str: ...

When ``x: int = USE_REMOTE_DEFAULT`` and the caller omits ``x``, the
proxy *does not ship* ``x`` over the wire. The impl side's mashumaro
wrapper has ``USE_REMOTE_DEFAULT`` as its own field default, so the
field decodes to the sentinel; before invoking the impl method we
strip sentinel-valued kwargs, and Python's normal call machinery
applies the impl method's own default for the omitted parameter.

Typed as ``Any`` so users can write ``x: int = USE_REMOTE_DEFAULT``
without pyright complaining that the sentinel isn't an ``int``.

Restrictions: only meaningful as a *top-level* parameter default. It
is an error to pass ``USE_REMOTE_DEFAULT`` inside a ``*args`` tuple,
inside a ``**kwargs`` value, or nested in any container — the typed
service layer rejects these at encode time. The wire is shipped by
keyword name; positional sentinels would have nowhere to go.
"""


# =============================================================================
# @service_method discovery helper
# =============================================================================


_SERVICE_METHOD_MARKER = "_fxtr_service_method"
"""Function attribute set by ``@service_method``.

Lives here so multiple service-impl strategies (the
``@service_method``-discovery shape used by
:class:`~fxtr2.services.untyped_service.SimpleAsyncServiceImpl` /
:class:`~fxtr2.services.typed_service.TypedServiceImpl` plus any
future strategy) can share a single tagged-method convention.
"""


_P_SVC = ParamSpec("_P_SVC")
_R_SVC = TypeVar("_R_SVC")


def service_method(
    fn: Callable[_P_SVC, Awaitable[_R_SVC]],
) -> Callable[_P_SVC, Awaitable[_R_SVC]]:
    """Mark a service-impl method as exposed via the service protocol.

    Methods without this decorator are not callable through a
    ``ServiceRef[UnknownService]``; the receiver returns
    ``IRException(category="UnknownMethod")`` for unknown names.

    The decorator is consumed by every service-impl strategy that
    uses the tagged-method convention:
    :class:`~fxtr2.services.untyped_service.SimpleAsyncServiceImpl`
    (untyped vocabulary),
    :class:`~fxtr2.services.typed_service.TypedServiceImpl` (mashumaro-
    driven typed services), and any future strategy. The marker
    attribute (:data:`_SERVICE_METHOD_MARKER`) lives next to this
    decorator so all the tagged-method machinery shares one source
    of truth.

    Args:
        fn: The async method to expose. Must be an ``async def``; sync
            methods are rejected by the discovery walk.

    Returns:
        ``fn`` itself, so the decorator is a no-op at call time.
    """
    setattr(fn, _SERVICE_METHOD_MARKER, True)
    return fn


class ServiceProtocolError(TypeError):
    """A service-impl class or Protocol violates the convention.

    Raised when ``@service_method``-tagged methods are sync,
    user-defined dunders, or otherwise structurally invalid.
    """


def walk_service_methods(
    cls_or_instance: type | object,
    *,
    require_async: bool = True,
    forbid_dunders: bool = True,
) -> Mapping[str, Callable[..., Awaitable[Any]]]:
    """Collect ``@service_method``-tagged methods from a class or instance.

    Walks ``cls.__mro__`` (or ``type(instance).__mro__``) from most-
    specific to least-specific. Subclass-defined methods take
    precedence over inherited ones with the same name.

    Args:
        cls_or_instance: Either a class to inspect (returns unbound
            functions in the result mapping) or an instance (returns
            bound methods).
        require_async: If True (default), every tagged method must be
            ``async def`` — sync methods raise
            :class:`ServiceProtocolError`. Both
            :class:`SimpleAsyncServiceImpl` and the typed service
            layer require async; this flag is here for completeness.
        forbid_dunders: If True (default), tagged dunder names raise
            :class:`ServiceProtocolError`. Dunders are reserved for
            Python's protocol and shouldn't be exposed as service
            methods even if the user explicitly tags them.

    Returns:
        Ordered mapping (subclass-first) of method name to bound method
        (if ``cls_or_instance`` is an instance) or unbound function
        (if it's a class).

    Raises:
        ServiceProtocolError: A tagged method is sync (when
            ``require_async``) or is a user-defined dunder (when
            ``forbid_dunders``).
    """
    import inspect  # local import; only needed for the validation pass

    is_instance = not isinstance(cls_or_instance, type)
    cls = type(cls_or_instance) if is_instance else cls_or_instance
    result: dict[str, Callable[..., Awaitable[Any]]] = {}
    for klass in cls.__mro__:
        for name, attr in vars(klass).items():
            if name in result:
                continue
            if not getattr(attr, _SERVICE_METHOD_MARKER, False):
                continue
            if forbid_dunders and name.startswith("__") and name.endswith("__"):
                raise ServiceProtocolError(
                    f"@service_method on dunder name {name!r} on {cls.__name__} "
                    "is not allowed; dunders are reserved for Python's "
                    "object protocol."
                )
            if require_async and not inspect.iscoroutinefunction(attr):
                raise ServiceProtocolError(
                    f"@service_method on {cls.__name__}.{name} must be 'async def'; "
                    "got a sync function."
                )
            result[name] = getattr(cls_or_instance, name) if is_instance else attr
    return result


# =============================================================================
# Type aliases and constants
# =============================================================================


type UnknownService = Any
"""Phantom target type for the wire's untyped service vocabulary.

A ``ServiceRef[UnknownService]`` is the dynamically-typed form: the
resolver returns an :class:`~fxtr2.services.untyped_service.UnknownServiceProxy`
that accepts any method name via ``__getattr__`` and forwards any
:data:`FxtrServiceData`-shaped arguments to the wire.

Lives here (rather than in ``untyped_service.py``) because the wire
decoder produces ``ServiceRef[UnknownService]`` for every
``service_ref`` leaf — it's a wire-vocabulary contract, not specific
to the dynamic-proxy strategy. ``untyped_service.py`` re-exports.
"""


type FxtrServiceData = (
    None
    | bool
    | int
    | float
    | str
    | bytes
    | FxtrID
    | EntityRef[Any]
    | ServiceRef[Any]
    | Mapping[str, "FxtrServiceData"]
    | Sequence["FxtrServiceData"]
)
"""The vocabulary of values that can flow over a service call.

Primitives, nested containers, ``FxtrID`` / ``EntityRef`` references,
and ``ServiceRef`` references. ``str`` and ``bytes`` are leaves (not
iterables) by convention to match how callers use them.

Wire encoding:
- ``FxtrID`` / ``EntityRef`` → ``IRTagged("fxtr_id", bytes)``; both
  decode back to a bare ``FxtrID`` (the untyped path drops
  ``EntityRef``'s target-type annotation).
- ``ServiceRef`` → ``IRTagged("service_ref", IRHandle)``; decodes to
  ``ServiceRef(handle, target_type=UnknownService)``. Like ``EntityRef``,
  the target-type annotation is dropped on the untyped path.
"""


_FXTR_CALL_TAG = "fxtr_call"
"""Outer tag for input packets (``IRTagged("fxtr_call", {…})``)."""

_FXTR_RETURN_TAG = "fxtr_return"
"""Outer tag for return packets (``IRTagged("fxtr_return", {…})``)."""

_SERVICE_REF_TAG = "service_ref"
"""Inner tag for ``ServiceRef`` leaves.

Wire form: ``IRTagged("service_ref", {"handle": IRHandle, "type_id":
bytes})``. The ``type_id`` is just the data-id raw bytes — no
``FxtrID`` envelope (the enclosing ``service_ref`` tag already implies
"this is a type_id"), no provenance parent, no fxtr_arena coverage.
``service_type_id`` is wire-portable identity used for fast-path
comparison / validator caching, *not* arena-tracked content; consumers
that need the layered (loadable) form invoke ``$fxtr.type``."""

_SERVICE_REF_HANDLE_FIELD = "handle"
"""Field name in the ``service_ref`` payload for the bare ``IRHandle``."""

_SERVICE_REF_TYPE_ID_FIELD = "type_id"
"""Field name in the ``service_ref`` payload for the type-id raw bytes
(see :data:`_SERVICE_REF_TAG`)."""

FXTR_FRAMEWORK_METHOD_PREFIX = "$fxtr."
"""Reserved wire-method-name prefix for framework introspection paths.

Method names starting with this prefix are intercepted by
:meth:`CommonServiceImpl._handle_reserved_invoke` before reaching user
dispatch. ``$fxtr.`` is not a syntactically valid Python identifier,
so user methods can't accidentally collide.

Currently registered paths: :data:`FXTR_CTX_METHOD_NAME` and
:data:`FXTR_TYPE_METHOD_NAME`."""

FXTR_CTX_METHOD_NAME = "$fxtr.ctx"
"""Wire method name returning the mount scope's context marker.

Takes no args/kwargs. Returns the live value of
``mount_scope.get_context_marker_id()`` as a :class:`FxtrID`. See the
provenanced-scopes Stage 4 plan for the broader story."""

FXTR_TYPE_METHOD_NAME = "$fxtr.type"
"""Wire method name returning the mount scope's runtime type identity.

Takes no args / kwargs. Returns the FxtrID the impl provided via
:meth:`CommonServiceImpl._service_type_id` at mount time (cached on the
impl as ``_cached_service_type_id``). The id is already tracked in the
mount scope, so the per-call return arena picks it up automatically;
the caller-side decode bridges it into the home scope like any other
returned FxtrID."""


# The ``fxtr_arena`` slot ships a self-describing arena packet so the
# receiver can sync-decode prov-bearing FxtrIDs in args/return without
# needing a `load_flat` round-trip. Three fields:
_FXTR_ARENA_HANDLE_FIELD = "handle"
"""``IRHandle[arena_token]`` wrapping the sender-forked arena."""

_FXTR_ARENA_TRACKED_IDS_FIELD = "tracked_ids"
"""List of ``IRTagged(FXTR_ID_TAG, bytes)`` — the arena's complete liveset.

Every arena-side identity referenced anywhere in the args / return is
in this list. Used by the receiver to seed its
:class:`CapIRArenaRemoteState`'s refcount map before any args are
decoded.
"""

_FXTR_ARENA_INITIAL_PRECOMP_FIELD = "initial_precomp"
"""Closed precomp closure over ``tracked_ids``.

Same shape as ``compute_precomp_closure``'s output (a list of
``{"id": ..., "unwraps_to": ..., "ctx_node": ..., "step_back_p_ctx":
...}`` mappings). Lets the receiver answer
``unwrap_provenance`` / ``c_node_to_context_data`` /
``p_node_to_pathroot`` / ``step_back_p_ctx`` synchronously for any
provenance-bearing FxtrID in the args / return.
"""


def _build_fxtr_arena_packet(
    *,
    handle: IRHandle[Any],
    tracked_ids: Collection[FxtrID],
    initial_precomp: list[dict[str, IRValue]],
) -> dict[str, IRValue]:
    """Assemble the wire `fxtr_arena` Mapping from its three pieces.

    Each ID in `tracked_ids` is bare (the arena-side identity) so the
    encoding is always ``FXTR_ID_TAG``.
    """
    return {
        _FXTR_ARENA_HANDLE_FIELD: handle,
        _FXTR_ARENA_TRACKED_IDS_FIELD: [IRTagged(FXTR_ID_TAG, bytes(i)) for i in tracked_ids],
        _FXTR_ARENA_INITIAL_PRECOMP_FIELD: initial_precomp,
    }


def _parse_fxtr_arena_packet(
    value: Any,
    *,
    where: str,
) -> tuple[IRHandle[Any], list[FxtrID], Sequence[Mapping[str, IRValue]]]:
    """Validate and unpack a wire `fxtr_arena` packet.

    Returns ``(handle, tracked_ids, initial_precomp)``. Raises
    ``IRException(category="MalformedPacket")`` on any structural
    problem.
    """
    if not isinstance(value, Mapping):
        raise IRException(
            message=f"{where}: `fxtr_arena` must be a Mapping, got {type(value).__name__}",
            category="MalformedPacket",
        )
    handle = value.get(_FXTR_ARENA_HANDLE_FIELD)
    if not isinstance(handle, IRHandle):
        raise IRException(
            message=(
                f"{where}: `fxtr_arena.{_FXTR_ARENA_HANDLE_FIELD}` must be an IRHandle, "
                f"got {type(handle).__name__}"
            ),
            category="MalformedPacket",
        )
    raw_tracked = value.get(_FXTR_ARENA_TRACKED_IDS_FIELD, [])
    if not isinstance(raw_tracked, Sequence) or isinstance(raw_tracked, (str, bytes)):
        raise IRException(
            message=(
                f"{where}: `fxtr_arena.{_FXTR_ARENA_TRACKED_IDS_FIELD}` must be a Sequence,"
                f" got {type(raw_tracked).__name__}"
            ),
            category="MalformedPacket",
        )
    tracked_ids: list[FxtrID] = []
    for i, entry in enumerate(raw_tracked):
        if not isinstance(entry, IRTagged) or entry.tag != FXTR_ID_TAG:
            raise IRException(
                message=(
                    f"{where}: `fxtr_arena.{_FXTR_ARENA_TRACKED_IDS_FIELD}[{i}]` must be"
                    f" IRTagged({FXTR_ID_TAG!r}, bytes); got {entry!r}"
                ),
                category="MalformedPacket",
            )
        if not isinstance(entry.value, bytes):
            raise IRException(
                message=(
                    f"{where}: `fxtr_arena.{_FXTR_ARENA_TRACKED_IDS_FIELD}[{i}]` value must"
                    f" be bytes; got {type(entry.value).__name__}"
                ),
                category="MalformedPacket",
            )
        tracked_ids.append(FxtrID(raw_data_id=entry.value))
    raw_precomp = value.get(_FXTR_ARENA_INITIAL_PRECOMP_FIELD, [])
    if not isinstance(raw_precomp, Sequence) or isinstance(raw_precomp, (str, bytes)):
        raise IRException(
            message=(
                f"{where}: `fxtr_arena.{_FXTR_ARENA_INITIAL_PRECOMP_FIELD}` must be a Sequence,"
                f" got {type(raw_precomp).__name__}"
            ),
            category="MalformedPacket",
        )
    initial_precomp = cast(Sequence[Mapping[str, IRValue]], raw_precomp)
    return handle, tracked_ids, initial_precomp


# Field names inside the ``new_dep_leases`` entries in the ``fxtr_return``
# packet. One entry per unique non-input-set token referenced in the
# return body; the wire layer looks them up by name.
_DEP_REF_TOKEN = "token"
"""The target token of the returned cap (a ``LifetimeToken``)."""

_DEP_REF_KEY_LEASE = "key_lease"
"""A ``Lease`` on the dispatch scope's private key (= ``K_call``)."""

_DEP_REF_CONVERTER = "converter"
"""IRHandle keyed on ``K_call``; ``Call("convert", list[Lease])`` returns ``Lease[token]``.
The list is order-insensitive — the receiver maps each lease to its derived-input
slot by ``lease.subject``."""

_DEP_REF_INPUT_TOKENS = "input_tokens"
"""Set of dispatch-scope input tokens (excluding ``K_call``) the converter
expects leases for. Carried on the wire as a list (IR has no native set
type), but order is not significant — the receiver validates by
``lease.subject`` rather than position."""


# =============================================================================
# Validation
# =============================================================================


class NotFxtrServiceDataError(TypeError):
    """A value passed as a service-call arg / return is not ``FxtrServiceData``."""


def validate_fxtr_service_data(value: Any, *, where: str) -> None:
    """Raise ``NotFxtrServiceDataError`` if ``value`` isn't ``FxtrServiceData``-shaped.

    ``str`` and ``bytes`` are leaves; other ``Sequence`` / ``Mapping``
    instances recurse. Mapping keys must be ``str``. ``FxtrID``,
    ``EntityRef[Any]``, and ``ServiceRef[Any]`` are accepted as leaves.

    Args:
        value: Candidate value to validate. Walks the structure
            recursively; raises on the first non-conforming leaf or
            non-string mapping key.
        where: Path-style breadcrumb threaded through recursion (e.g.
            ``"args[0]"`` → ``"args[0][2]"``); appears in the error
            message to identify which sub-value failed.
    """
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return
    if isinstance(value, (FxtrID, EntityRef, ServiceRef)):
        return
    if isinstance(value, Mapping):
        for k, v in value.items():
            if not isinstance(k, str):
                raise NotFxtrServiceDataError(
                    f"{where}: dict keys must be str, got {type(k).__name__}"
                )
            validate_fxtr_service_data(v, where=f"{where}[{k!r}]")
        return
    if isinstance(value, Sequence):
        for i, v in enumerate(value):
            validate_fxtr_service_data(v, where=f"{where}[{i}]")
        return
    raise NotFxtrServiceDataError(
        f"{where}: not a FxtrServiceData primitive (got {type(value).__name__}: {value!r})"
    )


def _arena_id(value: FxtrID) -> FxtrID:
    """Return the arena-side identity of `value`.

    This is the ID the wire arena packet must include in its
    ``tracked_ids`` set so the receiver can decode `value`. For a
    bare ID (``_provenance_parent is None``) this is the data ID
    itself. For a layered (provenance-bearing) ID this is the
    provenance parent — the data ID is implicit, recoverable on the
    receiver via sync ``unwrap_provenance`` once the prov parent's
    precomp is in place.
    """
    return value._provenance_parent if value._provenance_parent is not None else value


def _encode_fxtr_service_data(
    value: FxtrServiceData,
    *,
    fxtr_ids: set[FxtrID],
    service_keys: set[ServiceKey],
    where: str,
) -> IRValue:
    """Encode ``FxtrServiceData`` to ``IRValue``, collecting referenced refs.

    Walks ``value`` recursively. Leaf encodings:

    - ``FxtrID`` / ``EntityRef`` → ``IRTagged(FXTR_ID_TAG, bytes(id))``
      for bare IDs, or ``IRTagged(FXTR_ID_WITH_PROV_TAG,
      bytes(prov_parent))`` for layered IDs. The arena-side identity
      (prov parent for layered IDs, data ID for bare ones) is added
      to ``fxtr_ids`` so the sender can fork an input arena and ship
      a precomp closure that lets the receiver sync-unwrap.
    - ``ServiceRef`` → ``IRTagged(_SERVICE_REF_TAG, {"handle":
      IRHandle, "type_id": <encoded FxtrID>})``; the
      :class:`ServiceKey` ``(ref.handle, ref.service_type_id)`` is
      added to ``service_keys`` and the ``service_type_id`` (encoded
      with prov-awareness) flows through the same arena.

    Args:
        value: A ``FxtrServiceData`` value (already validated by
            :func:`validate_fxtr_service_data`, typically).
        fxtr_ids: Out-parameter set; every encountered FxtrID's
            arena-side identity (prov parent if layered, data ID
            otherwise) is added.
        service_keys: Out-parameter set; every ``ServiceRef``'s
            :class:`ServiceKey` is added to this set.
        where: Breadcrumb for error messages.

    Returns:
        The encoded ``IRValue`` tree, identical in shape to ``value``
        except that leaf refs are replaced with their tagged forms.
    """
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return value
    if isinstance(value, FxtrID):
        fxtr_ids.add(_arena_id(value))
        return encode_fxtr_id_with_prov(value)
    if isinstance(value, EntityRef):
        # EntityRef's target_type is dropped — untyped path doesn't use it.
        fid = value.entity_id
        fxtr_ids.add(_arena_id(fid))
        return encode_fxtr_id_with_prov(fid)
    if isinstance(value, ServiceRef):
        # ServiceRef's target_type is dropped — untyped path doesn't use
        # it. The service_type_id ships as raw data-id bytes (no
        # FxtrID envelope, no provenance parent, no fxtr_arena coverage):
        # it's wire-portable identity for fast-path comparison /
        # validator caching, not arena-tracked content. Consumers that
        # need the layered loadable form must dispatch $fxtr.type.
        service_keys.add(ServiceKey(value.handle, value.service_type_id))
        return IRTagged(
            _SERVICE_REF_TAG,
            {
                _SERVICE_REF_HANDLE_FIELD: value.handle,
                _SERVICE_REF_TYPE_ID_FIELD: bytes(value.service_type_id),
            },
        )
    if isinstance(value, Mapping):
        return {
            k: _encode_fxtr_service_data(
                v,
                fxtr_ids=fxtr_ids,
                service_keys=service_keys,
                where=f"{where}[{k!r}]",
            )
            for k, v in value.items()
        }
    if isinstance(value, Sequence):
        return [
            _encode_fxtr_service_data(
                v,
                fxtr_ids=fxtr_ids,
                service_keys=service_keys,
                where=f"{where}[{i}]",
            )
            for i, v in enumerate(value)
        ]
    raise NotFxtrServiceDataError(
        f"{where}: not a FxtrServiceData primitive (got {type(value).__name__}: {value!r})"
    )


def _decode_fxtr_service_data(
    value: IRValue,
    *,
    fxtr_ids: set[FxtrID],
    service_keys: set[ServiceKey],
    scope: FxtrScope,
    where: str,
    lift_into: FxtrScope | None = None,
) -> FxtrServiceData:
    """Decode an ``IRValue`` produced by :func:`_encode_fxtr_service_data`.

    Leaf decodings:

    - ``IRTagged(FXTR_ID_TAG, bytes)`` → bare ``FxtrID``.
    - ``IRTagged(FXTR_ID_WITH_PROV_TAG, bytes)`` → layered ``FxtrID``
      via :meth:`FxtrScope.embed_as_provenance_parent` on ``scope``.
      The prov ID must be tracked in ``scope`` (so callers must
      bridge any wire-arena into ``scope`` before calling this).
    - ``IRTagged(_SERVICE_REF_TAG, {"handle": IRHandle, "type_id":
      bytes})`` →
      ``ServiceRef(handle, UnknownService, service_type_id=fid)``.
      Type_id ships as raw data-id bytes (no envelope, no provenance);
      only the data-id half travels — consumers wanting the loadable
      form invoke ``$fxtr.type``.

    Args:
        value: The encoded ``IRValue`` tree.
        fxtr_ids: Out-parameter set; every decoded FxtrID's arena-side
            identity is added — symmetric with the encoder's
            collection rule.
        service_keys: Out-parameter set; every ``ServiceRef``'s
            :class:`ServiceKey` is added so the caller can install
            the corresponding cap-bridge on the call scope.
        scope: The scope that has every prov-bearing FxtrID's
            provenance parent tracked (so ``embed_as_provenance_parent``
            can resolve them). For receiver-side arg decoding this is
            the call scope (with the wire arena bridged in); for
            proxy-side return decoding this is the home scope (with
            the return arena bridged in, plus any arg IDs already
            lifted there).
        where: Breadcrumb for error messages.
        lift_into: When set, every decoded FxtrID (and, for layered
            ids, both the data half and the provenance parent) is
            additionally ``lift``-ed into this scope — used by the
            receiver side to keep the pre-input-scope invariant that
            arg ids are directly tracked in the *call* scope (strict
            same-scope ops like ``create_u_node`` /
            ``extract_provenance_parent`` rely on it) while the wire
            arena itself lives on the input scope. ``lift`` is sync,
            copies no data, and is idempotent.
    """
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return value
    if isinstance(value, IRTagged):
        if value.tag in (FXTR_ID_TAG, FXTR_ID_WITH_PROV_TAG):
            try:
                fid = decode_fxtr_id_with_prov(value, embed_prov=scope.embed_as_provenance_parent)
            except (IRException, ValueError) as exc:
                exc.add_note(f"while decoding FxtrID at {where}")
                raise
            fxtr_ids.add(fid._provenance_parent if fid._provenance_parent is not None else fid)
            if lift_into is not None:
                if fid._provenance_parent is not None:
                    lift_into.lift(FxtrID(raw_data_id=fid._raw_data_id))
                    lift_into.lift(fid._provenance_parent)
                else:
                    lift_into.lift(fid)
            return fid
        if value.tag == _SERVICE_REF_TAG:
            payload = value.value
            if not isinstance(payload, Mapping):
                raise IRException(
                    message=(
                        f"{where}: IRTagged({_SERVICE_REF_TAG!r}) payload must be "
                        f"a mapping, got {type(payload).__name__}"
                    ),
                    category="MalformedPacket",
                )
            handle = payload.get(_SERVICE_REF_HANDLE_FIELD)
            if not isinstance(handle, IRHandle):
                raise IRException(
                    message=(
                        f"{where}: service_ref.{_SERVICE_REF_HANDLE_FIELD!r} must be "
                        f"IRHandle, got {type(handle).__name__}"
                    ),
                    category="MalformedPacket",
                )
            type_id_raw = payload.get(_SERVICE_REF_TYPE_ID_FIELD)
            if not isinstance(type_id_raw, bytes):
                raise IRException(
                    message=(
                        f"{where}: service_ref.{_SERVICE_REF_TYPE_ID_FIELD!r} must be "
                        f"raw bytes; got {type_id_raw!r}"
                    ),
                    category="MalformedPacket",
                )
            type_id = FxtrID(raw_data_id=type_id_raw)
            service_keys.add(ServiceKey(handle, type_id))
            return ServiceRef(handle=handle, target_type=UnknownService, service_type_id=type_id)
        raise IRException(
            message=f"{where}: unexpected IRTagged tag {value.tag!r}",
            category="MalformedPacket",
        )
    if isinstance(value, Mapping):
        return {
            k: _decode_fxtr_service_data(
                v,
                fxtr_ids=fxtr_ids,
                service_keys=service_keys,
                scope=scope,
                where=f"{where}[{k!r}]",
                lift_into=lift_into,
            )
            for k, v in value.items()
        }
    if isinstance(value, Sequence):
        return [
            _decode_fxtr_service_data(
                v,
                fxtr_ids=fxtr_ids,
                service_keys=service_keys,
                scope=scope,
                where=f"{where}[{i}]",
                lift_into=lift_into,
            )
            for i, v in enumerate(value)
        ]
    raise IRException(
        message=f"{where}: cannot decode FxtrServiceData from {type(value).__name__}: {value!r}",
        category="MalformedPacket",
    )


# =============================================================================
# CommonServiceImpl ABC + CommonServiceImplHandle
# =============================================================================


class CommonServiceImpl[T](FxtrServiceImpl[T]):
    """Base ABC for service implementations using the standard wire shape.

    Concrete subclasses implement two abstract methods:

    - :meth:`_handle_invoke` is the per-call entry point. Receives
      decoded :data:`FxtrServiceData` args / kwargs (positional and
      keyword respectively) and returns a :data:`FxtrServiceData`
      result. Strategy-specific (method dispatch, type conversion,
      etc.) — see :class:`SimpleAsyncServiceImpl` for the
      ``@service_method`` discovery shape, or substage-8 typed
      services for the mashumaro-driven shape.
    - :meth:`_target_type_annotation` returns the type annotation to
      embed in the resulting :class:`ServiceRef`.

    The provided :meth:`_mount_as_fxtr_service` ties them together:
    constructs a :class:`CommonServiceImplHandle` wrapping ``self`` and
    calls :meth:`FxtrScope.mount_lowlevel_cap_as_service` with the
    target-type annotation.
    """

    _cached_service_type_id: FxtrID | None = None
    """Populated by :meth:`_mount_as_fxtr_service` from
    :meth:`_service_type_id` at mount time, read by
    :meth:`_handle_reserved_invoke` to answer ``$fxtr.type`` requests
    without re-invoking the (mount-scope-relative) :meth:`_service_type_id`.
    ``None`` until mount.
    """

    @abc.abstractmethod
    async def _handle_invoke(
        self, method_name: str, *args: FxtrServiceData, **kwargs: FxtrServiceData
    ) -> FxtrServiceData:
        """Run a single method invocation and return its result.

        Subclasses dispatch on ``method_name`` however they like —
        :class:`SimpleAsyncServiceImpl` looks up an
        ``@service_method``-tagged async method on ``self``; typed
        services apply per-method conversion before delegating.

        Raise :class:`IRException` for protocol-level failures
        (unknown method, malformed input). Other exceptions raised
        from inside this coroutine are wrapped by the dispatch body
        via :func:`wrap_exception` (category=``type(exc).__name__``;
        the original Python traceback is captured into
        ``IRException.traceback_info`` so it survives the wire hop).

        Args:
            method_name: The user-facing method name from the wire packet.
            *args: Decoded positional :data:`FxtrServiceData` args.
            **kwargs: Decoded keyword :data:`FxtrServiceData` args.

        Returns:
            A :data:`FxtrServiceData`-shaped value to ship back to the
            caller. Validated against the vocabulary by the dispatch
            body before encoding.
        """
        ...

    @abc.abstractmethod
    def _target_type_annotation(self) -> ServiceTypeAnnotation[T]:
        """Return the target-type annotation for ``ServiceRef[T]``.

        Threaded into the :class:`ServiceRef` produced by
        :meth:`_mount_as_fxtr_service`. For :class:`UntypedServiceImpl`
        this returns ``UnknownService``; typed services return the
        relevant ``Protocol`` (or other annotation).
        """
        ...

    @abc.abstractmethod
    def _service_type_id(self) -> FxtrID:
        """Track (or look up) the service's runtime type identity in the mount scope, return its FxtrID.

        Called once per mount, right before
        :meth:`FxtrScope.mount_lowlevel_cap_as_service`. The mount
        scope is the active scope (set by :meth:`FxtrScope.mount`), so
        implementations can use scope-relative APIs directly:

        - ``get_active_scope().track_builtin(FxtrID.builtin("..."))``
          for the dynamic-vocabulary case.
        - ``MyDescriptor(...).store().entity_id`` to mint a stored
          descriptor entity in the mount scope.

        The returned id flows into the resulting :class:`ServiceRef`'s
        ``service_type_id`` field and is also cached on this impl as
        ``_cached_service_type_id`` so the ``$fxtr.type`` framework
        method can return it on dispatch. Callers that need the id in
        loadable form (i.e. tracked in the home scope, suitable for
        ``EntityRef.load``) invoke ``$fxtr.type`` — e.g.
        ``await proxy.service_type_id()`` on
        :class:`UnknownServiceProxy`. The same id is also exposed as
        an opaque hash on :attr:`ServiceProxyContext.service_type_id`
        for advanced users that don't need to load it. The framework
        verifies the id is tracked in the mount scope before mounting;
        an untracked id raises a clear ``ValueError`` from
        :meth:`FxtrScope.mount_lowlevel_cap_as_service`.
        """
        ...

    async def _mount_as_fxtr_service(self, scope: FxtrScope) -> ServiceRef[T]:
        """Mount via a framework lifespan attached to ``scope``.

        The lifespan (entered by ``scope.attach_lifespan_to_scope``, which runs
        it authorized + with ``scope`` active) runs the user
        :meth:`_fxtr_mount_lifespan` setup, opens the impl's **own** dispatch
        task group nested inside it, builds + registers the
        :class:`CommonServiceImplHandle`, and yields the ``ServiceRef`` — which
        ``attach_lifespan_to_scope`` returns once setup completes. On mount-scope
        ``aclose`` the token-drain step waits for in-flight calls (they hold
        ``Lease[mount.main]``); only then does the closing event fire, the
        dispatch TG close over already-drained calls, and user teardown run.
        """
        target_type = self._target_type_annotation()

        @contextlib.asynccontextmanager
        async def _framework_lifespan() -> AsyncIterator[ServiceRef[T]]:
            # Runs authorized + with ``scope`` active (set by the lifespan
            # runner), so ``_service_type_id`` / ``_fxtr_mount_lifespan`` setup
            # can use scope-relative APIs and mount sub-services against ``scope``.
            type_id = self._service_type_id()
            # Cache for ``_handle_reserved_invoke`` so ``$fxtr.type`` doesn't
            # re-run the impl's tracking step on every dispatch.
            self._cached_service_type_id = type_id
            async with self._fxtr_mount_lifespan(scope) as lifespan_state:
                async with anyio.create_task_group() as dispatch_tg:
                    handle = CommonServiceImplHandle(self, scope, dispatch_tg, lifespan_state)
                    ref = scope.mount_lowlevel_cap_as_service(handle, target_type, type_id)
                    yield ref
                    # dispatch_tg closes here (in-flight calls already drained);
                    # then the user _fxtr_mount_lifespan teardown runs.

        return await scope.attach_lifespan_to_scope(_framework_lifespan())

    @contextlib.asynccontextmanager
    async def _fxtr_mount_lifespan(self, scope: FxtrScope) -> AsyncIterator[Any]:
        """Per-mount async lifespan hook (no-op default; yields ``None``).

        Single-yield async CM run by the framework mount lifespan: setup before
        the yield runs **authorized** and with ``scope`` as the **active** scope
        (and inside the framework lifespan, before the dispatch TG opens), so it
        may store entities / mount sub-services against ``scope``. The yielded
        value is the per-mount *lifespan state* — the channel for per-mount
        resources (sockets, etc.) — which the framework puts on the per-mount
        :class:`CommonServiceImplHandle`; reach it inside a call via
        :meth:`_fxtr_get_lifespan_state`. Teardown (after the yield) runs after
        the token has drained (all calls complete) and is **unauthorized**.

        The recommended convention is to stash **nothing** on ``self`` — carry
        per-mount state through the yielded value, so the same impl instance
        mounted into two scopes stays isolated (two independent handles).
        """
        yield None

    def _fxtr_get_mount_scope(self) -> FxtrScope:
        """Return the mount scope for the currently-dispatching call.

        Read from the framework-owned ``_CURRENT_DISPATCH_CONTEXT`` contextvar
        (bound per-call by ``_dispatch_body``). The returned scope is authorized
        for the call's duration (§5.3). Raises if called outside a dispatch.

        Lifetime consequence worth knowing: anything mounted on — or kept
        reachable only through — the mount scope or the per-call *call* scope
        holds ``Lease[mount.main]`` (directly, or via the call token), so the
        mount cannot finish draining until it is released. Retaining such a
        lease past the dispatch therefore deadlocks graceful teardown unless
        you own a matching release path. When what you need to keep is the
        call's *data*, retain the input scope instead — see
        :meth:`_fxtr_get_input_scope`.
        """
        ctx = _CURRENT_DISPATCH_CONTEXT.get()
        if ctx is None:
            raise RuntimeError("_fxtr_get_mount_scope() called outside a service dispatch")
        return ctx.handle.scope

    def _fxtr_get_lifespan_state(self) -> Any:
        """Return the lifespan state for the currently-dispatching call.

        The value yielded by this mount's :meth:`_fxtr_mount_lifespan`, read from
        the same ``_CURRENT_DISPATCH_CONTEXT`` contextvar. Raises if called
        outside a dispatch.
        """
        ctx = _CURRENT_DISPATCH_CONTEXT.get()
        if ctx is None:
            raise RuntimeError("_fxtr_get_lifespan_state() called outside a service dispatch")
        return ctx.handle.lifespan_state

    def _fxtr_get_input_scope(self) -> FxtrScope:
        """Return the currently-dispatching call's **input scope**.

        The lifespanless per-call scope owning the call's wire inputs: the
        arg leases, the bridged-in input arena, and everything decoded from
        them (see ``_dispatch_body``). Its base authorization is live for the
        whole call, so no re-authorization is needed to use it during the
        dispatch; it is a *parent* of the call scope, so args are reachable
        from the active scope as usual.

        Why you'd want it: unlike the call scope, the input scope has **no
        mount parentage** — retaining ``clone_authorization()`` on it keeps
        the call's data loadable past the dispatch (drained-but-open, no
        copies) *without pinning the mount token*, so the impl can still
        drain and tear down gracefully. What it pins instead are the
        **callers' arg-export tokens** (the senders of the data), until the
        retention is released. Impls can also ``store()`` /
        ``copy_from_scope`` additional entities into it during the call, or
        bridge resources out through it rather than through the call scope.

        Limitation: the input scope is lifespanless (no task group), so it
        cannot mount or resolve services — it is for entity residency only.

        Raises if called outside a dispatch.
        """
        ctx = _CURRENT_DISPATCH_CONTEXT.get()
        if ctx is None:
            raise RuntimeError("_fxtr_get_input_scope() called outside a service dispatch")
        return ctx.input_scope

    @final
    async def _handle_reserved_invoke(
        self,
        method_name: str,
        *args: FxtrServiceData,
        **kwargs: FxtrServiceData,
    ) -> FxtrServiceData:
        """Handle a framework-reserved ``$fxtr.*`` method invocation.

        Routed to by :func:`_run_user_method` whenever
        ``method_name.startswith(FXTR_FRAMEWORK_METHOD_PREFIX)`` —
        :meth:`_handle_invoke` is bypassed entirely in that case, so
        subclasses can't accidentally override (or fail to delegate
        to) the framework path. The dispatch infrastructure (call
        scope, args decode, return encode, fxtr_arena precomp) is
        identical to user-method dispatch.

        Currently registered paths:

        - :data:`FXTR_CTX_METHOD_NAME` (``"$fxtr.ctx"``): no args / kwargs;
          returns the live value of the mount scope's
          ``get_context_marker_id()``. The active scope at the point
          this runs is the per-call derived scope, which inherits the
          mount scope's marker via Stage 1's parent-marker inheritance,
          so reading from the active scope is equivalent and stays
          correct under any future change to the marker source.
        - :data:`FXTR_TYPE_METHOD_NAME` (``"$fxtr.type"``): no args /
          kwargs; returns the impl's ``_cached_service_type_id`` (set
          at mount time from :meth:`_service_type_id`). Already tracked
          in the mount scope, so the per-call return-arena path picks
          it up automatically and the caller-side decode bridges it
          into the home scope.

        Any other ``$fxtr.*`` name raises
        :class:`IRException(category="UnknownMethod")`.

        This method has a default implementation covering every
        currently-registered path; subclasses do not normally
        override it. It exists as a method (rather than a free
        function) to leave a hook for future strategies that want
        to *extend* the registered paths (e.g. by overriding to
        super()-call for the default set and add their own).
        """
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
                # Defensive: every dispatch goes through a handle, and
                # the handle is only built inside
                # ``_mount_as_fxtr_service`` after the cache was
                # populated, so this should be unreachable in practice.
                raise IRException(
                    message=(
                        "service_type_id was not cached at mount time " f"on {type(self).__name__}"
                    ),
                    category="InternalError",
                )
            return self._cached_service_type_id
        raise IRException(
            message=f"unknown framework method {method_name!r}",
            category="UnknownMethod",
        )


class CommonServiceImplHandle(IRHandleLocalImplBase[Any]):
    """``IRHandle`` wrapping a :class:`CommonServiceImpl` for a specific scope.

    Token = the mount scope's main token. Task group = the mount scope's
    task group. Each request spawns a dispatch task that opens a
    per-call child scope, invokes ``impl._handle_invoke``, and ships
    the result back via an ``anyio`` memory object stream. The
    dispatch task stays alive past the request's resolution so any
    returned ServiceRefs remain dispatchable while the caller holds
    them.
    """

    def __init__(
        self,
        impl: CommonServiceImpl[Any],
        scope: FxtrScope,
        dispatch_tg: anyio.abc.TaskGroup,
        lifespan_state: Any,
    ) -> None:
        """Construct a handle bound to ``impl`` for the given scope.

        Args:
            impl: The concrete service implementation. Each request
                calls ``impl._handle_invoke(method_name, *args,
                **kwargs)`` after decoding.
            scope: The mount scope. ``scope._token`` becomes
                ``self.token`` (handle's token must equal the scope's
                token, per ``mount_lowlevel_cap_as_service``).
            dispatch_tg: The impl's **own** dispatch task group, created
                inside its framework lifespan (nested in the user
                ``_fxtr_mount_lifespan``). Each request spawns a dispatch
                body via ``dispatch_tg.start``; it is *not* the mount
                scope's task group (which hosts only lifespan tasks). It
                closes when the lifespan tears down — after the token has
                drained, so every in-flight call has completed.
            lifespan_state: The value yielded by
                ``impl._fxtr_mount_lifespan(scope)`` for this mount. Reached
                inside a call via ``impl._fxtr_get_lifespan_state()``.
        """
        token = scope._token
        assert token is not None, "mount handle built on an unentered scope"
        self._impl = impl
        self._scope = scope
        self._token = token
        # The base ``IRHandleLocalImplBase`` dispatch path spawns the per-request
        # work coroutine into ``self._task_group``; our ``_run_request_async``
        # spawns the dispatch body into the same group. Both are the impl's OWN
        # dispatch task group (nested in the framework lifespan), not the mount
        # scope's task group.
        self._task_group = dispatch_tg
        self._lifespan_state = lifespan_state

    @property
    def scope(self) -> FxtrScope:
        """The mount scope this handle dispatches against."""
        return self._scope

    @property
    def lifespan_state(self) -> Any:
        """The per-mount value yielded by ``_fxtr_mount_lifespan``."""
        return self._lifespan_state

    async def _run_request_async(
        self,
        lease: Lease[Any],
        request: Call[Any] | GetItem,
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        if isinstance(request, GetItem):
            lease.discard()
            raise IRException(
                message="GetItem is not supported on services",
                category="UnsupportedRequest",
            )
        method_name = request.method
        if not isinstance(method_name, str):
            lease.discard()
            raise IRException(
                message=f"Service method must be a single string; got {method_name!r}",
                category="UnsupportedRequest",
            )
        if not isinstance(request.arg, IRTagged) or request.arg.tag != _FXTR_CALL_TAG:
            lease.discard()
            raise IRException(
                message=(
                    f"Expected IRTagged({_FXTR_CALL_TAG!r}, ...) call arg; "
                    f"got {type(request.arg).__name__}"
                ),
                category="MalformedPacket",
            )
        packet_value = request.arg.value
        if not isinstance(packet_value, Mapping):
            lease.discard()
            raise IRException(
                message=f"Call packet must be a mapping; got {type(packet_value).__name__}",
                category="MalformedPacket",
            )

        # Spawn the dispatch task into the impl's OWN dispatch task group (nested
        # inside the framework lifespan), not the mount scope's task group. It
        # owns ``lease`` from this point on — we transfer it via the partial.
        # ``self._scope`` is always our memory-store parent: even if it's
        # draining (e.g. the spawner pattern, where the service was mounted on a
        # transient ``call_scope`` whose ``aclose`` is now blocked on the
        # caller's ret_ref), the dispatch's outer ``lease`` is itself a
        # ``Lease[self._scope.main]``, so ``_dispatch_body`` can hand that lease
        # to ``open_with_explicit_leases`` as the parent-lease for the sub-call
        # scope — the explicit-lease entry primitive is allowed to derive from a
        # draining-but-open parent.
        # The "err" arm carries any exception (IRException for in-band
        # protocol failures, plain Exception for bugs anywhere else in
        # the dispatch body). The outer ``runner`` wraps non-IRException
        # into IRException once at the request boundary; we do not
        # rewrap here.
        send_stream, recv_stream = anyio.create_memory_object_stream[
            tuple[str, IRValue | BaseException]
        ](max_buffer_size=1)
        async with recv_stream:
            await self._task_group.start(
                _dispatch_body_factory(
                    handle=self,
                    method_name=method_name,
                    packet=packet_value,
                    outer_lease=lease,
                    send_stream=send_stream,
                )
            )
            kind, value = await recv_stream.receive()

        if kind == "err":
            assert isinstance(value, BaseException)
            raise value
        assert not isinstance(value, BaseException)
        return value


@dataclasses.dataclass(frozen=True)
class _DispatchContext:
    """Per-call record bound to ``_CURRENT_DISPATCH_CONTEXT``.

    ``handle`` is the per-mount :class:`CommonServiceImplHandle` (mount
    scope + lifespan state); ``input_scope`` is the call's per-call
    input scope (see ``_dispatch_body``).
    """

    handle: CommonServiceImplHandle
    input_scope: FxtrScope


# Framework-owned contextvar bound per-call (by ``_dispatch_body``) to the
# call's ``_DispatchContext`` (the dispatching ``CommonServiceImplHandle``
# plus the call's input scope), so the user method can reach its mount
# scope / lifespan state / input scope via ``CommonServiceImpl._fxtr_get_*``.
# ONE explicit framework contextvar — not a replay of any captured context.
_CURRENT_DISPATCH_CONTEXT: contextvars.ContextVar["_DispatchContext | None"] = (
    contextvars.ContextVar("_CURRENT_DISPATCH_CONTEXT", default=None)
)


@contextlib.contextmanager
def _bind_current_dispatch_context(
    handle: CommonServiceImplHandle, input_scope: FxtrScope
) -> Iterator[None]:
    """Bind ``_CURRENT_DISPATCH_CONTEXT`` for a single call."""
    tok = _CURRENT_DISPATCH_CONTEXT.set(_DispatchContext(handle=handle, input_scope=input_scope))
    try:
        yield
    finally:
        _CURRENT_DISPATCH_CONTEXT.reset(tok)


def _dispatch_body_factory(
    *,
    handle: CommonServiceImplHandle,
    method_name: str,
    packet: Mapping[str, Any],
    outer_lease: Lease[Any],
    send_stream: anyio.streams.memory.MemoryObjectSendStream[tuple[str, IRValue | BaseException]],
) -> Callable[..., Awaitable[None]]:
    """Bind ``_dispatch_body``'s args into a one-shot callable for ``task_group.start``.

    ``task_group.start`` calls the function with ``task_status=...``;
    ``functools.partial`` would put ``task_status`` after our keyword
    args, which is fine, but a small wrapper makes the signature
    obvious at the call site. (This is *only* an arg-binder — there is no
    captured-context replay here.)
    """

    async def run(task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
        await _dispatch_body(
            handle=handle,
            method_name=method_name,
            packet=packet,
            outer_lease=outer_lease,
            send_stream=send_stream,
            task_status=task_status,
        )

    return run


async def _dispatch_body(
    *,
    handle: CommonServiceImplHandle,
    method_name: str,
    packet: Mapping[str, Any],
    outer_lease: Lease[Any],
    send_stream: anyio.streams.memory.MemoryObjectSendStream[tuple[str, IRValue | BaseException]],
    task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Run a single user-method invocation in its own input + call scope pair.

    Per call, two scopes (stage 12 post 1 — ``input_scope_plan.md``):

    - The **input scope** — parentless, lifespanless
      (:meth:`FxtrScope.lowlevel_start_lifespanless`): it owns the input-arg
      lease tokens (drained from ``packet["leases"]`` by
      :func:`_take_input_leases`), hosts the bridged-in input arena, and is
      what everything wire-borne decodes against. When the call ends we
      ``discard_base_authorization()`` — the scope closes the moment the call
      token's parent lease on it is released, *unless* the impl retained
      ``clone_authorization()`` on it (see
      ``CommonServiceImpl._fxtr_get_input_scope``), in which case it sits
      drained-but-open — with **no task waiting on it** (that's why it must be
      lifespanless rather than a CM in this task) — pinning only the callers'
      arg-export tokens until the retention is released.
    - The **call scope** — has a task group (so the user method can mount
      sub-services) and is derived from the mount scope *plus the input
      scope*, built via ``open_with_explicit_leases`` — the entry primitive
      that may derive from a draining-but-open parent. Args are reachable
      from it through the input-scope parent entry. Once
      ``impl._handle_invoke`` returns or raises, we send a tagged result over
      ``send_stream``; the call scope then drains naturally as the
      ``async with`` exits.

    The dispatch's outer lease (``Lease[mount_scope.main]``) is cloned twice:
    one clone is the call scope's mount-*parent* lease (making
    ``call_scope.main`` derived from ``mount_scope.main`` — a live
    ``Lease[call_scope.main]`` carries ``Lease[mount.main]`` as a via, which is
    what makes drain wait for in-flight calls and breaks the
    "resolve-into-mount-scope" drain cycle — and is also why retaining a
    call-scope lease past the dispatch deadlocks graceful teardown; retain the
    input scope instead when what you need is the call's data); the other
    re-authorizes the mount scope for the call's duration (so
    ``_fxtr_get_mount_scope()`` returns an *authorized* scope) and is discarded
    when the call ends — never held as a standing lease.

    ``task_status.started()`` fires once the call scope is established;
    any further error is reported in-band via ``("err", IRException)``
    rather than as a startup failure.

    Args:
        handle: The dispatching :class:`CommonServiceImplHandle`. Supplies the
            mount scope (``handle.scope``) and impl (``handle._impl``), is bound
            (with the input scope) to ``_CURRENT_DISPATCH_CONTEXT`` for the
            call, and re-authorizes the mount scope around the invoke.
        method_name: User-facing method name from the wire packet.
        packet: Unpacked ``IRTagged("fxtr_call", ...)`` body.
        outer_lease: The dispatch's outer authorization lease (=
            ``Lease[mount_scope.main]``); cloned for the parent input + the
            mount-scope re-auth, then discarded.
        send_stream: One-shot result channel. The body sends exactly
            one ``("ok", packet)`` or ``("err", IRException)`` tuple
            and closes the stream on exit.
        task_status: anyio's ``task_status`` plumbed through
            ``dispatch_tg.start``. ``started()`` is called once the call
            scope is open.
    """
    impl = handle._impl
    mount_scope = handle.scope
    async with send_stream:
        with outer_lease.take() as held_outer_lease:
            try:
                extra_derived_leases = _take_input_leases(packet)
            except _MalformedPacketError as exc:
                # Surface as an in-band IRException so `start()` returns
                # cleanly; the caller's await on recv_stream then raises.
                task_status.started()
                await send_stream.send(("err", wrap_exception(exc, category="MalformedPacket")))
                return
            # Clone the outer lease (Lease[mount.main]) for the call scope's
            # parent input and for the per-call mount-scope re-auth; the
            # original is discarded when the ``with`` block exits.
            try:
                parent_clone = held_outer_lease.clone()
            except BaseException:
                for owned in extra_derived_leases.values():
                    owned.discard()
                raise
            try:
                reauth_clone = held_outer_lease.clone()
            except BaseException:
                parent_clone.discard()
                for owned in extra_derived_leases.values():
                    owned.discard()
                raise

        # The input scope: lifespanless, owning the wire-input leases.
        # ``GLOBAL_SCOPE`` is its only parent (type-descriptor reachability,
        # so impls can ``store()`` fresh entities into it; GLOBAL is eternal,
        # so the parent lease pins nothing that can drain). Started before
        # the call scope so the call scope can take it as a parent.
        input_scope = FxtrScope(
            isolated=True,
            include=[GLOBAL_SCOPE],
            name=f"{mount_scope!r}/call/{method_name}/input",
        )
        # Tagged for the scope-drain report (fxtr2/debug/scope_report.py):
        # per-call scopes are ephemeral by contract, and the report labels
        # them so wedges involving them read correctly.
        input_scope._is_dispatch_input_scope = True
        try:
            input_scope.lowlevel_start_lifespanless(
                parent_leases={GLOBAL_SCOPE: GLOBAL_SCOPE.clone_authorization()},
                extra_derived_leases=extra_derived_leases,
            )
            input_parent_clone = input_scope.clone_authorization()
        except BaseException:
            parent_clone.discard()
            reauth_clone.discard()
            raise

        call_scope = FxtrScope(
            isolated=True,
            include=[mount_scope, input_scope],
            name=f"{mount_scope!r}/call/{method_name}",
        )
        call_scope._is_dispatch_call_scope = True
        try:
            async with call_scope.open_with_explicit_leases(
                parent_leases={mount_scope: parent_clone, input_scope: input_parent_clone},
            ):
                # The parent clones are now owned by the scope. The
                # call scope is established; further errors are reported in-band.
                task_status.started()
                try:
                    try:
                        with (
                            mount_scope.authorize(reauth_clone),
                            _bind_current_dispatch_context(handle, input_scope),
                        ):
                            return_packet = await _run_user_method(
                                call_scope=call_scope,
                                input_scope=input_scope,
                                impl=impl,
                                method_name=method_name,
                                packet=packet,
                            )
                    except Exception as exc:  # noqa: BLE001
                        # Forward any exception (IRException for in-band protocol
                        # failures; anything else for a user-method raise or
                        # post-invoke encode/build failure). The caller side raises
                        # it as-is; the outer ``runner`` wraps non-IRException once.
                        await send_stream.send(("err", exc))
                        return
                    await send_stream.send(("ok", return_packet))
                finally:
                    # Begin the input scope's drain BEFORE the call scope's CM
                    # exit: that exit legitimately parks while the caller holds
                    # returned dependent refs (their wire key leases end K_call
                    # only when released — pre-existing behavior), and the input
                    # base's vias on the *callers'* tokens must not be held
                    # hostage to it. After this discard the input scope is kept
                    # open by the call token's parent lease (until the call token
                    # ends) plus any retention the impl took
                    # (``_fxtr_get_input_scope`` + ``clone_authorization``) —
                    # nobody ever waits on it.
                    input_scope.discard_base_authorization()
        finally:
            # ``authorize`` consumes reauth_clone on the happy/in-band paths;
            # discard is idempotent, so an unconditional discard here also covers
            # the case where entry failed before ``authorize`` took ownership
            # (entry already discarded the parent clones).
            reauth_clone.discard()


class _MalformedPacketError(Exception):
    """Internal: raised by lease-extraction helpers; converted to IRException."""


def _take_input_leases(
    packet: Mapping[str, Any],
) -> dict[LifetimeToken[Any], Lease[Any]]:
    """Extract and take ownership of the leases supplied in ``packet["leases"]``.

    Returns a ``{lease.subject: lease}`` mapping suitable for
    ``open_with_explicit_leases(extra_derived_leases=...)``. On any
    structural error, every lease taken so far is discarded and a
    ``_MalformedPacketError`` is raised.

    Args:
        packet: Unpacked ``IRTagged("fxtr_call", ...)`` body.

    Returns:
        Mapping of subject token → lease (each lease owned, ready to
        transfer onward into ``exchange_forward``).
    """
    raw_leases = packet.get("leases", [])
    if not isinstance(raw_leases, Sequence):
        raise _MalformedPacketError(f"`leases` must be a sequence; got {type(raw_leases).__name__}")
    taken: dict[LifetimeToken[Any], Lease[Any]] = {}
    try:
        for i, entry in enumerate(raw_leases):
            if not isinstance(entry, Lease):
                raise _MalformedPacketError(
                    f"leases[{i}]: expected Lease, got {type(entry).__name__}"
                )
            subject = entry.subject
            if subject in taken:
                raise _MalformedPacketError(f"leases[{i}]: duplicate subject token {subject!r}")
            taken[subject] = entry.take()
    except BaseException:
        for owned in taken.values():
            owned.discard()
        raise
    return taken


async def _run_user_method(
    *,
    call_scope: FxtrScope,
    input_scope: FxtrScope,
    impl: CommonServiceImpl[Any],
    method_name: str,
    packet: Mapping[str, Any],
) -> IRTagged[str, Mapping[str, IRValue]]:
    """Decode the input packet, bridge in the arena, run ``impl._handle_invoke``.

    Any failure (``IRException`` or otherwise) propagates straight out
    of this coroutine. The dispatch boundary in :func:`_dispatch_body`
    forwards whatever surfaces through the ``("err", exc)`` channel,
    and the outer ``runner`` wraps non-``IRException`` into
    ``IRException`` once at the request boundary; this function does
    *not* pre-wrap. Concretely:

    - ``IRException(category="MalformedPacket")`` is raised inline for
      structural problems with the input ``args`` / ``kwargs`` /
      ``fxtr_arena`` (wrong outer type, non-str kwarg keys, values
      outside the ``FxtrServiceData`` vocabulary, or a malformed
      arena handle).
    - ``IRException(category="MalformedReturn")`` is raised inline if
      the user method returned a value that isn't
      ``FxtrServiceData``-shaped.
    - Anything raised by ``_handle_invoke`` propagates as-is.
    - Failures while building the return packet
      (:func:`_build_return_resources`) propagate as their original
      exception, with an ``add_note`` annotation identifying the
      method + impl. The outer wrap layer turns these into
      ``IRException(category=type(exc).__name__)`` and preserves the
      original Python traceback + note via
      :attr:`IRException.traceback_info`.

    Args:
        call_scope: The per-call child scope (parents: mount scope +
            ``input_scope``). The active scope for the
            ``_handle_invoke`` invocation; results are tracked and
            encoded here.
        input_scope: The per-call input scope. The
            ``lazily_attach_from_arena`` host for the incoming
            ``fxtr_arena`` and the decode target for args;
            ``input_scope._derived`` is the case-3 hook that turns
            Lease[input_scope] into Lease[arg-token] on every load.
        impl: The :class:`CommonServiceImpl` to invoke.
        method_name: The user-facing method name. Forwarded to
            ``_handle_invoke`` as the ``method_name`` arg.
        packet: The unpacked ``IRTagged("fxtr_call", ...)`` body. The
            ``"args"`` / ``"kwargs"`` keys carry the call arguments;
            ``"fxtr_arena"`` (optional) is the incoming
            ``IRHandle[B_caller]`` wrapping the caller's input arena;
            ``"leases"`` was already drained by ``_take_input_leases``
            into the call scope's derived inputs.

    Returns:
        The ``IRTagged("fxtr_return", {...})`` packet shipping the
        method's result.
    """
    raw_args = packet.get("args", [])
    raw_kwargs = packet.get("kwargs", {})
    if not isinstance(raw_args, Sequence):
        raise IRException(
            message=f"args must be a sequence; got {type(raw_args).__name__}",
            category="MalformedPacket",
        )
    if not isinstance(raw_kwargs, Mapping):
        raise IRException(
            message=f"kwargs must be a mapping; got {type(raw_kwargs).__name__}",
            category="MalformedPacket",
        )
    for k in raw_kwargs.keys():
        if not isinstance(k, str):
            raise IRException(
                message=f"kwargs key must be str; got {type(k).__name__}",
                category="MalformedPacket",
            )

    # Parse the wire fxtr_arena packet and bridge it into input_scope
    # *before* decoding args. Prov-bearing FxtrIDs are decoded via
    # `input_scope.embed_as_provenance_parent`, which requires the
    # prov ID to be tracked in `input_scope` — so the bridge has to
    # come first. (The call scope reaches everything decoded here
    # through its input-scope parent entry.)
    raw_fxtr_arena = packet.get("fxtr_arena", None)
    if raw_fxtr_arena is not None:
        arena_handle, arena_tracked_ids, arena_initial_precomp = _parse_fxtr_arena_packet(
            raw_fxtr_arena, where="fxtr_arena"
        )
        _bridge_input_arena(
            input_scope=input_scope,
            arena_handle=arena_handle,
            tracked_ids=arena_tracked_ids,
            initial_precomp=arena_initial_precomp,
        )

    # Decode args / kwargs, collecting all referenced FxtrIDs and
    # ServiceRef keys. The decoder uses `input_scope.embed_as_provenance_parent`
    # for prov-bearing leaves; bare leaves come back as bare FxtrIDs.
    arg_ids: set[FxtrID] = set()
    arg_service_keys: set[ServiceKey] = set()
    decoded_args = [
        _decode_fxtr_service_data(
            v,
            fxtr_ids=arg_ids,
            service_keys=arg_service_keys,
            scope=input_scope,
            where=f"args[{i}]",
            lift_into=call_scope,
        )
        for i, v in enumerate(raw_args)
    ]
    decoded_kwargs = {
        k: _decode_fxtr_service_data(
            v,
            fxtr_ids=arg_ids,
            service_keys=arg_service_keys,
            scope=input_scope,
            where=f"kwargs[{k!r}]",
            lift_into=call_scope,
        )
        for k, v in raw_kwargs.items()
    }

    # Validate that any args referencing FxtrIDs actually had an arena.
    if raw_fxtr_arena is None and arg_ids:
        raise IRException(
            message=(
                f"args reference {len(arg_ids)} FxtrID(s) but the packet "
                "has no `fxtr_arena` to bridge them."
            ),
            category="MalformedPacket",
        )

    # Bridge in each ServiceRef arg as a Dependent cap-bridge on
    # input_scope (case 3 — input_scope is derived from arg.handle.token,
    # so we can mint Lease[arg.handle.token] from Lease[input_scope.main]
    # via exchange_reverse). Mounting as Dependent (not Independent) is
    # load-bearing — see §1.6 of part6_plan. The call scope resolves
    # these through its ancestor view of the input-scope parent.
    for key in arg_service_keys:
        _bridge_input_service_ref(input_scope=input_scope, key=key)

    # Run the user method with the call scope as the active scope so
    # impl code can use scope-relative APIs naturally. The contextvar
    # is only set across this single await. Any exception (IRException
    # or otherwise) propagates straight up — the dispatch boundary in
    # ``_dispatch_body`` forwards it through the ``("err", exc)``
    # channel, and the outer ``runner`` wraps non-IRException into
    # IRException once. No preemptive wrap here.
    #
    # Reserved-prefix dispatch: framework introspection paths
    # (``method_name.startswith(FXTR_FRAMEWORK_METHOD_PREFIX)``) bypass
    # the impl's ``_handle_invoke`` entirely and route to
    # ``_handle_reserved_invoke``. This guarantees subclasses can't
    # override / forget to delegate the framework paths, and keeps
    # the user-method registry clean of framework-reserved names.
    with call_scope.as_active_scope():
        if method_name.startswith(FXTR_FRAMEWORK_METHOD_PREFIX):
            result = await impl._handle_reserved_invoke(
                method_name, *decoded_args, **decoded_kwargs
            )
        else:
            result = await impl._handle_invoke(method_name, *decoded_args, **decoded_kwargs)

    try:
        validate_fxtr_service_data(result, where="return")
    except NotFxtrServiceDataError as exc:
        raise wrap_exception(exc, category="MalformedReturn") from None

    # Encode the result, collecting any FxtrIDs / ServiceRefs it references.
    result_ids: set[FxtrID] = set()
    result_service_keys: set[ServiceKey] = set()
    encoded_result = _encode_fxtr_service_data(
        result,
        fxtr_ids=result_ids,
        service_keys=result_service_keys,
        where="return",
    )

    # Auto-lift any returned ids that are reachable via the call_scope's
    # parents (typically the impl's mount scope) but haven't yet been
    # registered locally. Symmetric with the input-arena bridging path
    # for arg ids: an impl that returns something stashed in ``self``
    # at construction time — or otherwise reachable through a parent —
    # shouldn't have to manually track it just to make the return-packet
    # encoder happy. ``lift`` is sync, idempotent for already-tracked
    # ids, and registers an inheritance entry from the parent's bridge
    # without copying any data. If a returned id is unreachable from
    # any ancestor, ``lift`` raises and the caller surfaces a clear
    # IRException with the ``add_note`` context below.
    for nid in result_ids:
        call_scope.lift(nid)

    # Build the return-packet's lease/converter slots covering every
    # unique token referenced in ``result`` (handle tokens) plus the
    # ``fxtr_arena`` token if any new IDs were returned. Tokens that
    # are already in the dispatch scope's derived inputs (i.e. the
    # caller already has them) are skipped — the caller is expected
    # to use its existing bridges for those.
    new_ids = result_ids - arg_ids
    try:
        fxtr_arena_packet, new_indep_leases, new_dep_leases = await _build_return_resources(
            call_scope=call_scope,
            input_scope=input_scope,
            result_keys=result_service_keys,
            new_ids=new_ids,
        )
    except Exception as exc:
        exc.add_note(
            "raised while building the return packet for "
            f"{type(impl).__name__}.{method_name}() — the returned value "
            "references resources the call dispatcher couldn't lift into "
            "the return arena. Returned FxtrIDs / ServiceRef handles must "
            "be reachable from the call scope (e.g. tracked via "
            "track_builtin / copy_from_scope into the impl's mount scope "
            "at construction time, or received as method args)."
        )
        raise

    return IRTagged(
        _FXTR_RETURN_TAG,
        {
            "result": encoded_result,
            "fxtr_arena": fxtr_arena_packet,
            "new_indep_leases": new_indep_leases,
            "new_dep_leases": new_dep_leases,
        },
    )


async def _build_return_resources(
    *,
    call_scope: FxtrScope,
    input_scope: FxtrScope,
    result_keys: set[ServiceKey],
    new_ids: set[FxtrID],
) -> tuple[
    dict[str, IRValue] | None,
    list[Lease[Any]],
    list[Mapping[str, IRValue]],
]:
    """Build the wire-side resources for the return packet.

    For every unique token referenced in the encoded result + the
    ``fxtr_arena`` (if new IDs are returned), produces one of:

    - **No emit** — token is in ``call_scope._derived.inputs`` or in
      ``input_scope._derived.inputs`` (i.e. the caller already has a
      bridge for it — it sent the lease in the original packet; arg
      tokens live on the input scope).
    - **``new_indep_leases`` entry** — call scope's bridge for the
      token is :class:`IndependentLocalBridge` (clone the stored
      lease and ship as a concrete ``Lease[token]``) or
      :class:`IndependentPipelinedBridge` (await the inner future to
      resolve to a concrete lease before shipping — at-rest pipelined
      leases never escape onto the wire from this side).
    - **``new_dep_leases`` entry** — call scope's bridge is
      :class:`DependentLocalBridge` / :class:`DependentPipelinedBridge`
      (or absent, for ``call_scope.main``): build a
      :class:`_ChainConverterHandle` keyed on ``K_call``, ship the
      handle plus a fresh ``Lease[K_call]`` and the ordered list of
      input tokens the caller must supply at dispatch.

    Returns ``(fxtr_arena_packet, new_indep_leases, new_dep_leases)``:
    ``fxtr_arena_packet`` is ``None`` unless ``new_ids`` is non-empty
    (in which case it's the self-describing packet shape from
    :func:`_build_fxtr_arena_packet`, including a precomp closure so
    the receiver can sync-decode prov-bearing FxtrIDs in the result).
    """
    assert call_scope._derived is not None
    assert call_scope._key_ull is not None
    assert call_scope._task_group is not None

    assert input_scope._derived is not None
    assert input_scope._key_ull is not None

    derived_inputs = call_scope._derived.inputs
    input_scope_inputs = input_scope._derived.inputs
    k_call_token = call_scope._key_ull.token
    input_main_token = input_scope.scope_token
    # Wire converter input tokens: the call scope's inputs minus K_call
    # and the input scope's main (≈ the mount token), plus the input
    # scope's inputs minus its key (= the call's arg tokens) — the same
    # set the pre-input-scope single-scope dispatch shipped. The handle
    # re-mints ``Lease[input.main]`` per convert from its at-rest key
    # lease + the arg leases (see ``_make_chain_converter_handle``).
    # Order doesn't matter — the wire dispatch carries leases as a
    # list, and the receiver maps them by subject (see
    # ``_ChainConverterHandle._run_request_async``).
    x_tokens: frozenset[LifetimeToken[Any]] = (
        derived_inputs - {k_call_token, input_main_token}
    ) | (input_scope_inputs - {input_scope._key_ull.token, GLOBAL_SCOPE.scope_token})

    # Collect referenced tokens from result service keys.
    referenced_tokens: set[LifetimeToken[Any]] = {k.handle.token for k in result_keys}

    # Fork an arena if any new IDs were returned. The closure walk
    # runs on the **fork** (not the source), so every visited ID
    # lands in the fork's liveset — that's the server-side guarantee
    # that any precomp value the receiver may sync-discover is
    # ``load_flat``-able through the bridge. (See
    # ``v2/plans/phase3/stage8c_user_caps/part5bugfix1_plan.md`` for
    # the invariant discussion. The fork's token aliases the
    # source's, so the source's lease validates against it.)
    fxtr_arena_packet: dict[str, IRValue] | None = None
    if new_ids:
        sub_arena = call_scope._main_arena.copy_to_new_arena(
            call_scope._current_auth_lease().clone(), list(new_ids)
        )
        fxtr_arena_handle = ArenaIRHandleAdapter(sub_arena, task_group=call_scope._task_group)
        precomp = compute_and_track_precomp_closure(
            sub_arena, call_scope._current_auth_lease().clone(), new_ids
        )
        fxtr_arena_packet = _build_fxtr_arena_packet(
            handle=fxtr_arena_handle,
            tracked_ids=new_ids,
            initial_precomp=precomp,
        )
        # Register the introduction refcount: every id mentioned in
        # this packet (tracked_ids ∪ precomp keys ∪ precomp values)
        # contributes +1 to the adapter's ``_total_introductions``.
        # The receiver mirrors the bump on its
        # ``_remote_refcounts``; on abandon it ships the count back
        # so the server can decrement the same amount.
        fxtr_arena_handle.register_emission(set(new_ids) | collect_precomp_ids(precomp))
        referenced_tokens.add(fxtr_arena_handle.token)

    new_indep_leases: list[Lease[Any]] = []
    new_dep_leases: list[Mapping[str, IRValue]] = []

    try:
        for tok in referenced_tokens:
            if tok in derived_inputs or tok in input_scope_inputs:
                # Caller already has a bridge for this token (it sent
                # the lease in the original packet — arg tokens are the
                # input scope's derived inputs); no emit needed.
                continue
            entry = call_scope._cap_bridges.get(tok)
            if entry is not None and isinstance(entry.bridge, IndependentLocalBridge):
                # Clone the stored lease and ship as Independent.
                new_indep_leases.append(entry.bridge.lease.clone())
                continue
            if entry is not None and isinstance(entry.bridge, IndependentPipelinedBridge):
                # At-rest pipelined: resolve to a concrete lease before
                # shipping — wire-shipped entries are always concrete
                # ``Lease[token]``.
                pipelined = entry.bridge.pipelined
                new_indep_leases.append(await pipelined.future.wait(pipelined.lease.clone()))
                continue
            # Dependent or absent (=> arena on call_scope.main) → chain handle.
            chain_handle = _make_chain_converter_handle(
                call_scope=call_scope,
                input_scope=input_scope,
                target_token=tok,
            )
            new_dep_leases.append(
                {
                    _DEP_REF_TOKEN: tok,
                    _DEP_REF_KEY_LEASE: call_scope._current_key_lease(),
                    _DEP_REF_CONVERTER: chain_handle,
                    _DEP_REF_INPUT_TOKENS: list(x_tokens),
                }
            )
    except BaseException:
        # Discard everything we've allocated so far. The
        # ``fxtr_arena_handle`` (if any) has no public close — its
        # underlying arena will be cleaned up via finalize when the
        # handle is GC'd.
        for lease in new_indep_leases:
            lease.discard()
        for entry_dict in new_dep_leases:
            key_lease = entry_dict[_DEP_REF_KEY_LEASE]
            assert isinstance(key_lease, Lease)
            key_lease.discard()
        raise

    return fxtr_arena_packet, new_indep_leases, new_dep_leases


def _validate_case3_inputs(
    scope: FxtrScope, target_token: LifetimeToken[Any]
) -> DerivedLocalLifetime[Any]:
    """Confirm ``scope`` is derived and ``target_token`` is one of its inputs.

    Used against the per-call input scope (wire-input tokens). Returns
    the derived lifetime so the caller can build a converter directly
    closing over it. Raises :class:`IRException` on misconfiguration.
    """
    if scope._derived is None:
        raise IRException(
            message=f"internal error: {scope!r} is not a derived scope",
            category="InternalError",
        )
    if target_token not in scope._derived.inputs:
        raise IRException(
            message=(
                f"input lease subject {target_token!r} is not among {scope!r}'s derived inputs"
            ),
            category="MalformedPacket",
        )
    return scope._derived


# =============================================================================
# Chain converter — for returned tokens whose dispatch-scope bridge is Dependent
# =============================================================================


class _ChainConverterHandle(IRHandleLocalImplBase[Any]):
    """Server-side chain-converter handle for one returned dependent ref.

    Token is the dispatch scope's private key (``K_call`` =
    ``call_scope._key_ull.token``). Each dispatch:

    1. Receives outer ``Lease[K_call]`` and arg = ``list[Lease]``,
       one lease per expected ``_x_tokens`` entry — the union of the
       call scope's non-``K_call``/non-``input.main`` derived inputs
       (the mount) and the *input scope's* non-key derived inputs (the
       call's arg tokens). This is the same token set the
       pre-input-scope single-scope dispatch shipped. Order of the
       list is irrelevant — the handle maps each lease to its slot by
       ``lease.subject`` (validated against ``_x_tokens``).
    2. Re-mints the input scope's main (hop 1):
       ``input_derived.exchange_forward({at-rest clones (key + GLOBAL
       parent), arg leases...})`` → ``Lease[input.main]``. The handle
       holds the input scope's *key* lease + a GLOBAL authorization at
       rest — neither pins anything that can drain (keys are inputs to
       the derivation, not outputs; GLOBAL is eternal), so the input
       scope itself stays unpinned between converts; standing pins for
       a held returned ref live caller-side exactly as before (where
       ``_UseBaseSpec`` dodges own-token self-pins).
    3. Re-mints the call scope's main (hop 2):
       ``derived.exchange_forward({K_call: outer, mount lease...,
       input.main: <hop-1 result>})`` → ``Lease[call_scope.main]``.
    4. Calls ``snapshot.bridge_converter(snapshot_clones,
       call_scope_main)`` → ``Lease[target]`` and returns it.

    Snapshots ``call_scope._cap_bridges[target_token].bridge`` at
    construction (clones every entry of ``bridge.leases``, captures
    a Python ref to ``bridge.converter``). This makes the chain
    handle independent of subsequent ``forget_service`` / mutation
    on ``call_scope``: it always uses its own snapshot.

    The snapshot leases + the input-scope key lease live until the
    handle's ``token`` (= K_call) ends; a token-end callback discards
    them. K_call's token ends naturally when its lease count drops to
    0 (caller and dispatch scope both released their clones).
    """

    def __init__(
        self,
        *,
        token: LocalLifetimeToken[Any],
        task_group: anyio.abc.TaskGroup,
        derived: DerivedLocalLifetime[Any],
        call_x_tokens: Collection[LifetimeToken[Any]],
        input_derived: DerivedLocalLifetime[Any],
        input_main_token: LifetimeToken[Any],
        input_at_rest: Mapping[LifetimeToken[Any], Lease[Any]],
        input_x_tokens: Collection[LifetimeToken[Any]],
        snapshot_bridge: DependentLocalBridge | DependentPipelinedBridge,
    ) -> None:
        self._token = token
        self._task_group = task_group
        self._derived = derived
        # Stored as frozensets: ``Call("convert", x_leases)`` is
        # validated by lease subject (not position), so order doesn't
        # matter here or on the wire.
        self._call_x_tokens: frozenset[LifetimeToken[Any]] = frozenset(call_x_tokens)
        self._input_x_tokens: frozenset[LifetimeToken[Any]] = frozenset(input_x_tokens)
        self._x_tokens: frozenset[LifetimeToken[Any]] = self._call_x_tokens | self._input_x_tokens
        self._input_derived = input_derived
        self._input_main_token = input_main_token
        # Input-scope derived inputs the caller doesn't supply (its key +
        # the eternal GLOBAL parent), held at rest until K_call token end
        # — neither pins anything that can drain. Ownership taken.
        self._input_at_rest: dict[LifetimeToken[Any], Lease[Any]] = dict(input_at_rest)
        self._snapshot_bridge = snapshot_bridge
        token.register_end_callback(self._on_token_end)

    def _on_token_end(self) -> None:
        self._snapshot_bridge.discard_at_rest()
        for lease in self._input_at_rest.values():
            lease.discard()
        self._input_at_rest.clear()

    async def _run_request_async(
        self,
        lease: Lease[Any],
        request: Call[Any] | GetItem,
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        if isinstance(request, GetItem):
            lease.discard()
            raise IRException(
                message="GetItem is not supported on chain converters",
                category="UnsupportedRequest",
            )
        if request.method != "convert":
            lease.discard()
            raise IRException(
                message=(
                    "chain converter only accepts Call('convert', ...); "
                    f"got method {request.method!r}"
                ),
                category="UnsupportedRequest",
            )
        x_leases = request.arg
        if not isinstance(x_leases, list):
            lease.discard()
            raise IRException(
                message=(
                    f"chain converter arg must be a list of Leases; got {type(x_leases).__name__}"
                ),
                category="MalformedPacket",
            )
        # Take ownership of every input lease and map by subject. The
        # caller's iteration order is irrelevant — what matters is
        # that each ``x_lease.subject`` is in our expected
        # ``_x_tokens`` set, with no duplicates and nothing missing.
        # ``supplied`` is the single source of truth for the caller's
        # leases (including the outer K_call lease): the exchanges
        # below consume fresh *clones* (a supplied lease can feed both
        # hops), and every supplied original is discarded on exit.
        supplied: dict[LifetimeToken[Any], Lease[Any]] = {self._token: lease}
        try:
            for x_lease in x_leases:
                if not isinstance(x_lease, Lease):
                    raise IRException(
                        message=(
                            "chain converter input must be a Lease, "
                            f"got {type(x_lease).__name__}"
                        ),
                        category="MalformedPacket",
                    )
                subject = x_lease.subject
                if subject in supplied:
                    x_lease.discard()
                    raise IRException(
                        message=(
                            f"chain converter received duplicate leases for token {subject!r}"
                        ),
                        category="MalformedPacket",
                    )
                supplied[subject] = x_lease
                if subject not in self._x_tokens:
                    raise IRException(
                        message=(
                            "chain converter received a lease on unexpected "
                            f"token {subject!r}; expected one of {self._x_tokens!r}"
                        ),
                        category="MalformedPacket",
                    )
            missing = self._x_tokens - supplied.keys()
            if missing:
                raise IRException(
                    message=(
                        "chain converter missing leases for tokens "
                        f"{sorted(repr(t) for t in missing)}"
                    ),
                    category="MalformedPacket",
                )

            # Hop 1: re-mint the input scope's main from its at-rest
            # inputs (key + GLOBAL parent, held by this handle) + the
            # caller's arg leases.
            input_forward: dict[LifetimeToken[Any], Lease[Any]] = {}
            try:
                for tok, at_rest in self._input_at_rest.items():
                    input_forward[tok] = at_rest.clone()
                for tok in self._input_x_tokens:
                    input_forward[tok] = supplied[tok].clone()
            except BaseException:
                for v in input_forward.values():
                    v.discard()
                raise
            input_main = self._input_derived.exchange_forward(input_forward)

            # Hop 2: re-mint the call scope's main from K_call + the
            # call-side inputs + the hop-1 result.
            call_forward: dict[LifetimeToken[Any], Lease[Any]] = {
                self._input_main_token: input_main
            }
            try:
                call_forward[self._token] = supplied[self._token].clone()
                for tok in self._call_x_tokens:
                    call_forward[tok] = supplied[tok].clone()
            except BaseException:
                for v in call_forward.values():
                    v.discard()
                raise
            call_scope_main = self._derived.exchange_forward(call_forward)
        finally:
            for v in supplied.values():
                v.discard()
        # Run the snapshot bridge's converter with fresh clones of its
        # at-rest entries. The converter consumes its inputs (clones +
        # base).
        bridge = self._snapshot_bridge
        if isinstance(bridge, DependentLocalBridge):
            local_clones = {tok: lease.clone() for tok, lease in bridge.leases.items()}
            return bridge.converter(local_clones, call_scope_main)
        cloned = bridge.clone()
        return cloned.converter(cloned.leases, call_scope_main)


def _make_chain_converter_handle(
    *,
    call_scope: FxtrScope,
    input_scope: FxtrScope,
    target_token: LifetimeToken[Any],
) -> _ChainConverterHandle:
    """Build a server-side ``_ChainConverterHandle`` for ``target_token``.

    Snapshots ``call_scope._cap_bridges[target_token].bridge``. The
    dispatch scope must be a derived scope (``_derived`` and
    ``_key_ull`` set), and its bridge for ``target_token`` must be
    :class:`DependentLocalBridge` / :class:`DependentPipelinedBridge`
    (Independent targets ship as ``new_indep_leases`` instead).

    The handle's expected wire-input token set (``_x_tokens``) is
    derived here from the two scopes: the call scope's inputs minus
    ``{K_call, input.main}`` (≈ the mount token) plus the input
    scope's inputs minus its key (= the call's arg tokens) — the same
    set the pre-input-scope single-scope dispatch shipped, so the
    caller-side contract is unchanged. The handle also takes the input
    scope's *key* lease at rest (released at K_call token end) so each
    convert can re-mint ``Lease[input.main]`` — a key lease pins
    nothing, so holding a returned ref leaves the input scope (and the
    mount) unpinned server-side, exactly as before.
    """
    assert call_scope._derived is not None, "chain handle requires a derived call scope"
    assert call_scope._key_ull is not None, "chain handle requires a derived call scope"
    assert call_scope._task_group is not None, "chain handle requires an open task group"
    assert input_scope._derived is not None, "chain handle requires a derived input scope"
    assert input_scope._key_ull is not None, "chain handle requires a derived input scope"
    entry = call_scope._cap_bridges.get(target_token)
    snapshot_bridge: DependentLocalBridge | DependentPipelinedBridge
    if entry is None:
        # No bridge installed — only valid when target_token is the
        # call scope's own main token (the typical "no service mounted,
        # just a forked arena" return shape). Synthesize an identity
        # bridge so the dispatch produces ``Lease[call_scope.main]``
        # straight from ``exchange_forward``.
        if target_token is not call_scope._token:
            raise IRException(
                message=(
                    f"call scope has no bridge for returned token {target_token!r} "
                    "and it is not the call scope's main token; cannot produce "
                    "a chain converter."
                ),
                category="MalformedReturn",
            )
        snapshot_bridge = DependentLocalBridge(
            leases={},
            converter=_identity_dependent_local_converter,
        )
    else:
        bridge = entry.bridge
        if not isinstance(bridge, (DependentLocalBridge, DependentPipelinedBridge)):
            raise AssertionError(
                "_make_chain_converter_handle called for an Independent bridge "
                f"(token={target_token!r}); use the new_indep_leases path instead."
            )
        snapshot_bridge = bridge.clone()
    input_main_token = input_scope.scope_token
    global_token = GLOBAL_SCOPE.scope_token
    call_x_tokens = call_scope._derived.inputs - {call_scope._key_ull.token, input_main_token}
    input_x_tokens = input_scope._derived.inputs - {input_scope._key_ull.token, global_token}
    input_at_rest: dict[LifetimeToken[Any], Lease[Any]] = {
        input_scope._key_ull.token: input_scope._current_key_lease()
    }
    if global_token in input_scope._derived.inputs:
        input_at_rest[global_token] = GLOBAL_SCOPE.clone_authorization()
    return _ChainConverterHandle(
        token=call_scope._key_ull.token,
        task_group=call_scope._task_group,
        derived=call_scope._derived,
        call_x_tokens=call_x_tokens,
        input_derived=input_scope._derived,
        input_main_token=input_main_token,
        input_at_rest=input_at_rest,
        input_x_tokens=input_x_tokens,
        snapshot_bridge=snapshot_bridge,
    )


# =============================================================================
# Caller-side chain bridge install
# =============================================================================


@dataclasses.dataclass(frozen=True)
class _UseBaseSpec:
    """Mint spec: input == home_main; uses ``base.clone()`` at mint time."""


@dataclasses.dataclass(frozen=True)
class _IndependentMintSpec:
    """Mint spec: input I has Independent bridge in home scope.

    The merged-leases dict has an entry under ``x_token`` whose value
    is the snapshotted ``bridge.lease`` clone (concrete or pipelined).
    Mint time: clone that entry, return as the input lease.
    """

    x_token: LifetimeToken[Any]


@dataclasses.dataclass(frozen=True)
class _DependentLocalMintSpec:
    """Mint spec: input I has a :class:`DependentLocalBridge` in home scope.

    The merged-leases dict has entries for each token in
    ``required_tokens`` (the input bridge's ``leases`` keys), each
    cloned from the input bridge — and known to be concrete since
    Local bridges have strictly concrete inputs. Mint time: clone
    the relevant entries, run ``inner_converter(clones_subset, base.clone())``,
    return the resulting concrete ``Lease``.
    """

    inner_converter: DependentLocalBridgeConverter
    required_tokens: tuple[LifetimeToken[Any], ...]


@dataclasses.dataclass(frozen=True)
class _DependentPipelinedMintSpec:
    """Mint spec: input I has a :class:`DependentPipelinedBridge` in home scope.

    Same structure as :class:`_DependentLocalMintSpec` but the inner
    converter accepts possibly-pipelined inputs and returns an
    :class:`UnwrapForPipelining`.
    """

    inner_converter: DependentPipelinedBridgeConverter
    required_tokens: tuple[LifetimeToken[Any], ...]


_InputMintSpec = (
    _UseBaseSpec | _IndependentMintSpec | _DependentLocalMintSpec | _DependentPipelinedMintSpec
)


def _build_chain_bridge(
    *,
    home_scope: FxtrScope,
    wire_handle: IRHandle[Any],
    wire_key_token: LifetimeToken[Any],
    wire_key_lease: Lease[Any],
    input_tokens: Collection[LifetimeToken[Any]],
) -> DependentPipelinedBridge:
    """Build a caller-side chain :class:`DependentPipelinedBridge` for one ``dep_ret_ref`` entry.

    Snapshots ``home_scope``'s bridges for each non-base input token
    (cloning their stored leases, capturing their converters). The
    merged-leases dict is what gets stored as the bridge's ``leases``;
    the converter callable closes over the input mint specs and the
    wire handle.

    Always returns a :class:`DependentPipelinedBridge`: the converter
    dispatches a wire handle and returns the resulting future-lease
    pair as :class:`UnwrapForPipelining`.

    Takes ownership of ``wire_key_lease``. On any error path (e.g.
    a missing input bridge), discards everything taken so far and
    re-raises.

    Args:
        home_scope: The caller scope on which the bridge will be
            installed. Used at install time to look up bridges for
            non-base input tokens; inputs whose token equals
            ``home_scope``'s own main token use the converter's
            ``base`` lease at mint time instead of being snapshotted.
        wire_handle: The chain converter ``IRHandle`` from the wire.
            Token = ``wire_key_token``. Closed over.
        wire_key_token: The wire converter's token (= ``K_call``).
            Stored in the merged-leases dict alongside its lease so
            mint-time clones it for the dispatch's outer-lease arg.
        wire_key_lease: A ``Lease[K_call]`` (taken on entry; consumed).
        input_tokens: The set of tokens the wire converter expects
            leases for in its ``Call("convert", list[Lease])`` arg.
            Iteration order is irrelevant: the receiver maps the
            list back to tokens by ``lease.subject``.
    """
    home_main_token = home_scope.scope_token
    taken_key = wire_key_lease.take()
    merged_leases: dict[LifetimeToken[Any], Lease[Any] | UnwrapForPipelining[Lease[Any], Any]] = {
        wire_key_token: taken_key
    }
    input_specs: list[_InputMintSpec] = []

    def merge_concrete(tok: LifetimeToken[Any], source: Lease[Any]) -> None:
        """Insert a concrete-lease candidate at ``tok``, with concrete-wins precedence.

        - If ``tok`` is absent: clone ``source`` into the merged dict.
        - If existing entry is concrete: nothing to do (concrete already wins).
        - If existing entry is pipelined: discard its keep-alive and
          replace with a clone of ``source`` — concrete beats pipelined.
        """
        existing = merged_leases.get(tok)
        if existing is None:
            merged_leases[tok] = source.clone()
            return
        if not isinstance(existing, UnwrapForPipelining):
            return
        existing.lease.discard()
        merged_leases[tok] = source.clone()

    def merge_pipelined(
        tok: LifetimeToken[Any], source: UnwrapForPipelining[Lease[Any], Any]
    ) -> None:
        """Insert a pipelined candidate at ``tok``, with concrete-wins precedence.

        - If ``tok`` is absent: clone ``source`` into the merged dict.
        - If existing entry is anything (concrete or pipelined): keep
          it (concrete already wins; both-pipelined ties go to the
          first writer).
        """
        if tok not in merged_leases:
            merged_leases[tok] = UnwrapForPipelining(
                future=source.future, lease=source.lease.clone()
            )

    try:
        for x_token in input_tokens:
            if x_token is home_main_token:
                input_specs.append(_UseBaseSpec())
                continue
            entry = home_scope._cap_bridges.get(x_token)
            if entry is None:
                raise IRException(
                    message=(
                        "home scope has no bridge for input token "
                        f"{x_token!r}; the chain converter needs one."
                    ),
                    category="MissingInputBridge",
                )
            bridge = entry.bridge
            if isinstance(bridge, IndependentLocalBridge):
                merge_concrete(x_token, bridge.lease)
                input_specs.append(_IndependentMintSpec(x_token=x_token))
            elif isinstance(bridge, IndependentPipelinedBridge):
                merge_pipelined(x_token, bridge.pipelined)
                input_specs.append(_IndependentMintSpec(x_token=x_token))
            elif isinstance(bridge, DependentLocalBridge):
                # Local-output dependent bridge: every input is concrete
                # by the bridge's invariant; merge each into the dict.
                for tok, stored_lease in bridge.leases.items():
                    merge_concrete(tok, stored_lease)
                input_specs.append(
                    _DependentLocalMintSpec(
                        inner_converter=bridge.converter,
                        required_tokens=tuple(bridge.leases.keys()),
                    )
                )
            else:
                # DependentPipelinedBridge: each input may be concrete
                # or pipelined; merge per-entry.
                for tok, stored_value in bridge.leases.items():
                    if isinstance(stored_value, UnwrapForPipelining):
                        merge_pipelined(tok, stored_value)
                    else:
                        merge_concrete(tok, stored_value)
                input_specs.append(
                    _DependentPipelinedMintSpec(
                        inner_converter=bridge.converter,
                        required_tokens=tuple(bridge.leases.keys()),
                    )
                )
    except BaseException:
        for v in merged_leases.values():
            if isinstance(v, UnwrapForPipelining):
                v.lease.discard()
            else:
                v.discard()
        raise

    # The converter closure: receives the merged-dict clones + base,
    # walks input_specs to produce input leases for the wire dispatch,
    # then dispatches and returns UnwrapForPipelining.
    specs_tuple = tuple(input_specs)

    def chain_converter(
        clones: dict[LifetimeToken[Any], Lease[Any] | UnwrapForPipelining[Lease[Any], Any]],
        base: Lease[Any],
    ) -> UnwrapForPipelining[Lease[Any], Any]:
        # Build a fresh K-clone + input lease sequence by cloning out of
        # ``clones`` / ``base``; the originals get discarded in ``finally``
        # below, but the new clones survive for the dispatch.
        k_for_dispatch: Lease[Any] | None = None
        input_leases: list[Lease[Any] | UnwrapForPipelining[Lease[Any], Any]] = []
        try:
            k_source = clones[wire_key_token]
            if isinstance(k_source, UnwrapForPipelining):
                # K_call is the dispatch scope's private ULL — its
                # snapshot is always concrete. A pipelined K would
                # imply a previous lift over a chain bridge for the
                # same ULL token, which our wire shape never produces.
                raise IRException(
                    message="chain converter received a pipelined K_call clone (unexpected)",
                    category="InternalError",
                )
            k_for_dispatch = k_source.clone()
            for spec in specs_tuple:
                if isinstance(spec, _UseBaseSpec):
                    input_leases.append(base.clone())
                elif isinstance(spec, _IndependentMintSpec):
                    src = clones[spec.x_token]
                    if isinstance(src, UnwrapForPipelining):
                        input_leases.append(
                            UnwrapForPipelining(future=src.future, lease=src.lease.clone())
                        )
                    else:
                        input_leases.append(src.clone())
                elif isinstance(spec, _DependentLocalMintSpec):
                    # Local converter wants strictly concrete inputs.
                    # The concrete-wins merge policy guarantees that
                    # every ``required_token`` (sourced from a
                    # ``DependentLocalBridge.leases.keys()``) maps to
                    # a concrete merged-dict entry: the bridge
                    # contributed a concrete value at that slot, which
                    # either wrote it or (per ``merge_concrete``)
                    # displaced any earlier pipelined entry.
                    inner_clones_local: dict[LifetimeToken[Any], Lease[Any]] = {}
                    for tok in spec.required_tokens:
                        v = clones[tok]
                        assert isinstance(v, Lease), (
                            "merge_concrete invariant violated: "
                            f"token {tok!r} resolved to pipelined "
                            "but a DependentLocalBridge contributed concrete."
                        )
                        inner_clones_local[tok] = v.clone()
                    input_leases.append(spec.inner_converter(inner_clones_local, base.clone()))
                else:
                    inner_clones_pipelined: dict[
                        LifetimeToken[Any], Lease[Any] | UnwrapForPipelining[Lease[Any], Any]
                    ] = {}
                    for tok in spec.required_tokens:
                        src_v = clones[tok]
                        if isinstance(src_v, UnwrapForPipelining):
                            inner_clones_pipelined[tok] = UnwrapForPipelining(
                                future=src_v.future, lease=src_v.lease.clone()
                            )
                        else:
                            inner_clones_pipelined[tok] = src_v.clone()
                    input_leases.append(spec.inner_converter(inner_clones_pipelined, base.clone()))
        except BaseException:
            # On the error path, don't leak the partially-built
            # dispatch-side clones either.
            if k_for_dispatch is not None:
                k_for_dispatch.discard()
            for built in input_leases:
                if isinstance(built, UnwrapForPipelining):
                    built.lease.discard()
                else:
                    built.discard()
            raise
        finally:
            # Discard the originals we cloned from + base. The fresh
            # clones above (``k_for_dispatch``, ``input_leases`` entries)
            # are independent of these and survive for the dispatch.
            for v in clones.values():
                if isinstance(v, UnwrapForPipelining):
                    v.lease.discard()
                else:
                    v.discard()
            base.discard()
        # Dispatch the wire handle with ``k_for_dispatch`` outer + the
        # input lease sequence. Result is ``(future, future_lease)``
        # which we wrap as UnwrapForPipelining.
        assert k_for_dispatch is not None
        fut, fut_lease = wire_handle.dispatch(k_for_dispatch, Call("convert", input_leases))
        return UnwrapForPipelining(fut, fut_lease)

    return DependentPipelinedBridge(leases=merged_leases, converter=chain_converter)


def _index_return_packet(
    new_indep_leases: Any,
    new_dep_leases: Any,
) -> tuple[
    dict[LifetimeToken[Any], Lease[Any]],
    dict[LifetimeToken[Any], Mapping[str, Any]],
]:
    """Validate and index the wire packet's bridge slots by target token.

    Returns ``(indep_by_token, dep_by_token)``. Both dicts own their
    referenced wire-side leases (the indep entries themselves; the
    dep entries' ``key_lease`` field). Caller is responsible for
    discarding any unconsumed entries.

    Raises :class:`IRException` ``MalformedReturn`` on shape errors;
    on the raise path, no resources are owned (we discard everything
    indexed so far before re-raising).
    """
    if not isinstance(new_indep_leases, list):
        raise IRException(
            message=f"`new_indep_leases` must be a list, got {type(new_indep_leases).__name__}",
            category="MalformedReturn",
        )
    if not isinstance(new_dep_leases, list):
        raise IRException(
            message=f"`new_dep_leases` must be a list, got {type(new_dep_leases).__name__}",
            category="MalformedReturn",
        )
    indep_by_token: dict[LifetimeToken[Any], Lease[Any]] = {}
    dep_by_token: dict[LifetimeToken[Any], Mapping[str, Any]] = {}
    try:
        for entry in new_indep_leases:
            if not isinstance(entry, Lease):
                raise IRException(
                    message=f"new_indep_leases entry must be Lease, got {type(entry).__name__}",
                    category="MalformedReturn",
                )
            if entry.subject in indep_by_token:
                raise IRException(
                    message=f"duplicate new_indep_leases entry for token {entry.subject!r}",
                    category="MalformedReturn",
                )
            indep_by_token[entry.subject] = entry
        for entry in new_dep_leases:
            if not isinstance(entry, Mapping):
                raise IRException(
                    message=f"new_dep_leases entry must be a mapping, got {type(entry).__name__}",
                    category="MalformedReturn",
                )
            token = entry.get(_DEP_REF_TOKEN)
            if not isinstance(token, LifetimeToken):
                raise IRException(
                    message=(
                        "new_dep_leases entry token must be a LifetimeToken, got"
                        f" {type(token).__name__}"
                    ),
                    category="MalformedReturn",
                )
            if token in dep_by_token:
                raise IRException(
                    message=f"duplicate new_dep_leases entry for token {token!r}",
                    category="MalformedReturn",
                )
            dep_by_token[token] = entry
    except BaseException:
        _discard_wire_entries(indep_by_token, dep_by_token)
        raise
    return indep_by_token, dep_by_token


def _validate_dep_entry(
    dep: Mapping[str, Any],
) -> tuple[Lease[Any], IRHandle[Any], list[LifetimeToken[Any]]]:
    """Validate a ``new_dep_leases`` entry's shape and extract its fields.

    Returns ``(key_lease, converter, input_tokens)``. Does *not* take
    ownership of the lease — the caller decides whether to clone or
    consume. On validation failure, raises :class:`IRException`
    without touching the lease.
    """
    key_lease = dep.get(_DEP_REF_KEY_LEASE)
    if not isinstance(key_lease, Lease):
        raise IRException(
            message="new_dep_leases entry key_lease must be a Lease",
            category="MalformedReturn",
        )
    converter = dep.get(_DEP_REF_CONVERTER)
    if not isinstance(converter, IRHandle):
        raise IRException(
            message="new_dep_leases entry converter must be an IRHandle",
            category="MalformedReturn",
        )
    input_tokens = dep.get(_DEP_REF_INPUT_TOKENS)
    if not isinstance(input_tokens, list):
        raise IRException(
            message="new_dep_leases entry input_tokens must be a list",
            category="MalformedReturn",
        )
    for t in input_tokens:
        if not isinstance(t, LifetimeToken):
            raise IRException(
                message="input_tokens entries must be LifetimeToken",
                category="MalformedReturn",
            )
    return key_lease, converter, input_tokens


def _discard_dep_entry(dep: Mapping[str, Any]) -> None:
    """Discard the key_lease of a ``new_dep_leases`` entry, if present."""
    kl = dep.get(_DEP_REF_KEY_LEASE)
    if isinstance(kl, Lease):
        kl.discard()


def _discard_wire_entries(
    indep_by_token: Mapping[LifetimeToken[Any], Lease[Any]],
    dep_by_token: Mapping[LifetimeToken[Any], Mapping[str, Any]],
) -> None:
    """Discard every still-owned wire entry. Used on error paths and the
    final unused-entry sweep."""
    for v in indep_by_token.values():
        v.discard()
    for entry in dep_by_token.values():
        _discard_dep_entry(entry)


def _install_return_arena(
    *,
    home_scope: FxtrScope,
    fresh_bridges: Mapping[LifetimeToken[Any], LocalOrPipelinedBridge],
    arena_handle: IRHandle[Any],
    tracked_ids: Collection[FxtrID],
    initial_precomp: Sequence[Mapping[str, IRValue]],
) -> None:
    """Install the wire return arena into ``home_scope`` (pre-decode pass).

    Brings every id in ``tracked_ids`` into ``home_scope``'s main arena
    via :func:`_install_arena_with_bridge`, after first selecting an
    appropriate bridge for the arena's token by priority:

    - Token in ``fresh_bridges``: snapshot.
    - Token has an existing entry on ``home_scope``: snapshot from
      the entry's bridge.
    - Token *is* ``home_scope``'s own main token and neither of the
      above: passthrough (no bridge — the install path uses
      :meth:`CapIRArena.direct`).
    - Otherwise: error.

    The pre-populated ``initial_precomp`` is what makes
    :meth:`FxtrScope.embed_as_provenance_parent` work for prov-bearing
    FxtrIDs in the result body during the subsequent decode.
    """
    arena_token = arena_handle.token
    snapshot: LocalOrPipelinedBridge | None
    fresh = fresh_bridges.get(arena_token)
    if fresh is not None:
        snapshot = fresh.clone()
    else:
        existing = home_scope._cap_bridges.get(arena_token)
        if existing is not None:
            snapshot = existing.bridge.clone()
        elif arena_token is home_scope.scope_token:
            snapshot = None
        else:
            raise IRException(
                message=(
                    f"return packet has fxtr_arena (token={arena_token!r}) "
                    "but no bridge source: not in new_*_leases, not on "
                    "home_scope, not home_main."
                ),
                category="MalformedReturn",
            )
    try:
        _install_arena_with_bridge(
            home_scope=home_scope,
            arena_handle=arena_handle,
            tracked_ids=tracked_ids,
            initial_precomp=initial_precomp,
            bridge=snapshot,
        )
    except BaseException:
        if snapshot is not None:
            snapshot.discard_at_rest()
        raise


def _install_return_handles(
    *,
    home_scope: FxtrScope,
    fresh_bridges: Mapping[LifetimeToken[Any], LocalOrPipelinedBridge],
    result_keys: Collection[ServiceKey],
    arena_token: LifetimeToken[Any] | None,
) -> None:
    """Install caller-side cap-bridges for returned ``ServiceRef`` handles (post-decode pass).

    Validates that every entry in ``fresh_bridges`` is actually
    referenced by a handle or the arena — leftovers indicate a
    malformed return packet. Then per unique handle token, snapshots
    the bridge from ``fresh_bridges`` / an existing local entry / a
    parent-walk lift, and installs the handles.
    """
    keys_by_token: dict[LifetimeToken[Any], list[ServiceKey]] = {}
    for k in result_keys:
        keys_by_token.setdefault(k.handle.token, []).append(k)
    referenced_tokens: set[LifetimeToken[Any]] = set(keys_by_token) | (
        {arena_token} if arena_token is not None else set()
    )

    unused = set(fresh_bridges) - referenced_tokens
    if unused:
        raise IRException(
            message=(
                "return packet provided unused new_*_leases entries for "
                f"tokens {sorted(repr(t) for t in unused)}"
            ),
            category="MalformedReturn",
        )

    for token, keys in keys_by_token.items():
        _install_handles_for_token(
            home_scope=home_scope,
            token=token,
            keys=keys,
            fresh_bridges=fresh_bridges,
        )


def _build_fresh_bridges(
    *,
    home_scope: FxtrScope,
    indep_by_token: dict[LifetimeToken[Any], Lease[Any]],
    dep_by_token: dict[LifetimeToken[Any], Mapping[str, Any]],
    fresh_bridges: dict[LifetimeToken[Any], LocalOrPipelinedBridge],
) -> None:
    """Drain ``indep_by_token`` and ``dep_by_token`` into ``fresh_bridges``.

    For each ``new_indep_leases`` entry: builds an
    :class:`IndependentLocalBridge` taking ownership of the lease.
    For each ``new_dep_leases`` entry: validates the entry shape and
    builds a chain :class:`DependentPipelinedBridge` via
    :func:`_build_chain_bridge`.

    On success, the input dicts are empty. On any error, every entry
    that has been popped but not yet placed in ``fresh_bridges`` has
    been discarded; remaining entries are left in the input dicts for
    the caller's cleanup to handle.
    """
    overlap = set(indep_by_token) & set(dep_by_token)
    if overlap:
        raise IRException(
            message=(
                f"tokens {sorted(repr(t) for t in overlap)} appear in both "
                "new_indep_leases and new_dep_leases"
            ),
            category="MalformedReturn",
        )
    while indep_by_token:
        token, lease = indep_by_token.popitem()
        # ``Lease.take`` is a state-only transition — no failure mode
        # to clean up around.
        fresh_bridges[token] = IndependentLocalBridge(lease=lease.take())
    while dep_by_token:
        token, dep_entry = dep_by_token.popitem()
        try:
            wire_kl, wire_conv, wire_input_tokens = _validate_dep_entry(dep_entry)
        except BaseException:
            _discard_dep_entry(dep_entry)
            raise
        # ``_build_chain_bridge`` takes ownership of ``wire_kl`` and
        # cleans up its own taken inputs on failure.
        fresh_bridges[token] = _build_chain_bridge(
            home_scope=home_scope,
            wire_handle=wire_conv,
            wire_key_token=wire_kl.subject,
            wire_key_lease=wire_kl,
            input_tokens=wire_input_tokens,
        )


def _install_handles_for_token(
    *,
    home_scope: FxtrScope,
    token: LifetimeToken[Any],
    keys: Collection[ServiceKey],
    fresh_bridges: Mapping[LifetimeToken[Any], LocalOrPipelinedBridge],
) -> None:
    """Register ``keys`` against the right bridge source for ``token``.

    Per token, three source paths — all expressed through
    :meth:`FxtrScope.attach_bridged_lowlevel_cap`, no direct
    manipulation of scope internals:

    - **Token already in ``home_scope._cap_bridges``** (service or
      ancestor entry): register each key with ``bridge=None``. The
      wire may have over-provided a fresh bridge for this token; the
      caller's cleanup discards it.
    - **Token in ``fresh_bridges``**: install the fresh bridge with
      the first key, then register subsequent keys with
      ``bridge=None`` against the now-existing local entry.
    - **Otherwise**: parent-walk lift via
      ``attach_bridged_lowlevel_cap(bridge=None)``, which calls
      :meth:`FxtrScope._lift_bridge_for_service_key` internally. Errors if
      not reachable from any ancestor.
    """
    if not keys:
        return
    existing = home_scope._cap_bridges.get(token)
    fresh = fresh_bridges.get(token)
    keys_iter = iter(keys)
    first_key = next(keys_iter)
    if existing is None and fresh is not None:
        # Wire provided a fresh bridge and home has no entry for the
        # token: install via attach_bridged_lowlevel_cap with the
        # cloned fresh bridge.
        home_scope.attach_bridged_lowlevel_cap(
            first_key.handle,
            UnknownService,
            first_key.type_id,
            bridge=fresh.clone(),
        )
    elif existing is not None:
        # Home already has a local entry for the token (either
        # ancestor-installed or from a previous attach). Register the
        # key on top with bridge=None.
        home_scope.attach_bridged_lowlevel_cap(
            first_key.handle, UnknownService, first_key.type_id, bridge=None
        )
    else:
        # No entry and no fresh bridge: parent-walk lift. The lift
        # both verifies reachability AND installs a co-located entry on
        # home, so subsequent keys can register with bridge=None.
        if home_scope._lift_bridge_for_service_key(first_key) is None:
            raise _missing_bridge_error(token, first_key.handle)
    # Subsequent keys: bridge=None against the now-existing local entry.
    for key in keys_iter:
        home_scope.attach_bridged_lowlevel_cap(key.handle, UnknownService, key.type_id, bridge=None)


def _missing_bridge_error(token: LifetimeToken[Any], handle: IRHandle[Any]) -> IRException:
    return IRException(
        message=(
            f"return references handle {handle!r} on token {token!r} "
            "that has no caller-side bridge and was not provided "
            "in new_indep_leases / new_dep_leases."
        ),
        category="MalformedReturn",
    )


def _install_arena_with_bridge(
    *,
    home_scope: FxtrScope,
    arena_handle: IRHandle[Any],
    tracked_ids: Collection[FxtrID],
    initial_precomp: Sequence[Mapping[str, IRValue]],
    bridge: LocalOrPipelinedBridge | None,
) -> None:
    """Bridge ``arena_handle`` into ``home_scope``'s main arena via
    ``lazily_attach_from_arena``.

    ``tracked_ids`` is the wire-supplied complete liveset of the remote
    arena — passed both as the :class:`CapIRArena`'s ``initial_tracked``
    (so refcounts are seeded for every shipped id) and as the node-id
    list to :meth:`lazily_attach_from_arena` (so all of them get
    attached into ``home_scope``'s main arena). ``initial_precomp`` is
    the closed precomp closure over ``tracked_ids``; pre-populating it
    is what makes the resulting arena answer sync provenance reads
    without a wire round-trip.

    The shape depends on ``bridge``:

    - ``bridge is None``: ``arena_handle.token`` is ``home_main`` and
      no conversion is needed. Uses :meth:`CapIRArena.direct` plus a
      passthrough store-converter.
    - ``bridge`` is one of the four :data:`LocalOrPipelinedBridge`
      variants: wraps the arena in :meth:`CapIRArena.with_converter`
      with a local ``D = derive({home_main})``.

    Takes ownership of ``bridge``'s at-rest leases — the
    :class:`CapIRArena` will discard them on ``close()``. Raises
    :class:`IRException` on the validation path; the caller is
    responsible for discarding ``bridge`` on the raise path.
    """
    arena_token = arena_handle.token
    home_main_token = home_scope.scope_token

    if bridge is None:
        # arena_token == home_main; trivial pass-through. The store needs
        # a Lease[arena_token] = Lease[home_main] to load through the
        # CapIRArena — an IndependentLocalBridge holding home_main_lease
        # is the case-1 shape (Y outlives X — they're literally the
        # same lifetime).
        assert arena_token is home_main_token
        cap_ir: CapIRArena[Any] = CapIRArena.direct(
            token=arena_token,
            remote=arena_handle,
            initial_tracked=list(tracked_ids),
            initial_precomp=initial_precomp,
        )

        try:
            home_scope._main_arena.lazily_attach_from_arena(
                home_scope._current_auth_lease().clone(),
                list(tracked_ids),
                cap_ir,
                IndependentLocalBridge(lease=home_scope._current_auth_lease().clone()),
            )
        finally:
            # ``lazily_attach_from_arena`` forks a sibling
            # (``owned_source_arena``) that the store retains; the
            # original ``cap_ir`` is no longer referenced and we close
            # it so any owned state (no leases in direct mode, but
            # cleared liveset / caches all the same) doesn't linger.
            cap_ir.close()
        return

    # Bridge mode: wrap in CapIRArena.with_converter via a local
    # D = derive({home_main}). The bridge handles all at-rest state;
    # D just provides the "we're alive while home_main is" envelope
    # the foreign-arena row needs.
    derived_d: DerivedLocalLifetime[Any] = DerivedLocalLifetime(
        "ret_arena_local_D",
        inputs={home_main_token},
    )

    # Local converter wraps base = Lease[home_main] into Lease[D] via
    # exchange_forward. Packaged as an empty-input DependentLocalBridge
    # (case 2 from the perspective of the home_main → D relationship).
    def local_dep_converter(
        inputs: dict[LifetimeToken[Any], Lease[Any]], base: Lease[Any]
    ) -> Lease[Any]:
        for v in inputs.values():
            v.discard()
        return derived_d.exchange_forward({home_main_token: base})

    store_bridge = DependentLocalBridge(leases={}, converter=local_dep_converter)

    try:
        cap_ir = CapIRArena.with_converter(
            derived=derived_d,
            base_token=home_main_token,
            remote=arena_handle,
            bridge=bridge,
            initial_tracked=list(tracked_ids),
            initial_precomp=initial_precomp,
        )
    except BaseException:
        derived_d.revoke("CapIRArena setup failed")
        store_bridge.discard_at_rest()
        # Caller owns ``bridge`` and will discard it on the raise path.
        raise

    try:
        home_scope._main_arena.lazily_attach_from_arena(
            home_scope._current_auth_lease().clone(),
            list(tracked_ids),
            cap_ir,
            store_bridge,
        )
    except BaseException:
        cap_ir.close()
        derived_d.revoke("lazily_attach_from_arena failed")
        raise
    # ``lazily_attach_from_arena`` forks a sibling
    # (``owned_source_arena``) via ``cap_ir.copy_to_new_arena`` and
    # retains *that* on each adopted node's ``inheritance_claims``.
    # The original ``cap_ir`` is no longer referenced by the store;
    # close it now so its ``bridge_state`` (which owns the chain
    # bridge's at-rest K_call clone + input-bridge snapshots) is
    # discarded. The sibling's *independent* snapshot lives until
    # ``_on_token_end`` closes it.
    cap_ir.close()


# =============================================================================
# Server-side: bridging incoming arena / ServiceRef args into the call scope
# =============================================================================


def _bridge_input_arena(
    *,
    input_scope: FxtrScope,
    arena_handle: IRHandle[Any],
    tracked_ids: Collection[FxtrID],
    initial_precomp: Sequence[Mapping[str, IRValue]],
) -> None:
    """Wrap an incoming arena packet with ``CapIRArena`` and bridge it into ``input_scope``.

    The arena's token is one of ``input_scope._derived``'s inputs (the
    sender's home-scope token). Single ``lazily_attach_from_arena``
    call brings every ID in ``tracked_ids`` into ``input_scope``'s main
    arena via the case-3 converter built from ``derived.subtake``.

    Args:
        input_scope: The per-call input scope (must be a derived scope).
        arena_handle: Sender-side ``IRHandle`` from the packet. Its
            token must be one of ``input_scope._derived.inputs``.
        tracked_ids: The arena's complete liveset (from the wire
            packet's ``tracked_ids`` field). Seeds the
            :class:`CapIRArena`'s refcounted liveset and is the set of
            ids attached into ``input_scope``'s main arena.
        initial_precomp: Closed precomp closure over `tracked_ids`
            (from the wire packet's ``initial_precomp`` field).
            Pre-populates the sync provenance caches so the bridged
            arena answers ``unwrap_provenance`` etc. without a wire
            round-trip — required for prov-bearing FxtrIDs in args /
            return to be decodable via
            ``input_scope.embed_as_provenance_parent``.
    """
    arena_token = arena_handle.token
    derived = _validate_case3_inputs(input_scope, arena_token)

    # Case-3 attach (X is derived from Y): empty-input
    # DependentLocalBridge whose converter runs exchange_reverse on the
    # base lease to surface the via on Y.
    def case3_converter(
        inputs: dict[LifetimeToken[Any], Lease[Any]], base: Lease[Any]
    ) -> Lease[Any]:
        for v in inputs.values():
            v.discard()
        return derived.subtake(base, arena_token)

    cap_ir = CapIRArena.direct(
        token=arena_token,
        remote=arena_handle,
        initial_tracked=tracked_ids,
        initial_precomp=initial_precomp,
    )

    try:
        input_scope._main_arena.lazily_attach_from_arena(
            input_scope._current_auth_lease().clone(),
            list(tracked_ids),
            cap_ir,
            DependentLocalBridge(leases={}, converter=case3_converter),
        )
    finally:
        # ``lazily_attach_from_arena`` forks a sibling and retains it on
        # each adopted node; ``cap_ir`` is no longer referenced here, so
        # close it to release its share of the shared remote state's
        # refcounts (the fork bumped them, so the shared state stays
        # alive through the sibling).
        cap_ir.close()


def _bridge_input_service_ref(
    *,
    input_scope: FxtrScope,
    key: ServiceKey,
) -> None:
    """Register an input-arg ``ServiceKey`` on ``input_scope``.

    ``key.handle.token`` must be one of ``input_scope._derived``'s
    inputs (added by ``_take_input_leases`` from ``packet["leases"]``).
    Input-scope entry (``lowlevel_start_lifespanless`` with these tokens
    as ``extra_derived_leases``) already installed an
    ``is_ancestor_or_arena_owner=True`` :class:`DependentLocalBridge`
    ancestor entry for every derived-input token (the case-3
    ``derived.subtake`` shape) — so we just need to register the
    key on the existing entry. The ``bridge=None`` form of
    :meth:`FxtrScope.attach_bridged_lowlevel_cap` does exactly that
    and errors if the entry is missing.

    The ``key.type_id`` arrives via the bridged-in input arena (the
    encoder emitted it as an ``FXTR_ID_TAG`` leaf collected into
    ``arg_ids``, and ``_bridge_input_arena`` lazy-attached the
    arena's contents into ``input_scope``'s main arena), so the
    ``attach_bridged_lowlevel_cap`` precondition
    ``is_tracked(type_id)`` is satisfied by the time we reach here.

    Mounting via the ancestor entry (Dependent / case-3) — rather
    than constructing a fresh Independent bridge — is what makes the
    arg-handle's lifetime cycle cleanly when this ref is later
    returned through the chain converter (see §1.6 of part6_plan):
    each lookup mints a fresh ``Lease[handle.token]`` via
    ``derived.subtake``, never persistently storing a
    ``Lease[handle.token]`` whose vias would pin the input scope.

    Args:
        input_scope: The per-call input scope.
        key: The :class:`ServiceKey` decoded from the packet's
            ``service_ref`` leaf.
    """
    # Validate up-front so a missing/foreign-input handle errors with a
    # clearer message than ``attach_bridged_lowlevel_cap``'s generic
    # "no pre-existing entry" path.
    _validate_case3_inputs(input_scope, key.handle.token)
    input_scope.attach_bridged_lowlevel_cap(key.handle, UnknownService, key.type_id, bridge=None)


# =============================================================================
# Client-side: invoke_service_method (the proxy-side entry point)
# =============================================================================


async def invoke_service_method(
    ctx: ServiceProxyContext,
    method_name: str,
    *args: FxtrServiceData,
    **kwargs: FxtrServiceData,
) -> FxtrServiceData:
    """Invoke ``method_name`` on the service handle in ``ctx``, returning the decoded result.

    This is the proxy-side counterpart to
    :class:`CommonServiceImplHandle`'s server-side dispatch. Handles
    every step of the call:

    1. Validate args / kwargs against :data:`FxtrServiceData`.
    2. Encode + collect referenced FxtrIDs and ServiceRef handles.
    3. Fork an input arena (if any FxtrIDs) via
       ``ctx.fork_arena_handle_for_ids``.
    4. Mint per-handle leases (via ``ctx.mint_lease_for_handle``);
       dedup by subject token.
    5. Build the ``IRTagged("fxtr_call", {…})`` packet and dispatch
       on ``ctx.proxy_handle``.
    6. Validate the return packet shape; decode ``result``; install
       caller-side bridges for returned tokens via
       :func:`_install_returned_resources`.
    7. Return the decoded result as :data:`FxtrServiceData`.

    Used directly by :class:`UnknownServiceProxy.__getattr__`. Typed
    proxy layers wrap with per-method conversion before / after this
    call.

    Args:
        ctx: The :class:`ServiceProxyContext` produced by
            :meth:`FxtrScope.resolve_service`.
        method_name: The user-facing method name. Looked up against
            the impl's ``_handle_invoke`` on the receiving side.
        *args: Positional :data:`FxtrServiceData` args.
        **kwargs: Keyword :data:`FxtrServiceData` args.

    Returns:
        The decoded :data:`FxtrServiceData` return value. The wire
        round-trip normalizes tuples to lists and immutable mappings
        to dicts.

    Raises:
        TypeError: An argument was not :data:`FxtrServiceData`-shaped.
        IRException: The receiver raised (unknown method, malformed
            packet, user method exception wrapped with
            ``category=type(exc).__name__``, etc.).
        RuntimeError: The receiver returned an unexpected packet
            shape (should never happen against a conforming impl).
    """
    # Validate + encode args/kwargs in one walk. Encoded values
    # have FxtrIDs/EntityRefs/ServiceRefs replaced by their tagged
    # forms; the sets we pass in collect every referenced ID and
    # ServiceRef handle so the lease / arena setup below can use
    # them directly.
    for i, v in enumerate(args):
        validate_fxtr_service_data(v, where=f"args[{i}]")
    for k, v in kwargs.items():
        validate_fxtr_service_data(v, where=f"kwargs[{k!r}]")

    arg_ids: set[FxtrID] = set()
    arg_service_keys: set[ServiceKey] = set()
    encoded_args: list[IRValue] = [
        _encode_fxtr_service_data(
            v,
            fxtr_ids=arg_ids,
            service_keys=arg_service_keys,
            where=f"args[{i}]",
        )
        for i, v in enumerate(args)
    ]
    encoded_kwargs: dict[str, IRValue] = {
        k: _encode_fxtr_service_data(
            v,
            fxtr_ids=arg_ids,
            service_keys=arg_service_keys,
            where=f"kwargs[{k!r}]",
        )
        for k, v in kwargs.items()
    }

    # Build the leases list and (optional) input arena via the
    # context. Pipelined leases (e.g. from a chain-converter
    # bridge) ride through ``packet["leases"]`` as IRCallArg —
    # the dispatch path's `_unwrap_request_futures` resolves
    # ``UnwrapForPipelining`` before the receiver sees the packet.
    fxtr_arena_packet: dict[str, IRValue] | None = None
    # Leases inside the packet may be either concrete or pipelined;
    # both shapes are valid IRCallArgs.
    leases: list[Lease[Any] | UnwrapForPipelining[Lease[Any], Any]] = []
    seen_lease_subjects: set[LifetimeToken[Any]] = set()

    try:
        # Fork an input arena (if any FxtrIDs) and ship the corresponding
        # lease + handle + tracked_ids + initial_precomp.
        if arg_ids:
            arena_handle, arena_lease, arena_precomp = ctx.fork_arena_handle_for_ids(arg_ids)
            fxtr_arena_packet = _build_fxtr_arena_packet(
                handle=arena_handle,
                tracked_ids=arg_ids,
                initial_precomp=arena_precomp,
            )
            leases.append(arena_lease)
            seen_lease_subjects.add(arena_lease.subject)

        # For each ServiceRef arg, mint a lease via the home scope's
        # bridge for that key. Dedup by subject token: if multiple
        # args share a token, we only add one lease (the receiver
        # rebuilds the {subject: lease} map by walking the list).
        for key in arg_service_keys:
            if key.handle.token in seen_lease_subjects:
                continue
            minted = ctx.mint_lease_for_key(key)
            leases.append(minted)
            seen_lease_subjects.add(key.handle.token)
    except BaseException:
        # Discard everything we minted so far. Each entry may be
        # concrete or pipelined; both forms expose discard() on the
        # underlying lease.
        for entry in leases:
            if isinstance(entry, UnwrapForPipelining):
                entry.lease.discard()
            else:
                entry.discard()
        raise

    packet: IRTagged[str, Mapping[str, IRCallArg]] = IRTagged(
        _FXTR_CALL_TAG,
        {
            "args": encoded_args,
            "kwargs": encoded_kwargs,
            "fxtr_arena": fxtr_arena_packet,
            "leases": leases,
        },
    )
    result = await ctx.proxy_handle.request(
        ctx.mint_lease_for_key(ctx.proxy_key),
        Call(method_name, packet),
    )
    if not isinstance(result, IRTagged) or result.tag != _FXTR_RETURN_TAG:
        raise RuntimeError(
            f"Expected IRTagged({_FXTR_RETURN_TAG!r}, ...) return packet; "
            f"got {type(result).__name__}"
        )
    body = result.value
    if not isinstance(body, Mapping):
        raise RuntimeError(f"Return packet body must be a mapping; got {type(body).__name__}")

    # Parse the wire fxtr_arena packet.
    raw_return_arena = body.get("fxtr_arena")
    return_arena_handle: IRHandle[Any] | None
    return_arena_tracked_ids: list[FxtrID]
    return_initial_precomp: Sequence[Mapping[str, IRValue]]
    if raw_return_arena is None:
        return_arena_handle = None
        return_arena_tracked_ids = []
        return_initial_precomp = []
    else:
        return_arena_handle, return_arena_tracked_ids, return_initial_precomp = (
            _parse_fxtr_arena_packet(raw_return_arena, where="return")
        )

    # Index the wire packet's lease / converter slots and build the
    # per-token fresh-bridge map. The same map is consumed by the
    # arena install (pre-decode) and the handle install (post-decode).
    indep_by_token, dep_by_token = _index_return_packet(
        body.get("new_indep_leases", []),
        body.get("new_dep_leases", []),
    )

    fresh_bridges: dict[LifetimeToken[Any], LocalOrPipelinedBridge] = {}
    try:
        _build_fresh_bridges(
            home_scope=ctx.home_scope,
            indep_by_token=indep_by_token,
            dep_by_token=dep_by_token,
            fresh_bridges=fresh_bridges,
        )

        # Phase A: bridge the return arena into home_scope (must run
        # before the decoder because prov-bearing IDs in the result
        # body need their prov parent tracked in home_scope).
        if return_arena_handle is not None and return_arena_tracked_ids:
            _install_return_arena(
                home_scope=ctx.home_scope,
                fresh_bridges=fresh_bridges,
                arena_handle=return_arena_handle,
                tracked_ids=return_arena_tracked_ids,
                initial_precomp=return_initial_precomp,
            )

        # Phase B: decode the result body using home_scope as the
        # unwrap source. Arg-side IDs (already lifted into home_scope
        # by the proxy's send path) and any IDs from the just-bridged
        # return arena both resolve through `embed_as_provenance_parent`.
        result_ids: set[FxtrID] = set()
        result_service_keys: set[ServiceKey] = set()
        decoded_result = _decode_fxtr_service_data(
            body["result"],
            fxtr_ids=result_ids,
            service_keys=result_service_keys,
            scope=ctx.home_scope,
            where="return",
        )

        # Phase C: install caller-side bridges for every returned
        # ``ServiceRef`` handle, plus validate that no fresh-bridge
        # entry is unreferenced.
        arena_token = (
            return_arena_handle.token
            if return_arena_handle is not None and return_arena_tracked_ids
            else None
        )
        _install_return_handles(
            home_scope=ctx.home_scope,
            fresh_bridges=fresh_bridges,
            result_keys=result_service_keys,
            arena_token=arena_token,
        )
    finally:
        for bridge in fresh_bridges.values():
            bridge.discard_at_rest()
        _discard_wire_entries(indep_by_token, dep_by_token)
    return decoded_result
