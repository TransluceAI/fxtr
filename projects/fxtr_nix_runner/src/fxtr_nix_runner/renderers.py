"""Renderer presets (manager tier) — eval + build a flake's ``uiPresets``.

A flake declares dynamic-dispatch renderers under a preset attr (the
``fxtr.uiPresets.<name>`` convention), produced by ``lib.mkUiPreset``:
a type-id-string-keyed attrset where each entry is ``{ typeEntity, byDispatchKey
}`` and each dispatch-key route carries an ``esbuildRenderer`` derivation. This
module is the nix layer that turns such a preset into buildable outputs:

- :func:`eval_preset_table` — ``nix eval`` the preset through :data:`_PRESET_APPLY`,
  reshaping it into a JSON table that carries, per type id_string, the renderer +
  ``typeEntity`` layer **`.drvPath`s** (forces eval/instantiation, not realization
  — no builds yet) plus the plain metadata (subtype keys, dispatch-key uuids,
  ``layerKey``).
- :func:`build_drv_outputs` — one batched ``nix build "<drv>^out" …`` over the
  renderer + ``typeEntity`` drvs captured by the eval, materializing each output;
  returns ``{drv_path: Materialized}`` (the ``.mjs`` file / the layered-store dir).

Both take a transport :class:`~fxtr_nix_runner.Handle` and are fxtr2-free, so they
serve the workstation's nix/git dispatcher impl and this package's own renderer
read adapter alike. On failure they raise a plain :class:`RuntimeError` with the
nix stderr in the message (no stderr classification).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope
from fxtr2.data.layered_file_store import LayeredFileStoreReader, ParentPathRemap
from fxtr2.experimental.dynamic_dispatch import RendererEntity
from fxtr_nix_runner._backend import Handle, Materialized

if TYPE_CHECKING:
    from fxtr_nix_runner.manager import BuildSpan

# nix `--apply` function: reshape a `fxtr.uiPresets.<name>` attrset
# (type-id-string-keyed; each entry `{ typeEntity, byDispatchKey }`) into the JSON
# the caller decodes. `.drvPath` forces instantiation only (no realization), so the
# table keys + drv paths come back without any renderer / typeEntity build.
#
# Two `typeEntity` shapes (see `lib.fxtr.builtinEntity` / `mkEntityFromPython`): a
# `type == "builtin"` entity is a primitive FxtrID — no layer to build, so the entry
# carries its `idString` directly and the caller validates it without a build; a
# `fxtr_layer_entity` carries its layer's `.drvPath` + `layerKey` for the batched
# build + read-back validation.
_PRESET_APPLY = """
preset: builtins.mapAttrs (idStr: r:
  let
    te = r.typeEntity;
    byDispatchKey = builtins.mapAttrs (uuid: e: {
      inherit (e) subtypeKey;
      rendererDrv = e.renderer.drvPath;
    }) r.byDispatchKey;
  in
  if (te.type or "") == "builtin"
  then { inherit byDispatchKey; builtin = true; idString = te.idString; }
  else {
    inherit byDispatchKey;
    typeEntityLayerDrv = te.layer.drvPath;
    typeEntityLayerKey = te.layerKey;
  }) preset
