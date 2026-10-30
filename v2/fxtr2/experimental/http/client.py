"""HTTP client for consuming a fxtr HTTP API.

Provides:
- HttpReadableArena: ReadableArena backed by HTTP requests
- HttpDeepFetchable: DeepFetchable for deep-fetch integration
- HttpFxtrReadNamespace: Read-only namespace backed by HTTP
"""

from __future__ import annotations

import functools
import inspect
import json
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Collection, override

import cbor2
import httpx

from fxtr2.data import arena_util, entity_ref_scope
from fxtr2.data.abstract_arena import (
    DeepFetchable,
    DepKind,
    FxtrEntityFlatData,
    ReadableArena,
    validate_lease_grant,
)
from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
    fxtr_cbor_loads,
)
from fxtr2.experimental.read_namespace import FxtrReadNamespace
from fxtr2.leases.lease_bridges import IndependentLocalBridge
from fxtr2.leases.lease_types import Lease, LifetimeToken
from fxtr2.leases.local_lease import UnconditionalLocalLifetime


def _decode_pin_value(value: Any) -> FxtrID:
    """Decode a pin / discovery id value into a bare or layered ``FxtrID``.

    Bare ``"<data>"`` (string) → a bare id; layered ``["<data>", "<prov>"]``
    (a 2-element array) → ``FxtrID(data, _provenance_parent=prov)``. Both halves
    are bare local id_strings (the ``@``-form is rejected). Strict shape — any
    other value is an error, never silently coerced.
    """
    if isinstance(value, str):
        return FxtrID.from_id_string(value, allow_provenance=False)
    if isinstance(value, list) and len(value) == 2 and all(isinstance(x, str) for x in value):
        data = FxtrID.from_id_string(value[0], allow_provenance=False)
        prov = FxtrID.from_id_string(value[1], allow_provenance=False)
        return FxtrID(raw_data_id=bytes(data), provenance_parent=prov)
    raise ValueError(f"malformed pin value (expected a str or [data, prov]): {value!r}")


def _with_validated_arena_lease(fn):
    """Validate lease against the arena lifetime (first arg after ``self`` is ``lease``)."""

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def _async_wrapper(self: HttpReadableArena, lease: Lease[Any], *args, **kwargs):
            validate_lease_grant(self._lifetime.root_lease.grant, lease)
            with lease.take() as held:
                return await fn(self, held, *args, **kwargs)

        return _async_wrapper

    @functools.wraps(fn)
    def _sync_wrapper(self: HttpReadableArena, lease: Lease[Any], *args, **kwargs):
        validate_lease_grant(self._lifetime.root_lease.grant, lease)
        with lease.take() as held:
            return fn(self, held, *args, **kwargs)

    return _sync_wrapper


