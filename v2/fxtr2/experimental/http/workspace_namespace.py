"""Read-write fxtr namespace backed by a remote hub workspace.

Provides:
- WorkspaceNamespace: FxtrNamespace implementation that delegates to a hub workspace
- WorkspacePinningBackend: PersistentPinningBackend that operates over HTTP
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, AsyncGenerator, Self, override

import anyio
import cbor2
import httpx

from fxtr2.data import entity_ref_scope
from fxtr2.data.abstract_arena import PersistentPinningBackend, ReadableArena
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.fxtr_context_api import FxtrNamespace
from fxtr2.data.remote.remote_client import RemoteClient
from fxtr2.experimental.http.client import HttpDeepFetchable, HttpReadableArena
from fxtr2.leases.lease_bridges import IndependentLocalBridge
from fxtr2.leases.lease_types import Lease, LifetimeBrand, LifetimeToken
from fxtr2.leases.local_lease import UnconditionalLocalLifetime


async def _collect_entity_ids_from_arena(
    source: ReadableArena[Any],
    root_id: FxtrID,
    src_lease: Lease[Any],
) -> list[FxtrID]:
    """Walk deps from an arena, returning entity IDs in topological order.

    Skips builtin nodes (. and ! tags) since they're virtual and don't
    need to be transferred to the server.
    """
    visited: set[FxtrID] = set()
    topo_order: list[FxtrID] = []

    stack: list[tuple[FxtrID, bool]] = [(root_id, False)]
    while stack:
        eid, expanded = stack.pop()
        if eid in visited:
            continue

        tag = eid.get_tag()
        if tag in (".", "!"):
            visited.add(eid)
            continue

        if expanded:
            visited.add(eid)
            topo_order.append(eid)
            continue

        if not source.is_tracked(src_lease.clone(), eid):
            visited.add(eid)
            continue

        stack.append((eid, True))
        flat = await source.load_flat(src_lease.clone(), eid)
        for dep_id, _ in flat.deps:
            if dep_id not in visited:
                stack.append((dep_id, False))

    return topo_order


class WorkspacePinningBackend(PersistentPinningBackend[Any]):
    """PersistentPinningBackend backed by a remote hub workspace."""

    def __init__(
        self,
        data_client: httpx.AsyncClient,
        entity_transfer: RemoteClient,
        lifetime: UnconditionalLocalLifetime[Any],
    ):
        self._client = data_client
        self._entity_transfer = entity_transfer
        self._lifetime = lifetime

    @property
    @override
    def token(self) -> LifetimeToken[Any]:
        return self._lifetime.token

    @override
    async def pin_entity_from_arena(
        self,
        key: str,
        entity_id: FxtrID,
        source: ReadableArena[Any],
        src_lease: Lease[Any],
        *,
        overwrite: bool = False,
    ) -> None:
        """Upload missing entities from the closure, then set the pin on the server."""
        with src_lease.take() as src_held:
            all_ids = await _collect_entity_ids_from_arena(source, entity_id, src_held)

            if all_ids:
                _known, missing = await self._entity_transfer.negotiate(all_ids)
                if missing:
                    to_push = [await source.load_flat(src_held.clone(), eid) for eid in missing]
                    await self._entity_transfer.push_entities(to_push)

            resp = await self._client.put(
                f"/pin/{key}",
                json={"entity_id": entity_id.id_string(), "overwrite": overwrite},
            )
            if resp.status_code == 409:
                raise KeyError(f"Pin '{key}' already exists")
            resp.raise_for_status()

    @override
    async def unpin_entity(self, key: str) -> None:
        """Remove a pin by key. Raises KeyError if the pin doesn't exist."""
        resp = await self._client.delete(f"/pin/{key}")
        if resp.status_code == 404:
            raise KeyError(f"Pin '{key}' not found")
        resp.raise_for_status()

    @override
    async def retrieve_pinned(self, key: str) -> tuple[FxtrID, ReadableArena[Any]]:
        """Fetch a pin's entity ID and an arena with precomputed metadata."""
        resp = await self._client.get(f"/pin/{key}")
        if resp.status_code == 404:
            raise KeyError(f"Pin '{key}' not found")
        resp.raise_for_status()

        result = cbor2.loads(resp.content)
        fxtr_id = FxtrID.from_id_string(result["id"])

        arena = HttpReadableArena(self._client, self._lifetime)
        arena._tracked.add(fxtr_id)
        if "precomp" in result:
            arena._ingest_precomp(result["precomp"])

        return fxtr_id, arena

    @override
    async def list_pins(self, prefix: str = "") -> dict[str, FxtrID]:
        """List pins, optionally filtered by prefix. Returns {name: FxtrID}."""
        params: dict[str, str] = {}
        if prefix:
            params["prefix"] = prefix
        resp = await self._client.get("/pins", params=params)
        resp.raise_for_status()
        data = json.loads(resp.content)
        return {name: FxtrID.from_id_string(id_str) for name, id_str in data.items()}


