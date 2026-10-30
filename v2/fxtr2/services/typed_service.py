"""Typed-service layer: ``@service_protocol`` decorator + ``TypedServiceImpl``.

Builds on :mod:`fxtr2.services.service_cap_common` (wire shape +
dispatch) and :mod:`fxtr2.services.service_dialect` (mashumaro
strategies + codec cache) to provide a typed service protocol /
implementation pair:

- ``@service_protocol`` decorates a ``Protocol`` class. Walks its
  ``async def`` methods, dynamically generates a per-method args /
  return wrapper-dataclass pair (parameterized by the Protocol's
  TypeVars), eagerly builds codecs against the Protocol's dialect to
  surface unsupported annotations early, and registers a resolver for
  the Protocol type with the service-resolver registry.
- :class:`TypedServiceImpl` is a :class:`CommonServiceImpl` subclass.
  Subclasses tag methods with ``@service_method`` (same convention
  as :class:`SimpleAsyncServiceImpl`) and supply
  :meth:`_target_type_annotation` + :meth:`_service_type_id`. At
  mount time, codecs are built against the impl's
  :meth:`_own_full_type_for_serialization` form (default:
  ``type(self)``); generic impls override this to return a
  parameterized form (e.g. ``MyImpl[int]``) so the impl's
  TypeVar-bearing annotations can be substituted.

**Wire shape.** The wrapper dataclass exists purely so mashumaro can
substitute TypeVars; its field names mirror the user's parameter names
one-to-one (including the user-spelled ``*args`` / ``**kwargs`` names —
no reserved names). The proxy encodes the bound call into a flat
field-keyed dict, strips ``USE_REMOTE_DEFAULT`` entries, and hands the
result to a fresh :class:`inspect.BoundArguments` whose
``.args`` / ``.kwargs`` properties produce the natural positional /
keyword wire split for free. The impl side mirrors: ``inspect_sig.bind``
the wire's args/kwargs into a name-keyed dict, decode via the wrapper,
strip ``USE_REMOTE_DEFAULT``, and call ``method(*new_bound.args,
**new_bound.kwargs)``. Cross-side parameter rename tolerance for
variadics is automatic — the variadic content travels as bare wire
positional/keyword args; no name appears on the wire.

**``USE_REMOTE_DEFAULT``.** A top-level named-parameter value of
``USE_REMOTE_DEFAULT`` is the sentinel for "the proxy didn't ship a
value here; let the impl's own default fire." The dialect's
``pass_through`` strategy keeps the sentinel through encode unchanged;
the proxy strips entries equal to it before the
:class:`inspect.BoundArguments` split (which makes the routing handle
mid-positional gaps cleanly, since a missing entry naturally switches
``BoundArguments`` from ``.args`` to ``.kwargs``). Layouts that
``BoundArguments`` can't faithfully represent (e.g. ``USE_REMOTE_DEFAULT``
mid-positional-only) surface as a ``MalformedArgs`` IRException at the
impl side's bind step. The sentinel placed inside ``*args`` / ``**kwargs``
content or nested in a collection is rejected by the
:data:`FxtrServiceData` vocabulary check in
:func:`invoke_service_method` — no extra layer of validation needed.

**Type-id introspection.** No reserved Protocol attribute. To expose
the impl's runtime type identity, declare an explicit Protocol method
tagged with ``@proxy_alias_method("$fxtr.type")`` — its return
annotation drives the typed decoder (typically ``FxtrID`` or
``EntityRef[SomeDescriptor]``), and the wire dispatch lands on
:meth:`CommonServiceImpl._handle_reserved_invoke`'s framework path.
"""

from __future__ import annotations

import abc
import dataclasses
import inspect
import types
import typing
import weakref
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, ClassVar, Generic, Protocol, TypeVar, get_args, get_origin, get_type_hints

import mashumaro.config
import mashumaro.dialect

from fxtr2.conversion.base_registry import substitute_typevars
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import (
    FxtrScope,
    ServiceProxyContext,
    ServiceRef,
    ServiceResolver,
    ServiceTypeAnnotation,
    register_service_resolver,
)
from fxtr2.leases.leased_cap_ir import IRException

from .service_cap_common import (
    FXTR_TYPE_METHOD_NAME,
    USE_REMOTE_DEFAULT,
    CommonServiceImpl,
    FxtrServiceData,
    ServiceProtocolError,
    _UseRemoteDefault,
    invoke_service_method,
    walk_service_methods,
)
from .service_dialect import FxtrServicesDefaultDialect, get_basic_decoder, get_basic_encoder

# =============================================================================
# Errors
# =============================================================================


class ServiceTypeIdMismatchError(IRException[None]):
    """Raised when a typed-service resolve gates on ``service_type_id`` and the gate fails.

    Triggered by the ``service_type_id=`` or ``service_type_validator=``
    args on :func:`service_protocol`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message=message, category="ServiceTypeIdMismatch")


# =============================================================================
# Reserved field names
# =============================================================================


_RESERVED_RESULT_FIELD = "value"
"""Sole field name on the result wrapper (single-field shell)."""


# =============================================================================
# proxy_alias_method — Protocol-method wire-name override
# =============================================================================


_PROXY_WIRE_NAME_ATTR = "__fxtr_proxy_wire_name__"
"""Function attribute set by :func:`proxy_alias_method`.

Read by :func:`_build_method_wrappers` to populate
:attr:`_MethodSpec.wire_name`, which :func:`_make_proxy_method` uses
as the ``method_name`` argument to :func:`invoke_service_method`. The
function's Python ``__name__`` stays the proxy method's display name
(used for ``__getattr__`` resolution and tracebacks); the alias only
affects the over-the-wire string."""


def proxy_alias_method(wire_name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Override the wire method-name shipped when this Protocol method is invoked.

    Apply on a Protocol method declaration. The proxy generated by
    :func:`service_protocol` will, when the user calls this method,
    ship ``wire_name`` as the wire method-name string instead of the
    Python attribute name. Useful for targeting framework-reserved
    paths (e.g. ``"$fxtr.ctx"`` for the mount-scope context marker)
    that aren't syntactically valid Python identifiers.

    Example::

        @service_protocol
        class MyService(Protocol):
            @proxy_alias_method("$fxtr.ctx")
            async def service_mount_context(self) -> FxtrID: ...

            async def some_method(self, x: int) -> str: ...

    The decorator is generic — any non-empty string works. There is
    no impl-side dispatch alias, so non-framework aliases that don't
    have a framework-prefix interception will land in the impl's
    typed-method registry under the alias name (and fail to match the
    user-defined method, which is registered under its Python name).
    For now, the only first-class supported alias targets are wire
    names matching :data:`fxtr2.services.service_cap_common.FXTR_FRAMEWORK_METHOD_PREFIX`.

    Args:
        wire_name: The wire method-name string. Must be a non-empty
            string. Validated at decoration time.

    Returns:
        A decorator that stashes ``wire_name`` on the wrapped function
        as :data:`_PROXY_WIRE_NAME_ATTR` and returns it unchanged.

    Raises:
        ValueError: ``wire_name`` is the empty string.
    """
    if not wire_name:
        raise ValueError("proxy_alias_method(wire_name=...): wire_name must be non-empty")

    def deco(func: Callable[..., Any]) -> Callable[..., Any]:
        setattr(func, _PROXY_WIRE_NAME_ATTR, wire_name)
        return func

    return deco


