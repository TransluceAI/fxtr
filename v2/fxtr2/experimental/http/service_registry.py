"""Service registry + discovery for the stateless backend.

A small builder so the discovery response can't drift from the
actually-mounted routes. The author adds services + entities, then
mounts ``registry.routes()`` and the ``/discovery`` handler alongside
the existing entity routes from ``create_fxtr_app(scope, pins)`` (which
is left untouched).

Discovery content is entirely the author's choice (decision
``projects/fxtr_workstation/plans/phase0/part7a_stateless_ws_server/open_questions.md``
Q-1) — the registry is convenience, not policy. Each service
descriptor carries its ``service_type_id`` annotation (same file,
Q-H): the connector reads it to fill records + answer
``allocateGetServiceMetadata`` without an extra ``$fxtr.type`` round
trip.

Descriptor URLs are **relative** (the route path / ws path) unless a
``base_url`` is supplied, in which case they're resolved to absolute
(http→ws for ws services). The client resolves relative URLs against
its own base URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from starlette.responses import JSONResponse
from starlette.routing import WebSocketRoute

from .service_codec import ServiceDescriptor, encode_pin_value
from .service_dispatch import ServiceImpl
from .service_rest_endpoint import rest_service_route
from .service_ws_endpoint import ws_services_endpoint

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.routing import BaseRoute

    from fxtr2.data.entity_core import FxtrID
    from fxtr2.data.entity_ref_scope import FxtrScope


@dataclass(frozen=True)
class _RestEntry:
    discovery_name: str | None  # None → mounted but not listed in /discovery
    path: str
    method: str
    impl: ServiceImpl
    type_id: FxtrID | None


@dataclass(frozen=True)
class _WsEntry:
    routing_name: str  # wire routing key (the descriptor `name`); always required
    discovery_name: str | None  # None → mounted but not listed in /discovery
    ws_path: str
    impl: ServiceImpl
    type_id: FxtrID | None


def _to_ws_url(url: str) -> str:
    """Map an http(s) URL to its ws(s) equivalent.

    Args:
        url: An absolute http/https URL, or a relative path.

    Returns:
        The ws/wss form for an absolute http/https URL; a relative path
        is returned unchanged (the client resolves + picks the scheme).
    """
    parts = urlsplit(url)
    if parts.scheme == "http":
        return urlunsplit(("ws", *parts[1:]))
    if parts.scheme == "https":
        return urlunsplit(("wss", *parts[1:]))
    return url


class ServiceRegistry:
    """Accumulates services + named entities and emits both the route
    table and the matching discovery payload."""

    def __init__(
        self, scope: FxtrScope, *, base_url: str | None = None, provenanced: bool = False
    ) -> None:
        self._scope = scope
        self._base_url = base_url
        self._provenanced = provenanced
        self._entities: dict[str, FxtrID] = {}
        self._rest: list[_RestEntry] = []
        self._ws: list[_WsEntry] = []
        # Discovery names claimed across all services (cross-service unique).
        self._service_names: set[str] = set()
        # Wire routing keys claimed per ws_path (unique within a ws endpoint).
        self._ws_routing: dict[str, set[str]] = {}

    # -- registration -------------------------------------------------

    def add_entity(self, name: str, fxtr_id: FxtrID) -> None:
        """Register a named entity for discovery.

        Args:
            name: The discovery name.
            fxtr_id: The entity's id.

        Raises:
            ValueError: If ``name`` is already registered as an entity.
        """
        if name in self._entities:
            raise ValueError(f"duplicate entity name: {name!r}")
        self._entities[name] = fxtr_id

    def add_rest_service(
        self,
        *,
        path: str,
        impl: ServiceImpl,
        method: str = "POST",
        type_id: FxtrID | None = None,
        discovery_name: str | None = None,
    ) -> ServiceDescriptor:
        """Register a REST service (one route at ``path``).

        A REST service is identified on the wire purely by its url +
        method — it has no wire routing key — so the only name it can
        carry is an optional discovery label.

        Args:
            path: The route path to mount it at.
            impl: The service impl (callable or method-map).
            method: ``"GET"`` or ``"POST"``.
            type_id: The service's type entity id, surfaced as the
                descriptor's ``service_type_id`` annotation.
            discovery_name: The name to list it under in ``/discovery``
                (unique across all services). ``None`` (the default)
                leaves it out of discovery — the route is still mounted;
                an impl reaches it by handing out the returned descriptor.

        Returns:
            The service's :class:`ServiceDescriptor` — useful for a
            non-discoverable service, which an impl can hand out by
            returning the descriptor from an invoke.

        Raises:
            ValueError: If ``discovery_name`` collides with another
                service.
        """
        if discovery_name is not None:
            self._claim_service_name(discovery_name)
        method = method.upper()
        self._rest.append(_RestEntry(discovery_name, path, method, impl, type_id))
        return ServiceDescriptor(
            kind="http",
            url=self._url(path),
            method=method,
            service_type_id=type_id.id_string() if type_id else None,
        )

    def add_ws_service(
        self,
        routing_name: str,
        *,
        ws_path: str,
        impl: ServiceImpl,
        type_id: FxtrID | None = None,
        discovery_name: str | None = None,
    ) -> ServiceDescriptor:
        """Register a ws service (multiplexed on ``ws_path``).

        Unlike a REST service, a ws service is addressed on the wire by
        its descriptor ``name`` — the routing key the client sends as
        ``service`` — so a routing key is always required. Whether it's
        *discoverable* is a separate, optional choice; the two names may
        differ and are never defaulted to each other.

        Args:
            routing_name: The wire routing key (the descriptor ``name``),
                unique within ``ws_path``. The client sends it as
                ``service`` to address invokes at this impl.
            ws_path: The ws endpoint path; all ws services sharing it
                multiplex one connection.
            impl: The service impl (callable or method-map).
            type_id: The service's type entity id, surfaced as the
                descriptor's ``service_type_id`` annotation.
            discovery_name: The name to list it under in ``/discovery``
                (unique across all services). ``None`` (the default)
                leaves it out of discovery — the route is still mounted;
                an impl reaches it by handing out the returned descriptor.

        Returns:
            The service's :class:`ServiceDescriptor` (carrying the
            routing ``name``) — useful for a non-discoverable service,
            which an impl can hand out by returning it from an invoke.

        Raises:
            ValueError: If ``discovery_name`` collides with another
                service, or ``routing_name`` collides on ``ws_path``.
        """
        if discovery_name is not None:
            self._claim_service_name(discovery_name)
        bucket = self._ws_routing.setdefault(ws_path, set())
        if routing_name in bucket:
            raise ValueError(f"duplicate ws routing name {routing_name!r} on {ws_path!r}")
        bucket.add(routing_name)
        self._ws.append(_WsEntry(routing_name, discovery_name, ws_path, impl, type_id))
        return ServiceDescriptor(
            kind="ws",
            url=_to_ws_url(self._url(ws_path)),
            name=routing_name,
            service_type_id=type_id.id_string() if type_id else None,
        )

    def _claim_service_name(self, name: str) -> None:
        """Reserve a discovery name, rejecting cross-service duplicates.

        Args:
            name: The candidate discovery name.

        Raises:
            ValueError: If ``name`` is already claimed by a service.
        """
        if name in self._service_names:
            raise ValueError(f"duplicate discovery name: {name!r}")
        self._service_names.add(name)

    # -- output -------------------------------------------------------

    def _url(self, path: str) -> str:
        """Resolve ``path`` to absolute against ``base_url`` (or leave
        it relative when no ``base_url`` was given)."""
        return urljoin(self._base_url, path) if self._base_url else path

    def routes(self) -> list[BaseRoute]:
        """Build the Starlette routes for all registered services.

        Returns:
            REST ``Route``s (one per service) plus one ``WebSocketRoute``
            per distinct ws path.
        """
        routes: list[BaseRoute] = [
            rest_service_route(
                e.path, e.impl, scope=self._scope, method=e.method, provenanced=self._provenanced
            )
            for e in self._rest
        ]
        # Group ws impls by path (one multiplexed endpoint per ws_path),
        # keyed by their wire routing name.
        ws_by_path: dict[str, dict[str, ServiceImpl]] = {}
        for e in self._ws:
            ws_by_path.setdefault(e.ws_path, {})[e.routing_name] = e.impl
        routes.extend(
            WebSocketRoute(
                ws_path,
                ws_services_endpoint(impls, scope=self._scope, provenanced=self._provenanced),
            )
            for ws_path, impls in ws_by_path.items()
        )
        return routes

    def discovery(self) -> dict[str, Any]:
        """Build the discovery payload matching the mounted routes.

        Returns:
            ``{"entities": {name: id_value}, "services": {name:
            descriptor}}`` where each service descriptor carries its
            ``service_type_id`` annotation. An entity ``id_value`` is a bare
            id_string, or — for a layered entity when the registry is
            ``provenanced`` — the ``[data_id, prov_id]`` form (matching
            ``/pins``).
        """
        services: dict[str, Any] = {}
        for e in self._rest:
            if e.discovery_name is None:
                continue  # non-discoverable: mounted, but not listed
            services[e.discovery_name] = ServiceDescriptor(
                kind="http",
                url=self._url(e.path),
                method=e.method,
                service_type_id=e.type_id.id_string() if e.type_id else None,
            ).to_wire()
        for e in self._ws:
            if e.discovery_name is None:
                continue  # non-discoverable: mounted, but not listed
            services[e.discovery_name] = ServiceDescriptor(
                kind="ws",
                url=_to_ws_url(self._url(e.ws_path)),
                name=e.routing_name,
                service_type_id=e.type_id.id_string() if e.type_id else None,
            ).to_wire()
        entities = {
            name: encode_pin_value(fid, provenanced=self._provenanced)
            for name, fid in self._entities.items()
        }
        return {"entities": entities, "services": services}

    async def discovery_handler(self, request: Request) -> JSONResponse:
        """Starlette handler that serves :meth:`discovery` as JSON.

        Args:
            request: The incoming request (unused).

        Returns:
            A ``JSONResponse`` of the discovery payload.
        """
        return JSONResponse(self.discovery())
