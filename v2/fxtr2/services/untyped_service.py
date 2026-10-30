"""Untyped-service support: ``@service_method`` discovery + dynamic proxy.

Two layers built on top of
:mod:`fxtr2.services.service_cap_common`:

- :class:`SimpleAsyncServiceImpl` — a generic
  :class:`CommonServiceImpl` whose ``_handle_invoke`` dispatches by
  walking ``@service_method``-tagged async methods on ``self``.
  Strategy-agnostic on the target type ``T``; subclasses just supply
  ``_target_type_annotation`` and ``_service_type_id``.
- :class:`UntypedServiceImpl` —
  ``SimpleAsyncServiceImpl[UnknownService]``, the canonical "no-types"
  service base. Pairs with :class:`UnknownServiceProxy`, the dynamic
  ``__getattr__``-style proxy registered against ``UnknownService``
  in the service-resolver registry.

A future typed-service layer builds on this by layering per-method
conversion in another :class:`CommonServiceImpl` subclass.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Mapping

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import (
    ServiceProxyContext,
    ServiceTypeAnnotation,
    get_active_scope,
    register_service_resolver,
)
from fxtr2.leases.leased_cap_ir import IRException

from .service_cap_common import (
    FXTR_TYPE_METHOD_NAME,
    CommonServiceImpl,
    FxtrServiceData,
    UnknownService,
    invoke_service_method,
    service_method,
    walk_service_methods,
)

# Re-exported for backward compatibility — the decorator's home is
# :mod:`service_cap_common` (alongside the marker attribute it sets),
# so every strategy that uses the tagged-method convention can import
# it from one place.
__all__ = [
    "NoOpService",
    "SimpleAsyncServiceImpl",
    "UnknownServiceProxy",
    "UntypedServiceImpl",
    "service_method",
]


# =============================================================================
# SimpleAsyncServiceImpl — @service_method discovery + _handle_invoke
# =============================================================================


class SimpleAsyncServiceImpl[T](CommonServiceImpl[T]):
    """:class:`CommonServiceImpl` whose ``_handle_invoke`` dispatches via ``@service_method``.

    Subclasses tag exposed methods with :func:`service_method`; any
    ``async def`` method so tagged is reachable over the wire by name.
    Default :meth:`_service_exposed_methods` walks the MRO collecting
    tagged methods; subclasses may override for more dynamic discovery.

    Generic on the target-type annotation ``T``; subclasses still
    implement :meth:`_target_type_annotation` and
    :meth:`_service_type_id`.

    Each method invocation runs in a fresh per-call scope that is a
    child of the impl's mount scope; data and services from any other
    scope must be explicitly copied or bridged in (e.g. via
    :meth:`FxtrScope.copy_from_scope` /
    :meth:`FxtrScope.bridge_service_from_scope` at construction time).
    """

    def _service_exposed_methods(
        self,
    ) -> Mapping[str, Callable[..., Awaitable[FxtrServiceData]]]:
        """Return the mapping of ``method_name -> bound async method``.

        Walks the MRO via :func:`walk_service_methods`, collecting
        methods marked with ``@service_method``. Subclass-defined
        methods take precedence over inherited ones.

        Subclasses may override to customise discovery (e.g. wrap a list
        of explicitly-declared method names) without changing the
        ``@service_method`` convention.

        Returns:
            A mapping from method name to the bound async method on
            ``self``. The dispatch path looks up incoming method-name
            strings against this mapping.
        """
        return walk_service_methods(self, require_async=True, forbid_dunders=True)

    async def _handle_invoke(
        self, method_name: str, *args: FxtrServiceData, **kwargs: FxtrServiceData
    ) -> FxtrServiceData:
        """Look up ``method_name`` via :meth:`_service_exposed_methods` and invoke it.

        Framework-reserved ``$fxtr.*`` paths never reach here — the
        dispatch infrastructure
        (:func:`fxtr2.services.service_cap_common._run_user_method`)
        routes them to :meth:`CommonServiceImpl._handle_reserved_invoke`
        before this method is invoked.

        Raises ``IRException(category="UnknownMethod")`` if no
        ``@service_method``-tagged method matches. The dispatch body
        (in :mod:`fxtr2.services.service_cap_common`) wraps any other
        exception as ``IRException(category=type(exc).__name__)``.
        """
        methods = self._service_exposed_methods()
        method = methods.get(method_name)
        if method is None:
            raise IRException(
                message=(
                    f"Method {method_name!r} is not exposed on "
                    f"{type(self).__name__}. "
                    f"Available: {sorted(methods.keys())!r}"
                ),
                category="UnknownMethod",
            )
        return await method(*args, **kwargs)


# =============================================================================
# UntypedServiceImpl — SimpleAsyncServiceImpl[UnknownService]
# =============================================================================


_UNTYPED_SERVICE_TYPE_ID_NAME = "untyped_service"
"""Builtin name used by :class:`UntypedServiceImpl._service_type_id`.