# =============================================================================
# Per-method bookkeeping
# =============================================================================


@dataclasses.dataclass(frozen=True)
class _MethodSpec:
    """Per-method metadata recorded at decorator/__init_subclass__ time.

    The wrapper dataclass + cached codec live elsewhere (built lazily
    against a concrete parameterization, since impls can be generic).
    """

    name: str
    """The Python method name on the owner class."""
    wire_name: str
    """The wire method-name string the proxy ships when this method is
    invoked. Defaults to ``name``; overridden via :func:`proxy_alias_method`
    so a Protocol method can target a wire path that isn't a valid
    Python identifier (e.g. ``"$fxtr.ctx"`` for the framework
    introspection path)."""
    signature: inspect.Signature
    """The method's signature, including ``self``."""
    signature_no_self: inspect.Signature
    """``signature`` with ``self`` stripped — used to drive
    ``inspect.BoundArguments`` construction at proxy-encode time
    (``self`` never reaches the wrapper)."""
    args_wrapper_cls: type
    """Plain @dataclass parameterized by the owner class's TypeVars.
    Field layout mirrors the method's signature one-to-one — including
    using the user's actual ``*args`` / ``**kwargs`` parameter names
    as field names (not reserved names). The wire ships
    :class:`inspect.BoundArguments`-split positional/keyword args, so
    field names never need to agree across proxy and impl — both sides
    re-bind to their own signature."""
    return_wrapper_cls: type
    """Single-field ('value') @dataclass for the return value."""


# =============================================================================
# Wrapper dataclass generation
# =============================================================================


def _resolve_method_hints(
    owner_cls: type, method_name: str, method: Callable[..., Any]
) -> Mapping[str, Any]:
    """Resolve ``method``'s annotations against ``owner_cls``'s module scope.

    Uses ``typing.get_type_hints`` so forward refs / ``from __future__
    import annotations`` strings are evaluated. Per the part8_plan
    discipline note, every annotation read goes through this helper.

    PEP 695 class-level TypeVars (declared via ``class Foo[T]: ...``)
    are not in any normal namespace ``get_type_hints`` searches; we
    inject them into ``localns`` keyed by name so annotations like
    ``async def get(self) -> T`` resolve correctly.

    .. warning::

       Because the injection is keyed by *name*, ``owner_cls`` must be
       the class that **lexically defines** ``method`` — passing a
       different class (e.g. a subclass of the defining class) would
       silently rebind a string annotation like ``"T"`` to that class's
       same-named TypeVar object. :func:`_build_method_wrappers`
       therefore resolves inherited methods against their defining
       class and maps the result into the owner's vocabulary
       afterwards via object-keyed substitution
       (:func:`_typevar_subst_map`).
    """
    localns: dict[str, Any] = {}
    # Make the owning protocol resolvable by its own name so a method may
    # reference its defining class in its annotations (e.g. a self-referential
    # return type ``async def subslot(...) -> ServiceRef[OwnerProto]``).
    # ``@service_protocol`` resolves hints eagerly — before the class name is
    # bound in the module globals — so without this such forward refs fail.
    # The decorator returns the class unchanged, so the name binds to this same
    # object afterwards. ``setdefault`` so a same-named TypeVar still wins.
    localns.setdefault(owner_cls.__name__, owner_cls)
    for tv in getattr(owner_cls, "__type_params__", ()) or ():
        localns[tv.__name__] = tv
    for tv in getattr(owner_cls, "__parameters__", ()) or ():
        localns.setdefault(tv.__name__, tv)
    try:
        return get_type_hints(method, localns=localns, include_extras=False)
    except Exception as exc:  # noqa: BLE001
        raise ServiceProtocolError(
            f"Failed to resolve type hints for {owner_cls.__name__}.{method_name}: {exc!r}"
        ) from exc


def _validate_no_method_typevars(
    owner_cls: type,
    method_name: str,
    signature: inspect.Signature,
) -> None:
    """Reject method-level type parameters (only class-level supported)."""
    method_tvars = getattr(signature, "__type_params__", None) or ()
    if method_tvars:
        raise ServiceProtocolError(
            f"Method-level type parameters are not allowed; "
            f"{owner_cls.__name__}.{method_name} declares {method_tvars!r}."
        )


def _typevar_subst_map(owner_cls: type, defining_cls: type) -> dict[Any, Any]:
    """Compose the TypeVar substitution map from ``owner_cls`` to ``defining_cls``.

    Walks ``types.get_original_bases`` hop by hop along the first
    inheritance path that reaches ``defining_cls``, composing the
    per-hop parameterization maps, and returns a map from
    ``defining_cls``'s TypeVar *objects* to annotations in
    ``owner_cls``'s vocabulary (concrete types and/or ``owner_cls``'s
    own TypeVars). E.g. for ``class Foo[V](EventSource[V], Protocol)``
    this returns ``{T_eventsource: V}``; for
    ``class Bar(EventSource[int], Protocol)`` it returns
    ``{T_eventsource: int}``; multi-hop chains compose.

    An unparameterized hop (e.g. ``class X(EventSource, Protocol)``)
    contributes no substitutions — the base's TypeVars stay
    unsubstituted and downstream codec builds fall back to their
    bounds (the documented fallback for unparameterized bases).
    Keying on TypeVar objects (not names) means a subclass reusing
    the base's TypeVar *name* doesn't alias.
    """
    if owner_cls is defining_cls:
        return {}
    for base in types.get_original_bases(owner_cls):
        origin = typing.get_origin(base) or base
        if origin is Generic or origin is Protocol or not isinstance(origin, type):
            continue
        if origin is not defining_cls and defining_cls not in origin.__mro__:
            continue
        params = tuple(getattr(origin, "__parameters__", ()) or ())
        args = typing.get_args(base)
        hop: dict[Any, Any] = dict(zip(params, args)) if args else {}
        if origin is defining_cls:
            return hop
        inner = _typevar_subst_map(origin, defining_cls)
        return {tv: substitute_typevars(anno, hop) for tv, anno in inner.items()}
    return {}