class WorkspaceNamespace(FxtrNamespace, anyio.AsyncContextManagerMixin):
    """Read-write namespace backed by a remote hub workspace.

    Implements FxtrNamespace with all operations going over HTTP to a
    hub server's workspace API. Entity data is lazily fetched on demand.

    Usage::

        hub_client = HubClient(hub_url, repo="alice/my-data", api_key="...")
        async with WorkspaceNamespace(hub_client) as ws:
            await ws.pin("my-model", model_entity)
            ref = await ws.retrieve("my-model", type=MyModel)
            model = ref.load()
    """

    def __init__(
        self,
        hub_client: Any,
        *,
        isolate_scope: bool = False,
    ):
        """Initialize a workspace namespace.

        Args:
            hub_client: A HubClient instance (or any object with _base_url,
                _api_key, _repo attributes and RemoteClient methods). Must
                have a repo configured.
            isolate_scope: If True, the scope will be isolated from the
                active scope.
        """
        self._hub_client = hub_client
        self._isolate_scope = isolate_scope
        self._scope: entity_ref_scope.FxtrScope | None = None
        self._backend: WorkspacePinningBackend | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._backend_lifetime: UnconditionalLocalLifetime[Any] | None = None

    def _build_workspace_url(self) -> str:
        """Construct the workspace API base URL from the hub client's repo config."""
        base = self._hub_client._base_url.rstrip("/")
        repo = self._hub_client._repo
        if not repo:
            raise ValueError("HubClient must have a repo configured")
        return f"{base}/api/v1/repos/{repo}/workspace"

    def _build_headers(self) -> dict[str, str]:
        """Build auth headers from the hub client's API key."""
        api_key = self._hub_client._api_key
        if api_key:
            return {"Authorization": f"Bearer {api_key}"}
        return {}

    @contextlib.asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
        """Set up the HTTP client, scope, and pinning backend."""
        workspace_url = self._build_workspace_url()
        headers = self._build_headers()
        transport = getattr(self._hub_client, "_transport", None)

        async with httpx.AsyncClient(
            base_url=workspace_url,
            headers=headers,
            timeout=120,
            transport=transport,
        ) as http_client:
            self._http_client = http_client
            async with UnconditionalLocalLifetime("workspace_ns") as backend_lifetime:
                self._backend_lifetime = backend_lifetime
                self._scope = entity_ref_scope.FxtrScope(
                    name="workspace_ns",
                    isolated=self._isolate_scope,
                )
                try:
                    async with self._scope:
                        deep = HttpDeepFetchable(http_client, backend_lifetime)
                        self._scope.register_deep_fetch_source(
                            deep,
                            IndependentLocalBridge(lease=backend_lifetime.root_lease.clone()),
                        )

                        self._backend = WorkspacePinningBackend(
                            http_client,
                            self._hub_client,
                            backend_lifetime,
                        )

                        yield self
                finally:
                    self._scope = None
                    self._backend = None
                self._backend_lifetime = None
            self._http_client = None

    @override
    def _get_scope(self) -> entity_ref_scope.FxtrScope:
        assert self._scope is not None, "WorkspaceNamespace not entered"
        return self._scope

    @override
    def _get_backend(self) -> WorkspacePinningBackend:
        assert self._backend is not None, "WorkspaceNamespace not entered"
        return self._backend

    @override
    def _backend_lease(self) -> Lease[Any]:
        if self._backend_lifetime is None:
            raise RuntimeError("WorkspaceNamespace must be used as a context manager!")
        return self._backend_lifetime.root_lease.clone()