The corresponding :class:`FxtrID` is ``FxtrID.builtin("untyped_service")``;
all :class:`UntypedServiceImpl` instances share this identity, so
peers cross-recognising "this is an untyped service" works without
any per-impl coordination.
"""


_NOOP_SERVICE_TYPE_ID_NAME = "noop_service"
"""Builtin name used by :class:`NoOpService._service_type_id`.

Used as a placeholder main service for callers (e.g.
:func:`fxtr2.rpc.scope_bridge.open_rpc_scope`) that don't need to
expose any callable interface to the peer. Distinct from the untyped
type id so peers can recognise "this side has no methods to offer"
without having to invoke a method to find out.
"""


class UntypedServiceImpl(SimpleAsyncServiceImpl[UnknownService]):
    """Base for untyped service implementations.

    :class:`SimpleAsyncServiceImpl` with the target type pinned to
    ``UnknownService`` and :meth:`_service_type_id` pinned to the
    builtin ``FxtrID.builtin("untyped_service")``: subclasses inherit
    ``@service_method`` discovery, :meth:`_handle_invoke`, and the
    type-identity wiring for free, and only need to declare their
    service methods.

    Mounting is done via ``await scope.mount(impl)``, which delegates to
    ``await impl._mount_as_fxtr_service(scope)`` on the
    :class:`CommonServiceImpl` base class. The scope must have a task group
    (entered as an async context manager — not a lifespanless scope).

    Each method invocation runs in a fresh per-call scope that is a
    child of the impl's mount scope; data and services from any other
    scope must be explicitly copied or bridged in.
    """

    def _target_type_annotation(self) -> ServiceTypeAnnotation[UnknownService]:
        return UnknownService

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin(_UNTYPED_SERVICE_TYPE_ID_NAME))


# =============================================================================
# NoOpService — a service with no exposed methods
# =============================================================================


class NoOpService(SimpleAsyncServiceImpl[UnknownService]):
    """A no-method service, intended as a placeholder main service.

    Useful for callers that want to enter an RPC bridge without exposing any callable
    interface to the peer (e.g. one-direction client calls, or a peer that's expected
    to do its own shutdown signalling). The peer can resolve a
    :class:`ServiceRef[UnknownService]` against this service and read its
    ``service_type_id`` for runtime introspection (the builtin
    ``FxtrID.builtin("noop_service")``), but every method invocation raises
    ``IRException(category="UnknownMethod")``.

    Pairs with :func:`fxtr2.rpc.scope_bridge.open_rpc_scope`'s ``our_main_service=None``
    convenience: when the caller passes ``None``, the bridge mounts a
    ``NoOpService`` on a fresh isolated host scope so the peer still has something
    structurally valid to receive in its bootstrap packet.
    """

    def _target_type_annotation(self) -> ServiceTypeAnnotation[UnknownService]:
        return UnknownService

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin(_NOOP_SERVICE_TYPE_ID_NAME))


# =============================================================================
# UnknownServiceProxy + resolver registration
# =============================================================================


class UnknownServiceProxy:
    """Dynamic proxy for ``ServiceRef[UnknownService]``.

    Calling ``proxy.foo(*args, **kwargs)`` builds an
    ``IRTagged("fxtr_call", …)`` packet, dispatches it on the bound
    handle, and unwraps the return packet's ``result`` field. All of
    the heavy lifting lives in
    :func:`fxtr2.services.service_cap_common.invoke_service_method`;
    the proxy is just a thin ``__getattr__`` adapter that binds the
    method name.

    The proxy is bound to a :class:`ServiceProxyContext` at resolve
    time; all scope-side operations (lease minting, arena forks, etc.)
    go through that context — the proxy never reaches into ``FxtrScope``
    private members directly.

    Type-id introspection: :meth:`service_type_id` is an async method
    that wire-dispatches ``$fxtr.type`` and returns the impl's runtime
    type identity in *loadable* form — bridged into the home scope's
    main arena via the ordinary return-arena path, so callers can
    follow it through ``EntityRef.load`` etc. (analogous to
    :class:`UnknownEntity`'s ability to inspect the type tag of a
    loaded entity without performing type-specific deserialization).
    Advanced callers who only need the type_id as an opaque hash —
    no loading required — can read ``self._ctx.service_type_id``
    directly, which avoids the wire round-trip but isn't tracked in
    the home scope.

    Dunder names are excluded from the proxy interface so Python's
    introspection (``__class__``, ``__repr__``, etc.) keeps working.
    The :meth:`service_type_id` async method also wins over any
    impl-side ``@service_method``-tagged method named
    ``service_type_id`` (Python's normal descriptor lookup); such a
    method would be unreachable via :class:`UnknownServiceProxy`.
    Authors who want a different reserved-name policy can build their
    own ``ServiceRef[MyOwnUnknownService]`` proxy with a different
    introspection slot.

    Underscore-prefixed names *do* go through the proxy — if the impl
    chooses to expose ``_internal``, the caller can call it.
    """

    def __init__(self, context: ServiceProxyContext) -> None:
        """Bind the proxy to a scope-side context.

        Args:
            context: The :class:`ServiceProxyContext` produced by
                :meth:`FxtrScope.resolve_service`. Carries the home
                scope and the proxy's underlying handle, plus all the
                scope-side helpers the proxy needs.
        """
        self._ctx = context

    def __repr__(self) -> str:
        return f"<UnknownServiceProxy handle={self._ctx.proxy_handle!r}>"

    async def service_type_id(self) -> FxtrID:
        """Look up the impl's runtime type identity by wire dispatch.

        Equivalent to ``await getattr(proxy, "$fxtr.type")()`` — invokes
        the framework-reserved ``$fxtr.type`` method on the bound handle
        and returns the FxtrID the impl declared at mount time. The id
        is bridged into the home scope's main arena via the ordinary
        return-arena path, so callers can :meth:`EntityRef.load` it
        afterwards if it carries a stored type-descriptor entity.

        Advanced callers who only need the type_id as an opaque hash
        (e.g. fast comparison against a known builtin name, no entity
        loading) can read ``self._ctx.service_type_id`` directly: same
        :class:`FxtrID`, no wire round-trip, but not tracked in the
        home scope so it can't be loaded. Use this async method when
        you need the loadable form.
        """
        result = await invoke_service_method(self._ctx, FXTR_TYPE_METHOD_NAME)
        assert isinstance(result, FxtrID), f"$fxtr.type returned non-FxtrID {result!r}"
        return result

    def __getattr__(self, name: str) -> Callable[..., Awaitable[FxtrServiceData]]:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return functools.partial(invoke_service_method, self._ctx, name)


async def _unknown_service_resolver(
    context: ServiceProxyContext,
    target_type: ServiceTypeAnnotation[UnknownService],  # noqa: ARG001
) -> UnknownServiceProxy:
    """Build an :class:`UnknownServiceProxy` bound to ``context``."""
    return UnknownServiceProxy(context)


register_service_resolver(UnknownService, _unknown_service_resolver)