def _build_method_wrappers(
    owner_cls: type,
    method_name: str,
    method: Callable[..., Any],
    *,
    defining_cls: type | None = None,
) -> _MethodSpec:
    """Generate the args / return wrapper dataclasses for a single method.

    Both wrappers share the *same TypeVar objects* as ``owner_cls`` (read
    from ``__parameters__``) so passing a parameterized form of the
    wrapper to mashumaro substitutes correctly. The owner can be a
    Protocol or a TypedServiceImpl subclass — both have
    ``__parameters__`` for class-level TypeVars.

    For a method inherited from a base class, pass ``defining_cls``
    (the MRO class whose ``vars()`` holds the function). The method's
    annotations are then resolved against the *defining* class — its
    module globals and its TypeVar objects — and mapped into
    ``owner_cls``'s vocabulary via :func:`_typevar_subst_map`, so the
    wrapper's fields reference ``owner_cls``'s TypeVars (or the
    owner's concrete bindings) instead of the base's. Without this, an
    inherited generic method's base-TypeVar never appears in the
    wrapper's params and mashumaro silently resolves it to its bound.
    """
    sig = inspect.signature(method)
    if defining_cls is not None and defining_cls is not owner_cls:
        hints: Mapping[str, Any] = _resolve_method_hints(defining_cls, method_name, method)
        subst = _typevar_subst_map(owner_cls, defining_cls)
        if subst:
            hints = {name: substitute_typevars(anno, subst) for name, anno in hints.items()}
    else:
        hints = _resolve_method_hints(owner_cls, method_name, method)
    _validate_no_method_typevars(owner_cls, method_name, sig)

    # Owner's TypeVars (PEP 695 class-level type params end up in
    # __parameters__ for our wrapper purposes).
    owner_tvars: tuple[TypeVar, ...] = tuple(getattr(owner_cls, "__parameters__", ()) or ())

    # Build the args wrapper's field list. Field names mirror the
    # method's parameter names one-to-one; ``*args`` / ``**kwargs``
    # use their actual user-spelled names (no reserved-name swap), with
    # the field type widened to ``tuple[anno, ...]`` / ``dict[str, anno]``
    # so the wrapper carries the variadic content as a single field.
    args_fields: list[tuple[str, Any] | tuple[str, Any, Any]] = []
    for param_name, param in sig.parameters.items():
        if param_name == "self":
            continue
        anno = hints.get(param_name, Any)
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            # Default to empty tuple. The wire may legitimately omit the
            # variadic field (caller passed no extra positional args),
            # in which case ``inspect.Signature.bind`` on the impl side
            # leaves it absent from ``bound.arguments``; the wrapper
            # then needs to fall back to ``()`` rather than raise
            # ``MissingField``.
            args_fields.append(
                (
                    param_name,
                    tuple[anno, ...],  # type: ignore[valid-type]
                    dataclasses.field(default=()),
                )
            )
        elif param.kind is inspect.Parameter.VAR_KEYWORD:
            args_fields.append(
                (
                    param_name,
                    dict[str, anno],  # type: ignore[valid-type]
                    dataclasses.field(default_factory=dict),
                )
            )
        elif param.default is inspect.Parameter.empty:
            # Required on this side. Wrapper field is required too; the
            # wire MUST carry a value for it.
            args_fields.append((param_name, anno))
        else:
            # Optional on this side. The wrapper field defaults to
            # ``USE_REMOTE_DEFAULT`` so a missing wire entry decodes to
            # the sentinel; the dispatcher strips sentinels before the
            # actual call so Python's normal call machinery applies the
            # impl's own defaults. The widened union
            # ``_UseRemoteDefault | T`` lets mashumaro accept the
            # sentinel as a valid value for the field.
            #
            # Order matters: ``_UseRemoteDefault`` MUST come first.
            # Mashumaro's ``pack_union`` calls
            # ``issubclass(get_type_origin(arg), Collection)`` the first
            # time it sees each unique packer — once a packer is in the
            # set, subsequent members sharing it skip the check. If
            # ``T`` is something whose origin isn't a class (notably
            # ``T = FxtrServiceData``, a ``TypeAliasType``), the
            # check raises ``TypeError`` and codec build fails. Putting
            # ``_UseRemoteDefault`` (a real class) first makes the
            # check run on it, then any same-packer ``T`` rides along
            # without triggering the check again.
            args_fields.append(
                (
                    param_name,
                    _UseRemoteDefault | anno,
                    dataclasses.field(default=USE_REMOTE_DEFAULT),
                )
            )

    args_wrapper_cls = _make_wrapper_dataclass(
        f"{owner_cls.__name__}__{method_name}__Args",
        args_fields,
        owner_tvars,
    )

    # Build the return wrapper.
    return_anno = hints.get("return", Any)
    return_wrapper_cls = _make_wrapper_dataclass(
        f"{owner_cls.__name__}__{method_name}__Result",
        [(_RESERVED_RESULT_FIELD, return_anno)],
        owner_tvars,
    )

    sig_no_self = inspect.Signature(
        parameters=[p for n, p in sig.parameters.items() if n != "self"],
        return_annotation=sig.return_annotation,
    )

    wire_name = getattr(method, _PROXY_WIRE_NAME_ATTR, method_name)
    return _MethodSpec(
        name=method_name,
        wire_name=wire_name,
        signature=sig,
        signature_no_self=sig_no_self,
        args_wrapper_cls=args_wrapper_cls,
        return_wrapper_cls=return_wrapper_cls,
    )


def _make_wrapper_dataclass(
    name: str,
    fields: Sequence[tuple[str, Any] | tuple[str, Any, Any]],
    type_params: tuple[TypeVar, ...],
) -> type:
    """Generate a plain ``@dataclass`` parameterized by ``type_params``.

    *Important*: this is a plain ``@dataclass``, not a
    ``mashumaro.DataClassDictMixin`` subclass. ``DataClassDictMixin``
    compiles its codec eagerly inside ``__init_subclass__``, before
    the dialect can be supplied — at which point any custom strategy
    (``EntityRef[T]``, ``ServiceRef[T]``, etc.) is invisible and
    mashumaro will descend into the leaf type's internal fields and
    error. ``BasicEncoder`` / ``BasicDecoder`` (built in
    ``service_dialect.get_basic_encoder/decoder``) compile against
    the dialect at codec-build time, so a plain dataclass is what we
    need. Don't "consolidate" by adding ``DataClassDictMixin`` to
    ``bases`` — see ``part8_plan.md`` "Spike notes" for the full
    history.

    The ``Generic[*type_params]`` base shares the *same TypeVar
    objects* as the owning class, so passing ``WrapperCls[*concrete]``
    to mashumaro substitutes correctly throughout the wrapper's
    fields (including nested ``EntityRef[T]`` / ``ServiceRef[T]``).

    The dataclass is ``kw_only=True`` so field ordering doesn't have
    to obey the "required-before-defaulted" constructor rule. The
    Protocol method's signature is the source of truth for parameter
    order on the wire; the wrapper class is only ever constructed
    via ``args_cls(**...)`` on both the proxy and impl sides, so
    making it kw-only is a pure relaxation that lets Protocols
    declare keyword-only params in any order (e.g.
    ``async def foo(self, *, msg: str, prefix: str = "kw")`` where a
    required kw-only follows an optional one — legal in Python but
    rejected by a positional-or-keyword dataclass).
    """
    bases: tuple[type, ...] = (Generic[*type_params],) if type_params else ()  # type: ignore[valid-type]
    cls = dataclasses.make_dataclass(
        name,
        fields=fields,
        bases=bases,
        frozen=True,
        kw_only=True,
    )
    # Mashumaro silently drops extra keys by default. We want a hard
    # error: drift between proxy- and impl-side wrappers (other than
    # via missing-with-default fields) should surface immediately
    # rather than silently. Inject a Config inner class with
    # ``forbid_extra_keys = True`` so mashumaro's dataclass codec
    # raises ``ExtraKeysError`` at decode if the wire dict contains
    # any key the wrapper doesn't declare.
    cls.Config = type(  # type: ignore[attr-defined]
        "Config",
        (mashumaro.config.BaseConfig,),
        {"forbid_extra_keys": True},
    )
    return cls


