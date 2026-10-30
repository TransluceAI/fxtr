"""The flake manager (manager tier) — the fxtr2-aware front door to a flake repo.

A :class:`FlakeManager` wraps a resolved transport backend + a repo's
:class:`RepoEntityStore`, and is the single object the publishing serving script
(and, later, the MCP) talks to. It owns:

- **repo-store discovery** — entering the manager (``async with
  FlakeManager(...)``) opens (creating if absent) the repo entity store and keeps
  it open for the manager's whole lifetime.
- **the build-time rehost** — :meth:`build_span` stands the rehost up
  *unconditionally* for a build span, computes the build-visible URL
  via the transport ``Handle.expose_host_endpoint`` hook, and threads the
  ``FXTR_REMOTE_*`` env into every ``run_nix`` so FODs can fetch from the store.
- **store exposure** — :attr:`repo_store` hands consumers a
  ``(DeepFetchable, Lease)`` pair to mount on **their own** scope (the manager
  never mutates a caller's scope).

This module imports fxtr2 — it is part of the manager tier, kept separate from
the fxtr2-free transport modules; import it explicitly.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator, AsyncIterator, Self, Sequence

import anyio
from fxtr2.data.abstract_arena import DeepFetchable
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.layered_file_store import ParentPathRemap, open_seed_arena, resolve_pin
from fxtr2.data.repo_storage import RepoEntityStore
from fxtr2.leases.lease_bridges import IndependentLocalBridge
from fxtr2.leases.lease_types import Lease
from fxtr_nix_runner._backend import Backend, Handle, NixResult, impure_env_nix_flags
from fxtr_nix_runner._config import NixRuntimeConfig
from fxtr_nix_runner._resolve import resolve_backend
from fxtr_nix_runner.entities import EntitiesReadAdapter
from fxtr_nix_runner.namespaces import (
    NamespaceReadAdapter,
    build_namespace_manifest,
    eval_namespace_names,
)
from fxtr_nix_runner.rehost import RehostInfo, rehost_context
from fxtr_nix_runner.renderers import eval_preset_names


def _rehost_bind_host(backend: Backend) -> str:
    """Which interface the rehost must bind for this backend's builds to reach it.

    Podman builds dial ``host.containers.internal`` (the gvproxy gateway), so a
    loopback-bound server is unreachable — bind ``0.0.0.0`` (the bearer token is
    then the only access control). Host builds reach loopback directly.
    """
    from fxtr_nix_runner._podman import PodmanBackend

    return "0.0.0.0" if isinstance(backend, PodmanBackend) else "127.0.0.1"


@dataclass
class BuildSpan:
    """An active build span: a borrowed builder handle + a live rehost wired in.

    Attributes:
        handle: The transport :class:`~fxtr_nix_runner.Handle` for the repo
            (use it for ``materialize`` / ``flake_ref_root`` / ``lock``).
        build_env: The ``{FXTR_REMOTE_BASE_URL, FXTR_REMOTE_BEARER_TOKEN}`` env,
            with the **build-visible** URL for this backend.
        rehost: The live :class:`~fxtr_nix_runner.rehost.RehostInfo`.
    """

    handle: Handle
    build_env: dict[str, str]
    rehost: RehostInfo

    async def run_nix(self, argv: Sequence[str]) -> NixResult:
        """Run a nix invocation with the rehost env wired in (unconditional).

        Owns the build-time env-delivery policy (the transport `run_nix` is pure):
        prepends :func:`impure_env_nix_flags` as **global** flags before the argv
        — ``nix <flags> <argv>`` — so FOD builds see the rehost's
        ``FXTR_REMOTE_*`` as ``impureEnvVars``. Global flags precede the
        subcommand, so this needs no knowledge of the argv's shape. It's a no-op
        for eval, so prepending on every call is harmless and keeps the
        "always rehost" rule simple.
        """
        return await self.handle.run_nix([*impure_env_nix_flags(self.build_env), *argv])


class FlakeManager(anyio.AsyncContextManagerMixin):
    """The fxtr2-aware manager of a flake repo (see module docstring).

    Construction is inert — it only records config. Enter it as an async context
    manager to open the repo store + manager scope (mirroring
    ``SQLiteFxtrNamespace`` and the other ``fxtr_context_api`` namespaces)::

        async with FlakeManager(repo, rev=rev) as mgr:
            async with mgr.build_span() as span:
                ref = await mgr.entities(span).retrieve("report")
    """

    def __init__(
        self,
        repo: Path | str,
        *,
        rev: str | None = None,
        backend: Backend | None = None,
        config: NixRuntimeConfig | None = None,
        isolate_scope: bool = False,
    ) -> None:
        """Record config for the manager; nothing is opened until entered.

        Args:
            repo: Any path inside the flake repo.
            rev: A committed rev to read the flake at, or ``None`` for the live
                workdir. Stored for the read adapters; blobs always come from the
                current repo store.
            backend: A pre-resolved backend (mostly for tests); resolved from
                config when omitted.
            config: Optional ``NixRuntimeConfig`` for backend resolution.
            isolate_scope: If True, the manager scope is isolated from the active
                scope (no parent inheritance, doesn't become active) — matching
                the ``fxtr_context_api`` namespace pattern. Default False: the
                scope inherits the active scope and becomes active for the block,
                so retrieved entities are reachable from the active scope.
        """
        self._repo = Path(repo)
        self._rev = rev
        self._provided_backend = backend
        self._config = config
        self._isolate_scope = isolate_scope
        # Bound on enter, cleared on exit (see __asynccontextmanager__).
        self._backend: Backend | None = None
        self._store: RepoEntityStore[Any] | None = None
        self._scope: FxtrScope | None = None

    @contextlib.asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
        """Resolve the backend, open the repo store + the long-lived manager scope.

        The store is opened ``create=True`` (mints ``.fxtr/`` for a fresh clone)
        and stays open for the manager's lifetime — the rehost is a build-only
        sub-span underneath it, and serving holds the store as a deep-fetch
        source for the whole serve, so the session must outlive both.

        The manager :class:`FxtrScope` is entered *after* the store (so it closes
        *before* it — releasing the store leases held by mounted entities before
        the session ends). Entities read through the adapters land in this scope
        and stay lazily loadable for the manager's lifetime; see :attr:`scope`.
        """
        backend = (
            self._provided_backend
            if self._provided_backend is not None
            else resolve_backend(self._config)
        )
        async with (
            RepoEntityStore.open(self._repo, create=True) as store,
            FxtrScope(name="flake-manager", isolated=self._isolate_scope) as scope,
        ):
            self._backend = backend
            self._store = store
            self._scope = scope
            try:
                yield self
            finally:
                self._backend = None
                self._store = None
                self._scope = None

    def _get_store(self) -> RepoEntityStore[Any]:
        if self._store is None:
            raise RuntimeError("FlakeManager must be used as a context manager!")
        return self._store

    def _get_backend(self) -> Backend:
        if self._backend is None:
            raise RuntimeError("FlakeManager must be used as a context manager!")
        return self._backend

    @property
    def scope(self) -> FxtrScope:
        """The manager's long-lived scope — the home for entities read through
        the adapters, lazily loadable for the manager's lifetime (local bytes
        from materialized layers on disk, remote-pointer closures from the repo
        store). Isolated only if constructed with ``isolate_scope=True``;
        otherwise it inherits the active scope and is active for the ``async
        with`` block. A consumer reads its closure by making its own scope a
        child or ``include=[mgr.scope]``, or by passing ``into_scope`` to a
        retrieve for an independent copy.
        """
        if self._scope is None:
            raise RuntimeError("FlakeManager must be used as a context manager!")
        return self._scope

    @property
    def repo_store(self) -> tuple[DeepFetchable[Any], Lease[Any]]:
        """The opened repo store as a ``(DeepFetchable, Lease)`` pair.

        Each access hands out a **fresh lease clone** off the store's session
        (the manager owns the session); the consumer takes ownership and is
        responsible for it (e.g. wrapping it in an ``IndependentLocalBridge``
        that the consuming scope discards at exit). Mount it on your **own**
        scope — the manager never registers sources on a caller's scope.
        """
        store = self._get_store()
        return store, store.get_lease()

    @contextlib.asynccontextmanager
    async def build_span(self) -> AsyncIterator[BuildSpan]:
        """Open a build span: borrow a builder + stand up the rehost, env wired.

        Stands the rehost up unconditionally, binds it per backend,
        resolves the build-visible URL through the transport
        ``expose_host_endpoint`` hook, and yields a :class:`BuildSpan` whose
        ``run_nix`` threads the ``FXTR_REMOTE_*`` env into every build.
        """
        backend = self._get_backend()
        store = self._get_store()
        bind_host = _rehost_bind_host(backend)
        async with backend.builder_for(self._repo) as handle:
            async with rehost_context(store, host=bind_host) as info:
                async with handle.expose_host_endpoint(info.port) as ep:
                    env = info.build_env(ep.url_for_builds)
                    yield BuildSpan(handle=handle, build_env=env, rehost=info)

    def mount_pin(self, layer_dir: Path, pin: str, *, remap: ParentPathRemap | None) -> FxtrID:
        """Mount a built layer's ``pin`` into the manager scope; return its id.

        The entity stays lazily loadable from :attr:`scope` for the manager's
        lifetime: local bytes come from the layer on disk (via the search set),
        remote-pointer closures fall through to the repo store. The per-retrieve
        arena is branded on the store's grant and threads the store lease through
        for remote resolution; we mount it via an ``IndependentLocalBridge`` over
        a store-lease clone (the store outlives the scope) and close the transient
        arena — the mounted node survives because the scope holds the bridge.

        Assumes ``layer_dir`` (and its parents) stay present for the manager's
        lifetime — the layered-store invariant (materialized nix layers persist;
        GC-rooting them is the caller's concern).

        A pin in a **provenanced** layer resolves to a layered ``(data, prov)``
        id (its ``pins.json`` value is a ``[data, prov]`` pair). We open the
        arena on, and attach, the bare **prov node** (``id._provenance_parent``,
        whose ``UNWRAPS_TO`` pulls the data node into ``scope``), then
        reconstruct the layered id via ``embed_as_provenance_parent`` (the
        validated form). A bare pin opens/attaches on itself and is returned
        unchanged.
        """
        store = self._get_store()
        scope = self.scope
        fxtr_id = resolve_pin(layer_dir, pin)
        # The bare node to load: the prov node for a layered pin (its closure
        # root), else the id itself.
        prov = fxtr_id._provenance_parent
        seed = prov if prov is not None else fxtr_id
        arena = open_seed_arena(layer_dir, seed, grant=store.grant, source=store, remap=remap)
        scope._main_arena.lazily_attach_from_arena(
            scope._current_auth_lease().clone(),
            seed,
            arena,
            IndependentLocalBridge(lease=store.get_lease()),
        )
        arena.close()
        if prov is not None:
            return scope.embed_as_provenance_parent(seed)
        return fxtr_id

    def entities(self, span: BuildSpan) -> EntitiesReadAdapter:
        """The ``fxtr.entities.*`` read adapter for an active build span.

        Bind it to the span (whose ``run_nix`` carries the rehost env) so a
        preload-FOD layer can fetch from the store during its build, and to this
        manager (whose scope is the home for retrieved entities, and whose
        :meth:`mount_pin` resolves a built layer's pin against the store).
        """
        return EntitiesReadAdapter(span=span, manager=self, rev=self._rev)

    async def list_namespace_names(self, span: BuildSpan, prefix: str = "") -> list[str]:
        """The published ``fxtr.namespaces.*`` names (cheap eval, no builds).

        The namespace analogue of ``EntitiesReadAdapter.list_entity_names``:
        resolving a name to a manifest needs a build (see :meth:`namespace`), but
        enumerating the declared names is a free eval at the manager's ``rev`` /
        live workdir. Bind it to an active ``span`` (whose ``run_nix`` reaches the
        flake).
        """
        return await eval_namespace_names(span, self._rev, prefix)

    async def list_preset_names(self, span: BuildSpan, prefix: str = "") -> list[str]:
        """The declared renderer-preset names (``fxtr.uiPresets.*``).

        A cheap eval (no builds), mirroring :meth:`list_namespace_names`; building
        a preset into a servable table is :func:`fxtr_nix_runner.renderers.build_preset_table`.
        """
        return await eval_preset_names(span, self._rev, prefix)

    @contextlib.asynccontextmanager
    async def namespace(self, span: BuildSpan, name: str) -> AsyncIterator[NamespaceReadAdapter]:
        """A read view over a published ``fxtr.namespaces.<name>``.

        Builds + reads the export manifest once (needs the span's ``run_nix``),
        then yields a :class:`NamespaceReadAdapter` over the manifest + the repo
        store. Per-``retrieve`` resolution is pure repo-store lookups — no layer,
        no rehost::

            async with mgr.build_span() as span:
                async with mgr.namespace(span, "demo") as ns:
                    ref = await ns.retrieve("greeting", into_scope=scope)
        """
        manifest = await build_namespace_manifest(span, self._rev, name)
        store, lease = self.repo_store
        async with NamespaceReadAdapter(manifest, store, lease) as ns:
            yield ns
