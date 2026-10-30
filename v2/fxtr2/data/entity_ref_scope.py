"""Core Python API for entities, entity references, and scopes.

Entities in fxtr are canonically defined by their serialized form as FxtrCbor. However, for use
in Python code, they need to be represented and manipulated as Python objects. This module provides
the core Python API for:
- Entity classes: Python classes that represent fxtr entities and know how to serialize themselves
  to FxtrCbor.
- The Entity deserialization registry: a global registry of entity deserialization handlers,
  which know how to deserialize fxtr entities into Python Entity objects.
- EntityRef[T]: A reference to an entity of type T, which bundles together both the entity ID and
  the target type to deserialize to.
- FxtrScope: A context manager that represents the lifetime of stored and loaded fxtr entities,
  and which is responsible for actually loading and storing entities to and from a backing store
  as well as caching loaded entities when possible.

Scope lifecycle:

The scope's lifetime is expressed purely through lease drain — there is no separate "keep
alive" mechanism, and scope lifetimes are owned by the scope itself rather than by the memory
store. **Construction is inert**: ``FxtrScope(...)`` records configuration only; the token,
arena, parent leases, and marker are created at *entry*. Entry is either ``async with scope:``
(opens an :mod:`anyio` task group + closing event — every CM-entered scope is *within-lifespan*
and can host services) or :meth:`FxtrScope.lowlevel_start_lifespanless` (no task group; the root
scope and lowlevel callers; cannot host services). There is no autostart and no ``mountable=``
axis.

Every scope's token is uniformly a ``DerivedLocalLifetime`` over a private key ULL
(:meth:`FxtrScope._do_open`) plus each direct parent's main token, so child-keeps-parent-alive
is structural (a live ``Lease[child.main]`` carries each ``Lease[parent.main]`` as a via).

Authorization is two-layered (see ``plans/phase3/stage12_better_scope/new_scope_design.md`` §2):
a scope-level **base** (``_scope_auth_lease``, an attribute, usable from any task while open) and
per-context **overrides** (``with scope.authorize(lease):``, the single module-level
``_AUTH_OVERRIDES`` contextvar). ``_current_auth_lease`` resolves **base-first** (a live
base authorizes any context; the override is consulted only when the base is gone).
A scope is considered *draining* while its token is alive but the base lease has been dropped
(to None). :meth:`FxtrScope.check_authorized` (token created + this context resolves a live authorization)
guards user-facing ops; internal helpers (e.g. ``_lift_unchecked``) mint ancestor leases from
their own bridge graph so in-flight / child-scope work completes during drain.

Drain itself is :meth:`FxtrScope.aclose` (drop the base lease, then wait for the token to end)
or the sync :meth:`FxtrScope.force_close` (revoke the derived token + the key ULL immediately).
The async CM wraps these in ``try: yield; except: force_close; raise; else: aclose``. Scope-owned
foreign state (deep-fetch sources, cap-bridges) is torn down in a token-end callback rather than
at the start of ``aclose``, so it stays usable for in-flight operations during the drain window.

Low-level note: There is an asymmetry between entity serialization and deserialization: when
serializing an entity, the object already has a concrete Python type, but when deserializing an
entity, we don't know what type it should have, and it might be a tagged union of multiple possible
types. Additionally, in the presence of Python type variables, things become even more tricky,
because going from a Python type annotation like Foo[Bar | Baz] to a python object Foo (containing
perhaps a list of Bar and Baz objects) loses the information about the generic parameter
`Bar | Baz`, and then when we subsequently serialize Foo we may not know how to reconstruct its
internal objects upon deserialization. To keep the system predictable, fxtr imposes the following
contract:
- Entity objects must be able to _serialize_ themselves without knowing their generic parameters,
  and therefore the serialized form of an entity must not depend on its generic parameters. (Of
  course, the serialized value may depend on the _runtime types_ of its internal objects, just not
  the actual type annotation, because this is lost at runtime.)
- When entity objects serialize themselves, they must also declare a default type annotation that
  should be used to deserialize themselves, such that it is possible to load the entity again using
  that type annotation. However, this type annotation does NOT have to perfectly reconstruct the
  original object; instead, it is sufficient that it reconstruct _some_ object that serializes to
  the same FxtrCbor.
  - This means that if we take a Foo[Bar | Baz] and store it and then immediately load it again,
    without informing deserialization about what specific type annotation to use, it is _possible_
    that the loaded object is a Foo[UnknownEntity] or similar, where the children are some fallback
    type.
  - However, Entity subclasses are _allowed_ to do type introspection to construct a more specific
    type annotation object at runtime, in which case fxtr will preserve that type annotation in the
    runtime state of the EntityRef and use it to load the object.
- Entity class objects are _not_ directly responsible for deserializing themselves, because (a) we
  may need generic parameters to correclty deserialize an entity, and (b) it should be possible to
  resolve unions of entities or even return "duck-typed" entities that don't have a known Python
  type at typechecking time. Instead, Entity _type annotations_ are responsible for deserializing
  a value to some entity object consistent with the annotation. This mechanism uses a special
  entity deserialization registry, and allows users to customize the decoding process by defining
  a Python type alias, without defining a new Python class.
- To handle unions of entities, every registered deserializer must implicitly specify a _set_ of
  $fxtr.type IDs that it knows how to deserialize.
"""

from __future__ import annotations

import abc
import collections
import contextlib
import contextvars
import dataclasses
import functools
import inspect
import logging
import types
import typing
from collections.abc import Collection
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    Generator,
    Generic,
    Iterable,
    Literal,
    Mapping,
    NewType,
    Protocol,
    Self,
    Sequence,
    TypeAliasType,
    overload,
)

import anyio
import anyio.abc

from fxtr2.conversion import base_registry
from fxtr2.data import abstract_arena, arena_util, memory_arena
from fxtr2.data.entity_core import (
    FrozenFxtrCbor,
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
)
from fxtr2.debug import scope_instrumentation as _scope_debug
from fxtr2.leases.lease_bridges import (
    DependentLocalBridge,
    DependentPipelinedBridge,
    IndependentLocalBridge,
    IndependentPipelinedBridge,
    LocalBridge,
    LocalOrPipelinedBridge,
)
from fxtr2.leases.lease_types import Lease, LifetimeBrand, LifetimeToken
from fxtr2.leases.leased_cap_ir import IRHandle, UnwrapForPipelining
from fxtr2.leases.local_lease import (
    DerivedLocalLifetime,
    LocalLifetimeLease,
    LocalLifetimeToken,
    UnconditionalLocalLifetime,
)
from fxtr2.services.cap_ir_arena import ArenaIRHandleAdapter
from fxtr2.util.anyio_util import create_task_group

# creation_site() walks past this module's frames to the user's construction site.
_scope_debug.register_internal_file(__file__)

logger = logging.getLogger(__name__)


GLOBAL_MEMORY_STORE = contextvars.ContextVar[memory_arena.MemoryStore](
    "GLOBAL_MEMORY_STORE", default=memory_arena.MemoryStore()
)
"""The global shared memory store used by all scopes."""

# Keep the old name as an alias for backward compatibility during migration.
GLOBAL_BACKING_STORE = GLOBAL_MEMORY_STORE

# =========================================================================
# Entity serialization and deserialization
# =============================================================================


class Entity(abc.ABC):
    """Base class for all entities.

    A Fxtr entity object is a Python object that knows how to serialize itself to CBOR following
    fxtr's entity rules.
    """

    @abc.abstractmethod
    def _fxtr_store_impl(self) -> EntityRef[Self]:
        """Store the entity into the active scope and return an EntityRef.

        This method is called internally by fxtr when storing an entity. Entity subclasses must
        implement this to (a) serialize the entity to CBOR, (b) store the CBOR into the active
        scope (usually using `scope.store_cbor`), and (c) return an EntityRef declaring the default
        type that should be used when loading the entity again.

        The returned EntityRef should be capable of loading the entity data without running into
        deserialization errors. Additionally, storing the resulting entity should always produce
        the same FxtrID (and should generally also produce the same EntityRef). It is not required
        that loading the EntityRef produce an equal _Python_ object, and in particular the loaded
        form may lose information about generic parameters to the entity type. However, subclasses
        are encouraged to return an EntityRef that is as close to round-trippable as possible, by
        e.g. using type introspection where applicable.

        Returns:
            An EntityRef referring to the stored entity and declaring the default type that should
            be used when loading the entity again. The FxtrID of the returned EntityRef must be
            tracked in the active scope.
        """
        raise NotImplementedError("Entity subclasses must implement _fxtr_store_impl")

    @typing.final
    def store(self, scope: FxtrScope | None = None) -> EntityRef[Self]:
        """Store the entity into the active scope and return an EntityRef.

        Args:
            scope: The scope to store the entity in. Defaults to the active scope.

        Returns:
            An EntityRef referring to the stored entity, which can be used to load the entity again.
            Note that the loaded entity may or may not be equal to the original Python object
            according to Python, but will always serialize to the same fxtr data.
        """
        return _resolve_scope(scope).store(self)


type EntityTypeAnnotation[T: Entity] = (
    type[T] | TypeAliasType | NewType | types.UnionType | types.GenericAlias
)
"""The type of a type annotation for an entity that is a subtype of T, e.g. `FooEnt | BarEnt[Baz]`.

A value of type EntityTypeAnnotation[T] can be looked up in the entity deserialization registry
to find a deserializer that will convert a serialized form of a fxtr entity to a Python object of
type T (or error if the serialized form is incompatible).

Note that the Python typechecker cannot guarantee that a given value can be looked up at runtime
in the registry. Annotating a value as EntityTypeAnnotation[T] implies that the caller promises
the given value is a type annotation that can be looked up in the registry.
"""


@dataclasses.dataclass(frozen=True)
class DeserializedEntity[T]:
    """The result of deserializing an entity.

    This is a wrapper around the deserialized entity that provides additional metadata about the
    deserialization process.

    Attributes:
        value: The deserialized entity value.
        cacheable: Whether the entity is immutable and safe to cache. If True, the deserializer
            guarantees that the object does not support observable mutation and can safely be
            cached, so that repeated loads of the same entity reference will re-use the same
            object without re-creating it. If True, the returned object must be hashable.
    """

    value: T
    cacheable: bool = False


class EntityLoadHandler[T: Entity](Protocol):
    """Protocol for an entity deserializer.

    Entity deserializers are responsible for deserializing entity CBOR objects into a user-facing
    Python object.
    """

    async def supports_entity_type(
        self,
        type_id: FxtrID,
        target_type: EntityTypeAnnotation[T],
    ) -> bool:
        """Check if the deserializer can deserialize the given type ID into this annotated type.

        This method will always be called in a scope that can load `type_id`, and is allowed to
        `type_id` or anything referenced by it in that scope. Other than this, its result must
        be deterministic given its inputs and must have no side effects. The result of calling this
        may be cached.

        Args:
            type_id: The type ID of the entity to deserialize, retrieved from the "$fxtr.type"
                field of the CBOR.
            target_type: The annotated type of the entity. Will not be a union type; each
                variant of a union is independently checked by `can_deserialize`.

        Returns:
            True if the deserializer can deserialize the given type ID into this annotated type,
            False otherwise.
        """
        ...

    async def from_lowlevel_entity_data(
        self,
        cbor: FxtrCbor,
        target_type: EntityTypeAnnotation[T],
    ) -> DeserializedEntity[T]:
        """Deserialize an entity from CBOR.

        This method will always be called in a scope that can load every FxtrID referenced in the
        `cbor` argument. It is assumed that `can_deserialize` has already been called and returned
        True for the given `type_id` and `target_type`.

        Child entities referenced by `cbor` may be loaded by the implementation using the active
        scope. If the implementation reports that the returned entity is immutable, the value will
        be cached along with any child entities loaded from the active scope. No other side effects
        are allowed, and the implementation should not access any other scope other than the
        active scope.

        Args:
            cbor: The CBOR to deserialize.
            target_type: The annotated type of the entity. Will not be a union type; unions are
                resolved by `can_deserialize` before calling `deserialize`.

        Returns:
            The deserialized entity with metadata indicating whether the Python form of the entity
            is immutable. It is an error to return a mutable Python object and declare it immutable.
            If the entity is declared immutable, fxtr is free to cache and re-use the same Python
            object for repeated loads, without re-creating the object; this would be unsafe for
            mutable objects.
        """
        ...


_ENTITY_LOAD_REGISTRY = base_registry.TypeAnnotationHandlerRegistry[
    Entity, EntityLoadHandler[Entity]
]()

# =============================================================================
# EntityRef
# =============================================================================


@functools.cache
def _type_to_str(ty: EntityTypeAnnotation) -> str:
    origin = typing.get_origin(ty)
    if origin is types.UnionType or origin is typing.Union:
        return " | ".join(map(_type_to_str, typing.get_args(ty)))
    elif isinstance(ty, type):
        return ty.__name__
    else:
        return str(ty)


class EntityNotLoadedError(RuntimeError):
    """Error raised when attempting to access an entity via an EntityRef that is safely loaded.

    This error is raised when accessing the .val of an EntityRef when either (a) the value has not
    yet been cached, or (b) the cached value is unsafe to use due to concurrent changes to the
    arena. In either case, the entity should be asynchronously loaded via .load() instead.
    """

    def __init__(self, ref: EntityRef, message: str):
        self.ref = ref
        super().__init__(message)


def _resolve_scope(scope: FxtrScope | None) -> FxtrScope:
    if scope is None:
        scope = get_active_scope()
    return scope


_CACHED_HANDLER_SUPPORT_RESULTS: dict[
    tuple[base_registry.MatchedHandler[Entity, EntityLoadHandler[Entity]], FxtrID], bool
] = {}


async def handler_supports_entity_type(
    matched_handler: base_registry.MatchedHandler[Entity, EntityLoadHandler[Entity]],
    type_id: FxtrID,
) -> bool:
    """Check if a handler supports the given entity type, with caching.

    Args:
        matched_handler: The matched handler to check.
        type_id: The type ID of the entity to check.
    """
    cache_key = (matched_handler, type_id)
    if cache_key in _CACHED_HANDLER_SUPPORT_RESULTS:
        return _CACHED_HANDLER_SUPPORT_RESULTS[cache_key]
    async with FxtrScope():
        result = await matched_handler.entry.handler.supports_entity_type(
            type_id, matched_handler.subquery
        )
    _CACHED_HANDLER_SUPPORT_RESULTS[cache_key] = result
    return result


T_co = typing.TypeVar("T_co", covariant=True, bound=Entity)


class EntityRef(Generic[T_co]):
    """A reference to an entity in the fxtr graph."""

    entity_id: FxtrID
    target_type: EntityTypeAnnotation[T_co]

    def __init__(self, entity_id: FxtrID, target_type: EntityTypeAnnotation[T_co]):
        if target_type is Entity:
            raise TypeError(
                "EntityRef cannot be created with the Entity base class as the target type!"
                " EntityRef requires a concrete target type or type alias that has a registered"
                " load handler. If you want to load an entity without knowing what type it is,"
                " consider using UnknownEntity from"
                " fxtr2.conversion.converters.unknown_entity.\n\nNote: If you are seeing this error"
                " while loading a generic entity class, it is possible you forgot to specify a"
                " concrete type for a generic type parameter (e.g. `MyClass[T: Entity]`). In this"
                " case you can change the default behavior to always load as UnknownEntity when no"
                " generic parameters are passed by instead declaring your type as `MyClass[T:"
                " LoadAsUnknownByDefault[Entity]]`"
            )
        _ = _ENTITY_LOAD_REGISTRY.find_handler_or_union_handlers(target_type)
        object.__setattr__(self, "entity_id", entity_id)
        object.__setattr__(self, "target_type", target_type)

    def __eq__(self, other: Any) -> bool:
        return (
            type(other) is type(self)
            and self.entity_id == other.entity_id
            and self.target_type == other.target_type
        )

    def __hash__(self) -> int:
        return hash((type(self), self.entity_id, self.target_type))

    @property
    def val(self) -> T_co:
        return _resolve_scope(None).get_cached_value(self)

    async def load(self, scope: FxtrScope | None = None) -> T_co:
        return await _resolve_scope(scope).load(self)

    def __repr__(self):
        typestr = _type_to_str(self.target_type)
        return f"<EntityRef {self.entity_id.summary()} type={typestr}>"

    def replace_target_type[
        U: Entity
    ](self, new_target_type: EntityTypeAnnotation[U]) -> EntityRef[U]:
        """Replace the target type of the ref with a new type.

        Args:
            new_target_type: The new target type to replace the current target type with.

        Returns:
            A ref that points to the same entity data but will be loaded with a new target type.
        """
        return EntityRef(self.entity_id, new_target_type)

    async def copy_to(self, scope: FxtrScope, *, from_scope: FxtrScope | None = None) -> Self:
        """Copy the entity to another scope (usually from the active scope).

        Args:
            scope: The scope to copy the entity to.
            from_scope: Optional, the scope to copy the entity from. Defaults to the active scope.

        Returns:
            The ref itself, for convenient chaining.
        """
        return await scope.copy_from_scope(self, _resolve_scope(from_scope))

    async def copy_from(self, scope: FxtrScope, *, to_scope: FxtrScope | None = None) -> Self:
        """Copy the entity from another scope (usually the active scope).

        Args:
            scope: The scope to copy the entity to.
            to_scope: Optional, the scope to copy the entity to. Defaults to the active scope.

        Returns:
            The ref itself, for convenient chaining.
        """
        return await _resolve_scope(to_scope).copy_from_scope(self, scope)

    def lift(self, scope: FxtrScope | None = None) -> Self:
        """Lift the entity to a given scope (usually the active scope).

        Args:
            scope: The scope to lift the entity to. Defaults to the active scope.

        Returns:
            The ref itself, for convenient chaining.
        """
        return typing.cast(Self, _resolve_scope(scope).lift(self))


def register_entity_load_handler[
    T: Entity
](
    for_type: base_registry.RegistryKey[T],
    handler: EntityLoadHandler[T],
    allow_implicit_overrides: bool = False,
) -> None:
    """Register an entity load handler for the given type.

    Handlers can be registered for base Python classes or runtime-introspectable type aliases
    (e.g. PEP 695-style `type Foo = ...` aliases or NewType objects). Handlers registered for
    base classes will be automatically applied to subclasses, and handlers for most types will also
    apply to aliases of those types.

    By default, we expect exactly one handler to be registered for each type annotation. If multiple
    handlers are registered for the same type annotation, less-specific handlers will be ignored
    if they have allow_implicit_overrides=True, or an error will be raised otherwise.

    To declare a handler for a type alias that _explicitly overrides_ the default laoding behavior
    for the annotated value (e.g. to use custom logic for deserializing a union), you can use the
    OverrideFxtrHandlers sentinel annotation, e.g.

    ```
    type MyAlias = OverrideFxtrHandlers[Foo | Bar]
    register_entity_load_handler(MyAlias, CustomHandler())
    # now CustomHandler will be used to deserialize EntityRef(..., MyAlias), ignoring any handlers
    # registered for Foo or Bar.
    ```

    Args:
        key: The key to register the handler for.
        handler: The handler to register.
        allow_implicit_overrides: Whether to allow this handler to be overridden by a more specific handler.
            In this case the given handler acts like a "default implementation" for types that do
            not have more specific handlers. If False (the default), it is an error for two handlers
            to overlap (e.g. to define a handler for both a base class and a subclass). Note that
            including OverrideFxtrHandlers in a type annotation will always override any existing handlers.
    """
    _ENTITY_LOAD_REGISTRY.register_handler(
        key=for_type,
        handler=typing.cast(EntityLoadHandler[Entity], handler),
        allow_implicit_overrides=allow_implicit_overrides,
    )


class _DisallowAnyHandler(EntityLoadHandler[Any]):

    _ERROR_MESSAGE = (
        "EntityRef[Any] is not allowed at conversion boundaries, because it is impossible to"
        " infer what concrete value the entity should be loaded as. Use"
        " EntityRef[UnknownEntity] if you want to load an arbitrary entity data, or FxtrID if"
        " you just want to pass around an entity of an unknown type."
    )

    async def supports_entity_type(
        self,
        type_id: FxtrID,
        target_type: EntityTypeAnnotation[Any],
    ) -> bool:
        raise TypeError(self._ERROR_MESSAGE)

    async def from_lowlevel_entity_data(
        self, cbor: FxtrCbor, target_type: EntityTypeAnnotation[Any]
    ) -> DeserializedEntity[Any]:
        raise TypeError(self._ERROR_MESSAGE)


register_entity_load_handler(Any, _DisallowAnyHandler())  # type: ignore[arg-type]

# =============================================================================
# Scopes
# =============================================================================


@dataclasses.dataclass(frozen=True)
class RefLoadCacheEntry[T: Entity]:
    """A cache entry for a loaded value of an EntityRef.

    Having a cache entry implies that the loaded value was immutable and is safe to reuse.

    Attributes:
        ref: The ref that was loaded.
        value: The loaded value.
        side_effect_loads: Cache entries for other refs that were loaded as side effects of loading
            this ref.
    """

    ref: EntityRef[T]
    value: T
    side_effect_loads: tuple[RefLoadCacheEntry[Any] | RefLoadUncacheableMarker[Any], ...]


@dataclasses.dataclass(frozen=True)
class RefLoadUncacheableMarker[T: Entity]:
    """Cache entry indicating that a ref was loaded, but the result was not cacheable.

    Attributes:
        ref: The ref that was loaded.
        side_effect_loads: Cache entries for other refs that were loaded as side effects of loading
            this ref.
    """

    ref: EntityRef[T]
    side_effect_loads: tuple[RefLoadCacheEntry[Any] | RefLoadUncacheableMarker[Any], ...]


@dataclasses.dataclass
class DeepFetchSourceEntry[B_Y: memory_arena.LifetimeBrand]:
    """Scope-side record for a deep-fetch source registered on a scope.

    Stored on ``FxtrScope._deep_fetch_sources`` as
    ``DeepFetchSourceEntry[Any]``. The scope owns ``bridge`` (its at-rest
    leases were ``.take()``-ed by ``register_deep_fetch_source``); on a
    deep-fetch hit, ``bridge.clone()`` is passed to
    ``MemoryArena.lazily_attach_from_arena`` so the memory store inherits
    its own clone of the bridge to mint per-load ``Lease[B_Y]`` values.
    There is no deregistration — namespaces must outlive their registering
    scope; on scope exit the stored bridge's at-rest leases are discarded.
    """

    source: abstract_arena.DeepFetchable[B_Y]
    """The deep-fetch-capable source (typically a namespace backend)."""

    bridge: LocalBridge
    """Scope-owned bridge for minting per-load ``Lease[B_Y]`` values when
    the source's ``try_lookup`` returns a hit."""


# =============================================================================
# Service references and capability bridges
# =============================================================================


type ServiceTypeAnnotation[T] = (
    type[T] | TypeAliasType | NewType | types.UnionType | types.GenericAlias
)
"""The type of a target-type annotation for a ``ServiceRef``.

Parallels :data:`EntityTypeAnnotation`, but unconstrained on T — service
target types are typically Protocols (or aliases / unions thereof), not
necessarily subclasses of any common base. The untyped service
uses ``type UntypedService = Any`` (a ``TypeAliasType``), which is why
plain ``type[T]`` is too narrow.
"""


@dataclasses.dataclass(frozen=True)
class ServiceKey:
    """Scope-side identity for a registered service: ``(handle, type_id)``.

    Two ``ServiceKey``s are equal iff both fields match. The scope's
    ``_known_handles`` set and each ``_CapBridgeEntry.handles`` set
    are keyed on ``ServiceKey`` rather than bare ``IRHandle``: a
    handle registered under two different ``service_type_id`` values
    counts as two distinct services. (In practice an impl mints
    exactly one type_id per handle so that case is rare; the design
    just treats ``ServiceKey`` as the unit of identity throughout.)

    The ``type_id`` is the wire-portable hash from the
    :class:`ServiceRef` — *not* required to be tracked in the scope.
    To introspect the loadable form (e.g. for ``EntityRef.load``),
    dispatch ``$fxtr.type`` over the handle.
    """

    handle: IRHandle[Any]
    type_id: FxtrID


@dataclasses.dataclass(frozen=True)
class ServiceRef[T]:
    """A portable reference to a live capability.

    A ``ServiceRef`` is the live-capability analog of ``EntityRef`` — a
    lightweight, copyable object that names a remote-or-local capability
    by its underlying ``IRHandle`` plus a runtime type identity
    (``service_type_id``) and the Python type annotation the caller
    wants to view it as (``target_type``). It carries no lease; the
    lease comes from a scope's ``_cap_bridges`` at resolve / call time.

    Equality is value-based on ``(handle, target_type, service_type_id)``.
    Two refs to the same handle but different ``target_type`` or
    different ``service_type_id`` are distinct.

    The ``service_type_id`` is the wire-portable runtime identity (an
    arbitrary :class:`FxtrID` minted by the impl — typically a builtin
    like ``FxtrID.builtin("untyped_service")``, but could also be a
    stored type-descriptor entity for typed services). It is *not*
    required to be tracked in any scope that knows about this ref —
    the wire only ships the data-id half (FxtrID equality ignores the
    provenance parent), so callers treat it as an opaque hash. To
    introspect the loadable form (suitable for ``EntityRef.load``),
    dispatch ``$fxtr.type`` over the handle, which returns the same
    id with the impl's tracking step routed through the per-call
    return arena.

    Use ``await ref.resolve(scope)`` to get a ``T`` bound to ``scope``.
    """

    handle: IRHandle[Any]
    target_type: ServiceTypeAnnotation[T]
    service_type_id: FxtrID

    async def resolve(self, scope: FxtrScope | None = None) -> T:
        """Resolve the ref into a usable ``T`` against ``scope``.

        Looks up the closest scope that has ``self`` registered
        (walking ``scope`` → parents), builds a
        :class:`ServiceProxyContext` bound to ``(scope, self.handle,
        self.service_type_id)``, and invokes the registered
        :class:`ServiceResolver` for ``self.target_type`` with that
        context.

        ``scope`` defaults to ``FxtrScope.get_active()``. Equivalent to
        ``await scope.resolve_service(self)``.
        """
        return await _resolve_scope(scope).resolve_service(self)

    async def bridge_from_scope(
        self,
        source_scope: FxtrScope,
        into_scope: FxtrScope | None = None,
        *,
        keepalive_via: Literal["independent_lease", "concurrent_child_bridge"],
    ) -> None:
        """Bridge this ref from ``source_scope`` into ``into_scope``.

        Convenience for ``into_scope.bridge_service_from_scope(self,
        source_scope, keepalive_via=…)``. ``into_scope`` defaults to the active
        scope.

        See :meth:`FxtrScope.bridge_service_from_scope` for the lifetime
        semantics and the meaning of ``keepalive_via``.
        """
        await _resolve_scope(into_scope).bridge_service_from_scope(
            self, source_scope, keepalive_via=keepalive_via
        )

    def replace_service_type[U](self, new_target_type: ServiceTypeAnnotation[U]) -> ServiceRef[U]:
        """Return a copy of this ref with a different ``target_type`` annotation.

        The handle and ``service_type_id`` are preserved; only the
        Python target-type annotation changes. Useful in the typed-
        service layer's wire-decode path, where every ``service_ref``
        leaf decodes to ``ServiceRef[UnknownService]`` and needs to be
        retyped against a typed-method's annotation.

        Args:
            new_target_type: The new target-type annotation.

        Returns:
            A new ``ServiceRef[U]`` with the same handle and
            ``service_type_id`` but the new ``target_type``.
        """
        return ServiceRef(
            handle=self.handle,
            target_type=new_target_type,
            service_type_id=self.service_type_id,
        )

    def lift(self, scope: FxtrScope | None = None) -> Self:
        """Lift this service ref into ``scope`` (default: active scope), like :meth:`EntityRef.lift`.

        Ensures the :class:`ServiceKey` ``(handle, service_type_id)`` is registered
        locally, lifting the cap-bridge from a parent if needed — the same
        bridge setup :meth:`FxtrScope.resolve_service` performs before invoking
        the resolver. ``service_type_id`` itself is *not* lifted (it's wire-
        portable identity, not arena-tracked content).
        """
        return typing.cast(Self, _resolve_scope(scope).lift(self))