def _walk_protocol_methods(proto: type) -> Mapping[str, tuple[type, Callable[..., Any]]]:
    """Find async-def methods on a ``Protocol`` class, rejecting bad shapes.

    Skips ``self``-less attributes, dunders, and any sync /
    classmethod / staticmethod / property declarations.

    Returns ``name -> (defining_cls, method)`` pairs, first-wins by
    name across the MRO (an override on a subclass shadows the base's
    declaration). The defining class is needed by
    :func:`_build_method_wrappers` to resolve an inherited method's
    annotations in the right namespace and substitute the defining
    class's TypeVars with the owner's bindings.
    """
    result: dict[str, tuple[type, Callable[..., Any]]] = {}
    for klass in proto.__mro__:
        if klass is object or klass is Protocol or klass is Generic:
            continue
        for name, attr in vars(klass).items():
            if name in result:
                continue
            if name.startswith("__") and name.endswith("__"):
                continue
            if isinstance(attr, (classmethod, staticmethod, property)):
                raise ServiceProtocolError(
                    f"Protocol {proto.__name__} declares {name!r} as a "
                    f"{type(attr).__name__}; only async methods are supported."
                )
            if not callable(attr):
                # Plain typed attributes (e.g. data fields on the Protocol)
                # are tolerated but not exposed as methods.
                continue
            if not inspect.iscoroutinefunction(attr):
                raise ServiceProtocolError(
                    f"Protocol {proto.__name__}.{name} must be 'async def'; "
                    f"got a sync callable."
                )
            result[name] = (klass, attr)
    return result


# =============================================================================
# Protocol metadata
# =============================================================================


class ServiceTypeValidator(Protocol):
    """Async predicate for resolve-time service-type-id gating.

    Mirrors :meth:`fxtr2.data.entity_ref_scope.EntityLoadHandler.supports_entity_type`.
    The validator is invoked after the resolver has lifted the
    service_type_id locally, so it can ``EntityRef(service_type_id,
    ...).load()`` if needed.
    """

    async def __call__(
        self,
        service_type_id: FxtrID,
        target_service_type: ServiceTypeAnnotation[Any],
    ) -> bool: ...


_METADATA_ATTR = "_fxtr_service_protocol_metadata"


@dataclasses.dataclass(frozen=True, eq=False)
class _ServiceProtocolMetadata:
    """Metadata attached to a ``@service_protocol``-decorated class.

    Equality is by identity (the dataclass is mutable-keyed in the
    proxy-class build cache; identity is the right key).
    """

    proto: type
    dialect: type[mashumaro.dialect.Dialect]
    service_type_id_check: FxtrID | None
    service_type_validator: ServiceTypeValidator | None
    method_specs: Mapping[str, _MethodSpec]


# =============================================================================
# @service_protocol decorator
# =============================================================================


_T_PROTO = TypeVar("_T_PROTO")


@typing.overload
def service_protocol(proto: type[_T_PROTO], /) -> type[_T_PROTO]: ...


@typing.overload
def service_protocol(
    *,
    dialect: type[mashumaro.dialect.Dialect] = FxtrServicesDefaultDialect,
    service_type_id: FxtrID | None = None,
    service_type_validator: ServiceTypeValidator | None = None,
    defer_codec_to_runtime: bool = False,
    allow_protocol_subtypes: bool = False,
) -> Callable[[type[_T_PROTO]], type[_T_PROTO]]: ...