"""


def _build_flake_ref(h: Handle, rev: str | None, flake_path: str | None = None) -> str:
    """Form the nix flake ref for the handle's repo.

    ``h.flake_ref_root()`` is the path to embed (the host path on the host backend,
    ``/workdir`` inside the builder on podman). A committed ``rev`` gives a frozen
    ``git+file://…?rev=…`` ref (``flake_path`` threaded in as ``&dir=``); ``rev is
    None`` reads the **live workdir** via a bare ``<root>`` path ref (non-frozen,
    nix resolves adaptively — the same form the entities/namespace adapters use).
    """
    root = h.flake_ref_root()
    if rev is None:
        # Live workdir. flake_path on a path ref is a plain subdir.
        return f"{root}/{flake_path}" if flake_path else root
    ref = f"git+file://{root}?rev={rev}"
    if flake_path:
        ref += f"&dir={flake_path}"
    return ref


async def _run_nix(h: Handle, args: list[str], *, step: str, context: str) -> str:
    """Run ``nix <step> <args>`` through the backend; return stdout, or raise
    ``RuntimeError``."""
    result = await h.run_nix([step, *args])
    if result.returncode != 0:
        raise RuntimeError(
            f"nix {step} failed for {context} (exit {result.returncode}):\n{result.stderr}"
        )
    return result.stdout


async def eval_preset_table(
    h: Handle, rev: str | None, attr: str, flake_path: str | None = None
) -> dict[str, Any]:
    """``nix eval --json`` the preset attr through :data:`_PRESET_APPLY`.

    ``attr`` is the preset attr path, e.g. ``"fxtr.uiPresets.my_preset"``.
    ``rev`` is a committed rev (frozen git ref) or ``None`` for the live workdir.
    Returns the decoded table ``{type_id_string: {typeEntityLayerDrv,
    typeEntityLayerKey, byDispatchKey: {uuid: {subtypeKey, rendererDrv}}}}`` —
    except a *builtin*-typeEntity entry carries ``{builtin: true, idString,
    byDispatchKey: …}`` instead (no layer drv to build; the caller validates
    ``idString`` directly). No renderer / typeEntity build is forced — only
    ``.drvPath`` instantiation. Raises ``RuntimeError`` on any eval failure (incl. a
    malformed preset shape that trips the apply).
    """
    ref = f"{_build_flake_ref(h, rev, flake_path)}#{attr}"
    out = await _run_nix(
        h,
        ["--json", ref, "--apply", _PRESET_APPLY, "--no-update-lock-file"],
        step="eval",
        context=ref,
    )
    return json.loads(out)


async def build_drv_outputs(h: Handle, drv_paths: list[str]) -> dict[str, Materialized]:
    """``nix build "<drv>^out" … --json`` over a batch, then materialize each
    output; return ``{drv: Materialized}``.

    ``drv_paths`` are ``.drv``s captured by :func:`eval_preset_table` (renderer ESMs
    and/or ``typeEntity`` layers). A single ``nix build`` lets nix schedule the
    builds in parallel (its own ``max-jobs``); ``--json`` emits one ``{"drvPath",
    "outputs": {"out": …}}`` record per installable, so the ``drvPath → out``
    mapping is unambiguous (no reliance on output order). Each output is then
    ``materialize``d so it's host-readable: the caller reads renderer ``.mjs`` bytes
    from ``Materialized.host_path`` and opens a layered-store reader over a
    ``typeEntity`` layer's ``host_path`` (threading its ``remap``). ``--no-link`` (no
    ``result`` symlinks). Building realized ``.drv``s involves no flake locking, so
    no ``--no-update-lock-file``. Returns ``{}`` for an empty input; raises
    ``RuntimeError`` on any failure.
    """
    if not drv_paths:
        return {}
    installables = [f"{d}^out" for d in drv_paths]
    out = await _run_nix(
        h,
        ["--no-link", "--json", *installables],
        step="build",
        context=f"{len(drv_paths)} derivation(s)",
    )
    result: dict[str, Materialized] = {}
    for rec in json.loads(out):
        result[rec["drvPath"]] = await h.materialize(rec["outputs"]["out"], closure=True)
    return result


# ── Renderer read adapter (manager tier, fxtr2-aware) ───────────────────────
# Turns a built preset into a servable table of RendererEntity refs, mirroring
# fxtr_workstation's NixConfigurableDispatchImpl.set_preset (build → validate →
# mint), but bound to a manager BuildSpan and minting into a caller scope.

#: The flake attr path presets are declared under (the convention `lib.mkUiPreset`
#: produces).
_PRESET_ATTR_PREFIX = "fxtr.uiPresets"

#: Names the declared presets (``{}`` if the attr path is absent).
_PRESET_NAMES_APPLY = "f: builtins.attrNames (f.uiPresets or {})"


class RendererPresetError(Exception):
    """A renderer preset is malformed — e.g. a key not matching its typeEntity id."""


@dataclass(frozen=True)
class RendererEntry:
    """One resolved renderer route: the opaque ``subtype_key`` + the renderer ref.

    The serving dispatcher returns this on the wire as ``{"subtype_key", "renderer"}``
    (the renderer ref escapes to an entity tag the frontend loads via ``/data``).
    """

    subtype_key: str
    renderer: EntityRef[RendererEntity]


#: ``type_id_string -> dispatch_key_uuid -> RendererEntry`` — what a dispatcher
#: ``resolve(type_ref, key_uuid)`` looks up.
RendererTable = dict[str, dict[str, RendererEntry]]


async def eval_preset_names(span: BuildSpan, rev: str | None, prefix: str = "") -> list[str]:
    """The declared ``fxtr.uiPresets`` names (cheap eval, no builds).

    The renderer analogue of :func:`~fxtr_nix_runner.namespaces.eval_namespace_names`;
    surfaced as :meth:`FlakeManager.list_preset_names`.
    """
    ref = f"{_build_flake_ref(span.handle, rev)}#fxtr"
    out = await _run_nix(
        span.handle,
        ["--json", ref, "--apply", _PRESET_NAMES_APPLY, "--no-update-lock-file"],
        step="eval",
        context=ref,
    )
    names: list[str] = json.loads(out)
    return [n for n in names if n.startswith(prefix)]


async def _validate_type_entity(
    type_id_str: str, layer: Materialized, layer_key: str, attr: str
) -> None:
    """Read the (built, materialized) typeEntity layer; assert its id matches the key.

    Catches a stale/hand-pasted preset key. Mismatch → :class:`RendererPresetError`
    (rejects the whole preset)."""
    remap = ParentPathRemap(*layer.remap) if layer.remap is not None else None
    async with LayeredFileStoreReader(layer.host_path, parent_path_remap=remap) as reader:
        type_fid = await reader.retrieve(layer_key)
    actual = type_fid.id_string()
    if actual != type_id_str:
        raise RendererPresetError(
            f"preset key {type_id_str!r} does not match its typeEntity id {actual!r} "
            f"(attr={attr!r}); re-paste the type's id_string."
        )


def _validate_builtin(type_id_str: str, builtin_id: str, attr: str) -> None:
    """A ``builtin`` typeEntity has no layer — its idString IS the type; assert it
    matches the preset key. Sync, no I/O."""
    if builtin_id != type_id_str:
        raise RendererPresetError(
            f"preset key {type_id_str!r} does not match its builtin entity idString "
            f"{builtin_id!r} (attr={attr!r})."
        )


async def build_preset_table(
    span: BuildSpan,
    scope: FxtrScope,
    rev: str | None,
    preset_name: str,
    *,
    flake_path: str | None = None,
) -> RendererTable:
    """Build ``fxtr.uiPresets.<preset_name>`` into a servable table.

    Evals the preset (drv paths), builds the renderer + typeEntity drvs in one batch,
    validates each typeEntity id against its preset key, mints a :class:`RendererEntity`
    per renderer into ``scope``, and returns the :data:`RendererTable`.

    ``scope`` must make ``RENDERER_ENTITY_TYPE_ID``'s descriptor reachable (include
    ``GLOBAL_SCOPE``) and is where the renderer blobs live (served via ``/data``).
    Raises :class:`RendererPresetError` on a key↔typeEntity mismatch; ``RuntimeError``
    on any nix eval/build failure.
    """
    attr = f"{_PRESET_ATTR_PREFIX}.{preset_name}"
    table_json = await eval_preset_table(span.handle, rev, attr, flake_path)

    renderer_drvs = {
        route["rendererDrv"]
        for entry in table_json.values()
        for route in entry["byDispatchKey"].values()
    }
    type_entity_drvs = {
        entry["typeEntityLayerDrv"] for entry in table_json.values() if not entry.get("builtin")
    }
    outs = await build_drv_outputs(span.handle, sorted(renderer_drvs | type_entity_drvs))

    for type_id_str, entry in table_json.items():
        if entry.get("builtin"):
            _validate_builtin(type_id_str, entry["idString"], attr)
        else:
            await _validate_type_entity(
                type_id_str, outs[entry["typeEntityLayerDrv"]], entry["typeEntityLayerKey"], attr
            )

    # One RendererEntity per distinct renderer drv (content-addressed, so a renderer
    # shared across dispatch keys / types dedups to a single entity).
    renderer_refs: dict[str, EntityRef[RendererEntity]] = {
        drv: RendererEntity(module=outs[drv].host_path.read_bytes()).store(scope)
        for drv in renderer_drvs
    }

    return {
        type_id_str: {
            key_uuid: RendererEntry(
                subtype_key=route["subtypeKey"],
                renderer=renderer_refs[route["rendererDrv"]],
            )
            for key_uuid, route in entry["byDispatchKey"].items()
        }
        for type_id_str, entry in table_json.items()
    }