class FxtrServiceImpl[T](abc.ABC):
    """Base ABC for objects that can be mounted as a service.

    The single abstract method is ``async _mount_as_fxtr_service(scope)``.
    The implementation typically brings up its dispatch machinery inside a
    lifespan it attaches to ``scope`` (via
    :meth:`FxtrScope.attach_lifespan_to_scope`, which owns the task group and
    teardown), constructs an ``IRHandle`` wrapping ``self``, calls
    ``scope.mount_lowlevel_cap_as_service`` to register it, and returns the
    resulting ``ServiceRef`` (surfaced out through the lifespan's yielded
    value).

    Active-scope contract: :meth:`FxtrScope.mount` sets the mount scope as the
    active scope before calling ``_mount_as_fxtr_service``, and ``scope`` is
    *also* passed explicitly. Implementations may use either the explicit
    ``scope`` arg or scope-relative APIs (mirroring how
    :meth:`Entity._fxtr_store_impl` reads its target scope) — including any
    sub-stores during ``_service_type_id`` etc.

    The impl creates its own dispatch task group inside the lifespan it
    attaches, so dispatch outlives the mount-scope CM body (and drains
    structurally with the token).

    The ABC is deliberately bare — it does not define a method-discovery
    convention or a marshalling protocol. Those belong to specific
    implementation strategies (untyped, mashumaro-typed, etc.).
    """

    @abc.abstractmethod
    async def _mount_as_fxtr_service(self, scope: FxtrScope) -> ServiceRef[T]: ...


@dataclasses.dataclass(frozen=True)
class ServiceProxyContext:
    """Low-level scope-side API a service proxy uses against its home scope.

    Created by :meth:`FxtrScope.resolve_service` once per resolve and
    handed to the registered :class:`ServiceResolver`. Encapsulates
    every scope-internal operation a proxy needs (lease minting, arena
    forks, etc.) so proxies don't reach into ``FxtrScope`` private
    members directly.

    Active only while ``home_scope`` is alive: every method calls
    ``home_scope.check_authorized()`` first, so once drain begins all
    operations raise ``RuntimeError``. The context itself is stateless
    apart from the (``home_scope``, ``proxy_handle``,
    ``service_type_id``) triple, so it is cheap to rebuild — callers
    can either cache it for the lifetime of a resolved proxy or
    re-derive at each use.

    Attributes:
        home_scope: The scope this proxy is bound to. Public so resolvers
            and proxies can use scope-level public APIs (e.g.
            ``home_scope.is_tracked``, ``home_scope.as_active_scope``).
        proxy_handle: The proxy's own ``IRHandle`` — typically used to
            build a :class:`ServiceKey` for
            :meth:`mint_lease_for_key` to mint outer-dispatch leases.
        service_type_id: The wire-shipped type identity from the
            resolved :class:`ServiceRef`. Wire-portable hash — *not*
            required to be tracked in ``home_scope``. Useful as an
            opaque hash for fast-path comparison / cache lookup.
            Callers that need the id in *loadable* form (i.e.
            tracked locally and suitable for ``EntityRef.load``)
            should invoke ``$fxtr.type`` over the wire — e.g.
            :meth:`UnknownServiceProxy.service_type_id` — which
            returns the same id with the home-scope arena bridge in
            place.
    """

    home_scope: FxtrScope
    proxy_handle: IRHandle[Any]
    service_type_id: FxtrID

    @property
    def proxy_key(self) -> ServiceKey:
        """The :class:`ServiceKey` for the proxy's own handle."""
        return ServiceKey(self.proxy_handle, self.service_type_id)

    def mint_lease_for_key(
        self, key: ServiceKey
    ) -> Lease[Any] | UnwrapForPipelining[Lease[Any], Any]:
        """Mint a lease for ``key`` via the home scope's bridge graph.

        Pass ``self.proxy_key`` for the proxy's own outer-dispatch
        lease; pass any other ``ServiceKey`` the home scope knows
        about (e.g. one built from a ``ServiceRef`` arg's
        ``handle`` + ``service_type_id``) for that key's lease.

        The returned value may be a concrete ``Lease`` or an
        ``UnwrapForPipelining``. Either form is a valid Cap-IR call
        argument — the dispatch path's ``_unwrap_request_futures``
        resolves any ``UnwrapForPipelining`` before the receiver's
        ``_run_request_async`` sees the packet, so callers can put
        either form into a packet's leases list directly.

        Args:
            key: The :class:`ServiceKey` to mint a lease for. Must be
                registered in ``home_scope`` or one of its ancestors.

        Returns:
            ``Lease[key.handle.token]`` or
            ``UnwrapForPipelining[Lease[...], ...]``.

        Raises:
            ValueError: ``key`` is not registered in the home scope or
                any ancestor.
            RuntimeError: ``home_scope`` is draining or closed.
        """
        self.home_scope.check_authorized()
        # Lift on first use: walk parents (if needed), install a fresh
        # ``IndependentLocalBridge`` / ``IndependentPipelinedBridge`` on
        # the home scope so subsequent mints — both ours and any later
        # resolves of the same key — go straight through the local
        # entry. Symmetric with ``resolve_service``: every key
        # reachable from the home scope gets a co-located bridge on
        # first contact.
        entry = self.home_scope._lift_bridge_for_service_key(key)
        if entry is None:
            raise ValueError(
                f"ServiceKey {key!r} is not registered on home scope "
                f"{self.home_scope!r} or any of its ancestors."
            )
        # The lifted entry lives on the home scope, so its dependent
        # converter (if any) wants ``base = Lease[home_scope.main]``.
        return self.home_scope._mint_lease_via_ancestor_bridge(
            self.home_scope._entered_token, entry.bridge
        )

    def fork_arena_handle_for_ids(
        self, ids: Collection[FxtrID]
    ) -> tuple[IRHandle[Any], Lease[Any], list[dict[str, Any]]]:
        """Fork the home scope's main arena to cover ``ids``; return a wired bundle.

        Each ID is auto-lifted from ancestor scopes if not yet tracked
        directly in the home scope. The returned ``IRHandle`` is an
        :class:`ArenaIRHandleAdapter` wrapping the fork; the lease is
        on the home scope's main token (matching the handle's token);
        the precomp list is a closed precomp closure over ``ids``,
        ready to ship as the ``initial_precomp`` field of the wire
        ``fxtr_arena`` packet so the receiver can sync-decode any
        provenance-bearing FxtrIDs in the same packet.

        Requires the home scope to be mountable (the adapter needs the
        scope's task group to spawn its dispatch tasks).
        """
        self.home_scope.check_authorized()
        if self.home_scope._task_group is None:
            raise RuntimeError(
                f"Home scope {self.home_scope!r} has no task group; cannot fork "
                "arenas (lifespanless scopes can't host services)."
            )
        for fid in ids:
            self.home_scope.lift(fid)
        sub_arena = self.home_scope._main_arena.copy_to_new_arena(
            self.home_scope._current_auth_lease().clone(), list(ids)
        )
        handle = ArenaIRHandleAdapter(sub_arena, task_group=self.home_scope._task_group)
        lease = self.home_scope._current_auth_lease().clone()
        # Compute the precomp closure on the **fork** so every visited
        # ID lands in the fork's liveset; that's the server-side
        # guarantee that any precomp value the receiver may
        # sync-discover is ``load_flat``-able through the bridge. (See
        # ``v2/plans/phase3/stage8c_user_caps/part5bugfix1_plan.md``
        # for the invariant discussion. The fork's token aliases
        # home_main, so the home-main lease validates against it.)
        from fxtr2.services.cap_ir_arena import (
            collect_precomp_ids,
            compute_and_track_precomp_closure,
        )

        precomp = compute_and_track_precomp_closure(
            sub_arena,
            self.home_scope._current_auth_lease().clone(),
            ids,
        )
        # Register the introduction refcount: every id mentioned in
        # this packet contributes +1 to the adapter's
        # ``_total_introductions``. See the call in
        # ``service_cap_common._emit_call_result_packet`` for the
        # full pattern.
        handle.register_emission(set(ids) | collect_precomp_ids(precomp))
        return handle, lease, precomp


class ServiceResolver[T](Protocol):
    """How to resolve a ``ServiceRef`` into a usable user-facing object.

    Registered against a target type via :func:`register_service_resolver`;
    looked up by :meth:`ServiceRef.resolve` via the type-annotation registry.
    """

    async def __call__(
        self,
        context: ServiceProxyContext,
        target_type: ServiceTypeAnnotation[T],
    ) -> T:
        """Build a ``T`` proxy bound to ``context``.

        Args:
            context: The scope-side API surface the proxy will use.
                ``context.home_scope`` is the home scope, and
                ``context.proxy_handle`` is the proxy's underlying
                handle. All scope-internal operations the proxy needs
                are reachable through the context; resolvers / proxies
                should not reach into ``FxtrScope`` private members
                directly.
            target_type: The type the registry matched on. May be a more
                specific type than the registration key (e.g. a generic
                alias substituted with concrete params).
        """
        ...


_SERVICE_RESOLVER_REGISTRY: base_registry.TypeAnnotationHandlerRegistry[
    object, ServiceResolver[Any]
] = base_registry.TypeAnnotationHandlerRegistry()
"""Registry of ``ServiceResolver`` handlers by target-type annotation.

Starts empty; concrete resolvers (e.g. for ``UntypedService``) register
themselves at module import via :func:`register_service_resolver`. The
``object`` upper bound is just "anything goes" — service target types
range over arbitrary protocols, generic aliases, and type aliases.
"""


def register_service_resolver[
    T
](
    for_type: base_registry.RegistryKey[T],
    resolver: ServiceResolver[T],
    allow_implicit_overrides: bool = False,
) -> None:
    """Register a service resolver for the given target type or annotation.

    Mirrors :func:`register_entity_load_handler` for live capabilities:
    accepts a plain Python type, a ``TypeAliasType``, or a ``NewType`` as
    the key. Generic aliases / unions / etc. are unwrapped per
    ``TypeAnnotationHandlerRegistry``'s usual rules.
    """
    _SERVICE_RESOLVER_REGISTRY.register_handler(
        key=for_type,
        handler=typing.cast(ServiceResolver[Any], resolver),
        allow_implicit_overrides=allow_implicit_overrides,
    )


def _identity_dependent_local_converter(
    leases: dict[LifetimeToken[Any], Lease[Any]], base: Lease[Any]
) -> Lease[Any]:
    """Identity converter: discard any inputs, return ``base`` unchanged.

    Used by :meth:`FxtrScope.mount_lowlevel_cap_as_service` (whose
    bridge has no inputs at all — ``leases={}``) and by
    :func:`fxtr2.services.service_cap_common._make_chain_converter_handle`'s
    no-bridge-on-call_scope.main case. The converter follows the
    standard transfer convention: every entry in ``leases`` is
    discarded, ``base`` flows out as the returned lease.
    """
    for v in leases.values():
        v.discard()
    return base


@dataclasses.dataclass
class _CapBridgeEntry:
    """A cap-bridge plus the set of known handles that share it.

    A scope's ``_cap_bridges`` dict stores one entry per token. Two
    populating sources, distinguished by ``is_ancestor_or_arena_owner``:

    - **Service entries** (``is_ancestor_or_arena_owner=False``): created
      by ``mount_*`` / ``attach_*`` / eager-lift. Each handle registered
      against the entry is recorded in ``handles``; ``forget_service``
      drops the handle from the set; once it empties the entry is torn
      down and its lease(s) discarded.
    - **Ancestor / arena-owner entries**
      (``is_ancestor_or_arena_owner=True``): created at scope
      construction (one per direct parent + transitively for each strict
      ancestor) and by ``_register_external_arena_bridge`` (one per
      external arena owner whose lease this scope must hold for the
      lift-into-store path). The bridge is an
      :class:`IndependentLocalBridge` whose ``lease`` is a
      ``Lease[token]`` this scope holds for its own lifetime — used
      both as the inheritance bridge handed to the memory store on
      ``lift`` (sentinel mode) and as the source of ``base`` leases
      when invoking dependent bridges held in ancestor scopes.
      ``handles`` may still grow on these entries when a service
      handle happens to be mounted on an ancestor's main token (so
      ``handle.token == ancestor.main_token``); the entry doubles as
      that handle's service bridge. ``forget_service`` preserves these
      entries even when ``handles`` empties — the held lease is
      structural, not service-traffic state.

    Both kinds are torn down in ``_on_token_end`` (their leases get
    discarded). The scope is the sole owner of parent / arena-owner
    lifetime tracking — the memory store no longer holds any
    parent-lease clones itself.

    Equality is identity-based (mutable dataclass) so two entries on
    the same token are never accidentally conflated; the dict's
    one-entry-per-token invariant covers the structural side.
    """

    bridge: LocalOrPipelinedBridge
    handles: set[ServiceKey]
    is_ancestor_or_arena_owner: bool = False


StoreProvenance = Literal["auto", "if_marked", "strip"]
"""How a scope handles provenance when storing entities. See ``FxtrScope``."""


def _infer_scope_name(skip_frames: int) -> str:
    """Walk back ``skip_frames`` frames and return that caller's ``co_name``.

    ``skip_frames=1`` skips this helper itself, so the caller of the
    *caller* is returned. Used to fill in a default ``name`` for
    ``FxtrScope.__init__``.
    """
    frame = inspect.currentframe()
    for _ in range(skip_frames + 1):
        if frame is None:
            return "<unnamed>"
        frame = frame.f_back
    if frame is None:
        return "<unnamed>"
    return frame.f_code.co_name


# Per-context authorization *overrides*, unioned with each scope's scope-level
# base authorization (``FxtrScope._scope_auth_lease``). One process-wide
# ContextVar holding a copy-on-write ``{id(scope): Lease[main]}`` map, touched
# only by ``FxtrScope.authorize`` — never by ordinary scope entry/exit (the base
# is a plain attribute). Resolution is **base-first**: a live base authorizes any
# context and the override is consulted only when the base is gone (draining);
# there, a live override authorizes and a present-but-discarded one is a hard
# error. See the design doc (stage12 §2) for the rationale and resolution order.
_AUTH_OVERRIDES: contextvars.ContextVar[Mapping[int, "Lease[Any]"]] = contextvars.ContextVar(
    "fxtr_scope.AUTH_OVERRIDES", default=types.MappingProxyType({})
)

# Immutable empty mapping used as a safe default for the ``extra_derived_leases``
# / ``extra_leases`` parameters (never use a mutable ``{}`` as a default value).
_EMPTY_LEASE_MAP: Mapping[Any, "Lease[Any]"] = types.MappingProxyType({})


class AuthorizationHandle:
    """Handle for one :meth:`FxtrScope.authorize` override binding.

    Yielded by the ``authorize`` context manager so the holder can drop *this*
    override's lease early (before the block exits) — e.g. a lifespan task that
    must release its standing ``Lease[main]`` right after ``started()`` so the
    parked task doesn't pin the token's drain. Distinct from
    :meth:`FxtrScope.discard_base_authorization`, which drops the scope-level
    base.
    """

    __slots__ = ("_lease", "_discarded")

    def __init__(self, lease: "Lease[Any]") -> None:
        self._lease = lease
        self._discarded = False

    def discard(self) -> None:
        """Discard this override's lease (idempotent).

        Leaves the ``_AUTH_OVERRIDES`` map entry in place — restoration is the
        enclosing ``authorize`` block's job — so afterward the entry references a
        dead lease, which ``_current_auth_lease`` treats as a discarded override
        *only once the base is gone* (a live base still authorizes; §2).
        """
        if not self._discarded:
            self._lease.discard()
            self._discarded = True

    @property
    def is_discarded(self) -> bool:
        return self._discarded