def service_protocol(
    proto: type | None = None,
    /,
    *,
    dialect: type[mashumaro.dialect.Dialect] = FxtrServicesDefaultDialect,
    service_type_id: FxtrID | None = None,
    service_type_validator: ServiceTypeValidator | None = None,
    defer_codec_to_runtime: bool = False,
    allow_protocol_subtypes: bool = False,
) -> Any:
    """Register a ``Protocol`` class as a typed service interface.

    Walks the Protocol's async methods, builds per-method args / return
    wrapper dataclasses, eagerly compiles codecs against ``dialect``
    (so unsupported annotations fail at decoration time), inspects the
    reserved ``service_type_id`` attribute, and registers a resolver
    for the Protocol type.

    Args:
        proto: The Protocol class. May be omitted when used as
            ``@service_protocol(...)`` with kwargs.
        dialect: The mashumaro dialect that drives per-arg/return
            conversion. Default: :class:`FxtrServicesDefaultDialect`.
        service_type_id: If supplied, the resolver raises
            :class:`ServiceTypeIdMismatchError` whenever the resolved
            ``ServiceRef.service_type_id`` differs from this value.
            Mutually exclusive with ``service_type_validator``.
        service_type_validator: An async predicate called at resolve
            time to gate compatibility. Mutually exclusive with
            ``service_type_id``.
        defer_codec_to_runtime: If True, skip the eager
            unparameterized codec build at decoration time; codecs are
            built lazily at first resolve / mount instead. Useful when
            an annotation is only resolvable in a context not
            available at module-import time (e.g. forward references
            into a not-yet-imported sibling module). The trade-off is
            losing the early-failure feedback on broken annotations.
        allow_protocol_subtypes: If True, this Protocol is marked as a
            subclassable *base*: other ``@service_protocol`` Protocols
            may subclass it and register their own resolvers, and
            resolving a subclass type picks the most-specific resolver
            (the subclass's own). Inherited methods get genuine
            TypeVar substitution — a subclass binding the base's
            TypeVars (``class Foo(Base[int], Protocol)``) inherits the
            base's methods with the substituted concrete annotations.
            An *undecorated* subclass resolves via this base's
            resolver with the base's *method vocabulary* (methods the
            subclass adds aren't exposed), but TypeVar bindings made
            via inheritance are honored: resolving
            ``class Sub(Base[int], Protocol)`` builds the proxy with
            the same typed codecs as resolving ``Base[int]`` (the
            parameterization is recovered from the inheritance chain
            by :func:`_proxy_parameterization`). If False (the
            default), registering a subclass Protocol still succeeds
            but resolving the subclass type raises a
            conflicting-handlers error.

    Returns:
        The Protocol class unchanged, with metadata attached as
        ``_fxtr_service_protocol_metadata``.
    """
    if service_type_id is not None and service_type_validator is not None:
        raise ServiceProtocolError(
            "service_type_id and service_type_validator are mutually exclusive."
        )

    def _apply(p: type) -> type:
        method_attrs = _walk_protocol_methods(p)
        method_specs: dict[str, _MethodSpec] = {}
        for name, (defining_cls, method) in method_attrs.items():
            spec = _build_method_wrappers(p, name, method, defining_cls=defining_cls)
            method_specs[name] = spec
            if defer_codec_to_runtime:
                continue
            # Eager codec build to surface annotation errors at decoration
            # time. We use the unparameterized form (the Protocol class
            # itself) — mashumaro accepts it and resolves TypeVars to
            # their bounds (typically Any). When the resolver is invoked
            # with a parameterized subquery, the codecs are rebuilt
            # against the parameterized form via the cached factory.
            try:
                get_basic_encoder(spec.args_wrapper_cls, dialect)
                get_basic_decoder(spec.args_wrapper_cls, dialect)
                get_basic_encoder(
                    spec.return_wrapper_cls,
                    dialect,
                    post_encoder_func=_strip_result_wrapper,
                )
                get_basic_decoder(
                    spec.return_wrapper_cls,
                    dialect,
                    pre_decoder_func=_wrap_result_value,
                )
            except Exception as exc:  # noqa: BLE001
                raise ServiceProtocolError(
                    f"Failed to build codecs for {p.__name__}.{name}: {exc!r}"
                ) from exc
        metadata = _ServiceProtocolMetadata(
            proto=p,
            dialect=dialect,
            service_type_id_check=service_type_id,
            service_type_validator=service_type_validator,
            method_specs=method_specs,
        )
        setattr(p, _METADATA_ATTR, metadata)
        # ``ServiceResolver[T]`` is a Protocol with ``async def __call__``;
        # an inner ``async def`` whose return type is ``Any`` is
        # structurally compatible at runtime, but Pyright's structural
        # check rejects ``Awaitable[Any]`` vs ``CoroutineType[...]``.
        # Cast at the call site to keep the inner resolver simple.
        register_service_resolver(
            p,
            typing.cast("ServiceResolver[Any]", _make_typed_resolver(metadata)),
            allow_implicit_overrides=allow_protocol_subtypes,
        )
        return p

    if proto is not None:
        return _apply(proto)
    return _apply


def _strip_result_wrapper(d: Mapping[str, Any]) -> Any:
    """``post_encoder_func`` that unwraps ``{"value": x}`` to ``x``."""
    return d[_RESERVED_RESULT_FIELD]


def _wrap_result_value(v: Any) -> Mapping[str, Any]:
    """``pre_decoder_func`` that re-wraps ``x`` to ``{"value": x}``."""
    return {_RESERVED_RESULT_FIELD: v}


# =============================================================================
# TypedServiceProxy factory
# =============================================================================


def _proxy_class_cache_key(
    target_type: ServiceTypeAnnotation[Any], metadata: _ServiceProtocolMetadata
) -> tuple[Any, int]:
    """``(target_type, id(metadata))`` — metadata is identity-keyed."""
    return (target_type, id(metadata))


_PROXY_CLASS_CACHE: dict[tuple[Any, int], type] = {}


def _proxy_parameterization(
    target_type: ServiceTypeAnnotation[Any], proto: type
) -> tuple[Any, ...] | None:
    """Compute the wrapper parameterization (in ``proto``'s TypeVar order)
    for a resolve target.

    ``target_type`` is whatever annotation the registry matched for
    ``proto``'s resolver: ``proto`` itself, ``proto[...]``, an
    (undecorated) subclass, or a generic alias of one. For subclass
    targets, bindings made *via inheritance*
    (``class Sub(Base[int], Protocol)``) are recovered by composing
    the :func:`_typevar_subst_map` from the subclass down to ``proto``
    with any use-site args binding the subclass's own TypeVars — so an
    undecorated bound subclass resolves with the same typed codecs as
    ``Base[int]`` instead of silently degrading to the TypeVar
    default.

    Returns ``None`` when no binding information is available (bare
    proto, non-generic proto, fully-unbound subclass) — the caller
    then uses the unparameterized wrappers as before.
    """
    origin = get_origin(target_type) or target_type
    args = get_args(target_type)
    if origin is proto:
        return args or None
    if not isinstance(origin, type) or proto not in getattr(origin, "__mro__", ()):
        # Unrecognized target shape (e.g. an alias the registry already
        # unwrapped differently); preserve the old behavior.
        return args or None
    subst = _typevar_subst_map(origin, proto)
    if args:
        # Use-site args bind the *subclass's* TypeVars; compose them in.
        own = dict(zip(tuple(getattr(origin, "__parameters__", ()) or ()), args))
        subst = {tv: substitute_typevars(anno, own) for tv, anno in subst.items()}
    proto_params: tuple[TypeVar, ...] = tuple(getattr(proto, "__parameters__", ()) or ())
    if not proto_params or not subst:
        return None
    out = tuple(subst.get(tv, tv) for tv in proto_params)
    if all(isinstance(entry, TypeVar) for entry in out):
        return None
    return out


def _build_proxy_class(
    target_type: ServiceTypeAnnotation[Any], metadata: _ServiceProtocolMetadata
) -> type:
    """Build (or return cached) proxy class for ``(target_type, metadata)``.

    Generates one async method per Protocol method. Type-id introspection
    is opt-in via an explicit Protocol method tagged with
    ``@proxy_alias_method("$fxtr.type")`` — that becomes a regular
    proxy method whose wire dispatch lands on the framework
    ``$fxtr.type`` handler.
    """
    key = _proxy_class_cache_key(target_type, metadata)
    cached = _PROXY_CLASS_CACHE.get(key)
    if cached is not None:
        return cached

    parameterization = _proxy_parameterization(target_type, metadata.proto)
    namespace: dict[str, Any] = {
        "__init__": _proxy_init,
        "__repr__": _proxy_repr,
    }

    # Build per-method async wrappers.
    for method_name, spec in metadata.method_specs.items():
        namespace[method_name] = _make_proxy_method(
            method_name, spec, metadata.dialect, parameterization
        )

    cls = type(f"{metadata.proto.__name__}__TypedServiceProxy", (object,), namespace)
    _PROXY_CLASS_CACHE[key] = cls
    return cls