class HttpReadableArena(ReadableArena[Any]):
    """A ReadableArena backed by HTTP requests to a fxtr server.

    Follows the same precomp pattern as SQLiteNamespaceArena, but keyed
    directly by FxtrID instead of internal IDs.
    """

    def __init__(self, client: httpx.AsyncClient, lifetime: UnconditionalLocalLifetime[Any]):
        self._client = client
        self._lifetime = lifetime
        self._tracked: set[FxtrID] = set()
        self._precomp_unwraps_to: dict[FxtrID, FxtrID] = {}
        self._precomp_ctx_node: dict[FxtrID, FxtrID] = {}
        self._precomp_step_back_p_ctx: dict[FxtrID, FxtrID] = {}

    @property
    @override
    def token(self) -> LifetimeToken[Any]:
        return self._lifetime.token

    def _ingest_precomp(self, precomp: dict[str, dict[str, str]]) -> None:
        """Populate precomp dicts from a server response's precomp map."""
        for id_str, entry in precomp.items():
            node_id = FxtrID.from_id_string(id_str)
            self._tracked.add(node_id)

            if "unwraps_to" in entry:
                target = FxtrID.from_id_string(entry["unwraps_to"])
                self._precomp_unwraps_to[node_id] = target
                self._tracked.add(target)

            if "ctx_node" in entry:
                target = FxtrID.from_id_string(entry["ctx_node"])
                self._precomp_ctx_node[node_id] = target
                self._tracked.add(target)

            if "backstep" in entry:
                target = FxtrID.from_id_string(entry["backstep"])
                self._precomp_step_back_p_ctx[node_id] = target
                self._tracked.add(target)

    async def _fetch_data(self, node_id: FxtrID) -> dict[str, Any]:
        """Fetch from /data/{id_string} and return the decoded CBOR dict."""
        resp = await self._client.get(f"/data/{node_id.id_string()}")
        if resp.status_code == 404:
            raise ValueError(f"Entity {node_id} not found on server")
        if resp.status_code == 503:
            raise RuntimeError("Server scope is closed")
        resp.raise_for_status()
        return cbor2.loads(resp.content)

    @override
    @_with_validated_arena_lease
    def is_tracked(self, lease: Lease[Any], node_id: FxtrID) -> bool:
        return node_id in self._tracked

    @override
    @_with_validated_arena_lease
    async def load(self, lease: Lease[Any], node_id: FxtrID) -> FxtrCbor:
        if node_id not in self._tracked:
            raise ValueError(f"Node {node_id} is not tracked in this arena")

        result = await self._fetch_data(node_id)

        # Ingest precomp
        if "precomp" in result:
            self._ingest_precomp(result["precomp"])

        # Decode entity bytes
        entity_bytes = result["bytes"]
        decoded = fxtr_cbor_loads(entity_bytes)

        # Walk CBOR to discover and track child IDs
        def _track_child(
            raw_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
        ) -> None:
            self._tracked.add(raw_id.inner_id)

        arena_util.visit_ids(decoded, _track_child)

        return decoded

    @override
    @_with_validated_arena_lease
    async def load_flat(self, lease: Lease[Any], node_id: FxtrID) -> FxtrEntityFlatData:
        if node_id not in self._tracked:
            raise ValueError(f"Node {node_id} is not tracked in this arena")

        result = await self._fetch_data(node_id)

        # Ingest precomp
        if "precomp" in result:
            self._ingest_precomp(result["precomp"])

        entity_bytes = result["bytes"]

        # Walk CBOR to extract deps
        decoded = fxtr_cbor_loads(entity_bytes)
        deps: list[tuple[FxtrID, DepKind]] = []

        def _collect_dep(
            raw_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
        ) -> None:
            self._tracked.add(raw_id.inner_id)
            match raw_id:
                case RawStrongFxtrID():
                    deps.append((raw_id.inner_id, DepKind.STRONG_REF))
                case RawWeakFxtrID() | RawFxtrEntityMarkerID():
                    deps.append((raw_id.inner_id, DepKind.WEAK_REF))
                case _:
                    raise ValueError(f"Unknown ID type: {type(raw_id)}")

        arena_util.visit_ids(decoded, _collect_dep)

        return FxtrEntityFlatData(
            entity_id=node_id,
            entity_bytes=entity_bytes,
            deps=deps,
        )

    @override
    @_with_validated_arena_lease
    def unwrap_provenance(self, lease: Lease[Any], node_id: FxtrID) -> FxtrID | None:
        if node_id not in self._tracked:
            raise ValueError(f"Node {node_id} is not tracked in this arena")
        result = self._precomp_unwraps_to.get(node_id)
        if result is not None:
            self._tracked.add(result)
        return result

    @override
    @_with_validated_arena_lease
    def c_node_to_context_data(self, lease: Lease[Any], c_node_id: FxtrID) -> FxtrID:
        if c_node_id not in self._tracked:
            raise ValueError(f"Node {c_node_id} is not tracked in this arena")
        result = self._precomp_ctx_node.get(c_node_id)
        assert result is not None, f"No ctx_node precomp for {c_node_id}"
        self._tracked.add(result)
        return result

    @override
    @_with_validated_arena_lease
    def p_node_to_pathroot(self, lease: Lease[Any], p_node_id: FxtrID) -> FxtrID:
        if p_node_id not in self._tracked:
            raise ValueError(f"Node {p_node_id} is not tracked in this arena")
        result = self._precomp_ctx_node.get(p_node_id)
        assert result is not None, f"No ctx_node precomp for {p_node_id}"
        self._tracked.add(result)
        return result

    @override
    @_with_validated_arena_lease
    def step_back_p_ctx(self, lease: Lease[Any], p_node_id: FxtrID) -> FxtrID | None:
        if p_node_id not in self._tracked:
            raise ValueError(f"Node {p_node_id} is not tracked in this arena")
        result = self._precomp_step_back_p_ctx.get(p_node_id)
        if result is not None:
            self._tracked.add(result)
        return result

    @override
    @_with_validated_arena_lease
    def abandon(self, lease: Lease[Any], node_id: FxtrID) -> None:
        if node_id not in self._tracked:
            raise ValueError(f"Node {node_id} is not tracked in this arena")
        self._tracked.discard(node_id)
        self._precomp_unwraps_to.pop(node_id, None)
        self._precomp_ctx_node.pop(node_id, None)
        self._precomp_step_back_p_ctx.pop(node_id, None)

    @override
    @_with_validated_arena_lease
    def copy_to_new_arena(
        self, lease: Lease[Any], node_ids: Collection[FxtrID]
    ) -> HttpReadableArena:
        for node_id in node_ids:
            if node_id not in self._tracked:
                raise ValueError(f"Node {node_id} is not tracked in this arena")

        new_arena = HttpReadableArena(self._client, self._lifetime)

        to_copy: set[FxtrID] = set()
        frontier: deque[FxtrID] = deque(node_ids)
        while frontier:
            nid = frontier.popleft()
            if nid in to_copy:
                continue
            to_copy.add(nid)
            for precomp_dict in (
                self._precomp_unwraps_to,
                self._precomp_ctx_node,
                self._precomp_step_back_p_ctx,
            ):
                target = precomp_dict.get(nid)
                if target is not None and target not in to_copy:
                    frontier.append(target)

        for nid in to_copy:
            new_arena._tracked.add(nid)
            if nid in self._precomp_unwraps_to:
                new_arena._precomp_unwraps_to[nid] = self._precomp_unwraps_to[nid]
            if nid in self._precomp_ctx_node:
                new_arena._precomp_ctx_node[nid] = self._precomp_ctx_node[nid]
            if nid in self._precomp_step_back_p_ctx:
                new_arena._precomp_step_back_p_ctx[nid] = self._precomp_step_back_p_ctx[nid]

        return new_arena

    @override
    def close(self) -> None:
        self._tracked.clear()
        self._precomp_unwraps_to.clear()
        self._precomp_ctx_node.clear()
        self._precomp_step_back_p_ctx.clear()


