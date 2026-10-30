"""The entities read adapter (manager tier) — a ``FxtrReadNamespace``-*shaped*
view over a flake's ``fxtr.entities.*``.

This adapter follows the same pattern the MCP's ``retrieve_nix_entity`` uses
(eval one entity's metadata → build ``<attr>.layer`` → ``materialize``), then
hands the built layer to :meth:`FlakeManager.mount_pin`, which mounts the pin's
entity into the **manager's long-lived scope**. The entity stays lazily loadable
for the manager's lifetime: local bytes from the materialized layer on disk,
remote-pointer closures from the repo store (the manager brands the layer arena
on the store and threads the store lease through). The eval/build logic is
reimplemented here rather than shared because the runner can't import the MCP
(that would be an import cycle).

It is *shaped like* ``FxtrReadNamespace`` (``retrieve`` / ``try_retrieve``) but
deliberately not a subclass: there is no id-valued ``list_pins`` — resolving an
``fxtr.entities`` name to a ``FxtrID`` needs a per-entity build — only a cheap
``list_entity_names`` (eval, no builds).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import Entity, EntityRef, FxtrScope
from fxtr2.data.layered_file_store import ParentPathRemap
from fxtr_nix_runner._backend import Materialized

if TYPE_CHECKING:
    from fxtr_nix_runner.manager import BuildSpan, FlakeManager


# The --apply for one entity attr's metadata (mirrors the MCP's nix_util shape).
# Touching `.type` (+ `.layerKey` / `.idString`) also *validates* the attr is
# entity-shaped; `.layer` is never touched here, so no build is forced.
_ENTITY_METADATA_APPLY = (
    "ent: { inherit (ent) type; } // "
    '(if (ent.type or "") == "builtin" '
    "then { idString = ent.idString; } "
    "else { inherit (ent) layerKey; })"
)

#: Default flake attr path the adapter reads entities under.
_DEFAULT_NIX_LOCATION = "fxtr.entities"


def _names_apply(nix_location: str) -> str:
    """The ``--apply`` for ``list_entity_names`` at ``nix_location``.

    The flake ref selects ``#fxtr``, so this navigates the tail after ``fxtr``
    gracefully (``or {}`` at each step → ``[]`` if the location is absent). For
    the default ``fxtr.entities`` this is ``f: builtins.attrNames (f.entities or
    {})``. ``nix_location`` must be under ``fxtr`` (the flake's fxtr attrset)."""
    parts = nix_location.split(".")
    if parts[0] != "fxtr":
        raise NixEntityError(f"nix_location must be under 'fxtr', got {nix_location!r}")
    expr = "f"
    for p in parts[1:]:
        expr = f"({expr}.{p} or {{}})"
    return f"f: builtins.attrNames {expr}"


class NixEntityError(Exception):
    """A ``nix eval`` / ``nix build`` for an entity attr exited non-zero."""

    def __init__(self, message: str, *, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


class EntitiesReadAdapter:
    """A ``fxtr.entities.*`` read view bound to a build span + its manager.

    Construct via :meth:`FlakeManager.entities`. Reads the flake at the
    manager's ``rev`` (committed) or the live workdir (``rev is None``);
    retrieved entities are mounted into the manager's scope and resolve their
    bytes through the repo store.
    """

    def __init__(
        self,
        *,
        span: BuildSpan,
        manager: FlakeManager,
        rev: str | None,
    ) -> None:
        self._span = span
        self._manager = manager
        self._rev = rev

    # -- flake ref + nix shell-outs ----------------------------------------

    def _flake_ref(self, attr: str) -> str:
        root = self._span.handle.flake_ref_root()
        if self._rev is not None:
            return f"git+file://{root}?rev={self._rev}#{attr}"
        # Live workdir: bare path lets nix resolve adaptively (git+file:// inside
        # a repo, path: otherwise), non-frozen — same as the MCP's data loop.
        return f"{root}#{attr}"

    async def list_entity_names(
        self, prefix: str = "", *, nix_location: str = _DEFAULT_NIX_LOCATION
    ) -> list[str]:
        """The attr names under ``nix_location`` (default ``fxtr.entities``; cheap
        eval, no builds).

        These are the adapter's "pin names"; resolving one to a ``FxtrID``
        happens only when :meth:`retrieve` is called (and may build a layer).
        """
        result = await self._span.run_nix(
            ["eval", "--json", self._flake_ref("fxtr"), "--apply", _names_apply(nix_location)]
        )
        if result.returncode != 0:
            raise NixEntityError(f"nix eval of {nix_location} names failed", stderr=result.stderr)
        names: list[str] = json.loads(result.stdout)
        return [n for n in names if n.startswith(prefix)]

    async def _eval_metadata(self, name: str, nix_location: str) -> dict[str, Any]:
        attr = f"{nix_location}.{name}"
        result = await self._span.run_nix(
            ["eval", "--json", self._flake_ref(attr), "--apply", _ENTITY_METADATA_APPLY]
        )
        if result.returncode != 0:
            raise NixEntityError(f"nix eval of {attr} failed", stderr=result.stderr)
        return json.loads(result.stdout)

    async def _build_layer(self, name: str, nix_location: str) -> Materialized:
        attr = f"{nix_location}.{name}.layer"
        # The rehost env is wired into run_nix by the build span, so a preload-FOD
        # layer can fetch entity bytes from the repo store during this build.
        result = await self._span.run_nix(
            ["build", "--no-link", "--print-out-paths", self._flake_ref(attr)]
        )
        if result.returncode != 0:
            raise NixEntityError(f"nix build of {attr} failed", stderr=result.stderr)
        stdout = result.stdout.strip()
        if not stdout:
            raise NixEntityError(f"nix build of {attr} printed no store path")
        store_path = stdout.splitlines()[-1]
        return await self._span.handle.materialize(store_path, closure=True)

    # -- retrieve / try_retrieve -------------------------------------------

    async def retrieve(
        self,
        name: str,
        type: type[Entity] | None = None,
        *,
        nix_location: str = _DEFAULT_NIX_LOCATION,
        into_scope: FxtrScope | None = None,
        copy_into_scope: bool = True,
    ) -> FxtrID | EntityRef[Any]:
        """Retrieve ``<nix_location>.<name>``, raising ``KeyError`` if absent.

        ``nix_location`` defaults to ``fxtr.entities``; pass another flake attr
        path (under ``fxtr``) to read an entity declared elsewhere — the MCP's
        ``retrieve_nix_entity`` threads its caller-supplied location through here.
        """
        result = await self._retrieve_impl(
            name, type, nix_location, into_scope, copy_into_scope, False
        )
        assert result is not None
        return result

    async def try_retrieve(
        self,
        name: str,
        type: type[Entity] | None = None,
        *,
        nix_location: str = _DEFAULT_NIX_LOCATION,
        into_scope: FxtrScope | None = None,
        copy_into_scope: bool = True,
    ) -> FxtrID | EntityRef[Any] | None:
        """Like :meth:`retrieve`, but returns ``None`` if the name is absent."""
        return await self._retrieve_impl(
            name, type, nix_location, into_scope, copy_into_scope, True
        )

    async def _retrieve_impl(
        self,
        name: str,
        type_: type[Entity] | None,
        nix_location: str,
        into_scope: FxtrScope | None,
        copy_into_scope: bool,
        allow_not_found: bool,
    ) -> FxtrID | EntityRef[Any] | None:
        # Presence check via the cheap names eval — cleanly separates
        # "no such entity" from "the flake/attr failed to evaluate".
        if name not in await self.list_entity_names(nix_location=nix_location):
            if allow_not_found:
                return None
            raise KeyError(f"no entity named {name!r} in {nix_location}")

        meta = await self._eval_metadata(name, nix_location)
        if meta.get("type") == "builtin":
            fxtr_id = FxtrID.from_id_string(meta["idString"])
            tag = fxtr_id.get_tag()
            if tag not in ("!", "."):
                # A content-addressed id smuggled in as a "builtin" needs a real
                # layer entity, not a bare id (mirrors the MCP's guard).
                raise NixEntityError(
                    f"builtin entity {name} resolved to id {meta['idString']!r} with "
                    f"tag {tag!r}; only primitive '!'/'.' ids may be builtins."
                )
            # Primitive ids carry no content/arena lifetime — nothing to copy.
            return EntityRef(fxtr_id, type_) if type_ is not None else fxtr_id

        mat = await self._build_layer(name, nix_location)
        remap = ParentPathRemap(*mat.remap) if mat.remap is not None else None
        try:
            # Mount the built layer's pin into the manager scope — lazily
            # loadable for the manager's lifetime (local from the layer on disk,
            # remote-pointer closures from the repo store).
            fxtr_id = self._manager.mount_pin(mat.host_path, meta["layerKey"], remap=remap)
        except KeyError as exc:
            raise NixEntityError(
                f"layerKey {meta['layerKey']!r} not found in the built layer for {name}"
            ) from exc

        # `into_scope` is optional sugar: the entity already lives in the manager
        # scope. copy_into_scope=True gives an independent eager copy; =False
        # lifts (requires `into_scope` to inherit the manager scope).
        if into_scope is not None:
            if copy_into_scope:
                await into_scope.copy_from_scope(fxtr_id, source_scope=self._manager.scope)
            else:
                into_scope.lift(fxtr_id)

        return EntityRef(fxtr_id, type_) if type_ is not None else fxtr_id