def _proxy_init(self: Any, ctx: ServiceProxyContext) -> None:
    self._ctx = ctx


def _proxy_repr(self: Any) -> str:
    return f"<{type(self).__name__} handle={self._ctx.proxy_handle!r}>"


def _make_proxy_method(
    method_name: str,
    spec: _MethodSpec,
    dialect: type[mashumaro.dialect.Dialect],
    parameterization: tuple[Any, ...] | None,
) -> Callable[..., Awaitable[Any]]:
    """Generate the proxy-side async wrapper for one method.

    Flow:
      1. Bind user args/kwargs to the Protocol method's signature
         (``apply_defaults`` so the Protocol's stated default — including
         ``USE_REMOTE_DEFAULT`` — appears in ``bound.arguments``).
      2. Reject nested ``USE_REMOTE_DEFAULT`` (inside ``*args`` content,
         ``**kwargs`` content, or collections) — those can't be stripped
         from the wire.
      3. Build the args-wrapper instance from ``bound.arguments`` (with
         ``self`` dropped) and encode. The wrapper exists purely so
         mashumaro can substitute TypeVars; field names mirror the
         user's parameter names.
      4. Strip ``USE_REMOTE_DEFAULT`` entries from the encoded dict —
         those fields fall through to the impl's own defaults.
      5. Hand the stripped dict to a fresh :class:`inspect.BoundArguments`
         so its ``.args`` / ``.kwargs`` properties produce the natural
         positional/keyword wire split for free.
      6. Dispatch via ``invoke_service_method(*new_bound.args, **new_bound.kwargs)``.
      7. Decode the result.
    """
    # Build codecs at proxy-class-build time (cached). Parameterized form
    # of the wrapper substitutes the Protocol's TypeVars.
    args_cls = (
        spec.args_wrapper_cls[*parameterization]  # type: ignore[index]
        if parameterization
        else spec.args_wrapper_cls
    )
    return_cls = (
        spec.return_wrapper_cls[*parameterization]  # type: ignore[index]
        if parameterization
        else spec.return_wrapper_cls
    )
    args_encoder = get_basic_encoder(args_cls, dialect)
    return_decoder = get_basic_decoder(return_cls, dialect, pre_decoder_func=_wrap_result_value)

    sig = spec.signature
    sig_no_self = spec.signature_no_self

    async def _proxy_method(self: Any, *args: Any, **kwargs: Any) -> Any:
        bound = sig.bind(self, *args, **kwargs)
        bound.apply_defaults()

        # Build the wrapper instance from bound.arguments (drop self).
        # Field names mirror the user's parameter names, so this is a
        # one-to-one mapping.
        wrapper_kwargs = {k: v for k, v in bound.arguments.items() if k != "self"}
        try:
            instance = args_cls(**wrapper_kwargs)
        except TypeError as exc:
            raise IRException(
                message=f"Failed to build args wrapper for {method_name}: {exc}",
                category="MalformedArgs",
            ) from exc
        encoded = args_encoder.encode(instance)

        # Strip USE_REMOTE_DEFAULT entries — these mean "no value here,
        # use the impl's default". The dialect's ``pass_through`` keeps
        # the sentinel through encode, so identity comparison works.
        # Dropping the entries also makes the BoundArguments split route
        # later params to .kwargs naturally (a missing entry mid-positional
        # makes BoundArguments switch from .args to .kwargs at that point).
        encoded = {k: v for k, v in encoded.items() if not isinstance(v, _UseRemoteDefault)}

        # Use BoundArguments to compute the natural wire split.
        new_bound = inspect.BoundArguments(sig_no_self, encoded)  # type: ignore[arg-type]
        result = await invoke_service_method(
            self._ctx, spec.wire_name, *new_bound.args, **new_bound.kwargs
        )
        decoded = return_decoder.decode(result)
        return getattr(decoded, _RESERVED_RESULT_FIELD)

    _proxy_method.__name__ = method_name
    _proxy_method.__qualname__ = method_name
    return _proxy_method


# =============================================================================
# Resolver builder
# =============================================================================


_VALIDATOR_RESULT_CACHE: weakref.WeakKeyDictionary["ServiceTypeValidator", dict[FxtrID, bool]] = (
    weakref.WeakKeyDictionary()
)
"""Per-validator cache mapping wire-shipped ``service_type_id`` to predicate result.

Keyed weakly on the validator callable so user code that constructs
short-lived validators doesn't accumulate cache entries forever (the
inner dict goes when the validator goes). Validators registered at
import time and held by the surrounding ``_ServiceProtocolMetadata``
stay alive indefinitely, which is what we want for the steady-state
cache to do its job. The inner dict is keyed on the wire-shipped
``FxtrID`` (FxtrID equality already ignores provenance, so the wire
hash and any layered ``$fxtr.type`` response compare equal as long
as their data ids match).

Cache hit: trust the wire hash, return the cached bool, skip the
``$fxtr.type`` dispatch entirely. Cache miss: invoke ``$fxtr.type``
to produce a properly-tracked id (so the validator can
``EntityRef.load`` it), assert it equals the wire hash, run the
predicate with the returned id, cache the bool keyed on the wire
hash.
"""


async def _check_validator_with_cache(
    *,
    validator: "ServiceTypeValidator",
    wire_type_id: FxtrID,
    target_type: ServiceTypeAnnotation[Any],
    context: ServiceProxyContext,
    proto_name: str,
) -> bool:
    """Cache + ``$fxtr.type``-on-miss wrapper for ``service_type_validator``.

    See :data:`_VALIDATOR_RESULT_CACHE` for the cache shape and rules.
    Mismatches between the wire-shipped hash and the impl's
    ``$fxtr.type`` answer raise :class:`ServiceTypeIdMismatchError`
    (the impl is claiming a different identity than the sender shipped).
    """
    cache = _VALIDATOR_RESULT_CACHE.setdefault(validator, {})
    cached = cache.get(wire_type_id)
    if cached is not None:
        return cached
    returned_id = await invoke_service_method(context, FXTR_TYPE_METHOD_NAME)
    if not isinstance(returned_id, FxtrID):
        raise IRException(
            message=(
                f"Resolver for {proto_name}: $fxtr.type returned " f"non-FxtrID {returned_id!r}"
            ),
            category="MalformedPacket",
        )
    if returned_id != wire_type_id:
        raise ServiceTypeIdMismatchError(
            f"$fxtr.type returned {returned_id!r}; wire hash "
            f"declared {wire_type_id!r}. Proxy on {proto_name} "
            f"refusing to validate (impl identity disagrees with sender)."
        )
    ok = await validator(returned_id, target_type)
    cache[wire_type_id] = ok
    return ok


