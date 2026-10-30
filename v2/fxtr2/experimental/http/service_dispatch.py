"""Shared dispatch primitives for the stateless service endpoints.

A *service impl* is either:

- a **callable** ``async (method, args, kwargs, ctx) -> value`` (the
  impl routes the method itself), or
- a **mapping** ``{method_name: async (args, kwargs, ctx) -> value}``
  (the adapter routes by name; an unknown name raises
  :class:`UnknownServiceMethodError`).

``ctx`` is endpoint-specific and duck-typed by impls: REST passes a
``RestServiceContext`` (``.scope`` + ``.request``); ws passes a
``WsServiceContext`` (``.scope`` + ``.notify`` + ``.transient``). The
``$fxtr.type`` convention is just a method name an impl may handle
(returning its type entity ``FxtrID``) — no special handling here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeAlias

#: A method-map impl's per-method function: ``(args, kwargs, ctx) -> value``.
MethodFn: TypeAlias = Callable[[list[Any], dict[str, Any], Any], Awaitable[Any]]
#: A callable impl: ``(method, args, kwargs, ctx) -> value``.
CallableImpl: TypeAlias = Callable[[str, list[Any], dict[str, Any], Any], Awaitable[Any]]
#: A registered service impl.
ServiceImpl: TypeAlias = CallableImpl | Mapping[str, MethodFn]


class ServiceError(Exception):
    """Base for service-level dispatch failures (mapped to an error
    response / frame by the endpoints)."""


class UnknownServiceMethodError(ServiceError):
    """A method-map impl was invoked with a method it doesn't define."""


async def invoke_impl(
    impl: ServiceImpl,
    method: str,
    args: list[Any],
    kwargs: dict[str, Any],
    ctx: Any,
) -> Any:
    """Dispatch ``method`` against ``impl`` (callable or method-map).

    Args:
        impl: The service impl — a callable ``(method, args, kwargs,
            ctx)`` or a ``{method: (args, kwargs, ctx)}`` map.
        method: The method name to invoke.
        args: Positional args (already unescaped).
        kwargs: Keyword args (already unescaped).
        ctx: The endpoint-specific context handed to the impl.

    Returns:
        The impl's return value.

    Raises:
        UnknownServiceMethodError: If ``impl`` is a map without
            ``method``.
    """
    if isinstance(impl, Mapping):
        fn = impl.get(method)
        if fn is None:
            raise UnknownServiceMethodError(f"unknown method: {method!r}")
        return await fn(args, kwargs, ctx)
    return await impl(method, args, kwargs, ctx)