class HttpDeepFetchable(DeepFetchable[Any]):
    """DeepFetchable backed by HTTP: ``try_lookup`` does GET /data/ with precomp."""

    def __init__(self, client: httpx.AsyncClient, lifetime: UnconditionalLocalLifetime[Any]):
        self._client = client
        self._lifetime = lifetime

    @override
    async def try_lookup(self, node_id: FxtrID) -> HttpReadableArena | None:
        resp = await self._client.get(f"/data/{node_id.id_string()}")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            return None

        arena = HttpReadableArena(self._client, self._lifetime)
        arena._tracked.add(node_id)

        result = cbor2.loads(resp.content)
        if "precomp" in result:
            arena._ingest_precomp(result["precomp"])

        return arena


class HttpFxtrReadNamespace(FxtrReadNamespace):
    """Read-only fxtr namespace backed by HTTP.

    Connects to a fxtr HTTP server and provides the standard read namespace
    interface (retrieve, try_retrieve, list_pins).
    """

    def __init__(self, client: httpx.AsyncClient, *, isolate_scope: bool = False):
        """Initialize with an httpx AsyncClient pointing at the server base URL.

        Args:
            client: An httpx AsyncClient with base_url set to the server.
            isolate_scope: If True, the scope will be isolated from the active scope.
        """
        self._client = client
        self._isolate_scope = isolate_scope
        self._scope: entity_ref_scope.FxtrScope | None = None
        self._backend_lifetime: UnconditionalLocalLifetime[Any] | None = None
        self._deep_fetch_source: HttpDeepFetchable | None = None

    async def __aenter__(self) -> HttpFxtrReadNamespace:
        self._backend_lifetime = UnconditionalLocalLifetime("http_read_ns")
        self._scope = entity_ref_scope.FxtrScope(name="http_read_ns", isolated=self._isolate_scope)
        await self._scope.__aenter__()
        assert self._backend_lifetime is not None
        deep = HttpDeepFetchable(self._client, self._backend_lifetime)
        self._deep_fetch_source = deep
        self._scope.register_deep_fetch_source(
            deep,
            IndependentLocalBridge(lease=self._backend_lifetime.root_lease.clone()),
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._scope is not None:
            await self._scope.__aexit__(*args)
            self._scope = None
        if self._backend_lifetime is not None:
            self._backend_lifetime.root_lease.discard()
            self._backend_lifetime = None
        self._deep_fetch_source = None

    def as_deep_fetch_source(self) -> tuple[DeepFetchable[Any], Lease[Any]]:
        """Expose this namespace as a ``(DeepFetchable, Lease)`` pair —
        the shape the layered-file-store writer + reader consume as
        their ``remote=`` argument.

        Caller takes ownership of the returned lease (clone of the
        backend root). Per the standard lease idiom, the caller must
        ``.clone()`` again if they want to retain a copy themselves.
        """
        assert self._deep_fetch_source is not None, "Namespace not entered"
        assert self._backend_lifetime is not None
        return self._deep_fetch_source, self._backend_lifetime.root_lease.clone()

    @property
    @override
    def scope(self) -> entity_ref_scope.FxtrScope:
        assert self._scope is not None, "Namespace not entered"
        return self._scope

    @override
    async def _retrieve_impl[
        T: entity_ref_scope.Entity
    ](
        self,
        key: str,
        type: type[T] | None,
        into_scope: entity_ref_scope.FxtrScope | None,
        copy_into_scope: bool,
        allow_not_found: bool,
    ) -> (FxtrID | entity_ref_scope.EntityRef[T] | None):
        resp = await self._client.get(f"/pin/{key}")
        if resp.status_code == 404:
            if allow_not_found:
                return None
            raise KeyError(f"Pin '{key}' not found")
        resp.raise_for_status()

        result = cbor2.loads(resp.content)
        fxtr_id = _decode_pin_value(result["id"])

        # A layered pin reads back as (data, prov): seed/attach on the bare prov
        # node (its precomp `unwraps_to` pulls the data node in), then `embed`
        # reconstructs the layered id from what the prov node actually unwraps to
        # — resolve-or-fail, never a layered id whose provenance can't resolve. A
        # bare pin seeds on itself and is delivered unchanged. (Mirrors the
        # layered-file-store reader.)
        prov = fxtr_id._provenance_parent
        seed = prov if prov is not None else fxtr_id

        assert self._backend_lifetime is not None
        arena = HttpReadableArena(self._client, self._backend_lifetime)
        arena._tracked.add(seed)
        if "precomp" in result:
            arena._ingest_precomp(result["precomp"])

        scope = self.scope
        scope._main_arena.lazily_attach_from_arena(
            scope._current_auth_lease().clone(),
            seed,
            arena,
            IndependentLocalBridge(lease=self._backend_lifetime.root_lease.clone()),
        )
        arena.close()

        delivered = scope.embed_as_provenance_parent(seed) if prov is not None else fxtr_id

        # Resolve into_scope
        if into_scope is not None:
            if copy_into_scope:
                await into_scope.copy_from_scope(delivered, source_scope=scope)
            else:
                into_scope.lift(delivered)

        if type is None:
            return delivered
        else:
            return entity_ref_scope.EntityRef(delivered, type)

    @override
    async def list_pins(self, prefix: str = "") -> dict[str, FxtrID]:
        params = {}
        if prefix:
            params["prefix"] = prefix
        resp = await self._client.get("/pins", params=params)
        resp.raise_for_status()
        data = json.loads(resp.content)
        return {name: _decode_pin_value(value) for name, value in data.items()}


@asynccontextmanager
async def http_deep_fetchable(
    base_url: str, *, token: str | None = None, timeout: float = 300.0
) -> AsyncIterator[tuple[HttpDeepFetchable, Lease[Any]]]:
    """Open an :class:`HttpDeepFetchable` against a fxtr server by URL.

    The ``(DeepFetchable, Lease)`` shape that ``LayeredFileStoreReader`` /
    ``LayeredFileStoreIsolatedWriter`` take as their ``remote=`` argument, built
    from a base URL (+ optional bearer token). Factors out the client +
    lifetime construction otherwise inlined in :class:`HttpFxtrReadNamespace`;
    the primary consumer is the build-time FOD writer, which reads
    ``FXTR_REMOTE_BASE_URL`` / ``FXTR_REMOTE_BEARER_TOKEN`` and fetches entity
    bytes through the runner's rehost.

    Owns the httpx client + the backing lifetime for the block; yields a fresh
    lease clone the caller takes ownership of (per the standard idiom, the
    caller ``.clone()``s again to retain a copy). The generous default timeout
    suits in-build fetches of large closures.
    """
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    lifetime = UnconditionalLocalLifetime("http_deep_fetchable")
    try:
        async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
            yield HttpDeepFetchable(client, lifetime), lifetime.root_lease.clone()
    finally:
        lifetime.root_lease.discard()
