"""The repo entity store: standalone by-id entity storage for a repo.

The **repo entity store** is repo-associated, by-``FxtrID`` entity storage —
one per repo (the ``.fxtr/`` dir, sibling of the VCS root). It is the artifact
formerly known as the tsv namespace's *index namespace*: an append-y accumulator of
every entity ever pinned in the repo, keyed by its own id. **No pins, no keys,
no TSVs.**

This module exposes it as a first-class, standalone concept so consumers that
only need by-id reads/writes (notably ``fxtr_nix_runner``, the flake manager,
which rehosts entity bytes for build-time FOD fetches and resolves
remote-pointer entities) don't have to stand up a full
:class:`~fxtr2.data.tsv_namespace.tsv_namespace.TsvNamespaceBackend` (worktree
sync, dirty marks, TSV discovery). ``TsvNamespaceBackend`` becomes one *view*
over a repo entity store rather than its owner.

On-disk-compatible with the index namespace: same
``.fxtr/repo_backing_storage.sqlite`` backing store at namespace id
``index:{uuid}``. No data migration.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fxtr2.data.abstract_arena import DeepFetchable, FlatEntitySource, ReadableArena
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.repo_storage_config import load_repo_store_config
from fxtr2.data.sqlite_store import SQLiteFxtrSession, SQLiteNamespaceBackend
from fxtr2.leases.lease_types import FreshLifetime, Grant, Lease, LifetimeBrand, LifetimeToken


class RepoEntityStore[B: FreshLifetime[Any]](DeepFetchable[B]):
    """By-id entity storage for a repo (the artifact formerly known as the index namespace).

    Backed by a :class:`SQLiteNamespaceBackend` over the ``index:{uuid}``
    namespace in ``.fxtr/repo_backing_storage.sqlite``. By-id storage is
    implemented as pinning keyed by ``entity_id.id_string()`` (each entity
    individually rooted so it survives across git history); by-id reads use the
    namespace's ``try_lookup`` (resolving any entity in a tracked closure, not
    only individually-pinned roots).

    Two construction paths:

    - :meth:`open` — standalone async CM that opens its *own*
      :class:`SQLiteFxtrSession` and owns its lifetime. Used by the runner.
    - :meth:`from_session` — wraps an already-open session + store namespace
      (does NOT own the session lifetime). Used by ``TsvNamespaceBackend``,
      which keeps owning its single session and exposes the store as
      ``tsv_ns.store``.
    """

    def __init__(
        self,
        session: SQLiteFxtrSession[B],
        store_ns: SQLiteNamespaceBackend[B],
    ) -> None:
        """Internal constructor. Use :meth:`open` or :meth:`from_session`."""
        self._session = session
        self._store_ns = store_ns
        # The raw session lease is private — callers go through get_lease()
        # (which hands out clones) so nobody mucks with the underlying lease.
        self._root_lease: Lease[B] = session.root_lease

    @property
    def token(self) -> LifetimeToken[B]:
        return self._session.token

    @property
    def grant(self) -> Grant[B]:
        """The session's canonical grant (identity — pins nothing).

        Use where a ``Grant[B]`` for this session's brand is needed *without*
        holding a lease — e.g. branding a ``LayeredFileArena`` over this store
        via the ``layered_file_store`` pin-arena seam. A ``Grant`` holds only
        weak references, so retaining it does not keep the session alive (unlike
        :meth:`get_lease`); all leases from :meth:`get_lease` share this grant.
        """
        return self._root_lease.grant

    def get_lease(self) -> Lease[B]:
        """Return a fresh clone of the underlying session's root lease.

        Use this for arenas returned by this store and wherever a ``Lease[B]``
        for this session's brand is needed (e.g.
        ``LayeredFileStoreReader(remote=(store, store.get_lease()))`` or
        ``register_deep_fetch_source(store, IndependentLocalBridge(lease=store.get_lease()))``).
        """
        return self._root_lease.clone()

    @classmethod
    @contextlib.asynccontextmanager
    async def open(
        cls,
        repo_path: Path,
        *,
        create: bool = False,
        read_only: bool = False,
    ) -> AsyncIterator[RepoEntityStore[Any]]:
        """Open the repo entity store for ``repo_path`` (standalone, owns its session).

        Discovers ``.fxtr/`` (sibling of the VCS root), opens its own
        :class:`SQLiteFxtrSession`, and opens the ``index:{uuid}`` store
        namespace.

        Args:
            repo_path: Any path inside the repository.
            create: If True, initialize ``.fxtr/`` + ``fxtr_store_repo_config.toml``
                (minting a fresh store namespace id) if absent.
            read_only: If True, open the session read-only (no writes). A
                read-only store must already exist.

        Raises:
            ValueError: If both ``create`` and ``read_only`` are set — read-only
                sessions skip schema init, so the store must already exist;
                callers wanting create-then-read do a writable create pass first
                and then reopen ``read_only=True``.
            FxtrConfigError: If discovery fails or the store config is missing
                (when ``create`` is False).
        """
        if create and read_only:
            raise ValueError(
                "RepoEntityStore.open(create=True, read_only=True) is not allowed: "
                "read-only sessions skip schema init, so the store must already "
                "exist. Do a writable create pass first, then reopen read_only=True."
            )

        config = await load_repo_store_config(repo_path, create=create)
        async with SQLiteFxtrSession.open(
            str(config.sqlite_store_path), read_only=read_only
        ) as session:
            store_ns = await session.create_or_retrieve_user_namespace(config.store_namespace_id)
            yield cls(session, store_ns)

    @classmethod
    def from_session(
        cls,
        session: SQLiteFxtrSession[B],
        store_ns: SQLiteNamespaceBackend[B],
    ) -> RepoEntityStore[B]:
        """Wrap an already-open session + store namespace (does NOT own the session).

        Used by ``TsvNamespaceBackend`` to expose its existing index namespace
        as a ``RepoEntityStore`` without opening a second session.
        """
        return cls(session, store_ns)

    # =========================================================================
    # By-id read/write (DeepFetchable)
    # =========================================================================

    async def try_lookup(self, node_id: FxtrID) -> ReadableArena[B] | None:
        """Look up an entity by id (DeepFetchable). Resolves any entity in a
        tracked closure, not only individually-pinned roots. Returns an arena
        tracking it, or None if absent."""
        return await self._store_ns.try_lookup(node_id)

    async def read(self, node_id: FxtrID) -> ReadableArena[B] | None:
        """Friendly alias for :meth:`try_lookup` (by-id read)."""
        return await self._store_ns.try_lookup(node_id)

    async def write_from_arena[
        B_Src: LifetimeBrand
    ](self, entity_id: FxtrID, source: FlatEntitySource[B_Src], src_lease: Lease[B_Src],) -> None:
        """Write an entity (and its closure) into the store by id, no pin/key/TSV.

        Idempotent (overwrites the by-id entry). Mirrors the current index
        namespace's "copy to index first" write, minus any worktree/TSV
        involvement.

        Args:
            entity_id: The entity to store.
            source: The source containing the entity.
            src_lease: Lease for ``source``'s token, consumed by the call.
        """
        await self._store_ns.pin_entity_from_arena(
            entity_id.id_string(),
            entity_id,
            source,
            src_lease,
            overwrite=True,
        )
