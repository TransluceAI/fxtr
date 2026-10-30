"""The namespace read view (manager tier) — a real ``FxtrReadNamespace`` over a
published ``fxtr.namespaces.<name>``.

Where the entities adapter (``entities.py``) is only *shaped* like a read
namespace (no id-valued ``list_pins`` — resolving a name needs a per-entity
build), this one is a genuine :class:`FxtrReadNamespace` subclass: a namespace
export's manifest *is* a ``key -> id`` map, so ``list_pins`` is free and the
whole ABC is satisfiable.

It mirrors :class:`fxtr2.experimental.http.client.HttpFxtrReadNamespace` with two
swaps: the pins come from the built manifest (an in-memory dict) instead of
``GET /pins``, and the deep-fetch source is the manager's **repo store** instead
of an HTTP client. No layer is involved — the export is pure metadata; the store
supplies the bytes. ``retrieve`` is side-effect-free w.r.t. the caller's scope
beyond attaching the one requested entity into ``into_scope``; the store is
registered on the view's **own** internal scope.

Two pieces, split so the read view is testable without nix:
- :func:`build_namespace_manifest` — the nix half (eval the attr, build +
  materialize ``.export``, read the manifest); needs an active build span.
- :class:`NamespaceReadAdapter` — the pure read view over a manifest + the repo
  store as a ``(DeepFetchable, Lease)`` pair.

:meth:`FlakeManager.namespace` composes them.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncGenerator, Self

import anyio
from fxtr2.data.abstract_arena import DeepFetchable
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import Entity, EntityRef, FxtrScope
from fxtr2.experimental.read_namespace import FxtrReadNamespace
from fxtr2.leases.lease_bridges import IndependentLocalBridge
from fxtr2.leases.lease_types import Lease
from fxtr_nix_runner.entities import NixEntityError

if TYPE_CHECKING:
    from fxtr_nix_runner.manager import BuildSpan

# The publication manifest contract (source of truth: the ``namespace_export``
# writer in ``nix/fxtr_nix_writer``). Kept in sync by hand — the runner and the
# writer are separate packages, both depending only on fxtr2.
NAMESPACE_EXPORT_FILENAME = "fxtr_namespace_export.json"
NAMESPACE_EXPORT_FORMAT_VERSION = 1
NAMESPACE_EXPORT_TYPE = "fxtr_namespace_export"

# Validate-the-shape --apply for a `fxtr.namespaces.<name>` attr: touching
# `.type` (without forcing `.export`) asserts it is export-shaped, no build.
_NAMESPACE_METADATA_APPLY = "ns: { inherit (ns) type; }"

#: Names the whole `fxtr.namespaces` attrset (`{}` if absent), for
#: `eval_namespace_names` (mirrors the entities adapter's `_ENTITY_NAMES_APPLY`).
_NAMESPACE_NAMES_APPLY = "f: builtins.attrNames (f.namespaces or {})"


def _flake_ref(span: BuildSpan, rev: str | None, attr: str) -> str:
    root = span.handle.flake_ref_root()
    if rev is not None:
        return f"git+file://{root}?rev={rev}#{attr}"
    # Live workdir: bare path lets nix resolve adaptively, non-frozen.
    return f"{root}#{attr}"


async def eval_namespace_names(span: BuildSpan, rev: str | None, prefix: str = "") -> list[str]:
    """The ``fxtr.namespaces`` attr names (cheap eval, no builds).

    The namespace analogue of :meth:`EntitiesReadAdapter.list_entity_names`:
    evaluates ``builtins.attrNames (f.namespaces or {})`` at the manager's
    ``rev`` / live workdir. There is no pre-build namespace adapter (resolving a
    namespace to a manifest needs a build), so this is a free standalone eval —
    :meth:`FlakeManager.list_namespace_names` is the public wrapper.
    """
    result = await span.run_nix(
        ["eval", "--json", _flake_ref(span, rev, "fxtr"), "--apply", _NAMESPACE_NAMES_APPLY]
    )
    if result.returncode != 0:
        raise NixEntityError("nix eval of fxtr.namespaces names failed", stderr=result.stderr)
    names: list[str] = json.loads(result.stdout)
    return [n for n in names if n.startswith(prefix)]


async def build_namespace_manifest(
    span: BuildSpan, rev: str | None, name: str
) -> dict[str, FxtrID]:
    """Eval + build ``fxtr.namespaces.<name>``, returning its ``key -> id`` map.

    Validates the attr is export-shaped (cheap eval), builds + materializes
    ``.export``, and reads ``fxtr_namespace_export.json`` from the output. Runs
    on the build span's ``run_nix`` (so it reads at the manager's rev / live
    workdir like the entities adapter). The export is pure, so the span's rehost
    env is unused here — it's harmless.
    """
    attr = f"fxtr.namespaces.{name}"
    meta_res = await span.run_nix(
        ["eval", "--json", _flake_ref(span, rev, attr), "--apply", _NAMESPACE_METADATA_APPLY]
    )
    if meta_res.returncode != 0:
        raise NixEntityError(f"nix eval of {attr} failed", stderr=meta_res.stderr)
    meta = json.loads(meta_res.stdout)
    if meta.get("type") != NAMESPACE_EXPORT_TYPE:
        raise NixEntityError(
            f"{attr}.type is {meta.get('type')!r}, expected {NAMESPACE_EXPORT_TYPE!r}"
        )

    export_attr = f"{attr}.export"
    build_res = await span.run_nix(
        ["build", "--no-link", "--print-out-paths", _flake_ref(span, rev, export_attr)]
    )
    if build_res.returncode != 0:
        raise NixEntityError(f"nix build of {export_attr} failed", stderr=build_res.stderr)
    stdout = build_res.stdout.strip()
    if not stdout:
        raise NixEntityError(f"nix build of {export_attr} printed no store path")
    store_path = stdout.splitlines()[-1]
    # The export is a single self-contained JSON file (no runtime closure), so a
    # non-closure materialize is enough to read it host-side.
    mat = await span.handle.materialize(store_path, closure=False)

    manifest_path = Path(mat.host_path) / NAMESPACE_EXPORT_FILENAME
    data = json.loads(manifest_path.read_text())
    if data.get("format_version") != NAMESPACE_EXPORT_FORMAT_VERSION:
        raise NixEntityError(
            f"{export_attr}: unsupported manifest format_version "
            f"{data.get('format_version')!r} (expected {NAMESPACE_EXPORT_FORMAT_VERSION})"
        )
    return {key: FxtrID.from_id_string(id_str) for key, id_str in data["pins"].items()}


class NamespaceReadAdapter(FxtrReadNamespace, anyio.AsyncContextManagerMixin):
    """A read view over one namespace export's manifest, data via the repo store.

    Pure (no nix): given the already-built ``manifest`` and the repo store as a
    ``(DeepFetchable, Lease)`` pair, entering it opens an internal scope with the
    store registered as a deep-fetch source. ``retrieve`` / ``try_retrieve`` /
    ``list_pins`` then serve off the manifest + store. The owned ``lease`` is
    cloned per use and discarded on exit.
    """

    def __init__(
        self,
        manifest: dict[str, FxtrID],
        store: DeepFetchable[Any],
        lease: Lease[Any],
    ) -> None:
        self._pins = manifest
        self._store = store
        self._lease = lease.take()
        self._scope: FxtrScope | None = None

    @contextlib.asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
        try:
            async with FxtrScope(name="namespace-read-view", isolated=True) as scope:
                # The store resolves by-id reads this scope has never seen
                # (case 1: the manager's store session outlives this view).
                scope.register_deep_fetch_source(
                    self._store, IndependentLocalBridge(lease=self._lease.clone())
                )
                self._scope = scope
                try:
                    yield self
                finally:
                    self._scope = None
        finally:
            self._lease.discard()

    @property
    def scope(self) -> FxtrScope:
        if self._scope is None:
            raise RuntimeError("NamespaceReadAdapter must be used as a context manager!")
        return self._scope

    async def list_pins(self, prefix: str = "") -> dict[str, FxtrID]:
        return {k: v for k, v in self._pins.items() if k.startswith(prefix)}

    async def _retrieve_impl[
        T: Entity
    ](
        self,
        key: str,
        type: type[T] | None,
        into_scope: FxtrScope | None,
        copy_into_scope: bool,
        allow_not_found: bool,
    ) -> (FxtrID | EntityRef[T] | None):
        fxtr_id = self._pins.get(key)
        if fxtr_id is None:
            if allow_not_found:
                return None
            raise KeyError(f"no pin named {key!r} in this namespace")

        scope = self.scope
        # Look the id up in the store directly (a ReadableArena branded on the
        # store's lifetime, matching our owned lease), then attach it into our own
        # scope as a lazy claim — the exact HttpFxtrReadNamespace dance, with the
        # store as the source. Transitive deps resolve through the deep-fetch
        # source registered on enter. The arena is a transient handle; close it
        # after the attach (the claim lives on the scope's main arena + bridge).
        arena = await self._store.try_lookup(fxtr_id)
        if arena is None:
            if allow_not_found:
                return None
            raise KeyError(f"pin {key!r} -> {fxtr_id.id_string()} not found in the repo store")
        scope._main_arena.lazily_attach_from_arena(
            scope._current_auth_lease().clone(),
            fxtr_id,
            arena,
            IndependentLocalBridge(lease=self._lease.clone()),
        )
        arena.close()

        if into_scope is not None:
            if copy_into_scope:
                await into_scope.copy_from_scope(fxtr_id, source_scope=scope)
            else:
                into_scope.lift(fxtr_id)

        return EntityRef(fxtr_id, type) if type is not None else fxtr_id