def _make_typed_resolver(
    metadata: _ServiceProtocolMetadata,
) -> Callable[[ServiceProxyContext, ServiceTypeAnnotation[Any]], Awaitable[Any]]:
    async def _resolver(
        context: ServiceProxyContext,
        target_type: ServiceTypeAnnotation[Any],
    ) -> Any:
        if metadata.service_type_id_check is not None:
            # Exact-match gate: trust the wire hash, no $fxtr.type call.
            # Equivalent to a 1-entry cache where the registered id
            # maps to True and every other id maps to False.
            if context.service_type_id != metadata.service_type_id_check:
                raise ServiceTypeIdMismatchError(
                    f"Resolved ServiceRef has service_type_id "
                    f"{context.service_type_id!r}; protocol "
                    f"{metadata.proto.__name__} requires "
                    f"{metadata.service_type_id_check!r}."
                )
        elif metadata.service_type_validator is not None:
            ok = await _check_validator_with_cache(
                validator=metadata.service_type_validator,
                wire_type_id=context.service_type_id,
                target_type=target_type,
                context=context,
                proto_name=metadata.proto.__name__,
            )
            if not ok:
                raise ServiceTypeIdMismatchError(
                    f"Resolved ServiceRef's service_type_id "
                    f"{context.service_type_id!r} rejected by validator on "
                    f"protocol {metadata.proto.__name__}."
                )
        cls = _build_proxy_class(target_type, metadata)
        return cls(context)

    return _resolver


# =============================================================================
# TypedServiceImpl
# =============================================================================