class FxtrScope(anyio.AsyncContextManagerMixin):
    """A scope within which entities can be saved into IDs and retrieved again later.

    **Construction is inert.** ``FxtrScope(...)`` only records configuration; it
    does not create a token, take parent leases, build an arena, or run marker
    setup. None of those exist until the scope is *entered*. A constructed-but-
    unentered scope can be GC'd with no effect and no warning; no scope method
    works on it.

    **Isolation.** ``isolated=False`` (default) makes the scope inherit from the
    currently-active scope (captured at construction) and become the active
    scope while entered as a context manager. ``isolated=True`` does neither.

    **Entry routes.** Two ways to start a scope:

    - ``async with scope:`` — the normal route. Opens an ``anyio`` task group +
      a closing event (every CM-entered scope is *within-lifespan* and can host
      service mounts / lifespans), creates the scope's derived token, mints the
      base authorization lease, and (non-isolated) sets ``ACTIVE_SCOPE``.
      Delegates to :meth:`open_with_explicit_leases` with parent leases cloned
      from each parent's in-context authorization.
    - :meth:`lowlevel_start_lifespanless` — start without a task group / closing
      event (cannot mount/resolve services). Used for the root scope and
      lowlevel callers. Pair with :meth:`discard_base_authorization` or
      :meth:`force_close` to tear down.

    **Lifetime.** Every scope's token is a ``DerivedLocalLifetime`` over a
    private key ULL plus each direct parent's main token, so a live
    ``Lease[child.main]`` carries each ``Lease[parent.main]`` as a via —
    child-keeps-parent-alive is structural and a parent revoke cascades to
    children. The first authorization lease is retained as the scope-level
    **base** authorization (``_scope_auth_lease``); the bare key root lease is
    discarded (the ``_key_ull`` object is kept as a revoke handle).

    **Authorization.** Almost every operation requires authorization. Base
    authorization is the scope-level ``_scope_auth_lease`` attribute — visible
    in any task while the scope is open. Per-context *overrides* (see
    :meth:`authorize`) layer over the base for re-authorizing a scope from a
    context that holds a lease (e.g. a draining scope). ``_current_auth_lease``
    resolves override-then-base; see the module-level ``_AUTH_OVERRIDES``.

    **Drain and close.** ``await scope.aclose()`` drops the base lease (→ None,
    rejecting new ambient use everywhere) and waits for the token to end; the
    async CM wraps this. ``scope.force_close()`` is the sync revoke-only
    counterpart (revokes the derived token and the key ULL), used by
    atexit-style teardown and on exceptions inside the CM body. In-flight
    operations re-authorized via overrides keep working through drain.
    """

    def __init__(
        self,
        include: Sequence[FxtrScope] = (),
        isolated: bool = False,
        name: str | None = None,
        *,
        provenance_context: FxtrID | None = None,
        provenance_context_from_scope: FxtrScope | None = None,
        store_provenance: StoreProvenance = "auto",
    ):
        """Create a new (inert) scope, configured to include the given parents.

        **Construction is inert** (see the class docstring): this records
        configuration only — it does *not* create a token, take parent leases,
        build an arena, or run marker setup. The scope becomes usable once
        entered via ``async with scope:`` or
        :meth:`lowlevel_start_lifespanless`.

        - ``FxtrScope()`` (non-isolated): captures the currently-active scope as
          its parent; ``async with`` makes it the active scope for its body.
        - ``FxtrScope(isolated=True)``: no implicit active-scope parent, does not
          change ``ACTIVE_SCOPE`` on entry.

        Args:
            include: Additional parent scopes. Each becomes a derived-token
                input at entry, so a live ``Lease[self.main]`` carries each
                parent's main token as a via (structural parent-outlives-child).
            isolated: If True, do not implicitly include the active scope as a
                parent and do not change ``ACTIVE_SCOPE`` on CM entry. See
                ``as_active_scope()`` for explicit activation.
            name: Human-readable name for debugging. Defaults to the
                caller's ``co_name`` (inferred via ``inspect.currentframe()``).
                Empty strings are kept verbatim; only ``None`` triggers
                inference.
            provenance_context: If supplied, a c-node FxtrID identifying what
                this scope represents. Must unwrap to ``"."``. The scope's
                ``$fxtr.ctx`` marker becomes ``FxtrID(".",
                _provenance_parent=provenance_context)``, and that marker
                propagates to every entity stored into this scope (and is
                inherited by child scopes that don't supply their own).
                When ``None`` (the default), the scope inherits its marker
                from its first parent (or, for root scopes, falls back to
                the bare ``"."``). Applied at entry.
            provenance_context_from_scope: The scope in which
                ``provenance_context`` is already tracked. The c-node is
                synchronously copied (via ``track_from_memory_arena``) into
                this scope's main arena *at entry*, so that subsequent stores
                can always reach it without a parent-walk and without holding a
                lifetime reference back to the source. If ``None``, we
                walk parents to find one that already tracks the c-node.
                Required when the c-node was built in a sibling-style scope
                that isn't a parent. Ignored when ``provenance_context`` is
                ``None``.
            store_provenance: How this scope handles provenance during
                ``store_cbor``. ``"auto"`` (default) preserves provenance
                whenever the cbor's marker or any of its children carry a
                provenance parent — substituting the marker with the scope's
                own c-node or a lazily-minted fallback c-node when needed.
                ``"if_marked"`` preserves provenance only when the marker
                itself carries one (the previous default behavior).
                ``"strip"`` always discards child provenance on store and
                keeps the scope's marker bare; it cannot be combined with
                ``provenance_context``. Loads always preserve provenance
                regardless of mode. Does not inherit from the parent.
        """
        if name is None:
            name = _infer_scope_name(skip_frames=1)
        if store_provenance == "strip" and provenance_context is not None:
            raise ValueError(
                "store_provenance='strip' cannot be combined with"
                " provenance_context; strip mode keeps the scope's marker"
                " bare and refuses inbound provenance traces."
            )
        self._store_provenance: StoreProvenance = store_provenance
        # Provenance config is applied at *entry* (it needs the arena + ancestor
        # bridges), not at construction.
        self._provenance_context = provenance_context
        self._provenance_context_from_scope = provenance_context_from_scope
        self._init_common(include=include, isolated=isolated, name=name)

        # Inert: lifetime / arena / marker fields are created at *entry*
        # (``_do_open`` / ``lowlevel_start_lifespanless``). Until then they are
        # None and ``_started`` is False, so ``check_authorized`` raises a clear
        # not-started error for any operation on an unentered scope.
        self._key_ull: UnconditionalLocalLifetime[Any] | None = None
        self._derived: DerivedLocalLifetime[Any] | None = None
        # Extra (non-parent) derived-input tokens supplied at entry via
        # ``extra_derived_leases`` — tokens this scope gates on but that have no
        # parent FxtrScope (e.g. dispatch arg-ServiceRef tokens). Set at entry.
        self._extra_derived_tokens: tuple[LifetimeToken[Any], ...] = ()
        # Base scope-level authorization lease. Set at entry, dropped to None at
        # ``aclose``.
        self._scope_auth_lease: Lease[Any] | None = None
        self._token: LocalLifetimeToken[Any] | None = None
        # Backing field for the ``_main_arena`` property (set at entry). The
        # property narrows to non-None for the many post-entry read sites.
        self._main_arena_obj: memory_arena.MemoryArena[Any] | None = None
        self._context_marker: FxtrID | None = None
        self._fallback_marker: FxtrID | None = None
        # Within-lifespan machinery (chunk 2). The closing event is created at
        # CM entry (alongside the task group); lifespanless scopes leave it
        # None. Attached lifespans (``attach_lifespan_to_scope``) park on it and
        # tear down once it fires (after the token drains). ``_lifespan_count``
        # tracks attached-but-not-finished lifespan tasks; the last to finish
        # sets ``_lifespans_closed_event`` so ``aclose`` can wait for structured
        # teardown (it can't await the task group itself — it runs inside it).
        self._closing_event: anyio.Event | None = None
        self._lifespans_closed_event: anyio.Event | None = None
        self._lifespan_count: int = 0

    def _init_common(
        self,
        *,
        include: Sequence[FxtrScope],
        isolated: bool,
        name: str,
    ) -> None:
        """Config-only init body: parent resolution, ancestor DAG, lifecycle
        flags, task-group attribute, global store, and deep-fetch state.

        Runs at construction (inert): records *which* scopes are parents and
        captures the active scope for the non-isolated case, but does not mint
        any lifetime, take any parent lease, or validate parent liveness — all
        of that happens at entry.
        """
        self._isolated = isolated

        if not isolated:
            self._outer_scope: FxtrScope | None = get_active_scope()
            include = (self._outer_scope, *include)
        else:
            self._outer_scope = None

        # Debug metadata for the scope-drain report
        # (fxtr2/debug/scope_report.py); all cheap-always. The implicit
        # capture just above is classified same-task vs fork-inherited
        # (the latter is the hazard ingredient the report names); the
        # creation site is one short frame walk; the traceback is
        # captured only under FXTR_SCOPE_REPORT_TB.
        self._implicit_capture_cross_task: bool = (
            False if isolated else _scope_debug.classify_implicit_capture(self._outer_scope)
        )
        self._creation_site: str | None = _scope_debug.creation_site()
        self._creation_tb: str | None = _scope_debug.capture_creation_tb()
        self._is_dispatch_call_scope: bool = False
        self._is_dispatch_input_scope: bool = False
        self._created_at: float = _scope_debug.monotonic()
        self._drain_started_at: float | None = None
        self._lifespan_wait_started_at: float | None = None

        # Dedupe parents by identity, preserving order: a non-isolated scope
        # whose ``include=`` already names the active (outer) scope would
        # otherwise list it twice, and the entry path would clone its
        # authorization twice into a scope-keyed map (leaking the first clone).
        _seen_parents: set[int] = set()
        _deduped_parents: list[FxtrScope] = []
        for _p in include:
            if id(_p) not in _seen_parents:
                _seen_parents.add(id(_p))
                _deduped_parents.append(_p)
        self._parents: Sequence[FxtrScope] = tuple(_deduped_parents)
        self._name = name
        self._all_ancestor_scope_ids: set[int] = {id(pscope) for pscope in include}.union(
            {aid for pscope in include for aid in pscope._all_ancestor_scope_ids}
        )

        self._frozen = False
        self._is_root_scope = False

        # Lifecycle state. A scope is "draining" while its token is alive but the
        # base ``_scope_auth_lease`` has been dropped to None.
        # `_started` flips True at entry (token created). `_inside_cm` is a
        # re-entry guard while inside the async-CM body. `_closed` is set once
        # teardown completes (idempotency of aclose/force_close).
        self._started: bool = False
        self._inside_cm: bool = False
        self._closed: bool = False

        # Task group: opened at ``async with`` / ``open_with_explicit_leases``
        # entry (every CM-entered scope is within-lifespan), torn down via the
        # AsyncExitStack on exit. None for lifespanless / unentered scopes.
        self._task_group: anyio.abc.TaskGroup | None = None

        self._global_store: memory_arena.MemoryStore = GLOBAL_MEMORY_STORE.get()

        # Experimental: deep fetchability. A scope is deeply fetchable if every
        # node reachable from the tracked set can be directly fetched by ID.
        # Inferred from parents on first read (lazy commit via
        # ``_is_deeply_fetchable``); eagerly committed in the constructor
        # tail for auto-started scopes and in ``__aenter__`` for CM-required
        # scopes, so ``_mark_not_deeply_fetchable`` has a defined "before
        # commit" window.
        self._deeply_fetchable_flag: bool | None = None
        self._deep_fetch_sources: list[DeepFetchSourceEntry[Any]] = []

        # Live-capability state. Two parallel structures:
        #
        # - ``_known_handles``: the flat set of :class:`ServiceKey`s
        #   ``(handle, type_id)`` this scope is willing to resolve.
        #   Multiple keys can share the same ``handle.token`` (or even
        #   the same ``handle``, with different ``type_id``s); each is
        #   tracked individually so mounting one key does not
        #   "magically" make another key on the
        #   same token resolvable through this scope. Kept alongside the
        #   per-entry ``handles`` set so ``handle in _known_handles`` is
        #   O(1) regardless of how many tokens we know about.
        # - ``_cap_bridges``: keyed by ``handle.token``; each value is a
        #   :class:`_CapBridgeEntry` carrying the underlying bridge (one
        #   of the four :data:`LocalOrPipelinedBridge` variants) plus
        #   the subset of ``_known_handles`` whose token matches. The
        #   bridge is the lease-minting machinery shared by every known
        #   handle on its token; the per-entry ``handles`` set lets
        #   ``forget_service`` know in O(1) whether the bridge is still
        #   needed.
        #
        # Resolution walks ``self`` → ``self._parents`` for a scope that
        # has the handle in ``_known_handles``; on the first hit through
        # ``resolve_service`` / ``ServiceProxyContext.mint_lease_for_handle``
        # the bridge is *lifted* — a fresh ``IndependentLocalBridge`` /
        # ``IndependentPipelinedBridge`` entry is installed locally so
        # subsequent walks short-circuit and we no longer rely on the
        # parent not forgetting the handle.
        # Both structures are torn down in the token-end callback. Only
        # scopes with a task group accept new service bridges, but the dicts
        # exist on every scope so the parent walk is uniform.
        self._known_handles: set[ServiceKey] = set()
        self._cap_bridges: dict[LifetimeToken[Any], _CapBridgeEntry] = {}

        # Parent liveness is validated at *entry* (when we clone each parent's
        # in-context authorization), not at construction — an inert scope may
        # be constructed long before it is entered.

    def _init_arena_and_cache(self) -> None:
        """Build the scope's main arena + context marker, and set up the
        ref-load cache containers. Called at entry, after ``_scope_auth_lease``
        (the base authorization) has been set.

        Sets ``self._context_marker`` to the bare ``"."`` builtin. If the
        scope was configured with a ``provenance_context`` (or it inherits
        one from a parent), :meth:`_apply_context_marker_override` overrides
        the marker after ancestor cap-bridges are populated.
        """
        base = self._scope_auth_lease
        assert base is not None, "_init_arena_and_cache called before base auth was set"
        self._main_arena_obj = self._global_store.new_arena(base.clone())
        self._context_marker = self._main_arena.track_builtin(
            base.clone(), FxtrID.from_tag_and_payload(".", b"")
        )
        # Lazily-minted layered marker used by store_provenance="auto" when
        # the cbor's marker is bare but children carry provenance and the
        # scope has no explicit provenance_context. Populated on first use
        # in :meth:`_get_or_mint_fallback_or_own_marker`.
        self._fallback_marker: FxtrID | None = None
        # Cache keyed on (data_id, provenance_parent) so the same data ID
        # under two different provenance traces caches independently. Inner
        # dict is keyed on the load target type, same as before.
        self._ref_loaded_value_cache: collections.defaultdict[
            tuple[FxtrID, FxtrID | None],
            dict[EntityTypeAnnotation[Any], RefLoadCacheEntry],
        ] = collections.defaultdict(dict)
        # Loaded-since-abandon tracking: keyed on (data_id, prov_id, target_type)
        # tuples derived from a ref. EntityRef equality / hashing stays
        # provenance-blind (matches FxtrID); the provenance dimension is
        # added at the cache layer.
        self._refs_loaded_since_abandon: set[
            tuple[FxtrID, FxtrID | None, EntityTypeAnnotation[Any]]
        ] = set()
        self._load_tracer: contextvars.ContextVar[
            list[RefLoadCacheEntry[Any] | RefLoadUncacheableMarker[Any]] | None
        ] = contextvars.ContextVar(f"{self}._load_tracer", default=None)

    # -----------------------------------------------------------------
    # Entry core + authorization
    # -----------------------------------------------------------------

    def _do_open(
        self,
        parent_leases: Mapping[FxtrScope, Lease[Any]],
        extra_derived_leases: Mapping[LifetimeToken[Any], Lease[Any]] = _EMPTY_LEASE_MAP,
    ) -> None:
        """Sync entry core: mint the derived token + base authorization, build
        the arena, install ancestor bridges, run marker setup, mark started.

        The freshly-minted private key ULL is added to the derived-input set
        automatically. Shared by :meth:`open_with_explicit_leases` and
        :meth:`lowlevel_start_lifespanless`. Does **not** open a task group or
        set the active scope — the caller's CM does that. On any failure every
        supplied lease is discarded and any partial lifetime is revoked.

        Args:
            parent_leases: Exactly one live ``Lease[parent.main]`` per direct
                parent (``self._parents``), consumed as derived-token inputs.
                **Ownership is taken.**
            extra_derived_leases: Additional derived-token inputs keyed by
                **token** rather than parent scope — for tokens the scope must
                gate on that have no ``FxtrScope`` object (e.g. service-ref
                argument tokens in dispatch). Each gets a case-3 dependent
                ancestor entry (so handles on it can attach) but is **not** a
                parent scope (no transitive ancestor walk, not in the
                scope-ancestry DAG). **Ownership is taken.**

        Raises:
            RuntimeError: the scope is already started/closed, ``parent_leases``
                does not cover exactly the direct parents, or an
                ``extra_derived_leases`` token collides with a parent token.
        """

        def _discard_all_supplied() -> None:
            for lease in parent_leases.values():
                lease.discard()
            for lease in extra_derived_leases.values():
                lease.discard()

        if self._started or self._token is not None:
            _discard_all_supplied()
            raise RuntimeError(f"Scope {self!r} has already been started")
        if self._closed:
            _discard_all_supplied()
            raise RuntimeError(f"Scope {self!r} has already been closed")

        name = self._name
        parents = list(self._parents)
        # Validate parent coverage: exactly one lease per direct parent, no extras.
        missing = [p for p in parents if p not in parent_leases]
        extra = [p for p in parent_leases if p not in parents]
        if missing or extra:
            _discard_all_supplied()
            raise RuntimeError(
                f"_do_open: parent_leases must cover exactly {parents!r}; "
                f"missing={missing!r} extra={extra!r}"
            )
        taken_parent: dict[LifetimeToken[Any], Lease[Any]] = {}
        taken_extra: dict[LifetimeToken[Any], Lease[Any]] = {}
        # Extra derived tokens that duplicate a parent (or the key) token are
        # tolerated: the redundant lease is discarded here and recorded so the
        # error-path cleanup below doesn't double-discard it.
        dropped_extra: set[LifetimeToken[Any]] = set()
        try:
            self._key_ull = UnconditionalLocalLifetime(f"{name or repr(self)}/key")
            key_root = self._key_ull.root_lease
            for parent in parents:
                # An entered parent has a token; clone-then-take semantics are
                # the caller's responsibility (we take ownership here).
                taken_parent[parent._entered_token] = parent_leases[parent].take()
            # A derived input is a *set* of tokens, so a token already supplied as
            # a parent — or the freshly-minted key — needs no separate extra
            # input. Tolerate (and discard) any such duplicate rather than
            # rejecting it; the token keeps its parent/key treatment and is not
            # recorded as an extra (so decompose/compose still round-trip).
            already_covered = set(taken_parent.keys()) | {self._key_ull.token}
            for tok, lease in extra_derived_leases.items():
                if tok in already_covered:
                    lease.discard()
                    dropped_extra.add(tok)
                    continue
                taken_extra[tok] = lease.take()
            self._extra_derived_tokens = tuple(taken_extra.keys())
            input_tokens: set[LifetimeToken[Any]] = (
                {self._key_ull.token} | set(taken_parent.keys()) | set(taken_extra.keys())
            )
            derived = DerivedLocalLifetime[Any](
                name=f"{name or repr(self)}/derived",
                inputs=input_tokens,
            )
            self._derived = derived
            # exchange_forward consumes every supplied lease; the key via is a
            # fresh clone of the key root, which we discard immediately after
            # (the key now lives only as a via inside the base auth lease).
            forward_inputs: dict[LifetimeToken[Any], Lease[Any]] = {
                self._key_ull.token: key_root.clone(),
                **taken_parent,
                **taken_extra,
            }
            self._scope_auth_lease = derived.exchange_forward(forward_inputs)
            self._token = derived.token
            key_root.discard()
            self._global_store.register_token(self._token, name=name or repr(self))
        except BaseException:
            for taken in (*taken_parent.values(), *taken_extra.values()):
                taken.discard()
            for parent, lease in parent_leases.items():
                if parent._token not in taken_parent:
                    lease.discard()
            for tok, lease in extra_derived_leases.items():
                if tok not in taken_extra and tok not in dropped_extra:
                    lease.discard()
            if self._scope_auth_lease is not None:
                self._scope_auth_lease.discard()
                self._scope_auth_lease = None
            if self._derived is not None:
                self._derived.revoke("scope init failure")
                self._derived = None
            if self._key_ull is not None:
                self._key_ull.revoke("scope init failure")
                self._key_ull = None
            self._token = None
            raise

        self._init_arena_and_cache()
        self._register_scope_token_end_callback()
        self._populate_ancestor_bridges()
        try:
            self._apply_context_marker_override(
                provenance_context=self._provenance_context,
                provenance_context_from_scope=self._provenance_context_from_scope,
            )
        except BaseException:
            # Token + arena are live at this point; revoke fires the token-end
            # callback (cap-bridge cleanup), then close the arena.
            if self._scope_auth_lease is not None:
                self._scope_auth_lease.discard()
                self._scope_auth_lease = None
            assert self._derived is not None
            self._derived.revoke("scope init failure (provenance context)")
            if self._key_ull is not None:
                self._key_ull.revoke("scope init failure (provenance context)")
            if self._main_arena_obj is not None:
                self._main_arena_obj.close()
            raise

        self._started = True
        _scope_debug.LIVE_SCOPES.add(self)
        self._is_deeply_fetchable()

    @property
    def _main_arena(self) -> memory_arena.MemoryArena[Any]:
        """The scope's main arena (non-None), created at entry.

        Read-only narrowing accessor over ``_main_arena_obj`` (which is ``None``
        before entry / for an inert scope). Raises if the scope was never
        started; the many post-entry op sites read this without a None check.
        """
        if self._main_arena_obj is None:
            raise RuntimeError(f"Scope {self!r} has not been started")
        return self._main_arena_obj

    @property
    def _entered_token(self) -> LifetimeToken[Any]:
        """``_token`` narrowed to non-None (raises if the scope was never entered).

        Used at sites that pass the token to APIs expecting a non-optional
        ``LifetimeToken`` and that only run after entry; ``_token`` itself stays
        ``| None`` for the legitimate not-started checks.
        """
        if self._token is None:
            raise RuntimeError(f"Scope {self!r} has not been started")
        return self._token

    def _current_auth_lease(self) -> Lease[Any]:
        """Return the live authorization lease this context resolves to.

        Resolution is **base-first** (design §2): a live scope-level base
        authorizes any context, and per-context overrides *union* with it (they
        are consulted only when the base is gone). Order:

        - token not yet created → raise not-started;
        - **base** ``_scope_auth_lease`` set and active → return it (the floor:
          while the scope is open it is usable from any context, and a stale
          override inherited via context-copy cannot revoke that);
        - else (base gone — draining) an **override** for this scope present in
          ``_AUTH_OVERRIDES`` and active → return it;
        - else an override present **but discarded/dead** → raise a *discarded
          authorization override* error (you let your lease die on a draining
          scope);
        - otherwise raise not-authorized.

        The returned lease is owned by the scope (base) or the enclosing
        ``authorize`` block (override); callers must ``.clone()`` to use it.
        """
        if self._token is None:
            raise RuntimeError(f"Scope {self!r} has not been started")
        base = self._scope_auth_lease
        if base is not None and base.is_active():
            return base
        # Base gone (draining): fall back to a per-context override if one is held.
        override = _AUTH_OVERRIDES.get().get(id(self))
        if override is not None:
            if override.is_active():
                return override
            raise RuntimeError(
                f"Scope {self!r} authorization override in this context was "
                "discarded while the scope is draining (its base authorization "
                "is gone); the lifetime was yours to manage and the lease is "
                "now dead (re-authorize with `with scope.authorize(lease):` if "
                "you still hold one)."
            )
        raise RuntimeError(
            f"Scope {self!r} is not authorized in this context "
            "(base authorization is gone — draining/closed — and no override "
            "is set; use `with scope.authorize(lease):`)."
        )

    def _current_key_lease(self) -> Lease[Any]:
        """Split a fresh ``Lease[key_token]`` out of this context's authorization.

        Non-consuming (``derived.subclone``): peeks the key via inside the
        current authorization lease (override-or-base) and returns a fresh clone
        of it, leaving the authorization usable. Used server-side to ship the
        call scope's key via in dependent-return entries.

        Raises:
            RuntimeError: this context is not authorized, or the scope has not
                been started.
        """
        if self._derived is None or self._key_ull is None:
            raise RuntimeError(f"Scope {self!r} has not been started")
        return self._derived.subclone(self._current_auth_lease(), self._key_ull.token)

    def is_open(self) -> bool:
        """True iff the token has been created and not ended (per-scope)."""
        return self._token is not None and not self._token.ended()

    def is_authorized(self) -> bool:
        """True iff this context resolves a live authorization for the scope.

        Non-raising counterpart of :meth:`check_authorized`. False on a
        present-but-discarded override, an unentered scope, or a drained base
        with no override.
        """
        try:
            self._current_auth_lease()
        except RuntimeError:
            return False
        return self.is_open()

    def check_authorized(self) -> None:
        """Raise unless the scope is open AND this context resolves a live
        authorization (override-or-base; see :meth:`_current_auth_lease`).

        A *draining* scope (token alive but the base lease dropped) raises here
        for any context without an override, while in-flight contexts holding
        overrides keep working. Token-state errors (not-started / closed) are
        reported before the authorization error so the message is the most
        specific one available.

        Raises:
            RuntimeError: the scope has not been started, has been closed, this
                context's override was discarded, or no authorization resolves.
        """
        if self._token is None:
            raise RuntimeError(f"Scope {self!r} has not been started")
        if self._token.ended():
            raise RuntimeError(f"Scope {self!r} has already been closed")
        # Raises the precise discarded-override / not-authorized message.
        self._current_auth_lease()

    @contextlib.contextmanager
    def authorize(self, lease: Lease[Any]) -> Generator["AuthorizationHandle", None, None]:
        """Bind ``lease`` as this context's authorization *override* for ``self``.

        COW-inserts ``id(self) -> lease`` into the shared ``_AUTH_OVERRIDES`` map
        for the duration of the block and restores the prior map on exit. The
        override *unions* with the scope-level base: while the base is live this
        is a no-op (base-first resolution, §2); it is what re-authorizes the
        scope when the base has been dropped (draining) or you hold only a lease
        in a context with no live base.

        Args:
            lease: A ``Lease[self.main]`` to authorize with. **Ownership is
                taken** (discarded on block exit); pass ``lease.clone()`` to
                retain your own copy.

        Yields:
            An :class:`AuthorizationHandle` whose :meth:`~AuthorizationHandle.discard`
            drops *this* override's lease early (idempotent), leaving the
            contextvar bookkeeping to the block. Used by the lifespan runner to
            drop its lease right after ``started()``.
        """
        held = lease.take()
        handle = AuthorizationHandle(held)
        prev = _AUTH_OVERRIDES.get()
        new_map = dict(prev)
        new_map[id(self)] = held
        token = _AUTH_OVERRIDES.set(types.MappingProxyType(new_map))
        try:
            yield handle
        finally:
            _AUTH_OVERRIDES.reset(token)
            handle.discard()

    def discard_base_authorization(self) -> None:
        """Discard *only* the scope-level base ``_scope_auth_lease`` (→ ``None``).

        Idempotent. This is the scope owner relinquishing the scope's
        authorization, beginning drain — the lowlevel cleanup partner of
        :meth:`lowlevel_start_lifespanless`. To drop a per-context *override*
        early (e.g. in a lifespan task after ``started()``), use the
        :class:`AuthorizationHandle` returned by :meth:`authorize` —
        ``auth.discard()`` — not this method.
        """
        if self._scope_auth_lease is not None:
            self._scope_auth_lease.discard()
            self._scope_auth_lease = None

    def clone_authorization(self) -> Lease[Any]:
        """Return a fresh clone of this context's current authorization lease.

        Pair with :meth:`authorize` in another context, or stash / pass across a
        task boundary.

        Lifetime consequence worth knowing when retaining the clone: a derived
        scope's lease carries each derived input as a via, so the retained
        clone keeps every input pinned until it is discarded. In particular, a
        service dispatch's per-call *call scope* is derived from the impl's
        mount scope — retaining its authorization past the dispatch keeps the
        mount from ever finishing draining (deadlocking graceful teardown)
        unless you own a matching release path. When what you need to keep is
        the call's *data*, retain the call's input scope instead
        (``CommonServiceImpl._fxtr_get_input_scope``), which has no mount
        parentage.

        Returns:
            A fresh ``Lease[self.main]`` cloned from the lease this context
            resolves to (override-or-base). Owned by the caller.

        Raises:
            RuntimeError: this context is not authorized for ``self``.
        """
        return self._current_auth_lease().clone()

    def decompose_authorization(self, lease: Lease[Any]) -> dict[LifetimeToken[Any], Lease[Any]]:
        """Split a ``Lease[self.main]`` into all its vias, keyed by token.

        Args:
            lease: A ``Lease[self.main]``. **Ownership is taken** — it is
                consumed (via ``derived.exchange_reverse``).

        Returns:
            A ``{token: Lease}`` map covering every derived input: the key via
            (at :attr:`key_token`), one per-parent via (at ``parent.scope_token``),
            and one per non-parent derived input. Inverse of
            :meth:`compose_authorization`. Callers index it by token using the
            public ``scope_token`` / ``key_token`` accessors.

        Raises:
            RuntimeError: the scope has not been started.
        """
        if self._derived is None:
            lease.discard()
            raise RuntimeError(f"Scope {self!r} has not been started")
        # exchange_reverse already returns the full {token: lease} via map; a live
        # Lease[main] implies every via is live, so the reverse is safe.
        return self._derived.exchange_reverse(lease)

    def compose_authorization(
        self,
        leases: Mapping[LifetimeToken[Any], Lease[Any]],
    ) -> Lease[Any]:
        """Recombine a ``{token: Lease}`` map into ``Lease[self.main]``.

        Inverse of :meth:`decompose_authorization`.

        Args:
            leases: One lease per derived-input token. The key set must match the
                scope's full derived-input set exactly — :attr:`key_token` + every
                parent's ``scope_token`` + every extra derived-input token.
                **Ownership is taken.**

        Returns:
            A fresh ``Lease[self.main]``. Owned by the caller.

        Raises:
            RuntimeError: the scope has not been started, or ``leases`` does not
                cover exactly the scope's derived-input set.
        """
        if self._derived is None or self._key_ull is None or self._token is None:
            for lease in leases.values():
                lease.discard()
            raise RuntimeError(f"Scope {self!r} has not been started")
        expected: set[LifetimeToken[Any]] = (
            {self._key_ull.token}
            | {p._entered_token for p in self._parents}
            | set(self._extra_derived_tokens)
        )
        if set(leases.keys()) != expected:
            for lease in leases.values():
                lease.discard()
            raise RuntimeError(
                "compose_authorization: leases must cover exactly the scope's "
                f"derived inputs {expected!r}; got {set(leases.keys())!r}"
            )
        return self._derived.exchange_forward(dict(leases))

    def _apply_context_marker_override(
        self,
        *,
        provenance_context: FxtrID | None,
        provenance_context_from_scope: FxtrScope | None,
    ) -> None:
        """Override the bare ``"."`` marker with a provenance-bearing one.

        Called once per scope, after ``_populate_ancestor_bridges`` (so that
        ``track_from_memory_arena`` can dest-adopt inheritance entries from
        the source scope when needed) and before the auto-start tail.

        Four cases:

        - ``store_provenance`` is ``"strip"``: the scope is a provenance-free
          island; skip both the explicit-c-node and parent-inherit branches
          and leave the bare ``"."`` marker untouched. The constructor's
          validation should already have rejected an explicit
          ``provenance_context``; this is asserted defensively.
        - ``provenance_context`` is given: validate it's a c-node, copy it
          into our main arena from ``provenance_context_from_scope`` (or
          a parent scope that tracks it), validate it unwraps to ``"."``,
          and set the marker to
          ``FxtrID(".", _provenance_parent=provenance_context)``.
        - ``provenance_context`` is ``None`` and a parent has a
          provenance-bearing marker: copy that parent's c-node into our
          arena and reuse it as our marker's provenance parent.
        - Otherwise: the bare ``"."`` set by ``_init_arena_and_cache``
          remains.

        Raises:
            ValueError: ``provenance_context`` is not a c-node, doesn't
                unwrap to ``"."``, or isn't tracked in any candidate
                source scope.
        """
        if self._store_provenance == "strip":
            assert provenance_context is None, (
                "store_provenance='strip' + provenance_context should have"
                " been rejected at constructor validation; reaching this"
                " branch is a bug."
            )
            # Don't inherit a parent's provenance marker either. The bare
            # "." marker set by _init_arena_and_cache stands.
            return

        if provenance_context is None:
            if provenance_context_from_scope is not None:
                raise ValueError(
                    "provenance_context_from_scope was supplied without a"
                    " provenance_context; nothing to track."
                )
            # Inherit from the closest parent's marker. The marker is set in
            # the parent's _init_arena_and_cache + _apply_context_marker_override
            # cycle, so it's a fully-formed FxtrID by the time we read it.
            #
            # The parent's own ``_scope_auth_lease`` may already have been
            # discarded by the time this fires — e.g. when ``self`` is
            # a per-call dispatch sub-scope for a method on a service
            # mounted in the parent, and the parent is being kept
            # alive only by chain-bridges from the returned ref. So we
            # mint a fresh ``Lease[parent._token]`` from ``self``'s
            # own ancestor cap-bridge graph, the same way
            # :meth:`_lift_unchecked` does.
            for pscope in self._parents:
                p_marker = pscope._context_marker
                assert p_marker is not None  # entered parent always has a marker
                pp = p_marker._provenance_parent
                if pp is None:
                    continue
                # Bring the c-node into our arena via a sync transfer so our
                # store/load hot path doesn't have to walk parents for it.
                source_lease = self._mint_ancestor_lease(pscope._entered_token)
                self._track_provenance_context_from(pp, pscope, source_lease=source_lease)
                self._context_marker = FxtrID.from_tag_and_payload(".", b"", provenance_parent=pp)
                return
            # No parent has a provenance-bearing marker — bare "." stands.
            return

        if provenance_context.get_tag() != "c":
            raise ValueError(
                "provenance_context must be a c-node FxtrID; got tag"
                f" {provenance_context.get_tag()!r} ({provenance_context})"
            )
        if provenance_context._provenance_parent is not None:
            raise ValueError(
                "provenance_context must be a bare c-node FxtrID; got one"
                f" with a provenance_parent attached ({provenance_context})"
            )

        if provenance_context_from_scope is not None:
            # Explicit source: the caller has chosen this scope as the
            # source and is responsible for it being alive. A fresh
            # main-lease clone is the right credential here.
            source_scope = provenance_context_from_scope
            source_lease = source_scope._current_auth_lease().clone()
            if not source_scope._is_tracked_internal(source_lease.clone(), provenance_context):
                source_lease.discard()
                raise ValueError(
                    f"provenance_context {provenance_context} is not tracked"
                    " in supplied provenance_context_from_scope"
                    f" {source_scope!r}"
                )
        else:
            # Implicit source: walk parents. Each parent might be in
            # late-life state (alive via chain-bridges, at-rest
            # ``_scope_auth_lease`` discarded), so we mint the read-side
            # credential via our own ancestor cap-bridge graph.
            source_scope = None
            source_lease = None
            for pscope in self._parents:
                candidate_lease = self._mint_ancestor_lease(pscope._entered_token)
                if pscope._is_tracked_internal(candidate_lease.clone(), provenance_context):
                    source_scope = pscope
                    source_lease = candidate_lease
                    break
                candidate_lease.discard()
            if source_scope is None or source_lease is None:
                raise ValueError(
                    f"provenance_context {provenance_context} is not tracked"
                    " in any parent scope; supply"
                    " provenance_context_from_scope= explicitly."
                )

        self._track_provenance_context_from(
            provenance_context, source_scope, source_lease=source_lease
        )

        # Validate the c-node unwraps to bare ".".
        unwrapped = self._main_arena.unwrap_provenance(
            self._current_auth_lease().clone(), provenance_context
        )
        expected_dot = FxtrID.from_tag_and_payload(".", b"")
        if unwrapped != expected_dot:
            raise ValueError(
                f"provenance_context {provenance_context} must unwrap to"
                f" '.', but unwraps to {unwrapped}"
            )
        self._context_marker = FxtrID.from_tag_and_payload(
            ".", b"", provenance_parent=provenance_context
        )

    def _track_provenance_context_from(
        self, c_node: FxtrID, source_scope: FxtrScope, *, source_lease: Lease[Any]
    ) -> None:
        """Sync-copy ``c_node`` from ``source_scope``'s arena into ours.

        Uses :meth:`MemoryArena.track_from_memory_arena` with the
        ancestor cap-bridges as ``dest_inheritance_bridges`` so the walk
        can lazily adopt closure nodes claim-held by any ancestor —
        including entities the source scope only inherits (e.g. from a
        foreign arena whose data was never loaded locally).

        ``source_lease`` is the lease on ``source_scope``'s main token
        to use as the read-side credential. Callers must supply the
        right kind:

        - For an *explicit* ``provenance_context_from_scope`` the
          caller has chosen the source and is responsible for it being
          alive — a fresh ``source_scope._current_auth_lease().clone()`` is
          appropriate.
        - For *parent-inferred* sourcing (during construction of a
          derived child scope), the parent's ``_scope_auth_lease`` may
          already be discarded (the parent's ``async with`` body has
          exited but the scope is kept alive by external chain-bridges).
          The caller must instead mint a fresh
          ``Lease[source_scope._token]`` from ``self``'s own ancestor
          cap-bridge for that token — see
          :meth:`_mint_ancestor_lease`. This is the same pattern
          :meth:`_lift_unchecked` follows; it works even when the
          parent is mid-drain because the bridge holds an at-rest
          lease that's alive for ``self``'s full lifetime.
        """
        dest_bridges: dict[LifetimeToken[Any], LocalBridge] = {}
        for token, entry in self._cap_bridges.items():
            if not entry.is_ancestor_or_arena_owner:
                continue
            if isinstance(entry.bridge, (IndependentLocalBridge, DependentLocalBridge)):
                dest_bridges[token] = entry.bridge
        self._main_arena.track_from_memory_arena(
            self._current_auth_lease().clone(),
            source_scope._main_arena,
            source_lease,
            c_node,
            dest_inheritance_bridges=dest_bridges,
        )

    def _get_or_mint_fallback_or_own_marker(self) -> FxtrID:
        """Return a layered ``FxtrID(".", _provenance_parent=<c_node>)``.

        Used by ``store_cbor`` under ``store_provenance="auto"`` to
        substitute the cbor's bare ``$fxtr.ctx`` marker with a
        provenance-bearing one when children carry provenance parents.

        Two cases:

        - The scope already has a layered marker (because the
          constructor was given a ``provenance_context``): return it
          as-is — the existing layered ``.``-marker already encodes
          this scope's c-node.
        - The scope has a bare ``.`` marker: lazily mint a fallback
          c-node from a :class:`FallbackContextMarker` system entity
          tied to ``self._name``, cache the resulting layered marker
          on ``self._fallback_marker``, and return it. Subsequent
          calls reuse the cached value. Two scopes with the same
          ``_name`` (and hence the same ``FallbackContextMarker``
          payload) produce the same fallback c-node ID — intentional
          dedup so fallback provenance from a single code path stays
          on one trace.
        """
        marker = self._context_marker
        assert marker is not None, "scope marker is set at entry"
        if marker._provenance_parent is not None:
            return marker

        if self._fallback_marker is None:
            # Local import to avoid circular import at module load
            # (system_entities imports entity_ref_scope).
            from fxtr2.data.system_entities import FallbackContextMarker

            # Bypass _fxtr_convert_to_cbor's MISSING_FXTR_CTX_SENTINEL
            # replacement by supplying the marker explicitly; avoids
            # any dependency on the active scope. The c-node body's
            # marker is a bare "." (it's a d-node payload, not a
            # provenanced entity in its own right).
            ground_marker = RawFxtrEntityMarkerID(inner_id=FxtrID.from_tag_and_payload(".", b""))
            fallback_cbor = FallbackContextMarker(
                scope_name=self._name,
                fxtr_ctx=ground_marker,
            )._fxtr_convert_to_cbor()
            # Create in a temporary lifespanless child scope that always sees
            # globals. (Sync context — lowlevel start, not ``async with``.)
            tmp_scope = FxtrScope(
                isolated=True, include=[GLOBAL_SCOPE]
            ).lowlevel_start_lifespanless(
                parent_leases={GLOBAL_SCOPE: GLOBAL_SCOPE.clone_authorization()}
            )
            c_node = tmp_scope.create_c_node(fallback_cbor)
            self.track_from_scope(c_node, tmp_scope)
            tmp_scope.force_close()
            self._fallback_marker = FxtrID.from_tag_and_payload(".", b"", provenance_parent=c_node)

        return self._fallback_marker

    def _register_scope_token_end_callback(self) -> None:
        """Register a callback on our token so scope-owned foreign leases are
        cleaned up when the token ends.

        Fires on both natural drain (``aclose``) and revoke (``force_close``,
        or derived input-end cascade). Mirrors the pattern
        ``MemoryStore._on_token_end`` uses for its own foreign-lease map.
        Future substages (6+) will add cap-bridge cleanup to this same
        callback.
        """

        def _on_token_end() -> None:
            for entry in self._deep_fetch_sources:
                entry.bridge.discard_at_rest()
            self._deep_fetch_sources.clear()
            for bridge_entry in self._cap_bridges.values():
                bridge_entry.bridge.discard_at_rest()
            self._cap_bridges.clear()
            self._known_handles.clear()

        self._entered_token.register_end_callback(_on_token_end)

    @property
    def isolated(self) -> bool:
        return self._isolated

    @property
    def frozen(self) -> bool:
        return self._frozen

    @classmethod
    def get_active(cls) -> FxtrScope:
        """Return the currently-active scope.

        Raises:
            NoActiveScopeError: The current context deliberately runs
                without an ambient scope (see ``no_active_scope``).
        """
        return get_active_scope()

    @property
    def outer(self) -> FxtrScope:
        if self._isolated:
            raise RuntimeError("Cannot access outer scope of an isolated scope")
        assert self._outer_scope is not None
        return self._outer_scope

    @property
    def scope_token(self) -> LifetimeToken[Any]:
        """The scope's main derived token (the subject of ``Lease[main]``).

        This is the public name for the key used to index a decomposed
        ``{token: Lease}`` map (see :meth:`decompose_authorization`). Raises if
        the scope has not been started.
        """
        if self._token is None:
            raise RuntimeError(f"Scope {self!r} has not been started")
        return self._token

    @property
    def key_token(self) -> LifetimeToken[Any]:
        """The scope's private key token (``_key_ull.token``).

        The key via in a decomposed ``{token: Lease}`` map lives at this token.
        Raises if the scope has not been started.
        """
        if self._key_ull is None:
            raise RuntimeError(f"Scope {self!r} has not been started")
        return self._key_ull.token

    def get_context_marker_id(self) -> FxtrID:
        marker = self._context_marker
        if marker is None:
            raise RuntimeError(f"Scope {self!r} has not been started")
        return marker

    def extract_provenance_parent(
        self, data_with_prov: FxtrID, *, extract_as_u_if_missing: bool = False
    ) -> FxtrID:
        """Drop the provenance parent of an ID, returning just the parent.

        Given a FxtrID with ``_provenance_parent`` set, returns the bare
        provenance-level FxtrID with no ``_provenance_parent`` of its own —
        the caller is now operating on that node *as data* (e.g. for
        explicit pinning into a namespace, or for inspection at the
        provenance level).

        Inverse of :meth:`embed_as_provenance_parent`.

        Args:
            data_with_prov: A FxtrID. With its ``_provenance_parent`` set, the
                layered form; otherwise a bare id (only valid when
                ``extract_as_u_if_missing=True``).
            extract_as_u_if_missing: When ``True`` and ``data_with_prov`` has
                no ``_provenance_parent``, return ``create_u_node(data_with_prov)``
                instead of raising.

        Returns:
            ``data_with_prov._provenance_parent`` with no ``_provenance_parent``
            of its own (layered input), or ``u(data_with_prov)`` (bare input
            with ``extract_as_u_if_missing=True``). Both the original data ID
            and its provenance parent must be tracked in this scope.

        Raises:
            ValueError: ``data_with_prov`` has no ``_provenance_parent`` and
                ``extract_as_u_if_missing`` is ``False``, or either side of the
                (data, prov) pair is not tracked here.
        """
        self.check_authorized()
        if data_with_prov._provenance_parent is None:
            if extract_as_u_if_missing:
                return self.create_u_node(data_with_prov)
            raise ValueError(
                f"extract_provenance_parent: {data_with_prov} has no provenance parent attached"
            )
        prov = data_with_prov._provenance_parent
        if not self._is_tracked_internal(self._current_auth_lease().clone(), data_with_prov):
            raise ValueError(
                f"extract_provenance_parent: data ID {data_with_prov} is not tracked in {self!r}"
            )
        if not self._is_tracked_internal(self._current_auth_lease().clone(), prov):
            raise ValueError(
                f"extract_provenance_parent: provenance parent {prov} is not tracked in {self!r}"
            )
        # Strip any nested provenance parent on the parent itself; the bare
        # returned FxtrID is at the data level (from the user's now-elevated
        # perspective).
        return FxtrID(raw_data_id=prov._raw_data_id)

    def embed_as_provenance_parent(self, prov_node: FxtrID) -> FxtrID:
        """Pair a provenance node with its data node, producing a layered FxtrID.

        Given a bare provenance-node FxtrID tracked in this scope, looks up
        what it unwraps to (sync arena read) and returns
        ``FxtrID(unwrapped_data, _provenance_parent=prov_node)``. The result
        is a "layered" ID: subsequent storage / load operations treat it as
        data while internally maintaining the provenance trace.

        Inverse of :meth:`extract_provenance_parent`.

        Args:
            prov_node: A provenance-node FxtrID (tag ``n`` / ``c`` / ``p`` /
                ``u``) tracked in this scope. Must NOT itself carry a
                ``_provenance_parent`` — the caller hands us the bare
                provenance node.

        Returns:
            ``FxtrID(unwrapped_data, _provenance_parent=prov_node)``.

        Raises:
            ValueError: ``prov_node`` already carries a ``_provenance_parent``;
                ``prov_node`` is not a provenance-node tag; ``prov_node`` is
                not tracked in this scope; or the unwrapped data ID is not
                tracked (which would indicate a memory-arena bug, since
                ``unwraps_to`` propagation should keep the data alongside).
        """
        self.check_authorized()
        if prov_node._provenance_parent is not None:
            raise ValueError(
                f"embed_as_provenance_parent: {prov_node} already has a"
                " provenance parent attached; expected a bare provenance node"
            )
        tag = prov_node.get_tag()
        if tag not in ("n", "c", "p", "u"):
            raise ValueError(
                f"embed_as_provenance_parent: {prov_node} is not a"
                f" provenance-node tag (got {tag!r}; expected one of n/c/p/u)"
            )
        if not self._is_tracked_internal(self._current_auth_lease().clone(), prov_node):
            raise ValueError(f"embed_as_provenance_parent: {prov_node} is not tracked in {self!r}")
        unwrapped = self._main_arena.unwrap_provenance(
            self._current_auth_lease().clone(), prov_node
        )
        if unwrapped is None:
            raise RuntimeError(
                f"embed_as_provenance_parent: internal error, arena cannot unwrap {prov_node}"
            )
        if not self._is_tracked_internal(self._current_auth_lease().clone(), unwrapped):
            raise ValueError(
                f"embed_as_provenance_parent: unwrapped data ID {unwrapped} is"
                f" not tracked in {self!r} (this should not happen since the"
                " arena's unwraps_to propagation tracks both ends together)"
            )
        return FxtrID(raw_data_id=unwrapped._raw_data_id, provenance_parent=prov_node)

    def c_node_to_context_data(self, c_node: FxtrID) -> FxtrID:
        """Get the d-node a c-node was built from.

        Returns the data-level FxtrID of the d-node whose CBOR
        contents are the source the c-node was minted from (the
        c-node's payload is the SHA-256 hash of that d-node's
        contents). Sync; relies on the arena's pre-populated
        ``ctx_node`` link for the c-node.

        Args:
            c_node: A bare c-tagged FxtrID tracked in this scope.

        Returns:
            The bare d-tagged FxtrID for the c-node's context data.

        Raises:
            ValueError: ``c_node`` is not a c-tag, carries a
                ``_provenance_parent``, or is not tracked here.
        """
        self.check_authorized()
        if c_node._provenance_parent is not None:
            raise ValueError(
                f"c_node_to_context_data: {c_node} already has a"
                " provenance parent attached; expected a bare c-node"
            )
        if c_node.get_tag() != "c":
            raise ValueError(
                f"c_node_to_context_data: {c_node} is not a c-node (got tag {c_node.get_tag()!r})"
            )
        if not self._is_tracked_internal(self._current_auth_lease().clone(), c_node):
            raise ValueError(f"c_node_to_context_data: {c_node} is not tracked in {self!r}")
        return self._main_arena.c_node_to_context_data(self._current_auth_lease().clone(), c_node)

    def create_c_node(self, context_data: FxtrCbor) -> FxtrID:
        """Mint a c-node wrapping the given d-node payload.

        Thin scope-level wrapper over
        :meth:`MemoryArena.create_c_node`. The payload must be a
        d-node cbor with a ``value`` field; the resulting c-node
        unwraps to that ``value`` (typically the ``.`` ground node).
        The d-node materialized from ``context_data`` and the c-node
        itself are both registered under this scope's lifetime token.

        Pair the returned id with
        :meth:`FxtrScope.embed_as_provenance_parent` to obtain a
        layered ``(., c)`` form, or pass it as another scope's
        ``provenance_context``.

        Auto-lifts every FxtrID referenced from ``context_data``
        (matching the store_cbor convenience): callers can hand in
        cbor whose references live on parent scopes without having
        to manually ``lift`` each one first. The arena-level
        ``create_c_node`` would reject untracked references, so the
        lift happens first.

        Args:
            context_data: d-node cbor for the context. References
                (``$fxtr.ctx`` / weak / strong slots) must be
                reachable from this scope or one of its ancestors.
                Must contain a ``value`` field; the arena enforces
                this.

        Returns:
            A bare c-tagged FxtrID with no ``_provenance_parent``.

        Raises:
            ValueError / arena errors: ``context_data`` is malformed
                or references untracked children unreachable from
                this scope's ancestor chain.
        """
        self.check_authorized()
        arena_util.visit_ids(context_data, lambda fid: self.lift(fid.inner_id))
        return self._main_arena.create_c_node(self._current_auth_lease().clone(), context_data)

    def create_p_node(self, pathroot_data: FxtrCbor) -> FxtrID:
        """Mint a path-provenance (p-) node from its pathroot data.

        Thin scope-level wrapper over
        :meth:`MemoryArena.create_p_node`. ``pathroot_data`` must be a
        d-node cbor with a ``value`` field pointing at a node id; the
        d-node materialized from it and the resulting p-node are both
        registered under this scope's lifetime token.

        Auto-lifts every FxtrID referenced from ``pathroot_data``
        (matching :meth:`create_c_node` / ``store_cbor``): callers can
        hand in cbor whose references live on parent scopes without
        manually ``lift``-ing each one first.

        Args:
            pathroot_data: d-node cbor for the pathroot. Must contain a
                ``value`` field that is a node id; references must be
                reachable from this scope or one of its ancestors.

        Returns:
            A ``p``-tagged FxtrID.

        Raises:
            ValueError / arena errors: ``pathroot_data`` is malformed or
                references untracked children unreachable from this
                scope's ancestor chain.
        """
        self.check_authorized()
        arena_util.visit_ids(pathroot_data, lambda fid: self.lift(fid.inner_id))
        return self._main_arena.create_p_node(self._current_auth_lease().clone(), pathroot_data)

    def create_u_node(self, wrapped_id: FxtrID) -> FxtrID:
        """Create a u-type provenance node wrapping ``wrapped_id``.

        Returns a bare ``u``-tagged FxtrID whose ``unwraps_to`` chain
        resolves to ``wrapped_id``. Pair with
        :meth:`embed_as_provenance_parent` to produce a layered
        ``(wrapped_id, u(wrapped_id))`` form — the canonical
        "data with trivial provenance" shape used by tools that
        need every node to carry a provenance parent (e.g. the
        stage-7 preserve-provenance persistence cap).

        Args:
            wrapped_id: A FxtrID tracked in this scope. Must NOT
                itself carry a ``_provenance_parent`` — the caller
                hands us the bare node to wrap.

        Returns:
            A bare ``u``-tagged FxtrID with no
            ``_provenance_parent``. The new node is registered
            under this scope's lifetime token.

        Raises:
            ValueError: ``wrapped_id`` already carries a
                ``_provenance_parent``, or is not tracked in this
                scope.
        """
        self.check_authorized()
        if wrapped_id._provenance_parent is not None:
            raise ValueError(
                f"create_u_node: {wrapped_id} already has a"
                " provenance parent attached; expected a bare node"
            )
        if not self._is_tracked_internal(self._current_auth_lease().clone(), wrapped_id):
            raise ValueError(f"create_u_node: {wrapped_id} is not tracked in {self!r}")
        return self._main_arena.create_u_node(self._current_auth_lease().clone(), wrapped_id)

    def step_back_p_ctx(self, p_node: FxtrID) -> FxtrID | None:
        """Step back along a p-node's path. Returns the p-node parent
        with the last path component dropped, or ``None`` if the
        p-node is a pathroot (no recorded backstep).

        Sync; reads the arena's pre-populated ``backstep_node`` link.

        Args:
            p_node: A bare p-tagged FxtrID tracked in this scope.

        Returns:
            The bare p-tagged FxtrID for the parent step in the path,
            or ``None`` if ``p_node`` is a pathroot.

        Raises:
            ValueError: ``p_node`` is not a p-tag, carries a
                ``_provenance_parent``, or is not tracked here.
        """
        self.check_authorized()
        if p_node._provenance_parent is not None:
            raise ValueError(
                f"step_back_p_ctx: {p_node} already has a provenance"
                " parent attached; expected a bare p-node"
            )
        if p_node.get_tag() != "p":
            raise ValueError(
                f"step_back_p_ctx: {p_node} is not a p-node (got tag {p_node.get_tag()!r})"
            )
        if not self._is_tracked_internal(self._current_auth_lease().clone(), p_node):
            raise ValueError(f"step_back_p_ctx: {p_node} is not tracked in {self!r}")
        return self._main_arena.step_back_p_ctx(self._current_auth_lease().clone(), p_node)

    def __repr__(self) -> str:
        return f"<FxtrScope {self._name!r} at {id(self):x}>"

    def _start_as_root_scope(self) -> Self:
        """Start this scope as a root scope.

        Root scopes are process-lifetime singletons (e.g. ``GLOBAL_SCOPE``) that
        never go through an ``async with`` cycle. We start them lifespanless (no
        task group), which stores the base authorization lease as a plain
        attribute — visible in every task/context, so the root reads authorized
        everywhere with no per-context setup (preserving bare
        ``entity.store()``). An ``atexit`` hook synchronously revokes the
        scope's lifetime on interpreter shutdown so the lease drains cleanly
        without a leak warning.
        """
        if self._closed:
            raise RuntimeError("Scope is already closed")
        if self._is_root_scope:
            raise RuntimeError("Scope has already been started as root")
        if not self._isolated:
            raise RuntimeError("Root scope must be isolated")
        if self._parents:
            raise RuntimeError("Root scope cannot have parents")

        self._is_root_scope = True
        self.lowlevel_start_lifespanless()

        import atexit

        atexit.register(self.force_close)
        return self

    async def aclose(self) -> None:
        """Close the scope: drop the base authorization and wait for drain.

        Drops the scope-level base lease (``_scope_auth_lease`` → ``None``,
        which rejects new ambient use everywhere) and awaits the derived
        token's drain — i.e. waits for every outstanding ``Lease[main]`` clone
        (in-flight calls re-authorized via overrides, etc.) to release. The key
        token ends naturally once its last via (inside the last ``Lease[main]``)
        drops; we do not hold a standing key lease.

        On cancellation during drain the underlying derived lifetime's
        ``aclose`` revokes itself for a fast close; the cancellation propagates.

        Idempotent on an already-closed scope. Raises on a scope that was never
        started (constructed but never entered) — there is nothing to close.

        **Sharp edge.** A bare ``try: ... finally: await scope.aclose()`` will
        block indefinitely if the body raised but there are still outstanding
        leases to the scope. The drain wait may not be automatically cancelled
        by anyio in this case because the exception has been caught, and has not
        yet propagated to an outer anyio cancel scope — the
        in-flight exception just sits as ``__context__`` on the never-completing
        wait. Three robust patterns in order of preference:

        1. ``async with scope:`` — the CM routes to ``force_close`` on
           exception automatically.
        2. ``try/except BaseException: scope.force_close(); raise / else:
           await scope.aclose()`` — explicit fast-revoke on the exception path.
        3. ``finally: with anyio.move_on_after(N): await scope.aclose()`` —
           bound the drain so a hung close eventually cancels and revokes the
           outstanding leases, surfacing the original exception.
        """
        if self._closed:
            return
        if not self._started:
            raise RuntimeError(
                f"Scope {self!r} was never opened; nothing to close "
                "(construction is inert — enter via `async with` or "
                "`lowlevel_start_lifespanless()` first)."
            )

        try:
            # Drop the base first (global attribute write — new ambient use is
            # rejected everywhere), then await the derived token's drain.
            if self._scope_auth_lease is not None:
                self._scope_auth_lease.discard()
                self._scope_auth_lease = None
            assert self._derived is not None
            self._drain_started_at = _scope_debug.monotonic()
            await self._derived.aclose()
            # The key token outlives the scope harmlessly if external holders
            # kept split-out key leases; it gates nothing once the output ended.
        finally:
            # Signal attached lifespans to tear down — on the normal path (drain
            # complete) AND on cancellation (a bounded/forceful close where
            # ``derived.aclose`` revoked the token). Setting it in ``finally`` is
            # load-bearing: a cancelled ``aclose`` that skipped this would leave
            # parked lifespan tasks waiting forever, and the enclosing CM's
            # task-group exit would then hang awaiting them. (force_close sets it
            # too, for the same reason.)
            if self._closing_event is not None:
                self._closing_event.set()
            # Close the arena right after drain — every Lease[main] holder has
            # released (drain waited for them), so nothing can touch it now.
            if self._main_arena_obj is not None:
                self._main_arena_obj.close()
            self._closed = True
        # Structured teardown: don't return until attached lifespan tasks have
        # actually exited. We can't await the task group itself (we run inside
        # it), so we wait on the latch the last finishing lifespan sets. (Skip
        # when there are none — the latch would otherwise never fire. On
        # cancellation here, the CM stack's task-group exit cancels the parked /
        # tearing-down tasks on unwind.)
        if self._lifespan_count > 0:
            assert self._lifespans_closed_event is not None
            self._lifespan_wait_started_at = _scope_debug.monotonic()
            await self._lifespans_closed_event.wait()

    def force_close(self) -> None:
        """Synchronously revoke owned lifetimes and close the arena.

        Equivalent to aborting ``aclose`` via the cancellation path. Drops the
        base lease, revokes the derived token (breaks all outstanding
        ``Lease[main]`` clones) **and** the key ULL (breaks any externally-split
        key leases), then closes the arena. Used by ``GLOBAL_SCOPE``'s atexit
        hook, by the exception branch of the async CM, and by callers that need
        a fast sync teardown (tests, etc.). Idempotent: no-op on an
        already-closed scope. Raises on a constructed-but-unentered scope —
        construction is inert, so there is nothing to close.
        """
        if self._closed:
            return
        if not self._started:
            raise RuntimeError(
                f"Scope {self!r} was never opened; nothing to close "
                "(construction is inert — enter via `async with` or "
                "`lowlevel_start_lifespanless()` first)."
            )
        try:
            if self._scope_auth_lease is not None:
                self._scope_auth_lease.discard()
                self._scope_auth_lease = None
            if self._derived is not None:
                self._derived.revoke("force_close")
            if self._key_ull is not None:
                self._key_ull.revoke("force_close")
            # Wake any parked lifespans (harmless if also cancelled by the
            # task group unwind on the CM exception path).
            if self._closing_event is not None:
                self._closing_event.set()
        finally:
            if self._main_arena_obj is not None:
                self._main_arena_obj.close()
            self._closed = True

    def _check_not_frozen(self, method: str) -> None:
        if self._frozen:
            raise RuntimeError(
                f"Scope has been frozen! `{method}` operations are not allowed on a "
                "frozen scope, as they affect the set of tracked nodes. To perform "
                "operations in a frozen scope, you should create a new FxtrScope that "
                "inherits from this one and perform your operations there."
            )

    def _check_not_tracing_loads(self, method: str) -> None:
        if self._load_tracer.get() is not None:
            raise RuntimeError(
                f"Scope is actively loading an entity! `{method}` operations are not allowed on a "
                "scope that is in the middle of running a load handler; the only allowed operation "
                "is to load more entities."
            )

    def _check_not_frozen_and_not_tracing_loads(self, method: str) -> None:
        self._check_not_frozen(method)
        self._check_not_tracing_loads(method)

    def freeze_scope(self) -> None:
        """Freeze this scope, preventing any further mutations to its tracked node set.

        After freezing, operations that would mutate the scope (store, load, lift,
        copy_from_scope, abandon, etc.) will raise RuntimeError. Read-only operations
        like get_cached_value and is_tracked remain allowed.

        Freezing requires all parent scopes to already be frozen, ensuring that no
        mutations to parent scopes can be visible through this scope. Root scopes
        cannot be frozen. This means only isolated scopes (and their descendants) can
        be frozen.

        To populate a scope before freezing it, use a temporary helper child scope
        that also inherits from the global scope (needed for type builtins), store
        entities there, and copy them into the target scope::

            async with FxtrScope(isolated=True) as freezable:
                # Use a helper scope that bridges the global scope (for type builtins)
                # and the target scope (for copying entities into it).
                async with FxtrScope(
                    isolated=True, include=[freezable, GLOBAL_SCOPE]
                ) as helper:
                    ref = await helper.store(my_entity).copy_to(freezable)

                freezable.freeze_scope()
                # Now freezable is immutable; child scopes can safely depend on it.
                async with FxtrScope(isolated=True, include=[freezable]) as child:
                    ...  # child can load from freezable, store its own entities, etc.
        """
        self.check_authorized()
        self._check_not_tracing_loads("freeze_scope")
        if self._is_root_scope:
            raise RuntimeError("Root scopes cannot be frozen")
        if self._frozen:
            return  # already frozen, idempotent
        for parent in self._parents:
            if not parent.frozen:
                raise RuntimeError(
                    f"Cannot freeze scope: parent {parent} is not frozen. "
                    "All parent scopes must be frozen before freezing a child scope."
                )
        self._frozen = True

    def _is_deeply_fetchable(self) -> bool:
        """Return whether this scope is deeply fetchable, committing the flag if not yet set.

        Reads from a parent walk on first call and caches the result; subsequent
        reads return the cached value. The commit is a one-way door — once True,
        ``_mark_not_deeply_fetchable`` will refuse the downgrade.

        Construction validates parents are started, so the parents this walks
        are guaranteed to have committed flags by the time we reach them.
        """
        if self._deeply_fetchable_flag is None:
            inferred = True
            for parent in self._parents:
                if not parent._is_deeply_fetchable():
                    inferred = False
                    break
            self._deeply_fetchable_flag = inferred
        return self._deeply_fetchable_flag

    def _mark_not_deeply_fetchable(self) -> None:
        """Mark this scope as not deeply fetchable."""
        if self._deeply_fetchable_flag is True:
            raise RuntimeError("Scope has already been marked as deeply fetchable")
        self._deeply_fetchable_flag = False

    @contextlib.asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
        """Enter the scope via the normal ``async with`` route.

        Validates (non-isolated) that the active scope is still the outer scope
        captured at construction, clones each parent's in-context authorization
        lease as the derived-token inputs, and delegates to
        :meth:`open_with_explicit_leases`. Cross-task / opaque-lease callers use
        ``open_with_explicit_leases`` directly with leases they supply.
        """
        if not self._isolated:
            active = get_active_scope()
            if active != self._outer_scope:
                raise RuntimeError(
                    f"Scope {self._outer_scope} was active when this scope ({self})"
                    " was constructed, but is no longer active when this scope is entered;"
                    " this is not allowed unless isolated=True was set."
                )
        parent_leases: dict[FxtrScope, Lease[Any]] = {}
        try:
            for parent in self._parents:
                parent_leases[parent] = parent._current_auth_lease().clone()
        except BaseException:
            for lease in parent_leases.values():
                lease.discard()
            raise
        async with self.open_with_explicit_leases(parent_leases=parent_leases) as opened:
            yield opened

    @contextlib.asynccontextmanager
    async def open_with_explicit_leases(
        self,
        *,
        parent_leases: Mapping[FxtrScope, Lease[Any]],
        extra_derived_leases: Mapping[LifetimeToken[Any], Lease[Any]] = _EMPTY_LEASE_MAP,
    ) -> AsyncGenerator[Self]:
        """The entry primitive: open the scope from explicitly-supplied leases.

        ``__asynccontextmanager__`` (the normal ``async with scope:``) *is* this,
        called with ``parent_leases`` cloned from each parent's in-context
        authorization. The explicit form exists for callers that hold a lease
        but not in-context authorization (cross-task, opaquely-passed, or
        draining-but-open parents — e.g. service dispatch); it is the one path
        allowed to derive from a draining-but-open parent. This is **not** the
        same as nesting ``with parent.authorize(lease)`` around the body — the
        supplied leases are needed only to mint the child's token at entry, not
        to keep the parent authorized for the child's whole body.

        Body: open the scope lifetime (token, base authorization, arena,
        ancestor bridges, marker), open the task group (every CM-entered scope
        is within-lifespan and can host services), set ``ACTIVE_SCOPE``
        (non-isolated only), yield; on exception ``force_close``, on normal exit
        ``aclose``; the task group is closed as the stack unwinds.

        Args:
            parent_leases: One ``Lease[parent.main]`` per direct parent, used as
                the derived-token inputs; keys must cover ``self``'s direct
                parents exactly. **Ownership is taken** (consumed at entry).
            extra_derived_leases: Additional derived-token inputs keyed by
                **token** (not parent scope) — for tokens the scope must gate on
                that have no ``FxtrScope`` object (e.g. dispatch arg-ServiceRef
                tokens; see :meth:`_do_open`). **Ownership is taken.**

        Yields:
            ``self`` (started + within its task group), for the ``async with``
                body.

        Raises:
            RuntimeError: the scope is already closed, already inside an
                ``async with`` block, or ``parent_leases`` does not cover exactly
                the direct parents.
        """

        def _discard_all_supplied() -> None:
            for lease in parent_leases.values():
                lease.discard()
            for lease in extra_derived_leases.values():
                lease.discard()

        if self._closed:
            _discard_all_supplied()
            raise RuntimeError(f"Scope {self!r} has already been closed")
        if self._inside_cm:
            _discard_all_supplied()
            raise RuntimeError(f"Scope {self!r} is already inside an async-with block")
        self._inside_cm = True
        try:
            # Open the scope lifetime first (consumes supplied leases; self-cleans
            # on failure). Marks _started and commits deep-fetchability.
            self._do_open(parent_leases, extra_derived_leases)
            try:
                async with contextlib.AsyncExitStack() as stack:
                    # Every CM-entered scope is within-lifespan: open a task
                    # group co-managed with the CM (closed as the stack unwinds,
                    # after drain on the happy path / after revoke on error).
                    self._task_group = await stack.enter_async_context(
                        create_task_group(collapse_exc_group=True)
                    )
                    # Attached lifespans park on this until the token drains
                    # (set by ``aclose``/``force_close``); the latch event lets
                    # ``aclose`` wait for them to actually finish tearing down.
                    self._closing_event = anyio.Event()
                    self._lifespans_closed_event = anyio.Event()
                    # Non-isolated scopes take over ACTIVE_SCOPE for their body.
                    if not self._isolated:
                        scope_override_token = ACTIVE_SCOPE.set(self)
                        stack.callback(ACTIVE_SCOPE.reset, scope_override_token)
                        activation_token = _scope_debug.push_activation(self)
                        stack.callback(_scope_debug.pop_activation, activation_token)
                    try:
                        yield self
                    except BaseException:
                        # Fast close on exception: revoke owned lifetimes so
                        # external lease holders break immediately; stack unwind
                        # then cancels any spawned tasks in the task group.
                        self.force_close()
                        raise
                    else:
                        await self.aclose()
            finally:
                # Guard against an exception between _do_open and a clean close
                # (e.g. task-group open failure): make sure the opened lifetime
                # is torn down. Only meaningful once _do_open marked us started;
                # idempotent if aclose/force_close already ran.
                if self._started and not self._closed:
                    self.force_close()
        finally:
            self._inside_cm = False

    def lowlevel_start_lifespanless(
        self,
        *,
        parent_leases: Mapping[FxtrScope, Lease[Any]] = _EMPTY_LEASE_MAP,
        extra_derived_leases: Mapping[LifetimeToken[Any], Lease[Any]] = _EMPTY_LEASE_MAP,
    ) -> Self:
        """Start the scope without a task group / lifespan; return ``self``.

        Mints the token + base authorization lease (stored as the scope-level
        ``_scope_auth_lease`` attribute) and builds the arena / ancestor bridges
        / marker, but opens **no** task group — so the scope cannot mount or
        resolve services. The synchronous, explicit-lease counterpart of
        :meth:`open_with_explicit_leases`. Used by the root scope and any
        lowlevel caller that wants a scope without CM structure. Pair with
        :meth:`discard_base_authorization` (closes when the last lease releases) or
        :meth:`force_close` (revoke now) to tear down.

        Args:
            parent_leases: One ``Lease[parent.main]`` per direct parent; keys
                must cover ``self``'s direct parents exactly (empty for a
                parentless scope such as the root). **Ownership is taken.**
                Unlike :meth:`open_with_explicit_leases` via ``async with``,
                these are supplied explicitly rather than cloned from in-context
                authorization (this is a low-level primitive).
            extra_derived_leases: Non-parent derived-input leases keyed by token
                (see :meth:`_do_open`). **Ownership is taken.**

        Returns:
            ``self`` (started), for chaining (``FxtrScope(...).lowlevel_start_lifespanless()``).

        Raises:
            RuntimeError: already started/closed, or ``parent_leases`` does not
                cover exactly the direct parents.
        """
        self._do_open(parent_leases, extra_derived_leases)
        return self

    @contextlib.contextmanager
    def as_active_scope(self) -> Generator[None, None, None]:
        """Context manager that temporarily sets this scope as the active scope.

        Note that non-isolated scopes are automatically set as the active scope during their
        ``async with`` body in the task where they are created. This method can be used to
        temporarily override the active scope for a particular block of code (or to activate
        an isolated scope explicitly).

        This is a pure contextvar set — it does **not** hold a lease on the scope and does
        **not** extend its lifetime. Entering this CM on a draining or never-started scope
        is legal (no ``check_authorized`` call); any user-facing operation invoked through the
        resulting active scope will surface the state via its own ``check_authorized`` guard.
        """
        scope_override_token = ACTIVE_SCOPE.set(self)
        activation_token = _scope_debug.push_activation(self)
        try:
            yield
        finally:
            _scope_debug.pop_activation(activation_token)
            ACTIVE_SCOPE.reset(scope_override_token)

    async def attach_lifespan_to_scope[T](self, cm: contextlib.AbstractAsyncContextManager[T]) -> T:
        """Enter ``cm`` in the scope's task group for the scope's lifetime.

        Spawns a task (via ``task_group.start``, so ``cm`` is fully entered
        before this returns) that enters ``cm`` authorized + with ``self``
        active, signals readiness with ``cm``'s yielded value, drops its
        authorization, parks on the scope's closing event, and on wake exits
        ``cm`` (teardown). Teardown therefore runs **after the token has
        drained** and is **unauthorized** — external-resource cleanup only;
        don't retain a standing ``Lease[main]`` past the yield (it would wedge
        the drain). Multiple lifespans tear down concurrently (not LIFO).

        Args:
            cm: A single-yield async context manager. Setup (before its yield)
                runs authorized and with ``self`` as the active scope; teardown
                (after its yield) runs post-drain, unauthorized.

        Returns:
            The value ``cm`` yields.

        Raises:
            RuntimeError: this context is not authorized for ``self``, or the
                scope is lifespanless (no task group).
        """
        self.check_authorized()
        if self._task_group is None:
            raise RuntimeError(
                f"Scope {self!r} has no task group; cannot attach a lifespan "
                "(lifespanless scopes are not within-lifespan)."
            )
        # Mint a setup lease from this (authorized) caller; the runner owns it.
        setup_lease = self.clone_authorization()
        return await self._task_group.start(self._run_lifespan, cm, setup_lease)

    async def _run_lifespan[
        T
    ](
        self,
        cm: contextlib.AbstractAsyncContextManager[T],
        setup_lease: Lease[Any],
        *,
        task_status: anyio.abc.TaskStatus[T] = anyio.TASK_STATUS_IGNORED,
    ) -> None:
        """Task body for :meth:`attach_lifespan_to_scope` (see its docstring).

        Manages authorization explicitly: a ``proper async with cm`` under an
        ``authorize(setup_lease)`` + ``as_active_scope`` block; drops the
        override right after ``started()`` so the parked task holds no
        ``Lease[main]`` (which would wedge the token-drain wait); parks on the
        closing event; tears down unauthorized after the token drains.

        The count is bumped at task start and decremented in a ``finally``
        (setting ``_lifespans_closed_event`` when it reaches 0) — paired inside
        this task so a ``tg.start`` failure can't leak it; this is what lets
        ``aclose`` wait for structured teardown.
        """
        assert self._closing_event is not None, "lifespan attached to a lifespanless scope"
        self._lifespan_count += 1
        try:
            with self.authorize(setup_lease) as auth, self.as_active_scope():
                async with cm as value:
                    # Setup ran authorized + active; signal readiness, then drop
                    # our standing authorization lease so drain isn't pinned.
                    task_status.started(value)
                    auth.discard()
                    await self._closing_event.wait()
                # cm teardown runs here — token already drained, unauthorized.
            # authorize block exits — idempotent re-discard of the dead override.
        finally:
            self._lifespan_count -= 1
            if self._lifespan_count == 0 and self._lifespans_closed_event is not None:
                self._lifespans_closed_event.set()

    def included_in_active_scope(self) -> bool:
        """Check if this scope is included in the active scope.

        If this returns True, then any IDs in this scope can be loaded with a bare .load() call
        in the active scope, and can also be lifted there. If False, then IDs in this scope may not
        be automatically loadable in the active scope, and must be explicitly copied out.

        This is a pure structural query over scope ancestry and is answerable even on
        closed or draining scopes — it does not reach into scope state.
        """
        return self.included_in_scope(get_active_scope())

    def included_in_scope(self, scope: FxtrScope) -> bool:
        """Check if this scope is included in the given scope.

        If this returns True, then any IDs in this scope can be loaded into the given scope, and
        can also be lifted there. If False, then IDs in this scope may not be automatically loadable
        into the given scope, and must be explicitly copied out.

        A scope is always included in itself.

        This is a pure structural query over scope ancestry and is answerable even on
        closed or draining scopes — it does not reach into scope state.

        Args:
            scope: The scope to check inclusion in.

        Returns:
            True if this scope is included in the given scope, False otherwise.
        """
        return self is scope or id(self) in scope._all_ancestor_scope_ids

    def is_tracked(self, node: FxtrID | EntityRef[Any] | ServiceRef[Any]) -> bool:
        """Check if a node is tracked in this scope.

        For a :class:`ServiceRef`, this is true iff this scope has that handle and
        ``service_type_id`` registered locally as a :class:`ServiceKey` — the same
        notion of "known" as after a local :meth:`resolve_service`.
        """
        self.check_authorized()
        if isinstance(node, ServiceRef):
            return ServiceKey(node.handle, node.service_type_id) in self._known_handles
        return self._is_tracked_internal(self._current_auth_lease().clone(), node)

    def _is_tracked_internal(self, lease: Lease[Any], node: FxtrID | EntityRef[Any]) -> bool:
        """``is_tracked`` without ``check_alive`` — for internal use during drain
        and within helpers called from other methods that already checked.

        Takes ownership of ``lease`` (transferred in via the underlying
        :meth:`MemoryArena.is_tracked` call). The lease must be a
        :class:`Lease` on this scope's main token. Callers in
        ``self``-context typically pass ``self._current_auth_lease().clone()``;
        callers walking into an ancestor (e.g. ``_lift_unchecked``,
        ``_deep_fetch_*``) must pass a lease minted from their *own*
        bridge graph via :meth:`_mint_ancestor_lease` so the query
        works even when the ancestor is mid-drain (the ancestor's
        ``_scope_auth_lease`` is discarded during ``aclose`` but its token
        and arena remain alive until the bridge graph holding clones
        of its lease releases them).

        For an ID with a ``_provenance_parent`` set, both the data ID
        and the provenance ID must be tracked in the arena; we clone
        ``lease`` for the second arena call.
        """
        if isinstance(node, EntityRef):
            node_id = node.entity_id
        else:
            node_id = node
        if node_id._provenance_parent is None:
            return self._main_arena.is_tracked(lease, node_id)
        # Provenance-aware: both data and provenance must be tracked.
        # The arena's ``unwraps_to`` propagation keeps them paired on
        # attach, but we check both explicitly so an asymmetric state
        # (e.g. one half abandoned by hand) reads as not-tracked.
        data_tracked = self._main_arena.is_tracked(lease.clone(), node_id)
        if not data_tracked:
            lease.discard()
            return False
        return self._main_arena.is_tracked(lease, node_id._provenance_parent)

    def clear_cache(self, include_parents: bool = False) -> None:
        """Clear the cache of all loaded entities.

        Note that parent scopes may keep their caches, so values that were lifted from a parent
        scope may still be cached when loaded unless they are also cleared.

        Args:
            include_parents: If True, also clear the caches of all parent scopes.
        """
        # We disallow clear_cache on frozen scopes because get_cached_value relies on cache
        # entries being present, and child scopes that depend on a frozen scope's cache shouldn't
        # see them disappear. We may want to revisit this later — possibly by allowing clear_cache
        # but instead disallowing get_cached_value on frozen scopes. However, there are scope
        # inheritance questions to work through for that (child scopes look up parent caches),
        # so for now we take the conservative approach.
        self.check_authorized()
        self._check_not_frozen_and_not_tracing_loads("clear_cache")
        self._ref_loaded_value_cache.clear()
        self._refs_loaded_since_abandon.clear()
        if include_parents:
            for parent in self._parents:
                parent.clear_cache(include_parents=True)

    def track_builtin(self, builtin_id: FxtrID) -> FxtrID:
        """Track a builtin ID in this scope."""
        self.check_authorized()
        self._check_not_frozen_and_not_tracing_loads("track_builtin")
        return self._main_arena.track_builtin(self._current_auth_lease().clone(), builtin_id)

    @staticmethod
    def _cache_key_for_id(node_id: FxtrID) -> tuple[FxtrID, FxtrID | None]:
        """Cache key for a FxtrID — pairs the data ID with its provenance parent.

        Two FxtrIDs with the same data but different ``_provenance_parent``
        compare equal under ``FxtrID.__eq__`` (which is provenance-blind),
        but the cache wants them to live in distinct slots so each
        provenance trace caches independently.
        """
        return (node_id, node_id._provenance_parent)

    @staticmethod
    def _ref_load_key(
        ref: EntityRef[Any],
    ) -> tuple[FxtrID, FxtrID | None, EntityTypeAnnotation[Any]]:
        """Tuple-key for ``_refs_loaded_since_abandon`` membership.

        Combines the cache-key dimensions with the load target type so the
        same data ID can be tracked separately under different
        provenance traces *and* different target types.
        """
        return (ref.entity_id, ref.entity_id._provenance_parent, ref.target_type)

    def _get_cache_entries_for_key(
        self, key: tuple[FxtrID, FxtrID | None]
    ) -> Iterable[RefLoadCacheEntry]:
        """All cache entries for a specific ``(data_id, provenance_parent)`` pair.

        Used by lift / copy_from_scope / track_from_scope when adopting
        cache entries from a source scope: only entries that match the
        exact provenance trace get adopted.
        """
        return self._ref_loaded_value_cache.get(key, {}).values()

    def _get_cache_entry_for_ref(self, ref: EntityRef[Any]) -> RefLoadCacheEntry | None:
        return self._ref_loaded_value_cache.get(self._cache_key_for_id(ref.entity_id), {}).get(
            ref.target_type
        )

    @overload
    def lift(self, node: FxtrID) -> FxtrID: ...

    @overload
    def lift[T: Entity](self, node: EntityRef[T]) -> EntityRef[T]: ...

    @overload
    def lift[T](self, node: ServiceRef[T]) -> ServiceRef[T]: ...

    def lift(
        self, node: FxtrID | EntityRef[Any] | ServiceRef[Any]
    ) -> FxtrID | EntityRef[Any] | ServiceRef[Any]:
        """Ensure that an entity or service is tracked in this scope, given it was in some parent.

        After `lift` is called, the entity or service will remain tracked in this scope, even if it
        is abandoned from the parent scope.

        Caching behavior (subject to change):
        - If the ID is not tracked, then all cache entries from the first parent that contains the
          ID will also be cached in this scope.
        - If given a EntityRef, and that ref is not cached in this scope, parent scopes will be
          searched for a cached value for the ref even if the ID is already tracked.

        Args:
            node: The ID, entity ref, or service ref to lift.

        Returns:
            The same node, for convenience.
        """
        self.check_authorized()
        self._check_not_frozen_and_not_tracing_loads("lift")
        if isinstance(node, ServiceRef):
            key = ServiceKey(node.handle, node.service_type_id)
            if self._lift_bridge_for_service_key(key) is None:
                raise ValueError(
                    f"Service {node!r} was not registered on scope {self!r} or any of its parents."
                )
            return node
        return self._lift_unchecked(node)

    def _lift_unchecked(self, node: FxtrID | EntityRef[Any]) -> FxtrID | EntityRef[Any]:
        if isinstance(node, EntityRef):
            tracked_here = self._is_tracked_internal(
                self._current_auth_lease().clone(), node.entity_id
            )
            if tracked_here and self._get_cache_entry_for_ref(node) is not None:
                # Already have a cached value for this ref.
                return node

            node_id = node.entity_id
            requested_ref = node
        else:
            if self._is_tracked_internal(self._current_auth_lease().clone(), node):
                # Already have this ID.
                return node

            tracked_here = False
            node_id = node
            requested_ref = None

        # Walk the parent scopes to find the node. We mint leases for
        # each visited ancestor from *our own* bridge graph (via
        # :meth:`_mint_ancestor_lease`) — never from the ancestor's
        # ``_scope_auth_lease``, which may be discarded if the ancestor is
        # mid-drain. The original caller is the only "anchor" whose
        # main_lease is guaranteed alive; ``_populate_ancestor_bridges``
        # ensures we have entries for every transitive ancestor at
        # construction so this minting is always available.
        stack: list[FxtrScope] = [self]
        seen = set[int]([id(self)])
        while stack and (not tracked_here or requested_ref is not None):
            scope = stack.pop()
            for parent in scope._parents:
                if id(parent) not in seen:
                    if not tracked_here:
                        parent_lease = self._mint_ancestor_lease(parent._entered_token)
                        if parent._is_tracked_internal(parent_lease, node_id):
                            # Found it in a parent scope's arena. Install a
                            # same-store sentinel inheritance bridge into our
                            # main arena pointing at the parent's token, then
                            # the memory-store-side track_in_arena will accept
                            # the node under our own token.
                            self._install_sentinel_lift(node_id, parent)
                            # Adopt any cached refs matching the *exact*
                            # (data_id, prov_id) pair from this parent —
                            # we never want to mix entries across
                            # provenance traces.
                            cache_key = self._cache_key_for_id(node_id)
                            for cache_entry in parent._get_cache_entries_for_key(cache_key):
                                self._ref_loaded_value_cache[cache_key][
                                    cache_entry.ref.target_type
                                ] = cache_entry
                            # Mark it tracked.
                            tracked_here = True

                    if requested_ref is not None and (
                        cache_entry := parent._get_cache_entry_for_ref(requested_ref)
                    ):
                        # Found a cached entry for the requested ref.
                        self._ref_loaded_value_cache[self._cache_key_for_id(node_id)][
                            cache_entry.ref.target_type
                        ] = cache_entry
                        requested_ref = None

                    seen.add(id(parent))
                    stack.append(parent)

        if not tracked_here:
            err = ValueError(f"Node {node_id} was not found in any parent scope!")
            self._maybe_attach_global_scope_note(err, node_id)
            raise err

        return node

    def _maybe_attach_global_scope_note(self, err: BaseException, node_id: FxtrID) -> None:
        """Attach a hint to ``err`` if the missing node is in GLOBAL_SCOPE
        but this scope doesn't inherit from it.

        Common pitfall: a scope built with ``isolated=True`` and no
        ``include=[GLOBAL_SCOPE]`` won't see builtin / type-descriptor
        entities (which live in ``GLOBAL_SCOPE``). The resulting "not
        found in any parent scope" error doesn't hint at this. We
        check the node against ``GLOBAL_SCOPE`` and, if it's there,
        add a note explaining how to fix it.
        """
        global GLOBAL_SCOPE
        gs = globals().get("GLOBAL_SCOPE")
        if gs is None or self is gs:
            return
        # Walk parents to see if GLOBAL_SCOPE is in our ancestor chain.
        stack: list[FxtrScope] = list(self._parents)
        seen = {id(self)}
        while stack:
            scope = stack.pop()
            if scope is gs:
                # We *do* inherit from GLOBAL_SCOPE; the node truly
                # isn't there. No hint to add.
                return
            if id(scope) in seen:
                continue
            seen.add(id(scope))
            stack.extend(scope._parents)
        # We don't inherit from GLOBAL_SCOPE; check if the node is there.
        try:
            if gs._is_tracked_internal(gs._current_auth_lease().clone(), node_id):
                err.add_note(
                    "Note: this node is tracked in GLOBAL_SCOPE, but the "
                    "current scope does not inherit from GLOBAL_SCOPE. "
                    "Consider adding GLOBAL_SCOPE to `include=`, or using a "
                    "non-isolated scope. (Builtin entities like type "
                    "descriptors live in GLOBAL_SCOPE by default.)"
                )
        except Exception:  # noqa: BLE001
            # Best-effort hint; don't let it shadow the original error.
            pass

    def _install_sentinel_lift(self, node_id: FxtrID, parent: FxtrScope) -> None:
        """Install a same-store sentinel inheritance bridge for ``node_id``
        on this scope's main arena, pointing at ``parent._token``.

        Looks up the ancestor cap-bridge entry in ``self._cap_bridges`` for
        ``parent._token`` (populated at construction by
        ``_populate_ancestor_bridges``), clones its
        :class:`IndependentLocalBridge` / :class:`DependentLocalBridge`,
        and hands the clone to ``MemoryArena.lazily_attach_from_arena``.
        The memory store auto-detects same-store and stores a
        ``_SameStoreSentinel`` entry keyed on this scope's token;
        subsequent loads recurse under the parent's token via
        ``_ensure_own``.

        For a layered ``node_id`` (one with ``_provenance_parent`` set),
        we attach **both** halves: the provenance parent (the actual
        node we're bridging in) and a bare copy of the data ID. The
        arena's ``unwraps_to`` propagation already lets the data ID be
        *reached* from the bridged provenance node, but ``is_tracked``
        checks the arena's *liveset*, which only gains entries for IDs
        explicitly passed to ``lazily_attach_from_arena``. Attaching
        both keeps the liveset symmetric with what the user model
        promises (both halves "tracked" after lift).

        Equivalent alternative (not used): attach only the prov parent
        and follow up with a sync ``unwrap_provenance`` on the prov
        parent — that goes through ``_track_from_store_or_none`` and
        puts the data ID in our liveset too. This matches how
        ``copy_from_scope`` / ``track_from_scope`` handle the layered
        case. Both shapes produce the same end state; we picked the
        explicit-both-halves form here because the sentinel attach
        path is already passing a list of IDs and adding one more
        is cheap.

        Idempotent on the bridge: the per-(self_token, parent_token)
        bridge record on the memory store is shared across nodes; a
        second lift with the same parent reuses the existing record.
        """
        # Strip layering before handing IDs to the arena. The arena
        # treats every ID as bare; when our caller hands us a layered
        # node_id, we attach the prov parent and a bare copy of the
        # data ID side by side.
        target_ids: list[FxtrID]
        if node_id._provenance_parent is not None:
            target_ids = [
                node_id._provenance_parent,
                FxtrID(raw_data_id=node_id._raw_data_id),
            ]
        else:
            target_ids = [node_id]
        entry = self._cap_bridges.get(parent._entered_token)
        if entry is None:
            raise RuntimeError(
                f"Lift: scope {self!r} has no ancestor cap-bridge entry for "
                f"parent token {parent._token!r}; check that {parent!r} is in "
                "this scope's parent chain."
            )
        if not isinstance(entry.bridge, (IndependentLocalBridge, DependentLocalBridge)):
            # Pipelined ancestors aren't supported here — the memory
            # store wants a synchronously-mintable LocalBridge.
            raise NotImplementedError(
                f"Lift: ancestor bridge for parent token {parent._token!r} is "
                f"not a LocalBridge (got {type(entry.bridge).__name__})."
            )
        cloned_bridge = entry.bridge.clone()
        self._main_arena.lazily_attach_from_arena(
            self._current_auth_lease().clone(),
            target_ids,
            parent._main_arena,
            cloned_bridge,
        )

    async def copy_from_scope[
        IDOrRef: (
            FxtrID,
            EntityRef[Any],
        )
    ](self, node: IDOrRef, source_scope: FxtrScope | None = None) -> IDOrRef:
        """Copy an ID or EntityRef into this scope from another (by default the active scope).

        Does not actually copy the object, but instead copies data referenced by all IDs reachable
        from the object. Can be used to extract entities from the active scope into a different
        scope.

        Caching behavior: If the ID or ref has a cached value in the source scope, that cache entry
        will be duplicated into this scope so that this scope can also return it from cache.

        Args:
            value: The value to copy.
            source_scope: The scope to copy the value from. Defaults to the active scope.

        Returns:
            The copied value itself, for convenience.
        """
        self.check_authorized()
        self._check_not_frozen_and_not_tracing_loads("copy_from_scope")
        source_scope = _resolve_scope(source_scope)
        # Use the internal lift path: the source scope may be draining while we
        # read through it (in-flight operations during another scope's drain),
        # and `source_scope.lift` would raise via check_alive.
        source_scope._lift_unchecked(node)

        if isinstance(node, EntityRef):
            node_id = node.entity_id
        else:
            node_id = node

        # When the ID is layered, copy *only* the provenance parent; the
        # arena's DAG walk follows ``unwraps_to`` as a hard dependency
        # and pulls the data ID into our token's owned set automatically.
        # Halves the work in the common case and avoids double-tracking
        # shared substructure. Below we then call ``unwrap_provenance``
        # on the prov ID — that's a sync arena read that *also* adds the
        # data ID to our liveset, restoring the two-halves-tracked
        # invariant ``is_tracked`` checks for.
        target_id = (
            node_id._provenance_parent if node_id._provenance_parent is not None else node_id
        )
        await self._main_arena.copy_from_arena(
            self._current_auth_lease().clone(),
            source_scope._main_arena,
            source_scope._current_auth_lease().clone(),
            target_id,
        )
        if node_id._provenance_parent is not None:
            self._main_arena.unwrap_provenance(
                self._current_auth_lease().clone(), node_id._provenance_parent
            )

        # Copy cache entries for this exact (data_id, prov_id) key from the
        # source scope. We deliberately don't pull entries with different
        # provenance parents — those represent different traces.
        cache_key = self._cache_key_for_id(node_id)
        for cache_entry in source_scope._get_cache_entries_for_key(cache_key):
            self._ref_loaded_value_cache[cache_key][cache_entry.ref.target_type] = cache_entry

        return node

    def track_from_scope[
        IDOrRef: (
            FxtrID,
            EntityRef[Any],
        )
    ](self, node: IDOrRef, source_scope: FxtrScope | None = None) -> IDOrRef:
        """Sync alternative to copy_from_scope for transferring data between scopes.

        Walks the entity DAG and extends lifetime claims without loading from external
        resources. Errors if any node in the DAG lacks in-memory data and isn't already
        claimed by this scope's token. This is useful for the common pattern of building
        a value in a sub-scope and transferring it back to a parent scope when you know
        everything is in-memory.

        Caching behavior: same as copy_from_scope — cache entries from the source scope
        are duplicated into this scope.

        Args:
            node: The ID or EntityRef to track.
            source_scope: The scope to track from. Defaults to the active scope.

        Returns:
            The node itself, for convenience.
        """
        self.check_authorized()
        self._check_not_frozen_and_not_tracing_loads("track_from_scope")
        source_scope = _resolve_scope(source_scope)
        # Internal lift path — tolerate a draining source_scope.
        source_scope._lift_unchecked(node)

        if isinstance(node, EntityRef):
            node_id = node.entity_id
        else:
            node_id = node

        # Build dest_inheritance_bridges from this scope's
        # ``is_ancestor_or_arena_owner=True`` cap-bridges. We pass the
        # *original* bridge references — `_register_bridge` calls `.clone()`
        # internally when it actually adopts one, so the originals stay
        # intact in `_cap_bridges`. Pipelined bridges are silently skipped
        # (track_from_scope is sync-same-process by definition; pipelined
        # bridges only matter for cross-process dispatch).
        dest_bridges: dict[LifetimeToken[Any], LocalBridge] = {}
        for token, entry in self._cap_bridges.items():
            if not entry.is_ancestor_or_arena_owner:
                continue
            if isinstance(entry.bridge, (IndependentLocalBridge, DependentLocalBridge)):
                dest_bridges[token] = entry.bridge
        # Same single-ID flow as copy_from_scope (track only the prov
        # parent when layered; ``unwraps_to`` propagation pulls the
        # data ID along into our token's owned set), then a sync
        # ``unwrap_provenance`` to put the data ID into our liveset
        # too — see the comment in ``copy_from_scope``.
        target_id = (
            node_id._provenance_parent if node_id._provenance_parent is not None else node_id
        )
        self._main_arena.track_from_memory_arena(
            self._current_auth_lease().clone(),
            source_scope._main_arena,
            source_scope._current_auth_lease().clone(),
            target_id,
            dest_inheritance_bridges=dest_bridges,
        )
        if node_id._provenance_parent is not None:
            self._main_arena.unwrap_provenance(
                self._current_auth_lease().clone(), node_id._provenance_parent
            )

        # Same paired-key cache adoption as copy_from_scope.
        cache_key = self._cache_key_for_id(node_id)
        for cache_entry in source_scope._get_cache_entries_for_key(cache_key):
            self._ref_loaded_value_cache[cache_key][cache_entry.ref.target_type] = cache_entry

        return node

    def abandon(self, node_id: FxtrID | EntityRef[Any]) -> None:
        self.check_authorized()
        self._check_not_frozen_and_not_tracing_loads("abandon")
        if isinstance(node_id, EntityRef):
            node_id = node_id.entity_id

        if not self.is_tracked(node_id):
            raise ValueError(f"Node {node_id} is not tracked in this scope!")
        # Abandon the data ID. If the caller is operating on a layered ID
        # (provenance parent attached), abandon the provenance parent too —
        # the arena's ``unwraps_to`` propagation pulls data alongside on
        # *attach*, but ``abandon`` doesn't cascade, so we have to release
        # both halves explicitly.
        self._main_arena.abandon(self._current_auth_lease().clone(), node_id)
        if node_id._provenance_parent is not None:
            self._main_arena.abandon(self._current_auth_lease().clone(), node_id._provenance_parent)

        # Remove cached ref entries for this exact (data_id, prov_id) key.
        self._ref_loaded_value_cache.pop(self._cache_key_for_id(node_id), None)

        # Clear the `_refs_loaded_since_abandon` set. This is a conservative measure to ensure that
        # any IDs that would become tracked during a load are not accidentally re-used without
        # explicitly tracking them again.
        self._refs_loaded_since_abandon.clear()

    def store[T: Entity](self, entity: T) -> EntityRef[T]:
        """Store an entity into this scope, producing a ref to it.

        Args:
            entity: The entity to store.

        Returns:
            A ref to the stored entity.
        """
        self.check_authorized()
        self._check_not_frozen_and_not_tracing_loads("store")

        # Run the handler, but don't cache the resulting ref yet.
        # We don't know if it is immutable and we can't guarantee that
        # storing and loading is a roundtrip operation, so it's possible loading the entity back
        # will produce a different Python object. This entity can be cached the first time it is
        # actually loaded.
        with self.as_active_scope():
            entity_ref = entity._fxtr_store_impl()
        return entity_ref

    def store_cbor(self, cbor: FxtrCbor) -> FxtrID:
        """Store a CBOR-serialized entity into this scope.

        store_cbor is primarily intended to be used to implement the _fxtr_store_impl method of
        Entity subclasses. Most end users should use Entity.store or FxtrScope.store(entity)
        instead.

        Dispatch is controlled by ``self._store_provenance``:

        - ``"strip"``: always take the **bare** path. Every child ID is
          projected to its bare data form before being handed to the
          arena; child provenance is discarded silently.
        - ``"if_marked"``: take the **prov** path iff the cbor's
          ``$fxtr.ctx`` marker carries a provenance parent; otherwise
          bare.
        - ``"auto"`` (default): take the **prov** path iff the marker
          carries a provenance parent *or* any non-marker child
          reference in the cbor does. In the second case the marker
          is substituted with this scope's layered marker (its own
          c-node if it has a ``provenance_context``, otherwise a
          lazily-minted fallback c-node via
          :meth:`_get_or_mint_fallback_or_own_marker`).

        The **bare** path: each child id is lifted and projected to
        its bare data form (arenas only see FxtrIDs without
        ``_provenance_parent``); the cbor is stored as a d-node and
        the returned FxtrID has no ``_provenance_parent``.

        The **prov** path: every FxtrID in the cbor is projected to
        its provenance form (the ID's existing prov parent if it has
        one, otherwise a freshly-created ``u``-node wrapping it). The
        projected cbor is stored as an n-node; the returned FxtrID
        pairs the unwrapped data ID with the n-node as
        ``_provenance_parent``.

        Args:
            cbor: The CBOR-serialized entity to store.

        Returns:
            The ID of the stored entity.
        """
        self.check_authorized()
        self._check_not_frozen_and_not_tracing_loads("store_cbor")

        mode = self._store_provenance
        ctx = cbor.get("$fxtr.ctx") if isinstance(cbor, dict) else None
        marker_has_prov = (
            isinstance(ctx, RawFxtrEntityMarkerID) and ctx.inner_id._provenance_parent is not None
        )

        if mode == "strip":
            return self._store_cbor_bare_path(cbor)

        if marker_has_prov:
            return self._store_cbor_prov_path(cbor)

        if mode == "if_marked":
            return self._store_cbor_bare_path(cbor)

        # mode == "auto" with a bare marker. Scan for any non-marker
        # child reference that carries a provenance parent.
        any_child_has_prov = False

        def _scan(
            typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
        ) -> None:
            nonlocal any_child_has_prov
            if isinstance(typed_id, RawFxtrEntityMarkerID):
                return
            if typed_id.inner_id._provenance_parent is not None:
                any_child_has_prov = True

        arena_util.visit_ids(cbor, _scan)

        if not any_child_has_prov:
            return self._store_cbor_bare_path(cbor)

        # Auto-override: substitute the marker with our own layered
        # marker (existing provenance_context c-node, or lazily-minted
        # fallback) and take the prov path.
        new_marker = self._get_or_mint_fallback_or_own_marker()
        assert isinstance(cbor, dict), (
            "store_cbor under auto mode reached the override branch with"
            " a non-dict cbor; this shouldn't happen since any_child_has_prov"
            " implies the cbor contains children, which implies it's a dict."
        )
        rewritten_cbor: FxtrCbor = {
            **cbor,
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=new_marker),
        }
        return self._store_cbor_prov_path(rewritten_cbor)

    def _store_cbor_bare_path(self, cbor: FxtrCbor) -> FxtrID:
        """Project every child id to its bare data form and store as a d-node.

        Arenas only see FxtrIDs without a ``_provenance_parent``; children
        may arrive layered (e.g. lifted in from a provenance-bearing
        source), so they're stripped to their bare data form here.
        """

        def _bare_project(
            typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
        ) -> FxtrID:
            fid = typed_id.inner_id
            self.lift(fid)
            if fid._provenance_parent is None:
                return fid
            return FxtrID(raw_data_id=fid._raw_data_id)

        projected_cbor = arena_util.visit_and_project_ids(cbor, _bare_project)
        return self._main_arena.store(self._current_auth_lease().clone(), projected_cbor)

    def _store_cbor_prov_path(self, cbor: FxtrCbor) -> FxtrID:
        """Project every child id to provenance form and store as an n-node.

        Each FxtrID in the cbor is projected to its provenance form (its
        existing prov parent if it has one, otherwise a freshly-created
        ``u``-node wrapping it). The projected cbor is stored via the
        arena's normal d-/n-node path; storing a cbor whose marker is a
        c-node automatically produces an n-node and populates the
        unwraps_to chain to the corresponding d-node.
        """

        def _project(
            typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
        ) -> FxtrID:
            fid = typed_id.inner_id
            # Ensure the data ID is tracked here. (For an already-layered
            # FxtrID this also brings the prov parent along by virtue of
            # the layered is_tracked / lift paths.)
            self.lift(fid)
            if fid._provenance_parent is not None:
                # Already provenance-bearing — strip the layering and
                # use the bare prov ID as the projected ID. The
                # ``self.lift(fid)`` above already attached the prov
                # ID into our arena.
                return FxtrID(raw_data_id=fid._provenance_parent._raw_data_id)
            # Bare data ID — wrap in a freshly-materialized u-node
            # tracked under our token.
            return self._main_arena.create_u_node(self._current_auth_lease().clone(), fid)

        projected_cbor = arena_util.visit_and_project_ids(cbor, _project)
        n_id = self._main_arena.store(self._current_auth_lease().clone(), projected_cbor)
        d_id = self._main_arena.unwrap_provenance(self._current_auth_lease().clone(), n_id)
        assert d_id is not None, (
            f"Arena returned an n-node {n_id} with unknown unwraps_to chain;"
            " this is a memory-arena bug."
        )
        return FxtrID(raw_data_id=d_id._raw_data_id, provenance_parent=n_id)

    async def _load_arena_cbor(self, node_id: FxtrID) -> FxtrCbor:
        """Load the cbor for ``node_id``, applying provenance rewrap when layered.

        For a bare ``node_id`` (no ``_provenance_parent``) this is a thin
        wrapper over ``_main_arena.load``: the arena gives back the
        d-node's content with data-level children.

        For a layered ID, we load the *provenance form* (the
        n-node) — its content has provenance-level children — and walk
        the cbor, rewrapping each child FxtrID to its layered form
        (data ID, prov parent) by sync-unwrapping the prov ID via
        ``MemoryArena.unwrap_provenance``. The result is cbor whose
        children carry their full provenance trace, so deserialization
        handlers naturally produce layered child refs that propagate
        the trace through subsequent loads.
        """
        if node_id._provenance_parent is None:
            return await self._main_arena.load(self._current_auth_lease().clone(), node_id)

        prov_cbor = await self._main_arena.load(
            self._current_auth_lease().clone(), node_id._provenance_parent
        )

        def _rewrap(
            typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
        ) -> FxtrID:
            child_prov = typed_id.inner_id
            child_data = self._main_arena.unwrap_provenance(
                self._current_auth_lease().clone(), child_prov
            )
            assert child_data is not None, (
                f"Loaded prov-form cbor referenced {child_prov} but the arena"
                " has no unwraps_to chain for it; this is a memory-arena bug."
            )
            return FxtrID(raw_data_id=child_data._raw_data_id, provenance_parent=child_prov)

        return arena_util.visit_and_project_ids(prov_cbor, _rewrap)

    async def load[T: Entity](self, ref: EntityRef[T]) -> T:
        """Load an EntityRef in this scope.

        This method loads the data corresponding to the referenced ID, and then converts it into
        the requested target type.

        Args:
            ref: The EntityRef to load.

        Returns:
            The loaded value.
        """
        # Internal invariants:
        # - If self._load_tracer is set, we always push the result of this load to it, regardless
        #   of whether it is cached or not.
        # - Whenever we return a value from cache, we must check all side effects of the cached
        #   load via _load_for_side_effects.
        self.check_authorized()
        self._check_not_frozen("load")
        # Check if we have a cached load entry for this ref already.
        cached_value = self._get_cache_entry_for_ref(ref)
        if cached_value is None:
            # Try lifting the value from a parent scope first, to pick up cached entries in a
            # parent scope. This also lifts the ID itself if it was not tracked in this scope.
            self._lift_unchecked(ref)
            cached_value = self._get_cache_entry_for_ref(ref)

        if cached_value is not None:
            # We got a cached value. Make sure all side effects have been appropriately handled.
            await self._load_for_side_effects(cached_value)
            # Log that we loaded this if the parent context is watching.
            if (outer_log := self._load_tracer.get()) is not None:
                outer_log.append(cached_value)
            # Then return the cached value.
            return cached_value.value

        # Not in cache, but we've already called .lift, which means the value should now be
        # tracked in this scope. ``_load_arena_cbor`` handles the layered case
        # (loading the prov form and rewrapping children).
        loaded_cbor = await self._load_arena_cbor(ref.entity_id)
        assert isinstance(loaded_cbor, dict)
        type_field = loaded_cbor["$fxtr.type"]
        assert isinstance(
            type_field, (RawWeakFxtrID, RawStrongFxtrID)
        ), f"Expected RawWeakFxtrID or RawStrongFxtrID for $fxtr.type, got {type(type_field)}"

        matched_handlers = _ENTITY_LOAD_REGISTRY.find_handler_or_union_handlers(ref.target_type)
        supported_handlers: list[
            base_registry.MatchedHandler[Entity, EntityLoadHandler[Entity]]
        ] = []
        failed_handlers: list[base_registry.MatchedHandler[Entity, EntityLoadHandler[Entity]]] = []

        with self.as_active_scope():  # <- for loads in handler_supports_entity_type
            for matched_handler in matched_handlers:
                if await handler_supports_entity_type(matched_handler, type_field.inner_id):
                    supported_handlers.append(matched_handler)
                else:
                    failed_handlers.append(matched_handler)

        if not supported_handlers:
            failed_summaries = []
            for handler in failed_handlers:
                failed_summaries.append(
                    f"- {handler.subquery!r} -> {handler.entry.key!r} via {handler.entry.handler!r}"
                )
            raise ValueError(
                f"Cannot load entity with union target type {ref.target_type}: tried"
                f" {len(failed_handlers)} handlers, but none supported this entity's type"
                f" {type_field.inner_id}. Tried handlers:"
                f"\n{'\n'.join(failed_summaries)}"
            )
        elif len(supported_handlers) > 1:
            conflict_summaries = []
            for handler in supported_handlers:
                conflict_summaries.append(
                    f"- {handler.subquery!r} -> {handler.entry.key!r} via {handler.entry.handler!r}"
                )
            raise ValueError(
                f"Cannot load entity with union target type {ref.target_type}: multiple handlers"
                f" matched this entity's type {type_field.inner_id}. When deserializing an"
                " EntityRef, all union variants must match disjoint types. Matched"
                f" handlers:\n{'\n'.join(conflict_summaries)}\nYou may be able to address this by"
                " defining a type alias for this union that overrides the default handler and"
                " registering a custom entity handler for it which specifies how to break ties,"
                " e.g. via something like\ntype MyAlias = OverrideFxtrHandlers[A | B | C]\n"
            )

        # Check if we happen to already have a cache entry for the more-specific target type,
        # now that we know what it is. (It is possible that the concrete subtype is identical
        # to the original target type, in which case we can ignore it.)
        chosen_handler = supported_handlers[0]
        concrete_subtype_ref = EntityRef(ref.entity_id, chosen_handler.subquery)
        self._lift_unchecked(concrete_subtype_ref)
        if cached_subtype_value := self._get_cache_entry_for_ref(concrete_subtype_ref):
            # Make sure all side effects have been appropriately handled.
            await self._load_for_side_effects(cached_subtype_value)
            # Re-cache under the broader target type.
            new_cache_entry = RefLoadCacheEntry(
                ref, cached_subtype_value.value, cached_subtype_value.side_effect_loads
            )
            self._ref_loaded_value_cache[self._cache_key_for_id(ref.entity_id)][
                ref.target_type
            ] = new_cache_entry
            # Log that we loaded this if the parent context is watching.
            if (outer_log := self._load_tracer.get()) is not None:
                outer_log.append(new_cache_entry)
            # Then return the cached value.
            return cached_subtype_value.value

        # Convert the loaded CBOR into the requested target type, tracking recursive loads in this
        # scope.
        with self.as_active_scope(), self._tracking_loads() as load_log:
            deserialized_entity: DeserializedEntity[T] = typing.cast(
                DeserializedEntity[T],
                await chosen_handler.entry.handler.from_lowlevel_entity_data(
                    loaded_cbor, chosen_handler.subquery
                ),
            )

        load_log = tuple(load_log)

        if deserialized_entity.cacheable:
            try:
                _ = hash(deserialized_entity.value)
            except Exception as exc:
                raise ValueError(
                    f"Entity {ref} is declared cacheable, but its value is not hashable! This"
                    " probably means either the entity has unexpected mutable elements, or was"
                    " mistakenly declared as cacheable."
                ) from exc

            # Entity is immutable, so we can cache the result.
            new_cache_entry = RefLoadCacheEntry(ref, deserialized_entity.value, load_log)
            cache_key = self._cache_key_for_id(ref.entity_id)
            self._ref_loaded_value_cache[cache_key][ref.target_type] = new_cache_entry
            self._refs_loaded_since_abandon.add(self._ref_load_key(ref))

            # Log that we loaded this if the parent context is watching.
            if (outer_log := self._load_tracer.get()) is not None:
                outer_log.append(new_cache_entry)

            # Cache under the more-specific target type also, if it is different.
            if concrete_subtype_ref.target_type != ref.target_type:
                self._ref_loaded_value_cache[cache_key][concrete_subtype_ref.target_type] = (
                    RefLoadCacheEntry(concrete_subtype_ref, deserialized_entity.value, load_log)
                )
                self._refs_loaded_since_abandon.add(self._ref_load_key(concrete_subtype_ref))

        elif (outer_log := self._load_tracer.get()) is not None:
            # Log that this was loaded but wasn't safe to cache, in case this is loaded inside some
            # _other_ entity that _is_ safe to cache.
            outer_log.append(RefLoadUncacheableMarker(ref, load_log))

            # Still mark this ref as safe for cache fast path.
            self._refs_loaded_since_abandon.add(self._ref_load_key(ref))
            if concrete_subtype_ref.target_type != ref.target_type:
                self._refs_loaded_since_abandon.add(self._ref_load_key(concrete_subtype_ref))

        return deserialized_entity.value

    async def _load_for_side_effects(
        self, cached_load: RefLoadCacheEntry | RefLoadUncacheableMarker
    ) -> None:
        """Replay loading operations purely for ther side effects.

        Args:
            cached_load: The cache entry to replay side effects for.
        """
        # Fast path: we've loaded this exact ref in this scope before, and we haven't abandoned
        # anything since. All loading side effects are idempotent so we don't have to do anything
        # else.
        if self._ref_load_key(cached_load.ref) in self._refs_loaded_since_abandon:
            return

        # Slow path: replay side effects of loading and converting this ref, and check if each
        # of them is safe to cache.
        # First load the entity itself, tracking all of its deps, but throw away the value (we
        # already have it cached). For a layered ref, this routes through
        # ``_load_arena_cbor`` so the side-effect replay tracks the prov
        # form's deps (matching what the original load did), not the d-node's.
        _ = await self._load_arena_cbor(cached_load.ref.entity_id)
        # Then replay side effects of loading anything this ref references.
        for side_effect_load in cached_load.side_effect_loads:
            await self._load_for_side_effects(side_effect_load)
        # Now we know this ref has been loaded in this scope before. Mark it safe for fast path.
        self._refs_loaded_since_abandon.add(self._ref_load_key(cached_load.ref))

    @contextlib.contextmanager
    def _tracking_loads(
        self,
    ) -> Generator[list[RefLoadCacheEntry[Any] | RefLoadUncacheableMarker[Any]]]:
        """Internal context manager to track loads for the duration of the context.

        The list of load entries is returned and can be used to replay side effects of loading.
        """
        result: list[RefLoadCacheEntry[Any] | RefLoadUncacheableMarker[Any]] = []
        token = self._load_tracer.set(result)
        try:
            yield result
        finally:
            self._load_tracer.reset(token)

    def get_cached_value[T: Entity](self, ref: EntityRef[T]) -> T:
        """Retrieve a cached value for an EntityRef.

        Used to implement EntityRef.val by returning a cached value if one is availble.

        Args:
            ref: The EntityRef to retrieve a cached value for.

        Returns:
            The cached value.

        Raises:
            EntityNotLoadedError: If the EntityRef is not cached in this scope.
        """
        self.check_authorized()
        cached_value = self._get_cache_entry_for_ref(ref)
        if cached_value is None:
            raise EntityNotLoadedError(
                ref,
                f"{ref} is not cached in this scope, so its value cannot be"
                " accessed synchronously! Please either use EntityRef.load() to load it"
                " asynchronously, or use the helper `deep_load`.",
            )

        # Fast path: we've loaded this exact ref in this scope before, and we haven't abandoned
        # anything since. All loading side effects are idempotent so it is safe to directly return
        # the cached value.
        ref_key = self._ref_load_key(ref)
        if ref_key in self._refs_loaded_since_abandon:
            return cached_value.value

        # Slow path: entity itself is cached, but we aren't sure if all side effects of loading
        # the entity are still valid. Check if every side effect is also safe to cache.
        for side_effect_load in cached_value.side_effect_loads:
            try:
                _ = self.get_cached_value(side_effect_load.ref)
            except EntityNotLoadedError as exc:
                exc.add_note(f"While checking recursive loads performed by {ref}")
                raise

        # All side effects are safe to cache. Mark the ref as loaded and return the value.
        self._refs_loaded_since_abandon.add(ref_key)
        return cached_value.value

    async def deep_load[T](self, thunk: Callable[[], T]) -> T:
        """Load all EntityRefs whose .val are accessed by a thunk, calling it repeatedly if needed.

        This is a convenience function for accessing entities via the EntityRef.val property, when
        it is possible that some of the EntityRefs needed are not yet loaded. It repeatedly calls the
        thunk, and loads any EntityRefs whose value is accessed while the thunk is executing.

        Args:
            thunk: A thunk that returns the value to access. May be called repeatedly.

        Returns:
            The value returned by the thunk.
        """
        self.check_authorized()
        self._check_not_frozen("deep_load")

        loaded_ents: set[EntityRef] = set()

        # Override the active scope for the duration of the deep_load operation.
        with self.as_active_scope():
            while True:
                try:
                    return thunk()
                except EntityNotLoadedError as e:
                    if e.ref in loaded_ents:
                        raise RuntimeError(
                            f"EntityRef {e.ref} was loaded, but the results of the load were not"
                            " cached! Cannot use deep_load with this EntityRef. This can happen"
                            " when an entity is either itself mutable or contains mutable"
                            " elements."
                        ) from e
                    logger.info(f"deep_load triggering load of {e.ref}")
                    await self.load(e.ref)
                    loaded_ents.add(e.ref)

    async def load_cbor(self, node_id: FxtrID) -> FxtrCbor:
        """Load an entity's CBOR data directly from a scope.

        Args:
            node_id: The ID of the entity to load.

        Returns:
            The mutable CBOR-serialized entity.
        """
        self.check_authorized()
        self._check_not_frozen("load_cbor")
        # For simplicity in the presence of caching, we use the EntityRef load logic for _all_ load
        # operations. Loading of entity CBOR uses UnknownMutableEntity to avoid unnecessary
        # freeze/thaw overhead.
        # Avoid circular import:
        from fxtr2.conversion.converters.unknown_entity import UnknownMutableEntity

        fallback_entity = await self.load(EntityRef(node_id, UnknownMutableEntity))
        return fallback_entity.raw_cbor

    async def load_frozen_cbor(self, node_id: FxtrID) -> FrozenFxtrCbor:
        """Load an entity's CBOR data in frozen (immutable) form from a scope.

        The result is cached, so repeated loads of the same entity return the same frozen object.

        Args:
            node_id: The ID of the entity to load.

        Returns:
            The frozen CBOR-serialized entity.
        """
        self.check_authorized()
        self._check_not_frozen("load_frozen_cbor")
        # Avoid circular import:
        from fxtr2.conversion.converters.unknown_entity import UnknownEntity

        fallback_entity = await self.load(EntityRef(node_id, UnknownEntity))
        return fallback_entity.raw_cbor

    # =========================================================================
    # Live-capability mount / attach / resolve.
    # =========================================================================

    def _check_has_task_group(self, op: str) -> None:
        """Guard mount/attach methods: only scopes with a task group host services.

        Every CM-entered scope opens a task group; lifespanless scopes (root,
        lowlevel) do not and therefore cannot mount / resolve services.
        """
        self.check_authorized()
        if self._task_group is None:
            raise RuntimeError(
                f"Scope {self!r} has no task group; cannot {op}. "
                "Enter the scope with `async with` (lifespanless scopes can't host services)."
            )

    def mount_lowlevel_cap_as_service[
        B: LifetimeBrand, T
    ](
        self,
        handle: IRHandle[B],
        service_type: ServiceTypeAnnotation[T],
        service_type_id: FxtrID,
    ) -> ServiceRef[T]:
        """Mount an ``IRHandle`` whose token is this scope's own token.

        Adds :class:`ServiceKey`\\ ``(handle, service_type_id)`` to
        ``self._known_handles`` and ensures a
        :class:`DependentLocalBridge` for ``self._token`` exists with no
        stored input leases and an identity converter (returns
        ``base`` unchanged). We never persistently hold a
        ``Lease[scope.main_token]`` because that would pin our own token
        via the lease's via chain (a case-2 cycle in
        ``lifetime_conversion_mechanics.md``). Each lookup clones
        ``self._scope_auth_lease``.

        Idempotent on the bridge: multiple handles mounted on the same
        scope share one bridge. Each ``ServiceKey`` is tracked
        individually in ``_known_handles`` so resolving an unrelated
        ``ServiceKey`` on the same token through this scope does not
        succeed by accident.

        Args:
            handle: The low-level capability handle. ``handle.token`` must
                be ``self._token``.
            service_type: The target type to encode in the returned ref.
            service_type_id: The runtime type identity for the service.
                Wire-portable hash — *not* required to be tracked in
                this scope. Impls typically still track it (via
                ``track_builtin`` / ``store`` / similar) so the
                ``$fxtr.type`` framework method can return it through
                the per-call return arena, but that's an impl-side
                convention rather than a framework precondition.

        Returns:
            A ``ServiceRef`` carrying ``handle``, ``service_type``, and
            ``service_type_id``.
        """
        self._check_has_task_group("mount_lowlevel_cap_as_service")
        if handle.token is not self._token:
            raise ValueError(
                "mount_lowlevel_cap_as_service requires handle.token to be the "
                f"scope's own token; got {handle.token!r} (handle) vs "
                f"{self._token!r} (scope)."
            )
        key = ServiceKey(handle, service_type_id)
        entry = self._cap_bridges.get(handle.token)
        if entry is None:
            entry = _CapBridgeEntry(
                bridge=DependentLocalBridge(
                    leases={},
                    converter=_identity_dependent_local_converter,
                ),
                handles=set(),
            )
            self._cap_bridges[handle.token] = entry
        entry.handles.add(key)
        self._known_handles.add(key)
        return ServiceRef(handle=handle, target_type=service_type, service_type_id=service_type_id)

    def attach_bridged_lowlevel_cap[
        T
    ](
        self,
        handle: IRHandle[Any],
        service_type: ServiceTypeAnnotation[T],
        service_type_id: FxtrID,
        bridge: LocalOrPipelinedBridge | None,
    ) -> ServiceRef[T]:
        """Attach a low-level cap with a caller-supplied bridge (or reuse an existing entry).

        Generic low-level entry point covering both the
        Independent-cap shape (caller hands in an
        :class:`IndependentLocalBridge` / :class:`IndependentPipelinedBridge`
        wrapping a pre-existing lease whose subject token outlives
        us) and the Dependent-cap shape (caller hands in a
        :class:`DependentLocalBridge` / :class:`DependentPipelinedBridge`
        whose converter mints fresh leases per call from
        ``(stored_inputs.clones(), our_main_lease.clone())`` —
        avoiding the cycle that would arise from persistently holding
        a ``Lease[handle.token]`` whose vias pin our own token).

        ``bridge`` is **required** (no default). Pass ``None``
        explicitly when this scope already has a local cap-bridge
        entry for ``handle.token`` (typically an
        ``is_ancestor_or_arena_owner=True`` entry installed at scope
        entry for one of its derived-input tokens, or a
        previously-mounted service entry) and you just want to
        register the :class:`ServiceKey` on top. With ``None``, the
        call errors if no pre-existing entry is found — there's no
        implicit parent-walk fallback. Callers needing a parent-walk
        lift should call :meth:`_lift_bridge_for_service_key` first.

        Takes ownership of every at-rest lease in ``bridge``.
        Idempotent on the bridge: if one already exists for
        ``handle.token`` (regardless of which method registered it),
        ``bridge.discard_at_rest()`` is called on the supplied bridge
        and the existing entry is reused. The :class:`ServiceKey` is
        added to ``_known_handles`` either way.

        Validation: for :class:`IndependentLocalBridge` we check that
        ``bridge.lease.subject is handle.token`` at attach time — a
        cheap, useful diagnostic for the common typo case. The other
        three variants commit to producing leases on ``handle.token``
        but cannot be statically verified (the pipelined variant's
        keep-alive lease is on the future's result lifetime, not on
        ``handle.token``; the dependent variants' output is determined
        by the converter at mint time). Mismatches in those cases
        surface as a grant-mismatch on first dispatch.

        ``service_type_id`` is the wire-portable hash from the
        :class:`ServiceRef`; it does *not* need to be tracked in this
        scope. To introspect the loadable type identity (for
        ``EntityRef.load`` etc.), dispatch ``$fxtr.type``.
        """
        try:
            if bridge is None:
                # Pure ServiceKey registration on an existing entry —
                # plain bookkeeping, so it is valid on started
                # lifespanless scopes too (the per-call *input scope*
                # registers its arg ServiceKeys this way; the keys are
                # later parent-walk-lifted into TG-bearing scopes for
                # actual resolution). Only authorization is required.
                self.check_authorized()
                # Local token-keyed bridge entry must exist (whether it
                # was installed as an ancestor entry or via a previous
                # ``attach_*`` call). Callers needing a parent-walk lift
                # should use the explicit ``_lift_bridge_for_service_key`` path
                # instead.
                entry = self._cap_bridges.get(handle.token)
                if entry is None:
                    raise RuntimeError(
                        "attach_bridged_lowlevel_cap: bridge=None requires a "
                        "pre-existing cap-bridge entry for "
                        f"handle.token={handle.token!r}, but none was found "
                        f"on {self!r}."
                    )
            else:
                self._check_has_task_group("attach_bridged_lowlevel_cap")
                if isinstance(bridge, IndependentLocalBridge):
                    if bridge.lease.subject is not handle.token:
                        raise ValueError(
                            "IndependentLocalBridge lease subject "
                            f"{bridge.lease.subject!r} does not match handle "
                            f"token {handle.token!r}."
                        )
                entry = self._cap_bridges.get(handle.token)
                if entry is None:
                    entry = _CapBridgeEntry(bridge=bridge, handles=set())
                    self._cap_bridges[handle.token] = entry
                else:
                    # Idempotent collision: discard the supplied bridge
                    # and reuse the existing entry.
                    bridge.discard_at_rest()
        except BaseException:
            if bridge is not None:
                bridge.discard_at_rest()
            raise
        key = ServiceKey(handle, service_type_id)
        entry.handles.add(key)
        self._known_handles.add(key)
        return ServiceRef(handle=handle, target_type=service_type, service_type_id=service_type_id)

    def _peek_independent_keepalive_lease(self, handle: IRHandle[Any]) -> Lease[Any] | None:
        """Return the standing at-rest lease this scope holds on
        ``handle.token``, if its cap-bridge is an
        :class:`IndependentLocalBridge`; otherwise ``None``.

        Read-only and non-consuming: the returned lease is the bridge's
        *stored* keep-alive lease, not a fresh clone. Callers must not
        ``discard`` / ``take`` it — it backs the bridge's per-lookup
        clones for the scope's lifetime. The intended use is to
        ``register_broken_callback`` on it so a holder can observe the
        gating grant being revoked (e.g. a broker that wants to notify
        downstream consumers when a bridged service goes dead).

        Returns ``None`` for bridge shapes that don't hold a single
        standing lease on ``handle.token`` — the pipelined variant
        (keep-alive lease is on a future's result lifetime) and the
        dependent variants (leases are minted per call). Such bridges
        can't be observed for breakage this way.
        """
        entry = self._cap_bridges.get(handle.token)
        if entry is None:
            return None
        if isinstance(entry.bridge, IndependentLocalBridge):
            return entry.bridge.lease
        return None

    async def mount[T](self, impl: FxtrServiceImpl[T]) -> ServiceRef[T]:
        """Mount a ``FxtrServiceImpl`` on this scope.

        Async wrapper that sets ``self`` as the active scope and delegates to
        ``await impl._mount_as_fxtr_service(self)``. The active-scope set ensures
        the impl can use scope-relative APIs (``store``, ``track_builtin``,
        ``get_active_scope``) directly, and ``self`` is also passed explicitly.
        Mirrors how :meth:`Entity._fxtr_store_impl` operates against the active
        scope.

        ``mount`` is async because the impl attaches its dispatch lifespan via
        ``await self.attach_lifespan_to_scope(...)``, whose async setup brings up
        the dispatch task group and registers the handle before the
        ``ServiceRef`` is returned. The impl creates its *own* dispatch task
        group inside that lifespan — no ``task_group`` is passed in.

        Args:
            impl: The service implementation. Must conform to
                ``FxtrServiceImpl[T]``; its ``_mount_as_fxtr_service`` receives
                ``self`` (also the active scope during the call).

        Returns:
            The ``ServiceRef[T]`` produced by the impl's mount logic.

        Raises:
            RuntimeError: If this scope has no task group (lifespanless), or if
                ``__aenter__`` has not yet opened the task group.
        """
        self._check_has_task_group("mount")
        if self._task_group is None:
            raise RuntimeError(
                f"Scope {self!r} has no open task group; mount inside an `async with` body."
            )
        with self.as_active_scope():
            return await impl._mount_as_fxtr_service(self)

    def forget_service(self, ref: ServiceRef[Any]) -> None:
        """Remove a known :class:`ServiceKey` from this scope, releasing the bridge if unused.

        Symmetric counterpart to ``mount`` / ``attach_*`` — use to clean
        up early when a service ref is no longer needed (rather than
        waiting for scope drain). The matching ``ServiceKey(ref.handle,
        ref.service_type_id)`` must be in ``self._known_handles``;
        raises if not.

        If no other ``ServiceKey`` in ``self._known_handles`` shares
        ``ref.handle.token``, the bridge for that token is dropped and
        its leases are discarded immediately. Otherwise the bridge is
        kept (other known keys still depend on it).

        Note that this only affects this scope's own state; bridges in
        parent scopes are untouched. A child scope cannot forget a
        key the parent registered.
        """
        self.check_authorized()
        key = ServiceKey(ref.handle, ref.service_type_id)
        if key not in self._known_handles:
            raise ValueError(f"Service {ref!r} is not registered on scope {self!r}; cannot forget.")
        self._known_handles.discard(key)
        token = ref.handle.token
        entry = self._cap_bridges.get(token)
        # The entry is registered alongside every known key, so it must
        # exist; defensive get() to keep error-path behaviour sane.
        if entry is None:
            return
        entry.handles.discard(key)
        # If any other known key still shares the bridge, keep it alive.
        if entry.handles:
            return
        # Ancestor entries are part of the scope's own structural
        # ancestor-lease tracking; they exist independently of any
        # service key that happens to share the slot. Preserve them
        # — their lease is required for the lift path and is torn
        # down only at ``_on_token_end``.
        if entry.is_ancestor_or_arena_owner:
            return
        del self._cap_bridges[token]
        entry.bridge.discard_at_rest()

    def _find_bridge_for_service_key(
        self, key: ServiceKey
    ) -> tuple[FxtrScope, _CapBridgeEntry] | None:
        """Walk ``self`` → parents looking for the closest scope that knows ``key``.

        Closest scope wins on conflicts. Returns ``(scope, entry)`` where
        ``scope`` is the bridge-owning scope and ``entry`` is its
        ``_cap_bridges`` entry for ``key.handle.token``. Returns
        ``None`` if no scope in the chain has ``key`` registered.

        A scope that "knows" a key is by construction one that owns a
        bridge for ``key.handle.token`` (every mount/attach/lift
        registers both together). Side-effect-free: prefer
        :meth:`_lift_bridge_for_service_key` when you want the bridge (and the
        type_id) to be cached locally on the first walk.
        """
        if key in self._known_handles:
            return (self, self._cap_bridges[key.handle.token])
        for parent in self._parents:
            found = parent._find_bridge_for_service_key(key)
            if found is not None:
                return found
        return None

    def _lift_bridge_for_service_key(self, key: ServiceKey) -> _CapBridgeEntry | None:
        """Ensure ``key`` has a local cap-bridge entry on ``self``.

        Returns the local entry, or ``None`` if no bridge for ``key``
        is reachable from this scope's parent chain. Three behaviours,
        in order:

        1. ``key in self._known_handles`` — already registered locally;
           return the existing entry. Fast path; no walk needed.
        2. Walk parents to verify ``key`` is registered somewhere
           (preserves the magical-prevention rule: a stranger
           ``ServiceKey`` on a token we happen to bridge must not
           resolve). If not reachable, return ``None``.
        3. Reachable. If ``key.handle.token`` is already known
           locally (typical: we ``mount``-ed our own service on
           this token, and a parent later passed us a distinct key
           on the same token), share the existing local entry by
           adding ``key`` to its ``handles`` set — do not lift a
           duplicate bridge. This preserves the invariant that we
           never persistently store a ``Lease[T]`` whose vias would
           pin our own token. Otherwise lift fresh: mint a lease
           via the parent's bridge (concrete or pipelined) and
           install an :class:`IndependentLocalBridge` (concrete) or
           :class:`IndependentPipelinedBridge` (pipelined) entry
           locally. Subsequent walks short-circuit on the local entry.

        ``key.type_id`` is *not* lifted into ``self`` — type_id is
        wire-portable identity, not arena-tracked content.
        """
        if key in self._known_handles:
            return self._cap_bridges[key.handle.token]
        # Walk parents recursively for the closest registered hit. We
        # walk even if we already have a local entry on this token, so
        # an unrelated stranger key on the same token still fails to
        # resolve.
        found: tuple[FxtrScope, _CapBridgeEntry] | None = None
        for parent in self._parents:
            found = parent._find_bridge_for_service_key(key)
            if found is not None:
                break
        if found is None:
            return None
        existing = self._cap_bridges.get(key.handle.token)
        if existing is not None:
            # Token is already bridged locally; share the entry without
            # lifting a duplicate. The local bridge may be Dependent
            # (e.g. our own ``mount`` with identity converter), and
            # installing a fresh ``IndependentLocalBridge`` here would
            # store a ``Lease[handle.token]`` whose vias pin our own
            # token — exactly the cycle case 2 from
            # ``lifetime_conversion_mechanics.md``.
            existing.handles.add(key)
            self._known_handles.add(key)
            return existing
        parent_scope, parent_entry = found
        minted = self._mint_lease_via_ancestor_bridge(
            parent_scope._entered_token, parent_entry.bridge
        )
        bridge: IndependentLocalBridge | IndependentPipelinedBridge
        if isinstance(minted, UnwrapForPipelining):
            bridge = IndependentPipelinedBridge(pipelined=minted)
        else:
            bridge = IndependentLocalBridge(lease=minted)
        entry = _CapBridgeEntry(
            bridge=bridge,
            handles={key},
        )
        self._cap_bridges[key.handle.token] = entry
        self._known_handles.add(key)
        return entry

    def _populate_ancestor_bridges(self) -> None:
        """Install ``is_ancestor_or_arena_owner=True`` entries on ``self._cap_bridges``.

        In the uniform-derived model every direct parent's main token is an
        input to ``self._derived``, so each direct parent gets a
        :class:`DependentLocalBridge` (case-3) ancestor entry whose converter is
        ``self._derived.subtake(base, parent.main_token)`` — a
        ``Lease[parent.main]`` is minted from a ``Lease[self.main]`` via the via
        mechanic, with nothing stored at rest. The structural cascade ("a live
        ``Lease[self.main]`` carries each parent's token as a via") is what keeps
        the parents alive; no separate Independent keepalive clone is needed.

        After the direct entries, walks each parent's own ``_cap_bridges`` for
        transitively-inherited ancestor entries (see
        :meth:`_install_transitive_ancestors_from`). Transitive **Independent**
        entries (e.g. external-arena owners registered via
        ``_register_external_arena_bridge``) clone directly; transitive
        **Dependent** entries install as a *composed* Dependent entry threading
        ``self._derived.subtake`` into the parent's converter.

        These entries are torn down in ``_on_token_end``. ``forget_service``
        skips ``is_ancestor_or_arena_owner=True`` entries, so the ancestor
        leases survive any service-handle traffic on the same slot.

        Pipelined ancestors are rejected with ``TypeError`` — ancestor entries
        must be synchronously usable.
        """
        assert self._derived is not None, "_populate_ancestor_bridges before entry"
        # Direct parents → case-3 dependent ancestors.
        for parent_scope in self._parents:
            if parent_scope._token in self._cap_bridges:
                continue
            self._install_ancestor_bridge(
                parent_scope._entered_token,
                self._make_self_case3_dependent_bridge(parent_scope._entered_token),
            )
        # Extra (non-parent) derived tokens → case-3 dependent ancestors too, so
        # handles on those tokens can attach via ``attach_bridged_lowlevel_cap``.
        # They have no parent scope, so there is no transitive walk for them.
        for extra_token in self._extra_derived_tokens:
            if extra_token in self._cap_bridges:
                continue
            self._install_ancestor_bridge(
                extra_token,
                self._make_self_case3_dependent_bridge(extra_token),
            )
        # Transitive ancestors: every parent carries its own ``_cap_bridges``
        # (its own ancestor entries + any external-arena owners) we inherit.
        for parent_scope in self._parents:
            self._install_transitive_ancestors_from(parent_scope)

    def _install_transitive_ancestors_from(self, parent_scope: FxtrScope) -> None:
        """Walk ``parent_scope._cap_bridges`` for ancestor entries; install on self.

        Two branches per inherited entry (self is always a derived scope now):

        - :class:`IndependentLocalBridge` parent entry (e.g. an external-arena
          owner): clone directly into a matching Independent entry on self.
        - :class:`DependentLocalBridge` parent entry: install a *composed*
          Dependent entry whose converter does ``self._derived.subtake``
          (``Lease[self.main]`` → ``Lease[parent.main]``) and then the parent's
          converter — so a ``Lease[grandparent]`` is mintable from a
          ``Lease[self.main]`` via two hops without needing the parent's main
          lease at lookup time.

        Pipelined transitive ancestors raise ``TypeError`` (ancestor leases must
        be synchronously usable).
        """
        for anc_token, anc_entry in parent_scope._cap_bridges.items():
            if not anc_entry.is_ancestor_or_arena_owner:
                continue
            if anc_token in self._cap_bridges:
                continue
            anc_bridge = anc_entry.bridge
            if isinstance(anc_bridge, IndependentPipelinedBridge | DependentPipelinedBridge):
                raise TypeError(
                    f"Pipelined ancestor bridges are not supported (token={anc_token!r})."
                )
            if isinstance(anc_bridge, IndependentLocalBridge):
                self._install_ancestor_entry(anc_token, anc_bridge.lease.clone())
                continue
            assert isinstance(anc_bridge, DependentLocalBridge)
            # Compose self._derived.subtake (base → Lease[parent.main]) with the
            # parent's converter.
            self._install_ancestor_bridge(
                anc_token,
                self._make_composed_dependent_bridge(parent_scope._entered_token, anc_bridge),
            )

    def _make_self_case3_dependent_bridge(
        self, target_token: LifetimeToken[Any]
    ) -> DependentLocalBridge:
        """Build a case-3 :class:`DependentLocalBridge` for one of our derived inputs.

        Empty stored ``leases`` — the converter's ``base`` (a
        ``Lease[self.main]``) carries the target token as a via;
        ``self._derived.subtake`` consumes ``base`` and returns the
        via clone.
        """
        derived = self._derived
        assert derived is not None

        def converter(
            input_leases: dict[LifetimeToken[Any], Lease[Any]], base: Lease[Any]
        ) -> Lease[Any]:
            for v in input_leases.values():
                v.discard()
            return derived.subtake(base, target_token)

        return DependentLocalBridge(leases={}, converter=converter)

    def _make_composed_dependent_bridge(
        self,
        parent_main_token: LifetimeToken[Any],
        parent_bridge: DependentLocalBridge,
    ) -> DependentLocalBridge:
        """Compose self's case-3 subtake with a parent's Dependent bridge.

        The parent's bridge's converter expects ``base =
        Lease[parent_main_token]`` and consumes the supplied input-lease
        clones. Our composed converter starts from ``base =
        Lease[self.main_token]``, runs ``self._derived.subtake`` to mint
        the parent main lease, and threads through to the parent's
        converter unchanged.

        Stored ``leases`` are clones of the parent bridge's at-rest
        leases — taken eagerly so the composed bridge is independent of
        the parent's bridge object beyond the captured converter
        pointer.
        """
        derived = self._derived
        assert derived is not None
        parent_converter = parent_bridge.converter
        cloned_leases = {tok: lease.clone() for tok, lease in parent_bridge.leases.items()}

        def composed(
            input_leases: dict[LifetimeToken[Any], Lease[Any]], base: Lease[Any]
        ) -> Lease[Any]:
            parent_lease = derived.subtake(base, parent_main_token)
            return parent_converter(input_leases, parent_lease)

        return DependentLocalBridge(leases=cloned_leases, converter=composed)

    def _install_ancestor_entry(self, token: LifetimeToken[Any], lease: Lease[Any]) -> None:
        """Install one ``is_ancestor_or_arena_owner=True`` :class:`IndependentLocalBridge`.

        Takes ownership of ``lease``. If an entry already exists for
        ``token`` (e.g. we encountered the same ancestor through two
        different parents), the supplied lease is discarded and the
        existing entry is left as-is.
        """
        self._install_ancestor_bridge(token, IndependentLocalBridge(lease=lease.take()))

    def _install_ancestor_bridge(self, token: LifetimeToken[Any], bridge: LocalBridge) -> None:
        """Install one ``is_ancestor_or_arena_owner=True`` entry from a pre-built bridge.

        Idempotent on collision: if an entry already exists for
        ``token``, the supplied bridge is discarded via
        ``bridge.discard_at_rest()`` and the existing entry is left
        as-is.
        """
        if token in self._cap_bridges:
            bridge.discard_at_rest()
            return
        self._cap_bridges[token] = _CapBridgeEntry(
            bridge=bridge,
            handles=set(),
            is_ancestor_or_arena_owner=True,
        )

    def _register_external_arena_bridge(self, token: LifetimeToken[Any], lease: Lease[Any]) -> None:
        """Register a non-ancestor external-arena bridge on ``self._cap_bridges``.

        Used by namespace-mount and similar paths that own an arena whose
        lifetime is independent of any ancestor scope but whose lease this
        scope must hold for as long as it might lift the arena's data into
        its own memory store. Installed as
        ``is_ancestor_or_arena_owner=True`` so ``forget_service`` can't tear
        it down on empty handles. ``lease`` is consumed (``.take()``).

        Idempotent on collision (same token already in ``_cap_bridges``):
        discards the supplied lease and leaves the existing entry alone.
        """
        self._install_ancestor_entry(token, lease)

    def _mint_ancestor_lease(self, token: LifetimeToken[Any]) -> Lease[Any]:
        """Mint a fresh ``Lease[token]`` from this scope's ancestor entries.

        - ``token is self._token``: clone this context's current authorization
          (override-or-base).
        - Otherwise: look up an ``is_ancestor_or_arena_owner=True`` entry
          for ``token``; for an :class:`IndependentLocalBridge` clone
          the stored lease, for a :class:`DependentLocalBridge` run the
          converter against a clone of our own authorization (the bridge's
          contract is that its converter accepts a
          ``Lease[self.main_token]``).
        - Pipelined ancestor entries raise ``TypeError`` (ancestor
          leases must be synchronously mintable).

        Used both directly (e.g. memory-store registration) and as the
        base-sourcing helper for :meth:`_mint_lease_via_ancestor_bridge`.
        """
        if token is self._token:
            return self._current_auth_lease().clone()
        anc_entry = self._cap_bridges.get(token)
        if anc_entry is None or not anc_entry.is_ancestor_or_arena_owner:
            raise RuntimeError(f"no ancestor entry on {self!r} for token {token!r}.")
        anc_bridge = anc_entry.bridge
        if isinstance(anc_bridge, IndependentLocalBridge):
            return anc_bridge.lease.clone()
        if isinstance(anc_bridge, DependentLocalBridge):
            local_clones = {tok: l.clone() for tok, l in anc_bridge.leases.items()}
            base = self._current_auth_lease().clone()
            return anc_bridge.converter(local_clones, base)
        # Pipelined variants.
        raise TypeError(f"Pipelined ancestor bridges are not supported (token={token!r}).")

    def _mint_lease_via_ancestor_bridge(
        self,
        ancestor_main_token: LifetimeToken[Any],
        bridge: LocalOrPipelinedBridge,
    ) -> Lease[Any] | UnwrapForPipelining[Lease[Any], Any]:
        """Mint a fresh lease from a bridge held by ``self`` or an ancestor.

        For an :class:`IndependentLocalBridge` /
        :class:`IndependentPipelinedBridge`, clones the stored value —
        no ancestor base needed.

        For a :class:`DependentLocalBridge` /
        :class:`DependentPipelinedBridge`, the converter wants a
        ``base = Lease[ancestor_main_token]``. We source it via
        :meth:`_mint_ancestor_lease`, which handles both Independent and
        Dependent ancestor entries (the latter via case-3 subtake on
        ``self._derived``). Sourcing from our own structural ancestor
        leases — rather than from the bridge-owning scope's
        ``_scope_auth_lease`` — is what makes the lift work even when an
        ancestor scope is mid-drain (its ``_scope_auth_lease`` is gone, but
        our ancestor entry is alive for our entire lifetime).
        """
        if isinstance(bridge, IndependentLocalBridge):
            return bridge.lease.clone()
        if isinstance(bridge, IndependentPipelinedBridge):
            return UnwrapForPipelining(
                future=bridge.pipelined.future,
                lease=bridge.pipelined.lease.clone(),
            )
        base = self._mint_ancestor_lease(ancestor_main_token)
        if isinstance(bridge, DependentLocalBridge):
            local_clones = {tok: lease.clone() for tok, lease in bridge.leases.items()}
            return bridge.converter(local_clones, base)
        # DependentPipelinedBridge: clone the (possibly-pipelined) inputs.
        cloned = bridge.clone()
        return cloned.converter(cloned.leases, base)

    async def resolve_service[T](self, ref: ServiceRef[T]) -> T:
        """Resolve ``ref`` against this scope into a usable ``T``.

        Ensures ``ref.handle`` has a local cap-bridge entry on ``self``
        (lifting from a parent if needed via
        :meth:`_lift_bridge_for_service_key`), looks up a resolver in
        the registry by ``ref.target_type``, and invokes it with a
        fresh :class:`ServiceProxyContext` bound to ``(self, ref.handle,
        ref.service_type_id)``.

        Public counterpart to :meth:`ServiceRef.resolve` — the latter
        is a thin convenience that defaults the home scope to the
        active scope and delegates here.

        After resolve, the matching :class:`ServiceKey` is in
        ``self._known_handles`` and a corresponding entry lives in
        ``self._cap_bridges`` — subsequent resolves of the same key
        short-circuit and we no longer depend on the parent that
        originally registered it. ``ref.service_type_id`` is *not*
        tracked in ``self``'s main arena (it's just wire-portable
        identity); proxies expose it as an opaque hash via
        ``ctx.service_type_id`` and surface the loadable form by
        dispatching ``$fxtr.type``.

        Requires this scope to be **mountable** — services can only be
        resolved against scopes whose task group is open, since the
        resolved proxy may need to fork arenas / spawn dispatch tasks.
        """
        self.check_authorized()
        if self._task_group is None:
            raise RuntimeError(
                f"Scope {self!r} has no task group; cannot resolve services. "
                "Enter the scope with `async with` (lifespanless scopes can't "
                "host or resolve services)."
            )
        key = ServiceKey(ref.handle, ref.service_type_id)
        if self._lift_bridge_for_service_key(key) is None:
            raise RuntimeError(
                f"Service {ref!r} is not registered on scope {self!r} or any of its ancestors."
            )
        context = ServiceProxyContext(
            home_scope=self,
            proxy_handle=ref.handle,
            service_type_id=ref.service_type_id,
        )
        matched = _SERVICE_RESOLVER_REGISTRY.find_handler(ref.target_type)
        return await matched.entry.handler(context, matched.subquery)

    async def bridge_service_from_scope[
        T
    ](
        self,
        ref: ServiceRef[T],
        source_scope: FxtrScope,
        *,
        keepalive_via: Literal["independent_lease", "concurrent_child_bridge"],
    ) -> None:
        """Bridge ``ref`` from ``source_scope`` into this scope (``dest``) so
        ``dest`` can resolve and call it.

        For the bigger picture (mounting services, lifespans, and all the ways to
        move a service between scopes), see ``v2/notes/scope_service_lifetimes.md``.

        ``keepalive_via`` is **required**: it picks how ``source_scope`` is kept
        alive relative to ``dest``, which constrains how you may arrange the two
        scopes. The rule both modes share: **``source`` must never be forced to
        finish closing before ``dest``.** Concretely, ``source`` must not be
        ``async with``-nested *inside* ``dest`` in the same task — the inner scope
        must close first, but ``dest`` is holding it open, so it deadlocks. (This
        "block nesting" is a per-task runtime property and is **not checked** —
        it's your responsibility. The scope-parent relationship the modes require
        *is* checked.)

        - ``"independent_lease"`` — ``dest`` keeps ``source`` alive until ``dest``
          drains, so **``source`` closes after ``dest``**. ``source`` and
          ``dest`` are typically unrelated scopes.

          * OK — run concurrently, or nest ``dest`` inside ``source``
            (``async with source: async with dest:``).
          * BAD — nest ``source`` inside ``dest`` (deadlock).

        - ``"concurrent_child_bridge"`` — ``dest`` and ``source`` **close
          together**: closing ``dest`` also closes ``source``. ``ref`` must be
          mounted on ``source`` itself, and ``source`` must be a **direct
          scope-child of ``dest`` with no other non-parent inputs** (checked).
          The RPC service-return shape — a service mounted on a per-call child
          scope, handed back to and kept usable by the caller.

          * OK — ``source`` (a child of ``dest``) runs in a concurrent task;
            closing ``dest`` cascades ``source`` closed.
          * BAD — nest ``source`` inside ``dest`` in one task (deadlock).
          * N/A — ``source`` cannot be outside ``dest`` (it derives from it).

        ``ref.service_type_id`` is not tracked into ``dest`` (it's wire-portable
        identity, not arena content); dispatch ``$fxtr.type`` for the loadable
        form.

        Args:
            ref: The :class:`ServiceRef` to bridge.
            source_scope: The scope that knows the ref. Must not be ``self``.
            keepalive_via: Keep-alive strategy (see above).

        Raises:
            ValueError: ``source_scope is self``; ``ref`` already registered on
                ``self``; ``ref`` not reachable from ``source_scope``; or a
                ``concurrent_child_bridge`` precondition is violated.
            RuntimeError: ``self`` is draining/closed, or lifespanless.
        """
        self.check_authorized()
        self._check_has_task_group("bridge_service_from_scope")
        if source_scope is self:
            raise ValueError("bridge_service_from_scope: source_scope must be different from self.")
        key = ServiceKey(ref.handle, ref.service_type_id)
        if key in self._known_handles:
            raise ValueError(
                f"Service {ref!r} is already registered on {self!r}; "
                "use forget_service first if you want to re-bridge."
            )

        if keepalive_via == "independent_lease":
            bridge = self._build_independent_lease_bridge(ref, source_scope, key)
        elif keepalive_via == "concurrent_child_bridge":
            bridge = self._build_concurrent_child_bridge(ref, source_scope)
        else:
            typing.assert_never(keepalive_via)

        # Attach on self. The type_id isn't tracked locally — it rides on the
        # ServiceKey as a wire-portable hash; consumers that need the loadable
        # form dispatch ``$fxtr.type``.
        self.attach_bridged_lowlevel_cap(ref.handle, ref.target_type, ref.service_type_id, bridge)

    def _build_independent_lease_bridge[
        T
    ](self, ref: ServiceRef[T], source_scope: FxtrScope, key: ServiceKey) -> (
        IndependentLocalBridge | IndependentPipelinedBridge
    ):
        """Build the ``independent_lease`` bridge: clone a standing source lease.

        Verifies ``ref`` is reachable from ``source_scope`` (lifting a co-located
        entry onto source if it was only reachable via source's parents), mints a
        fresh ``Lease[ref.handle.token]`` via source's bridge, and wraps it as the
        matching Independent variant. Dependent shapes on source still produce a
        concrete / pipelined output (the converter runs against source's own
        authorization), so ``self`` only reuses the resulting lease.
        """
        source_entry = source_scope._lift_bridge_for_service_key(key)
        if source_entry is None:
            raise ValueError(
                f"Service {ref!r} is not registered on {source_scope!r} or any of its ancestors."
            )
        minted = source_scope._mint_lease_via_ancestor_bridge(
            source_scope._entered_token, source_entry.bridge
        )
        if isinstance(minted, UnwrapForPipelining):
            return IndependentPipelinedBridge(pipelined=minted)
        # Debug label for the scope-drain report: names the destination so
        # a wedged source drain can say who its bridge lease serves.
        if isinstance(minted, LocalLifetimeLease):
            minted.set_debug_label(f"independent_lease into {self._name or repr(self)}")
        return IndependentLocalBridge(lease=minted)

    def _build_concurrent_child_bridge[
        T
    ](self, ref: ServiceRef[T], source_scope: FxtrScope) -> DependentLocalBridge:
        """Build the ``concurrent_child_bridge`` bridge (see the caller's docstring).

        Validates the preconditions (dest is a direct parent of source; ``ref``
        mounted on source itself; source has no extra derived inputs; no non-dest
        source-parent is a descendant of dest), recovers source's other-input vias
        non-consuming from source's authorization, and returns a
        :class:`DependentLocalBridge` whose converter mints ``Lease[source.main]``
        from a per-call ``base = Lease[self.main]``.
        """
        if self not in source_scope._parents:
            raise ValueError(
                f"concurrent_child_bridge: dest {self!r} must be a direct parent of "
                f"source {source_scope!r}."
            )
        if ref.handle.token is not source_scope.scope_token:
            raise ValueError(
                "concurrent_child_bridge: ref must be mounted on source_scope itself"
                " (ref.handle.token must be source_scope's main token). For a ref reachable only"
                ' via source_scope\'s ancestors, use keepalive_via="independent_lease".'
            )
        if source_scope._extra_derived_tokens:
            raise ValueError(
                "concurrent_child_bridge: source_scope must have no extra (non-parent) "
                "derived inputs — their descendant-of-dest safety can't be statically verified."
            )
        source_scope.check_authorized()
        other_parents = [p for p in source_scope._parents if p is not self]
        for p in other_parents:
            if self.included_in_scope(p):
                raise ValueError(
                    f"concurrent_child_bridge: source parent {p!r} is a descendant of dest "
                    f"{self!r}; its recovered via would carry a dest.main via and reintroduce "
                    "the drain cycle."
                )

        src_derived = source_scope._derived
        assert src_derived is not None
        src_auth = source_scope._current_auth_lease()
        # Recover source's other-input vias (key + non-dest parents) non-consuming
        # (subclone leaves source's authorization intact, so source stays usable).
        stored: dict[LifetimeToken[Any], Lease[Any]] = {}
        try:
            stored[source_scope.key_token] = src_derived.subclone(src_auth, source_scope.key_token)
            for p in other_parents:
                stored[p._entered_token] = src_derived.subclone(src_auth, p._entered_token)
        except BaseException:
            for via in stored.values():
                via.discard()
            raise

        dest_token = self.scope_token

        def converter(
            input_leases: dict[LifetimeToken[Any], Lease[Any]], base: Lease[Any]
        ) -> Lease[Any]:
            # base = Lease[dest.main] (minted per call by dest); input_leases =
            # clones of the stored other-input vias. Combine into the full input
            # set and mint a fresh Lease[source.main].
            forward: dict[LifetimeToken[Any], Lease[Any]] = {dest_token: base}
            forward.update(input_leases)
            return src_derived.exchange_forward(forward)

        return DependentLocalBridge(leases=stored, converter=converter)

    def register_deep_fetch_source[
        B_Y: memory_arena.LifetimeBrand
    ](self, source: abstract_arena.DeepFetchable[B_Y], bridge: LocalBridge,) -> None:
        """Register a deep-fetch source (e.g. a namespace backend) on this scope.

        Takes ownership of every at-rest lease in ``bridge`` (consumed via
        ``bridge.discard_at_rest()`` at scope exit). The caller — typically
        a namespace context manager — must hold its own separate key-lease
        clone(s) so the underlying ULL(s) stay alive while the scope needs
        to scan this source. There is no deregistration: the caller's
        lifetime must outlive this scope.

        On a deep-fetch hit, ``bridge.clone()`` is handed to the memory
        store via ``MemoryArena.lazily_attach_from_arena``; subsequent
        loads through that lazy attach use the cloned bridge to mint
        fresh ``Lease[B_Y]`` values. The bridge variant determines the
        attach pattern (case 1 / case 2 / case 3 in
        ``lifetime_conversion_mechanics.md``):
        ``IndependentLocalBridge`` for case 1 (Y outlives X),
        ``DependentLocalBridge`` for case 2/3.
        """
        self.check_authorized()
        entry: DeepFetchSourceEntry[B_Y] = DeepFetchSourceEntry(
            source=source,
            bridge=bridge,
        )
        self._deep_fetch_sources.append(entry)

    async def _deep_fetch(self, node_id: FxtrID) -> abstract_arena.ReadableArena | None:
        """Deeply fetch an entity from the scope by ID, if possible.

        Searches for the entity in all sources and parent scopes and provides an arena that
        contains the entity if found. Can only be called if the scope is deeply fetchable.

        Args:
            node_id: The ID of the entity to fetch.

        Returns:
            An arena that contains the entity, or None if the entity is not found.
        """
        if not self._is_deeply_fetchable():
            raise RuntimeError("Scope is not deeply fetchable; cannot use _deep_fetch")

        # First, try to fetch it from memory by scanning this node's store and all parent stores.
        if found := await self._deep_fetch_cached(node_id):
            return found

        # If none of our scopes know about it, it's possible it was actually stored in some
        # external namespace or similar external source that is lazily loaded. Scan all stores to
        # try to find it, and if we do, adopt it into the scope that owns the store.
        return await self._deep_fetch_scan_sources(node_id)

    async def _deep_fetch_cached(self, node_id: FxtrID) -> abstract_arena.ReadableArena | None:
        """Deeply fetch an entity that is already tracked in the scope's store or any ancestor's.

        If the node is reachable to our own token (owned or inheritance),
        return a fresh arena bound to our token. Otherwise walk
        transitive ancestors (BFS, closest first): the first ancestor
        whose arena's liveset directly contains the node is the source we
        lift through. We install a single sentinel inheritance bridge
        from ``self``'s arena directly to that ancestor's arena (using
        our own bridge graph from ``_populate_ancestor_bridges``) — no
        per-level recursion, so we never need any ancestor's
        ``_scope_auth_lease`` to be active. Each query into an ancestor uses a
        lease minted from *our* bridges via :meth:`_mint_ancestor_lease`,
        which keeps the walk safe even if intermediate scopes are
        mid-drain.

        Cost-of-skipping-recursion note: the previous recursive shape
        installed sentinel lifts at every intermediate level (parent →
        grandparent, grandparent → great-grandparent, …), so subsequent
        lookups on intermediate scopes hit the cache. This flat version
        only caches on ``self``; intermediate scopes don't benefit from
        our walk. Acceptable in practice because deep_fetch lookups are
        not on the hot path and the sentinel chain on ``self`` is one
        wire-load away from the actual data either way.
        """

        if found := await self._global_store.try_lookup_with_lease(
            self._current_auth_lease().clone(), node_id
        ):
            return found

        # BFS over transitive ancestors so the closest tracker wins.
        from collections import deque

        queue: deque[FxtrScope] = deque(self._parents)
        seen: set[int] = {id(self)}
        for direct_parent in self._parents:
            seen.add(id(direct_parent))
        while queue:
            ancestor = queue.popleft()
            ancestor_lease = self._mint_ancestor_lease(ancestor._entered_token)
            if ancestor._is_tracked_internal(ancestor_lease, node_id):
                # Found. Install one sentinel lift from self → ancestor;
                # the same-store sentinel inheritance handles the lookup
                # crossing arenas. No need to walk back through
                # intermediate parents.
                self._install_sentinel_lift(node_id, ancestor)
                return await self._global_store.try_lookup_with_lease(
                    self._current_auth_lease().clone(), node_id
                )
            for grandparent in ancestor._parents:
                if id(grandparent) not in seen:
                    seen.add(id(grandparent))
                    queue.append(grandparent)
        return None

    async def _deep_fetch_scan_sources(
        self, node_id: FxtrID
    ) -> abstract_arena.ReadableArena | None:
        """Deeply fetch an entity by scanning ``self`` and every transitive ancestor's sources.

        For each entry, call ``source.try_lookup(node_id)`` (no per-call backend lease;
        the bridge is used only in ``lazily_attach_from_arena`` below). On a hit, fork
        a new ``MemoryArena`` bound to this scope's token and register the found arena
        as a lazy inheritance claim via ``lazily_attach_from_arena``, threading a clone
        of the entry's bridge so subsequent loads re-run the same minting flow.
        Returned arena is branded as this scope; the caller ``lift``s as needed.

        Walks transitive ancestors iteratively (BFS) rather than recursing
        through ``parent._deep_fetch_scan_sources`` so we never need any
        ancestor's ``_scope_auth_lease`` to be active — only ``self``'s. Each
        ancestor's ``_deep_fetch_sources`` list is just attribute access;
        on a hit we install the lazy attach into ``self``'s arena using
        ``self._scope_auth_lease`` as the target lease, which is independent of
        the ancestor's drain state.
        """
        from collections import deque

        # Walk self first, then transitive ancestors via BFS.
        queue: deque[FxtrScope] = deque([self])
        seen: set[int] = {id(self)}
        while queue:
            scope = queue.popleft()
            for entry in scope._deep_fetch_sources:
                found = await entry.source.try_lookup(node_id)
                if found is not None:
                    # Adopt it into our memory store, so that if it gets loaded, our store will track
                    # it and adopt its children. This should mean that if someone makes a sequence of
                    # _deep_fetch calls by pointer-chasing the IDs they get, only the first will have to
                    # scan all of the remote stores, and the rest will hit the memory store that already
                    # knows where the data lives.
                    new_arena = self._global_store.new_arena(self._current_auth_lease().clone())
                    new_arena.lazily_attach_from_arena(
                        self._current_auth_lease().clone(),
                        node_id,
                        found,
                        entry.bridge.clone(),
                    )
                    found.close()
                    return new_arena
            for ancestor in scope._parents:
                if id(ancestor) not in seen:
                    seen.add(id(ancestor))
                    queue.append(ancestor)
        return None