class TypedServiceImpl[T](CommonServiceImpl[T]):
    """Typed service implementation base.

    Subclasses tag methods with ``@service_method`` (same as
    :class:`SimpleAsyncServiceImpl`); per-method type conversion is
    applied on top using the impl's ``_service_dialect`` class
    attribute.

    Subclass responsibilities:

    - Implement :meth:`_target_type_annotation` to return the Protocol
      class (or other ServiceRef target type).
    - Implement :meth:`_service_type_id` to track + return the runtime
      type identity in the mount scope.
    - Optionally override :meth:`_own_full_type_for_serialization` to
      return a parameterized form (e.g. ``MyImpl[int]``) when the
      impl class has class-level TypeVars whose bindings are needed
      to substitute method annotations.

    The default :meth:`_own_full_type_for_serialization` returns
    ``type(self)``, which works for non-generic impls.

    Each method invocation runs in a fresh per-call scope that is a
    child of the impl's mount scope; data and services from any other
    scope must be explicitly copied or bridged in.
    """

    _service_dialect: ClassVar[type[mashumaro.dialect.Dialect]] = FxtrServicesDefaultDialect
    _fxtr_defer_codec_to_runtime: ClassVar[bool] = False
    """If True, skip the eager unparameterized codec build at
    ``__init_subclass__`` time. Codecs are built lazily at mount.
    Use when annotations need a context only available at runtime
    (e.g. forward references resolvable later)."""

    # Populated by __init_subclass__: signature-only specs (codecs are
    # built at mount time, when _own_full_type_for_serialization can be
    # called).
    _fxtr_typed_method_specs: ClassVar[Mapping[str, _MethodSpec]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if abc.ABC in cls.__bases__ or inspect.isabstract(cls):
            # Allow further-abstract subclasses (e.g. intermediate base
            # classes that don't yet declare service methods).
            return
        method_specs: dict[str, _MethodSpec] = {}
        # Walk @service_method-tagged methods (validates async + dunder
        # rejection).
        try:
            tagged = walk_service_methods(cls, require_async=True, forbid_dunders=True)
        except ServiceProtocolError:
            raise
        for method_name in tagged:
            defining_cls, unbound = _resolve_unbound(cls, method_name)
            spec = _build_method_wrappers(cls, method_name, unbound, defining_cls=defining_cls)
            method_specs[method_name] = spec
        # Eager unparameterized codec build to surface annotation errors
        # at class-definition time. Subclasses can opt out by setting
        # ``_fxtr_defer_codec_to_runtime = True``.
        for spec in method_specs.values():
            if cls._fxtr_defer_codec_to_runtime:
                continue
            try:
                get_basic_encoder(spec.args_wrapper_cls, cls._service_dialect)
                get_basic_decoder(spec.args_wrapper_cls, cls._service_dialect)
                get_basic_encoder(
                    spec.return_wrapper_cls,
                    cls._service_dialect,
                    post_encoder_func=_strip_result_wrapper,
                )
                get_basic_decoder(
                    spec.return_wrapper_cls,
                    cls._service_dialect,
                    pre_decoder_func=_wrap_result_value,
                )
            except Exception as exc:  # noqa: BLE001
                raise ServiceProtocolError(
                    f"Failed to build codecs for {cls.__name__}.{spec.name}: {exc!r}"
                ) from exc
        cls._fxtr_typed_method_specs = method_specs

    def _own_full_type_for_serialization(self) -> ServiceTypeAnnotation[Any]:
        """Return the runtime concrete annotation for this instance.

        Default: ``type(self)``. Override for generic impls — e.g.::

            class MyImpl[T](TypedServiceImpl[MyProto[T]]):
                def __init__(self, type_arg: type) -> None:
                    self._type_arg = type_arg

                def _own_full_type_for_serialization(self) -> Any:
                    return MyImpl[self._type_arg]

        Validation: the returned annotation must be either ``type(self)``
        itself (for non-generic impls) or a generic alias whose origin
        is ``type(self)`` and whose args bind every class-level TypeVar.
        Anything else raises :class:`ServiceProtocolError` at mount time.
        """
        return type(self)

    async def _mount_as_fxtr_service(self, scope: FxtrScope) -> ServiceRef[T]:
        # Validate the concrete type and build per-instance codecs.
        concrete_type = self._own_full_type_for_serialization()
        cls = type(self)
        self._fxtr_typed_runtime_specs = _build_runtime_specs(
            cls, concrete_type, cls._service_dialect, cls._fxtr_typed_method_specs
        )
        return await super()._mount_as_fxtr_service(scope)

    async def _handle_invoke(
        self, method_name: str, *args: FxtrServiceData, **kwargs: FxtrServiceData
    ) -> FxtrServiceData:
        # Framework-reserved ``$fxtr.*`` paths never reach here — the
        # dispatch infrastructure routes them to
        # ``CommonServiceImpl._handle_reserved_invoke`` before this
        # method is invoked.
        runtime_specs: Mapping[str, _RuntimeMethodSpec] = getattr(
            self, "_fxtr_typed_runtime_specs", {}
        )
        rspec = runtime_specs.get(method_name)
        if rspec is None:
            raise IRException(
                message=(
                    f"Method {method_name!r} is not exposed on "
                    f"{type(self).__name__}. Available: "
                    f"{sorted(runtime_specs.keys())!r}"
                ),
                category="UnknownMethod",
            )
        # Mirror of the proxy:
        #   1. Bind the wire's positional + keyword args to the impl
        #      signature so we get a name-keyed dict of encoded values.
        #   2. Decode via the args-wrapper codec.
        #   3. Strip USE_REMOTE_DEFAULT entries — those mean "the proxy
        #      didn't ship a value", so let Python's call machinery
        #      apply the impl method's own default.
        #   4. Hand the stripped dict to a fresh BoundArguments and call
        #      ``method(*new_bound.args, **new_bound.kwargs)``.
        method = getattr(self, method_name)
        impl_sig = inspect.signature(method)
        try:
            bound = impl_sig.bind(*args, **kwargs)
        except TypeError as exc:
            raise IRException(
                message=f"Failed to bind args for {method_name}: {exc}",
                category="MalformedArgs",
            ) from exc
        try:
            decoded = rspec.args_decoder.decode(dict(bound.arguments))
        except Exception as exc:  # noqa: BLE001
            raise IRException(
                message=f"Failed to decode args for {method_name}: {exc}",
                category="MalformedArgs",
            ) from exc

        flat = {f.name: getattr(decoded, f.name) for f in dataclasses.fields(decoded)}
        flat = {k: v for k, v in flat.items() if not isinstance(v, _UseRemoteDefault)}
        # Drop an EMPTY variadic entry: the wrapper field defaults to
        # ``()`` when the wire omits it, but ``BoundArguments`` treats a
        # gap (a stripped defaulted param before the variadic) by
        # spilling everything after it into ``.kwargs`` — which turns
        # the variadic into an illegal keyword argument. An empty
        # variadic is call-equivalent to an absent one, so dropping it
        # is always safe; a NON-empty variadic can only have arrived
        # with every earlier positional supplied (Python call syntax),
        # so no gap can precede it.
        flat = {
            k: v
            for k, v in flat.items()
            if not (
                (param := impl_sig.parameters.get(k)) is not None
                and param.kind is inspect.Parameter.VAR_POSITIONAL
                and not v
            )
        }
        new_bound = inspect.BoundArguments(impl_sig, flat)  # type: ignore[arg-type]
        result = await method(*new_bound.args, **new_bound.kwargs)

        try:
            wrapper_instance = rspec.return_wrapper_cls(**{_RESERVED_RESULT_FIELD: result})
        except TypeError as exc:
            raise IRException(
                message=f"Failed to build return wrapper for {method_name}: {exc}",
                category="MalformedReturn",
            ) from exc
        try:
            encoded = rspec.return_encoder.encode(wrapper_instance)
        except Exception as exc:  # noqa: BLE001
            raise IRException(
                message=f"Failed to encode return for {method_name}: {exc}",
                category="MalformedReturn",
            ) from exc
        return encoded


@dataclasses.dataclass(frozen=True)
class _RuntimeMethodSpec:
    """Per-method codec instances bound to a concrete impl type."""

    spec: _MethodSpec
    args_decoder: Any
    return_encoder: Any
    return_wrapper_cls: type


def _build_runtime_specs(
    cls: type,
    concrete_type: ServiceTypeAnnotation[Any],
    dialect: type[mashumaro.dialect.Dialect],
    method_specs: Mapping[str, _MethodSpec],
) -> Mapping[str, _RuntimeMethodSpec]:
    """Validate ``concrete_type`` and build per-instance codecs."""
    parameterization = _validate_concrete_type(cls, concrete_type)
    result: dict[str, _RuntimeMethodSpec] = {}
    for name, spec in method_specs.items():
        args_cls = (
            spec.args_wrapper_cls[*parameterization]  # type: ignore[index]
            if parameterization
            else spec.args_wrapper_cls
        )
        return_cls = (
            spec.return_wrapper_cls[*parameterization]  # type: ignore[index]
            if parameterization
            else spec.return_wrapper_cls
        )
        result[name] = _RuntimeMethodSpec(
            spec=spec,
            args_decoder=get_basic_decoder(args_cls, dialect),
            return_encoder=get_basic_encoder(
                return_cls, dialect, post_encoder_func=_strip_result_wrapper
            ),
            return_wrapper_cls=return_cls,
        )
    return result


def _validate_concrete_type(
    cls: type, concrete_type: ServiceTypeAnnotation[Any]
) -> tuple[Any, ...] | None:
    """Validate ``concrete_type`` matches ``cls`` and bind every class TypeVar.

    Returns the tuple of substituted args (suitable for
    ``Wrapper[*args]``) or ``None`` if ``cls`` has no class-level
    TypeVars.
    """
    cls_tvars = tuple(getattr(cls, "__parameters__", ()) or ())
    if not cls_tvars:
        if concrete_type is not cls:
            origin = get_origin(concrete_type)
            if origin is not cls:
                raise ServiceProtocolError(
                    f"_own_full_type_for_serialization on {cls.__name__} "
                    f"returned {concrete_type!r}; expected {cls.__name__} "
                    "(non-generic impl)."
                )
        return None
    origin = get_origin(concrete_type)
    if origin is not cls:
        raise ServiceProtocolError(
            f"_own_full_type_for_serialization on {cls.__name__} returned "
            f"{concrete_type!r}; expected {cls.__name__}[*type_args]."
        )
    args = get_args(concrete_type)
    if len(args) != len(cls_tvars):
        raise ServiceProtocolError(
            f"_own_full_type_for_serialization on {cls.__name__} returned "
            f"{concrete_type!r} with {len(args)} type args; expected "
            f"{len(cls_tvars)} (the class's TypeVar count)."
        )
    for a in args:
        if isinstance(a, TypeVar):
            raise ServiceProtocolError(
                f"_own_full_type_for_serialization on {cls.__name__} "
                f"returned {concrete_type!r}; TypeVar {a!r} is unbound. "
                "Every class-level TypeVar must be bound to a concrete type."
            )
    return args


def _resolve_unbound(cls: type, method_name: str) -> tuple[type, Callable[..., Any]]:
    """Return ``(defining_cls, unbound_function)`` for ``method_name`` on ``cls``.

    Walks the MRO mirroring ``walk_service_methods`` but returns
    functions (not bound methods), along with the MRO class whose
    ``vars()`` holds the function — needed by
    :func:`_build_method_wrappers` for inherited-method TypeVar
    substitution.
    """
    for klass in cls.__mro__:
        attr = vars(klass).get(method_name)
        if attr is not None:
            return klass, attr
    raise ServiceProtocolError(f"Method {method_name!r} not found on {cls.__name__}'s MRO.")