# =============================================================================
# Active scope and active scope utilities
# =============================================================================

# Note: calling FxtrScope with isolated=True avoids us circularly asking for ACTIVE_SCOPE while
# setting it up.
GLOBAL_SCOPE = FxtrScope(isolated=True, name="GLOBAL_SCOPE")._start_as_root_scope()
"""The global scope, where top-level entities (e.g. type definitions) are stored."""


class _NoActiveScope:
    """The hidden poison value for ``ACTIVE_SCOPE``.

    Never exposed through the supported interface: ``get_active_scope()``
    raises :class:`NoActiveScopeError` instead of returning it. Installed
    by :func:`no_active_scope` (the block form) and by the task-start
    helpers in ``fxtr2.common.task_utils`` with ``scope=None`` (the task
    form), so regions that deliberately run without an ambient scope turn
    any implicit active-scope use into an immediate, well-located error.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<no active scope>"


_NO_ACTIVE_SCOPE = _NoActiveScope()


class NoActiveScopeError(RuntimeError):
    """Implicit active-scope use inside a region that deliberately has none.

    Raised by ``get_active_scope()`` (and everything routed through it,
    including non-isolated ``FxtrScope`` construction) when the current
    context was set up via :func:`no_active_scope` or a task-start helper
    with ``scope=None``. The remedy is to pass scopes explicitly — see
    ``v2/notes/scope_discipline.md``.
    """


ACTIVE_SCOPE = contextvars.ContextVar[FxtrScope | _NoActiveScope](
    "fxtr_scope.ACTIVE_SCOPE", default=GLOBAL_SCOPE
)
"""The current scope where operations execute by default.

Internal: read through ``get_active_scope()`` (which hides the poison
value) rather than ``.get()``.
"""


def get_active_scope() -> FxtrScope:
    """Return the currently-active scope.

    Returns:
        The scope operations execute against by default in this context.

    Raises:
        NoActiveScopeError: The current context deliberately runs without
            an ambient scope (``no_active_scope()`` /
            ``start_*_with_scope(..., scope=None)``).
    """
    scope = ACTIVE_SCOPE.get()
    if scope is _NO_ACTIVE_SCOPE:
        raise NoActiveScopeError(
            "No active scope: this region deliberately runs without an ambient scope"
            " (it was entered via no_active_scope() or a task started with scope=None)."
            " Pass scopes explicitly instead of relying on the active scope; see"
            " v2/notes/scope_discipline.md."
        )
    assert isinstance(scope, FxtrScope)
    return scope


@contextlib.contextmanager
def no_active_scope() -> Generator[None, None, None]:
    """Run a block with *no* active scope: implicit use fails immediately.

    The block (and any code it calls) sees :class:`NoActiveScopeError`
    from ``get_active_scope()`` — including from non-isolated
    ``FxtrScope`` construction, whose implicit-parent capture goes
    through it — instead of silently inheriting whatever scope happened
    to be ambient. Use it around regions that are supposed to manage
    scopes fully explicitly (scope discipline's strict form); the
    task-start helpers in ``fxtr2.common.task_utils`` apply the same
    state to a whole task via ``scope=None``.

    Pure contextvar manipulation: holds no lease, affects no lifetime,
    restores the previous active scope on exit.

    Yields:
        Nothing; run the region in the ``with`` block.
    """
    token = ACTIVE_SCOPE.set(_NO_ACTIVE_SCOPE)
    try:
        yield
    finally:
        ACTIVE_SCOPE.reset(token)


_ERROR_TEMPLATE_INTO_ACTIVE_SCOPE = """
Calling {type_name}.{method_name} is not allowed in this scope by default, because the currently-active scope is not attached to the {type_name} and might outlive it. To retrieve this value, you can either:
- Explicitly copy the value and all of its transitive dependencies out of the namespace and into memory, by passing `copy_into_scope=True`
- Enter a new temporary active scope that inherits from the namespace, using `async with FxtrScope(include=[namespace.scope]):` and call `retrieve` there
- {block_instruction} `{method_name}` inside the `async with {type_name}(...):` block
"""
_ERROR_TEMPLATE_INTO_PROVIDED_SCOPE = """
Calling {type_name}.{method_name} with into_scope={into_scope} is not allowed without copy_into_scope=True, because the provided scope is not attached to the {type_name} and might outlive it. To retrieve this value, you can either:
- Explicitly copy the value and all of its transitive dependencies out of the namespace and into memory, by passing `copy_into_scope=True`
- Enter a new temporary scope that inherits from the namespace, using `async with FxtrScope(include=[namespace.scope]):`, and call `{method_name}` there with no `into_scope` parameter, or pass that scope as the `into_scope` parameter
- {block_instruction} `{method_name}` inside the `async with {type_name}(...):` block with no `into_scope` parameter
"""
_ERROR_TEMPLATE_BLOCK_NORMAL = "Call"
_ERROR_TEMPLATE_BLOCK_ISOLATED = "Re-open the namespace with isolate_scope=False and call"


def resolve_retrieve_into_scope(
    ns_scope: FxtrScope,
    into_scope: FxtrScope | None,
    copy_into_scope: bool,
    *,
    type_name: str,
    method_name: str,
) -> FxtrScope:
    """Resolve + validate the target scope for a namespace ``retrieve``-family op.

    Shared by every ``FxtrNamespace`` / ``FxtrReadNamespace`` ``_retrieve_impl``:
    when ``into_scope`` is ``None`` the target is the active scope; the lift path
    (``copy_into_scope=False``) additionally requires ``ns_scope`` to be included
    in the target (so the retrieved id, which is attached on ``ns_scope``, stays
    loadable) and otherwise raises a ``ValueError`` pointing at
    ``copy_into_scope=True`` / ``FxtrScope(include=[namespace.scope])``.
    """
    block_instruction = (
        _ERROR_TEMPLATE_BLOCK_ISOLATED if ns_scope.isolated else _ERROR_TEMPLATE_BLOCK_NORMAL
    )
    if into_scope is None:
        if not copy_into_scope and not ns_scope.included_in_active_scope():
            raise ValueError(
                _ERROR_TEMPLATE_INTO_ACTIVE_SCOPE.format(
                    type_name=type_name,
                    method_name=method_name,
                    block_instruction=block_instruction,
                )
            )
        return get_active_scope()
    if not copy_into_scope and not ns_scope.included_in_scope(into_scope):
        raise ValueError(
            _ERROR_TEMPLATE_INTO_PROVIDED_SCOPE.format(
                type_name=type_name,
                method_name=method_name,
                into_scope=f"{into_scope!r}",
                block_instruction=block_instruction,
            )
        )
    return into_scope


def get_active_context_id() -> FxtrID:
    """Get the current context ID for the active scope."""
    return get_active_scope().get_context_marker_id()


def get_active_context_marker() -> RawFxtrEntityMarkerID:
    """Get the current context marker for the active scope."""
    return RawFxtrEntityMarkerID(inner_id=get_active_context_id())


def track_builtin_id(name: str) -> FxtrID:
    """Track a builtin ID in the active scope."""
    scope = get_active_scope()
    return scope._main_arena.track_builtin(
        scope._current_auth_lease().clone(), FxtrID.builtin(name)
    )
